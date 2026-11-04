"""Encrypt settings secrets (API keys) in admin DB."""

import base64
import hashlib
import os

from cryptography.fernet import Fernet

_fernet: Fernet | None = None


def _key_bytes() -> bytes:
    key = os.getenv("ADMIN_SETTINGS_FERNET_KEY", "").strip()
    if not key:
        raw = hashlib.sha256(b"lambot-admin-settings-fernet-v1").digest()
        key = base64.urlsafe_b64encode(raw).decode()
    return key.encode("ascii")


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_key_bytes())
    return _fernet


def encrypt_secret(plain: str) -> str:
    return get_fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return get_fernet().decrypt(token.encode()).decode()
