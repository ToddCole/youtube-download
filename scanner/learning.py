"""The scanner's memory: what the editor actually did with past picks.

Two signals, fed into every Claude scan as an EDITOR FEEDBACK block:
  1. What got published on MFO (WordPress archive index) and how it is doing
     in Google (GSC via OpenSEO). No effort from Todd.
  2. 👍 / 👎 taps on the phone digest. ntfy buttons POST to a private
     feedback topic; each cycle polls it (ntfy keeps messages ~12h, the
     cycle runs every 3h).

Manual:  python3 learning.py [--poll] [--refresh-index] [--show]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import settings  # noqa: F401  (loads .env)

SCANNER_DIR = Path(__file__).resolve().parent
TREND_DB = SCANNER_DIR / "trend_radar.db"
MFO_INDEX = SCANNER_DIR / "mfo_index.json"
MFO_SITE = "https://mensfitnessonline.com.au"
LOOKBACK_DAYS = 14
MAX_PICKS = 40
MAX_POSTS = 40
VOTES = {"up": "👍", "down": "👎"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pick_feedback (
            msg_id TEXT PRIMARY KEY,
            scan_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            vote TEXT NOT NULL,
            source TEXT NOT NULL,
            at TEXT NOT NULL
        )"""
    )
    conn.commit()


def feedback_topic() -> str | None:
    topic = os.environ.get("NTFY_FEEDBACK_TOPIC")
    if topic:
        return topic
    main = os.environ.get("NTFY_TOPIC")
    return f"{main}-fb" if main else None


def ntfy_server() -> str:
    return os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def vote_body(vote: str, scan_id: int, rank: int) -> str:
    return f"{vote} {scan_id} {rank}"


def vote_tag(vote: str, scan_id: int, rank: int) -> str:
    return f"vote-{vote}-{scan_id}-{rank}"


def parse_vote_tags(tags: list[str] | None) -> tuple[str, int, int] | None:
    for tag in tags or []:
        parts = tag.split("-")
        if len(parts) == 4 and parts[0] == "vote" and parts[1] in VOTES:
            try:
                return parts[1], int(parts[2]), int(parts[3])
            except ValueError:
                return None
    return None


def parse_vote(body: str) -> tuple[str, int, int] | None:
    parts = (body or "").split()
    if len(parts) != 3 or parts[0] not in VOTES:
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        return None


def record_vote(conn, vote: str, scan_id: int, rank: int, source: str, msg_id: str | None = None) -> bool:
    """Latest vote per pick wins (a 👎 after a 👍 is a change of mind)."""
    ensure_tables(conn)
    msg_id = msg_id or f"{source}:{scan_id}:{rank}:{utc_now()}"
    if conn.execute("SELECT 1 FROM pick_feedback WHERE msg_id=?", (msg_id,)).fetchone():
        return False
    conn.execute(
        "INSERT INTO pick_feedback (msg_id, scan_id, rank, vote, source, at) VALUES (?,?,?,?,?,?)",
        (msg_id, scan_id, rank, vote, source, utc_now()),
    )
    conn.commit()
    return True


def poll_feedback(conn) -> int:
    """Votes arrive as tagged "✅ Saved" messages in the main topic (current
    buttons) or as "up S R" bodies in the -fb topic (first digests)."""
    import ssl

    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    added = 0
    for topic in filter(None, {os.environ.get("NTFY_TOPIC"), feedback_topic()}):
        request = Request(f"{ntfy_server()}/{topic}/json?poll=1&since=24h", headers={"User-Agent": "mfo-editorial-desk/1.0"})
        with urlopen(request, timeout=30, context=context) as response:
            lines = response.read().decode("utf-8", errors="replace").splitlines()
        for line in lines:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("event") != "message":
                continue
            parsed = parse_vote_tags(message.get("tags")) or parse_vote(message.get("message", ""))
            if parsed:
                added += record_vote(conn, *parsed, source="phone", msg_id=message.get("id"))
    return added


def latest_votes(conn) -> dict[tuple[int, int], str]:
    ensure_tables(conn)
    votes = {}
    for scan_id, rank, vote in conn.execute("SELECT scan_id, rank, vote FROM pick_feedback ORDER BY at"):
        votes[(scan_id, rank)] = vote
    return votes


def refresh_index() -> int:
    import scanner

    index = scanner.refresh_mfo_index(MFO_SITE, MFO_INDEX)
    return len(index.get("pages") or [])


# --- The feedback block ------------------------------------------------------

def recent_picks(conn, since: datetime) -> list[dict]:
    from trend_radar.scan_store import ensure_tables as ensure_scans

    ensure_scans(conn)
    try:
        from seo_opportunity import ensure_tables as ensure_seo

        ensure_seo(conn)
        has_seo = True
    except Exception:  # noqa: BLE001
        has_seo = False
    votes = latest_votes(conn)
    picks, seen = [], set()
    rows = conn.execute("SELECT id, generated_at, result_json FROM claude_scans ORDER BY id DESC").fetchall()
    for scan_id, generated_at, result_json in rows:
        try:
            when = datetime.strptime((generated_at or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if when < since:
            break
        verdicts = {}
        if has_seo:
            for rank, score in conn.execute("SELECT rank, score_json FROM seo_scores WHERE scan_id=?", (scan_id,)):
                verdicts[rank] = json.loads(score).get("label")
        for c in json.loads(result_json).get("candidates") or []:
            headline = (c.get("headline") or "").strip()
            key = headline.lower()[:60]
            vote = votes.get((scan_id, c.get("rank")))
            if not headline or (key in seen and not vote):
                continue
            seen.add(key)
            picks.append({"date": when.date().isoformat(), "headline": headline,
                          "seo": verdicts.get(c.get("rank")), "vote": vote})
    return picks[:MAX_PICKS]


def published_posts(since: datetime, index_path: Path = MFO_INDEX) -> list[dict]:
    try:
        pages = json.loads(index_path.read_text(encoding="utf-8")).get("pages") or []
    except (OSError, ValueError):
        return []
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%S")
    posts = [p for p in pages if isinstance(p, dict) and (p.get("date") or "") >= cutoff]
    posts.sort(key=lambda p: p.get("date") or "", reverse=True)
    return [{"date": (p.get("date") or "")[:10], "title": p.get("title") or p.get("slug"), "url": p.get("url")}
            for p in posts[:MAX_POSTS]]


def page_traffic() -> dict[str, dict]:
    """GSC clicks/impressions per page, last 28 days (free via OpenSEO)."""
    if not os.environ.get("OPENSEO_API_KEY"):
        return {}
    from seo_opportunity import OpenSEOClient, gsc_rows

    rows = gsc_rows(OpenSEOClient(), dateRange="last_28_days", dimensions=["page"], rowLimit=1000)
    return {(r.get("keys") or [""])[0].rstrip("/"): r for r in rows}


def build_feedback(conn, *, now: datetime | None = None, traffic: dict | None = None,
                   index_path: Path = MFO_INDEX, open_stories_path: Path | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    import open_stories

    open_block = open_stories.build_block(open_stories.load(open_stories_path or open_stories.OPEN_STORIES_PATH))
    picks = recent_picks(conn, since)
    posts = published_posts(since, index_path)
    if not picks and not posts:
        return open_block
    if traffic is None:
        try:
            traffic = page_traffic()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: no GSC traffic for feedback: {exc}", file=sys.stderr)
            traffic = {}

    lines = [
        "EDITOR FEEDBACK — learn the MFO editor's taste from what the editor actually did with recent picks.",
        "",
        f"Published on MFO in the last {LOOKBACK_DAYS} days (the editor's real choices; Google numbers are last 28 days):",
    ]
    for p in posts:
        t = traffic.get((p.get("url") or "").rstrip("/"))
        perf = f" — {t.get('clicks', 0)} clicks, {t.get('impressions', 0)} impressions" if t else ""
        url = f" — {p['url']}" if p.get("url") else ""
        lines.append(f"- {p['date']} {p['title']}{url}{perf}")
    if not posts:
        lines.append("- (nothing published)")
    lines += ["", "Your recent picks, newest first, with the editor's reaction:"]
    for p in picks:
        mark = f" [{VOTES[p['vote']]} editor]" if p.get("vote") else ""
        seo = f" ({p['seo']})" if p.get("seo") else ""
        lines.append(f"- {p['date']} {p['headline']}{seo}{mark}")
    if not picks:
        lines.append("- (no earlier picks)")
    lines += [
        "",
        "How to use this:",
        "- A pick that matches a published post, or got 👍, is the kind of story to find more of.",
        "- 👎 means avoid that kind of story (topic, angle or story type).",
        "- Picks more than 3 days old that were neither published nor 👍 are weak evidence of low interest.",
        "- Published posts you never picked are misses: look harder for that kind of story.",
        "- Posts with strong Google numbers show which kinds of stories pay off.",
        "- Never re-pick a story MFO has already published; a genuinely new development is a follow-up, say so.",
        "- Taste never overrides evidence: every pick still needs the cited signals.",
    ]
    if open_block:
        lines += ["", open_block]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--poll", action="store_true", help="pull 👍/👎 taps from ntfy")
    parser.add_argument("--refresh-index", action="store_true", help="re-read MFO's published posts")
    parser.add_argument("--show", action="store_true", help="print the feedback block Claude will see")
    args = parser.parse_args()
    conn = sqlite3.connect(TREND_DB)
    ensure_tables(conn)
    status = 0
    if args.refresh_index:
        try:
            print(f"MFO index: {refresh_index()} pages")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: index refresh failed: {exc}", file=sys.stderr)
            status = 2
        try:
            import open_stories

            print(f"open stories: {len(open_stories.refresh())}")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: open stories refresh failed: {exc}", file=sys.stderr)
            status = 2
    if args.poll:
        try:
            print(f"feedback: {poll_feedback(conn)} new vote(s)")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: feedback poll failed: {exc}", file=sys.stderr)
            status = 2
    if args.show:
        print(build_feedback(conn) or "(no feedback yet)")
    conn.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
