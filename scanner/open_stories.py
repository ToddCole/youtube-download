"""MFO's open stories: recent posts that promise a follow-up.

e.g. "We'll update this page with both results after the lifting." Each
open story (a) becomes a Google News search in the next collection (see
trend_radar/sources.py), so the follow-up evidence gets collected, and
(b) is listed for Claude in an OPEN MFO STORIES block, so a development
on it becomes an "update" pick pointing at that page.

Refreshed before each Claude scan by `learning.py --refresh-index`.
Manual:  python3 open_stories.py [--show]
"""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

SCANNER_DIR = Path(__file__).resolve().parent
OPEN_STORIES_PATH = SCANNER_DIR / "open_stories.json"
MFO_SITE = "https://mensfitnessonline.com.au"
LOOKBACK_DAYS = 30
MAX_STORIES = 12

PROMISE = re.compile(
    r"[^.!?]*\b("
    r"will (?:be )?update|we(?:'|’)ll update|update (?:this|the) (?:story|article|page|box)"
    r"|once (?:the )?results|results (?:are|will be) (?:in|known|posted|announced)"
    r"|stay tuned|yet to be (?:announced|confirmed)|pending confirmation|check back"
    r")\b[^.!?]*[.!?]",
    re.IGNORECASE,
)


def _text(rendered: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", rendered or ""))).strip()


# Longer "sentences" are run-together link lists (related posts), not promises.
MAX_PROMISE_CHARS = 200


def find_promise(text: str) -> str | None:
    for match in PROMISE.finditer(text or ""):
        sentence = match.group(0).strip()
        if len(sentence) <= MAX_PROMISE_CHARS:
            return sentence
    return None


def search_query(title: str) -> str:
    """A Google News query from a post title: drop punctuation and the
    explainer tail ("..., explained", "...: dates and ...")."""
    title = re.split(r"[:–—|]", title)[0]
    title = re.sub(r"[‘’'\"“”()?,!]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def fetch_recent_posts(now: datetime | None = None, days: int = LOOKBACK_DAYS) -> list[dict]:
    import scanner

    now = now or datetime.now(timezone.utc)
    after = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    params = urlencode({"per_page": 100, "after": after, "_fields": "date,link,title,content"})
    data = json.loads(scanner.http_get_text(f"{MFO_SITE}/wp-json/wp/v2/posts?{params}", timeout=30))
    return data if isinstance(data, list) else []


def find_open_stories(posts: list[dict]) -> list[dict]:
    stories = []
    for post in posts:
        promise = find_promise(_text((post.get("content") or {}).get("rendered", "")))
        if not promise:
            continue
        title = html.unescape((post.get("title") or {}).get("rendered", "")).strip()
        stories.append({
            "title": title,
            "url": post.get("link"),
            "date": (post.get("date") or "")[:10],
            "promise": promise,
            "query": search_query(title),
        })
    stories.sort(key=lambda s: s["date"], reverse=True)
    return stories[:MAX_STORIES]


def refresh(path: Path = OPEN_STORIES_PATH) -> list[dict]:
    stories = find_open_stories(fetch_recent_posts())
    path.write_text(json.dumps({
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stories": stories,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return stories


def load(path: Path = OPEN_STORIES_PATH) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("stories") or []
    except (OSError, ValueError):
        return []


def build_block(stories: list[dict]) -> str | None:
    if not stories:
        return None
    lines = [
        "OPEN MFO STORIES — MFO pages that promised a follow-up. If the evidence shows the promised",
        "development has happened (results, a confirmation, a new date), that is a strong pick: make it an",
        "\"update\" candidate with update_url set to the page below. If there's no evidence yet, leave it out.",
        "",
    ]
    for s in stories:
        lines.append(f"- {s['date']} {s['title']} — {s['url']}")
        lines.append(f"  Promise: \"{s['promise']}\"")
    return "\n".join(lines)


def main() -> int:
    stories = refresh() if "--show" not in sys.argv else load()
    print(build_block(stories) or "(no open stories)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
