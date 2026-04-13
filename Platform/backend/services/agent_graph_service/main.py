"""
LangGraph ReAct-агент с tool calling: инструменты платформы (контекст, статус, инфра, sandbox).
Вызывается через api-gateway (авторизация пользователя).
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.agent_graph_service.graph_runner import invoke_graph
from services.agent_graph_service.orchestration_runner import get_run, run_orchestration_sync
from services.agent_graph_service.team_client import team_create_run, team_latest_for_project

logger = logging.getLogger("agent_graph")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="agent-graph-service", version="0.1.0")


class ChatHistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=32000)


class GraphInvokeBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=32000)
    history: list[ChatHistoryTurn] = Field(default_factory=list, max_length=80)


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
