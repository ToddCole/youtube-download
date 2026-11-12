"""Fit-food pass: the editor's FIT FOOD BRIEF (fit_food_brief.md), run on
every Claude scan, after the news picks.

Two Claude calls per mode, kept apart like the main scan (web search and
structured output never share a request):
  1. research  plain-text notes, with web search: find, open and check
  2. picks     those notes -> 0-3 structured FitFoodPicks

Modes:
  scanner   starts from the food-related signals the scanner collected,
            then searches beyond them
  web_only  the same brief from scratch, ignoring the scanner -- run
            alongside (fit_food.json "compare") so the two can be compared

Every URL in a pick must be one the research call actually saw (a search
result) or a collected signal; anything else is dropped and flagged in
the pick's risk, so a guessed link never reaches the editor.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from .claude_client import SpendLedger, UsageTotals, call_structured, call_web_research
from .claude_evidence import EvidenceRecord
from .claude_prompts import FIT_FOOD_PICKS_SYSTEM_PROMPT, FIT_FOOD_RESEARCH_SYSTEM_PROMPT
from .claude_schemas import FitFoodOutput

SCANNER_DIR = Path(__file__).resolve().parent.parent
BRIEF_PATH = SCANNER_DIR / "fit_food_brief.md"
CONFIG_PATH = SCANNER_DIR / "fit_food.json"
MFO_INDEX = SCANNER_DIR / "mfo_index.json"
MFO_PREFIX = "https://mensfitnessonline.com.au"
RESEARCH_MAX_TOKENS = 20000
PICKS_MAX_TOKENS = 16000
MAX_SIGNALS = 250
MAX_PAGES = 80
MAX_PICKS = 3
RECENT_DAYS = 14
MODES = ("scanner", "web_only")


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fit_food_source_test(config: dict | None = None):
    """source_id -> True for the sources that exist only for this pass."""
    config = load_config() if config is None else config
    dedicated = set(config.get("dedicated_sources") or [])
    return lambda source_id: source_id in dedicated or source_id.startswith("beat:fit_food:")


def news_source_filter(config: dict | None = None):
    """For the main news scan: everything except the fit-food-only sources."""
    is_food = fit_food_source_test(config)
    return lambda source_id: not is_food(source_id)


def fit_food_packet(conn, config: dict | None = None, lookback_days: int = 7) -> list[EvidenceRecord]:
    """Its own packet: every source (fit-food and news), deduplicated the same way."""
    from .claude_evidence import build_evidence_packet

    config = load_config() if config is None else config
    return build_evidence_packet(conn, lookback_days=lookback_days,
                                 max_signals=int(config.get("max_packet_signals") or 1500))


def _matcher(keywords: list[str]):
    """Whole words with plural/-ing forms ('egg' -> eggs, not eggplant);
    a trailing * makes a keyword a prefix ('contaminat*')."""
    if not keywords:
        return lambda text: False
    parts = [re.escape(k[:-1].lower()) + r"\w*" if k.endswith("*") else re.escape(k.lower()) + r"(?:s|es|ing)?\b"
             for k in keywords]
    pattern = re.compile(r"\b(?:" + "|".join(parts) + r")", re.I)
    return lambda text: bool(pattern.search(text or ""))


def _source_id(e: EvidenceRecord) -> str:
    return e.source_id or ""


def food_signals(evidence: list[EvidenceRecord], keywords: list[str], limit: int = MAX_SIGNALS,
                 is_food_source=None) -> list[EvidenceRecord]:
    """Titles only for news sources; the body counts too for food-only sources."""
    match = _matcher(keywords)
    is_food_source = is_food_source or (lambda _sid: False)

    def hit(e: EvidenceRecord) -> bool:
        text = f"{e.title} {e.body_excerpt[:300]}" if is_food_source(_source_id(e)) else e.title
        return match(text)

    hits = [e for e in evidence if hit(e)]
    hits.sort(key=lambda e: e.published_at or "", reverse=True)
    return hits[:limit]


def brief_page_urls(brief: str) -> set[str]:
    """MFO pages the brief names, written either in full or as /slug/."""
    urls = set()
    for slug in re.findall(r"(?:mensfitnessonline\.com\.au)?(/[a-z0-9-]+/)", brief):
        urls.add(f"{MFO_PREFIX}{slug}")
    return urls


def mfo_food_pages(keywords: list[str], index_path: Path = MFO_INDEX, limit: int = MAX_PAGES) -> list[dict]:
    try:
        pages = json.loads(index_path.read_text(encoding="utf-8")).get("pages") or []
    except (OSError, ValueError):
        return []
    match = _matcher(keywords)
    hits = [p for p in pages if isinstance(p, dict) and match(f"{p.get('slug', '')} {p.get('title', '')}")]
    hits.sort(key=lambda p: p.get("modified") or p.get("date") or "", reverse=True)
    return [{"title": p.get("title"), "url": p.get("url")} for p in hits[:limit]]


def recent_posts(index_path: Path = MFO_INDEX, days: int = RECENT_DAYS, now: datetime | None = None) -> list[dict]:
    """What MFO published lately, so the pass never re-picks a covered story."""
    try:
        pages = json.loads(index_path.read_text(encoding="utf-8")).get("pages") or []
    except (OSError, ValueError):
        return []
    cutoff = ((now or datetime.now()) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    posts = [p for p in pages if isinstance(p, dict) and (p.get("date") or "") >= cutoff]
    posts.sort(key=lambda p: p.get("date") or "", reverse=True)
    return [{"date": (p.get("date") or "")[:10], "title": p.get("title"), "url": p.get("url")} for p in posts]


def today_line(now: datetime | None = None) -> str:
    """The pass can't judge "the last 7 days" without knowing today."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=7)
    return (f"TODAY: {now:%A %d %B %Y} (Australia). The 7-day window starts {cutoff:%d %B %Y}: a story first "
            "published before then is out, unless a big outlet first picked it up inside the window.")


def research_content(brief: str, signals: list[EvidenceRecord] | None, pages: list[dict],
                     recent: list[dict] | None = None) -> str:
    parts = [today_line(), "FIT FOOD BRIEF (from the editor)\n\n" + brief.strip()]
    if recent:
        parts.append("MFO PUBLISHED IN THE LAST 14 DAYS (already covered: never pick these stories again; "
                     "a genuinely new development on one is an 'add to existing post' pick)\n\n"
                     + "\n".join(f"- {p['date']} {p['title']} | {p['url']}" for p in recent))
    if signals is not None:
        lines = [f"- {e.published_at[:10] if e.published_at else 'undated'} | {e.source} | {e.title} | {e.url or '(no url)'}"
                 for e in signals]
        parts.append("SIGNALS THE SCANNER COLLECTED (food-related, last 7 days; headlines only, unchecked)\n\n"
                     + ("\n".join(lines) if lines else "- (none this scan)"))
    parts.append("MFO PAGES (existing posts you may suggest updating or linking)\n\n"
                 + "\n".join(f"- {p['title']} | {p['url']}" for p in pages))
    return "\n\n".join(parts)


def _norm(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()


def repair_picks(output: FitFoodOutput, seen_urls: set[str], mfo_urls: set[str]) -> list[str]:
    """Drop any URL the research never saw; keep at most MAX_PICKS."""
    notes = []
    seen = {_norm(u) for u in seen_urls}
    allowed_mfo = {_norm(u) for u in mfo_urls}
    for pick in output.picks:
        for field in ("original_source_url", "other_outlet_url"):
            url = getattr(pick, field)
            if url and _norm(url) not in seen:
                notes.append(f"fit food {pick.headline[:50]!r}: dropped unseen {field} {url!r}")
                setattr(pick, field, None)
                if field == "original_source_url":
                    pick.original_source_opened = False
                    pick.risk = f"{pick.risk} Original source link not verified.".strip()
        if pick.update_url and _norm(pick.update_url) not in allowed_mfo:
            notes.append(f"fit food {pick.headline[:50]!r}: dropped unknown MFO update_url {pick.update_url!r}")
            pick.update_url = None
            if pick.format == "add to existing post":
                pick.format = "news post"
    if len(output.picks) > MAX_PICKS:
        notes.append(f"fit food: kept the best {MAX_PICKS} of {len(output.picks)} picks")
        output.picks = output.picks[:MAX_PICKS]
    return notes


def run_mode(client, *, model: str, mode: str, brief: str, signals: list[EvidenceRecord] | None,
             pages: list[dict], max_searches: int, ledger: SpendLedger | None,
             recent: list[dict] | None = None) -> dict:
    usage = UsageTotals(model=model)
    started = time.monotonic()
    research_input = research_content(brief, signals if mode == "scanner" else None, pages, recent)
    notes, sources = call_web_research(
        client, model=model, system=FIT_FOOD_RESEARCH_SYSTEM_PROMPT, user_content=research_input,
        max_tokens=RESEARCH_MAX_TOKENS, max_searches=max_searches, usage=usage, ledger=ledger,
    )
    picks_input = (
        today_line() + "\n\nFIT FOOD BRIEF (from the editor)\n\n" + brief.strip()
        + "\n\nMFO PAGES\n\n" + "\n".join(f"- {p['title']} | {p['url']}" for p in pages)
        + "\n\nMFO PUBLISHED IN THE LAST 14 DAYS (never pick these again)\n\n"
        + ("\n".join(f"- {p['date']} {p['title']} | {p['url']}" for p in recent or []) or "- (none)")
        + "\n\nRESEARCH NOTES\n\n" + notes
    )
    output: FitFoodOutput = call_structured(
        client, model=model, system=FIT_FOOD_PICKS_SYSTEM_PROMPT, user_content=picks_input,
        output_format=FitFoodOutput, max_tokens=PICKS_MAX_TOKENS, usage=usage, ledger=ledger,
    )
    seen_urls = {s["url"] for s in sources} | {e.url for e in (signals or []) if e.url}
    mfo_urls = {p["url"] for p in pages + (recent or []) if p.get("url")} | brief_page_urls(brief)
    repairs = repair_picks(output, seen_urls, mfo_urls)
    return {
        "mode": mode, **output.model_dump(), "repairs": repairs, "research_notes": notes,
        "sources_seen": len(sources), "signals_given": len(signals or []) if mode == "scanner" else 0,
        "usage": usage.as_dict(), "seconds": round(time.monotonic() - started, 1),
    }


def compare_enabled(config: dict, today=None) -> bool:
    """The web-only comparison runs while "compare" is on and, if set,
    until "compare_until" (YYYY-MM-DD, inclusive)."""
    if not config.get("compare", True):
        return False
    until = config.get("compare_until")
    if not until:
        return True
    from datetime import date
    today = today or date.today()
    try:
        return today <= date.fromisoformat(until)
    except ValueError:
        return True


def run_fit_food(client, *, model: str, evidence: list[EvidenceRecord], ledger: SpendLedger | None,
                 config: dict | None = None, brief_path: Path = BRIEF_PATH, index_path: Path = MFO_INDEX) -> dict:
    """Both modes in parallel. A failing mode is recorded, never raised."""
    config = load_config() if config is None else config
    brief = brief_path.read_text(encoding="utf-8")
    signals = food_signals(evidence, config.get("signal_keywords") or [], is_food_source=fit_food_source_test(config))
    pages = mfo_food_pages(config.get("mfo_page_keywords") or [], index_path)
    recent = recent_posts(index_path)
    max_searches = int(config.get("max_searches") or 15)
    modes = MODES if compare_enabled(config) else ("scanner",)

    def one(mode: str) -> dict:
        try:
            return run_mode(client, model=model, mode=mode, brief=brief, signals=signals, pages=pages,
                            max_searches=max_searches, ledger=ledger, recent=recent)
        except Exception as exc:  # noqa: BLE001 -- fit food is an extra; the scan is kept either way
            return {"mode": mode, "error": f"{exc.__class__.__name__}: {exc}", "picks": []}

    with ThreadPoolExecutor(max_workers=len(modes)) as pool:
        results = dict(zip(modes, pool.map(one, modes)))
    return {"brief_chars": len(brief), **results}


# --- Comparing the two modes -------------------------------------------

_STOP = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "is", "how", "what", "why", "does", "with",
         "new", "your", "you", "it", "its", "at", "by", "from", "about", "after", "than", "more", "less"}


def _words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 2}


def same_story(a: dict, b: dict) -> bool:
    src_a, src_b = _norm(a.get("original_source_url")), _norm(b.get("original_source_url"))
    if src_a and src_a == src_b:
        return True
    wa, wb = _words(a.get("headline")), _words(b.get("headline"))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.3


RANK_BASE = {"scanner": 200, "web_only": 300}  # vote/SEO ranks (digest + seo_opportunity)


def shown_picks(fit_food: dict) -> list[tuple[str, int, dict]]:
    """(label, rank, pick) for what the digest and board show: the
    scanner-led picks, then any story only the web-only search found."""
    led = (fit_food.get("scanner") or {}).get("picks") or []
    out = [(f"Fit food {n}/{len(led)}", RANK_BASE["scanner"] + n, p) for n, p in enumerate(led, 1)]
    web = (fit_food.get("web_only") or {}).get("picks") or []
    result = compare(fit_food)
    extras = result["web_only"] if result else ([] if led else web)
    for pick in extras:
        out.append(("Fit food (web only)", RANK_BASE["web_only"] + web.index(pick) + 1, pick))
    return out


def compare(fit_food: dict) -> dict | None:
    """Which picks each mode found: both, scanner only, web-only only."""
    scanner, web = fit_food.get("scanner"), fit_food.get("web_only")
    if not scanner or not web:
        return None
    s_picks, w_picks = scanner.get("picks") or [], web.get("picks") or []
    both, web_matched = [], set()
    scanner_only = []
    for sp in s_picks:
        match = next((i for i, wp in enumerate(w_picks) if i not in web_matched and same_story(sp, wp)), None)
        if match is None:
            scanner_only.append(sp)
        else:
            web_matched.add(match)
            both.append((sp, w_picks[match]))
    web_only = [wp for i, wp in enumerate(w_picks) if i not in web_matched]

    def cost(m):
        return (m.get("usage") or {}).get("cost_usd")

    return {"both": both, "scanner_only": scanner_only, "web_only": web_only,
            "scanner_cost": cost(scanner), "web_cost": cost(web),
            "scanner_error": scanner.get("error"), "web_error": web.get("error"),
            "scanner_summary": scanner.get("summary"), "web_summary": web.get("summary")}
