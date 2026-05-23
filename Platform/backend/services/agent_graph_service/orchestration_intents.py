"""Эвристики намерения пользователя (маршрутизация и проверка ответа LLM)."""

from __future__ import annotations

import re

_BUILD_SITE = re.compile(
    r"(сделай|создай|построй|разработай|собери|нужен|нужно|хочу)\s+.{0,40}?(сайт|веб|dashboard|дашборд|лендинг|страниц)",
    re.I,
)
_FAKE_TOOL_JSON = re.compile(
    r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"(arguments|args)"\s*:',
    re.I | re.DOTALL,
)
_OPS_FACTS = re.compile(
    r"(uptime|load\s*average|ресурс|памят|ram\b|cpu\b|диск|disk\b|df\b|free\s+-m|"
    r"хватает\s+ли|сколько\s+места|версия\s+linux|статус\s+сервис|нагрузк|"
    r"свободн\w*\s+памят|использован\w*\s+памят|место\s+на\s+диск)",
    re.I,
)


_FORMAL_SPEC = re.compile(
    r"(тз\b|техническ\w*\s+задани|спецификац\w*\s+требован|формальн\w*\s+требован|"
    r"as[- ]?is|to[- ]?be|глоссарий|traceability|трассировк\w*\s+требован|"
    r"матриц\w*\s+требован|user\s*stor|use\s*case|bpmn|диаграмм\w*\s+процесс|"
    r"интеграц\w+\s+с\s+(crm|erp|1c|битрикс)|согласован\w*\s+тз)",
    re.I,
)


def formal_spec_intent(message: str) -> bool:
    """Нужен системный аналитик: формальные артефакты, эпики/задачи в трекере."""
    t = (message or "").strip()
    if not t:
        return False
    if _FORMAL_SPEC.search(t):
        return True
    if re.search(r"(эпик|бэклог|карточк\w*\s+в\s+трекер|задач\w+\s+для\s+агент)", t, re.I):
        return True
    return False


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


def _strip_markdown_json_fence(text: str) -> str:
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def operational_facts_intent(message: str) -> bool:
    """Запрос фактов с сервера/инфры — нужны реальные tool calls, не текстовый JSON."""
    t = (message or "").strip()
    if not t:
        return False
    return bool(_OPS_FACTS.search(t))


def looks_like_fake_tool_call_text(text: str) -> bool:
    """Модель описала tool call JSON в тексте, без native function calling."""
    t = _strip_markdown_json_fence(text or "")
    if not t:
        return False
    return bool(_FAKE_TOOL_JSON.search(t))
