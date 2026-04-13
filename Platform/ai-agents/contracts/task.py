"""Модели задач и результатов для оркестратора и воркеров."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    EXECUTE = "execute"  # исполнитель: LLM + tools
    REVIEW = "review"  # проверяющий: рубрика, diff, политика


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"


class ReviewVerdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_INFO = "needs_info"


class TaskEnvelope(BaseModel):
    """Сообщение очереди: одна задача для воркера."""

    task_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Ключ идемпотентности для безопасных повторов",
    )
    task_type: TaskType
    project_id: UUID
    tenant_id: UUID
    trace_id: UUID = Field(default_factory=uuid4)
    parent_task_id: UUID | None = None
    """Задача-родитель в DAG (например review → execute)."""

    dag_id: str | None = Field(default=None, description="Идентификатор графа внутри этапа")
    step_name: str = Field(description="Имя шага: plan_chunk, codegen_file, review_security, ...")

    input_ref: str | None = Field(
        default=None,
        description="Ссылка на blob/S3/внутренний storage с входом (промпт, контекст)",
    )
    input_inline: dict[str, Any] = Field(
        default_factory=dict,
        description="Небольшой inline-контекст (лучше не класть секреты)",
    )

    required_capabilities: list[str] = Field(
        default_factory=list,
        description="Теги: ansible, python, security_review, ...",
    )
    deadline_at_epoch: float | None = None


class TaskResult(BaseModel):
    """Ответ исполнителя или ревьюера."""

    task_id: UUID
    status: TaskStatus
    task_type: TaskType

    output_ref: str | None = Field(default=None, description="Артефакт: diff, yaml, лог")
    output_inline: dict[str, Any] = Field(default_factory=dict)

    review_verdict: ReviewVerdict | None = Field(
        default=None,
        description="Только для task_type=review",
    )
    review_notes: str | None = None

    error_code: str | None = None
    error_detail: str | None = None

    worker_id: str = Field(default="unknown", description="Ид экземпляра воркера")
