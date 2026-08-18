"""Lane 4: PubMed, reusing scanner.py's own pubmed_search()/pubmed_fetch()/
extract_pubmed_articles() and its NCBI rate-limit compliance
(ncbi_delay_seconds(), ncbi_params()) unchanged. Research items are raw
signals at this stage only -- Phase 1 does not score, rank, or promote
them. That distinction (research is a separate signal lane, publication
is not editorial importance) is enforced upstream by never running these
through clustering/scoring in this phase, not by anything in this file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .._scanner_bridge import scanner
from ..models import RawSignalInput

SEARCH_WINDOW_DAYS = 14


def fetch_pubmed_source(source_row: sqlite3.Row) -> list[RawSignalInput]:
    query = source_row["canonical_identifier"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=SEARCH_WINDOW_DAYS)
    pmids = scanner.pubmed_search(query, start, end, max_results=25)
    if not pmids:
        return []
    xml_text = scanner.pubmed_fetch(pmids)
    papers = scanner.extract_pubmed_articles(xml_text, {pmid: source_row["name"] for pmid in pmids})

    signals = []
    for paper in papers:
        engagement: dict = {}
        url = paper.pubmed_url or (f"https://pubmed.ncbi.nlm.nih.gov/{paper.pmid}/" if paper.pmid else None)
        signals.append(
            RawSignalInput(
                source_id=source_row["source_id"],
                lane=source_row["lane"],
                platform="pubmed",
                source_name=source_row["name"],
                source_role=source_row["source_role"],
                url=url,
                title=paper.title,
                body_text=paper.abstract or "",
                author_or_account=paper.journal,
                published_at=paper.electronic_publication_date or paper.publication_date,
                engagement=engagement,
            )
        )
    return signals


def fetch_official_rss_source(source_row: sqlite3.Row) -> list[RawSignalInput]:
    """Non-PubMed Lane 4 sources that are plain RSS (e.g. Sports Medicine
    Australia, confirmed working in Phase 0). Deliberately delegates to
    the same reused rss_items_from_xml() path as Lane 1 -- an RSS feed is
    an RSS feed regardless of lane; only the lane/source_role differ."""
    from .publishers import fetch_rss_source

    return fetch_rss_source(source_row)


def fetch_for(source_row: sqlite3.Row) -> list[RawSignalInput]:
    if source_row["platform"] == "pubmed":
        return fetch_pubmed_source(source_row)
    return fetch_official_rss_source(source_row)
