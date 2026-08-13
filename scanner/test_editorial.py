import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scanner  # noqa: E402


def valid_review() -> dict:
    reviewed = []
    for rank in range(1, 5):
        reviewed.append(
            {
                "lead_id": f"news:test-story-{rank}",
                "scanner_type": "news",
                "agent_rating": "Strong" if rank <= 2 else "Possible",
                "concise_reason": "A current development with usable evidence.",
                "mfo_angle": "Australian men's fitness angle.",
                "evidence_risk": "Primary source plus independent support.",
                "archive_overlap_warning": "No direct overlap.",
                "editorial_rank": rank,
                "why_editorial_ranking_differs": "Editorial fit is stronger than raw scanner position.",
                "recommended_action": "commission",
                "facts_to_check": ["Confirm date."],
            }
        )
    reviewed.append(
        {
            "lead_id": "creator:weak-story",
            "scanner_type": "creator",
            "agent_rating": "Weak",
            "concise_reason": "Popular but thin as an MFO article.",
            "mfo_angle": "Only usable if a practical training angle emerges.",
            "evidence_risk": "Video title needs verification.",
            "archive_overlap_warning": "No exact overlap.",
            "editorial_rank": None,
            "why_editorial_ranking_differs": "Raw velocity does not create a defensible story.",
            "recommended_action": "reject",
            "facts_to_check": [],
        }
    )
    return {
        "recommended_ids": [f"news:test-story-{rank}" for rank in range(1, 5)],
        "reviewed_candidates": reviewed,
        "excluded_notes": [{"lead_id": "creator:duplicate", "reason": "Already covered."}],
    }


def valid_article() -> dict:
    return {
        "headline": "Strength Training Study Shows Practical Benefits",
        "slug": "strength-training-study-practical-benefits",
        "excerpt": "A concise summary of a new strength training finding.",
        "article_html": "<p>This is the full article with attributed evidence.</p>",
        "seo_title": "Strength Training Study Practical Benefits",
        "meta_description": "What the new strength training study means for Australian men.",
        "focus_keyphrase": "strength training study",
        "related_keyphrases": ["resistance training", "men over 40"],
        "tags": ["Strength Training", "Research"],
        "category_suggestion": "Fitness",
        "source_attribution": [{"title": "Study", "url": "https://example.com/study"}],
        "facts_checked": ["Publication date confirmed."],
        "risks_disclosures": ["Findings may not apply to all populations."],
        "internal_links": [{"url": "https://mensfitnessonline.com.au/example/"}],
        "embed_media_notes": ["No video embed required."],
    }


class EditorialDeskTests(unittest.TestCase):
    def test_valid_agent_review_passes(self):
        ok, errors = main.validate_agent_review_payload(valid_review())
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_current_response_importer_stays_backward_compatible(self):
        review = valid_review()
        for item in review["reviewed_candidates"]:
            item["primary_source_url"] = "https://example.com/original"
        ok, errors = main.validate_agent_review_payload(review)
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_bad_primary_source_url_is_reported(self):
        review = valid_review()
        review["reviewed_candidates"][0]["primary_source_url"] = ""
        ok, errors = main.validate_agent_review_payload(review)
        self.assertFalse(ok)
        self.assertTrue(any("primary_source_url" in error for error in errors))

    def test_malformed_agent_review_reports_useful_errors(self):
        ok, errors = main.validate_agent_review_payload({"recommendations": [{}]})
        self.assertFalse(ok)
        self.assertTrue(any("recommended_ids" in error for error in errors))
        self.assertTrue(any("reviewed_candidates" in error for error in errors))

    def test_recommended_ids_must_be_reviewed_candidates(self):
        review = valid_review()
        review["recommended_ids"].append("news:missing")
        ok, errors = main.validate_agent_review_payload(review)
        self.assertFalse(ok)
        self.assertTrue(any("news:missing" in error for error in errors))

    def test_review_packet_markdown_contains_prompt_and_sources(self):
        packet = {
            "generated_at": "2026-08-10T00:00:00+00:00",
            "editorial_supervisor_prompt": "You are the supervising editor",
            "required_response_schema": {"type": "object"},
            "scanner_metadata": {"creator": {}, "news": {}},
            "review_candidates": {
                "creator": [{"lead_id": "creator:abc"}],
                "news": [{"lead_id": "news:def"}],
                "research": [{"lead_id": "research:12345"}],
                "manual": [{"lead_id": "manual:1"}],
            },
            "excluded_candidates": {
                "creator": [{"lead_id": "creator:excluded"}],
                "news": [],
                "research": [],
                "manual": [],
            },
            "archive_overlap_information": {"page_count": 500},
            "editorial_source_information": {"sources": []},
        }
        rendered = main.render_review_packet_markdown(packet)
        self.assertIn("You are the supervising editor", rendered)
        self.assertIn("creator:abc", rendered)
        self.assertIn("news:def", rendered)
        self.assertIn("research:12345", rendered)
        self.assertIn("manual:1", rendered)
        self.assertIn("creator:excluded", rendered)

    def test_decision_tables_persist_rows(self):
        conn = sqlite3.connect(":memory:")
        try:
            main.ensure_editorial_tables(conn)
            conn.execute(
                """
                INSERT INTO editorial_decisions (lead_id, decision, note, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                ("creator:abc", "hold", "needs check", "2026-08-10T00:00:00+00:00"),
            )
            row = conn.execute(
                "SELECT decision, note FROM editorial_decisions WHERE lead_id = ?",
                ("creator:abc",),
            ).fetchone()
            self.assertEqual(row, ("hold", "needs check"))
        finally:
            conn.close()

    def test_invalid_timestamp_is_stale(self):
        status = main.report_age_status("not-a-date")
        self.assertTrue(status["is_stale"])
        self.assertIn("could not be read", status["warning"])

    def test_scanner_error_is_concise(self):
        message = main.concise_scanner_error(
            "",
            'Traceback\n  File "x", line 1\nKeyboardInterrupt',
        )
        self.assertEqual(message, "Scanner was interrupted before completion.")

    def test_valid_article_json_imports_successfully(self):
        ok, errors = main.validate_article_payload(valid_article())
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_malformed_article_json_reports_useful_errors(self):
        article = valid_article()
        article.pop("article_html")
        article["tags"] = "fitness"
        ok, errors = main.validate_article_payload(article)
        self.assertFalse(ok)
        self.assertTrue(any("article_html" in error for error in errors))
        self.assertTrue(any("tags must be a list" in error for error in errors))

    def test_draft_payload_contains_required_wordpress_fields(self):
        payload, yoast_status = main.create_wp_draft_payload(
            valid_article(),
            tag_ids=[10, 11],
            category_ids=[2],
            yoast_keys=set(),
        )
        self.assertEqual(payload["status"], "draft")
        self.assertEqual(payload["title"], valid_article()["headline"])
        self.assertEqual(payload["content"], valid_article()["article_html"])
        self.assertEqual(payload["excerpt"], valid_article()["excerpt"])
        self.assertEqual(payload["slug"], valid_article()["slug"])
        self.assertEqual(payload["tags"], [10, 11])
        self.assertEqual(payload["categories"], [2])
        self.assertEqual(yoast_status, "manual_copy_required")

    def test_draft_payload_applies_yoast_when_meta_fields_are_registered(self):
        payload, yoast_status = main.create_wp_draft_payload(
            valid_article(),
            tag_ids=[],
            category_ids=[],
            yoast_keys={"_yoast_wpseo_title", "_yoast_wpseo_metadesc", "_yoast_wpseo_focuskw"},
        )
        self.assertEqual(yoast_status, "applied")
        self.assertEqual(payload["meta"]["_yoast_wpseo_focuskw"], "strength training study")

    def test_missing_wordpress_env_vars_block_draft_creation_clearly(self):
        with mock.patch.dict(main.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                main.wp_config()
        self.assertIn("Missing WordPress environment variables", str(ctx.exception))

    def test_failed_wordpress_response_stores_failed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main, "SCANNER_DB", Path(tmp) / "scanner.db"):
                main.save_editorial_decision(main.EditorialDecisionRequest(lead_id="news:test", decision="commission"))
                main.import_article("news:test", main.ArticleImportRequest(article=valid_article()))
                with mock.patch.object(main, "create_wordpress_draft", side_effect=RuntimeError("WP unavailable")):
                    with self.assertRaises(main.HTTPException):
                        main.create_wordpress_draft_for_queue_item("news:test")
                queue = main.list_production_queue()
                self.assertEqual(queue["news:test"]["status"], "wp_draft_failed")
                self.assertIn("WP unavailable", queue["news:test"]["wp_error"])

    def test_successful_draft_stores_draft_id_and_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main, "SCANNER_DB", Path(tmp) / "scanner.db"):
                main.save_editorial_decision(main.EditorialDecisionRequest(lead_id="news:test", decision="commission"))
                main.import_article("news:test", main.ArticleImportRequest(article=valid_article()))
                draft = {
                    "wp_draft_id": 123,
                    "wp_draft_url": "https://mensfitnessonline.com.au/?p=123",
                    "wp_edit_url": "https://mensfitnessonline.com.au/wp-admin/post.php?post=123&action=edit",
                    "wp_yoast_status": "manual_copy_required",
                    "payload": {},
                }
                with mock.patch.object(main, "create_wordpress_draft", return_value=draft):
                    result = main.create_wordpress_draft_for_queue_item("news:test")
                queue = main.list_production_queue()
                self.assertEqual(result["wp_draft_id"], 123)
                self.assertEqual(queue["news:test"]["status"], "wp_draft_created")
                self.assertEqual(queue["news:test"]["wp_draft_url"], draft["wp_draft_url"])

    def test_fixture_workflow_persists_after_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main, "SCANNER_DB", Path(tmp) / "scanner.db"):
                lead = {"lead_id": "news:test", "title": "Test story", "source_url": "https://example.com"}
                main.save_editorial_decision(main.EditorialDecisionRequest(lead_id="news:test", decision="commission"))
                packet = main.prepare_writing_packet("news:test", main.WritingPacketRequest(lead=lead, assessment={"mfo_angle": "Angle"}))
                main.import_article("news:test", main.ArticleImportRequest(article=valid_article(), lead=lead))
                self.assertEqual(packet["status"], "writing_packet_prepared")
                queue = main.list_production_queue()
                self.assertEqual(queue["news:test"]["status"], "article_imported")
                self.assertEqual(queue["news:test"]["source_lead"]["title"], "Test story")

    def test_manual_news_duplicate_keeps_manual_candidate(self):
        alcohol_url = "https://www.sciencedaily.com/releases/2026/08/260806100000.htm"
        groups = {
            "manual": [
                {
                    "lead_id": "manual:1",
                    "scanner_type": "manual",
                    "title": f"Editor supplied alcohol lead {alcohol_url}",
                    "source_url": alcohol_url,
                    "source_fingerprints": [f"url:{main.normalise_packet_url(alcohol_url)}"],
                    "status": "manual",
                }
            ],
            "news": [
                {
                    "lead_id": "news:alcohol",
                    "scanner_type": "news",
                    "title": "ScienceDaily alcohol report",
                    "source_url": alcohol_url,
                    "source_fingerprints": [f"url:{main.normalise_packet_url(alcohol_url)}"],
                    "status": "ranked",
                    "editorial_opportunity_score": 80,
                }
            ],
            "creator": [],
            "research": [],
        }
        deduped, notes = main.dedupe_packet_candidates(groups)
        self.assertEqual([lead["lead_id"] for lead in deduped["manual"]], ["manual:1"])
        self.assertEqual(deduped["news"], [])
        self.assertEqual(notes[0]["merged_lead_id"], "news:alcohol")

    def test_source_fingerprint_helpers_cover_urls_google_youtube_pmid_and_doi(self):
        google_url = "https://news.google.com/rss/articles/test?url=https%3A%2F%2Fexample.com%2Fstory%3Futm_source%3Dx&oc=5"
        self.assertEqual(scanner.canonical_url(google_url), "https://example.com/story")
        self.assertIn("youtube:abc123DEF", scanner.fingerprints_for_values("https://youtu.be/abc123DEF"))
        self.assertIn("pmid:42576331", scanner.fingerprints_for_values("PMID: 42576331"))
        self.assertIn("doi:10.1000/example", scanner.fingerprints_for_values("doi:10.1000/example"))


if __name__ == "__main__":
    unittest.main()
