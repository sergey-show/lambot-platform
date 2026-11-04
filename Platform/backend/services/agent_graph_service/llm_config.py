"""ChatOpenAI config (OpenAI-compatible), aligned with llm-adapter / admin."""

from __future__ import annotations

import os
from typing import Any

import httpx
from langchain_openai import ChatOpenAI

ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")


def _fetch_llm_settings() -> dict[str, Any] | None:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/llm-settings",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=5.0,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def build_chat_model(
    *,
    purpose: str = "coding",
    timeout_sec: float | None = None,
    model_override: str | None = None,
    base_url_override: str | None = None,
) -> ChatOpenAI:
    """
    purpose: default — shared model; coding — coding model (as in llm-adapter).
    timeout_sec: override HTTP timeout for short calls (classifiers, etc.).
    model_override: explicit model name (e.g. a light model for intent classification).
    base_url_override: another OpenAI-compatible endpoint (separate vLLM under load).
    If AGENT_GRAPH_OPENAI_BASE_URL is set (and no base_url_override) — after admin, a proxy
    is used (e.g. llm-adapter /v1) so every chat/completions call is logged in Langfuse.
    """
    base_url = (base_url_override or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip()
    proxy_base = (os.getenv("AGENT_GRAPH_OPENAI_BASE_URL") or "").strip()
    api_key = os.getenv("OPENAI_API_KEY") or "dummy"
    default_model = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    coding_model = os.getenv("OPENAI_CODING_MODEL", "").strip()
    cfg = _fetch_llm_settings()
    if cfg:
        if not base_url_override:
            base_url = (cfg.get("openai_base_url") or base_url).strip()
        default_model = (cfg.get("default_model") or default_model).strip()
        cm = cfg.get("coding_model")
        if isinstance(cm, str) and cm.strip():
            coding_model = cm.strip()
        if cfg.get("api_key"):
            api_key = cfg["api_key"]
    if proxy_base and not base_url_override:
        base_url = proxy_base.rstrip("/")
    if model_override and str(model_override).strip():
        model = str(model_override).strip()
    else:
        model = coding_model if purpose == "coding" and coding_model else default_model
    llm_timeout = (
        float(timeout_sec)
        if timeout_sec is not None
        else float(os.getenv("OPENAI_AGENT_TIMEOUT_SEC", "1800"))
    )
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        temperature=0.15,
        timeout=llm_timeout,
    )
