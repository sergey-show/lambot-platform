"""Factory helpers for per-service outbox/inbox tables (each service uses its own Base)."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


def make_outbox_model(base):
    class OutboxMessage(base):
        __tablename__ = "outbox_messages"

        id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
        topic: Mapped[str] = mapped_column(String(255), nullable=False, default="platform.events")
        payload: Mapped[str] = mapped_column(Text, nullable=False)
        created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
        published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    return OutboxMessage


def make_inbox_model(base):
    class InboxMessage(base):
        __tablename__ = "inbox_messages"

        event_id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True)
        payload: Mapped[str] = mapped_column(Text, nullable=False)
        received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
        processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    return InboxMessage
