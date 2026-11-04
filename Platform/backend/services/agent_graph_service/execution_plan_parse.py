"""Parse execution plan JSON (execution_plan step) and a fallback task set."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from services.agent_graph_service.team_client import team_get_run

MAX_TASKS_PER_BATCH = int(os.getenv("AGENT_PLAN_MAX_TASKS_PER_ITER", "5"))

# action_kind values that agent-worker-service actually runs.
WORKER_ACTION_KINDS = frozenset(
    {
        "ssh_run_command",
        "kubernetes_query",
        "ansible_run_template",
        "noop",
        "adapter_agent_demo",
        "gitlab_pipeline",
    }
)

# Only LangGraph tools on *_plan steps — workers cannot run them.
LANGGRAPH_ONLY_ACTION_KINDS = frozenset(
    {
        "gitlab_create_repository",
        "repo_clone_or_sync",
        "repo_read_file",
        "repo_write_commit_push",
        "search_codebase",
        "gitlab_provision_status",
        "list_project_repos",
        "get_development_context",
        "tracker_create_epic",
        "tracker_create_task",
    }
)

_FAKE_ANSIBLE_TEMPLATE = re.compile(
    r"^(api_service|web_interface|auth_service|stock_market_service|observability_service)_template$",
    re.I,
)


def _dev_context_flags(dev_context_summary: str) -> tuple[bool, bool]:
    has_ssh = False
    has_k8s = False
    try:
        parsed = json.loads(dev_context_summary)
        if isinstance(parsed, dict):
            ssh_tools = parsed.get("ssh_tools") if isinstance(parsed.get("ssh_tools"), dict) else {}
            ssh_list = ssh_tools.get("ssh_targets_for_tools") if isinstance(ssh_tools, dict) else []
            has_ssh = isinstance(ssh_list, list) and len(ssh_list) > 0
            k8s_list = parsed.get("kubernetes_clusters")
            has_k8s = isinstance(k8s_list, list) and len(k8s_list) > 0
    except Exception:
        pass
    return has_ssh, has_k8s


def normalize_worker_execution_tasks(
    tasks: list[dict[str, Any]],
    dev_context_summary: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drops tasks the worker cannot run (GitLab tools, k8s without a cluster, fake ansible)."""
    has_ssh, has_k8s = _dev_context_flags(dev_context_summary)
    notes: list[str] = []
    out: list[dict[str, Any]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        action = str(t.get("action_kind") or "noop").strip().lower()
        title = str(t.get("title") or "Task")[:80]
        if action in LANGGRAPH_ONLY_ACTION_KINDS:
            notes.append(f"skipped `{action}` ({title}): LangGraph tools only, not the worker")
            continue
        if action not in WORKER_ACTION_KINDS:
            notes.append(f"skipped unknown `{action}` ({title})")
            continue
        if action == "kubernetes_query" and not has_k8s:
            notes.append(f"skipped kubernetes_query ({title}): no Kubernetes in the project")
            continue
        if action == "ansible_run_template":
            payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
            tid = str(payload.get("template_id") or "").strip()
            if tid and _FAKE_ANSIBLE_TEMPLATE.match(tid):
                notes.append(f"skipped ansible_run_template ({title}): invented template_id={tid!r}")
                continue
        if action == "ssh_run_command" and not has_ssh:
            notes.append(f"skipped ssh_run_command ({title}): no SSH target")
            continue
        out.append(t)
    if not out and has_ssh:
        notes.append("batch empty after filtering — inserted SSH smoke from context")
        out = default_execution_tasks_if_empty(dev_context_summary)
    return out, notes


def _extract_first_json_object(raw: str) -> dict[str, Any] | None:
    """Tries to extract the first JSON object even with text prefix/suffix."""
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
    """Extracts a task list from the LLM reply (JSON in text or a fenced block)."""
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
        title = str(t.get("title") or "Task")[:512]
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
    """Returns (tasks, meta) for the batch planner."""
    data = _extract_first_json_object(text)
    if not isinstance(data, dict):
        return [], {"continue_planning": True, "done_reason": "invalid_json"}
    tasks = parse_execution_plan_json(text)
    meta = {
        "continue_planning": bool(data.get("continue_planning", False)),
        "goal_done": bool(data.get("goal_done", False)),
        "done_reason": str(data.get("done_reason") or "").strip()[:1000],
    }
    # If there are no tasks and continue_planning is unset, treat as completion.
    if not tasks and "continue_planning" not in data:
        meta["continue_planning"] = False
        meta["goal_done"] = True
    return tasks, meta


def default_execution_tasks_if_empty(dev_context_summary: str) -> list[dict[str, Any]]:
    """If the LLM did not return JSON — try to provide an executable task from available tool targets."""
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
                "title": "SSH: server check and deploy prep",
                "action_kind": "ssh_run_command",
                "payload": {
                    "target_id": ssh_target_id,
                    "commands": [
                        "set -e; uname -a; whoami; pwd",
                        "if command -v docker >/dev/null 2>&1; then docker --version; else echo docker_not_found; fi",
                    ],
                },
                "plan_summary": "Auto-select: found SSH target in context; running real commands on the server.",
            }
        ]
    if k8s_target_id:
        return [
            {
                "title": "Kubernetes: cluster state",
                "action_kind": "kubernetes_query",
                "payload": {"target_id": k8s_target_id, "operation": "nodes", "wide": True},
                "plan_summary": "Auto-select: found Kubernetes target in context.",
            }
        ]
    return [
        {
            "title": "Waiting: no executable target in context",
            "action_kind": "noop",
            "payload": {},
            "plan_summary": "No SSH/Kubernetes target for auto-start",
        }
    ]


def wait_for_ai_team_execution_tasks(
    run_id: str,
    timeout_sec: float = 900.0,
    task_ids: list[str] | None = None,
) -> bool:
    """Blocks the orchestrator until workers finish all tasks; False on timeout."""
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
    lines: list[str] = ["### Execution results (workers)", ""]
    tasks = run.get("execution_tasks") or []
    if not tasks:
        lines.append("_No execution tasks._")
        return "\n".join(lines)
    for t in tasks:
        title = t.get("title", "—")
        kind = t.get("action_kind", "—")
        st = t.get("status", "—")
        lines.append(f"- **{title}** (`{kind}`) — **{st}**")
        if t.get("result") is not None:
            lines.append(f"- result: `{t.get('result')}`")
        if t.get("error"):
            lines.append(f"- error: {t.get('error')}")
    return "\n".join(lines)
