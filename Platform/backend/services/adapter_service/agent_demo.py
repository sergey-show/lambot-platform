"""Multi-agent demo scenario: PM → architect → development → DevOps (CI/CD) via GitLab API.
Requires a GitLab resource with a PAT in the project (after provision-gitlab-omnibus or run-stack)."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.adapter_service.crypto import decrypt_secret
from services.adapter_service.models import ProjectInfraResource
from services.adapter_service.clickhouse_activity import log_activity

GITLAB_RESOURCE_LABEL = "GitLab agent (group and token)"
PROJECT_SERVICE_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")
LLM_ADAPTER_URL = os.getenv("LLM_ADAPTER_SERVICE_URL", "http://llm-adapter-service:8005")
ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
_GITLAB_SSL_VERIFY = os.getenv("ADAPTER_GITLAB_SSL_VERIFY", "1").strip().lower() in ("1", "true", "yes")


def _fetch_project_title(project_id: UUID) -> str:
    try:
        r = httpx.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}", timeout=8.0)
        if r.status_code == 200:
            n = r.json().get("name")
            if isinstance(n, str) and n.strip():
                return n.strip()
    except Exception:
        pass
    return "project"


def _fetch_dev_prompts() -> dict[str, str]:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/agent-development-prompts",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=5.0,
        )
        if r.status_code == 200 and isinstance(r.json(), dict):
            return {str(k): str(v) for k, v in r.json().items()}
    except Exception:
        pass
    return {}


def _llm_narrative(
    role: str,
    user_line: str,
    *,
    purpose: str = "default",
) -> str:
    prompts = _fetch_dev_prompts()
    system = prompts.get(role, f"You are a {role} agent on the Lambot team.")
    try:
        r = httpx.post(
            f"{LLM_ADAPTER_URL.rstrip('/')}/api/v1/llm/generate",
            json={
                "prompt": f"{system}\n\n---\n\nBrief title (2–4 sentences), in English: {user_line}",
                "temperature": 0.25,
                "purpose": purpose,
            },
            timeout=90.0,
        )
        if r.status_code == 200:
            out = r.json().get("output") or ""
            return str(out).strip()[:2000]
    except Exception as exc:
        return f"(LLM unavailable: {exc})"
    return "(empty LLM reply)"


def _load_gitlab(db: Session, project_id: UUID) -> dict[str, Any] | None:
    row = db.execute(
        select(ProjectInfraResource)
        .where(
            ProjectInfraResource.project_id == project_id,
            ProjectInfraResource.category == "gitlab",
            ProjectInfraResource.label == GITLAB_RESOURCE_LABEL,
        )
        .limit(1)
    ).scalars().first()
    if row is None or not row.secret_encrypted:
        return None
    vj = row.value_json or {}
    url = (vj.get("gitlab_url") or "").strip()
    group_path = (vj.get("group_path") or "").strip()
    if not url or not group_path:
        return None
    try:
        plain = decrypt_secret(row.secret_encrypted)
        data = json.loads(plain)
    except Exception:
        return None
    token = (data.get("token") or "").strip()
    if not token:
        return None
    return {"gitlab_url": url.rstrip("/"), "group_path": group_path, "token": token}


def _api_v4_base(gitlab_url: str) -> str:
    return f"{gitlab_url.rstrip('/')}/api/v4"


def _gitlab_project_path(group_path: str, repo_slug: str) -> str:
    return f"{group_path}/{repo_slug}"


def run_agent_demo(
    *,
    db: Session,
    project_id: UUID,
    tenant_id: UUID | None,
    repo_slug: str | None = None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    def step(name: str, status: str, summary: str, detail: dict[str, Any] | None = None) -> None:
        entry = {"step": name, "status": status, "summary": summary}
        if detail:
            entry["detail"] = detail
        steps.append(entry)
        log_activity(
            project_id=project_id,
            tenant_id=tenant_id,
            action="agent_demo.step",
            status=status,
            summary=f"{name}: {summary}",
            details=detail or {},
            actor="agent-demo",
            subject=name,
        )

    gl = _load_gitlab(db, project_id)
    if not gl:
        step("precheck", "error", "No GitLab credentials (run Ansible for GitLab and wait for a PAT resource).", {})
        return {"ok": False, "steps": steps, "error": "gitlab_credentials_missing"}

    pname = _fetch_project_title(project_id)
    slug_base = re.sub(r"[^a-z0-9-]+", "-", pname.lower()).strip("-")[:24] or "app"
    short = str(project_id).replace("-", "")[:8]
    repo_slug = (repo_slug or "").strip() or f"lambot-demo-{slug_base}-{short}"
    repo_slug = re.sub(r"[^a-z0-9._-]", "-", repo_slug.lower()).strip("-")[:60]

    headers = {"PRIVATE-TOKEN": gl["token"]}
    api = _api_v4_base(gl["gitlab_url"])
    group_path = gl["group_path"]

    # --- PM
    pm_text = _llm_narrative(
        "pm",
        f"Project «{pname}». State the demo MVP goal: a repository with a mini-app and CI/CD in GitLab.",
        purpose="default",
    )
    step("pm", "ok", "PM: demo direction agreed", {"narrative": pm_text})

    # --- Architect
    arch_text = _llm_narrative(
        "architect",
        f"Project «{pname}». Describe a minimal architecture: one Python (FastAPI) service, one verify+deploy pipeline.",
        purpose="default",
    )
    step("architect", "ok", "Architect: solution outline", {"narrative": arch_text})

    with httpx.Client(timeout=120.0, verify=_GITLAB_SSL_VERIFY) as client:
        gr = client.get(f"{api}/groups/{quote(group_path, safe='')}", headers=headers)
        if gr.status_code != 200:
            step("gitlab_group", "error", f"GitLab group not found: {group_path}", {"http": gr.status_code, "body": gr.text[:500]})
            return {"ok": False, "steps": steps, "error": "gitlab_group_not_found"}

        namespace_id = gr.json().get("id")
        if not namespace_id:
            step("gitlab_group", "error", "GitLab response without a group id", {})
            return {"ok": False, "steps": steps, "error": "gitlab_group_bad_response"}

        create_payload = {
            "name": repo_slug.replace("-", " ").title(),
            "path": repo_slug,
            "namespace_id": namespace_id,
            "visibility": "private",
            "initialize_with_readme": False,
        }
        cr = client.post(f"{api}/projects", headers=headers, json=create_payload)
        proj: dict[str, Any]
        if cr.status_code in (200, 201):
            proj = cr.json()
            step("gitlab_repo", "ok", f"Created project {repo_slug}", {"id": proj.get("id"), "web_url": proj.get("web_url")})
        elif cr.status_code == 409 or (
            cr.status_code == 400 and "already been taken" in (cr.text or "").lower()
        ):
            enc = quote(_gitlab_project_path(group_path, repo_slug), safe="")
            gr2 = client.get(f"{api}/projects/{enc}", headers=headers)
            if gr2.status_code == 200:
                proj = gr2.json()
                step("gitlab_repo", "ok", f"Repository already exists: {repo_slug}", {"id": proj.get("id"), "web_url": proj.get("web_url")})
            else:
                step("gitlab_repo", "error", "Failed to create or find the project", {"http": cr.status_code, "body": cr.text[:800]})
                return {"ok": False, "steps": steps, "error": "gitlab_repo_failed"}
        else:
            step("gitlab_repo", "error", "GitLab rejected project creation", {"http": cr.status_code, "body": cr.text[:800]})
            return {"ok": False, "steps": steps, "error": "gitlab_repo_failed"}

        project_id_gl = proj.get("id")
        if not project_id_gl:
            return {"ok": False, "steps": steps, "error": "gitlab_no_project_id"}

        default_branch = (proj.get("default_branch") or "main").strip() or "main"

        readme = f"""# Lambot demo — {pname}

Mini-service created by the **platform agents** scenario (PM → architect → development → DevOps).

- `app.py` — FastAPI hello
- `.gitlab-ci.yml` — pipeline verify + conditional deploy

Open **CI/CD → Pipelines** in GitLab to see the run (a GitLab Runner is required).
"""
        app_py = '''"""Lambot agent demo — minimal API."""
from fastapi import FastAPI

app = FastAPI(title="Lambot Demo", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "demo": "lambot"}


@app.get("/")
def root():
    return {"message": "Hello from Lambot multi-agent demo"}
'''
        req_txt = "fastapi>=0.110,<1\nuvicorn[standard]>=0.27,<1\n"

        ci_yml = """# Lambot demo pipeline — works with any shell runner (alpine)
stages:
  - verify
  - deploy

verify:
  stage: verify
  image: alpine:3.19
  script:
    - echo "Lambot demo — verify"
    - test -f app.py
    - test -f .gitlab-ci.yml

deploy:
  stage: deploy
  image: alpine:3.19
  script:
    - echo "Lambot demo — conditional deploy (green job = CI/CD configured)"
  when: on_success
"""

        files_spec = [
            ("README.md", readme),
            ("app.py", app_py),
            ("requirements.txt", req_txt),
            (".gitlab-ci.yml", ci_yml),
        ]

        dev_narrative = _llm_narrative(
            "backend",
            f"We added FastAPI app.py and dependencies to repository {repo_slug}. One status sentence.",
            purpose="coding",
        )
        step("dev_backend", "ok", "Backend: code generated (demo template)", {"narrative": dev_narrative})

        for fpath, content in files_spec:
            fe = quote(fpath, safe="")
            url = f"{api}/projects/{project_id_gl}/repository/files/{fe}"
            body = {
                "branch": default_branch,
                "content": content,
                "commit_message": f"Add {fpath} (lambot agent demo)",
            }
            put = client.post(url, headers=headers, json=body)
            if put.status_code not in (200, 201):
                step(
                    f"commit:{fpath}",
                    "error",
                    f"Failed to create file {fpath}",
                    {"http": put.status_code, "body": put.text[:600]},
                )
                return {"ok": False, "steps": steps, "error": "gitlab_file_failed", "file": fpath}

        step("gitlab_files", "ok", f"Uploaded files ({len(files_spec)}) to branch {default_branch}", {})

        devops_narrative = _llm_narrative(
            "devops",
            f"Repository {repo_slug} has .gitlab-ci.yml enabled (stages verify, deploy). Brief note of what to check in GitLab.",
            purpose="coding",
        )
        step("devops", "ok", "DevOps: CI/CD described in the repository", {"narrative": devops_narrative})

        web_url = (proj.get("web_url") or "").rstrip("/")
        pipelines_url = f"{web_url}/-/pipelines" if web_url else ""

    return {
        "ok": True,
        "steps": steps,
        "repo_slug": repo_slug,
        "gitlab_web_url": proj.get("web_url"),
        "pipelines_url": pipelines_url,
        "default_branch": default_branch,
        "note": "If the pipeline does not start — register a GitLab Runner for the project.",
    }
