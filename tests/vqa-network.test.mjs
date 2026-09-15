import assert from 'node:assert/strict';
import {
  classifyRequestFailures,
  isExpectedPrefetchAbort,
  isExpectedRscRetryAbort,
  snapshotRequest,
} from '../scripts/vqa-network.mjs';

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

const abortedRsc = {
  order: 2,
  url: 'https://example.test/labs/mass-vs-weight?_rsc=first',
  method: 'GET',
  headers: { rsc: '1' },
  navigation: false,
  errorText: 'net::ERR_ABORTED',
};
const completedRetry = {
  order: 3,
  url: 'https://example.test/labs/mass-vs-weight?_rsc=replacement',
  method: 'GET',
  headers: { rsc: '1' },
  navigation: false,
  status: 200,
};

assert.equal(isExpectedRscRetryAbort(abortedRsc, {
  completedRequests: [completedRetry],
  expectedStateReached: true,
  pageErrors: [],
}), true);
assert.deepEqual(classifyRequestFailures([abortedRsc], [completedRetry], {
  expectedStateReached: true,
  pageErrors: [],
}), { ignored: [abortedRsc], failures: [] });

for (const [name, failed, completed, context] of [
  ['no successful retry', abortedRsc, [], { expectedStateReached: true, pageErrors: [] }],
  ['retry is earlier', abortedRsc, [{ ...completedRetry, order: 1 }], { expectedStateReached: true, pageErrors: [] }],
  ['different pathname', abortedRsc, [{ ...completedRetry, url: 'https://example.test/labs/other?_rsc=x' }], { expectedStateReached: true, pageErrors: [] }],
  ['different origin', abortedRsc, [{ ...completedRetry, url: 'https://other.test/labs/mass-vs-weight?_rsc=x' }], { expectedStateReached: true, pageErrors: [] }],
  ['failed retry', abortedRsc, [{ ...completedRetry, status: 500 }], { expectedStateReached: true, pageErrors: [] }],
  ['page did not reach expected state', abortedRsc, [completedRetry], { expectedStateReached: false, pageErrors: [] }],
  ['page error occurred', abortedRsc, [completedRetry], { expectedStateReached: true, pageErrors: ['boom'] }],
  ['document navigation', { ...abortedRsc, navigation: true }, [completedRetry], { expectedStateReached: true, pageErrors: [] }],
  ['non-RSC abort', { ...abortedRsc, headers: {} }, [completedRetry], { expectedStateReached: true, pageErrors: [] }],
]) {
  assert.equal(isExpectedRscRetryAbort(failed, { completedRequests: completed, ...context }), false, name);
}

const snap = snapshotRequest(request({
  url: 'https://example.test/labs/mass-vs-weight?_rsc=x',
  headers: { rsc: '1' },
}), 7, 204);
assert.equal(snap.order, 7);
assert.equal(snap.status, 204);
assert.equal(snap.headers.rsc, '1');

console.log('ok - aborted RSC navigation requests require a later successful exact-path retry');
