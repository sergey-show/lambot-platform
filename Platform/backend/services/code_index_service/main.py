"""
code-index-service: семантический поиск по клонированному коду (repo-workspace).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from aiokafka import AIOKafkaConsumer
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.code_index_service.db import get_db, init_db
from services.code_index_service.indexer import index_workspace
from services.code_index_service.models import CodeChunk
from shared.contracts.events import EventTypes
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_GROUP_CODE_INDEX, KAFKA_TOPIC_EVENTS
from shared.rag_util import cosine_similarity, embed_many, tokenize

logger = logging.getLogger("code_index_service")
logging.basicConfig(level=logging.INFO)

INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
REPO_WORKSPACE_URL = os.getenv("REPO_WORKSPACE_SERVICE_URL", "http://repo-workspace-service:8018")

consumer: AIOKafkaConsumer | None = None
consumer_task: asyncio.Task | None = None


def _require_internal(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    if (x_internal_token or "").strip() != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def _resolve_commit_sha(project_id: UUID, repo_slug: str, branch: str) -> str:
    r = httpx.get(
        f"{REPO_WORKSPACE_URL.rstrip('/')}/api/v1/projects/{project_id}/repos",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=20.0,
    )
    if r.status_code != 200:
        return "unknown"
    for item in r.json().get("items") or []:
        if item.get("repo_slug") == repo_slug:
            return str(item.get("last_commit_sha") or "unknown")
    return "unknown"


def _run_reindex(project_id: UUID, repo_slug: str, branch: str) -> dict:
    from services.code_index_service.db import SessionLocal

    commit_sha = _resolve_commit_sha(project_id, repo_slug, branch)
    with SessionLocal() as db:
        try:
            return index_workspace(
                db,
                project_id=str(project_id),
                repo_slug=repo_slug,
                branch=branch,
                commit_sha=commit_sha,
            )
        except FileNotFoundError as exc:
            return {"error": str(exc), "chunks": 0}


def _handle_code_index_event(event: dict) -> None:
    if event.get("event_type") != EventTypes.CODE_INDEX_REQUESTED:
        return
    payload = event.get("payload") or {}
    project_id = event.get("project_id")
    repo_slug = payload.get("repo_slug")
    branch = payload.get("branch") or "main"
    if not project_id or not repo_slug:
        return
    _run_reindex(UUID(str(project_id)), str(repo_slug), str(branch))


async def _consume_loop() -> None:
    if not consumer:
        return
    async for msg in consumer:
        try:
            event = json.loads(msg.value.decode("utf-8"))
        except Exception:
            continue
        await asyncio.to_thread(_handle_code_index_event, event)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global consumer, consumer_task
    init_db()
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC_EVENTS,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_GROUP_CODE_INDEX,
        auto_offset_reset="earliest",
    )
    try:
        await consumer.start()
        consumer_task = asyncio.create_task(_consume_loop())
    except Exception as exc:
        logger.exception("kafka start failed: %s", exc)
        consumer = None
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


app = FastAPI(title="code-index-service", version="1.0.0", lifespan=lifespan)


class ReindexBody(BaseModel):
    repo_slug: str
    branch: str = "main"


@app.post("/api/v1/internal/projects/{project_id}/code-index/reindex")
def internal_reindex(
    project_id: UUID,
    body: ReindexBody,
    _: None = Depends(_require_internal),
) -> dict[str, Any]:
    return _run_reindex(project_id, body.repo_slug.strip(), body.branch.strip() or "main")


class SearchBody(BaseModel):
    query: str = Field(..., min_length=1)
    repo_slug: str | None = None
    branch: str | None = None
    top_k: int = Field(default=12, ge=1, le=40)


@app.post("/api/v1/projects/{project_id}/code-index/search")
def search_code(
    project_id: UUID,
    body: SearchBody,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict[str, Any]:
    q = body.query.strip()
    if not q:
        return {"items": []}
    stmt = select(CodeChunk).where(CodeChunk.project_id == project_id)
    if body.repo_slug:
        stmt = stmt.where(CodeChunk.repo_slug == body.repo_slug.strip())
    if body.branch:
        stmt = stmt.where(CodeChunk.branch == body.branch.strip())
    chunks = list(db.execute(stmt).scalars().all())
    if not chunks:
        return {"items": []}
    q_tokens = tokenize(q)
    q_embed_list, _, _ = embed_many([q])
    q_embed = q_embed_list[0] if q_embed_list else None
    scored: list[tuple[float, CodeChunk]] = []
    for ch in chunks:
        text_l = (ch.chunk_text or "").lower()
        lex = 0.0
        if q_tokens:
            hits = sum(1 for t in q_tokens if t in text_l)
            lex = hits / max(1, len(q_tokens))
        cos = cosine_similarity(q_embed, ch.embedding if isinstance(ch.embedding, list) else None)
        score = (0.65 * cos) + (0.35 * lex)
        if score > 0.0001:
            scored.append((score, ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    items: list[dict[str, Any]] = []
    for score, ch in scored[: body.top_k]:
        excerpt = (ch.chunk_text or "").strip()
        if len(excerpt) > 700:
            excerpt = excerpt[:700] + "…"
        items.append(
            {
                "score": round(score, 6),
                "repo_slug": ch.repo_slug,
                "branch": ch.branch,
                "commit_sha": ch.commit_sha,
                "file_path": ch.file_path,
                "chunk_idx": ch.chunk_idx,
                "language": ch.language,
                "excerpt": excerpt,
            }
        )
    return {"items": items}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "code-index-service"}
