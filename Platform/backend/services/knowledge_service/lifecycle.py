"""Удаление баз/документов и enable/disable (Qdrant + S3)."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from services.knowledge_service import s3_store
from services.knowledge_service.ingest import reindex_document_vectors
from services.knowledge_service.kafka_pub import publish_ingest
from services.knowledge_service.models import (
    DOC_PENDING,
    DOC_READY,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
)
from services.knowledge_service.qdrant_store import delete_document_points

logger = logging.getLogger("knowledge_lifecycle")


def _get_doc_in_kb(db: Session, kb: KnowledgeBase, doc_id: UUID) -> KnowledgeDocument:
    row = db.get(KnowledgeDocument, doc_id)
    if row is None or row.knowledge_base_id != kb.id:
        raise ValueError("document not found")
    return row


def delete_document(db: Session, doc_id: UUID) -> None:
    row = db.get(KnowledgeDocument, doc_id)
    if row is None:
        return
    try:
        delete_document_points(str(row.id))
    except Exception as exc:
        logger.warning("qdrant delete doc=%s: %s", doc_id, exc)
    try:
        s3_store.delete_object(row.s3_key)
    except Exception as exc:
        logger.warning("s3 delete doc=%s: %s", doc_id, exc)
    db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    db.commit()


def delete_knowledge_base(db: Session, kb_id: UUID) -> int:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise ValueError("knowledge base not found")
    docs = list(
        db.execute(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb_id)).scalars().all()
    )
    for doc in docs:
        try:
            delete_document_points(str(doc.id))
        except Exception:
            pass
        try:
            s3_store.delete_object(doc.s3_key)
        except Exception:
            pass
    db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb_id))
    kb.status = "deleted"
    from datetime import datetime

    kb.updated_at = datetime.utcnow()
    db.commit()
    return len(docs)


def set_document_enabled(db: Session, kb: KnowledgeBase, doc_id: UUID, *, enabled: bool) -> KnowledgeDocument:
    row = _get_doc_in_kb(db, kb, doc_id)
    if bool(row.enabled) == enabled:
        return row

    row.enabled = enabled
    from datetime import datetime

    row.updated_at = datetime.utcnow()

    if not enabled:
        try:
            delete_document_points(str(row.id))
        except Exception as exc:
            logger.warning("qdrant disable doc=%s: %s", doc_id, exc)
        db.commit()
        db.refresh(row)
        return row

    db.commit()
    db.refresh(row)
    if row.status == DOC_READY and row.chunk_count > 0:
        reindex_document_vectors(db, row.id)
        return row
    from services.knowledge_service.models import DOC_FAILED

    if row.status in (DOC_PENDING, DOC_FAILED):
        row.status = DOC_PENDING
        row.error_message = None
        db.commit()
        try:
            publish_ingest(str(row.id))
        except Exception as exc:
            logger.warning("kafka publish on enable doc=%s: %s", doc_id, exc)
    db.refresh(row)
    return row
