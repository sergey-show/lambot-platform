"""Read project GitLab credentials (internal only)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.adapter_service.crypto import decrypt_secret
from services.adapter_service.models import ProjectInfraResource
from shared.gitlab_constants import GITLAB_PLATFORM_CATEGORY, GITLAB_PLATFORM_RESOURCE_LABEL

LEGACY_GITLAB_LABEL = "GitLab agent (group and token)"


def load_gitlab_credentials(db: Session, project_id: UUID) -> dict[str, Any] | None:
    for label in (GITLAB_PLATFORM_RESOURCE_LABEL, LEGACY_GITLAB_LABEL):
        row = db.execute(
            select(ProjectInfraResource)
            .where(
                ProjectInfraResource.project_id == project_id,
                ProjectInfraResource.category == GITLAB_PLATFORM_CATEGORY,
                ProjectInfraResource.label == label,
            )
            .limit(1)
        ).scalars().first()
        if row is None or not row.secret_encrypted:
            continue
        vj = dict(row.value_json or {})
        if vj.get("enabled") is False:
            continue
        try:
            plain = decrypt_secret(row.secret_encrypted)
            sec = json.loads(plain)
        except Exception:
            continue
        token = (sec.get("token") or "").strip()
        url = (vj.get("gitlab_url") or "").strip().rstrip("/")
        group_path = (vj.get("full_group_path") or vj.get("group_path") or "").strip()
        if token and url and group_path:
            return {
                "gitlab_url": url,
                "group_path": group_path,
                "group_id": vj.get("group_id"),
                "token": token,
                "label": label,
                "provisioning": vj.get("provisioning", "unknown"),
            }
    return None
