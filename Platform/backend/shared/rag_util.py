"""Общие функции RAG: токенизация, cosine, вызов llm-adapter embed."""

from __future__ import annotations

import math
import os
import re
from typing import Any

import httpx

LLM_ADAPTER_URL = os.getenv("LLM_ADAPTER_SERVICE_URL", os.getenv("LLM_ADAPTER_URL", "http://llm-adapter-service:8005"))


def tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zA-Z0-9_]{3,}", (text or "").lower()) if len(t) >= 3}


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def embed_many(texts: list[str]) -> tuple[list[list[float]] | None, str | None, str | None]:
    if not texts:
        return [], None, None
    try:
        with httpx.Client(timeout=120.0) as client:
            r = client.post(f"{LLM_ADAPTER_URL.rstrip('/')}/api/v1/llm/embed", json={"input": texts})
            if r.status_code != 200:
                return None, None, None
            body = r.json()
            embeds = body.get("embeddings")
            if not isinstance(embeds, list):
                return None, None, None
            valid: list[list[float]] = []
            for item in embeds:
                if isinstance(item, list):
                    valid.append([float(x) for x in item if isinstance(x, (float, int))])
            return valid, str(body.get("model") or ""), str(body.get("provider") or "")
    except Exception:
        return None, None, None
