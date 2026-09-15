import assert from 'node:assert/strict';
import { isExpectedPrefetchAbort } from '../scripts/vqa-network.mjs';

function request({
  url = 'https://example.test/dashboard',
  errorText = 'net::ERR_ABORTED',
  headers = {},
  navigation = false,
} = {}) {
  return {
    failure: () => ({ errorText }),
    headers: () => headers,
    isNavigationRequest: () => navigation,
    url: () => url,
  };
}

assert.equal(
  isExpectedPrefetchAbort(
    request({ url: 'https://example.test/dashboard?_rsc=abc' }),
  ),
  false,
);
assert.equal(
  isExpectedPrefetchAbort(request({ headers: { 'next-router-prefetch': '1' } })),
  true,
);
assert.equal(
  isExpectedPrefetchAbort(request({ headers: { purpose: 'prefetch' } })),
  true,
);
assert.equal(isExpectedPrefetchAbort(request()), false);
assert.equal(isExpectedPrefetchAbort(request({ navigation: true })), false);
assert.equal(
  isExpectedPrefetchAbort(
    request({
      url: 'https://example.test/dashboard?_rsc=abc',
      errorText: 'net::ERR_FAILED',
    }),
  ),
  false,
);

console.log('ok - expected Next.js prefetch aborts are distinguished from real failures');
