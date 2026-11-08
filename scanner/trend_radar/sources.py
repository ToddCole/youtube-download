"""Source configuration: trend_sources.json (human-authored, like
channels.json/news_sources.json in the old scanner) is the registry of
record. Each source is upserted into the `sources` table on every run so
runtime state (access_status, last_collected_at, last_error) is queryable
without re-reading the config file, while the config file stays the
single place a human edits the registry.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCES_PATH = BASE_DIR / "trend_sources.json"
BEATS_PATH = BASE_DIR / "beats.json"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def beat_source_configs(path: Path | None = None) -> list[dict]:
    """beats.json -> one Google News (AU, last 7 days) source per query."""
    path = path or BEATS_PATH
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    configs = []
    for beat in data.get("beats", []):
        active = beat.get("active", True)
        for query in beat.get("queries", []):
            configs.append({
                "source_id": f"beat:{beat['beat']}:{_slug(query)}",
                "name": f"Beat ({beat.get('name') or beat['beat']}): {query}",
                "lane": "publisher",
                "platform": "google-news",
                "canonical_identifier": f"{query} when:7d",
                "collection_method": "google_news_rss_search",
                "collection_interval_hours": 4,
                "reliability": 5,
                "source_role": "signal",
                "active": active,
                "known_restrictions": "Beat discovery feed (beats.json), not an evidence authority.",
                "engagement_available": False,
                "beat": beat["beat"],
            })
    return configs


def load_source_configs(path: Path | None = None) -> list[dict]:
    path = path or SOURCES_PATH
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    configs = data.get("sources", []) if isinstance(data, dict) else []
    if path == SOURCES_PATH:
        configs = configs + beat_source_configs()
    return configs


def sync_sources(conn: sqlite3.Connection, configs: list[dict]) -> None:
    """Upserts every configured source into the sources table. Authored
    fields are refreshed from config on every sync; runtime fields
    (access_status, last_collected_at, last_success_at, last_error) are
    left untouched here -- those are only updated by collection itself."""
    for cfg in configs:
        existing = conn.execute(
            "SELECT source_id FROM sources WHERE source_id = ?", (cfg["source_id"],)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE sources SET
                    name = ?, lane = ?, platform = ?, canonical_identifier = ?,
                    collection_method = ?, collection_interval_hours = ?,
                    reliability = ?, source_role = ?, active = ?,
                    known_restrictions = ?, engagement_available = ?, config_json = ?
                   WHERE source_id = ?""",
                (
                    cfg["name"], cfg["lane"], cfg["platform"], cfg["canonical_identifier"],
                    cfg["collection_method"], cfg.get("collection_interval_hours", 4),
                    cfg.get("reliability"), cfg["source_role"], int(cfg.get("active", True)),
                    cfg.get("known_restrictions"), int(cfg.get("engagement_available", False)),
                    json.dumps(cfg), cfg["source_id"],
                ),
            )
        else:
            conn.execute(
                """INSERT INTO sources (
                    source_id, name, lane, platform, canonical_identifier,
                    collection_method, collection_interval_hours, reliability,
                    source_role, active, known_restrictions, engagement_available,
                    access_status, config_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cfg["source_id"], cfg["name"], cfg["lane"], cfg["platform"],
                    cfg["canonical_identifier"], cfg["collection_method"],
                    cfg.get("collection_interval_hours", 4), cfg.get("reliability"),
                    cfg["source_role"], int(cfg.get("active", True)),
                    cfg.get("known_restrictions"), int(cfg.get("engagement_available", False)),
                    "unknown", json.dumps(cfg),
                ),
            )
    conn.commit()


def active_sources(conn: sqlite3.Connection, lane: str | None = None) -> list[sqlite3.Row]:
    if lane:
        return conn.execute(
            "SELECT * FROM sources WHERE active = 1 AND lane = ? ORDER BY source_id", (lane,)
        ).fetchall()
    return conn.execute("SELECT * FROM sources WHERE active = 1 ORDER BY lane, source_id").fetchall()


def update_source_status(
    conn: sqlite3.Connection,
    source_id: str,
    status: str,
    now: str,
    last_error: str | None = None,
) -> None:
    if status == "ok":
        conn.execute(
            "UPDATE sources SET access_status = ?, last_collected_at = ?, last_success_at = ?, last_error = NULL WHERE source_id = ?",
            (status, now, now, source_id),
        )
    else:
        conn.execute(
            "UPDATE sources SET access_status = ?, last_collected_at = ?, last_error = ? WHERE source_id = ?",
            (status, now, last_error, source_id),
        )
    conn.commit()
