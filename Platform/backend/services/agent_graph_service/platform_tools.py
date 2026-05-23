"""
Инструменты LangGraph: встроенные (редактируемые из админки) + HTTP GET из БД (шаблон URL).
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from uuid import UUID

import httpx
from langchain_core.tools import StructuredTool, tool

from services.agent_graph_service.git_platform_tools import GIT_PLATFORM_FACTORIES, build_git_platform_tools
from services.agent_graph_service.tool_policy import fetch_tool_policy

ADAPTER_URL = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
PROJECT_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")
PLANNING_URL = os.getenv("PLANNING_SERVICE_URL", "http://planning-service:8002")
SANDBOX_URL = os.getenv("SANDBOX_SERVICE_URL", "http://sandbox-service:8012")
SANDBOX_TOKEN = os.getenv("SANDBOX_SERVICE_TOKEN", "internal-sandbox-dev-token")
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000")
INTERNAL_TOKEN = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")


def _make_get_development_context(pid: str):
    @tool
    def get_development_context() -> str:
        """Сводка для агентов: targets, поле ssh_tools (ssh_run_command_available, ssh_targets_for_tools), K8s, ресурсы, промпты. Всегда вызывай перед утверждениями про SSH или серверы. JSON."""
        try:
            r = httpx.get(
                f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/development-context",
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return get_development_context


def _make_get_project_status(pid: str):
    @tool
    def get_project_status() -> str:
        """Текущий статус workflow проекта (IDEA, PLAN_READY, INFRA_IN_PROGRESS, ...). JSON."""
        try:
            r = httpx.get(
                f"{PROJECT_URL.rstrip('/')}/api/v1/projects/{pid}/status",
                timeout=15.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return get_project_status


def _make_list_infra_resources(pid: str):
    @tool
    def list_infra_resources() -> str:
        """Список ресурсов инфраструктуры проекта (URL, метки; флаг has_secret без значений секретов). JSON."""
        try:
            r = httpx.get(
                f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/infra/resources",
                params={"include_disabled": "false"},
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return list_infra_resources


def _make_run_product_planning(pid: str):
    @tool
    def run_product_planning(idea: str) -> str:
        """Запускает планирование MVP продукта (LLM + события воркфлоу): идея стартапа или сервиса. Вызывай, когда пользователь хочет сформировать план, описать продукт с нуля или прогнать шаг IDEA→PLAN. Передавай полный текст идеи пользователя (не сокращай)."""
        snippet = (idea or "").strip()
        if len(snippet) < 8:
            return json.dumps(
                {
                    "error": "idea_too_short",
                    "hint": "Опишите продукт подробнее (несколько предложений): аудитория, задача, ценность.",
                }
            )
        if len(snippet) > 12000:
            snippet = snippet[:12000]
        try:
            r = httpx.post(
                f"{PLANNING_URL.rstrip('/')}/api/v1/projects/{pid}/planning/run",
                json={"idea": snippet, "attachments": []},
                timeout=180.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return run_product_planning


def _make_get_activity_logs(pid: str):
    @tool
    def get_activity_logs(limit: int = 40) -> str:
        """Журнал действий по проекту: Ansible, SSH, назначения, операции (ClickHouse). Верни JSON с items (время, действие, статус, summary, детали)."""
        try:
            try:
                lim = max(1, min(int(limit), 80))
            except (TypeError, ValueError):
                lim = 40
            r = httpx.get(
                f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/activity-logs",
                params={"limit": lim, "offset": 0},
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return get_activity_logs


def _make_get_agent_metrics(pid: str):
    @tool
    def get_agent_metrics(limit: int = 30) -> str:
        """Последние метрики с удалённых агентов на серверах проекта (heartbeat, payload). JSON."""
        try:
            try:
                lim = max(1, min(int(limit), 100))
            except (TypeError, ValueError):
                lim = 30
            r = httpx.get(
                f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/agent/metrics",
                params={"limit": lim},
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return get_agent_metrics


def _make_ssh_run_command(pid: str):
    @tool
    def ssh_run_command(
        target_id: str,
        command: str,
        user_confirmed: bool = False,
        confirmation_token: str = "",
    ) -> str:
        """Выполнить одну shell-команду на сохранённом SSH-сервере проекта: target_id из get_development_context.ssh_tools.ssh_targets_for_tools[].target_id или из targets при target_kind=ssh. Если ssh_tools.ssh_run_command_available=false — серверов нет, команду не вызывать. Опасные команды: платформа может выполнить повтор с подтверждением автоматически (без участия пользователя в чате). При необходимости явного согласия пользователя передай user_confirmed=true и confirmation_token из ответа adapter. Не передавай секреты в команду."""
        tid = (target_id or "").strip()
        cmd = (command or "").strip()
        if not tid or not cmd:
            return json.dumps({"error": "target_id и command обязательны"})
        payload: dict[str, Any] = {
            "command": cmd,
            "confirmed": bool(user_confirmed),
        }
        ct = (confirmation_token or "").strip()
        if ct:
            payload["confirmation_token"] = ct
        try:
            r = httpx.post(
                f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/targets/{tid}/ssh-exec",
                json=payload,
                timeout=180.0,
            )
            txt = r.text if r.text else json.dumps({"http": r.status_code})
            if r.status_code == 200 and not user_confirmed:
                try:
                    data = r.json()
                except Exception:
                    return txt
                if isinstance(data, dict) and data.get("needs_confirmation"):
                    tok = str(data.get("confirmation_token") or "").strip()
                    if tok:
                        r2 = httpx.post(
                            f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/targets/{tid}/ssh-exec",
                            json={
                                "command": cmd,
                                "confirmed": True,
                                "confirmation_token": tok,
                            },
                            timeout=180.0,
                        )
                        return r2.text if r2.text else json.dumps({"http": r2.status_code})
            return txt
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return ssh_run_command


def _make_kubernetes_query(pid: str):
    @tool
    def kubernetes_query(
        operation: str,
        target_id: str,
        namespace: str = "",
        name: str = "",
        resource: str = "",
        container: str = "",
        tail_lines: int = 200,
        all_namespaces: bool = False,
        wide: bool = False,
        replicas: int | None = None,
    ) -> str:
        """Управление Kubernetes по kubeconfig-таргету проекта (kubectl внутри платформы). operation: cluster_info, version, nodes, namespaces, pods, deployments, services, ingresses, statefulsets, daemonsets, events, describe, logs, top_nodes, top_pods, rollout_status, scale, rollout_restart. target_id — UUID из kubernetes_clusters в контексте. Для describe укажи resource (pod, deployment, …) и name; для logs — namespace, name (pod); для scale/rollout — namespace и name (deployment), для scale ещё replicas."""
        op = (operation or "").strip().lower()
        tid = (target_id or "").strip()
        if not tid:
            return json.dumps({"error": "target_id required"})
        payload: dict[str, Any] = {
            "target_id": tid,
            "operation": op,
            "tail_lines": int(tail_lines) if tail_lines else 200,
            "all_namespaces": bool(all_namespaces),
            "wide": bool(wide),
        }
        ns = (namespace or "").strip()
        if ns:
            payload["namespace"] = ns
        nm = (name or "").strip()
        if nm:
            payload["name"] = nm
        res = (resource or "").strip()
        if res:
            payload["resource"] = res
        ct = (container or "").strip()
        if ct:
            payload["container"] = ct
        if replicas is not None:
            try:
                payload["replicas"] = int(replicas)
            except (TypeError, ValueError):
                pass
        try:
            r = httpx.post(
                f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/k8s/query",
                json=payload,
                timeout=180.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return kubernetes_query


def _make_run_sandbox_python(pid: str):
    @tool
    def run_sandbox_python(code: str) -> str:
        """Выполнить короткий Python-код в песочнице платформы (ограничение по времени и размеру). Верни stdout/stderr."""
        _ = pid  # контекст проекта для будущей политики
        snippet = (code or "").strip()
        if len(snippet) > 24000:
            return json.dumps({"error": "code too long", "max_chars": 24000})
        try:
            r = httpx.post(
                f"{SANDBOX_URL.rstrip('/')}/api/v1/sandbox/python",
                headers={"X-Sandbox-Token": SANDBOX_TOKEN, "Content-Type": "application/json"},
                json={"code": snippet, "timeout_sec": 25},
                timeout=60.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return run_sandbox_python


def _make_http_request(pid: str):
    @tool
    def http_request(
        endpoint_alias: str,
        method: str = "GET",
        path: str = "",
        query: dict | None = None,
        headers: dict | None = None,
        json_body: dict | list | None = None,
        text_body: str = "",
        timeout_sec: int = 20,
    ) -> str:
        """Универсальный HTTP запрос через безопасный executor gateway. endpoint_alias: adapter|project|planning|catalog|agent_graph|documents. Используй path только внутри alias (можно с {project_id}); method: GET/POST/PUT/PATCH/DELETE. Для тела используй json_body или text_body. Секретные/служебные заголовки передавать нельзя."""
        p = (path or "").strip()
        if not p:
            return json.dumps({"error": "path required"})
        payload: dict[str, Any] = {
            "endpoint_alias": (endpoint_alias or "").strip().lower(),
            "method": (method or "GET").strip().upper(),
            "path": p,
            "query": query if isinstance(query, dict) else {},
            "headers": headers if isinstance(headers, dict) else {},
            "timeout_sec": int(timeout_sec) if timeout_sec else 20,
        }
        if json_body is not None:
            payload["json_body"] = json_body
        elif (text_body or "").strip():
            payload["text_body"] = text_body
        try:
            r = httpx.post(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/http/execute",
                headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                json=payload,
                timeout=65.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return http_request


def _make_browser_session(pid: str):
    @tool
    def browser_session(
        action: str,
        run_id: str = "",
        session_id: str = "",
        url: str = "",
        selector: str = "",
        text: str = "",
        timeout_sec: int = 25,
        timeout_ms: int = 12000,
        wait_until: str = "domcontentloaded",
        full_page: bool = False,
        viewport_width: int = 1366,
        viewport_height: int = 900,
    ) -> str:
        """Браузерный инструмент Playwright (через browser-tool-service). action=create|navigate|click|type|fill|press|wait|extract_text|content|snapshot|screenshot|close. Для create можно передать url (start_url) и viewport_*; для остальных нужен session_id. run_id передавай для привязки логов браузера к текущему прогону оркестрации."""
        act = (action or "").strip().lower()
        payload: dict[str, Any] = {
            "action": act,
            "timeout_sec": int(timeout_sec) if timeout_sec else 25,
            "timeout_ms": int(timeout_ms) if timeout_ms else 12000,
            "wait_until": (wait_until or "domcontentloaded").strip(),
            "full_page": bool(full_page),
        }
        if run_id.strip():
            payload["run_id"] = run_id.strip()
        if session_id.strip():
            payload["session_id"] = session_id.strip()
        if selector.strip():
            payload["selector"] = selector.strip()
        if text:
            payload["text"] = text
        if act == "create":
            payload["start_url"] = (url or "").strip() or None
            payload["viewport_width"] = int(viewport_width) if viewport_width else 1366
            payload["viewport_height"] = int(viewport_height) if viewport_height else 900
        elif url.strip():
            payload["url"] = url.strip()
        try:
            r = httpx.post(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/browser/execute",
                headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                json=payload,
                timeout=90.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return browser_session


def _make_get_project_docs_context(pid: str):
    @tool
    def get_project_docs_context(query: str = "", doc_types_csv: str = "", top_k: int = 8) -> str:
        """Поиск в wiki проекта (RAG по запросу). Вызывай только когда нужны факты из документации; query — конкретный вопрос (не весь user_message). doc_types_csv: architecture,user_guide,admin_guide. top_k 3–8."""
        types = []
        for raw in (doc_types_csv or "").split(","):
            v = raw.strip().lower()
            if v:
                types.append(v)
        try:
            k = max(1, min(int(top_k), 20))
        except (TypeError, ValueError):
            k = 8
        payload = {"query": (query or "").strip(), "doc_types": types, "top_k": k}
        try:
            r = httpx.post(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/documents/context",
                headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                json=payload,
                timeout=60.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return get_project_docs_context


def _make_upsert_project_document(pid: str):
    @tool
    def upsert_project_document(
        doc_type: str,
        title: str,
        content_md: str,
        slug: str = "",
        tags_csv: str = "",
        change_summary: str = "",
        run_id: str = "",
    ) -> str:
        """Создать/обновить wiki-документ проекта и новую версию. Используй после решения задачи для: architecture, user_guide, admin_guide."""
        if not title.strip() or not content_md.strip():
            return json.dumps({"error": "title and content_md are required"})
        tags = [x.strip().lower() for x in tags_csv.split(",") if x.strip()]
        payload = {
            "doc_type": (doc_type or "other").strip().lower(),
            "title": title.strip(),
            "content_md": content_md.strip(),
            "slug": slug.strip() or None,
            "tags": tags,
            "change_summary": (change_summary or "Updated by agent").strip(),
            "author_type": "agent",
            "author_id": "agent-graph",
            "run_id": (run_id or "").strip() or None,
        }
        try:
            r = httpx.post(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/documents/upsert",
                headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                json=payload,
                timeout=90.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return upsert_project_document


def _make_list_project_documents(pid: str):
    @tool
    def list_project_documents(doc_type: str = "", status: str = "active", limit: int = 100) -> str:
        """Список wiki-документов проекта (фильтр по типу/статусу)."""
        params: dict[str, str] = {"limit": str(max(1, min(int(limit or 100), 300)))}
        if doc_type.strip():
            params["doc_type"] = doc_type.strip().lower()
        if status.strip():
            params["status"] = status.strip().lower()
        try:
            r = httpx.get(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/projects/{pid}/documents",
                params=params,
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return list_project_documents


def _make_ansible_run_template(pid: str):
    @tool
    def ansible_run_template(
        target_id: str,
        template_id: str,
        variables_json: str = "{}",
        allow_fallback: bool = False,
        fallback_service: str = "",
    ) -> str:
        """Запустить Ansible-шаблон из внутреннего каталога marketplace на SSH target проекта. Если шаблон не найден и allow_fallback=true, можно выполнить fallback_service=gitlab|docker|nexus через SSH."""
        tid = (target_id or "").strip()
        tpl = (template_id or "").strip()
        if not tid or not tpl:
            return json.dumps({"error": "target_id and template_id are required"})
        vars_dict: dict[str, Any] = {}
        raw = (variables_json or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    vars_dict = parsed
            except Exception:
                return json.dumps({"error": "variables_json must be valid JSON object"})
        payload = {
            "target_id": tid,
            "template_id": tpl,
            "variables": vars_dict,
            "allow_fallback": bool(allow_fallback),
            "fallback_service": (fallback_service or "").strip().lower() or None,
        }
        try:
            r = httpx.post(
                f"{ADAPTER_URL.rstrip('/')}/api/v1/projects/{pid}/infra/run-template",
                json=payload,
                timeout=1800.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return ansible_run_template


def _parse_meta_json(meta_json: str) -> dict[str, Any]:
    raw = (meta_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _make_tracker_list_items(pid: str):
    @tool
    def tracker_list_items(kind: str = "", limit: int = 200) -> str:
        """Список эпиков и задач трекера проекта (видно в UI «Задачи агентов»). kind: epic|task или пусто."""
        params: dict[str, str] = {"limit": str(max(1, min(int(limit or 200), 500)))}
        k = (kind or "").strip().lower()
        if k in ("epic", "task"):
            params["kind"] = k
        try:
            r = httpx.get(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/tracker/items",
                headers={"X-Internal-Token": INTERNAL_TOKEN},
                params=params,
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return tracker_list_items


def _make_tracker_create_epic(pid: str):
    @tool
    def tracker_create_epic(
        title: str,
        description: str = "",
        meta_json: str = "",
        run_id: str = "",
        seq: int = 0,
    ) -> str:
        """Создать эпик в трекере (backlog). meta_json — JSON: requirement_id, run_id и др."""
        if not title.strip():
            return json.dumps({"error": "title is required"})
        meta = _parse_meta_json(meta_json)
        rid = (run_id or "").strip()
        if rid:
            meta["run_id"] = rid
        payload: dict[str, Any] = {
            "kind": "epic",
            "title": title.strip(),
            "description": description or "",
            "seq": int(seq or 0),
        }
        if meta:
            payload["meta"] = meta
        try:
            r = httpx.post(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/tracker/items",
                headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                json=payload,
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return tracker_create_epic


def _make_tracker_create_task(pid: str):
    @tool
    def tracker_create_task(
        parent_epic_id: str,
        title: str,
        assignee_role: str,
        description: str = "",
        meta_json: str = "",
        run_id: str = "",
        requirement_id: str = "",
        seq: int = 0,
    ) -> str:
        """Создать задачу под эпиком с ролью агента (devops|backend|frontend|qa|architect|pm|sa)."""
        if not title.strip():
            return json.dumps({"error": "title is required"})
        parent = (parent_epic_id or "").strip()
        if not parent:
            return json.dumps({"error": "parent_epic_id is required"})
        role = (assignee_role or "").strip().lower()
        if not role:
            return json.dumps({"error": "assignee_role is required"})
        meta = _parse_meta_json(meta_json)
        rid = (run_id or "").strip()
        if rid:
            meta["run_id"] = rid
        req = (requirement_id or "").strip()
        if req:
            meta["requirement_id"] = req
        payload: dict[str, Any] = {
            "kind": "task",
            "parent_id": parent,
            "title": title.strip(),
            "description": description or "",
            "assignee_role": role,
            "seq": int(seq or 0),
        }
        if meta:
            payload["meta"] = meta
        try:
            r = httpx.post(
                f"{API_GATEWAY_URL.rstrip('/')}/api/v1/internal/projects/{pid}/tracker/items",
                headers={"X-Internal-Token": INTERNAL_TOKEN, "Content-Type": "application/json"},
                json=payload,
                timeout=30.0,
            )
            return r.text if r.text else json.dumps({"http": r.status_code})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return tracker_create_task


BUILTIN_FACTORIES: dict[str, Callable[[str], Any]] = {
    **GIT_PLATFORM_FACTORIES,
    "get_development_context": _make_get_development_context,
    "get_project_status": _make_get_project_status,
    "list_infra_resources": _make_list_infra_resources,
    "run_product_planning": _make_run_product_planning,
    "get_activity_logs": _make_get_activity_logs,
    "get_agent_metrics": _make_get_agent_metrics,
    "ssh_run_command": _make_ssh_run_command,
    "kubernetes_query": _make_kubernetes_query,
    "run_sandbox_python": _make_run_sandbox_python,
    "http_request": _make_http_request,
    "browser_session": _make_browser_session,
    "get_project_docs_context": _make_get_project_docs_context,
    "upsert_project_document": _make_upsert_project_document,
    "list_project_documents": _make_list_project_documents,
    "ansible_run_template": _make_ansible_run_template,
    "tracker_list_items": _make_tracker_list_items,
    "tracker_create_epic": _make_tracker_create_epic,
    "tracker_create_task": _make_tracker_create_task,
}


def _http_get_tool_impl(url: str) -> str:
    try:
        r = httpx.get(url, timeout=30.0)
        return r.text if r.text else json.dumps({"http": r.status_code})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _make_custom_http_tool(pid: str, spec: dict[str, Any]) -> StructuredTool:
    name = spec["tool_name"]
    desc = spec["description"]
    tpl = spec["url_template"]

    def _run() -> str:
        url = tpl.format(project_id=pid)
        return _http_get_tool_impl(url)

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=desc,
    )


def build_platform_tools(project_id: UUID) -> list[Any]:
    pid = str(project_id)
    policy = fetch_tool_policy()
    builtin_flags: dict[str, bool] = policy.get("builtin") or {}
    tools: list[Any] = []

    for key, factory in BUILTIN_FACTORIES.items():
        if key in GIT_PLATFORM_FACTORIES:
            continue
        if builtin_flags.get(key, True):
            tools.append(factory(pid))
    tools.extend(build_git_platform_tools(project_id, builtin_flags))

    for spec in policy.get("http_tools") or []:
        if isinstance(spec, dict) and spec.get("tool_name") and spec.get("url_template"):
            tools.append(_make_custom_http_tool(pid, spec))

    return tools
