from services.llm_adapter_service.ollama_tool_coerce import (
    coerce_chat_completion_payload,
    parse_tool_call_from_assistant_text,
)


def test_parse_tool_call_from_content_json():
    p = parse_tool_call_from_assistant_text('{"name": "add", "arguments": {"a": 2, "b": 2}}')
    assert p is not None
    assert p["name"] == "add"
    assert '"a": 2' in p["arguments"]


def test_coerce_chat_completion_moves_content_to_tool_calls():
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"name": "ssh_run_command", "arguments": {"target_id": "x", "command": "uptime"}}',
                },
                "finish_reason": "stop",
            }
        ]
    }
    out = coerce_chat_completion_payload(payload, request_had_tools=True)
    msg = out["choices"][0]["message"]
    assert msg.get("content") == ""
    assert len(msg.get("tool_calls") or []) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "ssh_run_command"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
