import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from services.orchestrator_service.db import SessionLocal, init_db
from services.orchestrator_service.models import InboxMessage, OutboxMessage
from shared.contracts.api import ProjectStatusResponse
from shared.contracts.events import Actor, EventEnvelope, EventMeta, EventTypes
from shared.domain.project_status import ProjectStatus
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_GROUP_ORCHESTRATOR, KAFKA_TOPIC_EVENTS

consumer: AIOKafkaConsumer | None = None
producer: AIOKafkaProducer | None = None
consumer_task: asyncio.Task | None = None
outbox_task: asyncio.Task | None = None
kafka_events_processed_total = 0
kafka_last_message_timestamp = 0.0
project_state_projection: dict[str, ProjectStatus] = {}


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


def process_kafka_event(event: dict) -> None:
    global kafka_events_processed_total, kafka_last_message_timestamp
    if "event_id" not in event:
        return
    event_type = event.get("event_type")
    project_id = event.get("project_id")
    tenant_id = event.get("tenant_id")
    if not event_type or not project_id:
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

        kafka_events_processed_total += 1
        kafka_last_message_timestamp = time.time()

        if event_type == EventTypes.PROJECT_STATUS_CHANGED:
            status = event.get("payload", {}).get("status")
            if status:
                project_state_projection[str(project_id)] = ProjectStatus(status)
                if (
                    status == ProjectStatus.PLAN_READY
                    and tenant_id
                ):
                    infra_request = EventEnvelope(
                        event_type=EventTypes.PROJECT_STATUS_CHANGE_REQUESTED,
                        project_id=UUID(project_id),
                        tenant_id=UUID(tenant_id),
                        actor=Actor(type="agent", id="orchestrator-service"),
                        payload={
                            "from_status": ProjectStatus.PLAN_READY,
                            "to_status": ProjectStatus.INFRA_IN_PROGRESS,
                            "reason": "orchestrator pipeline progression",
                        },
                        meta=EventMeta(source_service="orchestrator-service"),
                    )
                    enqueue_outbox(db, infra_request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def consume_events() -> None:
    if not consumer:
        return
    async for msg in consumer:
        try:
            event = json.loads(msg.value.decode("utf-8"))
        except Exception:
            continue
        await asyncio.to_thread(process_kafka_event, event)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global consumer, producer, consumer_task, outbox_task
    init_db()
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC_EVENTS,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_GROUP_ORCHESTRATOR,
        auto_offset_reset="earliest",
    )
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    try:
        await consumer.start()
        await producer.start()
        outbox_task = asyncio.create_task(publish_outbox_loop())
        consumer_task = asyncio.create_task(consume_events())
    except Exception:
        consumer = None
        producer = None
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


app = FastAPI(title="orchestrator-service", version="0.4.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    lag_total = 0
    if consumer:
        for tp in consumer.assignment():
            try:
                highwater = consumer.highwater(tp) or 0
                position = await consumer.position(tp)
                lag_total += max(highwater - position, 0)
            except Exception:
                continue

    lines = [
        "# HELP orchestrator_service_health Service health status.",
        "# TYPE orchestrator_service_health gauge",
        "orchestrator_service_health 1",
        "# HELP orchestrator_kafka_events_processed_total Number of processed Kafka events.",
        "# TYPE orchestrator_kafka_events_processed_total counter",
        f"orchestrator_kafka_events_processed_total {kafka_events_processed_total}",
        "# HELP orchestrator_kafka_lag_messages Consumer lag in messages.",
        "# TYPE orchestrator_kafka_lag_messages gauge",
        f"orchestrator_kafka_lag_messages {lag_total}",
        "# HELP orchestrator_kafka_last_message_timestamp_seconds Last processed message timestamp.",
        "# TYPE orchestrator_kafka_last_message_timestamp_seconds gauge",
        f"orchestrator_kafka_last_message_timestamp_seconds {kafka_last_message_timestamp}",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/api/v1/projects/{project_id}/orchestrator/status", response_model=ProjectStatusResponse)
def get_orchestrator_status(project_id: UUID) -> ProjectStatusResponse:
    status = project_state_projection.get(str(project_id))
    if not status:
        raise HTTPException(status_code=404, detail="Project not present in orchestrator projection yet")
    return ProjectStatusResponse(project_id=project_id, status=status)
