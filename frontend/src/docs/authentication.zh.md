# 鉴权机制

QoderGate 有两层鉴权：管理控制台鉴权，以及外部 API 调用鉴权。它们服务于不同场景，不应该混用。

## 管理控制台 Token

你在登录页输入的密钥会作为管理密钥使用。前端请求管理接口时会发送：

```http
X-Gateway-Token: admin
```

它保护这些接口：

- `/ui/status`
- `/ui/accounts`
- `/ui/config`
- `/ui/logs`
- `/ui/api-keys/usage`

## 外部 API Key

OpenAI 兼容接口可以单独开启 Bearer Key 校验。

开启后，客户端必须传入：

```http
Authorization: Bearer <allowed-api-key>
```

控制台的 API Key 管理页会按最近 24 小时统计每个 Key 和模型的请求数、输入 token、输出 token、总 token 以及 prompt 缓存命中率。统计接口是 `GET /ui/api-keys/usage?window_hours=24`，需要携带管理 Token；请求统计事件只保存 Key 的不可逆指纹，不保存原始 Key。原始 Key 仍保存在鉴权配置中，用于校验客户端请求。

## New API Provider 模式

将 `QODER_PROVIDER_MODE=new_api` 后，`/v1/models` 和 `/v1/chat/completions` 会强制要求 Bearer Key。优先使用环境变量 `QODER_PROVIDER_API_KEY`，未设置时兼容控制台中配置的 API Key。这个 Key 只填写到 New API 的渠道配置中，最终用户应使用 New API 签发的 Key。

## 两种密钥的区别

| 使用场景 | Header | 作用范围 |
| --- | --- | --- |
| 管理后台 | `X-Gateway-Token` | `/ui/*` 管理接口 |
| OpenAI 兼容调用 | `Authorization` | `/v1/chat/completions` |

## 推荐实践

- 不要把管理 Token 写入脚本或分享给外部客户端。
- 如果网关监听非本机地址，建议开启 API Key 鉴权。
- 如果 API Key 出现在日志、截图或脚本中，及时删除并重新生成。
