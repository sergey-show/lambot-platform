# Code index + Qdrant

## Архитектура

| Хранилище | Данные |
|-----------|--------|
| **Qdrant** | embedding-векторы, payload (project_id, repo_slug, branch, file_path, chunk_idx) |
| **Postgres** (`code_chunks`) | полный `chunk_text`, метаданные, `qdrant_point_id` (= id точки) |

## Переменные

```bash
QDRANT_URL=http://qdrant:6333
QDRANT_CODE_COLLECTION=lambot_code_chunks
CODE_INDEX_VECTOR_SIZE=1536   # размерность модели эмбеддингов (OpenAI text-embedding-3-small)
```

## Миграция со старой схемы (embedding в JSONB)

1. `docker compose up -d qdrant code-index-service --build`
2. Для каждого проекта: `repo_clone_or_sync` → reindex (авто после push) или  
   `POST /api/v1/internal/projects/{id}/code-index/reindex` с телом `{"repo_slug":"...","branch":"..."}`

Либо сброс БД: `docker volume rm ..._postgres_code_index_data` (потеряете только метаданные чанков, не git).

## Health

`GET http://code-index-service:8017/health` → поле `qdrant: ok|unreachable`.
