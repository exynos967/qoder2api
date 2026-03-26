import fs from "node:fs/promises";

import { Camoufox } from "camoufox-js";

interface InspectConfig {
  accountsPath: string;
  targetUrl: string;
  headless: boolean;
  locale: string;
}

function parseArgs(argv: string[]): InspectConfig {
  let accountsPath = "";
  let targetUrl = "";
  let headless = true;
  let locale = "zh-CN";

  for (let index = 0; index < argv.length; index += 1) {
    const current = argv[index];
    const next = argv[index + 1];

    if (current === "--accounts" && next) {
      accountsPath = next;
      index += 1;
      continue;
    }
    if (current === "--target-url" && next) {
      targetUrl = next;
      index += 1;
      continue;
    }
    if (current === "--locale" && next) {
      locale = next;
      index += 1;
      continue;
    }
    if (current === "--headed") {
      headless = false;
      continue;
    }
  }

  if (!accountsPath || !targetUrl) {
    throw new Error(
      "用法: npx tsx inspect_oauth_page.ts --accounts /path/to/accounts.json --target-url https://www.qoder.com/device/selectAccounts?... [--locale zh-CN] [--headed]",
    );
  }

  return { accountsPath, targetUrl, headless, locale };
}

const config = parseArgs(process.argv.slice(2));
const accounts = JSON.parse(await fs.readFile(config.accountsPath, "utf8"));
const account = accounts[0];
if (!account?.cookies) {
  throw new Error("accounts.json 中未找到可用 cookies");
}

const browser = await Camoufox({
  headless: config.headless,
  os: "windows",
  locale: config.locale,
});
const context = await browser.newContext({
  viewport: { width: 1400, height: 1200 },
  locale: config.locale,
  ignoreHTTPSErrors: true,
});
await context.addCookies(account.cookies);
const page = await context.newPage();
await page.goto(config.targetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
await page.waitForTimeout(8000);
const info = await page.evaluate(() => {
  const text = document.body?.innerText || "";
  const buttons = Array.from(document.querySelectorAll("button, [role=\"button\"]"))
    .map((element) => ({
      text: (element.textContent || "").trim(),
      tag: element.tagName,
      cls: element.className,
    }))
    .filter((item) => item.text);
  const cards = Array.from(document.querySelectorAll("div, li, button, a"))
    .map((element) => ({
      text: (element.textContent || "").trim().slice(0, 200),
      cls: element.className,
      role: element.getAttribute("role"),
      data: element.getAttribute("data-testid") || element.getAttribute("data-test") || "",
    }))
    .filter(
      (item) =>
        item.text &&
        (
          item.text.includes("@") ||
          item.text.includes("Continue") ||
          item.text.includes("Authorize") ||
          item.text.includes("继续") ||
          item.text.includes("授权")
        ),
    )
    .slice(0, 50);
  return { url: location.href, title: document.title, text: text.slice(0, 2000), buttons, cards };
});
console.log(JSON.stringify(info, null, 2));
await context.close();
await browser.close();
