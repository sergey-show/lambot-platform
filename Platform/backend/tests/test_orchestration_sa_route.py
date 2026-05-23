"""Маршрутизация SA и слои DAG."""

import json

from services.agent_graph_service.orchestration_intents import (
    extract_tool_names_from_assistant_content,
    formal_spec_intent,
    looks_like_fake_tool_call_text,
    operational_facts_intent,
    looks_like_invalid_architect_output,
    user_requests_tool_execution_approval,
)
from services.agent_graph_service.execution_plan_parse import normalize_worker_execution_tasks
from services.agent_graph_service.orchestration_route import (
    active_dag_layers,
    enrich_route_with_sa_if_needed,
    heuristic_orchestration_route,
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


def test_enrich_route_injects_sa():
    out = enrich_route_with_sa_if_needed({"mode": "direct", "roles": ["pm"], "primary_role": "pm"}, "Нужен глоссарий")
    assert "sa" in out["roles"]
    assert out["mode"] == "full"


def test_operational_facts_intent_uptime():
    assert operational_facts_intent("какой uptime сервера сейчас? и хватает ли там ресурсов?")


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
