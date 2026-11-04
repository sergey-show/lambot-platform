"""GitLab name/path utilities for platform projects."""

from __future__ import annotations

import re
from uuid import UUID


def slugify_name(name: str, *, max_len: int = 48) -> str:
    s = (name or "project").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "project"
    return s[:max_len].strip("-") or "project"


def project_group_path(project_id: UUID, project_name: str | None = None) -> str:
    """Relative group path under the platform root group (without parent path)."""
    short = str(project_id).replace("-", "")[:12]
    slug = slugify_name(project_name or "")
    return f"p-{short}-{slug}"[:120]


def repo_slug_safe(slug: str) -> str:
    s = slugify_name(slug, max_len=63)
    if not re.match(r"^[a-z0-9]", s):
        s = f"r-{s}"
    return s
