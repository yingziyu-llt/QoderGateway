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
from .env import httpx_client_kwargs
from .regions import get_region


QODER_CHAT_URL = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
# 新版协议（Qoder CLI 现行）：OpenAI 兼容端点，纯 Bearer，无 COSY 签名，响应为标准 OpenAI SSE。
# 性能远优于老版（老版默认带长 reasoning，复杂任务可到分钟级）。
QODER_CHAT_URL_NEW = "https://api2-v2.qoder.sh/model/v1/chat/completions"

_CN_MODEL_KEYS = {
    "auto": "auto",
    "lite": "auto",
    "qwen3.7-max": "qmodel_latest",
    "qwen3.7plus": "qmodel",
    "qwen3.7-plus": "qmodel",
    "qwen3.6-flash": "q36fmodel",
    "deepseek-v4-pro": "dmodel",
    "deepseek-v4-flash": "dfmodel",
    "glm-5.2": "gm51model",
    "kimi-k2.7-code": "kmodel",
    "minimax-m2.7": "mmodel",
}


def cn_model_key(model: str) -> str:
    return _CN_MODEL_KEYS.get(model.strip().lower(), model)


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
    messages = copy.deepcopy(req.get("messages") if isinstance(req.get("messages"), list) else [])
    tools = copy.deepcopy(req.get("tools") if isinstance(req.get("tools"), list) else [])
    last_user_text = ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                last_user_text = content
            elif isinstance(content, list):
                last_user_text = "\n".join(
                    str(part.get("text") or "") for part in content if isinstance(part, dict)
                )
            break

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
        "model": model,
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
    return body, model, tools_enabled


@dataclass
class BridgeDelta:
    role: str = ""
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None

    @property
    def is_empty(self) -> bool:
        return not self.role and not self.content and not self.tool_calls


def extract_delta(data_line: str) -> BridgeDelta:
    try:
        obj = json.loads(data_line)
        if not isinstance(obj, dict):
            return BridgeDelta()
        # 新版：标准 OpenAI chunk（choices 直接在顶层）
        if "choices" in obj:
            for choice in obj.get("choices", []):
                delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                role = delta.get("role") or ""
                content = delta.get("content") or ""
                tool_calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else None
                if role or content or tool_calls:
                    return BridgeDelta(role, content, tool_calls)
            return BridgeDelta()
        # 老版：wrapper 内嵌 body 字符串
        inner = obj.get("body")
        if not inner:
            return BridgeDelta()
        inner_json = json.loads(inner) if isinstance(inner, str) else inner
        if not isinstance(inner_json, dict):
            return BridgeDelta()
        for choice in inner_json.get("choices", []):
            delta = choice.get("delta", {})
            role = delta.get("role") or ""
            content = delta.get("content") or ""
            tool_calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else None
            if role or content or tool_calls:
                return BridgeDelta(role, content, tool_calls)
    except (TypeError, json.JSONDecodeError):
        return BridgeDelta()
    return BridgeDelta()


def make_chunk(chunk_id: str, created: int, model: str, delta: dict[str, Any] | None = None, finish_reason: str | None = None) -> dict[str, Any]:
    return {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]}


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
        "User-Agent": "qoder/1.1.16",
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


async def stream_openai_response(req: dict[str, Any], sess: SessionContext) -> AsyncIterator[str]:
    body, model, tools_enabled = build_qoder_body(req, sess)
    chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    tool_calls = ToolCallAccumulator()
    emitted = False
    pending = ""
    streaming_text = False
    pending_role = "assistant"

    def event(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"

    async for line in qoder_stream_lines(sess, body, model):
        if not line.startswith("data:"):
            continue
        delta = extract_delta(line[5:].strip())
        if delta.is_empty:
            continue
        if delta.role:
            pending_role = delta.role
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
        yield event(make_chunk(chunk_id, created, model, {"content": pending, "role": pending_role} if not emitted else {"content": pending}))

    finish_reason = "tool_calls" if tool_calls.calls else "stop"
    yield event(make_chunk(chunk_id, created, model, {}, finish_reason))
    yield "data: [DONE]\n\n"


async def complete_openai_response(req: dict[str, Any], sess: SessionContext) -> dict[str, Any]:
    body, model, tools_enabled = build_qoder_body(req, sess)
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    full = []
    tool_calls = ToolCallAccumulator()
    async for line in qoder_stream_lines(sess, body, model):
        if not line.startswith("data:"):
            continue
        delta = extract_delta(line[5:].strip())
        if delta.content:
            full.append(delta.content)
        if delta.tool_calls:
            tool_calls.append(delta.tool_calls)
    content = "".join(full)
    fallback_tool_calls = None if tool_calls.calls or not tools_enabled else parse_tool_calls_text(content)
    message: dict[str, Any] = {"role": "assistant"}
    if fallback_tool_calls:
        message["content"] = None
        message["tool_calls"] = fallback_tool_calls
    elif not content and tool_calls.calls:
        message["content"] = None
    else:
        message["content"] = content
    if tool_calls.calls:
        message["tool_calls"] = tool_calls.snapshot()
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls.calls or fallback_tool_calls else "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
