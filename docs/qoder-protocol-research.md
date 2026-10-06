# Qoder 协议逆向研究结果

> 研究日期：2026-08-06
> 来源：反编译 `@qodercn-ai/qoderclicn@1.1.16`（npm 包，66MB，`qoderclicn.js` 单文件 bundle）+ 对 `openapi.qoder.sh` / `api2-v2.qoder.sh` / `api3.qoder.sh` 的实测抓包
> 关联项目：QoderGateway（Python 版 qoder2api，本仓库）

---

## 0. 结论速览

- Qoder 存在**两代协议**：老版（`center.qoder.sh` + COSY 签名，cubk1/qoder2api 及本仓库 QoderGateway 所用）和新版（`openapi.qoder.sh` / `api2-v2.qoder.sh` + 纯 `Bearer` token，Qoder CLI 现行）。
- 新版协议是 **OpenAI 兼容**的：`POST https://api2-v2.qoder.sh/model/v1/chat/completions`，仅需 `Authorization: Bearer <security_oauth_token>`，无需任何签名。
- **同一个 `security_oauth_token`（`dt-` 前缀）在老版和新版端点上都能用**（已实测：两者都返回 200 并正常出流）。
- 登录/换取 token 走 **PKCE device flow**：`GET https://openapi.qoder.sh/api/v1/deviceToken/poll?nonce=...&verifier=...&challenge_method=S256` 轮询，**404 = 等待用户授权**（非错误），**200 = 授权完成返回 token 凭据**。

---

## 1. Token 数据结构（deviceToken poll 的 200 响应）

```json
{
  "id": "019fd6c9-...",
  "token": "dt-0IUJEMYfFUFu9beAsx0cMaR9",
  "user_id": "019ec623-...",
  "code_challenge": "h-1Oi6gLAOdnbUD2orefq3JxJz7j4iGjUobnD8JQJ-8",
  "code_challenge_method": "S256",
  "nonce": "7554805c-e90c-4110-87f8-579cbf3e16a8",
  "expires_at": "2026-09-05T11:16:15Z",
  "refresh_token_id": "019fd6c9-...",
  "refresh_token": "drt-Wq7deftCwhAJbQ7geTmyPrOL",
  "created_at": "2026-08-06T11:16:15Z",
  "updated_at": "2026-08-06T11:16:15Z",
  "expires_in": 2591999994,
  "refresh_token_expires_in": 31103999996,
  "refresh_token_expires_at": "2027-08-01T11:16:15Z"
}
```

关键字段到项目代码的映射：

| 字段 | 含义 | 写入 QoderGateway `accounts` 表的列 |
|---|---|---|
| `token` (`dt-`) | security_oauth_token | `security_oauth_token` |
| `refresh_token` (`drt-`) | refresh token | `refresh_token` |
| `user_id` | 用户 uid | `uid`（主键） |
| `code_challenge` / `nonce` | 登录流程内部参数，请求 API 时用不到 | 忽略 |
| `expires_at` | dt- 有效期 | 到期后需用 drt- 刷新 |

---

## 2. 新版协议端点（openapi.qoder.sh 域，纯 Bearer）

| 端点 | 方法 | 用途 | 请求头/体 |
|---|---|---|---|
| `https://openapi.qoder.sh/api/v1/deviceToken/poll` | GET | 轮询 device 授权结果 | `Accept: application/json` |
| `https://openapi.qoder.sh/api/v1/userinfo` | GET | 获取用户信息（uid/name/email/org） | `Authorization: Bearer <token>` |
| `https://openapi.qoder.sh/api/v1/jobToken/exchange` | POST | PAT → job token | body `{"personal_token": "<PAT>"}` |
| `https://openapi.qoder.sh/api/v1/jobToken/refresh` | POST | refresh token 换新 | body `{"refresh_token": "<drt-...>"}` |
| `https://openapi.qoder.sh/api/v1/serviceToken/exchange` | POST | service account key 换 token | body `{"grant_type":"client_credentials","audience":"qoder","scope":"...","ttl_seconds":3600}`，头 `Authorization: Bearer <serviceKey>` |
| `https://api2-v2.qoder.sh/model/v1/chat/completions` | POST | **OpenAI 兼容 chat 接口** | `Authorization: Bearer <token>`、`Content-Type: application/json`、`Accept: text/event-stream`、`X-Request-ID`、`X-Session-ID` |

### chat/completions 请求体（新版）

```json
{
  "model": "lite",
  "messages": [{"role": "user", "content": "hi"}],
  "stream": true,
  "stream_options": {"include_usage": true},
  "metadata": {
    "context": {
      "request_id": "<uuid>",
      "request_set_id": "<uuid>",
      "session_id": "<uuid>",
      "task_id": "common",
      "client_type": "qodercli"
    }
  }
}
```

- 响应为 SSE，标准 OpenAI `chat.completion.chunk` 格式，`raw_usage` 携带 token 统计。
- `tools`、`stop`、`max_tokens`、`temperature`、`reasoning_effort`、`patches`、`custom_model` 均可选传入。
- 401/403 时客户端会 `forceRefreshToken`（用 drt- 刷新）后重试一次。

### usage / prompt cache 结构（2026-09 网关侧实测）

同协议的 usage 可能出现在多个位置，网关 `normalize_usage()`（`src/qoder2api/bridge.py`）会合并 `usage`、`raw_usage`、`rawUsage`、`response_meta` 后归一化：

| 上游形态 | 示例字段 | 归一化结果 |
| --- | --- | --- |
| OpenAI / Qoder 新版 | `prompt_tokens_details.cached_tokens` | `prompt_tokens_details.cached_tokens` |
| `usage` 简版 + `raw_usage` 详版 | 简版无 details，`raw_usage` 带 `cached_tokens` | 合并后保留详情 |
| DeepSeek 风格 | `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | 追加映射到 `cached_tokens`，原始字段保留 |
| Anthropic 风格 | `cache_read_input_tokens` / `cache_creation_input_tokens` | `cached_tokens`，且 `prompt_tokens` 补齐为 input+cache |
| 老版信封 | `body.response_meta.usage` | 递归解析后同样归一化 |

缓存命中率统一定义为 `prompt_tokens_details.cached_tokens / prompt_tokens`。老版 CN 路径实测会返回真实 usage（`tokens_estimated=false`）；若上游不返回 usage，网关用估算值补齐并写入 `cached_tokens=0`，telemetry 以 `tokens_estimated` 标记。

直连复现脚本：`scripts/probe_cache_usage.py`（需要有效账号，会分别请求区域老版与新版端点并 dump 含 usage/cache 的原始 SSE 行）。

### 域名映射（国际版，国内为 `.com.cn`）

```js
inference : api2-v2.qoder.sh      (新版 chat)
center    : center.qoder.sh       (老版，QoderGateway 所用)
openapi   : openapi.qoder.sh      (鉴权/用户/兑换)
sse/chat  : api3.qoder.sh         (老版 agent_chat_generation，QoderGateway 所用)
```

---

## 3. 老版协议对照（QoderGateway 当前实现）

- 兑换：`POST https://center.qoder.sh/algo/api/v3/user/jobToken?Encode=1`，body `{"payload": "<QoderEncoding 编码>", "encodeVersion": "1"}`，`personalToken` 字段放 PAT。
- 编码（`src/qoder2api/encoding.py` 与上游 Java 一致）：**自定义 alphabet base64 + 三段重排**，无 XOR。alphabet：`_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!`，pad `$`。
- 签名：`signature = md5("cosy&d2FyLCB3YXIgbmV2ZXIgY2hhbmdlcw==&<RFC1123 GMT date>")`；`appcode = "cosy"`。
- 请求头：`cosy-machinetoken`、`cosy-machinetype`、`cosy-machineid`、`login-version: v2`、`cosy-version: 0.1.43`、`cosy-clienttype: 5`、UA `Go-http-client/2.0`。
- chat：`POST https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1`，`Authorization: Bearer COSY.<payloadB64>.<md5sig>`，body 为 `template_base()`（`src/qoder2api/bridge.py`）。
- 实测：**`dt-` 前缀的 security_oauth_token 在老版端点上同样有效**（用 QoderGateway 现有代码构造 `AuthIdentity` 直接成功）。

### CN 网关消息格式（2026-09 实测，Kimi K3 工具调用复现）

CN 端点（`gateway.qoder.com.cn/.../agent_chat_generation`）期望的是 Qoder 客户端**原生消息格式**，不是 OpenAI 原始消息：

- 用户消息：`{"role":"user","content":"","contents":[{"type":"text","text":"..."}],"response_meta":<usage>,"reasoning_content_signature":""}`
- 助手消息：`content` 必为字符串，同时带 `response_meta` 与 `reasoning_content_signature`；携带 `tool_calls` 时 `content` 必须是 `""`，**不能是 `null`**。
- 工具结果：`{"role":"tool","content":"...","name":"...","tool_call_id":"...","response_meta":<usage>,"reasoning_content_signature":""}`

实测矩阵（同一段“user → assistant(tool_calls) → tool result”对话，经 CN 网关）：

| 消息形态 | 上游结果 |
| --- | --- |
| 原始 OpenAI 透传，assistant `content: null` | **空补全**：`completion_tokens=0`、无 reasoning/content、立即 `finish_reason=stop` |
| 仅把 assistant `content` 改成 `""`（其余原始）| 正常回答 |
| 完整原生格式（上表三种消息）| 正常回答（reasoning + content 均正常） |

因此 `build_cn_qoder_body()` 会把入站 OpenAI 消息统一经 `convert_incoming_message()` 转换后再编码；转换后工具链（第一轮原生 tool_calls → 第二轮工具结果）已验证可用，消息中任何 `content: null` 都会被归一为字符串。

---

## 4. deviceToken 认证流程（PKCE device flow）

实现位于 qoderclicn bundle 的 `GfI()` 函数，共 4 步：

### 4.1 生成 PKCE 参数

```js
verifier  = 随机 43~128 个字符，字符集 "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"（66 字符）
challenge = BASE64URL(SHA256(verifier))   // base64 后 + → -, / → _, 去掉尾部 =
```

### 4.2 生成 nonce

`nonce = randomUUID()`（UUID v4）。

### 4.3 授权 URL（浏览器打开）

```
https://qoder.com/device/selectAccounts?challenge=<challenge>&challenge_method=S256&nonce=<nonce>&machine_id=<machineId>&client_id=<clientId>
```

- `client_id`（从混淆解密得到）：`e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb`（默认）或 `e93fe488-5778-4c35-a6fc-0f54ed7b3139`
- 用户登录 → 选账号 → 同意 → 服务端绑定 `challenge ↔ nonce ↔ 用户`

### 4.4 轮询 poll（循环）

```js
params = new URLSearchParams({ nonce, verifier, challenge_method: "S256" })
url = `https://openapi.qoder.sh/api/v1/deviceToken/poll?${params}`

while (Date.now() < deadline) {          // deadline = now + 300000ms（5 分钟超时）
    resp = fetch(url, { headers: {Accept: "application/json"},
                        markErrorStatus: s => s !== 404 })   // 404 不算错误
    if (resp.status === 404) { await sleep(1000); continue } // 1 秒一轮
    if (!resp.ok) throw ...
    data = await resp.json()
    if (data.token && typeof data.token === "string") return data   // 200 → 凭据
}
throw "timeout"
```

**状态码语义**：
- `404` = 用户尚未完成授权（设计内的"等待中"），每 1 秒重试，最长 5 分钟
- `200` = 授权完成，服务端校验 `SHA256(verifier) == challenge` 后返回 §1 的凭据 JSON
- 其他非 2xx = 真实错误

---

## 5. 混淆字符串解密方法（复现用）

bundle 文件第一行的解密函数：

```js
const _$d = (s, k = "syJkkdK5Dxwd") => {
  const b = Buffer.from(s, "base64");
  for (let i = 0; i < b.length; i++) b[i] ^= k.charCodeAt(i % k.length);
  return b.toString();
};
```

- 算法：base64 解码 → 用 key `syJkkdK5Dxwd` 循环 XOR。
- 已验证解出的常量：
  - client_id（两个）：`e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb`、`e93fe488-5778-4c35-a6fc-0f54ed7b3139`
  - verifier 字符集：`ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~`

---

## 6. 关键常量汇总（qoderclicn@1.1.16）

| 常量 | 值 | 含义 |
|---|---|---|
| `cxn` | `1000` | 轮询间隔 ms |
| `vfI` | `300000` | 轮询总超时 ms（5 分钟） |
| `HfI` | `3` | 网络错误重试次数 |
| `client_id` | 见 §4.3 | device flow 客户端标识 |
| verifier 字符集 | 66 字符 | PKCE verifier 合法字符 |

---

## 7. 实测记录（2026-08-06，token 为 `dt-` 前缀）

1. `GET openapi.qoder.sh/api/v1/userinfo`（Bearer dt-）→ **200**，返回用户 `bzym2`（GitHub SSO），`id` 与 poll 响应的 `user_id` 一致。
2. `POST api2-v2.qoder.sh/model/v1/chat/completions`（Bearer dt-，model=lite）→ **200**，SSE 流式正常出内容，带 `raw_usage`。
3. QoderGateway 老版路径（COSY 签名 + api3.qoder.sh agent_chat_generation，`security_oauth_token`=dt-）→ **200**，流式正常。
4. `jobToken` 老兑换端点把 `dt-`/`drt-` 当 `personalToken` 提交 → **401 `personal token is invalid`**（PAT 与 device token 是不同凭证，互不通用）。

---

## 8. 安全注意事项

- `verifier` + `nonce` 组合等于兑换凭证：poll URL 泄露给第三方可导致账号 token 被冒领，**不得外传**。
- `dt-` / `drt-` token 已明文存入 `~/.qoder/qoder2api.db`，该库 = 完整登录身份，注意文件权限与备份。
- `dt-` / `drt-` / `jt-` / `jrt-` 等 session token 会过期；**只有 PAT（`pt-`）能无限续期**，因此 PAT 也会一并入库（`personal_access_token` 列）。

### Token 自动刷新（已实现）

优先级：**有 PAT 就用 PAT 重新兑换，否则才用 refresh_token**。

| 凭据 | 刷新方式 |
| --- | --- |
| `pt-`（PAT） | `POST {openapi}/api/v1/jobToken/exchange`，body `{"personal_token": "<pt-...>"}` |
| `jrt-`（job token） | `POST {openapi}/api/v1/jobToken/refresh`，body `{"refresh_token": "<jrt-...>"}` |
| `drt-`（device token） | `POST {openapi}/api/v1/deviceToken/refresh`，body 额外带 `machine_id` / `machine_token` |

`{openapi}` 按区域选择：国际 `openapi.qoder.sh`，中国 `openapi.qoder.com.cn`。

> ⚠️ **不要**刷新到 `gateway.qoder.com.cn/algo/api/v3/user/refresh_token`——该端点不存在，实测返回 `403 Request discarded`。CN 客户端的刷新走 `openapi.qoder.com.cn` 的 `/api/v1/jobToken/refresh`。

刷新时机：

1. **请求前惰性刷新**：`ensure_fresh_token()` 在 token 进入 `token_expires_at - 5min` 窗口时同步换新。
2. **后台定时线程**：按最近一个到期时间休眠（上限 6 小时），到点刷新所有进入窗口的账号。
3. **上游 401/403 兜底**：换新成功后原地重试当前账号，而不是立刻丢弃。
4. **手动**：`POST /ui/accounts/refresh-tokens`（全部）或 `/ui/accounts/{uid}/refresh-token`（单个）。

---

## 9. 后续可做（未实施）

- [ ] QoderGateway 增加新版协议适配：`bridge.py` 新增 `api2-v2.qoder.sh/model/v1/chat/completions` 路径（纯 Bearer，无需 COSY 签名）
- [x] 增加 token 自动刷新：定时/请求前检查 `expires_at`，用 PAT 兑换或 `jobToken/refresh` 换新并回写数据库（见 §8）
- [ ] 自动化 device flow 脚本：生成 verifier/challenge → 打印授权 URL → 轮询 poll → 拿到凭据自动入库
