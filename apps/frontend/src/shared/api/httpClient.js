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
    error.executionToken = response?.headers?.get('X-Lens-Execution-Token') || null;
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

/** Execution headers shared by the guarded provisioning steps. */
export const PLAN_HASH_HEADER = 'X-Lens-Plan-Hash';
export const EXECUTION_TOKEN_HEADER = 'X-Lens-Execution-Token';

export function executionHeaders({ planHash, token } = {}, base = undefined) {
    const headers = new Headers(base);
    if (planHash) headers.set(PLAN_HASH_HEADER, planHash);
    if (token) headers.set(EXECUTION_TOKEN_HEADER, token);
    return headers;
}

export function executionToken(response) {
    return response?.headers?.get(EXECUTION_TOKEN_HEADER) || null;
}

export async function requestJson(url, options = {}, { fallback = {}, errorFactory = problemError, strictJson = false, expectedStatus, returnMeta = false } = {}) {
    const headers = new Headers(options.headers);
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    const method = (options.method || 'GET').toUpperCase();
    if (!SAFE_METHODS.has(method) && !headers.has('X-Prism-CSRF')) {
        const csrf = readCookie('prism_csrf');
        if (csrf) headers.set('X-Prism-CSRF', csrf);
    }
    const response = await fetch(url, { credentials: 'include', ...options, headers });
    const payload = strictJson && response.ok ? await response.json() : await readJson(response, fallback);
    if (!response.ok || (expectedStatus !== undefined && response.status !== expectedStatus)) {
        const code = payload?.code || payload?.detail?.code;
        if (response.status === 401 && (!code || code === 'unauthenticated' || code === 'session_expired') && typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('prism:auth-expired'));
        }
        throw errorFactory(payload, response);
    }
    // Opt-in metadata keeps every existing caller's return value unchanged.
    if (returnMeta) return { body: payload, response };
    return payload;
}

/** JSON request that also returns the raw response for response-header reads. */
export function requestJsonWithMeta(url, options = {}, config = {}) {
    return requestJson(url, options, { ...config, returnMeta: true });
}

export function postJson(url, body, options = {}) {
    const headers = new Headers(options.headers);
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    return requestJson(url, { ...options, method: 'POST', headers, body: JSON.stringify(body) });
}
