"""Оркестрация AI-команды: шаги в Postgres (admin), LLM по ролям, итог и архитектура."""

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
    parse_orchestration_route,
    pipeline_for_direct,
    step_keys_to_skip_when_direct,
)
from services.agent_graph_service.orchestration_step_output import parse_step_envelope
from services.agent_graph_service.execution_plan_parse import (
    default_execution_tasks_if_empty,
    format_execution_tasks_markdown,
    parse_execution_plan_payload,
    wait_for_ai_team_execution_tasks,
)
from services.agent_graph_service.execution_publish import fetch_project_tenant_id, publish_ai_team_execution_tasks
from services.agent_graph_service import langfuse_tracing
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
        "Сделай только маршрутизацию: кто нужен для решения задачи.\n"
        "Верни СТРОГО один JSON-объект, без преамбулы и без markdown.\n"
        "Формат:\n"
        '{"orchestration_route":{"mode":"full|direct","roles":["pm|architect|devops|backend|frontend|qa"],"primary_role":"pm|architect|devops|backend|frontend|qa|null","reason":"кратко"}}\n'
        "rules:\n"
        "- roles: список реально нужных ролей для задачи (без лишних).\n"
        "- если нужна ровно одна роль -> mode=direct и primary_role=<эта роль>.\n"
        "- если нужно 2+ роли -> mode=full.\n"
        "- Никакого плана/анализа/архитектуры на intake."
    ),
    "pm": (
        "Ты PM-координатор. Сконцентрируйся на управлении ходом работ: декомпозиция задач, приоритеты, зависимости, "
        "контроль последовательности шагов и критерии готовности для команды. "
        "Не выдумывай технические детали кода/инфраструктуры — их формируют профильные роли."
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
        "Кто что делает и в каком порядке (можно таблицей или списком). Согласуй с архитектурой выше.\n"
        "Для задач по изменению существующего проекта сначала сделай анализ текущего состояния на основе фактов "
        "опирайся только на (контекст/доки/инструменты), затем давай план. В ответе обязательно разделы: "
        "«Анализ текущего состояния (факты)» и «План изменений»."
    ),
    "backend_plan": (
        "Составь план работ backend: API, БД, интеграции. Кто что делает и когда. Согласуй с архитектурой.\n"
        "Для задач по изменению существующего проекта сначала сделай анализ текущего кода/контрактов на основе фактов "
        "(контекст/документация/инструменты), затем давай план. В ответе обязательно разделы: "
        "«Анализ текущего кода (факты)» и «План изменений»."
    ),
    "frontend_plan": (
        "Составь план работ frontend: экраны, состояние, вызовы API. Кто что делает и когда.\n"
        "Для задач по изменению существующего проекта сначала сделай анализ текущего UI-кода на основе фактов "
        "(контекст/документация/инструменты), затем давай план. В ответе обязательно разделы: "
        "«Анализ текущего кода (факты)» и «План изменений»."
    ),
    "qa_validation": (
        "Проведи QA-валидацию после фактического деплоя на основе результатов шагов и фактов исполнения. "
        "Сформируй минимальный test plan (smoke/regression), риски и вердикт релиза. "
        "В summary_md обязательно укажи строку: release_recommendation: go|no_go. "
        "Если no_go — выставь pipeline_continue=false и добавь минимум 1 вопрос/блокер в questions_for_user."
    ),
    "execution_plan": (
        "Это шаг ТОЛЬКО для машинного плана исполнения БАТЧАМИ (отдельно от нарратива). "
        "Ответь строго одним JSON-объектом без markdown и без пояснений вокруг. Формат:\n"
        '{"tasks":[{"title":"кратко","action_kind":"ssh_run_command|kubernetes_query|ansible_run_template|noop","payload":{}}],"continue_planning":false,"goal_done":false,"done_reason":""}\n'
        "Смысл action_kind:\n"
        "- ssh_run_command: реальный запуск команд на SSH target. payload: {\"target_id\":\"<uuid>\",\"command\":\"...\"} "
        "или {\"target_id\":\"<uuid>\",\"commands\":[\"...\",\"...\"]}.\n"
        "- kubernetes_query: реальный kubectl через adapter. payload: {\"target_id\":\"<uuid>\",\"operation\":\"...\", ...}.\n"
        "- ansible_run_template: запуск роли из внутреннего Ansible marketplace. payload: "
        "{\"target_id\":\"<uuid>\",\"template_id\":\"<template-id>\",\"variables\":{...},"
        "\"allow_fallback\":true,\"fallback_service\":\"<service-id>\"}.\n"
        "- noop: ничего не делать (только если реально нет доступных действий).\n"
        "- adapter_agent_demo / gitlab_pipeline: legacy-режимы, использовать только по явному запросу пользователя и при наличии соответствующих ресурсов.\n"
        "- GitLab платформа (группа при создании проекта): gitlab_create_repository → repo_clone_or_sync (feature_branch) → search_codebase → repo_write_commit_push; "
        "не выдумывай репозитории без gitlab_provision_status / list_project_repos.\n"
        "Верни только один исполнимый батч с ограниченным числом задач за итерацию (не монолитный план).\n"
        "Если пользователь просит реальный деплой/действия на сервере и в контексте есть ssh_targets_for_tools — используй ssh_run_command "
        "с конкретными командами, а не noop. Для k8s-кластера используй kubernetes_query. "
        "Если запрос про установку типовых сервисов — предпочитай ansible_run_template, "
        "но только для сервисов, которые реально есть в контексте/каталоге или явно запрошены пользователем.\n"
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
        "Не добавляй фиктивные фразы «успешно развернули», если предыдущие шаги содержат ошибки.\n\n"
        "Блок «Накопленные результаты шагов» содержит только итоги ролей (PM, архитектор, планы ролей, QA) — без сырого "
        "JSON планов исполнения и без длинных отчётов implementation/deploy; при большом объёме текст обрезан. "
        "Блок «Факты исполнения» — краткий список задач воркеров (название, вид действия, статус, краткая ошибка), без полных логов."
    ),
}

STEP_META: dict[str, tuple[str, str]] = {k: (lbl, task) for k, lbl, task in DEFAULT_PIPELINE}
STEP_ORDER: dict[str, int] = {k: i for i, (k, _lbl, _task) in enumerate(DEFAULT_PIPELINE)}
# Только narrative-шаги ролей для финального отчёта (без execution_plan / implementation / deploy).
PM_SUMMARY_SOURCE_STEP_KEYS = frozenset(
    {"pm", "architect", "devops_plan", "backend_plan", "frontend_plan", "qa_validation"}
)
PM_SUMMARY_STEP_BODY_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_STEP_MAX_CHARS", "28000"))
PM_SUMMARY_CTX_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_CTX_MAX_CHARS", "10000"))
PM_SUMMARY_DOCS_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_DOCS_MAX_CHARS", "8000"))
PM_SUMMARY_EXEC_COMPACT_MAX = int(os.getenv("AGENT_ORCHESTRATION_PM_SUMMARY_EXEC_COMPACT_MAX", "12000"))
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
    ("implementation",),
    ("deploy",),
    ("qa_validation",),
    ("pm_summary",),
)
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
INTAKE_MAX_TOKENS = int(os.getenv("AGENT_ORCHESTRATION_INTAKE_MAX_TOKENS", "120"))
# Маршрутизация intake: не отправляем в LLM весь user_message (длинные вопросы = долгий prompt / дорого).
INTAKE_USER_MSG_MAX_CHARS = int(os.getenv("AGENT_ORCHESTRATION_INTAKE_USER_MSG_MAX_CHARS", "14000"))
INTAKE_USER_MSG_TAIL_CHARS = int(os.getenv("AGENT_ORCHESTRATION_INTAKE_USER_MSG_TAIL_CHARS", "4000"))
INTAKE_LLM_TIMEOUT_SEC = float(os.getenv("AGENT_ORCHESTRATION_INTAKE_TIMEOUT_SEC", "120"))
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
  "pipeline_continue": true,
  "live_execution": false
}
Правила:
- summary_md — главный передаваемый дальше контент; пиши сюда всё важное для команды.
- live_execution (опционально, по умолчанию false): true — дальше по пайплайну нужны реальные операции на инфраструктуре
  (SSH/kubectl/ansible, правки на уже развёрнутом, деплой), а не только текстовые планы; false — достаточно проектирования/кода без живого исполнения.
  Для шагов pm и architect оцени по запросу пользователя и выставь явно.
- Если нужен ответ пользователя до продолжения — step_status=need_user_clarification, заполни questions_for_user, pipeline_continue=false.
- Если вопрос к другой роли команды (backend/frontend/devops/architect/pm/qa) — добавь в peer_queries: [{"role":"backend","question":"..."}]. Оркестратор запросит коллегу автоматически; не жди пользователя.
- Не задавай вопросы пользователю, если их можно закрыть через peer_queries.
- При pipeline_continue=false пайплайн остановится до следующего сообщения пользователя.
"""


def _text_fingerprint(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    return t[:1200]


def _sanitize_model_text(text: str) -> str:
    """Убирает служебные токены провайдера из ответа модели."""
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"<\|[^|>]+?\|>", "", t)
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
    """Начало + конец длинного запроса: для маршрутизации ролей достаточно оценочного взгляда."""
    m = (user_message or "").strip()
    if len(m) <= INTAKE_USER_MSG_MAX_CHARS:
        return m
    tail_n = min(max(400, INTAKE_USER_MSG_TAIL_CHARS), INTAKE_USER_MSG_MAX_CHARS // 2)
    reserve = 220
    head_n = INTAKE_USER_MSG_MAX_CHARS - tail_n - reserve
    if head_n < 2000:
        head_n = INTAKE_USER_MSG_MAX_CHARS - tail_n - 120
    marker = (
        f"\n\n---\n[фрагмент опущен: всего символов {len(m)}; для маршрутизации учтены начало и конец запроса]\n---\n\n"
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
    """Стримит ответ в output_md шага (троттлинг) для live UI; fallback — invoke."""
    try:
        prompt_preview = _messages_prompt_preview(messages)
        model_label = _lc_model_label(model)
        _publish_llm_prompt_meta(run_id, step_key, prompt_preview, model_label)
        throttle: dict[str, Any] = {"last_t": 0.0, "last_len": -1}
        parts: list[str] = []
        if not stream:
            try:
                resp = model.invoke(messages, config=config)
                raw = resp.content if isinstance(resp.content, str) else str(resp.content)
                text = _sanitize_model_text(raw)
                _patch_step_streaming_output(run_id, step_key, text, {"last_t": 0.0, "last_len": -1})
                return text
            except Exception:
                pass
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
    """coding — использовать coding_model из LLM-настроек, если задана; иначе default_model."""
    return "coding" if step_key in CODING_MODEL_STEPS else "default"


def _ctx_trim(ctx: str, max_chars: int) -> str:
    c = (ctx or "").strip()
    if len(c) <= max_chars:
        return c
    return c[:max_chars] + "\n… [обрезано по лимиту символов]"


def _compact_execution_tasks_summary(run_obj: dict[str, Any] | None) -> str:
    """Краткая сводка execution_tasks для pm_summary — без полных payload и логов."""
    if not run_obj or not isinstance(run_obj, dict):
        return "(нет данных execution_tasks)"
    tasks_raw = run_obj.get("execution_tasks")
    tasks = tasks_raw if isinstance(tasks_raw, list) else []
    if not tasks:
        return "(исполняемых задач нет)"
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
        out = out[:PM_SUMMARY_EXEC_COMPACT_MAX] + "\n… [список задач обрезан]"
    return out


def _execution_change_request_heuristic(text: str) -> bool:
    """Маркеры «живого» исполнения без LLM: только для режима direct, когда нет шагов pm/architect."""
    if os.getenv("AGENT_ORCHESTRATION_LEGACY_BROAD_EXEC_HEURISTIC", "").strip() in (
        "1",
        "true",
        "yes",
    ):
        t = (text or "").lower()
        if not t:
            return False
        legacy = (
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
        return any(m in t for m in legacy)

    t = (text or "").lower()
    if not t:
        return False
    infra_markers = (
        "ssh",
        "kubectl",
        "ansible",
        "deploy",
        "деплой",
        "разверни",
        "развернуть",
        "на сервер",
        "на сервере",
        "продакш",
        "production",
        "ingress",
        "helm ",
        "docker compose",
        "docker-compose",
        "kubernetes",
        "k8s",
        "playbook",
        "ansible-playbook",
    )
    if any(m in t for m in infra_markers):
        return True
    maintenance_ru = (
        "убери",
        "уберите",
        "удали",
        "удалить",
        "удалите",
        "исправ",
        "исправь",
        "исправьте",
        "доработ",
        "правк",
        "почини",
        "почините",
        "не работает",
        "сломан",
        "сломано",
        "сломал",
        "баг",
        "баги",
        "регресс",
        "hotfix",
        "хотфикс",
        "откат",
        "rollback",
        "костыль",
    )
    maintenance_en = (
        "fix ",
        "fix:",
        "fixed ",
        "remove ",
        "remove:",
        "delete ",
        "broken",
        "rollback",
        "patch ",
        "patch:",
        "hotfix",
        "regression",
    )
    if any(m in t for m in maintenance_ru):
        return True
    if any(m in t for m in maintenance_en):
        return True
    existing_markers = (
        "существующ",
        "текущий код",
        "в коде",
        "в репозитор",
        "этот компонент",
        "этот endpoint",
        "этот api",
        "эту страницу",
        "эту форму",
        "уже развернут",
        "уже задеплоен",
    )
    return any(m in t for m in existing_markers)


def _has_executable_execution_tasks(tasks: list[dict[str, Any]]) -> bool:
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if str(t.get("action_kind") or "").strip().lower() in EXECUTABLE_ACTION_KINDS:
            return True
    return False


def _infer_infra_restrictions(ctx: str) -> str:
    """Явно запрещает LLM придумывать GitLab/Nexus, если их нет в project context."""
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
        "Ограничение по фактам контекста: "
        + ", ".join(missing)
        + " отсутствуют в проектных ресурсах. Не упоминай их как существующие."
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
        if not isinstance(tc, list):
            continue
        for item in tc:
            if not isinstance(item, dict):
                continue
            nm = str(item.get("name") or "").strip()
            if not nm:
                continue
            tool_calls_count += 1
            tool_names.append(nm)
    unique_names = sorted({x for x in tool_names if x})
    exec_tools = {"ssh_run_command", "kubernetes_query", "ansible_run_template"}
    return {
        "tool_calls_count": tool_calls_count,
        "tool_results_count": tool_results_count,
        "tool_names": unique_names,
        "has_execution_tool_call": any(x in exec_tools for x in unique_names),
    }


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


def _active_dag_layers() -> tuple[tuple[str, ...], ...]:
    return FULL_DAG_LAYERS


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
            "Ты маршрутизатор запроса к AI-команде. Нужно только определить: полный пайплайн (full) или "
            "ответ одного специалиста (direct) и при direct — какая роль. Не расписывай этапы работ, "
            "не собирай требования к продукту — это следующие шаги.\n\n"
            f"{extra}\n\n---\n"
            "Для intake запрещено опираться на контекст проекта/документацию — только формулировка запроса пользователя.\n\n"
            "Ответь только JSON orchestration_route. "
            "Не выдумывай факты — опирайся только на формулировку запроса.\n\n"
            f"Запрос пользователя:\n{um}"
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
    restrictions = f"{infra_restrictions}\n\n---\n" if infra_restrictions else ""
    tail = (
        f"{role_prompt}\n\n---\n{extra}\n\n---\n"
        f"{restrictions}"
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
            user_message = f"{base}\n\nУточнение пользователя к приостановленному шагу:\n{incoming_message}"
        elif continue_existing_run:
            base = original_user_message or incoming_message
            user_message = f"{base}\n\nНовое сообщение пользователя в рамках того же run:\n{incoming_message}"
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
        dag_layers = _active_dag_layers()
        # Режим «живого» исполнения (граф/инструменты на планах): из JSON шагов pm/architect (live_execution),
        # либо при resume — по факту execution_tasks / текущему шагу; отдельного LLM после intake нет.
        role_execution_mode = False
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
            """Для pm_summary: только выводы ролевых шагов, без execution_plan/implementation/deploy и без раздувания."""
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
                        + "\n\n… [вывод шага обрезан для финального отчёта; полный текст в панели оркестрации]"
                    )
                chunks.append(f"### {label}\n{body}")
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
            text = _stream_chat_then_sanitize(
                model,
                [
                    SystemMessage(content=system_peer),
                    HumanMessage(content="Дай ответ коллеге для продолжения пайплайна."),
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
                # Доп. защита от "ложного ok": старые/нестандартные результаты могли быть done,
                # но с ошибкой во вложенном payload.
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
                    return ["Нет execution_tasks и нет подтверждённых evidence от профильных ролей."]
                ev_rows = [role_execution_evidence.get(r, {}) for r in roles]
                has_exec = any(isinstance(ev, dict) and ev.get("has_execution_tool_call") is True for ev in ev_rows)
                has_tool_results = any(int(ev.get("tool_results_count") or 0) > 0 for ev in ev_rows if isinstance(ev, dict))
                if not has_exec:
                    return ["Профильные роли не выполнили реальных инструментальных действий (ssh/k8s/ansible)."]
                if not has_tool_results:
                    return ["У профильных ролей нет результатов tool-вызовов; фактическое выполнение не подтверждено."]
                return []
            executable = [
                t
                for t in tasks
                if isinstance(t, dict)
                and str(t.get("action_kind") or "").strip().lower() in EXECUTABLE_ACTION_KINDS
            ]
            if not executable:
                return ["В execution_tasks нет исполнимых действий (ssh/k8s/ansible)."]
            done_exec = [t for t in executable if str(t.get("status") or "") == "done"]
            if not done_exec:
                return ["Исполнимые действия есть, но ни одно не завершилось со статусом done."]
            if all(_task_is_diagnostic_only(t) for t in done_exec):
                return ["Выполнены только диагностические проверки, а не шаги развёртывания/изменений."]
            evidence_text = _extract_text_blobs(done_exec).lower()
            if "docker_not_found" in evidence_text:
                return ["На целевом хосте отсутствует Docker (docker_not_found), деплой не мог быть выполнен."]
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
                q0 = (fallback or "Нужно уточнение от пользователя для продолжения шага.").strip()
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
            nonlocal ctx, docs_ctx, infra_restrictions, role_execution_evidence
            label, _task = STEP_META.get(step_key, (step_key, ""))
            pr_key = STEP_PROMPT_ROLE.get(step_key, "pm")
            role_prompt = prompts.get(pr_key, "Ты помощник команды разработки.")
            if step_key != "intake" and not ctx:
                ctx = fetch_development_context_summary(pid)
            if step_key != "intake" and not docs_ctx:
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
                system_full += f"\n\n---\nСовместная отладка между ролями:\n{debug_hint}"
            human_content = (
                "Маршрутизация: только JSON по инструкции system."
                if step_key == "intake"
                else f"Выполни шаг «{label}» ({step_key})."
            )
            if _should_force_tool_analysis(step_key, role_execution_mode):
                analysis_prompt = (
                    f"{user_message}\n\n"
                    "Это задача изменения существующего проекта. "
                    "Шаг 1 (обязательный): собери факты через доступные инструменты. "
                    "Шаг 2: сделай анализ текущего состояния по фактам. "
                    "Не придумывай факты, пути файлов и результаты."
                )
                graph_pv = f"### LangGraph ReAct (инструменты)\n\n{analysis_prompt}"
                _publish_llm_prompt_meta(run_id, step_key, graph_pv, "langgraph-react")
                team_patch_step(
                    run_id,
                    step_key,
                    status="running",
                    output_md="⏳ LangGraph ReAct: сбор фактов через инструменты (может занять несколько минут)…",
                )
                graph_result = invoke_graph(project_id, analysis_prompt, orchestration_run_id=run_id)
                analysis_text = _extract_graph_text(graph_result)
                if not analysis_text:
                    return (
                        "Инструментальный анализ выполнен, но не вернулся итоговый текст ответа. "
                        "Повторите запрос, чтобы агент сформировал вывод по фактам."
                    )
                model = build_chat_model(purpose=_llm_purpose_for_step(step_key))
                final_messages = [
                    SystemMessage(content=system_full),
                    HumanMessage(
                        content=(
                            f"Выполни шаг «{label}» ({step_key}) на основе фактов ниже.\n\n"
                            "Обязательный формат ответа:\n"
                            "1) Анализ текущего состояния (факты)\n"
                            "2) План изменений\n"
                            "3) Проверка себя (что может быть неверно/что проверить дополнительно)\n\n"
                            f"Факты инструментального анализа:\n{analysis_text}"
                        )
                    ),
                ]
                return _stream_chat_then_sanitize(
                    model, final_messages, _lf_cfg(obs_step_key=step_key), run_id=run_id, step_key=step_key
                )
            if direct_answer and step_key in DIRECT_TOOL_STEPS:
                graph_pv = f"### LangGraph ReAct (прямой ответ)\n\n{user_message}"
                _publish_llm_prompt_meta(run_id, step_key, graph_pv, "langgraph-react")
                team_patch_step(
                    run_id,
                    step_key,
                    status="running",
                    output_md="⏳ LangGraph ReAct: инструменты и формирование ответа…",
                )
                graph_result = invoke_graph(project_id, user_message, orchestration_run_id=run_id)
                role_execution_evidence[step_key] = _collect_graph_execution_evidence(graph_result)
                text = _extract_graph_text(graph_result)
                if not text:
                    return (
                        "Инструментальный прогон выполнен, но не вернулся итоговый текст ответа. "
                        "Повторите запрос, чтобы агент сформировал финальный вывод из результатов инструментов."
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
            """Последний bool: halt_pipeline — остановить прогон (ожидание ответа пользователя)."""
            nonlocal execution_ctx, role_execution_mode
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
            if step_key in ("implementation", "deploy", "qa_validation", "pm_summary"):
                run_now = team_get_run(run_id) or {}
                blockers_now = _execution_blockers(run_now)
                real_work_issues = (
                    _execution_real_work_issues(run_now) if role_execution_mode else []
                )
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
                    _pause_run_with_question(
                        step_key,
                        ["Для продолжения деплоя/итога сначала закройте блокеры исполнения: исправить окружение, повторить задачи или подтвердить ручной обход."],
                        "Есть блокеры исполнения.",
                    )
                    return True, msg, None, True
                if real_work_issues:
                    msg = (
                        "Блокировка шага: нет подтверждения фактического выполнения изменений.\n"
                        + "\n".join(f"- {x}" for x in real_work_issues)
                    )
                    team_patch_step(
                        run_id,
                        step_key,
                        status="done",
                        output_md=msg,
                        structured={
                            "paused_for_user": True,
                            "execution_real_work_issues": real_work_issues,
                            "questions_for_user": [
                                "Нужно выполнить реальные шаги деплоя/изменений (не только диагностику). Разрешить повтор профильных шагов с обязательным tool-calling?"
                            ],
                        },
                        finished_at=_utc_iso(),
                    )
                    accumulated[step_key] = msg
                    _pause_run_with_question(
                        step_key,
                        ["Нужно выполнить реальные шаги деплоя/изменений (не только диагностику). Разрешить повтор профильных шагов с обязательным tool-calling?"],
                        "Нет подтверждения фактического выполнения изменений.",
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
                    sig_out = _text_fingerprint(text)
                    prev_out = step_output_signatures.get(step_key, "")
                    if sig_out and sig_out == prev_out:
                        step_output_repeats[step_key] = step_output_repeats.get(step_key, 1) + 1
                    else:
                        step_output_signatures[step_key] = sig_out
                        step_output_repeats[step_key] = 1
                    if step_output_repeats.get(step_key, 1) >= MAX_IDENTICAL_OUTPUT_REPEATS and attempt < max_retries:
                        raise RuntimeError(
                            "loop_detected: модель повторяет одинаковый вывод, пробую переформулировать шаг"
                        )

                    structured_payload: dict[str, Any] = {
                        "attempt": attempt,
                        "max_attempts": max_retries,
                    }
                    if direct_answer and step_key in ("devops_plan", "backend_plan", "frontend_plan"):
                        ev = role_execution_evidence.get(step_key)
                        if isinstance(ev, dict):
                            structured_payload["role_execution_evidence"] = ev
                        if role_execution_mode:
                            calls = int((ev or {}).get("tool_calls_count") or 0)
                            results = int((ev or {}).get("tool_results_count") or 0)
                            has_exec = bool((ev or {}).get("has_execution_tool_call"))
                            # Только devops_plan обязан подтвердить infra-инструментами; backend/frontend
                            # могут опираться на контекст через get_development_context/http_request и т.д.
                            missing_any_tools = calls <= 0 or results <= 0
                            missing_infra_tools = step_key == "devops_plan" and not has_exec
                            if missing_any_tools or missing_infra_tools:
                                msg = (
                                    "Блокировка шага: профильная роль не подтвердила фактическое выполнение.\n"
                                    f"- tool_calls: {calls}\n"
                                    f"- tool_results: {results}\n"
                                    f"- execution_tool_call (ssh/k8s/ansible): {'yes' if has_exec else 'no'}\n\n"
                                    + (
                                        "Нужно выполнить реальные инструментальные действия по своей зоне (ssh/k8s/ansible), "
                                        "а не только текстовый отчёт."
                                        if step_key == "devops_plan"
                                        else "Нужно вызвать хотя бы один инструмент платформы (контекст проекта, docs, http, sandbox) "
                                        "и получить результат, а не только текстовый отчёт."
                                    )
                                )
                                structured_payload["paused_for_user"] = True
                                structured_payload["questions_for_user"] = [
                                    f"Разрешить повторить шаг {step_key} с обязательным tool-calling и фактическими результатами?"
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
                                    [f"Разрешить повторить шаг {step_key} с обязательным tool-calling и фактическими результатами?"],
                                    "Профильная роль не подтвердила фактическое выполнение.",
                                )
                                return True, msg, None, True
                    halt_pipeline = False
                    apply_structured = step_key in STRUCTURED_STEP_KEYS and not (
                        direct_answer and step_key in DIRECT_TOOL_STEPS
                    )
                    if apply_structured:
                        env, _fb = parse_step_envelope(text)
                        if env:
                            if env.live_execution is True:
                                role_execution_mode = True
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
                        _pause_run_with_question(
                            step_key,
                            structured_payload.get("questions_for_user") if isinstance(structured_payload.get("questions_for_user"), list) else None,
                            "Шаг ожидает уточнения от пользователя.",
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
                            if not parsed and iteration == 1:
                                parsed = default_execution_tasks_if_empty(ctx)
                            if PLAN_MAX_TASKS_PER_ITER > 0:
                                parsed = parsed[:PLAN_MAX_TASKS_PER_ITER]
                            if role_execution_mode and parsed and not _has_executable_execution_tasks(parsed):
                                recheck_hint = (
                                    "Self-check: в батче нет исполнимых задач "
                                    "(ssh_run_command|kubernetes_query|ansible_run_template) для запроса на правку. "
                                    "Пересобери ближайший батч так, чтобы он содержал реальные шаги исполнения."
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
                                        "Execution plan не содержит исполнимых задач для запроса на правку.\n\n"
                                        "Нужно: добавить SSH/K8s target проекта или уточнить, где выполнять изменения."
                                    )
                                    structured_payload["execution_plan_self_check"] = {
                                        "triggered": True,
                                        "reason": "missing_executable_tasks",
                                        "fixed": False,
                                        "iteration": iteration,
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
                                    _pause_run_with_question(
                                        step_key,
                                        ["Укажите target (SSH/K8s) для правки или добавьте сервер в проект, чтобы я сформировал исполнимые шаги."],
                                        "Не удалось сформировать исполнимый план.",
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
                                    "execution_timeout: batch-задачи воркеров не завершились за "
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
                                    "Исполнение батча завершилось с ошибками. Разрешите план восстановления (повторные задачи/исправление окружения) или укажите ручные действия."
                                ]
                                text = (
                                    f"Итеративное исполнение остановлено на батче {iteration}.\n\n### Блокеры исполнения\n"
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
                                _pause_run_with_question(
                                    step_key,
                                    ["Исполнение батча завершилось с ошибками. Разрешите план восстановления (повторные задачи/исправление окружения) или укажите ручные действия."],
                                    "Исполнение батча завершилось с ошибками.",
                                )
                                return True, text, None, True
                            batch_lines: list[str] = [f"### Batch {iteration}", ""]
                            for t in batch_tasks:
                                st = str(t.get("status") or "pending")
                                batch_lines.append(
                                    f"- {t.get('title', 'Задача')} (`{t.get('action_kind', 'noop')}`) — {st}"
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
                                    "Сформируй следующий батч задач на основе уже выполненных execution tasks. "
                                    "Не повторяй завершённые задачи. Верни только JSON по формату execution_plan."
                                ),
                            )
                        run_done = team_get_run(run_id) or {}
                        exec_md = format_execution_tasks_markdown(run_done)
                        execution_ctx = exec_md
                        text = (
                            "Итеративный план исполнения сформирован и обработан батчами.\n\n"
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
                    if attempt < max_retries:
                        team_patch_step(
                            run_id,
                            step_key,
                            status="running",
                            output_md=(
                                f"Попытка {attempt}/{max_retries} завершилась ошибкой: {last_err}\n"
                                f"Перезапуск шага (попытка {attempt + 1}/{max_retries})…"
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
        # Ранний flush: intake виден в Langfuse без ожидания конца всего пайплайна.
        langfuse_tracing.flush()
        route = parse_orchestration_route(intake_text or "")
        team_patch_run(run_id, architecture_json={"orchestration_route": route})
        if route.get("mode") == "direct" and route.get("primary_role"):
            tail, specialist_key = pipeline_for_direct(route["primary_role"])
            if tail and specialist_key:
                direct_routing_text = (original_user_message or incoming_message).strip()
                direct_with_execution = specialist_key in (
                    "devops_plan",
                    "backend_plan",
                    "frontend_plan",
                ) and _execution_change_request_heuristic(direct_routing_text)
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
                    # В direct-with-execution сначала обязателен execution_plan,
                    # чтобы появились реальные execution_tasks и подтверждение выполнения.
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
    finally:
        langfuse_tracing.flush()


def get_run(run_id: str) -> dict[str, Any] | None:
    return team_get_run(run_id)
