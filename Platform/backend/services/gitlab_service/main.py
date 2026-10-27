"""
gitlab-service: при project.created создаёт приватную GitLab-группу и group access token;
секреты сохраняются в adapter (ProjectInfraResource).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from uuid import UUID

from aiokafka import AIOKafkaConsumer
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.gitlab_service.db import get_db, init_db
from services.gitlab_service.gitlab_client import create_project_in_group
from services.gitlab_service.models import GitlabProvision
from services.gitlab_service.provision import provision_project_gitlab
from shared.contracts.events import EventTypes
from shared.gitlab_util import repo_slug_safe
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_GROUP_GITLAB_SERVICE, KAFKA_TOPIC_EVENTS

logger = logging.getLogger("gitlab_service")
logging.basicConfig(level=logging.INFO)

INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
PROVISION_MOCK = os.getenv("GITLAB_PROVISION_MOCK", "0").strip().lower() in ("1", "true", "yes")
_processed: set[str] = set()

consumer: AIOKafkaConsumer | None = None
consumer_task: asyncio.Task | None = None


def _require_internal(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    if (x_internal_token or "").strip() != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def _handle_project_created(event: dict) -> None:
    if event.get("event_type") != EventTypes.PROJECT_CREATED:
        return
    project_id = event.get("project_id")
    if not project_id:
        return
    pid = str(project_id)
    if pid in _processed:
        return
    payload = event.get("payload") or {}
    name = str(payload.get("name") or "project")
    tenant_id = event.get("tenant_id")
    tid = UUID(str(tenant_id)) if tenant_id else None
    from services.gitlab_service.db import SessionLocal

    with SessionLocal() as db:
        existing = db.execute(
            select(GitlabProvision).where(GitlabProvision.project_id == UUID(pid))
        ).scalar_one_or_none()
        if existing and existing.status in ("ready", "in_progress"):
            _processed.add(pid)
            return
        out = provision_project_gitlab(db, project_id=UUID(pid), tenant_id=tid, project_name=name)
        if out.get("ok") or out.get("status") in ("ready", "disabled", "failed"):
            _processed.add(pid)


async def _consume_loop() -> None:
    if not consumer:
        return
    async for msg in consumer:
        try:
            event = json.loads(msg.value.decode("utf-8"))
        except Exception:
            continue
        await asyncio.to_thread(_handle_project_created, event)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global consumer, consumer_task
    init_db()
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC_EVENTS,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_GROUP_GITLAB_SERVICE,
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


app = FastAPI(title="gitlab-service", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "gitlab-service"}


def _provision_public(row: GitlabProvision | None, project_id: UUID) -> dict:
    if row is None:
        return {
            "project_id": str(project_id),
            "status": "not_started",
            "gitlab_group_created": False,
        }
    created = row.status == "ready"
    return {
        "project_id": str(project_id),
        "status": row.status,
        "gitlab_group_created": created,
        "group_path": row.full_group_path or row.group_path,
        "group_id": row.group_id,
        "error_message": row.error_message,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.get("/api/v1/projects/{project_id}/gitlab-provision")
def get_provision_status(project_id: UUID, db: Session = Depends(get_db)) -> dict:
    row = db.execute(select(GitlabProvision).where(GitlabProvision.project_id == project_id)).scalar_one_or_none()
    return _provision_public(row, project_id)


@app.get("/api/v1/internal/gitlab-provision/index")
def internal_provision_index(
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict:
    """Сводка для Superadmin: project_id → статус GitLab-группы."""
    rows = list(db.execute(select(GitlabProvision)).scalars().all())
    items = [_provision_public(r, UUID(str(r.project_id))) for r in rows]
    by_id = {str(x["project_id"]): x for x in items}
    return {"items": items, "by_project_id": by_id}


class ReprovisionBody(BaseModel):
    project_name: str = "project"
    force: bool = False


def _run_provision(
    db: Session,
    *,
    project_id: UUID,
    tenant_id: UUID | None,
    project_name: str,
    force: bool = False,
) -> dict:
    row = db.execute(select(GitlabProvision).where(GitlabProvision.project_id == project_id)).scalar_one_or_none()
    if row and row.status == "ready" and not force:
        return {
            "ok": True,
            "status": "ready",
            "gitlab_group_created": True,
            "group_path": row.full_group_path or row.group_path,
            "skipped": True,
            "message": "GitLab group already provisioned",
        }
    if row and row.status == "in_progress" and not force:
        return {
            "ok": False,
            "status": "in_progress",
            "gitlab_group_created": False,
            "message": "Provisioning already in progress",
        }
    return provision_project_gitlab(
        db,
        project_id=project_id,
        tenant_id=tenant_id,
        project_name=project_name,
    )


@app.post("/api/v1/internal/projects/{project_id}/gitlab-provision/retry")
def retry_provision(
    project_id: UUID,
    body: ReprovisionBody,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict:
    return _run_provision(
        db,
        project_id=project_id,
        tenant_id=None,
        project_name=body.project_name,
        force=body.force,
    )


class ProvisionPublicBody(BaseModel):
    project_name: str | None = None
    force: bool = False


@app.post("/api/v1/projects/{project_id}/gitlab-provision")
def provision_public(
    project_id: UUID,
    body: ProvisionPublicBody | None = None,
    db: Session = Depends(get_db),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict:
    """Ручной запуск из UI (gateway проксирует с internal token)."""
    if (x_internal_token or "").strip() != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    payload = body or ProvisionPublicBody()
    return _run_provision(
        db,
        project_id=project_id,
        tenant_id=None,
        project_name=(payload.project_name or "project").strip() or "project",
        force=payload.force,
    )


class CreateRepoBody(BaseModel):
    repo_slug: str
    name: str | None = None
    description: str = ""


@app.post("/api/v1/projects/{project_id}/gitlab/repos")
def create_gitlab_repo(
    project_id: UUID,
    body: CreateRepoBody,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict:
    """Создать пустой репозиторий в группе проекта (агенты вызывают при необходимости)."""
    row = db.execute(select(GitlabProvision).where(GitlabProvision.project_id == project_id)).scalar_one_or_none()
    if row is None or row.status != "ready":
        raise HTTPException(status_code=409, detail="GitLab group not ready for project")
    slug = repo_slug_safe(body.repo_slug)
    if PROVISION_MOCK or not row.group_id:
        full = f"{row.full_group_path or row.group_path}/{slug}".strip("/")
        return {
            "repo_slug": slug,
            "gitlab_project_id": 0,
            "path_with_namespace": full,
            "web_url": f"https://gitlab.example.com/{full}",
            "mock": True,
        }
    proj = create_project_in_group(
        group_id=int(row.group_id),
        name=(body.name or slug)[:255],
        path=slug,
        description=body.description,
        initialize_with_readme=False,
    )
    return {
        "repo_slug": slug,
        "gitlab_project_id": proj.get("id"),
        "path_with_namespace": proj.get("path_with_namespace"),
        "web_url": proj.get("web_url"),
        "ssh_url_to_repo": proj.get("ssh_url_to_repo"),
        "http_url_to_repo": proj.get("http_url_to_repo"),
    }
