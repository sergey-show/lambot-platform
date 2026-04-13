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

from services.agent_graph_service.tool_policy import fetch_tool_policy

ADAPTER_URL = os.getenv("ADAPTER_SERVICE_URL", "http://adapter-service:8008")
PROJECT_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8001")
PLANNING_URL = os.getenv("PLANNING_SERVICE_URL", "http://planning-service:8002")
SANDBOX_URL = os.getenv("SANDBOX_SERVICE_URL", "http://sandbox-service:8012")
SANDBOX_TOKEN = os.getenv("SANDBOX_SERVICE_TOKEN", "internal-sandbox-dev-token")


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
        """Выполнить одну shell-команду на сохранённом SSH-сервере проекта: target_id из get_development_context.ssh_tools.ssh_targets_for_tools[].target_id или из targets при target_kind=ssh. Если ssh_tools.ssh_run_command_available=false — серверов нет, команду не вызывать. Read-only команды (ls, pwd, cat …) — сразу; опасные — needs_confirmation и повтор с user_confirmed=true и confirmation_token. Не передавай секреты в команду."""
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
            return r.text if r.text else json.dumps({"http": r.status_code})
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


BUILTIN_FACTORIES: dict[str, Callable[[str], Any]] = {
    "get_development_context": _make_get_development_context,
    "get_project_status": _make_get_project_status,
    "list_infra_resources": _make_list_infra_resources,
    "run_product_planning": _make_run_product_planning,
    "get_activity_logs": _make_get_activity_logs,
    "get_agent_metrics": _make_get_agent_metrics,
    "ssh_run_command": _make_ssh_run_command,
    "kubernetes_query": _make_kubernetes_query,
    "run_sandbox_python": _make_run_sandbox_python,
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
        if builtin_flags.get(key, True):
            tools.append(factory(pid))

    for spec in policy.get("http_tools") or []:
        if isinstance(spec, dict) and spec.get("tool_name") and spec.get("url_template"):
            tools.append(_make_custom_http_tool(pid, spec))

    return tools
