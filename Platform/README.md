# Platform

MVP scaffold for the multi-agent development platform described in `TZ.md`.

## Structure

- `backend` - Python microservices and shared contracts.
- `frontend` - TypeScript frontend skeleton for the operator UI.
- `agents` - reserved for future agent runtime/plugins.
- `bakend` - legacy folder kept as-is; do not use for new code.

## Current MVP services

- `project_service` - project lifecycle API.
- `planning_service` - AI planning trigger API.
- `orchestrator_service` - state machine transitions and event commands.
- `catalog_service` - stack/blueprint catalog API.
- `llm_adapter_service` - adapter to OpenAI-compatible APIs with local mock fallback.
- `admin_service` - superadmin portal API for global project oversight.
- `api_gateway` - single entrypoint with CORS and auth middleware.

## Quick start

Backend setup:

1. `cd backend`
2. `python -m venv .venv && source .venv/bin/activate`
3. `pip install -e .`
4. Run a service:
   - `uvicorn services.project_service.main:app --reload --port 8001`
   - `uvicorn services.planning_service.main:app --reload --port 8002`
   - `uvicorn services.orchestrator_service.main:app --reload --port 8003`
   - `uvicorn services.api_gateway.main:app --reload --port 8000`

Frontend setup:

1. `cd frontend`
2. `npm install`
3. `npm run dev`

## Docker compose (local platform)

From `Platform` folder:

1. `docker compose up -d --build`
2. **Демо-сценарий (UI):** Superadmin → создать пользователя (Portal users) и настроить LLM → `#/login` → User portal: проект → SSH → bundle ansible → после метрик агента или «Подтвердить настройку» открывается LLM-чат (planning).
3. Run E2E flow (старый dev-token):
   - `bash backend/scripts/e2e_flow.sh`

Optional env:

- copy `.env.example` to `.env` and set `OPENAI_API_KEY` for real LLM responses.
- set `SUPERADMIN_TOKEN` for admin API/UI access.
- set `JWT_SECRET`/`JWT_ALGORITHM` for JWT validation in gateway.

## Data isolation

Compose runs **four separate PostgreSQL instances** (four containers), each with its own database:

- `postgres-project` → `project_db` (projects, outbox, inbox)
- `postgres-planning` → `planning_db` (plan runs, outbox)
- `postgres-admin` → `admin_db` (admin projection, inbox)
- `postgres-orchestrator` → `orchestrator_db` (orchestrator outbox/inbox)

Cross-service writes are not allowed. Kafka integration uses the **outbox pattern** (write to `outbox_messages` in the same transaction as domain data, then a background publisher sends to Kafka) and the **inbox pattern** (dedupe by `event_id` before applying side effects).

Services:

- API Gateway: `http://localhost:8000`
- Project: `http://localhost:8001`
- Planning: `http://localhost:8002`
- Orchestrator: `http://localhost:8003`
- Catalog: `http://localhost:8004`
- LLM Adapter: `http://localhost:8005`
- Admin (superadmin): `http://localhost:8006`
- Ansible orchestrator (swarm templates): `http://localhost:8007`
- Adapter (SSH targets, agent ingest): `http://localhost:8008`

## E2E tests

From `Platform/backend`:

1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install -e .`
3. `pytest`

## Frontend routes

- `#/portal` - user portal (Owner/Admin/Viewer flows).
- `#/superadmin` - superadmin dashboard with filters, tables, and metrics.
