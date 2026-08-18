"""Shared per-source error isolation: one bad source records an error and
moves on, it never aborts the run. Same discipline the old scanner uses
per-channel/per-feed/per-topic-group in scanner.py's own collection loops.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from ..models import RawSignalInput, SourceCollectionOutcome
from ..signals import upsert_signal
from ..sources import update_source_status


def record_error(
    conn: sqlite3.Connection,
    run_id: str,
    source_id: str | None,
    error_type: str,
    message: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO collection_errors (run_id, source_id, occurred_at, error_type, message) VALUES (?,?,?,?,?)",
        (run_id, source_id, now, error_type, message[:2000]),
    )
    conn.commit()


def collect_from_source(
    conn: sqlite3.Connection,
    source_row: sqlite3.Row,
    run_id: str,
    now: str,
    fetch_fn: Callable[[sqlite3.Row], list[RawSignalInput]],
) -> SourceCollectionOutcome:
    """Runs one source's fetch_fn, isolating any exception it raises.
    On success, upserts every returned signal (existing signals get an
    updated last_seen_at + a new observation, never a duplicate row)."""
    outcome = SourceCollectionOutcome(source_id=source_row["source_id"])
    try:
        raw_items = fetch_fn(source_row)
    except Exception as exc:  # noqa: BLE001 - deliberate: isolate any collector failure
        outcome.error = str(exc)[:500]
        outcome.error_type = type(exc).__name__
        record_error(conn, run_id, source_row["source_id"], outcome.error_type, outcome.error, now)
        update_source_status(conn, source_row["source_id"], "degraded", now, last_error=outcome.error)
        return outcome

    for item in raw_items:
        _, is_new = upsert_signal(conn, item, run_id, now)
        if is_new:
            outcome.new_signals += 1
        else:
            outcome.duplicate_signals += 1
        outcome.observations += 1
    conn.commit()

    outcome.succeeded = True
    update_source_status(conn, source_row["source_id"], "ok", now)
    return outcome
