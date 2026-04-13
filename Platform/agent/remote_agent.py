#!/usr/bin/env python3
"""
Агент на стороне пользовательского сервера: шлёт метрики в platform ingest.
Переменные: LAMBOT_INGEST_URL, LAMBOT_AGENT_TOKEN (задаёт Ansible/playbook или systemd).
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request


def collect_payload() -> dict:
    load_avg: list[float] = []
    try:
        load_avg = list(os.getloadavg())
    except (AttributeError, OSError):
        pass
    return {
        "hostname": socket.gethostname(),
        "ts": time.time(),
        "loadavg": load_avg,
        "agent": "lambot-remote-v1",
    }


def push_once() -> None:
    url = os.environ.get("LAMBOT_INGEST_URL", "").strip()
    token = os.environ.get("LAMBOT_AGENT_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError("LAMBOT_INGEST_URL and LAMBOT_AGENT_TOKEN required")
    body = json.dumps({"payload": collect_payload()}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main() -> None:
    interval = int(os.environ.get("LAMBOT_AGENT_INTERVAL_SEC", "60"))
    while True:
        try:
            push_once()
        except urllib.error.HTTPError as e:
            print("ingest http error", e.code, e.read().decode(errors="ignore"))
        except Exception as e:
            print("ingest error", e)
        time.sleep(max(interval, 10))


if __name__ == "__main__":
    main()
