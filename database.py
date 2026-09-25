import os
import time
import sqlite3
import logging
from datetime import datetime
from config import DATABASE_PATH, DATABASE_URL, DIRECT_URL

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None

logger = logging.getLogger(__name__)

def _get_pg_url():
    url = DIRECT_URL or DATABASE_URL or os.getenv("DIRECT_URL", "") or os.getenv("DATABASE_URL", "")
    if url:
        return url.replace("?pgbouncer=true", "").replace("&pgbouncer=true", "")
    return None

class DBContext:
    def __init__(self):
        self.pg_url = _get_pg_url()
        self.is_pg = bool(self.pg_url and psycopg is not None)
        self.conn = None

    def __enter__(self):
        if self.is_pg:
            self.conn = psycopg.connect(self.pg_url, autocommit=True, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(DATABASE_PATH)
            self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if not self.is_pg:
                if exc_type is None:
                    self.conn.commit()
            self.conn.close()

    def execute(self, sql, params=()):
        if self.is_pg:
            sql = sql.replace("?", "%s")
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur
        else:
            return self.conn.execute(sql, params)

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

def init_db():
    with DBContext() as db:
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
                    check_interval  INTEGER DEFAULT 600,
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
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, content_id)
                )
            """)
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id     INTEGER PRIMARY KEY,
                    lms_username    TEXT    NOT NULL,
                    lms_password    TEXT    NOT NULL,
                    lms_user_id     TEXT,
                    full_name       TEXT,
                    is_monitoring   INTEGER DEFAULT 0,
                    notify_assign   INTEGER DEFAULT 1,
                    notify_quiz     INTEGER DEFAULT 1,
                    notify_resource INTEGER DEFAULT 1,
                    check_interval  INTEGER DEFAULT 600,
                    created_at      TEXT    DEFAULT (datetime('now','localtime')),
                    last_check      TEXT
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
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, content_id)
                )
            """)
    logger.info("DB initialised")

def save_user(telegram_id, lms_username, lms_password,
              lms_user_id=None, full_name=None):
    with DBContext() as db:
        db.execute("""
            INSERT INTO users (telegram_id, lms_username, lms_password,
                               lms_user_id, full_name)
            VALUES (?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                lms_username = excluded.lms_username,
                lms_password = excluded.lms_password,
                lms_user_id  = excluded.lms_user_id,
                full_name    = excluded.full_name
        """, (telegram_id, lms_username, lms_password, lms_user_id, full_name))

def get_user(telegram_id):
    with DBContext() as db:
        return db.fetchone("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))

def get_all_monitoring_users():
    with DBContext() as db:
        return db.fetchall("SELECT * FROM users WHERE is_monitoring=1")

def set_monitoring(telegram_id, status: bool):
    with DBContext() as db:
        db.execute("UPDATE users SET is_monitoring=? WHERE telegram_id=?",
                     (1 if status else 0, telegram_id))

def update_notify_settings(telegram_id, assign=None, quiz=None, resource=None):
    with DBContext() as db:
        if assign is not None:
            db.execute("UPDATE users SET notify_assign=? WHERE telegram_id=?", (int(assign), telegram_id))
        if quiz is not None:
            db.execute("UPDATE users SET notify_quiz=? WHERE telegram_id=?", (int(quiz), telegram_id))
        if resource is not None:
            db.execute("UPDATE users SET notify_resource=? WHERE telegram_id=?", (int(resource), telegram_id))

def update_check_interval(telegram_id, minutes: int):
    with DBContext() as db:
        db.execute("UPDATE users SET check_interval=? WHERE telegram_id=?",
                     (minutes, telegram_id))

def update_last_check(telegram_id):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with DBContext() as db:
        db.execute(
            "UPDATE users SET last_check=? WHERE telegram_id=?",
            (now_str, telegram_id)
        )

def delete_user(telegram_id):
    with DBContext() as db:
        db.execute("DELETE FROM seen_content WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM reminders    WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM user_courses WHERE telegram_id=?", (telegram_id,))
        db.execute("DELETE FROM users        WHERE telegram_id=?", (telegram_id,))

def add_course(telegram_id, course_id, course_name, course_url=""):
    with DBContext() as db:
        db.execute("""
            INSERT INTO user_courses (telegram_id, course_id, course_name, course_url)
            VALUES (?,?,?,?)
            ON CONFLICT(telegram_id, course_id) DO UPDATE SET
                course_name = excluded.course_name,
                course_url  = excluded.course_url
        """, (telegram_id, course_id, course_name, course_url))

def remove_course(telegram_id, course_id):
    with DBContext() as db:
        db.execute("DELETE FROM user_courses WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))
        db.execute("DELETE FROM seen_content WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))
        db.execute("DELETE FROM reminders    WHERE telegram_id=? AND course_id=?", (telegram_id, course_id))

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

def mark_seen(telegram_id, course_id, content_id, content_type,
              name=None, url=None, due=None):
    with DBContext() as db:
        db.execute("""
            INSERT INTO seen_content
            (telegram_id, course_id, content_id, content_type,
             content_name, content_url, due_date)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, content_id, content_type) DO NOTHING
        """, (telegram_id, course_id, str(content_id), content_type,
              name, url, due))

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
                    content_url, due_ts, due_str):
    with DBContext() as db:
        db.execute("""
            INSERT INTO reminders
                (telegram_id, course_id, content_id, content_name,
                 content_url, due_ts, due_str)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, content_id) DO UPDATE SET
                content_name = excluded.content_name,
                content_url  = excluded.content_url,
                due_ts       = excluded.due_ts,
                due_str      = excluded.due_str
        """, (telegram_id, course_id, str(content_id), content_name,
              content_url, due_ts, due_str))

def get_pending_reminders(now_ts: int) -> list:
    with DBContext() as db:
        return db.fetchall("""
            SELECT r.*, u.lms_username, u.notify_assign
            FROM reminders r
            JOIN users u ON r.telegram_id = u.telegram_id
            WHERE u.is_monitoring = 1
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
