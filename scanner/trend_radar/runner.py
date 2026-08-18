"""Orchestrates one collection run: creates the collection_runs row,
dispatches each active source to its platform's collector, isolates
per-source failures (collect_from_source), and aggregates the summary
this phase's completion report and CLI output are built from.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field

from .collectors import publishers, reddit, research, youtube
from .collectors.base import collect_from_source
from .models import SourceCollectionOutcome
from .signals import utc_now_iso
from .sources import active_sources, sync_sources, load_source_configs, update_source_status

_FETCHERS = {
    "rss": publishers.fetch_for,
    "google-news": publishers.fetch_for,
    "youtube": youtube.fetch_youtube_channel_source,
    "reddit": reddit.fetch_reddit_source,
    "pubmed": research.fetch_for,
}


@dataclass
class RunSummary:
    run_id: str
    lanes: list[str]
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    sources_skipped: int = 0
    new_signals: int = 0
    duplicate_signals: int = 0
    observations: int = 0
    outcomes: list[SourceCollectionOutcome] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


def _fetcher_for(platform: str):
    fetcher = _FETCHERS.get(platform)
    if fetcher is None:
        raise ValueError(f"no collector registered for platform {platform!r}")
    return fetcher


def run_collection(
    conn: sqlite3.Connection,
    lanes: list[str] | None = None,
    source_configs_path=None,
) -> RunSummary:
    """Collects every active source in the given lanes (all lanes if
    None). Loads/syncs trend_sources.json first so newly-added or
    newly-deactivated sources are reflected before this run reads
    `sources`."""
    sync_sources(conn, load_source_configs(source_configs_path))

    run_id = str(uuid.uuid4())
    started_at = utc_now_iso()
    lane_list = lanes or ["publisher", "audience", "creator", "research"]
    conn.execute(
        "INSERT INTO collection_runs (run_id, started_at, lanes, status) VALUES (?,?,?,?)",
        (run_id, started_at, ",".join(lane_list), "running"),
    )
    conn.commit()

    summary = RunSummary(run_id=run_id, lanes=lane_list, started_at=started_at)

    sources_to_collect = []
    for lane in lane_list:
        sources_to_collect.extend(active_sources(conn, lane))

    reddit_credentials_present = reddit.has_oauth_credentials()

    for source_row in sources_to_collect:
        now = utc_now_iso()

        if source_row["platform"] == "reddit" and not reddit_credentials_present:
            # No network request is made for this source at all. Reported
            # as skipped/credential_required, not an ordinary collection
            # failure -- no collection_errors row is written, so eight
            # configured subreddits don't produce eight repetitive error
            # rows for the same one missing credential. collectors/reddit.py
            # and its fixture tests are untouched; real collection resumes
            # automatically the moment REDDIT_CLIENT_ID/SECRET are set,
            # with no change needed here.
            reason = "Reddit OAuth credentials not configured (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET unset)"
            update_source_status(conn, source_row["source_id"], "credential_required", now)
            summary.outcomes.append(
                SourceCollectionOutcome(source_id=source_row["source_id"], attempted=False, skipped=True, skip_reason=reason)
            )
            summary.sources_skipped += 1
            continue

        fetcher = _fetcher_for(source_row["platform"])
        outcome = collect_from_source(conn, source_row, run_id, now, fetcher)
        summary.outcomes.append(outcome)
        summary.sources_attempted += 1
        if outcome.succeeded:
            summary.sources_succeeded += 1
        else:
            summary.sources_failed += 1
        summary.new_signals += outcome.new_signals
        summary.duplicate_signals += outcome.duplicate_signals
        summary.observations += outcome.observations

    summary.finished_at = utc_now_iso()
    conn.execute(
        """UPDATE collection_runs SET
            finished_at = ?, sources_attempted = ?, sources_succeeded = ?,
            sources_failed = ?, sources_skipped = ?, raw_signals_collected = ?,
            duplicates_suppressed = ?, observations_recorded = ?, status = ?
           WHERE run_id = ?""",
        (
            summary.finished_at, summary.sources_attempted, summary.sources_succeeded,
            summary.sources_failed, summary.sources_skipped, summary.new_signals,
            summary.duplicate_signals, summary.observations, "completed", run_id,
        ),
    )
    conn.commit()
    return summary
