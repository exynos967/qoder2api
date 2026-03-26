# qoder2api-register

这是给 `qoder2api` 账号池使用的独立注册/OAuth worker 源码目录。

## 目的
- 保留与 `qoder2api` 主服务解耦的 Node/浏览器自动化逻辑
- 通过 `qoder2api` 提供的注册任务 API 拉取任务并回传结果
- 不参与 Docker 镜像构建

## 常用命令
```bash
cd register
npm ci
npm run typecheck
npm run worker -- --backend-url http://127.0.0.1:28001
```

也可以只跑单次指定会话：
```bash
npm run worker -- --backend-url http://127.0.0.1:28001 --session-id <会话ID> --once
```

## 其他脚本
```bash
npm run register
npm run oauth -- <邮箱> --target-url <OAuth链接>
```

## 配置
默认读取：
- `register/config.json`

也支持：
- `QODERREG_CONFIG_FILE=/abs/path/config.json`
- `--config /abs/path/config.json`

其中 worker 相关配置包含：
- `backendUrl`
- `autoStart`
- `pollIntervalSeconds`
- `count`
- `browserMode`
- `proxy`
- `mailApiBase`
- `mailApiKey`

当 `autoStart=true` 时，只要启动 `worker.ts`，它就会主动连接 `qoder2api`：
1. 先把本地 `config.json` 里的注册相关设置同步到 `qoder2api`
2. 自动调用 `POST /webui/api/registration/start` 创建一轮注册任务
3. 自动领取任务，完成注册 / 登录 / OAuth / 入池

运行时 `register.ts` / `login.ts` 的浏览器、代理、邮箱等配置，会由 `worker.ts` 根据 `config.json` 与 qoder2api 下发的任务动态生成临时配置。
