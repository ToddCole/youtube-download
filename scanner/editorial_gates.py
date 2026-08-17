"""Deterministic editorial hard-gate logic for the MFO story scanner.

Operates on plain dicts and primitive values (never scanner.py's
dataclasses), so it can be imported by both scanner.py (at scan time) and
main.py (at packet-assembly time) without either module importing the
other. Stdlib-only.

A high score must never override a failed hard gate: callers are expected
to run these checks as the *last* step after their own scoring function
computes a number, and to clamp/exclude based on the result rather than
letting a high score re-elevate a gated-out lead.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

CONFIG_PATH = Path(__file__).resolve().parent / "editorial_gates_config.json"
RESEARCH_DATE_CACHE_PATH = Path(__file__).resolve().parent / "research_date_cache.json"
USER_AGENT = "MFO Editorial Gates/1.0 (+https://mensfitnessonline.com.au)"

DEFAULT_CONFIG: dict[str, Any] = {
    "freshness_windows": {
        "competition_result": {"hours": 72, "hard_gate": True},
        "official_announcement": {"hours": 168, "hard_gate": True},
        "practical_research": {"hours": 336, "hard_gate": False},
        "creator_video": {"hours": 168, "hard_gate": True},
        "evergreen": {"hours": None, "hard_gate": False, "requires_reason": True},
    },
    "saturation": {"cooldown_days_source": 14, "cooldown_days_format": 30},
    "research_resurfacing": {"stale_gap_days": 14},
    "breakout_confidence": {
        "comparison_window": 20,
        "min_observations": {"high": 12, "medium": 5, "low": 1},
        "age_match_tolerance_hours": 12,
        "score_cap_when_pending_or_low": 55,
    },
    "creator_aliases": {},
    "creator_lexicon": {"excluded_handles": [], "extra_variants": {}},
    "entities": {},
    "overlap_stopwords_extra": [],
    "cannibalisation_thresholds": {"high_score": 0.4, "medium_score": 0.35},
    "kill_reasons": {},
    "slate_constraints": {"max_creator": 2, "max_research": 2, "max_same_broad_topic": 2, "max_slate_size": 6},
    "creator_story_checklist": {"criteria": [], "min_pass": 2, "score_cap_when_ineligible": 50},
    "topic_taxonomy": {},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or CONFIG_PATH
    if not target.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        file_config = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(file_config, dict):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return _deep_merge(DEFAULT_CONFIG, file_config)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Real-world sources feed this bare timestamps with no offset (the
        # WordPress REST API's `date` field is documented as "site
        # timezone" but carries no offset). Treat naive timestamps as UTC
        # rather than leaving them naive, since every arithmetic use of
        # this function (saturation cooldowns, freshness windows) compares
        # against utc_now() and a naive/aware subtraction raises TypeError.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def recent_years(n: int = 6) -> set[str]:
    """Years to strip from overlap comparisons, computed from the current
    date rather than hardcoded, so this doesn't silently go stale."""
    current = utc_now().year
    return {str(year) for year in range(current - n + 1, current + 1)}


# ---------------------------------------------------------------------------
# Kill reasons
# ---------------------------------------------------------------------------

def kill_reason_description(code: str, config: dict[str, Any]) -> str:
    return config.get("kill_reasons", {}).get(code, code)


# ---------------------------------------------------------------------------
# Creator/source normalisation
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def normalize_creator_key(name: str | None, config: dict[str, Any]) -> str | None:
    if not name:
        return None
    lowered = name.strip().lower()
    aliases = config.get("creator_aliases", {}) or {}
    if lowered in aliases:
        return aliases[lowered]
    # Word-boundary match only: a naive substring check lets short aliases
    # like "rp" match inside unrelated words (e.g. "...powerp[ro]ject...").
    for alias, canonical in aliases.items():
        if alias and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered):
            return canonical
    return slugify(name)


_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]+)")


def channel_handle_from_url(url: str) -> str:
    match = _HANDLE_RE.search(url or "")
    return match.group(1) if match else (url or "").rstrip("/").rsplit("/", 1)[-1]


def derive_display_name_variants(handle: str) -> list[str]:
    """Readable-name guesses from a YouTube handle, e.g. "WillTennyson" ->
    "will tennyson". CamelCase handles split cleanly; all-lowercase
    handles (e.g. "athleanx") don't split automatically and typically
    need a manual variant seeded via config's creator_lexicon.extra_variants."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", handle)
    variants = {handle.lower(), spaced.lower()}
    return sorted(variant for variant in variants if len(variant) > 2)


def build_creator_lexicon(channel_urls: list[str], config: dict[str, Any]) -> dict[str, list[str]]:
    """Maps normalized creator_key -> distinctive name variants to search
    for in archived MFO article text, used to attribute a published page
    to the creator it was primarily sourced from. Excludes handles listed
    in config's creator_lexicon.excluded_handles (organisation/competition
    channels like CrossFit, whose name is also a common topic word and
    would produce false "creator-sourced" attributions on any article
    that simply discusses the sport)."""
    lexicon_config = config.get("creator_lexicon", {}) or {}
    excluded = {handle.lower() for handle in lexicon_config.get("excluded_handles", [])}
    extra_variants = lexicon_config.get("extra_variants", {}) or {}
    lexicon: dict[str, list[str]] = {}
    for url in channel_urls:
        handle = channel_handle_from_url(url)
        if handle.lower() in excluded:
            continue
        variants = set(derive_display_name_variants(handle))
        variants.update(extra_variants.get(handle.lower(), []))
        variants.update(extra_variants.get(handle, []))
        # Normalize against the *spaced* variant, not the raw handle: a
        # configured alias like "renaissance periodization" is written
        # with spaces and won't word-boundary-match a no-space CamelCase
        # handle, which would otherwise split the same creator across two
        # different keys depending on whether the source was a channel
        # URL or free text mentioning them by name.
        spaced_variant = max(variants, key=len) if variants else handle
        creator_key = normalize_creator_key(spaced_variant, config)
        lexicon.setdefault(creator_key, set()).update(variants)
    return {key: sorted(variants, key=len, reverse=True) for key, variants in lexicon.items()}


def match_creator_in_text(text: str, lexicon: dict[str, list[str]]) -> str | None:
    """Longest-variant-first match so a specific name (e.g. "jeff
    nippard") wins over an accidental short substring collision."""
    if not text or not lexicon:
        return None
    lowered = text.lower()
    best_key: str | None = None
    best_length = 0
    for creator_key, variants in lexicon.items():
        for variant in variants:
            if variant and variant in lowered and len(variant) > best_length:
                best_key = creator_key
                best_length = len(variant)
                break
    return best_key


# ---------------------------------------------------------------------------
# Entity / competition-result classification
# ---------------------------------------------------------------------------

_EVENT_DATE_RE = re.compile(
    r"\b(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b",
    re.I,
)


def _find_entity(text: str, config: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    lowered = text.lower()
    for entity_key, entity_conf in (config.get("entities", {}) or {}).items():
        for alias in entity_conf.get("aliases", []) or []:
            if alias and alias.lower() in lowered:
                return entity_key, entity_conf
    return None, None


def classify_development_type(
    primary_text: str,
    config: dict[str, Any],
    *,
    evidence_links: list[str] | None = None,
    fallback_type: str = "other",
) -> dict[str, Any]:
    """Entity-aware competition-result / development-type classifier.

    Requires the entity to appear in the *primary* item's own text (not a
    whole-cluster concatenation, which is how an unrelated pickup can
    pollute classification for the rest of a cluster), plus a result verb
    or structured evidence, before returning "competition_result".
    """
    text = primary_text or ""
    lowered = text.lower()
    evidence_links = evidence_links or []

    entity_key, entity_conf = _find_entity(text, config)
    result = {
        "development_type": fallback_type,
        "entity_matched": entity_key,
        "result_verb_matched": False,
        "structured_evidence": False,
        "event_name": None,
        "event_date": None,
        "official_url": None,
        "confidence": "low",
        "result_verb_without_entity": False,
    }

    if not entity_key:
        # The old keyword-only classifier would have called this a
        # competition result on words like "results"/"record" alone. Flag
        # that near-miss explicitly (kill_reason_codes:
        # competition_entity_mismatch) rather than letting it fall through
        # silently as "other" — the brief names this as a required, visible
        # rejection reason, not an implicit scoring side effect.
        all_result_verbs = {
            verb
            for entity_conf_ in (config.get("entities", {}) or {}).values()
            for verb in (entity_conf_.get("result_verbs", []) or [])
        }
        result["result_verb_without_entity"] = any(
            re.search(rf"\b{re.escape(verb)}\b", lowered) for verb in all_result_verbs
        )
        return result

    result_verbs = entity_conf.get("result_verbs", []) if entity_conf else []
    verb_matched = any(re.search(rf"\b{re.escape(verb)}\b", lowered) for verb in result_verbs)
    structured_evidence = bool(re.search(r"\b\d{1,3}[:.]\d{2}(?:[:.]\d{2})?\b", text)) or bool(
        re.search(r"\b\d{1,3}(?:st|nd|rd|th)\s+place\b", lowered)
    )
    date_match = _EVENT_DATE_RE.search(text)
    official_domains = entity_conf.get("official_domains", []) if entity_conf else []
    official_url = next(
        (link for link in evidence_links if any(domain in (link or "") for domain in official_domains)),
        None,
    )

    result["result_verb_matched"] = verb_matched
    result["structured_evidence"] = structured_evidence
    result["event_date"] = date_match.group(0) if date_match else None
    result["official_url"] = official_url
    result["event_name"] = entity_key.replace("_", " ").title() if (verb_matched or structured_evidence) else None

    if verb_matched or structured_evidence:
        result["development_type"] = "competition_result"
        if official_url and date_match:
            result["confidence"] = "high"
        elif official_url or date_match:
            result["confidence"] = "medium"
        else:
            result["confidence"] = "low"
    else:
        result["development_type"] = fallback_type

    return result


# ---------------------------------------------------------------------------
# Weighted topic overlap (archive matching)
# ---------------------------------------------------------------------------

def weighted_topic_overlap(
    candidate_terms: set[str],
    page_terms: set[str],
    config: dict[str, Any],
    *,
    entity_terms: set[str] | None = None,
) -> dict[str, Any]:
    """Bag-of-words overlap with named-entity terms weighted higher than
    generic vocabulary. `entity_terms` should be the subset of
    candidate_terms that are creator/athlete/event/exercise/condition
    names (pulled from the entities/topic_taxonomy config)."""
    entity_terms = entity_terms or set()
    shared = candidate_terms & page_terms
    if not candidate_terms:
        return {"score": 0.0, "shared_terms": [], "shared_entity_terms": []}
    shared_entities = shared & entity_terms
    shared_generic = shared - entity_terms
    weighted_shared = len(shared_entities) * 3 + len(shared_generic)
    weighted_total = len(entity_terms & candidate_terms) * 3 + len(candidate_terms - entity_terms)
    score = weighted_shared / max(weighted_total, 1)
    return {
        "score": round(score, 4),
        "shared_terms": sorted(shared),
        "shared_entity_terms": sorted(shared_entities),
    }


def entity_terms_for_text(text: str, config: dict[str, Any]) -> set[str]:
    """Tokens in `text` that correspond to configured entities/topics —
    used to weight archive-overlap comparisons toward named things rather
    than generic fitness vocabulary."""
    lowered = (text or "").lower()
    terms: set[str] = set()
    for entity_conf in (config.get("entities", {}) or {}).values():
        for alias in entity_conf.get("aliases", []) or []:
            # Match the whole alias/phrase, not each of its words in
            # isolation — otherwise a multi-word phrase like "strength
            # training" would let bare "training" register as if it named
            # an entity, defeating the entity/generic-vocabulary distinction
            # this function exists to draw.
            if alias and alias.lower() in lowered:
                terms.update(re.findall(r"[a-z0-9]+", alias.lower()))
    for keywords in (config.get("topic_taxonomy", {}) or {}).values():
        for phrase in keywords:
            if phrase and phrase.lower() in lowered:
                terms.update(re.findall(r"[a-z0-9]+", phrase.lower()))
    return terms


def broad_topic_for(text: str, config: dict[str, Any]) -> str | None:
    lowered = (text or "").lower()
    for topic, keywords in (config.get("topic_taxonomy", {}) or {}).items():
        if any(keyword in lowered for keyword in keywords):
            return topic
    return None


# ---------------------------------------------------------------------------
# Freshness gate
# ---------------------------------------------------------------------------

def freshness_gate(
    category: str,
    age_hours: float | None,
    config: dict[str, Any],
    *,
    has_new_development: bool = False,
    has_documented_reason: bool = False,
) -> dict[str, Any]:
    windows = config.get("freshness_windows", {}) or {}
    window = windows.get(category, {})
    hours_limit = window.get("hours")
    hard_gate = bool(window.get("hard_gate", False))

    if hours_limit is None:
        if window.get("requires_reason") and not has_documented_reason:
            return {"status": "fail", "kill_reason": "generic_evergreen"}
        return {"status": "pass", "kill_reason": None}

    if age_hours is None:
        return {"status": "warn", "kill_reason": None}

    if age_hours <= hours_limit:
        return {"status": "pass", "kill_reason": None}

    # A later pickup/announcement/press-release alone is not a new
    # development; callers must only pass has_new_development=True for a
    # specific, independently verifiable event (e.g. a documented
    # search-led service reason), never merely because the item matched
    # this category's keyword pattern.
    if has_new_development:
        return {"status": "warn", "kill_reason": None}

    if hard_gate:
        return {"status": "fail", "kill_reason": "canonical_source_too_old"}
    return {"status": "warn", "kill_reason": None}


# ---------------------------------------------------------------------------
# Saturation storage
# ---------------------------------------------------------------------------

def ensure_saturation_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS publication_history (
            page_url TEXT PRIMARY KEY,
            creator_key TEXT,
            creator_display_name TEXT,
            published_at TEXT,
            format TEXT,
            extracted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_publication_history_creator
            ON publication_history(creator_key, published_at);

        CREATE TABLE IF NOT EXISTS commission_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT,
            creator_key TEXT,
            scanner_type TEXT,
            format TEXT,
            decision TEXT,
            decided_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_commission_history_creator
            ON commission_history(creator_key, decided_at);
        """
    )
    conn.commit()


def record_publication_history(
    conn: sqlite3.Connection,
    *,
    page_url: str,
    creator_key: str | None,
    creator_display_name: str | None,
    published_at: str | None,
    format_: str | None,
) -> None:
    if not page_url:
        return
    conn.execute(
        """
        INSERT INTO publication_history (page_url, creator_key, creator_display_name, published_at, format, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_url) DO UPDATE SET
            creator_key = excluded.creator_key,
            creator_display_name = excluded.creator_display_name,
            published_at = excluded.published_at,
            format = excluded.format,
            extracted_at = excluded.extracted_at
        """,
        (page_url, creator_key, creator_display_name, published_at, format_, iso(utc_now())),
    )


def record_commission_history(
    conn: sqlite3.Connection,
    *,
    lead_id: str,
    creator_key: str | None,
    scanner_type: str,
    format_: str | None,
    decision: str,
    decided_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO commission_history (lead_id, creator_key, scanner_type, format, decision, decided_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (lead_id, creator_key, scanner_type, format_, decision, decided_at),
    )


def compute_saturation(
    creator_key: str | None,
    conn: sqlite3.Connection,
    config: dict[str, Any],
    *,
    format_key: str | None = None,
) -> dict[str, Any]:
    saturation_config = config.get("saturation", {}) or {}
    cooldown_source_days = int(saturation_config.get("cooldown_days_source", 14))
    cooldown_format_days = int(saturation_config.get("cooldown_days_format", 30))
    now = utc_now()

    source_result = {"status": "clear", "recent_story_count": 0, "cooldown_days_remaining": 0, "recent_matches": []}
    format_result = {"status": "clear", "format": format_key, "recent_matches": []}

    if not creator_key:
        return {"source_saturation": source_result, "format_saturation": format_result}

    matches: list[dict[str, Any]] = []
    for table, decided_col in (("publication_history", "published_at"), ("commission_history", "decided_at")):
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE creator_key = ? ORDER BY {decided_col} DESC",
            (creator_key,),
        ).fetchall()
        for row in rows:
            row_dict = dict(row)
            when = row_dict.get(decided_col)
            dt = parse_dt(when)
            if not dt:
                continue
            age_days = (now - dt).total_seconds() / 86400
            row_dict["_age_days"] = age_days
            row_dict["_table"] = table
            matches.append(row_dict)

    recent_source_matches = [m for m in matches if m["_age_days"] <= cooldown_source_days]
    if recent_source_matches:
        newest_age = min(m["_age_days"] for m in recent_source_matches)
        source_result = {
            "status": "blocked" if newest_age < cooldown_source_days else "warning",
            "recent_story_count": len(recent_source_matches),
            "cooldown_days_remaining": max(0, round(cooldown_source_days - newest_age, 1)),
            "recent_matches": [
                {"table": m["_table"], "lead_id": m.get("lead_id"), "page_url": m.get("page_url"), "age_days": round(m["_age_days"], 1)}
                for m in recent_source_matches[:5]
            ],
        }

    if format_key:
        format_matches = [m for m in matches if m.get("format") == format_key and m["_age_days"] <= cooldown_format_days]
        if format_matches:
            format_result = {
                "status": "blocked",
                "format": format_key,
                "recent_matches": [
                    {"table": m["_table"], "lead_id": m.get("lead_id"), "age_days": round(m["_age_days"], 1)}
                    for m in format_matches[:5]
                ],
            }

    return {"source_saturation": source_result, "format_saturation": format_result}


# ---------------------------------------------------------------------------
# Creator breakout confidence
# ---------------------------------------------------------------------------

def breakout_confidence_tier(matched_observation_count: int, config: dict[str, Any]) -> str:
    thresholds = config.get("breakout_confidence", {}).get("min_observations", {})
    if matched_observation_count >= int(thresholds.get("high", 12)):
        return "high"
    if matched_observation_count >= int(thresholds.get("medium", 5)):
        return "medium"
    if matched_observation_count >= int(thresholds.get("low", 1)):
        return "low"
    return "pending"


# ---------------------------------------------------------------------------
# Creator-story eligibility checklist
# ---------------------------------------------------------------------------

def creator_story_eligibility(criteria_results: dict[str, bool], config: dict[str, Any]) -> dict[str, Any]:
    checklist = config.get("creator_story_checklist", {})
    min_pass = int(checklist.get("min_pass", 2))
    passed = [name for name, ok in criteria_results.items() if ok]
    story_value = "strong" if len(passed) >= min_pass + 1 else "moderate" if len(passed) >= min_pass else "weak"
    return {
        "story_value": story_value,
        "checklist_results": criteria_results,
        "criteria_passed": passed,
        "eligible": len(passed) >= min_pass,
    }


# ---------------------------------------------------------------------------
# Canonical research date resolution (Crossref -> PubMed esummary)
# ---------------------------------------------------------------------------

def _http_get_json(url: str, timeout: int = 15) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _default_fetch_crossref(doi: str) -> str | None:
    try:
        data = _http_get_json(f"https://api.crossref.org/works/{doi}")
    except (URLError, TimeoutError, OSError, ValueError):
        return None
    message = data.get("message", {}) if isinstance(data, dict) else {}
    for field in ("published-online", "published-print", "published", "created"):
        parts = (message.get(field) or {}).get("date-parts")
        if parts and parts[0]:
            date_parts = parts[0]
            try:
                year = int(date_parts[0])
                month = int(date_parts[1]) if len(date_parts) > 1 else 1
                day = int(date_parts[2]) if len(date_parts) > 2 else 1
                return iso(datetime(year, month, day, tzinfo=timezone.utc))
            except (ValueError, TypeError, IndexError):
                continue
    return None


def _default_fetch_pubmed(pmid: str) -> str | None:
    try:
        data = _http_get_json(
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={pmid}&retmode=json"
        )
    except (URLError, TimeoutError, OSError, ValueError):
        return None
    result = data.get("result", {}) if isinstance(data, dict) else {}
    doc = result.get(pmid, {}) if isinstance(result, dict) else {}
    date_str = doc.get("epubdate") or doc.get("sortpubdate") or doc.get("pubdate")
    if not date_str:
        return None
    for fmt in ("%Y %b %d", "%Y %b", "%Y/%m/%d", "%Y-%m-%d", "%Y %m %d"):
        try:
            return iso(datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return None


def _load_date_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_date_cache(cache_path: Path, cache: dict[str, str]) -> None:
    try:
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def resolve_canonical_research_date(
    dois: list[str],
    pmids: list[str],
    *,
    fetch_crossref: Callable[[str], str | None] | None = None,
    fetch_pubmed: Callable[[str], str | None] | None = None,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve the true "first published online" date for a study,
    trying Crossref by DOI then PubMed esummary by PMID. Network calls
    are injectable so fixtures/tests stay offline; failures fall back to
    "unresolved" rather than raising, so a scheduled scan never crashes
    on a flaky lookup."""
    fetch_crossref = fetch_crossref or _default_fetch_crossref
    fetch_pubmed = fetch_pubmed or _default_fetch_pubmed
    cache_path = cache_path or RESEARCH_DATE_CACHE_PATH
    cache = _load_date_cache(cache_path)

    for doi in dois:
        cache_key = f"doi:{doi.lower()}"
        if cache_key in cache:
            return {"canonical_published_at": cache[cache_key], "canonical_date_source": "crossref_doi"}
        try:
            date = fetch_crossref(doi)
        except Exception:
            date = None
        if date:
            cache[cache_key] = date
            _save_date_cache(cache_path, cache)
            return {"canonical_published_at": date, "canonical_date_source": "crossref_doi"}

    for pmid in pmids:
        cache_key = f"pmid:{pmid}"
        if cache_key in cache:
            return {"canonical_published_at": cache[cache_key], "canonical_date_source": "pubmed_pmid"}
        try:
            date = fetch_pubmed(pmid)
        except Exception:
            date = None
        if date:
            cache[cache_key] = date
            _save_date_cache(cache_path, cache)
            return {"canonical_published_at": date, "canonical_date_source": "pubmed_pmid"}

    return {"canonical_published_at": None, "canonical_date_source": "unresolved"}


# ---------------------------------------------------------------------------
# Balanced slate construction
# ---------------------------------------------------------------------------

def select_slate(candidates: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Diversity-constrained slate admission over score-descending
    candidates. Each candidate dict is expected to carry: lead_id,
    scanner_type, score (numeric), creator_key (optional),
    broad_topic (optional), is_australian (optional bool),
    is_current_development (optional bool), story_category (optional),
    service_story_reason (optional). Returns admitted leads plus a
    kill_reason_codes-tagged list of everything excluded purely for
    diversity reasons (not for failing an earlier hard gate)."""
    constraints = config.get("slate_constraints", {})
    max_creator = int(constraints.get("max_creator", 2))
    max_research = int(constraints.get("max_research", 2))
    max_same_topic = int(constraints.get("max_same_broad_topic", 2))
    max_slate_size = int(constraints.get("max_slate_size", 6))

    ordered = sorted(candidates, key=lambda lead: lead.get("score", 0), reverse=True)

    admitted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen_creator_keys: set[str] = set()
    type_counts = {"creator": 0, "research": 0}
    topic_counts: dict[str, int] = {}

    for lead in ordered:
        if len(admitted) >= max_slate_size:
            excluded.append({**lead, "kill_reason_codes": ["diversity_cap_reached"]})
            continue

        scanner_type = lead.get("scanner_type")
        creator_key = lead.get("creator_key")
        broad_topic = lead.get("broad_topic")
        story_category = lead.get("story_category")

        if story_category == "service" and not lead.get("service_story_reason"):
            excluded.append({**lead, "kill_reason_codes": ["generic_evergreen"]})
            continue

        if scanner_type == "creator" and type_counts["creator"] >= max_creator:
            excluded.append({**lead, "kill_reason_codes": ["diversity_cap_reached"]})
            continue
        if scanner_type == "research" and type_counts["research"] >= max_research:
            excluded.append({**lead, "kill_reason_codes": ["diversity_cap_reached"]})
            continue
        if creator_key and creator_key in seen_creator_keys:
            excluded.append({**lead, "kill_reason_codes": ["creator_source_cooldown"]})
            continue
        if broad_topic and topic_counts.get(broad_topic, 0) >= max_same_topic:
            excluded.append({**lead, "kill_reason_codes": ["diversity_cap_reached"]})
            continue

        admitted.append(lead)
        if scanner_type in type_counts:
            type_counts[scanner_type] += 1
        if creator_key:
            seen_creator_keys.add(creator_key)
        if broad_topic:
            topic_counts[broad_topic] = topic_counts.get(broad_topic, 0) + 1

    return {
        "admitted": admitted,
        "excluded_for_diversity": excluded,
        "has_australian_story": any(lead.get("is_australian") for lead in admitted),
        "has_current_development": any(lead.get("is_current_development") for lead in admitted),
    }
