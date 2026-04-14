"""Browser tool service (Playwright): managed sessions + safe actions."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from playwright.async_api import Browser, BrowserContext, Error, Page, async_playwright

INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
BROWSER_ALLOWED_HOSTS = os.getenv(
    "BROWSER_ALLOWED_HOSTS",
    "frontend,api-gateway,localhost,127.0.0.1,adapter-service,project-service",
)
BROWSER_ALLOW_PUBLIC_WEB = os.getenv("BROWSER_ALLOW_PUBLIC_WEB", "0").strip().lower() in ("1", "true", "yes")
BROWSER_IDLE_TTL_SEC = int(os.getenv("BROWSER_IDLE_TTL_SEC", "900"))
MAX_HTML_CHARS = int(os.getenv("BROWSER_MAX_HTML_CHARS", "50000"))
ARTIFACTS_DIR = Path(os.getenv("BROWSER_ARTIFACTS_DIR", "/tmp/browser-artifacts"))
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

_HOST_RULES = [x.strip().lower() for x in BROWSER_ALLOWED_HOSTS.split(",") if x.strip()]
_RE_PRIVATE = re.compile(r"^(10\.|127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.)")


def _require_internal(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    if (x_internal_token or "").strip() != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_allowed_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return False
    if BROWSER_ALLOW_PUBLIC_WEB:
        return True
    if _RE_PRIVATE.match(h):
        return True
    for r in _HOST_RULES:
        if r.startswith("*."):
            if h.endswith(r[1:]):
                return True
        elif h == r:
            return True
    return False


def _validate_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise HTTPException(status_code=400, detail="url required")
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="only http/https allowed")
    if not _is_allowed_host(p.hostname or ""):
        raise HTTPException(status_code=400, detail=f"host not allowed: {p.hostname}")
    return u


@dataclass
class BrowserSession:
    project_id: str
    run_id: str | None
    browser: Browser
    context: BrowserContext
    page: Page
    created_at: float
    last_used: float


class SessionCreateBody(BaseModel):
    project_id: str
    run_id: str | None = None
    start_url: str | None = None
    viewport_width: int = Field(default=1366, ge=640, le=3840)
    viewport_height: int = Field(default=900, ge=480, le=2160)


class SessionActionBody(BaseModel):
    action: str = Field(..., min_length=2, max_length=32)
    run_id: str | None = None
    url: str | None = None
    selector: str | None = None
    text: str | None = None
    timeout_ms: int = Field(default=12000, ge=500, le=120000)
    wait_until: str = Field(default="domcontentloaded")
    full_page: bool = False


app = FastAPI(title="browser-tool-service", version="0.1.0")
_sessions: dict[str, BrowserSession] = {}
_lock = asyncio.Lock()
_pw = None
_logs: deque[dict[str, Any]] = deque(maxlen=1000)


def _append_log(entry: dict[str, Any]) -> None:
    _logs.append(
        {
            "id": str(uuid4()),
            "created_at": _utc_iso(),
            **entry,
        }
    )


async def _cleanup_idle_sessions() -> None:
    now = asyncio.get_event_loop().time()
    stale: list[str] = []
    async with _lock:
        for sid, sess in _sessions.items():
            if now - sess.last_used > BROWSER_IDLE_TTL_SEC:
                stale.append(sid)
        for sid in stale:
            sess = _sessions.pop(sid, None)
            if sess:
                await sess.context.close()
                await sess.browser.close()


async def _require_session(session_id: str) -> BrowserSession:
    await _cleanup_idle_sessions()
    async with _lock:
        sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    sess.last_used = asyncio.get_event_loop().time()
    return sess


@app.on_event("startup")
async def _startup() -> None:
    global _pw
    _pw = await async_playwright().start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _pw
    async with _lock:
        ids = list(_sessions.keys())
    for sid in ids:
        await _close_session_by_id(sid)
    if _pw is not None:
        await _pw.stop()
        _pw = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/internal/browser/sessions")
async def create_session(
    body: SessionCreateBody,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    _require_internal(x_internal_token)
    if _pw is None:
        raise HTTPException(status_code=503, detail="playwright not ready")
    browser = await _pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": body.viewport_width, "height": body.viewport_height},
        ignore_https_errors=True,
    )
    page = await context.new_page()
    if body.start_url:
        url = _validate_url(body.start_url)
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    sid = str(uuid4())
    now = asyncio.get_event_loop().time()
    sess = BrowserSession(
        project_id=body.project_id,
        run_id=(body.run_id or "").strip() or None,
        browser=browser,
        context=context,
        page=page,
        created_at=now,
        last_used=now,
    )
    async with _lock:
        _sessions[sid] = sess
    _append_log(
        {
            "project_id": body.project_id,
            "run_id": sess.run_id,
            "session_id": sid,
            "action": "create",
            "ok": True,
            "url": body.start_url or "",
        }
    )
    return {"session_id": sid, "created_at": _utc_iso()}


@app.post("/api/v1/internal/browser/sessions/{session_id}/action")
async def session_action(
    session_id: str,
    body: SessionActionBody,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    _require_internal(x_internal_token)
    sess = await _require_session(session_id)
    page = sess.page
    act = body.action.strip().lower()
    run_id = (body.run_id or sess.run_id or "").strip() or None
    try:
        result: dict[str, Any]
        if act == "navigate":
            url = _validate_url(body.url or "")
            await page.goto(url, wait_until=body.wait_until, timeout=body.timeout_ms)
            result = {"ok": True, "action": act, "url": page.url}
        elif act == "click":
            if not body.selector:
                raise HTTPException(status_code=400, detail="selector required")
            await page.click(body.selector, timeout=body.timeout_ms)
            result = {"ok": True, "action": act, "url": page.url}
        elif act == "type":
            if not body.selector:
                raise HTTPException(status_code=400, detail="selector required")
            await page.type(body.selector, body.text or "", timeout=body.timeout_ms)
            result = {"ok": True, "action": act}
        elif act == "fill":
            if not body.selector:
                raise HTTPException(status_code=400, detail="selector required")
            await page.fill(body.selector, body.text or "", timeout=body.timeout_ms)
            result = {"ok": True, "action": act}
        elif act == "press":
            if not body.selector:
                raise HTTPException(status_code=400, detail="selector required")
            await page.press(body.selector, body.text or "Enter", timeout=body.timeout_ms)
            result = {"ok": True, "action": act}
        elif act == "wait":
            await page.wait_for_timeout(max(200, min(body.timeout_ms, 120000)))
            result = {"ok": True, "action": act}
        elif act == "extract_text":
            if not body.selector:
                raise HTTPException(status_code=400, detail="selector required")
            txt = await page.locator(body.selector).inner_text(timeout=body.timeout_ms)
            result = {"ok": True, "action": act, "text": txt[:MAX_HTML_CHARS]}
        elif act == "content":
            html = await page.content()
            result = {"ok": True, "action": act, "url": page.url, "html": html[:MAX_HTML_CHARS]}
        elif act == "screenshot":
            fn = ARTIFACTS_DIR / f"{session_id}-{int(asyncio.get_event_loop().time())}-{uuid4().hex[:8]}.png"
            await page.screenshot(path=str(fn), full_page=body.full_page)
            result = {"ok": True, "action": act, "url": page.url, "artifact_path": str(fn), "artifact_name": fn.name}
        elif act == "snapshot":
            title = await page.title()
            result = {"ok": True, "action": act, "url": page.url, "title": title[:500]}
        else:
            raise HTTPException(status_code=400, detail=f"unsupported action: {act}")
        _append_log(
            {
                "project_id": sess.project_id,
                "run_id": run_id,
                "session_id": session_id,
                "action": act,
                "ok": True,
                "url": str(result.get("url") or page.url or "")[:1000],
                "artifact_name": str(result.get("artifact_name") or ""),
                "details": {k: v for k, v in result.items() if k in ("title", "text", "action")},
            }
        )
        return result
    except Error as exc:
        _append_log(
            {
                "project_id": sess.project_id,
                "run_id": run_id,
                "session_id": session_id,
                "action": act,
                "ok": False,
                "url": page.url,
                "error": str(exc),
            }
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _close_session_by_id(session_id: str) -> bool:
    async with _lock:
        sess = _sessions.pop(session_id, None)
    if not sess:
        return False
    await sess.context.close()
    await sess.browser.close()
    return True


@app.delete("/api/v1/internal/browser/sessions/{session_id}")
async def close_session(
    session_id: str,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    _require_internal(x_internal_token)
    async with _lock:
        sess = _sessions.get(session_id)
    _append_log(
        {
            "project_id": sess.project_id if sess else "",
            "run_id": sess.run_id if sess else None,
            "session_id": session_id,
            "action": "close",
            "ok": True,
        }
    )
    closed = await _close_session_by_id(session_id)
    return {"status": "ok", "closed": closed}


@app.get("/api/v1/internal/browser/logs")
async def list_logs(
    project_id: str,
    run_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=300),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    _require_internal(x_internal_token)
    rid = (run_id or "").strip()
    items = [x for x in reversed(_logs) if str(x.get("project_id") or "") == project_id]
    if rid:
        items = [x for x in items if str(x.get("run_id") or "") == rid]
    return {"items": items[:limit]}


@app.get("/api/v1/internal/browser/artifacts/{artifact_name}")
async def get_artifact(
    artifact_name: str,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    _require_internal(x_internal_token)
    nm = artifact_name.strip()
    if not nm or "/" in nm or "\\" in nm:
        raise HTTPException(status_code=400, detail="invalid artifact name")
    fp = (ARTIFACTS_DIR / nm).resolve()
    if not str(fp).startswith(str(ARTIFACTS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not fp.exists() or not fp.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(path=str(fp), media_type="image/png", filename=nm)
