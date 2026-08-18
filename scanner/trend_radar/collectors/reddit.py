"""Lane 2: Reddit. Phase 0 finding: anonymous access to Reddit's public
endpoints is real (a single request often succeeds) but rate-limit
fragile (429 within one or two subsequent requests), and Reddit's
official OAuth API is the only reliable production path -- registering
an app is an explicit external prerequisite this phase does not attempt.

This collector implements the anonymous path behind a clean interface so
it can be tested against captured fixtures, and to prove the graceful-
degradation behavior a credentialed version will also need: single
attempt, no retry loop, no backoff-and-retry after 429, informative
failure recorded and nothing more.

If REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET are unset (the default), the
lane is treated as unavailable rather than attempting anonymous
collection at all -- Phase 1 must not depend on Reddit access to
function, and repeated anonymous requests across many configured
subreddits in one run is exactly the pattern Phase 0 showed gets rate
limited fastest.
"""

from __future__ import annotations

import os
import sqlite3
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .._scanner_bridge import scanner
from ..models import RawSignalInput

USER_AGENT = "Mozilla/5.0 (compatible; MFOTrendRadar/0.1; +research)"


class RedditUnavailable(Exception):
    """Lane is not usable right now (no credentials, or anonymous access
    was blocked). Never retried within the same collection attempt."""


class RedditRateLimited(RedditUnavailable):
    """HTTP 429. Recorded as a normal per-source error; no retry, no
    backoff loop, no credential workaround attempted."""


def has_oauth_credentials() -> bool:
    return bool(os.environ.get("REDDIT_CLIENT_ID")) and bool(os.environ.get("REDDIT_CLIENT_SECRET"))


def _default_http_get(url: str) -> tuple[int, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, ""


def fetch_reddit_source(source_row: sqlite3.Row, http_get=None) -> list[RawSignalInput]:
    if not has_oauth_credentials():
        raise RedditUnavailable(
            "Reddit OAuth credentials not configured (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET unset); "
            "anonymous access is unreliable per Phase 0 findings and is not used for scheduled collection"
        )

    # OAuth-authenticated collection is not implemented in Phase 1 -- only
    # the credential gate above and the anonymous single-attempt fallback
    # below exist yet. This branch is reachable once credentials exist,
    # as a documented seam for the real OAuth client, not live today.
    http_get = http_get or _default_http_get
    subreddit = source_row["canonical_identifier"]
    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    status, body = http_get(url)

    if status == 429:
        raise RedditRateLimited(f"rate limited (HTTP 429): {url}")
    if status == 403:
        raise RedditUnavailable(f"anonymous access forbidden (HTTP 403): {url}")
    if status != 200:
        raise RedditUnavailable(f"unexpected HTTP {status}: {url}")

    items = scanner.rss_items_from_xml(body, source_row["name"], "reddit")
    return [
        RawSignalInput(
            source_id=source_row["source_id"],
            lane=source_row["lane"],
            platform="reddit",
            source_name=source_row["name"],
            source_role=source_row["source_role"],
            url=item.url,
            title=item.title,
            body_text=item.summary or "",
            author_or_account=item.source,
            published_at=item.published,
        )
        for item in items
    ]
