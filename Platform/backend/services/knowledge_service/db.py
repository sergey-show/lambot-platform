from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv(
    "KNOWLEDGE_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5438/knowledge_db",
)


class Base(DeclarativeBase):
    pass


engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _migrate_columns() -> None:
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE"
                )
            )
            conn.commit()
    except Exception:
        pass


def init_db() -> None:
    import services.knowledge_service.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
