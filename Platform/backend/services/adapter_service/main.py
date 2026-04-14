import base64
import hashlib
import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Generator
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.adapter_service.agent_demo import run_agent_demo as run_agent_demo_pipeline
from services.adapter_service.ssh_command_validator import classify_ssh_command
from services.adapter_service.ssh_confirm import issue_confirmation_token, verify_confirmation_token
from services.adapter_service.ssh_ops import (
    provision_systemd_agent,
    run_ssh_command,
    ssh_read_remote_file,
    test_ssh_connection,
)
from services.adapter_service.crypto import (
    decrypt_secret,
    encrypt_secret,
    hash_agent_token,
    new_agent_token,
)
from services.adapter_service.ansible_local import (
    ansible_playbook_available,
    build_k8s_three_node_inventory,
    iter_ansible_playbook,
    iter_ansible_playbook_multi,
)
from services.adapter_service.k8s_ops import (
    ALLOWED_OPERATIONS,
    execute_k8s_operation,
    kubectl_available,
    test_kubeconfig,
)
from services.adapter_service.clickhouse_activity import (
    clickhouse_status,
    ensure_table,
    fetch_activity,
    log_activity,
)
from services.adapter_service.db import get_db, init_db
from services.adapter_service.models import (
    AgentMetric,
    ProjectInfraResource,
    ServerTarget,
    TargetServiceAssignment,
)

PROJECT_SERVICE_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")
ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006")
INTERNAL_SETTINGS_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
ANSIBLE_ORCHESTRATOR_URL = os.getenv(
    "ANSIBLE_ORCHESTRATOR_URL", "http://ansible-orchestrator-service:8007"
)


SERVICE_INFRA_ROLES = ("gitlab", "nexus")
# Шаблоны по умолчанию: Ansible Galaxy (без Docker на хосте).
ROLE_TO_TEMPLATE = {"gitlab": "provision-gitlab-omnibus", "nexus": "provision-nexus-ck"}
# Старые provision-gitlab / provision-nexus (Docker Compose) остаются в каталоге оркестратора.
TEMPLATE_REQUIRES_DOCKER = {
    "provision-gitlab": True,
    "provision-nexus": True,
    "provision-gitlab-omnibus": False,
    "provision-nexus-ck": False,
    "k8s-kubeadm-3node": False,
}
PREPARE_DOCKER_TEMPLATE_ID = "prepare-docker-host"


def _template_requires_docker(template_id: str) -> bool:
    return TEMPLATE_REQUIRES_DOCKER.get(template_id, True)


def _prepare_docker_variables(tgt: ServerTarget) -> dict[str, str]:
    return {"ssh_user": tgt.ssh_user}


def _fetch_dev_prompts_from_admin() -> dict[str, str]:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/agent-development-prompts",
            headers={"X-Internal-Token": INTERNAL_SETTINGS_TOKEN},
            timeout=5.0,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _fetch_agent_settings_from_admin() -> dict | None:
    try:
        r = httpx.get(
            f"{ADMIN_SERVICE_URL.rstrip('/')}/api/v1/internal/agent-settings",
            headers={"X-Internal-Token": INTERNAL_SETTINGS_TOKEN},
            timeout=5.0,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_ingest_public_url() -> str:
    """URL ingest для remote-агентов: admin DB (если доступен), иначе AGENT_INGEST_PUBLIC_URL."""
    env_fallback = os.getenv(
        "AGENT_INGEST_PUBLIC_URL",
        "http://localhost:8000/api/v1/agent/ingest",
    ).strip()
    cfg = _fetch_agent_settings_from_admin()
    if cfg and isinstance(cfg.get("ingest_public_url"), str) and cfg["ingest_public_url"].strip():
        return cfg["ingest_public_url"].strip()
    return env_fallback
# Пороги статуса агента по последней метрике (интервал агента по умолчанию 60 с)
AGENT_STATUS_ONLINE_SEC = int(os.getenv("AGENT_STATUS_ONLINE_SEC", "180"))
AGENT_STATUS_STALE_MAX_SEC = int(os.getenv("AGENT_STATUS_STALE_MAX_SEC", "900"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        ensure_table()
    except Exception:
        pass
    yield


app = FastAPI(title="adapter-service", version="0.1.0", lifespan=lifespan)


class ActivityContext(BaseModel):
    actor: str | None = None
    subject: str | None = None


class AgentDemoRunBody(BaseModel):
    repo_slug: str | None = Field(
        default=None,
        max_length=80,
        description="Имя репозитория в группе GitLab; по умолчанию lambot-demo-<slug>-<shortid>",
    )


def get_activity_context(
    x_activity_actor: str | None = Header(default=None, alias="X-Activity-Actor"),
    x_activity_subject: str | None = Header(default=None, alias="X-Activity-Subject"),
) -> ActivityContext:
    return ActivityContext(actor=x_activity_actor, subject=x_activity_subject)


def _project_tenant_id(project_id: UUID) -> UUID | None:
    try:
        r = httpx.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}", timeout=5.0)
        if r.status_code != 200:
            return None
        t = r.json().get("tenant_id")
        return UUID(str(t)) if t else None
    except Exception:
        return None


def _emit_activity(
    project_id: UUID,
    act: ActivityContext,
    action: str,
    status: str,
    summary: str,
    details: dict | None = None,
) -> None:
    log_activity(
        project_id=project_id,
        tenant_id=_project_tenant_id(project_id),
        action=action,
        status=status,
        summary=summary,
        details=details,
        actor=act.actor,
        subject=act.subject,
    )


@app.get("/health")
def health() -> dict:
    ch = clickhouse_status()
    return {"status": "ok", "clickhouse": ch}


def _project_exists(project_id: UUID) -> bool:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}")
            return r.status_code == 200
    except Exception:
        return False


def _fetch_project_name(project_id: UUID) -> str:
    try:
        r = httpx.get(f"{PROJECT_SERVICE_URL}/api/v1/projects/{project_id}", timeout=8.0)
        if r.status_code == 200:
            n = r.json().get("name")
            if isinstance(n, str) and n.strip():
                return n.strip()
    except Exception:
        pass
    return "project"


def _slug_for_infra(name: str, project_id: UUID) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.lower().strip())[:32]
    base = re.sub(r"-+", "-", base).strip("-") or "proj"
    short = str(project_id).replace("-", "")[:8]
    return f"{base}-{short}"


def _orchestrator_render_playbook(template_id: str, variables: dict[str, Any]) -> str:
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            f"{ANSIBLE_ORCHESTRATOR_URL.rstrip('/')}/api/v1/ansible/swarm/render",
            json={"template_id": template_id, "variables": variables},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=r.text)
    data = r.json()
    return str(data.get("playbook_yaml") or "")


def _ssh_targets_same_credentials(a: ServerTarget, b: ServerTarget) -> bool:
    if getattr(a, "target_kind", "ssh") != "ssh" or getattr(b, "target_kind", "ssh") != "ssh":
        return False
    if a.ssh_user.strip() != b.ssh_user.strip():
        return False
    if a.auth_type != b.auth_type:
        return False
    return decrypt_secret(a.credential_encrypted) == decrypt_secret(b.credential_encrypted)


def _derive_service_password(project_id: UUID) -> str:
    pepper = os.getenv("LAMBOT_INFRA_CREDENTIALS_PEPPER", "lambot-dev-pepper-change-in-prod")
    digest = hmac.new(pepper.encode("utf-8"), str(project_id).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"Aa1!{digest[:20]}"


def _pull_and_store_infra_credentials(db: Session, project_id: UUID, tgt: ServerTarget, role: str) -> None:
    """Подтягивает JSON с целевого хоста (после Ansible) в ресурсы проекта."""
    sec = decrypt_secret(tgt.credential_encrypted)
    pid = str(project_id)
    base = f"/opt/lambot-infra/lambot/{pid}"
    if role == "gitlab":
        raw = ssh_read_remote_file(tgt.host, tgt.ssh_port, tgt.ssh_user, tgt.auth_type, sec, f"{base}/gitlab-credentials.json")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        label = "GitLab агент (группа и токен)"
        cat = "gitlab"
        token = (data.get("token") or "").strip()
        password = (data.get("password") or "").strip()
        secret_plain = json.dumps({"token": token, "password": password}, ensure_ascii=False) if (token or password) else None
        value_json = {
            "gitlab_url": data.get("gitlab_url"),
            "group_path": data.get("group_path"),
            "group_id": data.get("group_id"),
            "username": data.get("username"),
            "target_host": tgt.host,
            "enabled": True,
        }
        _upsert_infra_credential_row(db, project_id, cat, label, value_json, secret_plain)
    elif role == "nexus":
        raw = ssh_read_remote_file(tgt.host, tgt.ssh_port, tgt.ssh_user, tgt.auth_type, sec, f"{base}/nexus-credentials.json")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        label = "Nexus (Maven / Docker)"
        cat = "nexus"
        pwd = (data.get("admin_password") or "").strip()
        secret_plain = json.dumps({"admin_password": pwd}, ensure_ascii=False) if pwd else None
        value_json = {k: v for k, v in data.items() if k != "admin_password"}
        value_json["enabled"] = True
        _upsert_infra_credential_row(db, project_id, cat, label, value_json, secret_plain)


def _upsert_infra_credential_row(
    db: Session,
    project_id: UUID,
    category: str,
    label: str,
    value_json: dict,
    secret_plain: str | None,
) -> None:
    existing = db.execute(
        select(ProjectInfraResource)
        .where(
            ProjectInfraResource.project_id == project_id,
            ProjectInfraResource.category == category,
            ProjectInfraResource.label == label,
        )
        .limit(1)
    ).scalars().first()
    sec = encrypt_secret(secret_plain) if secret_plain else None
    if existing is None:
        if "enabled" not in value_json:
            value_json = {**value_json, "enabled": True}
        db.add(
            ProjectInfraResource(
                project_id=project_id,
                category=category,
                label=label,
                value_json=value_json,
                secret_encrypted=sec,
            )
        )
    else:
        old_vj = existing.value_json or {}
        merged = {**value_json}
        if "enabled" in old_vj:
            merged["enabled"] = old_vj["enabled"]
        existing.value_json = merged
        existing.secret_encrypted = sec if sec is not None else existing.secret_encrypted
        existing.updated_at = datetime.utcnow()


class TargetCreateRequest(BaseModel):
    target_kind: str = Field(
        default="ssh",
        description="ssh — сервер по SSH; kubernetes — кластер по kubeconfig (без установки агента на ноды)",
    )
    host: str = Field(description="IP или FQDN; для Kubernetes — отображаемое имя кластера")
    ssh_port: int = 22
    ssh_user: str = "ubuntu"
    auth_type: str
    ssh_private_key: str | None = None
    ssh_password: str | None = None
    kubeconfig: str | None = Field(default=None, description="Для target_kind=kubernetes: полный текст kubeconfig")
    kube_context: str | None = Field(default=None, description="Имя контекста в kubeconfig (если пусто — текущий по умолчанию)")
    label: str = "default"
    auto_provision: bool = Field(
        default=True,
        description="После сохранения установить агента по SSH (systemd), нужен sudo без пароля; для Kubernetes игнорируется",
    )


class TargetPublic(BaseModel):
    id: str
    project_id: str
    target_kind: str = "ssh"
    host: str
    ssh_port: int
    ssh_user: str
    auth_type: str
    kube_context: str | None = None
    label: str
    created_at: str
    setup_confirmed_at: str | None = None
    agent_status: str = Field(
        default="no_data",
        description="ok — недавняя метрика; stale — давно; offline — очень давно; no_data — метрик не было",
    )
    agent_last_seen_at: str | None = None
    agent_metrics_total: int = 0
    last_provision_ok: bool | None = Field(
        default=None,
        description="None — автоустановка не запускалась; иначе успех/ошибка последней попытки",
    )
    last_provision_at: str | None = None
    last_provision_log: str | None = Field(
        default=None,
        description="Вывод удалённой установки агента (обрезан при сохранении)",
    )


class TargetCreatedResponse(BaseModel):
    target: TargetPublic
    agent_token: str = Field(
        description="Однократно: сохраните, нужен для агента и prepare-bundle"
    )
    ingest_url: str
    provision: dict | None = Field(
        default=None,
        description="Результат авто-установки агента (ok, detail)",
    )


def _agent_status_for(metrics_total: int, last_seen: datetime | None) -> tuple[str, str | None]:
    if metrics_total <= 0 or last_seen is None:
        return "no_data", None
    age = (datetime.utcnow() - last_seen).total_seconds()
    last_iso = last_seen.isoformat()
    if age <= AGENT_STATUS_ONLINE_SEC:
        return "ok", last_iso
    if age <= AGENT_STATUS_STALE_MAX_SEC:
        return "stale", last_iso
    return "offline", last_iso


def _metrics_by_target(db: Session, project_id: UUID) -> dict[UUID, tuple[int, datetime | None]]:
    q = (
        select(
            AgentMetric.target_id,
            func.count(AgentMetric.id),
            func.max(AgentMetric.received_at),
        )
        .where(AgentMetric.project_id == project_id)
        .group_by(AgentMetric.target_id)
    )
    out: dict[UUID, tuple[int, datetime | None]] = {}
    for tid, cnt, mx in db.execute(q).all():
        out[tid] = (int(cnt), mx)
    return out


def _target_public(
    row: ServerTarget,
    metrics_total: int = 0,
    last_seen: datetime | None = None,
) -> TargetPublic:
    status, last_iso = _agent_status_for(metrics_total, last_seen)
    return TargetPublic(
        id=str(row.id),
        project_id=str(row.project_id),
        target_kind=getattr(row, "target_kind", None) or "ssh",
        host=row.host,
        ssh_port=row.ssh_port,
        ssh_user=row.ssh_user,
        auth_type=row.auth_type,
        kube_context=getattr(row, "kube_context", None),
        label=row.label,
        created_at=row.created_at.isoformat(),
        setup_confirmed_at=row.setup_confirmed_at.isoformat() if row.setup_confirmed_at else None,
        agent_status=status,
        agent_last_seen_at=last_iso,
        agent_metrics_total=metrics_total,
        last_provision_ok=row.last_provision_ok,
        last_provision_at=row.last_provision_at.isoformat() if row.last_provision_at else None,
        last_provision_log=row.last_provision_log,
    )


@app.post("/api/v1/projects/{project_id}/targets/test-connection")
def test_target_ssh(
    project_id: UUID,
    body: TargetCreateRequest,
) -> dict:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    kind = (body.target_kind or "ssh").strip().lower()
    if kind == "kubernetes":
        if not body.kubeconfig or not str(body.kubeconfig).strip():
            raise HTTPException(status_code=400, detail="kubeconfig required for kubernetes target")
        if not kubectl_available():
            raise HTTPException(
                status_code=503,
                detail="kubectl недоступен в adapter (проверьте образ: установлен ли kubectl)",
            )
        ok, detail = test_kubeconfig(body.kubeconfig, body.kube_context)
        return {"ok": ok, "detail": detail, "target_kind": "kubernetes"}
    if kind != "ssh":
        raise HTTPException(status_code=400, detail="target_kind must be ssh or kubernetes")
    if body.auth_type not in ("key", "password"):
        raise HTTPException(status_code=400, detail="auth_type must be key or password")
    if body.auth_type == "key" and not body.ssh_private_key:
        raise HTTPException(status_code=400, detail="ssh_private_key required for key auth")
    if body.auth_type == "password" and not body.ssh_password:
        raise HTTPException(status_code=400, detail="ssh_password required for password auth")
    secret = (body.ssh_private_key or body.ssh_password or "").strip()
    ok, detail = test_ssh_connection(
        body.host,
        body.ssh_port,
        body.ssh_user,
        body.auth_type,
        secret,
    )
    return {"ok": ok, "detail": detail, "target_kind": "ssh"}


@app.post("/api/v1/projects/{project_id}/targets", response_model=TargetCreatedResponse)
def create_target(
    project_id: UUID,
    body: TargetCreateRequest,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> TargetCreatedResponse:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    kind = (body.target_kind or "ssh").strip().lower()

    if kind == "kubernetes":
        if not body.kubeconfig or not str(body.kubeconfig).strip():
            raise HTTPException(status_code=400, detail="kubeconfig required for kubernetes target")
        cred = encrypt_secret(body.kubeconfig.strip())
        token = new_agent_token()
        th = hash_agent_token(token)
        token_store = encrypt_secret(token)
        kctx = (body.kube_context or "").strip() or None
        row = ServerTarget(
            project_id=project_id,
            target_kind="kubernetes",
            host=body.host.strip() or "Kubernetes",
            ssh_port=0,
            ssh_user="kubernetes",
            auth_type="kubeconfig",
            kube_context=kctx,
            credential_encrypted=cred,
            agent_token_hash=th,
            agent_token_encrypted=token_store,
            label=(body.label or "").strip() or "kubernetes",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        provision: dict | None = None
        if kubectl_available():
            ok, detail = test_kubeconfig(body.kubeconfig, kctx)
            provision = {"ok": ok, "detail": detail, "note": "Проверка kubectl после сохранения"}
            if ok:
                row.setup_confirmed_at = datetime.utcnow()
                row.last_provision_ok = True
                row.last_provision_at = datetime.utcnow()
                row.last_provision_log = (detail or "")[:8000]
            else:
                row.last_provision_ok = False
                row.last_provision_at = datetime.utcnow()
                row.last_provision_log = (detail or "")[:8000]
            db.commit()
            db.refresh(row)
        else:
            provision = {"ok": None, "detail": "kubectl недоступен в adapter, проверьте подключение вручную"}
        tp = _target_public(row, 0, None)
        _emit_activity(
            project_id,
            act,
            "target.create",
            "ok",
            f"Kubernetes target {row.host} ({row.label})",
            {"target_id": str(row.id), "target_kind": "kubernetes", "provision": provision},
        )
        return TargetCreatedResponse(
            target=tp,
            agent_token=token,
            ingest_url=get_ingest_public_url(),
            provision=provision,
        )

    if kind != "ssh":
        raise HTTPException(status_code=400, detail="target_kind must be ssh or kubernetes")
    if body.auth_type not in ("key", "password"):
        raise HTTPException(status_code=400, detail="auth_type must be key or password")
    if body.auth_type == "key" and not body.ssh_private_key:
        raise HTTPException(status_code=400, detail="ssh_private_key required for key auth")
    if body.auth_type == "password" and not body.ssh_password:
        raise HTTPException(status_code=400, detail="ssh_password required for password auth")

    secret = body.ssh_private_key or body.ssh_password or ""
    cred = encrypt_secret(secret)
    token = new_agent_token()
    th = hash_agent_token(token)
    token_store = encrypt_secret(token)

    row = ServerTarget(
        project_id=project_id,
        target_kind="ssh",
        host=body.host.strip(),
        ssh_port=body.ssh_port,
        ssh_user=body.ssh_user.strip(),
        auth_type=body.auth_type,
        credential_encrypted=cred,
        agent_token_hash=th,
        agent_token_encrypted=token_store,
        label=body.label,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    provision = None
    if body.auto_provision:
        secret_plain = body.ssh_private_key or body.ssh_password or ""
        try:
            ok, detail = provision_systemd_agent(
                body.host.strip(),
                body.ssh_port,
                body.ssh_user.strip(),
                body.auth_type,
                secret_plain,
                get_ingest_public_url(),
                token,
            )
            provision = {"ok": ok, "detail": detail}
            row.last_provision_ok = ok
            row.last_provision_at = datetime.utcnow()
            row.last_provision_log = (detail or "")[:8000]
            if ok:
                row.setup_confirmed_at = datetime.utcnow()
        except Exception as exc:
            provision = {"ok": False, "detail": str(exc)[:2000]}
            row.last_provision_ok = False
            row.last_provision_at = datetime.utcnow()
            row.last_provision_log = str(exc)[:8000]
        db.commit()
        db.refresh(row)

    tp = _target_public(row, 0, None)
    _emit_activity(
        project_id,
        act,
        "target.create",
        "ok",
        f"SSH target {row.host} ({row.label})",
        {"target_id": str(row.id), "auto_provision": body.auto_provision, "provision": provision},
    )
    return TargetCreatedResponse(
        target=tp,
        agent_token=token,
        ingest_url=get_ingest_public_url(),
        provision=provision,
    )


@app.get("/api/v1/projects/{project_id}/targets", response_model=list[TargetPublic])
def list_targets(project_id: UUID, db: Session = Depends(get_db)) -> list[TargetPublic]:
    rows = db.execute(
        select(ServerTarget).where(ServerTarget.project_id == project_id).order_by(ServerTarget.created_at.desc())
    ).scalars().all()
    agg = _metrics_by_target(db, project_id)
    out: list[TargetPublic] = []
    for r in rows:
        mt, last = agg.get(r.id, (0, None))
        out.append(_target_public(r, mt, last))
    return out


@app.delete("/api/v1/projects/{project_id}/targets/{target_id}")
def delete_target(
    project_id: UUID,
    target_id: UUID,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict[str, str]:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    row = db.get(ServerTarget, target_id)
    if not row or str(row.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Target not found")
    host = row.host
    db.execute(delete(AgentMetric).where(AgentMetric.target_id == target_id))
    db.delete(row)
    db.commit()
    _emit_activity(
        project_id,
        act,
        "target.delete",
        "ok",
        f"Удалён сервер {host} ({target_id})",
        {"deleted_target_id": str(target_id)},
    )
    return {"status": "ok", "deleted_target_id": str(target_id)}


class SshExecBody(BaseModel):
    command: str = Field(..., max_length=4000)
    confirmed: bool = False
    confirmation_token: str | None = None


@app.post("/api/v1/projects/{project_id}/targets/{target_id}/ssh-exec")
def ssh_exec_on_target(
    project_id: UUID,
    target_id: UUID,
    body: SshExecBody,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict[str, Any]:
    """
    Выполнение одной команды на SSH-таргете. Опасные команды — только после
    подтверждения (повтор с confirmed=true и confirmation_token).
    """
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    row = db.get(ServerTarget, target_id)
    if not row or str(row.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Target not found")
    if getattr(row, "target_kind", "ssh") != "ssh":
        raise HTTPException(status_code=400, detail="SSH exec доступен только для таргета с типом SSH")
    cmd = (body.command or "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="Пустая команда")

    cls, reason = classify_ssh_command(cmd)
    if cls == "blocked":
        raise HTTPException(status_code=400, detail=reason)

    # SSH_SKIP_USER_CONFIRMATION=1 — выполнять «опасные» команды без двухшагового подтверждения пользователем (политика площадки).
    if cls == "needs_confirmation" and os.getenv("SSH_SKIP_USER_CONFIRMATION", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        cls = "allowed"

    if cls == "needs_confirmation":
        if not body.confirmed:
            tok = issue_confirmation_token(str(project_id), str(target_id), cmd)
            return {
                "needs_confirmation": True,
                "reason": reason,
                "confirmation_token": tok,
                "hint": "Повторите запрос с confirmed=true и тем же confirmation_token после согласия пользователя.",
            }
        tok = (body.confirmation_token or "").strip()
        if not tok:
            raise HTTPException(status_code=400, detail="Нужен confirmation_token для подтверждённой команды")
        ok_t, verr = verify_confirmation_token(tok, str(project_id), str(target_id), cmd)
        if not ok_t:
            raise HTTPException(status_code=400, detail=verr)

    secret = decrypt_secret(row.credential_encrypted)
    timeout_sec = float(os.getenv("SSH_EXEC_TIMEOUT_SEC", "120"))
    try:
        rc, out, err = run_ssh_command(
            row.host,
            row.ssh_port,
            row.ssh_user,
            row.auth_type,
            secret,
            cmd,
            timeout=timeout_sec,
        )
    except Exception as exc:
        _emit_activity(
            project_id,
            act,
            "target.ssh_exec",
            "error",
            f"SSH exec: {row.host}",
            {"error": str(exc)[:500], "target_id": str(target_id)},
        )
        raise HTTPException(status_code=500, detail=str(exc)[:2000]) from exc

    ok_run = rc == 0
    _emit_activity(
        project_id,
        act,
        "target.ssh_exec",
        "ok" if ok_run else "error",
        f"SSH exec на {row.host}",
        {"exit_code": rc, "command_preview": cmd[:200], "target_id": str(target_id)},
    )
    return {
        "ok": ok_run,
        "exit_code": rc,
        "stdout": out,
        "stderr": err,
        "needs_confirmation": False,
    }


class PrepareRequest(BaseModel):
    template_id: str = "install-platform-agent"


def _orchestrator_prepare_bundle(template_id: str, variables: dict, row: ServerTarget) -> dict:
    payload = {
        "template_id": template_id,
        "variables": variables,
        "inventory_host": row.host,
        "inventory_user": row.ssh_user,
        "inventory_port": row.ssh_port,
    }
    with httpx.Client(timeout=120.0) as client:
        r = client.post(f"{ANSIBLE_ORCHESTRATOR_URL}/api/v1/ansible/swarm/prepare-bundle", json=payload)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=r.text)
    return r.json()


@app.post("/api/v1/projects/{project_id}/targets/{target_id}/prepare")
def prepare_bundle(
    project_id: UUID,
    target_id: UUID,
    body: PrepareRequest,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
):
    row = db.get(ServerTarget, target_id)
    if not row or str(row.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Target not found")
    if getattr(row, "target_kind", "ssh") == "kubernetes":
        raise HTTPException(
            status_code=400,
            detail="Для таргета Kubernetes (kubeconfig) скачивание Ansible bundle не поддерживается — используйте SSH-сервер.",
        )

    agent_token = decrypt_secret(row.agent_token_encrypted)

    variables = {
        "ssh_user": row.ssh_user,
        "ingest_url": get_ingest_public_url(),
        "agent_token": agent_token,
    }
    data = _orchestrator_prepare_bundle(body.template_id, variables, row)
    _emit_activity(
        project_id,
        act,
        "target.prepare_bundle",
        "ok",
        f"Bundle {body.template_id} для {row.host}",
        {"template_id": body.template_id, "target_id": str(target_id)},
    )
    return {
        **data,
        "note": "Запустите ansible-playbook с вашей машины, имея SSH-доступ; секрет не логируем.",
    }


@app.post("/api/v1/projects/{project_id}/targets/{target_id}/confirm-setup")
def confirm_target_setup(
    project_id: UUID,
    target_id: UUID,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict[str, str]:
    row = db.get(ServerTarget, target_id)
    if not row or str(row.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Target not found")
    row.setup_confirmed_at = datetime.utcnow()
    db.commit()
    _emit_activity(
        project_id,
        act,
        "target.confirm_setup",
        "ok",
        f"Подтверждение настройки {row.host}",
        {"target_id": str(target_id)},
    )
    return {"status": "ok"}


@app.get("/api/v1/projects/{project_id}/workspace")
def get_project_workspace(project_id: UUID, db: Session = Depends(get_db)) -> dict:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    targets = (
        db.execute(select(ServerTarget).where(ServerTarget.project_id == project_id).order_by(ServerTarget.created_at.desc()))
        .scalars()
        .all()
    )
    metrics_n = db.execute(
        select(func.count(AgentMetric.id)).where(AgentMetric.project_id == project_id)
    ).scalar_one()
    metrics_n = int(metrics_n or 0)
    can_chat = metrics_n > 0 or any(t.setup_confirmed_at is not None for t in targets)
    agg = _metrics_by_target(db, project_id)
    return {
        "project_id": str(project_id),
        "targets": [
            _target_public(t, *agg.get(t.id, (0, None))).model_dump()
            for t in targets
        ],
        "agent_metrics_count": metrics_n,
        "can_use_llm_chat": can_chat,
    }


class IngestBody(BaseModel):
    payload: dict = Field(default_factory=dict)


@app.post("/api/v1/agent/ingest")
def agent_ingest(
    body: IngestBody,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing agent token")
    raw = authorization.replace("Bearer ", "", 1).strip()
    th = hash_agent_token(raw)
    row = db.execute(select(ServerTarget).where(ServerTarget.agent_token_hash == th)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=403, detail="Invalid agent token")
    m = AgentMetric(
        target_id=row.id,
        project_id=row.project_id,
        payload=body.payload,
    )
    db.add(m)
    db.commit()
    return {"status": "ok"}


@app.get("/api/v1/projects/{project_id}/agent/metrics")
def list_metrics(
    project_id: UUID,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(AgentMetric)
        .where(AgentMetric.project_id == project_id)
        .order_by(AgentMetric.received_at.desc())
        .limit(min(limit, 200))
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "target_id": str(r.target_id),
            "received_at": r.received_at.isoformat(),
            "payload": r.payload,
        }
        for r in rows
    ]


class ServiceAssignmentsPutBody(BaseModel):
    assignments: dict[str, str | None] = Field(
        default_factory=dict,
        description="gitlab / nexus → target_id или null (снять назначение)",
    )


@app.get("/api/v1/projects/{project_id}/infra/service-assignments")
def list_service_assignments(project_id: UUID, db: Session = Depends(get_db)) -> list[dict]:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    rows = db.execute(
        select(TargetServiceAssignment).where(TargetServiceAssignment.project_id == project_id)
    ).scalars().all()
    out: list[dict] = []
    for a in rows:
        t = db.get(ServerTarget, a.target_id)
        out.append(
            {
                "service_role": a.service_role,
                "target_id": str(a.target_id),
                "host": t.host if t else None,
                "label": t.label if t else None,
            }
        )
    return out


@app.put("/api/v1/projects/{project_id}/infra/service-assignments")
def put_service_assignments(
    project_id: UUID,
    body: ServiceAssignmentsPutBody,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict[str, str]:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    for role in body.assignments:
        if role not in SERVICE_INFRA_ROLES:
            raise HTTPException(status_code=400, detail=f"Unknown service role: {role}")
    for role, tid_raw in body.assignments.items():
        existing = db.execute(
            select(TargetServiceAssignment).where(
                TargetServiceAssignment.project_id == project_id,
                TargetServiceAssignment.service_role == role,
            )
        ).scalar_one_or_none()
        if tid_raw is None or str(tid_raw).strip() == "":
            if existing:
                db.delete(existing)
            continue
        try:
            tid = UUID(str(tid_raw))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid target id for {role}")
        tgt = db.get(ServerTarget, tid)
        if not tgt or str(tgt.project_id) != str(project_id):
            raise HTTPException(status_code=400, detail=f"Target not in project for {role}")
        if getattr(tgt, "target_kind", "ssh") != "ssh":
            raise HTTPException(
                status_code=400,
                detail=f"Роль {role} требует сервер с подключением по SSH, не Kubernetes (kubeconfig).",
            )
        if existing:
            existing.target_id = tid
        else:
            db.add(
                TargetServiceAssignment(
                    project_id=project_id,
                    target_id=tid,
                    service_role=role,
                )
            )
    db.commit()
    _emit_activity(
        project_id,
        act,
        "infra.service_assignments",
        "ok",
        "Назначения GitLab/Nexus на серверы обновлены",
        {"assignments": body.assignments},
    )
    return {"status": "ok"}


class InfraResourceCreateBody(BaseModel):
    category: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=256)
    value_json: dict = Field(default_factory=dict)
    secret_plain: str | None = None


class InfraResourcePatchBody(BaseModel):
    label: str | None = Field(default=None, max_length=256)
    value_json: dict | None = None
    secret_plain: str | None = None
    enabled: bool | None = None


def _is_resource_enabled(r: ProjectInfraResource) -> bool:
    vj = r.value_json or {}
    e = vj.get("enabled", True)
    return e is not False


def _resource_public(r: ProjectInfraResource) -> dict:
    vj = dict(r.value_json or {})
    enabled = _is_resource_enabled(r)
    return {
        "id": str(r.id),
        "category": r.category,
        "label": r.label,
        "value_json": vj,
        "enabled": enabled,
        "has_secret": bool(r.secret_encrypted),
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


@app.get("/api/v1/projects/{project_id}/infra/resources")
def list_infra_resources(
    project_id: UUID,
    include_disabled: bool = True,
    db: Session = Depends(get_db),
) -> list[dict]:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    rows = db.execute(
        select(ProjectInfraResource)
        .where(ProjectInfraResource.project_id == project_id)
        .order_by(ProjectInfraResource.updated_at.desc())
    ).scalars().all()
    out = [_resource_public(r) for r in rows]
    if not include_disabled:
        out = [x for x in out if x.get("enabled") is not False]
    return out


@app.post("/api/v1/projects/{project_id}/infra/resources")
def create_infra_resource(
    project_id: UUID,
    body: InfraResourceCreateBody,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    sec = encrypt_secret(body.secret_plain) if body.secret_plain else None
    vj = dict(body.value_json or {})
    if "enabled" not in vj:
        vj["enabled"] = True
    row = ProjectInfraResource(
        project_id=project_id,
        category=body.category.strip(),
        label=body.label.strip(),
        value_json=vj,
        secret_encrypted=sec,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _emit_activity(
        project_id,
        act,
        "infra.resource_create",
        "ok",
        f"Ресурс {body.category}/{body.label}",
        {"resource_id": str(row.id), "category": body.category, "label": body.label},
    )
    return _resource_public(row)


@app.patch("/api/v1/projects/{project_id}/infra/resources/{resource_id}")
def patch_infra_resource(
    project_id: UUID,
    resource_id: UUID,
    body: InfraResourcePatchBody,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    row = db.get(ProjectInfraResource, resource_id)
    if not row or str(row.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    if body.label is not None:
        row.label = body.label.strip()
    if body.value_json is not None:
        row.value_json = body.value_json
    if body.enabled is not None:
        vj = dict(row.value_json or {})
        vj["enabled"] = body.enabled
        row.value_json = vj
    if body.secret_plain is not None:
        row.secret_encrypted = encrypt_secret(body.secret_plain) if body.secret_plain else None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    _emit_activity(
        project_id,
        act,
        "infra.resource_patch",
        "ok",
        f"Обновлён ресурс {row.label}",
        {"resource_id": str(resource_id)},
    )
    return _resource_public(row)


@app.delete("/api/v1/projects/{project_id}/infra/resources/{resource_id}")
def delete_infra_resource(
    project_id: UUID,
    resource_id: UUID,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict[str, str]:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    row = db.get(ProjectInfraResource, resource_id)
    if not row or str(row.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    lab = row.label
    db.delete(row)
    db.commit()
    _emit_activity(
        project_id,
        act,
        "infra.resource_delete",
        "ok",
        f"Удалён ресурс {lab}",
        {"resource_id": str(resource_id)},
    )
    return {"status": "ok"}


class K8sInstall3Body(BaseModel):
    master_target_id: UUID
    worker1_target_id: UUID
    worker2_target_id: UUID
    pod_network_cidr: str = "10.244.0.0/16"


class K8sQueryBody(BaseModel):
    """Безопасные операции kubectl по сохранённому kubeconfig (см. ALLOWED_OPERATIONS)."""

    target_id: UUID
    operation: str = Field(
        description="cluster_info, version, nodes, namespaces, pods, deployments, services, ingresses, statefulsets, daemonsets, events, describe, logs, top_nodes, top_pods, rollout_status, scale, rollout_restart",
    )
    namespace: str | None = None
    name: str | None = None
    resource: str | None = Field(default=None, description="Для describe: pod, deployment, service, …")
    container: str | None = Field(default=None, description="Для logs: контейнер в pod")
    tail_lines: int = Field(default=200, ge=1, le=5000)
    all_namespaces: bool = False
    wide: bool = False
    replicas: int | None = Field(default=None, ge=0, le=5000, description="Только для scale (deployment)")


def _iter_k8s_install_3node(
    db: Session,
    project_id: UUID,
    body: K8sInstall3Body,
    act: ActivityContext,
) -> Generator[dict[str, Any], None, None]:
    ids = {body.master_target_id, body.worker1_target_id, body.worker2_target_id}
    if len(ids) != 3:
        yield {
            "type": "done",
            "payload": {"ok": False, "error": "Укажите три разных SSH-таргета (control plane и два worker)."},
            "status": "error",
            "summary": "Дубликаты target_id",
        }
        return
    master = db.get(ServerTarget, body.master_target_id)
    w1 = db.get(ServerTarget, body.worker1_target_id)
    w2 = db.get(ServerTarget, body.worker2_target_id)
    for label, t in (("master", master), ("worker1", w1), ("worker2", w2)):
        if not t or str(t.project_id) != str(project_id):
            yield {
                "type": "done",
                "payload": {"ok": False, "error": f"Таргет {label} не найден в проекте."},
                "status": "error",
                "summary": "Таргет не найден",
            }
            return
        if getattr(t, "target_kind", "ssh") != "ssh":
            yield {
                "type": "done",
                "payload": {"ok": False, "error": f"Таргет {label}: нужен тип SSH (не kubeconfig)."},
                "status": "error",
                "summary": "Неверный тип таргета",
            }
            return
    assert master is not None and w1 is not None and w2 is not None
    if not _ssh_targets_same_credentials(master, w1) or not _ssh_targets_same_credentials(master, w2):
        yield {
            "type": "done",
            "payload": {
                "ok": False,
                "error": "На всех трёх нодах должны совпадать SSH-пользователь и тот же ключ или пароль.",
            },
            "status": "error",
            "summary": "Разные учётные данные SSH",
        }
        return

    pod = (body.pod_network_cidr or "").strip() or "10.244.0.0/16"
    yield {
        "type": "phase",
        "step": "k8s-install",
        "template_id": "k8s-kubeadm-3node",
        "host": master.host,
    }
    try:
        playbook_yaml = _orchestrator_render_playbook("k8s-kubeadm-3node", {})
    except HTTPException:
        raise
    except Exception as exc:
        yield {
            "type": "done",
            "payload": {"ok": False, "error": str(exc)},
            "status": "error",
            "summary": "Ошибка рендера шаблона",
        }
        return

    secret = decrypt_secret(master.credential_encrypted)
    with TemporaryDirectory(prefix="lambot-k8s-") as tmp:
        tmp_path = Path(tmp)
        key_path: Path | None = None
        if master.auth_type == "key":
            key_path = tmp_path / "ssh_key.pem"
            key_path.write_text(secret.strip() + ("\n" if not secret.endswith("\n") else ""), encoding="utf-8")
            key_path.chmod(0o600)
        inv_yaml = build_k8s_three_node_inventory(
            master,
            w1,
            w2,
            key_path=key_path if master.auth_type == "key" else None,
            password_plain=secret if master.auth_type != "key" else None,
            pod_network_cidr=pod,
        )
        gen = iter_ansible_playbook_multi(playbook_yaml, inv_yaml)
        rc = 127
        out = ""
        err = ""
        try:
            while True:
                line = next(gen)
                yield {"type": "line", "text": line}
        except StopIteration as ex:
            val = ex.value
            if val:
                rc, out, err = val
            else:
                rc, out, err = 127, "", "ansible: нет результата"

    ok = rc == 0
    summary = "Kubernetes (kubeadm): плейбук завершён" if ok else f"Kubernetes: ошибка ansible (код {rc})"
    payload: dict[str, Any] = {
        "ok": ok,
        "ansible_rc": rc,
        "stdout": _truncate_log(out),
        "stderr": _truncate_log(err),
        "template_id": "k8s-kubeadm-3node",
        "master_host": master.host,
        "worker_hosts": [w1.host, w2.host],
        "pod_network_cidr": pod,
        "note": "Проверьте узлы: kubectl get nodes. При необходимости сохраните kubeconfig отдельным таргетом «Kubernetes».",
    }
    st = "ok" if ok else "error"
    _emit_activity(
        project_id,
        act,
        "infra.k8s_install_3node",
        st,
        summary,
        {"ansible_rc": rc, "master_target_id": str(master.id), "worker_target_ids": [str(w1.id), str(w2.id)]},
    )
    yield {"type": "done", "payload": payload, "status": st, "summary": summary}


class PrepareStackBody(BaseModel):
    gitlab_http_port: int = 8081
    nexus_http_port: int = 8082
    nexus_docker_port: int = Field(default=5050, ge=1, le=65535, description="Порт на хосте для Docker Registry в Nexus")


def _infra_template_vars_and_url(
    role: str, tgt: ServerTarget, body: PrepareStackBody, project_id: UUID
) -> tuple[dict, str] | None:
    pname = _fetch_project_name(project_id)
    slug = _slug_for_infra(pname, project_id)
    pwd = _derive_service_password(project_id)
    pwd_b64 = base64.b64encode(pwd.encode("utf-8")).decode("ascii")
    common = {
        "ssh_user": tgt.ssh_user,
        "server_host": tgt.host,
        "lambot_project_id": str(project_id),
        "lambot_project_slug": slug,
        "lambot_project_name": pname,
        "lambot_service_password": pwd,
        "lambot_service_password_b64": pwd_b64,
        "nexus_docker_port": body.nexus_docker_port,
    }
    if role == "gitlab":
        ext = f"http://{tgt.host}:{body.gitlab_http_port}"
        return (
            {
                **common,
                "gitlab_http_port": body.gitlab_http_port,
                "gitlab_external_url": ext,
            },
            ext,
        )
    if role == "nexus":
        ext = f"http://{tgt.host}:{body.nexus_http_port}"
        return (
            {
                **common,
                "nexus_http_port": body.nexus_http_port,
                "nexus_url": ext,
                "nexus_host": tgt.host,
            },
            ext,
        )
    return None


def _ensure_infra_url_resource(
    db: Session,
    project_id: UUID,
    role: str,
    ext: str,
    template_id: str,
) -> None:
    label = "GitLab URL" if role == "gitlab" else "Nexus URL"
    cat = "gitlab" if role == "gitlab" else "nexus"
    payload = {"url": ext, "ansible_template": template_id}
    existing = db.execute(
        select(ProjectInfraResource)
        .where(
            ProjectInfraResource.project_id == project_id,
            ProjectInfraResource.category == cat,
            ProjectInfraResource.label == label,
        )
        .limit(1)
    ).scalars().first()
    if existing is None:
        db.add(
            ProjectInfraResource(
                project_id=project_id,
                category=cat,
                label=label,
                value_json=payload,
            )
        )
    else:
        existing.value_json = payload
        existing.updated_at = datetime.utcnow()


def _truncate_log(text: str, max_len: int = 48000) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 40] + "\n... [truncated]"


def _sanitize_prepare_results(results: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in results:
        c = dict(item)
        b = c.get("bundle")
        if isinstance(b, dict) and "playbook_yaml" in b:
            b2 = dict(b)
            py = b2.get("playbook_yaml") or ""
            b2["playbook_yaml"] = f"<omitted, {len(py)} chars>"
            c["bundle"] = b2
        out.append(c)
    return out


@app.post("/api/v1/projects/{project_id}/infra/prepare-stack")
def prepare_infra_stack(
    project_id: UUID,
    body: PrepareStackBody,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict:
    """Один запрос: bundle для GitLab и Nexus по назначенным target (ansible-playbook с локальной машины)."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    assignments = db.execute(
        select(TargetServiceAssignment).where(TargetServiceAssignment.project_id == project_id)
    ).scalars().all()
    by_role = {a.service_role: a for a in assignments}
    results: list[dict] = []
    # Сначала bundle prepare-docker-host на каждый уникальный target (GitLab/Nexus), затем роли.
    prep_targets: dict[UUID, ServerTarget] = {}
    for role in SERVICE_INFRA_ROLES:
        ass = by_role.get(role)
        if not ass:
            continue
        tgt = db.get(ServerTarget, ass.target_id)
        if tgt and str(tgt.project_id) == str(project_id):
            tid = ROLE_TO_TEMPLATE.get(role)
            if tid and _template_requires_docker(tid):
                prep_targets[tgt.id] = tgt
    for tgt in prep_targets.values():
        try:
            prep_bundle = _orchestrator_prepare_bundle(PREPARE_DOCKER_TEMPLATE_ID, _prepare_docker_variables(tgt), tgt)
        except HTTPException as e:
            results.append(
                {
                    "step": "prepare-docker-host",
                    "target_id": str(tgt.id),
                    "host": tgt.host,
                    "template_id": PREPARE_DOCKER_TEMPLATE_ID,
                    "error": str(e.detail),
                }
            )
            continue
        results.append(
            {
                "step": "prepare-docker-host",
                "target_id": str(tgt.id),
                "host": tgt.host,
                "template_id": PREPARE_DOCKER_TEMPLATE_ID,
                "bundle": prep_bundle,
            }
        )

    for role in SERVICE_INFRA_ROLES:
        template_id = ROLE_TO_TEMPLATE.get(role)
        if not template_id:
            continue
        ass = by_role.get(role)
        if not ass:
            results.append({"service_role": role, "skipped": True, "reason": "no_target_assigned"})
            continue
        tgt = db.get(ServerTarget, ass.target_id)
        if not tgt or str(tgt.project_id) != str(project_id):
            results.append({"service_role": role, "skipped": True, "reason": "invalid_assignment"})
            continue
        built = _infra_template_vars_and_url(role, tgt, body, project_id)
        if not built:
            continue
        variables, ext = built
        try:
            data = _orchestrator_prepare_bundle(template_id, variables, tgt)
        except HTTPException as e:
            results.append({"service_role": role, "error": str(e.detail)})
            continue
        results.append(
            {
                "service_role": role,
                "target_id": str(tgt.id),
                "host": tgt.host,
                "template_id": template_id,
                "bundle": data,
            }
        )
        _ensure_infra_url_resource(db, project_id, role, ext, template_id)
    db.commit()
    out = {
        "project_id": str(project_id),
        "results": results,
        "note": "Сначала выполните prepare-docker-host на хосте, затем GitLab/Nexus. Bundle — playbook и inventory.",
    }
    _emit_activity(
        project_id,
        act,
        "infra.prepare_stack",
        "ok",
        f"Подготовлено шагов: {len([r for r in results if not r.get('skipped')])}",
        {"results": _sanitize_prepare_results(results)},
    )
    return out


def _iter_infra_run_stack(
    db: Session,
    project_id: UUID,
    body: PrepareStackBody,
    act: ActivityContext,
) -> Generator[dict[str, Any], None, None]:
    """
    Одна логика run-stack: события NDJSON для стрима и для UI.
    События: phase, line, skip, bundle_error, done.
    """
    assignments = db.execute(
        select(TargetServiceAssignment).where(TargetServiceAssignment.project_id == project_id)
    ).scalars().all()
    by_role = {a.service_role: a for a in assignments}
    results: list[dict] = []
    prepared_ok: set[UUID] = set()
    prepare_failed: set[UUID] = set()

    for role in SERVICE_INFRA_ROLES:
        template_id = ROLE_TO_TEMPLATE.get(role)
        if not template_id:
            continue
        ass = by_role.get(role)
        if not ass:
            results.append({"service_role": role, "skipped": True, "reason": "no_target_assigned"})
            yield {"type": "skip", "service_role": role, "reason": "no_target_assigned"}
            continue
        tgt = db.get(ServerTarget, ass.target_id)
        if not tgt or str(tgt.project_id) != str(project_id):
            results.append({"service_role": role, "skipped": True, "reason": "invalid_assignment"})
            yield {"type": "skip", "service_role": role, "reason": "invalid_assignment"}
            continue

        if tgt.id in prepare_failed:
            results.append(
                {
                    "service_role": role,
                    "target_id": str(tgt.id),
                    "host": tgt.host,
                    "skipped": True,
                    "reason": "prepare_docker_host_failed",
                }
            )
            yield {"type": "skip", "service_role": role, "reason": "prepare_docker_host_failed"}
            continue

        needs_prep = _template_requires_docker(template_id)

        if needs_prep and tgt.id not in prepared_ok:
            try:
                prep_data = _orchestrator_prepare_bundle(
                    PREPARE_DOCKER_TEMPLATE_ID, _prepare_docker_variables(tgt), tgt
                )
            except HTTPException as e:
                prepare_failed.add(tgt.id)
                results.append(
                    {
                        "step": "prepare-docker-host",
                        "target_id": str(tgt.id),
                        "host": tgt.host,
                        "template_id": PREPARE_DOCKER_TEMPLATE_ID,
                        "error": str(e.detail),
                    }
                )
                results.append(
                    {
                        "service_role": role,
                        "target_id": str(tgt.id),
                        "host": tgt.host,
                        "skipped": True,
                        "reason": "prepare_docker_bundle_failed",
                    }
                )
                yield {
                    "type": "bundle_error",
                    "step": "prepare-docker-host",
                    "target_id": str(tgt.id),
                    "host": tgt.host,
                    "detail": str(e.detail),
                }
                continue
            playbook_prep = prep_data.get("playbook_yaml") or ""
            yield {
                "type": "phase",
                "step": "prepare-docker-host",
                "service_role": role,
                "target_id": str(tgt.id),
                "host": tgt.host,
                "template_id": PREPARE_DOCKER_TEMPLATE_ID,
            }
            gen = iter_ansible_playbook(playbook_prep, tgt)
            try:
                while True:
                    line = next(gen)
                    yield {"type": "line", "text": line}
            except StopIteration as ex:
                val = ex.value
                if not val:
                    rc_p, out_p, err_p = 127, "", "ansible: нет результата"
                else:
                    rc_p, out_p, err_p = val
            prep_ok = rc_p == 0
            results.append(
                {
                    "step": "prepare-docker-host",
                    "target_id": str(tgt.id),
                    "host": tgt.host,
                    "template_id": PREPARE_DOCKER_TEMPLATE_ID,
                    "ansible_rc": rc_p,
                    "ok": prep_ok,
                    "stdout": _truncate_log(out_p),
                    "stderr": _truncate_log(err_p),
                }
            )
            yield {
                "type": "phase_done",
                "step": "prepare-docker-host",
                "target_id": str(tgt.id),
                "host": tgt.host,
                "ansible_rc": rc_p,
                "ok": prep_ok,
            }
            if rc_p == -1 or not prep_ok:
                prepare_failed.add(tgt.id)
                results.append(
                    {
                        "service_role": role,
                        "target_id": str(tgt.id),
                        "host": tgt.host,
                        "skipped": True,
                        "reason": "prepare_docker_ansible_failed",
                    }
                )
                yield {"type": "skip", "service_role": role, "reason": "prepare_docker_ansible_failed"}
                continue
            prepared_ok.add(tgt.id)

        built = _infra_template_vars_and_url(role, tgt, body, project_id)
        if not built:
            continue
        variables, ext = built
        try:
            data = _orchestrator_prepare_bundle(template_id, variables, tgt)
        except HTTPException as e:
            results.append({"service_role": role, "error": str(e.detail)})
            yield {"type": "bundle_error", "service_role": role, "template_id": template_id, "detail": str(e.detail)}
            continue
        playbook_yaml = data.get("playbook_yaml") or ""
        yield {
            "type": "phase",
            "step": "provision",
            "service_role": role,
            "target_id": str(tgt.id),
            "host": tgt.host,
            "template_id": template_id,
        }
        gen = iter_ansible_playbook(playbook_yaml, tgt)
        try:
            while True:
                line = next(gen)
                yield {"type": "line", "text": line}
        except StopIteration as ex:
            val = ex.value
            if not val:
                rc, out, err = 127, "", "ansible: нет результата"
            else:
                rc, out, err = val
        if rc == -1:
            results.append(
                {
                    "service_role": role,
                    "target_id": str(tgt.id),
                    "host": tgt.host,
                    "template_id": template_id,
                    "ok": False,
                    "ansible_rc": rc,
                    "stderr": _truncate_log(err),
                }
            )
            yield {
                "type": "phase_done",
                "step": "provision",
                "service_role": role,
                "template_id": template_id,
                "ansible_rc": rc,
                "ok": False,
            }
            continue
        ok = rc == 0
        row: dict = {
            "service_role": role,
            "target_id": str(tgt.id),
            "host": tgt.host,
            "template_id": template_id,
            "ansible_rc": rc,
            "ok": ok,
            "stdout": _truncate_log(out),
            "stderr": _truncate_log(err),
        }
        results.append(row)
        yield {
            "type": "phase_done",
            "step": "provision",
            "service_role": role,
            "template_id": template_id,
            "ansible_rc": rc,
            "ok": ok,
        }
        if ok:
            _ensure_infra_url_resource(db, project_id, role, ext, template_id)
            _pull_and_store_infra_credentials(db, project_id, tgt, role)
    db.commit()
    ok_n = sum(1 for r in results if r.get("ok") is True)
    err_n = sum(1 for r in results if r.get("error") or r.get("ok") is False)
    skip_n = sum(1 for r in results if r.get("skipped"))
    if skip_n == len(results) and ok_n == 0:
        st = "skipped"
    elif err_n == 0 and ok_n > 0:
        st = "ok"
    elif ok_n > 0:
        st = "partial"
    else:
        st = "error"
    summary = f"Ansible: успех {ok_n}, ошибок {err_n}, пропусков {skip_n}"
    resources_rows = (
        db.execute(
            select(ProjectInfraResource)
            .where(ProjectInfraResource.project_id == project_id)
            .order_by(ProjectInfraResource.updated_at.desc())
        )
        .scalars()
        .all()
    )
    out: dict[str, Any] = {
        "project_id": str(project_id),
        "results": results,
        "infra_resources": [_resource_public(r) for r in resources_rows],
        "note": "Плейбуки выполнены на стороне adapter; первый старт GitLab может занимать много минут.",
    }
    _emit_activity(
        project_id,
        act,
        "infra.run_stack",
        st,
        summary,
        {
            "results": results,
            "gitlab_port": body.gitlab_http_port,
            "nexus_port": body.nexus_http_port,
            "infra_resources": out["infra_resources"],
        },
    )
    yield {"type": "done", "payload": out, "status": st, "summary": summary}


@app.post("/api/v1/projects/{project_id}/infra/run-stack")
def run_infra_stack(
    project_id: UUID,
    body: PrepareStackBody,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict:
    """GitLab/Nexus: рендер шаблона оркестратором и реальный запуск ansible-playbook из adapter (SSH из БД)."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not ansible_playbook_available():
        raise HTTPException(
            status_code=503,
            detail="ansible-playbook недоступен (нет ansible-core в образе или PATH)",
        )
    out: dict[str, Any] | None = None
    for ev in _iter_infra_run_stack(db, project_id, body, act):
        if ev.get("type") == "done":
            out = ev.get("payload")
    if out is None:
        raise HTTPException(status_code=500, detail="run-stack: нет результата")
    return out


@app.post("/api/v1/projects/{project_id}/infra/run-stack-stream")
def run_infra_stack_stream(
    project_id: UUID,
    body: PrepareStackBody,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
):
    """Тот же run-stack, но поток NDJSON: phase / line / phase_done / done для live-логов в UI."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not ansible_playbook_available():
        raise HTTPException(
            status_code=503,
            detail="ansible-playbook недоступен (нет ansible-core в образе или PATH)",
        )

    def generate():
        for ev in _iter_infra_run_stack(db, project_id, body, act):
            yield json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.post("/api/v1/projects/{project_id}/infra/k8s-install-3node")
def k8s_install_3node(
    project_id: UUID,
    body: K8sInstall3Body,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict[str, Any]:
    """Kubeadm-кластер на три SSH-таргета (1 control plane + 2 worker), одинаковый ключ/пароль."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not ansible_playbook_available():
        raise HTTPException(
            status_code=503,
            detail="ansible-playbook недоступен (нет ansible-core в образе или PATH)",
        )
    out: dict[str, Any] | None = None
    for ev in _iter_k8s_install_3node(db, project_id, body, act):
        if ev.get("type") == "done":
            out = ev.get("payload") if isinstance(ev.get("payload"), dict) else None
    if out is None:
        raise HTTPException(status_code=500, detail="k8s-install-3node: нет результата")
    return out


@app.post("/api/v1/projects/{project_id}/infra/k8s-install-3node-stream")
def k8s_install_3node_stream(
    project_id: UUID,
    body: K8sInstall3Body,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
):
    """Тот же k8s-install, поток NDJSON (phase / line / done)."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not ansible_playbook_available():
        raise HTTPException(
            status_code=503,
            detail="ansible-playbook недоступен (нет ansible-core в образе или PATH)",
        )

    def generate():
        for ev in _iter_k8s_install_3node(db, project_id, body, act):
            yield json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.get("/api/v1/projects/{project_id}/k8s/clusters")
def list_k8s_clusters(project_id: UUID, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Таргеты проекта с kubeconfig (для UI и агентов)."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    rows = db.execute(
        select(ServerTarget)
        .where(ServerTarget.project_id == project_id, ServerTarget.target_kind == "kubernetes")
        .order_by(ServerTarget.created_at.desc())
    ).scalars().all()
    return [
        {
            "target_id": str(r.id),
            "host": r.host,
            "label": r.label,
            "kube_context": getattr(r, "kube_context", None),
        }
        for r in rows
    ]


@app.get("/api/v1/projects/{project_id}/k8s/operations")
def list_k8s_operations(project_id: UUID) -> dict[str, Any]:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"operations": sorted(ALLOWED_OPERATIONS)}


@app.post("/api/v1/projects/{project_id}/k8s/query")
def k8s_query_endpoint(project_id: UUID, body: K8sQueryBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Безопасное управление/наблюдение за кластером через kubectl в adapter."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not kubectl_available():
        raise HTTPException(status_code=503, detail="kubectl недоступен в образе adapter")
    row = db.get(ServerTarget, body.target_id)
    if not row or str(row.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Target not found")
    if getattr(row, "target_kind", "ssh") != "kubernetes":
        raise HTTPException(status_code=400, detail="Операция только для таргета Kubernetes (kubeconfig)")
    kube = decrypt_secret(row.credential_encrypted)
    kctx = getattr(row, "kube_context", None)
    return execute_k8s_operation(
        kube,
        kctx,
        body.operation,
        namespace=body.namespace,
        name=body.name,
        resource=body.resource,
        container=body.container,
        tail_lines=body.tail_lines,
        all_namespaces=body.all_namespaces,
        wide=body.wide,
        replicas=body.replicas,
    )


@app.get("/api/v1/projects/{project_id}/activity-logs")
def get_activity_logs(
    project_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    items = fetch_activity(project_id, limit=limit, offset=offset)
    return {
        "project_id": str(project_id),
        "items": items,
        "clickhouse": clickhouse_status(),
    }


@app.get("/api/v1/projects/{project_id}/development-context")
def get_development_context(project_id: UUID, db: Session = Depends(get_db)) -> dict:
    """Сводка для агентов: таргеты, назначения сервисов, ресурсы, системные промпты (без секретов)."""
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    targets = (
        db.execute(select(ServerTarget).where(ServerTarget.project_id == project_id).order_by(ServerTarget.created_at.desc()))
        .scalars()
        .all()
    )
    agg = _metrics_by_target(db, project_id)
    tid_host = {t.id: t.host for t in targets}
    assignments = db.execute(
        select(TargetServiceAssignment).where(TargetServiceAssignment.project_id == project_id)
    ).scalars().all()
    resources = db.execute(
        select(ProjectInfraResource).where(ProjectInfraResource.project_id == project_id)
    ).scalars().all()
    k8s_clusters = [
        {
            "target_id": str(t.id),
            "host": t.host,
            "label": t.label,
            "kube_context": getattr(t, "kube_context", None),
        }
        for t in targets
        if getattr(t, "target_kind", "ssh") == "kubernetes"
    ]
    ssh_rows = [t for t in targets if getattr(t, "target_kind", "ssh") == "ssh"]
    ssh_targets_for_tools = [
        {"target_id": str(t.id), "host": t.host, "label": t.label, "ssh_user": t.ssh_user}
        for t in ssh_rows
    ]
    return {
        "project_id": str(project_id),
        "agent_system_prompts": _fetch_dev_prompts_from_admin(),
        "targets": [
            _target_public(t, *agg.get(t.id, (0, None))).model_dump()
            for t in targets
        ],
        "kubernetes_clusters": k8s_clusters,
        "kubernetes_operations_hint": sorted(ALLOWED_OPERATIONS),
        # Явная подсказка для LLM: не путать «нет SSH target в проекте» с «у ассистента нет доступа к SSH».
        "ssh_tools": {
            "ssh_run_command_available": bool(ssh_targets_for_tools),
            "ssh_targets_for_tools": ssh_targets_for_tools,
            "instruction_ru": (
                "Если ssh_run_command_available=true — инструмент ssh_run_command доступен; передавай target_id из "
                "ssh_targets_for_tools или из targets (где target_kind=ssh). Платформа выполняет команду по "
                "сохранённым учётным данным проекта. Не утверждай, что «нет доступа к SSH у ассистента», если "
                "ssh_run_command_available=true. Если false — попроси пользователя добавить SSH-сервер в проект."
            ),
        },
        "ssh_remote_exec": {
            "enabled": True,
            "endpoint": "POST /api/v1/projects/{project_id}/targets/{target_id}/ssh-exec",
            "policy": "Классификация команды: read-only без подтверждения; остальное — needs_confirmation с HMAC-токеном; отдельные шаблоны блокируются.",
        },
        "service_assignments": [
            {
                "service_role": a.service_role,
                "target_id": str(a.target_id),
                "host": tid_host.get(a.target_id),
            }
            for a in assignments
        ],
        "infra_resources": [_resource_public(r) for r in resources if _is_resource_enabled(r)],
    }


@app.post("/api/v1/projects/{project_id}/agent-demo/run")
def post_agent_demo_run(
    project_id: UUID,
    body: AgentDemoRunBody | None = None,
    db: Session = Depends(get_db),
    act: ActivityContext = Depends(get_activity_context),
) -> dict[str, Any]:
    """
    Демо: PM / архитектор / backend / DevOps (через LLM-нарратив) + реальный GitLab: репозиторий, файлы, `.gitlab-ci.yml`.
    Нужен ресурс GitLab с PAT (после успешного provision GitLab для проекта).
    """
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    payload = body or AgentDemoRunBody()
    out = run_agent_demo_pipeline(
        db=db,
        project_id=project_id,
        tenant_id=_project_tenant_id(project_id),
        repo_slug=payload.repo_slug,
    )
    st = "ok" if out.get("ok") else "error"
    summ = (
        "Мультиагентное демо: репозиторий и CI в GitLab"
        if out.get("ok")
        else "Демо агентов: ошибка (см. steps)"
    )
    _emit_activity(project_id, act, "agent_demo.run", st, summ, out)
    return out


@app.post("/api/v1/internal/projects/{project_id}/agent-demo/run")
def internal_agent_demo_run(
    project_id: UUID,
    db: Session = Depends(get_db),
    body: AgentDemoRunBody | None = None,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    """Вызов из ai-team-worker / воркеров Kafka без JWT пользователя."""
    if (x_internal_token or "") != INTERNAL_SETTINGS_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    payload = body or AgentDemoRunBody()
    out = run_agent_demo_pipeline(
        db=db,
        project_id=project_id,
        tenant_id=_project_tenant_id(project_id),
        repo_slug=payload.repo_slug,
    )
    st = "ok" if out.get("ok") else "error"
    act = ActivityContext(actor="ai-team-worker", subject="internal")
    _emit_activity(
        project_id,
        act,
        "agent_demo.run",
        st,
        "ИИ Команда: внутренний запуск agent-demo",
        out,
    )
    return out
