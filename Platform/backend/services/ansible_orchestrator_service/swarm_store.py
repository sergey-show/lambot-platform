"""Swarm Ansible templates: local filesystem or S3 (e.g. RustFS)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from botocore.exceptions import ClientError
from jinja2 import Environment, select_autoescape

from services.ansible_orchestrator_service.swarm_types import SwarmTemplateMeta, catalog_root

S3_PREFIX = "ansible-swarm"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def storage_mode() -> Literal["s3", "local"]:
    return "s3" if os.getenv("ANSIBLE_S3_BUCKET") else "local"


def _s3_client():
    import boto3
    from botocore.config import Config

    endpoint = os.getenv("ANSIBLE_S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    access = os.getenv("ANSIBLE_S3_ACCESS_KEY", "rustfsadmin")
    secret = os.getenv("ANSIBLE_S3_SECRET_KEY", "rustfsadmin")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def s3_bucket() -> str:
    return os.getenv("ANSIBLE_S3_BUCKET", "")


def ensure_bucket_exists() -> None:
    bucket = s3_bucket()
    if not bucket:
        return
    cli = _s3_client()
    try:
        cli.head_bucket(Bucket=bucket)
    except ClientError:
        cli.create_bucket(Bucket=bucket)


def _s3_key(template_id: str, name: str) -> str:
    return f"{S3_PREFIX}/{template_id}/{name}"


def _s3_meta_exists(cli, bucket: str, template_id: str) -> bool:
    try:
        cli.head_object(Bucket=bucket, Key=_s3_key(template_id, "meta.yaml"))
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound", "not_found"):
            return False
        raise


def seed_from_local_catalog_if_configured() -> None:
    """When ANSIBLE_S3_SEED_LOCAL: uploads on-disk templates to S3.

    By default only uploads missing objects (if meta.yaml already exists — skip).
    When ANSIBLE_S3_SYNC_LOCAL=1 overwrites meta.yaml and playbook.yml.j2 from the image for all
    local templates (needed after playbook updates; otherwise RustFS keeps old versions).
    """
    if storage_mode() != "s3":
        return
    if os.getenv("ANSIBLE_S3_SEED_LOCAL", "1") not in ("1", "true", "yes"):
        return
    sync_overwrite = os.getenv("ANSIBLE_S3_SYNC_LOCAL", "").strip().lower() in ("1", "true", "yes")
    ensure_bucket_exists()
    cli = _s3_client()
    bucket = s3_bucket()
    root = catalog_root()
    if not root.exists():
        return
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.yaml"
        pb_path = d / "playbook.yml.j2"
        if not meta_path.exists() or not pb_path.exists():
            continue
        raw = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        tid = str(raw.get("id", d.name))
        if not sync_overwrite and _s3_meta_exists(cli, bucket, tid):
            continue
        cli.put_object(
            Bucket=bucket,
            Key=_s3_key(tid, "meta.yaml"),
            Body=meta_path.read_bytes(),
            ContentType="text/yaml",
        )
        cli.put_object(
            Bucket=bucket,
            Key=_s3_key(tid, "playbook.yml.j2"),
            Body=pb_path.read_bytes(),
            ContentType="text/plain",
        )


@dataclass
class SwarmTemplateBundle:
    id: str
    meta: SwarmTemplateMeta
    meta_yaml: str
    playbook_j2: str


def _parse_meta_yaml(raw_text: str, fallback_id: str) -> SwarmTemplateMeta:
    raw = yaml.safe_load(raw_text) or {}
    return SwarmTemplateMeta(
        id=str(raw.get("id", fallback_id)),
        name=str(raw.get("name", fallback_id)),
        description=str(raw.get("description", "")),
        tags=list(raw.get("tags", [])),
        vars_schema=dict(raw.get("vars_schema", {})),
        active=bool(raw.get("active", True)),
    )


def list_template_ids() -> list[str]:
    if storage_mode() == "s3":
        ensure_bucket_exists()
        cli = _s3_client()
        bucket = s3_bucket()
        out: set[str] = set()
        token = None
        pfx = f"{S3_PREFIX}/"
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": pfx}
            if token:
                kwargs["ContinuationToken"] = token
            resp = cli.list_objects_v2(**kwargs)
            for cp in resp.get("CommonPrefixes", []):
                prefix = cp.get("Prefix", "").rstrip("/")
                parts = prefix.split("/")
                if len(parts) >= 2:
                    out.add(parts[1])
            for obj in resp.get("Contents", []):
                key = obj.get("Key") or ""
                if not key.startswith(pfx):
                    continue
                rel = key[len(pfx) :]
                tid = rel.split("/", 1)[0]
                if tid:
                    out.add(tid)
            token = resp.get("NextContinuationToken")
            if not resp.get("IsTruncated"):
                break
        return sorted(out)

    root = catalog_root()
    ids: list[str] = []
    if not root.exists():
        return ids
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "meta.yaml").exists():
            raw = yaml.safe_load((d / "meta.yaml").read_text(encoding="utf-8"))
            ids.append(str(raw.get("id", d.name)))
    return ids


def get_bundle(template_id: str) -> SwarmTemplateBundle | None:
    if storage_mode() == "s3":
        ensure_bucket_exists()
        cli = _s3_client()
        bucket = s3_bucket()
        try:
            m = cli.get_object(Bucket=bucket, Key=_s3_key(template_id, "meta.yaml"))
            meta_yaml = m["Body"].read().decode("utf-8")
            p = cli.get_object(Bucket=bucket, Key=_s3_key(template_id, "playbook.yml.j2"))
            playbook_j2 = p["Body"].read().decode("utf-8")
        except ClientError:
            return None
        meta = _parse_meta_yaml(meta_yaml, template_id)
        return SwarmTemplateBundle(
            id=meta.id, meta=meta, meta_yaml=meta_yaml, playbook_j2=playbook_j2
        )

    root = catalog_root()
    for d in root.iterdir() if root.exists() else []:
        if not d.is_dir():
            continue
        mp = d / "meta.yaml"
        if not mp.exists():
            continue
        raw_text = mp.read_text(encoding="utf-8")
        meta = _parse_meta_yaml(raw_text, d.name)
        if meta.id == template_id:
            pb = d / "playbook.yml.j2"
            if not pb.exists():
                return None
            return SwarmTemplateBundle(
                id=meta.id,
                meta=meta,
                meta_yaml=raw_text,
                playbook_j2=pb.read_text(encoding="utf-8"),
            )
    return None


def list_bundles_meta(*, only_active: bool) -> list[SwarmTemplateMeta]:
    out: list[SwarmTemplateMeta] = []
    for tid in list_template_ids():
        b = get_bundle(tid)
        if b and (not only_active or b.meta.active):
            out.append(b.meta)
    return sorted(out, key=lambda m: m.id)


def validate_template_id(tid: str) -> None:
    if not _ID_RE.match(tid):
        raise ValueError(
            "template id must match ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase, digits, hyphen)"
        )


def put_bundle(
    template_id: str,
    meta_yaml: str,
    playbook_j2: str,
) -> SwarmTemplateBundle:
    validate_template_id(template_id)
    _parse_meta_yaml(meta_yaml, template_id)  # validate YAML
    parsed = _parse_meta_yaml(meta_yaml, template_id)
    if parsed.id != template_id:
        raise ValueError("meta.yaml id must match URL id")

    if storage_mode() == "s3":
        ensure_bucket_exists()
        cli = _s3_client()
        bucket = s3_bucket()
        cli.put_object(
            Bucket=bucket,
            Key=_s3_key(template_id, "meta.yaml"),
            Body=meta_yaml.encode("utf-8"),
            ContentType="text/yaml",
        )
        cli.put_object(
            Bucket=bucket,
            Key=_s3_key(template_id, "playbook.yml.j2"),
            Body=playbook_j2.encode("utf-8"),
            ContentType="text/plain",
        )
    else:
        d = catalog_root() / template_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.yaml").write_text(meta_yaml, encoding="utf-8")
        (d / "playbook.yml.j2").write_text(playbook_j2, encoding="utf-8")

    b = get_bundle(template_id)
    assert b is not None
    return b


def delete_bundle(template_id: str) -> None:
    if storage_mode() == "s3":
        cli = _s3_client()
        bucket = s3_bucket()
        for name in ("meta.yaml", "playbook.yml.j2"):
            try:
                cli.delete_object(Bucket=bucket, Key=_s3_key(template_id, name))
            except ClientError:
                pass
    else:
        d = catalog_root() / template_id
        if d.is_dir():
            for f in ("meta.yaml", "playbook.yml.j2"):
                p = d / f
                if p.exists():
                    p.unlink()
            try:
                d.rmdir()
            except OSError:
                pass


def set_active(template_id: str, active: bool) -> SwarmTemplateBundle:
    b = get_bundle(template_id)
    if b is None:
        raise ValueError("Template not found")
    raw = yaml.safe_load(b.meta_yaml) or {}
    raw["active"] = active
    new_yaml = yaml.dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return put_bundle(template_id, new_yaml, b.playbook_j2)


def render_playbook(template_id: str, variables: dict[str, Any], *, require_active: bool) -> str:
    b = get_bundle(template_id)
    if b is None:
        raise ValueError(f"Unknown template: {template_id}")
    if require_active and not b.meta.active:
        raise ValueError(f"Template is inactive: {template_id}")
    from jinja2 import DictLoader

    env = Environment(
        loader=DictLoader({"playbook.yml.j2": b.playbook_j2}),
        autoescape=select_autoescape(enabled_extensions=()),
    )
    env.filters["to_json"] = lambda v: json.dumps(v, ensure_ascii=False)
    tmpl = env.get_template("playbook.yml.j2")
    return tmpl.render(**variables)


def render_inventory_ini(host: str, ssh_user: str, ssh_port: int = 22) -> str:
    return f"""[target]
{host} ansible_user={ssh_user} ansible_port={ssh_port} ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
"""
