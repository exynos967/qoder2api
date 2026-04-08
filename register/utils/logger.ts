import { sanitizeLogMeta, sanitizeLogText } from "./logRedaction";

function nowTime(): string {
  return new Date().toLocaleTimeString("zh-CN", {
    hour12: false,
  });
}

function formatMeta(meta?: unknown): string {
  if (meta === undefined) {
    return "";
  }

  if (typeof meta === "string") {
    return ` ${sanitizeLogText(meta)}`;
  }

  try {
    return ` ${JSON.stringify(sanitizeLogMeta(meta))}`;
  } catch {
    return ` ${sanitizeLogText(String(meta))}`;
  }
}

function print(level: string, message: string, meta?: unknown): void {
  console.log(`[${nowTime()}] [${level}] ${sanitizeLogText(message)}${formatMeta(meta)}`);
}

export const log = {
  info(message: string, meta?: unknown): void {
    print("INFO", message, meta);
  },

  warn(message: string, meta?: unknown): void {
    print("WARN", message, meta);
  },

  error(message: string, meta?: unknown): void {
    print("ERROR", message, meta);
  },

  debug(message: string, meta?: unknown): void {
    print("DEBUG", message, meta);
  },

  banner(lines: string[]): void {
    const border = "=".repeat(60);
    console.log(border);
    for (const line of lines) {
      console.log(line);
    }
    console.log(border);
  },
};
