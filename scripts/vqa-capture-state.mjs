import { classifyRequestFailures, snapshotRequest } from './vqa-network.mjs';

export function observeCaptureRequests(page, { baseUrl, isInfraNoise }) {
  const failedRequests = [];
  const completedRequests = [];
  const completedRequestTasks = [];
  const requestOrders = new WeakMap();
  let requestOrder = 0;

  page.on('request', (request) => {
    const url = request.url();
    if (!url.startsWith(baseUrl) || isInfraNoise(url)) return;
    requestOrders.set(request, ++requestOrder);
  });
  page.on('requestfailed', (request) => {
    const url = request.url();
    if (!url.startsWith(baseUrl) || isInfraNoise(url)) return;
    const order = requestOrders.get(request);
    // A missing start event cannot be proven to precede a retry, so retain the
    // request as a failure with an order that no observed request can exceed.
    failedRequests.push(snapshotRequest(request, order ?? Number.MAX_SAFE_INTEGER));
  });
  page.on('requestfinished', (request) => {
    const url = request.url();
    if (!url.startsWith(baseUrl)) return;
    const order = requestOrders.get(request);
    // A terminal event without its corresponding start is not retry evidence.
    if (order == null) return;
    const task = request.response()
      .then((response) => {
        if (response) completedRequests.push(snapshotRequest(request, order, response.status()));
      })
      .catch(() => {});
    completedRequestTasks.push(task);
  });

  return {
    failedRequests,
    completedRequests,
    waitForCompletedRequests: () => Promise.all(completedRequestTasks),
  };
}

export async function applyViewportRequestEvidence(
  entry,
  observed,
  { viewport, expectedStateReached, pageErrors },
) {
  await observed.waitForCompletedRequests();
  const classified = classifyRequestFailures(
    observed.failedRequests,
    observed.completedRequests,
    { expectedStateReached, pageErrors },
  );
  entry.failedRequests.push(...classified.failures.map((request) =>
    `${request.method} ${request.url} (${request.errorText || 'failed'})`,
  ));
  entry.ignoredRequestFailures.push(...classified.ignored.map((request) => ({
    viewport,
    method: request.method,
    url: request.url,
    error: request.errorText || 'failed',
  })));
}

export function recordCompletedRoute(manifest, entry) {
  for (const shot of entry.shots) {
    if (shot.timedOut) entry.hardFail = true;
    if (shot.status != null && shot.status >= 400) {
      entry.hardFail = true;
      entry.reasons.push(`${shot.name}: HTTP ${shot.status}`);
    }
    if (shot.blank) {
      entry.hardFail = true;
      entry.reasons.push(`${shot.name}: blank or error-boundary render`);
    }
  }
  if (entry.pageErrors.length) {
    entry.hardFail = true;
    entry.reasons.push(`uncaught page error(s): ${entry.pageErrors.length}`);
  }
  if (entry.consoleErrors.length) {
    entry.hardFail = true;
    entry.reasons.push(`console error(s): ${entry.consoleErrors.length}`);
  }
  if (entry.failedRequests.length) {
    entry.hardFail = true;
    entry.reasons.push(`failed same-origin request(s): ${entry.failedRequests.length}`);
  }

  if (entry.hardFail) manifest.summary.hardFailures += 1;
  manifest.routes.push(entry);
}

export function completeCaptureManifest(manifest) {
  manifest.summary.verdict = manifest.summary.hardFailures > 0 ? 'FAIL' : 'PASS';
  return manifest;
}
