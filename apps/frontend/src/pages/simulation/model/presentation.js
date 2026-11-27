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

import { BINARY_UNITS, scaleBytes } from '../../../shared/lib/formatBytes';

export const FALLBACK_SCENARIOS = [
    { value: 'chat', label: 'Chat' },
    { value: 'api-calling', label: 'Tool & API Use' },
    { value: 'coding', label: 'Code Generation' },
];

export const RESPONSE_CODE_ISSUE_PAGE_SIZE = 8;

export const SCENARIO_LABELS = Object.fromEntries(
    FALLBACK_SCENARIOS.map((scenario) => [scenario.value, scenario.label]),
);

export const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'canceled', 'stopped']);

export const RUNNING_STATUSES = new Set(['queued', 'pending', 'running', 'stopping', 'cancelling']);

export const STOPPABLE_STATUSES = new Set(['queued', 'pending', 'running']);

export function arrayFrom(payload, keys) {
    if (Array.isArray(payload)) return payload;
    for (const key of keys) {
        if (Array.isArray(payload?.[key])) return payload[key];
    }
    return [];
}

export function scenarioOptions(payload) {
    const items = arrayFrom(payload, ['scenarios', 'items', 'data']);
    if (!items.length) return FALLBACK_SCENARIOS;
    return items.map((item) => {
        if (typeof item === 'string') return { value: item, label: scenarioDisplayName(item) };
        const value = item.value || item.id || item.name || item.slug || item.label;
        return {
            ...item,
            value,
            label: SCENARIO_LABELS[value] || item.label || item.display_name || item.name || value,
        };
    }).filter((item) => item.value);
}

export function unwrapTask(payload) {
    return payload?.task || payload?.data?.task || payload?.data || payload;
}

function toFiniteNumber(value) {
    if (value === null || value === undefined || typeof value === 'boolean') return null;
    if (typeof value === 'string') {
        if (value.trim() === '') return null;
    } else if (typeof value !== 'number') {
        return null;
    }
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
}

export function numberAt(source, paths) {
    for (const path of paths) {
        const value = path.split('.').reduce((current, key) => current?.[key], source);
        const numeric = toFiniteNumber(value);
        if (numeric !== null) return numeric;
    }
    return null;
}

const COMPACT_NUMBER_TIERS = [
    { suffix: 'K', divisor: 1e3 },
    { suffix: 'M', divisor: 1e6 },
    { suffix: 'B', divisor: 1e9 },
    { suffix: 'T', divisor: 1e12 },
];

export function formatDecimal(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return '—';
    if (Number.isInteger(numeric)) {
        return new Intl.NumberFormat(undefined, {
            maximumFractionDigits: 0,
        }).format(numeric === 0 ? 0 : numeric);
    }
    const safeValue = Math.abs(numeric) < 0.005 ? 0 : numeric;
    return new Intl.NumberFormat(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    }).format(safeValue);
}

export function formatNumber(value, suffix = '') {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return '—';
    const abs = Math.abs(numeric);
    if (abs < 1000) {
        return `${formatDecimal(numeric)}${suffix}`;
    }
    let tierIndex = 0;
    for (let i = COMPACT_NUMBER_TIERS.length - 1; i >= 0; i -= 1) {
        if (abs >= COMPACT_NUMBER_TIERS[i].divisor) {
            tierIndex = i;
            break;
        }
    }
    if (
        tierIndex < COMPACT_NUMBER_TIERS.length - 1
        && Math.round((abs / COMPACT_NUMBER_TIERS[tierIndex].divisor) * 100) >= 100000
    ) {
        tierIndex += 1;
    }
    const tier = COMPACT_NUMBER_TIERS[tierIndex];
    const scaled = numeric / tier.divisor;
    return `${formatDecimal(scaled)}${tier.suffix}${suffix}`;
}

export function formatMilliseconds(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return '—';
    return `${formatDecimal(numeric)} ms`;
}

export function formatCount(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return '—';
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(numeric === 0 ? 0 : numeric);
}

export function clampPercent(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return null;
    return Math.min(100, Math.max(0, numeric));
}

function scaleBinaryForDisplay(numeric) {
    let { value, unit, unitIndex } = scaleBytes(numeric);
    if (unitIndex < BINARY_UNITS.length - 1 && Math.round(Math.abs(value) * 100) >= 102400) {
        unitIndex += 1;
        value /= 1024;
        unit = BINARY_UNITS[unitIndex];
    }
    return { value, unit, unitIndex };
}

export function formatBytes(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return null;
    const { value: amount, unit } = scaleBinaryForDisplay(numeric);
    return `${formatDecimal(amount)} ${unit}`;
}

export function formatByteRate(bytesPerSecond) {
    const numeric = toFiniteNumber(bytesPerSecond);
    if (numeric === null) return null;
    const { value: amount, unit } = scaleBinaryForDisplay(numeric);
    return `${formatDecimal(amount)} ${unit}/s`;
}

function formatScaledPair(primary, secondary, unitSuffix = '') {
    const primaryScale = scaleBinaryForDisplay(primary);
    const secondaryScale = scaleBinaryForDisplay(secondary);
    if (primary === 0 && secondary === 0) {
        return `0 / 0 ${primaryScale.unit}${unitSuffix}`;
    }
    if (primary === 0) {
        return `0 / ${formatDecimal(secondaryScale.value)} ${secondaryScale.unit}${unitSuffix}`;
    }
    if (secondary === 0) {
        return `${formatDecimal(primaryScale.value)} / 0 ${primaryScale.unit}${unitSuffix}`;
    }
    if (primaryScale.unitIndex === secondaryScale.unitIndex) {
        const left = formatDecimal(primaryScale.value);
        const right = formatDecimal(secondaryScale.value);
        return `${left} / ${right} ${primaryScale.unit}${unitSuffix}`;
    }
    const left = `${formatDecimal(primaryScale.value)} ${primaryScale.unit}${unitSuffix}`;
    const right = `${formatDecimal(secondaryScale.value)} ${secondaryScale.unit}${unitSuffix}`;
    return `${left} / ${right}`;
}

export function formatBytePair(primaryBytes, secondaryBytes) {
    const primary = toFiniteNumber(primaryBytes);
    const secondary = toFiniteNumber(secondaryBytes);
    if (primary === null && secondary === null) return null;
    if (primary === null) return `— / ${formatBytes(secondary)}`;
    if (secondary === null) return `${formatBytes(primary)} / —`;
    return formatScaledPair(primary, secondary);
}

export function formatByteRatePair(primaryBps, secondaryBps) {
    const primary = toFiniteNumber(primaryBps);
    const secondary = toFiniteNumber(secondaryBps);
    if (primary === null && secondary === null) return null;
    return formatScaledPair(primary ?? 0, secondary ?? 0, '/s');
}

export function selectByteUnitScale(byteValues) {
    let maxAbs = 0;
    if (Array.isArray(byteValues)) {
        for (const raw of byteValues) {
            const numeric = toFiniteNumber(raw);
            if (numeric !== null) {
                const abs = Math.abs(numeric);
                if (abs > maxAbs) maxAbs = abs;
            }
        }
    }
    const { unit, unitIndex } = scaleBinaryForDisplay(maxAbs);
    return {
        unit,
        unitIndex,
        divisor: 1024 ** unitIndex,
    };
}

export function selectByteRateUnitScale(bpsValues) {
    const scale = selectByteUnitScale(bpsValues);
    return {
        unit: `${scale.unit}/s`,
        unitIndex: scale.unitIndex,
        divisor: scale.divisor,
    };
}

export function displayName(value) {
    return String(value || '')
        .replace(/[_-]+/g, ' ')
        .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function taskBackend(task) {
    return task?.simulation?.backend || task?.backend || task?.result?.backend || '—';
}

export function taskDataset(task) {
    const explicit = task?.dataset || task?.prompt?.dataset?.name;
    if (explicit) return explicit;
    const tracePath = task?.prompt?.trace?.path || task?.trace_path;
    return tracePath ? tracePath.split('/').filter(Boolean).pop() : '—';
}

export function taskStatusChip(status) {
    const normalized = String(status || '').toLowerCase();
    if (normalized === 'completed') return 'verified';
    if (normalized === 'running') return 'running';
    if (['queued', 'pending', 'stopping', 'cancelling'].includes(normalized)) return 'pending';
    return normalized || 'inactive';
}

export function taskMetrics(task) {
    const summary = task?.result?.summary || task?.live_summary || task?.summary || {};
    const clientSeries = task?.result?.client_series || task?.client_series || {};
    const fullWindow = clientSeries?.windows?.full || summary?.windows?.full || null;
    const profilingWindow = clientSeries?.windows?.profiling || summary?.windows?.profiling || null;
    const backendOptions = task?.simulation?.backend_options || task?.backend_options || {};
    let totalRequests = numberAt(fullWindow, ['request_count', 'total_requests'])
        ?? numberAt(summary, ['total_requests', 'requests_total', 'request_count']);
    let failedRequests = numberAt(fullWindow, ['failed_requests', 'error_count'])
        ?? numberAt(summary, ['failed_requests', 'requests_failed', 'error_count']);
    if ((totalRequests === null || failedRequests === null) && Array.isArray(summary.completion_timeline)) {
        const totals = summary.completion_timeline.reduce((current, point) => ({
            total: current.total + Number(point.completed_requests || 0),
            failed: current.failed + Number(point.failed_requests || 0),
        }), { total: 0, failed: 0 });
        totalRequests = totals.total;
        failedRequests = totals.failed;
    }
    const windowRequestRate = numberAt(fullWindow, ['throughput_rps', 'request_throughput.avg', 'requests_per_second'])
        ?? numberAt(profilingWindow, ['throughput_rps', 'request_throughput.avg', 'requests_per_second']);
    const summaryRequestRate = numberAt(summary, ['throughput_rps', 'requests_per_second', 'request_throughput.avg']);
    const isRunning = RUNNING_STATUSES.has(String(task?.status || '').toLowerCase());
    const benchmarkDurationSeconds = numberAt(summary, ['duration_seconds', 'benchmark_duration.avg', 'duration'])
        ?? numberAt(clientSeries, ['duration_seconds', 'windows.full.duration_seconds', 'windows.profiling.duration_seconds']);
    const computedWindowRate = !isRunning && totalRequests > 0 && benchmarkDurationSeconds > 0
        ? totalRequests / benchmarkDurationSeconds
        : null;
    const requestRate = windowRequestRate
        ?? (computedWindowRate !== null && (summaryRequestRate === null || summaryRequestRate < computedWindowRate * 0.95)
            ? computedWindowRate
            : summaryRequestRate);

    const ttftP50 = numberAt(fullWindow, ['ttft_ms.p50', 'ttft.p50_ms', 'ttft_p50_ms'])
        ?? numberAt(profilingWindow, ['ttft_ms.p50', 'ttft.p50_ms', 'ttft_p50_ms'])
        ?? numberAt(summary, ['ttft.p50_ms', 'time_to_first_token.p50', 'ttft_p50_ms']);
    const ttftAvg = numberAt(fullWindow, ['ttft_ms.avg', 'ttft_ms.mean', 'ttft.mean_ms'])
        ?? numberAt(profilingWindow, ['ttft_ms.avg', 'ttft_ms.mean', 'ttft.mean_ms'])
        ?? numberAt(summary, ['ttft.mean_ms', 'time_to_first_token.avg', 'ttft_ms']);
    const ttft = ttftP50 ?? ttftAvg;
    const ttftStat = ttftP50 !== null ? 'P50' : (ttftAvg !== null ? 'Avg' : 'P50');

    const tpotP50 = numberAt(fullWindow, ['tpot_ms.p50', 'tpot.p50_ms', 'tpot_p50_ms'])
        ?? numberAt(profilingWindow, ['tpot_ms.p50', 'tpot.p50_ms', 'tpot_p50_ms'])
        ?? numberAt(summary, ['tpot.p50_ms', 'inter_token_latency.p50', 'tpot_p50_ms']);
    const tpotAvg = numberAt(fullWindow, ['tpot_ms.avg', 'tpot_ms.mean', 'tpot.mean_ms'])
        ?? numberAt(profilingWindow, ['tpot_ms.avg', 'tpot_ms.mean', 'tpot.mean_ms'])
        ?? numberAt(summary, ['tpot.mean_ms', 'inter_token_latency.avg', 'tpot_ms']);
    const tpot = tpotP50 ?? tpotAvg;
    const tpotStat = tpotP50 !== null ? 'P50' : (tpotAvg !== null ? 'Avg' : 'P50');

    const e2eP50 = numberAt(fullWindow, ['e2e_ms.p50', 'e2e.p50_ms', 'latency.p50_ms'])
        ?? numberAt(profilingWindow, ['e2e_ms.p50', 'e2e.p50_ms', 'latency.p50_ms'])
        ?? numberAt(summary, ['latency.p50_ms', 'request_latency.p50', 'e2e_p50_ms']);
    const e2eAvg = numberAt(fullWindow, ['e2e_ms.avg', 'e2e_ms.mean', 'e2e.mean_ms'])
        ?? numberAt(profilingWindow, ['e2e_ms.avg', 'e2e_ms.mean', 'e2e.mean_ms'])
        ?? numberAt(summary, ['latency.mean_ms', 'request_latency.avg', 'e2e_ms']);
    const e2e = e2eP50 ?? e2eAvg;
    const e2eStat = e2eP50 !== null ? 'P50' : (e2eAvg !== null ? 'Avg' : 'P50');

    const ttftSloSeconds = numberAt(backendOptions, ['ttft_slo']);
    const tpotSloSeconds = numberAt(backendOptions, ['tpot_slo']);
    const errorRateSlo = numberAt(backendOptions, ['error_rate_slo']);
    return {
        requestRate,
        errorRate: totalRequests > 0 && failedRequests !== null
            ? failedRequests / totalRequests * 100
            : null,
        ttft,
        ttftStat,
        tpot,
        tpotStat,
        e2e,
        e2eStat,
        ttftSloMs: ttftSloSeconds === null ? null : ttftSloSeconds * 1000,
        tpotSloMs: tpotSloSeconds === null ? null : tpotSloSeconds * 1000,
        errorRateSlo,
    };
}

export function findNamedSection(source, pattern) {
    if (!source || typeof source !== 'object') return null;
    for (const [key, value] of Object.entries(source)) {
        if (pattern.test(key) && value !== undefined && value !== null) return { [key]: value };
    }
    return null;
}

export function formatDurationSeconds(value) {
    const seconds = toFiniteNumber(value);
    if (seconds === null || seconds < 0) return '—';
    const totalSeconds = Math.round(seconds);
    if (totalSeconds < 60) return `${totalSeconds} s`;
    if (totalSeconds < 3600) {
        const minutes = Math.floor(totalSeconds / 60);
        const remainingSeconds = totalSeconds % 60;
        return remainingSeconds ? `${minutes} min ${remainingSeconds} s` : `${minutes} min`;
    }
    const hours = Math.floor(totalSeconds / 3600);
    const remainingMinutes = Math.floor((totalSeconds % 3600) / 60);
    return remainingMinutes ? `${hours} h ${remainingMinutes} min` : `${hours} h`;
}

export function formatDuration(task, summary, nowMs = Date.now()) {
    const running = RUNNING_STATUSES.has(String(task?.status || '').toLowerCase());
    if (running) {
        const elapsed = String(task?.progress_message || '').match(
            /Simulation running\s*·\s*([\d.]+)s elapsed/
        );
        if (elapsed) return formatDurationSeconds(Number(elapsed[1]));
        // The dashboard clock is anchored to when the backend tool
        // (aiperf) actually became ready and started running,
        // not to when the task was picked up — pod launch, data staging, and
        // on-demand tool installation shouldn't make the timer appear to be
        // running before there's really anything to measure.
        if (task?.execution_started_at) {
            const start = new Date(task.execution_started_at);
            const elapsedSeconds = (new Date(nowMs).getTime() - start.getTime()) / 1000;
            if (Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) {
                return formatDurationSeconds(elapsedSeconds);
            }
        }
        return 'Preparing…';
    }
    const reported = numberAt(summary, ['duration_seconds', 'benchmark_duration.avg', 'duration'])
        ?? numberAt(task, [
            'result.summary.duration_seconds',
            'result.summary.benchmark_duration.avg',
            'result.summary.duration',
            'result.client_series.duration_seconds',
            'result.client_series.windows.full.duration_seconds',
            'client_series.duration_seconds',
            'client_series.windows.full.duration_seconds',
            'summary.duration_seconds',
            'summary.benchmark_duration.avg',
            'summary.duration',
        ]);
    if (reported !== null && reported >= 0) {
        return formatDurationSeconds(reported);
    }
    const wallStart = task?.execution_started_at || task?.started_at;
    if (wallStart) {
        const start = new Date(wallStart);
        const end = task.completed_at ? new Date(task.completed_at) : new Date(nowMs);
        const elapsedSeconds = (end.getTime() - start.getTime()) / 1000;
        if (Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) {
            return formatDurationSeconds(elapsedSeconds);
        }
    }
    return '—';
}

export function metricDistribution(source) {
    if (!source || typeof source !== 'object') return [];
    return [
        ['Mean', numberAt(source, ['mean_ms', 'avg_ms', 'mean', 'avg'])],
        ['P50', numberAt(source, ['p50_ms', 'p50'])],
        ['P90', numberAt(source, ['p90_ms', 'p90'])],
        ['P95', numberAt(source, ['p95_ms', 'p95'])],
        ['P99', numberAt(source, ['p99_ms', 'p99'])],
        ['Max', numberAt(source, ['max_ms', 'max'])],
    ].filter(([, value]) => value !== null);
}

export function scenarioDisplayName(value) {
    return SCENARIO_LABELS[value] || displayName(value);
}

export function backendOptionLabel(key) {
    if (key === 'ttft_slo') return 'TTFT SLO';
    if (key === 'tpot_slo') return 'TPOT SLO';
    if (key === 'error_rate_slo') return 'Error Rate SLO';
    return displayName(key);
}

export function backendOptionValue(key, value) {
    if (key === 'ttft_slo' || key === 'tpot_slo') {
        const numeric = toFiniteNumber(value);
        return numeric === null ? '—' : `${formatNumber(numeric * 1000)} ms`;
    }
    if (key === 'error_rate_slo') {
        const numeric = toFiniteNumber(value);
        return numeric === null ? '—' : `${formatNumber(numeric)}%`;
    }
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export function responseStatusGuidance(statusCode) {
    if (statusCode === null || statusCode === undefined) return 'The request failed before an HTTP response code was available.';
    if (statusCode === 401 || statusCode === 403) return 'Check authentication, authorization, or tenant policy.';
    if (statusCode === 429) return 'The endpoint rate-limited requests; reduce or control offered load.';
    if (statusCode === 500) return 'The serving application encountered an internal error.';
    if (statusCode === 502 || statusCode === 503) return 'Check the upstream provider, gateway, availability, or failover path.';
    if (statusCode >= 400 && statusCode < 500) return 'The endpoint rejected the request; inspect request and policy details.';
    if (statusCode >= 500) return 'The server or an upstream dependency failed to serve the request.';
    return 'Inspect the response and task log for this status code.';
}

const PHASE_META = {
    preparing: { label: 'Preparing', tone: 'neutral' },
    warmup: { label: 'Warmup', tone: 'warning' },
    profiling: { label: 'Profiling', tone: 'info' },
    cooldown: { label: 'Cooldown', tone: 'violet' },
    collecting_telemetry: { label: 'Collecting Telemetry', tone: 'brand' },
};

export function formatPhase(phase) {
    const key = String(phase || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
    if (PHASE_META[key]) return PHASE_META[key];
    return { label: phase ? displayName(phase) : 'Preparing', tone: 'neutral' };
}

const VERDICT_LABELS = {
    error_saturation: 'Error / Saturation',
    client_schedule_drift: 'Client Schedule Drift',
    kv_cache_eviction: 'KV Cache & Eviction',
    queue_bound: 'Queue / Admission',
    prefill_bound: 'Prefill Compute',
    decode_bound: 'Decode Bound',
    routing_cache_miss: 'Routing / Cache Miss',
    healthy: 'Healthy / Within SLO',
};

export function verdictBadgeProps(verdict) {
    const isObj = verdict && typeof verdict === 'object';
    const code = String(isObj ? (verdict.code || '') : (verdict || '')).trim().toLowerCase();
    const severity = String(isObj ? (verdict.severity || '') : '').trim().toLowerCase();
    const label = (isObj && verdict.label) || VERDICT_LABELS[code] || (code ? displayName(code) : 'Unknown');
    let tone = 'neutral';
    if (['critical', 'crit', 'error', 'danger'].includes(severity)) tone = 'danger';
    else if (['warning', 'warn'].includes(severity)) tone = 'warning';
    else if (['healthy', 'ok', 'success'].includes(severity)) tone = 'success';
    else if (severity === 'info') tone = 'info';
    else if (code === 'healthy') tone = 'success';
    else if (['error_saturation', 'kv_cache_eviction'].includes(code)) tone = 'danger';
    else if (['client_schedule_drift', 'queue_bound', 'prefill_bound', 'decode_bound', 'routing_cache_miss'].includes(code)) tone = 'warning';
    return { label, tone };
}

export function alertSeverityProps(severity) {
    const normalized = String(severity || '').trim().toLowerCase();
    if (['crit', 'critical', 'error', 'danger'].includes(normalized)) return { label: 'Critical', tone: 'danger' };
    if (['warn', 'warning'].includes(normalized)) return { label: 'Warning', tone: 'warning' };
    if (['healthy', 'ok', 'success'].includes(normalized)) return { label: 'Healthy', tone: 'success' };
    if (normalized === 'info') return { label: 'Info', tone: 'info' };
    return { label: normalized ? displayName(normalized) : 'Info', tone: 'neutral' };
}

export function formatWatts(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return '—';
    return `${formatDecimal(numeric)} W`;
}

export function formatEnergyWh(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return '—';
    if (Math.abs(numeric) >= 1000) {
        return `${formatDecimal(numeric / 1000)} kWh`;
    }
    return `${formatDecimal(numeric)} Wh`;
}

export function formatDriftMs(value) {
    const numeric = toFiniteNumber(value);
    if (numeric === null) return '—';
    const isRoundedZero = Math.abs(numeric) < 0.005;
    const sign = !isRoundedZero && numeric > 0 ? '+' : '';
    return `${sign}${formatDecimal(numeric)} ms`;
}

export function isSafeHttpUrl(url) {
    if (typeof url !== 'string' || !url.trim()) return false;
    try {
        const parsed = new URL(url.trim());
        return parsed.protocol === 'http:' || parsed.protocol === 'https:';
    } catch {
        return false;
    }
}

export function formatErrorCategory(code) {
    if (code === null || code === undefined || code === '') return 'Client / Transport';
    const normalized = String(code).trim().toLowerCase();
    if (normalized === 'all') return 'All Errors';
    if (normalized === '4xx') return '4xx Client Errors';
    if (normalized === '5xx') return '5xx Server Errors';
    if (normalized === 'timeout') return 'Timeout';
    if (normalized === 'transport') return 'Transport / Stream';
    if (normalized === 'unknown') return 'Client / Transport';
    if (/^\d{3}$/.test(normalized)) return `HTTP ${normalized}`;
    return displayName(normalized);
}

export function deriveLegacyTelemetryFromTask(task) {
    const resultPayload = task?.result || task?.results || task?.output || null;
    const result = Array.isArray(resultPayload) ? resultPayload[0] || null : resultPayload;
    const summary = result?.summary || task?.live_summary || task?.summary || {};
    const backendOptions = task?.simulation?.backend_options || task?.backend_options || {};
    const totalRequests = numberAt(summary, ['total_requests', 'requests_total', 'request_count']) ?? 0;
    const successfulRequests = numberAt(summary, ['successful_requests', 'requests_success', 'success_count']) ?? 0;
    const failedRequests = numberAt(summary, ['failed_requests', 'requests_failed', 'error_count'])
        ?? Math.max(0, totalRequests - successfulRequests);
    const errorRatePct = totalRequests > 0 ? (failedRequests / totalRequests) * 100 : 0;
    const rps = numberAt(summary, ['request_throughput.avg', 'throughput_rps', 'requests_per_second']);
    const outputTps = numberAt(summary, ['output_token_throughput.avg', 'throughput_tps', 'tokens_per_second']);
    const inputTps = numberAt(summary, ['input_throughput_tps', 'input_token_throughput.avg']);
    const ttft = summary?.ttft || summary?.time_to_first_token || {};
    const tpot = summary?.tpot || summary?.inter_token_latency || {};
    const latency = summary?.latency || summary?.request_latency || {};
    const drift = summary?.drift || summary?.schedule_drift || {};
    const durationSeconds = numberAt(summary, ['duration_seconds', 'benchmark_duration.avg', 'duration'])
        ?? numberAt(task?.simulation || {}, ['duration_seconds'])
        ?? 0;
    const warmupSeconds = numberAt(backendOptions, ['warmup_seconds']) ?? 0;
    const ttftSloSeconds = numberAt(backendOptions, ['ttft_slo']);
    const tpotSloSeconds = numberAt(backendOptions, ['tpot_slo']);
    const e2eSloSeconds = numberAt(backendOptions, ['e2e_slo']);
    const errorRateSlo = numberAt(backendOptions, ['error_rate_slo']);
    const isErrorSat = totalRequests > 0 && (successfulRequests === 0 || (errorRateSlo !== null && errorRatePct > errorRateSlo));
    const verdictCode = isErrorSat ? 'error_saturation' : 'healthy';

    const ttftObj = {
        mean: numberAt(ttft, ['mean_ms', 'avg_ms', 'mean', 'avg']),
        min: numberAt(ttft, ['min_ms', 'min']),
        p50: numberAt(ttft, ['p50_ms', 'p50']),
        p90: numberAt(ttft, ['p90_ms', 'p90']),
        p95: numberAt(ttft, ['p95_ms', 'p95']),
        p99: numberAt(ttft, ['p99_ms', 'p99']),
        max: numberAt(ttft, ['max_ms', 'max']),
    };
    const tpotObj = {
        mean: numberAt(tpot, ['mean_ms', 'avg_ms', 'mean', 'avg']),
        min: numberAt(tpot, ['min_ms', 'min']),
        p50: numberAt(tpot, ['p50_ms', 'p50']),
        p90: numberAt(tpot, ['p90_ms', 'p90']),
        p95: numberAt(tpot, ['p95_ms', 'p95']),
        p99: numberAt(tpot, ['p99_ms', 'p99']),
        max: numberAt(tpot, ['max_ms', 'max']),
    };
    const e2eObj = {
        mean: numberAt(latency, ['mean_ms', 'avg_ms', 'mean', 'avg']),
        min: numberAt(latency, ['min_ms', 'min']),
        p50: numberAt(latency, ['p50_ms', 'p50']),
        p90: numberAt(latency, ['p90_ms', 'p90']),
        p95: numberAt(latency, ['p95_ms', 'p95']),
        p99: numberAt(latency, ['p99_ms', 'p99']),
        max: numberAt(latency, ['max_ms', 'max']),
    };
    const driftObj = {
        mean: numberAt(drift, ['mean_ms', 'avg_ms', 'mean', 'avg']),
        p50: numberAt(drift, ['p50_ms', 'p50']),
        p90: numberAt(drift, ['p90_ms', 'p90']),
        p95: numberAt(drift, ['p95_ms', 'p95']),
        p99: numberAt(drift, ['p99_ms', 'p99']),
        max: numberAt(drift, ['max_ms', 'max']),
    };

    const windowSummary = {
        request_count: totalRequests,
        total_requests: totalRequests,
        successful_requests: successfulRequests,
        failed_requests: failedRequests,
        good_requests: successfulRequests,
        error_rate_pct: errorRatePct,
        slo_attainment_pct: totalRequests > 0 ? Math.max(0, 100 - errorRatePct) : null,
        throughput_rps: rps,
        throughput_per_hr: rps === null ? null : rps * 3600,
        goodput_rps: rps,
        goodput_per_hr: rps === null ? null : rps * 3600,
        output_tok_per_s: outputTps,
        input_tok_per_s: inputTps,
        duration_seconds: durationSeconds,
        ttft_ms: ttftObj,
        tpot_ms: tpotObj,
        e2e_ms: e2eObj,
        drift_ms: driftObj,
        ttft: {
            mean_ms: ttftObj.mean,
            min_ms: ttftObj.min,
            p50_ms: ttftObj.p50,
            p90_ms: ttftObj.p90,
            p95_ms: ttftObj.p95,
            p99_ms: ttftObj.p99,
            max_ms: ttftObj.max,
        },
        tpot: {
            mean_ms: tpotObj.mean,
            min_ms: tpotObj.min,
            p50_ms: tpotObj.p50,
            p90_ms: tpotObj.p90,
            p95_ms: tpotObj.p95,
            p99_ms: tpotObj.p99,
            max_ms: tpotObj.max,
        },
        e2e: {
            mean_ms: e2eObj.mean,
            min_ms: e2eObj.min,
            p50_ms: e2eObj.p50,
            p90_ms: e2eObj.p90,
            p95_ms: e2eObj.p95,
            p99_ms: e2eObj.p99,
            max_ms: e2eObj.max,
        },
        drift: {
            mean_ms: driftObj.mean,
            p50_ms: driftObj.p50,
            p90_ms: driftObj.p90,
            p95_ms: driftObj.p95,
            p99_ms: driftObj.p99,
            max_ms: driftObj.max,
        },
    };

    const rawStatusBreakdown = Array.isArray(summary?.status_code_breakdown) ? summary.status_code_breakdown : [];

    return {
        task_id: task?.id || task?.task_id || '',
        telemetry_scope: 'client_only_legacy',
        legacy: true,
        partial: Boolean(task?.artifacts_incomplete),
        client_series: {
            warmup_seconds: warmupSeconds,
            duration_seconds: durationSeconds,
            windows: {
                full: windowSummary,
                profiling: { ...windowSummary, fallback_to_full: warmupSeconds <= 0 || durationSeconds <= warmupSeconds },
            },
            series: {
                throughput: Array.isArray(summary?.throughput_timeline) ? summary.throughput_timeline : [],
                completion: Array.isArray(summary?.completion_timeline) ? summary.completion_timeline : [],
                goodput: Array.isArray(summary?.goodput_timeline) ? summary.goodput_timeline : [],
                latency: Array.isArray(summary?.latency_timeline) ? summary.latency_timeline : [],
                ttft: Array.isArray(summary?.ttft_timeline) ? summary.ttft_timeline : [],
                tpot: Array.isArray(summary?.tpot_timeline) ? summary.tpot_timeline : [],
                errors: Array.isArray(summary?.error_timeline) ? summary.error_timeline : [],
            },
            heatmaps: {
                ttft: summary?.ttft_heatmap || null,
                tpot: summary?.tpot_heatmap || null,
            },
            status_code_breakdown: rawStatusBreakdown,
            prompt_buckets: [],
            cache_buckets: [],
            cdf: {},
        },
        forensics: {
            schema_version: 1,
            task_id: task?.id || task?.task_id || '',
            telemetry_scope: 'client_only_legacy',
            legacy: true,
            verdict: {
                code: verdictCode,
                label: VERDICT_LABELS[verdictCode],
                severity: isErrorSat ? 'critical' : 'healthy',
                attainment_pct: totalRequests > 0 ? Math.max(0, 100 - errorRatePct) : null,
                goodput_rps: rps,
                goodput_per_hr: rps === null ? null : rps * 3600,
                throughput_rps: rps,
                throughput_per_hr: rps === null ? null : rps * 3600,
                slo: {
                    ttft_s: ttftSloSeconds,
                    tpot_ms: tpotSloSeconds === null ? null : tpotSloSeconds * 1000,
                    e2e_s: e2eSloSeconds,
                    error_rate_pct: errorRateSlo,
                },
                primary_stage: 'client',
                reason: 'Derived from legacy client summary (no server telemetry snapshot recorded).',
                evidence: [],
            },
            findings: [],
            timeline_buckets: [],
            ttft_decomposition: null,
            routing_vs_cache: null,
            pod_skew: null,
        },
        server_telemetry: {
            available: false,
            reason: 'Server telemetry was not collected for this legacy run.',
        },
        sessions_summary: {
            available: false,
            reason: 'Session correlation data is unavailable for legacy runs.',
        },
        run_manifest: null,
        grafana_links: [],
        target_deployments: [],
        capabilities: {
            legacy: true,
            server_telemetry: false,
            sessions: false,
        },
    };
}

const FLOW_METRIC_FIELDS = {
    request_rate: 'request_rate_rps',
    failed_request_rate: 'failed_request_rate_rps',
    input_token_rate: 'input_token_rate_tps',
    output_token_rate: 'output_token_rate_tps',
    queue_length: 'waiting_requests',
    kv_cache_usage_perc: 'kv_cache_usage_percent',
    prefix_cache_hit_rate: 'prefix_cache_hit_percent',
    external_prefix_cache_hit_rate: 'external_prefix_cache_hit_percent',
};

const BENCHMARK_RUNNER_POD_PATTERN = /^(?:llm-d-lens-sim-|trace-replayer-|aiperf-|lens-eval-)/i;
const EPP_POD_PATTERN = /(?:^|[-_])(epp|router|gateway)(?:[-_]|$)/i;

function firstFiniteMetric(...values) {
    for (const value of values) {
        const numeric = toFiniteNumber(value);
        if (numeric !== null) return numeric;
    }
    return null;
}

export function isBenchmarkRunnerPod(pod) {
    if (!pod || typeof pod !== 'object') return false;
    const name = String(pod.pod || pod.name || '');
    const role = String(pod.display_role || pod.role || '').toLowerCase();
    if (['client', 'benchmark', 'runner'].includes(role)) return true;
    return BENCHMARK_RUNNER_POD_PATTERN.test(name);
}

export function effectivePodRole(pod) {
    if (isBenchmarkRunnerPod(pod)) return 'runner';
    const name = String(pod?.pod || pod?.name || '');
    const rawRole = String(pod?.display_role || pod?.role || '').toLowerCase();
    if (rawRole === 'epp' || EPP_POD_PATTERN.test(name)) return 'epp';
    return rawRole || 'worker';
}

function meanMetric(source, key) {
    const raw = source?.[key];
    if (Number.isFinite(raw)) return raw;
    if (raw && typeof raw === 'object' && Number.isFinite(raw.mean)) return raw.mean;
    return null;
}

function flowInstanceMetrics(source = {}) {
    const mapped = Object.fromEntries(
        Object.entries(FLOW_METRIC_FIELDS).map(([target, key]) => [target, meanMetric(source, key)]),
    );
    const successRate = meanMetric(source, 'success_request_rate_rps');
    if (successRate !== null) mapped.success_request_rate = successRate;
    return mapped;
}

export function buildRecordedFlowData(serverTelemetry) {
    if (!serverTelemetry || serverTelemetry.available === false) return null;
    const window = serverTelemetry.window || {};
    const rawPerPod = Array.isArray(serverTelemetry.per_pod) ? serverTelemetry.per_pod : [];
    const perPod = rawPerPod.filter((pod) => !isBenchmarkRunnerPod(pod));
    const topologyMode = String(serverTelemetry.topology_mode || '').toLowerCase();
    const components = [];

    if (Array.isArray(serverTelemetry.flow_components) && serverTelemetry.flow_components.length > 0) {
        serverTelemetry.flow_components.forEach((component) => {
            if (component && component.present !== false) {
                const filteredInstances = Array.isArray(component.instances)
                    ? component.instances.filter((inst) => !isBenchmarkRunnerPod(inst))
                    : [];
                if (!Array.isArray(component.instances) || filteredInstances.length > 0) {
                    components.push({
                        ...component,
                        present: true,
                        ...(Array.isArray(component.instances) ? { instances: filteredInstances } : {}),
                    });
                }
            }
        });
    } else {
        const prefillPods = perPod.filter((pod) => effectivePodRole(pod) === 'prefill');
        const decodePods = perPod.filter((pod) => effectivePodRole(pod) === 'decode');
        const explicitWorkers = perPod.filter((pod) => ['worker', 'model-server'].includes(effectivePodRole(pod)));
        const eppPods = perPod.filter((pod) => effectivePodRole(pod) === 'epp');

        if (topologyMode === 'aggregated') {
            const workerPods = explicitWorkers.length > 0
                ? explicitWorkers
                : (decodePods.length > 0 || prefillPods.length > 0
                    ? [...prefillPods, ...decodePods]
                    : perPod.filter((pod) => effectivePodRole(pod) !== 'epp'));
            if (workerPods.length > 0) {
                components.push({
                    role: 'worker',
                    label: 'Workers',
                    present: true,
                    instances: workerPods.map((pod) => ({
                        name: pod.pod || pod.name,
                        ...flowInstanceMetrics(pod),
                    })),
                });
            }
        } else {
            if (prefillPods.length > 0) {
                components.push({
                    role: 'prefill',
                    label: 'Prefill',
                    present: true,
                    instances: prefillPods.map((pod) => ({
                        name: pod.pod || pod.name,
                        ...flowInstanceMetrics(pod),
                    })),
                });
            }
            if (decodePods.length > 0) {
                components.push({
                    role: 'decode',
                    label: 'Decode',
                    present: true,
                    instances: decodePods.map((pod) => ({
                        name: pod.pod || pod.name,
                        ...flowInstanceMetrics(pod),
                    })),
                });
            }
            if (explicitWorkers.length > 0) {
                components.push({
                    role: 'worker',
                    label: 'Workers',
                    present: true,
                    instances: explicitWorkers.map((pod) => ({
                        name: pod.pod || pod.name,
                        ...flowInstanceMetrics(pod),
                    })),
                });
            }
        }

        const router = serverTelemetry.router || {};
        const routerMetrics = flowInstanceMetrics(router);
        const eppInflight = meanMetric(serverTelemetry.summary, 'epp_inflight_requests')
            ?? meanMetric(router, 'inflight_requests');
        if (Object.values(routerMetrics).some(Number.isFinite) || eppInflight !== null || eppPods.length > 0) {
            components.unshift({
                role: 'epp',
                label: 'EPP (all instances)',
                present: true,
                instances: [{
                    name: 'EPP (all instances)',
                    ...routerMetrics,
                    queue_length: eppInflight ?? routerMetrics.queue_length,
                }],
            });
        }
    }

    if (!components.length) return null;

    const summary = serverTelemetry.summary || {};
    const hasTraffic = ['request_rate_rps', 'input_token_rate_tps', 'output_token_rate_tps'].some(
        (key) => meanMetric(summary, key) !== null,
    ) || components.some((component) => (component.instances || []).some((instance) => (
        ['request_rate', 'input_token_rate', 'output_token_rate'].some((key) => Number.isFinite(instance[key]))
    )));

    return {
        namespace: serverTelemetry.namespace || '',
        window: {
            start: window.start || '',
            end: window.end || '',
        },
        components,
        hasTraffic,
        reason: hasTraffic
            ? null
            : (serverTelemetry.flow_reason || serverTelemetry.reason || 'No inference request or token samples were recorded during this simulation window.'),
    };
}

function resolveTelemetryOriginMs(clientSeries, serverTelemetry, runManifest) {
    const originEpoch = Number(clientSeries?.origin_epoch);
    if (Number.isFinite(originEpoch) && originEpoch > 1_000_000_000) {
        return originEpoch * 1000;
    }
    const candidates = [
        serverTelemetry?.window?.start,
        runManifest?.execution_started_at,
        serverTelemetry?.series?.[0]?.timestamp,
    ];
    for (const ts of candidates) {
        if (typeof ts === 'string' && ts) {
            const parsed = Date.parse(ts);
            if (Number.isFinite(parsed)) return parsed;
        }
    }
    return null;
}

function pointRelativeSeconds(point, originMs, fallbackSeconds) {
    const explicit = firstFiniteMetric(point?.elapsed_seconds, point?.time_seconds, point?.t);
    if (explicit !== null) return explicit;
    if (typeof point?.timestamp === 'string' && point.timestamp && originMs !== null) {
        const parsed = Date.parse(point.timestamp);
        if (Number.isFinite(parsed)) return Math.max(0, (parsed - originMs) / 1000);
    }
    return fallbackSeconds;
}

function instanceShareWeights(instances = []) {
    if (!instances.length) return [];
    const raw = instances.map((inst) => (
        firstFiniteMetric(inst.request_rate, inst.output_token_rate, inst.input_token_rate) ?? 0
    ));
    const total = raw.reduce((acc, v) => acc + Math.max(0, v), 0);
    if (total > 0) return raw.map((v) => Math.max(0, v) / total);
    return instances.map(() => 1 / instances.length);
}

export function buildRecordedFlowFrames(telemetry) {
    const serverTelemetry = telemetry?.server_telemetry;
    const baseFlow = buildRecordedFlowData(serverTelemetry);
    if (!baseFlow || !baseFlow.components?.length) return [];

    const clientSeries = telemetry?.client_series || {};
    const serverSeries = Array.isArray(serverTelemetry?.series) ? serverTelemetry.series : [];
    const roleSeries = Array.isArray(serverTelemetry?.role_series) ? serverTelemetry.role_series : [];
    const clientBuckets = Array.isArray(clientSeries?.buckets) ? clientSeries.buckets : [];
    const stepSeconds = firstFiniteMetric(serverTelemetry?.step_seconds, 5) || 5;
    const bucketSeconds = firstFiniteMetric(clientSeries?.bucket_seconds, 5) || 5;
    const originMs = resolveTelemetryOriginMs(clientSeries, serverTelemetry, telemetry?.run_manifest);

    const indexedBuckets = clientBuckets.map((b, idx) => {
        const dur = firstFiniteMetric(
            b.end_seconds != null && b.start_seconds != null ? Number(b.end_seconds) - Number(b.start_seconds) : null,
            bucketSeconds,
        );
        const t = firstFiniteMetric(b.mid_seconds, b.time_seconds, b.end_seconds, b.elapsed_seconds, b.t, idx * bucketSeconds);
        const arrivedRps = firstFiniteMetric(
            b.arrived_rps,
            b.request_arrival_rps,
            b.arrived_requests != null && dur > 0 ? Number(b.arrived_requests) / dur : null,
        );
        const completedRps = firstFiniteMetric(b.completed_rps, b.throughput_rps, b.request_completion_rps);
        const failedRps = firstFiniteMetric(
            b.failed_rps,
            b.failed_requests != null && dur > 0 ? Number(b.failed_requests) / dur : null,
        );
        const outputTps = firstFiniteMetric(b.output_tok_per_s, b.output_tps, b.token_throughput_tps);
        const inflight = firstFiniteMetric(b.in_flight_concurrency, b.client_inflight, b.in_flight);
        return { t, arrivedRps, completedRps, failedRps, outputTps, inflight, raw: b };
    });

    const findNearestBucket = (targetT) => {
        if (!indexedBuckets.length) return null;
        let best = null;
        let bestDist = Infinity;
        const limit = Math.max(5, stepSeconds, bucketSeconds);
        for (const item of indexedBuckets) {
            const dist = Math.abs(item.t - targetT);
            if (dist <= limit && dist < bestDist) {
                bestDist = dist;
                best = item;
            }
        }
        return best;
    };

    const engineStages = baseFlow.components.filter((c) => c.role !== 'epp');
    const soleEngineStage = engineStages.length === 1;
    const stageShares = new Map(
        baseFlow.components.map((comp) => [comp.role, instanceShareWeights(comp.instances || [])]),
    );

    const timelineSource = serverSeries.length > 0
        ? serverSeries.map((pt, idx) => ({
            t: pointRelativeSeconds(pt, originMs, idx * stepSeconds),
            srvPt: pt,
            rolePt: roleSeries[idx] || null,
            bucket: null,
        }))
        : indexedBuckets.map((b) => ({
            t: b.t,
            srvPt: {},
            rolePt: null,
            bucket: b,
        }));

    if (!timelineSource.length) return [];

    return timelineSource.map((entry) => {
        const { t, srvPt, rolePt } = entry;
        const bucket = entry.bucket || findNearestBucket(t);
        const prefillRunning = firstFiniteMetric(rolePt?.prefill_running_requests);
        const decodeRunning = firstFiniteMetric(rolePt?.decode_running_requests);

        const components = baseFlow.components.map((comp) => {
            const role = comp.role;
            const instances = comp.instances || [];
            const shares = stageShares.get(role) || [];

            if (role === 'epp') {
                const reqRate = firstFiniteMetric(
                    srvPt.router_request_rate_rps,
                    srvPt.router_scheduler_attempt_rate_rps,
                    srvPt.request_rate_rps,
                    bucket?.completedRps,
                    bucket?.arrivedRps,
                );
                const failRate = firstFiniteMetric(srvPt.failed_request_rate_rps, bucket?.failedRps, 0);
                const successRate = reqRate !== null ? Math.max(0, reqRate - failRate) : null;
                const inflight = firstFiniteMetric(
                    srvPt.epp_inflight_requests,
                    srvPt.router_running_requests,
                    srvPt.running_requests,
                    bucket?.inflight,
                );
                return {
                    ...comp,
                    arrival_request_rate: bucket?.arrivedRps ?? reqRate,
                    request_rate: reqRate,
                    success_request_rate: successRate,
                    failed_request_rate: failRate,
                    instances: instances.map((inst) => ({
                        ...inst,
                        request_rate: reqRate,
                        success_request_rate: successRate,
                        failed_request_rate: failRate,
                        input_token_rate: firstFiniteMetric(srvPt.router_input_token_rate_tps, srvPt.input_token_rate_tps),
                        output_token_rate: firstFiniteMetric(srvPt.output_token_rate_tps, bucket?.outputTps),
                        queue_length: inflight,
                        ...(prefillRunning !== null ? { prefill_inflight: prefillRunning } : {}),
                        ...(decodeRunning !== null ? { decode_inflight: decodeRunning } : {}),
                    })),
                };
            }

            const rolePrefixes = role === 'worker' ? ['worker', 'model-server', 'decode'] : [role];
            const pickRoleMetric = (suffix) => {
                if (!rolePt) return null;
                for (const prefix of rolePrefixes) {
                    const val = firstFiniteMetric(rolePt[`${prefix}_${suffix}`]);
                    if (val !== null) return val;
                }
                return null;
            };

            const reqRate = firstFiniteMetric(
                pickRoleMetric('request_rate_rps'),
                soleEngineStage ? firstFiniteMetric(srvPt.request_rate_rps, bucket?.completedRps) : null,
            );
            const failRate = firstFiniteMetric(
                pickRoleMetric('failed_request_rate_rps'),
                soleEngineStage ? firstFiniteMetric(srvPt.failed_request_rate_rps, bucket?.failedRps) : null,
            );
            const inTok = firstFiniteMetric(
                pickRoleMetric('input_token_rate_tps'),
                soleEngineStage ? srvPt.input_token_rate_tps : null,
            );
            const outTok = firstFiniteMetric(
                pickRoleMetric('output_token_rate_tps'),
                soleEngineStage ? firstFiniteMetric(srvPt.output_token_rate_tps, bucket?.outputTps) : null,
            );
            const waiting = firstFiniteMetric(
                pickRoleMetric('waiting_requests'),
                soleEngineStage ? firstFiniteMetric(srvPt.waiting_requests, srvPt.queue_depth) : null,
            );
            const kvPct = firstFiniteMetric(
                pickRoleMetric('kv_cache_usage_percent'),
                soleEngineStage ? firstFiniteMetric(srvPt.kv_cache_usage_percent, srvPt.gpu_cache_usage_percent) : null,
            );
            const prefixHit = firstFiniteMetric(
                pickRoleMetric('prefix_cache_hit_percent'),
                soleEngineStage ? srvPt.prefix_cache_hit_percent : null,
            );
            const extHit = firstFiniteMetric(
                pickRoleMetric('external_prefix_cache_hit_percent'),
                soleEngineStage ? srvPt.external_prefix_cache_hit_percent : null,
            );

            return {
                ...comp,
                arrival_request_rate: comp === baseFlow.components[0] ? (bucket?.arrivedRps ?? reqRate) : undefined,
                instances: instances.map((inst, idx) => {
                    const share = shares[idx] ?? (1 / Math.max(1, instances.length));
                    const instReq = reqRate !== null ? reqRate * share : null;
                    const instFail = failRate !== null ? failRate * share : null;
                    return {
                        ...inst,
                        request_rate: instReq,
                        success_request_rate: instReq !== null ? Math.max(0, instReq - (instFail ?? 0)) : null,
                        failed_request_rate: instFail,
                        input_token_rate: inTok !== null ? inTok * share : null,
                        output_token_rate: outTok !== null ? outTok * share : null,
                        queue_length: waiting !== null ? waiting * share : null,
                        kv_cache_usage_perc: kvPct,
                        prefix_cache_hit_rate: prefixHit,
                        external_prefix_cache_hit_rate: extHit,
                    };
                }),
            };
        });

        const frameRps = firstFiniteMetric(
            srvPt.request_rate_rps,
            rolePt?.decode_request_rate_rps,
            srvPt.router_request_rate_rps,
            bucket?.completedRps,
            bucket?.arrivedRps,
        );
        const frameOutputTps = firstFiniteMetric(
            srvPt.output_token_rate_tps,
            rolePt?.decode_output_token_rate_tps,
            bucket?.outputTps,
        );
        const frameRunning = firstFiniteMetric(
            srvPt.running_requests,
            (prefillRunning ?? 0) + (decodeRunning ?? 0) > 0 || prefillRunning !== null || decodeRunning !== null
                ? (prefillRunning ?? 0) + (decodeRunning ?? 0)
                : null,
            bucket?.inflight,
        );
        const frameWaiting = firstFiniteMetric(
            srvPt.queue_depth,
            srvPt.waiting_requests,
            rolePt?.decode_waiting_requests,
            rolePt?.prefill_waiting_requests,
        );
        const frameKvPct = firstFiniteMetric(
            srvPt.kv_cache_usage_percent,
            srvPt.gpu_cache_usage_percent,
            rolePt?.decode_kv_cache_usage_percent,
            rolePt?.prefill_kv_cache_usage_percent,
        );
        const hasFrameTraffic = [frameRps, frameOutputTps, frameRunning, bucket?.arrivedRps].some(
            (v) => v !== null && v > 0,
        );

        return {
            t,
            rps: frameRps,
            outputTps: frameOutputTps,
            runningRequests: frameRunning,
            waitingRequests: frameWaiting,
            kvCachePct: frameKvPct,
            hasTraffic: hasFrameTraffic,
            flowData: {
                ...baseFlow,
                hasTraffic: hasFrameTraffic || baseFlow.hasTraffic,
                components,
            },
        };
    });
}

const STAGE_LABELS = {
    epp_scheduling: 'EPP Scheduling',
    queue_time: 'Queue Time',
    kv_transfer: 'KV Transfer (NIXL)',
    prefill_time: 'Prefill Time',
    engine_overhead: 'Server TTFT Residual',
    gateway_overhead: 'Client / Gateway / Network',
    decode_time: 'Decode Time',
    ttft_unattributed: 'Client TTFT',
    client_ttft: 'Client TTFT',
};

const SERVER_STAGE_STACK_KEYS = ['epp_sched_ms', 'queue_ms', 'prefill_ms', 'decode_ms'];
const SERVER_PD_STAGE_STACK_KEYS = ['epp_sched_ms', 'queue_ms', 'kv_transfer_ms', 'prefill_ms', 'decode_ms'];
const PD_POOLED_STAGE_STACK_KEYS = ['epp_sched_ms', 'kv_transfer_ms', 'ttft_unattributed_ms', 'decode_ms'];
const CLIENT_STAGE_STACK_KEYS = ['ttft_ms', 'decode_ms'];
const E2E_STAGE_KEYS = ['epp_scheduling', 'queue_time', 'kv_transfer', 'prefill_time', 'engine_overhead', 'gateway_overhead', 'decode_time'];
const TTFT_STAGE_KEYS = ['epp_scheduling', 'queue_time', 'kv_transfer', 'prefill_time', 'engine_overhead', 'gateway_overhead'];
const E2E_STAGE_TIE_ORDER = ['queue_time', 'kv_transfer', 'prefill_time', 'decode_time', 'engine_overhead', 'gateway_overhead', 'epp_scheduling'];
const TTFT_STAGE_TIE_ORDER = ['queue_time', 'kv_transfer', 'prefill_time', 'engine_overhead', 'gateway_overhead', 'epp_scheduling'];
const PD_POOLED_CAVEAT = 'Legacy P/D server telemetry pooled Prefill and Decode histograms together, distorting per-stage quantiles. Showing Client E2E/TTFT split with measured EPP Scheduling and NIXL KV Transfer; refresh telemetry against Prometheus or re-run for role-separated Queue/Prefill/Decode attribution.';
const ROLE_LATENCY_KEYS = [
    'vllm_queue_time_p50_ms',
    'vllm_queue_time_p90_ms',
    'vllm_queue_time_p99_ms',
    'vllm_queue_time_mean_ms',
    'vllm_prefill_time_p50_ms',
    'vllm_prefill_time_p90_ms',
    'vllm_prefill_time_p99_ms',
    'vllm_prefill_time_mean_ms',
    'vllm_decode_time_p50_ms',
    'vllm_decode_time_p90_ms',
    'vllm_decode_time_p99_ms',
    'vllm_decode_time_mean_ms',
    'vllm_ttft_p50_ms',
    'vllm_ttft_p90_ms',
    'vllm_ttft_p99_ms',
    'vllm_ttft_mean_ms',
    'vllm_e2e_p50_ms',
    'vllm_e2e_p90_ms',
    'vllm_e2e_p99_ms',
    'vllm_e2e_mean_ms',
];
const SERVER_LATENCY_PREFIXES = [
    'vllm_prefill_time_',
    'vllm_queue_time_',
    'vllm_decode_time_',
    'vllm_ttft_',
    'nixl_transfer_latency_',
    'prefill_time_',
    'queue_time_',
    'decode_time_',
];
const ROUTER_LATENCY_PREFIXES = [
    'scheduler_latency_',
    'scheduler_e2e_',
    'router_scheduler_latency_',
    'router_scheduler_e2e_',
];

export function isFiniteMetricValue(val) {
    return toFiniteNumber(val) !== null;
}

function nonNegMetric(val) {
    if (!isFiniteMetricValue(val)) return null;
    return Math.round(Math.max(0, Number(val)) * 1000) / 1000;
}

function round1(val) {
    return Math.round(val * 10) / 10;
}

function round3(val) {
    return Math.round(val * 1000) / 1000;
}

export function formatStageName(stageKey, source = null) {
    if (!stageKey) return '—';
    const key = String(stageKey).trim().toLowerCase();
    if (key === 'ttft_unattributed' && source === 'pd_pooled_fallback') {
        return 'P/D Engine TTFT (Queue + Prefill)';
    }
    return STAGE_LABELS[key] || displayName(key);
}

function hasRoleSeparatedLatency(serverTelemetry) {
    if (!serverTelemetry || typeof serverTelemetry !== 'object') return false;
    const summaryByRole = serverTelemetry.summary_by_role;
    const roleSeries = Array.isArray(serverTelemetry.role_series) ? serverTelemetry.role_series : [];
    const hasPrefillSummary = Boolean(
        summaryByRole?.prefill && ROLE_LATENCY_KEYS.some((k) => summaryByRole.prefill[k] != null),
    );
    const hasDecodeSummary = Boolean(
        summaryByRole?.decode && ROLE_LATENCY_KEYS.some((k) => summaryByRole.decode[k] != null),
    );
    const hasPrefillSeries = roleSeries.some((pt) => pt && typeof pt === 'object' && ROLE_LATENCY_KEYS.some(
        (k) => isFiniteMetricValue(pt[`prefill_${k}`]),
    ));
    const hasDecodeSeries = roleSeries.some((pt) => pt && typeof pt === 'object' && ROLE_LATENCY_KEYS.some(
        (k) => isFiniteMetricValue(pt[`decode_${k}`]),
    ));
    return (hasPrefillSummary || hasPrefillSeries) && (hasDecodeSummary || hasDecodeSeries);
}

export function isLegacyPooledPd(forensics, serverTelemetry) {
    const slb = forensics?.stage_latency_breakdown;
    const decomp = forensics?.ttft_decomposition;
    if (slb?.source === 'pd_pooled_fallback' || decomp?.source === 'pd_pooled_fallback') {
        return true;
    }
    if (slb?.source === 'server_telemetry' && slb?.role_breakdown && typeof slb.role_breakdown === 'object') {
        const rb = slb.role_breakdown;
        const hasRbLatency = [
            'prefill_p50_ms',
            'queue_p50_ms',
            'decode_p50_ms',
            'queue_raw_p50_ms',
            'prefill_p99_ms',
            'queue_p99_ms',
            'decode_p99_ms',
            'queue_raw_p99_ms',
            'prefill_mean_ms',
            'decode_mean_ms',
        ].some((k) => isFiniteMetricValue(rb.prefill?.[k]) || isFiniteMetricValue(rb.decode?.[k]));
        if (hasRbLatency) return false;
    }
    if (!serverTelemetry || serverTelemetry.available === false || serverTelemetry.status === 'unavailable') {
        return false;
    }
    const vllmStageSource = serverTelemetry?.metric_sources?.vllm_stage_latencies;
    if (vllmStageSource === 'pooled_namespace_legacy') return true;
    if (vllmStageSource === 'role_separated' || vllmStageSource === 'aggregated_namespace') return false;
    if (hasRoleSeparatedLatency(serverTelemetry)) return false;

    const topologyMode = String(serverTelemetry?.topology_mode || serverTelemetry?.topology?.mode || '').toLowerCase();
    const rawPerPod = Array.isArray(serverTelemetry?.per_pod) ? serverTelemetry.per_pod : [];
    const perPod = rawPerPod.filter((pod) => !isBenchmarkRunnerPod(pod));
    const podRoles = new Set(perPod.map((pod) => effectivePodRole(pod)));
    const roleSeries = Array.isArray(serverTelemetry?.role_series) ? serverTelemetry.role_series : [];
    const summaryByRole = serverTelemetry?.summary_by_role || {};
    const roleSummary = serverTelemetry?.derived?.role_summary || {};
    const components = serverTelemetry?.components || {};

    const hasPrefillRole = podRoles.has('prefill')
        || Boolean(summaryByRole.prefill)
        || Boolean(roleSummary.prefill)
        || (Array.isArray(components.prefill) ? components.prefill.length > 0 : Boolean(components.prefill))
        || roleSeries.some((pt) => pt && Object.keys(pt).some((k) => k.startsWith('prefill_')));
    const hasDecodeRole = podRoles.has('decode')
        || Boolean(summaryByRole.decode)
        || Boolean(roleSummary.decode)
        || (Array.isArray(components.decode) ? components.decode.length > 0 : Boolean(components.decode))
        || roleSeries.some((pt) => pt && Object.keys(pt).some((k) => k.startsWith('decode_')));
    const isDisagg = topologyMode === 'disaggregated' || (hasPrefillRole && hasDecodeRole);
    return Boolean(isDisagg && hasPrefillRole && hasDecodeRole);
}

export function stageAreaLayers(source, hasKvTransfer = false) {
    if (source === 'client_fallback') {
        return [
            { key: 'ttft_ms', label: 'Client TTFT (Unattributed)', colorIndex: 0 },
            { key: 'decode_ms', label: 'Decode Time (Est.)', colorIndex: 1 },
        ];
    }
    if (source === 'pd_pooled_fallback') {
        const pdLayers = [
            { key: 'epp_sched_ms', label: 'EPP Scheduling', colorIndex: 3 },
        ];
        if (hasKvTransfer) {
            pdLayers.push({ key: 'kv_transfer_ms', label: 'KV Transfer (NIXL)', colorIndex: 4 });
        }
        pdLayers.push(
            { key: 'ttft_unattributed_ms', label: 'P/D Engine TTFT (Queue + Prefill)', colorIndex: 0 },
            { key: 'decode_ms', label: 'Decode Time (Est.)', colorIndex: 1 },
        );
        return pdLayers;
    }
    const layers = [
        { key: 'epp_sched_ms', label: 'EPP Scheduling', colorIndex: 3 },
        { key: 'queue_ms', label: 'Queue Time', colorIndex: 2 },
    ];
    if (hasKvTransfer) {
        layers.push({ key: 'kv_transfer_ms', label: 'KV Transfer (NIXL)', colorIndex: 4 });
    }
    layers.push(
        { key: 'prefill_ms', label: 'Prefill Time', colorIndex: 0 },
        { key: 'decode_ms', label: 'Decode Time', colorIndex: 1 },
    );
    return layers;
}

export function normalizeStageStackSeries(rows, keys) {
    if (!Array.isArray(rows) || !rows.length || !Array.isArray(keys) || !keys.length) {
        return Array.isArray(rows) ? rows : [];
    }
    return rows.map((row) => {
        const hasAnyFinite = keys.some((key) => isFiniteMetricValue(row?.[key]));
        const next = { ...row };
        for (const key of keys) {
            next[key] = hasAnyFinite
                ? (isFiniteMetricValue(row?.[key]) ? Math.max(0, Number(row[key])) : 0)
                : null;
        }
        return next;
    });
}

function hasAnyFiniteSeriesKey(rows, keys) {
    return Array.isArray(rows) && rows.some((row) => keys.some((key) => isFiniteMetricValue(row?.[key])));
}

function readSummaryStat(source, keys, statKeys = ['mean']) {
    if (!source || typeof source !== 'object') return null;
    for (const key of keys) {
        const raw = source[key];
        if (raw === null || raw === undefined) continue;
        if (typeof raw === 'object') {
            for (const statKey of statKeys) {
                const candidate = nonNegMetric(raw[statKey]);
                if (candidate !== null) return candidate;
            }
        } else {
            const candidate = nonNegMetric(raw);
            if (candidate !== null) return candidate;
        }
    }
    return null;
}

function hasAnyFiniteLatencyInObj(obj, prefixes) {
    if (!obj || typeof obj !== 'object') return false;
    for (const [k, raw] of Object.entries(obj)) {
        if (!prefixes.some((prefix) => k.startsWith(prefix))) continue;
        if (raw && typeof raw === 'object') {
            if (Object.values(raw).some((v) => nonNegMetric(v) !== null)) return true;
        } else if (nonNegMetric(raw) !== null) {
            return true;
        }
    }
    return false;
}

export function hasServerLatencyTelemetry(serverTelemetry) {
    if (!serverTelemetry || typeof serverTelemetry !== 'object') return false;
    if (serverTelemetry.available === false || serverTelemetry.status === 'unavailable') return false;
    if (hasAnyFiniteLatencyInObj(serverTelemetry.summary, SERVER_LATENCY_PREFIXES)) return true;
    const byRole = serverTelemetry.summary_by_role;
    if (
        hasAnyFiniteLatencyInObj(byRole?.prefill, SERVER_LATENCY_PREFIXES)
        || hasAnyFiniteLatencyInObj(byRole?.decode, SERVER_LATENCY_PREFIXES)
    ) {
        return true;
    }
    if (hasAnyFiniteLatencyInObj(serverTelemetry.router, ROUTER_LATENCY_PREFIXES)) return true;
    const srvSeries = Array.isArray(serverTelemetry.series) ? serverTelemetry.series : [];
    const seriesPrefixes = [...SERVER_LATENCY_PREFIXES, ...ROUTER_LATENCY_PREFIXES];
    if (srvSeries.some((pt) => hasAnyFiniteLatencyInObj(pt, seriesPrefixes))) return true;
    const roleSeries = Array.isArray(serverTelemetry.role_series) ? serverTelemetry.role_series : [];
    return roleSeries.some((pt) => {
        if (!pt || typeof pt !== 'object') return false;
        return Object.entries(pt).some(([k, v]) => {
            const stripped = k.startsWith('prefill_')
                ? k.slice('prefill_'.length)
                : (k.startsWith('decode_') ? k.slice('decode_'.length) : null);
            return Boolean(stripped && SERVER_LATENCY_PREFIXES.some((p) => stripped.startsWith(p)) && nonNegMetric(v) !== null);
        });
    });
}

function seriesPercentile(values, pct) {
    if (!Array.isArray(values) || !values.length) return null;
    const ordered = [...values].sort((a, b) => a - b);
    const idx = Math.min(ordered.length - 1, Math.max(0, Math.ceil((ordered.length * pct) / 100) - 1));
    return round3(ordered[idx]);
}

function aggregateSeriesValues(values, aggMode) {
    if (!Array.isArray(values) || !values.length) return null;
    if (aggMode === 'max') return round3(Math.max(...values));
    if (aggMode === 'p99') return seriesPercentile(values, 99);
    const sum = values.reduce((acc, v) => acc + v, 0);
    return round3(sum / values.length);
}

function filterSteadyPoints(points, originMsValue, warmupSec) {
    if (!Array.isArray(points) || !points.length) return [];
    const valid = points.filter((pt) => pt && typeof pt === 'object');
    if (!(warmupSec > 0)) return valid;
    const steady = [];
    for (const pt of valid) {
        if (typeof pt.timestamp === 'string' && pt.timestamp && originMsValue !== null) {
            const tsMs = Date.parse(pt.timestamp);
            if (Number.isFinite(tsMs) && (tsMs - originMsValue) / 1000 < warmupSec) {
                continue;
            }
        } else {
            const relSec = firstFiniteMetric(pt.elapsed_seconds, pt.time_seconds, pt.t);
            if (relSec !== null && relSec < warmupSec) {
                continue;
            }
        }
        steady.push(pt);
    }
    return steady.length > 0 ? steady : valid;
}

function seriesArithmeticMean(rows, key) {
    if (!Array.isArray(rows) || !rows.length) return null;
    let sum = 0;
    let count = 0;
    for (const row of rows) {
        const val = nonNegMetric(row?.[key]);
        if (val !== null) {
            sum += val;
            count += 1;
        }
    }
    return count > 0 ? round3(sum / count) : null;
}

function readWindowLatencyMetric(win, metricKey, quantile) {
    if (!win || typeof win !== 'object') return null;
    const msBlock = win[`${metricKey}_ms`];
    const baseBlock = win[metricKey];
    const statCandidates = quantile === 'mean' ? ['avg', 'mean', 'avg_ms', 'mean_ms'] : [quantile, `${quantile}_ms`];
    for (const block of [msBlock, baseBlock]) {
        if (block && typeof block === 'object') {
            for (const stat of statCandidates) {
                const candidate = nonNegMetric(block[stat]);
                if (candidate !== null) return candidate;
            }
        }
    }
    const flatCandidates = quantile === 'mean'
        ? [`${metricKey}_avg_ms`, `${metricKey}_mean_ms`]
        : [`${metricKey}_${quantile}_ms`];
    for (const flatKey of flatCandidates) {
        const candidate = nonNegMetric(win[flatKey]);
        if (candidate !== null) return candidate;
    }
    return null;
}

function readBucketLatencyMetric(bucket, metricKey, quantile) {
    if (!bucket || typeof bucket !== 'object') return null;
    const statCandidates = quantile === 'mean' ? ['mean', 'avg'] : [quantile];
    for (const blockKey of [metricKey, `${metricKey}_ms`]) {
        const block = bucket[blockKey];
        if (block && typeof block === 'object') {
            for (const stat of statCandidates) {
                const candidate = nonNegMetric(block[stat]);
                if (candidate !== null) return candidate;
            }
        }
    }
    const flatCandidates = quantile === 'mean'
        ? [`${metricKey}_mean_ms`, `${metricKey}_avg_ms`, `${metricKey}_ms`]
        : [`${metricKey}_${quantile}_ms`];
    for (const flatKey of flatCandidates) {
        const candidate = nonNegMetric(bucket[flatKey]);
        if (candidate !== null) return candidate;
    }
    return null;
}

function computeShareMap(valuesByKey, keys, denominator) {
    const out = {};
    const validDenom = denominator !== null && denominator !== undefined && Number.isFinite(denominator) && denominator > 0;
    for (const key of keys) {
        const val = valuesByKey[key];
        out[key] = validDenom && val !== null && val !== undefined ? round1((val / denominator) * 100) : null;
    }
    return out;
}

function dominantStageFromShares(shares, tieOrder) {
    if (!shares || typeof shares !== 'object') return null;
    let bestKey = null;
    let bestVal = 0;
    for (const key of tieOrder) {
        const val = shares[key];
        if (val !== null && val !== undefined && Number.isFinite(Number(val)) && Number(val) > 0) {
            const num = Number(val);
            if (bestKey === null || num > bestVal) {
                bestKey = key;
                bestVal = num;
            }
        }
    }
    return bestKey;
}

function buildStageReconciliation(clientE2eMs, totalStageMs, quantile) {
    if (clientE2eMs === null || totalStageMs === null || clientE2eMs <= 0) return null;
    const gapMs = round3(clientE2eMs - totalStageMs);
    const gapPct = round1((gapMs / clientE2eMs) * 100);
    return {
        client_e2e_ms: clientE2eMs,
        total_stage_ms: totalStageMs,
        e2e_gap_ms: gapMs,
        e2e_gap_pct: gapPct,
        quantile_non_additive: quantile !== 'mean',
    };
}

function finalizeServerStageSlice({
    quantile,
    eppSchedulingMs: rawEpp,
    queueTimeMs: rawQueue,
    kvTransferMs: rawKv,
    prefillTimeMs: rawPrefill,
    decodeTimeMs,
    clientTtftMs,
    clientE2eMs,
    serverTtftMs,
    serverE2eMs,
    pTtft = null,
    dTtft = null,
    pQueueEff = null,
    dQueueEff = null,
    pPrefillMs = null,
    dPrefillMs = null,
    isDisaggregated = false,
    useRolePd = false,
    applyOverAllocGuard = false,
    unboundedTailGuard = false,
    sliceNote = null,
}) {
    let eppSchedulingMs = rawEpp;
    let queueTimeMs = rawQueue;
    let kvTransferMs = rawKv;
    let prefillTimeMs = rawPrefill;
    const knownTotal = (eppSchedulingMs ?? 0) + (queueTimeMs ?? 0) + (kvTransferMs ?? 0) + (prefillTimeMs ?? 0);
    if (applyOverAllocGuard && clientTtftMs !== null && clientTtftMs > 0 && knownTotal > clientTtftMs) {
        const scale = clientTtftMs / knownTotal;
        const parts = [
            ['epp', eppSchedulingMs],
            ['queue', queueTimeMs],
            ['kv', kvTransferMs],
            ['prefill', prefillTimeMs],
        ].filter(([, val]) => val !== null);
        const scaled = {};
        let sumScaled = 0;
        let maxKey = parts[0]?.[0] ?? null;
        let maxRaw = -1;
        for (const [key, val] of parts) {
            scaled[key] = round3(val * scale);
            sumScaled = round3(sumScaled + scaled[key]);
            if (val > maxRaw) {
                maxRaw = val;
                maxKey = key;
            }
        }
        const drift = round3(clientTtftMs - sumScaled);
        if (drift !== 0 && maxKey) {
            scaled[maxKey] = round3(Math.max(0, scaled[maxKey] + drift));
        }
        if (eppSchedulingMs !== null) eppSchedulingMs = scaled.epp;
        if (queueTimeMs !== null) queueTimeMs = scaled.queue;
        if (kvTransferMs !== null) kvTransferMs = scaled.kv;
        if (prefillTimeMs !== null) prefillTimeMs = scaled.prefill;
    }

    const isMean = quantile === 'mean';
    const knownEngineTtft = (queueTimeMs ?? 0) + (kvTransferMs ?? 0) + (prefillTimeMs ?? 0);
    const hasEngineTtft = queueTimeMs !== null || kvTransferMs !== null || prefillTimeMs !== null;
    const totalTtftResidual = (clientTtftMs !== null && hasEngineTtft)
        ? round3(Math.max(0, clientTtftMs - ((eppSchedulingMs ?? 0) + knownEngineTtft)))
        : null;
    const serverTtftClamped = !isMean
        && (
            (isDisaggregated && useRolePd && ((pTtft ?? 0) >= 9500 || (dTtft ?? 0) >= 9500))
            || ((!isDisaggregated || !useRolePd) && serverTtftMs !== null && serverTtftMs >= 9500)
        )
        && clientTtftMs !== null
        && serverTtftMs !== null
        && clientTtftMs > serverTtftMs + 500;
    const isUnboundedTail = Boolean(
        unboundedTailGuard
        && !serverTtftClamped
        && clientTtftMs !== null
        && (serverTtftMs === null || clientTtftMs > serverTtftMs + 500),
    );

    let engineOverheadMs = null;
    let gatewayOverheadMs = null;
    let sliceCaveat = null;
    if (totalTtftResidual !== null) {
        if (serverTtftClamped) {
            engineOverheadMs = totalTtftResidual;
            gatewayOverheadMs = null;
            sliceCaveat = 'Server TTFT quantile hit the 10 s histogram ceiling; unmeasured engine tail is attributed to Server TTFT Residual (see Mean slice for exact additive attribution).';
        } else if (isUnboundedTail) {
            engineOverheadMs = totalTtftResidual;
            gatewayOverheadMs = null;
        } else if (serverTtftMs !== null) {
            engineOverheadMs = round3(Math.min(totalTtftResidual, Math.max(0, serverTtftMs - knownEngineTtft)));
            gatewayOverheadMs = round3(Math.max(0, totalTtftResidual - engineOverheadMs));
        } else {
            engineOverheadMs = null;
            gatewayOverheadMs = totalTtftResidual;
        }
    }

    const pdSplit = (useRolePd && !serverTtftClamped && !isUnboundedTail)
        ? reconcilePdEngineOverhead({
            engineOverheadMs,
            serverTtftClamped,
            pTtft,
            pQueueEff,
            pPrefill: pPrefillMs,
            dTtft,
            dQueueEff,
            dPrefill: dPrefillMs,
        })
        : {
            decode_first_step_ms: null,
            prefill_engine_overhead_ms: null,
            decode_handoff_overhead_ms: null,
        };

    const ttftParts = [eppSchedulingMs, queueTimeMs, kvTransferMs, prefillTimeMs, engineOverheadMs, gatewayOverheadMs].filter((v) => v !== null);
    const ttftStageMs = ttftParts.length > 0 ? round3(ttftParts.reduce((sum, v) => sum + v, 0)) : null;

    const totalParts = [eppSchedulingMs, queueTimeMs, kvTransferMs, prefillTimeMs, engineOverheadMs, gatewayOverheadMs, decodeTimeMs].filter((v) => v !== null);
    const totalStageMs = totalParts.length > 0 ? round3(totalParts.reduce((sum, v) => sum + v, 0)) : null;

    const e2eValues = {
        epp_scheduling: eppSchedulingMs,
        queue_time: queueTimeMs,
        kv_transfer: kvTransferMs,
        prefill_time: prefillTimeMs,
        engine_overhead: engineOverheadMs,
        gateway_overhead: gatewayOverheadMs,
        decode_time: decodeTimeMs,
    };
    const ttftValues = {
        epp_scheduling: eppSchedulingMs,
        queue_time: queueTimeMs,
        kv_transfer: kvTransferMs,
        prefill_time: prefillTimeMs,
        engine_overhead: engineOverheadMs,
        gateway_overhead: gatewayOverheadMs,
    };

    return {
        available: true,
        source: 'server_telemetry',
        epp_scheduling_ms: eppSchedulingMs,
        queue_time_ms: queueTimeMs,
        kv_transfer_ms: kvTransferMs,
        prefill_time_ms: prefillTimeMs,
        decode_time_ms: decodeTimeMs,
        engine_overhead_ms: engineOverheadMs,
        decode_first_step_ms: pdSplit.decode_first_step_ms,
        prefill_engine_overhead_ms: pdSplit.prefill_engine_overhead_ms,
        decode_handoff_overhead_ms: pdSplit.decode_handoff_overhead_ms,
        gateway_overhead_ms: gatewayOverheadMs,
        ttft_stage_ms: ttftStageMs,
        total_stage_ms: totalStageMs,
        client_ttft_ms: clientTtftMs,
        client_e2e_ms: clientE2eMs,
        server_ttft_ms: serverTtftMs,
        server_e2e_ms: serverE2eMs,
        server_ttft_clamped: serverTtftClamped,
        caveat: sliceCaveat,
        slice_note: sliceNote,
        reconciliation: buildStageReconciliation(clientE2eMs, totalStageMs, quantile),
        share_of_e2e_pct: computeShareMap(e2eValues, E2E_STAGE_KEYS, totalStageMs),
        share_of_ttft_pct: computeShareMap(ttftValues, TTFT_STAGE_KEYS, ttftStageMs),
    };
}

function aggSrvMetric({ summary, steadySrvPoints, effWarmup, aggMode, statOrder, seriesKeys, summaryKey }) {
    const steadyVals = [];
    for (const pt of steadySrvPoints) {
        for (const sk of seriesKeys) {
            const val = nonNegMetric(pt?.[sk]);
            if (val !== null) {
                steadyVals.push(val);
                break;
            }
        }
    }
    if (effWarmup > 0 && steadyVals.length > 0) {
        return aggregateSeriesValues(steadyVals, aggMode);
    }
    const sumVal = readSummaryStat(summary, [summaryKey], statOrder);
    if (sumVal !== null) return sumVal;
    return aggregateSeriesValues(steadyVals, aggMode);
}

function aggRoleMetric({ summaryByRole, steadyRolePoints, effWarmup, aggMode, statOrder, role, metricKey }) {
    const seriesKey = `${role}_${metricKey}`;
    const steadyVals = [];
    for (const pt of steadyRolePoints) {
        const val = nonNegMetric(pt?.[seriesKey]);
        if (val !== null) steadyVals.push(val);
    }
    if (effWarmup > 0 && steadyVals.length > 0) {
        return aggregateSeriesValues(steadyVals, aggMode);
    }
    const sumVal = readSummaryStat(summaryByRole?.[role], [metricKey], statOrder);
    if (sumVal !== null) return sumVal;
    return aggregateSeriesValues(steadyVals, aggMode);
}

function aggEppTailMetric({ router, steadySrvPoints, effWarmup, aggMode, statOrder }) {
    const levels = [
        {
            seriesKeys: ['router_scheduler_latency_p99_ms', 'router_scheduler_e2e_p99_ms'],
            routerKeys: ['scheduler_latency_p99_ms', 'scheduler_e2e_p99_ms'],
        },
        {
            seriesKeys: ['router_scheduler_latency_p95_ms'],
            routerKeys: ['scheduler_latency_p95_ms'],
        },
        {
            seriesKeys: ['router_scheduler_latency_p90_ms', 'router_scheduler_e2e_p90_ms'],
            routerKeys: ['scheduler_latency_p90_ms', 'scheduler_e2e_p90_ms'],
        },
    ];
    for (const level of levels) {
        const steadyVals = [];
        for (const pt of steadySrvPoints) {
            for (const sk of level.seriesKeys) {
                const val = nonNegMetric(pt?.[sk]);
                if (val !== null) {
                    steadyVals.push(val);
                    break;
                }
            }
        }
        if (effWarmup > 0 && steadyVals.length > 0) {
            return aggregateSeriesValues(steadyVals, aggMode);
        }
        const rVal = readSummaryStat(router, level.routerKeys, statOrder);
        if (rVal !== null) return rVal;
        const fallback = aggregateSeriesValues(steadyVals, aggMode);
        if (fallback !== null) return fallback;
    }
    return null;
}

function buildServerStageSlice(quantile, { summary, summaryByRole, router, steadyWindow, rawRows, isDisaggregated }) {
    const isP90 = quantile === 'p90';
    const isMean = quantile === 'mean';
    const routerStatOrder = isMean ? ['mean', 'avg'] : (isP90 ? ['mean', 'p90', 'p95'] : ['mean', 'p50']);
    const srvStatOrder = isMean ? ['mean', 'avg'] : (isP90 ? ['mean', 'p90'] : ['mean', 'p50']);
    const suffix = isMean ? 'mean' : (isP90 ? 'p90' : 'p50');

    const eppSchedulingMs = readSummaryStat(
        router,
        isMean
            ? ['scheduler_latency_mean_ms', 'scheduler_e2e_mean_ms']
            : (isP90
                ? ['scheduler_latency_p90_ms', 'scheduler_e2e_p90_ms', 'scheduler_latency_p95_ms']
                : ['scheduler_latency_p50_ms', 'scheduler_e2e_p50_ms']),
        routerStatOrder,
    ) ?? (isMean ? null : seriesArithmeticMean(rawRows, 'epp_sched_ms'));

    const rawQueueTimeMs = readSummaryStat(
        summary,
        [`vllm_queue_time_${suffix}_ms`, `queue_time_${suffix}_ms`],
        srvStatOrder,
    ) ?? (isMean ? null : seriesArithmeticMean(rawRows, 'queue_ms'));

    const rawNixlMs = isDisaggregated
        ? (readSummaryStat(summary, [`nixl_transfer_latency_${suffix}_ms`], srvStatOrder)
            ?? (isMean ? null : seriesArithmeticMean(rawRows, 'kv_transfer_ms')))
        : null;

    let kvTransferMs = null;
    let queueTimeMs = rawQueueTimeMs;
    if (isDisaggregated && rawNixlMs !== null) {
        const dQueueRaw = readSummaryStat(summaryByRole?.decode, [`vllm_queue_time_${suffix}_ms`], srvStatOrder);
        if (dQueueRaw !== null) {
            kvTransferMs = round3(Math.min(rawNixlMs, dQueueRaw));
            const pQueueEff = readSummaryStat(summaryByRole?.prefill, [`vllm_queue_time_${suffix}_ms`], srvStatOrder) ?? 0;
            queueTimeMs = round3(pQueueEff + Math.max(0, dQueueRaw - kvTransferMs));
        } else if (rawQueueTimeMs !== null) {
            kvTransferMs = round3(Math.min(rawNixlMs, rawQueueTimeMs));
            queueTimeMs = round3(Math.max(0, rawQueueTimeMs - kvTransferMs));
        } else {
            kvTransferMs = round3(rawNixlMs);
        }
    }

    const prefillTimeMs = readSummaryStat(
        summary,
        [`vllm_prefill_time_${suffix}_ms`, `prefill_time_${suffix}_ms`],
        srvStatOrder,
    ) ?? (isMean ? null : seriesArithmeticMean(rawRows, 'prefill_ms'));

    const decodeTimeMs = readSummaryStat(
        summary,
        [`vllm_decode_time_${suffix}_ms`, `decode_time_${suffix}_ms`],
        srvStatOrder,
    ) ?? (isMean ? null : seriesArithmeticMean(rawRows, 'decode_ms'));

    const hasStage = [queueTimeMs, kvTransferMs, prefillTimeMs, decodeTimeMs].some((v) => v !== null);
    if (!hasStage) {
        return {
            available: false,
            source: 'server_telemetry',
            epp_scheduling_ms: null,
            queue_time_ms: null,
            kv_transfer_ms: null,
            prefill_time_ms: null,
            decode_time_ms: null,
            engine_overhead_ms: null,
            gateway_overhead_ms: null,
            ttft_stage_ms: null,
            total_stage_ms: null,
            share_of_e2e_pct: computeShareMap({}, E2E_STAGE_KEYS, null),
            share_of_ttft_pct: computeShareMap({}, TTFT_STAGE_KEYS, null),
        };
    }

    const clientTtftMs = readWindowLatencyMetric(steadyWindow, 'ttft', quantile);
    const clientE2eMs = readWindowLatencyMetric(steadyWindow, 'e2e', quantile);
    const serverTtftMs = readSummaryStat(summary, [`vllm_ttft_${suffix}_ms`], srvStatOrder);
    const serverE2eMs = readSummaryStat(summary, [`vllm_e2e_${suffix}_ms`], srvStatOrder);

    return finalizeServerStageSlice({
        quantile,
        eppSchedulingMs,
        queueTimeMs,
        kvTransferMs,
        prefillTimeMs,
        decodeTimeMs,
        clientTtftMs,
        clientE2eMs,
        serverTtftMs,
        serverE2eMs,
    });
}

function buildServerTailStageSlice(quantile, {
    summary,
    summaryByRole,
    router,
    steadyWindow,
    steadySrvPoints,
    steadyRolePoints,
    effWarmup,
    isDisaggregated,
    hasRoleSep,
}) {
    const aggMode = quantile === 'max' ? 'max' : 'p99';
    const statOrder = aggMode === 'max' ? ['max', 'p99', 'mean'] : ['p99', 'max', 'mean'];
    const clientTtftMs = readWindowLatencyMetric(steadyWindow, 'ttft', quantile);
    const clientE2eMs = readWindowLatencyMetric(steadyWindow, 'e2e', quantile);

    let usedP90StageFallback = false;
    const pickSrvMetric = (vllmBase, altBase, trackFallback = false) => {
        const p99Val = aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: [`${vllmBase}_p99_ms`, `${altBase}_p99_ms`],
            summaryKey: `${vllmBase}_p99_ms`,
        }) ?? aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: [`${altBase}_p99_ms`],
            summaryKey: `${altBase}_p99_ms`,
        });
        if (p99Val !== null) return p99Val;
        const p90Val = aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: [`${vllmBase}_p90_ms`, `${altBase}_p90_ms`],
            summaryKey: `${vllmBase}_p90_ms`,
        }) ?? aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: [`${altBase}_p90_ms`],
            summaryKey: `${altBase}_p90_ms`,
        });
        if (p90Val !== null && trackFallback) usedP90StageFallback = true;
        return p90Val;
    };

    const pickRoleMetric = (role, vllmBase, trackFallback = false) => {
        const p99Val = aggRoleMetric({
            summaryByRole,
            steadyRolePoints,
            effWarmup,
            aggMode,
            statOrder,
            role,
            metricKey: `${vllmBase}_p99_ms`,
        });
        if (p99Val !== null) return p99Val;
        const p90Val = aggRoleMetric({
            summaryByRole,
            steadyRolePoints,
            effWarmup,
            aggMode,
            statOrder,
            role,
            metricKey: `${vllmBase}_p90_ms`,
        });
        if (p90Val !== null && trackFallback) usedP90StageFallback = true;
        return p90Val;
    };

    const useRolePd = Boolean(isDisaggregated && hasRoleSep);
    const eppSchedulingMs = aggEppTailMetric({ router, steadySrvPoints, effWarmup, aggMode, statOrder });
    let queueTimeMs = null;
    let kvTransferMs = null;
    let prefillTimeMs = null;
    let decodeTimeMs = null;
    let serverTtftMs = null;
    let serverE2eMs = null;
    let pTtft = null;
    let dTtft = null;
    let pQueueEff = null;
    let dQueueEff = null;
    let pPrefillMs = null;
    let dPrefillMs = null;

    if (useRolePd) {
        const nixlMs = aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: ['nixl_transfer_latency_p99_ms'],
            summaryKey: 'nixl_transfer_latency_p99_ms',
        });
        const pQRaw = pickRoleMetric('prefill', 'vllm_queue_time', true);
        const pQMean = aggRoleMetric({
            summaryByRole,
            steadyRolePoints,
            effWarmup,
            aggMode: 'mean',
            statOrder: ['mean', 'avg'],
            role: 'prefill',
            metricKey: 'vllm_queue_time_mean_ms',
        });
        const dQRaw = pickRoleMetric('decode', 'vllm_queue_time', true);
        const dQMean = aggRoleMetric({
            summaryByRole,
            steadyRolePoints,
            effWarmup,
            aggMode: 'mean',
            statOrder: ['mean', 'avg'],
            role: 'decode',
            metricKey: 'vllm_queue_time_mean_ms',
        });
        pQueueEff = effectiveRoleQueue(pQRaw, pQMean, quantile);
        dQueueEff = effectiveRoleQueue(dQRaw, dQMean, quantile);
        if (nixlMs !== null) {
            if (dQueueEff !== null) {
                kvTransferMs = round3(Math.min(nixlMs, dQueueEff));
                const dQueueExclKv = round3(Math.max(0, dQueueEff - kvTransferMs));
                queueTimeMs = round3((pQueueEff ?? 0) + dQueueExclKv);
            } else {
                const canonQ = pickSrvMetric('vllm_queue_time', 'queue_time', true);
                if (pQueueEff !== null) {
                    kvTransferMs = round3(nixlMs);
                    queueTimeMs = round3(pQueueEff);
                } else if (canonQ !== null) {
                    kvTransferMs = round3(Math.min(nixlMs, canonQ));
                    queueTimeMs = round3(Math.max(0, canonQ - kvTransferMs));
                } else {
                    kvTransferMs = round3(nixlMs);
                }
            }
        } else if (pQueueEff !== null || dQueueEff !== null) {
            queueTimeMs = round3((pQueueEff ?? 0) + (dQueueEff ?? 0));
        } else {
            queueTimeMs = pickSrvMetric('vllm_queue_time', 'queue_time', true);
        }

        pPrefillMs = pickRoleMetric('prefill', 'vllm_prefill_time', true);
        prefillTimeMs = pPrefillMs ?? pickSrvMetric('vllm_prefill_time', 'prefill_time', true);
        dPrefillMs = pickRoleMetric('decode', 'vllm_prefill_time', false);
        decodeTimeMs = pickRoleMetric('decode', 'vllm_decode_time', false)
            ?? pickSrvMetric('vllm_decode_time', 'decode_time', false);
        pTtft = aggRoleMetric({
            summaryByRole,
            steadyRolePoints,
            effWarmup,
            aggMode,
            statOrder,
            role: 'prefill',
            metricKey: 'vllm_ttft_p99_ms',
        });
        dTtft = aggRoleMetric({
            summaryByRole,
            steadyRolePoints,
            effWarmup,
            aggMode,
            statOrder,
            role: 'decode',
            metricKey: 'vllm_ttft_p99_ms',
        });
        serverTtftMs = aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: ['vllm_ttft_p99_ms'],
            summaryKey: 'vllm_ttft_p99_ms',
        });
        if (serverTtftMs === null && (pTtft !== null || dTtft !== null)) {
            serverTtftMs = round3((pTtft ?? 0) + (dTtft ?? 0));
        }
        serverE2eMs = aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: ['vllm_e2e_p99_ms'],
            summaryKey: 'vllm_e2e_p99_ms',
        });
    } else {
        const rawQueueMs = pickSrvMetric('vllm_queue_time', 'queue_time', true);
        const rawNixlMs = isDisaggregated
            ? aggSrvMetric({
                summary,
                steadySrvPoints,
                effWarmup,
                aggMode,
                statOrder,
                seriesKeys: ['nixl_transfer_latency_p99_ms'],
                summaryKey: 'nixl_transfer_latency_p99_ms',
            })
            : null;
        if (isDisaggregated && rawNixlMs !== null) {
            if (rawQueueMs !== null) {
                kvTransferMs = round3(Math.min(rawNixlMs, rawQueueMs));
                queueTimeMs = round3(Math.max(0, rawQueueMs - kvTransferMs));
            } else {
                kvTransferMs = round3(rawNixlMs);
            }
        } else {
            queueTimeMs = rawQueueMs;
        }
        prefillTimeMs = pickSrvMetric('vllm_prefill_time', 'prefill_time', true);
        decodeTimeMs = pickSrvMetric('vllm_decode_time', 'decode_time', false);
        serverTtftMs = aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: ['vllm_ttft_p99_ms'],
            summaryKey: 'vllm_ttft_p99_ms',
        });
        serverE2eMs = aggSrvMetric({
            summary,
            steadySrvPoints,
            effWarmup,
            aggMode,
            statOrder,
            seriesKeys: ['vllm_e2e_p99_ms'],
            summaryKey: 'vllm_e2e_p99_ms',
        });
    }

    const hasStage = [queueTimeMs, kvTransferMs, prefillTimeMs, decodeTimeMs].some((v) => v !== null);
    if (!hasStage || (clientTtftMs === null && clientE2eMs === null)) {
        return { available: false, source: 'server_telemetry' };
    }

    const sliceNote = quantile === 'max'
        ? 'Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.'
        : (usedP90StageFallback
            ? 'Prefill/Queue P99 estimated from peak-window P90 histograms (re-run to collect native P99 stage histograms).'
            : null);

    return finalizeServerStageSlice({
        quantile,
        eppSchedulingMs,
        queueTimeMs,
        kvTransferMs,
        prefillTimeMs,
        decodeTimeMs,
        clientTtftMs,
        clientE2eMs,
        serverTtftMs,
        serverE2eMs,
        pTtft,
        dTtft,
        pQueueEff,
        dQueueEff,
        pPrefillMs,
        dPrefillMs,
        isDisaggregated,
        useRolePd,
        applyOverAllocGuard: true,
        unboundedTailGuard: quantile === 'max',
        sliceNote,
    });
}

function buildPdPooledStageSlice(quantile, { summary, router, steadyWindow, rawRows }) {
    const isP90 = quantile === 'p90';
    const isMean = quantile === 'mean';
    const suffix = isMean ? 'mean' : (isP90 ? 'p90' : 'p50');
    const routerStatOrder = isMean ? ['mean', 'avg'] : (isP90 ? ['mean', 'p90', 'p95'] : ['mean', 'p50']);
    const srvStatOrder = isMean ? ['mean', 'avg'] : (isP90 ? ['mean', 'p90'] : ['mean', 'p50']);

    const obsTtft = readWindowLatencyMetric(steadyWindow, 'ttft', quantile);
    const obsE2e = readWindowLatencyMetric(steadyWindow, 'e2e', quantile);
    if (obsTtft === null && obsE2e === null) {
        return { available: false, source: 'pd_pooled_fallback' };
    }

    const eppSchedulingMs = readSummaryStat(
        router,
        isMean
            ? ['scheduler_latency_mean_ms', 'scheduler_e2e_mean_ms']
            : (isP90
                ? ['scheduler_latency_p90_ms', 'scheduler_e2e_p90_ms', 'scheduler_latency_p95_ms']
                : ['scheduler_latency_p50_ms', 'scheduler_e2e_p50_ms']),
        routerStatOrder,
    ) ?? (isMean ? null : seriesArithmeticMean(rawRows, 'epp_sched_ms'));

    const rawNixlMs = readSummaryStat(summary, [`nixl_transfer_latency_${suffix}_ms`], srvStatOrder)
        ?? (isMean ? null : seriesArithmeticMean(rawRows, 'kv_transfer_ms'));

    const kvTransferMs = rawNixlMs !== null
        ? (obsTtft !== null ? round3(Math.min(rawNixlMs, Math.max(0, obsTtft - (eppSchedulingMs ?? 0)))) : round3(rawNixlMs))
        : null;

    const ttftUnattributedMs = obsTtft !== null
        ? round3(Math.max(0, obsTtft - ((eppSchedulingMs ?? 0) + (kvTransferMs ?? 0))))
        : null;
    const decodeTimeMs = (obsE2e !== null && obsTtft !== null)
        ? round3(Math.max(0, obsE2e - obsTtft))
        : null;
    const ttftStageMs = obsTtft;
    const totalStageMs = (ttftStageMs !== null || decodeTimeMs !== null)
        ? round3((ttftStageMs ?? 0) + (decodeTimeMs ?? 0))
        : null;

    const e2eValues = {
        epp_scheduling: eppSchedulingMs,
        kv_transfer: kvTransferMs,
        ttft_unattributed: ttftUnattributedMs,
        decode_time: decodeTimeMs,
    };
    const ttftValues = {
        epp_scheduling: eppSchedulingMs,
        kv_transfer: kvTransferMs,
        ttft_unattributed: ttftUnattributedMs,
    };

    return {
        available: true,
        source: 'pd_pooled_fallback',
        epp_scheduling_ms: eppSchedulingMs,
        queue_time_ms: null,
        kv_transfer_ms: kvTransferMs,
        prefill_time_ms: null,
        decode_time_ms: decodeTimeMs,
        engine_overhead_ms: null,
        gateway_overhead_ms: null,
        ttft_unattributed_ms: ttftUnattributedMs,
        ttft_stage_ms: ttftStageMs,
        total_stage_ms: totalStageMs,
        client_ttft_ms: obsTtft,
        client_e2e_ms: obsE2e,
        slice_note: null,
        reconciliation: buildStageReconciliation(obsE2e, totalStageMs, quantile),
        share_of_e2e_pct: computeShareMap(e2eValues, ['epp_scheduling', 'kv_transfer', 'ttft_unattributed', 'decode_time'], totalStageMs),
        share_of_ttft_pct: computeShareMap(ttftValues, ['epp_scheduling', 'kv_transfer', 'ttft_unattributed'], ttftStageMs),
    };
}

function buildPdPooledTailStageSlice(quantile, { summary, router, steadyWindow, steadySrvPoints, effWarmup }) {
    const aggMode = quantile === 'max' ? 'max' : 'p99';
    const statOrder = aggMode === 'max' ? ['max', 'p99', 'mean'] : ['p99', 'max', 'mean'];
    const obsTtft = readWindowLatencyMetric(steadyWindow, 'ttft', quantile);
    const obsE2e = readWindowLatencyMetric(steadyWindow, 'e2e', quantile);
    if (obsTtft === null && obsE2e === null) {
        return { available: false, source: 'pd_pooled_fallback' };
    }
    const eppSchedulingMs = aggEppTailMetric({ router, steadySrvPoints, effWarmup, aggMode, statOrder });
    const rawNixlMs = aggSrvMetric({
        summary,
        steadySrvPoints,
        effWarmup,
        aggMode,
        statOrder,
        seriesKeys: ['nixl_transfer_latency_p99_ms'],
        summaryKey: 'nixl_transfer_latency_p99_ms',
    });
    const kvTransferMs = rawNixlMs !== null
        ? (obsTtft !== null ? round3(Math.min(rawNixlMs, Math.max(0, obsTtft - (eppSchedulingMs ?? 0)))) : round3(rawNixlMs))
        : null;
    const ttftUnattributedMs = obsTtft !== null
        ? round3(Math.max(0, obsTtft - ((eppSchedulingMs ?? 0) + (kvTransferMs ?? 0))))
        : null;
    const decodeTimeMs = (obsE2e !== null && obsTtft !== null)
        ? round3(Math.max(0, obsE2e - obsTtft))
        : null;
    const ttftStageMs = obsTtft;
    const totalStageMs = (ttftStageMs !== null || decodeTimeMs !== null)
        ? round3((ttftStageMs ?? 0) + (decodeTimeMs ?? 0))
        : null;
    const e2eValues = {
        epp_scheduling: eppSchedulingMs,
        kv_transfer: kvTransferMs,
        ttft_unattributed: ttftUnattributedMs,
        decode_time: decodeTimeMs,
    };
    const ttftValues = {
        epp_scheduling: eppSchedulingMs,
        kv_transfer: kvTransferMs,
        ttft_unattributed: ttftUnattributedMs,
    };
    const sliceNote = quantile === 'max'
        ? 'Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.'
        : null;
    return {
        available: true,
        source: 'pd_pooled_fallback',
        epp_scheduling_ms: eppSchedulingMs,
        queue_time_ms: null,
        kv_transfer_ms: kvTransferMs,
        prefill_time_ms: null,
        decode_time_ms: decodeTimeMs,
        engine_overhead_ms: null,
        gateway_overhead_ms: null,
        ttft_unattributed_ms: ttftUnattributedMs,
        ttft_stage_ms: ttftStageMs,
        total_stage_ms: totalStageMs,
        client_ttft_ms: obsTtft,
        client_e2e_ms: obsE2e,
        slice_note: sliceNote,
        reconciliation: buildStageReconciliation(obsE2e, totalStageMs, quantile),
        share_of_e2e_pct: computeShareMap(e2eValues, ['epp_scheduling', 'kv_transfer', 'ttft_unattributed', 'decode_time'], totalStageMs),
        share_of_ttft_pct: computeShareMap(ttftValues, ['epp_scheduling', 'kv_transfer', 'ttft_unattributed'], ttftStageMs),
    };
}

function buildClientStageSlice(quantile, steadyWindow, sliceNote = null, sourceOverride = 'client_fallback') {
    const obsTtft = readWindowLatencyMetric(steadyWindow, 'ttft', quantile);
    const obsE2e = readWindowLatencyMetric(steadyWindow, 'e2e', quantile);
    const decodeTimeMs = (obsE2e !== null && obsTtft !== null)
        ? round3(Math.max(0, obsE2e - obsTtft))
        : null;
    const ttftStageMs = obsTtft;
    const totalStageMs = (ttftStageMs !== null || decodeTimeMs !== null)
        ? round3((ttftStageMs ?? 0) + (decodeTimeMs ?? 0))
        : null;
    const available = totalStageMs !== null;
    const validDenom = totalStageMs !== null && totalStageMs > 0;
    return {
        available,
        source: available ? sourceOverride : 'unavailable',
        epp_scheduling_ms: null,
        queue_time_ms: null,
        kv_transfer_ms: null,
        prefill_time_ms: null,
        decode_time_ms: decodeTimeMs,
        engine_overhead_ms: null,
        gateway_overhead_ms: null,
        ttft_unattributed_ms: ttftStageMs,
        ttft_stage_ms: ttftStageMs,
        total_stage_ms: totalStageMs,
        client_ttft_ms: obsTtft,
        client_e2e_ms: obsE2e,
        server_ttft_clamped: false,
        caveat: null,
        slice_note: sliceNote,
        share_of_e2e_pct: {
            epp_scheduling: null,
            queue_time: null,
            kv_transfer: null,
            prefill_time: null,
            engine_overhead: null,
            decode_time: validDenom && decodeTimeMs !== null ? round1((decodeTimeMs / totalStageMs) * 100) : null,
            gateway_overhead: null,
            ttft_unattributed: validDenom && ttftStageMs !== null ? round1((ttftStageMs / totalStageMs) * 100) : null,
        },
        share_of_ttft_pct: computeShareMap({}, TTFT_STAGE_KEYS, null),
    };
}

function buildMinStageSlice(steadyWindow, source = 'client_fallback') {
    return buildClientStageSlice(
        'min',
        steadyWindow,
        'Server stage histograms do not expose single-request minimums; showing exact client Min TTFT.',
        source,
    );
}

function findNearestServerSample(samples, targetT, maxDist) {
    if (!Array.isArray(samples) || !samples.length || !isFiniteMetricValue(targetT)) return null;
    let best = null;
    let bestDist = Infinity;
    for (const sample of samples) {
        if (!isFiniteMetricValue(sample?.t)) continue;
        const dist = Math.abs(sample.t - targetT);
        if (dist <= maxDist && dist < bestDist) {
            bestDist = dist;
            best = sample;
        }
    }
    return best;
}

function emptyStageLatencyView() {
    return {
        available: false,
        source: 'unavailable',
        isLegacyPooledPd: false,
        hasP90: false,
        hasKvTransfer: false,
        caveat: null,
        roleBreakdown: null,
        p50Series: [],
        p90Series: [],
        slices: { min: null, p50: null, mean: null, p90: null, p99: null, max: null },
        dominantE2eStage: null,
        dominantTtftStage: null,
        dominantE2eStageP90: null,
        dominantTtftStageP90: null,
        peakWindowShift: null,
    };
}

export const TTFT_BAR_OPACITY_BY_KEY = {
    prefill_ms: 1,
    decode_first_step_ms: 0.45,
    queue_ms: 1,
    kv_transfer_ms: 1,
    prefill_engine_overhead_ms: 1,
    decode_handoff_overhead_ms: 0.45,
    engine_overhead_unsplit_ms: 1,
    epp_scheduling_ms: 1,
    gateway_overhead_ms: 0.45,
    ttft_unattributed_ms: 1,
    client_unattributed_ms: 0.38,
    residual_ms: 1,
};

function effectiveRoleQueue(qVal, qMeanVal, q) {
    const qNum = nonNegMetric(qVal);
    if (qNum === null) return null;
    const meanNum = nonNegMetric(qMeanVal);
    if (q !== 'p50' && q !== 'p90' && q !== 'p99' && q !== 'max') {
        return meanNum !== null ? meanNum : qNum;
    }
    const ceiling = q === 'p50' ? 160 : (q === 'p90' ? 280 : 305);
    if (meanNum !== null && meanNum < 25 && qNum <= ceiling) {
        return meanNum;
    }
    return qNum;
}

function reconcilePdEngineOverhead({
    engineOverheadMs,
    serverTtftClamped,
    pTtft,
    pQueueEff,
    pPrefill,
    dTtft,
    dQueueEff,
    dPrefill,
}) {
    if (
        !isFiniteMetricValue(engineOverheadMs)
        || serverTtftClamped === true
        || !isFiniteMetricValue(pTtft)
        || !isFiniteMetricValue(pQueueEff)
        || !isFiniteMetricValue(pPrefill)
        || !isFiniteMetricValue(dTtft)
        || !isFiniteMetricValue(dQueueEff)
    ) {
        return {
            decode_first_step_ms: null,
            prefill_engine_overhead_ms: null,
            decode_handoff_overhead_ms: null,
        };
    }
    const target = round3(Math.max(0, Number(engineOverheadMs)));
    if (target <= 0) {
        return {
            decode_first_step_ms: 0,
            prefill_engine_overhead_ms: 0,
            decode_handoff_overhead_ms: 0,
        };
    }
    const rawDPre = Math.max(0, isFiniteMetricValue(dPrefill) ? Number(dPrefill) : 0);
    const rawPOh = Math.max(0, Number(pTtft) - Number(pQueueEff) - Number(pPrefill));
    const rawDOh = Math.max(0, Number(dTtft) - Number(dQueueEff) - rawDPre);
    const raw = [rawDPre, rawPOh, rawDOh];
    const rawSum = raw[0] + raw[1] + raw[2];
    if (rawSum <= 0) {
        return {
            decode_first_step_ms: null,
            prefill_engine_overhead_ms: null,
            decode_handoff_overhead_ms: null,
        };
    }
    const scale = target / rawSum;
    const vals = raw.map((r) => round3(r * scale));
    const drift = round3(target - (vals[0] + vals[1] + vals[2]));
    if (drift !== 0) {
        let bestIdx = 0;
        for (let i = 1; i < raw.length; i += 1) {
            if (raw[i] > raw[bestIdx]) bestIdx = i;
        }
        vals[bestIdx] = round3(Math.max(0, vals[bestIdx] + drift));
    }
    return {
        decode_first_step_ms: vals[0],
        prefill_engine_overhead_ms: vals[1],
        decode_handoff_overhead_ms: vals[2],
    };
}

function normalizeV6StageSlice(slbSlice, suffix) {
    if (
        !slbSlice
        || typeof slbSlice !== 'object'
        || slbSlice.available !== true
        || slbSlice.source !== 'server_telemetry'
        || Object.prototype.hasOwnProperty.call(slbSlice, 'engine_overhead_ms')
        || !isFiniteMetricValue(slbSlice.server_ttft_ms)
        || !isFiniteMetricValue(slbSlice.gateway_overhead_ms)
    ) {
        return slbSlice ?? null;
    }
    const v6Residual = round3(Math.max(0, Number(slbSlice.gateway_overhead_ms)));
    const serverTtftMs = Number(slbSlice.server_ttft_ms);
    const clientTtftMs = isFiniteMetricValue(slbSlice.client_ttft_ms) ? Number(slbSlice.client_ttft_ms) : null;
    const isClamped = Boolean(slbSlice.server_ttft_clamped)
        || (suffix !== 'mean' && serverTtftMs >= 9500 && clientTtftMs !== null && clientTtftMs > serverTtftMs + 500);
    let engineOverheadMs;
    let gatewayOverheadMs;
    if (isClamped) {
        engineOverheadMs = v6Residual;
        gatewayOverheadMs = null;
    } else {
        const known = (nonNegMetric(slbSlice.queue_time_ms) ?? 0)
            + (nonNegMetric(slbSlice.kv_transfer_ms) ?? 0)
            + (nonNegMetric(slbSlice.prefill_time_ms) ?? 0);
        engineOverheadMs = round3(Math.min(v6Residual, Math.max(0, serverTtftMs - known)));
        gatewayOverheadMs = round3(Math.max(0, v6Residual - engineOverheadMs));
    }
    const e2eValues = {
        epp_scheduling: nonNegMetric(slbSlice.epp_scheduling_ms),
        queue_time: nonNegMetric(slbSlice.queue_time_ms),
        kv_transfer: nonNegMetric(slbSlice.kv_transfer_ms),
        prefill_time: nonNegMetric(slbSlice.prefill_time_ms),
        engine_overhead: engineOverheadMs,
        gateway_overhead: gatewayOverheadMs,
        decode_time: nonNegMetric(slbSlice.decode_time_ms),
    };
    const ttftValues = {
        epp_scheduling: e2eValues.epp_scheduling,
        queue_time: e2eValues.queue_time,
        kv_transfer: e2eValues.kv_transfer,
        prefill_time: e2eValues.prefill_time,
        engine_overhead: engineOverheadMs,
        gateway_overhead: gatewayOverheadMs,
    };
    const ttftParts = Object.values(ttftValues).filter((v) => v !== null);
    const ttftStageMs = isFiniteMetricValue(slbSlice.ttft_stage_ms)
        ? Number(slbSlice.ttft_stage_ms)
        : (ttftParts.length > 0 ? round3(ttftParts.reduce((sum, v) => sum + v, 0)) : null);
    const totalParts = Object.values(e2eValues).filter((v) => v !== null);
    const totalStageMs = isFiniteMetricValue(slbSlice.total_stage_ms)
        ? Number(slbSlice.total_stage_ms)
        : (totalParts.length > 0 ? round3(totalParts.reduce((sum, v) => sum + v, 0)) : null);
    const existingE2e = (slbSlice.share_of_e2e_pct && typeof slbSlice.share_of_e2e_pct === 'object')
        ? slbSlice.share_of_e2e_pct
        : {};
    const existingTtft = (slbSlice.share_of_ttft_pct && typeof slbSlice.share_of_ttft_pct === 'object')
        ? slbSlice.share_of_ttft_pct
        : {};
    const validE2eDenom = totalStageMs !== null && Number.isFinite(totalStageMs) && totalStageMs > 0;
    const validTtftDenom = ttftStageMs !== null && Number.isFinite(ttftStageMs) && ttftStageMs > 0;
    return {
        ...slbSlice,
        engine_overhead_ms: engineOverheadMs,
        gateway_overhead_ms: gatewayOverheadMs,
        ...(isClamped ? { server_ttft_clamped: true } : {}),
        share_of_e2e_pct: {
            ...existingE2e,
            engine_overhead: validE2eDenom && engineOverheadMs !== null ? round1((engineOverheadMs / totalStageMs) * 100) : null,
            gateway_overhead: validE2eDenom && gatewayOverheadMs !== null ? round1((gatewayOverheadMs / totalStageMs) * 100) : null,
        },
        share_of_ttft_pct: {
            ...existingTtft,
            engine_overhead: validTtftDenom && engineOverheadMs !== null ? round1((engineOverheadMs / ttftStageMs) * 100) : null,
            gateway_overhead: validTtftDenom && gatewayOverheadMs !== null ? round1((gatewayOverheadMs / ttftStageMs) * 100) : null,
        },
    };
}

export function buildStageLatencyView(forensics, serverTelemetry, clientSeries) {
    const slb = forensics?.stage_latency_breakdown;
    const legacyPooled = isLegacyPooledPd(forensics, serverTelemetry);
    const effWarmup = nonNegMetric(clientSeries?.warmup_seconds) ?? 0;
    const windows = clientSeries?.windows || {};
    const steadyWindow = effWarmup <= 0
        ? (windows.full ?? windows.profiling ?? null)
        : (windows.profiling ?? windows.full ?? null);
    const isServerAvailable = Boolean(
        serverTelemetry
        && serverTelemetry.available !== false
        && serverTelemetry.status !== 'unavailable',
    );
    const rawSrvSeries = isServerAvailable && Array.isArray(serverTelemetry.series) ? serverTelemetry.series : [];
    const rawRoleSeries = isServerAvailable && Array.isArray(serverTelemetry.role_series) ? serverTelemetry.role_series : [];
    const srvSummary = serverTelemetry?.summary || {};
    const srvSummaryByRole = serverTelemetry?.summary_by_role || null;
    const srvRouter = serverTelemetry?.router || {};
    const originMs = resolveTelemetryOriginMs(clientSeries, serverTelemetry, null);
    const stepSeconds = firstFiniteMetric(serverTelemetry?.step_seconds, 5) || 5;
    const bucketSeconds = firstFiniteMetric(clientSeries?.bucket_seconds, 5) || 5;
    const isDisaggregated = String(serverTelemetry?.topology_mode || '').toLowerCase() === 'disaggregated';
    const hasRoleSep = hasRoleSeparatedLatency(serverTelemetry);
    const steadySrvPoints = filterSteadyPoints(rawSrvSeries, originMs, effWarmup);
    const steadyRolePoints = filterSteadyPoints(rawRoleSeries, originMs, effWarmup);

    if (slb && typeof slb === 'object') {
        if (slb.available === false) {
            return emptyStageLatencyView();
        }
        const source = slb.source || 'server_telemetry';
        const rawSeries = Array.isArray(slb.series) ? slb.series : [];
        const isClientFallback = source === 'client_fallback';
        const isPdPooled = source === 'pd_pooled_fallback';
        let minSlice = normalizeV6StageSlice(slb.min ?? null, 'min');
        const p50Slice = normalizeV6StageSlice(slb.p50 ?? null, 'p50');
        let meanSlice = normalizeV6StageSlice(slb.mean ?? null, 'mean');
        const p90Slice = normalizeV6StageSlice(slb.p90 ?? null, 'p90');
        let p99Slice = normalizeV6StageSlice(slb.p99 ?? null, 'p99');
        let maxSlice = normalizeV6StageSlice(slb.max ?? null, 'max');

        const canSynthesizeMissing = !isClientFallback && Boolean(steadyWindow) && hasServerLatencyTelemetry(serverTelemetry);
        if (canSynthesizeMissing) {
            if (!minSlice || minSlice.available === false) {
                const builtMin = buildMinStageSlice(steadyWindow, isPdPooled ? 'pd_pooled_fallback' : 'client_fallback');
                if (builtMin.available) minSlice = builtMin;
            }
            if (isPdPooled) {
                if (!meanSlice || meanSlice.available === false) {
                    const builtMean = buildPdPooledStageSlice('mean', { summary: srvSummary, router: srvRouter, steadyWindow, rawRows: [] });
                    if (builtMean.available) meanSlice = builtMean;
                }
                if (!p99Slice || p99Slice.available === false) {
                    const builtP99 = buildPdPooledTailStageSlice('p99', { summary: srvSummary, router: srvRouter, steadyWindow, steadySrvPoints, effWarmup });
                    if (builtP99.available) p99Slice = builtP99;
                }
                if (!maxSlice || maxSlice.available === false) {
                    const builtMax = buildPdPooledTailStageSlice('max', { summary: srvSummary, router: srvRouter, steadyWindow, steadySrvPoints, effWarmup });
                    if (builtMax.available) maxSlice = builtMax;
                }
            } else {
                if (!meanSlice || meanSlice.available === false) {
                    const hasServerMean = ['vllm_queue_time_mean_ms', 'vllm_prefill_time_mean_ms', 'vllm_decode_time_mean_ms'].some((k) => (
                        readSummaryStat(srvSummary, [k], ['mean', 'avg']) !== null
                        || steadySrvPoints.some((pt) => nonNegMetric(pt?.[k]) !== null)
                        || (isDisaggregated && hasRoleSep && (
                            readSummaryStat(srvSummaryByRole?.prefill, [k], ['mean', 'avg']) !== null
                            || readSummaryStat(srvSummaryByRole?.decode, [k], ['mean', 'avg']) !== null
                        ))
                    ));
                    if (hasServerMean) {
                        const builtMean = buildServerStageSlice('mean', {
                            summary: srvSummary,
                            summaryByRole: srvSummaryByRole,
                            router: srvRouter,
                            steadyWindow,
                            rawRows: [],
                            isDisaggregated,
                        });
                        if (builtMean.available) meanSlice = builtMean;
                    }
                    if (!meanSlice || meanSlice.available === false) {
                        const builtClientMean = buildClientStageSlice(
                            'mean',
                            steadyWindow,
                            'Server mean stage histograms were not recorded; showing exact client Mean TTFT.',
                            'client_fallback',
                        );
                        if (builtClientMean.available) meanSlice = builtClientMean;
                    }
                }
                if (!p99Slice || p99Slice.available === false) {
                    const builtP99 = buildServerTailStageSlice('p99', {
                        summary: srvSummary,
                        summaryByRole: srvSummaryByRole,
                        router: srvRouter,
                        steadyWindow,
                        steadySrvPoints,
                        steadyRolePoints,
                        effWarmup,
                        isDisaggregated,
                        hasRoleSep,
                    });
                    if (builtP99.available) {
                        p99Slice = builtP99;
                    } else if (readWindowLatencyMetric(steadyWindow, 'ttft', 'p99') !== null) {
                        const builtClientP99 = buildClientStageSlice('p99', steadyWindow, null, 'client_fallback');
                        if (builtClientP99.available) p99Slice = builtClientP99;
                    }
                }
                if (!maxSlice || maxSlice.available === false) {
                    const builtMax = buildServerTailStageSlice('max', {
                        summary: srvSummary,
                        summaryByRole: srvSummaryByRole,
                        router: srvRouter,
                        steadyWindow,
                        steadySrvPoints,
                        steadyRolePoints,
                        effWarmup,
                        isDisaggregated,
                        hasRoleSep,
                    });
                    if (builtMax.available) {
                        maxSlice = builtMax;
                    } else if (readWindowLatencyMetric(steadyWindow, 'ttft', 'max') !== null) {
                        const builtClientMax = buildClientStageSlice(
                            'max',
                            steadyWindow,
                            'Server histograms do not report per-request max; showing exact client Max TTFT.',
                            'client_fallback',
                        );
                        if (builtClientMax.available) maxSlice = builtClientMax;
                    }
                }
            }
        }

        const hasKvTransfer = Boolean(
            slb.has_kv_transfer_series
            || isFiniteMetricValue(p50Slice?.kv_transfer_ms)
            || isFiniteMetricValue(p90Slice?.kv_transfer_ms)
            || isFiniteMetricValue(p99Slice?.kv_transfer_ms)
            || isFiniteMetricValue(maxSlice?.kv_transfer_ms)
            || isFiniteMetricValue(meanSlice?.kv_transfer_ms)
            || rawSeries.some((pt) => isFiniteMetricValue(pt?.kv_transfer_ms) || isFiniteMetricValue(pt?.kv_transfer_p90_ms)),
        );
        const stackKeys = isClientFallback
            ? CLIENT_STAGE_STACK_KEYS
            : (isPdPooled
                ? (hasKvTransfer ? PD_POOLED_STAGE_STACK_KEYS : ['epp_sched_ms', 'ttft_unattributed_ms', 'decode_ms'])
                : (hasKvTransfer ? SERVER_PD_STAGE_STACK_KEYS : SERVER_STAGE_STACK_KEYS));

        const p50Rows = rawSeries.map((pt, idx) => {
            const t = firstFiniteMetric(pt?.t, pt?.elapsed_seconds, pt?.time_seconds, idx);
            if (isClientFallback) {
                return { t, ttft_ms: nonNegMetric(pt?.ttft_ms), decode_ms: nonNegMetric(pt?.decode_ms) };
            }
            if (isPdPooled) {
                return {
                    t,
                    epp_sched_ms: nonNegMetric(pt?.epp_sched_ms),
                    ...(hasKvTransfer ? { kv_transfer_ms: nonNegMetric(pt?.kv_transfer_ms) } : {}),
                    ttft_unattributed_ms: nonNegMetric(firstFiniteMetric(pt?.ttft_unattributed_ms, pt?.ttft_ms)),
                    decode_ms: nonNegMetric(pt?.decode_ms),
                };
            }
            return {
                t,
                epp_sched_ms: nonNegMetric(pt?.epp_sched_ms),
                queue_ms: nonNegMetric(pt?.queue_ms),
                ...(hasKvTransfer ? { kv_transfer_ms: nonNegMetric(pt?.kv_transfer_ms) } : {}),
                prefill_ms: nonNegMetric(pt?.prefill_ms),
                decode_ms: nonNegMetric(pt?.decode_ms),
            };
        });
        const p90Rows = rawSeries.map((pt, idx) => {
            const t = firstFiniteMetric(pt?.t, pt?.elapsed_seconds, pt?.time_seconds, idx);
            if (isClientFallback) {
                return { t, ttft_ms: nonNegMetric(pt?.ttft_p90_ms), decode_ms: nonNegMetric(pt?.decode_p90_ms) };
            }
            if (isPdPooled) {
                return {
                    t,
                    epp_sched_ms: nonNegMetric(pt?.epp_sched_p90_ms),
                    ...(hasKvTransfer ? { kv_transfer_ms: nonNegMetric(pt?.kv_transfer_p90_ms) } : {}),
                    ttft_unattributed_ms: nonNegMetric(firstFiniteMetric(pt?.ttft_unattributed_p90_ms, pt?.ttft_p90_ms)),
                    decode_ms: nonNegMetric(pt?.decode_p90_ms),
                };
            }
            return {
                t,
                epp_sched_ms: nonNegMetric(pt?.epp_sched_p90_ms),
                queue_ms: nonNegMetric(pt?.queue_p90_ms),
                ...(hasKvTransfer ? { kv_transfer_ms: nonNegMetric(pt?.kv_transfer_p90_ms) } : {}),
                prefill_ms: nonNegMetric(pt?.prefill_p90_ms),
                decode_ms: nonNegMetric(pt?.decode_p90_ms),
            };
        });
        const p50Series = normalizeStageStackSeries(p50Rows, stackKeys);
        const p90Series = normalizeStageStackSeries(p90Rows, stackKeys);
        const hasP90 = Boolean(slb.has_p90_series) || Boolean(p90Slice?.available) || hasAnyFiniteSeriesKey(p90Series, stackKeys);
        const topCaveat = slb.caveat
            ?? p50Slice?.caveat
            ?? p90Slice?.caveat
            ?? p99Slice?.caveat
            ?? maxSlice?.caveat
            ?? meanSlice?.caveat
            ?? null;
        return {
            available: Boolean(slb.available) || Boolean(p50Slice?.available) || Boolean(p90Slice?.available) || hasAnyFiniteSeriesKey(p50Series, stackKeys),
            source,
            isLegacyPooledPd: isPdPooled || legacyPooled,
            hasP90,
            hasKvTransfer,
            caveat: topCaveat,
            roleBreakdown: slb.role_breakdown ?? null,
            p50Series,
            p90Series,
            slices: {
                min: minSlice,
                p50: p50Slice,
                mean: meanSlice,
                p90: p90Slice,
                p99: p99Slice,
                max: maxSlice,
            },
            dominantE2eStage: slb.dominant_e2e_stage ?? null,
            dominantTtftStage: slb.dominant_ttft_stage ?? null,
            dominantE2eStageP90: slb.dominant_e2e_stage_p90 ?? null,
            dominantTtftStageP90: slb.dominant_ttft_stage_p90 ?? null,
            peakWindowShift: slb.peak_window_shift ?? null,
        };
    }

    if (legacyPooled) {
        const srvSamples = rawSrvSeries.map((pt, idx) => ({
            t: pointRelativeSeconds(pt, originMs, idx * stepSeconds),
            epp_sched_ms: nonNegMetric(firstFiniteMetric(pt?.router_scheduler_latency_p50_ms, pt?.router_scheduler_e2e_p50_ms)),
            kv_transfer_ms: nonNegMetric(pt?.nixl_transfer_latency_p50_ms),
            epp_sched_p90_ms: nonNegMetric(firstFiniteMetric(pt?.router_scheduler_latency_p90_ms, pt?.router_scheduler_e2e_p90_ms)),
            kv_transfer_p90_ms: nonNegMetric(pt?.nixl_transfer_latency_p90_ms),
        }));
        const buckets = Array.isArray(clientSeries?.buckets) ? clientSeries.buckets : [];
        const maxDist = Math.max(5, stepSeconds, bucketSeconds);
        const pdP50Raw = buckets.map((b, idx) => {
            const t = firstFiniteMetric(b?.mid_seconds, b?.time_seconds, b?.end_seconds, b?.elapsed_seconds, b?.t, idx * bucketSeconds);
            const ttft = readBucketLatencyMetric(b, 'ttft', 'p50');
            const e2e = readBucketLatencyMetric(b, 'e2e', 'p50');
            const srv = findNearestServerSample(srvSamples, t, maxDist);
            const epp = srv?.epp_sched_ms ?? null;
            const rawNixl = srv?.kv_transfer_ms ?? null;
            const kvXfer = rawNixl !== null && ttft !== null
                ? round3(Math.min(rawNixl, Math.max(0, ttft - (epp ?? 0))))
                : rawNixl;
            const ttftUnattr = ttft !== null
                ? round3(Math.max(0, ttft - ((epp ?? 0) + (kvXfer ?? 0))))
                : null;
            return {
                t,
                epp_sched_ms: epp,
                kv_transfer_ms: kvXfer,
                ttft_unattributed_ms: ttftUnattr,
                decode_ms: ttft !== null && e2e !== null ? round3(Math.max(0, e2e - ttft)) : null,
            };
        });
        const pdP90Raw = buckets.map((b, idx) => {
            const t = firstFiniteMetric(b?.mid_seconds, b?.time_seconds, b?.end_seconds, b?.elapsed_seconds, b?.t, idx * bucketSeconds);
            const ttft = readBucketLatencyMetric(b, 'ttft', 'p90');
            const e2e = readBucketLatencyMetric(b, 'e2e', 'p90');
            const srv = findNearestServerSample(srvSamples, t, maxDist);
            const epp = srv?.epp_sched_p90_ms ?? null;
            const rawNixl = srv?.kv_transfer_p90_ms ?? null;
            const kvXfer = rawNixl !== null && ttft !== null
                ? round3(Math.min(rawNixl, Math.max(0, ttft - (epp ?? 0))))
                : rawNixl;
            const ttftUnattr = ttft !== null
                ? round3(Math.max(0, ttft - ((epp ?? 0) + (kvXfer ?? 0))))
                : null;
            return {
                t,
                epp_sched_ms: epp,
                kv_transfer_ms: kvXfer,
                ttft_unattributed_ms: ttftUnattr,
                decode_ms: ttft !== null && e2e !== null ? round3(Math.max(0, e2e - ttft)) : null,
            };
        });
        const minSlice = buildMinStageSlice(steadyWindow, 'pd_pooled_fallback');
        const p50Slice = buildPdPooledStageSlice('p50', { summary: srvSummary, router: srvRouter, steadyWindow, rawRows: pdP50Raw });
        const p90Slice = buildPdPooledStageSlice('p90', { summary: srvSummary, router: srvRouter, steadyWindow, rawRows: pdP90Raw });
        const meanSlice = buildPdPooledStageSlice('mean', { summary: srvSummary, router: srvRouter, steadyWindow, rawRows: [] });
        const p99Slice = buildPdPooledTailStageSlice('p99', { summary: srvSummary, router: srvRouter, steadyWindow, steadySrvPoints, effWarmup });
        const maxSlice = buildPdPooledTailStageSlice('max', { summary: srvSummary, router: srvRouter, steadyWindow, steadySrvPoints, effWarmup });
        const hasPdKvTransfer = isFiniteMetricValue(p50Slice.kv_transfer_ms)
            || isFiniteMetricValue(p90Slice.kv_transfer_ms)
            || isFiniteMetricValue(p99Slice.kv_transfer_ms)
            || isFiniteMetricValue(maxSlice.kv_transfer_ms)
            || isFiniteMetricValue(meanSlice.kv_transfer_ms)
            || hasAnyFiniteSeriesKey(pdP50Raw, ['kv_transfer_ms'])
            || hasAnyFiniteSeriesKey(pdP90Raw, ['kv_transfer_ms']);
        const pdStackKeys = hasPdKvTransfer
            ? PD_POOLED_STAGE_STACK_KEYS
            : ['epp_sched_ms', 'ttft_unattributed_ms', 'decode_ms'];
        const hasP50Series = hasAnyFiniteSeriesKey(pdP50Raw, pdStackKeys);
        const hasP90Series = hasAnyFiniteSeriesKey(pdP90Raw, pdStackKeys);
        if (p50Slice.available || p90Slice.available || meanSlice.available || hasP50Series || hasP90Series) {
            const pdDomE2e = (sl) => {
                if (!sl?.available) return null;
                const dec = sl.decode_time_ms ?? -1;
                const ttftU = sl.ttft_unattributed_ms ?? -1;
                const kv = sl.kv_transfer_ms ?? -1;
                if (dec > ttftU && dec > kv) return 'decode_time';
                if (kv > dec && kv > ttftU) return 'kv_transfer';
                return null;
            };
            const pdDomTtft = (sl) => ((sl?.share_of_ttft_pct?.kv_transfer ?? 0) >= 50 ? 'kv_transfer' : null);
            const primarySlice = p50Slice.available ? p50Slice : (p90Slice.available ? p90Slice : meanSlice);
            return {
                available: true,
                source: 'pd_pooled_fallback',
                isLegacyPooledPd: true,
                hasP90: Boolean(p90Slice.available || hasP90Series),
                hasKvTransfer: hasPdKvTransfer,
                caveat: PD_POOLED_CAVEAT,
                roleBreakdown: null,
                p50Series: normalizeStageStackSeries(pdP50Raw, pdStackKeys),
                p90Series: normalizeStageStackSeries(pdP90Raw, pdStackKeys),
                slices: {
                    min: minSlice.available ? minSlice : null,
                    p50: p50Slice.available ? p50Slice : null,
                    mean: meanSlice.available ? meanSlice : null,
                    p90: p90Slice.available ? p90Slice : null,
                    p99: p99Slice.available ? p99Slice : null,
                    max: maxSlice.available ? maxSlice : null,
                },
                dominantE2eStage: pdDomE2e(primarySlice),
                dominantTtftStage: pdDomTtft(primarySlice),
                dominantE2eStageP90: p90Slice.available ? pdDomE2e(p90Slice) : null,
                dominantTtftStageP90: p90Slice.available ? pdDomTtft(p90Slice) : null,
                peakWindowShift: null,
            };
        }
    }

    const hasServerNixl = isDisaggregated && (
        srvSummary.nixl_transfer_latency_p50_ms != null
        || srvSummary.nixl_transfer_latency_p90_ms != null
        || srvSummary.nixl_transfer_latency_p99_ms != null
        || srvSummary.nixl_transfer_latency_mean_ms != null
        || rawSrvSeries.some((pt) => (
            isFiniteMetricValue(pt?.nixl_transfer_latency_p50_ms)
            || isFiniteMetricValue(pt?.nixl_transfer_latency_p90_ms)
            || isFiniteMetricValue(pt?.nixl_transfer_latency_p99_ms)
        ))
    );
    const serverStackKeys = hasServerNixl ? SERVER_PD_STAGE_STACK_KEYS : SERVER_STAGE_STACK_KEYS;

    const srvP50Raw = rawSrvSeries.map((pt, idx) => {
        const t = pointRelativeSeconds(pt, originMs, idx * stepSeconds);
        const epp = nonNegMetric(firstFiniteMetric(pt?.router_scheduler_latency_p50_ms, pt?.router_scheduler_e2e_p50_ms));
        const rawQ = nonNegMetric(firstFiniteMetric(pt?.vllm_queue_time_p50_ms, pt?.queue_time_p50_ms));
        const rawNixl = hasServerNixl ? nonNegMetric(pt?.nixl_transfer_latency_p50_ms) : null;
        const kvXfer = (hasServerNixl && rawNixl !== null)
            ? (rawQ !== null ? round3(Math.min(rawNixl, rawQ)) : rawNixl)
            : null;
        const q = (hasServerNixl && kvXfer !== null && rawQ !== null) ? round3(Math.max(0, rawQ - kvXfer)) : rawQ;
        const prefill = nonNegMetric(firstFiniteMetric(pt?.vllm_prefill_time_p50_ms, pt?.prefill_time_p50_ms));
        const decode = nonNegMetric(firstFiniteMetric(pt?.vllm_decode_time_p50_ms, pt?.decode_time_p50_ms));
        return {
            t,
            epp_sched_ms: epp,
            queue_ms: q,
            ...(hasServerNixl ? { kv_transfer_ms: kvXfer } : {}),
            prefill_ms: prefill,
            decode_ms: decode,
        };
    });
    const srvP90Raw = rawSrvSeries.map((pt, idx) => {
        const t = pointRelativeSeconds(pt, originMs, idx * stepSeconds);
        const epp = nonNegMetric(firstFiniteMetric(pt?.router_scheduler_latency_p90_ms, pt?.router_scheduler_e2e_p90_ms));
        const rawQ = nonNegMetric(firstFiniteMetric(pt?.vllm_queue_time_p90_ms, pt?.queue_time_p90_ms));
        const rawNixl = hasServerNixl ? nonNegMetric(pt?.nixl_transfer_latency_p90_ms) : null;
        const kvXfer = (hasServerNixl && rawNixl !== null)
            ? (rawQ !== null ? round3(Math.min(rawNixl, rawQ)) : rawNixl)
            : null;
        const q = (hasServerNixl && kvXfer !== null && rawQ !== null) ? round3(Math.max(0, rawQ - kvXfer)) : rawQ;
        const prefill = nonNegMetric(firstFiniteMetric(pt?.vllm_prefill_time_p90_ms, pt?.prefill_time_p90_ms));
        const decode = nonNegMetric(firstFiniteMetric(pt?.vllm_decode_time_p90_ms, pt?.decode_time_p90_ms));
        return {
            t,
            epp_sched_ms: epp,
            queue_ms: q,
            ...(hasServerNixl ? { kv_transfer_ms: kvXfer } : {}),
            prefill_ms: prefill,
            decode_ms: decode,
        };
    });

    const minSlice = isServerAvailable ? buildMinStageSlice(steadyWindow, 'client_fallback') : null;
    const p50Slice = isServerAvailable
        ? buildServerStageSlice('p50', { summary: srvSummary, summaryByRole: srvSummaryByRole, router: srvRouter, steadyWindow, rawRows: srvP50Raw, isDisaggregated })
        : null;
    const p90Slice = isServerAvailable
        ? buildServerStageSlice('p90', { summary: srvSummary, summaryByRole: srvSummaryByRole, router: srvRouter, steadyWindow, rawRows: srvP90Raw, isDisaggregated })
        : null;
    const meanSlice = isServerAvailable
        ? buildServerStageSlice('mean', { summary: srvSummary, summaryByRole: srvSummaryByRole, router: srvRouter, steadyWindow, rawRows: [], isDisaggregated })
        : null;
    const p99Slice = isServerAvailable
        ? buildServerTailStageSlice('p99', {
            summary: srvSummary,
            summaryByRole: srvSummaryByRole,
            router: srvRouter,
            steadyWindow,
            steadySrvPoints,
            steadyRolePoints,
            effWarmup,
            isDisaggregated,
            hasRoleSep,
        })
        : null;
    const maxSlice = isServerAvailable
        ? buildServerTailStageSlice('max', {
            summary: srvSummary,
            summaryByRole: srvSummaryByRole,
            router: srvRouter,
            steadyWindow,
            steadySrvPoints,
            steadyRolePoints,
            effWarmup,
            isDisaggregated,
            hasRoleSep,
        })
        : null;
    const hasSrvP50Series = hasAnyFiniteSeriesKey(srvP50Raw, ['queue_ms', 'kv_transfer_ms', 'prefill_ms', 'decode_ms']);
    const hasSrvP90Series = hasAnyFiniteSeriesKey(srvP90Raw, ['queue_ms', 'kv_transfer_ms', 'prefill_ms', 'decode_ms']);

    if (isServerAvailable && (p50Slice?.available || p90Slice?.available || hasSrvP50Series || hasSrvP90Series)) {
        const p50Series = normalizeStageStackSeries(srvP50Raw, serverStackKeys);
        const p90Series = normalizeStageStackSeries(srvP90Raw, serverStackKeys);
        const primarySlice = p50Slice?.available ? p50Slice : p90Slice;
        const topCaveat = primarySlice?.caveat
            ?? p50Slice?.caveat
            ?? p90Slice?.caveat
            ?? p99Slice?.caveat
            ?? maxSlice?.caveat
            ?? meanSlice?.caveat
            ?? null;
        return {
            available: true,
            source: 'server_telemetry',
            isLegacyPooledPd: false,
            hasP90: Boolean(p90Slice?.available || hasSrvP90Series),
            hasKvTransfer: hasServerNixl,
            caveat: topCaveat,
            roleBreakdown: null,
            p50Series,
            p90Series,
            slices: {
                min: minSlice?.available ? minSlice : null,
                p50: p50Slice,
                mean: meanSlice?.available ? meanSlice : null,
                p90: p90Slice,
                p99: p99Slice?.available ? p99Slice : null,
                max: maxSlice?.available ? maxSlice : null,
            },
            dominantE2eStage: dominantStageFromShares(primarySlice?.share_of_e2e_pct, E2E_STAGE_TIE_ORDER),
            dominantTtftStage: dominantStageFromShares(primarySlice?.share_of_ttft_pct, TTFT_STAGE_TIE_ORDER),
            dominantE2eStageP90: p90Slice?.available ? dominantStageFromShares(p90Slice.share_of_e2e_pct, E2E_STAGE_TIE_ORDER) : null,
            dominantTtftStageP90: p90Slice?.available ? dominantStageFromShares(p90Slice.share_of_ttft_pct, TTFT_STAGE_TIE_ORDER) : null,
            peakWindowShift: null,
        };
    }

    const clientMinSlice = buildMinStageSlice(steadyWindow, 'client_fallback');
    const clientP50Slice = buildClientStageSlice('p50', steadyWindow);
    const clientP90Slice = buildClientStageSlice('p90', steadyWindow);
    const clientMeanSlice = buildClientStageSlice('mean', steadyWindow);
    const clientP99Slice = buildClientStageSlice('p99', steadyWindow);
    const clientMaxSlice = buildClientStageSlice('max', steadyWindow);
    const buckets = Array.isArray(clientSeries?.buckets) ? clientSeries.buckets : [];
    const clientP50Raw = buckets.map((b, idx) => {
        const t = firstFiniteMetric(b?.mid_seconds, b?.time_seconds, b?.end_seconds, b?.elapsed_seconds, b?.t, idx * bucketSeconds);
        const ttft = readBucketLatencyMetric(b, 'ttft', 'p50');
        const e2e = readBucketLatencyMetric(b, 'e2e', 'p50');
        return {
            t,
            ttft_ms: ttft,
            decode_ms: ttft !== null && e2e !== null ? round3(Math.max(0, e2e - ttft)) : null,
        };
    });
    const clientP90Raw = buckets.map((b, idx) => {
        const t = firstFiniteMetric(b?.mid_seconds, b?.time_seconds, b?.end_seconds, b?.elapsed_seconds, b?.t, idx * bucketSeconds);
        const ttft = readBucketLatencyMetric(b, 'ttft', 'p90');
        const e2e = readBucketLatencyMetric(b, 'e2e', 'p90');
        return {
            t,
            ttft_ms: ttft,
            decode_ms: ttft !== null && e2e !== null ? round3(Math.max(0, e2e - ttft)) : null,
        };
    });
    const hasClientP50Series = hasAnyFiniteSeriesKey(clientP50Raw, CLIENT_STAGE_STACK_KEYS);
    const hasClientP90Series = hasAnyFiniteSeriesKey(clientP90Raw, CLIENT_STAGE_STACK_KEYS);

    if (clientP50Slice.available || clientP90Slice.available || clientMeanSlice.available || hasClientP50Series || hasClientP90Series) {
        const primaryClientSlice = clientP50Slice.available ? clientP50Slice : (clientP90Slice.available ? clientP90Slice : clientMeanSlice);
        const clientDominant = (slice) => {
            const dec = slice?.share_of_e2e_pct?.decode_time;
            const ttft = slice?.share_of_e2e_pct?.ttft_unattributed ?? 0;
            return dec !== null && dec !== undefined && dec > ttft ? 'decode_time' : null;
        };
        return {
            available: true,
            source: 'client_fallback',
            isLegacyPooledPd: false,
            hasP90: Boolean(clientP90Slice.available || hasClientP90Series),
            hasKvTransfer: false,
            caveat: null,
            roleBreakdown: null,
            p50Series: normalizeStageStackSeries(clientP50Raw, CLIENT_STAGE_STACK_KEYS),
            p90Series: normalizeStageStackSeries(clientP90Raw, CLIENT_STAGE_STACK_KEYS),
            slices: {
                min: clientMinSlice.available ? clientMinSlice : null,
                p50: clientP50Slice.available ? clientP50Slice : null,
                mean: clientMeanSlice.available ? clientMeanSlice : null,
                p90: clientP90Slice.available ? clientP90Slice : null,
                p99: clientP99Slice.available ? clientP99Slice : null,
                max: clientMaxSlice.available ? clientMaxSlice : null,
            },
            dominantE2eStage: clientDominant(primaryClientSlice),
            dominantTtftStage: null,
            dominantE2eStageP90: clientP90Slice.available ? clientDominant(clientP90Slice) : null,
            dominantTtftStageP90: null,
            peakWindowShift: null,
        };
    }

    return emptyStageLatencyView();
}

export function buildTtftDecompositionView(forensics, stageView) {
    const decomp = forensics?.ttft_decomposition;
    const stageSource = stageView?.source;
    const hasDecomp = Boolean(decomp && typeof decomp === 'object' && decomp.available !== false);
    const hasExplicitDecomp = Boolean(
        hasDecomp
        && (
            (decomp.min && decomp.min.available !== false)
            || (decomp.p50 && decomp.p50.available !== false)
            || (decomp.mean && decomp.mean.available !== false)
            || (decomp.p90 && decomp.p90.available !== false)
            || (decomp.p99 && decomp.p99.available !== false)
            || (decomp.max && decomp.max.available !== false)
            || decomp.vllm_prefill_p50_ms != null || decomp.vllm_queue_p50_ms != null || decomp.overhead_p50_ms != null
            || decomp.vllm_prefill_p90_ms != null || decomp.vllm_queue_p90_ms != null || decomp.overhead_p90_ms != null
            || decomp.vllm_prefill_p99_ms != null || decomp.vllm_queue_p99_ms != null || decomp.overhead_p99_ms != null
            || decomp.prefill_ms != null || decomp.queue_ms != null || decomp.overhead_ms != null
        ),
    );
    if (
        (decomp && typeof decomp === 'object' && decomp.available === false && stageSource !== 'pd_pooled_fallback')
        || (!hasExplicitDecomp && stageSource !== 'server_telemetry' && stageSource !== 'pd_pooled_fallback')
    ) {
        return {
            rows: [],
            source: stageSource || 'unavailable',
            caveat: null,
            hasKvTransfer: false,
            hasEppBar: false,
            hasEngineResidual: false,
            residualLabel: 'Client / Gateway / Routing',
            unsplitEngineLabel: 'Engine / API Overhead',
            barDefs: [],
        };
    }
    const useStageAsFallback = stageSource === 'pd_pooled_fallback'
        && decomp?.source !== 'pd_pooled_fallback'
        && (forensics?.stage_latency_breakdown?.source === 'pd_pooled_fallback' || !hasExplicitDecomp);
    const source = useStageAsFallback
        ? 'pd_pooled_fallback'
        : (decomp?.source || (stageSource === 'pd_pooled_fallback' && !hasExplicitDecomp ? 'pd_pooled_fallback' : 'server_telemetry'));
    const roleBreakdown = stageView?.roleBreakdown ?? forensics?.stage_latency_breakdown?.role_breakdown ?? null;

    const extractSlice = (suffix) => {
        const rawDecompSlice = (!useStageAsFallback && hasDecomp && decomp[suffix] && typeof decomp[suffix] === 'object')
            ? decomp[suffix]
            : null;
        const decompSlice = (rawDecompSlice && rawDecompSlice.available !== false) ? rawDecompSlice : null;
        const rawSlbSlice = (
            stageView?.slices?.[suffix]
            && stageView.slices[suffix].available !== false
            && (source === 'pd_pooled_fallback' || (stageView.slices[suffix].source ?? stageSource) !== 'pd_pooled_fallback')
        )
            ? stageView.slices[suffix]
            : null;
        const slbSlice = rawSlbSlice ? normalizeV6StageSlice(rawSlbSlice, suffix) : null;
        const legacyMeanClientFallback = !decompSlice && !slbSlice && source !== 'pd_pooled_fallback'
            && suffix === 'mean'
            && rawDecompSlice?.source === 'client_fallback'
            && isFiniteMetricValue(firstFiniteMetric(
                rawDecompSlice.ttft_unattributed_ms,
                rawDecompSlice.client_ttft_ms,
                rawDecompSlice.ttft_stage_ms,
            ));
        const hasFlat = !decompSlice && !slbSlice && !useStageAsFallback && hasDecomp && (
            decomp[`vllm_prefill_${suffix}_ms`] != null
            || decomp[`vllm_queue_${suffix}_ms`] != null
            || decomp[`overhead_${suffix}_ms`] != null
            || decomp[`kv_transfer_${suffix}_ms`] != null
            || decomp[`epp_scheduler_${suffix}_ms`] != null
            || decomp[`engine_overhead_${suffix}_ms`] != null
            || decomp[`ttft_unattributed_${suffix}_ms`] != null
        );
        if (!decompSlice && !slbSlice && !legacyMeanClientFallback && !hasFlat) {
            return null;
        }

        let prefill = null;
        let queue = null;
        let kvTransfer = null;
        let eppScheduling = null;
        let engineOverhead = null;
        let gatewayOverhead = null;
        let ttftUnattributed = null;
        let clientTtftMs = null;
        let serverTtftClamped = false;
        let decodeFirstStep = null;
        let prefillEngineOverhead = null;
        let decodeHandoffOverhead = null;

        if (decompSlice || slbSlice) {
            prefill = nonNegMetric(firstFiniteMetric(
                decompSlice?.prefill_ms,
                decompSlice?.vllm_prefill_ms,
                decompSlice?.prefill_time_ms,
                slbSlice?.prefill_time_ms,
            ));
            queue = nonNegMetric(firstFiniteMetric(
                decompSlice?.queue_ms,
                decompSlice?.vllm_queue_ms,
                decompSlice?.queue_time_ms,
                slbSlice?.queue_time_ms,
            ));
            kvTransfer = nonNegMetric(firstFiniteMetric(
                decompSlice?.kv_transfer_ms,
                slbSlice?.kv_transfer_ms,
            ));
            eppScheduling = nonNegMetric(firstFiniteMetric(
                decompSlice?.epp_scheduling_ms,
                decompSlice?.epp_scheduler_ms,
                slbSlice?.epp_scheduling_ms,
            ));
            const decompHasEng = Boolean(decompSlice && Object.prototype.hasOwnProperty.call(decompSlice, 'engine_overhead_ms'));
            engineOverhead = decompHasEng
                ? nonNegMetric(decompSlice.engine_overhead_ms)
                : nonNegMetric(slbSlice?.engine_overhead_ms);
            const decompHasGw = Boolean(
                decompSlice && (
                    Object.prototype.hasOwnProperty.call(decompSlice, 'gateway_overhead_ms')
                    || Object.prototype.hasOwnProperty.call(decompSlice, 'overhead_ms')
                    || Object.prototype.hasOwnProperty.call(decompSlice, 'client_gateway_epp_ms')
                ),
            );
            gatewayOverhead = decompHasGw
                ? nonNegMetric(firstFiniteMetric(decompSlice.gateway_overhead_ms, decompSlice.overhead_ms, decompSlice.client_gateway_epp_ms))
                : nonNegMetric(slbSlice?.gateway_overhead_ms);
            clientTtftMs = nonNegMetric(firstFiniteMetric(
                decompSlice?.client_ttft_ms,
                slbSlice?.client_ttft_ms,
                decomp?.[`client_ttft_${suffix}_ms`],
            ));
            ttftUnattributed = nonNegMetric(firstFiniteMetric(
                decompSlice?.ttft_unattributed_ms,
                slbSlice?.ttft_unattributed_ms,
                slbSlice?.source === 'client_fallback' ? firstFiniteMetric(slbSlice?.ttft_stage_ms, slbSlice?.client_ttft_ms) : null,
                source === 'pd_pooled_fallback' ? slbSlice?.ttft_stage_ms : null,
            ));
            serverTtftClamped = Boolean(decompSlice?.server_ttft_clamped ?? slbSlice?.server_ttft_clamped ?? false);
            decodeFirstStep = nonNegMetric(firstFiniteMetric(
                decompSlice?.decode_first_step_ms,
                slbSlice?.decode_first_step_ms,
            ));
            prefillEngineOverhead = nonNegMetric(firstFiniteMetric(
                decompSlice?.prefill_engine_overhead_ms,
                slbSlice?.prefill_engine_overhead_ms,
            ));
            decodeHandoffOverhead = nonNegMetric(firstFiniteMetric(
                decompSlice?.decode_handoff_overhead_ms,
                slbSlice?.decode_handoff_overhead_ms,
            ));
        } else if (legacyMeanClientFallback) {
            clientTtftMs = nonNegMetric(firstFiniteMetric(rawDecompSlice.client_ttft_ms, rawDecompSlice.ttft_stage_ms));
            ttftUnattributed = nonNegMetric(firstFiniteMetric(
                rawDecompSlice.ttft_unattributed_ms,
                rawDecompSlice.client_ttft_ms,
                rawDecompSlice.ttft_stage_ms,
            ));
        } else {
            prefill = nonNegMetric(decomp[`vllm_prefill_${suffix}_ms`]);
            queue = nonNegMetric(decomp[`vllm_queue_${suffix}_ms`]);
            kvTransfer = nonNegMetric(decomp[`kv_transfer_${suffix}_ms`]);
            eppScheduling = nonNegMetric(decomp[`epp_scheduler_${suffix}_ms`]);
            engineOverhead = nonNegMetric(decomp[`engine_overhead_${suffix}_ms`]);
            const flatOverhead = nonNegMetric(decomp[`overhead_${suffix}_ms`]);
            gatewayOverhead = (flatOverhead !== null && eppScheduling !== null)
                ? round3(Math.max(0, flatOverhead - eppScheduling))
                : flatOverhead;
            ttftUnattributed = nonNegMetric(decomp[`ttft_unattributed_${suffix}_ms`]);
            clientTtftMs = nonNegMetric(decomp[`client_ttft_${suffix}_ms`]);
        }

        if (source !== 'pd_pooled_fallback') {
            const hasExplicitPdSub = isFiniteMetricValue(decodeFirstStep)
                && isFiniteMetricValue(prefillEngineOverhead)
                && isFiniteMetricValue(decodeHandoffOverhead);
            if (!hasExplicitPdSub && isFiniteMetricValue(engineOverhead) && !serverTtftClamped && roleBreakdown?.prefill && roleBreakdown?.decode) {
                const pRole = roleBreakdown.prefill;
                const dRole = roleBreakdown.decode;
                const reconciled = reconcilePdEngineOverhead({
                    engineOverheadMs: engineOverhead,
                    serverTtftClamped,
                    pTtft: nonNegMetric(pRole[`ttft_${suffix}_ms`]),
                    pQueueEff: effectiveRoleQueue(pRole[`queue_${suffix}_ms`], pRole.queue_mean_ms, suffix),
                    pPrefill: nonNegMetric(pRole[`prefill_${suffix}_ms`]),
                    dTtft: nonNegMetric(dRole[`ttft_${suffix}_ms`]),
                    dQueueEff: effectiveRoleQueue(
                        firstFiniteMetric(dRole[`queue_raw_${suffix}_ms`], dRole[`queue_${suffix}_ms`]),
                        firstFiniteMetric(dRole.queue_raw_mean_ms, dRole.queue_mean_ms),
                        suffix,
                    ),
                    dPrefill: nonNegMetric(dRole[`prefill_${suffix}_ms`]),
                });
                decodeFirstStep = reconciled.decode_first_step_ms;
                prefillEngineOverhead = reconciled.prefill_engine_overhead_ms;
                decodeHandoffOverhead = reconciled.decode_handoff_overhead_ms;
            }
        }

        const hasPdSubSplit = source !== 'pd_pooled_fallback'
            && isFiniteMetricValue(decodeFirstStep)
            && isFiniteMetricValue(prefillEngineOverhead)
            && isFiniteMetricValue(decodeHandoffOverhead);
        const clientUnattributed = source !== 'pd_pooled_fallback'
            && prefill === null
            && queue === null
            && kvTransfer === null
            && engineOverhead === null
            && gatewayOverhead === null
            ? ttftUnattributed
            : null;
        let sliceNote = (typeof decompSlice?.slice_note === 'string' && decompSlice.slice_note.trim())
            ? decompSlice.slice_note.trim()
            : ((typeof slbSlice?.slice_note === 'string' && slbSlice.slice_note.trim())
                ? slbSlice.slice_note.trim()
                : null);
        if (!sliceNote && clientUnattributed !== null) {
            if (suffix === 'min') {
                sliceNote = 'Server stage histograms do not expose single-request minimums; showing exact client Min TTFT.';
            } else if (suffix === 'mean') {
                sliceNote = 'Server mean stage histograms were not recorded; showing exact client Mean TTFT.';
            } else if (suffix === 'max') {
                sliceNote = 'Server histograms do not report per-request max; showing exact client Max TTFT.';
            }
        }

        return {
            prefill_ms: prefill,
            decode_first_step_ms: hasPdSubSplit ? decodeFirstStep : null,
            queue_ms: queue,
            kv_transfer_ms: kvTransfer,
            prefill_engine_overhead_ms: hasPdSubSplit ? prefillEngineOverhead : null,
            decode_handoff_overhead_ms: hasPdSubSplit ? decodeHandoffOverhead : null,
            engine_overhead_unsplit_ms: hasPdSubSplit ? null : (source === 'pd_pooled_fallback' ? null : engineOverhead),
            epp_scheduling_ms: eppScheduling,
            engine_overhead_ms: engineOverhead,
            gateway_overhead_ms: gatewayOverhead,
            ttft_unattributed_ms: ttftUnattributed,
            client_unattributed_ms: clientUnattributed,
            client_ttft_ms: clientTtftMs,
            has_pd_sub_split: hasPdSubSplit,
            server_ttft_clamped: serverTtftClamped,
            caveat: decompSlice?.caveat ?? slbSlice?.caveat ?? null,
            slice_note: sliceNote,
        };
    };

    const rows = [];
    for (const [suffix, label] of [['min', 'Min'], ['p50', 'P50'], ['mean', 'Mean'], ['p90', 'P90'], ['p99', 'P99'], ['max', 'Max']]) {
        const s = extractSlice(suffix);
        if (!s) continue;
        if (source === 'pd_pooled_fallback') {
            const hasVal = (s.ttft_unattributed_ms ?? 0) > 0 || (s.kv_transfer_ms ?? 0) > 0 || (s.epp_scheduling_ms ?? 0) > 0;
            if (!hasVal) continue;
            rows.push({
                key: suffix,
                stat: label,
                ...s,
            });
        } else {
            const hasResidual = isFiniteMetricValue(s.epp_scheduling_ms)
                || isFiniteMetricValue(s.engine_overhead_ms)
                || isFiniteMetricValue(s.gateway_overhead_ms);
            const residual = hasResidual
                ? round3((s.epp_scheduling_ms ?? 0) + (s.engine_overhead_ms ?? 0) + (s.gateway_overhead_ms ?? 0))
                : null;
            const hasVal = (s.prefill_ms ?? 0) > 0
                || (s.queue_ms ?? 0) > 0
                || (s.kv_transfer_ms ?? 0) > 0
                || (residual ?? 0) > 0
                || (s.client_unattributed_ms ?? 0) > 0;
            if (!hasVal) continue;
            rows.push({
                key: suffix,
                stat: label,
                ...s,
                residual_ms: residual,
            });
        }
    }

    if (!rows.length && !useStageAsFallback && hasDecomp && (decomp.prefill_ms != null || decomp.queue_ms != null || decomp.overhead_ms != null)) {
        const gw = nonNegMetric(decomp.overhead_ms);
        rows.push({
            key: 'ttft',
            stat: 'TTFT',
            prefill_ms: nonNegMetric(decomp.prefill_ms),
            decode_first_step_ms: null,
            queue_ms: nonNegMetric(decomp.queue_ms),
            kv_transfer_ms: nonNegMetric(decomp.kv_transfer_ms),
            prefill_engine_overhead_ms: null,
            decode_handoff_overhead_ms: null,
            engine_overhead_unsplit_ms: null,
            epp_scheduling_ms: null,
            engine_overhead_ms: null,
            gateway_overhead_ms: gw,
            residual_ms: gw,
            ttft_unattributed_ms: null,
            client_unattributed_ms: null,
            client_ttft_ms: null,
            has_pd_sub_split: false,
            server_ttft_clamped: false,
            caveat: null,
            slice_note: null,
        });
    }

    const hasKvTransfer = rows.some((r) => isFiniteMetricValue(r.kv_transfer_ms) && r.kv_transfer_ms > 0);
    const hasEppBar = rows.some((r) => isFiniteMetricValue(r.epp_scheduling_ms) && r.epp_scheduling_ms > 0);
    const hasEngineResidual = rows.some((r) => isFiniteMetricValue(r.engine_overhead_ms) && r.engine_overhead_ms > 0);
    const residualLabel = hasEngineResidual ? 'Server & Gateway Residual' : 'Client / Gateway / Routing';
    const unsplitEngineLabel = rows.some((r) => !r.has_pd_sub_split && r.server_ttft_clamped)
        ? 'Server TTFT Residual'
        : 'Engine / API Overhead';

    const candidateBarDefs = source === 'pd_pooled_fallback'
        ? [
            { key: 'ttft_unattributed_ms', label: 'P/D Engine TTFT (Queue + Prefill)', colorIndex: 0, opacity: TTFT_BAR_OPACITY_BY_KEY.ttft_unattributed_ms },
            { key: 'kv_transfer_ms', label: 'KV Transfer (NIXL)', colorIndex: 4, opacity: TTFT_BAR_OPACITY_BY_KEY.kv_transfer_ms },
            { key: 'epp_scheduling_ms', label: 'EPP Scheduling', colorIndex: 3, opacity: TTFT_BAR_OPACITY_BY_KEY.epp_scheduling_ms },
        ]
        : [
            { key: 'prefill_ms', label: 'Prefill Time', colorIndex: 0, opacity: TTFT_BAR_OPACITY_BY_KEY.prefill_ms },
            { key: 'decode_first_step_ms', label: 'Decode First-Step Compute', colorIndex: 0, opacity: TTFT_BAR_OPACITY_BY_KEY.decode_first_step_ms },
            { key: 'queue_ms', label: 'Queue Time', colorIndex: 2, opacity: TTFT_BAR_OPACITY_BY_KEY.queue_ms },
            { key: 'kv_transfer_ms', label: 'KV Transfer (NIXL)', colorIndex: 4, opacity: TTFT_BAR_OPACITY_BY_KEY.kv_transfer_ms },
            { key: 'prefill_engine_overhead_ms', label: 'Prefill Engine / API Overhead', colorIndex: 1, opacity: TTFT_BAR_OPACITY_BY_KEY.prefill_engine_overhead_ms },
            { key: 'decode_handoff_overhead_ms', label: 'Decode Handoff / SSE Overhead', colorIndex: 1, opacity: TTFT_BAR_OPACITY_BY_KEY.decode_handoff_overhead_ms },
            { key: 'engine_overhead_unsplit_ms', label: unsplitEngineLabel, colorIndex: 1, opacity: TTFT_BAR_OPACITY_BY_KEY.engine_overhead_unsplit_ms },
            { key: 'epp_scheduling_ms', label: 'EPP Scheduling', colorIndex: 3, opacity: TTFT_BAR_OPACITY_BY_KEY.epp_scheduling_ms },
            { key: 'gateway_overhead_ms', label: 'Client / Gateway / Network', colorIndex: 3, opacity: TTFT_BAR_OPACITY_BY_KEY.gateway_overhead_ms },
            { key: 'client_unattributed_ms', label: 'Unattributed TTFT', colorIndex: 2, opacity: TTFT_BAR_OPACITY_BY_KEY.client_unattributed_ms },
        ];
    const nonZeroBarDefs = candidateBarDefs.filter((def) =>
        rows.some((r) => isFiniteMetricValue(r[def.key]) && r[def.key] > 0),
    );
    const barDefs = nonZeroBarDefs.length
        ? nonZeroBarDefs
        : candidateBarDefs.filter((def) => rows.some((r) => isFiniteMetricValue(r[def.key])));

    const caveat = (useStageAsFallback ? stageView?.caveat : (decomp?.caveat ?? stageView?.caveat))
        ?? rows.find((r) => r.caveat)?.caveat
        ?? null;

    return {
        rows,
        source,
        caveat,
        hasKvTransfer,
        hasEppBar,
        hasEngineResidual,
        residualLabel,
        unsplitEngineLabel,
        barDefs,
    };
}

/**
 * Filter a list of model access tokens to active, non-expired items with valid IDs.
 */
export function filterActiveModelTokens(tokens, now = Date.now()) {
    if (!Array.isArray(tokens)) return [];
    return tokens.filter((token) => {
        if (!token || typeof token !== 'object') return false;
        const id = typeof token.id === 'string' ? token.id.trim() : token.id;
        if (!id) return false;
        if (String(token.status || '').trim().toLowerCase() !== 'active') return false;
        const expiresRaw = token.expiresAt ?? token.expires_at;
        if (expiresRaw !== undefined && expiresRaw !== null && expiresRaw !== '') {
            const expiresMs = Date.parse(expiresRaw);
            if (!Number.isFinite(expiresMs) || expiresMs <= now) return false;
        }
        return true;
    });
}

/**
 * Format a saved model access token as a non-secret select option label.
 */
export function formatModelTokenOptionLabel(token) {
    const name = String(token?.name || token?.id || 'API key');
    const rawHint = String(token?.tokenHint ?? token?.token_hint ?? '').trim();
    if (!rawHint) return name;
    const hint = rawHint.startsWith('lens-mk') ? rawHint.replace(/^lens-mk-?/, '') : rawHint;
    return `${name} (lens-mk-…${hint})`;
}
