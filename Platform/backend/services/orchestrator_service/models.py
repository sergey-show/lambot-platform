from shared.messaging.models import make_inbox_model, make_outbox_model

from services.orchestrator_service.db import Base

OutboxMessage = make_outbox_model(Base)
InboxMessage = make_inbox_model(Base)
