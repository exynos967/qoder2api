import assert from "node:assert/strict";
import test from "node:test";

import { isClosedPageLifecycleError } from "./utils/oauthWait.ts";

test("识别 Playwright 页面已关闭错误", () => {
  assert.equal(
    isClosedPageLifecycleError(new Error("page.waitForTimeout: Target page, context or browser has been closed")),
    true,
  );
  assert.equal(
    isClosedPageLifecycleError(new Error("Target page, context or browser has been closed")),
    true,
  );
  assert.equal(
    isClosedPageLifecycleError(new Error("browserContext.cookies: Target page, context or browser has been closed")),
    true,
  );
});

test("非关闭类错误不应误判", () => {
  assert.equal(
    isClosedPageLifecycleError(new Error("等待 OAuth 回调超时（>180s）")),
    false,
  );
});
