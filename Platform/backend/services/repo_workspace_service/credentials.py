from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx

ADAPTER_SERVICE_URL = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")


def fetch_gitlab_credentials(project_id: UUID) -> dict[str, Any]:
    r = httpx.get(
        f"{ADAPTER_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/gitlab-credentials",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=20.0,
    )
    if r.status_code == 404:
        raise RuntimeError("gitlab_credentials_missing")
    r.raise_for_status()
    return r.json()
