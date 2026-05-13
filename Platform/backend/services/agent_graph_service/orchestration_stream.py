"""Поток событий оркестрации (NDJSON / WebSocket): общая логика опроса Postgres."""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any
from uuid import UUID

from services.agent_graph_service.orchestration_runner import get_run

_PHASE_BATCH_RE = re.compile(r"(planning|execution_dispatch)_batch_(\d+)")


def derive_batch_meta(row: dict) -> dict[str, object]:
    phase = str(row.get("phase") or "")
    iteration = 0
    batch_status = "idle"
    m = _PHASE_BATCH_RE.search(phase)
    if m:
        iteration = int(m.group(2))
        batch_status = "planning" if m.group(1) == "planning" else "dispatching"
    elif phase.startswith("execution_done"):
        tasks = row.get("execution_tasks") if isinstance(row.get("execution_tasks"), list) else []
        max_seq = max((int(t.get("seq") or 0) for t in tasks if isinstance(t, dict)), default=-1)
        if max_seq >= 0:
            iteration = max_seq + 1
        batch_status = "done"
    elif phase.startswith("execution_mixed"):
        tasks = row.get("execution_tasks") if isinstance(row.get("execution_tasks"), list) else []
        max_seq = max((int(t.get("seq") or 0) for t in tasks if isinstance(t, dict)), default=-1)
        if max_seq >= 0:
            iteration = max_seq + 1
        batch_status = "mixed"
    elif phase.startswith("failed"):
        batch_status = "failed"
    elif phase.startswith("awaiting_clarification"):
        batch_status = "paused"
    return {"iteration": iteration, "batch_status": batch_status}


def build_run_client_view(row: dict) -> dict[str, Any]:
    """Полное состояние run для UI (как после merge run_meta)."""
    out: dict[str, Any] = dict(row)
    out.update(derive_batch_meta(row))
    return out


async def iter_orchestration_events(
    project_id: UUID,
    run_id: str,
    *,
    websocket_mode: bool,
) -> AsyncIterator[dict[str, Any]]:
    """
    websocket_mode=True: первое событие — snapshot (восстановление после перезагрузки страницы).
    websocket_mode=False: без snapshot в потоке; prev_row = снимок текущего run — дальше только новые дельты
    (клиент перед NDJSON должен один раз подтянуть run через GET/poll, см. portal).
    """
    initial = get_run(run_id)
    if initial is None or initial.get("project_id") != str(project_id):
        yield {
            "type": "error",
            "detail": "run not found" if initial is None else "run does not belong to project",
        }
        return

    if websocket_mode:
        yield {"type": "snapshot", "run": build_run_client_view(initial)}
        prev_row: dict | None = copy.deepcopy(initial)
    else:
        prev_row = copy.deepcopy(initial)

    last_emit_ts = monotonic()
    heartbeat_interval_sec = 10.0

    while True:
        row = get_run(run_id)
        if row is None:
            yield {"type": "error", "detail": "run not found"}
            break
        if row.get("project_id") != str(project_id):
            yield {"type": "error", "detail": "run does not belong to project"}
            break

        prev = prev_row or {}
        emitted = False
        meta_keys = (
            "run_id",
            "project_id",
            "status",
            "phase",
            "iteration",
            "batch_status",
            "error",
            "architecture_md",
            "architecture_json",
            "final_report_md",
            "user_message",
            "created_at",
            "updated_at",
        )
        meta_patch: dict[str, object] = {}
        current_meta = dict(row)
        current_meta.update(derive_batch_meta(row))
        prev_meta = dict(prev)
        prev_meta.update(derive_batch_meta(prev))
        for k in meta_keys:
            if current_meta.get(k) != prev_meta.get(k):
                meta_patch[k] = current_meta.get(k)
        if meta_patch:
            yield {"type": "run_meta", "patch": meta_patch}
            emitted = True
            last_emit_ts = monotonic()

        prev_steps_raw = prev.get("steps") if isinstance(prev.get("steps"), list) else []
        prev_steps = {str(s.get("step_key")): s for s in prev_steps_raw if isinstance(s, dict)}
        for s in row.get("steps") or []:
            if not isinstance(s, dict):
                continue
            step_key = str(s.get("step_key") or "").strip()
            if not step_key:
                continue
            p = prev_steps.get(step_key) or {}
            patch: dict[str, object] = {}
            for k in ("seq", "role_label", "task_title", "status", "error", "started_at", "finished_at", "structured"):
                if s.get(k) != p.get(k):
                    patch[k] = s.get(k)
            out_now = str(s.get("output") or "")
            out_prev = str(p.get("output") or "")
            if out_now != out_prev:
                if out_prev and out_now.startswith(out_prev):
                    appended = out_now[len(out_prev) :]
                    patch["output_append"] = appended
                    clip = appended.strip()
                    if clip:
                        yield {"type": "agent_trace", "step_key": step_key, "message": clip[:1200]}
                        emitted = True
                        last_emit_ts = monotonic()
                else:
                    patch["output_full"] = out_now
            if patch:
                yield {"type": "step_delta", "step_key": step_key, "patch": patch}
                emitted = True
                last_emit_ts = monotonic()

        prev_tasks_raw = prev.get("execution_tasks") if isinstance(prev.get("execution_tasks"), list) else []
        prev_tasks = {
            str((t.get("id") or t.get("seq"))): t for t in prev_tasks_raw if isinstance(t, dict)
        }
        for t in row.get("execution_tasks") or []:
            if not isinstance(t, dict):
                continue
            task_id = str(t.get("id") or t.get("seq") or "").strip()
            if not task_id:
                continue
            p = prev_tasks.get(task_id) or {}
            patch: dict[str, object] = {}
            for k in (
                "id",
                "seq",
                "title",
                "action_kind",
                "status",
                "error",
                "result",
                "started_at",
                "finished_at",
                "updated_at",
            ):
                if t.get(k) != p.get(k):
                    patch[k] = t.get(k)
            if patch:
                yield {"type": "execution_task_delta", "task_id": task_id, "patch": patch}
                emitted = True
                last_emit_ts = monotonic()

        prev_row = row

        st = str(row.get("status") or "")
        if st in ("done", "error", "paused"):
            yield {"type": "stream_end", "status": st}
            break
        if not emitted and (monotonic() - last_emit_ts) >= heartbeat_interval_sec:
            yield {
                "type": "heartbeat",
                "run_id": run_id,
                "status": st,
                "phase": row.get("phase"),
            }
            last_emit_ts = monotonic()
        await asyncio.sleep(0.8)
