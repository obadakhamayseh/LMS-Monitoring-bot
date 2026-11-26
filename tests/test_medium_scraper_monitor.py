"""Prove-It test suite for Medium items: M-QUIZ, M-ANN, M-UTC."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

# Isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST_MED"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import pytest
from bs4 import BeautifulSoup

import database as real_db
import scraper.attendance as scraper_attendance
import scraper.core as scraper_core
import scraper.parsers as scraper_parsers
import scraper.quizzes as scraper_quizzes
from lms_scraper import FetchResult, LMSHttpClient, LMSScraper
from monitor import MonitorEngine
from scraper.parsers import (
    extract_forum_links,
    parse_announcement_details_soup,
    parse_resources_from_course_soup,
)
from scraper.quizzes import parse_quiz_grade_text, parse_quiz_status_soup


class _HtmlResp:
    def __init__(
        self,
        text: str,
        url: str = "https://mulms.mutah.edu.jo/course/view.php?id=71177",
        status_code: int = 200,
    ):
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.history = []

    def raise_for_status(self):
        return None

    def close(self):
        return None


def _empty_content():
    return {
        "assignments": [],
        "quizzes": [],
        "resources": [],
        "grades": [],
        "announcements": [],
        "attendance": [],
    }


# ===========================================================================
# M-QUIZ: Dynamic quizattemptsummary headers, no date-as-grade, ,/٫ decimals,
#         and notify_grade gating in monitor.py
# ===========================================================================


def test_m_quiz_horizontal_quizattemptsummary_dynamic_headers_en():
    html = """<html><body>
    <table class="generaltable quizattemptsummary">
      <thead>
        <tr>
          <th class="header c0">Attempt</th>
          <th class="header c1">State</th>
          <th class="header c2">Marks / 10.00</th>
          <th class="header c3">Grade / 20.00</th>
          <th class="header c4">Review</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="cell c0">1</td>
          <td class="cell c1">Finished<span class="statedetails">Submitted Tuesday, 12/05/2026, 2:30 PM</span></td>
          <td class="cell c2">8.50</td>
          <td class="cell c3">17.00</td>
          <td class="cell c4">Review not permitted</td>
        </tr>
      </tbody>
    </table>
    </body></html>"""
    res = parse_quiz_status_soup(BeautifulSoup(html, "lxml"))
    assert res["is_submitted"] is True
    assert res["grade_val"] == "17.00"
    assert res["grade_max"] == "0–20.00"


def test_m_quiz_horizontal_quizattemptsummary_arabic_reordered_and_arabic_decimal():
    html = """<html><body>
    <table class="generaltable quizattemptsummary">
      <thead>
        <tr>
          <th>المراجعة</th>
          <th>العلامة / ١٠</th>
          <th>النقاط / ٥</th>
          <th>الحالة</th>
          <th>المحاولة</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>غير مسموح</td>
          <td>٨٫٥</td>
          <td>٤٫٢٥</td>
          <td>منتهية <span class="statedetails">تم التسليم 25/11/26</span></td>
          <td>1</td>
        </tr>
      </tbody>
    </table>
    </body></html>"""
    res = parse_quiz_status_soup(BeautifulSoup(html, "lxml"))
    assert res["is_submitted"] is True
    assert res["grade_val"] == "8.5"
    assert res["grade_max"] == "0–10"


def test_m_quiz_horizontal_quizattemptsummary_marks_fallback_and_in_progress():
    # Only Marks column present (no Grade column), plus comma decimal
    html_marks = """<html><body>
    <table class="generaltable quizattemptsummary">
      <thead>
        <tr>
          <th>State</th>
          <th>Marks / 10,00</th>
          <th>Review</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Finished</td>
          <td>7,50</td>
          <td><a href="/mod/quiz/review.php?attempt=1">Review</a></td>
        </tr>
      </tbody>
    </table>
    </body></html>"""
    res = parse_quiz_status_soup(BeautifulSoup(html_marks, "lxml"))
    assert res["is_submitted"] is True
    assert res["grade_val"] == "7.50"
    assert res["grade_max"] == "0–10.00"

    # In-progress attempt with date in State cell must NOT be submitted and must NOT parse date as grade
    html_in_prog = """<html><body>
    <table class="generaltable quizattemptsummary">
      <thead>
        <tr>
          <th>Attempt</th>
          <th>State</th>
          <th>Grade / 10.00</th>
          <th>Review</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>1</td>
          <td>In progress (started 12/05/2026)</td>
          <td>Not yet graded</td>
          <td>Review not permitted</td>
        </tr>
      </tbody>
    </table>
    </body></html>"""
    res2 = parse_quiz_status_soup(BeautifulSoup(html_in_prog, "lxml"))
    assert res2["is_submitted"] is False
    assert res2["grade_val"] == ""


@pytest.mark.parametrize(
    "date_text",
    [
        "12/05/2026",
        "25/11/26",
        "1/5/2026",
        "2026/05/12",
        "2026/5/1",
        "12 / 05 / 2026",
        "١٢/٠٥/٢٠٢٦",
        "Submitted on 12/05/2026",
        "Finished 25/11/26 14:30",
    ],
)
def test_m_quiz_grade_text_never_matches_dates(date_text: str):
    parsed = parse_quiz_grade_text(date_text)
    assert parsed["grade_val"] == "", f"Date string {date_text!r} falsely parsed as grade: {parsed}"
    assert parsed["grade_max"] == "", f"Date string {date_text!r} falsely parsed grade_max: {parsed}"


@pytest.mark.parametrize(
    "raw_text,expected_val,expected_max,expected_pct",
    [
        ("8,5 / 10", "8.5", "0–10", ""),
        ("٨٫٥ / ١٠", "8.5", "0–10", ""),
        ("14,25 out of 20", "14.25", "0–20", ""),
        ("14,25 out of 20 (71,25%)", "14.25", "0–20", "71.25%"),
        ("٩٫٧٥ من أصل ١٠ (٩٧٫٥%)", "9.75", "0–10", "97.5%"),
        ("12,5", "12.5", "", ""),
        ("٨٫٧٥", "8.75", "", ""),
        ("Submitted 12/05/2026 — 8,5 / 10", "8.5", "0–10", ""),
    ],
)
def test_m_quiz_grade_text_supports_comma_arabic_decimal_and_digits(
    raw_text: str, expected_val: str, expected_max: str, expected_pct: str
):
    parsed = parse_quiz_grade_text(raw_text)
    assert parsed["grade_val"] == expected_val
    assert parsed["grade_max"] == expected_max
    assert parsed["percentage"] == expected_pct


def test_m_quiz_monitor_respects_notify_grade_for_quiz_and_regular_grades():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng._send = AsyncMock(return_value=True)

        content = _empty_content()
        content["quizzes"] = [
            {
                "id": "quiz_77",
                "name": "Quiz 1",
                "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=77",
                "type": "quiz",
                "course_id": 601,
                "due_date": "",
                "is_submitted": True,
            }
        ]
        content["grades"] = [
            {
                "item_id": "mod_77",
                "item_name": "Quiz 1",
                "grade_val": "9.5",
                "grade_max": "0–10",
                "percentage": "95%",
                "feedback": "",
                "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=77",
                "course_id": 601,
                "is_quiz": True,
            },
            {
                "item_id": "mod_88",
                "item_name": "Midterm Exam",
                "grade_val": "24",
                "grade_max": "0–30",
                "percentage": "80%",
                "feedback": "",
                "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=88",
                "course_id": 601,
                "is_quiz": False,
            },
        ]

        scraper = MagicMock()
        scraper.session_expired = False
        scraper.waf_blocked = False
        scraper.fetch_course_content = MagicMock(return_value=FetchResult(content=content, ok=True))

        # Case 1: notify_grade=0, notify_quiz=1 -> MUST NOT send quiz grade alert OR regular grade alert
        user_muted_grades = {
            "telegram_id": 1,
            "notify_assign": 1,
            "notify_quiz": 1,
            "notify_resource": 1,
            "notify_grade": 0,
            "notify_announcement": 1,
            "notify_attendance": 1,
        }
        course = {"course_id": 601, "course_name": "OS"}

        with patch("monitor.db.is_course_seeded", return_value=True), \
             patch("monitor.db.get_seen_content", return_value={"is_submitted": 1, "due_date": "", "content_name": "Quiz 1", "content_url": ""}), \
             patch("monitor.db.mark_assignment_submitted"), \
             patch("monitor.db.update_seen_content"), \
             patch("monitor.db.get_grade", return_value=None), \
             patch("monitor.db.upsert_grade") as upsert_grade:
            await eng._check_course_result(1, user_muted_grades, scraper, course, bypass_cache=True)

        assert eng._send.call_count == 0, (
            f"Expected 0 grade alerts when notify_grade=0 (even with notify_quiz=1), got {eng._send.call_count}"
        )
        assert upsert_grade.call_count == 2

        # Case 2: notify_grade=1, notify_quiz=0 -> MUST send both quiz grade alert and regular grade alert
        eng._send.reset_mock()
        user_enabled_grades = dict(user_muted_grades, notify_grade=1, notify_quiz=0)
        with patch("monitor.db.is_course_seeded", return_value=True), \
             patch("monitor.db.get_seen_content", return_value={"is_submitted": 1, "due_date": "", "content_name": "Quiz 1", "content_url": ""}), \
             patch("monitor.db.mark_assignment_submitted"), \
             patch("monitor.db.update_seen_content"), \
             patch("monitor.db.get_grade", return_value=None), \
             patch("monitor.db.upsert_grade") as upsert_grade2:
            await eng._check_course_result(1, user_enabled_grades, scraper, course, bypass_cache=True)

        assert eng._send.call_count == 2
        assert upsert_grade2.call_count == 2

    asyncio.run(_run())


# ===========================================================================
# M-ANN: Post-scoped author, announcement forum priority + no [:4] cap,
#        and urljoin across scraper/*.py
# ===========================================================================


def test_m_ann_author_never_leaks_from_navbar_or_header_when_post_missing():
    # No data-region="post" or .forumpost container: navbar/header user links MUST NOT be picked as author
    html_no_post = """<html><body>
    <nav class="navbar">
      <a href="/user/view.php?id=112607&amp;course=71177">Student Decoy Name</a>
    </nav>
    <header>
      <a href="/user/view.php?id=112607">Header Student</a>
    </header>
    <main>
      <div class="posting">Body without post container</div>
    </main>
    </body></html>"""
    details = parse_announcement_details_soup(BeautifulSoup(html_no_post, "lxml"))
    assert details["author"] == "", f"Author leaked from navbar/header: {details['author']!r}"

    # Inside post container, skip empty avatar link and pick actual author link
    html_with_avatar = """<html><body>
    <nav class="navbar">
      <a href="/user/view.php?id=112607">Student Decoy Name</a>
    </nav>
    <article class="forumpost" data-region="post">
      <a href="/user/view.php?id=5012"><img class="userpicture" alt=""></a>
      <a href="/user/view.php?id=5012&amp;course=71177">Dr. Layla Mansour</a>
      <time>Wednesday, 26 November 2026</time>
      <div class="post-content-container"><p>Class moved to Hall 101.</p></div>
    </article>
    </body></html>"""
    details2 = parse_announcement_details_soup(BeautifulSoup(html_with_avatar, "lxml"))
    assert details2["author"] == "Dr. Layla Mansour"
    assert "Hall 101" in details2["body"]


def test_m_ann_prioritizes_announcement_forums_and_removes_four_forum_cap():
    course_html = """<html><body>
      <a href="/mod/forum/view.php?id=101">General Discussion</a>
      <a href="/mod/forum/view.php?id=102">Lab Q&amp;A</a>
      <a href="/mod/forum/view.php?id=103">Project Chat</a>
      <a href="/mod/forum/view.php?id=104">Study Group</a>
      <a href="/mod/forum/view.php?id=105">Course Announcements</a>
      <a href="/mod/forum/view.php?id=106">إعلانات المساق</a>
      <a href="/mod/forum/view.php?id=107">أخبار المادة</a>
    </body></html>"""
    course_soup = BeautifulSoup(course_html, "lxml")

    links = extract_forum_links(course_soup)
    assert len(links) == 7
    # Announcement / News forums MUST come BEFORE non-announcement forums
    first_three_names = [name for _, name in links[:3]]
    assert first_three_names == ["Course Announcements", "إعلانات المساق", "أخبار المادة"]

    # LMSScraper.get_course_announcements MUST visit ALL 7 forums (no [:4] slice cap) in priority order
    sc = LMSScraper("u", "p", http_client=LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0)))
    visited_urls: list[str] = []

    def fake_get(url, **kw):
        visited_urls.append(url)
        fid = re.search(r"id=(\d+)", url).group(1)
        body = (
            f'<tr class="discussion">'
            f'<th><a href="/mod/forum/discuss.php?d={fid}0">Post from {fid}</a></th>'
            f'<td class="author">Prof</td>'
            f'</tr>'
        )
        return _HtmlResp(f"<html><body><table>{body}</table></body></html>", url=url)

    sc.authenticated_get = fake_get
    anns = sc.get_course_announcements(71177, course_soup=course_soup)
    sc.close()

    assert len(visited_urls) == 7, f"Expected all 7 forums fetched (no [:4] cap), got {len(visited_urls)}"
    assert len(anns) == 7
    # Announcement forums (105, 106, 107) visited FIRST
    visited_ids = [re.search(r"id=(\d+)", u).group(1) for u in visited_urls]
    assert visited_ids[:3] == ["105", "106", "107"]


def test_m_ann_urljoin_safe_across_scraper_modules(monkeypatch):
    # Static check: no raw LMS_BASE_URL + href or f"{LMS_BASE_URL}{...}" or f"{LMS_BASE_URL}/..."
    for mod in (scraper_parsers, scraper_core, scraper_quizzes, scraper_attendance):
        src = inspect.getsource(mod)
        assert "LMS_BASE_URL +" not in src, f"Raw concatenation 'LMS_BASE_URL +' found in {mod.__name__}"
        assert 'f"{LMS_BASE_URL}' not in src, f"Raw f-string LMS_BASE_URL concatenation found in {mod.__name__}"
        assert "f'{LMS_BASE_URL}" not in src, f"Raw f-string LMS_BASE_URL concatenation found in {mod.__name__}"

    # Runtime check with trailing slash on LMS_BASE_URL and varied href formats
    monkeypatch.setattr(scraper_parsers, "LMS_BASE_URL", "https://mulms.mutah.edu.jo/")
    html = """<html><body>
      <a href="/mod/forum/view.php?id=1">Rel Slash</a>
      <a href="mod/forum/view.php?id=2">Bare Rel</a>
      <a href="//mulms.mutah.edu.jo/mod/forum/view.php?id=3">Proto Rel</a>
      <a href="https://mulms.mutah.edu.jo/mod/forum/view.php?id=4">Absolute</a>
      <a href="HTTPS://mulms.mutah.edu.jo/mod/forum/view.php?id=5">Upper Scheme</a>
    </body></html>"""
    links = extract_forum_links(BeautifulSoup(html, "lxml"))
    urls_by_name = {name: url for url, name in links}
    assert urls_by_name["Rel Slash"] == "https://mulms.mutah.edu.jo/mod/forum/view.php?id=1"
    assert urls_by_name["Bare Rel"] == "https://mulms.mutah.edu.jo/mod/forum/view.php?id=2"
    assert urls_by_name["Proto Rel"] == "https://mulms.mutah.edu.jo/mod/forum/view.php?id=3"
    assert urls_by_name["Absolute"] == "https://mulms.mutah.edu.jo/mod/forum/view.php?id=4"
    assert urls_by_name["Upper Scheme"].lower() == "https://mulms.mutah.edu.jo/mod/forum/view.php?id=5"
    for u in urls_by_name.values():
        assert "https://mulms.mutah.edu.johttps://" not in u.lower()
        assert "//mod/" not in u

    # Resource links with protocol-relative & uppercase absolute URLs must also resolve cleanly
    res_html = """<html><body>
      <a href="//mulms.mutah.edu.jo/mod/resource/view.php?id=91">File A</a>
      <a href="HTTPS://mulms.mutah.edu.jo/mod/resource/view.php?id=92">File B</a>
    </body></html>"""
    resources = parse_resources_from_course_soup(BeautifulSoup(res_html, "lxml"), course_id=71177)
    assert len(resources) == 2
    for r in resources:
        assert r["raw_url"].lower().startswith("https://mulms.mutah.edu.jo/mod/resource/view.php?id=")
        assert "https://mulms.mutah.edu.johttps://" not in r["raw_url"].lower()


# ===========================================================================
# M-UTC: Explicit UTC ISO-8601 last_check in database.py & monitor.py
# ===========================================================================


def _setup_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    db_file = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DIRECT_URL", "")
    monkeypatch.setattr(real_db, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(real_db, "DATABASE_URL", "")
    monkeypatch.setattr(real_db, "DIRECT_URL", "")
    if hasattr(real_db, "close"):
        real_db.close()
    real_db.init_db()
    return db_file


def test_m_utc_update_last_check_stores_explicit_utc_iso8601(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _setup_sqlite(tmp_path, monkeypatch, "utc_check.db")
    tid = 9101
    real_db.save_user(tid, "stu_utc", "pw_utc")

    before = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    real_db.update_last_check(tid)
    after = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)

    user = real_db.get_user(tid)
    lc = user["last_check"]
    assert isinstance(lc, str) and lc, "last_check must be a non-empty string"
    # Must be explicit UTC ISO-8601 (with 'T' separator and 'Z' or '+00:00' suffix)
    assert "T" in lc, f"last_check is not ISO-8601: {lc!r}"
    assert lc.endswith("Z") or lc.endswith("+00:00"), f"last_check lacks explicit UTC timezone: {lc!r}"

    parsed = datetime.fromisoformat(lc.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert before <= parsed <= after


def test_m_utc_monitor_writes_and_evaluates_utc_last_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _setup_sqlite(tmp_path, monkeypatch, "monitor_utc.db")
    tid = 9102
    real_db.save_user(tid, "stu_mon_utc", "pw_mon_utc")
    real_db.set_monitoring(tid, True)
    real_db.update_check_interval(tid, 30)

    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        user = real_db.get_user(tid)
        # No monitored courses -> updates last_check directly
        await eng._check_user(user)

    asyncio.run(_run())

    updated = real_db.get_user(tid)
    lc = updated["last_check"]
    assert lc and "T" in lc and (lc.endswith("Z") or lc.endswith("+00:00")), f"Unexpected last_check: {lc!r}"

    # Freshly updated user with 30m interval is NOT due yet
    assert MonitorEngine._is_user_due(updated) is False

    # User checked 31 minutes ago in UTC IS due
    old_utc = (datetime.now(timezone.utc) - timedelta(minutes=31)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    assert MonitorEngine._is_user_due({"last_check": old_utc, "check_interval": 30}) is True

    # User checked 5 minutes ago in UTC+3 offset is NOT due for 30m interval
    tz_plus3 = timezone(timedelta(hours=3))
    recent_plus3 = (datetime.now(tz_plus3) - timedelta(minutes=5)).replace(microsecond=0).isoformat()
    assert MonitorEngine._is_user_due({"last_check": recent_plus3, "check_interval": 30}) is False

    # Legacy naive UTC string 5 minutes ago in UTC MUST NOT drift even if host timezone != UTC
    recent_naive_utc = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert MonitorEngine._is_user_due({"last_check": recent_naive_utc, "check_interval": 30}) is False
