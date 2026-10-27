"""Файлы баз знаний в отдельном S3-бакете (RustFS / MinIO)."""

from __future__ import annotations

import os
from typing import Any

from botocore.exceptions import ClientError

S3_PREFIX = "knowledge"


def _client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("KNOWLEDGE_S3_ENDPOINT_URL", "http://rustfs:9000"),
        aws_access_key_id=os.getenv("KNOWLEDGE_S3_ACCESS_KEY", os.getenv("RUSTFS_ACCESS_KEY", "rustfsadmin")),
        aws_secret_access_key=os.getenv("KNOWLEDGE_S3_SECRET_KEY", os.getenv("RUSTFS_SECRET_KEY", "rustfsadmin")),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def bucket() -> str:
    return os.getenv("KNOWLEDGE_S3_BUCKET", "platform-knowledge")


def ensure_bucket() -> None:
    b = bucket()
    if not b:
        raise RuntimeError("KNOWLEDGE_S3_BUCKET is empty")
    cli = _client()
    try:
        cli.head_bucket(Bucket=b)
        return
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", "") or "")
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    try:
        cli.create_bucket(Bucket=b)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", "") or "")
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            return
        raise


def object_key(*, scope: str, kb_id: str, document_id: str, filename: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)[-200:]
    return f"{S3_PREFIX}/{scope}/{kb_id}/{document_id}/{safe}"


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    ensure_bucket()
    _client().put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)


def get_bytes(key: str) -> bytes:
    resp = _client().get_object(Bucket=bucket(), Key=key)
    return resp["Body"].read()


def delete_object(key: str) -> None:
    try:
        _client().delete_object(Bucket=bucket(), Key=key)
    except ClientError:
        pass
