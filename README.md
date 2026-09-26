<h1 align="center">QoderGate</h1>

<p align="center">
  把多个 Qoder 账号统一转换成 OpenAI 兼容接口的本地网关。<br>
  A local gateway that turns multiple Qoder accounts into one OpenAI-compatible API.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue?logo=python&logoColor=white" alt="Python >= 3.11">
  <img src="https://img.shields.io/badge/fastapi-0.115+-green?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="License">
  <a href="https://linux.do"><img src="https://img.shields.io/badge/LINUX_DO-%E7%A4%BE%E5%8C%BA-blue" alt="LINUX DO"></a>
</p>

---

## 致谢 / Acknowledgment

本项目思路来源于 [cubk1/qoder2api](https://github.com/cubk1/qoder2api/)，在此基础上用 Python 重写了后端并新增了 WebUI 管理控制台、SQLite 持久化、多账号池轮转和独立文档站。

This project is inspired by [cubk1/qoder2api](https://github.com/cubk1/qoder2api/). We rewrote the backend in Python and added a WebUI management console, SQLite persistence, multi-account pool rotation, and a standalone documentation site.

特别感谢 [LINUX DO](https://linux.do) 社区提供的交流与推广平台。

Special thanks to the [LINUX DO](https://linux.do) community for the platform of exchange and promotion.

## 功能 / Features

- **OpenAI 兼容接口** — 通过 `/v1/chat/completions` 向客户端提供标准 Chat Completions API
- **多账号池** — 导入多个 Qoder 账号，按 UID 自动去重，请求失败时自动轮转
- **两层鉴权** — 管理后台密钥与外部 API Key 分开配置
- **SQLite 持久化** — 账号、API Key、全局配置全部存入本地数据库
- **WebUI 控制台** — Dashboard、账号管理、API Key 管理、Playground、服务日志
- **模型观测页** — 拉取当前区域模型目录，查看近 24 小时实际访问的成功率/首字节/总耗时，并对选中模型做短请求测速
- **独立文档站** — `/documents` 提供中英文 Wiki，支持本地搜索和目录跳转
- **自动检测语言** — 根据浏览器地区自动切换中文/英文

## 快速开始 / Quickstart

### 安装 / Install

```bash
git clone https://github.com/bzym2/QoderGateway.git
cd QoderGateway
uv sync
```

### 前端构建 / Build Frontend

```bash
cd frontend
npm install
npm run build
cd ..
```

构建产物会输出到 `src/qoder2api/static/`，后端启动时直接托管 WebUI 与文档站。

### 配置 / Configure

```bash
cp .env.example .env
```

编辑 `.env`，修改管理员密码：

```env
QODER_ADMIN_PASSWORD=your-strong-password
```

如果使用中国区 Qoder，请同时设置：

```env
QODER_REGION=cn
QODER_PAT=pt-...
```

中国区 PAT 在 `qoder.com.cn/account/integrations` 获取；国际区 PAT 在 `qoder.com/account/integrations` 获取。`dt-`、`drt-`、`jt-`、`jrt-` 是登录态或 job token，不是可直接提交到 **Add PAT** 的 PAT。

> **默认密码是 `admin`，强烈建议第一次登录后立即修改。**

### 启动 / Start

```bash
uv run qoder2api
```

服务默认运行在 `http://127.0.0.1:5050/`。

| 路径 | 说明 |
|------|------|
| `/` | Landing Page |
| `/console` | 管理控制台 |
| `/documents` | 文档站 / Wiki |
| `/v1/chat/completions` | OpenAI 兼容 API |
| `/v1/models` | OpenAI 兼容模型列表 |

控制台的 **模型页** 默认按当前区域显示模型目录。测速使用当前激活账号发送短请求，会消耗少量 Qoder 额度；测速事件会单独标记，不会混入生产流量健康统计。

### 第一次 API 调用 / First API Call

在控制台导入账号后：

```bash
curl http://127.0.0.1:5050/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "lite",
    "messages": [{ "role": "user", "content": "Hello" }],
    "stream": false
  }'
```

## 环境变量 / Environment Variables

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `QODER_HOST` | 服务绑定地址 | `127.0.0.1` |
| `QODER_PORT` | 服务端口 | `5050` |
| `QODER_ADMIN_PASSWORD` | 管理员密码（覆盖 SQLite 存储值） | `admin` |
| `QODER_PROXY` | 出站代理地址 | 空 |
| `QODER_REGION` | Qoder 区域：`global` 或 `cn` | `global` |
| `QODER_ENABLE_DOCUMENTS` | 是否启用文档页 | `1` |
| `QODER_ENABLE_LANDING` | 是否启用 Landing Page | `1` |
| `QODER_PAT` | 首次启动时自动导入的 PAT | 空 |

## 项目结构 / Project Structure

```
├── src/qoder2api/          # Python 后端
│   ├── app.py              # FastAPI 路由
│   ├── accounts.py         # SQLite 账号管理
│   ├── auth.py             # Qoder 鉴权与签名
│   ├── bridge.py           # OpenAI 兼容响应转换
│   ├── models.py           # 模型目录与测速
│   ├── telemetry.py        # 请求耗时与近期访问统计
│   ├── config.py           # 配置读写
│   ├── database.py         # SQLite schema
│   ├── env.py              # 环境变量加载
│   └── static/             # 前端构建产物
├── frontend/               # React 前端源码
│   ├── src/App.tsx         # 管理控制台
│   ├── src/docs-main.tsx   # 文档站
│   ├── src/landing-main.tsx# Landing Page
│   └── src/docs/           # 中英文 Markdown 文档
├── .env.example            # 环境变量模板
└── pyproject.toml          # 项目配置
```

## License

MIT
