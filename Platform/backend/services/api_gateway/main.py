import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
import httpx
import jwt
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from urllib.parse import urlencode

PROJECT_SERVICE_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")
PLANNING_SERVICE_URL = os.getenv("PLANNING_SERVICE_URL", "http://planning-service:8002")
ORCHESTRATOR_SERVICE_URL = os.getenv("ORCHESTRATOR_SERVICE_URL", "http://orchestrator-service:8003")
CATALOG_SERVICE_URL = os.getenv("CATALOG_SERVICE_URL", "http://catalog-service:8004")
ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
ANSIBLE_ORCHESTRATOR_SERVICE_URL = os.getenv(
    "ANSIBLE_ORCHESTRATOR_SERVICE_URL", "http://ansible-orchestrator-service:8007"
)
ADAPTER_SERVICE_URL = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
AGENT_BOOTSTRAP_SERVICE_URL = os.getenv(
    "AGENT_BOOTSTRAP_SERVICE_URL", "http://agent-bootstrap-service:8011"
)
AGENT_BOOTSTRAP_INTERNAL_TOKEN = os.getenv(
    "AGENT_BOOTSTRAP_INTERNAL_TOKEN", "internal-agent-bootstrap-dev-token"
)
SANDBOX_SERVICE_URL = os.getenv("SANDBOX_SERVICE_URL", "http://sandbox-service:8012")
SANDBOX_SERVICE_TOKEN = os.getenv("SANDBOX_SERVICE_TOKEN", "internal-sandbox-dev-token")
AGENT_GRAPH_SERVICE_URL = os.getenv("AGENT_GRAPH_SERVICE_URL", "http://agent-graph-service:8013")
BROWSER_TOOL_SERVICE_URL = os.getenv("BROWSER_TOOL_SERVICE_URL", "http://browser-tool-service:8014")
DOCUMENTS_SERVICE_URL = os.getenv("DOCUMENTS_SERVICE_URL", "http://documents-service:8015")
KNOWLEDGE_SERVICE_URL = os.getenv("KNOWLEDGE_SERVICE_URL", "http://knowledge-service:8019")
GITLAB_SERVICE_URL = os.getenv("GITLAB_SERVICE_URL", "http://gitlab-service:8016")
INTERNAL_PROJECT_ADMIN_TOKEN = os.getenv(
    "INTERNAL_PROJECT_ADMIN_TOKEN", "internal-project-admin-dev-token"
)
SUPERADMIN_TOKEN = os.getenv("SUPERADMIN_TOKEN", "superadmin-dev-token")
USER_BEARER_TOKEN = os.getenv("USER_BEARER_TOKEN", "user-dev-token")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-jwt-secret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
INTERNAL_LLM_SETTINGS_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")

HTTP_EXEC_MAX_TIMEOUT_SEC = float(os.getenv("HTTP_EXEC_MAX_TIMEOUT_SEC", "45"))
HTTP_EXEC_MAX_BODY_CHARS = int(os.getenv("HTTP_EXEC_MAX_BODY_CHARS", "200000"))
HTTP_EXEC_MAX_RESPONSE_CHARS = int(os.getenv("HTTP_EXEC_MAX_RESPONSE_CHARS", "50000"))

ROLE_OWNER = "Owner"
ROLE_ADMIN = "Admin"
ROLE_VIEWER = "Viewer"
ROLE_SUPERADMIN = "Superadmin"
USER_ROLES = {ROLE_OWNER, ROLE_ADMIN, ROLE_VIEWER, ROLE_SUPERADMIN}


def _agent_graph_ws_base_url() -> str:
    base = (AGENT_GRAPH_SERVICE_URL or "").strip().rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base


logger = logging.getLogger(__name__)

app = FastAPI(title="api-gateway", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

REQUEST_COUNTER: dict[tuple[str, str, str], int] = defaultdict(int)
REQUEST_LATENCY_SUM: dict[tuple[str, str], float] = defaultdict(float)
REQUEST_LATENCY_COUNT: dict[tuple[str, str], int] = defaultdict(int)


@dataclass
class AuthContext:
    subject: str
    roles: set[str]
    tenant_id: str | None = None
    email: str | None = None


def parse_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.replace("Bearer ", "", 1).strip()


def _append_activity_headers(request: Request, headers: dict[str, str]) -> None:
    """Forwarded to adapter for the ClickHouse log (email / sub from JWT)."""
    try:
        token = parse_bearer_token(request.headers.get("Authorization"))
        ctx = decode_auth_context(token)
        if ctx.email:
            headers["X-Activity-Actor"] = ctx.email[:512]
        if ctx.subject:
            headers["X-Activity-Subject"] = ctx.subject[:128]
    except Exception:
        pass


def decode_auth_context(token: str) -> AuthContext:
    if token == SUPERADMIN_TOKEN:
        return AuthContext(subject="local-superadmin", roles={ROLE_SUPERADMIN}, tenant_id=None, email=None)
    if token == USER_BEARER_TOKEN:
        return AuthContext(subject="local-owner", roles={ROLE_OWNER}, tenant_id=None, email=None)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid JWT token: {exc}") from exc
    subject = str(payload.get("sub") or "")
    if not subject:
        raise HTTPException(status_code=401, detail="JWT missing 'sub' claim")
    roles_claim = payload.get("roles", [])
    if isinstance(roles_claim, str):
        roles = {roles_claim}
    else:
        roles = {str(role) for role in roles_claim}
    tid = payload.get("tenant_id")
    tenant_id = str(tid) if tid else None
    em = payload.get("email")
    email = str(em) if em else None
    return AuthContext(subject=subject, roles=roles, tenant_id=tenant_id, email=email)


def require_roles(request: Request, allowed_roles: set[str]) -> AuthContext:
    token = parse_bearer_token(request.headers.get("Authorization"))
    context = decode_auth_context(token)
    if not (context.roles & allowed_roles):
        raise HTTPException(status_code=403, detail="Insufficient role")
    return context


async def assert_project_tenant_access(ctx: AuthContext, project_id: str) -> None:
    if ROLE_SUPERADMIN in ctx.roles:
        return
    if ctx.tenant_id is None:
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Project not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Project service error")
    data = r.json()
    if str(data.get("tenant_id")) != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="Project not in your tenant")


async def assert_project_mutable(ctx: AuthContext, project_id: str) -> None:
    """Project exists and is not deleted; for a user — tenant matches; not suspended."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Project not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Project service error")
    data = r.json()
    if ROLE_SUPERADMIN not in ctx.roles and ctx.tenant_id is not None:
        if str(data.get("tenant_id")) != ctx.tenant_id:
            raise HTTPException(status_code=403, detail="Project not in your tenant")
    if data.get("suspended"):
        raise HTTPException(status_code=403, detail="Project suspended by administrator")


def _admin_tracker_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Internal-Token": INTERNAL_LLM_SETTINGS_TOKEN,
    }


def _knowledge_internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_LLM_SETTINGS_TOKEN}


_HTTP_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_HTTP_FORBIDDEN_HEADER_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "x-internal-token",
        "x-superadmin-token",
        "x-sandbox-token",
        "content-length",
    }
)
_HTTP_HEADER_KEY_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_HTTP_ALIAS_CONFIG: dict[str, dict[str, object]] = {
    "adapter": {
        "base_url": ADAPTER_SERVICE_URL,
        "path_prefixes": ["/api/v1/projects/{project_id}/"],
        "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
    },
    "project": {
        "base_url": PROJECT_SERVICE_URL,
        "path_prefixes": ["/api/v1/projects/{project_id}/", "/api/v1/projects/{project_id}"],
        "methods": ["GET", "POST", "PUT", "PATCH"],
    },
    "planning": {
        "base_url": PLANNING_SERVICE_URL,
        "path_prefixes": ["/api/v1/projects/{project_id}/"],
        "methods": ["GET", "POST"],
    },
    "catalog": {
        "base_url": CATALOG_SERVICE_URL,
        "path_prefixes": ["/api/v1/"],
        "methods": ["GET"],
    },
    "agent_graph": {
        "base_url": AGENT_GRAPH_SERVICE_URL,
        "path_prefixes": ["/api/v1/projects/{project_id}/"],
        "methods": ["GET", "POST"],
    },
    "documents": {
        "base_url": DOCUMENTS_SERVICE_URL,
        "path_prefixes": ["/api/v1/projects/{project_id}/documents"],
        "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
    },
}


def _trim_text(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "\n… [truncated]"


def _normalize_http_path(path: str, project_id: str) -> str:
    p = (path or "").strip()
    if not p:
        raise HTTPException(status_code=400, detail="path is required")
    if "://" in p:
        raise HTTPException(status_code=400, detail="absolute URL is not allowed, use alias + path")
    if not p.startswith("/"):
        p = "/" + p
    return p.replace("{project_id}", project_id)


def _build_http_headers(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        kk = str(k).strip()
        if not kk or not _HTTP_HEADER_KEY_RE.match(kk):
            continue
        if kk.lower() in _HTTP_FORBIDDEN_HEADER_KEYS:
            continue
        vv = str(v).strip()
        if not vv:
            continue
        out[kk] = vv[:500]
    return out


def mark_request(method: str, path: str, status: int, elapsed_sec: float) -> None:
    REQUEST_COUNTER[(method, path, str(status))] += 1
    REQUEST_LATENCY_SUM[(method, path)] += elapsed_sec
    REQUEST_LATENCY_COUNT[(method, path)] += 1


def _response_from_httpx(response: httpx.Response) -> JSONResponse:
    try:
        content = response.json()
    except Exception:
        content = {"raw": response.text}
    return JSONResponse(status_code=response.status_code, content=content)


async def proxy_json(
    method: str,
    url: str,
    request: Request,
    extra_headers: dict | None = None,
    timeout: float = 30.0,
):
    started = time.monotonic()
    payload = None
    if method in {"POST", "PUT", "PATCH"}:
        payload = await request.json()
    headers = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    if extra_headers:
        headers.update(extra_headers)
    if ADAPTER_SERVICE_URL in url:
        _append_activity_headers(request, headers)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, json=payload, headers=headers)
    mark_request(method, request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


async def downstream_health_metric() -> dict[str, int]:
    services = {
        "project_service": PROJECT_SERVICE_URL,
        "planning_service": PLANNING_SERVICE_URL,
        "orchestrator_service": ORCHESTRATOR_SERVICE_URL,
        "catalog_service": CATALOG_SERVICE_URL,
        "admin_service": ADMIN_SERVICE_URL,
        "ansible_orchestrator_service": ANSIBLE_ORCHESTRATOR_SERVICE_URL,
        "adapter_service": ADAPTER_SERVICE_URL,
        "agent_bootstrap_service": AGENT_BOOTSTRAP_SERVICE_URL,
        "sandbox_service": SANDBOX_SERVICE_URL,
        "agent_graph_service": AGENT_GRAPH_SERVICE_URL,
        "browser_tool_service": BROWSER_TOOL_SERVICE_URL,
    }
    out: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in services.items():
            try:
                resp = await client.get(f"{url}/health")
                out[name] = 1 if resp.status_code == 200 else 0
            except Exception:
                out[name] = 0
    return out


def render_metrics_text(service_health: dict[str, int]) -> str:
    lines: list[str] = []
    lines.append("# HELP api_gateway_requests_total Total requests passing through gateway.")
    lines.append("# TYPE api_gateway_requests_total counter")
    for (method, path, status), value in sorted(REQUEST_COUNTER.items()):
        lines.append(
            f'api_gateway_requests_total{{method="{method}",path="{path}",status="{status}"}} {value}'
        )
    lines.append("# HELP api_gateway_request_latency_seconds_sum Total observed request latency.")
    lines.append("# TYPE api_gateway_request_latency_seconds_sum counter")
    for (method, path), value in sorted(REQUEST_LATENCY_SUM.items()):
        lines.append(f'api_gateway_request_latency_seconds_sum{{method="{method}",path="{path}"}} {value}')
    lines.append("# HELP api_gateway_request_latency_seconds_count Request latency sample count.")
    lines.append("# TYPE api_gateway_request_latency_seconds_count counter")
    for (method, path), value in sorted(REQUEST_LATENCY_COUNT.items()):
        lines.append(f'api_gateway_request_latency_seconds_count{{method="{method}",path="{path}"}} {value}')
    lines.append("# HELP api_gateway_service_health Downstream service health status.")
    lines.append("# TYPE api_gateway_service_health gauge")
    for service_name, value in sorted(service_health.items()):
        lines.append(f'api_gateway_service_health{{service="{service_name}"}} {value}')
    return "\n".join(lines) + "\n"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/auth/me")
def auth_me(request: Request):
    ctx = require_roles(request, USER_ROLES)
    return {
        "subject": ctx.subject,
        "tenant_id": ctx.tenant_id,
        "email": ctx.email,
        "roles": sorted(ctx.roles),
    }


@app.post("/api/v1/auth/login")
async def auth_login(request: Request):
    payload = await request.json()
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(f"{ADMIN_SERVICE_URL}/api/v1/auth/login", json=payload)
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/internal/projects/{project_id}/http/execute")
async def internal_http_execute(project_id: str, request: Request):
    """Safe HTTP executor for agent-graph tools (alias + path + method)."""
    xit = (request.headers.get("X-Internal-Token") or "").strip()
    if xit != INTERNAL_LLM_SETTINGS_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    alias = str(payload.get("endpoint_alias") or "").strip().lower()
    cfg = _HTTP_ALIAS_CONFIG.get(alias)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"unknown endpoint_alias: {alias}")

    method = str(payload.get("method") or "GET").strip().upper()
    if method not in _HTTP_ALLOWED_METHODS:
        raise HTTPException(status_code=400, detail=f"unsupported method: {method}")
    cfg_methods = {str(x).upper() for x in (cfg.get("methods") or [])}
    if cfg_methods and method not in cfg_methods:
        raise HTTPException(status_code=400, detail=f"method {method} not allowed for alias {alias}")

    path = _normalize_http_path(str(payload.get("path") or ""), project_id)
    prefixes = [str(x).replace("{project_id}", project_id) for x in (cfg.get("path_prefixes") or [])]
    if prefixes and not any(path.startswith(p) for p in prefixes):
        raise HTTPException(status_code=400, detail=f"path is not allowed for alias {alias}")

    base_url = str(cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=500, detail=f"alias {alias} misconfigured")

    q = payload.get("query")
    query_pairs: list[tuple[str, str]] = []
    if isinstance(q, dict):
        for k, v in q.items():
            kk = str(k).strip()
            if not kk:
                continue
            if isinstance(v, list):
                for vv in v:
                    query_pairs.append((kk, str(vv)))
            else:
                query_pairs.append((kk, str(v)))
    query_str = urlencode(query_pairs, doseq=True) if query_pairs else ""
    url = f"{base_url}{path}"
    if query_str:
        url = f"{url}?{query_str}"

    timeout_sec = payload.get("timeout_sec")
    try:
        timeout_val = float(timeout_sec) if timeout_sec is not None else 20.0
    except (TypeError, ValueError):
        timeout_val = 20.0
    timeout_val = max(1.0, min(timeout_val, HTTP_EXEC_MAX_TIMEOUT_SEC))

    headers = _build_http_headers(payload.get("headers"))
    body_json = payload.get("json_body")
    body_text = payload.get("text_body")
    if body_text is not None and not isinstance(body_text, str):
        body_text = str(body_text)
    if isinstance(body_text, str):
        body_text = _trim_text(body_text, HTTP_EXEC_MAX_BODY_CHARS)
    if body_json is not None:
        if not isinstance(body_json, (dict, list)):
            raise HTTPException(status_code=400, detail="json_body must be object or array")
        if len(str(body_json)) > HTTP_EXEC_MAX_BODY_CHARS:
            raise HTTPException(status_code=400, detail="json_body too large")

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout_val, follow_redirects=False) as client:
        response = await client.request(
            method,
            url,
            headers=headers or None,
            json=body_json if body_json is not None else None,
            content=body_text.encode("utf-8") if body_json is None and isinstance(body_text, str) else None,
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)

    raw = response.text or ""
    raw = _trim_text(raw, HTTP_EXEC_MAX_RESPONSE_CHARS)
    parsed_json: object | None = None
    ctype = (response.headers.get("content-type") or "").lower()
    if "json" in ctype:
        try:
            parsed_json = response.json()
        except Exception:
            parsed_json = None

    return {
        "ok": response.status_code < 400,
        "endpoint_alias": alias,
        "method": method,
        "url": url,
        "status_code": response.status_code,
        "elapsed_ms": elapsed_ms,
        "response_headers": {
            "content-type": response.headers.get("content-type", ""),
            "content-length": response.headers.get("content-length", ""),
        },
        "response_json": parsed_json,
        "response_text": raw,
    }


@app.post("/api/v1/internal/projects/{project_id}/browser/execute")
async def internal_browser_execute(project_id: str, request: Request):
    """Proxy for browser-tool-service (internal token only)."""
    xit = (request.headers.get("X-Internal-Token") or "").strip()
    if xit != INTERNAL_LLM_SETTINGS_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    action = str(payload.get("action") or "").strip().lower()
    timeout = float(payload.get("timeout_sec") or 90.0)
    timeout = max(5.0, min(timeout, 300.0))
    headers = {"X-Internal-Token": INTERNAL_LLM_SETTINGS_TOKEN, "Content-Type": "application/json"}
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        if action == "create":
            body = dict(payload)
            body["project_id"] = project_id
            response = await client.post(
                f"{BROWSER_TOOL_SERVICE_URL}/api/v1/internal/browser/sessions",
                json=body,
                headers=headers,
            )
        elif action == "close":
            sid = str(payload.get("session_id") or "").strip()
            if not sid:
                raise HTTPException(status_code=400, detail="session_id required for close")
            response = await client.delete(
                f"{BROWSER_TOOL_SERVICE_URL}/api/v1/internal/browser/sessions/{sid}",
                headers=headers,
            )
        else:
            sid = str(payload.get("session_id") or "").strip()
            if not sid:
                raise HTTPException(status_code=400, detail="session_id required")
            response = await client.post(
                f"{BROWSER_TOOL_SERVICE_URL}/api/v1/internal/browser/sessions/{sid}/action",
                json=payload,
                headers=headers,
            )
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/internal/projects/{project_id}/documents/context")
async def internal_documents_context(project_id: str, request: Request):
    xit = (request.headers.get("X-Internal-Token") or "").strip()
    if xit != INTERNAL_LLM_SETTINGS_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await proxy_json(
        "POST",
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/context",
        request,
        timeout=60.0,
    )


@app.post("/api/v1/internal/projects/{project_id}/documents/upsert")
async def internal_documents_upsert(project_id: str, request: Request):
    xit = (request.headers.get("X-Internal-Token") or "").strip()
    if xit != INTERNAL_LLM_SETTINGS_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await proxy_json(
        "POST",
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/upsert",
        request,
        timeout=90.0,
    )


@app.get("/api/v1/admin/users")
async def admin_users_list(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/users",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/admin/users")
async def admin_users_create(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "POST",
        f"{ADMIN_SERVICE_URL}/api/v1/admin/users",
        request,
        extra_headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
    )


@app.get("/metrics")
async def metrics() -> Response:
    service_health = await downstream_health_metric()
    payload = render_metrics_text(service_health)
    return Response(content=payload, media_type="text/plain; version=0.0.4")


@app.get("/api/v1/projects")
async def list_projects_gateway(request: Request):
    ctx = require_roles(request, USER_ROLES)
    if ROLE_SUPERADMIN in ctx.roles:
        raise HTTPException(status_code=400, detail="Superadmin: use /api/v1/admin/projects")
    if not ctx.tenant_id:
        raise HTTPException(status_code=400, detail="JWT with tenant_id required for project list")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{PROJECT_SERVICE_URL}/api/v1/projects",
            params={"tenant_id": ctx.tenant_id},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects")
async def create_project(request: Request):
    ctx = require_roles(request, USER_ROLES)
    payload = await request.json()
    if ROLE_SUPERADMIN not in ctx.roles and ctx.tenant_id:
        payload = {**payload, "tenant_id": ctx.tenant_id}
    started = time.monotonic()
    headers = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{PROJECT_SERVICE_URL}/api/v1/projects", json=payload, headers=headers)
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}")
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/status")
async def get_project_status(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}/status")
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/planning/run")
async def run_planning(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    payload = await request.json()
    if ROLE_SUPERADMIN not in ctx.roles and ctx.tenant_id:
        payload = {**payload, "tenant_id": ctx.tenant_id}
    started = time.monotonic()
    headers = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{PLANNING_SERVICE_URL}/api/v1/projects/{project_id}/planning/run",
            json=payload,
            headers=headers,
        )
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/orchestrator/transition")
async def transition(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json("POST", f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}/status/transition", request)


@app.get("/api/v1/catalog")
async def list_catalog(request: Request):
    require_roles(request, USER_ROLES)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{CATALOG_SERVICE_URL}/api/v1/catalog")
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/admin/overview")
async def admin_overview(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    extra = {"X-Superadmin-Token": SUPERADMIN_TOKEN}
    return await proxy_json("GET", f"{ADMIN_SERVICE_URL}/api/v1/admin/overview", request, extra_headers=extra)


@app.get("/api/v1/admin/projects")
async def admin_projects(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    params = str(request.url.query)
    suffix = f"?{params}" if params else ""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/projects{suffix}",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
        gitlab_by_id: dict[str, dict] = {}
        try:
            gr = await client.get(
                f"{GITLAB_SERVICE_URL.rstrip('/')}/api/v1/internal/gitlab-provision/index",
                headers={"X-Internal-Token": INTERNAL_LLM_SETTINGS_TOKEN},
                timeout=15.0,
            )
            if gr.status_code == 200:
                gitlab_by_id = gr.json().get("by_project_id") or {}
        except Exception:
            pass
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    if response.status_code != 200:
        return _response_from_httpx(response)
    projects = response.json()
    if isinstance(projects, list):
        for row in projects:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("id") or "")
            g = gitlab_by_id.get(pid) or {}
            row["gitlab_provision_status"] = g.get("status") or "not_started"
            row["gitlab_group_created"] = bool(g.get("gitlab_group_created"))
            row["gitlab_group_path"] = g.get("group_path")
    return JSONResponse(status_code=response.status_code, content=projects)


async def _gitlab_provision_call(project_id: str, *, project_name: str, force: bool = False) -> httpx.Response:
    async with httpx.AsyncClient(timeout=180.0) as client:
        return await client.post(
            f"{GITLAB_SERVICE_URL.rstrip('/')}/api/v1/projects/{project_id}/gitlab-provision",
            headers={
                "X-Internal-Token": INTERNAL_LLM_SETTINGS_TOKEN,
                "Content-Type": "application/json",
            },
            json={"project_name": project_name, "force": force},
        )


@app.get("/api/v1/admin/projects/{project_id}/gitlab-provision")
async def admin_gitlab_provision_status(project_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{GITLAB_SERVICE_URL.rstrip('/')}/api/v1/projects/{project_id}/gitlab-provision")
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/admin/projects/{project_id}/gitlab-provision")
async def admin_gitlab_provision_run(project_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(body.get("force"))
    async with httpx.AsyncClient(timeout=15.0) as client:
        pr = await client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}")
    if pr.status_code == 404:
        raise HTTPException(status_code=404, detail="Project not found")
    if pr.status_code != 200:
        raise HTTPException(status_code=502, detail="Project service error")
    pname = str((pr.json() or {}).get("name") or "project")
    started = time.monotonic()
    response = await _gitlab_provision_call(project_id, project_name=pname, force=force)
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/gitlab-provision")
async def project_gitlab_provision_status(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{GITLAB_SERVICE_URL.rstrip('/')}/api/v1/projects/{project_id}/gitlab-provision")
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/gitlab-provision")
async def project_gitlab_provision_run(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(body.get("force"))
    async with httpx.AsyncClient(timeout=15.0) as client:
        pr = await client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}")
    if pr.status_code != 200:
        raise HTTPException(status_code=502, detail="Project service error")
    pname = str((pr.json() or {}).get("name") or "project")
    started = time.monotonic()
    response = await _gitlab_provision_call(project_id, project_name=pname, force=force)
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.patch("/api/v1/admin/projects/{project_id}")
async def admin_patch_project(project_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    payload = await request.json()
    started = time.monotonic()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Project-Token": INTERNAL_PROJECT_ADMIN_TOKEN,
    }
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.patch(
            f"{PROJECT_SERVICE_URL}/api/v1/internal/projects/{project_id}",
            json=payload,
            headers=headers,
        )
    mark_request("PATCH", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.delete("/api/v1/admin/projects/{project_id}")
async def admin_delete_project(project_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    headers = {"X-Internal-Project-Token": INTERNAL_PROJECT_ADMIN_TOKEN}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.delete(
            f"{PROJECT_SERVICE_URL}/api/v1/internal/projects/{project_id}",
            headers=headers,
        )
    mark_request("DELETE", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/admin/services/health")
async def admin_services_health(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/services/health",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/admin/ai-team/runs/live")
async def admin_ai_team_runs_live(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    params = str(request.url.query)
    suffix = f"?{params}" if params else ""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/ai-team/runs/live{suffix}",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/admin/llm-settings")
async def admin_llm_settings_get(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/llm-settings",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.put("/api/v1/admin/llm-settings")
async def admin_llm_settings_put(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PUT",
        f"{ADMIN_SERVICE_URL}/api/v1/admin/llm-settings",
        request,
        extra_headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
    )


@app.get("/api/v1/admin/agent-settings")
async def admin_agent_settings_get(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-settings",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.put("/api/v1/admin/agent-settings")
async def admin_agent_settings_put(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PUT",
        f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-settings",
        request,
        extra_headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
    )


@app.get("/api/v1/admin/agent-development-prompts")
async def admin_agent_dev_prompts_get(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-development-prompts",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.put("/api/v1/admin/agent-development-prompts")
async def admin_agent_dev_prompts_put(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PUT",
        f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-development-prompts",
        request,
        extra_headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
    )


@app.get("/api/v1/admin/agent-graph-tools")
async def admin_agent_graph_tools_get(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-graph-tools",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.put("/api/v1/admin/agent-graph-tools/builtins")
async def admin_agent_graph_builtins_put(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PUT",
        f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-graph-tools/builtins",
        request,
        extra_headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
    )


@app.post("/api/v1/admin/agent-graph-tools/http")
async def admin_agent_graph_http_post(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "POST",
        f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-graph-tools/http",
        request,
        extra_headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
    )


@app.patch("/api/v1/admin/agent-graph-tools/http/{tool_id}")
async def admin_agent_graph_http_patch(tool_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PATCH",
        f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-graph-tools/http/{tool_id}",
        request,
        extra_headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
    )


@app.delete("/api/v1/admin/agent-graph-tools/http/{tool_id}")
async def admin_agent_graph_http_delete(tool_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/agent-graph-tools/http/{tool_id}",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("DELETE", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


async def _proxy_get(url: str, request: Request) -> JSONResponse:
    started = time.monotonic()
    # Only the query from the incoming request. If the url already had "?…", do not duplicate (?limit=1?limit=1 → FastAPI 422).
    base = url.split("?", 1)[0]
    q = request.url.query
    url = f"{base}?{q}" if q else base
    headers: dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    if url.startswith(ADAPTER_SERVICE_URL):
        _append_activity_headers(request, headers)
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, headers=headers)
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/ansible/swarm/catalog")
async def ansible_swarm_catalog(request: Request):
    require_roles(request, USER_ROLES)
    return await _proxy_get(f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/ansible/swarm/catalog", request)


@app.get("/api/v1/ansible/swarm/templates/{template_id}")
async def ansible_swarm_template_detail(template_id: str, request: Request):
    require_roles(request, USER_ROLES)
    return await _proxy_get(
        f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/ansible/swarm/templates/{template_id}",
        request,
    )


@app.post("/api/v1/ansible/swarm/render")
async def ansible_swarm_render(request: Request):
    require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    return await proxy_json("POST", f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/ansible/swarm/render", request)


@app.post("/api/v1/ansible/swarm/prepare-bundle")
async def ansible_swarm_prepare_bundle(request: Request):
    require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    return await proxy_json(
        "POST", f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/ansible/swarm/prepare-bundle", request
    )


@app.post("/api/v1/projects/{project_id}/targets/test-connection")
async def test_server_target_connection(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/targets/test-connection",
        request,
    )


@app.post("/api/v1/projects/{project_id}/targets")
async def create_server_target(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/targets",
        request,
        timeout=120.0,
    )


@app.get("/api/v1/projects/{project_id}/targets")
async def list_server_targets(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/targets", request)


@app.post("/api/v1/projects/{project_id}/targets/{target_id}/ssh-exec")
async def ssh_exec_on_target_gateway(project_id: str, target_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/targets/{target_id}/ssh-exec",
        request,
        timeout=180.0,
    )


@app.delete("/api/v1/projects/{project_id}/targets/{target_id}")
async def delete_server_target(project_id: str, target_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    headers: dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    _append_activity_headers(request, headers)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/targets/{target_id}",
            headers=headers,
        )
    mark_request("DELETE", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/targets/{target_id}/prepare")
async def prepare_server_target(project_id: str, target_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/targets/{target_id}/prepare",
        request,
    )


@app.post("/api/v1/projects/{project_id}/targets/{target_id}/confirm-setup")
async def confirm_server_target(project_id: str, target_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    headers: dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    _append_activity_headers(request, headers)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/targets/{target_id}/confirm-setup",
            headers=headers,
        )
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/workspace")
async def project_workspace(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/workspace", request)


@app.get("/api/v1/projects/{project_id}/infra/service-assignments")
async def infra_service_assignments_get(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/service-assignments", request)


@app.put("/api/v1/projects/{project_id}/infra/service-assignments")
async def infra_service_assignments_put(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "PUT",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/service-assignments",
        request,
    )


@app.get("/api/v1/projects/{project_id}/infra/resources")
async def infra_resources_list(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/resources", request)


@app.post("/api/v1/projects/{project_id}/infra/resources")
async def infra_resources_create(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/resources",
        request,
    )


@app.patch("/api/v1/projects/{project_id}/infra/resources/{resource_id}")
async def infra_resources_patch(project_id: str, resource_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "PATCH",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/resources/{resource_id}",
        request,
    )


@app.delete("/api/v1/projects/{project_id}/infra/resources/{resource_id}")
async def infra_resources_delete(project_id: str, resource_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    headers: dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    _append_activity_headers(request, headers)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/resources/{resource_id}",
            headers=headers,
        )
    mark_request("DELETE", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/infra/prepare-stack")
async def infra_prepare_stack(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/prepare-stack",
        request,
        timeout=180.0,
    )


@app.post("/api/v1/projects/{project_id}/infra/run-stack")
async def infra_run_stack(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/run-stack",
        request,
        timeout=3900.0,
    )


@app.post("/api/v1/projects/{project_id}/infra/run-template")
async def infra_run_template(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/run-template",
        request,
        timeout=1800.0,
    )


@app.post("/api/v1/projects/{project_id}/infra/run-stack-stream")
async def infra_run_stack_stream(project_id: str, request: Request):
    """Proxy Ansible log NDJSON stream (long request)."""
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    payload = await request.json()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    _append_activity_headers(request, headers)
    url = f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/run-stack-stream"
    started = time.monotonic()

    client = httpx.AsyncClient(timeout=3900.0)
    req = client.build_request("POST", url, json=payload, headers=headers)
    response = await client.send(req, stream=True)
    if response.status_code != 200:
        body = await response.aread()
        await response.aclose()
        await client.aclose()
        mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
        return Response(
            content=body,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    async def forward():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
            mark_request("POST", request.url.path, "200", time.monotonic() - started)

    return StreamingResponse(forward(), media_type="application/x-ndjson")


@app.post("/api/v1/projects/{project_id}/infra/k8s-install-3node")
async def infra_k8s_install_3node(project_id: str, request: Request):
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/k8s-install-3node",
        request,
        timeout=3900.0,
    )


@app.post("/api/v1/projects/{project_id}/infra/k8s-install-3node-stream")
async def infra_k8s_install_3node_stream(project_id: str, request: Request):
    """NDJSON proxy: kubeadm on three SSH targets (long request)."""
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    payload = await request.json()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    _append_activity_headers(request, headers)
    url = f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/infra/k8s-install-3node-stream"
    started = time.monotonic()

    client = httpx.AsyncClient(timeout=3900.0)
    req = client.build_request("POST", url, json=payload, headers=headers)
    response = await client.send(req, stream=True)
    if response.status_code != 200:
        body = await response.aread()
        await response.aclose()
        await client.aclose()
        mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
        return Response(
            content=body,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    async def forward():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
            mark_request("POST", request.url.path, "200", time.monotonic() - started)

    return StreamingResponse(forward(), media_type="application/x-ndjson")


@app.post("/api/v1/projects/{project_id}/agent-demo/run")
async def agent_demo_run(project_id: str, request: Request):
    """Multi-agent demo: LLM narrative + create repository and CI in GitLab (PAT required in project resources)."""
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/agent-demo/run",
        request,
        timeout=300.0,
    )


@app.post("/api/v1/projects/{project_id}/agents/bootstrap-planning")
async def agents_bootstrap_planning(project_id: str, request: Request):
    """Re-run planning via agent-bootstrap (same as on project create). Optionally {\"force\": true}."""
    ctx = require_roles(request, {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPERADMIN})
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Internal-Token": AGENT_BOOTSTRAP_INTERNAL_TOKEN,
    }
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=130.0) as client:
        response = await client.post(
            f"{AGENT_BOOTSTRAP_SERVICE_URL}/api/v1/internal/bootstrap/planning/{project_id}",
            json=payload,
            headers=headers,
        )
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/agent-graph/invoke")
async def agent_graph_invoke(project_id: str, request: Request):
    """LangGraph ReAct agent with tool calling (platform tools + sandbox)."""
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    payload = await request.json()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=1800.0) as client:
        response = await client.post(
            f"{AGENT_GRAPH_SERVICE_URL}/api/v1/projects/{project_id}/invoke",
            json=payload,
            headers=headers,
        )
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/agent-orchestrate/start")
async def agent_orchestrate_start(project_id: str, request: Request):
    """Background role orchestration (PM → Frontend); return run_id for status polling."""
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    payload = await request.json()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{AGENT_GRAPH_SERVICE_URL}/api/v1/projects/{project_id}/orchestrate/start",
            json=payload,
            headers=headers,
        )
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/agent-orchestrate/runs/{run_id}")
async def agent_orchestrate_run_status(project_id: str, run_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    auth = request.headers.get("Authorization")
    h: dict[str, str] = {}
    if auth:
        h["Authorization"] = auth
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{AGENT_GRAPH_SERVICE_URL}/api/v1/projects/{project_id}/orchestrate/runs/{run_id}",
            headers=h,
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/agent-orchestrate/runs/{run_id}/continue")
async def agent_orchestrate_continue(project_id: str, run_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    payload = await request.json()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{AGENT_GRAPH_SERVICE_URL}/api/v1/projects/{project_id}/orchestrate/runs/{run_id}/continue",
            json=payload,
            headers=headers,
        )
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/agent-orchestrate/runs/{run_id}/stream")
async def agent_orchestrate_run_stream(project_id: str, run_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    auth = request.headers.get("Authorization")
    headers: dict[str, str] = {}
    if auth:
        headers["Authorization"] = auth
    url = f"{AGENT_GRAPH_SERVICE_URL}/api/v1/projects/{project_id}/orchestrate/runs/{run_id}/stream"

    client = httpx.AsyncClient(timeout=14400.0)
    req = client.build_request("GET", url, headers=headers)
    response = await client.send(req, stream=True)
    if response.status_code != 200:
        body = await response.aread()
        await response.aclose()
        await client.aclose()
        mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
        return Response(
            content=body,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    async def forward():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
            mark_request("GET", request.url.path, "200", time.monotonic() - started)

    return StreamingResponse(forward(), media_type="application/x-ndjson")


@app.websocket("/api/v1/projects/{project_id}/agent-orchestrate/runs/{run_id}/ws")
async def agent_orchestrate_run_ws(project_id: str, run_id: str, websocket: WebSocket):
    """Realtime orchestration: first frame is a snapshot (after page reload), then deltas."""
    started = time.monotonic()
    path = f"/api/v1/projects/{project_id}/agent-orchestrate/runs/{run_id}/ws"
    token = (websocket.query_params.get("access_token") or websocket.query_params.get("token") or "").strip()
    if not token:
        logger.warning("orchestration ws: no token project_id=%s run_id=%s", project_id, run_id)
        await websocket.close(code=1008)
        mark_request("WS", path, 1008, time.monotonic() - started)
        return
    try:
        ctx = decode_auth_context(token)
    except HTTPException as exc:
        logger.warning("orchestration ws: jwt invalid project_id=%s run_id=%s detail=%s", project_id, run_id, exc.detail)
        await websocket.close(code=1008)
        mark_request("WS", path, 1008, time.monotonic() - started)
        return
    if not (ctx.roles & USER_ROLES):
        logger.warning("orchestration ws: forbidden role project_id=%s run_id=%s roles=%s", project_id, run_id, ctx.roles)
        await websocket.close(code=1008)
        mark_request("WS", path, 1008, time.monotonic() - started)
        return
    try:
        await assert_project_tenant_access(ctx, project_id)
    except HTTPException as exc:
        logger.warning(
            "orchestration ws: tenant access denied project_id=%s run_id=%s detail=%s",
            project_id,
            run_id,
            exc.detail,
        )
        await websocket.close(code=1008)
        mark_request("WS", path, 1008, time.monotonic() - started)
        return

    await websocket.accept()
    upstream_url = f"{_agent_graph_ws_base_url()}/api/v1/projects/{project_id}/orchestrate/runs/{run_id}/stream/ws"
    close_status = 101
    try:
        logger.info("orchestration ws: client accepted, upstream=%s", upstream_url)
        async with websockets.connect(upstream_url, max_size=None, ping_interval=20, ping_timeout=120) as uws:
            frames = 0
            async for raw in uws:
                frames += 1
                await websocket.send_text(raw)
            logger.info(
                "orchestration ws: upstream ended normally project_id=%s run_id=%s frames=%s",
                project_id,
                run_id,
                frames,
            )
    except WebSocketDisconnect:
        logger.info("orchestration ws: browser disconnected project_id=%s run_id=%s", project_id, run_id)
        close_status = 1000
    except Exception:
        logger.exception(
            "orchestration ws: upstream/proxy failed project_id=%s run_id=%s upstream=%s",
            project_id,
            run_id,
            upstream_url,
        )
        close_status = 1011
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        mark_request("WS", path, close_status, time.monotonic() - started)


@app.get("/api/v1/projects/{project_id}/ai-team/state")
async def ai_team_state(project_id: str, request: Request):
    """Latest AI Team run: steps, architecture, summary (Postgres via agent-graph)."""
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{AGENT_GRAPH_SERVICE_URL}/api/v1/projects/{project_id}/ai-team/state")
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/ai-team/runs/live")
async def project_ai_team_runs_live(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    params = dict(request.query_params)
    params["project_id"] = project_id
    suffix = f"?{urlencode(params)}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/admin/ai-team/runs/live{suffix}",
            headers={"X-Superadmin-Token": SUPERADMIN_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/documents")
async def documents_list(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents", request)


@app.post("/api/v1/projects/{project_id}/documents")
async def documents_create(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json("POST", f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents", request)


@app.get("/api/v1/projects/{project_id}/documents/{doc_id}")
async def documents_get(project_id: str, doc_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/{doc_id}", request)


@app.patch("/api/v1/projects/{project_id}/documents/{doc_id}")
async def documents_patch(project_id: str, doc_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "PATCH",
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/{doc_id}",
        request,
    )


@app.delete("/api/v1/projects/{project_id}/documents/{doc_id}")
async def documents_delete(project_id: str, doc_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    hard = request.query_params.get("hard", "0")
    params = {"hard": hard}
    async with httpx.AsyncClient(timeout=40.0) as client:
        response = await client.delete(
            f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/{doc_id}",
            params=params,
        )
    mark_request("DELETE", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/documents/{doc_id}/versions")
async def documents_versions_list(project_id: str, doc_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/{doc_id}/versions",
        request,
    )


@app.post("/api/v1/projects/{project_id}/documents/{doc_id}/versions")
async def documents_versions_create(project_id: str, doc_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/{doc_id}/versions",
        request,
        timeout=90.0,
    )


@app.get("/api/v1/projects/{project_id}/documents/{doc_id}/versions/{version}")
async def documents_version_get(project_id: str, doc_id: str, version: int, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/{doc_id}/versions/{version}",
        request,
    )


@app.post("/api/v1/projects/{project_id}/documents/{doc_id}/restore/{version}")
async def documents_restore(project_id: str, doc_id: str, version: int, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/{doc_id}/restore/{version}",
        )
    mark_request("POST", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/documents/search")
async def documents_search(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/search",
        request,
        timeout=90.0,
    )


@app.post("/api/v1/projects/{project_id}/documents/reindex")
async def documents_reindex(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{DOCUMENTS_SERVICE_URL}/api/v1/projects/{project_id}/documents/reindex",
        request,
        timeout=180.0,
    )


@app.get("/api/v1/admin/knowledge/bases")
async def admin_knowledge_bases_list(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    params = dict(request.query_params)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases",
            headers=_knowledge_internal_headers(),
            params=params or None,
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/admin/knowledge/bases")
async def admin_knowledge_bases_create(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "POST",
        f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases",
        request,
        extra_headers=_knowledge_internal_headers(),
    )


@app.get("/api/v1/admin/knowledge/bases/{kb_id}/documents")
async def admin_knowledge_documents_list(kb_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases/{kb_id}/documents",
            headers=_knowledge_internal_headers(),
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/admin/knowledge/bases/{kb_id}/documents")
async def admin_knowledge_document_upload(kb_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="file is required")
    data = await upload.read()
    filename = (getattr(upload, "filename", None) or "upload.bin").strip() or "upload.bin"
    content_type = getattr(upload, "content_type", None) or "application/octet-stream"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases/{kb_id}/documents",
                headers=_knowledge_internal_headers(),
                files={"file": (filename, data, content_type)},
            )
    except httpx.HTTPError as exc:
        logger.exception("knowledge document upload proxy failed kb=%s", kb_id)
        raise HTTPException(
            status_code=502,
            detail=f"knowledge-service unreachable: {exc}",
        ) from exc
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/admin/knowledge/search")
async def admin_knowledge_search(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "POST",
        f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/search",
        request,
        extra_headers=_knowledge_internal_headers(),
        timeout=90.0,
    )


@app.delete("/api/v1/admin/knowledge/bases/{kb_id}")
async def admin_knowledge_base_delete(kb_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.delete(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases/{kb_id}",
            headers=_knowledge_internal_headers(),
        )
    mark_request("DELETE", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.delete("/api/v1/admin/knowledge/bases/{kb_id}/documents/{doc_id}")
async def admin_knowledge_document_delete(kb_id: str, doc_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.delete(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases/{kb_id}/documents/{doc_id}",
            headers=_knowledge_internal_headers(),
        )
    mark_request("DELETE", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.patch("/api/v1/admin/knowledge/bases/{kb_id}/documents/{doc_id}")
async def admin_knowledge_document_patch(kb_id: str, doc_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PATCH",
        f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases/{kb_id}/documents/{doc_id}",
        request,
        extra_headers=_knowledge_internal_headers(),
        timeout=180.0,
    )


@app.get("/api/v1/projects/{project_id}/knowledge/bases")
async def project_knowledge_bases_list(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    params = dict(request.query_params)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/knowledge/bases",
            headers=_knowledge_internal_headers(),
            params=params or None,
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/knowledge/bases")
async def project_knowledge_bases_create(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/knowledge/bases",
        request,
        extra_headers=_knowledge_internal_headers(),
    )


@app.get("/api/v1/projects/{project_id}/knowledge/bases/{kb_id}/documents")
async def project_knowledge_documents_list(project_id: str, kb_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/knowledge/bases/{kb_id}/documents",
            headers=_knowledge_internal_headers(),
            params={"project_id": project_id},
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/knowledge/bases/{kb_id}/documents")
async def project_knowledge_document_upload(project_id: str, kb_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="file is required")
    data = await upload.read()
    filename = (getattr(upload, "filename", None) or "upload.bin").strip() or "upload.bin"
    content_type = getattr(upload, "content_type", None) or "application/octet-stream"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}/documents",
                headers=_knowledge_internal_headers(),
                files={"file": (filename, data, content_type)},
            )
    except httpx.HTTPError as exc:
        logger.exception("knowledge document upload proxy failed project=%s kb=%s", project_id, kb_id)
        raise HTTPException(
            status_code=502,
            detail=f"knowledge-service unreachable: {exc}",
        ) from exc
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/knowledge/search")
async def project_knowledge_search(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/knowledge/search",
        request,
        extra_headers=_knowledge_internal_headers(),
        timeout=90.0,
    )


@app.delete("/api/v1/projects/{project_id}/knowledge/bases/{kb_id}")
async def project_knowledge_base_delete(project_id: str, kb_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.delete(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}",
            headers=_knowledge_internal_headers(),
        )
    mark_request("DELETE", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.delete("/api/v1/projects/{project_id}/knowledge/bases/{kb_id}/documents/{doc_id}")
async def project_knowledge_document_delete(project_id: str, kb_id: str, doc_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.delete(
            f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}/documents/{doc_id}",
            headers=_knowledge_internal_headers(),
        )
    mark_request("DELETE", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.patch("/api/v1/projects/{project_id}/knowledge/bases/{kb_id}/documents/{doc_id}")
async def project_knowledge_document_patch(
    project_id: str, kb_id: str, doc_id: str, request: Request
):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "PATCH",
        f"{KNOWLEDGE_SERVICE_URL.rstrip('/')}/api/v1/internal/projects/{project_id}/knowledge/bases/{kb_id}/documents/{doc_id}",
        request,
        extra_headers=_knowledge_internal_headers(),
        timeout=180.0,
    )


@app.get("/api/v1/projects/{project_id}/tracker/items")
async def tracker_items_list(project_id: str, request: Request):
    """Agent task tracker (epics/tasks) in the project."""
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    kind = request.query_params.get("kind")
    params: dict[str, str] = {}
    if kind:
        params["kind"] = kind
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/internal/projects/{project_id}/tracker/items",
            headers=_admin_tracker_headers(),
            params=params or None,
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/tracker/items")
async def tracker_items_create(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    payload = await request.json()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{ADMIN_SERVICE_URL}/api/v1/internal/projects/{project_id}/tracker/items",
            json=payload,
            headers=_admin_tracker_headers(),
        )
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/tracker/items/{item_id}")
async def tracker_item_get(project_id: str, item_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/internal/projects/{project_id}/tracker/items/{item_id}",
            headers=_admin_tracker_headers(),
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.patch("/api/v1/projects/{project_id}/tracker/items/{item_id}")
async def tracker_item_patch(project_id: str, item_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    payload = await request.json()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.patch(
            f"{ADMIN_SERVICE_URL}/api/v1/internal/projects/{project_id}/tracker/items/{item_id}",
            json=payload,
            headers=_admin_tracker_headers(),
        )
    mark_request("PATCH", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/tracker/items/{item_id}/events")
async def tracker_item_events(project_id: str, item_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    limit = request.query_params.get("limit", "100")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ADMIN_SERVICE_URL}/api/v1/internal/projects/{project_id}/tracker/items/{item_id}/events",
            headers=_admin_tracker_headers(),
            params={"limit": limit},
        )
    mark_request("GET", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/tracker/items/{item_id}/dispatch")
async def tracker_item_dispatch(project_id: str, item_id: str, request: Request):
    """Enqueue an agent task (Kafka → agent_worker)."""
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{ADMIN_SERVICE_URL}/api/v1/internal/projects/{project_id}/tracker/items/{item_id}/dispatch",
            headers=_admin_tracker_headers(),
        )
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/projects/{project_id}/sandbox/python")
async def sandbox_python(project_id: str, request: Request):
    """Python sandbox: run code in an isolated temp directory (internal service)."""
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    payload = await request.json()
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Sandbox-Token": SANDBOX_SERVICE_TOKEN,
    }
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            f"{SANDBOX_SERVICE_URL}/api/v1/sandbox/python",
            json=payload,
            headers=headers,
        )
    mark_request("POST", request.url.path, str(response.status_code), time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/development-context")
async def development_context(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/development-context", request)


@app.get("/api/v1/projects/{project_id}/k8s/clusters")
async def k8s_clusters_list(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/k8s/clusters", request)


@app.get("/api/v1/projects/{project_id}/k8s/operations")
async def k8s_operations_list(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/k8s/operations", request)


@app.post("/api/v1/projects/{project_id}/k8s/query")
async def k8s_query_gateway(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    await assert_project_mutable(ctx, project_id)
    return await proxy_json(
        "POST",
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/k8s/query",
        request,
        timeout=180.0,
    )


@app.get("/api/v1/projects/{project_id}/activity-logs")
async def project_activity_logs(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    # Base URL only: _proxy_get already adds request.url.query (otherwise duplicate ?limit=… → 422).
    return await _proxy_get(
        f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/activity-logs",
        request,
    )


@app.get("/api/v1/projects/{project_id}/agent/metrics")
async def list_agent_metrics(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    return await _proxy_get(f"{ADAPTER_SERVICE_URL}/api/v1/projects/{project_id}/agent/metrics", request)


@app.get("/api/v1/projects/{project_id}/browser/logs")
async def browser_logs(project_id: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    run_id = request.query_params.get("run_id", "").strip()
    limit = request.query_params.get("limit", "60").strip() or "60"
    params: dict[str, str] = {"project_id": project_id, "limit": limit}
    if run_id:
        params["run_id"] = run_id
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{BROWSER_TOOL_SERVICE_URL}/api/v1/internal/browser/logs",
            params=params,
            headers={"X-Internal-Token": INTERNAL_LLM_SETTINGS_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.get("/api/v1/projects/{project_id}/browser/artifacts/{artifact_name}")
async def browser_artifact(project_id: str, artifact_name: str, request: Request):
    ctx = require_roles(request, USER_ROLES)
    await assert_project_tenant_access(ctx, project_id)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{BROWSER_TOOL_SERVICE_URL}/api/v1/internal/browser/artifacts/{artifact_name}",
            headers={"X-Internal-Token": INTERNAL_LLM_SETTINGS_TOKEN},
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    if response.status_code != 200:
        return _response_from_httpx(response)
    return Response(
        content=response.content,
        status_code=200,
        media_type=response.headers.get("content-type", "application/octet-stream"),
    )


@app.post("/api/v1/agent/ingest")
async def agent_ingest_public(request: Request):
    """Public endpoint for the on-server agent (auth — Bearer agent token)."""
    return await proxy_json("POST", f"{ADAPTER_SERVICE_URL}/api/v1/agent/ingest", request)


_ANSIBLE_ADMIN = {"X-Superadmin-Token": SUPERADMIN_TOKEN}


@app.get("/api/v1/admin/ansible/swarm/templates")
async def admin_ansible_templates_list(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/admin/ansible/swarm/templates",
            headers=_ANSIBLE_ADMIN,
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.post("/api/v1/admin/ansible/swarm/templates")
async def admin_ansible_templates_create(request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "POST",
        f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/admin/ansible/swarm/templates",
        request,
        extra_headers=_ANSIBLE_ADMIN,
    )


@app.get("/api/v1/admin/ansible/swarm/templates/{template_id}")
async def admin_ansible_template_get(template_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/admin/ansible/swarm/templates/{template_id}",
            headers=_ANSIBLE_ADMIN,
        )
    mark_request("GET", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.put("/api/v1/admin/ansible/swarm/templates/{template_id}")
async def admin_ansible_template_put(template_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PUT",
        f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/admin/ansible/swarm/templates/{template_id}",
        request,
        extra_headers=_ANSIBLE_ADMIN,
    )


@app.delete("/api/v1/admin/ansible/swarm/templates/{template_id}")
async def admin_ansible_template_delete(template_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/admin/ansible/swarm/templates/{template_id}",
            headers=_ANSIBLE_ADMIN,
        )
    mark_request("DELETE", request.url.path, response.status_code, time.monotonic() - started)
    return _response_from_httpx(response)


@app.patch("/api/v1/admin/ansible/swarm/templates/{template_id}/active")
async def admin_ansible_template_active(template_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "PATCH",
        f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/admin/ansible/swarm/templates/{template_id}/active",
        request,
        extra_headers=_ANSIBLE_ADMIN,
    )


@app.post("/api/v1/admin/ansible/swarm/templates/{template_id}/preview")
async def admin_ansible_template_preview(template_id: str, request: Request):
    require_roles(request, {ROLE_SUPERADMIN})
    return await proxy_json(
        "POST",
        f"{ANSIBLE_ORCHESTRATOR_SERVICE_URL}/api/v1/admin/ansible/swarm/templates/{template_id}/preview",
        request,
        extra_headers=_ANSIBLE_ADMIN,
    )
