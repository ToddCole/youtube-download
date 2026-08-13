# YouTube Discovery Scanner

This folder contains a standalone scanner for the existing YT Downloader project. It reuses the project `yt-dlp` dependency and does not add a dashboard or change the downloader/MFO Pack interface.

## Setup

Install the parent project dependencies if needed:

```bash
cd /Users/toddcole/Projects/apps/youtube-download
python3 -m pip install -r requirements.txt
```

Run the scanner manually from this folder:

```bash
cd /Users/toddcole/Projects/apps/youtube-download/scanner
python3 scanner.py
```

When run from Terminal, generated Markdown reports open in Microsoft Word automatically. To suppress that:

```bash
python3 scanner.py --no-open-reports
```

To force Word opening from a non-interactive run:

```bash
python3 scanner.py --open-reports
```

## Channel Configuration

Edit `channels.json` as a simple JSON array of 15-25 YouTube channel URLs or handles:

```json
[
  "https://www.youtube.com/@WillTennyson/videos",
  "https://www.youtube.com/@RenaissancePeriodization/videos"
]
```

The scanner reads each configured channel and collects the five newest videos per scan.

`editorial_sources.json` adds editable MFO-specific notes for each source: category, likely fit, suggested angle, original value to add and common weakness.

## MFO Archive Index

The scanner can read the public MFO site to avoid duplicate or cannibalising pitches. It is read-only and uses the public WordPress REST API first, then the public sitemap if the API is unavailable.

Refresh the local cache:

```bash
python3 scanner.py --refresh-mfo-index
```

The cache is saved as `mfo_index.json`. Normal scans refresh it automatically when it is missing or older than 24 hours. Use `--skip-mfo-index` if the site is unavailable and you still want a YouTube-only scan.

## Output

Each run appends Creator Radar observations to `scanner.db` and writes a readable editorial lead sheet to `reports/latest.md`.
It also writes normalised JSON for the web Editorial Desk:

- `reports/latest.json`
- `reports/news-latest.json`
- `reports/research-latest.json`

The report ranks standard videos and Shorts separately. The first scan uses total views per hour and labels comparative growth as `baseline pending`. Later scans rank primarily by views gained since the previous scan and include observed hourly growth plus breakout score against that channel's accumulated baseline.

Each candidate includes source timing, traction, average views per hour, relative performance, MFO fit, proposed original value, weakness and any likely overlap with existing MFO pages.

Creator Radar separates:

- `New Leads`
- `Possible Update Or Follow-Up Opportunities`
- `Already Covered And Excluded`

If an exact YouTube video ID or source URL already appears in the MFO archive, the candidate is excluded from new leads.

The scanner now stores stable source fingerprints for archive checks and packet deduplication. Creator leads use YouTube video IDs, Research leads use PMID/DOI/PubMed/DOI URLs, and News/Manual leads use canonical source URLs with common redirect/tracking parameters removed. Exact source duplicates are excluded before editorial ranking; generic word overlap such as “review”, “strength” or “exercise” is ignored for archive warnings.

Growth intervals are labelled:

- under one hour: `insufficient growth interval`
- one to six hours: `preliminary growth`
- over six hours: `useful growth signal`

Sub-hour observations are stored, but not converted into an observed hourly growth ranking.

## News Radar

News Radar is separate from Creator Radar and writes `reports/news-latest.md`.

Configure free news inputs in:

- `news_sources.json`
- `news_queries.json`

News Radar looks for actual developments: announcements, results, records, documentaries, injuries, retirements, launches, recalls, regulation and credible controversies. It clusters multiple links about the same development, discounts syndicated press-release copies as independent pickup, checks MFO archive overlap and scores each story out of 100. Journal articles are separated into Research Radar rather than ranked as ordinary breaking news.

ScienceDaily Fitness, Nutrition, Sports Medicine, Diet and Weight Loss, and Dietary Supplements feeds are included as `research_media` sources. These are treated as public-interest alerts, not primary evidence. When a ScienceDaily item can be matched to a PubMed paper by DOI, PMID, exact title or strong title similarity, Research Radar keeps the paper as the primary source and records ScienceDaily as a public-interest signal.

News payloads separate audience momentum from `editorial_opportunity_score`. The score breakdown includes freshness, MFO fit, story angle, practical usefulness, evidence quality, Australian relevance, archive risk and estimated production effort.

## Research Radar

Research Radar is a separate lane for PubMed papers and writes:

- `reports/research-latest.md`
- `reports/research-latest.json`

Configure topic groups, thresholds and penalties in `research_queries.json`. It searches a rolling seven-day PubMed publication window with overlap, stores seen PMIDs/DOIs in `scanner.db`, and keeps the previous successful report if PubMed or enrichment requests fail.

The research configuration includes a strength-and-conditioning topic group and optional journal relevance boosts for highly relevant sports-science journals. These boosts affect MFO audience relevance only; they do not override evidence quality, study design, sample size, limitations or archive overlap.

Research extraction is conservative. If sample, population, intervention, comparison, duration or effect size cannot be identified reliably from the abstract, the JSON field is `null` and `extraction_warnings` explains what was left unknown. Research scoring distinguishes practical fitness findings from specialist clinical procedures, exploratory secondary analyses, protocols, animal/laboratory research, healthcare audits and academic noise.

Run Research Radar alone:

```bash
python3 scanner.py --skip-creator --skip-news --no-open-reports
```

Run Creator, News and Research together:

```bash
python3 scanner.py --no-open-reports
```

Optional environment variables:

- `NCBI_API_KEY`: raises the NCBI rate limit from 3 to 10 requests per second.
- `NCBI_EMAIL` or `CONTACT_EMAIL`: included as the NCBI E-utilities contact email.

The scanner never hard-codes private credentials. Missing research fields are written as `null`, empty lists or `not reported in abstract`.

## Editorial Desk

Start the existing local downloader app from the parent folder:

```bash
cd /Users/toddcole/Projects/apps/youtube-download
python3 main.py
```

Open `http://127.0.0.1:8090` and use the Editorial Desk section below the downloader. It can start Creator Radar, News Radar, Research Radar or all three without using Terminal, then reads the JSON reports above.

Phase 1 uses a manual agent adapter. Click `Prepare Agent Review`, copy or download the packet, send it to ChatGPT, Codex or another capable agent, then paste the returned JSON into `Import Agent Review`. The packet includes the top 10 Creator, top 10 News and top 10 Research candidates, plus any manual stories you add in the UI. The permanent supervisor prompt is stored in `editorial_supervisor_prompt.md`; the required response shape is documented in `editorial_supervisor_response.schema.json`.

Imported reviews render a recommended slate at the top, but all supplied candidates remain visible under Creator, News, Research and Manual tabs. The supervisor response must assess every supplied candidate as `Strong`, `Possible` or `Weak`, with an MFO angle, evidence risk and archive-overlap warning. Local `Commission`, `Hold` and `Reject` decisions are saved in `scanner.db` and survive refreshes and app restarts.

Review packets deduplicate candidates across Creator, News, Research and Manual streams using source fingerprints. Manual leads win over scanner leads, primary research/official sources win over secondary reporting, and merged duplicates remain attached as supporting sources. The packet also includes a concise `daily_editorial` section with up to three `commission_now` and three `hold_for_follow_up` candidates; it does not fill these slots with weak material.

Commissioning is an editorial decision only. Commissioned stories move into the local Production Queue, where an editor can:

- prepare a writing packet for a manual writing agent
- import a completed article JSON payload
- create a WordPress draft
- open the created draft in WordPress

The article import must include `headline`, `slug`, `excerpt`, `article_html`, `seo_title`, `meta_description`, `focus_keyphrase`, `tags`, `source_attribution`, `facts_checked`, `risks_disclosures`, `internal_links` and `embed_media_notes`. Missing fields or invalid JSON block the import.

WordPress draft creation is draft-only and requires local environment variables:

```bash
export MFO_WP_BASE_URL="https://mensfitnessonline.com.au"
export MFO_WP_USERNAME="your-wordpress-username"
export MFO_WP_APP_PASSWORD="your-wordpress-application-password"
```

Draft creation uses the WordPress REST API with Application Password authentication. Tags and the suggested category are resolved or created before the post is created. Yoast title, meta description and focus keyphrase are sent only when WordPress exposes the relevant registered REST meta fields; otherwise the queue marks them as manual copy required. The app never publishes automatically.

## Scheduling Twice Daily On macOS

The included `com.mfo.youtube-scanner.plist` runs at 8:00 and 20:00 local time.
It uses `python3 scanner.py` through your shell, matching the manual command above.

```bash
mkdir -p ~/Library/LaunchAgents
cp /Users/toddcole/Projects/apps/youtube-download/scanner/com.mfo.youtube-scanner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mfo.youtube-scanner.plist
```

Run once immediately:

```bash
launchctl start com.mfo.youtube-scanner
```

Disable the schedule:

```bash
launchctl unload ~/Library/LaunchAgents/com.mfo.youtube-scanner.plist
```

Logs are written to `/tmp/com.mfo.youtube-scanner.out.log` and `/tmp/com.mfo.youtube-scanner.err.log`.

## Growth Test

To prove view growth calculation without waiting for public counts to change:

```bash
python3 scanner.py --fixture-growth-test --db /tmp/youtube-scanner-fixture.db --report /tmp/youtube-scanner-fixture.md
```

Run fixture assertions for the current editorial rules:

```bash
python3 scanner.py --fixture-tests --skip-mfo-index
```

The fixture tests cover exact archive-source matches, cross-stream deduplication, CrossFit result clustering, generic-overlap false positives, conservative research extraction and editorial score ordering. For a syntax and unit-test pass:

```bash
PYTHONPYCACHEPREFIX=/tmp/mfo-pycache python3 -m py_compile scanner.py ../main.py test_editorial.py
PYTHONPYCACHEPREFIX=/tmp/mfo-pycache python3 -m unittest test_editorial.py
```
