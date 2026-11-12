"""Orchestrates the full two-pass Claude-powered scan: build evidence
packet -> Pass A discovery -> validate -> controlled expansion ->
web research (web search, plain-text notes) -> Pass B editorial
judgement -> validate -> assemble final result.

No network/API call happens anywhere at import time -- build_client()
and every collector/search function are only ever invoked inside
run_claude_scan() itself.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from .claude_client import (
    SpendLedger,
    UsageTotals,
    build_client,
    call_structured,
    call_web_research,
    resolve_model,
)
from .claude_discovery import build_discovery_user_content, repair_discovery_output, validate_discovery_output
from .claude_editorial import (
    MAX_CANDIDATES,
    MIN_CANDIDATES,
    EditorialValidationError,
    build_editorial_user_content,
    build_research_user_content,
    repair_final_output,
    validate_final_output,
)
from .claude_evidence import EvidenceRecord, build_evidence_packet
from .claude_expansion import run_expansion
from .claude_prompts import DISCOVERY_SYSTEM_PROMPT, EDITORIAL_SYSTEM_PROMPT, RESEARCH_SYSTEM_PROMPT
from .claude_schemas import ClaudeScanOutput, DiscoveryOutput

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_SIGNALS = 400
# Streaming, so these can be generous; thinking shares the budget.
DISCOVERY_MAX_TOKENS = 32000
EDITORIAL_MAX_TOKENS = 32000
RESEARCH_MAX_TOKENS = 16000
# Web research before the editorial pass: on unless MFO_WEB_RESEARCH=0.
DEFAULT_MAX_SEARCHES = 8


def web_research_settings() -> tuple[bool, int]:
    enabled = os.environ.get("MFO_WEB_RESEARCH", "1") != "0"
    try:
        searches = int(os.environ.get("MFO_WEB_SEARCH_MAX", DEFAULT_MAX_SEARCHES))
    except ValueError:
        searches = DEFAULT_MAX_SEARCHES
    return enabled and searches > 0, max(0, searches)


def fit_food_enabled() -> bool:
    from .claude_fit_food import BRIEF_PATH

    return os.environ.get("MFO_FIT_FOOD", "1") != "0" and BRIEF_PATH.exists()


def fit_food_ran_today(conn, now: datetime | None = None) -> bool:
    """Fit food runs once per local day (the first scan of the day); later
    scans that day skip it. A run whose scanner mode errored doesn't count."""
    import json as _json

    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    since = (now - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        rows = conn.execute("SELECT generated_at, result_json FROM claude_scans WHERE generated_at >= ?", (since,)).fetchall()
    except Exception:
        return False
    for generated_at, raw in rows:
        try:
            when = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone().date()
            scanner = ((_json.loads(raw).get("fit_food") or {}).get("scanner")) or {}
        except (ValueError, AttributeError, TypeError):
            continue
        if when == today and scanner and not scanner.get("error"):
            return True
    return False


def add_fit_food_cost(result: dict) -> None:
    """Fit-food spend counts in the scan's total, and is shown separately."""
    usage = result.setdefault("usage", {})
    extra = sum(((m or {}).get("usage") or {}).get("cost_usd") or 0
                for m in (result.get("fit_food") or {}).values() if isinstance(m, dict))
    base = usage.get("cost_usd") or 0
    usage["cost_usd"] = round(base - (usage.get("fit_food_cost_usd") or 0) + extra, 6)
    usage["fit_food_cost_usd"] = round(extra, 6)


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


_REF = re.compile(r"([SX])0*(\d+)")


def _translate(ref_or_id: str, back: dict[str, str]) -> str:
    """Short ref -> signal UUID. The model sometimes drops the zero padding
    ("S75" for "S075"); that can only mean one record, so it's accepted
    rather than discarded (it once cost two whole waves in one scan)."""
    ref = ref_or_id.strip()
    if ref in back:
        return back[ref]
    m = _REF.fullmatch(ref)
    if m:
        padded = f"{m.group(1)}{int(m.group(2)):03d}"
        if padded in back:
            return back[padded]
    return ref_or_id


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
    from .claude_fit_food import news_source_filter

    # The fit-food-only sources feed the fit-food pass, never the news scan's packet.
    evidence = build_evidence_packet(conn, lookback_days=lookback_days, max_signals=max_signals + 1,
                                     include_source=news_source_filter())
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

    research: dict = {"enabled": False}
    research_enabled, max_searches = web_research_settings()
    if research_enabled and discovery.waves:
        research = {"enabled": True, "max_searches": max_searches}
        searches_before = usage.web_search_requests
        try:
            notes, sources = call_web_research(
                client,
                model=resolved_model,
                system=RESEARCH_SYSTEM_PROMPT,
                user_content=build_research_user_content(discovery, evidence_by_id, expansion_results),
                max_tokens=RESEARCH_MAX_TOKENS,
                max_searches=max_searches,
                usage=usage,
                ledger=ledger,
            )
            research.update({"notes": notes, "sources": sources})
        except Exception as exc:  # noqa: BLE001 -- research is an aid; the scan goes on without it
            research["error"] = f"{exc.__class__.__name__}: {exc}"
            repairs.append(f"web research skipped: {research['error']}")
            print(f"WARNING: web research failed: {research['error']}", file=sys.stderr)
        research["searches"] = usage.web_search_requests - searches_before
    notes_suffix = f"\n\nWEB RESEARCH NOTES\n\n{research['notes']}" if research.get("notes") else ""

    user_content = build_editorial_user_content(discovery, evidence_by_id, expansion_results, refs) + notes_suffix + feedback_suffix
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
    if fit_food_enabled() and not fit_food_ran_today(conn):
        from .claude_fit_food import fit_food_packet, run_fit_food

        result["fit_food"] = run_fit_food(client, model=resolved_model,
                                          evidence=fit_food_packet(conn, lookback_days=lookback_days), ledger=ledger)
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    result["model"] = resolved_model
    result["usage"] = usage.as_dict()
    add_fit_food_cost(result)
    result["_validation_repairs"] = repairs
    result["_expansion_queries_executed"] = expansion_log
    result["_evidence_packet_size"] = len(evidence)
    result["_evidence_packet_truncated"] = truncated
    result["_editor_feedback_used"] = bool(editor_feedback)
    result["_web_research"] = research
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
