"""Оркестрация AI-команды: шаги в Postgres (admin), LLM по ролям, итог и архитектура."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
    parse_orchestration_route,
    pipeline_for_direct,
    step_keys_to_skip_when_direct,
)
from services.agent_graph_service.orchestration_step_output import parse_step_envelope
from services.agent_graph_service.execution_plan_parse import (
    default_execution_tasks_if_empty,
    format_execution_tasks_markdown,
    parse_execution_plan_json,
    wait_for_ai_team_execution_tasks,
)
from services.agent_graph_service.execution_publish import fetch_project_tenant_id, publish_ai_team_execution_tasks
from services.agent_graph_service.graph_runner import invoke_graph
from services.agent_graph_service.llm_config import build_chat_model
from services.agent_graph_service.redis_team import cache_latest_run_id
from services.agent_graph_service.team_client import (
    team_get_run,
    team_patch_run,
    team_patch_step,
    team_replace_execution_tasks,
)

# Какой промпт роли из admin подставить для шага
STEP_PROMPT_ROLE: dict[str, str] = {
    "intake": "pm",
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
        "Сделай только решение о маршруте (без развёрнутого плана команды и без MVP).\n"
        "Сначала 1–3 коротких предложения: что хочет пользователь и почему full или direct.\n"
        "Затем в конце ответа ОДИН JSON-объект в одну строку или блок, без markdown-ограждений:\n"
        '{"orchestration_route":{"mode":"full","reason":"кратко"}}\n'
        "или для узкого вопроса одному специалисту (без полного цикла продукт→архитектура→планы):\n"
        '{"orchestration_route":{"mode":"direct","primary_role":"backend","reason":"кратко"}}\n'
        "primary_role: pm | architect | devops | backend | frontend | qa — только при mode=direct.\n"
        "mode=full если нужен продукт/архитектура/несколько ролей; mode=direct если достаточно одного ответа специалиста.\n"
        "Если запрос про малую правку существующего проекта/кода — допускается mode=direct, "
        "но с последующим execution-хвостом (execution_plan + implementation + pm_summary). "
        "Для крупных изменений/рефакторинга выбирай mode=full."
    ),
    "pm": (
        "Собери функциональные и нефункциональные требования, аудиторию, границы MVP. "
        "Структурируй: продукт, пользователи, ключевые сценарии, NFR (производительность, безопасность, доступность)."
    ),
    "architect": (
        "Опиши целевую архитектуру, стек технологий, границы сервисов, данные и интеграции. "
        "Разбей работу на этапы. Явно сформулируй задачи для DevOps, Backend, Frontend и QA (списком). "
        "Этот текст пойдёт в раздел «Архитектура» для пользователя.\n"
        "Добавь полную структуру проекта: ключевые директории/сервисы, назначение модулей, контракты между ними, "
        "основные API/таблицы/очереди и где выполняется деплой."
    ),
    "devops_plan": (
        "Составь план работ DevOps: CI/CD, инфраструктура, секреты, окружения, деплой. "
        "Кто что делает и в каком порядке (можно таблицей или списком). Согласуй с архитектурой выше."
    ),
    "backend_plan": (
        "Составь план работ backend: API, БД, интеграции. Кто что делает и когда. Согласуй с архитектурой."
    ),
    "frontend_plan": (
        "Составь план работ frontend: экраны, состояние, вызовы API. Кто что делает и когда."
    ),
    "qa_validation": (
        "Проведи QA-валидацию после фактического деплоя на основе результатов шагов и фактов исполнения. "
        "Сформируй минимальный test plan (smoke/regression), риски и вердикт релиза. "
        "В summary_md обязательно укажи строку: release_recommendation: go|no_go. "
        "Если no_go — выставь pipeline_continue=false и добавь минимум 1 вопрос/блокер в questions_for_user."
    ),
    "execution_plan": (
        "Это шаг ТОЛЬКО для машинного плана исполнения (отдельно от нарратива). "
        "Ответь строго одним JSON-объектом без markdown и без пояснений вокруг. Формат:\n"
        '{"tasks":[{"title":"кратко","action_kind":"ssh_run_command|kubernetes_query|ansible_run_template|adapter_agent_demo|noop|gitlab_pipeline","payload":{}}]}\n'
        "Смысл action_kind:\n"
        "- ssh_run_command: реальный запуск команд на SSH target. payload: {\"target_id\":\"<uuid>\",\"command\":\"...\"} "
        "или {\"target_id\":\"<uuid>\",\"commands\":[\"...\",\"...\"]}.\n"
        "- kubernetes_query: реальный kubectl через adapter. payload: {\"target_id\":\"<uuid>\",\"operation\":\"...\", ...}.\n"
        "- ansible_run_template: запуск роли из внутреннего Ansible marketplace. payload: "
        "{\"target_id\":\"<uuid>\",\"template_id\":\"<template-id>\",\"variables\":{...},"
        "\"allow_fallback\":true,\"fallback_service\":\"docker|gitlab|nexus\"}.\n"
        "- adapter_agent_demo: GitLab demo через платформу (нужен GitLab ресурс).\n"
        "- noop: ничего не делать (только если реально нет доступных действий).\n"
        "- gitlab_pipeline: зарезервировано.\n"
        "Если пользователь просит реальный деплой/действия на сервере и в контексте есть ssh_targets_for_tools — используй ssh_run_command "
        "с конкретными командами, а не noop. Для k8s-кластера используй kubernetes_query. "
        "Если запрос про установку типовых сервисов (docker, GitLab, Nexus и т.п.) — предпочитай ansible_run_template, "
        "но только для сервисов, которые реально есть в контексте или явно запрошены пользователем.\n"
        "Если запрос — правка уже существующего приложения на сервере, сформируй ssh_run_command с конкретными шагами "
        "правки файлов/конфигов, проверкой результата и перезапуском нужного сервиса."
    ),
    "implementation": (
        "Опирайся на блок «Факты исполнения» (структурированные задачи, воркеры) — что реально запускалось и с каким результатом. "
        "Дополни человекочитаемым планом: репозитории, пайплайны, Ansible. Если воркеры не запускались или упали — честно укажи. "
        "Никогда не пиши, что сервис/деплой успешен, если в фактах исполнения есть ошибки или незавершённые задачи."
    ),
    "deploy": (
        "Опиши, как развернуть результат на сервере проекта, опираясь на контекст (targets, URL). "
        "Если серверов нет — напиши, что сначала нужно зарегистрировать SSH target. "
        "Запрещено утверждать «успешно развернули», если в execution facts есть ошибки или нет подтверждённого успешного исполнения."
    ),
    "pm_summary": (
        "Финальный отчёт для заказчика без технического бэкграунда: что получилось, как попробовать продукт. "
        "URL, логины и пароли указывай ТОЛЬКО если они явно есть в контексте проекта ниже; иначе — честно напиши, "
        "что учётные данные нужно получить у администратора или завести в используемом репозитории/на сервере. "
        "Не добавляй фиктивные фразы «успешно развернули», если предыдущие шаги содержат ошибки."
    ),
}

STEP_META: dict[str, tuple[str, str]] = {k: (lbl, task) for k, lbl, task in DEFAULT_PIPELINE}
STEP_ORDER: dict[str, int] = {k: i for i, (k, _lbl, _task) in enumerate(DEFAULT_PIPELINE)}
DIRECT_TOOL_STEPS = frozenset({"devops_plan", "backend_plan", "frontend_plan", "qa_validation"})
# Модель «для кода» из настроек платформы (coding_model) — только для инженерных шагов.
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
FULL_DAG_LAYERS: tuple[tuple[str, ...], ...] = (
    ("pm",),
    ("architect",),
    ("devops_plan", "backend_plan", "frontend_plan"),
    ("execution_plan",),
    ("implementation",),
    ("deploy",),
    ("qa_validation",),
    ("pm_summary",),
)
MAX_STEP_RETRIES = 3
MAX_STEP_INVOCATIONS_PER_RUN = 4
MAX_IDENTICAL_ERROR_REPEATS = 2
MAX_IDENTICAL_OUTPUT_REPEATS = 2
MAX_ORCHESTRATION_SECONDS = int(os.getenv("AGENT_ORCHESTRATION_MAX_SECONDS", "7200"))
EXECUTION_TASKS_WAIT_SECONDS = int(os.getenv("AGENT_ORCHESTRATION_EXECUTION_WAIT_SECONDS", "3600"))
MAX_PEER_QUERIES = 5
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")

# Шаги с единым JSON-конвертом (summary + вопросы/peer_queries); execution_plan и intake — отдельно.
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
--- ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТВЕТА ДЛЯ ЭТОГО ШАГА ---
Сначала выведи РОВНО ОДИН JSON-объект (без markdown-ограждений), затем можно пустую строку и дополнительный текст.
Схема JSON:
{
  "schema_version": 1,
  "step_status": "complete | need_user_clarification | need_peer_sync",
  "summary_md": "основной результат шага для коллег и отчёта (markdown)",
  "questions_for_user": [],
  "peer_queries": [],
  "pipeline_continue": true
}
Правила:
- summary_md — главный передаваемый дальше контент; пиши сюда всё важное для команды.
- Если нужен ответ пользователя до продолжения — step_status=need_user_clarification, заполни questions_for_user, pipeline_continue=false.
- Если вопрос к другой роли команды (backend/frontend/devops/architect/pm/qa) — добавь в peer_queries: [{"role":"backend","question":"..."}]. Оркестратор запросит коллегу автоматически; не жди пользователя.
- Не задавай вопросы пользователю, если их можно закрыть через peer_queries.
- При pipeline_continue=false пайплайн остановится до следующего сообщения пользователя.
"""


def _text_fingerprint(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    return t[:1200]


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
    """coding — использовать coding_model из LLM-настроек, если задана; иначе default_model."""
    return "coding" if step_key in CODING_MODEL_STEPS else "default"


def _ctx_trim(ctx: str, max_chars: int) -> str:
    c = (ctx or "").strip()
    if len(c) <= max_chars:
        return c
    return c[:max_chars] + "\n… [контекст обрезан для шага маршрутизации]"


def _looks_like_execution_change_request(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    markers = (
        "убер",
        "удал",
        "исправ",
        "измени",
        "доработ",
        "правк",
        "fix",
        "remove",
        "delete",
        "update",
        "deploy",
        "frontend",
        "backend",
        "api",
        "endpoint",
        "график",
        "компонент",
        "сервер",
        "ssh",
    )
    return any(m in t for m in markers)


def _has_executable_execution_tasks(tasks: list[dict[str, Any]]) -> bool:
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if str(t.get("action_kind") or "").strip().lower() in EXECUTABLE_ACTION_KINDS:
            return True
    return False


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
) -> str:
    if step_key == "intake":
        extra = STEP_EXTRA.get("intake", "")
        ctx_use = _ctx_trim(ctx, 4500)
        docs_use = _ctx_trim(docs_ctx, 2500)
        return (
            "Ты маршрутизатор запроса к AI-команде. Нужно только определить: полный пайплайн (full) или "
            "ответ одного специалиста (direct) и при direct — какая роль. Не расписывай этапы работ, "
            "не собирай требования к продукту — это следующие шаги.\n\n"
            f"{extra}\n\n---\n"
            f"Контекст проекта:\n{ctx_use}\n\n---\n"
            f"Документация проекта:\n{docs_use}\n\n---\n"
            "Ответь по-русски: короткая преамбула, затем JSON orchestration_route. "
            "Не выдумывай факты — опирайся на контекст и формулировку запроса.\n\n"
            f"Запрос пользователя:\n{user_message.strip()}"
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
            "\n\nРежим прямого ответа: пользователь задал узкий вопрос. Дай конкретный ответ по сути, "
            "без развёрнутого многоэтапного плана всего продукта. Не выдумывай факты из контекста."
        )
    acc = accumulated_md.strip() or "(предыдущие шаги ещё не дали выводов)"
    ex = execution_ctx.strip() or "(ещё нет — шаг execution_plan и воркеры не завершены)"
    tail = (
        f"{role_prompt}\n\n---\n{extra}\n\n---\n"
        f"Контекст проекта (development-context):\n{ctx}\n\n---\n"
        f"Документация проекта (wiki / RAG):\n{docs_ctx}\n\n---\n"
        f"Накопленные результаты шагов:\n{acc}\n\n---\n"
        f"Факты исполнения (структурированные задачи, воркеры):\n{ex}\n\n---\n"
        "Ответь по-русски, кроме шага execution_plan — там только JSON по инструкции. "
        "Не выдумывай секреты и URL — используй только контекст."
        f"\n\nЗапрос пользователя:\n{user_message}"
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
        paused_step_key = ""
        if existing_status == "paused":
            for s in sorted(prev_steps, key=lambda x: int(x.get("seq") or 999)):
                st = str(s.get("status") or "")
                structured = s.get("structured") if isinstance(s.get("structured"), dict) else {}
                if st == "done" and structured.get("paused_for_user") is True:
                    paused_step_key = str(s.get("step_key") or "").strip()
                    break
        resume_from_paused = bool(paused_step_key)
        if resume_from_paused:
            team_patch_run(run_id, status="running", phase="resume_pipeline")
        else:
            team_patch_run(run_id, status="running", phase="pipeline")
        tenant_id = fetch_project_tenant_id(pid)
        prompts = fetch_development_prompts()
        ctx = fetch_development_context_summary(pid)
        docs_ctx = fetch_documents_context_summary(pid, user_message)
        accumulated: dict[str, str] = {}
        execution_ctx = ""
        if resume_from_paused:
            original_user_message = str(existing_run.get("user_message") or "").strip()
            base = original_user_message or user_message
            user_message = f"{base}\n\nУточнение пользователя к приостановленному шагу:\n{user_message}"
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
        run_started = monotonic()
        step_invocations: dict[str, int] = {}
        step_error_signatures: dict[str, str] = {}
        step_error_repeats: dict[str, int] = {}
        step_output_signatures: dict[str, str] = {}
        step_output_repeats: dict[str, int] = {}
        
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

        def _consult_peer(role: str, question: str, requester_step: str) -> str:
            role_l = (role or "").strip().lower()
            peer_step = PRIMARY_ROLE_TO_STEP.get(role_l)
            if not peer_step:
                return (
                    f"[роль {role!r} не поддерживается для peer-синхронизации — "
                    "используй: pm, architect, devops, backend, frontend]"
                )
            lbl, _tsk = STEP_META.get(peer_step, (peer_step, ""))
            pr_key = STEP_PROMPT_ROLE.get(peer_step, "pm")
            peer_prompt = prompts.get(pr_key, "Ты помощник команды разработки.")
            peer_ctx = _ctx_trim(ctx, 6000)
            acc_now = _accumulated_md()
            system_peer = (
                f"{peer_prompt}\n\n---\n"
                "Внутренний запрос от коллеги по AI-команде. Ответь кратко и по делу, чтобы коллега мог "
                f"продолжить шаг «{requester_step}». Не обращайся к конечному пользователю — только ответ коллеге.\n"
                "Запрещено придумывать успешно выполненные действия, URL или результаты тестов без фактов из контекста.\n\n"
                f"Контекст проекта:\n{peer_ctx}\n\n---\n"
                f"Накопленные результаты шагов:\n{acc_now}\n\n---\n"
                f"Вопрос от коллеги (шаг пайплайна: {requester_step}; консультант: {lbl}):\n{question.strip()}"
            )
            model = build_chat_model(purpose=_llm_purpose_for_step(peer_step))
            resp = model.invoke(
                [
                    SystemMessage(content=system_peer),
                    HumanMessage(content="Дай ответ коллеге для продолжения пайплайна."),
                ]
            )
            out = resp.content if isinstance(resp.content, str) else str(resp.content)
            return (out or "").strip()

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
            return blockers

        def _run_single_step_text(
            step_key: str,
            *,
            direct_answer: bool = False,
            acc_override: str | None = None,
            debug_hint: str = "",
        ) -> str:
            label, _task = STEP_META.get(step_key, (step_key, ""))
            pr_key = STEP_PROMPT_ROLE.get(step_key, "pm")
            role_prompt = prompts.get(pr_key, "Ты помощник команды разработки.")
            system_full = _build_system(
                step_key,
                role_prompt,
                ctx,
                docs_ctx,
                acc_override if acc_override is not None else _accumulated_md(),
                user_message,
                execution_ctx=execution_ctx,
                direct_answer=direct_answer,
            )
            if debug_hint:
                system_full += f"\n\n---\nСовместная отладка между ролями:\n{debug_hint}"
            human_content = (
                "Маршрутизация: только JSON и краткая преамбула по инструкции system."
                if step_key == "intake"
                else f"Выполни шаг «{label}» ({step_key})."
            )
            if direct_answer and step_key in DIRECT_TOOL_STEPS:
                graph_result = invoke_graph(project_id, user_message)
                text = _extract_graph_text(graph_result)
                if not text:
                    return (
                        "Инструментальный прогон выполнен, но не вернулся итоговый текст ответа. "
                        "Повторите запрос, чтобы агент сформировал финальный вывод из результатов инструментов."
                    )
                return text
            model = build_chat_model(purpose=_llm_purpose_for_step(step_key))
            messages = [
                SystemMessage(content=system_full),
                HumanMessage(content=human_content),
            ]
            invoker = model.bind(max_tokens=900, temperature=0) if step_key == "intake" else model
            resp = invoker.invoke(messages)
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
            return (text or "").strip()

        def _execute_step(
            step_key: str,
            *,
            direct_answer: bool = False,
            fail_run_on_error: bool = True,
            acc_override: str | None = None,
            debug_hint: str = "",
        ) -> tuple[bool, str | None, str | None, bool]:
            """Последний bool: halt_pipeline — остановить прогон (ожидание ответа пользователя)."""
            nonlocal execution_ctx
            label, _task = STEP_META.get(step_key, (step_key, ""))
            step_invocations[step_key] = step_invocations.get(step_key, 0) + 1
            if step_invocations[step_key] > MAX_STEP_INVOCATIONS_PER_RUN:
                err = (
                    f"loop_detected: шаг {step_key} запускался слишком много раз "
                    f"({step_invocations[step_key]} > {MAX_STEP_INVOCATIONS_PER_RUN})"
                )
                team_patch_step(run_id, step_key, status="error", error=err, finished_at=_utc_iso())
                if fail_run_on_error:
                    team_patch_run(run_id, status="error", error=f"{label}: {err}", phase="failed")
                return False, None, err, False
            if monotonic() - run_started > MAX_ORCHESTRATION_SECONDS:
                err = (
                    "loop_detected: превышен лимит времени оркестрации "
                    f"({MAX_ORCHESTRATION_SECONDS}s)"
                )
                team_patch_step(run_id, step_key, status="error", error=err, finished_at=_utc_iso())
                if fail_run_on_error:
                    team_patch_run(run_id, status="error", error=f"{label}: {err}", phase="failed")
                return False, None, err, False
            if step_key in ("deploy", "pm_summary"):
                run_now = team_get_run(run_id) or {}
                blockers_now = _execution_blockers(run_now)
                if blockers_now:
                    lines = []
                    for b in blockers_now[:8]:
                        er = f" · {b['error']}" if b.get("error") else ""
                        lines.append(f"- {b['title']} (`{b['action_kind']}`): {b['status']}{er}")
                    msg = (
                        "Блокировка шага: в execution tasks есть ошибки/незавершённые задачи.\n"
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
                                "Для продолжения деплоя/итога сначала закройте блокеры исполнения: исправить окружение, повторить задачи или подтвердить ручной обход."
                            ],
                        },
                        finished_at=_utc_iso(),
                    )
                    accumulated[step_key] = msg
                    team_patch_run(run_id, status="paused", phase="awaiting_clarification")
                    return True, msg, None, True

            team_patch_step(run_id, step_key, status="running", started_at=_utc_iso())
            last_err = "unknown error"
            for attempt in range(1, MAX_STEP_RETRIES + 1):
                try:
                    text = _run_single_step_text(
                        step_key,
                        direct_answer=direct_answer,
                        acc_override=acc_override,
                        debug_hint=debug_hint,
                    )
                    sig_out = _text_fingerprint(text)
                    prev_out = step_output_signatures.get(step_key, "")
                    if sig_out and sig_out == prev_out:
                        step_output_repeats[step_key] = step_output_repeats.get(step_key, 1) + 1
                    else:
                        step_output_signatures[step_key] = sig_out
                        step_output_repeats[step_key] = 1
                    if step_output_repeats.get(step_key, 1) >= MAX_IDENTICAL_OUTPUT_REPEATS and attempt < MAX_STEP_RETRIES:
                        raise RuntimeError(
                            "loop_detected: модель повторяет одинаковый вывод, пробую переформулировать шаг"
                        )

                    structured_payload: dict[str, Any] = {
                        "attempt": attempt,
                        "max_attempts": MAX_STEP_RETRIES,
                    }
                    halt_pipeline = False
                    apply_structured = step_key in STRUCTURED_STEP_KEYS and not (
                        direct_answer and step_key in DIRECT_TOOL_STEPS
                    )
                    if apply_structured:
                        env, _fb = parse_step_envelope(text)
                        if env:
                            peer_responses: list[dict[str, Any]] = []
                            if env.peer_queries:
                                peer_parts: list[str] = []
                                for pq in env.peer_queries[:MAX_PEER_QUERIES]:
                                    ans = _consult_peer(pq.role, pq.question, step_key)
                                    peer_responses.append(
                                        {"role": pq.role, "question": pq.question, "answer": ans}
                                    )
                                    peer_parts.append(f"#### Ответ ({pq.role})\n{ans}")
                                if peer_parts:
                                    env.summary_md = (
                                        env.summary_md
                                        + "\n\n---\n### Синхронизация с командой (авто)\n\n"
                                        + "\n\n".join(peer_parts)
                                    )
                                    structured_payload["peer_responses"] = peer_responses
                            text = env.summary_md
                            structured_payload["step_envelope"] = env.to_dict()
                            halt_pipeline = (
                                (not env.pipeline_continue)
                                or bool(env.questions_for_user)
                                or env.step_status == "need_user_clarification"
                            )
                            if step_key == "qa_validation" and not halt_pipeline:
                                sm = (env.summary_md or "").lower()
                                if "release_recommendation" in sm and ("no_go" in sm or "no-go" in sm):
                                    halt_pipeline = True
                                    structured_payload["qa_gate"] = "no_go"
                                    if not env.questions_for_user:
                                        env.questions_for_user = [
                                            "QA дал вердикт NO_GO. Укажите, какие дефекты блокируют релиз и как планируем исправление."
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
                        team_patch_run(run_id, status="paused", phase="awaiting_clarification")
                        return True, text, None, True

                    if step_key == "execution_plan":
                        parsed = parse_execution_plan_json(text)
                        if not parsed:
                            parsed = default_execution_tasks_if_empty(ctx)
                        if _looks_like_execution_change_request(user_message) and not _has_executable_execution_tasks(parsed):
                            recheck_hint = (
                                "Self-check: в плане нет ни одной исполнимой задачи "
                                "(ssh_run_command|kubernetes_query|ansible_run_template) для запроса на правку. "
                                "Пересобери execution_plan так, чтобы он содержал реальные шаги исполнения."
                            )
                            retry_text = _run_single_step_text(
                                step_key,
                                direct_answer=direct_answer,
                                acc_override=acc_override,
                                debug_hint=recheck_hint,
                            )
                            retry_parsed = parse_execution_plan_json(retry_text)
                            if not retry_parsed:
                                retry_parsed = default_execution_tasks_if_empty(ctx)
                            if _has_executable_execution_tasks(retry_parsed):
                                text = retry_text
                                parsed = retry_parsed
                                structured_payload["execution_plan_self_check"] = {
                                    "triggered": True,
                                    "reason": "missing_executable_tasks",
                                    "fixed": True,
                                }
                            else:
                                msg = (
                                    "Execution plan не содержит исполнимых задач для запроса на правку. "
                                    "Агент выполнил самопроверку, но не смог сформировать runnable-план.\n\n"
                                    "Нужно: добавить SSH/K8s target проекта или уточнить, где выполнять изменения."
                                )
                                structured_payload["execution_plan_self_check"] = {
                                    "triggered": True,
                                    "reason": "missing_executable_tasks",
                                    "fixed": False,
                                }
                                structured_payload["paused_for_user"] = True
                                structured_payload["questions_for_user"] = [
                                    "Укажите target (SSH/K8s) для правки или добавьте сервер в проект, чтобы я сформировал исполнимые шаги."
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
                                team_patch_run(run_id, status="paused", phase="awaiting_clarification")
                                return True, msg, None, True
                        team_replace_execution_tasks(run_id, parsed)
                        run = team_get_run(run_id)
                        if not run:
                            raise RuntimeError("run not found after execution tasks create")
                        etasks = run.get("execution_tasks") or []
                        team_patch_run(run_id, phase="execution_dispatch")
                        publish_ai_team_execution_tasks(
                            project_id=pid,
                            tenant_id=tenant_id,
                            run_id=run_id,
                            tasks=etasks,
                        )
                        wait_done = wait_for_ai_team_execution_tasks(
                            run_id,
                            timeout_sec=EXECUTION_TASKS_WAIT_SECONDS,
                        )
                        if not wait_done:
                            raise RuntimeError(
                                "execution_timeout: задачи воркеров не завершились за "
                                f"{EXECUTION_TASKS_WAIT_SECONDS}s"
                            )
                        run_done = team_get_run(run_id) or run
                        exec_md = format_execution_tasks_markdown(run_done)
                        text = f"{text}\n\n---\n{exec_md}"
                        execution_ctx = exec_md
                        blockers = _execution_blockers(run_done)
                        if blockers:
                            blocker_lines = []
                            for b in blockers[:8]:
                                er = f" · {b['error']}" if b.get("error") else ""
                                blocker_lines.append(
                                    f"- {b['title']} (`{b['action_kind']}`): {b['status']}{er}"
                                )
                            structured_payload["execution_blockers"] = blockers
                            structured_payload["paused_for_user"] = True
                            structured_payload["questions_for_user"] = [
                                "Исполнение завершилось с ошибками. Разрешите план восстановления (повторные задачи/исправление окружения) или укажите ручные действия."
                            ]
                            text = (
                                f"{text}\n\n---\n### Блокеры исполнения\n"
                                + "\n".join(blocker_lines)
                                + "\n\nПайплайн приостановлен до решения блокеров."
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
                            team_patch_run(run_id, status="paused", phase="awaiting_clarification")
                            return True, text, None, True
                        team_patch_run(
                            run_id,
                            phase="execution_done"
                            if all(
                                x.get("status") == "done" for x in (run_done.get("execution_tasks") or [])
                            )
                            else "execution_mixed",
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
                        last_err = f"loop_detected: повторяющаяся ошибка шага ({last_err})"
                        break
                    if attempt < MAX_STEP_RETRIES:
                        team_patch_step(
                            run_id,
                            step_key,
                            status="running",
                            output_md=(
                                f"Попытка {attempt}/{MAX_STEP_RETRIES} завершилась ошибкой: {last_err}\n"
                                f"Перезапуск шага (попытка {attempt + 1}/{MAX_STEP_RETRIES})…"
                            ),
                            structured={"attempt": attempt, "max_attempts": MAX_STEP_RETRIES},
                        )

            team_patch_step(
                run_id,
                step_key,
                status="error",
                error=last_err,
                structured={"attempt": MAX_STEP_RETRIES, "max_attempts": MAX_STEP_RETRIES},
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
            paused_order = STEP_ORDER.get(paused_step_key, 0)
            layers_to_run: list[tuple[str, ...]] = []
            for layer in FULL_DAG_LAYERS:
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
                    ok_step, _txt, _e, halt_step = _execute_step(k)
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
                            direct_answer=False,
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
                        "Коллеги уже выполнили часть параллельных шагов. "
                        "Учти их вывод и помоги закрыть оставшиеся пробелы.\n\n"
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
        route = parse_orchestration_route(intake_text or "")
        team_patch_run(run_id, architecture_json={"orchestration_route": route})
        if route.get("mode") == "direct" and route.get("primary_role"):
            tail, specialist_key = pipeline_for_direct(route["primary_role"])
            if tail and specialist_key:
                direct_with_execution = (
                    specialist_key in ("devops_plan", "backend_plan", "frontend_plan")
                    and _looks_like_execution_change_request(user_message)
                )
                if direct_with_execution:
                    keep = {
                        specialist_key,
                        "intake",
                        "execution_plan",
                        "implementation",
                        "deploy",
                        "qa_validation",
                        "pm_summary",
                    }
                    skip_keys = [k for k in STEP_ORDER.keys() if k not in keep]
                else:
                    skip_keys = step_keys_to_skip_when_direct(specialist_key)
                for skip_k in skip_keys:
                    team_patch_step(
                        run_id,
                        skip_k,
                        status="skipped",
                        output_md="Пропущено: простой запрос — прямой ответ выбранного специалиста.",
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
                if direct_with_execution:
                    for k in ("execution_plan", "implementation", "deploy", "qa_validation", "pm_summary"):
                        ok_step, _txt_step, _err_step, halt_step = _execute_step(k)
                        if not ok_step or halt_step:
                            return
                _finalize_success()
                return

        team_patch_run(run_id, phase="dag_full")
        for layer in FULL_DAG_LAYERS:
            if len(layer) == 1:
                k = layer[0]
                ok_step, _txt, _e, halt_step = _execute_step(k)
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
                        direct_answer=False,
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
                    "Коллеги уже выполнили часть параллельных шагов. "
                    "Учти их вывод и помоги закрыть оставшиеся пробелы.\n\n"
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


def get_run(run_id: str) -> dict[str, Any] | None:
    return team_get_run(run_id)
