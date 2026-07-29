"""SQLite connection helper for the Investigation Intelligence Engine.

Provides a single place to open tuned, offline SQLite connections:

* WAL journal mode for concurrent reads during background indexing.
* ``foreign_keys`` enforced for referential integrity.
* ``row_factory`` set to ``sqlite3.Row`` so services get dict-like rows.

Connections are created per request/worker and closed by the caller. The
database and its evidence store live under the module ``instance/`` folder,
mirroring the pattern used by the other portal modules.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional, TypeVar

from investigation_app.config import runtime_root

_INSTANCE_DIR = runtime_root()
_DB_PATH = os.path.join(_INSTANCE_DIR, "iie.db")
_EVIDENCE_DIR = os.path.join(_INSTANCE_DIR, "evidence_store")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "db", "schema.sql")

T = TypeVar("T")
_DB_WRITE_LOCK = threading.RLock()
_WAL_SETUP_LOCK = threading.Lock()
_WAL_SETUP_DONE = False


@contextmanager
def db_write_lock():
    """Process-local write gate for SQLite.

    SQLite allows many readers but only one writer. Large XLSX evidence jobs can
    produce thousands of entity/transaction/FTS rows, while the browser may also
    autosave, upload, delete, reprocess, or ask AI at the same time. This lock
    serializes writes inside this Flask process so those actions wait briefly
    instead of racing into ``database is locked`` errors.
    """
    with _DB_WRITE_LOCK:
        yield


def run_with_db_retry(fn: Callable[[], T], attempts: int = 8, base_sleep: float = 0.20) -> T:
    """Run a DB write function with lock-aware retry on transient lock errors."""
    last_exc = None
    for attempt in range(max(1, attempts)):
        try:
            with db_write_lock():
                return fn()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            time.sleep(base_sleep * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def instance_dir() -> str:
    os.makedirs(_INSTANCE_DIR, exist_ok=True)
    return _INSTANCE_DIR


def evidence_dir() -> str:
    os.makedirs(_EVIDENCE_DIR, exist_ok=True)
    return _EVIDENCE_DIR


def db_path() -> str:
    return _DB_PATH


def _ensure_wal_once(conn: sqlite3.Connection) -> None:
    """Best-effort WAL setup without fighting every active request.

    Running ``PRAGMA journal_mode=WAL`` on every connection can itself need a
    lock. During heavy XLSX processing that made normal reads/writes compete
    with startup tuning. We try once per process; schema initialisation also
    applies WAL, so this remains safe and idempotent.
    """
    global _WAL_SETUP_DONE
    if _WAL_SETUP_DONE:
        return
    with _WAL_SETUP_LOCK:
        if _WAL_SETUP_DONE:
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            # Not fatal; busy_timeout and process write lock still protect the
            # app. A later process restart or init_db call can apply WAL.
            pass
        _WAL_SETUP_DONE = True


def get_connection() -> sqlite3.Connection:
    """Open a tuned SQLite connection. Caller is responsible for closing.

    Large XLSX evidence can keep the writer busy for a short time while the
    background worker replaces transactions/entities/timeline rows. The UI also
    autosaves workspace state during that window. Use a generous busy timeout,
    WAL and process-level write gating so small UI writes wait instead of
    crashing with ``sqlite3.OperationalError: database is locked``.
    """
    instance_dir()
    conn = sqlite3.connect(_DB_PATH, timeout=300.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=300000;")
    _ensure_wal_once(conn)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-20000;")
    return conn


def init_db(schema_path: Optional[str] = None) -> None:
    """Create tables from schema.sql if they do not already exist.

    Idempotent: every statement in the schema uses ``IF NOT EXISTS`` so this
    is safe to call on every startup.
    """
    path = schema_path or _SCHEMA_PATH
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        script = handle.read()
    conn = get_connection()
    try:
        conn.executescript(script)
        conn.commit()
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply small, idempotent column additions to existing databases.

    ``CREATE TABLE IF NOT EXISTS`` never alters a table that already exists,
    so older installs may be missing columns added to existing tables. Each
    ALTER is guarded so this is safe to run on every startup.
    """
    def _columns(table: str) -> set[str]:
        try:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.OperationalError:
            return set()

    def _add_column(table: str, column: str, ddl: str) -> None:
        if column in _columns(table):
            return
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already present / table missing - ignore

    # Columns added to the embeddings table after its first release.
    for column, ddl in (
        ("case_id", "ALTER TABLE embeddings ADD COLUMN case_id INTEGER"),
        ("seq", "ALTER TABLE embeddings ADD COLUMN seq INTEGER"),
        ("text", "ALTER TABLE embeddings ADD COLUMN text TEXT"),
    ):
        _add_column("embeddings", column, ddl)

    # Structured intelligence and progress tracking are additive for existing installations.
    _add_column("evidence", "intel_json", "ALTER TABLE evidence ADD COLUMN intel_json TEXT")
    _add_column("evidence", "progress_percent", "ALTER TABLE evidence ADD COLUMN progress_percent REAL NOT NULL DEFAULT 0")
    _add_column("evidence", "progress_current", "ALTER TABLE evidence ADD COLUMN progress_current INTEGER DEFAULT 0")
    _add_column("evidence", "progress_total", "ALTER TABLE evidence ADD COLUMN progress_total INTEGER DEFAULT 0")
    _add_column("evidence", "progress_detail", "ALTER TABLE evidence ADD COLUMN progress_detail TEXT")
    _add_column("evidence_similarity", "reasons", "ALTER TABLE evidence_similarity ADD COLUMN reasons TEXT")

    # Universal intelligence tables added after first release. These are
    # additive and safe for existing installations.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS communications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
          evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
          platform TEXT, sender TEXT, receiver TEXT, sender_handle TEXT, receiver_handle TEXT,
          message_text TEXT, timestamp TEXT, entities_json TEXT, attachments_json TEXT,
          urls_json TEXT, amounts_json TEXT, risk_flags_json TEXT, source_ref TEXT,
          confidence REAL, created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_communications_case ON communications(case_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_communications_evidence ON communications(evidence_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_communications_platform ON communications(case_id, platform)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS social_profiles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
          evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
          platform TEXT, profile_name TEXT, username TEXT, profile_url TEXT, bio TEXT,
          metadata_json TEXT, source_ref TEXT, confidence REAL, created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_social_profiles_case ON social_profiles(case_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_social_profiles_username ON social_profiles(case_id, username)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS technical_indicators (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
          evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
          type TEXT NOT NULL, value TEXT NOT NULL, norm TEXT NOT NULL,
          source_ref TEXT, confidence REAL, metadata_json TEXT, created_at TEXT NOT NULL,
          UNIQUE(case_id, evidence_id, type, norm)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_technical_indicators_case ON technical_indicators(case_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_technical_indicators_norm ON technical_indicators(case_id, type, norm)")
