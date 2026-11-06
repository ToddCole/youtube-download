"""Phone digest formatting (no network)."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest  # noqa: E402

SCAN = {"_scan_id": 7, "candidates": [
    {"rank": 1, "headline": "Almgren wins NYC"},
    {"rank": 2, "headline": "Spanish squats"},
    {"rank": 3, "headline": "Peptides"},
    {"rank": 4, "headline": "F1 training"},
]}


def score(verdict, volume, kw="k", kd=5):
    return {"verdict": verdict, "label": verdict, "demand": {"best_keyword": kw, "total_volume": volume, "best_kd": kd}}


class DigestTests(unittest.TestCase):
    def test_write_now_picks_lead_and_strong_one_is_high_priority(self):
        scores = {1: score("watch", 0), 2: score("write_now", 14500, "spanish squat", 0),
                  3: score("skip", 5000, kd=40), 4: score("write_now", 300)}
        d = digest.build_digest(SCAN, scores, None, now=datetime(2026, 11, 7, 7, 5))
        self.assertEqual(d["title"], "MFO Morning picks: 2 Write now")
        first = d["body"].splitlines()[0]
        self.assertIn("Spanish squats", first)
        self.assertIn("Spanish squats · 14.5k/mo KD0", d["body"])
        self.assertIn("+1 more on the Desk", d["body"])
        self.assertEqual(d["priority"], "high")

    def test_weak_write_now_stays_default_and_emerging_is_listed(self):
        emerging = {"rows": [{"query": "100 physique france", "impressions_7d": 1157,
                              "page": "https://mensfitnessonline.com.au/100-physique-france/"}]}
        d = digest.build_digest(SCAN, {1: score("write_now", 70)}, emerging, now=datetime(2026, 11, 7, 16, 5))
        self.assertTrue(d["title"].startswith("MFO Afternoon picks"))
        self.assertEqual(d["priority"], "default")
        self.assertIn("📈 '100 physique france' 1.2k impr, new", d["body"])

    def test_unscored_scan_still_digests(self):
        d = digest.build_digest(SCAN, {}, None)
        self.assertIn("not SEO-scored", d["body"])
        self.assertNotIn("Write now", d["title"])


if __name__ == "__main__":
    unittest.main()
