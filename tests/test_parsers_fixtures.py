"""F3: Fixture-driven test suite covering all 4 Moodle 5 parsers + date parsing."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

# Configure isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from bs4 import BeautifulSoup

import reminder
import scraper.dates as scraper_dates
from lms_scraper import LMSScraper, LMSHttpClient
from scraper.attendance import parse_attendance_page_soup, parse_attendance_status_category
from scraper.parsers import (
    extract_course_page_completions,
    parse_activities_index_soup,
    parse_announcement_details_soup,
    parse_assignment_status_soup,
    parse_forum_announcements_soup,
    parse_submission_state,
)
from scraper.quizzes import parse_quiz_grade_text, parse_quiz_status_soup

FIXTURES_DIR = Path(__file__).parent / "fixtures"
AMMAN_TZ = ZoneInfo("Asia/Amman")


def _load_fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _load_section_soup(fixture_name: str, section_id: str) -> BeautifulSoup:
    full = BeautifulSoup(_load_fixture_text(fixture_name), "lxml")
    sec = full.find(id=section_id)
    assert sec is not None, f"Missing section #{section_id} in {fixture_name}"
    return BeautifulSoup(str(sec), "lxml")


class _FakeResp:
    def __init__(self, text: str, url: str = "https://mulms.mutah.edu.jo/course/view.php?id=71177"):
        self.text = text
        self.url = url
        self.status_code = 200
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.history = []

    def raise_for_status(self):
        return None

    def close(self):
        return None


# ===========================================================================
# 1. Submission Parser (Moodle 5 overview table, completion badges, status)
# ===========================================================================

def test_fixture_submission_moodle5_overview_table():
    soup = _load_section_soup("submission_moodle5.html", "fixture-overview-table")
    acts = parse_activities_index_soup(soup, 71177, "assign", parse_quiz_grade_fn=parse_quiz_grade_text)
    by_id = {a["id"]: a for a in acts}

    assert set(by_id) == {"assign_257625", "assign_284175", "assign_284176", "assign_284177"}
    assert by_id["assign_257625"]["name"] == "Homework #2"
    assert by_id["assign_257625"]["is_submitted"] is True
    assert "28 November" in by_id["assign_257625"]["due_date"]
    assert "1 November" not in by_id["assign_257625"]["due_date"]

    # Unix timestamp converted and volatile __ncforminfo stripped
    assert by_id["assign_284175"]["is_submitted"] is False
    assert "__ncforminfo" not in by_id["assign_284175"]["url"]
    assert "2026" in by_id["assign_284175"]["due_date"]

    # Draft with '-' due date must NOT leak allowsubmissionsfromdate into due_date
    assert by_id["assign_284176"]["is_submitted"] is False
    assert by_id["assign_284176"]["due_date"] == ""

    # Arabic unsubmitted must remain False
    assert by_id["assign_284177"]["is_submitted"] is False
    assert "27 November 2026" in by_id["assign_284177"]["due_date"]


def test_fixture_submission_moodle5_completion_and_activity_dates():
    soup = _load_section_soup("submission_moodle5.html", "fixture-course-completion")
    completed = extract_course_page_completions(soup)
    dates = scraper_dates.extract_course_page_activity_dates(soup)

    assert "284175" not in completed  # manual:mark-done
    assert "284180" not in completed  # Done: View only
    assert "284181" not in completed  # Done: View + To do: Make a submission
    assert "290627" in completed      # manual:undo Done

    assert dates["284175"] == "Wednesday, 25 November 2026, 12:00 AM"
    assert dates["290627"] == "Monday, 23 November 2026, 2:40 PM"


def test_fixture_submission_moodle5_individual_status():
    not_sub_soup = _load_section_soup("submission_moodle5.html", "fixture-assign-not-submitted")
    st_not_sub = parse_assignment_status_soup(not_sub_soup)
    assert st_not_sub["is_submitted"] is False
    assert st_not_sub["due_date"] == "Wednesday, 25 November 2026, 12:00 AM"

    sub_ungraded_soup = _load_section_soup("submission_moodle5.html", "fixture-assign-submitted-ungraded")
    st_sub = parse_assignment_status_soup(sub_ungraded_soup)
    assert st_sub["is_submitted"] is True
    assert st_sub["grading_status"] == "Not graded"
    assert parse_submission_state("Not submitted") is False
    assert parse_submission_state("Submitted for grading") is True


# ===========================================================================
# 2. Quiz Grade Parser (Marks vs Grade precedence, graded, ungraded, view-only)
# ===========================================================================

def test_fixture_quiz_grade_moodle5_marks_vs_grade_precedence():
    soup = _load_section_soup("quiz_grade_moodle5.html", "fixture-quiz-marks-and-grade")
    res = parse_quiz_status_soup(soup)
    assert res["is_submitted"] is True
    assert res["grade_val"] == "10.00"
    assert res["grade_max"] == "0–10.00"
    assert res["percentage"] == "100%"


def test_fixture_quiz_grade_moodle5_full_graded_ungraded_and_in_progress():
    graded_soup = _load_section_soup("quiz_grade_moodle5.html", "fixture-quiz-graded")
    graded = parse_quiz_status_soup(graded_soup)
    assert graded["is_submitted"] is True
    assert graded["grade_val"] == "15.00"
    assert graded["grade_max"] == "0–15.00"
    assert graded["percentage"] == "100%"
    assert "15.00/15.00" in graded["feedback"]
    assert graded["due_date"] == "Monday, 16 November 2026, 2:00 PM"

    ungraded_soup = _load_section_soup("quiz_grade_moodle5.html", "fixture-quiz-ungraded")
    ungraded = parse_quiz_status_soup(ungraded_soup)
    assert ungraded["is_submitted"] is True
    assert ungraded["grade_val"] == ""

    in_prog_soup = _load_section_soup("quiz_grade_moodle5.html", "fixture-quiz-in-progress-view-only")
    in_prog = parse_quiz_status_soup(in_prog_soup)
    assert in_prog["is_submitted"] is False
    assert in_prog["grade_val"] == ""


def test_fixture_quiz_grade_text_variants():
    assert parse_quiz_grade_text("15.00 out of 15.00 ( 100 %)") == {
        "grade_val": "15.00",
        "grade_max": "0–15.00",
        "percentage": "100%",
    }
    assert parse_quiz_grade_text("14 من 15 (93.3%)") == {
        "grade_val": "14",
        "grade_max": "0–15",
        "percentage": "93.3%",
    }
    assert parse_quiz_grade_text("١٣.٥ / ١٥") == {
        "grade_val": "13.5",
        "grade_max": "0–15",
        "percentage": "",
    }
    assert parse_quiz_grade_text("Not yet graded")["grade_val"] == ""
    assert parse_quiz_grade_text("Review not permitted")["grade_val"] == ""


# ===========================================================================
# 3. Attendance Parser (dynamic col_map, Group column, P/A/L/E, Arabic reordered)
# ===========================================================================

def test_fixture_attendance_moodle5_english_with_group_and_pale():
    soup = _load_section_soup("attendance_moodle5.html", "fixture-attendance-en-group")
    rec = parse_attendance_page_soup(
        soup,
        course_id=71177,
        cmid="285625",
        title="Attendance",
        base_url="https://mulms.mutah.edu.jo/mod/attendance/view.php?id=285625",
    )
    assert rec["att_id"] == "att_285625"
    assert rec["taken_sessions"] == 4
    assert rec["total_sessions"] == 5
    assert rec["present_count"] == 1
    assert rec["absent_count"] == 1
    assert rec["late_count"] == 1
    assert rec["excused_count"] == 1
    assert rec["points_str"] == "5 / 8"
    assert rec["percentage_str"] == "62.5%"
    assert len(rec["sessions"]) == 4  # '?' future lecture skipped
    assert rec["sessions"][0]["group"] == "Section 1"
    assert rec["sessions"][0]["status"] == "P"
    assert rec["sessions"][1]["status"] == "A"
    assert rec["sessions"][2]["status"] == "L"
    assert rec["sessions"][3]["status"] == "E"


def test_fixture_attendance_moodle5_arabic_reordered_columns():
    soup = _load_section_soup("attendance_moodle5.html", "fixture-attendance-ar-reordered")
    rec = parse_attendance_page_soup(
        soup,
        course_id=71177,
        cmid="285626",
        title="الحضور والغياب",
        base_url="https://mulms.mutah.edu.jo/mod/attendance/view.php?id=285626",
    )
    assert rec["taken_sessions"] == 4
    assert rec["total_sessions"] == 6
    assert rec["present_count"] == 1
    assert rec["absent_count"] == 1
    assert rec["late_count"] == 1
    assert rec["excused_count"] == 1
    assert rec["percentage_str"] == "75%"
    assert rec["sessions"][0]["date"] == "الأحد 15 نوفمبر 2026"
    assert rec["sessions"][0]["group"] == "شعبة 2"
    assert parse_attendance_status_category("P") == "present"
    assert parse_attendance_status_category("غائب") == "absent"


# ===========================================================================
# 4. Announcements Parser (discussion table, __ncforminfo cleaning, post details)
# ===========================================================================

def test_fixture_announcements_moodle5_forum_list_and_post_details():
    list_soup = _load_section_soup("announcements_moodle5.html", "fixture-forum-list")
    anns = parse_forum_announcements_soup(list_soup, course_id=71177, forum_name="Announcements")
    assert len(anns) == 2

    a0, a1 = anns
    assert a0["discuss_id"] == "discuss_40123"
    assert a0["title"] == "Midterm Exam Schedule & Rooms"
    assert a0["author"] == "Dr. Ahmad Al-Tarawneh"
    assert a0["posted_date"] == "Tuesday, 24 November 2026, 10:15 AM"
    assert "__ncforminfo" not in a0["url"]

    assert a1["discuss_id"] == "discuss_40124"
    assert a1["title"] == "تأجيل محاضرة يوم الخميس"  # accesshide 'Forum' stripped
    assert a1["author"] == "د. سمر الخطيب"
    assert a1["url"] == "https://mulms.mutah.edu.jo/mod/forum/discuss.php?d=40124"

    detail_soup = _load_section_soup("announcements_moodle5.html", "fixture-discuss-detail")
    details = parse_announcement_details_soup(detail_soup)
    assert "Hall 204" in details["body"]
    # Author must be scoped to data-region="post" (NOT navbar Student Decoy Name!)
    assert details["author"] == "Dr. Ahmad Al-Tarawneh"
    assert details["date"] == "Tuesday, 24 November 2026, 10:15 AM"


def test_fixture_lms_scraper_end_to_end_via_mock_http():
    """Verify LMSScraper methods integrate cleanly with all 4 Moodle 5 HTML fixtures."""
    sc = LMSScraper("stu", "pw", http_client=LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0)))
    try:
        sub_html = str(_load_section_soup("submission_moodle5.html", "fixture-overview-table"))
        quiz_html = str(_load_section_soup("quiz_grade_moodle5.html", "fixture-quiz-graded"))
        att_html = str(_load_section_soup("attendance_moodle5.html", "fixture-attendance-en-group"))
        ann_html = str(_load_section_soup("announcements_moodle5.html", "fixture-forum-list"))
        disc_html = str(_load_section_soup("announcements_moodle5.html", "fixture-discuss-detail"))

        sc.authenticated_get = MagicMock(return_value=_FakeResp(sub_html))
        assert len(sc._get_activities_from_index(71177, "assign")) == 4

        sc.authenticated_get = MagicMock(return_value=_FakeResp(quiz_html))
        assert sc.get_quiz_status("https://mulms.mutah.edu.jo/mod/quiz/view.php?id=258139")["grade_val"] == "15.00"

        course_soup = BeautifulSoup(
            '<a href="/mod/attendance/view.php?id=285625">Attendance</a>'
            '<a href="/mod/forum/view.php?id=901">Announcements</a>',
            "lxml",
        )
        sc.authenticated_get = MagicMock(return_value=_FakeResp(att_html))
        assert sc.get_course_attendance(71177, course_soup=course_soup)[0]["taken_sessions"] == 4

        sc.authenticated_get = MagicMock(return_value=_FakeResp(ann_html))
        assert len(sc.get_course_announcements(71177, course_soup=course_soup)) == 2

        sc.authenticated_get = MagicMock(return_value=_FakeResp(disc_html))
        assert sc.get_announcement_details("https://mulms.mutah.edu.jo/mod/forum/discuss.php?d=40123")[
            "author"
        ] == "Dr. Ahmad Al-Tarawneh"
    finally:
        sc.close()


# ===========================================================================
# 5. Dates Fixtures (reminder._parse_due_date + scraper.dates.normalize_due_date_year)
# ===========================================================================

def test_fixture_dates_samples(monkeypatch):
    data = json.loads(_load_fixture_text("dates_samples.json"))

    for case in data["amman_parse_cases"]:
        expected_ts = int(datetime(*case["expected"], tzinfo=AMMAN_TZ).timestamp())
        assert reminder._parse_due_date(case["input"]) == expected_ts, case

    for case in data["yearless_boundary_cases"]:
        fake_now = datetime(*case["fake_now"], tzinfo=AMMAN_TZ)
        monkeypatch.setattr(reminder, "_now_jordan", lambda fn=fake_now: fn)
        expected_ts = int(datetime(*case["expected"], tzinfo=AMMAN_TZ).timestamp())
        assert reminder._parse_due_date(case["input"]) == expected_ts, case

    for case in data["normalize_year_cases"]:
        fake_now = datetime(*case["fake_now"], tzinfo=AMMAN_TZ)
        monkeypatch.setattr(scraper_dates, "_now_amman", lambda fn=fake_now: fn)
        normalized = scraper_dates.normalize_due_date_year(case["input"])
        assert case["must_contain"] in normalized, (case, normalized)

    for bad in data["invalid_cases"]:
        assert reminder._parse_due_date(bad) is None, bad
