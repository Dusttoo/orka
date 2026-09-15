const ABORT_ERRORS = /^(?:net::ERR_ABORTED|NS_BINDING_ABORTED)$/i;

function requestHeaders(request) {
  return typeof request.headers === 'function' ? request.headers() : (request.headers || {});
}

function requestFailure(request) {
  return typeof request.failure === 'function' ? request.failure() : request.failure;
}

function requestValue(request, name) {
  return typeof request[name] === 'function' ? request[name]() : request[name];
}

export function snapshotRequest(request, order, status = null) {
  return {
    order,
    url: requestValue(request, 'url'),
    method: requestValue(request, 'method'),
    headers: requestHeaders(request),
    navigation: Boolean(requestValue(request, 'isNavigationRequest')),
    errorText: requestFailure(request)?.errorText || '',
    status,
  };
}

export function isExpectedPrefetchAbort(request) {
  const errorText = requestFailure(request)?.errorText || request.errorText || '';
  if (!/^(?:net::ERR_ABORTED|NS_BINDING_ABORTED)$/i.test(errorText)) return false;
  if (Boolean(requestValue(request, 'isNavigationRequest') ?? request.navigation)) return false;

  const headers = requestHeaders(request);
  const purpose = `${headers.purpose || ''} ${headers['sec-purpose'] || ''}`;
  return (
    headers['next-router-prefetch'] === '1' ||
    /\bprefetch\b/i.test(purpose)
  );
}

function urlIdentity(value) {
  try {
    const url = new URL(value);
    return { origin: url.origin, pathname: url.pathname, rsc: url.searchParams.has('_rsc') };
  } catch {
    return null;
  }
}

export function isExpectedRscRetryAbort(
  failed,
  { completedRequests = [], expectedStateReached = false, pageErrors = [] } = {},
) {
  if (!ABORT_ERRORS.test(failed.errorText || '')) return false;
  if (failed.navigation || failed.method !== 'GET') return false;
  if (failed.headers?.rsc !== '1') return false;
  if (!expectedStateReached || pageErrors.length > 0) return false;

  const failedUrl = urlIdentity(failed.url);
  if (!failedUrl?.rsc) return false;

  return completedRequests.some((completed) => {
    if (completed.order <= failed.order) return false;
    if (completed.method !== failed.method) return false;
    if (completed.headers?.rsc !== '1') return false;
    if (completed.status == null || completed.status < 200 || completed.status >= 300) return false;
    const completedUrl = urlIdentity(completed.url);
    return Boolean(
      completedUrl?.rsc &&
      completedUrl.origin === failedUrl.origin &&
      completedUrl.pathname === failedUrl.pathname
    );
  });
}

export function classifyRequestFailures(
  failedRequests,
  completedRequests,
  { expectedStateReached = false, pageErrors = [] } = {},
) {
  const ignored = [];
  const failures = [];
  for (const failed of failedRequests) {
    const expected = isExpectedPrefetchAbort(failed) || isExpectedRscRetryAbort(failed, {
      completedRequests,
      expectedStateReached,
      pageErrors,
    });
    (expected ? ignored : failures).push(failed);
  }
  return { ignored, failures };
}
