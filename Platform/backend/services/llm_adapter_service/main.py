import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI

from shared.contracts.api import (
    LlmEmbedRequest,
    LlmEmbedResponse,
    LlmGenerateRequest,
    LlmGenerateResponse,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="llm-adapter-service", version="0.3.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _fetch_settings_from_admin() -> dict[str, Any] | None:
    base = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006").rstrip("/")
    token = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
    try:
        r = httpx.get(
            f"{base}/api/v1/internal/llm-settings",
            headers={"X-Internal-Token": token},
            timeout=5.0,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning("admin llm settings: status %s", r.status_code)
    except Exception as exc:
        logger.warning("admin llm settings unavailable: %s", exc)
    return None


def _resolve_chat_model(cfg: dict[str, Any] | None, request: LlmGenerateRequest) -> str:
    default_model = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    coding_model = (os.getenv("OPENAI_CODING_MODEL") or "").strip()
    if cfg:
        default_model = (cfg.get("default_model") or default_model).strip()
        cm = cfg.get("coding_model")
        if isinstance(cm, str) and cm.strip():
            coding_model = cm.strip()
    if request.model and request.model.strip():
        return request.model.strip()
    if request.purpose == "coding" and coding_model:
        return coding_model
    return default_model


def _resolve_embedding_model(cfg: dict[str, Any] | None, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    env_m = (os.getenv("OPENAI_EMBEDDING_MODEL") or "").strip()
    if env_m:
        return env_m
    if cfg:
        em = cfg.get("embedding_model")
        if isinstance(em, str) and em.strip():
            return em.strip()
    return "text-embedding-3-small"


def _resolve_base_and_key(cfg: dict[str, Any] | None) -> tuple[str, str | None]:
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("OPENAI_API_KEY")
    if cfg:
        base_url = (cfg.get("openai_base_url") or base_url).strip()
        if cfg.get("api_key"):
            api_key = cfg["api_key"]
    return base_url, api_key


@app.post("/api/v1/llm/generate", response_model=LlmGenerateResponse)
def generate(request: LlmGenerateRequest) -> LlmGenerateResponse:
    cfg = _fetch_settings_from_admin()
    base_url, api_key = _resolve_base_and_key(cfg)
    model = _resolve_chat_model(cfg, request)

    if not api_key:
        return LlmGenerateResponse(
            output=f"[MOCK LLM OUTPUT] {request.prompt[:300]}",
            provider="mock",
        )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": request.prompt}],
        "temperature": request.temperature,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=120.0) as client:
        response = client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

    content = body["choices"][0]["message"]["content"]
    return LlmGenerateResponse(output=content, provider="openai-compatible")


@app.post("/api/v1/llm/embed", response_model=LlmEmbedResponse)
def embed(request: LlmEmbedRequest) -> LlmEmbedResponse:
    cfg = _fetch_settings_from_admin()
    base_url, api_key = _resolve_base_and_key(cfg)
    model = _resolve_embedding_model(cfg, request.model)

    if not api_key:
        fake = [[0.0, 0.0, 0.0, 0.0]]
        if isinstance(request.input, list) and len(request.input) > 1:
            fake = [list(fake[0]) for _ in request.input]
        return LlmEmbedResponse(embeddings=fake, model="mock-embedding", provider="mock")

    payload: dict[str, Any] = {"model": model, "input": request.input}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=120.0) as client:
        response = client.post(f"{base_url.rstrip('/')}/embeddings", headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

    data = body.get("data") or []
    ordered = sorted(data, key=lambda x: x.get("index", 0))
    embeddings = [item["embedding"] for item in ordered]
    used = str(body.get("model") or model)
    return LlmEmbedResponse(embeddings=embeddings, model=used, provider="openai-compatible")
