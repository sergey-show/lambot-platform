"""Non-routing helpers: tool-call detection, approval phrases, output quality checks."""

from __future__ import annotations

import json
import re

_BUILD_SITE = re.compile(
    r"(build|create|make|develop|need|want)\s+.{0,40}?(website|web\s*app|site|dashboard|landing\s*page)",
    re.I,
)
_FAKE_TOOL_JSON = re.compile(
    r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"(arguments|args)"\s*:',
    re.I | re.DOTALL,
)
_OPS_FACTS = re.compile(
    r"(uptime|load\s*average|\bram\b|\bcpu\b|disk\b|\bdf\b|free\s+-m|"
    r"how\s+much\s+(disk|space|memory)|disk\s+space|free\s+space|"
    r"linux\s+version|service\s+status|memory\s+usage)",
    re.I,
)

_FORMAL_SPEC = re.compile(
    r"(formal\s+spec|requirements?\s+spec|specification|glossary|traceability|"
    r"as[- ]?is|to[- ]?be|user\s*stor(?:y|ies)|use\s*case|bpmn|"
    r"requirements?\s+matrix|approved\s+spec)",
    re.I,
)


def formal_spec_intent(message: str) -> bool:
    """True when the user asks for formal SA artifacts / tracker backlog."""
    t = (message or "").strip()
    if not t:
        return False
    if _FORMAL_SPEC.search(t):
        return True
    if re.search(r"(epics?|backlog|tracker\s+cards?|tasks?\s+for\s+agents?)", t, re.I):
        return True
    return False


def site_build_intent(message: str) -> bool:
    """Request to build a site or dashboard — not a single-role consultation."""
    t = (message or "").strip()
    if not t:
        return False
    if _BUILD_SITE.search(t):
        return True
    if re.search(r"(website|dashboard|landing).{0,60}(build|create|make)", t, re.I):
        return True
    return False


def _strip_markdown_json_fence(text: str) -> str:
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def user_requests_tool_execution_approval(message: str) -> bool:
    """User explicitly asks to execute actions, not only plan."""
    t = (message or "").lower()
    if not t:
        return False
    markers = (
        "execute",
        "run it",
        "do it",
        "proceed",
        "go ahead",
        "continue",
        "confirm",
        "approved",
        "yes,",
        "yes ",
        "yep",
        "ok,",
        "okay,",
        "don't ask",
        "do not ask",
        "just do",
        "apply changes",
        "deploy now",
    )
    return any(m in t for m in markers)


def operational_facts_intent(message: str) -> bool:
    """Request for server/infra facts — need real tool calls, not textual JSON."""
    t = (message or "").strip()
    if not t:
        return False
    return bool(_OPS_FACTS.search(t))


def extract_tool_names_from_assistant_content(text: str) -> list[str]:
    """Tool names from JSON in content (Ollama OpenAI API without message.tool_calls)."""
    raw = _strip_markdown_json_fence(text or "")
    names: list[str] = []
    if not raw:
        return names
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            arr = None
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    nm = item.get("name")
                    if isinstance(nm, str) and nm.strip():
                        names.append(nm.strip())
            return names
    if _FAKE_TOOL_JSON.search(raw):
        try:
            obj = json.loads(raw[raw.find("{") :])
        except json.JSONDecodeError:
            return names
        if isinstance(obj, dict):
            nm = obj.get("name")
            if isinstance(nm, str) and nm.strip():
                names.append(nm.strip())
    return names


def looks_like_fake_tool_call_text(text: str) -> bool:
    """Model described a tool call JSON in text, without native function calling."""
    t = _strip_markdown_json_fence(text or "")
    if not t:
        return False
    return bool(_FAKE_TOOL_JSON.search(t))


_GARBAGE_ARCHITECT = re.compile(
    r"initial analysis\s*&\s*planning|text fragment|await user feedback|"
    r"provided text, which appears to be a fragment|redacted_thinking|"
    r"end of response\"\s*,\s*\"questions_for_user\"|step 1:\s*initial analysis",
    re.I,
)


def looks_like_invalid_architect_output(summary_md: str, user_message: str) -> bool:
    """
    Architect reply is not about the user task (chat-template / meta-English leak).
    """
    s = (summary_md or "").strip()
    if len(s) < 120:
        return True
    if _GARBAGE_ARCHITECT.search(s):
        return True
    if site_build_intent(user_message):
        domain = re.search(
            r"(promo|order|catalog|stock|website|site|frontend|backend|api|postgres|docker|gitlab|fastapi|react)",
            s,
            re.I,
        )
        if not domain and re.search(r"step\s+\d+:\s*", s, re.I):
            return True
    return False
