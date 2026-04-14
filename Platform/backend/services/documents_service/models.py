from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from services.documents_service.db import Base


class ProjectDocument(Base):
    __tablename__ = "project_documents"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_project_document_slug"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    versions: Mapped[list["ProjectDocumentVersion"]] = relationship(
        "ProjectDocumentVersion",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    chunks: Mapped[list["ProjectDocumentChunk"]] = relationship(
        "ProjectDocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class ProjectDocumentVersion(Base):
    __tablename__ = "project_document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_version"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author_type: Mapped[str] = mapped_column(String(32), nullable=False, default="agent")
    author_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    document: Mapped["ProjectDocument"] = relationship("ProjectDocument", back_populates="versions")
    chunks: Mapped[list["ProjectDocumentChunk"]] = relationship(
        "ProjectDocumentChunk",
        back_populates="version_ref",
        cascade="all, delete-orphan",
    )


class ProjectDocumentChunk(Base):
    __tablename__ = "project_document_chunks"
    __table_args__ = (UniqueConstraint("document_version_id", "chunk_idx", name="uq_document_chunk_idx"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String(300), nullable=True)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    document: Mapped["ProjectDocument"] = relationship("ProjectDocument", back_populates="chunks")
    version_ref: Mapped["ProjectDocumentVersion"] = relationship(
        "ProjectDocumentVersion",
        back_populates="chunks",
    )
