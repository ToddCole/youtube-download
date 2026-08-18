"""Lane 1: fitness publisher RSS/Atom feeds, plus the permanent broad
Google News RSS queries proven in Phase 0B. Both platforms reuse
scanner.py's http_get_text() and rss_items_from_xml() unchanged -- the
latter is a generic, dependency-free RSS 2.0/Atom parser with no
Google-News-specific coupling, confirmed during Phase 0 research.

Google News sources are discovery feeds, not evidence authorities: they
widen what gets ingested, but the article itself (once followed) is the
real evidence, same as any other publisher pickup.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import quote_plus

from .._scanner_bridge import scanner
from ..models import RawSignalInput


def _item_to_signal(item, source_row: sqlite3.Row) -> RawSignalInput:
    return RawSignalInput(
        source_id=source_row["source_id"],
        lane=source_row["lane"],
        platform=source_row["platform"],
        source_name=source_row["name"],
        source_role=source_row["source_role"],
        url=item.url,
        title=item.title,
        body_text=item.summary or "",
        author_or_account=item.source,
        published_at=item.published,
    )


def fetch_rss_source(source_row: sqlite3.Row) -> list[RawSignalInput]:
    url = source_row["canonical_identifier"]
    text = scanner.http_get_text(url, timeout=15)
    items = scanner.rss_items_from_xml(text, source_row["name"], "rss")
    return [_item_to_signal(item, source_row) for item in items]


def fetch_google_news_source(source_row: sqlite3.Row) -> list[RawSignalInput]:
    query = source_row["canonical_identifier"]
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-AU&gl=AU&ceid=AU:en"
    text = scanner.http_get_text(url, timeout=15)
    items = scanner.rss_items_from_xml(text, source_row["name"], "google-news")
    return [_item_to_signal(item, source_row) for item in items]


def fetch_for(source_row: sqlite3.Row) -> list[RawSignalInput]:
    if source_row["platform"] == "google-news":
        return fetch_google_news_source(source_row)
    return fetch_rss_source(source_row)
