"""Qdrant: векторы чанков кода; полный текст — в Postgres."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger("code_index_store")

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = (os.getenv("QDRANT_API_KEY") or "").strip() or None
COLLECTION = (os.getenv("QDRANT_CODE_COLLECTION") or "lambot_code_chunks").strip()
VECTOR_SIZE = int(os.getenv("CODE_INDEX_VECTOR_SIZE", "1536"))
UPSERT_BATCH = int(os.getenv("CODE_INDEX_QDRANT_UPSERT_BATCH", "128"))

_client: QdrantClient | None = None
_collection_ready = False


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"url": QDRANT_URL}
        if QDRANT_API_KEY:
            kwargs["api_key"] = QDRANT_API_KEY
        _client = QdrantClient(**kwargs)
    return _client


def ensure_collection(*, vector_size: int | None = None) -> None:
    global _collection_ready
    if _collection_ready:
        return
    size = vector_size or VECTOR_SIZE
    client = get_client()
    names = {c.name for c in client.get_collections().collections}
    if COLLECTION not in names:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=size, distance=Distance.COSINE),
        )
        logger.info("created qdrant collection %s size=%s", COLLECTION, size)
    _collection_ready = True


def delete_scope(*, project_id: str, repo_slug: str, branch: str) -> None:
    ensure_collection()
    client = get_client()
    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(
            must=[
                FieldCondition(key="project_id", match=MatchValue(value=project_id)),
                FieldCondition(key="repo_slug", match=MatchValue(value=repo_slug)),
                FieldCondition(key="branch", match=MatchValue(value=branch)),
            ]
        ),
    )


def upsert_chunks(
    *,
    project_id: str,
    repo_slug: str,
    branch: str,
    commit_sha: str,
    rows: list[tuple[str, list[float], dict[str, Any]]],
) -> int:
    """
    rows: (point_id uuid str, vector, payload without vector)
    payload must include chunk_id for postgres join.
    """
    if not rows:
        return 0
    ensure_collection(vector_size=len(rows[0][1]))
    client = get_client()
    written = 0
    for i in range(0, len(rows), UPSERT_BATCH):
        batch = rows[i : i + UPSERT_BATCH]
        points = [
            PointStruct(
                id=pid if isinstance(pid, (str, int)) else str(pid),
                vector=vec,
                payload={
                    "project_id": project_id,
                    "repo_slug": repo_slug,
                    "branch": branch,
                    "commit_sha": commit_sha,
                    **payload,
                },
            )
            for pid, vec, payload in batch
        ]
        client.upsert(collection_name=COLLECTION, points=points)
        written += len(points)
    return written


def search_vectors(
    *,
    query_vector: list[float],
    project_id: str,
    repo_slug: str | None,
    branch: str | None,
    limit: int,
) -> list[tuple[float, str, dict[str, Any]]]:
    """Возвращает (score, chunk_id, payload)."""
    ensure_collection(vector_size=len(query_vector))
    client = get_client()
    must = [FieldCondition(key="project_id", match=MatchValue(value=project_id))]
    if repo_slug:
        must.append(FieldCondition(key="repo_slug", match=MatchValue(value=repo_slug)))
    if branch:
        must.append(FieldCondition(key="branch", match=MatchValue(value=branch)))
    hits = client.search(
        collection_name=COLLECTION,
        query_vector=query_vector,
        query_filter=Filter(must=must),
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


def new_point_id() -> str:
    return str(uuid.uuid4())
