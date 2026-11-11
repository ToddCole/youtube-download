"""SEO opportunity layer for the Today board.

For each pick in the latest Claude news scan:
  demand   -> OpenSEO get_keyword_metrics (volume + KD, AU and US)  [credits]
  footing  -> OpenSEO get_search_console_performance (MFO's own GSC) [free]
  verdict  -> write_now / update_existing / watch / skip

Plus a "demand we're not serving" feed: queries that started getting MFO
impressions in the last 7 days but had (almost) none in the 28 days before
-- the pattern behind the Physical 100 wins (Sartori, Nisi).

OpenSEO is called over its MCP endpoint with a personal API key
(OPENSEO_API_KEY in .env, sent as a bearer token). Credit use is measured
with whoami before/after and capped per day (OPENSEO_DAILY_CREDIT_CAP).

Run:  python3 seo_opportunity.py            (score latest scan + emerging feed)
      python3 seo_opportunity.py --check    (connection test, no credits)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import ssl
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import settings  # noqa: F401  (loads .env)

SCANNER_DIR = Path(__file__).resolve().parent
TREND_DB = SCANNER_DIR / "trend_radar.db"
CREDIT_LEDGER = SCANNER_DIR / "openseo_credits.json"
OPENSEO_URL = os.environ.get("OPENSEO_MCP_URL", "https://app.openseo.so/mcp")
MFO_PROJECT_ID = os.environ.get("OPENSEO_MFO_PROJECT_ID", "1eb3646f-c3a6-4805-986c-4124f8a26c13")
LOCATIONS = {"AU": 2036, "US": 2840}
CACHE_DAYS = 7
MAX_QUERIES_PER_PICK = 5
DEFAULT_DAILY_CREDIT_CAP = 150
# Rough upper bound per keyword-metrics call, used only for the pre-call cap check.
EST_CREDITS_PER_CALL = 15
EST_CREDITS_PER_KEYWORD = 1

# MFO is an Australian site: verdicts are judged on AU numbers. US numbers
# are kept for display and for the "US demand only" verdict.
WRITE_NOW_MIN_AU_VOLUME = 50
WRITE_NOW_MAX_KD = 20
US_ONLY_MIN_VOLUME = 200


class OpenSEOError(RuntimeError):
    pass


class CreditCapReached(RuntimeError):
    pass


# --- Minimal MCP (streamable HTTP) client --------------------------------

class OpenSEOClient:
    def __init__(self, api_key: str | None = None, url: str = OPENSEO_URL):
        self.api_key = api_key or os.environ.get("OPENSEO_API_KEY")
        if not self.api_key:
            raise OpenSEOError("OPENSEO_API_KEY is not set in .env")
        self.url = url
        self.session_id: str | None = None
        self._next_id = 1
        import certifi

        self._ssl = ssl.create_default_context(cafile=certifi.where())

    def _post(self, payload: dict) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.api_key}",
            "MCP-Protocol-Version": "2025-06-18",
            # Cloudflare blocks urllib's default UA (error 1010).
            "User-Agent": "mfo-editorial-desk/1.0",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=120, context=self._ssl) as response:
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                body = response.read().decode("utf-8", errors="replace")
                ctype = response.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001
            # Never include headers (the key) in the message.
            raise OpenSEOError(f"OpenSEO request failed: {exc.__class__.__name__}: {getattr(exc, 'code', '')} {getattr(exc, 'reason', '')}".strip()) from None
        if "id" not in payload or not body.strip():
            return None
        if "text/event-stream" in ctype:
            messages = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            for raw in reversed(messages):
                message = json.loads(raw)
                if message.get("id") == payload["id"]:
                    return message
            raise OpenSEOError("no matching response in event stream")
        return json.loads(body)

    def _rpc(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if message is None:
            raise OpenSEOError(f"empty response to {method}")
        if "error" in message:
            raise OpenSEOError(f"{method}: {message['error'].get('message')}")
        return message["result"]

    def connect(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mfo-editorial-desk", "version": "1.0"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict) -> dict:
        if self.session_id is None and self._next_id == 1:
            self.connect()
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []))
            raise OpenSEOError(f"{tool}: {text[:300]}")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except ValueError:
                    continue
        raise OpenSEOError(f"{tool}: no JSON content in result")

    def credits(self) -> int | None:
        value = self.call("whoami", {}).get("creditsRemaining")
        return int(value) if isinstance(value, (int, float)) else None


# --- Credit ledger -------------------------------------------------------

class CreditLedger:
    def __init__(self, path: Path = CREDIT_LEDGER, daily_cap: int | None = None):
        self.path = path
        self.daily_cap = daily_cap if daily_cap is not None else int(os.environ.get("OPENSEO_DAILY_CREDIT_CAP") or DEFAULT_DAILY_CREDIT_CAP)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def used_today(self) -> int:
        return int(self._load().get(date.today().isoformat(), 0))

    def check(self, estimate: int) -> None:
        if self.used_today() + estimate > self.daily_cap:
            raise CreditCapReached(f"OpenSEO daily cap {self.daily_cap} credits (used {self.used_today()}, next call ~{estimate})")

    def add(self, credits: int) -> None:
        data = self._load()
        today = date.today().isoformat()
        data[today] = int(data.get(today, 0)) + max(int(credits), 0)
        keep = sorted(data)[-60:]
        self.path.write_text(json.dumps({k: data[k] for k in keep}, indent=2) + "\n", encoding="utf-8")


# --- Storage -------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS seo_keyword_cache (
            keyword TEXT NOT NULL, location TEXT NOT NULL, fetched_at TEXT NOT NULL, metrics_json TEXT,
            PRIMARY KEY (keyword, location)
        );
        CREATE TABLE IF NOT EXISTS seo_scores (
            scan_id INTEGER NOT NULL, rank INTEGER NOT NULL, scored_at TEXT NOT NULL, score_json TEXT NOT NULL,
            PRIMARY KEY (scan_id, rank)
        );
        CREATE TABLE IF NOT EXISTS seo_emerging (
            id INTEGER PRIMARY KEY AUTOINCREMENT, computed_at TEXT NOT NULL, rows_json TEXT NOT NULL
        );
        """
    )
    conn.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_query(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"'“”‘’]", "", (text or "").lower())).strip()


def cached_metrics(conn: sqlite3.Connection, keyword: str, location: str) -> dict | None | bool:
    """Returns metrics dict, None (fetched: no data), or False (not cached / stale)."""
    row = conn.execute(
        "SELECT fetched_at, metrics_json FROM seo_keyword_cache WHERE keyword=? AND location=?", (keyword, location)
    ).fetchone()
    if not row:
        return False
    fetched = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched > timedelta(days=CACHE_DAYS):
        return False
    return json.loads(row[1]) if row[1] else None


def fetch_metrics(conn, client: OpenSEOClient, ledger: CreditLedger, keywords: list[str], location: str) -> None:
    missing = [k for k in keywords if cached_metrics(conn, k, location) is False]
    if not missing:
        return
    ledger.check(EST_CREDITS_PER_CALL + EST_CREDITS_PER_KEYWORD * len(missing))
    before = client.credits()
    data = client.call("get_keyword_metrics", {
        "projectId": MFO_PROJECT_ID,
        "keywords": missing,
        "locationCode": LOCATIONS[location],
        "includeMonthlyTrends": False,
    })
    after = client.credits()
    if before is not None and after is not None:
        ledger.add(before - after)
    else:
        ledger.add(EST_CREDITS_PER_CALL + len(missing))
    found = {normalise_query(row.get("keyword", "")): row for row in data.get("keywords") or []}
    now = utc_now()
    for keyword in missing:
        row = found.get(keyword)
        conn.execute(
            "INSERT OR REPLACE INTO seo_keyword_cache (keyword, location, fetched_at, metrics_json) VALUES (?,?,?,?)",
            (keyword, location, now, json.dumps(row) if row else None),
        )
    conn.commit()


# --- Search Console ------------------------------------------------------

def gsc_rows(client: OpenSEOClient, **arguments) -> list[dict]:
    data = client.call("get_search_console_performance", {"projectId": MFO_PROJECT_ID, **arguments})
    return data.get("rows") or []


_STOP = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "is", "how", "what", "why", "does", "do", "vs", "with", "after", "best"}


def _content_words(query: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", query) if w not in _STOP and len(w) > 1}


def gsc_matches(queries: list[str], rows: list[dict]) -> list[dict]:
    """GSC rows whose query contains all content words of a pick query (or
    vice versa). Conservative on purpose: a false 'we already rank' would
    hide a real opportunity."""
    matches = []
    for row in rows:
        keys = row.get("keys") or []
        if not keys:
            continue
        gsc_query = normalise_query(keys[0])
        gsc_words = _content_words(gsc_query)
        for query in queries:
            words = _content_words(query)
            if words and (words <= gsc_words or (gsc_words and gsc_words <= words and len(gsc_words) >= 2)):
                matches.append({
                    "query": gsc_query,
                    "page": keys[1] if len(keys) > 1 else None,
                    "impressions": row.get("impressions", 0),
                    "clicks": row.get("clicks", 0),
                    "position": row.get("position"),
                })
                break
    matches.sort(key=lambda m: -(m["impressions"] or 0))
    return matches


def keyword_pages(queries: list[str], pages: list[dict]) -> list[dict]:
    """MFO archive pages whose title/slug contain every content word of one of
    the queries: strict on purpose, so 'Update existing' means a real page."""
    out = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_words = _content_words(normalise_query(f"{page.get('title', '')} {page.get('slug', '')}".replace("-", " ")))
        for query in queries:
            words = _content_words(query)
            if len(words) >= 2 and words <= page_words:
                out.append({"title": page.get("title") or page.get("slug"), "url": page.get("url"), "query": query})
                break
    return out


def load_mfo_pages() -> list[dict]:
    from editorial_today import MFO_INDEX
    try:
        return json.loads(MFO_INDEX.read_text(encoding="utf-8")).get("pages") or []
    except (OSError, ValueError):
        return []


# --- Verdicts ------------------------------------------------------------

@dataclass
class Verdict:
    code: str
    label: str
    reason: str


def decide(demand: dict, footing: list[dict], related_pages: list[dict], update_url: str | None = None) -> Verdict:
    au = demand.get("au_volume") or 0
    us = demand.get("us_volume") or 0
    kd = demand.get("best_kd")  # AU difficulty; None when the provider has no score
    if footing:
        top = footing[0]
        pos = top.get("position")
        pos_txt = f"pos {pos:.1f}" if isinstance(pos, (int, float)) else "no position"
        return Verdict(
            "update_existing", "Update existing page",
            f"MFO already gets {top['impressions']} impressions for '{top['query']}' ({pos_txt}) on {top.get('page') or 'a page'}",
        )
    if update_url:
        # Claude matched this pick to an existing MFO page: never "no MFO page yet".
        slug = update_url.replace("https://mensfitnessonline.com.au", "")
        return Verdict("update_existing", "Update existing page",
                       f"Claude matched this to MFO's {slug} ({au:,}/mo AU searches for '{demand.get('best_keyword')}')")
    if related_pages and (au or us):
        return Verdict("update_existing", "Update existing page",
                       f"AU {au:,}/mo, US {us:,}/mo search demand and MFO already has a page on "
                       f"'{related_pages[0].get('query')}': {related_pages[0].get('title')}")
    keyword = demand.get("best_keyword")
    if au >= WRITE_NOW_MIN_AU_VOLUME and (kd is None or kd <= WRITE_NOW_MAX_KD):
        kd_txt = f"AU KD {kd}" if kd is not None else "AU KD unknown, check before committing"
        return Verdict("write_now", "Write now",
                       f"{au:,}/mo AU searches ({keyword}, {kd_txt}) and no MFO page yet")
    if au >= WRITE_NOW_MIN_AU_VOLUME and kd is not None and kd > WRITE_NOW_MAX_KD:
        return Verdict("skip", "Low SEO upside",
                       f"AU demand exists ({au:,}/mo) but AU KD {kd} is above {WRITE_NOW_MAX_KD}: write it for news value only")
    if us >= US_ONLY_MIN_VOLUME and au < WRITE_NOW_MIN_AU_VOLUME:
        return Verdict("us_only", "US demand only",
                       f"{us:,}/mo US searches ({keyword}) but only {au:,}/mo in Australia: news value first, not an AU search play")
    if not au and not us:
        return Verdict("watch", "Watch",
                       "No measurable search demand yet: news value only. Re-check GSC in 48h for new impressions")
    return Verdict("watch", "Watch", f"Small demand (AU {au}/mo, US {us}/mo): news value first")


def _kd(metrics: dict):
    """Keyword difficulty, with 0 treated as unknown: the provider returns 0
    when it has no score, which made hard keywords look free."""
    kd = metrics.get("keyword_difficulty")
    return kd if kd else None


def demand_for(conn, queries: list[str]) -> dict:
    per_keyword = []
    for query in queries:
        row = {"keyword": query}
        for location in LOCATIONS:
            metrics = cached_metrics(conn, query, location)
            metrics = metrics if isinstance(metrics, dict) else {}
            row[f"{location}_volume"] = metrics.get("search_volume") or 0
            row[f"{location}_kd"] = _kd(metrics)
        row["volume"] = sum(row[f"{loc}_volume"] for loc in LOCATIONS)
        per_keyword.append(row)
    # Best keyword = most AU searches; US volume only breaks ties.
    per_keyword.sort(key=lambda r: (-r["AU_volume"], -r["US_volume"]))
    best = per_keyword[0] if per_keyword else {}
    return {
        "keywords": per_keyword,
        "best_keyword": best.get("keyword"),
        "total_volume": best.get("volume", 0),
        "au_volume": best.get("AU_volume", 0),
        "us_volume": best.get("US_volume", 0),
        "best_kd": best.get("AU_kd"),
        "us_kd": best.get("US_kd"),
    }


def score_latest_scan(client: OpenSEOClient, ledger: CreditLedger, db_path: Path = TREND_DB) -> dict:
    from trend_radar.scan_store import latest_scan

    conn = sqlite3.connect(db_path)
    ensure_tables(conn)
    scan = latest_scan(conn)
    if not scan:
        return {"scored": 0, "note": "no Claude scan yet"}
    scan_id = scan["_scan_id"]
    picks = []
    for candidate in scan.get("candidates") or []:
        queries = []
        for q in candidate.get("search_queries") or []:
            q = normalise_query(q)
            if q and q not in queries and len(q) <= 80:
                queries.append(q)
        picks.append((candidate, queries[:MAX_QUERIES_PER_PICK]))

    all_queries = sorted({q for _c, qs in picks for q in qs})
    errors = []
    if all_queries:
        for location in LOCATIONS:
            try:
                fetch_metrics(conn, client, ledger, all_queries, location)
            except CreditCapReached as exc:
                errors.append(str(exc))
                break
    rows = gsc_rows(client, dateRange="last_28_days", dimensions=["query", "page"], rowLimit=1000)
    mfo_pages = load_mfo_pages()

    scored = 0
    for candidate, queries in picks:
        demand = demand_for(conn, queries)
        footing = gsc_matches(queries, rows)[:5]
        demand_queries = [k["keyword"] for k in demand["keywords"] if k["volume"] > 0]
        pages = keyword_pages(demand_queries, mfo_pages)
        update_url = candidate.get("update_url") if candidate.get("story_type") == "update" else None
        verdict = decide(demand, footing, pages, update_url) if queries else Verdict(
            "watch", "Watch", "No search queries proposed for this pick (scan predates SEO scoring)")
        score = {
            "verdict": verdict.code, "label": verdict.label, "reason": verdict.reason,
            "queries": queries, "demand": demand, "gsc": footing, "pages": pages,
        }
        conn.execute(
            "INSERT OR REPLACE INTO seo_scores (scan_id, rank, scored_at, score_json) VALUES (?,?,?,?)",
            (scan_id, candidate.get("rank"), utc_now(), json.dumps(score)),
        )
        scored += 1
    conn.commit()
    conn.close()
    return {"scan_id": scan_id, "scored": scored, "keywords": len(all_queries), "errors": errors}


def emerging_demand(client: OpenSEOClient, db_path: Path = TREND_DB, limit: int = 15) -> list[dict]:
    """Queries with real impressions in the last 7 days that had (almost)
    none in the 28 days before: new demand MFO is catching by accident."""
    end = date.today() - timedelta(days=3)  # GSC lag
    recent_start = end - timedelta(days=6)
    prior_end = recent_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=27)
    recent = gsc_rows(client, startDate=recent_start.isoformat(), endDate=end.isoformat(),
                      dimensions=["query", "page"], rowLimit=1000, minImpressions=20)
    prior = gsc_rows(client, startDate=prior_start.isoformat(), endDate=prior_end.isoformat(),
                     dimensions=["query"], rowLimit=1000)
    prior_impr = {normalise_query((r.get("keys") or [""])[0]): r.get("impressions", 0) for r in prior}
    out = []
    seen = set()
    for row in sorted(recent, key=lambda r: -(r.get("impressions") or 0)):
        keys = row.get("keys") or []
        query = normalise_query(keys[0]) if keys else ""
        if not query or query in seen:
            continue
        before = prior_impr.get(query, 0)
        if before <= 5:
            seen.add(query)
            out.append({
                "query": query, "page": keys[1] if len(keys) > 1 else None,
                "impressions_7d": row.get("impressions", 0), "clicks_7d": row.get("clicks", 0),
                "position": row.get("position"), "impressions_prior_28d": before,
            })
        if len(out) >= limit:
            break
    conn = sqlite3.connect(db_path)
    ensure_tables(conn)
    conn.execute("INSERT INTO seo_emerging (computed_at, rows_json) VALUES (?, ?)", (utc_now(), json.dumps(out)))
    conn.commit()
    conn.close()
    return out


def load_scores(scan_id: int, db_path: Path = TREND_DB) -> dict[int, dict]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        ensure_tables(conn)
        rows = conn.execute("SELECT rank, score_json FROM seo_scores WHERE scan_id = ?", (scan_id,)).fetchall()
    finally:
        conn.close()
    return {int(rank): json.loads(score) for rank, score in rows}


def load_emerging(db_path: Path = TREND_DB) -> dict | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        ensure_tables(conn)
        row = conn.execute("SELECT computed_at, rows_json FROM seo_emerging ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return {"computed_at": row[0], "rows": json.loads(row[1])} if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="connection test only (no credits)")
    parser.add_argument("--no-emerging", action="store_true")
    args = parser.parse_args()
    try:
        client = OpenSEOClient()
        client.connect()
        if args.check:
            print(f"OpenSEO OK: {client.credits()} credits remaining")
            return 0
        ledger = CreditLedger()
        result = score_latest_scan(client, ledger)
        print(f"SEO scored {result.get('scored', 0)} pick(s), {result.get('keywords', 0)} keyword(s); credits used today {ledger.used_today()}/{ledger.daily_cap}")
        for error in result.get("errors") or []:
            print(f"WARNING: {error}", file=sys.stderr)
        if not args.no_emerging:
            rows = emerging_demand(client)
            print(f"Emerging demand: {len(rows)} new quer(ies)")
        try:
            import evergreen
            from editorial_today import latest_trend_scan

            scan = latest_trend_scan(TREND_DB)
            if scan:
                ideas = evergreen.compute(scan, evergreen.striking_rows(client), load_mfo_pages())
                print(f"Evergreen: {len(ideas)} idea(s) for scan {scan.get('_scan_id')}")
        except Exception as exc:  # noqa: BLE001  (evergreen is a bonus; never fail the step)
            print(f"WARNING: evergreen ideas failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    except OpenSEOError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
