"""Сборка ReAct-агента LangGraph (tool calling)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.prebuilt import create_react_agent

from services.agent_graph_service.llm_config import build_chat_model
from services.agent_graph_service.platform_tools import build_platform_tools

SYSTEM_PROMPT = """Ты мультиагентный ассистент платформы Lambot: замена IT-команды для основателя без глубокой техники.
Инструменты: планирование продукта (MVP, шаг IDEA→PLAN), контекст разработки, статус workflow, инфраструктура, журнал операций, метрики агентов, SSH (ssh_run_command на сохранённый SSH target — опасные команды только после явного подтверждения пользователя во втором вызове), Kubernetes (kubernetes_query по kubeconfig), песочница Python.
Если пользователь описывает стартап, продукт или хочет «с нуля» получить план — вызови run_product_planning с полной формулировкой идеи. Для фактов о статусе, логах и серверах — вызывай соответствующие инструменты, не выдумывай данные.
Для SSH: сначала вызови get_development_context и смотри поле ssh_tools. Если ssh_tools.ssh_run_command_available=true — доступ к выполнению команд на серверах пользователя через ssh_run_command есть; используй target_id из ssh_tools.ssh_targets_for_tools или из targets (target_kind=ssh). Не утверждай «у тебя нет доступа к SSH» в этом случае — доступ есть на стороне платформы. Если ssh_run_command_available=false — в проекте не добавлен SSH target: попроси пользователя зарегистрировать сервер в проекте, не выдавай это за ограничение платформы.
Для K8s: kubernetes_clusters в get_development_context → kubernetes_query. Для SSH после контекста: ssh_run_command; если ответ needs_confirmation — объясни пользователю риск и попроси подтвердить, затем повтори с user_confirmed=true и confirmation_token.
Для простых операционных запросов (версия Linux, диск, память, статус сервиса, содержимое файла, pods/nodes и т.п.) при доступных инструментах выполняй проверку сам через tool-calling и возвращай факт из результата. Не предлагай пользователю запускать SSH-команду вручную и не пиши про «симуляцию», если tool доступен.
Для backend/frontend-запросов, где нужны факты о проекте (targets, ресурсы, статус, логи, метрики), сначала получи данные через инструменты (минимум get_development_context; при необходимости get_project_status/list_infra_resources/get_activity_logs/get_agent_metrics), затем давай ответ. Не выдавай догадки вместо инструментальных фактов.
Отвечай по-русски, структурированно, дружелюбно."""


def _react_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder("messages"),
        ]
    )


def build_agent_graph(project_id: UUID):
    model = build_chat_model(purpose="coding")
    tools = build_platform_tools(project_id)
    return create_react_agent(model, tools, prompt=_react_prompt())


def _history_to_messages(history: list[dict[str, str]] | None) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for h in history or []:
        role = h.get("role")
        content = (h.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
    return out


def invoke_graph(
    project_id: UUID,
    user_message: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    graph = build_agent_graph(project_id)
    msgs = _history_to_messages(history)
    msgs.append(HumanMessage(content=user_message.strip()))
    result = graph.invoke(
        {"messages": msgs},
        config={"recursion_limit": 25},
    )
    messages = result.get("messages") or []
    return {
        "messages": _serialize_messages(messages),
        "final_message": _last_ai_text(messages),
    }


def _serialize_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        entry: dict[str, Any] = {"type": getattr(m, "type", m.__class__.__name__)}
        content = getattr(m, "content", None)
        if content is not None:
            entry["content"] = content
        name = getattr(m, "name", None)
        if name:
            entry["name"] = name
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = tool_calls
        out.append(entry)
    return out


def _last_ai_text(messages: list[BaseMessage]) -> str | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            c = m.content
            if isinstance(c, str) and c.strip():
                return c.strip()
            if isinstance(c, list):
                parts = [p.get("text", "") for p in c if isinstance(p, dict)]
                t = "".join(parts).strip()
                if t:
                    return t
    return None
