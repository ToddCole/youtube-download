"""Trending now: what Australians are searching for and watching, matched
to MFO's pages and topics. Free sources only, no Claude:

  * Google Trends "Trending now" for Australia (RSS, refreshed through the day)
  * Wikipedia pages with a sudden jump in Australian views (Wikimedia API)
  * Netflix Top 10 TV in Australia, new this week (Netflix's public TSV)

An item is kept only when it lines up with MFO: it matches an existing MFO
page (update it while people are searching) or one of MFO's topics (write
the piece). Everything else is dropped, which is most of it.

  python3 trending.py            # compute and store
  python3 trending.py --dry-run  # print, store nothing
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

SCANNER_DIR = Path(__file__).resolve().parent
TREND_DB = SCANNER_DIR / "trend_radar.db"
MFO_INDEX = SCANNER_DIR / "mfo_index.json"
NETFLIX_CACHE = SCANNER_DIR / "reports" / "netflix-top10-countries.tsv"
NETFLIX_MAX_AGE_HOURS = 24

GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo=AU"
WIKI_TOP_AU = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/AU/all-access/{y}/{m:02d}/{d:02d}"
WIKI_API = "https://en.wikipedia.org/w/api.php"
NETFLIX_TSV = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"
USER_AGENT = "mfo-editorial-desk/1.0 (trending scanner)"

WIKI_BASELINE_DAYS = 7
WIKI_SPIKE_RATIO = 3.0       # today's views vs the article's average over the baseline days
WIKI_MIN_VIEWS = 1500        # AU views (Wikimedia rounds up) to count at all
MAX_ITEMS = {"google": 10, "wikipedia": 10, "netflix": 6}

# MFO's topics. A Trends search (with its news headlines), a Wikipedia
# description or a Netflix show title must hit one of these to count.
TOPIC_TERMS = {
    "Training and gyms": ["workout", "gym", "fitness", "strength training", "weightlifting", "bench press",
                          "deadlift", "squat", "personal trainer", "crossfit", "hyrox", "pilates", "calisthenics"],
    "Bodybuilding and strength sport": ["bodybuild", "mr olympia", "olympia", "physique", "strongman",
                                        "powerlift", "arm wrestl", "steroid"],
    "Fitness TV": ["physical 100", "physical:", "100 physique", "the challenge", "american gladiators",
                   "gladiators", "ninja warrior", "the biggest loser", "squid game"],
    "Endurance": ["marathon", "ultramarathon", "ultra runner", "backyard ultra", "triathlon", "ironman",
                  "cyclist", "swimmer"],
    "Combat sport": ["ufc", "boxer", "boxing", "mixed martial", "mma", "kickboxer", "wrestler"],
    "Australian football codes": ["nrl", "afl", "rugby league", "rugby union", "wallabies", "state of origin",
                                  "australian rules"],
    "Diet, weight loss and supplements": ["diet", "weight loss", "ozempic", "wegovy", "mounjaro", "glp-1",
                                          "semaglutide", "tirzepatide", "protein", "creatine", "supplement",
                                          "peptide", "testosterone", "tga", "recall"],
    "Health and longevity": ["heart attack", "cardiac arrest", "longevity", "men's health", "cancer screening",
                             "prostate"],
}
_TOPIC_RES = {topic: re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")", re.I)
              for topic, terms in TOPIC_TERMS.items()}
# Any athlete or Olympian; other sportspeople only when Australian (a
# trending English football manager isn't an MFO story, Ash Barty might be).
_TOPIC_RES["Athletes"] = re.compile(
    r"\b(?:athlete|olympian|paralympian|australian [\w ]{0,30}?(?:footballer|cricketer|swimmer|tennis player|"
    r"sprinter|surfer|cyclist|basketball player|golfer|racing driver|jockey))", re.I)

# Wikipedia articles that are never news (and sports fixtures/seasons, which
# spike every week without being a story).
_WIKI_SKIP = re.compile(r"^(Main_Page|Special:|Wikipedia:|Portal:|File:|Help:|Talk:|Category:|Template:|User:|"
                        r"Deaths_in_|List_of_|\d{4}(–\d{2,4})?_)", re.I)
_STOPWORDS = {"the", "and", "for", "with", "from", "how", "what", "who", "why", "vs", "of", "in", "on", "at",
              "to", "a", "an", "is", "his", "her", "new", "2025", "2026", "2027", "season", "film", "tv", "series"}


# --- HTTP -------------------------------------------------------------------

def _context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http_get(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout, context=_context()) as response:
        return response.read()


# --- Matching -----------------------------------------------------------------

def _tokens(text: str | None) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower().replace("'", "")) if t not in _STOPWORDS]


def topics_for(*texts: str | None) -> list[str]:
    blob = " ".join(t for t in texts if t)
    return [topic for topic, rx in _TOPIC_RES.items() if rx.search(blob)]


def load_pages(path: Path = MFO_INDEX) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pages = data.get("pages") if isinstance(data, dict) else data
    return [p for p in pages or [] if isinstance(p, dict) and p.get("url")]


def mfo_page_for(term: str, pages: list[dict]) -> dict | None:
    """The MFO page whose slug or title contains every word of `term`
    (e.g. "abdul el sayed" -> /abdul-el-sayed-workout/). Single-word terms
    need an exact slug-word hit, so "magic" doesn't match everything."""
    words = _tokens(term)
    if len(words) < 2:  # one word ("body", "magic") matches far too much
        return None
    best = None
    for page in pages:
        page_words = set(_tokens(page.get("slug"))) | set(_tokens(page.get("title")))
        if all(w in page_words for w in words):
            # Prefer the shortest slug: the page *about* the term, not one that mentions it.
            if best is None or len(page.get("slug") or "") < len(best.get("slug") or ""):
                best = page
    return {"url": best["url"], "title": best.get("title")} if best else None


def classify(term: str, context: list[str], pages: list[dict]) -> dict | None:
    """{"mfo_page", "topics"} when the item lines up with MFO, else None."""
    page = mfo_page_for(term, pages)
    topics = topics_for(term, *context)
    if not page and not topics:
        return None
    return {"mfo_page": page, "topics": topics}


# --- Google Trends ----------------------------------------------------------

_HT = "{https://trends.google.com/trending/rss}"


def parse_google_trends(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter("item"):
        term = (item.findtext("title") or "").strip()
        if not term:
            continue
        news = []
        for n in item.iter(f"{_HT}news_item"):
            news.append({"title": (n.findtext(f"{_HT}news_item_title") or "").strip(),
                         "url": (n.findtext(f"{_HT}news_item_url") or "").strip(),
                         "source": (n.findtext(f"{_HT}news_item_source") or "").strip()})
        out.append({"term": term, "traffic": (item.findtext(f"{_HT}approx_traffic") or "").strip(),
                    "published": (item.findtext("pubDate") or "").strip(), "news": news})
    return out


def google_trends(pages: list[dict]) -> list[dict]:
    kept = []
    for t in parse_google_trends(http_get(GOOGLE_TRENDS_RSS)):
        match = classify(t["term"], [n["title"] for n in t["news"]], pages)
        if match:
            kept.append({"source": "google", "term": t["term"], "traffic": t["traffic"],
                         "detail": t["news"][0]["title"] if t["news"] else "",
                         "url": t["news"][0]["url"] if t["news"] else None,
                         "news": t["news"][:3], **match})
    return kept[:MAX_ITEMS["google"]]


# --- Wikipedia --------------------------------------------------------------

def _wiki_day(day: date) -> dict[str, int]:
    raw = json.loads(http_get(WIKI_TOP_AU.format(y=day.year, m=day.month, d=day.day)))
    articles = (raw.get("items") or [{}])[0].get("articles") or []
    return {a["article"]: a.get("views_ceil") or a.get("views") or 0
            for a in articles if a.get("project") == "en.wikipedia"}


def wiki_spikes(today_views: dict[str, int], baseline: list[dict[str, int]]) -> list[tuple[str, int, float]]:
    """(article, views, ratio) for articles well above their recent level.
    An article absent from a baseline day's top list counts as zero there,
    so a name nobody looked up last week and everyone looks up today wins."""
    out = []
    for article, views in today_views.items():
        if views < WIKI_MIN_VIEWS or _WIKI_SKIP.search(article):
            continue
        avg = sum(day.get(article, 0) for day in baseline) / max(len(baseline), 1)
        ratio = views / avg if avg else float("inf")
        if ratio >= WIKI_SPIKE_RATIO:
            out.append((article, views, ratio))
    out.sort(key=lambda x: -x[1])
    return out


def wiki_descriptions(titles: list[str]) -> dict[str, str]:
    """Wikipedia's one-line short description for each title ("Australian
    rugby league footballer"), 50 titles per request."""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        query = urlencode({"action": "query", "prop": "description", "format": "json", "redirects": 1,
                           "titles": "|".join(t.replace("_", " ") for t in chunk)})
        data = json.loads(http_get(f"{WIKI_API}?{query}"))
        q = data.get("query") or {}
        back = {r["to"]: r["from"] for r in q.get("redirects") or []}
        back.update({n["to"]: n["from"] for n in q.get("normalized") or []})
        for page in (q.get("pages") or {}).values():
            title = page.get("title", "")
            original = back.get(back.get(title, title), back.get(title, title))
            out[original.replace(" ", "_")] = page.get("description") or ""
            out[title.replace(" ", "_")] = page.get("description") or ""
    return out


def wikipedia(pages: list[dict], today: date | None = None) -> list[dict]:
    # Wikimedia publishes a day's per-country numbers the following day.
    day = today or (date.today() - timedelta(days=1))
    try:
        current = _wiki_day(day)
    except Exception:  # noqa: BLE001  (yesterday may not be published yet)
        day -= timedelta(days=1)
        current = _wiki_day(day)
    baseline = []
    for n in range(1, WIKI_BASELINE_DAYS + 1):
        try:
            baseline.append(_wiki_day(day - timedelta(days=n)))
        except Exception:  # noqa: BLE001
            continue
    spikes = wiki_spikes(current, baseline)[:60]
    descriptions = wiki_descriptions([a for a, _, _ in spikes]) if spikes else {}
    kept = []
    for article, views, ratio in spikes:
        name = article.replace("_", " ")
        desc = descriptions.get(article, "")
        match = classify(re.sub(r"\s*\(.*\)$", "", name), [desc], pages)
        if match:
            kept.append({"source": "wikipedia", "term": name, "detail": desc,
                         "traffic": f"{views:,} AU views" + (f", {ratio:.0f}x usual" if ratio != float("inf") else ", new"),
                         "url": f"https://en.wikipedia.org/wiki/{quote(article)}", "day": day.isoformat(), **match})
    return kept[:MAX_ITEMS["wikipedia"]]


# --- Netflix ----------------------------------------------------------------

def _netflix_tsv() -> str:
    fresh = NETFLIX_CACHE.exists() and (time.time() - NETFLIX_CACHE.stat().st_mtime) < NETFLIX_MAX_AGE_HOURS * 3600
    if not fresh:
        NETFLIX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NETFLIX_CACHE.write_bytes(http_get(NETFLIX_TSV, timeout=120))
    return NETFLIX_CACHE.read_text(encoding="utf-8")


def netflix_au_new(tsv_text: str) -> list[dict]:
    """Australia's TV Top 10 for the latest week, shows new to the list
    (first or second week) only."""
    rows = [r for r in csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
            if r.get("country_iso2") == "AU" and (r.get("category") or "").startswith("TV")]
    if not rows:
        return []
    week = max(r["week"] for r in rows)
    out = []
    for r in sorted((r for r in rows if r["week"] == week), key=lambda r: int(r.get("weekly_rank") or 99)):
        weeks = int(r.get("cumulative_weeks_in_top_10") or 0)
        if weeks <= 2:
            out.append({"show": r["show_title"], "season": r.get("season_title") if r.get("season_title") != "N/A" else "",
                        "rank": int(r.get("weekly_rank") or 0), "weeks": weeks, "week": week})
    return out


def netflix(pages: list[dict]) -> list[dict]:
    kept = []
    for show in netflix_au_new(_netflix_tsv()):
        match = classify(show["show"], [show["season"]], pages)
        if match:
            kept.append({"source": "netflix", "term": show["show"], "detail": show["season"],
                         "traffic": f"#{show['rank']} in Australia, week of {show['week']}"
                                    + (" (new)" if show["weeks"] == 1 else ""),
                         "url": f"https://www.netflix.com/search?q={quote(show['show'])}", **match})
    return kept[:MAX_ITEMS["netflix"]]


# --- Compute and store ------------------------------------------------------

def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS trending (
        id INTEGER PRIMARY KEY AUTOINCREMENT, computed_at TEXT NOT NULL, result_json TEXT NOT NULL)""")
    conn.commit()


def compute(dry_run: bool = False, db_path: Path = TREND_DB) -> dict:
    pages = load_pages()
    result: dict = {"computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "errors": []}
    for name, fn in (("google", google_trends), ("wikipedia", wikipedia), ("netflix", netflix)):
        try:
            result[name] = fn(pages)
        except Exception as exc:  # noqa: BLE001  (one dead source never sinks the others)
            result[name] = []
            result["errors"].append(f"{name}: {exc.__class__.__name__}: {exc}"[:200])
    # Items that match an MFO page first: those are the quickest wins.
    for name in ("google", "wikipedia", "netflix"):
        result[name].sort(key=lambda i: i.get("mfo_page") is None)
    if not dry_run:
        conn = sqlite3.connect(db_path)
        try:
            ensure_table(conn)
            conn.execute("INSERT INTO trending (computed_at, result_json) VALUES (?, ?)",
                         (result["computed_at"], json.dumps(result)))
            conn.commit()
        finally:
            conn.close()
    return result


def load(db_path: Path = TREND_DB) -> dict | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        ensure_table(conn)
        row = conn.execute("SELECT result_json FROM trending ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def load_recent(hours: int = 24, db_path: Path = TREND_DB) -> dict | None:
    """Every item matched in the last `hours`, newest sighting kept: Google's
    feed only shows ~10 searches at a time, so one run alone misses most of
    the day. Same shape as one compute() result."""
    if not db_path.exists():
        return None
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    try:
        ensure_table(conn)
        rows = conn.execute("SELECT result_json FROM trending WHERE computed_at >= ? ORDER BY id DESC",
                            (since,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    runs = [json.loads(r[0]) for r in rows]
    merged: dict = {"computed_at": runs[0]["computed_at"], "errors": runs[0].get("errors") or []}
    for name in ("google", "wikipedia", "netflix"):
        seen, items = set(), []
        for run in runs:
            for item in run.get(name) or []:
                key = item["term"].lower()
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        items.sort(key=lambda i: i.get("mfo_page") is None)
        merged[name] = items[:MAX_ITEMS[name]]
    return merged


def all_items(result: dict | None) -> list[dict]:
    return [i for name in ("google", "wikipedia", "netflix") for i in (result or {}).get(name) or []]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = compute(dry_run=args.dry_run)
    for item in all_items(result):
        where = item["mfo_page"]["url"].replace("https://mensfitnessonline.com.au", "") if item.get("mfo_page") \
            else ", ".join(item.get("topics") or [])
        print(f"[{item['source']}] {item['term']} · {item.get('traffic', '')} · {item.get('detail', '')[:80]} -> {where}")
    print(f"Trending: {len(all_items(result))} item(s) matched MFO")
    for error in result["errors"]:
        print(f"WARNING: {error}", file=sys.stderr)
    # One source down is a warning in the log, not an alert email; all three down is a failure.
    return 1 if len(result["errors"]) >= 3 else 0


if __name__ == "__main__":
    raise SystemExit(main())
