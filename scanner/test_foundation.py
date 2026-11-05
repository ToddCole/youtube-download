"""Phase 1 foundation safeguards: cycle lock, MFO index protection,
expansion sources never polled, .env never loaded under tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCANNER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCANNER_DIR))

import scanner  # noqa: E402
import settings  # noqa: E402
from cycle_lock import LockBusy, cycle_lock  # noqa: E402
from trend_radar import db as trdb  # noqa: E402
from trend_radar.claude_expansion import ensure_expansion_sources  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_dotenv_not_loaded_under_pytest(self):
        self.assertFalse(settings.load())


class CycleLockTests(unittest.TestCase):
    def test_second_holder_is_rejected_then_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            with cycle_lock("first", path):
                with self.assertRaises(LockBusy) as ctx:
                    with cycle_lock("second", path):
                        pass
                self.assertIn("first", str(ctx.exception))
            with cycle_lock("third", path):
                pass


class MfoIndexProtectionTests(unittest.TestCase):
    def _write_cache(self, path: Path, n: int) -> None:
        pages = [{"url": f"https://x/{i}", "title": f"t{i}"} for i in range(n)]
        path.write_text(json.dumps({"source": "wordpress-rest", "refreshed_at": None, "pages": pages}))

    def test_partial_wordpress_crawl_raises_instead_of_truncating(self):
        def fake_get2(url, timeout=20):
            if "&page=1&" in url:
                return json.dumps([{"link": "https://x/a", "slug": "a", "title": {"rendered": "A"}}]), {"X-WP-TotalPages": "3"}
            raise OSError("boom")

        with mock.patch.object(scanner, "http_get", side_effect=fake_get2):
            with self.assertRaises(ValueError):
                scanner.fetch_wordpress_posts("https://x")

    def test_shrunken_refresh_keeps_cached_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "idx.json"
            self._write_cache(path, 100)
            few = [scanner.MfoPage(**{**self._page_kwargs(i)}) for i in range(10)] if hasattr(scanner, "MfoPage") else []
            with mock.patch.object(scanner, "fetch_wordpress_posts", return_value=few), \
                 mock.patch.object(scanner, "fetch_sitemap_posts", return_value=few):
                payload = scanner.load_mfo_index(path, "https://x", refresh=True)
            self.assertEqual(len(payload["pages"]), 100)
            self.assertIn("cached", payload["source"])
            self.assertEqual(len(json.loads(path.read_text())["pages"]), 100)

    @staticmethod
    def _page_kwargs(i: int) -> dict:
        import dataclasses

        fields = {f.name: f for f in dataclasses.fields(scanner.MfoPage)}
        values = {}
        for name, field in fields.items():
            if field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING:  # type: ignore[attr-defined]
                continue
            values[name] = f"https://x/{i}" if name == "url" else ("" if field.type in ("str", str) else [])
        return values


class ExpansionSourcesTests(unittest.TestCase):
    def test_expansion_sources_are_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = trdb.connect(Path(tmp) / "t.db")
            ensure_expansion_sources(conn)
            rows = conn.execute(
                "SELECT source_id, active FROM sources WHERE collection_method = 'claude_expansion'"
            ).fetchall()
            self.assertTrue(rows)
            self.assertTrue(all(int(r[1]) == 0 for r in rows))


if __name__ == "__main__":
    unittest.main()
