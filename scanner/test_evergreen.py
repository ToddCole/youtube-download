"""Evergreen ideas: selection, cooldown and digest formatting (no network)."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest  # noqa: E402
import evergreen  # noqa: E402
import learning  # noqa: E402

SITE = "https://mensfitnessonline.com.au"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def row(page, query, pos, impressions, clicks=1):
    return {"keys": [f"{SITE}{page}", query], "position": pos, "impressions": impressions, "clicks": clicks}


GSC = [
    row("/physical-100-italy-cast/", "physical 100 italy", 10.8, 2000),
    row("/physical-100-italy-cast/", "physical 100 italy cast", 6.1, 900),
    row("/physical-100-italy-challenges/", "physical 100 italy", 11.3, 800),
    row("/anatoly-powerlifter/", "anatoly weight", 7.5, 1500),
    row("/already-top/", "top query", 1.4, 5000),         # already on top: not a refresh
    row("/too-small/", "tiny", 9.0, 50),                   # below the impressions floor
    row("/just-edited/", "fresh edit", 8.0, 3000),         # edited recently: leave it
]
PAGES = [
    {"url": f"{SITE}/physical-100-italy-cast/", "title": "Physical 100 Italy cast", "modified": "2026-09-01T00:00:00"},
    {"url": f"{SITE}/anatoly-powerlifter/", "title": "Anatoly", "modified": "2026-08-01T00:00:00"},
    {"url": f"{SITE}/just-edited/", "title": "Just edited", "modified": "2026-11-10T00:00:00"},
]
ANGLES = [{"topic": "What California's creatine law covers", "angle": "Not a ban: a labelling rule",
           "why_it_lasts": "People keep asking if creatine is banned", "update_url": None}]


def scan(n_picks=1, scan_id=20, **extra):
    s = {"_scan_id": scan_id, "candidates": [{"rank": i, "headline": f"pick {i}"} for i in range(1, n_picks + 1)],
         "evergreen_angles": ANGLES}
    s.update(extra)
    return s


class SelectionTests(unittest.TestCase):
    def test_refreshes_rank_by_striking_impressions_and_skip_the_rest(self):
        ideas = evergreen.find_refreshes(GSC, PAGES, set(), NOW)
        self.assertEqual([i["title"] for i in ideas], ["Physical 100 Italy cast", "Anatoly", "/physical-100-italy-challenges/"])
        top = ideas[0]
        self.assertEqual(top["impressions"], 2900)
        self.assertEqual(top["queries"][0], {"query": "physical 100 italy", "position": 10.8, "impressions": 2000})
        self.assertEqual(top["rivals"], ["/physical-100-italy-challenges/"])

    def test_fills_up_to_four_alternating_refresh_and_angle(self):
        ideas = evergreen.select(scan(n_picks=1), GSC, PAGES, set())
        self.assertEqual([i["kind"] for i in ideas], ["refresh", "angle", "refresh"])
        self.assertEqual([i["rank"] for i in ideas], [101, 102, 103])

    def test_busy_news_day_gets_no_evergreen(self):
        self.assertEqual(evergreen.select(scan(n_picks=4), GSC, PAGES, set()), [])

    def test_pages_already_picked_for_an_update_are_skipped(self):
        s = scan(n_picks=2)
        s["candidates"][0]["update_url"] = f"{SITE}/physical-100-italy-cast/"
        titles = [i["title"] for i in evergreen.select(s, GSC, PAGES, set())]
        self.assertNotIn("Physical 100 Italy cast", titles)


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ideas_are_stored_once_and_not_repeated_next_scan(self):
        first = evergreen.compute(scan(scan_id=20), GSC, PAGES, self.db)
        self.assertEqual(evergreen.compute(scan(scan_id=20), [], PAGES, self.db), first)  # stored, not recomputed
        second = evergreen.compute(scan(scan_id=21), GSC, PAGES, self.db)
        self.assertFalse({i["key"] for i in first} & {i["key"] for i in second})

    def test_downvoted_idea_stays_away_after_the_normal_cooldown(self):
        first = evergreen.compute(scan(scan_id=20), GSC, PAGES, self.db)
        conn = sqlite3.connect(self.db)
        learning.record_vote(conn, "down", 20, first[0]["rank"], "test")
        conn.execute("UPDATE evergreen_ideas SET created_at='2026-10-01T00:00:00Z'")  # past the 14 days
        conn.commit()
        keys = evergreen.recent_keys(conn, NOW, 99)
        conn.close()
        self.assertEqual(keys, {first[0]["key"]})


class DigestTests(unittest.TestCase):
    def test_refresh_and_angle_messages(self):
        ideas = evergreen.select(scan(n_picks=1), GSC, PAGES, set())
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "mfo-test"}):
            msgs = digest.build_messages(scan(n_picks=1, why_moving_now="x"), {}, None,
                                         now=datetime(2026, 11, 12, 7, 5), evergreen=ideas)
        self.assertEqual(len(msgs), 4)
        refresh, angle = msgs[1], msgs[2]
        self.assertEqual(refresh["title"], "🌲 Evergreen: refresh Physical 100 Italy cast")
        self.assertIn("#10.8 for 'physical 100 italy' (2k impressions", refresh["message"])
        self.assertIn("Split ranking: /physical-100-italy-challenges/", refresh["message"])
        self.assertEqual(refresh["click"], f"{SITE}/physical-100-italy-cast/")
        self.assertEqual(refresh["priority"], 2)
        self.assertIn("vote-up-20-101", refresh["actions"][0]["headers"]["Tags"])
        self.assertEqual(angle["title"], "🌲 Evergreen: What California's creatine law covers")
        self.assertIn("Angle: Not a ban", angle["message"])
        self.assertNotIn("click", angle)


if __name__ == "__main__":
    unittest.main()
