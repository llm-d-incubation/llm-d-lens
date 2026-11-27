// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { requestJson as sharedRequestJson } from '../../../shared/api/httpClient';

function apiError(payload, fallback) {
    const detail = payload?.detail || payload?.error || payload?.message;
    if (Array.isArray(detail)) {
        return detail.map((item) => item.msg || item.message || JSON.stringify(item)).join('; ');
    }
    return typeof detail === 'string' ? detail : fallback;
}

export async function requestJson(url, options) {
    return sharedRequestJson(url, options, {
        fallback: null,
        errorFactory: (payload, response) => {
            const error = new Error(apiError(payload, `${response.status} ${response.statusText}`));
            error.status = response.status;
            return error;
        },
    });
}

export async function getLiveTelemetry(taskId, options = {}) {
    return requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/live-telemetry`, options);
}

export async function getTaskTelemetry(taskId, { section = 'all', warmupSeconds } = {}, options = {}) {
    const query = new URLSearchParams();
    if (section) query.set('section', section);
    if (warmupSeconds !== undefined && warmupSeconds !== null && warmupSeconds !== '') {
        query.set('warmup_seconds', String(warmupSeconds));
    }
    const qs = query.toString();
    return requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/telemetry${qs ? `?${qs}` : ''}`, options);
}

export async function getResponseCodeIssues(
    taskId,
    { statusCode = 'all', offset = 0, limit = 100, sort = 'newest' } = {},
    options = {},
) {
    const resolvedCode = statusCode === null || statusCode === undefined || statusCode === ''
        ? 'all'
        : String(statusCode);
    const query = new URLSearchParams({
        status_code: resolvedCode,
        offset: String(Math.max(0, Number(offset) || 0)),
        limit: String(Math.max(1, Number(limit) || 100)),
        sort: String(sort || 'newest'),
    });
    return requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/response-code-issues?${query.toString()}`, options);
}

export async function getMultipleTaskTelemetry(taskIds, options = {}) {
    const ids = Array.isArray(taskIds) ? taskIds.filter(Boolean) : [];
    return Promise.all(ids.map((taskId) => getTaskTelemetry(taskId, { section: 'all' }, options)));
}

export async function listAIProviders(options = {}) {
    const payload = await requestJson('/api/v1/ai-providers', options);
    return Array.isArray(payload?.items) ? payload.items : [];
}

export async function analyzeSimulationTaskWithAI(taskId, payload = {}, options = {}) {
    const headers = new Headers(options.headers);
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    return requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/ai-analysis`, {
        ...options,
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
    });
}
