"""Shared types for Ansible swarm templates."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SwarmTemplateMeta:
    id: str
    name: str
    description: str
    tags: list[str]
    vars_schema: dict[str, Any]
    active: bool = True


def catalog_root() -> Path:
    return Path(__file__).resolve().parent / "catalog" / "swarm"
