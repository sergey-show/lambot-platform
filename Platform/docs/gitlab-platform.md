# GitLab platform (production wiring)

One GitLab instance for the whole site. On `project.created`, **gitlab-service** creates a private **group** and a **group access token** (repositories are not created yet). Secrets are stored in the adapter (`ProjectInfraResource`, label “GitLab platform (group and token)”).

## Services

| Service | Port | Purpose |
|---------|------|---------|
| gitlab-service | 8016 | Group provisioning, repository create API |
| code-index-service | 8017 | Search over clone: **Qdrant** (vectors) + **Postgres** (chunk text) |
| repo-workspace-service | 8018 | Local git: clone, feature branch, commit, push |

Shared volume: `repo_workspaces_data` → `/var/lambot/repo-workspaces`.

### Code index (Qdrant)

- **qdrant** service in compose (`6333`), collection `lambot_code_chunks`.
- Reindex: deletes points by `project_id` + `repo_slug` + `branch`, then upserts.
- Search: ANN in Qdrant → text from Postgres → light lexical rerank.
- After migrating from Postgres embeddings: recreate the `postgres_code_index` volume or reindex per project.

## Environment variables (prod)

```bash
GITLAB_URL=https://gitlab.company.com
GITLAB_ADMIN_TOKEN=glpat-...   # api on root group
GITLAB_ROOT_GROUP_PATH=lambot    # parent for projects
GITLAB_PROVISION_ENABLED=1
GITLAB_PROVISION_MOCK=0
```

## Agent workflow

1. `gitlab_provision_status` / `get_development_context` → `gitlab_platform`
2. `gitlab_create_repository` — empty repository in the project group
3. `repo_clone_or_sync` — `feature_branch` for work; the active branch is indexed
4. `search_codebase` → `repo_read_file` / `repo_write_commit_push`

The GitLab token is never passed to agents.

## GitLab CE in docker-compose

Optional **`gitlab-ce`** service (profile `gitlab`, ~4–8 GB RAM). On a host with ~16+ GB free memory this is usually enough.

```bash
docker compose --profile gitlab up -d
# UI: http://localhost:8929
docker exec platform-gitlab-ce grep Password /etc/gitlab/initial_root_password
```

In the UI: create group `lambot`, root PAT (`api`) → `.env`: `GITLAB_ADMIN_TOKEN`, `GITLAB_PROVISION_MOCK=0`.

## Legacy projects (before auto-provisioning)

On successful provisioning, adapter `value_json` sets **`gitlab_group_created: true`**.

- **Superadmin** → projects table → “GitLab group” column → **Create group** / **Retry**
- **User portal** → Infrastructure → “GitLab platform” block

API: `POST /api/v1/admin/projects/{id}/gitlab-provision` (superadmin) or `POST /api/v1/projects/{id}/gitlab-provision` (Owner/Admin).

## Local development without GitLab

By default `GITLAB_PROVISION_MOCK=1` — adapter write without a real API. Full git cycle: `--profile gitlab` or external `GITLAB_URL`.
