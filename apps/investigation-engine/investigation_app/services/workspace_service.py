"""Workspace session state: autosave, restore, crash recovery.

Persists the investigator's UI state (open tabs, active tab, layout, filters,
recent searches, pinned evidence) as a single JSON blob per case in the
``workspace`` table. Autosaved from the client on change; restored on load so
a crash or reload returns the investigator to exactly where they were.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from investigation_app.extensions import db_write_lock, get_connection, run_with_db_retry


def _case_id(conn, case_uid: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM cases WHERE uid = ?", (case_uid,)).fetchone()
    return row["id"] if row else None


def load(case_uid: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        case_id = _case_id(conn, case_uid)
        if case_id is None:
            return {}
        row = conn.execute(
            "SELECT state_json FROM workspace WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None or not row["state_json"]:
            return {}
        try:
            return json.loads(row["state_json"])
        except ValueError:
            return {}
    finally:
        conn.close()


def save(case_uid: str, state: Dict[str, Any]) -> bool:
    """Upsert the workspace state blob for a case.

    Autosave must never break the UI. During large evidence ingestion SQLite can
    briefly have a writer lock, so retry and then fail soft instead of returning
    a Flask 500.
    """
    payload = json.dumps(state)
    for attempt in range(6):
        conn = get_connection()
        try:
            case_id = _case_id(conn, case_uid)
            if case_id is None:
                return False
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with db_write_lock():
                conn.execute(
                    "INSERT INTO workspace (case_id, state_json, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(case_id) DO UPDATE SET "
                    "state_json = excluded.state_json, updated_at = excluded.updated_at",
                    (case_id, payload, now),
                )
                conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if "locked" not in str(exc).lower() or attempt >= 5:
                return False
            time.sleep(0.15 * (attempt + 1))
        finally:
            conn.close()
    return False


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    def _set() -> None:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()
    try:
        run_with_db_retry(_set, attempts=6, base_sleep=0.15)
    except sqlite3.OperationalError:
        # This setting is non-critical UI state; never crash the workspace API.
        return None
