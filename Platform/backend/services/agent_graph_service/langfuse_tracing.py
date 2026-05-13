"""Опциональная трассировка LangChain/LangGraph в Langfuse (LLM observability)."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _tracing_disabled_by_flag() -> bool:
    """LANGFUSE_TRACING_ENABLED=0|false|no|off — явно выключить (остальные значения / пусто = не блокируем)."""
    v = os.getenv("LANGFUSE_TRACING_ENABLED", "").strip().lower()
    return v in ("0", "false", "no", "off")


def enabled() -> bool:
    if _tracing_disabled_by_flag():
        return False
    pub = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    sec = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not pub or not sec:
        return False
    host = (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "").strip()
    return bool(host)


def stable_trace_id_for_run(run_id: str) -> str:
    """Детерминированный trace_id: 32 lowercase hex (W3C / Langfuse), без дефисов.

    Langfuse SDK парсит trace_id как int(..., 16) — строка UUID с дефисами недопустима.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"lambot/agent-graph/orchestration:{run_id}").hex


def invoke_config(
    *,
    orchestration_run_id: str | None = None,
    step_key: str | None = None,
) -> dict[str, Any]:
    """
    Возвращает RunnableConfig для model.invoke / graph.invoke.
    orchestration_run_id: id run AI-команды — общий trace в Langfuse (trace_id детерминирован).
    step_key: тег/metadata для фильтрации в UI Langfuse.

    Если задан AGENT_GRAPH_OPENAI_BASE_URL, LangChain ходит в llm-adapter /v1/chat/completions, где уже
    пишется generation в Langfuse — CallbackHandler отключаем, чтобы не дублировать события.
    """
    if not enabled():
        return {}
    if (os.getenv("AGENT_GRAPH_OPENAI_BASE_URL") or "").strip():
        return {}
    try:
        from langfuse import get_client
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        logger.warning("langfuse: CallbackHandler недоступен (%s)", exc)
        return {}
    try:
        # Singleton SDK подхватывает LANGFUSE_* из env; до CallbackHandler — как в доке Langfuse.
        get_client()
    except Exception:
        logger.exception("langfuse: get_client() не инициализировался (проверьте ключи и LANGFUSE_BASE_URL)")
        return {}
    try:
        if orchestration_run_id:
            h = CallbackHandler(trace_context={"trace_id": stable_trace_id_for_run(orchestration_run_id)})
        else:
            h = CallbackHandler()
    except TypeError:
        h = CallbackHandler()
    meta: dict[str, Any] = {"langfuse_tags": ["agent-graph"]}
    if orchestration_run_id:
        meta["langfuse_session_id"] = orchestration_run_id
        meta["orchestration_run_id"] = orchestration_run_id
    if step_key:
        meta["langfuse_tags"] = [*meta["langfuse_tags"], f"step:{step_key}"]
        meta["step_key"] = step_key
    out: dict[str, Any] = {"callbacks": [h], "metadata": meta}
    if orchestration_run_id and step_key:
        out["run_name"] = f"orch:{step_key}"
    return out


def flush() -> None:
    if not enabled():
        return
    try:
        from langfuse import get_client

        get_client().flush()
        return
    except Exception:
        logger.warning("langfuse: flush через get_client() не удался", exc_info=True)
    try:
        from langfuse import Langfuse

        Langfuse().flush()
    except Exception:
        logger.warning("langfuse: flush через Langfuse() не удался", exc_info=True)
