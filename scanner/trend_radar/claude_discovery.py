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


def build_discovery_user_content(evidence: list[EvidenceRecord]) -> str:
    payload = {"evidence": [e.to_prompt_dict() for e in evidence]}
    return (
        f"Here are {len(evidence)} recent signals, each already deduplicated by exact URL. "
        "Identify emerging story waves per the instructions.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


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
