"""Pass B: editorial judgement. Builds the user-turn content (waves plus
their original and expanded evidence) and validates Claude's final
structured response before it is written out anywhere.
"""

from __future__ import annotations

import json

from .claude_evidence import EvidenceRecord
from .claude_schemas import ClaudeScanOutput, DiscoveryOutput

MFO_URL_PREFIX = "https://mensfitnessonline.com.au/"
MIN_CANDIDATES = 0  # a quiet day may have nothing worth MFO's time
MAX_CANDIDATES = 6


class EditorialValidationError(ValueError):
    """Raised when Claude's final response violates a hard constraint --
    an invented signal_id, a mismatched url/source/lane, an invalid rank,
    or a candidate count outside [3, 6]. Never silently repaired."""


def _default_related_pages(text: str) -> list[dict]:
    """MFO archive pages on the same subject (scanner/editorial_today.py);
    empty when the archive index isn't available."""
    try:
        from editorial_today import related_mfo_pages
    except ImportError:
        return []
    try:
        return related_mfo_pages(text, limit=4)
    except Exception:  # noqa: BLE001
        return []


def build_editorial_user_content(
    discovery: DiscoveryOutput,
    evidence_by_id: dict[str, EvidenceRecord],
    expansion_results: dict[str, list[EvidenceRecord]],
    refs: dict[str, str] | None = None,
    related_pages=_default_related_pages,
) -> str:
    def as_prompt(record: EvidenceRecord) -> dict:
        if refs:
            return record.to_prompt_dict(ref=refs[record.signal_id], include_url=False)
        return record.to_prompt_dict()

    waves_payload = []
    for wave in discovery.waves:
        original = [as_prompt(evidence_by_id[sid]) for sid in wave.signal_ids if sid in evidence_by_id]
        expanded = [as_prompt(r) for r in expansion_results.get(wave.wave_id, [])]
        subject = " ".join([wave.working_label] + [e.get("title") or "" for e in original + expanded])
        mfo_pages = [{"title": p.get("title"), "url": p.get("url")} for p in (related_pages(subject) if related_pages else [])]
        waves_payload.append(
            {
                "wave_id": wave.wave_id,
                "working_label": wave.working_label,
                "cross_source_reasoning": wave.cross_source_reasoning,
                "ambiguous_name_caution": wave.ambiguous_name_caution,
                "evidence": original + expanded,
                # Existing MFO coverage of this subject (archive match): a pick on
                # it is probably an update to one of these, not a new story.
                "mfo_pages_on_this_subject": mfo_pages,
            }
        )
    return (
        "Here are the candidate story waves from the discovery pass, each with its original evidence "
        "plus any additional evidence found by approved follow-up searches. Return your editorial "
        "judgement per the instructions.\n\n"
        + json.dumps({"waves": waves_payload}, ensure_ascii=False, indent=None if refs else 2)
    )


def build_research_user_content(
    discovery: DiscoveryOutput,
    evidence_by_id: dict[str, EvidenceRecord],
    expansion_results: dict[str, list[EvidenceRecord]],
    max_items_per_wave: int = 8,
) -> str:
    """Compact view of each discovery wave for the web research step:
    label, reasoning and the headlines/sources/dates behind it."""
    lines = ["Candidate stories from today's discovery pass:", ""]
    for wave in discovery.waves:
        records = [evidence_by_id[sid] for sid in wave.signal_ids if sid in evidence_by_id]
        records += expansion_results.get(wave.wave_id, [])
        lines.append(f"### {wave.wave_id}: {wave.working_label}")
        lines.append(f"Evidence reasoning: {wave.cross_source_reasoning}")
        if wave.ambiguous_name_caution:
            lines.append(f"Ambiguity caution: {wave.ambiguous_name_caution}")
        for r in records[:max_items_per_wave]:
            lines.append(f"- {r.title} ({r.source}, {(r.published_at or '')[:10]})")
        lines.append("")
    return "\n".join(lines)


def repair_final_output(output: ClaudeScanOutput, known_signal_ids: set[str]) -> list[str]:
    """Visible, logged repair: drops evidence items citing ids that were not
    supplied, drops candidates left with no evidence, and renumbers ranks
    1..n in their original order. The candidate-count check in
    validate_final_output still applies afterwards."""
    notes: list[str] = []
    kept = []
    for candidate in sorted(output.candidates, key=lambda c: c.rank):
        unknown = [item.signal_id for item in candidate.evidence if item.signal_id not in known_signal_ids]
        if unknown:
            candidate.evidence = [item for item in candidate.evidence if item.signal_id in known_signal_ids]
            notes.append(f"editorial {candidate.headline[:60]!r}: dropped unknown signal_id(s) {unknown}")
        if not candidate.evidence:
            notes.append(f"editorial {candidate.headline[:60]!r}: dropped candidate with no valid evidence")
            continue
        if candidate.update_url and not candidate.update_url.startswith(MFO_URL_PREFIX):
            notes.append(f"editorial {candidate.headline[:60]!r}: dropped non-MFO update_url {candidate.update_url!r}")
            candidate.update_url = None
        if candidate.story_type == "update" and not candidate.update_url:
            notes.append(f"editorial {candidate.headline[:60]!r}: update without a valid MFO page, treated as new")
            candidate.story_type = "new"
        kept.append(candidate)
    for index, candidate in enumerate(kept, start=1):
        candidate.rank = index
    output.candidates = kept
    for angle in output.evergreen_angles:
        if angle.update_url and not angle.update_url.startswith(MFO_URL_PREFIX):
            notes.append(f"evergreen {angle.topic[:60]!r}: dropped non-MFO update_url {angle.update_url!r}")
            angle.update_url = None
    output.evergreen_angles = output.evergreen_angles[:3]
    return notes


def validate_final_output(
    output: ClaudeScanOutput,
    known_signal_ids: set[str],
    evidence_by_id: dict[str, EvidenceRecord],
) -> None:
    if not (MIN_CANDIDATES <= len(output.candidates) <= MAX_CANDIDATES):
        raise EditorialValidationError(
            f"expected {MIN_CANDIDATES}-{MAX_CANDIDATES} candidates, got {len(output.candidates)}"
        )

    seen_ranks: set[int] = set()
    valid_ranks = set(range(1, len(output.candidates) + 1))
    for candidate in output.candidates:
        if candidate.rank in seen_ranks:
            raise EditorialValidationError(f"duplicate candidate rank: {candidate.rank}")
        seen_ranks.add(candidate.rank)
        if candidate.rank not in valid_ranks:
            raise EditorialValidationError(
                f"candidate rank {candidate.rank} is not a valid rank for {len(output.candidates)} candidates"
            )

        if not candidate.evidence:
            raise EditorialValidationError(f"candidate rank {candidate.rank} ({candidate.headline!r}) cites zero evidence")

        for item in candidate.evidence:
            if item.signal_id not in known_signal_ids:
                raise EditorialValidationError(
                    f"candidate rank {candidate.rank} cites signal_id {item.signal_id!r}, "
                    "which was not supplied as evidence in this pass"
                )
            stored = evidence_by_id.get(item.signal_id)
            if stored is None:
                raise EditorialValidationError(
                    f"candidate rank {candidate.rank} cites signal_id {item.signal_id!r} with no matching stored record"
                )
            # Only signal_id is required from the model; everything else is
            # filled from the stored record. A value the model *did* supply
            # that disagrees with the record is still rejected below.
            if item.url is None:
                item.url = stored.url
            if item.source is None:
                item.source = stored.source
            if item.lane is None:
                item.lane = stored.lane
            if item.title is None:
                item.title = stored.title
            if item.published_at is None:
                item.published_at = stored.published_at
            if item.url != stored.url:
                raise EditorialValidationError(
                    f"candidate rank {candidate.rank} cites url {item.url!r} for signal_id {item.signal_id!r}, "
                    f"but the stored url is {stored.url!r}"
                )
            if item.source != stored.source:
                raise EditorialValidationError(
                    f"candidate rank {candidate.rank} cites source {item.source!r} for signal_id {item.signal_id!r}, "
                    f"but the stored source is {stored.source!r}"
                )
            if item.lane != stored.lane:
                raise EditorialValidationError(
                    f"candidate rank {candidate.rank} cites lane {item.lane!r} for signal_id {item.signal_id!r}, "
                    f"but the stored lane is {stored.lane!r}"
                )
