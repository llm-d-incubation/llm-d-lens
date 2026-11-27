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

import React, { useMemo, useState } from 'react';
import {
    Activity,
    AlertTriangle,
    ArrowUpRight,
    Clock3,
    Database,
    Gauge,
    Layers,
    Zap,
} from 'lucide-react';
import {
    Area,
    Bar,
    BarChart,
    CartesianGrid,
    ComposedChart,
    Line,
    LineChart,
    ReferenceArea,
    ReferenceLine,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { cn } from '../../../../shared/lib/cn';
import {
    Badge,
    CHART_SERIES,
    ChartContainer,
    ChartLegend,
    ChartTooltip,
    ChartTooltipRow,
    EmptyState,
    HelpTooltip,
    Panel,
    SectionLabel,
    StatCard,
    ToggleGroup,
} from '../../../../shared/ui';
import {
    displayName,
    formatCount,
    formatDecimal,
    formatDurationSeconds,
    formatEnergyWh,
    formatMilliseconds,
    formatNumber,
    formatWatts,
    numberAt,
    verdictBadgeProps,
} from '../../model/presentation';

const AXIS_TICK = { fill: 'var(--text-muted)', fontSize: 10 };
const BAR_TOOLTIP_CURSOR = { fill: 'var(--chart-cursor-fill, rgba(148, 163, 184, 0.10))' };
const LINE_TOOLTIP_CURSOR = { stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' };

function positiveFiniteConcurrency(value) {
    if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'object') {
        return null;
    }
    if (typeof value === 'string' && value.trim() === '') return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
}

const CONCURRENCY_OPTION_KEYS = ['concurrency', 'max_concurrency', 'session_concurrency'];

/**
 * Extracts user-configured request concurrency for a simulation run, returning null
 * for historical trace-replayer runs or runs without an explicit positive concurrency limit.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function resolveConfiguredConcurrency(task, runManifest) {
    const backendName = String(
        task?.simulation?.backend || task?.backend || runManifest?.backend || task?.result?.backend || '',
    ).trim().toLowerCase();
    // Defensive historical compatibility: legacy trace-replayer rows never used concurrency slots.
    if (backendName === 'trace-replayer' || backendName === 'trace_replayer') {
        return null;
    }

    const optionCandidates = [
        task?.simulation?.backend_options,
        task?.backend_options,
        runManifest?.replay?.backend_options,
        runManifest?.backend_options,
        task?.config_snapshot?.backend_options,
    ];
    for (const options of optionCandidates) {
        if (!options || typeof options !== 'object') continue;
        for (const key of CONCURRENCY_OPTION_KEYS) {
            const parsed = positiveFiniteConcurrency(options[key]);
            if (parsed !== null) return parsed;
        }
    }

    const directCandidates = [
        task?.config_snapshot?.concurrency,
        task?.config_snapshot?.max_concurrency,
        task?.config_snapshot?.session_concurrency,
        task?.concurrency,
    ];
    for (const value of directCandidates) {
        const parsed = positiveFiniteConcurrency(value);
        if (parsed !== null) return parsed;
    }

    return null;
}

function nonNegativeFinite(value) {
    if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'object') {
        return null;
    }
    if (typeof value === 'string' && value.trim() === '') return null;
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 0) return null;
    return numeric === 0 ? 0 : numeric;
}

function secondsToMs(value) {
    const seconds = nonNegativeFinite(value);
    if (seconds === null) return null;
    return seconds === 0 ? 0 : Number((seconds * 1000).toPrecision(12));
}

function firstNonNull(...values) {
    for (const value of values) {
        if (value !== null && value !== undefined) return value;
    }
    return null;
}

/**
 * Extracts user-configured SLO targets (latency thresholds in ms and error rate in %)
 * without leaking the backend verdict's default 5% error-rate threshold when unset.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function resolveConfiguredSlos(task, telemetry) {
    const runManifest = telemetry?.run_manifest;
    const manifestSlo = runManifest?.slo && typeof runManifest.slo === 'object' && !Array.isArray(runManifest.slo)
        ? runManifest.slo
        : null;
    const optionCandidates = [
        task?.simulation?.backend_options,
        task?.backend_options,
        runManifest?.replay?.backend_options,
        runManifest?.backend_options,
        task?.config_snapshot?.backend_options,
    ].filter((options) => Boolean(options && typeof options === 'object' && !Array.isArray(options)));
    const verdictSlo = telemetry?.forensics?.verdict?.slo
        && typeof telemetry.forensics.verdict.slo === 'object'
        && !Array.isArray(telemetry.forensics.verdict.slo)
        ? telemetry.forensics.verdict.slo
        : null;

    const firstFromOptions = (key) => {
        for (const options of optionCandidates) {
            const parsed = nonNegativeFinite(options[key]);
            if (parsed !== null) return parsed;
        }
        return null;
    };

    const ttftSloMs = firstNonNull(
        secondsToMs(manifestSlo?.ttft_s),
        nonNegativeFinite(manifestSlo?.ttft_ms),
        secondsToMs(firstFromOptions('ttft_slo')),
        secondsToMs(verdictSlo?.ttft_s),
        nonNegativeFinite(verdictSlo?.ttft_ms),
    );
    const tpotSloMs = firstNonNull(
        secondsToMs(manifestSlo?.tpot_s),
        nonNegativeFinite(manifestSlo?.tpot_ms),
        secondsToMs(firstFromOptions('tpot_slo')),
        nonNegativeFinite(verdictSlo?.tpot_ms),
        secondsToMs(verdictSlo?.tpot_s),
    );
    const e2eSloMs = firstNonNull(
        secondsToMs(manifestSlo?.e2e_s),
        nonNegativeFinite(manifestSlo?.e2e_ms),
        secondsToMs(firstFromOptions('e2e_slo')),
        secondsToMs(verdictSlo?.e2e_s),
        nonNegativeFinite(verdictSlo?.e2e_ms),
    );
    // Backend forensics defaults verdict.slo.error_rate_pct to 5.0 even when unset;
    // only fall back to verdict.slo when neither run_manifest.slo nor backend_options exists.
    const hasExplicitSloSource = manifestSlo !== null || optionCandidates.length > 0;
    const errorRateSloPct = firstNonNull(
        nonNegativeFinite(manifestSlo?.error_rate_pct),
        firstFromOptions('error_rate_slo'),
        hasExplicitSloSource ? null : nonNegativeFinite(verdictSlo?.error_rate_pct),
    );

    const targets = [];
    if (ttftSloMs !== null) {
        targets.push({ key: 'ttft', label: 'TTFT', formatted: formatMilliseconds(ttftSloMs) });
    }
    if (tpotSloMs !== null) {
        targets.push({ key: 'tpot', label: 'TPOT', formatted: formatMilliseconds(tpotSloMs) });
    }
    if (e2eSloMs !== null) {
        targets.push({
            key: 'e2e',
            label: 'E2E',
            formatted: e2eSloMs >= 1000 ? formatNumber(e2eSloMs / 1000, ' s') : formatMilliseconds(e2eSloMs),
        });
    }
    if (errorRateSloPct !== null) {
        targets.push({ key: 'error_rate', label: 'Error Rate', formatted: formatNumber(errorRateSloPct, '%') });
    }

    return {
        ttftSloMs,
        tpotSloMs,
        e2eSloMs,
        errorRateSloPct,
        targets,
    };
}

function firstFinite(...values) {
    for (const value of values) {
        if (value !== null && value !== undefined && value !== '' && typeof value !== 'boolean' && typeof value !== 'object') {
            if (typeof value === 'string' && value.trim() === '') continue;
            const numeric = Number(value);
            if (Number.isFinite(numeric)) return numeric;
        }
    }
    return null;
}

function windowMetrics(source = {}, fallback = {}) {
    return {
        totalRequests: numberAt(source, ['total_requests', 'request_count', 'requests_total'])
            ?? numberAt(fallback, ['total_requests', 'request_count', 'requests_total']),
        successfulRequests: numberAt(source, ['successful_requests', 'requests_success', 'success_count'])
            ?? numberAt(fallback, ['successful_requests', 'requests_success', 'success_count']),
        goodRequests: numberAt(source, ['good_requests', 'slo_compliant_requests'])
            ?? numberAt(fallback, ['good_requests', 'slo_compliant_requests']),
        failedRequests: numberAt(source, ['failed_requests', 'requests_failed', 'error_count'])
            ?? numberAt(fallback, ['failed_requests', 'requests_failed', 'error_count']),
        errorRatePct: numberAt(source, ['error_rate_pct', 'error_rate_percent'])
            ?? numberAt(fallback, ['error_rate_pct', 'error_rate_percent']),
        sloAttainmentPct: numberAt(source, ['slo_attainment_pct', 'attainment_pct'])
            ?? numberAt(fallback, ['slo_attainment_pct', 'attainment_pct']),
        rps: numberAt(source, ['throughput_rps', 'request_throughput.avg', 'requests_per_second'])
            ?? numberAt(fallback, ['throughput_rps', 'request_throughput.avg', 'requests_per_second']),
        throughputPerHr: numberAt(source, ['throughput_per_hr'])
            ?? numberAt(fallback, ['throughput_per_hr']),
        goodputRps: numberAt(source, ['goodput_rps'])
            ?? numberAt(fallback, ['goodput_rps']),
        goodputPerHr: numberAt(source, ['goodput_per_hr'])
            ?? numberAt(fallback, ['goodput_per_hr']),
        outputTps: numberAt(source, ['output_tps', 'output_tok_per_s', 'output_token_throughput.avg', 'throughput_tps'])
            ?? numberAt(fallback, ['output_tps', 'output_tok_per_s', 'output_token_throughput.avg', 'throughput_tps']),
        totalOutputTokens: numberAt(source, ['total_output_tokens', 'output_tokens_total'])
            ?? numberAt(fallback, ['total_output_tokens', 'output_tokens_total']),
        durationSeconds: numberAt(source, ['duration_seconds'])
            ?? numberAt(fallback, ['duration_seconds']),
        ttftP50: numberAt(source, ['ttft_ms.p50', 'ttft.p50_ms', 'ttft.p50', 'ttft_p50_ms'])
            ?? numberAt(fallback, ['ttft_ms.p50', 'ttft.p50_ms', 'ttft.p50', 'ttft_p50_ms']),
        ttftP90: numberAt(source, ['ttft_ms.p90', 'ttft.p90_ms', 'ttft.p90', 'ttft_p90_ms'])
            ?? numberAt(fallback, ['ttft_ms.p90', 'ttft.p90_ms', 'ttft.p90', 'ttft_p90_ms']),
        ttftP99: numberAt(source, ['ttft_ms.p99', 'ttft.p99_ms', 'ttft.p99', 'ttft_p99_ms'])
            ?? numberAt(fallback, ['ttft_ms.p99', 'ttft.p99_ms', 'ttft.p99', 'ttft_p99_ms']),
        tpotP50: numberAt(source, ['tpot_ms.p50', 'tpot.p50_ms', 'tpot.p50', 'tpot_p50_ms'])
            ?? numberAt(fallback, ['tpot_ms.p50', 'tpot.p50_ms', 'tpot.p50', 'tpot_p50_ms']),
        tpotP90: numberAt(source, ['tpot_ms.p90', 'tpot.p90_ms', 'tpot.p90', 'tpot_p90_ms'])
            ?? numberAt(fallback, ['tpot_ms.p90', 'tpot.p90_ms', 'tpot.p90', 'tpot_p90_ms']),
        tpotP99: numberAt(source, ['tpot_ms.p99', 'tpot.p99_ms', 'tpot.p99', 'tpot_p99_ms'])
            ?? numberAt(fallback, ['tpot_ms.p99', 'tpot.p99_ms', 'tpot.p99', 'tpot_p99_ms']),
        tpotMeasuredRatio: numberAt(source, ['tpot.measured_ratio', 'tpot_measured_ratio'])
            ?? numberAt(fallback, ['tpot.measured_ratio', 'tpot_measured_ratio']),
    };
}

function resolveOriginMs(clientSeries, serverTelemetry, runManifest) {
    if (Number.isFinite(Number(clientSeries?.origin_epoch)) && Number(clientSeries.origin_epoch) > 1_000_000_000) {
        return Number(clientSeries.origin_epoch) * 1000;
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

function pointRelativeSeconds(point, originMs, fallbackIdx) {
    const explicit = firstFinite(point?.elapsed_seconds, point?.time_seconds, point?.t);
    if (explicit !== null) return explicit;
    if (typeof point?.timestamp === 'string' && point.timestamp && originMs !== null) {
        const parsed = Date.parse(point.timestamp);
        if (Number.isFinite(parsed)) return Math.max(0, (parsed - originMs) / 1000);
    }
    return fallbackIdx;
}

const LATENCY_STAT_DESCRIPTORS = [
    { key: 'min', label: 'Min', color: CHART_SERIES[0], strokeWidth: 1.25, strokeDasharray: '2 3', strokeOpacity: 0.65 },
    { key: 'p50', label: 'P50', color: CHART_SERIES[0], strokeWidth: 2, strokeDasharray: undefined, strokeOpacity: 1 },
    { key: 'mean', label: 'Mean', color: CHART_SERIES[3], strokeWidth: 1.75, strokeDasharray: '6 2', strokeOpacity: 0.95 },
    { key: 'p90', label: 'P90', color: CHART_SERIES[1], strokeWidth: 1.5, strokeDasharray: '4 3', strokeOpacity: 1 },
    { key: 'p99', label: 'P99', color: CHART_SERIES[4], strokeWidth: 1.75, strokeDasharray: undefined, strokeOpacity: 0.95 },
    { key: 'max', label: 'Max', color: CHART_SERIES[4], strokeWidth: 1.25, strokeDasharray: '2 2', strokeOpacity: 0.65 },
];

const LATENCY_STAT_OPACITY_BY_KEY = Object.fromEntries(
    LATENCY_STAT_DESCRIPTORS.map((stat) => [stat.key, stat.strokeOpacity]),
);

const DEFAULT_LATENCY_STATS = ['p50', 'mean', 'p90'];

const STAT_KEY_ALIASES = {
    min: ['min'],
    p50: ['p50'],
    mean: ['mean', 'avg'],
    p90: ['p90'],
    p99: ['p99'],
    max: ['max'],
};

const COLD_COMPARISON_METRICS = [
    { key: 'ttft', label: 'TTFT', unit: 'ms' },
    { key: 'tpot', label: 'TPOT', unit: 'ms/tok' },
    { key: 'e2e', label: 'E2E', unit: 'ms' },
];

function extractWindowLatencyStat(win, mode, statKey) {
    if (!win || typeof win !== 'object') return null;
    const aliases = STAT_KEY_ALIASES[statKey] || [statKey];
    const nestedMs = win[`${mode}_ms`];
    const nested = win[mode];
    const candidates = [];
    for (const alias of aliases) {
        const capAlias = alias.charAt(0).toUpperCase() + alias.slice(1);
        candidates.push(
            nestedMs?.[alias],
            nested?.[`${alias}_ms`],
            nested?.[alias],
            win[`${mode}_${alias}_ms`],
            win[`${mode}_${alias}`],
            win[`${mode}${capAlias}`],
            win[`${mode}${capAlias}Ms`],
        );
    }
    return firstFinite(...candidates);
}

/**
 * Builds grouped bar rows for Chart 4 ("Warmup vs Steady-State Latency")
 * across all 6 canonical stats in monotonic order (Min, P50, Mean, P90, P99, Max).
 */
// eslint-disable-next-line react-refresh/only-export-components
export function buildColdComparisonBars(coldWindow, steadyWindow, metricKey = 'ttft') {
    const meta = COLD_COMPARISON_METRICS.find((m) => m.key === metricKey) || COLD_COMPARISON_METRICS[0];
    const rows = [];
    for (const stat of LATENCY_STAT_DESCRIPTORS) {
        const cold = extractWindowLatencyStat(coldWindow, meta.key, stat.key);
        const steady = extractWindowLatencyStat(steadyWindow, meta.key, stat.key);
        if (cold !== null || steady !== null) {
            rows.push({
                metric: `${meta.label} ${stat.label}`,
                stat: stat.label,
                cold,
                steady,
                unit: meta.unit,
            });
        }
    }
    return rows;
}

const LEGACY_FLAT_LATENCY_KEYS = {
    ttft: {
        p50: ['average_ttft_ms'],
        mean: ['average_ttft_ms'],
        p90: ['p95_ttft_ms'],
    },
    tpot: {
        p50: ['average_tpot_ms'],
        mean: ['average_tpot_ms'],
        p90: ['p95_tpot_ms'],
    },
    e2e: {
        min: ['latency_min_ms', 'min_latency_ms'],
        p50: ['latency_p50_ms', 'average_latency_ms'],
        mean: ['latency_mean_ms', 'average_latency_ms'],
        p90: ['latency_p90_ms', 'p95_latency_ms'],
        p99: ['latency_p99_ms'],
        max: ['latency_max_ms', 'max_latency_ms'],
    },
};

function extractBucketModeStats(bucket, mode) {
    const nested = bucket?.[mode];
    const nestedMs = bucket?.[`${mode}_ms`];
    const extra = LEGACY_FLAT_LATENCY_KEYS[mode] || EMPTY_OBJ;
    const out = {};
    for (const stat of LATENCY_STAT_DESCRIPTORS) {
        const aliases = STAT_KEY_ALIASES[stat.key] || [stat.key];
        const candidates = [];
        for (const alias of aliases) {
            candidates.push(bucket?.[`${mode}_${alias}_ms`], bucket?.[`${mode}_${alias}`]);
        }
        for (const alias of aliases) {
            candidates.push(nested?.[`${alias}_ms`], nested?.[alias]);
        }
        for (const alias of aliases) {
            candidates.push(nestedMs?.[alias]);
        }
        if (extra[stat.key]) {
            for (const flatKey of extra[stat.key]) {
                candidates.push(bucket?.[flatKey]);
            }
        }
        out[`${mode}_${stat.key}`] = firstFinite(...candidates);
    }
    const p50 = out[`${mode}_p50`];
    const p90 = out[`${mode}_p90`];
    out[`${mode}_band`] = p50 !== null && p90 !== null ? Math.max(0, p90 - p50) : null;
    return out;
}

/**
 * Resolves the effective visible latency stat descriptors in canonical order,
 * excluding unavailable stats and falling back to all available stats when
 * none of the currently selected stats have finite data in the active mode.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function resolveVisibleLatencyStats(selectedStats, availableStats) {
    const selectedSet = new Set(Array.isArray(selectedStats) ? selectedStats : []);
    const available = Array.isArray(availableStats) ? availableStats : [];
    const selectedAvailable = available.filter((stat) => selectedSet.has(stat.key));
    return selectedAvailable.length > 0 ? selectedAvailable : available;
}

/**
 * Toggles a single latency stat key while guarding against deselecting the
 * last active available stat.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function toggleLatencyStatSelection(prevSelected, statKey, availableStatKeys) {
    const availableSet = availableStatKeys instanceof Set
        ? availableStatKeys
        : new Set(Array.isArray(availableStatKeys) ? availableStatKeys : []);
    if (!availableSet.has(statKey)) return prevSelected;

    const prevList = Array.isArray(prevSelected) ? prevSelected : [];
    const prevAvailable = prevList.filter((key) => availableSet.has(key));
    const activeKeys = prevAvailable.length > 0
        ? prevAvailable
        : LATENCY_STAT_DESCRIPTORS.map((stat) => stat.key).filter((key) => availableSet.has(key));

    if (activeKeys.includes(statKey)) {
        if (activeKeys.length <= 1) return prevList;
        return activeKeys.filter((key) => key !== statKey);
    }
    return LATENCY_STAT_DESCRIPTORS
        .map((stat) => stat.key)
        .filter((key) => key === statKey || activeKeys.includes(key));
}

/**
 * Toggles between selecting all available stats and resetting to the default
 * ['p50', 'mean', 'p90'] set.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function toggleAllLatencyStats(allAvailableActive, availableStats) {
    if (allAvailableActive) return DEFAULT_LATENCY_STATS;
    return (Array.isArray(availableStats) ? availableStats : []).map((stat) =>
        typeof stat === 'string' ? stat : stat.key,
    );
}

/**
 * Computes the Overview Latency-over-Time Y-axis ceiling from only the currently
 * visible stats (preserving SLO ReferenceLine headroom when configured).
 */
// eslint-disable-next-line react-refresh/only-export-components
export function computeOverviewLatencyYMax({
    timeSeries,
    latencyMode,
    visibleStats,
    latencySloMs = null,
}) {
    const statsToCheck = Array.isArray(visibleStats) ? visibleStats : [];
    const maxObserved = (Array.isArray(timeSeries) ? timeSeries : []).reduce((maxVal, pt) => {
        let next = maxVal;
        for (const stat of statsToCheck) {
            const key = typeof stat === 'string' ? stat : stat?.key;
            if (!key) continue;
            const val = pt?.[`${latencyMode}_${key}`];
            if (Number.isFinite(val) && val >= 0) next = Math.max(next, val);
        }
        return next;
    }, 0);
    if (latencySloMs !== null) {
        return Math.max(1, Math.ceil(Math.max(maxObserved, latencySloMs) * 1.12));
    }
    return null;
}

/**
 * Normalizes client time-series buckets and aligns nearest server telemetry samples.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function buildTimeSeriesPoints(clientSeries, serverTelemetry, runManifest) {
    const legacySeries = clientSeries?.series && !Array.isArray(clientSeries.series) ? clientSeries.series : null;
    let raw = Array.isArray(clientSeries?.buckets)
        ? clientSeries.buckets
        : Array.isArray(clientSeries?.series)
            ? clientSeries.series
            : Array.isArray(clientSeries?.timeline)
                ? clientSeries.timeline
                : [];
    if (!raw.length && legacySeries) {
        const byTime = new Map();
        const mergeList = (items, mapper) => {
            if (!Array.isArray(items)) return;
            for (const item of items) {
                const sec = firstFinite(item.time_seconds, item.end_seconds, item.elapsed_seconds, item.t);
                if (sec === null) continue;
                const entry = byTime.get(sec) || { time_seconds: sec };
                Object.assign(entry, mapper(item));
                byTime.set(sec, entry);
            }
        };
        const mapLegacyLatency = (prefix) => (p) => ({
            [`${prefix}_min_ms`]: firstFinite(p.min_latency_ms),
            [`${prefix}_p50_ms`]: firstFinite(p.p50_latency_ms, p.average_latency_ms),
            [`${prefix}_mean_ms`]: firstFinite(p.mean_latency_ms, p.average_latency_ms),
            [`${prefix}_p90_ms`]: firstFinite(p.p90_latency_ms, p.p95_latency_ms),
            [`${prefix}_p99_ms`]: firstFinite(p.p99_latency_ms),
            [`${prefix}_max_ms`]: firstFinite(p.max_latency_ms),
        });
        mergeList(legacySeries.throughput, (p) => ({
            arrived_rps: firstFinite(p.request_arrival_rps),
            completed_rps: firstFinite(p.request_completion_rps),
            output_tps: firstFinite(p.token_throughput_tps),
        }));
        mergeList(legacySeries.goodput, (p) => ({
            goodput_rps: firstFinite(p.goodput_rps),
            completed_rps: firstFinite(p.request_throughput_rps),
        }));
        mergeList(legacySeries.ttft, mapLegacyLatency('ttft'));
        mergeList(legacySeries.tpot, mapLegacyLatency('tpot'));
        mergeList(legacySeries.latency, mapLegacyLatency('e2e'));
        raw = [...byTime.values()].sort((a, b) => a.time_seconds - b.time_seconds);
    }
    const serverPoints = Array.isArray(serverTelemetry?.series) ? serverTelemetry.series : [];
    const originMs = resolveOriginMs(clientSeries, serverTelemetry, runManifest);
    const indexedServer = serverPoints.map((point, idx) => ({
        t: pointRelativeSeconds(point, originMs, idx),
        point,
    }));
    const bucketSeconds = firstFinite(clientSeries?.bucket_seconds);
    const maxMatchDelta = Math.max(5, bucketSeconds || 5);

    const findNearestServer = (targetT) => {
        if (!indexedServer.length) return EMPTY_OBJ;
        let best = null;
        let bestDelta = Infinity;
        for (const entry of indexedServer) {
            const delta = Math.abs(entry.t - targetT);
            if (delta <= maxMatchDelta && delta < bestDelta) {
                bestDelta = delta;
                best = entry.point;
            }
        }
        return best || EMPTY_OBJ;
    };

    return raw.map((bucket) => {
        const t = firstFinite(bucket.time_seconds, bucket.mid_seconds, bucket.end_seconds, bucket.elapsed_seconds, bucket.t, 0);
        const server = findNearestServer(t);
        const dur = firstFinite(
            bucket.end_seconds != null && bucket.start_seconds != null ? bucket.end_seconds - bucket.start_seconds : null,
            bucketSeconds,
        );
        const derivedArrivedRps = bucket.arrived_requests != null && dur && dur > 0
            ? Number(bucket.arrived_requests) / dur
            : null;

        return {
            t,
            arrived_rps: firstFinite(bucket.arrived_rps, bucket.request_arrival_rps, bucket.target_arrival_rps, derivedArrivedRps),
            completed_rps: firstFinite(bucket.completed_rps, bucket.throughput_rps, bucket.request_completion_rps, bucket.request_throughput_rps),
            goodput_rps: firstFinite(bucket.goodput_rps),
            output_tps: firstFinite(bucket.output_tps, bucket.output_tok_per_s, bucket.token_throughput_tps, bucket.output_token_rate_tps),
            ...extractBucketModeStats(bucket, 'ttft'),
            ...extractBucketModeStats(bucket, 'tpot'),
            ...extractBucketModeStats(bucket, 'e2e'),
            client_inflight: firstFinite(bucket.client_inflight, bucket.in_flight_concurrency, bucket.in_flight, bucket.concurrency),
            server_running: firstFinite(bucket.server_running, server.running_requests, server.vllm_num_requests_running),
        };
    });
}

function maxFinite(values) {
    let best = null;
    for (const raw of values) {
        const numeric = firstFinite(raw);
        if (numeric !== null && (best === null || numeric > best)) {
            best = numeric;
        }
    }
    return best;
}

function resolvePeakKvCachePct(serverTelemetry, forensics, runManifest) {
    const topologyMode = String(
        serverTelemetry?.topology_mode || runManifest?.target?.topology_mode || '',
    ).trim().toLowerCase();
    const perPod = Array.isArray(serverTelemetry?.per_pod) ? serverTelemetry.per_pod : [];
    const summaryByRole = serverTelemetry?.summary_by_role && typeof serverTelemetry.summary_by_role === 'object'
        ? serverTelemetry.summary_by_role
        : {};
    const isDisaggregated = topologyMode === 'disaggregated'
        || perPod.some((pod) => ['prefill', 'decode'].includes(String(pod?.role || '').toLowerCase()))
        || Boolean(summaryByRole.prefill || summaryByRole.decode);

    if (isDisaggregated) {
        const perPodMax = maxFinite(perPod.map((pod) => firstFinite(
            pod?.kv_cache_usage_percent?.max,
            pod?.kv_cache_usage_percent,
            pod?.gpu_cache_usage_percent?.max,
            pod?.gpu_cache_usage_percent,
            pod?.kv_cache_peak_pct,
        )));
        if (perPodMax !== null) return perPodMax;

        const roleSummaryMax = maxFinite(Object.values(summaryByRole).map((roleStats) => firstFinite(
            roleStats?.kv_cache_usage_percent?.max,
            roleStats?.kv_cache_usage_percent,
            roleStats?.gpu_cache_usage_percent?.max,
            roleStats?.gpu_cache_usage_percent,
            roleStats?.kv_cache_peak_pct,
        )));
        if (roleSummaryMax !== null) return roleSummaryMax;

        const roleSeries = Array.isArray(serverTelemetry?.role_series) ? serverTelemetry.role_series : [];
        const roleSeriesMax = maxFinite(roleSeries.flatMap((pt) => [
            pt?.decode_kv_cache_usage_percent,
            pt?.prefill_kv_cache_usage_percent,
        ]));
        if (roleSeriesMax !== null) return roleSeriesMax;

        const forensicsPeak = firstFinite(forensics?.peak_kv_cache_pct, serverTelemetry?.peak_kv_cache_pct);
        if (forensicsPeak !== null) return forensicsPeak;
    }

    return firstFinite(
        serverTelemetry?.summary?.kv_cache_usage_percent?.max,
        serverTelemetry?.summary?.gpu_cache_usage_percent?.max,
        forensics?.peak_kv_cache_pct,
        serverTelemetry?.peak_kv_cache_pct,
    );
}

function computeSteadyStateEnergyWh(clientSeries, serverTelemetry, runManifest, warmupSeconds) {
    const series = Array.isArray(serverTelemetry?.series) ? serverTelemetry.series : [];
    if (series.length < 2) return null;
    const originMs = resolveOriginMs(clientSeries, serverTelemetry, runManifest);
    const samples = [];
    for (const pt of series) {
        let relSec = firstFinite(pt?.elapsed_seconds, pt?.time_seconds, pt?.t);
        if (relSec === null && typeof pt?.timestamp === 'string' && pt.timestamp && originMs !== null) {
            const parsed = Date.parse(pt.timestamp);
            if (Number.isFinite(parsed)) relSec = (parsed - originMs) / 1000;
        }
        const watts = firstFinite(pt?.gpu_power_watts, pt?.power_watts, pt?.accelerator_power_watts);
        if (relSec !== null && relSec >= warmupSeconds && watts !== null && watts >= 0) {
            samples.push([relSec, watts]);
        }
    }
    if (samples.length < 2) return null;
    samples.sort((a, b) => a[0] - b[0]);
    const stepSeconds = firstFinite(serverTelemetry?.step_seconds, 5) || 5;
    const maxGap = Math.max(1, 3 * stepSeconds);
    let totalWs = 0;
    let integrated = false;
    for (let i = 1; i < samples.length; i += 1) {
        const [t0, p0] = samples[i - 1];
        const [t1, p1] = samples[i];
        const dt = t1 - t0;
        if (dt > 0 && dt <= maxGap) {
            totalWs += 0.5 * (p0 + p1) * dt;
            integrated = true;
        }
    }
    return integrated && totalWs > 0 ? totalWs / 3600 : null;
}

function buildPowerBreakdown({
    serverTelemetry,
    clientSeries,
    runManifest,
    analysisWindow,
    activeOutputTokens,
    fullOutputTokens,
    warmupSeconds,
}) {
    const perPod = Array.isArray(serverTelemetry?.per_pod) ? serverTelemetry.per_pod : [];
    const powerRows = [];
    for (const pod of perPod) {
        const powerStat = pod.gpu_power_watts || pod.power_watts || pod.accelerator_power_watts;
        const avgW = firstFinite(powerStat?.mean, powerStat?.avg, pod.avg_power_watts);
        const peakW = firstFinite(powerStat?.max, powerStat?.peak, pod.peak_power_watts, avgW);
        if (avgW !== null || peakW !== null) {
            powerRows.push({
                name: pod.pod || pod.device || 'Pod',
                avg_w: avgW ?? 0,
                peak_w: peakW ?? avgW ?? 0,
            });
        }
    }

    const summaryPower = serverTelemetry?.summary?.gpu_power_watts
        || serverTelemetry?.summary?.power_watts
        || serverTelemetry?.summary?.accelerator_power_watts
        || serverTelemetry?.power_summary
        || {};
    const avgPowerW = firstFinite(
        serverTelemetry?.energy?.accelerator_avg_power_watts,
        summaryPower.mean,
        summaryPower.avg,
        serverTelemetry?.power?.avg_watts,
        serverTelemetry?.energy?.avg_power_watts,
    );
    const peakPowerW = firstFinite(
        serverTelemetry?.energy?.accelerator_peak_power_watts,
        summaryPower.max,
        summaryPower.peak,
        serverTelemetry?.power?.peak_watts,
        serverTelemetry?.energy?.peak_power_watts,
    );
    const fullEnergyWh = firstFinite(
        serverTelemetry?.energy?.accelerator_energy_wh,
        serverTelemetry?.energy_wh,
        serverTelemetry?.energy?.total_wh,
        serverTelemetry?.power?.energy_wh,
        serverTelemetry?.summary?.energy_wh,
    );
    const profilingEnergyWh = analysisWindow === 'profiling' && warmupSeconds > 0
        ? computeSteadyStateEnergyWh(clientSeries, serverTelemetry, runManifest, warmupSeconds)
        : null;
    const useProfilingEnergy = analysisWindow === 'profiling'
        && profilingEnergyWh !== null
        && profilingEnergyWh > 0
        && activeOutputTokens !== null;
    const totalEnergyWh = useProfilingEnergy ? profilingEnergyWh : fullEnergyWh;
    const fullTokPerWh = firstFinite(
        serverTelemetry?.energy?.output_tokens_per_wh,
        fullEnergyWh && fullEnergyWh > 0 && fullOutputTokens !== null
            ? fullOutputTokens / fullEnergyWh
            : null,
    );
    const outputTokPerWh = useProfilingEnergy
        ? activeOutputTokens / profilingEnergyWh
        : fullTokPerWh;

    return {
        rows: powerRows.slice(0, 12),
        avgPowerW,
        peakPowerW,
        totalEnergyWh,
        outputTokPerWh,
    };
}

function buildBoundaryLabels({
    timeSeries,
    totalDurationSeconds,
    warmupEndSeconds,
    dispatchDurationSeconds,
    hasGraceDrain,
}) {
    const tValues = timeSeries.map((p) => Number(p?.t)).filter(Number.isFinite);
    const xMin = tValues.length > 0 ? Math.min(...tValues) : 0;
    const xMax = tValues.length > 0 ? Math.max(...tValues) : (totalDurationSeconds ?? 0);
    const xSpan = Number.isFinite(xMin) && Number.isFinite(xMax) && xMax > xMin ? xMax - xMin : 0;

    // On a zero-width vertical ReferenceLine, Recharts 'insideTopLeft' renders at x + offset
    // (to the RIGHT of the line, textAnchor="start") while 'insideTopRight' renders at x - offset
    // (to the LEFT of the line, textAnchor="end").
    // Warmup always belongs on the LEFT of the warmup boundary line, and Drain always belongs on
    // the RIGHT of the drain boundary line. Placing both labels in the top margin band (dy: -18
    // with margin.top >= 22) keeps small warmups clear of Y-axis ticks and avoids curve overlap.
    const warmupPosition = 'insideTopRight';
    const drainPosition = 'insideTopLeft';
    const labelDy = -18;
    let drainDy = labelDy;

    // Only in a degenerate run where warmup extends past drain within a narrow span,
    // stagger Drain downward so the two inward-facing labels do not overlap.
    if (warmupEndSeconds > 0 && hasGraceDrain && xSpan > 0 && warmupEndSeconds > dispatchDurationSeconds) {
        const gapFrac = (warmupEndSeconds - dispatchDurationSeconds) / xSpan;
        if (gapFrac < 0.24) {
            drainDy = labelDy + 14;
        }
    }

    return {
        warmupLabel: {
            value: 'Warmup',
            position: warmupPosition,
            fill: 'var(--text-muted)',
            fontSize: 10,
            dy: labelDy,
        },
        drainLabel: {
            value: 'Drain',
            position: drainPosition,
            fill: 'var(--text-muted)',
            fontSize: 10,
            dy: drainDy,
        },
    };
}

function sparseDotProp(data, dataKey, color) {
    if (!Array.isArray(data) || !data.length) return false;
    const isFiniteAt = (idx) => {
        if (idx < 0 || idx >= data.length) return false;
        const val = data[idx]?.[dataKey];
        return val !== null && val !== undefined && val !== '' && Number.isFinite(Number(val));
    };
    const nonNullIndices = [];
    for (let i = 0; i < data.length; i += 1) {
        if (isFiniteAt(i)) nonNullIndices.push(i);
    }
    if (nonNullIndices.length === 0) return false;
    if (nonNullIndices.length === 1) {
        return { r: 3.5, fill: color, stroke: 'var(--bg-card)', strokeWidth: 1.5 };
    }
    const isolatedPayloads = new Set(
        nonNullIndices
            .filter((idx) => !isFiniteAt(idx - 1) && !isFiniteAt(idx + 1))
            .map((idx) => data[idx]),
    );
    if (isolatedPayloads.size === 0) return false;
    return function IsolatedDot(dotProps) {
        const { cx, cy, value, payload, index } = dotProps || {};
        if (value === null || value === undefined || !Number.isFinite(cx) || !Number.isFinite(cy)) return null;
        if (!isolatedPayloads.has(payload)) return null;
        return (
            <circle
                key={`${dataKey}-dot-${index}`}
                className="recharts-dot recharts-line-dot"
                cx={cx}
                cy={cy}
                r={3.5}
                fill={color}
                stroke="var(--bg-card)"
                strokeWidth={1.5}
            />
        );
    };
}

function formatMsPerTok(value) {
    const formatted = formatMilliseconds(value);
    return formatted === '—' ? '—' : `${formatted}/tok`;
}

function filterTooltipRows(payload) {
    if (!Array.isArray(payload)) return [];
    const rows = [];
    const seenKeys = new Set();
    for (const item of payload) {
        if (!item?.dataKey || String(item.dataKey).endsWith('_band')) continue;
        if (item.color === 'transparent' || item.color === 'none') continue;
        if (item.value === null || item.value === undefined) continue;
        if (seenKeys.has(item.dataKey)) continue;
        seenKeys.add(item.dataKey);
        rows.push(item);
    }
    return rows;
}

function buildTimeSeriesTooltip(unit) {
    return function OverviewSeriesTooltip({ active, payload, label }) {
        if (!active || !payload?.length) return null;
        const rows = filterTooltipRows(payload);
        if (!rows.length) return null;
        return (
            <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
                {rows.map((item) => {
                    const value = unit === 'ms/tok'
                        ? formatMsPerTok(item.value)
                        : unit === 'ms'
                            ? formatMilliseconds(item.value)
                            : formatNumber(item.value);
                    const rowUnit = (unit === 'ms' || unit === 'ms/tok' || value === '—') ? '' : ` ${unit}`;
                    const statKey = String(item.dataKey || '').split('_').pop();
                    const rowOpacity = item.strokeOpacity ?? LATENCY_STAT_OPACITY_BY_KEY[statKey] ?? 1;
                    return (
                        <ChartTooltipRow
                            key={item.dataKey}
                            color={item.color}
                            opacity={rowOpacity}
                            label={item.name}
                            value={value}
                            unit={rowUnit}
                        />
                    );
                })}
            </ChartTooltip>
        );
    };
}

function ComparisonLatencyTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = filterTooltipRows(payload);
    if (!rows.length) return null;
    const category = label ?? rows[0]?.payload?.metric ?? '';
    const metricUnit = rows[0]?.payload?.unit
        || (String(category).toUpperCase().includes('TPOT') ? 'ms/tok' : 'ms');
    return (
        <ChartTooltip title={String(category)}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={metricUnit === 'ms/tok' ? formatMsPerTok(item.value) : formatMilliseconds(item.value)}
                />
            ))}
        </ChartTooltip>
    );
}

function PowerTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = filterTooltipRows(payload);
    if (!rows.length) return null;
    const category = label ?? rows[0]?.payload?.name ?? '';
    return (
        <ChartTooltip title={String(category)}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={formatWatts(item.value)}
                />
            ))}
        </ChartTooltip>
    );
}

const QpsTooltip = buildTimeSeriesTooltip('req/s');
const TokTooltip = buildTimeSeriesTooltip('tok/s');
const LatencyTooltip = buildTimeSeriesTooltip('ms');
const TpotTooltip = buildTimeSeriesTooltip('ms/tok');
const ConcurrencyTooltip = buildTimeSeriesTooltip('req');

const EMPTY_OBJ = {};

export function OverviewTab({ task, telemetry, analysisWindow = 'profiling', onSelectTab, onOpenErrorInspector }) {
    const [throughputMode, setThroughputMode] = useState('qps');
    const [latencyMode, setLatencyMode] = useState('ttft');
    const [selectedLatencyStats, setSelectedLatencyStats] = useState(DEFAULT_LATENCY_STATS);
    const [coldLatencyMode, setColdLatencyMode] = useState('ttft');

    const forensics = telemetry?.forensics ?? EMPTY_OBJ;
    const verdict = forensics.verdict ?? EMPTY_OBJ;
    const clientSeries = telemetry?.client_series ?? EMPTY_OBJ;
    const serverTelemetry = telemetry?.server_telemetry ?? EMPTY_OBJ;
    const capabilities = telemetry?.capabilities ?? EMPTY_OBJ;
    const hwProfile = serverTelemetry?.hardware_profile ?? serverTelemetry?.hardware_profiles?.[0] ?? null;
    const accelLabel = hwProfile?.ui?.labels?.metric_group
        || hwProfile?.metric_group
        || hwProfile?.display_name
        || 'Accelerator';

    const windows = clientSeries.windows ?? forensics.windows ?? EMPTY_OBJ;
    const profilingRaw = windows.profiling ?? clientSeries.summary ?? task?.result?.summary ?? EMPTY_OBJ;
    const fullRaw = windows.full ?? clientSeries.summary ?? task?.result?.summary ?? EMPTY_OBJ;
    const warmupRaw = windows.warmup ?? null;

    const active = useMemo(
        () => windowMetrics(analysisWindow === 'full' ? fullRaw : profilingRaw, fullRaw),
        [analysisWindow, fullRaw, profilingRaw],
    );
    const full = useMemo(() => windowMetrics(fullRaw), [fullRaw]);
    const warmup = useMemo(() => (warmupRaw ? windowMetrics(warmupRaw) : null), [warmupRaw]);

    const serverAvailable = serverTelemetry?.status === 'available' || (serverTelemetry?.available === true && serverTelemetry?.status !== 'unavailable');
    const scope = forensics.telemetry_scope
        || telemetry?.telemetry_scope
        || (serverAvailable ? 'full' : 'client_only');
    const isClientOnly = String(scope).includes('client_only') || !serverAvailable;
    const isPartial = Boolean(forensics.partial || telemetry?.partial || capabilities?.partial);
    const isLegacy = Boolean(forensics.legacy || scope === 'client_only_legacy');

    const verdictBadge = verdictBadgeProps(verdict);
    const severityTone = verdict.severity === 'critical'
        ? 'danger'
        : verdict.severity === 'warning'
            ? 'warning'
            : verdict.severity === 'healthy'
                ? 'success'
                : 'neutral';

    const backendOptions = task?.simulation?.backend_options || task?.backend_options || {};
    const configuredSlos = resolveConfiguredSlos(task, telemetry);
    const { ttftSloMs, tpotSloMs, e2eSloMs } = configuredSlos;

    const isFullWindow = analysisWindow === 'full';
    const activeSloAttainmentPct = firstFinite(
        active.sloAttainmentPct,
        active.goodRequests !== null && active.totalRequests > 0
            ? (active.goodRequests / active.totalRequests) * 100
            : null,
    );
    const attainmentPct = isFullWindow
        ? firstFinite(activeSloAttainmentPct, verdict.attainment_pct)
        : firstFinite(verdict.attainment_pct, activeSloAttainmentPct);
    const verdictThroughputRps = isFullWindow
        ? firstFinite(active.rps, verdict.throughput_rps)
        : firstFinite(verdict.throughput_rps, active.rps);
    const verdictThroughputHr = isFullWindow
        ? firstFinite(
            active.throughputPerHr,
            active.rps !== null ? active.rps * 3600 : null,
            verdict.throughput_per_hr,
            verdictThroughputRps !== null ? verdictThroughputRps * 3600 : null,
        )
        : firstFinite(
            verdict.throughput_per_hr,
            verdictThroughputRps !== null ? verdictThroughputRps * 3600 : null,
            active.throughputPerHr,
        );
    const verdictGoodputRps = isFullWindow
        ? firstFinite(active.goodputRps, verdict.goodput_rps)
        : firstFinite(verdict.goodput_rps, active.goodputRps);
    const verdictGoodputHr = isFullWindow
        ? firstFinite(
            active.goodputPerHr,
            active.goodputRps !== null ? active.goodputRps * 3600 : null,
            verdict.goodput_per_hr,
            verdictGoodputRps !== null ? verdictGoodputRps * 3600 : null,
        )
        : firstFinite(
            verdict.goodput_per_hr,
            verdictGoodputRps !== null ? verdictGoodputRps * 3600 : null,
            active.goodputPerHr,
        );

    const evidenceItems = Array.isArray(verdict.evidence) ? verdict.evidence.slice(0, 4) : [];
    const errorRatePct = active.errorRatePct ?? (
        active.totalRequests > 0 && active.failedRequests !== null
            ? (active.failedRequests / active.totalRequests) * 100
            : null
    );

    const peakKvPct = resolvePeakKvCachePct(serverTelemetry, forensics, telemetry?.run_manifest);

    const warmupEndSeconds = firstFinite(
        clientSeries.warmup_seconds,
        backendOptions.warmup_seconds,
        0,
    ) || 0;

    const powerInfo = useMemo(
        () => buildPowerBreakdown({
            serverTelemetry,
            clientSeries,
            runManifest: telemetry?.run_manifest,
            analysisWindow,
            activeOutputTokens: active.totalOutputTokens,
            fullOutputTokens: full.totalOutputTokens,
            warmupSeconds: warmupEndSeconds,
        }),
        [serverTelemetry, clientSeries, telemetry?.run_manifest, analysisWindow, active.totalOutputTokens, full.totalOutputTokens, warmupEndSeconds],
    );

    const timeSeries = useMemo(
        () => buildTimeSeriesPoints(clientSeries, serverTelemetry, telemetry?.run_manifest),
        [clientSeries, serverTelemetry, telemetry?.run_manifest],
    );

    const dispatchDurationSeconds = firstFinite(clientSeries.dispatch_duration_seconds);
    const lastSeriesT = timeSeries.length > 0 ? timeSeries[timeSeries.length - 1].t : null;
    const totalDurationSeconds = firstFinite(
        clientSeries.duration_seconds,
        fullRaw.duration_seconds,
        full.durationSeconds,
        lastSeriesT,
    );
    const hasGraceDrain = dispatchDurationSeconds !== null
        && totalDurationSeconds !== null
        && (totalDurationSeconds - dispatchDurationSeconds) >= 3.0;
    const drainEndSeconds = hasGraceDrain
        ? (lastSeriesT !== null && lastSeriesT > dispatchDurationSeconds ? Math.min(lastSeriesT, totalDurationSeconds) : totalDurationSeconds)
        : null;
    const drainSubtitleSuffix = hasGraceDrain
        ? ` · Drain starts at ${formatDecimal(dispatchDurationSeconds)}s`
        : '';
    const { warmupLabel, drainLabel } = buildBoundaryLabels({
        timeSeries,
        totalDurationSeconds,
        warmupEndSeconds,
        dispatchDurationSeconds,
        hasGraceDrain,
    });

    const hasConcurrencyServer = timeSeries.some((p) => p.server_running !== null);
    const hasConcurrencyClient = timeSeries.some((p) => p.client_inflight !== null);
    const configuredConcurrency = resolveConfiguredConcurrency(task, telemetry?.run_manifest);
    const maxObservedConcurrency = useMemo(() => {
        let maxVal = 0;
        for (const point of timeSeries) {
            if (Number.isFinite(point.client_inflight)) maxVal = Math.max(maxVal, point.client_inflight);
            if (Number.isFinite(point.server_running)) maxVal = Math.max(maxVal, point.server_running);
        }
        return maxVal;
    }, [timeSeries]);
    const concurrencyYMax = configuredConcurrency !== null
        ? Math.max(configuredConcurrency + 1, Math.ceil(Math.max(maxObservedConcurrency, configuredConcurrency) * 1.12))
        : null;
    const concurrencyLegendEntries = [
        ...(hasConcurrencyClient ? [{ label: 'Client In-Flight Requests', color: CHART_SERIES[0] }] : []),
        ...(hasConcurrencyServer ? [{ label: 'vLLM Running Requests', color: CHART_SERIES[1] }] : []),
        ...(configuredConcurrency !== null
            ? [{ label: `Configured Concurrency (${formatCount(configuredConcurrency)})`, color: CHART_SERIES[2] }]
            : []),
    ];

    const latencySloMs = latencyMode === 'ttft' ? ttftSloMs : latencyMode === 'tpot' ? tpotSloMs : e2eSloMs;
    const latencyLabel = latencyMode === 'ttft' ? 'TTFT' : latencyMode === 'tpot' ? 'TPOT' : 'E2E';
    const availableLatencyStats = useMemo(
        () => LATENCY_STAT_DESCRIPTORS.filter((stat) =>
            timeSeries.some((pt) => Number.isFinite(pt[`${latencyMode}_${stat.key}`])),
        ),
        [timeSeries, latencyMode],
    );
    const availableLatencyStatKeys = useMemo(
        () => new Set(availableLatencyStats.map((stat) => stat.key)),
        [availableLatencyStats],
    );
    const visibleLatencyStats = useMemo(
        () => resolveVisibleLatencyStats(selectedLatencyStats, availableLatencyStats),
        [availableLatencyStats, selectedLatencyStats],
    );
    const visibleLatencyStatKeys = useMemo(
        () => new Set(visibleLatencyStats.map((stat) => stat.key)),
        [visibleLatencyStats],
    );
    const allAvailableLatencyStatsActive = availableLatencyStats.length > 0
        && availableLatencyStats.every((stat) => visibleLatencyStatKeys.has(stat.key));

    const handleToggleLatencyStat = (statKey) => {
        if (!availableLatencyStatKeys.has(statKey)) return;
        setSelectedLatencyStats((prev) => toggleLatencyStatSelection(prev, statKey, availableLatencyStatKeys));
    };

    const handleToggleAllLatencyStats = () => {
        setSelectedLatencyStats(toggleAllLatencyStats(allAvailableLatencyStatsActive, availableLatencyStats));
    };

    const overviewLatencyYMax = computeOverviewLatencyYMax({
        timeSeries,
        latencyMode,
        visibleStats: visibleLatencyStats,
        latencySloMs,
    });
    const hasLatencySeries = availableLatencyStats.length > 0;
    const showLatencyBand = visibleLatencyStatKeys.has('p50') && visibleLatencyStatKeys.has('p90');
    const visibleLatencyLabels = visibleLatencyStats.map((stat) => stat.label).join(', ');
    const latencySubtitle = showLatencyBand
        ? `${latencyLabel} ${visibleLatencyLabels} with P50–P90 envelope${drainSubtitleSuffix}`
        : `${latencyLabel} ${visibleLatencyLabels || 'stats'}${drainSubtitleSuffix}`;
    const latencyLegendEntries = [
        ...visibleLatencyStats.map((stat) => ({
            label: `${latencyLabel} ${stat.label}`,
            color: stat.color,
            opacity: stat.strokeOpacity,
        })),
        ...(latencySloMs !== null
            ? [{ label: `SLO (${formatMilliseconds(latencySloMs)})`, color: CHART_SERIES[2] }]
            : []),
    ];

    const coldStart = clientSeries.cold_start_comparison || null;
    const coldWindow = coldStart?.warmup ?? coldStart?.cold ?? warmupRaw ?? warmup;
    const steadyWindow = coldStart?.steady_state ?? coldStart?.steady ?? windows.profiling ?? active;
    const coldStartSourceLabel = coldStart?.note
        || ((coldStart?.mode === 'auto_10_percent' || coldStart?.mode === 'auto_10pct' || warmupEndSeconds <= 0)
            ? 'Auto 10% warmup check'
            : `Configured ${formatDurationSeconds(warmupEndSeconds)} warmup`);

    const coldComparisonSeriesByMetric = useMemo(
        () => Object.fromEntries(
            COLD_COMPARISON_METRICS.map((meta) => [
                meta.key,
                buildColdComparisonBars(coldWindow, steadyWindow, meta.key),
            ]),
        ),
        [coldWindow, steadyWindow],
    );
    const availableColdMetrics = useMemo(
        () => COLD_COMPARISON_METRICS.filter((meta) => (coldComparisonSeriesByMetric[meta.key]?.length || 0) > 0),
        [coldComparisonSeriesByMetric],
    );
    const activeColdLatencyMode = availableColdMetrics.some((meta) => meta.key === coldLatencyMode)
        ? coldLatencyMode
        : (availableColdMetrics[0]?.key || 'ttft');
    const activeColdMetricMeta = COLD_COMPARISON_METRICS.find((meta) => meta.key === activeColdLatencyMode)
        || COLD_COMPARISON_METRICS[0];
    const coldComparisonBars = coldComparisonSeriesByMetric[activeColdLatencyMode] || [];
    const activeColdP50 = extractWindowLatencyStat(coldWindow, activeColdLatencyMode, 'p50');
    const activeSteadyP50 = extractWindowLatencyStat(steadyWindow, activeColdLatencyMode, 'p50');
    const coldToSteadyRatio = (activeColdP50 !== null && activeSteadyP50 !== null && activeSteadyP50 > 0)
        ? (activeColdP50 / activeSteadyP50)
        : (activeColdLatencyMode === 'ttft' ? firstFinite(coldStart?.cold_to_steady_ratio) : null);

    const windowLabel = analysisWindow === 'full' ? 'Full Run' : 'Profiling Steady-State';
    const comparisonSuffix = analysisWindow === 'full'
        ? (warmup ? `Warmup TTFT P50: ${formatMilliseconds(warmup.ttftP50)}` : null)
        : (full.rps !== null ? `Full Run: ${formatNumber(full.rps, ' req/s')}` : null);

    return (
        <div className="space-y-6">
            {/* Automated Verdict Hero Card */}
            <Panel tone="soft" className="space-y-4">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0 flex-1 space-y-2">
                        <div className="flex flex-wrap items-center gap-1.5">
                            <SectionLabel tone="cyan" className="mb-0">Overview · Automated Verdict</SectionLabel>
                            <HelpTooltip
                                label="Automated verdict help"
                                align="start"
                                iconSize={13}
                                formula="Verdict = ArgMaxSeverity(SLO_Attainment, Error_Rate, Queue_Share, KV_Usage, Pod_Skew)"
                            >
                                Rule-based bottleneck classification combining client SLO attainment and Prometheus server telemetry. Summarizes the primary limit constraining throughput or tail latency.
                            </HelpTooltip>
                            {isFullWindow && (
                                <Badge tone="info" size="xs">
                                    Classification: Profiling Steady-State · KPIs: Full Run
                                </Badge>
                            )}
                        </div>
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="inline-flex items-center gap-1">
                                <Badge tone={verdictBadge.tone} size="md">
                                    {verdictBadge.label}
                                </Badge>
                                <HelpTooltip
                                    label="Dominant limit help"
                                    align="start"
                                    iconSize={12}
                                    formula="Primary_Limit = TopRankedFinding(Bottleneck_Rules) | 'Healthy'"
                                >
                                    Dominant bottleneck category identified for this run (for example Healthy, Queueing, KV Pressure, or Capacity Limit) from forensic thresholds.
                                </HelpTooltip>
                            </span>
                            {verdict.severity && (
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone={severityTone} size="sm">
                                        {displayName(verdict.severity)}
                                    </Badge>
                                    <HelpTooltip
                                        label="Verdict severity help"
                                        align="start"
                                        iconSize={12}
                                        formula="Severity = max_i Severity(Finding_i) ∈ {Healthy, Warning, Critical}"
                                    >
                                        Overall impact level of the dominant limit (Healthy, Warning, or Critical) based on SLO violations, error rates, and saturation signals.
                                    </HelpTooltip>
                                </span>
                            )}
                            {isClientOnly && (
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone="warning" size="sm">Client-only verdict</Badge>
                                    <HelpTooltip
                                        label="Client-only verdict help"
                                        align="start"
                                        iconSize={12}
                                        formula="Scope = 'client_only' iff Server_Telemetry = Unavailable"
                                    >
                                        Server-side Prometheus metrics were unavailable, so this verdict relies solely on client benchmark runner timing and error outcomes.
                                    </HelpTooltip>
                                </span>
                            )}
                            {isPartial && (
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone="warning" size="sm">Partial Results</Badge>
                                    <HelpTooltip
                                        label="Partial results help"
                                        align="start"
                                        iconSize={12}
                                        formula="Partial iff any Expected_Telemetry_Stream = Missing"
                                    >
                                        One or more telemetry artifacts were incomplete or omitted for this run; unaffected metrics remain valid.
                                    </HelpTooltip>
                                </span>
                            )}
                            {isLegacy && (
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone="neutral" size="sm">Legacy run</Badge>
                                    <HelpTooltip
                                        label="Legacy run help"
                                        align="start"
                                        iconSize={12}
                                        formula="Legacy iff Per_Bucket_Forensic_Streams = Absent"
                                    >
                                        Telemetry was reconstructed from a legacy task summary format without full per-bucket client or cluster forensic streams.
                                    </HelpTooltip>
                                </span>
                            )}
                        </div>
                        <p className="text-sm leading-relaxed text-theme-text">
                            {verdict.reason || 'Diagnostic summary derived from recorded trace replay metrics.'}
                        </p>
                    </div>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 shrink-0">
                        <div className="rounded-lg border border-theme-border bg-theme-card px-4 py-3">
                            <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                                <span>SLO Attainment</span>
                                <HelpTooltip
                                    label="SLO Attainment help"
                                    align="end"
                                    iconSize={12}
                                    formula="SLO_Attainment (%) = (Good_Requests / Total_Window_Requests) × 100"
                                >
                                    Percentage of dispatched requests completing without errors and within all configured TTFT, TPOT, and E2E SLO targets. Higher values indicate compliant serving capacity.
                                </HelpTooltip>
                            </div>
                            <div className="mt-1 text-xl font-bold font-mono text-theme-text">
                                {attainmentPct !== null ? formatNumber(attainmentPct, '%') : '—'}
                            </div>
                            <div className="mt-0.5 text-[11px] text-theme-muted">
                                {configuredSlos.targets.length > 0 ? (
                                    <HelpTooltip
                                        label="Configured SLOs"
                                        align="start"
                                        tooltipClassName="w-56 max-w-[min(18rem,80vw)]"
                                        trigger={(
                                            <button
                                                type="button"
                                                aria-label={`Configured SLOs (${configuredSlos.targets.length})`}
                                                className="inline-flex items-center gap-1 rounded border border-theme-border bg-theme-bg/60 px-1.5 py-0.5 text-[11px] font-medium text-theme-muted underline decoration-dotted underline-offset-2 transition-colors hover:border-theme-text/30 hover:text-theme-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40"
                                            >
                                                <span>SLOs</span>
                                                <span className="font-mono text-[10px]">
                                                    ({configuredSlos.targets.length})
                                                </span>
                                            </button>
                                        )}
                                    >
                                        <span className="block space-y-1.5">
                                            <span className="block text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                                                Configured SLOs
                                            </span>
                                            <span className="block space-y-1">
                                                {configuredSlos.targets.map((item) => (
                                                    <span
                                                        key={item.key}
                                                        className="flex items-center justify-between gap-3 text-[11px]"
                                                    >
                                                        <span className="text-theme-muted">{item.label}</span>
                                                        {' '}
                                                        <span className="font-mono font-semibold text-theme-text">
                                                            ≤ {item.formatted}
                                                        </span>
                                                    </span>
                                                ))}
                                            </span>
                                        </span>
                                    </HelpTooltip>
                                ) : (
                                    <span>No SLOs configured</span>
                                )}
                            </div>
                        </div>
                        <div className="rounded-lg border border-theme-border bg-theme-card px-4 py-3">
                            <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                                <span>Request Throughput</span>
                                <HelpTooltip
                                    label="Throughput verdict help"
                                    align="end"
                                    iconSize={12}
                                    formula="Throughput = Total_Window_Requests / Effective_Window_s;  Effective_Window_s = max(Configured_Window_s, max(t_complete) − min(t_arrival))"
                                >
                                    Sustained request completion rate (req/s and req/hr) over the active analysis window, regardless of latency SLO compliance.
                                </HelpTooltip>
                            </div>
                            <div className="mt-1 text-xl font-bold font-mono text-theme-text">
                                {verdictThroughputRps !== null ? formatNumber(verdictThroughputRps, ' req/s') : '—'}
                            </div>
                            <div className="mt-0.5 text-[11px] text-theme-muted">
                                {verdictThroughputHr !== null ? `${formatCount(verdictThroughputHr)} req/hr` : 'Rate unavailable'}
                            </div>
                        </div>
                        <div className="rounded-lg border border-theme-border bg-theme-card px-4 py-3">
                            <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                                <span>Goodput</span>
                                <HelpTooltip
                                    label="Goodput help"
                                    align="end"
                                    iconSize={12}
                                    formula="Goodput = Good_Requests / Effective_Window_s  (× 3600 for req/hr);  Good iff OK ∧ TTFT ≤ SLO ∧ TPOT ≤ SLO ∧ E2E ≤ SLO"
                                >
                                    Effective request rate (req/s) meeting both success and latency SLO criteria. A widening gap below total throughput signals latency violations under load.
                                </HelpTooltip>
                            </div>
                            <div className="mt-1 text-xl font-bold font-mono text-emerald-600 dark:text-emerald-300">
                                {verdictGoodputRps !== null ? formatNumber(verdictGoodputRps, ' req/s') : '—'}
                            </div>
                            <div className="mt-0.5 text-[11px] text-theme-muted">
                                {verdictGoodputHr !== null ? `${formatCount(verdictGoodputHr)} req/hr` : 'SLO-compliant rate'}
                            </div>
                        </div>
                    </div>
                </div>

                {evidenceItems.length > 0 && (
                    <div className="border-t border-theme-border pt-3">
                        <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-theme-muted">
                            <span>Key Evidence</span>
                            <HelpTooltip
                                label="Key evidence help"
                                align="start"
                                iconSize={12}
                                formula="Top_4(Observed_Metrics exceeding Forensic_Thresholds)"
                            >
                                Top measured client and server observations supporting the automated verdict. Click an item to jump to its detailed diagnostic tab.
                            </HelpTooltip>
                        </div>
                        <ul className="grid gap-2 md:grid-cols-2">
                            {evidenceItems.map((item, idx) => {
                                const text = typeof item === 'string' ? item : (item.text || item.summary || item.message || '');
                                const pairedFinding = Array.isArray(forensics.findings) ? forensics.findings[idx] : null;
                                const targetTab = typeof item === 'object'
                                    ? (item.target_tab ?? item.tab ?? pairedFinding?.tab ?? pairedFinding?.target_tab)
                                    : (pairedFinding?.tab ?? pairedFinding?.target_tab ?? null);
                                return (
                                    <li key={`${idx}-${targetTab || 'ev'}`}>
                                        <button
                                            type="button"
                                            disabled={!targetTab || !onSelectTab}
                                            onClick={() => targetTab && onSelectTab?.(targetTab)}
                                            className={cn(
                                                'flex w-full items-start justify-between gap-3 rounded-lg border border-theme-border bg-theme-card px-3 py-2.5 text-left text-xs transition-colors',
                                                targetTab && onSelectTab
                                                    ? 'cursor-pointer hover:border-emerald-500/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50'
                                                    : 'cursor-default',
                                            )}
                                        >
                                            <span className="leading-relaxed text-theme-text">{text}</span>
                                            {targetTab && (
                                                <span className="inline-flex shrink-0 items-center gap-1 font-semibold text-emerald-600 dark:text-emerald-300">
                                                    {displayName(targetTab)}
                                                    <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
                                                </span>
                                            )}
                                        </button>
                                    </li>
                                );
                            })}
                        </ul>
                    </div>
                )}
            </Panel>

            {/* Summary KPI Tiles */}
            <section aria-label="Overview KPI summary">
                <div className="mb-2 flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5">
                        <SectionLabel tone="emerald" className="mb-0">
                            Window Metrics · {windowLabel}
                        </SectionLabel>
                        <HelpTooltip
                            label="Analysis window help"
                            align="start"
                            iconSize={13}
                            formula="Profiling = [Warmup_s, End_s];  Full = [0, End_s]"
                        >
                            Profiling Steady-State excludes initial warmup traffic to isolate sustained performance; Full Run spans the entire replay including cold-start warmup and grace drain.
                        </HelpTooltip>
                    </div>
                    {comparisonSuffix && (
                        <span className="text-xs text-theme-muted">{comparisonSuffix}</span>
                    )}
                </div>
                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                    <StatCard
                        compact
                        icon={<Layers className="h-4 w-4" />}
                        title="Completed / SLO-Compliant Requests"
                        help="Total requests vs requests that succeeded within all configured latency SLOs during the active window. A higher SLO-compliant share indicates healthy serving headroom."
                        helpFormula="Good = Count(OK ∧ TTFT ≤ SLO_TTFT ∧ TPOT ≤ SLO_TPOT ∧ E2E ≤ SLO_E2E)"
                        helpAlign="start"
                        value={`${formatCount(active.totalRequests)} / ${formatCount(active.goodRequests ?? active.successfulRequests)}`}
                        details={[
                            {
                                label: 'Successful',
                                value: formatCount(active.successfulRequests),
                                help: 'Requests that returned a valid non-error response in the active window, including those exceeding latency SLOs.',
                                formula: 'Successful = Count(HTTP_2xx ∧ ¬Transport_Error)',
                            },
                            {
                                label: 'Full Run Total',
                                value: formatCount(full.totalRequests),
                                help: 'Total requests dispatched across the entire replay including warmup and grace-drain periods.',
                                formula: 'Full_Total = Warmup_Requests + Profiling_Requests + Drain_Requests',
                            },
                        ]}
                    />
                    <StatCard
                        compact
                        icon={<Gauge className="h-4 w-4" />}
                        title="Request Throughput"
                        help="Average completed request rate (req/s) in the active window from client timing. Compare with target arrival rate to spot saturation or queueing backpressure."
                        helpFormula="Throughput = Total_Window_Requests / Effective_Window_s;  Effective_Window_s = max(Configured_Window_s, max(t_complete) − min(t_arrival))"
                        helpAlign="end"
                        value={formatNumber(active.rps, ' req/s')}
                        details={[
                            {
                                label: 'Output Throughput',
                                value: formatNumber(active.outputTps, ' tok/s'),
                                help: 'Aggregate generated decode tokens per second across completed requests. Reflects sustained decode compute capacity.',
                                formula: 'Output_Rate (tok/s) = Σ Output_Tokens / Effective_Window_s',
                            },
                            {
                                label: 'Full Run (req/s)',
                                value: formatNumber(full.rps, ' req/s'),
                                help: 'Completed request rate averaged over the entire replay duration including warmup and drain.',
                                formula: 'Full_Throughput = Total_Full_Requests / Effective_Full_Window_s',
                            },
                        ]}
                    />
                    <StatCard
                        compact
                        icon={<Clock3 className="h-4 w-4" />}
                        title="TTFT (P50)"
                        help="Median Time to First Token (ms) from client request dispatch to first streamed token. Elevated TTFT indicates prefill compute pressure, queue wait, or Inference Gateway / EPP overhead."
                        helpFormula="TTFT_i = t_first_token − t_request_start;  P50 = Quantile_0.50(TTFT)"
                        helpAlign="start"
                        value={formatMilliseconds(active.ttftP50)}
                        tone={ttftSloMs !== null && active.ttftP90 !== null && active.ttftP90 > ttftSloMs ? 'warning' : 'neutral'}
                        details={[
                            {
                                label: 'P90 / P99',
                                value: `${formatMilliseconds(active.ttftP90)} / ${formatMilliseconds(active.ttftP99)}`,
                                help: '90th and 99th percentile TTFT (ms) highlighting tail latency spikes under bursty arrivals or KV pressure.',
                                formula: 'Pq = Quantile_q(sorted(TTFT))  for q ∈ {0.90, 0.99}',
                            },
                            {
                                label: 'Full Run P50',
                                value: formatMilliseconds(full.ttftP50),
                                help: 'Median TTFT across the full run including cold-start warmup requests.',
                                formula: 'Full_TTFT_P50 = Quantile_0.50(TTFT_full_run)',
                            },
                        ]}
                    />
                    <StatCard
                        compact
                        icon={<Activity className="h-4 w-4" />}
                        title="TPOT (P50)"
                        help="Median Time per Output Token (ms/tok), mean per-request decode latency per generated token after TTFT (distinct from per-step ITL). Rising TPOT signals decode batch contention or memory-bandwidth saturation."
                        helpFormula="TPOT_i = (t_last_token − t_first_token) / max(1, Output_Tokens − 1);  P50 = Quantile_0.50(TPOT)"
                        helpAlign="end"
                        value={formatMsPerTok(active.tpotP50)}
                        tone={tpotSloMs !== null && active.tpotP90 !== null && active.tpotP90 > tpotSloMs ? 'warning' : 'neutral'}
                        details={[
                            {
                                label: 'P90 / P99',
                                value: `${formatMsPerTok(active.tpotP90)} / ${formatMsPerTok(active.tpotP99)}`,
                                help: '90th and 99th percentile TPOT (ms/tok), capturing decode tail jitter.',
                                formula: 'Pq = Quantile_q(sorted(TPOT))  for q ∈ {0.90, 0.99}',
                            },
                            {
                                label: 'Source',
                                value: active.tpotMeasuredRatio !== null && active.tpotMeasuredRatio < 0.99
                                    ? `${formatNumber(active.tpotMeasuredRatio * 100, '%')} measured`
                                    : 'Measured',
                                help: 'Share of requests with directly timestamped streaming token chunks versus fallback average decode calculation.',
                                formula: 'Measured_Share (%) = (Requests_with_Token_Timestamps / Total_TPOT_Requests) × 100',
                            },
                        ]}
                    />
                    <StatCard
                        compact
                        icon={<AlertTriangle className="h-4 w-4" />}
                        title="Error Rate"
                        help="Percentage of requests in the active window that failed with HTTP or transport errors. Non-zero values directly reduce goodput and indicate overload or timeouts."
                        helpFormula="Error_Rate (%) = (Failed_Requests / Total_Window_Requests) × 100"
                        helpAlign="start"
                        value={formatNumber(errorRatePct, '%')}
                        tone={errorRatePct !== null && errorRatePct > 0 ? 'danger' : 'success'}
                        onClick={onOpenErrorInspector ? () => onOpenErrorInspector('all') : undefined}
                        details={[
                            {
                                label: 'Failed Requests',
                                value: formatCount(active.failedRequests),
                                help: 'Count of failed, dropped, or timed-out requests in the active analysis window.',
                                formula: 'Failed = Total_Window_Requests − Successful_Requests',
                            },
                            {
                                label: 'Action',
                                value: 'Inspect errors',
                                help: 'Opens the error breakdown drawer to inspect HTTP status codes, failure counts, and remediation guidance.',
                            },
                        ]}
                    />
                    <StatCard
                        compact
                        icon={<Database className="h-4 w-4" />}
                        title="Peak KV Cache Usage"
                        help="Maximum KV-cache block usage (%) observed across serving engines from Prometheus telemetry. Values approaching 95–100% cause request queueing or preemptions."
                        helpFormula="Peak_KV_Usage (%) = max_t ((Used_Device_KV_Blocks(t) / Total_Device_KV_Blocks) × 100)"
                        helpAlign="end"
                        value={peakKvPct !== null ? formatNumber(peakKvPct, '%') : 'Unavailable'}
                        tone={peakKvPct !== null && peakKvPct >= 90 ? 'warning' : 'neutral'}
                        details={[
                            {
                                label: 'Telemetry Scope',
                                value: displayName(scope),
                                help: 'Indicates whether both client replay and server Prometheus telemetry were collected (Full) or only client metrics (Client Only).',
                                formula: "Scope = 'full' iff Client_Stream ∧ Server_Telemetry else 'client_only'",
                            },
                            {
                                label: 'Mean Prefix Cache Hit Rate',
                                value: formatNumber(serverTelemetry?.summary?.prefix_cache_hit_percent?.mean, '%'),
                                help: 'Average percentage of prompt tokens served from the serving engine prefix cache rather than recomputed, lowering prefill compute load.',
                                formula: 'Mean_Hit_Rate (%) = mean_t ((ΔCached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100)',
                            },
                        ]}
                    />
                    <StatCard
                        compact
                        icon={<Zap className="h-4 w-4" />}
                        title={`${accelLabel} Power & Energy`}
                        help={`Mean power draw (W) across allocated ${accelLabel.toLowerCase()} devices from cluster hardware telemetry. Tracks serving compute power demand during the run.`}
                        helpFormula="Mean_Power (W) = mean_t (Σ_devices Power_W(t))"
                        helpAlign="start"
                        value={powerInfo.avgPowerW !== null ? formatWatts(powerInfo.avgPowerW) : 'Unavailable'}
                        details={[
                            {
                                label: 'Peak Power',
                                value: formatWatts(powerInfo.peakPowerW),
                                help: `Maximum instantaneous ${accelLabel.toLowerCase()} power draw (W) recorded during the run.`,
                                formula: 'Peak_Power (W) = max_t (Σ_devices Power_W(t))',
                            },
                            {
                                label: 'Total Energy',
                                value: formatEnergyWh(powerInfo.totalEnergyWh),
                                help: `Cumulative ${accelLabel.toLowerCase()} energy consumed (Wh) via trapezoidal integration across the telemetry window.`,
                                formula: 'Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i  (skipping Δt > 3×step)',
                            },
                            {
                                label: 'Efficiency',
                                value: powerInfo.outputTokPerWh !== null
                                    ? `${formatNumber(powerInfo.outputTokPerWh)} tok/Wh`
                                    : '—',
                                help: 'Generated output tokens per Watt-hour (tok/Wh); higher values indicate better decode energy efficiency.',
                                formula: 'Efficiency (tok/Wh) = Total_Output_Tokens / Total_Accelerator_Energy_Wh',
                            },
                        ]}
                    />
                </div>
            </section>

            {/* 5 Overview Charts */}
            <div className="grid gap-6 xl:grid-cols-2">
                {/* Chart 1: Throughput over Time */}
                <ChartContainer
                    title="Throughput over Time"
                    subtitle={`${warmupEndSeconds > 0 ? `Shaded warmup: 0–${formatDurationSeconds(warmupEndSeconds)}` : 'Full replay window'}${drainSubtitleSuffix}`}
                    help="Time-series of arrived, completed, and SLO-compliant goodput request rates (req/s) or generated output throughput (tok/s) from client buckets. Falling goodput below arrived req/s indicates saturation or SLO breaches."
                    helpFormula="Arrived = ΔArrived / Δt;  Completed = ΔCompleted / Δt;  Goodput = ΔGood / Δt;  Output = Σ Out_Tokens / Δt"
                    helpAlign="start"
                    actions={(
                        <ToggleGroup
                            size="xs"
                            value={throughputMode}
                            onChange={setThroughputMode}
                            options={[
                                { value: 'qps', label: 'QPS' },
                                { value: 'output', label: 'Output' },
                            ]}
                        />
                    )}
                >
                    {timeSeries.length > 0 ? (
                        <>
                            <div className="h-64 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <ComposedChart data={timeSeries} margin={{ top: 22, right: hasGraceDrain ? 44 : 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="t"
                                            type="number"
                                            domain={['dataMin', 'dataMax']}
                                            tickFormatter={(v) => `${formatNumber(v)}s`}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                        />
                                        <YAxis
                                            width={56}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{
                                                value: throughputMode === 'qps' ? 'req/s' : 'tok/s',
                                                angle: -90,
                                                position: 'insideLeft',
                                                fill: 'var(--text-muted)',
                                                fontSize: 10,
                                            }}
                                        />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={throughputMode === 'qps' ? <QpsTooltip /> : <TokTooltip />} />
                                        {warmupEndSeconds > 0 && (
                                            <ReferenceArea x1={0} x2={warmupEndSeconds} fill="var(--text-muted)" fillOpacity={0.08} />
                                        )}
                                        {warmupEndSeconds > 0 && (
                                            <ReferenceLine
                                                x={warmupEndSeconds}
                                                stroke="var(--text-muted)"
                                                strokeDasharray="4 4"
                                                label={warmupLabel}
                                            />
                                        )}
                                        {hasGraceDrain && drainEndSeconds !== null && drainEndSeconds > dispatchDurationSeconds && (
                                            <ReferenceArea x1={dispatchDurationSeconds} x2={drainEndSeconds} fill="var(--text-muted)" fillOpacity={0.06} />
                                        )}
                                        {hasGraceDrain && (
                                            <ReferenceLine
                                                x={dispatchDurationSeconds}
                                                stroke="var(--text-muted)"
                                                strokeDasharray="4 3"
                                                label={drainLabel}
                                            />
                                        )}
                                        {throughputMode === 'qps' ? (
                                            <>
                                                <Line type="monotone" dataKey="arrived_rps" name="Arrived" stroke={CHART_SERIES[1]} strokeWidth={1.75} connectNulls dot={sparseDotProp(timeSeries, 'arrived_rps', CHART_SERIES[1])} isAnimationActive={false} />
                                                <Line type="monotone" dataKey="completed_rps" name="Completed" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(timeSeries, 'completed_rps', CHART_SERIES[0])} isAnimationActive={false} />
                                                <Line type="monotone" dataKey="goodput_rps" name="Goodput" stroke={CHART_SERIES[2]} strokeWidth={2} strokeDasharray="4 3" connectNulls dot={sparseDotProp(timeSeries, 'goodput_rps', CHART_SERIES[2])} isAnimationActive={false} />
                                            </>
                                        ) : (
                                            <Area type="monotone" dataKey="output_tps" name="Output Throughput (tok/s)" stroke={CHART_SERIES[0]} fill={CHART_SERIES[0]} fillOpacity={0.16} strokeWidth={2} connectNulls dot={sparseDotProp(timeSeries, 'output_tps', CHART_SERIES[0])} isAnimationActive={false} />
                                        )}
                                    </ComposedChart>
                                </ResponsiveContainer>
                            </div>
                            {throughputMode === 'qps' && (
                                <ChartLegend
                                    className="mt-3"
                                    entries={[
                                        { label: 'Arrived (req/s)', color: CHART_SERIES[1] },
                                        { label: 'Completed (req/s)', color: CHART_SERIES[0] },
                                        { label: 'Goodput (req/s)', color: CHART_SERIES[2] },
                                    ]}
                                />
                            )}
                        </>
                    ) : (
                        <EmptyState title="No throughput timeline" message="Per-second client throughput samples were not recorded for this run." />
                    )}
                </ChartContainer>

                {/* Chart 2: Latency over Time */}
                <ChartContainer
                    title="Latency over Time"
                    subtitle={latencySubtitle}
                    help="Tracks selectable latency statistics (Min, P50, Mean, P90, P99, Max) and optional P50–P90 spread (ms, or ms/tok for TPOT) over elapsed time for TTFT, TPOT, or E2E, with optional SLO threshold line. Toggle tail stats off to zoom the Y-axis onto typical latency."
                    helpFormula="Stats = {Min, P50, Mean, P90, P99, Max}(Bucket);  Band = [P50, P90];  TTFT = t_1st − t_0;  TPOT = (t_last − t_1st) / max(1, Out − 1);  E2E = t_end − t_0"
                    helpAlign="end"
                    actions={(
                        <div className="flex flex-wrap items-center justify-end gap-2">
                            <div
                                role="group"
                                aria-label="Latency statistics"
                                className="inline-flex flex-wrap items-center gap-1 rounded-lg border border-theme-border bg-slate-100 dark:bg-slate-900 p-0.5"
                            >
                                {LATENCY_STAT_DESCRIPTORS.map((stat) => {
                                    const isAvailable = availableLatencyStatKeys.has(stat.key);
                                    const isVisible = visibleLatencyStatKeys.has(stat.key);
                                    return (
                                        <button
                                            key={stat.key}
                                            type="button"
                                            disabled={!isAvailable}
                                            aria-disabled={!isAvailable}
                                            aria-pressed={isVisible}
                                            title={!isAvailable ? 'Not available in legacy telemetry' : `Toggle ${stat.label}`}
                                            onClick={() => handleToggleLatencyStat(stat.key)}
                                            className={cn(
                                                'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium transition-colors',
                                                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50',
                                                !isAvailable
                                                    ? 'cursor-not-allowed opacity-40 text-theme-muted'
                                                    : isVisible
                                                        ? 'bg-white dark:bg-slate-700 text-slate-900 dark:text-white shadow-sm'
                                                        : 'text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200',
                                            )}
                                        >
                                            <span
                                                className="h-2 w-2 rounded-full shrink-0"
                                                style={{ backgroundColor: stat.color, opacity: stat.strokeOpacity }}
                                                aria-hidden="true"
                                            />
                                            <span>{stat.label}</span>
                                        </button>
                                    );
                                })}
                                <button
                                    type="button"
                                    disabled={!hasLatencySeries}
                                    onClick={handleToggleAllLatencyStats}
                                    title={allAvailableLatencyStatsActive ? 'Reset to default stats (P50, Mean, P90)' : 'Select all available stats'}
                                    className={cn(
                                        'rounded-md border-l border-theme-border px-1.5 py-0.5 text-[10px] font-semibold transition-colors',
                                        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50',
                                        'text-theme-muted hover:text-theme-text disabled:opacity-40 disabled:pointer-events-none',
                                    )}
                                >
                                    {allAvailableLatencyStatsActive ? 'Default' : 'All'}
                                </button>
                            </div>
                            <ToggleGroup
                                size="xs"
                                value={latencyMode}
                                onChange={setLatencyMode}
                                options={[
                                    { value: 'ttft', label: 'TTFT' },
                                    { value: 'tpot', label: 'TPOT' },
                                    { value: 'e2e', label: 'E2E' },
                                ]}
                            />
                        </div>
                    )}
                >
                    {hasLatencySeries ? (
                        <>
                            <div className="h-64 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <ComposedChart data={timeSeries} margin={{ top: 22, right: hasGraceDrain ? 44 : 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="t"
                                            type="number"
                                            domain={['dataMin', 'dataMax']}
                                            tickFormatter={(v) => `${formatNumber(v)}s`}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                        />
                                        <YAxis
                                            width={60}
                                            domain={overviewLatencyYMax !== null ? [0, overviewLatencyYMax] : undefined}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{
                                                value: latencyMode === 'tpot' ? 'ms/tok' : 'ms',
                                                angle: -90,
                                                position: 'insideLeft',
                                                fill: 'var(--text-muted)',
                                                fontSize: 10,
                                            }}
                                        />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={latencyMode === 'tpot' ? <TpotTooltip /> : <LatencyTooltip />} />
                                        {warmupEndSeconds > 0 && (
                                            <ReferenceArea x1={0} x2={warmupEndSeconds} fill="var(--text-muted)" fillOpacity={0.08} />
                                        )}
                                        {warmupEndSeconds > 0 && (
                                            <ReferenceLine
                                                x={warmupEndSeconds}
                                                stroke="var(--text-muted)"
                                                strokeDasharray="4 4"
                                                label={warmupLabel}
                                            />
                                        )}
                                        {hasGraceDrain && drainEndSeconds !== null && drainEndSeconds > dispatchDurationSeconds && (
                                            <ReferenceArea x1={dispatchDurationSeconds} x2={drainEndSeconds} fill="var(--text-muted)" fillOpacity={0.06} />
                                        )}
                                        {hasGraceDrain && (
                                            <ReferenceLine
                                                x={dispatchDurationSeconds}
                                                stroke="var(--text-muted)"
                                                strokeDasharray="4 3"
                                                label={drainLabel}
                                            />
                                        )}
                                        {latencySloMs !== null && (
                                            <ReferenceLine
                                                y={latencySloMs}
                                                stroke={CHART_SERIES[2]}
                                                strokeDasharray="5 3"
                                                ifOverflow="extendDomain"
                                                label={{
                                                    value: `SLO: ${formatMilliseconds(latencySloMs)}`,
                                                    position: 'insideTopRight',
                                                    fill: CHART_SERIES[2],
                                                    fontSize: 10,
                                                    fontWeight: 600,
                                                }}
                                            />
                                        )}
                                        {showLatencyBand && (
                                            <>
                                                <Area
                                                    type="monotone"
                                                    dataKey={`${latencyMode}_p50`}
                                                    stackId="band"
                                                    stroke="none"
                                                    fill="transparent"
                                                    connectNulls
                                                    tooltipType="none"
                                                    isAnimationActive={false}
                                                    legendType="none"
                                                />
                                                <Area
                                                    type="monotone"
                                                    dataKey={`${latencyMode}_band`}
                                                    stackId="band"
                                                    name={`${latencyLabel} P50–P90 band`}
                                                    stroke="none"
                                                    fill={CHART_SERIES[1]}
                                                    fillOpacity={0.18}
                                                    connectNulls
                                                    tooltipType="none"
                                                    isAnimationActive={false}
                                                />
                                            </>
                                        )}
                                        {visibleLatencyStats.map((stat) => {
                                            const dataKey = `${latencyMode}_${stat.key}`;
                                            return (
                                                <Line
                                                    key={dataKey}
                                                    type="monotone"
                                                    dataKey={dataKey}
                                                    name={`${latencyLabel} ${stat.label}`}
                                                    stroke={stat.color}
                                                    strokeWidth={stat.strokeWidth}
                                                    strokeDasharray={stat.strokeDasharray}
                                                    strokeOpacity={stat.strokeOpacity}
                                                    connectNulls
                                                    dot={sparseDotProp(timeSeries, dataKey, stat.color)}
                                                    isAnimationActive={false}
                                                />
                                            );
                                        })}
                                    </ComposedChart>
                                </ResponsiveContainer>
                            </div>
                            {latencyLegendEntries.length > 0 && (
                                <ChartLegend
                                    className="mt-3"
                                    entries={latencyLegendEntries}
                                />
                            )}
                        </>
                    ) : (
                        <EmptyState
                            title={`No ${latencyLabel} timeline`}
                            message={timeSeries.length > 0
                                ? `Per-bucket ${latencyLabel} percentiles were not recorded for this run.`
                                : 'Per-second latency percentiles were not recorded for this run.'}
                        />
                    )}
                </ChartContainer>

                {/* Chart 3: In-Flight Concurrency */}
                <ChartContainer
                    title="In-Flight Concurrency"
                    subtitle={`Client in-flight requests vs vLLM running requests${drainSubtitleSuffix}`}
                    help="Compares client-side in-flight requests against server-side serving engine running requests over time. A persistent gap where client in-flight exceeds engine running requests indicates Inference Gateway / EPP buffering or engine waiting queue."
                    helpFormula="Client_InFlight(t) = Σ 1[t_start ≤ t < t_complete];  Engine_Running(t) = Σ_pods Active_Running_Requests(t)"
                    helpAlign="start"
                >
                    {(hasConcurrencyClient || hasConcurrencyServer) ? (
                        <>
                            <div className="h-64 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={timeSeries} margin={{ top: 22, right: hasGraceDrain ? 44 : 16, left: 4, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="t"
                                            type="number"
                                            domain={['dataMin', 'dataMax']}
                                            tickFormatter={(v) => `${formatNumber(v)}s`}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                        />
                                        <YAxis
                                            width={52}
                                            allowDecimals={false}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            {...(concurrencyYMax !== null ? { domain: [0, concurrencyYMax] } : {})}
                                            label={{
                                                value: 'requests',
                                                angle: -90,
                                                position: 'insideLeft',
                                                fill: 'var(--text-muted)',
                                                fontSize: 10,
                                            }}
                                        />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<ConcurrencyTooltip />} />
                                        {warmupEndSeconds > 0 && (
                                            <ReferenceLine
                                                x={warmupEndSeconds}
                                                stroke="var(--text-muted)"
                                                strokeDasharray="4 4"
                                                label={warmupLabel}
                                            />
                                        )}
                                        {hasGraceDrain && (
                                            <ReferenceLine
                                                x={dispatchDurationSeconds}
                                                stroke="var(--text-muted)"
                                                strokeDasharray="4 3"
                                                label={drainLabel}
                                            />
                                        )}
                                        {configuredConcurrency !== null && (
                                            <ReferenceLine
                                                y={configuredConcurrency}
                                                stroke={CHART_SERIES[2]}
                                                strokeDasharray="6 4"
                                                strokeWidth={1.5}
                                                ifOverflow="extendDomain"
                                                label={{
                                                    value: `Configured: ${formatCount(configuredConcurrency)}`,
                                                    position: 'insideTopRight',
                                                    fill: CHART_SERIES[2],
                                                    fontSize: 10,
                                                    fontWeight: 600,
                                                }}
                                            />
                                        )}
                                        {hasConcurrencyClient && (
                                            <Line type="monotone" dataKey="client_inflight" name="Client In-Flight Requests" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(timeSeries, 'client_inflight', CHART_SERIES[0])} isAnimationActive={false} />
                                        )}
                                        {hasConcurrencyServer && (
                                            <Line type="monotone" dataKey="server_running" name="vLLM Running Requests" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(timeSeries, 'server_running', CHART_SERIES[1])} isAnimationActive={false} />
                                        )}
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            {concurrencyLegendEntries.length >= 2 && (
                                <ChartLegend
                                    className="mt-3"
                                    entries={concurrencyLegendEntries}
                                />
                            )}
                        </>
                    ) : (
                        <EmptyState title="Concurrency unavailable" message="In-flight request concurrency was not recorded for this run." />
                    )}
                </ChartContainer>

                {/* Chart 4: Warmup vs Steady-State Latency */}
                <ChartContainer
                    title="Warmup vs Steady-State Latency"
                    subtitle={`${coldStartSourceLabel} · ${activeColdMetricMeta.label} (${activeColdMetricMeta.unit}) across Min, P50, Mean, P90, P99, Max`}
                    help={`Compares ${activeColdMetricMeta.label} Min, P50, Mean, P90, P99, and Max (${activeColdMetricMeta.unit}) during the initial warmup phase (or auto 10% window) against steady-state serving. Large ratios point to JIT compilation, cold prefix caches, or autoscaling lag.`}
                    helpFormula={`${activeColdMetricMeta.label}_Stats = {Min, P50, Mean, P90, P99, Max}(${activeColdMetricMeta.label}_slice);  Warmup_Ratio = Warmup_${activeColdMetricMeta.label}_P50 / Steady_State_${activeColdMetricMeta.label}_P50`}
                    helpAlign="end"
                    actions={(coldToSteadyRatio !== null || availableColdMetrics.length > 1) ? (
                        <div className="flex flex-wrap items-center gap-2">
                            {coldToSteadyRatio !== null && (
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone={coldToSteadyRatio > 1.5 ? 'warning' : 'success'} size="sm">
                                        Warmup / Steady-State: {formatNumber(coldToSteadyRatio, 'x')}
                                    </Badge>
                                    <HelpTooltip
                                        label="Warmup to steady-state ratio help"
                                        align="end"
                                        iconSize={12}
                                        formula={`Warmup_Ratio = Warmup_${activeColdMetricMeta.label}_P50 / Steady_State_${activeColdMetricMeta.label}_P50`}
                                    >
                                        {`Ratio of warmup-phase ${activeColdMetricMeta.label} P50 to steady-state ${activeColdMetricMeta.label} P50. Ratios above 1.5x indicate noticeable initial warmup overhead.`}
                                    </HelpTooltip>
                                </span>
                            )}
                            {availableColdMetrics.length > 1 && (
                                <ToggleGroup
                                    size="xs"
                                    value={activeColdLatencyMode}
                                    onChange={setColdLatencyMode}
                                    options={availableColdMetrics.map((meta) => ({
                                        value: meta.key,
                                        label: meta.label,
                                    }))}
                                />
                            )}
                        </div>
                    ) : null}
                >
                    {coldComparisonBars.length > 0 ? (
                        <div className="space-y-4">
                            <div className="h-44 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={coldComparisonBars} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="metric"
                                            tickFormatter={(v) => String(v).replace(/^(?:TTFT|TPOT|E2E)\s+/i, '')}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                        />
                                        <YAxis
                                            width={56}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: activeColdMetricMeta.unit, angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<ComparisonLatencyTooltip />} />
                                        <Bar dataKey="cold" name="Warmup Phase" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={32} isAnimationActive={false} />
                                        <Bar dataKey="steady" name="Steady-State" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={32} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                entries={[
                                    { label: 'Warmup Phase', color: CHART_SERIES[2] },
                                    { label: 'Steady-State', color: CHART_SERIES[0] },
                                ]}
                            />
                            <div className="grid grid-cols-2 gap-3 text-xs">
                                <div className="rounded-lg border border-theme-border bg-theme-card p-3">
                                    <div className="flex items-center gap-1 font-semibold text-theme-text">
                                        <span>Warmup Phase</span>
                                        <HelpTooltip
                                            label="Warmup phase help"
                                            align="start"
                                            iconSize={12}
                                            formula="Warmup = [0, Warmup_s] if Warmup_s > 0 else First_10%_of_Requests"
                                        >
                                            Initial warmup slice (configured warmup seconds or first 10% of requests) used to measure cold-cache and engine startup latency.
                                        </HelpTooltip>
                                    </div>
                                    <div className="mt-1 text-theme-muted">
                                        Requests: <span className="font-mono text-theme-text">{formatCount(firstFinite(coldWindow?.request_count, coldWindow?.total_requests, coldWindow?.totalRequests))}</span>
                                    </div>
                                    <div className="text-theme-muted">
                                        Duration: <span className="font-mono text-theme-text">{formatDurationSeconds(firstFinite(coldWindow?.duration_seconds, coldWindow?.durationSeconds))}</span>
                                    </div>
                                </div>
                                <div className="rounded-lg border border-theme-border bg-theme-card p-3">
                                    <div className="flex items-center gap-1 font-semibold text-theme-text">
                                        <span>Steady-State Profiling Window</span>
                                        <HelpTooltip
                                            label="Steady-state profiling window help"
                                            align="end"
                                            iconSize={12}
                                            formula="Steady_State = [Warmup_End, Run_End]"
                                        >
                                            Post-warmup profiling slice representing warmed-up engine and prefix-cache behavior under sustained load.
                                        </HelpTooltip>
                                    </div>
                                    <div className="mt-1 text-theme-muted">
                                        Requests: <span className="font-mono text-theme-text">{formatCount(firstFinite(steadyWindow?.request_count, steadyWindow?.total_requests, steadyWindow?.totalRequests))}</span>
                                    </div>
                                    <div className="text-theme-muted">
                                        Duration: <span className="font-mono text-theme-text">{formatDurationSeconds(firstFinite(steadyWindow?.duration_seconds, steadyWindow?.durationSeconds))}</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    ) : (
                        <EmptyState title="Comparison unavailable" message="Insufficient warmup or steady-state samples to compare warmup vs steady-state latency." />
                    )}
                </ChartContainer>
            </div>

            {/* Chart 5: Accelerator Power & Energy */}
            <ChartContainer
                title={`${accelLabel} Power & Energy`}
                subtitle={hwProfile?.display_name ? `${hwProfile.display_name} · Profile-driven telemetry` : 'Profile-driven serving power & trapezoidal energy integration'}
                help={`Average and peak power draw (Watts) per serving pod or ${accelLabel.toLowerCase()} device from cluster telemetry. Large gaps between pods indicate uneven load or P/D stage power profiles.`}
                helpFormula="Avg_Power_pod = mean_t Power_W(t);  Peak_Power_pod = max_t Power_W(t)"
                helpAlign="start"
                actions={(
                    <div className="flex flex-wrap items-center gap-2">
                        {powerInfo.totalEnergyWh !== null && (
                            <span className="inline-flex items-center gap-1">
                                <Badge tone="info" size="sm">Energy: {formatEnergyWh(powerInfo.totalEnergyWh)}</Badge>
                                <HelpTooltip
                                    label="Total energy badge help"
                                    align="end"
                                    iconSize={12}
                                    formula="Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i  (skipping Δt > 3×step)"
                                >
                                    Total {accelLabel.toLowerCase()} energy consumed (Wh) integrated via trapezoidal rule over the serving telemetry window.
                                </HelpTooltip>
                            </span>
                        )}
                        {powerInfo.outputTokPerWh !== null && (
                            <span className="inline-flex items-center gap-1">
                                <Badge tone="success" size="sm">Efficiency: {formatNumber(powerInfo.outputTokPerWh)} tok/Wh</Badge>
                                <HelpTooltip
                                    label="Energy efficiency badge help"
                                    align="end"
                                    iconSize={12}
                                    formula="Efficiency (tok/Wh) = Total_Output_Tokens / Total_Accelerator_Energy_Wh"
                                >
                                    Output tokens generated per Watt-hour (tok/Wh); higher values reflect better throughput per unit of energy.
                                </HelpTooltip>
                            </span>
                        )}
                    </div>
                )}
            >
                {powerInfo.rows.length > 0 ? (
                    <>
                        <div className="h-60 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <BarChart data={powerInfo.rows} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                    <XAxis dataKey="name" tick={AXIS_TICK} stroke="var(--border-color)" />
                                    <YAxis
                                        width={56}
                                        tick={AXIS_TICK}
                                        stroke="var(--border-color)"
                                        label={{ value: 'W', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                    />
                                    <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<PowerTooltip />} />
                                    <Bar dataKey="avg_w" name="Average Power" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                    <Bar dataKey="peak_w" name="Peak Power" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                </BarChart>
                            </ResponsiveContainer>
                        </div>
                        <ChartLegend
                            className="mt-3"
                            entries={[
                                { label: 'Average Power (W)', color: CHART_SERIES[0] },
                                { label: 'Peak Power (W)', color: CHART_SERIES[2] },
                            ]}
                        />
                    </>
                ) : (
                    <EmptyState
                        icon={<Zap className="h-6 w-6" />}
                        title={`${accelLabel} power telemetry unavailable`}
                        message="Per-device power metrics were not reported by the cluster exporter for this run."
                    />
                )}
            </ChartContainer>
        </div>
    );
}

export default OverviewTab;
