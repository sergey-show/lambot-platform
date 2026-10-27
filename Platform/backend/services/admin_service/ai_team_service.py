"""CRUD и сериализация прогонов AI-команды (Postgres)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from services.admin_service.models import AITeamExecutionTask, AITeamRun, AITeamStep

# step_key, role_label, task_title
DEFAULT_PIPELINE: list[tuple[str, str, str]] = [
    ("intake", "Маршрутизация", "Режим full/direct и JSON orchestration_route"),
    ("pm", "PM", "Функциональные и нефункциональные требования; описание продукта"),
    ("architect", "Архитектор", "Архитектура, стек, этапы; задачи для DevOps, Backend, Frontend"),
    ("devops_plan", "DevOps", "План работ: инфраструктура, CI/CD, деплой — кто что и когда"),
    ("backend_plan", "Backend", "План API и сервисов — кто что и когда"),
    ("frontend_plan", "Frontend", "План UI/клиента — кто что и когда"),
    ("implementation", "Реализация", "Итоги и пояснения после фактического исполнения задач"),
    ("deploy", "Деплой", "Развёртывание на сервере (из контекста проекта)"),
    (
        "qa_validation",
        "QA",
        "Фактическая проверка после деплоя: smoke/regression, риски, решение GO/NO_GO",
    ),
    ("pm_summary", "PM — итог", "Отчёт пользователю: как попробовать продукт, URL, учётные записи"),
]


def create_run_with_steps(db: Session, project_id: UUID, user_message: str) -> UUID:
    rid = uuid4()
    now = datetime.utcnow()
    run = AITeamRun(
        id=rid,
        project_id=project_id,
        status="pending",
        phase="init",
        user_message=user_message.strip(),
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    for seq, (step_key, role_label, task_title) in enumerate(DEFAULT_PIPELINE):
        db.add(
            AITeamStep(
                id=uuid4(),
                run_id=rid,
                seq=seq,
                step_key=step_key,
                role_label=role_label,
                task_title=task_title,
                status="pending",
            )
        )
    db.commit()
    return rid


def get_run_by_id(db: Session, run_id: UUID) -> AITeamRun | None:
    return db.execute(
        select(AITeamRun)
        .where(AITeamRun.id == run_id)
        .options(selectinload(AITeamRun.steps), selectinload(AITeamRun.execution_tasks))
    ).scalar_one_or_none()


def get_latest_run_for_project(db: Session, project_id: UUID) -> AITeamRun | None:
    return db.execute(
        select(AITeamRun)
        .where(AITeamRun.project_id == project_id)
        .order_by(AITeamRun.created_at.desc())
        .limit(1)
        .options(selectinload(AITeamRun.steps), selectinload(AITeamRun.execution_tasks))
    ).scalar_one_or_none()


def update_run_fields(
    db: Session,
    run_id: UUID,
    *,
    status: str | None = None,
    phase: str | None = None,
    architecture_md: str | None = None,
    architecture_json: dict | None = None,
    final_report_md: str | None = None,
    error: str | None = None,
) -> None:
    run = db.get(AITeamRun, run_id)
    if run is None:
        return
    if status is not None:
        run.status = status
    if phase is not None:
        run.phase = phase
    if architecture_md is not None:
        run.architecture_md = architecture_md
    if architecture_json is not None:
        run.architecture_json = architecture_json
    if final_report_md is not None:
        run.final_report_md = final_report_md
    if error is not None:
        run.error = error
    run.updated_at = datetime.utcnow()
    db.commit()


def update_step(
    db: Session,
    run_id: UUID,
    step_key: str,
    *,
    status: str,
    output_md: str | None = None,
    structured: dict | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    row = db.execute(
        select(AITeamStep).where(AITeamStep.run_id == run_id, AITeamStep.step_key == step_key)
    ).scalar_one_or_none()
    if row is None:
        return
    row.status = status
    if output_md is not None:
        row.output_md = output_md
    if structured is not None:
        row.structured = structured
    if error is not None:
        row.error = error
    if started_at is not None:
        row.started_at = started_at
    if finished_at is not None:
        row.finished_at = finished_at
    r = db.get(AITeamRun, run_id)
    if r:
        r.updated_at = datetime.utcnow()
    db.commit()


def replace_execution_tasks(
    db: Session,
    run_id: UUID,
    items: list[dict],
) -> list[AITeamExecutionTask]:
    """Удаляет старые задачи исполнения и создаёт новые (для одного прогона)."""
    run = db.get(AITeamRun, run_id)
    if run is None:
        return []
    db.execute(delete(AITeamExecutionTask).where(AITeamExecutionTask.run_id == run_id))
    out: list[AITeamExecutionTask] = []
    now = datetime.utcnow()
    for seq, raw in enumerate(items):
        title = str(raw.get("title") or f"Задача {seq + 1}")[:512]
        action_kind = str(raw.get("action_kind") or raw.get("action") or "noop")[:64]
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else None
        plan_summary = raw.get("plan_summary")
        plan_summary = str(plan_summary)[:8000] if plan_summary is not None else None
        row = AITeamExecutionTask(
            id=uuid4(),
            run_id=run_id,
            seq=seq,
            title=title,
            action_kind=action_kind,
            payload=payload,
            plan_summary=plan_summary,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        out.append(row)
    run.updated_at = now
    db.commit()
    for r in out:
        db.refresh(r)
    return out


def append_execution_tasks(
    db: Session,
    run_id: UUID,
    items: list[dict],
) -> list[AITeamExecutionTask]:
    """Добавляет задачи исполнения в конец существующей очереди run (батчами)."""
    run = db.get(AITeamRun, run_id)
    if run is None:
        return []
    max_seq = db.execute(
        select(func.max(AITeamExecutionTask.seq)).where(AITeamExecutionTask.run_id == run_id)
    ).scalar_one()
    next_seq = (int(max_seq) + 1) if max_seq is not None else 0
    out: list[AITeamExecutionTask] = []
    now = datetime.utcnow()
    for idx, raw in enumerate(items):
        title = str(raw.get("title") or f"Задача {next_seq + idx + 1}")[:512]
        action_kind = str(raw.get("action_kind") or raw.get("action") or "noop")[:64]
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else None
        plan_summary = raw.get("plan_summary")
        plan_summary = str(plan_summary)[:8000] if plan_summary is not None else None
        row = AITeamExecutionTask(
            id=uuid4(),
            run_id=run_id,
            seq=next_seq + idx,
            title=title,
            action_kind=action_kind,
            payload=payload,
            plan_summary=plan_summary,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        out.append(row)
    run.updated_at = now
    db.commit()
    for r in out:
        db.refresh(r)
    return out


def update_execution_task(
    db: Session,
    task_id: UUID,
    *,
    status: str | None = None,
    result: dict | None = None,
    error: str | None = None,
    kafka_task_id: UUID | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> AITeamExecutionTask | None:
    row = db.get(AITeamExecutionTask, task_id)
    if row is None:
        return None
    if status is not None:
        row.status = status
    if result is not None:
        row.result = result
    if error is not None:
        row.error = error
    if kafka_task_id is not None:
        row.kafka_task_id = kafka_task_id
    if started_at is not None:
        row.started_at = started_at
    if finished_at is not None:
        row.finished_at = finished_at
    row.updated_at = datetime.utcnow()
    r = db.get(AITeamRun, row.run_id)
    if r:
        r.updated_at = row.updated_at
    db.commit()
    db.refresh(row)
    return row


def get_execution_task(db: Session, task_id: UUID) -> AITeamExecutionTask | None:
    return db.get(AITeamExecutionTask, task_id)


def serialize_run_public(run: AITeamRun) -> dict:
    steps = sorted(run.steps, key=lambda s: s.seq)
    exec_tasks = sorted(run.execution_tasks, key=lambda t: t.seq)
    return {
        "run_id": str(run.id),
        "project_id": str(run.project_id),
        "status": run.status,
        "phase": run.phase,
        "user_message": run.user_message,
        "architecture_md": run.architecture_md,
        "architecture_json": run.architecture_json,
        "final_report_md": run.final_report_md,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "steps": [
            {
                "step_key": s.step_key,
                "seq": s.seq,
                "role_label": s.role_label,
                "task_title": s.task_title,
                "status": s.status,
                "output": s.output_md,
                "structured": s.structured,
                "error": s.error,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
            }
            for s in steps
        ],
        "execution_tasks": [
            {
                "id": str(t.id),
                "seq": t.seq,
                "title": t.title,
                "action_kind": t.action_kind,
                "payload": t.payload,
                "plan_summary": t.plan_summary,
                "status": t.status,
                "result": t.result,
                "error": t.error,
                "kafka_task_id": str(t.kafka_task_id) if t.kafka_task_id else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            }
            for t in exec_tasks
        ],
    }
