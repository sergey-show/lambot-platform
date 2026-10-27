"""
repo-workspace-service: локальные git-клоны репозиториев проекта для агентов.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.repo_workspace_service.credentials import fetch_gitlab_credentials
from services.repo_workspace_service.db import get_db, init_db
from services.repo_workspace_service.git_ops import (
    GitOpsError,
    clone_url,
    commit_all,
    create_branch,
    ensure_clone,
    list_tree,
    push_branch,
    read_file,
    workspace_dir,
    write_files,
    head_sha,
)
from services.repo_workspace_service.models import RepoWorkspace
from shared.gitlab_util import repo_slug_safe

logger = logging.getLogger("repo_workspace_service")
logging.basicConfig(level=logging.INFO)

INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")


GITLAB_SERVICE_URL = os.getenv("GITLAB_SERVICE_URL", "http://gitlab-service:8016")
CODE_INDEX_SERVICE_URL = os.getenv("CODE_INDEX_SERVICE_URL", "http://code-index-service:8017")

app = FastAPI(title="repo-workspace-service", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    from services.repo_workspace_service.git_ops import WORKSPACE_ROOT

    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def _require_internal(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    if (x_internal_token or "").strip() != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def _schedule_code_index(project_id: UUID, repo_slug: str, branch: str) -> None:
    try:
        httpx.post(
            f"{CODE_INDEX_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/code-index/reindex",
            headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
            json={"repo_slug": repo_slug, "branch": branch},
            timeout=10.0,
        )
    except Exception as exc:
        logger.warning("code-index schedule failed: %s", exc)


def _get_or_create_workspace_row(
    db: Session,
    *,
    project_id: UUID,
    repo_slug: str,
    path_with_namespace: str,
    gitlab_project_id: int | None,
) -> RepoWorkspace:
    row = db.execute(
        select(RepoWorkspace).where(
            RepoWorkspace.project_id == project_id,
            RepoWorkspace.repo_slug == repo_slug,
        )
    ).scalar_one_or_none()
    disk = str(workspace_dir(str(project_id), repo_slug))
    if row is None:
        row = RepoWorkspace(
            project_id=project_id,
            repo_slug=repo_slug,
            path_with_namespace=path_with_namespace,
            gitlab_project_id=gitlab_project_id,
            disk_path=disk,
            status="registered",
        )
        db.add(row)
    else:
        row.path_with_namespace = path_with_namespace
        row.gitlab_project_id = gitlab_project_id
        row.disk_path = disk
        row.updated_at = datetime.utcnow()
    db.flush()
    return row


class CreateRepoBody(BaseModel):
    repo_slug: str = Field(..., min_length=1, max_length=120)
    name: str | None = None
    description: str = ""


@app.post("/api/v1/projects/{project_id}/repos")
def register_and_create_gitlab_repo(
    project_id: UUID,
    body: CreateRepoBody,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict[str, Any]:
    slug = repo_slug_safe(body.repo_slug)
    r = httpx.post(
        f"{GITLAB_SERVICE_URL.rstrip('/')}/api/v1/projects/{project_id}/gitlab/repos",
        headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
        json={"repo_slug": slug, "name": body.name, "description": body.description},
        timeout=120.0,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=r.text[:800])
    gl = r.json()
    pwn = str(gl.get("path_with_namespace") or "")
    row = _get_or_create_workspace_row(
        db,
        project_id=project_id,
        repo_slug=slug,
        path_with_namespace=pwn,
        gitlab_project_id=gl.get("gitlab_project_id"),
    )
    db.commit()
    return {"repo_slug": slug, "path_with_namespace": pwn, "workspace_id": str(row.id), "gitlab": gl}


class SyncBody(BaseModel):
    branch: str = "main"
    create_branch: str | None = None
    create_from_ref: str | None = None


@app.post("/api/v1/projects/{project_id}/repos/{repo_slug}/sync")
def sync_repo(
    project_id: UUID,
    repo_slug: str,
    body: SyncBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict[str, Any]:
    slug = repo_slug_safe(repo_slug)
    row = db.execute(
        select(RepoWorkspace).where(RepoWorkspace.project_id == project_id, RepoWorkspace.repo_slug == slug)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="repo not registered; create repo first")
    creds = fetch_gitlab_credentials(project_id)
    branch = (body.create_branch or body.branch or row.active_branch or "main").strip()
    dest = workspace_dir(str(project_id), slug)
    remote = clone_url(creds["gitlab_url"], row.path_with_namespace, creds["token"])
    try:
        base = (body.branch or "main").strip()
        if body.create_branch:
            if not (dest / ".git").is_dir():
                ensure_clone(dest=dest, remote_url=remote, branch=base)
            create_branch(dest=dest, branch=branch, from_ref=body.create_from_ref or f"origin/{base}")
            sha = head_sha(dest=dest)
        else:
            sha = ensure_clone(dest=dest, remote_url=remote, branch=branch)
        row.active_branch = branch
        row.last_commit_sha = sha
        row.status = "synced"
        row.last_error = None
        row.updated_at = datetime.utcnow()
        db.commit()
    except GitOpsError as exc:
        row.status = "error"
        row.last_error = str(exc)[:4000]
        row.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    background_tasks.add_task(_schedule_code_index, project_id, slug, branch)
    return {
        "repo_slug": slug,
        "branch": branch,
        "commit_sha": sha,
        "disk_path": str(dest),
        "status": row.status,
    }


@app.get("/api/v1/projects/{project_id}/repos")
def list_repos(project_id: UUID, db: Session = Depends(get_db), _: None = Depends(_require_internal)) -> dict:
    rows = list(
        db.execute(select(RepoWorkspace).where(RepoWorkspace.project_id == project_id).order_by(RepoWorkspace.updated_at.desc()))
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "repo_slug": r.repo_slug,
                "path_with_namespace": r.path_with_namespace,
                "active_branch": r.active_branch,
                "last_commit_sha": r.last_commit_sha,
                "status": r.status,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    }


@app.get("/api/v1/projects/{project_id}/repos/{repo_slug}/tree")
def repo_tree(
    project_id: UUID,
    repo_slug: str,
    path: str = "",
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict:
    slug = repo_slug_safe(repo_slug)
    row = db.execute(
        select(RepoWorkspace).where(RepoWorkspace.project_id == project_id, RepoWorkspace.repo_slug == slug)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="repo not found")
    dest = workspace_dir(str(project_id), slug)
    if not (dest / ".git").is_dir():
        raise HTTPException(status_code=409, detail="repo not cloned; call sync first")
    try:
        entries = list_tree(dest=dest, subpath=path)
    except GitOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo_slug": slug, "branch": row.active_branch, "entries": entries}


@app.get("/api/v1/projects/{project_id}/repos/{repo_slug}/file")
def repo_file(
    project_id: UUID,
    repo_slug: str,
    path: str,
    _: None = Depends(_require_internal),
) -> dict:
    slug = repo_slug_safe(repo_slug)
    dest = workspace_dir(str(project_id), slug)
    try:
        content = read_file(dest=dest, rel_path=path)
    except GitOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo_slug": slug, "path": path, "content": content}


class WriteFilesBody(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)


@app.put("/api/v1/projects/{project_id}/repos/{repo_slug}/files")
def repo_write_files(
    project_id: UUID,
    repo_slug: str,
    body: WriteFilesBody,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict:
    slug = repo_slug_safe(repo_slug)
    dest = workspace_dir(str(project_id), slug)
    try:
        written = write_files(dest=dest, files=body.files)
    except GitOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo_slug": slug, "written": written}


class CommitBody(BaseModel):
    message: str = "chore: agent changes"


@app.post("/api/v1/projects/{project_id}/repos/{repo_slug}/commit")
def repo_commit(
    project_id: UUID,
    repo_slug: str,
    body: CommitBody,
    _: None = Depends(_require_internal),
) -> dict:
    slug = repo_slug_safe(repo_slug)
    dest = workspace_dir(str(project_id), slug)
    try:
        sha = commit_all(dest=dest, message=body.message)
    except GitOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo_slug": slug, "commit_sha": sha}


class PushBody(BaseModel):
    branch: str | None = None


@app.post("/api/v1/projects/{project_id}/repos/{repo_slug}/push")
def repo_push(
    project_id: UUID,
    repo_slug: str,
    body: PushBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal),
) -> dict:
    slug = repo_slug_safe(repo_slug)
    row = db.execute(
        select(RepoWorkspace).where(RepoWorkspace.project_id == project_id, RepoWorkspace.repo_slug == slug)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="repo not found")
    branch = (body.branch or row.active_branch or "main").strip()
    dest = workspace_dir(str(project_id), slug)
    try:
        push_branch(dest=dest, branch=branch)
        sha = head_sha(dest=dest)
    except GitOpsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    row.last_commit_sha = sha
    row.updated_at = datetime.utcnow()
    db.commit()
    background_tasks.add_task(_schedule_code_index, project_id, slug, branch)
    return {"repo_slug": slug, "branch": branch, "commit_sha": sha, "pushed": True}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "repo-workspace-service"}
