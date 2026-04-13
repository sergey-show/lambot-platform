"""Журнал действий по проектам в ClickHouse (опционально, если задан CLICKHOUSE_HOST)."""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID, uuid4

_client = None
_table_ready = False

MAX_DETAILS = 900_000
MAX_SUMMARY = 4000


def _enabled() -> bool:
    return bool(os.getenv("CLICKHOUSE_HOST", "").strip())


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not _enabled():
        return None
    try:
        import clickhouse_connect
    except ImportError:
        return None
    # Подключаемся к default: БД из CLICKHOUSE_DATABASE создаётся в ensure_table(); иначе при первом старте
    # клиент падает с UNKNOWN_DATABASE, пока таблица не создана.
    _client = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost").strip(),
        port=int(os.getenv("CLICKHOUSE_HTTP_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default").strip(),
        password=os.getenv("CLICKHOUSE_PASSWORD", "").strip(),
        database="default",
    )
    return _client


def ensure_table() -> None:
    global _table_ready
    if _table_ready or not _enabled():
        return
    client = _get_client()
    if client is None:
        return
    db = os.getenv("CLICKHOUSE_DATABASE", "platform").strip()
    client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {db}.project_activity (
            event_id String,
            ts DateTime64(3) DEFAULT now64(3),
            project_id String,
            tenant_id String,
            actor String,
            subject String,
            action String,
            status String,
            summary String,
            details_json String CODEC(ZSTD(3))
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(toDate(ts))
        ORDER BY (project_id, ts)
        """
    )
    _table_ready = True


def log_activity(
    *,
    project_id: UUID,
    tenant_id: UUID | None,
    action: str,
    status: str,
    summary: str,
    details: dict[str, Any] | None = None,
    actor: str | None = None,
    subject: str | None = None,
) -> None:
    if not _enabled():
        return
    try:
        ensure_table()
        client = _get_client()
        if client is None:
            return
        db = os.getenv("CLICKHOUSE_DATABASE", "platform").strip()
        details_s = json.dumps(details or {}, ensure_ascii=False)
        if len(details_s) > MAX_DETAILS:
            details_s = details_s[: MAX_DETAILS - 40] + '\n... [details truncated]'
        row = [
            str(uuid4()),
            str(project_id),
            str(tenant_id) if tenant_id else "00000000-0000-0000-0000-000000000000",
            (actor or "")[:512],
            (subject or "")[:256],
            action[:128],
            status[:32],
            (summary or "")[:MAX_SUMMARY],
            details_s,
        ]
        client.insert(
            f"{db}.project_activity",
            [row],
            column_names=[
                "event_id",
                "project_id",
                "tenant_id",
                "actor",
                "subject",
                "action",
                "status",
                "summary",
                "details_json",
            ],
        )
    except Exception:
        # журнал не должен ломать основной сценарий
        pass


def fetch_activity(
    project_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not _enabled():
        return []
    try:
        ensure_table()
        client = _get_client()
        if client is None:
            return []
        db = os.getenv("CLICKHOUSE_DATABASE", "platform").strip()
        pid = str(project_id).replace("'", "")
        lim = max(1, min(limit, 200))
        off = max(0, min(offset, 10_000))
        result = client.query(
            f"""
            SELECT event_id, ts, project_id, tenant_id, actor, subject, action, status, summary, details_json
            FROM {db}.project_activity
            WHERE project_id = '{pid}'
            ORDER BY ts DESC
            LIMIT {lim} OFFSET {off}
            """
        )
        rows = result.result_rows
        out: list[dict[str, Any]] = []
        for r in rows:
            details_raw = r[9] if len(r) > 9 else "{}"
            try:
                details_parsed = json.loads(details_raw) if details_raw else {}
            except json.JSONDecodeError:
                details_parsed = {"_raw": details_raw[:2000]}
            out.append(
                {
                    "event_id": r[0],
                    "ts": r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]),
                    "project_id": r[2],
                    "tenant_id": r[3],
                    "actor": r[4] or None,
                    "subject": r[5] or None,
                    "action": r[6],
                    "status": r[7],
                    "summary": r[8],
                    "details": details_parsed,
                }
            )
        return out
    except Exception:
        return []


def clickhouse_status() -> dict[str, Any]:
    if not _enabled():
        return {"enabled": False, "reachable": False}
    try:
        client = _get_client()
        if client is None:
            return {"enabled": True, "reachable": False}
        client.query("SELECT 1")
        return {"enabled": True, "reachable": True}
    except Exception:
        return {"enabled": True, "reachable": False}
