"""
ИИ/bootstrap агенты платформы: реагируют на события Kafka и запускают цепочку (planning → дальше orchestrator).

По умолчанию при `project.created` вызывается planning/run (IDEA → PLAN_READY), чтобы воркфлоу «ожил» без ручного клика.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from aiokafka import AIOKafkaConsumer
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from shared.contracts.events import EventTypes
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_GROUP_AGENT_BOOTSTRAP, KAFKA_TOPIC_EVENTS

logger = logging.getLogger("agent_bootstrap")
logging.basicConfig(level=logging.INFO)

PROJECT_SERVICE_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")
PLANNING_SERVICE_URL = os.getenv("PLANNING_SERVICE_URL", "http://planning-service:8002")
AUTO_PLANNING = os.getenv("AGENT_BOOTSTRAP_AUTO_PLANNING", "1").lower() in ("1", "true", "yes")
INTERNAL_TOKEN = os.getenv("AGENT_BOOTSTRAP_INTERNAL_TOKEN", "internal-agent-bootstrap-dev-token")

consumer: AIOKafkaConsumer | None = None
consumer_task: asyncio.Task | None = None
_processed_projects: set[str] = set()
_bootstrap_runs_total = 0
_bootstrap_errors_total = 0


def _maybe_run_planning_from_event(event: dict) -> None:
    global _bootstrap_runs_total, _bootstrap_errors_total
    if not AUTO_PLANNING:
        return
    if event.get("event_type") != EventTypes.PROJECT_CREATED:
        return
    project_id = event.get("project_id")
    tenant_id = event.get("tenant_id")
    if not project_id or not tenant_id:
        return
    pid = str(project_id)
    if pid in _processed_projects:
        return
    payload = event.get("payload") or {}
    name = str(payload.get("name") or "project")
    description = str(payload.get("description") or "")

    try:
        sr = httpx.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}/status", timeout=10.0)
        if sr.status_code != 200:
            return
        if sr.json().get("status") != "IDEA":
            return
    except Exception as exc:
        logger.warning("bootstrap: status check failed: %s", exc)
        _bootstrap_errors_total += 1
        return

    idea = f"{name}: {description}".strip()[:4000] or "MVP delivery"
    try:
        pr = httpx.post(
            f"{PLANNING_SERVICE_URL}/api/v1/projects/{project_id}/planning/run",
            json={"idea": idea, "tenant_id": str(tenant_id), "attachments": []},
            timeout=120.0,
        )
        if pr.status_code in (200, 201):
            _processed_projects.add(pid)
            _bootstrap_runs_total += 1
            logger.info("bootstrap: planning started for project_id=%s", project_id)
        else:
            logger.warning("bootstrap: planning HTTP %s: %s", pr.status_code, pr.text[:500])
            _bootstrap_errors_total += 1
    except Exception as exc:
        logger.exception("bootstrap: planning call failed: %s", exc)
        _bootstrap_errors_total += 1


async def _consume_loop() -> None:
    if not consumer:
        return
    async for msg in consumer:
        try:
            event = json.loads(msg.value.decode("utf-8"))
        except Exception:
            continue
        await asyncio.to_thread(_maybe_run_planning_from_event, event)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global consumer, consumer_task
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC_EVENTS,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_GROUP_AGENT_BOOTSTRAP,
        auto_offset_reset="earliest",
    )
    try:
        await consumer.start()
        consumer_task = asyncio.create_task(_consume_loop())
    except Exception as exc:
        logger.exception("kafka start failed: %s", exc)
        consumer = None
        consumer_task = None
    yield
    if consumer_task:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
    if consumer:
        await consumer.stop()


app = FastAPI(title="agent-bootstrap-service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str | int | bool]:
    return {
        "status": "ok",
        "auto_planning": AUTO_PLANNING,
        "bootstrap_runs_total": _bootstrap_runs_total,
        "bootstrap_errors_total": _bootstrap_errors_total,
    }


def _require_internal(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    if (x_internal_token or "").strip() != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid internal token")


class ManualBootstrapBody(BaseModel):
    force: bool = Field(default=False, description="Запустить planning даже если проект уже не IDEA (осторожно)")


@app.post("/api/v1/internal/bootstrap/planning/{project_id}")
def manual_run_planning(
    project_id: UUID,
    body: ManualBootstrapBody = ManualBootstrapBody(),
    _: None = Depends(_require_internal),
) -> dict:
    """Ручной запуск planning (тот же сценарий, что и по событию)."""
    b = body
    try:
        gr = httpx.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}", timeout=10.0)
        if gr.status_code != 200:
            raise HTTPException(status_code=404, detail="Project not found")
        proj = gr.json()
        tenant_id = proj.get("tenant_id")
        name = str(proj.get("name") or "project")
        description = str(proj.get("description") or "")
        sr = httpx.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}/status", timeout=10.0)
        if sr.status_code != 200:
            raise HTTPException(status_code=502, detail="status unavailable")
        st = sr.json().get("status")
        if st != "IDEA" and not b.force:
            return {"status": "skipped", "reason": f"project status is {st}, not IDEA", "hint": "use force=true to override"}
        idea = f"{name}: {description}".strip()[:4000] or "MVP delivery"
        pr = httpx.post(
            f"{PLANNING_SERVICE_URL}/api/v1/projects/{project_id}/planning/run",
            json={"idea": idea, "tenant_id": str(tenant_id), "attachments": []},
            timeout=120.0,
        )
        if pr.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=pr.text[:800])
        return {"status": "ok", "planning": pr.json()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
