"""Load swarm (Ansible bundle) templates — delegates to swarm_store (local or S3)."""

from typing import Any

import yaml

from services.ansible_orchestrator_service.swarm_store import (
    get_bundle,
    list_bundles_meta,
    render_inventory_ini,
    render_playbook as _render_playbook,
    storage_mode,
)
from services.ansible_orchestrator_service.swarm_types import SwarmTemplateMeta, catalog_root

__all__ = [
    "SwarmTemplateMeta",
    "catalog_root",
    "list_swarm_templates",
    "get_template_dir",
    "render_playbook",
    "render_inventory_ini",
]


def list_swarm_templates(*, only_active: bool = True) -> list[SwarmTemplateMeta]:
    """Catalog entries for API (default: only active templates)."""
    return list_bundles_meta(only_active=only_active)


def get_template_dir(template_id: str):
    """Legacy helper — returns pathlib.Path to template dir only for local mode."""
    if storage_mode() == "s3":
        return None
    root = catalog_root()
    if not root.exists():
        return None
    direct = root / template_id
    if direct.is_dir() and (direct / "meta.yaml").exists():
        return direct
    for d in root.iterdir():
        if not d.is_dir():
            continue
        mp = d / "meta.yaml"
        if not mp.exists():
            continue
        raw = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
        if str(raw.get("id", d.name)) == template_id:
            return d
    return None


def render_playbook(template_id: str, variables: dict[str, Any]) -> str:
    return _render_playbook(template_id, variables, require_active=True)
