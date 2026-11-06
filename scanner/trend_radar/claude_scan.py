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

from .claude_client import SpendLedger, UsageTotals, build_client, call_structured, resolve_model
from .claude_discovery import build_discovery_user_content, repair_discovery_output, validate_discovery_output
from .claude_editorial import (
    MAX_CANDIDATES,
    MIN_CANDIDATES,
    EditorialValidationError,
    build_editorial_user_content,
    repair_final_output,
    validate_final_output,
)
from .claude_evidence import EvidenceRecord, build_evidence_packet
from .claude_expansion import run_expansion
from .claude_prompts import DISCOVERY_SYSTEM_PROMPT, EDITORIAL_SYSTEM_PROMPT
from .claude_schemas import ClaudeScanOutput, DiscoveryOutput

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_SIGNALS = 400
# Streaming, so these can be generous; thinking shares the budget.
DISCOVERY_MAX_TOKENS = 32000
EDITORIAL_MAX_TOKENS = 32000


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


def _assign_refs(records: list[EvidenceRecord], prefix: str, refs: dict[str, str], start: int = 1) -> None:
    for index, record in enumerate(records, start=start):
        refs.setdefault(record.signal_id, f"{prefix}{index:03d}")


def _translate(ref_or_id: str, back: dict[str, str]) -> str:
    return back.get(ref_or_id.strip(), ref_or_id)


def run_claude_scan(
    conn: sqlite3.Connection,
    *,
    model: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_signals: int = DEFAULT_MAX_SIGNALS,
    skip_expansion: bool = False,
    dry_run_packet: bool = False,
    ledger: SpendLedger | None = None,
    editor_feedback: str | None = None,
) -> dict:
    """editor_feedback: learning.build_feedback() text, appended to both
    passes so discovery and editorial lean toward what the editor uses."""
    feedback_suffix = f"\n\n{editor_feedback}" if editor_feedback else ""
    evidence = build_evidence_packet(conn, lookback_days=lookback_days, max_signals=max_signals + 1)
    truncated = len(evidence) > max_signals
    evidence = evidence[:max_signals]

    if dry_run_packet:
        report = build_packet_report(evidence)
        report["truncated_at_max_signals"] = truncated
        return report

    if not evidence:
        raise ValueError(f"no raw signals found in the last {lookback_days} day(s) -- nothing to scan")

    resolved_model = resolve_model(model)
    client = build_client()
    usage = UsageTotals(model=resolved_model)
    repairs: list[str] = []

    evidence_by_id = {e.signal_id: e for e in evidence}
    refs: dict[str, str] = {}
    _assign_refs(evidence, "S", refs)
    back = {ref: sid for sid, ref in refs.items()}

    discovery: DiscoveryOutput = call_structured(
        client,
        model=resolved_model,
        system=DISCOVERY_SYSTEM_PROMPT,
        user_content=build_discovery_user_content(evidence, refs) + feedback_suffix,
        output_format=DiscoveryOutput,
        max_tokens=DISCOVERY_MAX_TOKENS,
        usage=usage,
        ledger=ledger,
    )
    for wave in discovery.waves:
        wave.signal_ids = [_translate(sid, back) for sid in wave.signal_ids]
    repairs += repair_discovery_output(discovery, set(evidence_by_id))
    validate_discovery_output(discovery, set(evidence_by_id))

    expansion_results: dict[str, list[EvidenceRecord]] = {}
    expansion_log: list[dict] = []
    if not skip_expansion:
        expansion_results, expansion_log = run_expansion(conn, discovery.waves)
        expanded = [r for records in expansion_results.values() for r in records if r.signal_id not in evidence_by_id]
        _assign_refs(expanded, "X", refs)
        for record in expanded:
            evidence_by_id.setdefault(record.signal_id, record)
        back = {ref: sid for sid, ref in refs.items()}

    # Only what Pass B is actually shown counts as citable in Pass B.
    shown_ids = {sid for wave in discovery.waves for sid in wave.signal_ids}
    shown_ids |= {r.signal_id for records in expansion_results.values() for r in records}

    user_content = build_editorial_user_content(discovery, evidence_by_id, expansion_results, refs) + feedback_suffix
    editorial: ClaudeScanOutput | None = None
    last_error: Exception | None = None
    for attempt in (1, 2):
        content = user_content
        if last_error is not None:
            content += (
                "\n\nYour previous answer could not be used: "
                f"{last_error}. Return between {MIN_CANDIDATES} and {MAX_CANDIDATES} candidates, "
                "each citing only signal_ids given above."
            )
        candidate_output: ClaudeScanOutput = call_structured(
            client,
            model=resolved_model,
            system=EDITORIAL_SYSTEM_PROMPT,
            user_content=content,
            output_format=ClaudeScanOutput,
            max_tokens=EDITORIAL_MAX_TOKENS,
            usage=usage,
            ledger=ledger,
        )
        for candidate in candidate_output.candidates:
            for item in candidate.evidence:
                item.signal_id = _translate(item.signal_id, back)
        attempt_repairs = repair_final_output(candidate_output, shown_ids)
        try:
            validate_final_output(candidate_output, shown_ids, evidence_by_id)
        except EditorialValidationError as exc:
            repairs += attempt_repairs + [f"editorial attempt {attempt} rejected: {exc}"]
            last_error = exc
            continue
        repairs += attempt_repairs
        editorial = candidate_output
        break
    if editorial is None:
        raise EditorialValidationError(f"editorial pass failed twice; last error: {last_error}")

    result = editorial.model_dump()
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    result["model"] = resolved_model
    result["usage"] = usage.as_dict()
    result["_validation_repairs"] = repairs
    result["_expansion_queries_executed"] = expansion_log
    result["_evidence_packet_size"] = len(evidence)
    result["_evidence_packet_truncated"] = truncated
    result["_editor_feedback_used"] = bool(editor_feedback)
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
