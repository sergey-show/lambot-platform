"""SSH: access check and remote agent install (Paramiko)."""
from __future__ import annotations

import base64
import io
import shlex
from typing import Tuple

import paramiko

# Keep in sync with Platform/agent/remote_agent.py
REMOTE_AGENT_PY = r'''#!/usr/bin/env python3
"""Agent on the user server: sends metrics to platform ingest."""
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
'''


def run_ssh_command(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str,
    command: str,
    *,
    timeout: float = 120.0,
    max_output_chars: int = 200_000,
) -> tuple[int, str, str]:
    """Run one command on the remote host via bash -lc (like a shell).
    Returns (exit_code, stdout, stderr); output is truncated."""
    cmd = (command or "").strip()
    if not cmd:
        return 127, "", "empty command"
    client = ssh_connect(host, port, username, auth_type, secret, timeout=30.0)
    try:
        wrapped = f"bash -lc {shlex.quote(cmd)}"
        stdin, stdout, stderr = client.exec_command(wrapped, get_pty=True)
        if timeout and float(timeout) > 0:
            stdout.channel.settimeout(float(timeout))
        out_b = stdout.read()
        err_b = stderr.read()
        code = stdout.channel.recv_exit_status()
        out = out_b.decode(errors="replace")
        err = err_b.decode(errors="replace")
        if len(out) > max_output_chars:
            out = out[: max_output_chars - 60] + "\n… [stdout truncated]"
        if len(err) > max_output_chars:
            err = err[: max_output_chars - 60] + "\n… [stderr truncated]"
        return code, out, err
    finally:
        client.close()


def _load_pkey(pem_or_password: str, auth_type: str):
    if auth_type != "key":
        return None
    raw = pem_or_password.strip()
    if not raw:
        raise ValueError("empty key")
    buf = io.StringIO(raw)
    for Key in (
        paramiko.Ed25519Key,
        paramiko.RSAKey,
        paramiko.ECDSAKey,
    ):
        try:
            buf.seek(0)
            return Key.from_private_key(buf)
        except Exception:
            continue
    raise ValueError("failed to parse private key (RSA/Ed25519/ECDSA PEM)")


def ssh_connect(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str,
    timeout: float = 25.0,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw: dict = {
        "hostname": host.strip(),
        "port": port,
        "username": username.strip(),
        "timeout": timeout,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if auth_type == "key":
        kw["pkey"] = _load_pkey(secret, "key")
    else:
        kw["password"] = secret
    client.connect(**kw)
    return client


def test_ssh_connection(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str,
) -> Tuple[bool, str]:
    try:
        client = ssh_connect(host, port, username, auth_type, secret)
        try:
            stdin, stdout, stderr = client.exec_command(
                "uname -n 2>/dev/null; echo LAMBOT_SSH_OK",
                get_pty=True,
            )
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            code = stdout.channel.recv_exit_status()
            text = (out + err).strip()
            ok = code == 0 and "LAMBOT_SSH_OK" in text
            return ok, text[:2000] if ok else (text or f"exit {code}")
        finally:
            client.close()
    except Exception as exc:
        return False, str(exc)[:2000]


def _b64pipe(data: str) -> str:
    return base64.b64encode(data.encode("utf-8")).decode("ascii")


def provision_systemd_agent(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str,
    ingest_url: str,
    agent_token: str,
) -> Tuple[bool, str]:
    """Upload script + systemd unit to the remote host (passwordless sudo required)."""
    env_lines = f"LAMBOT_INGEST_URL={ingest_url}\nLAMBOT_AGENT_TOKEN={agent_token}\n"
    unit = """[Unit]
Description=Lambot platform metrics agent
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/lambot/lambot-agent.env
ExecStart=/usr/bin/python3 /opt/lambot/agent/remote_agent.py
Restart=always

[Install]
WantedBy=multi-user.target
"""

    client = ssh_connect(host, port, username, auth_type, secret, timeout=60.0)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.file("/tmp/lambot_remote_agent.py", "w") as rf:
                rf.write(REMOTE_AGENT_PY)
        finally:
            sftp.close()

        script = f"""set -e
sudo mkdir -p /opt/lambot/agent /etc/lambot
sudo mv /tmp/lambot_remote_agent.py /opt/lambot/agent/remote_agent.py
sudo chmod +x /opt/lambot/agent/remote_agent.py
echo {_b64pipe(env_lines)} | base64 -d | sudo tee /etc/lambot/lambot-agent.env >/dev/null
sudo chmod 600 /etc/lambot/lambot-agent.env
echo {_b64pipe(unit)} | base64 -d | sudo tee /etc/systemd/system/lambot-platform-agent.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now lambot-platform-agent.service
echo LAMBOT_PROVISION_OK
"""
        stdin, stdout, stderr = client.exec_command(script, get_pty=True)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        full = (out + err).strip()
        if code == 0 and "LAMBOT_PROVISION_OK" in full:
            return True, full[-500:]
        return False, full[:2000] or f"exit code {code}"
    finally:
        client.close()


def ssh_read_remote_file(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str,
    remote_path: str,
    *,
    max_bytes: int = 65536,
    timeout: float = 30.0,
) -> str | None:
    """Read a text file over SSH (cat). On error — None."""
    try:
        client = ssh_connect(host, port, username, auth_type, secret, timeout=timeout)
        try:
            safe = remote_path.replace("'", "'\"'\"'")
            cmd = f"cat '{safe}' 2>/dev/null"
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=False)
            out = stdout.read()
            code = stdout.channel.recv_exit_status()
            if code != 0 or len(out) > max_bytes:
                return None
            return out.decode("utf-8", errors="replace").strip() or None
        finally:
            client.close()
    except Exception:
        return None
