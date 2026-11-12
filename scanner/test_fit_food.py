"""Fit-food pass: signal filtering, link repair, the two modes, comparison
and digest formatting. No network, no API calls."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest  # noqa: E402
from trend_radar import claude_fit_food as ff  # noqa: E402
from trend_radar.claude_evidence import EvidenceRecord  # noqa: E402
from trend_radar.claude_scan import add_fit_food_cost  # noqa: E402
from trend_radar.claude_schemas import FitFoodOutput, FitFoodPick  # noqa: E402

SITE = "https://mensfitnessonline.com.au"
BRIEF = "Existing posts: mensfitnessonline.com.au/high-protein-foods/, /wpc-vs-wpi/.\nAt most 3 picks."


def rec(i, title, url=None, when="2026-11-10T00:00:00Z"):
    return EvidenceRecord(signal_id=f"id{i}", lane="publisher", platform="rss", source="Src", published_at=when,
                          timestamp_precision="day", title=title, body_excerpt="", url=url or f"https://news.example/{i}",
                          discovery_method="feed")


def pick(**kw):
    base = {"headline": "Whey prices jump", "why_care": "Your tub costs more", "first_published": "2026-11-10",
            "original_source_url": "https://abc.net.au/whey", "original_source_opened": True,
            "other_outlet_url": "https://news.example/2", "facts": "ABC report", "search_term": "whey price",
            "format": "news post", "fit_score": 8, "risk": "Single outlet."}
    base.update(kw)
    return FitFoodPick(**base)


class FilterTests(unittest.TestCase):
    def test_food_signals_match_word_starts_newest_first(self):
        evidence = [rec(1, "Creatine and sleep study", when="2026-11-08T00:00:00Z"),
                    rec(2, "Cricket recall for Smith"),             # 'recall' matches: word start
                    rec(3, "Protein coffee lands in Woolworths", when="2026-11-11T00:00:00Z"),
                    rec(4, "Marathon world record")]
        got = ff.food_signals(evidence, ["creatine", "protein", "recall"])
        self.assertEqual([e.signal_id for e in got], ["id3", "id2", "id1"])

    def test_whole_words_with_plurals_and_star_prefixes(self):
        match = ff._matcher(["gut", "egg", "contaminat*"])
        self.assertTrue(match("Gut health and training"))
        self.assertTrue(match("Free-range eggs recalled"))
        self.assertTrue(match("Protein powder contamination found"))
        self.assertFalse(match("Nancy Guthrie update"))
        self.assertFalse(match("Can gutter guards help?"))
        self.assertFalse(match("Eggplant parmigiana"))

    def test_body_counts_only_for_food_only_sources(self):
        def r(i, source_id):
            e = rec(i, "Weekly roundup")
            e.body_excerpt = "new creatine trial results"
            e.source_id = source_id
            return e

        got = ff.food_signals([r(1, "food:nutraingredients"), r(2, "publisher:mens_journal")], ["creatine"],
                              is_food_source=ff.fit_food_source_test({"dedicated_sources": ["food:nutraingredients"]}))
        self.assertEqual([e.signal_id for e in got], ["id1"])

    def test_news_scan_never_sees_fit_food_only_sources(self):
        keep = ff.news_source_filter({"dedicated_sources": ["food:fsanz_recalls"]})
        self.assertFalse(keep("food:fsanz_recalls"))
        self.assertFalse(keep("beat:fit_food:whey-protein"))
        self.assertTrue(keep("beat:hyrox:hyrox-australia"))
        self.assertTrue(keep("research:sciencedaily_nutrition"))

    def test_brief_pages_accept_full_and_slug_forms(self):
        self.assertEqual(ff.brief_page_urls(BRIEF), {f"{SITE}/high-protein-foods/", f"{SITE}/wpc-vs-wpi/"})

    def test_recent_posts_cut_at_14_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "i.json"
            index.write_text(json.dumps({"pages": [
                {"title": "New", "url": f"{SITE}/new/", "date": "2026-11-10T08:00:00"},
                {"title": "Old", "url": f"{SITE}/old/", "date": "2026-10-01T08:00:00"}]}))
            got = ff.recent_posts(index, now=datetime(2026, 11, 12))
        self.assertEqual([p["title"] for p in got], ["New"])


class RepairTests(unittest.TestCase):
    def test_unseen_links_are_dropped_and_flagged(self):
        out = FitFoodOutput(summary="s", picks=[pick(original_source_url="https://made.up/paper",
                                                     update_url=f"{SITE}/guessed/", format="add to existing post")])
        notes = ff.repair_picks(out, {"https://news.example/2"}, {f"{SITE}/wpc-vs-wpi/"})
        p = out.picks[0]
        self.assertIsNone(p.original_source_url)
        self.assertFalse(p.original_source_opened)
        self.assertIn("not verified", p.risk)
        self.assertEqual(p.other_outlet_url, "https://news.example/2")   # seen, kept
        self.assertIsNone(p.update_url)
        self.assertEqual(p.format, "news post")
        self.assertEqual(len(notes), 2)

    def test_seen_links_survive_trailing_slash_and_case(self):
        out = FitFoodOutput(summary="s", picks=[pick(update_url=f"{SITE}/WPC-vs-wpi", format="add to existing post")])
        ff.repair_picks(out, {"https://ABC.net.au/whey/", "https://news.example/2"}, {f"{SITE}/wpc-vs-wpi/"})
        self.assertEqual(out.picks[0].original_source_url, "https://abc.net.au/whey")
        self.assertEqual(out.picks[0].update_url, f"{SITE}/WPC-vs-wpi")

    def test_at_most_three_picks(self):
        out = FitFoodOutput(summary="s", picks=[pick(headline=f"p{i}") for i in range(5)])
        ff.repair_picks(out, {"https://abc.net.au/whey", "https://news.example/2"}, set())
        self.assertEqual(len(out.picks), 3)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text(BRIEF)
        self.index = Path(self.tmp.name) / "i.json"
        self.index.write_text(json.dumps({"pages": [{"title": "WPC vs WPI", "url": f"{SITE}/wpc-vs-wpi/", "slug": "wpc-vs-wpi"}]}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_scanner_mode_gets_signals_web_only_does_not_and_a_failure_is_contained(self):
        seen_inputs = {}

        def research(client, *, user_content, usage, **kw):
            mode = "scanner" if "SIGNALS THE SCANNER COLLECTED" in user_content else "web_only"
            seen_inputs[mode] = user_content
            if mode == "web_only":
                raise RuntimeError("search down")
            return "notes", [{"url": "https://abc.net.au/whey"}]

        def structured(client, *, user_content, usage, **kw):
            self.assertIn("RESEARCH NOTES\n\nnotes", user_content)
            return FitFoodOutput(summary="one pick", picks=[pick()])

        evidence = [rec(1, "Whey protein prices", url="https://news.example/2"), rec(2, "Marathon record")]
        with mock.patch.object(ff, "call_web_research", side_effect=research), \
                mock.patch.object(ff, "call_structured", side_effect=structured):
            result = ff.run_fit_food(None, model="claude-sonnet-5-5", evidence=evidence, ledger=None,
                                     config={"signal_keywords": ["whey"], "mfo_page_keywords": ["wpc"], "compare": True},
                                     brief_path=self.brief, index_path=self.index)
        self.assertIn("Whey protein prices", seen_inputs["scanner"])
        self.assertNotIn("Marathon record", seen_inputs["scanner"])
        self.assertNotIn("SIGNALS THE SCANNER COLLECTED", seen_inputs["web_only"])
        self.assertIn("FIT FOOD BRIEF", seen_inputs["web_only"])
        self.assertEqual(result["scanner"]["picks"][0]["other_outlet_url"], "https://news.example/2")  # a signal URL counts as seen
        self.assertEqual(result["scanner"]["signals_given"], 1)
        self.assertIn("search down", result["web_only"]["error"])

    def test_fit_food_cost_joins_the_scan_total_once(self):
        result = {"usage": {"cost_usd": 1.0}, "fit_food": {"brief_chars": 10,
                  "scanner": {"usage": {"cost_usd": 0.5}}, "web_only": {"usage": {"cost_usd": 0.25}}}}
        add_fit_food_cost(result)
        self.assertEqual(result["usage"]["cost_usd"], 1.75)
        result["fit_food"]["web_only"]["usage"]["cost_usd"] = 0.5   # a re-run replaces, never stacks
        add_fit_food_cost(result)
        self.assertEqual(result["usage"]["cost_usd"], 2.0)
        self.assertEqual(result["usage"]["fit_food_cost_usd"], 1.0)


SCANNER_PICK = pick(headline="Whey prices jump as dairy costs bite").model_dump()
SAME_STORY = pick(headline="Whey protein prices jump again", original_source_url="https://abc.net.au/whey").model_dump()
WEB_EXTRA = pick(headline="Cottage cheese protein boom", original_source_url="https://x.example/c", fit_score=6).model_dump()


class CompareAndDigestTests(unittest.TestCase):
    def scan(self, web_picks):
        return {"_scan_id": 30, "candidates": [], "fit_food": {
            "scanner": {"picks": [SCANNER_PICK], "summary": "s", "usage": {"cost_usd": 0.6}},
            "web_only": {"picks": web_picks, "summary": "w", "usage": {"cost_usd": 0.4}}}}

    def test_same_source_counts_as_the_same_story(self):
        result = ff.compare(self.scan([SAME_STORY, WEB_EXTRA])["fit_food"])
        self.assertEqual(len(result["both"]), 1)
        self.assertEqual(result["scanner_only"], [])
        self.assertEqual([p["headline"] for p in result["web_only"]], ["Cottage cheese protein boom"])

    def test_phone_gets_scanner_picks_plus_web_only_extras(self):
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "mfo-test"}):
            msgs = digest.fit_food_messages(self.scan([SAME_STORY, WEB_EXTRA]), {201: {"verdict": "write_now",
                                            "demand": {"best_keyword": "whey price", "au_volume": 1300, "best_kd": 4}}})
        self.assertEqual([m["title"] for m in msgs], ["🥗 Fit food 1/1: Whey prices jump as dairy costs bite",
                                                      "🥗 Fit food (web only): Cottage cheese protein boom"])
        first = msgs[0]
        self.assertIn("8/10 fit · news post", first["message"])
        self.assertIn("🟢 AU 1.3k/mo KD4 ('whey price')", first["message"])
        self.assertIn("source: abc.net.au (opened)", first["message"])
        self.assertEqual(first["click"], "https://abc.net.au/whey")
        self.assertEqual(first["priority"], 3)
        self.assertIn("vote-up-30-201", first["actions"][1]["headers"]["Tags"])
        self.assertIn("vote-up-30-302", msgs[1]["actions"][1]["headers"]["Tags"])  # web pick #2 -> rank 302

    def test_email_carries_the_comparison(self):
        text = digest.fit_food_comparison_text(self.scan([WEB_EXTRA]))
        self.assertIn("Only the scanner-led search (1):\n  - 8/10 Whey prices jump", text)
        self.assertIn("Only the web-only search (1):\n  - 6/10 Cottage cheese protein boom", text)
        self.assertIn("Scanner-led (US$0.6): s", text)
        subject, body = digest.build_email([{"title": "t", "message": "m"}], text)
        self.assertTrue(body.rstrip().endswith("Web-only (US$0.4): w"))


class RefTranslationTests(unittest.TestCase):
    def test_unpadded_refs_resolve_but_unknown_ones_do_not(self):
        from trend_radar.claude_scan import _translate

        back = {"S075": "uuid-75", "X003": "uuid-x3", "S100": "uuid-100"}
        self.assertEqual(_translate("S75", back), "uuid-75")
        self.assertEqual(_translate(" S075 ", back), "uuid-75")
        self.assertEqual(_translate("X3", back), "uuid-x3")
        self.assertEqual(_translate("S0100", back), "uuid-100")
        self.assertEqual(_translate("S76", back), "S76")        # not given: stays unknown, still dropped


if __name__ == "__main__":
    unittest.main()


def test_compare_until_ends_the_web_only_run():
    from datetime import date

    from trend_radar.claude_fit_food import compare_enabled

    assert compare_enabled({"compare": True, "compare_until": "2026-11-16"}, today=date(2026, 11, 16))
    assert not compare_enabled({"compare": True, "compare_until": "2026-11-16"}, today=date(2026, 11, 17))
    assert not compare_enabled({"compare": False}, today=date(2026, 11, 1))
    assert compare_enabled({"compare": True}, today=date(2030, 1, 1))


def test_fit_food_runs_once_a_day():
    import json
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from trend_radar.claude_scan import fit_food_ran_today
    from trend_radar.scan_store import ensure_tables

    conn = sqlite3.connect(":memory:")
    ensure_tables(conn)
    now = datetime.now(timezone.utc)
    assert not fit_food_ran_today(conn, now)

    def add(when, fit_food):
        conn.execute("INSERT INTO claude_scans (generated_at, model, cost_usd, candidate_count, repair_count, result_json)"
                     " VALUES (?, 'm', 0, 0, 0, ?)",
                     (when.isoformat(timespec="seconds").replace("+00:00", "Z"), json.dumps({"fit_food": fit_food})))

    add(now - timedelta(minutes=5), {"scanner": {"error": "boom"}})
    assert not fit_food_ran_today(conn, now)  # a failed run doesn't count
    add(now - timedelta(minutes=1), {"scanner": {"picks": []}})
    assert fit_food_ran_today(conn, now)


def test_latest_fit_food_scan_skips_scans_without_it():
    import json
    import sqlite3

    from trend_radar.scan_store import ensure_tables, latest_fit_food_scan

    conn = sqlite3.connect(":memory:")
    ensure_tables(conn)
    for result in ({"fit_food": {"scanner": {"picks": [{"headline": "A"}]}}}, {"candidates": []}):
        conn.execute("INSERT INTO claude_scans (generated_at, model, cost_usd, candidate_count, repair_count, result_json)"
                     " VALUES ('2026-11-12T00:00:00Z', 'm', 0, 0, 0, ?)", (json.dumps(result),))
    scan = latest_fit_food_scan(conn)
    assert scan["_scan_id"] == 1 and scan["fit_food"]["scanner"]["picks"][0]["headline"] == "A"
