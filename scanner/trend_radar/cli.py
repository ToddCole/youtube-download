"""Command-line interface. Every command accepts --db to point at a test
or temporary database path -- production code never has to be run
against the default path to be exercised end-to-end.
"""

from __future__ import annotations

import argparse
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trend_radar", description="MFO Fitness Trend Radar (Phase 1: raw collection)")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
