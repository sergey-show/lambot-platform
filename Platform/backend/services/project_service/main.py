import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID, uuid4

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from services.project_service.db import SessionLocal, get_db, init_db
from services.project_service.models import InboxMessage, OutboxMessage, Project
from shared.contracts.api import (
    ProjectAdminPatchRequest,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectStatusResponse,
    TransitionRequest,
)
from shared.contracts.events import Actor, EventEnvelope, EventMeta, EventTypes
from shared.domain.project_status import ProjectStatus
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC_EVENTS

producer: AIOKafkaProducer | None = None
consumer: AIOKafkaConsumer | None = None
consumer_task: asyncio.Task | None = None
outbox_task: asyncio.Task | None = None


def require_internal_project_token(
    x_internal_project_token: str | None = Header(default=None, alias="X-Internal-Project-Token"),
) -> None:
    expected = os.getenv("INTERNAL_PROJECT_ADMIN_TOKEN", "internal-project-admin-dev-token")
    if x_internal_project_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _project_to_response(entity: Project) -> ProjectResponse:
    return ProjectResponse(
        id=entity.id,
        name=entity.name,
        description=entity.description,
        tenant_id=entity.tenant_id,
        status=ProjectStatus(entity.status),
        suspended=bool(entity.suspended),
        deleted_at=entity.deleted_at,
    )


def _emit_project_updated(db: Session, entity: Project) -> None:
    env = EventEnvelope(
        event_type=EventTypes.PROJECT_UPDATED,
        project_id=entity.id,
        tenant_id=entity.tenant_id,
        actor=Actor(type="system", id="project-service"),
        payload={
            "name": entity.name,
            "description": entity.description,
            "status": entity.status,
            "suspended": bool(entity.suspended),
            "deleted_at": entity.deleted_at.isoformat() if entity.deleted_at else None,
        },
        meta=EventMeta(source_service="project-service"),
    )
    enqueue_outbox(db, env)


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


def process_status_command_event(event: dict) -> None:
    if "event_id" not in event:
        return
    if event.get("event_type") != EventTypes.PROJECT_STATUS_CHANGE_REQUESTED:
        return
    payload = event.get("payload", {})
    project_id = event.get("project_id")
    to_status = payload.get("to_status")
    from_status = payload.get("from_status")
    if not project_id or not to_status:
        return

    db = SessionLocal()
    try:
        stmt = (
            insert(InboxMessage)
            .values(
                event_id=UUID(event["event_id"]),
                payload=json.dumps(event),
            )
            .on_conflict_do_nothing()
        )
        result = db.execute(stmt)
        if result.rowcount == 0:
            db.rollback()
            return

        project = db.get(Project, UUID(project_id))
        if not project:
            db.commit()
            return
        if project.deleted_at is not None or project.suspended:
            db.commit()
            return
        current = ProjectStatus(project.status)
        if from_status and current != from_status:
            db.commit()
            return
        project.status = to_status
        changed = EventEnvelope(
            event_type=EventTypes.PROJECT_STATUS_CHANGED,
            project_id=project.id,
            tenant_id=project.tenant_id,
            actor=Actor(type="system", id="project-service"),
            payload={"status": to_status, "updated_at": datetime.utcnow().isoformat()},
            meta=EventMeta(source_service="project-service"),
        )
        enqueue_outbox(db, changed)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def consume_status_commands() -> None:
    if not consumer:
        return
    async for msg in consumer:
        try:
            event = json.loads(msg.value.decode("utf-8"))
        except Exception:
            continue
        await asyncio.to_thread(process_status_command_event, event)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global producer, consumer, consumer_task, outbox_task
    init_db()
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC_EVENTS,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="project-service",
        auto_offset_reset="earliest",
    )
    try:
        await producer.start()
        await consumer.start()
        outbox_task = asyncio.create_task(publish_outbox_loop())
        consumer_task = asyncio.create_task(consume_status_commands())
    except Exception:
        producer = None
        consumer = None
        consumer_task = None
        outbox_task = None
    yield
    if consumer_task:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
    if outbox_task:
        outbox_task.cancel()
        try:
            await outbox_task
        except asyncio.CancelledError:
            pass
    if consumer:
        await consumer.stop()
    if producer:
        await producer.stop()


app = FastAPI(title="project-service", version="0.3.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/projects", response_model=list[ProjectResponse])
def list_projects(tenant_id: UUID, db: Session = Depends(get_db)) -> list[ProjectResponse]:
    rows = (
        db.execute(
            select(Project)
            .where(Project.tenant_id == tenant_id, Project.deleted_at.is_(None))
            .order_by(Project.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_project_to_response(row) for row in rows]


@app.post("/api/v1/projects", response_model=ProjectResponse)
def create_project(request: ProjectCreateRequest, db: Session = Depends(get_db)) -> ProjectResponse:
    entity = Project(
        id=uuid4(),
        name=request.name,
        description=request.description,
        tenant_id=request.tenant_id,
        status=ProjectStatus.IDEA,
    )
    db.add(entity)
    db.flush()
    created = EventEnvelope(
        event_type=EventTypes.PROJECT_CREATED,
        project_id=entity.id,
        tenant_id=entity.tenant_id,
        actor=Actor(type="user", id="api"),
        payload={
            "name": entity.name,
            "description": entity.description,
            "status": entity.status,
        },
        meta=EventMeta(source_service="project-service"),
    )
    enqueue_outbox(db, created)
    db.commit()
    db.refresh(entity)
    return _project_to_response(entity)


@app.get("/api/v1/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: UUID, db: Session = Depends(get_db)) -> ProjectResponse:
    entity = db.get(Project, project_id)
    if not entity or entity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_to_response(entity)


@app.get("/api/v1/projects/{project_id}/status", response_model=ProjectStatusResponse)
def get_project_status(project_id: UUID, db: Session = Depends(get_db)) -> ProjectStatusResponse:
    entity = db.get(Project, project_id)
    if not entity or entity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectStatusResponse(project_id=project_id, status=ProjectStatus(entity.status))


@app.post("/api/v1/projects/{project_id}/status/transition", response_model=ProjectStatusResponse)
def transition_project_status(
    project_id: UUID, request: TransitionRequest, db: Session = Depends(get_db)
) -> ProjectStatusResponse:
    entity = db.get(Project, project_id)
    if not entity or entity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    if entity.suspended:
        raise HTTPException(status_code=403, detail="Project suspended")
    if ProjectStatus(entity.status) != request.from_status:
        raise HTTPException(status_code=409, detail="Invalid from_status")
    entity.status = request.to_status
    changed = EventEnvelope(
        event_type=EventTypes.PROJECT_STATUS_CHANGED,
        project_id=entity.id,
        tenant_id=entity.tenant_id,
        actor=Actor(type="user", id="api"),
        payload={"status": request.to_status},
        meta=EventMeta(source_service="project-service"),
    )
    enqueue_outbox(db, changed)
    db.commit()
    return ProjectStatusResponse(project_id=project_id, status=request.to_status)


@app.patch(
    "/api/v1/internal/projects/{project_id}",
    response_model=ProjectResponse,
    dependencies=[Depends(require_internal_project_token)],
)
def internal_patch_project(
    project_id: UUID,
    body: ProjectAdminPatchRequest,
    db: Session = Depends(get_db),
) -> ProjectResponse:
    entity = db.get(Project, project_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Project not found")
    if body.name is not None:
        entity.name = body.name.strip()
    if body.description is not None:
        entity.description = body.description
    if body.suspended is not None:
        entity.suspended = body.suspended
    if body.deleted is not None:
        entity.deleted_at = datetime.utcnow() if body.deleted else None
    _emit_project_updated(db, entity)
    db.commit()
    db.refresh(entity)
    return _project_to_response(entity)


@app.delete(
    "/api/v1/internal/projects/{project_id}",
    response_model=ProjectResponse,
    dependencies=[Depends(require_internal_project_token)],
)
def internal_soft_delete_project(project_id: UUID, db: Session = Depends(get_db)) -> ProjectResponse:
    body = ProjectAdminPatchRequest(deleted=True)
    return internal_patch_project(project_id, body, db)
