export type CaptchaMode = "auto" | "manual";

export interface CaptchaSolveResult {
  success: boolean;
  durationMs: number;
  error?: string;
}
