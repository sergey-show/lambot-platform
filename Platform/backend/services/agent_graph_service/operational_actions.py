"""Deterministic execution of ops requests (fallback if ReAct did not call tools)."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from uuid import UUID

import httpx

from services.agent_graph_service.dev_prompts import fetch_development_context_summary

ADAPTER_URL = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
OPS_PROBE_COMMAND = "uptime && free -m && df -h"


def command_for_operational_query(message: str) -> str:
    """SSH command for a typical user ops question (English keywords)."""
    t = (message or "").lower()
    if re.search(r"\bdisk\b|\bdf\b|free\s+space|disk\s+space|filesystem", t):
        return "df -h"
    if re.search(r"\bram\b|memory|swap", t):
        return "free -m"
    if re.search(r"uptime|load\s*average|cpu\s+load", t):
        return "uptime"
    return OPS_PROBE_COMMAND


def _parse_context(project_id: str) -> dict[str, Any] | None:
    raw = fetch_development_context_summary(project_id, max_chars=50000)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _pick_ssh_target_id(ctx: dict[str, Any]) -> str | None:
    ssh_tools = ctx.get("ssh_tools") if isinstance(ctx.get("ssh_tools"), dict) else {}
    if not ssh_tools.get("ssh_run_command_available"):
        return None
    for row in ssh_tools.get("ssh_targets_for_tools") or []:
        if isinstance(row, dict):
            tid = str(row.get("target_id") or "").strip()
            if tid:
                return tid
    for row in ctx.get("targets") or []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("target_kind") or row.get("kind") or "").strip().lower()
        if kind == "ssh":
            tid = str(row.get("target_id") or row.get("id") or "").strip()
            if tid:
                return tid
    return None


def _ssh_exec(project_id: str, target_id: str, command: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"command": command, "confirmed": False}
    url = f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{project_id}/targets/{target_id}/ssh-exec"
    r = httpx.post(url, json=payload, timeout=180.0)
    try:
        data = r.json()
    except Exception:
        data = {"http_status": r.status_code, "body": (r.text or "")[:8000]}
    if r.status_code == 200 and isinstance(data, dict) and data.get("needs_confirmation"):
        tok = str(data.get("confirmation_token") or "").strip()
        if tok:
            r2 = httpx.post(
                url,
                json={"command": command, "confirmed": True, "confirmation_token": tok},
                timeout=180.0,
            )
            try:
                return r2.json() if r2.status_code == 200 else {"http_status": r2.status_code, "body": r2.text}
            except Exception:
                return {"http_status": r2.status_code, "body": r2.text}
    if r.status_code != 200:
        return {"http_status": r.status_code, "error": data}
    return data if isinstance(data, dict) else {"result": data}


def execute_operational_ssh_probe(project_id: UUID, *, command: str = OPS_PROBE_COMMAND) -> tuple[bool, str]:
    """
  get_development_context + ssh_run_command without an LLM.
  Returns (ok, markdown for the devops_plan step).
    """
    pid = str(project_id)
    ctx = _parse_context(pid)
    if ctx is None:
        return (
            False,
            "Failed to get development-context (JSON). "
            "Check adapter-service and the project SSH target.",
        )
    target_id = _pick_ssh_target_id(ctx)
    if not target_id:
        return (
            False,
            "No available SSH target in the project (`ssh_run_command_available=false` or empty list). "
            "Add a server in project settings and retry.",
        )
    result = _ssh_exec(pid, target_id, command.strip() or OPS_PROBE_COMMAND)
    stdout = ""
    if isinstance(result, dict):
        stdout = str(result.get("stdout") or result.get("output") or result.get("result") or "").strip()
        if not stdout:
            stdout = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        stdout = str(result)
    lines = [
        "### Operational check (platform fallback)",
        "",
        f"- **target_id:** `{target_id}`",
        f"- **command:** `{command}`",
        "",
        "```",
        stdout[:12000],
        "```",
    ]
    if len(stdout) > 12000:
        lines.append("\n… output truncated")
    return True, "\n".join(lines)
