import assert from 'node:assert/strict';
import {
  classifyRequestFailures,
  isExpectedPrefetchAbort,
  isExpectedRscRetryAbort,
  snapshotRequest,
} from '../scripts/vqa-network.mjs';
import {
  applyViewportRequestEvidence,
  completeCaptureManifest,
  observeCaptureRequests,
  recordCompletedRoute,
} from '../scripts/vqa-capture-state.mjs';

function request({
  url = 'https://example.test/dashboard',
  errorText = 'net::ERR_ABORTED',
  headers = {},
  method = 'GET',
  navigation = false,
  status = 200,
} = {}) {
  return {
    failure: () => ({ errorText }),
    headers: () => headers,
    isNavigationRequest: () => navigation,
    method: () => method,
    response: async () => ({ status: () => status }),
    url: () => url,
  };
}

class FakePage {
  handlers = new Map();

  on(event, handler) {
    const handlers = this.handlers.get(event) || [];
    handlers.push(handler);
    this.handlers.set(event, handlers);
  }

  emit(event, value) {
    for (const handler of this.handlers.get(event) || []) handler(value);
  }
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
  ['302 redirect is not a successful retry', abortedRsc, [{ ...completedRetry, status: 302 }], { expectedStateReached: true, pageErrors: [] }],
  ['307 redirect is not a successful retry', abortedRsc, [{ ...completedRetry, status: 307 }], { expectedStateReached: true, pageErrors: [] }],
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

const page = new FakePage();
const observed = observeCaptureRequests(page, {
  baseUrl: 'https://example.test',
  isInfraNoise: () => false,
});
const olderRequest = request({
  url: 'https://example.test/labs/mass-vs-weight?_rsc=older',
  headers: { rsc: '1' },
});
const abortedAfterOlderStarted = request({
  url: 'https://example.test/labs/mass-vs-weight?_rsc=aborted',
  headers: { rsc: '1' },
});

page.emit('request', olderRequest);
page.emit('request', abortedAfterOlderStarted);
page.emit('requestfailed', abortedAfterOlderStarted);
page.emit('requestfinished', olderRequest);
await observed.waitForCompletedRequests();
assert.deepEqual(classifyRequestFailures(
  observed.failedRequests,
  observed.completedRequests,
  { expectedStateReached: true, pageErrors: [] },
), { ignored: [], failures: [observed.failedRequests[0]] });

console.log('ok - request-start order prevents an older concurrent completion from hiding a later abort');

const missingStartPage = new FakePage();
const missingStartObserved = observeCaptureRequests(missingStartPage, {
  baseUrl: 'https://example.test',
  isInfraNoise: () => false,
});
const missingStartAbort = request({
  url: 'https://example.test/labs/mass-vs-weight?_rsc=missing-start',
  headers: { rsc: '1' },
});
const laterRetry = request({
  url: 'https://example.test/labs/mass-vs-weight?_rsc=later',
  headers: { rsc: '1' },
});
missingStartPage.emit('requestfailed', missingStartAbort);
missingStartPage.emit('request', laterRetry);
missingStartPage.emit('requestfinished', laterRetry);
await missingStartObserved.waitForCompletedRequests();
assert.deepEqual(classifyRequestFailures(
  missingStartObserved.failedRequests,
  missingStartObserved.completedRequests,
  { expectedStateReached: true, pageErrors: [] },
), { ignored: [], failures: [missingStartObserved.failedRequests[0]] });

console.log('ok - a missing request-start event remains a blocking failure');

const retriedPage = new FakePage();
const retriedObserved = observeCaptureRequests(retriedPage, {
  baseUrl: 'https://example.test',
  isInfraNoise: () => false,
});
const aborted = request({
  url: 'https://example.test/labs/mass-vs-weight?_rsc=aborted',
  headers: { rsc: '1' },
});
const retry = request({
  url: 'https://example.test/labs/mass-vs-weight?_rsc=retry',
  headers: { rsc: '1' },
  status: 204,
});
retriedPage.emit('request', aborted);
retriedPage.emit('requestfailed', aborted);
retriedPage.emit('request', retry);
retriedPage.emit('requestfinished', retry);

const route = {
  route: '/labs/mass-vs-weight',
  url: 'https://example.test/labs/mass-vs-weight',
  shots: [{ width: 1280, name: 'desktop', file: '/tmp/shot.png', status: 200, blank: false, timedOut: false }],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  ignoredRequestFailures: [],
  hardFail: false,
  reasons: [],
};
await applyViewportRequestEvidence(route, retriedObserved, {
  viewport: 'desktop',
  expectedStateReached: true,
  pageErrors: [],
});
const captureManifest = {
  routes: [],
  summary: { total: 1, hardFailures: 0, verdict: 'PASS' },
};
recordCompletedRoute(captureManifest, route);
completeCaptureManifest(captureManifest);

assert.equal(captureManifest.summary.verdict, 'PASS');
assert.deepEqual(captureManifest.routes[0].failedRequests, []);
assert.deepEqual(captureManifest.routes[0].ignoredRequestFailures, [{
  viewport: 'desktop',
  method: 'GET',
  url: 'https://example.test/labs/mass-vs-weight?_rsc=aborted',
  error: 'net::ERR_ABORTED',
}]);

console.log('ok - capture manifest passes with one fully evidenced ignored RSC retry abort');
