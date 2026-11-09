"""SEO opportunity verdicts, GSC matching, credit cap. No network."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seo_opportunity as seo  # noqa: E402


def demand(volume, kd, keyword="x", us=0):
    """volume = AU searches/mo; kd = AU keyword difficulty."""
    return {"au_volume": volume, "us_volume": us, "best_kd": kd, "best_keyword": keyword}


class VerdictTests(unittest.TestCase):
    def test_write_now_needs_demand_low_kd_and_no_page(self):
        self.assertEqual(seo.decide(demand(14800, 4), [], []).code, "write_now")

    def test_existing_gsc_footing_means_update(self):
        footing = [{"query": "matteo sartori", "page": "https://m/p", "impressions": 988, "position": 5.4}]
        self.assertEqual(seo.decide(demand(14800, 4), footing, []).code, "update_existing")

    def test_related_archive_page_with_demand_means_update(self):
        v = seo.decide(demand(90, 0), [], [{"title": "HYROX Training", "url": "u", "query": "hyrox training"}])
        self.assertEqual(v.code, "update_existing")

    def test_no_demand_is_watch(self):
        self.assertEqual(seo.decide(demand(0, None), [], []).code, "watch")

    def test_high_kd_is_low_upside(self):
        self.assertEqual(seo.decide(demand(5000, 45), [], []).code, "skip")

    def test_us_heavy_demand_is_us_only_not_write_now(self):
        # 6 Nov scan: "metabolic walking" had 5,400 US searches and 0 in AU.
        v = seo.decide(demand(0, None, "metabolic walking", us=5400), [], [])
        self.assertEqual(v.code, "us_only")

    def test_small_au_demand_is_not_write_now_even_with_big_us(self):
        # 6 Nov scan: "james bond workout" was 30 AU / 260 US, AU KD 36.
        v = seo.decide(demand(30, 36, "james bond workout", us=260), [], [])
        self.assertEqual(v.code, "us_only")

    def test_unknown_au_kd_still_write_now_but_flagged(self):
        v = seo.decide(demand(400, None), [], [])
        self.assertEqual(v.code, "write_now")
        self.assertIn("KD unknown", v.reason)

    def test_pick_matched_to_mfo_page_is_update_not_write_now(self):
        # 9 Nov scan: peptides update said "Write now ... no MFO page".
        v = seo.decide(demand(2900, None, "are peptides legal in australia"), [], [],
                       "https://mensfitnessonline.com.au/peptides-australia-legal/")
        self.assertEqual(v.code, "update_existing")
        self.assertIn("/peptides-australia-legal/", v.reason)
        self.assertNotIn("no MFO page", v.reason)

    def test_real_gsc_footing_explains_an_update_pick_first(self):
        footing = [{"query": "karl bushby", "page": "https://m/p", "impressions": 593, "position": 2.9}]
        v = seo.decide(demand(0, None), footing, [], "https://mensfitnessonline.com.au/karl-bushby/")
        self.assertEqual(v.code, "update_existing")
        self.assertIn("593 impressions", v.reason)

    def test_small_demand_is_watch(self):
        self.assertEqual(seo.decide(demand(20, 0), [], []).code, "watch")


class GscMatchTests(unittest.TestCase):
    ROWS = [
        {"keys": ["andreas almgren marathon", "https://m/a"], "impressions": 300, "position": 8.0},
        {"keys": ["marathon", "https://m/b"], "impressions": 900, "position": 30.0},
        {"keys": ["matteo sartori", "https://m/c"], "impressions": 988, "position": 5.4},
    ]

    def test_all_query_words_must_appear(self):
        matches = seo.gsc_matches(["andreas almgren"], self.ROWS)
        self.assertEqual([m["page"] for m in matches], ["https://m/a"])

    def test_single_generic_word_row_does_not_match(self):
        self.assertEqual(seo.gsc_matches(["nyc marathon winner"], self.ROWS), [])


class KeywordPageTests(unittest.TestCase):
    PAGES = [
        {"title": "Lifting Linked to Longevity", "url": "u1"},
        {"title": "How to do a Bulgarian Split Squat", "url": "u2"},
        {"title": "Spanish Squat: Benefits and Form", "url": "u3", "slug": "spanish-squat"},
    ]

    def test_page_needs_every_word_of_the_query(self):
        self.assertEqual([p["url"] for p in seo.keyword_pages(["spanish squat"], self.PAGES)], ["u3"])
        self.assertEqual(seo.keyword_pages(["build muscle after 50"], self.PAGES), [])


class CacheAndLedgerTests(unittest.TestCase):
    def test_demand_uses_best_keyword_across_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "t.db")
            seo.ensure_tables(conn)
            now = seo.utc_now()
            conn.executemany(
                "INSERT INTO seo_keyword_cache VALUES (?,?,?,?)",
                [
                    ("peptides", "AU", now, '{"search_volume": 1000, "keyword_difficulty": 10}'),
                    ("peptides", "US", now, '{"search_volume": 20000, "keyword_difficulty": 30}'),
                    ("hyrox app", "AU", now, None),
                    ("hyrox app", "US", now, '{"search_volume": 90, "keyword_difficulty": 0}'),
                ],
            )
            d = seo.demand_for(conn, ["hyrox app", "peptides"])
            self.assertEqual(d["best_keyword"], "peptides")
            self.assertEqual(d["total_volume"], 21000)
            self.assertEqual(d["au_volume"], 1000)
            self.assertEqual(d["best_kd"], 10)  # AU difficulty, not the lower of AU/US
            self.assertEqual(d["us_kd"], 30)
            hyrox = next(k for k in d["keywords"] if k["keyword"] == "hyrox app")
            self.assertIsNone(hyrox["US_kd"])  # provider's 0 means "no score"
            self.assertIsNone(seo.cached_metrics(conn, "hyrox app", "AU"))
            self.assertIs(seo.cached_metrics(conn, "unknown", "AU"), False)

    def test_best_keyword_is_by_au_volume_and_kd_comes_from_au(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "t.db")
            seo.ensure_tables(conn)
            now = seo.utc_now()
            conn.executemany(
                "INSERT INTO seo_keyword_cache VALUES (?,?,?,?)",
                [
                    ("james bond workout", "AU", now, '{"search_volume": 30, "keyword_difficulty": 36}'),
                    ("james bond workout", "US", now, '{"search_volume": 260, "keyword_difficulty": 0}'),
                    ("bond 26 casting", "AU", now, '{"search_volume": 10, "keyword_difficulty": 8}'),
                ],
            )
            d = seo.demand_for(conn, ["bond 26 casting", "james bond workout"])
            self.assertEqual(d["best_keyword"], "james bond workout")
            self.assertEqual(d["best_kd"], 36)
            self.assertNotEqual(seo.decide(d, [], []).code, "write_now")

    def test_credit_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = seo.CreditLedger(Path(tmp) / "c.json", daily_cap=50)
            ledger.add(40)
            ledger.check(10)
            with self.assertRaises(seo.CreditCapReached):
                ledger.check(11)


if __name__ == "__main__":
    unittest.main()
