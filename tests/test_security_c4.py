"""Tests for C4: Fail-Closed Fernet encryption (enc:v1:) and idempotent DB migration."""
import base64
import hashlib
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_INITIAL_TEST_KEY = Fernet.generate_key().decode("utf-8")
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = _INITIAL_TEST_KEY

import database  # noqa: E402
import security  # noqa: E402


def _legacy_encrypt_from_bot_token(plain: str, bot_token: str) -> str:
    """Reproduce the pre-C4 encryption derived from TELEGRAM_BOT_TOKEN."""
    derived = base64.urlsafe_b64encode(hashlib.sha256(bot_token.encode("utf-8")).digest())
    return Fernet(derived).encrypt(plain.encode("utf-8")).decode("utf-8")


def test_missing_encryption_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        security.encrypt_password("super_secret")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        security.decrypt_password("enc:v1:gAAAAABfake")


def test_empty_encryption_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "   ")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        security.encrypt_password("super_secret")


def test_invalid_encryption_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        security.encrypt_password("super_secret")


def test_round_trip_enc_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    plain = "P@ss w0rd! ١٢٣"
    encrypted = security.encrypt_password(plain)

    assert encrypted.startswith("enc:v1:")
    assert plain not in encrypted
    assert security.decrypt_password(encrypted) == plain

    assert security.encrypt_password("") == ""
    assert security.decrypt_password("") == ""


def test_encryption_independent_of_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ORIGINAL_TOKEN")

    encrypted = security.encrypt_password("my_lms_pass")
    assert encrypted.startswith("enc:v1:")

    # Rotating TELEGRAM_BOT_TOKEN must not break decryption of enc:v1 passwords
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:ROTATED_TOKEN")
    assert security.decrypt_password(encrypted) == "my_lms_pass"


def test_tampered_ciphertext_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    encrypted = security.encrypt_password("sensitive_password")
    # Flip the last character of the token to tamper with HMAC/payload
    last_char = "A" if encrypted[-1] != "A" else "B"
    tampered = encrypted[:-1] + last_char

    with pytest.raises(RuntimeError):
        security.decrypt_password(tampered)


def test_foreign_ciphertext_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("utf-8")
    foreign_key = Fernet.generate_key()
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    foreign_token = Fernet(foreign_key).encrypt(b"secret").decode("utf-8")

    # Foreign token wrapped with enc:v1: must raise (never return ciphertext)
    with pytest.raises(RuntimeError):
        security.decrypt_password("enc:v1:" + foreign_token)

    # Raw unprefixed foreign Fernet token must also fail closed
    with pytest.raises(RuntimeError):
        security.decrypt_password(foreign_token)


def test_password_migration_idempotent_and_preserves_undecryptable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_file = tmp_path / "migration_test.db"
    key = Fernet.generate_key().decode("utf-8")
    bot_token = "123456:MIGRATION_TEST"

    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DIRECT_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DIRECT_URL", "")

    # Create tables first
    database.init_db()

    legacy_encrypted = _legacy_encrypt_from_bot_token("legacy_bot_pass", bot_token)
    legacy_plaintext = "plain_pass_2026"
    already_v1 = security.encrypt_password("already_v1_pass")
    undecryptable_foreign = Fernet(Fernet.generate_key()).encrypt(b"unknown_secret").decode("utf-8")

    conn = sqlite3.connect(str(db_file))
    try:
        conn.executemany(
            "INSERT INTO users (telegram_id, lms_username, lms_password) VALUES (?, ?, ?)",
            [
                (101, "u101", legacy_encrypted),
                (102, "u102", legacy_plaintext),
                (103, "u103", already_v1),
                (104, "u104", undecryptable_foreign),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    def _read_raw_passwords() -> dict[int, str]:
        c = sqlite3.connect(str(db_file))
        try:
            rows = c.execute("SELECT telegram_id, lms_password FROM users").fetchall()
            return {int(tid): str(pwd) for tid, pwd in rows}
        finally:
            c.close()

    # First migration pass
    with caplog.at_level(logging.ERROR):
        database.init_db()

    after_first = _read_raw_passwords()
    assert after_first[101].startswith("enc:v1:")
    assert security.decrypt_password(after_first[101]) == "legacy_bot_pass"

    assert after_first[102].startswith("enc:v1:")
    assert security.decrypt_password(after_first[102]) == legacy_plaintext

    assert after_first[103] == already_v1
    assert security.decrypt_password(after_first[103]) == "already_v1_pass"

    # Undecryptable row must remain untouched and log ERROR with telegram_id (no secrets)
    assert after_first[104] == undecryptable_foreign
    error_messages = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert any("104" in msg for msg in error_messages)
    assert all(undecryptable_foreign not in msg and "unknown_secret" not in msg for msg in error_messages)

    # Second migration pass must be strictly idempotent
    database.init_db()
    after_second = _read_raw_passwords()
    assert after_second == after_first

    # Rotating bot token after migration must not break migrated user 101
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:NEW_BOT_TOKEN")
    user_101 = database.get_user(101)
    assert user_101 is not None
    assert user_101["lms_password"] == "legacy_bot_pass"


def test_password_migration_unwraps_double_wrapped_and_unprefixed_env_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "migration_unwrap.db"
    key = Fernet.generate_key().decode("utf-8")
    bot_token = "123456:MIGRATION_UNWRAP"

    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DIRECT_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DIRECT_URL", "")

    database.init_db()

    # Case 1: enc:v1: token wrapped again by a pre-C4 instance using TELEGRAM_BOT_TOKEN
    inner_v1 = security.encrypt_password("real_pass_1")
    double_wrapped = _legacy_encrypt_from_bot_token(inner_v1, bot_token)

    # Case 2: unprefixed Fernet token encrypted directly with ENCRYPTION_KEY (pre-C4 with ENCRYPTION_KEY set)
    unprefixed_env_key = Fernet(key.encode("utf-8")).encrypt(b"real_pass_2").decode("utf-8")

    conn = sqlite3.connect(str(db_file))
    try:
        conn.executemany(
            "INSERT INTO users (telegram_id, lms_username, lms_password) VALUES (?, ?, ?)",
            [
                (201, "u201", double_wrapped),
                (202, "u202", unprefixed_env_key),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    database.init_db()

    user_201 = database.get_user(201)
    assert user_201 is not None
    assert user_201["lms_password"] == "real_pass_1"

    user_202 = database.get_user(202)
    assert user_202 is not None
    assert user_202["lms_password"] == "real_pass_2"
