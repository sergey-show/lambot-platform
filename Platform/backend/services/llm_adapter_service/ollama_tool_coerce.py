"""Ollama /v1 often returns tool calls as JSON in content, not in message.tool_calls."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any


def _strip_markdown_fence(text: str) -> str:
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.I)
    return m.group(1).strip() if m else t


def parse_tool_call_from_assistant_text(text: str) -> dict[str, str] | None:
    """
    Распознать {"name": "...", "arguments": {...}} из content (как отдаёт Ollama OpenAI API).
    Возвращает {name, arguments} где arguments — JSON-строка для OpenAI tool_calls.
    """
    raw = _strip_markdown_fence(text)
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    args = obj.get("arguments")
    if args is None:
        args = obj.get("args")
    if args is None:
        args = {}
    if isinstance(args, str):
        args_str = args
    else:
        args_str = json.dumps(args, ensure_ascii=False)
    return {"name": name.strip(), "arguments": args_str}


def coerce_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return message
    existing = message.get("tool_calls")
    if isinstance(existing, list) and existing:
        return message
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return message
    parsed = parse_tool_call_from_assistant_text(content)
    if not parsed:
        return message
    out = dict(message)
    out["tool_calls"] = [
        {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": parsed["name"],
                "arguments": parsed["arguments"],
            },
        }
    ]
    out["content"] = ""
    return out


def coerce_chat_completion_payload(data: dict[str, Any], *, request_had_tools: bool) -> dict[str, Any]:
    if not request_had_tools or not isinstance(data, dict):
        return data
    choices = data.get("choices")
    if not isinstance(choices, list):
        return data
    out = dict(data)
    new_choices: list[Any] = []
    changed = False
    for ch in choices:
        if not isinstance(ch, dict):
            new_choices.append(ch)
            continue
        msg = ch.get("message")
        if not isinstance(msg, dict):
            new_choices.append(ch)
            continue
        coerced = coerce_assistant_message(msg)
        if coerced is not msg:
            changed = True
            new_ch = dict(ch)
            new_ch["message"] = coerced
            if coerced.get("tool_calls"):
                new_ch["finish_reason"] = "tool_calls"
            new_choices.append(new_ch)
        else:
            new_choices.append(ch)
    if changed:
        out["choices"] = new_choices
    return out
