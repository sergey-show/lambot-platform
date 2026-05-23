"""Трекер задач агентов: CRUD, dispatch → Kafka, завершение исполнения."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from services.admin_service.models import AdminProjectProjection, TrackerEvent, TrackerItem
from services.admin_service.tracker_kafka import publish_agent_task_envelope
from shared.contracts.agent_task import TaskEnvelope, TaskType

TRACKER_KIND_EPIC = "epic"
TRACKER_KIND_TASK = "task"

VALID_ASSIGNEE_ROLES = frozenset({"sa", "pm", "architect", "devops", "backend", "frontend", "qa"})

# Статусы карточки
S_BACKLOG = "backlog"
S_READY = "ready"
S_QUEUED = "queued"
S_IN_PROGRESS = "in_progress"
S_BLOCKED = "blocked"
S_DONE = "done"
S_FAILED = "failed"
S_CANCELLED = "cancelled"

VALID_STATUSES = frozenset(
    {
        S_BACKLOG,
        S_READY,
        S_QUEUED,
        S_IN_PROGRESS,
        S_BLOCKED,
        S_DONE,
        S_FAILED,
        S_CANCELLED,
    }
)

# Поля title/description/роль/seq/meta — только до постановки в очередь исполнения
EDITABLE_CONTENT_STATUSES = frozenset(
    {
        S_BACKLOG,
        S_READY,
        S_BLOCKED,
        S_FAILED,
    }
)

STEP_TRACKER_EXECUTE = "tracker_task_execute"


def agent_key_for_project_role(project_id: UUID, role: str) -> str:
    return f"agent:project:{project_id}:{role}"


def get_tenant_id_for_project(db: Session, project_id: UUID) -> UUID | None:
    row = db.execute(
        select(AdminProjectProjection.tenant_id).where(AdminProjectProjection.project_id == project_id)
    ).scalar_one_or_none()
    return UUID(str(row)) if row is not None else None


def serialize_item(row: TrackerItem) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "tenant_id": str(row.tenant_id),
        "kind": row.kind,
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "title": row.title,
        "description": row.description or "",
        "status": row.status,
        "assignee_agent_key": row.assignee_agent_key,
        "assignee_role": row.assignee_role,
        "seq": row.seq,
        "meta": row.meta if isinstance(row.meta, dict) else {},
        "last_execution_id": str(row.last_execution_id) if row.last_execution_id else None,
        "execution_count": row.execution_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_event(row: TrackerEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "item_id": str(row.item_id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "actor_type": row.actor_type,
        "actor_ref": row.actor_ref,
        "action": row.action,
        "payload": row.payload if isinstance(row.payload, dict) else {},
    }


def append_event(
    db: Session,
    *,
    project_id: UUID,
    item_id: UUID,
    actor_type: str,
    actor_ref: str,
    action: str,
    payload: dict[str, Any] | None = None,
) -> TrackerEvent:
    ev = TrackerEvent(
        project_id=project_id,
        item_id=item_id,
        actor_type=actor_type,
        actor_ref=actor_ref[:512],
        action=action,
        payload=payload or {},
    )
    db.add(ev)
    return ev


def list_items(db: Session, project_id: UUID, *, kind: str | None = None) -> list[TrackerItem]:
    cond = TrackerItem.project_id == project_id
    if kind:
        cond = and_(cond, TrackerItem.kind == kind)
    q = select(TrackerItem).where(cond).order_by(TrackerItem.seq, TrackerItem.created_at)
    return list(db.execute(q).scalars().all())


def get_item(db: Session, project_id: UUID, item_id: UUID) -> TrackerItem | None:
    return db.execute(
        select(TrackerItem).where(TrackerItem.project_id == project_id, TrackerItem.id == item_id)
    ).scalar_one_or_none()


def list_events(db: Session, project_id: UUID, item_id: UUID, *, limit: int = 100) -> list[TrackerEvent]:
    return list(
        db.execute(
            select(TrackerEvent)
            .where(TrackerEvent.project_id == project_id, TrackerEvent.item_id == item_id)
            .order_by(TrackerEvent.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def create_item(
    db: Session,
    *,
    project_id: UUID,
    kind: str,
    title: str,
    description: str = "",
    parent_id: UUID | None = None,
    assignee_role: str | None = None,
    status: str = S_BACKLOG,
    seq: int = 0,
    meta: dict[str, Any] | None = None,
    actor_ref: str = "user",
) -> TrackerItem:
    tenant_id = get_tenant_id_for_project(db, project_id)
    if tenant_id is None:
        raise ValueError("project_projection not found for project")

    if kind not in (TRACKER_KIND_EPIC, TRACKER_KIND_TASK):
        raise ValueError("invalid kind")
    if kind == TRACKER_KIND_EPIC and parent_id is not None:
        raise ValueError("epic must not have parent_id")
    if kind == TRACKER_KIND_TASK and parent_id is not None:
        parent = get_item(db, project_id, parent_id)
        if parent is None or parent.kind != TRACKER_KIND_EPIC:
            raise ValueError("task parent must be an epic in the same project")

    role = assignee_role.strip().lower() if assignee_role else None
    if role and role not in VALID_ASSIGNEE_ROLES:
        raise ValueError("invalid assignee_role")

    ak = agent_key_for_project_role(project_id, role) if role else None

    row = TrackerItem(
        project_id=project_id,
        tenant_id=tenant_id,
        kind=kind,
        parent_id=parent_id,
        title=title.strip()[:512],
        description=description or "",
        status=status,
        assignee_agent_key=ak,
        assignee_role=role,
        seq=seq,
        meta=meta if isinstance(meta, dict) else {},
    )
    db.add(row)
    db.flush()
    append_event(
        db,
        project_id=project_id,
        item_id=row.id,
        actor_type="user",
        actor_ref=actor_ref,
        action="created",
        payload={"kind": kind, "title": row.title},
    )
    db.commit()
    db.refresh(row)
    return row


def update_item(
    db: Session,
    project_id: UUID,
    item_id: UUID,
    *,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    assignee_role: str | None = None,
    seq: int | None = None,
    meta: dict[str, Any] | None = None,
    actor_ref: str = "user",
) -> TrackerItem | None:
    row = get_item(db, project_id, item_id)
    if row is None:
        return None

    content_change = any(v is not None for v in (title, description, assignee_role, seq, meta))
    if content_change and row.status not in EDITABLE_CONTENT_STATUSES:
        raise ValueError(
            "cannot edit fields while the task is queued, in progress, done, or cancelled; "
            "only closing (status=cancelled) is allowed"
        )

    if title is not None:
        row.title = title.strip()[:512]
    if description is not None:
        row.description = description
    if seq is not None:
        row.seq = seq
    if meta is not None:
        m = dict(row.meta or {})
        m.update(meta)
        row.meta = m

    if assignee_role is not None:
        raw = assignee_role.strip()
        r = raw.lower() if raw else None
        if r and r not in VALID_ASSIGNEE_ROLES:
            raise ValueError("invalid assignee_role")
        row.assignee_role = r
        row.assignee_agent_key = agent_key_for_project_role(project_id, r) if r else None

    prev_status = row.status
    if status is not None:
        st = status.strip().lower()
        if st not in VALID_STATUSES:
            raise ValueError("invalid status")
        if st != S_CANCELLED and row.status not in EDITABLE_CONTENT_STATUSES:
            raise ValueError(
                "cannot change status while the task is queued, in progress, done, or cancelled; "
                "use status=cancelled to close"
            )
        row.status = st

    row.updated_at = datetime.utcnow()
    evt_action = "updated"
    evt_payload: dict[str, Any] = {}
    if status is not None and row.status == S_CANCELLED and prev_status != S_CANCELLED:
        evt_action = "cancelled"
        evt_payload = {"previous_status": prev_status}
    elif status is not None:
        evt_payload = {"previous_status": prev_status, "new_status": row.status}

    append_event(
        db,
        project_id=project_id,
        item_id=item_id,
        actor_type="user",
        actor_ref=actor_ref,
        action=evt_action,
        payload=evt_payload,
    )
    db.commit()
    db.refresh(row)
    return row


def dispatch_item(
    db: Session,
    project_id: UUID,
    item_id: UUID,
    *,
    actor_ref: str = "user",
) -> tuple[TrackerItem, UUID]:
    """
    Ставит задачу в очередь исполнения и публикует TaskEnvelope (tracker_task_execute).
    Возвращает (item, execution_id).
    """
    row = get_item(db, project_id, item_id)
    if row is None:
        raise ValueError("not_found")
    if row.kind != TRACKER_KIND_TASK:
        raise ValueError("only tasks can be dispatched")
    if not row.assignee_role:
        raise ValueError("assignee_role required")
    if row.status == S_CANCELLED:
        raise ValueError("cannot dispatch cancelled task")

    if row.status not in (S_BACKLOG, S_READY, S_FAILED, S_BLOCKED):
        raise ValueError(f"cannot dispatch from status {row.status}")

    execution_id = uuid4()
    row.last_execution_id = execution_id
    row.execution_count = int(row.execution_count or 0) + 1
    row.status = S_QUEUED
    row.updated_at = datetime.utcnow()

    append_event(
        db,
        project_id=project_id,
        item_id=item_id,
        actor_type="user",
        actor_ref=actor_ref,
        action="dispatched",
        payload={"execution_id": str(execution_id)},
    )
    db.commit()
    db.refresh(row)

    tenant_id = row.tenant_id
    env = TaskEnvelope(
        task_id=execution_id,
        idempotency_key=str(execution_id),
        task_type=TaskType.EXECUTE,
        project_id=project_id,
        tenant_id=UUID(str(tenant_id)),
        step_name=STEP_TRACKER_EXECUTE,
        input_inline={
            "tracker_item_id": str(item_id),
            "execution_id": str(execution_id),
            "assignee_agent_key": row.assignee_agent_key,
            "assignee_role": row.assignee_role,
            "title": row.title,
            "description": row.description,
            "prompt": _build_tracker_prompt(row),
        },
    )

    try:
        publish_agent_task_envelope(env.model_dump(mode="json"))
    except Exception:
        row = get_item(db, project_id, item_id)
        if row:
            row.status = S_FAILED
            m = dict(row.meta or {})
            m["dispatch_error"] = "kafka_publish_failed"
            row.meta = m
            row.updated_at = datetime.utcnow()
            append_event(
                db,
                project_id=project_id,
                item_id=item_id,
                actor_type="system",
                actor_ref="tracker",
                action="dispatch_failed",
                payload={},
            )
            db.commit()
            db.refresh(row)
        raise

    db.refresh(row)
    return row, execution_id


def _build_tracker_prompt(row: TrackerItem) -> str:
    role = row.assignee_role or "developer"
    return (
        f"Ты агент роли «{role}» в проекте пользователя. Выполни задачу по сути, опираясь на контекст платформы.\n\n"
        f"Заголовок: {row.title}\n\n"
        f"Описание:\n{row.description or '(нет)'}\n"
    )


def patch_execution_result(
    db: Session,
    project_id: UUID,
    item_id: UUID,
    execution_id: UUID,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    actor_ref: str = "agent_worker",
) -> TrackerItem | None:
    """Вызывается из agent_worker после исполнения."""
    row = get_item(db, project_id, item_id)
    if row is None:
        return None
    if row.last_execution_id != execution_id:
        return None
    if row.status == S_CANCELLED:
        m = dict(row.meta or {})
        m["last_worker_result_after_cancel"] = {"status": status, "result": result, "error": error}
        row.meta = m
        row.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
        return row

    if status == "succeeded":
        row.status = S_DONE
    elif status == "failed":
        row.status = S_FAILED
    else:
        row.status = S_FAILED

    m = dict(row.meta or {})
    m["last_result"] = result or {}
    if error:
        m["last_error"] = error[:8000]
    row.meta = m
    row.updated_at = datetime.utcnow()

    append_event(
        db,
        project_id=project_id,
        item_id=item_id,
        actor_type="agent",
        actor_ref=actor_ref,
        action="execution_finished",
        payload={"status": status, "execution_id": str(execution_id)},
    )
    db.commit()
    db.refresh(row)
    return row


def mark_running(
    db: Session,
    project_id: UUID,
    item_id: UUID,
    execution_id: UUID,
    *,
    actor_ref: str = "agent_worker",
) -> TrackerItem | None:
    row = get_item(db, project_id, item_id)
    if row is None or row.last_execution_id != execution_id:
        return None
    if row.status == S_CANCELLED:
        return None
    row.status = S_IN_PROGRESS
    row.updated_at = datetime.utcnow()
    append_event(
        db,
        project_id=project_id,
        item_id=item_id,
        actor_type="agent",
        actor_ref=actor_ref,
        action="execution_started",
        payload={"execution_id": str(execution_id)},
    )
    db.commit()
    db.refresh(row)
    return row
