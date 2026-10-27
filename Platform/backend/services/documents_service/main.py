from __future__ import annotations

import math
import os
import re
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from services.documents_service.db import get_db, init_db
from services.documents_service.models import ProjectDocument, ProjectDocumentChunk, ProjectDocumentVersion

LLM_ADAPTER_URL = os.getenv("LLM_ADAPTER_SERVICE_URL", "http://llm-adapter-service:8005")
DEFAULT_TOP_K = 8
MAX_TOP_K = 40

DOC_TYPES = frozenset(
    {
        "architecture",
        "user_guide",
        "admin_guide",
        "runbook",
        "adr",
        "api_contract",
        "spec",
        "glossary",
        "traceability",
        "other",
    }
)

STOPWORDS = frozenset(
    {
        "и",
        "в",
        "на",
        "по",
        "с",
        "для",
        "что",
        "это",
        "как",
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "for",
        "is",
    }
)

app = FastAPI(title="documents-service", version="0.1.0")


class DocumentCreateBody(BaseModel):
    title: str = Field(..., min_length=3, max_length=512)
    doc_type: str = Field(default="other")
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)
    content_md: str = Field(default="")
    change_summary: str = Field(default="Document created")
    author_type: str = Field(default="user")
    author_id: str | None = None
    run_id: str | None = None


class DocumentPatchBody(BaseModel):
    title: str | None = None
    doc_type: str | None = None
    slug: str | None = None
    status: str | None = None
    tags: list[str] | None = None


class VersionCreateBody(BaseModel):
    content_md: str = Field(..., min_length=1)
    change_summary: str = Field(default="Updated")
    author_type: str = Field(default="agent")
    author_id: str | None = None
    run_id: str | None = None
    expected_base_version: int | None = None


class SearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000)
    doc_types: list[str] = Field(default_factory=list)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    include_archived: bool = False


class UpsertBody(BaseModel):
    doc_type: str = Field(default="other")
    title: str = Field(..., min_length=2, max_length=512)
    content_md: str = Field(..., min_length=1)
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)
    change_summary: str = Field(default="Auto update from agent")
    author_type: str = Field(default="agent")
    author_id: str | None = None
    run_id: str | None = None


class ContextBody(BaseModel):
    query: str = Field(default="")
    doc_types: list[str] = Field(default_factory=list)
    top_k: int = Field(default=8, ge=1, le=25)


class ReindexBody(BaseModel):
    doc_id: str | None = None


def _utcnow() -> datetime:
    return datetime.utcnow()


def _doc_type(raw: str | None) -> str:
    v = (raw or "other").strip().lower()
    return v if v in DOC_TYPES else "other"


def _slugify(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9а-яА-Я_-]+", "-", (raw or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s or "doc")[:150]


def _normalize_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        v = str(t).strip().lower()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v[:64])
    return out


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Zа-яА-Я0-9_]{2,}", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def _split_markdown(md: str, max_chars: int = 1200, overlap: int = 150) -> list[tuple[str | None, str]]:
    lines = (md or "").splitlines()
    chunks: list[tuple[str | None, str]] = []
    heading: str | None = None
    buf = ""
    for line in lines:
        if line.startswith("#"):
            heading = line.strip("# ").strip()[:280] or heading
        candidate = (buf + "\n" + line).strip() if buf else line
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf.strip():
            chunks.append((heading, buf.strip()))
        tail = (buf[-overlap:] if overlap > 0 and len(buf) > overlap else buf).strip()
        buf = ((tail + "\n" + line).strip() if tail else line).strip()
    if buf.strip():
        chunks.append((heading, buf.strip()))
    out: list[tuple[str | None, str]] = []
    for h, c in chunks:
        if len(c) < 20:
            continue
        out.append((h, c))
    return out


def _cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _embed_many(texts: list[str]) -> tuple[list[list[float]] | None, str | None, str | None]:
    if not texts:
        return [], None, None
    try:
        with httpx.Client(timeout=120.0) as client:
            r = client.post(f"{LLM_ADAPTER_URL.rstrip('/')}/api/v1/llm/embed", json={"input": texts})
            if r.status_code != 200:
                return None, None, None
            body = r.json()
            embeds = body.get("embeddings")
            if not isinstance(embeds, list):
                return None, None, None
            valid: list[list[float]] = []
            for item in embeds:
                if isinstance(item, list):
                    valid.append([float(x) for x in item if isinstance(x, (float, int))])
            return valid, str(body.get("model") or ""), str(body.get("provider") or "")
    except Exception:
        return None, None, None


def _create_version(
    db: Session,
    *,
    doc: ProjectDocument,
    content_md: str,
    change_summary: str,
    author_type: str,
    author_id: str | None,
    run_id: str | None,
) -> ProjectDocumentVersion:
    next_ver = int(doc.latest_version or 0) + 1
    ver = ProjectDocumentVersion(
        document_id=doc.id,
        project_id=doc.project_id,
        version=next_ver,
        content_md=content_md,
        change_summary=(change_summary or "").strip()[:8000],
        author_type=(author_type or "agent").strip()[:32],
        author_id=(author_id or "").strip()[:128] or None,
        run_id=(run_id or "").strip()[:64] or None,
        created_at=_utcnow(),
    )
    db.add(ver)
    db.flush()

    pairs = _split_markdown(content_md)
    texts = [p[1] for p in pairs]
    embeddings, emb_model, emb_provider = _embed_many(texts)
    db.execute(delete(ProjectDocumentChunk).where(ProjectDocumentChunk.document_version_id == ver.id))
    for idx, (heading, text) in enumerate(pairs):
        emb = None
        if embeddings and idx < len(embeddings):
            emb = embeddings[idx]
        db.add(
            ProjectDocumentChunk(
                project_id=doc.project_id,
                document_id=doc.id,
                document_version_id=ver.id,
                chunk_idx=idx,
                heading=heading,
                chunk_text=text,
                char_count=len(text),
                embedding=emb,
                embedding_model=emb_model if emb is not None else None,
                embedding_provider=emb_provider if emb is not None else None,
                created_at=_utcnow(),
            )
        )
    doc.latest_version = next_ver
    doc.updated_at = _utcnow()
    db.flush()
    return ver


def _serialize_doc(doc: ProjectDocument) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "project_id": str(doc.project_id),
        "slug": doc.slug,
        "title": doc.title,
        "doc_type": doc.doc_type,
        "status": doc.status,
        "tags": doc.tags or [],
        "latest_version": int(doc.latest_version or 0),
        "created_by": doc.created_by,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "deleted_at": doc.deleted_at.isoformat() if doc.deleted_at else None,
    }


def _serialize_ver(v: ProjectDocumentVersion) -> dict[str, Any]:
    return {
        "id": str(v.id),
        "document_id": str(v.document_id),
        "project_id": str(v.project_id),
        "version": int(v.version),
        "content_md": v.content_md,
        "change_summary": v.change_summary,
        "author_type": v.author_type,
        "author_id": v.author_id,
        "run_id": v.run_id,
        "created_at": v.created_at.isoformat(),
    }


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/projects/{project_id}/documents")
def list_documents(
    project_id: UUID,
    db: Session = Depends(get_db),
    doc_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    stmt = select(ProjectDocument).where(ProjectDocument.project_id == project_id)
    if not include_deleted:
        stmt = stmt.where(ProjectDocument.deleted_at.is_(None))
    if doc_type:
        stmt = stmt.where(ProjectDocument.doc_type == _doc_type(doc_type))
    if status:
        stmt = stmt.where(ProjectDocument.status == status.strip().lower())
    if q and q.strip():
        qq = f"%{q.strip().lower()}%"
        stmt = stmt.where((ProjectDocument.title.ilike(qq)) | (ProjectDocument.slug.ilike(qq)))
    rows = list(db.execute(stmt.order_by(desc(ProjectDocument.updated_at)).limit(limit)).scalars().all())
    return {"items": [_serialize_doc(r) for r in rows]}


@app.post("/api/v1/projects/{project_id}/documents")
def create_document(project_id: UUID, body: DocumentCreateBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    slug = _slugify(body.slug or f"{body.doc_type}-{body.title}")
    exists = db.execute(
        select(ProjectDocument).where(
            ProjectDocument.project_id == project_id,
            ProjectDocument.slug == slug,
            ProjectDocument.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="document with this slug already exists")
    doc = ProjectDocument(
        project_id=project_id,
        slug=slug,
        title=body.title.strip(),
        doc_type=_doc_type(body.doc_type),
        status="active",
        tags=_normalize_tags(body.tags),
        latest_version=0,
        created_by=(body.author_id or "").strip()[:128] or None,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(doc)
    db.flush()
    if body.content_md.strip():
        _create_version(
            db,
            doc=doc,
            content_md=body.content_md,
            change_summary=body.change_summary,
            author_type=body.author_type,
            author_id=body.author_id,
            run_id=body.run_id,
        )
    db.commit()
    db.refresh(doc)
    return {"document": _serialize_doc(doc)}


@app.get("/api/v1/projects/{project_id}/documents/{doc_id}")
def get_document(project_id: UUID, doc_id: UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or str(doc.project_id) != str(project_id) or doc.deleted_at is not None:
        raise HTTPException(status_code=404, detail="document not found")
    current = db.execute(
        select(ProjectDocumentVersion).where(
            ProjectDocumentVersion.document_id == doc.id,
            ProjectDocumentVersion.version == doc.latest_version,
        )
    ).scalar_one_or_none()
    return {"document": _serialize_doc(doc), "current_version": _serialize_ver(current) if current else None}


@app.patch("/api/v1/projects/{project_id}/documents/{doc_id}")
def patch_document(
    project_id: UUID,
    doc_id: UUID,
    body: DocumentPatchBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or str(doc.project_id) != str(project_id) or doc.deleted_at is not None:
        raise HTTPException(status_code=404, detail="document not found")
    if body.title is not None and body.title.strip():
        doc.title = body.title.strip()[:512]
    if body.doc_type is not None:
        doc.doc_type = _doc_type(body.doc_type)
    if body.slug is not None and body.slug.strip():
        slug = _slugify(body.slug)
        clash = db.execute(
            select(ProjectDocument).where(
                ProjectDocument.project_id == project_id,
                ProjectDocument.slug == slug,
                ProjectDocument.id != doc.id,
            )
        ).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=409, detail="slug already used")
        doc.slug = slug
    if body.status is not None and body.status.strip():
        doc.status = body.status.strip().lower()[:32]
    if body.tags is not None:
        doc.tags = _normalize_tags(body.tags)
    doc.updated_at = _utcnow()
    db.commit()
    db.refresh(doc)
    return {"document": _serialize_doc(doc)}


@app.delete("/api/v1/projects/{project_id}/documents/{doc_id}")
def delete_document(
    project_id: UUID,
    doc_id: UUID,
    db: Session = Depends(get_db),
    hard: bool = Query(default=False),
) -> dict[str, Any]:
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or str(doc.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="document not found")
    if hard:
        db.delete(doc)
    else:
        doc.deleted_at = _utcnow()
        doc.status = "deleted"
        doc.updated_at = _utcnow()
    db.commit()
    return {"status": "ok", "hard_deleted": hard}


@app.get("/api/v1/projects/{project_id}/documents/{doc_id}/versions")
def list_versions(project_id: UUID, doc_id: UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or str(doc.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="document not found")
    rows = list(
        db.execute(
            select(ProjectDocumentVersion)
            .where(ProjectDocumentVersion.document_id == doc.id)
            .order_by(desc(ProjectDocumentVersion.version))
        ).scalars().all()
    )
    return {"items": [_serialize_ver(r) for r in rows], "latest_version": doc.latest_version}


@app.post("/api/v1/projects/{project_id}/documents/{doc_id}/versions")
def create_version(
    project_id: UUID,
    doc_id: UUID,
    body: VersionCreateBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or str(doc.project_id) != str(project_id) or doc.deleted_at is not None:
        raise HTTPException(status_code=404, detail="document not found")
    if body.expected_base_version is not None and int(body.expected_base_version) != int(doc.latest_version):
        raise HTTPException(status_code=409, detail="base version mismatch")
    ver = _create_version(
        db,
        doc=doc,
        content_md=body.content_md,
        change_summary=body.change_summary,
        author_type=body.author_type,
        author_id=body.author_id,
        run_id=body.run_id,
    )
    db.commit()
    return {"version": _serialize_ver(ver), "document": _serialize_doc(doc)}


@app.get("/api/v1/projects/{project_id}/documents/{doc_id}/versions/{version}")
def get_version(project_id: UUID, doc_id: UUID, version: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or str(doc.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="document not found")
    row = db.execute(
        select(ProjectDocumentVersion).where(
            ProjectDocumentVersion.document_id == doc.id,
            ProjectDocumentVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="version not found")
    return {"version": _serialize_ver(row)}


@app.post("/api/v1/projects/{project_id}/documents/{doc_id}/restore/{version}")
def restore_version(project_id: UUID, doc_id: UUID, version: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or str(doc.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="document not found")
    row = db.execute(
        select(ProjectDocumentVersion).where(
            ProjectDocumentVersion.document_id == doc.id,
            ProjectDocumentVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="version not found")
    new_ver = _create_version(
        db,
        doc=doc,
        content_md=row.content_md,
        change_summary=f"Restore from version {version}",
        author_type="system",
        author_id="restore",
        run_id=row.run_id,
    )
    db.commit()
    return {"restored_to_version": _serialize_ver(new_ver)}


@app.post("/api/v1/projects/{project_id}/documents/upsert")
def upsert_document(project_id: UUID, body: UpsertBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    slug = _slugify(body.slug or f"{body.doc_type}-{body.title}")
    doc = db.execute(
        select(ProjectDocument).where(
            ProjectDocument.project_id == project_id,
            ProjectDocument.slug == slug,
        )
    ).scalar_one_or_none()
    if doc is None:
        doc = ProjectDocument(
            project_id=project_id,
            slug=slug,
            title=body.title.strip(),
            doc_type=_doc_type(body.doc_type),
            status="active",
            tags=_normalize_tags(body.tags),
            latest_version=0,
            created_by=(body.author_id or "").strip()[:128] or "agent",
            created_at=_utcnow(),
            updated_at=_utcnow(),
            deleted_at=None,
        )
        db.add(doc)
        db.flush()
    else:
        doc.title = body.title.strip()[:512]
        doc.doc_type = _doc_type(body.doc_type)
        if body.tags:
            doc.tags = _normalize_tags(body.tags)
        if doc.deleted_at is not None:
            doc.deleted_at = None
            doc.status = "active"
        doc.updated_at = _utcnow()
    current = db.execute(
        select(ProjectDocumentVersion).where(
            ProjectDocumentVersion.document_id == doc.id,
            ProjectDocumentVersion.version == doc.latest_version,
        )
    ).scalar_one_or_none()
    if current and (current.content_md or "").strip() == body.content_md.strip():
        db.commit()
        return {"status": "unchanged", "document": _serialize_doc(doc), "version": _serialize_ver(current)}
    ver = _create_version(
        db,
        doc=doc,
        content_md=body.content_md,
        change_summary=body.change_summary,
        author_type=body.author_type,
        author_id=body.author_id,
        run_id=body.run_id,
    )
    db.commit()
    return {"status": "ok", "document": _serialize_doc(doc), "version": _serialize_ver(ver)}


@app.post("/api/v1/projects/{project_id}/documents/reindex")
def reindex_documents(project_id: UUID, body: ReindexBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    docs_stmt = select(ProjectDocument).where(ProjectDocument.project_id == project_id, ProjectDocument.deleted_at.is_(None))
    if body.doc_id:
        try:
            did = UUID(body.doc_id)
            docs_stmt = docs_stmt.where(ProjectDocument.id == did)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid doc_id: {exc}") from exc
    docs = list(db.execute(docs_stmt).scalars().all())
    changed = 0
    for doc in docs:
        if not doc.latest_version:
            continue
        ver = db.execute(
            select(ProjectDocumentVersion).where(
                ProjectDocumentVersion.document_id == doc.id,
                ProjectDocumentVersion.version == doc.latest_version,
            )
        ).scalar_one_or_none()
        if ver is None:
            continue
        db.execute(delete(ProjectDocumentChunk).where(ProjectDocumentChunk.document_version_id == ver.id))
        pairs = _split_markdown(ver.content_md)
        texts = [p[1] for p in pairs]
        embeds, model, provider = _embed_many(texts)
        for idx, (heading, text) in enumerate(pairs):
            db.add(
                ProjectDocumentChunk(
                    project_id=doc.project_id,
                    document_id=doc.id,
                    document_version_id=ver.id,
                    chunk_idx=idx,
                    heading=heading,
                    chunk_text=text,
                    char_count=len(text),
                    embedding=embeds[idx] if embeds and idx < len(embeds) else None,
                    embedding_model=model if embeds else None,
                    embedding_provider=provider if embeds else None,
                    created_at=_utcnow(),
                )
            )
        changed += 1
    db.commit()
    return {"status": "ok", "reindexed": changed}


@app.post("/api/v1/projects/{project_id}/documents/search")
def search_documents(project_id: UUID, body: SearchBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    q = body.query.strip()
    if not q:
        return {"items": []}
    dtypes = {_doc_type(x) for x in body.doc_types if x}
    docs_stmt = select(ProjectDocument).where(ProjectDocument.project_id == project_id, ProjectDocument.deleted_at.is_(None))
    if not body.include_archived:
        docs_stmt = docs_stmt.where(ProjectDocument.status == "active")
    if dtypes:
        docs_stmt = docs_stmt.where(ProjectDocument.doc_type.in_(list(dtypes)))
    docs = list(db.execute(docs_stmt).scalars().all())
    if not docs:
        return {"items": []}
    doc_ids = [d.id for d in docs]
    latest_by_doc = {d.id: int(d.latest_version or 0) for d in docs}
    vers = list(
        db.execute(select(ProjectDocumentVersion).where(ProjectDocumentVersion.document_id.in_(doc_ids))).scalars().all()
    )
    latest_ver_ids = {v.id for v in vers if int(v.version) == latest_by_doc.get(v.document_id)}
    if not latest_ver_ids:
        return {"items": []}
    chunks = list(
        db.execute(
            select(ProjectDocumentChunk).where(
                ProjectDocumentChunk.project_id == project_id,
                ProjectDocumentChunk.document_version_id.in_(list(latest_ver_ids)),
            )
        ).scalars().all()
    )
    if not chunks:
        return {"items": []}
    q_tokens = _tokenize(q)
    q_embed_list, _m, _p = _embed_many([q])
    q_embed = q_embed_list[0] if q_embed_list and len(q_embed_list) > 0 else None
    doc_map = {d.id: d for d in docs}
    scored: list[tuple[float, ProjectDocumentChunk]] = []
    for ch in chunks:
        text_l = (ch.chunk_text or "").lower()
        lex = 0.0
        if q_tokens:
            hits = sum(1 for t in q_tokens if t in text_l)
            lex = hits / max(1, len(q_tokens))
        cos = _cosine_similarity(q_embed, ch.embedding if isinstance(ch.embedding, list) else None)
        score = (0.65 * cos) + (0.35 * lex)
        if score > 0.0001:
            scored.append((score, ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, ch in scored[: body.top_k]:
        d = doc_map.get(ch.document_id)
        if not d:
            continue
        excerpt = (ch.chunk_text or "").strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:600] + "…"
        out.append(
            {
                "score": round(score, 6),
                "document_id": str(d.id),
                "slug": d.slug,
                "title": d.title,
                "doc_type": d.doc_type,
                "version": int(latest_by_doc.get(d.id, 0)),
                "chunk_idx": ch.chunk_idx,
                "heading": ch.heading,
                "excerpt": excerpt,
            }
        )
    return {"items": out}


@app.post("/api/v1/projects/{project_id}/documents/context")
def docs_context(project_id: UUID, body: ContextBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    query = body.query.strip() or "architecture deployment api ui admin"
    data = search_documents(
        project_id=project_id,
        body=SearchBody(query=query, doc_types=body.doc_types, top_k=body.top_k, include_archived=False),
        db=db,
    )
    items = data.get("items") or []
    if not items:
        return {"summary_md": "(документация проекта пока пуста)", "items": []}
    lines = ["### Релевантная документация проекта"]
    for it in items:
        lines.append(
            f"- [{it.get('doc_type')}] {it.get('title')} (slug: {it.get('slug')}, v{it.get('version')}, score={it.get('score')})\n"
            f"  {it.get('excerpt')}"
        )
    return {"summary_md": "\n".join(lines), "items": items}
