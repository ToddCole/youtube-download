# Handover: MFO Editorial Desk scanner

Last updated 9 Nov 2026. Branch: `feature/mfo-trend-radar-claude`.

## Where it stands

The five-phase scanner upgrade and the phone digest are built and pushed.
147 tests pass: from `scanner/`, run `python3 -m pytest -q test_*.py`.

Pipeline: learn -> collect -> Claude (discovery, expansion, web research,
editorial) -> SEO -> ntfy digest. launchd runs `com.mfo.editorial-cycle` every
3 hours at :05. The paid Claude scan runs at 07:05 and 16:05 only.

Last live scan: 9 Nov, about US$1.14 with 8 web searches. That is roughly
US$68 a month at two scans a day.

## Still to check

- Freshness gate: added after the last live scan, so it hasn't run live yet.
  After the 9 Nov 16:05 scan, check recirculated stories aren't picked as new.
- Phone digest: the step reported sent, but arrival on the phone isn't confirmed.
- Tate-Miles Masters Games results: last scan held it because web research
  only found a headline, with no figures.

## On the other Mac

- `git pull`
- `python3 -m pip install -U yt-dlp` (2026.10.26 fixed the YouTube 403s)
- `scanner/open_stories.json` is gitignored and rebuilt before each Claude scan.

## Rules

- This repo is public. Keep keys and incident details out of it.
- Check the API key only with:
  `[ -n "$ANTHROPIC_API_KEY" ] && echo "SET (length: ${#ANTHROPIC_API_KEY})" || echo "NOT SET"`
  Never dump the environment.
- Prompts stay free of specific people and stories (a test enforces this).
  Standing topics belong in `scanner/beats.json`.
- Start the Desk with a full PATH, or ffmpeg won't be found.
- Push only when asked.
