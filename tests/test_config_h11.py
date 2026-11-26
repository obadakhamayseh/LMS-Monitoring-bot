"""Tests for H11: OWNER_TELEGRAM_ID validation and removal of hardcoded default ID."""
import importlib
import os
import subprocess
from pathlib import Path

import dotenv
import pytest
from cryptography.fernet import Fernet

_INITIAL_TEST_KEY = Fernet.generate_key().decode("utf-8")
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = _INITIAL_TEST_KEY

import config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
# Constructed dynamically so this test file itself never contains the raw literal ID
_OLD_HARDCODED_OWNER_ID = "".join(["933", "343", "496"])
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".db", ".sqlite", ".pdf"}


@pytest.fixture(autouse=True)
def _restore_valid_config(monkeypatch: pytest.MonkeyPatch):
    """Prevent reload tests from reading the real .env and restore valid config afterwards."""
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    yield
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "1")
    importlib.reload(config)


def test_owner_telegram_id_missing_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OWNER_TELEGRAM_ID", raising=False)
    with pytest.raises(RuntimeError, match="OWNER_TELEGRAM_ID"):
        importlib.reload(config)


@pytest.mark.parametrize("bad_value", ["", "   ", "abc", "12.34", "0", "-5"])
def test_owner_telegram_id_invalid_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, bad_value: str
) -> None:
    monkeypatch.setenv("OWNER_TELEGRAM_ID", bad_value)
    with pytest.raises(RuntimeError, match="OWNER_TELEGRAM_ID"):
        importlib.reload(config)


def test_owner_telegram_id_valid_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "424242")
    importlib.reload(config)
    assert config.OWNER_TELEGRAM_ID == 424242


def test_repo_has_no_old_hardcoded_owner_id() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    candidate_files = sorted(
        {
            line.strip()
            for line in (tracked.stdout.splitlines() + untracked.stdout.splitlines())
            if line.strip()
        }
    )
    excluded_dirs = {".venv", ".git", "__pycache__"}
    offenders: list[str] = []
    for rel_path in candidate_files:
        path = REPO_ROOT / rel_path
        if (
            not path.is_file()
            or path.name in {".env", "bot.log"}
            or any(part in excluded_dirs for part in path.parts)
            or path.suffix.lower() in _BINARY_SUFFIXES
        ):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if _OLD_HARDCODED_OWNER_ID in content:
            offenders.append(rel_path)

    assert offenders == [], f"Found hardcoded legacy owner ID in repo files: {offenders}"


def test_env_example_and_render_yaml_placeholders() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "OWNER_TELEGRAM_ID=" in env_example
    assert "ENCRYPTION_KEY=" in env_example
    assert "WEBAPP_URL=" in env_example
    assert _OLD_HARDCODED_OWNER_ID not in env_example

    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "key: OWNER_TELEGRAM_ID" in render_yaml
    assert "key: ENCRYPTION_KEY" in render_yaml
    assert _OLD_HARDCODED_OWNER_ID not in render_yaml
