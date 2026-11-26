"""Prove-It tests for Features Phase Web API & Live iCal Feed (F1/F4/F5/F6/F7/F8)."""

from __future__ import annotations

import os
from cryptography.fernet import Fernet

# Configure isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST_WEB_ICAL_SECRET"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Optional

from fastapi.testclient import TestClient
import pytest

import config
import database as db
from services.ical_service import build_ical_feed, escape_ics_text, safe_ics_url
from services.observability import get_observability
import webapp_server

TEST_BOT_TOKEN = "123456:TEST_WEB_ICAL_SECRET"
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


def _parse_set_cookies(set_cookies: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in set_cookies:
        head = raw.split(";", 1)[0]
        if "=" in head:
            k, v = head.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _pair_device(client: TestClient, device_name: str = "pytest-device") -> tuple[str, str]:
    code = webapp_server.create_pairing_code(TEST_OWNER_ID)
    client.cookies.clear()
    resp = client.post(
        "/api/pair",
        json={"code": code, "device": device_name},
    )
    assert resp.status_code == 200, f"Pairing failed: {resp.status_code} {resp.text}"
    cookies = _parse_set_cookies(resp.headers.get_list("set-cookie"))
    return cookies["mutah_session"], cookies["mutah_csrf"]


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_features_web_ical.db")
    monkeypatch.setattr(db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    monkeypatch.setattr(webapp_server, "TELEGRAM_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setattr(webapp_server, "OWNER_TELEGRAM_ID", TEST_OWNER_ID)
    monkeypatch.setattr(webapp_server, "_ENGINE", None)
    monkeypatch.setattr(webapp_server, "_REMINDER_ENGINE", None)
    monkeypatch.setattr(config, "ENABLE_ICAL_FEED", True)
    monkeypatch.setattr(config, "ENABLE_ABSENCE_RISK", True)
    monkeypatch.setattr(config, "ENABLE_DAILY_DIGEST", True)
    monkeypatch.setattr(config, "ENABLE_MOODLE_API", False)
    monkeypatch.setattr(config, "ENABLE_WEB_PUSH", False)
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "")

    get_observability().reset()
    webapp_server.reset_rate_limits()

    db.init_db()
    db.save_user(TEST_OWNER_ID, "20240001", "super_secret_lms_pw", "99", "Test Owner")
    db.add_course(TEST_OWNER_ID, 101, "Algorithms", "https://mulms.mutah.edu.jo/course/view.php?id=101")

    with TestClient(webapp_server.app) as client:
        yield client


def _extract_vevents(ics: str) -> list[dict[str, str]]:
    lines = ics.split("\r\n")
    events: list[dict[str, str]] = []
    current: Optional[dict[str, str]] = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            k, v = line.split(":", 1)
            current[k] = v
    return events


# ── F4: iCal Service Unit & RFC 5545 Compliance Tests ──


def test_f4_ical_feed_rfc5545_compliance_sources_escaping_and_filtering(api_client):
    fixed_now = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone.utc)
    future_ts = int(datetime(2026, 12, 5, 18, 0, 0, tzinfo=timezone.utc).timestamp())
    past_ts = int(datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc).timestamp())

    # 1. Reminder item (upcoming, with special RFC 5545 chars and valid https URL)
    db.upsert_reminder(
        TEST_OWNER_ID,
        101,
        "assign_501",
        "HW 1, Part; A\\B\nLine2",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=501",
        future_ts,
        "5 December 2026, 21:00",
        is_submitted=0,
    )
    # Duplicate in seen_content (must NOT produce duplicate VEVENT)
    db.mark_seen(
        TEST_OWNER_ID,
        101,
        "assign_501",
        "assign",
        "HW 1, Part; A\\B\nLine2",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=501",
        due="2026-12-05 21:00",
        is_submitted=0,
    )
    # Past reminder -> must be skipped
    db.upsert_reminder(
        TEST_OWNER_ID,
        101,
        "assign_past",
        "Old Homework",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=900",
        past_ts,
        "20 November 2026, 21:00",
        is_submitted=0,
    )
    # Completed reminder -> must be skipped
    db.upsert_reminder(
        TEST_OWNER_ID,
        101,
        "assign_done",
        "Submitted Homework",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=901",
        future_ts + 7200,
        "5 December 2026, 23:00",
        is_submitted=1,
    )
    # 2. seen_content-only item with valid Arabic/ISO due date & unsafe javascript URL
    db.mark_seen(
        TEST_OWNER_ID,
        101,
        "quiz_601",
        "quiz",
        "Quiz 2",
        "javascript:alert(1)",
        due="2026-12-08 15:30",
        is_submitted=0,
    )
    # seen_content with NO due date and invalid due date -> must NEVER fabricate dates!
    db.mark_seen(
        TEST_OWNER_ID,
        101,
        "assign_nodate",
        "assign",
        "No Date Assignment",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=602",
        due="",
        is_submitted=0,
    )
    db.mark_seen(
        TEST_OWNER_ID,
        101,
        "assign_baddate",
        "assign",
        "Unparseable Date Assignment",
        "https://mulms.mutah.edu.jo/mod/assign/view.php?id=603",
        due="not a real date",
        is_submitted=0,
    )
    # 3. user_meta.personal_reminders (one upcoming, one invalid date, one completed)
    db.merge_user_meta(
        TEST_OWNER_ID,
        {
            "personal_reminders": [
                {
                    "title": "Study for Midterm, Ch;1",
                    "course_name": "Algorithms",
                    "when": "2026-12-10T14:00",
                    "url": "https://mulms.mutah.edu.jo/my/",
                },
                {
                    "title": "Invalid Personal Reminder",
                    "course_name": "Algorithms",
                    "when": "someday soon",
                },
                {
                    "title": "Completed Personal Reminder",
                    "course_name": "Algorithms",
                    "when": "2026-12-11T14:00",
                    "done": True,
                },
            ]
        },
    )

    ics = build_ical_feed(db, TEST_OWNER_ID, now=fixed_now)
    # Strict CRLF check: no bare LF anywhere
    assert ics.endswith("\r\n")
    assert "\n" not in ics.replace("\r\n", "")

    assert "BEGIN:VCALENDAR\r\n" in ics
    assert "VERSION:2.0\r\n" in ics
    assert "PRODID:-//Mutah LMS Monitor//EN\r\n" in ics
    assert "CALSCALE:GREGORIAN\r\n" in ics
    assert "X-WR-CALNAME:Mutah LMS Monitor\r\n" in ics
    assert "X-WR-TIMEZONE:Asia/Amman\r\n" in ics
    assert ics.endswith("END:VCALENDAR\r\n")

    vevents = _extract_vevents(ics)
    assert len(vevents) == 3, f"Expected exactly 3 upcoming valid VEVENTs, got {vevents}"

    # Verify first event (reminder assign_501)
    ev1 = vevents[0]
    assert ev1["DTSTAMP"] == "20261127T100000Z"
    assert ev1["DTSTART"] == "20261205T180000Z"
    assert ev1["DTEND"] == "20261205T190000Z"  # +1 hour exact
    assert r"HW 1\, Part\; A\\B\nLine2" in ev1["SUMMARY"]
    assert ev1["URL"] == "https://mulms.mutah.edu.jo/mod/assign/view.php?id=501"

    # Verify second event (seen_content quiz_601) omitted unsafe javascript: URL
    ev2 = vevents[1]
    assert "Quiz 2" in ev2["SUMMARY"]
    assert "URL" not in ev2
    assert "javascript:" not in ics

    # Verify third event (personal reminder)
    ev3 = vevents[2]
    assert r"Study for Midterm\, Ch\;1" in ev3["SUMMARY"]
    assert ev3["URL"] == "https://mulms.mutah.edu.jo/my/"

    # Verify stability of UID across different `now` values
    ics_later = build_ical_feed(db, TEST_OWNER_ID, now=datetime(2026, 11, 28, 12, 0, 0, tzinfo=timezone.utc))
    vevents_later = _extract_vevents(ics_later)
    assert [e["UID"] for e in vevents] == [e["UID"] for e in vevents_later]

    # Direct helper checks
    assert escape_ics_text("a\\b;c,d\r\ne") == r"a\\b\;c\,d\ne"
    assert safe_ics_url("https://example.com/a") == "https://example.com/a"
    assert safe_ics_url("http://example.com/a") == "http://example.com/a"
    assert safe_ics_url("javascript:alert(1)") == ""
    assert safe_ics_url("https://example.com/a\r\nINJECT:1") == ""


# ── F4: Live iCal Endpoints, Token Rotation/Revocation, Disable Toggle & Zero Log Leaks ──


def test_f4_calendar_token_lifecycle_and_feed_endpoints(api_client, monkeypatch):
    sess_tok, csrf_tok = _pair_device(api_client, "ical-phone")
    cookie_hdr = f"mutah_session={sess_tok}; mutah_csrf={csrf_tok}"

    # 1. Initially no token (GET must NOT auto-generate)
    r_init = api_client.get("/api/calendar-token", headers={"Cookie": cookie_hdr})
    assert r_init.status_code == 200
    d_init = r_init.json()
    assert d_init == {"success": True, "enabled": True, "has_token": False, "feed_url": ""}

    # 2. Rotate requires CSRF on cookie auth
    r_no_csrf = api_client.post("/api/calendar-token/rotate", json={}, headers={"Cookie": cookie_hdr})
    assert r_no_csrf.status_code == 403

    # 3. Rotate generates token & feed URL
    r_rot1 = api_client.post(
        "/api/calendar-token/rotate",
        json={},
        headers={"Cookie": cookie_hdr, "X-CSRF-Token": csrf_tok},
    )
    assert r_rot1.status_code == 200
    d_rot1 = r_rot1.json()
    assert d_rot1["success"] is True
    tok1 = d_rot1["token"]
    assert len(tok1) >= 24
    assert d_rot1["feed_url"] == f"/api/calendar.ics?token={tok1}"

    # GET /api/calendar-token now returns active token URL
    r_after = api_client.get("/api/calendar-token", headers={"Cookie": cookie_hdr})
    assert r_after.status_code == 200
    assert r_after.json()["has_token"] is True
    assert r_after.json()["feed_url"] == f"/api/calendar.ics?token={tok1}"

    # 4. Fetch live .ics feed with valid token
    r_ics = api_client.get(f"/api/calendar.ics?token={tok1}")
    assert r_ics.status_code == 200
    assert r_ics.headers["content-type"].lower().startswith("text/calendar")
    assert "no-store" in r_ics.headers["cache-control"].lower()
    assert 'filename="mutah-lms.ics"' in r_ics.headers["content-disposition"]
    assert r_ics.text.startswith("BEGIN:VCALENDAR\r\n")

    # 5. Missing / wrong token -> 401
    assert api_client.get("/api/calendar.ics").status_code == 401
    assert api_client.get("/api/calendar.ics?token=invalid_token_value_12345").status_code == 401

    # 6. Rotate invalidates previous token
    r_rot2 = api_client.post(
        "/api/calendar-token/rotate",
        json={},
        headers={"Cookie": cookie_hdr, "X-CSRF-Token": csrf_tok},
    )
    assert r_rot2.status_code == 200
    tok2 = r_rot2.json()["token"]
    assert tok2 != tok1
    assert api_client.get(f"/api/calendar.ics?token={tok1}").status_code == 401
    assert api_client.get(f"/api/calendar.ics?token={tok2}").status_code == 200

    # 7. Per-user disable toggle returns 401 on feed
    r_dis = api_client.post(
        "/api/update-settings",
        json={"enable_ical_feed": False},
        headers={"Cookie": cookie_hdr, "X-CSRF-Token": csrf_tok},
    )
    assert r_dis.status_code == 200
    assert api_client.get(f"/api/calendar.ics?token={tok2}").status_code == 401
    r_tok_dis = api_client.get("/api/calendar-token", headers={"Cookie": cookie_hdr})
    assert r_tok_dis.json()["enabled"] is False

    # Re-enable user toggle -> 200 again
    api_client.post(
        "/api/update-settings",
        json={"enable_ical_feed": True},
        headers={"Cookie": cookie_hdr, "X-CSRF-Token": csrf_tok},
    )
    assert api_client.get(f"/api/calendar.ics?token={tok2}").status_code == 200

    # 8. Global disable toggle returns 404
    monkeypatch.setattr(config, "ENABLE_ICAL_FEED", False)
    assert api_client.get(f"/api/calendar.ics?token={tok2}").status_code == 404
    monkeypatch.setattr(config, "ENABLE_ICAL_FEED", True)

    # 9. Revoke invalidates active token
    r_rev = api_client.post(
        "/api/calendar-token/revoke",
        json={},
        headers={"Cookie": cookie_hdr, "X-CSRF-Token": csrf_tok},
    )
    assert r_rev.status_code == 200
    assert r_rev.json()["success"] is True
    assert api_client.get(f"/api/calendar.ics?token={tok2}").status_code == 401
    r_tok_rev = api_client.get("/api/calendar-token", headers={"Cookie": cookie_hdr})
    assert r_tok_rev.json()["has_token"] is False
    assert r_tok_rev.json()["feed_url"] == ""


def test_f4_calendar_secret_token_never_leaks_into_logs_on_200_401_404_500(api_client, monkeypatch, caplog):
    init_data = _signed_init()
    r_rot = api_client.post(
        "/api/calendar-token/rotate",
        json={},
        headers={"X-Telegram-Init-Data": init_data},
    )
    valid_token = r_rot.json()["token"]
    bad_token_401 = "SECRET_BAD_TOKEN_401_DO_NOT_LOG_XYZ987"
    disabled_token_404 = "SECRET_DISABLED_TOKEN_404_DO_NOT_LOG_ABC654"

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        # 200 OK
        r200 = api_client.get(f"/api/calendar.ics?token={valid_token}")
        assert r200.status_code == 200

        # 401 Unauthorized (triggers AccessErrorLoggingMiddleware WARNING)
        r401 = api_client.get(f"/api/calendar.ics?token={bad_token_401}")
        assert r401.status_code == 401

        # 404 Global Disabled (triggers AccessErrorLoggingMiddleware WARNING)
        monkeypatch.setattr(config, "ENABLE_ICAL_FEED", False)
        r404 = api_client.get(f"/api/calendar.ics?token={disabled_token_404}")
        assert r404.status_code == 404
        monkeypatch.setattr(config, "ENABLE_ICAL_FEED", True)

        # 500 Internal Error (triggers AccessErrorLoggingMiddleware ERROR)
        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated ical failure")

        monkeypatch.setattr(webapp_server, "build_ical_feed", _boom)
        with TestClient(webapp_server.app, raise_server_exceptions=False) as err_client:
            r500 = err_client.get(f"/api/calendar.ics?token={valid_token}")
            assert r500.status_code == 500

    assert caplog.records, "Expected middleware warning/error logs for 401/404/500"
    all_log_text = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    for secret in (valid_token, bad_token_401, disabled_token_404, "token="):
        assert secret not in all_log_text, f"Secret token leaked into logs: {secret!r} in {all_log_text}"
    assert any("/api/calendar.ics" in r.getMessage() for r in caplog.records)


# ── F1 / F5 / F6: Extended /api/user-data, /api/update-settings & /api/course-credits ──


def test_f1_f5_f6_user_data_includes_features_settings_and_absence_risks(api_client, monkeypatch):
    init_data = _signed_init()
    db.upsert_attendance(
        TEST_OWNER_ID,
        101,
        "att_101",
        taken_sessions=20,
        total_sessions=48,
        present_count=14,
        absent_count=6,  # 15% of 48 = 7 -> remaining 1 -> critical
    )

    resp = api_client.get("/api/user-data", headers={"X-Telegram-Init-Data": init_data})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True

    # User feature settings
    u = data["user"]
    assert u["use_moodle_api"] is False
    assert u["enable_ical_feed"] is True
    assert u["digest_enabled"] is True
    assert u["digest_time"] == "08:00"
    assert u["notify_absence_risk"] is True
    assert u["max_absence_pct"] == 15.0
    assert u["enable_web_push"] is True

    # Features map
    feats = data["features"]
    assert feats == {
        "moodle_api_global": False,
        "ical_global": True,
        "digest_global": True,
        "absence_risk_global": True,
        "web_push_global": False,
    }

    # Absence risks populated
    risks = data["absence_risks"]
    assert len(risks) == 1
    assert risks[0]["course_id"] == 101
    assert risks[0]["risk_level"] == "critical"
    assert risks[0]["remaining"] == 1

    # When ENABLE_ABSENCE_RISK is False -> absence_risks is []
    monkeypatch.setattr(config, "ENABLE_ABSENCE_RISK", False)
    resp_off = api_client.get("/api/user-data", headers={"X-Telegram-Init-Data": init_data})
    assert resp_off.status_code == 200
    assert resp_off.json()["absence_risks"] == []
    assert resp_off.json()["features"]["absence_risk_global"] is False


def test_f1_f5_f6_update_settings_valid_and_invalid_payloads(api_client):
    init_data = _signed_init()
    hdrs = {"X-Telegram-Init-Data": init_data}

    # Valid update across all new settings
    r_ok = api_client.post(
        "/api/update-settings",
        json={
            "use_moodle_api": True,
            "enable_ical_feed": False,
            "digest_enabled": False,
            "digest_time": "07:30",
            "notify_absence_risk": False,
            "max_absence_pct": 20.5,
            "enable_web_push": False,
        },
        headers=hdrs,
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["success"] is True

    u = api_client.get("/api/user-data", headers=hdrs).json()["user"]
    assert u["use_moodle_api"] is True
    assert u["enable_ical_feed"] is False
    assert u["digest_enabled"] is False
    assert u["digest_time"] == "07:30"
    assert u["notify_absence_risk"] is False
    assert u["max_absence_pct"] == 20.5
    assert u["enable_web_push"] is False

    # Boundary digest_time values accepted
    for valid_t in ("00:00", "23:59"):
        r = api_client.post("/api/update-settings", json={"digest_time": valid_t}, headers=hdrs)
        assert r.status_code == 200, f"Expected 200 for digest_time={valid_t!r}"

    # Invalid digest_time rejected with 400
    for bad_t in ("24:00", "8:00", "08:60", "-1:00", "12:5", "abc", "", None, 800):
        r = api_client.post("/api/update-settings", json={"digest_time": bad_t}, headers=hdrs)
        assert r.status_code == 400, f"Expected 400 for digest_time={bad_t!r}, got {r.status_code}"
        assert r.json()["success"] is False

    # Boundary max_absence_pct values accepted
    for valid_pct in (1.0, 50.0, "12.5"):
        r = api_client.post("/api/update-settings", json={"max_absence_pct": valid_pct}, headers=hdrs)
        assert r.status_code == 200, f"Expected 200 for max_absence_pct={valid_pct!r}"

    # Invalid max_absence_pct rejected with 400
    for bad_pct in (0, 0.9, -10, 50.1, 100, "abc", "", True, False, None):
        r = api_client.post("/api/update-settings", json={"max_absence_pct": bad_pct}, headers=hdrs)
        assert r.status_code == 400, f"Expected 400 for max_absence_pct={bad_pct!r}, got {r.status_code}"
        assert r.json()["success"] is False


def test_f6_course_credits_endpoint_validation_and_persistence(api_client):
    sess_tok, csrf_tok = _pair_device(api_client, "credits-device")
    cookie_hdr = f"mutah_session={sess_tok}; mutah_csrf={csrf_tok}"
    hdrs = {"Cookie": cookie_hdr, "X-CSRF-Token": csrf_tok}

    # Missing CSRF rejected
    r_csrf = api_client.post(
        "/api/course-credits",
        json={"course_id": 101, "credit_hours": 4},
        headers={"Cookie": cookie_hdr},
    )
    assert r_csrf.status_code == 403

    # Valid update
    r_ok = api_client.post(
        "/api/course-credits",
        json={"course_id": 101, "credit_hours": 4},
        headers=hdrs,
    )
    assert r_ok.status_code == 200
    assert r_ok.json() == {"success": True, "course_id": 101, "credit_hours": 4}
    assert db.get_course(TEST_OWNER_ID, 101)["credit_hours"] == 4

    # Reflected in /api/user-data
    ud = api_client.get("/api/user-data", headers={"Cookie": cookie_hdr}).json()
    assert ud["courses"][0]["credit_hours"] == 4

    # Unknown course -> 404
    r_404 = api_client.post(
        "/api/course-credits",
        json={"course_id": 99999, "credit_hours": 3},
        headers=hdrs,
    )
    assert r_404.status_code == 404

    # Invalid course_id or credit_hours -> 400
    for payload in (
        {"course_id": 0, "credit_hours": 3},
        {"course_id": "abc", "credit_hours": 3},
        {"course_id": 101, "credit_hours": 0},
        {"course_id": 101, "credit_hours": 13},
        {"course_id": 101, "credit_hours": -1},
        {"course_id": 101, "credit_hours": 2.5},
        {"course_id": 101, "credit_hours": True},
        {"course_id": 101, "credit_hours": None},
    ):
        r_bad = api_client.post("/api/course-credits", json=payload, headers=hdrs)
        assert r_bad.status_code == 400, f"Expected 400 for {payload!r}, got {r_bad.status_code}"
        assert r_bad.json()["success"] is False


# ── F7: Web Push VAPID Endpoints & Device Unlink/Logout Cleanup ──


def test_f7_vapid_public_key_subscribe_unsubscribe_and_validation(api_client, monkeypatch):
    init_data = _signed_init()
    hdrs = {"X-Telegram-Init-Data": init_data}

    # Unauthenticated -> 401
    assert api_client.get("/api/push/vapid-public-key").status_code == 401

    # Disabled globally
    r_off = api_client.get("/api/push/vapid-public-key", headers=hdrs)
    assert r_off.status_code == 200
    assert r_off.json() == {"success": True, "enabled": False, "public_key": ""}

    # Enabled globally
    monkeypatch.setattr(config, "ENABLE_WEB_PUSH", True)
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "test_private_key_not_exposed")

    r_on = api_client.get("/api/push/vapid-public-key", headers=hdrs)
    assert r_on.status_code == 200
    body_on = r_on.json()
    assert body_on["success"] is True
    assert body_on["enabled"] is True
    assert body_on["public_key"] == config.VAPID_PUBLIC_KEY
    assert "test_private_key_not_exposed" not in r_on.text

    # Invalid subscribe payloads rejected with 400
    for bad_sub in (
        {"endpoint": "http://insecure.example/push", "keys": {"p256dh": "k1", "auth": "a1"}},
        {"endpoint": "javascript:alert(1)", "keys": {"p256dh": "k1", "auth": "a1"}},
        {"endpoint": "https://push.example/" + "x" * 2040, "keys": {"p256dh": "k1", "auth": "a1"}},
        {"endpoint": "https://push.example/ok", "keys": {"p256dh": "", "auth": "a1"}},
        {"endpoint": "https://push.example/ok", "keys": {"p256dh": "k1", "auth": "   "}},
        {"endpoint": "https://push.example/ok", "keys": {"p256dh": 123, "auth": "a1"}},
        {"endpoint": "https://push.example/ok"},
    ):
        r_bad = api_client.post("/api/push/subscribe", json=bad_sub, headers=hdrs)
        assert r_bad.status_code == 400, f"Expected 400 for {bad_sub!r}, got {r_bad.status_code}"

    # Valid subscribe + unsubscribe
    ep = "https://fcm.googleapis.com/fcm/send/test-sub-1"
    r_sub = api_client.post(
        "/api/push/subscribe",
        json={"endpoint": ep, "keys": {"p256dh": "pub_key_b64", "auth": "auth_b64"}},
        headers=hdrs,
    )
    assert r_sub.status_code == 200
    assert r_sub.json()["success"] is True
    subs = db.get_push_subscriptions(TEST_OWNER_ID)
    assert len(subs) == 1 and subs[0]["endpoint"] == ep

    # Invalid unsubscribe endpoint -> 400
    assert api_client.post("/api/push/unsubscribe", json={"endpoint": "http://bad"}, headers=hdrs).status_code == 400

    # Valid unsubscribe removes it
    r_unsub = api_client.post("/api/push/unsubscribe", json={"endpoint": ep}, headers=hdrs)
    assert r_unsub.status_code == 200
    assert db.get_push_subscriptions(TEST_OWNER_ID) == []


@pytest.mark.parametrize("unlink_endpoint", ["/api/app-logout", "/api/unlink-device"])
def test_f7_device_unlink_cleans_only_that_device_push_subscription(api_client, unlink_endpoint):
    sess_a, csrf_a = _pair_device(api_client, "device-A")
    sess_b, csrf_b = _pair_device(api_client, "device-B")

    ep_a = "https://push.example.com/sub-device-a"
    ep_b = "https://push.example.com/sub-device-b"

    # Subscribe device A & device B
    r_a = api_client.post(
        "/api/push/subscribe",
        json={"endpoint": ep_a, "keys": {"p256dh": "pkA", "auth": "auA"}},
        headers={"Cookie": f"mutah_session={sess_a}; mutah_csrf={csrf_a}", "X-CSRF-Token": csrf_a},
    )
    assert r_a.status_code == 200
    r_b = api_client.post(
        "/api/push/subscribe",
        json={"endpoint": ep_b, "keys": {"p256dh": "pkB", "auth": "auB"}},
        headers={"Cookie": f"mutah_session={sess_b}; mutah_csrf={csrf_b}", "X-CSRF-Token": csrf_b},
    )
    assert r_b.status_code == 200
    assert {s["endpoint"] for s in db.get_push_subscriptions(TEST_OWNER_ID)} == {ep_a, ep_b}

    # Unlink device A -> only device A's push subscription is removed!
    r_unlink = api_client.post(
        unlink_endpoint,
        json={},
        headers={"Cookie": f"mutah_session={sess_a}; mutah_csrf={csrf_a}", "X-CSRF-Token": csrf_a},
    )
    assert r_unlink.status_code == 200
    remaining = db.get_push_subscriptions(TEST_OWNER_ID)
    assert [s["endpoint"] for s in remaining] == [ep_b]


def test_f7_full_logout_cleans_all_push_subscriptions_and_calendar_tokens(api_client):
    sess_a, csrf_a = _pair_device(api_client, "device-logout")
    hdrs = {"Cookie": f"mutah_session={sess_a}; mutah_csrf={csrf_a}", "X-CSRF-Token": csrf_a}

    api_client.post("/api/calendar-token/rotate", json={}, headers=hdrs)
    api_client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://push.example.com/logout-sub", "keys": {"p256dh": "pk", "auth": "au"}},
        headers=hdrs,
    )
    assert len(db.get_push_subscriptions(TEST_OWNER_ID)) == 1
    assert db.get_calendar_token(TEST_OWNER_ID) is not None

    r_out = api_client.post("/api/logout", json={}, headers=hdrs)
    assert r_out.status_code == 200
    assert db.get_push_subscriptions(TEST_OWNER_ID) == []
    assert db.get_calendar_token(TEST_OWNER_ID) is None


# ── F8: Enhanced GET /health with Observability Snapshot & Zero Secrets ──


def test_f8_health_endpoint_merges_observability_snapshot_and_preserves_503(api_client, monkeypatch):
    obs = get_observability()
    obs.set_monitor_running(True)
    obs.record_check_success(
        {"api_requests": 3, "scrape_requests": 1, "requests_saved": 2},
        now_ts=1_760_000_000.0,
    )
    obs.record_check_error("INTERNAL_SECRET_DO_NOT_EXPOSE_ON_HEALTH")
    obs.record_waf_state(True, until_ts=time.time() + 300)

    r_ok = api_client.get("/health")
    assert r_ok.status_code == 200
    data = r_ok.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["monitor_running"] is True
    assert data["last_successful_check_at"] == "2025-10-09T08:53:20Z"
    assert data["waf_blocked"] is True
    assert data["total_checks"] == 2
    assert data["error_count"] == 1
    assert data["error_rate"] == 0.5
    assert data["api_requests_total"] == 3
    assert data["scrape_requests_total"] == 1
    assert data["requests_saved_total"] == 2
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0.0

    # Zero secrets or raw internal error messages leaked
    raw_text = r_ok.text
    for forbidden in (
        TEST_BOT_TOKEN,
        "super_secret_lms_pw",
        "INTERNAL_SECRET_DO_NOT_EXPOSE_ON_HEALTH",
        "last_error",
    ):
        assert forbidden not in raw_text

    # DB unhealthy -> 503 while preserving observability metrics
    monkeypatch.setattr(db, "health_check", lambda: False)
    r_fail = api_client.get("/health")
    assert r_fail.status_code == 503
    fail_data = r_fail.json()
    assert fail_data["status"] == "error"
    assert fail_data["db"] == "error"
    assert fail_data["monitor_running"] is True
    assert fail_data["error_count"] == 1
