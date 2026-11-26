import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cryptography.fernet import Fernet
import pytest
from telegram.error import BadRequest
from telegram.ext import ConversationHandler

# Must be set BEFORE importing project modules (never touch live DB or real .env)
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import bot  # noqa: E402


def _make_callback_update(data: str, user_id: int = 1):
    msg = MagicMock()
    msg.message_id = 42
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.get_bot = MagicMock(return_value=MagicMock())

    q = MagicMock()
    q.data = data
    q.message = msg
    q.answer = AsyncMock()

    update = MagicMock()
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = SimpleNamespace(id=user_id)
    update.callback_query = q
    update.message = None
    return update, q


def _make_ctx(user_data=None, bot_data=None):
    return SimpleNamespace(
        user_data={} if user_data is None else user_data,
        bot_data={} if bot_data is None else bot_data,
        error=None,
    )


FAKE_USER = {
    "telegram_id": 1,
    "lms_username": "20240001",
    "lms_password": "secret",
    "full_name": "Test Student",
    "is_monitoring": 0,
    "check_interval": 10,
    "last_check": "2026-01-01 10:00",
    "notify_assign": 1,
    "notify_quiz": 1,
    "notify_resource": 1,
    "notify_grade": 1,
    "notify_announcement": 1,
    "notify_attendance": 1,
}


@pytest.mark.parametrize(
    "callback_data,user_obj,courses_obj,expect_alert",
    [
        ("main_menu", FAKE_USER, [], False),
        ("my_courses", FAKE_USER, [], False),
        ("sync_courses", None, [], True),
        ("start_monitor", None, [], True),
        ("start_monitor", FAKE_USER, [], True),
        ("stop_monitor", None, [], True),
        ("deadlines", None, [], True),
        ("check_now", None, [], True),
        ("check_now", FAKE_USER, [], True),
        ("stats", None, [], True),
        ("grades_menu", None, [], True),
        ("refresh_grades", FAKE_USER, [], True),
        ("announcements_menu", None, [], True),
        ("attendance_menu", None, [], True),
        ("settings", None, [], True),
        ("iv_10", FAKE_USER, [], True),
        ("toggle_101", FAKE_USER, [], False),
        ("cToggle_101", None, [], True),
        ("cToggle_101", FAKE_USER, [], False),
        ("confirmDel_101", FAKE_USER, [], True),
        ("confirm_clear_all_courses", FAKE_USER, [], True),
        ("view_101", None, [], True),
        ("refresh_101", None, [], True),
        ("logout_prompt", FAKE_USER, [], False),
        ("unrecognized_callback_xyz", FAKE_USER, [], False),
    ],
)
def test_handle_callback_answers_exactly_once(
    monkeypatch, callback_data, user_obj, courses_obj, expect_alert
):
    monkeypatch.setattr(bot.db, "get_user", lambda tid: user_obj)
    monkeypatch.setattr(bot.db, "get_user_courses", lambda tid, monitored_only=False: courses_obj)
    monkeypatch.setattr(bot.db, "update_check_interval", lambda tid, mins: None)
    monkeypatch.setattr(bot.db, "toggle_course", lambda tid, cid: None)
    monkeypatch.setattr(bot.db, "remove_course", lambda tid, cid: None)
    monkeypatch.setattr(bot.db, "clear_all_user_courses", lambda tid: None)

    monkeypatch.setattr(bot, "_show_main", AsyncMock())
    monkeypatch.setattr(bot, "_render_courses", AsyncMock())
    monkeypatch.setattr(bot, "_render_settings", AsyncMock())
    monkeypatch.setattr(bot, "_fetch_show_course", AsyncMock())
    monkeypatch.setattr(bot, "_send", AsyncMock())

    update, q = _make_callback_update(callback_data, user_id=1)
    ctx = _make_ctx()

    asyncio.run(bot.handle_callback(update, ctx))

    assert q.answer.await_count == 1, f"{callback_data}: expected 1 answer, got {q.answer.await_count}"
    if expect_alert:
        kwargs = q.answer.await_args.kwargs
        assert kwargs.get("show_alert") is True, f"{callback_data}: expected show_alert=True"


def test_add_course_start_answers_exactly_once_when_not_logged_in(monkeypatch):
    monkeypatch.setattr(bot.db, "get_user", lambda tid: None)
    monkeypatch.setattr(bot, "_send", AsyncMock())

    update, q = _make_callback_update("add_course_start", user_id=1)
    ctx = _make_ctx()

    res = asyncio.run(bot.add_course_start(update, ctx))

    assert res == ConversationHandler.END
    assert q.answer.await_count == 1
    assert q.answer.await_args.kwargs.get("show_alert") is True


def test_add_course_start_answers_exactly_once_when_logged_in(monkeypatch):
    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    monkeypatch.setattr(bot, "_send", AsyncMock())

    update, q = _make_callback_update("add_course_start", user_id=1)
    ctx = _make_ctx()

    res = asyncio.run(bot.add_course_start(update, ctx))

    assert res == bot.STATE_ADD_COURSE
    assert q.answer.await_count == 1


def test_global_error_handler_ignores_old_query_and_logs_other_errors(caplog):
    assert hasattr(bot, "error_handler"), "bot.error_handler must exist"

    ctx_old = _make_ctx()
    ctx_old.error = BadRequest("Query is too old and response timeout expired or query id is invalid")
    with caplog.at_level(logging.ERROR, logger=bot.logger.name):
        asyncio.run(bot.error_handler(None, ctx_old))
    assert not caplog.records

    ctx_boom = _make_ctx()
    ctx_boom.error = RuntimeError("unexpected boom")
    with caplog.at_level(logging.ERROR, logger=bot.logger.name):
        asyncio.run(bot.error_handler({"update_id": 9}, ctx_boom))
    assert any("unexpected boom" in rec.getMessage() and rec.exc_info for rec in caplog.records)


def test_login_got_pass_deletes_message_even_when_username_missing(monkeypatch):
    msg = MagicMock()
    msg.text = "  MySecret١٢٣  "
    msg.delete = AsyncMock()
    msg.reply_text = AsyncMock()

    tg_bot = MagicMock()
    tg_bot.edit_message_text = AsyncMock()

    update = MagicMock()
    update.message = msg
    update.effective_user = SimpleNamespace(id=1)
    update.effective_chat = SimpleNamespace(id=1)
    update.get_bot = MagicMock(return_value=tg_bot)

    ctx = _make_ctx(user_data={"login_msg_id": 99})  # missing lms_user

    res = asyncio.run(bot.login_got_pass(update, ctx))

    assert res == ConversationHandler.END
    assert msg.delete.await_count == 1


def test_login_got_pass_preserves_password_bytes_and_deletes_before_login(monkeypatch):
    raw_password = "  P@ss ١٢٣٤ word \t"
    events = []

    async def _tracked_delete():
        events.append("delete")

    msg = MagicMock()
    msg.text = raw_password
    msg.delete = AsyncMock(side_effect=_tracked_delete)
    msg.reply_text = AsyncMock()

    tg_bot = MagicMock()
    tg_bot.edit_message_text = AsyncMock()

    update = MagicMock()
    update.message = msg
    update.effective_user = SimpleNamespace(id=1)
    update.effective_chat = SimpleNamespace(id=1)
    update.get_bot = MagicMock(return_value=tg_bot)

    captured = {}

    class DummyScraper:
        def __init__(self, username, password):
            events.append("scraper_init")
            captured["username"] = username
            captured["password"] = password
            self.user_id = "77"
            self.full_name = "Test Student"
            self.error_message = ""

        def login(self):
            events.append("scraper_login")
            return True

        def close(self):
            events.append("scraper_close")

    monkeypatch.setattr(bot, "LMSScraper", DummyScraper)
    saved = {}

    def _fake_save_user(tid, username, password, uid, full_name):
        saved["password"] = password

    monkeypatch.setattr(bot.db, "save_user", _fake_save_user)
    monkeypatch.setattr(bot.db, "get_user_courses", lambda tid, monitored_only=False: [])
    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    monkeypatch.setattr(bot, "_ensure_user_menu_button", AsyncMock())

    ctx = _make_ctx(user_data={"lms_user": "20240001", "login_msg_id": 99})

    res = asyncio.run(bot.login_got_pass(update, ctx))

    assert res == ConversationHandler.END
    assert msg.delete.await_count == 1
    assert events[0] == "delete", f"Password message must be deleted FIRST; got order {events}"
    assert captured["password"] == raw_password
    assert saved["password"] == raw_password


def test_login_got_pass_logs_and_warns_user_when_delete_fails(monkeypatch, caplog):
    msg = MagicMock()
    msg.text = "secret123"
    msg.delete = AsyncMock(side_effect=RuntimeError("no permission to delete"))
    msg.reply_text = AsyncMock()

    tg_bot = MagicMock()
    tg_bot.edit_message_text = AsyncMock()

    update = MagicMock()
    update.message = msg
    update.effective_user = SimpleNamespace(id=1)
    update.effective_chat = SimpleNamespace(id=1)
    update.get_bot = MagicMock(return_value=tg_bot)

    ctx = _make_ctx(user_data={"login_msg_id": 99})  # missing username triggers early return after delete

    with caplog.at_level(logging.WARNING, logger=bot.logger.name):
        res = asyncio.run(bot.login_got_pass(update, ctx))

    assert res == ConversationHandler.END
    assert msg.delete.await_count == 1
    assert any("no permission to delete" in rec.getMessage() for rec in caplog.records)
    assert msg.reply_text.await_count == 1
    warn_text = msg.reply_text.await_args.args[0]
    assert "حذف" in warn_text
