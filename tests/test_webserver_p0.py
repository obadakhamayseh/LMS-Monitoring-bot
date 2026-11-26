"""Prove-It exploit tests for webapp_server.py Phase P0 items: C5, H1, C2."""

from __future__ import annotations

import os
from cryptography.fernet import Fernet

# Must be configured BEFORE importing any project module so tests never touch
# live Supabase DB, real .env secrets, or real Telegram/LMS endpoints.
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from typing import Any, Optional

import pytest

import database as db
from lms_scraper import FetchResult, LMSScraper as RealLMSScraper
import webapp_server

TEST_BOT_TOKEN = "123456:TEST"
TEST_OWNER_ID = 1


def _build_signed_init_data(
    user_id: int = TEST_OWNER_ID,
    auth_date: Optional[Any] = None,
    bot_token: str = TEST_BOT_TOKEN,
    include_auth_date: bool = True,
) -> str:
    """Build a validly HMAC-SHA256-signed Telegram WebApp initData query string."""
    params: dict[str, str] = {
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    if include_auth_date:
        ts = int(time.time()) if auth_date is None else auth_date
        params["auth_date"] = str(ts)

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params["hash"] = calc_hash
    return urllib.parse.urlencode(params)


def _raw_http_request(host: str, port: int, raw_bytes: bytes, timeout: float = 1.5) -> tuple[int, str]:
    """Send raw HTTP bytes over TCP and return (status_code, raw_response_text)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(raw_bytes)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                # Once we have status line and headers + complete body (if Content-Length set), stop waiting
                joined = b"".join(chunks)
                if b"\r\n\r\n" in joined:
                    head, body = joined.split(b"\r\n\r\n", 1)
                    cl = None
                    for line in head.split(b"\r\n")[1:]:
                        if line.lower().startswith(b"content-length:"):
                            cl = int(line.split(b":", 1)[1].strip())
                            break
                    if cl is not None and len(body) >= cl:
                        break
            except socket.timeout:
                break
    raw_resp = b"".join(chunks).decode("utf-8", errors="replace")
    assert raw_resp, "Server timed out without sending a response (likely tried to read oversized body)"
    status_line = raw_resp.split("\r\n", 1)[0]
    status_code = int(status_line.split()[1])
    return status_code, raw_resp


def test_c2_real_lms_scraper_exposes_fetch_course_content_contract():
    """Verify real LMSScraper.fetch_course_content and FetchResult match webapp_server expectations."""
    assert callable(getattr(RealLMSScraper, "fetch_course_content", None))
    sample = FetchResult(content={}, ok=True)
    assert (sample.content, sample.ok, sample.session_expired, sample.waf_blocked, sample.error) == (
        {}, True, False, False, None
    )


@pytest.fixture()
def web_server(tmp_path, monkeypatch):
    """Run WebAppRequestHandler on an ephemeral localhost port with an isolated SQLite DB."""
    db_file = str(tmp_path / "test_webserver.db")
    monkeypatch.setattr(db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    monkeypatch.setattr(webapp_server, "TELEGRAM_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setattr(webapp_server, "OWNER_TELEGRAM_ID", TEST_OWNER_ID)

    # Clear in-memory pairing state between tests
    with webapp_server._pair_lock:
        webapp_server._pair_codes.clear()
        webapp_server._pair_fail_times.clear()

    db.init_db()
    db.save_user(TEST_OWNER_ID, "20240001", "secret_pass", "99", "Test Owner")

    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp_server.WebAppRequestHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _http_req(
    host: str,
    port: int,
    method: str,
    path: str,
    body: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection(host, port, timeout=3.0)
    try:
        req_headers = dict(headers or {})
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        resp_body = resp.read().decode("utf-8", errors="replace")
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, resp_body
    finally:
        conn.close()


# ── C5: Pre-auth Content-Length cap, invalid length, invalid JSON / non-dict ──


def test_c5_oversized_content_length_returns_413_without_reading_or_auth(web_server, monkeypatch):
    host, port = web_server
    auth_calls = 0
    orig_auth = webapp_server.WebAppRequestHandler._authenticate

    def spy_auth(self, *args, **kwargs):
        nonlocal auth_calls
        auth_calls += 1
        return orig_auth(self, *args, **kwargs)

    monkeypatch.setattr(webapp_server.WebAppRequestHandler, "_authenticate", spy_auth)

    # Claim 10 MB body but send ZERO body bytes: fixed server must reply 413 immediately
    raw_req = (
        b"POST /api/toggle-monitor HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Length: 10485760\r\n"
        b"Content-Type: application/json\r\n"
        b"\r\n"
    )
    status, _ = _raw_http_request(host, port, raw_req, timeout=1.5)
    assert status == 413
    assert auth_calls == 0


@pytest.mark.parametrize("bad_len", ["-5", "abc", "12.34"])
def test_c5_invalid_or_negative_content_length_returns_400_before_auth(web_server, monkeypatch, bad_len):
    host, port = web_server
    auth_calls = 0
    orig_auth = webapp_server.WebAppRequestHandler._authenticate

    def spy_auth(self, *args, **kwargs):
        nonlocal auth_calls
        auth_calls += 1
        return orig_auth(self, *args, **kwargs)

    monkeypatch.setattr(webapp_server.WebAppRequestHandler, "_authenticate", spy_auth)

    raw_req = (
        f"POST /api/toggle-monitor HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Content-Length: {bad_len}\r\n"
        f"Content-Type: application/json\r\n"
        f"\r\n"
    ).encode("ascii")
    status, _ = _raw_http_request(host, port, raw_req, timeout=1.5)
    assert status == 400
    assert auth_calls == 0


@pytest.mark.parametrize("raw_body", [b"[1, 2, 3]", b'"just a string"', b"123", b"null", b"{bad json", b"\xff\xfe\xfd"])
def test_c5_non_dict_or_invalid_json_or_utf8_returns_400_before_auth(web_server, monkeypatch, raw_body):
    host, port = web_server
    auth_calls = 0
    orig_auth = webapp_server.WebAppRequestHandler._authenticate

    def spy_auth(self, *args, **kwargs):
        nonlocal auth_calls
        auth_calls += 1
        return orig_auth(self, *args, **kwargs)

    monkeypatch.setattr(webapp_server.WebAppRequestHandler, "_authenticate", spy_auth)

    status, _, body = _http_req(
        host,
        port,
        "POST",
        "/api/toggle-monitor",
        body=raw_body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert auth_calls == 0
    data = json.loads(body)
    assert data.get("success") is False


# ── H1: initData auth_date freshness + header-only X-Telegram-Init-Data ──


def test_h1_validate_init_data_unit_freshness_and_hmac():
    now = int(time.time())
    assert webapp_server._validate_telegram_init_data(
        _build_signed_init_data(auth_date=now), TEST_BOT_TOKEN
    ) == TEST_OWNER_ID
    assert webapp_server._validate_telegram_init_data(
        _build_signed_init_data(auth_date=now - 301), TEST_BOT_TOKEN
    ) is None
    assert webapp_server._validate_telegram_init_data(
        _build_signed_init_data(auth_date=now + 61), TEST_BOT_TOKEN
    ) is None
    assert webapp_server._validate_telegram_init_data(
        _build_signed_init_data(include_auth_date=False), TEST_BOT_TOKEN
    ) is None
    assert webapp_server._validate_telegram_init_data(
        _build_signed_init_data(auth_date="bad"), TEST_BOT_TOKEN
    ) is None
    assert webapp_server._validate_telegram_init_data(
        _build_signed_init_data(auth_date=now, bot_token="wrong:token"), TEST_BOT_TOKEN
    ) is None


def test_h1_stale_auth_date_rejected_401(web_server):
    host, port = web_server
    stale_init = _build_signed_init_data(auth_date=int(time.time()) - 305)
    status, _, _ = _http_req(
        host,
        port,
        "GET",
        "/api/user-data",
        headers={"X-Telegram-Init-Data": stale_init},
    )
    assert status == 401


def test_h1_future_auth_date_rejected_401(web_server):
    host, port = web_server
    future_init = _build_signed_init_data(auth_date=int(time.time()) + 65)
    status, _, _ = _http_req(
        host,
        port,
        "GET",
        "/api/user-data",
        headers={"X-Telegram-Init-Data": future_init},
    )
    assert status == 401


def test_h1_missing_or_invalid_auth_date_rejected_401(web_server):
    host, port = web_server
    no_date = _build_signed_init_data(include_auth_date=False)
    bad_date = _build_signed_init_data(auth_date="not-an-int")
    for token in (no_date, bad_date):
        status, _, _ = _http_req(
            host,
            port,
            "GET",
            "/api/user-data",
            headers={"X-Telegram-Init-Data": token},
        )
        assert status == 401


def test_h1_valid_fresh_init_data_in_header_accepted_on_get_and_post(web_server):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))

    status_get, headers_get, body_get = _http_req(
        host,
        port,
        "GET",
        "/api/user-data",
        headers={"X-Telegram-Init-Data": fresh_init},
    )
    assert status_get == 200
    assert json.loads(body_get)["success"] is True
    assert "x-telegram-init-data" in headers_get.get("access-control-allow-headers", "").lower()

    status_post, _, body_post = _http_req(
        host,
        port,
        "POST",
        "/api/toggle-monitor",
        body=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Init-Data": fresh_init,
        },
    )
    assert status_post == 200
    assert json.loads(body_post)["success"] is True

    # OPTIONS preflight must advertise X-Telegram-Init-Data
    status_opt, headers_opt, _ = _http_req(host, port, "OPTIONS", "/api/user-data")
    assert status_opt == 200
    assert "x-telegram-init-data" in headers_opt.get("access-control-allow-headers", "").lower()


def test_h1_init_data_in_query_string_and_json_body_ignored_401(web_server):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))

    qs = urllib.parse.urlencode({"initData": fresh_init})
    status_get, _, _ = _http_req(host, port, "GET", f"/api/user-data?{qs}")
    assert status_get == 401

    status_post, _, _ = _http_req(
        host,
        port,
        "POST",
        "/api/toggle-monitor",
        body=json.dumps({"initData": fresh_init}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status_post == 401


def test_h1_pairing_and_bearer_auth_still_work(web_server):
    host, port = web_server
    code = webapp_server.create_pairing_code(TEST_OWNER_ID)

    status_pair, _, body_pair = _http_req(
        host,
        port,
        "POST",
        "/api/pair",
        body=json.dumps({"code": code, "device": "pytest"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status_pair == 200
    token = json.loads(body_pair)["token"]

    status_get, _, body_get = _http_req(
        host,
        port,
        "GET",
        "/api/user-data",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_get == 200
    assert json.loads(body_get)["success"] is True


# ── C2: Sequential course scanning with FetchResult (no ThreadPoolExecutor) ──


def test_c2_sync_courses_sequential_and_uses_fetch_course_content(web_server, monkeypatch):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))

    state = {"in_flight": 0, "max_in_flight": 0, "fetched": [], "legacy_called": 0}
    lock = threading.Lock()

    class FakeScraper:
        def __init__(self, username: str, password: str):
            self.waf_blocked = False
            self.session_expired = False
            self.error_message = ""

        def login(self) -> bool:
            return True

        def get_courses(self) -> list[dict[str, Any]]:
            return [
                {"id": 101, "name": "Course 1", "url": "https://mulms.mutah.edu.jo/course/view.php?id=101"},
                {"id": 102, "name": "Course 2", "url": "https://mulms.mutah.edu.jo/course/view.php?id=102"},
                {"id": 103, "name": "Course 3", "url": "https://mulms.mutah.edu.jo/course/view.php?id=103"},
            ]

        def get_course_content(self, cid: int):
            state["legacy_called"] += 1
            raise AssertionError("sync-courses must call fetch_course_content, not get_course_content")

        def fetch_course_content(self, cid: int) -> FetchResult:
            with lock:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
                state["fetched"].append(cid)
            time.sleep(0.04)
            with lock:
                state["in_flight"] -= 1
            return FetchResult(
                content={
                    "assignments": [
                        {"id": f"a_{cid}", "name": f"HW {cid}", "url": f"https://mulms.mutah.edu.jo/mod/assign/view.php?id={cid}"}
                    ],
                    "quizzes": [],
                    "resources": [],
                },
                ok=True,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(webapp_server, "LMSScraper", FakeScraper)

    status, _, body = _http_req(
        host,
        port,
        "POST",
        "/api/sync-courses",
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": fresh_init},
    )
    assert status == 200
    data = json.loads(body)
    assert data["success"] is True
    assert data["synced_count"] == 3
    assert state["legacy_called"] == 0
    assert state["max_in_flight"] == 1
    assert state["fetched"] == [101, 102, 103]


def test_c2_force_check_sequential_and_uses_fetch_course_content(web_server, monkeypatch):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))

    for cid in (201, 202, 203):
        db.add_course(TEST_OWNER_ID, cid, f"Course {cid}", f"https://mulms.mutah.edu.jo/course/view.php?id={cid}")

    state = {"in_flight": 0, "max_in_flight": 0, "fetched": [], "legacy_called": 0}
    lock = threading.Lock()

    class FakeScraper:
        def __init__(self, username: str, password: str):
            self.waf_blocked = False
            self.session_expired = False
            self.error_message = ""

        def login(self) -> bool:
            return True

        def get_course_content(self, cid: int):
            state["legacy_called"] += 1
            raise AssertionError("force-check must call fetch_course_content, not get_course_content")

        def fetch_course_content(self, cid: int) -> FetchResult:
            with lock:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
                state["fetched"].append(cid)
            time.sleep(0.04)
            with lock:
                state["in_flight"] -= 1
            return FetchResult(
                content={
                    "assignments": [
                        {"id": f"fc_{cid}", "name": f"Assign {cid}", "url": f"https://mulms.mutah.edu.jo/mod/assign/view.php?id={cid}"}
                    ],
                    "quizzes": [],
                    "resources": [],
                },
                ok=True,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(webapp_server, "LMSScraper", FakeScraper)

    status, _, body = _http_req(
        host,
        port,
        "POST",
        "/api/force-check",
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": fresh_init},
    )
    assert status == 200
    data = json.loads(body)
    assert data["success"] is True
    assert data["new_items"] == 3
    assert state["legacy_called"] == 0
    assert state["max_in_flight"] == 1
    assert len(state["fetched"]) == 3


@pytest.mark.parametrize("endpoint", ["/api/sync-courses", "/api/force-check"])
def test_c2_waf_blocked_stops_scan_and_returns_429(web_server, monkeypatch, endpoint):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))

    for cid in (301, 302):
        db.add_course(TEST_OWNER_ID, cid, f"Course {cid}", f"https://mulms.mutah.edu.jo/course/view.php?id={cid}")

    fetched: list[int] = []

    class WafScraper:
        def __init__(self, username: str, password: str):
            self.waf_blocked = False
            self.session_expired = False
            self.error_message = ""

        def login(self) -> bool:
            return True

        def get_courses(self) -> list[dict[str, Any]]:
            return [
                {"id": 301, "name": "Course 301", "url": "https://mulms.mutah.edu.jo/course/view.php?id=301"},
                {"id": 302, "name": "Course 302", "url": "https://mulms.mutah.edu.jo/course/view.php?id=302"},
            ]

        def fetch_course_content(self, cid: int) -> FetchResult:
            fetched.append(cid)
            return FetchResult(content={}, ok=False, waf_blocked=True, error="WAF CAPTCHA")

        def close(self) -> None:
            pass

    monkeypatch.setattr(webapp_server, "LMSScraper", WafScraper)

    status, _, body = _http_req(
        host,
        port,
        "POST",
        endpoint,
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": fresh_init},
    )
    assert status == 429
    data = json.loads(body)
    assert data["success"] is False
    assert data.get("waf_blocked") is True
    assert len(fetched) == 1


@pytest.mark.parametrize("endpoint", ["/api/sync-courses", "/api/force-check"])
def test_c2_session_expired_stops_scan_and_reports_session_expired(web_server, monkeypatch, endpoint):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))

    for cid in (401, 402):
        db.add_course(TEST_OWNER_ID, cid, f"Course {cid}", f"https://mulms.mutah.edu.jo/course/view.php?id={cid}")

    fetched: list[int] = []

    class ExpiredScraper:
        def __init__(self, username: str, password: str):
            self.waf_blocked = False
            self.session_expired = False
            self.error_message = ""

        def login(self) -> bool:
            return True

        def get_courses(self) -> list[dict[str, Any]]:
            return [
                {"id": 401, "name": "Course 401", "url": "https://mulms.mutah.edu.jo/course/view.php?id=401"},
                {"id": 402, "name": "Course 402", "url": "https://mulms.mutah.edu.jo/course/view.php?id=402"},
            ]

        def fetch_course_content(self, cid: int) -> FetchResult:
            fetched.append(cid)
            return FetchResult(content={}, ok=False, session_expired=True, error="Session expired")

        def close(self) -> None:
            pass

    monkeypatch.setattr(webapp_server, "LMSScraper", ExpiredScraper)

    status, _, body = _http_req(
        host,
        port,
        "POST",
        endpoint,
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": fresh_init},
    )
    assert status in (400, 401, 502)
    data = json.loads(body)
    assert data["success"] is False
    assert data.get("session_expired") is True
    assert len(fetched) == 1


# ── C10 (webapp_server.py /api/login): never mutate password ──


def test_c10_login_preserves_password_verbatim_without_strip_or_translate(web_server, monkeypatch):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))
    captured: dict[str, str] = {}

    class CaptureLoginScraper:
        def __init__(self, username: str, password: str):
            captured["username"] = username
            captured["password"] = password
            self.user_id = "77"
            self.full_name = "Test Owner"
            self.error_message = ""

        def login(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(webapp_server, "LMSScraper", CaptureLoginScraper)

    exact_password = "  p@ss١٢٣\t "
    status, _, body = _http_req(
        host,
        port,
        "POST",
        "/api/login",
        body=json.dumps({"username": " ٢٠٢٤٠٠٠١ ", "password": exact_password}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": fresh_init},
    )
    assert status == 200
    assert json.loads(body)["success"] is True
    assert captured["username"] == "20240001"
    assert captured["password"] == exact_password
    saved = db.get_user(TEST_OWNER_ID)
    assert saved is not None
    assert saved["lms_password"] == exact_password


@pytest.mark.parametrize("bad_password", [None, "", 12345, ["secret"], {"p": "1"}])
def test_c10_login_rejects_non_string_or_empty_password(web_server, monkeypatch, bad_password):
    host, port = web_server
    fresh_init = _build_signed_init_data(auth_date=int(time.time()))
    called = 0

    class ShouldNotRunScraper:
        def __init__(self, username: str, password: str):
            nonlocal called
            called += 1

    monkeypatch.setattr(webapp_server, "LMSScraper", ShouldNotRunScraper)

    status, _, body = _http_req(
        host,
        port,
        "POST",
        "/api/login",
        body=json.dumps({"username": "20240001", "password": bad_password}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": fresh_init},
    )
    assert status == 400
    assert json.loads(body)["success"] is False
    assert called == 0

