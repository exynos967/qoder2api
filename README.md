# qoder2api

`qoder2api` 将 **Qoder** 暴露为一个最小可用的 **OpenAI Chat Completions 兼容接口**，并提供 WebUI 账号池。

当前仓库只保留 **Docker 部署方式**，并且**默认所有网页登录都走镜像内置的 `qoder-machine/xieyi` 纯协议工作流**。

## 功能

- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`
- WebUI 管理界面：`/webui`
- Qoder 账号池：网页登录入池、启停、改名、删除、测试
- 账号状态检测：展示并刷新 `user/status` / `user/plan`，区分 `Pro Trial`、`Free` 与额度状态
- 默认走镜像内置的 `xieyi` 纯协议登录前置工作流
- 登录态与账号池持久化：`./data`
- GHCR 镜像自动构建：`ghcr.io/exynos967/qoder2api`

## 快速开始

### 1. 启动容器

```bash
git clone https://github.com/exynos967/qoder2api.git
cd qoder2api

docker compose up -d
```

默认 `docker-compose.yml` 会直接拉取：

```text
ghcr.io/exynos967/qoder2api:edge
```

并自动配置：

- `QODER2API_BROWSER_LOGIN_BACKEND=namespaced`
- `QODER2API_NAMESPACED_LOGIN_SCRIPT=/opt/qoder-machine/qoder-machine/xieyi/qoder_pure_login.py`
- `QODER2API_NAMESPACED_LOGIN_OUTPUT_ROOT=/data/.qoder2api/xieyi`

也就是说，**镜像本身已经内置了纯协议工作流**，不再依赖宿主机额外挂载 `qoder-machine` 目录，也不再依赖 `Qoder` 二进制。

### 2. 打开 WebUI

- 地址：`http://localhost:28001/webui`

进入“账号池”页面后，点击“网页登录并入池”即可。

此时每次登录都会按下面链路执行：

1. `qoder2api` 先调用镜像内的 `/opt/qoder-machine/qoder-machine/xieyi/qoder_pure_login.py`
2. `xieyi` 纯协议脚本内部先生成新的设备身份（`machine_token / machine_type / mac / hostname`）
3. 脚本再创建官方网页登录地址，并在登录成功后继续轮询 `user/status`、`user/plan` 直到 Trial 状态收敛
4. WebUI 打开登录页
5. 登录成功后，结果写回当前账号池的 `.qoder/.auth/*`，并额外保存账号套餐状态，支持在账号池里继续“检测状态”

### 3. 使用 API

- OpenAI Base URL：`http://localhost:28001/v1`
- 模型列表：`http://localhost:28001/v1/models`
- Swagger：`http://localhost:28001/docs`

示例：

```bash
curl http://localhost:28001/v1/models
```

## 持久化目录

默认 `docker-compose.yml` 只挂载：

```text
./data -> /data
```

其中包含：

- `/data/.qoder`：基础 Qoder 登录态与模型缓存
- `/data/.qoder2api`：账号池与 WebUI 持久化数据
- `/data`：默认工作区

## 更新服务

```bash
docker compose pull
docker compose up -d
```

## 常用命令

```bash
# 查看日志
docker compose logs -f

# 停止服务
docker compose down

# 强制重建
docker compose up -d --force-recreate
```

## 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `QODER2API_HOST` | 监听地址 | `0.0.0.0` |
| `QODER2API_PORT` | 监听端口 | `28001` |
| `QODER2API_BROWSER_LOGIN_BACKEND` | 登录后端 | `namespaced` |
| `QODER2API_QODER_HOME` | `.qoder` 路径 | `/data/.qoder` |
| `QODER2API_WORKSPACE` | 工作目录 | `/data` |
| `QODER2API_NAMESPACED_LOGIN_SCRIPT` | `xieyi` 纯协议脚本路径 | `/opt/qoder-machine/qoder-machine/xieyi/qoder_pure_login.py` |
| `QODER2API_NAMESPACED_LOGIN_OUTPUT_ROOT` | `xieyi` 输出根目录 | `/data/.qoder2api/xieyi` |
| `QODER2API_QODER_INFER_ENDPOINT` | 推理入口 | 自动发现 |
| `QODER2API_QODER_OPENAPI_ENDPOINT` | OpenAPI 入口 | `https://openapi.qoder.sh` |
| `QODER2API_QODER_CENTER_ENDPOINT` | Center 入口 | `https://center.qoder.sh` |
| `QODER2API_QODER_WEB_ENDPOINT` | Web 登录页入口 | `https://www.qoder.com` |
| `QODER2API_CLIENT_VERSION` | 上游鉴权使用的 client version | `0.1.32` |
| `QODER2API_DEFAULT_MODEL` | 默认模型 key | `auto` |
| `QODER2API_REQUEST_TIMEOUT_SECONDS` | 单次请求超时 | `600` |

## 兼容接口

| 路径 | 说明 |
| --- | --- |
| `/health` | 健康检查 |
| `/v1/models` | 模型列表 |
| `/v1/chat/completions` | OpenAI Chat Completions |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/webui` | Web 管理界面 |

## GitHub Actions 自动构建

仓库内置：

```text
.github/workflows/docker.yml
```

触发规则：

- 推送 `main`：发布 `ghcr.io/exynos967/qoder2api:edge`
- 推送 `v*` tag：发布 semver tag，并自动处理 `latest`
- 支持手动 `workflow_dispatch`

## 说明

- 当前推荐并默认支持的部署方式只有 Docker。
- 如果没有配置账号，服务仍可启动，但实际请求前需要先在 `/webui` 完成网页登录。
- 当前默认登录链路不是本地 direct flow，而是固定先走镜像内置的 `qoder-machine/xieyi` 纯协议设备工作流，再进入标准网页登录轮询。
