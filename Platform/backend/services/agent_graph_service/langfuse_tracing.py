"""Опциональная трассировка LangChain/LangGraph в Langfuse (LLM observability)."""

from __future__ import annotations

import os
import uuid
from typing import Any


def enabled() -> bool:
    pub = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    sec = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not pub or not sec:
        return False
    host = (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "").strip()
    return bool(host)


def stable_trace_id_for_run(run_id: str) -> str:
    """Детерминированный trace_id: все вызовы одного AI-team run склеиваются в один trace."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"lambot/agent-graph/orchestration:{run_id}").hex


def invoke_config(*, orchestration_run_id: str | None = None) -> dict[str, Any]:
    """
    Возвращает RunnableConfig для model.invoke / graph.invoke.
    orchestration_run_id: id run AI-команды — общий trace в Langfuse.
    """
    if not enabled():
        return {}
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        return {}
    try:
        if orchestration_run_id:
            h = CallbackHandler(trace_context={"trace_id": stable_trace_id_for_run(orchestration_run_id)})
        else:
            h = CallbackHandler()
    except TypeError:
        h = CallbackHandler()
    return {"callbacks": [h]}


def flush() -> None:
    if not enabled():
        return
    try:
        from langfuse import get_client

        get_client().flush()
        return
    except Exception:
        pass
    try:
        from langfuse import Langfuse

        Langfuse().flush()
    except Exception:
        pass
