"""Маршрутизация SA и слои DAG."""

from services.agent_graph_service.orchestration_intents import formal_spec_intent
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


def test_enrich_route_injects_sa():
    out = enrich_route_with_sa_if_needed({"mode": "direct", "roles": ["pm"], "primary_role": "pm"}, "Нужен глоссарий")
    assert "sa" in out["roles"]
    assert out["mode"] == "full"
