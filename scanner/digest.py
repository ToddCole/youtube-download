"""Phone digest after each Claude scan: top picks with their SEO verdicts.

Sent by run_cycle.py once per new scan (07:05 = morning picks, 16:05 =
afternoon picks). Push priority goes up when a pick is a strong Write now
(real demand, low KD), so that one actually buzzes.

Manual:  python3 digest.py [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import settings  # noqa: F401  (loads .env)

SCANNER_DIR = Path(__file__).resolve().parent
TREND_DB = SCANNER_DIR / "trend_radar.db"
STATE_FILE = SCANNER_DIR / "digest_state.json"
TOP_N = 3
# A Write now this strong earns a high-priority push.
STRONG_WRITE_NOW_VOLUME = 1000
VERDICT_MARK = {"write_now": "🟢", "update_existing": "🔵", "watch": "🟠", "skip": "⚪"}


def _short(headline: str, limit: int = 48) -> str:
    """Lock-screen banners show ~4 lines: keep each pick to one."""
    text = (headline or "").split(":")[0].strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def _volume_line(seo: dict | None) -> str:
    if not seo:
        return "not SEO-scored"
    demand = seo.get("demand") or {}
    kw = demand.get("best_keyword")
    vol = demand.get("total_volume") or 0
    kd = demand.get("best_kd")
    if seo.get("verdict") == "update_existing" and seo.get("gsc"):
        g = seo["gsc"][0]
        return f"ranking for '{g.get('query')}'"
    if not kw or not vol:
        return "no demand yet"
    kd_txt = f" KD{kd}" if kd is not None else ""
    return f"{_k(vol)}/mo{kd_txt}"


def build_digest(scan: dict, scores: dict[int, dict], emerging: dict | None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    slot = "Morning" if now.hour < 12 else "Afternoon"
    picks = sorted(scan.get("candidates") or [], key=lambda c: c.get("rank") or 99)
    order = {"write_now": 0, "update_existing": 1, "watch": 2, "skip": 3}
    # Lead with the best SEO bets (biggest Write now first), then Claude's rank.
    def key(c):
        seo = scores.get(c.get("rank")) or {}
        volume = (seo.get("demand") or {}).get("total_volume") or 0
        return (order.get(seo.get("verdict"), 4), -volume if seo.get("verdict") == "write_now" else 0, c.get("rank") or 99)

    picks.sort(key=key)

    lines = []
    for c in picks[:TOP_N]:
        seo = scores.get(c.get("rank"))
        mark = VERDICT_MARK.get((seo or {}).get("verdict"), "•")
        lines.append(f"{mark} {_short(c.get('headline', ''))} · {_volume_line(seo)}")

    write_now = [r for r, s in scores.items() if s.get("verdict") == "write_now"]
    strong = [
        r for r in write_now
        if ((scores[r].get("demand") or {}).get("total_volume") or 0) >= STRONG_WRITE_NOW_VOLUME
    ]
    rows = (emerging or {}).get("rows") or []
    if rows:
        top = rows[0]
        lines.append(f"📈 '{top.get('query')}' {_k(top.get('impressions_7d') or 0)} impr, new")

    extra = len(picks) - TOP_N
    if extra > 0:
        lines.append(f"+{extra} more on the Desk")
    title = f"MFO {slot} picks: {len(write_now)} Write now" if scores else f"MFO {slot} picks"
    return {
        "scan_id": scan.get("_scan_id"),
        "title": title,
        "body": "\n".join(lines) or "No picks this scan.",
        "priority": "high" if strong else "default",
    }


def _already_sent(scan_id) -> bool:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_scan_id") == scan_id
    except (OSError, ValueError):
        return False


def _mark_sent(scan_id) -> None:
    STATE_FILE.write_text(json.dumps({"last_scan_id": scan_id, "sent_at": datetime.now().isoformat()}), encoding="utf-8")


def send_digest(force: bool = False, dry_run: bool = False, db_path: Path = TREND_DB) -> str:
    from editorial_today import latest_trend_scan
    from seo_opportunity import load_emerging, load_scores

    scan = latest_trend_scan(db_path)
    if not scan:
        return "no scan"
    if not force and not dry_run and _already_sent(scan.get("_scan_id")):
        return "already sent"
    digest = build_digest(scan, load_scores(scan["_scan_id"], db_path), load_emerging(db_path))
    if dry_run:
        return f"[{digest['priority']}] {digest['title']}\n{digest['body']}"
    from run_cycle import send_alert

    if send_alert(digest["title"], digest["body"], priority=digest["priority"]):
        _mark_sent(digest["scan_id"])
        return "sent"
    return "not sent"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print, don't push")
    parser.add_argument("--force", action="store_true", help="send even if this scan was already sent")
    args = parser.parse_args()
    result = send_digest(force=args.force, dry_run=args.dry_run)
    print(result)
    return 0 if result in {"sent", "already sent"} or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
