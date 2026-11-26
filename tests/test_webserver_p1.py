"""Prove-It tests for webapp_server.py Phase P1 items: H5, H4, H2, C9, H15, H16 (FastAPI)."""

from __future__ import annotations

import os
from cryptography.fernet import Fernet

# Configure isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

import database as db
from lms_scraper import FetchResult
import webapp_server

TEST_BOT_TOKEN = "123456:TEST"
TEST_OWNER_ID = 1


def _build_signed_init_data(
    user_id: int = TEST_OWNER_ID,
    auth_date: Optional[int] = None,
    bot_token: str = TEST_BOT_TOKEN,
) -> str:
    ts = int(time.time()) if auth_date is None else auth_date
    params = {
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
        "auth_date": str(ts),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


def _http_req(
    client: TestClient,
    method: str,
    path: str,
    body: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, str], list[str], str]:
    """Return (status, headers_lower_dict, set_cookie_list, body_text)."""
    client.cookies.clear()
    resp = client.request(method, path, content=body, headers=dict(headers or {}))
    headers_lower = {k.lower(): v for k, v in resp.headers.items()}
    set_cookies = resp.headers.get_list("set-cookie")
    return resp.status_code, headers_lower, set_cookies, resp.text


def _parse_set_cookies(set_cookies: list[str]) -> dict[str, dict[str, Any]]:
    """Parse Set-Cookie headers into {name: {value, attrs, raw}}."""
    parsed: dict[str, dict[str, Any]] = {}
    for raw in set_cookies:
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        if not parts or "=" not in parts[0]:
            continue
        name, val = parts[0].split("=", 1)
        attrs: dict[str, Any] = {}
        for attr in parts[1:]:
            if "=" in attr:
                ak, av = attr.split("=", 1)
                attrs[ak.strip().lower()] = av.strip()
            else:
                attrs[attr.strip().lower()] = True
        parsed[name.strip()] = {"value": val.strip(), "attrs": attrs, "raw": raw}
    return parsed


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    """Provide FastAPI TestClient backed by an isolated SQLite DB."""
    db_file = str(tmp_path / "test_webserver_p1.db")
    monkeypatch.setattr(db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    monkeypatch.setattr(webapp_server, "TELEGRAM_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setattr(webapp_server, "OWNER_TELEGRAM_ID", TEST_OWNER_ID)
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setattr(webapp_server, "_ENGINE", None)
    monkeypatch.setattr(webapp_server, "_REMINDER_ENGINE", None)

    webapp_server.reset_rate_limits()

    db.init_db()
    db.save_user(TEST_OWNER_ID, "20240001", "secret_pass", "99", "Test Owner")

    with TestClient(webapp_server.app) as client:
        yield client, db_file


def _pair_and_get_cookies(
    client: TestClient,
    extra_headers: Optional[dict[str, str]] = None,
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    code = webapp_server.create_pairing_code(TEST_OWNER_ID)
    hdrs = {"Content-Type": "application/json"}
    if extra_headers:
        hdrs.update(extra_headers)
    status, _, set_cookies, body = _http_req(
        client,
        "POST",
        "/api/pair",
        body=json.dumps({"code": code, "device": "pytest"}).encode("utf-8"),
        headers=hdrs,
    )
    assert status == 200, f"Pairing failed: {status} {body}"
    cookies = _parse_set_cookies(set_cookies)
    assert "mutah_session" in cookies, f"Missing mutah_session cookie: {set_cookies}"
    assert "mutah_csrf" in cookies, f"Missing mutah_csrf cookie: {set_cookies}"
    return cookies["mutah_session"]["value"], cookies["mutah_csrf"]["value"], cookies


# ── H5: item_url, /api/mark-submitted (seen_content + reminders + toggleDone(0)), merge_user_meta ──


def test_h5_user_data_uses_item_url_and_filters_attendance_by_item_url(web_client):
    client, _ = web_client
    init_data = _build_signed_init_data()
    db.add_course(TEST_OWNER_ID, 101, "Algorithms", "https://mulms.mutah.edu.jo/course/view.php?id=101")
    db.upsert_grade(
        TEST_OWNER_ID, 101, "g_midterm", "Midterm Exam", "28", "30", "93%", "Great",
        "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=555",
    )
    # Attendance grade whose item_name is custom (not 'attendance') so ONLY item_url filter catches it
    db.upsert_grade(
        TEST_OWNER_ID, 101, "g_att", "Class Participation Roll", "5", "5", "100%", "",
        "https://mulms.mutah.edu.jo/mod/attendance/view.php?id=777",
    )

    status, _, _, body = _http_req(
        client, "GET", "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert status == 200
    data = json.loads(body)
    grades = data["grades"]
    assert len(grades) == 1, f"Attendance grade was not filtered by item_url: {grades}"
    assert grades[0]["item_id"] == "g_midterm"
    assert grades[0]["item_url"] == "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=555"


def test_h5_mark_submitted_updates_seen_content_and_reminders_including_toggle_done_zero(web_client):
    client, _ = web_client
    init_data = _build_signed_init_data()
    db.add_course(TEST_OWNER_ID, 101, "OS", "https://mulms.mutah.edu.jo/course/view.php?id=101")
    # Item present ONLY in seen_content (NOT yet in reminders)
    db.mark_seen(
        TEST_OWNER_ID, 101, "assign_501", "assign", "Lab 1",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=501", "", is_submitted=0,
    )

    # Mark submitted=1 via toggleDone payload (not in reminders yet)
    status1, _, _, body1 = _http_req(
        client, "POST", "/api/mark-submitted",
        body=json.dumps({
            "content_id": "assign_501",
            "item_key": "assign_501",
            "course_id": 101,
            "title": "Lab 1",
            "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=501",
            "item_type": "assign",
            "is_submitted": 1,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert status1 == 200
    assert json.loads(body1)["is_submitted"] is True

    sc1 = db.get_seen_content(TEST_OWNER_ID, "assign_501", "assign")
    assert sc1["is_submitted"] == 1
    with db.DBContext() as ctx:
        rem1 = ctx.fetchone(
            "SELECT * FROM reminders WHERE telegram_id=? AND content_id=?",
            (TEST_OWNER_ID, "assign_501"),
        )
    assert rem1 is not None and rem1["is_submitted"] == 1

    # Now toggleDone(0) to unmark it — BOTH seen_content and reminders must flip back to 0!
    status0, _, _, body0 = _http_req(
        client, "POST", "/api/mark-submitted",
        body=json.dumps({
            "content_id": "assign_501",
            "item_key": "assign_501",
            "course_id": 101,
            "title": "Lab 1",
            "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=501",
            "item_type": "assign",
            "is_submitted": 0,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert status0 == 200
    assert json.loads(body0)["is_submitted"] is False

    sc0 = db.get_seen_content(TEST_OWNER_ID, "assign_501", "assign")
    assert sc0["is_submitted"] == 0, f"seen_content was not unmarked on toggleDone(0): {sc0}"
    with db.DBContext() as ctx:
        rem0 = ctx.fetchone(
            "SELECT * FROM reminders WHERE telegram_id=? AND content_id=?",
            (TEST_OWNER_ID, "assign_501"),
        )
    assert rem0 is not None and rem0["is_submitted"] == 0, f"reminders was not unmarked on toggleDone(0): {rem0}"


def test_h5_user_meta_uses_atomic_merge_user_meta(web_client, monkeypatch):
    client, _ = web_client
    init_data = _build_signed_init_data()
    calls: list[tuple[int, dict]] = []

    assert hasattr(db, "merge_user_meta"), "database.merge_user_meta not implemented yet"
    orig_merge = db.merge_user_meta

    def spy_merge(tid: int, patch: dict) -> dict:
        calls.append((tid, patch))
        return orig_merge(tid, patch)

    monkeypatch.setattr(db, "merge_user_meta", spy_merge)

    status, _, _, body = _http_req(
        client, "POST", "/api/user-meta",
        body=json.dumps({"meta": {"absence_limit": 15}}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert status == 200
    assert len(calls) == 1
    assert calls[0] == (TEST_OWNER_ID, {"absence_limit": 15})
    assert json.loads(body)["user_meta"]["absence_limit"] == 15


# ── H4: Security headers, strict CORS, MonitorEngine public API, request timeouts ──


def test_h4_security_headers_on_webapp_and_api(web_client):
    client, _ = web_client
    init_data = _build_signed_init_data()

    # 1. HTML /webapp over HTTPS proxy header
    st_html, hdr_html, _, _ = _http_req(
        client, "GET", "/webapp",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert st_html == 200
    csp = hdr_html.get("content-security-policy", "")
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors" in csp
    assert "https://telegram.org" in csp
    assert hdr_html.get("x-content-type-options") == "nosniff"
    assert hdr_html.get("strict-transport-security") == "max-age=31536000; includeSubDomains"

    # 2. Plain HTTP should NOT emit HSTS
    st_plain, hdr_plain, _, _ = _http_req(client, "GET", "/webapp")
    assert st_plain == 200
    assert "strict-transport-security" not in hdr_plain

    # 3. API responses must include Cache-Control: no-store and X-Content-Type-Options: nosniff
    st_api, hdr_api, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "X-Forwarded-Proto": "https"},
    )
    assert st_api == 200
    assert "no-store" in hdr_api.get("cache-control", "").lower()
    assert hdr_api.get("x-content-type-options") == "nosniff"
    assert hdr_api.get("strict-transport-security") == "max-age=31536000; includeSubDomains"

    # 4. Unauthenticated 401 API response also has Cache-Control: no-store
    st_401, hdr_401, _, _ = _http_req(client, "GET", "/api/user-data")
    assert st_401 == 401
    assert "no-store" in hdr_401.get("cache-control", "").lower()
    assert hdr_401.get("x-content-type-options") == "nosniff"


def test_h4_cors_rejects_evil_origin_and_echoes_configured_webapp_url(web_client, monkeypatch):
    client, _ = web_client
    init_data = _build_signed_init_data()
    monkeypatch.setenv("WEBAPP_URL", "https://mutah-app.onrender.com/webapp")

    # Evil origin on GET
    st_evil, hdr_evil, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "Origin": "https://evil.example"},
    )
    assert st_evil == 200
    assert "access-control-allow-origin" not in hdr_evil
    assert "access-control-allow-credentials" not in hdr_evil
    assert "origin" in hdr_evil.get("vary", "").lower()

    # Evil origin on OPTIONS preflight
    st_opt_evil, hdr_opt_evil, _, _ = _http_req(
        client, "OPTIONS", "/api/user-data",
        headers={"Origin": "https://evil.example"},
    )
    assert st_opt_evil == 403
    assert "access-control-allow-origin" not in hdr_opt_evil
    assert "access-control-allow-credentials" not in hdr_opt_evil

    # Allowed origin on GET
    st_ok, hdr_ok, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "Origin": "https://mutah-app.onrender.com"},
    )
    assert st_ok == 200
    assert hdr_ok.get("access-control-allow-origin") == "https://mutah-app.onrender.com"
    assert hdr_ok.get("access-control-allow-credentials") == "true"
    assert "origin" in hdr_ok.get("vary", "").lower()

    # Allowed origin on OPTIONS
    st_opt_ok, hdr_opt_ok, _, _ = _http_req(
        client, "OPTIONS", "/api/user-data",
        headers={"Origin": "https://mutah-app.onrender.com"},
    )
    assert st_opt_ok in (200, 204)
    assert hdr_opt_ok.get("access-control-allow-origin") == "https://mutah-app.onrender.com"
    assert hdr_opt_ok.get("access-control-allow-credentials") == "true"
    assert "x-csrf-token" in hdr_opt_ok.get("access-control-allow-headers", "").lower()


def test_h4_logout_uses_public_monitor_engine_api_not_private_state(web_client, monkeypatch):
    client, _ = web_client
    init_data = _build_signed_init_data()

    class StrictEngine:
        def __init__(self):
            self.invalidated: list[tuple[int, dict]] = []

        def invalidate_user_session(self, tid: int, **kwargs):
            self.invalidated.append((tid, kwargs))

        def __getattr__(self, name: str):
            if name.startswith("_"):
                raise AssertionError(f"webapp_server poked private MonitorEngine attribute: {name}")
            raise AttributeError(name)

    engine = StrictEngine()
    monkeypatch.setattr(webapp_server, "_ENGINE", engine)

    status, _, _, body = _http_req(
        client, "POST", "/api/logout",
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert status == 200
    assert json.loads(body)["success"] is True
    assert len(engine.invalidated) == 1
    assert engine.invalidated[0][0] == TEST_OWNER_ID


def test_h4_uvicorn_config_sets_request_timeout():
    timeout_val = getattr(webapp_server, "REQUEST_TIMEOUT_SECONDS", None)
    assert timeout_val is not None and 0 < timeout_val <= 60
    cfg = webapp_server.build_uvicorn_config(host="127.0.0.1", port=0)
    assert cfg.timeout_keep_alive == timeout_val


# ── H2 + C9: Per-IP rate limiting, DB-backed pair codes, session TTL, /health ──


def test_h2_pair_rate_limit_is_per_ip_and_does_not_wipe_codes_for_other_ips(web_client):
    client, _ = web_client
    valid_code = webapp_server.create_pairing_code(TEST_OWNER_ID)

    # Brute-force from IP A until 429
    got_429 = False
    for i in range(webapp_server._PAIR_MAX_FAILS + 3):
        st, _, _, _ = _http_req(
            client, "POST", "/api/pair",
            body=json.dumps({"code": f"0000000{i % 10}"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Forwarded-For": "198.51.100.10"},
        )
        if st == 429:
            got_429 = True
            break
    assert got_429, "Expected 429 Too Many Requests after repeated failed /api/pair attempts from same IP"

    # IP A is STILL rate-limited even with the valid code
    st_blocked, _, _, _ = _http_req(
        client, "POST", "/api/pair",
        body=json.dumps({"code": valid_code}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Forwarded-For": "198.51.100.10"},
    )
    assert st_blocked == 429

    # IP B can STILL pair with the valid code (global wipe must NOT happen!)
    st_other, _, set_cookies, body_other = _http_req(
        client, "POST", "/api/pair",
        body=json.dumps({"code": valid_code}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.55"},
    )
    assert st_other == 200, f"Valid code was wiped or blocked for another IP: {st_other} {body_other}"
    assert "mutah_session" in _parse_set_cookies(set_cookies)


def test_c9_pair_code_persisted_in_db_survives_simulated_restart(web_client):
    code = webapp_server.create_pairing_code(TEST_OWNER_ID)

    # Ensure there is no in-memory _pair_codes cache relied upon
    assert not hasattr(webapp_server, "_pair_codes"), "_pair_codes in-memory dict should be replaced by DB table"

    # Spin up a fresh FastAPI app instance against the same DB (simulating Render restart)
    fresh_app = webapp_server.create_app()
    with TestClient(fresh_app) as client2:
        st, _, set_cookies, body = _http_req(
            client2, "POST", "/api/pair",
            body=json.dumps({"code": code}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert st == 200, f"Pair code did not survive restart: {st} {body}"
        assert "mutah_session" in _parse_set_cookies(set_cookies)

        # Single-use: second attempt fails
        st_reuse, _, _, _ = _http_req(
            client2, "POST", "/api/pair",
            body=json.dumps({"code": code}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert st_reuse == 400


def test_c9_health_endpoint_returns_200_and_503_on_db_failure(web_client, monkeypatch):
    client, _ = web_client

    st_ok, hdr_ok, _, body_ok = _http_req(client, "GET", "/health")
    assert st_ok == 200
    data_ok = json.loads(body_ok)
    assert data_ok["status"] == "ok"
    assert data_ok["db"] == "ok"
    assert "no-store" in hdr_ok.get("cache-control", "").lower()

    # Simulate DB failure
    monkeypatch.setattr(db, "health_check", lambda: False)
    st_fail, _, _, body_fail = _http_req(client, "GET", "/health")
    assert st_fail == 503
    data_fail = json.loads(body_fail)
    assert data_fail["status"] != "ok"


def test_h2_expired_app_session_cookie_returns_401(web_client):
    client, _ = web_client
    sess_tok, _, _ = _pair_and_get_cookies(client)

    # Sanity check: valid session works
    st_ok, _, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"Cookie": f"mutah_session={sess_tok}"},
    )
    assert st_ok == 200

    # Expire the session in DB
    with db.DBContext() as ctx:
        ctx.execute(
            "UPDATE app_sessions SET expires_at=? WHERE telegram_id=?",
            (int(time.time()) - 10, TEST_OWNER_ID),
        )

    st_exp, _, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"Cookie": f"mutah_session={sess_tok}"},
    )
    assert st_exp == 401


# ── H15: Eliminate N+1 queries in /api/user-data and /api/force-check ──


def test_h15_user_data_eliminates_n_plus_1_queries(web_client, monkeypatch):
    client, _ = web_client
    init_data = _build_signed_init_data()

    def _count_queries_for_course_count(n_courses: int) -> int:
        db.clear_all_user_courses(TEST_OWNER_ID)
        for i in range(1, n_courses + 1):
            db.add_course(
                TEST_OWNER_ID, 1000 + i, f"Course {i}", f"https://mulms.mutah.edu.jo/course/view.php?id={1000 + i}"
            )
            db.mark_seen(
                TEST_OWNER_ID, 1000 + i, f"assign_{i}", "assign", f"HW {i}",
                "https://mulms.mutah.edu.jo/mod/assign/view.php?id=1",
            )

        sql_count = 0
        orig_execute = db.DBContext.execute

        def counting_execute(self, sql, params=()):
            nonlocal sql_count
            sql_count += 1
            return orig_execute(self, sql, params)

        monkeypatch.setattr(db.DBContext, "execute", counting_execute)
        try:
            st, _, _, body = _http_req(
                client, "GET", "/api/user-data",
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert st == 200
            data = json.loads(body)
            assert len(data["courses"]) == n_courses
            return sql_count
        finally:
            monkeypatch.setattr(db.DBContext, "execute", orig_execute)

    q_small = _count_queries_for_course_count(2)
    q_large = _count_queries_for_course_count(15)
    assert q_large == q_small, f"N+1 detected! 2 courses ran {q_small} queries vs 15 courses ran {q_large} queries"
    assert q_large <= 12, f"Too many queries in /api/user-data: {q_large}"


def test_h15_force_check_eliminates_is_seen_n_plus_1(web_client, monkeypatch):
    client, _ = web_client
    init_data = _build_signed_init_data()

    for cid in (2001, 2002, 2003):
        db.add_course(TEST_OWNER_ID, cid, f"Course {cid}", f"https://mulms.mutah.edu.jo/course/view.php?id={cid}")
    db.mark_seen(
        TEST_OWNER_ID, 2001, "a_2001_0", "assign", "Old HW",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=1",
    )

    is_seen_calls = 0

    def spy_is_seen(*args, **kwargs):
        nonlocal is_seen_calls
        is_seen_calls += 1
        raise AssertionError("force-check must NOT call db.is_seen in a loop (N+1)!")

    monkeypatch.setattr(db, "is_seen", spy_is_seen)

    class MultiItemScraper:
        def __init__(self, username, password):
            self.waf_blocked = False
            self.session_expired = False
            self.error_message = ""

        def login(self):
            return True

        def fetch_course_content(self, cid):
            return FetchResult(
                content={
                    "assignments": [
                        {
                            "id": f"a_{cid}_0",
                            "name": "HW 0",
                            "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=1",
                        },
                        {
                            "id": f"a_{cid}_1",
                            "name": "HW 1",
                            "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=2",
                        },
                    ],
                    "quizzes": [],
                    "resources": [],
                },
                ok=True,
            )

        def close(self):
            pass

    monkeypatch.setattr(webapp_server, "LMSScraper", MultiItemScraper)

    st, _, _, body = _http_req(
        client, "POST", "/api/force-check",
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st == 200
    data = json.loads(body)
    assert data["success"] is True
    assert data["new_items"] == 5  # 6 total - 1 already seen
    assert is_seen_calls == 0


# ── H16: HttpOnly SameSite=Strict cookie + CSRF protection + check_interval bounds ──


def test_h16_pair_sets_httponly_samesite_strict_and_secure_cookie_and_omits_json_token(web_client):
    client, _ = web_client
    code = webapp_server.create_pairing_code(TEST_OWNER_ID)

    st, _, set_cookies, body = _http_req(
        client, "POST", "/api/pair",
        body=json.dumps({"code": code, "device": "iphone"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"},
    )
    assert st == 200
    data = json.loads(body)
    assert data["success"] is True
    assert "token" not in data, "Raw session token must NOT be exposed in JSON response body"

    cookies = _parse_set_cookies(set_cookies)
    sess = cookies["mutah_session"]
    csrf = cookies["mutah_csrf"]

    assert sess["attrs"].get("httponly") is True
    assert str(sess["attrs"].get("samesite", "")).lower() == "strict"
    assert sess["attrs"].get("path") == "/"
    assert sess["attrs"].get("secure") is True

    assert "httponly" not in csrf["attrs"]  # Must be JS-readable for X-CSRF-Token
    assert str(csrf["attrs"].get("samesite", "")).lower() == "strict"
    assert csrf["attrs"].get("path") == "/"
    assert csrf["attrs"].get("secure") is True


def test_h16_csrf_required_for_cookie_post_and_not_for_telegram_init_data(web_client):
    client, _ = web_client
    sess_tok, csrf_tok, _ = _pair_and_get_cookies(client)
    cookie_hdr = f"mutah_session={sess_tok}; mutah_csrf={csrf_tok}"

    # 1. Cookie POST WITHOUT X-CSRF-Token -> 403
    st_no_csrf, _, _, _ = _http_req(
        client, "POST", "/api/toggle-monitor",
        body=b"{}",
        headers={"Content-Type": "application/json", "Cookie": cookie_hdr},
    )
    assert st_no_csrf == 403

    # 2. Cookie POST WITH wrong X-CSRF-Token -> 403
    st_bad_csrf, _, _, _ = _http_req(
        client, "POST", "/api/toggle-monitor",
        body=b"{}",
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie_hdr,
            "X-CSRF-Token": "wrong-csrf-token",
        },
    )
    assert st_bad_csrf == 403

    # 3. Cookie POST WITH matching X-CSRF-Token -> 200
    st_good_csrf, _, _, body_ok = _http_req(
        client, "POST", "/api/toggle-monitor",
        body=b"{}",
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie_hdr,
            "X-CSRF-Token": csrf_tok,
        },
    )
    assert st_good_csrf == 200
    assert json.loads(body_ok)["success"] is True

    # 4. Telegram X-Telegram-Init-Data POST WITHOUT CSRF -> 200
    init_data = _build_signed_init_data()
    st_tg, _, _, body_tg = _http_req(
        client, "POST", "/api/toggle-monitor",
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st_tg == 200
    assert json.loads(body_tg)["success"] is True


@pytest.mark.parametrize("endpoint", ["/api/logout", "/api/unlink-device", "/api/app-logout"])
def test_h16_cookies_cleared_and_session_revoked_on_logout_and_unlink(web_client, endpoint):
    client, _ = web_client
    sess_tok, csrf_tok, _ = _pair_and_get_cookies(client)
    cookie_hdr = f"mutah_session={sess_tok}; mutah_csrf={csrf_tok}"

    st, _, set_cookies, body = _http_req(
        client, "POST", endpoint,
        body=b"{}",
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie_hdr,
            "X-CSRF-Token": csrf_tok,
        },
    )
    assert st == 200, f"{endpoint} failed: {st} {body}"
    cleared = _parse_set_cookies(set_cookies)
    assert "mutah_session" in cleared and cleared["mutah_session"]["attrs"].get("max-age") == "0"
    assert "mutah_csrf" in cleared and cleared["mutah_csrf"]["attrs"].get("max-age") == "0"

    # Reusing the revoked session cookie must now return 401
    st_after, _, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"Cookie": f"mutah_session={sess_tok}"},
    )
    assert st_after == 401


@pytest.mark.parametrize("bad_interval", [0, 1, 4, -5, 1441, 9999, "abc", 5.5, True, False, None])
def test_h16_check_interval_rejects_out_of_bounds_or_invalid(web_client, bad_interval):
    client, _ = web_client
    init_data = _build_signed_init_data()
    st, _, _, body = _http_req(
        client, "POST", "/api/update-settings",
        body=json.dumps({"check_interval": bad_interval}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st == 400, f"Expected 400 for check_interval={bad_interval!r}, got {st} {body}"
    assert json.loads(body)["success"] is False


@pytest.mark.parametrize("good_interval", [5, 15, 60, 1440])
def test_h16_check_interval_accepts_valid_bounds(web_client, good_interval):
    client, _ = web_client
    init_data = _build_signed_init_data()
    st, _, _, body = _http_req(
        client, "POST", "/api/update-settings",
        body=json.dumps({"check_interval": good_interval}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st == 200
    assert json.loads(body)["success"] is True
    assert db.get_user(TEST_OWNER_ID)["check_interval"] == good_interval


def test_h16_bearer_token_header_is_rejected_401_and_https_webapp_url_sets_secure_cookie(web_client, monkeypatch):
    client, _ = web_client
    monkeypatch.setenv("WEBAPP_URL", "https://mutah-app.onrender.com/webapp")
    sess_tok, _, cookies = _pair_and_get_cookies(client)
    # WEBAPP_URL=https:// forces Secure attribute even without X-Forwarded-Proto
    assert cookies["mutah_session"]["attrs"].get("secure") is True
    assert cookies["mutah_csrf"]["attrs"].get("secure") is True

    # Old Authorization: Bearer flow must NOT authenticate (HttpOnly cookie replaces localStorage bearer)
    st_bearer, _, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"Authorization": f"Bearer {sess_tok}"},
    )
    assert st_bearer == 401


def test_h4_cors_dev_localhost_allowed_when_webapp_url_unset_and_evil_rejected(web_client):
    client, _ = web_client
    init_data = _build_signed_init_data()

    st_local, hdr_local, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "Origin": "http://127.0.0.1:8080"},
    )
    assert st_local == 200
    assert hdr_local.get("access-control-allow-origin") == "http://127.0.0.1:8080"
    assert hdr_local.get("access-control-allow-credentials") == "true"

    st_evil, hdr_evil, _, _ = _http_req(
        client, "GET", "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "Origin": "https://attacker.evil"},
    )
    assert st_evil == 200
    assert "access-control-allow-origin" not in hdr_evil
    assert "access-control-allow-credentials" not in hdr_evil


def test_h4_login_uses_public_monitor_engine_api_not_private_state(web_client, monkeypatch):
    client, _ = web_client
    init_data = _build_signed_init_data()

    class StrictEngine:
        def __init__(self):
            self.calls: list[tuple[int, dict]] = []

        def invalidate_user_session(self, tid: int, **kwargs):
            self.calls.append((tid, kwargs))

        def __getattr__(self, name: str):
            if name.startswith("_"):
                raise AssertionError(f"webapp_server poked private MonitorEngine attribute: {name}")
            raise AttributeError(name)

    class OkScraper:
        def __init__(self, username, password):
            self.user_id = "99"
            self.full_name = "Test Owner"
            self.error_message = ""

        def login(self):
            return True

        def close(self):
            pass

    engine = StrictEngine()
    monkeypatch.setattr(webapp_server, "_ENGINE", engine)
    monkeypatch.setattr(webapp_server, "LMSScraper", OkScraper)

    st, _, _, body = _http_req(
        client, "POST", "/api/login",
        body=json.dumps({"username": "20240001", "password": "secret"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st == 200
    assert json.loads(body)["success"] is True
    assert engine.calls == [(TEST_OWNER_ID, {"logout": False})]


def test_h5_mark_submitted_creates_and_toggles_item_not_yet_in_reminders_or_seen(web_client):
    client, _ = web_client
    init_data = _build_signed_init_data()
    db.add_course(TEST_OWNER_ID, 202, "Networks", "https://mulms.mutah.edu.jo/course/view.php?id=202")

    # Item doesn't exist in reminders OR seen_content yet
    st1, _, _, _ = _http_req(
        client, "POST", "/api/mark-submitted",
        body=json.dumps({
            "course_id": 202,
            "item_key": "quiz_888",
            "title": "Quiz 3",
            "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=888",
            "item_type": "quiz",
            "is_submitted": 1,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st1 == 200
    sc = db.get_seen_content(TEST_OWNER_ID, "quiz_888", "quiz")
    assert sc is not None and sc["is_submitted"] == 1

    st0, _, _, _ = _http_req(
        client, "POST", "/api/mark-submitted",
        body=json.dumps({
            "course_id": 202,
            "item_key": "quiz_888",
            "title": "Quiz 3",
            "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=888",
            "item_type": "quiz",
            "is_submitted": 0,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st0 == 200
    sc0 = db.get_seen_content(TEST_OWNER_ID, "quiz_888", "quiz")
    assert sc0 is not None and sc0["is_submitted"] == 0
    with db.DBContext() as ctx:
        rem0 = ctx.fetchone(
            "SELECT is_submitted FROM reminders WHERE telegram_id=? AND content_id=?",
            (TEST_OWNER_ID, "quiz_888"),
        )
    assert rem0 is not None and rem0["is_submitted"] == 0


def test_h4_logout_integrates_with_real_monitor_engine_invalidate_user_session(web_client, monkeypatch):
    """Integration test against the real MonitorEngine.invalidate_user_session API."""
    from monitor import MonitorEngine

    client, _ = web_client
    init_data = _build_signed_init_data()

    class DummyBot:
        pass

    class DummyScraper:
        def __init__(self):
            self.logout_called = False
            self.close_called = False

        def logout(self):
            self.logout_called = True

        def close(self):
            self.close_called = True

    engine = MonitorEngine(DummyBot())
    dummy_scraper = DummyScraper()
    engine._sessions[TEST_OWNER_ID] = ("20240001", "secret_pass", dummy_scraper)
    engine._auth_required.add(TEST_OWNER_ID)
    engine._waf_backoff_until[TEST_OWNER_ID] = time.monotonic() + 300

    monkeypatch.setattr(webapp_server, "_ENGINE", engine)

    st, _, _, body = _http_req(
        client, "POST", "/api/logout",
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
    )
    assert st == 200
    assert json.loads(body)["success"] is True
    assert dummy_scraper.logout_called is True
    assert TEST_OWNER_ID not in engine._sessions
    assert TEST_OWNER_ID not in engine._auth_required
    assert TEST_OWNER_ID not in engine._waf_backoff_until
