"""Definition of Done (DoD) gates: deploy / implementation / pm_summary without confirmed artifacts."""

from __future__ import annotations

from typing import Any

DOD_GATE_STEP_KEYS = frozenset({"implementation", "deploy", "pm_summary"})

EXECUTABLE_ACTION_KINDS = frozenset({"ssh_run_command", "kubernetes_query", "ansible_run_template"})

GITLAB_CODE_TOOLS = frozenset(
    {
        "repo_write_commit_push",
        "gitlab_create_repository",
        "repo_clone_or_sync",
    }
)
GITLAB_WRITE_TOOLS = frozenset({"repo_write_commit_push", "gitlab_create_repository"})
DEPLOY_TOOL_NAMES = frozenset({"ssh_run_command", "ansible_run_template"})


def _merge_role_evidence(
    role_execution_evidence: dict[str, dict[str, Any]],
    prev_steps: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = {k: v for k, v in role_execution_evidence.items() if isinstance(v, dict)}
    for s in prev_steps:
        if not isinstance(s, dict):
            continue
        key = str(s.get("step_key") or "").strip()
        structured = s.get("structured") if isinstance(s.get("structured"), dict) else {}
        ev = structured.get("role_execution_evidence")
        if key and isinstance(ev, dict):
            merged[key] = ev
    return merged


def _has_gitlab_code_artifact(tool_names: set[str], evidence: dict[str, dict[str, Any]]) -> bool:
    if "repo_write_commit_push" in tool_names:
        return True
    if "gitlab_create_repository" in tool_names and "repo_clone_or_sync" in tool_names:
        return int(_max_tool_results(evidence)) > 0
    return False


def _max_tool_results(evidence: dict[str, dict[str, Any]]) -> int:
    m = 0
    for ev in evidence.values():
        if isinstance(ev, dict):
            m = max(m, int(ev.get("tool_results_count") or 0))
    return m


def _has_deploy_artifact(
    tasks: list[Any],
    tool_names: set[str],
    evidence: dict[str, dict[str, Any]],
) -> bool:
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "") != "done":
            continue
        kind = str(t.get("action_kind") or "").strip().lower()
        if kind == "ansible_run_template":
            return True
        if kind == "ssh_run_command" and not _ssh_task_is_diagnostic_only(t):
            return True
        if kind == "kubernetes_query":
            payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
            op = str(payload.get("operation") or "").strip().lower()
            if op not in {"nodes", "pods", "events", "version", "namespaces", "cluster-info", "cluster_info"}:
                return True

    if DEPLOY_TOOL_NAMES & tool_names:
        for ev in evidence.values():
            if not isinstance(ev, dict):
                continue
            if not ev.get("has_execution_tool_call"):
                continue
            if int(ev.get("tool_results_count") or 0) > 0:
                return True
    return False


def _ssh_task_is_diagnostic_only(task: dict[str, Any]) -> bool:
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    commands: list[str] = []
    one = str(payload.get("command") or "").strip()
    if one:
        commands.append(one)
    many = payload.get("commands")
    if isinstance(many, list):
        commands.extend(str(x or "").strip() for x in many if str(x or "").strip())
    if not commands:
        return True
    action_markers = (
        "docker compose up",
        "docker-compose up",
        "docker run",
        "docker pull",
        "docker build",
        "kubectl apply",
        "helm upgrade",
        "ansible-playbook",
        "systemctl restart",
        "systemctl start",
        "git clone",
        "git pull",
        "npm run build",
        "npm run start",
        "pm2 restart",
        "nginx -s reload",
        "mkdir ",
        "tee ",
        "cat >",
        "scp ",
        "rsync ",
        "curl -",
    )
    diagnostic_markers = (
        "uname",
        "whoami",
        "pwd",
        "ls ",
        "docker --version",
        "kubectl version",
        "echo docker_not_found",
        "df -h",
    )
    has_action = any(any(m in c.lower() for m in action_markers) for c in commands)
    if has_action:
        return False
    return all(any(m in c.lower() for m in diagnostic_markers) for c in commands)
