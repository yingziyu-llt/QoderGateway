# API Reference

QoderGate exposes an OpenAI-compatible chat completions endpoint.

## Base URL

```text
http://127.0.0.1:5050
```

## Chat Completions

```http
POST /v1/chat/completions
```

### Request Body

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `model` | string | No | Defaults to `lite`. |
| `messages` | array | Yes | OpenAI-style message list. |
| `stream` | boolean | No | Enables SSE streaming when `true`. |
| `prompt_cache_key` | string | No | Prompt-cache routing key forwarded to the upstream so cache hits stay stable. |
| `user` | string | No | End-user identifier forwarded to the upstream to help cache routing. |

### Non-Streaming Example

```bash
curl http://127.0.0.1:5050/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer qg_live_xxx" \
  -d '{
    "model": "lite",
    "stream": false,
    "messages": [{ "role": "user", "content": "Explain QoderGate" }]
  }'
```

### Streaming Example

```bash
curl http://127.0.0.1:5050/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "lite",
    "stream": true,
    "messages": [{ "role": "user", "content": "Stream a short answer" }]
  }'
```

## Token Usage and Cache Hits

The non-streaming `usage` object and the final streaming chunk use the OpenAI shape:

| Field | Meaning |
| --- | --- |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | Input, output, and total tokens. |
| `prompt_tokens_details.cached_tokens` | Input tokens served from the prompt cache (KV cache). Cache hit rate = `cached_tokens / prompt_tokens`. |
| `completion_tokens_details.reasoning_tokens` | Reasoning tokens, when the upstream reports them. |

When the upstream returns DeepSeek-style (`prompt_cache_hit_tokens`) or Anthropic-style (`cache_read_input_tokens`) counters, the gateway maps them onto `prompt_tokens_details.cached_tokens` and keeps the original fields as well. If the upstream omits usage, the gateway emits an estimate, marks it as estimated in the console, and reports `cached_tokens` as `0`.

The API key usage page and the model observability page show cached tokens and the cache hit rate; the endpoints are `GET /ui/api-keys/usage` and `GET /ui/models`.

## Error Responses

| Status | Meaning |
| --- | --- |
| `401` | Missing or invalid API key. |
| `400` | No active Qoder account available. |
| `502` | Upstream request failed across available accounts. |
