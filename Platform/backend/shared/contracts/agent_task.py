"""Контракты задач агентов (Kafka topic platform.agent.tasks)."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    EXECUTE = "execute"
    REVIEW = "review"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewVerdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_INFO = "needs_info"


class TaskEnvelope(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))
    task_type: TaskType
    project_id: UUID
    tenant_id: UUID
    trace_id: UUID = Field(default_factory=uuid4)
    parent_task_id: UUID | None = None
    dag_id: str | None = None
    step_name: str = ""
    input_ref: str | None = None
    input_inline: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    deadline_at_epoch: float | None = None


class TaskResult(BaseModel):
    task_id: UUID
    status: TaskStatus
    task_type: TaskType
    output_ref: str | None = None
    output_inline: dict[str, Any] = Field(default_factory=dict)
    review_verdict: ReviewVerdict | None = None
    review_notes: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    worker_id: str = "unknown"
