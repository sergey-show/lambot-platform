"""HMAC token for confirming a dangerous SSH command (stateless)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time


def _secret() -> bytes:
    return os.getenv("SSH_EXEC_CONFIRM_SECRET", "lambot-ssh-exec-confirm-change-in-prod").encode()


def issue_confirmation_token(project_id: str, target_id: str, command: str, ttl_sec: int = 600) -> str:
    exp = int(time.time()) + ttl_sec
    ch = hashlib.sha256(command.encode("utf-8")).hexdigest()
    msg = f"{exp}|{project_id}|{target_id}|{ch}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
    raw = f"{exp}|{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_confirmation_token(token: str, project_id: str, target_id: str, command: str) -> tuple[bool, str]:
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + pad).encode()).decode()
        exp_s, sig = raw.split("|", 1)
        exp = int(exp_s)
        if int(time.time()) > exp:
            return False, "confirmation expired (request again)"
        ch = hashlib.sha256(command.encode("utf-8")).hexdigest()
        msg = f"{exp}|{project_id}|{target_id}|{ch}"
        expect = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return False, "invalid token signature"
        return True, "ok"
    except Exception as e:
        return False, f"token: {e}"
