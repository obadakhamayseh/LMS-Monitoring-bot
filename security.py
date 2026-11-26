"""Password encryption utilities (C4: Fail-Closed Fernet with enc:v1: prefix)."""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENC_V1_PREFIX: str = "enc:v1:"
_FERNET_MIN_DECODED_BYTES: int = 57  # 1 version + 8 timestamp + 16 IV + 32 HMAC


def _get_cipher_suite() -> Fernet:
    """Return a Fernet cipher built strictly from the ENCRYPTION_KEY env var (Fail-Closed)."""
    raw_key = os.getenv("ENCRYPTION_KEY", "").strip()
    if not raw_key:
        raise RuntimeError(
            "ENCRYPTION_KEY environment variable is required and must be a valid Fernet key. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(raw_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "ENCRYPTION_KEY is invalid: expected a 32-byte url-safe base64-encoded Fernet key."
        ) from exc


def is_encrypted_v1(stored_password: str) -> bool:
    """Return True if stored_password already uses the enc:v1: ciphertext format."""
    return bool(stored_password) and stored_password.startswith(ENC_V1_PREFIX)


def is_legacy_fernet_token(stored_password: str) -> bool:
    """Return True if stored_password looks like a legacy unprefixed Fernet token."""
    if not stored_password or stored_password.startswith(ENC_V1_PREFIX):
        return False
    if not stored_password.startswith("gAAAAA"):
        return False
    try:
        decoded = base64.urlsafe_b64decode(stored_password.encode("ascii"))
    except Exception:
        return False
    return len(decoded) >= _FERNET_MIN_DECODED_BYTES and decoded[0] == 0x80


def encrypt_password(plain_password: str) -> str:
    """Encrypt a plaintext password into an enc:v1: Fernet token (Fail-Closed)."""
    if not plain_password:
        return ""
    cipher = _get_cipher_suite()
    try:
        token = cipher.encrypt(plain_password.encode("utf-8")).decode("utf-8")
        return f"{ENC_V1_PREFIX}{token}"
    except Exception as exc:
        logger.error("Failed to encrypt password: %s", exc)
        raise RuntimeError("Failed to encrypt password.") from exc


def decrypt_password(stored_password: str) -> str:
    """Decrypt an enc:v1: stored password (Fail-Closed: never returns ciphertext or plaintext)."""
    if not stored_password:
        return ""
    cipher = _get_cipher_suite()
    if not stored_password.startswith(ENC_V1_PREFIX):
        logger.error("Stored password does not have expected %s prefix.", ENC_V1_PREFIX)
        raise RuntimeError(
            f"Stored password is not in expected {ENC_V1_PREFIX} format; run DB migration first."
        )
    token_part = stored_password[len(ENC_V1_PREFIX):]
    try:
        return cipher.decrypt(token_part.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        logger.error("Failed to decrypt stored password (invalid/tampered token or wrong key).")
        raise RuntimeError(
            "Failed to decrypt stored password: ciphertext is invalid/tampered or ENCRYPTION_KEY does not match."
        ) from exc


def decrypt_legacy_password(stored_token: str) -> str:
    """Decrypt a pre-C4 Fernet token derived from TELEGRAM_BOT_TOKEN.

    Used ONLY by the one-time database migration in database.py.
    """
    if not stored_token:
        return ""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN environment variable is required to decrypt legacy passwords."
        )
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(bot_token.encode("utf-8")).digest())
    try:
        return Fernet(derived_key).decrypt(stored_token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError("Failed to decrypt legacy password with TELEGRAM_BOT_TOKEN key.") from exc
