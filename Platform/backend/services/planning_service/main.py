import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import httpx
from aiokafka import AIOKafkaProducer
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.planning_service.db import SessionLocal, get_db, init_db
from services.planning_service.models import OutboxMessage, PlanRun
from shared.contracts.api import PlanningRunRequest, PlanningRunResponse
from shared.contracts.events import Actor, EventEnvelope, EventMeta, EventTypes
from shared.domain.project_status import ProjectStatus
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC_EVENTS

LLM_ADAPTER_URL = os.getenv("LLM_ADAPTER_URL", "http://llm-adapter-service:8005")
PROJECT_SERVICE_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")
producer: AIOKafkaProducer | None = None
outbox_task: asyncio.Task | None = None


def enqueue_outbox(db: Session, envelope: EventEnvelope) -> None:
    db.add(
        OutboxMessage(
            topic=KAFKA_TOPIC_EVENTS,
            payload=envelope.model_dump_json(),
        )
    )


async def publish_outbox_loop() -> None:
    while True:
        if not producer:
            await asyncio.sleep(0.3)
            continue
        db = SessionLocal()
        try:
            row = db.execute(
                select(OutboxMessage)
                .where(OutboxMessage.published_at.is_(None))
                .order_by(OutboxMessage.created_at)
                .limit(1)
            ).scalar_one_or_none()
            if row:
                await producer.send_and_wait(row.topic, row.payload.encode("utf-8"))
                row.published_at = datetime.utcnow()
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        await asyncio.sleep(0.05)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global producer, outbox_task
    init_db()
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    try:
        await producer.start()
        outbox_task = asyncio.create_task(publish_outbox_loop())
    except Exception:
        producer = None
        outbox_task = None
    yield
    if outbox_task:
        outbox_task.cancel()
        try:
            await outbox_task
        except asyncio.CancelledError:
            pass
    if producer:
        await producer.stop()


app = FastAPI(title="planning-service", version="0.3.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/projects/{project_id}/planning/run", response_model=PlanningRunResponse)
def run_planning(
    project_id: UUID, request: PlanningRunRequest, db: Session = Depends(get_db)
) -> PlanningRunResponse:
    tenant_id: UUID | None = request.tenant_id
    if not tenant_id:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}")
                if resp.status_code == 200:
                    tenant_id = UUID(resp.json()["tenant_id"])
        except Exception:
            tenant_id = None
    if not tenant_id:
        raise HTTPException(status_code=404, detail="Project not found for planning")

    llm_output = ""
    llm_provider = "unknown"
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{LLM_ADAPTER_URL}/api/v1/llm/generate",
                json={"prompt": f"Create MVP delivery plan for: {request.idea}"},
            )
            resp.raise_for_status()
            data = resp.json()
            llm_output = data.get("output", "")
            llm_provider = str(data.get("provider") or "unknown")
    except Exception:
        llm_output = "LLM adapter unavailable, fallback planning used."
        llm_provider = "unavailable"

    summary = "MVP plan generated."
    artifacts = {
        "scope": "MVP with SSO, planning, IaC, codegen, deploy",
        "architecture": "Python microservices + TS frontend + Kafka + PostgreSQL + Redis",
        "backlog": [
            "Identity and access",
            "Project workspace",
            "Planning",
            "Infrastructure provisioning",
            "Codegen and delivery",
        ],
        "risks": ["IaC drift", "LLM cost control", "integration complexity"],
        "input_idea": request.idea,
        "attachments_count": len(request.attachments),
        "llm_output": llm_output,
        "llm_provider": llm_provider,
    }
    plan = PlanRun(project_id=project_id, tenant_id=tenant_id, summary=summary, artifacts=artifacts)
    db.add(plan)
    db.flush()

    planning_event = EventEnvelope(
        event_type=EventTypes.PROJECT_PLANNING_COMPLETED,
        project_id=project_id,
        tenant_id=tenant_id,
        actor=Actor(type="agent", id="planning-service"),
        payload={"plan_id": str(plan.id)},
        meta=EventMeta(source_service="planning-service"),
    )
    status_request = EventEnvelope(
        event_type=EventTypes.PROJECT_STATUS_CHANGE_REQUESTED,
        project_id=project_id,
        tenant_id=tenant_id,
        actor=Actor(type="system", id="planning-service"),
        payload={
            "from_status": ProjectStatus.IDEA,
            "to_status": ProjectStatus.PLAN_READY,
            "reason": "planning completed",
        },
        meta=EventMeta(source_service="planning-service"),
    )
    enqueue_outbox(db, planning_event)
    enqueue_outbox(db, status_request)
    db.commit()
    db.refresh(plan)

    return PlanningRunResponse(
        plan_id=plan.id, project_id=project_id, summary=summary, artifacts=artifacts
    )
