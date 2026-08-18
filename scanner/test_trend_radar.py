"""Trend Radar Phase 1 tests: raw collection and manual intake only.
Mirrors test_editorial.py's conventions (tempdir + explicit db paths,
:memory:-friendly, no live network calls). No clustering/scoring/entity
extraction is tested here -- that's Phase 2+ scope.

Every test that opens a database closes it deterministically via
self.addCleanup(conn.close) -- run with `-W error::ResourceWarning`-style
visibility (or just watch stderr) to confirm no unclosed-database
warnings remain.
"""

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

from trend_radar import db as trdb
from trend_radar import manual as trmanual
from trend_radar import sources as trsources
from trend_radar.collectors import reddit as tr_reddit
from trend_radar.collectors.base import collect_from_source
from trend_radar.collectors.youtube import extract_published_at, fetch_youtube_channel_source
from trend_radar.db import migrate_youtube_raw_payload_ref_to_json
from trend_radar.models import RawSignalInput
from trend_radar.runner import run_collection
from trend_radar.signals import fingerprint_for, upsert_signal, utc_now_iso

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Fixture Publisher</title>
<item>
  <title>Tom Holland's Favourite CrossFit Workout Is One of 'The Girls'</title>
  <link>https://example.com/tom-holland-cindy</link>
  <pubDate>Mon, 10 Aug 2026 14:42:00 +0000</pubDate>
  <description>The Spider-Man star's incredible 'Cindy' score has gone viral.</description>
  <source url="https://example.com">Fixture Publisher</source>
</item>
<item>
  <title>Second Fixture Article</title>
  <link>https://example.com/second-article</link>
  <pubDate>Tue, 11 Aug 2026 09:00:00 +0000</pubDate>
  <description>An unrelated fixture item.</description>
  <source url="https://example.com">Fixture Publisher</source>
</item>
</channel></rss>"""

SAMPLE_REDDIT_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title>Does the Cindy workout actually work?</title>
  <link href="https://www.reddit.com/r/crossfit/comments/abc123/does_cindy_work/"/>
  <updated>2026-08-12T10:00:00+00:00</updated>
  <content type="html">Trying it this week, any tips?</content>
</entry>
</feed>"""


class TrendRadarTestCase(unittest.TestCase):
    """Base class: connect() and always register the close via
    addCleanup, so every subclass's tests close their connections
    deterministically without repeating the boilerplate everywhere."""

    def connect(self, path: Path) -> sqlite3.Connection:
        conn = trdb.connect(path)
        self.addCleanup(conn.close)
        return conn

    def ensure_run(self, conn: sqlite3.Connection, run_id: str) -> None:
        existing = conn.execute("SELECT run_id FROM collection_runs WHERE run_id = ?", (run_id,)).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO collection_runs (run_id, started_at, lanes, status) VALUES (?,?,?,?)",
            (run_id, utc_now_iso(), "publisher", "running"),
        )
        conn.commit()

    def fixture_source_row(self, conn: sqlite3.Connection, **overrides) -> sqlite3.Row:
        cfg = {
            "source_id": "publisher:fixture",
            "name": "Fixture Publisher",
            "lane": "publisher",
            "platform": "rss",
            "canonical_identifier": "https://example.com/feed",
            "collection_method": "rss_atom",
            "collection_interval_hours": 4,
            "reliability": 5,
            "source_role": "signal",
            "active": True,
            "known_restrictions": None,
            "engagement_available": False,
        }
        cfg.update(overrides)
        trsources.sync_sources(conn, [cfg])
        return conn.execute("SELECT * FROM sources WHERE source_id = ?", (cfg["source_id"],)).fetchone()


class DatabaseTests(TrendRadarTestCase):
    def test_creates_schema_with_wal_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "trend_radar_test.db")
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for expected in ("sources", "collection_runs", "collection_errors", "raw_signals", "signal_observations"):
                self.assertIn(expected, tables)

    def test_reopening_existing_database_does_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trend_radar_test.db"
            self.connect(path)  # closed via addCleanup at test end
            conn2 = self.connect(path)  # CREATE TABLE IF NOT EXISTS must not raise on reopen
            self.assertIsNotNone(conn2)

    def test_connect_returns_context_manager_that_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ctx_test.db"
            with trdb.connect(path) as conn:
                conn.execute("SELECT 1")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")  # closed connections raise on use

    def test_no_side_effects_on_import(self):
        default_path = trdb.DB_PATH
        self.assertIsInstance(default_path, Path)

    def test_ensure_column_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            trdb.ensure_column(conn, "collection_runs", "sources_skipped", "INTEGER NOT NULL DEFAULT 0")
            trdb.ensure_column(conn, "collection_runs", "sources_skipped", "INTEGER NOT NULL DEFAULT 0")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(collection_runs)")}
            self.assertIn("sources_skipped", columns)


class RawPayloadMigrationTests(TrendRadarTestCase):
    """migrate_youtube_raw_payload_ref_to_json(): moves inline JSON that
    an earlier collector version wrote into raw_payload_ref (reserved for
    a genuine external reference) into the correctly-named
    raw_payload_json column, without disturbing a real external
    reference stored in the same column on a different row."""

    def _insert_raw_signal(self, conn, *, signal_id, source_id, platform, raw_payload_ref):
        now = utc_now_iso()
        conn.execute(
            """INSERT INTO raw_signals (
                signal_id, source_id, lane, platform, source_name, source_role,
                first_seen_at, last_seen_at, collected_at, content_fingerprint, raw_payload_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, source_id, "creator", platform, "Fixture Creator", "signal",
             now, now, now, f"manual:creator:{source_id}:{signal_id}", raw_payload_ref),
        )
        conn.commit()

    def test_valid_inline_json_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn, source_id="creator:fixture", lane="creator", platform="youtube")
            payload = json.dumps({"id": "abc123", "title": "Fixture video"})
            self._insert_raw_signal(conn, signal_id="sig-1", source_id="creator:fixture", platform="youtube", raw_payload_ref=payload)

            result = migrate_youtube_raw_payload_ref_to_json(conn)

            self.assertEqual(result["migrated"], 1)
            self.assertEqual(result["skipped_invalid_json"], [])
            row = conn.execute("SELECT raw_payload_ref, raw_payload_json FROM raw_signals WHERE signal_id = ?", ("sig-1",)).fetchone()
            self.assertIsNone(row["raw_payload_ref"])
            self.assertEqual(json.loads(row["raw_payload_json"]), {"id": "abc123", "title": "Fixture video"})

    def test_genuine_non_json_reference_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn, source_id="creator:fixture", lane="creator", platform="youtube")
            self._insert_raw_signal(
                conn, signal_id="sig-2", source_id="creator:fixture", platform="youtube",
                raw_payload_ref="s3://mfo-trend-radar-archive/youtube/abc123.json",
            )

            result = migrate_youtube_raw_payload_ref_to_json(conn)

            self.assertEqual(result["migrated"], 0)
            self.assertEqual(result["skipped_invalid_json"], ["sig-2"])
            row = conn.execute("SELECT raw_payload_ref, raw_payload_json FROM raw_signals WHERE signal_id = ?", ("sig-2",)).fetchone()
            self.assertEqual(row["raw_payload_ref"], "s3://mfo-trend-radar-archive/youtube/abc123.json")
            self.assertIsNone(row["raw_payload_json"])

    def test_non_youtube_platform_is_not_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn, source_id="publisher:fixture", lane="publisher", platform="rss")
            payload = json.dumps({"foo": "bar"})
            self._insert_raw_signal(conn, signal_id="sig-3", source_id="publisher:fixture", platform="rss", raw_payload_ref=payload)

            result = migrate_youtube_raw_payload_ref_to_json(conn)

            self.assertEqual(result["migrated"], 0)
            row = conn.execute("SELECT raw_payload_ref, raw_payload_json FROM raw_signals WHERE signal_id = ?", ("sig-3",)).fetchone()
            self.assertEqual(row["raw_payload_ref"], payload)
            self.assertIsNone(row["raw_payload_json"])

    def test_already_populated_raw_payload_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn, source_id="creator:fixture", lane="creator", platform="youtube")
            self._insert_raw_signal(conn, signal_id="sig-4", source_id="creator:fixture", platform="youtube", raw_payload_ref=json.dumps({"id": "x"}))
            conn.execute("UPDATE raw_signals SET raw_payload_json = ? WHERE signal_id = ?", (json.dumps({"already": "here"}), "sig-4"))
            conn.commit()

            result = migrate_youtube_raw_payload_ref_to_json(conn)

            self.assertEqual(result["migrated"], 0)
            row = conn.execute("SELECT raw_payload_ref, raw_payload_json FROM raw_signals WHERE signal_id = ?", ("sig-4",)).fetchone()
            self.assertEqual(json.loads(row["raw_payload_json"]), {"already": "here"})
            self.assertEqual(row["raw_payload_ref"], json.dumps({"id": "x"}))  # untouched, since json column wasn't empty

    def test_migration_is_safe_to_run_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn, source_id="creator:fixture", lane="creator", platform="youtube")
            self._insert_raw_signal(conn, signal_id="sig-5", source_id="creator:fixture", platform="youtube", raw_payload_ref=json.dumps({"id": "y"}))

            first = migrate_youtube_raw_payload_ref_to_json(conn)
            second = migrate_youtube_raw_payload_ref_to_json(conn)

            self.assertEqual(first["migrated"], 1)
            self.assertEqual(second["migrated"], 0)
            self.assertEqual(second["candidates_considered"], 0)


class SourceLoadingTests(TrendRadarTestCase):
    def test_loads_real_registry_file_and_required_fields_present(self):
        configs = trsources.load_source_configs()
        self.assertGreater(len(configs), 0)
        required = {
            "source_id", "name", "lane", "platform", "canonical_identifier",
            "collection_method", "source_role",
        }
        for cfg in configs:
            missing = required - set(cfg.keys())
            self.assertFalse(missing, f"{cfg.get('source_id')} missing fields: {missing}")
            self.assertIn(cfg["lane"], ("publisher", "audience", "creator", "research", "manual"))

    def test_sync_sources_upserts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            cfg = {
                "source_id": "publisher:upsert_test", "name": "A", "lane": "publisher",
                "platform": "rss", "canonical_identifier": "https://a.example/feed",
                "collection_method": "rss_atom", "source_role": "signal",
            }
            trsources.sync_sources(conn, [cfg])
            trsources.sync_sources(conn, [{**cfg, "name": "A renamed"}])
            rows = conn.execute("SELECT * FROM sources WHERE source_id = ?", (cfg["source_id"],)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "A renamed")

    def test_active_sources_filters_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            trsources.sync_sources(conn, [
                {"source_id": "publisher:on", "name": "On", "lane": "publisher", "platform": "rss",
                 "canonical_identifier": "x", "collection_method": "rss_atom", "source_role": "signal", "active": True},
                {"source_id": "publisher:off", "name": "Off", "lane": "publisher", "platform": "rss",
                 "canonical_identifier": "x", "collection_method": "rss_atom", "source_role": "signal", "active": False},
            ])
            active = trsources.active_sources(conn, "publisher")
            ids = {row["source_id"] for row in active}
            self.assertIn("publisher:on", ids)
            self.assertNotIn("publisher:off", ids)


class RssCollectionTests(TrendRadarTestCase):
    def test_rss_fixture_parses_into_raw_signal_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn)
            with mock.patch("trend_radar.collectors.publishers.scanner.http_get_text", return_value=SAMPLE_RSS):
                from trend_radar.collectors.publishers import fetch_rss_source
                items = fetch_rss_source(source_row)
            self.assertEqual(len(items), 2)
            self.assertTrue(any("Tom Holland" in (i.title or "") for i in items))
            self.assertTrue(any("Cindy" in (i.body_text or "") for i in items))

    def test_one_source_failure_does_not_abort_other_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            good = self.fixture_source_row(conn, source_id="publisher:good", name="Good")
            bad = self.fixture_source_row(conn, source_id="publisher:bad", name="Bad")
            run_id = "test-run"
            now = utc_now_iso()
            self.ensure_run(conn, run_id)

            def fetch_ok(_row):
                return [RawSignalInput(source_id="publisher:good", lane="publisher", platform="rss",
                                        source_name="Good", source_role="signal",
                                        url="https://example.com/ok", title="OK item")]

            def fetch_fail(_row):
                raise RuntimeError("simulated network failure")

            outcome_good = collect_from_source(conn, good, run_id, now, fetch_ok)
            outcome_bad = collect_from_source(conn, bad, run_id, now, fetch_fail)

            self.assertTrue(outcome_good.succeeded)
            self.assertEqual(outcome_good.new_signals, 1)
            self.assertFalse(outcome_bad.succeeded)
            self.assertEqual(outcome_bad.error_type, "RuntimeError")

            errors = conn.execute("SELECT * FROM collection_errors WHERE source_id = ?", ("publisher:bad",)).fetchall()
            self.assertEqual(len(errors), 1)
            signals = conn.execute("SELECT * FROM raw_signals").fetchall()
            self.assertEqual(len(signals), 1)  # only the good source's item persisted


class YouTubeTimestampExtractionTests(unittest.TestCase):
    """Fixture-level tests for extract_published_at(), independent of any
    database -- pure function tests per the hardening requirements.

    extract_published_at() now returns (published_at, timestamp_precision).
    release_timestamp is a real scheduled/premiere moment -> "exact".
    timestamp (populated only via the approximate_date extractor arg) and
    upload_date (date-only, no time-of-day) are both -> "approximate".
    Nothing usable -> (None, "unknown")."""

    def test_unix_timestamp_field_is_approximate(self):
        result = extract_published_at({"timestamp": 1754826000})  # 2025-08-10T11:40:00Z
        self.assertEqual(result, ("2025-08-10T11:40:00Z", "approximate"))

    def test_release_timestamp_is_exact_and_preferred_over_timestamp(self):
        # Both present: release_timestamp (exact) must win, not whichever
        # field the extraction loop happens to check first.
        result = extract_published_at({"timestamp": 1700000000, "release_timestamp": 1754826000})
        self.assertEqual(result, ("2025-08-10T11:40:00Z", "exact"))

    def test_release_timestamp_used_when_timestamp_absent(self):
        result = extract_published_at({"timestamp": None, "release_timestamp": 1754826000})
        self.assertEqual(result, ("2025-08-10T11:40:00Z", "exact"))

    def test_upload_date_field_is_approximate(self):
        result = extract_published_at({"upload_date": "20260810"})
        self.assertEqual(result, ("2026-08-10T00:00:00Z", "approximate"))

    def test_missing_publication_metadata_returns_unknown(self):
        result = extract_published_at({"title": "No date fields at all"})
        self.assertEqual(result, (None, "unknown"))

    def test_invalid_timestamp_falls_through_without_raising(self):
        result = extract_published_at({"timestamp": "not-a-number", "upload_date": None})
        self.assertEqual(result, (None, "unknown"))

    def test_invalid_upload_date_falls_through_without_raising(self):
        result = extract_published_at({"upload_date": "not-a-date"})
        self.assertEqual(result, (None, "unknown"))

    def test_invalid_timestamp_falls_back_to_valid_upload_date(self):
        result = extract_published_at({"timestamp": "garbage", "upload_date": "20260101"})
        self.assertEqual(result, ("2026-01-01T00:00:00Z", "approximate"))

    def test_invalid_release_timestamp_falls_through_to_approximate_timestamp(self):
        result = extract_published_at({"release_timestamp": "garbage", "timestamp": 1754826000})
        self.assertEqual(result, ("2025-08-10T11:40:00Z", "approximate"))


class YouTubeCollectionTests(TrendRadarTestCase):
    def test_flat_entries_normalise_to_raw_signal_inputs(self):
        fake_info = {
            "entries": [
                {"id": "abc123", "title": "Tom Holland's CrossFit Cindy Workout", "uploader": "Fixture Creator",
                 "view_count": 50000, "timestamp": 1754826000},
                None,  # yt-dlp can yield None entries for unavailable videos; must be skipped, not crash
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="creator:fixture", lane="creator", platform="youtube")
            with mock.patch("trend_radar.collectors.youtube.yt_dlp.YoutubeDL") as mock_ydl_cls:
                mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
                mock_ydl.extract_info.return_value = fake_info
                items = fetch_youtube_channel_source(source_row)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].url, "https://www.youtube.com/watch?v=abc123")
            self.assertEqual(items[0].engagement.get("view_count"), 50000)
            self.assertEqual(items[0].published_at, "2025-08-10T11:40:00Z")
            self.assertEqual(items[0].timestamp_precision, "approximate")
            self.assertIsNone(items[0].raw_payload_ref)
            self.assertIn("abc123", items[0].raw_payload_json)

    def test_entry_with_release_timestamp_is_exact(self):
        fake_info = {"entries": [{"id": "premiere1", "title": "Scheduled premiere", "release_timestamp": 1754826000}]}
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="creator:fixture_premiere", lane="creator", platform="youtube")
            with mock.patch("trend_radar.collectors.youtube.yt_dlp.YoutubeDL") as mock_ydl_cls:
                mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
                mock_ydl.extract_info.return_value = fake_info
                items = fetch_youtube_channel_source(source_row)
            self.assertEqual(items[0].published_at, "2025-08-10T11:40:00Z")
            self.assertEqual(items[0].timestamp_precision, "exact")

    def test_entry_with_no_timestamp_fields_has_null_published_at_not_first_seen(self):
        fake_info = {"entries": [{"id": "xyz", "title": "No timestamp fields", "uploader": "Fixture Creator"}]}
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="creator:fixture2", lane="creator", platform="youtube")
            with mock.patch("trend_radar.collectors.youtube.yt_dlp.YoutubeDL") as mock_ydl_cls:
                mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
                mock_ydl.extract_info.return_value = fake_info
                items = fetch_youtube_channel_source(source_row)
            self.assertIsNone(items[0].published_at)
            self.assertEqual(items[0].timestamp_precision, "unknown")

    def test_invalid_metadata_does_not_abort_source_run(self):
        fake_info = {"entries": [
            {"id": "bad1", "title": "Bad timestamp", "timestamp": "not-a-number", "upload_date": "also-bad"},
            {"id": "good1", "title": "Good one", "upload_date": "20260810"},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="creator:fixture3", lane="creator", platform="youtube")
            with mock.patch("trend_radar.collectors.youtube.yt_dlp.YoutubeDL") as mock_ydl_cls:
                mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
                mock_ydl.extract_info.return_value = fake_info
                items = fetch_youtube_channel_source(source_row)  # must not raise
            self.assertEqual(len(items), 2)
            self.assertIsNone(items[0].published_at)
            self.assertEqual(items[0].timestamp_precision, "unknown")
            self.assertEqual(items[1].published_at, "2026-08-10T00:00:00Z")
            self.assertEqual(items[1].timestamp_precision, "approximate")

    def test_channel_with_no_videos_tab_raises_isolated_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="creator:no_tab", lane="creator", platform="youtube")
            with mock.patch("trend_radar.collectors.youtube.yt_dlp.YoutubeDL") as mock_ydl_cls:
                mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
                mock_ydl.extract_info.return_value = None
                with self.assertRaises(RuntimeError):
                    fetch_youtube_channel_source(source_row)


class RedditCollectionTests(TrendRadarTestCase):
    """Unit tests for the collector itself (collectors/reddit.py), which
    is preserved unchanged and untouched for later OAuth implementation.
    The runner-level "skip without any request" behavior is tested
    separately, below, in RedditRunnerSkipTests."""

    def test_unavailable_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="audience:fixture", lane="audience", platform="reddit",
                                                  canonical_identifier="crossfit")
            with mock.patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(tr_reddit.RedditUnavailable):
                    tr_reddit.fetch_reddit_source(source_row)

    def test_rate_limited_response_raises_without_retry(self):
        calls = []

        def fake_http_get(url):
            calls.append(url)
            return 429, ""

        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="audience:fixture2", lane="audience", platform="reddit",
                                                  canonical_identifier="crossfit")
            with mock.patch.dict("os.environ", {"REDDIT_CLIENT_ID": "x", "REDDIT_CLIENT_SECRET": "y"}):
                with self.assertRaises(tr_reddit.RedditRateLimited):
                    tr_reddit.fetch_reddit_source(source_row, http_get=fake_http_get)
            self.assertEqual(len(calls), 1, "must not retry after a 429")

    def test_fixture_atom_feed_parses_when_credentials_present(self):
        def fake_http_get(url):
            return 200, SAMPLE_REDDIT_ATOM

        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="audience:fixture3", lane="audience", platform="reddit",
                                                  canonical_identifier="crossfit")
            with mock.patch.dict("os.environ", {"REDDIT_CLIENT_ID": "x", "REDDIT_CLIENT_SECRET": "y"}):
                items = tr_reddit.fetch_reddit_source(source_row, http_get=fake_http_get)
            self.assertEqual(len(items), 1)
            self.assertIn("Cindy", items[0].title)

    def test_reddit_collector_failure_isolated_via_collect_from_source(self):
        """Proves collect_from_source() itself still isolates a Reddit
        failure correctly if something ever calls it directly (e.g. once
        OAuth exists and a token-refresh error occurs mid-run) -- this is
        independent of the runner's pre-emptive skip-without-credentials
        path tested in RedditRunnerSkipTests."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="audience:fixture4", lane="audience", platform="reddit",
                                                  canonical_identifier="crossfit")
            run_id = "test-run"
            now = utc_now_iso()
            self.ensure_run(conn, run_id)
            with mock.patch.dict("os.environ", {}, clear=True):
                outcome = collect_from_source(conn, source_row, run_id, now, tr_reddit.fetch_reddit_source)
            self.assertFalse(outcome.succeeded)
            self.assertEqual(outcome.error_type, "RedditUnavailable")
            row = conn.execute("SELECT access_status FROM sources WHERE source_id = ?", (source_row["source_id"],)).fetchone()
            self.assertEqual(row["access_status"], "degraded")


class RedditRunnerSkipTests(TrendRadarTestCase):
    """Runner-level behavior: with no OAuth credentials configured, Reddit
    sources must be skipped before any network call, reported distinctly
    from an ordinary failure, and must not write one collection_errors
    row per subreddit."""

    def _configs(self, n=8):
        return [
            {
                "source_id": f"audience:r_fixture{i}", "name": f"r/fixture{i}", "lane": "audience",
                "platform": "reddit", "canonical_identifier": f"fixture{i}",
                "collection_method": "reddit_anonymous_rss", "source_role": "signal", "active": True,
            }
            for i in range(n)
        ]

    def test_no_credentials_means_zero_network_calls_and_zero_error_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            with tempfile.TemporaryDirectory() as cfg_tmp:
                cfg_path = Path(cfg_tmp) / "sources.json"
                cfg_path.write_text(json.dumps({"sources": self._configs(8)}))
                with mock.patch.dict("os.environ", {}, clear=True), \
                     mock.patch("trend_radar.collectors.reddit._default_http_get") as mock_http:
                    summary = run_collection(conn, lanes=["audience"], source_configs_path=cfg_path)
                mock_http.assert_not_called()

            self.assertEqual(summary.sources_skipped, 8)
            self.assertEqual(summary.sources_attempted, 0)
            self.assertEqual(summary.sources_failed, 0)

            error_count = conn.execute("SELECT COUNT(*) FROM collection_errors").fetchone()[0]
            self.assertEqual(error_count, 0, "must not write one collection_errors row per skipped subreddit")

            statuses = {row["source_id"]: row["access_status"] for row in conn.execute("SELECT source_id, access_status FROM sources")}
            for i in range(8):
                self.assertEqual(statuses[f"audience:r_fixture{i}"], "credential_required")

            run_row = conn.execute("SELECT sources_skipped FROM collection_runs WHERE run_id = ?", (summary.run_id,)).fetchone()
            self.assertEqual(run_row["sources_skipped"], 8)

    def test_with_credentials_present_reddit_sources_are_attempted_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            with tempfile.TemporaryDirectory() as cfg_tmp:
                cfg_path = Path(cfg_tmp) / "sources.json"
                cfg_path.write_text(json.dumps({"sources": self._configs(1)}))
                with mock.patch.dict("os.environ", {"REDDIT_CLIENT_ID": "x", "REDDIT_CLIENT_SECRET": "y"}), \
                     mock.patch("trend_radar.collectors.reddit._default_http_get", return_value=(200, SAMPLE_REDDIT_ATOM)) as mock_http:
                    summary = run_collection(conn, lanes=["audience"], source_configs_path=cfg_path)
                mock_http.assert_called_once()
            self.assertEqual(summary.sources_skipped, 0)
            self.assertEqual(summary.sources_attempted, 1)
            self.assertEqual(summary.sources_succeeded, 1)


class PubMedCollectionTests(TrendRadarTestCase):
    def test_fixture_pmids_and_xml_normalise_to_raw_signals(self):
        from trend_radar.collectors.research import fetch_pubmed_source

        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            source_row = self.fixture_source_row(conn, source_id="research:fixture", lane="research", platform="pubmed",
                                                  canonical_identifier="exercise trend[Title]")
            with mock.patch("trend_radar.collectors.research.scanner.pubmed_search", return_value=["12345"]), \
                 mock.patch("trend_radar.collectors.research.scanner.pubmed_fetch", return_value="<xml/>"), \
                 mock.patch("trend_radar.collectors.research.scanner.extract_pubmed_articles") as mock_extract:
                from scanner import ResearchPaper
                mock_extract.return_value = [
                    ResearchPaper(
                        pmid="12345", doi=None, topic_group="fixture", title="Fixture Study",
                        journal="Fixture Journal", authors=[], abstract="Fixture abstract",
                        publication_date="2026-08-01", electronic_publication_date=None,
                        indexed_at=None, publication_types=[],
                    )
                ]
                items = fetch_pubmed_source(source_row)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].title, "Fixture Study")
            self.assertIn("12345", items[0].url)


class ManualIntakeTests(TrendRadarTestCase):
    def test_add_manual_signal_creates_labelled_raw_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            signal_id = trmanual.add_manual_signal(
                conn,
                url="https://instagram.com/p/fixture",
                title_or_description="Seeing this everywhere on Instagram",
                observation="No article yet, but attempts flooding my feed.",
                observed_platform="instagram",
            )
            row = conn.execute("SELECT * FROM raw_signals WHERE signal_id = ?", (signal_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["lane"], "manual")
            self.assertEqual(row["source_role"], "manual")
            self.assertEqual(row["is_manual"], 1)
            self.assertIn("instagram", row["body_text"])

    def test_each_manual_submission_creates_its_own_completed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            trmanual.add_manual_signal(conn, url="https://example.com/a", title_or_description="A", observation="first")
            trmanual.add_manual_signal(conn, url="https://example.com/b", title_or_description="B", observation="second")
            runs = conn.execute("SELECT run_id, status, lanes FROM collection_runs WHERE lanes = 'manual'").fetchall()
            self.assertEqual(len(runs), 2, "each manual submission must create its own run, not share one placeholder")
            run_ids = {r["run_id"] for r in runs}
            self.assertEqual(len(run_ids), 2)
            for r in runs:
                self.assertTrue(r["run_id"].startswith("manual:"))
                self.assertNotEqual(r["run_id"], "manual:")  # must have a real unique suffix
                self.assertEqual(r["status"], "completed")

    def test_repeated_manual_submission_same_url_dedupes_but_each_observation_has_its_own_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            id1 = trmanual.add_manual_signal(conn, url="https://example.com/x", title_or_description="X", observation="first note")
            id2 = trmanual.add_manual_signal(conn, url="https://example.com/x", title_or_description="X", observation="second note, same URL")

            self.assertEqual(id1, id2, "same URL must dedupe to one raw_signals row")
            signal_count = conn.execute("SELECT COUNT(*) FROM raw_signals WHERE signal_id = ?", (id1,)).fetchone()[0]
            self.assertEqual(signal_count, 1)

            observations = conn.execute(
                "SELECT run_id FROM signal_observations WHERE signal_id = ? ORDER BY observation_id", (id1,)
            ).fetchall()
            self.assertEqual(len(observations), 2, "two submissions must leave two observation records")
            run_id_1, run_id_2 = observations[0]["run_id"], observations[1]["run_id"]
            self.assertNotEqual(run_id_1, run_id_2, "each observation must reference its own manual run")

            for run_id in (run_id_1, run_id_2):
                run_row = conn.execute("SELECT status, lanes FROM collection_runs WHERE run_id = ?", (run_id,)).fetchone()
                self.assertEqual(run_row["status"], "completed")
                self.assertEqual(run_row["lanes"], "manual")


class DeduplicationTests(TrendRadarTestCase):
    def test_same_url_different_title_case_dedupes_via_canonical_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn)
            self.ensure_run(conn, "r1")
            sig1 = RawSignalInput(source_id="publisher:fixture", lane="publisher", platform="rss",
                                   source_name="Fixture Publisher", source_role="signal",
                                   url="https://example.com/story?utm_source=twitter", title="A Story")
            sig2 = RawSignalInput(source_id="publisher:fixture", lane="publisher", platform="rss",
                                   source_name="Fixture Publisher", source_role="signal",
                                   url="https://example.com/story?utm_source=facebook", title="A Story")
            id1, is_new1 = upsert_signal(conn, sig1, run_id="r1")
            id2, is_new2 = upsert_signal(conn, sig2, run_id="r1")
            self.assertTrue(is_new1)
            self.assertFalse(is_new2, "utm-only URL variants must fingerprint identically via canonical_url()")
            self.assertEqual(id1, id2)

    def test_youtube_id_dedup_across_different_url_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn, source_id="creator:fixture", lane="creator", platform="youtube")
            self.ensure_run(conn, "r1")
            sig1 = RawSignalInput(source_id="creator:fixture", lane="creator", platform="youtube",
                                   source_name="Fixture Creator", source_role="signal",
                                   url="https://www.youtube.com/watch?v=abc123XYZ_", title="Video")
            sig2 = RawSignalInput(source_id="creator:fixture", lane="creator", platform="youtube",
                                   source_name="Fixture Creator", source_role="signal",
                                   url="https://youtu.be/abc123XYZ_", title="Video (short link)")
            id1, is_new1 = upsert_signal(conn, sig1, run_id="r1")
            id2, is_new2 = upsert_signal(conn, sig2, run_id="r1")
            self.assertTrue(is_new1)
            self.assertFalse(is_new2)
            self.assertEqual(id1, id2)

    def test_manual_signal_without_url_fingerprints_on_content(self):
        sig = RawSignalInput(source_id="manual:editor-submitted", lane="manual", platform="manual",
                              source_name="x", source_role="manual", url=None, title="Some observed thing")
        fp = fingerprint_for(sig)
        self.assertTrue(fp.startswith("manual:"))


class ObservationHistoryTests(TrendRadarTestCase):
    def test_repeated_collection_updates_last_seen_and_appends_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.fixture_source_row(conn)
            self.ensure_run(conn, "r1")
            self.ensure_run(conn, "r2")
            sig = RawSignalInput(source_id="publisher:fixture", lane="publisher", platform="rss",
                                  source_name="Fixture Publisher", source_role="signal",
                                  url="https://example.com/growing-story", title="Growing Story",
                                  engagement={"view_count": 100})
            id1, _ = upsert_signal(conn, sig, run_id="r1", now="2026-08-10T00:00:00Z")
            sig.engagement = {"view_count": 5000}
            id2, is_new = upsert_signal(conn, sig, run_id="r2", now="2026-08-11T00:00:00Z")

            self.assertEqual(id1, id2)
            self.assertFalse(is_new)
            row = conn.execute("SELECT last_seen_at, first_seen_at FROM raw_signals WHERE signal_id = ?", (id1,)).fetchone()
            self.assertEqual(row["last_seen_at"], "2026-08-11T00:00:00Z")
            self.assertEqual(row["first_seen_at"], "2026-08-10T00:00:00Z")

            observations = conn.execute(
                "SELECT engagement_json FROM signal_observations WHERE signal_id = ? ORDER BY observation_id", (id1,)
            ).fetchall()
            self.assertEqual(len(observations), 2)
            self.assertIn('"view_count": 100', observations[0]["engagement_json"])
            self.assertIn('"view_count": 5000', observations[1]["engagement_json"])


class RunOrchestrationTests(TrendRadarTestCase):
    def test_run_collection_writes_summary_row_and_supports_test_db_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "run_test.db"
            conn = self.connect(db_path)
            configs = [{
                "source_id": "publisher:orchestration_fixture", "name": "Fixture", "lane": "publisher",
                "platform": "rss", "canonical_identifier": "https://example.com/feed",
                "collection_method": "rss_atom", "source_role": "signal", "active": True,
            }]
            with tempfile.TemporaryDirectory() as cfg_tmp:
                cfg_path = Path(cfg_tmp) / "sources.json"
                cfg_path.write_text(json.dumps({"sources": configs}))
                with mock.patch("trend_radar.collectors.publishers.scanner.http_get_text", return_value=SAMPLE_RSS):
                    summary = run_collection(conn, lanes=["publisher"], source_configs_path=cfg_path)

            self.assertEqual(summary.sources_attempted, 1)
            self.assertEqual(summary.sources_succeeded, 1)
            self.assertEqual(summary.new_signals, 2)

            run_row = conn.execute("SELECT * FROM collection_runs WHERE run_id = ?", (summary.run_id,)).fetchone()
            self.assertEqual(run_row["status"], "completed")
            self.assertEqual(run_row["raw_signals_collected"], 2)
            self.assertTrue(db_path.exists())


if __name__ == "__main__":
    unittest.main()
