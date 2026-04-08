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

test('register progress 日志中的 URL 默认隐藏', () => {
  const line = formatWorkerProgressLine('oauth', '[13:36:11] [INFO] Cookie 登录成功，当前 URL: https://qoder.com/device/selectAccounts?nonce=abc');
  assert.equal(line, '[oauth] Cookie 登录成功，当前 URL: [url-hidden]');
});
