import base64
import hashlib
import os
import secrets

from cryptography.fernet import Fernet

_fernet_singleton: Fernet | None = None


def get_fernet() -> Fernet:
    global _fernet_singleton
    if _fernet_singleton is not None:
        return _fernet_singleton
    key = os.getenv("ADAPTER_FERNET_KEY", "").strip()
    if not key:
        # Deterministic dev key (set ADAPTER_FERNET_KEY in production)
        raw = hashlib.sha256(b"lambot-adapter-dev-fernet-v1").digest()
        key = base64.urlsafe_b64encode(raw).decode()
    _fernet_singleton = Fernet(key.encode("ascii"))
    return _fernet_singleton


def encrypt_secret(plain: str) -> str:
    return get_fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return get_fernet().decrypt(token.encode()).decode()


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_agent_token() -> str:
    return secrets.token_urlsafe(32)
