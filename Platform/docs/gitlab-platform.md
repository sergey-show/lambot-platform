# GitLab платформа (промышленная связка)

Один GitLab на всю площадку. При `project.created` **gitlab-service** создаёт приватную **группу** и **group access token** (репозитории не создаются). Секреты хранятся в adapter (`ProjectInfraResource`, label «GitLab платформа (группа и токен)»).

## Сервисы

| Сервис | Порт | Назначение |
|--------|------|------------|
| gitlab-service | 8016 | Провижининг группы, API создания репозитория |
| code-index-service | 8017 | Эмбеддинги и поиск по клону |
| repo-workspace-service | 8018 | Локальный git: clone, feature branch, commit, push |

Общий volume: `repo_workspaces_data` → `/var/lambot/repo-workspaces`.

## Переменные (prod)

```bash
GITLAB_URL=https://gitlab.company.com
GITLAB_ADMIN_TOKEN=glpat-...   # api на корневой группе
GITLAB_ROOT_GROUP_PATH=lambot    # parent для проектов
GITLAB_PROVISION_ENABLED=1
GITLAB_PROVISION_MOCK=0
```

## Workflow агентов

1. `gitlab_provision_status` / `get_development_context` → `gitlab_platform`
2. `gitlab_create_repository` — пустой репозиторий в группе проекта
3. `repo_clone_or_sync` — `feature_branch` для работы; индексируется активная ветка
4. `search_codebase` → `repo_read_file` / `repo_write_commit_push`

Токен GitLab агентам не передаётся.

## GitLab CE в docker-compose

Опциональный сервис **`gitlab-ce`** (профиль `gitlab`, ~4–8 GB RAM). На сервере с ~16+ GB свободной памяти обычно хватает.

```bash
docker compose --profile gitlab up -d
# UI: http://localhost:8929
docker exec platform-gitlab-ce grep Password /etc/gitlab/initial_root_password
```

В UI: группа `lambot`, root PAT (`api`) → `.env`: `GITLAB_ADMIN_TOKEN`, `GITLAB_PROVISION_MOCK=0`.

## Локальная разработка без GitLab

По умолчанию `GITLAB_PROVISION_MOCK=1` — запись в adapter без реального API. Полный git-цикл: `--profile gitlab` или внешний `GITLAB_URL`.
