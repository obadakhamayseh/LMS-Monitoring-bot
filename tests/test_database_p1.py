"""Prove-It P1 tests for database.py, requirements.txt, render.yaml, .env.example, README.md.

Covers:
- H3: psycopg_pool.ConnectionPool, autocommit=False, commit/rollback, close(),
      atomic delete_user rollback, never retrying IntegrityError, SQLite PRAGMAs
      (foreign_keys, WAL, busy_timeout), idempotent indexes, DATABASE_URL vs DIRECT_URL
      (pgbouncer prepare_threshold=None).
- H5 (DB): atomic merge_user_meta (SQLite concurrency + PG JSONB || + empty list
           personal_reminders: [] preservation), mark_item_submitted updating both
           seen_content and reminders (including toggleDone(0)).
- H6 (DB): user_courses.is_seeded migration + backfill for existing seen_content +
           is_course_seeded / mark_course_seeded helpers.
- H2 + C9 (DB): app_sessions.expires_at migration + 30d TTL + expiry enforcement/deletion,
                DB-backed pair_codes (save/consume/cleanup), set_user_notifications_enabled,
                health_check.
- H15 (DB): single-query get_all_seen_content(tid) grouped by str(course_id),
            get_pending_reminders respecting notify_quiz for quiz_% items.
- C9 (Config/Docs): requirements.txt, render.yaml, .env.example, README.md.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

_TEST_KEY = Fernet.generate_key().decode("utf-8")
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST_P1"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = _TEST_KEY

import database  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _setup_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "test_p1.db") -> Path:
    db_file = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DIRECT_URL", "")
    monkeypatch.setenv("ENCRYPTION_KEY", _TEST_KEY)
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DIRECT_URL", "")
    if hasattr(database, "close"):
        database.close()
    return db_file


# ==================== H3: SQLite PRAGMAs & Indexes ====================


def test_h3_sqlite_pragmas_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "pragmas.db")
    database.init_db()

    with database.DBContext() as ctx:
        fk = ctx.fetchone("PRAGMA foreign_keys")
        jm = ctx.fetchone("PRAGMA journal_mode")
        bt = ctx.fetchone("PRAGMA busy_timeout")

    assert fk is not None and list(fk.values())[0] == 1
    assert jm is not None and str(list(jm.values())[0]).lower() == "wal"
    assert bt is not None and int(list(bt.values())[0]) >= 5000


def test_h3_idempotent_indexes_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = _setup_sqlite(tmp_path, monkeypatch, "indexes.db")
    database.init_db()
    # Second call must remain idempotent
    database.init_db()

    conn = sqlite3.connect(str(db_file))
    try:
        idx_rows = conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'"
        ).fetchall()
    finally:
        conn.close()

    by_name = {name: (tbl, sql or "") for name, tbl, sql in idx_rows}
    assert any(
        tbl == "seen_content" and "telegram_id" in sql and "course_id" in sql
        for tbl, sql in by_name.values()
    ), f"Missing index on seen_content(telegram_id, course_id): {by_name}"
    assert any(
        tbl == "reminders" and "telegram_id" in sql and "due_ts" in sql
        for tbl, sql in by_name.values()
    ), f"Missing index on reminders(telegram_id, due_ts): {by_name}"
    assert any(
        tbl == "user_courses" and "telegram_id" in sql and "course_id" in sql
        for tbl, sql in by_name.values()
    ), f"Missing index on user_courses(telegram_id, course_id): {by_name}"


# ==================== H3 + H2: Atomic delete_user ====================


def test_h3_h2_delete_user_clears_all_tables_including_sessions_and_pair_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "delete_all.db")
    database.init_db()

    tid = 7001
    database.save_user(tid, "student1", "secret123", "u1", "Student One")
    database.add_course(tid, 101, "Algorithms")
    database.mark_seen(tid, 101, "assign_1", "assign", "HW 1", "https://lms/1", "tomorrow")
    database.upsert_reminder(tid, 101, "assign_1", "HW 1", "https://lms/1", int(time.time()) + 3600, "soon")
    database.upsert_grade(tid, 101, "g1", "Midterm", "20", "25", "80%")
    database.upsert_announcement(tid, 101, "d1", "News", "Hello")
    database.upsert_attendance(tid, 101, "att1", taken_sessions=5, present_count=5)
    token = database.create_app_session(tid, "iPhone")
    database.save_pair_code("11223344", tid, int(time.time()) + 600)

    database.delete_user(tid)

    assert database.get_user(tid) is None
    assert database.get_user_courses(tid) == []
    assert database.get_course_items(tid, 101) == []
    assert database.get_upcoming_deadlines(tid) == []
    assert database.verify_app_token(token) is None
    assert database.list_app_sessions(tid) == []
    assert database.consume_pair_code("11223344") is None


def test_h3_delete_user_is_atomic_rolls_back_on_mid_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "delete_atomic.db")
    database.init_db()

    tid = 7002
    database.save_user(tid, "student2", "secret456", "u2", "Student Two")
    database.add_course(tid, 202, "OS")
    database.mark_seen(tid, 202, "assign_2", "assign", "HW 2")
    database.upsert_reminder(tid, 202, "assign_2", "HW 2", "", int(time.time()) + 7200, "soon")
    token = database.create_app_session(tid, "Android")
    database.save_pair_code("55667788", tid, int(time.time()) + 600)

    orig_execute = database.DBContext.execute

    def _boom_on_users(self, sql, params=()):
        if "DELETE FROM users" in sql:
            raise RuntimeError("Simulated mid-transaction crash")
        return orig_execute(self, sql, params)

    monkeypatch.setattr(database.DBContext, "execute", _boom_on_users)
    with pytest.raises(RuntimeError, match="Simulated mid-transaction crash"):
        database.delete_user(tid)

    monkeypatch.setattr(database.DBContext, "execute", orig_execute)
    # Every single table must still contain the user's rows (full rollback!)
    assert database.get_user(tid) is not None
    assert len(database.get_user_courses(tid)) == 1
    assert len(database.get_course_items(tid, 202)) == 1
    assert database.verify_app_token(token) == tid
    assert database.consume_pair_code("55667788") == tid


# ==================== H3: Mocked Postgres ConnectionPool & Retry Policy ====================


class _FakeIntegrityError(Exception):
    pass


_FakeIntegrityError.__name__ = "IntegrityError"


class _FakeOperationalError(Exception):
    pass


_FakeOperationalError.__name__ = "OperationalError"


class _FakePGCursor:
    def __init__(self, conn: "_FakePGConn"):
        self.conn = conn
        self._result: Any = None

    def execute(self, sql: str, params=()):
        self.conn.executed.append((sql, params))
        if self.conn.on_execute:
            self._result = self.conn.on_execute(sql, params, self.conn)

    def fetchone(self):
        if isinstance(self._result, list):
            return self._result[0] if self._result else None
        return self._result

    def fetchall(self):
        if isinstance(self._result, list):
            return self._result
        return [self._result] if self._result is not None else []


class _FakePGConn:
    def __init__(self, on_execute=None):
        self.on_execute = on_execute
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.broken = False

    def cursor(self):
        return _FakePGCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakePoolConnCM:
    def __init__(self, pool: "_FakeConnectionPool"):
        self.pool = pool
        self.conn: _FakePGConn | None = None

    def __enter__(self) -> _FakePGConn:
        self.conn = self.pool.acquire()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.pool.released.append(self.conn)
        return False


class _FakeConnectionPool:
    created: list["_FakeConnectionPool"] = []

    def __init__(self, conninfo: str, **kwargs):
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.closed = False
        self.acquired: list[_FakePGConn] = []
        self.released: list[_FakePGConn] = []
        self.on_execute = None
        _FakeConnectionPool.created.append(self)

    def acquire(self) -> _FakePGConn:
        conn = _FakePGConn(on_execute=self.on_execute)
        self.acquired.append(conn)
        return conn

    def connection(self):
        return _FakePoolConnCM(self)

    def close(self):
        self.closed = True


def _setup_mock_pg(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str = "postgresql://u:p@aws-0-eu.pooler.supabase.com:6543/postgres?pgbouncer=true",
    direct_url: str = "postgresql://u:p@db.xxx.supabase.co:5432/postgres",
):
    _FakeConnectionPool.created.clear()
    if hasattr(database, "close"):
        database.close()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DIRECT_URL", direct_url)
    monkeypatch.setattr(database, "DATABASE_URL", database_url)
    monkeypatch.setattr(database, "DIRECT_URL", direct_url)
    monkeypatch.setattr(database, "ConnectionPool", _FakeConnectionPool)
    return _FakeConnectionPool


def test_h3_pg_pool_autocommit_false_pgbouncer_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    pool_cls = _setup_mock_pg(monkeypatch)

    with database.DBContext() as ctx:
        assert ctx.is_pg is True
        ctx.execute("SELECT ?", (1,))

    assert len(pool_cls.created) == 1
    pool = pool_cls.created[0]
    # Runtime pool must prefer DATABASE_URL (pgbouncer) and strip pgbouncer=true cleanly
    assert "pooler.supabase.com:6543" in pool.conninfo
    assert "pgbouncer=true" not in pool.conninfo
    conn_kwargs = pool.kwargs.get("kwargs", {})
    assert conn_kwargs.get("autocommit") is False
    assert conn_kwargs.get("prepare_threshold") is None
    assert len(pool.acquired) == 1
    assert pool.acquired[0].commits == 1
    assert pool.acquired[0].rollbacks == 0
    assert len(pool.released) == 1

    # Exception inside DBContext must rollback and not commit
    with pytest.raises(ValueError, match="abort"):
        with database.DBContext() as ctx:
            ctx.execute("DELETE FROM users WHERE telegram_id=?", (9,))
            raise ValueError("abort")

    assert pool.acquired[1].commits == 0
    assert pool.acquired[1].rollbacks == 1

    # close() must close the pool safely and be idempotent
    database.close()
    assert pool.closed is True
    database.close()


def test_h3_pg_never_retries_integrity_error_but_retries_transient_first_stmt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_cls = _setup_mock_pg(monkeypatch)
    attempts = {"integrity": 0, "transient": 0}

    # 1) IntegrityError must NEVER be retried
    with database.DBContext() as ctx:
        pool = pool_cls.created[0]
        assert ctx.is_pg

    def _raise_integrity(sql, params, conn):
        attempts["integrity"] += 1
        raise _FakeIntegrityError("duplicate key value violates unique constraint")

    pool.on_execute = _raise_integrity
    with pytest.raises(_FakeIntegrityError):
        with database.DBContext() as ctx:
            ctx.execute("INSERT INTO users (telegram_id) VALUES (?)", (1,))
    assert attempts["integrity"] == 1
    assert pool.acquired[-1].rollbacks >= 1

    # 2) Transient OperationalError on first statement retries once on a fresh conn
    def _transient_once(sql, params, conn):
        attempts["transient"] += 1
        if attempts["transient"] == 1:
            raise _FakeOperationalError("server closed the connection unexpectedly")
        return [{"ok": 1}]

    pool.on_execute = _transient_once
    with database.DBContext() as ctx:
        row = ctx.fetchone("SELECT 1 AS ok")
    assert row == {"ok": 1}
    assert attempts["transient"] == 2

    # 3) OperationalError on SECOND statement mid-transaction must NOT silently retry
    mid_calls = {"n": 0}

    def _fail_second_stmt(sql, params, conn):
        mid_calls["n"] += 1
        if mid_calls["n"] == 2:
            raise _FakeOperationalError("connection lost mid-tx")
        return None

    pool.on_execute = _fail_second_stmt
    with pytest.raises(_FakeOperationalError):
        with database.DBContext() as ctx:
            ctx.execute("DELETE FROM reminders WHERE telegram_id=?", (1,))
            ctx.execute("DELETE FROM users WHERE telegram_id=?", (1,))
    assert mid_calls["n"] == 2


# ==================== H5 (DB): Atomic merge_user_meta & mark_item_submitted ====================


def test_h5_merge_user_meta_sqlite_concurrent_and_empty_personal_reminders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "meta_concurrent.db")
    database.init_db()

    tid = 8001
    database.save_user(tid, "u8001", "pw8001")

    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def _worker(idx: int):
        try:
            barrier.wait()
            database.merge_user_meta(
                tid,
                {
                    f"key_{idx}": idx,
                    "notes": {f"task_{idx}": f"note_{idx}"},
                },
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    meta = database.get_user_meta(tid)
    for i in range(8):
        assert meta.get(f"key_{i}") == i
        assert meta.get("notes", {}).get(f"task_{i}") == f"note_{i}"

    # Deleting the last personal reminder (sending []) must persist [] and NOT revive old item
    database.merge_user_meta(tid, {"personal_reminders": [{"id": "r1", "text": "Study"}]})
    assert len(database.get_user_meta(tid)["personal_reminders"]) == 1

    updated = database.merge_user_meta(tid, {"personal_reminders": []})
    assert updated["personal_reminders"] == []
    assert database.get_user_meta(tid)["personal_reminders"] == []


def test_h5_merge_user_meta_postgres_uses_jsonb_concat(monkeypatch: pytest.MonkeyPatch) -> None:
    pool_cls = _setup_mock_pg(monkeypatch)
    captured_sql: list[str] = []

    with database.DBContext():
        pool = pool_cls.created[0]

    def _on_exec(sql, params, conn):
        captured_sql.append(sql)
        return {"user_meta": json.dumps({"a": 1, "b": 2})}

    pool.on_execute = _on_exec
    result = database.merge_user_meta(9001, {"b": 2})

    assert result == {"a": 1, "b": 2}
    assert any("::jsonb" in s and "||" in s for s in captured_sql), f"Expected JSONB || in SQL: {captured_sql}"


def test_h5_mark_item_submitted_updates_seen_content_and_reminders_and_toggle_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "mark_submitted.db")
    database.init_db()

    tid = 8002
    database.save_user(tid, "u8002", "pw8002")
    database.add_course(tid, 501, "Networks")
    # Item exists in seen_content ONLY (not yet in reminders)
    database.mark_seen(tid, 501, "assign_42", "assign", "Lab 1", "https://lms/mod/assign/view.php?id=42", "")

    res1 = database.mark_item_submitted(
        tid, 501, "assign_42", title="Lab 1", url="https://lms/mod/assign/view.php?id=42",
        item_type="assignment", is_submitted=1,
    )
    assert isinstance(res1, dict)
    assert res1["is_submitted"] is True

    sc = database.get_seen_content(tid, "assign_42", "assign")
    assert sc is not None and sc["is_submitted"] == 1
    with database.DBContext() as ctx:
        rem = ctx.fetchone("SELECT * FROM reminders WHERE telegram_id=? AND content_id=?", (tid, "assign_42"))
    assert rem is not None and rem["is_submitted"] == 1

    # toggleDone(0) must unmark BOTH seen_content and reminders cleanly
    res0 = database.mark_item_submitted(
        tid, 501, "assign_42", title="Lab 1", url="https://lms/mod/assign/view.php?id=42",
        item_type="assignment", is_submitted=0,
    )
    assert res0["is_submitted"] is False
    sc_after = database.get_seen_content(tid, "assign_42", "assign")
    assert sc_after is not None and sc_after["is_submitted"] == 0
    with database.DBContext() as ctx:
        rem_after = ctx.fetchone("SELECT * FROM reminders WHERE telegram_id=? AND content_id=?", (tid, "assign_42"))
    assert rem_after is not None and rem_after["is_submitted"] == 0

    # Brand new item not yet in either table
    res_new = database.mark_item_submitted(
        tid, 501, "quiz_77", title="Quiz 1", url="https://lms/mod/quiz/view.php?id=77",
        item_type="quiz", is_submitted=1,
    )
    assert res_new["is_submitted"] is True
    assert database.get_seen_content(tid, "quiz_77", "quiz")["is_submitted"] == 1


# ==================== H6 (DB): is_seeded migration, backfill, helpers ====================


def test_h6_course_is_seeded_migration_backfill_and_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = _setup_sqlite(tmp_path, monkeypatch, "seeded_migration.db")

    # Simulate pre-P1 DB without user_courses.is_seeded column
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            """CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                lms_username TEXT NOT NULL,
                lms_password TEXT NOT NULL,
                check_interval INTEGER DEFAULT 10
            )"""
        )
        conn.execute(
            """CREATE TABLE user_courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                course_name TEXT NOT NULL,
                course_url TEXT,
                is_monitored INTEGER DEFAULT 1,
                added_at TEXT,
                UNIQUE(telegram_id, course_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE seen_content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                content_id TEXT NOT NULL,
                content_type TEXT NOT NULL,
                content_name TEXT,
                content_url TEXT,
                due_date TEXT,
                is_submitted INTEGER DEFAULT 0,
                first_seen TEXT,
                UNIQUE(telegram_id, content_id, content_type)
            )"""
        )
        conn.execute("INSERT INTO users (telegram_id, lms_username, lms_password) VALUES (10, 'u10', 'plain')")
        conn.execute("INSERT INTO user_courses (telegram_id, course_id, course_name) VALUES (10, 100, 'Old Seeded')")
        conn.execute("INSERT INTO user_courses (telegram_id, course_id, course_name) VALUES (10, 200, 'Empty Old')")
        conn.execute(
            "INSERT INTO seen_content (telegram_id, course_id, content_id, content_type) VALUES (10, 100, 'a1', 'assign')"
        )
        conn.commit()
    finally:
        conn.close()

    # Run P1 init_db migration
    database.init_db()

    # Existing course WITH seen_content must be backfilled to seeded=True
    assert database.is_course_seeded(10, 100) is True
    # Existing course WITHOUT seen_content stays False
    assert database.is_course_seeded(10, 200) is False

    # Newly added course starts as is_seeded = 0 (False)
    database.add_course(10, 300, "Fresh Course")
    assert database.is_course_seeded(10, 300) is False

    # Explicit mark_course_seeded flips it to True
    database.mark_course_seeded(10, 300)
    assert database.is_course_seeded(10, 300) is True

    # Re-running init_db() must NOT backfill a brand-new unseeded course that happens to have seen_content
    database.add_course(10, 400, "New Unseeded")
    database.mark_seen(10, 400, "r1", "resource", "Slide 1")
    database.init_db()
    assert database.is_course_seeded(10, 400) is False


# ==================== H2 + C9 (DB): app_sessions TTL, pair_codes, notifications, health_check ====================


def test_h2_app_sessions_expires_at_migration_ttl_and_expiry_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = _setup_sqlite(tmp_path, monkeypatch, "sessions_ttl.db")

    # Simulate legacy app_sessions without expires_at
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            """CREATE TABLE app_sessions (
                token_hash TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                device TEXT,
                created_at TEXT,
                last_used TEXT
            )"""
        )
        legacy_raw = "legacy_token_1234567890_abcdefg"
        legacy_hash = database._hash_token(legacy_raw)
        conn.execute(
            "INSERT INTO app_sessions (token_hash, telegram_id, device, created_at, last_used) VALUES (?,?,?,?,?)",
            (legacy_hash, 55, "OldPhone", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    database.init_db()

    # Legacy session got backfilled with future expires_at and remains valid
    sess = database.get_app_session(legacy_raw)
    assert sess is not None
    assert sess["telegram_id"] == 55
    assert int(sess["expires_at"]) > int(time.time()) + 20 * 86400

    # Default create_app_session sets ~30d TTL
    fresh_tok = database.create_app_session(55, "NewPhone")
    fresh_sess = database.get_app_session(fresh_tok)
    assert fresh_sess is not None
    assert fresh_sess["telegram_id"] == 55
    assert int(fresh_sess["expires_at"]) >= int(time.time()) + 29 * 86400

    # Expired session is rejected AND deleted from DB
    expired_tok = database.create_app_session(55, "ExpiredPhone", ttl_seconds=-5)
    assert database.get_app_session(expired_tok) is None
    assert database.verify_app_token(expired_tok) is None
    with database.DBContext() as ctx:
        row = ctx.fetchone(
            "SELECT * FROM app_sessions WHERE token_hash=?", (database._hash_token(expired_tok),)
        )
    assert row is None


def test_h2_c9_pair_codes_persistent_atomic_single_use_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "pair_codes.db")
    database.init_db()

    now = int(time.time())
    database.save_pair_code("12345678", 99, now + 600)
    database.save_pair_code("00000001", 99, now - 10)
    database.save_pair_code("99999999", 99, now + 600)

    # Valid code succeeds once, then returns None on replay
    assert database.consume_pair_code("12345678") == 99
    assert database.consume_pair_code("12345678") is None

    # Expired code returns None
    assert database.consume_pair_code("00000001") is None

    # cleanup_pair_codes removes expired rows while keeping valid ones
    database.save_pair_code("00000002", 99, now - 5)
    removed = database.cleanup_pair_codes()
    assert removed >= 1
    assert database.consume_pair_code("99999999") == 99


def test_h12_c9_set_user_notifications_enabled_and_health_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "health_notify.db")
    database.init_db()

    assert database.health_check() is True

    tid = 6001
    database.save_user(tid, "u6001", "pw6001")
    database.set_monitoring(tid, True)
    database.set_user_notifications_enabled(tid, False)
    u = database.get_user(tid)
    assert u["is_monitoring"] == 0
    assert u["notify_assign"] == 0
    assert u["notify_quiz"] == 0
    assert u["notify_resource"] == 0
    assert u["notify_grade"] == 0
    assert u["notify_announcement"] == 0
    assert u["notify_attendance"] == 0

    database.set_user_notifications_enabled(tid, True)
    u2 = database.get_user(tid)
    assert u2["notify_assign"] == 1
    assert u2["notify_quiz"] == 1
    assert u2["notify_resource"] == 1
    assert u2["notify_grade"] == 1
    assert u2["notify_announcement"] == 1
    assert u2["notify_attendance"] == 1

    # health_check returns False cleanly on DB error
    monkeypatch.setattr(database.DBContext, "__enter__", lambda self: (_ for _ in ()).throw(RuntimeError("DB down")))
    assert database.health_check() is False


# ==================== H15 (DB): Single-query get_all_seen_content & notify_quiz ====================


def test_h15_get_all_seen_content_single_query_grouped_by_course(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "seen_batch.db")
    database.init_db()

    tid = 4001
    database.save_user(tid, "u4001", "pw4001")
    for cid in (10, 20, 30):
        database.add_course(tid, cid, f"Course {cid}")
        database.mark_seen(tid, cid, f"assign_{cid}", "assign", f"HW {cid}")
        database.mark_seen(tid, cid, f"res_{cid}", "resource", f"PDF {cid}")

    orig_execute = database.DBContext.execute
    calls = {"count": 0}

    def _spy(self, sql, params=()):
        calls["count"] += 1
        return orig_execute(self, sql, params)

    monkeypatch.setattr(database.DBContext, "execute", _spy)
    grouped = database.get_all_seen_content(tid)

    assert calls["count"] == 1
    assert set(grouped.keys()) == {"10", "20", "30"}
    assert len(grouped["10"]) == 2
    assert len(grouped["20"]) == 2
    assert len(grouped["30"]) == 2


def test_h15_get_pending_reminders_respects_notify_quiz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "quiz_reminders.db")
    database.init_db()

    now = int(time.time())
    due = now + 7200  # inside 3h window

    # User A: notify_assign=1, notify_quiz=0 -> only assignment reminder
    database.save_user(1, "ua", "pwa")
    database.set_monitoring(1, True)
    database.update_notify_settings(1, assign=1, quiz=0)
    database.add_course(1, 100, "Math")
    database.upsert_reminder(1, 100, "assign_1", "HW 1", "", due, "soon")
    database.upsert_reminder(1, 100, "quiz_1", "Quiz 1", "", due, "soon")

    # User B: notify_assign=0, notify_quiz=1 -> only quiz reminder
    database.save_user(2, "ub", "pwb")
    database.set_monitoring(2, True)
    database.update_notify_settings(2, assign=0, quiz=1)
    database.add_course(2, 100, "Math")
    database.upsert_reminder(2, 100, "assign_2", "HW 2", "", due, "soon")
    database.upsert_reminder(2, 100, "quiz_2", "Quiz 2", "", due, "soon")

    pending = database.get_pending_reminders(now)
    ids = {(r["telegram_id"], r["content_id"]) for r in pending}
    assert (1, "assign_1") in ids
    assert (1, "quiz_1") not in ids
    assert (2, "quiz_2") in ids
    assert (2, "assign_2") not in ids


# ==================== C9: Hosting config & docs ====================


def test_c9_config_and_docs_requirements() -> None:
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "psycopg-pool" in req or "psycopg[binary,pool]" in req

    render = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "type: web" in render
    assert "healthCheckPath: /health" in render
    for key in ("OWNER_TELEGRAM_ID", "ENCRYPTION_KEY", "WEBAPP_URL", "DATABASE_URL", "DIRECT_URL"):
        assert key in render, f"Missing {key} in render.yaml"

    env_ex = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("OWNER_TELEGRAM_ID", "ENCRYPTION_KEY", "WEBAPP_URL", "DATABASE_URL", "DIRECT_URL"):
        assert key in env_ex, f"Missing {key} in .env.example"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "UptimeRobot" in readme
    assert "/health" in readme
    assert "DIRECT_URL" in readme
    assert "pgbouncer" in readme.lower() or "PgBouncer" in readme
