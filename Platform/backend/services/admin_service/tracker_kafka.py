"""Синхронная публикация TaskEnvelope в Kafka (admin-service, sync FastAPI routes)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from kafka import KafkaProducer

from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC_AGENT_TASKS

logger = logging.getLogger("tracker_kafka")

_producer: KafkaProducer | None = None


def _get_producer() -> KafkaProducer:
    global _producer
    if _producer is None:
        servers = [s.strip() for s in KAFKA_BOOTSTRAP_SERVERS.split(",") if s.strip()]
        _producer = KafkaProducer(
            bootstrap_servers=servers,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            acks="all",
            retries=3,
        )
    return _producer


def publish_agent_task_envelope(envelope: dict[str, Any]) -> None:
    """Публикует JSON задачи в topic platform.agent.tasks."""
    p = _get_producer()
    topic = os.getenv("KAFKA_TOPIC_AGENT_TASKS", KAFKA_TOPIC_AGENT_TASKS)
    future = p.send(topic, value=envelope)
    future.get(timeout=15)
    p.flush(timeout=10)
