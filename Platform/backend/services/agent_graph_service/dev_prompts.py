"""Загрузка системных промптов ролей из admin-service."""

from __future__ import annotations

import os

import httpx

ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")

_DEFAULTS: dict[str, str] = {
    "pm": "Ты PM: цели MVP и приоритеты.",
    "architect": "Ты архитектор: границы системы и риски.",
    "devops": "Ты DevOps: CI/CD и инфраструктура.",
    "backend": "Ты backend-разработчик: API и сервисы.",
    "frontend": "Ты frontend-разработчик: UI и интеграции.",
}


def fetch_development_prompts() -> dict[str, str]:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/agent-development-prompts",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=8.0,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                out: dict[str, str] = {}
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        out[k] = v.strip()
                return {**_DEFAULTS, **out}
    except Exception:
        pass
    return dict(_DEFAULTS)


def fetch_development_context_summary(project_id: str, max_chars: int = 12000) -> str:
    """Краткая сводка development-context для оркестрации (adapter GET)."""
    adapter = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
    try:
        r = httpx.get(
            f"{adapter.rstrip('/')}/api/v1/projects/{project_id}/development-context",
            timeout=30.0,
        )
        if r.status_code != 200:
            return f"(context http {r.status_code})"
        text = r.text
        if len(text) > max_chars:
            return text[:max_chars] + "\n… [truncated]"
        return text
    except Exception as exc:
        return f"(context error: {exc})"


def fetch_documents_context_summary(project_id: str, query: str = "", max_chars: int = 12000) -> str:
    """RAG-сводка wiki-документации проекта (documents-service)."""
    docs = os.getenv("DOCUMENTS_SERVICE_URL", "http://documents-service:8015")
    payload = {"query": (query or "").strip(), "doc_types": [], "top_k": 10}
    try:
        r = httpx.post(
            f"{docs.rstrip('/')}/api/v1/projects/{project_id}/documents/context",
            json=payload,
            timeout=30.0,
        )
        if r.status_code != 200:
            return f"(docs context http {r.status_code})"
        data = r.json() if "json" in (r.headers.get("content-type") or "") else None
        text = ""
        if isinstance(data, dict):
            text = str(data.get("summary_md") or "")
        if not text:
            text = r.text
        if len(text) > max_chars:
            return text[:max_chars] + "\n… [truncated]"
        return text
    except Exception as exc:
        return f"(docs context error: {exc})"
