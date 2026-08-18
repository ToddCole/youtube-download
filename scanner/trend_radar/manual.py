"""Manual editor-submitted signal intake. Enters the exact same
raw_signals structure as any automated collector, labelled
lane="manual"/source_role="manual"/is_manual=True -- this is the
sanctioned way an Instagram/TikTok/Facebook/Google-result observation
that no automated collector can reach enters the pipeline, per the
approved architecture: a human is the access layer, not a scraper.

Each manual submission is its own auditable event: it creates its own
completed collection_runs row (run_id="manual:<uuid>") rather than
sharing one permanent placeholder run. Submitting the same URL twice
still dedupes to one raw_signals row (via the normal fingerprint
mechanism in signals.py), but produces two signal_observations rows,
each referencing the specific manual run that produced it -- so "when
was this seen and by which submission" stays answerable per observation,
not collapsed into one undated bucket.

CLI-only for Phase 1 (`python3 -m trend_radar add-manual`). Dashboard
intake is Phase 7 scope, not this phase.
"""

from __future__ import annotations

import sqlite3
import uuid

from .models import RawSignalInput
from .signals import upsert_signal, utc_now_iso
from .sources import update_source_status

MANUAL_SOURCE_ID = "manual:editor-submitted"


def create_manual_run(conn: sqlite3.Connection, now: str | None = None) -> str:
    """Creates and returns a fresh, already-completed collection_runs row
    for one manual submission. Never reused across submissions -- this is
    what makes each manual signal's observation individually auditable
    (which submission event produced it, and when)."""
    now = now or utc_now_iso()
    run_id = f"manual:{uuid.uuid4()}"
    conn.execute(
        """INSERT INTO collection_runs (
            run_id, started_at, finished_at, lanes, status,
            sources_attempted, sources_succeeded
        ) VALUES (?,?,?,?,?,?,?)""",
        (run_id, now, now, "manual", "completed", 1, 1),
    )
    conn.commit()
    return run_id


def ensure_manual_source(conn: sqlite3.Connection) -> None:
    """The manual lane's one pseudo-source row, so raw_signals'
    source_id foreign key is always satisfiable even though "manual"
    isn't a polled feed with its own URL. This is a source-identity
    singleton (there is only one "manual submission" source), unlike
    collection_runs, where every submission gets its own row."""
    existing = conn.execute(
        "SELECT source_id FROM sources WHERE source_id = ?", (MANUAL_SOURCE_ID,)
    ).fetchone()
    if existing:
        return
    conn.execute(
        """INSERT INTO sources (
            source_id, name, lane, platform, canonical_identifier,
            collection_method, collection_interval_hours, reliability,
            source_role, active, known_restrictions, engagement_available,
            access_status, config_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            MANUAL_SOURCE_ID, "Editor manual submission", "manual", "manual",
            "manual", "manual_intake", 0, None, "manual", 1,
            "Human-submitted only; not polled.", 0, "ok", "{}",
        ),
    )
    conn.commit()


def add_manual_signal(
    conn: sqlite3.Connection,
    url: str | None,
    title_or_description: str,
    observation: str,
    observed_platform: str | None = None,
    observed_at: str | None = None,
) -> str:
    """Records one manually-submitted signal. Returns the signal_id.

    `observation` (the editor's own note -- e.g. "seeing this all over
    Instagram, no article yet") is stored in body_text alongside
    title_or_description so it enters phrase-mining/co-occurrence
    analysis in Phase 2 exactly like any automated signal's text does.

    A repeated submission of the same URL/content dedupes to the same
    raw_signals row (fingerprint-based, same as any collector), but this
    call's own fresh manual run is what its new signal_observations row
    references -- so two submissions of one URL leave two distinctly
    attributable observations, not one merged into the other's run.
    """
    ensure_manual_source(conn)
    now = utc_now_iso()
    run_id = create_manual_run(conn, now=now)

    body_parts = [observation.strip()]
    if observed_platform:
        body_parts.append(f"[observed on: {observed_platform}]")
    body_text = "\n".join(p for p in body_parts if p)

    signal = RawSignalInput(
        source_id=MANUAL_SOURCE_ID,
        lane="manual",
        platform=observed_platform or "manual",
        source_name="Editor manual submission",
        source_role="manual",
        url=url,
        title=title_or_description.strip(),
        body_text=body_text,
        author_or_account=None,
        published_at=observed_at,
        is_manual=True,
    )
    signal_id, _ = upsert_signal(conn, signal, run_id=run_id, now=now)
    conn.commit()
    update_source_status(conn, MANUAL_SOURCE_ID, "ok", now)
    return signal_id
