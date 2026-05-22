"""Разбор решения маршрутизации: полный пайплайн или прямой ответ одного специалиста."""

from __future__ import annotations

import json
import re
from typing import Any

from services.admin_service.ai_team_service import DEFAULT_PIPELINE

VALID_PRIMARY_ROLES = frozenset({"pm", "architect", "devops", "backend", "frontend", "qa"})

# Роль из JSON → step_key в DEFAULT_PIPELINE
PRIMARY_ROLE_TO_STEP: dict[str, str] = {
    "pm": "pm",
    "architect": "architect",
    "devops": "devops_plan",
    "backend": "backend_plan",
    "frontend": "frontend_plan",
    "qa": "qa_validation",
}

ALL_STEP_KEYS: list[str] = [s[0] for s in DEFAULT_PIPELINE]


def _normalize_role(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    r = raw.strip().lower()
    aliases = {
        "devops_engineer": "devops",
        "dev_ops": "devops",
        "fe": "frontend",
        "be": "backend",
        "tester": "qa",
        "quality_assurance": "qa",
    }
    r = aliases.get(r, r)
    return r if r in VALID_PRIMARY_ROLES else None


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3].rstrip()
    return t


_ROLE_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pm",
        re.compile(
            r"(требован|mvp|продукт|user\s*stor|бэклог|roadmap|функциональн)",
            re.I,
        ),
    ),
    (
        "architect",
        re.compile(
            r"(архитектур|стек\s*технолог|микросервис|system\s*design|high[- ]level\s*design)",
            re.I,
        ),
    ),
    (
        "devops",
        re.compile(
            r"(разверн|деплой|deploy|инфра|ci/?cd|docker|kubernetes|k8s|ansible|"
            r"сервер|gitlab|репозит|terraform|helm|nginx|pipeline)",
            re.I,
        ),
    ),
    (
        "backend",
        re.compile(
            r"(\bapi\b|backend|бэкенд|сервис|эндпоинт|rest|graphql|баз[аы]\s*данн|postgres|redis)",
            re.I,
        ),
    ),
    (
        "frontend",
        re.compile(
            r"(сайт|веб[- ]?страниц|ui\b|ux\b|frontend|фронтенд|график|chart|react|vue|angular|"
            r"верстк|лендинг|dashboard|дашборд)",
            re.I,
        ),
    ),
    (
        "qa",
        re.compile(
            r"(тестир|qa\b|smoke|regression|e2e|автотест|проверь\s*работ|баг)",
            re.I,
        ),
    ),
)


def _roles_from_message(user_message: str) -> list[str]:
    text = (user_message or "").strip()
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for role, pat in _ROLE_SIGNALS:
        if pat.search(text) and role not in seen:
            seen.add(role)
            found.append(role)
    return found


def heuristic_orchestration_route(user_message: str) -> dict[str, Any] | None:
    """
    Быстрая маршрутизация без LLM по ключевым словам.
    None — эвристика не уверена, нужен intake через модель.
    """
    roles = _roles_from_message(user_message)
    if not roles:
        return None
    if len(roles) >= 2:
        return {
            "mode": "full",
            "primary_role": None,
            "roles": roles,
            "reason": "heuristic: в запросе несколько зон (роли: " + ", ".join(roles) + ")",
        }
    role = roles[0]
    return {
        "mode": "direct",
        "primary_role": role,
        "roles": [role],
        "reason": f"heuristic: одна зона — {role}",
    }


def parse_orchestration_route(intake_output: str) -> dict[str, Any]:
    """
    Ищет в тексте intake JSON с полем orchestration_route.
    Возвращает mode (full|direct), primary_role, reason.
    """
    default: dict[str, Any] = {"mode": "full", "primary_role": None, "roles": [], "reason": ""}
    text = _strip_markdown_fences(intake_output or "")

    def _normalize_roles(raw: Any) -> list[str]:
        vals: list[str] = []
        if isinstance(raw, list):
            vals = [str(x) for x in raw]
        elif isinstance(raw, str):
            vals = [x.strip() for x in raw.split(",")]
        out: list[str] = []
        seen: set[str] = set()
        for v in vals:
            r = _normalize_role(v)
            if not r or r in seen:
                continue
            seen.add(r)
            out.append(r)
        return out

    def _from_flat(obj: dict[str, Any]) -> dict[str, Any]:
        mode = str(obj.get("mode", "full")).lower().strip()
        if mode not in ("full", "direct"):
            mode = "full"
        role = _normalize_role(obj.get("primary_role") or obj.get("role"))
        roles = _normalize_roles(obj.get("roles") or obj.get("needed_roles"))
        reason = str(obj.get("reason", ""))[:800]
        out = {**default, "reason": reason, "roles": roles}
        out["mode"] = mode
        out["primary_role"] = role
        if not role and len(roles) == 1:
            role = roles[0]
            out["primary_role"] = role
        if mode == "full" and role and len(out["roles"]) <= 1:
            out["mode"] = "direct"
        if mode == "direct" and not role:
            out["mode"] = "full"
        if mode == "full" and role and not roles:
            out["roles"] = [role]
        return out

    if '"orchestration_route"' not in text and "'orchestration_route'" not in text:
        start = text.rfind("{", 0, len(text))
        if start == -1:
            return default
        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            return default
        if isinstance(obj, dict) and "mode" in obj:
            return _from_flat(obj)
        return default

    pos = text.find('"orchestration_route"')
    if pos == -1:
        pos = text.find("'orchestration_route'")
    if pos == -1:
        return default

    start = text.rfind("{", 0, pos)
    if start == -1:
        return default

    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return default

    inner = obj.get("orchestration_route")
    if inner is None and isinstance(obj, dict) and "mode" in obj:
        inner = obj
    if not isinstance(inner, dict):
        return default

    return _from_flat(inner)


def pipeline_for_direct(primary_role: str) -> tuple[list[tuple[str, str, str]], str | None]:
    """
    Только шаг выбранного специалиста (без PM-итога и без остальных ролей).
    Возвращает ([spec], specialist_step_key) или ([], None).
    """
    step_key = PRIMARY_ROLE_TO_STEP.get(primary_role or "")
    if not step_key:
        return [], None
    spec = next((t for t in DEFAULT_PIPELINE if t[0] == step_key), None)
    if not spec:
        return [], None
    return [spec], step_key


def step_keys_to_skip_when_direct(specialist_step_key: str) -> list[str]:
    """Все шаги кроме уже выполненного intake и выбранного специалиста (включая pm_summary)."""
    keep = {specialist_step_key, "intake"}
    return [k for k in ALL_STEP_KEYS if k not in keep]
