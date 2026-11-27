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
    AlertTriangle,
    ArrowUpRight,
    CheckCircle2,
    GitBranch,
    Layers,
    Server,
} from 'lucide-react';
import {
    Area,
    AreaChart,
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
    Button,
    CHART_SERIES,
    ChartContainer,
    ChartLegend,
    ChartTooltip,
    ChartTooltipRow,
    EmptyState,
    HelpTooltip,
    Panel,
    SectionLabel,
    ToggleGroup,
} from '../../../../shared/ui';
import {
    TTFT_BAR_OPACITY_BY_KEY,
    buildStageLatencyView,
    buildTtftDecompositionView,
    clampPercent,
    displayName,
    effectivePodRole,
    formatCount,
    formatDecimal,
    formatMilliseconds,
    formatNumber,
    formatStageName,
    isBenchmarkRunnerPod,
    isFiniteMetricValue,
    stageAreaLayers,
} from '../../model/presentation';

const AXIS_TICK = { fill: 'var(--text-muted)', fontSize: 10 };
const BAR_TOOLTIP_CURSOR = { fill: 'var(--chart-cursor-fill, rgba(148, 163, 184, 0.10))' };
const LINE_TOOLTIP_CURSOR = { stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' };

const BUCKET_STATUS_ORDER = ['healthy', 'queueing', 'kv_pressure', 'evicting', 'error'];

const BUCKET_STATUS_STYLE = {
    healthy: {
        label: 'Healthy',
        help: 'Nominal serving bucket with low waiting requests, safe KV cache usage, and zero preemptions.',
        formula: 'Failed = 0 ∧ Preemptions = 0 ∧ Max_KV_% < 90% ∧ Waiting_Mean = 0',
        dot: 'bg-emerald-500',
        bar: 'bg-emerald-500',
        chip: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200',
        badgeTone: 'success',
        bandColor: null,
        rank: 0,
    },
    queueing: {
        label: 'Queueing',
        help: 'Requests queued waiting for scheduler admission during this bucket, elevating TTFT.',
        formula: 'Waiting_Mean > 0  (with Failed = 0 ∧ Preemptions = 0 ∧ Max_KV_% < 90%)',
        dot: 'bg-sky-500',
        bar: 'bg-sky-500',
        chip: 'border-sky-500/30 bg-sky-500/10 text-sky-800 dark:text-sky-200',
        badgeTone: 'info',
        bandColor: CHART_SERIES[1],
        rank: 1,
    },
    kv_pressure: {
        label: 'KV Pressure',
        help: 'High KV cache usage (approaching capacity), risking throttling or admission delays.',
        formula: 'Max_KV_Usage_% ≥ 90%  (with Failed = 0 ∧ Preemptions = 0)',
        dot: 'bg-amber-500',
        bar: 'bg-amber-500',
        chip: 'border-amber-500/35 bg-amber-500/10 text-amber-800 dark:text-amber-200',
        badgeTone: 'warning',
        bandColor: CHART_SERIES[2],
        rank: 2,
    },
    evicting: {
        label: 'Evicting',
        help: 'KV cache saturation triggered request preemptions or block evictions, forcing costly recomputation.',
        formula: 'Preemption_Rate > 0  (with Failed = 0)',
        dot: 'bg-red-500',
        bar: 'bg-red-500',
        chip: 'border-red-500/35 bg-red-500/10 text-red-800 dark:text-red-200',
        badgeTone: 'danger',
        bandColor: CHART_SERIES[4],
        rank: 3,
    },
    error: {
        label: 'Error',
        help: 'Errors or failed requests occurred in this time bucket alongside server pressure.',
        formula: 'Failed_Requests > 0',
        dot: 'bg-red-600',
        bar: 'bg-red-600',
        chip: 'border-red-600/40 bg-red-500/15 text-red-900 dark:text-red-100',
        badgeTone: 'danger',
        bandColor: CHART_SERIES[4],
        rank: 4,
    },
};

const ROLE_DISPLAY_NAMES = {
    prefill: 'Prefill Role',
    decode: 'Decode Role',
    worker: 'Aggregated Worker',
    workers: 'Aggregated Worker',
    epp: 'EPP Router',
    client: 'Client',
};

function firstFinite(...values) {
    for (const value of values) {
        if (isFiniteMetricValue(value)) return Number(value);
    }
    return null;
}

function toFiniteOrNull(value) {
    return isFiniteMetricValue(value) ? Number(value) : null;
}

function meanSeriesValue(rows, key) {
    let sum = 0;
    let count = 0;
    for (const row of rows) {
        const val = row?.[key];
        if (isFiniteMetricValue(val)) {
            sum += Number(val);
            count += 1;
        }
    }
    return count > 0 ? sum / count : null;
}

function roleDisplayLabel(role) {
    const key = String(role || 'worker').toLowerCase();
    return ROLE_DISPLAY_NAMES[key] || displayName(role || 'worker');
}

function severityTone(severity) {
    const normalized = String(severity || '').toLowerCase();
    if (normalized === 'critical' || normalized === 'crit' || normalized === 'high') return 'danger';
    if (normalized === 'warning' || normalized === 'warn' || normalized === 'medium') return 'warning';
    if (normalized === 'healthy' || normalized === 'ok') return 'success';
    return 'info';
}

const FINDING_ROLE_BADGES = {
    prefill: { label: 'Prefill (P)', tone: 'info' },
    decode: { label: 'Decode (D)', tone: 'violet' },
    both: { label: 'P + D', tone: 'neutral' },
};

function findingRoleBadge(role) {
    if (typeof role !== 'string') return null;
    return FINDING_ROLE_BADGES[role.trim().toLowerCase()] || null;
}

function normalizeFindings(forensics) {
    const raw = Array.isArray(forensics?.findings) ? forensics.findings : [];
    const order = { critical: 0, crit: 0, high: 0, warning: 1, warn: 1, medium: 1, info: 2, low: 2 };
    return [...raw].sort((a, b) => (order[String(a?.severity || '').toLowerCase()] ?? 3) - (order[String(b?.severity || '').toLowerCase()] ?? 3));
}

function buildRoleSaturationView(forensics) {
    const buckets = Array.isArray(forensics?.timeline_buckets) ? forensics.timeline_buckets : [];
    if (!buckets.length) {
        return { engineRoles: [], eppSummary: null, clientSummary: null, hasData: false };
    }

    const byRole = new Map();
    const pushPoint = (rawRole, bucketIdx, cell, fallbackStart, fallbackEnd, fallbackLabel, fallbackIdx = 0) => {
        if (!cell || typeof cell !== 'object') return;
        const roleKey = String(rawRole || 'worker').toLowerCase();
        const numericIdx = toFiniteOrNull(bucketIdx) ?? fallbackIdx;
        const start = toFiniteOrNull(cell.start_seconds ?? fallbackStart);
        const end = toFiniteOrNull(cell.end_seconds ?? fallbackEnd);
        const hasWindow = start !== null && end !== null;
        const resolvedStart = hasWindow ? start : numericIdx;
        const resolvedEnd = hasWindow ? Math.max(start, end) : numericIdx + 1;
        const t = hasWindow ? (resolvedStart + resolvedEnd) / 2 : numericIdx + 0.5;
        const label = cell.label
            || fallbackLabel
            || (hasWindow ? `${formatNumber(resolvedStart)}–${formatNumber(resolvedEnd)}s` : `B${numericIdx + 1}`);
        const rawStatus = String(cell.state ?? cell.status ?? 'healthy').toLowerCase();
        const status = BUCKET_STATUS_STYLE[rawStatus] ? rawStatus : 'healthy';

        if (!byRole.has(roleKey)) {
            byRole.set(roleKey, []);
        }
        byRole.get(roleKey).push({
            order: numericIdx,
            t,
            start_seconds: resolvedStart,
            end_seconds: resolvedEnd,
            label,
            status,
            running: toFiniteOrNull(cell.running),
            waiting: toFiniteOrNull(cell.waiting),
            kv_pct: clampPercent(cell.kv_cache_pct ?? cell.kv_pct),
            prefix_hit_pct: clampPercent(cell.prefix_hit_pct ?? cell.prefix_cache_hit_pct),
            preemptions: toFiniteOrNull(cell.preemptions) ?? 0,
            failed_requests: toFiniteOrNull(cell.failed_requests) ?? 0,
        });
    };

    const isFlatList = buckets.some((item) => item && typeof item === 'object' && !item.roles && item.role);
    if (isFlatList) {
        buckets.forEach((cell, idx) => {
            if (!cell || typeof cell !== 'object') return;
            pushPoint(cell.role || 'worker', cell.bucket_index, cell, cell.start_seconds, cell.end_seconds, cell.label, idx);
        });
    } else {
        buckets.forEach((bucket, idx) => {
            if (!bucket || typeof bucket !== 'object') return;
            const rolesMap = bucket.roles && typeof bucket.roles === 'object' ? bucket.roles : {};
            Object.entries(rolesMap).forEach(([role, cell]) => {
                pushPoint(role, bucket.bucket_index, cell, bucket.start_seconds, bucket.end_seconds, bucket.label, idx);
            });
        });
    }

    if (!byRole.size) {
        return { engineRoles: [], eppSummary: null, clientSummary: null, hasData: false };
    }

    const summarizeRole = (roleKey, rawPoints) => {
        const series = [...rawPoints].sort((a, b) => (a.order - b.order) || (a.start_seconds - b.start_seconds) || (a.t - b.t));
        const totalBuckets = series.length;
        const counts = Object.fromEntries(BUCKET_STATUS_ORDER.map((key) => [key, 0]));
        let peakWaiting = null;
        let peakKvPct = null;
        let totalPreemptions = 0;
        let totalFailed = 0;
        let worstStatus = 'healthy';
        let runningSum = 0;
        let runningCount = 0;

        for (const pt of series) {
            counts[pt.status] = (counts[pt.status] || 0) + 1;
            if ((BUCKET_STATUS_STYLE[pt.status]?.rank ?? 0) > (BUCKET_STATUS_STYLE[worstStatus]?.rank ?? 0)) {
                worstStatus = pt.status;
            }
            if (pt.waiting !== null) {
                peakWaiting = peakWaiting === null ? pt.waiting : Math.max(peakWaiting, pt.waiting);
            }
            if (pt.kv_pct !== null) {
                peakKvPct = peakKvPct === null ? pt.kv_pct : Math.max(peakKvPct, pt.kv_pct);
            }
            if (pt.running !== null) {
                runningSum += pt.running;
                runningCount += 1;
            }
            totalPreemptions += pt.preemptions || 0;
            totalFailed += pt.failed_requests || 0;
        }

        const stateBreakdown = BUCKET_STATUS_ORDER.map((key) => ({
            key,
            count: counts[key] || 0,
            pct: totalBuckets > 0 ? ((counts[key] || 0) / totalBuckets) * 100 : 0,
            meta: BUCKET_STATUS_STYLE[key],
        }));

        const xMin = Math.min(...series.map((pt) => pt.start_seconds));
        const rawXMax = Math.max(...series.map((pt) => pt.end_seconds));
        const xMax = rawXMax > xMin ? rawXMax : xMin + 1;

        const saturationBands = [];
        for (const pt of series) {
            if (pt.status === 'healthy') continue;
            const prev = saturationBands[saturationBands.length - 1];
            if (prev && prev.status === pt.status && Math.abs(prev.x2 - pt.start_seconds) <= 0.5) {
                prev.x2 = Math.max(prev.x2, pt.end_seconds);
            } else {
                saturationBands.push({
                    key: `${roleKey}-${pt.status}-${pt.start_seconds}-${saturationBands.length}`,
                    status: pt.status,
                    x1: pt.start_seconds,
                    x2: pt.end_seconds,
                    color: BUCKET_STATUS_STYLE[pt.status]?.bandColor || CHART_SERIES[2],
                });
            }
        }
        for (const band of saturationBands) {
            if (band.x2 <= band.x1) {
                if (band.x1 < xMax) {
                    band.x2 = Math.min(xMax, band.x1 + 1);
                } else if (band.x1 > xMin) {
                    band.x1 = Math.max(xMin, band.x2 - 1);
                }
            }
        }

        const meanRunning = runningCount > 0 ? runningSum / runningCount : null;

        return {
            role: roleKey,
            label: roleDisplayLabel(roleKey),
            series,
            totalBuckets,
            xMin,
            xMax,
            peakWaiting,
            peakKvPct,
            totalPreemptions,
            totalFailed,
            meanRunning,
            worstStatus,
            stateBreakdown,
            saturationBands,
            hasSaturationBands: saturationBands.length > 0,
            hasRunningSeries: series.some((pt) => pt.running !== null),
            hasWaitingSeries: series.some((pt) => pt.waiting !== null),
            hasKvSeries: series.some((pt) => pt.kv_pct !== null),
        };
    };

    const preferredEngineOrder = ['prefill', 'decode', 'worker', 'workers'];
    const engineRoles = [...byRole.entries()]
        .filter(([roleKey]) => roleKey !== 'epp' && roleKey !== 'client')
        .sort(([a], [b]) => {
            const ai = preferredEngineOrder.indexOf(a);
            const bi = preferredEngineOrder.indexOf(b);
            if (ai !== -1 && bi !== -1) return ai - bi;
            if (ai !== -1) return -1;
            if (bi !== -1) return 1;
            return a.localeCompare(b);
        })
        .map(([roleKey, pts]) => summarizeRole(roleKey, pts));

    const eppSummary = byRole.has('epp') ? summarizeRole('epp', byRole.get('epp')) : null;
    const clientSummary = byRole.has('client') ? summarizeRole('client', byRole.get('client')) : null;

    return {
        engineRoles,
        eppSummary,
        clientSummary,
        hasData: engineRoles.length > 0 || Boolean(clientSummary) || Boolean(eppSummary),
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

function PercentTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => item?.dataKey && isFiniteMetricValue(item.value));
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => (
                <ChartTooltipRow key={item.dataKey} color={item.color} label={item.name} value={formatNumber(item.value)} unit="%" />
            ))}
        </ChartTooltip>
    );
}

function MsStackTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => item?.dataKey && isFiniteMetricValue(item.value));
    if (!rows.length) return null;
    const rowData = rows[0]?.payload || {};
    const category = label ?? rowData.stat ?? '';
    const title = category && category !== 'TTFT' ? `TTFT (${category})` : String(category);
    const total = rows.reduce((sum, item) => sum + Number(item.value), 0);
    const isLegacyResidual = rows.some((item) => item.dataKey === 'residual_ms');
    const hasGranularResidual = isLegacyResidual && (
        isFiniteMetricValue(rowData.epp_scheduling_ms)
        || isFiniteMetricValue(rowData.engine_overhead_ms)
        || isFiniteMetricValue(rowData.gateway_overhead_ms)
    );
    const positiveRows = isLegacyResidual ? rows : rows.filter((item) => Number(item.value) > 0);
    const displayRows = positiveRows.length > 0 ? positiveRows : rows;
    return (
        <ChartTooltip title={title}>
            {displayRows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    opacity={item.fillOpacity ?? TTFT_BAR_OPACITY_BY_KEY[item.dataKey] ?? 1}
                    label={item.name}
                    value={formatMilliseconds(item.value)}
                />
            ))}
            {hasGranularResidual && (
                <div className="mt-1 border-t border-theme-border pt-1 space-y-0.5 text-[10px]">
                    {isFiniteMetricValue(rowData.epp_scheduling_ms) && (
                        <ChartTooltipRow label="  EPP Scheduling" value={formatMilliseconds(rowData.epp_scheduling_ms)} />
                    )}
                    {isFiniteMetricValue(rowData.engine_overhead_ms) && (
                        <ChartTooltipRow label="  Server TTFT Residual" value={formatMilliseconds(rowData.engine_overhead_ms)} />
                    )}
                    {isFiniteMetricValue(rowData.gateway_overhead_ms) && (
                        <ChartTooltipRow label="  Client / Gateway / Network" value={formatMilliseconds(rowData.gateway_overhead_ms)} />
                    )}
                </div>
            )}
            <div className="mt-1 border-t border-theme-border pt-1">
                <ChartTooltipRow label="Total" value={formatMilliseconds(total)} />
            </div>
            {rowData.slice_note && (
                <p className="mt-1.5 max-w-[260px] whitespace-normal text-[10px] leading-snug text-theme-muted">
                    {rowData.slice_note}
                </p>
            )}
        </ChartTooltip>
    );
}

function formatMsAxisTick(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return '';
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '';
    return formatDecimal(numeric);
}

function StackedMsTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = [];
    const seen = new Set();
    for (const item of payload) {
        if (!item?.dataKey || !isFiniteMetricValue(item.value) || seen.has(item.dataKey)) continue;
        seen.add(item.dataKey);
        rows.push(item);
    }
    if (!rows.length) return null;
    const total = rows.reduce((sum, item) => sum + Number(item.value), 0);
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={formatMilliseconds(item.value)}
                />
            ))}
            <div className="mt-1 border-t border-theme-border pt-1">
                <ChartTooltipRow label="Total" value={formatMilliseconds(total)} />
            </div>
        </ChartTooltip>
    );
}

const SERVER_STAGE_PILL_META = [
    {
        key: 'epp_scheduling',
        msKey: 'epp_scheduling_ms',
        label: 'EPP Scheduling',
        hasTtftShare: true,
        help: 'Endpoint Picker (EPP) routing and scheduling decision latency before backend dispatch.',
        formula: 'EPP_Scheduling = HistogramQuantile(q, ΔEPP_Scheduling_Buckets / Δt)',
    },
    {
        key: 'queue_time',
        msKey: 'queue_time_ms',
        label: 'Queue Time',
        hasTtftShare: true,
        help: 'Time requests waited in the engine scheduler queue before prefill or decode execution began (excluding NIXL KV transfer wait).',
        formula: 'Queue_Time = P_Queue_Eff + max(0, D_Queue_Raw − KV_Transfer)',
    },
    {
        key: 'kv_transfer',
        msKey: 'kv_transfer_ms',
        label: 'KV Transfer (NIXL)',
        hasTtftShare: true,
        help: 'Disaggregated Prefill-to-Decode KV cache transfer duration over NIXL while waiting for remote KVs.',
        formula: 'KV_Transfer = min(NIXL_Xfer_Latency, D_Queue_Raw)',
    },
    {
        key: 'prefill_time',
        msKey: 'prefill_time_ms',
        label: 'Prefill Time',
        hasTtftShare: true,
        help: 'Engine prompt prefill computation duration up to first token generation.',
        formula: 'Prefill_Time = HistogramQuantile(q, ΔEngine_Prefill_Buckets / Δt)',
    },
    {
        key: 'engine_overhead',
        msKey: 'engine_overhead_ms',
        label: 'Server TTFT Residual',
        hasTtftShare: true,
        help: 'Server-side TTFT residual inside engine TTFT beyond measured Queue, KV Transfer, and Prefill stages (in P/D disaggregation, decomposed into Decode First-Step Compute + Prefill Engine / API Overhead + Decode Handoff / SSE Overhead in TTFT Stage Decomposition).',
        formula: 'Server_TTFT_Residual = min(Total_Residual, max(0, Server_TTFT − (Queue + KV_Transfer + Prefill)))',
    },
    {
        key: 'gateway_overhead',
        msKey: 'gateway_overhead_ms',
        label: 'Client / Gateway / Network',
        hasTtftShare: true,
        help: 'Residual difference between client-observed TTFT and reported EPP Scheduling plus engine TTFT (network, TLS, Inference Gateway, and client overhead).',
        formula: 'Client_Gateway_Network = max(0, Client_TTFT − (EPP_Scheduling + Server_TTFT_Accounted))',
    },
    {
        key: 'decode_time',
        msKey: 'decode_time_ms',
        label: 'Decode Time',
        hasTtftShare: false,
        help: 'Engine token decode generation duration across all emitted output tokens.',
        formula: 'Decode_Time = HistogramQuantile(q, ΔEngine_Decode_Buckets / Δt)',
    },
];

function TokenRateTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => item?.dataKey && isFiniteMetricValue(item.value));
    if (!rows.length) return null;
    const category = label ?? rows[0]?.payload?.category ?? '';
    return (
        <ChartTooltip title={String(category)}>
            {rows.map((item) => (
                <ChartTooltipRow key={item.dataKey} color={item.color} label={item.name} value={formatNumber(item.value)} unit=" tok/s" />
            ))}
        </ChartTooltip>
    );
}

function PodLoadTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => item?.dataKey && isFiniteMetricValue(item.value));
    if (!rows.length) return null;
    const category = label ?? rows[0]?.payload?.pod ?? '';
    return (
        <ChartTooltip title={String(category)}>
            {rows.map((item) => (
                <ChartTooltipRow key={item.dataKey} color={item.color} label={item.name} value={formatNumber(item.value)} />
            ))}
        </ChartTooltip>
    );
}

function RoleSaturationTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const point = payload[0]?.payload;
    if (!point) return null;
    const statusMeta = BUCKET_STATUS_STYLE[point.status] || BUCKET_STATUS_STYLE.healthy;
    const title = point.label || `Elapsed ${formatNumber(label)} s`;
    return (
        <ChartTooltip title={title}>
            <ChartTooltipRow label="Status" value={statusMeta.label} />
            {isFiniteMetricValue(point.running) && (
                <ChartTooltipRow color={CHART_SERIES[0]} label="Running Requests" value={formatNumber(point.running)} />
            )}
            {isFiniteMetricValue(point.waiting) && (
                <ChartTooltipRow color={CHART_SERIES[1]} label="Waiting Requests" value={formatNumber(point.waiting)} />
            )}
            {isFiniteMetricValue(point.kv_pct) && (
                <ChartTooltipRow color={CHART_SERIES[2]} label="KV Cache Usage (%)" value={formatNumber(point.kv_pct)} unit="%" />
            )}
            {isFiniteMetricValue(point.prefix_hit_pct) && (
                <ChartTooltipRow label="Prefix Cache Hit Rate (%)" value={formatNumber(point.prefix_hit_pct)} unit="%" />
            )}
            {isFiniteMetricValue(point.preemptions) && (
                <ChartTooltipRow label="Preemptions" value={formatCount(point.preemptions)} />
            )}
            {point.failed_requests > 0 && (
                <ChartTooltipRow label="Failed Requests" value={formatCount(point.failed_requests)} />
            )}
        </ChartTooltip>
    );
}

function ClientSaturationTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const point = payload[0]?.payload;
    if (!point) return null;
    const statusMeta = BUCKET_STATUS_STYLE[point.status] || BUCKET_STATUS_STYLE.healthy;
    const title = point.label || `Elapsed ${formatNumber(label)} s`;
    return (
        <ChartTooltip title={title}>
            <ChartTooltipRow label="Status" value={statusMeta.label} />
            <ChartTooltipRow color={CHART_SERIES[4]} label="Failed Requests" value={formatCount(point.failed_requests ?? 0)} />
        </ChartTooltip>
    );
}

const EMPTY_OBJ = {};

export function BottlenecksTab({ task, telemetry, onSelectTab }) {
    const [stageQuantile, setStageQuantile] = useState('p50');

    const forensics = telemetry?.forensics ?? EMPTY_OBJ;
    const serverTelemetry = telemetry?.server_telemetry ?? EMPTY_OBJ;
    const sessionsSummary = telemetry?.sessions_summary ?? EMPTY_OBJ;
    const clientSeries = telemetry?.client_series ?? EMPTY_OBJ;
    const backendOptions = task?.simulation?.backend_options ?? task?.backend_options ?? telemetry?.run_manifest?.backend_options ?? EMPTY_OBJ;

    const findings = useMemo(() => normalizeFindings(forensics), [forensics]);
    const roleSaturation = useMemo(() => buildRoleSaturationView(forensics), [forensics]);
    const warmupSeconds = firstFinite(clientSeries.warmup_seconds, backendOptions.warmup_seconds, 0) || 0;
    const dispatchDurationSeconds = firstFinite(clientSeries.dispatch_duration_seconds);
    const totalDurationSeconds = firstFinite(
        clientSeries.duration_seconds,
        clientSeries.windows?.full?.duration_seconds,
        roleSaturation.engineRoles.reduce((acc, r) => Math.max(acc, r.xMax), 0) || null,
    );
    const hasGraceDrain = dispatchDurationSeconds !== null
        && totalDurationSeconds !== null
        && (totalDurationSeconds - dispatchDurationSeconds) >= 3.0;
    const ttftDecomp = forensics.ttft_decomposition ?? EMPTY_OBJ;
    const stageView = useMemo(
        () => buildStageLatencyView(forensics, serverTelemetry, telemetry?.client_series),
        [forensics, serverTelemetry, telemetry?.client_series],
    );
    const ttftDecompView = useMemo(
        () => buildTtftDecompositionView(forensics, stageView),
        [forensics, stageView],
    );
    const ttftRows = ttftDecompView.rows;
    const isClientStageFallback = stageView.source === 'client_fallback';
    const isPdPooledStageFallback = stageView.source === 'pd_pooled_fallback';
    const stageLayers = useMemo(
        () => stageAreaLayers(stageView.source, stageView.hasKvTransfer),
        [stageView.source, stageView.hasKvTransfer],
    );
    const activeStageKeys = useMemo(() => stageLayers.map((layer) => layer.key), [stageLayers]);
    const hasP50StageData = Boolean(
        stageView.slices?.p50?.available
        || stageView.p50Series.some((pt) => activeStageKeys.some((key) => isFiniteMetricValue(pt?.[key]))),
    );
    const effectiveQuantile = (stageQuantile === 'p90' || !hasP50StageData) && stageView.hasP90 ? 'p90' : 'p50';
    const activeSliceKey = effectiveQuantile === 'p90' && stageView.slices?.p90?.available
        ? 'p90'
        : (stageView.slices?.p50?.available
            ? 'p50'
            : (stageView.slices?.mean?.available ? 'mean' : (stageView.slices?.p90?.available ? 'p90' : null)));
    const activeSlice = activeSliceKey ? stageView.slices[activeSliceKey] : null;
    const activeSliceLabel = activeSliceKey === 'mean'
        ? 'Mean'
        : (activeSliceKey ? activeSliceKey.toUpperCase() : effectiveQuantile.toUpperCase());
    const activeStageSeries = effectiveQuantile === 'p90' ? stageView.p90Series : stageView.p50Series;
    const hasActiveStageSeries = activeStageSeries.some((pt) => activeStageKeys.some((key) => isFiniteMetricValue(pt?.[key])));
    const isP90PrimarySlice = !stageView.slices?.p50?.available && !stageView.slices?.mean?.available;
    const activeDominantE2e = activeSliceKey === 'p90'
        ? (isP90PrimarySlice ? (stageView.dominantE2eStageP90 ?? stageView.dominantE2eStage) : stageView.dominantE2eStageP90)
        : stageView.dominantE2eStage;
    const activeDominantShare = activeDominantE2e
        ? activeSlice?.share_of_e2e_pct?.[activeDominantE2e] ?? null
        : null;
    const activeStageCaveat = activeSlice?.caveat ?? stageView.caveat ?? null;
    const activeReconciliation = activeSlice?.reconciliation ?? null;
    const showReconciliationNote = Boolean(
        activeReconciliation
        && isFiniteMetricValue(activeReconciliation.e2e_gap_pct)
        && Math.abs(Number(activeReconciliation.e2e_gap_pct)) >= 15,
    );
    const stageSharePills = useMemo(() => {
        if (!activeSlice) return [];
        if (isClientStageFallback) {
            const pills = [];
            if (activeSlice.ttft_stage_ms != null || activeSlice.share_of_e2e_pct?.ttft_unattributed != null) {
                pills.push({
                    key: 'ttft_unattributed',
                    label: 'Client TTFT',
                    ms: activeSlice.ttft_stage_ms,
                    e2ePct: activeSlice.share_of_e2e_pct?.ttft_unattributed ?? null,
                    ttftPct: null,
                    help: 'Observed client-side Time to First Token; internal engine queue vs prefill breakdown requires server stage histograms.',
                    formula: 'Client_TTFT = Quantile_q(t_first_token − t_request_start)',
                });
            }
            if (activeSlice.decode_time_ms != null || activeSlice.share_of_e2e_pct?.decode_time != null) {
                pills.push({
                    key: 'decode_time',
                    label: 'Decode Time (Est.)',
                    ms: activeSlice.decode_time_ms,
                    e2ePct: activeSlice.share_of_e2e_pct?.decode_time ?? null,
                    ttftPct: null,
                    help: 'Estimated Decode Time when engine stage histograms are unavailable.',
                    formula: 'Decode_Est = max(0, Client_E2E − Client_TTFT)',
                });
            }
            return pills;
        }
        if (isPdPooledStageFallback) {
            const pdPills = [
                {
                    key: 'epp_scheduling',
                    label: 'EPP Scheduling',
                    ms: activeSlice.epp_scheduling_ms ?? null,
                    e2ePct: activeSlice.share_of_e2e_pct?.epp_scheduling ?? null,
                    ttftPct: activeSlice.share_of_ttft_pct?.epp_scheduling ?? null,
                    help: 'Endpoint Picker (EPP) routing and scheduling decision latency before backend dispatch.',
                    formula: 'EPP_Scheduling = HistogramQuantile(q, ΔEPP_Scheduling_Buckets / Δt)',
                },
                {
                    key: 'kv_transfer',
                    label: 'KV Transfer (NIXL)',
                    ms: activeSlice.kv_transfer_ms ?? null,
                    e2ePct: activeSlice.share_of_e2e_pct?.kv_transfer ?? null,
                    ttftPct: activeSlice.share_of_ttft_pct?.kv_transfer ?? null,
                    help: 'Disaggregated Prefill-to-Decode KV cache transfer duration over NIXL.',
                    formula: 'KV_Transfer = min(NIXL_Xfer_Latency, max(0, Client_TTFT − EPP_Scheduling))',
                },
                {
                    key: 'ttft_unattributed',
                    label: 'P/D Engine TTFT (Queue + Prefill)',
                    ms: activeSlice.ttft_unattributed_ms ?? activeSlice.ttft_stage_ms ?? null,
                    e2ePct: activeSlice.share_of_e2e_pct?.ttft_unattributed ?? null,
                    ttftPct: activeSlice.share_of_ttft_pct?.ttft_unattributed ?? null,
                    help: 'Combined Prefill and Decode engine TTFT (queue wait, prompt prefill, and residual) when legacy telemetry pooled P/D stage histograms.',
                    formula: 'PD_Engine_TTFT = max(0, Client_TTFT − (EPP_Scheduling + KV_Transfer))',
                },
                {
                    key: 'decode_time',
                    label: 'Decode Time (Est.)',
                    ms: activeSlice.decode_time_ms ?? null,
                    e2ePct: activeSlice.share_of_e2e_pct?.decode_time ?? null,
                    ttftPct: null,
                    help: 'Estimated Decode Time derived from client E2E minus TTFT on legacy pooled P/D artifacts.',
                    formula: 'Decode_Est = max(0, Client_E2E − Client_TTFT)',
                },
            ];
            return pdPills.filter((pill) => pill.ms !== null || pill.e2ePct !== null);
        }
        return SERVER_STAGE_PILL_META
            .map((meta) => ({
                key: meta.key,
                label: meta.label,
                ms: activeSlice[meta.msKey] ?? null,
                e2ePct: activeSlice.share_of_e2e_pct?.[meta.key] ?? null,
                ttftPct: meta.hasTtftShare ? (activeSlice.share_of_ttft_pct?.[meta.key] ?? null) : null,
                help: meta.help,
                formula: meta.formula,
            }))
            .filter((pill) => pill.ms !== null || pill.e2ePct !== null);
    }, [activeSlice, isClientStageFallback, isPdPooledStageFallback]);

    // Prompt token flow & follow-up turn cache behavior
    const serverSummary = serverTelemetry.summary || EMPTY_OBJ;
    const derived = serverTelemetry.derived ?? EMPTY_OBJ;
    const metricSources = serverTelemetry.metric_sources ?? EMPTY_OBJ;
    const isPdCanonical = metricSources?.pd_canonical_metrics === 'role_separated';
    const sbrPrefill = serverTelemetry.summary_by_role?.prefill ?? EMPTY_OBJ;
    const roleSummaryPrefill = derived.role_summary?.prefill ?? EMPTY_OBJ;
    const rawPerPod = Array.isArray(serverTelemetry.per_pod) ? serverTelemetry.per_pod : [];
    const rawRoleSeries = useMemo(
        () => (Array.isArray(serverTelemetry.role_series) ? serverTelemetry.role_series : []),
        [serverTelemetry.role_series],
    );
    const roleByTs = useMemo(() => {
        const map = new Map();
        for (const rp of rawRoleSeries) {
            if (rp?.timestamp != null && rp.timestamp !== '') {
                map.set(rp.timestamp, rp);
            }
        }
        return map;
    }, [rawRoleSeries]);
    const rawPromptByRoleRows = Array.isArray(derived.prompt_tokens_by_role_source)
        ? derived.prompt_tokens_by_role_source
        : [];
    const podSkew = forensics.pod_skew ?? EMPTY_OBJ;
    const rawTopologyMode = String(
        serverTelemetry?.topology_mode
        || serverTelemetry?.topology?.mode
        || telemetry?.run_manifest?.target?.topology_mode
        || 'aggregated',
    ).toLowerCase();
    const hasPrefillEvidence = rawPerPod.some((p) => !isBenchmarkRunnerPod(p) && effectivePodRole(p) === 'prefill')
        || rawRoleSeries.some((rp) => rp && Object.keys(rp).some((k) => k.startsWith('prefill_') && isFiniteMetricValue(rp[k])))
        || Boolean(serverTelemetry?.summary_by_role?.prefill && Object.keys(serverTelemetry.summary_by_role.prefill).length)
        || Boolean(derived?.role_summary?.prefill && Object.keys(derived.role_summary.prefill).length)
        || rawPromptByRoleRows.some((r) => String(r?.role || '').trim().toLowerCase() === 'prefill' && Number(r?.total_tokens || 0) > 0)
        || (Array.isArray(podSkew.pods) && podSkew.pods.some((p) => String(p?.role || '').trim().toLowerCase() === 'prefill'));
    const isDisaggregated = rawTopologyMode === 'disaggregated' && hasPrefillEvidence;

    const prefillSourceRow = rawPromptByRoleRows.find(
        (r) => String(r?.role || '').trim().toLowerCase() === 'prefill' && Number(r?.total_tokens || 0) > 0,
    );
    const decodeSourceRow = rawPromptByRoleRows.find(
        (r) => String(r?.role || '').trim().toLowerCase() === 'decode' && Number(r?.total_tokens || 0) > 0,
    );
    const prefillLocalCacheShare = (prefillSourceRow && decodeSourceRow
        && (Number(prefillSourceRow.local_cache_tokens || 0) + Number(decodeSourceRow.local_cache_tokens || 0)) > 0)
        ? Number(prefillSourceRow.local_cache_tokens || 0)
            / (Number(prefillSourceRow.local_cache_tokens || 0) + Number(decodeSourceRow.local_cache_tokens || 0))
        : 1.0;

    const routingVsCache = forensics.routing_vs_cache ?? EMPTY_OBJ;
    let cachedTps = 0;
    let freshTps = 0;
    if (isDisaggregated) {
        if (sbrPrefill.engine_prompt_local_cache_tps?.mean != null || sbrPrefill.engine_prompt_external_tps?.mean != null) {
            cachedTps = Number(sbrPrefill.engine_prompt_local_cache_tps?.mean ?? 0)
                + Number(sbrPrefill.engine_prompt_external_tps?.mean ?? 0);
        } else if (routingVsCache.role_scope === 'prefill' && routingVsCache.engine_cached_tok_s != null) {
            cachedTps = Number(routingVsCache.engine_cached_tok_s);
        } else if (isPdCanonical) {
            cachedTps = Number(
                serverSummary.engine_prompt_local_cache_tps?.mean
                ?? routingVsCache.engine_cached_tok_s
                ?? forensics.cache_behavior?.cached_tps
                ?? 0,
            );
        } else {
            const rawCached = firstFinite(
                serverSummary.engine_prompt_local_cache_tps?.mean,
                routingVsCache.engine_cached_tok_s,
                forensics.cache_behavior?.cached_tps,
            );
            cachedTps = rawCached !== null ? rawCached * prefillLocalCacheShare : 0;
        }
        const prefillFresh = firstFinite(
            sbrPrefill.engine_prompt_local_compute_tps?.mean,
            routingVsCache.role_scope === 'prefill' ? routingVsCache.engine_fresh_tok_s : null,
            serverSummary.engine_prompt_local_compute_tps?.mean,
            routingVsCache.engine_fresh_tok_s,
            forensics.cache_behavior?.fresh_tps,
        ) ?? 0;
        const prefillRecomp = firstFinite(
            sbrPrefill.engine_prompt_recomputed_tps?.mean,
            routingVsCache.role_scope === 'prefill' ? routingVsCache.engine_recomputed_tok_s : null,
            serverSummary.engine_prompt_recomputed_tps?.mean,
            routingVsCache.engine_recomputed_tok_s,
        ) ?? 0;
        freshTps = prefillFresh + prefillRecomp;
    } else {
        cachedTps = Number(
            routingVsCache.engine_cached_tok_s
            ?? serverSummary.engine_prompt_local_cache_tps?.mean
            ?? forensics.cache_behavior?.cached_tps
            ?? 0,
        ) + Number(serverSummary.engine_prompt_external_tps?.mean ?? forensics.cache_behavior?.external_cached_tps ?? 0);
        freshTps = Number(
            routingVsCache.engine_fresh_tok_s
            ?? serverSummary.engine_prompt_local_compute_tps?.mean
            ?? forensics.cache_behavior?.fresh_tps
            ?? serverTelemetry.derived?.recomputed_token_rate_tps
            ?? 0,
        ) + Number(routingVsCache.engine_recomputed_tok_s ?? 0);
    }
    const hasPromptBreakdown = cachedTps > 0 || freshTps > 0;
    const promptFlowData = hasPromptBreakdown
        ? [{ category: 'Prompt Token Throughput', cached_tps: cachedTps, fresh_tps: freshTps }]
        : [];

    const turnCacheStats = forensics.cache_behavior?.follow_up_turns
        || sessionsSummary.followup_turn_stats
        || sessionsSummary.follow_up_turns
        || sessionsSummary.turn_cache_summary
        || null;

    // Router / EPP cache view vs vLLM reality
    const originEpoch = telemetry?.client_series?.origin_epoch;
    const executionStartedAt = telemetry?.run_manifest?.execution_started_at;
    const routingSeries = useMemo(() => {
        const computeEppHitPct = (pt) => {
            if (
                pt?.router_input_token_rate_tps != null
                && pt.router_input_token_rate_tps !== ''
                && pt?.router_cached_token_rate_tps != null
                && pt.router_cached_token_rate_tps !== ''
            ) {
                const rInput = Number(pt.router_input_token_rate_tps);
                const rCached = Number(pt.router_cached_token_rate_tps);
                if (Number.isFinite(rInput) && rInput > 0 && Number.isFinite(rCached)) {
                    return Math.min(100, Math.max(0, (rCached / rInput) * 100));
                }
            }
            return null;
        };
        const raw = Array.isArray(routingVsCache?.series)
            ? routingVsCache.series
            : Array.isArray(routingVsCache)
                ? routingVsCache
                : [];
        if (raw.length) {
            return raw.map((pt, idx) => {
                const rolePt = (pt.timestamp != null && pt.timestamp !== '' ? roleByTs.get(pt.timestamp) : null)
                    || (rawRoleSeries.length === raw.length ? rawRoleSeries[idx] : null)
                    || EMPTY_OBJ;
                const rawVllm = pt.vllm_hit_pct ?? pt.vllm_prefix_hit_pct ?? pt.vllm_actual_hit_pct ?? pt.engine_hit_pct ?? null;
                const healedVllm = isDisaggregated && routingVsCache.role_scope !== 'prefill'
                    ? firstFinite(rolePt.prefill_prefix_cache_hit_percent, rawVllm)
                    : rawVllm;
                return {
                    t: Number(pt.time_seconds ?? pt.elapsed_seconds ?? pt.t ?? idx),
                    epp_hit_pct: clampPercent(pt.epp_hit_pct ?? pt.epp_cache_hit_pct ?? pt.epp_expected_hit_pct ?? pt.router_hit_pct ?? computeEppHitPct(pt)),
                    vllm_hit_pct: clampPercent(healedVllm),
                };
            });
        }
        const serverSeries = Array.isArray(serverTelemetry.series) ? serverTelemetry.series : [];
        const originCandidate = (originEpoch ? originEpoch * 1000 : null)
            ?? (serverTelemetry?.window?.start ? Date.parse(serverTelemetry.window.start) : null)
            ?? (executionStartedAt ? Date.parse(executionStartedAt) : null)
            ?? (serverSeries[0]?.timestamp ? Date.parse(serverSeries[0].timestamp) : null);
        return serverSeries
            .map((pt, idx) => {
                const rolePt = (pt.timestamp != null && pt.timestamp !== '' ? roleByTs.get(pt.timestamp) : null)
                    || rawRoleSeries[idx]
                    || EMPTY_OBJ;
                const parsedTs = pt.timestamp ? Date.parse(pt.timestamp) : NaN;
                const t = Number.isFinite(Number(pt.elapsed_seconds ?? pt.time_seconds))
                    ? Number(pt.elapsed_seconds ?? pt.time_seconds)
                    : (Number.isFinite(parsedTs) && Number.isFinite(originCandidate) ? Math.max(0, (parsedTs - originCandidate) / 1000) : idx);
                const rawHit = isDisaggregated
                    ? firstFinite(rolePt.prefill_prefix_cache_hit_percent, pt.prefix_cache_hit_percent)
                    : pt.prefix_cache_hit_percent;
                const vllmHitPct = rawHit
                    ?? (pt.prefix_cache_hit_ratio != null && pt.prefix_cache_hit_ratio !== '' && Number.isFinite(Number(pt.prefix_cache_hit_ratio))
                        ? Number(pt.prefix_cache_hit_ratio) * 100
                        : null);
                return {
                    t,
                    epp_hit_pct: clampPercent(pt.epp_cache_hit_percent ?? pt.router_avoided_prefill_ratio ?? computeEppHitPct(pt)),
                    vllm_hit_pct: clampPercent(vllmHitPct),
                };
            })
            .filter((pt) => pt.epp_hit_pct !== null || pt.vllm_hit_pct !== null);
    }, [routingVsCache, serverTelemetry, originEpoch, executionStartedAt, isDisaggregated, roleByTs, rawRoleSeries]);

    const prefillRoleHitPctFromSource = prefillSourceRow && Number(prefillSourceRow.total_tokens) > 0
        ? ((Number(prefillSourceRow.local_cache_tokens || 0) + Number(prefillSourceRow.external_kv_tokens || 0))
            / Number(prefillSourceRow.total_tokens)) * 100
        : null;
    const eppExpectedHitPct = firstFinite(routingVsCache.epp_expected_hit_pct);
    const vllmActualHitPct = isDisaggregated && routingVsCache.role_scope !== 'prefill'
        ? firstFinite(
            sbrPrefill.prefix_cache_hit_percent?.mean,
            prefillRoleHitPctFromSource,
            roleSummaryPrefill.prefix_cache_hit_percent,
            meanSeriesValue(rawRoleSeries, 'prefill_prefix_cache_hit_percent'),
            routingVsCache.vllm_actual_hit_pct,
        )
        : firstFinite(routingVsCache.vllm_actual_hit_pct);
    const routingGapPct = isDisaggregated && routingVsCache.role_scope !== 'prefill'
        ? (eppExpectedHitPct !== null && vllmActualHitPct !== null ? eppExpectedHitPct - vllmActualHitPct : firstFinite(routingVsCache.gap_pct))
        : firstFinite(routingVsCache.gap_pct, eppExpectedHitPct !== null && vllmActualHitPct !== null ? eppExpectedHitPct - vllmActualHitPct : null);

    const hasRoutingComparison = routingSeries.some((pt) => pt.epp_hit_pct !== null || pt.vllm_hit_pct !== null)
        || (eppExpectedHitPct != null || vllmActualHitPct != null);

    // Per-Pod Load & P/D Skew
    const podRows = useMemo(() => {
        if (Array.isArray(podSkew.pods) && podSkew.pods.length > 0) {
            return podSkew.pods.map((pod) => ({
                ...pod,
                avg_running: pod.running_mean ?? pod.avg_running ?? null,
                avg_waiting: pod.waiting_mean ?? pod.avg_waiting ?? null,
                peak_kv_pct: pod.kv_cache_peak_pct ?? pod.peak_kv_pct ?? null,
                prefix_hit_pct: pod.prefix_cache_hit_pct ?? pod.prefix_hit_pct ?? null,
            }));
        }
        const perPod = Array.isArray(serverTelemetry.per_pod) ? serverTelemetry.per_pod : [];
        return perPod.map((pod) => ({
            pod: pod.pod,
            role: pod.role || 'worker',
            request_rate_rps: pod.request_rate_rps?.mean ?? null,
            avg_running: pod.running_requests?.mean ?? null,
            avg_waiting: pod.waiting_requests?.mean ?? null,
            peak_kv_pct: pod.kv_cache_usage_percent?.max ?? null,
            prefix_hit_pct: pod.prefix_cache_hit_percent?.mean ?? null,
        }));
    }, [podSkew.pods, serverTelemetry.per_pod]);

    const skewRatio = podSkew.skew_ratio ?? null;

    const podChartData = useMemo(
        () => podRows.slice(0, 12).map((row) => ({
            pod: row.pod,
            request_rate_rps: Number(row.request_rate_rps ?? 0),
            avg_waiting: Number(row.avg_waiting ?? 0),
        })),
        [podRows],
    );

    return (
        <div className="space-y-6">
            {/* Ranked Findings Cards */}
            <Panel
                title="Ranked Bottleneck Findings"
                tone="subtle"
                help="Automated diagnostic findings ranked by severity (Critical to Info) using client latency, queueing, KV cache, and routing rules."
                helpFormula="Findings = Sort_by_Severity(Evaluate_Rules(Client_SLOs, Stage_Breakdown, KV_Cache, Pod_Skew))"
                helpAlign="start"
            >
                {findings.length > 0 ? (
                    <div className="space-y-3">
                        {findings.map((finding, idx) => {
                            const jumpTab = finding.tab ?? finding.target_tab ?? null;
                            const roleBadge = findingRoleBadge(finding.role);
                            const evidenceList = Array.isArray(finding.evidence) && finding.evidence.length > 0
                                ? finding.evidence
                                : (finding.metric && finding.value != null ? [{ metric: finding.metric, value: finding.value }] : []);
                            return (
                                <div
                                    key={finding.id || finding.code || idx}
                                    className="rounded-xl border border-theme-border bg-theme-card p-4 space-y-2"
                                >
                                    <div className="flex flex-wrap items-center justify-between gap-2">
                                        <div className="flex flex-wrap items-center gap-2">
                                            <span className="inline-flex items-center gap-1">
                                                <Badge tone={severityTone(finding.severity)} size="sm">
                                                    {displayName(finding.severity || 'info')}
                                                </Badge>
                                                <HelpTooltip
                                                    label="Finding severity help"
                                                    align="start"
                                                    iconSize={12}
                                                    formula="Severity ∈ {Critical, Warning, Info} from forensic threshold rules"
                                                >
                                                    Forensic severity rank (Critical, Warning, or Info) reflecting how strongly this bottleneck degrades SLO attainment or stability.
                                                </HelpTooltip>
                                            </span>
                                            {finding.category && (
                                                <Badge tone="neutral" size="sm">
                                                    {displayName(finding.category)}
                                                </Badge>
                                            )}
                                            {roleBadge && (
                                                <Badge tone={roleBadge.tone} size="sm">
                                                    {roleBadge.label}
                                                </Badge>
                                            )}
                                            <h4 className="text-sm font-semibold text-theme-text">
                                                {finding.title || displayName(finding.code || `Finding ${idx + 1}`)}
                                            </h4>
                                        </div>
                                        {jumpTab && onSelectTab && (
                                            <Button
                                                variant="ghost"
                                                size="xs"
                                                onClick={() => onSelectTab(jumpTab, finding.anchor || null)}
                                            >
                                                Open {displayName(jumpTab)}
                                                <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
                                            </Button>
                                        )}
                                    </div>
                                    {finding.description && (
                                        <p className="text-xs leading-relaxed text-theme-muted">{finding.description}</p>
                                    )}
                                    {evidenceList.length > 0 && (
                                        <div className="flex flex-wrap items-center gap-2 pt-1">
                                            <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-theme-muted">
                                                <span>Evidence:</span>
                                                <HelpTooltip
                                                    label="Finding evidence help"
                                                    align="start"
                                                    iconSize={11}
                                                    formula="Observed_Metric vs Forensic_Rule_Threshold"
                                                >
                                                    Measured client or Prometheus telemetry values that crossed the rule threshold and triggered this finding.
                                                </HelpTooltip>
                                            </span>
                                            {evidenceList.map((ev, evIdx) => (
                                                <span
                                                    key={evIdx}
                                                    className="rounded border border-theme-border bg-slate-100/70 dark:bg-slate-900/60 px-2 py-0.5 font-mono text-[11px] text-theme-text"
                                                >
                                                    {typeof ev === 'string' ? ev : `${ev.metric}: ${ev.value}`}
                                                </span>
                                            ))}
                                        </div>
                                    )}
                                    {finding.recommendation && (
                                        <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-3 py-2 text-xs text-theme-text">
                                            <span className="font-semibold text-emerald-700 dark:text-emerald-300">Recommendation: </span>
                                            {finding.recommendation}
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                ) : (
                    <EmptyState
                        icon={<CheckCircle2 className="h-6 w-6" />}
                        tone="success"
                        title="No critical bottlenecks detected"
                        message="All evaluated bottleneck rules were within nominal thresholds for this run."
                    />
                )}
            </Panel>

            {/* Per-Role Saturation Timeline */}
            <Panel
                title="Per-Role Saturation Timeline"
                help="Time-series saturation view across serving engine roles (Prefill, Decode, or Aggregated Worker) from Prometheus telemetry, plotting running vs. waiting requests, KV cache usage (%), shaded pressure windows, and time-in-state share."
                helpFormula="Bucket_Status = Classify(Failed_Requests, Preemption_Rate, Max_KV_Usage_%, Waiting_Mean)"
                helpAlign="start"
                actions={(
                    <div className="flex flex-wrap items-center gap-3 text-[11px] text-theme-muted">
                        {Object.entries(BUCKET_STATUS_STYLE).map(([statusKey, meta]) => (
                            <span key={statusKey} className="inline-flex items-center gap-1">
                                <span className={cn('h-2.5 w-2.5 rounded-sm', meta.dot)} aria-hidden="true" />
                                <span>{meta.label}</span>
                                <HelpTooltip label={`${meta.label} state help`} align="end" iconSize={11} formula={meta.formula}>
                                    {meta.help}
                                </HelpTooltip>
                            </span>
                        ))}
                    </div>
                )}
            >
                {roleSaturation.engineRoles.length > 0 ? (
                    <div className="space-y-4">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex items-center gap-1.5 text-[11px] font-semibold text-theme-muted">
                                <span>Engine Saturation by Serving Role</span>
                                <HelpTooltip label="Serving role help" align="start" iconSize={11}>
                                    Deployment component role (Endpoint Picker (EPP), Prefill, Decode, or Aggregated Worker) evaluated across elapsed time windows.
                                </HelpTooltip>
                            </div>
                            {roleSaturation.eppSummary && (
                                <div className="flex flex-wrap items-center gap-2">
                                    <Badge
                                        tone={roleSaturation.eppSummary.totalFailed > 0 || roleSaturation.eppSummary.worstStatus === 'error' ? 'danger' : 'success'}
                                        size="sm"
                                    >
                                        {roleSaturation.eppSummary.totalFailed > 0
                                            ? `EPP Router: ${formatCount(roleSaturation.eppSummary.totalFailed)} failed reqs`
                                            : roleSaturation.eppSummary.worstStatus === 'error'
                                                ? 'EPP Router: Error'
                                                : 'EPP Router: Healthy (0 failed)'}
                                    </Badge>
                                    {roleSaturation.eppSummary.meanRunning !== null && roleSaturation.eppSummary.meanRunning > 0 && (
                                        <span className="inline-flex items-center gap-1 text-[11px] font-mono text-theme-muted">
                                            <span>Mean EPP load: {formatNumber(roleSaturation.eppSummary.meanRunning)}</span>
                                            <HelpTooltip
                                                label="EPP router load help"
                                                align="end"
                                                iconSize={11}
                                                formula="EPP_Load = mean_t (EPP_Request_Rate(t) | EPP_InFlight(t))"
                                            >
                                                Mean request rate or in-flight load observed on the Endpoint Picker (EPP) router across timeline buckets.
                                            </HelpTooltip>
                                        </span>
                                    )}
                                </div>
                            )}
                        </div>

                        <div
                            className={cn(
                                'grid min-w-0 gap-6',
                                roleSaturation.engineRoles.length > 1 ? 'xl:grid-cols-2' : 'grid-cols-1',
                            )}
                        >
                            {roleSaturation.engineRoles.map((role) => {
                                const worstMeta = BUCKET_STATUS_STYLE[role.worstStatus] || BUCKET_STATUS_STYLE.healthy;
                                const activeStates = role.stateBreakdown.filter((item) => item.count > 0);
                                const xSpan = role.xMax > role.xMin ? role.xMax - role.xMin : 0;
                                const showWarmup = warmupSeconds > role.xMin && warmupSeconds < role.xMax;
                                const showDrain = hasGraceDrain
                                    && dispatchDurationSeconds > role.xMin
                                    && dispatchDurationSeconds < role.xMax;
                                const drainDy = showWarmup && showDrain && xSpan > 0 && warmupSeconds > dispatchDurationSeconds && ((warmupSeconds - dispatchDurationSeconds) / xSpan) < 0.24
                                    ? -4
                                    : -18;
                                const chartMargin = {
                                    top: showWarmup || showDrain ? 22 : 10,
                                    right: showDrain ? 44 : 12,
                                    left: 0,
                                    bottom: 8,
                                };

                                return (
                                    <div
                                        key={role.role}
                                        className="min-w-0 rounded-xl border border-theme-border bg-theme-card p-4 space-y-4"
                                    >
                                        <div className="flex flex-wrap items-start justify-between gap-3">
                                            <div className="flex min-w-0 flex-wrap items-center gap-2">
                                                <h4 className="text-sm font-semibold text-theme-text">{role.label}</h4>
                                                <Badge tone={worstMeta.badgeTone} size="xs">
                                                    {worstMeta.label}
                                                </Badge>
                                                {role.totalFailed > 0 && (
                                                    <Badge tone="danger" size="xs">
                                                        Failed: {formatCount(role.totalFailed)}
                                                    </Badge>
                                                )}
                                            </div>
                                            <div className="flex flex-wrap items-center gap-3 text-[11px] text-theme-muted">
                                                <span className="inline-flex items-center gap-1">
                                                    <span>
                                                        Peak Waiting:{' '}
                                                        <strong className="font-mono text-theme-text">
                                                            {role.peakWaiting !== null ? `${formatNumber(role.peakWaiting)} reqs` : '—'}
                                                        </strong>
                                                    </span>
                                                    <HelpTooltip
                                                        label={`${role.label} peak waiting help`}
                                                        align="end"
                                                        iconSize={11}
                                                        formula="Peak_Waiting = max_t (Waiting_Requests(t))"
                                                    >
                                                        Maximum number of requests queued waiting for scheduler admission or KV blocks across all buckets for this role.
                                                    </HelpTooltip>
                                                </span>
                                                <span className="inline-flex items-center gap-1">
                                                    <span>
                                                        Peak KV Cache:{' '}
                                                        <strong className="font-mono text-theme-text">
                                                            {formatNumber(role.peakKvPct, '%')}
                                                        </strong>
                                                    </span>
                                                    <HelpTooltip
                                                        label={`${role.label} peak KV cache help`}
                                                        align="end"
                                                        iconSize={11}
                                                        formula="Peak_KV_Usage (%) = max_t ((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100)"
                                                    >
                                                        Peak device KV cache occupancy (%) observed for this role across all time buckets.
                                                    </HelpTooltip>
                                                </span>
                                                <span className="inline-flex items-center gap-1">
                                                    <span>
                                                        Preemptions:{' '}
                                                        <strong className="font-mono text-theme-text">
                                                            {formatCount(role.totalPreemptions)}
                                                        </strong>
                                                    </span>
                                                    <HelpTooltip
                                                        label={`${role.label} preemptions help`}
                                                        align="end"
                                                        iconSize={11}
                                                        formula="Preemptions = Σ (Preemption_Rate(/s) × Δt_s)"
                                                    >
                                                        Total requests preempted or evicted due to KV cache pressure in this role.
                                                    </HelpTooltip>
                                                </span>
                                            </div>
                                        </div>

                                        {/* Proportional Time-in-State Segmented Progress Bar + Active Percentage Chips */}
                                        <div className="space-y-2">
                                            <div
                                                className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-200/70 dark:bg-slate-800/80"
                                                role="img"
                                                aria-label={`${role.label} time-in-state distribution`}
                                            >
                                                {activeStates.map((item) => (
                                                    <div
                                                        key={item.key}
                                                        className={cn('h-full transition-all', item.meta.bar)}
                                                        style={{ width: `${item.pct}%` }}
                                                        title={`${item.meta.label}: ${formatNumber(item.pct, '%')} (${item.count}/${role.totalBuckets} buckets)`}
                                                    />
                                                ))}
                                            </div>
                                            <div className="flex flex-wrap items-center gap-2">
                                                {activeStates.map((item) => (
                                                    <span
                                                        key={item.key}
                                                        className={cn(
                                                            'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium',
                                                            item.meta.chip,
                                                        )}
                                                        title={`${item.meta.label}: ${item.count} of ${role.totalBuckets} buckets`}
                                                    >
                                                        <span className={cn('h-2 w-2 rounded-full', item.meta.dot)} aria-hidden="true" />
                                                        <span>{item.meta.label}</span>
                                                        <span className="font-mono font-semibold">{formatNumber(item.pct, '%')}</span>
                                                    </span>
                                                ))}
                                            </div>
                                        </div>

                                        {/* Dual-Axis Quantitative Saturation ComposedChart */}
                                        <div className="h-56 w-full">
                                            <ResponsiveContainer width="100%" height="100%">
                                                <ComposedChart data={role.series} margin={chartMargin}>
                                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                    <XAxis
                                                        dataKey="t"
                                                        type="number"
                                                        domain={[role.xMin, role.xMax]}
                                                        tickFormatter={(v) => `${formatNumber(v)}s`}
                                                        tick={AXIS_TICK}
                                                        stroke="var(--border-color)"
                                                    />
                                                    <YAxis
                                                        yAxisId="left"
                                                        width={48}
                                                        allowDecimals={false}
                                                        domain={[0, (max) => Math.max(1, Math.ceil((Number.isFinite(max) ? max : 0) * 1.15))]}
                                                        tick={AXIS_TICK}
                                                        stroke="var(--border-color)"
                                                        label={{ value: 'Requests', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                                    />
                                                    <YAxis
                                                        yAxisId="right"
                                                        orientation="right"
                                                        width={44}
                                                        domain={[0, 100]}
                                                        ticks={[0, 25, 50, 75, 100]}
                                                        tick={AXIS_TICK}
                                                        stroke="var(--border-color)"
                                                        label={{ value: 'KV %', angle: 90, position: 'insideRight', fill: 'var(--text-muted)', fontSize: 10 }}
                                                    />
                                                    <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<RoleSaturationTooltip />} />
                                                    {role.saturationBands.map((band) => (
                                                        <ReferenceArea
                                                            key={band.key}
                                                            yAxisId="left"
                                                            x1={band.x1}
                                                            x2={band.x2}
                                                            fill={band.color}
                                                            fillOpacity={0.14}
                                                            ifOverflow="hidden"
                                                        />
                                                    ))}
                                                    {showWarmup && (
                                                        <ReferenceLine
                                                            yAxisId="left"
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
                                                    {showDrain && (
                                                        <ReferenceLine
                                                            yAxisId="left"
                                                            x={dispatchDurationSeconds}
                                                            stroke="var(--text-muted)"
                                                            strokeDasharray="4 3"
                                                            label={{
                                                                value: 'Drain',
                                                                position: 'insideTopLeft',
                                                                fill: 'var(--text-muted)',
                                                                fontSize: 10,
                                                                dy: drainDy,
                                                            }}
                                                        />
                                                    )}
                                                    <ReferenceLine
                                                        yAxisId="right"
                                                        y={90}
                                                        stroke="var(--text-muted)"
                                                        strokeDasharray="5 4"
                                                        label={{ value: 'KV 90%', position: 'insideTopRight', fill: 'var(--text-muted)', fontSize: 10 }}
                                                    />
                                                    <Area
                                                        yAxisId="left"
                                                        type="monotone"
                                                        dataKey="running"
                                                        name="Running Requests"
                                                        stroke={CHART_SERIES[0]}
                                                        fill={CHART_SERIES[0]}
                                                        fillOpacity={0.14}
                                                        strokeWidth={2}
                                                        connectNulls
                                                        dot={sparseDotProp(role.series, 'running', CHART_SERIES[0])}
                                                        isAnimationActive={false}
                                                    />
                                                    <Area
                                                        yAxisId="left"
                                                        type="monotone"
                                                        dataKey="waiting"
                                                        name="Waiting Requests"
                                                        stroke={CHART_SERIES[1]}
                                                        fill={CHART_SERIES[1]}
                                                        fillOpacity={0.22}
                                                        strokeWidth={2.25}
                                                        connectNulls
                                                        dot={sparseDotProp(role.series, 'waiting', CHART_SERIES[1])}
                                                        isAnimationActive={false}
                                                    />
                                                    <Line
                                                        yAxisId="right"
                                                        type="monotone"
                                                        dataKey="kv_pct"
                                                        name="KV Cache Usage (%)"
                                                        stroke={CHART_SERIES[2]}
                                                        strokeWidth={2}
                                                        connectNulls
                                                        dot={sparseDotProp(role.series, 'kv_pct', CHART_SERIES[2])}
                                                        isAnimationActive={false}
                                                    />
                                                </ComposedChart>
                                            </ResponsiveContainer>
                                        </div>

                                        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                                            <ChartLegend
                                                entries={[
                                                    { label: 'Running Requests', color: CHART_SERIES[0] },
                                                    { label: 'Waiting Requests', color: CHART_SERIES[1] },
                                                    { label: 'KV Cache Usage (%)', color: CHART_SERIES[2] },
                                                ]}
                                            />
                                            {role.hasSaturationBands && (
                                                <span className="text-[11px] text-theme-muted">
                                                    Shaded windows highlight non-healthy pressure intervals
                                                </span>
                                            )}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                ) : roleSaturation.clientSummary ? (
                    <div className="space-y-4">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex items-center gap-1.5 text-[11px] font-semibold text-theme-muted">
                                <span>Client-Only Saturation Fallback</span>
                                <HelpTooltip label="Serving role help" align="start" iconSize={11}>
                                    Deployment component role (Endpoint Picker (EPP), Prefill, Decode, or Aggregated Worker). Only client telemetry was available for this run.
                                </HelpTooltip>
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                <Badge
                                    tone={roleSaturation.clientSummary.totalFailed > 0 ? 'danger' : 'success'}
                                    size="xs"
                                >
                                    {roleSaturation.clientSummary.totalFailed > 0
                                        ? `Failed Requests: ${formatCount(roleSaturation.clientSummary.totalFailed)}`
                                        : 'Client: Healthy (0 failed)'}
                                </Badge>
                            </div>
                        </div>

                        <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                            <span>
                                Server queue depth and KV cache saturation telemetry require Prometheus. Showing client-observed failed request windows over elapsed time.
                            </span>
                        </div>

                        <div className="space-y-2">
                            <div
                                className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-200/70 dark:bg-slate-800/80"
                                role="img"
                                aria-label="Client time-in-state distribution"
                            >
                                {roleSaturation.clientSummary.stateBreakdown
                                    .filter((item) => item.count > 0)
                                    .map((item) => (
                                        <div
                                            key={item.key}
                                            className={cn('h-full transition-all', item.meta.bar)}
                                            style={{ width: `${item.pct}%` }}
                                            title={`${item.meta.label}: ${formatNumber(item.pct, '%')} (${item.count}/${roleSaturation.clientSummary.totalBuckets} buckets)`}
                                        />
                                    ))}
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                {roleSaturation.clientSummary.stateBreakdown
                                    .filter((item) => item.count > 0)
                                    .map((item) => (
                                        <span
                                            key={item.key}
                                            className={cn(
                                                'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium',
                                                item.meta.chip,
                                            )}
                                        >
                                            <span className={cn('h-2 w-2 rounded-full', item.meta.dot)} aria-hidden="true" />
                                            <span>{item.meta.label}</span>
                                            <span className="font-mono font-semibold">{formatNumber(item.pct, '%')}</span>
                                        </span>
                                    ))}
                            </div>
                        </div>

                        <div className="h-44 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <BarChart data={roleSaturation.clientSummary.series} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                    <XAxis
                                        dataKey="t"
                                        type="number"
                                        domain={[roleSaturation.clientSummary.xMin, roleSaturation.clientSummary.xMax]}
                                        tickFormatter={(v) => `${formatNumber(v)}s`}
                                        tick={AXIS_TICK}
                                        stroke="var(--border-color)"
                                    />
                                    <YAxis
                                        width={48}
                                        allowDecimals={false}
                                        domain={[0, (max) => Math.max(1, Math.ceil((Number.isFinite(max) ? max : 0) * 1.15))]}
                                        tick={AXIS_TICK}
                                        stroke="var(--border-color)"
                                        label={{ value: 'Failed', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                    />
                                    <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<ClientSaturationTooltip />} />
                                    <Bar
                                        dataKey="failed_requests"
                                        name="Failed Requests"
                                        fill={CHART_SERIES[4]}
                                        radius={[6, 6, 0, 0]}
                                        maxBarSize={40}
                                        isAnimationActive={false}
                                    />
                                </BarChart>
                            </ResponsiveContainer>
                        </div>
                    </div>
                ) : (
                    <EmptyState
                        icon={<Layers className="h-6 w-6" />}
                        title="Per-role saturation timeline unavailable"
                        message="Per-role saturation timeline buckets require Prometheus server telemetry."
                    />
                )}
            </Panel>

            <div className="grid gap-6 xl:grid-cols-2">
                {/* Request Stage Latency Breakdown */}
                <div id="b-stage-breakdown" className="scroll-mt-16">
                    <ChartContainer
                        title="Request Stage Latency Breakdown"
                        subtitle={
                            isClientStageFallback
                                ? 'Client TTFT (Unattributed) + Estimated Decode Time (server stage histograms unavailable)'
                                : isPdPooledStageFallback
                                    ? 'EPP Scheduling + KV Transfer (NIXL) + P/D Engine TTFT + Estimated Decode Time'
                                    : (() => {
                                        const stageNames = stageLayers.map((layer) => layer.label).join(' + ');
                                        return `Stacked ${effectiveQuantile === 'p90' ? 'P90' : 'P50'} stage latencies (${stageNames})`;
                                    })()
                        }
                        help="Stacked stage latencies over elapsed time (ms). Toggle P50/P90 to separate typical vs tail bottlenecks across EPP Scheduling, Queue Time, optional KV Transfer (NIXL), Prefill Time, and Decode Time."
                        helpFormula="Total_Stage = EPP_Scheduling + Queue_Time + KV_Transfer + Prefill_Time + Server_TTFT_Residual + Client_Gateway_Network + Decode_Time"
                        helpAlign="start"
                        actions={(stageView.available || (stageView.source === 'server_telemetry' && Boolean(onSelectTab))) && (
                            <div className="flex flex-wrap items-center gap-2">
                                {activeDominantE2e && (
                                    <span className="inline-flex items-center gap-1">
                                        <Badge
                                            tone={['queue_time', 'kv_transfer', 'gateway_overhead', 'engine_overhead', 'epp_scheduling'].includes(activeDominantE2e) ? 'warning' : 'info'}
                                            size="xs"
                                        >
                                            Dominant: {formatStageName(activeDominantE2e, stageView.source)}
                                            {activeDominantShare != null ? ` · ${formatNumber(activeDominantShare, '%')}` : ''}
                                        </Badge>
                                        <HelpTooltip
                                            label="Dominant stage help"
                                            align="end"
                                            iconSize={11}
                                            formula="Dominant = argmax_stage (Stage_Latency / Total_Stage_Latency)"
                                        >
                                            Stage contributing the largest share of total stage latency for the selected percentile.
                                        </HelpTooltip>
                                    </span>
                                )}
                                {stageView.hasP90 && (
                                    <ToggleGroup
                                        size="xs"
                                        value={effectiveQuantile}
                                        onChange={setStageQuantile}
                                        options={[
                                            { value: 'p50', label: 'P50', disabled: !hasP50StageData },
                                            { value: 'p90', label: 'P90' },
                                        ]}
                                    />
                                )}
                                {stageView.source === 'server_telemetry' && onSelectTab && (
                                    <Button
                                        variant="ghost"
                                        size="xs"
                                        onClick={() => onSelectTab('serving', 's-latency')}
                                    >
                                        Serving · Latency
                                        <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
                                    </Button>
                                )}
                            </div>
                        )}
                    >
                        {stageView.available ? (
                            <div className="space-y-3">
                                {stageView.peakWindowShift?.detected && stageView.peakWindowShift?.summary && (
                                    <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
                                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                                        <span>{stageView.peakWindowShift.summary}</span>
                                    </div>
                                )}
                                {activeStageCaveat && (
                                    <div className="flex items-start gap-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-900 dark:text-sky-200">
                                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                                        <span>{activeStageCaveat}</span>
                                    </div>
                                )}
                                {isClientStageFallback && !activeStageCaveat && (
                                    <p className="rounded-lg border border-theme-border bg-theme-card px-3 py-2 text-[11px] text-theme-muted">
                                        Server stage histograms were unavailable for this run; Decode Time is estimated as max(0, Client E2E − Client TTFT).
                                    </p>
                                )}
                                {showReconciliationNote && (
                                    <p className="rounded-lg border border-theme-border bg-theme-card px-3 py-2 text-[11px] text-theme-muted">
                                        E2E reconciliation: stage sum ({formatMilliseconds(activeReconciliation.total_stage_ms)}) vs Client E2E ({formatMilliseconds(activeReconciliation.client_e2e_ms)}), gap {activeReconciliation.e2e_gap_ms >= 0 ? '+' : ''}{formatMilliseconds(activeReconciliation.e2e_gap_ms)} ({activeReconciliation.e2e_gap_pct >= 0 ? '+' : ''}{formatNumber(activeReconciliation.e2e_gap_pct, '%')})
                                        {activeReconciliation.quantile_non_additive ? ' — marginal quantiles (P50/P90) are non-additive across stages.' : '.'}
                                    </p>
                                )}
                                {hasActiveStageSeries ? (
                                    <>
                                        <div className="h-60 w-full">
                                            <ResponsiveContainer width="100%" height="100%">
                                                <AreaChart data={activeStageSeries} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
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
                                                        allowDecimals
                                                        tickFormatter={formatMsAxisTick}
                                                        tick={AXIS_TICK}
                                                        stroke="var(--border-color)"
                                                        label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                                    />
                                                    <Tooltip content={<StackedMsTooltip />} />
                                                    {stageLayers.map((layer) => (
                                                        <Area
                                                            key={layer.key}
                                                            type="monotone"
                                                            stackId="stage"
                                                            dataKey={layer.key}
                                                            name={layer.label}
                                                            fill={CHART_SERIES[layer.colorIndex]}
                                                            fillOpacity={0.32}
                                                            stroke={CHART_SERIES[layer.colorIndex]}
                                                            strokeWidth={1.75}
                                                            connectNulls
                                                            dot={sparseDotProp(activeStageSeries, layer.key, CHART_SERIES[layer.colorIndex])}
                                                            isAnimationActive={false}
                                                        />
                                                    ))}
                                                </AreaChart>
                                            </ResponsiveContainer>
                                        </div>
                                        <ChartLegend
                                            className="mt-3"
                                            entries={stageLayers.map((layer) => ({
                                                label: layer.label,
                                                color: CHART_SERIES[layer.colorIndex],
                                            }))}
                                        />
                                    </>
                                ) : (
                                    <p className="rounded-lg border border-theme-border bg-theme-card px-3 py-4 text-center text-xs text-theme-muted">
                                        {effectiveQuantile === 'p90'
                                            ? `Per-interval P90 stage time series unavailable; ${activeSliceLabel} summary shares are shown below.`
                                            : `Per-interval stage time series unavailable; ${activeSliceLabel} summary shares are shown below.`}
                                    </p>
                                )}
                                {stageSharePills.length > 0 && (
                                    <div className="space-y-1.5 pt-1">
                                        <div className="flex items-center gap-1 text-[11px] font-semibold text-theme-muted">
                                            <span>Stage Share Summary ({activeSliceLabel})</span>
                                            <HelpTooltip
                                                label="Stage Share Summary help"
                                                align="start"
                                                iconSize={11}
                                                formula="E2E_Share (%) = (Stage / Total_Stage) × 100;  TTFT_Share (%) = (Stage / TTFT_Stage) × 100"
                                            >
                                                Per-stage duration (ms), share of E2E total (Stage / Total Stage Time), and share of TTFT (Stage / TTFT Stage Time).
                                            </HelpTooltip>
                                        </div>
                                        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 text-[11px]">
                                            {stageSharePills.map((pill) => (
                                                <div
                                                    key={pill.key}
                                                    className="flex items-center justify-between gap-2 rounded-lg border border-theme-border bg-theme-card px-2.5 py-1.5"
                                                >
                                                    <span className="inline-flex min-w-0 items-center gap-1 text-theme-muted">
                                                        <span className="truncate">{pill.label}</span>
                                                        <HelpTooltip label={`${pill.label} stage help`} align="start" iconSize={11} formula={pill.formula}>
                                                            {pill.help}
                                                        </HelpTooltip>
                                                    </span>
                                                    <span className="shrink-0 text-right font-mono">
                                                        <strong className="text-theme-text">{formatMilliseconds(pill.ms)}</strong>
                                                        {pill.e2ePct != null && (
                                                            <span className="ml-1.5 text-theme-muted">
                                                                {formatNumber(pill.e2ePct, '%')} E2E
                                                            </span>
                                                        )}
                                                        {pill.ttftPct != null && (
                                                            <span className="ml-1.5 text-theme-muted">
                                                                · {formatNumber(pill.ttftPct, '%')} TTFT
                                                            </span>
                                                        )}
                                                    </span>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        ) : (
                            <EmptyState
                                title="Request stage latency breakdown unavailable"
                                message="Neither server stage histograms nor client TTFT/E2E stage measurements were recorded for this run."
                            />
                        )}
                    </ChartContainer>
                </div>

                {/* TTFT Stage Decomposition */}
                <ChartContainer
                    title="TTFT Stage Decomposition"
                    subtitle={
                        ttftDecompView.source === 'pd_pooled_fallback'
                            ? 'P/D Engine TTFT (Queue + Prefill) + KV Transfer (NIXL) + EPP Scheduling'
                            : (ttftDecompView.barDefs?.length
                                ? ttftDecompView.barDefs.map((b) => b.label).join(' + ')
                                : 'Prefill Time + Queue Time + Engine / API Overhead + EPP Scheduling + Client / Gateway / Network')
                    }
                    help="Decomposes client-observed TTFT (ms) across available statistics (Min, P50, Mean, P90, P99, and Max) into serving engine prefill and decode first-step compute, queue wait, optional KV transfer, engine/API and handoff overhead, EPP scheduling, and client/gateway/network latency."
                    helpFormula={
                        ttftDecompView.source === 'pd_pooled_fallback'
                            ? 'Client_TTFT = PD_Engine_TTFT + KV_Transfer + EPP_Scheduling'
                            : (ttftDecompView.rows.some((r) => r.has_pd_sub_split)
                                ? 'Client_TTFT = Prefill + Decode_First_Step + Queue + KV_Transfer + Prefill_Engine_Overhead + Decode_Handoff_Overhead + EPP_Scheduling + Client_Gateway_Network'
                                : 'Client_TTFT = Prefill + Queue + KV_Transfer + Engine_API_Overhead + EPP_Scheduling + Client_Gateway_Network')
                    }
                    helpAlign="start"
                >
                    {ttftRows.length > 0 ? (() => {
                        const isPdPooled = ttftDecompView.source === 'pd_pooled_fallback';
                        const barDefs = (ttftDecompView.barDefs || []).map((bar) => ({
                            ...bar,
                            color: CHART_SERIES[bar.colorIndex],
                        }));
                        const primaryRow = ['p50', 'mean', 'p90', 'p99', 'min', 'max']
                            .map((k) => ttftRows.find((r) => r.key === k || r.stat?.toLowerCase() === k))
                            .find(Boolean) || ttftRows[0] || {};
                        const unsplitLabel = primaryRow.server_ttft_clamped ? 'Server TTFT Residual' : 'Engine / API Overhead';
                        const unsplitHelp = primaryRow.server_ttft_clamped
                            ? 'Server-side TTFT residual inside engine TTFT beyond Queue, KV Transfer, and Prefill (includes unmeasured tail when server histogram hits 10 s ceiling).'
                            : 'Internal serving engine and API server overhead inside server TTFT beyond measured Queue, KV Transfer, and Prefill stages (tokenization, chat template, engine IPC, scheduling, and first-token SSE flush).';
                        const unsplitFormula = primaryRow.server_ttft_clamped
                            ? 'Server_TTFT_Residual = max(0, Client_TTFT − (EPP_Scheduling + Queue + KV_Transfer + Prefill))'
                            : 'Engine_API_Overhead = min(Total_Residual, max(0, Server_TTFT − (Queue + KV_Transfer + Prefill)))';
                        const candidatePills = isPdPooled
                            ? [
                                { key: 'ttft_unattributed', label: 'P/D Engine TTFT (Queue + Prefill)', val: primaryRow.ttft_unattributed_ms, help: 'Combined Prefill and Decode engine TTFT when legacy telemetry pooled P/D histograms.', formula: 'PD_Engine_TTFT = max(0, Client_TTFT − (EPP_Scheduling + KV_Transfer))' },
                                { key: 'kv_transfer', label: 'KV Transfer (NIXL)', val: primaryRow.kv_transfer_ms, help: 'Disaggregated KV cache transfer duration over NIXL.', formula: 'KV_Transfer = min(NIXL_Xfer_Latency, max(0, Client_TTFT − EPP_Scheduling))' },
                                { key: 'epp_scheduling', label: 'EPP Scheduling', val: primaryRow.epp_scheduling_ms, help: 'Endpoint Picker (EPP) routing and scheduling latency.', formula: 'EPP_Scheduling = HistogramQuantile(q, ΔEPP_Scheduling_Buckets / Δt)' },
                            ]
                            : [
                                { key: 'prefill', label: 'Prefill Time', val: primaryRow.prefill_ms, help: 'Server-side prompt processing time inside the serving engine (ms).', formula: 'Prefill_Time = HistogramQuantile(q, ΔEngine_Prefill_Buckets / Δt)' },
                                ...(primaryRow.has_pd_sub_split
                                    ? [{ key: 'decode_first_step', label: 'Decode First-Step Compute', val: primaryRow.decode_first_step_ms, help: 'Decode pod local first-step forward pass after receiving KV cache from the Prefill pod (reconciled within server TTFT).', formula: 'Decode_First_Step = Reconciled(D_Prefill)' }]
                                    : []),
                                { key: 'queue', label: 'Queue Time', val: primaryRow.queue_ms, help: 'Time requests waited in the serving engine scheduler queue before execution (ms).', formula: 'Queue_Time = P_Queue_Eff + max(0, D_Queue_Raw − KV_Transfer)' },
                                ...(isFiniteMetricValue(primaryRow.kv_transfer_ms) ? [{ key: 'kv_transfer', label: 'KV Transfer (NIXL)', val: primaryRow.kv_transfer_ms, help: 'Disaggregated KV cache transfer duration over NIXL.', formula: 'KV_Transfer = min(NIXL_Xfer_Latency, D_Queue_Raw)' }] : []),
                                ...(primaryRow.has_pd_sub_split
                                    ? [
                                        { key: 'prefill_engine_overhead', label: 'Prefill Engine / API Overhead', val: primaryRow.prefill_engine_overhead_ms, help: 'Internal Prefill pod overhead beyond Queue and Prefill compute: API tokenization, chat template formatting, engine IPC, scheduler overhead, and KV handshake.', formula: 'Prefill_Engine_Overhead = Reconciled(max(0, P_TTFT − P_Queue_Eff − P_Prefill))' },
                                        { key: 'decode_handoff_overhead', label: 'Decode Handoff / SSE Overhead', val: primaryRow.decode_handoff_overhead_ms, help: 'Decode pod handoff and first-token overhead: P-to-D routing sidecar handoff, Decode scheduler wake-up after KV arrival, first-token sampling, and SSE flush.', formula: 'Decode_Handoff_Overhead = Reconciled(max(0, D_TTFT − D_Queue_Raw − D_Prefill))' },
                                    ]
                                    : (isFiniteMetricValue(primaryRow.engine_overhead_unsplit_ms)
                                        ? [{ key: 'engine_overhead_unsplit', label: unsplitLabel, val: primaryRow.engine_overhead_unsplit_ms, help: unsplitHelp, formula: unsplitFormula }]
                                        : [])),
                                ...(isFiniteMetricValue(primaryRow.epp_scheduling_ms) ? [{ key: 'epp', label: 'EPP Scheduling', val: primaryRow.epp_scheduling_ms, help: 'Endpoint Picker (EPP) routing and scheduling decision latency.', formula: 'EPP_Scheduling = HistogramQuantile(q, ΔEPP_Scheduling_Buckets / Δt)' }] : []),
                                ...(isFiniteMetricValue(primaryRow.gateway_overhead_ms) ? [{ key: 'gateway_overhead', label: 'Client / Gateway / Network', val: primaryRow.gateway_overhead_ms, help: 'Network, Inference Gateway, and client latency beyond EPP Scheduling and server TTFT.', formula: 'Client_Gateway_Network = max(0, Client_TTFT − (EPP_Scheduling + Server_TTFT_Accounted))' }] : []),
                                ...(isFiniteMetricValue(primaryRow.client_unattributed_ms) ? [{ key: 'client_unattributed', label: 'Unattributed TTFT', val: primaryRow.client_unattributed_ms, help: 'Exact client-observed TTFT when per-stage server histograms are unavailable for this summary statistic.', formula: 'Unattributed_TTFT = Client_TTFT' }] : []),
                            ];
                        const finitePills = candidatePills.filter((item) => isFiniteMetricValue(item.val));
                        const positivePills = finitePills.filter((item) => Number(item.val) > 0);
                        const summaryPills = positivePills.length > 0 ? positivePills : finitePills;
                        return (
                            <>
                                {ttftDecompView.caveat && (
                                    <div className="mb-3 flex items-start gap-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-900 dark:text-sky-200">
                                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                                        <span>{ttftDecompView.caveat}</span>
                                    </div>
                                )}
                                <div className="h-60 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <BarChart data={ttftRows} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                            <XAxis dataKey="stat" interval={0} tick={AXIS_TICK} stroke="var(--border-color)" />
                                            <YAxis
                                                width={56}
                                                tick={AXIS_TICK}
                                                stroke="var(--border-color)"
                                                label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                            />
                                            <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<MsStackTooltip />} />
                                            {barDefs.map((bar, idx) => (
                                                <Bar
                                                    key={bar.key}
                                                    dataKey={bar.key}
                                                    stackId="ttft"
                                                    name={bar.label}
                                                    fill={bar.color}
                                                    fillOpacity={bar.opacity}
                                                    radius={idx === barDefs.length - 1 ? [6, 6, 0, 0] : undefined}
                                                    maxBarSize={50}
                                                    isAnimationActive={false}
                                                />
                                            ))}
                                        </BarChart>
                                    </ResponsiveContainer>
                                </div>
                                <ChartLegend
                                    className="mt-3"
                                    entries={barDefs.map((bar) => ({ label: bar.label, color: bar.color, opacity: bar.opacity }))}
                                />
                                {summaryPills.length > 0 && (
                                    <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 text-[11px]">
                                        {summaryPills.map((pill) => (
                                            <div key={pill.key} className="flex items-center justify-between rounded-lg border border-theme-border bg-theme-card px-2.5 py-1.5">
                                                <span className="inline-flex items-center gap-1 text-theme-muted">
                                                    <span>{pill.label}</span>
                                                    <HelpTooltip label={`${pill.label} help`} align="start" iconSize={11} formula={pill.formula}>
                                                        {pill.help}
                                                    </HelpTooltip>
                                                </span>
                                                <span className="font-mono font-semibold text-theme-text">{formatMilliseconds(pill.val)}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </>
                        );
                    })() : (
                        <EmptyState
                            title="Stage decomposition unavailable"
                            message={ttftDecomp.reason || 'Server-side engine queue and prefill stage histograms were not exposed for this run.'}
                        />
                    )}
                </ChartContainer>
            </div>

            <div className="grid min-w-0 gap-6 lg:grid-cols-2">
                {/* Multi-Turn Prefix Cache Reuse */}
                <ChartContainer
                    className="min-w-0"
                    title="Multi-Turn Prefix Cache Reuse"
                    subtitle="Prompt token throughput (Prefix Cache vs Prefill Compute) and multi-turn cache misses"
                    help="Splits prompt token throughput (tok/s) into prefix cache hits vs prefill-computed tokens (uncached or recomputed), paired with multi-turn follow-up cache statistics. High prefill compute rates indicate cache misses or KV evictions."
                    helpFormula="Prefix_Cache_Rate = Local_Cache_tok/s + External_KV_tok/s;  Prefill_Compute_Rate = Fresh_Compute_tok/s + Recomputed_tok/s"
                    helpAlign="start"
                >
                    {promptFlowData.length > 0 || turnCacheStats ? (
                        <div className="space-y-4">
                            {promptFlowData.length > 0 && (
                                <>
                                    <div className="flex items-center gap-1 text-[11px] font-semibold text-theme-muted">
                                        <span>Prompt Token Throughput (Prefix Cache vs Prefill Compute)</span>
                                        <HelpTooltip
                                            label="Prompt Token Throughput help"
                                            align="start"
                                            iconSize={11}
                                            formula="Cached_tok/s = ΔCached_Prompt_Tokens / Δt;  Compute_tok/s = ΔComputed_Prompt_Tokens / Δt"
                                        >
                                            Mean prompt tokens per second served from local/external prefix cache versus prefill-computed (uncached or recomputed after eviction).
                                        </HelpTooltip>
                                    </div>
                                    <div className="h-44 w-full">
                                        <ResponsiveContainer width="100%" height="100%">
                                            <BarChart data={promptFlowData} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                                <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                <XAxis dataKey="category" tick={AXIS_TICK} stroke="var(--border-color)" />
                                                <YAxis
                                                    width={60}
                                                    tick={AXIS_TICK}
                                                    stroke="var(--border-color)"
                                                    label={{ value: 'tok/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                                />
                                                <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<TokenRateTooltip />} />
                                                <Bar dataKey="cached_tps" stackId="tokens" name="Prefix Cache" fill={CHART_SERIES[0]} maxBarSize={80} isAnimationActive={false} />
                                                <Bar dataKey="fresh_tps" stackId="tokens" name="Prefill Compute" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={80} isAnimationActive={false} />
                                            </BarChart>
                                        </ResponsiveContainer>
                                    </div>
                                    <ChartLegend
                                        entries={[
                                            { label: 'Prefix Cache (tok/s)', color: CHART_SERIES[0] },
                                            { label: 'Prefill Compute (tok/s)', color: CHART_SERIES[2] },
                                        ]}
                                    />
                                </>
                            )}
                            {sessionsSummary?.available && turnCacheStats ? (
                                <div className="rounded-lg border border-theme-border bg-theme-card p-3 text-xs space-y-1">
                                    <div className="flex items-center gap-1 font-semibold text-theme-text">
                                        <span>Multi-Turn Follow-up Analysis (turn &gt; 1)</span>
                                        <HelpTooltip
                                            label="Multi-turn follow-up analysis help"
                                            align="start"
                                            iconSize={11}
                                            formula="Followup_TTFT_Ratio = Followup_TTFT_P50 / Turn1_TTFT_P50"
                                        >
                                            Evaluates whether subsequent turns in multi-turn sessions reuse KV prefix cache or suffer TTFT regressions from cache misses.
                                        </HelpTooltip>
                                    </div>
                                    <div className="flex flex-wrap gap-4 text-theme-muted">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Follow-up turns: <strong className="font-mono text-theme-text">{formatCount(turnCacheStats.followup_count ?? turnCacheStats.count)}</strong></span>
                                            <HelpTooltip
                                                label="Follow-up turns count help"
                                                align="start"
                                                iconSize={11}
                                                formula="Followup_Turns = Count(Requests where Session_Turn ≥ 2)"
                                            >
                                                Total number of requests occurring at turn 2 or later within correlated multi-turn sessions.
                                            </HelpTooltip>
                                        </span>
                                        <span className="inline-flex items-center gap-1">
                                            <span>Follow-up regression rate: <strong className="font-mono text-theme-text">{formatNumber(turnCacheStats.followup_regression_pct ?? turnCacheStats.miss_rate_pct, '%')}</strong></span>
                                            <HelpTooltip
                                                label="Follow-up regression rate help"
                                                align="start"
                                                iconSize={11}
                                                formula="Followup_Regression_Rate (%) = (Count(Followup_TTFT > 1.5 × Turn1_TTFT_P50) / Followup_Turns) × 100"
                                            >
                                                Share of follow-up turns experiencing unexpected cache misses or elevated TTFT compared to warm-cache expectations.
                                            </HelpTooltip>
                                        </span>
                                        {turnCacheStats.first_turn_ttft_p50_ms != null && (
                                            <span className="inline-flex items-center gap-1">
                                                <span>Turn 1 TTFT P50: <strong className="font-mono text-theme-text">{formatMilliseconds(turnCacheStats.first_turn_ttft_p50_ms)}</strong></span>
                                                <HelpTooltip
                                                    label="Turn 1 TTFT P50 help"
                                                    align="start"
                                                    iconSize={11}
                                                    formula="Turn1_TTFT_P50 = Quantile_0.50(TTFT | Session_Turn = 1)"
                                                >
                                                    Median TTFT (ms) for initial session turns (cold prefix cache baseline).
                                                </HelpTooltip>
                                            </span>
                                        )}
                                        {(turnCacheStats.followup_ttft_p50_ms ?? turnCacheStats.p90_ttft_ms) != null && (
                                            <span className="inline-flex items-center gap-1">
                                                <span>Follow-up TTFT P50: <strong className="font-mono text-theme-text">{formatMilliseconds(turnCacheStats.followup_ttft_p50_ms ?? turnCacheStats.p90_ttft_ms)}</strong></span>
                                                <HelpTooltip
                                                    label="Follow-up TTFT P50 help"
                                                    align="start"
                                                    iconSize={11}
                                                    formula="Followup_TTFT_P50 = Quantile_0.50(TTFT | Session_Turn ≥ 2)"
                                                >
                                                    Median TTFT (ms) on subsequent turns; values near or above Turn 1 TTFT signal poor prefix cache retention.
                                                </HelpTooltip>
                                            </span>
                                        )}
                                    </div>
                                </div>
                            ) : (
                                <p className="rounded-lg border border-theme-border bg-theme-card px-3 py-2 text-xs text-theme-muted">
                                    Per-turn follow-up breakdown requires a session-bearing trace format (such as BailianTrace).
                                </p>
                            )}
                        </div>
                    ) : (
                        <EmptyState
                            icon={<GitBranch className="h-6 w-6" />}
                            title="Prompt cache breakdown unavailable"
                            message="Engine prompt token source counters and session-level turn metadata were not present for this run."
                        />
                    )}
                </ChartContainer>

                {/* EPP Predicted vs vLLM Actual Prefix Cache Hit Rate */}
                <ChartContainer
                    className="min-w-0"
                    title="EPP Predicted vs vLLM Actual Prefix Cache Hit Rate"
                    subtitle="EPP predicted prefix cache hit rate (%) vs vLLM actual prefix cache hit rate (%)"
                    help="Compares Endpoint Picker (EPP)'s predicted prefix cache hit rate (%) with the serving engine's actual prefix cache hit rate (%) over time. Large positive gaps suggest stale EPP prefix cache indices or worker KV evictions."
                    helpFormula="Gap_pp = EPP_Predicted_Hit_% − Engine_Actual_Hit_%"
                    helpAlign="end"
                >
                    {hasRoutingComparison ? (
                        <div className="space-y-4">
                            {(eppExpectedHitPct != null || vllmActualHitPct != null) && (
                                <div className="grid min-w-0 grid-cols-1 gap-3 text-xs sm:grid-cols-3">
                                    <div className="min-w-0 rounded-lg border border-theme-border bg-theme-card p-2.5">
                                        <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-theme-muted">
                                            <span className="truncate">EPP Predicted</span>
                                            <HelpTooltip
                                                label="EPP Predicted hit help"
                                                align="start"
                                                iconSize={11}
                                                formula="EPP_Predicted_Hit (%) = (EPP_Cached_Input_Rate / EPP_Input_Token_Rate) × 100"
                                            >
                                                Prefix cache hit rate (%) predicted by Endpoint Picker (EPP) from its in-memory prefix index.
                                            </HelpTooltip>
                                        </div>
                                        <div className="mt-0.5 truncate font-mono font-semibold text-theme-text">{formatNumber(eppExpectedHitPct, '%')}</div>
                                    </div>
                                    <div className="min-w-0 rounded-lg border border-theme-border bg-theme-card p-2.5">
                                        <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-theme-muted">
                                            <span className="truncate">vLLM Actual</span>
                                            <HelpTooltip
                                                label="vLLM Actual hit help"
                                                align="center"
                                                iconSize={11}
                                                formula="Engine_Actual_Hit (%) = (ΔCached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100"
                                            >
                                                Actual prefix cache hit rate (%) reported by serving engine metrics during prompt processing.
                                            </HelpTooltip>
                                        </div>
                                        <div className="mt-0.5 truncate font-mono font-semibold text-theme-text">{formatNumber(vllmActualHitPct, '%')}</div>
                                    </div>
                                    <div className="min-w-0 rounded-lg border border-theme-border bg-theme-card p-2.5">
                                        <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-theme-muted">
                                            <span className="truncate">Prediction Gap</span>
                                            <HelpTooltip
                                                label="EPP vs vLLM cache gap help"
                                                align="end"
                                                iconSize={11}
                                                formula="Prediction_Gap (pp) = EPP_Predicted_Hit_% − Engine_Actual_Hit_%"
                                            >
                                                Percentage-point discrepancy between EPP predicted hit rate (%) and serving engine actual hit rate (%). Large gaps indicate cache eviction or routing drift.
                                            </HelpTooltip>
                                        </div>
                                        <div className="mt-0.5 truncate font-mono font-semibold text-theme-text">{formatNumber(routingGapPct, '%')}</div>
                                    </div>
                                </div>
                            )}
                            {routingSeries.length > 0 && (
                                <>
                                    <div className="h-52 w-full">
                                        <ResponsiveContainer width="100%" height="100%">
                                            <LineChart data={routingSeries} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
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
                                                    domain={[0, 100]}
                                                    ticks={[0, 25, 50, 75, 100]}
                                                    allowDataOverflow
                                                    width={48}
                                                    tick={AXIS_TICK}
                                                    stroke="var(--border-color)"
                                                    label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                                />
                                                <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<PercentTooltip />} />
                                                <Line type="monotone" dataKey="epp_hit_pct" name="EPP Predicted Prefix Cache Hit Rate (%)" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(routingSeries, 'epp_hit_pct', CHART_SERIES[1])} isAnimationActive={false} />
                                                <Line type="monotone" dataKey="vllm_hit_pct" name="vLLM Actual Prefix Cache Hit Rate (%)" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(routingSeries, 'vllm_hit_pct', CHART_SERIES[0])} isAnimationActive={false} />
                                            </LineChart>
                                        </ResponsiveContainer>
                                    </div>
                                    <ChartLegend
                                        entries={[
                                            { label: 'EPP Predicted Prefix Cache Hit Rate (%)', color: CHART_SERIES[1] },
                                            { label: 'vLLM Actual Prefix Cache Hit Rate (%)', color: CHART_SERIES[0] },
                                        ]}
                                    />
                                </>
                            )}
                        </div>
                    ) : (
                        <EmptyState
                            title="EPP vs vLLM cache comparison unavailable"
                            message="EPP prefix-cache index metrics are only populated when traffic routes through an EPP-monitored gateway."
                        />
                    )}
                </ChartContainer>
            </div>

            {/* Per-Pod Load Skew */}
            <ChartContainer
                    title="Per-Pod Load Skew"
                    subtitle={isDisaggregated ? 'Disaggregated Prefill / Decode pod distribution' : 'Aggregated Worker pod distribution'}
                    help="Compares per-pod request rate, waiting requests, peak KV cache usage (%), and prefix cache hit rate (%) from Prometheus pod metrics. High skew highlights load imbalance or P/D bottlenecks."
                    helpFormula="Skew_Ratio = max_pod(Pod_Load) / mean_pod(Pod_Load)  (N ≥ 2, mean > 0)"
                    helpAlign="end"
                    actions={skewRatio !== null && (
                        <span className="inline-flex items-center gap-1">
                            <Badge tone={Number(skewRatio) >= 2.0 ? 'warning' : 'success'} size="sm">
                                Skew Ratio: {formatNumber(skewRatio, 'x')}
                            </Badge>
                            <HelpTooltip
                                label="Pod skew ratio help"
                                align="end"
                                iconSize={12}
                                formula="Skew_Ratio = max_pod(Pod_Load) / mean_pod(Pod_Load)"
                            >
                                Ratio of busiest-to-mean pod request rate or waiting requests. Ratios at or above 2.0x signal uneven routing or P/D stage imbalance.
                            </HelpTooltip>
                        </span>
                    )}
                >
                    {podRows.length > 0 ? (
                        <div className="space-y-4">
                            <div className="h-44 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={podChartData} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="pod" tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis
                                            width={52}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'req/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<PodLoadTooltip />} />
                                        <Bar dataKey="request_rate_rps" name="Request Rate (req/s)" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                            <div className="overflow-x-auto lg:overflow-visible rounded-lg border border-theme-border">
                                <table className="w-full text-left text-xs">
                                    <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                                        <tr>
                                            <th className="px-3 py-2">
                                                <span className="inline-flex items-center gap-1">
                                                    <span>Pod</span>
                                                    <HelpTooltip label="Pod column help" align="start" iconSize={11}>
                                                        Kubernetes pod name of the serving engine instance.
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                            <th className="px-3 py-2">
                                                <span className="inline-flex items-center gap-1">
                                                    <span>Role</span>
                                                    <HelpTooltip label="Role column help" align="start" iconSize={11}>
                                                        Serving role assigned to the pod (Prefill, Decode, or Worker).
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                            <th className="px-3 py-2 text-right">
                                                <span className="inline-flex items-center justify-end gap-1">
                                                    <span>Req/s</span>
                                                    <HelpTooltip
                                                        label="Pod request rate column help"
                                                        align="end"
                                                        iconSize={11}
                                                        formula="Pod_RPS = mean_t (ΔPod_Requests / Δt)"
                                                    >
                                                        Mean request throughput (req/s) handled by this pod over the telemetry window.
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                            <th className="px-3 py-2 text-right">
                                                <span className="inline-flex items-center justify-end gap-1">
                                                    <span>Waiting</span>
                                                    <HelpTooltip
                                                        label="Pod waiting requests column help"
                                                        align="end"
                                                        iconSize={11}
                                                        formula="Pod_Waiting = mean_t Queued_Waiting_Requests_pod(t)"
                                                    >
                                                        Mean number of waiting requests on this pod; high values signal local saturation.
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                            <th className="px-3 py-2 text-right">
                                                <span className="inline-flex items-center justify-end gap-1">
                                                    <span>Peak KV Cache (%)</span>
                                                    <HelpTooltip
                                                        label="Pod peak KV column help"
                                                        align="end"
                                                        iconSize={11}
                                                        formula="Pod_Peak_KV (%) = max_t ((Used_KV_Blocks_pod / Total_KV_Blocks_pod) × 100)"
                                                    >
                                                        Maximum KV cache usage (%) reached on this pod; values near 100% risk preemptions.
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                            <th className="px-3 py-2 text-right">
                                                <span className="inline-flex items-center justify-end gap-1">
                                                    <span>Prefix Cache Hit Rate (%)</span>
                                                    <HelpTooltip
                                                        label="Pod prefix hit column help"
                                                        align="end"
                                                        iconSize={11}
                                                        formula="Pod_Prefix_Hit (%) = mean_t ((ΔCached_Prompt_pod / ΔTotal_Prompt_pod) × 100)"
                                                    >
                                                        Mean prefix cache hit rate (%) on this pod; higher values reduce prefill compute load.
                                                    </HelpTooltip>
                                                </span>
                                            </th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-theme-border">
                                        {podRows.map((row) => (
                                            <tr key={row.pod} className="text-theme-text">
                                                <td className="px-3 py-2 font-mono font-semibold">{row.pod}</td>
                                                <td className="px-3 py-2">
                                                    <Badge tone="neutral" size="xs">{displayName(row.role || 'worker')}</Badge>
                                                </td>
                                                <td className="px-3 py-2 text-right font-mono">{formatNumber(row.request_rate_rps)}</td>
                                                <td className="px-3 py-2 text-right font-mono">{formatNumber(row.avg_waiting)}</td>
                                                <td className="px-3 py-2 text-right font-mono">{formatNumber(row.peak_kv_pct, '%')}</td>
                                                <td className="px-3 py-2 text-right font-mono">{formatNumber(row.prefix_hit_pct, '%')}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    ) : (
                        <EmptyState
                            icon={<Server className="h-6 w-6" />}
                            title="Per-pod load unavailable"
                            message="Pod-level inference metrics were not recorded for this run."
                        />
                    )}
                </ChartContainer>
        </div>
    );
}

export default BottlenecksTab;
