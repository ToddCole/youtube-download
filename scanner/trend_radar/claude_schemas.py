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


StoryType = Literal["new", "update"]


class Candidate(BaseModel):
    rank: int = Field(ge=1, le=6)
    headline: str
    story_type: StoryType = Field(
        default="new",
        description='"update" when MFO already has a page on this subject and the pick is to update it; otherwise "new".',
    )
    update_url: str | None = Field(
        default=None,
        description="For an update: the existing MFO page URL, exactly as given (mfo_pages_on_this_subject, EDITOR FEEDBACK or OPEN MFO STORIES). Never guessed.",
    )
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


class EvergreenAngle(BaseModel):
    topic: str = Field(description="Short working title for the evergreen piece.")
    angle: str = Field(description="The original, lasting MFO angle: what the piece explains or teaches, not the old news.")
    why_it_lasts: str = Field(description="One line on why readers will still search for this in months.")
    update_url: str | None = Field(
        default=None,
        description="An existing MFO page to expand instead, exactly as given to you. Never guessed.",
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
    candidates: list[Candidate] = Field(description="0 to 6 picks, ranked by what MFO should do first. Never padded: return fewer, or none, when fewer clear the bar.")
    watchlist: list[str] = Field(default_factory=list, description="Items worth holding (too early, too narrow, or needing an original angle), one line each with the reason.")
    rejected_or_merged_topics: list[str] = Field(default_factory=list, description="Short notes on waves that were considered and rejected or merged into a candidate above.")
    evergreen_angles: list[EvergreenAngle] = Field(
        default_factory=list,
        description="0 to 3 held items (old news, no fresh hook) where MFO could still publish a lasting explainer or guide.",
    )


# --- Fit-food pass ------------------------------------------------------

FitFoodFormat = Literal["news post", "explainer", "add to existing post"]


class FitFoodPick(BaseModel):
    headline: str = Field(description="Headline idea: plain, punchy, no clickbait.")
    why_care: str = Field(description="One line: why a man who trains should care.")
    first_published: str = Field(description="The date the story first came out (YYYY-MM-DD), not a re-run date.")
    original_source_url: str | None = Field(
        default=None,
        description="The original source (paper, regulator notice, company statement), exactly as it appears in the research notes. Never guessed.",
    )
    original_source_opened: bool = Field(description="True only if the notes say the original source itself was opened and read.")
    other_outlet_url: str | None = Field(default=None, description="One other outlet covering it, exactly as in the notes, if any.")
    facts: str = Field(description="Study details (journal, year, type, n, who, how long, main result in numbers, funder) or the regulator facts.")
    search_term: str = Field(description="The search a reader would type (lowercase, no quotes).")
    demand_note: str | None = Field(default=None, description="Any search-demand evidence from the notes; otherwise leave empty.")
    format: FitFoodFormat
    update_url: str | None = Field(default=None, description="For 'add to existing post': the MFO URL, exactly as given. Never guessed.")
    fit_score: int = Field(ge=1, le=10, description="How good a fit for MFO, out of 10.")
    risk: str = Field(description="One line on any risk: thin evidence, industry-funded, a health claim needing care.")


class FitFoodOutput(BaseModel):
    summary: str = Field(description="One or two sentences on what this fit-food search found overall.")
    picks: list[FitFoodPick] = Field(description="0 to 3 fit-food picks, best first. Never padded.")
    left_out: list[str] = Field(default_factory=list, description="Short notes on stories considered and left out, and why.")
