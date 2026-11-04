# Product Requirements — Lambot Platform (v1)

Draft requirements suitable for decomposition into epics and user stories.

## 1) Product goal

Build a **Development-as-a-Team** platform that replaces core IT-team functions:

- capture and formalize requirements;
- design and generate infrastructure;
- generate code for a chosen stack;
- CI/CD and deployment;
- ongoing support via a multi-agent control plane.

## 2) Target audience

- **Startups without a tech team** (turnkey MVP).
- **Founders / product managers** (fast idea-to-launch).
- **SMB companies** (automating routine development and DevOps).

## 3) End-to-end user journey

1. User signs up / signs in via SSO.
2. Creates a project.
3. Describes the idea; uploads screenshots / references.
4. Clicks **Think it through**:
   - AI produces concept, architecture plan, backlog, roadmap.
5. Chooses infrastructure target:
   - `own server` or `cloud`.
6. Platform generates IaC:
   - Terraform / Ansible depending on the target.
7. Base infrastructure is provisioned:
   - GitLab, Kubernetes, Vault.
8. User selects an application stack (from ready-made patterns).
9. Agents generate services, CI/CD, and deploys.
10. Platform deploys and provides access to the result.

## 4) MVP scope

**In MVP:**

- 1 cloud (e.g. AWS **or** Yandex Cloud) + 1 `own server` profile.
- 2–3 application stack templates.
- Base agent roles.
- Infrastructure generation and apply with validation.
- Backend / frontend generation + basic CI/CD.
- Public marketing site (`Site`) as a separate module.

**Out of MVP:**

- Fully autonomous self-healing SRE.
- Advanced FinOps and multi-cloud orchestration.
- Auto-refactoring of customer legacy / monoliths.

## 5) Functional requirements

### 5.1 Users and access

- Registration, login, SSO (OIDC / SAML).
- Roles: `Owner`, `Admin`, `Viewer`.
- Multi-project tenancy; access scoped by project.

### 5.2 Project workspace

- Create / edit projects.
- Store idea description and attachments (screenshots, documents).
- Requirements versioning (history of “Think it through” iterations).

### 5.3 AI planning

- Generate:
  - product concept,
  - technical architecture,
  - backlog (epics / features / tasks),
  - risks and estimates.
- Human edits + re-plan.

### 5.4 Infrastructure and IaC

- Target selection: `own server` / `cloud`.
- Terraform / Ansible generation.
- Required components: GitLab, K8s, Vault.
- Apply with logging and a rollback strategy.

### 5.5 Codegen and delivery

- Stack selection from templates / patterns.
- Generate service skeletons and CI/CD configs.
- Build, test, deploy into the provisioned cluster.
- Show pipeline and environment status.

### 5.6 Public site (`Site`)

- Separate directory / service for the marketing site.
- Platform content, case studies, CTAs, docs / demo.

## 6) Non-functional requirements

- **Architecture:** Python microservices, DDD.
- **Reliability:** retry, idempotency, DLQ for tasks.
- **Security:** Vault for secrets, RBAC, action audit.
- **Observability:** centralized logs, metrics, tracing.
- **Scalability:** horizontal scale of workers / agents.
- **Performance:** Kafka task queues, Redis cache.
- **Data:** PostgreSQL as source of truth.

## 7) Platform tech stack

- `Python` — backend / microservices.
- `TypeScript` — platform frontend.
- `PostgreSQL` — OLTP.
- `Redis` — cache, fast state.
- `Kafka` — async tasks / events.
- `LLM Adapter` — unified gateway to OpenAI-compatible APIs.
- `SSO` — OIDC / SAML provider.
- `Kubernetes` — platform runtime.

## 8) Microservice model (DDD bounded contexts)

- `Identity & Access BC` — users, SSO, roles, policies.
- `Project BC` — projects, artifacts, attachments, requirement versions.
- `Planning BC` — concept and backlog generation.
- `Agent Orchestration BC` — agent roles, task routing, state machine.
- `Infra BC` — IaC generate / validate / apply.
- `Codegen BC` — stack- and pattern-based code generation.
- `Delivery BC` — CI/CD orchestration, deploys, release status.
- `Catalog BC` — library of stacks and architecture patterns.

## 9) Agent roles (required model)

- `Product Analyst Agent` — turn idea into requirements.
- `Solution Architect Agent` — tech architecture and delivery plan.
- `Infra Architect Agent` — infra layout for the chosen target.
- `IaC Agent` — Terraform / Ansible generation and checks.
- `Backend Agent` — services, APIs, business logic.
- `Frontend Agent` — TypeScript UI.
- `DevOps Agent` — CI/CD, GitLab pipelines, deploy.
- `QA Agent` — automated tests, smoke / e2e, quality gates.
- `Security Agent` — secrets, policies, baseline security checks.

## 10) Patterns and stack packs

Catalog of **Blueprints**:

- `SaaS Web App`
- `Marketplace`
- `Internal Tool`
- `API-first backend`

Each blueprint includes:

- architecture diagram;
- supported languages / frameworks;
- infra profile;
- production-readiness checklist.

## 11) Queues and orchestration

- Kafka topics by pipeline stage:
  - `project.planning.requested`
  - `infra.provision.requested`
  - `codegen.requested`
  - `deploy.requested`
- Orchestrator keeps project state machine:
  - `IDEA -> PLAN_READY -> INFRA_READY -> CODE_READY -> DEPLOYED`.

## 12) Success metrics

- Time-to-first-deploy (goal: hours, not weeks).
- Share of projects reaching `DEPLOYED`.
- Average number of manual edits after generation.
- First-try success rate of IaC apply.
- NPS / user satisfaction.

## 13) Key risks

- Over-promising “full team replacement” too early.
- IaC failures across environments.
- Code quality without strict quality gates.
- Security and access in the self-hosted path.

## 14) Rollout phases

- **Phase 1 (MVP):** planning + 1 infra target + 1 stack + basic deploy.
- **Phase 2:** more stacks / patterns; stronger QA / Sec.
- **Phase 3:** semi-autonomous ops; cost / reliability optimization.

---

## 15) Epics and user stories (MVP)

### Epic A. Identity & Access

**Goal:** secure login, SSO, and role-based access.

- **US-A1:** As a user, I want to sign in via SSO so I do not create separate passwords.
  - **Acceptance Criteria:**
    - OIDC provider supported;
    - after login, user profile is created / updated;
    - session is valid and bound to tenant / project context.
- **US-A2:** As a project Owner, I want to assign `Admin` and `Viewer` roles.
  - **Acceptance Criteria:**
    - role changes via API / UI;
    - changes are audited;
    - unauthorized users get `403`.

### Epic B. Project Workspace

**Goal:** give the user a workspace for the idea.

- **US-B1:** As a user, I want to create a project with name and description.
  - **Acceptance Criteria:**
    - project is created in `IDEA` status;
    - project owner is assigned automatically.
- **US-B2:** As a user, I want to attach screenshots / files.
  - **Acceptance Criteria:**
    - jpg / png / pdf supported;
    - file size limits validated;
    - files linked to a requirements version.

### Epic C. AI Planning

**Goal:** get a formal delivery plan from an idea.

- **US-C1:** As a user, I click **Think it through** and receive concept + roadmap.
  - **Acceptance Criteria:**
    - `plan_version` artifact is created;
    - plan has sections: scope, architecture, backlog, risks;
    - project status becomes `PLAN_READY`.
- **US-C2:** As a user, I edit the plan and re-run planning.
  - **Acceptance Criteria:**
    - previous version is retained;
    - diff between plan versions is visible.

### Epic D. Infrastructure Provisioning (IaC)

**Goal:** provision infrastructure from templates.

- **US-D1:** As a user, I choose target `own server` or `cloud`.
  - **Acceptance Criteria:**
    - choice is stored on the project;
    - required environment fields are validated before start.
- **US-D2:** As a user, I want to generate and apply IaC.
  - **Acceptance Criteria:**
    - Terraform / Ansible package is generated;
    - GitLab, Kubernetes, Vault are created;
    - apply steps, errors, and final status are logged;
    - on failure, rollback runs or a safe fail-state is recorded.

### Epic E. Codegen & Delivery

**Goal:** generate the app for the chosen stack and deploy it.

- **US-E1:** As a user, I choose a blueprint and stack.
  - **Acceptance Criteria:**
    - at least 2 stack templates in MVP;
    - selection is stored in `Catalog BC` and the project.
- **US-E2:** As a user, I start codegen and CI/CD.
  - **Acceptance Criteria:**
    - backend and frontend skeletons created;
    - GitLab pipeline created;
    - basic tests run;
    - successful deploy moves status to `DEPLOYED`.

### Epic F. Public Site

**Goal:** platform promo site as a separate `Site` module.

- **US-F1:** As a visitor, I want to see platform value and a CTA.
  - **Acceptance Criteria:**
    - site available on a separate domain / subdomain;
    - sections: product, capabilities, case studies, CTA.

## 16) Domain events and Kafka schema (MVP)

### 16.1 Topics

- `project.planning.requested`
- `project.planning.completed`
- `infra.provision.requested`
- `infra.provision.completed`
- `codegen.requested`
- `codegen.completed`
- `deploy.requested`
- `deploy.completed`
- `pipeline.failed`

### 16.2 Base event format

```json
{
  "event_id": "uuid",
  "event_type": "project.planning.requested",
  "project_id": "uuid",
  "tenant_id": "uuid",
  "trace_id": "uuid",
  "timestamp": "2026-04-10T12:00:00Z",
  "actor": {
    "type": "user|agent|system",
    "id": "uuid-or-name"
  },
  "payload": {},
  "meta": {
    "schema_version": "1.0",
    "source_service": "planning-service"
  }
}
```

### 16.3 Event processing requirements

- consumers must be idempotent;
- messages validated against JSON schema;
- processing errors go to DLQ;
- retry policy: exponential backoff + max attempts.

## 17) Minimal microservices and API contracts (MVP)

### 17.1 Services

- `identity-service` (SSO, roles, sessions)
- `project-service` (projects, requirements, attachments)
- `planning-service` (LLM planning)
- `agent-orchestrator` (state machine and task routing)
- `infra-service` (IaC generation / apply)
- `codegen-service` (stack-based codegen)
- `delivery-service` (CI/CD + deploy orchestration)
- `catalog-service` (blueprints / patterns / stacks)
- `llm-adapter-service` (unified LLM API access)
- `site` (public marketing frontend)

### 17.2 Sample APIs (REST, v1)

- `POST /api/v1/projects` — create project
- `GET /api/v1/projects/{project_id}` — get project
- `POST /api/v1/projects/{project_id}/attachments` — upload attachments
- `POST /api/v1/projects/{project_id}/planning/run` — run **Think it through**
- `POST /api/v1/projects/{project_id}/infra/plan` — generate IaC
- `POST /api/v1/projects/{project_id}/infra/apply` — apply IaC
- `POST /api/v1/projects/{project_id}/codegen/run` — generate code
- `POST /api/v1/projects/{project_id}/deploy/run` — start deploy
- `GET /api/v1/projects/{project_id}/status` — state machine status

### 17.3 Project statuses (state machine)

- `IDEA`
- `PLAN_READY`
- `INFRA_IN_PROGRESS`
- `INFRA_READY`
- `CODE_IN_PROGRESS`
- `CODE_READY`
- `DEPLOY_IN_PROGRESS`
- `DEPLOYED`
- `FAILED`

## 18) System-level acceptance criteria (MVP)

- User completes SSO and creates a project in under 3 minutes.
- From an idea description, a structured plan is produced (scope / architecture / backlog / risks).
- For the chosen infra target, a valid IaC bundle is generated.
- Base infrastructure (GitLab + K8s + Vault) comes up automatically.
- For the chosen stack, backend / frontend and a CI/CD pipeline are generated.
- At least one Kubernetes deploy succeeds and is visible in the UI.
- All key steps write audit and technical logs.

## 19) Roadmap (8–12 weeks)

### Weeks 1–2: Foundation

- repositories, base microservice architecture, DDD boundaries;
- identity-service + SSO;
- project-service (project CRUD and statuses).

### Weeks 3–4: Planning

- llm-adapter-service;
- planning-service v1;
- UI flow: idea → **Think it through** → plan version.

### Weeks 5–6: Infrastructure

- infra-service: Terraform / Ansible generation;
- orchestrator + Kafka event integration;
- GitLab / K8s / Vault in a test environment.

### Weeks 7–8: Codegen & Delivery

- codegen-service (at least 2 blueprints);
- delivery-service (pipeline + deploy);
- end-to-end: idea to deploy.

### Weeks 9–10: Hardening

- observability, audit, retry / DLQ;
- security baseline and role checks;
- stabilization and bugfix.

### Weeks 11–12: Go-live preparation

- public `Site`;
- documentation, demo scenarios, onboarding;
- load / acceptance tests and MVP release.

## 20) Open decisions (fix before kickoff)

- First cloud provider for MVP.
- SSO provider (e.g. Keycloak / Auth0 / other).
- Base blueprints and their stacks (2–3 options).
- LLM cost policy (limits and quotas).
- SLA / SLO for the first release.
