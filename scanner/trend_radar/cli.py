"""Command-line interface. Every command accepts --db to point at a test
or temporary database path -- production code never has to be run
against the default path to be exercised end-to-end.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db as db_module
from .manual import add_manual_signal
from .runner import run_collection

VALID_LANES = ["publisher", "audience", "creator", "research"]


def _print_summary(summary) -> None:
    print(f"Run {summary.run_id}")
    print(f"  lanes: {', '.join(summary.lanes)}")
    print(f"  started: {summary.started_at}  finished: {summary.finished_at}")
    print(
        f"  sources attempted={summary.sources_attempted} "
        f"succeeded={summary.sources_succeeded} failed={summary.sources_failed} "
        f"skipped={summary.sources_skipped}"
    )
    print(
        f"  signals: new={summary.new_signals} duplicates={summary.duplicate_signals} "
        f"observations={summary.observations}"
    )
    if summary.sources_failed:
        print("  failures:")
        for outcome in summary.outcomes:
            if not outcome.succeeded and not outcome.skipped:
                print(f"    - {outcome.source_id}: {outcome.error_type}: {outcome.error}")
    if summary.sources_skipped:
        print("  skipped (no attempt made):")
        for outcome in summary.outcomes:
            if outcome.skipped:
                print(f"    - {outcome.source_id}: {outcome.skip_reason}")


def cmd_collect(args: argparse.Namespace) -> int:
    with db_module.connect(args.db) as conn:
        lanes = [args.lane] if args.lane else None
        summary = run_collection(conn, lanes=lanes)
        _print_summary(summary)
    return 0


def cmd_add_manual(args: argparse.Namespace) -> int:
    with db_module.connect(args.db) as conn:
        signal_id = add_manual_signal(
            conn,
            url=args.url,
            title_or_description=args.title,
            observation=args.observation,
            observed_platform=args.platform,
            observed_at=args.observed_at,
        )
        print(f"Recorded manual signal {signal_id}")
    return 0


def cmd_claude_scan(args: argparse.Namespace) -> int:
    # Imported lazily (not at module top-level) so `collect`/`add-manual`
    # never require the anthropic package to be installed, and so
    # nothing Claude-related is touched until this specific command
    # actually runs.
    from .claude_client import ClaudeOutputError, MissingApiKeyError, SpendCapReached, SpendLedger
    from .claude_discovery import DiscoveryValidationError
    from .claude_editorial import EditorialValidationError
    from .claude_scan import run_claude_scan
    from .scan_store import save_scan

    ledger = None if (args.no_spend_cap or args.dry_run_packet) else SpendLedger()
    with db_module.connect(args.db) as conn:
        editor_feedback = None
        if not (args.no_feedback or args.dry_run_packet):
            try:
                from learning import build_feedback

                editor_feedback = build_feedback(conn)
            except Exception as exc:  # noqa: BLE001 — learning is a bonus, never a blocker
                print(f"warning: no editor feedback this scan: {exc}", file=sys.stderr)
        try:
            result = run_claude_scan(
                conn,
                model=args.model,
                lookback_days=args.lookback_days,
                max_signals=args.max_signals,
                skip_expansion=args.skip_expansion,
                dry_run_packet=args.dry_run_packet,
                ledger=ledger,
                editor_feedback=editor_feedback,
            )
        except MissingApiKeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except SpendCapReached as exc:
            print(f"ERROR: spend cap: {exc}", file=sys.stderr)
            return 3
        except (ClaudeOutputError, DiscoveryValidationError, EditorialValidationError) as exc:
            print(f"ERROR: Claude scan failed: {exc}", file=sys.stderr)
            return 1
        if not args.dry_run_packet:
            scan_id = save_scan(conn, result)
            cost = result.get("usage", {}).get("cost_usd")
            print(f"Saved Claude scan {scan_id} ({len(result.get('candidates', []))} candidates, US${cost})")
            if result.get("_validation_repairs"):
                print(f"WARNING: {len(result['_validation_repairs'])} citation repair(s) applied", file=sys.stderr)

    output_path = Path(args.output) if args.output else None
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if output_path:
        output_path.write_text(text, encoding="utf-8")
        print(f"Wrote {output_path}")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trend_radar", description="MFO Fitness Trend Radar (raw collection + Claude-powered editorial scan)")
    parser.add_argument("--db", type=Path, default=None, help="Path to the Trend Radar SQLite database (default: scanner/trend_radar.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect", help="Collect raw signals from active sources")
    collect_p.add_argument("--lane", choices=VALID_LANES, default=None, help="Collect only this lane (default: all lanes)")
    collect_p.set_defaults(func=cmd_collect)

    manual_p = sub.add_parser("add-manual", help="Record a manually-submitted signal")
    manual_p.add_argument("--url", default=None, help="URL, if any")
    manual_p.add_argument("--title", required=True, help="Title or short description")
    manual_p.add_argument("--observation", required=True, help="Editor's own observation/note")
    manual_p.add_argument("--platform", default=None, help="Observed platform, e.g. instagram, tiktok, facebook, google")
    manual_p.add_argument("--observed-at", default=None, help="ISO timestamp, if known")
    manual_p.set_defaults(func=cmd_add_manual)

    claude_p = sub.add_parser("claude-scan", help="Run the Claude-powered two-pass editorial radar scan")
    claude_p.add_argument("--output", default=None, help="Path to write the resulting JSON (default: stdout)")
    claude_p.add_argument("--model", default=None, help="Override the model (default: MFO_TREND_MODEL env var, then claude-sonnet-5-5)")
    claude_p.add_argument("--lookback-days", type=int, default=7, help="How many days of raw signals to include (default: 7)")
    claude_p.add_argument("--max-signals", type=int, default=400, help="Cap on evidence packet size (default: 400)")
    claude_p.add_argument("--dry-run-packet", action="store_true", help="Build and report the evidence packet without calling Claude")
    claude_p.add_argument("--skip-expansion", action="store_true", help="Skip the controlled expansion step")
    claude_p.add_argument("--no-spend-cap", action="store_true", help="Do not check/record the daily Claude spend ledger")
    claude_p.add_argument("--no-feedback", action="store_true", help="Do not show Claude the editor feedback (published posts, 👍/👎)")
    claude_p.set_defaults(func=cmd_claude_scan)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
