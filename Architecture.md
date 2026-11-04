# Lambot Platform — Architecture Overview

The platform replaces an IT team for a founder: projects, product planning, an **AI team** (SA/PM/architect/DevOps/Backend/Frontend/QA roles), execution on the user’s infrastructure (SSH, K8s, Ansible), GitLab, wiki/documents, and knowledge bases.

Default deployment stack: `Platform/docker-compose.yml` (Python/FastAPI microservices + SPA + Kafka + several PostgreSQL instances).

---

## Architecture diagram: agents and components (for presentations)

Below are diagrams for slides and discussions with architects: the **virtual IT team (agents)**, the **orchestration engine**, **platform services**, and the **tool plane**.

### 1. Platform components and where agents sit

```mermaid
flowchart TB
  classDef agent fill:#e8f4fc,stroke:#1565c0,stroke-width:2px
  classDef engine fill:#fff3e0,stroke:#ef6c00,stroke-width:2px
  classDef domain fill:#f3e5f5,stroke:#7b1fa2
  classDef exec fill:#e8f5e9,stroke:#2e7d32
  classDef data fill:#eceff1,stroke:#546e7a

  subgraph PRES["Presentation"]
    UI["frontend<br/>Portal · Superadmin · AI-team · Tracker"]
  end

  subgraph EDGE["API & Security"]
    GW["api-gateway<br/>JWT · ACL · REST · WebSocket"]
  end

  subgraph DOMAIN["Domain & Control — microservices"]
    PRJ["project-service"]
    PLN["planning-service"]
    CAT["catalog-service"]
    ADM["admin-service<br/>runs · tracker · prompts · LLM config"]
    ORC["orchestrator-service<br/>project status"]
    BOOT["agent-bootstrap"]
  end

  subgraph AIENG["AI Engine — one runtime, many roles"]
    AG["agent-graph-service"]:::engine
    LLM["llm-adapter-service"]:::engine
    WRK["agent-worker-service"]:::engine
    DOC["documents-service"]
    KB["knowledge-service"]
  end

  subgraph AGENTS["Virtual IT Team — role agents (prompts + pipeline steps)"]
    direction TB
    A_SA["SA<br/>Systems Analyst"]:::agent
    A_PM["PM<br/>Product Manager"]:::agent
    A_AR["Architect"]:::agent
    A_DO["DevOps"]:::agent
    A_BE["Backend"]:::agent
    A_FE["Frontend"]:::agent
    A_QA["QA"]:::agent
  end

  subgraph TOOLS["Tool Plane — capabilities via LangGraph tools"]
    T_CTX["Context · status · metrics"]
    T_INFRA["SSH · K8s · Ansible"]
    T_CODE["GitLab · repo · code search"]
    T_DOC["Wiki · Knowledge RAG · Tracker"]
    T_AUX["Sandbox · Browser · HTTP"]
  end

  subgraph EXEC["Execution Adapters"]
    ADP["adapter-service"]
    GL["gitlab-service"]
    RWS["repo-workspace"]
    IDX["code-index"]
    ANS["ansible-orchestrator"]
    SBX["sandbox · browser"]
  end

  subgraph BUS["Integration"]
    KF[("Kafka")]
    RD[("Redis")]
  end

  subgraph DATA["Data"]
    PG[("PostgreSQL")]:::data
    QD[("Qdrant · S3")]:::data
    CH[("ClickHouse")]:::data
    LF["Langfuse"]
  end

  UI --> GW
  GW --> DOMAIN
  GW --> AIENG
  AG --> AGENTS
  AG --> LLM
  AG --> TOOLS
  AG --> ADM
  WRK --> AG
  WRK --> EXEC
  ADM --> KF
  AG --> KF
  KF --> WRK
  TOOLS --> EXEC
  EXEC --> DATA
  DOMAIN --> PG
  AIENG --> PG
  KB --> QD
  LLM --> LF
  AG --> LF
```

### 2. Multi-agent team: pipeline and modes

```mermaid
flowchart LR
  classDef step fill:#e3f2fd,stroke:#1976d2
  classDef gate fill:#fff8e1,stroke:#f9a825

  REQ["User request"] --> INTAKE["Intake<br/>route full / direct<br/>live_execution"]:::gate

  INTAKE -->|full pipeline| SA["SA"]:::step
  SA --> PM["PM"]:::step
  PM --> AR["Architect"]:::step

  AR --> PAR["In parallel"]
  PAR --> DO["DevOps plan"]:::step
  PAR --> BE["Backend plan"]:::step
  PAR --> FE["Frontend plan"]:::step

  DO --> EPLAN["execution_plan"]:::step
  BE --> EPLAN
  FE --> EPLAN

  EPLAN --> IMPL["implementation"]:::step
  IMPL --> DEP["deploy"]:::step
  DEP --> QA["QA validation"]:::step
  QA --> SUM["pm_summary"]:::step

  INTAKE -->|direct + 1 role| DIR["Single agent<br/>ReAct"]:::step
  DIR --> SUM2["pm_summary"]
  INTAKE -->|direct consultative| DIR2["ReAct without execution tail"]:::step
  DIR2 --> SUM2
```

**Intake modes (LLM decision, not keyword heuristics):**

| Mode | Agents | Execution |
|------|--------|-----------|
| `full` | SA → PM → Architect → plans → execution | DAG + worker tasks |
| `direct` + `live_execution: false` | One role | ReAct, facts from the server / consultation |
| `direct` + `live_execution: true` | One role + execution tail | ReAct + execution_plan → deploy |

### 3. Agents: responsibility zones and tools

| Agent | Pipeline steps | Zone | Typical tools |
|-------|----------------|------|---------------|
| **SA** | `sa` | Spec, glossary, traceability, epics/tasks in the tracker | `upsert_project_document`, `tracker_create_*`, `tracker_list_items` |
| **PM** | `pm`, `pm_summary` | Scope, KPI, stakeholder summary | context, wiki, `get_project_status` |
| **Architect** | `architect` | Stack, HLD, service boundaries | wiki, context, peer consult |
| **DevOps** | `devops_plan`, `execution_plan`, `implementation`, `deploy` | CI/CD, SSH, Ansible, K8s | `ssh_run_command`, `ansible_run_template`, `kubernetes_query`, GitLab |
| **Backend** | `backend_plan` | API, services, DB | `search_codebase`, `repo_*`, context |
| **Frontend** | `frontend_plan` | UI, web, charts | `repo_write_commit_push`, `browser_session`, context |
| **QA** | `qa_validation` | Smoke, GO/NO_GO | `browser_session`, SSH, metrics |

On tool-enabled steps, all roles use **one LangGraph ReAct runtime** (`agent-graph`) and a **shared platform tools catalog** (`tool_policy` per project).

### 4. Agent runtime: ReAct + orchestrator

```mermaid
flowchart TB
  subgraph RUNTIME["agent-graph-service"]
    ORCH["orchestration_runner<br/>steps · DoD · stream"]
    ROUTE["intake JSON<br/>orchestration_route"]
    GRAPH["LangGraph ReAct<br/>create_react_agent"]
    PT["platform_tools<br/>~25 tools"]
    ORCH --> ROUTE
    ORCH -->|step / invoke / tracker| GRAPH
    GRAPH --> PT
  end

  subgraph MODEL["llm-adapter-service"]
    CHAT["chat/completions"]
    GEN["generate"]
    COERCE["Ollama tool_calls coerce"]
  end

  subgraph STORE["admin-service · Postgres"]
    RUN["AITeamRun"]
    STEPS["AITeamStep"]
    ETASK["AITeamExecutionTask"]
    TRK["TrackerItem"]
  end

  subgraph WORKER["agent-worker-service"]
    W_TRK["tracker_task_execute<br/>→ /invoke"]
    W_EXEC["ai_team tasks<br/>ssh · k8s · ansible"]
  end

  ORCH <--> STORE
  GRAPH --> MODEL
  ORCH -->|publish tasks| KF[("Kafka")]
  KF --> WORKER
  WORKER --> GRAPH
  WORKER --> ADP["adapter-service"]
  W_EXEC --> ETASK
  TRK --> W_TRK
```

### 5. Three channels for interacting with agents

```mermaid
flowchart LR
  U((User))

  subgraph C1["① Chat / Copilot"]
    C1A["agent-graph /invoke"]
    C1B["ReAct + tools"]
  end

  subgraph C2["② AI team"]
    C2A["orchestrate/start"]
    C2B["Role DAG + DoD"]
    C2C["execution_tasks → worker"]
  end

  subgraph C3["③ Task tracker"]
    C3A["admin dispatch"]
    C3B["Kafka → worker"]
    C3C["worker → /invoke"]
  end

  U --> UI["Portal UI"]
  UI --> C1
  UI --> C2
  UI --> C3
  C1 --> C1B
  C2 --> C2B --> C2C
  C3 --> C3B --> C3C --> C1B
```

### 6. Tool plane (capability groups)

```mermaid
mindmap
  root((Platform Tools))
    Context
      get_development_context
      get_project_status
      list_infra_resources
      get_activity_logs
      get_agent_metrics
    Infrastructure
      ssh_run_command
      kubernetes_query
      ansible_run_template
    Code and Git
      gitlab_*
      repo_clone_or_sync
      repo_read_file
      repo_write_commit_push
      search_codebase
    Knowledge
      get_project_docs_context
      upsert_project_document
      search_knowledge
    Product
      run_product_planning
      tracker_create_epic
      tracker_create_task
    Auxiliary
      run_sandbox_python
      browser_session
      http_request
```

---

## How agents communicate with each other

Agents **do not maintain a persistent group chat** and **do not call each other as separate LangGraph instances**. Coordination goes through the **orchestrator** and **shared run state** (a “blackboard” pattern).

### Orchestration with shared state and failback control

```mermaid
flowchart TB
  classDef control fill:#fff3e0,stroke:#ef6c00,stroke-width:2px
  classDef state fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
  classDef agent fill:#e3f2fd,stroke:#1976d2,stroke-width:2px
  classDef fail fill:#ffebee,stroke:#c62828,stroke-width:2px

  USER["User request"]
  INTAKE["Intake + policy<br/>route/full/direct · live_execution"]:::control
  ORCH["orchestration_runner"]:::control
  STATE[("Shared Run State<br/>AITeamRun + steps + execution_tasks<br/>status · retries · evidence · gates")]:::state

  subgraph TEAM["Role agents"]
    SA["SA"]:::agent
    PM["PM"]:::agent
    AR["Architect"]:::agent
    DO["DevOps"]:::agent
    BE["Backend"]:::agent
    FE["Frontend"]:::agent
    QA["QA"]:::agent
  end

  TOOLS["ReAct tools<br/>SSH · GitLab · K8s · Docs · KB"]:::control
  WQ["Worker queue (Kafka)"]:::control
  WRK["agent-worker"]:::control

  FB["Failback controller<br/>DoD gate · retry policy · rollback policy"]:::fail
  RETRY["Auto retry with debug_hint<br/>exp backoff · max attempts"]:::fail
  ROLLBACK["Compensation / rollback task<br/>if side effects were already applied"]:::fail
  ESC["Escalation: questions_for_user<br/>or manual approve gate"]:::fail
  DONE["Run done / pm_summary"]:::state

  USER --> INTAKE --> ORCH
  ORCH --> STATE
  ORCH --> SA --> STATE
  ORCH --> PM --> STATE
  ORCH --> AR --> STATE
  ORCH --> DO --> STATE
  ORCH --> BE --> STATE
  ORCH --> FE --> STATE
  ORCH --> QA --> STATE

  ORCH --> TOOLS
  ORCH --> WQ --> WRK --> STATE

  ORCH --> FB
  FB -->|DoD passed| DONE
  FB -->|step/tool failed| RETRY
  RETRY --> ORCH
  FB -->|partial side-effect| ROLLBACK
  ROLLBACK --> WRK
  FB -->|attempts exhausted / high risk| ESC
  ESC --> ORCH
```

**How to read the diagram:**

- All agents read and write a single `Shared Run State` (blackboard): the source of truth for steps, artifacts, execution tasks, evidence, and retry counts.
- The orchestrator makes decisions only via policy + state (not local agent heuristics): start a step, transition, pause, finish.
- A failback loop is built into every step: `DoD gate` → retry → rollback/compensation → escalation to the user.
- Worker task execution is controlled via `execution_tasks` with statuses (`queued/running/done/failed`) and `tool_verified` confirmation.
- After retries are exhausted or for high-risk changes, a `manual approve gate` is enabled (user continue), then the run resumes from the same state.

### Ways of “communication”

| Method | When | How |
|--------|------|-----|
| **1. Accumulated context** | Every next pipeline step | The system prompt includes `### PM`, `### Architect`, … — outputs of all completed steps (`accumulated`) |
| **2. Peer queries** | A step returned `peer_queries` in JSON | The orchestrator synchronously asks a “colleague” (a separate LLM call with the peer role) and appends the answer to `summary_md` |
| **3. Parallel plans** | devops / backend / frontend at once | They start from **one snapshot** of `accumulated`; on retry failure — a `debug_hint` with outputs from peers that already finished |
| **4. Artifacts** | SA, wiki, tracker | Wiki documents, epics/tasks in the tracker with `assignee_role` — **asynchronous** handoff of work to another role |
| **5. Execution tasks** | After `execution_plan` | Shared task list in `AITeamRun.execution_tasks` — workers execute; all steps can see statuses |
| **6. ReAct tools** | Inside a single `/invoke` call | One agent may call `tracker_list_items`, `get_project_docs_context` — this is access to **data**, not a dialog with another agent |

**There is no:** direct message-passing between two ReAct graphs, shared memory between parallel steps in real time, or a separate “agent manager” LLM.

### Exchange flow in the AI team

```mermaid
sequenceDiagram
  participant O as orchestration_runner
  participant A as Step N agent
  participant B as Peer role agent
  participant DB as admin Postgres
  participant W as agent-worker

  O->>DB: steps 1..N-1 done
  O->>A: system + accumulated + role prompt
  A-->>O: JSON envelope summary_md peer_queries
  alt peer_queries is not empty
    loop up to MAX_PEER_QUERIES
      O->>B: _consult_peer(role, question)
      B-->>O: reply to colleague (not to user)
      O->>O: append to summary_md
    end
  end
  O->>DB: PATCH step N
  O->>A: next step N+1 sees accumulated

  Note over O,W: execution_plan
  O->>DB: execution_tasks
  O->>W: Kafka
  W-->>DB: task statuses
  O->>A: implementation/deploy read execution_tasks
```

### Peer sync (detail)

In step JSON an agent may specify:

```json
"peer_queries": [{"role": "backend", "question": "Which endpoints already exist in the API?"}]
```

The orchestrator calls `_consult_peer`: a separate chat with the **backend role prompt**, the same **project context** and **accumulated steps**, and the instruction “answer a colleague, not the user.” The reply is inserted into a “Team sync (auto)” block in the step result.

Peer roles: `pm`, `architect`, `devops`, `backend`, `frontend` (not `qa` / `sa` in the `PRIMARY_ROLE_TO_STEP` mapping).

### Parallel plans

```mermaid
flowchart LR
  ACC[accumulated snapshot<br/>at architect done]
  ACC --> D[devops_plan]
  ACC --> B[backend_plan]
  ACC --> F[frontend_plan]
  D --> MERGE[next step sees<br/>all three summaries]
  B --> MERGE
  F --> MERGE
```

While plans run in parallel, they **do not learn about each other mid-flight** — only after write-back into `accumulated`.

### Tracker vs AI team

| Channel | Meaning |
|---------|---------|
| **AI team** | One run, one step stream, shared `run_id` |
| **Tracker** | SA (or the user) creates tasks with `assignee_role`; execution is a **separate** Kafka → worker → ReAct path; linkage to a run is only via `meta_json` / description, not live chat between agents |

### With the user (not between agents, but important)

- `questions_for_user` + `pipeline_continue: false` → pause the run; the user’s answer goes to `orchestrate/continue`.
- Tool-calling confirmation (“yes, execute”) → clears a gate; it is not a peer dialog.

---

## Functional platform diagram

Functional view: **what the platform does for the business**, who consumes each function, and **which end-to-end scenarios** connect the blocks (without tying to ports and service names).

### F1. Function map (top level)

```mermaid
flowchart TB
  classDef gov fill:#fce4ec,stroke:#c2185b
  classDef prod fill:#e8eaf6,stroke:#3949ab
  classDef eng fill:#e0f2f1,stroke:#00695c
  classDef ops fill:#fff3e0,stroke:#e65100
  classDef know fill:#f3e5f5,stroke:#6a1b9a

  LP["Lambot Platform<br/>virtual IT team for the founder"]

  LP --> GOV["Platform governance"]:::gov
  LP --> PROD["Product and project"]:::prod
  LP --> ENG["Engineering with AI agents"]:::eng
  LP --> OPS["Operations and infrastructure"]:::ops
  LP --> KNOW["Knowledge and artifacts"]:::know

  GOV --> GOV1["Users · roles · tenants"]
  GOV --> GOV2["LLM settings and agent prompts"]
  GOV --> GOV3["Tool policies · superadmin"]

  PROD --> PROD1["Project creation · stack catalog"]
  PROD --> PROD2["Product planning IDEA → PLAN"]
  PROD --> PROD3["Project lifecycle status"]

  ENG --> ENG1["Request intake routing"]
  ENG --> ENG2["Formal SA specification"]
  ENG --> ENG3["Architecture and role plans"]
  ENG --> ENG4["Code implementation in GitLab"]
  ENG --> ENG5["End-to-end AI-team run"]
  ENG --> ENG6["Interactive ReAct copilot"]

  OPS --> OPS1["Deploy and changes on servers"]
  OPS --> OPS2["SSH / K8s diagnostics"]
  OPS --> OPS3["Ansible marketplace"]
  OPS --> OPS4["Agent metrics and logs"]
  OPS --> OPS5["Tracker task enqueue and execution"]

  KNOW --> KNOW1["Project wiki"]
  KNOW --> KNOW2["Knowledge base RAG"]
  KNOW --> KNOW3["Epic and task tracker"]
  KNOW --> KNOW4["Codebase search"]
```

### F2. Actors and functions

```mermaid
flowchart LR
  subgraph ACTORS["Actors"]
    F((Founder / Owner))
    M((Member / Viewer))
    S((Superadmin))
  end

  subgraph FUN["Key functions"]
    P1["Project and planning"]
    P2["AI team · chat"]
    P3["Agent tracker"]
    P4["Infra · Git · deploy"]
    P5["Administration"]
  end

  F --> P1
  F --> P2
  F --> P3
  F --> P4
  M --> P2
  M --> P3
  S --> P5
  S --> P2
```

| Function | Founder | Member | Superadmin | UI implementation |
|----------|:-------:|:------:|:----------:|-------------------|
| Create project, choose stack | ✓ | — | ✓ | Portal → projects |
| Product planning | ✓ | view | ✓ | Planning |
| Start AI team, stream the run | ✓ | ✓ | ✓ | AI-team |
| Copilot / one-shot ReAct | ✓ | ✓ | — | Agent graph chat |
| Tracker: epics, tasks, dispatch | ✓ | ✓ | — | Agent tasks |
| SSH targets, GitLab, context | ✓ | limited | ✓ | Project settings |
| Agent prompts, LLM, tools policy | — | — | ✓ | Superadmin |

### F3. End-to-end functional scenarios

```mermaid
flowchart TB
  classDef user fill:#bbdefb
  classDef func fill:#c8e6c9

  subgraph S1["Scenario A — New product from scratch"]
    U1["Product idea"]:::user --> F1A["Planning: MVP structure"]:::func
    F1A --> F1B["Project creation + bootstrap"]
    F1B --> F1C["AI team full: SA → … → pm_summary"]
    F1C --> F1D["Artifacts: wiki · tracker · Git · deploy"]
  end

  subgraph S2["Scenario B — Operational question"]
    U2["How much disk / uptime / OS version?"]:::user --> F2A["Intake: direct · live_execution=false"]
    F2A --> F2B["DevOps ReAct: context + SSH"]
    F2B --> F2C["Fact-based answer · no extra deploy"]
  end

  subgraph S3["Scenario C — Tracker task"]
    U3["Card with assignee_role"]:::user --> F3A["Dispatch to queue"]
    F3A --> F3B["Worker → ReAct /invoke"]
    F3B --> F3C["DoD: tool_verified"]
    F3C --> F3D["DONE / FAILED in UI"]
  end

  subgraph S4["Scenario D — Change and release"]
    U4["Build the site / deploy / hotfix"]:::user --> F4A["Intake: full or direct + live_execution"]
    F4A --> F4B["Plans · execution_plan"]
    F4B --> F4C["Worker: SSH · Ansible · Git"]
    F4C --> F4D["QA GO/NO_GO · pm_summary"]
  end
```

### F4. Functions ↔ agents (responsibility matrix)

```mermaid
flowchart LR
  subgraph FN["Functional blocks"]
    direction TB
    f_spec["Specification"]
    f_arch["Architecture"]
    f_code["Code"]
    f_infra["Infra · deploy"]
    f_qa["Acceptance"]
    f_prod["Product · summary"]
  end

  subgraph AG["Agents"]
    direction TB
    a_sa[SA]
    a_ar[Architect]
    a_be[Backend]
    a_fe[Frontend]
    a_do[DevOps]
    a_qa[QA]
    a_pm[PM]
  end

  a_sa --> f_spec
  a_ar --> f_arch
  a_be --> f_code
  a_fe --> f_code
  a_do --> f_infra
  a_qa --> f_qa
  a_pm --> f_prod
  f_spec --> f_arch
  f_arch --> f_code
  f_arch --> f_infra
  f_code --> f_infra
  f_infra --> f_qa
  f_qa --> f_prod
```

| Functional block | Lead agent | Support | User-facing output |
|------------------|------------|---------|--------------------|
| Spec, traceability, tracker tasks | SA | PM | Wiki documents, tracker cards |
| Stack, system boundaries | Architect | SA, PM | Architecture doc |
| API and backend | Backend | Architect, DevOps | Plan + commits |
| UI and frontend | Frontend | Architect | Plan + commits + browser smoke |
| CI/CD, servers, K8s | DevOps | Architect | execution_tasks, deploy |
| Release verification | QA | DevOps | GO / NO_GO |
| Stakeholder report | PM | all steps | pm_summary |

### F5. Data flows by function (simplified)

```mermaid
flowchart LR
  subgraph IN["Input"]
    I1[Request text]
    I2[KB files]
    I3[Project config SSH GitLab]
  end

  subgraph PROC["Processing"]
    P1[Intake routing]
    P2[Role steps + ReAct]
    P3[Execution plan JSON]
    P4[Execution workers]
  end

  subgraph OUT["Output"]
    O1[Chat / stream reply]
    O2[Wiki · tracker]
    O3[Git repository]
    O4[Server state]
    O5[Langfuse metrics]
  end

  I1 --> P1 --> P2
  P2 --> O1
  P2 --> O2
  P2 --> P3 --> P4
  P4 --> O3
  P4 --> O4
  I2 --> P2
  I3 --> P2
  P2 --> O5
```

### F6. Function boundaries: inside / outside

| Inside the platform | Outside (user responsibility) |
|---------------------|-------------------------------|
| Agent orchestration, LLM, DoD, tracker | Business decisions, product priorities |
| SSH/K8s/Ansible calls via adapter | The servers themselves, network, firewall, kubeconfig |
| GitLab provision, clone, commit | Prod GitLab contents, human code review |
| Wiki, KB, runs storage in Postgres | Legal compliance for data in the user’s cloud |
| Product planning as a draft plan | Approving scope and budget |

---

## High-level diagram (layers)

```mermaid
flowchart TB
  subgraph L0["Users"]
    U[Founder / team / superadmin]
  end

  subgraph L1["Edge"]
    UI[frontend SPA]
    GW[api-gateway JWT + proxy]
  end

  subgraph L2["Platform domain"]
    DOM[project · planning · catalog · admin<br/>tracker · AI-team runs · LLM settings]
    ORC[orchestrator — project status from events]
  end

  subgraph L3["Intelligence"]
    AG[agent-graph — team orchestration + ReAct tools]
    LLM[llm-adapter — models + Langfuse]
    WRK[agent-worker — Kafka execution]
    KB[documents · knowledge RAG]
  end

  subgraph L4["Infrastructure actions"]
    ADP[adapter SSH / HTTP / metrics]
    GIT[gitlab + repo-workspace + code-index]
    ANS[ansible-orchestrator]
    SBX[sandbox · browser Playwright]
  end

  subgraph L5["Bus and data"]
    KF[(Kafka)]
    PG[(PostgreSQL × N)]
    VEC[(Qdrant · S3 · ClickHouse)]
  end

  U --> UI --> GW
  GW --> DOM
  GW --> AG
  DOM --> KF
  ORC --> KF
  AG --> LLM
  AG --> DOM
  AG --> ADP
  AG --> GIT
  AG --> KB
  WRK --> AG
  WRK --> ADP
  DOM --> WRK
  ADP --> ANS
  GIT --> VEC
  KB --> VEC
  KF --> ORC
  KF --> WRK
  DOM --> PG
  AG --> PG
```

**Three entry points for agents:**

| Entry | Path | Result |
|-------|------|--------|
| **Chat / ReAct** | UI → gateway → `agent-graph` `/invoke` | One LangGraph run + tools |
| **AI team** | UI → `agent-graph` `/orchestrate` ↔ `admin` runs | Multi-step role DAG + execution tasks |
| **Tracker** | UI → `admin` dispatch → Kafka → `agent-worker` → `agent-graph` `/invoke` | Task card with tool-calling verification |

---

## Platform diagram (detailed)

```mermaid
flowchart TB
  subgraph users["Users"]
    U[Founder / Owner / Admin / Viewer]
    SA_UI[Superadmin]
  end

  subgraph edge["Edge"]
    FE[frontend :5173<br/>SPA + nginx]
    GW[api-gateway :8000<br/>JWT, API routing]
  end

  U --> FE
  SA_UI --> FE
  FE -->|/api/* REST + WS| GW

  subgraph control["Control and domain"]
    ADM[admin-service :8006<br/>users, AI-team runs, LLM, tracker]
    PRJ[project-service :8001<br/>projects, tenants]
    PLN[planning-service :8002<br/>IDEA → product plan]
    ORC[orchestrator-service :8003<br/>project statuses via Kafka]
    CAT[catalog-service :8004<br/>stacks and blueprints]
  end

  GW --> ADM
  GW --> PRJ
  GW --> PLN
  GW --> ORC
  GW --> CAT

  subgraph ai["AI and team orchestration"]
    AG[agent-graph-service :8013<br/>AI-team pipeline, LangGraph ReAct]
    LLM[llm-adapter-service :8005<br/>OpenAI-compatible + Langfuse]
    DOC[documents-service :8015<br/>project wiki]
    KB[knowledge-service :8019<br/>KB, RAG, ingest]
    BOOT[agent-bootstrap-service :8011<br/>auto-planning on project create]
    WRK[agent-worker-service :8010<br/>Kafka: agent tasks]
  end

  GW --> AG
  GW --> DOC
  GW --> KB
  AG --> LLM
  AG --> ADM
  PLN --> LLM
  DOC --> LLM
  KB --> LLM
  BOOT --> PRJ
  BOOT --> PLN

  subgraph exec["Execution and tools"]
    ADP[adapter-service :8008<br/>SSH, HTTP executor, metrics ingest]
    ANS[ansible-orchestrator-service :8007<br/>Ansible catalog in S3]
    SBX[sandbox-service :8012<br/>Python sandbox]
    BR[browser-tool-service :8014<br/>Playwright]
    GL[gitlab-service :8016<br/>repositories, provision]
    RWS[repo-workspace-service :8018<br/>clone/commit/push]
    IDX[code-index-service :8017<br/>code search, Qdrant]
  end

  AG --> ADP
  AG --> SBX
  AG --> BR
  AG --> GL
  AG --> RWS
  AG --> IDX
  AG --> DOC
  AG --> KB
  WRK --> AG
  WRK --> ADP
  WRK --> LLM
  RWS --> GL
  IDX --> RWS
  ADP --> ANS

  subgraph bus["Events and queues"]
    KF[Kafka]
    RD[Redis]
  end

  PRJ --> KF
  PLN --> KF
  ORC --> KF
  ADM --> KF
  AG --> KF
  WRK --> KF
  KB --> KF
  AG --> RD

  subgraph data["Stores"]
    PG[(PostgreSQL<br/>separate DB per service)]
    QD[(Qdrant<br/>KB + code vectors)]
    S3[(RustFS S3<br/>Ansible, KB files)]
    CH[(ClickHouse<br/>agent metrics/logs)]
  end

  PRJ --> PG
  ADM --> PG
  AG --> PG
  KB --> PG
  KB --> QD
  KB --> S3
  IDX --> QD
  ANS --> S3
  ADP --> CH

  subgraph observe["Observability"]
    LF[Langfuse<br/>web + worker]
  end

  LLM --> LF
  AG --> LF

  subgraph optional["Optional"]
    GCE[gitlab-ce<br/>profile gitlab]
  end

  GL --> GCE
```

### Typical scenario: request to the AI team

```mermaid
sequenceDiagram
  participant UI as frontend
  participant GW as api-gateway
  participant AG as agent-graph-service
  participant ADM as admin-service
  participant LLM as llm-adapter
  participant ADP as adapter-service

  UI->>GW: POST agent-orchestrate/start
  GW->>AG: start run
  AG->>ADM: create/update AITeamRun + steps
  loop intake → roles → execution_plan
    AG->>LLM: chat / structured JSON
    AG->>AG: LangGraph ReAct + tools
    AG->>ADP: SSH / K8s / Ansible / HTTP
    AG->>ADM: PATCH steps, execution_tasks
  end
  UI->>GW: WS or NDJSON stream
  GW->>AG: run events
  AG-->>UI: step and status deltas
```

### Typical scenario: tracker task

```mermaid
sequenceDiagram
  participant UI as frontend
  participant ADM as admin-service
  participant KF as Kafka
  participant WRK as agent-worker
  participant AG as agent-graph
  participant ADP as adapter-service

  UI->>ADM: dispatch tracker task
  ADM->>KF: TaskEnvelope tracker_task_execute
  KF->>WRK: consume
  WRK->>AG: POST /invoke ReAct
  AG->>ADP: ssh_run_command / context
  AG-->>WRK: final_message + tool evidence
  WRK->>ADM: PATCH execution (DONE / FAILED by tools)
```

---

## Layers and principles

| Layer | Purpose |
|-------|---------|
| **Edge** | Single browser entry point: UI static assets + `/api` proxy to the gateway (including WebSocket). |
| **API Gateway** | Auth (JWT), tenant/project ACL, proxying to internal services. |
| **Domain** | Projects, planning, catalog, admin projection, project status machine. |
| **AI team** | Multi-step role pipeline, LangGraph with tool calling, publishing execution tasks. |
| **Execution** | Safe adapters to SSH, Ansible, Git, browser, sandbox. |
| **Data** | Isolation: each service has its own PostgreSQL; async via Kafka (outbox/inbox). |

---

## Components

### User interface

| Component | Port | What it does |
|-----------|------|--------------|
| **frontend** | 5173 → 80 | SPA (user portal `#/portal`, superadmin `#/superadmin`). In prod, nginx serves static assets and proxies `/api` to the gateway. |
| **api-gateway** | 8000 | Unified REST/WS API: projects, planning, AI-team orchestration, adapter, admin, knowledge, browser logs, etc. |

### Platform control and domain

| Component | Port | What it does |
|-----------|------|--------------|
| **admin-service** | 8006 | Users and roles, LLM/agent settings, **AI-team runs** (runs/steps/execution_tasks), tracker (epics/tasks), role prompts, internal APIs for agent-graph. |
| **project-service** | 8001 | Project lifecycle, tenants, Kafka event outbox. |
| **planning-service** | 8002 | Product planning run (idea → structured plan), LLM calls. |
| **orchestrator-service** | 8003 | **Do not confuse with AI-orchestrate:** Kafka consumer, projection of **project status** (platform workflow). |
| **catalog-service** | 8004 | Stack and blueprint catalog for projects. |
| **agent-bootstrap-service** | 8011 | Listens for project-created events → starts planning automatically when needed. |

### AI team and LLM

| Component | Port | What it does |
|-----------|------|--------------|
| **agent-graph-service** | 8013 | **AI-team** core: intake (full/direct), SA/PM/architect/plans/implementation/deploy/QA steps, `execution_plan` → worker tasks; LangGraph ReAct with platform tools; event stream/WS. |
| **llm-adapter-service** | 8005 | Proxy to OpenAI-compatible APIs; model settings from admin; Langfuse tracing. |
| **agent-worker-service** | 8010 | Kafka `platform.agent.tasks`: **tracker** → ReAct via `agent-graph` `/invoke`; **AI-team execution** → SSH/K8s/Ansible via adapter. |
| **documents-service** | 8015 | Project wiki/documents (architecture, guides), context for agents. |
| **knowledge-service** | 8019 | Knowledge bases (platform + project): file upload, ingest (Kafka), chunks in Qdrant, `search_knowledge` for agents. |

Pipeline roles (simplified): **intake** → full/direct route → **SA** (formal artifacts, tracker) → **PM / architect / devops / backend / frontend** → **execution_plan** → **implementation / deploy / QA / pm_summary**. In `direct` mode some steps are skipped; one role answers via ReAct.

### Execution and DevTools

| Component | Port | What it does |
|-----------|------|--------------|
| **adapter-service** | 8008 | SSH targets, command execution, confirmations, HTTP executor, agent metrics ingest; links to Ansible and ClickHouse. |
| **ansible-orchestrator-service** | 8007 | Ansible role marketplace (templates in S3/RustFS), playbook runs on request from adapter/agents. |
| **sandbox-service** | 8012 | Restricted Python execution for agents. |
| **browser-tool-service** | 8014 | Playwright sessions: navigation, UI snapshots, web app smoke. |
| **gitlab-service** | 8016 | Group/repo provision, tokens, GitLab CE integration (optional). |
| **repo-workspace-service** | 8018 | Local git workspaces: clone, read/write, commit, push. |
| **code-index-service** | 8017 | Code indexing from workspace, semantic search (Qdrant). |

### Data infrastructure

| Component | Purpose |
|-----------|---------|
| **Kafka** | Domain events (`platform.events`), agent tasks (`platform.agent.tasks`), knowledge ingest. Outbox/inbox patterns in services. |
| **Redis** | Cache/coordination in agent-graph (e.g. last project run). |
| **PostgreSQL** | Separate instance per service: `project`, `planning`, `admin`, `orchestrator`, `adapter`, `documents`, `knowledge`, `gitlab`, `repo-workspace`, `code-index`. |
| **Qdrant** | Vector collections: knowledge-base and code-index chunks. |
| **RustFS (S3)** | Object storage: Ansible templates, knowledge-base files. |
| **ClickHouse** | Metrics and logs from agents on the user’s servers. |

### Observability and options

| Component | Purpose |
|-----------|---------|
| **Langfuse** (web, worker, postgres, redis, clickhouse, minio) | LLM call traces (llm-adapter, agent-graph). |
| **gitlab-ce** (profile `gitlab`) | Local GitLab CE for development; **gitlab-service** talks to it via API. |

---

## Two kinds of “orchestration”

| Term | Service | Meaning |
|------|---------|---------|
| **Project orchestration** | `orchestrator-service` | **Project** lifecycle statuses from Kafka events. |
| **AI-team orchestration** | `agent-graph-service` + `admin-service` | Running a **user request** through team roles, execution tasks, UI stream. |

---

## Data flows (brief)

1. **Project creation** → `project-service` → Kafka → `agent-bootstrap` / `orchestrator` / projection into `admin`.
2. **Product planning** → UI → gateway → `planning-service` → LLM → result in planning DB.
3. **AI team** → gateway → `agent-graph` ↔ `admin` (runs) ↔ tools → `adapter` / Git / docs / knowledge → Kafka tasks → `agent-worker`.
4. **Knowledge upload** → gateway → `knowledge-service` → S3 + Kafka ingest → Qdrant; agents search via the `search_knowledge` tool.
5. **Code** → `gitlab-service` + `repo-workspace` + `code-index` (clone → edits → push → search).

---

## Repository

| Path | Contents |
|------|----------|
| `Platform/backend` | All microservices (`services/*`), shared contracts, tests. |
| `Platform/frontend` | TypeScript SPA, nginx for prod. |
| `Platform/docs` | Topic notes (GitLab, knowledge, code-index). |
| `schema.md` | Simplified diagram of the AI-team pipeline only. |

---

## Local run

```bash
cd Platform
docker compose up -d --build
```

- UI: http://localhost:5173  
- API: http://localhost:8000  
- Details: `Platform/README.md`

For heavy GitLab CE: `docker compose --profile gitlab up -d`.
