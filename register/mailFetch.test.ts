import assert from "node:assert/strict";
import test from "node:test";

import { HttpStatusError, fetchJsonWithRetry, isRetryableMailRequestError } from "./utils/mailFetch.ts";

test("mail fetch 在 transient fetch failed 时会重试后成功", async () => {
  let attempts = 0;

  const result = await fetchJsonWithRetry<{ success: boolean }>(
    "https://example.test/messages",
    { method: "GET" },
    {
      attempts: 3,
      retryDelayMs: 0,
      fetchImpl: async () => {
        attempts += 1;
        if (attempts === 1) {
          throw new TypeError("fetch failed");
        }
        return new Response(JSON.stringify({ success: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    },
  );

  assert.equal(attempts, 2);
  assert.equal(result.success, true);
});

test("mail fetch 在 HTTP 503 时会重试", async () => {
  let attempts = 0;

  const result = await fetchJsonWithRetry<{ ok: boolean }>(
    "https://example.test/accounts",
    { method: "POST" },
    {
      attempts: 3,
      retryDelayMs: 0,
      fetchImpl: async () => {
        attempts += 1;
        if (attempts < 3) {
          return new Response("busy", { status: 503, statusText: "Service Unavailable" });
        }
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    },
  );

  assert.equal(attempts, 3);
  assert.equal(result.ok, true);
});

test("mail fetch 在 HTTP 401 时不会重试", async () => {
  let attempts = 0;

  await assert.rejects(
    fetchJsonWithRetry("https://example.test/messages", { method: "GET" }, {
      attempts: 3,
      retryDelayMs: 0,
      fetchImpl: async () => {
        attempts += 1;
        return new Response("unauthorized", { status: 401, statusText: "Unauthorized" });
      },
    }),
    (error: unknown) => {
      assert.equal(error instanceof HttpStatusError, true);
      assert.equal((error as HttpStatusError).status, 401);
      return true;
    },
  );

  assert.equal(attempts, 1);
});

test("isRetryableMailRequestError 能识别 fetch failed", () => {
  assert.equal(isRetryableMailRequestError(new TypeError("fetch failed")), true);
  assert.equal(isRetryableMailRequestError(new HttpStatusError(429, "Too Many Requests")), true);
  assert.equal(isRetryableMailRequestError(new HttpStatusError(401, "Unauthorized")), false);
});
