"""Trending now (Google Trends / Wikipedia / Netflix) and the free daily
digest. No network: every source is fed from fixtures."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest  # noqa: E402
import evergreen  # noqa: E402
import run_cycle  # noqa: E402
import trending  # noqa: E402

PAGES = [
    {"url": "https://mensfitnessonline.com.au/abdul-el-sayed-workout/", "slug": "abdul-el-sayed-workout",
     "title": "Abdul El-Sayed's workout: the senator who benches 315 lb"},
    {"url": "https://mensfitnessonline.com.au/battle-ropes/", "slug": "battle-ropes",
     "title": "Battle ropes: a full-body workout"},
    {"url": "https://mensfitnessonline.com.au/physical-100-usa/", "slug": "physical-100-usa",
     "title": "Physical 100: USA: release date and cast"},
]

TRENDS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>magic vs bucks</title><ht:approx_traffic>200+</ht:approx_traffic>
  <ht:news_item><ht:news_item_title>Bucks vs Magic preview</ht:news_item_title>
  <ht:news_item_url>https://nba.example/1</ht:news_item_url><ht:news_item_source>Brew Hoop</ht:news_item_source></ht:news_item></item>
<item><title>abdul el sayed</title><ht:approx_traffic>2000+</ht:approx_traffic>
  <ht:news_item><ht:news_item_title>Senator's bench press goes viral</ht:news_item_title>
  <ht:news_item_url>https://news.example/abdul</ht:news_item_url><ht:news_item_source>News</ht:news_item_source></ht:news_item></item>
<item><title>sam smith</title><ht:approx_traffic>500+</ht:approx_traffic>
  <ht:news_item><ht:news_item_title>Sam Smith's 12-week workout transformation</ht:news_item_title>
  <ht:news_item_url>https://news.example/sam</ht:news_item_url><ht:news_item_source>News</ht:news_item_source></ht:news_item></item>
</channel></rss>"""


class MatchingTest(unittest.TestCase):
    def test_multi_word_term_matches_its_page(self):
        self.assertEqual(trending.mfo_page_for("abdul el sayed", PAGES)["url"], PAGES[0]["url"])
        self.assertEqual(trending.mfo_page_for("Physical 100", PAGES)["url"], PAGES[2]["url"])

    def test_single_word_never_matches_a_page(self):
        self.assertIsNone(trending.mfo_page_for("The Body", PAGES))
        self.assertIsNone(trending.mfo_page_for("workout", PAGES))

    def test_topics_from_context(self):
        self.assertIn("Combat sport", trending.topics_for("Usman Nurmagomedov", "Russian mixed martial arts fighter"))
        self.assertEqual(trending.topics_for("magic vs bucks", "Bucks vs Magic preview"), [])

    def test_only_australian_sportspeople_count_as_athletes(self):
        self.assertEqual(trending.topics_for("Michael Carrick", "English footballer and manager"), [])
        self.assertIn("Athletes", trending.topics_for("Pat Cummins", "Australian cricketer (born 1993)"))
        self.assertIn("Athletes", trending.topics_for("Someone", "Kenyan Olympian"))


class SourcesTest(unittest.TestCase):
    def test_google_trends_keeps_only_mfo_matches(self):
        with mock.patch.object(trending, "http_get", return_value=TRENDS_XML):
            items = trending.google_trends(PAGES)
        terms = {i["term"]: i for i in items}
        self.assertNotIn("magic vs bucks", terms)
        self.assertEqual(terms["abdul el sayed"]["mfo_page"]["url"], PAGES[0]["url"])
        self.assertEqual(terms["abdul el sayed"]["traffic"], "2000+")
        self.assertIn("Training and gyms", terms["sam smith"]["topics"])  # from the news headline

    def test_wiki_spikes(self):
        today = {"Main_Page": 90000, "Usman_Nurmagomedov": 4000, "Steady_Person": 5000, "Low": 100,
                 "2026_NRL_season": 9000}
        baseline = [{"Usman_Nurmagomedov": 400, "Steady_Person": 4800}] * 7
        spikes = {a: r for a, _, r in trending.wiki_spikes(today, baseline)}
        self.assertEqual(set(spikes), {"Usman_Nurmagomedov"})
        self.assertAlmostEqual(spikes["Usman_Nurmagomedov"], 10.0)

    def test_netflix_new_au_tv_latest_week(self):
        tsv = "\t".join(["country_name", "country_iso2", "week", "category", "weekly_rank", "show_title",
                         "season_title", "cumulative_weeks_in_top_10"]) + "\n"
        rows = [
            ("Australia", "AU", "2026-11-08", "TV (Non-English)", "1", "Physical: Asia", "Physical: Asia: Season 2", "1"),
            ("Australia", "AU", "2026-11-08", "TV (English)", "2", "Old Show", "Old Show: Season 1", "9"),
            ("Australia", "AU", "2026-11-01", "TV (English)", "1", "Last Week", "N/A", "1"),
            ("Australia", "AU", "2026-11-08", "Films (English)", "1", "A Film", "N/A", "1"),
            ("Argentina", "AR", "2026-11-08", "TV (English)", "1", "Elsewhere", "N/A", "1"),
        ]
        tsv += "\n".join("\t".join(r) for r in rows) + "\n"
        shows = trending.netflix_au_new(tsv)
        self.assertEqual([s["show"] for s in shows], ["Physical: Asia"])
        self.assertIn("Fitness TV", trending.classify(shows[0]["show"], [shows[0]["season"]], PAGES)["topics"])

    def test_one_dead_source_does_not_sink_the_others(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trending, "load_pages", return_value=PAGES), \
                mock.patch.object(trending, "google_trends", return_value=[{"source": "google", "term": "x", "mfo_page": None}]), \
                mock.patch.object(trending, "wikipedia", side_effect=TimeoutError("slow")), \
                mock.patch.object(trending, "netflix", return_value=[]):
            result = trending.compute(db_path=Path(tmp) / "t.db")
        self.assertEqual(len(result["google"]), 1)
        self.assertEqual(result["wikipedia"], [])
        self.assertEqual(len(result["errors"]), 1)


class StorageTest(unittest.TestCase):
    def test_load_recent_pools_the_last_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = sqlite3.connect(db)
            trending.ensure_table(conn)
            now = datetime.now(timezone.utc)
            for hours, terms in ((30, ["too old"]), (6, ["early", "both"]), (1, ["late", "both"])):
                at = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
                result = {"computed_at": at, "errors": [], "wikipedia": [], "netflix": [],
                          "google": [{"source": "google", "term": t, "mfo_page": None} for t in terms]}
                conn.execute("INSERT INTO trending (computed_at, result_json) VALUES (?, ?)", (at, json.dumps(result)))
            conn.commit()
            conn.close()
            merged = trending.load_recent(db_path=db)
        self.assertEqual(sorted(i["term"] for i in merged["google"]), ["both", "early", "late"])


class FreeDayTest(unittest.TestCase):
    def test_free_scan_id_is_distinct_and_vote_safe(self):
        scan = evergreen.free_scan(date(2026, 11, 14))
        self.assertEqual(scan["_scan_id"], 920261114)
        from learning import parse_vote_tags, vote_tag

        self.assertEqual(parse_vote_tags([vote_tag("up", scan["_scan_id"], 101)]), ("up", 920261114, 101))

    def test_free_digest_has_no_news_header_and_leads_with_trending(self):
        trend = {"google": [{"source": "google", "term": "abdul el sayed", "traffic": "2000+",
                             "detail": "Bench goes viral", "url": "https://news.example/abdul",
                             "mfo_page": {"url": PAGES[0]["url"], "title": "x"}, "topics": []}],
                 "wikipedia": [], "netflix": []}
        idea = {"kind": "refresh", "rank": 101, "key": "k", "url": PAGES[2]["url"], "title": "Physical 100 USA",
                "queries": [{"query": "physical 100 usa", "position": 5.6, "impressions": 2808}], "rivals": []}
        with mock.patch.dict(os.environ, {}, clear=True):
            msgs = digest.build_messages(evergreen.free_scan(), {}, None, evergreen=[idea], trending=trend)
        self.assertEqual(msgs[0]["title"], "🔥 Trending: abdul el sayed")
        self.assertIn("/abdul-el-sayed-workout/", msgs[0]["message"])
        self.assertTrue(msgs[1]["title"].startswith("🌲 Evergreen: refresh"))
        self.assertFalse(any("nothing new" in m["title"] for m in msgs))

    def test_free_digest_with_nothing_sends_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(digest.build_messages(evergreen.free_scan(), {}, None), [])

    def test_trending_already_sent_is_skipped(self):
        trend = {"google": [{"source": "google", "term": "abdul el sayed", "url": "u",
                             "mfo_page": {"url": PAGES[0]["url"]}, "topics": []}]}
        sent = [digest.sent_log.item("trending", "abdul el sayed", PAGES[0]["url"])]
        self.assertEqual(digest.trending_messages(trend, sent), [])

    def test_free_slot(self):
        at7 = datetime(2026, 11, 14, 7, 5)
        with mock.patch.object(run_cycle, "datetime") as dt:
            dt.now.return_value = at7
            with mock.patch.dict(os.environ, {"MFO_CLAUDE_SCAN": "0"}):
                self.assertTrue(run_cycle.free_slot(None))
            with mock.patch.dict(os.environ, {"MFO_CLAUDE_SCAN": "1"}):
                self.assertFalse(run_cycle.free_slot(None))
                self.assertTrue(run_cycle.free_slot(False))  # --no-claude at 07:00
                self.assertFalse(run_cycle.free_slot(True))
            dt.now.return_value = datetime(2026, 11, 14, 10, 5)
            with mock.patch.dict(os.environ, {"MFO_CLAUDE_SCAN": "0"}):
                self.assertFalse(run_cycle.free_slot(None))


if __name__ == "__main__":
    unittest.main()
