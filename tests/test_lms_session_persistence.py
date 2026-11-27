"""Tests for encrypted persistent LMS session cookies across restarts and webapp actions."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

_KEY = Fernet.generate_key().decode("utf-8")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:SESSION_TEST")
os.environ.setdefault("OWNER_TELEGRAM_ID", "999")
os.environ.setdefault("ENCRYPTION_KEY", _KEY)
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""

import database as db  # noqa: E402
from lms_scraper import LMSScraper  # noqa: E402
from monitor import MonitorEngine  # noqa: E402
import webapp_server  # noqa: E402


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_file = tmp_path / "session_persist.db"
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DIRECT_URL", "")
    monkeypatch.setattr(db, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    db.init_db()
    return db_file


def test_lms_session_encrypted_roundtrip_and_delete_cleanup(isolated_db: Path) -> None:
    db.save_user(999, "20240001", "secret_pw", "42", "Student Name")
    state = {
        "username": "20240001",
        "cookies": [
            {
                "name": "MoodleSession",
                "value": "sess_token_abc123",
                "domain": "mulms.mutah.edu.jo",
                "path": "/",
                "secure": True,
                "expires": None,
            }
        ],
        "sesskey": "sk_xyz",
        "user_id": "42",
        "full_name": "Student Name",
    }
    assert db.save_lms_session(999, state) is True

    # Verify stored ciphertext is encrypted (enc:v1:) and does not expose cookie value
    conn = sqlite3.connect(str(isolated_db))
    try:
        raw = conn.execute("SELECT encrypted_state FROM lms_sessions WHERE telegram_id=999").fetchone()[0]
    finally:
        conn.close()
    assert raw.startswith("enc:v1:")
    assert "sess_token_abc123" not in raw

    loaded = db.load_lms_session(999)
    assert loaded == state

    # delete_user must cleanly wipe persisted LMS session
    db.delete_user(999)
    assert db.load_lms_session(999) is None


def test_scraper_export_and_restore_filters_foreign_domains_and_wrong_user() -> None:
    s1 = LMSScraper("20240001", "pw")
    s1.session.cookies.set("MoodleSession", "valid_cookie", domain="mulms.mutah.edu.jo", path="/")
    s1.session.cookies.set("evil", "bad", domain="attacker.example.com", path="/")
    s1.sesskey = "sk123"
    s1.user_id = "77"
    s1.full_name = "Ali"
    s1.logged_in = True

    exported = s1.export_session_state()
    assert len(exported["cookies"]) == 1
    assert exported["cookies"][0]["name"] == "MoodleSession"

    # Wrong username must be rejected
    s_wrong = LMSScraper("99999999", "pw")
    assert s_wrong.restore_session_state(exported) is False
    assert s_wrong.logged_in is False

    # Matching username restores cookies and marks logged_in=True
    s2 = LMSScraper("20240001", "pw")
    assert s2.restore_session_state(exported) is True
    assert s2.logged_in is True
    assert s2.sesskey == "sk123"
    assert s2.user_id == "77"
    assert s2.session.cookies.get("MoodleSession") == "valid_cookie"

    # Foreign domain cookie injected into state must be ignored
    tampered = {
        "username": "20240001",
        "cookies": [{"name": "steal", "value": "x", "domain": "evil.com", "path": "/"}],
    }
    s3 = LMSScraper("20240001", "pw")
    assert s3.restore_session_state(tampered) is False
    assert s3.logged_in is False


def test_monitor_engine_restores_persisted_session_after_restart_without_relogin(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.save_user(999, "20240001", "secret_pw", "42", "Student Name")
    state = {
        "username": "20240001",
        "cookies": [{"name": "MoodleSession", "value": "warm_cookie", "domain": "mulms.mutah.edu.jo", "path": "/"}],
        "sesskey": "sk_warm",
        "user_id": "42",
        "full_name": "Student Name",
    }
    assert db.save_lms_session(999, state) is True

    login_calls = 0

    def _forbid_login(self):
        nonlocal login_calls
        login_calls += 1
        return True

    monkeypatch.setattr(LMSScraper, "login", _forbid_login)

    engine = MonitorEngine(MagicMock())
    user = db.get_user(999)
    assert user is not None

    async def _run():
        scraper, err = await engine._ensure_logged_in(user, force_fresh=False)
        assert err is None
        assert scraper is not None
        assert scraper.logged_in is True
        assert scraper.session.cookies.get("MoodleSession") == "warm_cookie"
        assert login_calls == 0

        # force_fresh=True must clear stale DB session and invoke login() once
        scraper2, err2 = await engine._ensure_logged_in(user, force_fresh=True)
        assert err2 is None
        assert scraper2 is not None
        assert login_calls == 1

    asyncio.run(_run())


def test_webapp_open_scraper_reuses_persisted_session_without_login(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.save_user(999, "20240001", "secret_pw", "42", "Student Name")
    db.save_lms_session(
        999,
        {
            "username": "20240001",
            "cookies": [{"name": "MoodleSession", "value": "web_cookie", "domain": "mulms.mutah.edu.jo", "path": "/"}],
            "sesskey": "sk_web",
        },
    )

    login_mock = MagicMock(return_value=True)
    monkeypatch.setattr(LMSScraper, "login", login_mock)

    user = db.get_user(999)
    scraper, auth_ok, restored = webapp_server._open_webapp_scraper(999, user)
    try:
        assert auth_ok is True
        assert restored is True
        assert login_mock.call_count == 0
        assert scraper.session.cookies.get("MoodleSession") == "web_cookie"
    finally:
        scraper.close()
