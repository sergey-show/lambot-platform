# Базы знаний (knowledge-service)

## Назначение

- **Платформенные** базы (Superadmin): общие знания для всех проектов (разработка, анализ, …).
- **Проектные** базы (портал): свои коллекции внутри проекта.
- Файлы: PDF, изображения, текст — лимит **5 МБ** на документ.
- Хранение файлов: **S3** (RustFS, бакет `platform-knowledge`, отдельные `KNOWLEDGE_S3_*` env).
- Очередь: Kafka topic `platform.knowledge.ingest`.
- Векторы: **Qdrant** коллекция `lambot_knowledge_chunks`; полный текст чанков — Postgres `knowledge_db`.

## Пайплайн индексации

1. Upload → S3 + запись `knowledge_documents` (`pending`).
2. Kafka → worker: `processing` → извлечение текста:
   - PDF: `pypdf`
   - изображения: общая chat-модель (vision через `/v1/chat/completions`)
   - текст: decode
3. Чанки (~2000 символов) → **embedding-модель** (`/api/v1/llm/embed`) → upsert Qdrant + `knowledge_chunks`.
4. Статус `ready` или `failed`.

## API

| Кто | Примеры |
|-----|---------|
| Superadmin | `GET/POST /api/v1/admin/knowledge/bases`, upload, search |
| Портал | `GET/POST /api/v1/projects/{id}/knowledge/bases`, upload, search |
| Агенты | tool `search_knowledge` (внутренний `.../knowledge/search`) |

Удаление и enable/disable:

| Действие | Метод |
|----------|--------|
| Удалить базу | `DELETE .../knowledge/bases/{kb_id}` |
| Удалить документ | `DELETE .../bases/{kb_id}/documents/{doc_id}` |
| Вкл/выкл документ | `PATCH .../documents/{doc_id}` body `{"enabled": true\|false}` |

При **выключении** векторы удаляются из Qdrant (поиск и агенты не видят документ). При **включении** готового документа чанки переиндексируются; для `pending`/`failed` — снова в очередь Kafka.

## Переменные

```bash
KNOWLEDGE_SERVICE_URL=http://knowledge-service:8019
KNOWLEDGE_S3_BUCKET=platform-knowledge
KNOWLEDGE_S3_ENDPOINT_URL=http://rustfs:9000
KNOWLEDGE_MAX_UPLOAD_BYTES=5242880
QDRANT_KNOWLEDGE_COLLECTION=lambot_knowledge_chunks
KNOWLEDGE_VECTOR_SIZE=768
KAFKA_TOPIC_KNOWLEDGE_INGEST=platform.knowledge.ingest
```

`KNOWLEDGE_VECTOR_SIZE` должен совпадать с размерностью векторов embedding-модели (например **768** для `nomic-embed-text`, **1536** для `text-embedding-3-small`). Если коллекция в Qdrant уже создана с другой размерностью, ingest падает с `Vector dimension error` — удалите коллекцию и переиндексируйте документы (см. troubleshooting ниже).

## Запуск

```bash
docker compose up -d --build knowledge-service api-gateway qdrant rustfs kafka postgres-knowledge frontend
```

Health: `GET http://knowledge-service:8019/health`

## Troubleshooting

### `Vector dimension error: expected dim: 1536, got 768`

Коллекция Qdrant создана под одну embedding-модель, а `llm-adapter` отдаёт векторы другой размерности.

1. Узнать dim модели:

```bash
curl -s -X POST http://localhost:8005/api/v1/llm/embed \
  -H 'Content-Type: application/json' \
  -d '{"input":["probe"]}' | jq '.embeddings[0] | length, .model'
```

2. В `.env` на сервере: `KNOWLEDGE_VECTOR_SIZE=768` (или ваш dim).

3. Пересоздать коллекцию (если старые векторы не нужны):

```bash
curl -X DELETE http://localhost:6333/collections/lambot_knowledge_chunks
docker compose up -d --build knowledge-service
```

4. Переиндексировать: в UI выключить/включить документ, или загрузить файл снова. Документы со `status=failed` — toggle `enabled` или повторный upload.

В логах успех: `knowledge doc ready id=... chunks=N`.
