"""Прямое выполнение GitLab-операций (fallback, если LLM не вызвал tools)."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from uuid import UUID

import httpx

INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000")
REPO_WS_URL = os.getenv("REPO_WORKSPACE_SERVICE_URL", "http://repo-workspace-service:8018")
GITLAB_SVC_URL = os.getenv("GITLAB_SERVICE_URL", "http://gitlab-service:8016")
PROJECT_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")


def _headers() -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"}


def parse_repo_slug_from_message(message: str) -> str | None:
    msg = (message or "").strip().lower()
    if not msg:
        return None
    patterns = [
        r"(?:репозитор(?:ий|ия)|repo(?:sitory)?)\s+[`'\"]?([a-z0-9][a-z0-9._-]{1,62})",
        r"(?:для\s+)?(?:проекта|project)\s+[`'\"]?([a-z0-9][a-z0-9._-]{1,62})",
        r"gitlab[^\n]{0,80}?[`'\"]?([a-z0-9][a-z0-9._-]{1,62})[`'\"]?",
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            slug = m.group(1).strip().lower()
            if slug not in ("gitlab", "проекта", "project", "demo", "репозиторий", "repository"):
                return slug
    return None


def gitlab_repo_create_intent(message: str) -> bool:
    m = (message or "").lower()
    if "gitlab" not in m and "гитлаб" not in m:
        return False
    markers = (
        "создать репозитор",
        "создай репозитор",
        "create repo",
        "create repository",
        "новый репозитор",
        "репозиторий в gitlab",
        "repo in gitlab",
    )
    return any(x in m for x in markers)


def _format_result(title: str, payload: Any) -> str:
    if isinstance(payload, dict):
        return f"### {title}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    return f"### {title}\n\n{payload}"


def execute_gitlab_create_repository(project_id: UUID | str, repo_slug: str, *, description: str = "") -> tuple[bool, str]:
    pid = str(project_id)
    slug = re.sub(r"[^a-z0-9._-]", "-", (repo_slug or "").strip().lower()).strip("-") or "app"
    lines: list[str] = []

    try:
        st = httpx.get(
            f"{GITLAB_SVC_URL.rstrip('/')}/api/v1/projects/{pid}/gitlab-provision",
            timeout=30.0,
        )
        prov = st.json() if st.status_code == 200 else {"http": st.status_code, "body": st.text[:500]}
        lines.append(_format_result("GitLab provision", prov))
        if st.status_code == 200 and not prov.get("gitlab_group_created"):
            pname = slug
            try:
                pr = httpx.get(f"{PROJECT_URL.rstrip('/')}/api/v1/projects/{pid}", timeout=15.0)
                if pr.status_code == 200:
                    pname = str(pr.json().get("name") or slug)
            except Exception:
                pass
            post = httpx.post(
                f"{GITLAB_SVC_URL.rstrip('/')}/api/v1/projects/{pid}/gitlab-provision",
                headers=_headers(),
                json={"project_name": pname, "force": False},
                timeout=180.0,
            )
            prov2 = post.json() if post.content else {"http": post.status_code}
            lines.append(_format_result("GitLab provision (создание группы)", prov2))
            if post.status_code not in (200, 201) or not prov2.get("gitlab_group_created"):
                return False, "\n\n".join(lines) + "\n\nНе удалось подготовить GitLab-группу проекта."
    except Exception as exc:
        return False, f"Ошибка проверки GitLab-группы: {exc}"

    try:
        cr = httpx.post(
            f"{REPO_WS_URL.rstrip('/')}/api/v1/projects/{pid}/repos",
            headers=_headers(),
            json={"repo_slug": slug, "name": slug, "description": description or f"Lambot repo {slug}"},
            timeout=180.0,
        )
        body = cr.json() if cr.content else {"http": cr.status_code, "text": cr.text[:2000]}
        lines.append(_format_result("Создание репозитория", body))
        if cr.status_code not in (200, 201):
            return False, "\n\n".join(lines)
        pwn = body.get("path_with_namespace") or body.get("gitlab", {}).get("path_with_namespace")
        web = body.get("gitlab", {}).get("web_url") if isinstance(body.get("gitlab"), dict) else body.get("web_url")
        summary = f"Репозиторий **{slug}** создан."
        if pwn:
            summary += f" Путь: `{pwn}`."
        if web:
            summary += f" URL: {web}."
        return True, summary + "\n\n" + "\n\n".join(lines)
    except Exception as exc:
        return False, "\n\n".join(lines) + f"\n\nОшибка создания репозитория: {exc}"
