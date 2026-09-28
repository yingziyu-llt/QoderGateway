# Authentication

QoderGate has two authentication layers: one for the management console and one for external API clients.

## Management Console Token

The WebUI uses the gateway token you enter on login. Frontend requests send it as:

```http
X-Gateway-Token: admin
```

This protects routes such as:

- `/ui/status`
- `/ui/accounts`
- `/ui/config`
- `/ui/logs`
- `/ui/api-keys/usage`

## External API Keys

The OpenAI-compatible API can optionally require Bearer keys.

When enabled, clients must send:

```http
Authorization: Bearer <allowed-api-key>
```

The API key page reports the last 24 hours of requests, input tokens, output tokens, and total tokens grouped by key and model. The management endpoint is `GET /ui/api-keys/usage?window_hours=24` and requires the management token. Request telemetry stores only a non-reversible key fingerprint, never the raw key; the raw key remains in the authentication configuration so incoming requests can be validated.

## New API Provider Mode

Set `QODER_PROVIDER_MODE=new_api` to require a Bearer key on `/v1/models` and `/v1/chat/completions`. The gateway first checks `QODER_PROVIDER_API_KEY`, then falls back to keys configured in the console. Use this shared key only in the New API channel; end users should receive New API issued keys.

## Which Token Should I Use?

| Use case | Header | Scope |
| --- | --- | --- |
| WebUI management | `X-Gateway-Token` | `/ui/*` routes |
| OpenAI-compatible calls | `Authorization` | `/v1/chat/completions` |

## Recommended Setup

- Keep the management token private.
- Enable API key auth before exposing the gateway to other machines.
- Rotate API keys if they are shared in logs or scripts.
