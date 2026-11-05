"""Controlled expansion: executes the search queries Claude proposed in
Pass A, using the two existing search channels already available to
Trend Radar -- Google News RSS search (same URL pattern as
collectors/publishers.py's fetch_google_news_source) and a small
yt-dlp-based YouTube search (same options idiom as
collectors/youtube.py's channel fetcher, with a `ytsearchN:` prefix
instead of a channel URL).

No new persistence model: results are upserted through the existing
signals.upsert_signal() dedup path into the same raw_signals table
everything else uses, under a fresh, individually-auditable
collection_runs row -- the same pattern manual.py already uses for
manual submissions, just for expansion instead.

Budget-capped at MAX_TOTAL_SEARCHES per run. One failing search never
aborts the others (per-query try/except isolation, same discipline as
collectors/base.py's collect_from_source).
"""

from __future__ import annotations

import sqlite3
import uuid
from urllib.parse import quote_plus

import yt_dlp

from ._scanner_bridge import scanner
from .claude_evidence import EvidenceRecord, record_for_signal_id
from .claude_schemas import DiscoveryWave, ExpansionQueryProposal
from .collectors.youtube import extract_published_at
from .models import RawSignalInput
from .signals import upsert_signal, utc_now_iso
from .sources import update_source_status

MAX_TOTAL_SEARCHES = 12
YOUTUBE_SEARCH_RESULTS = 8

GOOGLE_NEWS_SOURCE_ID = "expansion:google-news"
YOUTUBE_SEARCH_SOURCE_ID = "expansion:youtube"

_YOUTUBE_SEARCH_YDL_OPTS = {
    "quiet": True,
    "ignoreerrors": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "socket_timeout": 12,
    "extractor_args": {"youtubetab": {"approximate_date": ["true"]}},
}


def ensure_expansion_sources(conn: sqlite3.Connection) -> None:
    """Two permanent singleton source rows -- one per expansion channel
    -- so raw_signals.source_id's foreign key is always satisfiable.
    Same idiom as manual.py's ensure_manual_source()."""
    for source_id, name, lane, platform in (
        (GOOGLE_NEWS_SOURCE_ID, "Claude-directed Google News search", "publisher", "google-news"),
        (YOUTUBE_SEARCH_SOURCE_ID, "Claude-directed YouTube search", "creator", "youtube"),
    ):
        existing = conn.execute("SELECT source_id FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if existing:
            continue
        conn.execute(
            """INSERT INTO sources (
                source_id, name, lane, platform, canonical_identifier,
                collection_method, collection_interval_hours, reliability,
                source_role, active, known_restrictions, engagement_available,
                access_status, config_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source_id, name, lane, platform, "dynamic-query",
                "claude_expansion", 0, None, "signal", 1,
                "Query-driven, not a fixed feed/channel.", 0, "ok", "{}",
            ),
        )
    conn.commit()


def create_expansion_run(conn: sqlite3.Connection, now: str | None = None) -> str:
    now = now or utc_now_iso()
    run_id = f"expand:{uuid.uuid4()}"
    conn.execute(
        """INSERT INTO collection_runs (
            run_id, started_at, finished_at, lanes, status,
            sources_attempted, sources_succeeded
        ) VALUES (?,?,?,?,?,?,?)""",
        (run_id, now, now, "publisher,creator", "completed", 1, 1),
    )
    conn.commit()
    return run_id


def _google_news_search(query: str) -> list[RawSignalInput]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-AU&gl=AU&ceid=AU:en"
    text = scanner.http_get_text(url, timeout=15)
    items = scanner.rss_items_from_xml(text, "Google News search (Claude expansion)", "google-news")
    return [
        RawSignalInput(
            source_id=GOOGLE_NEWS_SOURCE_ID,
            lane="publisher",
            platform="google-news",
            source_name="Google News search (Claude expansion)",
            source_role="signal",
            url=item.url,
            title=item.title,
            body_text=item.summary or "",
            author_or_account=item.source,
            published_at=item.published,
        )
        for item in items
    ]


def _youtube_search(query: str) -> list[RawSignalInput]:
    with yt_dlp.YoutubeDL(_YOUTUBE_SEARCH_YDL_OPTS) as ydl:
        info = ydl.extract_info(f"ytsearch{YOUTUBE_SEARCH_RESULTS}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    signals = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        published_at, precision = extract_published_at(entry)
        signals.append(
            RawSignalInput(
                source_id=YOUTUBE_SEARCH_SOURCE_ID,
                lane="creator",
                platform="youtube",
                source_name="YouTube search (Claude expansion)",
                source_role="signal",
                url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
                title=entry.get("title"),
                body_text="",
                author_or_account=entry.get("uploader") or entry.get("channel"),
                published_at=published_at,
                timestamp_precision=precision,
            )
        )
    return signals


def _execute_query(platform: str, query: str) -> list[RawSignalInput]:
    """Dispatches by platform, resolving the executor functions by
    module-level name at call time (not a dict built at import time) so
    that patching trend_radar.claude_expansion._google_news_search /
    _youtube_search in tests actually takes effect."""
    if platform == "google-news":
        return _google_news_search(query)
    if platform == "youtube":
        return _youtube_search(query)
    raise ValueError(f"unknown expansion platform: {platform!r}")


def _select_queries(waves: list[DiscoveryWave], budget: int) -> list[tuple[str, ExpansionQueryProposal]]:
    """Round-robin across waves (in the order Claude returned them) so
    one wave's two proposed queries can't consume the whole budget
    before a later wave gets a turn. Returns (wave_id, proposal) pairs,
    capped at `budget` total."""
    selected: list[tuple[str, ExpansionQueryProposal]] = []
    max_per_wave = max((len(w.expansion_queries) for w in waves), default=0)
    for slot in range(max_per_wave):
        for wave in waves:
            if len(selected) >= budget:
                return selected
            if slot < len(wave.expansion_queries):
                selected.append((wave.wave_id, wave.expansion_queries[slot]))
    return selected


def run_expansion(
    conn: sqlite3.Connection,
    waves: list[DiscoveryWave],
    budget: int = MAX_TOTAL_SEARCHES,
) -> tuple[dict[str, list[EvidenceRecord]], list[dict]]:
    """Executes up to `budget` of Claude's proposed expansion queries.
    Returns (per-wave new EvidenceRecords, an executed-query log for the
    completion report). Every query is attempted independently -- one
    failure is recorded and skipped, never raised."""
    ensure_expansion_sources(conn)
    run_id = create_expansion_run(conn)
    now = utc_now_iso()

    results: dict[str, list[EvidenceRecord]] = {}
    log: list[dict] = []

    for wave_id, proposal in _select_queries(waves, budget):
        entry = {"wave_id": wave_id, "platform": proposal.platform, "query": proposal.query, "rationale": proposal.rationale}
        try:
            raw_items = _execute_query(proposal.platform, proposal.query)
        except Exception as exc:  # noqa: BLE001 - deliberate: isolate one query's failure from the rest
            entry["status"] = "failed"
            entry["error"] = str(exc)[:500]
            log.append(entry)
            continue

        new_signal_ids = []
        for item in raw_items:
            signal_id, is_new = upsert_signal(conn, item, run_id=run_id, now=now)
            if is_new:
                new_signal_ids.append(signal_id)
        conn.commit()

        entry["status"] = "success"
        entry["result_count"] = len(raw_items)
        entry["new_signal_count"] = len(new_signal_ids)
        log.append(entry)

        wave_records = results.setdefault(wave_id, [])
        for signal_id in new_signal_ids:
            record = record_for_signal_id(conn, signal_id)
            if record is not None:
                wave_records.append(record)

    for source_id in (GOOGLE_NEWS_SOURCE_ID, YOUTUBE_SEARCH_SOURCE_ID):
        update_source_status(conn, source_id, "ok", now)

    return results, log
