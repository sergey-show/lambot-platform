import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID, uuid4

import bcrypt
import httpx
import jwt
from aiokafka import AIOKafkaConsumer
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field

from services.admin_service.db import SessionLocal, get_db, init_db
from services.admin_service.ai_team_service import (
    append_execution_tasks,
    create_run_with_steps,
    get_latest_run_for_project,
    get_run_by_id,
    replace_execution_tasks,
    serialize_run_public,
    update_execution_task,
    update_run_fields,
    update_step,
)
from services.admin_service import tracker_service as tracker_svc
from services.admin_service.models import (
    AGENT_SETTINGS_SINGLETON_ID,
    AITeamRun,
    LLM_SETTINGS_SINGLETON_ID,
    AdminProjectProjection,
    AgentDevelopmentPrompt,
    AgentGraphBuiltinTool,
    AgentGraphHttpTool,
    AgentPlatformSettings,
    InboxMessage,
    LlmAdapterSettings,
    PlatformUser,
)
from services.admin_service import settings_crypto
from shared.contracts.events import EventTypes
from shared.kafka import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC_EVENTS

consumer: AIOKafkaConsumer | None = None
consumer_task: asyncio.Task | None = None


def require_superadmin(x_superadmin_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("SUPERADMIN_TOKEN", "superadmin-dev-token")
    if x_superadmin_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def require_internal_llm_token(x_internal_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
    if x_internal_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


JWT_SECRET = os.getenv("JWT_SECRET", "dev-jwt-secret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_SECONDS = int(os.getenv("JWT_EXPIRE_SECONDS", str(7 * 24 * 3600)))


def _hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def _verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


class LoginRequest(BaseModel):
    email: str
    password: str


class UserCreateBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    display_name: str | None = None
    roles: list[str] | None = None


class UserPublic(BaseModel):
    id: str
    email: str
    display_name: str
    tenant_id: str
    roles: list[str]
    created_at: str


class LlmSettingsPublicResponse(BaseModel):
    openai_base_url: str
    default_model: str
    coding_model: str | None = Field(
        default=None,
        description="Модель для кода/агентов; пусто в UI = использовать модель по умолчанию",
    )
    embedding_model: str | None = Field(
        default=None,
        description="Модель для эмбеддингов; пусто = использовать env/дефолт на стороне adapter",
    )
    has_api_key: bool
    api_key_hint: str | None = Field(
        default=None, description="Последние символы ключа, если задан"
    )


class LlmSettingsUpdateBody(BaseModel):
    openai_base_url: str | None = None
    default_model: str | None = None
    coding_model: str | None = Field(
        default=None,
        description="Пустая строка — сбросить (как модель по умолчанию)",
    )
    embedding_model: str | None = Field(
        default=None,
        description="Пустая строка — сбросить",
    )
    api_key: str | None = Field(
        default=None,
        description="Новый ключ; пустая строка = не менять",
    )


class LlmSettingsInternalResponse(BaseModel):
    openai_base_url: str
    default_model: str
    coding_model: str | None = None
    embedding_model: str | None = None
    api_key: str | None = None


class AgentSettingsPublicResponse(BaseModel):
    ingest_public_url: str


class AgentSettingsUpdateBody(BaseModel):
    ingest_public_url: str = Field(..., min_length=8)


class AgentSettingsInternalResponse(BaseModel):
    ingest_public_url: str


class AgentDevelopmentPromptsPublicResponse(BaseModel):
    prompts: dict[str, str] = Field(
        description="Ключ — роль: sa, devops, backend, frontend, qa, architect, pm; значение — системный промпт"
    )


class AgentDevelopmentPromptsUpdateBody(BaseModel):
    prompts: dict[str, str] = Field(
        default_factory=dict,
        description="Полные тексты промптов по ролям; можно передать только изменённые ключи",
    )


class AgentGraphBuiltinUpdateBody(BaseModel):
    tools: dict[str, bool] = Field(
        default_factory=dict,
        description="Ключ — имя встроенного tool (get_development_context, …), значение — включён",
    )


class AgentGraphHttpToolCreateBody(BaseModel):
    tool_name: str = Field(..., min_length=2, max_length=64)
    description: str = Field(..., min_length=4, max_length=4000)
    url_template: str = Field(
        ...,
        description="GET URL с плейсхолдером {project_id}, только adapter-service / project-service",
    )
    enabled: bool = True


class AgentGraphHttpToolPatchBody(BaseModel):
    tool_name: str | None = Field(default=None, min_length=2, max_length=64)
    description: str | None = Field(default=None, min_length=4, max_length=4000)
    url_template: str | None = None
    enabled: bool | None = None


_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_ALLOWED_HTTP_HOSTS = frozenset({"adapter-service", "project-service"})


def _validate_agent_http_url_template(template: str) -> None:
    if "{project_id}" not in template:
        raise HTTPException(status_code=400, detail="url_template должен содержать {project_id}")
    sample = "00000000-0000-0000-0000-000000000001"
    try:
        url = template.format(project_id=sample)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"шаблон URL: {exc}") from exc
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="только http/https")
    host = (p.hostname or "").lower()
    if host not in _ALLOWED_HTTP_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=f"разрешены только хосты: {', '.join(sorted(_ALLOWED_HTTP_HOSTS))}",
        )
    if not (p.path or "").startswith("/api/v1/projects/"):
        raise HTTPException(status_code=400, detail="путь должен начинаться с /api/v1/projects/")


def _get_agent_row(db: Session) -> AgentPlatformSettings:
    default_ingest = os.getenv(
        "DEFAULT_AGENT_INGEST_PUBLIC_URL",
        "http://localhost:8000/api/v1/agent/ingest",
    ).strip()
    row = db.get(AgentPlatformSettings, AGENT_SETTINGS_SINGLETON_ID)
    if row is None:
        row = AgentPlatformSettings(
            id=AGENT_SETTINGS_SINGLETON_ID,
            ingest_public_url=default_ingest,
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _get_llm_row(db: Session) -> LlmAdapterSettings:
    row = db.get(LlmAdapterSettings, LLM_SETTINGS_SINGLETON_ID)
    if row is None:
        cm = os.getenv("DEFAULT_OPENAI_CODING_MODEL", "").strip() or None
        em = os.getenv("DEFAULT_OPENAI_EMBEDDING_MODEL", "").strip() or None
        row = LlmAdapterSettings(
            id=LLM_SETTINGS_SINGLETON_ID,
            openai_base_url=os.getenv("DEFAULT_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            default_model=os.getenv("DEFAULT_OPENAI_MODEL", "gpt-4o-mini"),
            coding_model=cm,
            embedding_model=em,
            api_key_encrypted=None,
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _mask_key_hint(plain: str | None) -> str | None:
    if not plain or len(plain) < 4:
        return None
    return f"…{plain[-4:]}"


def _parse_deleted_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def process_projection_event(event: dict) -> None:
    if "event_id" not in event:
        return
    event_type = event.get("event_type")
    if event_type not in (
        EventTypes.PROJECT_CREATED,
        EventTypes.PROJECT_STATUS_CHANGED,
        EventTypes.PROJECT_UPDATED,
    ):
        return
    project_id = event.get("project_id")
    tenant_id = event.get("tenant_id")
    payload = event.get("payload", {})
    if not project_id:
        return

    db = SessionLocal()
    try:
        stmt = (
            insert(InboxMessage)
            .values(
                event_id=UUID(event["event_id"]),
                payload=json.dumps(event),
            )
            .on_conflict_do_nothing()
        )
        result = db.execute(stmt)
        if result.rowcount == 0:
            db.rollback()
            return

        row = db.execute(
            select(AdminProjectProjection).where(
                AdminProjectProjection.project_id == UUID(project_id)
            )
        ).scalar_one_or_none()

        if event_type == EventTypes.PROJECT_CREATED:
            if row is None:
                row = AdminProjectProjection(
                    project_id=UUID(project_id),
                    name=payload.get("name", "unknown"),
                    description=payload.get("description", ""),
                    tenant_id=UUID(tenant_id),
                    status=payload.get("status", "IDEA"),
                    suspended=False,
                    deleted_at=None,
                    updated_at=datetime.utcnow(),
                )
                db.add(row)
            else:
                row.name = payload.get("name", row.name)
                row.description = payload.get("description", row.description)
                row.status = payload.get("status", row.status)
                row.updated_at = datetime.utcnow()
        elif event_type == EventTypes.PROJECT_STATUS_CHANGED and row is not None:
            row.status = payload.get("status", row.status)
            row.updated_at = datetime.utcnow()
        elif event_type == EventTypes.PROJECT_UPDATED:
            if row is None:
                row = AdminProjectProjection(
                    project_id=UUID(project_id),
                    name=payload.get("name", "unknown"),
                    description=payload.get("description", ""),
                    tenant_id=UUID(tenant_id),
                    status=payload.get("status", "IDEA"),
                    suspended=bool(payload.get("suspended", False)),
                    deleted_at=_parse_deleted_at(payload.get("deleted_at")),
                    updated_at=datetime.utcnow(),
                )
                db.add(row)
            else:
                if "name" in payload:
                    row.name = payload["name"]
                if "description" in payload:
                    row.description = payload["description"]
                if "status" in payload:
                    row.status = payload["status"]
                if "suspended" in payload:
                    row.suspended = bool(payload["suspended"])
                if "deleted_at" in payload:
                    row.deleted_at = _parse_deleted_at(payload.get("deleted_at"))
                row.updated_at = datetime.utcnow()

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def consume_projection_events() -> None:
    if not consumer:
        return
    async for msg in consumer:
        try:
            event = json.loads(msg.value.decode("utf-8"))
        except Exception:
            continue
        await asyncio.to_thread(process_projection_event, event)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global consumer, consumer_task
    init_db()
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC_EVENTS,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="admin-service",
        auto_offset_reset="earliest",
    )
    try:
        await consumer.start()
        consumer_task = asyncio.create_task(consume_projection_events())
    except Exception:
        consumer = None
        consumer_task = None
    yield
    if consumer_task:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
    if consumer:
        await consumer.stop()


app = FastAPI(title="admin-service", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/admin/projects")
def admin_list_projects(
    status: str | None = None,
    tenant_id: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(AdminProjectProjection)
    if status:
        query = query.where(AdminProjectProjection.status == status)
    if tenant_id:
        query = query.where(AdminProjectProjection.tenant_id == UUID(tenant_id))
    if search:
        query = query.where(AdminProjectProjection.name.ilike(f"%{search}%"))
    rows = db.execute(
        query.order_by(AdminProjectProjection.updated_at.desc()).limit(min(limit, 200)).offset(offset)
    ).scalars().all()
    return [
        {
            "id": str(row.project_id),
            "name": row.name,
            "description": row.description or "",
            "tenant_id": str(row.tenant_id),
            "status": row.status,
            "suspended": row.suspended,
            "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            "created_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]


@app.get("/api/v1/admin/overview")
def admin_overview(_: None = Depends(require_superadmin), db: Session = Depends(get_db)) -> dict:
    active = AdminProjectProjection.deleted_at.is_(None)
    total = db.execute(select(func.count(AdminProjectProjection.id)).where(active)).scalar_one()
    by_status_rows = db.execute(
        select(AdminProjectProjection.status, func.count(AdminProjectProjection.id))
        .where(active)
        .group_by(AdminProjectProjection.status)
    ).all()
    by_status = {status: count for status, count in by_status_rows}
    return {"projects_total": total, "projects_by_status": by_status}


@app.get("/api/v1/admin/services/health")
def admin_services_health(_: None = Depends(require_superadmin)) -> dict:
    service_urls = {
        "project-service": os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001"),
        "planning-service": os.getenv("PLANNING_SERVICE_URL", "http://planning-service:8002"),
        "orchestrator-service": os.getenv(
            "ORCHESTRATOR_SERVICE_URL", "http://orchestrator-service:8003"
        ),
        "catalog-service": os.getenv("CATALOG_SERVICE_URL", "http://catalog-service:8004"),
        "llm-adapter-service": os.getenv("LLM_ADAPTER_SERVICE_URL", "http://llm-adapter-service:8005"),
        "ansible-orchestrator-service": os.getenv(
            "ANSIBLE_ORCHESTRATOR_SERVICE_URL", "http://ansible-orchestrator-service:8007"
        ),
        "adapter-service": os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008"),
        "agent-worker-service": os.getenv("AGENT_WORKER_SERVICE_URL", "http://agent-worker-service:8010"),
        "agent-bootstrap-service": os.getenv(
            "AGENT_BOOTSTRAP_SERVICE_URL", "http://agent-bootstrap-service:8011"
        ),
        "sandbox-service": os.getenv("SANDBOX_SERVICE_URL", "http://sandbox-service:8012"),
        "agent-graph-service": os.getenv(
            "AGENT_GRAPH_SERVICE_URL", "http://agent-graph-service:8013"
        ),
        "browser-tool-service": os.getenv("BROWSER_TOOL_SERVICE_URL", "http://browser-tool-service:8014"),
        "documents-service": os.getenv("DOCUMENTS_SERVICE_URL", "http://documents-service:8015"),
        "api-gateway": os.getenv("API_GATEWAY_URL", "http://api-gateway:8000"),
    }
    results: dict[str, str] = {}
    with httpx.Client(timeout=5.0) as client:
        for name, base_url in service_urls.items():
            try:
                resp = client.get(f"{base_url}/health")
                results[name] = "ok" if resp.status_code == 200 else f"error:{resp.status_code}"
            except Exception:
                results[name] = "unreachable"
    return {"services": results}


@app.get("/api/v1/admin/ai-team/runs/live")
def admin_ai_team_runs_live(
    limit: int = 50,
    status: str | None = None,
    project_id: UUID | None = None,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict:
    lim = max(1, min(limit, 200))
    q = select(AITeamRun).order_by(AITeamRun.updated_at.desc()).limit(lim)
    if status:
        q = q.where(AITeamRun.status == status)
    if project_id:
        q = q.where(AITeamRun.project_id == project_id)
    runs = db.execute(q).scalars().all()
    project_ids = [r.project_id for r in runs]
    proj_name_by_id: dict[str, str] = {}
    if project_ids:
        rows = db.execute(
            select(AdminProjectProjection.project_id, AdminProjectProjection.name).where(
                AdminProjectProjection.project_id.in_(project_ids)
            )
        ).all()
        proj_name_by_id = {str(pid): str(name) for pid, name in rows}
    items: list[dict[str, object]] = []
    for r in runs:
        phase = str(r.phase or "")
        iteration = 0
        batch_status = "idle"
        m = re.search(r"(planning|execution_dispatch)_batch_(\d+)", phase)
        if m:
            iteration = int(m.group(2))
            batch_status = "planning" if m.group(1) == "planning" else "dispatching"
        elif phase.startswith("execution_done"):
            batch_status = "done"
        elif phase.startswith("execution_mixed"):
            batch_status = "mixed"
        elif phase.startswith("failed"):
            batch_status = "failed"
        elif phase.startswith("awaiting_clarification"):
            batch_status = "paused"
        items.append(
            {
                "run_id": str(r.id),
                "project_id": str(r.project_id),
                "project_name": proj_name_by_id.get(str(r.project_id), ""),
                "status": r.status,
                "phase": r.phase,
                "iteration": iteration,
                "batch_status": batch_status,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "error": r.error,
            }
        )
    return {"runs": items}


@app.get("/api/v1/admin/llm-settings", response_model=LlmSettingsPublicResponse)
def admin_get_llm_settings(
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> LlmSettingsPublicResponse:
    row = _get_llm_row(db)
    plain_key: str | None = None
    if row.api_key_encrypted:
        try:
            plain_key = settings_crypto.decrypt_secret(row.api_key_encrypted)
        except Exception:
            plain_key = None
    return LlmSettingsPublicResponse(
        openai_base_url=row.openai_base_url,
        default_model=row.default_model,
        coding_model=row.coding_model,
        embedding_model=row.embedding_model,
        has_api_key=bool(plain_key),
        api_key_hint=_mask_key_hint(plain_key),
    )


@app.put("/api/v1/admin/llm-settings")
def admin_put_llm_settings(
    body: LlmSettingsUpdateBody,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    row = _get_llm_row(db)
    if body.openai_base_url is not None and body.openai_base_url.strip():
        row.openai_base_url = body.openai_base_url.strip()
    if body.default_model is not None and body.default_model.strip():
        row.default_model = body.default_model.strip()
    if body.coding_model is not None:
        row.coding_model = body.coding_model.strip() or None
    if body.embedding_model is not None:
        row.embedding_model = body.embedding_model.strip() or None
    if body.api_key is not None and body.api_key.strip() != "":
        row.api_key_encrypted = settings_crypto.encrypt_secret(body.api_key.strip())
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@app.get("/api/v1/internal/llm-settings", response_model=LlmSettingsInternalResponse)
def internal_get_llm_settings(
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> LlmSettingsInternalResponse:
    """Только для сервисов внутри сети (llm-adapter-service)."""
    row = _get_llm_row(db)
    api_key: str | None = None
    if row.api_key_encrypted:
        try:
            api_key = settings_crypto.decrypt_secret(row.api_key_encrypted)
        except Exception:
            api_key = None
    return LlmSettingsInternalResponse(
        openai_base_url=row.openai_base_url,
        default_model=row.default_model,
        coding_model=row.coding_model,
        embedding_model=row.embedding_model,
        api_key=api_key,
    )


@app.get("/api/v1/admin/agent-settings", response_model=AgentSettingsPublicResponse)
def admin_get_agent_settings(
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> AgentSettingsPublicResponse:
    row = _get_agent_row(db)
    return AgentSettingsPublicResponse(ingest_public_url=row.ingest_public_url.strip())


@app.put("/api/v1/admin/agent-settings")
def admin_put_agent_settings(
    body: AgentSettingsUpdateBody,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    url = body.ingest_public_url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="ingest_public_url must start with http:// or https://",
        )
    if "/api/v1/agent/ingest" not in url:
        raise HTTPException(
            status_code=400,
            detail="URL должен указывать на endpoint .../api/v1/agent/ingest",
        )
    row = _get_agent_row(db)
    row.ingest_public_url = url
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@app.get("/api/v1/internal/agent-settings", response_model=AgentSettingsInternalResponse)
def internal_get_agent_settings(
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> AgentSettingsInternalResponse:
    """Только для сервисов внутри сети (adapter-service)."""
    row = _get_agent_row(db)
    return AgentSettingsInternalResponse(ingest_public_url=row.ingest_public_url.strip())


def _all_dev_prompts(db: Session) -> dict[str, str]:
    rows = db.execute(select(AgentDevelopmentPrompt)).scalars().all()
    return {r.role: r.system_prompt for r in rows}


@app.get("/api/v1/admin/agent-development-prompts", response_model=AgentDevelopmentPromptsPublicResponse)
def admin_get_agent_development_prompts(
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> AgentDevelopmentPromptsPublicResponse:
    return AgentDevelopmentPromptsPublicResponse(prompts=_all_dev_prompts(db))


@app.put("/api/v1/admin/agent-development-prompts")
def admin_put_agent_development_prompts(
    body: AgentDevelopmentPromptsUpdateBody,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    allowed = {"sa", "devops", "backend", "frontend", "qa", "architect", "pm"}
    for role, text in body.prompts.items():
        if role not in allowed:
            raise HTTPException(status_code=400, detail=f"Unknown role: {role}")
        if not text or not str(text).strip():
            raise HTTPException(status_code=400, detail=f"Empty prompt for {role}")
        row = db.get(AgentDevelopmentPrompt, role)
        if row is None:
            db.add(
                AgentDevelopmentPrompt(
                    role=role,
                    system_prompt=text.strip(),
                    updated_at=datetime.utcnow(),
                )
            )
        else:
            row.system_prompt = text.strip()
            row.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@app.get("/api/v1/internal/agent-development-prompts")
def internal_get_agent_development_prompts(
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Системные промпты для агентов разработки (adapter / worker)."""
    return _all_dev_prompts(db)


KNOWN_AGENT_GRAPH_BUILTIN_KEYS = frozenset(
    {
        "get_development_context",
        "get_project_status",
        "list_infra_resources",
        "run_product_planning",
        "get_activity_logs",
        "get_agent_metrics",
        "ssh_run_command",
        "kubernetes_query",
        "run_sandbox_python",
        "http_request",
        "browser_session",
        "ansible_run_template",
        "get_project_docs_context",
        "upsert_project_document",
        "list_project_documents",
        "gitlab_provision_status",
        "gitlab_create_repository",
        "list_project_repos",
        "repo_clone_or_sync",
        "repo_read_file",
        "repo_write_commit_push",
        "search_codebase",
        "tracker_list_items",
        "tracker_create_epic",
        "tracker_create_task",
    }
)


def _agent_graph_builtin_rows(db: Session) -> list[AgentGraphBuiltinTool]:
    return list(db.execute(select(AgentGraphBuiltinTool).order_by(AgentGraphBuiltinTool.tool_key)).scalars().all())


@app.get("/api/v1/admin/agent-graph-tools")
def admin_get_agent_graph_tools(
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict:
    builtins = _agent_graph_builtin_rows(db)
    http_rows = db.execute(select(AgentGraphHttpTool).order_by(AgentGraphHttpTool.tool_name)).scalars().all()
    return {
        "builtins": [
            {"tool_key": r.tool_key, "label": r.label, "enabled": r.enabled} for r in builtins
        ],
        "http_tools": [
            {
                "id": str(r.id),
                "tool_name": r.tool_name,
                "description": r.description,
                "url_template": r.url_template,
                "enabled": r.enabled,
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in http_rows
        ],
    }


@app.put("/api/v1/admin/agent-graph-tools/builtins")
def admin_put_agent_graph_builtins(
    body: AgentGraphBuiltinUpdateBody,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    for key, en in body.tools.items():
        if key not in KNOWN_AGENT_GRAPH_BUILTIN_KEYS:
            raise HTTPException(status_code=400, detail=f"Неизвестный встроенный tool: {key}")
        row = db.get(AgentGraphBuiltinTool, key)
        if row is None:
            db.add(
                AgentGraphBuiltinTool(
                    tool_key=key,
                    label=key,
                    enabled=bool(en),
                    updated_at=datetime.utcnow(),
                )
            )
        else:
            row.enabled = bool(en)
            row.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@app.post("/api/v1/admin/agent-graph-tools/http")
def admin_create_agent_graph_http_tool(
    body: AgentGraphHttpToolCreateBody,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict:
    name = body.tool_name.strip()
    if not _TOOL_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="tool_name: только a-z, цифры, _; начинается с буквы",
        )
    existing = db.execute(select(AgentGraphHttpTool).where(AgentGraphHttpTool.tool_name == name)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="tool_name уже занят")
    _validate_agent_http_url_template(body.url_template.strip())
    if name in KNOWN_AGENT_GRAPH_BUILTIN_KEYS:
        raise HTTPException(status_code=400, detail="имя конфликтует со встроенным tool")
    row = AgentGraphHttpTool(
        tool_name=name,
        description=body.description.strip(),
        url_template=body.url_template.strip(),
        enabled=body.enabled,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "ok", "id": str(row.id)}


@app.patch("/api/v1/admin/agent-graph-tools/http/{tool_id}")
def admin_patch_agent_graph_http_tool(
    tool_id: UUID,
    body: AgentGraphHttpToolPatchBody,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    row = db.get(AgentGraphHttpTool, tool_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if body.tool_name is not None:
        nn = body.tool_name.strip()
        if not _TOOL_NAME_RE.match(nn):
            raise HTTPException(status_code=400, detail="tool_name: неверный формат")
        if nn != row.tool_name:
            clash = db.execute(select(AgentGraphHttpTool).where(AgentGraphHttpTool.tool_name == nn)).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status_code=409, detail="tool_name занят")
        if nn in KNOWN_AGENT_GRAPH_BUILTIN_KEYS:
            raise HTTPException(status_code=400, detail="имя конфликтует со встроенным tool")
        row.tool_name = nn
    if body.description is not None:
        row.description = body.description.strip()
    if body.url_template is not None:
        _validate_agent_http_url_template(body.url_template.strip())
        row.url_template = body.url_template.strip()
    if body.enabled is not None:
        row.enabled = body.enabled
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@app.delete("/api/v1/admin/agent-graph-tools/http/{tool_id}")
def admin_delete_agent_graph_http_tool(
    tool_id: UUID,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    row = db.get(AgentGraphHttpTool, tool_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    db.delete(row)
    db.commit()
    return {"status": "ok"}


@app.get("/api/v1/internal/agent-graph-tools")
def internal_get_agent_graph_tools(
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    """Конфигурация инструментов LangGraph-агента (agent-graph-service)."""
    builtins = _agent_graph_builtin_rows(db)
    builtin_map = {r.tool_key: r.enabled for r in builtins}
    for k in KNOWN_AGENT_GRAPH_BUILTIN_KEYS:
        builtin_map.setdefault(k, True)
    http_rows = db.execute(
        select(AgentGraphHttpTool).where(AgentGraphHttpTool.enabled.is_(True))
    ).scalars().all()
    return {
        "builtin": builtin_map,
        "http_tools": [
            {
                "id": str(r.id),
                "tool_name": r.tool_name,
                "description": r.description,
                "url_template": r.url_template,
            }
            for r in http_rows
        ],
    }


class AITeamRunCreateBody(BaseModel):
    project_id: UUID
    user_message: str = Field(..., min_length=1, max_length=32000)


class AITeamRunPatchBody(BaseModel):
    status: str | None = None
    phase: str | None = None
    architecture_md: str | None = None
    architecture_json: dict | None = None
    final_report_md: str | None = None
    error: str | None = None


class AITeamStepPatchBody(BaseModel):
    status: str
    output_md: str | None = None
    structured: dict | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class AITeamExecutionTasksBody(BaseModel):
    tasks: list[dict] = Field(default_factory=list)


class AITeamExecutionTaskPatchBody(BaseModel):
    status: str | None = None
    result: dict | None = None
    error: str | None = None
    kafka_task_id: UUID | None = None
    started_at: str | None = None
    finished_at: str | None = None


@app.post("/api/v1/internal/ai-team/runs")
def internal_ai_team_create_run(
    body: AITeamRunCreateBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    rid = create_run_with_steps(db, body.project_id, body.user_message)
    run = get_run_by_id(db, rid)
    if run is None:
        raise HTTPException(status_code=500, detail="run create failed")
    return serialize_run_public(run)


@app.get("/api/v1/internal/ai-team/runs/{run_id}")
def internal_ai_team_get_run(
    run_id: UUID,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="not found")
    return serialize_run_public(run)


@app.get("/api/v1/internal/ai-team/projects/{project_id}/latest")
def internal_ai_team_latest(
    project_id: UUID,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    run = get_latest_run_for_project(db, project_id)
    if run is None:
        return {"run": None}
    return {"run": serialize_run_public(run)}


@app.patch("/api/v1/internal/ai-team/runs/{run_id}")
def internal_ai_team_patch_run(
    run_id: UUID,
    body: AITeamRunPatchBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if patch:
        update_run_fields(db, run_id, **patch)
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="not found")
    return serialize_run_public(run)


def _parse_dt_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    return datetime.fromisoformat(t)


@app.patch("/api/v1/internal/ai-team/runs/{run_id}/steps/{step_key}")
def internal_ai_team_patch_step(
    run_id: UUID,
    step_key: str,
    body: AITeamStepPatchBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    update_step(
        db,
        run_id,
        step_key,
        status=body.status,
        output_md=body.output_md,
        structured=body.structured,
        error=body.error,
        started_at=_parse_dt_iso(body.started_at),
        finished_at=_parse_dt_iso(body.finished_at),
    )
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="not found")
    return serialize_run_public(run)


@app.post("/api/v1/internal/ai-team/runs/{run_id}/execution-tasks")
def internal_ai_team_replace_execution_tasks(
    run_id: UUID,
    body: AITeamExecutionTasksBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    replace_execution_tasks(db, run_id, body.tasks)
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="not found")
    return serialize_run_public(run)


@app.post("/api/v1/internal/ai-team/runs/{run_id}/execution-tasks/append")
def internal_ai_team_append_execution_tasks(
    run_id: UUID,
    body: AITeamExecutionTasksBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    append_execution_tasks(db, run_id, body.tasks)
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="not found")
    return serialize_run_public(run)


@app.patch("/api/v1/internal/ai-team/execution-tasks/{task_id}")
def internal_ai_team_patch_execution_task(
    task_id: UUID,
    body: AITeamExecutionTaskPatchBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    patch = body.model_dump(exclude_unset=True)
    row = update_execution_task(
        db,
        task_id,
        status=patch.get("status") if "status" in patch else None,
        result=patch.get("result") if "result" in patch else None,
        error=patch.get("error") if "error" in patch else None,
        kafka_task_id=patch.get("kafka_task_id") if "kafka_task_id" in patch else None,
        started_at=_parse_dt_iso(patch["started_at"]) if "started_at" in patch else None,
        finished_at=_parse_dt_iso(patch["finished_at"]) if "finished_at" in patch else None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    run = get_run_by_id(db, row.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="not found")
    return serialize_run_public(run)


class TrackerItemCreateBody(BaseModel):
    kind: str = Field(..., pattern="^(epic|task)$")
    title: str = Field(..., min_length=1, max_length=512)
    description: str = ""
    parent_id: UUID | None = None
    assignee_role: str | None = None
    status: str | None = None
    seq: int = 0
    meta: dict | None = None


class TrackerItemPatchBody(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    assignee_role: str | None = None
    seq: int | None = None
    meta: dict | None = None


class TrackerExecutionPatchBody(BaseModel):
    execution_id: UUID
    phase: str = Field(..., pattern="^(started|finished)$")
    result_status: str | None = None
    result: dict | None = None
    error: str | None = None


@app.get("/api/v1/internal/projects/{project_id}/tracker/items")
def internal_tracker_list(
    project_id: UUID,
    kind: str | None = None,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    rows = tracker_svc.list_items(db, project_id, kind=kind)
    return {"items": [tracker_svc.serialize_item(r) for r in rows]}


@app.post("/api/v1/internal/projects/{project_id}/tracker/items")
def internal_tracker_create(
    project_id: UUID,
    body: TrackerItemCreateBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    try:
        st = body.status or tracker_svc.S_BACKLOG
        row = tracker_svc.create_item(
            db,
            project_id=project_id,
            kind=body.kind,
            title=body.title,
            description=body.description,
            parent_id=body.parent_id,
            assignee_role=body.assignee_role,
            status=st,
            seq=body.seq,
            meta=body.meta,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return tracker_svc.serialize_item(row)


@app.get("/api/v1/internal/projects/{project_id}/tracker/items/{item_id}")
def internal_tracker_get(
    project_id: UUID,
    item_id: UUID,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    row = tracker_svc.get_item(db, project_id, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return tracker_svc.serialize_item(row)


@app.patch("/api/v1/internal/projects/{project_id}/tracker/items/{item_id}")
def internal_tracker_patch(
    project_id: UUID,
    item_id: UUID,
    body: TrackerItemPatchBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    try:
        row = tracker_svc.update_item(
            db,
            project_id,
            item_id,
            title=body.title,
            description=body.description,
            status=body.status,
            assignee_role=body.assignee_role,
            seq=body.seq,
            meta=body.meta,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return tracker_svc.serialize_item(row)


@app.get("/api/v1/internal/projects/{project_id}/tracker/items/{item_id}/events")
def internal_tracker_events(
    project_id: UUID,
    item_id: UUID,
    limit: int = 100,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    row = tracker_svc.get_item(db, project_id, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    events = tracker_svc.list_events(db, project_id, item_id, limit=min(limit, 500))
    return {"events": [tracker_svc.serialize_event(e) for e in reversed(events)]}


@app.post("/api/v1/internal/projects/{project_id}/tracker/items/{item_id}/dispatch")
def internal_tracker_dispatch(
    project_id: UUID,
    item_id: UUID,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    try:
        row, execution_id = tracker_svc.dispatch_item(db, project_id, item_id)
    except ValueError as exc:
        msg = str(exc)
        if msg == "not_found":
            raise HTTPException(status_code=404, detail="not found") from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"dispatch failed: {exc}") from exc
    return {"item": tracker_svc.serialize_item(row), "execution_id": str(execution_id)}


@app.patch("/api/v1/internal/projects/{project_id}/tracker/items/{item_id}/execution")
def internal_tracker_execution_patch(
    project_id: UUID,
    item_id: UUID,
    body: TrackerExecutionPatchBody,
    _: None = Depends(require_internal_llm_token),
    db: Session = Depends(get_db),
) -> dict:
    if body.phase == "started":
        row = tracker_svc.mark_running(db, project_id, item_id, body.execution_id)
    else:
        if body.result_status not in ("succeeded", "failed"):
            raise HTTPException(
                status_code=400,
                detail="result_status must be succeeded or failed when phase is finished",
            )
        rs = body.result_status
        row = tracker_svc.patch_execution_result(
            db,
            project_id,
            item_id,
            body.execution_id,
            status=rs,
            result=body.result,
            error=body.error,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="not found or execution mismatch")
    return tracker_svc.serialize_item(row)


@app.post("/api/v1/auth/login")
def auth_login(body: LoginRequest, db: Session = Depends(get_db)) -> dict:
    email = body.email.strip().lower()
    row = db.execute(select(PlatformUser).where(PlatformUser.email == email)).scalar_one_or_none()
    if row is None or not _verify_password(body.password, row.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    now = int(time.time())
    exp = now + JWT_EXPIRE_SECONDS
    token = jwt.encode(
        {
            "sub": str(row.id),
            "email": row.email,
            "tenant_id": str(row.tenant_id),
            "roles": row.roles,
            "iat": now,
            "exp": exp,
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "tenant_id": str(row.tenant_id),
        "email": row.email,
        "display_name": row.display_name,
    }


@app.get("/api/v1/admin/users", response_model=list[UserPublic])
def admin_list_users(_: None = Depends(require_superadmin), db: Session = Depends(get_db)) -> list[UserPublic]:
    rows = db.execute(select(PlatformUser).order_by(PlatformUser.created_at.desc())).scalars().all()
    return [
        UserPublic(
            id=str(r.id),
            email=r.email,
            display_name=r.display_name,
            tenant_id=str(r.tenant_id),
            roles=list(r.roles) if r.roles else [],
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


@app.post("/api/v1/admin/users", response_model=UserPublic, status_code=201)
def admin_create_user(
    body: UserCreateBody,
    _: None = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> UserPublic:
    email = body.email.strip().lower()
    existing = db.execute(select(PlatformUser).where(PlatformUser.email == email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    roles = body.roles if body.roles else ["Owner"]
    disp = (body.display_name or "").strip() or email.split("@")[0]
    tenant_id = uuid4()
    row = PlatformUser(
        id=uuid4(),
        email=email,
        password_hash=_hash_password(body.password),
        display_name=disp,
        tenant_id=tenant_id,
        roles=roles,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return UserPublic(
        id=str(row.id),
        email=row.email,
        display_name=row.display_name,
        tenant_id=str(row.tenant_id),
        roles=list(row.roles) if row.roles else [],
        created_at=row.created_at.isoformat(),
    )
