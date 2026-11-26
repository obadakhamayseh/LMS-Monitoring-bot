import asyncio
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

# Configure safe isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import lms_scraper
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


def test_c2_monitor_scans_courses_strictly_sequentially():
    FetchResult = getattr(lms_scraper, "FetchResult")

    async def _run():
        bot = MagicMock()
        bot.send_message = AsyncMock()
        eng = MonitorEngine(bot=bot)

        in_flight = 0
        max_concurrency = 0
        lock = threading.Lock()
        scanned_order = []

        def fake_fetch(cid, course_soup=None):
            nonlocal in_flight, max_concurrency
            with lock:
                in_flight += 1
                max_concurrency = max(max_concurrency, in_flight)
                scanned_order.append(cid)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            return FetchResult(content=_empty_content(), ok=True, session_expired=False, waf_blocked=False, error=None)

        scraper = MagicMock()
        scraper.session_expired = False
        scraper.waf_blocked = False
        scraper.fetch_course_content = MagicMock(side_effect=fake_fetch)

        user = {
            "telegram_id": 1,
            "notify_assign": 1,
            "notify_quiz": 1,
            "notify_resource": 1,
            "notify_grade": 1,
            "notify_announcement": 1,
            "notify_attendance": 1,
        }
        courses = [
            {"course_id": 101, "course_name": "C1"},
            {"course_id": 102, "course_name": "C2"},
            {"course_id": 103, "course_name": "C3"},
            {"course_id": 104, "course_name": "C4"},
        ]

        with patch("monitor.db.get_course_items", return_value=[]), \
             patch("monitor.db.get_user_grades", return_value=[]), \
             patch("monitor.db.get_user_announcements", return_value=[]):
            sent, complete = await eng._scan_user(user, courses, scraper, bypass_cache=True)

        assert complete is True
        assert sent == 0
        assert max_concurrency == 1, f"Monitor scanned concurrently! max_concurrency={max_concurrency}"
        assert scanned_order == [101, 102, 103, 104]
        assert scraper.fetch_course_content.call_count == 4

    asyncio.run(_run())


def test_c2_monitor_stops_scan_on_waf_fetch_result():
    FetchResult = getattr(lms_scraper, "FetchResult")

    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)

        calls = []

        def fake_fetch(cid, course_soup=None):
            calls.append(cid)
            return FetchResult(
                content=_empty_content(),
                ok=False,
                session_expired=False,
                waf_blocked=True,
                error="WAF CAPTCHA",
            )

        scraper = MagicMock()
        # Legacy attrs stay False to prove monitor inspects FetchResult, not mutated scraper attrs
        scraper.session_expired = False
        scraper.waf_blocked = False
        scraper.fetch_course_content = MagicMock(side_effect=fake_fetch)

        user = {"telegram_id": 1}
        courses = [
            {"course_id": 201, "course_name": "C1"},
            {"course_id": 202, "course_name": "C2"},
        ]

        with patch("monitor.db.get_course_items", return_value=[]), \
             patch("monitor.db.get_user_grades", return_value=[]):
            _, complete = await eng._scan_user(user, courses, scraper, bypass_cache=True)

        assert complete is False
        # Must abort immediately after first WAF result instead of hammering course 202
        assert calls == [201]

    asyncio.run(_run())


def test_c3_monitor_excludes_mod_url_from_auto_download_and_bounds_downloads():
    FetchResult = getattr(lms_scraper, "FetchResult")

    async def _run():
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        eng = MonitorEngine(bot=bot)
        eng._send = AsyncMock(return_value=True)

        downloaded_urls = []

        def fake_download(url, max_mb=40, raw_url=None, referer=None):
            downloaded_urls.append((url, max_mb))
            return ("lecture.pdf", b"%PDF-1.4")

        content = _empty_content()
        content["resources"] = [
            {
                "id": "url_1",
                "name": "External Video Link",
                "url": "https://mulms.mutah.edu.jo/mod/url/view.php?id=1",
                "raw_url": "https://mulms.mutah.edu.jo/mod/url/view.php?id=1",
                "type": "url",
                "course_id": 301,
                "due_date": "",
            },
            {
                "id": "page_2",
                "name": "Course Page",
                "url": "https://mulms.mutah.edu.jo/mod/page/view.php?id=2",
                "raw_url": "https://mulms.mutah.edu.jo/mod/page/view.php?id=2",
                "type": "page",
                "course_id": 301,
                "due_date": "",
            },
            {
                "id": "resource_3",
                "name": "Lecture 1 Slides",
                "url": "https://mulms.mutah.edu.jo/mod/resource/view.php?id=3",
                "raw_url": "https://mulms.mutah.edu.jo/mod/resource/view.php?id=3",
                "type": "resource",
                "course_id": 301,
                "due_date": "",
            },
        ]

        scraper = MagicMock()
        scraper.session_expired = False
        scraper.waf_blocked = False
        scraper.fetch_course_content = MagicMock(
            return_value=FetchResult(content=content, ok=True, session_expired=False, waf_blocked=False, error=None)
        )
        scraper.download_resource = MagicMock(side_effect=fake_download)

        user = {
            "telegram_id": 1,
            "notify_assign": 1,
            "notify_quiz": 1,
            "notify_resource": 1,
            "notify_grade": 1,
            "notify_announcement": 1,
            "notify_attendance": 1,
        }
        courses = [{"course_id": 301, "course_name": "Networks"}]

        with patch("monitor.db.get_course_items", return_value=[]), \
             patch("monitor.db.get_user_grades", return_value=[]), \
             patch("monitor.db.get_seen_content", return_value=None), \
             patch("monitor.db.mark_seen"), \
             patch("monitor.db.update_seen_content"), \
             patch("monitor.db.get_user_announcements", return_value=[]):
            sent, complete = await eng._scan_user(user, courses, scraper, bypass_cache=True)

        assert complete is True
        assert sent == 3
        # ONLY /mod/resource/ may be auto-downloaded; /mod/url/ and /mod/page/ MUST be excluded
        assert len(downloaded_urls) == 1, downloaded_urls
        assert downloaded_urls[0][0] == "https://mulms.mutah.edu.jo/mod/resource/view.php?id=3"
        assert downloaded_urls[0][1] <= 25  # bounded max_mb

    asyncio.run(_run())
