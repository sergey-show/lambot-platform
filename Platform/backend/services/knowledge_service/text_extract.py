"""Text extraction: PDF, images (shared LLM), plain text."""

from __future__ import annotations

import base64
import logging
import os
import re

import httpx

logger = logging.getLogger("knowledge_extract")

LLM_ADAPTER_URL = os.getenv("LLM_ADAPTER_SERVICE_URL", "http://llm-adapter-service:8005")
MAX_EXTRACT_CHARS = int(os.getenv("KNOWLEDGE_MAX_EXTRACT_CHARS", "120000"))


def _truncate(text: str) -> str:
    t = (text or "").strip()
    if len(t) <= MAX_EXTRACT_CHARS:
        return t
    return t[:MAX_EXTRACT_CHARS] + "\n\n[… truncated …]"


def extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from io import BytesIO

        reader = PdfReader(BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return _truncate("\n\n".join(parts))
    except Exception as exc:
        raise ValueError(f"pdf_extract_failed: {exc}") from exc


def extract_plain(data: bytes, mime: str) -> str:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return _truncate(data.decode(enc))
        except Exception:
            continue
    raise ValueError("text_decode_failed")


def extract_image_with_llm(data: bytes, mime: str) -> tuple[str, str]:
    """Recognition via the shared (default) chat model."""
    b64 = base64.standard_b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "This is a page/scan for the corporate knowledge base. "
                            "Return all readable text and a short description of diagrams/tables in the image. "
                            "Format: markdown, facts only, no fluff."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(f"{LLM_ADAPTER_URL.rstrip('/')}/v1/chat/completions", json=payload)
        if r.status_code != 200:
            raise ValueError(f"llm_vision_failed: http {r.status_code}")
        body = r.json()
        model = str(body.get("model") or "")
        choices = body.get("choices") or []
        if not choices:
            raise ValueError("llm_vision_empty")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = str(content or "")
        return _truncate(text), model


def extract_document(*, data: bytes, mime: str, filename: str) -> tuple[str, str | None]:
    """Returns (text, recognition_model)."""
    mime_l = (mime or "").lower().strip()
    name_l = (filename or "").lower()
    if mime_l == "application/pdf" or name_l.endswith(".pdf"):
        return extract_pdf(data), None
    if mime_l.startswith("image/"):
        return extract_image_with_llm(data, mime_l)
    if mime_l.startswith("text/") or name_l.endswith((".md", ".txt", ".csv", ".json")):
        return extract_plain(data, mime_l), None
    # fallback: try plain
    try:
        return extract_plain(data, mime_l), None
    except ValueError:
        raise ValueError(f"unsupported_mime: {mime_l or 'unknown'}")
