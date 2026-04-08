const DEFAULT_FETCH_TIMEOUT_MS = 15_000;
const DEFAULT_RETRY_DELAY_MS = 1_500;
const RETRYABLE_HTTP_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

export interface FetchJsonRetryOptions {
  timeoutMs?: number;
  attempts?: number;
  retryDelayMs?: number;
  onRetry?: (info: { attempt: number; maxAttempts: number; error: string; url: string }) => void | Promise<void>;
  fetchImpl?: typeof fetch;
}

export class HttpStatusError extends Error {
  status: number;

  constructor(status: number, statusText: string) {
    super(`HTTP ${status} ${statusText}`);
    this.name = "HttpStatusError";
    this.status = status;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function isRetryableMailRequestError(error: unknown): boolean {
  if (error instanceof HttpStatusError) {
    return RETRYABLE_HTTP_STATUS.has(error.status);
  }

  const message = getErrorMessage(error).toLowerCase();
  return [
    "fetch failed",
    "timed out",
    "timeout",
    "aborterror",
    "networkerror",
    "econnreset",
    "econnrefused",
    "enotfound",
    "socket hang up",
  ].some((token) => message.includes(token));
}

export async function fetchJsonWithRetry<T>(
  url: string,
  init: RequestInit,
  options: FetchJsonRetryOptions = {},
): Promise<T> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS;
  const maxAttempts = Math.max(1, options.attempts ?? 1);
  const retryDelayMs = options.retryDelayMs ?? DEFAULT_RETRY_DELAY_MS;
  const fetchImpl = options.fetchImpl ?? fetch;

  let lastError: unknown;

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const response = await fetchImpl(url, {
        ...init,
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!response.ok) {
        throw new HttpStatusError(response.status, response.statusText);
      }
      return (await response.json()) as T;
    } catch (error) {
      lastError = error;
      const shouldRetry = attempt < maxAttempts && isRetryableMailRequestError(error);
      if (!shouldRetry) {
        throw error;
      }
      if (options.onRetry) {
        await options.onRetry({
          attempt,
          maxAttempts,
          error: getErrorMessage(error),
          url,
        });
      }
      await sleep(retryDelayMs);
    }
  }

  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}
