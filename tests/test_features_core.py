"""Unit tests for Features Phase core DB, config, and pure services (F1, F4, F5, F6, F7, F8)."""

from __future__ import annotations

import importlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import dotenv
import pytest
from cryptography.fernet import Fernet

# Isolated test environment BEFORE importing project modules
_TEST_KEY = Fernet.generate_key().decode("utf-8")
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST_FEATURES_CORE"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = _TEST_KEY

import config  # noqa: E402
import database  # noqa: E402
from services import absence_risk, observability, web_push  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _setup_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str = "test_features_core.db",
) -> Path:
    db_file = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DIRECT_URL", "")
    monkeypatch.setenv("ENCRYPTION_KEY", _TEST_KEY)
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DIRECT_URL", "")
    database.close()
    return db_file


# ==================== 1. Config, Env Example, Render, Requirements ====================


def test_config_feature_flags_defaults_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    feature_env_keys = (
        "ENABLE_MOODLE_API",
        "ENABLE_ICAL_FEED",
        "ENABLE_DAILY_DIGEST",
        "DEFAULT_DIGEST_TIME",
        "ENABLE_ABSENCE_RISK",
        "DEFAULT_MAX_ABSENCE_PCT",
        "VAPID_PUBLIC_KEY",
        "VAPID_PRIVATE_KEY",
        "VAPID_SUBJECT",
        "ENABLE_WEB_PUSH",
        "LOG_FORMAT",
        "ENABLE_STALE_ALERT",
        "STALE_MONITOR_SECONDS",
    )
    for key in feature_env_keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "1")

    try:
        importlib.reload(config)
        assert config.ENABLE_MOODLE_API is False
        assert config.ENABLE_ICAL_FEED is True
        assert config.ENABLE_DAILY_DIGEST is True
        assert config.DEFAULT_DIGEST_TIME == "08:00"
        assert config.ENABLE_ABSENCE_RISK is True
        assert config.DEFAULT_MAX_ABSENCE_PCT == pytest.approx(15.0)
        assert config.VAPID_PUBLIC_KEY == ""
        assert config.VAPID_PRIVATE_KEY == ""
        assert config.VAPID_SUBJECT == "mailto:admin@example.com"
        assert config.ENABLE_WEB_PUSH is False
        assert config.LOG_FORMAT == "json"
        assert config.ENABLE_STALE_ALERT is True
        assert config.STALE_MONITOR_SECONDS == 3600

        monkeypatch.setenv("ENABLE_MOODLE_API", "1")
        monkeypatch.setenv("ENABLE_ICAL_FEED", "false")
        monkeypatch.setenv("ENABLE_DAILY_DIGEST", "0")
        monkeypatch.setenv("DEFAULT_DIGEST_TIME", "07:30")
        monkeypatch.setenv("ENABLE_ABSENCE_RISK", "no")
        monkeypatch.setenv("DEFAULT_MAX_ABSENCE_PCT", "20.5")
        monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub_key_test")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv_key_test")
        monkeypatch.setenv("VAPID_SUBJECT", "mailto:owner@mutah.edu.jo")
        monkeypatch.setenv("ENABLE_WEB_PUSH", "true")
        monkeypatch.setenv("LOG_FORMAT", "TEXT")
        monkeypatch.setenv("ENABLE_STALE_ALERT", "off")
        monkeypatch.setenv("STALE_MONITOR_SECONDS", "1800")

        importlib.reload(config)
        assert config.ENABLE_MOODLE_API is True
        assert config.ENABLE_ICAL_FEED is False
        assert config.ENABLE_DAILY_DIGEST is False
        assert config.DEFAULT_DIGEST_TIME == "07:30"
        assert config.ENABLE_ABSENCE_RISK is False
        assert config.DEFAULT_MAX_ABSENCE_PCT == pytest.approx(20.5)
        assert config.VAPID_PUBLIC_KEY == "pub_key_test"
        assert config.VAPID_PRIVATE_KEY == "priv_key_test"
        assert config.VAPID_SUBJECT == "mailto:owner@mutah.edu.jo"
        assert config.ENABLE_WEB_PUSH is True
        assert config.LOG_FORMAT == "text"
        assert config.ENABLE_STALE_ALERT is False
        assert config.STALE_MONITOR_SECONDS == 1800
    finally:
        for key in feature_env_keys:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("OWNER_TELEGRAM_ID", "1")
        importlib.reload(config)


def test_env_example_render_and_requirements_updated() -> None:
    expected_keys = (
        "ENABLE_MOODLE_API",
        "ENABLE_ICAL_FEED",
        "ENABLE_DAILY_DIGEST",
        "DEFAULT_DIGEST_TIME",
        "ENABLE_ABSENCE_RISK",
        "DEFAULT_MAX_ABSENCE_PCT",
        "ENABLE_WEB_PUSH",
        "VAPID_PUBLIC_KEY",
        "VAPID_PRIVATE_KEY",
        "VAPID_SUBJECT",
        "LOG_FORMAT",
        "ENABLE_STALE_ALERT",
        "STALE_MONITOR_SECONDS",
    )
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    for key in expected_keys:
        assert f"{key}=" in env_example, f"Missing {key} in .env.example"
        assert f"key: {key}" in render_yaml, f"Missing {key} in render.yaml"

    req_txt = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pywebpush>=2.0.0" in req_txt

    req_lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "pywebpush==" in req_lock


# ==================== 2. Database Migrations, Settings, Calendar Tokens & Push ====================


def test_db_idempotent_feature_column_migrations_from_legacy_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = _setup_sqlite(tmp_path, monkeypatch, "legacy_features.db")

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
            "INSERT INTO users (telegram_id, lms_username, lms_password) VALUES (101, 'st1', 'pw1')"
        )
        conn.execute(
            "INSERT INTO user_courses (telegram_id, course_id, course_name) VALUES (101, 55, 'Discrete Math')"
        )
        conn.commit()
    finally:
        conn.close()

    # Run twice to verify strict idempotency
    database.init_db()
    database.init_db()

    user = database.get_user(101)
    assert user is not None
    assert user["use_moodle_api"] == 0
    assert user["enable_ical_feed"] == 1
    assert user["digest_enabled"] == 1
    assert user["digest_time"] == "08:00"
    assert user["last_digest_date"] == ""
    assert user["notify_absence_risk"] == 1
    assert float(user["max_absence_pct"]) == pytest.approx(15.0)
    assert user["enable_web_push"] == 1

    course = database.get_course(101, 55)
    assert course is not None
    assert course["credit_hours"] == 3
    assert course["last_risk_level"] == ""


def test_db_update_settings_whitelist_digest_and_course_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "settings_helpers.db")
    database.init_db()

    tid = 201
    database.save_user(tid, "u201", "original_secret", "lm201", "Student 201")
    database.add_course(tid, 301, "Algorithms")

    applied = database.update_settings(
        tid,
        settings={"telegram_id": 999, "1=1; DROP TABLE users; --": 1},
        use_moodle_api=True,
        enable_ical_feed=0,
        digest_enabled=True,
        digest_time="07:45",
        notify_absence_risk=0,
        max_absence_pct=20.0,
        enable_web_push=False,
        lms_password="HACKED",  # non-whitelisted: must be ignored!
        lms_username="spoofed",
    )
    assert "lms_password" not in applied
    assert "lms_username" not in applied
    assert "telegram_id" not in applied

    u = database.get_user(tid)
    assert u is not None
    assert u["lms_password"] == "original_secret"
    assert u["use_moodle_api"] == 1
    assert u["enable_ical_feed"] == 0
    assert u["digest_enabled"] == 1
    assert u["digest_time"] == "07:45"
    assert u["notify_absence_risk"] == 0
    assert float(u["max_absence_pct"]) == pytest.approx(20.0)
    assert u["enable_web_push"] == 0

    database.mark_digest_sent(tid, "2026-11-26")
    assert database.get_user(tid)["last_digest_date"] == "2026-11-26"
    digest_users = database.get_digest_users()
    assert [r["telegram_id"] for r in digest_users] == [tid]

    database.update_course_credit_hours(tid, 301, 4)
    database.set_course_risk_level(tid, 301, "warning")
    c = database.get_course(tid, 301)
    assert c["credit_hours"] == 4
    assert c["last_risk_level"] == "warning"

    with pytest.raises(ValueError):
        database.update_course_credit_hours(tid, 301, 0)
    with pytest.raises(ValueError):
        database.update_course_credit_hours(tid, 301, 15)


def test_db_calendar_tokens_create_rotate_revoke_and_ical_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = _setup_sqlite(tmp_path, monkeypatch, "calendar_tokens.db")
    database.init_db()

    tid = 301
    database.save_user(tid, "u301", "pw301")

    assert database.get_calendar_token(tid) is None

    tok1 = database.create_or_rotate_calendar_token(tid)
    assert len(tok1) >= 32
    assert database.get_calendar_token(tid) == tok1
    assert database.resolve_calendar_token(tok1) == tid
    assert database.resolve_calendar_token("invalid_token_value_1234567890") is None

    # Raw token must NEVER appear in SQLite storage (only hash + enc:v1: ciphertext)
    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute(
            "SELECT token_hash, encrypted_token, revoked FROM calendar_tokens WHERE telegram_id=?",
            (tid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert tok1 not in row[0]
    assert tok1 not in row[1]
    assert row[1].startswith("enc:v1:")
    assert row[2] == 0

    # Disabling enable_ical_feed must reject token resolution
    database.update_settings(tid, enable_ical_feed=0)
    assert database.resolve_calendar_token(tok1) is None
    database.update_settings(tid, enable_ical_feed=1)
    assert database.resolve_calendar_token(tok1) == tid

    # Rotation invalidates old token and activates new one
    tok2 = database.create_or_rotate_calendar_token(tid)
    assert tok2 != tok1
    assert database.resolve_calendar_token(tok1) is None
    assert database.resolve_calendar_token(tok2) == tid
    assert database.get_calendar_token(tid) == tok2

    # Revoking blocks both get_calendar_token and resolve_calendar_token
    database.revoke_calendar_token(tid)
    assert database.get_calendar_token(tid) is None
    assert database.resolve_calendar_token(tok2) is None


def test_db_push_subscriptions_session_cleanup_and_atomic_delete_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = _setup_sqlite(tmp_path, monkeypatch, "push_subs.db")
    database.init_db()

    tid = 401
    database.save_user(tid, "u401", "pw401")
    dev_tok1 = database.create_app_session(tid, "Phone 1")
    dev_tok2 = database.create_app_session(tid, "Phone 2")

    database.save_push_subscription(
        tid,
        "https://push.example/sub1",
        "p256dh_1",
        "auth_1",
        device_token=dev_tok1,
    )
    database.save_push_subscription(
        tid,
        "https://push.example/sub2",
        "p256dh_2",
        "auth_2",
        device_token=dev_tok2,
    )
    # Upsert same endpoint updates keys cleanly
    database.save_push_subscription(
        tid,
        "https://push.example/sub1",
        "p256dh_1_updated",
        "auth_1_updated",
        device_token=dev_tok1,
    )

    subs = database.get_push_subscriptions(tid)
    assert len(subs) == 2
    sub1 = next(s for s in subs if s["endpoint"] == "https://push.example/sub1")
    assert sub1["p256dh"] == "p256dh_1_updated"
    assert sub1["device_token"] != dev_tok1  # hashed, never plaintext session token

    # delete_app_session(token) must delete BOTH app_session and bound push_subscription
    database.delete_app_session(dev_tok1)
    assert database.verify_app_token(dev_tok1) is None
    assert database.verify_app_token(dev_tok2) == tid
    subs_after = database.get_push_subscriptions(tid)
    assert [s["endpoint"] for s in subs_after] == ["https://push.example/sub2"]

    # delete_push_subscriptions_by_device works directly
    database.save_push_subscription(
        tid,
        "https://push.example/sub3",
        "p256dh_3",
        "auth_3",
        device_token=dev_tok2,
    )
    removed = database.delete_push_subscriptions_by_device(dev_tok2)
    assert removed == 2
    assert database.get_push_subscriptions(tid) == []

    # delete_user atomically cleans calendar_tokens AND push_subscriptions
    cal_tok = database.create_or_rotate_calendar_token(tid)
    database.save_push_subscription(tid, "https://push.example/sub4", "k4", "a4")
    database.delete_user(tid)

    assert database.get_user(tid) is None
    assert database.resolve_calendar_token(cal_tok) is None
    assert database.get_push_subscriptions(tid) == []

    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute("SELECT COUNT(*) FROM calendar_tokens WHERE telegram_id=?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE telegram_id=?", (tid,)).fetchone()[0] == 0
    finally:
        conn.close()


# ==================== 3. services/absence_risk.py (F6) ====================


def test_calculate_absence_risk_thresholds_and_partial_semester() -> None:
    # 3 credits, mid-semester partial (10 sessions) -> effective 48 -> 15% = 7 allowed
    safe = absence_risk.calculate_absence_risk(3, 10, credit_hours=3, max_absence_pct=15.0, course_name="هياكل بيانات")
    assert safe["effective_total_sessions"] == 48
    assert safe["max_allowed"] == 7
    assert safe["remaining"] == 4
    assert safe["risk_level"] == "safe"
    assert safe["at_risk"] is False
    assert safe["message"] == ""

    warning = absence_risk.calculate_absence_risk(5, 10, credit_hours=3, max_absence_pct=15.0, course_name="هياكل بيانات")
    assert warning["remaining"] == 2
    assert warning["risk_level"] == "warning"
    assert warning["at_risk"] is True
    assert "⚠️" in warning["message"]
    assert "2" in warning["message"]
    assert "هياكل بيانات" in warning["message"]
    assert "15%" in warning["message"]

    critical = absence_risk.calculate_absence_risk(6, 10, credit_hours=3, max_absence_pct=15.0, course_name="هياكل بيانات")
    assert critical["remaining"] == 1
    assert critical["risk_level"] == "critical"
    assert "⚠️" in critical["message"]
    assert "1" in critical["message"]

    deprived = absence_risk.calculate_absence_risk(7, 10, credit_hours=3, max_absence_pct=15.0, course_name="هياكل بيانات")
    assert deprived["remaining"] == 0
    assert deprived["risk_level"] == "deprived"
    assert "حرمان" in deprived["message"]

    # When total_sessions >= credit_hours * 10, use recorded total directly
    recorded = absence_risk.calculate_absence_risk(5, 40, credit_hours=3, max_absence_pct=15.0, course_name="شبكات")
    assert recorded["effective_total_sessions"] == 40
    assert recorded["max_allowed"] == 6
    assert recorded["remaining"] == 1
    assert recorded["risk_level"] == "critical"


def test_evaluate_user_absence_risks_reads_db_and_sorts_by_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "absence_eval.db")
    database.init_db()

    tid = 501
    database.save_user(tid, "u501", "pw501")
    database.update_settings(tid, max_absence_pct=15.0)

    database.add_course(tid, 10, "Safe Course")
    database.add_course(tid, 20, "Critical Course")
    database.add_course(tid, 30, "Deprived Course")
    database.update_course_credit_hours(tid, 30, 2)  # 2 * 16 = 32 -> 15% = 4 max
    database.set_course_risk_level(tid, 20, "warning")

    database.upsert_attendance(tid, 10, "att1", taken_sessions=10, total_sessions=10, absent_count=1)
    database.upsert_attendance(tid, 20, "att2", taken_sessions=12, total_sessions=12, absent_count=6)
    database.upsert_attendance(tid, 30, "att3", taken_sessions=10, total_sessions=10, absent_count=4)

    risks = absence_risk.evaluate_user_absence_risks(database, tid)
    assert [r["course_id"] for r in risks] == [30, 20, 10]
    assert [r["risk_level"] for r in risks] == ["deprived", "critical", "safe"]
    assert risks[0]["credit_hours"] == 2
    assert risks[1]["last_risk_level"] == "warning"


# ==================== 4. services/observability.py (F8) ====================


def test_json_log_formatter_and_configure_logging(tmp_path: Path) -> None:
    formatter = observability.JsonLogFormatter()
    rec = logging.LogRecord(
        name="test.obs",
        level=logging.WARNING,
        pathname=__file__,
        lineno=42,
        msg="Check failed for course %s",
        args=(101,),
        exc_info=None,
    )
    rec.course_id = 101
    line = formatter.format(rec)
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["level"] == "WARNING"
    assert parsed["logger"] == "test.obs"
    assert parsed["message"] == "Check failed for course 101"
    assert parsed["course_id"] == 101
    assert parsed["timestamp"].endswith("Z")

    # Exception formatting included in exc_info
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_rec = logging.LogRecord(
            name="test.exc",
            level=logging.ERROR,
            pathname=__file__,
            lineno=55,
            msg="Unhandled",
            args=(),
            exc_info=sys.exc_info(),
        )
    exc_parsed = json.loads(formatter.format(exc_rec))
    assert "ValueError: boom" in exc_parsed["exc_info"]

    # configure_logging attaches RotatingFileHandler + JsonLogFormatter
    root = logging.getLogger()
    orig_handlers = list(root.handlers)
    orig_level = root.level
    log_path = tmp_path / "obs_test.log"
    try:
        rfh = observability.configure_logging(log_file=str(log_path), log_format="json")
        assert isinstance(rfh, RotatingFileHandler)
        assert isinstance(rfh.formatter, observability.JsonLogFormatter)
        logging.getLogger("obs.filetest").info("hello json")
        rfh.flush()
        content = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(content) >= 1
        entry = json.loads(content[-1])
        assert entry["message"] == "hello json"

        rfh_text = observability.configure_logging(log_file=str(log_path), log_format="text")
        assert isinstance(rfh_text, RotatingFileHandler)
        assert not isinstance(rfh_text.formatter, observability.JsonLogFormatter)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in orig_handlers:
            root.addHandler(h)
        root.setLevel(orig_level)


def test_observability_state_metrics_staleness_and_thread_safety() -> None:
    state = observability.ObservabilityState(started_at=1000.0)
    assert observability.get_observability() is observability.get_observability()

    # Not stale while stopped
    assert state.is_stale(threshold_seconds=3600, now_ts=6000.0) is False
    assert state.should_send_stale_alert(threshold_seconds=3600, now_ts=6000.0) is False

    # Running and past threshold -> stale & alerts once
    state.set_monitor_running(True)
    assert state.is_stale(threshold_seconds=3600, now_ts=4600.0) is True
    assert state.should_send_stale_alert(threshold_seconds=3600, now_ts=4600.0) is True
    state.mark_stale_alerted(True)
    assert state.should_send_stale_alert(threshold_seconds=3600, now_ts=4700.0) is False

    # Successful check resets stale_alerted & updates metrics
    state.record_check_success(
        metrics={"api_requests": 3, "scrape_requests": 1, "requests_saved": 5},
        now_ts=5000.0,
    )
    assert state.is_stale(threshold_seconds=3600, now_ts=6000.0) is False
    assert state.should_send_stale_alert(threshold_seconds=3600, now_ts=8700.0) is True

    state.record_check_error("timeout")
    state.record_waf_state(True, until_ts=6500.0)
    snap = state.snapshot(now_ts=6000.0)
    assert snap["monitor_running"] is True
    assert snap["last_successful_check_at"] is not None
    assert snap["last_successful_check_at"].endswith("Z")
    assert snap["waf_blocked"] is True
    assert snap["waf_until"] == 6500.0
    assert snap["total_checks"] == 2
    assert snap["error_count"] == 1
    assert snap["error_rate"] == pytest.approx(0.5)
    assert snap["api_requests_total"] == 3
    assert snap["scrape_requests_total"] == 1
    assert snap["requests_saved_total"] == 5
    assert snap["uptime_seconds"] == pytest.approx(5000.0)

    # Concurrent updates stay exact
    concurrent_state = observability.ObservabilityState(started_at=100.0)
    barrier = threading.Barrier(6)

    def _worker() -> None:
        barrier.wait()
        for _ in range(50):
            concurrent_state.record_check_success(metrics={"api_requests": 1, "requests_saved": 2})
            concurrent_state.record_check_error("e")

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    csnap = concurrent_state.snapshot()
    assert csnap["total_checks"] == 600
    assert csnap["error_count"] == 300
    assert csnap["api_requests_total"] == 300
    assert csnap["requests_saved_total"] == 600


# ==================== 5. services/web_push.py (F7) ====================


def test_web_push_enabled_checks_and_delivery_with_expired_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_sqlite(tmp_path, monkeypatch, "web_push.db")
    database.init_db()

    tid = 601
    database.save_user(tid, "u601", "pw601")

    # Disabled by default
    monkeypatch.setattr(config, "ENABLE_WEB_PUSH", False)
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "")
    assert web_push.is_web_push_enabled() is False
    res_disabled = web_push.send_web_push_to_user(database, tid, "Title", "Body")
    assert res_disabled["enabled"] is False
    assert res_disabled["skipped"] is True

    # Enabled but missing keys -> still False
    monkeypatch.setattr(config, "ENABLE_WEB_PUSH", True)
    assert web_push.is_web_push_enabled() is False

    # Enabled with keys -> True
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "test_pub_key")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "test_priv_key")
    monkeypatch.setattr(config, "VAPID_SUBJECT", "mailto:test@example.com")
    assert web_push.is_web_push_enabled() is True
    assert web_push.get_vapid_public_key() == "test_pub_key"

    # Register 4 subscriptions: 1 ok, 1 HTTP 410 (expired), 1 HTTP 404 (expired), 1 HTTP 500 + 1 unexpected
    database.save_push_subscription(tid, "https://push.example/ok", "p_ok", "a_ok")
    database.save_push_subscription(tid, "https://push.example/gone410", "p_410", "a_410")
    database.save_push_subscription(tid, "https://push.example/notfound404", "p_404", "a_404")
    database.save_push_subscription(tid, "https://push.example/err500", "p_500", "a_500")
    database.save_push_subscription(tid, "https://push.example/crash", "p_c", "a_c")

    sent_calls: list[dict] = []

    def _fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, timeout=None):
        ep = subscription_info["endpoint"]
        if ep.endswith("/gone410"):
            raise web_push.WebPushException("Gone", response=SimpleNamespace(status_code=410))
        if ep.endswith("/notfound404"):
            raise web_push.WebPushException("Not Found", response=SimpleNamespace(status_code=404))
        if ep.endswith("/err500"):
            raise web_push.WebPushException("Server Error", response=SimpleNamespace(status_code=500))
        if ep.endswith("/crash"):
            raise RuntimeError("network unreachable")
        sent_calls.append(
            {
                "subscription_info": subscription_info,
                "data": json.loads(data),
                "vapid_private_key": vapid_private_key,
                "vapid_claims": vapid_claims,
            }
        )

    monkeypatch.setattr(web_push, "webpush", _fake_webpush)

    # Must NEVER raise even with 404/410/500/RuntimeError
    res = web_push.send_web_push_to_user(
        database, tid, "واجب جديد", "HW 1 - Algorithms", url="/webapp?go=tasks"
    )
    assert res == {
        "enabled": True,
        "sent": 1,
        "failed": 2,
        "expired_removed": 2,
        "skipped": False,
    }
    assert len(sent_calls) == 1
    assert sent_calls[0]["data"] == {
        "title": "واجب جديد",
        "body": "HW 1 - Algorithms",
        "url": "/webapp?go=tasks",
    }
    assert sent_calls[0]["vapid_claims"] == {"sub": "mailto:test@example.com"}

    # 410 and 404 subscriptions were auto-deleted; ok, 500, and crash remain
    remaining_eps = {s["endpoint"] for s in database.get_push_subscriptions(tid)}
    assert remaining_eps == {
        "https://push.example/ok",
        "https://push.example/err500",
        "https://push.example/crash",
    }

    # User opt-out (enable_web_push=0) skips delivery cleanly
    database.update_settings(tid, enable_web_push=0)
    sent_calls.clear()
    res_opt_out = web_push.send_web_push_to_user(database, tid, "T", "B")
    assert res_opt_out["skipped"] is True
    assert res_opt_out["sent"] == 0
    assert sent_calls == []
