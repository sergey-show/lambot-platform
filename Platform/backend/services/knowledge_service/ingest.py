"""Чанкинг, эмбеддинг, запись в Qdrant + Postgres."""

from __future__ import annotations

import logging
import os
import re
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from services.knowledge_service import s3_store
from services.knowledge_service.models import (
    DOC_FAILED,
    DOC_PROCESSING,
    DOC_READY,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
)
from services.knowledge_service.qdrant_store import delete_document_points, upsert_rows
from services.knowledge_service.text_extract import extract_document
from shared.rag_util import embed_many, rerank_llm, tokenize

logger = logging.getLogger("knowledge_ingest")

CHUNK_SIZE = int(os.getenv("KNOWLEDGE_CHUNK_CHARS", "2000"))
CHUNK_OVERLAP = int(os.getenv("KNOWLEDGE_CHUNK_OVERLAP", "200"))
EMBED_BATCH = int(os.getenv("KNOWLEDGE_EMBED_BATCH", "32"))
RERANK_ENABLED = os.getenv("KNOWLEDGE_RERANK_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
RERANK_USE_LLM = os.getenv("KNOWLEDGE_RERANK_USE_LLM", "1").strip().lower() not in {"0", "false", "off", "no"}
RERANK_CANDIDATES = int(os.getenv("KNOWLEDGE_RERANK_CANDIDATES", "40"))
RERANK_TEXT_WINDOW = int(os.getenv("KNOWLEDGE_RERANK_TEXT_WINDOW", "1600"))


def chunk_text(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    paras = [p.strip() for p in re.split(r"\n{2,}", t) if p.strip()]
    if not paras:
        paras = [t]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= CHUNK_SIZE:
            buf = f"{buf}\n\n{p}".strip() if buf else p
        else:
            if buf:
                chunks.append(buf)
            while len(p) > CHUNK_SIZE:
                chunks.append(p[:CHUNK_SIZE])
                p = p[CHUNK_SIZE - CHUNK_OVERLAP :]
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _normalize_tokens(text: str) -> list[str]:
    out: list[str] = []
    for raw in re.findall(r"[a-zA-Z0-9_]{2,}", (text or "").lower()):
        if len(raw) >= 2:
            out.append(raw)
    return out


def _token_f1(query_tokens: set[str], text_tokens: list[str]) -> float:
    if not query_tokens or not text_tokens:
        return 0.0
    text_set = set(text_tokens)
    common = len(query_tokens.intersection(text_set))
    if common <= 0:
        return 0.0
    precision = common / max(1, len(text_set))
    recall = common / max(1, len(query_tokens))
    if precision + recall <= 0:
        return 0.0
    return (2.0 * precision * recall) / (precision + recall)


def _rerank_score(*, query: str, query_tokens: set[str], vec_score: float, chunk_text: str) -> float:
    """Second-stage reranking over retrieved vector candidates."""
    text = (chunk_text or "").strip()
    if not text:
        return vec_score
    window = text[:RERANK_TEXT_WINDOW]
    window_l = window.lower()
    lex_recall = 0.0
    if query_tokens:
        lex_recall = sum(1 for t in query_tokens if t in window_l) / max(1, len(query_tokens))
    token_f1 = _token_f1(query_tokens, _normalize_tokens(window_l))
    phrase_boost = 0.1 if query.strip().lower() in window_l else 0.0
    return (0.65 * vec_score) + (0.2 * lex_recall) + (0.15 * token_f1) + phrase_boost


def reindex_document_vectors(db: Session, document_id: UUID) -> int:
    """Повторно залить векторы чанков в Qdrant (после enable)."""
    row = db.get(KnowledgeDocument, document_id)
    if row is None or not row.enabled:
        return 0
    chunks = list(
        db.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document_id)
            .order_by(KnowledgeChunk.chunk_idx)
        )
        .scalars()
        .all()
    )
    if not chunks:
        return 0
    delete_document_points(str(document_id))
    qdrant_rows: list[tuple[str, list[float], dict]] = []
    texts = [c.chunk_text for c in chunks]
    for batch_start in range(0, len(texts), EMBED_BATCH):
        batch_chunks = chunks[batch_start : batch_start + EMBED_BATCH]
        batch_texts = texts[batch_start : batch_start + EMBED_BATCH]
        embeds, model, _provider = embed_many(batch_texts)
        if model and not row.embedding_model:
            row.embedding_model = model
        for i, ch in enumerate(batch_chunks):
            emb = embeds[i] if embeds and i < len(embeds) else None
            if emb is None:
                continue
            qdrant_rows.append(
                (
                    ch.qdrant_point_id,
                    emb,
                    {
                        "chunk_id": ch.qdrant_point_id,
                        "document_id": str(row.id),
                        "knowledge_base_id": str(row.knowledge_base_id),
                        "scope": row.scope,
                        "project_id": str(row.project_id) if row.project_id else "",
                        "chunk_idx": ch.chunk_idx,
                        "filename": row.filename,
                    },
                )
            )
    if qdrant_rows:
        upsert_rows(qdrant_rows)
    db.commit()
    return len(qdrant_rows)


def process_document(db: Session, document_id: UUID) -> None:
    row = db.get(KnowledgeDocument, document_id)
    if row is None:
        return
    if not row.enabled:
        logger.info("skip ingest disabled doc=%s", document_id)
        return
    row.status = DOC_PROCESSING
    row.error_message = None
    db.commit()
    try:
        data = s3_store.get_bytes(row.s3_key)
        text, rec_model = extract_document(data=data, mime=row.mime_type, filename=row.filename)
        if not text.strip():
            raise ValueError("empty_extracted_text")
        row.recognition_model = rec_model
        row.extracted_chars = len(text)

        delete_document_points(str(row.id))
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == row.id))

        parts = chunk_text(text)
        qdrant_rows: list[tuple[str, list[float], dict]] = []
        stored = 0
        model_name = ""
        provider_name = ""

        for batch_start in range(0, len(parts), EMBED_BATCH):
            batch = parts[batch_start : batch_start + EMBED_BATCH]
            embeds, model, provider = embed_many(batch)
            if model:
                model_name = model
            if provider:
                provider_name = provider

            for i, ch in enumerate(batch):
                idx = batch_start + i
                cid = uuid4()
                point_id = str(cid)
                emb = embeds[i] if embeds and i < len(embeds) else None
                db.add(
                    KnowledgeChunk(
                        id=cid,
                        document_id=row.id,
                        knowledge_base_id=row.knowledge_base_id,
                        scope=row.scope,
                        project_id=row.project_id,
                        chunk_idx=idx,
                        chunk_text=ch,
                        char_count=len(ch),
                        qdrant_point_id=point_id,
                    )
                )
                stored += 1
                if emb is not None:
                    qdrant_rows.append(
                        (
                            point_id,
                            emb,
                            {
                                "chunk_id": point_id,
                                "document_id": str(row.id),
                                "knowledge_base_id": str(row.knowledge_base_id),
                                "scope": row.scope,
                                "project_id": str(row.project_id) if row.project_id else "",
                                "chunk_idx": idx,
                                "filename": row.filename,
                            },
                        )
                    )

        db.flush()
        upsert_rows(qdrant_rows)
        row.chunk_count = stored
        row.embedding_model = model_name or None
        row.status = DOC_READY
        row.error_message = None
        db.commit()
        logger.info("knowledge doc ready id=%s chunks=%s", row.id, stored)
    except Exception as exc:
        logger.exception("knowledge ingest failed doc=%s", document_id)
        row.status = DOC_FAILED
        row.error_message = str(exc)[:4000]
        db.commit()


def search_knowledge(
    db: Session,
    *,
    query: str,
    project_id: UUID | None,
    kb_ids: list[UUID] | None,
    include_platform: bool,
    top_k: int,
) -> list[dict]:
    from services.knowledge_service.qdrant_store import search_vectors

    q = query.strip()
    if not q:
        return []
    q_embed_list, _, _ = embed_many([q])
    if not q_embed_list:
        return []
    kb_id_strs = [str(x) for x in kb_ids] if kb_ids else []
    candidate_limit = max(top_k * 4, RERANK_CANDIDATES if RERANK_ENABLED else top_k * 4)
    hits = search_vectors(
        query_vector=q_embed_list[0],
        kb_ids=kb_id_strs,
        scope=None,
        project_id=str(project_id) if project_id else None,
        include_platform=include_platform,
        limit=min(120, candidate_limit),
    )
    if not hits:
        return []

    chunk_ids: list[UUID] = []
    for _, cid, _ in hits:
        try:
            chunk_ids.append(UUID(cid))
        except ValueError:
            continue
    chunk_rows = list(
        db.execute(
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeChunk.id.in_(chunk_ids),
                KnowledgeDocument.enabled.is_(True),
                KnowledgeDocument.status == DOC_READY,
            )
        ).all()
    )
    by_id = {str(ch.id): ch for ch, _doc in chunk_rows}
    active_kb_ids: set[UUID] | None = None
    if kb_ids:
        active_kb_ids = set(
            db.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.id.in_(kb_ids),
                    KnowledgeBase.status == "active",
                )
            )
            .scalars()
            .all()
        )
    q_tokens = tokenize(q)
    llm_scores: dict[str, float] = {}
    rerank_model: str | None = None
    if RERANK_ENABLED and RERANK_USE_LLM:
        rerank_docs: list[tuple[str, str]] = []
        for _vec_score, cid, _payload in hits:
            ch = by_id.get(cid)
            if ch is None:
                continue
            rerank_docs.append((cid, ch.chunk_text or ""))
        llm_scores, rerank_model = rerank_llm(q, rerank_docs)
        if rerank_model:
            logger.info("knowledge rerank model=%s candidates=%s", rerank_model, len(rerank_docs))
    scored: list[tuple[float, KnowledgeChunk, dict]] = []
    for vec_score, cid, payload in hits:
        ch = by_id.get(cid)
        if ch is None:
            continue
        if active_kb_ids is not None and ch.knowledge_base_id not in active_kb_ids:
            continue
        if RERANK_ENABLED and RERANK_USE_LLM and llm_scores:
            score = (0.45 * vec_score) + (0.55 * llm_scores.get(cid, 0.0))
        elif RERANK_ENABLED:
            score = _rerank_score(query=q, query_tokens=q_tokens, vec_score=vec_score, chunk_text=ch.chunk_text or "")
        else:
            text_l = (ch.chunk_text or "").lower()
            lex = 0.0
            if q_tokens:
                lex = sum(1 for t in q_tokens if t in text_l) / max(1, len(q_tokens))
            score = (0.75 * vec_score) + (0.25 * lex)
        scored.append((score, ch, payload))
    scored.sort(key=lambda x: x[0], reverse=True)

    items: list[dict] = []
    for score, ch, payload in scored[:top_k]:
        excerpt = (ch.chunk_text or "").strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200] + "…"
        items.append(
            {
                "score": round(score, 4),
                "knowledge_base_id": str(ch.knowledge_base_id),
                "document_id": str(ch.document_id),
                "chunk_idx": ch.chunk_idx,
                "filename": payload.get("filename") or "",
                "scope": ch.scope,
                "excerpt": excerpt,
            }
        )
    return items
