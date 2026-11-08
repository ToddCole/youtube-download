"""Learning loop: votes, published posts and the feedback block (no network)."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import learning  # noqa: E402
from trend_radar.scan_store import save_scan  # noqa: E402

NOW = datetime(2026, 11, 7, 7, 0, tzinfo=timezone.utc)


def scan(generated_at, headlines):
    return {"generated_at": generated_at, "model": "m", "usage": {}, "candidates": [
        {"rank": i, "headline": h} for i, h in enumerate(headlines, 1)]}


class VoteTests(unittest.TestCase):
    def test_parse_vote(self):
        self.assertEqual(learning.parse_vote("up 7 2"), ("up", 7, 2))
        self.assertIsNone(learning.parse_vote("hello"))
        self.assertIsNone(learning.parse_vote("up x 2"))
        self.assertEqual(learning.vote_body("down", 7, 2), "down 7 2")
        self.assertEqual(learning.parse_vote_tags(["white_check_mark", learning.vote_tag("up", 7, 2)]), ("up", 7, 2))
        self.assertIsNone(learning.parse_vote_tags(["newspaper"]))

    def test_latest_vote_wins_and_duplicates_ignored(self):
        conn = sqlite3.connect(":memory:")
        self.assertTrue(learning.record_vote(conn, "up", 7, 2, "phone", msg_id="a"))
        self.assertFalse(learning.record_vote(conn, "up", 7, 2, "phone", msg_id="a"))
        learning.record_vote(conn, "down", 7, 2, "desk", msg_id="b")
        self.assertEqual(learning.latest_votes(conn), {(7, 2): "down"})


class FeedbackTests(unittest.TestCase):
    def test_block_has_published_posts_traffic_picks_and_votes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "t.db")
            old = save_scan(conn, scan("2026-10-01T05:00:00Z", ["Ancient story"]))
            sid = save_scan(conn, scan("2026-11-06T05:00:00Z", ["Spanish squats", "F1 champion trains"]))
            learning.record_vote(conn, "down", sid, 2, "phone", msg_id="m1")
            index = Path(tmp) / "idx.json"
            index.write_text(json.dumps({"pages": [
                {"title": "Spanish squat: how to do it", "url": "https://m/spanish-squat/", "date": "2026-11-06T10:00:00"},
                {"title": "Old post", "url": "https://m/old/", "date": "2026-09-01T10:00:00"},
            ]}))
            traffic = {"https://m/spanish-squat": {"clicks": 9, "impressions": 400}}
            block = learning.build_feedback(conn, open_stories_path=Path('/nonexistent/open_stories.json'), now=NOW, traffic=traffic, index_path=index)
            self.assertIn("Spanish squat: how to do it — 9 clicks, 400 impressions", block)
            self.assertNotIn("Old post", block)
            self.assertIn("F1 champion trains [👎 editor]", block)
            self.assertNotIn("Ancient story", block)
            self.assertIn("Never re-pick", block)
            self.assertIsNotNone(old)

    def test_nothing_to_learn_from_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "t.db")
            self.assertIsNone(learning.build_feedback(conn, open_stories_path=Path('/nonexistent/open_stories.json'), now=NOW, traffic={}, index_path=Path(tmp) / "none.json"))


if __name__ == "__main__":
    unittest.main()
