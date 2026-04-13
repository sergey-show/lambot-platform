"""Запуск ansible-playbook на стороне adapter с учётными данными из ServerTarget."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Callable, Generator

import yaml

from services.adapter_service.crypto import decrypt_secret

if TYPE_CHECKING:
    from services.adapter_service.models import ServerTarget


def build_k8s_three_node_inventory(
    master: ServerTarget,
    worker1: ServerTarget,
    worker2: ServerTarget,
    *,
    key_path: Path | None,
    password_plain: str | None,
    pod_network_cidr: str,
) -> str:
    """Inventory YAML: группы masters/workers + pod_network_cidr в all.vars."""
    user = master.ssh_user.strip()
    common: dict = {
        "ansible_user": user,
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    }
    if master.auth_type == "key":
        if not key_path:
            raise ValueError("key_path required for key auth")
        common["ansible_ssh_private_key_file"] = str(key_path)
    else:
        common["ansible_password"] = (password_plain or "").strip()

    inv = {
        "all": {
            "vars": {"pod_network_cidr": pod_network_cidr.strip()},
            "children": {
                "masters": {
                    "hosts": {
                        "k8s-master": {**common, "ansible_host": master.host.strip()},
                    }
                },
                "workers": {
                    "hosts": {
                        "k8s-worker1": {**common, "ansible_host": worker1.host.strip()},
                        "k8s-worker2": {**common, "ansible_host": worker2.host.strip()},
                    },
                },
            },
        }
    }
    return yaml.safe_dump(inv, allow_unicode=True, default_flow_style=False, sort_keys=False)


def iter_ansible_playbook_multi(
    playbook_yaml: str,
    inventory_yaml: str,
    *,
    timeout_sec: int | None = None,
) -> Generator[str, None, tuple[int, str, str]]:
    """ansible-playbook с произвольным inventory (например, несколько хостов)."""
    if not ansible_playbook_available():
        yield from ()
        return (127, "", "ansible-playbook not found in PATH")

    timeout = timeout_sec if timeout_sec is not None else int(os.getenv("ADAPTER_ANSIBLE_TIMEOUT_SEC", "3600"))

    with TemporaryDirectory(prefix="lambot-ansible-") as tmp:
        tmp_path = Path(tmp)
        pb = tmp_path / "playbook.yml"
        inv = tmp_path / "inventory.yml"
        pb.write_text(playbook_yaml, encoding="utf-8")
        inv.write_text(inventory_yaml, encoding="utf-8")

        env = os.environ.copy()
        env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
        env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"
        env.setdefault("ANSIBLE_CONFIG", "/app/ansible.cfg")
        env.setdefault("ANSIBLE_ROLES_PATH", "/app/ansible/roles")
        env.setdefault("ANSIBLE_COLLECTIONS_PATHS", "/app/ansible/collections")

        try:
            proc = subprocess.Popen(
                ["ansible-playbook", "-i", str(inv), str(pb), "-v"],
                cwd=str(tmp_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            yield from ()
            return (127, "", f"ansible-playbook failed to start: {e}")

        out_parts: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                out_parts.append(line)
                yield line.rstrip("\n\r")
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            return (-1, "".join(out_parts), f"ansible-playbook exceeded timeout ({timeout}s); increase ADAPTER_ANSIBLE_TIMEOUT_SEC")
        return (rc, "".join(out_parts), "")


def ansible_playbook_available() -> bool:
    return shutil.which("ansible-playbook") is not None


def _inventory_yaml(host: str, port: int, user: str, auth_type: str, secret: str, key_file: Path) -> str:
    h: dict = {
        "ansible_host": host.strip(),
        "ansible_port": port,
        "ansible_user": user.strip(),
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    }
    if auth_type == "key":
        h["ansible_ssh_private_key_file"] = str(key_file)
    else:
        h["ansible_password"] = secret
    inv = {
        "all": {
            "children": {
                "target": {
                    "hosts": {
                        "lambot_target": h,
                    }
                }
            }
        }
    }
    return yaml.safe_dump(inv, allow_unicode=True, default_flow_style=False, sort_keys=False)


def iter_ansible_playbook(
    playbook_yaml: str,
    row: ServerTarget,
    *,
    timeout_sec: int | None = None,
) -> Generator[str, None, tuple[int, str, str]]:
    """
    Потоковый запуск ansible-playbook: отдаёт строки объединённого вывода (stdout+stderr).
    По завершении генератор возвращает (returncode, stdout, stderr) через StopIteration.value.
    """
    if not ansible_playbook_available():
        yield from ()
        return (127, "", "ansible-playbook not found in PATH")

    secret = decrypt_secret(row.credential_encrypted)
    timeout = timeout_sec if timeout_sec is not None else int(os.getenv("ADAPTER_ANSIBLE_TIMEOUT_SEC", "3600"))

    with TemporaryDirectory(prefix="lambot-ansible-") as tmp:
        tmp_path = Path(tmp)
        pb = tmp_path / "playbook.yml"
        inv = tmp_path / "inventory.yml"
        pb.write_text(playbook_yaml, encoding="utf-8")

        key_path = tmp_path / "ssh_key.pem"
        if row.auth_type == "key":
            key_path.write_text(secret.strip() + ("\n" if not secret.endswith("\n") else ""), encoding="utf-8")
            key_path.chmod(0o600)

        inv.write_text(
            _inventory_yaml(row.host, row.ssh_port, row.ssh_user, row.auth_type, secret, key_path),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
        env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"
        # Playbook в /tmp: явно задаём config и пути к ролям/коллекциям из образа adapter.
        env.setdefault("ANSIBLE_CONFIG", "/app/ansible.cfg")
        env.setdefault("ANSIBLE_ROLES_PATH", "/app/ansible/roles")
        env.setdefault("ANSIBLE_COLLECTIONS_PATHS", "/app/ansible/collections")

        try:
            proc = subprocess.Popen(
                ["ansible-playbook", "-i", str(inv), str(pb), "-v"],
                cwd=str(tmp_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            yield from ()
            return (127, "", f"ansible-playbook failed to start: {e}")

        out_parts: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                out_parts.append(line)
                yield line.rstrip("\n\r")
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            return (-1, "".join(out_parts), f"ansible-playbook exceeded timeout ({timeout}s); increase ADAPTER_ANSIBLE_TIMEOUT_SEC")
        return (rc, "".join(out_parts), "")


def run_playbook_for_target(
    playbook_yaml: str,
    row: ServerTarget,
    *,
    timeout_sec: int | None = None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str, str]:
    """
    Выполняет playbook (YAML-текст) по SSH из сохранённых credential_encrypted.
    Возвращает (returncode, stdout, stderr). stderr пуст, вывод объединён в stdout.
    on_line — опционально вызывается для каждой строки вывода (без буферизации всего прогона).
    """
    if not ansible_playbook_available():
        return 127, "", "ansible-playbook not found in PATH"

    gen = iter_ansible_playbook(playbook_yaml, row, timeout_sec=timeout_sec)
    try:
        while True:
            line = next(gen)
            if on_line:
                on_line(line)
    except StopIteration as ex:
        val = ex.value
        if val is None:
            return 127, "", "ansible-playbook failed to start"
        rc, out, err = val
        return rc, out, err
