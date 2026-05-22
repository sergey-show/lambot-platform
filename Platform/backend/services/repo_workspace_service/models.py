from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.repo_workspace_service.db import Base


class RepoWorkspace(Base):
    __tablename__ = "repo_workspaces"
    __table_args__ = (
        UniqueConstraint("project_id", "repo_slug", name="uq_repo_workspace_project_slug"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    repo_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    path_with_namespace: Mapped[str] = mapped_column(String(512), nullable=False)
    gitlab_project_id: Mapped[int | None] = mapped_column(nullable=True)
    active_branch: Mapped[str] = mapped_column(String(256), nullable=False, default="main")
    last_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disk_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="empty")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
