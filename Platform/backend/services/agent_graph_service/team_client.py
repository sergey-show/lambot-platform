"""HTTP-клиент к admin-service: состояние AI-команды (Postgres)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
import httpx

ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")


def _headers() -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def team_create_run(project_id: str, user_message: str) -> dict[str, Any]:
    r = httpx.post(
        f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/runs",
        json={"project_id": project_id, "user_message": user_message},
        headers=_headers(),
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()


def team_get_run(run_id: str) -> dict[str, Any] | None:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/runs/{run_id}",
            headers=_headers(),
            timeout=30.0,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def team_patch_run(run_id: str, **fields: Any) -> dict[str, Any]:
    r = httpx.patch(
        f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/runs/{run_id}",
        json={k: v for k, v in fields.items() if v is not None},
        headers=_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def team_patch_step(
    run_id: str,
    step_key: str,
    *,
    status: str,
    output_md: str | None = None,
    structured: dict | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"status": status}
    if output_md is not None:
        body["output_md"] = output_md
    if structured is not None:
        body["structured"] = structured
    if error is not None:
        body["error"] = error
    if started_at is not None:
        body["started_at"] = started_at
    if finished_at is not None:
        body["finished_at"] = finished_at
    r = httpx.patch(
        f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/runs/{run_id}/steps/{step_key}",
        json=body,
        headers=_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def team_replace_execution_tasks(run_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    r = httpx.post(
        f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/runs/{run_id}/execution-tasks",
        json={"tasks": tasks},
        headers=_headers(),
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()


def team_append_execution_tasks(run_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    r = httpx.post(
        f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/runs/{run_id}/execution-tasks/append",
        json={"tasks": tasks},
        headers=_headers(),
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()


def team_patch_execution_task(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.patch(
        f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/execution-tasks/{task_id}",
        json=body,
        headers=_headers(),
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def team_latest_for_project(project_id: str) -> dict[str, Any] | None:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/projects/{project_id}/latest",
            headers=_headers(),
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("run")
    except Exception:
        return None
