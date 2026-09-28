# New API 供应商

QoderGateway 可以作为 New API 的标准 OpenAI 上游渠道。推荐拓扑是：

```text
用户/客户端 -> New API -> QoderGateway -> Qoder 账号池
```

New API 管理用户、分组、模型权限、API Key、配额和审计；QoderGateway 管理 Qoder 账号、token 刷新、额度轮转和协议转换。

## 配置 QoderGateway

在 `.env` 中设置：

```env
QODER_PROVIDER_MODE=new_api
QODER_PROVIDER_API_KEY=change-this-channel-key
QODER_PROVIDER_MODELS=lite
QODER_REGION=global
```

中国区可以使用已经支持的模型目录，例如：

```env
QODER_REGION=cn
QODER_PROVIDER_MODELS=lite,qwen3.7-max,qwen3.7-plus,qwen3.6-flash
```

确保至少有一个启用的 Qoder 账号，然后检查：

```bash
curl http://127.0.0.1:5050/healthz
curl http://127.0.0.1:5050/readyz
curl http://127.0.0.1:5050/v1/models \
  -H 'Authorization: Bearer change-this-channel-key'
```

## 配置 New API 渠道

在 New API 管理后台新增标准 OpenAI 渠道：

| 字段 | 值 |
| --- | --- |
| 类型 | OpenAI |
| Base URL | `http://qodergate:5050/v1` |
| API Key | 与 `QODER_PROVIDER_API_KEY` 完全相同 |
| 模型 | 与 `QODER_PROVIDER_MODELS` 一致 |
| 模型映射 | 按需配置用户别名到 Qoder 模型 ID |
| 分组 | 绑定允许使用该渠道的用户组 |

Base URL 填到 `/v1`，不要填写 `/chat/completions`。保存后先执行渠道测试，再执行模型拉取。

## 用户调用

用户使用 New API 地址和 New API 签发的 Key：

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

直接调用 QoderGateway 会绕过 New API 的用户配额、模型权限和审计。QoderGateway 当前只提供 Chat Completions；`/v1/responses`、远程 compact、Anthropic Messages、Embedding 和图像接口不在本阶段支持范围内。

## 用量边界

Qoder 上游有时不返回 token usage，QoderGateway 会生成估算值并在内部 telemetry 中标记。New API 的计费和统计应视为内部控制数据，不等同于 Qoder 官方 Credits 或账单。
