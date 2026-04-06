export const AUTO_MAX_ATTEMPTS = 15;
export const AUTO_TOTAL_TIMEOUT_MS = 300_000;

export function canStartAutoCaptchaAttempt(
  completedAttempts: number,
  startedAtMs: number,
  nowMs: number,
  options?: {
    maxAttempts?: number;
    totalTimeoutMs?: number;
  },
): boolean {
  const maxAttempts = options?.maxAttempts ?? AUTO_MAX_ATTEMPTS;
  const totalTimeoutMs = options?.totalTimeoutMs ?? AUTO_TOTAL_TIMEOUT_MS;
  return completedAttempts < maxAttempts && (nowMs - startedAtMs) < totalTimeoutMs;
}
