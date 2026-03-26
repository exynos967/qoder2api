import { existsSync, readFileSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { Camoufox } from "camoufox-js";
import type { Locator, Page } from "playwright-core";

import { solveCaptcha } from "./captchaSolver";
import { attachDebugRecorder } from "./debug";
import { log } from "./utils/logger";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const QODER_BASE = "https://qoder.com";
const QODER_SIGNUP_URL = `${QODER_BASE}/users/sign-up`;

const CAPTCHA_VERIFY_HOST = "captcha-open-southeast.aliyuncs.com";
const DEFAULT_CONFIG_FILE = path.join(__dirname, "config.json");

type BrowserMode = boolean | "virtual";
type BrowserModeConfig = "headed" | "headless" | "virtual";
type ProxyConfig = string | {
  server: string;
  username?: string;
  password?: string;
  bypass?: string;
};

interface AppConfig {
  count?: number;
  browserMode?: BrowserModeConfig;
  headless?: BrowserMode;
  locale?: string;
  languages?: string[] | string;
  proxy?: ProxyConfig | null;
  debugMode?: boolean;
  debugDir?: string;
  mailApiBase?: string;
  mailApiKey?: string;
  outputFile?: string;
}

interface TempEmailAccount {
  id: string;
  address: string;
  token: string;
}

interface RegisterResult {
  url: string;
  success: boolean;
  title: string;
  bodyText: string;
  html: string;
  bodyPreview: string;
}

interface StoredAccountRecord {
  email: string;
  password: string;
  cookies: any[];
}

interface CliOptions {
  configFile?: string;
}

interface PageDebugSnapshot {
  url: string;
  title: string;
  bodyPreview: string;
}

function stripJsonComments(input: string): string {
  let result = "";
  let inString = false;
  let stringQuote = '"';
  let escaped = false;
  let inLineComment = false;
  let inBlockComment = false;

  for (let i = 0; i < input.length; i += 1) {
    const char = input[i];
    const next = input[i + 1];

    if (inLineComment) {
      if (char === "\n") {
        inLineComment = false;
        result += char;
      }
      continue;
    }

    if (inBlockComment) {
      if (char === "*" && next === "/") {
        inBlockComment = false;
        i += 1;
      }
      continue;
    }

    if (inString) {
      result += char;
      if (escaped) {
        escaped = false;
        continue;
      }
      if (char === "\\") {
        escaped = true;
        continue;
      }
      if (char === stringQuote) {
        inString = false;
      }
      continue;
    }

    if ((char === '"' || char === "'") && !inString) {
      inString = true;
      stringQuote = char;
      result += char;
      continue;
    }

    if (char === "/" && next === "/") {
      inLineComment = true;
      i += 1;
      continue;
    }

    if (char === "/" && next === "*") {
      inBlockComment = true;
      i += 1;
      continue;
    }

    result += char;
  }

  return result;
}

function parseCliOptions(args: string[]): CliOptions {
  const options: CliOptions = {};
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === '--config') {
      options.configFile = args[index + 1] || '';
      index += 1;
    }
  }
  return options;
}

function resolveConfigFile(explicit?: string): string {
  const candidate = explicit || process.env.QODERREG_CONFIG_FILE || "";
  if (!candidate) {
    return DEFAULT_CONFIG_FILE;
  }
  return path.isAbsolute(candidate) ? candidate : path.resolve(process.cwd(), candidate);
}

function loadConfig(explicit?: string): AppConfig {
  const configFile = resolveConfigFile(explicit);
  if (!existsSync(configFile)) {
    return {};
  }

  try {
    const raw = readFileSync(configFile, "utf8");
    const parsed = JSON.parse(stripJsonComments(raw)) as AppConfig;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    throw new Error(
      `读取 config.json 失败: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

function normalizeProxyServerString(value: string): string {
  return value
    .trim()
    .replace(/：/g, ":")
    .replace(/；/g, ":")
    .replace(/，/g, ",");
}

function normalizeProxyConfig(proxy: AppConfig["proxy"]): ProxyConfig | undefined {
  if (!proxy) {
    return undefined;
  }

  if (typeof proxy === "string") {
    const value = normalizeProxyServerString(proxy);
    if (value !== proxy.trim()) {
      log.warn(`代理地址已自动纠正为: ${value}`);
    }
    return value || undefined;
  }

  const server = normalizeProxyServerString(proxy.server || "");
  if (proxy.server && server !== proxy.server.trim()) {
    log.warn(`代理地址已自动纠正为: ${server}`);
  }
  if (!server) {
    return undefined;
  }

  return {
    server,
    username: proxy.username?.trim() || undefined,
    password: proxy.password || undefined,
    bypass: proxy.bypass?.trim() || undefined,
  };
}

function maskProxy(proxy: ProxyConfig | undefined): string {
  if (!proxy) {
    return "disabled";
  }

  if (typeof proxy === "string") {
    return proxy.replace(/:\/\/([^:@/]+):([^@/]+)@/, "://***:***@");
  }

  return proxy.username ? `${proxy.server} (auth)` : proxy.server;
}

const CLI_OPTIONS = parseCliOptions(process.argv.slice(2));
const APP_CONFIG = loadConfig(CLI_OPTIONS.configFile);
const BROWSER_LOCALE = APP_CONFIG.locale || "zh-CN";
const BROWSER_LANGUAGES = (
  Array.isArray(APP_CONFIG.languages)
    ? APP_CONFIG.languages
    : (APP_CONFIG.languages || "zh-CN,zh").split(",")
).map((lang) => lang.trim()).filter(Boolean);
const BROWSER_ACCEPT_LANGUAGES = BROWSER_LANGUAGES.join(",");
const BROWSER_PROXY = normalizeProxyConfig(APP_CONFIG.proxy);
const YYDSMAIL_API = APP_CONFIG.mailApiBase || "https://maliapi.215.im/v1";
const YYDSMAIL_API_KEY = APP_CONFIG.mailApiKey || "AC-0d9d789d3dc8151d1c6c8cac";
const OUTPUT_FILE = path.isAbsolute(APP_CONFIG.outputFile || "")
  ? (APP_CONFIG.outputFile as string)
  : path.join(__dirname, APP_CONFIG.outputFile || "accounts.json");

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryableNavigationError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  const normalized = message.toLowerCase();
  return (
    normalized.includes("ns_error_net_reset")
    || normalized.includes("err_connection_reset")
    || normalized.includes("err_proxy_connection_failed")
    || normalized.includes("err_tunnel_connection_failed")
    || normalized.includes("timeout")
    || normalized.includes("net::")
  );
}

async function gotoWithRetry(
  page: Page,
  url: string,
  options: { timeout: number; attempts?: number },
): Promise<void> {
  const { timeout, attempts = 3 } = options;
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout });
      return;
    } catch (error) {
      lastError = error;
      if (!isRetryableNavigationError(error) || attempt >= attempts) {
        throw error;
      }
      const delayMs = 1500 * attempt;
      log.warn(`打开页面失败，准备重试 (${attempt}/${attempts})`, {
        url,
        delayMs,
        error: error instanceof Error ? error.message : String(error),
      });
      await sleep(delayMs);
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

function isLinuxPlatform(): boolean {
  return process.platform === "linux";
}

export function resolveBrowserMode(): BrowserMode {
  const defaultMode: BrowserMode = false;

  const raw = APP_CONFIG.browserMode ?? APP_CONFIG.headless;
  if (!raw) {
    return defaultMode;
  }

  if (typeof raw === "string") {
    const named = raw.trim().toLowerCase();
    if (named === "headed") {
      return false;
    }
    if (named === "headless") {
      return true;
    }
    if (named === "virtual" || named === "xvfb") {
      return isLinuxPlatform() ? "virtual" : false;
    }
  }

  const value = String(raw).trim().toLowerCase();
  if (value === "virtual" || value === "xvfb") {
    return isLinuxPlatform() ? "virtual" : false;
  }
  if (["0", "false", "no", "n", "off", "headed"].includes(value)) {
    return false;
  }
  if (["1", "true", "yes", "y", "on", "headless"].includes(value)) {
    return true;
  }

  return defaultMode;
}

function browserModeDesc(browserMode: BrowserMode): string {
  if (browserMode === true) {
    return "Camoufox 无头 (headless)";
  }
  if (browserMode === "virtual") {
    return "Camoufox 虚拟显示 (headless=virtual)";
  }
  return "Camoufox 有头 (headed)";
}

function randomPassword(length = 16): string {
  const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%";
  const picks = [
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789",
    "!@#$%",
  ].map((alphabet) => alphabet[Math.floor(Math.random() * alphabet.length)]);

  for (let i = picks.length; i < length; i += 1) {
    picks.push(chars[Math.floor(Math.random() * chars.length)]);
  }

  for (let i = picks.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [picks[i], picks[j]] = [picks[j], picks[i]];
  }

  return picks.join("");
}

function randomName(): { firstName: string; lastName: string } {
  const firstNames = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Quinn", "Avery", "Dakota"];
  const lastNames = ["Smith", "Lee", "Wang", "Chen", "Kim", "Park", "Liu", "Yang", "Lin", "Wu"];
  return {
    firstName: firstNames[Math.floor(Math.random() * firstNames.length)],
    lastName: lastNames[Math.floor(Math.random() * lastNames.length)],
  };
}

async function fetchJson<T>(url: string, init: RequestInit, timeoutMs = 15_000): Promise<T> {
  const response = await fetch(url, {
    ...init,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

async function readJsonArrayFile(filePath: string): Promise<unknown[]> {
  if (!existsSync(filePath)) {
    return [];
  }

  const raw = await readFile(filePath, "utf8");
  if (!raw.trim()) {
    return [];
  }

  const parsed = JSON.parse(raw) as unknown;
  return Array.isArray(parsed) ? parsed : [];
}

async function appendAccountRecord(record: StoredAccountRecord): Promise<void> {
  const existing = await readJsonArrayFile(OUTPUT_FILE);
  existing.push(record);
  await writeFile(OUTPUT_FILE, `${JSON.stringify(existing, null, 2)}\n`, "utf8");
}

async function collectPageStorage(page: Page): Promise<{
  userAgent: string;
  localStorage: Record<string, string>;
  sessionStorage: Record<string, string>;
}> {
  return page.evaluate(() => {
    const dump = (storage: Storage): Record<string, string> => {
      const data: Record<string, string> = {};
      for (let i = 0; i < storage.length; i += 1) {
        const key = storage.key(i);
        if (key !== null) {
          data[key] = storage.getItem(key) || "";
        }
      }
      return data;
    };

    return {
      userAgent: navigator.userAgent,
      localStorage: dump(window.localStorage),
      sessionStorage: dump(window.sessionStorage),
    };
  });
}

async function createTempEmail(): Promise<TempEmailAccount> {
  const data = await fetchJson<{ success: boolean; data: TempEmailAccount }>(
    `${YYDSMAIL_API}/accounts`,
    {
      method: "POST",
      headers: {
        "X-API-Key": YYDSMAIL_API_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),
    },
  );

  if (!data.success) {
    throw new Error(`YYDS Mail 创建邮箱失败: ${JSON.stringify(data)}`);
  }

  log.info(`临时邮箱创建成功: ${data.data.address} (ID: ${data.data.id})`);
  return data.data;
}

function extractCodeFromEmail(body: string): string | null {
  const patterns = [
    /(?:验证码|校验码|驗證碼|verification code|verify code|one[-\s]?time password|otp|code)[^\d]{0,30}(\d{6})/i,
    /\b(\d{6})\b/,
  ];

  for (const pattern of patterns) {
    const match = body.match(pattern);
    if (match?.[1]) return match[1];
  }

  return null;
}

function decodeHtmlEntities(text: string): string {
  return text
    .replace(/&nbsp;/gi, " ")
    .replace(/&ensp;/gi, " ")
    .replace(/&emsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(Number(code)));
}

function htmlToText(html: string): string {
  return decodeHtmlEntities(
    html
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/p>/gi, "\n")
      .replace(/<\/div>/gi, "\n")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim(),
  );
}

function rankMessages<T extends { subject?: string; createdAt?: string }>(messages: T[]): T[] {
  const subjectScore = (subject?: string): number => {
    const value = (subject || "").toLowerCase();
    if (value.includes("verify your email")) return 30;
    if (value.includes("verification")) return 20;
    if (value.includes("验证码")) return 20;
    if (value.includes("qoder")) return 10;
    return 0;
  };

  return [...messages].sort((a, b) => {
    const scoreDiff = subjectScore(b.subject) - subjectScore(a.subject);
    if (scoreDiff !== 0) return scoreDiff;
    return new Date(b.createdAt || 0).getTime() - new Date(a.createdAt || 0).getTime();
  });
}

async function waitForVerificationCode(
  address: string,
  tempToken: string,
  timeoutMs = 120_000,
  pollIntervalMs = 5_000,
): Promise<string> {
  log.info(`等待验证码邮件... (超时: ${Math.floor(timeoutMs / 1000)}s)`);
  const start = Date.now();

  while (Date.now() - start < timeoutMs) {
    try {
      const messagesData = await fetchJson<{
        success: boolean;
        data?: { messages?: Array<{ id: string; subject?: string; createdAt?: string }> };
      }>(
        `${YYDSMAIL_API}/messages`,
        {
          method: "GET",
          headers: {
            Authorization: `Bearer ${tempToken}`,
          },
        },
      );

      if (!messagesData.success) {
        log.warn(`查询邮件失败: ${JSON.stringify(messagesData)}`);
        await sleep(pollIntervalMs);
        continue;
      }

      const messages = rankMessages(messagesData.data?.messages || []);
      for (const message of messages) {
        log.info(`收到邮件: ${(message.subject || "").slice(0, 60)}`);

        const detailData = await fetchJson<{
          success: boolean;
          data?: { html?: string[]; text?: string };
        }>(
          `${YYDSMAIL_API}/messages/${encodeURIComponent(message.id)}`,
          {
            method: "GET",
            headers: {
              Authorization: `Bearer ${tempToken}`,
            },
          },
        );

        if (!detailData.success || !detailData.data) {
          continue;
        }

        const textBody = (detailData.data.text || "").trim();
        const htmlBody = detailData.data.html?.join(" ") || "";
        const body = textBody || htmlToText(htmlBody);
        const code = extractCodeFromEmail(body);
        if (code) {
          log.info(`验证码获取成功: ${code}`);
          return code;
        }

        log.debug("邮件中未提取到验证码", {
          subject: message.subject,
          preview: body.slice(0, 200),
        });
      }
    } catch (error) {
      log.warn("轮询邮件异常", {
        error: error instanceof Error ? error.message : String(error),
      });
    }

    await sleep(pollIntervalMs);
  }

  throw new Error("获取验证码超时");
}

function attachCaptchaDebugLogging(page: Page): void {
  page.on("response", async (response) => {
    try {
      const url = response.url();
      if (!url.includes(CAPTCHA_VERIFY_HOST)) {
        return;
      }

      const postData = response.request().postData() || "";
      if (!postData.includes("Action=VerifyCaptchaV3")) {
        return;
      }

      const payload = (await response.json()) as {
        Message?: string;
        Result?: {
          VerifyCode?: string;
          VerifyResult?: boolean;
        };
      };

      log.info("[CaptchaAPI] VerifyCaptchaV3", {
        verifyCode: payload.Result?.VerifyCode,
        verifyResult: payload.Result?.VerifyResult,
        message: payload.Message,
      });
    } catch (error) {
      log.warn("[CaptchaAPI] Failed to parse verify response", {
        error: error instanceof Error ? error.message : String(error),
      });
    }
  });
}

async function logBrowserFingerprint(page: Page): Promise<void> {
  try {
    const fp = await page.evaluate(() => ({
      ua: navigator.userAgent,
      webdriver: navigator.webdriver,
      language: navigator.language,
      languages: navigator.languages,
      platform: navigator.platform,
      plugins: navigator.plugins?.length ?? 0,
      intl: Intl.DateTimeFormat().resolvedOptions().locale,
    }));
    log.info("[Env] 浏览器指纹", fp);
  } catch (error) {
    log.warn("[Env] 读取浏览器指纹失败", {
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

async function doRegisterWithBrowser(
  page: Page,
  email: string,
  firstName: string,
  lastName: string,
  password: string,
  tempToken: string,
): Promise<RegisterResult> {
  log.info("打开注册页面...");
  await gotoWithRetry(page, QODER_SIGNUP_URL, { timeout: 30_000, attempts: 3 });
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForTimeout(2000);
  await logBrowserFingerprint(page);

  log.info(`填写第一步: name=${firstName} ${lastName}, email=${email}`);
  const firstNameSelectors = [
    "#basic_firstName",
    'input[name="firstName"]',
    'input[autocomplete="given-name"]',
    'input[placeholder*="first" i]',
    'input[aria-label*="first" i]',
  ];
  const lastNameSelectors = [
    "#basic_lastName",
    'input[name="lastName"]',
    'input[autocomplete="family-name"]',
    'input[placeholder*="last" i]',
    'input[aria-label*="last" i]',
  ];
  const fullNameSelectors = [
    'input[name="name"]',
    'input[autocomplete="name"]',
    'input[placeholder*="full name" i]',
    'input[placeholder*="your name" i]',
    'input[aria-label="name" i]',
  ];
  const emailSelectors = [
    "#basic_email",
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[placeholder*="email" i]',
    'input[aria-label*="email" i]',
  ];
  const fieldTimeout = 25_000;

  const firstNameSelector = await waitForAnyVisibleInput(page, firstNameSelectors, { timeout: fieldTimeout });
  if (firstNameSelector) {
    await page.locator(firstNameSelector).first().fill(firstName);
    await fillVisibleInputBySelectors(page, lastNameSelectors, lastName, {
      timeout: fieldTimeout,
      label: "姓氏",
    });
  } else {
    await fillVisibleInputBySelectors(page, fullNameSelectors, `${firstName} ${lastName}`, {
      timeout: fieldTimeout,
      label: "姓名",
    });
  }

  await fillVisibleInputBySelectors(page, emailSelectors, email, {
    timeout: fieldTimeout,
    label: "邮箱",
  });

  try {
    const checkbox = page.locator(".ant-checkbox-input").first();
    if (!(await checkbox.isChecked())) {
      await checkbox.check();
      log.info("勾选协议");
    }
  } catch (error) {
    log.warn("勾选协议异常", {
      error: error instanceof Error ? error.message : String(error),
    });
  }

  log.info("点击 Continue (第一次, 展开密码框)...");
  const continueButton = page.locator("button").filter({
    hasText: /Continue|继\s*续/i,
  }).first();
  await continueButton.waitFor({ state: "visible", timeout: 5000 });
  await continueButton.click();

  log.info("等待密码框显示...");
  const passwordSelector = await revealPasswordInput(page, continueButton, {
    timeout: 18_000,
  });
  await page.locator(passwordSelector).first().fill(password);
  log.info("填写密码完毕");

  await page.waitForTimeout(300);
  log.info("点击 Continue (第二次, 提交表单)...");
  await continueButton.click();

  await page.waitForTimeout(3000);

  log.info("等待滑块验证...");
  const captchaResult = await solveCaptcha(page, email, "auto");
  if (!captchaResult.success) {
    throw new Error(`滑块验证码自动解决失败: ${captchaResult.error || "unknown"}`);
  }

  log.info("滑块验证通过, 等待邮箱验证码...");
  const code = await waitForVerificationCode(email, tempToken);

  log.info(`填入验证码: ${code}`);
  await page.waitForTimeout(1000);

  let otpFilled = false;

  try {
    const otpInputs = page.locator('input[maxlength="1"]');
    const count = await otpInputs.count();
    if (count >= 4) {
      for (let i = 0; i < Math.min(count, code.length); i += 1) {
        await otpInputs.nth(i).fill(code[i]);
        await page.waitForTimeout(50);
      }
      otpFilled = true;
      log.info(`OTP 输入完成 (单字符输入框 x${count})`);
    }
  } catch (error) {
    log.debug("单字符 OTP 输入失败", {
      error: error instanceof Error ? error.message : String(error),
    });
  }

  if (!otpFilled) {
    try {
      const codeInput = page.locator(
        [
          'input[name="code"]',
          'input[placeholder*="验证码" i]',
          'input[placeholder*="code" i]',
          'input[aria-label*="code" i]',
          'input[aria-label*="OTP" i]',
        ].join(", "),
      ).first();
      await codeInput.waitFor({ state: "visible", timeout: 5000 });
      await codeInput.fill(code);
      otpFilled = true;
      log.info("OTP 输入完成 (完整输入框)");
    } catch (error) {
      log.debug("完整 OTP 输入失败", {
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  if (!otpFilled) {
    log.info("尝试键盘直接输入验证码...");
    await page.keyboard.type(code, { delay: 80 });
  }

  await page.waitForTimeout(500);
  try {
    const confirmButton = page.locator(
      [
        'button[type="submit"]',
        'button:has-text("Verify")',
        'button:has-text("确认")',
        'button:has-text("Submit")',
        'button:has-text("完成")',
      ].join(", "),
    ).first();
    if (await confirmButton.isVisible()) {
      await confirmButton.click();
      log.info("点击确认按钮");
    }
  } catch {
    // noop
  }

  await page.waitForTimeout(3000);

  const currentUrl = page.url();
  const title = await page.title().catch(() => "");
  const html = await page.content().catch(() => "");
  const bodyText = await page.evaluate(() => document.body.innerText || "");
  const result = {
    url: currentUrl,
    success: !currentUrl.includes("sign-up"),
    title,
    bodyText,
    html,
    bodyPreview: bodyText.slice(0, 200),
  };

  log.info(`注册流程完成, 当前 URL: ${currentUrl}`);
  return result;
}

async function doRegister(): Promise<{
  email: string;
  password: string;
  result: RegisterResult;
}> {
  const account = await createTempEmail();
  const password = randomPassword();
  const { firstName, lastName } = randomName();
  const browserMode = resolveBrowserMode();
  const requestedMode = String(APP_CONFIG.browserMode ?? APP_CONFIG.headless ?? "").trim().toLowerCase();

  log.info(`用户信息: name=${firstName} ${lastName}, email=${account.address}, pwd=${password.slice(0, 4)}****`);

  if ((requestedMode === "virtual" || requestedMode === "xvfb") && !isLinuxPlatform()) {
    log.warn('config.json 中 browserMode="virtual" 仅 Linux 支持，当前平台已回退为 Camoufox 有头模式。');
  }
  if (browserMode === true) {
    log.warn("当前使用 Camoufox headless 模式；Aliyun captcha 在纯无头环境更容易触发风控。");
  } else if (browserMode === "virtual") {
    log.warn("当前未检测到 DISPLAY，自动使用 Camoufox virtual display。");
  }

  log.info("[Env] 浏览器内核: Camoufox JS (Firefox)");
  log.info(`[Env] 浏览器模式: ${browserModeDesc(browserMode)}`);
  log.info(`[Env] locale: ${BROWSER_LOCALE}`);
  log.info(`[Env] languages: ${BROWSER_ACCEPT_LANGUAGES}`);
  log.info(`[Env] proxy: ${maskProxy(BROWSER_PROXY)}`);

  const browser = await Camoufox({
    headless: browserMode,
    os: "windows",
    locale: BROWSER_LOCALE,
    proxy: BROWSER_PROXY,
    firefox_user_prefs: {
      "intl.accept_languages": BROWSER_ACCEPT_LANGUAGES,
    },
  });

  const context = await browser.newContext({
    viewport: { width: 1400, height: 1200 },
    locale: BROWSER_LOCALE,
    ignoreHTTPSErrors: true,
  });

  const page = await context.newPage();
  const debugRecorder = await attachDebugRecorder(page, {
    enabled: Boolean(APP_CONFIG.debugMode),
    outputDir: APP_CONFIG.debugDir,
    baseDir: __dirname,
    prefix: "register",
  });
  attachCaptchaDebugLogging(page);
  await debugRecorder.captureCurrent("page-created");

  try {
    const result = await doRegisterWithBrowser(
      page,
      account.address,
      firstName,
      lastName,
      password,
      account.token,
    );
    const cookies = await context.cookies();

    const record: StoredAccountRecord = {
      email: account.address,
      password,
      cookies,
    };

    await appendAccountRecord(record);
    log.info(`注册完成! ${account.address}----${password}`);
    return {
      email: account.address,
      password,
      result,
    };
  } finally {
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
  }
}

function printUsage(): void {
  console.log("Usage: npx tsx register.ts [--config PATH]");
  console.log("配置来源: 默认 register/config.json，也可通过 --config 或 QODERREG_CONFIG_FILE 覆盖");
}

async function collectPageDebugSnapshot(page: Page): Promise<PageDebugSnapshot> {
  const url = page.url();
  const title = await page.title().catch(() => "");
  const bodyText = await page.evaluate(() => document.body?.innerText || "").catch(() => "");
  return {
    url,
    title,
    bodyPreview: bodyText.slice(0, 1000),
  };
}

async function waitForAnyVisibleInput(
  page: Page,
  selectors: string[],
  options: { timeout: number },
): Promise<string | null> {
  const { timeout } = options;
  try {
    await page.waitForFunction(
      (candidateSelectors: string[]) => {
        return candidateSelectors.some((selector) => {
          const element = document.querySelector(selector) as HTMLInputElement | null;
          if (!element) {
            return false;
          }
          const rect = element.getBoundingClientRect();
          const style = window.getComputedStyle(element);
          return rect.width > 0
            && rect.height > 0
            && style.display !== "none"
            && style.visibility !== "hidden";
        });
      },
      selectors,
      { timeout },
    );
  } catch {
    return null;
  }

  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.isVisible()) {
        return selector;
      }
    } catch {
      // noop
    }
  }
  return null;
}

async function fillVisibleInputBySelectors(
  page: Page,
  selectors: string[],
  value: string,
  options: { timeout: number; label: string },
): Promise<string> {
  const { timeout, label } = options;
  const selector = await waitForAnyVisibleInput(page, selectors, { timeout });
  if (!selector) {
    const snapshot = await collectPageDebugSnapshot(page);
    throw new Error(
      `未找到可见的${label}输入框。当前 URL=${snapshot.url}，标题=${snapshot.title}，页面摘要=${snapshot.bodyPreview}`,
    );
  }
  await page.locator(selector).first().fill(value);
  return selector;
}

async function revealPasswordInput(
  page: Page,
  continueButton: Locator,
  options: { timeout: number },
): Promise<string> {
  const passwordSelectors = [
    "#basic_password",
    'input[name="password"]',
    'input[type="password"]',
    'input[autocomplete="new-password"]',
    'input[placeholder*="密码" i]',
    'input[aria-label*="密码" i]',
  ];
  const deadline = Date.now() + options.timeout;
  let attempts = 0;

  while (Date.now() < deadline) {
    const selector = await waitForAnyVisibleInput(page, passwordSelectors, { timeout: 1200 });
    if (selector) {
      return selector;
    }

    attempts += 1;
    if (attempts <= 3) {
      log.info(`密码框尚未出现，重试展开第 ${attempts} 次...`);
      try {
        if (await continueButton.isVisible().catch(() => false)) {
          await continueButton.click({ force: attempts > 1 });
        }
      } catch {
        // noop
      }
    }
    await page.waitForTimeout(1200);
  }

  const snapshot = await collectPageDebugSnapshot(page);
  throw new Error(
    `未找到可见的密码输入框。当前 URL=${snapshot.url}，标题=${snapshot.title}，页面摘要=${snapshot.bodyPreview}`,
  );
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes("--help") || args.includes("-h")) {
    printUsage();
    return;
  }

  const countArg = String(APP_CONFIG.count ?? 1);
  const count = Number.parseInt(countArg, 10);
  if (!Number.isFinite(count) || count <= 0) {
    throw new Error(`无效数量: ${countArg}`);
  }

  log.info("========== qoder2api 自动注册 Worker ==========");
  log.info(`目标数量: ${count}`);
  log.info(`临时邮箱: YYDS Mail API (${YYDSMAIL_API})`);
  log.info("滑块验证: captchaSolver.ts (直接调用 TS)");
  log.info(`模式: ${browserModeDesc(resolveBrowserMode())}`);
  log.info("==========================================");

  let success = 0;
  let fail = 0;

  for (let i = 0; i < count; i += 1) {
    log.info(`\n===== 第 ${i + 1}/${count} 个账号 =====`);
    try {
      await doRegister();
      success += 1;
      log.info(`成功! 当前进度: ${success} 成功 / ${fail} 失败`);
    } catch (error) {
      fail += 1;
      log.error("注册失败", {
        error: error instanceof Error ? error.stack || error.message : String(error),
      });
    }

    if (i < count - 1) {
      const delayMs = 3000 + Math.floor(Math.random() * 5000);
      log.info(`等待 ${(delayMs / 1000).toFixed(1)}s 后继续...`);
      await sleep(delayMs);
    }
  }

  log.info("\n========== 批量注册完成 ==========");
  log.info(`成功: ${success}, 失败: ${fail}`);
  log.info(`账号保存: ${OUTPUT_FILE}`);

  if (fail > 0) {
    process.exitCode = 1;
  }
}

void main().catch((error) => {
  log.error("主流程异常", {
    error: error instanceof Error ? error.stack || error.message : String(error),
  });
  process.exitCode = 1;
});
