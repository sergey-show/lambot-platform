"""AI Team orchestration: steps in Postgres (admin), LLM per role, summary and architecture."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
import re
from time import monotonic
from typing import Any
from uuid import UUID

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from services.admin_service.ai_team_service import DEFAULT_PIPELINE
from services.agent_graph_service.dev_prompts import (
    fetch_development_context_summary,
    fetch_development_prompts,
    fetch_documents_context_summary,
)
from services.agent_graph_service.orchestration_route import (
    PRIMARY_ROLE_TO_STEP,
    active_dag_layers,
    enrich_route_with_sa_if_needed,
    parse_orchestration_route,
    route_is_usable,
    pipeline_for_direct,
    route_includes_sa,
)
from services.agent_graph_service.orchestration_policy import (
    direct_skip_keys,
    direct_use_execution_tail,
    dod_applies_to_step,
    dod_gate_issues_for_run,
    dod_gate_user_questions,
    live_execution_from_route,
    merge_live_execution_flag,
)
from services.agent_graph_service.orchestration_step_output import parse_step_envelope
from services.agent_graph_service.execution_plan_parse import (
    default_execution_tasks_if_empty,
    format_execution_tasks_markdown,
    normalize_worker_execution_tasks,
    parse_execution_plan_payload,
    wait_for_ai_team_execution_tasks,
)
from services.agent_graph_service.execution_publish import fetch_project_tenant_id, publish_ai_team_execution_tasks
from services.agent_graph_service import langfuse_tracing
from services.agent_graph_service.gitlab_actions import (
    execute_gitlab_create_repository,
    gitlab_repo_create_intent,
    parse_repo_slug_from_message,
)
from services.agent_graph_service.operational_actions import (
    OPS_PROBE_COMMAND,
    execute_operational_ssh_probe,
)
from services.agent_graph_service.orchestration_intents import (
    extract_tool_names_from_assistant_content,
    looks_like_fake_tool_call_text,
    looks_like_invalid_architect_output,
    user_requests_tool_execution_approval,
)
from services.agent_graph_service.graph_runner import invoke_graph
from services.agent_graph_service.llm_config import build_chat_model
from services.agent_graph_service.redis_team import cache_latest_run_id
from services.agent_graph_service.team_client import (
    team_append_execution_tasks,
    team_get_run,
    team_patch_run,
    team_patch_step,
    team_replace_execution_tasks,
)

# Which admin role prompt to use for the step
STEP_PROMPT_ROLE: dict[str, str] = {
    "intake": "pm",
    "sa": "sa",
    "pm": "pm",
    "architect": "architect",
    "devops_plan": "devops",
    "backend_plan": "backend",
    "frontend_plan": "frontend",
    "qa_validation": "qa",
    "execution_plan": "devops",
    "implementation": "devops",
    "deploy": "devops",
    "pm_summary": "pm",
}

STEP_EXTRA: dict[str, str] = {
    "intake": (
        "You are the orchestration intake router. Decide ONLY who must work next.\n"
        "Return STRICTLY one JSON object, no preamble, no markdown fences.\n"
        "Format:\n"
        '{"orchestration_route":{"mode":"full|direct","roles":["sa|pm|architect|devops|backend|frontend|qa"],'
        '"primary_role":"sa|pm|architect|devops|backend|frontend|qa|null",'
        '"live_execution":false,"reason":"brief English rationale"}}\n'
        "Decision rules (apply in order):\n"
        "1) roles = minimal set actually required; never invent unused roles.\n"
        "2) live_execution=true ONLY for real side effects (deploy, GitLab writes, SSH/Ansible/K8s changes). "
        "Factual questions (disk, uptime, status, versions) => live_execution=false.\n"
        "3) Formal requirements / glossary / traceability / epics+tracker tasks => include sa and mode=full.\n"
        "4) Simple MVP/site without a formal spec => do NOT include sa.\n"
        "5) Exactly one specialist role and no sa => mode=direct and primary_role=<that role>, roles=[that role].\n"
        "6) Two or more roles OR sa present => mode=full and primary_role=null.\n"
        "7) Do not write plans, architecture, or analysis at intake — routing JSON only."
    ),
    "sa": (
        "You are a systems analyst. Required artifacts:\n"
        "1) wiki: upsert_project_document — doc_type=spec (formal requirements), glossary, traceability; in the spec — mermaid (context, processes).\n"
        "2) tracker: tracker_create_epic (1+ epics), tracker_create_task (tasks with assignee_role devops|backend|frontend|qa|architect).\n"
        "3) in card meta_json/run_id set the orchestration run_id; in tasks — requirement_id (REQ-001…).\n"
        "4) tracker_list_items — verify created cards.\n"
        "PM and architect work after you — do not duplicate their areas (release priorities, stack). "
        "In the step text give a short summary + links (document slugs, epic/task ids)."
    ),
    "pm": (
        "You are a PM: product release scope, priorities, KPI, stakeholder risks, schedule alignment. "
        "Build on SA artifacts (spec, tracker) if they exist in accumulated steps. "
        "Do not write a detailed tech spec, AS-IS/TO-BE diagrams, or a traceability matrix — that is SA's area."
    ),
    "architect": (
        "Describe the target architecture, technology stack, service boundaries, data, and integrations. "
        "Break work into stages. Explicitly state tasks for DevOps, Backend, Frontend, and QA (as a list). "
        "This text goes into the \"Architecture\" section for the user.\n"
        "Add the full project structure: key directories/services, module purpose, contracts between them, "
        "main APIs/tables/queues, and where deployment runs."
    ),
    "devops_plan": (
        "Make a DevOps work plan: CI/CD, infrastructure, secrets, environments, deploy. "
        "Who does what and in what order (table or list is fine). Align with the architecture above.\n"
        "For changes to an existing project, first analyze the current state from facts "
        "and rely only on (context/docs/tools), then give a plan. The reply must include sections: "
        "\"Current state analysis (facts)\" and \"Change plan\"."
    ),
    "backend_plan": (
        "Make a backend work plan: APIs, DB, integrations. Who does what and when. Align with the architecture.\n"
        "For changes to an existing project, first analyze current code/contracts from facts "
        "(context/documentation/tools), then give a plan. The reply must include sections: "
        "\"Current code analysis (facts)\" and \"Change plan\"."
    ),
    "frontend_plan": (
        "Make a frontend work plan: screens, state, API calls. Who does what and when.\n"
        "For changes to an existing project, first analyze current UI code from facts "
        "(context/documentation/tools), then give a plan. The reply must include sections: "
        "\"Current code analysis (facts)\" and \"Change plan\"."
    ),
    "qa_validation": (
        "Run QA validation after the actual deploy based on step results and execution facts. "
        "Produce a minimal test plan (smoke/regression), risks, and a release verdict. "
        "In summary_md you must include the line: release_recommendation: go|no_go. "
        "If no_go — set pipeline_continue=false and add at least 1 question/blocker in questions_for_user."
    ),
    "execution_plan": (
        "This step is ONLY for a machine execution plan in BATCHES (separate from narrative). "
        "Reply with strictly one JSON object, no markdown and no surrounding explanation. Format:\n"
        '{"tasks":[{"title":"brief","action_kind":"ssh_run_command|kubernetes_query|ansible_run_template|noop","payload":{}}],"continue_planning":false,"goal_done":false,"done_reason":""}\n'
        "action_kind meaning:\n"
        "- ssh_run_command: real commands on an SSH target. payload: {\"target_id\":\"<uuid>\",\"command\":\"...\"} "
        "or {\"target_id\":\"<uuid>\",\"commands\":[\"...\",\"...\"]}.\n"
        "- kubernetes_query: real kubectl via adapter. payload: {\"target_id\":\"<uuid>\",\"operation\":\"...\", ...}.\n"
        "- ansible_run_template: run a role from the internal Ansible marketplace. payload: "
        "{\"target_id\":\"<uuid>\",\"template_id\":\"<template-id>\",\"variables\":{...},"
        "\"allow_fallback\":true,\"fallback_service\":\"<service-id>\"}.\n"
        "- noop: do nothing (only if there are truly no available actions).\n"
        "- adapter_agent_demo / gitlab_pipeline: legacy modes, only on explicit request.\n"
        "FORBIDDEN in action_kind: gitlab_create_repository, repo_clone_or_sync, search_codebase, repo_write_commit_push — "
        "these are LangGraph tools on devops/backend/frontend_plan steps; workers do not run them. "
        "For GitLab/code on this step use adapter_agent_demo (if appropriate) or ssh_run_command.\n"
        "kubernetes_query — only if context has kubernetes_clusters (non-empty list).\n"
        "ansible_run_template — only with a real template_id from the marketplace catalog; do not invent *_template.\n"
        "Return only one executable batch with a limited number of tasks per iteration (not a monolithic plan).\n"
        "If the user asks for a real deploy/server actions and context has ssh_targets_for_tools — use ssh_run_command "
        "with concrete commands, not noop. For a k8s cluster use kubernetes_query. "
        "If the request is about installing common services — prefer ansible_run_template, "
        "but only for services that actually exist in context/catalog or were explicitly requested.\n"
        "If the request is to patch an existing app on a server, build ssh_run_command with concrete steps "
        "to edit files/configs, verify the result, and restart the needed service."
    ),
    "implementation": (
        "Build on the \"Execution facts\" block (structured tasks, workers) — what actually ran and with what result. "
        "Add a human-readable plan: repositories, pipelines, Ansible. If workers did not run or failed — say so honestly. "
        "Never claim a service/deploy succeeded if execution facts have errors or unfinished tasks."
    ),
    "deploy": (
        "Describe how to deploy the result on the project server using context (targets, URL). "
        "If there are no servers — say an SSH target must be registered first. "
        "Do not claim \"successfully deployed\" if execution facts have errors or no confirmed successful run."
    ),
    "pm_summary": (
        "Final report for a non-technical customer: what was delivered, how to try the product. "
        "Give URLs, logins, and passwords ONLY if they are explicitly in the project context below; otherwise say honestly "
        "that credentials must be obtained from an admin or set up in the repo/on the server. "
        "Do not add fake \"successfully deployed\" phrases if previous steps have errors.\n\n"
        "The \"Accumulated step results\" block has only role outcomes (PM, architect, role plans, QA) — without raw "
        "execution-plan JSON and without long implementation/deploy reports; large text may be truncated. "
        "The \"Execution facts\" block is a short worker task list (name, action kind, status, short error), without full logs."
    ),
}

STEP_META: dict[str, tuple[str, str]] = {k: (lbl, task) for k, lbl, task in DEFAULT_PIPELINE}
STEP_ORDER: dict[str, int] = {k: i for i, (k, _lbl, _task) in enumerate(DEFAULT_PIPELINE)}
# Only narrative role steps for the final report (without execution_plan / implementation / deploy).
PM_SUMMARY_SOURCE_STEP_KEYS = frozenset(
    {"sa", "pm", "architect", "devops_plan", "backend_plan", "frontend_plan", "qa_validation"}
)
PM_SUMMARY_STEP_BODY_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_STEP_MAX_CHARS", "28000"))
PM_SUMMARY_CTX_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_CTX_MAX_CHARS", "10000"))
PM_SUMMARY_DOCS_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_DOCS_MAX_CHARS", "8000"))
PM_SUMMARY_EXEC_COMPACT_MAX = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_EXEC_COMPACT_MAX", "12000"))
DIRECT_TOOL_STEPS = frozenset({"devops_plan", "backend_plan", "frontend_plan", "qa_validation"})
PLAN_ROLE_STEPS = frozenset({"devops_plan", "backend_plan", "frontend_plan"})
PLANNING_PLATFORM_TOOLS = frozenset(
    {
        "get_development_context",
        "gitlab_provision_status",
        "gitlab_create_repository",
        "list_project_repos",
        "repo_clone_or_sync",
        "repo_read_file",
        "repo_write_commit_push",
        "search_codebase",
        "get_project_docs_context",
        "list_infra_resources",
        "get_project_status",
        "http_request",
    }
)
# "Coding" model from platform settings (coding_model) — engineering steps only.
CODING_MODEL_STEPS = frozenset(
    {
        "devops_plan",
        "backend_plan",
        "frontend_plan",
        "qa_validation",
        "execution_plan",
        "implementation",
        "deploy",
    }
)
EXECUTABLE_ACTION_KINDS = frozenset({"ssh_run_command", "kubernetes_query", "ansible_run_template"})
MAX_STEP_RETRIES = 3
MAX_INTAKE_RETRIES = int(os.getenv("AGENT_ORCHESTRATION_INTAKE_MAX_RETRIES", "1"))
MAX_STEP_INVOCATIONS_PER_RUN = 4
MAX_IDENTICAL_ERROR_REPEATS = 2
MAX_IDENTICAL_OUTPUT_REPEATS = 2
MAX_ORCHESTRATION_SECONDS = int(os.getenv("AGENT_ORCHESTRATION_MAX_SECONDS", "7200"))
EXECUTION_TASKS_WAIT_SECONDS = int(os.getenv("AGENT_ORCHESTRATION_EXECUTION_WAIT_SECONDS", "3600"))
PLAN_MAX_TASKS_PER_ITER = int(os.getenv("AGENT_PLAN_MAX_TASKS_PER_ITER", "5"))
PLAN_MAX_ITERATIONS = int(os.getenv("AGENT_PLAN_MAX_ITERATIONS", "20"))
PLAN_BATCH_WAIT_SECONDS = int(os.getenv("AGENT_PLAN_BATCH_WAIT_SECONDS", "900"))
MAX_PEER_QUERIES = 5
# Intake is always LLM-based (no keyword heuristics). Slightly higher token budget for reliable JSON.
INTAKE_MAX_TOKENS = int(os.getenv("AGENT_ORCHESTRATION_INTAKE_MAX_TOKENS", "256"))
# Intake routing: do not send the full user_message to the LLM (long questions = long/expensive prompt).
INTAKE_USER_MSG_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_INTAKE_USER_MSG_MAX_CHARS", "14000"))
INTAKE_USER_MSG_TAIL_CHARS = int(os.getenv("AGENT_ORCHESTRATION_INTAKE_USER_MSG_TAIL_CHARS", "4000"))
INTAKE_LLM_TIMEOUT_SEC = float(os.getenv("AGENT_ORCHESTRATION_INTAKE_TIMEOUT_SEC", "90"))
# Inject wiki RAG summary into every step (heavy prompt, risk of stale "✅ done"). Off by default — agents search via get_project_docs_context.
INLINE_RAG_IN_PROMPT = os.getenv("AGENT_ORCHESTRATION_INLINE_RAG", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
WIKI_ON_DEMAND_HINT = (
    "Project documentation (wiki/RAG) is intentionally not fully loaded into this prompt. "
    "If you need facts from past iterations — craft a narrow search query (what exactly: deploy, repo, URL, architecture). "
    "In tool mode (LangGraph): get_project_docs_context (wiki) or search_knowledge (PDF/scan knowledge bases, top_k 3–8). "
    'Do not claim "already deployed/done" from wiki or guesses — only "Execution facts" of the current run and development-context.'
)
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")

# Steps with a unified JSON envelope (summary + questions/peer_queries); execution_plan and intake are separate.
STRUCTURED_STEP_KEYS = frozenset(
    {
        "pm",
        "architect",
        "devops_plan",
        "backend_plan",
        "frontend_plan",
        "qa_validation",
        "implementation",
        "deploy",
        "pm_summary",
    }
)

STEP_OUTPUT_JSON_INSTRUCTIONS = """
--- REQUIRED RESPONSE FORMAT FOR THIS STEP ---
First output EXACTLY ONE JSON object (no markdown fences), then you may add a blank line and extra text.
JSON schema:
{
  "schema_version": 1,
  "step_status": "complete | need_user_clarification | need_peer_sync",
  "summary_md": "main step result for teammates and the report (markdown)",
  "questions_for_user": [],
  "peer_queries": [],
  "pipeline_continue": true,
  "live_execution": false
}
Rules:
- summary_md — the main content passed forward; put everything important for the team here.
- live_execution (optional, default false): true — later pipeline steps need real infrastructure operations
  (SSH/kubectl/ansible, edits on an already deployed system, deploy), not only text plans; false — design/code without live execution is enough.
  For pm and architect steps, judge from the user request and set explicitly.
- If a user reply is needed before continuing — step_status=need_user_clarification, fill questions_for_user, pipeline_continue=false.
- If the question is for another team role (backend/frontend/devops/architect/pm/qa) — add to peer_queries: [{"role":"backend","question":"..."}]. The orchestrator will query the colleague automatically; do not wait for the user.
- Do not ask the user questions that can be resolved via peer_queries.
- When pipeline_continue=false the pipeline stops until the next user message.
"""


def _text_fingerprint(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    return t[:1200]


def _sanitize_model_text(text: str) -> str:
    """Strips provider control tokens from the model reply."""
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"<\|[^|>]+?\|>", "", t)
    t = re.sub(r"<\|?(?:im_start|im_end|endoftext)\|?>", "", t, flags=re.I)
    t = re.sub(r"</?redacted_thinking>", "", t, flags=re.I)
    return t.strip()


_LLM_STREAM_PATCH_SEC = float(os.getenv("AGENT_ORCHESTRATION_LLM_STREAM_PATCH_SEC", "0.45"))
_LLM_STREAM_MIN_CHARS = int(os.getenv("AGENT_ORCHESTRATION_LLM_STREAM_MIN_CHARS", "280"))
_LLM_PROMPT_MAX = int(os.getenv("AGENT_ORCHESTRATION_LLM_PROMPT_MAX_CHARS", "120000"))


def _lc_model_label(model: Any) -> str:
    try:
        m = getattr(model, "model_name", None) or getattr(model, "model", None)
        if isinstance(m, str) and m.strip():
            return m.strip()
        b = getattr(model, "bound", None)
        if b is not None:
            m2 = getattr(b, "model_name", None) or getattr(b, "model", None)
            if isinstance(m2, str) and m2.strip():
                return m2.strip()
    except Exception:
        pass
    return ""


def _message_text_content(msg: Any) -> str:
    c = getattr(msg, "content", None)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: list[str] = []
        for part in c:
            if isinstance(part, dict):
                t = str(part.get("text") or part.get("content") or "").strip()
                if t:
                    parts.append(t)
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(c) if c is not None else ""


def _messages_prompt_preview(messages: list[Any]) -> str:
    lines: list[str] = []
    for m in messages:
        cls = m.__class__.__name__.replace("Message", "")
        role = cls.lower() if cls else "message"
        lines.append(f"### {role}\n{_message_text_content(m)}")
    return "\n\n".join(lines)[:_LLM_PROMPT_MAX]


def _chunk_to_text(chunk: Any) -> str:
    c = getattr(chunk, "content", None)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: list[str] = []
        for part in c:
            if isinstance(part, dict):
                t = str(part.get("text") or "").strip()
                if t:
                    parts.append(t)
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(c) if c is not None else ""


def _step_running_status(run_id: str, step_key: str) -> str:
    row = team_get_run(run_id) or {}
    for s in row.get("steps") or []:
        if isinstance(s, dict) and str(s.get("step_key")) == step_key:
            return str(s.get("status") or "running")
    return "running"


def _patch_step_streaming_output(run_id: str, step_key: str, text: str, throttle: dict[str, Any]) -> None:
    now = monotonic()
    last_t = float(throttle.get("last_t") or 0.0)
    last_len = int(throttle.get("last_len") or -1)
    if (
        text
        and len(text) - last_len < _LLM_STREAM_MIN_CHARS
        and now - last_t < _LLM_STREAM_PATCH_SEC
    ):
        return
    throttle["last_t"] = now
    throttle["last_len"] = len(text)
    st = _step_running_status(run_id, step_key)
    team_patch_step(run_id, step_key, status=st, output_md=text)


def _publish_llm_prompt_meta(run_id: str, step_key: str, prompt_preview: str, model_label: str) -> None:
    row = team_get_run(run_id) or {}
    prev: dict[str, Any] = {}
    for s in row.get("steps") or []:
        if isinstance(s, dict) and str(s.get("step_key")) == step_key:
            p = s.get("structured")
            prev = dict(p) if isinstance(p, dict) else {}
            break
    prev["llm_prompt_preview"] = prompt_preview
    if model_label:
        prev["llm_model"] = model_label
    st = _step_running_status(run_id, step_key)
    team_patch_step(run_id, step_key, status=st, structured=prev)


def _truncate_user_message_for_intake(user_message: str) -> str:
    """Start + end of a long request: enough for role routing at a glance."""
    m = (user_message or "").strip()
    if len(m) <= INTAKE_USER_MSG_MAX_CHARS:
        return m
    tail_n = min(max(400, INTAKE_USER_MSG_TAIL_CHARS), INTAKE_USER_MSG_MAX_CHARS // 2)
    reserve = 220
    head_n = INTAKE_USER_MSG_MAX_CHARS - tail_n - reserve
    if head_n < 2000:
        head_n = INTAKE_USER_MSG_MAX_CHARS - tail_n - 120
    marker = (
        f"\n\n---\n[fragment omitted: {len(m)} chars total; routing uses the start and end of the request]\n---\n\n"
    )
    return m[:head_n] + marker + m[-tail_n:]


def _stream_chat_then_sanitize(
    model: Any,
    messages: list[Any],
    config: dict[str, Any],
    *,
    run_id: str,
    step_key: str,
    stream: bool = True,
) -> str:
    """Streams the reply into step output_md (throttled) for live UI; fallback — invoke."""
    try:
        prompt_preview = _messages_prompt_preview(messages)
        model_label = _lc_model_label(model)
        _publish_llm_prompt_meta(run_id, step_key, prompt_preview, model_label)
        throttle: dict[str, Any] = {"last_t": 0.0, "last_len": -1}
        parts: list[str] = []
        if not stream:
            resp = model.invoke(messages, config=config)
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            text = _sanitize_model_text(raw)
            _patch_step_streaming_output(run_id, step_key, text, {"last_t": 0.0, "last_len": -1})
            return text
        try:
            for chunk in model.stream(messages, config=config):
                piece = _chunk_to_text(chunk)
                if not piece:
                    continue
                parts.append(piece)
                _patch_step_streaming_output(run_id, step_key, "".join(parts), throttle)
        except Exception:
            resp = model.invoke(messages, config=config)
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            text = _sanitize_model_text(raw)
            _patch_step_streaming_output(run_id, step_key, text, {"last_t": 0.0, "last_len": -1})
            return text
        text = _sanitize_model_text("".join(parts))
        _patch_step_streaming_output(run_id, step_key, text, {"last_t": 0.0, "last_len": -1})
        return text
    finally:
        if langfuse_tracing.enabled():
            langfuse_tracing.flush()


def _extract_graph_text(graph_result: dict[str, Any]) -> str:
    final = str(graph_result.get("final_message") or "").strip()
    if final:
        return final
    for msg in reversed(graph_result.get("messages") or []):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    t = str(part.get("text") or "").strip()
                    if t:
                        parts.append(t)
            merged = "".join(parts).strip()
            if merged:
                return merged
    return ""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _llm_purpose_for_step(step_key: str) -> str:
    """coding — use coding_model from LLM settings if set; otherwise default_model."""
    return "coding" if step_key in CODING_MODEL_STEPS else "default"


def _ctx_trim(ctx: str, max_chars: int) -> str:
    c = (ctx or "").strip()
    if len(c) <= max_chars:
        return c
    return c[:max_chars] + "\n… [truncated by character limit]"


def _compact_execution_tasks_summary(run_obj: dict[str, Any] | None) -> str:
    """Short execution_tasks summary for pm_summary — without full payloads and logs."""
    if not run_obj or not isinstance(run_obj, dict):
        return "(no execution_tasks data)"
    tasks_raw = run_obj.get("execution_tasks")
    tasks = tasks_raw if isinstance(tasks_raw, list) else []
    if not tasks:
        return "(no executable tasks)"
    lines: list[str] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "—")[:160]
        ak = str(t.get("action_kind") or "—")[:48]
        st = str(t.get("status") or "—")
        err = str(t.get("error") or "").strip()
        line = f"- {title} · {ak} · {st}"
        if err:
            line += f" · {err[:240]}"
        lines.append(line)
    out = "\n".join(lines)
    if len(out) > PM_SUMMARY_EXEC_COMPACT_MAX:
        out = out[:PM_SUMMARY_EXEC_COMPACT_MAX] + "\n… [task list truncated]"
    return out


def _has_executable_execution_tasks(tasks: list[dict[str, Any]]) -> bool:
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if str(t.get("action_kind") or "").strip().lower() in EXECUTABLE_ACTION_KINDS:
            return True
    return False


def _infer_infra_restrictions(ctx: str) -> str:
    """Explicitly forbids the LLM inventing GitLab/Nexus if they are not in project context."""
    try:
        obj = json.loads(ctx) if ctx else {}
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    roles: set[str] = set()
    for row in obj.get("service_assignments") or []:
        if isinstance(row, dict):
            v = str(row.get("service_role") or "").strip().lower()
            if v:
                roles.add(v)
    for row in obj.get("infra_resources") or []:
        if not isinstance(row, dict):
            continue
        for key in ("category", "resource_type", "id", "name", "title"):
            val = str(row.get(key) or "").strip().lower()
            if "gitlab" in val:
                roles.add("gitlab")
            if "nexus" in val:
                roles.add("nexus")
    missing: list[str] = []
    if "gitlab" not in roles:
        missing.append("GitLab/GitLab CI")
    if "nexus" not in roles:
        missing.append("Nexus")
    if not missing:
        return ""
    return (
        "Context fact constraint: "
        + ", ".join(missing)
        + " are absent from project resources. Do not mention them as existing."
    )


def _extract_text_blobs(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, list):
        return "\n".join(_extract_text_blobs(x) for x in obj)
    if isinstance(obj, dict):
        return "\n".join(_extract_text_blobs(v) for v in obj.values())
    return ""


def _collect_graph_execution_evidence(graph_result: dict[str, Any]) -> dict[str, Any]:
    messages = graph_result.get("messages") if isinstance(graph_result.get("messages"), list) else []
    tool_names: list[str] = []
    tool_calls_count = 0
    tool_results_count = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        t = str(m.get("type") or "").strip().lower()
        if t == "tool":
            tool_results_count += 1
        tc = m.get("tool_calls")
        if isinstance(tc, list):
            for item in tc:
                if not isinstance(item, dict):
                    continue
                nm = str(item.get("name") or "").strip()
                if not nm:
                    continue
                tool_calls_count += 1
                tool_names.append(nm)
        if t in ("ai", "aimessage", "assistant"):
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                for nm in extract_tool_names_from_assistant_content(content):
                    tool_calls_count += 1
                    tool_names.append(nm)
    unique_names = sorted({x for x in tool_names if x})
    exec_tools = {"ssh_run_command", "kubernetes_query", "ansible_run_template"}
    return {
        "tool_calls_count": tool_calls_count,
        "tool_results_count": tool_results_count,
        "tool_names": unique_names,
        "has_execution_tool_call": any(x in exec_tools for x in unique_names),
        "has_planning_tool_call": any(x in PLANNING_PLATFORM_TOOLS for x in unique_names),
    }


def _merge_tool_evidence_from_text(ev: dict[str, Any], text: str) -> dict[str, Any]:
    """Enrich evidence with tool names from text (Ollama JSON in content, markdown instructions)."""
    out = dict(ev) if isinstance(ev, dict) else {}
    names = list(out.get("tool_names") or [])
    for nm in extract_tool_names_from_assistant_content(text):
        if nm not in names:
            names.append(nm)
    calls = int(out.get("tool_calls_count") or 0)
    results = int(out.get("tool_results_count") or 0)
    extra = max(0, len(names) - calls)
    if extra:
        calls += extra
    if extra and results <= 0:
        results = min(calls, extra)
    out["tool_names"] = sorted(names)
    out["tool_calls_count"] = calls
    out["tool_results_count"] = results
    out["has_execution_tool_call"] = any(
        x in {"ssh_run_command", "kubernetes_query", "ansible_run_template"} for x in names
    )
    out["has_planning_tool_call"] = any(x in PLANNING_PLATFORM_TOOLS for x in names)
    return out


def _task_is_diagnostic_only(task: dict[str, Any]) -> bool:
    kind = str(task.get("action_kind") or "").strip().lower()
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    if kind == "ansible_run_template":
        return False
    if kind == "kubernetes_query":
        op = str(payload.get("operation") or "").strip().lower()
        return op in {"nodes", "pods", "events", "version", "namespaces", "cluster-info", "cluster_info"}
    if kind != "ssh_run_command":
        return False
    commands: list[str] = []
    one = str(payload.get("command") or "").strip()
    if one:
        commands.append(one)
    many = payload.get("commands")
    if isinstance(many, list):
        commands.extend(str(x or "").strip() for x in many if str(x or "").strip())
    if not commands:
        return False
    action_markers = (
        "docker compose up",
        "docker-compose up",
        "docker run",
        "docker pull",
        "docker build",
        "kubectl apply",
        "helm upgrade",
        "ansible-playbook",
        "systemctl restart",
        "systemctl start",
        "git clone",
        "git pull",
        "npm run build",
        "npm run start",
        "pm2 restart",
        "nginx -s reload",
    )
    diagnostic_markers = (
        "uname",
        "whoami",
        "pwd",
        "ls ",
        "docker --version",
        "kubectl version",
        "echo docker_not_found",
    )
    has_action = any(any(m in c.lower() for m in action_markers) for c in commands)
    if has_action:
        return False
    return all(any(m in c.lower() for m in diagnostic_markers) for c in commands)


def _should_force_tool_analysis(step_key: str, execution_change: bool) -> bool:
    if step_key not in ("devops_plan", "backend_plan", "frontend_plan"):
        return False
    return execution_change


def _build_system(
    step_key: str,
    role_prompt: str,
    ctx: str,
    docs_ctx: str,
    accumulated_md: str,
    user_message: str,
    *,
    execution_ctx: str = "",
    direct_answer: bool = False,
    infra_restrictions: str = "",
) -> str:
    if step_key == "intake":
        extra = STEP_EXTRA.get("intake", "")
        um = _truncate_user_message_for_intake(user_message)
        return (
            "You are a router for AI Team requests. Only decide: full pipeline (full) or "
            "a single-specialist reply (direct) and for direct — which role. Do not spell out work stages, "
            "do not gather product requirements — that is later steps.\n\n"
            f"{extra}\n\n---\n"
            "For intake you must not rely on project context/docs — only the user request wording.\n\n"
            "Reply with orchestration_route JSON only. "
            "Do not invent facts — rely only on the request wording.\n\n"
            f"User request:\n{um}"
        )

    extra = STEP_EXTRA.get(step_key, "")
    if direct_answer and step_key in (
        "pm",
        "architect",
        "devops_plan",
        "backend_plan",
        "frontend_plan",
        "qa_validation",
    ):
        extra += (
            "\n\nDirect-answer mode: the user asked a narrow question. Give a concrete answer on point, "
            "without a long multi-stage plan for the whole product. Do not invent facts from context."
        )
    acc = accumulated_md.strip() or "(previous steps have not produced conclusions yet)"
    ex = execution_ctx.strip() or "(none yet — execution_plan step and workers not finished)"
    restrictions = f"{infra_restrictions}\n\n---\n" if infra_restrictions else ""
    docs_block = (
        f"Project documentation (wiki / RAG):\n{docs_ctx}\n\n---\n"
        if (docs_ctx or "").strip()
        else f"{WIKI_ON_DEMAND_HINT}\n\n---\n"
    )
    tail = (
        f"{role_prompt}\n\n---\n{extra}\n\n---\n"
        f"{restrictions}"
        f"Project context (development-context):\n{ctx}\n\n---\n"
        f"{docs_block}"
        f"Accumulated step results:\n{acc}\n\n---\n"
        f"Execution facts (structured tasks, workers):\n{ex}\n\n---\n"
        "Reply in English, except the execution_plan step — JSON only per instructions. "
        "Do not invent secrets or URLs — use context only."
        f"\n\nUser request:\n{user_message}"
    )
    if step_key in STRUCTURED_STEP_KEYS:
        tail += STEP_OUTPUT_JSON_INSTRUCTIONS
    return tail


def run_orchestration_sync(project_id: UUID, run_id: str, user_message: str) -> None:
    pid = str(project_id)
    user_message = user_message.strip()
    cache_latest_run_id(pid, run_id)
    try:
        existing_run = team_get_run(run_id) or {}
        existing_status = str(existing_run.get("status") or "")
        prev_steps_raw = existing_run.get("steps") if isinstance(existing_run.get("steps"), list) else []
        prev_steps: list[dict[str, Any]] = [s for s in prev_steps_raw if isinstance(s, dict)]
        incoming_message = user_message
        original_user_message = str(existing_run.get("user_message") or "").strip()
        paused_step_key = ""
        if existing_status == "paused":
            for s in sorted(prev_steps, key=lambda x: int(x.get("seq") or 999)):
                st = str(s.get("status") or "")
                structured = s.get("structured") if isinstance(s.get("structured"), dict) else {}
                if st == "done" and structured.get("paused_for_user") is True:
                    paused_step_key = str(s.get("step_key") or "").strip()
                    break
        resume_from_paused = bool(paused_step_key)
        continue_existing_run = existing_status in ("done", "error")
        if resume_from_paused:
            base = original_user_message or incoming_message
            user_message = f"{base}\n\nUser clarification for the paused step:\n{incoming_message}"
        elif continue_existing_run:
            base = original_user_message or incoming_message
            user_message = f"{base}\n\nNew user message within the same run:\n{incoming_message}"
        else:
            user_message = incoming_message
        if resume_from_paused:
            team_patch_run(run_id, status="running", phase="resume_pipeline")
        elif continue_existing_run:
            team_patch_run(run_id, status="running", phase="continue_pipeline", error="")
        else:
            team_patch_run(run_id, status="running", phase="pipeline")
        tenant_id = fetch_project_tenant_id(pid)
        prompts = fetch_development_prompts()
        ctx = ""
        docs_ctx = ""
        infra_restrictions = ""
        accumulated: dict[str, str] = {}
        execution_ctx = ""
        if resume_from_paused:
            for s in sorted(prev_steps, key=lambda x: int(x.get("seq") or 999)):
                key = str(s.get("step_key") or "")
                if not key or key == paused_step_key:
                    break
                if str(s.get("status") or "") != "done":
                    continue
                out = str(s.get("output") or "").strip()
                if out:
                    accumulated[key] = out
            if isinstance(existing_run.get("execution_tasks"), list) and existing_run.get("execution_tasks"):
                try:
                    execution_ctx = format_execution_tasks_markdown(existing_run)
                except Exception:
                    execution_ctx = ""
        elif continue_existing_run:
            for s in sorted(prev_steps, key=lambda x: int(x.get("seq") or 999)):
                key = str(s.get("step_key") or "")
                if not key or str(s.get("status") or "") != "done":
                    continue
                out = str(s.get("output") or "").strip()
                if out:
                    accumulated[key] = out
            if isinstance(existing_run.get("execution_tasks"), list) and existing_run.get("execution_tasks"):
                try:
                    execution_ctx = format_execution_tasks_markdown(existing_run)
                except Exception:
                    execution_ctx = ""
        run_started = monotonic()
        step_invocations: dict[str, int] = {}
        step_error_signatures: dict[str, str] = {}
        step_error_repeats: dict[str, int] = {}
        step_output_signatures: dict[str, str] = {}
        step_output_repeats: dict[str, int] = {}
        role_execution_evidence: dict[str, dict[str, Any]] = {}
        orchestration_route: dict[str, Any] = {}
        route_obj0 = existing_run.get("architecture_json") if isinstance(existing_run.get("architecture_json"), dict) else {}
        inner0 = route_obj0.get("orchestration_route") if isinstance(route_obj0.get("orchestration_route"), dict) else {}
        if inner0:
            orchestration_route = inner0
        dag_layers = active_dag_layers(orchestration_route)
        # Live execution mode: intake orchestration_route.live_execution and live_execution envelope on steps.
        role_execution_mode = live_execution_from_route(orchestration_route)
        skip_role_execution_tool_gate = resume_from_paused and user_requests_tool_execution_approval(
            incoming_message
        )
        if skip_role_execution_tool_gate:
            role_execution_mode = True
            if paused_step_key:
                team_patch_step(
                    run_id,
                    paused_step_key,
                    status="running",
                    output_md="⏳ Continuing with real actions (platform tools)…",
                    structured={"live_execution_approved": True},
                )
                team_patch_run(run_id, status="running", phase="resume_live_execution", error="")
        for s in prev_steps:
            if not isinstance(s, dict):
                continue
            key = str(s.get("step_key") or "").strip()
            if not key:
                continue
            structured = s.get("structured") if isinstance(s.get("structured"), dict) else {}
            ev = structured.get("role_execution_evidence") if isinstance(structured, dict) else None
            if isinstance(ev, dict):
                role_execution_evidence[key] = ev

        def _lf_cfg(*, obs_step_key: str | None = None) -> dict[str, Any]:
            return langfuse_tracing.invoke_config(orchestration_run_id=run_id, step_key=obs_step_key)

        def _accumulated_md(extra: dict[str, str] | None = None) -> str:
            merged = dict(accumulated)
            if extra:
                merged.update(extra)
            if not merged:
                return ""
            keys = sorted(merged.keys(), key=lambda k: STEP_ORDER.get(k, 999))
            chunks: list[str] = []
            for k in keys:
                label, _task = STEP_META.get(k, (k, ""))
                chunks.append(f"### {label}\n{merged[k]}")
            return "\n\n".join(chunks)

        def _accumulated_md_agent_steps_only(extra: dict[str, str] | None = None) -> str:
            """For pm_summary: only role-step outputs, without execution_plan/implementation/deploy and without bloat."""
            merged = {k: v for k, v in accumulated.items() if k in PM_SUMMARY_SOURCE_STEP_KEYS}
            if extra:
                for ek, ev in extra.items():
                    if ek in PM_SUMMARY_SOURCE_STEP_KEYS:
                        merged[ek] = ev
            if not merged:
                return ""
            keys = sorted(merged.keys(), key=lambda k: STEP_ORDER.get(k, 999))
            cap = PM_SUMMARY_STEP_BODY_MAX_CHARS
            chunks: list[str] = []
            for k in keys:
                label, _task = STEP_META.get(k, (k, ""))
                body = str(merged.get(k) or "").strip()
                if len(body) > cap:
                    body = (
                        body[:cap]
                        + "\n\n… [step output truncated for the final report; full text is in the orchestration panel]"
                    )
                chunks.append(f"### {label}\n{body}")
            return "\n\n".join(chunks)

        def _consult_peer(role: str, question: str, requester_step: str) -> str:
            role_l = (role or "").strip().lower()
            peer_step = PRIMARY_ROLE_TO_STEP.get(role_l)
            if not peer_step:
                return (
                    f"[role {role!r} is not supported for peer sync — "
                    "use: pm, architect, devops, backend, frontend]"
                )
            lbl, _tsk = STEP_META.get(peer_step, (peer_step, ""))
            pr_key = STEP_PROMPT_ROLE.get(peer_step, "pm")
            peer_prompt = prompts.get(pr_key, "You are a development team assistant.")
            peer_ctx = _ctx_trim(ctx, 6000)
            acc_now = _accumulated_md()
            system_peer = (
                f"{peer_prompt}\n\n---\n"
                "Internal request from an AI Team colleague. Reply briefly and on point so the colleague can "
                f"continue the \"{requester_step}\" step. Do not address the end user — reply to the colleague only.\n"
                "Do not invent successful actions, URLs, or test results without facts from context.\n\n"
                f"Project context:\n{peer_ctx}\n\n---\n"
                f"Accumulated step results:\n{acc_now}\n\n---\n"
                f"Question from colleague (pipeline step: {requester_step}; consultant: {lbl}):\n{question.strip()}"
            )
            model = build_chat_model(purpose=_llm_purpose_for_step(peer_step))
            text = _stream_chat_then_sanitize(
                model,
                [
                    SystemMessage(content=system_peer),
                    HumanMessage(content="Give a reply to the colleague so the pipeline can continue."),
                ],
                _lf_cfg(obs_step_key=f"peer:{peer_step}"),
                run_id=run_id,
                step_key=requester_step,
            )
            return (text or "").strip()

        def _upsert_doc(
            *,
            doc_type: str,
            title: str,
            content_md: str,
            slug: str,
            change_summary: str,
        ) -> None:
            content = (content_md or "").strip()
            if not content:
                return
            payload = {
                "doc_type": doc_type,
                "title": title,
                "content_md": content,
                "slug": slug,
                "tags": ["auto", "orchestration", doc_type],
                "change_summary": change_summary,
                "author_type": "agent",
                "author_id": "agent-graph-orchestrator",
                "run_id": run_id,
            }
            with httpx.Client(timeout=60.0) as client:
                r = client.post(
                    f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/documents/upsert",
                    headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                    json=payload,
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"documents upsert {doc_type} failed: http {r.status_code}")

        def _persist_docs_after_run() -> None:
            arch = (accumulated.get("architect") or "").strip()
            impl = (accumulated.get("implementation") or "").strip()
            deploy = (accumulated.get("deploy") or "").strip()
            summary = (accumulated.get("pm_summary") or "").strip()
            if not (arch or impl or deploy or summary):
                return
            architecture_doc = "\n\n".join(
                [
                    x
                    for x in [
                        arch,
                        accumulated.get("devops_plan") or "",
                        accumulated.get("backend_plan") or "",
                        accumulated.get("frontend_plan") or "",
                    ]
                    if (x or "").strip()
                ]
            ).strip()
            _upsert_doc(
                doc_type="architecture",
                title="Architecture Overview",
                slug="architecture-overview",
                content_md=architecture_doc or summary,
                change_summary="Auto-sync architecture from orchestration run",
            )
            user_doc = "\n\n".join(
                [x for x in [summary, accumulated.get("frontend_plan") or "", deploy] if (x or "").strip()]
            ).strip()
            _upsert_doc(
                doc_type="user_guide",
                title="User Guide",
                slug="user-guide",
                content_md=user_doc,
                change_summary="Auto-sync user guide from orchestration run",
            )
            admin_doc = "\n\n".join(
                [
                    x
                    for x in [
                        accumulated.get("devops_plan") or "",
                        accumulated.get("backend_plan") or "",
                        impl,
                        deploy,
                        accumulated.get("qa_validation") or "",
                    ]
                    if (x or "").strip()
                ]
            ).strip()
            _upsert_doc(
                doc_type="admin_guide",
                title="Administrator Guide",
                slug="administrator-guide",
                content_md=admin_doc,
                change_summary="Auto-sync admin guide from orchestration run",
            )

        def _execution_blockers(run_obj: dict[str, Any]) -> list[dict[str, str]]:
            tasks = run_obj.get("execution_tasks") if isinstance(run_obj.get("execution_tasks"), list) else []
            blockers: list[dict[str, str]] = []
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                st = str(t.get("status") or "")
                title = str(t.get("title") or "task")
                kind = str(t.get("action_kind") or "action")
                if st != "done":
                    blockers.append(
                        {
                            "title": title,
                            "action_kind": kind,
                            "status": st or "unknown",
                            "error": str(t.get("error") or ""),
                        }
                    )
                    continue
                res = t.get("result")
                if isinstance(res, dict) and res.get("ok") is False:
                    blockers.append(
                        {
                            "title": title,
                            "action_kind": kind,
                            "status": "done_but_not_ok",
                            "error": str(t.get("error") or res.get("error") or ""),
                        }
                    )
                    continue
                # Extra guard against "false ok": old/nonstandard results may have been done,
                # but with an error in the nested payload.
                if kind == "ssh_run_command" and isinstance(res, dict):
                    rows = res.get("results") if isinstance(res.get("results"), list) else []
                    nested_bad = False
                    nested_err = ""
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        r = row.get("response") if isinstance(row.get("response"), dict) else {}
                        if not isinstance(r, dict):
                            continue
                        ok_nested = bool(r.get("ok", False))
                        rc_nested = int(r.get("exit_code") or 0)
                        if ok_nested and rc_nested == 0:
                            continue
                        nested_bad = True
                        nested_err = str(r.get("stderr") or r.get("stdout") or "").strip()
                        break
                    if nested_bad:
                        blockers.append(
                            {
                                "title": title,
                                "action_kind": kind,
                                "status": "done_but_nested_error",
                                "error": nested_err[:600],
                            }
                        )
                        continue
                if kind == "ansible_run_template" and isinstance(res, dict):
                    rr = res.get("response") if isinstance(res.get("response"), dict) else {}
                    if isinstance(rr, dict) and rr.get("ok") is False:
                        fb = rr.get("fallback_result") if isinstance(rr.get("fallback_result"), dict) else {}
                        err = ""
                        if isinstance(fb, dict):
                            err = str(fb.get("stderr") or fb.get("stdout") or "").strip()
                        if not err:
                            err = str(rr.get("detail") or "").strip()
                        blockers.append(
                            {
                                "title": title,
                                "action_kind": kind,
                                "status": "done_but_nested_error",
                                "error": err[:600],
                            }
                        )
            return blockers

        def _execution_real_work_issues(run_obj: dict[str, Any]) -> list[str]:
            tasks = run_obj.get("execution_tasks") if isinstance(run_obj.get("execution_tasks"), list) else []
            if not tasks:
                roles = ("devops_plan", "backend_plan", "frontend_plan")
                done_roles = [r for r in roles if str((next((x for x in prev_steps if str(x.get("step_key") or "") == r), {}) or {}).get("status") or "") == "done"]
                if not done_roles and not any(k in role_execution_evidence for k in roles):
                    return ["No execution_tasks and no confirmed evidence from specialist roles."]
                ev_rows = [role_execution_evidence.get(r, {}) for r in roles]
                has_exec = any(isinstance(ev, dict) and ev.get("has_execution_tool_call") is True for ev in ev_rows)
                has_tool_results = any(int(ev.get("tool_results_count") or 0) > 0 for ev in ev_rows if isinstance(ev, dict))
                if not has_exec:
                    return ["Specialist roles did not perform real tool actions (ssh/k8s/ansible)."]
                if not has_tool_results:
                    return ["Specialist roles have no tool-call results; actual execution is not confirmed."]
                return []
            executable = [
                t
                for t in tasks
                if isinstance(t, dict)
                and str(t.get("action_kind") or "").strip().lower() in EXECUTABLE_ACTION_KINDS
            ]
            if not executable:
                return ["execution_tasks has no executable actions (ssh/k8s/ansible)."]
            done_exec = [t for t in executable if str(t.get("status") or "") == "done"]
            if not done_exec:
                return ["Executable actions exist, but none finished with status done."]
            if all(_task_is_diagnostic_only(t) for t in done_exec):
                return ["Only diagnostic checks ran, not deploy/change steps."]
            evidence_text = _extract_text_blobs(done_exec).lower()
            if "docker_not_found" in evidence_text:
                return ["Docker is missing on the target host (docker_not_found); deploy could not run."]
            return []

        def _pause_run_with_question(step_key: str, questions: list[str] | None = None, fallback: str = "") -> None:
            label, _task = STEP_META.get(step_key, (step_key, ""))
            q0 = ""
            if isinstance(questions, list):
                for q in questions:
                    s = str(q or "").strip()
                    if s:
                        q0 = s
                        break
            if not q0:
                q0 = (fallback or "User clarification is required to continue the step.").strip()
            team_patch_run(
                run_id,
                status="paused",
                phase="awaiting_clarification",
                error=f"{label}: {q0}"[:1200],
            )

        def _run_single_step_text(
            step_key: str,
            *,
            direct_answer: bool = False,
            acc_override: str | None = None,
            debug_hint: str = "",
        ) -> str:
            nonlocal ctx, docs_ctx, infra_restrictions, role_execution_evidence, skip_role_execution_tool_gate
            label, _task = STEP_META.get(step_key, (step_key, ""))
            pr_key = STEP_PROMPT_ROLE.get(step_key, "pm")
            role_prompt = prompts.get(pr_key, "You are a development team assistant.")
            if step_key != "intake" and not ctx:
                ctx = fetch_development_context_summary(pid)
            if step_key != "intake" and not docs_ctx and INLINE_RAG_IN_PROMPT:
                docs_ctx = fetch_documents_context_summary(pid, user_message)
            if step_key != "intake" and not infra_restrictions:
                infra_restrictions = _infer_infra_restrictions(ctx)
            acc_for_system = acc_override if acc_override is not None else _accumulated_md()
            exec_for_system = execution_ctx
            ctx_for_system = ctx
            docs_for_system = docs_ctx
            infra_for_system = infra_restrictions
            if step_key == "pm_summary":
                acc_for_system = acc_override if acc_override is not None else _accumulated_md_agent_steps_only()
                run_now = team_get_run(run_id) or {}
                exec_for_system = _compact_execution_tasks_summary(run_now)
                ctx_for_system = _ctx_trim(ctx, PM_SUMMARY_CTX_MAX_CHARS)
                docs_for_system = _ctx_trim(docs_ctx, PM_SUMMARY_DOCS_MAX_CHARS)
                if infra_restrictions:
                    infra_for_system = _ctx_trim(infra_restrictions, 6000)
            system_full = _build_system(
                step_key,
                role_prompt,
                ctx_for_system,
                docs_for_system,
                acc_for_system,
                user_message,
                execution_ctx=exec_for_system,
                direct_answer=direct_answer,
                infra_restrictions=infra_for_system,
            )
            if debug_hint:
                system_full += f"\n\n---\nCross-role collaborative debugging:\n{debug_hint}"
            human_content = (
                "Routing: JSON only per the system instruction."
                if step_key == "intake"
                else f'Execute step "{label}" ({step_key}).'
            )
            if _should_force_tool_analysis(step_key, role_execution_mode):
                analysis_prompt = (
                    f"{user_message}\n\n"
                    "This is a change task for an existing project. "
                    "Step 1 (required): gather facts via available tools. "
                    "Step 2: analyze the current state from the facts. "
                    "Do not invent facts, file paths, or results."
                )
                graph_pv = f"### LangGraph ReAct (tools)\n\n{analysis_prompt}"
                _publish_llm_prompt_meta(run_id, step_key, graph_pv, "langgraph-react")
                team_patch_step(
                    run_id,
                    step_key,
                    status="running",
                    output_md="⏳ LangGraph ReAct: gathering facts via tools (may take several minutes)…",
                )
                graph_result = invoke_graph(project_id, analysis_prompt, orchestration_run_id=run_id)
                role_execution_evidence[step_key] = _collect_graph_execution_evidence(graph_result)
                analysis_text = _extract_graph_text(graph_result)
                if analysis_text:
                    role_execution_evidence[step_key] = _merge_tool_evidence_from_text(
                        role_execution_evidence.get(step_key) or {},
                        analysis_text,
                    )
                if not analysis_text:
                    return (
                        "Tool analysis finished, but no final reply text was returned. "
                        "Repeat the request so the agent can form an output from the facts."
                    )
                model = build_chat_model(purpose=_llm_purpose_for_step(step_key))
                final_messages = [
                    SystemMessage(content=system_full),
                    HumanMessage(
                        content=(
                            f"Execute step \"{label}\" ({step_key}) based on the facts below.\n\n"
                            "Required response format:\n"
                            "1) Current state analysis (facts)\n"
                            "2) Change plan\n"
                            "3) Self-check (what may be wrong / what else to verify)\n\n"
                            f"Tool-analysis facts:\n{analysis_text}"
                        )
                    ),
                ]
                return _stream_chat_then_sanitize(
                    model, final_messages, _lf_cfg(obs_step_key=step_key), run_id=run_id, step_key=step_key
                )
            if direct_answer and step_key in DIRECT_TOOL_STEPS:
                if gitlab_repo_create_intent(user_message):
                    slug = parse_repo_slug_from_message(user_message) or "app"
                    team_patch_step(
                        run_id,
                        step_key,
                        status="running",
                        output_md=f"⏳ GitLab: creating repository `{slug}`…",
                    )
                    ok_gl, gl_text = execute_gitlab_create_repository(project_id, slug)
                    role_execution_evidence[step_key] = {
                        "tool_calls_count": 1 if ok_gl else 0,
                        "tool_results_count": 1 if ok_gl else 0,
                        "tool_names": ["gitlab_create_repository"] if ok_gl else [],
                        "has_execution_tool_call": False,
                        "gitlab_deterministic_fallback": True,
                    }
                    if ok_gl:
                        return _sanitize_model_text(gl_text)
                graph_prompt = user_message
                if gitlab_repo_create_intent(user_message):
                    graph_prompt = (
                        f"{user_message}\n\n"
                        "MUST call tools via function calling (do not write JSON in the text): "
                        "gitlab_provision_status → gitlab_create_repository. "
                        f"repo_slug for create: {parse_repo_slug_from_message(user_message) or 'app'}."
                    )
                else:
                    graph_prompt = (
                        f"{user_message}\n\n"
                        "MUST use function calling (not JSON and not a markdown block with name/arguments). "
                    )
                    if role_execution_mode:
                        graph_prompt += (
                            "Real changes are needed: GitLab (provision/repos/commit), SSH/ansible on deploy. "
                            "Do not call run_product_planning for edits to an existing project."
                        )
                    else:
                        graph_prompt += (
                            "First get_development_context, then ssh_run_command on target_id from "
                            "ssh_tools.ssh_targets_for_tools (not a placeholder). "
                            "Reply only from tool result facts."
                        )
                graph_pv = f"### LangGraph ReAct (direct answer)\n\n{graph_prompt}"
                _publish_llm_prompt_meta(run_id, step_key, graph_pv, "langgraph-react")
                team_patch_step(
                    run_id,
                    step_key,
                    status="running",
                    output_md="⏳ LangGraph ReAct: tools and forming the reply…",
                )
                graph_result = invoke_graph(project_id, graph_prompt, orchestration_run_id=run_id)
                role_execution_evidence[step_key] = _collect_graph_execution_evidence(graph_result)
                ev = role_execution_evidence[step_key]
                if (
                    gitlab_repo_create_intent(user_message)
                    and int(ev.get("tool_calls_count") or 0) == 0
                ):
                    slug = parse_repo_slug_from_message(user_message) or "app"
                    ok_gl, gl_text = execute_gitlab_create_repository(project_id, slug)
                    ev["gitlab_deterministic_fallback"] = True
                    ev["tool_calls_count"] = 1 if ok_gl else 0
                    ev["tool_results_count"] = 1 if ok_gl else 0
                    if ok_gl:
                        ev["tool_names"] = ["gitlab_create_repository"]
                        role_execution_evidence[step_key] = ev
                        return _sanitize_model_text(gl_text)
                text = _extract_graph_text(graph_result)
                if not text:
                    return (
                        "Tool run finished, but no final reply text was returned. "
                        "Repeat the request so the agent can form a final output from tool results."
                    )
                if gitlab_repo_create_intent(user_message) and int(ev.get("tool_calls_count") or 0) == 0:
                    return (
                        "The model described a tool call in text but did not execute it. "
                        "Check GitLab (project group, GITLAB_PROVISION_MOCK=0, token) and retry.\n\n"
                        + _sanitize_model_text(text)
                    )
                if int(ev.get("tool_calls_count") or 0) == 0:
                    if not role_execution_mode:
                        ok_ops, ops_text = execute_operational_ssh_probe(
                            project_id, command=OPS_PROBE_COMMAND
                        )
                        if ok_ops:
                            ev["operational_deterministic_fallback"] = True
                            ev["tool_calls_count"] = 2
                            ev["tool_results_count"] = 2
                            ev["tool_names"] = ["get_development_context", "ssh_run_command"]
                            ev["has_execution_tool_call"] = True
                            role_execution_evidence[step_key] = ev
                            return _sanitize_model_text(ops_text)
                        if looks_like_fake_tool_call_text(text):
                            return (
                                "The model returned a tool-call JSON as text but did not perform function calling. "
                                "Retry or enable a model with native tool calling.\n\n"
                                + _sanitize_model_text(text)
                                + "\n\n---\n"
                                + ops_text
                            )
                        return (
                            "ReAct did not call tools; the platform SSH fallback could not run.\n\n"
                            + ops_text
                            + ("\n\n" + _sanitize_model_text(text) if text else "")
                        )
                    if looks_like_fake_tool_call_text(text):
                        return (
                            "The model described a tool call in text but did not perform function calling. "
                            "Retry or rebuild llm-adapter-service (Ollama tool_calls coerce).\n\n"
                            + _sanitize_model_text(text)
                        )
                    return (
                        "live_execution request was not completed: no tool calls. "
                        "Retry with confirmation \"yes, execute\" or a model with tool calling.\n\n"
                        + (_sanitize_model_text(text) if text else "")
                    )
                return _sanitize_model_text(text)
            if step_key == "sa":
                graph_prompt = (
                    f"{user_message}\n\n"
                    f"Orchestration run id (run_id for wiki and tracker meta): {run_id}\n\n"
                    "Run the systems-analyst step via function calling (not JSON in text):\n"
                    "- upsert_project_document: spec, glossary, traceability (mermaid in content_md where needed).\n"
                    "- tracker_create_epic / tracker_create_task with assignee_role and meta run_id, requirement_id.\n"
                    "- tracker_list_items at the end for verification.\n"
                    "Brief summary in the reply: what was created (slug, card ids)."
                )
                graph_pv = f"### LangGraph ReAct (SA)\n\n{graph_prompt}"
                _publish_llm_prompt_meta(run_id, step_key, graph_pv, "langgraph-react")
                team_patch_step(
                    run_id,
                    step_key,
                    status="running",
                    output_md="⏳ SA: wiki artifacts and tracker cards…",
                )
                graph_result = invoke_graph(project_id, graph_prompt, orchestration_run_id=run_id)
                role_execution_evidence[step_key] = _collect_graph_execution_evidence(graph_result)
                text = _extract_graph_text(graph_result)
                if not text:
                    return (
                        "SA step ran via tools, but final text was not formed. "
                        "Check wiki and tracker in the UI."
                    )
                ev = role_execution_evidence.get(step_key) or {}
                if int(ev.get("tool_calls_count") or 0) == 0:
                    return (
                        "SA did not call tools (wiki/tracker). Retry the run or enable a model with tool calling.\n\n"
                        + _sanitize_model_text(text)
                    )
                return _sanitize_model_text(text)
            if step_key == "intake":
                intake_model_override = (os.getenv("AGENT_ORCHESTRATION_INTAKE_MODEL") or "").strip() or None
                model = build_chat_model(
                    purpose="default",
                    timeout_sec=INTAKE_LLM_TIMEOUT_SEC,
                    model_override=intake_model_override,
                )
                invoker = model.bind(max_tokens=INTAKE_MAX_TOKENS, temperature=0)
                stream_intake = False
            else:
                model = build_chat_model(purpose=_llm_purpose_for_step(step_key))
                invoker = model
                stream_intake = True
            messages = [
                SystemMessage(content=system_full),
                HumanMessage(content=human_content),
            ]
            return _stream_chat_then_sanitize(
                invoker,
                messages,
                _lf_cfg(obs_step_key=step_key),
                run_id=run_id,
                step_key=step_key,
                stream=stream_intake,
            )

        def _execute_step(
            step_key: str,
            *,
            direct_answer: bool = False,
            fail_run_on_error: bool = True,
            acc_override: str | None = None,
            debug_hint: str = "",
        ) -> tuple[bool, str | None, str | None, bool]:
            """Last bool: halt_pipeline — stop the run (waiting for user reply)."""
            nonlocal execution_ctx, role_execution_mode, dag_layers, orchestration_route
            label, _task = STEP_META.get(step_key, (step_key, ""))
            if step_key == "sa" and not route_includes_sa(orchestration_route):
                team_patch_step(
                    run_id,
                    step_key,
                    status="skipped",
                    output_md="Skipped: intake did not include the systems analyst (sa).",
                    finished_at=_utc_iso(),
                )
                return True, "Skipped: SA not in orchestration_route.roles.", None, False
            step_invocations[step_key] = step_invocations.get(step_key, 0) + 1
            if step_invocations[step_key] > MAX_STEP_INVOCATIONS_PER_RUN:
                err = (
                    f"loop_detected: step {step_key} ran too many times "
                    f"({step_invocations[step_key]} > {MAX_STEP_INVOCATIONS_PER_RUN})"
                )
                team_patch_step(run_id, step_key, status="error", error=err, finished_at=_utc_iso())
                if fail_run_on_error:
                    team_patch_run(run_id, status="error", error=f"{label}: {err}", phase="failed")
                return False, None, err, False
            if monotonic() - run_started > MAX_ORCHESTRATION_SECONDS:
                err = (
                    "loop_detected: orchestration time limit exceeded "
                    f"({MAX_ORCHESTRATION_SECONDS}s)"
                )
                team_patch_step(run_id, step_key, status="error", error=err, finished_at=_utc_iso())
                if fail_run_on_error:
                    team_patch_run(run_id, status="error", error=f"{label}: {err}", phase="failed")
                return False, None, err, False
            if step_key in ("implementation", "deploy", "qa_validation", "pm_summary"):
                run_now = team_get_run(run_id) or {}
                blockers_now = _execution_blockers(run_now)
                dod_issues = dod_gate_issues_for_run(
                    step_key=step_key,
                    role_execution_mode=role_execution_mode,
                    run_obj=run_now,
                    role_execution_evidence=role_execution_evidence,
                    prev_steps=prev_steps,
                )
                real_work_issues = list(dod_issues)
                if role_execution_mode:
                    real_work_issues.extend(_execution_real_work_issues(run_now))
                if blockers_now:
                    lines = []
                    for b in blockers_now[:8]:
                        er = f" · {b['error']}" if b.get("error") else ""
                        lines.append(f"- {b['title']} (`{b['action_kind']}`): {b['status']}{er}")
                    msg = (
                        "Step blocked: execution tasks have errors/unfinished work.\n"
                        + "\n".join(lines)
                    )
                    team_patch_step(
                        run_id,
                        step_key,
                        status="done",
                        output_md=msg,
                        structured={
                            "paused_for_user": True,
                            "execution_blockers": blockers_now,
                            "questions_for_user": [
                                "To continue deploy/summary, first clear execution blockers: fix the environment, retry tasks, or confirm a manual bypass."
                            ],
                        },
                        finished_at=_utc_iso(),
                    )
                    accumulated[step_key] = msg
                    _pause_run_with_question(
                        step_key,
                        ["To continue deploy/summary, first clear execution blockers: fix the environment, retry tasks, or confirm a manual bypass."],
                        "There are execution blockers.",
                    )
                    return True, msg, None, True
                if real_work_issues:
                    dod_only = bool(dod_issues) and not role_execution_mode
                    title = (
                        "DoD not met — the step cannot be marked successful."
                        if dod_only or dod_issues
                        else "Step blocked: no confirmation of actual change execution."
                    )
                    msg = title + "\n" + "\n".join(f"- {x}" for x in real_work_issues)
                    questions = dod_gate_user_questions(dod_issues) if dod_issues else [
                        "Real deploy/change steps are required (not only diagnostics). "
                        "Allow retry of specialist steps with mandatory tool-calling?"
                    ]
                    team_patch_step(
                        run_id,
                        step_key,
                        status="done",
                        output_md=msg,
                        structured={
                            "paused_for_user": True,
                            "dod_gate_failed": bool(dod_issues),
                            "dod_issues": dod_issues,
                            "execution_real_work_issues": real_work_issues,
                            "questions_for_user": questions,
                        },
                        finished_at=_utc_iso(),
                    )
                    accumulated[step_key] = msg
                    _pause_run_with_question(
                        step_key,
                        questions,
                        "DoD / execution not confirmed." if dod_issues else "No confirmation of actual change execution.",
                    )
                    return True, msg, None, True

            team_patch_step(run_id, step_key, status="running", started_at=_utc_iso())
            last_err = "unknown error"
            max_retries = MAX_INTAKE_RETRIES if step_key == "intake" else MAX_STEP_RETRIES
            for attempt in range(1, max_retries + 1):
                try:
                    text = _run_single_step_text(
                        step_key,
                        direct_answer=direct_answer,
                        acc_override=acc_override,
                        debug_hint=debug_hint,
                    )
                    if step_key == "intake":
                        parsed_route = parse_orchestration_route(text or "")
                        if not route_is_usable(parsed_route):
                            raise RuntimeError(
                                "intake_route_invalid: model did not return a usable orchestration_route JSON; retrying"
                            )
                    sig_out = _text_fingerprint(text)
                    prev_out = step_output_signatures.get(step_key, "")
                    if sig_out and sig_out == prev_out:
                        step_output_repeats[step_key] = step_output_repeats.get(step_key, 1) + 1
                    else:
                        step_output_signatures[step_key] = sig_out
                        step_output_repeats[step_key] = 1
                    if step_output_repeats.get(step_key, 1) >= MAX_IDENTICAL_OUTPUT_REPEATS and attempt < max_retries:
                        raise RuntimeError(
                            "loop_detected: model repeats the same output; trying to rephrase the step"
                        )

                    structured_payload: dict[str, Any] = {
                        "attempt": attempt,
                        "max_attempts": max_retries,
                    }
                    if direct_answer and step_key in ("devops_plan", "backend_plan", "frontend_plan"):
                        ev = role_execution_evidence.get(step_key)
                        if isinstance(ev, dict):
                            structured_payload["role_execution_evidence"] = ev
                        # Direct frontend/backend: without tool calls the step is not successful (except explicit pause above).
                        if step_key in ("frontend_plan", "backend_plan"):
                            calls = int((ev or {}).get("tool_calls_count") or 0)
                            has_planning = bool((ev or {}).get("has_planning_tool_call"))
                            out_check = text or ""
                            if (
                                not skip_role_execution_tool_gate
                                and calls <= 0
                                and not has_planning
                                and (
                                    looks_like_fake_tool_call_text(out_check)
                                    or role_execution_mode
                                )
                            ):
                                raise RuntimeError(
                                    "direct_step_no_tools: model did not call platform tools "
                                    f"(tool_calls={calls}); creating a site/code needs function calling, "
                                    "rebuild llm-adapter (Ollama coerce) or full pipeline"
                                )
                        if role_execution_mode and not skip_role_execution_tool_gate:
                            calls = int((ev or {}).get("tool_calls_count") or 0)
                            results = int((ev or {}).get("tool_results_count") or 0)
                            has_exec = bool((ev or {}).get("has_execution_tool_call"))
                            has_planning = bool((ev or {}).get("has_planning_tool_call"))
                            # At *_plan stage, context/GitLab is enough; SSH is for execution_plan/deploy.
                            if step_key in PLAN_ROLE_STEPS:
                                needs_pause = calls <= 0 and not has_planning and not has_exec
                            else:
                                needs_pause = (calls <= 0 or results <= 0) or (
                                    step_key == "devops_plan" and not has_exec
                                )
                            if needs_pause:
                                msg = (
                                    "Pipeline paused: the agent described a plan in text but did not call platform tools "
                                    "(GitLab, project context, SSH).\n"
                                    f"- tool_calls: {calls}\n"
                                    f"- tool_results: {results}\n"
                                    f"- planning_tools (gitlab/context): {'yes' if has_planning else 'no'}\n"
                                    f"- ssh/k8s/ansible: {'yes' if has_exec else 'no'}\n\n"
                                    'To continue, send in chat: "yes, execute real actions" — '
                                    "or rebuild llm-adapter-service (Ollama tool_calls coerce)."
                                )
                                structured_payload["paused_for_user"] = True
                                structured_payload["questions_for_user"] = [
                                    'Confirm continuation with tool calls: reply "yes, execute" '
                                    'or "continue with tools".'
                                ]
                                team_patch_step(
                                    run_id,
                                    step_key,
                                    status="done",
                                    output_md=msg,
                                    structured=structured_payload,
                                    finished_at=_utc_iso(),
                                )
                                accumulated[step_key] = msg
                                _pause_run_with_question(
                                    step_key,
                                    [f"Allow retry of step {step_key} with mandatory tool-calling and factual results?"],
                                    "The specialist role did not confirm actual execution.",
                                )
                                return True, msg, None, True
                    halt_pipeline = False
                    apply_structured = step_key in STRUCTURED_STEP_KEYS and not (
                        direct_answer and step_key in DIRECT_TOOL_STEPS
                    )
                    if apply_structured:
                        env, _fb = parse_step_envelope(text)
                        if env:
                            if step_key == "architect" and looks_like_invalid_architect_output(
                                env.summary_md, user_message
                            ):
                                raise RuntimeError(
                                    "architect_invalid_output: model returned template/meta text "
                                    "instead of architecture for the request (site/promotions). "
                                    "Retry the step or switch the architect model (not qwen with chat-template leak)."
                                )
                            role_execution_mode = merge_live_execution_flag(
                                role_execution_mode,
                                orchestration_route,
                                step_envelope=env.to_dict(),
                            )
                            peer_responses: list[dict[str, Any]] = []
                            if env.peer_queries:
                                peer_parts: list[str] = []
                                for pq in env.peer_queries[:MAX_PEER_QUERIES]:
                                    ans = _consult_peer(pq.role, pq.question, step_key)
                                    peer_responses.append(
                                        {"role": pq.role, "question": pq.question, "answer": ans}
                                    )
                                    peer_parts.append(f"#### Reply ({pq.role})\n{ans}")
                                if peer_parts:
                                    env.summary_md = (
                                        env.summary_md
                                        + "\n\n---\n### Team sync (auto)\n\n"
                                        + "\n\n".join(peer_parts)
                                    )
                                    structured_payload["peer_responses"] = peer_responses
                            text = env.summary_md
                            structured_payload["step_envelope"] = env.to_dict()
                            halt_pipeline = (
                                env.step_status == "need_user_clarification"
                                or bool(env.questions_for_user)
                                or not env.pipeline_continue
                            )
                            if step_key == "qa_validation" and not halt_pipeline:
                                sm = (env.summary_md or "").lower()
                                if "release_recommendation" in sm and ("no_go" in sm or "no-go" in sm):
                                    halt_pipeline = True
                                    structured_payload["qa_gate"] = "no_go"
                                    if not env.questions_for_user:
                                        env.questions_for_user = [
                                            "QA returned NO_GO. Specify which defects block the release and how we plan to fix them."
                                        ]
                                        structured_payload["questions_for_user"] = env.questions_for_user
                            if env.questions_for_user:
                                structured_payload["questions_for_user"] = env.questions_for_user

                    if halt_pipeline:
                        structured_payload["paused_for_user"] = True
                        team_patch_step(
                            run_id,
                            step_key,
                            status="done",
                            output_md=text,
                            structured=structured_payload,
                            finished_at=_utc_iso(),
                        )
                        accumulated[step_key] = text
                        if step_key == "architect":
                            team_patch_run(run_id, architecture_md=text, phase="architecture_done")
                        _pause_run_with_question(
                            step_key,
                            structured_payload.get("questions_for_user") if isinstance(structured_payload.get("questions_for_user"), list) else None,
                            "The step is waiting for user clarification.",
                        )
                        return True, text, None, True

                    if step_key == "execution_plan":
                        batch_reports: list[str] = []
                        iteration = 1
                        planner_text = text
                        current_run = team_get_run(run_id) or {}
                        created_task_ids: set[str] = {
                            str(t.get("id"))
                            for t in (current_run.get("execution_tasks") or [])
                            if isinstance(t, dict) and t.get("id")
                        }
                        while iteration <= PLAN_MAX_ITERATIONS:
                            team_patch_run(run_id, phase=f"planning_batch_{iteration}")
                            parsed, planner_meta = parse_execution_plan_payload(planner_text)
                            parsed, norm_notes = normalize_worker_execution_tasks(parsed, ctx)
                            if norm_notes:
                                planner_meta = {**planner_meta, "task_normalization": norm_notes}
                            if not parsed and iteration == 1:
                                parsed = default_execution_tasks_if_empty(ctx)
                            if PLAN_MAX_TASKS_PER_ITER > 0:
                                parsed = parsed[:PLAN_MAX_TASKS_PER_ITER]
                            if role_execution_mode and parsed and not _has_executable_execution_tasks(parsed):
                                recheck_hint = (
                                    "Self-check: the batch has no executable tasks "
                                    "(ssh_run_command|kubernetes_query|ansible_run_template) for a change request. "
                                    "Rebuild the next batch so it contains real execution steps."
                                )
                                retry_text = _run_single_step_text(
                                    step_key,
                                    direct_answer=direct_answer,
                                    acc_override=acc_override,
                                    debug_hint=recheck_hint,
                                )
                                retry_parsed, retry_meta = parse_execution_plan_payload(retry_text)
                                if PLAN_MAX_TASKS_PER_ITER > 0:
                                    retry_parsed = retry_parsed[:PLAN_MAX_TASKS_PER_ITER]
                                if retry_parsed and _has_executable_execution_tasks(retry_parsed):
                                    planner_text = retry_text
                                    parsed = retry_parsed
                                    planner_meta = retry_meta
                                    structured_payload["execution_plan_self_check"] = {
                                        "triggered": True,
                                        "reason": "missing_executable_tasks",
                                        "fixed": True,
                                        "iteration": iteration,
                                    }
                                else:
                                    msg = (
                                        "Execution plan has no executable tasks for the change request.\n\n"
                                        "Needed: add a project SSH/K8s target or clarify where to apply changes."
                                    )
                                    structured_payload["execution_plan_self_check"] = {
                                        "triggered": True,
                                        "reason": "missing_executable_tasks",
                                        "fixed": False,
                                        "iteration": iteration,
                                    }
                                    structured_payload["paused_for_user"] = True
                                    structured_payload["questions_for_user"] = [
                                        "Specify a target (SSH/K8s) for the change or add a server to the project so I can form executable steps."
                                    ]
                                    team_patch_step(
                                        run_id,
                                        step_key,
                                        status="done",
                                        output_md=msg,
                                        structured=structured_payload,
                                        finished_at=_utc_iso(),
                                    )
                                    accumulated[step_key] = msg
                                    _pause_run_with_question(
                                        step_key,
                                        ["Specify a target (SSH/K8s) for the change or add a server to the project so I can form executable steps."],
                                        "Failed to form an executable plan.",
                                    )
                                    return True, msg, None, True
                            if not parsed:
                                break
                            for item in parsed:
                                ps = str(item.get("plan_summary") or "").strip()
                                item["plan_summary"] = (f"[batch {iteration}] {ps}" if ps else f"[batch {iteration}]").strip()
                            team_patch_run(run_id, phase=f"execution_dispatch_batch_{iteration}")
                            if iteration == 1 and not (team_get_run(run_id) or {}).get("execution_tasks"):
                                run_after_write = team_replace_execution_tasks(run_id, parsed)
                            else:
                                run_after_write = team_append_execution_tasks(run_id, parsed)
                            etasks = run_after_write.get("execution_tasks") if isinstance(run_after_write, dict) else []
                            batch_tasks = [
                                t for t in (etasks or []) if isinstance(t, dict) and str(t.get("id") or "") not in created_task_ids
                            ]
                            for t in batch_tasks:
                                tid = str(t.get("id") or "")
                                if tid:
                                    created_task_ids.add(tid)
                            if not batch_tasks:
                                break
                            publish_ai_team_execution_tasks(
                                project_id=pid,
                                tenant_id=tenant_id,
                                run_id=run_id,
                                tasks=batch_tasks,
                            )
                            wait_done = wait_for_ai_team_execution_tasks(
                                run_id,
                                timeout_sec=min(EXECUTION_TASKS_WAIT_SECONDS, PLAN_BATCH_WAIT_SECONDS),
                                task_ids=[str(t.get("id")) for t in batch_tasks if t.get("id")],
                            )
                            if not wait_done:
                                raise RuntimeError(
                                    "execution_timeout: worker batch tasks did not finish within "
                                    f"{min(EXECUTION_TASKS_WAIT_SECONDS, PLAN_BATCH_WAIT_SECONDS)}s"
                                )
                            run_done = team_get_run(run_id)
                            if not run_done:
                                raise RuntimeError("run not found after batch execution")
                            blockers = _execution_blockers(run_done)
                            if blockers:
                                blocker_lines = []
                                for b in blockers[:8]:
                                    er = f" · {b['error']}" if b.get("error") else ""
                                    blocker_lines.append(f"- {b['title']} (`{b['action_kind']}`): {b['status']}{er}")
                                structured_payload["execution_blockers"] = blockers
                                structured_payload["paused_for_user"] = True
                                structured_payload["questions_for_user"] = [
                                    "Batch execution finished with errors. Allow a recovery plan (retry tasks/fix environment) or specify manual actions."
                                ]
                                text = (
                                    f"Iterative execution stopped at batch {iteration}.\n\n### Execution blockers\n"
                                    + "\n".join(blocker_lines)
                                    + "\n\nPipeline paused until blockers are resolved."
                                )
                                team_patch_step(
                                    run_id,
                                    step_key,
                                    status="done",
                                    output_md=text,
                                    structured=structured_payload,
                                    finished_at=_utc_iso(),
                                )
                                accumulated[step_key] = text
                                _pause_run_with_question(
                                    step_key,
                                    ["Batch execution finished with errors. Allow a recovery plan (retry tasks/fix environment) or specify manual actions."],
                                    "Batch execution finished with errors.",
                                )
                                return True, text, None, True
                            batch_lines: list[str] = [f"### Batch {iteration}", ""]
                            for t in batch_tasks:
                                st = str(t.get("status") or "pending")
                                batch_lines.append(
                                    f"- {t.get('title', 'Task')} (`{t.get('action_kind', 'noop')}`) — {st}"
                                )
                            batch_reports.append("\n".join(batch_lines))
                            if planner_meta.get("goal_done") is True or planner_meta.get("continue_planning") is False:
                                break
                            iteration += 1
                            if iteration > PLAN_MAX_ITERATIONS:
                                break
                            planner_text = _run_single_step_text(
                                step_key,
                                direct_answer=direct_answer,
                                acc_override=acc_override,
                                debug_hint=(
                                    "Form the next task batch based on already completed execution tasks. "
                                    "Do not repeat completed tasks. Return only JSON in execution_plan format."
                                ),
                            )
                        run_done = team_get_run(run_id) or {}
                        exec_md = format_execution_tasks_markdown(run_done)
                        execution_ctx = exec_md
                        text = (
                            "Iterative execution plan was formed and processed in batches.\n\n"
                            + "\n\n".join(batch_reports)
                            + "\n\n---\n"
                            + exec_md
                        ).strip()
                        structured_payload["batch_iterations"] = max(1, len(batch_reports))
                        structured_payload["planner_limit"] = PLAN_MAX_TASKS_PER_ITER
                        team_patch_run(
                            run_id,
                            phase="execution_done"
                            if all(x.get("status") == "done" for x in (run_done.get("execution_tasks") or []))
                            else "execution_mixed",
                        )

                    if dod_applies_to_step(step_key, role_execution_mode=role_execution_mode):
                        run_post = team_get_run(run_id) or {}
                        post_dod = dod_gate_issues_for_run(
                            step_key=step_key,
                            role_execution_mode=role_execution_mode,
                            run_obj=run_post,
                            role_execution_evidence=role_execution_evidence,
                            prev_steps=prev_steps,
                        )
                        if post_dod:
                            questions = dod_gate_user_questions(post_dod)
                            gate_msg = (
                                "DoD not met — cannot mark deploy/implementation/summary as successful.\n"
                                + "\n".join(f"- {x}" for x in post_dod)
                            )
                            gate_structured = (
                                dict(structured_payload) if isinstance(structured_payload, dict) else {}
                            )
                            gate_structured.update(
                                {
                                    "paused_for_user": True,
                                    "dod_gate_failed": True,
                                    "dod_issues": post_dod,
                                    "questions_for_user": questions,
                                    "llm_output_rejected": (text or "")[:4000],
                                }
                            )
                            team_patch_step(
                                run_id,
                                step_key,
                                status="done",
                                output_md=gate_msg,
                                structured=gate_structured,
                                finished_at=_utc_iso(),
                            )
                            accumulated[step_key] = gate_msg
                            if step_key == "pm_summary":
                                team_patch_run(run_id, final_report_md=gate_msg, phase="summary_blocked_dod")
                            _pause_run_with_question(
                                step_key,
                                questions,
                                "DoD not met.",
                            )
                            return True, gate_msg, None, True

                    team_patch_step(
                        run_id,
                        step_key,
                        status="done",
                        output_md=text,
                        structured=structured_payload,
                        finished_at=_utc_iso(),
                    )
                    accumulated[step_key] = text
                    if step_key == "architect":
                        team_patch_run(run_id, architecture_md=text, phase="architecture_done")
                    if step_key == "pm_summary":
                        team_patch_run(run_id, final_report_md=text, phase="summary_done")
                    return True, text, None, False
                except Exception as exc:
                    last_err = str(exc)
                    sig_err = _text_fingerprint(last_err)
                    prev_err = step_error_signatures.get(step_key, "")
                    if sig_err and sig_err == prev_err:
                        step_error_repeats[step_key] = step_error_repeats.get(step_key, 1) + 1
                    else:
                        step_error_signatures[step_key] = sig_err
                        step_error_repeats[step_key] = 1
                    if step_error_repeats.get(step_key, 1) >= MAX_IDENTICAL_ERROR_REPEATS:
                        last_err = f"loop_detected: repeating step error ({last_err})"
                        break
                    if attempt < max_retries:
                        team_patch_step(
                            run_id,
                            step_key,
                            status="running",
                            output_md=(
                                f"Attempt {attempt}/{max_retries} failed: {last_err}\n"
                                f"Restarting step (attempt {attempt + 1}/{max_retries})…"
                            ),
                            structured={"attempt": attempt, "max_attempts": max_retries},
                        )

            team_patch_step(
                run_id,
                step_key,
                status="error",
                error=last_err,
                structured={"attempt": max_retries, "max_attempts": max_retries},
                finished_at=_utc_iso(),
            )
            if fail_run_on_error:
                team_patch_run(run_id, status="error", error=f"{label}: {last_err}", phase="failed")
            return False, None, last_err, False

        def _finalize_success() -> None:
            try:
                _persist_docs_after_run()
            except Exception as exc:
                team_patch_run(run_id, phase=f"docs_sync_warning: {exc}"[:120])
            team_patch_run(run_id, status="done", phase="done")

        if resume_from_paused:
            etasks_raw = existing_run.get("execution_tasks")
            etasks = etasks_raw if isinstance(etasks_raw, list) else []
            role_execution_mode = bool(etasks) or paused_step_key in (
                "devops_plan",
                "backend_plan",
                "frontend_plan",
                "execution_plan",
                "implementation",
                "deploy",
            )
            prev_status_map: dict[str, str] = {
                str(s.get("step_key") or ""): str(s.get("status") or "")
                for s in prev_steps
                if isinstance(s, dict)
            }
            route_obj = existing_run.get("architecture_json") if isinstance(existing_run.get("architecture_json"), dict) else {}
            route = route_obj.get("orchestration_route") if isinstance(route_obj.get("orchestration_route"), dict) else {}
            if not route:
                route = {"mode": "full"}

            if route.get("mode") == "direct" and route.get("primary_role"):
                _tail, specialist_key = pipeline_for_direct(str(route.get("primary_role")))
                step_to_run = paused_step_key or specialist_key
                if not step_to_run:
                    team_patch_run(run_id, status="error", error="resume failed: no step to continue", phase="failed")
                    return
                ok_direct, _txt, _e, halt_direct = _execute_step(
                    step_to_run,
                    direct_answer=True,
                    fail_run_on_error=True,
                )
                if not ok_direct or halt_direct:
                    return
                _finalize_success()
                return

            team_patch_run(run_id, phase="dag_resume")
            if paused_step_key == "execution_plan":
                paused_order = STEP_ORDER.get("implementation", 0)
            else:
                paused_order = STEP_ORDER.get(paused_step_key, 0)
            layers_to_run: list[tuple[str, ...]] = []
            for layer in dag_layers:
                keys: list[str] = []
                for k in layer:
                    if STEP_ORDER.get(k, 999) < paused_order:
                        continue
                    if k == paused_step_key:
                        keys.append(k)
                        continue
                    if prev_status_map.get(k) == "done":
                        continue
                    keys.append(k)
                if keys:
                    layers_to_run.append(tuple(keys))

            for layer in layers_to_run:
                if len(layer) == 1:
                    k = layer[0]
                    ok_step, _txt, _e, halt_step = _execute_step(
                        k,
                        direct_answer=role_execution_mode and k in ("devops_plan", "backend_plan", "frontend_plan"),
                    )
                    if not ok_step or halt_step:
                        return
                    continue

                team_patch_run(run_id, phase="parallel_plans")
                acc_snapshot = _accumulated_md()
                results: dict[str, tuple[bool, str | None, str | None, bool]] = {}
                with ThreadPoolExecutor(max_workers=len(layer)) as pool:
                    futs = {
                        pool.submit(
                            _execute_step,
                            k,
                            direct_answer=role_execution_mode and k in ("devops_plan", "backend_plan", "frontend_plan"),
                            fail_run_on_error=False,
                            acc_override=acc_snapshot,
                        ): k
                        for k in layer
                    }
                    for fut in as_completed(futs):
                        k = futs[fut]
                        try:
                            results[k] = fut.result()
                        except Exception as exc:  # pragma: no cover
                            results[k] = (False, None, str(exc), False)

                if any(results.get(k, (False, None, None, False))[3] for k in layer):
                    return

                failed = [k for k in layer if not results.get(k, (False, None, "no result", False))[0]]
                if failed:
                    peer_outputs = []
                    for k in layer:
                        ok_peer, txt_peer, _, _hp = results.get(k, (False, None, None, False))
                        if ok_peer and txt_peer:
                            lbl, _task = STEP_META.get(k, (k, ""))
                            peer_outputs.append(f"### {lbl}\n{txt_peer}")
                    debug_hint = (
                        "Colleagues already completed some parallel steps. "
                        "Use their output and help close the remaining gaps.\n\n"
                        + "\n\n".join(peer_outputs)
                    )[:12000]
                    acc_for_retry = _accumulated_md()
                    hard_failed: list[tuple[str, str]] = []
                    for k in failed:
                        ok_retry, _txt_retry, err_retry, _h = _execute_step(
                            k,
                            fail_run_on_error=False,
                            acc_override=acc_for_retry,
                            debug_hint=debug_hint,
                        )
                        if not ok_retry:
                            hard_failed.append((k, err_retry or "unknown error"))
                    if hard_failed:
                        first_k, first_err = hard_failed[0]
                        first_label, _task = STEP_META.get(first_k, (first_k, ""))
                        team_patch_run(
                            run_id,
                            status="error",
                            error=f"{first_label}: {first_err}",
                            phase="failed",
                        )
                        return
                team_patch_run(run_id, phase="parallel_done")

            _finalize_success()
            return

        ok_intake, intake_text, _err, _halt_intake = _execute_step("intake")
        if not ok_intake:
            return
        # Early flush: intake is visible in Langfuse without waiting for the full pipeline.
        langfuse_tracing.flush()
        route = enrich_route_with_sa_if_needed(parse_orchestration_route(intake_text or ""), user_message)
        orchestration_route = route
        role_execution_mode = merge_live_execution_flag(
            role_execution_mode, orchestration_route
        )
        dag_layers = active_dag_layers(orchestration_route)
        team_patch_run(run_id, architecture_json={"orchestration_route": route})
        if route.get("mode") == "direct" and route.get("primary_role"):
            tail, specialist_key = pipeline_for_direct(route["primary_role"])
            if tail and specialist_key:
                direct_with_execution = direct_use_execution_tail(specialist_key, route)
                skip_keys = direct_skip_keys(
                    specialist_key, live_execution=direct_with_execution
                )
                for skip_k in skip_keys:
                    team_patch_step(
                        run_id,
                        skip_k,
                        status="skipped",
                        output_md="Skipped: simple request — direct answer from the selected specialist.",
                        finished_at=_utc_iso(),
                    )
                team_patch_run(run_id, phase="routing_direct_execute" if direct_with_execution else "routing_direct")
                ok_direct, _txt, _e, halt_direct = _execute_step(
                    specialist_key,
                    direct_answer=not direct_with_execution,
                    fail_run_on_error=True,
                )
                if not ok_direct:
                    return
                if halt_direct:
                    return
                if not direct_with_execution:
                    ok_pm, _txt_pm, _err_pm, halt_pm = _execute_step("pm_summary")
                    if not ok_pm or halt_pm:
                        return
                elif direct_with_execution:
                    # In direct-with-execution, execution_plan is required first,
                    # so real execution_tasks appear and execution is confirmed.
                    tail_steps = ("execution_plan", "implementation", "deploy", "qa_validation", "pm_summary")
                    for k in tail_steps:
                        ok_step, _txt_step, _err_step, halt_step = _execute_step(k)
                        if not ok_step or halt_step:
                            return
                _finalize_success()
                return

        team_patch_run(run_id, phase="dag_full")
        for layer in dag_layers:
            if len(layer) == 1:
                k = layer[0]
                ok_step, _txt, _e, halt_step = _execute_step(
                    k,
                    direct_answer=role_execution_mode and k in ("devops_plan", "backend_plan", "frontend_plan"),
                )
                if not ok_step:
                    return
                if halt_step:
                    return
                continue

            team_patch_run(run_id, phase="parallel_plans")
            acc_snapshot = _accumulated_md()
            results: dict[str, tuple[bool, str | None, str | None, bool]] = {}
            with ThreadPoolExecutor(max_workers=len(layer)) as pool:
                futs = {
                    pool.submit(
                        _execute_step,
                        k,
                        direct_answer=role_execution_mode and k in ("devops_plan", "backend_plan", "frontend_plan"),
                        fail_run_on_error=False,
                        acc_override=acc_snapshot,
                    ): k
                    for k in layer
                }
                for fut in as_completed(futs):
                    k = futs[fut]
                    try:
                        results[k] = fut.result()
                    except Exception as exc:  # pragma: no cover
                        results[k] = (False, None, str(exc), False)

            if any(results.get(k, (False, None, None, False))[3] for k in layer):
                return

            failed = [k for k in layer if not results.get(k, (False, None, "no result", False))[0]]
            if failed:
                peer_outputs = []
                for k in layer:
                    ok_peer, txt_peer, _, _hp = results.get(k, (False, None, None, False))
                    if ok_peer and txt_peer:
                        lbl, _task = STEP_META.get(k, (k, ""))
                        peer_outputs.append(f"### {lbl}\n{txt_peer}")
                debug_hint = (
                    "Colleagues already completed some parallel steps. "
                    "Use their output and help close the remaining gaps.\n\n"
                    + "\n\n".join(peer_outputs)
                )[:12000]
                acc_for_retry = _accumulated_md()
                hard_failed: list[tuple[str, str]] = []
                for k in failed:
                    ok_retry, _txt_retry, err_retry, _h = _execute_step(
                        k,
                        fail_run_on_error=False,
                        acc_override=acc_for_retry,
                        debug_hint=debug_hint,
                    )
                    if not ok_retry:
                        hard_failed.append((k, err_retry or "unknown error"))
                if hard_failed:
                    first_k, first_err = hard_failed[0]
                    first_label, _task = STEP_META.get(first_k, (first_k, ""))
                    team_patch_run(
                        run_id,
                        status="error",
                        error=f"{first_label}: {first_err}",
                        phase="failed",
                    )
                    return
            team_patch_run(run_id, phase="parallel_done")

        _finalize_success()
    except Exception as exc:
        team_patch_run(run_id, status="error", error=str(exc), phase="failed")
    finally:
        langfuse_tracing.flush()


def get_run(run_id: str) -> dict[str, Any] | None:
    return team_get_run(run_id)
