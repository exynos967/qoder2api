import assert from 'node:assert/strict';
import test from 'node:test';

import { WorkerLifecycleController } from './utils/workerLifecycle.ts';

test('shutdown 后应取消当前会话并屏蔽后续会话上报', async () => {
  const controller = new WorkerLifecycleController();
  const cancelled: string[] = [];

  controller.trackSession('session-1');

  assert.equal(controller.canSendSessionUpdate('session-1'), true);

  await controller.shutdown(async (sessionId) => {
    cancelled.push(sessionId);
  });

  assert.deepEqual(cancelled, ['session-1']);
  assert.equal(controller.canSendSessionUpdate('session-1'), false);
});

test('shutdown 只应执行一次 cancel 与 terminator', async () => {
  const controller = new WorkerLifecycleController();
  const cancelled: string[] = [];
  let terminated = 0;

  controller.trackSession('session-2');
  controller.registerTerminator(() => {
    terminated += 1;
  });

  await Promise.all([
    controller.shutdown(async (sessionId) => {
      cancelled.push(sessionId);
    }),
    controller.shutdown(async (sessionId) => {
      cancelled.push(`${sessionId}-duplicate`);
    }),
  ]);

  assert.equal(terminated, 1);
  assert.deepEqual(cancelled, ['session-2']);
});
