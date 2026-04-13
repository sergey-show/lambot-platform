from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from shared.domain.project_status import ProjectStatus


class ProjectCreateRequest(BaseModel):
    name: str
    description: str
    tenant_id: UUID


class ProjectResponse(BaseModel):
    id: UUID
    name: str
    description: str
    tenant_id: UUID
    status: ProjectStatus
    suspended: bool = False
    deleted_at: datetime | None = None


class ProjectAdminPatchRequest(BaseModel):
    """Изменение проекта администратором (internal / admin API)."""

    name: str | None = None
    description: str | None = None
    suspended: bool | None = None
    deleted: bool | None = Field(
        default=None,
        description="True — мягкое удаление (deleted_at), False — восстановить",
    )


class ProjectStatusResponse(BaseModel):
    project_id: UUID
    status: ProjectStatus


class PlanningRunRequest(BaseModel):
    idea: str
    attachments: list[str] = Field(default_factory=list)
    tenant_id: UUID | None = None


class PlanningRunResponse(BaseModel):
    plan_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    summary: str
    artifacts: dict[str, Any] = Field(default_factory=dict)


class TransitionRequest(BaseModel):
    from_status: ProjectStatus
    to_status: ProjectStatus


class CatalogItem(BaseModel):
    id: str
    name: str
    category: str
    description: str


class LlmGenerateRequest(BaseModel):
    prompt: str
    model: str | None = None
    temperature: float = 0.2
    purpose: Literal["default", "coding"] = Field(
        default="default",
        description="default — общая модель из настроек; coding — модель для кода/агентов (если задана)",
    )


class LlmGenerateResponse(BaseModel):
    output: str
    provider: str


class LlmEmbedRequest(BaseModel):
    input: str | list[str]
    model: str | None = Field(
        default=None,
        description="Переопределение; иначе берётся embedding_model из admin / env",
    )


class LlmEmbedResponse(BaseModel):
    """Совместимо по смыслу с OpenAI embeddings: векторы по порядку input."""

    embeddings: list[list[float]]
    model: str
    provider: str
