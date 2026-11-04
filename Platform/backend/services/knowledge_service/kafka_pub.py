"""Publish indexing tasks to Kafka."""

from __future__ import annotations

import json
import logging
import os

from kafka import KafkaProducer

from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC_KNOWLEDGE_INGEST

logger = logging.getLogger("knowledge_kafka_pub")

_producer: KafkaProducer | None = None


def _producer_client() -> KafkaProducer:
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


def publish_ingest(document_id: str) -> None:
    topic = os.getenv("KAFKA_TOPIC_KNOWLEDGE_INGEST", KAFKA_TOPIC_KNOWLEDGE_INGEST)
    payload = {"document_id": document_id, "action": "ingest"}
    future = _producer_client().send(topic, value=payload)
    future.get(timeout=15)
    _producer_client().flush(timeout=10)
