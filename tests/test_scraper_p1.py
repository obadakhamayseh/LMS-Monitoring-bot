import os
from typing import Any, Dict, List
from unittest.mock import MagicMock

from cryptography.fernet import Fernet

# Configure safe isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import pytest
from bs4 import BeautifulSoup as RealBeautifulSoup

import lms_scraper
from lms_scraper import LMSScraper, LMSHttpClient, LMSWafError


class _HtmlResp:
    def __init__(
        self,
        text: str,
        url: str = "https://mulms.mutah.edu.jo/course/view.php?id=71177",
        status_code: int = 200,
        headers: Dict[str, str] | None = None,
        json_data: Any = None,
    ):
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = headers if headers is not None else {"Content-Type": "text/html; charset=utf-8"}
        self._json_data = json_data
        self.history = []
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        raw = self.text.encode("utf-8")
        for i in range(0, len(raw), chunk_size):
            yield raw[i : i + chunk_size]

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


# ===========================================================================
# H8: Dynamic attendance col_map (with Group column + P/A/L/E abbreviations)
# ===========================================================================

ATTENDANCE_WITH_GROUP_AND_ABBREV_HTML = """<html><body>
<table class="generaltable attwidth boxaligncenter">
  <thead>
    <tr>
      <th>Date</th>
      <th>Group</th>
      <th>Description</th>
      <th>Status</th>
      <th>Points</th>
      <th>Remarks</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Sun 15 Nov 2026 10:00AM</td>
      <td>Section 1</td>
      <td>Regular class session</td>
      <td>P</td>
      <td>2 / 2</td>
      <td>On time</td>
    </tr>
    <tr>
      <td>Tue 17 Nov 2026 10:00AM</td>
      <td>Section 1</td>
      <td>Lab session</td>
      <td>A</td>
      <td>0 / 2</td>
      <td>Unexcused</td>
    </tr>
    <tr>
      <td>Thu 19 Nov 2026 10:00AM</td>
      <td>Section 1</td>
      <td>Tutorial</td>
      <td>L</td>
      <td>1 / 2</td>
      <td>Arrived 10m late</td>
    </tr>
    <tr>
      <td>Sun 22 Nov 2026 10:00AM</td>
      <td>Section 1</td>
      <td>Lecture 4</td>
      <td>E</td>
      <td>2 / 2</td>
      <td>Medical report</td>
    </tr>
    <tr>
      <td>Tue 24 Nov 2026 10:00AM</td>
      <td>Section 1</td>
      <td>Future lecture</td>
      <td>?</td>
      <td>0 / 2</td>
      <td></td>
    </tr>
  </tbody>
</table>
<table class="attlist">
  <tr><td>Taken sessions:</td><td>4</td></tr>
  <tr><td>Points over taken sessions:</td><td>5 / 8</td></tr>
  <tr><td>Percentage over taken sessions:</td><td>62.5%</td></tr>
</table>
</body></html>"""


ATTENDANCE_ARABIC_REORDERED_HTML = """<html><body>
<table class="generaltable">
  <tr>
    <th>المجموعة</th>
    <th>التاريخ</th>
    <th>الوصف</th>
    <th>الحالة</th>
    <th>النقاط</th>
    <th>ملاحظات</th>
  </tr>
  <tr>
    <td>شعبة 2</td>
    <td>الأحد 15 نوفمبر 2026</td>
    <td>محاضرة 1</td>
    <td>حاضر</td>
    <td>2/2</td>
    <td>ممتاز</td>
  </tr>
  <tr>
    <td>شعبة 2</td>
    <td>الثلاثاء 17 نوفمبر 2026</td>
    <td>محاضرة 2</td>
    <td>غائب</td>
    <td>0/2</td>
    <td>بدون عذر</td>
  </tr>
</table>
</body></html>"""


def test_h8_attendance_dynamic_col_map_with_group_column_and_pale():
    sc = LMSScraper("u", "p", http_client=LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0)))
    course_soup = RealBeautifulSoup(
        '<html><body><a href="/mod/attendance/view.php?id=555">Attendance</a></body></html>',
        "lxml",
    )
    sc.http.session.get = lambda url, **kw: _HtmlResp(ATTENDANCE_WITH_GROUP_AND_ABBREV_HTML, url=url)

    records = sc.get_course_attendance(71177, course_soup=course_soup)
    assert len(records) == 1
    rec = records[0]
    assert rec["present_count"] == 1
    assert rec["absent_count"] == 1
    assert rec["late_count"] == 1
    assert rec["excused_count"] == 1
    assert len(rec["sessions"]) == 4  # '?' future session skipped

    s0, s1, s2, s3 = rec["sessions"]
    assert s0["date"] == "Sun 15 Nov 2026 10:00AM"
    assert s0["description"] == "Regular class session"
    assert s0["status"] == "P"
    assert s0["points"] == "2 / 2"
    assert s0["remarks"] == "On time"
    assert s1["status"] == "A"
    assert s2["status"] == "L"
    assert s3["status"] == "E"
    sc.close()


def test_h8_attendance_dynamic_col_map_arabic_reordered_columns():
    sc = LMSScraper("u", "p", http_client=LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0)))
    course_soup = RealBeautifulSoup(
        '<html><body><a href="/mod/attendance/view.php?id=556">الحضور</a></body></html>',
        "lxml",
    )
    sc.http.session.get = lambda url, **kw: _HtmlResp(ATTENDANCE_ARABIC_REORDERED_HTML, url=url)

    records = sc.get_course_attendance(71177, course_soup=course_soup)
    assert len(records) == 1
    rec = records[0]
    assert rec["present_count"] == 1
    assert rec["absent_count"] == 1
    assert rec["sessions"][0]["date"] == "الأحد 15 نوفمبر 2026"
    assert rec["sessions"][0]["description"] == "محاضرة 1"
    assert rec["sessions"][0]["status"] == "حاضر"
    assert rec["sessions"][0]["points"] == "2/2"
    assert rec["sessions"][0]["remarks"] == "ممتاز"
    sc.close()


# ===========================================================================
# H8: Exclude allowsubmissionsfromdate from due_col_idx and fallback scan
# ===========================================================================

MOODLE5_ALLOW_FROM_AND_NO_DUE_HTML = """<html><body>
<table class="course-overview-table">
  <tr>
    <th data-mdl-overview-column="name">Name</th>
    <th data-mdl-overview-column="allowsubmissionsfromdate">Allow submissions from</th>
    <th data-mdl-overview-column="submissionstatus">Submission status</th>
  </tr>
  <tr data-mdl-overview-cmid="301001">
    <td data-mdl-overview-item="name"><a class="activityname" href="https://mulms.mutah.edu.jo/mod/assign/view.php?id=301001">Open Only HW</a></td>
    <td data-mdl-overview-item="allowsubmissionsfromdate">Monday, 1 November 2026, 8:00 AM</td>
    <td data-mdl-overview-item="submissionstatus">Not submitted</td>
  </tr>
</table>
</body></html>"""

MOODLE4_ALLOW_FROM_AFTER_DUE_HTML = """<html><body>
<table class="generaltable">
  <tr>
    <th>Name</th>
    <th>Due date</th>
    <th>Allow submissions from</th>
    <th>Submission status</th>
  </tr>
  <tr>
    <td><a href="https://mulms.mutah.edu.jo/mod/assign/view.php?id=301002">HW 2</a></td>
    <td>Friday, 27 November 2026, 11:59 PM</td>
    <td>Monday, 1 November 2026, 8:00 AM</td>
    <td>Not submitted</td>
  </tr>
</table>
</body></html>"""


def test_h8_allowsubmissionsfromdate_never_parsed_as_due_date():
    sc = LMSScraper("u", "p", http_client=LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0)))

    # Case 1: Only allowsubmissionsfromdate column exists (no due date column)
    sc.http.session.get = lambda url, **kw: _HtmlResp(MOODLE5_ALLOW_FROM_AND_NO_DUE_HTML, url=url)
    acts1 = sc._get_activities_from_index(71177, "assign")
    assert len(acts1) == 1
    assert acts1[0]["due_date"] == "", f"allowsubmissionsfromdate leaked into due_date: {acts1[0]['due_date']!r}"

    # Case 2: allowsubmissionsfromdate appears AFTER Due date in header (must NOT overwrite due_col_idx)
    sc.http.session.get = lambda url, **kw: _HtmlResp(MOODLE4_ALLOW_FROM_AFTER_DUE_HTML, url=url)
    acts2 = sc._get_activities_from_index(71177, "assign")
    assert len(acts2) == 1
    assert "27 November 2026" in acts2[0]["due_date"]
    assert "1 November 2026" not in acts2[0]["due_date"]
    sc.close()


# ===========================================================================
# H8: AJAX requests in get_courses() route through self.http & detect WAF
# ===========================================================================

def test_h8_get_courses_ajax_routes_through_http_and_detects_waf():
    sc = LMSScraper("u", "p", http_client=LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0)))
    sc.logged_in = True
    sc.sesskey = "TESTKEY"

    calls: List[tuple] = []
    waf_html = '<html><body><title>Validation request</title><h3>User validation required to continue..</h3><img src="/captcha.gif"></body></html>'

    def fake_get(url, **kw):
        calls.append(("GET", url))
        if "/my/courses.php" in url:
            return _HtmlResp('<html><body><input name="sesskey" value="TESTKEY"></body></html>', url=url)
        return _HtmlResp("<html><body>fallback should not run after WAF</body></html>", url=url)

    def fake_post(url, **kw):
        calls.append(("POST", url))
        # Simulate NetScaler WAF returning CAPTCHA HTML with missing/non-html Content-Type
        return _HtmlResp(waf_html, url=url, headers={"Content-Type": "text/plain"})

    sc.http.session.get = fake_get
    sc.http.session.post = fake_post

    # 1. With raise_auth_errors=True, AJAX WAF must raise LMSWafError
    with pytest.raises(LMSWafError):
        sc.get_courses(raise_auth_errors=True)

    assert sc.waf_blocked is True
    assert sc.http.waf_cooldown_active is True
    # Only /my/courses.php GET and ONE AJAX POST should have happened (no 2nd classification or /my/ fallback)
    assert len(calls) == 2
    assert calls[0][0] == "GET" and "/my/courses.php" in calls[0][1]
    assert calls[1][0] == "POST" and "/lib/ajax/service.php" in calls[1][1]

    # 2. Verify _is_waf_response exists and detects WAF regardless of Content-Type
    assert hasattr(sc, "_is_waf_response")
    assert sc._is_waf_response(_HtmlResp(waf_html, headers={"Content-Type": "text/plain"})) is True
    sc.close()


# ===========================================================================
# H7: No duplicate BeautifulSoup parsing per HTML page
# ===========================================================================

def test_h7_beautifulsoup_called_once_per_page(monkeypatch):
    parse_count = 0

    def spy_bs(markup, *args, **kwargs):
        nonlocal parse_count
        parse_count += 1
        return RealBeautifulSoup(markup, *args, **kwargs)

    monkeypatch.setattr(lms_scraper, "BeautifulSoup", spy_bs)

    sc = LMSScraper("u", "p", http_client=LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0)))
    sc.logged_in = True

    course_html = """<html><body>
      <li class="activity assign" data-id="101"><a href="/mod/assign/view.php?id=101">HW 1</a></li>
      <a href="/mod/resource/view.php?id=202">Slides PDF</a>
    </body></html>"""
    empty_html = "<html><body><div>Empty</div></body></html>"

    def fake_get(url, **kw):
        if "/course/view.php" in url:
            return _HtmlResp(course_html, url=url)
        return _HtmlResp(empty_html, url=url)

    sc.http.session.get = fake_get

    # Single course page fetch via authenticated_get + _get_resources_from_course_page
    parse_count = 0
    resources, soup = sc._get_resources_from_course_page(71177, return_soup=True)
    assert len(resources) == 1
    assert soup is not None
    assert parse_count == 1, f"Expected 1 BeautifulSoup parse for course page, got {parse_count}"

    # Single index page fetch via authenticated_get + _get_activities_from_index
    parse_count = 0
    sc._get_activities_from_index(71177, "assign")
    assert parse_count == 1, f"Expected 1 BeautifulSoup parse for index page, got {parse_count}"

    # Single grades page fetch via authenticated_get + get_course_grades
    parse_count = 0
    sc.get_course_grades(71177)
    assert parse_count == 1, f"Expected 1 BeautifulSoup parse for grades page, got {parse_count}"

    # Interstitial HTML page in download_resource must also parse HTML only once
    parse_count = 0
    interstitial = _HtmlResp(
        '<html><body><a href="https://mulms.mutah.edu.jo/pluginfile.php/1/mod_resource/content/1/doc.pdf">PDF</a></body></html>',
        url="https://mulms.mutah.edu.jo/mod/resource/view.php?id=202",
    )
    pdf_resp = _HtmlResp(
        "%PDF-1.4",
        url="https://mulms.mutah.edu.jo/pluginfile.php/1/mod_resource/content/1/doc.pdf",
        headers={"Content-Type": "application/pdf"},
    )
    seq = iter([interstitial, pdf_resp])
    sc.http.session.get = lambda url, **kw: next(seq)
    dl = sc.download_resource("https://mulms.mutah.edu.jo/mod/resource/view.php?id=202")
    assert dl is not None
    assert parse_count == 1, f"Expected 1 BeautifulSoup parse for interstitial HTML, got {parse_count}"
    sc.close()
