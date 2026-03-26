import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import type { Page } from "playwright-core";

import { log } from "./utils/logger";

export interface DebugCaptureConfig {
  enabled?: boolean;
  outputDir?: string;
  baseDir?: string;
  prefix: string;
}

function sanitizePathSegment(value: string): string {
  return value
    .replace(/^https?:\/\//i, "")
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 120) || "blank";
}

function nowStamp(): string {
  const date = new Date();
  const parts = [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
    String(date.getHours()).padStart(2, "0"),
    String(date.getMinutes()).padStart(2, "0"),
    String(date.getSeconds()).padStart(2, "0"),
  ];
  return parts.join("");
}

export function resolveDebugDirectory(baseDir: string, configuredDir?: string): string {
  const candidate = (configuredDir || "debug").trim() || "debug";
  return path.isAbsolute(candidate) ? candidate : path.join(baseDir, candidate);
}

export async function attachDebugRecorder(
  page: Page,
  config: DebugCaptureConfig,
): Promise<{ captureCurrent: (reason?: string) => Promise<void> }> {
  if (!config.enabled) {
    return {
      async captureCurrent(): Promise<void> {
        return;
      },
    };
  }

  const outputDir = resolveDebugDirectory(config.baseDir || process.cwd(), config.outputDir);
  const visited = new Set<string>();
  let counter = 0;

  async function captureCurrent(reason = "visit"): Promise<void> {
    const url = page.url().trim();
    if (!url || visited.has(url)) {
      return;
    }
    visited.add(url);
    counter += 1;

    await mkdir(outputDir, { recursive: true });
    const safeUrl = sanitizePathSegment(url);
    const baseName = `${String(counter).padStart(3, "0")}-${config.prefix}-${nowStamp()}-${safeUrl}`;
    const imagePath = path.join(outputDir, `${baseName}.png`);
    const metaPath = path.join(outputDir, `${baseName}.json`);

    try {
      await page.waitForTimeout(300);
      await page.screenshot({ path: imagePath, fullPage: true });
      const title = await page.title().catch(() => "");
      await writeFile(
        metaPath,
        JSON.stringify({ url, title, reason, capturedAt: new Date().toISOString() }, null, 2),
        "utf8",
      );
      log.info("[Debug] 已保存页面截图", { reason, url, imagePath });
    } catch (error) {
      log.warn("[Debug] 页面截图失败", {
        reason,
        url,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  page.on("framenavigated", (frame) => {
    if (frame !== page.mainFrame()) {
      return;
    }
    void captureCurrent("navigation");
  });

  return { captureCurrent };
}
