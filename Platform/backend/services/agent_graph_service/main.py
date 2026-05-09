"""
LangGraph ReAct-агент с tool calling: инструменты платформы (контекст, статус, инфра, sandbox).
Вызывается через api-gateway (авторизация пользователя).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.agent_graph_service.graph_runner import invoke_graph
from services.agent_graph_service.orchestration_runner import get_run, run_orchestration_sync
from services.agent_graph_service.team_client import team_create_run, team_latest_for_project

logger = logging.getLogger("agent_graph")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="agent-graph-service", version="0.1.0")

_PHASE_BATCH_RE = re.compile(r"(planning|execution_dispatch)_batch_(\d+)")


def _derive_batch_meta(row: dict) -> dict[str, object]:
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


class ChatHistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=32000)


class GraphInvokeBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=32000)
    history: list[ChatHistoryTurn] = Field(default_factory=list, max_length=80)


class OrchestrationContinueBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=32000)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "runtime": "langgraph"}


@app.post("/api/v1/projects/{project_id}/invoke")
def graph_invoke(project_id: UUID, body: GraphInvokeBody) -> dict:
    try:
        hist = [t.model_dump() for t in body.history]
        return invoke_graph(project_id, body.message.strip(), hist)
    except Exception as exc:
        logger.exception("graph invoke failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/projects/{project_id}/orchestrate/start")
def orchestrate_start(
    project_id: UUID,
    body: GraphInvokeBody,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Запуск оркестрации AI-команды (шаги в Postgres); фоновый пайплайн."""
    msg = body.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message is empty")
    try:
        data = team_create_run(str(project_id), msg)
    except Exception as exc:
        logger.exception("team create run failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    run_id = data.get("run_id")
    if not run_id:
        raise HTTPException(status_code=500, detail="no run_id from admin")
    background_tasks.add_task(run_orchestration_sync, project_id, run_id, msg)
    return {"run_id": run_id, "status": "pending"}


@app.post("/api/v1/projects/{project_id}/orchestrate/runs/{run_id}/continue")
def orchestrate_continue(
    project_id: UUID,
    run_id: str,
    body: OrchestrationContinueBody,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    msg = body.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message is empty")
    row = get_run(run_id)
    if row is None or row.get("project_id") != str(project_id):
        raise HTTPException(status_code=404, detail="run not found")
    st = str(row.get("status") or "")
    if st not in ("paused", "done", "error"):
        raise HTTPException(status_code=409, detail="run is not continuable")
    background_tasks.add_task(run_orchestration_sync, project_id, run_id, msg)
    return {"run_id": run_id, "status": "resuming" if st == "paused" else "continuing"}


@app.get("/api/v1/projects/{project_id}/ai-team/state")
def ai_team_state(project_id: UUID) -> dict:
    """Последний прогон AI-команды по проекту (из Postgres)."""
    run = team_latest_for_project(str(project_id))
    return {"run": run}


@app.get("/api/v1/projects/{project_id}/orchestrate/runs/{run_id}")
def orchestrate_run_status(project_id: UUID, run_id: str) -> dict:
    row = get_run(run_id)
    if row is None or row.get("project_id") != str(project_id):
        raise HTTPException(status_code=404, detail="run not found")
    return row


@app.get("/api/v1/projects/{project_id}/orchestrate/runs/{run_id}/stream")
async def orchestrate_run_stream(project_id: UUID, run_id: str):
    """Поток NDJSON с дельтами run/step/task (для realtime UI)."""
    initial = get_run(run_id)
    if initial is None or initial.get("project_id") != str(project_id):
        raise HTTPException(status_code=404, detail="run not found")

    async def generate():
        prev_row: dict | None = None
        last_emit_ts = monotonic()
        heartbeat_interval_sec = 10.0
        while True:
            row = get_run(run_id)
            if row is None:
                payload = {"type": "error", "detail": "run not found"}
                yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                break
            if row.get("project_id") != str(project_id):
                payload = {"type": "error", "detail": "run does not belong to project"}
                yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
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
            current_meta.update(_derive_batch_meta(row))
            prev_meta = dict(prev)
            prev_meta.update(_derive_batch_meta(prev))
            for k in meta_keys:
                if current_meta.get(k) != prev_meta.get(k):
                    meta_patch[k] = current_meta.get(k)
            if meta_patch:
                payload = {"type": "run_meta", "patch": meta_patch}
                yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                emitted = True
                last_emit_ts = monotonic()

            prev_steps_raw = prev.get("steps") if isinstance(prev.get("steps"), list) else []
            prev_steps = {str(s.get("step_key")): s for s in prev_steps_raw if isinstance(s, dict)}
            for s in (row.get("steps") or []):
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
                            payload = {"type": "agent_trace", "step_key": step_key, "message": clip[:1200]}
                            yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                            emitted = True
                            last_emit_ts = monotonic()
                    else:
                        patch["output_full"] = out_now
                if patch:
                    payload = {"type": "step_delta", "step_key": step_key, "patch": patch}
                    yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                    emitted = True
                    last_emit_ts = monotonic()

            prev_tasks_raw = prev.get("execution_tasks") if isinstance(prev.get("execution_tasks"), list) else []
            prev_tasks = {
                str((t.get("id") or t.get("seq"))): t for t in prev_tasks_raw if isinstance(t, dict)
            }
            for t in (row.get("execution_tasks") or []):
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
                    payload = {"type": "execution_task_delta", "task_id": task_id, "patch": patch}
                    yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                    emitted = True
                    last_emit_ts = monotonic()

            prev_row = row

            st = str(row.get("status") or "")
            if st in ("done", "error", "paused"):
                payload = {"type": "stream_end", "status": st}
                yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                break
            if not emitted and (monotonic() - last_emit_ts) >= heartbeat_interval_sec:
                payload = {
                    "type": "heartbeat",
                    "run_id": run_id,
                    "status": st,
                    "phase": row.get("phase"),
                }
                yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                last_emit_ts = monotonic()
            await asyncio.sleep(0.8)

    return StreamingResponse(generate(), media_type="application/x-ndjson")
