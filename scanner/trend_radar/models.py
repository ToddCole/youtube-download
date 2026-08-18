"""Phase 1 data shapes. Deliberately narrow: raw collection and manual
intake only. No entity/cluster/trend structures here yet -- those belong
to Phase 2 onward per the approved plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawSignalInput:
    """What a collector (or manual intake) produces, before it is written
    to the database. signals.upsert_signal() turns this into either a new
    raw_signals row + its first signal_observations row, or -- if a
    signal with the same content_fingerprint already exists -- an update
    to last_seen_at/collected_at plus a new observation, never a
    duplicate row."""

    source_id: str
    lane: str
    platform: str
    source_name: str
    source_role: str
    url: str | None
    title: str | None
    body_text: str = ""
    author_or_account: str | None = None
    published_at: str | None = None
    timestamp_precision: str | None = None  # "exact" | "approximate" | "unknown" -- see signals.py
    engagement: dict[str, Any] = field(default_factory=dict)
    raw_payload_ref: str | None = None  # a genuine external file/object/blob reference -- never inline JSON
    raw_payload_json: str | None = None  # inline preserved source-API metadata (e.g. the raw yt-dlp entry)
    is_manual: bool = False


@dataclass
class SourceCollectionOutcome:
    """Per-source result of one collection attempt, aggregated into the
    collection_runs summary and used for the completion report.

    `skipped` is distinct from a failure: it means collection was never
    attempted at all (e.g. a Reddit source with no OAuth credentials
    configured) -- no network request was made, and no collection_errors
    row is written for it. `succeeded`/`error*` describe an attempt that
    was actually made."""

    source_id: str
    attempted: bool = True
    succeeded: bool = False
    skipped: bool = False
    skip_reason: str | None = None
    new_signals: int = 0
    duplicate_signals: int = 0
    observations: int = 0
    error: str | None = None
    error_type: str | None = None
