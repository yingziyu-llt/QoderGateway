# New API Provider

QoderGateway can be configured as a standard OpenAI upstream channel for New API:

```text
User/client -> New API -> QoderGateway -> Qoder account pool
```

New API owns users, groups, model permissions, API keys, quotas, and audit logs. QoderGateway owns Qoder account import, token refresh, quota rotation, and protocol conversion.

## Configure QoderGateway

Set the following in `.env`:

```env
QODER_PROVIDER_MODE=new_api
QODER_PROVIDER_API_KEY=change-this-channel-key
QODER_PROVIDER_MODELS=lite
QODER_REGION=global
```

For China, use the supported regional model IDs, for example:

```env
QODER_REGION=cn
QODER_PROVIDER_MODELS=lite,qwen3.8-max,qwen3.8-flash,glm-5.3,kimi-k3
```

When `QODER_PROVIDER_MODELS` is empty the regional catalog is used: the gateway first pulls the live model list from Qoder (`algo/api/v2/model/list`) and falls back to the built-in catalog. The built-in CN catalog is: `auto`, `lite`, `qwen3.8-max`, `qwen3.8-flash`, `qwen3.7-max`, `qwen3.7-plus`, `qwen3.7-flash`, `deepseek-v4-pro`, `deepseek-flash`, `glm-5.3`, `glm-5.3-flash`, `glm-5.2`, `kimi-k3`, `kimi-k2.8-preview`, `minimax-m2.7`.

Keep at least one Qoder account enabled and check:

```bash
curl http://127.0.0.1:5050/healthz
curl http://127.0.0.1:5050/readyz
curl http://127.0.0.1:5050/v1/models \
  -H 'Authorization: Bearer change-this-channel-key'
```

## Configure the New API Channel

Create a standard OpenAI channel in the New API admin console:

| Field | Value |
| --- | --- |
| Type | OpenAI |
| Base URL | `http://qodergate:5050/v1` |
| API Key | Exactly the value of `QODER_PROVIDER_API_KEY` |
| Models | The same IDs as `QODER_PROVIDER_MODELS` |
| Model mapping | Optional aliases to Qoder model IDs |
| Groups | User groups allowed to use this channel |

Use the `/v1` Base URL, without `/chat/completions`. Save the channel, run its test, and fetch the upstream models.

## User Requests

Users call New API with a New API issued key:

```bash
curl http://new-api.example.com/v1/chat/completions \
  -H 'Authorization: Bearer <new-api-user-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "lite",
    "messages": [{"role": "user", "content": "Say hello"}],
    "stream": false
  }'
```

Direct calls to QoderGateway bypass New API user quotas, model permissions, and audit logs. This provider currently exposes Chat Completions only; `/v1/responses`, remote compaction, Anthropic Messages, embeddings, and image APIs are outside this phase.

## Usage Boundary

Qoder sometimes omits token usage. QoderGateway estimates it and marks the estimate in telemetry. When the upstream reports prompt-cache counters, the gateway relays them as `prompt_tokens_details.cached_tokens` so New API can compute a cache hit rate. New API accounting is internal control data, not an authoritative Qoder Credits balance or invoice.
