"""Open MFO stories: promise detection, search queries, Claude block. No network."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import open_stories  # noqa: E402


def post(title, body, date="2026-10-29T10:00:00", link="https://mensfitnessonline.com.au/x/"):
    return {"title": {"rendered": title}, "content": {"rendered": body}, "date": date, "link": link}


class OpenStoriesTests(unittest.TestCase):
    def test_update_promise_is_found(self):
        body = "<p>Both men lift on Sunday.</p><p>We&#8217;ll update this page with both results after the lifting.</p>"
        stories = open_stories.find_open_stories([post("Steven Miles takes up Tom Tate’s Masters Games challenge", body)])
        self.assertEqual(len(stories), 1)
        self.assertIn("update this page", stories[0]["promise"])
        self.assertEqual(stories[0]["query"], "Steven Miles takes up Tom Tate s Masters Games challenge")

    def test_race_report_without_promise_is_not_open(self):
        body = "<p>Seven weeks after the race that made her famous, she won her comeback race.</p>"
        self.assertEqual(open_stories.find_open_stories([post("Comeback win", body)]), [])

    def test_long_related_links_run_is_not_a_promise(self):
        body = "<p>" + " ".join(["Some related post title"] * 12) + " world record pending confirmation and more links.</p>"
        self.assertIsNone(open_stories.find_promise(open_stories._text(body)))

    def test_query_drops_explainer_tail(self):
        self.assertEqual(open_stories.search_query("Darwin Supercars 2027: dates and the first night race"), "Darwin Supercars 2027")

    def test_block_lists_url_and_promise(self):
        block = open_stories.build_block([{"date": "2026-10-29", "title": "T", "url": "https://m/t/", "promise": "We'll update."}])
        self.assertIn("update_url", block)
        self.assertIn("https://m/t/", block)
        self.assertIsNone(open_stories.build_block([]))


if __name__ == "__main__":
    unittest.main()
