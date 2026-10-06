# 账号池

账号池让 QoderGate 可以通过多个 Qoder 账号处理请求，并在某个账号失败时自动切换到其他账号。

## 导入方式

### Auto Import

读取当前机器上的 Qoder 本地登录会话，并导入 SQLite。

### Add PAT

通过 Qoder Personal Access Token（`pt-…`）换取可用会话，并保存到账号池。PAT 本身也会落库，用于后续自动续期。

> 推荐优先使用 Add PAT：PAT 不会过期，只要它有效，网关就能一直重新兑换出新的 job token。Auto Import 只能拿到会过期的 session token。

## Token 自动刷新

账号池里存两类凭据，刷新优先级是「有 PAT 就用 PAT」：

| 存了什么 | 刷新方式 |
| --- | --- |
| PAT（`pt-…`） | `POST {openapi}/api/v1/jobToken/exchange` |
| job token（`jrt-…`） | `POST {openapi}/api/v1/jobToken/refresh` |
| device token（`drt-…`） | `POST {openapi}/api/v1/deviceToken/refresh`（额外带 machine 身份） |

`{openapi}` 按账号区域选择：国际 `openapi.qoder.sh`，中国 `openapi.qoder.com.cn`。

刷新会在三种时机发生：

1. 请求前，token 进入到期前 5 分钟窗口时同步换新；
2. 后台线程按最近一个到期时间定时刷新；
3. 上游返回 401/403 时，换新成功则原地重试当前账号。

也可以手动触发：`POST /ui/accounts/refresh-tokens`（全部）或 `/ui/accounts/{uid}/refresh-token`（单个）。

## 自动去重

账号按 `uid` 去重。重复导入同一个用户时，会更新会话数据，而不是创建重复账号。

## 启用和禁用

禁用的账号仍保留在 SQLite 中，但不会参与请求路由。

## Active Account

Active 账号会作为请求的第一候选。若请求失败，QoderGate 会自动轮转到其他启用账号。

## 额度字段

| 字段 | 含义 |
| --- | --- |
| `quota` | Qoder 返回的当前额度值。 |
| `is_quota_exceeded` | 账号是否已经超出额度。 |
| `plan` | 账号套餐标识。 |
| `user_tag` | Qoder 返回的展示标签。 |
| `next_reset_at` | 额度预计重置时间。 |

## 导入记录字段

批量导入支持注册机导出的 JSON 数组，也支持 `{"accounts": [...]}` 包裹形式。每条记录可用字段：

| 字段 | 含义 |
| --- | --- |
| `user_id` / `uid` | 账号主键；缺失时用 token 前 24 位兜底。 |
| `pat` / `personal_access_token` | PAT，会存入 `personal_access_token` 列，优先用于刷新。 |
| `token` / `security_oauth_token` | 已是 job/device token 时的直接凭据。 |
| `refresh_token` | `jrt-`/`drt-` 刷新令牌。 |
| `machine_id` | device token 刷新所需；缺失时生成新的。 |
| `region` | `global` 或 `cn`；缺失时用 `QODER_REGION`。 |
| `expires_at` | token 到期时间，写入 `token_expires_at`。 |
