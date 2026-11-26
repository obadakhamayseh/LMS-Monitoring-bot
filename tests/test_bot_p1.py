import asyncio
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cryptography.fernet import Fernet
import pytest
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter

# Must be set BEFORE importing project modules (never touch live DB or real .env)
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import bot  # noqa: E402

BOT_SRC = Path(__file__).resolve().parent.parent / "bot.py"


class _TagBalanceChecker(HTMLParser):
    """Verify every opened HTML tag in a chunk is closed in LIFO order and no broken tags exist."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text_parts: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag.lower())

    def handle_endtag(self, tag):
        t = tag.lower()
        if not self.stack or self.stack[-1] != t:
            self.errors.append(f"Unexpected </{t}> with stack {self.stack!r}")
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text_parts.append(data)


def _validate_html_chunk(chunk: str) -> str:
    # Ensure no dangling '<' or unclosed tag fragment
    assert not re.search(r"<[^>]*$", chunk), f"Dangling unclosed tag in chunk: {chunk!r}"
    assert not re.search(r"&[a-zA-Z0-9#]+$", chunk), f"Truncated entity at end of chunk: {chunk!r}"
    checker = _TagBalanceChecker()
    checker.feed(chunk)
    checker.close()
    assert not checker.errors, f"HTML tag mismatch in chunk: {checker.errors} | {chunk!r}"
    assert not checker.stack, f"Unclosed tags {checker.stack} in chunk: {chunk!r}"
    return "".join(checker.text_parts)


def _make_callback_update(data: str, user_id: int = 1):
    msg = MagicMock()
    msg.message_id = 42
    msg.chat_id = user_id
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
    "full_name": "Test <Student> & Co",
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


# ── H12: telegram_utils.py ────────────────────────────────────────────────────

def test_h12_escape_html():
    import telegram_utils as tu

    assert tu.escape_html('<script>alert("x") & \'y\'</script>') == (
        "&lt;script&gt;alert(&quot;x&quot;) &amp; &#x27;y&#x27;&lt;/script&gt;"
    )
    assert tu.escape_html(None) == ""
    assert tu.escape_html(12345) == "12345"


def test_h12_split_html_message_preserves_content_and_balances_tags():
    import telegram_utils as tu

    # Long message inside nested <b><i> and with <a> + entities that exceeds max_len
    lines = [
        (
            f"Line {i}: &lt;Assignment &amp; Quiz #{i}&gt; — "
            f'<a href="https://mulms.mutah.edu.jo/mod/assign/view.php?id={i}&amp;x=1">Open #{i}</a>'
        )
        for i in range(1, 80)
    ]
    full_html = "<b><i>" + "\n".join(lines) + "</i></b>"

    chunks = tu.split_html_message(full_html, max_len=350)
    assert len(chunks) > 5

    reconstructed_visible = []
    for chunk in chunks:
        assert len(chunk) <= 350, f"Chunk exceeded max_len: {len(chunk)}"
        reconstructed_visible.append(_validate_html_chunk(chunk))

    orig_visible = _validate_html_chunk(full_html)
    # Normalize whitespace around chunk boundary joins (each chunk is a separate Telegram message)
    assert re.sub(r"\s+", " ", "\n".join(reconstructed_visible)).strip() == re.sub(r"\s+", " ", orig_visible).strip()


def test_h12_split_html_message_never_splits_inside_entity_or_tag_on_long_line():
    import telegram_utils as tu

    # Single huge line without newlines, packed with entities and inline <code>/<a> tags
    piece = '<code>A&amp;B&lt;C&gt;</code> <a href="https://example.com/item?a=1&amp;b=2">LinkText</a> '
    long_line = piece * 40

    chunks = tu.split_html_message(long_line, max_len=120)
    assert len(chunks) > 5
    visible_parts = [_validate_html_chunk(c) for c in chunks]
    orig_visible = _validate_html_chunk(long_line)
    assert re.sub(r"\s+", "", "".join(visible_parts)) == re.sub(r"\s+", "", orig_visible)


def test_h12_send_html_message_retries_after_retry_after(monkeypatch):
    import telegram_utils as tu

    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(tu.asyncio, "sleep", fake_sleep)

    fake_bot = MagicMock()
    calls = {"n": 0}

    async def fake_send_message(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RetryAfter(2)
        return MagicMock()

    fake_bot.send_message = AsyncMock(side_effect=fake_send_message)

    ok = asyncio.run(
        tu.send_html_message(fake_bot, 999, "<b>Hello</b>", max_retries=3)
    )
    assert ok is True
    assert calls["n"] == 3
    assert sleeps == [2.5, 2.5]
    assert fake_bot.send_message.await_args.kwargs["parse_mode"] == ParseMode.HTML


def test_h12_send_html_message_returns_false_when_retries_exhausted(monkeypatch):
    import telegram_utils as tu

    monkeypatch.setattr(tu.asyncio, "sleep", AsyncMock())
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(side_effect=RetryAfter(1))

    ok = asyncio.run(
        tu.send_html_message(fake_bot, 999, "<b>Hello</b>", max_retries=2)
    )
    assert ok is False
    assert fake_bot.send_message.await_count == 3  # initial + 2 retries


def test_h12_send_html_message_handles_forbidden_sync_and_async():
    import telegram_utils as tu

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(side_effect=Forbidden("bot was blocked by the user"))

    sync_called = []
    ok_sync = asyncio.run(
        tu.send_html_message(
            fake_bot, 777, "<b>Hi</b>\n" * 500, on_forbidden=lambda cid: sync_called.append(cid)
        )
    )
    assert ok_sync is False
    assert sync_called == [777]
    # Must stop immediately after Forbidden without sending remaining chunks
    assert fake_bot.send_message.await_count == 1

    fake_bot.send_message.reset_mock()
    async_called = []

    async def _async_cb(cid):
        async_called.append(cid)

    ok_async = asyncio.run(
        tu.send_html_message(fake_bot, 888, "<b>Hi</b>", on_forbidden=_async_cb)
    )
    assert ok_async is False
    assert async_called == [888]


def test_h12_bot_has_zero_markdown_and_zero_3900_truncation():
    src = BOT_SRC.read_text(encoding="utf-8")
    assert "ParseMode.MARKDOWN" not in src, "bot.py must not use ParseMode.MARKDOWN"
    assert 'parse_mode="Markdown"' not in src and "parse_mode='Markdown'" not in src
    assert "[:3900]" not in src, "bot.py must not silently truncate messages with [:3900]"


def test_h12_bot_long_grades_view_splits_instead_of_truncating(monkeypatch):
    many_grades = [
        {
            "course_name": f"Course {i} <CS&101>",
            "item_name": f"Exam {i} [Midterm]",
            "grade_val": "95",
            "grade_max": "100",
            "percentage": "95%",
            "feedback": "Great work & keep it up! " * 3,
            "item_url": f"https://mulms.mutah.edu.jo/grade/item/{i}",
        }
        for i in range(1, 60)
    ]
    monkeypatch.setattr(bot.db, "get_user_grades", lambda tid: many_grades)

    msg = MagicMock()
    msg.chat_id = 1
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()

    asyncio.run(bot._render_grades_view(msg, 1, edit=True))

    sent_chunks = []
    if msg.edit_text.await_count:
        for c in msg.edit_text.await_args_list:
            assert c.kwargs.get("parse_mode") == ParseMode.HTML
            sent_chunks.append(c.kwargs["text"])
    for c in msg.reply_text.await_args_list:
        assert c.kwargs.get("parse_mode") == ParseMode.HTML
        sent_chunks.append(c.kwargs["text"])

    assert len(sent_chunks) >= 2, "Long grades view must split across multiple messages"
    combined = "\n".join(sent_chunks)
    assert "Exam 1 " in combined and "Exam 59 " in combined, "Must preserve first and last items without truncation"
    assert "&lt;CS&amp;101&gt;" in combined, "HTML special chars must be escaped"
    for chunk in sent_chunks:
        _validate_html_chunk(chunk)


# ── H5: att_url in attendance rendering ───────────────────────────────────────

def test_h5_attendance_view_uses_att_url(monkeypatch):
    att_rows = [
        {
            "course_name": "Algorithms & Data Structures",
            "taken_sessions": 10,
            "total_sessions": 15,
            "present_count": 9,
            "absent_count": 1,
            "late_count": 0,
            "excused_count": 0,
            "points_str": "9 / 10",
            "percentage_str": "90%",
            "att_url": "https://mulms.mutah.edu.jo/mod/attendance/view.php?id=555",
            "sessions_json": '[{"date": "2026-03-01", "status": "Present"}]',
        }
    ]
    monkeypatch.setattr(bot.db, "get_user_attendance", lambda tid: att_rows)

    msg = MagicMock()
    msg.chat_id = 1
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()

    asyncio.run(bot._render_attendance_view(msg, 1, edit=True))

    assert msg.edit_text.await_count == 1
    kwargs = msg.edit_text.await_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML
    text = kwargs["text"]
    assert 'href="https://mulms.mutah.edu.jo/mod/attendance/view.php?id=555"' in text
    assert "Algorithms &amp; Data Structures" in text
    _validate_html_chunk(text)


def test_h5_attendance_view_handles_missing_att_url_cleanly(monkeypatch):
    att_rows = [
        {
            "course_name": "Physics 101",
            "taken_sessions": 2,
            "total_sessions": 10,
            "present_count": 2,
            "absent_count": 0,
            "late_count": 0,
            "excused_count": 0,
            "points_str": "",
            "percentage_str": "100%",
            "att_url": "",
            "sessions_json": "[]",
        }
    ]
    monkeypatch.setattr(bot.db, "get_user_attendance", lambda tid: att_rows)

    msg = MagicMock()
    msg.chat_id = 1
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()

    asyncio.run(bot._render_attendance_view(msg, 1, edit=True))

    text = msg.edit_text.await_args.kwargs["text"]
    assert 'href=""' not in text
    assert "Physics 101" in text
    _validate_html_chunk(text)


# ── H4: db.init_db() BEFORE web_thread.start() + attach_loop ─────────────────

def test_h4_main_calls_init_db_before_web_thread_start(monkeypatch):
    order = []

    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setattr(bot.db, "init_db", lambda: order.append("init_db"))

    class FakeMonitorEngine:
        def __init__(self, tg_bot, rem_engine):
            pass

        def attach_loop(self, loop):
            pass

        async def start(self):
            order.append("monitor_start")

    class FakeReminderEngine:
        def __init__(self, tg_bot):
            pass

        async def start(self):
            order.append("reminder_start")

    async def fake_start_webapp(**kwargs):
        order.append("web_server_start")
        return None

    monkeypatch.setattr(bot, "MonitorEngine", FakeMonitorEngine)
    monkeypatch.setattr(bot, "ReminderEngine", FakeReminderEngine)
    monkeypatch.setattr(bot, "init_webapp_engines", lambda e, r: order.append("init_webapp_engines"))
    monkeypatch.setattr(bot, "start_webapp_server_async", fake_start_webapp)
    monkeypatch.setattr(bot, "get_webapp_url", lambda: "")

    captured_post_init = {}
    fake_app = MagicMock()
    fake_app.bot = SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock())
    fake_app.bot_data = {}
    fake_app.add_handler = MagicMock()
    fake_app.add_error_handler = MagicMock()

    def _run_polling(**kw):
        order.append("run_polling")
        if captured_post_init.get("fn"):
            asyncio.run(captured_post_init["fn"](fake_app))

    fake_app.run_polling = MagicMock(side_effect=_run_polling)

    class FakeBuilder:
        def token(self, t): return self
        def post_init(self, fn):
            captured_post_init["fn"] = fn
            return self
        def post_shutdown(self, fn): return self
        def build(self): return fake_app

    monkeypatch.setattr(bot.Application, "builder", staticmethod(lambda: FakeBuilder()))

    bot.main()

    assert "init_db" in order and "web_server_start" in order
    assert order.index("init_db") < order.index("web_server_start"), (
        f"db.init_db() MUST run before web server starts; got order {order}"
    )


def test_h4_post_init_attaches_running_loop_to_engine(monkeypatch):
    attached_loops = []
    started = []

    class FakeMonitorEngine:
        def __init__(self, tg_bot, rem_engine):
            self.bot = tg_bot
            self.rem_engine = rem_engine

        def attach_loop(self, loop):
            attached_loops.append(loop)

        async def start(self):
            started.append("monitor")

    class FakeReminderEngine:
        def __init__(self, tg_bot):
            self.bot = tg_bot

        async def start(self):
            started.append("reminder")

    monkeypatch.setattr(bot, "MonitorEngine", FakeMonitorEngine)
    monkeypatch.setattr(bot, "ReminderEngine", FakeReminderEngine)
    init_webapp_calls = []
    monkeypatch.setattr(bot, "init_webapp_engines", lambda e, r: init_webapp_calls.append((e, r)))
    webapp_starts = []

    async def fake_start_webapp(**kwargs):
        webapp_starts.append(asyncio.get_running_loop())
        return None

    monkeypatch.setattr(bot, "start_webapp_server_async", fake_start_webapp)
    monkeypatch.setattr(bot, "get_webapp_url", lambda: "")

    fake_app = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock()),
        bot_data={},
    )

    async def _run():
        expected_loop = asyncio.get_running_loop()
        await bot.post_init(fake_app)
        assert attached_loops == [expected_loop], "post_init must attach the running asyncio loop to MonitorEngine"
        assert webapp_starts == [expected_loop], "post_init must start FastAPI on the same running asyncio loop"

    asyncio.run(_run())
    assert started == ["monitor", "reminder"]
    assert len(init_webapp_calls) == 1


# ── H13: non-blocking executor for logout/DB + graceful shutdown ─────────────

def test_h13_confirm_logout_runs_delete_user_in_executor(monkeypatch):
    executor_calls = []
    deleted = []
    stopped = []

    class FakeEngine:
        async def stop_user(self, tid):
            stopped.append(tid)

    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    monkeypatch.setattr(bot.db, "delete_user", lambda tid: deleted.append(tid))
    monkeypatch.setattr(bot, "_send", AsyncMock())

    update, q = _make_callback_update("confirm_logout", user_id=1)
    ctx = _make_ctx(bot_data={"engine": FakeEngine()})

    async def _run():
        loop = asyncio.get_running_loop()
        orig_run = loop.run_in_executor

        def spy_run_in_executor(executor, func, *args):
            executor_calls.append(getattr(func, "__name__", repr(func)))
            return orig_run(executor, func, *args)

        monkeypatch.setattr(loop, "run_in_executor", spy_run_in_executor)
        await bot.handle_callback(update, ctx)

    asyncio.run(_run())
    assert stopped == [1]
    assert deleted == [1]
    assert len(executor_calls) >= 1, "Blocking DB calls in confirm_logout must run in executor"


def test_h13_post_shutdown_awaits_engine_stop_and_tasks_before_db_close(monkeypatch):
    order = []

    class FakeEngine:
        async def stop(self):
            await asyncio.sleep(0.01)
            order.append("engine_stop")

    class FakeReminder:
        async def stop(self):
            await asyncio.sleep(0.01)
            order.append("reminder_stop")

    monkeypatch.setattr(bot.db, "close", lambda: order.append("db_close"), raising=False)

    fake_app = SimpleNamespace(
        bot_data={"engine": FakeEngine(), "reminder_engine": FakeReminder()}
    )

    async def _bg_work():
        await asyncio.sleep(0.01)
        order.append("bg_task_done")

    async def _run():
        if hasattr(bot, "_track_background_task"):
            bot._track_background_task(asyncio.create_task(_bg_work()))
        await bot.post_shutdown(fake_app)

    asyncio.run(_run())
    assert "engine_stop" in order and "reminder_stop" in order and "db_close" in order
    assert order.index("engine_stop") < order.index("db_close"), (
        f"engine.stop() must finish before db.close(): {order}"
    )
    assert order.index("reminder_stop") < order.index("db_close"), (
        f"reminder.stop() must finish before db.close(): {order}"
    )
    if hasattr(bot, "_track_background_task"):
        assert order.index("bg_task_done") < order.index("db_close"), (
            f"bg tasks must finish before db.close(): {order}"
        )


# ── H16: check_interval bounds 5..1440 ───────────────────────────────────────

@pytest.mark.parametrize("bad_cb", ["iv_0", "iv_1", "iv_2", "iv_4", "iv_-5", "iv_1441", "iv_9999", "iv_abc"])
def test_h16_check_interval_rejects_out_of_range_and_invalid(monkeypatch, bad_cb):
    updated = []
    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    monkeypatch.setattr(bot.db, "update_check_interval", lambda tid, mins: updated.append(mins))
    monkeypatch.setattr(bot, "_render_settings", AsyncMock())

    update, q = _make_callback_update(bad_cb, user_id=1)
    ctx = _make_ctx()

    asyncio.run(bot.handle_callback(update, ctx))

    assert updated == [], f"{bad_cb} must be rejected without calling db.update_check_interval"
    assert q.answer.await_count == 1
    assert q.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.parametrize("good_cb,expected", [("iv_5", 5), ("iv_10", 10), ("iv_60", 60), ("iv_1440", 1440)])
def test_h16_check_interval_accepts_valid_bounds(monkeypatch, good_cb, expected):
    updated = []
    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    monkeypatch.setattr(bot.db, "update_check_interval", lambda tid, mins: updated.append(mins))
    monkeypatch.setattr(bot, "_render_settings", AsyncMock())

    update, q = _make_callback_update(good_cb, user_id=1)
    ctx = _make_ctx()

    asyncio.run(bot.handle_callback(update, ctx))

    assert updated == [expected]
    assert q.answer.await_count == 1
    assert q.answer.await_args.kwargs.get("show_alert") is True


def test_h16_settings_keyboard_has_no_interval_below_5_or_above_1440(monkeypatch):
    captured_kb = []

    async def fake_send(obj, text, keyboard=None, edit=False):
        captured_kb.append(keyboard)

    monkeypatch.setattr(bot, "_send", fake_send)
    msg = MagicMock()
    asyncio.run(bot._render_settings(msg, FAKE_USER, edit=True))

    assert captured_kb
    iv_values = []
    for row in captured_kb[0]:
        for btn in row:
            cb = getattr(btn, "callback_data", "") or ""
            if cb.startswith("iv_"):
                iv_values.append(int(cb.split("_", 1)[1]))
    assert iv_values, "Settings must expose check_interval buttons"
    assert all(5 <= v <= 1440 for v in iv_values), f"All interval buttons must be in 5..1440; got {iv_values}"


def test_h12_safe_edit_or_reply_html_retries_and_handles_forbidden(monkeypatch):
    import telegram_utils as tu

    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(tu.asyncio, "sleep", fake_sleep)

    msg = MagicMock()
    msg.chat_id = 55
    attempts = {"n": 0}

    async def flaky_edit(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RetryAfter(1)
        return MagicMock()

    msg.edit_text = AsyncMock(side_effect=flaky_edit)
    msg.reply_text = AsyncMock()

    ok = asyncio.run(tu.safe_edit_or_reply_html(msg, "<b>OK</b>", edit=True))
    assert ok is True
    assert sleeps == [1.5]
    assert msg.edit_text.await_args.kwargs["parse_mode"] == ParseMode.HTML

    msg2 = MagicMock()
    msg2.chat_id = 66
    msg2.reply_text = AsyncMock(side_effect=Forbidden("blocked"))
    forbidden_ids = []
    ok2 = asyncio.run(
        tu.safe_edit_or_reply_html(msg2, "<b>Hi</b>", edit=False, on_forbidden=lambda cid: forbidden_ids.append(cid))
    )
    assert ok2 is False
    assert forbidden_ids == [66]


def test_h4_and_h13_confirm_logout_uses_real_monitor_engine_public_api(monkeypatch):
    from monitor import MonitorEngine

    real_engine = MonitorEngine(MagicMock(), None)
    called = []

    async def spy_invalidate(tid, logout=True):
        called.append((tid, logout))

    monkeypatch.setattr(real_engine, "invalidate_user_session_async", spy_invalidate)
    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    deleted = []
    monkeypatch.setattr(bot.db, "delete_user", lambda tid: deleted.append(tid))
    monkeypatch.setattr(bot, "_send", AsyncMock())

    update, q = _make_callback_update("confirm_logout", user_id=1)
    ctx = _make_ctx(bot_data={"engine": real_engine})

    asyncio.run(bot.handle_callback(update, ctx))

    assert called == [(1, True)]
    assert deleted == [1]
    assert q.answer.await_count == 1

