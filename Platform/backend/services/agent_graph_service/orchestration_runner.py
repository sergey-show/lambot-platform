"""Оркестрация AI-команды: шаги в Postgres (admin), LLM по ролям, итог и архитектура."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import re
from time import monotonic
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage

from services.admin_service.ai_team_service import DEFAULT_PIPELINE
from services.agent_graph_service.dev_prompts import fetch_development_context_summary, fetch_development_prompts
from services.agent_graph_service.orchestration_route import (
    parse_orchestration_route,
    pipeline_for_direct,
    step_keys_to_skip_when_direct,
)
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
        "primary_role: pm | architect | devops | backend | frontend — только при mode=direct.\n"
        "mode=full если нужен продукт/архитектура/несколько ролей; mode=direct если достаточно одного ответа специалиста."
    ),
    "pm": (
        "Собери функциональные и нефункциональные требования, аудиторию, границы MVP. "
        "Структурируй: продукт, пользователи, ключевые сценарии, NFR (производительность, безопасность, доступность)."
    ),
    "architect": (
        "Опиши целевую архитектуру, стек технологий, границы сервисов, данные и интеграции. "
        "Разбей работу на этапы. Явно сформулируй задачи для DevOps, Backend и Frontend (списком). "
        "Этот текст пойдёт в раздел «Архитектура» для пользователя."
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
    "execution_plan": (
        "Это шаг ТОЛЬКО для машинного плана исполнения (отдельно от нарратива). "
        "Ответь строго одним JSON-объектом без markdown и без пояснений вокруг. Формат:\n"
        '{"tasks":[{"title":"кратко","action_kind":"ssh_run_command|kubernetes_query|adapter_agent_demo|noop|gitlab_pipeline","payload":{}}]}\n'
        "Смысл action_kind:\n"
        "- ssh_run_command: реальный запуск команд на SSH target. payload: {\"target_id\":\"<uuid>\",\"command\":\"...\"} "
        "или {\"target_id\":\"<uuid>\",\"commands\":[\"...\",\"...\"]}.\n"
        "- kubernetes_query: реальный kubectl через adapter. payload: {\"target_id\":\"<uuid>\",\"operation\":\"...\", ...}.\n"
        "- adapter_agent_demo: GitLab demo через платформу (нужен GitLab ресурс).\n"
        "- noop: ничего не делать (только если реально нет доступных действий).\n"
        "- gitlab_pipeline: зарезервировано.\n"
        "Если пользователь просит реальный деплой/действия на сервере и в контексте есть ssh_targets_for_tools — используй ssh_run_command "
        "с конкретными командами, а не noop. Для k8s-кластера используй kubernetes_query."
    ),
    "implementation": (
        "Опирайся на блок «Факты исполнения» (структурированные задачи, воркеры) — что реально запускалось и с каким результатом. "
        "Дополни человекочитаемым планом: репозитории, пайплайны, Ansible. Если воркеры не запускались или упали — честно укажи."
    ),
    "deploy": (
        "Опиши, как развернуть результат на сервере проекта, опираясь на контекст (targets, URL). "
        "Если серверов нет — напиши, что сначала нужно зарегистрировать SSH target."
    ),
    "pm_summary": (
        "Финальный отчёт для заказчика без технического бэкграунда: что получилось, как попробовать продукт. "
        "URL, логины и пароли указывай ТОЛЬКО если они явно есть в контексте проекта ниже; иначе — честно напиши, "
        "что учётные данные нужно получить у администратора или завести в GitLab/на сервере."
    ),
}

STEP_META: dict[str, tuple[str, str]] = {k: (lbl, task) for k, lbl, task in DEFAULT_PIPELINE}
STEP_ORDER: dict[str, int] = {k: i for i, (k, _lbl, _task) in enumerate(DEFAULT_PIPELINE)}
DIRECT_TOOL_STEPS = frozenset({"devops_plan", "backend_plan", "frontend_plan"})
FULL_DAG_LAYERS: tuple[tuple[str, ...], ...] = (
    ("pm",),
    ("architect",),
    ("devops_plan", "backend_plan", "frontend_plan"),
    ("execution_plan",),
    ("implementation",),
    ("deploy",),
    ("pm_summary",),
)
MAX_STEP_RETRIES = 3
MAX_STEP_INVOCATIONS_PER_RUN = 4
MAX_IDENTICAL_ERROR_REPEATS = 2
MAX_IDENTICAL_OUTPUT_REPEATS = 2
MAX_ORCHESTRATION_SECONDS = 1800


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


def _ctx_trim(ctx: str, max_chars: int) -> str:
    c = (ctx or "").strip()
    if len(c) <= max_chars:
        return c
    return c[:max_chars] + "\n… [контекст обрезан для шага маршрутизации]"


def _build_system(
    step_key: str,
    role_prompt: str,
    ctx: str,
    accumulated_md: str,
    user_message: str,
    *,
    execution_ctx: str = "",
    direct_answer: bool = False,
) -> str:
    if step_key == "intake":
        extra = STEP_EXTRA.get("intake", "")
        ctx_use = _ctx_trim(ctx, 4500)
        return (
            "Ты маршрутизатор запроса к AI-команде. Нужно только определить: полный пайплайн (full) или "
            "ответ одного специалиста (direct) и при direct — какая роль. Не расписывай этапы работ, "
            "не собирай требования к продукту — это следующие шаги.\n\n"
            f"{extra}\n\n---\n"
            f"Контекст проекта:\n{ctx_use}\n\n---\n"
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
    ):
        extra += (
            "\n\nРежим прямого ответа: пользователь задал узкий вопрос. Дай конкретный ответ по сути, "
            "без развёрнутого многоэтапного плана всего продукта. Не выдумывай факты из контекста."
        )
    acc = accumulated_md.strip() or "(предыдущие шаги ещё не дали выводов)"
    ex = execution_ctx.strip() or "(ещё нет — шаг execution_plan и воркеры не завершены)"
    return (
        f"{role_prompt}\n\n---\n{extra}\n\n---\n"
        f"Контекст проекта (development-context):\n{ctx}\n\n---\n"
        f"Накопленные результаты шагов:\n{acc}\n\n---\n"
        f"Факты исполнения (структурированные задачи, воркеры):\n{ex}\n\n---\n"
        "Ответь по-русски, кроме шага execution_plan — там только JSON по инструкции. "
        "Не выдумывай секреты и URL — используй только контекст."
        f"\n\nЗапрос пользователя:\n{user_message}"
    )


def run_orchestration_sync(project_id: UUID, run_id: str, user_message: str) -> None:
    pid = str(project_id)
    user_message = user_message.strip()
    cache_latest_run_id(pid, run_id)
    try:
        team_patch_run(run_id, status="running", phase="pipeline")
        tenant_id = fetch_project_tenant_id(pid)
        prompts = fetch_development_prompts()
        ctx = fetch_development_context_summary(pid)
        accumulated: dict[str, str] = {}
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
            model = build_chat_model(purpose="coding")
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
        ) -> tuple[bool, str | None, str | None]:
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
                return False, None, err
            if monotonic() - run_started > MAX_ORCHESTRATION_SECONDS:
                err = "loop_detected: превышен лимит времени оркестрации"
                team_patch_step(run_id, step_key, status="error", error=err, finished_at=_utc_iso())
                if fail_run_on_error:
                    team_patch_run(run_id, status="error", error=f"{label}: {err}", phase="failed")
                return False, None, err

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

                    if step_key == "execution_plan":
                        parsed = parse_execution_plan_json(text)
                        if not parsed:
                            parsed = default_execution_tasks_if_empty(ctx)
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
                        wait_for_ai_team_execution_tasks(run_id)
                        run_done = team_get_run(run_id) or run
                        exec_md = format_execution_tasks_markdown(run_done)
                        text = f"{text}\n\n---\n{exec_md}"
                        execution_ctx = exec_md
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
                        structured={"attempt": attempt, "max_attempts": MAX_STEP_RETRIES},
                        finished_at=_utc_iso(),
                    )
                    accumulated[step_key] = text
                    if step_key == "architect":
                        team_patch_run(run_id, architecture_md=text, phase="architecture_done")
                    if step_key == "pm_summary":
                        team_patch_run(run_id, final_report_md=text, phase="summary_done")
                    return True, text, None
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
            return False, None, last_err

        ok_intake, intake_text, _err = _execute_step("intake")
        if not ok_intake:
            return
        route = parse_orchestration_route(intake_text or "")
        team_patch_run(run_id, architecture_json={"orchestration_route": route})
        if route.get("mode") == "direct" and route.get("primary_role"):
            tail, specialist_key = pipeline_for_direct(route["primary_role"])
            if tail and specialist_key:
                for skip_k in step_keys_to_skip_when_direct(specialist_key):
                    team_patch_step(
                        run_id,
                        skip_k,
                        status="skipped",
                        output_md="Пропущено: простой запрос — прямой ответ выбранного специалиста.",
                        finished_at=_utc_iso(),
                    )
                team_patch_run(run_id, phase="routing_direct")
                ok_direct, _txt, _e = _execute_step(
                    specialist_key,
                    direct_answer=True,
                    fail_run_on_error=True,
                )
                if not ok_direct:
                    return
                team_patch_run(run_id, status="done", phase="done")
                return

        team_patch_run(run_id, phase="dag_full")
        for layer in FULL_DAG_LAYERS:
            if len(layer) == 1:
                k = layer[0]
                ok_step, _txt, _e = _execute_step(k)
                if not ok_step:
                    return
                continue

            team_patch_run(run_id, phase="parallel_plans")
            acc_snapshot = _accumulated_md()
            results: dict[str, tuple[bool, str | None, str | None]] = {}
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
                        results[k] = (False, None, str(exc))

            failed = [k for k in layer if not results.get(k, (False, None, "no result"))[0]]
            if failed:
                peer_outputs = []
                for k in layer:
                    ok_peer, txt_peer, _ = results.get(k, (False, None, None))
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
                    ok_retry, _txt_retry, err_retry = _execute_step(
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

        team_patch_run(run_id, status="done", phase="done")
    except Exception as exc:
        team_patch_run(run_id, status="error", error=str(exc), phase="failed")


def get_run(run_id: str) -> dict[str, Any] | None:
    return team_get_run(run_id)
