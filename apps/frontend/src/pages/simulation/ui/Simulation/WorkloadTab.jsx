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
    BarChart3,
    Gauge,
    GitBranch,
    Layers,
    MessageSquare,
    SlidersHorizontal,
} from 'lucide-react';
import {
    Area,
    AreaChart,
    Bar,
    BarChart,
    CartesianGrid,
    Line,
    LineChart,
    ReferenceArea,
    ReferenceLine,
    ResponsiveContainer,
    Scatter,
    ScatterChart,
    Tooltip,
    XAxis,
    YAxis,
    ZAxis,
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
    clampPercent,
    formatCount,
    formatDecimal,
    formatNumber,
    numberAt,
    scenarioDisplayName,
    taskDataset,
} from '../../model/presentation';
import { formatTaskPacing } from './ConfigurationGroup';

const AXIS_TICK = { fill: 'var(--text-muted)', fontSize: 10 };
const BAR_TOOLTIP_CURSOR = { fill: 'var(--chart-cursor-fill, rgba(148, 163, 184, 0.10))' };
const LINE_TOOLTIP_CURSOR = { stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' };
const EMPTY_OBJ = {};
const EMPTY_ARR = [];
const WORKLOAD_CATEGORIES = [
    { id: 'all', label: 'All' },
    { id: 'kpis', label: 'Summary' },
    { id: 'isl', label: 'Input Sequence Length' },
    { id: 'osl', label: 'Output Sequence Length' },
    { id: 'joint', label: 'ISL vs OSL' },
    { id: 'conc', label: 'Concurrency & Arrival' },
    { id: 'multiturn', label: 'Multi-Turn & Prefix' },
];

function finiteNum(value) {
    if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'object') {
        return null;
    }
    if (typeof value === 'string' && value.trim() === '') return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? (numeric === 0 ? 0 : numeric) : null;
}

function fmtPct(value) {
    const clamped = clampPercent(value);
    return clamped === null ? '—' : `${formatDecimal(clamped)}%`;
}

function fmtRatio(value) {
    const numeric = finiteNum(value);
    return numeric === null ? '—' : `${formatDecimal(numeric)}×`;
}

function fmtRps(value) {
    const numeric = finiteNum(value);
    return numeric === null ? '—' : `${formatDecimal(numeric)} req/s`;
}

function fmtTok(value) {
    const numeric = finiteNum(value);
    return numeric === null ? '—' : formatNumber(numeric, ' tok');
}

function fmtExactTok(value) {
    const numeric = finiteNum(value);
    return numeric === null ? '—' : `${formatDecimal(numeric)} tok`;
}

function fmtSec(value) {
    const numeric = finiteNum(value);
    return numeric === null ? '—' : `${formatDecimal(numeric)} s`;
}

function fmtStatPair(left, right, suffix = '') {
    const a = finiteNum(left);
    const b = finiteNum(right);
    if (a === null && b === null) return '—';
    const leftText = a === null ? '—' : formatDecimal(a);
    const rightText = b === null ? '—' : formatDecimal(b);
    return `${leftText} / ${rightText}${suffix}`;
}

function fmtRatioPair(left, right) {
    const a = finiteNum(left);
    const b = finiteNum(right);
    if (a === null && b === null) return '—';
    return `${fmtRatio(a)} / ${fmtRatio(b)}`;
}

function fmtReqCount(value) {
    const numeric = finiteNum(value);
    return numeric === null ? '—' : `${formatCount(numeric)} req`;
}

function fmtReqDecimal(value) {
    const numeric = finiteNum(value);
    return numeric === null ? '—' : `${formatDecimal(numeric)} req`;
}

function fmtRegimeShare(regime, sampleCount = null) {
    if (!regime || typeof regime !== 'object') return '—';
    const samples = finiteNum(sampleCount);
    if (samples === 0) return '—';
    const share = finiteNum(regime.share_pct);
    if (share === null) return '—';
    const count = finiteNum(regime.count);
    return count === null ? fmtPct(share) : `${fmtPct(share)} (${formatCount(count)})`;
}

/**
 * Computes a degenerate-safe [min, max] numeric domain for Recharts axes.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function computeNumericDomain(points, key = 'value', minFloor = null) {
    const values = (Array.isArray(points) ? points : [])
        .map((point) => finiteNum(point?.[key]))
        .filter((val) => val !== null);
    if (!values.length) return [0, 1];
    let lo = Math.min(...values);
    if (minFloor !== null && Number.isFinite(minFloor)) {
        lo = Math.min(lo, minFloor);
    }
    const hi = Math.max(...values);
    return lo === hi ? [Math.max(0, lo - 1), hi + 1] : [lo, hi];
}

/**
 * Classifies per-request (isl, osl) pairs into prefill-heavy, balanced, and
 * decode-heavy regimes while safely handling zero input sequence lengths.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function classifyIslOslPoints(rawPoints) {
    const prefillHeavy = [];
    const balanced = [];
    const decodeHeavy = [];
    const allPoints = [];

    for (const pt of (Array.isArray(rawPoints) ? rawPoints : EMPTY_ARR)) {
        const isl = finiteNum(pt?.isl);
        const osl = finiteNum(pt?.osl);
        if (isl === null || osl === null || isl < 0 || osl < 0) continue;

        let ratio = null;
        let regime = 'balanced';
        let regimeLabel = 'Balanced (0.25–1.0×)';

        if (isl === 0) {
            if (osl > 0) {
                ratio = null;
                regime = 'decode_heavy';
                regimeLabel = 'Decode-Heavy (>1.0×)';
            } else {
                ratio = 0;
                regime = 'balanced';
                regimeLabel = 'Balanced (0.25–1.0×)';
            }
        } else {
            ratio = osl / isl;
            if (ratio < 0.25) {
                regime = 'prefill_heavy';
                regimeLabel = 'Prefill-Heavy (<0.25×)';
            } else if (ratio <= 1.0) {
                regime = 'balanced';
                regimeLabel = 'Balanced (0.25–1.0×)';
            } else {
                regime = 'decode_heavy';
                regimeLabel = 'Decode-Heavy (>1.0×)';
            }
        }

        const item = {
            ...pt,
            isl,
            osl,
            ratio,
            regime,
            regimeLabel,
        };
        allPoints.push(item);
        if (regime === 'prefill_heavy') {
            prefillHeavy.push(item);
        } else if (regime === 'decode_heavy') {
            decodeHeavy.push(item);
        } else {
            balanced.push(item);
        }
    }

    return {
        allPoints,
        prefillHeavy,
        balanced,
        decodeHeavy,
    };
}

function sparseDotProp(data, dataKey, color) {
    if (!Array.isArray(data) || !data.length) return false;
    const isFiniteAt = (idx) => idx >= 0 && idx < data.length && finiteNum(data[idx]?.[dataKey]) !== null;
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

function normalizeTokenDist(raw) {
    if (!raw || typeof raw !== 'object') return EMPTY_OBJ;
    return {
        min: finiteNum(raw.min),
        p50: finiteNum(raw.p50),
        mean: finiteNum(raw.mean ?? raw.avg),
        p90: finiteNum(raw.p90),
        p99: finiteNum(raw.p99),
        max: finiteNum(raw.max),
        cv: finiteNum(raw.cv),
    };
}

/**
 * Resolves the active window's workload payload, falling back cleanly from
 * legacy summary windows and buckets when `client_series.workload` is absent.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function resolveWorkloadWindowData(task, telemetry, analysisWindow = 'profiling') {
    const clientSeries = telemetry?.client_series ?? EMPTY_OBJ;
    const sessionsSummary = telemetry?.sessions_summary ?? EMPTY_OBJ;
    const winKey = analysisWindow === 'full' ? 'full' : 'profiling';
    const workload = clientSeries?.workload;

    if (workload && typeof workload === 'object' && workload.windows && typeof workload.windows === 'object') {
        const windowData = workload.windows[winKey] || workload.windows.full || workload.windows.profiling || null;
        if (!windowData) return null;
        return {
            winKey,
            windowData,
            fallbackToFull: winKey === 'profiling' && Boolean(workload.fallback_to_full),
            fallbackSynthesized: Boolean(workload.fallback_synthesized),
            isClientFallback: false,
        };
    }

    const csWindows = clientSeries?.windows ?? EMPTY_OBJ;
    const csWin = csWindows[winKey] ?? csWindows.full ?? csWindows.profiling ?? clientSeries?.summary ?? task?.result?.summary ?? null;
    const buckets = Array.isArray(clientSeries?.buckets) ? clientSeries.buckets : EMPTY_ARR;
    const warmupSeconds = finiteNum(clientSeries?.warmup_seconds)
        ?? finiteNum(task?.simulation?.backend_options?.warmup_seconds)
        ?? 0;
    const totalDuration = finiteNum(clientSeries?.duration_seconds)
        ?? finiteNum(csWindows?.full?.duration_seconds)
        ?? finiteNum(csWin?.duration_seconds);
    const fallbackToFull = winKey === 'profiling' && (warmupSeconds <= 0 || (totalDuration !== null && totalDuration <= warmupSeconds));
    const winBuckets = (winKey === 'profiling' && !fallbackToFull && warmupSeconds > 0)
        ? buckets.filter((b) => !b?.is_warmup && (finiteNum(b?.end_seconds ?? b?.time_seconds ?? b?.t) ?? 0) > warmupSeconds)
        : buckets;

    const total = numberAt(csWin, ['request_count', 'total_requests', 'requests_total']);
    const completed = numberAt(csWin, ['successful_requests', 'requests_success', 'success_count']);
    const failed = numberAt(csWin, ['failed_requests', 'requests_failed', 'error_count'])
        ?? (total !== null && completed !== null ? Math.max(0, total - completed) : null);

    const isl = normalizeTokenDist(csWin?.input_tokens);
    const osl = normalizeTokenDist(csWin?.output_tokens);
    const timeline = winBuckets
        .map((b, idx) => {
            const t = finiteNum(b?.mid_seconds ?? b?.time_seconds ?? b?.t);
            if (t === null) return null;
            return {
                t: t ?? idx,
                start_seconds: finiteNum(b?.start_seconds),
                end_seconds: finiteNum(b?.end_seconds),
                is_warmup: Boolean(b?.is_warmup || (warmupSeconds > 0 && (finiteNum(b?.end_seconds ?? t) ?? 0) <= warmupSeconds)),
                in_flight_avg: finiteNum(b?.in_flight_avg ?? b?.in_flight_concurrency ?? b?.client_inflight),
                in_flight_max: finiteNum(b?.in_flight_max ?? b?.in_flight_concurrency ?? b?.client_inflight),
                arrival_count: finiteNum(b?.arrived_requests),
                arrival_rps: finiteNum(b?.arrived_rps ?? b?.actual_send_rps ?? b?.target_arrival_rps),
                completed_count: finiteNum(b?.completed_requests),
                completed_rps: finiteNum(b?.completed_rps ?? b?.throughput_rps),
            };
        })
        .filter(Boolean);

    const avgConcVals = timeline.map((p) => p.in_flight_avg).filter((v) => v !== null);
    const maxConcVals = timeline.map((p) => p.in_flight_max).filter((v) => v !== null);
    const arrRateVals = timeline.map((p) => p.arrival_rps).filter((v) => v !== null);
    const winDur = finiteNum(csWin?.duration_seconds) ?? totalDuration;
    const meanArrival = arrRateVals.length > 0
        ? arrRateVals.reduce((acc, v) => acc + v, 0) / arrRateVals.length
        : numberAt(csWin, ['throughput_rps', 'requests_per_second']);
    const peakArrival = arrRateVals.length > 0 ? Math.max(...arrRateVals) : null;

    const rawPromptBuckets = Array.isArray(clientSeries?.prompt_length_buckets)
        ? clientSeries.prompt_length_buckets
        : (Array.isArray(clientSeries?.prompt_buckets) ? clientSeries.prompt_buckets : EMPTY_ARR);
    const totalBucketCount = rawPromptBuckets.reduce((acc, b) => acc + (finiteNum(b?.count) ?? 0), 0);
    const islVsOslBuckets = rawPromptBuckets
        .filter((b) => (finiteNum(b?.count) ?? 0) > 0)
        .map((b) => {
            const count = finiteNum(b?.count) ?? 0;
            return {
                label: b.label ?? b.bucket ?? 'Unknown',
                count,
                share_pct: finiteNum(b?.share_pct) ?? (totalBucketCount > 0 ? (count / totalBucketCount) * 100 : null),
                osl_p50: finiteNum(b?.osl_p50),
                osl_mean: finiteNum(b?.osl_mean),
                osl_p90: finiteNum(b?.osl_p90),
                ratio_p50: finiteNum(b?.ratio_p50),
                ratio_mean: finiteNum(b?.ratio_mean),
            };
        });

    const hasIsl = Object.values(isl).some((v) => v !== null);
    const hasOsl = Object.values(osl).some((v) => v !== null);
    if (total === null && timeline.length === 0 && !hasIsl && !hasOsl) {
        return null;
    }

    const turnDistRaw = (winKey === 'full' || fallbackToFull) && Array.isArray(sessionsSummary?.turn_distribution)
        ? sessionsSummary.turn_distribution
        : EMPTY_ARR;
    const totalTurnSessions = turnDistRaw.reduce((acc, item) => acc + (finiteNum(item?.sessions ?? item?.count) ?? 0), 0);
    const turnsDistribution = turnDistRaw.map((item) => {
        const turns = finiteNum(item?.turns) ?? 1;
        const sessions = finiteNum(item?.sessions ?? item?.count) ?? 0;
        return {
            turns,
            label: item?.label || (turns >= 10 ? '10+ turns' : `${turns} ${turns === 1 ? 'turn' : 'turns'}`),
            sessions,
            share_pct: finiteNum(item?.share_pct) ?? (totalTurnSessions > 0 ? (sessions / totalTurnSessions) * 100 : 0),
        };
    });
    const maxTurns = turnsDistribution.length > 0 ? Math.max(...turnsDistribution.map((d) => d.turns)) : 0;
    const isMultiTurn = turnsDistribution.length > 0 && maxTurns > 1;
    const totalTurnsAcrossSessions = turnsDistribution.reduce((acc, d) => acc + (d.turns * d.sessions), 0);

    return {
        winKey,
        fallbackToFull,
        fallbackSynthesized: true,
        isClientFallback: true,
        windowData: {
            duration_seconds: winDur,
            summary: {
                counts: { completed, failed, total },
                total_input_tokens: numberAt(csWin, ['total_input_tokens', 'input_tokens_total']),
                total_output_tokens: numberAt(csWin, ['total_output_tokens', 'output_tokens_total']),
                isl,
                osl,
                osl_isl_ratio: {
                    sample_count: 0,
                    mean: null,
                    p50: null,
                    p90: null,
                    regimes: {},
                },
                concurrency: {
                    mean: avgConcVals.length > 0 ? avgConcVals.reduce((a, b) => a + b, 0) / avgConcVals.length : null,
                    p50: null,
                    p90: null,
                    p99: null,
                    peak: maxConcVals.length > 0 ? Math.max(...maxConcVals) : null,
                },
                arrivals: {
                    mean_arrival_rps: meanArrival,
                    peak_arrival_rps: peakArrival,
                    burstiness_peak_to_mean: (peakArrival !== null && meanArrival !== null && meanArrival > 0)
                        ? peakArrival / meanArrival
                        : null,
                    inter_arrival_cv: null,
                },
            },
            isl_histogram: EMPTY_ARR,
            isl_cdf: EMPTY_ARR,
            osl_histogram: EMPTY_ARR,
            osl_cdf: EMPTY_ARR,
            isl_vs_osl_buckets: islVsOslBuckets,
            request_points: EMPTY_ARR,
            request_points_total: 0,
            request_points_sampled: 0,
            concurrency_residency: EMPTY_ARR,
            timeline,
            multi_turn: {
                available: Boolean(sessionsSummary?.available && (winKey === 'full' || fallbackToFull)),
                is_multi_turn: isMultiTurn,
                reason: (winKey === 'profiling' && !fallbackToFull)
                    ? 'Per-window session breakdown requires raw request export.'
                    : 'No multi-turn session progression detected in this trace.',
                session_count: totalTurnSessions || null,
                max_turns: maxTurns || null,
                turns_per_session_mean: totalTurnSessions > 0 ? totalTurnsAcrossSessions / totalTurnSessions : null,
                reusable_prefix_tokens: null,
                reusable_prefix_pct: null,
                turns_distribution: turnsDistribution,
                isl_by_turn: EMPTY_ARR,
            },
        },
    };
}

function TokenHistogramTooltip({ active, payload }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    if (!row) return null;
    const color = payload[0]?.color || CHART_SERIES[0];
    return (
        <ChartTooltip title={`${row.label} tok`}>
            <ChartTooltipRow color={color} label="Requests" value={formatCount(row.count)} />
            <ChartTooltipRow color={color} opacity={0.65} label="Share" value={fmtPct(row.share_pct)} />
        </ChartTooltip>
    );
}

function TokenCdfTooltip({ active, payload, unitLabel = 'Tokens' }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    if (!row) return null;
    const color = payload[0]?.color || CHART_SERIES[0];
    return (
        <ChartTooltip title={`P${formatDecimal(row.percentile)}`}>
            <ChartTooltipRow color={color} label={unitLabel} value={fmtExactTok(row.value)} />
            <ChartTooltipRow color={color} opacity={0.65} label="Cumulative Share" value={fmtPct(row.percentile)} />
        </ChartTooltip>
    );
}

function PromptJointOslTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    return (
        <ChartTooltip title={`ISL ${String(label || row?.label || '')} tok`}>
            {row?.count !== undefined && row?.count !== null && (
                <ChartTooltipRow
                    label="Requests"
                    value={`${formatCount(row.count)} (${fmtPct(row.share_pct)})`}
                />
            )}
            {payload.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={fmtExactTok(item.value)}
                />
            ))}
        </ChartTooltip>
    );
}

function PromptJointRatioTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    return (
        <ChartTooltip title={`ISL ${String(label || row?.label || '')} tok`}>
            {row?.count !== undefined && row?.count !== null && (
                <ChartTooltipRow
                    label="Requests"
                    value={`${formatCount(row.count)} (${fmtPct(row.share_pct)})`}
                />
            )}
            {payload.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={fmtRatio(item.value)}
                />
            ))}
        </ChartTooltip>
    );
}

function IslOslScatterTooltip({ active, payload }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    if (!row) return null;
    const color = payload[0]?.color || payload[0]?.fill || (
        row.regime === 'prefill_heavy'
            ? CHART_SERIES[1]
            : row.regime === 'decode_heavy'
                ? CHART_SERIES[2]
                : CHART_SERIES[0]
    );
    const ratioText = row.ratio === null
        ? (row.isl === 0 && row.osl > 0 ? '∞' : '—')
        : fmtRatio(row.ratio);
    return (
        <ChartTooltip title={finiteNum(row.t) !== null ? `Request @ ${formatDecimal(row.t)} s` : 'Request Token Pair'}>
            <ChartTooltipRow color={color} label="Input Sequence Length" value={fmtExactTok(row.isl)} />
            <ChartTooltipRow color={color} opacity={0.8} label="Output Sequence Length" value={fmtExactTok(row.osl)} />
            <ChartTooltipRow label="OSL / ISL Ratio" value={ratioText} />
            <ChartTooltipRow color={color} label="Regime" value={row.regimeLabel || '—'} />
        </ChartTooltip>
    );
}

function ConcurrencyResidencyTooltip({ active, payload }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    if (!row) return null;
    const color = payload[0]?.color || CHART_SERIES[0];
    return (
        <ChartTooltip title={`In-Flight: ${row.label}`}>
            <ChartTooltipRow color={color} label="Window Share" value={fmtPct(row.share_pct)} />
            <ChartTooltipRow color={color} opacity={0.65} label="Duration" value={fmtSec(row.duration_seconds)} />
        </ChartTooltip>
    );
}

function ConcurrencyTimelineTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => finiteNum(item?.value) !== null);
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={item.dataKey === 'in_flight_max'
                        ? `${formatCount(item.value)} req`
                        : `${formatDecimal(item.value)} req`}
                />
            ))}
        </ChartTooltip>
    );
}

function ArrivalRateTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => finiteNum(item?.value) !== null);
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={fmtRps(item.value)}
                />
            ))}
        </ChartTooltip>
    );
}

function TurnsDistTooltip({ active, payload }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    if (!row) return null;
    const color = payload[0]?.color || CHART_SERIES[3];
    return (
        <ChartTooltip title={row.label}>
            <ChartTooltipRow color={color} label="Sessions" value={formatCount(row.sessions)} />
            <ChartTooltipRow color={color} opacity={0.65} label="Share" value={fmtPct(row.share_pct)} />
        </ChartTooltip>
    );
}

function TurnIslTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    return (
        <ChartTooltip title={String(label || row?.label || '')}>
            {row?.request_count !== undefined && row?.request_count !== null && (
                <ChartTooltipRow label="Requests" value={formatCount(row.request_count)} />
            )}
            {payload.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={fmtExactTok(item.value)}
                />
            ))}
            {finiteNum(row?.reusable_prefix_pct) !== null && (
                <ChartTooltipRow label="Reusable Prefix" value={fmtPct(row.reusable_prefix_pct)} />
            )}
        </ChartTooltip>
    );
}

function StatStrip({ title, stats, help, helpFormula }) {
    const items = [
        ['Min', stats?.min],
        ['P50', stats?.p50],
        ['Mean', stats?.mean],
        ['P90', stats?.p90],
        ['P99', stats?.p99],
        ['Max', stats?.max],
    ];
    return (
        <div className="rounded-xl border border-theme-border bg-theme-card px-4 py-3">
            <div className="mb-2 flex items-center gap-1.5">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-theme-muted">{title}</span>
                {help && (
                    <HelpTooltip label={`${title} help`} align="start" iconSize={12} formula={helpFormula}>
                        {help}
                    </HelpTooltip>
                )}
            </div>
            <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
                {items.map(([label, val]) => (
                    <div key={label} className="min-w-0">
                        <div className="text-[10px] uppercase tracking-wider text-theme-muted">{label}</div>
                        <div className="mt-0.5 font-mono text-xs font-semibold text-theme-text">
                            {fmtExactTok(val)}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}

function firstValidNumberAt(entries, isValid) {
    for (const [source, path] of entries) {
        const numeric = numberAt(source, [path]);
        if (numeric !== null && isValid(numeric)) return numeric;
    }
    return null;
}

/**
 * Resolves dataset, replay window, pacing mode, configured concurrency, and
 * scale factor metadata cleanly from `task` and `telemetry?.run_manifest`.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function resolveWorkloadReplayConfig(task, telemetry) {
    const safeTask = task && typeof task === 'object' ? task : null;
    const rawManifest = telemetry?.run_manifest;
    const runManifest = rawManifest && typeof rawManifest === 'object' ? rawManifest : null;
    const hasTask = Boolean(safeTask && Object.keys(safeTask).length > 0);
    const hasManifest = Boolean(runManifest && Object.keys(runManifest).length > 0);
    const hasRunContext = hasTask || hasManifest;

    const trace = safeTask?.prompt?.trace && typeof safeTask.prompt.trace === 'object'
        ? safeTask.prompt.trace
        : EMPTY_OBJ;
    const sim = safeTask?.simulation && typeof safeTask.simulation === 'object'
        ? safeTask.simulation
        : EMPTY_OBJ;
    const backendOptions = sim.backend_options && typeof sim.backend_options === 'object'
        ? sim.backend_options
        : EMPTY_OBJ;

    const datasetName = runManifest?.dataset?.name || (safeTask ? taskDataset(safeTask) : '—') || '—';
    const traceFormat = runManifest?.dataset?.format
        || trace.format
        || safeTask?.trace_format
        || safeTask?.config_snapshot?.trace_format
        || null;
    const rawScenario = safeTask?.scenario
        || safeTask?.prompt?.dataset?.scenario
        || runManifest?.dataset?.scenario
        || '';
    const scenario = rawScenario ? scenarioDisplayName(rawScenario) : '';

    const isNonNegative = (n) => n >= 0;
    const isPositive = (n) => n > 0;

    const rawReplayStart = firstValidNumberAt(
        [
            [trace, 'start_seconds'],
            [runManifest, 'replay.start_seconds'],
        ],
        isNonNegative,
    );
    const replayStartSeconds = rawReplayStart ?? (hasRunContext ? 0 : null);
    const rawConfiguredReplayEnd = firstValidNumberAt(
        [
            [trace, 'end_seconds'],
            [runManifest, 'replay.end_seconds'],
        ],
        isPositive,
    );
    const configuredReplayEndSeconds = rawConfiguredReplayEnd !== null
        && (replayStartSeconds === null || rawConfiguredReplayEnd > replayStartSeconds)
        ? rawConfiguredReplayEnd
        : null;
    const replayDurationSeconds = firstValidNumberAt(
        [
            [sim, 'duration_seconds'],
            [runManifest, 'replay.duration_seconds'],
            [runManifest, 'simulation.duration_seconds'],
            [runManifest, 'duration_seconds'],
            [safeTask, 'duration_seconds'],
        ],
        isPositive,
    );
    const scaleFactor = firstValidNumberAt(
        [
            [trace, 'synthesis_speedup_ratio'],
            [safeTask, 'scale_factor'],
            [runManifest, 'replay.scale_factor'],
            [runManifest, 'dataset.scale_factor'],
            [runManifest, 'scale_factor'],
        ],
        isPositive,
    ) ?? (hasRunContext ? 1 : null);
    const replayEndSeconds = configuredReplayEndSeconds ?? (
        replayStartSeconds !== null && replayDurationSeconds !== null
            ? replayStartSeconds + replayDurationSeconds * (scaleFactor ?? 1)
            : null
    );

    const warmupSeconds = firstValidNumberAt(
        [
            [telemetry?.client_series, 'warmup_seconds'],
            [backendOptions, 'warmup_seconds'],
            [safeTask, 'backend_options.warmup_seconds'],
            [runManifest, 'replay.warmup_seconds'],
            [runManifest, 'replay.backend_options.warmup_seconds'],
            [runManifest, 'warmup_seconds'],
            [safeTask, 'warmup_seconds'],
        ],
        isNonNegative,
    );
    const graceSeconds = firstValidNumberAt(
        [
            [sim, 'grace_period_seconds'],
            [backendOptions, 'grace_period_seconds'],
            [runManifest, 'replay.grace_period_seconds'],
            [runManifest, 'replay.backend_options.grace_period_seconds'],
            [runManifest, 'simulation.grace_period_seconds'],
            [runManifest, 'grace_period_seconds'],
            [safeTask, 'grace_period_seconds'],
        ],
        isNonNegative,
    );

    const pacing = formatTaskPacing(hasTask ? safeTask : null, hasManifest ? runManifest : null);
    const explicitConcurrencyCap = firstValidNumberAt(
        [
            [backendOptions, 'concurrency'],
            [safeTask, 'backend_options.concurrency'],
            [safeTask, 'config_snapshot.backend_options.concurrency'],
            [safeTask, 'config_snapshot.concurrency'],
            [runManifest, 'replay.backend_options.concurrency'],
            [runManifest, 'replay.concurrency'],
        ],
        isPositive,
    );

    return {
        hasRunContext,
        datasetName,
        traceFormat,
        scenario,
        replayStartSeconds,
        replayEndSeconds,
        replayDurationSeconds,
        warmupSeconds,
        graceSeconds,
        scaleFactor,
        pacing,
        explicitConcurrencyCap,
    };
}

function WorkloadReplayConfigCard({ config }) {
    const {
        datasetName,
        traceFormat,
        scenario,
        replayStartSeconds,
        replayEndSeconds,
        replayDurationSeconds,
        warmupSeconds,
        graceSeconds,
        scaleFactor,
        pacing,
        explicitConcurrencyCap,
    } = config;

    const replayRangeText = replayStartSeconds !== null && replayEndSeconds !== null
        ? `${formatDecimal(replayStartSeconds)}s – ${formatDecimal(replayEndSeconds)}s`
        : replayStartSeconds !== null
            ? `From ${formatDecimal(replayStartSeconds)}s (full trace)`
            : '—';

    const timingParts = [];
    if (replayDurationSeconds !== null) timingParts.push(`Duration: ${formatDecimal(replayDurationSeconds)}s`);
    if (warmupSeconds !== null) timingParts.push(`Warmup: ${formatDecimal(warmupSeconds)}s`);
    if (graceSeconds !== null && graceSeconds > 0) timingParts.push(`Grace: ${formatDecimal(graceSeconds)}s`);
    const timingDetail = timingParts.join(' · ');

    const slotUnit = (count) => (count === 1 ? 'slot' : 'slots');
    const hasPacing = Boolean(pacing?.label);
    const pacingModeText = !hasPacing
        ? '—'
        : pacing.isFixedSchedule
            ? 'Fixed Schedule'
            : 'Concurrency';
    const pacingModeDetail = !hasPacing
        ? null
        : pacing.isFixedSchedule
            ? 'Source Timestamps'
            : pacing.label;

    const configuredConcurrencyText = !hasPacing
        ? '—'
        : pacing.isFixedSchedule
            ? 'Fixed Schedule (trace-driven)'
            : `${formatCount(pacing.concurrency)} ${slotUnit(pacing.concurrency)}`;
    const configuredConcurrencyDetail = hasPacing && pacing.isFixedSchedule && explicitConcurrencyCap !== null
        ? `Cap: ${formatCount(explicitConcurrencyCap)} ${slotUnit(explicitConcurrencyCap)}`
        : null;

    const scaleFactorText = scaleFactor !== null ? `${formatDecimal(scaleFactor)}×` : '—';

    return (
        <Panel padding="sm" className="space-y-3" aria-label="Workload and replay configuration">
            <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                    <SectionLabel tone="sky" className="mb-0">
                        Workload &amp; Replay Configuration
                    </SectionLabel>
                    <HelpTooltip
                        label="Workload and replay configuration help"
                        align="start"
                        iconSize={13}
                        formula="Replay_End_s = Replay_Start_s + Duration_s × Scale_Factor"
                    >
                        Configured trace dataset, source trace replay window, pacing strategy, concurrency limit, and time-scale multiplier for this benchmark run.
                    </HelpTooltip>
                </div>
            </div>

            <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
                <div className="min-w-0 rounded-lg border border-theme-border bg-theme-bg/50 px-3 py-2.5">
                    <dt className="text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                        Dataset
                    </dt>
                    <dd className="mt-1 truncate text-xs font-semibold text-theme-text" title={datasetName}>
                        {datasetName}
                    </dd>
                    {(traceFormat || scenario) && (
                        <dd className="mt-1.5 flex flex-wrap items-center gap-1.5">
                            {traceFormat && (
                                <Badge tone="neutral" size="xs">
                                    {traceFormat}
                                </Badge>
                            )}
                            {scenario && (
                                <Badge tone="info" size="xs">
                                    {scenario}
                                </Badge>
                            )}
                        </dd>
                    )}
                </div>

                <div className="min-w-0 rounded-lg border border-theme-border bg-theme-bg/50 px-3 py-2.5">
                    <dt className="text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                        Replay Range
                    </dt>
                    <dd className="mt-1 font-mono text-xs font-semibold text-theme-text">
                        {replayRangeText}
                    </dd>
                    {timingDetail && (
                        <dd className="mt-1 text-[11px] text-theme-muted">
                            {timingDetail}
                        </dd>
                    )}
                </div>

                <div className="min-w-0 rounded-lg border border-theme-border bg-theme-bg/50 px-3 py-2.5">
                    <dt className="text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                        Pacing Mode
                    </dt>
                    <dd className="mt-1 text-xs font-semibold text-theme-text">
                        {pacingModeText}
                    </dd>
                    {pacingModeDetail && (
                        <dd className="mt-1 text-[11px] text-theme-muted">
                            {pacingModeDetail}
                        </dd>
                    )}
                </div>

                <div className="min-w-0 rounded-lg border border-theme-border bg-theme-bg/50 px-3 py-2.5">
                    <dt className="text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                        Configured Concurrency
                    </dt>
                    <dd className="mt-1 text-xs font-semibold text-theme-text">
                        {configuredConcurrencyText}
                    </dd>
                    {configuredConcurrencyDetail && (
                        <dd className="mt-1 text-[11px] text-theme-muted">
                            {configuredConcurrencyDetail}
                        </dd>
                    )}
                </div>

                <div className="min-w-0 rounded-lg border border-theme-border bg-theme-bg/50 px-3 py-2.5">
                    <dt className="text-[10px] font-semibold uppercase tracking-wider text-theme-muted">
                        Scale Factor
                    </dt>
                    <dd className="mt-1 font-mono text-xs font-semibold text-theme-text">
                        {scaleFactorText}
                    </dd>
                    {scaleFactor !== null && (
                        <dd className="mt-1 text-[11px] text-theme-muted">
                            Trace arrival multiplier
                        </dd>
                    )}
                </div>
            </dl>
        </Panel>
    );
}

export function WorkloadTab({ task, telemetry, analysisWindow = 'profiling' }) {
    const [activeCategory, setActiveCategory] = useState('all');
    const [islHistMode, setIslHistMode] = useState('count');
    const [oslHistMode, setOslHistMode] = useState('count');

    const replayConfig = useMemo(
        () => resolveWorkloadReplayConfig(task, telemetry),
        [task, telemetry],
    );

    const resolved = useMemo(
        () => resolveWorkloadWindowData(task, telemetry, analysisWindow),
        [task, telemetry, analysisWindow],
    );

    const rawRequestPoints = Array.isArray(resolved?.windowData?.request_points)
        ? resolved.windowData.request_points
        : EMPTY_ARR;
    const scatterData = useMemo(() => classifyIslOslPoints(rawRequestPoints), [rawRequestPoints]);

    const showSection = (id) => activeCategory === 'all' || activeCategory === id;

    const warmupSeconds = replayConfig.warmupSeconds ?? 0;

    if (!resolved) {
        if (!replayConfig.hasRunContext) {
            return (
                <Panel>
                    <EmptyState
                        icon={<BarChart3 className="h-6 w-6" />}
                        title="Workload profile unavailable"
                        message="Per-request token lengths, arrival timestamps, and concurrency intervals were not recorded for this run."
                    />
                </Panel>
            );
        }
        return (
            <div className="space-y-6">
                <WorkloadReplayConfigCard config={replayConfig} />
                <Panel>
                    <EmptyState
                        icon={<BarChart3 className="h-6 w-6" />}
                        title="Workload profile unavailable"
                        message="Per-request token lengths, arrival timestamps, and concurrency intervals were not recorded for this run."
                    />
                </Panel>
            </div>
        );
    }

    const { winKey, windowData, fallbackToFull, fallbackSynthesized, isClientFallback } = resolved;
    const windowLabel = winKey === 'full' ? 'Full Run' : 'Profiling Steady-State';
    const summary = windowData.summary ?? EMPTY_OBJ;
    const counts = summary.counts ?? EMPTY_OBJ;
    const isl = summary.isl ?? EMPTY_OBJ;
    const osl = summary.osl ?? EMPTY_OBJ;
    const ratio = summary.osl_isl_ratio ?? EMPTY_OBJ;
    const regimes = ratio.regimes ?? EMPTY_OBJ;
    const conc = summary.concurrency ?? EMPTY_OBJ;
    const arrivals = summary.arrivals ?? EMPTY_OBJ;

    const islHistogram = Array.isArray(windowData.isl_histogram) ? windowData.isl_histogram : EMPTY_ARR;
    const islCdf = Array.isArray(windowData.isl_cdf) ? windowData.isl_cdf : EMPTY_ARR;
    const oslHistogram = Array.isArray(windowData.osl_histogram) ? windowData.osl_histogram : EMPTY_ARR;
    const oslCdf = Array.isArray(windowData.osl_cdf) ? windowData.osl_cdf : EMPTY_ARR;
    const islVsOslBuckets = Array.isArray(windowData.isl_vs_osl_buckets) ? windowData.isl_vs_osl_buckets : EMPTY_ARR;
    const concurrencyResidency = Array.isArray(windowData.concurrency_residency) ? windowData.concurrency_residency : EMPTY_ARR;
    const timeline = Array.isArray(windowData.timeline) ? windowData.timeline : EMPTY_ARR;
    const multiTurn = windowData.multi_turn ?? EMPTY_OBJ;

    const islCdfDomain = computeNumericDomain(islCdf, 'value');
    const oslCdfDomain = computeNumericDomain(oslCdf, 'value');
    const islScatterDomain = computeNumericDomain(scatterData.allPoints, 'isl', 0);
    const oslScatterDomain = computeNumericDomain(scatterData.allPoints, 'osl', 0);
    const scatterDiagEnd = Math.min(islScatterDomain[1], oslScatterDomain[1]);
    const scatterTotal = finiteNum(windowData.request_points_total) ?? scatterData.allPoints.length;
    const scatterSampled = finiteNum(windowData.request_points_sampled) ?? scatterData.allPoints.length;
    const isScatterDownsampled = scatterTotal > 0 && scatterSampled > 0 && scatterSampled < scatterTotal;
    const showWarmupOverlay = winKey === 'full' && warmupSeconds > 0 && timeline.length > 0;
    const timelineDomain = computeNumericDomain(timeline, 't', showWarmupOverlay ? 0 : null);

    const hasOslJointData = islVsOslBuckets.some(
        (b) => finiteNum(b?.osl_p50) !== null || finiteNum(b?.osl_mean) !== null || finiteNum(b?.osl_p90) !== null,
    );
    const hasRatioJointData = islVsOslBuckets.some(
        (b) => finiteNum(b?.ratio_p50) !== null || finiteNum(b?.ratio_mean) !== null,
    );
    const hasConcTimeline = timeline.some(
        (p) => finiteNum(p?.in_flight_avg) !== null || finiteNum(p?.in_flight_max) !== null,
    );
    const hasRateTimeline = timeline.some(
        (p) => finiteNum(p?.arrival_rps) !== null || finiteNum(p?.completed_rps) !== null,
    );
    const turnsDistribution = Array.isArray(multiTurn.turns_distribution) ? multiTurn.turns_distribution : EMPTY_ARR;
    const islByTurn = Array.isArray(multiTurn.isl_by_turn) ? multiTurn.isl_by_turn : EMPTY_ARR;
    const hasReusablePrefix = finiteNum(multiTurn.reusable_prefix_pct) !== null;
    const windowDuration = finiteNum(windowData.duration_seconds) ?? finiteNum(conc.window_duration_seconds);

    const islP50 = finiteNum(isl.p50);
    const islP90 = finiteNum(isl.p90);
    const oslP50 = finiteNum(osl.p50);
    const oslP90 = finiteNum(osl.p90);

    return (
        <div className="space-y-6">
            {replayConfig.hasRunContext && (
                <WorkloadReplayConfigCard config={replayConfig} />
            )}

            {/* Sticky Sub-Category Pill Bar */}
            <div
                className="sticky top-0 z-10 flex flex-wrap items-center gap-1.5 rounded-xl border border-theme-border bg-theme-card/95 px-3 py-2 shadow-sm backdrop-blur-sm"
                role="tablist"
                aria-label="Workload sub-categories"
            >
                {WORKLOAD_CATEGORIES.map((cat) => {
                    const active = activeCategory === cat.id;
                    return (
                        <button
                            key={cat.id}
                            type="button"
                            role="tab"
                            aria-selected={active}
                            onClick={() => setActiveCategory(cat.id)}
                            className={cn(
                                'rounded-lg border px-2.5 py-1 text-xs font-semibold transition-colors',
                                active
                                    ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                                    : 'border-theme-border bg-theme-bg text-theme-muted hover:text-theme-text',
                            )}
                        >
                            {cat.label}
                        </button>
                    );
                })}
            </div>

            {/* Section 1: Header Strip & 6 Summary StatCards */}
            {showSection('kpis') && (
            <section id="w-kpis" aria-label="Workload KPI summary" className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                        <SectionLabel tone="emerald" className="mb-0">
                            Workload Profile · {windowLabel}
                        </SectionLabel>
                        <HelpTooltip
                            label="Workload analysis window help"
                            align="start"
                            iconSize={13}
                            formula="Profiling = [Warmup_s, End_s];  Full = [0, End_s]"
                        >
                            Characterizes offered request input/output token distributions, prefill-vs-decode balance, in-flight concurrency residency, arrival dynamics, and multi-turn prefix context growth in the active window.
                        </HelpTooltip>
                        {fallbackToFull && (
                            <Badge tone="info" size="xs">
                                Full Run fallback (no warmup split)
                            </Badge>
                        )}
                        {(fallbackSynthesized || isClientFallback) && (
                            <Badge tone="neutral" size="xs">
                                Summary fallback (no raw request export)
                            </Badge>
                        )}
                    </div>
                    {windowDuration !== null && (
                        <span className="text-xs text-theme-muted font-mono">
                            Window Duration: {fmtSec(windowDuration)}
                        </span>
                    )}
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
                    <StatCard
                        compact
                        icon={<Layers className="h-4 w-4" />}
                        title="Requests & Tokens"
                        help="Total requests and aggregate input/output tokens observed during the active analysis window."
                        helpFormula="Total = Completed + Failed;  Total_ISL = Σ Input_Tokens;  Total_OSL = Σ Output_Tokens"
                        helpAlign="start"
                        value={fmtReqCount(counts.total)}
                        details={[
                            {
                                label: 'Completed',
                                value: formatCount(counts.completed),
                                help: 'Requests that completed without transport or HTTP errors in the active window.',
                                formula: 'Completed = Count(Successful_Requests_in_Window)',
                            },
                            {
                                label: 'Failed',
                                value: formatCount(counts.failed),
                                help: 'Requests that failed, timed out, or dropped in the active window.',
                                formula: 'Failed = Total_Window_Requests − Completed',
                            },
                            {
                                label: 'Total ISL',
                                value: fmtTok(summary.total_input_tokens),
                                help: 'Sum of input sequence tokens across all requests in the active window.',
                                formula: 'Total_ISL = Σ_i Input_Tokens_i',
                            },
                            {
                                label: 'Total OSL',
                                value: fmtTok(summary.total_output_tokens),
                                help: 'Sum of generated output tokens across all requests in the active window.',
                                formula: 'Total_OSL = Σ_i Output_Tokens_i',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<BarChart3 className="h-4 w-4" />}
                        title="Input Sequence Length (ISL)"
                        help="Median (P50) Input Sequence Length in tokens, alongside mean, tail percentiles, extremes, and coefficient of variation."
                        helpFormula="ISL_P50 = Quantile_0.50(Input_Tokens);  CV = σ(Input_Tokens) / μ(Input_Tokens)"
                        helpAlign="start"
                        value={fmtExactTok(isl.p50)}
                        details={[
                            {
                                label: 'Mean',
                                value: fmtExactTok(isl.mean),
                                help: 'Arithmetic mean Input Sequence Length across requests with recorded input tokens.',
                                formula: 'ISL_Mean = (1 / N) Σ_i Input_Tokens_i',
                            },
                            {
                                label: 'P90 / P99',
                                value: fmtStatPair(isl.p90, isl.p99, ' tok'),
                                help: '90th and 99th percentile Input Sequence Lengths showing long-context tail demand.',
                                formula: 'Pq = Quantile_q(sorted(Input_Tokens))  for q ∈ {0.90, 0.99}',
                            },
                            {
                                label: 'Min / Max',
                                value: fmtStatPair(isl.min, isl.max, ' tok'),
                                help: 'Shortest and longest Input Sequence Lengths observed in the active window.',
                                formula: 'Min = min(Input_Tokens);  Max = max(Input_Tokens)',
                            },
                            {
                                label: 'CV',
                                value: formatDecimal(isl.cv),
                                help: 'Coefficient of variation (std dev / mean) measuring Input Sequence Length dispersion. Higher CV indicates heterogeneous input sizes.',
                                formula: 'ISL_CV = σ_pop(Input_Tokens) / Mean(Input_Tokens)',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<MessageSquare className="h-4 w-4" />}
                        title="Output Sequence Length (OSL)"
                        help="Median (P50) Output Sequence Length in tokens, alongside mean, tail percentiles, extremes, and dispersion."
                        helpFormula="OSL_P50 = Quantile_0.50(Output_Tokens);  CV = σ(Output_Tokens) / μ(Output_Tokens)"
                        helpAlign="end"
                        value={fmtExactTok(osl.p50)}
                        details={[
                            {
                                label: 'Mean',
                                value: fmtExactTok(osl.mean),
                                help: 'Arithmetic mean Output Sequence Length across requests with recorded output tokens.',
                                formula: 'OSL_Mean = (1 / N) Σ_i Output_Tokens_i',
                            },
                            {
                                label: 'P90 / P99',
                                value: fmtStatPair(osl.p90, osl.p99, ' tok'),
                                help: '90th and 99th percentile Output Sequence Lengths highlighting long decode completions.',
                                formula: 'Pq = Quantile_q(sorted(Output_Tokens))  for q ∈ {0.90, 0.99}',
                            },
                            {
                                label: 'Min / Max',
                                value: fmtStatPair(osl.min, osl.max, ' tok'),
                                help: 'Shortest and longest Output Sequence Lengths observed in the active window.',
                                formula: 'Min = min(Output_Tokens);  Max = max(Output_Tokens)',
                            },
                            {
                                label: 'CV',
                                value: formatDecimal(osl.cv),
                                help: 'Coefficient of variation (std dev / mean) of Output Sequence Lengths.',
                                formula: 'OSL_CV = σ_pop(Output_Tokens) / Mean(Output_Tokens)',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<SlidersHorizontal className="h-4 w-4" />}
                        title="OSL / ISL Ratio"
                        help="Median per-request decode-to-prefill token ratio (OSL / ISL) and request mix across prefill-heavy, balanced, and decode-heavy regimes."
                        helpFormula="Ratio_i = Output_Tokens_i / Input_Tokens_i  (Input_Tokens_i > 0)"
                        helpAlign="start"
                        value={fmtRatio(ratio.p50)}
                        details={[
                            {
                                label: 'Mean / P90',
                                value: fmtRatioPair(ratio.mean, ratio.p90),
                                help: 'Mean and 90th percentile per-request OSL / ISL ratio.',
                                formula: 'Mean = (1 / N) Σ Ratio_i;  P90 = Quantile_0.90(Ratio)',
                            },
                            {
                                label: 'Prefill-Heavy (<0.25)',
                                value: fmtRegimeShare(regimes.prefill_heavy, ratio.sample_count),
                                help: 'Share and count of requests where output tokens are less than 0.25× input tokens.',
                                formula: 'Prefill_Heavy iff Ratio_i < 0.25',
                            },
                            {
                                label: 'Balanced (0.25–1.0)',
                                value: fmtRegimeShare(regimes.balanced, ratio.sample_count),
                                help: 'Share and count of requests with balanced input and output sequence lengths (0.25 ≤ OSL / ISL ≤ 1.0).',
                                formula: 'Balanced iff 0.25 ≤ Ratio_i ≤ 1.0',
                            },
                            {
                                label: 'Decode-Heavy (>1.0)',
                                value: fmtRegimeShare(regimes.decode_heavy, ratio.sample_count),
                                help: 'Share and count of requests generating more output tokens than input tokens (OSL / ISL > 1.0).',
                                formula: 'Decode_Heavy iff Ratio_i > 1.0',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<Activity className="h-4 w-4" />}
                        title="In-Flight Concurrency"
                        help="Time-weighted average number of simultaneous in-flight requests from exact client arrival-to-completion interval sweeps."
                        helpFormula="Mean = (1 / W) ∫_{t_start}^{t_end} N_active(t) dt"
                        helpAlign="start"
                        value={fmtReqDecimal(conc.mean)}
                        details={[
                            {
                                label: 'P50 / P90',
                                value: fmtStatPair(conc.p50, conc.p90),
                                help: 'Time-weighted 50th and 90th percentile in-flight concurrency levels across the window duration.',
                                formula: 'Pq = min { c : Duration(N_active ≤ c) / W ≥ q }',
                            },
                            {
                                label: 'P99',
                                value: formatDecimal(conc.p99),
                                help: 'Time-weighted 99th percentile in-flight concurrency level.',
                                formula: 'P99 = min { c : Duration(N_active ≤ c) / W ≥ 0.99 }',
                            },
                            {
                                label: 'Peak',
                                value: fmtReqCount(conc.peak),
                                help: 'Maximum simultaneous in-flight requests observed at any instant in the window.',
                                formula: 'Peak = max_{t ∈ [t_start, t_end]} N_active(t)',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<Gauge className="h-4 w-4" />}
                        title="Arrival Dynamics"
                        help="Mean request arrival rate and temporal burstiness across the active window."
                        helpFormula="Mean_Arrival_RPS = Window_Arrivals / W;  Burstiness = Peak_Bucket_RPS / Mean_Arrival_RPS"
                        helpAlign="end"
                        value={fmtRps(arrivals.mean_arrival_rps)}
                        details={[
                            {
                                label: 'Peak Arrival',
                                value: fmtRps(arrivals.peak_arrival_rps),
                                help: 'Highest bucketed request arrival rate observed in the active window.',
                                formula: 'Peak_Arrival_RPS = max_b (Arrivals_b / Δt_b)',
                            },
                            {
                                label: 'Burstiness (Peak/Mean)',
                                value: fmtRatio(arrivals.burstiness_peak_to_mean),
                                help: 'Peak-to-mean arrival rate ratio; values well above 1.0× indicate bursty traffic spikes.',
                                formula: 'Burstiness = Peak_Arrival_RPS / Mean_Arrival_RPS',
                            },
                            {
                                label: 'Inter-Arrival CV',
                                value: formatDecimal(arrivals.inter_arrival_cv),
                                help: 'Coefficient of variation of inter-arrival time gaps (≈0 for uniform pacing, ≈1 for Poisson, >1 for clustered bursts).',
                                formula: 'InterArrival_CV = σ_pop(Δt_arrival) / Mean(Δt_arrival)',
                            },
                        ]}
                    />
                </div>
            </section>
            )}

            {/* Section 2: ISL Distribution & CDF */}
            {showSection('isl') && (
            <section id="w-isl" className="space-y-4">
                <div className="mb-2 flex items-center gap-1.5">
                    <SectionLabel tone="cyan" className="mb-0">Input Sequence Length (ISL)</SectionLabel>
                    <HelpTooltip
                        label="Input sequence length profile help"
                        align="start"
                        iconSize={13}
                        formula="ISL_i = Input_Tokens_i;  F_ISL(x) = P(Input_Tokens ≤ x)"
                    >
                        Characterizes offered Input Sequence Length distribution across summary percentiles, binned request counts, and cumulative share.
                    </HelpTooltip>
                </div>
                <StatStrip
                    title="ISL Summary Statistics"
                    stats={isl}
                    help="Exact Input Sequence Length (tokens) summary statistics for the active window."
                    helpFormula="Min, P50, Mean, P90, P99, Max over Input_Tokens"
                />
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                    <ChartContainer
                        title="Request ISL Distribution"
                        subtitle="Histogram of Input Sequence Length (tokens)"
                        help="Distribution of request Input Sequence Lengths binned into adaptive token ranges. Toggle between absolute request counts and percentage share."
                        helpFormula="Bin_Share (%) = (Bin_Count / Total_Valid_ISL_Requests) × 100"
                        helpAlign="start"
                        actions={(
                            <ToggleGroup
                                size="xs"
                                value={islHistMode}
                                onChange={setIslHistMode}
                                options={[
                                    { value: 'count', label: 'Count' },
                                    { value: 'share_pct', label: 'Share %' },
                                ]}
                            />
                        )}
                    >
                        {islHistogram.length > 0 ? (
                            <div className="h-60 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={islHistogram} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 16 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="label"
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Input Tokens', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            width={52}
                                            allowDecimals={islHistMode === 'share_pct'}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{
                                                value: islHistMode === 'count' ? 'Requests' : '% of Window',
                                                angle: -90,
                                                position: 'insideLeft',
                                                fill: 'var(--text-muted)',
                                                fontSize: 10,
                                            }}
                                        />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<TokenHistogramTooltip />} />
                                        <Bar
                                            dataKey={islHistMode}
                                            name={islHistMode === 'count' ? 'Requests' : 'Share %'}
                                            fill={CHART_SERIES[0]}
                                            radius={[6, 6, 0, 0]}
                                            maxBarSize={80}
                                            isAnimationActive={false}
                                        />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                        ) : (
                            <EmptyState
                                title="ISL histogram unavailable"
                                message="Per-request input token lengths require raw request export records."
                            />
                        )}
                    </ChartContainer>

                    <ChartContainer
                        title="Request ISL CDF"
                        subtitle="Empirical cumulative distribution with P50 and P90 markers"
                        help="Cumulative percentage of requests with Input Sequence Length at or below a given token threshold. Dashed markers highlight P50 and P90."
                        helpFormula="F_ISL(x) = (1 / N) Σ 1[Input_Tokens_i ≤ x] × 100%"
                        helpAlign="end"
                    >
                        {islCdf.length > 0 ? (
                            <div className="h-60 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <AreaChart data={islCdf} margin={{ top: 18, right: 16, left: 0, bottom: 16 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="value"
                                            type="number"
                                            domain={islCdfDomain}
                                            tickFormatter={(v) => formatNumber(v)}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Input Tokens', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            domain={[0, 100]}
                                            width={48}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<TokenCdfTooltip unitLabel="Input Tokens" />} />
                                        {islP50 !== null && (
                                            <ReferenceLine
                                                x={islP50}
                                                stroke={CHART_SERIES[2]}
                                                strokeDasharray="4 3"
                                                label={{
                                                    value: islP50 === islP90 ? 'P50 / P90' : 'P50',
                                                    position: 'insideTopRight',
                                                    fill: CHART_SERIES[2],
                                                    fontSize: 10,
                                                }}
                                            />
                                        )}
                                        {islP90 !== null && islP90 !== islP50 && (
                                            <ReferenceLine
                                                x={islP90}
                                                stroke={CHART_SERIES[3]}
                                                strokeDasharray="4 3"
                                                label={{
                                                    value: 'P90',
                                                    position: 'insideTopLeft',
                                                    fill: CHART_SERIES[3],
                                                    fontSize: 10,
                                                }}
                                            />
                                        )}
                                        <Area
                                            type="monotone"
                                            dataKey="percentile"
                                            name="Cumulative %"
                                            stroke={CHART_SERIES[0]}
                                            fill={CHART_SERIES[0]}
                                            fillOpacity={0.16}
                                            strokeWidth={2}
                                            isAnimationActive={false}
                                        />
                                    </AreaChart>
                                </ResponsiveContainer>
                            </div>
                        ) : (
                            <EmptyState
                                title="ISL CDF unavailable"
                                message="Empirical Input Sequence Length quantiles require raw request export records."
                            />
                        )}
                    </ChartContainer>
                </div>
            </section>
            )}

            {/* Section 3: OSL Distribution & CDF */}
            {showSection('osl') && (
            <section id="w-osl" className="space-y-4">
                <div className="mb-2 flex items-center gap-1.5">
                    <SectionLabel tone="sky" className="mb-0">Output Sequence Length (OSL)</SectionLabel>
                    <HelpTooltip
                        label="Output sequence length profile help"
                        align="start"
                        iconSize={13}
                        formula="OSL_i = Output_Tokens_i;  F_OSL(x) = P(Output_Tokens ≤ x)"
                    >
                        Characterizes generated Output Sequence Length distribution across summary percentiles, binned completion counts, and cumulative share.
                    </HelpTooltip>
                </div>
                <StatStrip
                    title="OSL Summary Statistics"
                    stats={osl}
                    help="Exact Output Sequence Length (tokens) summary statistics for the active window."
                    helpFormula="Min, P50, Mean, P90, P99, Max over Output_Tokens"
                />
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                    <ChartContainer
                        title="Request OSL Distribution"
                        subtitle="Histogram of Output Sequence Length (tokens)"
                        help="Distribution of Output Sequence Lengths binned into adaptive token ranges. Toggle between absolute request counts and percentage share."
                        helpFormula="Bin_Share (%) = (Bin_Count / Total_Valid_OSL_Requests) × 100"
                        helpAlign="start"
                        actions={(
                            <ToggleGroup
                                size="xs"
                                value={oslHistMode}
                                onChange={setOslHistMode}
                                options={[
                                    { value: 'count', label: 'Count' },
                                    { value: 'share_pct', label: 'Share %' },
                                ]}
                            />
                        )}
                    >
                        {oslHistogram.length > 0 ? (
                            <div className="h-60 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={oslHistogram} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 16 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="label"
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Output Tokens', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            width={52}
                                            allowDecimals={oslHistMode === 'share_pct'}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{
                                                value: oslHistMode === 'count' ? 'Requests' : '% of Window',
                                                angle: -90,
                                                position: 'insideLeft',
                                                fill: 'var(--text-muted)',
                                                fontSize: 10,
                                            }}
                                        />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<TokenHistogramTooltip />} />
                                        <Bar
                                            dataKey={oslHistMode}
                                            name={oslHistMode === 'count' ? 'Requests' : 'Share %'}
                                            fill={CHART_SERIES[1]}
                                            radius={[6, 6, 0, 0]}
                                            maxBarSize={80}
                                            isAnimationActive={false}
                                        />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                        ) : (
                            <EmptyState
                                title="OSL histogram unavailable"
                                message="Per-request output token lengths require raw request export records."
                            />
                        )}
                    </ChartContainer>

                    <ChartContainer
                        title="Request OSL CDF"
                        subtitle="Empirical cumulative distribution with P50 and P90 markers"
                        help="Cumulative percentage of requests generating at most a given number of output tokens. Dashed markers highlight P50 and P90."
                        helpFormula="F_OSL(x) = (1 / N) Σ 1[Output_Tokens_i ≤ x] × 100%"
                        helpAlign="end"
                    >
                        {oslCdf.length > 0 ? (
                            <div className="h-60 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <AreaChart data={oslCdf} margin={{ top: 18, right: 16, left: 0, bottom: 16 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="value"
                                            type="number"
                                            domain={oslCdfDomain}
                                            tickFormatter={(v) => formatNumber(v)}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Output Tokens', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            domain={[0, 100]}
                                            width={48}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<TokenCdfTooltip unitLabel="Output Tokens" />} />
                                        {oslP50 !== null && (
                                            <ReferenceLine
                                                x={oslP50}
                                                stroke={CHART_SERIES[2]}
                                                strokeDasharray="4 3"
                                                label={{
                                                    value: oslP50 === oslP90 ? 'P50 / P90' : 'P50',
                                                    position: 'insideTopRight',
                                                    fill: CHART_SERIES[2],
                                                    fontSize: 10,
                                                }}
                                            />
                                        )}
                                        {oslP90 !== null && oslP90 !== oslP50 && (
                                            <ReferenceLine
                                                x={oslP90}
                                                stroke={CHART_SERIES[3]}
                                                strokeDasharray="4 3"
                                                label={{
                                                    value: 'P90',
                                                    position: 'insideTopLeft',
                                                    fill: CHART_SERIES[3],
                                                    fontSize: 10,
                                                }}
                                            />
                                        )}
                                        <Area
                                            type="monotone"
                                            dataKey="percentile"
                                            name="Cumulative %"
                                            stroke={CHART_SERIES[1]}
                                            fill={CHART_SERIES[1]}
                                            fillOpacity={0.16}
                                            strokeWidth={2}
                                            isAnimationActive={false}
                                        />
                                    </AreaChart>
                                </ResponsiveContainer>
                            </div>
                        ) : (
                            <EmptyState
                                title="OSL CDF unavailable"
                                message="Empirical Output Sequence Length quantiles require raw request export records."
                            />
                        )}
                    </ChartContainer>
                </div>
            </section>
            )}

            {/* Section 4: ISL vs OSL Joint Profile */}
            {showSection('joint') && (
            <section id="w-joint" className="space-y-4">
                <div className="mb-2 flex items-center gap-1.5">
                    <SectionLabel tone="blue" className="mb-0">ISL vs OSL Joint Profile</SectionLabel>
                    <HelpTooltip
                        label="ISL vs OSL joint profile help"
                        align="start"
                        iconSize={13}
                        formula="Ratio_i = OSL_i / ISL_i  (ISL_i > 0)"
                    >
                        Visualizes the per-request joint relationship between Input Sequence Length and Output Sequence Length alongside bucketed decode intensity regimes.
                    </HelpTooltip>
                </div>

                <ChartContainer
                    title="Input Sequence Length vs Output Sequence Length"
                    subtitle={isScatterDownsampled
                        ? `Per-request Input Sequence Length vs Output Sequence Length by regime · Showing ${formatCount(scatterSampled)} of ${formatCount(scatterTotal)} requests`
                        : 'Per-request Input Sequence Length vs Output Sequence Length colored by OSL / ISL regime'}
                    help="Plots each request's Input Sequence Length (x-axis) against its Output Sequence Length (y-axis), colored by decode-to-prefill regime. The dashed diagonal marks equal input and output sequence lengths (OSL = ISL)."
                    helpFormula="Point_i = (Input_Tokens_i, Output_Tokens_i);  Ratio_i = Output_Tokens_i / Input_Tokens_i  (Input_Tokens_i > 0)"
                    helpAlign="start"
                >
                    {scatterData.allPoints.length > 0 ? (
                        <>
                            <div className="h-72 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <ScatterChart margin={{ top: 18, right: 16, left: 0, bottom: 16 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" />
                                        <XAxis
                                            dataKey="isl"
                                            type="number"
                                            name="Input Sequence Length"
                                            domain={islScatterDomain}
                                            tickFormatter={(v) => formatNumber(v)}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Input Sequence Length (tokens)', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            dataKey="osl"
                                            type="number"
                                            name="Output Sequence Length"
                                            domain={oslScatterDomain}
                                            width={56}
                                            tickFormatter={(v) => formatNumber(v)}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Output Sequence Length (tokens)', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <ZAxis range={[28, 28]} />
                                        <Tooltip
                                            cursor={{ strokeDasharray: '3 3', stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' }}
                                            content={<IslOslScatterTooltip />}
                                        />
                                        {scatterDiagEnd > 0 && (
                                            <ReferenceLine
                                                segment={[{ x: 0, y: 0 }, { x: scatterDiagEnd, y: scatterDiagEnd }]}
                                                stroke="var(--text-muted)"
                                                strokeDasharray="4 3"
                                                ifOverflow="discard"
                                                label={{
                                                    value: 'OSL = ISL (1:1)',
                                                    position: 'insideTopLeft',
                                                    fill: 'var(--text-muted)',
                                                    fontSize: 10,
                                                }}
                                            />
                                        )}
                                        <Scatter
                                            name="Prefill-Heavy (<0.25×)"
                                            data={scatterData.prefillHeavy}
                                            fill={CHART_SERIES[1]}
                                            fillOpacity={0.65}
                                            isAnimationActive={false}
                                        />
                                        <Scatter
                                            name="Balanced (0.25–1.0×)"
                                            data={scatterData.balanced}
                                            fill={CHART_SERIES[0]}
                                            fillOpacity={0.65}
                                            isAnimationActive={false}
                                        />
                                        <Scatter
                                            name="Decode-Heavy (>1.0×)"
                                            data={scatterData.decodeHeavy}
                                            fill={CHART_SERIES[2]}
                                            fillOpacity={0.65}
                                            isAnimationActive={false}
                                        />
                                    </ScatterChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'Prefill-Heavy (<0.25×)', color: CHART_SERIES[1] },
                                    { label: 'Balanced (0.25–1.0×)', color: CHART_SERIES[0] },
                                    { label: 'Decode-Heavy (>1.0×)', color: CHART_SERIES[2] },
                                ]}
                            />
                        </>
                    ) : (
                        <EmptyState
                            title="Input Sequence Length vs Output Sequence Length unavailable"
                            message="Per-request Input Sequence Length and Output Sequence Length scatter points require raw request export records."
                        />
                    )}
                </ChartContainer>

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <ChartContainer
                    title="OSL by Input Sequence Length Bucket"
                    subtitle="Output Sequence Length (P50, Mean, P90) conditioned on Input Sequence Length"
                    help="Shows how Output Sequence Length scales with Input Sequence Length across canonical ISL buckets (<512 to >32K tokens)."
                    helpFormula="OSL_Stat_bucket = Stat(Output_Tokens | Input_Tokens ∈ Bucket)"
                    helpAlign="start"
                >
                    {hasOslJointData ? (
                        <>
                            <div className="h-60 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={islVsOslBuckets} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 16 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="label"
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Input Sequence Length Bucket (ISL)', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            width={52}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Output Tokens', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<PromptJointOslTooltip />} />
                                        <Bar dataKey="osl_p50" name="OSL P50" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                        <Bar dataKey="osl_mean" name="OSL Mean" fill={CHART_SERIES[1]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                        <Bar dataKey="osl_p90" name="OSL P90" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'OSL P50', color: CHART_SERIES[0] },
                                    { label: 'OSL Mean', color: CHART_SERIES[1] },
                                    { label: 'OSL P90', color: CHART_SERIES[2] },
                                ]}
                            />
                        </>
                    ) : (
                        <EmptyState
                            title="Joint OSL profile unavailable"
                            message="Output Sequence Lengths grouped by Input Sequence Length bucket require per-request token records."
                        />
                    )}
                </ChartContainer>

                <ChartContainer
                    title="Decode Intensity (OSL / ISL Ratio) by Input Sequence Length"
                    subtitle="Per-request OSL / ISL ratio with Prefill-Heavy (<0.25×) and Decode-Heavy (>1.0×) thresholds"
                    help="Reveals whether short inputs trigger long generations (decode-heavy) or long contexts yield concise answers (prefill-heavy)."
                    helpFormula="Ratio_bucket = Stat(Output_Tokens_i / Input_Tokens_i | Input_Tokens_i ∈ Bucket ∧ Input_Tokens_i > 0)"
                    helpAlign="end"
                >
                    {hasRatioJointData ? (
                        <>
                            <div className="h-60 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={islVsOslBuckets} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 16 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="label"
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Input Sequence Length Bucket (ISL)', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            width={52}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Ratio (OSL / ISL)', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<PromptJointRatioTooltip />} />
                                        <ReferenceLine
                                            y={0.25}
                                            stroke="var(--text-muted)"
                                            strokeDasharray="4 3"
                                            label={{ value: '0.25× (Prefill)', position: 'insideTopRight', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <ReferenceLine
                                            y={1.0}
                                            stroke="var(--text-muted)"
                                            strokeDasharray="4 3"
                                            label={{ value: '1.0× (Decode)', position: 'insideTopRight', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Bar dataKey="ratio_p50" name="Ratio P50" fill={CHART_SERIES[3]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                        <Bar dataKey="ratio_mean" name="Ratio Mean" fill={CHART_SERIES[4]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'Ratio P50', color: CHART_SERIES[3] },
                                    { label: 'Ratio Mean', color: CHART_SERIES[4] },
                                ]}
                            />
                        </>
                    ) : (
                        <EmptyState
                            title="Decode intensity profile unavailable"
                            message="Per-request OSL / ISL ratios require per-request input and output token records."
                        />
                    )}
                </ChartContainer>
                </div>
            </section>
            )}

            {/* Section 5: Concurrency Residency & Separate Timelines */}
            {showSection('conc') && (
            <section id="w-conc" className="space-y-4">
                <div className="mb-2 flex items-center gap-1.5">
                    <SectionLabel tone="cyan" className="mb-0">Concurrency &amp; Arrival Dynamics</SectionLabel>
                    <HelpTooltip
                        label="Concurrency and arrival dynamics help"
                        align="start"
                        iconSize={13}
                        formula="InFlight(t) = N_active(t);  Arrival_RPS(b) = Arrivals_b / Δt_b"
                    >
                        Shows time-weighted in-flight concurrency residency alongside offered arrival and completion rate timelines across the active window.
                    </HelpTooltip>
                </div>
                <ChartContainer
                    title="In-Flight Concurrency Residency"
                    subtitle="Percentage of active window duration spent at each in-flight concurrency level"
                    help="Exact event-sweep distribution showing how much of the active window elapsed at each simultaneous request concurrency level."
                    helpFormula="Residency_Share(B) = (Σ_{c ∈ B} Duration(N_active = c) / W) × 100%"
                    helpAlign="start"
                >
                    {concurrencyResidency.length > 0 ? (
                        <div className="h-56 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <BarChart data={concurrencyResidency} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 16 }}>
                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                    <XAxis
                                        dataKey="label"
                                        tick={AXIS_TICK}
                                        stroke="var(--border-color)"
                                        label={{ value: 'In-Flight Concurrency', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                    />
                                    <YAxis
                                        width={52}
                                        tick={AXIS_TICK}
                                        stroke="var(--border-color)"
                                        label={{ value: '% of Window', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                    />
                                    <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<ConcurrencyResidencyTooltip />} />
                                    <Bar
                                        dataKey="share_pct"
                                        name="% of Window"
                                        fill={CHART_SERIES[0]}
                                        radius={[6, 6, 0, 0]}
                                        maxBarSize={80}
                                        isAnimationActive={false}
                                    />
                                </BarChart>
                            </ResponsiveContainer>
                        </div>
                    ) : (
                        <EmptyState
                            title="Concurrency residency unavailable"
                            message="Time-weighted concurrency residency requires per-request arrival and completion timestamps."
                        />
                    )}
                </ChartContainer>

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                    <ChartContainer
                        title="In-Flight Concurrency over Time"
                        subtitle={showWarmupOverlay
                            ? `Time-weighted average and peak simultaneous requests (shaded warmup: 0–${fmtSec(warmupSeconds)})`
                            : 'Time-weighted average and peak simultaneous in-flight requests per bucket'}
                        help="Tracks average and instantaneous peak concurrent requests across elapsed buckets. Diverging peak vs average highlights short microbursts."
                        helpFormula="InFlight_Avg_b = (1 / Δt_b) ∫_{t_b0}^{t_b1} N_active(t) dt;  InFlight_Max_b = max_{t ∈ b} N_active(t)"
                        helpAlign="start"
                    >
                        {hasConcTimeline ? (
                            <>
                                <div className="h-60 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <LineChart data={timeline} margin={{ top: 22, right: 16, left: 0, bottom: 8 }}>
                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                            <XAxis
                                                dataKey="t"
                                                type="number"
                                                domain={timelineDomain}
                                                tickFormatter={(v) => `${formatNumber(v)}s`}
                                                tick={AXIS_TICK}
                                                stroke="var(--border-color)"
                                            />
                                            <YAxis
                                                width={52}
                                                allowDecimals={false}
                                                tick={AXIS_TICK}
                                                stroke="var(--border-color)"
                                                label={{ value: 'Requests', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                            />
                                            <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<ConcurrencyTimelineTooltip />} />
                                            {showWarmupOverlay && (
                                                <ReferenceArea x1={0} x2={warmupSeconds} fill="var(--text-muted)" fillOpacity={0.08} />
                                            )}
                                            {showWarmupOverlay && (
                                                <ReferenceLine
                                                    x={warmupSeconds}
                                                    stroke="var(--text-muted)"
                                                    strokeDasharray="4 4"
                                                    label={{
                                                        value: 'Warmup',
                                                        position: 'insideTopRight',
                                                        fill: 'var(--text-muted)',
                                                        fontSize: 10,
                                                        dy: -18,
                                                    }}
                                                />
                                            )}
                                            <Line
                                                type="monotone"
                                                dataKey="in_flight_avg"
                                                name="In-Flight Avg"
                                                stroke={CHART_SERIES[0]}
                                                strokeWidth={2}
                                                connectNulls
                                                dot={sparseDotProp(timeline, 'in_flight_avg', CHART_SERIES[0])}
                                                isAnimationActive={false}
                                            />
                                            <Line
                                                type="monotone"
                                                dataKey="in_flight_max"
                                                name="In-Flight Max"
                                                stroke={CHART_SERIES[2]}
                                                strokeWidth={1.75}
                                                strokeDasharray="4 3"
                                                connectNulls
                                                dot={sparseDotProp(timeline, 'in_flight_max', CHART_SERIES[2])}
                                                isAnimationActive={false}
                                            />
                                        </LineChart>
                                    </ResponsiveContainer>
                                </div>
                                <ChartLegend
                                    className="mt-3"
                                    entries={[
                                        { label: 'In-Flight Avg', color: CHART_SERIES[0] },
                                        { label: 'In-Flight Max', color: CHART_SERIES[2] },
                                    ]}
                                />
                            </>
                        ) : (
                            <EmptyState
                                title="Concurrency timeline unavailable"
                                message="Per-bucket concurrency series was not recorded for this window."
                            />
                        )}
                    </ChartContainer>

                    <ChartContainer
                        title="Arrival vs Completion Rate over Time"
                        subtitle={showWarmupOverlay
                            ? `Offered arrival rate vs completed throughput (shaded warmup: 0–${fmtSec(warmupSeconds)})`
                            : 'Offered arrival rate vs completed request throughput per bucket'}
                        help="Compares incoming request arrival rate against completion rate. Sustained arrival excess over completion indicates queueing accumulation."
                        helpFormula="Arrival_RPS_b = Arrivals_b / Δt_b;  Completed_RPS_b = Completed_b / Δt_b"
                        helpAlign="end"
                    >
                        {hasRateTimeline ? (
                            <>
                                <div className="h-60 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <LineChart data={timeline} margin={{ top: 22, right: 16, left: 0, bottom: 8 }}>
                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                            <XAxis
                                                dataKey="t"
                                                type="number"
                                                domain={timelineDomain}
                                                tickFormatter={(v) => `${formatNumber(v)}s`}
                                                tick={AXIS_TICK}
                                                stroke="var(--border-color)"
                                            />
                                            <YAxis
                                                width={52}
                                                tick={AXIS_TICK}
                                                stroke="var(--border-color)"
                                                label={{ value: 'req/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                            />
                                            <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<ArrivalRateTooltip />} />
                                            {showWarmupOverlay && (
                                                <ReferenceArea x1={0} x2={warmupSeconds} fill="var(--text-muted)" fillOpacity={0.08} />
                                            )}
                                            {showWarmupOverlay && (
                                                <ReferenceLine
                                                    x={warmupSeconds}
                                                    stroke="var(--text-muted)"
                                                    strokeDasharray="4 4"
                                                    label={{
                                                        value: 'Warmup',
                                                        position: 'insideTopRight',
                                                        fill: 'var(--text-muted)',
                                                        fontSize: 10,
                                                        dy: -18,
                                                    }}
                                                />
                                            )}
                                            <Line
                                                type="monotone"
                                                dataKey="arrival_rps"
                                                name="Arrival Rate"
                                                stroke={CHART_SERIES[1]}
                                                strokeWidth={2}
                                                connectNulls
                                                dot={sparseDotProp(timeline, 'arrival_rps', CHART_SERIES[1])}
                                                isAnimationActive={false}
                                            />
                                            <Line
                                                type="monotone"
                                                dataKey="completed_rps"
                                                name="Completion Rate"
                                                stroke={CHART_SERIES[0]}
                                                strokeWidth={2}
                                                connectNulls
                                                dot={sparseDotProp(timeline, 'completed_rps', CHART_SERIES[0])}
                                                isAnimationActive={false}
                                            />
                                        </LineChart>
                                    </ResponsiveContainer>
                                </div>
                                <ChartLegend
                                    className="mt-3"
                                    entries={[
                                        { label: 'Arrival Rate (req/s)', color: CHART_SERIES[1] },
                                        { label: 'Completion Rate (req/s)', color: CHART_SERIES[0] },
                                    ]}
                                />
                            </>
                        ) : (
                            <EmptyState
                                title="Arrival vs completion timeline unavailable"
                                message="Per-bucket arrival and completion rates were not recorded for this window."
                            />
                        )}
                    </ChartContainer>
                </div>
            </section>
            )}

            {/* Section 6: Multi-Turn & Prefix Context Profile */}
            {showSection('multiturn') && (
            <section id="w-multiturn" className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                        <SectionLabel tone="violet" className="mb-0">
                            Multi-Turn &amp; Prefix Context Profile
                        </SectionLabel>
                        <HelpTooltip
                            label="Multi-turn and prefix context profile help"
                            align="start"
                            iconSize={13}
                            formula="Reusable_Prefix_Share (%) = (Σ min(Input_Tokens_i, Reusable_Prefix_Tokens_i) / Σ Input_Tokens_i) × 100"
                        >
                            Summarizes session turn depth, Input Sequence Length expansion across follow-up turns, and client-side reusable prefix token share.
                        </HelpTooltip>
                    </div>
                    {multiTurn.is_multi_turn && (
                        <div className="flex flex-wrap items-center gap-2">
                            <Badge tone="neutral" size="sm">
                                Sessions: {formatCount(multiTurn.session_count)}
                            </Badge>
                            <Badge tone="info" size="sm">
                                Mean Turns: {formatDecimal(multiTurn.turns_per_session_mean)}
                            </Badge>
                            <Badge tone="violet" size="sm">
                                Max Turns: {formatCount(multiTurn.max_turns)}
                            </Badge>
                            {hasReusablePrefix && (
                                <Badge tone="success" size="sm">
                                    Reusable Prefix Share: {fmtPct(multiTurn.reusable_prefix_pct)}
                                    {finiteNum(multiTurn.reusable_prefix_tokens) !== null
                                        ? ` (${fmtTok(multiTurn.reusable_prefix_tokens)})`
                                        : ''}
                                </Badge>
                            )}
                        </div>
                    )}
                </div>

                {multiTurn.is_multi_turn ? (
                    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                        <ChartContainer
                            title="Turns per Session Distribution"
                            subtitle="Session count grouped by total conversation turns"
                            help="Shows how many sessions contain 1, 2, or more turns. Multi-turn sessions accumulate context across turns."
                            helpFormula="Sessions_k = Count(Sessions with Turn_Count = k)"
                            helpAlign="start"
                        >
                            {turnsDistribution.length > 0 ? (
                                <div className="h-60 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <BarChart data={turnsDistribution} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 16 }}>
                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                            <XAxis
                                                dataKey="label"
                                                tick={AXIS_TICK}
                                                stroke="var(--border-color)"
                                                label={{ value: 'Turns per Session', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                            />
                                            <YAxis
                                                width={52}
                                                allowDecimals={false}
                                                tick={AXIS_TICK}
                                                stroke="var(--border-color)"
                                                label={{ value: 'Sessions', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                            />
                                            <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<TurnsDistTooltip />} />
                                            <Bar
                                                dataKey="sessions"
                                                name="Sessions"
                                                fill={CHART_SERIES[3]}
                                                radius={[6, 6, 0, 0]}
                                                maxBarSize={80}
                                                isAnimationActive={false}
                                            />
                                        </BarChart>
                                    </ResponsiveContainer>
                                </div>
                            ) : (
                                <EmptyState
                                    title="Turn distribution unavailable"
                                    message="Session turn counts were not available in this window."
                                />
                            )}
                        </ChartContainer>

                        <ChartContainer
                            title="ISL Growth by Turn Depth"
                            subtitle="Input Sequence Length (P50, Mean, P90) across Turn 1 through Turn 5+"
                            help="Tracks how Input Sequence Length expands with each conversation turn as prior history accumulates."
                            helpFormula="ISL_Turn_Stat = Stat(Input_Tokens | Turn_Depth = d)"
                            helpAlign="end"
                        >
                            {islByTurn.length > 0 ? (
                                <>
                                    <div className="h-60 w-full">
                                        <ResponsiveContainer width="100%" height="100%">
                                            <BarChart data={islByTurn} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 16 }}>
                                                <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                <XAxis
                                                    dataKey="label"
                                                    tick={AXIS_TICK}
                                                    stroke="var(--border-color)"
                                                    label={{ value: 'Turn Depth', position: 'insideBottom', offset: -8, fill: 'var(--text-muted)', fontSize: 10 }}
                                                />
                                                <YAxis
                                                    width={52}
                                                    tick={AXIS_TICK}
                                                    stroke="var(--border-color)"
                                                    label={{ value: 'Input Tokens', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                                />
                                                <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<TurnIslTooltip />} />
                                                <Bar dataKey="isl_p50" name="ISL P50" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                                <Bar dataKey="isl_mean" name="ISL Mean" fill={CHART_SERIES[1]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                                <Bar dataKey="isl_p90" name="ISL P90" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                            </BarChart>
                                        </ResponsiveContainer>
                                    </div>
                                    <ChartLegend
                                        className="mt-3"
                                        entries={[
                                            { label: 'ISL P50', color: CHART_SERIES[0] },
                                            { label: 'ISL Mean', color: CHART_SERIES[1] },
                                            { label: 'ISL P90', color: CHART_SERIES[2] },
                                        ]}
                                    />
                                </>
                            ) : (
                                <EmptyState
                                    title="Turn-depth ISL unavailable"
                                    message="Per-turn input token progression requires raw per-request records."
                                />
                            )}
                        </ChartContainer>
                    </div>
                ) : (
                    <Panel className="space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex items-center gap-2 text-sm text-theme-text">
                                <GitBranch className="h-4 w-4 text-theme-muted shrink-0" aria-hidden="true" />
                                <span className="font-medium">Single-Turn / Sessionless Workload</span>
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                {finiteNum(multiTurn.session_count) !== null && multiTurn.session_count > 0 && (
                                    <Badge tone="neutral" size="sm">
                                        Sessions: {formatCount(multiTurn.session_count)}
                                    </Badge>
                                )}
                                {hasReusablePrefix && (
                                    <Badge tone="success" size="sm">
                                        Reusable Prefix Share: {fmtPct(multiTurn.reusable_prefix_pct)}
                                        {finiteNum(multiTurn.reusable_prefix_tokens) !== null
                                            ? ` (${fmtTok(multiTurn.reusable_prefix_tokens)})`
                                            : ''}
                                    </Badge>
                                )}
                            </div>
                        </div>
                        <p className="text-xs leading-relaxed text-theme-muted">
                            {multiTurn.reason || 'All requests in this window represent independent single-turn prompts or lack multi-turn session metadata.'}
                        </p>
                    </Panel>
                )}
            </section>
            )}
        </div>
    );
}

export default WorkloadTab;
