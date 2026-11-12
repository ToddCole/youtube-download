"""Persists each completed Claude scan so the Desk can show the latest
picks (and history) instead of the result living only in a JSON file."""

from __future__ import annotations

import json
import sqlite3


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS claude_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at TEXT NOT NULL,
            model TEXT,
            cost_usd REAL,
            candidate_count INTEGER,
            repair_count INTEGER,
            result_json TEXT NOT NULL
        )"""
    )
    conn.commit()


def save_scan(conn: sqlite3.Connection, result: dict) -> int:
    ensure_tables(conn)
    cursor = conn.execute(
        """INSERT INTO claude_scans (generated_at, model, cost_usd, candidate_count, repair_count, result_json)
           VALUES (?,?,?,?,?,?)""",
        (
            result.get("generated_at"),
            result.get("model"),
            (result.get("usage") or {}).get("cost_usd"),
            len(result.get("candidates") or []),
            len(result.get("_validation_repairs") or []),
            json.dumps(result, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def latest_scan(conn: sqlite3.Connection) -> dict | None:
    ensure_tables(conn)
    row = conn.execute("SELECT id, result_json FROM claude_scans ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    result = json.loads(row[1])
    result["_scan_id"] = row[0]
    return result


def update_scan(conn: sqlite3.Connection, scan_id: int, result: dict) -> None:
    """Rewrite a stored scan (e.g. after a re-run of the fit-food pass)."""
    ensure_tables(conn)
    clean = {k: v for k, v in result.items() if k != "_scan_id"}
    conn.execute("UPDATE claude_scans SET cost_usd=?, result_json=? WHERE id=?",
                 ((clean.get("usage") or {}).get("cost_usd"), json.dumps(clean, ensure_ascii=False), scan_id))
    conn.commit()


def latest_fit_food_scan(conn: sqlite3.Connection, limit: int = 6) -> dict | None:
    """The newest scan that ran fit food (it runs once a day, so the
    afternoon scan usually has none)."""
    ensure_tables(conn)
    for scan_id, raw in conn.execute("SELECT id, result_json FROM claude_scans ORDER BY id DESC LIMIT ?", (limit,)):
        result = json.loads(raw)
        if result.get("fit_food"):
            result["_scan_id"] = scan_id
            return result
    return None
