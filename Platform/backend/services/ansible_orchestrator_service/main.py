import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from services.ansible_orchestrator_service.catalog import (
    list_swarm_templates,
    render_inventory_ini,
    render_playbook,
)
from services.ansible_orchestrator_service.swarm_store import (
    delete_bundle,
    get_bundle,
    list_bundles_meta,
    put_bundle,
    render_playbook as render_playbook_raw,
    seed_from_local_catalog_if_configured,
    set_active,
    validate_template_id,
)


def require_superadmin(x_superadmin_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("SUPERADMIN_TOKEN", "superadmin-dev-token")
    if x_superadmin_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        seed_from_local_catalog_if_configured()
    except Exception as exc:
        import logging

        logging.getLogger("uvicorn.error").warning("Ansible S3 seed skipped: %s", exc)
    yield


app = FastAPI(title="ansible-orchestrator-service", version="0.2.0", lifespan=lifespan)


class RenderRequest(BaseModel):
    template_id: str
    variables: dict[str, Any] = Field(default_factory=dict)


class PrepareBundleRequest(BaseModel):
    template_id: str
    variables: dict[str, Any] = Field(default_factory=dict)
    inventory_host: str
    inventory_user: str
    inventory_port: int = 22


class TemplateCreateBody(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    meta_yaml: str
    playbook_j2: str


class TemplateUpdateBody(BaseModel):
    meta_yaml: str
    playbook_j2: str


class ActivePatchBody(BaseModel):
    active: bool


class PreviewBody(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/ansible/swarm/catalog")
def swarm_catalog():
    return [
        {
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "tags": m.tags,
            "vars_schema": m.vars_schema,
        }
        for m in list_swarm_templates(only_active=True)
    ]


@app.get("/api/v1/ansible/swarm/templates/{template_id}")
def swarm_template_detail(template_id: str):
    for m in list_swarm_templates(only_active=True):
        if m.id == template_id:
            return {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "tags": m.tags,
                "vars_schema": m.vars_schema,
            }
    raise HTTPException(status_code=404, detail="Template not found")


@app.post("/api/v1/ansible/swarm/render")
def swarm_render(body: RenderRequest):
    try:
        playbook = render_playbook(body.template_id, body.variables)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"template_id": body.template_id, "playbook_yaml": playbook}


@app.post("/api/v1/ansible/swarm/prepare-bundle")
def prepare_bundle(body: PrepareBundleRequest):
    """Ready artifact to run ansible-playbook locally or in CI."""
    try:
        playbook = render_playbook(body.template_id, body.variables)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    inventory = render_inventory_ini(
        body.inventory_host,
        body.inventory_user,
        body.inventory_port,
    )
    return {
        "template_id": body.template_id,
        "playbook_yaml": playbook,
        "inventory_ini": inventory,
        "hint": "ansible-playbook -i inventory.ini playbook.yml",
    }


@app.get("/api/v1/ansible/version")
def ansible_info():
    return {
        "orchestrator": "ansible-orchestrator-service",
        "ansible_core_in_image": os.getenv("ANSIBLE_INSTALLED", "false"),
        "storage": os.getenv("ANSIBLE_S3_BUCKET", "") and "s3" or "local",
    }


# ——— Superadmin: template CRUD (X-Superadmin-Token) ———


@app.get("/api/v1/admin/ansible/swarm/templates")
def admin_list_templates(_: None = Depends(require_superadmin)):
    return [
        {
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "tags": m.tags,
            "vars_schema": m.vars_schema,
            "active": m.active,
        }
        for m in list_bundles_meta(only_active=False)
    ]


@app.get("/api/v1/admin/ansible/swarm/templates/{template_id}")
def admin_get_template(template_id: str, _: None = Depends(require_superadmin)):
    b = get_bundle(template_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return {
        "id": b.id,
        "meta_yaml": b.meta_yaml,
        "playbook_j2": b.playbook_j2,
        "active": b.meta.active,
        "name": b.meta.name,
        "description": b.meta.description,
        "tags": b.meta.tags,
        "vars_schema": b.meta.vars_schema,
    }


@app.post("/api/v1/admin/ansible/swarm/templates", status_code=201)
def admin_create_template(body: TemplateCreateBody, _: None = Depends(require_superadmin)):
    try:
        validate_template_id(body.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if get_bundle(body.id) is not None:
        raise HTTPException(status_code=409, detail="Template already exists")
    try:
        put_bundle(body.id, body.meta_yaml, body.playbook_j2)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    b = get_bundle(body.id)
    assert b is not None
    return {"id": b.id, "active": b.meta.active}


@app.put("/api/v1/admin/ansible/swarm/templates/{template_id}")
def admin_put_template(
    template_id: str, body: TemplateUpdateBody, _: None = Depends(require_superadmin)
):
    if get_bundle(template_id) is None:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        put_bundle(template_id, body.meta_yaml, body.playbook_j2)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@app.delete("/api/v1/admin/ansible/swarm/templates/{template_id}")
def admin_delete_template(template_id: str, _: None = Depends(require_superadmin)):
    if get_bundle(template_id) is None:
        raise HTTPException(status_code=404, detail="Template not found")
    delete_bundle(template_id)
    return {"ok": True}


@app.patch("/api/v1/admin/ansible/swarm/templates/{template_id}/active")
def admin_patch_active(
    template_id: str, body: ActivePatchBody, _: None = Depends(require_superadmin)
):
    try:
        b = set_active(template_id, body.active)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"id": b.id, "active": b.meta.active}


@app.post("/api/v1/admin/ansible/swarm/templates/{template_id}/preview")
def admin_preview_render(
    template_id: str, body: PreviewBody, _: None = Depends(require_superadmin)
):
    """Render playbook even if template is inactive (for IDE checks)."""
    try:
        yaml_text = render_playbook_raw(template_id, body.variables, require_active=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"template_id": template_id, "playbook_yaml": yaml_text}
