"""Today board + Agent Review via the Claude API.

- trend_leads(): turns the latest persisted Claude Trend Radar scan into
  Desk leads (lead_id "trend:<scan_id>:<rank>") so they can be
  commissioned through the existing decisions / production queue.
- run_agent_review(): sends the existing supervisor prompt + review
  packet to Claude and returns a response that already satisfies
  editorial_supervisor_response.schema.json, replacing the manual
  copy-paste to ChatGPT.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

SCANNER_DIR = Path(__file__).resolve().parent
TREND_DB = SCANNER_DIR / "trend_radar.db"
MFO_INDEX = SCANNER_DIR / "mfo_index.json"
REVIEW_MAX_TOKENS = 32000

_STOP = {
    "the", "and", "for", "with", "what", "how", "why", "your", "you", "are", "this", "that", "from",
    "can", "into", "after", "about", "just", "more", "new", "its", "his", "her", "who", "was", "has",
    # generic headline words that make unrelated pages look related
    "according", "know", "actually", "increase", "perfect", "day", "guide", "fitness", "men", "man",
    "mens", "women", "woman", "best", "really", "should", "need", "way", "ways", "tips", "online",
    "power", "keep", "asking", "playbook", "what", "here", "s", "2025", "2026", "2027",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def related_mfo_pages(text: str, limit: int = 3, min_overlap: int = 2) -> list[dict]:
    """Cheap 'already covered?' hint: MFO archive pages sharing the most
    distinctive words with the story. A hint for the editor, not a gate."""
    try:
        index = json.loads(MFO_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    want = _tokens(text)
    if not want:
        return []
    scored = []
    for page in index.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_tokens = _tokens(f"{page.get('title', '')} {page.get('slug', '')}")
        overlap = want & page_tokens
        # Share of the *page's* distinctive words found in the story: a page
        # only counts as related when most of what it is about is present.
        share = len(overlap) / max(len(page_tokens), 1)
        if len(overlap) >= min_overlap and share >= 0.4:
            scored.append((share, len(overlap), page.get("title") or page.get("slug") or "", page.get("url") or ""))
    scored.sort(key=lambda row: (-row[0], -row[1]))
    return [{"title": title, "url": url, "shared_words": n} for _share, n, title, url in scored[:limit]]


def latest_trend_scan(db_path: Path | None = None) -> dict | None:
    db_path = db_path or TREND_DB
    if not db_path.exists():
        return None
    from trend_radar.scan_store import latest_scan

    conn = sqlite3.connect(db_path)
    try:
        return latest_scan(conn)
    finally:
        conn.close()


def candidate_to_lead(scan_id: int, generated_at: str | None, candidate: dict) -> dict:
    evidence = candidate.get("evidence") or []
    links = [e.get("url") for e in evidence if e.get("url")]
    sources = sorted({e.get("source") for e in evidence if e.get("source")})
    lead = {
        "lead_id": f"trend:{scan_id}:{candidate.get('rank')}",
        "scanner_type": "trend",
        "title": candidate.get("headline", ""),
        "source_summary": candidate.get("topic_summary", ""),
        "why_moving_now": candidate.get("why_moving_now", ""),
        "likely_mfo_angle": candidate.get("mfo_angle", ""),
        "confidence": candidate.get("confidence"),
        "evidence_strength": candidate.get("evidence_strength"),
        "independent_sources": candidate.get("independent_sources"),
        "lanes": candidate.get("lanes") or [],
        "verification_needed": candidate.get("verification_needed") or [],
        "editorial_cautions": candidate.get("editorial_cautions") or [],
        "source_url": links[0] if links else "",
        "evidence_links": links,
        "supporting_sources": sources,
        "evidence": [
            {k: e.get(k) for k in ("title", "source", "lane", "published_at", "url", "relevance")}
            for e in evidence
        ],
        "scan_generated_at": generated_at,
        "rank": candidate.get("rank"),
    }
    lead["related_mfo_pages"] = related_mfo_pages(f"{lead['title']} {lead['source_summary']}")
    return lead


def trend_leads(scan: dict | None = None) -> tuple[dict | None, list[dict]]:
    scan = scan if scan is not None else latest_trend_scan()
    if not scan:
        return None, []
    scan_id = scan.get("_scan_id", 0)
    leads = [candidate_to_lead(scan_id, scan.get("generated_at"), c) for c in scan.get("candidates") or []]
    meta = {
        "scan_id": scan_id,
        "generated_at": scan.get("generated_at"),
        "model": scan.get("model"),
        "cost_usd": (scan.get("usage") or {}).get("cost_usd"),
        "corpus_summary": scan.get("corpus_summary"),
        "watchlist": scan.get("watchlist") or [],
        "packet_size": scan.get("_evidence_packet_size"),
        "repairs": scan.get("_validation_repairs") or [],
    }
    return meta, leads


def find_trend_lead(lead_id: str) -> dict | None:
    match = re.fullmatch(r"trend:(\d+):(\d+)", lead_id or "")
    if not match or not TREND_DB.exists():
        return None
    scan_id, rank = int(match.group(1)), int(match.group(2))
    conn = sqlite3.connect(TREND_DB)
    try:
        row = conn.execute("SELECT result_json FROM claude_scans WHERE id = ?", (scan_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    scan = json.loads(row[0])
    for candidate in scan.get("candidates") or []:
        if candidate.get("rank") == rank:
            return candidate_to_lead(scan_id, scan.get("generated_at"), candidate)
    return None


# --- Agent Review via API ------------------------------------------------

class ReviewedCandidate(BaseModel):
    lead_id: str = Field(description="Exactly as supplied in the packet.")
    scanner_type: Literal["creator", "news", "research", "manual"]
    agent_rating: Literal["Strong", "Possible", "Weak"]
    concise_reason: str
    mfo_angle: str
    evidence_risk: str
    archive_overlap_warning: str
    editorial_rank: int | None = Field(description="1 = best; null when not ranked.")
    why_editorial_ranking_differs: str
    recommended_action: Literal["commission", "hold", "reject"]
    primary_source_url: str | None = None
    facts_to_check: list[str] = Field(default_factory=list)
    notes: str = ""


class ExcludedNote(BaseModel):
    lead_id: str
    reason: str


class SupervisorResponse(BaseModel):
    recommended_ids: list[str] = Field(description="Zero to six lead_ids from reviewed_candidates.")
    reviewed_candidates: list[ReviewedCandidate]
    excluded_notes: list[ExcludedNote] = Field(default_factory=list)
    notes: str = ""


def packet_lead_ids(packet: dict) -> list[str]:
    groups = (packet.get("review_candidates") or {}) if isinstance(packet, dict) else {}
    return [
        lead.get("lead_id")
        for key in ("creator", "news", "research", "manual")
        for lead in groups.get(key) or []
        if isinstance(lead, dict) and lead.get("lead_id")
    ]


# Internal scoring detail the reviewer doesn't need (and which made the
# full packet ~1M tokens). Excluded leads are summarised as counts.
HEAVY_LEAD_FIELDS = {
    "score_json", "topic_overlap_breakdown", "source_fingerprints", "editorial_score_breakdown",
    "score_breakdown", "channel_baseline", "creator_enrichment", "transcript", "transcript_text",
}
DROP_PACKET_KEYS = {"excluded_candidates", "scanner_results", "editorial_supervisor_prompt", "required_response_schema"}


def slim_lead(lead: dict) -> dict:
    slim = {k: v for k, v in lead.items() if k not in HEAVY_LEAD_FIELDS}
    abstract = slim.get("abstract")
    if isinstance(abstract, str) and len(abstract) > 1200:
        slim["abstract"] = abstract[:1200] + "…"
    return slim


def slim_packet_for_api(packet: dict) -> dict:
    slim = {k: v for k, v in packet.items() if k not in DROP_PACKET_KEYS}
    slim["review_candidates"] = {
        group: [slim_lead(lead) for lead in leads if isinstance(lead, dict)]
        for group, leads in (packet.get("review_candidates") or {}).items()
    }
    excluded = packet.get("excluded_candidates") or {}
    slim["excluded_candidate_counts"] = {
        group: len(leads) for group, leads in excluded.items() if isinstance(leads, list)
    }
    return slim


def run_agent_review(packet: dict, prompt: str) -> tuple[dict, dict]:
    """Returns (response_dict, usage_dict). Raises on API/validation failure.
    Uses the shared daily spend ledger, so it counts against the same cap
    as the scheduled news scans."""
    from trend_radar.claude_client import (
        SpendLedger,
        UsageTotals,
        build_client,
        call_structured,
        resolve_model,
    )

    model = resolve_model(None)
    usage = UsageTotals(model=model)
    expected = packet_lead_ids(packet)
    user_content = (
        "Here is today's review packet as JSON. Assess every lead in review_candidates "
        f"({len(expected)} leads) and return one reviewed_candidates entry per lead_id, plus recommended_ids.\n\n"
        + json.dumps(slim_packet_for_api(packet), ensure_ascii=False)
    )
    result: SupervisorResponse = call_structured(
        build_client(),
        model=model,
        system=prompt,
        user_content=user_content,
        output_format=SupervisorResponse,
        max_tokens=REVIEW_MAX_TOKENS,
        usage=usage,
        ledger=SpendLedger(),
    )
    response = result.model_dump()
    response["reviewed_candidates"] = [
        {k: v for k, v in item.items() if not (k == "primary_source_url" and not v)}
        for item in response["reviewed_candidates"]
    ]
    known = {item["lead_id"] for item in response["reviewed_candidates"]}
    response["recommended_ids"] = [lead_id for lead_id in response["recommended_ids"] if lead_id in known][:6]
    missing = [lead_id for lead_id in expected if lead_id not in known]
    if missing:
        response.setdefault("notes", "")
        response["notes"] = (response["notes"] + f" [Desk: no assessment returned for {len(missing)} lead(s).]").strip()
    return response, usage.as_dict()
