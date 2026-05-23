"""ReAct-исполнение задач трекера."""

from services.agent_worker_service.tracker_react import (
    augment_tracker_prompt,
    build_tracker_result,
    collect_react_evidence,
    extract_final_text,
)


def test_extract_final_text_prefers_final_message():
    assert extract_final_text({"final_message": "  ok  ", "messages": []}) == "ok"


def test_collect_react_evidence_counts_tools():
    graph = {
        "messages": [
            {"type": "ai", "tool_calls": [{"name": "get_development_context"}]},
            {"type": "tool", "content": "{}"},
            {"type": "ai", "tool_calls": [{"name": "ssh_run_command"}]},
            {"type": "tool", "content": "stdout"},
        ]
    }
    ev = collect_react_evidence(graph)
    assert ev["tool_results_count"] == 2
    assert ev["has_execution_tool_call"] is True


def test_build_tracker_result_fails_without_tools_when_required():
    result, ok = build_tracker_result(
        output="Запустите uname -a вручную",
        evidence={"tool_calls_count": 0, "tool_results_count": 0, "tool_names": []},
        title="OS version",
        role="devops",
        execution_id="exec-1",
        require_tools=True,
    )
    assert ok is False
    assert result["dod"]["status"] == "tools_missing"


def test_build_tracker_result_ok_with_ssh_tools():
    result, ok = build_tracker_result(
        output="Linux 6.1",
        evidence={
            "tool_calls_count": 2,
            "tool_results_count": 2,
            "tool_names": ["get_development_context", "ssh_run_command"],
            "has_execution_tool_call": True,
        },
        title="OS version",
        role="devops",
        execution_id="exec-1",
        require_tools=True,
    )
    assert ok is True
    assert result["dod"]["status"] == "tool_verified"
    assert result["engine"] == "agent_graph_react"


def test_augment_tracker_prompt_idempotent():
    base = "Задача"
    once = augment_tracker_prompt(base)
    twice = augment_tracker_prompt(once)
    assert once == twice
