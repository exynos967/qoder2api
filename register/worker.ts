import { existsSync, readFileSync } from 'node:fs';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { log } from './utils/logger';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_CONFIG_FILE = path.join(__dirname, 'config.json');
const DEFAULT_POLL_INTERVAL_SECONDS = 5;
const DEFAULT_CALLBACK_TIMEOUT_SECONDS = 180;

interface WorkerAppConfig {
  backendUrl?: string;
  pollIntervalSeconds?: number;
  autoStart?: boolean;
  count?: number;
  browserMode?: string;
  proxy?: string | null;
  mailApiBase?: string;
  mailApiKey?: string;
}

type RegisterRuntimeSettings = {
  registration_browser_mode?: string;
  registration_proxy_url?: string;
  registration_mail_api_base?: string;
  registration_mail_api_key?: string;
};

interface WorkerClaimPayload {
  session_id: string;
  auth_url: string;
  requested_count: number;
  success_count: number;
  failure_count: number;
  current_index: number;
  current_email?: string | null;
  settings?: RegisterRuntimeSettings;
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
  const parsed = JSON.parse(stripJsonComments(raw)) as WorkerAppConfig;
  return parsed && typeof parsed === 'object' ? parsed : {};
}

function resolveBackendUrl(options: CliOptions, config: WorkerAppConfig): string {
  const candidate = options.backendUrl || process.env.QODERREG_BACKEND_URL || config.backendUrl || '';
  return candidate.trim().replace(/\/+$/, '');
}

function resolveAutoStart(config: WorkerAppConfig): boolean {
  return config.autoStart !== false;
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

async function syncRuntimeSettings(backendUrl: string, config: WorkerAppConfig): Promise<void> {
  await apiRequest<Record<string, unknown>>(backendUrl, '/webui/api/settings', {
    method: 'PUT',
    body: JSON.stringify({
      registration_count: Math.max(1, Number.parseInt(String(config.count ?? 1), 10) || 1),
      registration_browser_mode: (config.browserMode || 'headless').trim() || 'headless',
      registration_proxy_url: typeof config.proxy === 'string' ? config.proxy : '',
      registration_mail_api_base: config.mailApiBase || 'https://maliapi.215.im/v1',
      registration_mail_api_key: config.mailApiKey || '',
    }),
  });
}

async function startRegistration(backendUrl: string): Promise<string | null> {
  const payload = await apiRequest<{ session?: { session_id?: string } | null }>(
    backendUrl,
    '/webui/api/registration/start',
    {
      method: 'POST',
      body: JSON.stringify({}),
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
): Promise<void> {
  await apiRequest<Record<string, unknown>>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}/worker/progress`,
    {
      method: 'POST',
      body: JSON.stringify({
        line,
        status,
        current_email: currentEmail,
      }),
    },
  );
}

async function sendComplete(
  backendUrl: string,
  sessionId: string,
  email: string,
  password: string,
): Promise<void> {
  await apiRequest<Record<string, unknown>>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}/worker/complete`,
    {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    },
  );
}

async function sendFailure(backendUrl: string, sessionId: string, message: string): Promise<void> {
  await apiRequest<Record<string, unknown>>(
    backendUrl,
    `/webui/api/registration/sessions/${sessionId}/worker/fail`,
    {
      method: 'POST',
      body: JSON.stringify({ message }),
    },
  );
}

async function writeRuntimeConfig(
  claim: WorkerClaimPayload,
  tempDir: string,
): Promise<{ configPath: string; outputFile: string }> {
  const outputFile = path.join(tempDir, 'accounts.json');
  const configPath = path.join(tempDir, 'worker-config.json');
  const settings = claim.settings || {};
  const payload = {
    count: 1,
    browserMode: settings.registration_browser_mode || 'headless',
    locale: 'zh-CN',
    languages: ['zh-CN', 'zh'],
    proxy: settings.registration_proxy_url || null,
    mailApiBase: settings.registration_mail_api_base || 'https://maliapi.215.im/v1',
    mailApiKey: settings.registration_mail_api_key || '',
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

async function runClaim(backendUrl: string, claim: WorkerClaimPayload): Promise<void> {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), 'qoder2api-register-'));
  const tsxPath = path.join(__dirname, 'node_modules', '.bin', 'tsx');
  if (!existsSync(tsxPath)) {
    throw new Error(`找不到 tsx: ${tsxPath}，请先在 register/ 下执行 npm ci`);
  }

  try {
    const { configPath, outputFile } = await writeRuntimeConfig(claim, tempDir);
    await sendProgress(
      backendUrl,
      claim.session_id,
      `开始处理第 ${claim.current_index}/${claim.requested_count} 个账号。`,
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
      'oauth_signing_in',
      account.email,
    );
    await runProcess(
      tsxPath,
      [
        'login.ts',
        account.email,
        '--target-url',
        claim.auth_url,
        '--callback-timeout',
        String(DEFAULT_CALLBACK_TIMEOUT_SECONDS),
        '--config',
        configPath,
      ],
      {
        cwd: __dirname,
        onLine: (line) => sendProgress(backendUrl, claim.session_id, `[oauth] ${line}`, 'oauth_signing_in', account.email),
      },
    );
    await sendComplete(backendUrl, claim.session_id, account.email, account.password || '');
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
      await syncRuntimeSettings(backendUrl, appConfig);
      const startedSessionId = await startRegistration(backendUrl);
      autoStarted = true;
      if (startedSessionId) {
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
      await runClaim(backendUrl, claim);
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
  log.error('注册 Worker 异常退出', {
    error: error instanceof Error ? error.stack || error.message : String(error),
  });
  process.exitCode = 1;
});
