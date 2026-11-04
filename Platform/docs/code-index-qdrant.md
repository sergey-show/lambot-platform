# Code index + Qdrant

## Architecture

| Store | Data |
|-------|------|
| **Qdrant** | embedding vectors, payload (`project_id`, `repo_slug`, `branch`, `file_path`, `chunk_idx`) |
| **Postgres** (`code_chunks`) | full `chunk_text`, metadata, `qdrant_point_id` (= point id) |

## Environment variables

```bash
QDRANT_URL=http://qdrant:6333
QDRANT_CODE_COLLECTION=lambot_code_chunks
CODE_INDEX_VECTOR_SIZE=1536   # embedding model dimension (OpenAI text-embedding-3-small)
```

## Migration from the old schema (embedding in JSONB)

1. `docker compose up -d qdrant code-index-service --build`
2. For each project: `repo_clone_or_sync` → reindex (auto after push) or  
   `POST /api/v1/internal/projects/{id}/code-index/reindex` with body `{"repo_slug":"...","branch":"..."}`

Or reset DB: `docker volume rm ..._postgres_code_index_data` (loses chunk metadata only, not git).

## Health

`GET http://code-index-service:8017/health` → field `qdrant: ok|unreachable`.
