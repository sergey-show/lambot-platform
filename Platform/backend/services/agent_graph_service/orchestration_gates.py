"""Definition of Done (DoD) gates: deploy / implementation / pm_summary без подтверждённых артефактов."""

from __future__ import annotations

from typing import Any

from services.agent_graph_service.gitlab_actions import gitlab_repo_create_intent
from services.agent_graph_service.orchestration_intents import site_build_intent

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


def dod_profile_for_message(user_message: str) -> str | None:
    """Профиль DoD по запросу пользователя; None — не блокируем финальные шаги."""
    msg = (user_message or "").strip()
    if not msg:
        return None
    if site_build_intent(msg):
        return "site_mvp"
    if gitlab_repo_create_intent(msg):
        return "gitlab_repo"
    if _infra_change_intent(msg):
        return "infra_change"
    return None


def _infra_change_intent(text: str) -> bool:
    t = (text or "").lower()
    markers = (
        "задеплой",
        "разверни",
        "развернуть",
        "деплой",
        "deploy",
        "на сервер",
        "ssh",
        "docker run",
        "исправь на сервере",
        "поменяй на сервере",
        "обнови на сервере",
    )
    return any(m in t for m in markers)


def dod_applies_to_step(step_key: str, user_message: str) -> bool:
    return step_key in DOD_GATE_STEP_KEYS and dod_profile_for_message(user_message) is not None


def dod_gate_issues(
    *,
    step_key: str,
    user_message: str,
    run_obj: dict[str, Any],
    role_execution_evidence: dict[str, dict[str, Any]],
    prev_steps: list[dict[str, Any]],
) -> list[str]:
    """Список нарушений DoD; пустой — gate пройден."""
    if step_key not in DOD_GATE_STEP_KEYS:
        return []
    profile = dod_profile_for_message(user_message)
    if not profile:
        return []

    evidence = _merge_role_evidence(role_execution_evidence, prev_steps)
    tool_names = _all_tool_names(evidence)
    tasks = run_obj.get("execution_tasks") if isinstance(run_obj.get("execution_tasks"), list) else []

    gitlab_ok = _has_gitlab_code_artifact(tool_names, evidence)
    deploy_ok = _has_deploy_artifact(tasks, tool_names, evidence)

    issues: list[str] = []
    if profile == "site_mvp":
        if not gitlab_ok:
            issues.append(
                "DoD (сайт/MVP): нет подтверждения кода в GitLab "
                "(нужен tool repo_write_commit_push или gitlab_create_repository + работа с репозиторием)."
            )
        if not deploy_ok:
            issues.append(
                "DoD (сайт/MVP): нет подтверждения деплоя на сервер "
                "(SSH/ansible с развёртыванием, не только uname/df/docker --version)."
            )
    elif profile == "gitlab_repo":
        if not gitlab_ok:
            issues.append(
                "DoD (репозиторий): нет подтверждения создания репозитория или коммита в GitLab."
            )
    elif profile == "infra_change":
        if not (deploy_ok or gitlab_ok):
            issues.append(
                "DoD (изменение инфраструктуры): нет подтверждённых действий "
                "(SSH/k8s/ansible или GitLab commit)."
            )

    return issues


def dod_gate_user_questions(profile: str | None, issues: list[str]) -> list[str]:
    if not issues:
        return []
    base = (
        "Критерии готовности (DoD) не выполнены — нельзя считать деплой/реализацию успешными. "
        "Нужно выполнить execution_plan / инструменты (GitLab, SSH), затем продолжить run."
    )
    if profile == "site_mvp":
        return [
            base,
            "Для сайта: создайте репозиторий и закоммитьте файлы (repo_write_commit_push), "
            "затем разверните на SSH-target (docker/nginx или аналог).",
        ]
    return [base]


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


def _all_tool_names(evidence: dict[str, dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for ev in evidence.values():
        if not isinstance(ev, dict):
            continue
        raw = ev.get("tool_names")
        if isinstance(raw, list):
            for n in raw:
                s = str(n or "").strip()
                if s:
                    names.add(s)
    return names


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
