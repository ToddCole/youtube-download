"""Lane 3: latest uploads from the curated YouTube channel list. Phase 1
scope only -- fixed-channel polling, not seed-triggered search (that is
Phase 2's `ytsearch:` expansion step, deliberately not built here).

Reuses the same flat-extraction two-pass shape scanner.py's
fetch_channel_videos() uses (extract_flat="in_playlist"), via the same
yt-dlp dependency already in requirements.txt -- no new dependency.

A channel with no accessible videos tab (confirmed real during Phase 0:
@hyroxworld) raises, which collect_from_source() isolates as a normal
per-source error -- never a fatal run failure.

Publication timestamps and precision: the `youtubetab:approximate_date`
extractor arg is enabled below. Verified against yt-dlp 2026.03.17's own
extractor source (extractor/youtube/_tab.py): with this arg on, the flat
`entries[].timestamp` field is populated via `_parse_time_text(time_text)`
-- parsing YouTube's displayed relative label ("3 weeks ago") into a
calendar date -- categorically an approximation, never a real publish
moment (confirmed empirically too: every populated `timestamp` value in
a live sample landed exactly on a UTC day boundary, the signature of a
label-derived date with no time-of-day component). `entries[].upload_date`
is not populated by this extractor path either way and is handled here
only as a defensive fallback -- if a future yt-dlp version starts
supplying it in flat mode, it is date-only (YYYYMMDD) and equally lacks
time-of-day precision, so it is also always "approximate", never "exact".

`entries[].release_timestamp` is different: it carries `scheduled_timestamp`,
a real premiere/scheduled-release moment set by the uploader, independent
of `approximate_date` -- this is the one field that can be genuinely
"exact". It is checked first for that reason: if both it and `timestamp`
are present, the exact value must win, not whichever field the loop
happens to see first.

published_at is honestly null whenever none of these fields yield a
parseable value -- never substituted with first_seen_at as though it
were the real publish time. Malformed values in one field never abort
collection; extraction just falls through to the next field.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import yt_dlp

from ..models import RawSignalInput

UPLOADS_PER_CHANNEL = 8

_YDL_OPTS = {
    "quiet": True,
    "ignoreerrors": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "playlistend": UPLOADS_PER_CHANNEL,
    "socket_timeout": 12,
    # Turns on entries[].timestamp, populated from YouTube's displayed
    # relative-age label -- day-granularity only, always "approximate"
    # precision below, never "exact". See module docstring.
    "extractor_args": {"youtubetab": {"approximate_date": ["true"]}},
}


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def extract_published_at(entry: dict) -> tuple[str | None, str | None]:
    """Best-effort real publication timestamp from yt-dlp entry metadata.

    Returns (published_at, timestamp_precision):
      - (iso_string, "exact") from release_timestamp (a real scheduled/
        premiere moment, not derived from a relative-age label)
      - (iso_string, "approximate") from timestamp (approximate_date's
        relative-label parse) or upload_date (date-only, no time-of-day)
      - (None, "unknown") if nothing trustworthy is present or parseable

    Never returns a substitute value -- callers must never treat the
    absence of a real timestamp as though first_seen_at were one.
    """
    raw = entry.get("release_timestamp")
    if raw is not None:
        try:
            return _iso_utc(datetime.fromtimestamp(float(raw), tz=timezone.utc)), "exact"
        except (TypeError, ValueError, OSError, OverflowError):
            pass  # malformed; fall through to the approximate fields

    raw = entry.get("timestamp")
    if raw is not None:
        try:
            return _iso_utc(datetime.fromtimestamp(float(raw), tz=timezone.utc)), "approximate"
        except (TypeError, ValueError, OSError, OverflowError):
            pass

    upload_date = entry.get("upload_date")
    if upload_date:
        try:
            parsed = datetime.strptime(str(upload_date), "%Y%m%d").replace(tzinfo=timezone.utc)
            return _iso_utc(parsed), "approximate"
        except (ValueError, TypeError):
            pass

    return None, "unknown"


def fetch_youtube_channel_source(source_row: sqlite3.Row) -> list[RawSignalInput]:
    channel_url = source_row["canonical_identifier"]
    with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    if info is None:
        raise RuntimeError(f"channel has no accessible videos tab: {channel_url}")
    entries = info.get("entries") or []

    signals = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        engagement = {}
        if entry.get("view_count") is not None:
            engagement["view_count"] = entry.get("view_count")
        published_at, precision = extract_published_at(entry)
        try:
            raw_payload_json = json.dumps(entry, default=str)
        except TypeError:
            raw_payload_json = None
        signals.append(
            RawSignalInput(
                source_id=source_row["source_id"],
                lane=source_row["lane"],
                platform="youtube",
                source_name=source_row["name"],
                source_role=source_row["source_role"],
                url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
                title=entry.get("title"),
                body_text="",
                author_or_account=entry.get("uploader") or entry.get("channel"),
                published_at=published_at,
                timestamp_precision=precision,
                engagement=engagement,
                raw_payload_json=raw_payload_json,
            )
        )
    return signals
