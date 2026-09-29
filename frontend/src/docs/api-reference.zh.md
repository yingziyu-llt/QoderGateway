# API 参考

QoderGate 提供 OpenAI 兼容的 Chat Completions 接口。

## Base URL

```text
http://127.0.0.1:5050
```

## Chat Completions

```http
POST /v1/chat/completions
```

### 请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `model` | string | 否 | 默认是 `lite`。 |
| `messages` | array | 是 | OpenAI 风格消息列表。 |
| `stream` | boolean | 否 | 为 `true` 时启用 SSE 流式输出。 |
| `prompt_cache_key` | string | 否 | 透传给上游的 prompt 缓存路由键，用于稳定命中缓存。 |
| `user` | string | 否 | 透传给上游的终端用户标识，辅助缓存路由。 |

### 非流式示例

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

### 流式示例

```bash
curl http://127.0.0.1:5050/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "lite",
    "stream": true,
    "messages": [{ "role": "user", "content": "Stream a short answer" }]
  }'
```

## Token 用量与缓存命中

非流式响应的 `usage` 与流式最后一个 chunk 的 `usage` 采用 OpenAI 结构：

| 字段 | 说明 |
| --- | --- |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | 输入、输出与总 token。 |
| `prompt_tokens_details.cached_tokens` | 命中 prompt 缓存（KV cache）的输入 token 数。缓存命中率 = `cached_tokens / prompt_tokens`。 |
| `completion_tokens_details.reasoning_tokens` | 思考 token（上游返回时才有）。 |

Qoder 上游返回 DeepSeek 风格（`prompt_cache_hit_tokens`）或 Anthropic 风格（`cache_read_input_tokens`）计数时，网关会统一映射为 `prompt_tokens_details.cached_tokens`，原始字段也会一并保留。若上游未返回 usage，网关会给出估算值并在控制台标注“估算”，此时 `cached_tokens` 为 `0`。

控制台的 API Key 用量页与模型观测页会展示缓存 token 数与缓存命中率，统计接口为 `GET /ui/api-keys/usage` 与 `GET /ui/models`。

## 错误码

| 状态码 | 含义 |
| --- | --- |
| `401` | 缺少或传入了错误的 API Key。 |
| `400` | 当前没有可用的 Qoder 账号。 |
| `502` | 所有可用账号请求上游都失败。 |
