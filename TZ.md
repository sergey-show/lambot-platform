Отличная идея. Ниже собрал черновик **ТЗ v1** в формате, который уже можно использовать для декомпозиции в эпики и задачи.

## 1) Продуктовая цель

Создать платформу “Development-as-a-Team”, которая заменяет ключевые функции IT-команды:
- сбор и формализация требований;
- проектирование и генерация инфраструктуры;
- генерация кода по выбранному стеку;
- CI/CD и деплой;
- сопровождение через мультиагентный контур.

## 2) Целевая аудитория

- **Стартапы без техкоманды** (MVP “под ключ”).
- **Предприниматели/продакты** (быстрый запуск идеи).
- **SMB-компании** (автоматизация рутинной разработки и DevOps).

## 3) Пользовательский сценарий (E2E)

1. Пользователь регистрируется/логинится через SSO.
2. Создаёт проект.
3. Описывает идею, загружает скриншоты/референсы.
4. Нажимает “Продумать”:
   - ИИ формирует концепцию, архитектурный план, backlog, roadmap.
5. Выбирает вариант инфраструктуры:
   - `own server` или `cloud`.
6. Платформа генерирует IaC:
   - Terraform/Ansible в зависимости от выбранного варианта.
7. Разворачивается базовая инфраструктура:
   - GitLab, Kubernetes, Vault.
8. Пользователь выбирает стек приложения (из готовых паттернов).
9. Агенты генерируют сервисы, CI/CD, деплои.
10. Платформа выполняет развертывание и даёт доступ к результату.

## 4) Границы MVP

**В MVP включить:**
- 1 облако (например AWS **или** Yandex Cloud) + 1 “own server” профиль.
- 2-3 шаблонных стека приложения.
- Базовые роли агентов.
- Генерация и запуск инфраструктуры с валидацией.
- Генерация backend/frontend + базовый CI/CD.
- Публичный сайт (`Site`) как отдельный модуль.

**Не включать в MVP:**
- Полностью автономный self-healing SRE.
- Сложный FinOps и multi-cloud оркестрация.
- Авто-рефакторинг legacy/монолитов клиента.

## 5) Функциональные требования

### 5.1 Управление пользователями и доступом
- Регистрация, логин, SSO (OIDC/SAML).
- Роли: `Owner`, `Admin`, `Viewer`.
- Мультипроектность, разграничение доступа по проектам.

### 5.2 Работа с проектом
- Создание/редактирование проекта.
- Хранение описания идеи, вложений (скриншоты, документы).
- Версионность требований (история “продумать” итераций).

### 5.3 AI-планирование
- Генерация:
  - продуктовой концепции,
  - техархитектуры,
  - backlog (epics/features/tasks),
  - рисков и оценок.
- Редактирование плана человеком + повторная генерация.

### 5.4 Инфраструктура и IaC
- Выбор target: `own server` / `cloud`.
- Генерация Terraform/Ansible.
- Поддержка обязательных компонентов: GitLab, K8s, Vault.
- Применение IaC с логированием и rollback-стратегией.

### 5.5 Генерация кода и деплой
- Выбор стека из шаблонов/паттернов.
- Генерация каркаса сервисов и конфигов CI/CD.
- Сборка, тесты, деплой в созданный кластер.
- Отображение статуса пайплайна и окружений.

### 5.6 Публичный сайт (`Site`)
- Отдельная директория/сервис для маркетингового сайта.
- Контент о платформе, кейсы, CTA, документация/демо.

## 6) Нефункциональные требования

- **Архитектура:** микросервисы на Python, DDD-подход.
- **Надежность:** retry, idempotency, DLQ для задач.
- **Безопасность:** Vault для секретов, RBAC, аудит действий.
- **Наблюдаемость:** централизованные логи, метрики, трассировка.
- **Масштабируемость:** горизонтальное масштабирование воркеров/агентов.
- **Производительность:** очередь задач через Kafka, кэш через Redis.
- **Данные:** PostgreSQL как источник истины.

## 7) Технологический стек платформы

- `Python` — backend/microservices.
- `TypeScript` — frontend платформы.
- `PostgreSQL` — OLTP.
- `Redis` — кеш, быстрые состояния.
- `Kafka` — асинхронные задачи/события.
- `LLM Adapter` — единый шлюз к OpenAI API/совместимым.
- `SSO` — через OIDC/SAML провайдер.
- `Kubernetes` — рантайм платформы.

## 8) Микросервисная модель (DDD, high-level bounded contexts)

- `Identity & Access BC` — пользователи, SSO, роли, политики.
- `Project BC` — проекты, артефакты, вложения, версии требований.
- `Planning BC` — генерация концепции и backlog.
- `Agent Orchestration BC` — роли агентов, маршрутизация задач, state machine.
- `Infra BC` — IaC генерация/валидация/применение.
- `Codegen BC` — генерация кода по стеку и паттернам.
- `Delivery BC` — CI/CD orchestration, деплои, статус релизов.
- `Catalog BC` — библиотека стеков и архитектурных паттернов.

## 9) Роли агентов (обязательная модель)

- `Product Analyst Agent` — формализация идеи в требования.
- `Solution Architect Agent` — техархитектура и план реализации.
- `Infra Architect Agent` — выбор схемы infra под target.
- `IaC Agent` — Terraform/Ansible генерация и проверки.
- `Backend Agent` — сервисы, API, бизнес-логика.
- `Frontend Agent` — UI на TypeScript.
- `DevOps Agent` — CI/CD, GitLab pipelines, деплой.
- `QA Agent` — автотесты, smoke/e2e, quality gates.
- `Security Agent` — секреты, политики, базовые security checks.

## 10) Паттерны и наборы стеков

Нужен каталог “Blueprints”:
- `SaaS Web App`
- `Marketplace`
- `Internal Tool`
- `API-first backend`

У каждого blueprint:
- архитектурная схема;
- поддерживаемые языки/фреймворки;
- infra-профиль;
- чеклист готовности к продакшену.

## 11) Очереди и оркестрация

- Kafka-топики по этапам пайплайна:
  - `project.planning.requested`
  - `infra.provision.requested`
  - `codegen.requested`
  - `deploy.requested`
- Оркестратор хранит state machine проекта:
  - `IDEA -> PLAN_READY -> INFRA_READY -> CODE_READY -> DEPLOYED`.

## 12) Метрики успеха

- Time-to-first-deploy (цель: часы, не недели).
- Доля проектов, дошедших до `DEPLOYED`.
- Среднее число ручных правок после генерации.
- Успешность IaC apply с первого раза.
- NPS/удовлетворённость пользователей.

## 13) Основные риски

- Переоценка “полной замены команды” на ранних этапах.
- Ошибки IaC в разных средах.
- Качество кода без строгих quality gates.
- Безопасность и доступы при self-hosted варианте.

## 14) Предложение по этапам запуска

- **Phase 1 (MVP):** планирование + 1 infra target + 1 стек + базовый деплой.
- **Phase 2:** расширение стеков/паттернов, улучшение QA/Sec.
- **Phase 3:** полуавтономное сопровождение, оптимизация стоимости/надежности.

---

## 15) Эпики и user stories (MVP)

### Epic A. Identity & Access
**Цель:** обеспечить безопасный вход, SSO и ролевой доступ.

- **US-A1:** как пользователь, я хочу войти через SSO, чтобы не создавать отдельные пароли.
  - **Acceptance Criteria:**
    - поддержан OIDC-провайдер;
    - после логина создается/обновляется профиль пользователя;
    - сессия валидна и привязана к tenant/project context.
- **US-A2:** как Owner проекта, я хочу назначать роли `Admin` и `Viewer`.
  - **Acceptance Criteria:**
    - роль меняется через API/UI;
    - изменения аудируются;
    - неавторизованный пользователь получает `403`.

### Epic B. Project Workspace
**Цель:** дать пользователю пространство для работы с идеей.

- **US-B1:** как пользователь, я хочу создать проект с названием и описанием.
  - **Acceptance Criteria:**
    - проект создается в статусе `IDEA`;
    - владелец проекта назначается автоматически.
- **US-B2:** как пользователь, я хочу прикреплять скриншоты/файлы.
  - **Acceptance Criteria:**
    - поддержаны jpg/png/pdf;
    - ограничения по размеру файла валидируются;
    - файлы связаны с версией требований.

### Epic C. AI Planning
**Цель:** получить формализованный план реализации из идеи.

- **US-C1:** как пользователь, я нажимаю `Продумать` и получаю концепцию + roadmap.
  - **Acceptance Criteria:**
    - создается артефакт `plan_version`;
    - в плане есть разделы: scope, architecture, backlog, risks;
    - статус проекта меняется в `PLAN_READY`.
- **US-C2:** как пользователь, я редактирую план и запускаю re-plan.
  - **Acceptance Criteria:**
    - предыдущая версия не теряется;
    - виден diff между версиями плана.

### Epic D. Infrastructure Provisioning (IaC)
**Цель:** развернуть инфраструктуру через шаблоны.

- **US-D1:** как пользователь, я выбираю target `own server` или `cloud`.
  - **Acceptance Criteria:**
    - выбор сохраняется в проекте;
    - валидация обязательных полей окружения проходит до запуска.
- **US-D2:** как пользователь, я хочу сгенерировать и применить IaC.
  - **Acceptance Criteria:**
    - генерируется пакет Terraform/Ansible;
    - создаются GitLab, Kubernetes, Vault;
    - логируются шаги apply, ошибки и итоговый статус;
    - при ошибке выполняется rollback или формируется безопасный fail-state.

### Epic E. Codegen & Delivery
**Цель:** сгенерировать приложение по выбранному стеку и развернуть его.

- **US-E1:** как пользователь, я выбираю blueprint и стек.
  - **Acceptance Criteria:**
    - доступны минимум 2 шаблона стека в MVP;
    - выбор фиксируется в `Catalog BC` и проекте.
- **US-E2:** как пользователь, я запускаю генерацию кода и CI/CD.
  - **Acceptance Criteria:**
    - создан backend и frontend skeleton;
    - создан pipeline в GitLab;
    - прогоняются базовые тесты;
    - успешный деплой меняет статус в `DEPLOYED`.

### Epic F. Public Site
**Цель:** промо-сайт платформы как отдельный модуль `Site`.

- **US-F1:** как посетитель, я хочу видеть ценность платформы и CTA.
  - **Acceptance Criteria:**
    - сайт доступен на отдельном домене/поддомене;
    - есть разделы: продукт, возможности, кейсы, CTA.

## 16) Доменные события и Kafka schema (MVP)

### 16.1 Топики
- `project.planning.requested`
- `project.planning.completed`
- `infra.provision.requested`
- `infra.provision.completed`
- `codegen.requested`
- `codegen.completed`
- `deploy.requested`
- `deploy.completed`
- `pipeline.failed`

### 16.2 Базовый формат события
```json
{
  "event_id": "uuid",
  "event_type": "project.planning.requested",
  "project_id": "uuid",
  "tenant_id": "uuid",
  "trace_id": "uuid",
  "timestamp": "2026-04-10T12:00:00Z",
  "actor": {
    "type": "user|agent|system",
    "id": "uuid-or-name"
  },
  "payload": {},
  "meta": {
    "schema_version": "1.0",
    "source_service": "planning-service"
  }
}
```

### 16.3 Требования к обработке событий
- consumer должен быть idempotent;
- сообщения валидируются по JSON schema;
- ошибки обработки уходят в DLQ;
- retry policy: exponential backoff + max attempts.

## 17) Минимальный список микросервисов и API-контракты (MVP)

### 17.1 Сервисы
- `identity-service` (SSO, роли, сессии)
- `project-service` (проекты, требования, вложения)
- `planning-service` (LLM-планирование)
- `agent-orchestrator` (state machine и маршрутизация задач)
- `infra-service` (IaC generation/apply)
- `codegen-service` (генерация кода по стеку)
- `delivery-service` (CI/CD + деплой orchestration)
- `catalog-service` (blueprints/patterns/stacks)
- `llm-adapter-service` (унифицированный доступ к LLM API)
- `site` (публичный маркетинговый фронтенд)

### 17.2 Примеры API (REST, v1)
- `POST /api/v1/projects` — создать проект
- `GET /api/v1/projects/{project_id}` — получить проект
- `POST /api/v1/projects/{project_id}/attachments` — загрузка вложений
- `POST /api/v1/projects/{project_id}/planning/run` — запуск `Продумать`
- `POST /api/v1/projects/{project_id}/infra/plan` — генерация IaC
- `POST /api/v1/projects/{project_id}/infra/apply` — применение IaC
- `POST /api/v1/projects/{project_id}/codegen/run` — генерация кода
- `POST /api/v1/projects/{project_id}/deploy/run` — запуск деплоя
- `GET /api/v1/projects/{project_id}/status` — состояние state machine

### 17.3 Статусы проекта (state machine)
- `IDEA`
- `PLAN_READY`
- `INFRA_IN_PROGRESS`
- `INFRA_READY`
- `CODE_IN_PROGRESS`
- `CODE_READY`
- `DEPLOY_IN_PROGRESS`
- `DEPLOYED`
- `FAILED`

## 18) Acceptance criteria уровня системы (MVP)

- Пользователь проходит SSO и создает проект менее чем за 3 минуты.
- На основе описания идеи создается структурированный план (scope/architecture/backlog/risks).
- Для выбранного infra target генерируется корректный IaC bundle.
- Базовая инфраструктура (GitLab + K8s + Vault) поднимается автоматически.
- По выбранному стеку генерируются backend/frontend и CI/CD pipeline.
- Минимум один деплой в Kubernetes завершается успешно и виден в UI.
- Все ключевые шаги пишут audit log и технические логи.

## 19) Roadmap 8-12 недель

### Недели 1-2: Foundation
- репозитории, базовая архитектура микросервисов, DDD-контуры;
- identity-service + SSO;
- project-service (CRUD проекта и статусы).

### Недели 3-4: Planning
- llm-adapter-service;
- planning-service v1;
- UI поток: идея -> `Продумать` -> версия плана.

### Недели 5-6: Infrastructure
- infra-service: генерация Terraform/Ansible;
- интеграция с orchestrator и событиями Kafka;
- подъем GitLab/K8s/Vault в тестовом окружении.

### Недели 7-8: Codegen & Delivery
- codegen-service (минимум 2 blueprint);
- delivery-service (pipeline + deploy);
- end-to-end: от идеи до деплоя.

### Недели 9-10: Hardening
- observability, аудит, retry/DLQ;
- security baseline и ролевые проверки;
- стабилизация и bugfix.

### Недели 11-12: Go-live preparation
- public `Site`;
- документация, демо-сценарии, онбординг;
- нагрузочные/приемочные тесты и релиз MVP.

## 20) Открытые решения (требуют фиксации до старта)

- Выбор первого cloud-провайдера в MVP.
- Выбор SSO-провайдера (например Keycloak/Auth0/другой).
- Базовые blueprints и их стек (2-3 варианта).
- Политика стоимости использования LLM (лимиты и квоты).
- SLA/SLO на первый релиз.