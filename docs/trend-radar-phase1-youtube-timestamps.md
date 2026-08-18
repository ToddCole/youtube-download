# Trend Radar — Phase 1 YouTube timestamp spike

Follow-up to the Phase 1 hardening pass. Investigates whether yt-dlp's
`youtubetab:approximate_date` extractor arg can raise YouTube creator-lane
`published_at` coverage above the previously-reported 0% (0/112), before
considering the heavier fallback of per-channel Atom feeds or full
per-video extraction.

## 1. Spike: `youtubetab:approximate_date` against 3 representative channels

Read-only, not committed to the repo (script lived in the session
scratchpad). Same flat-playlist shape as production
(`extract_flat="in_playlist"`, 8 uploads/channel), with
`extractor_args={"youtubetab": {"approximate_date": ["true"]}}` added.
yt-dlp version installed: `2026.03.17`.

| Channel | Videos returned | With `timestamp` | With `upload_date` | Usable % |
|---|---|---|---|---|
| `@JeffNippard` | 8 | 8 | 0 | 100.0% |
| `@athleanx` | 8 | 8 | 0 | 100.0% |
| `@CrossFitGamesTV` | 8 | 8 | 0 | 100.0% |

Request count: 1 per channel (no extra network round-trips — the arg
only changes how already-fetched tab-page data is parsed). Elapsed:
0.70–0.94s per channel, indistinguishable from the baseline (no-arg)
call.

Raw examples (all 24 returned entries, not just these 4):
```json
{"id": "TBrlPpviaqY", "timestamp": 1785196800, "upload_date": null}
{"id": "-dhegeVKdgE", "timestamp": 1786924800, "upload_date": null}
{"id": "NhUYaY5ZlZc", "timestamp": 1786752000, "upload_date": null}
```

**Exact or approximate?** Every one of the 24 populated `timestamp`
values in the sample landed exactly on a UTC day boundary
(`value % 86400 == 0`) — the signature of a date with no time-of-day
component. Confirmed authoritatively by reading the installed yt-dlp's
own extractor source
(`yt_dlp/extractor/youtube/_tab.py`, `_rich_item_renderer`/
`_extract_lockup_view_model`): with `approximate_date` on, `timestamp`
is set to `self._parse_time_text(time_text)` — parsing YouTube's
*displayed relative-age label* ("3 weeks ago") into a calendar date.
This is categorically an approximation, never a real publish moment,
regardless of channel or sample size. `release_timestamp` is a
different field entirely (`scheduled_timestamp`, a real premiere/
scheduled-release time set by the uploader) and is unaffected by this
arg — it is exact when present, but wasn't present in this non-premiere
sample.

## 2. Integration

Coverage was high enough (100% on the 3-channel sample) to integrate,
so the §3 Atom-feed fallback spike was not run — evaluated and skipped
per the conditional in the request, not omitted by oversight.

Changes (`trend_radar/collectors/youtube.py`):
- `_YDL_OPTS` now sets `extractor_args: {"youtubetab": {"approximate_date": ["true"]}}`.
- `extract_published_at(entry)` now returns `(published_at, timestamp_precision)`,
  checking fields in the order **`release_timestamp` → `timestamp` → `upload_date`**
  (exact field checked first, so a premiered video with both fields populated
  gets the exact value, not whichever field the old unordered loop hit first):
  - `release_timestamp` present and parseable → `"exact"`
  - `timestamp` present and parseable → `"approximate"` (relative-label derived)
  - `upload_date` present and parseable → `"approximate"` (date-only, no time-of-day)
  - nothing usable → `(None, "unknown")`, `published_at` stays null
- `raw_signals.timestamp_precision` (new column, additive migration via
  `ensure_column`) persists this alongside `published_at`.
- The full raw yt-dlp entry dict is preserved as JSON in the dedicated
  `raw_signals.raw_payload_json` column (`json.dumps(entry, default=str)`).
  An earlier draft of this change stored it in `raw_payload_ref` instead;
  that was corrected during review, since that column is reserved for a
  genuine external file/object/blob reference, never inline JSON.
  `raw_payload_json` (new column, additive migration via `ensure_column`)
  exists specifically for this. See the Phase 1 commit's raw-payload
  schema fix for the one-time migration that moved any rows already
  written under the old, incorrect column.
- Fixture tests added/updated in `test_trend_radar.py`
  (`YouTubeTimestampExtractionTests`, `YouTubeCollectionTests`) covering:
  approximate-only timestamp, exact release_timestamp preferred over a
  simultaneously-present approximate timestamp, upload_date-only,
  missing metadata, and malformed values in each field falling through
  without raising.

**Live coverage after integration**, run through the actual
`fetch_youtube_channel_source()` / `run_collection()` path (not the
spike script) against all 15 configured creator sources, against an
explicit temporary database:

- 15 sources attempted, 14 succeeded, 1 failed (`creator:hyroxworld`,
  the pre-existing no-videos-tab channel, isolated correctly — did not
  abort the run)
- **112/112 creator signals now have `published_at` = 100.0%**
  (previously 0/112 = 0.0%)
- Precision breakdown: **112 `approximate`, 0 `exact`, 0 `unknown`** —
  expected, since none of the sampled uploads were scheduled premieres.

## 3. Fallback spike (not run)

Not needed — §2's integrated coverage (100%) made the Atom-feed
(`youtube.com/feeds/videos.xml?channel_id=...`) and full-per-video-extraction
fallbacks unnecessary. Per the request, full per-video extraction is not
implemented without a separate review regardless; if pursued later, it
must hydrate only previously-unseen video IDs, never every video on every
run.

## 4. Phase 2 trend-safety rules (documented only, not implemented)

**A creator signal with `published_at` null must not contribute to
publication-velocity or recency scoring.** It may still be stored as a
raw signal and become usable later once other evidence or observation
history is available (e.g. its `first_seen_at`/observation cadence can
support relative ordering even without a trusted absolute publish time).

This is now largely moot for the *creator* lane specifically — coverage
is 100% approximate rather than 0% null — but the rule stands as
written and generalizes to any future signal (any lane, any platform)
that legitimately has no trustworthy `published_at`.

`timestamp_precision` exists precisely so Phase 2 can apply that
distinction correctly rather than treating every non-null `published_at`
as equally trustworthy. The actual rules for using it (not "sub-day" —
72 hours is 3 days, not sub-day; that framing was wrong):

- **Approximate (day-granularity) timestamps must not be used for
  hourly views-per-hour calculations.** A value with no time-of-day
  component can't support an hour-level rate.
- **They must not be treated as exact when applying a strict cutoff**
  — including the 72-hour acceleration window itself. A signal dated
  "3 days ago" by an approximate label could genuinely be anywhere
  within roughly a ±12–24h band of that boundary; a hard 72h cutoff
  applied to an approximate value can misclassify a signal that's
  actually just inside or just outside the window.
- **They may support daily or multi-day trend windows** (e.g. a 7-day
  or 28-day baseline) **when their uncertainty is explicitly
  recognised** — day-level resolution is fit for purpose at that
  granularity, unlike hourly or strict-cutoff use.
- **Exact and approximate timestamps must remain distinguishable
  throughout later phases** — never collapse `timestamp_precision`
  away or treat the two as interchangeable once `published_at` is
  read back out of storage.
- **Signals with no publication timestamp remain excluded from
  recency and velocity calculations**, per the rule above — this is
  the `"unknown"` case, not a third kind of usable precision.

Not implemented here; recorded for Phase 2 scoring/classification to apply.
