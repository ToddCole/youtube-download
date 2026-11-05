"""Pass A: discovery. Builds the user-turn content Claude sees, and
validates the structured response it returns before anything downstream
is allowed to use it.
"""

from __future__ import annotations

import json

from .claude_evidence import EvidenceRecord
from .claude_schemas import DiscoveryOutput

MAX_WAVES = 12
MAX_EXPANSION_QUERIES_PER_WAVE = 2


class DiscoveryValidationError(ValueError):
    """Raised when Claude's discovery response cites a signal_id it was
    not given, or otherwise violates a hard constraint. Never silently
    repaired -- callers must treat this as a failed pass."""


def build_discovery_user_content(evidence: list[EvidenceRecord], refs: dict[str, str] | None = None) -> str:
    if refs:
        lines = [json.dumps(e.to_prompt_dict(ref=refs[e.signal_id], include_url=False), ensure_ascii=False) for e in evidence]
        body = "\n".join(lines)
    else:
        body = json.dumps({"evidence": [e.to_prompt_dict() for e in evidence]}, ensure_ascii=False, indent=2)
    return (
        f"Here are {len(evidence)} recent signals (one JSON object per line), each already deduplicated by "
        "exact URL. Identify emerging story waves per the instructions.\n\n" + body
    )


def repair_discovery_output(output: DiscoveryOutput, known_signal_ids: set[str]) -> list[str]:
    """Visible, logged repair (never silent): drops cited ids that were not
    in the packet, then drops waves left with no evidence. Dropping can only
    remove citations, never add false ones, so the invariant "every emitted
    citation exists" still holds. Returns human-readable repair notes."""
    notes: list[str] = []
    kept = []
    for wave in output.waves:
        unknown = [sid for sid in wave.signal_ids if sid not in known_signal_ids]
        if unknown:
            wave.signal_ids = [sid for sid in wave.signal_ids if sid in known_signal_ids]
            notes.append(f"discovery {wave.wave_id}: dropped unknown signal_id(s) {unknown}")
        if not wave.signal_ids:
            notes.append(f"discovery {wave.wave_id}: dropped wave with no valid evidence")
            continue
        kept.append(wave)
    output.waves = kept
    if not kept:
        raise DiscoveryValidationError("no discovery wave had any valid evidence after repair")
    return notes


def validate_discovery_output(output: DiscoveryOutput, known_signal_ids: set[str]) -> None:
    """Hard-rejects the whole response if it violates any constraint.
    Raises DiscoveryValidationError with a specific, actionable message
    -- never drops or rewrites an offending wave/query and continues."""
    if len(output.waves) > MAX_WAVES:
        raise DiscoveryValidationError(f"discovery returned {len(output.waves)} waves, exceeding the cap of {MAX_WAVES}")

    seen_wave_ids: set[str] = set()
    for wave in output.waves:
        if wave.wave_id in seen_wave_ids:
            raise DiscoveryValidationError(f"duplicate wave_id: {wave.wave_id!r}")
        seen_wave_ids.add(wave.wave_id)

        if not wave.signal_ids:
            raise DiscoveryValidationError(f"wave {wave.wave_id!r} cites zero signal_ids")

        invented = [sid for sid in wave.signal_ids if sid not in known_signal_ids]
        if invented:
            raise DiscoveryValidationError(
                f"wave {wave.wave_id!r} cites signal_id(s) not present in the supplied evidence: {invented}"
            )

        if len(wave.expansion_queries) > MAX_EXPANSION_QUERIES_PER_WAVE:
            raise DiscoveryValidationError(
                f"wave {wave.wave_id!r} proposed {len(wave.expansion_queries)} expansion queries, "
                f"exceeding the cap of {MAX_EXPANSION_QUERIES_PER_WAVE} per wave"
            )
