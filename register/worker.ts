import { existsSync, readFileSync } from 'node:fs';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { log } from './utils/logger';
import { WorkerLifecycleController } from './utils/workerLifecycle';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_CONFIG_FILE = path.join(__dirname, 'config.json');
const TEMPLATE_CONFIG_FILE = path.join(__dirname, 'config.json.example');
const DEFAULT_POLL_INTERVAL_SECONDS = 5;
const DEFAULT_CALLBACK_TIMEOUT_SECONDS = 180;
const DEFAULT_PURE_LOGIN_TIMEOUT_SECONDS = 300;
const EXIT_CODE_SIGINT = 130;
const EXIT_CODE_SIGTERM = 143;

const workerLifecycle = new WorkerLifecycleController();

interface WorkerAppConfig {
  backendUrl?: string;
  pollIntervalSeconds?: number;
  autoStart?: boolean;
  count?: number;
  browserMode?: string;
  locale?: string;
  languages?: string[] | string;
  proxy?: string | { server: string; username?: string; password?: string; bypass?: string } | null;
  debugMode?: boolean;
  debugDir?: string;
  mailApiBase?: string;
  mailApiKey?: string;
  pythonExecutable?: string;
  pureLoginScript?: string;
  pureLoginTimeoutSeconds?: number;
}

interface WorkerClaimPayload {
  session_id: string;
  auth_url?: string | null;
  requested_count: number;
  success_count: number;
  failure_count: number;
  current_index: number;
  current_email?: string | null;
}

interface CliOptions {
  backendUrl?: string;
  sessionId?: string;
  configFile?: string;
  pollIntervalSeconds: number;
  once: boolean;
}

interface StoredAccountRecord {
  email: string;
  password: string;
  cookies?: unknown[];
}

interface PureLoginReadyPayload {
  loginUrl: string;
  workDir: string;
  summaryPath: string;
  machineId: string;
  machineToken: string;
  machineType: string;
}

interface PureLoginHandle {
  ready: Promise<PureLoginReadyPayload>;
  result: Promise<Record<string, unknown>>;
  terminate: () => void;
}

interface ManagedProcessHandle {
  result: Promise<void>;
  terminate: () => void;
}

interface RegistrationSessionResponse {
  done?: boolean;
  status?: string;
}

function isPureLoginSummaryNoise(line: string): boolean {
  const normalized = line.trim();
  if (!normalized) {
    return false;
  }
  if (
    normalized.startsWith('LOGIN_URL=')
    || normalized.startsWith('WORK_DIR=')
    || normalized.startsWith('MACHINE_TOKEN=')
    || normalized.startsWith('MACHINE_TYPE=')
    || normalized.startsWith('LOGIN_MACHINE_ID=')
  ) {
    return false;
  }
  if (
    normalized === '{'
    || normalized === '}'
    || normalized === '['
    || normalized === ']'
    || normalized === '},'
    || normalized === '],'
  ) {
    return true;
  }
  return /^"[^"]+":\s*.+,?$/.test(normalized);
}

function stripJsonComments(input: string): string {
  let result = '';
  let inString = false;
  let stringQuote = '"';
  let escaped = false;
  let inLineComment = false;
  let inBlockComment = false;

  for (let index = 0; index < input.length; index += 1) {
    const char = input[index];
    const next = input[index + 1];

    if (inLineComment) {
      if (char === '\n') {
        inLineComment = false;
        result += char;
      }
      continue;
    }

    if (inBlockComment) {
      if (char === '*' && next === '/') {
        inBlockComment = false;
        index += 1;
      }
      continue;
    }

    if (inString) {
      result += char;
      if (escaped) {
        escaped = false;
        continue;
      }
      if (char === '\\') {
        escaped = true;
        continue;
      }
      if (char === stringQuote) {
        inString = false;
      }
      continue;
    }

    if (char === '"' || char === "'") {
      inString = true;
      stringQuote = char;
      result += char;
      continue;
    }

    if (char === '/' && next === '/') {
      inLineComment = true;
      index += 1;
      continue;
    }

    if (char === '/' && next === '*') {
      inBlockComment = true;
      index += 1;
      continue;
    }

    result += char;
  }

  return result;
}

function parseCliOptions(args: string[]): CliOptions {
  const options: CliOptions = {
    pollIntervalSeconds: DEFAULT_POLL_INTERVAL_SECONDS,
    once: false,
  };

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === '--backend-url') {
      options.backendUrl = args[index + 1] || '';
      index += 1;
      continue;
    }
    if (arg === '--session-id') {
      options.sessionId = args[index + 1] || '';
      index += 1;
      continue;
    }
    if (arg === '--config') {
      options.configFile = args[index + 1] || '';
      index += 1;
      continue;
    }
    if (arg === '--poll-interval') {
      const raw = args[index + 1] || '';
      options.pollIntervalSeconds = Number.parseInt(raw, 10) || DEFAULT_POLL_INTERVAL_SECONDS;
      index += 1;
      continue;
    }
    if (arg === '--once') {
      options.once = true;
    }
  }

  return options;
}

function resolveConfigFile(explicit?: string): string {
  const candidate = explicit || process.env.QODERREG_CONFIG_FILE || '';
  if (!candidate) {
    return DEFAULT_CONFIG_FILE;
  }
  return path.isAbsolute(candidate) ? candidate : path.resolve(process.cwd(), candidate);
}

function loadConfig(explicit?: string): WorkerAppConfig {
  const configFile = resolveConfigFile(explicit);
  if (!existsSync(configFile)) {
    return {};
  }
  const raw = readFileSync(configFile, 'utf8');
  try {
    const parsed = JSON.parse(stripJsonComments(raw)) as WorkerAppConfig;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (error) {
    const details = error instanceof Error ? error.message : String(error);
    throw new Error(
      `配置文件解析失败: ${configFile}。请检查 JSON 格式；首次使用请从 ${TEMPLATE_CONFIG_FILE} 复制生成 config.json。原始错误: ${details}`,
    );
  }
}

function resolveBackendUrl(options: CliOptions, config: WorkerAppConfig): string {
  const candidate = options.backendUrl || process.env.QODERREG_BACKEND_URL || config.backendUrl || '';
  return candidate.trim().replace(/\/+$/, '');
}

function resolveAutoStart(config: WorkerAppConfig): boolean {
  return config.autoStart !== false;
}

function resolvePythonExecutable(config: WorkerAppConfig): string {
  const candidate = (config.pythonExecutable || process.env.QODERREG_PYTHON || '').trim();
  return candidate || 'python3';
}

function resolvePureLoginScript(config: WorkerAppConfig): string {
  const candidates = [
    (config.pureLoginScript || '').trim(),
    (process.env.QODERREG_PURE_LOGIN_SCRIPT || '').trim(),
    path.resolve(__dirname, '..', '..', '..', 'machine', 'qoder-machine', 'xieyi', 'qoder_pure_login.py'),
    '/root/workdir/qoder/machine/qoder-machine/xieyi/qoder_pure_login.py',
  ].filter(Boolean);

  for (const candidate of candidates) {
    const resolved = path.isAbsolute(candidate) ? candidate : path.resolve(process.cwd(), candidate);
    if (existsSync(resolved)) {
      return resolved;
    }
  }
  throw new Error(
    '找不到 qoder_pure_login.py，请在 config.json.pureLoginScript 或 QODERREG_PURE_LOGIN_SCRIPT 中指定脚本路径',
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function apiRequest<T>(backendUrl: string, pathname: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${backendUrl}${pathname}`, {
    ...init,
    headers: {
      'content-type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = payload?.error?.message || payload?.message || `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return payload as T;
}

async function claimSession(backendUrl: string, sessionId?: string): Promise<WorkerClaimPayload | null> {
  const pathname = sessionId
    ? `/webui/api/registration/sessions/${sessionId}/worker/claim`
    : '/webui/api/registration/worker/claim';
  const payload = await apiRequest<{ session: WorkerClaimPayload | null }>(backendUrl, pathname, {
    method: 'POST',
    body: JSON.stringify({}),
  });
  return payload.session;
}

async function cancelSession(backendUrl: string, sessionId: string): Promise<void> {
  await apiRequest<Record<string, unknown>>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}/cancel`,
    {
      method: 'POST',
      body: JSON.stringify({}),
    },
  );
}

async function getSessionSnapshot(
  backendUrl: string,
  sessionId: string,
): Promise<RegistrationSessionResponse> {
  return apiRequest<RegistrationSessionResponse>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}`,
  );
}

async function startRegistration(backendUrl: string, requestedCount: number): Promise<string | null> {
  const payload = await apiRequest<{ session?: { session_id?: string } | null }>(
    backendUrl,
    '/webui/api/registration/start',
    {
      method: 'POST',
      body: JSON.stringify({
        requested_count: Math.max(1, Number.parseInt(String(requestedCount || 1), 10) || 1),
      }),
    },
  );
  return payload.session?.session_id || null;
}

async function sendProgress(
  backendUrl: string,
  sessionId: string,
  line: string,
  status?: string,
  currentEmail?: string,
  authUrl?: string,
): Promise<void> {
  emitLocalProgressLog(line);
  if (!workerLifecycle.canSendSessionUpdate(sessionId)) {
    return;
  }
  await apiRequest<Record<string, unknown>>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}/worker/progress`,
    {
      method: 'POST',
      body: JSON.stringify({
        line,
        status,
        current_email: currentEmail,
        auth_url: authUrl,
      }),
    },
  );
}

function emitLocalProgressLog(line: string): void {
  const text = line.trim();
  if (!text) {
    return;
  }

  if (/\[error\]/i.test(text) || /\[stderr\]/i.test(text)) {
    log.error(text);
    return;
  }
  if (/\[warn\]/i.test(text)) {
    log.warn(text);
    return;
  }
  log.info(text);
}

async function sendComplete(
  backendUrl: string,
  sessionId: string,
  email: string,
  password: string,
  summary?: Record<string, unknown>,
): Promise<void> {
  if (!workerLifecycle.canSendSessionUpdate(sessionId)) {
    return;
  }
  const payload = await apiRequest<RegistrationSessionResponse>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}/worker/complete`,
    {
      method: 'POST',
      body: JSON.stringify({ email, password, summary }),
    },
  );
  if (payload.done) {
    workerLifecycle.clearSession(sessionId);
  }
}

function parseLastJsonObject(text: string): Record<string, unknown> | null {
  const decoder = JSON;
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  for (let index = trimmed.length - 1; index >= 0; index -= 1) {
    if (trimmed[index] !== '{') {
      continue;
    }
    const candidate = trimmed.slice(index);
    try {
      const payload = decoder.parse(candidate) as Record<string, unknown>;
      if (payload && typeof payload === 'object') {
        return payload;
      }
    } catch {
      // noop
    }
  }
  return null;
}

function startPureLogin(
  config: WorkerAppConfig,
  tempDir: string,
  onLine: (line: string) => Promise<void>,
): PureLoginHandle {
  const pureLoginScript = resolvePureLoginScript(config);
  const pythonExecutable = resolvePythonExecutable(config);
  const outputRoot = path.join(tempDir, 'pure-login-output');
  const timeoutSeconds = Math.max(
    DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    Number.parseInt(String(config.pureLoginTimeoutSeconds ?? DEFAULT_PURE_LOGIN_TIMEOUT_SECONDS), 10)
      || DEFAULT_PURE_LOGIN_TIMEOUT_SECONDS,
  );
  const child = spawn(
    pythonExecutable,
    [
      pureLoginScript,
      '--count',
      '1',
      '--timeout',
      String(timeoutSeconds),
      '--json',
      '--output-root',
      outputRoot,
    ],
    {
      cwd: path.dirname(pureLoginScript),
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );

  let stdoutBuffer = '';
  let stderrBuffer = '';
  let combinedOutput = '';
  let loginUrl = '';
  let workDir = '';
  let machineId = '';
  let machineToken = '';
  let machineType = '';
  let readyResolved = false;
  let terminatedByWorker = false;

  let readyResolve!: (payload: PureLoginReadyPayload) => void;
  let readyReject!: (error: Error) => void;
  const ready = new Promise<PureLoginReadyPayload>((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });

  const maybeResolveReady = (): void => {
    if (readyResolved || !loginUrl || !workDir) {
      return;
    }
    readyResolved = true;
    readyResolve({
      loginUrl,
      workDir,
      summaryPath: path.join(path.dirname(workDir), 'summary.json'),
      machineId,
      machineToken,
      machineType,
    });
  };

  const handleLine = async (prefix: string, rawLine: string): Promise<void> => {
    const normalized = rawLine.trim();
    if (!normalized) {
      return;
    }
    combinedOutput += `${normalized}\n`;
    if (!prefix) {
      if (normalized.startsWith('LOGIN_URL=')) {
        loginUrl = normalized.slice('LOGIN_URL='.length).trim();
      } else if (normalized.startsWith('WORK_DIR=')) {
        workDir = normalized.slice('WORK_DIR='.length).trim();
      } else if (normalized.startsWith('LOGIN_MACHINE_ID=')) {
        machineId = normalized.slice('LOGIN_MACHINE_ID='.length).trim();
      } else if (normalized.startsWith('MACHINE_TOKEN=')) {
        machineToken = normalized.slice('MACHINE_TOKEN='.length).trim();
      } else if (normalized.startsWith('MACHINE_TYPE=')) {
        machineType = normalized.slice('MACHINE_TYPE='.length).trim();
      }
      maybeResolveReady();
    }
    if (!prefix && isPureLoginSummaryNoise(normalized)) {
      return;
    }
    await onLine(`${prefix}${normalized}`);
  };

  const pump = (prefix: string, stream: NodeJS.ReadableStream | null, currentBufferRef: { value: string }): void => {
    stream?.on('data', (chunk: Buffer) => {
      void (async () => {
        const merged = currentBufferRef.value + chunk.toString('utf8');
        const lines = merged.split(/\r?\n/);
        currentBufferRef.value = lines.pop() || '';
        for (const line of lines) {
          await handleLine(prefix, line);
        }
      })().catch((error) => {
        readyReject(error instanceof Error ? error : new Error(String(error)));
      });
    });
  };

  pump('', child.stdout, { get value() { return stdoutBuffer; }, set value(v: string) { stdoutBuffer = v; } });
  pump('[pure-login][stderr] ', child.stderr, { get value() { return stderrBuffer; }, set value(v: string) { stderrBuffer = v; } });

  const result = new Promise<Record<string, unknown>>((resolve, reject) => {
    child.on('error', (error) => {
      if (!readyResolved) {
        readyReject(error);
      }
      reject(error);
    });
    child.on('close', (code, signal) => {
      void (async () => {
        if (stdoutBuffer.trim()) {
          await handleLine('', stdoutBuffer);
        }
        if (stderrBuffer.trim()) {
          await handleLine('[pure-login][stderr] ', stderrBuffer);
        }
        if (terminatedByWorker && signal === 'SIGTERM') {
          reject(new Error('qoder_pure_login.py 已被 Worker 主动终止'));
          return;
        }
        if (code !== 0) {
          const signalSuffix = signal ? `, signal=${signal}` : '';
          const error = new Error(`qoder_pure_login.py 退出码异常: ${code ?? -1}${signalSuffix}`);
          if (!readyResolved) {
            readyReject(error);
          }
          reject(error);
          return;
        }
        const summaryPath = workDir ? path.join(path.dirname(workDir), 'summary.json') : '';
        if (summaryPath && existsSync(summaryPath)) {
          const raw = await readFile(summaryPath, 'utf8');
          resolve(JSON.parse(raw) as Record<string, unknown>);
          return;
        }
        const fallback = parseLastJsonObject(combinedOutput);
        if (fallback) {
          resolve(fallback);
          return;
        }
        reject(new Error('纯协议登录结束后未找到 summary.json'));
      })().catch((error) => {
        if (!readyResolved) {
          readyReject(error instanceof Error ? error : new Error(String(error)));
        }
        reject(error instanceof Error ? error : new Error(String(error)));
      });
    });
  });

  const readyTimeout = setTimeout(() => {
    if (!readyResolved) {
      readyReject(new Error('等待 qoder_pure_login.py 输出 LOGIN_URL 超时'));
      child.kill('SIGTERM');
    }
  }, Math.min(timeoutSeconds, 120) * 1000);
  void ready.finally(() => clearTimeout(readyTimeout));
  void result.finally(() => clearTimeout(readyTimeout));

  return {
    ready,
    result,
    terminate: () => {
      if (!child.killed) {
        terminatedByWorker = true;
        child.kill('SIGTERM');
      }
    },
  };
}

async function sendFailure(backendUrl: string, sessionId: string, message: string): Promise<void> {
  if (!workerLifecycle.canSendSessionUpdate(sessionId)) {
    return;
  }
  const payload = await apiRequest<RegistrationSessionResponse>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}/worker/fail`,
    {
      method: 'POST',
      body: JSON.stringify({ message }),
    },
  );
  if (payload.done) {
    workerLifecycle.clearSession(sessionId);
  }
}

async function writeRuntimeConfig(
  claim: WorkerClaimPayload,
  tempDir: string,
  appConfig: WorkerAppConfig,
): Promise<{ configPath: string; outputFile: string }> {
  const outputFile = path.join(tempDir, 'accounts.json');
  const configPath = path.join(tempDir, 'worker-config.json');
  const hasLocalProxy = Object.prototype.hasOwnProperty.call(appConfig, 'proxy');
  const mergedProxy = hasLocalProxy ? (appConfig.proxy ?? null) : null;
  const payload = {
    count: 1,
    browserMode: (appConfig.browserMode || 'headless').trim() || 'headless',
    locale: (appConfig.locale || 'zh-CN').trim() || 'zh-CN',
    languages: Array.isArray(appConfig.languages)
      ? appConfig.languages
      : typeof appConfig.languages === 'string'
        ? appConfig.languages
        : ['zh-CN', 'zh'],
    proxy: mergedProxy,
    debugMode: Boolean(appConfig.debugMode),
    debugDir: typeof appConfig.debugDir === 'string' ? appConfig.debugDir : 'debug',
    mailApiBase: (appConfig.mailApiBase || 'https://maliapi.215.im/v1').trim()
      || 'https://maliapi.215.im/v1',
    mailApiKey: appConfig.mailApiKey || '',
    outputFile,
  };
  await writeFile(configPath, JSON.stringify(payload, null, 2), 'utf8');
  return { configPath, outputFile };
}

async function readLatestAccount(outputFile: string): Promise<StoredAccountRecord> {
  const raw = await readFile(outputFile, 'utf8');
  const payload = JSON.parse(raw) as StoredAccountRecord[];
  if (!Array.isArray(payload) || payload.length === 0) {
    throw new Error('注册结束后未发现新账号记录');
  }
  const latest = payload[payload.length - 1];
  if (!latest?.email) {
    throw new Error('账号记录缺少 email');
  }
  return latest;
}

async function runProcess(
  command: string,
  args: string[],
  options: {
    cwd: string;
    onLine: (line: string) => Promise<void>;
  },
): Promise<void> {
  const { cwd, onLine } = options;
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let stdoutBuffer = '';
    let stderrBuffer = '';

    const handleChunk = async (prefix: string, chunk: Buffer, current: string): Promise<string> => {
      const merged = current + chunk.toString('utf8');
      const lines = merged.split(/\r?\n/);
      const remain = lines.pop() || '';
      for (const line of lines) {
        if (line.trim()) {
          await onLine(`${prefix}${line}`);
        }
      }
      return remain;
    };

    child.stdout?.on('data', (chunk: Buffer) => {
      void (async () => {
        stdoutBuffer = await handleChunk('', chunk, stdoutBuffer);
      })().catch(reject);
    });

    child.stderr?.on('data', (chunk: Buffer) => {
      void (async () => {
        stderrBuffer = await handleChunk('[stderr] ', chunk, stderrBuffer);
      })().catch(reject);
    });

    child.on('error', reject);
    child.on('close', (code) => {
      void (async () => {
        if (stdoutBuffer.trim()) {
          await onLine(stdoutBuffer.trim());
        }
        if (stderrBuffer.trim()) {
          await onLine(`[stderr] ${stderrBuffer.trim()}`);
        }
        if (code === 0) {
          resolve();
          return;
        }
        reject(new Error(`子进程退出码异常: ${code ?? -1}`));
      })().catch(reject);
    });
  });
}

function startManagedProcess(
  command: string,
  args: string[],
  options: {
    cwd: string;
    onLine: (line: string) => Promise<void>;
  },
): ManagedProcessHandle {
  const { cwd, onLine } = options;
  const child = spawn(command, args, {
    cwd,
    env: process.env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let stdoutBuffer = '';
  let stderrBuffer = '';
  let terminatedByWorker = false;

  const handleChunk = async (prefix: string, chunk: Buffer, current: string): Promise<string> => {
    const merged = current + chunk.toString('utf8');
    const lines = merged.split(/\r?\n/);
    const remain = lines.pop() || '';
    for (const line of lines) {
      if (line.trim()) {
        await onLine(`${prefix}${line}`);
      }
    }
    return remain;
  };

  child.stdout?.on('data', (chunk: Buffer) => {
    void (async () => {
      stdoutBuffer = await handleChunk('', chunk, stdoutBuffer);
    })().catch(() => undefined);
  });

  child.stderr?.on('data', (chunk: Buffer) => {
    void (async () => {
      stderrBuffer = await handleChunk('[stderr] ', chunk, stderrBuffer);
    })().catch(() => undefined);
  });

  const result = new Promise<void>((resolve, reject) => {
    child.on('error', reject);
    child.on('close', (code, signal) => {
      void (async () => {
        if (stdoutBuffer.trim()) {
          await onLine(stdoutBuffer.trim());
        }
        if (stderrBuffer.trim()) {
          await onLine(`[stderr] ${stderrBuffer.trim()}`);
        }
        if (terminatedByWorker && signal === 'SIGTERM') {
          resolve();
          return;
        }
        if (code === 0) {
          resolve();
          return;
        }
        const signalSuffix = signal ? `, signal=${signal}` : '';
        reject(new Error(`子进程退出码异常: ${code ?? -1}${signalSuffix}`));
      })().catch(reject);
    });
  });

  return {
    result,
    terminate: () => {
      if (!child.killed) {
        terminatedByWorker = true;
        child.kill('SIGTERM');
      }
    },
  };
}

async function runClaim(
  backendUrl: string,
  claim: WorkerClaimPayload,
  appConfig: WorkerAppConfig,
): Promise<void> {
  workerLifecycle.trackSession(claim.session_id);
  const tempDir = await mkdtemp(path.join(os.tmpdir(), 'qoder2api-register-'));
  const tsxPath = path.join(__dirname, 'node_modules', '.bin', 'tsx');
  if (!existsSync(tsxPath)) {
    throw new Error(`找不到 tsx: ${tsxPath}，请先在 register/ 下执行 npm ci`);
  }

  try {
    const { configPath, outputFile } = await writeRuntimeConfig(claim, tempDir, appConfig);
    await sendProgress(
      backendUrl,
      claim.session_id,
      `开始处理第 ${claim.current_index}/${claim.requested_count} 个账号（使用本地 worker 配置执行注册/OAuth）。`,
      'registering',
      undefined,
    );
    await runProcess(tsxPath, ['register.ts', '--config', configPath], {
      cwd: __dirname,
      onLine: (line) => sendProgress(backendUrl, claim.session_id, `[register] ${line}`, 'registering'),
    });

    const account = await readLatestAccount(outputFile);
    await sendProgress(
      backendUrl,
      claim.session_id,
      `注册成功：${account.email}`,
      'preparing_oauth',
      account.email,
    );
    const pureLogin = startPureLogin(
      appConfig,
      tempDir,
      (line) => sendProgress(backendUrl, claim.session_id, line, 'preparing_oauth', account.email),
    );
    const unregisterPureLoginTerminator = workerLifecycle.registerTerminator(() => {
      pureLogin.terminate();
    });

    try {
      const ready = await pureLogin.ready;
      await sendProgress(
        backendUrl,
        claim.session_id,
        `已生成 OAuth 链接：${ready.loginUrl}`,
        'oauth_signing_in',
        account.email,
        ready.loginUrl,
      );
      const oauthProcess = startManagedProcess(
        tsxPath,
        [
          'login.ts',
          account.email,
          '--target-url',
          ready.loginUrl,
          '--callback-timeout',
          String(DEFAULT_CALLBACK_TIMEOUT_SECONDS),
          '--config',
          configPath,
        ],
        {
          cwd: __dirname,
          onLine: (line) => sendProgress(backendUrl, claim.session_id, `[oauth] ${line}`, 'oauth_signing_in', account.email, ready.loginUrl),
        },
      );
      const unregisterOauthTerminator = workerLifecycle.registerTerminator(() => {
        oauthProcess.terminate();
      });
      let summary: Record<string, unknown>;
      try {
        summary = await pureLogin.result;
      } finally {
        unregisterOauthTerminator();
        oauthProcess.terminate();
        await oauthProcess.result.catch(() => undefined);
      }
      await sendComplete(backendUrl, claim.session_id, account.email, account.password || '', summary);
    } catch (error) {
      pureLogin.terminate();
      await pureLogin.result.catch(() => undefined);
      throw error;
    } finally {
      unregisterPureLoginTerminator();
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await sendFailure(backendUrl, claim.session_id, message).catch(() => undefined);
    throw error;
  } finally {
    await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
  }
}

function printUsage(): void {
  console.log('Usage: npx tsx worker.ts --backend-url http://127.0.0.1:28001 [--session-id ID] [--poll-interval 5] [--once] [--config PATH]');
}

async function shutdownWorker(
  backendUrl: string,
  options: { reason: string },
): Promise<void> {
  const { reason } = options;
  await workerLifecycle.shutdown(async (sessionId) => {
    const snapshot = await getSessionSnapshot(backendUrl, sessionId).catch(() => null);
    if (snapshot?.done) {
      return;
    }
    log.warn(`Worker 即将退出，自动结束注册会话 ${sessionId}: ${reason}`);
    await cancelSession(backendUrl, sessionId);
  });
}

function installSignalHandlers(backendUrl: string): void {
  const handleSignal = (signal: NodeJS.Signals, exitCode: number): void => {
    void (async () => {
      try {
        await shutdownWorker(backendUrl, { reason: `收到 ${signal}` });
      } catch (error) {
        log.warn('退出前自动结束注册会话失败', {
          error: error instanceof Error ? error.message : String(error),
          signal,
        });
      } finally {
        process.exit(exitCode);
      }
    })();
  };

  process.once('SIGINT', () => {
    handleSignal('SIGINT', EXIT_CODE_SIGINT);
  });
  process.once('SIGTERM', () => {
    handleSignal('SIGTERM', EXIT_CODE_SIGTERM);
  });
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes('--help') || args.includes('-h')) {
    printUsage();
    return;
  }

  const options = parseCliOptions(args);
  const appConfig = loadConfig(options.configFile);
  const backendUrl = resolveBackendUrl(options, appConfig);
  if (!backendUrl) {
    throw new Error('缺少 qoder2api 后端地址，请通过 --backend-url、QODERREG_BACKEND_URL 或 config.json.backendUrl 提供');
  }

  workerLifecycle.trackSession(options.sessionId);
  installSignalHandlers(backendUrl);
  const pollIntervalSeconds = options.pollIntervalSeconds || appConfig.pollIntervalSeconds || DEFAULT_POLL_INTERVAL_SECONDS;
  const autoStart = resolveAutoStart(appConfig);
  let autoStarted = false;
  log.info('========== qoder2api 注册 Worker ==========');
  log.info(`backendUrl: ${backendUrl}`);
  log.info(`sessionId: ${options.sessionId || 'auto'}`);
  log.info(`pollInterval: ${pollIntervalSeconds}s`);
  log.info(`mode: ${options.once ? 'once' : 'watch'}`);
  log.info(`autoStart: ${autoStart}`);
  log.info('==========================================');

  do {
    let claim = await claimSession(backendUrl, options.sessionId);
    if (!claim && !options.sessionId && autoStart && !autoStarted) {
      log.info('当前没有待处理注册任务，准备自动向 qoder2api 创建任务。');
      const startedSessionId = await startRegistration(
        backendUrl,
        Math.max(1, Number.parseInt(String(appConfig.count ?? 1), 10) || 1),
      );
      autoStarted = true;
      if (startedSessionId) {
        workerLifecycle.trackSession(startedSessionId);
        log.info(`已自动创建注册会话: ${startedSessionId}`);
        claim = await claimSession(backendUrl, startedSessionId);
      }
    }
    if (!claim) {
      if (options.once) {
        log.info('当前没有可处理的注册任务，已退出。');
        return;
      }
      await sleep(pollIntervalSeconds * 1000);
      continue;
    }

    log.info(`已领取会话 ${claim.session_id}，开始处理第 ${claim.current_index}/${claim.requested_count} 个账号`);
    try {
      await runClaim(backendUrl, claim, appConfig);
      log.info(`会话 ${claim.session_id} 当前轮次已完成。`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      log.error('注册 Worker 执行失败', { error: message, sessionId: claim.session_id });
      if (options.once) {
        throw error;
      }
      await sleep(pollIntervalSeconds * 1000);
    }
    if (options.once) {
      return;
    }
  } while (true);
}

void main().catch((error) => {
  void (async () => {
    const message = error instanceof Error ? error.stack || error.message : String(error);
    log.error('注册 Worker 异常退出', { error: message });
    try {
      const options = parseCliOptions(process.argv.slice(2));
      const appConfig = loadConfig(options.configFile);
      const backendUrl = resolveBackendUrl(options, appConfig);
      if (backendUrl) {
        try {
          await shutdownWorker(backendUrl, { reason: '发生未处理错误' });
        } catch (cleanupError) {
          log.warn('异常退出前自动结束注册会话失败', {
            error: cleanupError instanceof Error ? cleanupError.message : String(cleanupError),
          });
        }
      }
    } catch (cleanupBootstrapError) {
      log.warn('异常退出前初始化清理上下文失败', {
        error: cleanupBootstrapError instanceof Error ? cleanupBootstrapError.message : String(cleanupBootstrapError),
      });
    }
    process.exitCode = 1;
  })();
});
