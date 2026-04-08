const URL_PATTERN = /\bhttps?:\/\/[^\s<>"')\]}]+/gi;
const REDACTED_URL = '[url-hidden]';

export function sanitizeLogText(text: string): string {
  if (!text) {
    return text;
  }
  return text.replace(URL_PATTERN, REDACTED_URL);
}

export function sanitizeLogMeta<T>(value: T): T {
  if (typeof value === 'string') {
    return sanitizeLogText(value) as T;
  }
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeLogMeta(item)) as T;
  }
  if (!value || typeof value !== 'object') {
    return value;
  }

  const sanitized: Record<string, unknown> = {};
  for (const [key, nestedValue] of Object.entries(value)) {
    sanitized[key] = sanitizeLogMeta(nestedValue);
  }
  return sanitized as T;
}
