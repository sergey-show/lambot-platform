"""
Песочница для выполнения ограниченного Python-кода (агенты, проверки, демо).
Не замена полноценной изоляции: для production используйте отдельные контейнеры/K8s Job.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

SANDBOX_TOKEN = os.getenv("SANDBOX_SERVICE_TOKEN", "internal-sandbox-dev-token")
MAX_CODE_LEN = int(os.getenv("SANDBOX_MAX_CODE_BYTES", "96000"))
DEFAULT_TIMEOUT = float(os.getenv("SANDBOX_DEFAULT_TIMEOUT_SEC", "15"))
MAX_TIMEOUT = float(os.getenv("SANDBOX_MAX_TIMEOUT_SEC", "60"))


app = FastAPI(title="sandbox-service", version="0.1.0")


def _require_token(x_internal_token: str | None = Header(default=None, alias="X-Sandbox-Token")) -> None:
    if (x_internal_token or "").strip() != SANDBOX_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid sandbox token")


class SandboxPythonRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=MAX_CODE_LEN)
    timeout_sec: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=MAX_TIMEOUT)


class SandboxPythonResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _execute_python(req: SandboxPythonRequest) -> SandboxPythonResponse:
    if len(req.code.encode("utf-8")) > MAX_CODE_LEN:
        raise HTTPException(status_code=400, detail="code too large")

    exe = sys.executable or "python3"
    with tempfile.TemporaryDirectory(prefix="lambot-sbx-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "TMPDIR": tmp,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            proc = subprocess.run(
                [exe, "-c", req.code],
                capture_output=True,
                text=True,
                timeout=req.timeout_sec,
                cwd=tmp,
                env=env,
            )
            return SandboxPythonResponse(
                exit_code=proc.returncode,
                stdout=(proc.stdout or "")[:512_000],
                stderr=(proc.stderr or "")[:512_000],
                timed_out=False,
            )
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = (e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return SandboxPythonResponse(
                exit_code=-1,
                stdout=out[:512_000],
                stderr=(err + "\n[sandbox: timeout]")[:512_000],
                timed_out=True,
            )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/sandbox/python", response_model=SandboxPythonResponse)
def run_python(
    req: SandboxPythonRequest,
    _: None = Depends(_require_token),
) -> SandboxPythonResponse:
    """
    Выполняет `python -c <code>` во временном каталоге с урезанным окружением.
    Сеть и ФС не изолированы на уровне ядра — только для доверенного контура платформы.
    """
    return _execute_python(req)
