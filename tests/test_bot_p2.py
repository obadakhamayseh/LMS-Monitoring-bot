"""Phase P2 tests for H9 bot decomposition, sync_service delegation, dead code removal, and shared FastAPI loop."""

import ast
import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cryptography.fernet import Fernet
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

# Must be set BEFORE importing project modules (never touch live DB or real .env)
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import bot  # noqa: E402
from handlers import register_handlers  # noqa: E402
from handlers import auth as h_auth  # noqa: E402
from handlers import callbacks as h_callbacks  # noqa: E402
from handlers import common as h_common  # noqa: E402
from handlers import courses as h_courses  # noqa: E402
from handlers import views as h_views  # noqa: E402
from services import sync_service  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BOT_PATH = ROOT / "bot.py"
HANDLERS_DIR = ROOT / "handlers"

FAKE_USER = {
    "telegram_id": 1,
    "lms_username": "20240001",
    "lms_password": "secret",
    "full_name": "Test Student",
    "is_monitoring": 1,
    "check_interval": 10,
    "last_check": "2026-01-01 10:00",
    "notify_assign": 1,
    "notify_quiz": 1,
    "notify_resource": 1,
    "notify_grade": 1,
    "notify_announcement": 1,
    "notify_attendance": 1,
}


def _make_ctx(user_data=None, bot_data=None):
    return SimpleNamespace(
        user_data={} if user_data is None else user_data,
        bot_data={} if bot_data is None else bot_data,
        error=None,
    )


def test_p2_bot_py_is_slim_under_300_lines():
    lines = BOT_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 300, f"bot.py must be < 300 lines; got {len(lines)}"
    assert len(lines) < 250, f"bot.py should stay slim (< 250 lines); got {len(lines)}"


def test_p2_all_handler_modules_import_and_register_cleanly():
    module_names = [
        "handlers",
        "handlers.common",
        "handlers.auth",
        "handlers.courses",
        "handlers.views",
        "handlers.callbacks",
    ]
    for name in module_names:
        mod = importlib.import_module(name)
        assert mod is not None

    registered = []
    err_handlers = []

    fake_app = MagicMock()
    fake_app.add_handler = MagicMock(side_effect=lambda h, *args, **kwargs: registered.append(h))
    fake_app.add_error_handler = MagicMock(side_effect=lambda h: err_handlers.append(h))

    register_handlers(fake_app)

    assert any(isinstance(h, ConversationHandler) for h in registered)
    assert any(isinstance(h, CallbackQueryHandler) for h in registered)
    assert any(isinstance(h, MessageHandler) for h in registered)
    assert err_handlers == [h_common.error_handler]

    commands = set()
    for h in registered:
        if isinstance(h, CommandHandler):
            commands.update(h.commands)
    assert {"start", "courses", "logout", "app", "pair", "unlink", "help"}.issubset(commands)


def test_p2_zero_direct_lms_scraper_instantiations_outside_auth_login():
    """Verify LMSScraper(...) is NEVER instantiated in bot.py or handlers/* except handlers/auth.py login_got_pass."""
    target_files = [BOT_PATH] + sorted(HANDLERS_DIR.glob("*.py"))
    offending = []

    for path in target_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
                if name == "LMSScraper":
                    offending.append(f"{path.name}:{node.lineno}")

    # Only handlers/auth.py (via scraper_cls = resolve_hook("LMSScraper", ...)) may construct LMSScraper
    assert offending == [], f"Unexpected direct LMSScraper(...) calls found: {offending}"

    # Also ensure no handler module outside auth.py imports LMSScraper at all
    for path in sorted(HANDLERS_DIR.glob("*.py")):
        if path.name == "auth.py":
            continue
        src = path.read_text(encoding="utf-8")
        assert "LMSScraper" not in src, f"{path.name} must not reference LMSScraper directly"


def test_p2_no_dead_sync_courses_or_legacy_http_server_or_markdown():
    all_files = [BOT_PATH] + sorted(HANDLERS_DIR.glob("*.py"))
    for path in all_files:
        src = path.read_text(encoding="utf-8")
        assert "sync_courses" not in src, f"Dead sync_courses still referenced in {path.name}"
        assert "ThreadingHTTPServer" not in src, f"Legacy ThreadingHTTPServer referenced in {path.name}"
        assert "BaseHTTPRequestHandler" not in src, f"Legacy BaseHTTPRequestHandler referenced in {path.name}"
        assert "web_thread" not in src, f"Legacy daemon web_thread referenced in {path.name}"
        assert "ParseMode.MARKDOWN" not in src, f"Legacy ParseMode.MARKDOWN referenced in {path.name}"
        assert "[:3900]" not in src, f"Legacy [:3900] truncation referenced in {path.name}"


def test_p2_dead_sync_courses_callback_is_noop_and_answers_once(monkeypatch):
    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    msg = MagicMock()
    msg.message_id = 10
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()

    q = MagicMock()
    q.data = "sync_courses"
    q.message = msg
    q.answer = AsyncMock()

    update = MagicMock()
    update.effective_user = SimpleNamespace(id=1)
    update.effective_chat = SimpleNamespace(id=1)
    update.callback_query = q
    update.message = None

    asyncio.run(h_callbacks.handle_callback(update, _make_ctx()))

    assert q.answer.await_count == 1
    assert msg.edit_text.await_count == 0
    assert msg.reply_text.await_count == 0


def test_p2_add_course_got_id_uses_engine_and_sync_service(monkeypatch):
    engine_calls = []
    seed_calls = []
    added_courses = []

    sample_content = {
        "assignments": [{"id": "a1", "name": "HW1", "url": "https://mulms.mutah.edu.jo/a/1", "due_date": ""}],
        "quizzes": [],
        "resources": [],
        "grades": [],
        "announcements": [],
        "attendance": [],
    }

    class FakeScraper:
        def get_course_info(self, cid):
            return "Algorithms 101", sample_content

    class FakeEngine:
        async def run_with_user_scraper(self, tid, fn, *args, **kwargs):
            engine_calls.append(tid)
            return fn(FakeScraper(), *args, **kwargs)

    def spy_seed(db_mod, tid, cid, content, **kwargs):
        seed_calls.append((tid, cid, content, kwargs))
        return {"course_id": cid, "seeded": True}

    monkeypatch.setattr(bot.db, "get_user", lambda tid: FAKE_USER)
    monkeypatch.setattr(bot.db, "add_course", lambda tid, cid, name, url: added_courses.append((tid, cid, name, url)))
    monkeypatch.setattr(sync_service, "seed_course_snapshot", spy_seed)

    msg = MagicMock()
    msg.text = "https://mulms.mutah.edu.jo/course/view.php?id=4321"
    msg.delete = AsyncMock()

    tg_bot = MagicMock()
    tg_bot.edit_message_text = AsyncMock()
    tg_bot.send_message = AsyncMock()

    update = MagicMock()
    update.message = msg
    update.effective_user = SimpleNamespace(id=1)
    update.effective_chat = SimpleNamespace(id=1)
    update.get_bot = MagicMock(return_value=tg_bot)

    ctx = _make_ctx(
        user_data={"add_course_msg_id": 77},
        bot_data={"engine": FakeEngine(), "reminder_engine": "REM_ENG"},
    )

    res = asyncio.run(h_courses.add_course_got_id(update, ctx))

    assert res == ConversationHandler.END
    assert engine_calls == [1], "add_course_got_id must route LMS fetch through MonitorEngine.run_with_user_scraper"
    assert len(added_courses) == 1 and added_courses[0][1] == 4321
    assert len(seed_calls) == 1
    assert seed_calls[0][0] == 1 and seed_calls[0][1] == 4321
    assert seed_calls[0][3]["reminder_engine"] == "REM_ENG"
    assert seed_calls[0][3]["mark_seeded"] is True


def test_p2_fetch_show_course_uses_engine_and_sync_service(monkeypatch):
    engine_calls = []
    seed_calls = []

    sample_content = {
        "assignments": [],
        "quizzes": [],
        "resources": [{"id": "r1", "name": "Week 1", "url": "https://mulms.mutah.edu.jo/r/1"}],
        "grades": [],
        "announcements": [],
        "attendance": [],
    }

    class FakeScraper:
        def get_course_info(self, cid):
            return "Operating Systems", sample_content

    class FakeEngine:
        async def run_with_user_scraper(self, tid, fn, *args, **kwargs):
            engine_calls.append(tid)
            return fn(FakeScraper(), *args, **kwargs)

    def spy_seed(db_mod, tid, cid, content, **kwargs):
        seed_calls.append((tid, cid, content, kwargs))
        return {"course_id": cid, "seeded": True}

    monkeypatch.setattr(bot.db, "get_course", lambda tid, cid: {"course_id": cid, "is_monitored": 1})
    monkeypatch.setattr(sync_service, "seed_course_snapshot", spy_seed)
    monkeypatch.setattr(h_courses, "_send", AsyncMock())
    monkeypatch.setattr(h_views, "_send_sections", AsyncMock())

    h_common.set_engine(FakeEngine())
    try:
        msg = MagicMock()
        asyncio.run(h_courses._fetch_show_course(msg, 1, FAKE_USER, 888, edit=True))
    finally:
        h_common.set_engine(None)

    assert engine_calls == [1]
    assert len(seed_calls) == 1 and seed_calls[0][1] == 888


def test_p2_refresh_handlers_route_through_engine(monkeypatch):
    calls = []

    class FakeScraper:
        def get_course_grades(self, cid):
            calls.append(("grades", cid))
            return []

        def get_course_announcements(self, cid):
            calls.append(("announcements", cid))
            return []

        def get_course_attendance(self, cid):
            calls.append(("attendance", cid))
            return []

    class FakeEngine:
        async def run_with_user_scraper(self, tid, fn, *args, **kwargs):
            return fn(FakeScraper(), *args, **kwargs)

    monkeypatch.setattr(h_courses, "_send", AsyncMock())
    monkeypatch.setattr(h_courses, "_render_grades_view", AsyncMock())
    monkeypatch.setattr(h_courses, "_render_announcements_view", AsyncMock())
    monkeypatch.setattr(h_courses, "_render_attendance_view", AsyncMock())

    ctx = _make_ctx(bot_data={"engine": FakeEngine()})
    sample_courses = [{"course_id": 101}]
    msg = MagicMock()

    asyncio.run(h_courses.handle_refresh_grades(msg, 1, sample_courses, ctx))
    asyncio.run(h_courses.handle_refresh_announcements(msg, 1, sample_courses, ctx))
    asyncio.run(h_courses.handle_refresh_attendance(msg, 1, sample_courses, ctx))

    assert calls == [("grades", 101), ("announcements", 101), ("attendance", 101)]


def test_p2_shared_loop_lifecycle_init_db_before_fastapi_and_clean_shutdown(monkeypatch):
    order = []

    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setattr(bot.db, "init_db", lambda: order.append("init_db"))
    monkeypatch.setattr(bot.db, "close", lambda: order.append("db_close"), raising=False)

    class FakeMonitorEngine:
        def __init__(self, tg_bot, rem_engine):
            self.loop = None

        def attach_loop(self, loop):
            self.loop = loop
            order.append("attach_loop")

        async def start(self):
            order.append("monitor_start")

        async def stop(self):
            order.append("monitor_stop")

    class FakeReminderEngine:
        def __init__(self, tg_bot):
            pass

        async def start(self):
            order.append("reminder_start")

        async def stop(self):
            order.append("reminder_stop")

    fastapi_loops = []

    async def fake_start_fastapi(**kwargs):
        fastapi_loops.append(asyncio.get_running_loop())
        order.append("fastapi_start")
        return "FAKE_TASK"

    async def fake_stop_fastapi():
        order.append("fastapi_stop")

    monkeypatch.setattr(bot, "MonitorEngine", FakeMonitorEngine)
    monkeypatch.setattr(bot, "ReminderEngine", FakeReminderEngine)
    monkeypatch.setattr(bot, "init_webapp_engines", lambda e, r: order.append("init_webapp_engines"))
    monkeypatch.setattr(bot, "start_webapp_server_async", fake_start_fastapi)
    monkeypatch.setattr(bot, "stop_webapp_server_async", fake_stop_fastapi)
    monkeypatch.setattr(bot, "get_webapp_url", lambda: "")

    hooks = {}
    fake_app = MagicMock()
    fake_app.bot = SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock())
    fake_app.bot_data = {}

    def _run_polling(**kw):
        async def _lifecycle():
            loop = asyncio.get_running_loop()
            await hooks["post_init"](fake_app)
            assert fake_app.bot_data["engine"].loop is loop
            assert fastapi_loops == [loop]
            assert h_common.get_engine() is fake_app.bot_data["engine"]
            assert fake_app.bot_data["webapp_task"] == "FAKE_TASK"
            await hooks["post_shutdown"](fake_app)
            assert h_common.get_engine() is None

        asyncio.run(_lifecycle())

    fake_app.run_polling = MagicMock(side_effect=_run_polling)

    class FakeBuilder:
        def token(self, t): return self
        def post_init(self, fn):
            hooks["post_init"] = fn
            return self
        def post_shutdown(self, fn):
            hooks["post_shutdown"] = fn
            return self
        def build(self): return fake_app

    monkeypatch.setattr(bot.Application, "builder", staticmethod(lambda: FakeBuilder()))

    bot.main()

    assert order.index("init_db") < order.index("fastapi_start")
    assert order.index("fastapi_start") < order.index("fastapi_stop")
    assert order.index("fastapi_stop") < order.index("db_close")
    assert order.index("monitor_stop") < order.index("db_close")
    assert order.index("reminder_stop") < order.index("db_close")


def test_p2_owner_commands_courses_pair_help(monkeypatch):
    monkeypatch.setattr(h_views, "_send", AsyncMock())
    monkeypatch.setattr(h_auth, "_send", AsyncMock())
    monkeypatch.setattr(h_courses, "_render_courses", AsyncMock())

    update_owner = MagicMock()
    update_owner.effective_user = SimpleNamespace(id=1)
    update_owner.message = MagicMock()
    update_owner.callback_query = None

    ctx = _make_ctx()
    assert asyncio.run(h_courses.cmd_courses(update_owner, ctx)) == ConversationHandler.END
    assert h_courses._render_courses.await_count == 1

    assert asyncio.run(h_views.cmd_help(update_owner, ctx)) == ConversationHandler.END
    assert h_views._send.await_count == 1

    monkeypatch.setattr(h_auth, "get_webapp_url", lambda: "")
    assert asyncio.run(h_auth.cmd_pair(update_owner, ctx)) == ConversationHandler.END
    assert h_auth._send.await_count == 1

    # Non-owner rejected
    update_stranger = MagicMock()
    update_stranger.effective_user = SimpleNamespace(id=99999)
    update_stranger.message = MagicMock()
    update_stranger.message.reply_text = AsyncMock()
    update_stranger.callback_query = None

    assert asyncio.run(h_courses.cmd_courses(update_stranger, ctx)) == ConversationHandler.END
    assert update_stranger.message.reply_text.await_count == 1


def test_p2_integration_real_monitor_engine_and_sync_service(monkeypatch, tmp_path):
    """End-to-end check: handlers/courses.py -> real MonitorEngine.run_with_user_scraper -> real sync_service."""
    from monitor import MonitorEngine

    db_path = tmp_path / "p2_bot_integration.db"
    monkeypatch.setattr(bot.db, "SQLITE_PATH", str(db_path), raising=False)
    monkeypatch.setattr(bot.db, "DB_PATH", str(db_path), raising=False)
    if hasattr(bot.db, "close"):
        bot.db.close()
    bot.db.init_db()

    try:
        bot.db.save_user(1, "20240001", "secret_pw", "u1", "Integration Student")

        sample_content = {
            "assignments": [
                {
                    "id": "assign_10",
                    "name": "Lab 1",
                    "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=10",
                    "due_date": "",
                    "is_submitted": False,
                }
            ],
            "quizzes": [],
            "resources": [
                {
                    "id": "res_20",
                    "name": "Slides 1",
                    "url": "https://mulms.mutah.edu.jo/mod/resource/view.php?id=20",
                }
            ],
            "grades": [
                {
                    "item_id": "g_1",
                    "item_name": "Midterm",
                    "grade_val": "28",
                    "grade_max": "30",
                    "percentage": "93%",
                    "feedback": "",
                    "item_url": "https://mulms.mutah.edu.jo/grade/1",
                }
            ],
            "announcements": [],
            "attendance": [],
        }

        class FakeScraper:
            def get_course_info(self, cid):
                return "Software Engineering", sample_content

            def close(self):
                pass

        real_engine = MonitorEngine(MagicMock(), None)

        async def fake_ensure_logged_in(user, force_fresh=False, notify_on_invalid=False):
            return FakeScraper(), None

        monkeypatch.setattr(real_engine, "_ensure_logged_in", fake_ensure_logged_in)
        monkeypatch.setattr(real_engine, "_respect_request_cooldown", AsyncMock())

        msg = MagicMock()
        msg.text = "7777"
        msg.delete = AsyncMock()
        tg_bot = MagicMock()
        tg_bot.edit_message_text = AsyncMock()
        tg_bot.send_message = AsyncMock()

        update = MagicMock()
        update.message = msg
        update.effective_user = SimpleNamespace(id=1)
        update.effective_chat = SimpleNamespace(id=1)
        update.get_bot = MagicMock(return_value=tg_bot)

        ctx = _make_ctx(user_data={"add_course_msg_id": 55}, bot_data={"engine": real_engine})
        res = asyncio.run(h_courses.add_course_got_id(update, ctx))

        assert res == ConversationHandler.END
        course = bot.db.get_course(1, 7777)
        assert course is not None and course["course_name"] == "Software Engineering"
        items = bot.db.get_course_items(1, 7777)
        assert {it["content_id"] for it in items} == {"assign_10", "res_20"}
        grades = bot.db.get_user_grades(1)
        assert len(grades) == 1 and grades[0]["grade_val"] == "28"
        assert bot.db.is_course_seeded(1, 7777) is True
    finally:
        if hasattr(bot.db, "close"):
            bot.db.close()

