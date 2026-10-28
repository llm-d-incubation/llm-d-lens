/** JSON transport shared by domain clients; streaming/downloads use fetch directly. */
export function problemError(payload, response) {
    const detail = payload?.detail;
    const structured = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : {};
    const base = structured.message || structured.code || payload?.error
        || (typeof detail === 'string' ? detail : null) || payload?.message || payload?.title
        || `Request failed (${response.status})`;
    // Surface the server's request id so the full traceback can be found in logs.
    const message = payload?.requestId ? `${base} (ref ${payload.requestId})` : base;
    const error = new Error(message);
    error.status = response.status;
    error.code = payload?.code || structured.code || '';
    error.title = payload?.title || '';
    error.details = detail ?? payload?.error ?? null;
    error.retryable = Boolean(structured.retryable ?? payload?.retryable);
    error.body = payload;
    return error;
}

export async function readJson(response, fallback = {}) {
    if (response.status === 204) return fallback;
    return response.json().catch(() => fallback);
}

function readCookie(name) {
    if (typeof document === 'undefined') return '';
    const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    return match ? decodeURIComponent(match[1]) : '';
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

let accessToken = '';
let unauthorizedHandler = null;
let refreshPromise = null;

/** Keep the short-lived Bearer token in memory; never persist it in storage. */
export function setAuthToken(token) {
    accessToken = token || '';
}

export function getAuthToken() {
    return accessToken;
}

/** Register one refresh callback used to recover from a 401. */
export function setUnauthorizedHandler(handler) {
    unauthorizedHandler = handler;
}

/** Share one refresh across all concurrent 401 responses. */
export async function refreshAccessTokenOnce() {
    if (typeof unauthorizedHandler !== 'function') return false;
    if (!refreshPromise) {
        refreshPromise = Promise.resolve()
            .then(() => unauthorizedHandler())
            .then((value) => Boolean(value))
            .catch(() => false)
            .finally(() => {
                refreshPromise = null;
            });
    }
    return refreshPromise;
}

/** Fetch a raw response (download/SSE) with one shared 401 refresh-and-retry. */
export async function fetchWithAuthRetry(url, options = {}, { retryOnUnauthorized = true } = {}) {
    const token = getAuthToken();
    let headers = options.headers;
    if (token) {
        headers = new Headers(options.headers);
        headers.set('Authorization', `Bearer ${token}`);
    }
    const response = await fetch(url, {
        ...options,
        headers,
        credentials: options.credentials || (token ? 'omit' : 'include'),
    });
    if (response.status === 401 && token && retryOnUnauthorized) {
        const refreshed = await refreshAccessTokenOnce();
        if (refreshed) {
            return fetchWithAuthRetry(url, options, { retryOnUnauthorized: false });
        }
        if (typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('prism:auth-expired'));
        }
    }
    return response;
}

export async function requestJson(
    url,
    options = {},
    {
        fallback = {},
        errorFactory = problemError,
        strictJson = false,
        expectedStatus,
        auth = 'auto',
        retryOnUnauthorized = true,
    } = {},
) {
    const headers = new Headers(options.headers);
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    const method = (options.method || 'GET').toUpperCase();
    const useBearer = auth !== 'none' && Boolean(accessToken);
    if (useBearer) headers.set('Authorization', `Bearer ${accessToken}`);
    if (!SAFE_METHODS.has(method) && !useBearer && !headers.has('X-Prism-CSRF')) {
        const csrf = readCookie('prism_csrf');
        if (csrf) headers.set('X-Prism-CSRF', csrf);
    }
    const credentials = options.credentials || (useBearer ? 'omit' : 'include');
    const response = await fetch(url, { ...options, credentials, headers });
    if (
        response.status === 401
        && useBearer
        && retryOnUnauthorized
        && typeof unauthorizedHandler === 'function'
    ) {
        const refreshed = await refreshAccessTokenOnce();
        if (refreshed) {
            return requestJson(url, options, {
                fallback,
                errorFactory,
                strictJson,
                expectedStatus,
                auth,
                retryOnUnauthorized: false,
            });
        }
    }
    const payload = strictJson && response.ok ? await response.json() : await readJson(response, fallback);
    if (!response.ok || (expectedStatus !== undefined && response.status !== expectedStatus)) {
        if (response.status === 401 && typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('prism:auth-expired'));
        }
        throw errorFactory(payload, response);
    }
    return payload;
}

export function postJson(url, body, options = {}, requestOptions = {}) {
    const headers = new Headers(options.headers);
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    return requestJson(url, { ...options, method: 'POST', headers, body: JSON.stringify(body) }, requestOptions);
}
