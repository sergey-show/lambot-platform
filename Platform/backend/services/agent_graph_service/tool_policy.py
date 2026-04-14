"""Загрузка конфигурации инструментов из admin-service (вкл/выкл + HTTP tools)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")


def fetch_tool_policy() -> dict[str, Any]:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/agent-graph-tools",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=5.0,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {
        "builtin": {
            "http_request": True,
            "browser_session": True,
            "ansible_run_template": True,
            "get_project_docs_context": True,
            "upsert_project_document": True,
            "list_project_documents": True,
        },
        "http_tools": [],
    }
