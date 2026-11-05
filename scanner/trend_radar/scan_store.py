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
