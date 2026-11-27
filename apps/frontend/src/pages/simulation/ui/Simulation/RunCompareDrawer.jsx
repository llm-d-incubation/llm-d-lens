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

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    CartesianGrid,
    Line,
    LineChart,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { AlertTriangle, GitCompareArrows } from 'lucide-react';
import {
    Badge,
    ChartContainer,
    ChartLegend,
    ChartTooltip,
    ChartTooltipRow,
    CHART_SERIES,
    EmptyState,
    HelpTooltip,
    Modal,
    Panel,
    Spinner,
    ToggleGroup,
} from '../../../../shared/ui';
import { cn } from '../../../../shared/lib/cn';
import { getTaskTelemetry } from '../../model/client';
import { formatTaskPacing } from './ConfigurationGroup';
import {
    deriveLegacyTelemetryFromTask,
    displayName,
    formatDecimal,
    formatDurationSeconds,
    formatEnergyWh,
    formatMilliseconds,
    formatNumber,
    formatWatts,
    numberAt,
    taskBackend,
    taskDataset,
    verdictBadgeProps,
} from '../../model/presentation';

export const MAX_COMPARE_RUNS = 3;

function maxFinite(values) {
    let best = null;
    for (const raw of values) {
        if (raw === null || raw === undefined || raw === '' || typeof raw === 'boolean') continue;
        const numeric = Number(raw);
        if (Number.isFinite(numeric) && (best === null || numeric > best)) {
            best = numeric;
        }
    }
    return best;
}

function resolvePeakKvCachePct(serverTelemetry, forensics, runManifest) {
    const serverSummary = serverTelemetry?.summary || {};
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
        const perPodMax = maxFinite(perPod.map((pod) => (
            numberAt(pod || {}, [
                'kv_cache_usage_percent.max',
                'kv_cache_usage_percent',
                'gpu_cache_usage_percent.max',
                'gpu_cache_usage_percent',
                'kv_cache_peak_pct',
            ])
        )));
        if (perPodMax !== null) return perPodMax;

        const roleSummaryMax = maxFinite(Object.values(summaryByRole).map((roleStats) => (
            numberAt(roleStats || {}, [
                'kv_cache_usage_percent.max',
                'kv_cache_usage_percent',
                'gpu_cache_usage_percent.max',
                'gpu_cache_usage_percent',
                'kv_cache_peak_pct',
            ])
        )));
        if (roleSummaryMax !== null) return roleSummaryMax;

        const roleSeries = Array.isArray(serverTelemetry?.role_series) ? serverTelemetry.role_series : [];
        const roleSeriesMax = maxFinite(roleSeries.flatMap((pt) => [
            pt?.decode_kv_cache_usage_percent,
            pt?.prefill_kv_cache_usage_percent,
        ]));
        if (roleSeriesMax !== null) return roleSeriesMax;

        const forensicsPeak = numberAt(forensics, ['peak_kv_cache_pct']);
        if (forensicsPeak !== null) return forensicsPeak;
    }

    return numberAt(serverSummary, ['kv_cache_usage_percent.max', 'kv_cache_peak_pct', 'gpu_cache_usage_percent.max'])
        ?? numberAt(serverTelemetry, ['peak_kv_cache_pct'])
        ?? numberAt(forensics, ['peak_kv_cache_pct']);
}

function extractRunMetrics(telemetry, task) {
    const forensics = telemetry?.forensics || {};
    const verdict = forensics?.verdict || {};
    const clientSeries = telemetry?.client_series || {};
    const profiling = clientSeries?.windows?.profiling || clientSeries?.windows?.full || {};
    const serverTelemetry = telemetry?.server_telemetry || {};
    const serverSummary = serverTelemetry?.summary || {};
    const energy = serverTelemetry?.energy || serverTelemetry?.power_summary || serverSummary?.power || {};

    const throughputRps = numberAt(profiling, ['throughput_rps', 'request_throughput.avg'])
        ?? numberAt(verdict, ['throughput_rps']);
    const goodputRps = numberAt(profiling, ['goodput_rps'])
        ?? numberAt(verdict, ['goodput_rps']);
    const outputTokS = numberAt(profiling, ['output_tok_per_s', 'output_tok_s', 'output_token_throughput.avg']);
    const totalRequests = numberAt(profiling, ['request_count', 'total_requests', 'requests_total']);
    const failedRequests = numberAt(profiling, ['failed_requests', 'requests_failed', 'error_count']);
    const errorRatePct = numberAt(profiling, ['error_rate_pct', 'error_rate_percent'])
        ?? (totalRequests !== null && totalRequests > 0 && failedRequests !== null
            ? (failedRequests / totalRequests) * 100
            : null);

    const peakKvPct = resolvePeakKvCachePct(serverTelemetry, forensics, telemetry?.run_manifest);
    const avgPowerW = numberAt(energy, ['accelerator_avg_power_watts', 'avg_watts', 'mean_watts', 'mean'])
        ?? numberAt(serverSummary, ['gpu_power_watts.mean', 'accelerator_power_watts.mean']);
    const peakPowerW = numberAt(energy, ['accelerator_peak_power_watts', 'peak_watts', 'max_watts', 'max'])
        ?? numberAt(serverSummary, ['gpu_power_watts.max', 'accelerator_power_watts.max']);
    const totalEnergyWh = numberAt(energy, ['accelerator_energy_wh', 'energy_wh', 'total_energy_wh'])
        ?? numberAt(serverTelemetry, ['energy_wh']);

    const totalOutputTokens = numberAt(profiling, ['total_output_tokens', 'output_tokens_total'])
        ?? numberAt(task?.result?.summary || {}, ['total_output_tokens']);
    const tokPerWh = numberAt(energy, ['output_tokens_per_wh', 'output_tok_per_wh'])
        ?? (totalEnergyWh && totalEnergyWh > 0 && totalOutputTokens !== null
            ? totalOutputTokens / totalEnergyWh
            : null);

    return {
        verdict,
        attainmentPct: numberAt(verdict, ['attainment_pct']) ?? numberAt(profiling, ['slo_attainment_pct']),
        goodputRps,
        throughputRps,
        outputTokS,
        ttftP50: numberAt(profiling, ['ttft_ms.p50', 'ttft.p50_ms', 'ttft_ms.mean', 'ttft.mean_ms']),
        ttftP90: numberAt(profiling, ['ttft_ms.p90', 'ttft.p90_ms']),
        ttftP99: numberAt(profiling, ['ttft_ms.p99', 'ttft.p99_ms']),
        tpotP50: numberAt(profiling, ['tpot_ms.p50', 'tpot.p50_ms', 'tpot_ms.mean', 'tpot.mean_ms']),
        tpotP90: numberAt(profiling, ['tpot_ms.p90', 'tpot.p90_ms']),
        tpotP99: numberAt(profiling, ['tpot_ms.p99', 'tpot.p99_ms']),
        errorRatePct,
        peakKvPct,
        avgPowerW,
        peakPowerW,
        totalEnergyWh,
        tokPerWh,
    };
}

function extractRunConfig(telemetry, task) {
    const manifest = telemetry?.run_manifest || {};
    const sim = task?.simulation || {};
    const backendOptions = sim.backend_options || task?.backend_options || {};
    const durationSeconds = numberAt(sim, ['duration_seconds'])
        ?? numberAt(manifest, ['replay.duration_seconds', 'simulation.duration_seconds', 'duration_seconds']);
    const gracePeriodSeconds = numberAt(sim, ['grace_period_seconds'])
        ?? numberAt(task || {}, ['grace_period_seconds'])
        ?? numberAt(manifest, ['replay.grace_period_seconds', 'simulation.grace_period_seconds', 'grace_period_seconds']);
    const warmupSeconds = numberAt(backendOptions, ['warmup_seconds'])
        ?? numberAt(manifest, ['replay.warmup_seconds', 'warmup_seconds'])
        ?? numberAt(telemetry?.client_series || {}, ['warmup_seconds']);
    const scaleFactor = numberAt(task?.prompt?.trace || {}, ['synthesis_speedup_ratio'])
        ?? numberAt(task || {}, ['scale_factor'])
        ?? numberAt(manifest, ['replay.scale_factor', 'dataset.scale_factor', 'scale_factor']);
    const backend = manifest.backend || taskBackend(task);
    const dataset = manifest.dataset?.name || taskDataset(task);
    const pacing = formatTaskPacing(task, telemetry);
    return {
        backend: backend && backend !== '—' ? displayName(backend) : '—',
        model: manifest.target?.model_name || task?.model_name || '—',
        dataset: dataset || '—',
        pacing: pacing.label || '—',
        duration: durationSeconds !== null ? formatDurationSeconds(durationSeconds) : '—',
        gracePeriod: gracePeriodSeconds !== null ? formatDurationSeconds(gracePeriodSeconds) : '—',
        warmup: warmupSeconds !== null ? formatDurationSeconds(warmupSeconds) : '—',
        scaleFactor: scaleFactor !== null ? `${formatNumber(scaleFactor)}x` : '—',
    };
}

const CONFIG_COMPARISON_ROWS = [
    {
        key: 'backend',
        label: 'Backend',
        help: 'Traffic generator and replay engine used for the simulation (for example, AIPerf).',
    },
    {
        key: 'model',
        label: 'Model',
        help: 'Target served model identifier sent in inference requests.',
    },
    {
        key: 'dataset',
        label: 'Dataset',
        help: 'Trace or prompt dataset replayed during the run.',
    },
    {
        key: 'pacing',
        label: 'Pacing',
        help: 'Replay pacing mode: Fixed Schedule replays source trace timestamps, while Concurrency (N) maintains N simultaneous in-flight session/request slots.',
        formula: 'Fixed Schedule: t_target = t_trace / Scale_Factor | Concurrency: Active_Slots ≤ N',
    },
    {
        key: 'duration',
        label: 'Duration',
        help: 'Configured active replay duration before the post-send grace drain window.',
        formula: 'Replay_Duration_s = Selected_Trace_Span_s / Scale_Factor',
    },
    {
        key: 'gracePeriod',
        label: 'Grace period',
        help: 'Extra drain window after replay duration ends allowing in-flight requests to finish.',
        formula: 'Effective_Window_s = Run_Duration_s + Grace_Period_s',
    },
    {
        key: 'warmup',
        label: 'Warmup',
        help: 'Initial seconds excluded from steady-state profiling KPIs to let caches and queues stabilize.',
        formula: 'Profiling_Window = [Warmup_s, Run_Duration_s + Grace_Period_s]',
    },
    {
        key: 'scaleFactor',
        label: 'Scale factor',
        help: 'Time-compression multiplier applied to source trace inter-arrival intervals (>1x increases request rate).',
        formula: 'Arrival_Interval = Base_Trace_Δt / Scale_Factor',
    },
];

function pointTimeSeconds(point, baseEpochMs) {
    if (Number.isFinite(Number(point?.t))) return Math.round(Number(point.t));
    if (Number.isFinite(Number(point?.elapsed_seconds))) return Math.round(Number(point.elapsed_seconds));
    if (Number.isFinite(Number(point?.mid_seconds))) return Math.round(Number(point.mid_seconds));
    if (Number.isFinite(Number(point?.end_seconds))) return Math.round(Number(point.end_seconds));
    if (Number.isFinite(Number(point?.time_seconds))) return Math.round(Number(point.time_seconds));
    if (point?.timestamp && baseEpochMs !== null) {
        const ts = Date.parse(point.timestamp);
        if (Number.isFinite(ts)) return Math.max(0, Math.round((ts - baseEpochMs) / 1000));
    }
    return null;
}

function resolveOriginEpochMs(run, points) {
    const clientOrigin = Number(run.telemetry?.client_series?.origin_epoch);
    if (Number.isFinite(clientOrigin) && clientOrigin > 0) return clientOrigin * 1000;
    const windowStart = run.telemetry?.server_telemetry?.window?.start;
    if (windowStart) {
        const parsed = Date.parse(windowStart);
        if (Number.isFinite(parsed)) return parsed;
    }
    const manifestStarted = run.telemetry?.run_manifest?.execution_started_at;
    if (manifestStarted) {
        const parsed = Date.parse(manifestStarted);
        if (Number.isFinite(parsed)) return parsed;
    }
    const taskStarted = run.task?.execution_started_at;
    if (taskStarted) {
        const parsed = Date.parse(taskStarted);
        if (Number.isFinite(parsed)) return parsed;
    }
    const clockStart = Number(run.telemetry?.clock?.start_epoch);
    if (Number.isFinite(clockStart) && clockStart > 0) return clockStart * 1000;
    const firstTs = points.find((pt) => pt?.timestamp)?.timestamp;
    if (firstTs) {
        const parsed = Date.parse(firstTs);
        if (Number.isFinite(parsed)) return parsed;
    }
    return null;
}

function mergeRunSeries(runs, getPoints, getValue) {
    const byTime = new Map();
    runs.forEach((run, idx) => {
        const points = getPoints(run.telemetry) || [];
        const baseTs = resolveOriginEpochMs(run, points);
        points.forEach((point) => {
            const t = pointTimeSeconds(point, baseTs);
            const val = getValue(point);
            if (t === null || val === null || val === undefined || !Number.isFinite(Number(val))) return;
            if (!byTime.has(t)) byTime.set(t, { t });
            byTime.get(t)[`run_${idx}`] = Number(val);
        });
    });
    return [...byTime.values()].sort((a, b) => a.t - b.t);
}

function deltaText(value, baseline, lowerIsBetter) {
    if (value === null || baseline === null || baseline === 0) return null;
    const pct = ((value - baseline) / Math.abs(baseline)) * 100;
    if (!Number.isFinite(pct) || Math.abs(pct) < 0.005) return '0%';
    const improved = lowerIsBetter ? pct < 0 : pct > 0;
    const sign = pct > 0 ? '+' : '';
    return {
        label: `${sign}${formatDecimal(pct)}%`,
        improved,
    };
}

function formatCompareValue(value, unit) {
    const trimmedUnit = String(unit || '').trim();
    if (trimmedUnit === 'ms') {
        return { value: formatMilliseconds(value), unit: '' };
    }
    if (trimmedUnit === 'ms/tok') {
        const formatted = formatMilliseconds(value);
        return { value: formatted === '—' ? '—' : `${formatted}/tok`, unit: '' };
    }
    const formatted = formatNumber(value);
    return { value: formatted, unit: formatted === '—' ? '' : (unit || '') };
}

export function CompareTooltip({ active, payload, label, unit }) {
    if (!active || !payload?.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)}s`} className="max-w-xs">
            {payload.map((entry) => {
                const formatted = formatCompareValue(entry.value, unit);
                return (
                    <ChartTooltipRow
                        key={entry.dataKey}
                        color={entry.color}
                        label={entry.name}
                        value={formatted.value}
                        unit={formatted.unit}
                    />
                );
            })}
        </ChartTooltip>
    );
}

export function OverlaidChart({ title, subtitle, help, helpFormula, helpAlign = 'start', actions, data, runs, unit, domain }) {
    const legendEntries = runs.map((run, idx) => ({
        label: run.label,
        color: CHART_SERIES[idx % CHART_SERIES.length],
    }));
    return (
        <ChartContainer title={title} subtitle={subtitle} help={help} helpFormula={helpFormula} helpAlign={helpAlign} actions={actions} className="min-w-0">
            {data.length > 0 ? (
                <>
                    <div className="h-56 w-full">
                        <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={data} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
                                <CartesianGrid strokeDasharray="3 3" stroke="var(--border-color)" vertical={false} />
                                <XAxis
                                    dataKey="t"
                                    type="number"
                                    domain={['dataMin', 'dataMax']}
                                    tickFormatter={(value) => `${formatNumber(value)}s`}
                                    tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
                                    stroke="var(--border-color)"
                                />
                                <YAxis
                                    width={52}
                                    domain={domain || [0, 'auto']}
                                    tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
                                    stroke="var(--border-color)"
                                    label={{ value: unit.trim(), angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                />
                                <Tooltip cursor={{ stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' }} content={<CompareTooltip unit={unit} />} />
                                {runs.map((run, idx) => (
                                    <Line
                                        key={run.taskId}
                                        type="monotone"
                                        dataKey={`run_${idx}`}
                                        name={run.label}
                                        stroke={CHART_SERIES[idx % CHART_SERIES.length]}
                                        strokeWidth={2}
                                        dot={false}
                                        connectNulls
                                        isAnimationActive={false}
                                    />
                                ))}
                            </LineChart>
                        </ResponsiveContainer>
                    </div>
                    <ChartLegend className="mt-3 max-w-full gap-x-3 gap-y-1.5 [&>span]:min-w-0 [&>span]:max-w-full [&>span]:wrap-anywhere" entries={legendEntries} />
                </>
            ) : (
                <EmptyState
                    title="No comparison series available"
                    message="None of the selected runs have persisted time-series data for this metric."
                />
            )}
        </ChartContainer>
    );
}

const COMPARISON_ROWS = [
    {
        key: 'attainmentPct',
        label: 'SLO Attainment',
        format: (v) => formatNumber(v, '%'),
        lowerIsBetter: false,
        help: 'Percentage of requests meeting all configured latency and error-rate SLOs. Positive deltas (+) indicate improved compliance.',
        formula: 'SLO_Attainment (%) = (Good_Requests / Total_Requests) × 100',
    },
    {
        key: 'goodputRps',
        label: 'Goodput',
        format: (v) => formatNumber(v, ' req/s'),
        lowerIsBetter: false,
        help: 'Rate of successful requests meeting SLO thresholds (req/s) in the steady-state window. Positive deltas (+) mean higher useful capacity.',
        formula: 'Goodput (req/s) = Good_Requests / Effective_Window_s',
    },
    {
        key: 'throughputRps',
        label: 'Request Throughput',
        format: (v) => formatNumber(v, ' req/s'),
        lowerIsBetter: false,
        help: 'Total completed request rate (req/s) across the profiling window. Positive deltas (+) indicate higher request throughput.',
        formula: 'Throughput (req/s) = Total_Window_Requests / Effective_Window_s',
    },
    {
        key: 'outputTokS',
        label: 'Output Throughput',
        format: (v) => formatNumber(v, ' tok/s'),
        lowerIsBetter: false,
        help: 'Generated output tokens per second across completed responses. Positive deltas (+) indicate faster aggregate decode throughput.',
        formula: 'Output_Rate (tok/s) = Σ Output_Tokens / Effective_Window_s',
    },
    {
        key: 'ttftP50',
        label: 'TTFT P50',
        format: formatMilliseconds,
        lowerIsBetter: true,
        help: 'Median Time to First Token (ms) from dispatch to first response chunk. Negative deltas (-) indicate faster responsiveness.',
        formula: 'TTFT_P50 = Quantile_0.50(t_first_token − t_request_start)',
    },
    {
        key: 'ttftP90',
        label: 'TTFT P90',
        format: formatMilliseconds,
        lowerIsBetter: true,
        help: '90th percentile Time to First Token (ms) capturing typical upper-tail prefill/queue delay. Negative deltas (-) are better.',
        formula: 'TTFT_P90 = Quantile_0.90(t_first_token − t_request_start)',
    },
    {
        key: 'ttftP99',
        label: 'TTFT P99',
        format: formatMilliseconds,
        lowerIsBetter: true,
        help: '99th percentile Time to First Token (ms) highlighting worst-case queuing or prefill tail latency. Negative deltas (-) are better.',
        formula: 'TTFT_P99 = Quantile_0.99(t_first_token − t_request_start)',
    },
    {
        key: 'tpotP50',
        label: 'TPOT P50',
        format: (v) => {
            const formatted = formatMilliseconds(v);
            return formatted === '—' ? '—' : `${formatted}/tok`;
        },
        lowerIsBetter: true,
        help: 'Median Time per Output Token (ms/tok) during streaming decode. Negative deltas (-) indicate faster per-token generation.',
        formula: 'TPOT_P50 = Quantile_0.50((t_last_token − t_first_token) / max(1, Output_Tokens − 1))',
    },
    {
        key: 'tpotP90',
        label: 'TPOT P90',
        format: (v) => {
            const formatted = formatMilliseconds(v);
            return formatted === '—' ? '—' : `${formatted}/tok`;
        },
        lowerIsBetter: true,
        help: '90th percentile Time per Output Token (ms/tok) across decode steps. Negative deltas (-) indicate lower decode tail jitter.',
        formula: 'TPOT_P90 = Quantile_0.90((t_last_token − t_first_token) / max(1, Output_Tokens − 1))',
    },
    {
        key: 'tpotP99',
        label: 'TPOT P99',
        format: (v) => {
            const formatted = formatMilliseconds(v);
            return formatted === '—' ? '—' : `${formatted}/tok`;
        },
        lowerIsBetter: true,
        help: '99th percentile Time per Output Token (ms/tok) highlighting severe decode stalls or preemption. Negative deltas (-) are better.',
        formula: 'TPOT_P99 = Quantile_0.99((t_last_token − t_first_token) / max(1, Output_Tokens − 1))',
    },
    {
        key: 'errorRatePct',
        label: 'Error Rate',
        format: (v) => formatNumber(v, '%'),
        lowerIsBetter: true,
        help: 'Share of dispatched requests failing with HTTP 4xx/5xx, timeout, or transport errors (%). Negative deltas (-) indicate fewer errors.',
        formula: 'Error_Rate (%) = (Failed_Requests / Total_Window_Requests) × 100',
    },
    {
        key: 'peakKvPct',
        label: 'Peak KV Cache Usage',
        format: (v) => formatNumber(v, '%'),
        lowerIsBetter: true,
        help: 'Maximum observed server KV cache usage (%) from Prometheus. Negative deltas (-) indicate lower memory pressure and eviction risk.',
        formula: 'Peak_KV_Usage (%) = max_t((Used_KV_Blocks / Total_KV_Blocks) × 100)',
    },
    {
        key: 'avgPowerW',
        label: 'Avg Serving Power',
        format: formatWatts,
        lowerIsBetter: true,
        help: 'Mean serving accelerator power draw in watts across the profiled window. Negative deltas (-) indicate lower power consumption.',
        formula: 'Avg_Power (W) = mean_t(Σ_devices Instantaneous_Power_W)',
    },
    {
        key: 'peakPowerW',
        label: 'Peak Serving Power',
        format: formatWatts,
        lowerIsBetter: true,
        help: 'Maximum observed accelerator power draw in watts during the run. Negative deltas (-) indicate lower peak power demand.',
        formula: 'Peak_Power (W) = max_t(Σ_devices Instantaneous_Power_W)',
    },
    {
        key: 'totalEnergyWh',
        label: 'Total Energy',
        format: formatEnergyWh,
        lowerIsBetter: true,
        help: 'Cumulative serving energy consumed in watt-hours (Wh) over the run. Negative deltas (-) indicate lower total energy use.',
        formula: 'Energy (Wh) = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i',
    },
    {
        key: 'tokPerWh',
        label: 'Energy Efficiency',
        format: (v) => formatNumber(v, ' tok/Wh'),
        lowerIsBetter: false,
        help: 'Generated output tokens per watt-hour of accelerator energy (tok/Wh). Positive deltas (+) indicate better performance per watt.',
        formula: 'Efficiency (tok/Wh) = Total_Output_Tokens / Total_Accelerator_Energy_Wh',
    },
];

export function RunCompareDrawer({ isOpen, onClose, taskIds = [], tasksById = {}, onPruneCompareIds }) {
    const [fetchState, setFetchState] = useState({ key: '', telemetriesById: {}, missingIds: [], error: '' });
    const [ttftPercentile, setTtftPercentile] = useState('p90');
    const [tpotPercentile, setTpotPercentile] = useState('p90');
    const onPruneCompareIdsRef = useRef(onPruneCompareIds);
    useEffect(() => {
        onPruneCompareIdsRef.current = onPruneCompareIds;
    }, [onPruneCompareIds]);

    const normalizedIds = useMemo(
        () => (Array.isArray(taskIds)
            ? [...new Set(taskIds.map((id) => (typeof id === 'string' ? id.trim() : id)).filter(Boolean))].slice(0, MAX_COMPARE_RUNS)
            : []),
        [taskIds],
    );
    const requestKey = isOpen && normalizedIds.length >= 2 ? normalizedIds.join(',') : '';

    useEffect(() => {
        if (!requestKey) return undefined;
        const controller = new AbortController();
        Promise.allSettled(
            normalizedIds.map((taskId) => getTaskTelemetry(taskId, { section: 'all' }, { signal: controller.signal })),
        )
            .then((settled) => {
                if (controller.signal.aborted) return;
                const telemetriesById = {};
                const missingIds = [];
                const failures = [];
                settled.forEach((result, idx) => {
                    const taskId = normalizedIds[idx];
                    if (result.status === 'fulfilled') {
                        telemetriesById[taskId] = result.value;
                    } else if (result.reason?.status === 404) {
                        missingIds.push(taskId);
                    } else if (result.reason?.name !== 'AbortError') {
                        failures.push(result.reason?.message || 'Request failed');
                    }
                });
                const errorMsg = failures.length > 0 && Object.keys(telemetriesById).length === 0
                    ? `Unable to load comparison telemetry: ${failures[0]}`
                    : (failures.length > 0 ? `Some runs could not be loaded: ${failures[0]}` : '');
                setFetchState({
                    key: requestKey,
                    telemetriesById,
                    missingIds,
                    error: errorMsg,
                });
                if (missingIds.length > 0) {
                    onPruneCompareIdsRef.current?.(missingIds);
                }
            });
        return () => controller.abort();
    }, [normalizedIds, requestKey]);

    const loading = Boolean(requestKey) && fetchState.key !== requestKey;
    const error = fetchState.key === requestKey ? fetchState.error : '';
    const missingIds = useMemo(
        () => (fetchState.key === requestKey ? fetchState.missingIds : []),
        [fetchState.key, fetchState.missingIds, requestKey],
    );
    const telemetriesById = useMemo(
        () => (fetchState.key === requestKey ? fetchState.telemetriesById : {}),
        [fetchState.key, fetchState.telemetriesById, requestKey],
    );
    const activeIds = useMemo(
        () => normalizedIds.filter((id) => !missingIds.includes(id)),
        [missingIds, normalizedIds],
    );

    const runs = useMemo(() => activeIds.map((taskId, idx) => {
        const task = tasksById[taskId] || null;
        const telemetry = telemetriesById[taskId] || (task ? deriveLegacyTelemetryFromTask(task) : {});
        const label = task?.name || telemetry?.run_manifest?.name || telemetry?.run_manifest?.task_name || `Run ${idx + 1} (${taskId})`;
        return {
            taskId,
            task,
            telemetry,
            label,
            metrics: extractRunMetrics(telemetry, task),
            config: extractRunConfig(telemetry, task),
        };
    }), [activeIds, tasksById, telemetriesById]);

    const throughputSeries = useMemo(() => mergeRunSeries(
        runs,
        (tel) => tel?.client_series?.buckets || tel?.client_series?.series?.throughput || tel?.client_series?.throughput_timeline,
        (pt) => pt.throughput_rps ?? pt.rps ?? pt.request_completion_rps ?? pt.request_throughput_rps,
    ), [runs]);

    const ttftSeries = useMemo(() => mergeRunSeries(
        runs,
        (tel) => tel?.client_series?.buckets || tel?.client_series?.series?.ttft || tel?.client_series?.series?.latency || tel?.client_series?.ttft_timeline,
        (pt) => (ttftPercentile === 'p50'
            ? (pt.ttft?.p50 ?? pt.ttft_p50_ms ?? pt.p50_ms ?? pt.average_ms)
            : (pt.ttft?.p90 ?? pt.ttft_p90_ms ?? pt.p90_ms ?? pt.p95_ms)),
    ), [runs, ttftPercentile]);

    const tpotSeries = useMemo(() => mergeRunSeries(
        runs,
        (tel) => tel?.client_series?.buckets || tel?.client_series?.series?.tpot || tel?.client_series?.series?.latency || tel?.client_series?.tpot_timeline,
        (pt) => (tpotPercentile === 'p50'
            ? (pt.tpot?.p50 ?? pt.tpot_p50_ms ?? pt.p50_ms ?? pt.average_ms)
            : (pt.tpot?.p90 ?? pt.tpot_p90_ms ?? pt.p90_ms ?? pt.p95_ms)),
    ), [runs, tpotPercentile]);

    const kvSeries = useMemo(() => mergeRunSeries(
        runs,
        (tel) => {
            const st = tel?.server_telemetry;
            const isDisagg = String(st?.topology_mode || tel?.run_manifest?.target?.topology_mode || '').toLowerCase() === 'disaggregated';
            if (isDisagg && Array.isArray(st?.role_series) && st.role_series.length > 0) {
                return st.role_series;
            }
            return st?.series;
        },
        (pt) => {
            const rolePeak = maxFinite([pt?.decode_kv_cache_usage_percent, pt?.prefill_kv_cache_usage_percent]);
            if (rolePeak !== null) return rolePeak;
            return pt?.kv_cache_usage_percent ?? pt?.kv_cache_peak_pct ?? pt?.gpu_cache_usage_percent ?? pt?.gpu_cache_usage_perc;
        },
    ), [runs]);

    return (
        <Modal
            isOpen={isOpen}
            onClose={onClose}
            variant="drawer"
            size="xl"
            title="Compare Simulation Runs"
            subtitle={`Side-by-side comparison across ${runs.length} runs (Run 1 is baseline)`}
        >
            <div className="min-w-0 max-w-full space-y-6">
                {missingIds.length > 0 && (
                    <div role="status" className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>Excluded deleted or unavailable run(s): {missingIds.join(', ')}</span>
                    </div>
                )}

                {error && (
                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-700 dark:text-red-300">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>{error}</span>
                    </div>
                )}

                {runs.length < 2 ? (
                    <EmptyState
                        icon={<GitCompareArrows className="h-8 w-8" />}
                        title="Select at least 2 runs"
                        message="Choose 2 or 3 completed, failed, or cancelled runs to compare side by side."
                    />
                ) : loading && Object.keys(telemetriesById).length === 0 ? (
                    <div className="flex items-center justify-center gap-2 py-16 text-sm text-theme-muted">
                        <Spinner /> Loading run telemetry…
                    </div>
                ) : (
                    <>
                        <Panel
                            title="KPI Comparison Matrix"
                            help="Side-by-side steady-state KPIs across selected runs. Run 1 is the baseline; percentage deltas on other runs are green for improvements and red for regressions."
                            helpFormula="Δ = Candidate − Baseline;  %Δ = ((Candidate − Baseline) / |Baseline|) × 100"
                            padding="none"
                        >
                            <div className="max-w-full overflow-x-auto lg:overflow-visible">
                                <table className="w-full min-w-[560px] lg:min-w-0 lg:table-fixed text-left text-xs">
                                    <colgroup>
                                        <col className="w-40 sm:w-44" />
                                        {runs.map((run) => (
                                            <col key={run.taskId} />
                                        ))}
                                    </colgroup>
                                    <thead className="border-b border-theme-border bg-theme-bg text-[10px] font-bold uppercase tracking-wider text-theme-muted">
                                        <tr>
                                            <th className="px-3 py-2.5">
                                                <span className="inline-flex items-center gap-1">
                                                    <span>Metric</span>
                                                    <HelpTooltip label="Metric column help" align="start" iconSize={12}>
                                                        Performance and efficiency KPIs extracted from each run&apos;s steady-state profiling telemetry.
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                            {runs.map((run, idx) => (
                                                <th key={run.taskId} className="px-3 py-2.5">
                                                    <div className="flex min-w-0 items-center gap-1.5">
                                                        <span
                                                            className="h-2.5 w-2.5 shrink-0 rounded-full"
                                                            style={{ backgroundColor: CHART_SERIES[idx % CHART_SERIES.length] }}
                                                            aria-hidden="true"
                                                        />
                                                        <span className="min-w-0 flex-1 truncate font-semibold text-theme-text" title={run.label}>{run.label}</span>
                                                        <HelpTooltip
                                                            label={`${run.label} delta column help`}
                                                            align={idx === 0 ? 'start' : 'end'}
                                                            iconSize={12}
                                                            formula={idx === 0 ? 'Baseline_Reference = Run_1_Metric' : '%Δ = ((Candidate − Baseline) / |Baseline|) × 100'}
                                                        >
                                                            {idx === 0
                                                                ? 'Baseline reference run. All percentage deltas in subsequent columns are computed relative to this run.'
                                                                : 'Displays measured values alongside percentage delta vs. Run 1 baseline; green indicates improvement and red indicates regression.'}
                                                        </HelpTooltip>
                                                    </div>
                                                    <div
                                                        className="mt-0.5 truncate font-mono text-[10px] font-normal text-theme-muted"
                                                        title={idx === 0 ? `${run.taskId} · Baseline` : run.taskId}
                                                    >
                                                        {idx === 0 ? `${run.taskId} · Baseline` : run.taskId}
                                                    </div>
                                                </th>
                                            ))}
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-theme-border">
                                        <tr>
                                            <td className="px-3 py-2.5 font-medium text-theme-muted">
                                                <span className="inline-flex items-center gap-1">
                                                    <span>Verdict</span>
                                                    <HelpTooltip label="Verdict help" align="start" iconSize={12} formula="Pass iff TTFT, TPOT, E2E & Error_Rate ≤ SLOs; Bottleneck from Stage & Saturation Rules">
                                                        Automated bottleneck classification derived from forensics telemetry (e.g., Healthy, Prefill Compute, Decode Bound, Queue / Admission, or KV Eviction).
                                                    </HelpTooltip>
                                                </span>
                                            </td>
                                            {runs.map((run) => {
                                                const badge = verdictBadgeProps(run.metrics.verdict);
                                                return (
                                                    <td key={run.taskId} className="px-3 py-2.5">
                                                        <Badge tone={badge.tone} size="xs" className="max-w-full" title={badge.label}>
                                                            <span className="min-w-0 truncate">{badge.label}</span>
                                                        </Badge>
                                                    </td>
                                                );
                                            })}
                                        </tr>
                                        {COMPARISON_ROWS.map((row) => {
                                            const numericValues = runs.map((run) => run.metrics[row.key]);
                                            const validValues = numericValues.filter((v) => v !== null && v !== undefined && Number.isFinite(v));
                                            const bestValue = validValues.length > 1
                                                ? (row.lowerIsBetter ? Math.min(...validValues) : Math.max(...validValues))
                                                : null;
                                            const baseline = numericValues[0] ?? null;

                                            return (
                                                <tr key={row.key} className="hover:bg-theme-bg/40">
                                                    <td className="px-3 py-2.5 font-medium text-theme-muted">
                                                        <span className="inline-flex items-center gap-1">
                                                            <span>{row.label}</span>
                                                            <HelpTooltip label={`${row.label} help`} align="start" iconSize={12} formula={row.formula}>
                                                                {row.help}
                                                            </HelpTooltip>
                                                        </span>
                                                    </td>
                                                    {runs.map((run, idx) => {
                                                        const value = numericValues[idx];
                                                        const isBest = bestValue !== null && value === bestValue;
                                                        const delta = idx > 0 ? deltaText(value, baseline, row.lowerIsBetter) : null;
                                                        return (
                                                            <td key={run.taskId} className="px-3 py-2.5 font-mono">
                                                                <div className="flex min-w-0 flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
                                                                    <span className={cn('font-semibold text-theme-text', isBest && 'text-emerald-600 dark:text-emerald-300')}>
                                                                        {row.format(value)}
                                                                    </span>
                                                                    {isBest && (
                                                                        <Badge tone="success" size="xxs">Best</Badge>
                                                                    )}
                                                                    {delta && typeof delta === 'object' && (
                                                                        <span
                                                                            className={cn(
                                                                                'text-[10px]',
                                                                                delta.improved
                                                                                    ? 'text-emerald-600 dark:text-emerald-400'
                                                                                    : 'text-red-600 dark:text-red-400',
                                                                            )}
                                                                        >
                                                                            {delta.label}
                                                                        </span>
                                                                    )}
                                                                </div>
                                                            </td>
                                                        );
                                                    })}
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        </Panel>

                        <Panel
                            title="Configuration Comparison"
                            help="Side-by-side workload, target, and timing configuration so differences in test conditions are visible before comparing metrics."
                            padding="none"
                        >
                            <div className="max-w-full overflow-x-auto lg:overflow-visible">
                                <table className="w-full min-w-[560px] lg:min-w-0 lg:table-fixed text-left text-xs">
                                    <colgroup>
                                        <col className="w-40 sm:w-44" />
                                        {runs.map((run) => (
                                            <col key={run.taskId} />
                                        ))}
                                    </colgroup>
                                    <thead className="border-b border-theme-border bg-theme-bg text-[10px] font-bold uppercase tracking-wider text-theme-muted">
                                        <tr>
                                            <th className="px-3 py-2.5">
                                                <span className="inline-flex items-center gap-1">
                                                    <span>Parameter</span>
                                                    <HelpTooltip label="Parameter column help" align="start" iconSize={12}>
                                                        Key replay configuration settings from each run&apos;s manifest or task specification.
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                            {runs.map((run) => (
                                                <th key={run.taskId} className="px-3 py-2.5 font-semibold text-theme-text">
                                                    <div className="min-w-0">
                                                        <div className="truncate font-semibold text-theme-text" title={run.label}>
                                                            {run.label}
                                                        </div>
                                                    </div>
                                                </th>
                                            ))}
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-theme-border">
                                        {CONFIG_COMPARISON_ROWS.map((row) => (
                                            <tr key={row.key} className="hover:bg-theme-bg/40">
                                                <td className="px-3 py-2.5 font-medium text-theme-muted">
                                                    <span className="inline-flex items-center gap-1">
                                                        <span>{row.label}</span>
                                                        <HelpTooltip label={`${row.label} help`} align="start" iconSize={12} formula={row.formula}>
                                                            {row.help}
                                                        </HelpTooltip>
                                                    </span>
                                                </td>
                                                {runs.map((run) => (
                                                    <td
                                                        key={run.taskId}
                                                        className="px-3 py-2.5 font-mono break-words text-theme-text"
                                                        title={String(run.config[row.key] ?? '')}
                                                    >
                                                        {run.config[row.key]}
                                                    </td>
                                                ))}
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </Panel>

                        <div className="grid min-w-0 gap-5 lg:grid-cols-2">
                            <OverlaidChart
                                title="Throughput Comparison"
                                subtitle="Request throughput (req/s) across runs"
                                help="Overlays completed request throughput (req/s) over elapsed run seconds across selected runs. Higher sustained lines indicate higher serving capacity."
                                helpFormula="Throughput(t) = Completed_Requests_in_Bin / Bin_Width_s"
                                helpAlign="start"
                                data={throughputSeries}
                                runs={runs}
                                unit=" req/s"
                            />
                            <OverlaidChart
                                title="TTFT Comparison"
                                subtitle="Time to First Token (TTFT, ms) over elapsed run time"
                                help="Overlays Time to First Token (ms) at P50 or P90 over elapsed run seconds. Lower curves indicate faster prefill and lower admission queuing."
                                helpFormula="TTFT_Pq(t) = Quantile_q(t_first_token − t_request_start) in Bin"
                                helpAlign="end"
                                actions={(
                                    <ToggleGroup
                                        size="xs"
                                        value={ttftPercentile}
                                        onChange={setTtftPercentile}
                                        options={[
                                            { value: 'p50', label: 'P50' },
                                            { value: 'p90', label: 'P90' },
                                        ]}
                                    />
                                )}
                                data={ttftSeries}
                                runs={runs}
                                unit=" ms"
                            />
                            <OverlaidChart
                                title="TPOT Comparison"
                                subtitle="Time per Output Token (TPOT, ms/tok) over elapsed run time"
                                help="Overlays Time per Output Token (ms/tok) at P50 or P90 over elapsed run seconds. Lower, flatter curves indicate steadier decode speed."
                                helpFormula="TPOT_Pq(t) = Quantile_q((t_last_token − t_first_token) / max(1, Output_Tokens − 1)) in Bin"
                                helpAlign="start"
                                actions={(
                                    <ToggleGroup
                                        size="xs"
                                        value={tpotPercentile}
                                        onChange={setTpotPercentile}
                                        options={[
                                            { value: 'p50', label: 'P50' },
                                            { value: 'p90', label: 'P90' },
                                        ]}
                                    />
                                )}
                                data={tpotSeries}
                                runs={runs}
                                unit=" ms/tok"
                            />
                            <OverlaidChart
                                title="KV Cache Usage"
                                subtitle="Peak server KV cache usage (%)"
                                help="Overlays server KV cache usage (%) over elapsed run seconds from Prometheus telemetry. Curves approaching 90–100% signal memory pressure and potential eviction."
                                helpFormula="Peak_KV_Usage(t) (%) = max_pods((Used_KV_Blocks / Total_KV_Blocks) × 100)"
                                helpAlign="end"
                                data={kvSeries}
                                runs={runs}
                                unit="%"
                                domain={[0, 100]}
                            />
                        </div>
                    </>
                )}
            </div>
        </Modal>
    );
}

export default RunCompareDrawer;
