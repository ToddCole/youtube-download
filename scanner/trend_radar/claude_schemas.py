"""Pydantic schemas for both Claude passes' Structured Outputs.

Kept deliberately shallow (per the brief) so structured generation stays
reliable: no deeply-nested optional unions, no recursive types, no free-
form dicts. Every field that matters for post-response validation
(signal_id, url, source, lane) is a plain string so Python can compare
it directly against what's actually stored in raw_signals.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- Pass A: discovery -----------------------------------------------

EvidencePlatform = Literal["google-news", "youtube"]


class ExpansionQueryProposal(BaseModel):
    query: str = Field(description="A concise search query built only from terms present in this wave's own evidence.")
    platform: EvidencePlatform
    rationale: str = Field(description="What specific missing evidence this query would help surface, and why.")


class DiscoveryWave(BaseModel):
    wave_id: str = Field(description="A short slug you invent to label this wave, e.g. 'wave-1'. Not a signal_id.")
    working_label: str = Field(description="A short human-readable label for what this wave is about.")
    signal_ids: list[str] = Field(description="Signal IDs from the supplied evidence packet that belong to this wave. Never invent an ID.")
    cross_source_reasoning: str = Field(description="Why this looks like genuine spread across independent sources, or a caution that it may be one publisher network / one creator repeating itself.")
    ambiguous_name_caution: str | None = Field(default=None, description="Note any name/term in this wave that could plausibly refer to something unrelated, if applicable.")
    expansion_queries: list[ExpansionQueryProposal] = Field(default_factory=list, description="Up to two proposed searches that would add missing evidence for this wave.")


class DiscoveryOutput(BaseModel):
    waves: list[DiscoveryWave] = Field(description="Up to 12 emerging story waves found in the supplied evidence.")


# --- Pass B: editorial judgement --------------------------------------

ConfidenceLevel = Literal["low", "medium", "high"]
EvidenceStrength = Literal["weak", "moderate", "strong"]


class EvidenceItem(BaseModel):
    signal_id: str = Field(description="The signal_id exactly as given to you (e.g. 'S012') -- never invented.")
    relevance: str = Field(description="One sentence on why this specific piece of evidence supports the candidate.")
    # Filled from the stored record after the response -- leave these out.
    title: str | None = None
    source: str | None = None
    lane: str | None = None
    published_at: str | None = None
    url: str | None = None


class Candidate(BaseModel):
    rank: int = Field(ge=1, le=6)
    headline: str
    topic_summary: str = Field(description="What the story actually is.")
    why_moving_now: str
    mfo_angle: str = Field(description="The publishable MFO angle on this story.")
    confidence: ConfidenceLevel
    evidence_strength: EvidenceStrength
    lanes: list[str]
    independent_sources: int = Field(ge=0)
    evidence: list[EvidenceItem]
    verification_needed: list[str] = Field(default_factory=list)
    editorial_cautions: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(
        default_factory=list,
        description="2-5 short Google searches a reader would type to find this story or its MFO angle (lowercase, no quotes).",
    )


class ClaudeScanOutput(BaseModel):
    """What Claude itself generates in Pass B. Deliberately does NOT
    include generated_at/model/usage -- Claude cannot know its own
    current wall-clock time or token usage while producing the very
    response being measured, and asking it to invent them would violate
    the "every factual statement must be traceable" requirement. Python
    attaches those three fields to the final on-disk JSON after the
    call completes -- see claude_scan.py:assemble_final_result()."""

    corpus_summary: str = Field(description="A short summary of what the supplied evidence corpus contained overall.")
    candidates: list[Candidate] = Field(description="3 to 6 editorial candidates, ranked.")
    watchlist: list[str] = Field(default_factory=list, description="Optional short list of borderline items worth watching but not yet a full candidate.")
    rejected_or_merged_topics: list[str] = Field(default_factory=list, description="Short notes on waves that were considered and rejected or merged into a candidate above.")
