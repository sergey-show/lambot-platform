from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.adapter_service.db import Base


class ServerTarget(Base):
    """User server: SSH + agent token (secrets encrypted with Fernet)."""

    __tablename__ = "server_targets"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="ssh")  # ssh | kubernetes
    host: Mapped[str] = mapped_column(String(512), nullable=False)
    ssh_port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    ssh_user: Mapped[str] = mapped_column(String(256), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False)  # key | password | kubeconfig
    kube_context: Mapped[str | None] = mapped_column(String(256), nullable=True)
    credential_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    agent_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    agent_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False, default="default")
    setup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_provision_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_provision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_provision_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AgentMetric(Base):
    __tablename__ = "agent_metrics"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    target_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class TargetServiceAssignment(Base):
    """Which SSH target runs a service role (gitlab / nexus) in the project."""

    __tablename__ = "target_service_assignments"
    __table_args__ = (UniqueConstraint("project_id", "service_role", name="uq_project_service_role"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("server_targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class ProjectInfraResource(Base):
    """Summary of resources/accounts/URLs for development agent context."""

    __tablename__ = "project_infra_resources"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
