# Handover: MFO Editorial Desk scanner

Last updated 14 Nov 2026. Branch: `feature/mfo-trend-radar-claude`.

**Status: paid Claude scans OFF since 14 Nov 2026** (`MFO_CLAUDE_SCAN=0`
in `.env`; set it to `1` to resume). The value trial ended after two days:
$3.50 spent for one possible update and no new stories, while every clear
win came from free Google data. The desk now runs on free sources only:

- **Every cycle:** `trending.py` checks Google Trends AU (RSS), Wikipedia
  AU page-view spikes and the Netflix AU Top 10, and keeps an item only when
  it matches an MFO page or topic. Board and digest pool the last 24h.
- **07:05 cycle:** `seo_opportunity.py --free` (rising searches + Google
  refreshes) and `digest.py --free`. Free days use stand-in scan ids
  `9YYYYMMDD` (`evergreen.free_scan()`), so votes still work.
- The "Scan now" button on the desk still runs a **paid** scan.

The trial history below is kept for reference.

## Where it stands

198 tests pass: from `scanner/`, run `python3 -m pytest -q`.

Pipeline: learn -> collect -> Claude scan -> SEO -> digest (phone via ntfy
and email). launchd runs `com.mfo.editorial-cycle` every 3 hours at :05.
The paid Claude scan runs **once a day at 07:05** (`CLAUDE_HOURS`). Daily cap:
`MFO_CLAUDE_DAILY_CAP_USD` in `.env`.

What each scan produces:
- **News picks**: discovery, expansion, web research and the editorial pass.
- **Fit food**: a pass on the editor's brief (`fit_food_brief.md`, settings in
  `fit_food.json`) with its own 15 food, supplement and nutrition-research
  sources. Once a day. The web-only comparison search is off (`compare`).
- **Evergreen**: at least 2 refreshes of MFO pages ranking 5–20 in Google
  (free GSC data), plus new angles on quiet days. Translated pages are skipped.
- **Rising in Google**: up to 3 pages with new search impressions this week.

The Today board at localhost:8090 shows all four.

## Changed 13 Nov (check these during the trial)

- **Repeat filter** (`trend_radar/sent_log.py`): everything the digest sends
  is logged. For 7 days the same story is left out of the digest, and both
  scans see the list so a story only returns with a real new development.
- **Web fetch**: research can open up to 6 pages per pass
  (`MFO_WEB_FETCH_MAX`, `max_fetches` in `fit_food.json`). It works on news
  sites. TGA and Nature block it, so regulator notices and papers still show
  "not opened". A pick is only marked opened if a fetch succeeded.
- **MFO index**: the site retired 236 posts on 12 Nov (now 482). The index
  refresh refuses a drop of more than 10%; the new count was accepted by hand.
  If it trips again after a deliberate cleanup, re-run the refresh with
  `MFO_INDEX_MIN_KEEP_RATIO = 0` (see `scanner.py`).

Expected cost: about US$1.30–1.60 a day.

## Review on 20 Nov

Compare `sent_items` and `claude_scans` in `trend_radar.db` with what MFO
published (`mfo_index.json`) and the 👍/👎 votes (`pick_feedback`). Count
the days with at least one publishable story, then make the keep-or-kill call.

## On the other Mac

- `git pull`
- `python3 -m pip install -U yt-dlp` (2026.10.26 fixed the YouTube 403s)
- `scanner/open_stories.json` is gitignored and rebuilt before each Claude scan.
- `.env` needs the email settings (`MFO_EMAIL_TO`, `SMTP_PASSWORD`) for the
  email digest.

## Rules

- This repo is public. Keep keys and incident details out of it.
- Check the API key only with:
  `[ -n "$ANTHROPIC_API_KEY" ] && echo "SET (length: ${#ANTHROPIC_API_KEY})" || echo "NOT SET"`
  Never dump the environment.
- Prompts stay free of specific people and stories (a test enforces this).
  Standing topics belong in `scanner/beats.json`.
- Start the Desk with a full PATH, or ffmpeg won't be found.
- Push only when asked.
