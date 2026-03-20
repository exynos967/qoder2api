# qoder2api

`qoder2api` 将 **Qoder** 暴露为一个最小可用的 **OpenAI Chat Completions 兼容接口**。

当前实现已经改成 **独立程序**：

- 不依赖 `qodercli` 子进程
- 直接完成 Qoder browser/device 登录流
- 直接读取 / 写入 `.qoder/.auth/*`
- 直接向 Qoder 上游发起鉴权和推理请求
- 提供 **账号池**：每个账号对应一个独立受管 `.qoder` 目录
- WebUI 沿用 `iflow2api` 风格，改造成 Qoder 账号池后台

## 当前能力

- `GET /health`
- `GET /webui`
- `GET /webui/api/overview`
- `GET /webui/api/logs`
- `GET /webui/api/accounts`
- `PATCH /webui/api/accounts/{account_id}/enabled`
- `PATCH /webui/api/accounts/{account_id}/label`
- `DELETE /webui/api/accounts/{account_id}`
- `POST /webui/api/accounts/{account_id}/test`
- `GET /auth/status`
- `POST /auth/browser/start`
- `GET /auth/browser/sessions/{session_id}`
- `POST /auth/browser/sessions/{session_id}/input`
- `POST /auth/browser/sessions/{session_id}/cancel`
- `GET /v1/models`
- `POST /v1/chat/completions`

## 已知限制

- 目前只做 **OpenAI Chat Completions**，未实现 Anthropic `/v1/messages`
- `tool_calls` 只会作为上下文文本透传，不做真正函数调用
- 当前独立模式 **不支持本地图片附件上传**
- 流式输出是对 Qoder 上游事件的保守兼容解析，已做本地单测，但未在当前受限网络环境中做完整在线验证
- 登录使用 **browser/device flow** 自动轮询，不再需要 PTY/TTY 驱动

## 路径发现规则

默认按以下顺序查找 `.qoder`：

1. `QODER2API_QODER_HOME`
2. 当前工作目录向上查找最近的 `.qoder`
3. `~/.qoder`

默认推理入口：

1. `QODER2API_QODER_INFER_ENDPOINT`
2. `<qoder_home>/.config.json` 中的 `preferredInferenceNode.endpoint`
3. `https://api3.qoder.sh`

其他默认入口：

- `QODER2API_QODER_OPENAPI_ENDPOINT` → `https://openapi.qoder.sh`
- `QODER2API_QODER_CENTER_ENDPOINT` → `https://center.qoder.sh`
- `QODER2API_QODER_WEB_ENDPOINT` → `https://www.qoder.com`

## 启动

```bash
cd qoder2api
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m qoder2api
```

默认监听：

```text
http://0.0.0.0:28001
```

WebUI：

```text
http://127.0.0.1:28001/webui
```

## 账号池目录结构

默认会在 `QODER2API_WORKSPACE/.qoder2api/` 下维护账号池：

```text
.qoder2api/
├── accounts.json
└── accounts/
    └── <account-id>/
        └── .qoder/
            ├── .auth/
            └── .config.json
```

说明：

- 新账号登录时，会先复制基础 `.qoder` 中必要的配置与模型清单
- 登录成功后，该账号会正式进入池中并可参与轮询
- 删除账号只会删除其受管目录，不会删宿主机原始 `.qoder`

## 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `QODER2API_HOST` | 监听地址 | `0.0.0.0` |
| `QODER2API_PORT` | 监听端口 | `28001` |
| `QODER2API_QODER_HOME` | `.qoder` 路径 | 自动发现 |
| `QODER2API_WORKSPACE` | 工作目录 | `.qoder` 的父目录 |
| `QODER2API_QODER_INFER_ENDPOINT` | 推理入口 | 自动发现 |
| `QODER2API_QODER_OPENAPI_ENDPOINT` | OpenAPI 入口 | `https://openapi.qoder.sh` |
| `QODER2API_QODER_CENTER_ENDPOINT` | Center 入口 | `https://center.qoder.sh` |
| `QODER2API_QODER_WEB_ENDPOINT` | Web 登录页入口 | `https://www.qoder.com` |
| `QODER2API_CLIENT_VERSION` | 上游鉴权使用的 client version | `0.1.32` |
| `QODER2API_DEFAULT_MODEL` | 默认模型 key | `auto` |
| `QODER2API_REQUEST_TIMEOUT_SECONDS` | 单次请求超时 | `600` |

## 调用示例

### 列模型

```bash
curl http://127.0.0.1:28001/v1/models
```

### WebUI 浏览器登录

1. 打开 `http://127.0.0.1:28001/webui`
2. 在“账号池”页填写可选标签，点击“网页登录并入池”
3. 页面会弹出 Qoder 官方登录页
4. 浏览器完成授权后，后台自动轮询并把登录态写入该账号独立的受管 `.qoder`

### 非流式

```bash
curl http://127.0.0.1:28001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "system", "content": "你是一个简洁助手"},
      {"role": "user", "content": "只回复 ok"}
    ]
  }'
```

### 流式

```bash
curl http://127.0.0.1:28001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "auto",
    "stream": true,
    "messages": [
      {"role": "user", "content": "介绍一下你自己"}
    ]
  }'
```

## Docker

```bash
cd qoder2api
docker compose up -d --build
```

## 测试

```bash
pytest -q
ruff check .
python3 -m compileall qoder2api tests
```
