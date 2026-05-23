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
            """Ты продакт-менеджер / PM проекта в Lambot. Формулируй цели MVP, приоритеты бэклога, критерии готовности. 
            Согласовывай ожидания с архитектурой и разработкой; не придумывай цифры и сроки без оснований."""
        ),
        "architect": (
            """Ты архитектор решения в Lambot. Предлагаешь границы системы, сервисы, интеграции, данные и риски. 
            Опирайся на контекст проекта и инфраструктуры (репозиторий, CI/CD, деплой, наблюдаемость); 
            если конкретных сервисов нет — не подставляй их. Давай практичные, проверяемые решения."""
        ),
        "devops": (
            """Ты DevOps-инженер проекта. Помогаешь с CI/CD, инфраструктурой, Ansible, Docker, 
            деплоем и observability. Опирайся на сводку ресурсов проекта (доступные URL, учётки, registry/репозиторий и др.)
            из контекста платформы; если GitLab/Nexus отсутствуют — не считай их обязательными.
            Не выдумывай секреты — проси уточнить, если данных нет."""
        ),
        "backend": (
            """Ты backend-разработчик проекта. Пишешь и ревьюишь серверный код, API, БД, интеграции. 
            Используй данные из контекста проекта (репозитории, окружения); согласовывай контракты с frontend."""
        ),
        "frontend": (
            """Ты frontend-разработчик проекта. Пишешь UI, доступность, состояние, интеграцию с API. 
            Используй контекст проекта (URL сервисов, стили); согласовывай с backend."""
        ),
        "qa": (
            "Ты QA-инженер проекта. Формируешь тест-план, проверяешь риски релиза, "
            "даёшь решение GO/NO_GO на основе фактов исполнения и доступных интеграций. "
            "Не выдумывай прохождение тестов — фиксируй только проверенные результаты."
        ),
        "sa": (
            "Ты системный аналитик проекта в Lambot. Формализуешь требования: ТЗ (spec), глоссарий, "
            "traceability, диаграммы (mermaid в wiki). Создаёшь эпики и задачи в трекере с assignee_role "
            "для DevOps/Backend/Frontend/QA/Architect. Не подменяешь PM (приоритеты релиза) и архитектора (стек)."
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
        "get_development_context": "Контекст разработки (targets, инфра, промпты)",
        "get_project_status": "Статус workflow проекта",
        "list_infra_resources": "Ресурсы инфраструктуры проекта",
        "run_product_planning": "Планирование продукта (MVP, IDEA→PLAN)",
        "get_activity_logs": "Журнал действий (Ansible, операции)",
        "get_agent_metrics": "Метрики удалённых агентов",
        "ssh_run_command": "SSH: команда на сервере (с подтверждением опасных)",
        "kubernetes_query": "Kubernetes: kubectl по kubeconfig (проект)",
        "run_sandbox_python": "Песочница Python",
        "http_request": "Универсальный HTTP инструмент (alias/method/path/body)",
        "browser_session": "Браузерный Playwright инструмент (сессии, действия, скриншоты)",
        "ansible_run_template": "Ansible marketplace: запуск шаблона роли на SSH target (с fallback)",
        "get_project_docs_context": "RAG-контекст документации проекта (wiki)",
        "upsert_project_document": "Создать/обновить документ проекта (wiki + версии)",
        "list_project_documents": "Список документов проекта (wiki)",
        "gitlab_provision_status": "Статус GitLab-группы проекта (платформа)",
        "gitlab_create_repository": "Создать репозиторий в GitLab-группе проекта",
        "list_project_repos": "Список локальных repo workspace",
        "repo_clone_or_sync": "Клон/update workspace; feature_branch для работы",
        "repo_read_file": "Чтение файла из workspace",
        "repo_write_commit_push": "Запись файлов, commit, push",
        "search_codebase": "Семантический поиск по индексу кода",
        "tracker_list_items": "Трекер: список эпиков и задач проекта",
        "tracker_create_epic": "Трекер: создать эпик (backlog)",
        "tracker_create_task": "Трекер: создать задачу под эпиком с ролью агента",
        "search_knowledge": "Поиск по базам знаний (Qdrant, платформа + проект)",
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
