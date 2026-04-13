from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.planning_service.db import Base
from shared.messaging.models import make_outbox_model


class PlanRun(Base):
    __tablename__ = "plan_runs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


OutboxMessage = make_outbox_model(Base)
