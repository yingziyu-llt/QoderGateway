import copy
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from . import encoding
from .auth import SessionContext, bearer_headers
from .catalog import cn_model_key, global_model_key
from .env import httpx_client_kwargs
from .regions import get_region


QODER_CHAT_URL = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
# 新版协议（Qoder CLI 现行）：OpenAI 兼容端点，纯 Bearer，无 COSY 签名，响应为标准 OpenAI SSE。
# 性能远优于老版（老版默认带长 reasoning，复杂任务可到分钟级）。
QODER_CHAT_URL_NEW = "https://api2-v2.qoder.sh/model/v1/chat/completions"


def now_ms() -> int:
    return int(time.time() * 1000)


def blank_response_meta() -> dict[str, Any]:
    return {
        "id": "",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def template_base() -> dict[str, Any]:
    return {
        "request_id": str(uuid.uuid4()),
        "request_set_id": str(uuid.uuid4()),
        "chat_record_id": str(uuid.uuid4()),
        "stream": True,
        "chat_task": "FREE_INPUT",
        "chat_context": {
            "chatPrompt": "",
            "extra": {"context": [], "modelConfig": {"is_reasoning": False, "key": "lite"}, "originalContent": {"type": "text", "text": "hi"}},
            "features": [],
            "imageUrls": None,
            "text": {"type": "text", "text": "hi"},
        },
        "image_urls": None,
        "is_reply": True,
        "is_retry": False,
        "session_id": str(uuid.uuid4()),
        "code_language": "",
        "source": 1,
        "version": "3",
        "chat_prompt": "",
        "parameters": {"max_tokens": 32768},
        "aliyun_user_type": "personal_standard",
        "session_type": "qodercli",
        "agent_id": "agent_common",
        "task_id": "common",
        "model_config": {
            "key": "lite",
            "display_name": "Lite",
            "model": "",
            "format": "openai",
            "is_vl": False,
            "is_reasoning": False,
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": 180000,
        },
        "messages": [
            {
                "role": "system",
                "content": "You are Qoder, an interactive CLI tool that helps users with software engineering tasks.",
                "response_meta": blank_response_meta(),
                "reasoning_content_signature": "",
            }
        ],
        "tools": [],
        "business": {"product": "cli", "version": "0.1.43", "type": "agent", "id": str(uuid.uuid4()), "name": "hi", "begin_at": now_ms(), "stage": "start"},
    }


def normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [normalize_content_part(item) for item in content]
        return "\n\n".join(part for part in parts if part.strip())
    return normalize_content_part(content)


def normalize_content_part(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        if isinstance(item.get("text"), str):
            return item["text"]
        if item.get("type") in {"image_url", "input_image"} and isinstance(item.get("image_url"), dict):
            url = item["image_url"].get("url")
            if isinstance(url, str):
                return f"[image] {url}"
        if isinstance(item.get("content"), (dict, list)):
            return normalize_content(item["content"])
        return json.dumps(item, ensure_ascii=False)
    return json.dumps(item, ensure_ascii=False)


def normalize_message_text(message: dict[str, Any]) -> str:
    text = normalize_content(message.get("content"))
    return text if text.strip() else normalize_content(message.get("contents"))


def extract_latest_user_prompt(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            text = normalize_message_text(message)
            if text.strip():
                return text
    return ""


def build_user_message(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": "",
        "contents": [{"type": "text", "text": text}],
        "response_meta": blank_response_meta(),
        "reasoning_content_signature": "",
    }


def build_structured_message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text or "", "response_meta": blank_response_meta(), "reasoning_content_signature": ""}


def normalize_tool_arguments(arguments: Any) -> str:
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def normalize_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw_tool_calls, list):
        return None
    normalized = []
    for raw in raw_tool_calls:
        function = raw.get("function", {}) if isinstance(raw, dict) else {}
        name = function.get("name", "")
        arguments = normalize_tool_arguments(function.get("arguments"))
        if not name and not arguments:
            continue
        normalized.append({"id": raw.get("id", ""), "type": raw.get("type", "function"), "function": {"name": name, "arguments": arguments}})
    return normalized or None


_USAGE_CONTAINER_KEYS = ("usage", "raw_usage", "rawUsage", "response_meta", "responseMeta")
_VENDOR_CACHE_KEYS = (
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _usage_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _first_int(source: Any, *keys: str) -> int | None:
    if not isinstance(source, dict):
        return None
    for key in keys:
        parsed = _usage_int(source.get(key))
        if parsed is not None:
            return parsed
    return None


def _merge_usage(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge usage objects: first non-null scalar wins, nested detail dicts combine."""
    for key, value in source.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_usage(target[key], value)
        elif key not in target or target[key] is None:
            target[key] = copy.deepcopy(value)


def _usage_candidates(raw: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Collect every dict that may carry usage, canonical containers first."""
    if depth > 3 or not isinstance(raw, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for key in _USAGE_CONTAINER_KEYS:
        nested = raw.get(key)
        if isinstance(nested, dict):
            candidates.extend(_usage_candidates(nested, depth + 1))
    # The bare object comes last so stray top-level counters (for example a
    # zeroed response_meta echo) cannot shadow a real nested usage object.
    candidates.append(raw)
    return candidates


def normalize_usage(raw: Any) -> dict[str, Any] | None:
    """Normalize the usage shapes returned by the modern and legacy gateways.

    Prompt-cache counters are preserved and mapped onto the OpenAI
    ``prompt_tokens_details.cached_tokens`` field so downstream clients (for
    example New API) can report a cache hit rate. Container objects such as
    ``usage``, ``raw_usage`` and ``response_meta`` are merged because Qoder's
    provider payload can carry cache detail the normalized object omits.
    """
    if not isinstance(raw, dict):
        return None
    merged: dict[str, Any] = {}
    for candidate in _usage_candidates(raw):
        _merge_usage(merged, candidate)

    prompt = _first_int(merged, "prompt_tokens", "input_tokens", "promptTokens", "inputTokens")
    completion = _first_int(merged, "completion_tokens", "output_tokens", "completionTokens", "outputTokens")
    total = _first_int(merged, "total_tokens", "totalTokens")
    if prompt is None and completion is None and total is None:
        return None
    prompt = prompt or 0
    completion = completion or 0

    prompt_details = merged.get("prompt_tokens_details") or merged.get("input_tokens_details")
    completion_details = merged.get("completion_tokens_details") or merged.get("output_tokens_details")

    # OpenAI-style details are inclusive in prompt_tokens; DeepSeek-style
    # hit/miss counters are too, while Anthropic-style cache_read counters are
    # additive and need the prompt total widened to stay consistent.
    cached = _first_int(prompt_details, "cached_tokens", "cachedTokens")
    if cached is None:
        cached = _first_int(merged, "cached_tokens", "prompt_cache_hit_tokens", "promptCacheHitTokens")
    if cached is None:
        cache_read = _first_int(merged, "cache_read_input_tokens", "cacheReadInputTokens")
        if cache_read is not None:
            cache_creation = _first_int(merged, "cache_creation_input_tokens", "cacheCreationInputTokens") or 0
            if prompt < cache_read:
                prompt += cache_read + cache_creation
            cached = cache_read
    if cached is not None and prompt:
        cached = min(cached, prompt)

    total = total if total is not None else prompt + completion
    total = max(total, prompt + completion)
    result: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}

    details: dict[str, int] = {}
    if cached is not None:
        details["cached_tokens"] = cached
    audio = _first_int(prompt_details, "audio_tokens", "audioTokens")
    if audio is not None:
        details["audio_tokens"] = audio
    if details:
        result["prompt_tokens_details"] = details

    completion_out: dict[str, int] = {}
    reasoning = _first_int(completion_details, "reasoning_tokens", "reasoningTokens")
    if reasoning is None:
        reasoning = _first_int(merged, "reasoning_tokens", "reasoningTokens")
    if reasoning is not None:
        completion_out["reasoning_tokens"] = reasoning
    if completion_out:
        result["completion_tokens_details"] = completion_out

    for key in _VENDOR_CACHE_KEYS:
        value = _first_int(merged, key)
        if value is not None:
            result[key] = value
    return result


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate used only when upstream omits usage."""
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def estimate_usage(req: dict[str, Any], output_text: str) -> dict[str, Any]:
    prompt_payload = json.dumps(
        {"messages": req.get("messages", []), "tools": req.get("tools", [])},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt_tokens = estimate_tokens(prompt_payload)
    completion_tokens = estimate_tokens(output_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        # Keep the OpenAI usage shape stable even when the upstream omits
        # usage; the telemetry entry marks the estimate separately.
        "prompt_tokens_details": {"cached_tokens": 0},
    }


def parse_tool_calls_text(text: str | None) -> list[dict[str, Any]] | None:
    if not text:
        return None
    trimmed = text.strip()
    if not trimmed.startswith("Tool calls:"):
        return None
    payload = trimmed[len("Tool calls:") :].strip()
    if payload.startswith("```") and payload.endswith("```"):
        newline = payload.find("\n")
        if newline >= 0:
            payload = payload[newline + 1 : -3].strip()
    if not payload.startswith("["):
        return None
    try:
        return normalize_tool_calls(json.loads(payload))
    except json.JSONDecodeError:
        return None


def render_tool_result(message: dict[str, Any], text: str) -> str:
    label = "Tool result"
    if message.get("name"):
        label += f" ({message['name']})"
    if message.get("tool_call_id"):
        label += f" [{message['tool_call_id']}]"
    return f"{label}:\n{text}" if text.strip() else label


def convert_incoming_message(message: dict[str, Any], tools_enabled: bool) -> dict[str, Any] | None:
    role = message.get("role", "user")
    text = normalize_message_text(message)
    if not tools_enabled and message.get("tool_calls"):
        calls = json.dumps(message["tool_calls"], ensure_ascii=False)
        text = f"{text}\n\nTool calls:\n{calls}" if text.strip() else f"Tool calls:\n{calls}"
    if role == "tool":
        if tools_enabled:
            out = build_structured_message("tool", text)
            if message.get("name"):
                out["name"] = message["name"]
            if message.get("tool_call_id"):
                out["tool_call_id"] = message["tool_call_id"]
            return out
        role = "user"
        text = render_tool_result(message, text)
    if not text.strip() and not (tools_enabled and role == "assistant" and message.get("tool_calls")):
        return None
    if role == "user":
        return build_user_message(text)
    out = build_structured_message(role, text)
    if tools_enabled and role == "assistant":
        tool_calls = normalize_tool_calls(message.get("tool_calls")) or parse_tool_calls_text(text)
        if tool_calls:
            out["tool_calls"] = tool_calls
            if parse_tool_calls_text(text):
                out["content"] = ""
    return out


def build_qoder_messages(template_messages: list[dict[str, Any]], incoming: list[dict[str, Any]], prompt: str, tools_enabled: bool) -> list[dict[str, Any]]:
    rebuilt = []
    if not any(message.get("role") == "system" for message in incoming or []):
        rebuilt.extend(copy.deepcopy(message) for message in template_messages if message.get("role") == "system")
    for message in incoming or []:
        converted = convert_incoming_message(message, tools_enabled)
        if converted:
            rebuilt.append(converted)
    if not rebuilt and prompt.strip():
        rebuilt.append(build_user_message(prompt))
    return rebuilt


def build_cn_qoder_body(req: dict[str, Any], model: str) -> dict[str, Any]:
    """Build the encoded legacy body used by the CN gateway."""
    raw_messages = copy.deepcopy(req.get("messages") if isinstance(req.get("messages"), list) else [])
    tools = copy.deepcopy(req.get("tools") if isinstance(req.get("tools"), list) else [])
    tools_enabled = bool(tools)
    last_user_text = ""
    for message in reversed(raw_messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                last_user_text = content
            elif isinstance(content, list):
                last_user_text = "\n".join(
                    str(part.get("text") or "") for part in content if isinstance(part, dict)
                )
            break

    # The CN gateway expects the Qoder client-native message shape. Raw OpenAI
    # messages mostly work for plain text, but an assistant message carrying
    # tool calls with ``"content": null`` makes the upstream return an empty
    # completion (no reasoning, no content, finish_reason=stop).
    messages: list[dict[str, Any]] = []
    for message in raw_messages:
        if not isinstance(message, dict):
            continue
        converted = convert_incoming_message(message, tools_enabled)
        if converted:
            messages.append(converted)
    if not messages and last_user_text.strip():
        messages.append(build_user_message(last_user_text))

    model_key = cn_model_key(model)
    max_tokens = req.get("max_tokens") or 32768
    parameters: dict[str, Any] = {"max_tokens": max_tokens, "enable_thinking": False}
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "reasoning_effort"):
        if key in req and req[key] is not None:
            parameters[key] = req[key]
    if req.get("reasoning_effort") not in (None, "", "off"):
        parameters["enable_thinking"] = True

    return {
        "request_id": str(uuid.uuid4()),
        "request_set_id": str(uuid.uuid4()),
        "chat_record_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "stream": True,
        "chat_task": "FREE_INPUT",
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "session_type": "qodercli",
        "agent_id": "agent_common",
        "task_id": "common",
        "code_language": "",
        "chat_prompt": "",
        "system": "",
        "image_urls": None,
        "aliyun_user_type": "",
        "messages": messages,
        "tools": tools,
        "parameters": parameters,
        "chat_context": {
            "chatPrompt": "",
            "imageUrls": None,
            "extra": {
                "context": [],
                "modelConfig": {"key": model_key, "is_reasoning": bool(parameters.get("enable_thinking"))},
                "originalContent": last_user_text,
            },
            "features": [],
            "text": last_user_text,
        },
        "model_config": {
            "key": model_key,
            "display_name": model,
            "model": "",
            "format": "openai",
            "is_vl": False,
            "is_reasoning": bool(parameters.get("enable_thinking")),
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": 180000,
        },
        "business": {
            "product": "cli",
            "version": "1.0.0",
            "type": "agent",
            "stage": "start",
            "id": str(uuid.uuid4()),
            "name": last_user_text[:30],
            "begin_at": now_ms(),
        },
    }


def build_qoder_body(req: dict[str, Any], sess: SessionContext) -> tuple[dict[str, Any], str, bool]:
    """Build the regional upstream body while preserving the OpenAI input shape."""
    model = req.get("model") or "lite"
    messages = req.get("messages") if isinstance(req.get("messages"), list) else []
    tools_enabled = bool(req.get("tools"))
    if sess.region == "cn":
        return build_cn_qoder_body(req, model), model, tools_enabled
    rid = str(uuid.uuid4())
    body: dict[str, Any] = {
        "model": global_model_key(model),
        "messages": copy.deepcopy(messages or []),
        "stream": True,
        "stream_options": {"include_usage": True},
        "metadata": {
            "context": {
                "request_id": rid,
                "request_set_id": rid,
                "session_id": str(uuid.uuid4()),
                "task_id": "common",
                "client_type": "qodercli",
            }
        },
    }
    if tools_enabled:
        body["tools"] = copy.deepcopy(req["tools"])
    # Preserve the client's cache routing hints so upstream prompt caching can
    # actually hit across requests.
    for routing_key in ("prompt_cache_key", "user"):
        routing_value = req.get(routing_key)
        if isinstance(routing_value, str) and routing_value.strip():
            body[routing_key] = routing_value
    return body, model, tools_enabled


@dataclass
class BridgeDelta:
    role: str = ""
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    usage: dict[str, Any] | None = None
    reasoning_content: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.role and not self.content and not self.tool_calls and not self.usage and not self.reasoning_content


def extract_delta(data_line: str) -> BridgeDelta:
    try:
        obj = json.loads(data_line)
        if not isinstance(obj, dict):
            return BridgeDelta()
        usage = normalize_usage(obj)
        # 新版：标准 OpenAI chunk（choices 直接在顶层）
        if "choices" in obj:
            for choice in obj.get("choices", []):
                delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                role = delta.get("role") or ""
                content = delta.get("content") or ""
                reasoning_content = delta.get("reasoning_content") or delta.get("reasoning") or ""
                tool_calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else None
                if role or content or reasoning_content or tool_calls:
                    return BridgeDelta(role, content, tool_calls, usage, reasoning_content)
            return BridgeDelta(usage=usage)
        # 老版：wrapper 内嵌 body 字符串
        inner = obj.get("body")
        if not inner:
            return BridgeDelta(usage=usage)
        inner_json = json.loads(inner) if isinstance(inner, str) else inner
        if not isinstance(inner_json, dict):
            return BridgeDelta(usage=usage)
        inner_delta = extract_delta(json.dumps(inner_json, ensure_ascii=False, separators=(",", ":")))
        if inner_delta.usage is None:
            inner_delta.usage = usage
        return inner_delta
    except (TypeError, json.JSONDecodeError):
        return BridgeDelta()
    return BridgeDelta()


def make_chunk(
    chunk_id: str,
    created: int,
    model: str,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


class ToolCallAccumulator:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def append(self, delta_calls: list[dict[str, Any]]) -> None:
        for delta in delta_calls:
            index = delta.get("index") if isinstance(delta.get("index"), int) else len(self.calls)
            while len(self.calls) <= index:
                self.calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            existing = self.calls[index]
            if isinstance(delta.get("id"), str):
                existing["id"] = delta["id"]
            if isinstance(delta.get("type"), str):
                existing["type"] = delta["type"]
            function = delta.get("function") or {}
            if isinstance(function.get("name"), str):
                existing["function"]["name"] = function["name"]
            if isinstance(function.get("arguments"), str):
                existing["function"]["arguments"] += function["arguments"]

    def snapshot(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.calls)


async def qoder_stream_lines(sess: SessionContext, body: dict[str, Any], model: str) -> AsyncIterator[str]:
    if sess.region == "cn":
        # The CN gateway uses the same encoded COSY/SSE protocol as Qoder's
        # current client. The response parser below accepts its envelope.
        encoded_body = encoding.encode(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        chat_url = get_region("cn").chat_url
        headers = bearer_headers(
            sess,
            chat_url,
            encoded_body,
            "text/event-stream",
            {
                "x-model-key": cn_model_key(model),
                "x-model-source": "system",
            },
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15), **httpx_client_kwargs()) as client:
            async with client.stream("POST", chat_url, content=encoded_body.encode("ascii"), headers=headers) as response:
                if response.status_code != 200:
                    text = await response.aread()
                    raise RuntimeError(f"HTTP {response.status_code} {text.decode(errors='replace')}")
                async for line in response.aiter_lines():
                    if line:
                        yield line
        return

    """新版国际协议：Bearer 直连 OpenAI-compatible chat。"""
    ctx = (body.get("metadata") or {}).get("context") or {}
    headers = {
        "Authorization": f"Bearer {sess.identity.security_oauth_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "qoder/1.1.65",
        "X-Request-ID": ctx.get("request_id", ""),
        "X-Session-ID": ctx.get("session_id", ""),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15), **httpx_client_kwargs()) as client:
        async with client.stream("POST", get_region("global").modern_chat_url or QODER_CHAT_URL_NEW, json=body, headers=headers) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(f"HTTP {response.status_code} {text.decode(errors='replace')}")
            async for line in response.aiter_lines():
                if line:
                    yield line


async def stream_openai_response(req: dict[str, Any], sess: SessionContext, usage_state: dict[str, Any] | None = None) -> AsyncIterator[str]:
    body, model, tools_enabled = build_qoder_body(req, sess)
    chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    tool_calls = ToolCallAccumulator()
    emitted = False
    pending = ""
    streaming_text = False
    pending_role = "assistant"
    upstream_usage: dict[str, Any] | None = None
    output_text: list[str] = []
    output_reasoning: list[str] = []

    def event(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"

    async for line in qoder_stream_lines(sess, body, model):
        if not line.startswith("data:"):
            continue
        delta = extract_delta(line[5:].strip())
        if delta.is_empty:
            continue
        if delta.usage:
            upstream_usage = delta.usage
        if delta.role:
            pending_role = delta.role
        if delta.reasoning_content:
            output_reasoning.append(delta.reasoning_content)
            out_delta = {"reasoning_content": delta.reasoning_content}
            if not emitted:
                out_delta["role"] = pending_role
            emitted = True
            yield event(make_chunk(chunk_id, created, model, out_delta))
        if delta.tool_calls:
            pending = "" if tools_enabled and pending.lstrip().startswith("Tool calls:") else pending
            indexed = []
            for index, call in enumerate(delta.tool_calls):
                item = copy.deepcopy(call)
                item.setdefault("index", index)
                indexed.append(item)
            tool_calls.append(indexed)
            out_delta = {"tool_calls": indexed}
            if not emitted:
                out_delta["role"] = pending_role
            emitted = True
            yield event(make_chunk(chunk_id, created, model, out_delta))
            continue
        if not delta.content:
            continue
        if not tools_enabled or streaming_text:
            output_text.append(delta.content)
            out_delta = {"content": delta.content}
            if not emitted:
                out_delta["role"] = pending_role
            emitted = True
            streaming_text = True
            yield event(make_chunk(chunk_id, created, model, out_delta))
            continue
        pending += delta.content
        candidate = pending.lstrip()
        if "Tool calls:".startswith(candidate) or candidate.startswith("Tool calls:"):
            continue
        streaming_text = True
        output_text.append(pending)
        out_delta = {"content": pending}
        if not emitted:
            out_delta["role"] = pending_role
        emitted = True
        pending = ""
        yield event(make_chunk(chunk_id, created, model, out_delta))

    parsed_calls = parse_tool_calls_text(pending) if tools_enabled else None
    if parsed_calls:
        indexed = []
        for index, call in enumerate(parsed_calls):
            item = copy.deepcopy(call)
            item.setdefault("index", index)
            indexed.append(item)
        tool_calls.append(indexed)
        yield event(make_chunk(chunk_id, created, model, {"tool_calls": indexed, "role": pending_role} if not emitted else {"tool_calls": indexed}))
    elif pending:
        output_text.append(pending)
        yield event(make_chunk(chunk_id, created, model, {"content": pending, "role": pending_role} if not emitted else {"content": pending}))

    finish_reason = "tool_calls" if tool_calls.calls else "stop"
    usage = upstream_usage or estimate_usage(req, "".join(output_reasoning) + "".join(output_text))
    estimated = upstream_usage is None
    if usage_state is not None:
        usage_state.clear()
        usage_state.update({"usage": usage, "estimated": estimated})
    yield event(make_chunk(chunk_id, created, model, {}, finish_reason, usage))
    yield "data: [DONE]\n\n"


async def complete_openai_response(req: dict[str, Any], sess: SessionContext) -> dict[str, Any]:
    body, model, tools_enabled = build_qoder_body(req, sess)
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    full = []
    tool_calls = ToolCallAccumulator()
    upstream_usage: dict[str, Any] | None = None
    reasoning: list[str] = []
    async for line in qoder_stream_lines(sess, body, model):
        if not line.startswith("data:"):
            continue
        delta = extract_delta(line[5:].strip())
        if delta.usage:
            upstream_usage = delta.usage
        if delta.content:
            full.append(delta.content)
        if delta.reasoning_content:
            reasoning.append(delta.reasoning_content)
        if delta.tool_calls:
            tool_calls.append(delta.tool_calls)
    content = "".join(full)
    fallback_tool_calls = None if tool_calls.calls or not tools_enabled else parse_tool_calls_text(content)
    message: dict[str, Any] = {"role": "assistant"}
    reasoning_text = "".join(reasoning)
    if reasoning_text:
        message["reasoning_content"] = reasoning_text
    if fallback_tool_calls:
        message["content"] = None
        message["tool_calls"] = fallback_tool_calls
    elif not content and tool_calls.calls:
        message["content"] = None
    else:
        message["content"] = content
    if tool_calls.calls:
        message["tool_calls"] = tool_calls.snapshot()
    usage = upstream_usage or estimate_usage(req, reasoning_text + content)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls.calls or fallback_tool_calls else "stop"}],
        "usage": usage,
        "_qodergate_usage_estimated": upstream_usage is None,
    }
