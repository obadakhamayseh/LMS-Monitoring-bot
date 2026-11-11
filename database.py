import os
import time
import sqlite3
import logging
import threading
from datetime import datetime
from config import DATABASE_PATH, DATABASE_URL, DIRECT_URL, CHECK_INTERVAL_MINUTES
from security import encrypt_password, decrypt_password

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None

logger = logging.getLogger(__name__)
_pg_local = threading.local()

def _get_pg_url():
    url = DIRECT_URL or DATABASE_URL or os.getenv("DIRECT_URL", "") or os.getenv("DATABASE_URL", "")
    if url:
        return url.replace("?pgbouncer=true", "").replace("&pgbouncer=true", "")
    return None

def _get_pg_conn(pg_url: str):
    conn = getattr(_pg_local, "conn", None)
    if conn is not None:
        try:
            if not conn.closed and not conn.broken:
                return conn
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    conn = psycopg.connect(pg_url, autocommit=True, row_factory=dict_row, connect_timeout=15)
    _pg_local.conn = conn
    return conn

class DBContext:
    def __init__(self):
        self.pg_url = _get_pg_url()
        self.is_pg = bool(self.pg_url and psycopg is not None)
        self.conn = None

    def __enter__(self):
        if self.is_pg:
            self.conn = _get_pg_conn(self.pg_url)
        else:
            self.conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if not self.is_pg:
                if exc_type is None:
                    self.conn.commit()
                self.conn.close()
            elif exc_type is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
                _pg_local.conn = None

    def execute(self, sql, params=()):
        if self.is_pg:
            sql = sql.replace("?", "%s")
            try:
                cur = self.conn.cursor()
                cur.execute(sql, params)
                return cur
            except Exception:
                try:
                    self.conn.close()
                except Exception:
                    pass
                _pg_local.conn = None
                self.conn = _get_pg_conn(self.pg_url)
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

        # Migration: add is_submitted to reminders/seen_content and notify_grade/notify_announcement to users
        try:
            if db.is_pg:
                db.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS is_submitted INTEGER DEFAULT 0")
                db.execute("ALTER TABLE seen_content ADD COLUMN IF NOT EXISTS is_submitted INTEGER DEFAULT 0")
                db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_grade INTEGER DEFAULT 1")
                db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_announcement INTEGER DEFAULT 1")
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
        except Exception as e:
            logger.debug(f"Migration for new columns: {e}")

        # Encrypt existing plain-text passwords if any
        try:
            legacy_users = db.fetchall("SELECT telegram_id, lms_password FROM users")
            for u in legacy_users:
                pwd = u.get("lms_password") or ""
                if pwd and not pwd.startswith("gAAAAA"):
                    enc = encrypt_password(pwd)
                    db.execute("UPDATE users SET lms_password=? WHERE telegram_id=?", (enc, u["telegram_id"]))
        except Exception as e:
            logger.warning(f"Password encryption migration check failed: {e}")

    logger.info("DB initialised")

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

def update_notify_settings(telegram_id, assign=None, quiz=None, resource=None,
                           grade=None, announcement=None):
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

def remove_course(telegram_id, course_id):
    with DBContext() as db:
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
                   SET content_name=?, content_url=?, due_date=?, is_submitted=?
                   WHERE telegram_id=? AND content_id=? AND content_type=?""",
                (name, url, due, 1 if is_submitted else 0, telegram_id, str(content_id), content_type)
            )
        else:
            db.execute(
                """UPDATE seen_content
                   SET content_name=?, content_url=?, due_date=?
                   WHERE telegram_id=? AND content_id=? AND content_type=?""",
                (name, url, due, telegram_id, str(content_id), content_type)
            )

def mark_seen(telegram_id, course_id, content_id, content_type,
              name=None, url=None, due=None, is_submitted=0):
    with DBContext() as db:
        db.execute("""
            INSERT INTO seen_content
            (telegram_id, course_id, content_id, content_type,
             content_name, content_url, due_date, is_submitted)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id, content_id, content_type) DO UPDATE SET
                due_date = CASE WHEN excluded.due_date <> '' THEN excluded.due_date ELSE seen_content.due_date END,
                is_submitted = excluded.is_submitted
        """, (telegram_id, course_id, str(content_id), content_type,
              name, url, due, 1 if is_submitted else 0))

def mark_assignment_submitted(telegram_id, content_id, is_submitted=1):
    val = 1 if is_submitted else 0
    with DBContext() as db:
        db.execute(
            "UPDATE reminders SET is_submitted=? WHERE telegram_id=? AND content_id=?",
            (val, telegram_id, str(content_id))
        )
        db.execute(
            "UPDATE seen_content SET is_submitted=? WHERE telegram_id=? AND content_id=?",
            (val, telegram_id, str(content_id))
        )

def clear_all_user_courses(telegram_id):
    with DBContext() as db:
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
                is_submitted = excluded.is_submitted
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
        return db.fetchall("""
            SELECT r.*, u.lms_username, u.notify_assign
            FROM reminders r
            JOIN users u ON r.telegram_id = u.telegram_id
            WHERE u.is_monitoring = 1
              AND u.notify_assign = 1
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
