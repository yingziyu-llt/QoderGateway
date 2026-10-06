# Account Pool

The account pool lets QoderGate route requests through multiple Qoder accounts and recover when one account fails.

## Import Methods

### Auto Import

Reads the current local Qoder auth session from your machine and imports it into SQLite.

### Add PAT

Exchanges a Qoder Personal Access Token (`pt-…`) for a usable session and stores it in the account pool. The PAT itself is persisted so it can be re-exchanged later.

> Prefer Add PAT: a PAT does not expire, so the gateway can keep minting fresh job tokens as long as it stays valid. Auto Import only captures an expiring session token.

## Automatic Token Refresh

Two credential kinds live in the pool. Refresh prefers the PAT:

| Stored credential | Refresh call |
| --- | --- |
| PAT (`pt-…`) | `POST {openapi}/api/v1/jobToken/exchange` |
| Job token (`jrt-…`) | `POST {openapi}/api/v1/jobToken/refresh` |
| Device token (`drt-…`) | `POST {openapi}/api/v1/deviceToken/refresh` (also sends machine identity) |

`{openapi}` follows the account's region: `openapi.qoder.sh` globally, `openapi.qoder.com.cn` in China.

Refresh runs at three points:

1. Before a request, when the token enters the 5-minute pre-expiry window.
2. In a background thread, scheduled against the soonest known expiry.
3. On an upstream 401/403, re-authenticating and retrying the same account in place.

You can also refresh manually: `POST /ui/accounts/refresh-tokens` (all) or `/ui/accounts/{uid}/refresh-token` (one).

## Deduplication

Accounts are deduplicated by `uid`. Re-importing the same user updates session data instead of creating duplicates.

## Enable and Disable

Disabled accounts stay in SQLite but are skipped during routing.

## Active Account

The active account is the first account used for a request. If it fails, QoderGate rotates to another enabled account.

## Quota Fields

| Field | Meaning |
| --- | --- |
| `quota` | Current quota value reported by Qoder. |
| `is_quota_exceeded` | Whether the account is over quota. |
| `plan` | Account plan identifier. |
| `user_tag` | Display label from Qoder. |
| `next_reset_at` | When quota is expected to reset. |

## Import Record Fields

Batch import accepts either a JSON array or an `{"accounts": [...]}` envelope. Each record may use:

| Field | Meaning |
| --- | --- |
| `user_id` / `uid` | Account key; falls back to the first 24 characters of the token. |
| `pat` / `personal_access_token` | PAT, stored in the `personal_access_token` column and preferred for refresh. |
| `token` / `security_oauth_token` | Direct credential when the value is already a job/device token. |
| `refresh_token` | `jrt-`/`drt-` refresh token. |
| `machine_id` | Required for device-token refresh; generated when missing. |
| `region` | `global` or `cn`; defaults to `QODER_REGION`. |
| `expires_at` | Token expiry, written to `token_expires_at`. |
