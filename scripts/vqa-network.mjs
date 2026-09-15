export function isExpectedPrefetchAbort(request) {
  const errorText = request.failure()?.errorText || '';
  if (!/^(?:net::ERR_ABORTED|NS_BINDING_ABORTED)$/i.test(errorText)) return false;
  if (request.isNavigationRequest()) return false;

  const headers = request.headers();
  const purpose = `${headers.purpose || ''} ${headers['sec-purpose'] || ''}`;
  let hasRscQuery = false;
  try {
    hasRscQuery = new URL(request.url()).searchParams.has('_rsc');
  } catch {
    return false;
  }
  return (
    hasRscQuery ||
    headers['next-router-prefetch'] === '1' ||
    /\bprefetch\b/i.test(purpose)
  );
}
