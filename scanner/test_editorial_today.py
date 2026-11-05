"""Today board + Agent Review via API (no network, no API calls)."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCANNER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCANNER_DIR))

import editorial_today as et  # noqa: E402
from trend_radar.scan_store import save_scan  # noqa: E402

SCAN = {
    "generated_at": "2026-11-06T05:00:00Z",
    "model": "claude-sonnet-5-5",
    "usage": {"cost_usd": 0.39},
    "corpus_summary": "c",
    "watchlist": ["w"],
    "_evidence_packet_size": 10,
    "candidates": [
        {
            "rank": 1, "headline": "HYROX launches a training app", "topic_summary": "s", "why_moving_now": "w",
            "mfo_angle": "a", "confidence": "high", "evidence_strength": "strong", "lanes": ["publisher"],
            "independent_sources": 3, "verification_needed": ["v"], "editorial_cautions": [],
            "evidence": [{"signal_id": "x", "title": "T", "source": "BoxRox", "lane": "publisher",
                          "published_at": None, "url": "https://boxrox.com/a", "relevance": "r"}],
        }
    ],
}


class TrendLeadTests(unittest.TestCase):
    def test_scan_becomes_commissionable_leads_and_is_found_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "trend.db"
            conn = sqlite3.connect(db)
            scan_id = save_scan(conn, SCAN)
            conn.close()
            with mock.patch.object(et, "TREND_DB", db), mock.patch.object(et, "MFO_INDEX", Path(tmp) / "none.json"):
                meta, leads = et.trend_leads()
                self.assertEqual(meta["scan_id"], scan_id)
                self.assertEqual(leads[0]["lead_id"], f"trend:{scan_id}:1")
                self.assertEqual(leads[0]["source_url"], "https://boxrox.com/a")
                found = et.find_trend_lead(f"trend:{scan_id}:1")
                self.assertEqual(found["title"], "HYROX launches a training app")
                self.assertIsNone(et.find_trend_lead(f"trend:{scan_id}:9"))
                self.assertIsNone(et.find_trend_lead("creator:abc"))

    def test_related_pages_need_most_of_the_page_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "idx.json"
            index.write_text(json.dumps({"pages": [
                {"title": "HYROX Training: How to Actually Build a Program", "url": "https://m/hyrox"},
                {"title": "How to Know if a Woman is Attracted to You", "url": "https://m/x"},
            ]}))
            with mock.patch.object(et, "MFO_INDEX", index):
                pages = et.related_mfo_pages("HYROX launches a training program app")
            self.assertEqual([p["url"] for p in pages], ["https://m/hyrox"])


class SlimPacketTests(unittest.TestCase):
    def test_slim_packet_drops_duplicates_and_heavy_fields(self):
        packet = {
            "review_candidates": {"news": [{"lead_id": "n1", "title": "t", "score_json": {"big": 1}, "abstract": "a" * 5000}]},
            "excluded_candidates": {"news": [{"lead_id": f"e{i}"} for i in range(50)]},
            "scanner_results": {"news": {}},
            "editorial_supervisor_prompt": "p",
            "required_response_schema": {},
        }
        slim = et.slim_packet_for_api(packet)
        self.assertNotIn("excluded_candidates", slim)
        self.assertNotIn("scanner_results", slim)
        self.assertEqual(slim["excluded_candidate_counts"], {"news": 50})
        lead = slim["review_candidates"]["news"][0]
        self.assertNotIn("score_json", lead)
        self.assertLess(len(lead["abstract"]), 1300)
        self.assertEqual(et.packet_lead_ids(packet), ["n1"])


if __name__ == "__main__":
    unittest.main()
