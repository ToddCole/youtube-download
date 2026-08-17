#!/usr/bin/env python3
"""Narrow YouTube discovery scanner for fast-moving source videos."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import ssl
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from email.utils import parsedate_to_datetime
from urllib.error import URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import yt_dlp

import editorial_gates


BASE_DIR = Path(__file__).resolve().parent
CHANNELS_PATH = BASE_DIR / "channels.json"
SOURCE_PROFILES_PATH = BASE_DIR / "editorial_sources.json"
MFO_INDEX_PATH = BASE_DIR / "mfo_index.json"
DB_PATH = BASE_DIR / "scanner.db"
REPORT_PATH = BASE_DIR / "reports" / "latest.md"
NEWS_REPORT_PATH = BASE_DIR / "reports" / "news-latest.md"
RESEARCH_REPORT_PATH = BASE_DIR / "reports" / "research-latest.md"
CREATOR_JSON_PATH = BASE_DIR / "reports" / "latest.json"
NEWS_JSON_PATH = BASE_DIR / "reports" / "news-latest.json"
RESEARCH_JSON_PATH = BASE_DIR / "reports" / "research-latest.json"
NEWS_SOURCES_PATH = BASE_DIR / "news_sources.json"
NEWS_QUERIES_PATH = BASE_DIR / "news_queries.json"
RESEARCH_QUERIES_PATH = BASE_DIR / "research_queries.json"
EDITORIAL_GATES_CONFIG_PATH = BASE_DIR / "editorial_gates_config.json"
_GATES_CONFIG_CACHE: dict[str, Any] | None = None


def gates_config() -> dict[str, Any]:
    """Process-wide cached editorial_gates config; avoids re-reading the
    config file on every development_type()/find_overlap() call."""
    global _GATES_CONFIG_CACHE
    if _GATES_CONFIG_CACHE is None:
        _GATES_CONFIG_CACHE = editorial_gates.load_config(EDITORIAL_GATES_CONFIG_PATH)
    return _GATES_CONFIG_CACHE


VIDEOS_PER_CHANNEL = 5
SHORTS_MAX_SECONDS = 180
MFO_SITE_URL = "https://mensfitnessonline.com.au"
MFO_INDEX_MAX_AGE_HOURS = 24
USER_AGENT = "MFO YouTube Scanner/1.0 (+https://mensfitnessonline.com.au)"
NCBI_TOOL_NAME = "mfo-editorial-scanner"
STOPWORDS = {
    "about",
    "after",
    "again",
    "and",
    "are",
    "best",
    "but",
    "can",
    "did",
    "does",
    "for",
    "from",
    "get",
    "has",
    "his",
    "how",
    "into",
    "its",
    "new",
    "not",
    "off",
    "out",
    "the",
    "this",
    "video",
    "was",
    "what",
    "when",
    "why",
    "with",
    "workout",
    "you",
    "your",
}
OVERLAP_STOPWORDS = (
    STOPWORDS
    | editorial_gates.recent_years()
    | {
        "fitness",
        "health",
        "journal",
        "media",
        "men",
        "muscle",
        "new",
        "release",
        "review",
        "research",
        "science",
        "study",
        "training",
        "strength",
        "exercise",
    }
    | set(gates_config().get("overlap_stopwords_extra", []))
)
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})"
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
PRESS_RELEASE_DOMAINS = {
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "newswire.com",
}
NEWS_TERMS = {
    "announce",
    "announced",
    "announcement",
    "ban",
    "banned",
    "correction",
    "documentary",
    "erratum",
    "injury",
    "launch",
    "launched",
    "record",
    "recall",
    "release",
    "released",
    "research",
    "reissue",
    "result",
    "results",
    "retire",
    "retirement",
    "reveals",
    "report",
    "signing",
    "suspended",
    "suspension",
    "study",
    "retraction",
}
FEMALE_PROFILE_TERMS = {
    "woman",
    "women",
    "female",
    "girl",
    "girls",
    "mother",
    "mum",
    "grandmother",
    "grandma",
    "supergran",
    "her",
    "she",
}
MAJOR_NEWS_TERMS = {
    "world championship",
    "olympic",
    "games champion",
    "banned",
    "suspended",
    "death",
    "lawsuit",
}
MEN_TRAINING_TERMS = {
    "testosterone",
    "prostate",
    "strength training",
    "muscle",
    "hypertrophy",
    "longevity",
}
UNIVERSAL_FINDING_TERMS = {
    "study",
    "research",
    "meta-analysis",
    "systematic review",
    "trial",
    "guideline",
    "consensus",
}


@dataclass
class Observation:
    channel_source: str
    channel_name: str
    video_title: str
    video_url: str
    video_id: str
    upload_datetime: str | None
    view_count: int
    duration_seconds: int | None
    video_type: str
    scan_timestamp: str
    age_hours: float | None
    total_views_per_hour: float | None
    views_gained: int | None = None
    observed_hourly_growth: float | None = None
    breakout_score: float | None = None
    breakout_confidence: str = "pending"
    breakout_comparison_count: int = 0
    breakout_expected_views_at_current_age: float | None = None
    comparison_hours: float | None = None
    growth_signal: str = "baseline pending"
    status: str = "ok"
    error: str | None = None


@dataclass
class MfoPage:
    title: str
    url: str
    slug: str
    date: str | None = None
    modified: str | None = None
    source_urls: list[str] | None = None
    youtube_ids: list[str] | None = None
    pmids: list[str] | None = None
    dois: list[str] | None = None
    source_fingerprints: list[str] | None = None
    primary_creator_key: str | None = None


@dataclass
class Overlap:
    score: float
    page: MfoPage | None
    shared_terms: list[str]


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: str | None
    summary: str = ""
    source_type: str = "rss"


@dataclass
class NewsCluster:
    key: str
    items: list[NewsItem]
    score: int = 0
    penalties: list[str] | None = None
    overlap: Overlap | None = None
    score_json: dict[str, Any] | None = None


@dataclass
class ResearchPaper:
    pmid: str
    doi: str | None
    topic_group: str
    title: str
    journal: str | None
    authors: list[str]
    abstract: str | None
    publication_date: str | None
    electronic_publication_date: str | None
    indexed_at: str | None
    publication_types: list[str]
    study_population: str | None = None
    sample_size: str | None = None
    intervention: str | None = None
    comparison: str | None = None
    duration: str | None = None
    primary_finding: str | None = None
    effect_size: str | None = None
    funding: str | None = None
    conflicts: str | None = None
    full_text_available: bool | None = None
    pubmed_url: str | None = None
    publisher_url: str | None = None
    score: int = 0
    score_breakdown: dict[str, int] | None = None
    penalties: list[str] | None = None
    status: str = "rejected"
    recommended_status: str = "reject"
    archive_overlap: Overlap | None = None
    public_interest: dict[str, Any] | None = None
    rejection_reasons: list[str] | None = None
    extraction_warnings: list[str] | None = None


class QuietYtdlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_upload_datetime(info: dict[str, Any]) -> str | None:
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if timestamp:
        try:
            return iso(datetime.fromtimestamp(int(timestamp), timezone.utc))
        except (TypeError, ValueError, OSError):
            pass

    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00Z"
    return None


def normalize_channel_source(source: str) -> str:
    source = source.strip()
    if not source:
        raise ValueError("channel source is blank")
    if source.startswith("@"):
        return f"https://www.youtube.com/{source}/videos"
    if source.startswith("http://") or source.startswith("https://"):
        cleaned = source.rstrip("/")
        if "youtube.com" in cleaned and not cleaned.endswith(("/videos", "/shorts", "/streams")):
            return f"{cleaned}/videos"
        return cleaned
    return f"https://www.youtube.com/@{source.lstrip('@')}/videos"


def load_channels(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Add 15-25 YouTube channel URLs or handles.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("channels.json must be a JSON array of channel URLs or handles.")
    channels = [item.strip() for item in data if isinstance(item, str) and item.strip()]
    if not channels:
        raise ValueError("channels.json does not contain any channel URLs or handles.")
    if not 15 <= len(channels) <= 25:
        print(
            f"Warning: channels.json contains {len(channels)} channels; target range is 15-25.",
            file=sys.stderr,
        )
    return channels


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object.")
    return data


def load_source_profiles(path: Path) -> dict[str, dict[str, str]]:
    raw = load_json_object(path)
    profiles: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            profiles[key.lower()] = {str(k): str(v) for k, v in value.items()}
    return profiles


def profile_for(obs: Observation, profiles: dict[str, dict[str, str]]) -> dict[str, str]:
    keys = {
        obs.channel_source.lower(),
        obs.channel_name.lower(),
        obs.channel_name.replace(" ", "").lower(),
    }
    for key in keys:
        if key in profiles:
            return profiles[key]
    return {
        "category": "fitness lead",
        "mfo_fit": "Needs editor review",
        # Deliberately avoids the literal words "Australia"/"Australian": this
        # boilerplate is applied to every unprofiled channel, and several
        # scoring/eligibility checks look for those words in the combined
        # title+profile text as a signal of genuine Australian relevance —
        # baking them into generic fallback copy would make every unprofiled
        # candidate falsely register as strongly Australia-relevant.
        "default_angle": "Assess whether the source claim is useful, true and practical for this MFO audience (men 35-65).",
        "default_value_add": "Add context, verification, practical takeaways and clear caveats.",
        "default_weakness": "Audience demand or local relevance may be too thin once the video is checked.",
    }


def http_get(url: str, timeout: int = 20) -> tuple[str, dict[str, str]]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/xml,text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace"), dict(response.headers)
    except URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        context = ssl._create_unverified_context()
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.read().decode("utf-8", errors="replace"), dict(response.headers)


def http_get_text(url: str, timeout: int = 20) -> str:
    text, _headers = http_get(url, timeout=timeout)
    return text


def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def slug_to_title(url: str) -> str:
    slug = urlparse(url).path.strip("/").split("/")[-1]
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip().title()


def clean_url(url: str) -> str:
    parsed = urlparse(html.unescape(url).strip().rstrip(").,]"))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return parsed._replace(fragment="").geturl().rstrip("/")


def canonical_url(url: str) -> str:
    cleaned = clean_url(url)
    if not cleaned:
        return ""
    parsed = urlparse(unwrap_redirect_url(cleaned))
    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query_pairs = []
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        key_lower = key.lower()
        if key_lower.startswith("utm_") or key_lower in {"fbclid", "gclid", "oc", "ceid", "hl", "gl"}:
            continue
        for value in values:
            query_pairs.append((key_lower, value))
    query = urlencode(sorted(query_pairs))
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlparse("")._replace(scheme=scheme, netloc=host, path=path, query=query).geturl()


def unwrap_redirect_url(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ("url", "u", "q", "target", "destination"):
        value = params.get(key, [""])[0]
        if value.startswith("http"):
            return clean_url(unquote(value)) or value
    return url


def youtube_id_from_url(url: str) -> str | None:
    match = YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None


def extract_pmids(text: str) -> list[str]:
    pmids = set(re.findall(r"(?:PMID[:\s]*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{4,10})", text, re.I))
    return sorted(pmids)


def extract_dois(text: str) -> list[str]:
    dois = {doi.rstrip(".,);]").lower() for doi in DOI_RE.findall(text or "")}
    return sorted(dois)


def fingerprints_for_values(*values: str | None, pmid: str | None = None, doi: str | None = None) -> set[str]:
    fingerprints: set[str] = set()
    if pmid:
        fingerprints.add(f"pmid:{pmid}")
    if doi:
        fingerprints.add(f"doi:{doi.lower().rstrip('.,);]')}")
        fingerprints.add(f"url:{canonical_url(f'https://doi.org/{doi}')}")
    for value in values:
        if not value:
            continue
        text = str(value)
        for video_id in extract_youtube_ids(text):
            fingerprints.add(f"youtube:{video_id}")
        for item_pmid in extract_pmids(text):
            fingerprints.add(f"pmid:{item_pmid}")
        for item_doi in extract_dois(text):
            fingerprints.add(f"doi:{item_doi}")
            fingerprints.add(f"url:{canonical_url(f'https://doi.org/{item_doi}')}")
        for url in extract_source_urls(text):
            canonical = canonical_url(url)
            if canonical:
                fingerprints.add(f"url:{canonical}")
            video_id = youtube_id_from_url(url)
            if video_id:
                fingerprints.add(f"youtube:{video_id}")
    return {item for item in fingerprints if item and not item.endswith(":")}


def page_fingerprints(page: MfoPage) -> set[str]:
    if page.source_fingerprints:
        return set(page.source_fingerprints)
    values = [page.url, page.title, page.slug, *(page.source_urls or [])]
    fingerprints = fingerprints_for_values(*values)
    for video_id in page.youtube_ids or []:
        fingerprints.add(f"youtube:{video_id}")
    for pmid in page.pmids or []:
        fingerprints.add(f"pmid:{pmid}")
    for doi in page.dois or []:
        fingerprints.add(f"doi:{doi.lower()}")
    return fingerprints


def extract_source_urls(content: str) -> list[str]:
    urls = {clean_url(url) for url in URL_RE.findall(content)}
    return sorted(url for url in urls if url)


def extract_youtube_ids(content: str) -> list[str]:
    ids = {match.group(1) for match in YOUTUBE_ID_RE.finditer(content)}
    return sorted(ids)


def index_payload(pages: list[MfoPage], site_url: str, source: str) -> dict[str, Any]:
    for page in pages:
        page.source_fingerprints = sorted(page_fingerprints(page))
    return {
        "site_url": site_url.rstrip("/"),
        "source": source,
        "refreshed_at": iso(utc_now()),
        "archive_warning": "",
        "source_fingerprint_count": len({fingerprint for page in pages for fingerprint in page_fingerprints(page)}),
        "pages": [page.__dict__ for page in pages],
    }


def fetch_wordpress_posts(site_url: str, creator_lexicon: dict[str, list[str]] | None = None) -> list[MfoPage]:
    pages: list[MfoPage] = []
    total_pages: int | None = None
    page_num = 1
    while total_pages is None or page_num <= total_pages:
        api_url = (
            f"{site_url.rstrip('/')}/wp-json/wp/v2/posts"
            f"?per_page=100&page={page_num}&_fields=link,slug,title,date,modified,content,excerpt"
        )
        try:
            text, headers = http_get(api_url)
            posts = json.loads(text)
            total_header = headers.get("X-WP-TotalPages") or headers.get("x-wp-totalpages")
            if total_header:
                total_pages = int(total_header)
        except Exception:
            if page_num == 1:
                raise
            break
        if not isinstance(posts, list) or not posts:
            break
        for post in posts:
            if not isinstance(post, dict):
                continue
            rendered = post.get("title", {}).get("rendered", "") if isinstance(post.get("title"), dict) else ""
            content = post.get("content", {}).get("rendered", "") if isinstance(post.get("content"), dict) else ""
            excerpt = post.get("excerpt", {}).get("rendered", "") if isinstance(post.get("excerpt"), dict) else ""
            url = str(post.get("link") or "")
            searchable = " ".join([rendered, content, excerpt, url])
            slug = str(post.get("slug") or urlparse(url).path.strip("/").split("/")[-1])
            if url:
                pages.append(
                    MfoPage(
                        title=strip_html(rendered) or slug_to_title(url),
                        url=url,
                        slug=slug,
                        date=post.get("date"),
                        modified=post.get("modified"),
                        source_urls=extract_source_urls(content),
                        youtube_ids=extract_youtube_ids(content),
                        pmids=extract_pmids(searchable),
                        dois=extract_dois(searchable),
                        primary_creator_key=editorial_gates.match_creator_in_text(
                            strip_html(f"{rendered} {content}"), creator_lexicon
                        ) if creator_lexicon else None,
                    )
                )
        page_num += 1
    return pages


def parse_sitemap_urls(xml_text: str) -> tuple[list[str], list[str]]:
    root = ElementTree.fromstring(xml_text)
    urls: list[str] = []
    sitemaps: list[str] = []
    for element in root.iter():
        if element.tag.endswith("loc") and element.text:
            loc = element.text.strip()
            if loc.endswith(".xml"):
                sitemaps.append(loc)
            else:
                urls.append(loc)
    return urls, sitemaps


def fetch_sitemap_posts(site_url: str, max_sitemaps: int = 12) -> list[MfoPage]:
    seen: set[str] = set()
    pages: list[MfoPage] = []
    queue = [urljoin(site_url.rstrip("/") + "/", "sitemap.xml")]
    while queue and len(seen) < max_sitemaps:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)
        xml_text = http_get_text(sitemap_url)
        urls, nested = parse_sitemap_urls(xml_text)
        queue.extend([url for url in nested if url not in seen])
        for url in urls:
            path = urlparse(url).path.strip("/")
            if not path or any(part in path for part in ("/tag/", "/category/", "wp-content")):
                continue
            pages.append(MfoPage(title=slug_to_title(url), url=url, slug=path.split("/")[-1], source_urls=[], youtube_ids=[], pmids=[], dois=[]))
    return pages


def refresh_mfo_index(site_url: str, index_path: Path, creator_lexicon: dict[str, list[str]] | None = None) -> dict[str, Any]:
    try:
        pages = fetch_wordpress_posts(site_url, creator_lexicon)
        source = "wordpress-rest"
    except Exception:
        pages = fetch_sitemap_posts(site_url)
        source = "sitemap"
    payload = index_payload(pages, site_url, source)
    index_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def load_mfo_index(index_path: Path, site_url: str, refresh: bool = False, creator_lexicon: dict[str, list[str]] | None = None) -> dict[str, Any]:
    if refresh or not index_path.exists():
        try:
            return refresh_mfo_index(site_url, index_path, creator_lexicon)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"Warning: could not refresh MFO index: {exc}", file=sys.stderr)
            return {
                "site_url": site_url.rstrip("/"),
                "source": "unavailable",
                "refreshed_at": None,
                "archive_warning": f"MFO archive refresh failed and no cache was available: {exc}",
                "source_fingerprint_count": 0,
                "pages": [],
            }

    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return load_mfo_index(index_path, site_url, refresh=True)

    refreshed = parse_dt(payload.get("refreshed_at"))
    if refreshed and utc_now() - refreshed > timedelta(hours=MFO_INDEX_MAX_AGE_HOURS):
        try:
            return refresh_mfo_index(site_url, index_path, creator_lexicon)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"Warning: using stale MFO index after refresh failed: {exc}", file=sys.stderr)
            payload["archive_warning"] = f"Using cached MFO archive after refresh failed: {exc}"
            payload["source"] = f"{payload.get('source', 'cache')}-cached"
    payload.setdefault("archive_warning", "")
    payload.setdefault(
        "source_fingerprint_count",
        len({fingerprint for page in mfo_pages_from_index(payload) for fingerprint in page_fingerprints(page)}),
    )
    return payload if isinstance(payload, dict) else {}


def tokenize(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", value.lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS}


def mfo_pages_from_index(index: dict[str, Any]) -> list[MfoPage]:
    pages: list[MfoPage] = []
    for item in index.get("pages", []) if isinstance(index.get("pages"), list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = str(item.get("title") or slug_to_title(url))
        if url:
            pages.append(
                MfoPage(
                    title=title,
                    url=url,
                    slug=str(item.get("slug") or ""),
                    date=item.get("date"),
                    modified=item.get("modified"),
                    source_urls=[str(source) for source in item.get("source_urls", []) if isinstance(source, str)],
                    youtube_ids=[str(video_id) for video_id in item.get("youtube_ids", []) if isinstance(video_id, str)],
                    pmids=[str(pmid) for pmid in item.get("pmids", []) if isinstance(pmid, str)],
                    dois=[str(doi) for doi in item.get("dois", []) if isinstance(doi, str)],
                    source_fingerprints=[str(fingerprint) for fingerprint in item.get("source_fingerprints", []) if isinstance(fingerprint, str)],
                    primary_creator_key=item.get("primary_creator_key") if isinstance(item.get("primary_creator_key"), str) else None,
                )
            )
    return pages


def sync_publication_history(conn: sqlite3.Connection, pages: list[MfoPage]) -> int:
    """Populate publication_history from the MFO archive index's resolved
    primary_creator_key per page. Only scanner.py writes this table (see
    editorial_gates' disjoint-writer design); main.py writes
    commission_history separately. Returns the number of pages with a
    resolved creator attribution."""
    synced = 0
    with conn:
        for page in pages:
            if not page.primary_creator_key:
                continue
            editorial_gates.record_publication_history(
                conn,
                page_url=page.url,
                creator_key=page.primary_creator_key,
                creator_display_name=page.primary_creator_key.replace("_", " ").title(),
                published_at=page.date,
                format_=None,
            )
            synced += 1
    return synced


def exact_source_match(obs: Observation, pages: list[MfoPage]) -> MfoPage | None:
    obs_fingerprints = fingerprints_for_values(obs.video_url, obs.video_title)
    if obs.video_id:
        obs_fingerprints.add(f"youtube:{obs.video_id}")
    for page in pages:
        if obs_fingerprints & page_fingerprints(page):
            return page
    return None


def find_overlap(obs: Observation, pages: list[MfoPage]) -> Overlap:
    config = gates_config()
    candidate_text = f"{obs.channel_name} {obs.video_title}"
    candidate_terms = tokenize(candidate_text) - OVERLAP_STOPWORDS
    best = Overlap(score=0.0, page=None, shared_terms=[])
    if not candidate_terms:
        return best
    # Entity-weighted, not plain shared/total: two short titles sharing a
    # few generic content words (e.g. "diet"/"loss"/"weight") can otherwise
    # produce a high fractional score purely from title brevity, with no
    # named entity/event in common. This is the same weighting
    # topic_overlap_breakdown() uses — kept in sync so the archive_overlap/
    # cannibalisation_risk fields an editor actually sees (and the penalty
    # this function's score drives in score_news_cluster()) reflect it too,
    # not just an additive side field nobody's decision depends on.
    entity_terms = editorial_gates.entity_terms_for_text(candidate_text, config)
    for page in pages:
        page_terms = tokenize(f"{page.title} {page.slug}") - OVERLAP_STOPWORDS
        result = editorial_gates.weighted_topic_overlap(candidate_terms, page_terms, config, entity_terms=entity_terms)
        if result["score"] > best.score:
            best = Overlap(score=result["score"], page=page, shared_terms=result["shared_terms"])
    return best


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_timestamp TEXT NOT NULL,
            channel_count INTEGER NOT NULL,
            error_count INTEGER NOT NULL DEFAULT 0,
            scan_kind TEXT NOT NULL DEFAULT 'scheduled'
        );

        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            channel_source TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            video_title TEXT NOT NULL,
            video_url TEXT NOT NULL,
            video_id TEXT NOT NULL,
            upload_datetime TEXT,
            view_count INTEGER NOT NULL,
            duration_seconds INTEGER,
            video_type TEXT NOT NULL CHECK(video_type IN ('standard', 'shorts')),
            scan_timestamp TEXT NOT NULL,
            age_hours REAL,
            total_views_per_hour REAL,
            previous_view_count INTEGER,
            previous_scan_timestamp TEXT,
            views_gained INTEGER,
            observed_hourly_growth REAL,
            breakout_score REAL,
            comparison_hours REAL,
            growth_signal TEXT NOT NULL DEFAULT 'baseline pending',
            status TEXT NOT NULL DEFAULT 'ok',
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_observations_video_scan
            ON observations(video_id, scan_timestamp);
        CREATE INDEX IF NOT EXISTS idx_observations_channel_type_growth
            ON observations(channel_name, video_type, observed_hourly_growth);

        CREATE TABLE IF NOT EXISTS news_clusters (
            cluster_key TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            title TEXT NOT NULL,
            primary_url TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_seen (
            identifier TEXT PRIMARY KEY,
            identifier_type TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            pmid TEXT,
            doi TEXT,
            title TEXT
        );
        """
    )
    ensure_column(conn, "scans", "scan_kind", "TEXT NOT NULL DEFAULT 'scheduled'")
    ensure_column(conn, "observations", "comparison_hours", "REAL")
    ensure_column(conn, "observations", "growth_signal", "TEXT NOT NULL DEFAULT 'baseline pending'")
    editorial_gates.ensure_saturation_tables(conn)
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def create_scan(conn: sqlite3.Connection, scan_timestamp: str, channel_count: int, scan_kind: str) -> int:
    cur = conn.execute(
        "INSERT INTO scans (scan_timestamp, channel_count, scan_kind) VALUES (?, ?, ?)",
        (scan_timestamp, channel_count, scan_kind),
    )
    return int(cur.lastrowid)


def ydl_opts(**overrides: Any) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietYtdlpLogger(),
        "ignoreerrors": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
        "file_access_retries": 1,
        **overrides,
    }


def ydl_client() -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL(
        ydl_opts(
            extract_flat="in_playlist",
            playlistend=VIDEOS_PER_CHANNEL,
        )
    )


def fetch_channel_videos(channel_source: str) -> list[dict[str, Any]]:
    normalized = normalize_channel_source(channel_source)
    with ydl_client() as ydl:
        playlist = ydl.extract_info(normalized, download=False)
    entries = [entry for entry in (playlist or {}).get("entries", []) if entry]

    videos: list[dict[str, Any]] = []
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        for entry in entries[:VIDEOS_PER_CHANNEL]:
            try:
                video_url = entry.get("url") or entry.get("webpage_url")
                if video_url and not str(video_url).startswith("http"):
                    video_url = f"https://www.youtube.com/watch?v={video_url}"
                if not video_url:
                    videos.append(entry)
                    continue
                info = ydl.extract_info(video_url, download=False)
                if info is None:
                    videos.append(
                        {
                            **entry,
                            "_scan_error": "yt-dlp returned no metadata; video may be unavailable, private or members-only",
                            "webpage_url": video_url,
                        }
                    )
                    continue
                videos.append(info)
            except Exception as exc:
                videos.append(
                    {
                        **entry,
                        "_scan_error": str(exc),
                        "webpage_url": entry.get("webpage_url") or entry.get("url") or normalized,
                    }
                )
    return videos


def video_type_for(info: dict[str, Any]) -> str:
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and duration <= SHORTS_MAX_SECONDS:
        return "shorts"
    urls = " ".join(str(info.get(key) or "") for key in ("url", "webpage_url", "original_url"))
    return "shorts" if "/shorts/" in urls else "standard"


def build_observation(channel_source: str, info: dict[str, Any], scan_timestamp: str) -> Observation:
    if not isinstance(info, dict):
        raise ValueError("video metadata is unavailable")
    upload_datetime = parse_upload_datetime(info)
    scan_dt = parse_dt(scan_timestamp) or utc_now()
    upload_dt = parse_dt(upload_datetime)
    age_hours = None
    if upload_dt:
        age_hours = max((scan_dt - upload_dt).total_seconds() / 3600, 0.0)

    view_count = int(info.get("view_count") or 0)
    duration = info.get("duration")
    duration_seconds = int(duration) if isinstance(duration, (int, float)) else None
    total_vph = view_count / age_hours if age_hours and age_hours > 0 else None
    video_id = str(info.get("id") or "")
    video_url = info.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")

    return Observation(
        channel_source=channel_source,
        channel_name=str(info.get("channel") or info.get("uploader") or "Unknown channel"),
        video_title=str(info.get("title") or "Unknown title"),
        video_url=str(video_url),
        video_id=video_id or str(video_url),
        upload_datetime=upload_datetime,
        view_count=view_count,
        duration_seconds=duration_seconds,
        video_type=video_type_for(info),
        scan_timestamp=scan_timestamp,
        age_hours=age_hours,
        total_views_per_hour=total_vph,
        status="error" if info.get("_scan_error") else "ok",
        error=info.get("_scan_error"),
    )


def previous_observation(conn: sqlite3.Connection, video_id: str, scan_timestamp: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT observations.view_count, observations.scan_timestamp
        FROM observations
        JOIN scans ON scans.id = observations.scan_id
        WHERE observations.video_id = ?
          AND observations.scan_timestamp < ?
          AND observations.status = 'ok'
          AND scans.scan_kind = 'scheduled'
        ORDER BY observations.scan_timestamp DESC
        LIMIT 1
        """,
        (video_id, scan_timestamp),
    ).fetchone()


def channel_baseline(
    conn: sqlite3.Connection,
    channel_name: str,
    video_type: str,
    scan_timestamp: str,
    age_hours: float | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[float | None, str, int]:
    """Median hourly-growth baseline for a channel, bounded to the most
    recent comparison_window observations and, when age_hours is known,
    restricted to observations measured at a comparable point in a
    video's lifecycle. Returns (baseline, confidence, comparison_count)
    rather than just a number, so callers can refuse to treat a
    thin/absent baseline as equivalent to a well-established one."""
    config = config or gates_config()
    breakout_config = config.get("breakout_confidence", {})
    window = int(breakout_config.get("comparison_window", 20))
    tolerance = float(breakout_config.get("age_match_tolerance_hours", 12))

    rows = conn.execute(
        """
        SELECT observed_hourly_growth, age_hours
        FROM observations
        WHERE channel_name = ?
          AND video_type = ?
          AND scan_timestamp < ?
          AND observed_hourly_growth IS NOT NULL
          AND observed_hourly_growth > 0
          AND status = 'ok'
        ORDER BY scan_timestamp DESC
        LIMIT ?
        """,
        (channel_name, video_type, scan_timestamp, window),
    ).fetchall()

    if age_hours is not None:
        matched = [row for row in rows if row["age_hours"] is not None and abs(float(row["age_hours"]) - age_hours) <= tolerance]
        # Fall back to the unfiltered window if age-matching leaves too few
        # rows to be meaningful, rather than silently treating "no exact
        # age match" as "no baseline at all".
        candidate_rows = matched if len(matched) >= 1 else rows
    else:
        candidate_rows = rows

    values = [float(row["observed_hourly_growth"]) for row in candidate_rows]
    confidence = editorial_gates.breakout_confidence_tier(len(values), config)
    return (median(values) if values else None), confidence, len(values)


def enrich_growth(conn: sqlite3.Connection, obs: Observation) -> Observation:
    previous = previous_observation(conn, obs.video_id, obs.scan_timestamp)
    if previous:
        previous_dt = parse_dt(previous["scan_timestamp"])
        current_dt = parse_dt(obs.scan_timestamp)
        elapsed_hours = None
        if previous_dt and current_dt:
            elapsed_hours = (current_dt - previous_dt).total_seconds() / 3600
        obs.comparison_hours = elapsed_hours
        obs.views_gained = obs.view_count - int(previous["view_count"])
        if elapsed_hours is None:
            obs.growth_signal = "baseline pending"
        elif elapsed_hours < 1:
            obs.growth_signal = "insufficient growth interval"
        elif elapsed_hours <= 6:
            obs.growth_signal = "preliminary growth"
        else:
            obs.growth_signal = "useful growth signal"
        if elapsed_hours and elapsed_hours >= 1:
            obs.observed_hourly_growth = obs.views_gained / elapsed_hours
    else:
        obs.growth_signal = "baseline pending"

    baseline, confidence, comparison_count = channel_baseline(conn, obs.channel_name, obs.video_type, obs.scan_timestamp, obs.age_hours)
    obs.breakout_confidence = confidence
    obs.breakout_comparison_count = comparison_count
    if baseline and obs.age_hours is not None:
        obs.breakout_expected_views_at_current_age = round(baseline * obs.age_hours, 1)
    if baseline and obs.observed_hourly_growth is not None:
        obs.breakout_score = obs.observed_hourly_growth / baseline
    return obs


def save_observation(conn: sqlite3.Connection, scan_id: int, obs: Observation) -> None:
    previous = previous_observation(conn, obs.video_id, obs.scan_timestamp)
    conn.execute(
        """
        INSERT INTO observations (
            scan_id, channel_source, channel_name, video_title, video_url, video_id,
            upload_datetime, view_count, duration_seconds, video_type, scan_timestamp,
            age_hours, total_views_per_hour, previous_view_count, previous_scan_timestamp,
            views_gained, observed_hourly_growth, breakout_score, comparison_hours,
            growth_signal, status, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_id,
            obs.channel_source,
            obs.channel_name,
            obs.video_title,
            obs.video_url,
            obs.video_id,
            obs.upload_datetime,
            obs.view_count,
            obs.duration_seconds,
            obs.video_type,
            obs.scan_timestamp,
            obs.age_hours,
            obs.total_views_per_hour,
            int(previous["view_count"]) if previous else None,
            previous["scan_timestamp"] if previous else None,
            obs.views_gained,
            obs.observed_hourly_growth,
            obs.breakout_score,
            obs.comparison_hours,
            obs.growth_signal,
            obs.status,
            obs.error,
        ),
    )


def fmt_number(value: int | float | None, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "baseline pending"
    if digits:
        return f"{value:,.{digits}f}"
    return f"{value:,.0f}"


def fmt_age(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def fmt_datetime(value: str | None) -> str:
    if not value:
        return "unknown"
    dt = parse_dt(value)
    if not dt:
        return value
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def exceptional_label(obs: Observation) -> str:
    if obs.growth_signal in {"baseline pending", "insufficient growth interval"}:
        return "baseline pending"
    if obs.breakout_score is None:
        return "baseline pending"
    if obs.breakout_score >= 3:
        return f"exceptional ({obs.breakout_score:.1f}x channel baseline)"
    if obs.breakout_score >= 1.5:
        return f"above normal ({obs.breakout_score:.1f}x channel baseline)"
    return f"not exceptional yet ({obs.breakout_score:.1f}x channel baseline)"


def overlap_label(overlap: Overlap) -> str:
    if not overlap.page:
        return "No obvious archive conflict found."
    if overlap.score >= 0.35:
        prefix = "High cannibalisation risk"
    elif overlap.score >= 0.2:
        prefix = "Possible overlap"
    else:
        prefix = "Weak overlap"
    terms = ", ".join(overlap.shared_terms[:8])
    return f"{prefix}: [{overlap.page.title}]({overlap.page.url})" + (f" (shared: {terms})." if terms else ".")


def overlap_payload(overlap: Overlap | None) -> dict[str, Any] | None:
    if not overlap or not overlap.page:
        return None
    return {
        "score": overlap.score,
        "page_title": overlap.page.title,
        "page_url": overlap.page.url,
        "shared_terms": overlap.shared_terms,
    }


def mfo_page_payload(page: MfoPage | None) -> dict[str, Any] | None:
    if not page:
        return None
    return {
        "title": page.title,
        "url": page.url,
        "slug": page.slug,
        "date": page.date,
    }


def topic_overlap_breakdown(
    candidate_text: str,
    pages: list[MfoPage],
    exact_page: MfoPage | None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Entity/topic-weighted archive overlap, additive alongside the
    existing lexical Overlap/archive_overlap fields. Distinguishes:
    - source_overlap: exact-fingerprint duplicate (same underlying source)
    - topic_overlap: weighted term overlap with named entities weighted
      higher than generic fitness vocabulary
    - search_intent_overlap: other MFO pages sharing the same broad topic
      bucket, regardless of exact wording
    - cannibalisation_risk: none|low|medium|high|exact, derived from the
      above rather than raw lexical overlap alone
    """
    config = config or gates_config()
    candidate_terms = tokenize(candidate_text) - OVERLAP_STOPWORDS
    entity_terms = editorial_gates.entity_terms_for_text(candidate_text, config)
    best: dict[str, Any] = {"score": 0.0, "shared_terms": [], "shared_entity_terms": [], "page": None}
    for page in pages:
        page_terms = tokenize(f"{page.title} {page.slug}") - OVERLAP_STOPWORDS
        result = editorial_gates.weighted_topic_overlap(candidate_terms, page_terms, config, entity_terms=entity_terms)
        if result["score"] > best["score"]:
            best = {**result, "page": page}

    broad_topic = editorial_gates.broad_topic_for(candidate_text, config)
    search_intent_matches: list[dict[str, str]] = []
    if broad_topic:
        for page in pages:
            if page is exact_page:
                continue
            if editorial_gates.broad_topic_for(f"{page.title} {page.slug}", config) == broad_topic:
                search_intent_matches.append({"title": page.title, "url": page.url})

    thresholds = config.get("cannibalisation_thresholds", {})
    high_score = float(thresholds.get("high_score", 0.4))
    medium_score = float(thresholds.get("medium_score", 0.35))
    if exact_page:
        cannibalisation_risk = "exact"
    elif best["shared_entity_terms"] and best["score"] >= high_score:
        cannibalisation_risk = "high"
    elif best["shared_entity_terms"] or best["score"] >= medium_score:
        cannibalisation_risk = "medium"
    elif best["score"] > 0:
        cannibalisation_risk = "low"
    else:
        cannibalisation_risk = "none"

    return {
        "source_overlap": [{"title": exact_page.title, "url": exact_page.url}] if exact_page else [],
        "topic_overlap": {
            "score": best["score"],
            "shared_terms": best.get("shared_terms", []),
            "shared_entity_terms": best.get("shared_entity_terms", []),
            "page": mfo_page_payload(best.get("page")),
        },
        "search_intent_overlap": search_intent_matches[:5],
        "cannibalisation_risk": cannibalisation_risk,
        "broad_topic": broad_topic,
    }


def creator_lead_payload(
    obs: Observation,
    profiles: dict[str, dict[str, str]],
    pages: list[MfoPage],
    status: str,
    rejection_reason: str | None = None,
    exact_page: MfoPage | None = None,
    transcript_enrichment: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    profile = profile_for(obs, profiles)
    overlap = find_overlap(obs, pages)
    dimensions = creator_editorial_dimensions(obs, profiles, pages)
    config = gates_config()
    creator_key = editorial_gates.normalize_creator_key(obs.channel_name, config)
    saturation = (
        editorial_gates.compute_saturation(creator_key, conn, config)
        if conn is not None
        else {
            "source_saturation": {"status": "clear", "recent_story_count": 0, "cooldown_days_remaining": 0, "recent_matches": []},
            "format_saturation": {"status": "clear", "format": None, "recent_matches": []},
        }
    )
    traction = {
        "view_count": obs.view_count,
        "duration_seconds": obs.duration_seconds,
        "age_hours": obs.age_hours,
        "total_views_per_hour": obs.total_views_per_hour,
        "views_gained": obs.views_gained,
        "observed_hourly_growth": obs.observed_hourly_growth,
        "comparison_hours": obs.comparison_hours,
        "growth_signal": obs.growth_signal,
        "breakout_score": obs.breakout_score,
    }
    channel_baseline_payload = {
        "comparison_video_count": obs.breakout_comparison_count,
        "expected_views_at_current_age": obs.breakout_expected_views_at_current_age,
        "actual_views": obs.view_count,
        "breakout_multiple": round(obs.breakout_score, 2) if obs.breakout_score is not None else None,
        "confidence": obs.breakout_confidence,
    }
    return {
        "lead_id": f"creator:{obs.video_id}",
        "scanner_type": "creator",
        "source_name": obs.channel_name,
        "source_category": profile.get("category"),
        "title": obs.video_title,
        "source_url": obs.video_url,
        "published_at": obs.upload_datetime,
        "discovered_at": obs.scan_timestamp,
        "traction": traction,
        "scanner_score": obs.breakout_score,
        "audience_momentum": {
            "relative_channel_breakout": dimensions["relative_channel_breakout"],
            "observed_hourly_growth": dimensions["observed_hourly_growth"],
            "absolute_views": dimensions["absolute_views"],
        },
        "channel_baseline": channel_baseline_payload,
        "editorial_opportunity_score": dimensions["score"],
        "editorial_score_breakdown": {key: value for key, value in dimensions.items() if key != "score"},
        "likely_mfo_angle": profile.get("default_angle"),
        "mfo_audience_fit": profile.get("mfo_fit"),
        "weakness_or_rejection_reason": rejection_reason or profile.get("default_weakness"),
        "primary_source": {
            "name": obs.channel_name,
            "url": obs.video_url,
            "video_id": obs.video_id,
        },
        "archive_overlap": overlap_payload(overlap),
        "cannibalisation_risk": "exact_source" if exact_page else "possible" if overlap.page and overlap.score >= 0.35 else "weak" if overlap.page else "none",
        "topic_overlap_breakdown": topic_overlap_breakdown(f"{obs.channel_name} {obs.video_title}", pages, exact_page),
        "imagery": {
            "available": None,
            "notes": "YouTube thumbnail may be available; verify usage rights before publication.",
        },
        "evidence_links": [obs.video_url],
        "source_fingerprints": sorted(fingerprints_for_values(obs.video_url, obs.video_title)),
        "creator_enrichment": transcript_enrichment or {"available": False, "warning": "Transcript enrichment was not requested for this candidate."},
        "status": status,
        "existing_mfo_page": mfo_page_payload(exact_page),
        "creator_key": creator_key,
        "source_saturation": saturation["source_saturation"],
        "format_saturation": saturation["format_saturation"],
        "story_value": dimensions.get("story_value"),
        "what_changed_now": dimensions.get("what_changed_now"),
        "kill_reason_codes": (
            dimensions.get("kill_reason_codes", [])
            + (["creator_source_cooldown"] if saturation["source_saturation"]["status"] == "blocked" else [])
        ),
    }


def write_json_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def candidate_lines(
    obs: Observation,
    rank: int,
    baseline_mode: bool,
    profiles: dict[str, dict[str, str]],
    pages: list[MfoPage],
) -> list[str]:
    profile = profile_for(obs, profiles)
    overlap = find_overlap(obs, pages)
    if obs.observed_hourly_growth is None:
        growth_text = f"provisional average {fmt_number(obs.total_views_per_hour, 1)} views/hour since upload"
    else:
        growth_text = f"observed {fmt_number(obs.observed_hourly_growth, 1)} views/hour"
    views_gained = "baseline pending" if obs.views_gained is None else fmt_number(obs.views_gained)
    source_time = fmt_datetime(obs.upload_datetime)
    story_angle = profile.get("default_angle", "")
    value_add = profile.get("default_value_add", "")
    weakness = profile.get("default_weakness", "")
    category = profile.get("category", "fitness lead")
    mfo_fit = profile.get("mfo_fit", "Needs editor review")

    return [
        f"### {rank}. {obs.channel_name}: {obs.video_title}",
        "",
        f"- Source and publication time: `{obs.channel_name}`, {source_time}.",
        f"- Current traction: {fmt_number(obs.view_count)} views; average {fmt_number(obs.total_views_per_hour, 1)} views/hour since upload.",
        f"- Scan growth: {views_gained} views gained; {growth_text}; interval status: {obs.growth_signal}.",
        f"- Exceptional relative to source: {exceptional_label(obs)}.",
        f"- MFO fit: {mfo_fit} ({category}).",
        f"- Original MFO value: {value_add}",
        f"- Likely angle: {story_angle}",
        f"- Weakness/rejection reason: {weakness}",
        f"- Archive overlap: {overlap_label(overlap)}",
        f"- URL for review/MFO Pack: {obs.video_url}",
        "",
    ]


def section_lines(
    title: str,
    rows: list[Observation],
    baseline_mode: bool,
    profiles: dict[str, dict[str, str]],
    pages: list[MfoPage],
) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        return lines + ["No videos found.", ""]
    for index, obs in enumerate(rows, 1):
        lines.extend(candidate_lines(obs, index, baseline_mode, profiles, pages))
    return lines


def reject_reason(obs: Observation, profiles: dict[str, dict[str, str]], pages: list[MfoPage]) -> str | None:
    freshness_limit = gates_config().get("freshness_windows", {}).get("creator_video", {}).get("hours", 168)
    if obs.age_hours and obs.age_hours > freshness_limit and (obs.breakout_score is None or obs.breakout_score < 1.5):
        return "older than the creator-video freshness window without a clear breakout signal"
    profile = profile_for(obs, profiles)
    if profile.get("mfo_fit", "").lower().startswith("low"):
        return "low configured MFO fit"
    return None


def creator_editorial_dimensions(obs: Observation, profiles: dict[str, dict[str, str]], pages: list[MfoPage]) -> dict[str, Any]:
    config = gates_config()
    profile = profile_for(obs, profiles)
    text = f"{obs.video_title} {profile.get('default_angle', '')} {profile.get('mfo_fit', '')}".lower()
    breakout = min(100, int((obs.breakout_score or 0) * 30))
    velocity = min(100, int((obs.observed_hourly_growth or obs.total_views_per_hour or 0) / 1000 * 20))
    low_confidence = obs.breakout_confidence in ("pending", "low")
    if low_confidence:
        # No confident channel-relative baseline exists: raw views/hour
        # (the obs.total_views_per_hour fallback above) cannot be trusted
        # as a velocity/breakout signal without a track record to compare
        # against. Cap these dimensions rather than the video's real
        # fit/story-angle/evidence merits.
        velocity = min(velocity, 20)
        breakout = min(breakout, 20)
    freshness_window_hours = config.get("freshness_windows", {}).get("creator_video", {}).get("hours", 168)
    freshness_check = editorial_gates.freshness_gate("creator_video", obs.age_hours, config)
    freshness = 100 if (obs.age_hours or 999) <= min(48, freshness_window_hours) else 70 if (obs.age_hours or 999) <= freshness_window_hours else 25
    if freshness_check["status"] == "fail":
        freshness = min(freshness, 10)
    fit = 80 if "high" in profile.get("mfo_fit", "").lower() else 55 if "medium" in profile.get("mfo_fit", "").lower() else 35
    story_angle = 75 if any(term in text for term in ("study", "experiment", "injury", "training", "challenge", "genetics", "transformation")) else 35
    practical = 70 if any(term in text for term in ("training", "workout", "strength", "exercise", "diet", "sleep")) else 35
    evidence = 45
    if any(term in text for term in ("medical", "injury", "genetics", "disease", "testosterone")):
        evidence = 35
    aus = 35 if "australia" not in text and "australian" not in text else 70
    effort = 35 if any(term in text for term in ("medical", "injury", "genetics", "transformation")) else 20
    archive_risk = 100 if exact_source_match(obs, pages) else 60 if find_overlap(obs, pages).score >= 0.35 else 10
    score = round(
        fit * 0.20
        + story_angle * 0.20
        + practical * 0.15
        + evidence * 0.15
        + freshness * 0.10
        + velocity * 0.08
        + breakout * 0.05
        + aus * 0.05
        - effort * 0.08
        - archive_risk * 0.35
    )
    if story_angle < 50:
        score = min(score, 45)
    kill_reason_codes: list[str] = []
    if freshness_check["status"] == "fail":
        # A high score must never override a failed hard gate.
        score = min(score, 20)
        kill_reason_codes.append(freshness_check["kill_reason"])
    if obs.breakout_confidence == "pending":
        # Zero comparable historical observations exist for this channel;
        # a baseline-pending video cannot receive a Strong rating from
        # views alone. This backstop caps the whole score below the
        # commission_now threshold even if fit/story-angle dimensions
        # alone would otherwise clear it on a thin video description.
        cap = config.get("breakout_confidence", {}).get("score_cap_when_pending_or_low", 55)
        score = min(score, cap)
        kill_reason_codes.append("no_channel_relative_breakout")

    # Creator-story eligibility checklist: the same 2-of-N discipline News
    # Radar already applies via what_changed_now(), rather than the single
    # crude story_angle keyword check this used to rely on alone.
    checklist_criteria = {
        "clear_channel_relative_breakout": obs.breakout_confidence in ("high", "medium") and (obs.breakout_score or 0) >= 1.5,
        "recognised_entity_or_event": bool(
            editorial_gates.classify_development_type(text, config, evidence_links=[obs.video_url]).get("entity_matched")
        ),
        "new_claim_or_result": any(term in text for term in ("record", "results", "proven", "debunk", "breaks", " vs ", "compared", "study shows", "world first")),
        "practical_lesson_not_recently_covered": practical >= 70 and archive_risk <= 10,
        "independently_verifiable_evidence": any(term in text for term in ("study", "data", "published", "peer-reviewed", "research", "official")),
        "strong_australian_angle": aus >= 70,
    }
    eligibility = editorial_gates.creator_story_eligibility(checklist_criteria, config)
    what_changed_now = None
    if eligibility["eligible"]:
        what_changed_now = (
            f"Meets {len(eligibility['criteria_passed'])} of {len(checklist_criteria)} creator-story "
            f"criteria: {', '.join(eligibility['criteria_passed'])}."
        )
    else:
        # A high score must never override a failed hard gate: fewer than
        # min_pass criteria means no clear, specific reason to commission
        # this video now rather than any other upload from this channel.
        cap = config.get("creator_story_checklist", {}).get("score_cap_when_ineligible", 50)
        score = min(score, cap)
        kill_reason_codes.append("no_new_development")

    return {
        "relative_channel_breakout": round(obs.breakout_score or 0, 2),
        "observed_hourly_growth": obs.observed_hourly_growth,
        "absolute_views": obs.view_count,
        "breakout_confidence": obs.breakout_confidence,
        "freshness": freshness,
        "freshness_gate_status": freshness_check["status"],
        "mfo_audience_fit": fit,
        "strength_of_story_angle": story_angle,
        "practical_usefulness": practical,
        "primary_evidence_quality": evidence,
        "australian_relevance": aus,
        "archive_risk": archive_risk,
        "estimated_production_effort": effort,
        "story_value": eligibility["story_value"],
        "checklist_results": eligibility["checklist_results"],
        "criteria_passed": eligibility["criteria_passed"],
        "what_changed_now": what_changed_now or "No clear current development found.",
        "kill_reason_codes": kill_reason_codes,
        "score": max(0, min(100, score)),
    }


def summarize_transcript_text(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    summary = " ".join(sentences[:3])[:700] if cleaned else ""
    lower = cleaned.lower()
    exercises = sorted({term for term in ("bench press", "squat", "deadlift", "pull-up", "push-up", "cardio", "hyrox", "running", "bodybuilding", "calorie", "protein") if term in lower})
    claims = sorted({term for term in ("genetics", "injury", "transformation", "testosterone", "steroid", "natural", "world record") if term in lower})
    return {
        "summary": summary,
        "people_featured": [],
        "actual_experiment_or_claim": summary[:240],
        "specific_exercises_training_methods_or_results": exercises,
        "unsupported_or_misleading_title_claims": claims,
        "what_mfo_could_add": "Verify the claims, separate entertainment from evidence, and add practical context for Australian men.",
    }


def srt_to_plain_text(srt_text: str) -> str:
    entries: list[str] = []
    seen: set[str] = set()
    blocks = re.split(r"\n\s*\n", srt_text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.isdigit() or "-->" in stripped or stripped.upper() == "WEBVTT":
                continue
            lines.append(re.sub(r"<[^>]+>", "", stripped))
        caption = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if caption and caption.lower() not in seen:
            seen.add(caption.lower())
            entries.append(caption)
    return "\n".join(entries)


def enrich_creator_transcript(obs: Observation, lang: str = "en") -> dict[str, Any]:
    output_dir = Path("/tmp/mfo-scanner-transcripts")
    output_dir.mkdir(parents=True, exist_ok=True)
    base_opts = {
        "outtmpl": str(output_dir / f"{obs.video_id}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "srt/best",
    }
    try:
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(obs.video_url, download=True)
            available_langs = sorted(set((info.get("subtitles") or {}).keys()) | set((info.get("automatic_captions") or {}).keys()))
        matches = sorted(output_dir.glob(f"{obs.video_id}.{lang}.*"))
        subtitle = next((path for path in matches if path.suffix == ".srt"), matches[0] if matches else None)
        if not subtitle:
            return {"available": False, "language": lang, "available_languages": available_langs, "warning": "No transcript file was downloaded."}
        text = srt_to_plain_text(subtitle.read_text(encoding="utf-8", errors="replace"))
        return {"available": True, "language": lang, "available_languages": available_langs, **summarize_transcript_text(text)}
    except Exception as exc:
        return {"available": False, "language": lang, "warning": str(exc)[:300]}


def enrich_leading_creator_candidates(rows: list[Observation], limit: int = 3) -> dict[str, dict[str, Any]]:
    enriched: dict[str, dict[str, Any]] = {}
    for obs in rows[:limit]:
        enriched[obs.video_id] = enrich_creator_transcript(obs)
    return enriched


def sort_metric(obs: Observation, baseline_mode: bool) -> tuple[float, float, float]:
    if obs.observed_hourly_growth is None or baseline_mode:
        return (0.0, obs.total_views_per_hour or 0.0, float(obs.view_count))
    return (
        float(obs.views_gained if obs.views_gained is not None else -1),
        obs.observed_hourly_growth or 0.0,
        obs.breakout_score or 0.0,
    )


def editorial_sort_metric(obs: Observation, profiles: dict[str, dict[str, str]], pages: list[MfoPage]) -> tuple[float, float, float]:
    dimensions = creator_editorial_dimensions(obs, profiles, pages)
    return (
        float(dimensions["score"]),
        float(dimensions["observed_hourly_growth"] or 0),
        float(dimensions["absolute_views"] or 0),
    )


def write_report(
    observations: list[Observation],
    errors: list[str],
    scan_timestamp: str,
    report_path: Path,
    profiles: dict[str, dict[str, str]],
    mfo_index: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> None:
    ok = [obs for obs in observations if obs.status == "ok"]
    baseline_mode = all(obs.views_gained is None for obs in ok)
    reliable_growth = any(obs.observed_hourly_growth is not None for obs in ok)
    pages = mfo_pages_from_index(mfo_index)

    covered: list[tuple[Observation, MfoPage]] = []
    followups: list[tuple[Observation, Overlap]] = []
    rejected: list[tuple[Observation, str]] = []
    new_leads: list[Observation] = []
    for obs in ok:
        exact = exact_source_match(obs, pages)
        if exact:
            covered.append((obs, exact))
            continue
        overlap = find_overlap(obs, pages)
        if overlap.page and overlap.score >= 0.35:
            followups.append((obs, overlap))
            continue
        reason = reject_reason(obs, profiles, pages)
        if reason:
            rejected.append((obs, reason))
            continue
        new_leads.append(obs)

    new_standard = sorted([obs for obs in new_leads if obs.video_type == "standard"], key=lambda obs: editorial_sort_metric(obs, profiles, pages), reverse=True)[:10]
    new_shorts = sorted([obs for obs in new_leads if obs.video_type == "shorts"], key=lambda obs: editorial_sort_metric(obs, profiles, pages), reverse=True)[:10]
    followup_rows = sorted(followups, key=lambda row: sort_metric(row[0], baseline_mode), reverse=True)[:12]
    transcript_enrichment: dict[str, dict[str, Any]] = {}
    if mfo_index.get("source") != "fixture":
        transcript_enrichment = enrich_leading_creator_candidates(new_standard + new_shorts, limit=3)

    lines = [
        "# MFO Creator Radar",
        "",
        f"- Scan timestamp: `{scan_timestamp}`",
        f"- Videos observed: `{len(ok)}`",
        f"- Ranking mode: `editorial opportunity score; raw breakout is reported separately`",
        f"- MFO archive index: `{len(pages)} pages` from `{mfo_index.get('source', 'not available')}` refreshed `{mfo_index.get('refreshed_at', 'unknown')}`",
        f"- Archive source fingerprints: `{mfo_index.get('source_fingerprint_count', 0)}`",
        "",
        "Use this as a lead sheet, not a publishing instruction. Titles are prompts to investigate; they are not facts.",
        "",
    ]
    if mfo_index.get("archive_warning"):
        lines.extend([f"**Archive warning:** {mfo_index.get('archive_warning')}", ""])
    lines.extend(section_lines("New Leads - Standard Videos", new_standard, baseline_mode, profiles, pages))
    lines.extend(section_lines("New Leads - Shorts", new_shorts, baseline_mode, profiles, pages))

    lines.extend(["## Possible Update Or Follow-Up Opportunities", ""])
    if not followup_rows:
        lines.extend(["No topical overlaps found.", ""])
    for index, (obs, overlap) in enumerate(followup_rows, 1):
        lines.extend(candidate_lines(obs, index, baseline_mode, profiles, pages))
        lines.append(f"  Follow-up trigger: {overlap_label(overlap)}")
        lines.append("")

    lines.extend(["## Already Covered And Excluded", ""])
    if not covered:
        lines.extend(["No exact source matches found in the MFO archive.", ""])
    for obs, page in covered[:30]:
        lines.append(f"- [{obs.channel_name}: {obs.video_title}]({obs.video_url}) - exact source already appears in [{page.title}]({page.url}).")
    lines.append("")

    if rejected:
        lines.extend(["## Other Rejected Or Weak Leads", ""])
        for obs, reason in rejected[:20]:
            lines.append(f"- [{obs.channel_name}: {obs.video_title}]({obs.video_url}) - {reason}.")
        lines.append("")

    if errors:
        lines.extend(["## Scan Errors", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")

    lead_payloads: list[dict[str, Any]] = []
    lead_payloads.extend(creator_lead_payload(obs, profiles, pages, "new_lead", transcript_enrichment=transcript_enrichment.get(obs.video_id), conn=conn) for obs in new_leads)
    lead_payloads.extend(creator_lead_payload(obs, profiles, pages, "follow_up", transcript_enrichment=transcript_enrichment.get(obs.video_id), conn=conn) for obs, _overlap in followups)
    lead_payloads.extend(creator_lead_payload(obs, profiles, pages, "already_covered", exact_page=page, conn=conn) for obs, page in covered)
    lead_payloads.extend(creator_lead_payload(obs, profiles, pages, "rejected", rejection_reason=reason, conn=conn) for obs, reason in rejected)
    write_json_payload(
        report_path.with_suffix(".json"),
        {
            "scanner_type": "creator",
            "schema_version": 1,
            "generated_at": scan_timestamp,
            "report_path": str(report_path),
            "lead_count": len(lead_payloads),
            "viable_count": len([lead for lead in lead_payloads if lead["status"] in {"new_lead", "follow_up"}]),
            "errors": errors,
            "metadata": {
                "videos_observed": len(ok),
                "ranking_mode": "editorial opportunity score; raw breakout reported separately",
                "mfo_archive_pages": len(pages),
                "mfo_archive_refreshed_at": mfo_index.get("refreshed_at"),
                "mfo_archive_source_fingerprint_count": mfo_index.get("source_fingerprint_count", 0),
                "mfo_archive_warning": mfo_index.get("archive_warning", ""),
            },
            "leads": lead_payloads,
        },
    )


def domain_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def rss_items_from_xml(xml_text: str, source_name: str, source_type: str) -> list[NewsItem]:
    root = ElementTree.fromstring(xml_text)
    items: list[NewsItem] = []
    for item in root.iter():
        if not item.tag.endswith("item") and not item.tag.endswith("entry"):
            continue
        title = ""
        link = ""
        published = None
        summary = ""
        item_source = source_name
        item_source_url = ""
        for child in item:
            tag = child.tag.lower()
            text = child.text or ""
            if tag.endswith("title"):
                title = strip_html(text)
            elif tag.endswith("link"):
                link = child.attrib.get("href") or text
            elif tag.endswith("pubdate") or tag.endswith("published") or tag.endswith("updated"):
                try:
                    published = iso(parsedate_to_datetime(text).astimezone(timezone.utc))
                except Exception:
                    published = text or None
            elif tag.endswith("description") or tag.endswith("summary"):
                summary = strip_html(text)
            elif tag.endswith("source"):
                item_source = strip_html(text) or item_source
                item_source_url = child.attrib.get("url") or item_source_url
        if title and link:
            url = clean_url(link) or clean_url(item_source_url) or link
            items.append(NewsItem(title=title, url=url, source=item_source, published=published, summary=summary, source_type=source_type))
    return items


def load_news_items(sources_path: Path, queries_path: Path) -> tuple[list[NewsItem], list[str]]:
    errors: list[str] = []
    items: list[NewsItem] = []
    sources = load_json_object(sources_path)
    for source in sources.get("feeds", []) if isinstance(sources.get("feeds"), list) else []:
        if not isinstance(source, dict):
            continue
        name = str(source.get("name") or domain_for(str(source.get("url") or "")))
        url = str(source.get("url") or "")
        if not url:
            continue
        try:
            items.extend(rss_items_from_xml(http_get_text(url), name, str(source.get("type") or "rss")))
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    queries = load_json_object(queries_path)
    for query in queries.get("queries", []) if isinstance(queries.get("queries"), list) else []:
        if isinstance(query, dict):
            q = str(query.get("query") or "")
        else:
            q = str(query)
        if not q:
            continue
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-AU&gl=AU&ceid=AU:en"
        try:
            items.extend(rss_items_from_xml(http_get_text(url), f"Google News: {q}", "google-news"))
        except Exception as exc:
            errors.append(f"Google News {q}: {exc}")
    return items, errors


def is_news_development(item: NewsItem) -> bool:
    terms = tokenize(f"{item.title} {item.summary}")
    return bool(terms & NEWS_TERMS)


def cluster_key_for(item: NewsItem, config: dict[str, Any] | None = None) -> str:
    """Stable, unique key for a news cluster's primary item.

    Must never collapse two distinct stories/URLs to the same key: this
    key is both the packet lead_id suffix and the news_clusters SQLite
    primary key, so a collision here both merges unrelated lead_ids and
    silently overwrites one story's history with another's. Prefer DOI,
    then PMID, then the canonical source URL (already content-clustered
    by cluster_news_items() before this ever runs, so by this point each
    cluster represents one distinct story and its URL is a safe, stable
    key). Only fall back to an entity/event/date slug when no URL is
    available at all.
    """
    text = f"{item.title} {item.summary}".lower()
    for doi in extract_dois(text):
        return f"doi-{doi}"
    for pmid in extract_pmids(text):
        return f"pmid-{pmid}"
    canonical = canonical_url(item.url)
    if canonical:
        return re.sub(r"[^a-z0-9]+", "-", canonical.lower()).strip("-")[:80]
    classification = editorial_gates.classify_development_type(
        f"{item.title} {item.summary}", config or gates_config(), evidence_links=[item.url]
    )
    entity = classification.get("entity_matched")
    event_date = classification.get("event_date")
    if entity:
        parts = [entity, classification.get("development_type") or "other"]
        if event_date:
            parts.append(re.sub(r"[^a-z0-9]+", "-", event_date.lower()))
        return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")[:80]
    terms = sorted(tokenize(item.title) - NEWS_TERMS - {"review", "strength", "exercise"})
    return "-".join(terms[:8]) or re.sub(r"\W+", "-", item.title.lower()).strip("-")[:60]


def cluster_news_items(items: list[NewsItem], config: dict[str, Any] | None = None) -> list[NewsCluster]:
    config = config or gates_config()
    clusters: list[NewsCluster] = []
    for item in [candidate for candidate in items if is_news_development(candidate)]:
        item_terms = tokenize(item.title)
        best: NewsCluster | None = None
        best_score = 0.0
        for cluster in clusters:
            cluster_terms = tokenize(" ".join(existing.title for existing in cluster.items))
            fingerprint_overlap = bool(fingerprints_for_values(item.url, item.title, item.summary) & fingerprints_for_values(*(existing.url for existing in cluster.items), *(existing.title for existing in cluster.items), *(existing.summary for existing in cluster.items)))
            score = len(item_terms & cluster_terms) / max(len(item_terms | cluster_terms), 1)
            if fingerprint_overlap:
                score = 1.0
            if score > best_score:
                best = cluster
                best_score = score
        if best and best_score >= 0.2:
            best.items.append(item)
        else:
            clusters.append(NewsCluster(key=cluster_key_for(item, config), items=[item], penalties=[]))
    return clusters


def upsert_news_clusters(conn: sqlite3.Connection, clusters: list[NewsCluster], seen_at: str) -> None:
    """Persist each cluster's first/last-seen history keyed by cluster_key.

    If a computed key already exists in the table but points at a
    materially different story (low title term-overlap with the stored
    title, or a different primary_url), assume this is a residual key
    collision rather than the same recurring story, and disambiguate
    with a numeric suffix instead of silently overwriting the older
    story's first_seen/title/primary_url.
    """
    for cluster in clusters:
        primary = primary_news_item(cluster)
        key = cluster.key
        existing = conn.execute("SELECT first_seen, title, primary_url FROM news_clusters WHERE cluster_key = ?", (key,)).fetchone()
        if existing and existing["primary_url"] != primary.url:
            existing_terms = tokenize(existing["title"] or "")
            new_terms = tokenize(primary.title or "")
            overlap = len(existing_terms & new_terms) / max(len(existing_terms | new_terms), 1)
            if overlap < 0.3:
                suffix = 2
                candidate_key = f"{key}-{suffix}"
                while True:
                    row = conn.execute("SELECT primary_url FROM news_clusters WHERE cluster_key = ?", (candidate_key,)).fetchone()
                    if not row or row["primary_url"] == primary.url:
                        break
                    suffix += 1
                    candidate_key = f"{key}-{suffix}"
                cluster.key = candidate_key
                key = candidate_key
                existing = conn.execute("SELECT first_seen FROM news_clusters WHERE cluster_key = ?", (key,)).fetchone()
        first_seen = existing["first_seen"] if existing else seen_at
        conn.execute(
            """
            INSERT INTO news_clusters (cluster_key, first_seen, last_seen, title, primary_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cluster_key) DO UPDATE SET
                last_seen = excluded.last_seen,
                title = excluded.title,
                primary_url = excluded.primary_url
            """,
            (key, first_seen, seen_at, primary.title, primary.url),
        )


def first_seen_for(conn: sqlite3.Connection, cluster: NewsCluster, fallback: str) -> str:
    row = conn.execute("SELECT first_seen FROM news_clusters WHERE cluster_key = ?", (cluster.key,)).fetchone()
    return row["first_seen"] if row else fallback


def source_preference_rank(item: NewsItem) -> int:
    domain = domain_for(item.url)
    if "youtube.com" in domain or "youtu.be" in domain:
        return 4
    if item.source_type in {"official", "organisation", "journal"}:
        return 1
    if item.source_type in {"publicity", "brand"}:
        return 2
    if item.source_type == "research_media":
        return 2
    if item.source_type in {"rss"} and not any(domain.endswith(press) for press in PRESS_RELEASE_DOMAINS):
        return 3
    if item.source_type == "google-news" or "news.google.com" in domain:
        return 5
    return 4


def primary_news_item(cluster: NewsCluster) -> NewsItem:
    return sorted(cluster.items, key=lambda item: (source_preference_rank(item), item.published or ""))[0]


def independent_domains(cluster: NewsCluster) -> set[str]:
    domains = {domain_for(item.url) for item in cluster.items}
    return {domain for domain in domains if not any(domain.endswith(press) for press in PRESS_RELEASE_DOMAINS)}


def syndicated_count(cluster: NewsCluster) -> int:
    return len({domain_for(item.url) for item in cluster.items}) - len(independent_domains(cluster))


def australian_relevance(cluster: NewsCluster) -> int:
    text = " ".join(f"{item.title} {item.summary} {item.source}" for item in cluster.items).lower()
    if any(term in text for term in ("australia", "australian", "sydney", "melbourne", "brisbane", "stan ", "aus ")):
        return 10
    if any(term in text for term in ("hyrox", "crossfit games", "zyzz")):
        return 6
    return 3


def source_credibility(cluster: NewsCluster) -> int:
    if any(item.source_type in {"official", "organisation", "journal"} for item in cluster.items):
        return 15
    if independent_domains(cluster):
        return 10
    return 6


def development_type(cluster: NewsCluster) -> str:
    text = " ".join(f"{item.title} {item.summary}" for item in cluster.items).lower()
    if any(term in text for term in ("correction", "corrected")):
        return "correction"
    if "erratum" in text:
        return "erratum"
    if "retraction" in text or "retracted" in text:
        return "retraction"
    if any(term in text for term in ("print issue", "reissue", "republished")):
        return "reissue_or_print_relist"
    if any(term in text for term in ("study", "research", "systematic review", "meta-analysis", "trial")):
        return "new_study"
    if "report" in text:
        return "new_report"
    if any(term in text for term in ("announce", "announced", "announcement", "launch", "launched", "release date")):
        return "announcement"
    # Entity-aware: only the *primary* item's own text is checked (not the
    # whole concatenated cluster), and a configured entity + result verb or
    # structured evidence is required — a generic "results"/"record"
    # substring match alone is not enough. See editorial_gates.classify_development_type.
    primary = primary_news_item(cluster)
    classification = editorial_gates.classify_development_type(
        f"{primary.title} {primary.summary}",
        gates_config(),
        evidence_links=[item.url for item in cluster.items],
    )
    if classification["development_type"] == "competition_result":
        return "competition_result"
    if any(term in text for term in ("challenge", "influencer", "youtube", "tiktok")):
        return "influencer_event"
    if any(term in text for term in ("opinion", "commentary")):
        return "opinion"
    return "other"


def conclusions_changed(cluster: NewsCluster) -> str:
    dtype = development_type(cluster)
    if dtype not in {"correction", "erratum", "retraction"}:
        return "n/a"
    text = " ".join(f"{item.title} {item.summary}" for item in cluster.items).lower()
    if any(term in text for term in ("conclusion changed", "changes the conclusion", "result changed", "major error")):
        return "true"
    if any(term in text for term in ("typographical", "minor correction", "does not affect", "no change")):
        return "false"
    return "unverified"


def topic_sensitivity(cluster: NewsCluster) -> str:
    text = " ".join(f"{item.title} {item.summary}" for item in cluster.items).lower()
    if any(term in text for term in ("death", "grief", "minor", "children", "legal", "lawsuit", "alleged", "accused", "political", "transgender", "medical", "disease")):
        return "high"
    if any(term in text for term in ("injury", "mental health", "supplement", "drug", "suspension")):
        return "medium"
    return "low"


def true_published_at(cluster: NewsCluster) -> str:
    """The canonical age-determining date for this story. For a
    research_media (ScienceDaily/EurekAlert-style) pickup, this is the
    study's own canonical publication date when resolvable, never the
    press-release/pickup date — a later news pickup must not reset a
    study's age. Falls back to the primary item's own published date
    when no canonical date can be resolved (unresolved DOI/PMID or
    network failure), matching the previous behaviour."""
    if is_research_media_item(cluster):
        canonical = canonical_research_date_info(cluster).get("canonical_published_at")
        if canonical:
            return canonical
    primary = primary_news_item(cluster)
    return primary.published or "unknown"


def age_hours_for_cluster(cluster: NewsCluster) -> float | None:
    dt = parse_dt(true_published_at(cluster))
    if not dt:
        return None
    return max((utc_now() - dt).total_seconds() / 3600, 0.0)


def independent_pickup_domains(cluster: NewsCluster) -> set[str]:
    primary_domain = domain_for(primary_news_item(cluster).url)
    feed_domains = {domain_for(item.url) for item in cluster.items if item.source.startswith("Google News:")}
    domains = independent_domains(cluster)
    return {domain for domain in domains if domain and domain != primary_domain and domain not in feed_domains}


def dimension_scores(cluster: NewsCluster) -> dict[str, int]:
    dtype = development_type(cluster)
    independent_count = len(independent_pickup_domains(cluster))
    age = age_hours_for_cluster(cluster)
    genuine = {
        "new_study": 75,
        "new_report": 70,
        "announcement": 80,
        "competition_result": 80,
        "influencer_event": 55,
        "opinion": 20,
        "other": 35,
    }.get(dtype, 10)
    if age is None:
        genuine = min(genuine, 40)
    elif age > 168:
        genuine = min(genuine, 30)
    if dtype in {"correction", "erratum", "retraction", "reissue_or_print_relist"} and conclusions_changed(cluster) != "true":
        genuine = min(genuine, 15)

    traction = min(100, independent_count * 35)
    fit = mfo_audience_affinity(cluster) * 5
    value = 75 if news_angle(cluster) else 40
    aus = australian_relevance(cluster) * 10
    credibility = min(100, source_credibility(cluster) * 6)
    if any("preprint" in f"{item.title} {item.summary}".lower() for item in cluster.items):
        credibility = min(credibility, 45)
    takeaway = 70 if any(term in " ".join(item.title.lower() for item in cluster.items) for term in ("workout", "training", "study", "research", "hyrox", "crossfit")) else 35
    return {
        "genuine_new_development": genuine,
        "independent_traction": traction,
        "mfo_audience_fit": fit,
        "original_value": value,
        "australian_relevance": aus,
        "evidence_quality": credibility,
        "practical_takeaway": takeaway,
    }


def weighted_base_score(scores: dict[str, int]) -> int:
    return round(
        scores["genuine_new_development"] * 0.25
        + scores["independent_traction"] * 0.20
        + scores["mfo_audience_fit"] * 0.20
        + scores["original_value"] * 0.10
        + scores["australian_relevance"] * 0.10
        + scores["evidence_quality"] * 0.10
        + scores["practical_takeaway"] * 0.05
    )


def recency_score(cluster: NewsCluster) -> int:
    published_times = [parse_dt(item.published) for item in cluster.items]
    published_times = [dt for dt in published_times if dt]
    if not published_times:
        return 5
    age_hours = (utc_now() - max(published_times)).total_seconds() / 3600
    if age_hours <= 24:
        return 10
    if age_hours <= 72:
        return 7
    if age_hours <= 168:
        return 4
    return 1


def newest_age_hours(cluster: NewsCluster) -> float | None:
    published_times = [parse_dt(item.published) for item in cluster.items]
    published_times = [dt for dt in published_times if dt]
    if not published_times:
        return None
    return (utc_now() - max(published_times)).total_seconds() / 3600


def is_profile_story(cluster: NewsCluster) -> bool:
    text = " ".join(f"{item.title} {item.summary}" for item in cluster.items).lower()
    return any(term in text for term in ("profile", "routine", "weekly workout", "how she", "what she", "inspiration", "inspiring"))


def has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def has_any_phrase(text: str, phrases: set[str]) -> bool:
    return any(has_phrase(text, phrase) for phrase in phrases)


def is_female_athlete_profile(cluster: NewsCluster) -> bool:
    text = " ".join(f"{item.title} {item.summary} {item.source}" for item in cluster.items).lower()
    women_source = any("women" in item.source.lower() or "womens" in domain_for(item.url) for item in cluster.items)
    return is_profile_story(cluster) and (women_source or bool(tokenize(text) & FEMALE_PROFILE_TERMS))


def has_mfo_exception_for_female_profile(cluster: NewsCluster) -> bool:
    text = " ".join(f"{item.title} {item.summary}" for item in cluster.items).lower()
    if australian_relevance(cluster) >= 10:
        return True
    if has_any_phrase(text, MAJOR_NEWS_TERMS):
        return True
    if has_any_phrase(text, MEN_TRAINING_TERMS):
        return True
    if has_any_phrase(text, UNIVERSAL_FINDING_TERMS):
        return True
    return False


def mfo_audience_affinity(cluster: NewsCluster) -> int:
    text = " ".join(f"{item.title} {item.summary} {item.source}" for item in cluster.items).lower()
    base = 20 if any(term in text for term in ("fitness", "strength", "gym", "hyrox", "crossfit", "bodybuilding", "training")) else 10
    if is_female_athlete_profile(cluster) and not has_mfo_exception_for_female_profile(cluster):
        return min(base, 7)
    return base


def is_research_media_item(cluster: NewsCluster) -> bool:
    return any(item.source_type == "research_media" for item in cluster.items)


def is_journal_news_item(cluster: NewsCluster) -> bool:
    text = " ".join(f"{item.title} {item.summary} {item.source_type} {item.source}" for item in cluster.items).lower()
    return any(item.source_type in {"journal", "research_media"} for item in cluster.items) or any(
        term in text for term in ("pubmed", "bmj sports medicine", "journal article", "doi:", "randomised trial", "randomized trial")
    )


def research_media_metadata(cluster: NewsCluster) -> dict[str, Any]:
    dois: list[str] = []
    pmids: list[str] = []
    journals: list[str] = []
    hype_terms: list[str] = []
    for item in cluster.items:
        text = f"{item.title} {item.summary}"
        for doi in DOI_RE.findall(text):
            clean = doi.rstrip(".,);")
            if clean.lower() not in {existing.lower() for existing in dois}:
                dois.append(clean)
        for pmid in extract_pmids(text):
            if pmid not in pmids:
                pmids.append(pmid)
        journal_match = re.search(r"\b(?:journal|published in)\s+([^.;]{4,90})", text, re.I)
        if journal_match:
            journal = journal_match.group(1).strip()
            if journal and journal.lower() not in {existing.lower() for existing in journals}:
                journals.append(journal)
        for term in ("breakthrough", "game changer", "miracle", "could change everything", "scientists discover"):
            if term in text.lower() and term not in hype_terms:
                hype_terms.append(term)
    return {
        "role": "public-interest research signal" if is_research_media_item(cluster) else None,
        "extracted_dois": dois,
        "extracted_pmids": pmids,
        "possible_journals": journals,
        "hype_terms": hype_terms,
        "verification_note": "Treat this as a public-interest alert. Verify the underlying paper via PubMed, DOI or publisher before writing." if is_research_media_item(cluster) else "",
    }


_CANONICAL_RESEARCH_DATE_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}


def canonical_research_date_info(cluster: NewsCluster) -> dict[str, Any]:
    """Resolve the true study publication date for a research_media
    (ScienceDaily/EurekAlert-style) cluster, distinguishing it from the
    publicity/pickup date. Falls back to "unresolved" (never crashes) on
    missing identifiers or network failure, so a scheduled scan is never
    blocked by a flaky Crossref/PubMed lookup. Memoized per-process by
    DOI/PMID set: this is called repeatedly for the same cluster across
    several scoring dimensions within one scan, and the result never
    changes for a given DOI/PMID within a single run."""
    if not is_research_media_item(cluster):
        return {
            "canonical_published_at": None,
            "publicity_published_at": None,
            "canonical_date_source": "not_applicable",
            "is_resurfaced_research": False,
            "resurfacing_reason": None,
        }
    primary = primary_news_item(cluster)
    publicity_published_at = primary.published
    metadata = research_media_metadata(cluster)
    cache_key = (tuple(sorted(metadata["extracted_dois"])), tuple(sorted(metadata["extracted_pmids"])))
    if cache_key in _CANONICAL_RESEARCH_DATE_CACHE:
        resolution = _CANONICAL_RESEARCH_DATE_CACHE[cache_key]
    else:
        resolution = editorial_gates.resolve_canonical_research_date(metadata["extracted_dois"], metadata["extracted_pmids"])
        _CANONICAL_RESEARCH_DATE_CACHE[cache_key] = resolution
    canonical_published_at = resolution["canonical_published_at"]
    is_resurfaced = False
    reason = None
    if canonical_published_at:
        canonical_dt = parse_dt(canonical_published_at)
        publicity_dt = parse_dt(publicity_published_at)
        if canonical_dt and publicity_dt:
            gap_days = (publicity_dt - canonical_dt).total_seconds() / 86400
            config = gates_config()
            stale_gap_days = config.get("research_resurfacing", {}).get("stale_gap_days", 14)
            if gap_days > stale_gap_days:
                is_resurfaced = True
                reason = (
                    f"Canonical publication date is {round(gap_days)} days before the publicity pickup date; "
                    "a later press release/news pickup is not a new development."
                )
    return {
        "canonical_published_at": canonical_published_at,
        "publicity_published_at": publicity_published_at,
        "canonical_date_source": resolution["canonical_date_source"],
        "is_resurfaced_research": is_resurfaced,
        "resurfacing_reason": reason,
    }


def is_irrelevant_entertainment_publicity(cluster: NewsCluster) -> bool:
    text = " ".join(f"{item.title} {item.summary} {item.source}" for item in cluster.items).lower()
    publicity = any(item.source_type in {"publicity", "brand"} for item in cluster.items)
    entertainment = any(term in text for term in ("stan ", "screening", "trailer", "streaming", "netflix", "documentary", "premiere", "screen realm", "season"))
    fitness = any(term in text for term in ("fitness", "bodybuilding", "gym", "strength", "training", "sport", "hyrox", "crossfit", "zyzz"))
    return publicity and entertainment and not fitness


def has_search_led_service_angle(cluster: NewsCluster) -> bool:
    text = " ".join(f"{item.title} {item.summary}" for item in cluster.items).lower()
    return any(term in text for term in ("guide", "how to", "explainer", "what it means", "schedule", "calendar", "results", "workout"))


def what_changed_now(cluster: NewsCluster) -> str | None:
    dtype = development_type(cluster)
    primary = primary_news_item(cluster)
    if dtype in {"announcement", "competition_result", "new_report", "influencer_event"}:
        return f"{dtype.replace('_', ' ')} published by {primary.source}."
    if dtype == "new_study" and not is_journal_news_item(cluster):
        return f"public report on a study from {primary.source}."
    if dtype in {"correction", "erratum", "retraction"} and conclusions_changed(cluster) == "true":
        return "correction/retraction appears to change a key result or conclusion."
    return None


def news_momentum(cluster: NewsCluster) -> int:
    domains = independent_domains(cluster)
    pickups = len({domain_for(item.url) for item in cluster.items})
    momentum = min(20, 5 + pickups * 4 + len(domains) * 3)
    age_hours = newest_age_hours(cluster)
    if is_profile_story(cluster) and (age_hours is None or age_hours > 72):
        return min(momentum, 6)
    return momentum


def score_news_cluster(cluster: NewsCluster, pages: list[MfoPage]) -> None:
    config = gates_config()
    text = " ".join(f"{item.title} {item.summary} {item.source}" for item in cluster.items).lower()
    primary = primary_news_item(cluster)
    dtype = development_type(cluster)
    changed = conclusions_changed(cluster)
    dimensions = dimension_scores(cluster)
    base_score = weighted_base_score(dimensions)
    score = base_score
    caps: list[str] = []
    penalties: list[str] = []
    kill_reasons: list[str] = []
    kill_reason_codes: list[str] = []
    verify_before_write = False
    independent_count = len(independent_pickup_domains(cluster))
    aus = australian_relevance(cluster)
    age = age_hours_for_cluster(cluster)
    changed_now = what_changed_now(cluster)
    date_info = canonical_research_date_info(cluster)
    if date_info["is_resurfaced_research"]:
        kill_reasons.append(date_info["resurfacing_reason"])
        kill_reason_codes.append("publicity_date_mistaken_for_research_date")

    entity_check = editorial_gates.classify_development_type(
        f"{primary.title} {primary.summary}",
        config,
        evidence_links=[item.url for item in cluster.items],
    )
    if entity_check.get("result_verb_without_entity"):
        kill_reasons.append(
            "Result-style language present (e.g. \"results\"/\"record\") but no confirmed "
            "competition entity, event date, or official source; not treated as a competition result."
        )
        kill_reason_codes.append("competition_entity_mismatch")

    overlap = find_news_overlap(cluster, pages)
    cluster.overlap = overlap
    exact_duplicate = False
    if exact_news_source_match(cluster, pages):
        exact_duplicate = True
        caps.append("exact story already covered: exclude")
        score = -1
        kill_reasons.append("Exact source URL already appears in the MFO archive.")
    elif overlap.page and overlap.score >= 0.35:
        penalties.append("strong archive overlap -15")
    elif overlap.page and overlap.score >= 0.2:
        penalties.append("weak archive overlap -5")

    if dtype in {"correction", "erratum", "retraction", "reissue_or_print_relist"} and changed != "true":
        caps.append("correction/erratum/retraction/relist without verified conclusion change: cap 25")
        score = min(score, 25)
    if true_published_at(cluster) == "unknown":
        caps.append("published_at unresolved: cap 40")
        score = min(score, 40)
        verify_before_write = True
        kill_reasons.append("Primary source publication date is unresolved.")
    if independent_count < 2:
        caps.append("fewer than two independent domains: cap 45")
        score = min(score, 45)
    if "preprint" in text and any(term in text for term in ("proves", "settled", "confirms")):
        caps.append("preprint/non-peer-reviewed presented as settled: cap 55")
        score = min(score, 55)
    sensitivity = topic_sensitivity(cluster)
    if sensitivity == "high" and (aus < 5 or dimensions["original_value"] < 60):
        caps.append("high sensitivity without strong Australian relevance/responsible value: cap 40")
        score = min(score, 40)
    if not changed_now:
        caps.append("what changed now unclear: cap 35")
        score = min(score, 35)
        kill_reasons.append("Could not clearly answer what changed today.")
    if is_journal_news_item(cluster):
        caps.append("research/journal article moved to Research Radar: cap 30")
        score = min(score, 30)
        kill_reasons.append("Academic or research-media item belongs in Research Radar, not breaking News Radar.")
    if dimensions["mfo_audience_fit"] < 50:
        caps.append("MFO audience fit below viable threshold: cap 35")
        score = min(score, 35)
        kill_reasons.append("MFO audience fit is too weak for the viable news list.")

    if not independent_pickup_domains(cluster) and any(domain_for(item.url) in PRESS_RELEASE_DOMAINS for item in cluster.items):
        penalties.append("commercial source without independent support -15")
    if any(item.source_type in {"publicity", "brand"} for item in cluster.items):
        penalties.append("press release or interested source; disclose commercial/institutional interest -5")
    if any(term in text for term in ("alleged", "allegation", "accused", "controversy")) and independent_count < 2:
        penalties.append("unverified allegation -25")
    if any(term in text for term in ("medical", "cure", "treatment", "disease")) and independent_count < 2:
        penalties.append("medical claim based only on publicity material -20")
    if recency_score(cluster) == 1:
        penalties.append("old story presented as new -25")
    if age is not None and age > 72:
        penalties.append("ordinary news older than 72 hours -15")
    if age is not None and age > 168 and not has_search_led_service_angle(cluster):
        caps.append("older than seven days without new development or service angle: cap 25")
        score = min(score, 25)
        kill_reasons.append("Story is more than seven days old without a new development or strong service angle.")
        kill_reason_codes.append("canonical_source_too_old")

    freshness_category = {"competition_result": "competition_result", "announcement": "official_announcement"}.get(dtype)
    if freshness_category:
        # No automatic textual exception here: has_search_led_service_angle's
        # keyword list includes "results", which would defeat this gate for
        # almost every genuine competition-result story. Per the brief, only
        # a specific, independently verifiable new event should exempt a
        # stale competition result/announcement, and no such signal is
        # available here beyond the category window itself.
        freshness_check = editorial_gates.freshness_gate(freshness_category, age, config)
        if freshness_check["status"] == "fail":
            caps.append(f"outside {freshness_category} freshness window ({age and round(age, 1)}h): exclude")
            score = min(score, 20)
            kill_reasons.append(f"Story is outside the {freshness_category.replace('_', ' ')} freshness window.")
            kill_reason_codes.append(freshness_check["kill_reason"] or "canonical_source_too_old")
    if is_profile_story(cluster) and (newest_age_hours(cluster) is None or (newest_age_hours(cluster) or 0) > 72):
        penalties.append("stale profile pickup, not current news -20")
    if is_female_athlete_profile(cluster) and not has_mfo_exception_for_female_profile(cluster):
        penalties.append("female athlete profile without clear MFO exception -25")
    if is_irrelevant_entertainment_publicity(cluster):
        caps.append("entertainment publicity unrelated to fitness: cap 20")
        score = min(score, 20)
        kill_reasons.append("Entertainment publicity is not sufficiently related to MFO fitness coverage.")

    if score >= 0:
        for penalty in penalties:
            amount_match = re.search(r"-(\d+)", penalty)
            if amount_match:
                score -= int(amount_match.group(1))
    final_score = -1 if exact_duplicate else max(min(round(score), 100), 0)
    if final_score < 40 and not kill_reasons:
        kill_reasons.append("Final score below editorial threshold.")
    confidence = "low" if verify_before_write or true_published_at(cluster) == "unknown" else "medium"
    if independent_count >= 2 and not verify_before_write and dtype not in {"correction", "erratum", "retraction", "reissue_or_print_relist"}:
        confidence = "high"
    recommendation = "pitch" if final_score >= 80 else "consider" if final_score >= 40 else "skip"
    if recommendation == "skip" and not kill_reasons:
        kill_reasons.append("Weak hook, fit, provenance or traction after caps and penalties.")

    cluster.score = final_score
    cluster.penalties = penalties
    cluster.score_json = {
        "final_score": final_score if final_score >= 0 else 0,
        "base_score": base_score,
        "audience_momentum": news_momentum(cluster),
        "editorial_opportunity_score": final_score if final_score >= 0 else 0,
        "editorial_score_breakdown": {
            "freshness": recency_score(cluster) * 10,
            "mfo_audience_fit": dimensions["mfo_audience_fit"],
            "strength_of_story_angle": dimensions["original_value"],
            "practical_usefulness": dimensions["practical_takeaway"],
            "primary_evidence_quality": dimensions["evidence_quality"],
            "australian_relevance": dimensions["australian_relevance"],
            "archive_risk": 100 if exact_news_source_match(cluster, pages) else 60 if overlap.page and overlap.score >= 0.35 else 10,
            "estimated_production_effort": 60 if sensitivity == "high" else 35 if is_journal_news_item(cluster) else 25,
        },
        "caps_applied": caps,
        "penalties_applied": penalties,
        "development_type": "resurfaced_research" if date_info["is_resurfaced_research"] else dtype,
        "canonical_published_at": date_info["canonical_published_at"],
        "publicity_published_at": date_info["publicity_published_at"],
        "canonical_date_source": date_info["canonical_date_source"],
        "is_resurfaced_research": date_info["is_resurfaced_research"],
        "resurfacing_reason": date_info["resurfacing_reason"],
        "conclusions_changed": changed,
        "what_happened": primary.summary or "No abstract/notice text available; title is unverified.",
        "what_changed_now": changed_now or "No clear current development found.",
        "why_news_now": f"Published {fmt_datetime(primary.published)}; first seen by scanner is stored separately. First-seen is not treated as publication date.",
        "true_published_at": true_published_at(cluster),
        "age_hours": round(age_hours_for_cluster(cluster) or 0, 1),
        "independent_domains": independent_count,
        "traction_note": f"{len(cluster.items)} pickups; {independent_count} independent domains after feed, source and syndication filtering.",
        "mfo_audience_fit": f"{dimensions['mfo_audience_fit']}/100",
        "australian_relevance_0_10": aus,
        "original_value_add": news_angle(cluster),
        "evidence_quality_note": f"{dimensions['evidence_quality']}/100; verify source details before writing.",
        "topic_sensitivity": sensitivity,
        "risks_or_bias": news_risk(cluster),
        "archive_overlap": overlap_label(overlap),
        "recommended_primary_source": primary.url,
        "supporting_sources": [item.url for item in cluster.items[1:6]],
        "research_media": research_media_metadata(cluster),
        "verify_before_write": verify_before_write,
        "kill_reasons": kill_reasons,
        "kill_reason_codes": kill_reason_codes,
        "confidence": confidence,
        "editor_recommendation": recommendation,
    }


def exact_news_source_match(cluster: NewsCluster, pages: list[MfoPage]) -> MfoPage | None:
    fingerprints = set()
    for item in cluster.items:
        fingerprints.update(fingerprints_for_values(item.url, item.title, item.summary))
    for page in pages:
        if fingerprints & page_fingerprints(page):
            return page
    return None


def find_news_overlap(cluster: NewsCluster, pages: list[MfoPage]) -> Overlap:
    fake = Observation(
        channel_source="news",
        channel_name=primary_news_item(cluster).source,
        video_title=" ".join(item.title for item in cluster.items[:3]),
        video_url=primary_news_item(cluster).url,
        video_id="",
        upload_datetime=primary_news_item(cluster).published,
        view_count=0,
        duration_seconds=None,
        video_type="standard",
        scan_timestamp=iso(utc_now()),
        age_hours=None,
        total_views_per_hour=None,
    )
    return find_overlap(fake, pages)


def news_angle(cluster: NewsCluster) -> str:
    text = " ".join(item.title for item in cluster.items).lower()
    if "research" in text or "study" in text:
        return "Explain what the evidence actually says and what a practical reader should or should not change."
    if "documentary" in text or "released" in text:
        return "Use the release as the news peg, then add context, Australian relevance and what the source material establishes."
    if "hyrox" in text or "crossfit" in text:
        return "Explain the event stakes, athlete relevance and practical training angle."
    return "Verify the development, explain why it matters now and add practical MFO context."


def news_risk(cluster: NewsCluster) -> str:
    text = " ".join(f"{item.title} {item.summary}" for item in cluster.items).lower()
    if any(term in text for term in ("alleged", "controversy", "accused")):
        return "Sensitive or disputed claim; needs careful attribution and independent confirmation."
    if any(item.source_type in {"publicity", "brand"} for item in cluster.items) and len(independent_domains(cluster)) < 2:
        return "May be publicity-led unless independently reported."
    return "Needs source verification before writing; do not treat headlines as fact."


def news_lead_payload(
    cluster: NewsCluster,
    pages: list[MfoPage],
    status: str,
    discovered_at: str | None = None,
) -> dict[str, Any]:
    primary = primary_news_item(cluster)
    score_json = cluster.score_json or {}
    overlap = cluster.overlap or find_news_overlap(cluster, pages)
    exact_page = exact_news_source_match(cluster, pages)
    supporting = [
        {"name": item.source, "url": item.url, "published_at": item.published}
        for item in cluster.items[1:]
    ]
    return {
        "lead_id": f"news:{cluster.key}",
        "scanner_type": "news",
        "source_name": primary.source,
        "source_category": primary.source_type,
        "title": primary.title,
        "source_url": primary.url,
        "published_at": primary.published,
        "discovered_at": discovered_at,
        "canonical_published_at": score_json.get("canonical_published_at"),
        "publicity_published_at": score_json.get("publicity_published_at"),
        "canonical_date_source": score_json.get("canonical_date_source"),
        "is_resurfaced_research": score_json.get("is_resurfaced_research", False),
        "resurfacing_reason": score_json.get("resurfacing_reason"),
        "actual_age_hours": score_json.get("age_hours"),
        "traction": {
            "pickups": len(cluster.items),
            "independent_domains": score_json.get("independent_domains"),
            "traction_note": score_json.get("traction_note"),
            "syndicated_copies": syndicated_count(cluster),
        },
        "scanner_score": score_json.get("final_score", cluster.score),
        "audience_momentum": score_json.get("audience_momentum", news_momentum(cluster)),
        "editorial_opportunity_score": score_json.get("editorial_opportunity_score", score_json.get("final_score", cluster.score)),
        "editorial_score_breakdown": score_json.get("editorial_score_breakdown", {}),
        "score_json": score_json,
        "likely_mfo_angle": score_json.get("original_value_add") or news_angle(cluster),
        "mfo_audience_fit": score_json.get("mfo_audience_fit"),
        "weakness_or_rejection_reason": "; ".join(score_json.get("kill_reasons") or score_json.get("penalties_applied") or []) or news_risk(cluster),
        "primary_source": {
            "name": primary.source,
            "url": primary.url,
            "published_at": primary.published,
        },
        "source_summary": primary.summary,
        "research_media": research_media_metadata(cluster),
        "archive_overlap": overlap_payload(overlap),
        "cannibalisation_risk": "strong" if overlap.page and overlap.score >= 0.35 else "weak" if overlap.page else "none",
        "topic_overlap_breakdown": topic_overlap_breakdown(f"{primary.source} {primary.title}", pages, exact_page),
        "imagery": {
            "available": None,
            "notes": "Check primary source/publicity assets manually.",
        },
        "evidence_links": [primary.url] + [item.url for item in cluster.items[1:]],
        "source_fingerprints": sorted({fingerprint for item in cluster.items for fingerprint in fingerprints_for_values(item.url, item.title, item.summary)}),
        "supporting_sources": supporting,
        "status": status,
        "kill_reason_codes": score_json.get("kill_reason_codes", []),
    }


def write_news_report(clusters: list[NewsCluster], errors: list[str], report_path: Path, mfo_index: dict[str, Any], conn: sqlite3.Connection) -> None:
    pages = mfo_pages_from_index(mfo_index)
    for cluster in clusters:
        score_news_cluster(cluster, pages)
    ranked = sorted([cluster for cluster in clusters if cluster.score >= 40], key=lambda cluster: cluster.score, reverse=True)
    excluded = [cluster for cluster in clusters if cluster.score < 0]
    skipped = sorted([cluster for cluster in clusters if 0 <= cluster.score < 40], key=lambda cluster: cluster.score)
    seen_at = iso(utc_now())
    with conn:
        upsert_news_clusters(conn, clusters, seen_at)

    lines = [
        "# MFO News Radar",
        "",
        f"- Scan timestamp: `{seen_at}`",
        f"- News candidates clustered: `{len(clusters)}`",
        f"- MFO archive index: `{len(pages)} pages` from `{mfo_index.get('source', 'not available')}` refreshed `{mfo_index.get('refreshed_at', 'unknown')}`",
        f"- Archive source fingerprints: `{mfo_index.get('source_fingerprint_count', 0)}`",
        "",
        "Creator popularity is not used here. A candidate needs a new development.",
        "",
        "## Ranked News Leads",
        "",
    ]
    if mfo_index.get("archive_warning"):
        lines[6:6] = [f"**Archive warning:** {mfo_index.get('archive_warning')}", ""]
    if not ranked:
        lines.extend(["No ranked news leads found.", ""])
    for index, cluster in enumerate(ranked[:15], 1):
        primary = primary_news_item(cluster)
        score_json = cluster.score_json or {}
        first_seen = first_seen_for(conn, cluster, seen_at)
        lines.extend(
            [
                f"### {index}. {primary.title}",
                "",
                f"- Score: {score_json.get('final_score', cluster.score)}/100 (base {score_json.get('base_score', 'n/a')}).",
                f"- Recommendation: {score_json.get('editor_recommendation', 'consider')}; confidence: {score_json.get('confidence', 'low')}.",
                f"- Development type: {score_json.get('development_type', 'other')}; conclusions changed: {score_json.get('conclusions_changed', 'n/a')}.",
                f"- What happened: {score_json.get('what_happened', 'No abstract/notice text available; title is unverified.')}",
                f"- Why it is news now: {score_json.get('why_news_now', '')} First seen by scanner {fmt_datetime(first_seen)}.",
                f"- Evidence interest is growing: {score_json.get('traction_note', '')} {syndicated_count(cluster)} likely syndicated copies.",
                f"- Australian relevance: {score_json.get('australian_relevance_0_10', australian_relevance(cluster))}/10.",
                f"- MFO audience fit: {score_json.get('mfo_audience_fit', '')}.",
                f"- What MFO can add: {score_json.get('original_value_add', news_angle(cluster))}",
                f"- Evidence quality: {score_json.get('evidence_quality_note', '')}",
                f"- Risks or bias: {score_json.get('risks_or_bias', news_risk(cluster))}",
                f"- Caps: {', '.join(score_json.get('caps_applied', [])) or 'none'}.",
                f"- Penalties: {', '.join(score_json.get('penalties_applied', [])) or 'none'}.",
                f"- Archive overlap: {score_json.get('archive_overlap', 'No obvious archive conflict found.')}",
                f"- Recommended primary source: [{primary.source}]({primary.url})",
                f"- Supporting sources: " + ", ".join(f"[{item.source}]({item.url})" for item in cluster.items[1:6]) if len(cluster.items) > 1 else "- Supporting sources: none found yet.",
                "",
            ]
        )

    if excluded:
        lines.extend(["## Already Covered And Excluded", ""])
        for cluster in excluded[:20]:
            primary = primary_news_item(cluster)
            score_json = cluster.score_json or {}
            reasons = score_json.get("kill_reasons") or cluster.penalties or ["exact story already covered"]
            lines.append(f"- [{primary.title}]({primary.url}) - {'; '.join(reasons)}.")
        lines.append("")

    if skipped:
        lines.extend(["## Skipped", ""])
        for cluster in skipped[:20]:
            primary = primary_news_item(cluster)
            score_json = cluster.score_json or {}
            reasons = score_json.get("kill_reasons") or ["Below editorial threshold."]
            lines.append(f"- [{primary.title}]({primary.url}) - score {score_json.get('final_score', cluster.score)}/100; {'; '.join(reasons)}.")
        lines.append("")

    if errors:
        lines.extend(["## Fetch Errors", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")

    lead_payloads: list[dict[str, Any]] = []
    lead_payloads.extend(news_lead_payload(cluster, pages, "ranked", first_seen_for(conn, cluster, seen_at)) for cluster in ranked)
    lead_payloads.extend(news_lead_payload(cluster, pages, "already_covered", first_seen_for(conn, cluster, seen_at)) for cluster in excluded)
    lead_payloads.extend(news_lead_payload(cluster, pages, "skipped", first_seen_for(conn, cluster, seen_at)) for cluster in skipped)
    write_json_payload(
        report_path.with_suffix(".json"),
        {
            "scanner_type": "news",
            "schema_version": 1,
            "generated_at": seen_at,
            "report_path": str(report_path),
            "lead_count": len(lead_payloads),
            "viable_count": len([lead for lead in lead_payloads if lead["status"] == "ranked"]),
            "errors": errors,
            "metadata": {
                "clusters": len(clusters),
                "mfo_archive_pages": len(pages),
                "mfo_archive_refreshed_at": mfo_index.get("refreshed_at"),
                "mfo_archive_source_fingerprint_count": mfo_index.get("source_fingerprint_count", 0),
                "mfo_archive_warning": mfo_index.get("archive_warning", ""),
            },
            "leads": lead_payloads,
        },
    )


def run_news_radar(
    db_path: Path,
    report_path: Path,
    sources_path: Path,
    queries_path: Path,
    mfo_index: dict[str, Any],
) -> list[NewsCluster]:
    items, errors = load_news_items(sources_path, queries_path)
    clusters = cluster_news_items(items)
    conn = connect_db(db_path)
    write_news_report(clusters, errors, report_path, mfo_index, conn)
    return clusters


def load_research_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Add research topic groups before running Research Radar.")
    config = load_json_object(path)
    if not isinstance(config.get("topic_groups"), list) or not config["topic_groups"]:
        raise ValueError("research_queries.json must contain a non-empty topic_groups list.")
    return config


def ncbi_delay_seconds() -> float:
    return 0.11 if os.environ.get("NCBI_API_KEY") else 0.34


def http_get_text_retry(url: str, timeout: int = 30, attempts: int = 3, delay: float = 0.5) -> str:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return http_get_text(url, timeout=timeout)
        except Exception as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay * (2**attempt))
    raise last_error or RuntimeError("request failed")


def ncbi_params(extra: dict[str, Any]) -> str:
    params: dict[str, Any] = {
        "tool": NCBI_TOOL_NAME,
        "retmode": "json",
    }
    email = os.environ.get("NCBI_EMAIL") or os.environ.get("CONTACT_EMAIL")
    if email:
        params["email"] = email
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    params.update(extra)
    return urlencode(params)


def pubmed_search(query: str, start: datetime, end: datetime, max_results: int = 40) -> list[str]:
    term = f"({query}) AND ({start:%Y/%m/%d}:{end:%Y/%m/%d}[pdat])"
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + ncbi_params(
        {
            "db": "pubmed",
            "term": term,
            "retmax": max_results,
            "sort": "pub date",
            "retmode": "json",
        }
    )
    time.sleep(ncbi_delay_seconds())
    data = json.loads(http_get_text_retry(url))
    return [str(pmid) for pmid in data.get("esearchresult", {}).get("idlist", [])]


def pubmed_fetch(pmids: list[str]) -> str:
    if not pmids:
        return ""
    params = ncbi_params({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
    params = params.replace("retmode=json", "retmode=xml")
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + params
    time.sleep(ncbi_delay_seconds())
    return http_get_text_retry(url)


def text_at(parent: ElementTree.Element, path: str) -> str | None:
    found = parent.find(path)
    if found is None or found.text is None:
        return None
    text = strip_html(" ".join(found.itertext()))
    return text or None


def pubmed_date_to_iso(element: ElementTree.Element | None) -> str | None:
    if element is None:
        return None
    year = text_at(element, "Year")
    month = text_at(element, "Month") or "01"
    day = text_at(element, "Day") or "01"
    if not year:
        medline = text_at(element, "MedlineDate")
        year_match = re.search(r"\d{4}", medline or "")
        year = year_match.group(0) if year_match else None
    if not year:
        return None
    month_map = {
        "jan": "01",
        "feb": "02",
        "mar": "03",
        "apr": "04",
        "may": "05",
        "jun": "06",
        "jul": "07",
        "aug": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dec": "12",
    }
    if not str(month).isdigit():
        month = month_map.get(str(month).lower()[:3], "01")
    if not str(day).isdigit():
        day = "01"
    if not str(month).isdigit():
        month = "01"
    try:
        year_i = int(str(year).strip())
        month_i = int(str(month).strip() or "1")
        day_i = int(str(day).strip() or "1")
    except ValueError:
        return None
    return f"{year_i:04d}-{month_i:02d}-{day_i:02d}T00:00:00Z"


def extract_pubmed_articles(xml_text: str, topic_by_pmid: dict[str, str]) -> list[ResearchPaper]:
    if not xml_text.strip():
        return []
    root = ElementTree.fromstring(xml_text)
    papers: list[ResearchPaper] = []
    for article in root.findall(".//PubmedArticle"):
        medline = article.find("MedlineCitation")
        pubmed_data = article.find("PubmedData")
        if medline is None:
            continue
        pmid = text_at(medline, "PMID")
        article_node = medline.find("Article")
        if not pmid or article_node is None:
            continue
        title = text_at(article_node, "ArticleTitle") or "Untitled PubMed record"
        journal = text_at(article_node, "Journal/Title") or text_at(article_node, "Journal/ISOAbbreviation")
        abstract_parts = [strip_html(" ".join(node.itertext())) for node in article_node.findall("Abstract/AbstractText")]
        abstract = "\n".join(part for part in abstract_parts if part) or None
        authors = []
        for author in article_node.findall("AuthorList/Author")[:8]:
            last = text_at(author, "LastName")
            fore = text_at(author, "ForeName") or text_at(author, "Initials")
            collective = text_at(author, "CollectiveName")
            name = collective or " ".join(part for part in [fore, last] if part)
            if name:
                authors.append(name)
        doi = None
        for aid in article.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi" and aid.text:
                doi = aid.text.strip()
                break
        publication_types = [
            strip_html(" ".join(node.itertext()))
            for node in article_node.findall("PublicationTypeList/PublicationType")
        ]
        pub_date = pubmed_date_to_iso(article_node.find("Journal/JournalIssue/PubDate"))
        epub_date = None
        for date_node in article_node.findall("ArticleDate"):
            epub_date = pubmed_date_to_iso(date_node)
            if epub_date:
                break
        indexed_at = pubmed_date_to_iso(pubmed_data.find("History/PubMedPubDate[@PubStatus='pubmed']") if pubmed_data is not None else None)
        paper = ResearchPaper(
            pmid=pmid,
            doi=doi,
            topic_group=topic_by_pmid.get(pmid, "Research"),
            title=title,
            journal=journal,
            authors=authors,
            abstract=abstract,
            publication_date=pub_date,
            electronic_publication_date=epub_date,
            indexed_at=indexed_at,
            publication_types=publication_types,
            pubmed_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            publisher_url=f"https://doi.org/{doi}" if doi else None,
        )
        enrich_research_from_text(paper)
        papers.append(paper)
    return papers


def enrich_research_from_text(paper: ResearchPaper) -> None:
    text = paper.abstract or ""
    lower = text.lower()
    warnings: list[str] = []
    sample = re.search(r"\b(?:n\s*=\s*)?([\d]{1,3}(?:,\d{3})+|\d{2,7})\s+(men|women|adults|participants|patients|subjects|athletes|trials|studies)\b", text, re.I)
    if not sample:
        sample = re.search(r"\bn\s*=\s*([\d]{1,3}(?:,\d{3})+|\d{2,7})\b", text, re.I)
    paper.sample_size = f"n = {sample.group(1)}" if sample else None
    if not sample:
        warnings.append("sample_size not reliably identified in abstract")

    pop = re.search(r"(?:included|enrolled|randomi[sz]ed|recruited|analysed|analyzed)\s+([\d,]+\s+)?([^.;]{8,140}?(?:men|women|adults|participants|patients|subjects|athletes|trials|studies)[^.;]{0,80})", text, re.I)
    paper.study_population = re.sub(r"\s+", " ", " ".join(part for part in pop.groups() if part)).strip() if pop else None
    if not paper.study_population:
        warnings.append("population not reliably identified in abstract")

    intervention = re.search(r"(?:assigned to|received|performed|underwent|intervention(?:s)? (?:included|was|were)?)\s+([^.;]{8,160})", text, re.I)
    paper.intervention = intervention.group(1).strip() if intervention else None
    if not paper.intervention:
        warnings.append("intervention not reliably identified in abstract")

    comparison = re.search(r"(?:compared with|compared to|versus|vs\.?)\s+([^.;]{5,120})", text, re.I)
    paper.comparison = comparison.group(1).strip() if comparison else None
    if not paper.comparison:
        warnings.append("comparison not reliably identified in abstract")

    duration = re.search(r"\b(?:for|over|during)\s+(\d+(?:\.\d+)?\s*(?:weeks|months|days))\b", text, re.I)
    paper.duration = duration.group(1) if duration else None
    if not paper.duration and re.search(r"\b\d+\s*years\b", text, re.I):
        warnings.append("duration mentions years but may describe age/follow-up; left unknown")
    elif not paper.duration:
        warnings.append("duration not reliably identified in abstract")

    result_sentence = None
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if any(term in sentence.lower() for term in ("result", "increased", "decreased", "improved", "reduced", "associated", "significant")):
            result_sentence = sentence.strip()
            break
    paper.primary_finding = result_sentence or None
    if not result_sentence:
        warnings.append("primary finding not reliably identified in abstract")
    effect = re.search(r"(\b\d+(?:\.\d+)?\s*%|\bmean difference[^.;]+|95%\s*CI[^.;]+|p\s*[<=>]\s*0\.\d+)", text, re.I)
    paper.effect_size = effect.group(0) if effect else None
    if not effect:
        warnings.append("effect size not reliably identified in abstract")
    paper.funding = "commercial or funding information requires full-text/manual check"
    if any(term in lower for term in ("funded by", "supported by", "grant", "sponsor")):
        paper.funding = "funding mentioned in abstract; verify details before writing"
    paper.conflicts = "not reported in abstract"
    if any(term in lower for term in ("conflict of interest", "competing interest", "employee of", "consultant")):
        paper.conflicts = "conflict/competing interest language appears in abstract; verify disclosure"
    paper.extraction_warnings = warnings


def enrich_research_reliable_metadata(paper: ResearchPaper, errors: list[str]) -> None:
    if not paper.doi:
        return
    try:
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(
            {"query": f"DOI:{paper.doi}", "format": "json", "pageSize": 1}
        )
        data = json.loads(http_get_text_retry(url, timeout=15, attempts=2))
        results = data.get("resultList", {}).get("result", [])
        if results:
            item = results[0]
            if paper.publisher_url is None and item.get("doi"):
                paper.publisher_url = f"https://doi.org/{item['doi']}"
            if item.get("isOpenAccess") is not None:
                paper.full_text_available = str(item.get("isOpenAccess")).upper() == "Y"
    except Exception as exc:
        errors.append(f"Europe PMC {paper.pmid}: {exc}")


def study_type_for(paper: ResearchPaper) -> str:
    text = " ".join([paper.title, paper.abstract or "", " ".join(paper.publication_types)]).lower()
    if "study protocol" in text or "protocol for" in text:
        return "protocol"
    if any(term in text for term in ("mice", "mouse", "rat ", "rats", "cell", "horse", "horses", "veterinary", "porcine", "ovine")):
        return "animal_or_cell_study"
    if "systematic review" in text or "meta-analysis" in text:
        return "systematic_review_meta_analysis"
    if "randomized" in text or "randomised" in text or "randomized controlled trial" in text:
        return "randomised_controlled_trial"
    if "guideline" in text or "consensus" in text:
        return "guideline_or_consensus"
    if "cohort" in text or "cross-sectional" in text or "observational" in text:
        return "observational_human_study"
    if "editorial" in text or "commentary" in text or "letter" in text:
        return "editorial_commentary_letter"
    return "human_study_or_unclear"


def has_direct_mfo_research_action(text: str) -> bool:
    return any(
        term in text
        for term in (
            "resistance training",
            "strength training",
            "strength and conditioning",
            "hypertrophy",
            "muscle strength",
            "lean mass",
            "sarcopenia",
            "testosterone",
            "erectile",
            "weight loss",
            "obesity",
            "glp-1",
            "protein",
            "creatine",
            "running",
            "vo2",
            "hiit",
            "sleep",
            "recovery",
            "back pain",
            "knee",
            "shoulder",
            "tendon",
            "sports injury",
            "physical activity",
            "exercise training",
            "training volume",
            "training frequency",
            "sports performance",
            "athletic performance",
            "powerlifting",
            "weightlifting",
            "sprint performance",
        )
    )


def is_specialist_clinical_research(text: str) -> bool:
    return any(
        term in text
        for term in (
            "cancer",
            "transplant",
            "dementia",
            "rheumatoid",
            "chronic kidney disease",
            "ckd",
            "surgery",
            "arthroplasty",
            "hematopoietic",
            "oncology",
            "diabetes care",
            "healthcare audit",
            "cardiac rehabilitation text message",
            "tacs",
            "transcranial alternating current stimulation",
            "paediatric",
            "pediatric",
        )
    )


def research_editorial_category(paper: ResearchPaper) -> str:
    text = " ".join([paper.title, paper.abstract or "", paper.journal or "", " ".join(paper.publication_types)]).lower()
    study_type = study_type_for(paper)
    if study_type == "protocol":
        return "protocol_without_results"
    if study_type == "animal_or_cell_study":
        return "animal_or_laboratory_research"
    if any(term in text for term in ("secondary analysis", "post hoc", "exploratory")):
        return "exploratory_secondary_analysis"
    if any(term in text for term in ("audit", "healthcare utilisation", "healthcare utilization", "care quality", "claims data")):
        return "geographically_specific_healthcare_audit"
    if is_specialist_clinical_research(text):
        return "specialist_clinical_procedure"
    if has_direct_mfo_research_action(text):
        return "practical_fitness_finding"
    if any(term in text for term in ("mortality", "sleep", "cardiovascular", "diabetes", "depression", "alcohol")):
        return "newsworthy_health_finding"
    return "academic_noise"


def research_age_hours(paper: ResearchPaper) -> float | None:
    dt = parse_dt(paper.electronic_publication_date or paper.publication_date)
    if not dt:
        return None
    return max((utc_now() - dt).total_seconds() / 3600, 0.0)


def research_public_interest(paper: ResearchPaper) -> dict[str, Any]:
    payload = {"matched": False, "matches": [], "independent_domains": 0, "note": "No public-interest match found."}
    news = {}
    try:
        news = json.loads(NEWS_JSON_PATH.read_text(encoding="utf-8")) if NEWS_JSON_PATH.exists() else {}
    except Exception:
        news = {}
    paper_terms = tokenize(paper.title)
    domains: set[str] = set()
    matches = []
    for lead in news.get("leads", []) if isinstance(news.get("leads"), list) else []:
        haystack = " ".join(
            str(value or "")
            for value in [
                lead.get("title"),
                lead.get("source_url"),
                lead.get("source_summary"),
                json.dumps(lead.get("score_json", {}), sort_keys=True) if isinstance(lead.get("score_json"), dict) else "",
                json.dumps(lead.get("research_media", {}), sort_keys=True) if isinstance(lead.get("research_media"), dict) else "",
                " ".join(lead.get("evidence_links", []) if isinstance(lead.get("evidence_links"), list) else []),
            ]
        )
        doi_match = bool(paper.doi and paper.doi.lower() in haystack.lower())
        pmid_match = bool(paper.pmid and paper.pmid in haystack)
        title_terms = tokenize(str(lead.get("title") or ""))
        title_similarity = len(paper_terms & title_terms) / max(len(paper_terms | title_terms), 1)
        if doi_match or pmid_match or title_similarity >= 0.65:
            url = str(lead.get("source_url") or "")
            domains.add(domain_for(url))
            matches.append({"lead_id": lead.get("lead_id"), "title": lead.get("title"), "url": url, "similarity": round(title_similarity, 2)})
    if matches:
        payload.update(
            {
                "matched": True,
                "matches": matches[:5],
                "independent_domains": len({domain for domain in domains if domain and "pubmed" not in domain}),
                "note": f"{len(matches)} public-interest/news matches found; paper remains the primary evidence source.",
            }
        )
    return payload


def score_research_paper(paper: ResearchPaper, pages: list[MfoPage], config: dict[str, Any]) -> None:
    text = " ".join([paper.title, paper.abstract or "", paper.journal or "", " ".join(paper.publication_types)]).lower()
    study_type = study_type_for(paper)
    score_breakdown = {
        "mfo_audience_relevance": 0,
        "evidence_strength": 0,
        "practical_importance": 0,
        "novelty": 0,
        "freshness": 0,
        "public_interest": 0,
        "mfo_interpretation_opportunity": 0,
        "australian_relevance": 0,
    }
    direct_action = has_direct_mfo_research_action(text)
    specialist_clinical = is_specialist_clinical_research(text)
    editorial_category = research_editorial_category(paper)
    if direct_action and not specialist_clinical:
        score_breakdown["mfo_audience_relevance"] = 18
    elif direct_action:
        score_breakdown["mfo_audience_relevance"] = 12
    elif any(term in text for term in ("exercise", "fitness", "sport")):
        score_breakdown["mfo_audience_relevance"] = 9
    else:
        score_breakdown["mfo_audience_relevance"] = 4
    journal_boosts = config.get("journal_boosts", []) if isinstance(config.get("journal_boosts"), list) else []
    journal_relevance_boost = 0
    if paper.journal and direct_action:
        journal_lower = paper.journal.lower()
        for entry in journal_boosts:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").lower()
            if name and (name in journal_lower or journal_lower in name):
                journal_relevance_boost = max(journal_relevance_boost, int(entry.get("mfo_relevance_boost", 0) or 0))
    if journal_relevance_boost:
        score_breakdown["mfo_audience_relevance"] = min(20, score_breakdown["mfo_audience_relevance"] + journal_relevance_boost)
    score_breakdown["evidence_strength"] = {
        "systematic_review_meta_analysis": 19,
        "randomised_controlled_trial": 18,
        "guideline_or_consensus": 17,
        "observational_human_study": 12,
        "human_study_or_unclear": 9,
        "protocol": 4,
        "animal_or_cell_study": 3,
        "editorial_commentary_letter": 2,
    }.get(study_type, 8)
    score_breakdown["practical_importance"] = 16 if editorial_category == "practical_fitness_finding" else 10 if editorial_category == "newsworthy_health_finding" else 4
    score_breakdown["novelty"] = 12 if any(term in text for term in ("novel", "first", "unexpected", "challeng", "compared", "new")) else 7
    age = research_age_hours(paper)
    practical_research_window = gates_config().get("freshness_windows", {}).get("practical_research", {}).get("hours", 336)
    if age is None:
        score_breakdown["freshness"] = 3
    elif age <= 72:
        score_breakdown["freshness"] = 10
    elif age <= practical_research_window:
        score_breakdown["freshness"] = 7
    else:
        score_breakdown["freshness"] = 3
    paper.public_interest = research_public_interest(paper)
    score_breakdown["public_interest"] = min(10, int(paper.public_interest.get("independent_domains") or 0) * 4)
    score_breakdown["mfo_interpretation_opportunity"] = 5 if paper.abstract else 2
    score_breakdown["australian_relevance"] = 5 if any(term in text for term in ("australia", "australian", "sydney", "melbourne")) else 1
    penalties: list[str] = []
    configured_penalties = config.get("penalties", {}) if isinstance(config.get("penalties"), dict) else {}
    def penalty(name: str, fallback: int) -> None:
        amount = int(configured_penalties.get(name, fallback))
        penalties.append(f"{name.replace('_', ' ')} -{amount}")
    if study_type == "animal_or_cell_study":
        penalty("animal_only", 25)
    if study_type == "protocol":
        penalties.append("study protocol without results -25")
    if "preprint" in text:
        penalty("preprint", 20)
    if study_type == "editorial_commentary_letter":
        penalty("editorial_or_commentary", 25)
    if not paper.abstract:
        penalty("missing_abstract", 20)
    sample_number = re.search(r"\d[\d,]*", paper.sample_size or "")
    if sample_number and int(sample_number.group(0).replace(",", "")) < 30 and study_type != "systematic_review_meta_analysis":
        penalty("extremely_small_sample", 15)
    if any(term in text for term in ("biomarker", "surrogate", "mechanistic")) and not any(term in text for term in ("strength", "mortality", "injury", "body composition", "lean mass")):
        penalty("surrogate_outcome", 10)
    if study_type == "observational_human_study" and any(term in text for term in ("caused", "prevents", "proves")):
        penalty("observational_causal_claim", 15)
    if any(term in text for term in ("industry", "sponsor", "funded by", "employee of", "supplement company", "pharmaceutical")):
        penalty("commercial_conflict", 10)
    if specialist_clinical and not any(term in text for term in ("men", "male", "resistance training", "strength training", "sarcopenia", "lean mass", "physical activity", "exercise training")):
        penalties.append("specialist clinical topic with weak MFO action -20")
    if not direct_action:
        penalties.append("no direct MFO training or men's-health action -20")
    category_caps = {
        "specialist_clinical_procedure": 52,
        "exploratory_secondary_analysis": 48,
        "animal_or_laboratory_research": 35,
        "protocol_without_results": 35,
        "geographically_specific_healthcare_audit": 42,
        "academic_noise": 38,
    }
    male_application = any(has_phrase(text, term) for term in ("men", "male", "both sexes", "both men and women"))
    if any(term in text for term in ("female lower", "women only", "female participants", "pregnant women", "postmenopausal women")) and not male_application:
        penalties.append("female-only study without clear male-audience application -20")
    fake = Observation(
        channel_source="research",
        channel_name=paper.journal or "PubMed",
        video_title=paper.title,
        video_url=paper.pubmed_url or "",
        video_id="",
        upload_datetime=paper.publication_date,
        view_count=0,
        duration_seconds=None,
        video_type="standard",
        scan_timestamp=iso(utc_now()),
        age_hours=None,
        total_views_per_hour=None,
    )
    overlap = find_overlap(fake, pages)
    paper.archive_overlap = overlap
    if exact_research_archive_match(paper, pages):
        penalties.append("exact MFO archive duplicate -100")
        paper.rejection_reasons = ["Exact DOI, PMID, PubMed or publisher URL already appears in MFO archive."]
    elif overlap.page and overlap.score >= 0.35:
        penalty("archive_overlap", 20)
    if any(term in text for term in ("breakthrough", "game changer", "miracle")):
        penalty("press_release_hype", 15)
    raw_score = sum(score_breakdown.values())
    for item in penalties:
        match = re.search(r"-(\d+)", item)
        if match:
            raw_score -= int(match.group(1))
    paper.score = max(0, min(100, round(raw_score)))
    if editorial_category in category_caps:
        paper.score = min(paper.score, category_caps[editorial_category])
    if score_breakdown["mfo_audience_relevance"] < 14:
        paper.score = min(paper.score, 49)
    thresholds = config.get("thresholds", {}) if isinstance(config.get("thresholds"), dict) else {}
    viable_threshold = int(thresholds.get("viable", 65))
    hold_threshold = int(thresholds.get("hold", 50))
    if paper.score >= viable_threshold:
        paper.status = "viable"
        paper.recommended_status = "pitch"
    elif paper.score >= hold_threshold:
        paper.status = "hold"
        paper.recommended_status = "hold"
    else:
        paper.status = "rejected"
        paper.recommended_status = "reject"
    paper.score_breakdown = score_breakdown
    paper.score_breakdown["editorial_category_score"] = {
        "practical_fitness_finding": 20,
        "newsworthy_health_finding": 13,
        "useful_service_explainer": 12,
        "specialist_clinical_procedure": 4,
        "exploratory_secondary_analysis": 3,
        "animal_or_laboratory_research": 2,
        "protocol_without_results": 1,
        "geographically_specific_healthcare_audit": 2,
        "academic_noise": 1,
    }.get(editorial_category, 1)
    paper.penalties = penalties
    reasons = paper.rejection_reasons or []
    if paper.status == "rejected" and not reasons:
        reasons.append("Research score below editorial threshold.")
    paper.rejection_reasons = reasons


def exact_research_archive_match(paper: ResearchPaper, pages: list[MfoPage]) -> MfoPage | None:
    identifiers = fingerprints_for_values(paper.pubmed_url, paper.publisher_url, paper.title, paper.abstract, pmid=paper.pmid, doi=paper.doi)
    for page in pages:
        if identifiers & page_fingerprints(page):
            return page
    return None


def research_kill_reason_codes(paper: ResearchPaper) -> list[str]:
    if paper.status != "rejected":
        return []
    codes: list[str] = []
    reasons_text = " ".join(paper.rejection_reasons or paper.penalties or []).lower()
    if "exact" in reasons_text and "archive" in reasons_text:
        codes.append("archive_cannibalisation")
    if "archive overlap" in reasons_text:
        codes.append("archive_cannibalisation")
    if "missing abstract" in reasons_text or "preprint" in reasons_text:
        codes.append("insufficient_primary_evidence")
    if "no direct mfo training" in reasons_text or "specialist clinical" in reasons_text:
        codes.append("wrong_audience")
    if not codes:
        codes.append("score_below_threshold")
    return codes


def research_payload(paper: ResearchPaper, pages: list[MfoPage] | None = None) -> dict[str, Any]:
    study_type = study_type_for(paper)
    exact_page = exact_research_archive_match(paper, pages) if pages else None
    return {
        "lead_id": f"research:{paper.pmid}" if paper.pmid else f"research:{paper.doi}",
        "scanner_type": "research",
        "topic_group": paper.topic_group,
        "source_name": paper.journal or "PubMed",
        "source_category": "research",
        "title": paper.title,
        "source_url": paper.pubmed_url,
        "published_at": paper.electronic_publication_date or paper.publication_date,
        "discovered_at": paper.indexed_at,
        "publication_dates": {
            "formal_publication": paper.publication_date,
            "electronic_publication": paper.electronic_publication_date,
            "pubmed_indexed": paper.indexed_at,
        },
        "study_type": study_type,
        "research_editorial_category": research_editorial_category(paper),
        "population": paper.study_population,
        "sample_size": paper.sample_size,
        "intervention": paper.intervention,
        "comparison": paper.comparison,
        "duration": paper.duration,
        "key_result": paper.primary_finding,
        "effect_size_or_numerical_result": paper.effect_size,
        "evidence_strength_explanation": evidence_strength_note(paper),
        "limitations": limitations_note(paper),
        "score": paper.score,
        "scanner_score": paper.score,
        "audience_momentum": (paper.public_interest or {}).get("independent_domains", 0),
        "editorial_opportunity_score": paper.score,
        "editorial_score_breakdown": paper.score_breakdown or {},
        "score_breakdown": paper.score_breakdown or {},
        "penalties": paper.penalties or [],
        "commercial_funding_or_disclosure_flags": disclosure_note(paper),
        "likely_mfo_angle": research_angle(paper),
        "what_mfo_adds": research_value_add(paper),
        "mfo_audience_fit": f"{(paper.score_breakdown or {}).get('mfo_audience_relevance', 0)}/20",
        "weakness_or_rejection_reason": "; ".join(paper.rejection_reasons or paper.penalties or []),
        "archive_overlap": overlap_payload(paper.archive_overlap),
        "topic_overlap_breakdown": topic_overlap_breakdown(f"{paper.journal or ''} {paper.title}", pages or [], exact_page),
        "facts_requiring_manual_verification": facts_to_verify(paper),
        "extraction_warnings": paper.extraction_warnings or [],
        "imagery": {
            "available": None,
            "notes": "Consider a simple MFO chart/table from reported outcomes; verify rights before using publisher figures.",
        },
        "estimated_production_effort_minutes": 90 if paper.status == "viable" else 45,
        "recommended_status": paper.recommended_status,
        "status": paper.status,
        "kill_reason_codes": research_kill_reason_codes(paper),
        "primary_source": {
            "name": "PubMed",
            "url": paper.pubmed_url,
            "pmid": paper.pmid,
            "doi": paper.doi,
            "publisher_url": paper.publisher_url,
        },
        "evidence_links": [url for url in [paper.pubmed_url, paper.publisher_url] if url],
        "source_fingerprints": sorted(fingerprints_for_values(paper.pubmed_url, paper.publisher_url, paper.title, paper.abstract, pmid=paper.pmid, doi=paper.doi)),
        "public_interest": paper.public_interest or {},
        "authors": paper.authors,
        "abstract": paper.abstract,
        "publication_types": paper.publication_types,
        "funding": paper.funding,
        "conflicts": paper.conflicts,
        "full_text_available": paper.full_text_available,
    }


def evidence_strength_note(paper: ResearchPaper) -> str:
    study_type = study_type_for(paper).replace("_", " ")
    return f"{study_type}; sample size {paper.sample_size or 'unknown from abstract'}; publication types: {', '.join(paper.publication_types) or 'not yet indexed'}."


def limitations_note(paper: ResearchPaper) -> str:
    notes = []
    if not paper.abstract:
        notes.append("No abstract available.")
    if "observational" in study_type_for(paper):
        notes.append("Observational design cannot prove causation.")
    if not paper.sample_size:
        notes.append("Sample size not reliably reported in abstract.")
    if paper.extraction_warnings:
        notes.append("Some abstract fields could not be extracted reliably.")
    if not notes:
        notes.append("Requires full-text review before giving practical advice.")
    return " ".join(notes)


def disclosure_note(paper: ResearchPaper) -> str:
    flags = [value for value in [paper.funding, paper.conflicts] if value and value != "not reported in abstract"]
    return "; ".join(flags) if flags else "No funding/conflict detail reported in abstract; verify full text."


def research_angle(paper: ResearchPaper) -> str:
    return "Explain what the study actually found, what it does not prove, and whether Australian men should change training, diet or health behaviour."


def research_value_add(paper: ResearchPaper) -> str:
    return "Translate the evidence into practical, caveated advice; compare it with common gym claims; add Australian context where possible."


def facts_to_verify(paper: ResearchPaper) -> list[str]:
    facts = ["Confirm full publication date and whether the paper has corrections or retractions.", "Read full text before making practical recommendations."]
    if paper.funding and "verify" in paper.funding.lower():
        facts.append("Verify funding and conflict disclosures.")
    if not paper.effect_size:
        facts.append("Find exact numerical results in full text.")
    return facts


def write_research_report(papers: list[ResearchPaper], errors: list[str], report_path: Path, mfo_index: dict[str, Any], config: dict[str, Any]) -> None:
    pages = mfo_pages_from_index(mfo_index)
    for paper in papers:
        score_research_paper(paper, pages, config)
    generated_at = iso(utc_now())
    viable = sorted([paper for paper in papers if paper.status == "viable"], key=lambda paper: paper.score, reverse=True)
    hold = sorted([paper for paper in papers if paper.status == "hold"], key=lambda paper: paper.score, reverse=True)
    rejected = sorted([paper for paper in papers if paper.status == "rejected"], key=lambda paper: paper.score, reverse=True)
    lines = [
        "# MFO Research Radar",
        "",
        f"- Scan timestamp: `{generated_at}`",
        f"- Papers assessed: `{len(papers)}`",
        f"- Viable research leads: `{len(viable)}`",
        f"- MFO archive index: `{len(pages)} pages` refreshed `{mfo_index.get('refreshed_at', 'unknown')}`",
        f"- Archive source fingerprints: `{mfo_index.get('source_fingerprint_count', 0)}`",
        "",
        "Research Radar is separate from breaking News Radar. A paper is evidence, not a headline.",
        "",
        "## Viable Research Leads",
        "",
    ]
    if mfo_index.get("archive_warning"):
        lines[6:6] = [f"**Archive warning:** {mfo_index.get('archive_warning')}", ""]
    if not viable:
        lines.extend(["No viable research leads found.", ""])
    for index, paper in enumerate(viable[:10], 1):
        lines.extend(research_markdown_lines(index, paper))
    lines.extend(["## Hold Or Evergreen Possibilities", ""])
    if not hold:
        lines.extend(["No hold candidates found.", ""])
    for index, paper in enumerate(hold[:10], 1):
        lines.extend(research_markdown_lines(index, paper))
    lines.extend(["## Rejected Research Candidates", ""])
    if not rejected:
        lines.extend(["No rejected candidates.", ""])
    for paper in rejected[:30]:
        lines.append(f"- [{paper.title}]({paper.pubmed_url}) - score {paper.score}; {'; '.join(paper.rejection_reasons or paper.penalties or ['below threshold'])}.")
    lines.append("")
    if errors:
        lines.extend(["## Source Errors", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    payloads = [research_payload(paper, pages) for paper in viable + hold + rejected]
    write_json_payload(
        report_path.with_suffix(".json"),
        {
            "scanner_type": "research",
            "schema_version": 1,
            "generated_at": generated_at,
            "report_path": str(report_path),
            "lead_count": len(payloads),
            "viable_count": len(viable),
            "hold_count": len(hold),
            "errors": errors,
            "metadata": {
                "mfo_archive_pages": len(pages),
                "mfo_archive_refreshed_at": mfo_index.get("refreshed_at"),
                "mfo_archive_source_fingerprint_count": mfo_index.get("source_fingerprint_count", 0),
                "mfo_archive_warning": mfo_index.get("archive_warning", ""),
                "source": "PubMed E-utilities",
                "thresholds": config.get("thresholds", {}),
            },
            "leads": payloads,
        },
    )


def research_markdown_lines(index: int, paper: ResearchPaper) -> list[str]:
    return [
        f"### {index}. {paper.title}",
        "",
        f"- Score: {paper.score}/100; status: {paper.recommended_status}.",
        f"- Topic group: {paper.topic_group}.",
        f"- Source: {paper.journal or 'PubMed'}; PMID `{paper.pmid}`; DOI `{paper.doi or 'not reported'}`.",
        f"- Publication dates: formal {fmt_datetime(paper.publication_date)}; electronic {fmt_datetime(paper.electronic_publication_date)}; PubMed indexed {fmt_datetime(paper.indexed_at)}.",
        f"- Study type: {study_type_for(paper).replace('_', ' ')}.",
        f"- Population/sample: {paper.study_population}; {paper.sample_size}.",
        f"- Key result: {paper.primary_finding}",
        f"- Numerical result/effect size: {paper.effect_size}",
        f"- Evidence strength: {evidence_strength_note(paper)}",
        f"- What it does not prove: {limitations_note(paper)}",
        f"- Public interest: {(paper.public_interest or {}).get('note', 'No public-interest match found.')}",
        f"- Funding/conflict: {disclosure_note(paper)}",
        f"- MFO angle: {research_angle(paper)}",
        f"- Archive overlap: {overlap_label(paper.archive_overlap or Overlap(0, None, []))}",
        f"- Primary source: {paper.pubmed_url}",
        "",
    ]


def upsert_research_seen(conn: sqlite3.Connection, papers: list[ResearchPaper], seen_at: str) -> None:
    for paper in papers:
        for identifier, identifier_type in [(paper.pmid, "pmid"), (paper.doi or "", "doi")]:
            if not identifier:
                continue
            existing = conn.execute("SELECT first_seen FROM research_seen WHERE identifier = ?", (identifier.lower(),)).fetchone()
            first_seen = existing["first_seen"] if existing else seen_at
            conn.execute(
                """
                INSERT INTO research_seen (identifier, identifier_type, first_seen, last_seen, pmid, doi, title)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identifier) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    pmid = excluded.pmid,
                    doi = excluded.doi,
                    title = excluded.title
                """,
                (identifier.lower(), identifier_type, first_seen, seen_at, paper.pmid, paper.doi, paper.title),
            )
    conn.execute(
        "INSERT OR REPLACE INTO research_state (key, value) VALUES ('last_successful_scan', ?)",
        (seen_at,),
    )


def dedupe_research_papers(papers: list[ResearchPaper]) -> list[ResearchPaper]:
    seen_pmids: set[str] = set()
    seen_dois: set[str] = set()
    deduped: list[ResearchPaper] = []
    for paper in papers:
        doi = (paper.doi or "").lower()
        if paper.pmid in seen_pmids or (doi and doi in seen_dois):
            continue
        seen_pmids.add(paper.pmid)
        if doi:
            seen_dois.add(doi)
        deduped.append(paper)
    return deduped


def run_research_radar(
    db_path: Path,
    report_path: Path,
    config_path: Path,
    mfo_index: dict[str, Any],
) -> list[ResearchPaper]:
    config = load_research_config(config_path)
    now = utc_now()
    window_days = int(config.get("window_days", 7))
    overlap_days = int(config.get("overlap_days", 3))
    start = now - timedelta(days=window_days + overlap_days)
    topic_by_pmid: dict[str, str] = {}
    errors: list[str] = []
    all_pmids: list[str] = []
    try:
        for group in config.get("topic_groups", []):
            if not isinstance(group, dict):
                continue
            name = str(group.get("name") or "Research")
            query = str(group.get("query") or "")
            if not query:
                continue
            try:
                pmids = pubmed_search(query, start, now)
                for pmid in pmids:
                    topic_by_pmid.setdefault(pmid, name)
                all_pmids.extend(pmids)
            except Exception as exc:
                errors.append(f"PubMed search {name}: {exc}")
        unique_pmids = sorted(set(all_pmids), key=all_pmids.index)
        papers: list[ResearchPaper] = []
        for offset in range(0, len(unique_pmids), 100):
            batch = unique_pmids[offset : offset + 100]
            try:
                papers.extend(extract_pubmed_articles(pubmed_fetch(batch), topic_by_pmid))
            except Exception as exc:
                errors.append(f"PubMed fetch batch {offset // 100 + 1}: {exc}")
        papers = dedupe_research_papers(papers)
        for paper in papers[:30]:
            enrich_research_reliable_metadata(paper, errors)
        if not papers:
            raise RuntimeError("; ".join(errors) or "PubMed returned no papers for configured research queries.")
        write_research_report(papers, errors, report_path, mfo_index, config)
        seen_at = iso(utc_now())
        with connect_db(db_path) as conn:
            upsert_research_seen(conn, papers, seen_at)
        return papers
    except Exception:
        if not report_path.exists() and not report_path.with_suffix(".json").exists():
            raise
        print("Warning: Research Radar failed; preserving previous successful research report.", file=sys.stderr)
        raise


def run_fixture_tests() -> None:
    fixture_db = Path("/tmp/mfo-scanner-fixtures.db")
    creator_report = Path("/tmp/mfo-scanner-creator-fixture.md")
    news_report = Path("/tmp/mfo-scanner-news-fixture.md")
    if fixture_db.exists():
        fixture_db.unlink()

    mfo_index = {
        "source": "fixture",
        "refreshed_at": "2026-08-06T00:00:00Z",
        "pages": [
            {
                "title": "Browney Spent 24 Hours Training With Bryan Mbeumo",
                "url": "https://mensfitnessonline.com.au/bryan-mbeumo-training-routine/",
                "slug": "bryan-mbeumo-training-routine",
                "source_urls": ["https://www.youtube.com/watch?v=uN2481h2Ut8"],
                "youtube_ids": ["uN2481h2Ut8"],
            }
            ,
            {
                "title": "Will Tennyson Rare Genetics Video Already Covered",
                "url": "https://mensfitnessonline.com.au/will-tennyson-rare-genetics/",
                "slug": "will-tennyson-rare-genetics",
                "source_urls": ["https://www.youtube.com/watch?v=s-F1EciASeE"],
                "youtube_ids": ["s-F1EciASeE"],
            },
            {
                "title": "Meditation And Sleep Paper",
                "url": "https://mensfitnessonline.com.au/meditation-sleep-paper/",
                "slug": "meditation-sleep-paper",
                "source_urls": ["https://pubmed.ncbi.nlm.nih.gov/42576331/", "https://doi.org/10.1000/meditation-sleep"],
                "youtube_ids": [],
                "pmids": ["42576331"],
                "dois": ["10.1000/meditation-sleep"],
            },
            {
                "title": "Generic Exercise Review Page",
                "url": "https://mensfitnessonline.com.au/generic-exercise-review/",
                "slug": "generic-exercise-review",
                "source_urls": [],
                "youtube_ids": [],
            },
        ],
    }
    mfo_index = index_payload(mfo_pages_from_index(mfo_index), MFO_SITE_URL, "fixture")
    profiles = {
        "fixture creator": {
            "category": "fitness challenge influencer",
            "mfo_fit": "High",
            "default_angle": "Fixture angle.",
            "default_value_add": "Fixture value add.",
            "default_weakness": "Fixture weakness.",
        }
    }

    first = "2026-08-06T00:00:00Z"
    second = "2026-08-06T00:30:00Z"
    conn = connect_db(fixture_db)
    observations: list[Observation] = []
    with conn:
        scan1 = create_scan(conn, first, 1, "scheduled")
        base = Observation(
            channel_source="fixture creator",
            channel_name="Fixture Creator",
            video_title="Covered Browney fixture",
            video_url="https://www.youtube.com/watch?v=uN2481h2Ut8",
            video_id="uN2481h2Ut8",
            upload_datetime="2026-08-05T00:00:00Z",
            view_count=1000,
            duration_seconds=900,
            video_type="standard",
            scan_timestamp=first,
            age_hours=24,
            total_views_per_hour=41.7,
        )
        save_observation(conn, scan1, base)
        short_base = Observation(
            channel_source="fixture creator",
            channel_name="Fixture Creator",
            video_title="Uncovered short interval fixture",
            video_url="https://www.youtube.com/watch?v=shortInterval1",
            video_id="shortInterval1",
            upload_datetime="2026-08-05T00:00:00Z",
            view_count=2000,
            duration_seconds=900,
            video_type="standard",
            scan_timestamp=first,
            age_hours=24,
            total_views_per_hour=83.3,
        )
        save_observation(conn, scan1, short_base)
        scan2 = create_scan(conn, second, 1, "scheduled")
        current = Observation(
            channel_source="fixture creator",
            channel_name="Fixture Creator",
            video_title="Covered Browney fixture",
            video_url="https://www.youtube.com/watch?v=uN2481h2Ut8",
            video_id="uN2481h2Ut8",
            upload_datetime="2026-08-05T00:00:00Z",
            view_count=1402,
            duration_seconds=900,
            video_type="standard",
            scan_timestamp=second,
            age_hours=24.5,
            total_views_per_hour=57.2,
        )
        current = enrich_growth(conn, current)
        save_observation(conn, scan2, current)
        observations.append(current)
        short_current = Observation(
            channel_source="fixture creator",
            channel_name="Fixture Creator",
            video_title="Uncovered short interval fixture",
            video_url="https://www.youtube.com/watch?v=shortInterval1",
            video_id="shortInterval1",
            upload_datetime="2026-08-05T00:00:00Z",
            view_count=2402,
            duration_seconds=900,
            video_type="standard",
            scan_timestamp=second,
            age_hours=24.5,
            total_views_per_hour=98.0,
        )
        short_current = enrich_growth(conn, short_current)
        save_observation(conn, scan2, short_current)
        observations.append(short_current)
        will_current = Observation(
            channel_source="fixture creator",
            channel_name="Will Tennyson",
            video_title="I Tested Rare Genetics For Muscle Growth",
            video_url="https://www.youtube.com/watch?v=s-F1EciASeE",
            video_id="s-F1EciASeE",
            upload_datetime="2026-08-05T00:00:00Z",
            view_count=500000,
            duration_seconds=900,
            video_type="standard",
            scan_timestamp=second,
            age_hours=24.5,
            total_views_per_hour=20408.0,
        )
        will_current = enrich_growth(conn, will_current)
        save_observation(conn, scan2, will_current)
        observations.append(will_current)

    write_report(observations, [], second, creator_report, profiles, mfo_index, conn)
    creator_text = creator_report.read_text(encoding="utf-8")
    assert "Already Covered And Excluded" in creator_text
    assert "exact source already appears" in creator_text
    assert "s-F1EciASeE" in creator_text
    assert "insufficient growth interval" in creator_text
    assert "observed 804.0 views/hour" not in creator_text

    news_items = [
        NewsItem(
            title="Stan announces Zyzz documentary release date",
            url="https://www.stan.news/zyzz-documentary-release",
            source="Stan",
            published="2026-08-06T00:00:00Z",
            summary="Stan announced a new documentary about Zyzz for Australian audiences.",
            source_type="publicity",
        ),
        NewsItem(
            title="Guardian reports on new Zyzz documentary release",
            url="https://www.theguardian.com/tv-and-radio/zyzz-documentary",
            source="The Guardian",
            published="2026-08-06T01:00:00Z",
            summary="A report on the documentary release and fitness culture.",
            source_type="rss",
        ),
        NewsItem(
            title="New trailer released for Zyzz documentary",
            url="https://www.youtube.com/watch?v=fixtureTrailer",
            source="Official trailer",
            published="2026-08-06T02:00:00Z",
            summary="Official trailer released for the documentary.",
            source_type="official",
        ),
        NewsItem(
            title="PRNewswire: Stan announces Zyzz documentary release date",
            url="https://www.prnewswire.com/news-releases/stan-announces-zyzz-documentary-release-date.html",
            source="PRNewswire",
            published="2026-08-06T00:05:00Z",
            summary="Syndicated copy of the announcement.",
            source_type="rss",
        ),
        NewsItem(
            title="Popular fitness creator gets huge views for chest workout",
            url="https://example.com/popular-workout-video",
            source="Example",
            published="2026-08-06T00:00:00Z",
            summary="A popular video but no new development.",
            source_type="rss",
        ),
        NewsItem(
            title="At 72, this Scottish Supergran broke a Hyrox world record - this is her weekly workout routine",
            url="https://www.womenshealthmag.com/fitness/scottish-supergran-hyrox",
            source="Women's Health",
            published="2026-08-06T00:00:00Z",
            summary="A female athlete profile focused on her weekly workout routine.",
            source_type="rss",
        ),
        NewsItem(
            title="Scottish Supergran Hyrox workout routine - Men's Health",
            url="https://www.menshealth.com/fitness/scottish-supergran-hyrox",
            source="Men's Health",
            published="2026-08-06T00:30:00Z",
            summary="A pickup of the same female athlete profile.",
            source_type="rss",
        ),
        NewsItem(
            title="HYROX announces new Australian championship event in Sydney",
            url="https://hyrox.com/new-australian-championship-sydney",
            source="HYROX",
            published=iso(utc_now() - timedelta(hours=1)),
            summary="HYROX announced a new Australian championship event and schedule.",
            source_type="official",
        ),
        NewsItem(
            title="Correction: body composition and physical fitness review",
            url="https://bjsm.bmj.com/content/correction-fixture",
            source="BMJ Sports Medicine",
            published="2026-08-06T01:00:00Z",
            summary="Correction notice for a previously published review. The notice does not say conclusions changed.",
            source_type="journal",
        ),
        NewsItem(
            title="Randomized trial of resistance training volume improves strength in middle aged men",
            url="https://www.sciencedaily.com/releases/2026/08/260806100000.htm",
            source="ScienceDaily",
            published="2026-08-06T03:00:00Z",
            summary="ScienceDaily reports on a new study published in Journal of Strength and Conditioning Research. DOI: 10.1000/rct.",
            source_type="research_media",
        ),
    ]
    clusters = cluster_news_items(news_items)
    zyzz_clusters = [cluster for cluster in clusters if "zyzz" in primary_news_item(cluster).title.lower()]
    assert len(zyzz_clusters) == 1
    assert len(zyzz_clusters[0].items) == 4
    assert "prnewswire.com" not in independent_domains(zyzz_clusters[0])
    pages = mfo_pages_from_index({})
    # Fixture tests must stay offline: stub the Crossref/PubMed canonical
    # date lookup that true_published_at() triggers for research_media
    # clusters (the sciencedaily fixture below) instead of hitting the
    # real network.
    original_resolve_canonical_date = editorial_gates.resolve_canonical_research_date
    editorial_gates.resolve_canonical_research_date = lambda dois, pmids, **_kwargs: {
        "canonical_published_at": None,
        "canonical_date_source": "unresolved",
    }
    try:
        for cluster in clusters:
            score_news_cluster(cluster, pages)
    finally:
        editorial_gates.resolve_canonical_research_date = original_resolve_canonical_date
    female_profile = next(cluster for cluster in clusters if "supergran" in primary_news_item(cluster).title.lower())
    australian_event = next(cluster for cluster in clusters if "australian championship" in primary_news_item(cluster).title.lower())
    correction = next(cluster for cluster in clusters if "correction" in primary_news_item(cluster).title.lower())
    sciencedaily = next(cluster for cluster in clusters if "sciencedaily.com" in primary_news_item(cluster).url)
    assert female_profile.score < australian_event.score
    assert any("female athlete profile" in penalty for penalty in female_profile.penalties or [])
    assert correction.score <= 25
    assert (correction.score_json or {}).get("development_type") == "correction"
    assert (correction.score_json or {}).get("conclusions_changed") == "unverified"
    assert sciencedaily.score <= 30
    assert (sciencedaily.score_json or {}).get("research_media", {}).get("role") == "public-interest research signal"
    assert "10.1000/rct" in (sciencedaily.score_json or {}).get("research_media", {}).get("extracted_dois", [])
    write_news_report(clusters, [], news_report, {}, connect_db(fixture_db))
    news_text = news_report.read_text(encoding="utf-8")
    assert "Stan announces Zyzz documentary release date" in news_text
    assert "Popular fitness creator" not in news_text
    news_payload_data = json.loads(news_report.with_suffix(".json").read_text(encoding="utf-8"))
    sciencedaily_payload = next(lead for lead in news_payload_data["leads"] if lead["source_url"].startswith("https://www.sciencedaily.com/releases/"))
    assert sciencedaily_payload["source_url"] == "https://www.sciencedaily.com/releases/2026/08/260806100000.htm"
    assert sciencedaily_payload["source_category"] == "research_media"
    assert sciencedaily_payload["research_media"]["verification_note"]
    stale = NewsCluster(
        key="stale",
        items=[
            NewsItem(
                title="HYROX announces old event launch",
                url="https://hyrox.com/old-event",
                source="HYROX",
                published="2026-07-01T00:00:00Z",
                summary="HYROX announced an event weeks ago.",
                source_type="official",
            )
        ],
    )
    score_news_cluster(stale, pages)
    assert any("older than seven days" in cap for cap in (stale.score_json or {}).get("caps_applied", []))
    entertainment = NewsCluster(
        key="entertainment",
        items=[
            NewsItem(
                title="Stan celebrates launch of unrelated comedy screening",
                url="https://www.stan.news/unrelated-comedy",
                source="Stan Media Releases",
                published="2026-08-06T00:00:00Z",
                summary="Stan announced a screening for a comedy show.",
                source_type="publicity",
            )
        ],
    )
    score_news_cluster(entertainment, pages)
    assert entertainment.score <= 20
    assert any("Entertainment publicity" in reason for reason in (entertainment.score_json or {}).get("kill_reasons", []))
    rss_fixture = """<rss><channel><item><title>Fixture item</title><link>https://example.com/exact-article</link><source url="https://example.com">Example</source><pubDate>Thu, 06 Aug 2026 00:00:00 GMT</pubDate></item></channel></rss>"""
    parsed_items = rss_items_from_xml(rss_fixture, "Fixture", "rss")
    assert parsed_items[0].url == "https://example.com/exact-article"
    crossfit_clusters = cluster_news_items(
        [
            NewsItem("CrossFit Games results roundup day one", "https://example.com/crossfit-results-1", "Example", "2026-08-06T00:00:00Z", "Results roundup from the CrossFit Games.", "rss"),
            NewsItem("CrossFit Games leaderboard and results roundup", "https://example.org/crossfit-results-2", "Example 2", "2026-08-06T01:00:00Z", "Another result roundup covering the same event.", "rss"),
        ]
    )
    assert len(crossfit_clusters) == 1
    assert len(crossfit_clusters[0].items) == 2
    generic_overlap = find_overlap(
        Observation(
            channel_source="fixture",
            channel_name="Fixture",
            video_title="Strength exercise review",
            video_url="https://example.com/unrelated",
            video_id="unrelated",
            upload_datetime=None,
            view_count=0,
            duration_seconds=None,
            video_type="standard",
            scan_timestamp=second,
            age_hours=None,
            total_views_per_hour=None,
        ),
        mfo_pages_from_index(mfo_index),
    )
    assert generic_overlap.score == 0

    research_report = Path("/tmp/mfo-scanner-research-fixture.md")
    research_config = {
        "thresholds": {"viable": 65, "hold": 50},
        "journal_boosts": [
            {"name": "Journal of Strength and Conditioning Research", "mfo_relevance_boost": 2},
        ],
        "penalties": {
            "animal_only": 25,
            "preprint": 20,
            "editorial_or_commentary": 25,
            "missing_abstract": 20,
            "extremely_small_sample": 15,
            "commercial_conflict": 10,
            "archive_overlap": 20,
        },
    }
    research_index = {
        "source": "fixture",
        "refreshed_at": "2026-08-06T00:00:00Z",
        "pages": [
            {
                "title": "Existing Creatine Study",
                "url": "https://mensfitnessonline.com.au/existing-creatine-study/",
                "slug": "existing-creatine-study",
                "source_urls": ["https://doi.org/10.1000/duplicate"],
                "youtube_ids": [],
            }
        ],
    }
    research_index = index_payload(mfo_pages_from_index(research_index) + [page for page in mfo_pages_from_index(mfo_index) if page.pmids], MFO_SITE_URL, "fixture")
    research_papers = [
        ResearchPaper(
            pmid="1001",
            doi="10.1000/rct",
            topic_group="Resistance training, strength and hypertrophy",
            title="Randomized trial of resistance training volume improves strength in middle aged men",
            journal="Journal of Strength",
            authors=["Fixture A"],
            abstract="Randomized controlled trial enrolled 180 men assigned to resistance training volume intervention compared with usual training for 12 weeks. Results increased strength by 18% with 95% CI 10 to 25.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Randomized Controlled Trial"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1001/",
            publisher_url="https://doi.org/10.1000/rct",
        ),
        ResearchPaper(
            pmid="42576331",
            doi="10.1000/meditation-sleep",
            topic_group="Sleep, recovery and fatigue",
            title="Meditation and sleep paper already covered",
            journal="Sleep Journal",
            authors=["Fixture J"],
            abstract="Randomized trial enrolled 180 adults assigned to meditation compared with sleep hygiene for 8 weeks. Results improved sleep quality by 12%.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Randomized Controlled Trial"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/42576331/",
            publisher_url="https://doi.org/10.1000/meditation-sleep",
        ),
        ResearchPaper(
            pmid="1002",
            doi="10.1000/review",
            topic_group="Protein, creatine and common sports supplements",
            title="Systematic review and meta-analysis of creatine and resistance training performance",
            journal="Sports Nutrition",
            authors=["Fixture B"],
            abstract="Systematic review and meta-analysis included 32 trials in adults. Results improved lean mass by 1.2 kg and strength performance.",
            publication_date=iso(utc_now() - timedelta(days=2)),
            electronic_publication_date=iso(utc_now() - timedelta(days=2)),
            indexed_at=iso(utc_now()),
            publication_types=["Systematic Review", "Meta-Analysis"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1002/",
            publisher_url="https://doi.org/10.1000/review",
        ),
        ResearchPaper(
            pmid="42573645",
            doi="10.1000/bone-density",
            topic_group="Resistance training, strength and hypertrophy",
            title="Optimal Resistance Exercise Strategies for Improving Bone Mineral Density in Middle-Aged and Older Adults: A Network Meta-Analysis Based on Exercise Intensity and Frequency",
            journal="Calcified Tissue International",
            authors=["Fixture Bone"],
            abstract="Systematic review and network meta-analysis included 42 trials and 1,746 adults. Results showed resistance exercise improved bone mineral density compared with control groups.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Network Meta-Analysis", "Systematic Review"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/42573645/",
            publisher_url="https://doi.org/10.1000/bone-density",
        ),
        ResearchPaper(
            pmid="42570001",
            doi="10.1000/german-diabetes-audit",
            topic_group="Diabetes, heart health, longevity and physical activity",
            title="Regional German diabetes care audit of healthcare utilisation",
            journal="Health Services Research",
            authors=["Fixture Audit"],
            abstract="A geographically specific healthcare audit analysed claims data from 312,645 patients in Germany and described diabetes care utilisation.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Journal Article"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/42570001/",
            publisher_url="https://doi.org/10.1000/german-diabetes-audit",
        ),
        ResearchPaper(
            pmid="42570002",
            doi="10.1000/tacs-review",
            topic_group="Sleep, recovery and fatigue",
            title="Transcranial alternating current stimulation review for specialist neurological practice",
            journal="Clinical Neurophysiology",
            authors=["Fixture TACS"],
            abstract="A narrative review summarised tACS protocols for specialist clinical neurophysiology without practical exercise or men's-health decisions.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Review"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/42570002/",
            publisher_url="https://doi.org/10.1000/tacs-review",
        ),
        ResearchPaper(
            pmid="42570003",
            doi="10.1000/cardiac-text-secondary",
            topic_group="Diabetes, heart health, longevity and physical activity",
            title="Secondary analysis of cardiac rehabilitation text-message adherence",
            journal="Cardiac Digital Health",
            authors=["Fixture Text"],
            abstract="A post hoc secondary analysis of cardiac rehabilitation text-message adherence found exploratory associations in a clinical programme.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Journal Article"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/42570003/",
            publisher_url="https://doi.org/10.1000/cardiac-text-secondary",
        ),
        ResearchPaper(
            pmid="1003",
            doi="10.1000/animal",
            topic_group="Sleep, recovery and fatigue",
            title="Mouse cell study of muscle fatigue pathways",
            journal="Basic Science",
            authors=["Fixture C"],
            abstract="Mouse cell experiment studied muscle fatigue biomarkers.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Journal Article"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1003/",
        ),
        ResearchPaper(
            pmid="1004",
            doi="10.1000/editorial",
            topic_group="Training frequency, volume, intensity and exercise selection",
            title="Editorial on exercise selection trends",
            journal="Opinion Journal",
            authors=["Fixture D"],
            abstract="Editorial commentary on training trends.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Editorial"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1004/",
        ),
        ResearchPaper(
            pmid="1005",
            doi="10.1000/preprint",
            topic_group="Weight loss, obesity, GLP-1 drugs and lean-mass retention",
            title="Preprint study of GLP-1 drugs and lean mass",
            journal="Preprints",
            authors=["Fixture E"],
            abstract="Preprint observational study enrolled 24 adults and claims semaglutide caused lean mass retention.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Preprint"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1005/",
        ),
        ResearchPaper(
            pmid="1006",
            doi="10.1000/commercial",
            topic_group="Protein, creatine and common sports supplements",
            title="Industry funded protein supplement trial",
            journal="Sports Nutrition",
            authors=["Fixture F"],
            abstract="Randomized trial enrolled 120 adults. The supplement company funded by industry sponsor reported improved muscle strength by 5%.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Randomized Controlled Trial"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1006/",
        ),
        ResearchPaper(
            pmid="1007",
            doi="10.1000/noabstract",
            topic_group="Running, VO2 max, HIIT and cardiovascular fitness",
            title="HIIT fitness paper without abstract",
            journal="No Abstract Journal",
            authors=["Fixture G"],
            abstract=None,
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Journal Article"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1007/",
        ),
        ResearchPaper(
            pmid="1008",
            doi="10.1000/duplicate",
            topic_group="Protein, creatine and common sports supplements",
            title="Existing creatine paper already covered",
            journal="Sports Nutrition",
            authors=["Fixture H"],
            abstract="Randomized trial enrolled 90 adults and improved strength.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Randomized Controlled Trial"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1008/",
            publisher_url="https://doi.org/10.1000/duplicate",
        ),
        ResearchPaper(
            pmid="1009",
            doi="10.1000/jscr",
            topic_group="Strength and conditioning, sports performance and power",
            title="Strength and conditioning training improves sprint and power performance in adult athletes",
            journal="Journal of Strength and Conditioning Research",
            authors=["Fixture I"],
            abstract="Randomized controlled trial enrolled 80 adult athletes assigned to strength and conditioning training compared with usual training for 8 weeks. Results improved sprint performance by 4% and power by 7%.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Randomized Controlled Trial"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1009/",
            publisher_url="https://doi.org/10.1000/jscr",
        ),
        ResearchPaper(
            pmid="1001",
            doi="10.1000/rct",
            topic_group="Resistance training, strength and hypertrophy",
            title="Duplicate RCT",
            journal="Journal of Strength",
            authors=["Fixture A"],
            abstract="Duplicate record.",
            publication_date=iso(utc_now() - timedelta(days=1)),
            electronic_publication_date=iso(utc_now() - timedelta(days=1)),
            indexed_at=iso(utc_now()),
            publication_types=["Randomized Controlled Trial"],
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/1001/",
        ),
    ]
    research_papers = dedupe_research_papers(research_papers)
    assert len(research_papers) == 14
    for paper in research_papers:
        enrich_research_from_text(paper)
    original_news_json_path = globals()["NEWS_JSON_PATH"]
    try:
        globals()["NEWS_JSON_PATH"] = news_report.with_suffix(".json")
        write_research_report(research_papers, [], research_report, research_index, research_config)
    finally:
        globals()["NEWS_JSON_PATH"] = original_news_json_path
    research_payload_data = json.loads(research_report.with_suffix(".json").read_text(encoding="utf-8"))
    by_id = {lead["lead_id"]: lead for lead in research_payload_data["leads"]}
    assert by_id["research:1001"]["status"] == "viable"
    assert by_id["research:1001"]["public_interest"]["matched"] is True
    assert "sciencedaily.com" in json.dumps(by_id["research:1001"]["public_interest"])
    assert by_id["research:1002"]["status"] == "viable"
    assert by_id["research:42573645"]["score"] > by_id["research:42570001"]["score"]
    assert by_id["research:42573645"]["score"] > by_id["research:42570002"]["score"]
    assert by_id["research:42573645"]["score"] > by_id["research:42570003"]["score"]
    assert by_id["research:42570001"]["sample_size"] == "n = 312,645"
    assert by_id["research:1003"]["status"] == "rejected"
    assert by_id["research:1004"]["status"] == "rejected"
    assert any("preprint" in item for item in by_id["research:1005"]["penalties"])
    assert any("commercial conflict" in item for item in by_id["research:1006"]["penalties"])
    assert any("missing abstract" in item for item in by_id["research:1007"]["penalties"])
    assert by_id["research:1008"]["status"] == "rejected"
    assert by_id["research:1009"]["status"] == "viable"
    assert by_id["research:1009"]["score_breakdown"]["mfo_audience_relevance"] == 20
    assert by_id["research:42576331"]["status"] == "rejected"
    assert any("exact MFO archive duplicate" in item for item in by_id["research:42576331"]["penalties"])
    failure_config = Path("/tmp/mfo-scanner-research-failure-config.json")
    failure_config.write_text(
        json.dumps({"topic_groups": [{"name": "Failure", "query": "resistance training"}]}),
        encoding="utf-8",
    )
    previous_text = research_report.read_text(encoding="utf-8")
    original_pubmed_search = globals()["pubmed_search"]
    try:
        globals()["pubmed_search"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture API failure"))
        try:
            run_research_radar(fixture_db, research_report, failure_config, research_index)
        except RuntimeError:
            pass
    finally:
        globals()["pubmed_search"] = original_pubmed_search
    assert research_report.read_text(encoding="utf-8") == previous_text

    # Known failure #5: two distinct HYROX stories must not collapse to the
    # same cluster_key/lead_id. Previously both fell into the hardcoded
    # "hyrox-<dtype>" collapse in cluster_key_for().
    distinct_hyrox_items = [
        NewsItem(
            title="HYROX Bangkok returns with record 20,000 participants",
            url="https://news.google.com/rss/articles/fixture-hyrox-bangkok",
            source="Khaosod English",
            published="2026-07-23T07:00:00Z",
            summary="HYROX Bangkok drew a record number of participants this year.",
            source_type="rss",
        ),
        NewsItem(
            title="Aberdeenshire's 'Hyrox Granny' continues to amaze as she breaks record aged 72",
            url="https://news.google.com/rss/articles/fixture-hyrox-granny",
            source="Aberdeen Live",
            published="2026-03-25T07:00:00Z",
            summary="A 72 year old competitor broke an age-group record at a local Hyrox event.",
            source_type="rss",
        ),
    ]
    distinct_hyrox_clusters = cluster_news_items(distinct_hyrox_items)
    assert len(distinct_hyrox_clusters) == 2, "distinct HYROX stories incorrectly merged into one cluster"
    distinct_keys = {cluster.key for cluster in distinct_hyrox_clusters}
    assert len(distinct_keys) == 2, f"distinct HYROX stories collapsed to the same cluster_key: {distinct_keys}"

    # Same-report duplicate lead_id check across all three JSON report types.
    for report_json_path in (creator_report.with_suffix(".json"), news_report.with_suffix(".json"), research_report.with_suffix(".json")):
        if not report_json_path.exists():
            continue
        report_data = json.loads(report_json_path.read_text(encoding="utf-8"))
        ids = [lead.get("lead_id") for lead in report_data.get("leads", [])]
        assert len(ids) == len(set(ids)), f"duplicate lead_id(s) found in {report_json_path}: {[i for i in set(ids) if ids.count(i) > 1]}"

    # Known failure #3/#6: an unrelated article using result-ish language must
    # not be classified as a HYROX/CrossFit competition result merely because
    # it contains generic result words.
    fixture_gates_config = editorial_gates.load_config(EDITORIAL_GATES_CONFIG_PATH)
    dsa_classification = editorial_gates.classify_development_type(
        "Disability Sports Australia releases new participation plan with results for regional programs",
        fixture_gates_config,
        evidence_links=["https://example.com/dsa-plan"],
    )
    assert dsa_classification["development_type"] != "competition_result", "unrelated article misclassified as a competition result"
    hyrox_classification = editorial_gates.classify_development_type(
        "HYROX Bangkok returns with record 20,000 participants",
        fixture_gates_config,
        evidence_links=["https://hyrox.com/bangkok-results"],
    )
    assert hyrox_classification["development_type"] == "competition_result"
    assert hyrox_classification["entity_matched"] == "hyrox"

    # A cluster whose only entity-bearing item is an unrelated pickup must
    # not have its development_type polluted by that item's presence
    # elsewhere in the cluster — classification runs on the primary item.
    dsa_cluster = NewsCluster(
        key="fixture-dsa",
        items=[
            NewsItem(
                title="Disability Sports Australia releases new participation plan with results for regional programs",
                url="https://example.com/dsa-plan",
                source="Disability Sports Australia",
                published="2026-08-15T00:00:00Z",
                summary="A participation plan with regional program results.",
                source_type="official",
            )
        ],
    )
    assert development_type(dsa_cluster) != "competition_result", "development_type() misclassified an unrelated article as a competition result"
    score_news_cluster(dsa_cluster, [])
    assert "competition_entity_mismatch" in (dsa_cluster.score_json or {}).get("kill_reason_codes", []), (
        "DSA article used result-style language but no confirmed entity; expected an explicit "
        "competition_entity_mismatch kill reason, not a silent low score"
    )
    genuine_hyrox_cluster = NewsCluster(
        key="fixture-hyrox-genuine",
        items=[
            NewsItem(
                title="HYROX Bangkok returns with record 20,000 participants",
                url="https://hyrox.com/bangkok-results",
                source="HYROX",
                published=iso(utc_now() - timedelta(hours=1)),
                summary="Official HYROX results from the Bangkok leg.",
                source_type="official",
            )
        ],
    )
    score_news_cluster(genuine_hyrox_cluster, [])
    assert "competition_entity_mismatch" not in (genuine_hyrox_cluster.score_json or {}).get("kill_reason_codes", []), (
        "genuine HYROX result incorrectly flagged as an entity mismatch"
    )

    # Known failure #6: the specific generic words named in the brief must
    # not register as meaningful archive overlap.
    generic_word_overlap = editorial_gates.weighted_topic_overlap(
        {"story", "wrong", "answer", "brain", "first", "more"} - set(fixture_gates_config.get("overlap_stopwords_extra", [])),
        {"story", "wrong", "answer"},
        fixture_gates_config,
    )
    assert generic_word_overlap["score"] == 0.0, "generic overlap words leaked through as meaningful overlap"

    # End-to-end topic_overlap_breakdown: generic shared words between an
    # unrelated candidate and an archive page must not register overlap,
    # while a genuine HYROX candidate must register topic overlap against
    # existing MFO HYROX coverage even when the wording differs.
    overlap_pages = mfo_pages_from_index(
        {
            "pages": [
                {
                    "title": "The real story behind the wrong answer everyone believes about training",
                    "url": "https://mensfitnessonline.com.au/wrong-answer-training-myth/",
                    "slug": "wrong-answer-training-myth",
                },
                {
                    "title": "HYROX Sydney: everything Australian competitors need to know",
                    "url": "https://mensfitnessonline.com.au/hyrox-sydney-guide/",
                    "slug": "hyrox-sydney-guide",
                },
            ]
        }
    )
    unrelated_breakdown = topic_overlap_breakdown(
        "The first story with the wrong answer explained", overlap_pages, None
    )
    assert unrelated_breakdown["cannibalisation_risk"] == "none", "generic shared words registered as archive overlap"
    hyrox_breakdown = topic_overlap_breakdown(
        "HYROX announces new Bangkok event with record entries", overlap_pages, None
    )
    assert hyrox_breakdown["topic_overlap"]["shared_entity_terms"] == ["hyrox"], "genuine HYROX thematic overlap was missed"
    assert hyrox_breakdown["cannibalisation_risk"] in {"medium", "high"}, "genuine HYROX thematic overlap was not flagged"

    # find_overlap() itself -- not just the additive topic_overlap_breakdown
    # field -- must use entity-weighted scoring. This is the function that
    # actually drives the -15/-5 archive-overlap penalty in
    # score_news_cluster() and the archive_overlap/cannibalisation_risk
    # fields an editor sees; a fix that only lived in topic_overlap_breakdown
    # would be inert against the real gate.
    hyrox_candidate_obs = Observation(
        channel_source="fixture news source",
        channel_name="Fixture News Source",
        video_title="HYROX announces new Bangkok event with record entries",
        video_url="https://example.com/hyrox-bangkok-announce",
        video_id="hyroxAnnounceFixture1",
        upload_datetime=first,
        view_count=0,
        duration_seconds=None,
        video_type="standard",
        scan_timestamp=first,
        age_hours=None,
        total_views_per_hour=None,
    )
    find_overlap_result = find_overlap(hyrox_candidate_obs, overlap_pages)
    candidate_text = f"{hyrox_candidate_obs.channel_name} {hyrox_candidate_obs.video_title}"
    expected_candidate_terms = tokenize(candidate_text) - OVERLAP_STOPWORDS
    expected_entity_terms = editorial_gates.entity_terms_for_text(candidate_text, fixture_gates_config)
    hyrox_page = next(p for p in overlap_pages if p.slug == "hyrox-sydney-guide")
    expected_page_terms = tokenize(f"{hyrox_page.title} {hyrox_page.slug}") - OVERLAP_STOPWORDS
    expected_weighted = editorial_gates.weighted_topic_overlap(
        expected_candidate_terms, expected_page_terms, fixture_gates_config, entity_terms=expected_entity_terms
    )
    naive_plain_score = len(expected_candidate_terms & expected_page_terms) / max(len(expected_candidate_terms), 1)
    assert find_overlap_result.score == expected_weighted["score"], (
        "find_overlap() is not using the entity-weighted scoring mechanism -- the actual "
        "archive_overlap/cannibalisation_risk fields and the score_news_cluster() penalty "
        "would silently fall back to the old unweighted formula"
    )
    assert "hyrox" in expected_entity_terms and find_overlap_result.score != naive_plain_score, (
        "expected the entity-weighted score to differ from the plain shared/total formula for a genuine entity match"
    )

    # Known failure #4: a competition result between 72h and 168h old must
    # now be excluded by the category-specific 72h competition_result gate,
    # even though it survived the old uniform 168h cap (only a -15 penalty).
    stale_result = NewsCluster(
        key="fixture-stale-hyrox-result",
        items=[
            NewsItem(
                title="HYROX Melbourne results: local athlete wins age-group title",
                url="https://example.com/hyrox-melbourne-results",
                source="Example Sport",
                published=iso(utc_now() - timedelta(hours=100)),
                summary="Results from the HYROX Melbourne event held recently.",
                source_type="rss",
            )
        ],
    )
    score_news_cluster(stale_result, pages)
    assert stale_result.score <= 20, "competition result older than the 72h window was not excluded"
    assert "canonical_source_too_old" in (stale_result.score_json or {}).get("kill_reason_codes", [])

    # Known failure #7: a viral-looking video with a huge raw view count
    # but zero comparable historical observations for its channel (a
    # brand new channel, first ever scan) must not be treated as a
    # confirmed breakout, and must not clear the commission_now threshold
    # on raw views alone.
    viral_db = Path("/tmp/mfo-scanner-viral-fixture.db")
    if viral_db.exists():
        viral_db.unlink()
    viral_conn = connect_db(viral_db)
    with viral_conn:
        viral_scan = create_scan(viral_conn, first, 1, "scheduled")
        viral_obs = Observation(
            channel_source="fixture viral creator",
            channel_name="Fixture Viral Creator",
            video_title="I tried the viral training method and it changed everything",
            video_url="https://www.youtube.com/watch?v=viralFixture1",
            video_id="viralFixture1",
            upload_datetime=first,
            view_count=5_000_000,
            duration_seconds=900,
            video_type="standard",
            scan_timestamp=first,
            age_hours=6,
            total_views_per_hour=833_333.0,
        )
        viral_obs = enrich_growth(viral_conn, viral_obs)
        save_observation(viral_conn, viral_scan, viral_obs)
    assert viral_obs.breakout_confidence == "pending", "first-ever observation should have pending breakout confidence"
    viral_dimensions = creator_editorial_dimensions(viral_obs, profiles, pages)
    assert viral_dimensions["score"] < 70, "cold-start video with huge raw views cleared the commission_now threshold on views alone"
    assert "no_channel_relative_breakout" in viral_dimensions["kill_reason_codes"]

    # Creator-story eligibility checklist: a thin, generic video description
    # (no recognised entity, no specific new claim, no verifiable evidence,
    # no Australian angle) must fail the 2-of-N checklist and be marked with
    # an explicit reason, not just a low score.
    assert viral_dimensions["story_value"] == "weak", f"thin creator video should score weak on the eligibility checklist, got {viral_dimensions['story_value']}"
    assert "no_new_development" in viral_dimensions["kill_reason_codes"], "creator video failing the eligibility checklist must carry an explicit no_new_development reason"

    # A creator video with a recognised entity, a specific new claim, a
    # practical lesson, verifiable evidence and a strong Australian angle
    # must clear the checklist (>= 2 of 6) even while its channel-relative
    # breakout confidence is still pending (a brand new channel).
    strong_case_obs = Observation(
        channel_source="fixture strong creator",
        channel_name="Fixture Strong Creator",
        video_title="Australian HYROX Sydney results: local dad breaks age-group record after new training study, verified by official data",
        video_url="https://www.youtube.com/watch?v=strongCaseFixture1",
        video_id="strongCaseFixture1",
        upload_datetime=first,
        view_count=50_000,
        duration_seconds=600,
        video_type="standard",
        scan_timestamp=first,
        age_hours=6,
        total_views_per_hour=8_333.0,
        breakout_confidence="pending",
    )
    strong_case_dimensions = creator_editorial_dimensions(strong_case_obs, profiles, pages)
    assert strong_case_dimensions["story_value"] in ("moderate", "strong"), (
        f"creator video passing multiple eligibility criteria should not be marked weak, got {strong_case_dimensions['story_value']}"
    )
    assert "no_new_development" not in strong_case_dimensions["kill_reason_codes"], "creator video clearing the eligibility checklist was incorrectly given a no_new_development kill reason"
    assert strong_case_dimensions["what_changed_now"] != "No clear current development found.", "eligible creator video should have a populated what_changed_now"
    assert len(strong_case_dimensions["criteria_passed"]) >= 2

    # Known failure #1: a study promoted by publicity months after its real
    # publication date must remain dated by its canonical publication date,
    # not the later press-release pickup date. Stub the Crossref/PubMed
    # lookup to return a specific old date and confirm true_published_at()
    # uses it instead of the fresh RSS pubDate.
    resurfaced_cluster = NewsCluster(
        key="fixture-resurfaced-study",
        items=[
            NewsItem(
                title="Scientists highlight new diabetes and exercise findings",
                url="https://www.sciencedaily.com/releases/2026/08/fixture-resurfaced.htm",
                source="ScienceDaily",
                published=iso(utc_now() - timedelta(hours=6)),
                summary="A university media office promoted a study on diabetes and exercise. DOI: 10.1000/resurfaced-study.",
                source_type="research_media",
            )
        ],
    )
    original_resolve_canonical_date_2 = editorial_gates.resolve_canonical_research_date
    canonical_study_date = iso(utc_now() - timedelta(days=100))
    editorial_gates.resolve_canonical_research_date = lambda dois, pmids, **_kwargs: {
        "canonical_published_at": canonical_study_date,
        "canonical_date_source": "crossref_doi",
    }
    try:
        date_info = canonical_research_date_info(resurfaced_cluster)
        assert date_info["canonical_published_at"] == canonical_study_date
        assert date_info["is_resurfaced_research"] is True, "resurfaced research was not flagged despite a large publicity/canonical date gap"
        assert date_info["resurfacing_reason"]
        resolved_published_at = true_published_at(resurfaced_cluster)
        assert resolved_published_at == canonical_study_date, "true_published_at() used the publicity pickup date instead of the canonical study date"
        resurfaced_age = age_hours_for_cluster(resurfaced_cluster)
        assert resurfaced_age is not None and resurfaced_age > 24 * 90, "resurfaced study age was computed from the publicity date, not the canonical date"
    finally:
        editorial_gates.resolve_canonical_research_date = original_resolve_canonical_date_2

    # Known failure #2: an RP-derived video must be blocked by source
    # saturation if an RP-derived story was already published recently,
    # even under different name spellings (RP / Renaissance Periodization
    # / Dr Mike Israetel all resolve to the same creator_key).
    saturation_db = Path("/tmp/mfo-scanner-saturation-fixture.db")
    if saturation_db.exists():
        saturation_db.unlink()
    saturation_conn = connect_db(saturation_db)
    fixture_gates_config_2 = editorial_gates.load_config(EDITORIAL_GATES_CONFIG_PATH)
    rp_key = editorial_gates.normalize_creator_key("Renaissance Periodization", fixture_gates_config_2)
    editorial_gates.record_publication_history(
        saturation_conn,
        page_url="https://mensfitnessonline.com.au/dr-mike-israetel-supplements/",
        creator_key=editorial_gates.normalize_creator_key("Dr Mike Israetel", fixture_gates_config_2),
        creator_display_name="Renaissance Periodization",
        published_at=iso(utc_now() - timedelta(days=3)),
        format_=None,
    )
    saturation_conn.commit()
    saturation = editorial_gates.compute_saturation(rp_key, saturation_conn, fixture_gates_config_2)
    assert saturation["source_saturation"]["status"] == "blocked", "recent RP-derived story did not block a new RP-derived lead via source saturation"
    assert saturation["source_saturation"]["recent_story_count"] == 1
    unrelated_saturation = editorial_gates.compute_saturation("jeff_nippard", saturation_conn, fixture_gates_config_2)
    assert unrelated_saturation["source_saturation"]["status"] == "clear", "an unrelated creator was incorrectly flagged as saturated"

    # Real WordPress REST API `date` fields carry no UTC offset (e.g.
    # "2026-08-10T09:24:11", not "...Z"), unlike every other timestamp this
    # scanner generates internally via iso(). A live scan crashed the first
    # time this path ran against real archive data because parse_dt() left
    # that string timezone-naive, and naive - aware datetime subtraction
    # raises TypeError. Confirm a naive published_at no longer crashes
    # compute_saturation() and is still treated as a real, recent match.
    naive_key = "naive_date_creator"
    editorial_gates.record_publication_history(
        saturation_conn,
        page_url="https://mensfitnessonline.com.au/naive-date-fixture/",
        creator_key=naive_key,
        creator_display_name="Naive Date Creator",
        published_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"),
        format_=None,
    )
    saturation_conn.commit()
    naive_date_saturation = editorial_gates.compute_saturation(naive_key, saturation_conn, fixture_gates_config_2)
    assert naive_date_saturation["source_saturation"]["status"] == "blocked", "a timezone-naive published_at (as WordPress's REST API returns) crashed or was not recognised as a recent match"

    # End-to-end: creator_lead_payload() itself must surface the cooldown
    # for a new RP video, given the same saturation_conn/history above.
    rp_obs = Observation(
        channel_source="fixture RP",
        channel_name="Renaissance Periodization",
        video_title="Francis Ngannou training breakdown",
        video_url="https://www.youtube.com/watch?v=rpFixtureVideo1",
        video_id="rpFixtureVideo1",
        upload_datetime=iso(utc_now() - timedelta(hours=2)),
        view_count=200000,
        duration_seconds=900,
        video_type="standard",
        scan_timestamp=iso(utc_now()),
        age_hours=2,
        total_views_per_hour=100000.0,
    )
    rp_payload = creator_lead_payload(rp_obs, {}, [], "new_lead", conn=saturation_conn)
    assert rp_payload["source_saturation"]["status"] == "blocked", "creator_lead_payload() did not surface the RP source-saturation cooldown"
    assert "creator_source_cooldown" in rp_payload["kill_reason_codes"]

    print(f"Fixture tests passed: {creator_report}, {news_report}")


def run_scan(
    channels: list[str],
    db_path: Path,
    report_path: Path,
    profiles: dict[str, dict[str, str]],
    mfo_index: dict[str, Any],
    scan_kind: str,
) -> list[Observation]:
    scan_timestamp = iso(utc_now())
    conn = connect_db(db_path)
    observations: list[Observation] = []
    errors: list[str] = []

    with conn:
        scan_id = create_scan(conn, scan_timestamp, len(channels), scan_kind)
        for channel_source in channels:
            try:
                videos = fetch_channel_videos(channel_source)
            except Exception as exc:
                message = f"{channel_source}: {exc}"
                errors.append(message)
                continue

            if not videos:
                errors.append(f"{channel_source}: no videos found")
                continue

            for video in videos[:VIDEOS_PER_CHANNEL]:
                if not isinstance(video, dict):
                    errors.append(f"{channel_source}: unavailable video metadata")
                    continue
                try:
                    obs = enrich_growth(conn, build_observation(channel_source, video, scan_timestamp))
                except Exception as exc:
                    errors.append(f"{channel_source}: failed to parse video metadata: {exc}")
                    continue
                observations.append(obs)
                save_observation(conn, scan_id, obs)
                if obs.error:
                    errors.append(f"{channel_source}: {obs.video_title}: {obs.error}")

        conn.execute("UPDATE scans SET error_count = ? WHERE id = ?", (len(errors), scan_id))

    write_report(observations, errors, scan_timestamp, report_path, profiles, mfo_index, conn)
    return observations


def insert_fixture_scan(
    db_path: Path,
    report_path: Path,
    profiles: dict[str, dict[str, str]] | None = None,
    mfo_index: dict[str, Any] | None = None,
) -> None:
    """Create local scans that prove growth and breakout math without network."""
    conn = connect_db(db_path)
    first = "2026-08-06T00:00:00Z"
    second = "2026-08-06T12:00:00Z"
    third = "2026-08-07T00:00:00Z"
    fixtures = [
        ("Test Standard Channel", "Fixture standard steady", "fixture-standard-1", 1000, 1300, 1600, 900),
        ("Test Standard Channel", "Fixture standard breakout", "fixture-standard-2", 500, 1700, 4100, 1200),
        ("Test Shorts Channel", "Fixture short steady", "fixture-shorts-1", 2000, 2600, 3200, 45),
        ("Test Shorts Channel", "Fixture short breakout", "fixture-shorts-2", 300, 2100, 5700, 50),
    ]
    third_observations: list[Observation] = []
    with conn:
        scan1 = create_scan(conn, first, 2, "scheduled")
        for channel, title, video_id, first_views, _second_views, _third_views, duration in fixtures:
            obs = Observation(
                channel_source=f"fixture:{channel}",
                channel_name=channel,
                video_title=title,
                video_url=f"https://www.youtube.com/watch?v={video_id}",
                video_id=video_id,
                upload_datetime="2026-08-05T00:00:00Z",
                view_count=first_views,
                duration_seconds=duration,
                video_type="shorts" if duration <= SHORTS_MAX_SECONDS else "standard",
                scan_timestamp=first,
                age_hours=24.0,
                total_views_per_hour=first_views / 24.0,
            )
            save_observation(conn, scan1, obs)

        scan2 = create_scan(conn, second, 2, "scheduled")
        for channel, title, video_id, _first_views, second_views, _third_views, duration in fixtures:
            obs = Observation(
                channel_source=f"fixture:{channel}",
                channel_name=channel,
                video_title=title,
                video_url=f"https://www.youtube.com/watch?v={video_id}",
                video_id=video_id,
                upload_datetime="2026-08-05T00:00:00Z",
                view_count=second_views,
                duration_seconds=duration,
                video_type="shorts" if duration <= SHORTS_MAX_SECONDS else "standard",
                scan_timestamp=second,
                age_hours=36.0,
                total_views_per_hour=second_views / 36.0,
            )
            obs = enrich_growth(conn, obs)
            save_observation(conn, scan2, obs)

        scan3 = create_scan(conn, third, 2, "scheduled")
        for channel, title, video_id, _first_views, _second_views, third_views, duration in fixtures:
            obs = Observation(
                channel_source=f"fixture:{channel}",
                channel_name=channel,
                video_title=title,
                video_url=f"https://www.youtube.com/watch?v={video_id}",
                video_id=video_id,
                upload_datetime="2026-08-05T00:00:00Z",
                view_count=third_views,
                duration_seconds=duration,
                video_type="shorts" if duration <= SHORTS_MAX_SECONDS else "standard",
                scan_timestamp=third,
                age_hours=48.0,
                total_views_per_hour=third_views / 48.0,
            )
            obs = enrich_growth(conn, obs)
            save_observation(conn, scan3, obs)
            third_observations.append(obs)

    write_report(third_observations, [], third, report_path, profiles or {}, mfo_index or {}, conn)


def should_auto_open_reports(args: argparse.Namespace) -> bool:
    if args.open_reports:
        return True
    if args.no_open_reports:
        return False
    return sys.stdout.isatty()


def open_reports_in_word(paths: list[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        try:
            subprocess.run(["open", "-a", "Microsoft Word", str(path)], check=False)
        except OSError as exc:
            print(f"Could not open {path} in Microsoft Word: {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan configured YouTube channels for fast-growing videos.")
    parser.add_argument("--channels", type=Path, default=CHANNELS_PATH, help="Path to channels.json")
    parser.add_argument("--source-profiles", type=Path, default=SOURCE_PROFILES_PATH, help="Path to editorial source profiles")
    parser.add_argument("--mfo-index", type=Path, default=MFO_INDEX_PATH, help="Path to cached MFO archive index")
    parser.add_argument("--mfo-site", default=MFO_SITE_URL, help="Public MFO site URL")
    parser.add_argument("--refresh-mfo-index", action="store_true", help="Refresh the MFO archive index before scanning")
    parser.add_argument("--skip-mfo-index", action="store_true", help="Do not use the MFO archive index")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--report", type=Path, default=REPORT_PATH, help="Path to Markdown report")
    parser.add_argument("--news-report", type=Path, default=NEWS_REPORT_PATH, help="Path to News Radar Markdown report")
    parser.add_argument("--news-sources", type=Path, default=NEWS_SOURCES_PATH, help="Path to news_sources.json")
    parser.add_argument("--news-queries", type=Path, default=NEWS_QUERIES_PATH, help="Path to news_queries.json")
    parser.add_argument("--research-report", type=Path, default=RESEARCH_REPORT_PATH, help="Path to Research Radar Markdown report")
    parser.add_argument("--research-queries", type=Path, default=RESEARCH_QUERIES_PATH, help="Path to research_queries.json")
    parser.add_argument("--skip-creator", action="store_true", help="Skip Creator Radar")
    parser.add_argument("--skip-news", action="store_true", help="Skip News Radar")
    parser.add_argument("--skip-research", action="store_true", help="Skip Research Radar")
    parser.add_argument("--scan-kind", choices=["scheduled", "manual"], default=None, help="Mark this Creator scan as scheduled or manual")
    parser.add_argument("--open-reports", action="store_true", help="Open generated Markdown reports in Microsoft Word after the run")
    parser.add_argument("--no-open-reports", action="store_true", help="Do not open generated reports after the run")
    parser.add_argument("--limit", type=int, default=None, help="Scan only the first N configured channels")
    parser.add_argument(
        "--fixture-growth-test",
        action="store_true",
        help="Write two fixture scans to prove growth calculations without network",
    )
    parser.add_argument("--fixture-tests", action="store_true", help="Run fixture assertions for Creator and News Radar")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profiles = load_source_profiles(args.source_profiles)
    mfo_index: dict[str, Any] = {}
    if not args.skip_mfo_index:
        full_scan = not args.skip_creator and not args.skip_news and not args.skip_research
        try:
            lexicon_channels = load_channels(args.channels)
        except (FileNotFoundError, ValueError):
            lexicon_channels = []
        creator_lexicon = editorial_gates.build_creator_lexicon(lexicon_channels, gates_config())
        mfo_index = load_mfo_index(args.mfo_index, args.mfo_site, refresh=args.refresh_mfo_index or full_scan, creator_lexicon=creator_lexicon)
        synced = sync_publication_history(connect_db(args.db), mfo_pages_from_index(mfo_index))
        if synced:
            print(f"Publication history: attributed {synced} archived pages to a known creator.")

    if args.fixture_growth_test:
        insert_fixture_scan(args.db, args.report, profiles, mfo_index)
        print(f"Fixture growth test written to {args.db} and {args.report}")
        return 0
    if args.fixture_tests:
        run_fixture_tests()
        return 0

    scan_kind = args.scan_kind or ("manual" if args.limit is not None else "scheduled")
    generated_reports: list[Path] = []
    if not args.skip_creator:
        channels = load_channels(args.channels)
        if args.limit is not None:
            channels = channels[: args.limit]
        observations = run_scan(channels, args.db, args.report, profiles, mfo_index, scan_kind)
        print(f"Creator Radar observed {len([obs for obs in observations if obs.status == 'ok'])} videos.")
        print(f"Creator report: {args.report}")
        generated_reports.append(args.report)
    if not args.skip_news:
        clusters = run_news_radar(args.db, args.news_report, args.news_sources, args.news_queries, mfo_index)
        print(f"News Radar clustered {len(clusters)} candidate stories.")
        print(f"News report: {args.news_report}")
        generated_reports.append(args.news_report)
    if not args.skip_research:
        try:
            papers = run_research_radar(args.db, args.research_report, args.research_queries, mfo_index)
            print(f"Research Radar assessed {len(papers)} papers.")
            print(f"Research report: {args.research_report}")
            generated_reports.append(args.research_report)
        except Exception as exc:
            print(f"Research Radar failed: {exc}", file=sys.stderr)
            if not args.skip_creator or not args.skip_news:
                pass
            else:
                return 1
    print(f"Database: {args.db}")
    if mfo_index:
        print(f"MFO index: {args.mfo_index}")
    if should_auto_open_reports(args):
        open_reports_in_word(generated_reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
