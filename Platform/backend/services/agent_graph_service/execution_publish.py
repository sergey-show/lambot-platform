"""Publish structured AI Team tasks to Kafka (`platform.agent.tasks`)."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import httpx
from aiokafka import AIOKafkaProducer

from shared.contracts.agent_task import TaskEnvelope, TaskType
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC_AGENT_TASKS

PROJECT_SERVICE_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")


def fetch_project_tenant_id(project_id: str) -> UUID:
    r = httpx.get(f"{PROJECT_SERVICE_URL.rstrip('/')}/api/v1/projects/{project_id}", timeout=15.0)
    r.raise_for_status()
    return UUID(str(r.json()["tenant_id"]))


def publish_ai_team_execution_tasks(
    *,
    project_id: str,
    tenant_id: UUID,
    run_id: str,
    tasks: list[dict],
) -> None:
    """One Kafka record per task; worker matches on `task_id` == admin DB row id."""

    async def _pub() -> None:
        producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
        await producer.start()
        try:
            for t in tasks:
                tid = UUID(str(t["id"]))
                env = TaskEnvelope(
                    task_id=tid,
                    task_type=TaskType.EXECUTE,
                    project_id=UUID(project_id),
                    tenant_id=tenant_id,
                    step_name=f"ai_team:{t.get('action_kind', 'task')}",
                    input_inline={
                        "ai_team_task_id": str(tid),
                        "ai_team_action": t.get("action_kind", "noop"),
                        "ai_team_run_id": run_id,
                        "payload": t.get("payload") or {},
                    },
                )
                await producer.send(
                    KAFKA_TOPIC_AGENT_TASKS,
                    value=env.model_dump_json().encode("utf-8"),
                )
        finally:
            await producer.stop()

    asyncio.run(_pub())
