"""Классификация SSH-команд: безопасные / нужно подтверждение / запрещено."""

from __future__ import annotations

import re
from typing import Literal

Classification = Literal["blocked", "needs_confirmation", "allowed"]

MAX_COMMAND_LEN = 4000

# Явная блокировка (не выполнять даже с подтверждением)
_BLOCKED = [
    re.compile(r"^\s*$", re.I),
    re.compile(r"rm\s+(-[rf]*\s+)*[/]\s", re.I),
    re.compile(r"rm\s+(-[rf]*\s+)*--no-preserve-root", re.I),
    re.compile(r">\s*/dev/(sd|hd|nvme|vd)", re.I),
    re.compile(r"dd\s+if=", re.I),
    re.compile(r"mkfs\.?", re.I),
    re.compile(r":\(\)\s*\{", re.I),  # fork bomb
    re.compile(r"chmod\s+-R\s+777\s+[/]", re.I),
    re.compile(r"curl\s+[^|]*\|\s*(ba)?sh", re.I),
    re.compile(r"wget\s+[^|]*\|\s*(ba)?sh", re.I),
]

# Без подтверждения: только «читающие» короткие команды (локальные модели — смягчённый режим)
_SAFE = [
    re.compile(r"^pwd\s*$", re.I),
    re.compile(r"^whoami\s*$", re.I),
    re.compile(r"^hostname(\s+-f)?\s*$", re.I),
    re.compile(r"^uptime\s*$", re.I),
    re.compile(r"^date(\s|$)", re.I),
    re.compile(r"^uname(\s|$)", re.I),
    re.compile(r"^ls(\s|$)", re.I),
    re.compile(r"^df(\s|$)", re.I),
    re.compile(r"^free(\s|$)", re.I),
    re.compile(r"^id\s*$", re.I),
    re.compile(r"^echo\s", re.I),
    re.compile(r"^which\s+\S+\s*$", re.I),
    re.compile(r"^env\s*$", re.I),
    re.compile(r"^ps(\s|$)", re.I),
    re.compile(r"^head\s+", re.I),
    re.compile(r"^tail\s+-n\s+\d+\s+\S", re.I),
    re.compile(r"^cat\s+[./a-zA-Z0-9_\-]+\s*$", re.I),
    re.compile(r"^grep\s+", re.I),
]


def classify_ssh_command(command: str) -> tuple[Classification, str]:
    """
    Возвращает (классификация, короткое пояснение для пользователя/лога).
    """
    cmd = (command or "").strip()
    if len(cmd) > MAX_COMMAND_LEN:
        return "blocked", "команда слишком длинная"
    if not cmd:
        return "blocked", "пустая команда"

    for rx in _BLOCKED:
        if rx.search(cmd):
            return "blocked", "команда отклонена политикой безопасности (опасный шаблон)"

    for rx in _SAFE:
        if rx.match(cmd):
            return "allowed", "разрешённая read-only команда"

    return "needs_confirmation", "команда может изменить систему или тянуть код из сети — нужно подтверждение пользователя"
