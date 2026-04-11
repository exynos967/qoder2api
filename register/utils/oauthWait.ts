export function isClosedPageLifecycleError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  const normalized = message.toLowerCase();
  return (
    normalized.includes("target page, context or browser has been closed")
    || normalized.includes("page closed")
    || normalized.includes("browser has been closed")
    || normalized.includes("context has been closed")
  );
}
