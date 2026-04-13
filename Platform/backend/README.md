# Backend

Python microservice scaffold (DDD-oriented) for MVP.

## Services

- `services/project_service` - projects and basic status read/write.
- `services/planning_service` - request planning and return a draft plan.
- `services/orchestrator_service` - state transitions and command intake.
- `services/catalog_service` - stack and blueprint catalog.
- `services/llm_adapter_service` - OpenAI-compatible abstraction.
- `services/admin_service` - superadmin endpoints.
- `services/api_gateway` - single entrypoint, CORS and auth middleware.
- `services/ansible_orchestrator_service` - каталог Ansible «роев» (шаблоны), рендер playbook/inventory.
- `services/adapter_service` - целевой сервер пользователя (SSH), токен агента, приём метрик (`/api/v1/agent/ingest`).
- `services/agent_bootstrap_service` - Kafka: при `project.created` автоматически вызывает planning (IDEA → PLAN_READY), чтобы цепочка платформы стартовала.
- `services/agent_worker_service` - очередь `platform.agent.tasks`, опционально LLM.
- `services/sandbox_service` - выполнение ограниченного Python-кода для агентов (таймаут, временный каталог).
- `services/agent_graph_service` - **LangGraph** ReAct-агент с **tool calling**: инструменты ходят в adapter/project-service/sandbox; новые tools добавляются в `platform_tools.py`.

## Shared modules

- `shared/domain/project_status.py` - canonical project states.
- `shared/contracts/api.py` - common API DTOs.
- `shared/contracts/events.py` - common event envelope and event names.

## Data ownership

Each service uses its **own PostgreSQL database** (separate compose instances in dev: `project_db`, `planning_db`, `admin_db`, `orchestrator_db`). Tables live in the `public` schema unless you add more.

- `project-service` — `projects`, `outbox_messages`, `inbox_messages`.
- `planning-service` — `plan_runs`, `outbox_messages`.
- `orchestrator-service` — `inbox_messages`, `outbox_messages` (plus in-memory projection for reads).
- `admin-service` — `project_projection`, `inbox_messages`.

## Reliable messaging (outbox / inbox)

- **Outbox:** domain changes and `outbox_messages` rows are committed together; a background task publishes to Kafka and sets `published_at`.
- **Inbox:** incoming events are deduplicated by `event_id` (primary key) before side effects run, so retries or redelivery do not double-apply.

Services do not read or write each other's tables.

## Superadmin API

Direct `admin-service` calls use header `X-Superadmin-Token`.
Through `api-gateway`, use `Authorization: Bearer <jwt-or-superadmin-token>`.

- `GET /api/v1/admin/overview`
- `GET /api/v1/admin/projects`
- `GET /api/v1/admin/services/health`

## Kafka flow

- `project-service` publishes `project.created` and `project.status.changed`.
- `planning-service` publishes `project.planning.completed` and `project.status.change.requested` (`IDEA -> PLAN_READY`).
- `orchestrator-service` consumes `project.status.changed` and requests pipeline progression (`PLAN_READY -> INFRA_IN_PROGRESS`).
- `project-service` consumes `project.status.change.requested` and applies authoritative status changes.
- `admin-service` builds its projection by consuming `project.created` and `project.status.changed`.

## Metrics (Prometheus)

- Gateway: `GET /metrics`
- Orchestrator: `GET /metrics` (includes Kafka lag and processed event counters)
