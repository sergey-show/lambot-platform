"""Parse AI intake routing: full pipeline or a direct single-specialist reply."""

from __future__ import annotations

import json
from typing import Any

from services.admin_service.ai_team_service import DEFAULT_PIPELINE

VALID_PRIMARY_ROLES = frozenset({"sa", "pm", "architect", "devops", "backend", "frontend", "qa"})

# Role from JSON → step_key in DEFAULT_PIPELINE
PRIMARY_ROLE_TO_STEP: dict[str, str] = {
    "sa": "sa",
    "systems_analyst": "sa",
    "analyst": "sa",
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
        "systems_analyst": "sa",
        "system_analyst": "sa",
        "business_analyst": "sa",
        "ba": "sa",
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


def parse_orchestration_route(intake_output: str) -> dict[str, Any]:
    """
    Extract orchestration_route from intake model JSON.
    Returns mode (full|direct), primary_role, roles, live_execution, reason.
    """
    default: dict[str, Any] = {
        "mode": "full",
        "primary_role": None,
        "roles": [],
        "live_execution": False,
        "reason": "",
    }
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
        live_raw = obj.get("live_execution")
        live = bool(live_raw) if isinstance(live_raw, bool) else str(live_raw or "").strip().lower() in (
            "true",
            "1",
            "yes",
        )
        out = {**default, "reason": reason, "roles": roles, "live_execution": live}
        out["mode"] = mode
        out["primary_role"] = role
        if not role and len(roles) == 1:
            role = roles[0]
            out["primary_role"] = role
        if len(roles) >= 2:
            out["mode"] = "full"
            out["primary_role"] = None
        elif mode == "full" and role and len(out["roles"]) <= 1:
            out["mode"] = "direct"
        if mode == "direct" and not role:
            out["mode"] = "full"
        if mode == "full" and role and not roles:
            out["roles"] = [role]
        if "sa" in roles and len(roles) > 1:
            out["mode"] = "full"
            out["primary_role"] = None
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


def route_is_usable(route: dict[str, Any] | None) -> bool:
    """True when intake JSON produced a non-empty routing decision."""
    if not isinstance(route, dict):
        return False
    mode = str(route.get("mode") or "").lower()
    roles = route.get("roles") or []
    primary = route.get("primary_role")
    if mode == "direct" and primary:
        return True
    if mode == "full" and (roles or primary):
        return True
    # Explicit empty full with reason still counts if roles listed
    return bool(roles)


def pipeline_for_direct(primary_role: str) -> tuple[list[tuple[str, str, str]], str | None]:
    """
    Only the selected specialist step (no PM summary and no other roles).
    Returns ([spec], specialist_step_key) or ([], None).
    """
    step_key = PRIMARY_ROLE_TO_STEP.get(primary_role or "")
    if not step_key:
        return [], None
    spec = next((t for t in DEFAULT_PIPELINE if t[0] == step_key), None)
    if not spec:
        return [], None
    return [spec], step_key


def step_keys_to_skip_when_direct(specialist_step_key: str) -> list[str]:
    """All steps except completed intake and the selected specialist (including pm_summary)."""
    keep = {specialist_step_key, "intake"}
    return [k for k in ALL_STEP_KEYS if k not in keep]


def route_includes_sa(route: dict[str, Any] | None) -> bool:
    roles = (route or {}).get("roles") or []
    return "sa" in roles


def enrich_route_with_sa_if_needed(route: dict[str, Any], user_message: str) -> dict[str, Any]:
    """Route is set by intake LLM only — no keyword override."""
    _ = user_message
    return dict(route or {})


# DAG layers after intake (sa optional per orchestration_route.roles).
FULL_DAG_LAYERS: tuple[tuple[str, ...], ...] = (
    ("sa",),
    ("pm",),
    ("architect",),
    ("devops_plan", "backend_plan", "frontend_plan"),
    ("execution_plan",),
    ("implementation",),
    ("deploy",),
    ("qa_validation",),
    ("pm_summary",),
)


def active_dag_layers(route: dict[str, Any] | None = None) -> tuple[tuple[str, ...], ...]:
    if route_includes_sa(route):
        return FULL_DAG_LAYERS
    return tuple(layer for layer in FULL_DAG_LAYERS if layer != ("sa",))
