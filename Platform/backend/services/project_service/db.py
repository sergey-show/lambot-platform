import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv(
    "PROJECT_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+psycopg://platform:platform@localhost:5432/project_db"),
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def _migrate_columns() -> None:
    """Add columns to an existing projects table (create_all does not alter schema)."""
    stmts = [
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    ]
    try:
        with engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))
    except Exception:
        pass


def init_db() -> None:
    import services.project_service.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
