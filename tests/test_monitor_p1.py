import asyncio
import inspect
import os
import threading
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

# Configure safe isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter

import lms_scraper
import monitor
from monitor import MonitorEngine


def _empty_content():
    return {
        "assignments": [],
        "quizzes": [],
        "resources": [],
        "grades": [],
        "announcements": [],
        "attendance": [],
    }


def _sample_user(last_check: str | None = None, interval: int = 30):
    return {
        "telegram_id": 1,
        "lms_username": "student",
        "lms_password": "secret",
        "check_interval": interval,
        "last_check": last_check,
        "notify_assign": 1,
        "notify_quiz": 1,
        "notify_resource": 1,
        "notify_grade": 1,
        "notify_announcement": 1,
        "notify_attendance": 1,
    }


# ===========================================================================
# H7: due check BEFORE _ensure_logged_in, no unconditional 120s keepalive,
#     exponential backoff on complete=False, last_request_at cooldown
# ===========================================================================

def test_h7_due_check_runs_before_ensure_logged_in_and_no_keepalive():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)

        fake_scraper = MagicMock()
        fake_scraper.logged_in = False
        fake_scraper.waf_blocked = False
        fake_scraper.session_expired = False
        fake_scraper.credentials_invalid = False
        fake_scraper.last_request_at = time.monotonic() - 500.0
        fake_scraper.login = MagicMock(return_value=True)
        fake_scraper.keep_session_alive = MagicMock(return_value=True)

        # User was just checked 1 minute ago with 30-min interval -> NOT due
        recent_check = datetime.now(timezone.utc).isoformat()
        user = _sample_user(last_check=recent_check, interval=30)

        with patch.object(eng, "_get_session", return_value=fake_scraper), \
             patch("monitor.db.get_user_courses", return_value=[{"course_id": 101, "course_name": "CS101"}]):
            await eng._check_user(user)

        # login() MUST NOT be called when user is not due
        assert fake_scraper.login.call_count == 0
        # keep_session_alive() MUST NOT be called unconditionally when not due
        assert fake_scraper.keep_session_alive.call_count == 0

        # Even if scraper IS already logged_in and idle > 120s, keep_session_alive must NOT run when not due
        fake_scraper.logged_in = True
        fake_scraper.last_request_at = time.monotonic() - 600.0
        with patch.object(eng, "_get_session", return_value=fake_scraper), \
             patch("monitor.db.get_user_courses", return_value=[{"course_id": 101, "course_name": "CS101"}]):
            await eng._check_user(user)

        assert fake_scraper.keep_session_alive.call_count == 0

    asyncio.run(_run())


def test_h7_exponential_backoff_on_incomplete_scan_and_reset_on_success():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng.MIN_REQUEST_COOLDOWN_SECONDS = 0.0

        fake_scraper = MagicMock()
        fake_scraper.logged_in = True
        fake_scraper.waf_blocked = False
        fake_scraper.session_expired = False
        fake_scraper.last_request_at = 0.0

        user = _sample_user(last_check=None, interval=10)
        courses = [{"course_id": 101, "course_name": "CS101"}]

        # Failure 1 -> backoff_1
        with patch.object(eng, "_get_session", return_value=fake_scraper), \
             patch.object(eng, "_scan_user", new=AsyncMock(return_value=(0, False))), \
             patch("monitor.db.get_user_courses", return_value=courses):
            t0 = time.monotonic()
            await eng._check_user(user)
            until_1 = eng._transient_backoff_until.get(1, 0.0)
            delay_1 = until_1 - t0
            assert delay_1 > 0, "Expected backoff after complete=False"

            # Immediate retry while in backoff must NOT call _scan_user
            scan_mock = AsyncMock(return_value=(0, False))
            with patch.object(eng, "_scan_user", new=scan_mock):
                await eng._check_user(user)
                assert scan_mock.call_count == 0

            # Simulate backoff elapsed -> Failure 2 must produce strictly larger delay (exponential)
            eng._transient_backoff_until[1] = 0.0
            t1 = time.monotonic()
            await eng._check_user(user)
            until_2 = eng._transient_backoff_until.get(1, 0.0)
            delay_2 = until_2 - t1
            assert delay_2 > delay_1 * 1.5, f"Expected exponential growth: {delay_1:.1f} -> {delay_2:.1f}"

        # Simulate backoff elapsed -> Success resets failure counter & clears backoff
        eng._transient_backoff_until[1] = 0.0
        with patch.object(eng, "_get_session", return_value=fake_scraper), \
             patch.object(eng, "_scan_user", new=AsyncMock(return_value=(1, True))), \
             patch("monitor.db.get_user_courses", return_value=courses), \
             patch("monitor.db.update_last_check"), \
             patch("monitor.db.append_sync_log"):
            await eng._check_user(user)

        assert eng._transient_backoff_until.get(1, 0.0) == 0.0
        assert eng._incomplete_failures.get(1, 0) == 0

    asyncio.run(_run())


def test_h7_respects_last_request_at_cooldown():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng.MIN_REQUEST_COOLDOWN_SECONDS = 0.15

        fake_scraper = MagicMock()
        fake_scraper.logged_in = True
        fake_scraper.waf_blocked = False
        fake_scraper.session_expired = False
        fake_scraper.last_request_at = time.monotonic()

        user = _sample_user(last_check=None, interval=10)
        courses = [{"course_id": 101, "course_name": "CS101"}]

        scan_started_at = []

        async def record_scan(*args, **kwargs):
            scan_started_at.append(time.monotonic())
            return (0, True)

        t0 = time.monotonic()
        with patch.object(eng, "_get_session", return_value=fake_scraper), \
             patch.object(eng, "_scan_user", new=AsyncMock(side_effect=record_scan)), \
             patch("monitor.db.get_user_courses", return_value=courses), \
             patch("monitor.db.update_last_check"), \
             patch("monitor.db.append_sync_log"):
            await eng._check_user(user)

        assert len(scan_started_at) == 1
        assert (scan_started_at[0] - t0) >= 0.10, "Expected monitor to honor last_request_at cooldown"

    asyncio.run(_run())


# ===========================================================================
# H6: courses.is_seeded gates announcements & attendance; notified saved ONLY after send=True
# ===========================================================================

def test_h6_is_seeded_gates_announcements_and_attendance():
    FetchResult = lms_scraper.FetchResult

    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng._send = AsyncMock(return_value=True)

        content = _empty_content()
        content["announcements"] = [{
            "discuss_id": "discuss_10",
            "forum_name": "Announcements",
            "title": "Midterm Date",
            "author": "Dr. Ahmad",
            "posted_date": "2026-11-26",
            "url": "https://mulms.mutah.edu.jo/mod/forum/discuss.php?d=10",
            "course_id": 501,
        }]
        content["attendance"] = [{
            "att_id": "att_20",
            "taken_sessions": 1,
            "total_sessions": 1,
            "present_count": 1,
            "absent_count": 0,
            "late_count": 0,
            "excused_count": 0,
            "points_str": "2/2",
            "percentage_str": "100%",
            "sessions": [{"date": "Sun 22 Nov", "description": "Lec 1", "status": "P", "points": "2/2", "remarks": ""}],
            "url": "https://mulms.mutah.edu.jo/mod/attendance/view.php?id=20",
            "course_id": 501,
        }]

        scraper = MagicMock()
        scraper.session_expired = False
        scraper.waf_blocked = False
        scraper.fetch_course_content = MagicMock(return_value=FetchResult(content=content, ok=True))
        scraper.get_announcement_details = MagicMock(return_value={"body": "Exam is Sunday", "author": "Dr. Ahmad", "date": "2026-11-26"})

        user = _sample_user()
        course = {"course_id": 501, "course_name": "Algorithms"}

        # Phase 1: Course NOT seeded yet -> silent seed, NO notifications sent, mark_course_seeded called
        mark_seeded = MagicMock()
        with patch("monitor.db.is_course_seeded", return_value=False, create=True), \
             patch("monitor.db.mark_course_seeded", mark_seeded, create=True), \
             patch("monitor.db.get_announcement", return_value=None), \
             patch("monitor.db.upsert_announcement") as upsert_ann, \
             patch("monitor.db.get_attendance", return_value=None), \
             patch("monitor.db.upsert_attendance") as upsert_att:
            await eng._check_course_result(1, user, scraper, course, bypass_cache=True)

        assert eng._send.call_count == 0, "Unseeded course must not flood notifications"
        assert upsert_ann.call_count == 1
        assert upsert_att.call_count == 1
        mark_seeded.assert_called_once_with(1, 501)

        # Phase 2: Course IS seeded, even though DB currently has 0 announcements & 0 attendance rows -> MUST notify!
        eng._send.reset_mock()
        with patch("monitor.db.is_course_seeded", return_value=True, create=True), \
             patch("monitor.db.mark_course_seeded", create=True) as mark_seeded_2, \
             patch("monitor.db.get_user_announcements", return_value=[]), \
             patch("monitor.db.get_announcement", return_value=None), \
             patch("monitor.db.upsert_announcement") as upsert_ann_2, \
             patch("monitor.db.get_attendance", return_value=None), \
             patch("monitor.db.upsert_attendance") as upsert_att_2:
            await eng._check_course_result(1, user, scraper, course, bypass_cache=True)

        assert eng._send.call_count == 2, f"Expected 2 notifications (ann + att) on seeded course, got {eng._send.call_count}"
        assert upsert_ann_2.call_count == 1
        assert upsert_att_2.call_count == 1
        assert mark_seeded_2.call_count == 0

    asyncio.run(_run())


def test_h6_notified_not_saved_when_send_fails():
    FetchResult = lms_scraper.FetchResult

    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng._send = AsyncMock(return_value=False)  # Telegram delivery fails!

        content = _empty_content()
        content["resources"] = [{
            "id": "resource_99",
            "name": "Slides 1",
            "url": "https://mulms.mutah.edu.jo/mod/resource/view.php?id=99",
            "type": "resource",
            "course_id": 601,
            "due_date": "",
        }]
        content["grades"] = [{
            "item_id": "mod_77",
            "item_name": "Quiz 1",
            "grade_val": "9.5",
            "grade_max": "10",
            "percentage": "95%",
            "feedback": "",
            "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=77",
            "course_id": 601,
        }]
        content["announcements"] = [{
            "discuss_id": "discuss_88",
            "forum_name": "Announcements",
            "title": "Class cancelled",
            "author": "Dr. Ali",
            "posted_date": "2026-11-26",
            "url": "https://mulms.mutah.edu.jo/mod/forum/discuss.php?d=88",
            "course_id": 601,
        }]
        content["attendance"] = [{
            "att_id": "att_55",
            "taken_sessions": 1,
            "total_sessions": 1,
            "present_count": 0,
            "absent_count": 1,
            "late_count": 0,
            "excused_count": 0,
            "points_str": "0/2",
            "percentage_str": "0%",
            "sessions": [{"date": "Thu 26 Nov", "description": "Lec 5", "status": "A", "points": "0/2", "remarks": ""}],
            "url": "https://mulms.mutah.edu.jo/mod/attendance/view.php?id=55",
            "course_id": 601,
        }]

        scraper = MagicMock()
        scraper.session_expired = False
        scraper.waf_blocked = False
        scraper.fetch_course_content = MagicMock(return_value=FetchResult(content=content, ok=True))
        scraper.get_announcement_details = MagicMock(return_value={"body": "No class today", "author": "Dr. Ali", "date": "2026-11-26"})

        user = _sample_user()
        courses = [{"course_id": 601, "course_name": "OS"}]

        with patch("monitor.db.is_course_seeded", return_value=True, create=True), \
             patch("monitor.db.get_course_items", return_value=[]), \
             patch("monitor.db.get_user_grades", return_value=[]), \
             patch("monitor.db.get_user_announcements", return_value=[]), \
             patch("monitor.db.get_seen_content", return_value=None), \
             patch("monitor.db.mark_seen") as mark_seen, \
             patch("monitor.db.delete_seen") as delete_seen, \
             patch("monitor.db.get_grade", return_value=None), \
             patch("monitor.db.upsert_grade") as upsert_grade, \
             patch("monitor.db.get_announcement", return_value=None), \
             patch("monitor.db.upsert_announcement") as upsert_ann, \
             patch("monitor.db.get_attendance", return_value=None), \
             patch("monitor.db.upsert_attendance") as upsert_att:
            sent_count, complete = await eng._scan_user(user, courses, scraper, bypass_cache=True)

        assert eng._send.call_count == 4, f"Expected 4 send attempts (grade, ann, att, resource), got {eng._send.call_count}"
        assert sent_count == 0
        assert complete is False
        # Nothing should have been persisted as notified when _send returned False!
        assert mark_seen.call_count == 0, "mark_seen must NOT be called before/when _send fails"
        assert delete_seen.call_count == 0
        assert upsert_grade.call_count == 0, "upsert_grade must NOT persist un-notified grade on send failure"
        assert upsert_ann.call_count == 0, "upsert_announcement must NOT persist un-notified announcement on send failure"
        assert upsert_att.call_count == 0, "upsert_attendance must NOT persist un-notified attendance on send failure"

    asyncio.run(_run())


# ===========================================================================
# H4 + H13: Thread-safe MonitorEngine public API, executor logout, stop() awaits tasks
# ===========================================================================

def test_h4_and_h13_threadsafe_invalidate_user_session_and_executor_logout():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        loop = asyncio.get_running_loop()
        loop_tid = threading.get_ident()
        eng.attach_loop(loop)

        logout_threads = []
        fake_scraper = MagicMock()
        fake_scraper.logout = MagicMock(side_effect=lambda: logout_threads.append(threading.get_ident()))
        fake_scraper.close = MagicMock()

        eng._sessions[42] = ("u", "p", fake_scraper)
        eng._auth_required.add(42)
        eng._auth_alerted.add(42)
        eng._waf_backoff_until[42] = time.monotonic() + 900
        eng._transient_backoff_until[42] = time.monotonic() + 300
        eng._course_cache[(42, 101)] = (time.monotonic(), _empty_content())

        # Invoke invalidate_user_session from ANOTHER thread (like webapp_server.py)
        err = []

        def web_thread_call():
            try:
                eng.invalidate_user_session(42)
            except Exception as exc:
                err.append(exc)

        t = threading.Thread(target=web_thread_call)
        t.start()
        # Yield to event loop so run_coroutine_threadsafe completes
        while t.is_alive():
            await asyncio.sleep(0.01)
        t.join(timeout=5)

        assert not err, err
        assert 42 not in eng._sessions
        assert 42 not in eng._auth_required
        assert 42 not in eng._auth_alerted
        assert 42 not in eng._waf_backoff_until
        assert 42 not in eng._transient_backoff_until
        assert (42, 101) not in eng._course_cache
        assert fake_scraper.logout.call_count == 1
        # H13: logout() MUST execute in worker thread, NOT event loop thread
        assert logout_threads and logout_threads[0] != loop_tid

    asyncio.run(_run())


def test_h4_invalidate_user_session_fallback_without_running_loop():
    bot = MagicMock()
    eng = MonitorEngine(bot=bot)
    fake_scraper = MagicMock()
    eng._sessions[7] = ("u", "p", fake_scraper)
    eng._auth_required.add(7)

    # No loop attached -> fallback lock path must work cleanly
    eng.invalidate_user_session(7)
    assert 7 not in eng._sessions
    assert 7 not in eng._auth_required
    assert fake_scraper.logout.call_count == 1


def test_h13_stop_awaits_in_flight_tasks_before_closing_sessions():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        events_order = []

        fake_scraper = MagicMock()
        fake_scraper.close = MagicMock(side_effect=lambda: events_order.append("session_closed"))
        eng._sessions[1] = ("u", "p", fake_scraper)

        started = asyncio.Event()

        async def slow_task():
            started.set()
            await asyncio.sleep(0.1)
            events_order.append("task_finished")

        task = eng._track_task(slow_task())
        await started.wait()

        await eng.stop()
        assert task.done()
        assert events_order == ["task_finished", "session_closed"], events_order

    asyncio.run(_run())


# ===========================================================================
# H12: HTML ParseMode, html escaping, RetryAfter & Forbidden handling
# ===========================================================================

def test_h12_monitor_uses_html_no_markdown_and_handles_retry_after_and_forbidden():
    src = inspect.getsource(monitor)
    assert "ParseMode.MARKDOWN" not in src, "monitor.py must not use deprecated ParseMode.MARKDOWN"

    async def _run():
        # 1. RetryAfter retries and succeeds
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RetryAfter(0.01), MagicMock()])
        eng = MonitorEngine(bot=bot)

        ok = await eng._send(123, "<b>Test &amp; Check</b>")
        assert ok is True
        assert bot.send_message.call_count == 2
        for call in bot.send_message.call_args_list:
            assert call.kwargs.get("parse_mode") == ParseMode.HTML

        # 2. Forbidden disables user notifications via db.set_user_notifications_enabled(tid, False)
        bot2 = MagicMock()
        bot2.send_message = AsyncMock(side_effect=Forbidden("bot was blocked by the user"))
        eng2 = MonitorEngine(bot=bot2)

        with patch("monitor.db.set_user_notifications_enabled", create=True) as disable_mock:
            ok2 = await eng2._send(456, "<b>Hello</b>")

        assert ok2 is False
        disable_mock.assert_called_once_with(456, False)

    asyncio.run(_run())


def test_h6_and_h12_real_sqlite_db_integration(tmp_path, monkeypatch):
    """Verify MonitorEngine H6 (user_courses.is_seeded) & H12 (Forbidden -> disable notifications) against real database.py."""
    import database as real_db

    db_file = str(tmp_path / "monitor_p1_int.db")
    monkeypatch.setattr(real_db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(real_db, "DATABASE_URL", "")
    monkeypatch.setattr(real_db, "DIRECT_URL", "")
    real_db.init_db()

    tid = 777001
    cid = 9001
    real_db.save_user(tid, "stu1", "pass1")
    real_db.set_monitoring(tid, True)
    real_db.add_course(tid, cid, "Real DB Course", "https://mulms.mutah.edu.jo/course/view.php?id=9001")
    assert real_db.is_course_seeded(tid, cid) is False

    FetchResult = lms_scraper.FetchResult

    async def _run():
        bot = MagicMock()
        bot.send_message = AsyncMock()
        eng = MonitorEngine(bot=bot)

        # First scan: course has 0 announcements -> marks course as seeded without flooding
        scraper = MagicMock()
        scraper.session_expired = False
        scraper.waf_blocked = False
        scraper.fetch_course_content = MagicMock(return_value=FetchResult(content=_empty_content(), ok=True))
        user = real_db.get_user(tid)
        course = real_db.get_course(tid, cid)
        await eng._check_course_result(tid, user, scraper, course, bypass_cache=True)
        assert real_db.is_course_seeded(tid, cid) is True
        assert bot.send_message.call_count == 0

        # Second scan: first ever announcement appears -> MUST notify (not swallowed!)
        c2 = _empty_content()
        c2["announcements"] = [{
            "discuss_id": "discuss_first_real",
            "forum_name": "Announcements",
            "title": "First Real Announcement",
            "author": "Prof",
            "posted_date": "2026-11-26",
            "url": "https://mulms.mutah.edu.jo/mod/forum/discuss.php?d=1",
            "course_id": cid,
        }]
        scraper.fetch_course_content = MagicMock(return_value=FetchResult(content=c2, ok=True))
        scraper.get_announcement_details = MagicMock(return_value={"body": "Welcome!", "author": "Prof", "date": "2026-11-26"})
        await eng._check_course_result(tid, user, scraper, course, bypass_cache=True)
        assert bot.send_message.call_count == 1
        assert real_db.get_announcement(tid, "discuss_first_real") is not None

        # Forbidden disables notifications in real DB
        bot.send_message = AsyncMock(side_effect=Forbidden("blocked"))
        ok = await eng._send(tid, "<b>Alert</b>")
        assert ok is False
        updated_user = real_db.get_user(tid)
        assert updated_user["is_monitoring"] == 0
        assert updated_user["notify_announcement"] == 0

    asyncio.run(_run())


def test_h13_blocking_db_calls_run_in_executor():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        loop_tid = threading.get_ident()
        db_threads = []

        def spy_get_courses(tid, monitored_only=False):
            db_threads.append(threading.get_ident())
            return []

        with patch("monitor.db.get_user_courses", side_effect=spy_get_courses), \
             patch("monitor.db.update_last_check"):
            await eng._check_user(_sample_user())

        assert db_threads and db_threads[0] != loop_tid, "Blocking DB calls must run in executor thread"

    asyncio.run(_run())

