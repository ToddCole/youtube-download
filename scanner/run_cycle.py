"""One scheduled cycle: learn -> collect -> (Claude slot) scan -> SEO -> digest -> health.

Run by launchd every 3h (see com.mfo.editorial-cycle.plist). Safe to run by
hand:  python3 run_cycle.py [--claude|--no-claude] [--no-alert]

Every run is recorded in scanner.db `pipeline_runs` with per-step status,
so a failure can never again disappear silently. Alerts go to ntfy when
NTFY_TOPIC is set in .env, and by email when MFO_EMAIL_TO + SMTP_PASSWORD are.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import mailer
import settings  # noqa: F401  (loads .env)
from cycle_lock import LockBusy, cycle_lock

SCANNER_DIR = Path(__file__).resolve().parent
SCANNER_DB = SCANNER_DIR / "scanner.db"
REPORTS = SCANNER_DIR / "reports"
CLAUDE_OUTPUT = REPORTS / "trend-claude-latest.json"
# Two Claude scans a day, so anything older than this means one was missed.
STALE_HOURS = 20
# Local hours (Mac clock) at which the paid Claude scan runs. Must line up
# with the launchd 3-hourly grid.
CLAUDE_HOURS = {7, 16}
DESK_URL = "http://127.0.0.1:8090/"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            steps_json TEXT NOT NULL DEFAULT '[]',
            problems_json TEXT NOT NULL DEFAULT '[]',
            claude_cost_usd REAL
        )"""
    )
    conn.commit()


def tail(text: str, lines: int = 6) -> str:
    kept = [line for line in (text or "").strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])[-800:]


def run_step(name: str, args: list[str], timeout: int) -> dict:
    started = time.monotonic()
    step = {"step": name, "started_at": utc_now()}
    try:
        result = subprocess.run(
            args, cwd=str(SCANNER_DIR), capture_output=True, text=True, timeout=timeout, check=False
        )
        step["returncode"] = result.returncode
        step["status"] = {0: "ok", 2: "partial"}.get(result.returncode, "failed")
        if result.returncode != 0 or result.stderr.strip():
            step["stderr_tail"] = tail(result.stderr)
        step["stdout_tail"] = tail(result.stdout, 4)
    except subprocess.TimeoutExpired:
        step["status"] = "failed"
        step["stderr_tail"] = f"timed out after {timeout}s"
    step["seconds"] = round(time.monotonic() - started, 1)
    return step


def report_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600


def health_problems(steps: list[dict]) -> list[str]:
    problems: list[str] = []
    for step in steps:
        if step["status"] != "ok":
            detail = (step.get("stderr_tail") or "").splitlines()[-1:] or [""]
            problems.append(f"{step['step']} {step['status']}: {detail[0][:200]}")
    age = report_age_hours(CLAUDE_OUTPUT)
    if age is None:
        problems.append("no Claude scan yet")
    elif age > STALE_HOURS and os.environ.get("MFO_CLAUDE_SCAN") == "1":
        problems.append(f"Claude scan stale ({age:.0f}h old)")
    try:
        with urlopen(DESK_URL, timeout=5) as response:
            if response.status != 200:
                problems.append(f"Desk app returned HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Desk app down: {exc.__class__.__name__}")
    return problems


def send_alert(title: str, body: str, priority: str = "default") -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    request = Request(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": "newspaper"},
        method="POST",
    )
    try:
        import ssl

        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(request, timeout=10, context=context):
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: ntfy alert failed: {exc.__class__.__name__}: {getattr(exc, 'reason', exc)}", file=sys.stderr)
        return False


def claude_enabled(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    if os.environ.get("MFO_CLAUDE_SCAN") != "1":
        return False
    return datetime.now().hour in CLAUDE_HOURS


def claude_cost(path: Path) -> float | None:
    try:
        usage = json.loads(path.read_text(encoding="utf-8")).get("usage") or {}
    except (OSError, ValueError):
        return None
    cost = usage.get("cost_usd")
    return float(cost) if cost is not None else None


def run_cycle(with_claude: bool | None, alert: bool) -> int:
    py = sys.executable
    conn = sqlite3.connect(SCANNER_DB)
    ensure_table(conn)
    started_at = utc_now()
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (started_at, status) VALUES (?, 'running')", (started_at,)
    ).lastrowid
    conn.commit()

    steps: list[dict] = []
    cost = None
    try:
        with cycle_lock("scheduled cycle"):
            claude_slot = claude_enabled(with_claude)
            # Learn first: pull 👍/👎 taps every cycle (ntfy keeps them ~12h);
            # before each Claude scan, re-read MFO's posts and its open stories,
            # so the collect below already searches for their follow-ups.
            learn_args = [py, "learning.py", "--poll"] + (["--refresh-index"] if claude_slot else [])
            steps.append(run_step("learn", learn_args, 600))
            steps.append(run_step("trend_collect", [py, "-m", "trend_radar", "collect"], 900))
            if claude_slot:
                started = time.time()
                step = run_step(
                    "claude_scan",
                    [py, "-m", "trend_radar", "claude-scan", "--output", str(CLAUDE_OUTPUT)],
                    1500,
                )
                if CLAUDE_OUTPUT.exists() and CLAUDE_OUTPUT.stat().st_mtime >= started:
                    cost = claude_cost(CLAUDE_OUTPUT)
                steps.append(step)
                if step["status"] == "ok" and os.environ.get("OPENSEO_API_KEY"):
                    steps.append(run_step("seo_score", [py, "seo_opportunity.py"], 600))
                if step["status"] == "ok" and alert and (os.environ.get("NTFY_TOPIC") or mailer.configured()):
                    steps.append(run_step("digest", [py, "digest.py"], 60))
    except LockBusy as exc:
        steps.append({"step": "lock", "status": "skipped", "stderr_tail": str(exc)})

    problems = health_problems(steps) if not (steps and steps[-1]["step"] == "lock") else []
    status = "ok" if not problems else ("failed" if any(s["status"] == "failed" for s in steps) else "warning")
    conn.execute(
        "UPDATE pipeline_runs SET finished_at=?, status=?, steps_json=?, problems_json=?, claude_cost_usd=? WHERE id=?",
        (utc_now(), status, json.dumps(steps), json.dumps(problems), cost, run_id),
    )
    conn.commit()
    conn.close()

    print(f"cycle {run_id}: {status}")
    for step in steps:
        print(f"  {step['step']}: {step['status']} ({step.get('seconds', 0)}s)")
    for problem in problems:
        print(f"  ! {problem}")
    if alert and problems:
        send_alert(
            f"MFO Desk: {status}",
            "\n".join(problems[:8]),
            priority="high" if status == "failed" else "default",
        )
        mailer.send(f"MFO Desk: {status}", "\n".join(problems[:8]))
    return 0 if status != "failed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--claude", dest="claude", action="store_true", default=None, help="force the Claude scan")
    group.add_argument("--no-claude", dest="claude", action="store_false", help="skip the Claude scan")
    parser.add_argument("--no-alert", action="store_true")
    parser.add_argument("--test-alert", action="store_true", help="send one test push and exit")
    args = parser.parse_args()
    if args.test_alert:
        ok = send_alert("MFO Desk test", "Alerts are working.")
        print("sent" if ok else "not sent (NTFY_TOPIC unset or request failed)")
        return 0 if ok else 1
    return run_cycle(args.claude, alert=not args.no_alert)


if __name__ == "__main__":
    raise SystemExit(main())
