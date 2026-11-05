"""Pass B: editorial judgement. Builds the user-turn content (waves plus
their original and expanded evidence) and validates Claude's final
structured response before it is written out anywhere.
"""

from __future__ import annotations

import json

from .claude_evidence import EvidenceRecord
from .claude_schemas import ClaudeScanOutput, DiscoveryOutput

MIN_CANDIDATES = 3
MAX_CANDIDATES = 6


class EditorialValidationError(ValueError):
    """Raised when Claude's final response violates a hard constraint --
    an invented signal_id, a mismatched url/source/lane, an invalid rank,
    or a candidate count outside [3, 6]. Never silently repaired."""


def build_editorial_user_content(
    discovery: DiscoveryOutput,
    evidence_by_id: dict[str, EvidenceRecord],
    expansion_results: dict[str, list[EvidenceRecord]],
) -> str:
    waves_payload = []
    for wave in discovery.waves:
        original = [evidence_by_id[sid].to_prompt_dict() for sid in wave.signal_ids if sid in evidence_by_id]
        expanded = [r.to_prompt_dict() for r in expansion_results.get(wave.wave_id, [])]
        waves_payload.append(
            {
                "wave_id": wave.wave_id,
                "working_label": wave.working_label,
                "cross_source_reasoning": wave.cross_source_reasoning,
                "ambiguous_name_caution": wave.ambiguous_name_caution,
                "evidence": original + expanded,
            }
        )
    return (
        "Here are the candidate story waves from the discovery pass, each with its original evidence "
        "plus any additional evidence found by approved follow-up searches. Return your editorial "
        "judgement per the instructions.\n\n"
        + json.dumps({"waves": waves_payload}, ensure_ascii=False, indent=2)
    )


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
            if item.url is None:
                # url is optional in the schema; signal_id is the authoritative
                # citation, so fill the real stored url rather than reject. A
                # non-None url that disagrees is still rejected below.
                item.url = stored.url
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
