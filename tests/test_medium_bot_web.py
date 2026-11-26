"""Prove-It tests for Medium/Low items: M-GUARD, M-REF (bot logging), M-VALID, M-UTC (API serialization)."""

from __future__ import annotations

import os
from cryptography.fernet import Fernet

# Configure isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import ast
import asyncio
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import urllib.parse

from fastapi.testclient import TestClient
import pytest
from telegram import Update
from telegram.ext import ApplicationHandlerStop, TypeHandler

import bot
import database as db
from handlers import register_handlers
from handlers import common as h_common
import webapp_server

ROOT = Path(__file__).resolve().parent.parent
TEST_BOT_TOKEN = "123456:TEST"
TEST_OWNER_ID = 1


def _signed_init(user_id: int = TEST_OWNER_ID, auth_date: int | None = None) -> str:
    ts = int(time.time()) if auth_date is None else auth_date
    params = {
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
        "auth_date": str(ts),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_medium_bot_web.db")
    monkeypatch.setattr(db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    monkeypatch.setattr(webapp_server, "TELEGRAM_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setattr(webapp_server, "OWNER_TELEGRAM_ID", TEST_OWNER_ID)
    monkeypatch.setattr(webapp_server, "_ENGINE", None)
    monkeypatch.setattr(webapp_server, "_REMINDER_ENGINE", None)
    webapp_server.reset_rate_limits()

    db.init_db()
    db.save_user(TEST_OWNER_ID, "20240001", "secret_pass", "99", "Test Owner")

    with TestClient(webapp_server.app) as client:
        yield client


# ── M-GUARD: owner_guard, TypeHandler(group=-1), owner-only set_chat_menu_button ──


def test_m_guard_owner_guard_blocks_non_owner_and_missing_user_without_leaking_text(caplog):
    assert hasattr(h_common, "owner_guard"), "handlers.common.owner_guard must exist"

    ctx = SimpleNamespace(user_data={}, bot_data={})

    # 1. Non-owner update with sensitive message text must raise ApplicationHandlerStop and log ONLY user_id
    stranger_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999999),
        message=SimpleNamespace(text="SUPER_SECRET_PASSWORD_123"),
        callback_query=None,
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="bot.handlers.common"):
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(h_common.owner_guard(stranger_update, ctx))

    assert any("999999" in rec.getMessage() for rec in caplog.records), "Unauthorized attempt must log user_id"
    assert all("SUPER_SECRET_PASSWORD_123" not in rec.getMessage() for rec in caplog.records), (
        "owner_guard must never log sensitive message text"
    )

    # 2. Missing effective_user must raise ApplicationHandlerStop
    anon_update = SimpleNamespace(
        effective_user=None,
        message=SimpleNamespace(text="ANON_SECRET_TEXT"),
        callback_query=None,
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="bot.handlers.common"):
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(h_common.owner_guard(anon_update, ctx))
    assert caplog.records, "Missing user update should emit unauthorized warning"
    assert all("ANON_SECRET_TEXT" not in rec.getMessage() for rec in caplog.records)

    # 3. Owner update passes cleanly without raising
    owner_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=int(h_common.OWNER_TELEGRAM_ID)),
        message=SimpleNamespace(text="/start"),
        callback_query=None,
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="bot.handlers.common"):
        result = asyncio.run(h_common.owner_guard(owner_update, ctx))
    assert result is None
    assert not caplog.records


def test_m_guard_register_handlers_registers_type_handler_in_group_minus_1():
    calls: list[tuple[object, dict]] = []

    fake_app = MagicMock()
    fake_app.add_handler = MagicMock(side_effect=lambda handler, *args, **kwargs: calls.append((handler, kwargs)))
    fake_app.add_error_handler = MagicMock()

    register_handlers(fake_app)

    guard_entries = [
        (h, kw)
        for h, kw in calls
        if isinstance(h, TypeHandler) and kw.get("group") == -1
    ]
    assert len(guard_entries) == 1, f"Expected exactly one TypeHandler in group=-1; got {calls}"
    guard_handler = guard_entries[0][0]
    assert guard_handler.type is Update
    assert guard_handler.callback is h_common.owner_guard


def test_m_guard_post_init_restricts_set_chat_menu_button_to_owner_chat_id(monkeypatch):
    class FakeMonitorEngine:
        def __init__(self, tg_bot, rem_engine):
            pass

        def attach_loop(self, loop):
            pass

        async def start(self):
            pass

    class FakeReminderEngine:
        def __init__(self, tg_bot):
            pass

        async def start(self):
            pass

    async def fake_start_webapp(**kwargs):
        return None

    monkeypatch.setattr(bot, "OWNER_TELEGRAM_ID", 777001)
    monkeypatch.setattr(bot, "MonitorEngine", FakeMonitorEngine)
    monkeypatch.setattr(bot, "ReminderEngine", FakeReminderEngine)
    monkeypatch.setattr(bot, "init_webapp_engines", lambda e, r: None)
    monkeypatch.setattr(bot, "start_webapp_server_async", fake_start_webapp)
    monkeypatch.setattr(bot, "get_webapp_url", lambda: "https://example.mutah.edu.jo/webapp")
    monkeypatch.setattr(h_common, "_ENGINE", None)
    monkeypatch.setattr(h_common, "_REMINDER_ENGINE", None)

    set_menu_mock = AsyncMock()
    fake_app = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=set_menu_mock),
        bot_data={},
    )

    try:
        asyncio.run(bot.post_init(fake_app))
    finally:
        h_common.set_engine(None)
        h_common.set_reminder_engine(None)

    assert set_menu_mock.await_count == 1
    kwargs = set_menu_mock.await_args.kwargs
    assert kwargs.get("chat_id") == 777001, (
        f"post_init must restrict set_chat_menu_button to chat_id=OWNER_TELEGRAM_ID; got {kwargs}"
    )
    assert kwargs.get("menu_button") is not None


# ── M-REF (bot.py logging): RotatingFileHandler ───────────────────────────────


def test_m_ref_bot_uses_rotating_file_handler_not_plain_file_handler():
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="bot.py")

    plain_file_handler_calls = []
    rotating_calls = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
            if name == "FileHandler":
                plain_file_handler_calls.append(node.lineno)
            elif name == "RotatingFileHandler":
                rotating_calls.append(node)

    assert plain_file_handler_calls == [], f"Plain FileHandler must be removed from bot.py: {plain_file_handler_calls}"
    assert len(rotating_calls) == 1, "bot.py must configure RotatingFileHandler"

    # Inspect actual configured handler object
    handler = getattr(bot, "_BOT_LOG_HANDLER", None)
    assert isinstance(handler, RotatingFileHandler), "bot._BOT_LOG_HANDLER must be a RotatingFileHandler instance"
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3
    assert (handler.encoding or "").lower() == "utf-8"
    assert Path(handler.baseFilename).name == "bot.log"


# ── M-VALID: strict course_id validation, check_interval bounds, 4xx/5xx logging ──


@pytest.mark.parametrize(
    "endpoint",
    ["/api/add-course", "/api/toggle-course", "/api/delete-course"],
)
@pytest.mark.parametrize(
    "bad_cid",
    ["", "   ", "abc", "-5", -5, "0", 0, "12; DROP", "../1", "1000000000", 1_000_000_000, True],
)
def test_m_valid_rejects_invalid_course_id_with_400(api_client, monkeypatch, endpoint, bad_cid):
    class ShouldNotRunScraper:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Scraper must never be invoked when course_id fails validation")

    monkeypatch.setattr(webapp_server, "LMSScraper", ShouldNotRunScraper)
    init_data = _signed_init()
    resp = api_client.post(
        endpoint,
        json={"course_id": bad_cid},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 400, f"{endpoint} with course_id={bad_cid!r} returned {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is False


@pytest.mark.parametrize(
    "bad_cid",
    ["", "   ", "abc", "-5", -5, "0", 0, "12; DROP", "../1", "1000000000", 1_000_000_000, True],
)
def test_m_valid_mark_submitted_rejects_invalid_course_id_when_provided(api_client, bad_cid):
    init_data = _signed_init()
    resp = api_client.post(
        "/api/mark-submitted",
        json={"item_key": "assign_101", "course_id": bad_cid, "is_submitted": 1},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 400, f"/api/mark-submitted with course_id={bad_cid!r} returned {resp.status_code}"
    assert resp.json()["success"] is False


def test_m_valid_mark_submitted_allows_omitted_or_valid_course_id(api_client):
    init_data = _signed_init()

    r_none = api_client.post(
        "/api/mark-submitted",
        json={"item_key": "assign_201", "is_submitted": 1},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert r_none.status_code == 200
    assert r_none.json()["success"] is True

    r_valid = api_client.post(
        "/api/mark-submitted",
        json={"item_key": "assign_202", "course_id": "999999999", "is_submitted": 1},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert r_valid.status_code == 200
    assert r_valid.json()["success"] is True


def test_m_valid_add_course_rejects_injected_url_and_accepts_positive_digits(api_client, monkeypatch):
    init_data = _signed_init()

    class StubScraper:
        def __init__(self, u, p):
            self.error_message = ""

        def login(self):
            return True

        def get_course_info(self, cid):
            return f"Course {cid}", {"assignments": [], "quizzes": [], "resources": []}

        def close(self):
            pass

    monkeypatch.setattr(webapp_server, "LMSScraper", StubScraper)

    # Injection disguised inside query param must be rejected with 400
    r_inject = api_client.post(
        "/api/add-course",
        json={"course_id": "https://mulms.mutah.edu.jo/course/view.php?id=12; DROP TABLE users"},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert r_inject.status_code == 400

    # Valid numeric ID succeeds
    r_ok = api_client.post(
        "/api/add-course",
        json={"course_id": "12345"},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["course_id"] == 12345


def test_m_valid_check_interval_strict_bounds_intact(api_client):
    init_data = _signed_init()
    for bad in (4, 0, -1, 1441, "abc", True):
        r = api_client.post(
            "/api/update-settings",
            json={"check_interval": bad},
            headers={"X-Telegram-Init-Data": init_data},
        )
        assert r.status_code == 400, f"Expected 400 for check_interval={bad!r}, got {r.status_code}"

    for good in (5, 30, 1440):
        r = api_client.post(
            "/api/update-settings",
            json={"check_interval": good},
            headers={"X-Telegram-Init-Data": init_data},
        )
        assert r.status_code == 200, f"Expected 200 for check_interval={good!r}, got {r.status_code}"


def test_m_valid_middleware_logs_4xx_warning_and_5xx_error_without_leaking_secrets(
    api_client, monkeypatch, caplog
):
    secret_code = "SECRET_PAIR_CODE_98765432"
    secret_pass = "TOP_SECRET_PASS_XYZ"
    secret_cookie = "SECRET_SESSION_COOKIE_ABC"
    secret_init = "hash=SECRET_INIT_DATA_HASH"

    # 1. Trigger 401 with secret query string, cookie, and initData header -> WARNING log, zero secrets
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=webapp_server.logger.name):
        r_401 = api_client.get(
            f"/api/user-data?code={secret_code}&token=LEAKME",
            headers={
                "X-Forwarded-For": "198.51.100.42",
                "Cookie": f"mutah_session={secret_cookie}",
                "X-Telegram-Init-Data": secret_init,
            },
        )
    assert r_401.status_code == 401
    warn_recs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warn_recs, "4xx response must emit a WARNING log"
    joined_warn = "\n".join(r.getMessage() for r in warn_recs)
    assert "GET" in joined_warn
    assert "/api/user-data" in joined_warn
    assert "401" in joined_warn
    assert "198.51.100.42" in joined_warn
    for secret in (secret_code, "LEAKME", secret_cookie, secret_init, "?code="):
        assert secret not in joined_warn, f"Secret leaked in 4xx log: {secret!r}"

    # 2. Trigger 400 on POST with secret password in body -> WARNING log, zero password leak
    init_data = _signed_init()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=webapp_server.logger.name):
        r_400 = api_client.post(
            f"/api/login?code={secret_code}",
            json={"username": "", "password": secret_pass},
            headers={"X-Telegram-Init-Data": init_data, "X-Forwarded-For": "203.0.113.9"},
        )
    assert r_400.status_code == 400
    joined_400 = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "POST" in joined_400 and "/api/login" in joined_400 and "400" in joined_400 and "203.0.113.9" in joined_400
    assert secret_pass not in joined_400
    assert secret_code not in joined_400

    # 3. Trigger 503 on /health (DB failure) -> ERROR log, zero query leaks
    monkeypatch.setattr(db, "health_check", lambda: False)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=webapp_server.logger.name):
        r_503 = api_client.get(
            f"/health?code={secret_code}",
            headers={"X-Forwarded-For": "192.0.2.55"},
        )
    assert r_503.status_code == 503
    err_recs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert err_recs, "5xx response must emit an ERROR log"
    joined_err = "\n".join(r.getMessage() for r in err_recs)
    assert "GET" in joined_err and "/health" in joined_err and "503" in joined_err and "192.0.2.55" in joined_err
    assert secret_code not in joined_err


# ── M-UTC: /api/user-data normalizes legacy naive last_check to UTC ISO-8601 ──


def test_m_utc_user_data_normalizes_legacy_naive_last_check_and_preserves_iso_utc(api_client):
    init_data = _signed_init()

    # 1. Legacy naive SQLite timestamp string
    with db.DBContext() as ctx:
        ctx.execute(
            "UPDATE users SET last_check=? WHERE telegram_id=?",
            ("2026-11-26 15:00:00", TEST_OWNER_ID),
        )
    r_naive = api_client.get("/api/user-data", headers={"X-Telegram-Init-Data": init_data})
    assert r_naive.status_code == 200
    assert r_naive.json()["user"]["last_check"] == "2026-11-26T15:00:00Z"

    # 2. Already explicit UTC ISO-8601 string with Z
    with db.DBContext() as ctx:
        ctx.execute(
            "UPDATE users SET last_check=? WHERE telegram_id=?",
            ("2026-11-26T15:30:45Z", TEST_OWNER_ID),
        )
    r_iso_z = api_client.get("/api/user-data", headers={"X-Telegram-Init-Data": init_data})
    assert r_iso_z.status_code == 200
    assert r_iso_z.json()["user"]["last_check"] == "2026-11-26T15:30:45Z"

    # 3. Already explicit UTC ISO-8601 string with +00:00
    with db.DBContext() as ctx:
        ctx.execute(
            "UPDATE users SET last_check=? WHERE telegram_id=?",
            ("2026-11-26T15:45:00+00:00", TEST_OWNER_ID),
        )
    r_iso_offset = api_client.get("/api/user-data", headers={"X-Telegram-Init-Data": init_data})
    assert r_iso_offset.status_code == 200
    assert r_iso_offset.json()["user"]["last_check"] == "2026-11-26T15:45:00Z"

    # 4. Empty/NULL last_check still falls back to Arabic placeholder cleanly
    with db.DBContext() as ctx:
        ctx.execute("UPDATE users SET last_check=NULL WHERE telegram_id=?", (TEST_OWNER_ID,))
    r_null = api_client.get("/api/user-data", headers={"X-Telegram-Init-Data": init_data})
    assert r_null.status_code == 200
    assert r_null.json()["user"]["last_check"] == "لم يتم بعد"
