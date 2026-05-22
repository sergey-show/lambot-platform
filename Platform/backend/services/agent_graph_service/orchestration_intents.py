"""Эвристики намерения пользователя (маршрутизация и проверка ответа LLM)."""

from __future__ import annotations

import re

_BUILD_SITE = re.compile(
    r"(сделай|создай|построй|разработай|собери|нужен|нужно|хочу)\s+.{0,40}?(сайт|веб|dashboard|дашборд|лендинг|страниц)",
    re.I,
)
_FAKE_TOOL_JSON = re.compile(
    r'^\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"(arguments|args)"\s*:',
    re.I | re.DOTALL,
)


def site_build_intent(message: str) -> bool:
    """Запрос на создание/сборку сайта или дашборда — не «консультация одной ролью»."""
    t = (message or "").strip()
    if not t:
        return False
    if _BUILD_SITE.search(t):
        return True
    if re.search(r"(сайт|dashboard|дашборд).{0,60}(сделай|создай|построй)", t, re.I):
        return True
    return False


def looks_like_fake_tool_call_text(text: str) -> bool:
    """Модель описала tool call JSON в тексте, без native function calling."""
    t = (text or "").strip()
    if not t or not t.startswith("{"):
        return False
    return bool(_FAKE_TOOL_JSON.match(t))
