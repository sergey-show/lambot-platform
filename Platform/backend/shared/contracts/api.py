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
    """Project update by an administrator (internal / admin API)."""

    name: str | None = None
    description: str | None = None
    suspended: bool | None = None
    deleted: bool | None = Field(
        default=None,
        description="True — soft delete (deleted_at), False — restore",
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
        description="default — shared model from settings; coding — model for code/agents (if set)",
    )


class LlmGenerateResponse(BaseModel):
    output: str
    provider: str


class LlmEmbedRequest(BaseModel):
    input: str | list[str]
    model: str | None = Field(
        default=None,
        description="Override; otherwise embedding_model from admin / env is used",
    )


class LlmEmbedResponse(BaseModel):
    """Compatible with OpenAI embeddings semantics: vectors in input order."""

    embeddings: list[list[float]]
    model: str
    provider: str


class LlmRerankDocument(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=8000)


class LlmRerankRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000)
    documents: list[LlmRerankDocument] = Field(default_factory=list, max_length=64)
    model: str | None = Field(
        default=None,
        description="Override; otherwise reranking_model from admin / env / default_model",
    )


class LlmRerankScore(BaseModel):
    id: str
    score: float = Field(..., ge=0.0, le=1.0)


class LlmRerankResponse(BaseModel):
    scores: list[LlmRerankScore]
    model: str
    provider: str
