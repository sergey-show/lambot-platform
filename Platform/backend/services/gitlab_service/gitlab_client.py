"""HTTP-клиент GitLab API v4 (платформенный admin token)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("gitlab_client")

GITLAB_URL = (os.getenv("GITLAB_URL") or "").strip().rstrip("/")
GITLAB_ADMIN_TOKEN = (os.getenv("GITLAB_ADMIN_TOKEN") or "").strip()
GITLAB_SSL_VERIFY = os.getenv("GITLAB_SSL_VERIFY", "1").strip().lower() in ("1", "true", "yes")
GITLAB_ROOT_GROUP_ID = os.getenv("GITLAB_ROOT_GROUP_ID", "").strip()
GITLAB_ROOT_GROUP_PATH = (os.getenv("GITLAB_ROOT_GROUP_PATH") or "lambot").strip().strip("/")
TOKEN_EXPIRE_DAYS = int(os.getenv("GITLAB_GROUP_ACCESS_TOKEN_EXPIRE_DAYS", "365"))


class GitlabClientError(Exception):
    pass


def _api_base() -> str:
    if not GITLAB_URL:
        raise GitlabClientError("GITLAB_URL is not configured")
    return f"{GITLAB_URL}/api/v4"


def _headers() -> dict[str, str]:
    if not GITLAB_ADMIN_TOKEN:
        raise GitlabClientError("GITLAB_ADMIN_TOKEN is not configured")
    return {"PRIVATE-TOKEN": GITLAB_ADMIN_TOKEN, "Content-Type": "application/json"}


def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> httpx.Response:
    url = f"{_api_base()}{path}"
    with httpx.Client(timeout=120.0, verify=GITLAB_SSL_VERIFY) as client:
        return client.request(method, url, headers=_headers(), json=json, params=params)


def resolve_root_group_id() -> int:
    if GITLAB_ROOT_GROUP_ID.isdigit():
        return int(GITLAB_ROOT_GROUP_ID)
    enc = quote(GITLAB_ROOT_GROUP_PATH, safe="")
    r = _request("GET", f"/groups/{enc}")
    if r.status_code != 200:
        raise GitlabClientError(f"root group not found: {GITLAB_ROOT_GROUP_PATH} ({r.status_code})")
    gid = r.json().get("id")
    if not isinstance(gid, int):
        raise GitlabClientError("invalid root group response")
    return gid


def find_group_by_path(full_path: str) -> dict[str, Any] | None:
    enc = quote(full_path, safe="")
    r = _request("GET", f"/groups/{enc}")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    raise GitlabClientError(f"find_group failed: {r.status_code} {r.text[:500]}")


def create_subgroup(*, parent_id: int, name: str, path: str) -> dict[str, Any]:
    payload = {
        "name": name[:255],
        "path": path[:120],
        "parent_id": parent_id,
        "visibility": "private",
    }
    r = _request("POST", "/groups", json=payload)
    if r.status_code in (200, 201):
        return r.json()
    if r.status_code == 400 and "already been taken" in (r.text or "").lower():
        full = f"{GITLAB_ROOT_GROUP_PATH}/{path}".strip("/")
        existing = find_group_by_path(full)
        if existing:
            return existing
    raise GitlabClientError(f"create_subgroup failed: {r.status_code} {r.text[:800]}")


def create_group_access_token(*, group_id: int, name: str = "lambot-platform-agent") -> tuple[str, dict[str, Any]]:
    expires = (datetime.now(timezone.utc) + timedelta(days=max(1, TOKEN_EXPIRE_DAYS))).strftime("%Y-%m-%d")
    payload = {
        "name": name,
        "scopes": ["api", "read_api", "read_repository", "write_repository"],
        "access_level": 40,
        "expires_at": expires,
    }
    r = _request("POST", f"/groups/{group_id}/access_tokens", json=payload)
    if r.status_code not in (200, 201):
        raise GitlabClientError(f"create_group_access_token failed: {r.status_code} {r.text[:800]}")
    body = r.json()
    token = (body.get("token") or "").strip()
    if not token:
        raise GitlabClientError("group access token missing in response")
    return token, body


def create_project_in_group(
    *,
    group_id: int,
    name: str,
    path: str,
    description: str = "",
    initialize_with_readme: bool = False,
) -> dict[str, Any]:
    payload = {
        "name": name[:255],
        "path": path[:120],
        "namespace_id": group_id,
        "visibility": "private",
        "description": (description or "")[:4000],
        "initialize_with_readme": initialize_with_readme,
    }
    r = _request("POST", "/projects", json=payload)
    if r.status_code in (200, 201):
        return r.json()
    if r.status_code == 400 and "already been taken" in (r.text or "").lower():
        full_path = payload.get("path_with_namespace")
        if full_path:
            enc = quote(str(full_path), safe="")
            gr = _request("GET", f"/projects/{enc}")
            if gr.status_code == 200:
                return gr.json()
    raise GitlabClientError(f"create_project failed: {r.status_code} {r.text[:800]}")
