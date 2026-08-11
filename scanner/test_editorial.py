import json
import sqlite3
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402


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


class EditorialDeskTests(unittest.TestCase):
    def test_valid_agent_review_passes(self):
        ok, errors = main.validate_agent_review_payload(valid_review())
        self.assertTrue(ok)
        self.assertEqual(errors, [])

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


if __name__ == "__main__":
    unittest.main()
