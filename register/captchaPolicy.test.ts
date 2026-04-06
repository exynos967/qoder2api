import assert from 'node:assert/strict';
import test from 'node:test';

import { AUTO_MAX_ATTEMPTS, AUTO_TOTAL_TIMEOUT_MS, canStartAutoCaptchaAttempt } from './utils/captchaPolicy.ts';

test('自动过码在未超时且未达到 15 次前允许继续', () => {
  assert.equal(canStartAutoCaptchaAttempt(14, 0, AUTO_TOTAL_TIMEOUT_MS - 1), true);
});

test('自动过码达到 15 次后禁止继续', () => {
  assert.equal(canStartAutoCaptchaAttempt(AUTO_MAX_ATTEMPTS, 0, 1), false);
});

test('自动过码超时后禁止继续', () => {
  assert.equal(canStartAutoCaptchaAttempt(1, 0, AUTO_TOTAL_TIMEOUT_MS), false);
});
