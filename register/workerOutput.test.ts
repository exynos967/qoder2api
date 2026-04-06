import assert from 'node:assert/strict';
import test from 'node:test';

import { formatWorkerProgressLine } from './utils/workerOutput.ts';

test('register info 日志应去掉子进程时间与级别前缀', () => {
  const line = formatWorkerProgressLine('register', '[02:17:47] [INFO] [Auto] Attempt 4...');
  assert.equal(line, '[register] [Auto] Attempt 4...');
});

test('register warn 日志应保留 warn 语义但去掉重复前缀', () => {
  const line = formatWorkerProgressLine('register', '[02:18:05] [WARN] [Auto] Drag execution failed {"error":"slider not visible after waiting"}');
  assert.equal(line, '[warn] [register] [Auto] Drag execution failed {"error":"slider not visible after waiting"}');
});
