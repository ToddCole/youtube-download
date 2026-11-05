"""Evidence packet construction for the Claude-powered scan.

Reads raw_signals directly (Phase 1's collection/manual-intake table,
unchanged) and deduplicates by exact canonical-URL/URL identity only --
no semantic grouping happens in Python anywhere in this module. Grouping
differently-worded signals about the same subject is Claude's job in
Pass A, per the brief.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BODY_EXCERPT_MAX_CHARS = 280


@dataclass
class EvidenceRecord:
    signal_id: str
    lane: str
    platform: str
    source: str
    published_at: str | None
    timestamp_precision: str | None
    title: str
    body_excerpt: str
    url: str | None
    discovery_method: str

    def to_prompt_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "lane": self.lane,
            "platform": self.platform,
            "source": self.source,
            "published_at": self.published_at,
            "timestamp_precision": self.timestamp_precision,
            "title": self.title,
            "body_excerpt": self.body_excerpt,
            "url": self.url,
            "discovery_method": self.discovery_method,
        }


def _excerpt(body_text: str | None) -> str:
    text = (body_text or "").strip()
    if len(text) <= BODY_EXCERPT_MAX_CHARS:
        return text
    return text[:BODY_EXCERPT_MAX_CHARS].rstrip() + "…"


def _discovery_method(row: sqlite3.Row) -> str:
    run_id = row["created_by_run_id"] or ""
    if run_id.startswith("manual:"):
        return "manual"
    if run_id.startswith("expand:"):
        return f"expand:{row['platform']}"
    return f"collect:{row['platform']}"


def _row_to_record(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        signal_id=row["signal_id"],
        lane=row["lane"],
        platform=row["platform"],
        source=row["author_or_account"] or row["source_name"],
        published_at=row["published_at"],
        timestamp_precision=row["timestamp_precision"],
        title=row["title"] or "",
        body_excerpt=_excerpt(row["body_text"]),
        url=row["url"],
        discovery_method=_discovery_method(row),
    )


def build_evidence_packet(
    conn: sqlite3.Connection,
    lookback_days: int,
    max_signals: int,
    now: datetime | None = None,
) -> list[EvidenceRecord]:
    """Signals from the last `lookback_days`, newest first, deduplicated
    by canonical_url (falling back to url, then to the signal_id itself
    for URL-less manual notes) -- exact identity only. Capped at
    `max_signals` records."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=lookback_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = conn.execute(
        """SELECT signal_id, lane, platform, source_name, author_or_account,
                  published_at, timestamp_precision, title, body_text, url,
                  canonical_url, first_seen_at, created_by_run_id
           FROM raw_signals
           WHERE COALESCE(published_at, first_seen_at) >= ?
           ORDER BY COALESCE(published_at, first_seen_at) DESC, signal_id ASC""",
        (cutoff,),
    ).fetchall()

    seen_identity: set[str] = set()
    records: list[EvidenceRecord] = []
    for row in rows:
        identity = row["canonical_url"] or row["url"] or f"signal:{row['signal_id']}"
        if identity in seen_identity:
            continue
        seen_identity.add(identity)
        records.append(_row_to_record(row))
        if len(records) >= max_signals:
            break
    return records


def record_for_signal_id(conn: sqlite3.Connection, signal_id: str) -> EvidenceRecord | None:
    """Looks up one signal by ID directly (used to build EvidenceRecords
    for freshly-expanded signals without re-running the whole packet
    query)."""
    row = conn.execute(
        """SELECT signal_id, lane, platform, source_name, author_or_account,
                  published_at, timestamp_precision, title, body_text, url,
                  canonical_url, first_seen_at, created_by_run_id
           FROM raw_signals WHERE signal_id = ?""",
        (signal_id,),
    ).fetchone()
    return _row_to_record(row) if row else None
