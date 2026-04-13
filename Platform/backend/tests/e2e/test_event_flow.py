import importlib
import os
import sys
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from testcontainers.kafka import KafkaContainer
from testcontainers.postgres import PostgresContainer

from shared.domain.project_status import ProjectStatus


def _ensure_databases(connection_url: str) -> None:
    """Create per-service databases on the same Postgres instance (E2E only)."""
    admin_url = connection_url.rsplit("/", 1)[0] + "/postgres"
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    names = ("project_db", "planning_db", "admin_db", "orchestrator_db")
    with engine.connect() as conn:
        for name in names:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": name},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))


def test_planning_to_orchestrator_kafka_flow():
    for key in list(sys.modules):
        if key.startswith(
            ("services.project_service", "services.planning_service", "services.admin_service", "services.orchestrator_service")
        ):
            sys.modules.pop(key, None)

    with PostgresContainer("postgres:16") as postgres, KafkaContainer() as kafka:
        database_url = postgres.get_connection_url().replace("psycopg2", "psycopg")
        _ensure_databases(database_url)
        base_url = database_url.rsplit("/", 1)[0]
        kafka_bootstrap = kafka.get_bootstrap_server()

        os.environ["PROJECT_DATABASE_URL"] = f"{base_url}/project_db"
        os.environ["PLANNING_DATABASE_URL"] = f"{base_url}/planning_db"
        os.environ["ADMIN_DATABASE_URL"] = f"{base_url}/admin_db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = f"{base_url}/orchestrator_db"
        os.environ["KAFKA_BOOTSTRAP_SERVERS"] = kafka_bootstrap
        os.environ["KAFKA_TOPIC_EVENTS"] = "platform.events"
        os.environ["KAFKA_GROUP_ORCHESTRATOR"] = "orchestrator-service-e2e"
        os.environ["SUPERADMIN_TOKEN"] = "superadmin-dev-token"
        os.environ["LLM_ADAPTER_URL"] = "http://localhost:9999"

        project_module = importlib.reload(importlib.import_module("services.project_service.main"))
        planning_module = importlib.reload(importlib.import_module("services.planning_service.main"))
        orchestrator_module = importlib.reload(importlib.import_module("services.orchestrator_service.main"))
        admin_module = importlib.reload(importlib.import_module("services.admin_service.main"))

        with TestClient(project_module.app) as project_client:
            with (
                TestClient(orchestrator_module.app),
                TestClient(planning_module.app) as planning_client,
                TestClient(admin_module.app) as admin_client,
            ):
                tenant_id = str(uuid4())
                create_resp = project_client.post(
                    "/api/v1/projects",
                    json={
                        "name": "e2e",
                        "description": "flow",
                        "tenant_id": tenant_id,
                    },
                )
                assert create_resp.status_code == 200
                project_id = create_resp.json()["id"]

                plan_resp = planning_client.post(
                    f"/api/v1/projects/{project_id}/planning/run",
                    json={"idea": "test kafka flow", "attachments": [], "tenant_id": tenant_id},
                )
                assert plan_resp.status_code == 200

                deadline = time.time() + 15
                status = None
                while time.time() < deadline:
                    status_resp = project_client.get(f"/api/v1/projects/{project_id}/status")
                    assert status_resp.status_code == 200
                    status = status_resp.json()["status"]
                    if status == ProjectStatus.INFRA_IN_PROGRESS:
                        break
                    time.sleep(0.5)
                assert status == ProjectStatus.INFRA_IN_PROGRESS

                overview = admin_client.get(
                    "/api/v1/admin/overview",
                    headers={"X-Superadmin-Token": "superadmin-dev-token"},
                )
                assert overview.status_code == 200
                assert overview.json()["projects_total"] >= 1
