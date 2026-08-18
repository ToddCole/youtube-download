"""Raw signal fingerprinting and dedup-aware persistence.

Reuses scanner.py's canonical_url()/fingerprints_for_values() -- the same
identity-normalization layer the old scanner uses for archive dedup --
rather than re-deriving URL/video-ID canonicalization from scratch.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from ._scanner_bridge import scanner
from .models import RawSignalInput


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fingerprint_for(signal: RawSignalInput) -> str:
    """A single stable fingerprint string per signal, priority-ordered
    youtube > pmid > doi > canonical url > manual fallback. Manual
    signals (no URL, e.g. a bare editor observation) fingerprint on
    lane+source+title so identical repeated notes still dedupe instead of
    silently accumulating duplicates."""
    values = [v for v in (signal.url, signal.title) if v]
    fps = scanner.fingerprints_for_values(*values) if values else set()
    for prefix in ("youtube:", "pmid:", "doi:"):
        for fp in fps:
            if fp.startswith(prefix):
                return fp
    for fp in fps:
        if fp.startswith("url:"):
            return fp
    normalized_title = (signal.title or "").strip().lower()
    return f"manual:{signal.lane}:{signal.source_id}:{normalized_title}"


def upsert_signal(
    conn: sqlite3.Connection,
    signal: RawSignalInput,
    run_id: str,
    now: str | None = None,
) -> tuple[str, bool]:
    """Writes one raw signal. Returns (signal_id, is_new).

    A repeatedly-collected item (same content_fingerprint) never creates a
    second raw_signals row: last_seen_at/collected_at are updated in
    place and a new signal_observations row records the fresh engagement
    snapshot, preserving growth history without uncontrolled duplication.
    """
    now = now or utc_now_iso()
    fp = fingerprint_for(signal)
    engagement_json = json.dumps(signal.engagement) if signal.engagement else None

    existing = conn.execute(
        "SELECT signal_id FROM raw_signals WHERE content_fingerprint = ?", (fp,)
    ).fetchone()

    if existing:
        signal_id = existing["signal_id"]
        conn.execute(
            """UPDATE raw_signals
               SET last_seen_at = ?, collected_at = ?,
                   raw_engagement_json = COALESCE(?, raw_engagement_json)
               WHERE signal_id = ?""",
            (now, now, engagement_json, signal_id),
        )
        conn.execute(
            "INSERT INTO signal_observations (signal_id, observed_at, engagement_json, run_id) VALUES (?, ?, ?, ?)",
            (signal_id, now, engagement_json, run_id),
        )
        return signal_id, False

    signal_id = str(uuid.uuid4())
    canonical = scanner.canonical_url(signal.url) if signal.url else None
    conn.execute(
        """INSERT INTO raw_signals (
            signal_id, source_id, lane, platform, source_name, source_role,
            url, canonical_url, title, body_text, author_or_account, published_at,
            timestamp_precision, first_seen_at, last_seen_at, collected_at,
            content_fingerprint, raw_engagement_json, raw_payload_ref,
            raw_payload_json, is_manual, created_by_run_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            signal_id, signal.source_id, signal.lane, signal.platform,
            signal.source_name, signal.source_role, signal.url, canonical,
            signal.title, signal.body_text, signal.author_or_account,
            signal.published_at, signal.timestamp_precision, now, now, now, fp,
            engagement_json, signal.raw_payload_ref, signal.raw_payload_json,
            int(signal.is_manual), run_id,
        ),
    )
    conn.execute(
        "INSERT INTO signal_observations (signal_id, observed_at, engagement_json, run_id) VALUES (?, ?, ?, ?)",
        (signal_id, now, engagement_json, run_id),
    )
    return signal_id, True
