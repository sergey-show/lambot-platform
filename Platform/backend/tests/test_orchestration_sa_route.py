"""Маршрутизация SA и слои DAG."""

import json

from services.agent_graph_service.orchestration_intents import (
    extract_tool_names_from_assistant_content,
    formal_spec_intent,
    looks_like_fake_tool_call_text,
    looks_like_invalid_architect_output,
    user_requests_tool_execution_approval,
)
from services.agent_graph_service.orchestration_policy import (
    direct_skip_keys,
    direct_use_execution_tail,
    dod_applies_to_step,
    live_execution_from_route,
)
from services.agent_graph_service.execution_plan_parse import normalize_worker_execution_tasks
from services.agent_graph_service.orchestration_route import (
    active_dag_layers,
    enrich_route_with_sa_if_needed,
    heuristic_orchestration_route,
    parse_orchestration_route,
    route_includes_sa,
)


def test_formal_spec_intent_positive():
    assert formal_spec_intent("Нужно ТЗ и глоссарий для интеграции с CRM")
    assert formal_spec_intent("Создай эпики и задачи в трекере для агентов")


def test_formal_spec_intent_negative_site():
    assert not formal_spec_intent("Сделай простой лендинг с формой")


def test_heuristic_includes_sa():
    hr = heuristic_orchestration_route("Подготовь формальную спецификацию требований и traceability")
    assert hr is not None
    assert "sa" in hr.get("roles", [])
    assert hr.get("mode") == "full"
    assert hr.get("live_execution") is False


def test_active_dag_layers_without_sa():
    layers = active_dag_layers({"mode": "full", "roles": ["pm", "architect"]})
    assert ("sa",) not in layers
    assert layers[0] == ("pm",)


def test_active_dag_layers_with_sa():
    layers = active_dag_layers({"mode": "full", "roles": ["sa", "pm"]})
    assert layers[0] == ("sa",)
    assert route_includes_sa({"roles": ["sa"]})


def test_route_includes_sa_none_roles():
    assert route_includes_sa({}) is False
    assert route_includes_sa({"roles": None}) is False
    assert route_includes_sa(None) is False


def test_enrich_route_no_keyword_override():
    route_in = {"mode": "direct", "roles": ["pm"], "primary_role": "pm", "live_execution": False}
    out = enrich_route_with_sa_if_needed(route_in, "Нужен глоссарий")
    assert out == route_in


def test_parse_live_execution_from_intake_json():
    raw = json.dumps(
        {
            "orchestration_route": {
                "mode": "direct",
                "roles": ["devops"],
                "primary_role": "devops",
                "live_execution": False,
                "reason": "ops question",
            }
        }
    )
    route = parse_orchestration_route(raw)
    assert route["mode"] == "direct"
    assert route["live_execution"] is False
    assert not live_execution_from_route(route)


def test_direct_skip_keys_consultative():
    skip = direct_skip_keys("devops_plan", live_execution=False)
    assert "devops_plan" not in skip
    assert "pm_summary" not in skip
    assert "execution_plan" in skip


def test_direct_use_execution_tail_requires_live_flag():
    route = {"mode": "direct", "primary_role": "devops", "live_execution": True}
    assert direct_use_execution_tail("devops_plan", route)
    route["live_execution"] = False
    assert not direct_use_execution_tail("devops_plan", route)


def test_dod_not_applied_without_live_execution():
    assert not dod_applies_to_step("pm_summary", role_execution_mode=False)


def test_looks_like_fake_tool_call_markdown_fence():
    text = '```json\n{\n  "name": "ssh_run_command",\n  "arguments": {"target_id": "<target_id>"}\n}\n```'
    assert looks_like_fake_tool_call_text(text)


def test_extract_tool_names_from_content():
    names = extract_tool_names_from_assistant_content(
        '{"name": "gitlab_provision_status", "arguments": {}}'
    )
    assert names == ["gitlab_provision_status"]


def test_user_requests_tool_execution_approval():
    assert user_requests_tool_execution_approval("выполняй реальные действия")
    assert not user_requests_tool_execution_approval("какой uptime")


def test_invalid_architect_output_detected():
    bad = "## Step 1: Initial Analysis & Planning\n\nAwait User Feedback"
    assert looks_like_invalid_architect_output(bad, "сделай сайт про акции")


def test_normalize_execution_plan_drops_gitlab_and_k8s():
    ctx = json.dumps(
        {
            "ssh_tools": {"ssh_targets_for_tools": [{"target_id": "ssh-1"}]},
            "kubernetes_clusters": [],
        }
    )
    tasks = [
        {"title": "GitLab", "action_kind": "gitlab_create_repository", "payload": {}},
        {"title": "K8s", "action_kind": "kubernetes_query", "payload": {"operation": "pods"}},
        {
            "title": "Ansible fake",
            "action_kind": "ansible_run_template",
            "payload": {"template_id": "api_service_template"},
        },
    ]
    out, notes = normalize_worker_execution_tasks(tasks, ctx)
    assert not any(t["action_kind"] == "gitlab_create_repository" for t in out)
    assert any("ssh_run_command" in t["action_kind"] for t in out)
    assert notes
