"""SSH command classification: safe / needs confirmation / forbidden."""

from __future__ import annotations

import re
from typing import Literal

Classification = Literal["blocked", "needs_confirmation", "allowed"]

MAX_COMMAND_LEN = 4000

# Hard block (do not run even with confirmation)
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

# Without confirmation: only short read-only commands (local models — relaxed mode)
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
    """Returns (classification, short note for user/log)."""
    cmd = (command or "").strip()
    if len(cmd) > MAX_COMMAND_LEN:
        return "blocked", "command too long"
    if not cmd:
        return "blocked", "empty command"

    for rx in _BLOCKED:
        if rx.search(cmd):
            return "blocked", "command rejected by security policy (dangerous pattern)"

    for rx in _SAFE:
        if rx.match(cmd):
            return "allowed", "allowed read-only command"

    return "needs_confirmation", "command may change the system or pull code from the network — user confirmation required"
