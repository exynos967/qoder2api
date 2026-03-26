import { existsSync, readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { Camoufox } from "camoufox-js";
import type { BrowserContext, Page } from "playwright-core";

import { solveCaptcha } from "./captchaSolver";
import { attachDebugRecorder } from "./debug";
import { log } from "./utils/logger";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const QODER_BASE = "https://qoder.com";
const QODER_SIGNIN_URL = `${QODER_BASE}/users/sign-in`;
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
  browserMode?: BrowserModeConfig;
  headless?: BrowserMode;
  locale?: string;
  languages?: string[] | string;
  proxy?: ProxyConfig | null;
  debugMode?: boolean;
  debugDir?: string;
  outputFile?: string;
}

interface CliOptions {
  accountSelector?: string;
  targetUrl?: string;
  configFile?: string;
  callbackTimeoutSeconds: number;
}

interface StoredAccountRecord {
  email: string;
  password: string;
  cookies: Parameters<BrowserContext["addCookies"]>[0];
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

function resolveConfigFile(explicit?: string): string {
  if (!explicit) {
    return process.env.QODERREG_CONFIG_FILE || DEFAULT_CONFIG_FILE;
  }
  return path.isAbsolute(explicit) ? explicit : path.resolve(process.cwd(), explicit);
}

function loadConfig(explicitConfigFile?: string): AppConfig {
  const configFile = resolveConfigFile(explicitConfigFile);
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

function isLinuxPlatform(): boolean {
  return process.platform === "linux";
}

function resolveBrowserMode(config: AppConfig): BrowserMode {
  const raw = config.browserMode ?? config.headless;
  if (!raw) {
    return false;
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

  return false;
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
  const { timeout, attempts = 5 } = options;
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

function readAccounts(accountsFile: string): StoredAccountRecord[] {
  if (!existsSync(accountsFile)) {
    throw new Error(`账号文件不存在: ${accountsFile}`);
  }

  const raw = readFileSync(accountsFile, "utf8");
  const parsed = JSON.parse(raw) as unknown;
  if (!Array.isArray(parsed)) {
    throw new Error(`账号文件格式错误: ${accountsFile}`);
  }

  return parsed.filter((item): item is StoredAccountRecord => {
    return Boolean(
      item
      && typeof item === "object"
      && typeof (item as StoredAccountRecord).email === "string"
      && typeof (item as StoredAccountRecord).password === "string"
      && Array.isArray((item as StoredAccountRecord).cookies),
    );
  });
}

async function writeAccounts(accountsFile: string, accounts: StoredAccountRecord[]): Promise<void> {
  await writeFile(accountsFile, `${JSON.stringify(accounts, null, 2)}\n`, "utf8");
}

function selectAccount(accounts: StoredAccountRecord[], selector?: string): {
  account: StoredAccountRecord;
  index: number;
} {
  if (accounts.length === 0) {
    throw new Error("账号文件为空");
  }

  if (!selector) {
    if (accounts.length > 1) {
      log.warn(`未指定登录账号，默认使用第 1 个账号: ${accounts[0].email}`);
    }
    return { account: accounts[0], index: 0 };
  }

  const trimmed = selector.trim();
  if (!trimmed) {
    return { account: accounts[0], index: 0 };
  }

  const asIndex = Number.parseInt(trimmed, 10);
  if (Number.isFinite(asIndex) && String(asIndex) === trimmed) {
    const index = asIndex - 1;
    if (index < 0 || index >= accounts.length) {
      throw new Error(`账号序号越界: ${trimmed} (有效范围 1-${accounts.length})`);
    }
    return { account: accounts[index], index };
  }

  const index = accounts.findIndex((item) => item.email === trimmed);
  if (index === -1) {
    throw new Error(`accounts.json 中未找到账号: ${trimmed}`);
  }
  return { account: accounts[index], index };
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

async function waitForPageStable(page: Page): Promise<void> {
  await page.waitForLoadState("domcontentloaded", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForTimeout(2000);
}

async function isStillOnSignIn(page: Page): Promise<boolean> {
  if (page.url().includes("/users/sign-in")) {
    return true;
  }

  try {
    const emailInput = page.locator("#basic_email").first();
    if (await emailInput.isVisible()) {
      return true;
    }
  } catch {
    // noop
  }

  try {
    const signInButton = page.locator("button").filter({
      hasText: /Sign in|登\s*录|登录/i,
    }).first();
    if (await signInButton.isVisible()) {
      return true;
    }
  } catch {
    // noop
  }

  return false;
}

async function fillVisiblePassword(page: Page, password: string): Promise<void> {
  const selectors = ["#password_password", "#basic_password", 'input[type="password"]'];

  await page.waitForFunction(
    (candidateSelectors: string[]) => {
      return candidateSelectors.some((selector) => {
        const el = document.querySelector(selector) as HTMLInputElement | null;
        if (!el) {
          return false;
        }
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      });
    },
    selectors,
    { timeout: 15_000 },
  );

  for (const selector of selectors) {
    const input = page.locator(selector).first();
    try {
      if (await input.isVisible()) {
        await input.fill(password);
        return;
      }
    } catch {
      // noop
    }
  }

  throw new Error("未找到可见的密码输入框");
}

function resolveEntryUrl(targetUrl?: string): string {
  const candidate = (targetUrl || "").trim();
  return candidate || QODER_SIGNIN_URL;
}

async function tryCookieLogin(
  context: BrowserContext,
  page: Page,
  account: StoredAccountRecord,
  targetUrl?: string,
): Promise<boolean> {
  if (!account.cookies.length) {
    log.warn("当前账号没有可用 cookies，跳过 cookie 登录");
    return false;
  }

  const entryUrl = resolveEntryUrl(targetUrl);
  log.info(`尝试使用 accounts.json 中保存的 cookies 自动登录: ${entryUrl}`);
  await context.addCookies(account.cookies);
  try {
    await gotoWithRetry(page, entryUrl, { timeout: 30_000, attempts: 5 });
  } catch (error) {
    log.warn("Cookie 登录入口打开失败，准备回退账号密码登录", {
      url: entryUrl,
      error: error instanceof Error ? error.message : String(error),
    });
    return false;
  }
  await waitForPageStable(page);

  if (!(await isStillOnSignIn(page))) {
    log.info(`Cookie 登录成功，当前 URL: ${page.url()}`);
    return true;
  }

  log.warn("Cookie 登录未命中，回退账号密码登录");
  return false;
}

async function doCredentialLogin(
  page: Page,
  account: StoredAccountRecord,
): Promise<void> {
  const entryUrl = resolveEntryUrl();
  log.info(`打开登录页面: ${entryUrl}`);
  await gotoWithRetry(page, entryUrl, { timeout: 30_000, attempts: 5 });
  await waitForPageStable(page);
  await logBrowserFingerprint(page);

  log.info(`填写登录邮箱: ${account.email}`);
  const emailInput = page.locator("#basic_email").first();
  await emailInput.waitFor({ state: "visible", timeout: 10_000 });
  await emailInput.fill(account.email);

  const continueButton = page.locator("button").filter({
    hasText: /Continue|继\s*续/i,
  }).first();
  if (await continueButton.isVisible().catch(() => false)) {
    log.info("点击 Continue...");
    await continueButton.click();
  }

  log.info("等待密码框显示...");
  await fillVisiblePassword(page, account.password);
  log.info("填写密码完毕");

  const signInButton = page.locator("button").filter({
    hasText: /Sign in|登\s*录|登录/i,
  }).first();
  await signInButton.waitFor({ state: "visible", timeout: 10_000 });

  log.info("点击 Sign in...");
  await signInButton.click();
  await page.waitForTimeout(3000);

  log.info("等待滑块验证...");
  const captchaResult = await solveCaptcha(page, account.email, "auto");
  if (!captchaResult.success) {
    throw new Error(`滑块验证码自动解决失败: ${captchaResult.error || "unknown"}`);
  }

  await waitForPageStable(page);
  if (await isStillOnSignIn(page)) {
    if (await signInButton.isVisible().catch(() => false)) {
      log.info("滑块后再次点击 Sign in...");
      await signInButton.click();
      await waitForPageStable(page);
    }
  }

  if (await isStillOnSignIn(page)) {
    const bodyText = await page.evaluate(() => document.body.innerText || "");
    throw new Error(`登录后仍停留在 sign-in 页面: ${bodyText.slice(0, 300)}`);
  }
}

async function openOauthTarget(page: Page, targetUrl: string): Promise<void> {
  log.info(`登录成功，准备打开 OAuth 页面: ${targetUrl}`);
  await gotoWithRetry(page, targetUrl, { timeout: 30_000, attempts: 5 });
  await waitForPageStable(page);
}

async function trySelectOauthAccount(page: Page, email: string): Promise<boolean> {
  if (!page.url().includes("/device/selectAccounts")) {
    return false;
  }

  const candidates = [
    page.locator("button").filter({ hasText: email }).first(),
    page.locator('[role="button"]').filter({ hasText: email }).first(),
    page.locator("label").filter({ hasText: email }).first(),
    page.locator('[role="option"]').filter({ hasText: email }).first(),
    page.locator(".ant-list-item").filter({ hasText: email }).first(),
    page.locator(".ant-card").filter({ hasText: email }).first(),
  ];

  for (const candidate of candidates) {
    try {
      if (!(await candidate.isVisible())) {
        continue;
      }
      await candidate.click();
      await waitForPageStable(page);
      log.info("检测到 OAuth 选账号页，已尝试点击匹配当前邮箱的账号项", { email });
      return true;
    } catch {
      // noop
    }
  }

  const fallbackCandidates = [
    page.locator('[role="option"]').first(),
    page.locator("label").first(),
    page.locator(".ant-list-item").first(),
    page.locator(".ant-card").first(),
  ];

  for (const candidate of fallbackCandidates) {
    try {
      if (!(await candidate.isVisible())) {
        continue;
      }
      await candidate.click();
      await waitForPageStable(page);
      log.warn("检测到 OAuth 选账号页，未匹配到邮箱，已尝试点击第一个可选账号项", { email });
      return true;
    } catch {
      // noop
    }
  }

  return false;
}

function isLocalCallbackUrl(url: string): boolean {
  return /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url);
}

async function tryApproveOauth(page: Page): Promise<boolean> {
  const candidates = [
    page.locator("button").filter({
      hasText: /Authorize|Allow|Continue|Open App|Open application|同意|授权|继\s*续/i,
    }).first(),
    page.locator('button.ant-btn-primary').first(),
    page.locator('button[type="button"]').filter({ hasText: /继\s*续/i }).first(),
  ];

  for (const button of candidates) {
    try {
      if (!(await button.isVisible())) {
        continue;
      }
      await button.click();
      log.info("检测到 OAuth 授权页，已尝试点击确认按钮");
      return true;
    } catch {
      // noop
    }
  }

  return false;
}

async function waitForOauthCompletion(page: Page, timeoutSeconds: number, email: string): Promise<void> {
  const deadline = Date.now() + timeoutSeconds * 1000;
  let approved = false;
  let accountSelected = false;
  while (Date.now() < deadline) {
    const currentUrl = page.url();
    if (isLocalCallbackUrl(currentUrl)) {
      log.info(`OAuth 回调已命中本地地址: ${currentUrl}`);
      return;
    }
    if (!accountSelected && currentUrl.includes("/device/selectAccounts")) {
      accountSelected = await trySelectOauthAccount(page, email);
    }
    if (!approved) {
      approved = await tryApproveOauth(page);
    }
    await page.waitForTimeout(1000);
  }
  throw new Error(`等待 OAuth 回调超时（>${timeoutSeconds}s）`);
}

function parseCliOptions(args: string[]): CliOptions {
  const options: CliOptions = {
    callbackTimeoutSeconds: 120,
  };

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === '--help' || arg === '-h') {
      continue;
    }
    if (!arg.startsWith('-') && !options.accountSelector) {
      options.accountSelector = arg;
      continue;
    }
    if (arg === '--target-url') {
      options.targetUrl = args[index + 1] || '';
      index += 1;
      continue;
    }
    if (arg === '--config') {
      options.configFile = args[index + 1] || '';
      index += 1;
      continue;
    }
    if (arg === '--callback-timeout') {
      const raw = args[index + 1] || '';
      options.callbackTimeoutSeconds = Number.parseInt(raw, 10) || 120;
      index += 1;
    }
  }

  return options;
}

async function updateStoredCookies(
  accountsFile: string,
  context: BrowserContext,
  accounts: StoredAccountRecord[],
  accountIndex: number,
): Promise<void> {
  accounts[accountIndex].cookies = await context.cookies();
  await writeAccounts(accountsFile, accounts);
}

function printUsage(): void {
  console.log("Usage: npx tsx login.ts [账号序号或邮箱] [--target-url URL] [--config PATH]");
  console.log("配置来源: 默认项目根目录 config.json，也可通过 --config 或 QODERREG_CONFIG_FILE 覆盖");
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes("--help") || args.includes("-h")) {
    printUsage();
    return;
  }

  const options = parseCliOptions(args);
  const appConfig = loadConfig(options.configFile);
  const browserMode = resolveBrowserMode(appConfig);
  const browserLocale = appConfig.locale || "zh-CN";
  const browserLanguages = (
    Array.isArray(appConfig.languages)
      ? appConfig.languages
      : (appConfig.languages || "zh-CN,zh").split(",")
  ).map((lang) => lang.trim()).filter(Boolean);
  const browserAcceptLanguages = browserLanguages.join(",");
  const browserProxy = normalizeProxyConfig(appConfig.proxy);
  const accountsFile = path.isAbsolute(appConfig.outputFile || "")
    ? (appConfig.outputFile as string)
    : path.join(__dirname, appConfig.outputFile || "accounts.json");

  const accounts = readAccounts(accountsFile);
  const { account, index } = selectAccount(accounts, options.accountSelector);

  if (browserMode === true) {
    log.warn("当前使用 Camoufox headless 模式；Aliyun captcha 在纯无头环境更容易触发风控。");
  } else if (browserMode === "virtual") {
    log.warn("当前使用 Camoufox virtual display 模式。");
  }

  log.info("========== qoder2api OAuth Worker ==========");
  log.info(`[Env] 浏览器内核: Camoufox JS (Firefox)`);
  log.info(`[Env] 浏览器模式: ${browserModeDesc(browserMode)}`);
  log.info(`[Env] locale: ${browserLocale}`);
  log.info(`[Env] languages: ${browserAcceptLanguages}`);
  log.info(`[Env] proxy: ${maskProxy(browserProxy)}`);
  log.info(`[Login] 目标账号: ${account.email}`);

  const browser = await Camoufox({
    headless: browserMode,
    os: "windows",
    locale: browserLocale,
    proxy: browserProxy,
    firefox_user_prefs: {
      "intl.accept_languages": browserAcceptLanguages,
    },
  });

  const context = await browser.newContext({
    viewport: { width: 1400, height: 1200 },
    locale: browserLocale,
    ignoreHTTPSErrors: true,
  });

  const page = await context.newPage();
  const debugRecorder = await attachDebugRecorder(page, {
    enabled: Boolean(appConfig.debugMode),
    outputDir: appConfig.debugDir,
    baseDir: __dirname,
    prefix: "oauth",
  });
  attachCaptchaDebugLogging(page);
  await debugRecorder.captureCurrent("page-created");

  try {
    const cookieLoginOk = await tryCookieLogin(context, page, account, options.targetUrl);
    if (!cookieLoginOk) {
      await doCredentialLogin(page, account);
    }

    if (options.targetUrl) {
      const currentUrl = page.url();
      if (!currentUrl.includes("/device/selectAccounts") && !isLocalCallbackUrl(currentUrl)) {
        await openOauthTarget(page, options.targetUrl);
      }
      await waitForOauthCompletion(page, options.callbackTimeoutSeconds, account.email);
    }

    await sleep(1500);
    await updateStoredCookies(accountsFile, context, accounts, index);
    log.info(`登录成功，当前 URL: ${page.url()}`);
  } finally {
    try {
      await context.close();
    } catch {
      // noop
    }
    try {
      await browser.close();
    } catch {
      // noop
    }
  }
}

void main().catch((error) => {
  log.error("登录失败", {
    error: error instanceof Error ? error.stack || error.message : String(error),
  });
  process.exitCode = 1;
});
