"""Task and result models for the orchestrator and workers."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    EXECUTE = "execute"  # executor: LLM + tools
    REVIEW = "review"  # reviewer: rubric, diff, policy


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
    """Queue message: one task for a worker."""

    task_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Idempotency key for safe retries",
    )
    task_type: TaskType
    project_id: UUID
    tenant_id: UUID
    trace_id: UUID = Field(default_factory=uuid4)
    parent_task_id: UUID | None = None
    """Parent task in the DAG (e.g. review → execute)."""

    dag_id: str | None = Field(default=None, description="Graph id within the stage")
    step_name: str = Field(description="Step name: plan_chunk, codegen_file, review_security, ...")

    input_ref: str | None = Field(
        default=None,
        description="Blob/S3/internal storage ref for input (prompt, context)",
    )
    input_inline: dict[str, Any] = Field(
        default_factory=dict,
        description="Small inline context (prefer not to put secrets here)",
    )

    required_capabilities: list[str] = Field(
        default_factory=list,
        description="Tags: ansible, python, security_review, ...",
    )
    deadline_at_epoch: float | None = None


class TaskResult(BaseModel):
    """Executor or reviewer response."""

    task_id: UUID
    status: TaskStatus
    task_type: TaskType

    output_ref: str | None = Field(default=None, description="Artifact: diff, yaml, log")
    output_inline: dict[str, Any] = Field(default_factory=dict)

    review_verdict: ReviewVerdict | None = Field(
        default=None,
        description="Only for task_type=review",
    )
    review_notes: str | None = None

    error_code: str | None = None
    error_detail: str | None = None

    worker_id: str = Field(default="unknown", description="Worker instance id")
