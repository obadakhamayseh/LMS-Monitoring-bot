"""Prove-It tests for Phase P2 F2: FastAPI + Uvicorn, OpenAPI, Pydantic, Rate Limit, Static & Lifecycle."""

from __future__ import annotations

import os
from cryptography.fernet import Fernet

# Isolated test environment before project imports
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import asyncio
import hashlib
import hmac
import json
import socket
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
import pytest
from fastapi.testclient import TestClient

import database as db
import webapp_server

TEST_BOT_TOKEN = "123456:TEST"
TEST_OWNER_ID = 1


def _signed_init(user_id: int = TEST_OWNER_ID, auth_date: Optional[int] = None) -> str:
    ts = int(time.time()) if auth_date is None else auth_date
    params = {
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
        "auth_date": str(ts),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


@pytest.fixture()
def fastapi_client(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_fastapi_p2.db")
    monkeypatch.setattr(db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    monkeypatch.setattr(webapp_server, "TELEGRAM_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setattr(webapp_server, "OWNER_TELEGRAM_ID", TEST_OWNER_ID)
    monkeypatch.setattr(webapp_server, "_ENGINE", None)
    monkeypatch.setattr(webapp_server, "_REMINDER_ENGINE", None)
    webapp_server.reset_rate_limits()

    db.init_db()
    db.save_user(TEST_OWNER_ID, "20240001", "secret_pass", "99", "Test Owner")

    with TestClient(webapp_server.app) as client:
        yield client


def test_f2_no_legacy_http_server_remains_in_webapp_server():
    source = Path(webapp_server.__file__).read_text(encoding="utf-8")
    assert "ThreadingHTTPServer" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "from http.server" not in source


def test_f2_openapi_schema_and_swagger_docs_work(fastapi_client):
    resp_schema = fastapi_client.get("/openapi.json")
    assert resp_schema.status_code == 200
    schema = resp_schema.json()
    assert schema["info"]["title"] == "Mutah LMS Monitor API"
    paths = schema["paths"]
    for expected in (
        "/health",
        "/api/user-data",
        "/api/pair",
        "/api/login",
        "/api/logout",
        "/api/app-logout",
        "/api/unlink-device",
        "/api/toggle-monitor",
        "/api/toggle-course",
        "/api/add-course",
        "/api/delete-course",
        "/api/sync-courses",
        "/api/force-check",
        "/api/update-settings",
        "/api/user-meta",
        "/api/mark-submitted",
    ):
        assert expected in paths, f"Missing OpenAPI path: {expected}"

    resp_docs = fastapi_client.get("/docs")
    assert resp_docs.status_code == 200
    assert "swagger-ui" in resp_docs.text.lower()


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("/api/login", {"username": "", "password": "secret"}),
        ("/api/login", {"username": "20240001", "password": 999}),
        ("/api/toggle-course", {"course_id": "not-an-int"}),
        ("/api/toggle-course", {"course_id": True}),
        ("/api/delete-course", {"clear_all": False}),
        ("/api/delete-course", {"course_id": "abc"}),
        ("/api/add-course", {"course_id": "invalid-text"}),
        ("/api/update-settings", {"check_interval": 2}),
        ("/api/update-settings", {"check_interval": 2000}),
        ("/api/update-settings", {"is_monitoring": "not-a-bool-obj" * 2}),
        ("/api/user-meta", {"meta": ["not", "a", "dict"]}),
        ("/api/mark-submitted", {"is_submitted": 1}),
    ],
)
def test_f2_pydantic_validation_rejects_bad_payloads_with_clean_400(fastapi_client, endpoint, payload):
    init_data = _signed_init()
    resp = fastapi_client.post(
        endpoint,
        json=payload,
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code in (400, 422), f"{endpoint} {payload!r} returned {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


def test_f2_chunked_streaming_body_over_max_bytes_returns_413_pre_auth(fastapi_client, monkeypatch):
    auth_calls = 0
    orig_auth = webapp_server._authenticate_request

    async def spy_auth(request):
        nonlocal auth_calls
        auth_calls += 1
        return await orig_auth(request)

    monkeypatch.setattr(webapp_server, "_authenticate_request", spy_auth)

    # Stream 5 chunks of 20KB each (100KB > 64KB) with NO Content-Length header
    chunks_delivered = 0

    async def run_chunked():
        nonlocal chunks_delivered
        status = 0
        body_out = bytearray()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/toggle-monitor",
            "raw_path": b"/api/toggle-monitor",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"transfer-encoding", b"chunked"),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 55555),
            "server": ("127.0.0.1", 8080),
        }

        async def receive():
            nonlocal chunks_delivered
            chunks_delivered += 1
            if chunks_delivered <= 5:
                return {"type": "http.request", "body": b"a" * 20_000, "more_body": chunks_delivered < 5}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                body_out.extend(msg.get("body", b""))

        await webapp_server.app(scope, receive, send)
        return status, bytes(body_out)

    status, raw = asyncio.run(run_chunked())
    assert status == 413
    assert chunks_delivered == 4  # Stopped as soon as 80KB > 64KB, did NOT read 5th chunk
    assert auth_calls == 0
    assert json.loads(raw.decode("utf-8"))["success"] is False


def test_f2_general_per_ip_api_rate_limiting_returns_429(fastapi_client, monkeypatch):
    monkeypatch.setattr(webapp_server, "API_RATE_LIMIT_MAX", 5)
    monkeypatch.setattr(webapp_server, "API_RATE_LIMIT_WINDOW", 60)
    webapp_server.reset_rate_limits()
    init_data = _signed_init()

    # First 5 requests from IP A succeed
    for _ in range(5):
        r = fastapi_client.get(
            "/api/user-data",
            headers={"X-Telegram-Init-Data": init_data, "X-Forwarded-For": "198.51.100.77"},
        )
        assert r.status_code == 200

    # 6th request from IP A returns 429 with Retry-After
    r_lim = fastapi_client.get(
        "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "X-Forwarded-For": "198.51.100.77"},
    )
    assert r_lim.status_code == 429
    assert r_lim.json()["rate_limited"] is True
    assert int(r_lim.headers.get("retry-after", "0")) >= 1

    # Different IP B is unaffected
    r_other = fastapi_client.get(
        "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "X-Forwarded-For": "203.0.113.88"},
    )
    assert r_other.status_code == 200

    # Resetting rate limits restores IP A
    webapp_server.reset_rate_limits()
    r_reset = fastapi_client.get(
        "/api/user-data",
        headers={"X-Telegram-Init-Data": init_data, "X-Forwarded-For": "198.51.100.77"},
    )
    assert r_reset.status_code == 200


def test_f2_static_assets_serve_js_css_and_block_path_traversal(fastapi_client, tmp_path, monkeypatch):
    static_root = tmp_path / "static"
    (static_root / "js").mkdir(parents=True)
    (static_root / "css").mkdir(parents=True)
    (static_root / "js" / "sample.js").write_text("console.log('ok');", encoding="utf-8")
    (static_root / "css" / "sample.css").write_text("body { color: #fff; }", encoding="utf-8")
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("TOP_SECRET", encoding="utf-8")

    monkeypatch.setattr(webapp_server, "_STATIC_ASSETS_DIR", static_root.resolve())

    r_js = fastapi_client.get("/static/js/sample.js")
    assert r_js.status_code == 200
    assert "javascript" in r_js.headers["content-type"].lower()
    assert "console.log('ok');" in r_js.text

    r_css = fastapi_client.get("/static/css/sample.css")
    assert r_css.status_code == 200
    assert "text/css" in r_css.headers["content-type"].lower()
    assert "color: #fff" in r_css.text

    # Path traversal attempts (plain, encoded, backslash, nested) must NEVER escape static_root
    for bad_path in (
        "/static/../secret.txt",
        "/static/%2e%2e/secret.txt",
        "/static/..%2fsecret.txt",
        "/static/js/../../secret.txt",
        "/static/..\\secret.txt",
        "/static/js/..%5c..%5csecret.txt",
    ):
        r_bad = fastapi_client.get(bad_path)
        assert r_bad.status_code == 404, f"Traversal path not blocked ({bad_path}): {r_bad.status_code}"
        assert "TOP_SECRET" not in r_bad.text


def test_f2_webapp_csp_drops_unsafe_inline_from_script_src(fastapi_client):
    resp = fastapi_client.get("/webapp")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    directives = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
    assert "script-src" in directives
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "https://telegram.org" in directives["script-src"]


def test_f2_async_uvicorn_lifecycle_and_real_tcp_pre_read_413(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_lifecycle.db")
    monkeypatch.setattr(db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    webapp_server.reset_rate_limits()
    db.init_db()

    # Reserve a free localhost port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def _run_lifecycle():
        task = await webapp_server.start_webapp_server_async(host="127.0.0.1", port=port)
        assert isinstance(task, asyncio.Task)
        assert not task.done()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r_health = await client.get(f"http://127.0.0.1:{port}/health")
                assert r_health.status_code == 200
                assert r_health.json() == {"status": "ok", "db": "ok"}

                r_head = await client.head(f"http://127.0.0.1:{port}/health")
                assert r_head.status_code == 200

            # Real TCP C5 test: advertise 10MB Content-Length with ZERO body bytes -> immediate 413
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                raw_req = (
                    f"POST /api/toggle-monitor HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Content-Length: 10485760\r\n"
                    f"Content-Type: application/json\r\n"
                    f"\r\n"
                ).encode("ascii")
                writer.write(raw_req)
                await writer.drain()
                status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                assert b"413" in status_line, f"Expected 413 over TCP, got: {status_line!r}"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await webapp_server.stop_webapp_server_async()

        assert task.done()
        assert webapp_server._UVICORN_SERVER is None
        assert webapp_server._UVICORN_TASK is None

    asyncio.run(_run_lifecycle())


def test_f2_engine_setters_update_module_references(monkeypatch):
    monkeypatch.setattr(webapp_server, "_ENGINE", None)
    monkeypatch.setattr(webapp_server, "_REMINDER_ENGINE", None)

    eng = object()
    rem = object()
    webapp_server.set_monitor_engine(eng)
    webapp_server.set_reminder_engine(rem)
    assert webapp_server._ENGINE is eng
    assert webapp_server._REMINDER_ENGINE is rem

    webapp_server.init_webapp_engines(None, None)
    assert webapp_server._ENGINE is None
    assert webapp_server._REMINDER_ENGINE is None


def test_f2_real_extracted_static_assets_served_with_correct_content_types(fastapi_client):
    expected_assets = {
        "/static/css/app.css": "text/css",
        "/static/js/state.js": "javascript",
        "/static/js/dates.js": "javascript",
        "/static/js/api.js": "javascript",
        "/static/js/render.js": "javascript",
        "/static/js/actions.js": "javascript",
    }
    for url, expected_ctype in expected_assets.items():
        resp = fastapi_client.get(url)
        assert resp.status_code == 200, f"Expected 200 for {url}, got {resp.status_code}"
        assert expected_ctype in resp.headers["content-type"].lower()
        assert len(resp.content) > 0


def test_f2_add_sync_and_force_check_delegate_to_sync_service_seed_course_snapshot(fastapi_client, monkeypatch):
    from lms_scraper import FetchResult

    init_data = _signed_init()
    seed_calls: list[tuple[int, int, str]] = []
    orig_seed = webapp_server.seed_course_snapshot

    def spy_seed(database_mod, telegram_id, course_id, content, **kwargs):
        seed_calls.append((int(telegram_id), int(course_id), str(kwargs.get("course_name", ""))))
        return orig_seed(database_mod, telegram_id, course_id, content, **kwargs)

    class StubScraper:
        def __init__(self, username, password):
            self.waf_blocked = False
            self.session_expired = False
            self.error_message = ""

        def login(self):
            return True

        def get_course_info(self, cid):
            return (
                f"Course {cid}",
                {"assignments": [{"id": f"a_{cid}", "name": "HW", "url": "https://mulms.mutah.edu.jo/a"}]},
            )

        def get_courses(self):
            return [{"id": 502, "name": "Course 502", "url": "https://mulms.mutah.edu.jo/course/view.php?id=502"}]

        def fetch_course_content(self, cid):
            return FetchResult(
                content={"assignments": [{"id": f"a_{cid}", "name": "HW", "url": "https://mulms.mutah.edu.jo/a"}]},
                ok=True,
            )

        def close(self):
            pass

    monkeypatch.setattr(webapp_server, "seed_course_snapshot", spy_seed)
    monkeypatch.setattr(webapp_server, "LMSScraper", StubScraper)

    r_add = fastapi_client.post(
        "/api/add-course",
        json={"course_id": "501"},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert r_add.status_code == 200

    r_sync = fastapi_client.post(
        "/api/sync-courses",
        json={},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert r_sync.status_code == 200

    r_fc = fastapi_client.post(
        "/api/force-check",
        json={},
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert r_fc.status_code == 200

    cids_seeded = [cid for _, cid, _ in seed_calls]
    assert 501 in cids_seeded
    assert 502 in cids_seeded
    assert len(seed_calls) >= 4  # add(501) + sync(502) + force-check(501, 502)

