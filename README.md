# Lambot Platform

**Development-as-a-Team** — an AI-native harness that acts as a virtual IT team: requirements, architecture, infrastructure, codegen, CI/CD, and ops.

Lambot orchestrates multi-agent roles (SA, PM, Architect, DevOps, Backend, Frontend, QA) and executes on the user’s infrastructure (SSH, Kubernetes, Ansible, GitLab).

## Why Lambot

- Turn an idea into a structured plan (scope, architecture, backlog, risks).
- Provision base infrastructure (GitLab, K8s, Vault) via IaC.
- Generate and deploy application code from stack blueprints.
- Run a multi-agent pipeline with shared state, tools, and human-in-the-loop gates.

## Repository layout

```
.
├── Architecture.md      # Architecture overview (agents, services, flows)
├── TZ.md                # Product requirements (MVP epics & APIs)
├── schema.md            # High-level orchestration diagram
└── Platform/            # Runnable MVP (microservices + UI)
    ├── backend/         # Python / FastAPI services
    ├── frontend/        # TypeScript operator UI
    ├── ai-agents/       # Multi-agent execution notes & contracts
    ├── docs/            # Knowledge bases, GitLab, code index
    └── docker-compose.yml
```

## Documentation

| Doc | Description |
|-----|-------------|
| [TZ.md](./TZ.md) | Product requirements, epics, Kafka topics, roadmap |
| [Architecture.md](./Architecture.md) | Platform architecture, agent pipeline, tool plane |
| [Platform/README.md](./Platform/README.md) | Local run, services, ports |
| [Platform/docs/](./Platform/docs/) | Knowledge bases, GitLab platform, Qdrant code index |

## Quick start

```bash
cd Platform
cp .env.example .env   # set OPENAI_API_KEY, SUPERADMIN_TOKEN, JWT_SECRET as needed
docker compose up -d --build
```

Main entrypoints:

| Service | URL |
|---------|-----|
| API Gateway | http://localhost:8000 |
| Frontend | see `Platform/frontend` (`npm run dev`) |
| Admin API | http://localhost:8006 |

For detailed setup (venv, individual uvicorn services, E2E), see [Platform/README.md](./Platform/README.md).

## Core stack

- **Backend:** Python, FastAPI, DDD-oriented microservices
- **Frontend:** TypeScript SPA
- **Data:** PostgreSQL (per-service DBs), Redis, Kafka
- **AI:** LangGraph ReAct agents, LLM adapter (OpenAI-compatible)
- **Infra tools:** SSH, Ansible, Kubernetes, GitLab, Qdrant (RAG / code index)

## Agent pipeline (high level)

```
intake → SA → PM → Architect → parallel plans (DevOps / Backend / Frontend)
      → execution_plan → implementation → deploy → QA → pm_summary
```

Intake can also route to a **direct** single-role ReAct path (consultative or with live execution).

## License

TBD — add a license before publishing the public release.

## Contributing

Issues and PRs are welcome once the repository is public. Prefer small, focused changes with clear descriptions. Align with the bounded contexts described in `Architecture.md` and the MVP scope in `TZ.md`.
