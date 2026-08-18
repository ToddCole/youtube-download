"""Trend Radar's own SQLite database. Never scanner.db.

The old scanner and the Editorial Desk already share scanner.db as two
disjoint writers. Trend Radar does not become a third writer to that
production database -- it owns a completely separate file. It may later
read scanner.db / mfo_index.json through a read-only adapter (archive
protection, a later phase); it must never open scanner.db for writing.

No import-time database or network side effects: connect() only runs when
called.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "trend_radar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lane TEXT NOT NULL CHECK(lane IN ('publisher', 'audience', 'creator', 'research', 'manual')),
    platform TEXT NOT NULL,
    canonical_identifier TEXT NOT NULL,
    collection_method TEXT NOT NULL,
    collection_interval_hours REAL NOT NULL DEFAULT 4,
    reliability INTEGER,
    source_role TEXT NOT NULL CHECK(source_role IN ('signal', 'authority', 'verification', 'manual')),
    active INTEGER NOT NULL DEFAULT 1,
    known_restrictions TEXT,
    engagement_available INTEGER NOT NULL DEFAULT 0,
    access_status TEXT NOT NULL DEFAULT 'unknown',
    last_collected_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    config_json TEXT
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    lanes TEXT NOT NULL,
    sources_attempted INTEGER NOT NULL DEFAULT 0,
    sources_succeeded INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    sources_skipped INTEGER NOT NULL DEFAULT 0,
    raw_signals_collected INTEGER NOT NULL DEFAULT 0,
    duplicates_suppressed INTEGER NOT NULL DEFAULT 0,
    observations_recorded INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS collection_errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
    source_id TEXT,
    occurred_at TEXT NOT NULL,
    error_type TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_collection_errors_run ON collection_errors(run_id);

CREATE TABLE IF NOT EXISTS raw_signals (
    signal_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    lane TEXT NOT NULL,
    platform TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_role TEXT NOT NULL,
    url TEXT,
    canonical_url TEXT,
    title TEXT,
    body_text TEXT,
    author_or_account TEXT,
    published_at TEXT,
    timestamp_precision TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL UNIQUE,
    raw_engagement_json TEXT,
    raw_payload_ref TEXT,   -- reserved for a genuine external file/object/blob reference, never inline JSON
    raw_payload_json TEXT,  -- inline preserved source-API metadata (e.g. the raw yt-dlp entry), when small enough to store directly
    is_manual INTEGER NOT NULL DEFAULT 0,
    created_by_run_id TEXT REFERENCES collection_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_signals_source ON raw_signals(source_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_raw_signals_lane ON raw_signals(lane, last_seen_at);

CREATE TABLE IF NOT EXISTS signal_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL REFERENCES raw_signals(signal_id),
    observed_at TEXT NOT NULL,
    engagement_json TEXT,
    run_id TEXT REFERENCES collection_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_signal_observations_signal ON signal_observations(signal_id, observed_at);
"""


class ClosingConnection(sqlite3.Connection):
    """Same pattern as main.py's ClosingSQLiteConnection: `with connect(...)
    as conn:` closes the connection on exit, not just commits/rolls back
    the transaction (sqlite3.Connection's own __exit__ does not close)."""

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Additive migration helper, same idiom as scanner.py's own
    ensure_column(): adds `column` to `table` if an older copy of this
    database predates it. Never touches existing data."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the Trend Radar database and ensure its
    schema exists. Always a distinct file from scanner.db -- callers pass
    an explicit path for tests; production code uses the module default.

    Returns a ClosingConnection: use `with connect(...) as conn:` (or call
    `.close()` explicitly) so every connection is closed deterministically
    -- this is a hard requirement, not just tidiness, once more than one
    process may open this file.
    """
    conn = sqlite3.connect(str(path) if path is not None else DB_PATH, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    # Deliberately not `with conn:` here -- ClosingConnection's __exit__
    # closes the connection, and this function must return it open.
    conn.executescript(SCHEMA)
    conn.commit()
    ensure_column(conn, "collection_runs", "sources_skipped", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "raw_signals", "timestamp_precision", "TEXT")
    ensure_column(conn, "raw_signals", "raw_payload_json", "TEXT")
    return conn


def migrate_youtube_raw_payload_ref_to_json(conn: sqlite3.Connection) -> dict:
    """One-time content migration, not run automatically by connect().

    An earlier version of the YouTube collector wrote its inline metadata
    JSON into raw_payload_ref, which is reserved for a genuine external
    file/object/blob reference. This moves any such rows into the correct
    column (raw_payload_json), leaving genuine non-JSON references in
    raw_payload_ref untouched, and is safe to call again afterwards --
    already-migrated rows no longer have an empty raw_payload_json, so
    they no longer match the candidate query.

    Only touches rows that are platform='youtube', have an empty
    raw_payload_json, and hold text in raw_payload_ref that parses as
    valid JSON. Each candidate is parsed and validated before being
    changed. All updates happen in one transaction; if the number of
    rows actually updated doesn't match the number of validated
    candidates, the whole migration rolls back rather than leaving a
    partial move.
    """
    candidates = conn.execute(
        """SELECT signal_id, raw_payload_ref FROM raw_signals
           WHERE platform = 'youtube'
             AND (raw_payload_json IS NULL OR raw_payload_json = '')
             AND raw_payload_ref IS NOT NULL AND raw_payload_ref != ''"""
    ).fetchall()

    to_migrate = []
    skipped_invalid_json = []
    for row in candidates:
        try:
            json.loads(row["raw_payload_ref"])
        except (json.JSONDecodeError, TypeError):
            skipped_invalid_json.append(row["signal_id"])
            continue
        to_migrate.append(row["signal_id"])

    conn.execute("BEGIN")
    try:
        updated = 0
        for signal_id in to_migrate:
            cur = conn.execute(
                """UPDATE raw_signals SET raw_payload_json = raw_payload_ref, raw_payload_ref = NULL
                   WHERE signal_id = ? AND platform = 'youtube'
                     AND (raw_payload_json IS NULL OR raw_payload_json = '')
                     AND raw_payload_ref IS NOT NULL""",
                (signal_id,),
            )
            updated += cur.rowcount
        if updated != len(to_migrate):
            raise RuntimeError(
                f"migration row-count mismatch: expected to update {len(to_migrate)}, actually updated {updated}"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "candidates_considered": len(candidates),
        "migrated": len(to_migrate),
        "skipped_invalid_json": skipped_invalid_json,
    }
