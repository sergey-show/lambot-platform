import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv(
    "ADAPTER_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+psycopg://platform:platform@localhost:5432/adapter_db"),
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    import services.adapter_service.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE server_targets ADD COLUMN IF NOT EXISTS setup_confirmed_at TIMESTAMP WITHOUT TIME ZONE"
            )
        )
        conn.execute(
            text("ALTER TABLE server_targets ADD COLUMN IF NOT EXISTS last_provision_ok BOOLEAN")
        )
        conn.execute(
            text(
                "ALTER TABLE server_targets ADD COLUMN IF NOT EXISTS last_provision_at TIMESTAMP WITHOUT TIME ZONE"
            )
        )
        conn.execute(text("ALTER TABLE server_targets ADD COLUMN IF NOT EXISTS last_provision_log TEXT"))
        conn.execute(
            text(
                "ALTER TABLE server_targets ADD COLUMN IF NOT EXISTS target_kind VARCHAR(32) NOT NULL DEFAULT 'ssh'"
            )
        )
        conn.execute(text("ALTER TABLE server_targets ADD COLUMN IF NOT EXISTS kube_context VARCHAR(256)"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
