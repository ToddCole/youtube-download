"""What the digest already sent the editor, so the same story isn't sent
again day after day.

Every item the digest sends (news pick, fit-food pick, evergreen idea,
rising search) is logged with its headline and URLs. For the next
REPEAT_DAYS days an item matching a logged one (a shared non-MFO source
URL, the same MFO page for a refresh or rising search, or a near-identical
headline) is dropped from the digest, and the scans are shown the list so
they only bring a story back with a genuinely new development."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

REPEAT_DAYS = 7
HEADLINE_MATCH = 0.5
MFO_PREFIX = "https://mensfitnessonline.com.au"

_STOP = {"the", "and", "for", "with", "you", "your", "what", "how", "why", "this", "that", "are", "was",
         "from", "into", "more", "after", "says", "new", "its", "his", "her", "who", "can", "does", "about"}


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sent_at TEXT NOT NULL,
        kind TEXT NOT NULL,
        headline TEXT NOT NULL,
        urls_json TEXT NOT NULL)""")


def norm_url(url: str | None) -> str:
    return (url or "").strip().split("#")[0].rstrip("/").lower()


def words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def _similar(a: str, b: str) -> bool:
    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= HEADLINE_MATCH


def item(kind: str, headline: str | None, *urls: str | None) -> dict:
    return {"kind": kind, "headline": (headline or "").strip(), "urls": [u for u in urls if u]}


def _match_urls(it: dict) -> set[str]:
    """URLs that identify the story. MFO pages only count for refreshes and
    rising searches (the page *is* the item); a news or fit-food pick that
    suggests updating an MFO page can be a different story on the same page."""
    keep_mfo = it.get("kind") in ("refresh", "rising")
    return {norm_url(u) for u in it.get("urls") or []
            if keep_mfo or not norm_url(u).startswith(MFO_PREFIX)}


def is_repeat(it: dict, recent: list[dict]) -> dict | None:
    urls = _match_urls(it)
    for old in recent:
        if urls & _match_urls(old):
            return old
        if it.get("kind") not in ("refresh", "rising") and _similar(it.get("headline"), old.get("headline")):
            return old
    return None


def recent(conn: sqlite3.Connection, days: int = REPEAT_DAYS, now: datetime | None = None) -> list[dict]:
    ensure_tables(conn)
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"sent_at": s, "kind": k, "headline": h, "urls": json.loads(u)}
            for s, k, h, u in conn.execute(
                "SELECT sent_at, kind, headline, urls_json FROM sent_items WHERE sent_at >= ? ORDER BY id DESC",
                (since,))]


def record(conn: sqlite3.Connection, items: list[dict], now: datetime | None = None) -> None:
    ensure_tables(conn)
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.executemany("INSERT INTO sent_items (sent_at, kind, headline, urls_json) VALUES (?,?,?,?)",
                     [(when, i["kind"], i["headline"], json.dumps(i["urls"])) for i in items])
    conn.commit()


def prompt_block(conn: sqlite3.Connection, now: datetime | None = None) -> str | None:
    """The list the scans see. None when nothing was sent recently."""
    rows = [r for r in recent(conn, now=now) if r["kind"] not in ("refresh", "rising")]
    if not rows:
        return None
    lines = [f"ALREADY SENT TO THE EDITOR IN THE LAST {REPEAT_DAYS} DAYS (newest first). Do not pick these "
             "stories again, in any form (news pick, fit-food pick, evergreen angle, add to existing post). "
             "Bring one back only for a material new development since it was sent (a new paper, ruling, "
             "result or statement), lead with that development, and say in the pick what is new and when it "
             "happened. The same story with a fresh outlet or a new headline is not new."]
    seen = set()
    for r in rows:
        key = r["headline"].lower()
        if key in seen:
            continue
        seen.add(key)
        url = next((u for u in r["urls"] if not norm_url(u).startswith(MFO_PREFIX)), "")
        lines.append(f"- {r['sent_at'][:10]} [{r['kind']}] {r['headline']}" + (f" | {url[:140]}" if url else ""))
    return "\n".join(lines)
