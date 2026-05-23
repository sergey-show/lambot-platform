import json
import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from shared.contracts.api import (
    LlmEmbedRequest,
    LlmEmbedResponse,
    LlmGenerateRequest,
    LlmGenerateResponse,
)
from services.agent_graph_service import langfuse_tracing
from services.llm_adapter_service.ollama_tool_coerce import coerce_chat_completion_payload

logger = logging.getLogger(__name__)


def _coerce_tool_calls_enabled() -> bool:
    v = (os.getenv("LLM_ADAPTER_COERCE_OLLAMA_TOOL_CALLS") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")

app = FastAPI(title="llm-adapter-service", version="0.4.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _fetch_settings_from_admin() -> dict[str, Any] | None:
    base = os.getenv("ADMIN_SERVICE_URL", "http://admin-service:8006").rstrip("/")
    token = os.getenv("INTERNAL_LLM_SETTINGS_TOKEN", "internal-dev-token")
    try:
        r = httpx.get(
            f"{base}/api/v1/internal/llm-settings",
            headers={"X-Internal-Token": token},
            timeout=5.0,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning("admin llm settings: status %s", r.status_code)
    except Exception as exc:
        logger.warning("admin llm settings unavailable: %s", exc)
    return None


def _resolve_chat_model(cfg: dict[str, Any] | None, request: LlmGenerateRequest) -> str:
    default_model = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    coding_model = (os.getenv("OPENAI_CODING_MODEL") or "").strip()
    if cfg:
        default_model = (cfg.get("default_model") or default_model).strip()
        cm = cfg.get("coding_model")
        if isinstance(cm, str) and cm.strip():
            coding_model = cm.strip()
    if request.model and request.model.strip():
        return request.model.strip()
    if request.purpose == "coding" and coding_model:
        return coding_model
    return default_model


def _resolve_embedding_model(cfg: dict[str, Any] | None, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    env_m = (os.getenv("OPENAI_EMBEDDING_MODEL") or "").strip()
    if env_m:
        return env_m
    if cfg:
        em = cfg.get("embedding_model")
        if isinstance(em, str) and em.strip():
            return em.strip()
    return "text-embedding-3-small"


def _resolve_base_and_key(cfg: dict[str, Any] | None) -> tuple[str, str | None]:
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("OPENAI_API_KEY")
    if cfg:
        base_url = (cfg.get("openai_base_url") or base_url).strip()
        if cfg.get("api_key"):
            api_key = cfg["api_key"]
    return base_url, api_key


def _get_langfuse_client():
    """Ленивая и опциональная инициализация Langfuse SDK для трассировки llm-adapter."""
    if not langfuse_tracing.enabled():
        return None
    try:
        from langfuse import get_client

        return get_client()
    except Exception:
        logger.exception("langfuse client init failed in llm-adapter")
        return None


_LF_CHAT_INPUT_MAX = int(os.getenv("LLM_ADAPTER_LANGFUSE_CHAT_INPUT_MAX_CHARS", "120000"))
_LF_CHAT_OUT_MAX = int(os.getenv("LLM_ADAPTER_LANGFUSE_CHAT_OUTPUT_MAX_CHARS", "80000"))


def _langfuse_safe_input(body: dict[str, Any]) -> dict[str, Any]:
    slim = {
        "model": body.get("model"),
        "stream": body.get("stream"),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_tokens"),
        "messages": body.get("messages"),
    }
    raw = json.dumps(slim, ensure_ascii=False)
    if len(raw) <= _LF_CHAT_INPUT_MAX:
        return slim
    return {
        "truncated": True,
        "chars": len(raw),
        "model": slim.get("model"),
        "stream": slim.get("stream"),
        "messages_head": raw[: _LF_CHAT_INPUT_MAX // 2],
        "messages_tail": raw[-(_LF_CHAT_INPUT_MAX // 2) :],
    }


def _parse_sse_assistant_text(sse_raw: str) -> str:
    parts: list[str] = []
    for block in sse_raw.split("\n\n"):
        for line in block.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                j = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(j, dict):
                continue
            for ch in j.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
                c = delta.get("content")
                if isinstance(c, str) and c:
                    parts.append(c)
    return "".join(parts)


def _upstream_timeout_sec(body: dict[str, Any]) -> float:
    """Короткие ответы (intake, классификаторы) — не держать upstream 15 минут."""
    default = float(os.getenv("LLM_ADAPTER_UPSTREAM_TIMEOUT_SEC", "900"))
    short = float(os.getenv("LLM_ADAPTER_SHORT_UPSTREAM_TIMEOUT_SEC", "90"))
    threshold = int(os.getenv("LLM_ADAPTER_SHORT_MAX_TOKENS_THRESHOLD", "256"))
    mt = body.get("max_tokens")
    if isinstance(mt, int) and 0 < mt <= threshold:
        return min(short, default)
    return default


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """Прокси OpenAI chat.completions для LangChain ChatOpenAI: каждый вызов пишется в Langfuse (input/output)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid json", "type": "invalid_request_error"}}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": {"message": "JSON body must be an object", "type": "invalid_request_error"}}, status_code=400)

    cfg = _fetch_settings_from_admin()
    base_url, api_key = _resolve_base_and_key(cfg)
    upstream = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    lf = _get_langfuse_client()
    lf_obs_cm: Any | None = None
    observation: Any | None = None
    if lf is not None:
        try:
            lf_obs_cm = lf.start_as_current_observation(
                as_type="generation",
                name="openai.chat_completions",
                model=str(body.get("model") or "unknown"),
                input=_langfuse_safe_input(body),
                metadata={"source": "llm-adapter", "stream": bool(body.get("stream"))},
            )
            observation = lf_obs_cm.__enter__()
        except Exception:
            logger.exception("langfuse start_as_current_observation failed in chat_completions")
            lf_obs_cm = None
            observation = None

    if not api_key:
        body = {**body, "stream": False}
        content = f"[MOCK CHAT] {json.dumps(body.get('messages'), ensure_ascii=False)[:400]}"
        out = {
            "id": "mock-chatcmpl",
            "object": "chat.completion",
            "model": body.get("model") or "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        if observation is not None:
            try:
                observation.update(output={"response": out, "provider": "mock"})
            except Exception:
                logger.exception("langfuse update failed in chat_completions (mock)")
        if lf_obs_cm is not None:
            try:
                lf_obs_cm.__exit__(None, None, None)
            except Exception:
                logger.exception("langfuse observation context exit failed in chat_completions (mock)")
        if lf is not None:
            langfuse_tracing.flush()
        return JSONResponse(out)

    request_had_tools = bool(body.get("tools"))
    stream = bool(body.get("stream"))
    # Ollama OpenAI API отдаёт tool call в content; LangChain ждёт message.tool_calls.
    if request_had_tools and stream and _coerce_tool_calls_enabled():
        body = {**body, "stream": False}
        stream = False
    upstream_timeout = _upstream_timeout_sec(body)

    async def _finish_obs(acc: bytes, *, streamed: bool) -> None:
        if observation is None:
            return
        try:
            if streamed:
                text = _parse_sse_assistant_text(acc.decode("utf-8", errors="replace"))
                out = {"assistant_text": text[:_LF_CHAT_OUT_MAX], "sse_bytes": len(acc)}
                if len(text) > _LF_CHAT_OUT_MAX:
                    out["assistant_text_truncated"] = True
            else:
                try:
                    parsed = json.loads(acc.decode("utf-8"))
                except Exception:
                    parsed = {"raw": acc.decode("utf-8", errors="replace")[:_LF_CHAT_OUT_MAX]}
                raw_out = json.dumps(parsed, ensure_ascii=False)
                if len(raw_out) > _LF_CHAT_OUT_MAX:
                    out = {"truncated": True, "head": raw_out[: _LF_CHAT_OUT_MAX // 2], "tail": raw_out[-(_LF_CHAT_OUT_MAX // 2) :]}
                else:
                    out = parsed
            observation.update(output={"response": out, "provider": "openai-compatible", "base_url": base_url})
        except Exception:
            logger.exception("langfuse update failed in chat_completions")

    if not stream:
        acc = b""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(upstream_timeout)) as client:
                r = await client.post(upstream, headers=headers, json=body)
                acc = r.content
                if (
                    r.status_code == 200
                    and request_had_tools
                    and _coerce_tool_calls_enabled()
                    and acc
                ):
                    try:
                        parsed = json.loads(acc)
                        if isinstance(parsed, dict):
                            coerced = coerce_chat_completion_payload(
                                parsed, request_had_tools=True
                            )
                            acc = json.dumps(coerced, ensure_ascii=False).encode("utf-8")
                    except Exception:
                        logger.exception("ollama tool_calls coerce failed")
                if r.status_code >= 400:
                    if observation is not None:
                        try:
                            observation.update(
                                output={
                                    "error": True,
                                    "status_code": r.status_code,
                                    "body": acc.decode("utf-8", errors="replace")[:8000],
                                }
                            )
                        except Exception:
                            logger.exception("langfuse update failed on upstream error")
                    if lf_obs_cm is not None:
                        try:
                            lf_obs_cm.__exit__(None, None, None)
                        except Exception:
                            pass
                    if lf is not None:
                        langfuse_tracing.flush()
                    return Response(content=acc, media_type="application/json", status_code=r.status_code)
                await _finish_obs(acc, streamed=False)
        except Exception as exc:
            if observation is not None:
                try:
                    observation.update(output={"error": str(exc)})
                except Exception:
                    pass
            if lf_obs_cm is not None:
                try:
                    lf_obs_cm.__exit__(None, None, None)
                except Exception:
                    pass
            if lf is not None:
                langfuse_tracing.flush()
            raise
        if lf_obs_cm is not None:
            try:
                lf_obs_cm.__exit__(None, None, None)
            except Exception:
                logger.exception("langfuse observation context exit failed in chat_completions")
        if lf is not None:
            langfuse_tracing.flush()
        return Response(content=acc, media_type="application/json", status_code=200)

    async def stream_gen():
        acc = bytearray()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(upstream_timeout)) as client:
                async with client.stream("POST", upstream, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        err = (await resp.aread()).decode("utf-8", errors="replace")
                        if observation is not None:
                            try:
                                observation.update(
                                    output={"error": True, "status_code": resp.status_code, "body": err[:8000]}
                                )
                            except Exception:
                                pass
                        yield f'data: {json.dumps({"error": {"message": err[:500], "type": "upstream_error"}})}\n\n'.encode()
                        return
                    async for chunk in resp.aiter_bytes():
                        acc.extend(chunk)
                        yield chunk
            await _finish_obs(bytes(acc), streamed=True)
        finally:
            if lf_obs_cm is not None:
                try:
                    lf_obs_cm.__exit__(None, None, None)
                except Exception:
                    logger.exception("langfuse observation context exit failed in chat_completions stream")
            if lf is not None:
                langfuse_tracing.flush()

    return StreamingResponse(stream_gen(), media_type="text/event-stream")


@app.post("/api/v1/llm/generate", response_model=LlmGenerateResponse)
def generate(request: LlmGenerateRequest) -> LlmGenerateResponse:
    cfg = _fetch_settings_from_admin()
    base_url, api_key = _resolve_base_and_key(cfg)
    model = _resolve_chat_model(cfg, request)
    lf = _get_langfuse_client()
    # start_as_current_observation возвращает context manager; .update() только у результата __enter__().
    lf_obs_cm: Any | None = None
    observation: Any | None = None
    if lf is not None:
        try:
            lf_obs_cm = lf.start_as_current_observation(
                as_type="generation",
                name="llm-adapter.generate",
                model=model,
                input={"prompt": request.prompt, "temperature": request.temperature, "base_url": base_url},
            )
            observation = lf_obs_cm.__enter__()
        except Exception:
            logger.exception("langfuse start_as_current_observation failed in generate")
            lf_obs_cm = None
            observation = None

    try:
        if not api_key:
            output = f"[MOCK LLM OUTPUT] {request.prompt[:300]}"
            if observation is not None:
                try:
                    observation.update(
                        output={"output": output, "provider": "mock", "model": model, "base_url": base_url}
                    )
                except Exception:
                    logger.exception("langfuse update failed in generate (mock)")
            return LlmGenerateResponse(output=output, provider="mock")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": request.temperature,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()

        content = body["choices"][0]["message"]["content"]
        if observation is not None:
            try:
                observation.update(
                    output={"output": content, "provider": "openai-compatible", "model": model, "base_url": base_url}
                )
            except Exception:
                logger.exception("langfuse update failed in generate (real)")
        return LlmGenerateResponse(output=content, provider="openai-compatible")
    finally:
        if lf_obs_cm is not None:
            try:
                lf_obs_cm.__exit__(None, None, None)
            except Exception:
                logger.exception("langfuse observation context exit failed in generate")
        if lf is not None:
            langfuse_tracing.flush()


@app.post("/api/v1/llm/embed", response_model=LlmEmbedResponse)
def embed(request: LlmEmbedRequest) -> LlmEmbedResponse:
    cfg = _fetch_settings_from_admin()
    base_url, api_key = _resolve_base_and_key(cfg)
    model = _resolve_embedding_model(cfg, request.model)
    lf = _get_langfuse_client()
    lf_obs_cm: Any | None = None
    observation: Any | None = None
    if lf is not None:
        try:
            lf_obs_cm = lf.start_as_current_observation(
                as_type="embedding",
                name="llm-adapter.embed",
                model=model,
                input={"input": request.input, "base_url": base_url},
            )
            observation = lf_obs_cm.__enter__()
        except Exception:
            logger.exception("langfuse start_as_current_observation failed in embed")
            lf_obs_cm = None
            observation = None

    try:
        if not api_key:
            fake = [[0.0, 0.0, 0.0, 0.0]]
            if isinstance(request.input, list) and len(request.input) > 1:
                fake = [list(fake[0]) for _ in request.input]
            if observation is not None:
                try:
                    observation.update(
                        output={
                            "embeddings_count": len(fake),
                            "model": "mock-embedding",
                            "provider": "mock",
                            "base_url": base_url,
                        }
                    )
                except Exception:
                    logger.exception("langfuse update failed in embed (mock)")
            return LlmEmbedResponse(embeddings=fake, model="mock-embedding", provider="mock")

        payload: dict[str, Any] = {"model": model, "input": request.input}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{base_url.rstrip('/')}/embeddings", headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()

        data = body.get("data") or []
        ordered = sorted(data, key=lambda x: x.get("index", 0))
        embeddings = [item["embedding"] for item in ordered]
        used = str(body.get("model") or model)
        if observation is not None:
            try:
                observation.update(
                    output={
                        "embeddings_count": len(embeddings),
                        "model": used,
                        "provider": "openai-compatible",
                        "base_url": base_url,
                    }
                )
            except Exception:
                logger.exception("langfuse update failed in embed (real)")
        return LlmEmbedResponse(embeddings=embeddings, model=used, provider="openai-compatible")
    finally:
        if lf_obs_cm is not None:
            try:
                lf_obs_cm.__exit__(None, None, None)
            except Exception:
                logger.exception("langfuse observation context exit failed in embed")
        if lf is not None:
            langfuse_tracing.flush()
