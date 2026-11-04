"""Parse structured AI Team step output (JSON + summary for following agents)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PeerQuery:
    role: str
    question: str


@dataclass
class StepEnvelope:
    schema_version: int = 1
    step_status: str = "complete"  # complete | need_user_clarification | need_peer_sync
    summary_md: str = ""
    questions_for_user: list[str] = field(default_factory=list)
    peer_queries: list[PeerQuery] = field(default_factory=list)
    pipeline_continue: bool = True
    # True — tool/infra mode is needed next (orchestrator lifts direct_answer on plans).
    live_execution: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "step_status": self.step_status,
            "summary_md": self.summary_md,
            "questions_for_user": list(self.questions_for_user),
            "peer_queries": [{"role": q.role, "question": q.question} for q in self.peer_queries],
            "pipeline_continue": self.pipeline_continue,
        }
        if self.live_execution is not None:
            out["live_execution"] = self.live_execution
        return out


def _strip_markdown_json_fence(text: str) -> str:
    t = text.strip()
    if "```" not in t:
        return t
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.I)
    if m:
        return m.group(1).strip()
    return t


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Finds the first valid JSON object in a string (after an optional preamble)."""
    raw = _strip_markdown_json_fence(text)
    start = raw.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(raw[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_step_envelope(raw_text: str) -> tuple[StepEnvelope | None, str]:
    """
    Returns (envelope, fallback_plain_text).
    If JSON is not recognized — (None, raw_text) for backward compatibility.
    """
    text = (raw_text or "").strip()
    if not text:
        return None, ""

    obj = _extract_first_json_object(text)
    if not obj:
        return None, text

    try:
        ver = int(obj.get("schema_version") or 1)
    except (TypeError, ValueError):
        ver = 1

    status = str(obj.get("step_status") or "complete").strip().lower()
    if status not in ("complete", "need_user_clarification", "need_peer_sync"):
        status = "complete"

    summary = str(obj.get("summary_md") or "").strip()
    if not summary:
        return None, text

    qfu = obj.get("questions_for_user")
    questions: list[str] = []
    if isinstance(qfu, list):
        for x in qfu:
            if isinstance(x, str) and x.strip():
                questions.append(x.strip())

    peers_raw = obj.get("peer_queries")
    peers: list[PeerQuery] = []
    if isinstance(peers_raw, list):
        for item in peers_raw[:8]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            q = str(item.get("question") or "").strip()
            if role and q:
                peers.append(PeerQuery(role=role, question=q))

    cont = obj.get("pipeline_continue")
    if isinstance(cont, bool):
        pipeline_continue = cont
    else:
        pipeline_continue = status == "complete" and not questions

    live_raw = obj.get("live_execution")
    live_execution: bool | None = None
    if isinstance(live_raw, bool):
        live_execution = live_raw
    elif isinstance(live_raw, str) and live_raw.strip().lower() in ("true", "false"):
        live_execution = live_raw.strip().lower() == "true"

    env = StepEnvelope(
        schema_version=ver,
        step_status=status,
        summary_md=summary,
        questions_for_user=questions,
        peer_queries=peers,
        pipeline_continue=pipeline_continue,
        live_execution=live_execution,
    )
    if questions or status == "need_user_clarification":
        env.pipeline_continue = False
    return env, text
