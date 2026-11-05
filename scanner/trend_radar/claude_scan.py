"""Orchestrates the full two-pass Claude-powered scan: build evidence
packet -> Pass A discovery -> validate -> controlled expansion ->
Pass B editorial judgement -> validate -> assemble final result.

No network/API call happens anywhere at import time -- build_client()
and every collector/search function are only ever invoked inside
run_claude_scan() itself.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .claude_client import UsageTotals, build_client, call_structured, resolve_model
from .claude_discovery import build_discovery_user_content, validate_discovery_output
from .claude_editorial import build_editorial_user_content, validate_final_output
from .claude_evidence import EvidenceRecord, build_evidence_packet
from .claude_expansion import run_expansion
from .claude_prompts import DISCOVERY_SYSTEM_PROMPT, EDITORIAL_SYSTEM_PROMPT
from .claude_schemas import ClaudeScanOutput, DiscoveryOutput

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_SIGNALS = 400
DISCOVERY_MAX_TOKENS = 20000
EDITORIAL_MAX_TOKENS = 20000


def build_packet_report(evidence: list[EvidenceRecord]) -> dict:
    """The --dry-run-packet result: what would be sent to Claude,
    without ever calling the API."""
    lanes: dict[str, int] = {}
    platforms: dict[str, int] = {}
    for record in evidence:
        lanes[record.lane] = lanes.get(record.lane, 0) + 1
        platforms[record.platform] = platforms.get(record.platform, 0) + 1
    return {
        "dry_run": True,
        "evidence_count": len(evidence),
        "lanes": lanes,
        "platforms": platforms,
        "evidence": [e.to_prompt_dict() for e in evidence],
    }


def run_claude_scan(
    conn: sqlite3.Connection,
    *,
    model: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_signals: int = DEFAULT_MAX_SIGNALS,
    skip_expansion: bool = False,
    dry_run_packet: bool = False,
) -> dict:
    evidence = build_evidence_packet(conn, lookback_days=lookback_days, max_signals=max_signals)

    if dry_run_packet:
        return build_packet_report(evidence)

    if not evidence:
        raise ValueError(f"no raw signals found in the last {lookback_days} day(s) -- nothing to scan")

    resolved_model = resolve_model(model)
    client = build_client()
    usage = UsageTotals(model=resolved_model)

    evidence_by_id = {e.signal_id: e for e in evidence}
    known_signal_ids = set(evidence_by_id)

    discovery: DiscoveryOutput = call_structured(
        client,
        model=resolved_model,
        system=DISCOVERY_SYSTEM_PROMPT,
        user_content=build_discovery_user_content(evidence),
        output_format=DiscoveryOutput,
        max_tokens=DISCOVERY_MAX_TOKENS,
        usage=usage,
    )
    validate_discovery_output(discovery, known_signal_ids)

    expansion_results: dict[str, list[EvidenceRecord]] = {}
    expansion_log: list[dict] = []
    if not skip_expansion:
        expansion_results, expansion_log = run_expansion(conn, discovery.waves)
        for records in expansion_results.values():
            for record in records:
                evidence_by_id.setdefault(record.signal_id, record)
        known_signal_ids = set(evidence_by_id)

    editorial: ClaudeScanOutput = call_structured(
        client,
        model=resolved_model,
        system=EDITORIAL_SYSTEM_PROMPT,
        user_content=build_editorial_user_content(discovery, evidence_by_id, expansion_results),
        output_format=ClaudeScanOutput,
        max_tokens=EDITORIAL_MAX_TOKENS,
        usage=usage,
    )
    validate_final_output(editorial, known_signal_ids, evidence_by_id)

    result = editorial.model_dump()
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    result["model"] = resolved_model
    result["usage"] = usage.as_dict()
    result["_expansion_queries_executed"] = expansion_log
    result["_evidence_packet_size"] = len(evidence)
    result["_discovery_waves"] = [
        {
            "wave_id": w.wave_id,
            "working_label": w.working_label,
            "signal_ids": w.signal_ids,
            "cross_source_reasoning": w.cross_source_reasoning,
            "ambiguous_name_caution": w.ambiguous_name_caution,
        }
        for w in discovery.waves
    ]
    return result
