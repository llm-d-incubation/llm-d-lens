import { requestJson } from '../../../shared/api/httpClient';

const base = '/api/v1/welcome';

function query(params) {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
    });
    return search.toString();
}

export function getWelcomeOverview({ clusterId, refresh = false, signal } = {}) {
    const search = query({ clusterId, refresh: refresh ? 'true' : undefined });
    return requestJson(`${base}/overview${search ? `?${search}` : ''}`, { signal });
}

export function getWelcomeIssues({
    clusterId,
    severity,
    category,
    search = '',
    cursor,
    limit = 25,
    signal,
} = {}) {
    const suffix = query({ clusterId, severity, category, search, cursor, limit });
    return requestJson(`${base}/issues${suffix ? `?${suffix}` : ''}`, { signal });
}
