"""Базы знаний: платформа + проекты, S3, очередь Kafka, Qdrant."""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.knowledge_service import s3_store
from services.knowledge_service.db import get_db, init_db
from services.knowledge_service.ingest import search_knowledge
from services.knowledge_service.kafka_pub import publish_ingest
from services.knowledge_service.lifecycle import (
    delete_document,
    delete_knowledge_base,
    set_document_enabled,
)
from services.knowledge_service.models import (
    KB_CATEGORIES,
    DOC_PENDING,
    KnowledgeBase,
    KnowledgeDocument,
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
)
from services.knowledge_service.qdrant_store import COLLECTION, ping as qdrant_ping
from services.knowledge_service.worker import start_worker, stop_worker

logger = logging.getLogger("knowledge_service")

INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
MAX_UPLOAD_BYTES = int(os.getenv("KNOWLEDGE_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
ALLOWED_MIME_PREFIXES = ("application/pdf", "image/", "text/")


def _utcnow() -> datetime:
    return datetime.utcnow()


def require_internal(x_internal_token: str | None = Header(None, alias="X-Internal-Token")) -> None:
    if (x_internal_token or "").strip() != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


def _slugify(raw: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return (s[:60] or "kb")


def _serialize_kb(row: KnowledgeBase) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "scope": row.scope,
        "project_id": str(row.project_id) if row.project_id else None,
        "slug": row.slug,
        "name": row.name,
        "description": row.description or "",
        "category": row.category,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_doc(row: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "knowledge_base_id": str(row.knowledge_base_id),
        "scope": row.scope,
        "project_id": str(row.project_id) if row.project_id else None,
        "filename": row.filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "status": row.status,
        "enabled": bool(row.enabled),
        "error_message": row.error_message,
        "extracted_chars": row.extracted_chars,
        "chunk_count": row.chunk_count,
        "recognition_model": row.recognition_model,
        "embedding_model": row.embedding_model,
        "uploaded_by": row.uploaded_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class KnowledgeBaseCreateBody(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=2, max_length=256)
    description: str = ""
    category: str = Field(default="other")
    created_by: str | None = None


class SearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000)
    kb_ids: list[str] = Field(default_factory=list)
    include_platform: bool = True
    top_k: int = Field(default=8, ge=1, le=40)


class DocumentPatchBody(BaseModel):
    enabled: bool | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    try:
        s3_store.ensure_bucket()
    except Exception as exc:
        logger.warning("knowledge s3 bucket: %s", exc)
    start_worker()
    yield
    stop_worker()


app = FastAPI(title="knowledge-service", version="0.1.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if not isinstance(detail, str):
            detail = str(detail)
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})
    logger.exception("unhandled knowledge-service error")
    return JSONResponse(status_code=500, content={"detail": str(exc)[:800]})


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "qdrant": "ok" if qdrant_ping() else "unreachable",
        "qdrant_collection": COLLECTION,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


def _get_kb_or_404(db: Session, kb_id: UUID, *, project_id: UUID | None = None) -> KnowledgeBase:
    row = db.get(KnowledgeBase, kb_id)
    if row is None or row.status != "active":
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if project_id is not None and (
        row.scope != SCOPE_PROJECT or str(row.project_id or "") != str(project_id)
    ):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return row


@app.get("/api/v1/internal/knowledge/bases")
def list_platform_bases(
    category: str | None = None,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(KnowledgeBase).where(KnowledgeBase.scope == SCOPE_PLATFORM, KnowledgeBase.status == "active")
    if category:
        stmt = stmt.where(KnowledgeBase.category == category.strip().lower())
    rows = list(db.execute(stmt.order_by(KnowledgeBase.name)).scalars().all())
    return {"items": [_serialize_kb(r) for r in rows]}


@app.post("/api/v1/internal/knowledge/bases")
def create_platform_base(
    body: KnowledgeBaseCreateBody,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    cat = body.category.strip().lower()
    if cat not in KB_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    slug = _slugify(body.slug)
    exists = db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.scope == SCOPE_PLATFORM,
            KnowledgeBase.project_id.is_(None),
            KnowledgeBase.slug == slug,
            KnowledgeBase.status == "active",
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="slug already exists")
    row = KnowledgeBase(
        scope=SCOPE_PLATFORM,
        project_id=None,
        slug=slug,
        name=body.name.strip(),
        description=(body.description or "").strip(),
        category=cat,
        created_by=(body.created_by or "admin")[:128],
        status="active",
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_kb(row)


@app.get("/api/v1/internal/projects/{project_id}/knowledge/bases")
def list_project_bases(
    project_id: UUID,
    category: str | None = None,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(KnowledgeBase).where(
        KnowledgeBase.scope == SCOPE_PROJECT,
        KnowledgeBase.project_id == project_id,
        KnowledgeBase.status == "active",
    )
    if category:
        stmt = stmt.where(KnowledgeBase.category == category.strip().lower())
    rows = list(db.execute(stmt.order_by(KnowledgeBase.name)).scalars().all())
    return {"items": [_serialize_kb(r) for r in rows]}


@app.post("/api/v1/internal/projects/{project_id}/knowledge/bases")
def create_project_base(
    project_id: UUID,
    body: KnowledgeBaseCreateBody,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    cat = body.category.strip().lower()
    if cat not in KB_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    slug = _slugify(body.slug)
    exists = db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.scope == SCOPE_PROJECT,
            KnowledgeBase.project_id == project_id,
            KnowledgeBase.slug == slug,
            KnowledgeBase.status == "active",
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="slug already exists")
    row = KnowledgeBase(
        scope=SCOPE_PROJECT,
        project_id=project_id,
        slug=slug,
        name=body.name.strip(),
        description=(body.description or "").strip(),
        category=cat,
        created_by=(body.created_by or "user")[:128],
        status="active",
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_kb(row)


@app.get("/api/v1/internal/knowledge/bases/{kb_id}/documents")
def list_documents(
    kb_id: UUID,
    project_id: UUID | None = None,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id, project_id=project_id)
    rows = list(
        db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == kb.id)
            .order_by(KnowledgeDocument.created_at.desc())
        )
        .scalars()
        .all()
    )
    return {"knowledge_base": _serialize_kb(kb), "items": [_serialize_doc(r) for r in rows]}


def _get_doc_or_404(db: Session, kb: KnowledgeBase, doc_id: UUID) -> KnowledgeDocument:
    row = db.get(KnowledgeDocument, doc_id)
    if row is None or row.knowledge_base_id != kb.id:
        raise HTTPException(status_code=404, detail="document not found")
    return row


@app.delete("/api/v1/internal/knowledge/bases/{kb_id}")
def delete_platform_base(
    kb_id: UUID,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.scope != SCOPE_PLATFORM or kb.status != "active":
        raise HTTPException(status_code=404, detail="knowledge base not found")
    n = delete_knowledge_base(db, kb_id)
    return {"status": "deleted", "documents_removed": n}


@app.delete("/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}")
def delete_project_base(
    project_id: UUID,
    kb_id: UUID,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id, project_id=project_id)
    n = delete_knowledge_base(db, kb_id)
    return {"status": "deleted", "documents_removed": n}


@app.delete("/api/v1/internal/knowledge/bases/{kb_id}/documents/{doc_id}")
def delete_platform_document(
    kb_id: UUID,
    doc_id: UUID,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id)
    _get_doc_or_404(db, kb, doc_id)
    delete_document(db, doc_id)
    return {"status": "deleted"}


@app.delete("/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}/documents/{doc_id}")
def delete_project_document(
    project_id: UUID,
    kb_id: UUID,
    doc_id: UUID,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id, project_id=project_id)
    _get_doc_or_404(db, kb, doc_id)
    delete_document(db, doc_id)
    return {"status": "deleted"}


@app.patch("/api/v1/internal/knowledge/bases/{kb_id}/documents/{doc_id}")
def patch_platform_document(
    kb_id: UUID,
    doc_id: UUID,
    body: DocumentPatchBody,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id)
    _get_doc_or_404(db, kb, doc_id)
    if body.enabled is None:
        raise HTTPException(status_code=400, detail="enabled is required")
    try:
        row = set_document_enabled(db, kb, doc_id, enabled=body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_doc(row)


@app.patch("/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}/documents/{doc_id}")
def patch_project_document(
    project_id: UUID,
    kb_id: UUID,
    doc_id: UUID,
    body: DocumentPatchBody,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id, project_id=project_id)
    _get_doc_or_404(db, kb, doc_id)
    if body.enabled is None:
        raise HTTPException(status_code=400, detail="enabled is required")
    try:
        row = set_document_enabled(db, kb, doc_id, enabled=body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_doc(row)


async def _save_upload(
    db: Session,
    *,
    kb: KnowledgeBase,
    file: UploadFile,
    uploaded_by: str | None,
) -> KnowledgeDocument:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {MAX_UPLOAD_BYTES} bytes limit",
        )
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="empty file")
    mime = (file.content_type or "application/octet-stream").strip().lower()
    if not any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        raise HTTPException(status_code=400, detail=f"mime not allowed: {mime}")
    from uuid import uuid4

    filename = (file.filename or "upload.bin").strip()[:512]
    doc_id = uuid4()
    key = s3_store.object_key(scope=kb.scope, kb_id=str(kb.id), document_id=str(doc_id), filename=filename)
    try:
        s3_store.put_bytes(key, data, mime)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"s3 upload failed: {exc}") from exc
    row = KnowledgeDocument(
        id=doc_id,
        knowledge_base_id=kb.id,
        scope=kb.scope,
        project_id=kb.project_id,
        filename=filename,
        mime_type=mime,
        size_bytes=len(data),
        s3_key=key,
        status=DOC_PENDING,
        enabled=True,
        uploaded_by=(uploaded_by or "user")[:128],
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    try:
        publish_ingest(str(row.id))
    except Exception as exc:
        logger.warning("kafka publish failed, worker will pick pending: %s", exc)
    return row


@app.post("/api/v1/internal/knowledge/bases/{kb_id}/documents")
async def upload_platform_document(
    kb_id: UUID,
    file: UploadFile = File(...),
    uploaded_by: str | None = Query(None),
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id)
    if kb.scope != SCOPE_PLATFORM:
        raise HTTPException(status_code=400, detail="not a platform knowledge base")
    row = await _save_upload(db, kb=kb, file=file, uploaded_by=uploaded_by)
    return _serialize_doc(row)


@app.post("/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}/documents")
async def upload_project_document(
    project_id: UUID,
    kb_id: UUID,
    file: UploadFile = File(...),
    uploaded_by: str | None = Query(None),
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb = _get_kb_or_404(db, kb_id, project_id=project_id)
    row = await _save_upload(db, kb=kb, file=file, uploaded_by=uploaded_by)
    return _serialize_doc(row)


@app.post("/api/v1/internal/knowledge/search")
def search_platform(
    body: SearchBody,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb_uuids: list[UUID] | None = None
    if body.kb_ids:
        kb_uuids = []
        for raw in body.kb_ids:
            try:
                kb_uuids.append(UUID(raw.strip()))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"invalid kb_id: {raw}") from exc
    items = search_knowledge(
        db,
        query=body.query,
        project_id=None,
        kb_ids=kb_uuids,
        include_platform=True,
        top_k=body.top_k,
    )
    return {"items": items, "vector_backend": "qdrant"}


@app.post("/api/v1/internal/projects/{project_id}/knowledge/search")
def search_project(
    project_id: UUID,
    body: SearchBody,
    _: None = Depends(require_internal),
    db: Session = Depends(get_db),
) -> dict:
    kb_uuids: list[UUID] | None = None
    if body.kb_ids:
        kb_uuids = []
        for raw in body.kb_ids:
            try:
                kb_uuids.append(UUID(raw.strip()))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"invalid kb_id: {raw}") from exc
    items = search_knowledge(
        db,
        query=body.query,
        project_id=project_id,
        kb_ids=kb_uuids,
        include_platform=body.include_platform,
        top_k=body.top_k,
    )
    return {"items": items, "vector_backend": "qdrant"}
