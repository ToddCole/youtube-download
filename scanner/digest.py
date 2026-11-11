"""Phone digest after each Claude scan: one notification per pick.

Each pick says why now, the MFO angle and its SEO verdict; tapping it
opens the story's source (or the MFO page, for an update), with Source,
👍 and 👎 buttons. Sent by run_cycle.py once per new scan (07:05 =
morning, 16:05 = afternoon). The first pick buzzes (priority 4 when it's
a strong Write now); the rest arrive quietly. On a quiet news day,
🌲 evergreen ideas (evergreen.py) top the digest up to four.

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
# A Write now with this many AU searches a month earns a high-priority push.
STRONG_WRITE_NOW_VOLUME = 500
VERDICT_MARK = {"write_now": "🟢", "update_existing": "🔵", "watch": "🟠", "us_only": "🟡", "skip": "⚪"}


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def _volume_line(seo: dict | None) -> str:
    if not seo:
        return "not SEO-scored"
    demand = seo.get("demand") or {}
    kw = demand.get("best_keyword")
    vol = demand.get("au_volume") or 0
    us = demand.get("us_volume") or 0
    kd = demand.get("best_kd")
    if seo.get("verdict") == "update_existing" and seo.get("gsc"):
        g = seo["gsc"][0]
        return f"ranking for '{g.get('query')}'"
    if kw and not vol and us:
        return f"US only {_k(us)}/mo"
    if not kw or not vol:
        return "no demand yet"
    kd_txt = f" KD{kd}" if kd is not None else ""
    return f"AU {_k(vol)}/mo{kd_txt}"


VERDICT_LABEL = {"write_now": "Write now", "update_existing": "Update existing page", "watch": "Watch",
                 "us_only": "US demand only", "skip": "Low SEO upside"}
ORDER = {"write_now": 0, "update_existing": 1, "watch": 2, "us_only": 3, "skip": 4}


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _sorted_picks(scan: dict, scores: dict[int, dict]) -> list[dict]:
    """Best SEO bets first (biggest Write now first), then Claude's rank."""
    def key(c):
        seo = scores.get(c.get("rank")) or {}
        volume = (seo.get("demand") or {}).get("au_volume") or 0
        return (ORDER.get(seo.get("verdict"), 5), -volume if seo.get("verdict") == "write_now" else 0, c.get("rank") or 99)

    return sorted(scan.get("candidates") or [], key=key)


def _source_url(candidate: dict) -> str | None:
    for item in candidate.get("evidence") or []:
        if item.get("url"):
            return item["url"]
    return None


def _is_strong(seo: dict | None) -> bool:
    return bool(seo) and seo.get("verdict") == "write_now" and \
        ((seo.get("demand") or {}).get("au_volume") or 0) >= STRONG_WRITE_NOW_VOLUME


def pick_message(candidate: dict, seo: dict | None, n: int, total: int, slot: str, scan_id) -> dict:
    """One notification per pick: tap opens the story (or the MFO page to
    update); buttons open the source and vote 👍 / 👎."""
    update_url = candidate.get("update_url") if candidate.get("story_type") == "update" else None
    source = _source_url(candidate)
    verdict = (seo or {}).get("verdict")
    lines = []
    if update_url:
        lines.append(f"🔵 Update MFO page: {update_url.replace('https://mensfitnessonline.com.au', '')}")
    if seo:
        lines.append(f"{VERDICT_MARK.get(verdict, '•')} {VERDICT_LABEL.get(verdict, seo.get('label') or '')} · {_volume_line(seo)}")
    lines.append(f"Now: {_clip(candidate.get('why_moving_now'), 260)}")
    lines.append(f"Angle: {_clip(candidate.get('mfo_angle'), 260)}")
    checks = (candidate.get("verification_needed") or []) + (candidate.get("editorial_cautions") or [])
    if checks:
        lines.append(f"Check: {_clip(checks[0], 160)}")
    actions = []
    if source:
        actions.append({"action": "view", "label": "Source", "url": source, "clear": False})
    actions += vote_actions(scan_id, candidate.get("rank"), _clip(candidate.get("headline"), 60))
    return {
        "title": f"{slot} {n}/{total}: {_clip(candidate.get('headline'), 90)}",
        "message": "\n".join(lines),
        "priority": 4 if (n == 1 and _is_strong(seo)) else (3 if n == 1 else 2),
        "tags": ["newspaper"],
        "click": update_url or source,
        "actions": actions,
    }


def evergreen_message(idea: dict, scan_id) -> dict:
    """A quiet 🌲 notification: what to refresh or write, and why it pays."""
    lines = []
    if idea.get("kind") == "refresh":
        title = f"🌲 Evergreen: refresh {_clip(idea.get('title'), 70)}"
        queries = idea.get("queries") or []
        if queries:
            q = queries[0]
            lines.append(f"Google has it at #{q['position']:g} for '{q['query']}' ({_k(q['impressions'])} impressions, 28 days).")
        if len(queries) > 1:
            lines.append("Also: " + ", ".join(f"'{q['query']}' #{q['position']:g}" for q in queries[1:]))
        lines.append("Push it onto page 1: bring it up to date and answer what those searchers want.")
        if idea.get("rivals"):
            rivals = idea["rivals"]
            lines.append(f"Split ranking: {', '.join(rivals)} also rank{'s' if len(rivals) == 1 else ''} for it. Link or merge them.")
    else:
        title = f"🌲 Evergreen: {_clip(idea.get('title'), 80)}"
        if idea.get("url"):
            lines.append(f"🔵 Expand MFO page: {idea['url'].replace('https://mensfitnessonline.com.au', '')}")
        lines.append(f"Angle: {_clip(idea.get('angle'), 260)}")
        if idea.get("why_it_lasts"):
            lines.append(f"Lasts: {_clip(idea['why_it_lasts'], 160)}")
    return {
        "title": title, "message": "\n".join(lines), "priority": 2, "tags": ["evergreen_tree"],
        "click": idea.get("url"), "actions": vote_actions(scan_id, idea.get("rank"), _clip(idea.get("title"), 60)),
    }


def build_messages(scan: dict, scores: dict[int, dict], emerging: dict | None, now: datetime | None = None,
                   evergreen: list[dict] | None = None) -> list[dict]:
    now = now or datetime.now()
    slot = "Morning" if now.hour < 12 else "Afternoon"
    picks = _sorted_picks(scan, scores)
    scan_id = scan.get("_scan_id")
    if picks:
        messages = [pick_message(c, scores.get(c.get("rank")), n, len(picks), slot, scan_id)
                    for n, c in enumerate(picks, 1)]
    else:
        messages = [{
            "title": f"MFO {slot}: nothing strong today",
            "message": _clip(scan.get("corpus_summary") or "No pick cleared the bar this scan.", 400),
            "priority": 2, "tags": ["zzz"], "actions": [],
        }]
    messages += [evergreen_message(idea, scan_id) for idea in evergreen or []]
    rows = (emerging or {}).get("rows") or []
    if rows:
        top = rows[0]
        messages.append({
            "title": f"📈 New in Google: '{top.get('query')}'",
            "message": f"{_k(top.get('impressions_7d') or 0)} impressions this week, almost none before. Feed it: update or expand the page.",
            "priority": 2, "tags": ["chart_with_upwards_trend"], "click": top.get("page"), "actions": [],
        })
    for m in messages:
        if not m.get("click"):
            m.pop("click", None)
    return messages


def vote_actions(scan_id, rank, name: str | None = None) -> list[dict]:
    """👍 / 👎 buttons. A tap posts a quiet "Saved" note into the main topic;
    the vote rides in its tag, which learning.py polls every cycle."""
    import os

    from learning import ntfy_server, vote_tag

    topic = os.environ.get("NTFY_TOPIC")
    if not topic or scan_id is None or rank is None:
        return []
    actions = []
    for vote, label, title in (("up", "👍", "more like this"), ("down", "👎", "less like this")):
        actions.append({
            "action": "http", "label": label, "url": f"{ntfy_server()}/{topic}", "method": "POST",
            "headers": {"Title": f"Saved: {title}", "Tags": f"white_check_mark,{vote_tag(vote, scan_id, rank)}",
                        "Priority": "1"},
            "body": f"{label} {name or ''}".strip(), "clear": True,
        })
    return actions


def publish(messages: list[dict]) -> int:
    """JSON publish, one request per notification (headers can't carry
    emoji). Returns how many were sent."""
    import os
    import ssl
    from urllib.request import Request, urlopen

    import certifi

    from learning import ntfy_server

    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return 0
    context = ssl.create_default_context(cafile=certifi.where())
    sent = 0
    for message in messages:
        payload = {"topic": topic, **message}
        request = Request(ntfy_server(), data=json.dumps(payload).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json", "User-Agent": "mfo-editorial-desk/1.0"})
        try:
            with urlopen(request, timeout=15, context=context):
                sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: digest push failed ({message.get('title')}): {exc}", file=sys.stderr)
    return sent


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
    import evergreen

    messages = build_messages(scan, load_scores(scan["_scan_id"], db_path), load_emerging(db_path),
                              evergreen=evergreen.load(scan["_scan_id"], db_path))
    if dry_run:
        return "\n\n".join(
            f"[p{m['priority']}] {m['title']}\n{m['message']}\n  tap -> {m.get('click', '-')}\n"
            f"  buttons: {', '.join(a['label'] for a in m.get('actions') or []) or '-'}"
            for m in messages
        )
    import os

    import mailer

    pushing, emailing = bool(os.environ.get("NTFY_TOPIC")), mailer.configured()
    if not pushing and not emailing:
        return "not sent (no NTFY_TOPIC or email configured)"
    sent = publish(messages) if pushing else 0
    emailed = mailer.send(*build_email(messages)) if emailing else False
    if sent or emailed:
        # Even a partial send counts: re-sending would duplicate what arrived.
        _mark_sent(scan.get("_scan_id"))
    push_ok = not pushing or sent == len(messages)
    email_ok = not emailing or emailed
    if push_ok and email_ok:
        return "sent"
    if not sent and not emailed:
        return "not sent"
    failed = []
    if not push_ok:
        failed.append(f"push {sent}/{len(messages)}")
    if not email_ok:
        failed.append("email failed")
    return f"partly sent ({', '.join(failed)})"


def build_email(messages: list[dict]) -> tuple[str, str]:
    """The whole digest as one email: subject from the first pick, then
    each notification with its links (ntfy vote buttons don't carry over)."""
    first = messages[0]["title"]
    subject = f"MFO Desk: {first}" + (f" (+{len(messages) - 1} more)" if len(messages) > 1 else "")
    blocks = []
    for m in messages:
        lines = [m["title"], m["message"]]
        links = [m["click"]] if m.get("click") else []
        links += [a["url"] for a in m.get("actions") or [] if a.get("action") == "view" and a["url"] not in links]
        lines += [f"→ {url}" for url in links]
        blocks.append("\n".join(lines))
    return subject, "\n\n" + ("\n\n" + "─" * 40 + "\n\n").join(blocks) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print, don't push")
    parser.add_argument("--force", action="store_true", help="send even if this scan was already sent")
    args = parser.parse_args()
    result = send_digest(force=args.force, dry_run=args.dry_run)
    print(result)
    return 0 if result in {"sent", "already sent"} or result.startswith("partly") or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
