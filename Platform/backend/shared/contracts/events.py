from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Actor(BaseModel):
    type: str = Field(description="user|agent|system")
    id: str


class EventMeta(BaseModel):
    schema_version: str = "1.0"
    source_service: str


class EventEnvelope(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    project_id: UUID
    tenant_id: UUID
    trace_id: UUID = Field(default_factory=uuid4)
    actor: Actor
    payload: dict[str, Any] = Field(default_factory=dict)
    meta: EventMeta


class EventTypes:
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_PLANNING_REQUESTED = "project.planning.requested"
    PROJECT_PLANNING_COMPLETED = "project.planning.completed"
    PROJECT_STATUS_CHANGE_REQUESTED = "project.status.change.requested"
    PROJECT_STATUS_CHANGED = "project.status.changed"
    INFRA_PROVISION_REQUESTED = "infra.provision.requested"
    INFRA_PROVISION_COMPLETED = "infra.provision.completed"
    CODEGEN_REQUESTED = "codegen.requested"
    CODEGEN_COMPLETED = "codegen.completed"
    DEPLOY_REQUESTED = "deploy.requested"
    DEPLOY_COMPLETED = "deploy.completed"
    PIPELINE_FAILED = "pipeline.failed"
    GITLAB_PROVISION_COMPLETED = "gitlab.provision.completed"
    GITLAB_PROVISION_FAILED = "gitlab.provision.failed"
    CODE_INDEX_REQUESTED = "code.index.requested"
    CODE_INDEX_COMPLETED = "code.index.completed"
    # AI agent worker (optional; публикуются оркестратором агентов при появлении)
    AGENT_TASK_QUEUED = "agent.task.queued"
    AGENT_TASK_FINISHED = "agent.task.finished"
