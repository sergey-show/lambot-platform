"""
LangGraph ReAct agent with tool calling: platform tools (context, status, infra, sandbox).
Called via api-gateway (user auth).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Literal
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.agent_graph_service.graph_runner import invoke_graph
from services.agent_graph_service import langfuse_tracing
from services.agent_graph_service.orchestration_runner import get_run, run_orchestration_sync
from services.agent_graph_service.orchestration_stream import iter_orchestration_events
from services.agent_graph_service.team_client import team_create_run, team_latest_for_project

logger = logging.getLogger("agent_graph")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="agent-graph-service", version="0.1.0")


@app.on_event("startup")
def _log_langfuse_on_startup() -> None:
    if langfuse_tracing.enabled():
        base = (os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "").strip()
        logger.info("langfuse tracing: on, base_url/host=%s", base or "(empty)")
    else:
        logger.info(
            "langfuse tracing: off (LANGFUSE_TRACING_ENABLED or keys/host; see LANGFUSE_* in compose)"
        )


class ChatHistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=32000)


class GraphInvokeBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=32000)
    history: list[ChatHistoryTurn] = Field(default_factory=list, max_length=80)


class OrchestrationContinueBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=32000)


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "runtime": "langgraph",
        "langfuse_tracing": langfuse_tracing.enabled(),
    }


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
    """Start AI Team orchestration (steps in Postgres); background pipeline."""
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
    """Latest AI Team run for the project (from Postgres)."""
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
    """NDJSON stream with run/step/task deltas (for realtime UI)."""
    row = get_run(run_id)
    if row is None or row.get("project_id") != str(project_id):
        raise HTTPException(status_code=404, detail="run not found")

    async def generate():
        async for ev in iter_orchestration_events(project_id, run_id, websocket_mode=False):
            yield (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.websocket("/api/v1/projects/{project_id}/orchestrate/runs/{run_id}/stream/ws")
async def orchestrate_run_ws(websocket: WebSocket, project_id: UUID, run_id: str):
    """WebSocket: first message is a run snapshot (UI restore), then the same events as NDJSON."""
    await websocket.accept()
    try:
        async for ev in iter_orchestration_events(project_id, run_id, websocket_mode=True):
            await websocket.send_json(ev)
    except Exception:
        logger.exception("orchestration websocket failed run_id=%s", run_id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
