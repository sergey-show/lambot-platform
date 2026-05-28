from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from services.admin_service.db import Base
from shared.messaging.models import make_inbox_model

LLM_SETTINGS_SINGLETON_ID = 1
AGENT_SETTINGS_SINGLETON_ID = 1


class LlmAdapterSettings(Base):
    """Один ряд (id=1): конфигурация LLM для llm-adapter-service."""

    __tablename__ = "llm_adapter_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    openai_base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    default_model: Mapped[str] = mapped_column(String(128), nullable=False)
    coding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reranking_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AgentPlatformSettings(Base):
    """Публичный URL ingest для remote-агентов (должен быть доступен с пользовательских серверов)."""

    __tablename__ = "agent_platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_public_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class PlatformUser(Base):
    """Пользователь портала: один tenant на пользователя (демо-изоляция)."""

    __tablename__ = "platform_users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AgentDevelopmentPrompt(Base):
    """Системные промпты для агентов разработки по ролям (редактирует superadmin)."""

    __tablename__ = "agent_development_prompts"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AgentGraphBuiltinTool(Base):
    """Встроенные инструменты LangGraph-агента (вкл/выкл из админки)."""

    __tablename__ = "agent_graph_builtin_tools"

    tool_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AgentGraphHttpTool(Base):
    """Пользовательские GET-инструменты: URL-шаблон с {project_id}, только доверенные хосты."""

    __tablename__ = "agent_graph_http_tools"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    url_template: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AITeamRun(Base):
    """Состояние прогона AI-команды по проекту (оркестрация ролей + архитектура + итог)."""

    __tablename__ = "ai_team_runs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    phase: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    architecture_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    architecture_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    final_report_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    steps: Mapped[list["AITeamStep"]] = relationship(
        "AITeamStep",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    execution_tasks: Mapped[list["AITeamExecutionTask"]] = relationship(
        "AITeamExecutionTask",
        back_populates="run",
        cascade="all, delete-orphan",
    )


class AITeamStep(Base):
    __tablename__ = "ai_team_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_key", name="uq_ai_team_step_run_key"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_team_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    role_label: Mapped[str] = mapped_column(String(128), nullable=False)
    task_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    output_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["AITeamRun"] = relationship("AITeamRun", back_populates="steps")


class AITeamExecutionTask(Base):
    """Структурированная задача исполнения (GitLab, CI, деплой) — отдельно от LLM-текста шагов."""

    __tablename__ = "ai_team_execution_tasks"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_ai_team_exec_run_seq"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_team_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    kafka_task_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["AITeamRun"] = relationship("AITeamRun", back_populates="execution_tasks")


class AdminProjectProjection(Base):
    __tablename__ = "project_projection"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    suspended: Mapped[bool] = mapped_column(default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class TrackerItem(Base):
    """Эпик или задача в трекере агентов (по проекту); триггер исполнения через Kafka → agent_worker."""

    __tablename__ = "tracker_items"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracker_items.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="backlog")
    assignee_agent_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    assignee_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_execution_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    execution_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrackerEvent(Base):
    """Журнал событий по карточке трекера."""

    __tablename__ = "tracker_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracker_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


InboxMessage = make_inbox_model(Base)
