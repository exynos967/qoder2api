import assert from 'node:assert/strict';
import test from 'node:test';

import { sanitizeLogMeta, sanitizeLogText } from './utils/logRedaction.ts';

test('日志文本中的 URL 默认隐藏', () => {
  assert.equal(
    sanitizeLogText('Cookie 登录成功，当前 URL: https://qoder.com/device/selectAccounts?foo=bar'),
    'Cookie 登录成功，当前 URL: [url-hidden]',
  );
});

test('日志元数据中的 URL 默认隐藏', () => {
  assert.deepEqual(
    sanitizeLogMeta({
      loginUrl: 'https://qoder.com/device/selectAccounts?foo=bar',
      nested: {
        backendUrl: 'http://127.0.0.1:28001',
      },
      list: ['https://example.test/a', 'plain-text'],
    }),
    {
      loginUrl: '[url-hidden]',
      nested: {
        backendUrl: '[url-hidden]',
      },
      list: ['[url-hidden]', 'plain-text'],
    },
  );
});
