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
KAFKA_TOPIC_KNOWLEDGE_INGEST=platform.knowledge.ingest
```

## Запуск

```bash
docker compose up -d --build knowledge-service api-gateway qdrant rustfs kafka postgres-knowledge frontend
```

Health: `GET http://knowledge-service:8019/health`
