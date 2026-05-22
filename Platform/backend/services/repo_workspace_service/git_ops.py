"""Git-операции в локальном workspace (subprocess)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

logger = logging.getLogger("git_ops")

WORKSPACE_ROOT = Path(os.getenv("REPO_WORKSPACE_ROOT", "/var/lambot/repo-workspaces")).resolve()
GIT_TIMEOUT_SEC = int(os.getenv("REPO_WORKSPACE_GIT_TIMEOUT_SEC", "600"))


class GitOpsError(Exception):
    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def workspace_dir(project_id: str, repo_slug: str) -> Path:
    return WORKSPACE_ROOT / project_id / repo_slug


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    merged = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            env=merged,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitOpsError("git command timeout", stderr=str(exc)) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:8000]
        raise GitOpsError(f"git failed: {' '.join(cmd[:4])}", stderr=err)
    return (proc.stdout or "").strip()


def clone_url(gitlab_url: str, path_with_namespace: str, token: str) -> str:
    parsed = urlparse(gitlab_url)
    host = parsed.netloc or parsed.path
    if not host:
        raise GitOpsError("invalid gitlab_url")
    enc_token = quote(token, safe="")
    enc_path = quote(path_with_namespace.strip("/"), safe="/")
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    return f"{scheme}://oauth2:{enc_token}@{host}/{enc_path}.git"


def ensure_clone(
    *,
    dest: Path,
    remote_url: str,
    branch: str,
) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest / ".git").is_dir():
        _run(["git", "fetch", "--all", "--prune"], cwd=dest)
        try:
            _run(["git", "checkout", branch], cwd=dest)
            _run(["git", "pull", "--ff-only", "origin", branch], cwd=dest)
        except GitOpsError:
            _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=dest)
    else:
        if dest.exists() and not (dest / ".git").is_dir():
            raise GitOpsError("workspace path exists but is not a git repo")
        try:
            _run(
                ["git", "clone", "--branch", branch, "--single-branch", remote_url, str(dest)],
                cwd=dest.parent,
            )
        except GitOpsError:
            _run(["git", "clone", remote_url, str(dest)], cwd=dest.parent)
            _run(["git", "checkout", "-B", branch], cwd=dest)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=dest)
    return sha


def create_branch(*, dest: Path, branch: str, from_ref: str | None = None) -> None:
    _run(["git", "fetch", "origin"], cwd=dest)
    start = from_ref.strip() if from_ref else "HEAD"
    _run(["git", "checkout", "-B", branch, start], cwd=dest)


def list_tree(*, dest: Path, subpath: str = "", max_entries: int = 500) -> list[dict[str, Any]]:
    base = (dest / subpath).resolve()
    if not str(base).startswith(str(dest.resolve())):
        raise GitOpsError("path escapes workspace")
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(base.rglob("*")):
        if ".git" in p.parts:
            continue
        rel = p.relative_to(dest).as_posix()
        if p.is_dir():
            out.append({"path": rel, "type": "dir"})
        else:
            out.append({"path": rel, "type": "file", "size": p.stat().st_size})
        if len(out) >= max_entries:
            break
    return out


def read_file(*, dest: Path, rel_path: str, max_bytes: int = 512_000) -> str:
    fp = (dest / rel_path).resolve()
    if not str(fp).startswith(str(dest.resolve())):
        raise GitOpsError("path escapes workspace")
    if not fp.is_file():
        raise GitOpsError("file not found")
    data = fp.read_bytes()
    if len(data) > max_bytes:
        raise GitOpsError("file too large")
    return data.decode("utf-8", errors="replace")


def write_files(*, dest: Path, files: dict[str, str]) -> list[str]:
    written: list[str] = []
    for rel, content in files.items():
        rel_clean = rel.strip().lstrip("/")
        if ".." in Path(rel_clean).parts:
            raise GitOpsError(f"invalid path: {rel}")
        fp = (dest / rel_clean).resolve()
        if not str(fp).startswith(str(dest.resolve())):
            raise GitOpsError(f"path escapes workspace: {rel}")
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        written.append(rel_clean)
    return written


def commit_all(*, dest: Path, message: str) -> str:
    _run(["git", "add", "-A"], cwd=dest)
    status = _run(["git", "status", "--porcelain"], cwd=dest)
    if not status.strip():
        return _run(["git", "rev-parse", "HEAD"], cwd=dest)
    safe_msg = re.sub(r"\s+", " ", (message or "agent commit").strip())[:2000]
    _run(["git", "commit", "-m", safe_msg], cwd=dest)
    return _run(["git", "rev-parse", "HEAD"], cwd=dest)


def push_branch(*, dest: Path, branch: str) -> None:
    _run(["git", "push", "-u", "origin", branch], cwd=dest)


def head_sha(*, dest: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], cwd=dest)
