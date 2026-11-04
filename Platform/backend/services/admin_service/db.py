import os
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv(
    "ADMIN_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+psycopg://platform:platform@localhost:5432/admin_db"),
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def _migrate_llm_adapter_columns() -> None:
    stmts = [
        "ALTER TABLE llm_adapter_settings ADD COLUMN IF NOT EXISTS coding_model VARCHAR(128)",
        "ALTER TABLE llm_adapter_settings ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(128)",
        "ALTER TABLE llm_adapter_settings ADD COLUMN IF NOT EXISTS reranking_model VARCHAR(128)",
    ]
    try:
        with engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))
    except Exception:
        pass


def _migrate_projection_columns() -> None:
    stmts = [
        "ALTER TABLE project_projection ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE project_projection ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE project_projection ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    ]
    try:
        with engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))
    except Exception:
        pass


def _seed_agent_development_prompts() -> None:
    from services.admin_service.models import AgentDevelopmentPrompt

    defaults = {
        "pm": (
            """You are a product manager / PM for a Lambot project. State MVP goals, backlog priorities, readiness criteria.
            Align expectations with architecture and engineering; do not invent numbers or deadlines without basis."""
        ),
        "architect": (
            """You are a solution architect in Lambot. Propose system boundaries, services, integrations, data, and risks.
            Build on project and infrastructure context (repo, CI/CD, deploy, observability);
            if specific services are missing — do not invent them. Give practical, verifiable solutions."""
        ),
        "devops": (
            """You are the project DevOps engineer. Help with CI/CD, infrastructure, Ansible, Docker,
            deploy, and observability. Build on the project resource summary (available URLs, accounts, registry/repo, etc.)
            from platform context; if GitLab/Nexus are missing — do not treat them as required.
            Do not invent secrets — ask for clarification when data is missing."""
        ),
        "backend": (
            """You are the project backend developer. Write and review server code, APIs, DBs, integrations.
            Use project context (repos, environments); align contracts with frontend."""
        ),
        "frontend": (
            """You are the project frontend developer. Write UI, accessibility, state, API integration.
            Use project context (service URLs, styles); align with backend."""
        ),
        "qa": (
            "You are the project QA engineer. Form a test plan, check release risks,"
            "give a GO/NO_GO decision based on execution facts and available integrations."
            "Do not invent passing tests — record only verified results."
        ),
        "sa": (
            "You are the project systems analyst in Lambot. Formalize requirements: spec, glossary,"
            "traceability, diagrams (mermaid in wiki). Create epics and tasks in the tracker with assignee_role"
            "for DevOps/Backend/Frontend/QA/Architect. Do not replace the PM (release priorities) or architect (stack)."
        ),
    }
    try:
        with SessionLocal() as db:
            for role, text in defaults.items():
                row = db.get(AgentDevelopmentPrompt, role)
                if row is None:
                    db.add(
                        AgentDevelopmentPrompt(
                            role=role,
                            system_prompt=text,
                            updated_at=datetime.utcnow(),
                        )
                    )
            db.commit()
    except Exception:
        pass


def _seed_agent_graph_builtin_tools() -> None:
    from services.admin_service.models import AgentGraphBuiltinTool

    defaults = {
        "get_development_context": "Development context (targets, infra, prompts)",
        "get_project_status": "Project workflow status",
        "list_infra_resources": "Project infrastructure resources",
        "run_product_planning": "Product planning (MVP, IDEA→PLAN)",
        "get_activity_logs": "Activity log (Ansible, operations)",
        "get_agent_metrics": "Remote agent metrics",
        "ssh_run_command": "SSH: server command (with confirmation for dangerous ones)",
        "kubernetes_query": "Kubernetes: kubectl via kubeconfig (project)",
        "run_sandbox_python": "Python sandbox",
        "http_request": "Universal HTTP tool (alias/method/path/body)",
        "browser_session": "Playwright browser tool (sessions, actions, screenshots)",
        "ansible_run_template": "Ansible marketplace: run a role template on an SSH target (with fallback)",
        "get_project_docs_context": "RAG context for project documentation (wiki)",
        "upsert_project_document": "Create/update a project document (wiki + versions)",
        "list_project_documents": "List project documents (wiki)",
        "gitlab_provision_status": "Project GitLab group status (platform)",
        "gitlab_create_repository": "Create a repository in the project GitLab group",
        "list_project_repos": "List local repo workspaces",
        "repo_clone_or_sync": "Clone/update workspace; feature_branch for work",
        "repo_read_file": "Read a file from the workspace",
        "repo_write_commit_push": "Write files, commit, push",
        "search_codebase": "Semantic search over the code index",
        "tracker_list_items": "Tracker: list project epics and tasks",
        "tracker_create_epic": "Tracker: create an epic (backlog)",
        "tracker_create_task": "Tracker: create a task under an epic with an agent role",
        "search_knowledge": "Search knowledge bases (Qdrant, platform + project)",
    }
    try:
        with SessionLocal() as db:
            for key, label in defaults.items():
                row = db.get(AgentGraphBuiltinTool, key)
                if row is None:
                    db.add(
                        AgentGraphBuiltinTool(
                            tool_key=key,
                            label=label,
                            enabled=True,
                            updated_at=datetime.utcnow(),
                        )
                    )
            db.commit()
    except Exception:
        pass


def init_db() -> None:
    import services.admin_service.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_llm_adapter_columns()
    _migrate_projection_columns()
    _seed_agent_development_prompts()
    _seed_agent_graph_builtin_tools()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
