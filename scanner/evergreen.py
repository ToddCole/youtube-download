"""Evergreen ideas for quiet news days.

When a Claude scan returns fewer than TARGET_SLOTS picks, the digest is
topped up with evergreen ideas, from two sources taken in turn:

  refresh  MFO pages Google already shows at positions 5-20 for queries
           with real impressions (last 28 days of Search Console, free via
           OpenSEO). A refresh is the cheapest traffic win there is.
  angle    held stories where the scan says MFO could still publish a
           lasting explainer (the scan's evergreen_angles).

An idea doesn't come back for COOLDOWN_DAYS; one that got 👎 stays away
for DOWNVOTE_COOLDOWN_DAYS. Pages edited in the last RECENTLY_EDITED_DAYS
are left alone so Google can re-rank them first.

Ideas are stored per scan (ranks 101+, so 👍/👎 votes reuse pick_feedback)
and computed once: seo_opportunity.py calls compute() after scoring.

Manual:  python3 evergreen.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path

import settings  # noqa: F401  (loads .env)

SCANNER_DIR = Path(__file__).resolve().parent
TREND_DB = SCANNER_DIR / "trend_radar.db"
MFO_PREFIX = "https://mensfitnessonline.com.au"

TARGET_SLOTS = 4
RANK_BASE = 100
STRIKE_MIN, STRIKE_MAX = 4.5, 20.0
MIN_PAGE_IMPRESSIONS = 200
COOLDOWN_DAYS = 14
DOWNVOTE_COOLDOWN_DAYS = 60
RECENTLY_EDITED_DAYS = 14


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS evergreen_ideas (
            scan_id INTEGER NOT NULL, rank INTEGER NOT NULL, kind TEXT NOT NULL, idea_key TEXT NOT NULL,
            created_at TEXT NOT NULL, idea_json TEXT NOT NULL, PRIMARY KEY (scan_id, rank))"""
    )
    conn.commit()


def _norm_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()


def _topic_key(text: str | None) -> str:
    return "topic:" + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))[:60]


def _path(url: str) -> str:
    return url.replace(MFO_PREFIX, "") or "/"


# --- Sources ---------------------------------------------------------------

def find_refreshes(rows: list[dict], pages: list[dict], exclude: set[str], now: datetime) -> list[dict]:
    """Pages with the most impressions on queries ranking 5-20, best first.
    rows are GSC page+query rows; pages is the MFO index (for titles and
    last-modified dates)."""
    by_url = {_norm_url(p.get("url")): p for p in pages if isinstance(p, dict)}
    edited_cutoff = (now - timedelta(days=RECENTLY_EDITED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    striking: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        keys = row.get("keys") or []
        pos = row.get("position")
        if len(keys) == 2 and isinstance(pos, (int, float)) and STRIKE_MIN <= pos <= STRIKE_MAX:
            striking[keys[0]].append(row)
    # Which pages compete for the same query (split rankings).
    pages_for_query: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        keys = row.get("keys") or []
        if len(keys) == 2 and (row.get("impressions") or 0) >= 20:
            pages_for_query[keys[1]].add(keys[0])

    ideas = []
    for url, qrows in striking.items():
        key = _norm_url(url)
        page = by_url.get(key) or {}
        impressions = sum(r.get("impressions") or 0 for r in qrows)
        if key in exclude or impressions < MIN_PAGE_IMPRESSIONS:
            continue
        if (page.get("modified") or "") >= edited_cutoff:
            continue
        qrows.sort(key=lambda r: -(r.get("impressions") or 0))
        top = qrows[0]
        rivals = sorted(_path(u) for u in pages_for_query.get(top["keys"][1], set()) if _norm_url(u) != key)
        ideas.append({
            "kind": "refresh", "key": key, "url": url, "title": page.get("title") or _path(url),
            "impressions": impressions, "clicks": sum(r.get("clicks") or 0 for r in qrows),
            "queries": [{"query": r["keys"][1], "position": round(r["position"], 1),
                         "impressions": r.get("impressions") or 0} for r in qrows[:3]],
            "rivals": rivals[:2],
        })
    ideas.sort(key=lambda i: -i["impressions"])
    return ideas


def find_angles(scan: dict, exclude: set[str]) -> list[dict]:
    ideas = []
    for angle in scan.get("evergreen_angles") or []:
        key = _norm_url(angle.get("update_url")) or _topic_key(angle.get("topic"))
        if not angle.get("topic") or key in exclude:
            continue
        ideas.append({"kind": "angle", "key": key, "title": angle["topic"], "angle": angle.get("angle"),
                      "why_it_lasts": angle.get("why_it_lasts"), "url": angle.get("update_url")})
    return ideas


def _interleave(*lists: list[dict]) -> list[dict]:
    out, seen = [], set()
    for group in zip_longest(*lists):
        for idea in group:
            if idea and idea["key"] not in seen:
                seen.add(idea["key"])
                out.append(idea)
    return out


# --- Cooldown and storage -------------------------------------------------

def recent_keys(conn: sqlite3.Connection, now: datetime, scan_id) -> set[str]:
    """Ideas suggested in the last COOLDOWN_DAYS, plus 👎'd ones for longer."""
    ensure_tables(conn)
    from learning import ensure_tables as ensure_feedback

    ensure_feedback(conn)
    since = (now - timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    down_since = (now - timedelta(days=DOWNVOTE_COOLDOWN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    keys = {k for (k,) in conn.execute(
        "SELECT idea_key FROM evergreen_ideas WHERE created_at >= ? AND scan_id != ?", (since, scan_id))}
    keys |= {k for (k,) in conn.execute(
        """SELECT e.idea_key FROM evergreen_ideas e JOIN pick_feedback f
           ON f.scan_id = e.scan_id AND f.rank = e.rank
           WHERE f.vote = 'down' AND e.created_at >= ?""", (down_since,))}
    return keys


def load(scan_id, db_path: Path = TREND_DB) -> list[dict]:
    if scan_id is None or not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        ensure_tables(conn)
        rows = conn.execute("SELECT rank, idea_json FROM evergreen_ideas WHERE scan_id=? ORDER BY rank",
                            (scan_id,)).fetchall()
    finally:
        conn.close()
    return [{**json.loads(idea), "rank": rank} for rank, idea in rows]


def select(scan: dict, gsc: list[dict], pages: list[dict], exclude: set[str]) -> list[dict]:
    slots = max(0, TARGET_SLOTS - len(scan.get("candidates") or []))
    if not slots:
        return []
    now = datetime.now(timezone.utc)
    taken = exclude | {_norm_url(c.get("update_url")) for c in scan.get("candidates") or [] if c.get("update_url")}
    ideas = _interleave(find_refreshes(gsc, pages, taken, now), find_angles(scan, taken))
    return [{**idea, "rank": RANK_BASE + n} for n, idea in enumerate(ideas[:slots], 1)]


def compute(scan: dict, gsc: list[dict] | None, pages: list[dict], db_path: Path = TREND_DB,
            dry_run: bool = False) -> list[dict]:
    """Pick and store this scan's evergreen ideas (once per scan)."""
    scan_id = scan.get("_scan_id")
    existing = load(scan_id, db_path)
    if existing and not dry_run:
        return existing
    conn = sqlite3.connect(db_path)
    try:
        ideas = select(scan, gsc or [], pages, recent_keys(conn, datetime.now(timezone.utc), scan_id))
        if not dry_run:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.executemany(
                "INSERT OR REPLACE INTO evergreen_ideas (scan_id, rank, kind, idea_key, created_at, idea_json) "
                "VALUES (?,?,?,?,?,?)",
                [(scan_id, i["rank"], i["kind"], i["key"], now,
                  json.dumps({k: v for k, v in i.items() if k != "rank"})) for i in ideas])
            conn.commit()
    finally:
        conn.close()
    return ideas


def striking_rows(client) -> list[dict]:
    from seo_opportunity import gsc_rows

    return gsc_rows(client, dateRange="last_28_days", dimensions=["page", "query"], rowLimit=1000, minImpressions=20)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the ideas for the latest scan, store nothing")
    args = parser.parse_args()
    from editorial_today import latest_trend_scan
    from seo_opportunity import OpenSEOClient, load_mfo_pages

    scan = latest_trend_scan(TREND_DB)
    if not scan:
        print("no scan")
        return 1
    ideas = compute(scan, striking_rows(OpenSEOClient()), load_mfo_pages(), dry_run=args.dry_run)
    print(f"scan {scan.get('_scan_id')}: {len(scan.get('candidates') or [])} pick(s), {len(ideas)} evergreen idea(s)")
    for idea in ideas:
        print(f"  {idea['rank']} {idea['kind']}: {idea['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
