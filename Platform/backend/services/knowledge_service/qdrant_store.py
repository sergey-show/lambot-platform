"""Qdrant: knowledge-base chunk vectors; full text in Postgres."""

from __future__ import annotations

import logging
import os
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger("knowledge_qdrant")

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = (os.getenv("QDRANT_API_KEY") or "").strip() or None
COLLECTION = (os.getenv("QDRANT_KNOWLEDGE_COLLECTION") or "lambot_knowledge_chunks").strip()
VECTOR_SIZE = int(os.getenv("KNOWLEDGE_VECTOR_SIZE", os.getenv("CODE_INDEX_VECTOR_SIZE", "1536")))
UPSERT_BATCH = int(os.getenv("KNOWLEDGE_QDRANT_UPSERT_BATCH", "64"))

_client: QdrantClient | None = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"url": QDRANT_URL}
        if QDRANT_API_KEY:
            kwargs["api_key"] = QDRANT_API_KEY
        _client = QdrantClient(**kwargs)
    return _client


def _collection_vector_size(client: QdrantClient) -> int | None:
    names = {c.name for c in client.get_collections().collections}
    if COLLECTION not in names:
        return None
    info = client.get_collection(COLLECTION)
    vectors = info.config.params.vectors
    if hasattr(vectors, "size"):
        return int(vectors.size)
    if isinstance(vectors, dict):
        raw = vectors.get("size")
        return int(raw) if raw else None
    return None


def ensure_collection(*, vector_size: int | None = None) -> None:
    """Create a collection or verify vector dimensionality."""
    size = vector_size or VECTOR_SIZE
    if size <= 0:
        raise ValueError("vector_size must be positive")
    client = get_client()
    existing = _collection_vector_size(client)
    if existing is None:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=size, distance=Distance.COSINE),
        )
        logger.info("created qdrant collection %s size=%s", COLLECTION, size)
        return
    if existing == size:
        return
    points_count = int(getattr(client.get_collection(COLLECTION), "points_count", 0) or 0)
    if points_count == 0:
        client.delete_collection(COLLECTION)
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=size, distance=Distance.COSINE),
        )
        logger.warning(
            "recreated empty qdrant collection %s vector size %s -> %s",
            COLLECTION,
            existing,
            size,
        )
        return
    raise ValueError(
        f"Qdrant collection {COLLECTION} expects vector dim {existing}, got {size}. "
        f"Set KNOWLEDGE_VECTOR_SIZE={size} and delete the collection to re-index, "
        f"or switch embedding model to dim={existing}."
    )


def delete_document_points(document_id: str) -> None:
    ensure_collection()
    get_client().delete(
        collection_name=COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        ),
    )


def upsert_rows(rows: list[tuple[str, list[float], dict[str, Any]]]) -> int:
    if not rows:
        return 0
    dim = len(rows[0][1])
    if dim <= 0:
        raise ValueError("embedding vector is empty")
    for _pid, vec, _payload in rows[1:]:
        if len(vec) != dim:
            raise ValueError(f"inconsistent embedding dims in batch: expected {dim}, got {len(vec)}")
    ensure_collection(vector_size=dim)
    client = get_client()
    written = 0
    for i in range(0, len(rows), UPSERT_BATCH):
        batch = rows[i : i + UPSERT_BATCH]
        points = [PointStruct(id=pid, vector=vec, payload=payload) for pid, vec, payload in batch]
        client.upsert(collection_name=COLLECTION, points=points)
        written += len(points)
    return written


def search_vectors(
    *,
    query_vector: list[float],
    kb_ids: list[str],
    scope: str | None,
    project_id: str | None,
    include_platform: bool,
    limit: int,
) -> list[tuple[float, str, dict[str, Any]]]:
    ensure_collection(vector_size=len(query_vector))
    must: list[Any] = []
    if kb_ids:
        must.append(FieldCondition(key="knowledge_base_id", match=MatchAny(any=kb_ids)))
    if project_id and include_platform:
        must.append(
            Filter(
                should=[
                    FieldCondition(key="scope", match=MatchValue(value="platform")),
                    Filter(
                        must=[
                            FieldCondition(key="scope", match=MatchValue(value="project")),
                            FieldCondition(key="project_id", match=MatchValue(value=project_id)),
                        ]
                    ),
                ]
            )
        )
    elif project_id:
        must.append(FieldCondition(key="scope", match=MatchValue(value="project")))
        must.append(FieldCondition(key="project_id", match=MatchValue(value=project_id)))
    elif scope:
        must.append(FieldCondition(key="scope", match=MatchValue(value=scope)))
    elif include_platform:
        must.append(FieldCondition(key="scope", match=MatchValue(value="platform")))
    q_filter = Filter(must=must) if must else None
    hits = get_client().search(
        collection_name=COLLECTION,
        query_vector=query_vector,
        query_filter=q_filter,
        limit=limit,
        with_payload=True,
    )
    out: list[tuple[float, str, dict[str, Any]]] = []
    for h in hits:
        payload = dict(h.payload or {})
        chunk_id = str(payload.get("chunk_id") or h.id)
        out.append((float(h.score or 0.0), chunk_id, payload))
    return out


def ping() -> bool:
    try:
        get_client().get_collections()
        return True
    except Exception as exc:
        logger.warning("qdrant ping failed: %s", exc)
        return False
