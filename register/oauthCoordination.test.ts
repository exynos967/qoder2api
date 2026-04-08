import assert from 'node:assert/strict';
import test from 'node:test';

import { waitForOauthAndPureLogin } from './utils/oauthCoordination.ts';

test('OAuth 先失败时应立即抛出，不继续等待 pure login 超时', async () => {
  const oauthError = new Error('oauth failed early');
  const pureLoginResult = new Promise<Record<string, unknown>>((resolve) => {
    setTimeout(() => resolve({ token: 'late-success' }), 200);
  });

  await assert.rejects(
    waitForOauthAndPureLogin(Promise.reject(oauthError), pureLoginResult),
    /oauth failed early/,
  );
});

test('OAuth 先成功时应继续等待 pure login 结果', async () => {
  const summary = await waitForOauthAndPureLogin(
    Promise.resolve(),
    new Promise<Record<string, unknown>>((resolve) => {
      setTimeout(() => resolve({ token: 'summary-ok' }), 20);
    }),
  );

  assert.deepEqual(summary, { token: 'summary-ok' });
});

test('pure login 先成功时应直接返回结果', async () => {
  const summary = await waitForOauthAndPureLogin(
    new Promise<void>((resolve) => {
      setTimeout(resolve, 200);
    }),
    Promise.resolve({ token: 'pure-first' }),
  );

  assert.deepEqual(summary, { token: 'pure-first' });
});
