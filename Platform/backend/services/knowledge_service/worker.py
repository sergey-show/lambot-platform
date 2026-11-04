"""Background Kafka consumer + catch up pending on start."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from uuid import UUID

from kafka import KafkaConsumer
from sqlalchemy import select

from services.knowledge_service.db import SessionLocal, init_db
from services.knowledge_service.ingest import process_document
from services.knowledge_service.models import DOC_PENDING, KnowledgeDocument
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_GROUP_KNOWLEDGE, KAFKA_TOPIC_KNOWLEDGE_INGEST

logger = logging.getLogger("knowledge_worker")

_stop = threading.Event()
_thread: threading.Thread | None = None


def _drain_pending_once() -> None:
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.status == DOC_PENDING).limit(20)
            ).scalars().all()
        )
        for row in rows:
            try:
                process_document(db, row.id)
            except Exception:
                logger.exception("pending ingest failed doc=%s", row.id)


def _consume_loop() -> None:
    servers = [s.strip() for s in KAFKA_BOOTSTRAP_SERVERS.split(",") if s.strip()]
    topic = os.getenv("KAFKA_TOPIC_KNOWLEDGE_INGEST", KAFKA_TOPIC_KNOWLEDGE_INGEST)
    group = os.getenv("KAFKA_GROUP_KNOWLEDGE", KAFKA_GROUP_KNOWLEDGE)
    try:
        consumer = KafkaConsumer(
            topic,
            bootstrap_servers=servers,
            group_id=group,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
    except Exception as exc:
        logger.warning("knowledge kafka consumer unavailable: %s", exc)
        while not _stop.is_set():
            _drain_pending_once()
            time.sleep(10)
        return

    logger.info("knowledge worker listening topic=%s group=%s", topic, group)
    while not _stop.is_set():
        polled = consumer.poll(timeout_ms=2000, max_records=5)
        if not polled:
            continue
        for _tp, records in polled.items():
            for rec in records:
                val = rec.value if isinstance(rec.value, dict) else {}
                doc_id = str(val.get("document_id") or "").strip()
                if not doc_id:
                    continue
                try:
                    with SessionLocal() as db:
                        process_document(db, UUID(doc_id))
                except Exception:
                    logger.exception("kafka ingest failed doc=%s", doc_id)


def start_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    init_db()
    _drain_pending_once()

    def run() -> None:
        while not _stop.is_set():
            try:
                _consume_loop()
            except Exception:
                logger.exception("knowledge worker loop error")
                time.sleep(5)

    _thread = threading.Thread(target=run, name="knowledge-ingest-worker", daemon=True)
    _thread.start()


def stop_worker() -> None:
    _stop.set()
