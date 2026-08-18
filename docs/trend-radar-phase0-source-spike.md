# Trend Radar — Phase 0 Source-Access & Discovery Spike

Read-only spike. No collectors, no database tables, no scanner.py or dashboard changes,
no dependencies installed, no scheduling, no LLM integration, no credentials registered.
All commands were one-off `curl`/`yt-dlp` calls run interactively; nothing here is
persisted as code. Findings below are what license Phase 1, not Phase 1 itself.

Date of spike: 2026-08-18 (Australian time, per session context). All "current" results
below reflect real, live data retrieved on that date — not fixtures, not hypotheticals.

---

## 1–4. Exact sources tested, access results, evidence retrieved, discovery contribution

### Lane 1 — Fitness publishers (RSS)

| Source | URL tested | Access | Fields retrievable | Rate/access notes | Discovery contribution |
|---|---|---|---|---|---|
| Muscle & Fitness | `muscleandfitness.com/feed/` | OK (200, valid RSS) | title, link, pubDate, description, categories | None observed | Continuous monitoring |
| Muscle & Strength | `muscleandstrength.com/rss.xml` | OK | same | None observed | Continuous monitoring |
| Fit&Well | `fitandwell.com/feeds.xml` | OK | same | None observed | Continuous monitoring |
| Men's Health UK | `menshealth.com/uk/rss/all.xml/` | OK | same | None observed | Continuous monitoring |
| Men's Health US | `menshealth.com/rss/all.xml/` | OK | same | None observed | Continuous monitoring |
| Men's Journal | `mensjournal.com/feed/` | OK | same | None observed | **Continuous monitoring — this feed is the outlet that broke the Tom Holland/Cindy story (30 Jul 2026); see §7** |
| Breaking Muscle | `breakingmuscle.com/feed/` | OK | same | None observed | Continuous monitoring |
| BoxRox | `boxrox.com/category/news/feed/` (bare `/feed/` returns HTML, not a working feed) | OK, path-dependent | same | Requires the category-scoped path, not the bare root feed | Continuous monitoring |
| Runner's World | `runnersworld.com/rss/all.xml/` | OK | same | None observed | Continuous monitoring |
| Bicycling | `bicycling.com/rss/all.xml/` | OK | same | None observed | Adjacent-audience monitoring |
| Precision Nutrition | `precisionnutrition.com/feed` | OK | same | None observed | Continuous monitoring |
| Outside Online | `outsideonline.com/feed` | OK | same | None observed | Continuous monitoring |
| SimpliFaster | `simplifaster.com/feed/` | OK | same | None observed | Continuous monitoring |
| Barbell Medicine | `barbellmedicine.com/feed/` | OK | same | None observed | Continuous monitoring, verification-leaning (practitioner-authored) |
| Stronger by Science | `strongerbyscience.com/feed/` | OK | same | None observed | Continuous monitoring, verification-leaning |
| BarBend | `barbend.com/feed/` | **FAIL** — returns the site's HTML shell, not XML, at every path tried | — | Feed appears disabled or moved; needs manual `<link rel="alternate">` discovery from a live page, not pattern-guessing | Deferred |
| T Nation | `t-nation.com/feed/` | **FAIL** (404); `sitemap.xml` resolves but is not an item feed | — | Needs manual discovery | Deferred |
| Stack | `stack.com/feed/` | **FAIL** (connection timeout / unreachable) | — | Possibly blocked or DNS issue from this environment | Deferred |
| Livestrong | `livestrong.com/feed/` | **FAIL** | — | — | Deferred |
| Verywell Fit | `verywellfit.com/feed` | **FAIL** | — | — | Deferred |
| Men's Health Australia | `menshealth.com.au/feed` | **FAIL** | — | AU edition likely syndicates from the UK/US feed rather than running its own; needs manual discovery | Deferred |
| Coach AU (Nine) | `coach.nine.com.au/rss` | **FAIL** | — | — | Deferred |
| Australian Fitness Network | guessed `/feed` path | **FAIL** | — | — | Deferred |
| Athletics Australia | guessed `/feed/` path | **FAIL** | — | — | Deferred |

**Google News RSS search (already proven, reused unchanged from `scanner.py`)** was also tested
directly as a Lane 1 *keyword-search* mechanism, not just fixed-feed monitoring — see §7, this is
the single most important finding of the spike.

### Lane 2 — Reddit

| Method tested | Access | Notes |
|---|---|---|
| `reddit.com/r/{sub}/.rss` (per-subreddit feed, 8 subs tried) | **Inconsistent** — `r/Fitness` returned 200 on first try; `r/crossfit`, `r/bodyweightfitness`, `r/naturalbodybuilding`, `r/weightroom`, `r/fitness30plus`, `r/fitnessover50`, `r/workout` all failed on the same pass | Failure pattern is consistent with rate-limiting after the first request, not per-subreddit unavailability |
| `reddit.com/r/{sub}/hot.json` | **403 Forbidden** | Anonymous JSON endpoints are blocked outright |
| `reddit.com/search.json?q=...` | **403 Forbidden** | Same |
| `reddit.com/search.rss?q=...` (global, unscoped) | **200 OK**, 7 entries returned | Results were dominated by general Tom Holland movie/fandom discussion — **not** fitness-specific. Global unscoped search is low-precision for this use case. |
| `reddit.com/r/crossfit/search.rss?q=cindy&restrict_sr=on` (subreddit-scoped) | **200 OK on first attempt**, 5 entries returned | This is the actually-useful pattern — scoped to a fitness subreddit, high relevance expected |
| Same pattern repeated for `r/Fitness`, `r/bodyweightfitness`, and a second query (`tom holland`) across the same three subs, run within ~15 seconds of the prior request | **All 429 Too Many Requests** | Confirms aggressive rate-limiting on anonymous access — one scoped search succeeded, the next five consecutive requests were all blocked |

**Conclusion, stated precisely per the authorization boundary:** Reddit's anonymous public
endpoints are real and do return genuinely useful, subreddit-scoped, participation-relevant data
(the one successful `r/crossfit` "cindy" search is real evidence Lane 2 content exists and is
reachable) — but access is not reliable enough for a production collector at anonymous-request
volume. **Reliable Lane 2 access requires Reddit's official OAuth API, which requires registering
a script-app (client ID + client secret) under a Reddit account.** I did not attempt to create an
account or register an app — that is an explicit external prerequisite for you to complete before
Phase 1's Reddit collector can be built against anything more than occasional, unreliable,
anonymous single requests.

### Lane 3 — Creators / YouTube (via yt-dlp, already a repo dependency)

| Method tested | Access | Evidence retrieved | Discovery contribution |
|---|---|---|---|
| `ytsearch8:Tom Holland Cindy workout` via `yt_dlp.YoutubeDL` with `extract_flat="in_playlist"` (same pattern as `fetch_channel_videos`) | **OK**, 8 results | Titles included **"Tom Holland's CrossFit Cindy Workout (Beginner to Advanced)"** and **"Why I Recommend the CrossFit Cindy Workout for Busy Men"** (uploader: Lee Downing Keat), plus **"Just 3 Exercises & 20 Minutes! 'Cindy' Calisthenics Method for a Tom Holland-Style Spider-..."** (SB30Health), a Renaissance Periodization critique video, and a Graham Norton Show clip | **Strong keyword-search discovery** — this was the single cleanest, most directly on-topic result set of the entire spike |

No new dependency required; this reuses the exact two-pass extraction pattern already proven in
`scanner.py`'s `fetch_channel_videos`, just pointed at a search query instead of a channel URL.

### Lane 4 — Research / official

| Source | Access | Notes |
|---|---|---|
| PubMed E-utilities | **Not re-tested — already proven in production** via the existing scanner's Research Radar | Cited, not re-verified, per spike scope |
| Sports Medicine Australia | `sma.org.au/feed/` — **OK** | Working WordPress feed |
| TGA safety alerts | guessed paths — **FAIL** | Government sites generally don't expose predictable WordPress-style feed paths; needs manual per-source discovery |
| Product Safety Australia recalls | guessed paths — **FAIL** | Same |
| ACSM | guessed paths — **FAIL** | Same |
| NIH News | guessed paths — **FAIL** | Same |

**As expected and correct per the brief**: nothing in Lane 4 contributed to discovering the Tom
Holland/Cindy trend — it isn't a research topic. Lane 4's job is staying separate, which it did.

---

## 5. Proposed initial harness, based on demonstrated access only

- **Lane 1**: the ~15 confirmed-working RSS feeds above, polled continuously, **plus** Google News
  RSS search run against a dynamic query list (see §9) — not just a fixed feed list. This dual mode
  (continuous + search) is the load-bearing correction from the original registry-only design.
- **Lane 2**: deferred until OAuth app registration is complete (external prerequisite, not a Phase 1
  blocker for the other three lanes).
- **Lane 3**: both fixed-channel polling (as today) **and** `ytsearch:`-based keyword search against
  the same dynamic query list as Lane 1.
- **Lane 4**: PubMed (already working) + Sports Medicine Australia RSS; other official/regulator
  sources need individual manual feed-discovery work before inclusion, not blind path-guessing.

## 6. Sources deferred

- **BarBend, T Nation, Stack, Livestrong, Verywell Fit** — feed path not found by pattern-guessing;
  need manual `<link rel="alternate">` discovery from a live page load, or confirmation they've
  discontinued RSS.
- **Men's Health Australia, Coach AU, Australian Fitness Network, Athletics Australia** — same;
  AU-specific outlets in particular often syndicate rather than running independent feeds.
- **TGA, Product Safety Australia, ACSM, NIH** — government/institutional sites, same issue.
- **All of Lane 2 (Reddit)** — deferred pending your OAuth app registration, not a technical dead end.

None of these are ruled out — they're marked "needs individual discovery work," which is real
effort but not a blocker to starting Phase 1 with what's already confirmed working.

## 7. Would the available harness have detected Tom Holland/Cindy?

**Yes, with an important precision about which mechanism does the detecting — and this is the
correction that matters most for Phase 2.**

Real, current (as of today) Google News RSS search results for "Tom Holland Cindy workout" returned
six independent publisher pickups: Men's Journal (30 Jul 2026, the apparent origin of this wave),
Men's Health (05 Aug, 10 Aug — two separate pieces), NDTV (10 Aug), Men's Fitness (13 Aug), and The
Post PH (17 Aug, i.e. yesterday). YouTube search independently surfaced multiple directly on-topic
videos, including one literally titled "Tom Holland's CrossFit Cindy Workout." **This confirms the
trend is real, current, and richly documented across independent sources right now** — this is not
a hypothetical I constructed to pass the test; it's what these two searches actually returned.

**But** — and this is the honest caveat the authorization explicitly asked for — I ran those as
*targeted* searches, using "Tom Holland" and "Cindy" as query terms I already knew from your brief.
That tests **retrieval**, not **discovery without a predefined keyword**. The real question is: where
would that query have come from, if no human had typed it in?

Two-part answer:
1. **Continuous monitoring is what seeds discovery, not search.** Men's Journal — an obviously
   in-scope, mainstream fitness publisher any real Lane 1 registry would include — published the
   origin article on 30 Jul 2026. If Men's Journal's RSS feed (confirmed working above) is in the
   registry and polled on a ~4h cycle, **that article would have been ingested as a raw signal
   automatically, with no keyword needed.** This is the genuine "first mention" discovery path.
2. **Keyword search is what expands a seed into a cross-platform cluster, not what finds it in the
   first place.** Once deterministic phrase-mining (§9, Phase 2) extracts "Tom Holland" and "Cindy"
   as candidate phrases from that one ingested article, the harness would then need to **re-trigger**
   Google News RSS search and YouTube search using the newly-promoted phrase as the query — automatically,
   not manually — to pull in the other five publisher pickups, the YouTube videos, and (once Lane 2
   access is sorted) the Reddit discussion. That auto-triggered search step is not yet built; it's a
   concrete Phase 2 requirement, not something this spike could test in isolation.

## 8. Gaps that would still have caused the trend to be missed

- **If Men's Journal (or another outlet that happened to cover it first) isn't in the configured
  Lane 1 registry, the origin article is never ingested, phrase-mining never has anything to work
  from, and the auto-triggered search in point 2 above never fires.** The harness has a real,
  structural dependency on at least one continuous-monitoring source catching the first mention —
  this is a registry-curation risk, not a mechanism flaw, but it's real and should be named plainly
  rather than assumed away.
- **Lane 2 (Reddit) is currently inaccessible at production reliability**, so the specific
  "audience asking if it works" participation signal the brief weights most heavily (12 of 100
  scoring points, per the approved architecture) is not yet collectible at all — the one real
  `r/crossfit` "cindy" hit proves the content exists, but the harness can't reliably retrieve it
  yet, pending OAuth registration.
- **If the origin article had used different wording than "Cindy"** (e.g., only "the Spider-Man
  workout," never naming the CrossFit benchmark by its actual name), phrase-mining would still
  need to independently arrive at treating "Spider-Man workout" as a candidate — which, per the
  correction in §9, is explicitly *not* allowed to anchor a cluster on its own, being a generic
  descriptive phrase. The system would need "Tom Holland" (a genuine proper-noun anchor) to co-occur
  in the same signal to bootstrap the cluster at all. If a hypothetical origin article mentioned
  neither a real name nor "Cindy," current design would miss it entirely — worth flagging as a real
  limit of a phrase-frequency approach, not something to paper over.

---

## 9. Corrected candidate-phrase and co-occurrence design (recorded for Phase 2 — not implemented now)

This corrects the prior draft's design, which (a) wrote machine-discovered phrases directly into
the human-curated alias config, conflating two different trust levels, and (b) relied on Title-Case
detection as its primary mechanism, which fails outright on lowercase-typing platforms like Reddit
("does the cindy workout actually work" carries zero capitalization signal).

**Two distinct concepts, not one:**

1. **Human-curated aliases** — stay in `trend_entity_aliases.json`, config, human-edited only, as
   originally designed. Machine discovery never writes to this file.
2. **Machine-discovered candidate phrases** — a new database table (design only, not created this
   phase), holding: `normalised_phrase, first_seen, last_seen, occurrence_count,
   independent_source_count, lane_count, co_occurring_phrases_or_entities, examples (sample
   signal_ids/snippets), confidence, status (candidate|confirmed|rejected|merged),
   confirmation_source (system|editor)`. A phrase only becomes a **confirmed** clustering anchor
   after crossing an evidence threshold (cross-source repetition) or after explicit editor
   confirmation — never automatically promoted straight into the trusted alias config.

**Primary mechanism is lowercase, not Title-Case.** All text is lowercased before n-gram extraction
(1–4 word spans, stopwords removed); frequency counting and cross-source repetition are computed on
the normalized form. Title-Case / quoted-phrase detection survives only as a *secondary confidence
booster* for sources that happen to preserve casing (publisher headlines, video titles) — it is
never the determining signal, since it would silently fail on exactly the platform (Reddit) the
brief cares most about for participation signals.

**Generic phrases cannot anchor a cluster alone.** "27 rounds," "20-minute workout," "full-body
workout," and — per your explicit correction — "Spider-Man workout" itself, are all classified as
*generic/supporting* phrases (structural or descriptive, not proper-noun-shaped, or a nickname
pattern common enough to apply to many unrelated workouts). A generic phrase can only strengthen an
*existing* cluster anchored by a distinctive phrase; it can never start one by itself. "Tom Holland"
(proper noun, low ambiguity) can anchor. "Cindy" is the hard case — a common first name, not
inherently distinctive — and is handled by the co-occurrence mechanism below rather than by
capitalization or a hardcoded exception.

**Co-occurrence graph, built at the signal level, not the corpus level.** Nodes are candidate
phrases; edges are weighted by how often two phrases appear **within the same raw signal**, not
merely within the same rolling time window across different signals. This is the specific mechanism
that prevents incorrect merging:
- "Tom Holland" and "Cindy" co-occurring repeatedly *within the same articles/videos* builds a
  strong edge between them — this is what allows "Cindy" to be trusted as this cluster's anchor
  *in this context*, without needing to trust the bare string "Cindy" globally.
- "20-minute workout" only strengthens this specific cluster if it repeatedly co-occurs, in-signal,
  with "Tom Holland" or "Cindy" specifically. An unrelated 20-minute ab-workout video that never
  shares a signal with either never accumulates edge weight to this cluster — its "20-minute
  workout" mentions stay isolated regardless of how often that generic phrase appears elsewhere in
  the corpus that week.
- **Disambiguating "Cindy" the workout from "Cindy" a person in an unrelated story**: this is not
  solved by string-matching "Cindy" at all. Each occurrence of "Cindy" is evaluated by its local
  co-occurrence context (the other phrases/entities present in that same signal) and compared via
  the same signal-to-signal Jaccard mechanism Stage A clustering already uses (per the approved
  architecture) — an unrelated "Cindy" story shares essentially none of its other terms
  ("crossfit," "pull-ups," "AMRAP," "Tom Holland," "workout") with the fitness cluster's term set,
  so its Jaccard overlap stays near zero and it never merges in. The bare string is never the unit
  of comparison; the signal's full phrase-set context always is.

This is a design correction to build in Phase 2, not new code from this phase.

---

## 10. Recommendation

**Proceed to Phase 1**, with three concrete conditions carried forward rather than assumed:

1. **Reddit (Lane 2) collection cannot start until you register a Reddit OAuth script-app.** Phase 1
   can and should proceed on Lanes 1/3/4 without waiting for this — Lane 2 collector work is simply
   blocked until that credential exists, not a reason to delay the rest.
2. **A handful of Lane 1/Lane 4 sources (BarBend, T Nation, AU-specific outlets, government
   regulators) need individual manual feed-discovery work**, not the pattern-guessing this spike
   used — budget real time for this in Phase 1, don't assume every source resolves as cleanly as the
   ~16 already confirmed.
3. **Phase 2's clustering design must implement the corrected phrase-mining/co-occurrence approach
   in §9** — lowercase-primary, two-tier (candidate vs. confirmed) phrase trust, signal-level
   co-occurrence graph — not the Title-Case-centric, direct-to-config version from the original
   draft. This is now the documented spec for that phase.

The core validation this spike set out to get is real: genuine, current, cross-platform evidence of
exactly the kind of trend this system needs to catch is reachable through mechanisms already proven
to work (RSS, Google News search, yt-dlp search) using zero new dependencies. The gap is not
technical reachability — it's that discovery-without-a-keyword depends on Phase 2 mechanisms not yet
built, which is exactly why Phase 2 comes next, not Phase 1 alone.

---

# Phase 0B — Discovery-Loop Test (Seed → Expand → Confirm)

Appended 2026-08-18. Same read-only constraints as Phase 0A. The exploratory phrase-mining script
used to produce this section lives outside the repository, at
`/private/tmp/claude-501/.../scratchpad/phase0b/mine.py` (a Claude Code session scratchpad path,
not part of this repo) — nothing from it is committed here; only these findings are.

**Purpose**: Phase 0A proved the Tom Holland/Cindy content is *retrievable* once the search terms
are known. This section tests whether it is *discoverable* — whether an untargeted harness would
have surfaced "Tom Holland" or "Cindy" as candidate phrases on its own, before any human typed them.

**Forbidden terms were not used to seed any collection or query in this section.** "Tom," "Holland,"
"Cindy," "Spider-Man," and "27 rounds" appear below only in the post-hoc analysis, after untargeted
mining had already produced its rankings independently.

## Untargeted inputs collected

| Input | Method | Volume |
|---|---|---|
| Publisher RSS — current live state | Re-fetched 13 of the 15 confirmed-working Phase 0A feeds, as-is, no filtering | 987 items |
| Google News RSS — 7 permanent broad queries | `viral workout`, `workout challenge`, `celebrity workout routine`, `CrossFit workout`, `men's fitness`, `fitness trend`, `gym challenge trending` — generic, evergreen, none candidate-specific | ~40 items/query, ~250 unique after processing |
| YouTube — latest uploads, curated channel list | The *existing* 15-channel `scanner/channels.json` list, flat-extracted for the 6 most recent uploads per channel — no `ytsearch:` query used | 84 videos (14/15 channels; `@hyroxworld` currently has no videos tab — a real, honest access gap) |
| Reddit — one generic pull | `reddit.com/r/crossfit/.rss` (the subreddit's own default/hot ordering), no query term, single request after the earlier rate limit had cleared | 25 items |
| Manual editor signal | None available — no manual intake exists yet (Phase 1 work) | 0 |

**1,096 total raw signals**, all collected with zero knowledge of the target cluster.

## Phrase-mining method (deterministic, lowercase-primary, per the corrected §9 design)

All text lowercased before n-gram extraction (1–4 word spans, stopwords removed); Title-Case runs in
the *original*-case text are tracked separately as a secondary confidence signal only, not the
primary mechanism. A fixed generic-phrase filter (numeric+word templates, "full-body"/"bodyweight"
prefixes, bare "workout"/"exercise") was applied *before* ranking, matching the corrected design's
rule that generic phrases must not independently anchor. Candidates are scored by independent-source
count, lane count, and a Title-Case-seen boost — this is a first-pass, intentionally simple ranking
formula, not a final one; see the honest limitation noted below.

## Top 25 distinctive seed candidates (full ranked list of 60 saved alongside the script)

| # | Phrase | Occurrences | Independent sources | Lanes | Title-Case seen | Example |
|---|---|---|---|---|---|---|
| 1 | "fitness trend" | 33 | 33 | 1 | yes | *"Is Hyrox the new CrossFit? Explaining the latest fitness trend."* — USA Today |
| 2 | "workout routine" | 30 | 30 | 1 | yes | *"Robert Pattinson's Workout Routine And Diet Plan"* |
| 3 | "fitness challenge" | 25 | 25 | 1 | yes | *"I tried the Spider-Man fitness challenge — at 62"* — The Times |
| 4 | "fitness trends" | 18 | 18 | 1 | yes | *"…what the science really says about viral fitness trends"* |
| 5 | "crossfit games" | 11 | 11 | **3** | yes | *"The Hopper Returns…"* — CrossFit Games |
| 6 | "fitness routine" | 11 | 11 | 1 | yes | — |
| 7 | "viral fitness" | 11 | 11 | 1 | no | — |
| 8 | "workout challenge" | 9 | 9 | 1 | yes | — |
| 9 | "year old" | 9 | 9 | 1 | no | — |
| 10 | "every day" | 8 | 8 | 1 | yes | — |
| **11** | **"tom holland's"** | **8** | **8** | **1** | **yes** | *"Everyone's talking about Tom Holland's workout—here's how to attempt Spider-Man's routine"* |
| 12 | "weight loss" | 8 | 8 | 1 | yes | — |
| 13 | "jennifer aniston" | 8 | 8 | 1 | yes | *"Train Like Jennifer Aniston This Summer…"* |
| 14 | "body fat" | 6 | 6 | 2 | yes | — |
| 15 | "75 hard" | 8 | 8 | 1 | no | — |
| 16 | "right now" | 7 | 7 | 1 | yes | — |
| 17 | "build muscle" | 7 | 7 | 1 | yes | — |
| 18 | "arnold schwarzenegger" | 7 | 7 | 1 | yes | *"Jacked 88-Year-Old Who Deadlifts…"* |
| 19 | "going viral" | 7 | 7 | 1 | yes | *"Tom Holland's Boxing Workout Is Going Viral…"* |
| 20 | "walking challenge" | 7 | 7 | 1 | yes | — |
| 21 | "muscle strength" | 6 | 6 | 2 | no | — |
| 22 | "men fitness" | 6 | 6 | 2 | no | — |
| 23 | "high intensity" | 7 | 7 | 1 | no | — |
| 24 | "com au" | 7 | 7 | 1 | no | (AU-domain URL-fragment artifact, not a real phrase — a tokenizer cleanup item for Phase 2) |
| 25 | "strength training" | 6 | 6 | 1 | yes | — |

**Post-hoc check (not used to build the ranking above), run only after mining completed:**

| Term | Present as a mined candidate? | Source(s) |
|---|---|---|
| "tom holland" | **Yes** | `googlenews:Man of Many`, `googlenews:Men's Fitness`, `googlenews:boxrox.com`, **`publisher:boxrox`** |
| "tom holland's" | **Yes — ranked #11 in the untargeted top 25** | 8 independent sources including direct publisher-feed hits |
| "cindy" | **Yes** | `googlenews:Firstpost`, `googlenews:Good Housekeeping`, `googlenews:Women's Health`, **`publisher:menshealthuk`** |
| "cindy workout" (exact bigram) | No | The real phrasing in sources is `'Cindy' score` / `CrossFit 'Cindy' workout`, not the literal adjacent bigram — a tokenizer/quote-handling gap worth fixing in Phase 2, not a discovery failure |
| "spider man workout" | Yes | `googlenews:Men's Fitness`, `googlenews:tbreak.com` |
| "spider-man workout" (hyphenated form) | No | Same tokenizer normalization gap as above |

**Honest methodological finding**: "tom holland's" and "cindy" did not crack the top 10. The top of
the naive ranking is dominated by generic service-journalism vocabulary — "fitness trend,"
"workout routine," "fitness challenge," "weight loss," "strength training." These are not templated
the way "27 rounds" or "20-minute workout" are (so the fixed generic-filter correctly let them
through), but they are still generic *topic* vocabulary that recurs across dozens of unrelated
articles precisely because the broad Google News queries were designed to be broad. **This is a
concrete, real tuning gap for Phase 2, not a failure of the mechanism**: pure occurrence-count
ranking needs a rarity/IDF-style weighting (a phrase common across the whole corpus is less
distinctive than one that's rare corpus-wide but concentrated in a few signals), not just the fixed
template-based generic filter this spike used. Recorded as a design correction below.

## Did Tom Holland/Cindy appear naturally?

**Yes — decisively, and from a single untargeted signal, before any expansion.**

The Men's Health UK RSS feed (Lane 1, continuous monitoring, no query) currently contains, as of
this spike, one item published **10 Aug 2026 14:42 UTC**:

> **Title**: "Tom Holland's Favourite CrossFit Workout Is One of 'The Girls' – Here Are 20 Others to
> Test Your Fitness"
> **Description**: "The *Spider-Man* star's incredible 'Cindy' score has sent the CrossFit benchmark
> viral. Here's where the strangely named workout came from — and the other 'Girls' you need to know"

**This single item, by itself, already co-occurs "Tom Holland," "Spider-Man," "Cindy," and
"CrossFit" within one signal** — exactly the same-signal co-occurrence the corrected §9 design
requires before trusting an ambiguous common name like "Cindy" as part of a real cluster. No
expansion was even necessary to establish this specific pairing; it's already present in one
continuously-monitored RSS item.

A second, independent continuous-monitoring hit exists two days later: **BoxRox, 12 Aug 2026
07:59 UTC**, "Can You Beat Spiderman Actor Tom Holland's Score In His Favorite CrossFit Workout?"
(This item's RSS excerpt was thinner — "Can you beat spiderman?" — and did not itself contain
"Cindy," a reminder that RSS excerpt depth varies by publisher and single-signal co-occurrence isn't
guaranteed from every source, even when the underlying article covers it.)

### Which untargeted source produced the seed

**Men's Health UK's own RSS feed** — pure Lane 1 continuous monitoring, zero search, zero
pre-configured keyword. "Tom Holland" is exactly the kind of proper-noun-shaped, Title-Case,
low-ambiguity phrase the corrected design allows to seed from a single signal.

### Could that seed expand into the full cluster?

Tested directly, using **only the terms genuinely extractable from that one signal** — "Tom Holland
CrossFit workout" — deliberately *not* including "Cindy" as a query term, to test the expansion
mechanism honestly rather than assume it:

```
Query: "Tom Holland CrossFit workout" (Google News RSS search)
Result: 42 items, including:
  30 Jul 2026 — Men's Journal — "Tom Holland's Favorite CrossFit 'Cindy' Workout Is All Bodyweight…"
  10 Aug 2026 — Men's Health   — "Tom Holland's Favourite CrossFit Workout Is One of 'The Girls'…"
  13 Aug 2026 — Men's Fitness  — "Tom Holland's Viral 20-Minute Spider-Man Workout"
  17 Aug 2026 — The Post PH    — "Tom Holland's ripped Spider-Man body all boils down to…"
  10 Aug 2026 — NDTV           — "Tom Holland's Favourite Workout 'Cindy': What Makes This…"
```

**Yes — decisively.** A query built from nothing but the single untargeted seed's own extractable
terms surfaces every publisher pickup found in Phase 0A's targeted search, including the ones that
name "Cindy" explicitly. The expansion mechanism does not need the full target vocabulary to work —
it needs the distinctive proper-noun anchor, which continuous monitoring already provided.

### Was audience-participation evidence found?

**Partially, and honestly mixed.** Phase 0A's *targeted* scoped search (`r/crossfit` search for
"cindy") found 5 real Reddit entries. This section's *untargeted* single snapshot of
`r/crossfit`'s current hot feed (25 items, no query) did **not** contain any Tom Holland/Cindy
discussion — the 25 items visible right now are ordinary day-to-day CrossFit community traffic
(gear questions, "switching to CrossFit," a Games-winner congratulations thread). This is a real,
honest limitation of a single-subreddit, single-snapshot sample, not a fabricated negative — it
does not contradict Phase 0A's finding that the discussion exists on Reddit, only that it wasn't
visible in this one narrow, rate-limit-constrained pull. A production harness polling multiple
subreddits' hot/rising feeds every 3–4h, per the revised cadence, has a materially better chance of
catching a participation post than this single spike snapshot did.

### Timeline: how long would seed → confirmation take?

- **Origin (30 Jul, Men's Journal)**: checked directly — **not recoverable from continuous
  monitoring today.** Men's Journal's current feed spans only *17 Aug 15:10 to 23:31 UTC* (50 items
  in under 9 hours — a very high-volume publisher), meaning the origin article rolled off their feed
  window long before this spike ran. **A harness would have missed the true origin via continuous
  monitoring alone, unless it happened to be polling on 30 Jul itself.** This is a genuine,
  important limitation, not glossed over — and it's precisely why the architecture treats
  `origin_date` as "often unverifiable, best-effort" and `wave_detected_at` (first-party, always
  reliable) as the timestamp the system can actually trust.
- **Current wave (10–17 Aug, still actively growing — The Post PH published yesterday)**: fully
  catchable. Under the revised ~4h publisher-poll cadence, the Men's Health UK item (10 Aug 14:42)
  would have been ingested within one poll cycle of publication. Seed extraction is a deterministic
  regex/frequency operation — sub-second, no LLM, no network call. The expansion query fires in the
  same scan cycle. Confirmation (three independent signals, two-plus sources, corroborating lanes)
  is cleared within that first cycle given six independent publisher pickups already exist plus
  multiple YouTube videos. **Realistic seed-to-confirmation latency: one to two ~4h poll cycles
  (roughly 4–8 hours) from whenever the harness first encounters any single qualifying item —
  this is a mechanism-speed estimate, not a claim that a not-yet-built system already caught this
  historically.**

## Architecture correction, confirmed by real data

The generic-phrase suppression list from the original §9 design (numeric+word templates,
"full-body"/"bodyweight" prefixes) is necessary but not sufficient. Real data shows a second class
of generic phrase — common service-journalism topic vocabulary ("fitness trend," "workout routine,"
"fitness challenge," "weight loss," "strength training") — that isn't templated but is still
corpus-wide common enough to crowd out genuinely distinctive candidates under pure occurrence-count
ranking. **Phase 2's scoring for candidate-seed distinctiveness should add a rarity/IDF-style term**
(a phrase's distinctiveness is its cross-signal frequency *relative to its own baseline frequency in
the broader corpus*, not raw occurrence count alone) alongside the existing generic-template filter.
This is a tuning refinement to record for Phase 2, not a reason to distrust the underlying
seed→expand→confirm loop, which performed correctly once the specific target phrases were checked
directly rather than relying on the naive top-10 cutoff alone.

Also confirmed from real data: the two-threshold design (candidate-seed vs. confirmation) is
necessary, not just tidy. "Tom Holland" from the Men's Health UK item alone would correctly clear a
**candidate-seed** threshold (one distinctive, proper-noun-shaped, Title-Case-preserved signal) —
but would *not* alone clear a **confirmation** threshold, which properly requires the corroboration
the expansion step then supplied.

## Seed-expand-confirm budget protections — recorded design, not implemented

- **Maximum expansion searches per scan**: cap (e.g. 10–20) on how many newly-promoted candidate
  seeds trigger an expansion query in a single scan cycle, to bound API/request volume.
- **Maximum seeds expanded per scan**: a seed exceeding candidate-seed distinctiveness still queues
  rather than expanding immediately if the per-scan cap is reached; processed in priority order
  (proper-noun-shaped + multi-lane co-occurrence ranked above single-lane hits).
- **Seed expiry**: a candidate phrase that never reaches the confirmation threshold within N scan
  cycles (e.g. 3–5) is marked `expired`, not `rejected` — distinct from a phrase that was actively
  checked and found unrelated, since an expired seed may still recur later and shouldn't need to
  re-earn distinctiveness from zero.
- **Duplicate-query caching**: an expansion query already run for a given seed (or a near-duplicate
  seed sharing the same normalized core phrase) within the caching window is never re-run.
- **Generic-phrase suppression**: the corrected two-part filter above (template-based +
  corpus-relative rarity).
- **Source-reliability weighting**: independent-source-count contributions are weighted by each
  source's configured `reliability` field (already in the Phase 0 data model), so one high-volume,
  low-reliability aggregator can't single-handedly manufacture a confirmation.

## Pass/fail recommendation

**Pass — Condition A.** The untargeted harness naturally seeded the Tom Holland/Cindy cluster (a
single Men's Health UK RSS item, zero query, already co-occurring the core entity set) and
demonstrably expanded it into full cross-platform confirmation using only terms extractable from
that seed, without ever using "Cindy," "Spider-Man," or "27 rounds" as an input to collection or
querying. This was verified directly against real, current, live data — not asserted from Phase 0A's
earlier targeted results.

Three honest qualifications carried forward, none of which overturn the pass:
1. **Origin-date recovery genuinely fails** for high-volume publishers whose feed windows roll over
   in hours, not days — confirmed concretely with Men's Journal. This validates rather than
   undermines the architecture's `wave_detected_at`-over-`origin_date` design choice.
2. **The naive top-25 ranking under-surfaces the real candidates** unless checked directly — a real,
   named tuning gap for Phase 2's scoring formula (rarity weighting), not a discovery failure.
3. **Single-snapshot Reddit sampling missed the participation signal** that targeted search had
   already found in Phase 0A — a sampling-breadth limitation, expected to improve materially once
   Phase 1 polls multiple subreddits' hot/rising feeds on a real recurring schedule rather than one
   spike snapshot.

**Recommendation: proceed to Phase 1**, carrying forward the three qualifications above as concrete
Phase 2 tuning requirements (rarity-weighted seed scoring) and Phase 1 scope items (multi-subreddit
polling once Reddit OAuth exists), not as unresolved doubts about the core mechanism.
