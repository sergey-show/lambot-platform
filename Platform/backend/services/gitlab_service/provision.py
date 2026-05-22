"""Провижининг GitLab-группы и токена при создании проекта."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.gitlab_service.gitlab_client import (
    GitlabClientError,
    create_group_access_token,
    create_subgroup,
    resolve_root_group_id,
)
from services.gitlab_service.models import GitlabProvision
from shared.gitlab_constants import GITLAB_PLATFORM_CATEGORY, GITLAB_PLATFORM_RESOURCE_LABEL
from shared.gitlab_util import project_group_path

logger = logging.getLogger("gitlab_provision")

ADAPTER_SERVICE_URL = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
GITLAB_URL = (os.getenv("GITLAB_URL") or "").strip().rstrip("/")
PROVISION_ENABLED = os.getenv("GITLAB_PROVISION_ENABLED", "1").strip().lower() in ("1", "true", "yes")
PROVISION_MOCK = os.getenv("GITLAB_PROVISION_MOCK", "0").strip().lower() in ("1", "true", "yes")
ROOT_GROUP_PATH = (os.getenv("GITLAB_ROOT_GROUP_PATH") or "lambot").strip().strip("/")


def _upsert_row(db: Session, project_id: UUID, tenant_id: UUID | None, **fields) -> GitlabProvision:
    row = db.execute(select(GitlabProvision).where(GitlabProvision.project_id == project_id)).scalar_one_or_none()
    if row is None:
        row = GitlabProvision(project_id=project_id, tenant_id=tenant_id)
        db.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def _store_adapter_credentials(
    *,
    project_id: UUID,
    gitlab_url: str,
    group_path: str,
    group_id: int,
    token: str,
    full_group_path: str,
) -> None:
    secret_plain = json.dumps({"token": token}, ensure_ascii=False)
    payload = {
        "gitlab_url": gitlab_url,
        "group_path": group_path,
        "full_group_path": full_group_path,
        "group_id": group_id,
        "provisioning": "platform",
        "enabled": True,
        "repos_managed_by_agents": True,
        "gitlab_group_created": True,
    }
    r = httpx.put(
        f"{ADAPTER_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/gitlab-platform-binding",
        headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
        json={
            "value_json": payload,
            "secret_plain": secret_plain,
        },
        timeout=30.0,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"adapter upsert failed: {r.status_code} {r.text[:500]}")


def provision_project_gitlab(
    db: Session,
    *,
    project_id: UUID,
    tenant_id: UUID | None,
    project_name: str,
) -> dict:
    if not PROVISION_ENABLED:
        _upsert_row(db, project_id, tenant_id, status="disabled", error_message="GITLAB_PROVISION_ENABLED=0")
        db.commit()
        return {"ok": False, "status": "disabled"}

    rel_path = project_group_path(project_id, project_name)
    full_group_path = f"{ROOT_GROUP_PATH}/{rel_path}"

    row = _upsert_row(
        db,
        project_id,
        tenant_id,
        status="in_progress",
        group_path=rel_path,
        full_group_path=full_group_path,
        error_message=None,
    )
    db.commit()

    try:
        if PROVISION_MOCK or not GITLAB_URL:
            token = f"mock-{project_id.hex[:24]}"
            group_id = 0
            _store_adapter_credentials(
                project_id=project_id,
                gitlab_url=GITLAB_URL or "https://gitlab.example.com",
                group_path=full_group_path,
                group_id=group_id,
                token=token,
                full_group_path=full_group_path,
            )
            _upsert_row(
                db,
                project_id,
                tenant_id,
                status="ready",
                group_id=group_id,
                meta_json={"mock": True},
            )
            db.commit()
            return {
                "ok": True,
                "status": "ready",
                "gitlab_group_created": True,
                "mock": True,
                "group_path": full_group_path,
            }

        parent_id = resolve_root_group_id()
        group = create_subgroup(parent_id=parent_id, name=project_name[:255] or rel_path, path=rel_path)
        group_id = int(group["id"])
        full_path = str(group.get("full_path") or full_group_path)
        token, _meta = create_group_access_token(group_id=group_id)
        _store_adapter_credentials(
            project_id=project_id,
            gitlab_url=GITLAB_URL,
            group_path=full_path,
            group_id=group_id,
            token=token,
            full_group_path=full_path,
        )
        _upsert_row(
            db,
            project_id,
            tenant_id,
            status="ready",
            group_id=group_id,
            full_group_path=full_path,
            group_path=rel_path,
            meta_json={"token_name": "lambot-platform-agent"},
        )
        db.commit()
        logger.info("gitlab provision ok project_id=%s group=%s", project_id, full_path)
        return {
            "ok": True,
            "status": "ready",
            "gitlab_group_created": True,
            "group_path": full_path,
            "group_id": group_id,
        }
    except (GitlabClientError, RuntimeError, httpx.HTTPError) as exc:
        logger.exception("gitlab provision failed project_id=%s: %s", project_id, exc)
        _upsert_row(db, project_id, tenant_id, status="failed", error_message=str(exc)[:4000])
        db.commit()
        return {"ok": False, "status": "failed", "error": str(exc)}
