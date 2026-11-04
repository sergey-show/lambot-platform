# Knowledge bases (knowledge-service)

## Purpose

- **Platform** bases (Superadmin): shared knowledge for all projects (development, analysis, …).
- **Project** bases (portal): project-scoped collections.
- Files: PDF, images, text — **5 MB** limit per document.
- File storage: **S3** (RustFS, bucket `platform-knowledge`, separate `KNOWLEDGE_S3_*` env vars).
- Queue: Kafka topic `platform.knowledge.ingest`.
- Vectors: **Qdrant** collection `lambot_knowledge_chunks`; full chunk text in Postgres `knowledge_db`.

## Indexing pipeline

1. Upload → S3 + `knowledge_documents` row (`pending`).
2. Kafka → worker: `processing` → text extraction:
   - PDF: `pypdf`
   - images: shared chat model (vision via `/v1/chat/completions`)
   - text: decode
3. Chunks (~2000 chars) → **embedding model** (`/api/v1/llm/embed`) → upsert Qdrant + `knowledge_chunks`.
4. Status `ready` or `failed`.

## API

| Who | Examples |
|-----|----------|
| Superadmin | `GET/POST /api/v1/admin/knowledge/bases`, upload, search |
| Portal | `GET/POST /api/v1/projects/{id}/knowledge/bases`, upload, search |
| Agents | tool `search_knowledge` (internal `.../knowledge/search`) |

Delete and enable/disable:

| Action | Method |
|--------|--------|
| Delete base | `DELETE .../knowledge/bases/{kb_id}` |
| Delete document | `DELETE .../bases/{kb_id}/documents/{doc_id}` |
| Enable/disable document | `PATCH .../documents/{doc_id}` body `{"enabled": true\|false}` |

On **disable**, vectors are removed from Qdrant (search and agents no longer see the document). On **enable** of a ready document, chunks are re-indexed; for `pending`/`failed` — back onto the Kafka queue.

## Environment variables

```bash
KNOWLEDGE_SERVICE_URL=http://knowledge-service:8019
KNOWLEDGE_S3_BUCKET=platform-knowledge
KNOWLEDGE_S3_ENDPOINT_URL=http://rustfs:9000
KNOWLEDGE_MAX_UPLOAD_BYTES=5242880
QDRANT_KNOWLEDGE_COLLECTION=lambot_knowledge_chunks
KNOWLEDGE_VECTOR_SIZE=768
KAFKA_TOPIC_KNOWLEDGE_INGEST=platform.knowledge.ingest
```

`KNOWLEDGE_VECTOR_SIZE` must match the embedding model dimension (e.g. **768** for `nomic-embed-text`, **1536** for `text-embedding-3-small`). If the Qdrant collection was created with a different size, ingest fails with `Vector dimension error` — delete the collection and re-index documents (see troubleshooting below).

## Run

```bash
docker compose up -d --build knowledge-service api-gateway qdrant rustfs kafka postgres-knowledge frontend
```

Health: `GET http://knowledge-service:8019/health`

## Troubleshooting

### `Vector dimension error: expected dim: 1536, got 768`

The Qdrant collection was created for one embedding model, but `llm-adapter` returns vectors of another dimension.

1. Check model dimension:

```bash
curl -s -X POST http://localhost:8005/api/v1/llm/embed \
  -H 'Content-Type: application/json' \
  -d '{"input":["probe"]}' | jq '.embeddings[0] | length, .model'
```

2. On the server `.env`: `KNOWLEDGE_VECTOR_SIZE=768` (or your dim).

3. Recreate the collection (if old vectors are not needed):

```bash
curl -X DELETE http://localhost:6333/collections/lambot_knowledge_chunks
docker compose up -d --build knowledge-service
```

4. Re-index: in the UI disable/enable the document, or upload the file again. Documents with `status=failed` — toggle `enabled` or re-upload.

Success in logs: `knowledge doc ready id=... chunks=N`.
