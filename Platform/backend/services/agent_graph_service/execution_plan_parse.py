"""Разбор JSON плана исполнения (шаг execution_plan) и запасной набор задач."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from services.agent_graph_service.team_client import team_get_run

MAX_TASKS_PER_BATCH = int(os.getenv("AGENT_PLAN_MAX_TASKS_PER_ITER", "5"))


def _extract_first_json_object(raw: str) -> dict[str, Any] | None:
    """Пытается достать первый JSON-объект даже при префиксе/суффиксе текста."""
    text = (raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _idx = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_execution_plan_json(text: str) -> list[dict[str, Any]]:
    """Извлекает список задач из ответа LLM (JSON в тексте или в fenced block)."""
    data = _extract_first_json_object(text)
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return []
    out: list[dict[str, Any]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "Задача")[:512]
        action = str(t.get("action_kind") or t.get("action") or "noop")[:64].strip().lower()
        alias = {
            "ssh_exec": "ssh_run_command",
            "ssh_run": "ssh_run_command",
            "k8s_query": "kubernetes_query",
            "kubernetes": "kubernetes_query",
            "ansible_template": "ansible_run_template",
            "ansible_marketplace": "ansible_run_template",
        }
        action = alias.get(action, action)
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
    if MAX_TASKS_PER_BATCH > 0:
        return out[:MAX_TASKS_PER_BATCH]
    return out


def parse_execution_plan_payload(text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Возвращает (tasks, meta) для batch-планировщика."""
    data = _extract_first_json_object(text)
    if not isinstance(data, dict):
        return [], {"continue_planning": True, "done_reason": "invalid_json"}
    tasks = parse_execution_plan_json(text)
    meta = {
        "continue_planning": bool(data.get("continue_planning", False)),
        "goal_done": bool(data.get("goal_done", False)),
        "done_reason": str(data.get("done_reason") or "").strip()[:1000],
    }
    # Если нет задач и не указан continue_planning, считаем это завершением.
    if not tasks and "continue_planning" not in data:
        meta["continue_planning"] = False
        meta["goal_done"] = True
    return tasks, meta


def default_execution_tasks_if_empty(dev_context_summary: str) -> list[dict[str, Any]]:
    """Если LLM не вернул JSON — стараемся дать исполняемую задачу по доступным tool-таргетам."""
    ssh_target_id = ""
    k8s_target_id = ""
    try:
        parsed = json.loads(dev_context_summary)
        if isinstance(parsed, dict):
            ssh_tools = parsed.get("ssh_tools") if isinstance(parsed.get("ssh_tools"), dict) else {}
            ssh_list = ssh_tools.get("ssh_targets_for_tools") if isinstance(ssh_tools, dict) else []
            if isinstance(ssh_list, list) and ssh_list:
                first = ssh_list[0]
                if isinstance(first, dict):
                    ssh_target_id = str(first.get("target_id") or "").strip()
            k8s_list = parsed.get("kubernetes_clusters")
            if isinstance(k8s_list, list) and k8s_list:
                first_k = k8s_list[0]
                if isinstance(first_k, dict):
                    k8s_target_id = str(first_k.get("target_id") or "").strip()
    except Exception:
        pass

    if ssh_target_id:
        return [
            {
                "title": "SSH: проверка сервера и подготовка деплоя",
                "action_kind": "ssh_run_command",
                "payload": {
                    "target_id": ssh_target_id,
                    "commands": [
                        "set -e; uname -a; whoami; pwd",
                        "if command -v docker >/dev/null 2>&1; then docker --version; else echo docker_not_found; fi",
                    ],
                },
                "plan_summary": "Автовыбор: найден SSH target в контексте, запускаем реальные команды на сервере.",
            }
        ]
    if k8s_target_id:
        return [
            {
                "title": "Kubernetes: состояние кластера",
                "action_kind": "kubernetes_query",
                "payload": {"target_id": k8s_target_id, "operation": "nodes", "wide": True},
                "plan_summary": "Автовыбор: найден Kubernetes target в контексте.",
            }
        ]
    return [
        {
            "title": "Ожидание: нет исполнимых target в контексте",
            "action_kind": "noop",
            "payload": {},
            "plan_summary": "Нет SSH/Kubernetes target для автозапуска",
        }
    ]


def wait_for_ai_team_execution_tasks(
    run_id: str,
    timeout_sec: float = 900.0,
    task_ids: list[str] | None = None,
) -> bool:
    """Блокирует оркестратор, пока воркеры не завершат все задачи; False при таймауте."""
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        r = team_get_run(run_id)
        if not r:
            return True
        tasks = r.get("execution_tasks") or []
        if not tasks:
            return True
        if task_ids:
            wanted = {str(x) for x in task_ids}
            tasks = [x for x in tasks if str(x.get("id")) in wanted]
            if not tasks:
                return True
        if all(x.get("status") in ("done", "error") for x in tasks):
            return True
        time.sleep(2.0)
    return False


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
