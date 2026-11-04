"""Agent tools: GitLab (platform), local clone, code index."""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from uuid import UUID

import httpx
from langchain_core.tools import tool

INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
GITLAB_URL = os.getenv("GITLAB_SERVICE_URL", "http://gitlab-service:8016")
REPO_WS_URL = os.getenv("REPO_WORKSPACE_SERVICE_URL", "http://repo-workspace-service:8018")
CODE_INDEX_URL = os.getenv("CODE_INDEX_SERVICE_URL", "http://code-index-service:8017")


def _headers() -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"}


def _make_gitlab_create_repository(pid: str):
    @tool
    def gitlab_create_repository(repo_slug: str, name: str = "", description: str = "") -> str:
        """Create an empty GitLab repository in the project group (no README). repo_slug — short Latin name. Agents create repos per task, not on project create."""
        try:
            r = httpx.post(
                f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos",
                headers=_headers(),
                json={"repo_slug": repo_slug, "name": name or repo_slug, "description": description},
                timeout=180.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return gitlab_create_repository


def _make_repo_clone_or_sync(pid: str):
    @tool
    def repo_clone_or_sync(
        repo_slug: str,
        branch: str = "main",
        feature_branch: str = "",
        create_from_ref: str = "",
    ) -> str:
        """Clone or update the local workspace. For a feature branch set feature_branch (created from branch). Indexing starts after sync."""
        payload: dict[str, Any] = {"branch": branch or "main"}
        if feature_branch.strip():
            payload["create_branch"] = feature_branch.strip()
            if create_from_ref.strip():
                payload["create_from_ref"] = create_from_ref.strip()
        try:
            r = httpx.post(
                f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos/{repo_slug}/sync",
                headers=_headers(),
                json=payload,
                timeout=900.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return repo_clone_or_sync


def _make_repo_read_file(pid: str):
    @tool
    def repo_read_file(repo_slug: str, path: str) -> str:
        """Read a file from the local workspace (after repo_clone_or_sync). path — relative path in the repository."""
        try:
            r = httpx.get(
                f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos/{repo_slug}/file",
                headers={"X-Internal-Token": INTERNAL_TOKEN},
                params={"path": path},
                timeout=60.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return repo_read_file


def _make_repo_write_commit_push(pid: str):
    @tool
    def repo_write_commit_push(
        repo_slug: str,
        files_json: str,
        commit_message: str,
        push: bool = True,
    ) -> str:
        """Write files (JSON object path→content), commit and optionally push to the active branch. files_json example: {\"src/a.py\": \"...\"}."""
        try:
            files = json.loads(files_json)
            if not isinstance(files, dict):
                return json.dumps({"error": "files_json must be a JSON object"})
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid files_json: {exc}"})
        try:
            w = httpx.put(
                f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos/{repo_slug}/files",
                headers=_headers(),
                json={"files": files},
                timeout=120.0,
            )
            if w.status_code not in (200, 201):
                return w.text if w.text else json.dumps({"http": w.status_code, "step": "write"})
            c = httpx.post(
                f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos/{repo_slug}/commit",
                headers=_headers(),
                json={"message": commit_message},
                timeout=120.0,
            )
            if c.status_code not in (200, 201):
                return c.text if c.text else json.dumps({"http": c.status_code, "step": "commit"})
            out: dict[str, Any] = {"write": w.json(), "commit": c.json()}
            if push:
                p = httpx.post(
                    f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos/{repo_slug}/push",
                    headers=_headers(),
                    json={},
                    timeout=300.0,
                )
                out["push"] = p.json() if p.status_code == 200 else {"http": p.status_code, "body": p.text[:2000]}
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return repo_write_commit_push


def _make_search_codebase(pid: str):
    @tool
    def search_codebase(query: str, repo_slug: str = "", branch: str = "", top_k: int = 12) -> str:
        """Semantic search over the code index (after clone/sync of a branch). repo_slug and branch optional to narrow."""
        try:
            body: dict[str, Any] = {"query": query, "top_k": max(1, min(int(top_k), 40))}
            if repo_slug.strip():
                body["repo_slug"] = repo_slug.strip()
            if branch.strip():
                body["branch"] = branch.strip()
            r = httpx.post(
                f"{CODE_INDEX_URL.rstrip('/')}/api/v1/projects/{pid}/code-index/search",
                headers=_headers(),
                json=body,
                timeout=90.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return search_codebase


def _make_list_project_repos(pid: str):
    @tool
    def list_project_repos() -> str:
        """List of registered repositories and branches in the project workspace."""
        try:
            r = httpx.get(
                f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos",
                headers={"X-Internal-Token": INTERNAL_TOKEN},
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return list_project_repos


def _make_gitlab_provision_status(pid: str):
    @tool
    def gitlab_provision_status() -> str:
        """Project GitLab group status (platform): ready / failed / in_progress."""
        try:
            r = httpx.get(f"{GITLAB_URL.rstrip('/')}/api/v1/projects/{pid}/gitlab-provision", timeout=20.0)
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return gitlab_provision_status


GIT_PLATFORM_FACTORIES: dict[str, Callable[[str], Any]] = {
    "gitlab_provision_status": _make_gitlab_provision_status,
    "gitlab_create_repository": _make_gitlab_create_repository,
    "list_project_repos": _make_list_project_repos,
    "repo_clone_or_sync": _make_repo_clone_or_sync,
    "repo_read_file": _make_repo_read_file,
    "repo_write_commit_push": _make_repo_write_commit_push,
    "search_codebase": _make_search_codebase,
}


def build_git_platform_tools(project_id: UUID, builtin_flags: dict[str, bool]) -> list[Any]:
    pid = str(project_id)
    tools: list[Any] = []
    for key, factory in GIT_PLATFORM_FACTORIES.items():
        if builtin_flags.get(key, True):
            tools.append(factory(pid))
    return tools
