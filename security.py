import os
import base64
import hashlib
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

def _get_cipher_suite() -> Fernet:
    """Generate or retrieve a Fernet cipher suite from environment or derive from Bot Token."""
    custom_key = os.getenv("ENCRYPTION_KEY", "").strip()
    if custom_key:
        try:
            # Validate if it's already a valid Fernet key
            key_bytes = custom_key.encode("utf-8")
            return Fernet(key_bytes)
        except Exception as e:
            logger.warning(f"ENCRYPTION_KEY is invalid, deriving key from it: {e}")
            derived = base64.urlsafe_b64encode(hashlib.sha256(custom_key.encode("utf-8")).digest())
            return Fernet(derived)

    # Fallback to deriving from TELEGRAM_BOT_TOKEN
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        # Ultimate fallback salt if no token configured yet
        token = "lms_monitor_default_secret_salt_2026"

    derived = base64.urlsafe_b64encode(hashlib.sha256(token.encode("utf-8")).digest())
    return Fernet(derived)

def encrypt_password(plain_password: str) -> str:
    """Encrypt a plain text password into a secure Fernet token string."""
    if not plain_password:
        return ""
    try:
        cipher = _get_cipher_suite()
        token = cipher.encrypt(plain_password.encode("utf-8"))
        return token.decode("utf-8")
    except Exception as e:
        logger.error(f"Error encrypting password: {e}")
        return plain_password

def decrypt_password(stored_password: str) -> str:
    """Decrypt a stored password. If it was stored in plain text, return it safely."""
    if not stored_password:
        return ""

    # Fernet tokens usually start with 'gAAAAA'
    if not stored_password.startswith("gAAAAA"):
        return stored_password

    try:
        cipher = _get_cipher_suite()
        decrypted = cipher.decrypt(stored_password.encode("utf-8"))
        return decrypted.decode("utf-8")
    except (InvalidToken, Exception) as e:
        logger.debug(f"Stored password could not be decrypted (likely legacy plain text): {e}")
        return stored_password

