"""Разбор JSON плана исполнения (шаг execution_plan) и запасной набор задач."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from services.agent_graph_service.team_client import team_get_run


def parse_execution_plan_json(text: str) -> list[dict[str, Any]]:
    """Извлекает список задач из ответа LLM (JSON в тексте или в fenced block)."""
    raw = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return []
    out: list[dict[str, Any]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "Задача")[:512]
        action = str(t.get("action_kind") or t.get("action") or "noop")[:64]
        payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
        notes = t.get("notes")
        plan_summary = str(notes)[:8000] if notes is not None else None
        out.append(
            {
                "title": title,
                "action_kind": action,
                "payload": payload,
                "plan_summary": plan_summary,
            }
        )
    return out


def default_execution_tasks_if_empty(dev_context_summary: str) -> list[dict[str, Any]]:
    """Если LLM не вернул JSON — одна реальная задача при наличии GitLab в контексте, иначе noop."""
    ctx = dev_context_summary.lower()
    if "gitlab" in ctx or "git lab" in ctx:
        return [
            {
                "title": "Репозиторий и CI в GitLab (agent-demo)",
                "action_kind": "adapter_agent_demo",
                "payload": {},
                "plan_summary": "Автовыбор: в контексте упомянут GitLab",
            }
        ]
    return [
        {
            "title": "Ожидание: нет GitLab в контексте",
            "action_kind": "noop",
            "payload": {},
            "plan_summary": "Нет интеграции для автозапуска",
        }
    ]


def wait_for_ai_team_execution_tasks(run_id: str, timeout_sec: float = 900.0) -> None:
    """Блокирует оркестратор, пока воркеры не завершат все задачи (или таймаут)."""
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        r = team_get_run(run_id)
        if not r:
            return
        tasks = r.get("execution_tasks") or []
        if not tasks:
            return
        if all(x.get("status") in ("done", "error") for x in tasks):
            return
        time.sleep(2.0)


def format_execution_tasks_markdown(run: dict) -> str:
    lines: list[str] = ["### Результаты исполнения (воркеры)", ""]
    tasks = run.get("execution_tasks") or []
    if not tasks:
        lines.append("_Задач исполнения нет._")
        return "\n".join(lines)
    for t in tasks:
        title = t.get("title", "—")
        kind = t.get("action_kind", "—")
        st = t.get("status", "—")
        lines.append(f"- **{title}** (`{kind}`) — **{st}**")
        if t.get("result") is not None:
            lines.append(f"  - результат: `{t.get('result')}`")
        if t.get("error"):
            lines.append(f"  - ошибка: {t.get('error')}")
    return "\n".join(lines)
