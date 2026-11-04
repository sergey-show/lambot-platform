"""Validate kubeconfig and safe kubectl operations (no arbitrary shell)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

MAX_KUBECTL_OUTPUT_CHARS = 200_000

_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$")

DESCRIBE_KINDS = frozenset(
    {
        "pod",
        "pods",
        "deployment",
        "deployments",
        "service",
        "services",
        "ingress",
        "ingresses",
        "node",
        "nodes",
        "namespace",
        "namespaces",
        "configmap",
        "configmaps",
        "secret",
        "secrets",
        "replicaset",
        "replicasets",
        "statefulset",
        "statefulsets",
        "daemonset",
        "daemonsets",
        "job",
        "jobs",
        "cronjob",
        "cronjobs",
        "persistentvolume",
        "persistentvolumes",
        "persistentvolumeclaim",
        "persistentvolumeclaims",
    }
)

ALLOWED_OPERATIONS = frozenset(
    {
        "cluster_info",
        "version",
        "nodes",
        "namespaces",
        "pods",
        "deployments",
        "services",
        "ingresses",
        "statefulsets",
        "daemonsets",
        "events",
        "describe",
        "logs",
        "top_nodes",
        "top_pods",
        "rollout_status",
        "scale",
        "rollout_restart",
    }
)


def kubectl_available() -> bool:
    return shutil.which("kubectl") is not None


def _sanitize_name(s: str | None, *, max_len: int = 253) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    if len(t) > max_len:
        raise ValueError("name too long")
    if not _NAME_RE.match(t):
        raise ValueError("invalid name")
    return t


def _truncate(s: str) -> str:
    if len(s) <= MAX_KUBECTL_OUTPUT_CHARS:
        return s
    return s[: MAX_KUBECTL_OUTPUT_CHARS - 80] + "\n… [output truncated]"


def _run_kubectl(
    kubeconfig_text: str,
    context: str | None,
    args: list[str],
    *,
    timeout: int = 120,
) -> tuple[int, str, str]:
    text = (kubeconfig_text or "").strip()
    if not text:
        return 127, "", "empty kubeconfig"
    if not kubectl_available():
        return 127, "", "kubectl not found in PATH"
    with TemporaryDirectory(prefix="lambot-kube-") as tmp:
        kc = Path(tmp) / "config"
        kc.write_text(text, encoding="utf-8")
        env = os.environ.copy()
        env["KUBECONFIG"] = str(kc)
        cmd = ["kubectl"]
        if context and str(context).strip():
            cmd += ["--context", str(context).strip()]
        cmd.extend(args)
        try:
            r = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return -1, "", f"kubectl timeout ({timeout}s)"
        except OSError as e:
            return 127, "", str(e)
        out = _truncate((r.stdout or "").strip())
        err = _truncate((r.stderr or "").strip())
        return r.returncode, out, err


def test_kubeconfig(kubeconfig_text: str, context: str | None) -> tuple[bool, str]:
    rc, out, err = _run_kubectl(kubeconfig_text, context, ["get", "nodes", "-o", "name"], timeout=45)
    if rc != 0:
        return False, (err or out or f"exit {rc}")[:2000]
    return True, f"OK, nodes: {out[:800]}"


def _ns_flags(
    namespace: str | None,
    all_namespaces: bool,
) -> list[str]:
    if all_namespaces:
        return ["-A"]
    if namespace:
        return ["-n", namespace]
    return []


def execute_k8s_operation(
    kubeconfig_text: str,
    context: str | None,
    operation: str,
    *,
    namespace: str | None = None,
    name: str | None = None,
    resource: str | None = None,
    container: str | None = None,
    tail_lines: int = 200,
    all_namespaces: bool = False,
    wide: bool = False,
    replicas: int | None = None,
) -> dict[str, Any]:
    op = (operation or "").strip().lower()
    if op not in ALLOWED_OPERATIONS:
        return {"ok": False, "error": f"unknown operation: {operation}", "returncode": -1, "stdout": "", "stderr": ""}

    try:
        ns = _sanitize_name(namespace, max_len=253)
        nm = _sanitize_name(name, max_len=253)
        res = (resource or "").strip().lower() if resource else None
        ct = _sanitize_name(container, max_len=253)
    except ValueError as e:
        return {"ok": False, "error": str(e), "returncode": -1, "stdout": "", "stderr": ""}

    tl = max(1, min(int(tail_lines), 5000))
    args: list[str] = []
    timeout = 120

    if op == "cluster_info":
        args = ["cluster-info"]
    elif op == "version":
        args = ["version", "-o", "yaml"]
    elif op == "nodes":
        args = ["get", "nodes"]
        if wide:
            args += ["-o", "wide"]
    elif op == "namespaces":
        args = ["get", "namespaces"]
        if wide:
            args += ["-o", "wide"]
    elif op == "pods":
        args = ["get", "pods"] + _ns_flags(ns, all_namespaces)
        if wide:
            args += ["-o", "wide"]
    elif op == "deployments":
        args = ["get", "deployments"] + _ns_flags(ns, all_namespaces)
        if wide:
            args += ["-o", "wide"]
    elif op == "services":
        args = ["get", "svc"] + _ns_flags(ns, all_namespaces)
        if wide:
            args += ["-o", "wide"]
    elif op == "ingresses":
        args = ["get", "ingress"] + _ns_flags(ns, all_namespaces)
        if wide:
            args += ["-o", "wide"]
    elif op == "statefulsets":
        args = ["get", "statefulset"] + _ns_flags(ns, all_namespaces)
        if wide:
            args += ["-o", "wide"]
    elif op == "daemonsets":
        args = ["get", "daemonset"] + _ns_flags(ns, all_namespaces)
        if wide:
            args += ["-o", "wide"]
    elif op == "events":
        args = ["get", "events", "--sort-by=.lastTimestamp"]
        if all_namespaces:
            args += ["-A"]
        elif ns:
            args += ["-n", ns]
        else:
            args += ["-A"]
    elif op == "describe":
        if not res or res not in DESCRIBE_KINDS:
            return {
                "ok": False,
                "error": f"describe: resource must be a known kind, got {resource!r}",
                "returncode": -1,
                "stdout": "",
                "stderr": "",
            }
        if not nm:
            return {"ok": False, "error": "describe: name required", "returncode": -1, "stdout": "", "stderr": ""}
        args = ["describe", res, nm]
        cluster_scoped = res in ("nodes", "node", "namespaces", "namespace", "persistentvolumes", "persistentvolume")
        if not cluster_scoped and ns:
            args += ["-n", ns]
        elif not cluster_scoped and not ns:
            return {"ok": False, "error": "describe: namespace required for this resource", "returncode": -1, "stdout": "", "stderr": ""}
    elif op == "logs":
        if not nm:
            return {"ok": False, "error": "logs: pod name required", "returncode": -1, "stdout": "", "stderr": ""}
        if not ns:
            return {"ok": False, "error": "logs: namespace required", "returncode": -1, "stdout": "", "stderr": ""}
        args = ["logs", nm, "-n", ns, f"--tail={tl}"]
        if ct:
            args += ["-c", ct]
        timeout = 180
    elif op == "top_nodes":
        args = ["top", "nodes"]
    elif op == "top_pods":
        args = ["top", "pods"] + _ns_flags(ns, all_namespaces)
    elif op == "rollout_status":
        if not nm or not ns:
            return {
                "ok": False,
                "error": "rollout_status: namespace and name (deployment) required",
                "returncode": -1,
                "stdout": "",
                "stderr": "",
            }
        args = ["rollout", "status", f"deployment/{nm}", "-n", ns]
    elif op == "scale":
        if not nm or not ns or replicas is None:
            return {
                "ok": False,
                "error": "scale: namespace, name (deployment), replicas required",
                "returncode": -1,
                "stdout": "",
                "stderr": "",
            }
        try:
            rep = int(replicas)
        except (TypeError, ValueError):
            return {"ok": False, "error": "scale: invalid replicas", "returncode": -1, "stdout": "", "stderr": ""}
        if rep < 0 or rep > 5000:
            return {"ok": False, "error": "scale: replicas out of range 0..5000", "returncode": -1, "stdout": "", "stderr": ""}
        args = ["scale", f"deployment/{nm}", f"--replicas={rep}", "-n", ns]
    elif op == "rollout_restart":
        if not nm or not ns:
            return {
                "ok": False,
                "error": "rollout_restart: namespace and name (deployment) required",
                "returncode": -1,
                "stdout": "",
                "stderr": "",
            }
        args = ["rollout", "restart", f"deployment/{nm}", "-n", ns]
    else:
        return {"ok": False, "error": "unhandled operation", "returncode": -1, "stdout": "", "stderr": ""}

    rc, out, err = _run_kubectl(kubeconfig_text, context, args, timeout=timeout)
    return {
        "ok": rc == 0,
        "returncode": rc,
        "stdout": out,
        "stderr": err,
        "operation": op,
    }
