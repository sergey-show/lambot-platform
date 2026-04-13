"""
Agent worker: читает Kafka `platform.agent.tasks`.
- AGENT_WORKER_USE_LLM=0 (default): только логировать TaskEnvelope (dry-run).
- AGENT_WORKER_USE_LLM=1: вызов llm-adapter-service с промптом из задачи.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI
from pydantic import ValidationError

from shared.contracts.agent_task import TaskEnvelope, TaskResult, TaskStatus, TaskType
from shared.contracts.api import LlmGenerateRequest
from shared.kafka import (
    KAFKA_BOOTSTRAP_SERVERS,
    KAFKA_GROUP_AGENT_WORKER,
    KAFKA_TOPIC_AGENT_RESULTS,
    KAFKA_TOPIC_AGENT_TASKS,
)

logger = logging.getLogger("agent_worker")
logging.basicConfig(level=logging.INFO)

WORKER_ID = os.getenv("AGENT_WORKER_ID", socket.gethostname())
USE_LLM = os.getenv("AGENT_WORKER_USE_LLM", "0").lower() in ("1", "true", "yes")
LLM_ADAPTER_URL = os.getenv("LLM_ADAPTER_SERVICE_URL", "http://llm-adapter-service:8005")
ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
ADAPTER_SERVICE_URL = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
SEED_SAMPLE = os.getenv("AGENT_WORKER_SEED_SAMPLE", "0").lower() in ("1", "true", "yes")

consumer: AIOKafkaConsumer | None = None
producer: AIOKafkaProducer | None = None
consumer_task: asyncio.Task | None = None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def _patch_ai_team_task(task_id: str, body: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.patch(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/ai-team/execution-tasks/{task_id}",
            json=body,
            headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
        )
        r.raise_for_status()


TRACKER_STEP = "tracker_task_execute"


async def _patch_tracker_execution(project_id: str, item_id: str, body: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.patch(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/tracker/items/{item_id}/execution",
            json=body,
            headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
        )
        r.raise_for_status()


async def _handle_tracker_task(envelope: TaskEnvelope) -> tuple[TaskStatus, dict[str, Any]]:
    """Задача из трекера пользователя: LLM по промпту, статусы в admin."""
    inline = envelope.input_inline or {}
    item_id = str(inline.get("tracker_item_id") or "").strip()
    project_id = str(envelope.project_id)
    execution_id = str(envelope.task_id)
    if not item_id:
        return TaskStatus.FAILED, {"mode": "tracker", "error": "missing tracker_item_id"}

    try:
        await _patch_tracker_execution(
            project_id,
            item_id,
            {"execution_id": execution_id, "phase": "started"},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return TaskStatus.CANCELLED, {
                "mode": "tracker",
                "cancelled": True,
                "reason": "user_closed_or_stale_execution",
            }
        logger.exception("tracker patch started failed")
        return TaskStatus.FAILED, {"mode": "tracker", "error": f"patch_started: {exc}"}
    except Exception as exc:
        logger.exception("tracker patch started failed")
        return TaskStatus.FAILED, {"mode": "tracker", "error": f"patch_started: {exc}"}

    prompt = _prompt_from_task(envelope)
    result_inline: dict[str, Any] = {"mode": "tracker", "dry_run": True}
    status = TaskStatus.SUCCEEDED

    if USE_LLM:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                req = LlmGenerateRequest(
                    prompt=prompt,
                    model=None,
                    temperature=0.2,
                    purpose="coding",
                )
                resp = await client.post(
                    f"{LLM_ADAPTER_URL.rstrip('/')}/api/v1/llm/generate",
                    json=req.model_dump(exclude_none=True),
                )
                resp.raise_for_status()
                body = resp.json()
            result_inline = {
                "mode": "tracker",
                "dry_run": False,
                "output": body.get("output", ""),
                "provider": body.get("provider", ""),
            }
        except Exception as exc:
            logger.exception("tracker LLM failed")
            status = TaskStatus.FAILED
            result_inline = {"mode": "tracker", "error": str(exc)}
            try:
                await _patch_tracker_execution(
                    project_id,
                    item_id,
                    {
                        "execution_id": execution_id,
                        "phase": "finished",
                        "result_status": "failed",
                        "error": str(exc),
                        "result": result_inline,
                    },
                )
            except Exception:
                logger.warning("could not patch tracker failed state")
            return status, result_inline

    try:
        await _patch_tracker_execution(
            project_id,
            item_id,
            {
                "execution_id": execution_id,
                "phase": "finished",
                "result_status": "succeeded" if status == TaskStatus.SUCCEEDED else "failed",
                "result": result_inline,
            },
        )
    except Exception as exc:
        logger.exception("tracker patch finished failed")
        return TaskStatus.FAILED, {"mode": "tracker", "error": f"patch_finished: {exc}", **result_inline}

    return status, result_inline


async def _handle_ai_team_execution(envelope: TaskEnvelope) -> tuple[TaskStatus, dict[str, Any]]:
    """ИИ Команда: реальные вызовы adapter/admin (отделено от LLM dry-run)."""
    inline = envelope.input_inline or {}
    task_id = str(inline.get("ai_team_task_id") or "").strip()
    if not task_id:
        return TaskStatus.FAILED, {"mode": "ai_team", "error": "missing ai_team_task_id"}
    action = str(inline.get("ai_team_action") or "noop")
    payload = inline.get("payload") if isinstance(inline.get("payload"), dict) else {}
    project_id = str(envelope.project_id)

    await _patch_ai_team_task(task_id, {"status": "running", "started_at": _utc_iso()})

    try:
        if action == "noop":
            result: dict[str, Any] = {"ok": True, "mode": "noop"}
        elif action == "gitlab_pipeline":
            result = {
                "ok": True,
                "mode": "stub",
                "message": "Зарезервировано: подключите API pipeline или используйте adapter_agent_demo.",
            }
        elif action == "adapter_agent_demo":
            body: dict[str, Any] = {}
            rs = payload.get("repo_slug")
            if isinstance(rs, str) and rs.strip():
                body["repo_slug"] = rs.strip()
            async with httpx.AsyncClient(timeout=600.0) as client:
                r = await client.post(
                    f"{ADAPTER_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/agent-demo/run",
                    json=body,
                    headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                )
                r.raise_for_status()
                result = {"ok": True, "mode": "adapter_agent_demo", "adapter_response": r.json()}
        else:
            raise ValueError(f"unknown ai_team_action: {action}")

        await _patch_ai_team_task(
            task_id,
            {"status": "done", "result": result, "finished_at": _utc_iso()},
        )
        return TaskStatus.SUCCEEDED, {"mode": "ai_team", "action": action, "detail": result}
    except Exception as exc:
        logger.exception("ai_team task failed task_id=%s", task_id)
        try:
            await _patch_ai_team_task(
                task_id,
                {"status": "error", "error": str(exc), "finished_at": _utc_iso()},
            )
        except Exception:
            logger.warning("could not patch task error state")
        return TaskStatus.FAILED, {"mode": "ai_team", "action": action, "error": str(exc)}


def _prompt_from_task(envelope: TaskEnvelope) -> str:
    inline = envelope.input_inline or {}
    if isinstance(inline.get("prompt"), str):
        base = inline["prompt"]
        role = inline.get("agent_role")
        if isinstance(role, str) and role.strip():
            return f"[Агент: {role.strip()}]\n{base}"
        return base
    return (
        f"Step: {envelope.step_name or 'unknown'}\n"
        f"Project: {envelope.project_id}\n"
        f"Context: {json.dumps(inline, ensure_ascii=False)[:4000]}"
    )


async def _handle_message(payload: bytes) -> None:
    try:
        data = json.loads(payload.decode("utf-8"))
        envelope = TaskEnvelope.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("skip invalid task: %s", exc)
        return

    logger.info(
        "TaskEnvelope task_id=%s type=%s step=%s trace=%s",
        envelope.task_id,
        envelope.task_type,
        envelope.step_name,
        envelope.trace_id,
    )
    logger.debug("TaskEnvelope full: %s", envelope.model_dump_json())

    inline_pre = envelope.input_inline if isinstance(envelope.input_inline, dict) else {}
    if envelope.task_type == TaskType.EXECUTE and (
        envelope.step_name == TRACKER_STEP or inline_pre.get("tracker_item_id")
    ):
        status, result_inline = await _handle_tracker_task(envelope)
        result = TaskResult(
            task_id=envelope.task_id,
            status=status,
            task_type=envelope.task_type,
            output_inline=result_inline,
            worker_id=WORKER_ID,
        )
        if producer:
            try:
                await producer.send(
                    KAFKA_TOPIC_AGENT_RESULTS,
                    value=result.model_dump_json().encode("utf-8"),
                )
            except Exception as exc:
                logger.warning("could not publish result: %s", exc)
        return

    if (
        envelope.task_type == TaskType.EXECUTE
        and isinstance(envelope.input_inline, dict)
        and envelope.input_inline.get("ai_team_task_id")
    ):
        status, result_inline = await _handle_ai_team_execution(envelope)
        result = TaskResult(
            task_id=envelope.task_id,
            status=status,
            task_type=envelope.task_type,
            output_inline=result_inline,
            worker_id=WORKER_ID,
        )
        if producer:
            try:
                await producer.send(
                    KAFKA_TOPIC_AGENT_RESULTS,
                    value=result.model_dump_json().encode("utf-8"),
                )
            except Exception as exc:
                logger.warning("could not publish result: %s", exc)
        return

    result_inline: dict[str, Any] = {"mode": "dry_run"}
    status = TaskStatus.SUCCEEDED

    if envelope.task_type != TaskType.EXECUTE:
        logger.info("skip non-execute task_type=%s", envelope.task_type)
        status = TaskStatus.SUCCEEDED
    elif USE_LLM:
        prompt = _prompt_from_task(envelope)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                req = LlmGenerateRequest(
                    prompt=prompt,
                    model=None,
                    temperature=0.2,
                    purpose="coding",
                )
                resp = await client.post(
                    f"{LLM_ADAPTER_URL.rstrip('/')}/api/v1/llm/generate",
                    json=req.model_dump(exclude_none=True),
                )
                resp.raise_for_status()
                body = resp.json()
            result_inline = {
                "mode": "llm",
                "output": body.get("output", ""),
                "provider": body.get("provider", ""),
            }
        except Exception as exc:
            logger.exception("LLM call failed: %s", exc)
            status = TaskStatus.FAILED
            result_inline = {"mode": "llm", "error": str(exc)}

    result = TaskResult(
        task_id=envelope.task_id,
        status=status,
        task_type=envelope.task_type,
        output_inline=result_inline,
        worker_id=WORKER_ID,
    )
    if producer:
        try:
            await producer.send(
                KAFKA_TOPIC_AGENT_RESULTS,
                value=result.model_dump_json().encode("utf-8"),
            )
        except Exception as exc:
            logger.warning("could not publish result: %s", exc)


async def consume_loop() -> None:
    if not consumer:
        return
    async for msg in consumer:
        await _handle_message(msg.value)


async def _seed_sample_task() -> None:
    await asyncio.sleep(2)
    if not producer:
        return
    env = TaskEnvelope(
        task_type=TaskType.EXECUTE,
        project_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        step_name="dry_run_sample",
        input_inline={"prompt": "Say hello from agent-worker seed sample."},
    )
    await producer.send(
        KAFKA_TOPIC_AGENT_TASKS,
        value=env.model_dump_json().encode("utf-8"),
    )
    logger.info("published seed TaskEnvelope to %s", KAFKA_TOPIC_AGENT_TASKS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global consumer, producer, consumer_task
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC_AGENT_TASKS,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_GROUP_AGENT_WORKER,
        auto_offset_reset="earliest",
    )
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    try:
        await consumer.start()
        await producer.start()
        consumer_task = asyncio.create_task(consume_loop())
        if SEED_SAMPLE:
            asyncio.create_task(_seed_sample_task())
    except Exception as exc:
        logger.exception("kafka start failed: %s", exc)
        consumer = None
        producer = None
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
    if producer:
        await producer.stop()


app = FastAPI(title="agent-worker-service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "use_llm": str(USE_LLM).lower()}
