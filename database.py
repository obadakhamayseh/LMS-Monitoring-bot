import os
import json
import time
import hashlib
import secrets
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config import DATABASE_PATH, DATABASE_URL, DIRECT_URL, CHECK_INTERVAL_MINUTES
from security import (
    decrypt_legacy_password,
    decrypt_password,
    encrypt_password,
    is_encrypted_v1,
    is_legacy_fernet_token,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None

logger = logging.getLogger(__name__)

# Default TTL for paired PWA/mobile device sessions (30 days)
DEFAULT_SESSION_TTL_SECONDS = 30 * 86400

# SQLite busy timeout (milliseconds)
SQLITE_BUSY_TIMEOUT_MS = 10000

# Process-wide PostgreSQL ConnectionPool (replaces thread-local connections)
_pg_pool: Any = None
_pg_pool_url: Optional[str] = None
_pg_pool_lock = threading.Lock()

# Serialization locks for SQLite atomic read-modify-write / check-and-delete flows
_sqlite_meta_lock = threading.Lock()
_sqlite_pair_lock = threading.Lock()


# ── PostgreSQL URL & PgBouncer policy ────────────────────────────────────
# Policy (H3 / C9):
#   * Runtime pooled queries prefer DATABASE_URL (Supabase PgBouncer transaction
#     pooler, port 6543) to avoid exhausting direct Postgres connection slots,
#     falling back to DIRECT_URL when DATABASE_URL is unset.
#   * Schema initialization / migrations (init_db) prefer DIRECT_URL (direct
#     session port 5432) when configured, falling back to DATABASE_URL.
#   * Whenever a PgBouncer URL is detected (?pgbouncer=true, pooler host, or
#     port 6543), psycopg3 prepared statements are explicitly disabled via
#     prepare_threshold=None and the non-libpq `pgbouncer` query param is
#     stripped cleanly via urllib.parse.

def _clean_pg_url(raw_url: str) -> tuple[str, bool]:
    """Strip non-libpq `pgbouncer` query parameter and detect PgBouncer endpoints."""
    parts = urlsplit(raw_url.strip())
    qsl = parse_qsl(parts.query, keep_blank_values=True)
    has_pgbouncer_param = any(
        k.lower() == "pgbouncer" and v.lower() in ("1", "true", "yes")
        for k, v in qsl
    )
    filtered_qsl = [(k, v) for k, v in qsl if k.lower() != "pgbouncer"]
    clean = urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(filtered_qsl, doseq=True),
        parts.fragment,
    ))
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    is_pgbouncer = has_pgbouncer_param or ("pooler" in host) or (port == 6543)
    return clean, is_pgbouncer


def _resolve_pg_url(prefer_direct: bool = False) -> tuple[Optional[str], bool]:
    """Resolve PostgreSQL URL per runtime (DATABASE_URL) vs migration (DIRECT_URL) policy."""
    db_url = (DATABASE_URL or os.getenv("DATABASE_URL", "")).strip()
    dir_url = (DIRECT_URL or os.getenv("DIRECT_URL", "")).strip()
    raw = (dir_url or db_url) if prefer_direct else (db_url or dir_url)
    if not raw:
        return None, False
    return _clean_pg_url(raw)


def _get_pg_url(prefer_direct: bool = False) -> Optional[str]:
    clean, _ = _resolve_pg_url(prefer_direct=prefer_direct)
    return clean


def _get_pg_pool(pg_url: str, is_pgbouncer: bool = False):
    """Return a lazily initialized psycopg_pool.ConnectionPool for runtime queries."""
    global _pg_pool, _pg_pool_url
    with _pg_pool_lock:
        if _pg_pool is not None and _pg_pool_url == pg_url:
            return _pg_pool
        if _pg_pool is not None:
            try:
                _pg_pool.close()
            except Exception:
                pass
            _pg_pool = None
            _pg_pool_url = None

        conn_kwargs: dict[str, Any] = {
            "row_factory": dict_row,
            "autocommit": False,
            "connect_timeout": 15,
        }
        if is_pgbouncer:
            # Required for PgBouncer transaction pooling mode
            conn_kwargs["prepare_threshold"] = None

        _pg_pool = ConnectionPool(
            pg_url,
            min_size=1,
            max_size=5,
            timeout=10,
            kwargs=conn_kwargs,
        )
        _pg_pool_url = pg_url
        return _pg_pool


def close() -> None:
    """Safely close the PostgreSQL connection pool (idempotent; no-op on SQLite)."""
    global _pg_pool, _pg_pool_url
    with _pg_pool_lock:
        pool = _pg_pool
        _pg_pool = None
        _pg_pool_url = None
    if pool is not None:
        try:
            pool.close()
        except Exception as exc:
            logger.debug("Ignoring error while closing PG pool: %s", exc)


close_pool = close


def _is_integrity_error(exc: BaseException) -> bool:
    """Return True if exc is a database IntegrityError (must NEVER be retried)."""
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    if psycopg is not None:
        pg_errors = getattr(psycopg, "errors", None)
        if pg_errors is not None and isinstance(exc, pg_errors.IntegrityError):
            return True
    return any(cls.__name__ == "IntegrityError" for cls in type(exc).__mro__)


def _is_retryable_pg_error(exc: BaseException) -> bool:
    """Return True only for transient connection/operational errors (never IntegrityError)."""
    if _is_integrity_error(exc):
        return False
    if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
        return True
    if psycopg is not None:
        retryable_types = tuple(
            t for t in (
                getattr(psycopg, "OperationalError", None),
                getattr(psycopg, "InterfaceError", None),
            )
            if isinstance(t, type)
        )
        if retryable_types and isinstance(exc, retryable_types):
            return True
    return any(
        cls.__name__ in ("OperationalError", "InterfaceError")
        for cls in type(exc).__mro__
    )


class DBContext:
    def __init__(self, *, use_direct: bool = False, immediate: bool = False):
        self.use_direct = use_direct
        self.immediate = immediate
        self.pg_url, self.is_pgbouncer = _resolve_pg_url(prefer_direct=use_direct)
        runtime_url, _ = _resolve_pg_url(prefer_direct=False)
        self._use_direct_conn = bool(use_direct and self.pg_url and runtime_url and self.pg_url != runtime_url)
        self.is_pg = bool(
            self.pg_url
            and psycopg is not None
            and (self._use_direct_conn or ConnectionPool is not None)
        )
        self.conn = None
        self._pool_cm = None
        self._tx_started = False

    def _open_pg_connection(self):
        if self._use_direct_conn:
            conn_kwargs: dict[str, Any] = {
                "autocommit": False,
                "row_factory": dict_row,
                "connect_timeout": 15,
            }
            if self.is_pgbouncer:
                conn_kwargs["prepare_threshold"] = None
            self.conn = psycopg.connect(self.pg_url, **conn_kwargs)
            self._pool_cm = None
        else:
            pool = _get_pg_pool(self.pg_url, is_pgbouncer=self.is_pgbouncer)
            self._pool_cm = pool.connection()
            self.conn = self._pool_cm.__enter__()

    def _release_pg_connection(self, exc_type=None, exc_val=None, exc_tb=None):
        pool_cm = self._pool_cm
        conn = self.conn
        self._pool_cm = None
        self.conn = None
        if pool_cm is not None:
            pool_cm.__exit__(exc_type, exc_val, exc_tb)
        elif conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def __enter__(self):
        self._tx_started = False
        if self.is_pg:
            self._open_pg_connection()
        else:
            self.conn = sqlite3.connect(
                DATABASE_PATH,
                timeout=10,
                isolation_level=None if self.immediate else "",
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            if self.immediate:
                self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.conn:
            return
        if not self.is_pg:
            try:
                if self.immediate:
                    if exc_type is None:
                        self.conn.execute("COMMIT")
                    else:
                        self.conn.execute("ROLLBACK")
                else:
                    if exc_type is None:
                        self.conn.commit()
                    else:
                        self.conn.rollback()
            finally:
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
        else:
            try:
                if exc_type is None:
                    try:
                        self.conn.commit()
                    except Exception:
                        try:
                            self.conn.rollback()
                        except Exception:
                            pass
                        raise
                else:
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
            finally:
                self._release_pg_connection(exc_type, exc_val, exc_tb)

    def execute(self, sql, params=()):
        if self.is_pg:
            pg_sql = sql.replace("?", "%s")
            try:
                cur = self.conn.cursor()
                cur.execute(pg_sql, params)
                self._tx_started = True
                return cur
            except Exception as exc:
                # Never retry IntegrityError, and never retry mid-transaction
                # after earlier statements have already run on this connection.
                if self._tx_started or not _is_retryable_pg_error(exc):
                    raise
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                self._release_pg_connection(type(exc), exc, exc.__traceback__)
                self._open_pg_connection()
                cur = self.conn.cursor()
                cur.execute(pg_sql, params)
                self._tx_started = True
                return cur
        else:
            cur = self.conn.execute(sql, params)
            self._tx_started = True
            return cur

    def fetchone(self, sql, params=()):
        cur = self.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self, sql, params=()):
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def health_check() -> bool:
    """Run a lightweight `SELECT 1` probe against the active database."""
    try:
        with DBContext() as db:
            row = db.fetchone("SELECT 1 AS ok")
            return bool(row)
    except Exception as exc:
        logger.warning("Database health check failed: %s", exc)
        return False


def init_db():
    with DBContext(use_direct=True) as db:
        if db.is_pg:
            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id     BIGINT PRIMARY KEY,
                    lms_username    TEXT    NOT NULL,
                    lms_password    TEXT    NOT NULL,
                    lms_user_id     TEXT,
                    full_name       TEXT,
                    is_monitoring   INTEGER DEFAULT 0,
                    notify_assign   INTEGER DEFAULT 1,
                    notify_quiz     INTEGER DEFAULT 1,
                    notify_resource INTEGER DEFAULT 1,
                    check_interval  INTEGER DEFAULT 10,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    last_check      TEXT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS user_courses (
                    id           BIGSERIAL PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL,
                    course_id    BIGINT NOT NULL,
                    course_name  TEXT    NOT NULL,
                    course_url   TEXT,
                    is_monitored INTEGER DEFAULT 1,
                    is_seeded    INTEGER DEFAULT 0,
                    added_at     TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, course_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS seen_content (
                    id           BIGSERIAL PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL,
                    course_id    BIGINT NOT NULL,
                    content_id   TEXT    NOT NULL,
                    content_type TEXT    NOT NULL,
                    content_name TEXT,
                    content_url  TEXT,
                    due_date     TEXT,
                    is_submitted INTEGER DEFAULT 0,
                    first_seen   TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, content_id, content_type)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id           BIGSERIAL PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL,
                    course_id    BIGINT NOT NULL,
                    content_id   TEXT    NOT NULL,
                    content_name TEXT,
                    content_url  TEXT,
                    due_ts       BIGINT,
                    due_str      TEXT,
                    reminded_24h INTEGER DEFAULT 0,
                    reminded_3h  INTEGER DEFAULT 0,
                    reminded_1h  INTEGER DEFAULT 0,
                    is_submitted INTEGER DEFAULT 0,
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, content_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS course_grades (
                    id           BIGSERIAL PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL,
                    course_id    BIGINT NOT NULL,
                    item_id      TEXT    NOT NULL,
                    item_name    TEXT,
                    grade_val    TEXT,
                    grade_max    TEXT,
                    percentage   TEXT,
                    feedback     TEXT,
                    item_url     TEXT,
                    updated_at   TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, course_id, item_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS course_announcements (
                    id           BIGSERIAL PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL,
                    course_id    BIGINT NOT NULL,
                    discuss_id   TEXT    NOT NULL,
                    forum_name   TEXT,
                    title        TEXT,
                    author       TEXT,
                    posted_date  TEXT,
                    body_text    TEXT,
                    url          TEXT,
                    first_seen   TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, discuss_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS course_attendance (
                    id              BIGSERIAL PRIMARY KEY,
                    telegram_id     BIGINT NOT NULL,
                    course_id       BIGINT NOT NULL,
                    att_id          TEXT    NOT NULL,
                    taken_sessions  INTEGER DEFAULT 0,
                    total_sessions  INTEGER DEFAULT 0,
                    present_count   INTEGER DEFAULT 0,
                    absent_count    INTEGER DEFAULT 0,
                    late_count      INTEGER DEFAULT 0,
                    excused_count   INTEGER DEFAULT 0,
                    points_str      TEXT,
                    percentage_str  TEXT,
                    sessions_json   TEXT,
                    att_url         TEXT,
                    updated_at      TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, course_id, att_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS app_sessions (
                    token_hash   TEXT PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL,
                    device       TEXT,
                    created_at   TEXT,
                    last_used    TEXT,
                    expires_at   BIGINT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS pair_codes (
                    code         TEXT PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL,
                    expires_at   BIGINT NOT NULL,
                    created_at   BIGINT NOT NULL
                )
            """)
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id         INTEGER PRIMARY KEY,
                    lms_username        TEXT    NOT NULL,
                    lms_password        TEXT    NOT NULL,
                    lms_user_id         TEXT,
                    full_name           TEXT,
                    is_monitoring       INTEGER DEFAULT 0,
                    notify_assign       INTEGER DEFAULT 1,
                    notify_quiz         INTEGER DEFAULT 1,
                    notify_resource     INTEGER DEFAULT 1,
                    notify_grade        INTEGER DEFAULT 1,
                    notify_announcement INTEGER DEFAULT 1,
                    check_interval      INTEGER DEFAULT 10,
                    created_at          TEXT    DEFAULT (datetime('now','localtime')),
                    last_check          TEXT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS user_courses (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id  INTEGER NOT NULL,
                    course_id    INTEGER NOT NULL,
                    course_name  TEXT    NOT NULL,
                    course_url   TEXT,
                    is_monitored INTEGER DEFAULT 1,
                    is_seeded    INTEGER DEFAULT 0,
                    added_at     TEXT    DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, course_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS seen_content (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id  INTEGER NOT NULL,
                    course_id    INTEGER NOT NULL,
                    content_id   TEXT    NOT NULL,
                    content_type TEXT    NOT NULL,
                    content_name TEXT,
                    content_url  TEXT,
                    due_date     TEXT,
                    is_submitted INTEGER DEFAULT 0,
                    first_seen   TEXT    DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, content_id, content_type)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id  INTEGER NOT NULL,
                    course_id    INTEGER NOT NULL,
                    content_id   TEXT    NOT NULL,
                    content_name TEXT,
                    content_url  TEXT,
                    due_ts       INTEGER,
                    due_str      TEXT,
                    reminded_24h INTEGER DEFAULT 0,
                    reminded_3h  INTEGER DEFAULT 0,
                    reminded_1h  INTEGER DEFAULT 0,
                    is_submitted INTEGER DEFAULT 0,
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, content_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS course_grades (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id  INTEGER NOT NULL,
                    course_id    INTEGER NOT NULL,
                    item_id      TEXT    NOT NULL,
                    item_name    TEXT,
                    grade_val    TEXT,
                    grade_max    TEXT,
                    percentage   TEXT,
                    feedback     TEXT,
                    item_url     TEXT,
                    updated_at   TEXT    DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, course_id, item_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS course_announcements (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id  INTEGER NOT NULL,
                    course_id    INTEGER NOT NULL,
                    discuss_id   TEXT    NOT NULL,
                    forum_name   TEXT,
                    title        TEXT,
                    author       TEXT,
                    posted_date  TEXT,
                    body_text    TEXT,
                    url          TEXT,
                    first_seen   TEXT    DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, discuss_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS course_attendance (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id     INTEGER NOT NULL,
                    course_id       INTEGER NOT NULL,
                    att_id          TEXT    NOT NULL,
                    taken_sessions  INTEGER DEFAULT 0,
                    total_sessions  INTEGER DEFAULT 0,
                    present_count   INTEGER DEFAULT 0,
                    absent_count    INTEGER DEFAULT 0,
                    late_count      INTEGER DEFAULT 0,
                    excused_count   INTEGER DEFAULT 0,
                    points_str      TEXT,
                    percentage_str  TEXT,
                    sessions_json   TEXT,
                    att_url         TEXT,
                    updated_at      TEXT    DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, course_id, att_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS app_sessions (
                    token_hash   TEXT PRIMARY KEY,
                    telegram_id  INTEGER NOT NULL,
                    device       TEXT,
                    created_at   TEXT,
                    last_used    TEXT,
                    expires_at   INTEGER
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS pair_codes (
                    code         TEXT PRIMARY KEY,
                    telegram_id  INTEGER NOT NULL,
                    expires_at   INTEGER NOT NULL,
                    created_at   INTEGER NOT NULL
                )
            """)

        # Migrate users from the old 10-hour default to the new 1-hour default
        # once, without overwriting later per-user interval choices.
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
        interval_migration = "check_interval_600_to_60_v1"
        if not db.fetchone("SELECT version FROM schema_migrations WHERE version=?",
                           (interval_migration,)):
            db.execute("UPDATE users SET check_interval=60 WHERE check_interval=600")
            db.execute("INSERT INTO schema_migrations (version) VALUES (?)",
                       (interval_migration,))
        interval_migration = "check_interval_5_to_10_v1"
        if not db.fetchone("SELECT version FROM schema_migrations WHERE version=?",
                           (interval_migration,)):
            db.execute("UPDATE users SET check_interval=10 WHERE check_interval=5")
            db.execute("INSERT INTO schema_migrations (version) VALUES (?)",
                       (interval_migration,))

        # Idempotent column migrations
        try:
            if db.is_pg:
                db.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS is_submitted INTEGER DEFAULT 0")
                db.execute("ALTER TABLE seen_content ADD COLUMN IF NOT EXISTS is_submitted INTEGER DEFAULT 0")
                db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_grade INTEGER DEFAULT 1")
                db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_announcement INTEGER DEFAULT 1")
                db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_attendance INTEGER DEFAULT 1")
                db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS user_meta TEXT DEFAULT '{}'")
                db.execute("ALTER TABLE user_courses ADD COLUMN IF NOT EXISTS is_seeded INTEGER DEFAULT 0")
                db.execute("ALTER TABLE app_sessions ADD COLUMN IF NOT EXISTS expires_at BIGINT")
            else:
                cols = [r["name"] for r in db.fetchall("PRAGMA table_info(reminders)")]
                if "is_submitted" not in cols:
                    db.execute("ALTER TABLE reminders ADD COLUMN is_submitted INTEGER DEFAULT 0")
                scols = [r["name"] for r in db.fetchall("PRAGMA table_info(seen_content)")]
                if "is_submitted" not in scols:
                    db.execute("ALTER TABLE seen_content ADD COLUMN is_submitted INTEGER DEFAULT 0")
                ucols = [r["name"] for r in db.fetchall("PRAGMA table_info(users)")]
                if "notify_grade" not in ucols:
                    db.execute("ALTER TABLE users ADD COLUMN notify_grade INTEGER DEFAULT 1")
                if "notify_announcement" not in ucols:
                    db.execute("ALTER TABLE users ADD COLUMN notify_announcement INTEGER DEFAULT 1")
                if "notify_attendance" not in ucols:
                    db.execute("ALTER TABLE users ADD COLUMN notify_attendance INTEGER DEFAULT 1")
                if "user_meta" not in ucols:
                    db.execute("ALTER TABLE users ADD COLUMN user_meta TEXT DEFAULT '{}'")
                ccols = [r["name"] for r in db.fetchall("PRAGMA table_info(user_courses)")]
                if "is_seeded" not in ccols:
                    db.execute("ALTER TABLE user_courses ADD COLUMN is_seeded INTEGER DEFAULT 0")
                acols = [r["name"] for r in db.fetchall("PRAGMA table_info(app_sessions)")]
                if "expires_at" not in acols:
                    db.execute("ALTER TABLE app_sessions ADD COLUMN expires_at INTEGER")
        except Exception as e:
            logger.debug(f"Migration for new columns: {e}")

        # H6: One-time idempotent backfill for user_courses.is_seeded so existing
        # courses that already have seen_content rows do not flood notifications on upgrade.
        seeded_migration = "user_courses_is_seeded_backfill_v1"
        if not db.fetchone("SELECT version FROM schema_migrations WHERE version=?", (seeded_migration,)):
            db.execute("""
                UPDATE user_courses
                SET is_seeded = 1
                WHERE (is_seeded IS NULL OR is_seeded = 0)
                  AND EXISTS (
                      SELECT 1 FROM seen_content sc
                      WHERE sc.telegram_id = user_courses.telegram_id
                        AND sc.course_id = user_courses.course_id
                  )
            """)
            db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (seeded_migration,))

        # H2: Backfill default TTL on any legacy app_sessions rows missing expires_at
        default_exp = int(time.time()) + DEFAULT_SESSION_TTL_SECONDS
        db.execute(
            "UPDATE app_sessions SET expires_at=? WHERE expires_at IS NULL",
            (default_exp,),
        )

        # H3: Idempotent indexes for hot lookup paths
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_content_user_course ON seen_content (telegram_id, course_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_user_due ON reminders (telegram_id, due_ts)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_courses_user_course ON user_courses (telegram_id, course_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_app_sessions_user ON app_sessions (telegram_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pair_codes_expires ON pair_codes (expires_at)"
        )

        # Idempotent migration of stored user passwords to enc:v1: format
        migrate_user_passwords(db)

    logger.info("DB initialised")


def migrate_user_passwords(db: DBContext) -> None:
    """Migrate legacy encrypted and plaintext user passwords to enc:v1: format idempotently."""
    rows = db.fetchall("SELECT telegram_id, lms_password FROM users")
    for row in rows:
        telegram_id = row.get("telegram_id")
        stored_pwd = row.get("lms_password") or ""
        if not stored_pwd or is_encrypted_v1(stored_pwd):
            continue

        if is_legacy_fernet_token(stored_pwd):
            try:
                plain_pwd = decrypt_legacy_password(stored_pwd)
            except RuntimeError:
                logger.error(
                    "Password migration skipped for telegram_id=%s: unable to decrypt legacy token; row left untouched.",
                    telegram_id,
                )
                continue
        else:
            # Legacy plaintext password without enc:v1: prefix
            plain_pwd = stored_pwd

        new_ciphertext = encrypt_password(plain_pwd)
        db.execute(
            "UPDATE users SET lms_password=? WHERE telegram_id=?",
            (new_ciphertext, telegram_id),
        )


def save_user(telegram_id, lms_username, lms_password,
              lms_user_id=None, full_name=None):
    enc_password = encrypt_password(lms_password)
    with DBContext() as db:
        db.execute("""
            INSERT INTO users (telegram_id, lms_username, lms_password,
                               lms_user_id, full_name, check_interval)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                lms_username = excluded.lms_username,
                lms_password = excluded.lms_password,
                lms_user_id  = excluded.lms_user_id,
                full_name    = excluded.full_name
        """, (telegram_id, lms_username, enc_password, lms_user_id, full_name,
              CHECK_INTERVAL_MINUTES))


def get_user(telegram_id):
    with DBContext() as db:
        user = db.fetchone("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
        if user and "lms_password" in user:
            user["lms_password"] = decrypt_password(user["lms_password"])
        return user


def get_all_monitoring_users():
    with DBContext() as db:
        users = db.fetchall("SELECT * FROM users WHERE is_monitoring=1")
        for user in users:
            if "lms_password" in user:
                user["lms_password"] = decrypt_password(user["lms_password"])
        return users


def set_monitoring(telegram_id, status: bool):
    with DBContext() as db:
        db.execute("UPDATE users SET is_monitoring=? WHERE telegram_id=?",
                   (1 if status else 0, telegram_id))


def set_user_notifications_enabled(telegram_id, enabled: bool) -> None:
    """Enable or disable all notifications for a user (used on Telegram Forbidden).

    When `enabled=False`, also disables `is_monitoring` so background scans and
    reminders stop attempting to message a user who blocked the bot.
    """
    flag = 1 if enabled else 0
    with DBContext() as db:
        if enabled:
            db.execute(
                """UPDATE users
                   SET notify_assign=?,
                       notify_quiz=?,
                       notify_resource=?,
                       notify_grade=?,
                       notify_announcement=?,
                       notify_attendance=?
                   WHERE telegram_id=?""",
                (flag, flag, flag, flag, flag, flag, telegram_id),
            )
        else:
            db.execute(
                """UPDATE users
                   SET is_monitoring=0,
                       notify_assign=0,
                       notify_quiz=0,
                       notify_resource=0,
                       notify_grade=0,
                       notify_announcement=0,
                       notify_attendance=0
                   WHERE telegram_id=?""",
                (telegram_id,),
            )


def update_notify_settings(telegram_id, assign=None, quiz=None, resource=None,
                           grade=None, announcement=None, attendance=None):
    with DBContext() as db:
        if assign is not None:
            db.execute("UPDATE users SET notify_assign=? WHERE telegram_id=?", (int(assign), telegram_id))
        if quiz is not None:
            db.execute("UPDATE users SET notify_quiz=? WHERE telegram_id=?", (int(quiz), telegram_id))
        if resource is not None:
            db.execute("UPDATE users SET notify_resource=? WHERE telegram_id=?", (int(resource), telegram_id))
        if grade is not None:
            db.execute("UPDATE users SET notify_grade=? WHERE telegram_id=?", (int(grade), telegram_id))
        if announcement is not None:
            db.execute("UPDATE users SET notify_announcement=? WHERE telegram_id=?", (int(announcement), telegram_id))
        if attendance is not None:
            db.execute("UPDATE users SET notify_attendance=? WHERE telegram_id=?", (int(attendance), telegram_id))


def update_check_interval(telegram_id, minutes: int):
    with DBContext() as db:
        db.execute("UPDATE users SET check_interval=? WHERE telegram_id=?",
                   (minutes, telegram_id))


def utc_now_iso() -> str:
    """Return current UTC timestamp in explicit ISO-8601 format (ending in 'Z')."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def update_last_check(telegram_id, checked_at: datetime | str | None = None) -> str:
    """Persist `users.last_check` in explicit UTC ISO-8601 format (e.g. '2026-11-26T22:30:00Z')."""
    if isinstance(checked_at, datetime):
        dt = (
            checked_at.astimezone(timezone.utc)
            if checked_at.tzinfo is not None
            else checked_at.replace(tzinfo=timezone.utc)
        )
        now_str = dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    elif isinstance(checked_at, str) and checked_at.strip():
        raw = checked_at.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        dt = (
            parsed.astimezone(timezone.utc)
            if parsed.tzinfo is not None
            else parsed.replace(tzinfo=timezone.utc)
        )
        now_str = dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        now_str = utc_now_iso()
    with DBContext() as db:
        db.execute(
            "UPDATE users SET last_check=? WHERE telegram_id=?",
            (now_str, telegram_id),
        )
    return now_str


def _parse_user_meta(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def get_user_meta(telegram_id) -> dict:
    with DBContext() as db:
        row = db.fetchone("SELECT user_meta FROM users WHERE telegram_id=?", (telegram_id,))
        if not row or not row.get("user_meta"):
            return {}
        return _parse_user_meta(row.get("user_meta"))


def merge_user_meta(telegram_id, patch: dict) -> dict:
    """Atomically merge `patch` into `users.user_meta` without lost updates.

    - PostgreSQL: uses atomic JSONB `||` (`COALESCE(NULLIF(user_meta,'')::jsonb, '{}'::jsonb) || %s::jsonb`),
      with single-statement `jsonb_set` + `||` for nested `notes`/`course_meta` dicts.
    - SQLite: serialized via `_sqlite_meta_lock` + `BEGIN IMMEDIATE` transaction.
    - Explicit empty lists (e.g. `{"personal_reminders": []}`) overwrite cleanly.
    """
    if not isinstance(patch, dict) or not patch:
        return get_user_meta(telegram_id)

    nested_keys = ("notes", "course_meta")
    nested_patches = {
        k: v for k, v in patch.items() if k in nested_keys and isinstance(v, dict)
    }
    top_patch = {k: v for k, v in patch.items() if k not in nested_patches}

    if _get_pg_url() and psycopg is not None and ConnectionPool is not None:
        with DBContext() as db:
            if not nested_patches:
                row = db.fetchone(
                    """UPDATE users
                       SET user_meta = (
                           COALESCE(NULLIF(user_meta, '')::jsonb, '{}'::jsonb) || %s::jsonb
                       )::text
                       WHERE telegram_id = %s
                       RETURNING user_meta""",
                    (json.dumps(top_patch, ensure_ascii=False), telegram_id),
                )
            else:
                expr = "base.m"
                params: list[Any] = []
                for nk in nested_keys:
                    if nk in nested_patches:
                        expr = (
                            f"jsonb_set({expr}, '{{{nk}}}', "
                            f"COALESCE(CASE WHEN jsonb_typeof(({expr})->'{nk}') = 'object' "
                            f"THEN ({expr})->'{nk}' ELSE '{{}}'::jsonb END, '{{}}'::jsonb) || %s::jsonb, true)"
                        )
                        params.append(json.dumps(nested_patches[nk], ensure_ascii=False))
                params.append(json.dumps(top_patch, ensure_ascii=False))
                params.append(telegram_id)
                row = db.fetchone(
                    f"""UPDATE users
                        SET user_meta = (
                            SELECT ({expr})::text
                            FROM (
                                SELECT (
                                    COALESCE(NULLIF(users.user_meta, '')::jsonb, '{{}}'::jsonb) || %s::jsonb
                                ) AS m
                            ) AS base
                        )
                        WHERE telegram_id = %s
                        RETURNING user_meta""",
                    tuple(params),
                )
            return _parse_user_meta((row or {}).get("user_meta"))

    with _sqlite_meta_lock:
        with DBContext(immediate=True) as db:
            row = db.fetchone("SELECT user_meta FROM users WHERE telegram_id=?", (telegram_id,))
            current = _parse_user_meta((row or {}).get("user_meta"))
            for k, v in patch.items():
                if k in nested_keys and isinstance(v, dict) and isinstance(current.get(k), dict):
                    merged = dict(current[k])
                    merged.update(v)
                    current[k] = merged
                else:
                    current[k] = v
            db.execute(
                "UPDATE users SET user_meta=? WHERE telegram_id=?",
                (json.dumps(current, ensure_ascii=False), telegram_id),
            )
            return current


def update_user_meta(telegram_id, patch: dict) -> dict:
    """Backward-compatible alias for atomic `merge_user_meta`."""
    return merge_user_meta(telegram_id, patch)


def append_sync_log(telegram_id, status: str, detail: str):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = {"time": now_str, "status": status, "detail": detail}
    if _get_pg_url() and psycopg is not None and ConnectionPool is not None:
        with DBContext() as db:
            row = db.fetchone(
                "SELECT user_meta FROM users WHERE telegram_id=%s FOR UPDATE",
                (telegram_id,),
            )
            current = _parse_user_meta((row or {}).get("user_meta"))
            logs = current.get("sync_logs")
            if not isinstance(logs, list):
                logs = []
            logs.insert(0, entry)
            logs = logs[:15]
            db.execute(
                """UPDATE users
                   SET user_meta = (
                       COALESCE(NULLIF(user_meta, '')::jsonb, '{}'::jsonb) || %s::jsonb
                   )::text
                   WHERE telegram_id = %s""",
                (json.dumps({"sync_logs": logs}, ensure_ascii=False), telegram_id),
            )
        return

    with _sqlite_meta_lock:
        with DBContext(immediate=True) as db:
            row = db.fetchone("SELECT user_meta FROM users WHERE telegram_id=?", (telegram_id,))
            current = _parse_user_meta((row or {}).get("user_meta"))
            logs = current.get("sync_logs")
            if not isinstance(logs, list):
                logs = []
            logs.insert(0, entry)
            current["sync_logs"] = logs[:15]
            db.execute(
                "UPDATE users SET user_meta=? WHERE telegram_id=?",
                (json.dumps(current, ensure_ascii=False), telegram_id),
            )


# ── Phone-app device sessions (with TTL) ─────────────────────────────────
def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create_app_session(
    telegram_id,
    device: str = "",
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> str:
    """Create a device session with TTL (default 30 days) and return the raw token."""
    token = secrets.token_urlsafe(32)
    now_ts = int(time.time())
    expires_at = now_ts + int(ttl_seconds)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with DBContext() as db:
        db.execute(
            """INSERT INTO app_sessions (token_hash, telegram_id, device, created_at, last_used, expires_at)
               VALUES (?,?,?,?,?,?)""",
            (_hash_token(token), int(telegram_id), (device or "")[:120], now_str, now_str, expires_at),
        )
    return token


def get_app_session(token: str) -> Optional[dict]:
    """Return the active session dict for a valid non-expired token, else None.

    Expired tokens are deleted automatically on access.
    """
    if not token or len(token) < 20:
        return None
    th = _hash_token(token)
    now_ts = int(time.time())
    with DBContext() as db:
        row = db.fetchone(
            "SELECT telegram_id, device, created_at, last_used, expires_at FROM app_sessions WHERE token_hash=?",
            (th,),
        )
        if not row:
            return None

        exp = row.get("expires_at")
        if exp is None or int(exp) <= now_ts:
            db.execute("DELETE FROM app_sessions WHERE token_hash=?", (th,))
            return None

        now = datetime.now()
        try:
            last = datetime.strptime(str(row.get("last_used") or ""), "%Y-%m-%d %H:%M:%S")
            stale = (now - last).total_seconds() > 600
        except Exception:
            stale = True
        if stale:
            last_used_str = now.strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "UPDATE app_sessions SET last_used=? WHERE token_hash=?",
                (last_used_str, th),
            )
            row["last_used"] = last_used_str

        return {
            "telegram_id": int(row["telegram_id"]),
            "device": row.get("device") or "",
            "created_at": row.get("created_at") or "",
            "last_used": row.get("last_used") or "",
            "expires_at": int(exp),
        }


def verify_app_token(token: str) -> Optional[int]:
    """Return the telegram_id bound to a valid non-expired device token, else None."""
    sess = get_app_session(token)
    if not sess:
        return None
    return int(sess["telegram_id"])


def revoke_app_token(token: str):
    with DBContext() as db:
        db.execute("DELETE FROM app_sessions WHERE token_hash=?", (_hash_token(token),))


def revoke_all_app_sessions(telegram_id) -> int:
    with DBContext() as db:
        n = db.fetchone("SELECT COUNT(*) AS n FROM app_sessions WHERE telegram_id=?", (int(telegram_id),))
        db.execute("DELETE FROM app_sessions WHERE telegram_id=?", (int(telegram_id),))
        return int((n or {}).get("n") or 0)


def list_app_sessions(telegram_id):
    now_ts = int(time.time())
    with DBContext() as db:
        db.execute(
            "DELETE FROM app_sessions WHERE telegram_id=? AND expires_at IS NOT NULL AND expires_at <= ?",
            (int(telegram_id), now_ts),
        )
        return db.fetchall(
            """SELECT device, created_at, last_used, expires_at
               FROM app_sessions
               WHERE telegram_id=?
               ORDER BY created_at DESC""",
            (int(telegram_id),),
        )


# ── DB-backed pairing codes (survive Render restarts) ────────────────────
def save_pair_code(code: str, telegram_id: int, expires_at: int) -> None:
    """Persist a one-time pairing code with its expiration epoch timestamp."""
    now_ts = int(time.time())
    with DBContext() as db:
        db.execute(
            """INSERT INTO pair_codes (code, telegram_id, expires_at, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                   telegram_id = excluded.telegram_id,
                   expires_at  = excluded.expires_at,
                   created_at  = excluded.created_at""",
            (str(code), int(telegram_id), int(expires_at), now_ts),
        )


def consume_pair_code(code: str) -> Optional[int]:
    """Atomically check and delete a one-time pairing code.

    Returns the associated `telegram_id` if present and unexpired, else `None`.
    """
    code_str = str(code or "").strip()
    if not code_str:
        return None
    now_ts = int(time.time())

    if _get_pg_url() and psycopg is not None and ConnectionPool is not None:
        with DBContext() as db:
            row = db.fetchone(
                "DELETE FROM pair_codes WHERE code=%s RETURNING telegram_id, expires_at",
                (code_str,),
            )
            if not row:
                return None
            if int(row["expires_at"]) <= now_ts:
                return None
            return int(row["telegram_id"])

    with _sqlite_pair_lock:
        with DBContext(immediate=True) as db:
            row = db.fetchone(
                "SELECT telegram_id, expires_at FROM pair_codes WHERE code=?",
                (code_str,),
            )
            if not row:
                return None
            db.execute("DELETE FROM pair_codes WHERE code=?", (code_str,))
            if int(row["expires_at"]) <= now_ts:
                return None
            return int(row["telegram_id"])


def cleanup_pair_codes(now_ts: Optional[int] = None) -> int:
    """Delete all expired pairing codes and return the number of rows removed."""
    cutoff = int(time.time()) if now_ts is None else int(now_ts)
    with DBContext() as db:
        row = db.fetchone("SELECT COUNT(*) AS n FROM pair_codes WHERE expires_at <= ?", (cutoff,))
        count = int((row or {}).get("n") or 0)
        if count:
            db.execute("DELETE FROM pair_codes WHERE expires_at <= ?", (cutoff,))
        return count


def delete_user(telegram_id):
    """Atomically delete a user and all dependent records across every table."""
    with DBContext() as db:
        db.execute("DELETE FROM app_sessions         WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM pair_codes           WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM course_attendance    WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM course_grades        WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM course_announcements WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM seen_content         WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM reminders            WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM user_courses         WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM users                WHERE telegram_id=?", (telegram_id,))


def add_course(telegram_id, course_id, course_name, course_url=""):
    with DBContext() as db:
        db.execute("""
            INSERT INTO user_courses (telegram_id, course_id, course_name, course_url)
            VALUES (?,?,?,?)
            ON CONFLICT(telegram_id, course_id) DO UPDATE SET
                course_name = excluded.course_name,
                course_url  = excluded.course_url
        """, (telegram_id, course_id, course_name, course_url))


def is_course_seeded(telegram_id, course_id) -> bool:
    """Return True if the course has already completed initial seeding."""
    with DBContext() as db:
        row = db.fetchone(
            "SELECT is_seeded FROM user_courses WHERE telegram_id=? AND course_id=?",
            (telegram_id, course_id),
        )
        return bool(row and row.get("is_seeded"))


def mark_course_seeded(telegram_id, course_id, seeded: bool = True) -> None:
    """Mark a course's initial seeding flag (`user_courses.is_seeded`)."""
    with DBContext() as db:
        db.execute(
            "UPDATE user_courses SET is_seeded=? WHERE telegram_id=? AND course_id=?",
            (1 if seeded else 0, telegram_id, course_id),
        )


def remove_course(telegram_id, course_id):
    with DBContext() as db:
        db.execute("DELETE FROM course_attendance    WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))
        db.execute("DELETE FROM course_grades        WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))
        db.execute("DELETE FROM course_announcements WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))
        db.execute("DELETE FROM user_courses         WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))
        db.execute("DELETE FROM seen_content         WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))
        db.execute("DELETE FROM reminders            WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))


def get_user_courses(telegram_id, monitored_only=False):
    with DBContext() as db:
        if monitored_only:
            return db.fetchall(
                "SELECT * FROM user_courses WHERE telegram_id=? AND is_monitored=1 ORDER BY added_at",
                (telegram_id,)
            )
        else:
            return db.fetchall(
                "SELECT * FROM user_courses WHERE telegram_id=? ORDER BY added_at",
                (telegram_id,)
            )


def get_course(telegram_id, course_id):
    with DBContext() as db:
        return db.fetchone(
            "SELECT * FROM user_courses WHERE telegram_id=? AND course_id=?",
            (telegram_id, course_id)
        )


def get_course_items(telegram_id, course_id):
    with DBContext() as db:
        return db.fetchall(
            "SELECT * FROM seen_content WHERE telegram_id=? AND course_id=? ORDER BY id DESC",
            (telegram_id, course_id)
        )


def get_all_seen_content(telegram_id) -> dict[str, list[dict]]:
    """Fetch all `seen_content` rows for a user in a single SQL query grouped by `str(course_id)`."""
    with DBContext() as db:
        rows = db.fetchall(
            "SELECT * FROM seen_content WHERE telegram_id=? ORDER BY course_id ASC, id DESC",
            (telegram_id,),
        )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row["course_id"])
        grouped.setdefault(key, []).append(row)
    return grouped


def toggle_course(telegram_id, course_id):
    with DBContext() as db:
        db.execute("""
            UPDATE user_courses
            SET is_monitored = 1 - is_monitored
            WHERE telegram_id=? AND course_id=?
        """, (telegram_id, course_id))


def is_seen(telegram_id, content_id, content_type) -> bool:
    with DBContext() as db:
        row = db.fetchone(
            "SELECT id FROM seen_content WHERE telegram_id=? AND content_id=? AND content_type=?",
            (telegram_id, str(content_id), content_type)
        )
        return row is not None


def get_seen_content(telegram_id, content_id, content_type):
    with DBContext() as db:
        return db.fetchone(
            """SELECT * FROM seen_content
               WHERE telegram_id=? AND content_id=? AND content_type=?""",
            (telegram_id, str(content_id), content_type)
        )


def delete_seen(telegram_id, content_id, content_type):
    with DBContext() as db:
        db.execute(
            "DELETE FROM seen_content WHERE telegram_id=? AND content_id=? AND content_type=?",
            (telegram_id, str(content_id), content_type)
        )


def update_seen_content(telegram_id, content_id, content_type,
                        name=None, url=None, due=None, is_submitted=None):
    with DBContext() as db:
        if is_submitted is not None:
            db.execute(
                """UPDATE seen_content
                   SET content_name=?,
                       content_url=?,
                       due_date=CASE WHEN ? <> '' THEN ? ELSE due_date END,
                       is_submitted=CASE WHEN ? = 1 THEN 1 ELSE is_submitted END
                   WHERE telegram_id=? AND content_id=? AND content_type=?""",
                (name, url, due or "", due or "", 1 if is_submitted else 0, telegram_id, str(content_id), content_type)
            )
        else:
            db.execute(
                """UPDATE seen_content
                   SET content_name=?,
                       content_url=?,
                       due_date=CASE WHEN ? <> '' THEN ? ELSE due_date END
                   WHERE telegram_id=? AND content_id=? AND content_type=?""",
                (name, url, due or "", due or "", telegram_id, str(content_id), content_type)
            )


def mark_seen(telegram_id, course_id, content_id, content_type,
              name=None, url=None, due=None, is_submitted=0):
    val = 1 if is_submitted else 0
    with DBContext() as db:
        db.execute("""
            INSERT INTO seen_content
            (telegram_id, course_id, content_id, content_type,
             content_name, content_url, due_date, is_submitted)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, content_id, content_type) DO UPDATE SET
                due_date = CASE WHEN excluded.due_date <> '' THEN excluded.due_date ELSE seen_content.due_date END,
                is_submitted = CASE WHEN excluded.is_submitted = 1 THEN 1 ELSE seen_content.is_submitted END
        """, (telegram_id, course_id, str(content_id), content_type,
              name, url, due, val))
        if val == 1:
            cid_str = str(content_id)
            raw_num = cid_str.replace("assign_", "").replace("quiz_", "")
            db.execute(
                "UPDATE reminders SET is_submitted=1 WHERE telegram_id=? AND (content_id=? OR content_id=? OR content_id=?)",
                (telegram_id, cid_str, f"assign_{raw_num}", f"quiz_{raw_num}")
            )


def mark_assignment_submitted(telegram_id, content_id, is_submitted=1):
    val = 1 if is_submitted else 0
    cid_str = str(content_id)
    raw_num = cid_str.replace("assign_", "").replace("quiz_", "")
    with DBContext() as db:
        db.execute(
            """UPDATE reminders
               SET is_submitted=?
               WHERE telegram_id=? AND (content_id=? OR content_id=? OR content_id=? OR content_id=?)""",
            (val, telegram_id, cid_str, raw_num, f"assign_{raw_num}", f"quiz_{raw_num}"),
        )
        db.execute(
            """UPDATE seen_content
               SET is_submitted=?
               WHERE telegram_id=? AND (content_id=? OR content_id=? OR content_id=? OR content_id=?)""",
            (val, telegram_id, cid_str, raw_num, f"assign_{raw_num}", f"quiz_{raw_num}"),
        )


def mark_item_submitted(
    telegram_id,
    course_id,
    item_key,
    title: str = "",
    url: str = "",
    item_type: str = "assignment",
    is_submitted: int | bool = 1,
) -> dict:
    """Atomically update submission state in BOTH `seen_content` and `reminders`.

    Supports toggling both done (`is_submitted=1`) and undone (`is_submitted=0` /
    `toggleDone(0)`), including items not yet present in `reminders` or `seen_content`.
    """
    val = 1 if is_submitted else 0
    cid_str = str(item_key).strip()
    raw_num = cid_str.replace("assign_", "").replace("quiz_", "")
    norm_type = str(item_type or "assign").strip().lower()
    if norm_type in ("assignment", "assign"):
        norm_type = "assign"
    elif norm_type == "quiz" or cid_str.startswith("quiz_"):
        norm_type = "quiz"

    match_params = (telegram_id, cid_str, raw_num, f"assign_{raw_num}", f"quiz_{raw_num}")

    with DBContext() as db:
        existing_seen = db.fetchone(
            """SELECT course_id, content_name, content_url, due_date, content_type
               FROM seen_content
               WHERE telegram_id=? AND (content_id=? OR content_id=? OR content_id=? OR content_id=?)
               LIMIT 1""",
            match_params,
        )
        existing_rem = db.fetchone(
            """SELECT course_id, content_name, content_url, due_str
               FROM reminders
               WHERE telegram_id=? AND (content_id=? OR content_id=? OR content_id=? OR content_id=?)
               LIMIT 1""",
            match_params,
        )

        resolved_course_id = course_id
        if resolved_course_id is None:
            if existing_seen and existing_seen.get("course_id") is not None:
                resolved_course_id = existing_seen["course_id"]
            elif existing_rem and existing_rem.get("course_id") is not None:
                resolved_course_id = existing_rem["course_id"]

        resolved_title = title or (existing_seen or {}).get("content_name") or (existing_rem or {}).get("content_name") or ""
        resolved_url = url or (existing_seen or {}).get("content_url") or (existing_rem or {}).get("content_url") or ""
        resolved_due = (existing_seen or {}).get("due_date") or (existing_rem or {}).get("due_str") or ""

        seen_updated = False
        if existing_seen:
            db.execute(
                """UPDATE seen_content
                   SET is_submitted=?
                   WHERE telegram_id=? AND (content_id=? OR content_id=? OR content_id=? OR content_id=?)""",
                (val, *match_params),
            )
            seen_updated = True
        elif resolved_course_id is not None:
            db.execute(
                """INSERT INTO seen_content
                   (telegram_id, course_id, content_id, content_type,
                    content_name, content_url, due_date, is_submitted)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(telegram_id, content_id, content_type) DO UPDATE SET
                       is_submitted = excluded.is_submitted""",
                (
                    telegram_id,
                    int(resolved_course_id),
                    cid_str,
                    norm_type,
                    resolved_title,
                    resolved_url,
                    resolved_due,
                    val,
                ),
            )
            seen_updated = True

        reminder_updated = False
        if existing_rem:
            db.execute(
                """UPDATE reminders
                   SET is_submitted=?
                   WHERE telegram_id=? AND (content_id=? OR content_id=? OR content_id=? OR content_id=?)""",
                (val, *match_params),
            )
            reminder_updated = True
        elif resolved_course_id is not None:
            db.execute(
                """INSERT INTO reminders
                   (telegram_id, course_id, content_id, content_name,
                    content_url, due_ts, due_str, is_submitted)
                   VALUES (?,?,?,?,?,NULL,?,?)
                   ON CONFLICT(telegram_id, content_id) DO UPDATE SET
                       is_submitted = excluded.is_submitted""",
                (
                    telegram_id,
                    int(resolved_course_id),
                    cid_str,
                    resolved_title,
                    resolved_url,
                    resolved_due,
                    val,
                ),
            )
            reminder_updated = True

    return {
        "telegram_id": int(telegram_id),
        "course_id": int(resolved_course_id) if resolved_course_id is not None else None,
        "item_key": cid_str,
        "item_type": norm_type,
        "is_submitted": bool(val),
        "seen_updated": seen_updated,
        "reminder_updated": reminder_updated,
    }


def clear_all_user_courses(telegram_id):
    with DBContext() as db:
        db.execute("DELETE FROM course_attendance WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM reminders WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM course_grades WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM course_announcements WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM seen_content WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM user_courses WHERE telegram_id=?", (telegram_id,))


def get_stats(telegram_id) -> dict:
    with DBContext() as db:
        row = db.fetchone("SELECT COUNT(*) as total FROM seen_content WHERE telegram_id=?", (telegram_id,))
        total = row["total"] if row else 0
        rows = db.fetchall(
            "SELECT content_type, COUNT(*) as n FROM seen_content WHERE telegram_id=? GROUP BY content_type",
            (telegram_id,)
        )
        by_type = {r["content_type"]: r["n"] for r in rows}
        return {"total": total, "by_type": by_type}


def upsert_reminder(telegram_id, course_id, content_id, content_name,
                    content_url, due_ts, due_str, is_submitted=0):
    with DBContext() as db:
        db.execute("""
            INSERT INTO reminders
                (telegram_id, course_id, content_id, content_name,
                 content_url, due_ts, due_str, is_submitted)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, content_id) DO UPDATE SET
                content_name = excluded.content_name,
                content_url  = excluded.content_url,
                reminded_24h = CASE WHEN reminders.due_ts IS NULL OR reminders.due_ts <> excluded.due_ts THEN 0 ELSE reminders.reminded_24h END,
                reminded_3h  = CASE WHEN reminders.due_ts IS NULL OR reminders.due_ts <> excluded.due_ts THEN 0 ELSE reminders.reminded_3h END,
                reminded_1h  = CASE WHEN reminders.due_ts IS NULL OR reminders.due_ts <> excluded.due_ts THEN 0 ELSE reminders.reminded_1h END,
                due_ts       = excluded.due_ts,
                due_str      = excluded.due_str,
                is_submitted = CASE WHEN excluded.is_submitted = 1 THEN 1 ELSE reminders.is_submitted END
        """, (telegram_id, course_id, str(content_id), content_name,
              content_url, due_ts, due_str, 1 if is_submitted else 0))


def delete_reminder(telegram_id, content_id):
    with DBContext() as db:
        db.execute(
            "DELETE FROM reminders WHERE telegram_id=? AND content_id=?",
            (telegram_id, str(content_id))
        )


def get_pending_reminders(now_ts: int) -> list:
    with DBContext() as db:
        return db.fetchall(r"""
            SELECT r.*, u.lms_username, u.notify_assign, u.notify_quiz
            FROM reminders r
            JOIN users u ON r.telegram_id = u.telegram_id
            WHERE u.is_monitoring = 1
              AND (
                  (r.content_id LIKE 'quiz\_%' ESCAPE '\' AND u.notify_quiz = 1)
                  OR (r.content_id NOT LIKE 'quiz\_%' ESCAPE '\' AND u.notify_assign = 1)
              )
              AND (r.is_submitted IS NULL OR r.is_submitted = 0)
              AND r.due_ts IS NOT NULL
              AND r.due_ts > ?
              AND (
                  (r.reminded_24h = 0 AND r.due_ts - ? <= 86400 AND r.due_ts - ? > 10800) OR
                  (r.reminded_3h  = 0 AND r.due_ts - ? <= 10800 AND r.due_ts - ? > 3600)  OR
                  (r.reminded_1h  = 0 AND r.due_ts - ? <= 3600)
              )
        """, (now_ts, now_ts, now_ts, now_ts, now_ts, now_ts))


def mark_reminded(reminder_id: int, field: str):
    if field not in ("reminded_24h", "reminded_3h", "reminded_1h"):
        return
    with DBContext() as db:
        db.execute(f"UPDATE reminders SET {field}=1 WHERE id=?", (reminder_id,))


def delete_reminders_for_course(telegram_id, course_id):
    with DBContext() as db:
        db.execute(
            "DELETE FROM reminders WHERE telegram_id=? AND course_id=?",
            (telegram_id, course_id)
        )


def delete_all_reminders(telegram_id):
    with DBContext() as db:
        db.execute("DELETE FROM reminders WHERE telegram_id=?", (telegram_id,))


def get_upcoming_deadlines(telegram_id, limit=10) -> list:
    now = int(time.time())
    with DBContext() as db:
        return db.fetchall("""
            SELECT r.*, uc.course_name
            FROM reminders r
            JOIN user_courses uc
              ON r.telegram_id = uc.telegram_id AND r.course_id = uc.course_id
            WHERE r.telegram_id = ?
              AND r.due_ts > ?
            ORDER BY r.due_ts ASC
            LIMIT ?
        """, (telegram_id, now, limit))


# ==================== GRADES & ANNOUNCEMENTS ====================

def get_grade(telegram_id, course_id, item_id):
    with DBContext() as db:
        return db.fetchone(
            "SELECT * FROM course_grades WHERE telegram_id=? AND course_id=? AND item_id=?",
            (telegram_id, course_id, str(item_id))
        )


def upsert_grade(telegram_id, course_id, item_id, item_name, grade_val,
                 grade_max="", percentage="", feedback="", item_url=""):
    with DBContext() as db:
        db.execute("""
            INSERT INTO course_grades
                (telegram_id, course_id, item_id, item_name,
                 grade_val, grade_max, percentage, feedback, item_url)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, course_id, item_id) DO UPDATE SET
                item_name  = excluded.item_name,
                grade_val  = excluded.grade_val,
                grade_max  = excluded.grade_max,
                percentage = excluded.percentage,
                feedback   = excluded.feedback,
                item_url   = excluded.item_url
        """, (telegram_id, course_id, str(item_id), item_name,
              str(grade_val), str(grade_max), str(percentage),
              str(feedback), str(item_url)))


def get_user_grades(telegram_id, course_id=None) -> list:
    with DBContext() as db:
        if course_id is not None:
            return db.fetchall("""
                SELECT g.*, uc.course_name
                FROM course_grades g
                JOIN user_courses uc
                  ON g.telegram_id = uc.telegram_id AND g.course_id = uc.course_id
                WHERE g.telegram_id = ? AND g.course_id = ?
                ORDER BY g.id ASC
            """, (telegram_id, course_id))
        return db.fetchall("""
            SELECT g.*, uc.course_name
            FROM course_grades g
            JOIN user_courses uc
              ON g.telegram_id = uc.telegram_id AND g.course_id = uc.course_id
            WHERE g.telegram_id = ?
            ORDER BY g.course_id ASC, g.id ASC
        """, (telegram_id,))


def get_announcement(telegram_id, discuss_id):
    with DBContext() as db:
        return db.fetchone(
            "SELECT * FROM course_announcements WHERE telegram_id=? AND discuss_id=?",
            (telegram_id, str(discuss_id))
        )


def upsert_announcement(telegram_id, course_id, discuss_id, forum_name,
                        title, author="", posted_date="", body_text="", url=""):
    with DBContext() as db:
        db.execute("""
            INSERT INTO course_announcements
                (telegram_id, course_id, discuss_id, forum_name,
                 title, author, posted_date, body_text, url)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, discuss_id) DO UPDATE SET
                forum_name  = excluded.forum_name,
                title       = excluded.title,
                author      = excluded.author,
                posted_date = excluded.posted_date,
                body_text   = CASE WHEN excluded.body_text <> '' THEN excluded.body_text ELSE course_announcements.body_text END,
                url         = excluded.url
        """, (telegram_id, course_id, str(discuss_id), forum_name,
              title, author, posted_date, body_text, url))


def get_user_announcements(telegram_id, course_id=None, limit=30) -> list:
    with DBContext() as db:
        if course_id is not None:
            return db.fetchall("""
                SELECT a.*, uc.course_name
                FROM course_announcements a
                JOIN user_courses uc
                  ON a.telegram_id = uc.telegram_id AND a.course_id = uc.course_id
                WHERE a.telegram_id = ? AND a.course_id = ?
                ORDER BY a.id DESC
                LIMIT ?
            """, (telegram_id, course_id, limit))
        return db.fetchall("""
            SELECT a.*, uc.course_name
            FROM course_announcements a
            JOIN user_courses uc
              ON a.telegram_id = uc.telegram_id AND a.course_id = uc.course_id
            WHERE a.telegram_id = ?
            ORDER BY a.id DESC
            LIMIT ?
        """, (telegram_id, limit))


# ==================== ATTENDANCE (الحضور والغياب) ====================

def get_attendance(telegram_id, course_id, att_id):
    with DBContext() as db:
        return db.fetchone(
            "SELECT * FROM course_attendance WHERE telegram_id=? AND course_id=? AND att_id=?",
            (telegram_id, course_id, str(att_id))
        )


def upsert_attendance(telegram_id, course_id, att_id, taken_sessions=0, total_sessions=0,
                      present_count=0, absent_count=0, late_count=0, excused_count=0,
                      points_str="", percentage_str="", sessions_json="[]", att_url="", url="", **kwargs):
    final_url = str(url or att_url or "")
    with DBContext() as db:
        db.execute("""
            INSERT INTO course_attendance
                (telegram_id, course_id, att_id, taken_sessions, total_sessions,
                 present_count, absent_count, late_count, excused_count,
                 points_str, percentage_str, sessions_json, att_url)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, course_id, att_id) DO UPDATE SET
                taken_sessions = excluded.taken_sessions,
                total_sessions = excluded.total_sessions,
                present_count  = excluded.present_count,
                absent_count   = excluded.absent_count,
                late_count     = excluded.late_count,
                excused_count  = excluded.excused_count,
                points_str     = excluded.points_str,
                percentage_str = excluded.percentage_str,
                sessions_json  = excluded.sessions_json,
                att_url        = excluded.att_url
        """, (telegram_id, course_id, str(att_id), int(taken_sessions or 0), int(total_sessions or 0),
              int(present_count or 0), int(absent_count or 0), int(late_count or 0), int(excused_count or 0),
              str(points_str or ""), str(percentage_str or ""), str(sessions_json or "[]"), final_url))


def get_user_attendance(telegram_id, course_id=None) -> list:
    with DBContext() as db:
        if course_id is not None:
            return db.fetchall("""
                SELECT att.*, uc.course_name
                FROM course_attendance att
                JOIN user_courses uc
                  ON att.telegram_id = uc.telegram_id AND att.course_id = uc.course_id
                WHERE att.telegram_id = ? AND att.course_id = ?
                ORDER BY att.id ASC
            """, (telegram_id, course_id))
        return db.fetchall("""
            SELECT att.*, uc.course_name
            FROM course_attendance att
            JOIN user_courses uc
              ON att.telegram_id = uc.telegram_id AND att.course_id = uc.course_id
            WHERE att.telegram_id = ?
            ORDER BY att.course_id ASC, att.id ASC
        """, (telegram_id,))
