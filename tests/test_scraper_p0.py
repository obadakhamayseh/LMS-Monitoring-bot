import os
import threading
import time
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
from bs4 import BeautifulSoup

import lms_scraper
from lms_scraper import LMSScraper


# ---------------------------------------------------------------------------
# Helpers / Mocks (strictly offline; no live network calls)
# ---------------------------------------------------------------------------

class _NoTextResponse:
    """Fake streaming/binary response whose .text property raises if accessed."""

    def __init__(
        self,
        url: str = "https://mulms.mutah.edu.jo/pluginfile.php/1/mod_resource/content/1/slides.pdf",
        status_code: int = 200,
        headers: Dict[str, str] | None = None,
        chunks: List[bytes] | None = None,
    ):
        self.url = url
        self.status_code = status_code
        self.headers = headers if headers is not None else {"Content-Type": "application/pdf"}
        self._chunks = chunks if chunks is not None else [b"%PDF-1.4 sample"]
        self.history = []
        self.closed = False

    @property
    def text(self) -> str:
        raise AssertionError("resp.text MUST NOT be read for binary or stream=True responses!")

    def iter_content(self, chunk_size: int = 65536):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _HtmlResponse:
    """Fake HTML response that tracks close() calls."""

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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# Moodle 5.x fixtures derived directly from scratch/test_moodle5_compat.py & test_quiz_grades.py
MOODLE5_LOGIN_GET_HTML = '''<html><body class="notloggedin"><form id="login" action="https://mulms.mutah.edu.jo/login/index.php" method="post">
<input name="anchor" type="hidden" value="">
<input name="logintoken" type="hidden" value="TOK123">
<input name="username" type="text" value="">
<input name="password" type="password" value="">
<input name="__ncforminfo" type="hidden" value="NCFORM_SECRET_VALUE">
</form></body></html>'''

MOODLE5_DASHBOARD_HTML = '''<html><head><script>
M.cfg = {"wwwroot":"https:\\/\\/mulms.mutah.edu.jo","apibase":"https:\\/\\/mulms.mutah.edu.jo\\/api","sesskey":"NIIokUjo6u","theme":"classic","userId":112607};
</script></head><body class="limitedwidth path-my theme"><h1>Dashboard</h1><span class="usertext">طالب تجريبي</span></body></html>'''

MOODLE5_OVERVIEW_TABLE_HTML = '''<html><body>
<table class="course-overview-table boxaligncenter table w-100 d-block d-md-table">
  <tr>
    <th data-mdl-overview-column="name">Name</th>
    <th data-mdl-overview-column="duedate">Due date</th>
    <th data-mdl-overview-column="submissionstatus">Submission status</th>
  </tr>
  <tr data-mdl-overview-cmid="257625">
    <td data-mdl-overview-item="name" data-mdl-overview-value="Homework #2">
      <div class="d-flex flex-column">
        <div class="fw-bold"><a class="activityname" href="https://mulms.mutah.edu.jo/mod/assign/view.php?id=257625">Homework #2</a></div>
      </div>
    </td>
    <td data-mdl-overview-item="duedate">Saturday, 28 November 2026, 11:59 PM</td>
    <td data-mdl-overview-item="submissionstatus">Submitted for grading</td>
  </tr>
  <tr data-mdl-overview-cmid="284175">
    <td data-mdl-overview-item="name" data-mdl-overview-value="Project phase 1">
      <div class="fw-bold"><a class="activityname" href="https://mulms.mutah.edu.jo/mod/assign/view.php?id=284175">Project phase 1</a></div>
    </td>
    <td data-mdl-overview-item="duedate">Wednesday, 25 November 2026, 12:00 AM</td>
    <td data-mdl-overview-item="submissionstatus">Not submitted</td>
  </tr>
  <tr data-mdl-overview-cmid="284176">
    <td data-mdl-overview-item="name" data-mdl-overview-value="Lab Draft">
      <div class="fw-bold"><a class="activityname" href="https://mulms.mutah.edu.jo/mod/assign/view.php?id=284176">Lab Draft</a></div>
    </td>
    <td data-mdl-overview-item="duedate">Thursday, 26 November 2026, 11:59 PM</td>
    <td data-mdl-overview-item="submissionstatus">Draft (not submitted)</td>
  </tr>
  <tr data-mdl-overview-cmid="284177">
    <td data-mdl-overview-item="name" data-mdl-overview-value="Arabic Unsubmitted">
      <div class="fw-bold"><a class="activityname" href="https://mulms.mutah.edu.jo/mod/assign/view.php?id=284177">Arabic Unsubmitted</a></div>
    </td>
    <td data-mdl-overview-item="duedate">Friday, 27 November 2026, 11:59 PM</td>
    <td data-mdl-overview-item="submissionstatus">لم تسلم</td>
  </tr>
</table></body></html>'''

MOODLE5_COURSE_COMPLETION_HTML = '''<html><body><ul>
  <li class="activity assign modtype_assign" id="module-284175" data-id="284175">
    <a href="/mod/assign/view.php?id=284175">Project phase 1</a>
    <div data-region="activity-dates">Opened: Sunday, 8 November 2026, 12:00 AM Due: Wednesday, 25 November 2026, 12:00 AM</div>
    <div data-region="completion-info"><button class="btn btn-subtle-body btn-sm" data-toggletype="manual:mark-done">Mark as done</button></div>
  </li>
  <li class="activity assign modtype_assign" id="module-284180" data-id="284180">
    <a href="/mod/assign/view.php?id=284180">View Only Assignment</a>
    <div data-region="completion-info">
      <span class="badge rounded-pill alert-success"><strong>Done:</strong> View</span>
    </div>
  </li>
  <li class="activity assign modtype_assign" id="module-284181" data-id="284181">
    <a href="/mod/assign/view.php?id=284181">Partial Completion Assignment</a>
    <div data-region="completion-info">
      <span class="badge rounded-pill alert-success"><strong>Done:</strong> View</span>
      <span class="badge rounded-pill alert-secondary"><strong>To do:</strong> Make a submission</span>
    </div>
  </li>
  <li class="activity quiz modtype_quiz" id="module-290627" data-id="290627">
    <a href="/mod/quiz/view.php?id=290627">QUIZ 2</a>
    <div data-region="activity-dates">Opened: Monday, 23 November 2026, 1:30 PM Closed: Monday, 23 November 2026, 2:40 PM</div>
    <div data-region="completion-info"><button class="btn btn-success btn-sm" data-toggletype="manual:undo">Done</button></div>
  </li>
</ul></body></html>'''


# ===========================================================================
# C2: LMSHttpClient thread safety, pacing, routing & FetchResult API
# ===========================================================================

def test_c2_http_client_serializes_concurrent_requests():
    LMSHttpClient = getattr(lms_scraper, "LMSHttpClient")
    client = LMSHttpClient(min_interval=0.02, jitter=(0.0, 0.0))

    in_flight = 0
    max_in_flight = 0
    counter_lock = threading.Lock()
    call_times: List[float] = []

    def fake_get(url, **kwargs):
        nonlocal in_flight, max_in_flight
        with counter_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            call_times.append(time.monotonic())
        time.sleep(0.03)
        with counter_lock:
            in_flight -= 1
        return _HtmlResponse("<html><body>ok</body></html>", url=url)

    client.session.get = fake_get

    errors = []

    def worker(i: int):
        try:
            r = client.get(f"https://mulms.mutah.edu.jo/course/view.php?id={i}")
            r.close()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert max_in_flight == 1, f"Expected strictly serialized requests, got max_in_flight={max_in_flight}"
    assert len(call_times) == 4
    client.close()


def test_c2_fetch_course_content_returns_fetch_result_without_mutating_self():
    FetchResult = getattr(lms_scraper, "FetchResult")
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session
    sc.last_fetch_ok = True
    sc.session_expired = False
    sc.waf_blocked = False

    # Simulate WAF response on course fetch
    waf_html = '<html><body><title>Validation request</title><h3>User validation required to continue..</h3><img src="/captcha.gif"></body></html>'
    sc.http.session.get = lambda url, **kw: _HtmlResponse(waf_html, url=url)

    res = sc.fetch_course_content(71177)
    assert isinstance(res, FetchResult)
    assert res.ok is False
    assert res.waf_blocked is True
    assert res.session_expired is False
    assert res.error
    # fetch_course_content must NOT mutate legacy flags on self
    assert sc.last_fetch_ok is True
    assert sc.waf_blocked is False
    assert sc.session_expired is False
    sc.close()


def test_c2_fetch_course_content_session_expired_result_and_legacy_wrapper():
    FetchResult = getattr(lms_scraper, "FetchResult")
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session

    sc.http.session.get = lambda url, **kw: _HtmlResponse(
        MOODLE5_LOGIN_GET_HTML,
        url="https://mulms.mutah.edu.jo/login/index.php",
    )

    res = sc.fetch_course_content(71177)
    assert isinstance(res, FetchResult)
    assert res.ok is False
    assert res.session_expired is True
    assert res.waf_blocked is False
    assert sc.session_expired is False

    # Backward-compatible get_course_content wrapper returns dict and mirrors legacy attrs
    content = sc.get_course_content(71177)
    assert isinstance(content, dict)
    assert sc.last_fetch_ok is False
    assert sc.session_expired is True
    sc.close()


def test_c2_login_and_ajax_route_through_http_client():
    sc = LMSScraper("student", "secret")
    recorded: List[tuple] = []

    def spy_request(method, url, **kwargs):
        recorded.append((method.upper(), url))
        if "/login/index.php" in url and method.upper() == "GET":
            return _HtmlResponse(MOODLE5_LOGIN_GET_HTML, url=url)
        if "/login/index.php" in url and method.upper() == "POST":
            return _HtmlResponse(MOODLE5_DASHBOARD_HTML, url="https://mulms.mutah.edu.jo/my/")
        if "/lib/ajax/service.php" in url:
            return _HtmlResponse(
                '[{"error": false, "data": {"courses": [{"id": 71177, "fullname": "CS101"}]}}]',
                url=url,
                headers={"Content-Type": "application/json"},
                json_data=[{"error": False, "data": {"courses": [{"id": 71177, "fullname": "CS101"}]}}],
            )
        return _HtmlResponse("<html><body></body></html>", url=url)

    sc.http.request = spy_request
    assert sc.login() is True
    courses = sc.get_courses()
    assert any(c["id"] == 71177 for c in courses)
    methods_and_paths = [(m, u.split("?")[0]) for m, u in recorded]
    assert ("GET", "https://mulms.mutah.edu.jo/login/index.php") in methods_and_paths
    assert ("POST", "https://mulms.mutah.edu.jo/login/index.php") in methods_and_paths
    assert ("POST", "https://mulms.mutah.edu.jo/lib/ajax/service.php") in methods_and_paths
    sc.close()


# ===========================================================================
# C3: Stream/binary safety (no resp.text), Host allowlist, download hardening
# ===========================================================================

def test_c3_authenticated_get_never_reads_text_on_stream_or_binary():
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session

    binary_resp = _NoTextResponse(
        url="https://mulms.mutah.edu.jo/pluginfile.php/99/mod_resource/content/1/file.pdf",
        headers={"Content-Type": "application/pdf"},
    )
    sc.http.session.get = lambda url, **kw: binary_resp

    # stream=True with binary Content-Type must NOT touch .text
    r1 = sc.authenticated_get(
        "https://mulms.mutah.edu.jo/pluginfile.php/99/mod_resource/content/1/file.pdf",
        stream=True,
    )
    assert r1 is binary_resp
    r1.close()

    # stream=True EVEN with text/html Content-Type must NOT touch .text in authenticated_get
    html_stream_resp = _NoTextResponse(
        url="https://mulms.mutah.edu.jo/mod/resource/view.php?id=10",
        headers={"Content-Type": "text/html; charset=utf-8"},
    )
    sc.http.session.get = lambda url, **kw: html_stream_resp
    r2 = sc.authenticated_get("https://mulms.mutah.edu.jo/mod/resource/view.php?id=10", stream=True)
    assert r2 is html_stream_resp
    r2.close()

    # stream=False with binary Content-Type must NOT touch .text
    sc.http.session.get = lambda url, **kw: binary_resp
    r3 = sc.authenticated_get(
        "https://mulms.mutah.edu.jo/pluginfile.php/99/mod_resource/content/1/file.pdf",
        stream=False,
    )
    assert r3 is binary_resp
    r3.close()
    sc.close()


def test_c3_blocks_http_and_non_lms_hosts():
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session
    called = False

    def fail_if_called(url, **kw):
        nonlocal called
        called = True
        return _HtmlResponse("<html></html>", url=url)

    sc.http.session.get = fail_if_called

    with pytest.raises(ValueError):
        sc.authenticated_get("http://mulms.mutah.edu.jo/my/")
    with pytest.raises(ValueError):
        sc.authenticated_get("https://evil.example.com/steal")
    with pytest.raises(ValueError):
        sc.authenticated_get("https://mulms.mutah.edu.jo.evil.com/my/")
    assert called is False
    sc.close()


def test_c3_blocks_cross_host_redirect_and_closes_response():
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session

    redirected = _NoTextResponse(
        url="https://169.254.169.254/latest/meta-data/",
        headers={"Content-Type": "text/plain"},
    )
    sc.http.session.get = lambda url, **kw: redirected

    with pytest.raises(ValueError):
        sc.authenticated_get("https://mulms.mutah.edu.jo/mod/url/view.php?id=55", stream=True)
    assert redirected.closed is True
    sc.close()


def test_c3_download_resource_sanitizes_filename_and_handles_bad_content_length():
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session

    resp = _NoTextResponse(
        url="https://mulms.mutah.edu.jo/pluginfile.php/1/mod_resource/content/1/doc.pdf",
        status_code=200,
        headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="../../etc/evil\r\nname.pdf"',
            "Content-Length": "not-a-number",
        },
        chunks=[b"%PDF-1.4 hello"],
    )
    sc.http.session.get = lambda url, **kw: resp

    res = sc.download_resource("https://mulms.mutah.edu.jo/mod/resource/view.php?id=10", max_mb=5)
    assert res is not None
    filename, data = res
    assert data == b"%PDF-1.4 hello"
    assert ".." not in filename
    assert "/" not in filename
    assert "\\" not in filename
    assert "\r" not in filename and "\n" not in filename
    assert filename.endswith(".pdf")
    assert resp.closed is True
    sc.close()


def test_c3_download_resource_checks_status_200_on_second_request():
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session

    first = _HtmlResponse(
        '<html><body><a href="https://mulms.mutah.edu.jo/pluginfile.php/1/mod_resource/content/1/lec.pdf">PDF</a></body></html>',
        url="https://mulms.mutah.edu.jo/mod/resource/view.php?id=10",
        status_code=200,
    )
    second = _NoTextResponse(
        url="https://mulms.mutah.edu.jo/pluginfile.php/1/mod_resource/content/1/lec.pdf",
        status_code=403,
        headers={"Content-Type": "application/pdf"},
        chunks=[b"Forbidden"],
    )
    calls = iter([first, second])
    sc.http.session.get = lambda url, **kw: next(calls)

    res = sc.download_resource("https://mulms.mutah.edu.jo/mod/resource/view.php?id=10", max_mb=5)
    assert res is None
    assert first.closed is True
    assert second.closed is True
    sc.close()


def test_c3_download_resource_aborts_oversize_stream():
    sc = LMSScraper("student", "secret")
    sc.http = lms_scraper.LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0))
    sc.session = sc.http.session

    # 3 chunks of 512KB = 1.5MB > max_mb=1 without Content-Length header
    big_resp = _NoTextResponse(
        url="https://mulms.mutah.edu.jo/pluginfile.php/1/mod_resource/content/1/big.bin",
        status_code=200,
        headers={"Content-Type": "application/octet-stream"},
        chunks=[b"a" * (512 * 1024), b"b" * (512 * 1024), b"c" * (512 * 1024)],
    )
    sc.http.session.get = lambda url, **kw: big_resp

    res = sc.download_resource("https://mulms.mutah.edu.jo/mod/resource/view.php?id=99", max_mb=1)
    assert res is None
    assert big_resp.closed is True
    sc.close()


# ===========================================================================
# C6: parse_submission_state() + View/partial completion badges
# ===========================================================================

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Not submitted", False),
        ("No submissions have been made yet", False),
        ("No submission", False),
        ("No attempts have been made", False),
        ("Not graded", False),
        ("Draft", False),
        ("Draft (not submitted)", False),
        ("In progress", False),
        ("Not finished", False),
        ("Submitted for grading", True),
        ("Submitted", True),
        ("Graded", True),
        ("Finished", True),
        ("لم تسلم", False),
        ("لم يتم التسليم", False),
        ("لم يتم تقديم أي شيء", False),
        ("لا توجد محاولة", False),
        ("لم يتم التقييم", False),
        ("غير مقيّم", False),
        ("مسودة", False),
        ("مسودة (غير مسلمة)", False),
        ("تم التسليم", True),
        ("تم تسليمها للتقييم", True),
        ("تم التقييم", True),
        ("منتهية", True),
        ("-", None),
        ("—", None),
        ("", None),
        ("View", None),
    ],
)
def test_c6_parse_submission_state_table(text, expected):
    parse_submission_state = getattr(lms_scraper, "parse_submission_state")
    assert parse_submission_state(text) is expected


def test_c6_index_table_not_submitted_and_draft_are_not_marked_submitted():
    sc = LMSScraper("u", "p")
    sc.authenticated_get = MagicMock(return_value=_HtmlResponse(MOODLE5_OVERVIEW_TABLE_HTML))
    acts = sc._get_activities_from_index(71177, "assign")
    by_id = {a["id"]: a for a in acts}

    assert by_id["assign_257625"]["is_submitted"] is True
    assert by_id["assign_284175"]["is_submitted"] is False
    assert by_id["assign_284176"]["is_submitted"] is False
    assert by_id["assign_284177"]["is_submitted"] is False
    sc.close()


def test_c6_assignment_status_not_submitted_and_submitted_not_graded():
    sc = LMSScraper("u", "p")

    unsubmitted_html = '''<html><body>
      <div data-region="completion-info">
        <span class="badge alert-success"><strong>Done:</strong> View</span>
        <span class="badge alert-secondary"><strong>To do:</strong> Make a submission</span>
      </div>
      <table>
        <tr><th>Submission status</th><td>Not submitted</td></tr>
        <tr><th>Grading status</th><td>Not graded</td></tr>
      </table>
    </body></html>'''
    sc.authenticated_get = MagicMock(return_value=_HtmlResponse(unsubmitted_html))
    st1 = sc.get_assignment_status("https://mulms.mutah.edu.jo/mod/assign/view.php?id=284175")
    assert st1["is_submitted"] is False

    submitted_ungraded_html = '''<html><body>
      <table>
        <tr><th>Submission status</th><td>Submitted for grading</td></tr>
        <tr><th>Grading status</th><td>Not graded</td></tr>
      </table>
    </body></html>'''
    sc.authenticated_get = MagicMock(return_value=_HtmlResponse(submitted_ungraded_html))
    st2 = sc.get_assignment_status("https://mulms.mutah.edu.jo/mod/assign/view.php?id=257625")
    assert st2["is_submitted"] is True
    sc.close()


def test_c6_view_badge_only_and_partial_completion_are_not_completed():
    csoup = BeautifulSoup(MOODLE5_COURSE_COMPLETION_HTML, "lxml")
    completed = LMSScraper._extract_course_page_completions(csoup)

    assert "284175" not in completed  # manual:mark-done
    assert "284180" not in completed  # green "Done: View" only -> NOT submitted
    assert "284181" not in completed  # "Done: View" + "To do: Make a submission" -> NOT submitted
    assert "290627" in completed      # manual:undo Done -> completed

    # Quiz page with green View-only badge must NOT be marked submitted
    quiz_view_only_html = '''<html><body>
      <div data-region="completion-info">
        <span class="badge rounded-pill bg-success">Done: View</span>
      </div>
      <table>
        <tr><th>Status</th><td>In progress</td></tr>
      </table>
    </body></html>'''
    sc = LMSScraper("u", "p")
    sc.authenticated_get = MagicMock(return_value=_HtmlResponse(quiz_view_only_html))
    qst = sc.get_quiz_status("https://mulms.mutah.edu.jo/mod/quiz/view.php?id=290628")
    assert qst["is_submitted"] is False
    sc.close()
