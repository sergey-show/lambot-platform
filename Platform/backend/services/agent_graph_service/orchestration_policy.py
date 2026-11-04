"""Unified routing and DoD policy: decisions from intake/steps, not keywords."""

from __future__ import annotations

from typing import Any

from services.admin_service.ai_team_service import DEFAULT_PIPELINE
from services.agent_graph_service.orchestration_gates import (
    DOD_GATE_STEP_KEYS,
    _has_deploy_artifact,
    _has_gitlab_code_artifact,
    _merge_role_evidence,
    _ssh_task_is_diagnostic_only,
)

ALL_STEP_KEYS: list[str] = [s[0] for s in DEFAULT_PIPELINE]

EXECUTION_CAPABLE_SPECIALIST_STEPS = frozenset({"devops_plan", "backend_plan", "frontend_plan"})

DIRECT_TAIL_WHEN_LIVE = (
    "execution_plan",
    "implementation",
    "deploy",
    "qa_validation",
    "pm_summary",
)


def live_execution_from_route(route: dict[str, Any] | None) -> bool:
    if not route or not isinstance(route, dict):
        return False
    raw = route.get("live_execution")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return False


def live_execution_from_step_envelope(envelope: dict[str, Any] | None) -> bool:
    if not envelope or not isinstance(envelope, dict):
        return False
    raw = envelope.get("live_execution")
    if isinstance(raw, bool):
        return raw
    return False


def merge_live_execution_flag(
    current: bool,
    route: dict[str, Any] | None = None,
    *,
    step_envelope: dict[str, Any] | None = None,
) -> bool:
    if current:
        return True
    if live_execution_from_route(route):
        return True
    if live_execution_from_step_envelope(step_envelope):
        return True
    return False


def direct_skip_keys(specialist_step_key: str, *, live_execution: bool) -> list[str]:
    keep = {specialist_step_key, "intake"}
    if live_execution:
        keep.update(DIRECT_TAIL_WHEN_LIVE)
    else:
        keep.add("pm_summary")
    return [k for k in ALL_STEP_KEYS if k not in keep]


def direct_use_execution_tail(specialist_step_key: str, route: dict[str, Any] | None) -> bool:
    return (
        specialist_step_key in EXECUTION_CAPABLE_SPECIALIST_STEPS
        and live_execution_from_route(route)
    )


def dod_applies_to_step(step_key: str, *, role_execution_mode: bool) -> bool:
    return step_key in DOD_GATE_STEP_KEYS and role_execution_mode


def execution_outcome_satisfied(
    run_obj: dict[str, Any],
    role_execution_evidence: dict[str, dict[str, Any]],
    prev_steps: list[dict[str, Any]],
) -> bool:
    """
    DoD for live_execution: a deploy/code artifact or successfully finished consultative SSH tasks.
    """
    evidence = _merge_role_evidence(role_execution_evidence, prev_steps)
    tool_names: set[str] = set()
    for ev in evidence.values():
        if not isinstance(ev, dict):
            continue
        raw = ev.get("tool_names")
        if isinstance(raw, list):
            for n in raw:
                s = str(n or "").strip()
                if s:
                    tool_names.add(s)

    if _has_gitlab_code_artifact(tool_names, evidence):
        return True
    if _has_deploy_artifact(
        run_obj.get("execution_tasks") if isinstance(run_obj.get("execution_tasks"), list) else [],
        tool_names,
        evidence,
    ):
        return True

    tasks = run_obj.get("execution_tasks") if isinstance(run_obj.get("execution_tasks"), list) else []
    done = [t for t in tasks if isinstance(t, dict) and str(t.get("status") or "") == "done"]
    if not done:
        return False
    if all(_ssh_task_is_diagnostic_only(t) for t in done):
        return True
    return False


def dod_gate_issues_for_run(
    *,
    step_key: str,
    role_execution_mode: bool,
    run_obj: dict[str, Any],
    role_execution_evidence: dict[str, dict[str, Any]],
    prev_steps: list[dict[str, Any]],
) -> list[str]:
    if not dod_applies_to_step(step_key, role_execution_mode=role_execution_mode):
        return []
    if execution_outcome_satisfied(run_obj, role_execution_evidence, prev_steps):
        return []
    return [
        "No confirmed execution result: successful worker tasks are required "
        "(SSH/k8s/ansible changes or a GitLab commit), or a completed SSH diagnostic."
    ]


def dod_gate_user_questions(issues: list[str]) -> list[str]:
    if not issues:
        return []
    return [
        "Readiness criteria were not met. Clarify the request, retry the step with tools, "
        "or confirm manual execution.",
        issues[0],
    ]
