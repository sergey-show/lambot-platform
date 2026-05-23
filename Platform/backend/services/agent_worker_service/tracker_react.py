"""ReAct для задач трекера: вызов agent-graph /invoke и проверка DoD по инструментам."""

from __future__ import annotations

import os
from typing import Any

from services.agent_graph_service.orchestration_intents import (
    extract_tool_names_from_assistant_content,
    looks_like_fake_tool_call_text,
)

EXECUTION_TOOL_NAMES = frozenset(
    {"ssh_run_command", "kubernetes_query", "ansible_run_template"}
)
PLANNING_TOOL_NAMES = frozenset(
    {
        "get_development_context",
        "gitlab_provision_status",
        "gitlab_create_repository",
        "repo_clone_or_sync",
        "repo_write_commit_push",
        "list_project_repos",
        "tracker_create_task",
        "tracker_create_epic",
        "upsert_project_document",
    }
)

TRACKER_REACT_SUFFIX = (
    "\n\n---\n"
    "Исполни задачу через function calling платформы (LangGraph ReAct), не текстовую инструкцию.\n"
    "1) get_development_context — узнай SSH targets и доступные инструменты.\n"
    "2) Для фактов с сервера/кластера — ssh_run_command / kubernetes_query / ansible_run_template.\n"
    "3) Для GitLab/wiki/трекера — соответствующие инструменты.\n"
    "Ответ пользователю — только по результатам инструментов; не предлагай «запустите uname вручную»."
)


def tracker_use_react() -> bool:
    if not os.getenv("AGENT_WORKER_USE_LLM", "0").lower() in ("1", "true", "yes"):
        return False
    return os.getenv("TRACKER_USE_REACT", "1").strip().lower() in ("1", "true", "yes")


def tracker_require_tools() -> bool:
    return os.getenv("TRACKER_REQUIRE_TOOLS", "1").strip().lower() in ("1", "true", "yes")


def tracker_react_timeout_sec() -> float:
    raw = os.getenv("TRACKER_REACT_TIMEOUT_SEC", "600").strip()
    try:
        n = float(raw)
    except ValueError:
        return 600.0
    return max(30.0, min(n, 1800.0))


def augment_tracker_prompt(base: str) -> str:
    b = (base or "").strip()
    if TRACKER_REACT_SUFFIX.strip() in b:
        return b
    return b + TRACKER_REACT_SUFFIX


def extract_final_text(graph_result: dict[str, Any]) -> str:
    final = str(graph_result.get("final_message") or "").strip()
    if final:
        return final
    for msg in reversed(graph_result.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    t = str(part.get("text") or "").strip()
                    if t:
                        parts.append(t)
            merged = "".join(parts).strip()
            if merged:
                return merged
    return ""


def collect_react_evidence(graph_result: dict[str, Any]) -> dict[str, Any]:
    messages = graph_result.get("messages") if isinstance(graph_result.get("messages"), list) else []
    tool_names: list[str] = []
    tool_calls_count = 0
    tool_results_count = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        t = str(m.get("type") or "").strip().lower()
        if t == "tool":
            tool_results_count += 1
        tc = m.get("tool_calls")
        if isinstance(tc, list):
            for item in tc:
                if not isinstance(item, dict):
                    continue
                nm = str(item.get("name") or "").strip()
                if not nm:
                    continue
                tool_calls_count += 1
                tool_names.append(nm)
        if t in ("ai", "aimessage", "assistant"):
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                for nm in extract_tool_names_from_assistant_content(content):
                    tool_calls_count += 1
                    tool_names.append(nm)
    unique_names = sorted({x for x in tool_names if x})
    return {
        "tool_calls_count": tool_calls_count,
        "tool_results_count": tool_results_count,
        "tool_names": unique_names,
        "has_execution_tool_call": any(x in EXECUTION_TOOL_NAMES for x in unique_names),
        "has_planning_tool_call": any(x in PLANNING_TOOL_NAMES for x in unique_names),
    }


def build_tracker_result(
    *,
    output: str,
    evidence: dict[str, Any],
    title: str,
    role: str,
    execution_id: str,
    require_tools: bool,
) -> tuple[dict[str, Any], bool]:
    """
    Собирает result_inline для admin и флаг успеха (True = SUCCEEDED).
  """
    tools_ok = int(evidence.get("tool_results_count") or 0) > 0 or bool(
        evidence.get("has_execution_tool_call")
    ) or int(evidence.get("tool_calls_count") or 0) > 0
    fake_tools = looks_like_fake_tool_call_text(output) and not tools_ok
    has_output = bool((output or "").strip())

    if not has_output:
        dod_status = "failed"
        dod_done = False
        ok = False
        work_done = ["ReAct завершился без итогового текста"]
    elif require_tools and not tools_ok:
        dod_status = "tools_missing"
        dod_done = False
        ok = False
        work_done = [
            "ReAct вернул текст без вызова инструментов платформы",
            "Для задач трекера нужны проверяемые факты (SSH/GitLab/контекст)",
        ]
    elif fake_tools:
        dod_status = "fake_tool_text"
        dod_done = False
        ok = False
        work_done = [
            "Модель описала инструменты текстом (JSON в ответе), но не выполнила function calling",
        ]
    else:
        dod_status = "tool_verified" if tools_ok else "text_only"
        dod_done = has_output and (tools_ok or not require_tools)
        ok = dod_done
        work_done = [
            "Задача выполнена через agent-graph ReAct (/invoke)",
            f"Инструменты: {', '.join(evidence.get('tool_names') or []) or '—'}",
        ]

    result: dict[str, Any] = {
        "mode": "tracker",
        "engine": "agent_graph_react",
        "dry_run": False,
        "output": output,
        "commentary": (output or "")[:1200],
        "react_evidence": evidence,
        "work_done": work_done,
        "dod": {
            "status": dod_status,
            "done": dod_done,
            "criteria": [
                "Есть итоговый ответ по задаче",
                "Есть хотя бы один вызов инструмента платформы с результатом",
            ],
            "notes": (
                "DoD по факту tool-calling (не self_reported). "
                f"execution_id={execution_id}"
            ),
        },
        "task_context": {"title": title, "assignee_role": role},
    }
    return result, ok
