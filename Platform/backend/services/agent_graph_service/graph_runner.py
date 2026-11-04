"""Build LangGraph ReAct agent (tool calling)."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.prebuilt import create_react_agent

from services.agent_graph_service import langfuse_tracing
from services.agent_graph_service.llm_config import build_chat_model
from services.agent_graph_service.platform_tools import build_platform_tools

def _react_recursion_limit() -> int:
    """Max ReAct graph steps (tool + reply). Was 25 — often too low for tool chains."""
    raw = os.getenv("LANGGRAPH_RECURSION_LIMIT", "").strip()
    if not raw:
        return 100
    try:
        n = int(raw)
    except ValueError:
        return 100
    return max(5, min(n, 500))


SYSTEM_PROMPT = """You are a multi-agent assistant for the Lambot platform: an IT team substitute for a founder without deep technical background.
Tools: product planning (MVP, IDEA→PLAN step), development context, workflow status, infrastructure, activity log, agent metrics, SSH (ssh_run_command on a saved SSH target — dangerous commands may be auto-confirmed by the platform), Kubernetes (kubernetes_query via kubeconfig), Ansible marketplace (ansible_run_template from the role catalog), Python sandbox, universal HTTP (http_request via the safe executor), Playwright browser (browser_session), project wiki docs (get_project_docs_context/list_project_documents/upsert_project_document).
If the user describes a startup/product or wants a plan from scratch — call run_product_planning with the full idea. Do not use run_product_planning for "build a site", dashboard, charts, code edits, or deploy — for those: gitlab_*, repo_clone_or_sync, repo_write_commit_push. For facts about status, logs, and servers — call the matching tools; do not invent data.
For SSH: first call get_development_context and look at ssh_tools. If ssh_tools.ssh_run_command_available=true — access to run commands on the user's servers via ssh_run_command exists; use target_id from ssh_tools.ssh_targets_for_tools or from targets (target_kind=ssh). Do not claim "you have no SSH access" in that case — access exists on the platform side. If ssh_run_command_available=false — no SSH target was added to the project: ask the user to register a server; do not present that as a platform limitation.
For K8s: kubernetes_clusters in get_development_context → kubernetes_query. For SSH: ssh_run_command; when needs_confirmation the platform may run a confirmed retry automatically — do not ask the user to click OK; wait for the tool result and continue from the response facts.
For simple ops questions (Linux version, disk, memory, service status, file contents, pods/nodes, etc.), when tools are available run the check yourself via tool-calling and return facts from the result. Do not ask the user to run SSH manually and do not talk about "simulation" if the tool is available.
For backend/frontend requests that need project facts (targets, resources, status, logs, metrics), first get data via tools (at least get_development_context; if needed get_project_status/list_infra_resources/get_activity_logs/get_agent_metrics), then answer. Do not present guesses instead of tool facts.
For GitLab code (platform instance): gitlab_provision_status → if needed gitlab_create_repository → repo_clone_or_sync (work in feature_branch) → search_codebase before edits → repo_read_file / repo_write_commit_push. The GitLab token is not available directly — only through these tools.
For change requests on existing code/projects, first analyze the current state from tool facts, then propose a plan/actions. Do not skip analysis and jump to a solution based on assumptions.
Wiki/RAG: do not wait for a ready summary in the prompt — decide whether docs are needed and call get_project_docs_context with a narrow query (and doc_types_csv if needed), top_k 3–8. Do not cite wiki as deploy/code fact without tool confirmation or current-run execution facts. After a substantial task, update documents via upsert_project_document: architecture, user_guide, admin_guide (if there were changes).
If the user asks to install an infrastructure service (e.g. Docker/GitLab/Nexus), try ansible_run_template from the internal marketplace first. Do not treat GitLab/Nexus as mandatory — rely on project context and the explicit user request. If the template is missing — retry ansible_run_template with allow_fallback=true and fallback_service.
For API integrations and webhooks use http_request (endpoint_alias + method + path + query/body), not invented replies.
For UI/web checks use browser_session: create -> navigate/click/type/... -> snapshot/screenshot/content -> close.
Important: browser_session is for checking the web UI and does not edit repository sources. Do not propose click/type in the browser as a way to change frontend/backend code.
If the user asks to change code (e.g. remove a component/chart), first check project context and ssh_tools. If there is an SSH target — perform real changes on the server via ssh_run_command (stepwise: find file, edit, verify, restart/reload the service). If there is no SSH target — do not fake execution: honestly request the missing access.
Forbidden: writing JSON like {{"name": "tool_name", "arguments": {{...}}}} in the reply — that is not a tool call. For GitLab/repos you must call gitlab_provision_status, gitlab_create_repository, repo_clone_or_sync via function calling and rely on their JSON responses.
Reply in English, structured and friendly."""


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
    *,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    graph = build_agent_graph(project_id)
    msgs = _history_to_messages(history)
    msgs.append(HumanMessage(content=user_message.strip()))
    cfg: dict[str, Any] = {"recursion_limit": _react_recursion_limit()}
    cfg.update(langfuse_tracing.invoke_config(orchestration_run_id=orchestration_run_id, step_key="langgraph_react"))
    try:
        result = graph.invoke({"messages": msgs}, config=cfg)
        messages = result.get("messages") or []
        return {
            "messages": _serialize_messages(messages),
            "final_message": _last_ai_text(messages),
        }
    finally:
        langfuse_tracing.flush()


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
