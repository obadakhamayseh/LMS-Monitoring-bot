import sqlite3
import logging
from config import DATABASE_PATH

logger = logging.getLogger(__name__)

def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
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

    c.execute("""
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

    c.execute("""
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id  INTEGER NOT NULL,
            course_id    INTEGER NOT NULL,
            content_id   TEXT    NOT NULL,
            content_name TEXT,
            content_url  TEXT,
            due_ts       INTEGER,            -- Unix timestamp لموعد التسليم
            due_str      TEXT,               -- النص الأصلي للموعد
            reminded_24h INTEGER DEFAULT 0,  -- هل أُرسل تذكير 24 ساعة؟
            reminded_3h  INTEGER DEFAULT 0,  -- هل أُرسل تذكير 3 ساعات؟
            reminded_1h  INTEGER DEFAULT 0,  -- هل أُرسل تذكير ساعة واحدة؟
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
            UNIQUE(telegram_id, content_id)
        )
    """)

    conn.commit()
    conn.close()
    logger.info("DB initialised")

def save_user(telegram_id, lms_username, lms_password,
              lms_user_id=None, full_name=None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO users (telegram_id, lms_username, lms_password,
                           lms_user_id, full_name)
        VALUES (?,?,?,?,?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            lms_username = excluded.lms_username,
            lms_password = excluded.lms_password,
            lms_user_id  = excluded.lms_user_id,
            full_name    = excluded.full_name
    """, (telegram_id, lms_username, lms_password, lms_user_id, full_name))
    conn.commit(); conn.close()

def get_user(telegram_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_monitoring_users():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM users WHERE is_monitoring=1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def set_monitoring(telegram_id, status: bool):
    conn = get_connection()
    conn.execute("UPDATE users SET is_monitoring=? WHERE telegram_id=?",
                 (1 if status else 0, telegram_id))
    conn.commit(); conn.close()

def update_notify_settings(telegram_id, assign=None, quiz=None, resource=None):
    conn = get_connection()
    if assign   is not None: conn.execute("UPDATE users SET notify_assign=?   WHERE telegram_id=?", (int(assign),   telegram_id))
    if quiz     is not None: conn.execute("UPDATE users SET notify_quiz=?     WHERE telegram_id=?", (int(quiz),     telegram_id))
    if resource is not None: conn.execute("UPDATE users SET notify_resource=? WHERE telegram_id=?", (int(resource), telegram_id))
    conn.commit(); conn.close()

def update_check_interval(telegram_id, minutes: int):
    conn = get_connection()
    conn.execute("UPDATE users SET check_interval=? WHERE telegram_id=?",
                 (minutes, telegram_id))
    conn.commit(); conn.close()

def update_last_check(telegram_id):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET last_check=datetime('now','localtime') WHERE telegram_id=?",
        (telegram_id,)
    )
    conn.commit(); conn.close()

def delete_user(telegram_id):
    conn = get_connection()
    conn.execute("DELETE FROM seen_content  WHERE telegram_id=?", (telegram_id,))
    conn.execute("DELETE FROM user_courses  WHERE telegram_id=?", (telegram_id,))
    conn.execute("DELETE FROM users         WHERE telegram_id=?", (telegram_id,))
    conn.commit(); conn.close()

def add_course(telegram_id, course_id, course_name, course_url=""):
    conn = get_connection()
    conn.execute("""
        INSERT INTO user_courses (telegram_id, course_id, course_name, course_url)
        VALUES (?,?,?,?)
        ON CONFLICT(telegram_id, course_id) DO UPDATE SET
            course_name = excluded.course_name,
            course_url  = excluded.course_url
    """, (telegram_id, course_id, course_name, course_url))
    conn.commit(); conn.close()

def remove_course(telegram_id, course_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM user_courses WHERE telegram_id=? AND course_id=?",
        (telegram_id, course_id)
    )
    conn.execute(
        "DELETE FROM seen_content WHERE telegram_id=? AND course_id=?",
        (telegram_id, course_id)
    )
    conn.commit(); conn.close()

def get_user_courses(telegram_id, monitored_only=False):
    conn = get_connection()
    if monitored_only:
        rows = conn.execute(
            "SELECT * FROM user_courses WHERE telegram_id=? AND is_monitored=1 ORDER BY added_at",
            (telegram_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM user_courses WHERE telegram_id=? ORDER BY added_at",
            (telegram_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_course(telegram_id, course_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM user_courses WHERE telegram_id=? AND course_id=?",
        (telegram_id, course_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def toggle_course(telegram_id, course_id):
    conn = get_connection()
    conn.execute("""
        UPDATE user_courses
        SET is_monitored = 1 - is_monitored
        WHERE telegram_id=? AND course_id=?
    """, (telegram_id, course_id))
    conn.commit(); conn.close()

def is_seen(telegram_id, content_id, content_type) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM seen_content WHERE telegram_id=? AND content_id=? AND content_type=?",
        (telegram_id, str(content_id), content_type)
    ).fetchone()
    conn.close()
    return row is not None

def mark_seen(telegram_id, course_id, content_id, content_type,
              name=None, url=None, due=None):
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO seen_content
            (telegram_id, course_id, content_id, content_type,
             content_name, content_url, due_date)
            VALUES (?,?,?,?,?,?,?)
        """, (telegram_id, course_id, str(content_id), content_type,
              name, url, due))
        conn.commit()
    finally:
        conn.close()

def get_stats(telegram_id) -> dict:
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) FROM seen_content WHERE telegram_id=?", (telegram_id,)
    ).fetchone()[0]
    by_type = conn.execute(
        "SELECT content_type, COUNT(*) as n FROM seen_content WHERE telegram_id=? GROUP BY content_type",
        (telegram_id,)
    ).fetchall()
    conn.close()
    return {"total": total, "by_type": {r[0]: r[1] for r in by_type}}

def upsert_reminder(telegram_id, course_id, content_id, content_name,
                    content_url, due_ts, due_str):
    conn = get_connection()
    conn.execute("""
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
    conn.commit(); conn.close()

def get_pending_reminders(now_ts: int) -> list:
    conn = get_connection()
    rows = conn.execute("""
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
    """, (now_ts, now_ts, now_ts, now_ts, now_ts, now_ts)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_reminded(reminder_id: int, field: str):
    conn = get_connection()
    conn.execute(f"UPDATE reminders SET {field}=1 WHERE id=?", (reminder_id,))
    conn.commit(); conn.close()

def delete_reminders_for_course(telegram_id, course_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM reminders WHERE telegram_id=? AND course_id=?",
        (telegram_id, course_id)
    )
    conn.commit(); conn.close()

def delete_all_reminders(telegram_id):
    conn = get_connection()
    conn.execute("DELETE FROM reminders WHERE telegram_id=?", (telegram_id,))
    conn.commit(); conn.close()

def get_upcoming_deadlines(telegram_id, limit=10) -> list:
    import time
    now = int(time.time())
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.*, uc.course_name
        FROM reminders r
        JOIN user_courses uc
          ON r.telegram_id = uc.telegram_id AND r.course_id = uc.course_id
        WHERE r.telegram_id = ?
          AND r.due_ts > ?
        ORDER BY r.due_ts ASC
        LIMIT ?
    """, (telegram_id, now, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
