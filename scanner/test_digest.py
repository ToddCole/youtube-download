"""Phone digest formatting (no network)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest  # noqa: E402


def cand(rank, headline, **extra):
    c = {"rank": rank, "headline": headline, "why_moving_now": f"why {rank}", "mfo_angle": f"angle {rank}",
         "evidence": [{"url": f"https://news.example/{rank}"}]}
    c.update(extra)
    return c


SCAN = {"_scan_id": 7, "corpus_summary": "summary", "candidates": [
    cand(1, "Almgren wins NYC"),
    cand(2, "Spanish squats"),
    cand(3, "Peptides", story_type="update", update_url="https://mensfitnessonline.com.au/peptides-australia-legal/",
         verification_needed=["Confirm the figures against the primary source"]),
]}


def score(verdict, volume, kw="k", kd=5, us=0):
    return {"verdict": verdict, "label": verdict,
            "demand": {"best_keyword": kw, "au_volume": volume, "us_volume": us, "best_kd": kd}}


ENV = {"NTFY_TOPIC": "mfo-test"}


class DigestTests(unittest.TestCase):
    def build(self, scores, emerging=None, scan=SCAN, hour=7):
        with mock.patch.dict(os.environ, ENV):
            return digest.build_messages(scan, scores, emerging, now=datetime(2026, 11, 9, hour, 5))

    def test_one_notification_per_pick_best_seo_first(self):
        scores = {1: score("watch", 0), 2: score("write_now", 14500, "spanish squat", None), 3: score("skip", 5000, kd=40)}
        msgs = self.build(scores)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["title"], "Morning 1/3: Spanish squats")
        self.assertIn("🟢 Write now · AU 14.5k/mo", msgs[0]["message"])
        self.assertIn("Now: why 2", msgs[0]["message"])
        self.assertIn("Angle: angle 2", msgs[0]["message"])
        self.assertEqual(msgs[0]["priority"], 4)  # strong Write now buzzes
        self.assertEqual(msgs[1]["priority"], 2)  # the rest arrive quietly

    def test_tap_opens_source_and_buttons_are_source_up_down(self):
        msgs = self.build({})
        first = msgs[0]
        self.assertEqual(first["click"], "https://news.example/1")
        self.assertEqual([a["label"] for a in first["actions"]], ["Source", "👍", "👎"])
        up, down = first["actions"][1], first["actions"][2]
        self.assertIn("vote-up-7-1", up["headers"]["Tags"])
        self.assertIn("vote-down-7-1", down["headers"]["Tags"])
        self.assertEqual(up["url"], "https://ntfy.sh/mfo-test")

    def test_update_pick_opens_the_mfo_page(self):
        msgs = self.build({})
        update = next(m for m in msgs if "Peptides" in m["title"])
        self.assertEqual(update["click"], "https://mensfitnessonline.com.au/peptides-australia-legal/")
        self.assertIn("🔵 Update MFO page: /peptides-australia-legal/", update["message"])
        self.assertIn("Check: Confirm the figures", update["message"])
        self.assertEqual(update["actions"][0]["url"], "https://news.example/3")

    def test_us_only_pick_says_so(self):
        msgs = self.build({1: score("us_only", 0, "metabolic walking", us=5400)})
        us = next(m for m in msgs if "Almgren" in m["title"])
        self.assertIn("🟡 US demand only · US only 5.4k/mo", us["message"])

    def test_empty_scan_sends_one_quiet_nothing_today(self):
        msgs = self.build({}, scan={"_scan_id": 8, "corpus_summary": "Quiet day.", "candidates": []}, hour=16)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["title"], "MFO Afternoon: nothing strong today")
        self.assertEqual(msgs[0]["message"], "Quiet day.")

    def test_emerging_demand_is_its_own_quiet_notification(self):
        emerging = {"rows": [{"query": "100 physique france", "impressions_7d": 1157,
                              "page": "https://mensfitnessonline.com.au/100-physique-france/"}]}
        last = self.build({}, emerging)[-1]
        self.assertEqual(last["title"], "📈 New in Google: '100 physique france'")
        self.assertIn("1.2k impressions", last["message"])
        self.assertEqual(last["click"], "https://mensfitnessonline.com.au/100-physique-france/")

    def test_no_topic_means_no_vote_buttons(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            msgs = digest.build_messages(SCAN, {}, None)
        self.assertEqual([a["label"] for a in msgs[0]["actions"]], ["Source"])


if __name__ == "__main__":
    unittest.main()
