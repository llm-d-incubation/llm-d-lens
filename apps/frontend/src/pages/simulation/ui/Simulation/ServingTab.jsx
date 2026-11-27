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

import React, { useEffect, useMemo, useState } from 'react';
import {
    Activity,
    AlertTriangle,
    Clock3,
    Cpu,
    Database,
    ExternalLink,
    Gauge,
    Layers,
    Network,
    Pause,
    Play,
    RotateCcw,
    Server,
    Thermometer,
    Zap,
} from 'lucide-react';
import {
    Area,
    AreaChart,
    CartesianGrid,
    ComposedChart,
    Line,
    LineChart,
    ReferenceLine,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { FlowMap } from '../../../clusters';
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
    StatCard,
    ToggleGroup,
} from '../../../../shared/ui';
import {
    buildRecordedFlowData,
    buildRecordedFlowFrames,
    buildStageLatencyView,
    clampPercent,
    displayName,
    effectivePodRole,
    formatBytes,
    formatCount,
    formatDecimal,
    formatEnergyWh,
    formatMilliseconds,
    formatNumber,
    formatWatts,
    isBenchmarkRunnerPod,
    isFiniteMetricValue,
    isLegacyPooledPd,
    isSafeHttpUrl,
    normalizeStageStackSeries,
    numberAt,
    stageAreaLayers,
} from '../../model/presentation';

const AXIS_TICK = { fill: 'var(--text-muted)', fontSize: 10 };
const EMPTY_OBJ = {};
const PROMPT_CACHE_STACK_KEYS = ['prompt_cached_tps', 'prompt_fresh_tps'];
const PROMPT_SOURCE_STACK_KEYS = ['prompt_local_cache_tps', 'prompt_external_tps', 'prompt_fresh_tps'];
const PREFIX_HIT_STACK_KEYS = ['prefix_hit_pct', 'ext_hit_pct', 'recompute_pct', 'cold_new_pct'];

function formatMsAxisTick(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return '';
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '';
    return formatDecimal(numeric);
}

function formatSeconds(value) {
    const formatted = formatDecimal(value);
    return formatted === '—' ? '—' : `${formatted} s`;
}

function firstFinite(...values) {
    for (const value of values) {
        if (isFiniteMetricValue(value)) {
            return Number(value);
        }
    }
    return null;
}

function positiveFinite(...values) {
    for (const value of values) {
        if (isFiniteMetricValue(value)) {
            const numeric = Number(value);
            if (numeric > 0) return numeric;
        }
    }
    return null;
}

function secondsToPositiveMs(value) {
    if (!isFiniteMetricValue(value)) return null;
    const numeric = Number(value) * 1000;
    return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
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

function maxSeriesValue(rows, key) {
    let maxVal = null;
    for (const row of rows) {
        const val = row?.[key];
        if (isFiniteMetricValue(val)) {
            const numeric = Number(val);
            maxVal = maxVal === null ? numeric : Math.max(maxVal, numeric);
        }
    }
    return maxVal;
}

function maxFinite(...values) {
    let best = null;
    for (const value of values) {
        if (isFiniteMetricValue(value)) {
            const numeric = Number(value);
            best = best === null ? numeric : Math.max(best, numeric);
        }
    }
    return best;
}

function matchEndpointToPod(endpointName, podNames) {
    const ep = String(endpointName || '').trim();
    if (!ep) return null;
    if (podNames.includes(ep)) return ep;
    const stripped = ep.replace(/-rank-\d+$/, '');
    if (stripped !== ep && podNames.includes(stripped)) return stripped;
    let best = null;
    for (const pod of podNames) {
        if (ep.startsWith(`${pod}-`) && (best === null || pod.length > best.length)) {
            best = pod;
        }
    }
    return best;
}

function coefficientOfVariation(values) {
    const finite = values.filter((v) => isFiniteMetricValue(v)).map((v) => Number(v));
    if (finite.length < 2) return finite.length ? 0.0 : null;
    const mean = finite.reduce((acc, v) => acc + v, 0) / finite.length;
    if (mean <= 0) return null;
    const variance = finite.reduce((acc, v) => acc + ((v - mean) ** 2), 0) / finite.length;
    return Math.round((Math.sqrt(variance) / mean) * 1e5) / 1e5;
}

function inferEndpointRole(nameOrRow, podRoleLookup, podNames) {
    if (nameOrRow && typeof nameOrRow === 'object') {
        const explicitRole = String(nameOrRow.role || '').trim().toLowerCase();
        if (explicitRole === 'prefill' || explicitRole === 'decode' || explicitRole === 'worker') {
            return explicitRole;
        }
        const rawName = String(nameOrRow.pod || nameOrRow.endpoint || nameOrRow.destination || '').trim();
        return inferEndpointRole(rawName, podRoleLookup, podNames);
    }
    const ep = String(nameOrRow || '').trim();
    const matchedPod = matchEndpointToPod(ep, podNames) || ep.replace(/-rank-\d+$/, '');
    const mapped = podRoleLookup.get(ep) || podRoleLookup.get(matchedPod);
    if (mapped === 'prefill' || mapped === 'decode' || mapped === 'worker') return mapped;
    const lower = ep.toLowerCase();
    if (lower.includes('prefill')) return 'prefill';
    if (lower.includes('decode')) return 'decode';
    return 'worker';
}

// eslint-disable-next-line react-refresh/only-export-components
export function resolveEffectiveTokenLoadCv(derived, perEndpoint = [], perPod = [], isDisaggregated = false) {
    if (!isDisaggregated) {
        return firstFinite(derived?.token_load_cv);
    }
    const endpoints = Array.isArray(perEndpoint) ? perEndpoint : [];
    const pods = Array.isArray(perPod) ? perPod : [];
    const podRoleLookup = new Map();
    const podNames = [];
    for (const pod of pods) {
        const name = String(pod?.pod || pod?.name || '').trim();
        if (!name) continue;
        podNames.push(name);
        podRoleLookup.set(name, String(effectivePodRole(pod) || 'worker').trim().toLowerCase());
    }
    const inflightByPod = new Map();
    const roleByPod = new Map();
    for (const ep of endpoints) {
        const load = firstFinite(ep?.inflight_token_load?.mean, ep?.inflight_token_load);
        if (load === null) continue;
        const epPod = String(ep?.pod || '').trim();
        const epName = String(ep?.endpoint || epPod || '').trim();
        const targetPod = (epPod && podNames.includes(epPod) ? epPod : null)
            || matchEndpointToPod(epName, podNames)
            || epName.replace(/-rank-\d+$/, '');
        if (!targetPod) continue;
        inflightByPod.set(targetPod, (inflightByPod.get(targetPod) ?? 0) + load);
        const explicitRole = String(ep?.role || '').trim().toLowerCase();
        if ((explicitRole === 'prefill' || explicitRole === 'decode') && !roleByPod.has(targetPod)) {
            roleByPod.set(targetPod, explicitRole);
        }
    }
    const roleBuckets = new Map();
    let hasAnyLoad = false;
    for (const [podName, load] of inflightByPod.entries()) {
        hasAnyLoad = true;
        const role = roleByPod.get(podName) || inferEndpointRole(podName, podRoleLookup, podNames);
        if (role !== 'prefill' && role !== 'decode') continue;
        if (!roleBuckets.has(role)) roleBuckets.set(role, []);
        roleBuckets.get(role).push(load);
    }
    const roleCvs = [];
    for (const loads of roleBuckets.values()) {
        if (loads.length >= 2) {
            const cv = coefficientOfVariation(loads);
            if (cv !== null) roleCvs.push(cv);
        }
    }
    if (roleCvs.length > 0) {
        return Math.max(...roleCvs);
    }
    if (hasAnyLoad && roleBuckets.size > 0) {
        return 0.0;
    }
    return firstFinite(derived?.token_load_cv);
}

// eslint-disable-next-line react-refresh/only-export-components
export function resolveServingLatencySlo(task, telemetry, serverLatencyMode = 'ttft') {
    const verdictSlo = telemetry?.forensics?.verdict?.slo;
    const manifestSlo = telemetry?.run_manifest?.slo;
    const backendOptionSources = [
        task?.simulation?.backend_options,
        task?.backend_options,
        telemetry?.run_manifest?.replay?.backend_options,
    ];
    const backendSloSeconds = (key) => positiveFinite(
        ...backendOptionSources.map((src) => numberAt(src, [key])),
    );
    const ttftSloMs = positiveFinite(
        secondsToPositiveMs(verdictSlo?.ttft_s),
        verdictSlo?.ttft_ms,
        secondsToPositiveMs(manifestSlo?.ttft_s),
        manifestSlo?.ttft_ms,
        secondsToPositiveMs(backendSloSeconds('ttft_slo')),
    );
    const tpotSloMs = positiveFinite(
        verdictSlo?.tpot_ms,
        secondsToPositiveMs(verdictSlo?.tpot_s),
        manifestSlo?.tpot_ms,
        secondsToPositiveMs(manifestSlo?.tpot_s),
        secondsToPositiveMs(backendSloSeconds('tpot_slo')),
    );
    const e2eSloMs = positiveFinite(
        secondsToPositiveMs(verdictSlo?.e2e_s),
        verdictSlo?.e2e_ms,
        secondsToPositiveMs(manifestSlo?.e2e_s),
        manifestSlo?.e2e_ms,
        secondsToPositiveMs(backendSloSeconds('e2e_slo')),
    );
    const activeServerSloMs = serverLatencyMode === 'ttft'
        ? ttftSloMs
        : serverLatencyMode === 'tpot'
            ? tpotSloMs
            : e2eSloMs;
    const sloDisplayText = activeServerSloMs !== null ? formatMilliseconds(activeServerSloMs) : null;
    return { ttftSloMs, tpotSloMs, e2eSloMs, activeServerSloMs, sloDisplayText };
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

function buildSeriesTooltip(unit, { showTotal = false } = {}) {
    return function ServingTooltip({ active, payload, label }) {
        if (!active || !payload?.length) return null;
        const rows = [];
        const seen = new Set();
        for (const item of payload) {
            if (!item?.dataKey || String(item.dataKey).endsWith('_band')) continue;
            if (item.color === 'transparent' || item.color === 'none') continue;
            if (!isFiniteMetricValue(item.value)) continue;
            if (seen.has(item.dataKey)) continue;
            seen.add(item.dataKey);
            rows.push(item);
        }
        if (!rows.length) return null;
        const formatVal = (val) => (unit === 'ms' || unit === 'ms/tok' ? formatMilliseconds(val) : formatNumber(val));
        const unitSuffix = unit === 'ms' ? '' : unit === 'ms/tok' ? '/tok' : ` ${unit}`;
        const total = showTotal
            ? rows.reduce((sum, item) => sum + Number(item.value), 0)
            : null;
        return (
            <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
                {rows.map((item) => (
                    <ChartTooltipRow
                        key={item.dataKey}
                        color={item.color}
                        label={item.name}
                        value={formatVal(item.value)}
                        unit={unitSuffix}
                    />
                ))}
                {showTotal && (
                    <div className="mt-1 border-t border-theme-border pt-1">
                        <ChartTooltipRow
                            label="Total"
                            value={formatVal(total)}
                            unit={unitSuffix}
                        />
                    </div>
                )}
            </ChartTooltip>
        );
    };
}

function MixedLoadTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => item.value !== null && item.value !== undefined);
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => {
                const isRps = String(item.dataKey).includes('rps');
                return (
                    <ChartTooltipRow
                        key={item.dataKey}
                        color={item.color}
                        label={item.name}
                        value={formatNumber(item.value)}
                        unit={isRps ? ' req/s' : ' req'}
                    />
                );
            })}
        </ChartTooltip>
    );
}

function OffloadTierUsageTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = [];
    const seen = new Set();
    for (const item of payload) {
        if (!item?.dataKey || !isFiniteMetricValue(item.value)) continue;
        if (seen.has(item.dataKey)) continue;
        seen.add(item.dataKey);
        rows.push(item);
    }
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={formatNumber(item.value)}
                    unit="%"
                />
            ))}
        </ChartTooltip>
    );
}

function OffloadTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const rows = [];
    const seen = new Set();
    for (const item of payload) {
        if (!item?.dataKey || !isFiniteMetricValue(item.value)) continue;
        if (seen.has(item.dataKey)) continue;
        seen.add(item.dataKey);
        rows.push(item);
    }
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={formatNumber(item.value)}
                    unit=" MiB/s"
                />
            ))}
        </ChartTooltip>
    );
}

function KvTierTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    const row = payload[0]?.payload;
    if (!row) return null;
    const tiers = [
        {
            key: 'hbm',
            label: 'HBM',
            color: CHART_SERIES[2],
            share: row.hbm_share_pct,
            usage: row.kv_pct,
        },
        {
            key: 'host',
            label: 'Host Memory',
            color: CHART_SERIES[1],
            share: row.host_share_pct,
            usage: row.cpu_kv_pct,
        },
        {
            key: 'nvme',
            label: 'NVMe',
            color: CHART_SERIES[0],
            share: row.nvme_share_pct,
            usage: row.disk_kv_pct,
        },
    ].filter((tier) => isFiniteMetricValue(tier.share) || isFiniteMetricValue(tier.usage));
    if (!tiers.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {tiers.map((tier) => {
                const shareStr = isFiniteMetricValue(tier.share) ? `${formatNumber(tier.share)}% share` : '—';
                const usageStr = isFiniteMetricValue(tier.usage) ? `${formatNumber(tier.usage)}% usage` : 'Not configured';
                return (
                    <ChartTooltipRow
                        key={tier.key}
                        color={tier.color}
                        label={tier.label}
                        value={`${shareStr} · ${usageStr}`}
                    />
                );
            })}
        </ChartTooltip>
    );
}

const TokTooltip = buildSeriesTooltip('tok/s');
const StackedTokTooltip = buildSeriesTooltip('tok/s', { showTotal: true });
const ReqTooltip = buildSeriesTooltip('req');
const PctTooltip = buildSeriesTooltip('%');
const StackedPctTooltip = buildSeriesTooltip('%', { showTotal: true });
const MsTooltip = buildSeriesTooltip('ms');
const MsPerTokTooltip = buildSeriesTooltip('ms/tok');
const StackedMsTooltip = buildSeriesTooltip('ms', { showTotal: true });
const RatePerSecTooltip = buildSeriesTooltip('/s');
const BlocksPerMinTooltip = buildSeriesTooltip('blocks/min');
const SecTooltip = buildSeriesTooltip('s');
const WattsTooltip = buildSeriesTooltip('W');

const NON_SERVING_ROLES = new Set(['epp', 'router', 'gateway', 'client', 'runner', 'benchmark']);

function formatPct(value) {
    return formatDecimal(value);
}

function formatTokMean(value) {
    return formatDecimal(value);
}

function integrateRateSeries(samples, stepSeconds = 5) {
    if (!Array.isArray(samples) || !samples.length) return null;
    const valid = samples
        .filter(([t, val]) => isFiniteMetricValue(t) && isFiniteMetricValue(val) && Number(val) >= 0)
        .map(([t, val]) => [Number(t), Number(val)])
        .sort((a, b) => a[0] - b[0]);
    if (!valid.length) return null;
    const effStep = Math.max(1, Number(stepSeconds) || 5);
    if (valid.length === 1) return Math.round(valid[0][1] * effStep);
    const maxGap = Math.max(1, 3 * effStep);
    let total = 0;
    let integrated = false;
    for (let i = 1; i < valid.length; i += 1) {
        const dt = valid[i][0] - valid[i - 1][0];
        if (dt > 0 && dt <= maxGap) {
            total += 0.5 * (valid[i - 1][1] + valid[i][1]) * dt;
            integrated = true;
        }
    }
    if (!integrated) {
        return Math.round(valid.reduce((acc, [, val]) => acc + val * effStep, 0));
    }
    return Math.round(total);
}

function resolveDurationSeconds(clientSeries, series, stepSeconds) {
    return firstFinite(
        clientSeries?.duration_seconds,
        clientSeries?.windows?.full?.duration_seconds,
        clientSeries?.windows?.profiling?.duration_seconds,
        series.length > 1 ? Math.max(stepSeconds, series[series.length - 1].t - series[0].t) : (series.length === 1 ? stepSeconds : null),
    );
}

function resolveTokenFlow({
    derived,
    clientSeries,
    series,
    summary,
    sessionsSummary,
    stepSeconds,
    durationSeconds,
    isDisaggregated = false,
    promptTokensByRoleData = [],
    sbrPrefill = EMPTY_OBJ,
}) {
    const prefillRow = Array.isArray(promptTokensByRoleData)
        ? promptTokensByRoleData.find(
            (row) => String(row?.role || '').trim().toLowerCase() === 'prefill' && Number(row?.total_tokens || 0) > 0,
        )
        : null;
    const prefillAvoidedRatio = prefillRow && Number(prefillRow.total_tokens) > 0
        ? (Math.max(0, Number(prefillRow.local_cache_tokens || 0)) + Math.max(0, Number(prefillRow.external_kv_tokens || 0)))
            / Number(prefillRow.total_tokens)
        : null;
    const prefillAvoidedPct = prefillAvoidedRatio !== null ? prefillAvoidedRatio * 100 : null;

    const fullWin = clientSeries?.windows?.full || clientSeries?.windows?.profiling;
    const reqCount = firstFinite(fullWin?.request_count, fullWin?.total_requests);
    let clientSent = firstFinite(clientSeries?.token_flow?.sent_tokens, fullWin?.total_input_tokens);
    if (
        clientSent === null
        && isFiniteMetricValue(fullWin?.input_tokens?.avg)
        && reqCount !== null
        && reqCount > 0
    ) {
        clientSent = Math.round(Number(fullWin.input_tokens.avg) * reqCount);
    }

    const rawTf = derived?.token_flow;
    const hasValidRawTf = Boolean(
        rawTf && typeof rawTf === 'object' && isFiniteMetricValue(rawTf.sent_tokens) && Number(rawTf.sent_tokens) > 0,
    );
    const rawTfCachedPct = hasValidRawTf
        ? (firstFinite(rawTf.cached_pct)
            ?? (isFiniteMetricValue(rawTf.cached_tokens) ? (Number(rawTf.cached_tokens) / Number(rawTf.sent_tokens)) * 100 : null))
        : null;
    const isLegacyPdTokenFlow = Boolean(
        isDisaggregated
        && prefillRow
        && hasValidRawTf
        && rawTf.role_scope !== 'prefill'
        && rawTfCachedPct !== null
        && ((rawTfCachedPct >= 99.5 && prefillAvoidedPct < 95) || Math.abs(rawTfCachedPct - prefillAvoidedPct) > 5),
    );

    if (hasValidRawTf && !isLegacyPdTokenFlow) {
        const sent = Math.round(Number(rawTf.sent_tokens));
        const cached = Math.min(sent, Math.max(0, Math.round(firstFinite(rawTf.cached_tokens) ?? 0)));
        const recomputed = Math.min(sent - cached, Math.max(0, Math.round(firstFinite(rawTf.recomputed_tokens) ?? 0)));
        const coldNew = Math.max(0, sent - cached - recomputed);
        const cachedPct = clampPercent(firstFinite(rawTf.cached_pct)) ?? (cached / sent) * 100;
        const recomputedPct = clampPercent(firstFinite(rawTf.recomputed_pct)) ?? (recomputed / sent) * 100;
        const coldNewPct = clampPercent(firstFinite(rawTf.cold_new_pct)) ?? Math.max(0, 100 - cachedPct - recomputedPct);
        const rawCeilingPct = firstFinite(rawTf.theoretical_ceiling_pct);
        const rawTheoreticalCached = firstFinite(rawTf.theoretical_cached_tokens);
        let ceilingPct = null;
        if (rawCeilingPct !== null) {
            ceilingPct = Math.min(100, Math.max(cachedPct, rawCeilingPct));
        } else if (rawTheoreticalCached !== null) {
            ceilingPct = Math.min(100, Math.max(cachedPct, (rawTheoreticalCached / sent) * 100));
        }
        return {
            sent_tokens: sent,
            cached_tokens: cached,
            recomputed_tokens: recomputed,
            cold_new_tokens: coldNew,
            cached_pct: cachedPct,
            recomputed_pct: recomputedPct,
            cold_new_pct: coldNewPct,
            theoretical_ceiling_pct: ceilingPct,
        };
    }

    const recompSeriesTok = integrateRateSeries(series.map((pt) => [pt.t, pt.prompt_recomputed_tps]), stepSeconds);
    const recompSummaryMean = firstFinite(
        isDisaggregated ? numberAt(sbrPrefill, ['engine_prompt_recomputed_tps.mean', 'engine_prompt_recomputed_tps']) : null,
        numberAt(summary, ['engine_prompt_recomputed_tps.mean']),
    );
    const vllmRecompTok = recompSeriesTok
        ?? (recompSummaryMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, recompSummaryMean) * durationSeconds) : 0);

    if (isDisaggregated && prefillRow && prefillAvoidedRatio !== null) {
        const sentRaw = firstFinite(
            hasValidRawTf ? Number(rawTf.sent_tokens) : null,
            clientSent !== null && clientSent > 0 ? clientSent : null,
            Number(prefillRow.total_tokens) > 0 ? Number(prefillRow.total_tokens) : null,
        );
        if (sentRaw !== null && sentRaw > 0) {
            const sent = Math.round(sentRaw);
            const cached = Math.min(sent, Math.max(0, Math.round(sent * prefillAvoidedRatio)));
            const cachedPct = (cached / sent) * 100;

            const rawReusable = positiveFinite(
                clientSeries?.token_flow?.raw_session_reusable_tokens,
                sessionsSummary?.reusable_prefix_tokens,
            );
            const overlapPct = positiveFinite(sessionsSummary?.theoretical_prefix_overlap_pct);
            const avoidedPct = clampPercent(firstFinite(derived?.avoided_prefill_ratio));

            let theoreticalCached = null;
            if (rawReusable !== null) {
                theoreticalCached = Math.min(sent, Math.max(cached, Math.round(rawReusable)));
            } else if (overlapPct !== null) {
                theoreticalCached = Math.min(sent, Math.max(cached, Math.round((sent * Math.min(100, overlapPct)) / 100)));
            } else if (avoidedPct !== null && avoidedPct > cachedPct) {
                theoreticalCached = Math.min(sent, Math.max(cached, Math.round((sent * avoidedPct) / 100)));
            }
            const ceilingPct = theoreticalCached !== null
                ? Math.min(100, Math.max(cachedPct, (theoreticalCached / sent) * 100))
                : null;

            const theoreticalRecompTok = theoreticalCached !== null ? Math.max(0, theoreticalCached - cached) : 0;
            const recomputed = Math.min(sent - cached, Math.max(theoreticalRecompTok, Math.max(0, Math.round(vllmRecompTok))));
            const coldNew = Math.max(0, sent - cached - recomputed);
            const recomputedPct = (recomputed / sent) * 100;
            const coldNewPct = Math.max(0, 100 - cachedPct - recomputedPct);

            return {
                sent_tokens: sent,
                cached_tokens: cached,
                recomputed_tokens: recomputed,
                cold_new_tokens: coldNew,
                cached_pct: cachedPct,
                recomputed_pct: recomputedPct,
                cold_new_pct: coldNewPct,
                theoretical_ceiling_pct: ceilingPct,
            };
        }
    }

    const localCacheSeriesTok = integrateRateSeries(series.map((pt) => [pt.t, pt.prompt_local_cache_tps]), stepSeconds);
    const extCacheSeriesTok = integrateRateSeries(series.map((pt) => [pt.t, pt.prompt_external_tps]), stepSeconds);
    const freshSeriesTok = integrateRateSeries(series.map((pt) => [pt.t, pt.prompt_fresh_tps]), stepSeconds);
    const inputSeriesTok = integrateRateSeries(series.map((pt) => [pt.t, pt.input_tps]), stepSeconds);

    const localCachedMean = numberAt(summary, ['engine_prompt_local_cache_tps.mean']);
    const extCachedMean = numberAt(summary, ['engine_prompt_external_tps.mean']);
    const freshMean = numberAt(summary, ['engine_prompt_local_compute_tps.mean']);
    const promptMean = numberAt(summary, ['input_token_rate_tps.mean']);
    const prefixHitMean = numberAt(summary, ['prefix_cache_hit_percent.mean']);

    const localCacheTok = localCacheSeriesTok
        ?? (localCachedMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, localCachedMean) * durationSeconds) : null);
    const extCacheTok = extCacheSeriesTok
        ?? (extCachedMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, extCachedMean) * durationSeconds) : null);
    const freshTok = freshSeriesTok
        ?? (freshMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, freshMean) * durationSeconds) : null);
    const inputTok = inputSeriesTok
        ?? (promptMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, promptMean) * durationSeconds) : null);

    const engineTotalTok = (localCacheTok !== null || extCacheTok !== null || freshTok !== null)
        ? (localCacheTok ?? 0) + (extCacheTok ?? 0) + (freshTok ?? 0)
        : null;

    const sentRaw = firstFinite(
        clientSent !== null && clientSent > 0 ? clientSent : null,
        engineTotalTok !== null && engineTotalTok > 0 ? engineTotalTok : null,
        inputTok !== null && inputTok > 0 ? inputTok : null,
    );
    if (sentRaw === null || sentRaw <= 0) return null;

    const sent = Math.round(sentRaw);
    const clientCachedTok = firstFinite(
        clientSeries?.token_flow?.client_cached_tokens,
        clientSeries?.token_flow?.cached_tokens,
    );
    let rawCached = 0;
    if (localCacheTok !== null || extCacheTok !== null) {
        rawCached = (localCacheTok ?? 0) + (extCacheTok ?? 0);
    } else if (prefixHitMean !== null || isFiniteMetricValue(derived?.avoided_prefill_ratio)) {
        const hitPct = clampPercent(firstFinite(prefixHitMean, derived?.avoided_prefill_ratio)) ?? 0;
        rawCached = Math.round((sent * hitPct) / 100);
    } else if (freshTok !== null) {
        rawCached = Math.max(0, sent - freshTok);
    } else if (clientCachedTok !== null) {
        rawCached = clientCachedTok;
    }
    const cached = Math.min(sent, Math.max(0, Math.round(rawCached)));
    const cachedPct = (cached / sent) * 100;

    const reusableTok = firstFinite(
        clientSeries?.token_flow?.raw_session_reusable_tokens,
        sessionsSummary?.reusable_prefix_tokens,
    );
    const overlapPct = firstFinite(
        sessionsSummary?.theoretical_prefix_overlap_pct,
        clientSeries?.token_flow?.theoretical_ceiling_pct,
    );
    const avoidedPct = clampPercent(firstFinite(derived?.avoided_prefill_ratio));

    let theoreticalCached = null;
    let ceilingPct = null;
    if (reusableTok !== null && reusableTok > 0) {
        theoreticalCached = Math.min(sent, Math.max(cached, Math.round(reusableTok)));
        ceilingPct = Math.min(100, Math.max(cachedPct, (theoreticalCached / sent) * 100));
    } else if (overlapPct !== null && overlapPct > 0) {
        theoreticalCached = Math.min(sent, Math.max(cached, Math.round((sent * Math.min(100, overlapPct)) / 100)));
        ceilingPct = Math.min(100, Math.max(cachedPct, (theoreticalCached / sent) * 100));
    } else if (avoidedPct !== null && avoidedPct > cachedPct) {
        theoreticalCached = Math.min(sent, Math.max(cached, Math.round((sent * avoidedPct) / 100)));
        ceilingPct = Math.min(100, Math.max(cachedPct, (theoreticalCached / sent) * 100));
    }

    const recompRateTok = Math.max(0, Math.round(vllmRecompTok));
    const theoreticalRecompTok = theoreticalCached !== null ? Math.max(0, theoreticalCached - cached) : 0;
    const recomputed = Math.min(sent - cached, Math.max(theoreticalRecompTok, recompRateTok));

    const coldNew = Math.max(0, sent - cached - recomputed);
    const recomputedPct = (recomputed / sent) * 100;
    const coldNewPct = Math.max(0, 100 - cachedPct - recomputedPct);

    return {
        sent_tokens: sent,
        cached_tokens: cached,
        recomputed_tokens: recomputed,
        cold_new_tokens: coldNew,
        cached_pct: cachedPct,
        recomputed_pct: recomputedPct,
        cold_new_pct: coldNewPct,
        theoretical_ceiling_pct: ceilingPct,
    };
}

function resolvePromptTokensPerRequest({
    derived,
    clientSeries,
    tokenFlow,
    prefixHitMean,
    isDisaggregated = false,
    promptTokensByRoleData = [],
}) {
    const prefillRow = Array.isArray(promptTokensByRoleData)
        ? promptTokensByRoleData.find(
            (row) => String(row?.role || '').trim().toLowerCase() === 'prefill' && Number(row?.total_tokens || 0) > 0,
        )
        : null;
    const prefillAvoidedPct = prefillRow && Number(prefillRow.total_tokens) > 0
        ? ((Math.max(0, Number(prefillRow.local_cache_tokens || 0)) + Math.max(0, Number(prefillRow.external_kv_tokens || 0)))
            / Number(prefillRow.total_tokens)) * 100
        : null;

    const raw = derived?.prompt_tokens_per_request;
    if (raw && typeof raw === 'object' && isFiniteMetricValue(raw.mean_prompt_tokens) && Number(raw.mean_prompt_tokens) > 0) {
        const meanPrompt = Number(raw.mean_prompt_tokens);
        const isLegacyPdPpr = isDisaggregated && raw.role_scope !== 'prefill';
        if (isLegacyPdPpr) {
            const tokenFlowTrusted = Boolean(prefillRow || !derived?.token_flow || derived.token_flow.role_scope === 'prefill');
            const healedCachedPct = clampPercent(firstFinite(prefillAvoidedPct, tokenFlowTrusted ? tokenFlow?.cached_pct : null));
            if (healedCachedPct !== null) {
                const meanCached = meanPrompt * (healedCachedPct / 100);
                const meanComputed = Math.max(0, meanPrompt - meanCached);
                return {
                    mean_prompt_tokens: meanPrompt,
                    mean_cached_tokens: meanCached,
                    mean_computed_tokens: meanComputed,
                    cached_pct: healedCachedPct,
                    prompt_p50: firstFinite(raw.prompt_p50),
                    prompt_p90: firstFinite(raw.prompt_p90),
                    computed_p50: null,
                    computed_p90: null,
                };
            }
        }
        const meanCached = Math.min(meanPrompt, Math.max(0, firstFinite(raw.mean_cached_tokens) ?? 0));
        const meanComputed = firstFinite(raw.mean_computed_tokens) !== null
            ? Math.min(meanPrompt, Math.max(0, Number(raw.mean_computed_tokens)))
            : Math.max(0, meanPrompt - meanCached);
        const cachedPct = clampPercent(firstFinite(raw.cached_pct)) ?? (meanCached / meanPrompt) * 100;
        return {
            mean_prompt_tokens: meanPrompt,
            mean_cached_tokens: meanCached,
            mean_computed_tokens: meanComputed,
            cached_pct: cachedPct,
            prompt_p50: firstFinite(raw.prompt_p50),
            prompt_p90: firstFinite(raw.prompt_p90),
            computed_p50: isLegacyPdPpr ? null : firstFinite(raw.computed_p50),
            computed_p90: isLegacyPdPpr ? null : firstFinite(raw.computed_p90),
        };
    }

    const fullWin = clientSeries?.windows?.full || clientSeries?.windows?.profiling;
    const inputDist = fullWin?.input_tokens;
    const reqCount = firstFinite(fullWin?.request_count, fullWin?.total_requests);
    const meanPrompt = firstFinite(
        inputDist?.avg,
        inputDist?.mean,
        isFiniteMetricValue(fullWin?.total_input_tokens)
            && reqCount !== null
            && reqCount > 0
            ? Number(fullWin.total_input_tokens) / reqCount
            : null,
    );
    if (meanPrompt === null || meanPrompt <= 0) return null;

    const hitPct = clampPercent(firstFinite(isDisaggregated ? prefillAvoidedPct : null, tokenFlow?.cached_pct, prefixHitMean, derived?.avoided_prefill_ratio)) ?? 0;
    const meanCached = (meanPrompt * hitPct) / 100;
    const meanComputed = Math.max(0, meanPrompt - meanCached);
    return {
        mean_prompt_tokens: meanPrompt,
        mean_cached_tokens: meanCached,
        mean_computed_tokens: meanComputed,
        cached_pct: hitPct,
        prompt_p50: firstFinite(inputDist?.p50),
        prompt_p90: firstFinite(inputDist?.p90),
        computed_p50: null,
        computed_p90: null,
    };
}

function buildRoleSourceRow(roleLabel, localCache, externalKv, localCompute) {
    const lc = Math.max(0, Math.round(localCache || 0));
    const ek = Math.max(0, Math.round(externalKv || 0));
    const fc = Math.max(0, Math.round(localCompute || 0));
    const total = lc + ek + fc;
    if (total <= 0) return null;
    return {
        role: roleLabel,
        local_cache_tokens: lc,
        external_kv_tokens: ek,
        local_compute_tokens: fc,
        total_tokens: total,
    };
}

function resolvePromptTokensByRoleSource({
    derived,
    serverTelemetry,
    series,
    perPod,
    summary,
    isDisaggregatedTopology,
    stepSeconds,
    durationSeconds,
}) {
    const rawRows = derived?.prompt_tokens_by_role_source;
    if (Array.isArray(rawRows) && rawRows.length > 0) {
        const normalized = rawRows
            .filter((row) => row && !NON_SERVING_ROLES.has(String(row.role || '').trim().toLowerCase()))
            .map((row) => {
                const lc = Math.max(0, Math.round(firstFinite(row.local_cache_tokens) ?? 0));
                const ek = Math.max(0, Math.round(firstFinite(row.external_kv_tokens) ?? 0));
                const fc = Math.max(0, Math.round(firstFinite(row.local_compute_tokens) ?? 0));
                const total = Math.max(lc + ek + fc, Math.round(firstFinite(row.total_tokens) ?? 0));
                if (total <= 0) return null;
                return {
                    role: displayName(row.role || 'Worker'),
                    local_cache_tokens: lc,
                    external_kv_tokens: ek,
                    local_compute_tokens: fc,
                    total_tokens: total,
                };
            })
            .filter(Boolean);
        if (normalized.length > 0) return normalized;
    }

    const rawRoleSeries = Array.isArray(serverTelemetry?.role_series) ? serverTelemetry.role_series : [];
    if (isDisaggregatedTopology && rawRoleSeries.length > 0) {
        const roleRows = [];
        for (const role of ['prefill', 'decode']) {
            const samplesFor = (metric) => rawRoleSeries.map((pt, idx) => [
                series[idx]?.t ?? firstFinite(pt.elapsed_seconds, pt.time_seconds, pt.t, idx * stepSeconds),
                pt[`${role}_${metric}`],
            ]);
            const lc = integrateRateSeries(samplesFor('engine_prompt_local_cache_tps'), stepSeconds) ?? 0;
            const ek = integrateRateSeries(samplesFor('engine_prompt_external_tps'), stepSeconds) ?? 0;
            const fc = integrateRateSeries(samplesFor('engine_prompt_local_compute_tps'), stepSeconds) ?? 0;
            const row = buildRoleSourceRow(displayName(role), lc, ek, fc);
            if (row) roleRows.push(row);
        }
        if (roleRows.length > 0) return roleRows;
    }

    if (durationSeconds > 0 && perPod.length > 0) {
        const grouped = new Map();
        for (const pod of perPod) {
            const roleKey = String(effectivePodRole(pod) || 'worker').trim().toLowerCase();
            if (NON_SERVING_ROLES.has(roleKey)) continue;
            const lc = numberAt(pod, ['engine_prompt_local_cache_tps.mean', 'engine_prompt_local_cache_tps']);
            const ek = numberAt(pod, ['engine_prompt_external_tps.mean', 'engine_prompt_external_tps']);
            const fc = numberAt(pod, ['engine_prompt_local_compute_tps.mean', 'engine_prompt_local_compute_tps']);
            if (lc === null && ek === null && fc === null) continue;
            const current = grouped.get(roleKey) || { lc: 0, ek: 0, fc: 0 };
            current.lc += Math.max(0, lc ?? 0) * durationSeconds;
            current.ek += Math.max(0, ek ?? 0) * durationSeconds;
            current.fc += Math.max(0, fc ?? 0) * durationSeconds;
            grouped.set(roleKey, current);
        }
        if (grouped.size > 0) {
            const podRows = [];
            for (const [roleKey, totals] of grouped.entries()) {
                const row = buildRoleSourceRow(displayName(roleKey), totals.lc, totals.ek, totals.fc);
                if (row) podRows.push(row);
            }
            if (podRows.length > 0) return podRows;
        }
    }

    const lcSeries = integrateRateSeries(series.map((pt) => [pt.t, pt.raw_prompt_local_cache_tps ?? pt.prompt_local_cache_tps]), stepSeconds);
    const ekSeries = integrateRateSeries(series.map((pt) => [pt.t, pt.raw_prompt_external_tps ?? pt.prompt_external_tps]), stepSeconds);
    const fcSeries = integrateRateSeries(series.map((pt) => [pt.t, pt.raw_prompt_fresh_tps ?? pt.prompt_fresh_tps]), stepSeconds);

    const lcMean = numberAt(summary, ['engine_prompt_local_cache_tps.mean']);
    const ekMean = numberAt(summary, ['engine_prompt_external_tps.mean']);
    const fcMean = numberAt(summary, ['engine_prompt_local_compute_tps.mean']);

    const lcTotal = lcSeries ?? (lcMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, lcMean) * durationSeconds) : 0);
    const ekTotal = ekSeries ?? (ekMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, ekMean) * durationSeconds) : 0);
    const fcTotal = fcSeries ?? (fcMean !== null && durationSeconds > 0 ? Math.round(Math.max(0, fcMean) * durationSeconds) : 0);

    if (lcTotal + ekTotal + fcTotal <= 0) return [];

    if (!isDisaggregatedTopology) {
        const workerRow = buildRoleSourceRow('Worker', lcTotal, ekTotal, fcTotal);
        return workerRow ? [workerRow] : [];
    }

    let prefillIn = 0;
    let decodeIn = 0;
    for (const pod of perPod) {
        const roleKey = String(effectivePodRole(pod) || '').trim().toLowerCase();
        const inRate = Math.max(0, numberAt(pod, ['input_token_rate_tps.mean', 'input_token_rate_tps']) ?? 0);
        if (roleKey === 'prefill') prefillIn += inRate;
        else if (roleKey === 'decode') decodeIn += inRate;
    }

    const rows = [];
    if (prefillIn > 0 && decodeIn > 0) {
        const totalRate = prefillIn + decodeIn;
        const pShare = prefillIn / totalRate;
        const pRow = buildRoleSourceRow('Prefill', lcTotal * pShare, ekTotal * pShare, fcTotal * pShare);
        const dRow = buildRoleSourceRow(
            'Decode',
            lcTotal - (pRow?.local_cache_tokens ?? 0),
            ekTotal - (pRow?.external_kv_tokens ?? 0),
            fcTotal - (pRow?.local_compute_tokens ?? 0),
        );
        if (pRow) rows.push(pRow);
        if (dRow) rows.push(dRow);
        return rows;
    }

    if (ekTotal > 0 && (lcTotal + fcTotal) > 0) {
        const pRow = buildRoleSourceRow('Prefill', lcTotal, 0, fcTotal);
        const dRow = buildRoleSourceRow('Decode', 0, ekTotal, 0);
        if (pRow) rows.push(pRow);
        if (dRow) rows.push(dRow);
        return rows;
    }

    const hasOnlyDecodePods = perPod.length > 0
        && perPod.every((pod) => String(effectivePodRole(pod) || '').trim().toLowerCase() === 'decode');
    const soleRoleRow = buildRoleSourceRow(hasOnlyDecodePods ? 'Decode' : 'Prefill', lcTotal, ekTotal, fcTotal);
    return soleRoleRow ? [soleRoleRow] : [];
}

function findMatchingClientBucket(buckets, targetT, stepSeconds, bucketSeconds) {
    if (!Array.isArray(buckets) || !buckets.length || !isFiniteMetricValue(targetT)) return null;
    const t = Number(targetT);
    let intervalMatch = null;
    for (const bucket of buckets) {
        const start = firstFinite(bucket?.start_seconds);
        const end = firstFinite(bucket?.end_seconds);
        if (start !== null && end !== null && t >= start && t <= end) {
            if (!intervalMatch || t < end) {
                intervalMatch = bucket;
                if (t < end) break;
            }
        }
    }
    if (intervalMatch) return intervalMatch;

    const maxDist = Math.max(1, stepSeconds, bucketSeconds);
    let nearest = null;
    let bestDist = Infinity;
    for (const bucket of buckets) {
        const bt = firstFinite(bucket?.mid_seconds, bucket?.time_seconds, bucket?.elapsed_seconds, bucket?.t);
        if (bt === null) continue;
        const dist = Math.abs(bt - t);
        if (dist <= maxDist && dist < bestDist) {
            bestDist = dist;
            nearest = bucket;
        }
    }
    return nearest;
}

function buildPrefixHitSeries(series, { clientSeries, stepSeconds, ceilingPct }) {
    const buckets = Array.isArray(clientSeries?.buckets) ? clientSeries.buckets : [];
    const bucketSeconds = firstFinite(clientSeries?.bucket_seconds, 5) || 5;
    const rawRows = series.map((pt) => {
        const rawPrefix = pt.raw_prefix_hit_pct;
        const rawExt = pt.raw_ext_hit_pct;
        const inputTps = pt.input_tps;
        const localCacheTps = pt.prompt_local_cache_tps;
        const extCacheTps = pt.prompt_external_tps;
        const freshTps = pt.prompt_fresh_tps;
        const recompTps = pt.prompt_recomputed_tps;

        let prefixHitPct = null;
        let extHitPct = null;
        if (rawPrefix !== null) {
            prefixHitPct = rawPrefix;
            extHitPct = clampPercent(Math.min(rawExt ?? 0, Math.max(0, 100 - prefixHitPct)));
        } else if (rawExt !== null) {
            prefixHitPct = 0;
            extHitPct = rawExt;
        } else if ((freshTps !== null || localCacheTps !== null || extCacheTps !== null) && inputTps !== null && inputTps > 0) {
            prefixHitPct = localCacheTps !== null ? clampPercent((localCacheTps / inputTps) * 100) : 0;
            extHitPct = extCacheTps !== null
                ? clampPercent(Math.min((extCacheTps / inputTps) * 100, Math.max(0, 100 - prefixHitPct)))
                : 0;
        }

        if (prefixHitPct === null && extHitPct === null) {
            return {
                ...pt,
                prefix_hit_pct: null,
                ext_hit_pct: null,
                recompute_pct: null,
                cold_new_pct: null,
            };
        }

        const totalHitPct = clampPercent((prefixHitPct ?? 0) + (extHitPct ?? 0)) ?? 0;
        const uncachedPct = Math.max(0, Math.min(100, 100 - totalHitPct));

        let vllmRecompPct = 0;
        if (recompTps !== null && recompTps > 0 && inputTps !== null && inputTps > 0) {
            vllmRecompPct = Math.min(uncachedPct, Math.max(0, (recompTps / inputTps) * 100));
        }

        const matchingBucket = findMatchingClientBucket(buckets, pt.t, stepSeconds, bucketSeconds);
        const bucketReusablePct = isFiniteMetricValue(matchingBucket?.reusable_prefix_pct)
            ? clampPercent(Number(matchingBucket.reusable_prefix_pct))
            : null;
        const bucketHasArrivals = Boolean(
            matchingBucket
            && (
                (isFiniteMetricValue(matchingBucket.arrived_requests) && Number(matchingBucket.arrived_requests) > 0)
                || (isFiniteMetricValue(matchingBucket.arrived_rps) && Number(matchingBucket.arrived_rps) > 0)
                || (isFiniteMetricValue(matchingBucket.input_tokens_total) && Number(matchingBucket.input_tokens_total) > 0)
            ),
        );
        const isPrefillDraining = (inputTps !== null && inputTps > 0)
            || (freshTps !== null && freshTps > 0)
            || (localCacheTps !== null && localCacheTps > 0)
            || (extCacheTps !== null && extCacheTps > 0)
            || (rawPrefix !== null && inputTps === null);

        let reusablePct = null;
        if (bucketReusablePct !== null) {
            reusablePct = bucketReusablePct;
        } else if ((!matchingBucket || !bucketHasArrivals || bucketReusablePct === null) && isPrefillDraining && isFiniteMetricValue(ceilingPct)) {
            reusablePct = clampPercent(Number(ceilingPct));
        }

        const sessionRecompPct = reusablePct !== null
            ? Math.min(uncachedPct, Math.max(0, reusablePct - totalHitPct))
            : 0;
        const recomputedPct = Math.min(uncachedPct, Math.max(vllmRecompPct, sessionRecompPct));
        const coldNewPct = Math.max(0, Math.min(100, uncachedPct - recomputedPct));

        return {
            ...pt,
            prefix_hit_pct: prefixHitPct,
            ext_hit_pct: extHitPct,
            recompute_pct: recomputedPct,
            cold_new_pct: coldNewPct,
        };
    });
    return normalizeStageStackSeries(rawRows, PREFIX_HIT_STACK_KEYS);
}

function ProportionalStackedBar({
    segments,
    markerPct = null,
    markerLabel = null,
    ariaLabel,
    barHeightClass = 'h-6',
}) {
    const [isHovered, setIsHovered] = useState(false);
    const hasMarker = isFiniteMetricValue(markerPct);
    const clampedMarker = hasMarker ? Math.min(100, Math.max(0, Number(markerPct))) : null;
    const markerAlignClass = clampedMarker !== null && clampedMarker > 84
        ? '-translate-x-full'
        : clampedMarker !== null && clampedMarker < 16
            ? 'translate-x-0'
            : '-translate-x-1/2';

    return (
        <div
            className="relative w-full"
            onMouseEnter={() => setIsHovered(true)}
            onMouseLeave={() => setIsHovered(false)}
        >
            {hasMarker && markerLabel && (
                <div className="relative mb-1 h-4 w-full">
                    <span
                        className={cn(
                            'absolute top-0 whitespace-nowrap font-mono text-[10px] font-semibold text-theme-text',
                            markerAlignClass,
                        )}
                        style={{ left: `${clampedMarker}%` }}
                    >
                        {markerLabel}
                    </span>
                </div>
            )}
            <div className="relative w-full">
                <div
                    role="img"
                    aria-label={ariaLabel}
                    className={cn(
                        'flex w-full overflow-hidden rounded-lg border border-theme-border bg-theme-bg',
                        barHeightClass,
                    )}
                >
                    {segments.map((seg) => {
                        const pct = Math.min(100, Math.max(0, Number(seg.pct) || 0));
                        if (pct <= 0) return null;
                        return (
                            <div
                                key={seg.key}
                                title={`${seg.label}: ${seg.displayValue} (${formatPct(pct)}%)`}
                                className="h-full transition-opacity hover:opacity-90"
                                style={{ width: `${pct}%`, backgroundColor: seg.color }}
                            />
                        );
                    })}
                </div>
                {hasMarker && (
                    <div
                        aria-hidden="true"
                        className="pointer-events-none absolute -top-1 -bottom-1 z-10 w-[2px] bg-theme-text"
                        style={{ left: `${clampedMarker}%` }}
                    />
                )}
            </div>
            {isHovered && (
                <div className="pointer-events-none absolute left-1/2 top-full z-50 mt-2 -translate-x-1/2">
                    <ChartTooltip title={ariaLabel}>
                        {segments.map((seg) => (
                            <ChartTooltipRow
                                key={seg.key}
                                color={seg.color}
                                label={`${seg.label} (${formatPct(seg.pct)}%)`}
                                value={seg.displayValue}
                            />
                        ))}
                        {hasMarker && markerLabel && (
                            <div className="mt-1 border-t border-theme-border pt-1">
                                <ChartTooltipRow label="Theoretical Ceiling" value={`${formatPct(clampedMarker)}%`} />
                            </div>
                        )}
                    </ChartTooltip>
                </div>
            )}
        </div>
    );
}

export function ServingTab({ task = null, telemetry, scrollAnchor = null }) {
    const [activeCategory, setActiveCategory] = useState('all');
    const [flowMetric, setFlowMetric] = useState('request');
    const [flowMode, setFlowMode] = useState('replay');
    const [isPlaying, setIsPlaying] = useState(false);
    const [playbackSpeed, setPlaybackSpeed] = useState('1x');
    const [selectedFrameIndex, setSelectedFrameIndex] = useState(null);
    const [serverLatencyMode, setServerLatencyMode] = useState('ttft');
    const [stageQuantile, setStageQuantile] = useState('p50');
    const [kvTierMode, setKvTierMode] = useState('share');
    const [prevScrollAnchor, setPrevScrollAnchor] = useState(scrollAnchor);

    if (scrollAnchor !== prevScrollAnchor) {
        setPrevScrollAnchor(scrollAnchor);
        if (scrollAnchor === 's-latency') {
            setActiveCategory('all');
        }
    }

    const serverTelemetry = telemetry?.server_telemetry ?? EMPTY_OBJ;
    const isServerAvailable = serverTelemetry?.status === 'available'
        || (serverTelemetry?.available === true && serverTelemetry?.status !== 'unavailable');

    const hwProfile = serverTelemetry?.hardware_profile ?? serverTelemetry?.hardware_profiles?.[0] ?? null;
    const accelLabel = hwProfile?.ui?.labels?.metric_group
        || hwProfile?.metric_group
        || hwProfile?.display_name
        || 'Accelerator';

    const categories = useMemo(() => ([
        { id: 'all', label: 'All' },
        { id: 'flow', label: 'Topology & Replay' },
        { id: 'latency', label: 'Latency' },
        { id: 'engine', label: 'Engine · vLLM' },
        { id: 'load', label: 'Load' },
        { id: 'cache', label: 'KV Cache' },
        { id: 'kvreuse', label: 'KV Reuse' },
        { id: 'offload', label: 'KV Offload' },
        { id: 'evict', label: 'KV Eviction' },
        { id: 'power', label: `${accelLabel} Power` },
        { id: 'grafana', label: 'Grafana' },
    ]), [accelLabel]);

    const showSection = (id) => activeCategory === 'all' || activeCategory === id;

    const grafanaLinks = useMemo(() => {
        const raw = telemetry?.grafana_links ?? serverTelemetry?.grafana_links ?? [];
        return Array.isArray(raw) ? raw.filter((item) => isSafeHttpUrl(item?.url)) : [];
    }, [telemetry?.grafana_links, serverTelemetry?.grafana_links]);

    const flowData = useMemo(() => {
        if (!isServerAvailable) return null;
        return buildRecordedFlowData(serverTelemetry);
    }, [isServerAvailable, serverTelemetry]);

    const flowFrames = useMemo(() => {
        if (!isServerAvailable) return [];
        return buildRecordedFlowFrames(telemetry);
    }, [isServerAvailable, telemetry]);

    const canReplay = flowFrames.length > 1 && flowFrames.some((frame) => frame.hasTraffic);
    const hasUsableFlowMap = Boolean(flowData && flowData.components?.length > 0 && (flowData.hasTraffic || canReplay));
    const activeFlowMode = canReplay ? flowMode : 'summary';

    const defaultFrameIndex = useMemo(() => {
        if (!flowFrames.length) return 0;
        const firstServerActive = flowFrames.findIndex(
            (frame) => (frame.rps ?? 0) > 0 || (frame.outputTps ?? 0) > 0 || (frame.runningRequests ?? 0) > 0,
        );
        if (firstServerActive >= 0) return firstServerActive;
        const firstActive = flowFrames.findIndex((frame) => frame.hasTraffic);
        return firstActive >= 0 ? firstActive : 0;
    }, [flowFrames]);

    const safeFrameIndex = flowFrames.length
        ? Math.max(0, Math.min(flowFrames.length - 1, selectedFrameIndex ?? defaultFrameIndex))
        : 0;
    const activeFrame = flowFrames[safeFrameIndex] || null;
    const totalReplayDuration = flowFrames.length ? flowFrames[flowFrames.length - 1].t : 0;

    useEffect(() => {
        if (!isPlaying || !canReplay || activeFlowMode !== 'replay') return undefined;
        const intervalMs = playbackSpeed === '2x' ? 500 : 1000;
        const timer = window.setInterval(() => {
            setSelectedFrameIndex((current) => {
                const base = current ?? defaultFrameIndex;
                return (base + 1) % flowFrames.length;
            });
        }, intervalMs);
        return () => window.clearInterval(timer);
    }, [activeFlowMode, canReplay, defaultFrameIndex, flowFrames.length, isPlaying, playbackSpeed]);

    const displayedFlowData = (activeFlowMode === 'replay' && activeFrame?.flowData)
        ? activeFrame.flowData
        : flowData;

    const originEpoch = telemetry?.client_series?.origin_epoch;
    const executionStartedAt = telemetry?.run_manifest?.execution_started_at;
    const rawSeries = serverTelemetry?.series;
    const rawRoleSeries = serverTelemetry?.role_series;
    const windowStart = serverTelemetry?.window?.start;
    const cacheConfig = serverTelemetry?.cache_config || serverTelemetry?.cache_config_info || EMPTY_OBJ;
    const hbmCapBytes = positiveFinite(cacheConfig.hbm_capacity_bytes);
    const cpuCapBytes = positiveFinite(cacheConfig.cpu_capacity_bytes);
    const nvmeCapBytes = positiveFinite(cacheConfig.nvme_capacity_bytes, cacheConfig.disk_capacity_bytes);
    const hbmCapTokens = positiveFinite(cacheConfig.hbm_capacity_tokens);
    const cpuCapTokens = positiveFinite(cacheConfig.cpu_capacity_tokens);

    const summary = serverTelemetry?.summary || EMPTY_OBJ;
    const router = serverTelemetry?.router || EMPTY_OBJ;
    const derived = serverTelemetry?.derived || EMPTY_OBJ;
    const energy = serverTelemetry?.energy || EMPTY_OBJ;
    const cacheEngines = useMemo(
        () => (Array.isArray(cacheConfig?.engines) ? cacheConfig.engines : []),
        [cacheConfig],
    );
    const rawPerEndpoint = serverTelemetry?.per_endpoint;
    const perEndpoint = useMemo(
        () => (Array.isArray(rawPerEndpoint) ? rawPerEndpoint : []),
        [rawPerEndpoint],
    );
    const deviceTelemetry = serverTelemetry?.device_telemetry || EMPTY_OBJ;
    const allocatedDevices = Array.isArray(deviceTelemetry?.devices) ? deviceTelemetry.devices : [];
    const metricSources = serverTelemetry?.metric_sources || EMPTY_OBJ;
    const sbrPrefill = serverTelemetry?.summary_by_role?.prefill || EMPTY_OBJ;
    const sbrDecode = serverTelemetry?.summary_by_role?.decode || EMPTY_OBJ;
    const roleSummaryPrefill = derived?.role_summary?.prefill || EMPTY_OBJ;
    const roleSummaryDecode = derived?.role_summary?.decode || EMPTY_OBJ;

    const rawPerPod = serverTelemetry?.per_pod;
    const perPod = useMemo(
        () => (Array.isArray(rawPerPod) ? rawPerPod : []).filter((pod) => !isBenchmarkRunnerPod(pod)),
        [rawPerPod],
    );

    const rawRoleSeriesList = useMemo(
        () => (Array.isArray(rawRoleSeries) ? rawRoleSeries : []),
        [rawRoleSeries],
    );
    const roleByTs = useMemo(() => {
        const map = new Map();
        for (const rp of rawRoleSeriesList) {
            if (rp?.timestamp != null && rp.timestamp !== '') {
                map.set(rp.timestamp, rp);
            }
        }
        return map;
    }, [rawRoleSeriesList]);

    const rawPromptByRoleRows = useMemo(
        () => (Array.isArray(derived?.prompt_tokens_by_role_source) ? derived.prompt_tokens_by_role_source : []),
        [derived],
    );
    const rawTopologyMode = String(
        serverTelemetry?.topology_mode
        || serverTelemetry?.topology?.mode
        || telemetry?.run_manifest?.target?.topology_mode
        || 'aggregated',
    ).toLowerCase();
    const hasPrefillEvidence = useMemo(() => (
        perPod.some((p) => effectivePodRole(p) === 'prefill')
        || rawRoleSeriesList.some((rp) => rp && Object.keys(rp).some((k) => k.startsWith('prefill_') && isFiniteMetricValue(rp[k])))
        || Boolean(serverTelemetry?.summary_by_role?.prefill && Object.keys(serverTelemetry.summary_by_role.prefill).length)
        || Boolean(derived?.role_summary?.prefill && Object.keys(derived.role_summary.prefill).length)
        || rawPromptByRoleRows.some((r) => String(r?.role || '').trim().toLowerCase() === 'prefill' && Number(r?.total_tokens || 0) > 0)
    ), [perPod, rawRoleSeriesList, serverTelemetry, derived, rawPromptByRoleRows]);

    const isDisaggregated = rawTopologyMode === 'disaggregated' && hasPrefillEvidence;
    const isDisaggregatedTopology = isDisaggregated;
    const isPdCanonical = metricSources?.pd_canonical_metrics === 'role_separated';

    const rawPrefillSourceRow = useMemo(() => {
        const match = rawPromptByRoleRows.find(
            (r) => String(r?.role || '').trim().toLowerCase() === 'prefill' && Number(r?.total_tokens || 0) > 0,
        );
        if (!match) return null;
        const lc = Math.max(0, Math.round(firstFinite(match.local_cache_tokens) ?? 0));
        const ek = Math.max(0, Math.round(firstFinite(match.external_kv_tokens) ?? 0));
        const fc = Math.max(0, Math.round(firstFinite(match.local_compute_tokens) ?? 0));
        const total = Math.max(lc + ek + fc, Math.round(firstFinite(match.total_tokens) ?? 0));
        return total > 0 ? { local_cache_tokens: lc, external_kv_tokens: ek, local_compute_tokens: fc, total_tokens: total } : null;
    }, [rawPromptByRoleRows]);

    const rawDecodeSourceRow = useMemo(() => {
        const match = rawPromptByRoleRows.find(
            (r) => String(r?.role || '').trim().toLowerCase() === 'decode' && Number(r?.total_tokens || 0) > 0,
        );
        if (!match) return null;
        const lc = Math.max(0, Math.round(firstFinite(match.local_cache_tokens) ?? 0));
        const ek = Math.max(0, Math.round(firstFinite(match.external_kv_tokens) ?? 0));
        const fc = Math.max(0, Math.round(firstFinite(match.local_compute_tokens) ?? 0));
        const total = Math.max(lc + ek + fc, Math.round(firstFinite(match.total_tokens) ?? 0));
        return total > 0 ? { local_cache_tokens: lc, external_kv_tokens: ek, local_compute_tokens: fc, total_tokens: total } : null;
    }, [rawPromptByRoleRows]);

    const prefillLocalCacheShare = useMemo(() => {
        if (rawPrefillSourceRow && rawDecodeSourceRow) {
            const pLc = rawPrefillSourceRow.local_cache_tokens;
            const dLc = rawDecodeSourceRow.local_cache_tokens;
            if (pLc + dLc > 0) {
                return pLc / (pLc + dLc);
            }
        }
        return 1.0;
    }, [rawPrefillSourceRow, rawDecodeSourceRow]);

    const { series, hostConfigured, nvmeConfigured } = useMemo(() => {
        const raw = Array.isArray(rawSeries) ? rawSeries : [];
        const roles = rawRoleSeriesList;
        const originCandidates = [
            originEpoch ? originEpoch * 1000 : null,
            windowStart ? Date.parse(windowStart) : null,
            executionStartedAt ? Date.parse(executionStartedAt) : null,
            raw[0]?.timestamp ? Date.parse(raw[0].timestamp) : null,
        ];
        const originMs = originCandidates.find((v) => typeof v === 'number' && Number.isFinite(v)) ?? null;
        const basePoints = raw.map((point, idx) => {
            const rolePt = (point.timestamp != null && point.timestamp !== '' ? roleByTs.get(point.timestamp) : null)
                || roles[idx]
                || EMPTY_OBJ;
            const parsedTs = point.timestamp ? Date.parse(point.timestamp) : NaN;
            const relSeconds = originMs !== null && Number.isFinite(parsedTs)
                ? Math.max(0, (parsedTs - originMs) / 1000)
                : idx;
            const ttftP50 = firstFinite(point.vllm_ttft_p50_ms, point.ttft_p50_ms);
            const ttftP90 = firstFinite(point.vllm_ttft_p90_ms, point.ttft_p90_ms);
            const tpotP50 = firstFinite(point.vllm_tpot_p50_ms, point.tpot_p50_ms);
            const tpotP90 = firstFinite(point.vllm_tpot_p90_ms, point.tpot_p90_ms);
            const e2eP50 = firstFinite(point.vllm_e2e_p50_ms, point.e2e_p50_ms);
            const e2eP90 = firstFinite(point.vllm_e2e_p90_ms, point.e2e_p90_ms);

            const rawLocalCacheTps = firstFinite(point.engine_prompt_local_cache_tps);
            const rawExtCacheTps = firstFinite(point.engine_prompt_external_tps);
            const rawFreshComputeTps = firstFinite(point.engine_prompt_local_compute_tps);
            const recomputedTps = firstFinite(
                isDisaggregated ? rolePt.prefill_engine_prompt_recomputed_tps : null,
                point.engine_prompt_recomputed_tps,
            );

            let localCacheTps = rawLocalCacheTps;
            let extCacheTps = rawExtCacheTps;
            let freshComputeTps = rawFreshComputeTps;
            if (isDisaggregated) {
                localCacheTps = firstFinite(
                    rolePt.prefill_engine_prompt_local_cache_tps,
                    rawLocalCacheTps !== null ? (isPdCanonical ? rawLocalCacheTps : rawLocalCacheTps * prefillLocalCacheShare) : null,
                );
                if (isFiniteMetricValue(rolePt.prefill_engine_prompt_external_tps)) {
                    extCacheTps = Number(rolePt.prefill_engine_prompt_external_tps);
                } else if (rawPrefillSourceRow && rawPrefillSourceRow.external_kv_tokens > 0 && rawExtCacheTps !== null) {
                    const extDenom = rawPrefillSourceRow.external_kv_tokens + (rawDecodeSourceRow?.external_kv_tokens ?? 0);
                    extCacheTps = extDenom > 0 ? rawExtCacheTps * (rawPrefillSourceRow.external_kv_tokens / extDenom) : 0;
                } else {
                    extCacheTps = (localCacheTps !== null || rawFreshComputeTps !== null || isFiniteMetricValue(rolePt.prefill_engine_prompt_local_compute_tps))
                        ? 0
                        : null;
                }
                freshComputeTps = firstFinite(rolePt.prefill_engine_prompt_local_compute_tps, rawFreshComputeTps);
            }

            const cachedPromptTps = localCacheTps !== null || extCacheTps !== null
                ? Math.max(0, localCacheTps ?? 0) + Math.max(0, extCacheTps ?? 0)
                : null;
            const inputTps = isDisaggregated
                ? firstFinite(rolePt.prefill_input_token_rate_tps, point.input_token_rate_tps, point.prompt_tokens_tps)
                : firstFinite(point.input_token_rate_tps, point.prompt_tokens_tps);
            const outputTps = isDisaggregated
                ? firstFinite(rolePt.decode_output_token_rate_tps, point.output_token_rate_tps, point.generation_tokens_tps)
                : firstFinite(point.output_token_rate_tps, point.generation_tokens_tps);
            const engineRps = isDisaggregated
                ? firstFinite(rolePt.decode_request_rate_rps, rolePt.prefill_request_rate_rps, point.request_rate_rps)
                : firstFinite(point.request_rate_rps);
            const rawPrefixHitPct = isDisaggregated
                ? clampPercent(firstFinite(rolePt.prefill_prefix_cache_hit_percent, point.prefix_cache_hit_percent))
                : clampPercent(firstFinite(point.prefix_cache_hit_percent));
            const rawExtHitPct = isDisaggregated
                ? clampPercent(firstFinite(rolePt.prefill_external_prefix_cache_hit_percent))
                : clampPercent(firstFinite(point.external_prefix_cache_hit_percent));
            const rawKvPct = clampPercent(firstFinite(point.kv_cache_usage_percent, point.gpu_cache_usage_percent));
            const decodeKvPct = isDisaggregated
                ? clampPercent(firstFinite(rolePt.decode_kv_cache_usage_max_percent, rolePt.decode_kv_cache_usage_percent))
                : null;
            const prefillKvPct = isDisaggregated
                ? clampPercent(firstFinite(rolePt.prefill_kv_cache_usage_max_percent, rolePt.prefill_kv_cache_usage_percent))
                : null;
            const effectiveKvPct = isDisaggregated
                ? (maxFinite(decodeKvPct, prefillKvPct) ?? rawKvPct)
                : rawKvPct;

            const offloadBps = firstFinite(point.kv_offload_bytes_per_second);
            const restoreBps = firstFinite(point.kv_restore_bytes_per_second);
            const nixlBps = firstFinite(point.nixl_transfer_bytes_per_second, point.nixl_bytes_per_second);
            return {
                t: firstFinite(point.elapsed_seconds, point.time_seconds, point.t, relSeconds),
                ttft_p50: ttftP50,
                ttft_p90: ttftP90,
                ttft_band: ttftP50 !== null && ttftP90 !== null ? Math.max(0, ttftP90 - ttftP50) : null,
                tpot_p50: tpotP50,
                tpot_p90: tpotP90,
                tpot_band: tpotP50 !== null && tpotP90 !== null ? Math.max(0, tpotP90 - tpotP50) : null,
                e2e_p50: e2eP50,
                e2e_p90: e2eP90,
                e2e_band: e2eP50 !== null && e2eP90 !== null ? Math.max(0, e2eP90 - e2eP50) : null,
                queue_ms: firstFinite(point.vllm_queue_time_p50_ms, point.queue_time_p50_ms),
                prefill_ms: firstFinite(point.vllm_prefill_time_p50_ms, point.prefill_time_p50_ms),
                decode_ms: firstFinite(point.vllm_decode_time_p50_ms, point.decode_time_p50_ms),
                epp_sched_ms: firstFinite(point.router_scheduler_latency_p50_ms, point.router_scheduler_e2e_p50_ms),
                queue_p90_ms: firstFinite(point.vllm_queue_time_p90_ms, point.queue_time_p90_ms),
                prefill_p90_ms: firstFinite(point.vllm_prefill_time_p90_ms, point.prefill_time_p90_ms),
                decode_p90_ms: firstFinite(point.vllm_decode_time_p90_ms, point.decode_time_p90_ms),
                epp_sched_p90_ms: firstFinite(point.router_scheduler_latency_p90_ms, point.router_scheduler_e2e_p90_ms),
                prompt_cached_tps: cachedPromptTps,
                prompt_local_cache_tps: localCacheTps,
                prompt_external_tps: extCacheTps,
                prompt_fresh_tps: freshComputeTps,
                prompt_recomputed_tps: recomputedTps,
                raw_prompt_local_cache_tps: rawLocalCacheTps,
                raw_prompt_external_tps: rawExtCacheTps,
                raw_prompt_fresh_tps: rawFreshComputeTps,
                input_tps: inputTps,
                output_tps: outputTps,
                running: firstFinite(point.running_requests, point.vllm_num_requests_running),
                waiting: firstFinite(point.queue_depth, point.waiting_requests, point.vllm_num_requests_waiting),
                prefill_running: firstFinite(rolePt.prefill_running_requests),
                prefill_waiting: firstFinite(rolePt.prefill_waiting_requests),
                decode_running: firstFinite(rolePt.decode_running_requests),
                decode_waiting: firstFinite(rolePt.decode_waiting_requests),
                epp_running: firstFinite(point.epp_inflight_requests, point.router_request_running, point.router_running_requests),
                router_rps: firstFinite(point.router_request_rate_rps, point.router_scheduler_attempt_rate_rps),
                engine_rps: engineRps,
                epp_sched_p50: firstFinite(point.router_scheduler_latency_p50_ms, point.router_scheduler_e2e_p50_ms),
                epp_sched_p90: firstFinite(point.router_scheduler_latency_p90_ms, point.router_scheduler_e2e_p90_ms),
                epp_plugin_p50: firstFinite(point.router_plugin_latency_p50_ms, point.plugin_duration_p50_ms),
                epp_plugin_p90: firstFinite(point.router_plugin_latency_p90_ms, point.plugin_duration_p90_ms),
                kv_pct: effectiveKvPct,
                decode_kv_pct: decodeKvPct,
                prefill_kv_pct: prefillKvPct,
                raw_prefix_hit_pct: rawPrefixHitPct,
                raw_ext_hit_pct: rawExtHitPct,
                cpu_kv_pct: clampPercent(firstFinite(point.cpu_cache_usage_percent)),
                disk_kv_pct: clampPercent(firstFinite(point.disk_cache_usage_percent)),
                kv_offload_mbps: offloadBps !== null ? offloadBps / (1024 ** 2) : null,
                kv_restore_mbps: restoreBps !== null ? restoreBps / (1024 ** 2) : null,
                nixl_mbps: nixlBps !== null ? nixlBps / (1024 ** 2) : null,
                preemptions_rate: firstFinite(point.vllm_preemptions_per_second, point.preemptions_per_second),
                preemptions_total: firstFinite(point.vllm_preemptions_total, point.num_preemptions_total),
                kv_blocks_evicted_per_min: firstFinite(point.kv_blocks_evicted_per_minute),
                kv_idle_evict_p50_s: firstFinite(point.kv_block_idle_before_evict_p50_seconds),
                kv_idle_evict_p90_s: firstFinite(point.kv_block_idle_before_evict_p90_seconds),
                kv_lifetime_p50_s: firstFinite(point.kv_block_lifetime_p50_seconds),
                kv_lifetime_p90_s: firstFinite(point.kv_block_lifetime_p90_seconds),
                kv_reuse_gap_p50_s: firstFinite(point.kv_block_reuse_gap_p50_seconds),
                kv_reuse_gap_p90_s: firstFinite(point.kv_block_reuse_gap_p90_seconds),
                accel_util: clampPercent(firstFinite(
                    point.gpu_utilization_percent,
                    point.gpu_utilization,
                    point.accelerator_utilization,
                    point.utilization_percent,
                )),
                accel_mem_bw: clampPercent(firstFinite(
                    point.gpu_memory_bandwidth_utilization_percent,
                    point.gpu_memory_bandwidth_percent,
                    point.memory_bandwidth_percent,
                )),
                accel_power_w: firstFinite(point.gpu_power_watts, point.power_watts, point.accelerator_power_watts),
                accel_temp_c: firstFinite(point.gpu_temperature_celsius, point.temperature_celsius, point.accelerator_temperature_celsius),
            };
        });

        const hasAnyPositiveCpuUsage = basePoints.some((pt) => pt.cpu_kv_pct !== null && pt.cpu_kv_pct > 0);
        const hasAnyPositiveDiskUsage = basePoints.some((pt) => pt.disk_kv_pct !== null && pt.disk_kv_pct > 0);
        const isHostConfigured = Boolean(cacheConfig.host_kv_configured || cpuCapBytes || cpuCapTokens || hasAnyPositiveCpuUsage);
        const isNvmeConfigured = Boolean(cacheConfig.nvme_kv_configured || nvmeCapBytes || hasAnyPositiveDiskUsage);

        const enrichedPoints = basePoints.map((pt) => {
            const hasAnyTierSample = pt.kv_pct !== null || pt.cpu_kv_pct !== null || pt.disk_kv_pct !== null;
            if (!hasAnyTierSample) {
                return {
                    ...pt,
                    hbm_share_pct: null,
                    host_share_pct: null,
                    nvme_share_pct: null,
                };
            }
            const hostActive = (pt.cpu_kv_pct ?? 0) > 0;
            const nvmeActive = (pt.disk_kv_pct ?? 0) > 0;
            const hbmVal = Math.max(0, pt.kv_pct ?? 0);
            const hostVal = Math.max(0, pt.cpu_kv_pct ?? 0);
            const nvmeVal = Math.max(0, pt.disk_kv_pct ?? 0);

            let uHbm;
            let uHost;
            let uNvme;
            if (hbmCapBytes && (!hostActive || cpuCapBytes) && (!nvmeActive || nvmeCapBytes)) {
                uHbm = (hbmVal / 100) * hbmCapBytes;
                uHost = hostActive ? (hostVal / 100) * cpuCapBytes : 0;
                uNvme = nvmeActive ? (nvmeVal / 100) * nvmeCapBytes : 0;
            } else if (hbmCapTokens && (!hostActive || cpuCapTokens) && !nvmeActive) {
                uHbm = (hbmVal / 100) * hbmCapTokens;
                uHost = hostActive ? (hostVal / 100) * cpuCapTokens : 0;
                uNvme = 0;
            } else {
                uHbm = hbmVal;
                uHost = hostVal;
                uNvme = nvmeVal;
            }

            const totalU = uHbm + uHost + uNvme;
            if (totalU > 0) {
                return {
                    ...pt,
                    hbm_share_pct: (uHbm / totalU) * 100,
                    host_share_pct: (uHost / totalU) * 100,
                    nvme_share_pct: (uNvme / totalU) * 100,
                };
            }

            if (pt.kv_pct !== null) {
                if ((isHostConfigured || isNvmeConfigured)
                    && hbmCapBytes
                    && (!isHostConfigured || cpuCapBytes)
                    && (!isNvmeConfigured || nvmeCapBytes)) {
                    const capHost = isHostConfigured ? cpuCapBytes : 0;
                    const capNvme = isNvmeConfigured ? nvmeCapBytes : 0;
                    const capTotal = hbmCapBytes + capHost + capNvme;
                    if (capTotal > 0) {
                        return {
                            ...pt,
                            hbm_share_pct: (hbmCapBytes / capTotal) * 100,
                            host_share_pct: (capHost / capTotal) * 100,
                            nvme_share_pct: (capNvme / capTotal) * 100,
                        };
                    }
                }
                if (isHostConfigured && !isNvmeConfigured && hbmCapTokens && cpuCapTokens) {
                    const capTotal = hbmCapTokens + cpuCapTokens;
                    if (capTotal > 0) {
                        return {
                            ...pt,
                            hbm_share_pct: (hbmCapTokens / capTotal) * 100,
                            host_share_pct: (cpuCapTokens / capTotal) * 100,
                            nvme_share_pct: 0,
                        };
                    }
                }
                return {
                    ...pt,
                    hbm_share_pct: 100,
                    host_share_pct: 0,
                    nvme_share_pct: 0,
                };
            }

            return {
                ...pt,
                hbm_share_pct: null,
                host_share_pct: null,
                nvme_share_pct: null,
            };
        });

        return {
            series: enrichedPoints,
            hostConfigured: isHostConfigured,
            nvmeConfigured: isNvmeConfigured,
        };
    }, [
        rawSeries,
        rawRoleSeriesList,
        roleByTs,
        originEpoch,
        windowStart,
        executionStartedAt,
        cacheConfig,
        hbmCapBytes,
        cpuCapBytes,
        nvmeCapBytes,
        hbmCapTokens,
        cpuCapTokens,
        isDisaggregated,
        isPdCanonical,
        prefillLocalCacheShare,
        rawPrefillSourceRow,
        rawDecodeSourceRow,
    ]);

    const hasDecodeKv = series.some((pt) => pt.decode_kv_pct !== null);
    const hasPrefillKv = series.some((pt) => pt.prefill_kv_pct !== null);
    const promptCacheSplitSeries = useMemo(() => normalizeStageStackSeries(series, PROMPT_CACHE_STACK_KEYS), [series]);
    const promptSourceSeries = useMemo(() => normalizeStageStackSeries(series, PROMPT_SOURCE_STACK_KEYS), [series]);
    const forensics = telemetry?.forensics ?? EMPTY_OBJ;
    const stageView = useMemo(
        () => buildStageLatencyView(forensics, serverTelemetry, telemetry?.client_series),
        [forensics, serverTelemetry, telemetry?.client_series],
    );
    const legacyPooledPd = useMemo(
        () => stageView.isLegacyPooledPd || isLegacyPooledPd(forensics, serverTelemetry),
        [stageView.isLegacyPooledPd, forensics, serverTelemetry],
    );
    const stageLayers = useMemo(
        () => stageAreaLayers(stageView.source, stageView.hasKvTransfer),
        [stageView.source, stageView.hasKvTransfer],
    );
    const activeStageKeys = useMemo(() => stageLayers.map((layer) => layer.key), [stageLayers]);

    const hasDisaggregatedRoles = series.some((pt) => pt.prefill_running !== null || pt.prefill_waiting !== null);

    // Latency & Stage KPIs
    const serverTtftP50 = numberAt(summary, ['vllm_ttft_p50_ms.mean', 'ttft_p50_ms.mean', 'server_ttft_p50_ms']);
    const serverTtftP90 = numberAt(summary, ['vllm_ttft_p90_ms.mean', 'ttft_p90_ms.mean', 'server_ttft_p90_ms']);
    const serverTtftP99 = numberAt(summary, ['vllm_ttft_p99_ms.mean', 'ttft_p99_ms.mean']);
    const serverTpotP50 = numberAt(summary, ['vllm_tpot_p50_ms.mean', 'tpot_p50_ms.mean', 'server_tpot_p50_ms']);
    const serverTpotP90 = numberAt(summary, ['vllm_tpot_p90_ms.mean', 'tpot_p90_ms.mean', 'server_tpot_p90_ms']);
    const serverTpotP99 = numberAt(summary, ['vllm_tpot_p99_ms.mean', 'tpot_p99_ms.mean']);
    const serverE2eP50 = numberAt(summary, ['vllm_e2e_p50_ms.mean', 'e2e_p50_ms.mean', 'server_e2e_p50_ms']);
    const serverE2eP90 = numberAt(summary, ['vllm_e2e_p90_ms.mean', 'e2e_p90_ms.mean', 'server_e2e_p90_ms']);
    const serverE2eP99 = numberAt(summary, ['vllm_e2e_p99_ms.mean', 'e2e_p99_ms.mean']);

    const stageQueueP50 = numberAt(summary, ['vllm_queue_time_p50_ms.mean']);
    const stageQueueP90 = numberAt(summary, ['vllm_queue_time_p90_ms.mean']);
    const stagePrefillP50 = numberAt(summary, ['vllm_prefill_time_p50_ms.mean']);
    const stagePrefillP90 = numberAt(summary, ['vllm_prefill_time_p90_ms.mean']);
    const stageDecodeP50 = numberAt(summary, ['vllm_decode_time_p50_ms.mean']);
    const stageDecodeP90 = numberAt(summary, ['vllm_decode_time_p90_ms.mean']);

    // Engine KPIs
    const runningMean = numberAt(summary, ['running_requests.mean', 'vllm_num_requests_running.mean']);
    const runningMax = numberAt(summary, ['running_requests.max', 'vllm_num_requests_running.max']);
    const waitingMean = numberAt(summary, ['queue_depth.mean', 'waiting_requests.mean', 'vllm_num_requests_waiting.mean']);
    const waitingMax = numberAt(summary, ['queue_depth.max', 'waiting_requests.max', 'vllm_num_requests_waiting.max']);
    const promptTpsMean = isDisaggregated
        ? firstFinite(
            numberAt(sbrPrefill, ['input_token_rate_tps.mean', 'input_token_rate_tps']),
            numberAt(roleSummaryPrefill, ['input_token_rate_tps']),
            meanSeriesValue(series, 'input_tps'),
            numberAt(summary, ['input_token_rate_tps.mean']),
        )
        : numberAt(summary, ['input_token_rate_tps.mean']);
    const promptTpsMax = isDisaggregated
        ? firstFinite(
            numberAt(sbrPrefill, ['input_token_rate_tps.max']),
            maxSeriesValue(series, 'input_tps'),
            numberAt(summary, ['input_token_rate_tps.max']),
        )
        : numberAt(summary, ['input_token_rate_tps.max']);
    const genTpsMean = isDisaggregated
        ? firstFinite(
            numberAt(sbrDecode, ['output_token_rate_tps.mean', 'output_token_rate_tps']),
            numberAt(roleSummaryDecode, ['output_token_rate_tps']),
            meanSeriesValue(series, 'output_tps'),
            numberAt(summary, ['output_token_rate_tps.mean']),
        )
        : numberAt(summary, ['output_token_rate_tps.mean']);
    const genTpsMax = isDisaggregated
        ? firstFinite(
            numberAt(sbrDecode, ['output_token_rate_tps.max']),
            maxSeriesValue(series, 'output_tps'),
            numberAt(summary, ['output_token_rate_tps.max']),
        )
        : numberAt(summary, ['output_token_rate_tps.max']);

    // Load & EPP KPIs
    const eppInflightMean = numberAt(summary, ['epp_inflight_requests.mean', 'epp_inflight_requests']);
    const eppInflightMax = numberAt(summary, ['epp_inflight_requests.max']);
    const eppSchedulerP50 = numberAt(router, ['scheduler_latency_p50_ms.mean', 'scheduler_e2e_p50_ms.mean', 'scheduler_e2e_p50_ms', 'scheduler_latency_p50_ms', 'e2e_duration_p50_ms']);
    const eppSchedulerP90 = numberAt(router, ['scheduler_latency_p90_ms.mean', 'scheduler_latency_p95_ms.mean', 'scheduler_e2e_p90_ms.mean', 'scheduler_e2e_p90_ms', 'scheduler_latency_p90_ms', 'e2e_duration_p90_ms']);
    const eppPluginP50 = numberAt(router, ['plugin_latency_p50_ms.mean', 'plugin_duration_p50_ms.mean', 'plugin_latency_p50_ms', 'plugin_duration_p50_ms']);
    const eppPluginP90 = numberAt(router, ['plugin_latency_p90_ms.mean', 'plugin_duration_p90_ms.mean', 'plugin_latency_p90_ms', 'plugin_duration_p90_ms', 'plugin_duration_mean_ms']);
    const eppReadyEndpoints = numberAt(router, ['ready_endpoints.mean', 'ready_endpoints']);
    const istioP90Ms = numberAt(router, ['istio_latency_p90_ms.mean', 'istio_request_duration_p90_ms.mean', 'istio_request_duration_p90_ms'])
        ?? numberAt(summary, ['istio_latency_p90_ms.mean', 'istio_request_duration_p90_ms.mean']);
    const pdDecisionRatio = numberAt(derived, ['pd_decision_ratio']);
    const isPdRoutingTile = isDisaggregatedTopology || pdDecisionRatio !== null;
    const routerCachedTps = numberAt(router, ['cached_token_rate_tps.mean', 'cached_token_rate_tps']);
    const routerInputTps = numberAt(router, ['input_token_rate_tps.mean', 'input_token_rate_tps']);
    const fallbackEppCacheHit = routerInputTps !== null && routerInputTps > 0 && routerCachedTps !== null
        ? Math.min(100, Math.max(0, (routerCachedTps / routerInputTps) * 100))
        : null;
    const eppPredictedCacheHit = numberAt(derived, ['avoided_prefill_ratio']) ?? fallbackEppCacheHit;
    const prefixIndexEntries = numberAt(router, ['index_entry_count.mean', 'index_entry_count']);
    const hasEppData = Object.keys(router).length > 0
        || eppInflightMean !== null
        || series.some((pt) => (
            pt.epp_running !== null
            || pt.router_rps !== null
            || pt.epp_sched_p50 !== null
            || pt.epp_sched_p90 !== null
            || pt.epp_plugin_p50 !== null
            || pt.epp_plugin_p90 !== null
        ));

    const effectiveTokenLoadCv = useMemo(
        () => resolveEffectiveTokenLoadCv(derived, perEndpoint, perPod, isDisaggregated),
        [derived, perEndpoint, perPod, isDisaggregated],
    );

    // Endpoint distribution
    const endpointShareRows = useMemo(() => {
        const podNames = [];
        const podRoleLookup = new Map();
        for (const pod of perPod) {
            const name = String(pod?.pod || pod?.name || '').trim();
            if (!name) continue;
            podNames.push(name);
            podRoleLookup.set(name, String(effectivePodRole(pod) || 'worker').trim().toLowerCase());
        }
        const inflightByPod = new Map();
        for (const ep of perEndpoint) {
            const load = firstFinite(ep?.inflight_token_load?.mean, ep?.inflight_token_load);
            if (load === null) continue;
            const epPod = String(ep?.pod || '').trim();
            const epName = String(ep?.endpoint || epPod || '').trim();
            const matchedPod = (epPod && podNames.includes(epPod) ? epPod : null)
                || matchEndpointToPod(epName, podNames)
                || epName.replace(/-rank-\d+$/, '');
            if (!matchedPod) continue;
            inflightByPod.set(matchedPod, (inflightByPod.get(matchedPod) ?? 0) + load);
        }
        const inflightKeys = [...inflightByPod.keys()];
        const attachInflightLoad = (row) => {
            if (row?.inflight_token_load != null) {
                return firstFinite(row.inflight_token_load?.mean, row.inflight_token_load);
            }
            const rowPod = String(row?.pod || row?.endpoint || row?.destination || '').trim();
            if (!rowPod) return null;
            const matched = (inflightByPod.has(rowPod) ? rowPod : null)
                || matchEndpointToPod(rowPod, inflightKeys)
                || (inflightByPod.has(rowPod.replace(/-rank-\d+$/, '')) ? rowPod.replace(/-rank-\d+$/, '') : null);
            return matched ? (inflightByPod.get(matched) ?? null) : null;
        };

        if (isDisaggregated) {
            const prefillDist = Array.isArray(derived.prefill_endpoint_distribution)
                ? derived.prefill_endpoint_distribution.map((r) => ({ ...r, role: r?.role || 'prefill' }))
                : [];
            const decodeDist = Array.isArray(derived.decode_endpoint_distribution)
                ? derived.decode_endpoint_distribution.map((r) => ({ ...r, role: r?.role || 'decode' }))
                : [];
            let rawRows = [];
            if (prefillDist.length > 0 || decodeDist.length > 0) {
                rawRows = [...prefillDist, ...decodeDist];
            } else if (Array.isArray(derived.request_destination_distribution) && derived.request_destination_distribution.length > 0) {
                rawRows = derived.request_destination_distribution;
            } else {
                rawRows = perEndpoint.map((ep) => ({
                    endpoint: ep.endpoint || ep.pod || 'endpoint',
                    pod: ep.pod || null,
                    role: ep.role || null,
                    request_rate_rps: ep.request_rate_rps?.mean ?? ep.request_rate_rps ?? null,
                    inflight_token_load: ep.inflight_token_load?.mean ?? ep.inflight_token_load ?? null,
                    waiting_requests: ep.waiting_requests?.mean ?? null,
                }));
            }
            const taggedRows = rawRows.map((row) => ({
                ...row,
                role: inferEndpointRole(row, podRoleLookup, podNames),
                inflight_token_load: attachInflightLoad(row),
            }));
            const byRole = new Map();
            for (const row of taggedRows) {
                if (!byRole.has(row.role)) byRole.set(row.role, []);
                byRole.get(row.role).push(row);
            }
            for (const group of byRole.values()) {
                const roleRateSum = group.reduce((acc, row) => (
                    isFiniteMetricValue(row.request_rate_rps) && Number(row.request_rate_rps) > 0
                        ? acc + Number(row.request_rate_rps)
                        : acc
                ), 0);
                if (roleRateSum > 0) {
                    for (const row of group) {
                        const rate = firstFinite(row.request_rate_rps);
                        row.percent = rate !== null && rate >= 0 ? (rate / roleRateSum) * 100 : null;
                    }
                } else {
                    const rolePctSum = group.reduce((acc, row) => {
                        const rawPct = firstFinite(row.percent, row.share_percent);
                        return rawPct !== null && rawPct > 0 ? acc + rawPct : acc;
                    }, 0);
                    for (const row of group) {
                        const rawPct = firstFinite(row.percent, row.share_percent);
                        row.percent = rolePctSum > 0 && rawPct !== null && rawPct >= 0
                            ? (rawPct / rolePctSum) * 100
                            : null;
                    }
                }
            }
            return taggedRows;
        }

        const dist = Array.isArray(derived.request_destination_distribution) && derived.request_destination_distribution.length > 0
            ? derived.request_destination_distribution
            : Array.isArray(derived.decode_endpoint_distribution)
                ? derived.decode_endpoint_distribution
                : [];
        if (dist.length > 0) {
            return dist.map((row) => ({
                ...row,
                inflight_token_load: attachInflightLoad(row),
            }));
        }
        return perEndpoint.map((ep) => ({
            endpoint: ep.endpoint || ep.pod || 'endpoint',
            inflight_token_load: ep.inflight_token_load?.mean ?? null,
            waiting_requests: ep.waiting_requests?.mean ?? null,
        }));
    }, [
        isDisaggregated,
        derived.prefill_endpoint_distribution,
        derived.decode_endpoint_distribution,
        derived.request_destination_distribution,
        perEndpoint,
        perPod,
    ]);

    // KV Cache & Reuse KPIs
    const perPodKvPeaks = perPod
        .filter((pod) => !NON_SERVING_ROLES.has(String(effectivePodRole(pod) || '').trim().toLowerCase()))
        .map((pod) => numberAt(pod, ['kv_cache_usage_percent.max', 'gpu_cache_usage_percent.max', 'kv_cache_usage_percent.mean', 'kv_cache_usage_percent']));
    const kvPeakPct = isDisaggregated
        ? maxFinite(
            numberAt(sbrDecode, ['kv_cache_usage_max_percent.max', 'kv_cache_usage_percent.max']),
            numberAt(sbrPrefill, ['kv_cache_usage_max_percent.max', 'kv_cache_usage_percent.max']),
            maxSeriesValue(series, 'decode_kv_pct'),
            maxSeriesValue(series, 'prefill_kv_pct'),
            ...perPodKvPeaks,
            numberAt(summary, ['kv_cache_usage_percent.max', 'gpu_cache_usage_percent.max']),
        )
        : numberAt(summary, ['kv_cache_usage_percent.max', 'gpu_cache_usage_percent.max']);
    const kvMeanPct = isDisaggregated
        ? (isPdCanonical
            ? numberAt(summary, ['kv_cache_usage_percent.mean', 'gpu_cache_usage_percent.mean'])
            : firstFinite(
                meanSeriesValue(series, 'kv_pct'),
                maxFinite(
                    numberAt(sbrDecode, ['kv_cache_usage_percent.mean']),
                    numberAt(sbrPrefill, ['kv_cache_usage_percent.mean']),
                    numberAt(roleSummaryDecode, ['kv_cache_usage_percent']),
                    numberAt(roleSummaryPrefill, ['kv_cache_usage_percent']),
                ),
                numberAt(summary, ['kv_cache_usage_percent.mean', 'gpu_cache_usage_percent.mean']),
            ))
        : numberAt(summary, ['kv_cache_usage_percent.mean', 'gpu_cache_usage_percent.mean']);

    const firstEngine = cacheEngines[0] || EMPTY_OBJ;
    const hbmCapacityTokens = firstFinite(cacheConfig.hbm_capacity_tokens, firstEngine.kv_cache_size_tokens);
    const effectiveCapacityTokens = firstFinite(cacheConfig.effective_capacity_tokens, hbmCapacityTokens);
    const blockSize = firstFinite(cacheConfig.block_size, firstEngine.block_size);
    const numGpuBlocks = firstFinite(cacheConfig.num_gpu_blocks, firstEngine.num_gpu_blocks);
    const maxKvConcurrency = firstFinite(cacheConfig.kv_cache_max_concurrency, firstEngine.kv_cache_max_concurrency);

    const { prefillHbmCapTokens, decodeHbmCapTokens } = useMemo(() => {
        const podRoleLookup = new Map();
        for (const pod of perPod) {
            const name = String(pod?.pod || pod?.name || '').trim();
            if (name) podRoleLookup.set(name, String(effectivePodRole(pod) || '').trim().toLowerCase());
        }
        let derivedPrefill = 0;
        let derivedDecode = 0;
        for (const eng of cacheEngines) {
            const engPod = String(eng?.pod || eng?.instance || '').trim();
            const role = String(
                eng?.role
                || podRoleLookup.get(engPod)
                || (engPod.toLowerCase().includes('prefill') ? 'prefill' : engPod.toLowerCase().includes('decode') ? 'decode' : ''),
            ).trim().toLowerCase();
            const tok = firstFinite(eng?.kv_cache_size_tokens, eng?.hbm_capacity_tokens);
            if (tok !== null && tok > 0) {
                if (role === 'prefill') derivedPrefill += tok;
                else if (role === 'decode') derivedDecode += tok;
            }
        }
        return {
            prefillHbmCapTokens: positiveFinite(
                cacheConfig.prefill_hbm_capacity_tokens,
                derived?.prefill_hbm_capacity_tokens,
                derivedPrefill > 0 ? derivedPrefill : null,
            ),
            decodeHbmCapTokens: positiveFinite(
                cacheConfig.decode_hbm_capacity_tokens,
                derived?.decode_hbm_capacity_tokens,
                derivedDecode > 0 ? derivedDecode : null,
            ),
        };
    }, [cacheConfig, derived, cacheEngines, perPod]);

    const prefillRoleHitPctFromSource = rawPrefillSourceRow && rawPrefillSourceRow.total_tokens > 0
        ? ((rawPrefillSourceRow.local_cache_tokens + rawPrefillSourceRow.external_kv_tokens) / rawPrefillSourceRow.total_tokens) * 100
        : null;
    const prefixHitMean = isDisaggregated
        ? firstFinite(
            numberAt(sbrPrefill, ['prefix_cache_hit_percent.mean', 'prefix_cache_hit_percent']),
            meanSeriesValue(rawRoleSeriesList, 'prefill_prefix_cache_hit_percent'),
            numberAt(roleSummaryPrefill, ['prefix_cache_hit_percent']),
            prefillRoleHitPctFromSource,
            numberAt(summary, ['prefix_cache_hit_percent.mean']),
        )
        : numberAt(summary, ['prefix_cache_hit_percent.mean']);
    const prefixHitMax = isDisaggregated
        ? firstFinite(
            numberAt(sbrPrefill, ['prefix_cache_hit_percent.max', 'prefix_cache_hit_percent']),
            maxSeriesValue(rawRoleSeriesList, 'prefill_prefix_cache_hit_percent'),
            numberAt(summary, ['prefix_cache_hit_percent.max']),
        )
        : numberAt(summary, ['prefix_cache_hit_percent.max']);
    const extHitMean = isDisaggregated
        ? firstFinite(
            numberAt(sbrDecode, ['external_prefix_cache_hit_percent.mean', 'external_prefix_cache_hit_percent']),
            meanSeriesValue(rawRoleSeriesList, 'decode_external_prefix_cache_hit_percent'),
            numberAt(summary, ['external_prefix_cache_hit_percent.mean']),
        )
        : numberAt(summary, ['external_prefix_cache_hit_percent.mean']);
    const rawLocalCachedTpsMean = numberAt(summary, ['engine_prompt_local_cache_tps.mean']);
    const localCachedTpsMean = isDisaggregated
        ? firstFinite(
            numberAt(sbrPrefill, ['engine_prompt_local_cache_tps.mean', 'engine_prompt_local_cache_tps']),
            meanSeriesValue(rawRoleSeriesList, 'prefill_engine_prompt_local_cache_tps'),
            rawLocalCachedTpsMean !== null
                ? (isPdCanonical ? rawLocalCachedTpsMean : rawLocalCachedTpsMean * prefillLocalCacheShare)
                : null,
        )
        : rawLocalCachedTpsMean;
    const externalCachedTpsMean = isDisaggregated
        ? firstFinite(
            numberAt(sbrDecode, ['engine_prompt_external_tps.mean', 'engine_prompt_external_tps']),
            meanSeriesValue(rawRoleSeriesList, 'decode_engine_prompt_external_tps'),
            numberAt(summary, ['engine_prompt_external_tps.mean']),
        )
        : numberAt(summary, ['engine_prompt_external_tps.mean']);
    const freshComputedTpsMean = isDisaggregated
        ? firstFinite(
            numberAt(sbrPrefill, ['engine_prompt_local_compute_tps.mean', 'engine_prompt_local_compute_tps']),
            meanSeriesValue(rawRoleSeriesList, 'prefill_engine_prompt_local_compute_tps'),
            numberAt(summary, ['engine_prompt_local_compute_tps.mean']),
        )
        : numberAt(summary, ['engine_prompt_local_compute_tps.mean']);

    const stepSeconds = firstFinite(serverTelemetry?.step_seconds, 5) || 5;
    const durationSeconds = resolveDurationSeconds(telemetry?.client_series, series, stepSeconds);
    const clientSeries = telemetry?.client_series;
    const sessionsSummary = telemetry?.sessions_summary;

    const promptTokensByRoleData = useMemo(
        () => resolvePromptTokensByRoleSource({
            derived,
            serverTelemetry,
            series,
            perPod,
            summary,
            isDisaggregatedTopology,
            stepSeconds,
            durationSeconds,
        }),
        [derived, serverTelemetry, series, perPod, summary, isDisaggregatedTopology, stepSeconds, durationSeconds],
    );

    const tokenFlowData = useMemo(
        () => resolveTokenFlow({
            derived,
            clientSeries,
            series,
            summary,
            sessionsSummary,
            stepSeconds,
            durationSeconds,
            isDisaggregated,
            promptTokensByRoleData,
            sbrPrefill,
        }),
        [
            derived,
            clientSeries,
            series,
            summary,
            sessionsSummary,
            stepSeconds,
            durationSeconds,
            isDisaggregated,
            promptTokensByRoleData,
            sbrPrefill,
        ],
    );

    const prefixHitSeries = useMemo(
        () => buildPrefixHitSeries(series, {
            clientSeries,
            stepSeconds,
            ceilingPct: tokenFlowData?.theoretical_ceiling_pct ?? null,
        }),
        [series, clientSeries, stepSeconds, tokenFlowData?.theoretical_ceiling_pct],
    );

    const promptTokensPerRequestData = useMemo(
        () => resolvePromptTokensPerRequest({
            derived,
            clientSeries,
            tokenFlow: tokenFlowData,
            prefixHitMean,
            isDisaggregated,
            promptTokensByRoleData,
        }),
        [derived, clientSeries, tokenFlowData, prefixHitMean, isDisaggregated, promptTokensByRoleData],
    );

    // Offload & Eviction
    const vramCacheMean = kvMeanPct ?? meanSeriesValue(series, 'kv_pct');
    const vramCacheMax = kvPeakPct ?? maxSeriesValue(series, 'kv_pct') ?? vramCacheMean;
    const cpuCacheMean = numberAt(summary, ['cpu_cache_usage_percent.mean']) ?? meanSeriesValue(series, 'cpu_kv_pct');
    const cpuCacheMax = numberAt(summary, ['cpu_cache_usage_percent.max', 'cpu_cache_usage_percent.mean']) ?? maxSeriesValue(series, 'cpu_kv_pct') ?? cpuCacheMean;
    const nvmeCacheMean = numberAt(summary, ['disk_cache_usage_percent.mean']) ?? meanSeriesValue(series, 'disk_kv_pct');
    const nvmeCacheMax = numberAt(summary, ['disk_cache_usage_percent.max', 'disk_cache_usage_percent.mean']) ?? maxSeriesValue(series, 'disk_kv_pct') ?? nvmeCacheMean;
    const offloadBytes = firstFinite(derived.offloaded_bytes, numberAt(summary, ['kv_offload_bytes_total']));
    const restoreBytes = firstFinite(derived.restored_bytes, numberAt(summary, ['kv_restore_bytes_total']));
    const offloadRateBps = numberAt(summary, ['kv_offload_bytes_per_second.mean']);
    const offloadPeakRateBps = numberAt(summary, ['kv_offload_bytes_per_second.max']);
    const restoreRateBps = numberAt(summary, ['kv_restore_bytes_per_second.mean']);
    const restorePeakRateBps = numberAt(summary, ['kv_restore_bytes_per_second.max']);
    const lookupSyncDelayP95Ms = numberAt(summary, [
        'kv_offload_lookup_latency_p95_ms.mean',
        'kv_offload_lookup_latency_p95_ms.max',
        'kv_offload_lookup_latency_p95_ms',
    ]);
    const nixlBytesRate = numberAt(summary, ['nixl_transfer_bytes_per_second.mean']);
    const nixlP95Ms = numberAt(summary, ['nixl_transfer_latency_p95_ms.mean']);
    const nixlFailed = numberAt(summary, ['nixl_failed_transfer_rate_rps.mean', 'nixl_num_failed_transfers', 'nixl_failed_transfers_total.max']);
    const hasNixlSeries = series.some((pt) => pt.nixl_mbps !== null);
    const hasNixlMetrics = nixlBytesRate !== null || nixlP95Ms !== null || nixlFailed !== null || hasNixlSeries;
    const showNixlSection = isDisaggregatedTopology || hasNixlMetrics;
    const hasTierUsageSeries = series.some((pt) => (
        pt.kv_pct !== null
        || pt.decode_kv_pct !== null
        || pt.prefill_kv_pct !== null
        || pt.cpu_kv_pct !== null
        || pt.disk_kv_pct !== null
    ));
    const hasOffloadThroughputSeries = series.some((pt) => (
        pt.kv_offload_mbps !== null
        || pt.kv_restore_mbps !== null
        || (showNixlSection && pt.nixl_mbps !== null)
    ));
    const hasOffloadOrNixl = cpuCacheMax !== null
        || nvmeCacheMax !== null
        || offloadBytes !== null
        || restoreBytes !== null
        || offloadRateBps !== null
        || restoreRateBps !== null
        || lookupSyncDelayP95Ms !== null
        || hasNixlMetrics
        || hasOffloadThroughputSeries;

    const preemptMax = numberAt(summary, ['vllm_preemptions_total.max', 'num_preemptions_total.max', 'preemptions_total']);
    const preemptMin = numberAt(summary, ['vllm_preemptions_total.min']);
    const totalPreemptions = firstFinite(
        serverTelemetry?.total_preemptions,
        preemptMax !== null && preemptMin !== null ? Math.max(0, preemptMax - preemptMin) : preemptMax,
    );
    const preemptRateMax = numberAt(summary, ['vllm_preemptions_per_second.max', 'preemptions_per_second.max']);
    const hasPreemptSeries = series.some((pt) => pt.preemptions_rate !== null || pt.preemptions_total !== null);
    const evictBlocksPeak = numberAt(summary, ['kv_blocks_evicted_per_minute.max']);
    const evictBlocksMean = numberAt(summary, ['kv_blocks_evicted_per_minute.mean']);
    const evictBlocksTotal = firstFinite(derived.kv_blocks_evicted_total);
    const idleEvictP50 = numberAt(summary, ['kv_block_idle_before_evict_p50_seconds.mean', 'kv_block_idle_before_evict_seconds.p50']);
    const idleEvictP90 = numberAt(summary, ['kv_block_idle_before_evict_p90_seconds.mean', 'kv_block_idle_before_evict_seconds.p90']);
    const idleEvictP99 = numberAt(summary, ['kv_block_idle_before_evict_p99_seconds.mean', 'kv_block_idle_before_evict_seconds.p99']);
    const blockLifetimeP50 = numberAt(summary, ['kv_block_lifetime_p50_seconds.mean', 'kv_block_lifetime_seconds.p50']);
    const blockLifetimeP90 = numberAt(summary, ['kv_block_lifetime_p90_seconds.mean', 'kv_block_lifetime_seconds.p90']);
    const blockLifetimeP99 = numberAt(summary, ['kv_block_lifetime_p99_seconds.mean', 'kv_block_lifetime_seconds.p99']);
    const reuseGapP50 = numberAt(summary, ['kv_block_reuse_gap_p50_seconds.mean', 'kv_block_reuse_gap_seconds.p50']);
    const reuseGapP90 = numberAt(summary, ['kv_block_reuse_gap_p90_seconds.mean', 'kv_block_reuse_gap_seconds.p90']);
    const reuseGapP99 = numberAt(summary, ['kv_block_reuse_gap_p99_seconds.mean', 'kv_block_reuse_gap_seconds.p99']);
    const kvCacheMetricsEnabled = cacheConfig.kv_cache_metrics_enabled;
    const kvCacheMetricsSample = firstFinite(cacheConfig.kv_cache_metrics_sample);
    const hasKvEvictBlocksSeries = series.some((pt) => pt.kv_blocks_evicted_per_min !== null);
    const hasKvIdleEvictSeries = series.some((pt) => pt.kv_idle_evict_p50_s !== null || pt.kv_idle_evict_p90_s !== null);
    const hasKvLifetimeSeries = series.some((pt) => pt.kv_lifetime_p50_s !== null || pt.kv_lifetime_p90_s !== null);
    const hasKvReuseGapSeries = series.some((pt) => pt.kv_reuse_gap_p50_s !== null || pt.kv_reuse_gap_p90_s !== null);
    const hasKvResidencySeries = hasKvIdleEvictSeries || hasKvLifetimeSeries || hasKvReuseGapSeries;
    const kvResidencyCharts = [
        {
            key: 'idle_evict',
            title: 'KV Block Idle Time Before Eviction (s)',
            subtitle: 'vllm:kv_block_idle_before_evict_seconds P50 and P90 (s)',
            help: 'Duration a cached physical KV block sat unused in accelerator HBM before eviction (P50 & P90, seconds). Short idle times alongside longer reuse gaps signal premature cache churn.',
            helpFormula: 'HistogramQuantile(q, ΔKV_Block_Idle_Before_Evict_Buckets / Δt)  (q ∈ {0.50, 0.90})',
            helpAlign: 'end',
            hasSeries: hasKvIdleEvictSeries,
            p50Key: 'kv_idle_evict_p50_s',
            p90Key: 'kv_idle_evict_p90_s',
            p50Label: 'Idle Before Evict P50',
            p90Label: 'Idle Before Evict P90',
            emptyTitle: 'No idle-before-evict series',
            emptyMessage: 'vllm:kv_block_idle_before_evict_seconds histogram was not sampled over time.',
        },
        {
            key: 'block_lifetime',
            title: 'KV Block Lifetime (s)',
            subtitle: 'vllm:kv_block_lifetime_seconds P50 and P90 (s)',
            help: 'Total elapsed time a physical KV-cache block stays allocated in accelerator HBM from allocation until eviction (P50 & P90, seconds).',
            helpFormula: 'HistogramQuantile(q, ΔKV_Block_Lifetime_Buckets / Δt)  (q ∈ {0.50, 0.90})',
            helpAlign: 'start',
            hasSeries: hasKvLifetimeSeries,
            p50Key: 'kv_lifetime_p50_s',
            p90Key: 'kv_lifetime_p90_s',
            p50Label: 'Block Lifetime P50',
            p90Label: 'Block Lifetime P90',
            emptyTitle: 'No block lifetime series',
            emptyMessage: 'vllm:kv_block_lifetime_seconds histogram was not sampled over time.',
        },
        {
            key: 'reuse_gap',
            title: 'KV Block Reuse Gap (s)',
            subtitle: 'vllm:kv_block_reuse_gap_seconds P50 and P90 (s)',
            help: 'Elapsed time between consecutive prefix-cache hits on the same physical KV block (P50 & P90, seconds). Shorter reuse gaps indicate hot shared prefixes.',
            helpFormula: 'HistogramQuantile(q, ΔKV_Block_Reuse_Gap_Buckets / Δt)  (q ∈ {0.50, 0.90})',
            helpAlign: 'end',
            hasSeries: hasKvReuseGapSeries,
            p50Key: 'kv_reuse_gap_p50_s',
            p90Key: 'kv_reuse_gap_p90_s',
            p50Label: 'Reuse Gap P50',
            p90Label: 'Reuse Gap P90',
            emptyTitle: 'No block reuse gap series',
            emptyMessage: 'vllm:kv_block_reuse_gap_seconds histogram was not sampled over time.',
        },
    ];

    // Profile-driven accelerator stats
    const accelUtilMean = clampPercent(numberAt(summary, ['gpu_utilization_percent.mean', 'gpu_utilization.mean', 'accelerator_utilization.mean', 'utilization_percent.mean']));
    const accelUtilMax = clampPercent(numberAt(summary, ['gpu_utilization_percent.max', 'gpu_utilization.max', 'accelerator_utilization.max', 'utilization_percent.max']));
    const accelMemUsedBytes = numberAt(summary, ['gpu_framebuffer_used_bytes.mean', 'gpu_memory_usage_bytes.mean', 'accelerator_memory_used_bytes.mean']);
    const accelMemFreeBytes = numberAt(summary, ['gpu_framebuffer_free_bytes.mean', 'accelerator_memory_free_bytes.mean']);
    const accelMemTotalBytes = numberAt(summary, ['gpu_memory_total_bytes.mean', 'accelerator_memory_total_bytes.mean'])
        ?? (accelMemUsedBytes !== null && accelMemFreeBytes !== null ? accelMemUsedBytes + accelMemFreeBytes : null);
    const accelMemBwMean = clampPercent(numberAt(summary, ['gpu_memory_bandwidth_utilization_percent.mean', 'gpu_memory_bandwidth_percent.mean', 'memory_bandwidth_percent.mean']));
    const accelMemBwMax = clampPercent(numberAt(summary, ['gpu_memory_bandwidth_utilization_percent.max', 'gpu_memory_bandwidth_percent.max']));
    const accelPowerMean = numberAt(summary, ['gpu_power_watts.mean', 'power_watts.mean', 'accelerator_power_watts.mean'])
        ?? firstFinite(energy?.accelerator_avg_power_watts);
    const accelPowerMax = numberAt(summary, ['gpu_power_watts.max', 'power_watts.max', 'accelerator_power_watts.max'])
        ?? firstFinite(energy?.accelerator_peak_power_watts);
    const accelEnergyWh = firstFinite(energy?.accelerator_energy_wh);
    const accelTokPerWh = firstFinite(energy?.output_tokens_per_wh);
    const accelTempMean = numberAt(summary, ['gpu_temperature_celsius.mean', 'temperature_celsius.mean', 'accelerator_temperature_celsius.mean']);
    const accelTempMax = numberAt(summary, ['gpu_temperature_celsius.max', 'temperature_celsius.max', 'accelerator_temperature_celsius.max']);

    // Series availability guards & SLO resolution
    const { activeServerSloMs, sloDisplayText } = resolveServingLatencySlo(task, telemetry, serverLatencyMode);
    const maxObservedServerLat = series.reduce((maxVal, pt) => {
        const p50 = pt[`${serverLatencyMode}_p50`];
        const p90 = pt[`${serverLatencyMode}_p90`];
        let next = maxVal;
        if (Number.isFinite(p50) && p50 >= 0) next = Math.max(next, p50);
        if (Number.isFinite(p90) && p90 >= 0) next = Math.max(next, p90);
        return next;
    }, 0);
    const serverLatencyYMax = activeServerSloMs !== null
        ? Math.max(1, Math.ceil(Math.max(maxObservedServerLat, activeServerSloMs) * 1.12))
        : null;

    const serverLatLabel = serverLatencyMode === 'ttft' ? 'TTFT' : serverLatencyMode === 'tpot' ? 'TPOT' : 'E2E';
    const hasServerLatencySeries = series.some(
        (pt) => pt[`${serverLatencyMode}_p50`] !== null || pt[`${serverLatencyMode}_p90`] !== null,
    );
    const hasP50StageData = Boolean(
        stageView.slices?.p50?.available
        || stageView.p50Series.some((pt) => activeStageKeys.some((key) => isFiniteMetricValue(pt?.[key]))),
    );
    const effectiveStageQuantile = (stageQuantile === 'p90' || !hasP50StageData) && stageView.hasP90 ? 'p90' : 'p50';
    const activeStageSeries = effectiveStageQuantile === 'p90' ? stageView.p90Series : stageView.p50Series;
    const hasActiveStageSeries = activeStageSeries.some((pt) => activeStageKeys.some((key) => isFiniteMetricValue(pt?.[key])));
    const activeStageSliceKey = effectiveStageQuantile === 'p90' && stageView.slices?.p90?.available
        ? 'p90'
        : (stageView.slices?.p50?.available
            ? 'p50'
            : (stageView.slices?.mean?.available ? 'mean' : (stageView.slices?.p90?.available ? 'p90' : null)));
    const activeStageSlice = activeStageSliceKey ? stageView.slices[activeStageSliceKey] : null;
    const activeStageCaveat = activeStageSlice?.caveat ?? stageView.caveat ?? null;
    const activeReconciliation = activeStageSlice?.reconciliation ?? null;
    const showReconciliationNote = Boolean(
        activeReconciliation
        && isFiniteMetricValue(activeReconciliation.e2e_gap_pct)
        && Math.abs(Number(activeReconciliation.e2e_gap_pct)) >= 15,
    );
    const hasPromptSourceSeries = series.some(
        (pt) => pt.prompt_cached_tps !== null || pt.prompt_fresh_tps !== null,
    );
    const hasTokenSeries = series.some((pt) => pt.input_tps !== null || pt.output_tps !== null);
    const hasQueueSeries = series.some((pt) => pt.running !== null || pt.waiting !== null || pt.epp_running !== null);
    const hasTpotSeries = series.some((pt) => pt.tpot_p50 !== null || pt.tpot_p90 !== null);
    const hasEppLoadSeries = series.some((pt) => pt.epp_running !== null || pt.router_rps !== null || pt.engine_rps !== null);
    const hasEppLatencySeries = series.some(
        (pt) => pt.epp_sched_p50 !== null || pt.epp_sched_p90 !== null || pt.epp_plugin_p50 !== null || pt.epp_plugin_p90 !== null,
    );
    const hasKvSeries = series.some((pt) => pt.kv_pct !== null || pt.decode_kv_pct !== null || pt.prefill_kv_pct !== null);
    const showSplitVramTiers = isDisaggregated && hasDecodeKv && hasPrefillKv;
    const showEndpointRoleCol = isDisaggregated || endpointShareRows.some((r) => Boolean(r?.role));
    const hasKvTierSeries = series.some(
        (pt) => pt.hbm_share_pct !== null
            || pt.host_share_pct !== null
            || pt.nvme_share_pct !== null
            || pt.kv_pct !== null
            || pt.cpu_kv_pct !== null
            || pt.disk_kv_pct !== null,
    );
    const hbmMeanShare = meanSeriesValue(series, 'hbm_share_pct');
    const hostMeanShare = meanSeriesValue(series, 'host_share_pct');
    const nvmeMeanShare = meanSeriesValue(series, 'nvme_share_pct');
    const hbmMeanUsage = kvMeanPct ?? meanSeriesValue(series, 'kv_pct');
    const hostMeanUsage = cpuCacheMean ?? meanSeriesValue(series, 'cpu_kv_pct');
    const diskMeanUsage = numberAt(summary, ['disk_cache_usage_percent.mean']) ?? meanSeriesValue(series, 'disk_kv_pct');
    const hbmCapLabel = hbmCapBytes ? formatBytes(hbmCapBytes) : (hbmCapacityTokens ? `${formatCount(hbmCapacityTokens)} tok` : null);
    const cpuCapLabel = cpuCapBytes ? formatBytes(cpuCapBytes) : (cpuCapTokens ? `${formatCount(cpuCapTokens)} tok` : null);
    const nvmeCapLabel = nvmeCapBytes ? formatBytes(nvmeCapBytes) : null;
    const hasPrefixHitSeries = prefixHitSeries.some(
        (pt) => pt.prefix_hit_pct !== null
            || pt.ext_hit_pct !== null
            || pt.recompute_pct !== null
            || pt.cold_new_pct !== null,
    );
    const hasPowerSeries = series.some((pt) => pt.accel_power_w !== null);
    const hasUtilBwSeries = series.some((pt) => pt.accel_util !== null || pt.accel_mem_bw !== null);

    return (
        <div className="space-y-6">
            {/* Sticky Sub-Category Pill Bar */}
            {isServerAvailable && (
                <div
                    className="sticky top-0 z-10 flex flex-wrap items-center gap-1.5 rounded-xl border border-theme-border bg-theme-card/95 px-3 py-2 shadow-sm backdrop-blur-sm"
                    role="tablist"
                    aria-label="Serving sub-categories"
                >
                    {categories.map((cat) => {
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
            )}

            {!isServerAvailable ? (
                <Panel>
                    <EmptyState
                        icon={<Server className="h-7 w-7" />}
                        title="Server telemetry unavailable"
                        message={
                            serverTelemetry.reason
                            || 'Endpoint is external or Prometheus is not configured for this target. Client-side telemetry remains fully available across Overview and Requests.'
                        }
                    />
                </Panel>
            ) : (
                <>
                    {/* #s-flow: Topology & Traffic Replay */}
                    {showSection('flow') && (
                        <section id="s-flow" className="space-y-4">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="cyan" className="mb-0">Topology &amp; Traffic Replay</SectionLabel>
                                <HelpTooltip
                                    label="Topology and traffic replay help"
                                    align="start"
                                    iconSize={13}
                                    formula="Frame(t) = {Req/s(t), Output_tok/s(t), Running(t), KV_Usage%(t)}"
                                >
                                    Reconstructs the recorded serving topology (Endpoint Picker (EPP) to engine pods) and replays per-interval request, token, and KV-cache metrics from Prometheus.
                                </HelpTooltip>
                            </div>

                            {hasUsableFlowMap && (
                                <Panel
                                    title="Recorded Deployment Flow Map"
                                    help="Interactive topology graph showing recorded traffic flow across Inference Gateway / EPP and serving pods. Switch between step-by-step Replay and run-wide Summary, or toggle Requests vs Tokens flow units."
                                    helpFormula="Edge_Flow(t) = Rate_rps (or tok/s) per Router→Pod link; Node_Load(t) = Pod_Active_Requests(t)"
                                    helpAlign="start"
                                    actions={(
                                        <div className="flex flex-wrap items-center gap-2">
                                            {canReplay && (
                                                <ToggleGroup
                                                    size="xs"
                                                    value={activeFlowMode}
                                                    onChange={(nextMode) => {
                                                        setFlowMode(nextMode);
                                                        if (nextMode === 'summary') setIsPlaying(false);
                                                    }}
                                                    options={[
                                                        { value: 'replay', label: 'Replay' },
                                                        { value: 'summary', label: 'Summary' },
                                                    ]}
                                                />
                                            )}
                                            <ToggleGroup
                                                size="xs"
                                                value={flowMetric}
                                                onChange={setFlowMetric}
                                                options={[
                                                    { value: 'request', label: 'Requests' },
                                                    { value: 'token', label: 'Tokens' },
                                                ]}
                                            />
                                        </div>
                                    )}
                                >
                                    {canReplay && activeFlowMode === 'replay' && activeFrame && (
                                        <div className="mb-4 flex flex-col gap-3 rounded-xl border border-theme-border bg-theme-bg/70 p-3">
                                            <div className="flex flex-wrap items-center justify-between gap-3">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <Button
                                                        variant={isPlaying ? 'secondary' : 'primary'}
                                                        size="xs"
                                                        onClick={() => setIsPlaying((playing) => !playing)}
                                                        aria-label={isPlaying ? 'Pause traffic replay' : 'Play traffic replay'}
                                                    >
                                                        {isPlaying ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                                                        {isPlaying ? 'Pause' : 'Play'}
                                                    </Button>
                                                    <Button
                                                        variant="ghost"
                                                        size="xs"
                                                        onClick={() => {
                                                            setIsPlaying(false);
                                                            setSelectedFrameIndex(0);
                                                        }}
                                                        aria-label="Reset traffic replay"
                                                    >
                                                        <RotateCcw className="h-3.5 w-3.5" />
                                                        Reset
                                                    </Button>
                                                    <ToggleGroup
                                                        size="xs"
                                                        value={playbackSpeed}
                                                        onChange={setPlaybackSpeed}
                                                        options={[
                                                            { value: '1x', label: '1x' },
                                                            { value: '2x', label: '2x' },
                                                        ]}
                                                    />
                                                </div>
                                                <div className="flex flex-wrap items-center gap-3 font-mono text-[11px] text-theme-muted">
                                                    <span className="inline-flex items-center gap-1">
                                                        <span>
                                                            t = <strong className="text-theme-text">{formatNumber(activeFrame.t)}s</strong> / {formatNumber(totalReplayDuration)}s
                                                        </span>
                                                        <HelpTooltip
                                                            label="Replay timestamp help"
                                                            align="start"
                                                            iconSize={12}
                                                            formula="t = Elapsed_Timestamp_s ∈ [0, Run_Duration_s]"
                                                        >
                                                            Elapsed simulation timestamp of the active replay frame relative to the total recorded run duration (seconds).
                                                        </HelpTooltip>
                                                    </span>
                                                    <span className="inline-flex items-center gap-1">
                                                        <span>
                                                            Req/s: <strong className="text-theme-text">{formatNumber(activeFrame.rps, ' req/s')}</strong>
                                                        </span>
                                                        <HelpTooltip
                                                            label="Replay req/s help"
                                                            align="center"
                                                            iconSize={12}
                                                            formula="Req/s(t) = Σ_pods (ΔCompleted_Requests / Δt)"
                                                        >
                                                            Aggregate request rate (req/s) across the serving deployment at this replay timestamp; higher values reflect peak arrival load.
                                                        </HelpTooltip>
                                                    </span>
                                                    <span className="inline-flex items-center gap-1">
                                                        <span>
                                                            Output: <strong className="text-theme-text">{formatNumber(activeFrame.outputTps, ' tok/s')}</strong>
                                                        </span>
                                                        <HelpTooltip
                                                            label="Replay output throughput help"
                                                            align="center"
                                                            iconSize={12}
                                                            formula="Output(t) = Σ_pods (ΔGenerated_Tokens / Δt)"
                                                        >
                                                            Aggregate decode output token rate (tok/s) emitted across serving pods at this timestamp.
                                                        </HelpTooltip>
                                                    </span>
                                                    <span className="inline-flex items-center gap-1">
                                                        <span>
                                                            Running: <strong className="text-theme-text">{formatCount(activeFrame.runningRequests)}</strong>
                                                        </span>
                                                        <HelpTooltip
                                                            label="Replay running requests help"
                                                            align="end"
                                                            iconSize={12}
                                                            formula="Running(t) = Σ_pods Active_Running_Requests(t)"
                                                        >
                                                            Active requests executing concurrently inside the serving engines at this timestamp (req).
                                                        </HelpTooltip>
                                                    </span>
                                                    <span className="inline-flex items-center gap-1">
                                                        <span>
                                                            KV: <strong className="text-theme-text">{formatNumber(activeFrame.kvCachePct, '%')}</strong>
                                                        </span>
                                                        <HelpTooltip
                                                            label="Replay KV cache usage help"
                                                            align="end"
                                                            iconSize={12}
                                                            formula="KV_Usage(t) (%) = mean_pods((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100)"
                                                        >
                                                            Mean accelerator HBM KV-cache usage (%) at this timestamp; values approaching 90–100% risk request preemption.
                                                        </HelpTooltip>
                                                    </span>
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-3">
                                                <span className="font-mono text-[10px] text-theme-muted">0.0s</span>
                                                <input
                                                    type="range"
                                                    min={0}
                                                    max={Math.max(0, flowFrames.length - 1)}
                                                    step={1}
                                                    value={safeFrameIndex}
                                                    aria-label="Traffic replay timeline scrubber"
                                                    onChange={(event) => {
                                                        setIsPlaying(false);
                                                        setSelectedFrameIndex(Number(event.target.value));
                                                    }}
                                                    className="h-1.5 flex-1 cursor-pointer accent-sky-500"
                                                />
                                                <span className="font-mono text-[10px] text-theme-muted">{formatNumber(totalReplayDuration)}s</span>
                                            </div>
                                        </div>
                                    )}
                                    {!displayedFlowData.hasTraffic && (
                                        <p role="status" className="mb-3 text-xs text-theme-muted">
                                            {displayedFlowData.reason || 'No active traffic recorded at this timestamp. Scrub or play to inspect active intervals.'}
                                        </p>
                                    )}
                                    <FlowMap
                                        data={displayedFlowData}
                                        metric={flowMetric}
                                        historical={activeFlowMode === 'summary' || !isPlaying}
                                    />
                                </Panel>
                            )}
                        </section>
                    )}

                    {/* #s-latency: Server Latency & Stage Breakdown */}
                    {showSection('latency') && (
                        <section id="s-latency" className="space-y-4 scroll-mt-16">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="emerald" className="mb-0">Latency · Server &amp; Stage Breakdown</SectionLabel>
                                <HelpTooltip
                                    label="Server latency and stage breakdown help"
                                    align="start"
                                    iconSize={13}
                                    formula="Total_Stage = EPP_Scheduling + Queue_Time + Prefill_Time + Decode_Time"
                                >
                                    Engine-side latency percentiles and internal stage durations (queue wait, prefill, decode, and EPP scheduling) sampled from Prometheus histograms.
                                </HelpTooltip>
                            </div>
                            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                                <StatCard
                                    compact
                                    icon={<Clock3 className="h-4 w-4" />}
                                    title="Server TTFT (P50)"
                                    help="Mean of per-scrape median Time to First Token measured at the inference engine (ms). Lower values indicate faster prompt scheduling and prefill."
                                    helpFormula="Mean_scrapes(HistogramQuantile(0.50, ΔEngine_TTFT_Buckets / Δt))"
                                    value={!legacyPooledPd && serverTtftP50 !== null ? formatMilliseconds(serverTtftP50) : '—'}
                                    details={legacyPooledPd ? undefined : [
                                        {
                                            label: 'P90 / P99',
                                            value: `${formatMilliseconds(serverTtftP90)} / ${formatMilliseconds(serverTtftP99)}`,
                                            help: 'Mean 90th and 99th percentile server TTFT across scrape intervals (ms), highlighting tail queuing or prefill spikes.',
                                            formula: 'Mean_scrapes(HistogramQuantile(q, ΔEngine_TTFT_Buckets / Δt))  (q ∈ {0.90, 0.99})',
                                        },
                                    ]}
                                >
                                    {legacyPooledPd && (
                                        <p className="text-[11px] text-theme-muted">
                                            Legacy pooled P/D histograms (unseparated)
                                        </p>
                                    )}
                                </StatCard>
                                <StatCard
                                    compact
                                    icon={<Activity className="h-4 w-4" />}
                                    title="Server TPOT (P50)"
                                    help="Mean of per-scrape median Time per Output Token during decode at the engine (ms/tok; distinct from per-step ITL, with ITL fallback when TPOT histogram is absent)."
                                    helpFormula="Mean_scrapes(HistogramQuantile(0.50, ΔEngine_TPOT_Buckets / Δt))  [fallback: ΔEngine_ITL_Buckets]"
                                    value={formatMsPerTok(serverTpotP50)}
                                    details={[
                                        {
                                            label: 'P90 / P99',
                                            value: `${formatMsPerTok(serverTpotP90)} / ${formatMsPerTok(serverTpotP99)}`,
                                            help: 'Mean 90th and 99th percentile server TPOT across scrape intervals (ms/tok), reflecting decode batch contention.',
                                            formula: 'Mean_scrapes(HistogramQuantile(q, ΔEngine_TPOT_Buckets / Δt))  (q ∈ {0.90, 0.99})',
                                        },
                                    ]}
                                />
                                <StatCard
                                    compact
                                    icon={<Gauge className="h-4 w-4" />}
                                    title="Server E2E (P50)"
                                    help="Mean of per-scrape median end-to-end request latency inside the inference engine (ms), combining queue, prefill, and decode."
                                    helpFormula="Mean_scrapes(HistogramQuantile(0.50, ΔEngine_E2E_Buckets / Δt))"
                                    value={!legacyPooledPd && serverE2eP50 !== null ? formatMilliseconds(serverE2eP50) : '—'}
                                    details={legacyPooledPd ? undefined : [
                                        {
                                            label: 'P90 / P99',
                                            value: `${formatMilliseconds(serverE2eP90)} / ${formatMilliseconds(serverE2eP99)}`,
                                            help: 'Mean 90th and 99th percentile server E2E latency across scrape intervals (ms), indicating tail completion delay.',
                                            formula: 'Mean_scrapes(HistogramQuantile(q, ΔEngine_E2E_Buckets / Δt))  (q ∈ {0.90, 0.99})',
                                        },
                                    ]}
                                >
                                    {legacyPooledPd && (
                                        <p className="text-[11px] text-theme-muted">
                                            Legacy pooled P/D histograms (unseparated)
                                        </p>
                                    )}
                                </StatCard>
                                {legacyPooledPd ? (() => {
                                    const slbP50 = stageView.slices?.p50;
                                    const slbP90 = stageView.slices?.p90;
                                    const kvP50 = slbP50?.kv_transfer_ms ?? null;
                                    const pdTtftP50 = slbP50?.ttft_unattributed_ms ?? slbP50?.ttft_stage_ms ?? null;
                                    const decEstP50 = slbP50?.decode_time_ms ?? null;
                                    const kvP90 = slbP90?.kv_transfer_ms ?? null;
                                    const pdTtftP90 = slbP90?.ttft_unattributed_ms ?? slbP90?.ttft_stage_ms ?? null;
                                    const decEstP90 = slbP90?.decode_time_ms ?? null;
                                    return (
                                        <StatCard
                                            compact
                                            icon={<Layers className="h-4 w-4" />}
                                            title="Stage P50 (KV Transfer / P/D TTFT / Decode Est.)"
                                            help="Median P/D stage attribution derived from measured NIXL KV transfer and client TTFT/E2E split when legacy telemetry pooled P/D engine histograms."
                                            helpFormula="KV_Transfer = NIXL_P50;  PD_Engine_TTFT = max(0, Client_TTFT − (EPP + KV_Transfer));  Decode_Est = max(0, Client_E2E − Client_TTFT)"
                                            helpAlign="end"
                                            value={
                                                kvP50 !== null || pdTtftP50 !== null || decEstP50 !== null
                                                    ? `${formatMilliseconds(kvP50)} / ${formatMilliseconds(pdTtftP50)} / ${formatMilliseconds(decEstP50)}`
                                                    : '—'
                                            }
                                            details={[
                                                {
                                                    label: 'KV Transfer P90',
                                                    value: formatMilliseconds(kvP90),
                                                    help: '90th percentile NIXL KV transfer latency between Prefill and Decode workers (ms).',
                                                    formula: 'KV_Transfer_P90 = HistogramQuantile(0.90, ΔNIXL_Xfer_Buckets / Δt)',
                                                },
                                                {
                                                    label: 'P/D TTFT / Decode Est. P90',
                                                    value: `${formatMilliseconds(pdTtftP90)} / ${formatMilliseconds(decEstP90)}`,
                                                    help: '90th percentile P/D Engine TTFT and estimated Decode Time on legacy pooled P/D artifacts (ms).',
                                                    formula: 'PD_TTFT_P90 + Decode_Est_P90 = Client_E2E_P90',
                                                },
                                            ]}
                                        />
                                    );
                                })() : (() => {
                                    const isServerStageSource = stageView.source === 'server_telemetry';
                                    const slbP50 = isServerStageSource ? stageView.slices?.p50 : null;
                                    const slbP90 = isServerStageSource ? stageView.slices?.p90 : null;
                                    const hasKvKpi = isFiniteMetricValue(slbP50?.kv_transfer_ms) || isFiniteMetricValue(slbP90?.kv_transfer_ms);
                                    const qP50 = slbP50?.queue_time_ms ?? stageQueueP50;
                                    const preP50 = slbP50?.prefill_time_ms ?? stagePrefillP50;
                                    const decP50 = slbP50?.decode_time_ms ?? stageDecodeP50;
                                    const qP90 = slbP90?.queue_time_ms ?? stageQueueP90;
                                    const preP90 = slbP90?.prefill_time_ms ?? stagePrefillP90;
                                    const decP90 = slbP90?.decode_time_ms ?? stageDecodeP90;
                                    return (
                                        <StatCard
                                            compact
                                            icon={<Layers className="h-4 w-4" />}
                                            title="Stage P50 (Queue / Prefill / Decode)"
                                            help="Mean per-scrape median duration spent in inference engine queue wait, prefill execution, and decode execution (ms)."
                                            helpFormula="Mean_scrapes(HistogramQuantile(0.50, ΔEngine_Stage_Buckets / Δt))  for Stage ∈ {Queue, Prefill, Decode}"
                                            helpAlign="end"
                                            value={
                                                qP50 !== null || preP50 !== null || decP50 !== null
                                                    ? `${formatMilliseconds(qP50)} / ${formatMilliseconds(preP50)} / ${formatMilliseconds(decP50)}`
                                                    : '—'
                                            }
                                            details={[
                                                {
                                                    label: 'Queue P90',
                                                    value: formatMilliseconds(qP90),
                                                    help: 'Mean 90th percentile time requests wait in the engine scheduler queue before execution (ms); high values signal saturation.',
                                                    formula: 'Mean_scrapes(HistogramQuantile(0.90, ΔEngine_Queue_Time_Buckets / Δt))',
                                                },
                                                {
                                                    label: 'Prefill / Decode P90',
                                                    value: `${formatMilliseconds(preP90)} / ${formatMilliseconds(decP90)}`,
                                                    help: 'Mean 90th percentile time spent in prompt prefill and token decode execution stages (ms).',
                                                    formula: 'Mean_scrapes(HistogramQuantile(0.90, ΔEngine_Stage_Buckets / Δt))  for Stage ∈ {Prefill, Decode}',
                                                },
                                                ...(hasKvKpi ? [{
                                                    label: 'KV Transfer P50 / P90',
                                                    value: `${formatMilliseconds(slbP50?.kv_transfer_ms)} / ${formatMilliseconds(slbP90?.kv_transfer_ms)}`,
                                                    help: 'Disaggregated Prefill-to-Decode NIXL KV cache transfer duration (ms).',
                                                    formula: 'KV_Transfer = min(NIXL_Xfer_Latency, D_Queue_Raw)',
                                                }] : []),
                                            ]}
                                        />
                                    );
                                })()}
                            </div>

                            <div className="grid gap-6 xl:grid-cols-2">
                                <ChartContainer
                                    title="Server Latency over Time"
                                    subtitle={`vLLM ${serverLatLabel} P50 with P50–P90 envelope`}
                                    help="Server-side P50 line with shaded P50–P90 tail band for the selected latency metric (ms, or ms/tok for TPOT) over elapsed time. Widening bands indicate growing tail variance under load."
                                    helpFormula="Line = P50(t); Band = [P50(t), P90(t)] via HistogramQuantile(q, ΔEngine_Metric_Buckets / Δt)"
                                    helpAlign="start"
                                    actions={(
                                        <ToggleGroup
                                            size="xs"
                                            value={serverLatencyMode}
                                            onChange={setServerLatencyMode}
                                            options={[
                                                { value: 'ttft', label: 'TTFT' },
                                                { value: 'tpot', label: 'TPOT' },
                                                { value: 'e2e', label: 'E2E' },
                                            ]}
                                        />
                                    )}
                                >
                                    {legacyPooledPd && (
                                        <div className="mb-3 flex items-start gap-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-900 dark:text-sky-200">
                                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                                            <span>
                                                Legacy P/D server telemetry pooled Prefill and Decode histograms across roles, distorting server TTFT and E2E curves. TPOT remains Decode-specific; refresh telemetry or re-run for role-separated curves.
                                            </span>
                                        </div>
                                    )}
                                    {hasServerLatencySeries ? (
                                        <>
                                            <div className="h-56 w-full">
                                                <ResponsiveContainer width="100%" height="100%">
                                                    <ComposedChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                        <YAxis width={56} allowDecimals domain={serverLatencyYMax !== null ? [0, serverLatencyYMax] : undefined} tickFormatter={formatMsAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: serverLatencyMode === 'tpot' ? 'ms/tok' : 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                        <Tooltip content={serverLatencyMode === 'tpot' ? <MsPerTokTooltip /> : <MsTooltip />} />
                                                        {activeServerSloMs !== null && (
                                                            <ReferenceLine
                                                                y={activeServerSloMs}
                                                                stroke={CHART_SERIES[2]}
                                                                strokeDasharray="6 4"
                                                                strokeWidth={1.5}
                                                                ifOverflow="extendDomain"
                                                                label={{
                                                                    value: `SLO: ${sloDisplayText}`,
                                                                    position: 'insideTopRight',
                                                                    fill: CHART_SERIES[2],
                                                                    fontSize: 10,
                                                                    fontWeight: 600,
                                                                }}
                                                            />
                                                        )}
                                                        <Area type="monotone" dataKey={`${serverLatencyMode}_p50`} stackId="band" stroke="none" fill="transparent" connectNulls tooltipType="none" isAnimationActive={false} legendType="none" />
                                                        <Area type="monotone" dataKey={`${serverLatencyMode}_band`} stackId="band" name={`${serverLatLabel} P50–P90`} stroke="none" fill={CHART_SERIES[1]} fillOpacity={0.18} connectNulls tooltipType="none" isAnimationActive={false} />
                                                        <Line type="monotone" dataKey={`${serverLatencyMode}_p50`} name={`${serverLatLabel} P50`} stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, `${serverLatencyMode}_p50`, CHART_SERIES[0])} isAnimationActive={false} />
                                                        <Line type="monotone" dataKey={`${serverLatencyMode}_p90`} name={`${serverLatLabel} P90`} stroke={CHART_SERIES[1]} strokeWidth={1.5} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, `${serverLatencyMode}_p90`, CHART_SERIES[1])} isAnimationActive={false} />
                                                    </ComposedChart>
                                                </ResponsiveContainer>
                                            </div>
                                            <ChartLegend
                                                className="mt-3"
                                                entries={[
                                                    { label: `${serverLatLabel} P50`, color: CHART_SERIES[0] },
                                                    { label: `${serverLatLabel} P90`, color: CHART_SERIES[1] },
                                                    ...(activeServerSloMs !== null
                                                        ? [{ label: `${serverLatLabel} SLO (${sloDisplayText})`, color: CHART_SERIES[2] }]
                                                        : []),
                                                ]}
                                            />
                                        </>
                                    ) : (
                                        <EmptyState title={`No server ${serverLatLabel} series`} message={`Per-scrape vLLM ${serverLatLabel} histogram percentiles were not exposed.`} />
                                    )}
                                </ChartContainer>

                                <ChartContainer
                                    title="Request Stage Latency Breakdown"
                                    subtitle={
                                        stageView.source === 'client_fallback'
                                            ? 'Client TTFT (Unattributed) + Estimated Decode Time (server stage histograms unavailable)'
                                            : stageView.source === 'pd_pooled_fallback'
                                                ? 'EPP Scheduling + KV Transfer (NIXL) + P/D Engine TTFT + Estimated Decode Time'
                                                : `Stacked ${effectiveStageQuantile === 'p90' ? 'P90' : 'P50'} stage latencies (${stageLayers.map((layer) => layer.label).join(' + ')})`
                                    }
                                    help="Stacked P50 or P90 per-stage request durations over elapsed time (ms), ordered bottom-to-top across routing, queue wait, optional KV transfer, prefill, and decode stages."
                                    helpFormula="Total_Stage = EPP_Scheduling + Queue_Time + KV_Transfer + Prefill_Time + Server_TTFT_Residual + Client_Gateway_Network + Decode_Time"
                                    helpAlign="end"
                                    actions={stageView.hasP90 ? (
                                        <ToggleGroup
                                            size="xs"
                                            value={effectiveStageQuantile}
                                            onChange={setStageQuantile}
                                            options={[
                                                { value: 'p50', label: 'P50', disabled: !hasP50StageData },
                                                { value: 'p90', label: 'P90' },
                                            ]}
                                        />
                                    ) : null}
                                >
                                    {activeStageCaveat && (
                                        <div className="mb-3 flex items-start gap-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-900 dark:text-sky-200">
                                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                                            <span>{activeStageCaveat}</span>
                                        </div>
                                    )}
                                    {showReconciliationNote && (
                                        <p className="mb-3 rounded-lg border border-theme-border bg-theme-card px-3 py-2 text-[11px] text-theme-muted">
                                            E2E reconciliation: stage sum ({formatMilliseconds(activeReconciliation.total_stage_ms)}) vs Client E2E ({formatMilliseconds(activeReconciliation.client_e2e_ms)}), gap {activeReconciliation.e2e_gap_ms >= 0 ? '+' : ''}{formatMilliseconds(activeReconciliation.e2e_gap_ms)} ({activeReconciliation.e2e_gap_pct >= 0 ? '+' : ''}{formatNumber(activeReconciliation.e2e_gap_pct, '%')})
                                            {activeReconciliation.quantile_non_additive ? ' — marginal quantiles (P50/P90) are non-additive across stages.' : '.'}
                                        </p>
                                    )}
                                    {hasActiveStageSeries ? (
                                        <>
                                            <div className="h-56 w-full">
                                                <ResponsiveContainer width="100%" height="100%">
                                                    <AreaChart data={activeStageSeries} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                        <YAxis width={56} allowDecimals tickFormatter={formatMsAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
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
                                        <EmptyState title="Stage breakdown series unavailable" message="Per-stage engine queue, prefill, and decode time histograms were not sampled over time." />
                                    )}
                                </ChartContainer>
                            </div>

                            {hasPromptSourceSeries && (
                                <ChartContainer
                                    title="Prompt Token Throughput: Prefix Cache vs Prefill Compute"
                                    subtitle="Stacked engine prompt processing split between prefix cache hits and prefill compute"
                                    help="Stacked prompt token throughput served from prefix cache hits versus prefill compute by the engine (tok/s); the combined stack equals total prompt processing throughput."
                                    helpFormula="Total_Prompt_Rate(t) = Prefix_Cache_Hit_Rate(t) + Prefill_Compute_Rate(t)  (tok/s)"
                                    helpAlign="start"
                                >
                                    <div className="h-52 w-full">
                                        <ResponsiveContainer width="100%" height="100%">
                                            <AreaChart data={promptCacheSplitSeries} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                <YAxis width={60} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'tok/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                <Tooltip content={<StackedTokTooltip />} />
                                                <Area type="monotone" stackId="promptSource" dataKey="prompt_cached_tps" name="Prefix Cache (tok/s)" fill={CHART_SERIES[1]} fillOpacity={0.32} stroke={CHART_SERIES[1]} strokeWidth={1.75} connectNulls dot={sparseDotProp(promptCacheSplitSeries, 'prompt_cached_tps', CHART_SERIES[1])} isAnimationActive={false} />
                                                <Area type="monotone" stackId="promptSource" dataKey="prompt_fresh_tps" name="Prefill Compute (tok/s)" fill={CHART_SERIES[2]} fillOpacity={0.32} stroke={CHART_SERIES[2]} strokeWidth={1.75} connectNulls dot={sparseDotProp(promptCacheSplitSeries, 'prompt_fresh_tps', CHART_SERIES[2])} isAnimationActive={false} />
                                            </AreaChart>
                                        </ResponsiveContainer>
                                    </div>
                                    <ChartLegend
                                        className="mt-3"
                                        entries={[
                                            { label: 'Prefix Cache (tok/s)', color: CHART_SERIES[1] },
                                            { label: 'Prefill Compute (tok/s)', color: CHART_SERIES[2] },
                                        ]}
                                    />
                                </ChartContainer>
                            )}
                        </section>
                    )}

                    {/* #s-engine: Engine · vLLM */}
                    {showSection('engine') && (
                        <section id="s-engine" className="space-y-4">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="sky" className="mb-0">Engine · vLLM</SectionLabel>
                                <HelpTooltip
                                    label="Engine vLLM help"
                                    align="start"
                                    iconSize={13}
                                    formula="Engine_Agg(t) = {Running(t), Waiting(t), Prefill_tok/s(t), Decode_tok/s(t), TPOT(t)}"
                                >
                                    Serving engine concurrency, prefill and decode token throughput, time per output token (TPOT), and per-pod execution summaries from Prometheus.
                                </HelpTooltip>
                            </div>
                            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                                <StatCard
                                    compact
                                    icon={<Activity className="h-4 w-4" />}
                                    title="Running Requests"
                                    help="Mean active requests concurrently executing in the inference engine continuous-batching scheduler per scrape (req)."
                                    helpFormula="Mean_scrapes(Σ_pods Active_Running_Requests(t))"
                                    value={runningMean !== null ? formatNumber(runningMean) : '—'}
                                    details={[{
                                        label: 'Peak',
                                        value: formatNumber(runningMax),
                                        help: 'Maximum concurrent running requests observed in any scrape interval during the run.',
                                        formula: 'max_scrapes(Σ_pods Active_Running_Requests(t))',
                                    }]}
                                />
                                <StatCard
                                    compact
                                    icon={<Layers className="h-4 w-4" />}
                                    title="Waiting Requests"
                                    help="Mean requests queued in the inference engine waiting for batch slots or KV-cache capacity (req). Sustained values above zero indicate saturation."
                                    helpFormula="Mean_scrapes(Σ_pods Queued_Waiting_Requests(t))"
                                    value={waitingMean !== null ? formatNumber(waitingMean) : '—'}
                                    details={[{
                                        label: 'Peak',
                                        value: formatNumber(waitingMax),
                                        help: 'Maximum waiting request count observed in any scrape interval; spikes precede TTFT degradation.',
                                        formula: 'max_scrapes(Σ_pods Queued_Waiting_Requests(t))',
                                    }]}
                                />
                                <StatCard
                                    compact
                                    icon={<Gauge className="h-4 w-4" />}
                                    title="Prefill Throughput"
                                    help="Mean input (prompt) token prefill processing rate across all serving engine pods (tok/s), reflecting prefill capacity and prefix-cache demand."
                                    helpFormula="Mean_scrapes(Σ_pods (ΔPrompt_Tokens / Δt))"
                                    value={promptTpsMean !== null ? formatNumber(promptTpsMean, ' tok/s') : '—'}
                                    details={[{
                                        label: 'Peak',
                                        value: promptTpsMax !== null ? formatNumber(promptTpsMax, ' tok/s') : '—',
                                        help: 'Maximum input (prompt) token prefill processing rate observed in any scrape interval (tok/s).',
                                        formula: 'max_scrapes(Σ_pods (ΔPrompt_Tokens / Δt))',
                                    }]}
                                />
                                <StatCard
                                    compact
                                    icon={<Zap className="h-4 w-4" />}
                                    title="Decode Throughput"
                                    help="Mean output token decode generation rate across all serving engine pods (tok/s). Higher values indicate greater decode throughput."
                                    helpFormula="Mean_scrapes(Σ_pods (ΔGenerated_Tokens / Δt))"
                                    helpAlign="end"
                                    value={genTpsMean !== null ? formatNumber(genTpsMean, ' tok/s') : '—'}
                                    details={[{
                                        label: 'Peak',
                                        value: genTpsMax !== null ? formatNumber(genTpsMax, ' tok/s') : '—',
                                        help: 'Maximum output token decode generation rate observed in any scrape interval (tok/s).',
                                        formula: 'max_scrapes(Σ_pods (ΔGenerated_Tokens / Δt))',
                                    }]}
                                />
                            </div>

                            <div className="grid gap-6 xl:grid-cols-2">
                                <ChartContainer
                                    title="Running vs Waiting Requests over Time"
                                    subtitle={hasDisaggregatedRoles ? 'Prefill & Decode running/waiting requests' : 'vLLM running/waiting requests and EPP in-flight requests'}
                                    help="Engine running and waiting request counts over elapsed time (split by Prefill/Decode pools in P/D Disaggregation mode). Rising waiting requests signal scheduler backlog."
                                    helpFormula="Running(t) = Σ_pods Active_Running(t); Waiting(t) = Σ_pods Queued_Waiting(t)"
                                    helpAlign="start"
                                >
                                    {hasQueueSeries ? (
                                        <>
                                            <div className="h-56 w-full">
                                                <ResponsiveContainer width="100%" height="100%">
                                                    <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                        <YAxis width={52} allowDecimals={false} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'requests', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                        <Tooltip content={<ReqTooltip />} />
                                                        {hasDisaggregatedRoles ? (
                                                            <>
                                                                <Line type="monotone" dataKey="prefill_running" name="Prefill Running" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'prefill_running', CHART_SERIES[0])} isAnimationActive={false} />
                                                                <Line type="monotone" dataKey="decode_running" name="Decode Running" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'decode_running', CHART_SERIES[1])} isAnimationActive={false} />
                                                                <Line type="monotone" dataKey="prefill_waiting" name="Prefill Waiting" stroke={CHART_SERIES[2]} strokeWidth={1.75} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'prefill_waiting', CHART_SERIES[2])} isAnimationActive={false} />
                                                                <Line type="monotone" dataKey="decode_waiting" name="Decode Waiting" stroke={CHART_SERIES[4]} strokeWidth={1.75} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'decode_waiting', CHART_SERIES[4])} isAnimationActive={false} />
                                                            </>
                                                        ) : (
                                                            <>
                                                                <Line type="monotone" dataKey="running" name="vLLM Running Requests" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'running', CHART_SERIES[0])} isAnimationActive={false} />
                                                                <Line type="monotone" dataKey="waiting" name="vLLM Waiting Requests" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'waiting', CHART_SERIES[2])} isAnimationActive={false} />
                                                                {series.some((pt) => pt.epp_running !== null) && (
                                                                    <Line type="monotone" dataKey="epp_running" name="EPP In-Flight" stroke={CHART_SERIES[1]} strokeWidth={1.75} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'epp_running', CHART_SERIES[1])} isAnimationActive={false} />
                                                                )}
                                                            </>
                                                        )}
                                                    </LineChart>
                                                </ResponsiveContainer>
                                            </div>
                                            <ChartLegend
                                                className="mt-3"
                                                entries={hasDisaggregatedRoles ? [
                                                    { label: 'Prefill Running', color: CHART_SERIES[0] },
                                                    { label: 'Decode Running', color: CHART_SERIES[1] },
                                                    { label: 'Prefill Waiting', color: CHART_SERIES[2] },
                                                    { label: 'Decode Waiting', color: CHART_SERIES[4] },
                                                ] : [
                                                    { label: 'vLLM Running Requests', color: CHART_SERIES[0] },
                                                    { label: 'vLLM Waiting Requests', color: CHART_SERIES[2] },
                                                    ...(series.some((pt) => pt.epp_running !== null) ? [{ label: 'EPP In-Flight', color: CHART_SERIES[1] }] : []),
                                                ]}
                                            />
                                        </>
                                    ) : (
                                        <EmptyState title="No queue series" message="vLLM running and waiting request gauges were not available." />
                                    )}
                                </ChartContainer>

                                <ChartContainer
                                    title="Prefill vs Decode Token Throughput"
                                    subtitle="vLLM prefill (input) vs decode (output) token rates (tok/s)"
                                    help="Server-side prefill (input) and decode (output) token rates over elapsed time (tok/s), comparing prefill bursts against steady decode output."
                                    helpFormula="Prefill_tok/s(t) = Σ_pods (ΔPrompt_Tokens / Δt); Decode_tok/s(t) = Σ_pods (ΔGenerated_Tokens / Δt)"
                                    helpAlign="end"
                                >
                                    {hasTokenSeries ? (
                                        <>
                                            <div className="h-56 w-full">
                                                <ResponsiveContainer width="100%" height="100%">
                                                    <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                        <YAxis width={60} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'tok/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                        <Tooltip content={<TokTooltip />} />
                                                        <Line type="monotone" dataKey="input_tps" name="Prefill tok/s" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'input_tps', CHART_SERIES[1])} isAnimationActive={false} />
                                                        <Line type="monotone" dataKey="output_tps" name="Decode tok/s" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'output_tps', CHART_SERIES[0])} isAnimationActive={false} />
                                                    </LineChart>
                                                </ResponsiveContainer>
                                            </div>
                                            <ChartLegend
                                                className="mt-3"
                                                entries={[
                                                    { label: 'Prefill tok/s', color: CHART_SERIES[1] },
                                                    { label: 'Decode tok/s', color: CHART_SERIES[0] },
                                                ]}
                                            />
                                        </>
                                    ) : (
                                        <EmptyState title="No server token rate series" message="vLLM prefill and decode token counters were not available." />
                                    )}
                                </ChartContainer>
                            </div>

                            {hasTpotSeries && (
                                <ChartContainer
                                    title="vLLM TPOT over Time"
                                    subtitle="Server TPOT P50 and P90 (ms/tok); falls back to vLLM ITL when TPOT histogram is absent"
                                    help="Server-side Time per Output Token (TPOT, ms/tok) P50 and P90 over elapsed time; falls back to engine Inter-Token Latency (ITL) when the TPOT histogram is absent. Rising TPOT under load indicates decode batch contention or memory bandwidth saturation."
                                    helpFormula="HistogramQuantile(q, ΔEngine_TPOT_Buckets / Δt)  (q ∈ {0.50, 0.90}; fallback: ΔEngine_ITL_Buckets)"
                                    helpAlign="start"
                                >
                                    <div className="h-52 w-full">
                                        <ResponsiveContainer width="100%" height="100%">
                                            <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                <YAxis width={56} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'ms/tok', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                <Tooltip content={<MsPerTokTooltip />} />
                                                <Line type="monotone" dataKey="tpot_p50" name="TPOT P50 (ms/tok)" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'tpot_p50', CHART_SERIES[0])} isAnimationActive={false} />
                                                <Line type="monotone" dataKey="tpot_p90" name="TPOT P90 (ms/tok)" stroke={CHART_SERIES[1]} strokeWidth={1.5} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'tpot_p90', CHART_SERIES[1])} isAnimationActive={false} />
                                            </LineChart>
                                        </ResponsiveContainer>
                                    </div>
                                    <ChartLegend
                                        className="mt-3"
                                        entries={[
                                            { label: 'TPOT P50 (ms/tok)', color: CHART_SERIES[0] },
                                            { label: 'TPOT P90 (ms/tok)', color: CHART_SERIES[1] },
                                        ]}
                                    />
                                </ChartContainer>
                            )}

                            <div className="grid gap-6 xl:grid-cols-2">
                                <ChartContainer
                                    title="Prompt Token Breakdown"
                                    subtitle="Local Prefix Cache · Recomputed Prefix · Uncached Prompt (vs theoretical cache ceiling)"
                                    help="Total sent prompt tokens split into local prefix cache hits, shared-prefix tokens recomputed despite prior turns, and uncached prompt tokens; the vertical marker indicates the theoretical hit ceiling with an infinite cache and ideal routing."
                                    helpFormula="Local_Cache + Recomputed_Prefix + Uncached_Prompt = 100%; Recomputed = max(0, Theoretical_Reusable − Cached)"
                                    helpAlign="start"
                                >
                                    {tokenFlowData ? (
                                        <div className="space-y-3">
                                            <ProportionalStackedBar
                                                ariaLabel="Prompt Token Breakdown"
                                                markerPct={tokenFlowData.theoretical_ceiling_pct}
                                                markerLabel={
                                                    tokenFlowData.theoretical_ceiling_pct !== null
                                                        ? `Ceiling ${formatPct(tokenFlowData.theoretical_ceiling_pct)}%`
                                                        : null
                                                }
                                                segments={[
                                                    {
                                                        key: 'cached',
                                                        label: 'Local Prefix Cache',
                                                        pct: tokenFlowData.cached_pct,
                                                        color: CHART_SERIES[1],
                                                        displayValue: `${formatCount(tokenFlowData.cached_tokens)} tok`,
                                                    },
                                                    {
                                                        key: 'recomputed',
                                                        label: 'Recomputed Prefix',
                                                        pct: tokenFlowData.recomputed_pct,
                                                        color: CHART_SERIES[2],
                                                        displayValue: `${formatCount(tokenFlowData.recomputed_tokens)} tok`,
                                                    },
                                                    {
                                                        key: 'cold_new',
                                                        label: 'Uncached Prompt',
                                                        pct: tokenFlowData.cold_new_pct,
                                                        color: CHART_SERIES[0],
                                                        displayValue: `${formatCount(tokenFlowData.cold_new_tokens)} tok`,
                                                    },
                                                ]}
                                            />
                                            <ChartLegend
                                                entries={[
                                                    { label: 'Local Prefix Cache', color: CHART_SERIES[1] },
                                                    { label: 'Recomputed Prefix', color: CHART_SERIES[2] },
                                                    { label: 'Uncached Prompt', color: CHART_SERIES[0] },
                                                ]}
                                            />
                                            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-theme-border pt-2.5 font-mono text-[11px] text-theme-muted">
                                                <span>
                                                    Total: <strong className="text-theme-text">{formatCount(tokenFlowData.sent_tokens)} tok</strong>
                                                </span>
                                                <span aria-hidden="true">·</span>
                                                <span>
                                                    Local Prefix Cache: <strong className="text-theme-text">{formatCount(tokenFlowData.cached_tokens)}</strong> ({formatPct(tokenFlowData.cached_pct)}%)
                                                </span>
                                                <span aria-hidden="true">·</span>
                                                <span>
                                                    Recomputed Prefix: <strong className="text-theme-text">{formatCount(tokenFlowData.recomputed_tokens)}</strong> ({formatPct(tokenFlowData.recomputed_pct)}%)
                                                </span>
                                                <span aria-hidden="true">·</span>
                                                <span>
                                                    Uncached Prompt: <strong className="text-theme-text">{formatCount(tokenFlowData.cold_new_tokens)}</strong> ({formatPct(tokenFlowData.cold_new_pct)}%)
                                                </span>
                                                {tokenFlowData.theoretical_ceiling_pct !== null && (
                                                    <>
                                                        <span aria-hidden="true">·</span>
                                                        <span>
                                                            Ceiling: <strong className="text-theme-text">{formatPct(tokenFlowData.theoretical_ceiling_pct)}%</strong>
                                                        </span>
                                                    </>
                                                )}
                                            </div>
                                        </div>
                                    ) : (
                                        <EmptyState
                                            title="Prompt token breakdown unavailable"
                                            message="Sent prompt token counts and prefix-cache hit metrics were not available for this run."
                                        />
                                    )}
                                </ChartContainer>

                                <ChartContainer
                                    title="Prompt Tokens per Request"
                                    subtitle="Mean prefix-cached vs prefill-computed prompt tokens per request"
                                    help="Average prompt tokens per request split between prefix-cache hits and prefill-computed tokens, alongside P50 and P90 request input sequence lengths."
                                    helpFormula="Mean_Prompt = Mean_Cached + Mean_Computed; Cached(%) = (Mean_Cached / Mean_Prompt) × 100"
                                    helpAlign="end"
                                >
                                    {promptTokensPerRequestData ? (
                                        <div className="space-y-3">
                                            <ProportionalStackedBar
                                                ariaLabel="Prompt Tokens per Request"
                                                segments={[
                                                    {
                                                        key: 'cached',
                                                        label: 'Prefix Cache',
                                                        pct: (promptTokensPerRequestData.mean_cached_tokens / promptTokensPerRequestData.mean_prompt_tokens) * 100,
                                                        color: CHART_SERIES[1],
                                                        displayValue: `${formatTokMean(promptTokensPerRequestData.mean_cached_tokens)} tok`,
                                                    },
                                                    {
                                                        key: 'computed',
                                                        label: 'Prefill Compute',
                                                        pct: (promptTokensPerRequestData.mean_computed_tokens / promptTokensPerRequestData.mean_prompt_tokens) * 100,
                                                        color: CHART_SERIES[2],
                                                        displayValue: `${formatTokMean(promptTokensPerRequestData.mean_computed_tokens)} tok`,
                                                    },
                                                ]}
                                            />
                                            <ChartLegend
                                                entries={[
                                                    { label: 'Prefix Cache', color: CHART_SERIES[1] },
                                                    { label: 'Prefill Compute', color: CHART_SERIES[2] },
                                                ]}
                                            />
                                            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-theme-border pt-2.5 font-mono text-[11px] text-theme-muted">
                                                <span>
                                                    Mean: <strong className="text-theme-text">{formatTokMean(promptTokensPerRequestData.mean_prompt_tokens)} tok</strong>
                                                </span>
                                                <span aria-hidden="true">·</span>
                                                <span>
                                                    Prefix Cache: <strong className="text-theme-text">{formatTokMean(promptTokensPerRequestData.mean_cached_tokens)} tok</strong> ({formatPct(promptTokensPerRequestData.cached_pct)}%)
                                                </span>
                                                <span aria-hidden="true">·</span>
                                                <span>
                                                    Prefill Compute: <strong className="text-theme-text">{formatTokMean(promptTokensPerRequestData.mean_computed_tokens)} tok</strong>
                                                </span>
                                                <span aria-hidden="true">·</span>
                                                <span>
                                                    P50 / P90 Prompt: <strong className="text-theme-text">{formatTokMean(promptTokensPerRequestData.prompt_p50)} / {formatTokMean(promptTokensPerRequestData.prompt_p90)} tok</strong>
                                                </span>
                                                {(promptTokensPerRequestData.computed_p50 !== null || promptTokensPerRequestData.computed_p90 !== null) && (
                                                    <>
                                                        <span aria-hidden="true">·</span>
                                                        <span>
                                                            P50 / P90 Prefill Compute: <strong className="text-theme-text">{formatTokMean(promptTokensPerRequestData.computed_p50)} / {formatTokMean(promptTokensPerRequestData.computed_p90)} tok</strong>
                                                        </span>
                                                    </>
                                                )}
                                            </div>
                                        </div>
                                    ) : (
                                        <EmptyState
                                            title="Per-request prompt tokens unavailable"
                                            message="Request input sequence length distributions were not recorded for this run."
                                        />
                                    )}
                                </ChartContainer>
                            </div>

                            <ChartContainer
                                title="Prompt Token Sources by Role"
                                subtitle="Local Prefix Cache · External KV Transfer · Prefill Compute"
                                help="Total prompt tokens processed per serving role broken down by inference engine source: local HBM prefix cache, external/CPU KV transfer, and prefill compute."
                                helpFormula="Role_Prompt_Tokens = Local_Prefix_Cache + External_KV_Transfer + Prefill_Compute  (100% per role)"
                                helpAlign="start"
                            >
                                {promptTokensByRoleData.length > 0 ? (
                                    <div className="space-y-3">
                                        <div className="space-y-2.5">
                                            {promptTokensByRoleData.map((row) => {
                                                const localPct = (row.local_cache_tokens / row.total_tokens) * 100;
                                                const extPct = (row.external_kv_tokens / row.total_tokens) * 100;
                                                const freshPct = Math.max(0, 100 - localPct - extPct);
                                                return (
                                                    <div key={row.role} className="grid grid-cols-1 items-center gap-3 sm:grid-cols-[110px_1fr_auto]">
                                                        <div className="flex items-center">
                                                            <Badge tone="neutral" size="xs">{row.role}</Badge>
                                                        </div>
                                                        <ProportionalStackedBar
                                                            ariaLabel={`${row.role} prompt token sources`}
                                                            barHeightClass="h-5"
                                                            segments={[
                                                                {
                                                                    key: 'local_cache',
                                                                    label: 'Local Prefix Cache',
                                                                    pct: localPct,
                                                                    color: CHART_SERIES[1],
                                                                    displayValue: `${formatCount(row.local_cache_tokens)} tok`,
                                                                },
                                                                {
                                                                    key: 'external_kv',
                                                                    label: 'External KV Transfer',
                                                                    pct: extPct,
                                                                    color: CHART_SERIES[3],
                                                                    displayValue: `${formatCount(row.external_kv_tokens)} tok`,
                                                                },
                                                                {
                                                                    key: 'fresh_compute',
                                                                    label: 'Prefill Compute',
                                                                    pct: freshPct,
                                                                    color: CHART_SERIES[2],
                                                                    displayValue: `${formatCount(row.local_compute_tokens)} tok`,
                                                                },
                                                            ]}
                                                        />
                                                        <div className="whitespace-nowrap text-right font-mono text-[11px] text-theme-muted">
                                                            <strong className="text-theme-text">{formatCount(row.total_tokens)} tok</strong> · Prefill Compute {formatPct(freshPct)}%
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                        <ChartLegend
                                            entries={[
                                                { label: 'Local Prefix Cache', color: CHART_SERIES[1] },
                                                { label: 'External KV Transfer', color: CHART_SERIES[3] },
                                                { label: 'Prefill Compute', color: CHART_SERIES[2] },
                                            ]}
                                        />
                                    </div>
                                ) : (
                                    <EmptyState
                                        title="Prompt token sources by role unavailable"
                                        message="vLLM prompt_tokens_by_source counters were not reported for active serving roles."
                                    />
                                )}
                            </ChartContainer>

                            {perPod.length > 0 && (
                                <Panel
                                    title="Per-Pod vLLM Summary"
                                    help="Per-pod mean engine throughput, running and waiting requests, KV-cache usage, and prefix-cache hit rate across serving pods."
                                    helpFormula="Per-pod mean over scrape window (peak/mean for KV Cache %)"
                                    helpAlign="start"
                                >
                                    <div className="overflow-x-auto xl:overflow-visible">
                                        <table className="w-full text-left text-xs">
                                            <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                                                <tr>
                                                    <th className="py-2 pr-3">
                                                        <span className="inline-flex items-center gap-1">
                                                            Pod
                                                            <HelpTooltip label="Pod column help" align="start" iconSize={11}>
                                                                Serving pod name reported in Prometheus labels.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2">
                                                        <span className="inline-flex items-center gap-1">
                                                            Role
                                                            <HelpTooltip label="Role column help" align="start" iconSize={11}>
                                                                Effective serving role for this pod (Prefill, Decode, or Aggregated Worker).
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Req/s
                                                            <HelpTooltip
                                                                label="Req/s column help"
                                                                align="start"
                                                                iconSize={11}
                                                                formula="Mean_scrapes(ΔPod_Completed_Requests / Δt)"
                                                            >
                                                                Mean request rate handled by this pod over the run (req/s).
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Prefill (tok/s)
                                                            <HelpTooltip
                                                                label="Prefill (tok/s) column help"
                                                                align="center"
                                                                iconSize={11}
                                                                formula="Mean_scrapes(ΔPod_Prompt_Tokens / Δt)"
                                                            >
                                                                Mean prefill (input/prompt) token processing throughput on this pod (tok/s).
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Decode (tok/s)
                                                            <HelpTooltip
                                                                label="Decode (tok/s) column help"
                                                                align="center"
                                                                iconSize={11}
                                                                formula="Mean_scrapes(ΔPod_Generated_Tokens / Δt)"
                                                            >
                                                                Mean decode (output/generated) token throughput on this pod (tok/s).
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Running
                                                            <HelpTooltip
                                                                label="Running column help"
                                                                align="center"
                                                                iconSize={11}
                                                                formula="Mean_scrapes(Pod_Active_Running_Requests(t))"
                                                            >
                                                                Mean active requests concurrently executing on this pod (req).
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Waiting
                                                            <HelpTooltip
                                                                label="Waiting column help"
                                                                align="end"
                                                                iconSize={11}
                                                                formula="Mean_scrapes(Pod_Queued_Waiting_Requests(t))"
                                                            >
                                                                Mean requests queued waiting for execution slots or KV memory on this pod (req).
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            KV Cache (%)
                                                            <HelpTooltip
                                                                label="KV Cache (%) column help"
                                                                align="end"
                                                                iconSize={11}
                                                                formula="max_or_mean_scrapes((Used_Pod_KV_Blocks / Total_Pod_KV_Blocks) × 100)"
                                                            >
                                                                Peak or mean HBM KV-cache usage (%) on this pod; values above 90% risk preemption.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="pl-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Prefix Cache Hit Rate (%)
                                                            <HelpTooltip
                                                                label="Prefix Cache Hit Rate (%) column help"
                                                                align="end"
                                                                iconSize={11}
                                                                formula="Mean_scrapes((ΔPod_Cached_Prompt_Tokens / ΔPod_Total_Prompt_Tokens) × 100)"
                                                            >
                                                                Mean percentage of queried prompt tokens matched in this pod&apos;s local prefix cache.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                </tr>
                                            </thead>
                                            <tbody className="divide-y divide-theme-border">
                                                {perPod.map((pod) => (
                                                    <tr key={pod.pod || pod.name} className="text-theme-text">
                                                        <td className="py-2 pr-3 font-mono font-semibold">{pod.pod || pod.name}</td>
                                                        <td className="px-3 py-2">
                                                            <Badge tone="neutral" size="xs">{displayName(effectivePodRole(pod))}</Badge>
                                                        </td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatNumber(numberAt(pod, ['request_rate_rps.mean', 'request_rate_rps']))}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatNumber(numberAt(pod, ['input_token_rate_tps.mean', 'input_token_rate_tps']))}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatNumber(numberAt(pod, ['output_token_rate_tps.mean', 'output_token_rate_tps', 'output_tps']))}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatNumber(numberAt(pod, ['running_requests.mean', 'running_requests']))}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatNumber(numberAt(pod, ['waiting_requests.mean', 'waiting_requests']))}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatNumber(numberAt(pod, ['kv_cache_usage_percent.max', 'kv_cache_usage_percent.mean', 'kv_cache_usage_percent']), '%')}</td>
                                                        <td className="pl-3 py-2 text-right font-mono">{formatNumber(numberAt(pod, ['prefix_cache_hit_percent.mean', 'prefix_cache_hit_percent']), '%')}</td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                </Panel>
                            )}
                        </section>
                    )}

                    {/* #s-load: Inference Gateway & EPP Routing */}
                    {showSection('load') && (
                        <section id="s-load" className="space-y-4">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="amber" className="mb-0">Load · Inference Gateway &amp; EPP Routing</SectionLabel>
                                <HelpTooltip
                                    label="Inference Gateway and EPP routing load help"
                                    align="start"
                                    iconSize={13}
                                    formula="EPP_InFlight(t), EPP_Sched_Pq(t), Endpoint_Share(%) = (Endpoint_Rate / Σ Endpoint_Rates) × 100"
                                >
                                    Inference Gateway and Endpoint Picker (EPP) routing metrics: EPP in-flight concurrency, scheduling/plugin overhead, cache-aware routing predictions, and per-endpoint traffic distribution.
                                </HelpTooltip>
                            </div>
                            <Panel>
                                {hasEppData ? (
                                    <div className="space-y-5">
                                        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                                            <StatCard
                                                compact
                                                icon={<Activity className="h-4 w-4" />}
                                                title="EPP In-Flight Requests"
                                                help="Mean concurrent requests actively tracked in-flight by the Endpoint Picker (EPP) across backend pods (req)."
                                                helpFormula="Mean_scrapes(EPP_InFlight_Requests(t))"
                                                value={formatNumber(eppInflightMean)}
                                                details={[
                                                    {
                                                        label: 'Peak',
                                                        value: formatNumber(eppInflightMax),
                                                        help: 'Maximum concurrent in-flight requests tracked by EPP in any scrape interval.',
                                                        formula: 'max_scrapes(EPP_InFlight_Requests(t))',
                                                    },
                                                    {
                                                        label: 'Ready Endpoints',
                                                        value: formatNumber(eppReadyEndpoints),
                                                        help: 'Mean number of backend serving pods marked ready to accept routed traffic.',
                                                        formula: 'Mean_scrapes(Count(Ready_Backend_Endpoints(t)))',
                                                    },
                                                    ...(effectiveTokenLoadCv !== null ? [{
                                                        label: isDisaggregated ? 'Intra-Role Load CV' : 'Token Load CV',
                                                        value: formatNumber(effectiveTokenLoadCv),
                                                        help: isDisaggregated
                                                            ? 'Maximum intra-role coefficient of variation of EPP in-flight token load across serving pools with at least two pods.'
                                                            : 'Coefficient of variation of EPP in-flight token load across serving endpoints.',
                                                        formula: isDisaggregated
                                                            ? 'max_role(σ_pod(InFlight_Tokens) / μ_pod(InFlight_Tokens))'
                                                            : 'σ_endpoint(InFlight_Tokens) / μ_endpoint(InFlight_Tokens)',
                                                    }] : []),
                                                ]}
                                            />
                                            <StatCard
                                                compact
                                                icon={<Network className="h-4 w-4" />}
                                                title="EPP Scheduling Latency"
                                                help="Mean of per-scrape median end-to-end endpoint selection duration inside EPP (ms). Measures routing overhead before dispatch."
                                                helpFormula="Mean_scrapes(HistogramQuantile(0.50, ΔEPP_Scheduler_E2E_Buckets / Δt))"
                                                value={eppSchedulerP50 !== null ? formatMilliseconds(eppSchedulerP50) : '—'}
                                                details={[{
                                                    label: 'P90',
                                                    value: formatMilliseconds(eppSchedulerP90),
                                                    help: 'Mean 90th percentile EPP scheduling latency across scrape intervals (ms).',
                                                    formula: 'Mean_scrapes(HistogramQuantile(0.90, ΔEPP_Scheduler_E2E_Buckets / Δt))',
                                                }]}
                                            />
                                            <StatCard
                                                compact
                                                icon={<Clock3 className="h-4 w-4" />}
                                                title="EPP Plugin Latency"
                                                help="Mean of per-scrape median duration spent running EPP filter and scorer plugins (ms). High values indicate expensive routing heuristics."
                                                helpFormula="Mean_scrapes(HistogramQuantile(0.50, ΔEPP_Plugin_Duration_Buckets / Δt))"
                                                value={eppPluginP50 !== null ? formatMilliseconds(eppPluginP50) : '—'}
                                                details={[
                                                    {
                                                        label: 'P90',
                                                        value: formatMilliseconds(eppPluginP90),
                                                        help: 'Mean 90th percentile EPP plugin execution duration across scrape intervals (ms).',
                                                        formula: 'Mean_scrapes(HistogramQuantile(0.90, ΔEPP_Plugin_Duration_Buckets / Δt))',
                                                    },
                                                    {
                                                        label: 'Gateway P90',
                                                        value: istioP90Ms !== null ? formatMilliseconds(istioP90Ms) : 'Unavailable',
                                                        help: 'Mean 90th percentile request latency observed at the Inference Gateway ingress proxy (ms), when exported.',
                                                        formula: 'Mean_scrapes(HistogramQuantile(0.90, ΔGateway_Request_Duration_Buckets / Δt))',
                                                    },
                                                ]}
                                            />
                                            {isPdRoutingTile ? (
                                                <StatCard
                                                    compact
                                                    icon={<Gauge className="h-4 w-4" />}
                                                    title="P/D Routing Ratio"
                                                    help="Percentage of requests routed through disaggregated Prefill-to-Decode (P/D) scheduling rather than single-pod worker execution."
                                                    helpFormula="PD_Routing_Ratio (%) = (Disaggregated_PD_Requests / Total_Routed_Requests) × 100"
                                                    helpAlign="end"
                                                    value={pdDecisionRatio !== null ? formatNumber(pdDecisionRatio, '%') : 'N/A'}
                                                    details={[
                                                        {
                                                            label: 'Avoided Prefill',
                                                            value: formatNumber(derived.avoided_prefill_ratio, '%'),
                                                            help: 'Estimated share of prompt prefill work avoided via prefix-aware routing or KV reuse (%).',
                                                            formula: 'Avoided_Prefill (%) = (Router_Cached_Tokens / Router_Input_Tokens) × 100',
                                                        },
                                                        {
                                                            label: 'Mode',
                                                            value: 'P/D Disaggregation',
                                                            help: 'Active routing architecture splitting prefill and decode across dedicated pod pools.',
                                                        },
                                                    ]}
                                                />
                                            ) : (
                                                <StatCard
                                                    compact
                                                    icon={<Gauge className="h-4 w-4" />}
                                                    title="EPP Predicted Prefix Cache Hit Rate"
                                                    help="Percentage of incoming prompt tokens predicted by the EPP prefix index to hit warm KV cache on the chosen worker pod."
                                                    helpFormula="EPP_Predicted_Hit (%) = (Router_Cached_Tokens / Router_Input_Tokens) × 100"
                                                    helpAlign="end"
                                                    value={eppPredictedCacheHit !== null ? formatNumber(eppPredictedCacheHit, '%') : 'Aggregated'}
                                                    details={[
                                                        {
                                                            label: 'Routing Mode',
                                                            value: 'Aggregated (100% Worker)',
                                                            help: 'Active routing architecture where every worker pod handles both prefill and decode.',
                                                        },
                                                        {
                                                            label: 'Prefix Index Entries',
                                                            value: prefixIndexEntries !== null ? formatCount(prefixIndexEntries) : '—',
                                                            help: 'Mean number of prefix block hashes tracked in the EPP prefix-cache routing index.',
                                                            formula: 'Mean_scrapes(EPP_Prefix_Index_Entry_Count(t))',
                                                        },
                                                    ]}
                                                />
                                            )}
                                        </div>

                                        <div className="grid gap-6 xl:grid-cols-2">
                                            <ChartContainer
                                                title="EPP Load · In-Flight & Request Rate"
                                                subtitle="EPP in-flight requests (left) and EPP / vLLM request rate (right)"
                                                help="EPP in-flight request count (left axis) compared against EPP and serving engine request rates in req/s (right axis) over elapsed time."
                                                helpFormula="EPP_InFlight(t) vs EPP_Rate(t) = ΔEPP_Requests / Δt and Engine_Rate(t) = Σ_pods (ΔEngine_Requests / Δt)"
                                                helpAlign="start"
                                            >
                                                {hasEppLoadSeries ? (
                                                    <>
                                                        <div className="h-52 w-full">
                                                            <ResponsiveContainer width="100%" height="100%">
                                                                <ComposedChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                                    <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                                    <YAxis yAxisId="left" width={48} allowDecimals={false} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'req', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                                    <YAxis yAxisId="right" orientation="right" width={52} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'req/s', angle: 90, position: 'insideRight', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                                    <Tooltip content={<MixedLoadTooltip />} />
                                                                    <Line yAxisId="left" type="monotone" dataKey="epp_running" name="EPP In-Flight (req)" stroke={CHART_SERIES[3]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'epp_running', CHART_SERIES[3])} isAnimationActive={false} />
                                                                    <Line yAxisId="right" type="monotone" dataKey="router_rps" name="EPP Request Rate (req/s)" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'router_rps', CHART_SERIES[0])} isAnimationActive={false} />
                                                                    <Line yAxisId="right" type="monotone" dataKey="engine_rps" name="vLLM Request Rate (req/s)" stroke={CHART_SERIES[1]} strokeWidth={1.75} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'engine_rps', CHART_SERIES[1])} isAnimationActive={false} />
                                                                </ComposedChart>
                                                            </ResponsiveContainer>
                                                        </div>
                                                        <ChartLegend
                                                            className="mt-3"
                                                            entries={[
                                                                { label: 'EPP In-Flight (req)', color: CHART_SERIES[3] },
                                                                { label: 'EPP Request Rate (req/s)', color: CHART_SERIES[0] },
                                                                { label: 'vLLM Request Rate (req/s)', color: CHART_SERIES[1] },
                                                            ]}
                                                        />
                                                    </>
                                                ) : (
                                                    <EmptyState title="No EPP load time series" message="Per-scrape EPP request rate and in-flight gauges were not emitted." />
                                                )}
                                            </ChartContainer>

                                            <ChartContainer
                                                title="EPP Scheduling & Plugin Latency"
                                                subtitle="EPP scheduling and plugin execution overhead (ms)"
                                                help="P50 and P90 EPP scheduling and plugin execution latencies over elapsed time (ms). Sustained spikes indicate EPP CPU or lock contention."
                                                helpFormula="HistogramQuantile(q, ΔEPP_Stage_Buckets / Δt)  (q ∈ {0.50, 0.90})"
                                                helpAlign="end"
                                            >
                                                {hasEppLatencySeries ? (
                                                    <>
                                                        <div className="h-52 w-full">
                                                            <ResponsiveContainer width="100%" height="100%">
                                                                <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                                    <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                                    <YAxis width={56} allowDecimals tickFormatter={formatMsAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                                    <Tooltip content={<MsTooltip />} />
                                                                    <Line type="monotone" dataKey="epp_sched_p50" name="EPP Scheduling P50" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'epp_sched_p50', CHART_SERIES[0])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="epp_sched_p90" name="EPP Scheduling P90" stroke={CHART_SERIES[1]} strokeWidth={1.75} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'epp_sched_p90', CHART_SERIES[1])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="epp_plugin_p50" name="EPP Plugin P50" stroke={CHART_SERIES[2]} strokeWidth={1.75} connectNulls dot={sparseDotProp(series, 'epp_plugin_p50', CHART_SERIES[2])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="epp_plugin_p90" name="EPP Plugin P90" stroke={CHART_SERIES[4]} strokeWidth={1.5} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'epp_plugin_p90', CHART_SERIES[4])} isAnimationActive={false} />
                                                                </LineChart>
                                                            </ResponsiveContainer>
                                                        </div>
                                                        <ChartLegend
                                                            className="mt-3"
                                                            entries={[
                                                                { label: 'EPP Scheduling P50', color: CHART_SERIES[0] },
                                                                { label: 'EPP Scheduling P90', color: CHART_SERIES[1] },
                                                                { label: 'EPP Plugin P50', color: CHART_SERIES[2] },
                                                                { label: 'EPP Plugin P90', color: CHART_SERIES[4] },
                                                            ]}
                                                        />
                                                    </>
                                                ) : (
                                                    <EmptyState title="EPP latency time series unavailable" message="EPP scheduling and plugin latency histograms were not sampled over time." />
                                                )}
                                            </ChartContainer>
                                        </div>

                                        {endpointShareRows.length > 0 && (
                                            <div className="overflow-x-auto lg:overflow-visible rounded-lg border border-theme-border">
                                                <table className="w-full text-left text-xs">
                                                    <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                                                        <tr>
                                                            <th className="px-3 py-2">
                                                                <span className="inline-flex items-center gap-1">
                                                                    Destination Endpoint
                                                                    <HelpTooltip label="Destination Endpoint column help" align="start" iconSize={11}>
                                                                        Backend serving pod or endpoint selected by the Endpoint Picker (EPP).
                                                                    </HelpTooltip>
                                                                </span>
                                                            </th>
                                                            {showEndpointRoleCol && (
                                                                <th className="px-3 py-2">
                                                                    <span className="inline-flex items-center gap-1">
                                                                        Role
                                                                        <HelpTooltip label="Endpoint Role column help" align="start" iconSize={11}>
                                                                            Serving pool role assigned to this endpoint (Prefill, Decode, or Worker).
                                                                        </HelpTooltip>
                                                                    </span>
                                                                </th>
                                                            )}
                                                            <th className="px-3 py-2 text-right">
                                                                <span className="inline-flex items-center justify-end gap-1">
                                                                    Share %
                                                                    <HelpTooltip
                                                                        label="Share % column help"
                                                                        align="center"
                                                                        iconSize={11}
                                                                        formula="Endpoint_Share (%) = (Endpoint_Rate / Σ_endpoints Endpoint_Rate) × 100"
                                                                    >
                                                                        Percentage of routed requests sent to this endpoint; skew often reflects prefix-cache affinity.
                                                                    </HelpTooltip>
                                                                </span>
                                                            </th>
                                                            <th className="px-3 py-2 text-right">
                                                                <span className="inline-flex items-center justify-end gap-1">
                                                                    Rate (req/s)
                                                                    <HelpTooltip
                                                                        label="Endpoint Rate column help"
                                                                        align="end"
                                                                        iconSize={11}
                                                                        formula="Mean_scrapes(ΔEndpoint_Requests / Δt)"
                                                                    >
                                                                        Mean request arrival rate dispatched to this endpoint (req/s).
                                                                    </HelpTooltip>
                                                                </span>
                                                            </th>
                                                            <th className="px-3 py-2 text-right">
                                                                <span className="inline-flex items-center justify-end gap-1">
                                                                    In-Flight Token Load
                                                                    <HelpTooltip
                                                                        label="In-Flight Token Load column help"
                                                                        align="end"
                                                                        iconSize={11}
                                                                        formula="Mean_scrapes(Endpoint_InFlight_Tokens(t))"
                                                                    >
                                                                        Mean concurrent token load queued or executing on this endpoint as tracked by EPP.
                                                                    </HelpTooltip>
                                                                </span>
                                                            </th>
                                                        </tr>
                                                    </thead>
                                                    <tbody className="divide-y divide-theme-border">
                                                        {endpointShareRows.map((row, idx) => {
                                                            const rowRole = String(row.role || 'worker').trim().toLowerCase();
                                                            return (
                                                                <tr key={row.endpoint || row.pod || row.destination || idx} className="text-theme-text">
                                                                    <td className="px-3 py-2 font-mono">{row.endpoint || row.pod || row.destination}</td>
                                                                    {showEndpointRoleCol && (
                                                                        <td className="px-3 py-2">
                                                                            <Badge
                                                                                tone={rowRole === 'prefill' ? 'info' : rowRole === 'decode' ? 'violet' : 'neutral'}
                                                                                size="xs"
                                                                            >
                                                                                {displayName(rowRole)}
                                                                            </Badge>
                                                                        </td>
                                                                    )}
                                                                    <td className="px-3 py-2 text-right font-mono">{row.percent != null ? formatNumber(row.percent, '%') : '—'}</td>
                                                                    <td className="px-3 py-2 text-right font-mono">{row.request_rate_rps != null ? formatNumber(row.request_rate_rps) : '—'}</td>
                                                                    <td className="px-3 py-2 text-right font-mono">{row.inflight_token_load != null ? formatNumber(row.inflight_token_load) : '—'}</td>
                                                                </tr>
                                                            );
                                                        })}
                                                    </tbody>
                                                </table>
                                            </div>
                                        )}
                                    </div>
                                ) : (
                                    <EmptyState
                                        icon={<Network className="h-6 w-6" />}
                                        title="EPP telemetry unavailable"
                                        message="Traffic either targeted a pod directly or EPP metrics were not scraped in this namespace."
                                    />
                                )}
                            </Panel>
                        </section>
                    )}

                    {/* #s-cache: KV Cache (Capacity & Usage) */}
                    {showSection('cache') && (
                        <section id="s-cache" className="space-y-4">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="emerald" className="mb-0">KV Cache · Capacity &amp; Usage</SectionLabel>
                                <HelpTooltip
                                    label="KV cache capacity and usage help"
                                    align="start"
                                    iconSize={13}
                                    formula="KV_Usage (%) = (Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100; HBM_Tokens = Device_KV_Blocks × Block_Size"
                                >
                                    Accelerator HBM key-value cache capacity, paged block geometry, and usage percentage across the serving run.
                                </HelpTooltip>
                            </div>
                            <Panel className="space-y-4">
                                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                                    <StatCard
                                        compact
                                        icon={<Database className="h-4 w-4" />}
                                        title="KV Cache Usage (Peak / Mean)"
                                        help="Peak HBM KV-cache usage (%) reached across serving pods; values above 90% risk request queuing or preemption."
                                        helpFormula="max_scrapes((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100)"
                                        value={kvPeakPct !== null ? formatNumber(kvPeakPct, '%') : '—'}
                                        tone={kvPeakPct !== null && kvPeakPct >= 90 ? 'warning' : 'neutral'}
                                        details={[{
                                            label: 'Mean Usage',
                                            value: formatNumber(kvMeanPct, '%'),
                                            help: 'Average HBM KV-cache usage (%) across scrape intervals during the run.',
                                            formula: 'Mean_scrapes((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100)',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Layers className="h-4 w-4" />}
                                        title="HBM Capacity"
                                        help="Total key-value cache token capacity provisioned in accelerator HBM across the reported engine configuration (tok)."
                                        helpFormula="HBM_Capacity (tok) = Device_KV_Blocks × Block_Size_Tokens"
                                        value={hbmCapacityTokens !== null ? `${formatCount(hbmCapacityTokens)} tok` : '—'}
                                        details={[
                                            {
                                                label: 'Source',
                                                value: cacheConfig.source || 'vLLM cache config',
                                                help: 'Telemetry or configuration source that reported the KV-cache geometry.',
                                            },
                                            ...(isDisaggregated && prefillHbmCapTokens ? [{
                                                label: 'Prefill HBM',
                                                value: `${formatCount(prefillHbmCapTokens)} tok`,
                                                help: 'Total accelerator HBM KV-cache token capacity provisioned across Prefill pods.',
                                                formula: 'Prefill_HBM (tok) = Σ_prefill (Device_KV_Blocks × Block_Size_Tokens)',
                                            }] : []),
                                            ...(isDisaggregated && decodeHbmCapTokens ? [{
                                                label: 'Decode HBM',
                                                value: `${formatCount(decodeHbmCapTokens)} tok`,
                                                help: 'Total accelerator HBM KV-cache token capacity provisioned across Decode pods.',
                                                formula: 'Decode_HBM (tok) = Σ_decode (Device_KV_Blocks × Block_Size_Tokens)',
                                            }] : []),
                                        ]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Server className="h-4 w-4" />}
                                        title="Effective Capacity"
                                        help="Usable KV-cache token capacity available for active sequences and prefix reuse across HBM and CPU tiers (tok)."
                                        helpFormula="Effective_Capacity (tok) = HBM_Capacity_Tokens + CPU_Capacity_Tokens  (fallback: HBM_Capacity_Tokens)"
                                        value={effectiveCapacityTokens !== null ? `${formatCount(effectiveCapacityTokens)} tok` : '—'}
                                        details={[{
                                            label: 'Max KV Concurrency',
                                            value: maxKvConcurrency !== null ? formatNumber(maxKvConcurrency, 'x') : '—',
                                            help: 'Maximum theoretical concurrent request multiplier supported by KV blocks at the configured max model length.',
                                            formula: 'Max_KV_Concurrency = Effective_KV_Tokens / Max_Model_Length_Tokens',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Activity className="h-4 w-4" />}
                                        title="Block Size & Device KV Blocks"
                                        help="Tokens stored per paged KV-cache block; smaller blocks reduce waste while larger blocks lower metadata overhead."
                                        helpFormula="Block_Size = Tokens_Per_KV_Block"
                                        helpAlign="end"
                                        value={blockSize !== null ? `${formatCount(blockSize)} tok` : '—'}
                                        details={[{
                                            label: 'Device KV Blocks',
                                            value: formatCount(numGpuBlocks),
                                            help: 'Total paged KV-cache blocks allocated in accelerator HBM for storing attention keys and values.',
                                            formula: 'Device_KV_Blocks = floor(Available_KV_HBM_Bytes / Bytes_Per_KV_Block)',
                                        }]}
                                    />
                                </div>

                                {hasKvSeries || hasKvTierSeries ? (
                                    <div className="grid min-w-0 gap-6 xl:grid-cols-2">
                                        <ChartContainer
                                            className="min-w-0"
                                            title="HBM KV Cache Usage over Time"
                                            subtitle="Accelerator HBM KV cache usage (%) with 90% pressure threshold"
                                            help="Engine HBM KV-cache usage (%) over elapsed time against the 90% pressure line. Sustained readings near or above 90% risk preemptions and latency spikes."
                                            helpFormula="KV_Usage(t) (%) = (Used_Device_KV_Blocks(t) / Total_Device_KV_Blocks) × 100  (Alert ≥ 90%)"
                                            helpAlign="start"
                                        >
                                            {hasKvSeries ? (
                                                <>
                                                    <div className="h-52 w-full">
                                                        <ResponsiveContainer width="100%" height="100%">
                                                            <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                                <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                                <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                                <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                                <Tooltip content={<PctTooltip />} />
                                                                <ReferenceLine y={90} stroke={CHART_SERIES[4]} strokeDasharray="4 3" label={{ value: '90% Alert', fill: 'var(--text-muted)', fontSize: 10, position: 'insideTopRight' }} />
                                                                {isDisaggregated && (hasDecodeKv || hasPrefillKv) ? (
                                                                    <>
                                                                        {hasDecodeKv && (
                                                                            <Line type="monotone" dataKey="decode_kv_pct" name="Decode HBM KV Usage (%)" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'decode_kv_pct', CHART_SERIES[2])} isAnimationActive={false} />
                                                                        )}
                                                                        {hasPrefillKv && (
                                                                            <Line type="monotone" dataKey="prefill_kv_pct" name="Prefill HBM KV Usage (%)" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'prefill_kv_pct', CHART_SERIES[1])} isAnimationActive={false} />
                                                                        )}
                                                                    </>
                                                                ) : (
                                                                    <Line type="monotone" dataKey="kv_pct" name="HBM KV Cache Usage (%)" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'kv_pct', CHART_SERIES[2])} isAnimationActive={false} />
                                                                )}
                                                            </LineChart>
                                                        </ResponsiveContainer>
                                                    </div>
                                                    {isDisaggregated && (hasDecodeKv || hasPrefillKv) && (
                                                        <ChartLegend
                                                            className="mt-3"
                                                            entries={[
                                                                ...(hasDecodeKv ? [{ label: 'Decode HBM KV Usage (%)', color: CHART_SERIES[2] }] : []),
                                                                ...(hasPrefillKv ? [{ label: 'Prefill HBM KV Usage (%)', color: CHART_SERIES[1] }] : []),
                                                            ]}
                                                        />
                                                    )}
                                                </>
                                            ) : (
                                                <EmptyState title="HBM KV cache series unavailable" message="Accelerator HBM KV cache usage gauges were not reported over time." />
                                            )}
                                        </ChartContainer>

                                        <ChartContainer
                                            className="min-w-0"
                                            title="KV Cache Tier Share (HBM · Host Memory · NVMe)"
                                            subtitle={
                                                kvTierMode === 'share'
                                                    ? 'Active KV cache distribution across HBM, Host Memory, and NVMe (%)'
                                                    : 'Individual tier capacity utilization across HBM, Host Memory, and NVMe (%)'
                                            }
                                            help="Proportional distribution (%) of active cached KV state across Accelerator HBM, Host Memory (CPU RAM offload), and NVMe (local disk offload) over time. Toggle Tier Usage (%) to inspect each storage tier's individual capacity utilization."
                                            helpFormula="Tier_Share_i(t) (%) = Active_KV_i(t) / (Active_KV_HBM(t) + Active_KV_Host(t) + Active_KV_NVMe(t)) × 100"
                                            helpAlign="end"
                                            actions={(
                                                <ToggleGroup
                                                    size="xs"
                                                    value={kvTierMode}
                                                    onChange={setKvTierMode}
                                                    options={[
                                                        { value: 'share', label: 'Share (%)' },
                                                        { value: 'usage', label: 'Tier Usage (%)' },
                                                    ]}
                                                />
                                            )}
                                        >
                                            {hasKvTierSeries ? (
                                                <>
                                                    <div className="mb-3 grid grid-cols-1 gap-2.5 text-xs sm:grid-cols-3">
                                                        <div className="rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2">
                                                            <div className="flex items-center justify-between gap-2">
                                                                <span className="inline-flex items-center gap-1.5 font-medium text-theme-muted">
                                                                    <span className="h-2.5 w-2.5 rounded-sm shrink-0" style={{ backgroundColor: CHART_SERIES[2] }} aria-hidden="true" />
                                                                    HBM
                                                                </span>
                                                                <span className="font-mono font-semibold text-theme-text">
                                                                    {hbmMeanShare !== null ? formatNumber(hbmMeanShare, '%') : '—'}
                                                                </span>
                                                            </div>
                                                            <div className="mt-1 truncate text-[11px] text-theme-muted">
                                                                Mean Usage: {formatNumber(hbmMeanUsage, '%')}
                                                                {hbmCapLabel ? ` · ${hbmCapLabel}` : ''}
                                                            </div>
                                                        </div>

                                                        <div className="rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2">
                                                            <div className="flex items-center justify-between gap-2">
                                                                <span className="inline-flex items-center gap-1.5 font-medium text-theme-muted">
                                                                    <span className="h-2.5 w-2.5 rounded-sm shrink-0" style={{ backgroundColor: CHART_SERIES[1] }} aria-hidden="true" />
                                                                    Host Memory
                                                                </span>
                                                                <span className="font-mono font-semibold text-theme-text">
                                                                    {hostMeanShare !== null ? formatNumber(hostMeanShare, '%') : '—'}
                                                                </span>
                                                            </div>
                                                            <div className="mt-1 truncate text-[11px] text-theme-muted">
                                                                {hostConfigured || (hostMeanUsage !== null && hostMeanUsage > 0)
                                                                    ? `Mean Usage: ${formatNumber(hostMeanUsage ?? 0, '%')}${cpuCapLabel ? ` · ${cpuCapLabel}` : ''}`
                                                                    : 'Not configured'}
                                                            </div>
                                                        </div>

                                                        <div className="rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2">
                                                            <div className="flex items-center justify-between gap-2">
                                                                <span className="inline-flex items-center gap-1.5 font-medium text-theme-muted">
                                                                    <span className="h-2.5 w-2.5 rounded-sm shrink-0" style={{ backgroundColor: CHART_SERIES[0] }} aria-hidden="true" />
                                                                    NVMe
                                                                </span>
                                                                <span className="font-mono font-semibold text-theme-text">
                                                                    {nvmeMeanShare !== null ? formatNumber(nvmeMeanShare, '%') : '—'}
                                                                </span>
                                                            </div>
                                                            <div className="mt-1 truncate text-[11px] text-theme-muted">
                                                                {nvmeConfigured || (diskMeanUsage !== null && diskMeanUsage > 0)
                                                                    ? `Mean Usage: ${formatNumber(diskMeanUsage ?? 0, '%')}${nvmeCapLabel ? ` · ${nvmeCapLabel}` : ''}`
                                                                    : 'Not configured'}
                                                            </div>
                                                        </div>
                                                    </div>

                                                    <div className="h-52 w-full">
                                                        <ResponsiveContainer width="100%" height="100%">
                                                            {kvTierMode === 'share' ? (
                                                                <AreaChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                                    <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                                    <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                                    <Tooltip content={<KvTierTooltip />} />
                                                                    <Area type="monotone" dataKey="hbm_share_pct" name="HBM" stackId="kvTierShare" stroke={CHART_SERIES[2]} fill={CHART_SERIES[2]} fillOpacity={0.65} strokeWidth={1.5} connectNulls isAnimationActive={false} />
                                                                    <Area type="monotone" dataKey="host_share_pct" name="Host Memory (Offload)" stackId="kvTierShare" stroke={CHART_SERIES[1]} fill={CHART_SERIES[1]} fillOpacity={0.65} strokeWidth={1.5} connectNulls isAnimationActive={false} />
                                                                    <Area type="monotone" dataKey="nvme_share_pct" name="NVMe (Offload)" stackId="kvTierShare" stroke={CHART_SERIES[0]} fill={CHART_SERIES[0]} fillOpacity={0.65} strokeWidth={1.5} connectNulls isAnimationActive={false} />
                                                                </AreaChart>
                                                            ) : (
                                                                <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                                    <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                                    <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                                    <Tooltip content={<KvTierTooltip />} />
                                                                    <Line type="monotone" dataKey="kv_pct" name="HBM Usage (%)" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'kv_pct', CHART_SERIES[2])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="cpu_kv_pct" name="Host Memory Usage (%)" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'cpu_kv_pct', CHART_SERIES[1])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="disk_kv_pct" name="NVMe Usage (%)" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'disk_kv_pct', CHART_SERIES[0])} isAnimationActive={false} />
                                                                </LineChart>
                                                            )}
                                                        </ResponsiveContainer>
                                                    </div>
                                                    <ChartLegend
                                                        className="mt-3"
                                                        entries={
                                                            kvTierMode === 'share'
                                                                ? [
                                                                    { label: 'HBM', color: CHART_SERIES[2] },
                                                                    { label: 'Host Memory (Offload)', color: CHART_SERIES[1] },
                                                                    { label: 'NVMe (Offload)', color: CHART_SERIES[0] },
                                                                ]
                                                                : [
                                                                    { label: 'HBM Usage (%)', color: CHART_SERIES[2] },
                                                                    { label: 'Host Memory Usage (%)', color: CHART_SERIES[1] },
                                                                    { label: 'NVMe Usage (%)', color: CHART_SERIES[0] },
                                                                ]
                                                        }
                                                    />
                                                </>
                                            ) : (
                                                <EmptyState title="KV cache tier share unavailable" message="KV cache tier utilization gauges were not reported over time." />
                                            )}
                                        </ChartContainer>
                                    </div>
                                ) : (
                                    <EmptyState title="HBM KV cache series unavailable" message="Accelerator HBM KV cache usage gauges were not reported over time." />
                                )}

                                {cacheEngines.length > 0 && (
                                    <div className="overflow-x-auto lg:overflow-visible rounded-lg border border-theme-border">
                                        <table className="w-full text-left text-xs">
                                            <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                                                <tr>
                                                    <th className="px-3 py-2">
                                                        <span className="inline-flex items-center gap-1">
                                                            Pod
                                                            <HelpTooltip label="Pod column help" align="start" iconSize={11}>
                                                                Serving pod reporting this KV-cache geometry.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Engine
                                                            <HelpTooltip label="Engine column help" align="start" iconSize={11}>
                                                                Engine worker or data-parallel index within the pod.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Block Size
                                                            <HelpTooltip
                                                                label="Block Size column help"
                                                                align="center"
                                                                iconSize={11}
                                                                formula="Block_Size = Tokens_Per_Paged_KV_Block"
                                                            >
                                                                Number of tokens per paged KV-cache memory block.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Device KV Blocks
                                                            <HelpTooltip
                                                                label="Device KV Blocks column help"
                                                                align="end"
                                                                iconSize={11}
                                                                formula="Device_KV_Blocks = Total_Allocated_HBM_KV_Blocks"
                                                            >
                                                                Total paged KV-cache blocks allocated in this pod&apos;s accelerator HBM.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            KV Tokens
                                                            <HelpTooltip
                                                                label="KV Tokens column help"
                                                                align="end"
                                                                iconSize={11}
                                                                formula="KV_Tokens = Device_KV_Blocks × Block_Size"
                                                            >
                                                                Total token capacity across all allocated KV-cache blocks on this pod.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                    <th className="px-3 py-2 text-right">
                                                        <span className="inline-flex items-center justify-end gap-1">
                                                            Max Concurrency
                                                            <HelpTooltip
                                                                label="Max Concurrency column help"
                                                                align="end"
                                                                iconSize={11}
                                                                formula="Max_Concurrency = KV_Tokens / Max_Model_Length_Tokens"
                                                            >
                                                                Maximum concurrent sequence multiplier supported by KV blocks at max sequence length.
                                                            </HelpTooltip>
                                                        </span>
                                                    </th>
                                                </tr>
                                            </thead>
                                            <tbody className="divide-y divide-theme-border">
                                                {cacheEngines.map((eng, idx) => (
                                                    <tr key={`${eng.pod || eng.instance || idx}-${eng.engine || 0}`} className="text-theme-text">
                                                        <td className="px-3 py-2 font-mono">{eng.pod || eng.instance || `engine-${idx}`}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{eng.engine ?? '0'}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatCount(eng.block_size)}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatCount(eng.num_gpu_blocks)}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{formatCount(eng.kv_cache_size_tokens)}</td>
                                                        <td className="px-3 py-2 text-right font-mono">{eng.kv_cache_max_concurrency != null ? formatNumber(eng.kv_cache_max_concurrency, 'x') : '—'}</td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                )}
                            </Panel>
                        </section>
                    )}

                    {/* #s-kvreuse: KV Reuse */}
                    {showSection('kvreuse') && (
                        <section id="s-kvreuse" className="space-y-4">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="cyan" className="mb-0">KV Reuse · Prefix Cache &amp; Prompt Sources</SectionLabel>
                                <HelpTooltip
                                    label="KV reuse help"
                                    align="start"
                                    iconSize={13}
                                    formula="Hit_Rate (%) = (ΔCached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100; Avoided_Prefill = Cached / Total_Prompt"
                                >
                                    Tracks automatic prefix caching (APC), external KV-cache hits, and how much prompt prefill compute was avoided versus prefill-computed.
                                </HelpTooltip>
                            </div>
                            <Panel className="space-y-4">
                                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                                    <StatCard
                                        compact
                                        icon={<Activity className="h-4 w-4" />}
                                        title="Prefix Cache Hit Rate"
                                        help="Mean percentage of queried prompt tokens matched in local HBM prefix cache across scrapes; higher hit rates reduce TTFT."
                                        helpFormula="Mean_scrapes((ΔLocal_Cached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100)"
                                        value={prefixHitMean !== null ? formatNumber(prefixHitMean, '%') : '—'}
                                        details={[{
                                            label: 'Peak',
                                            value: formatNumber(prefixHitMax, '%'),
                                            help: 'Maximum prefix-cache hit rate observed in any scrape interval.',
                                            formula: 'max_scrapes((ΔLocal_Cached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100)',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Database className="h-4 w-4" />}
                                        title={isDisaggregated ? 'Decode External KV Share (NIXL)' : 'External Prefix Cache Hit Rate'}
                                        help={isDisaggregated
                                            ? 'Mean percentage of Decode-pool prompt tokens fulfilled via disaggregated external KV transfer (NIXL). Tracked separately from Prefill local prefix cache hits to avoid double-counting.'
                                            : 'Mean percentage of prompt tokens satisfied from external or remote KV-cache tiers (e.g., CPU offload or disaggregated P/D transfer).'}
                                        helpFormula={isDisaggregated
                                            ? 'Decode_External_KV_Share (%) = (ΔDecode_External_KV_Tokens / ΔDecode_Prompt_Tokens) × 100'
                                            : 'Mean_scrapes((ΔExternal_Cached_Prompt_Tokens / ΔTotal_Prompt_Tokens) × 100)'}
                                        value={extHitMean !== null ? formatNumber(extHitMean, '%') : '—'}
                                        details={[{
                                            label: 'Avoided Prefill',
                                            value: formatNumber(derived.avoided_prefill_ratio, '%'),
                                            help: 'Estimated fraction of total prompt prefill compute avoided across the run due to cached KV reuse (%).',
                                            formula: 'Avoided_Prefill (%) = (Router_Cached_Tokens / Router_Input_Tokens) × 100',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Gauge className="h-4 w-4" />}
                                        title="Local Prefix Cache Throughput"
                                        help="Mean prompt token throughput served directly from local HBM prefix cache hits without recomputation (tok/s)."
                                        helpFormula="Mean_scrapes(Σ_pods (ΔLocal_Cached_Prompt_Tokens / Δt))"
                                        value={localCachedTpsMean !== null ? formatNumber(localCachedTpsMean, ' tok/s') : '—'}
                                        details={[{
                                            label: isDisaggregated ? 'Decode External KV (NIXL)' : 'External KV Transfer',
                                            value: externalCachedTpsMean !== null ? formatNumber(externalCachedTpsMean, ' tok/s') : '—',
                                            help: 'Mean prompt token throughput fulfilled via external KV transfers (tok/s).',
                                            formula: 'Mean_scrapes(Σ_pods (ΔExternal_Cached_Prompt_Tokens / Δt))',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Zap className="h-4 w-4" />}
                                        title="Prefill Compute Throughput"
                                        help="Mean prompt token throughput requiring prefill attention computation on the accelerators (tok/s)."
                                        helpFormula="Mean_scrapes(Σ_pods (ΔPrefill_Computed_Prompt_Tokens / Δt))"
                                        helpAlign="end"
                                        value={freshComputedTpsMean !== null ? formatNumber(freshComputedTpsMean, ' tok/s') : '—'}
                                        details={[{
                                            label: 'Recomputed Prefix',
                                            value: derived.recomputed_token_rate_tps != null ? formatNumber(derived.recomputed_token_rate_tps, ' tok/s') : '—',
                                            help: 'Mean rate of prompt tokens recomputed after eviction or preemption (tok/s); nonzero values signal KV thrashing.',
                                            formula: 'Recomputed_Rate = Recomputed_Prefix_Tokens / Run_Duration_s',
                                        }]}
                                    />
                                </div>

                                <div className="grid gap-6 xl:grid-cols-2">
                                    <ChartContainer
                                        title="Prefix Cache Hit Rate over Time"
                                        subtitle="Local Prefix Cache · External KV Transfer · Recomputed Prefix · Uncached Prompt (%)"
                                        help="Stacked prompt-token disposition over elapsed time (0–100%): local prefix cache hits, external KV transfers, shared-prefix tokens recomputed after miss or eviction, and uncached prompt tokens."
                                        helpFormula="Local_Prefix_Cache(t) + External_KV_Transfer(t) + Recomputed_Prefix(t) + Uncached_Prompt(t) = 100%"
                                        helpAlign="start"
                                    >
                                        {hasPrefixHitSeries ? (
                                            <>
                                                <div className="h-52 w-full">
                                                    <ResponsiveContainer width="100%" height="100%">
                                                        <AreaChart data={prefixHitSeries} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                            <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                            <Tooltip content={<StackedPctTooltip />} />
                                                            <Area type="monotone" stackId="prefixHit" dataKey="prefix_hit_pct" name="Local Prefix Cache (%)" fill={CHART_SERIES[0]} fillOpacity={0.32} stroke={CHART_SERIES[0]} strokeWidth={1.75} connectNulls dot={sparseDotProp(prefixHitSeries, 'prefix_hit_pct', CHART_SERIES[0])} isAnimationActive={false} />
                                                            <Area type="monotone" stackId="prefixHit" dataKey="ext_hit_pct" name="External KV Transfer (%)" fill={CHART_SERIES[3]} fillOpacity={0.32} stroke={CHART_SERIES[3]} strokeWidth={1.75} connectNulls dot={sparseDotProp(prefixHitSeries, 'ext_hit_pct', CHART_SERIES[3])} isAnimationActive={false} />
                                                            <Area type="monotone" stackId="prefixHit" dataKey="recompute_pct" name="Recomputed Prefix (%)" fill={CHART_SERIES[2]} fillOpacity={0.32} stroke={CHART_SERIES[2]} strokeWidth={1.75} connectNulls dot={sparseDotProp(prefixHitSeries, 'recompute_pct', CHART_SERIES[2])} isAnimationActive={false} />
                                                            <Area type="monotone" stackId="prefixHit" dataKey="cold_new_pct" name="Uncached Prompt (%)" fill={CHART_SERIES[4]} fillOpacity={0.32} stroke={CHART_SERIES[4]} strokeWidth={1.75} connectNulls dot={sparseDotProp(prefixHitSeries, 'cold_new_pct', CHART_SERIES[4])} isAnimationActive={false} />
                                                        </AreaChart>
                                                    </ResponsiveContainer>
                                                </div>
                                                <ChartLegend
                                                    className="mt-3"
                                                    entries={[
                                                        { label: 'Local Prefix Cache (%)', color: CHART_SERIES[0] },
                                                        { label: 'External KV Transfer (%)', color: CHART_SERIES[3] },
                                                        { label: 'Recomputed Prefix (%)', color: CHART_SERIES[2] },
                                                        { label: 'Uncached Prompt (%)', color: CHART_SERIES[4] },
                                                    ]}
                                                />
                                            </>
                                        ) : (
                                            <EmptyState title="Prefix cache hit series unavailable" message="vLLM prefix cache query/hit counters were not recorded over time." />
                                        )}
                                    </ChartContainer>

                                    <ChartContainer
                                        title="Prompt Token Sources over Time"
                                        subtitle={isDisaggregated
                                            ? 'Prefill pool prompt sources (tok/s); Decode NIXL KV transfer tracked separately'
                                            : 'Stacked local prefix cache + external KV transfer + prefill compute (tok/s)'}
                                        help="Stacked prompt token throughput broken down by source over elapsed time (tok/s): local HBM prefix cache hits, external KV transfers, and prefill compute tokens. Stack height equals total prompt token throughput."
                                        helpFormula="Total_Prompt_Rate(t) = Local_Cache_tok/s(t) + External_KV_tok/s(t) + Prefill_Compute_tok/s(t)"
                                        helpAlign="end"
                                    >
                                        {hasPromptSourceSeries ? (
                                            <>
                                                <div className="h-52 w-full">
                                                    <ResponsiveContainer width="100%" height="100%">
                                                        <AreaChart data={promptSourceSeries} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                            <YAxis width={60} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'tok/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                            <Tooltip content={<StackedTokTooltip />} />
                                                            <Area type="monotone" stackId="promptSource" dataKey="prompt_local_cache_tps" name="Local Prefix Cache" fill={CHART_SERIES[1]} fillOpacity={0.32} stroke={CHART_SERIES[1]} strokeWidth={1.75} connectNulls dot={sparseDotProp(promptSourceSeries, 'prompt_local_cache_tps', CHART_SERIES[1])} isAnimationActive={false} />
                                                            <Area type="monotone" stackId="promptSource" dataKey="prompt_external_tps" name="External KV Transfer" fill={CHART_SERIES[3]} fillOpacity={0.32} stroke={CHART_SERIES[3]} strokeWidth={1.75} connectNulls dot={sparseDotProp(promptSourceSeries, 'prompt_external_tps', CHART_SERIES[3])} isAnimationActive={false} />
                                                            <Area type="monotone" stackId="promptSource" dataKey="prompt_fresh_tps" name="Prefill Compute" fill={CHART_SERIES[2]} fillOpacity={0.32} stroke={CHART_SERIES[2]} strokeWidth={1.75} connectNulls dot={sparseDotProp(promptSourceSeries, 'prompt_fresh_tps', CHART_SERIES[2])} isAnimationActive={false} />
                                                        </AreaChart>
                                                    </ResponsiveContainer>
                                                </div>
                                                <ChartLegend
                                                    className="mt-3"
                                                    entries={[
                                                        { label: 'Local Prefix Cache', color: CHART_SERIES[1] },
                                                        { label: 'External KV Transfer', color: CHART_SERIES[3] },
                                                        { label: 'Prefill Compute', color: CHART_SERIES[2] },
                                                    ]}
                                                />
                                            </>
                                        ) : (
                                            <EmptyState title="Prompt token source series unavailable" message="vLLM prompt_tokens_by_source counters were not reported." />
                                        )}
                                    </ChartContainer>
                                </div>
                            </Panel>
                        </section>
                    )}

                    {/* #s-offload: KV Offload (Multi-Tier Usage & Data Movement) */}
                    {showSection('offload') && (
                        <section id="s-offload" className="space-y-4">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5">
                                    <SectionLabel tone="sky" className="mb-0">
                                        {showNixlSection ? 'KV Offload · Multi-Tier Usage & NIXL Transfer' : 'KV Offload · Multi-Tier Usage & Offload'}
                                    </SectionLabel>
                                    <HelpTooltip
                                        label="KV offload help"
                                        align="start"
                                        iconSize={13}
                                        formula="Tier_Usage (%) = (Used_Tier_KV / Total_Tier_KV) × 100; Transfer_Rate = (ΔTransferred_Bytes / Δt) / 2^20 (MiB/s)"
                                    >
                                        Multi-tier KV-cache capacity utilization across accelerator VRAM, host CPU memory, and NVMe storage, alongside KV offload/restore throughput and peer-to-peer NIXL KV-block transfers across disaggregated prefill and decode pools.
                                    </HelpTooltip>
                                </div>
                                {!showNixlSection && (
                                    <Badge tone="neutral" size="xs">P/D NIXL: N/A (Aggregated)</Badge>
                                )}
                            </div>
                            <Panel className="space-y-4">
                                <div className="grid gap-4 sm:grid-cols-3">
                                    <StatCard
                                        compact
                                        icon={<Cpu className="h-4 w-4" />}
                                        title="VRAM KV Cache Usage"
                                        help="Mean usage (%) of accelerator VRAM allocated for primary KV-cache blocks across serving pods."
                                        helpFormula="Mean_scrapes((Used_VRAM_KV_Blocks / Total_VRAM_KV_Blocks) × 100)"
                                        value={vramCacheMean !== null ? formatNumber(vramCacheMean, '%') : (vramCacheMax !== null ? formatNumber(vramCacheMax, '%') : 'N/A')}
                                        tone={vramCacheMax !== null && vramCacheMax >= 90 ? 'warning' : 'neutral'}
                                        details={[
                                            {
                                                label: 'Peak',
                                                value: vramCacheMax !== null ? formatNumber(vramCacheMax, '%') : '—',
                                                help: 'Maximum accelerator VRAM KV-cache usage (%) observed in any scrape interval.',
                                                formula: 'max_scrapes((Used_VRAM_KV_Blocks / Total_VRAM_KV_Blocks) × 100)',
                                            },
                                            ...(hbmCapLabel ? [{
                                                label: 'VRAM Capacity',
                                                value: hbmCapLabel,
                                                help: 'Total KV-cache capacity provisioned in accelerator VRAM.',
                                                formula: 'VRAM_Capacity = Device_KV_Blocks × Block_Size_Bytes',
                                            }] : []),
                                        ]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Server className="h-4 w-4" />}
                                        title="CPU KV Cache Usage"
                                        help="Mean usage (%) of host CPU RAM allocated as secondary KV-cache offload storage across serving pods."
                                        helpFormula="Mean_scrapes((Used_Host_KV_Capacity / Total_Host_KV_Capacity) × 100)"
                                        value={cpuCacheMean !== null ? formatNumber(cpuCacheMean, '%') : (cpuCacheMax !== null ? formatNumber(cpuCacheMax, '%') : (showNixlSection ? 'Not enabled' : 'N/A'))}
                                        details={[
                                            {
                                                label: 'Peak',
                                                value: cpuCacheMax !== null ? formatNumber(cpuCacheMax, '%') : '—',
                                                help: 'Maximum host CPU KV-cache usage (%) observed in any scrape interval.',
                                                formula: 'max_scrapes((Used_Host_KV_Capacity / Total_Host_KV_Capacity) × 100)',
                                            },
                                            ...(cpuCapLabel ? [{
                                                label: 'CPU Capacity',
                                                value: cpuCapLabel,
                                                help: 'Total KV-cache capacity provisioned in host CPU memory.',
                                                formula: 'CPU_Capacity = Host_KV_Blocks × Block_Size',
                                            }] : []),
                                        ]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Database className="h-4 w-4" />}
                                        title="NVMe KV Cache Usage"
                                        help="Mean usage (%) of local NVMe or disk tier allocated as tertiary KV-cache offload storage across serving pods."
                                        helpFormula="Mean_scrapes((Used_NVMe_KV_Capacity / Total_NVMe_KV_Capacity) × 100)"
                                        helpAlign="end"
                                        value={nvmeCacheMean !== null ? formatNumber(nvmeCacheMean, '%') : (nvmeCacheMax !== null ? formatNumber(nvmeCacheMax, '%') : (showNixlSection ? 'Not enabled' : 'N/A'))}
                                        details={[
                                            {
                                                label: 'Peak',
                                                value: nvmeCacheMax !== null ? formatNumber(nvmeCacheMax, '%') : '—',
                                                help: 'Maximum NVMe KV-cache usage (%) observed in any scrape interval.',
                                                formula: 'max_scrapes((Used_NVMe_KV_Capacity / Total_NVMe_KV_Capacity) × 100)',
                                            },
                                            ...(nvmeCapLabel ? [{
                                                label: 'NVMe Capacity',
                                                value: nvmeCapLabel,
                                                help: 'Total KV-cache capacity provisioned on NVMe storage.',
                                                formula: 'NVMe_Capacity = Configured_NVMe_Offload_Bytes',
                                            }] : []),
                                        ]}
                                    />
                                </div>

                                {showNixlSection ? (
                                    <div className="grid gap-4 sm:grid-cols-3">
                                        <StatCard
                                            compact
                                            icon={<Database className="h-4 w-4" />}
                                            title="KV Offload / Restore Throughput"
                                            help="Mean bandwidth (or total bytes) of KV-cache blocks offloaded from accelerator VRAM to CPU/NVMe tiers."
                                            helpFormula="Offload_Rate = (ΔOffloaded_Bytes / Δt) / 2^20  (MiB/s)"
                                            value={offloadRateBps !== null
                                                ? formatNumber(offloadRateBps / (1024 ** 2), ' MiB/s')
                                                : (offloadBytes !== null ? (formatBytes(offloadBytes) || '0 B') : 'Not enabled')}
                                            details={[
                                                {
                                                    label: 'Restore Rate',
                                                    value: restoreRateBps !== null ? formatNumber(restoreRateBps / (1024 ** 2), ' MiB/s') : (restoreBytes !== null ? (formatBytes(restoreBytes) || '0 B') : '—'),
                                                    help: 'Mean bandwidth (or total bytes) of KV-cache blocks restored back from offload tiers to accelerator VRAM.',
                                                    formula: 'Restore_Rate = (ΔRestored_Bytes / Δt) / 2^20  (MiB/s)',
                                                },
                                                {
                                                    label: 'Offloaded / Restored',
                                                    value: `${offloadBytes !== null ? (formatBytes(offloadBytes) || '0 B') : '—'} / ${restoreBytes !== null ? (formatBytes(restoreBytes) || '0 B') : '—'}`,
                                                    help: 'Cumulative KV-cache volume offloaded to secondary tiers and restored back to accelerators over the run.',
                                                    formula: 'Cumulative_Bytes = ΔTransferred_Bytes  (or ∫ Rate(t) dt)',
                                                },
                                            ]}
                                        />
                                        <StatCard
                                            compact
                                            icon={<Network className="h-4 w-4" />}
                                            title="NIXL KV Transfer Throughput"
                                            help="Mean NIXL KV-cache transfer throughput between prefill and decode workers (MiB/s)."
                                            helpFormula="NIXL_Rate = (ΔNIXL_Transferred_Bytes / Δt) / 2^20  (MiB/s)"
                                            value={nixlBytesRate !== null ? formatNumber(nixlBytesRate / (1024 ** 2), ' MiB/s') : 'Not enabled'}
                                            details={[{
                                                label: 'P95 Latency',
                                                value: nixlP95Ms !== null ? formatMilliseconds(nixlP95Ms) : '—',
                                                help: 'Mean 95th percentile NIXL KV-block transfer duration (ms); high latency adds directly to TTFT.',
                                                formula: 'Mean_scrapes(HistogramQuantile(0.95, ΔNIXL_Transfer_Duration_Buckets / Δt))',
                                            }]}
                                        />
                                        <StatCard
                                            compact
                                            icon={<Activity className="h-4 w-4" />}
                                            title="NIXL KV Transfer Failures"
                                            help="Total or rate of failed NIXL KV-cache transfers during the run; any value above zero indicates transport errors or timeouts."
                                            helpFormula="Failures = Σ Failed_NIXL_Transfers  (or Mean_scrapes(ΔFailed_Transfers / Δt))"
                                            helpAlign="end"
                                            value={nixlFailed !== null ? formatCount(nixlFailed) : 'Not enabled'}
                                            tone={nixlFailed !== null && nixlFailed > 0 ? 'danger' : 'neutral'}
                                            details={[{
                                                label: 'Status',
                                                value: hasOffloadOrNixl ? 'Active' : 'Disabled on deployment',
                                                help: 'Indicates whether multi-tier KV offload or NIXL transfer telemetry was emitted by this deployment.',
                                            }]}
                                        />
                                    </div>
                                ) : (
                                    <div className="grid gap-4 sm:grid-cols-3">
                                        <StatCard
                                            compact
                                            icon={<Database className="h-4 w-4" />}
                                            title="KV Offload Throughput"
                                            help="Total KV-cache bytes (or mean rate) offloaded from accelerator VRAM to CPU/NVMe tiers to avoid recomputation."
                                            helpFormula="Total_Bytes = ΔOffloaded_Bytes  (or ∫ Offload_Rate(t) dt)"
                                            value={offloadBytes !== null
                                                ? (formatBytes(offloadBytes) || '0 B')
                                                : (offloadRateBps !== null ? formatNumber(offloadRateBps / (1024 ** 2), ' MiB/s') : 'N/A')}
                                            details={[
                                                {
                                                    label: 'Mean',
                                                    value: offloadRateBps !== null ? formatNumber(offloadRateBps / (1024 ** 2), ' MiB/s') : '—',
                                                    help: 'Mean KV-cache offload bandwidth from accelerator VRAM to offload tiers (MiB/s).',
                                                    formula: 'Mean_scrapes((ΔOffloaded_Bytes / Δt) / 2^20)',
                                                },
                                                {
                                                    label: 'Peak',
                                                    value: offloadPeakRateBps !== null ? formatNumber(offloadPeakRateBps / (1024 ** 2), ' MiB/s') : '—',
                                                    help: 'Peak KV-cache offload bandwidth observed in any scrape interval (MiB/s).',
                                                    formula: 'max_scrapes((ΔOffloaded_Bytes / Δt) / 2^20)',
                                                },
                                            ]}
                                        />
                                        <StatCard
                                            compact
                                            icon={<Database className="h-4 w-4" />}
                                            title="KV Restore Throughput"
                                            help="Total KV-cache bytes (or mean rate) restored from CPU/NVMe tiers back into accelerator VRAM on prefix-cache hits."
                                            helpFormula="Total_Bytes = ΔRestored_Bytes  (or ∫ Restore_Rate(t) dt)"
                                            value={restoreBytes !== null
                                                ? (formatBytes(restoreBytes) || '0 B')
                                                : (restoreRateBps !== null ? formatNumber(restoreRateBps / (1024 ** 2), ' MiB/s') : 'N/A')}
                                            details={[
                                                {
                                                    label: 'Mean',
                                                    value: restoreRateBps !== null ? formatNumber(restoreRateBps / (1024 ** 2), ' MiB/s') : '—',
                                                    help: 'Mean KV-cache restore bandwidth from offload tiers back to accelerator VRAM (MiB/s).',
                                                    formula: 'Mean_scrapes((ΔRestored_Bytes / Δt) / 2^20)',
                                                },
                                                {
                                                    label: 'Peak',
                                                    value: restorePeakRateBps !== null ? formatNumber(restorePeakRateBps / (1024 ** 2), ' MiB/s') : '—',
                                                    help: 'Peak KV-cache restore bandwidth observed in any scrape interval (MiB/s).',
                                                    formula: 'max_scrapes((ΔRestored_Bytes / Δt) / 2^20)',
                                                },
                                            ]}
                                        />
                                        <StatCard
                                            compact
                                            icon={<Activity className="h-4 w-4" />}
                                            title="KV Offload Lookup Sync Delay (P95)"
                                            help="95th percentile latency for querying and synchronizing offloaded KV-cache entries across offload tiers (ms)."
                                            helpFormula="HistogramQuantile(0.95, ΔKV_Offload_Lookup_Latency_Buckets / Δt)"
                                            helpAlign="end"
                                            value={lookupSyncDelayP95Ms !== null ? formatMilliseconds(lookupSyncDelayP95Ms) : 'N/A'}
                                            details={[{
                                                label: 'Topology',
                                                value: 'Aggregated (Multi-tier offload)',
                                                help: 'Offload architecture active for this run (multi-tier KV offload without P/D NIXL).',
                                            }]}
                                        />
                                    </div>
                                )}

                                <div className="grid min-w-0 gap-6 xl:grid-cols-2">
                                    <ChartContainer
                                        className="min-w-0"
                                        title="KV Cache Usage by Tier"
                                        subtitle="VRAM, CPU host memory, and NVMe KV cache capacity utilization (%) over time"
                                        help="Tracks capacity utilization (%) across all three KV-cache tiers over elapsed time: accelerator VRAM, host CPU memory, and NVMe storage."
                                        helpFormula="Tier_Usage(t) (%) = (Used_Tier_KV(t) / Total_Tier_KV) × 100"
                                        helpAlign="start"
                                    >
                                        {hasTierUsageSeries ? (
                                            <>
                                                <div className="h-52 w-full">
                                                    <ResponsiveContainer width="100%" height="100%">
                                                        <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                            <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                            <Tooltip content={<OffloadTierUsageTooltip />} />
                                                            {showSplitVramTiers ? (
                                                                <>
                                                                    <Line type="monotone" dataKey="decode_kv_pct" name="Decode VRAM KV Usage (%)" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'decode_kv_pct', CHART_SERIES[0])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="prefill_kv_pct" name="Prefill VRAM KV Usage (%)" stroke={CHART_SERIES[3]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'prefill_kv_pct', CHART_SERIES[3])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="cpu_kv_pct" name="CPU KV Cache Usage (%)" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'cpu_kv_pct', CHART_SERIES[1])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="disk_kv_pct" name="NVMe KV Cache Usage (%)" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'disk_kv_pct', CHART_SERIES[2])} isAnimationActive={false} />
                                                                </>
                                                            ) : (
                                                                <>
                                                                    <Line type="monotone" dataKey="kv_pct" name="VRAM KV Cache Usage (%)" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'kv_pct', CHART_SERIES[0])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="cpu_kv_pct" name="CPU KV Cache Usage (%)" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'cpu_kv_pct', CHART_SERIES[1])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey="disk_kv_pct" name="NVMe KV Cache Usage (%)" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'disk_kv_pct', CHART_SERIES[2])} isAnimationActive={false} />
                                                                </>
                                                            )}
                                                        </LineChart>
                                                    </ResponsiveContainer>
                                                </div>
                                                <ChartLegend
                                                    className="mt-3"
                                                    entries={showSplitVramTiers ? [
                                                        { label: 'Decode VRAM KV Usage (%)', color: CHART_SERIES[0] },
                                                        { label: 'Prefill VRAM KV Usage (%)', color: CHART_SERIES[3] },
                                                        { label: 'CPU KV Cache Usage (%)', color: CHART_SERIES[1] },
                                                        { label: 'NVMe KV Cache Usage (%)', color: CHART_SERIES[2] },
                                                    ] : [
                                                        { label: 'VRAM KV Cache Usage (%)', color: CHART_SERIES[0] },
                                                        { label: 'CPU KV Cache Usage (%)', color: CHART_SERIES[1] },
                                                        { label: 'NVMe KV Cache Usage (%)', color: CHART_SERIES[2] },
                                                    ]}
                                                />
                                            </>
                                        ) : (
                                            <EmptyState
                                                title="No KV cache tier usage series"
                                                message="VRAM, CPU, and NVMe KV cache usage gauges were not reported over time."
                                            />
                                        )}
                                    </ChartContainer>

                                    <ChartContainer
                                        className="min-w-0"
                                        title={showNixlSection ? 'KV Offload, Restore & NIXL Transfer Throughput' : 'KV Offload & Restore Throughput'}
                                        subtitle={showNixlSection
                                            ? 'KV offload, restore, and NIXL KV transfer throughput (MiB/s) over time'
                                            : 'KV offload and restore data movement throughput (MiB/s) over time'}
                                        help={showNixlSection
                                            ? 'KV-cache data movement throughput in MiB/s over elapsed time for tier offload, tier restore, and peer-to-peer NIXL transfers.'
                                            : 'KV-cache data movement throughput in MiB/s over elapsed time for tier offload and restore operations.'}
                                        helpFormula="Rates(t) = (ΔBytes(t) / Δt) / 2^20  (MiB/s)"
                                        helpAlign="end"
                                    >
                                        {hasOffloadThroughputSeries ? (
                                            <>
                                                <div className="h-52 w-full">
                                                    <ResponsiveContainer width="100%" height="100%">
                                                        <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                            <YAxis width={56} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'MiB/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                            <Tooltip content={<OffloadTooltip />} />
                                                            <Line type="monotone" dataKey="kv_offload_mbps" name="KV Offload (MiB/s)" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'kv_offload_mbps', CHART_SERIES[0])} isAnimationActive={false} />
                                                            <Line type="monotone" dataKey="kv_restore_mbps" name="KV Restore (MiB/s)" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'kv_restore_mbps', CHART_SERIES[1])} isAnimationActive={false} />
                                                            {showNixlSection && (
                                                                <Line type="monotone" dataKey="nixl_mbps" name="NIXL KV Transfer (MiB/s)" stroke={CHART_SERIES[3]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'nixl_mbps', CHART_SERIES[3])} isAnimationActive={false} />
                                                            )}
                                                        </LineChart>
                                                    </ResponsiveContainer>
                                                </div>
                                                <ChartLegend
                                                    className="mt-3"
                                                    entries={[
                                                        { label: 'KV Offload (MiB/s)', color: CHART_SERIES[0] },
                                                        { label: 'KV Restore (MiB/s)', color: CHART_SERIES[1] },
                                                        ...(showNixlSection ? [{ label: 'NIXL KV Transfer (MiB/s)', color: CHART_SERIES[3] }] : []),
                                                    ]}
                                                />
                                            </>
                                        ) : (
                                            <EmptyState
                                                title="No KV data movement throughput series"
                                                message={showNixlSection
                                                    ? 'KV offload, restore, and NIXL KV transfer throughput metrics were not reported over time.'
                                                    : 'KV offload and restore data movement throughput metrics were not reported over time.'}
                                            />
                                        )}
                                    </ChartContainer>
                                </div>
                            </Panel>
                        </section>
                    )}

                    {/* #s-evict: KV Eviction */}
                    {showSection('evict') && (
                        <section id="s-evict" className="space-y-4">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="amber" className="mb-0">KV Eviction · Block Churn, Residency &amp; Preemptions</SectionLabel>
                                <HelpTooltip
                                    label="KV eviction and preemptions help"
                                    align="start"
                                    iconSize={13}
                                    formula="Eviction_Rate = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction; Preemptions = max(0, P_end − P_start)"
                                >
                                    Monitors physical KV-cache block evictions, block residency lifetimes, reuse gaps, and serving engine request preemptions triggered when accelerator HBM KV-cache memory saturates.
                                </HelpTooltip>
                            </div>
                            <Panel className="space-y-4">
                                <div className="grid gap-4 sm:grid-cols-3">
                                    <StatCard
                                        compact
                                        icon={<Gauge className="h-4 w-4" />}
                                        title="Total Preemptions"
                                        help="Total running requests preempted (swapped or recomputed) by the inference engine due to KV-cache exhaustion during the run; ideally zero."
                                        helpFormula="Total_Preemptions = max(0, Preemptions(t_end) − Preemptions(t_start))"
                                        value={totalPreemptions !== null ? formatCount(totalPreemptions) : '0'}
                                        tone={totalPreemptions !== null && totalPreemptions > 0 ? 'warning' : 'success'}
                                        details={[{
                                            label: 'Peak KV Usage',
                                            value: formatNumber(kvPeakPct, '%'),
                                            help: 'Maximum HBM KV-cache usage (%) reached across the run; values near 100% drive preemptions.',
                                            formula: 'max_scrapes((Used_Device_KV_Blocks / Total_Device_KV_Blocks) × 100)',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Activity className="h-4 w-4" />}
                                        title="Peak Preemption Rate"
                                        help="Maximum per-second rate of inference engine request preemptions observed in any scrape interval (/s)."
                                        helpFormula="max_scrapes(ΔPreempted_Requests / Δt)"
                                        value={preemptRateMax !== null ? formatNumber(preemptRateMax, ' /s') : '0 /s'}
                                        details={[{
                                            label: 'Pressure Status',
                                            value: (totalPreemptions ?? 0) > 0 ? 'Evicting' : 'Nominal',
                                            help: 'Indicates whether KV-cache evictions occurred (Evicting) or memory remained unpressured (Nominal).',
                                            formula: 'Evicting iff Total_Preemptions > 0; else Nominal',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Database className="h-4 w-4" />}
                                        title="KV Cache Pressure"
                                        help="Overall memory-pressure verdict combining peak HBM KV usage (>=90%) and non-zero preemptions."
                                        helpFormula="Pressure_Detected iff Peak_KV_Usage ≥ 90% ∨ Total_Preemptions > 0; else Healthy"
                                        helpAlign="end"
                                        value={(kvPeakPct ?? 0) >= 90 || (totalPreemptions ?? 0) > 0 ? 'Pressure Detected' : 'Healthy'}
                                        tone={(kvPeakPct ?? 0) >= 90 || (totalPreemptions ?? 0) > 0 ? 'warning' : 'success'}
                                        details={[{
                                            label: 'Threshold',
                                            value: '90% HBM / >0 preempts',
                                            help: 'Diagnostic thresholds used to flag KV-cache saturation: 90% HBM usage or any preemption event.',
                                            formula: 'Trigger iff Peak_KV_Usage ≥ 90% ∨ Total_Preemptions > 0',
                                        }]}
                                    />
                                </div>

                                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                                    <StatCard
                                        compact
                                        icon={<Layers className="h-4 w-4" />}
                                        title="KV Block Eviction Rate"
                                        help="Peak and mean physical KV-cache block eviction rate from accelerator HBM (blocks/min), scaled by 1 / sample rate."
                                        helpFormula="Eviction_Rate (blocks/min) = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction"
                                        helpAlign="start"
                                        value={
                                            evictBlocksPeak !== null || evictBlocksMean !== null
                                                ? `${formatNumber(evictBlocksPeak)} / ${formatNumber(evictBlocksMean)} blocks/min`
                                                : '—'
                                        }
                                        details={[
                                            {
                                                label: 'Total Evicted',
                                                value: evictBlocksTotal !== null ? `${formatCount(evictBlocksTotal)} blocks` : '—',
                                                help: 'Estimated total physical KV-cache blocks evicted across the profiling window, scaled by 1 / sample rate.',
                                                formula: 'Total_Evicted = ∫ (Eviction_Rate(t) / 60) dt',
                                            },
                                            ...(kvCacheMetricsEnabled !== undefined || kvCacheMetricsSample !== null ? [{
                                                label: 'Sample Rate',
                                                value: kvCacheMetricsEnabled === false
                                                    ? 'Disabled'
                                                    : (kvCacheMetricsSample !== null ? `${kvCacheMetricsSample}` : '1.0'),
                                                help: 'Engine KV-cache metrics sampling fraction (0 < s ≤ 1) used to scale traced physical KV block counters.',
                                                formula: 'Scale_Multiplier = 1 / Sampling_Fraction',
                                            }] : []),
                                        ]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Clock3 className="h-4 w-4" />}
                                        title="KV Block Idle Time Before Eviction (P50 / P90)"
                                        help="Mean 50th and 90th percentile duration a cached KV block sat idle in accelerator HBM before eviction (s)."
                                        helpFormula="Mean_scrapes(HistogramQuantile(q, ΔKV_Block_Idle_Before_Evict_Buckets / Δt))  (q ∈ {0.50, 0.90})"
                                        helpAlign="start"
                                        value={
                                            idleEvictP50 !== null || idleEvictP90 !== null
                                                ? `${formatSeconds(idleEvictP50)} / ${formatSeconds(idleEvictP90)}`
                                                : '—'
                                        }
                                        details={[{
                                            label: 'P99',
                                            value: formatSeconds(idleEvictP99),
                                            help: 'Mean 99th percentile idle time before eviction across scrape intervals (s).',
                                            formula: 'Mean_scrapes(HistogramQuantile(0.99, ΔKV_Block_Idle_Before_Evict_Buckets / Δt))',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Clock3 className="h-4 w-4" />}
                                        title="KV Block Lifetime (P50 / P90)"
                                        help="Mean 50th and 90th percentile total duration a physical KV-cache block stays allocated in accelerator HBM until eviction (s)."
                                        helpFormula="Mean_scrapes(HistogramQuantile(q, ΔKV_Block_Lifetime_Buckets / Δt))  (q ∈ {0.50, 0.90})"
                                        helpAlign="start"
                                        value={
                                            blockLifetimeP50 !== null || blockLifetimeP90 !== null
                                                ? `${formatSeconds(blockLifetimeP50)} / ${formatSeconds(blockLifetimeP90)}`
                                                : '—'
                                        }
                                        details={[{
                                            label: 'P99',
                                            value: formatSeconds(blockLifetimeP99),
                                            help: 'Mean 99th percentile allocated KV block lifetime across scrape intervals (s).',
                                            formula: 'Mean_scrapes(HistogramQuantile(0.99, ΔKV_Block_Lifetime_Buckets / Δt))',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Activity className="h-4 w-4" />}
                                        title="KV Block Reuse Gap (P50 / P90)"
                                        help="Mean 50th and 90th percentile elapsed time between two prefix-cache hits on the same physical KV block (s)."
                                        helpFormula="Mean_scrapes(HistogramQuantile(q, ΔKV_Block_Reuse_Gap_Buckets / Δt))  (q ∈ {0.50, 0.90})"
                                        helpAlign="end"
                                        value={
                                            reuseGapP50 !== null || reuseGapP90 !== null
                                                ? `${formatSeconds(reuseGapP50)} / ${formatSeconds(reuseGapP90)}`
                                                : '—'
                                        }
                                        details={[{
                                            label: 'P99',
                                            value: formatSeconds(reuseGapP99),
                                            help: 'Mean 99th percentile time between consecutive reuses of a cached KV block (s).',
                                            formula: 'Mean_scrapes(HistogramQuantile(0.99, ΔKV_Block_Reuse_Gap_Buckets / Δt))',
                                        }]}
                                    />
                                </div>

                                {(hasKvEvictBlocksSeries || hasKvResidencySeries) ? (
                                    <div className="grid gap-6 xl:grid-cols-2">
                                        <ChartContainer
                                            title="KV Block Eviction Rate (blocks/min)"
                                            subtitle="Physical KV-cache block eviction throughput (scaled by 1 / sample rate)"
                                            help="Physical KV-cache blocks evicted per minute (blocks/min) over elapsed time. Elevated churn alongside short idle times indicates cache capacity pressure."
                                            helpFormula="Eviction_Rate(t) = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction"
                                            helpAlign="start"
                                        >
                                            {hasKvEvictBlocksSeries ? (
                                                <div className="h-48 w-full">
                                                    <ResponsiveContainer width="100%" height="100%">
                                                        <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                            <YAxis width={56} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'blocks/min', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                            <Tooltip content={<BlocksPerMinTooltip />} />
                                                            <Line type="monotone" dataKey="kv_blocks_evicted_per_min" name="Blocks Evicted/min" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'kv_blocks_evicted_per_min', CHART_SERIES[2])} isAnimationActive={false} />
                                                        </LineChart>
                                                    </ResponsiveContainer>
                                                </div>
                                            ) : (
                                                <EmptyState title="No KV block eviction rate series" message="vllm:kv_block_idle_before_evict_seconds_count rate was not sampled over time." />
                                            )}
                                        </ChartContainer>

                                        {hasKvResidencySeries && kvResidencyCharts.map((chart) => (
                                            <ChartContainer
                                                key={chart.key}
                                                title={chart.title}
                                                subtitle={chart.subtitle}
                                                help={chart.help}
                                                helpFormula={chart.helpFormula}
                                                helpAlign={chart.helpAlign}
                                            >
                                                {chart.hasSeries ? (
                                                    <>
                                                        <div className="h-48 w-full">
                                                            <ResponsiveContainer width="100%" height="100%">
                                                                <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                                    <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                                    <YAxis width={56} allowDecimals tickFormatter={formatMsAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 's', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                                    <Tooltip content={<SecTooltip />} />
                                                                    <Line type="monotone" dataKey={chart.p50Key} name={chart.p50Label} stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, chart.p50Key, CHART_SERIES[0])} isAnimationActive={false} />
                                                                    <Line type="monotone" dataKey={chart.p90Key} name={chart.p90Label} stroke={CHART_SERIES[1]} strokeWidth={1.5} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, chart.p90Key, CHART_SERIES[1])} isAnimationActive={false} />
                                                                </LineChart>
                                                            </ResponsiveContainer>
                                                        </div>
                                                        <ChartLegend
                                                            className="mt-3"
                                                            entries={[
                                                                { label: `${chart.p50Label} (s)`, color: CHART_SERIES[0] },
                                                                { label: `${chart.p90Label} (s)`, color: CHART_SERIES[1] },
                                                            ]}
                                                        />
                                                    </>
                                                ) : (
                                                    <EmptyState title={chart.emptyTitle} message={chart.emptyMessage} />
                                                )}
                                            </ChartContainer>
                                        ))}
                                    </div>
                                ) : (
                                    <ChartContainer
                                        title="KV Block Eviction Rate (blocks/min)"
                                        subtitle="Traced physical KV block evictions and residency histograms (vllm:kv_block_*)"
                                        help="Tracks physical KV-cache block eviction throughput (blocks/min) and block residency histograms when engine block-level KV metrics are enabled."
                                        helpFormula="Eviction_Rate = (ΔEvicted_KV_Blocks / Δt_s) × 60 ÷ Sampling_Fraction; Residency = HistogramQuantile(q, ΔBlock_Buckets / Δt)"
                                        helpAlign="start"
                                    >
                                        <EmptyState
                                            title="Physical KV block eviction & residency metrics unavailable"
                                            message="vllm:kv_block_idle_before_evict_seconds, vllm:kv_block_lifetime_seconds, and vllm:kv_block_reuse_gap_seconds were not exposed by this deployment. Enable --kv-cache-metrics (and optional --kv-cache-metrics-sample 0.01) on vLLM to trace block churn and residency."
                                        />
                                    </ChartContainer>
                                )}

                                {hasPreemptSeries ? (
                                    <ChartContainer
                                        title="Request Preemption Rate over Time"
                                        subtitle="Per-second preemption rate (/s)"
                                        help="Per-second rate of inference engine request preemptions (/s) over elapsed time. Spikes pinpoint when KV exhaustion forced active sequences to pause or recompute."
                                        helpFormula="Preemption_Rate(t) = ΔPreempted_Requests / Δt"
                                        helpAlign="start"
                                    >
                                        <div className="h-48 w-full">
                                            <ResponsiveContainer width="100%" height="100%">
                                                <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                    <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                    <YAxis width={52} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                    <Tooltip content={<RatePerSecTooltip />} />
                                                    <Line type="monotone" dataKey="preemptions_rate" name="Preemptions/s" stroke={CHART_SERIES[4]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'preemptions_rate', CHART_SERIES[4])} isAnimationActive={false} />
                                                </LineChart>
                                            </ResponsiveContainer>
                                        </div>
                                    </ChartContainer>
                                ) : (
                                    <EmptyState title="No preemption series" message="vLLM preemption counters were not sampled over time." />
                                )}
                            </Panel>
                        </section>
                    )}

                    {/* #s-power: Accelerator / GPU Power (Profile-Driven) */}
                    {showSection('power') && (
                        <section id="s-power" className="space-y-4">
                            <div className="mb-2 flex items-center gap-1.5">
                                <SectionLabel tone="emerald" className="mb-0">{accelLabel} Power, Utilization &amp; Memory</SectionLabel>
                                <HelpTooltip
                                    label={`${accelLabel} power and utilization help`}
                                    align="start"
                                    iconSize={13}
                                    formula="Energy_Wh = (1/3600) Σ 0.5×(P_i + P_{i+1})×Δt_i; Efficiency = Total_Output_Tokens / Energy_Wh"
                                >
                                    Profile-driven hardware telemetry for serving accelerators: instantaneous power draw, cumulative energy efficiency, compute utilization, device memory, and thermals.
                                </HelpTooltip>
                            </div>
                            <Panel
                                title={`${accelLabel} Telemetry`}
                                help={`Aggregated device-level metrics collected via the active ${accelLabel} hardware profile across serving pods.`}
                                helpFormula="Aggregated across serving accelerator devices: mean/max(Power, Util, Mem, Temp)"
                                helpAlign="start"
                                actions={hwProfile?.display_name && <Badge tone="info" size="sm">{hwProfile.display_name}</Badge>}
                                className="space-y-4"
                            >
                                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                                    <StatCard
                                        compact
                                        icon={<Zap className="h-4 w-4" />}
                                        title={`${accelLabel} Power`}
                                        help={`Mean instantaneous power draw per serving ${accelLabel} device (W) during the run.`}
                                        helpFormula="Mean_scrapes(Device_Power_Draw_W(t))"
                                        value={accelPowerMean !== null ? formatWatts(accelPowerMean) : 'Unavailable'}
                                        details={[
                                            {
                                                label: 'Peak Power',
                                                value: formatWatts(accelPowerMax),
                                                help: `Maximum instantaneous ${accelLabel} power draw observed in any scrape interval (W).`,
                                                formula: 'max_scrapes(Device_Power_Draw_W(t))',
                                            },
                                            {
                                                label: 'Energy / Eff.',
                                                value: accelEnergyWh !== null ? `${formatEnergyWh(accelEnergyWh)}${accelTokPerWh !== null ? ` · ${formatNumber(accelTokPerWh)} tok/Wh` : ''}` : '—',
                                                help: `Total ${accelLabel} energy consumed (Wh) and output tokens generated per watt-hour (tok/Wh); higher tok/Wh means better efficiency.`,
                                                formula: 'Energy_Wh = (1/3600) Σ 0.5×(P_i + P_{i+1})×Δt_i; Efficiency = Total_Output_Tokens / Energy_Wh',
                                            },
                                        ]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Cpu className="h-4 w-4" />}
                                        title={`${accelLabel} Utilization`}
                                        help={`Mean compute utilization (%) across allocated serving ${accelLabel} devices; higher values indicate compute-bound saturation.`}
                                        helpFormula="Mean_scrapes(Device_Compute_Utilization_%)"
                                        value={accelUtilMean !== null ? formatNumber(accelUtilMean, '%') : 'Unavailable'}
                                        details={[{
                                            label: 'Peak',
                                            value: formatNumber(accelUtilMax, '%'),
                                            help: `Maximum ${accelLabel} compute utilization (%) observed in any scrape interval.`,
                                            formula: 'max_scrapes(Device_Compute_Utilization_%)',
                                        }]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Database className="h-4 w-4" />}
                                        title={`${accelLabel} Memory`}
                                        help={`Mean device memory (HBM/VRAM) actively allocated on serving ${accelLabel} devices for weights, activations, and KV cache.`}
                                        helpFormula="Mean_scrapes(Used_Device_Memory_Bytes)"
                                        value={accelMemUsedBytes !== null ? (formatBytes(accelMemUsedBytes) || '—') : 'Unavailable'}
                                        details={[
                                            {
                                                label: 'Total Capacity',
                                                value: accelMemTotalBytes !== null ? (formatBytes(accelMemTotalBytes) || '—') : '—',
                                                help: `Total physical memory capacity reported for the allocated ${accelLabel} device.`,
                                                formula: 'Total_Device_Memory = Used_Device_Mem + Free_Device_Mem',
                                            },
                                            {
                                                label: 'Mem Bandwidth',
                                                value: accelMemBwMean !== null ? `${formatNumber(accelMemBwMean, '%')} (peak ${formatNumber(accelMemBwMax, '%')})` : '—',
                                                help: `Mean and peak ${accelLabel} memory-controller bandwidth utilization (%); high values are typical during memory-bound decode.`,
                                                formula: 'Mean_scrapes / max_scrapes(Device_Memory_Bandwidth_Utilization_%)',
                                            },
                                        ]}
                                    />
                                    <StatCard
                                        compact
                                        icon={<Thermometer className="h-4 w-4" />}
                                        title={`${accelLabel} Temperature`}
                                        help={`Mean operating temperature (°C) reported by serving ${accelLabel} sensors; sustained high temperatures can cause thermal throttling.`}
                                        helpFormula="Mean_scrapes(Device_Temperature_C(t))"
                                        helpAlign="end"
                                        value={accelTempMean !== null ? formatNumber(accelTempMean, ' °C') : 'Unavailable'}
                                        details={[
                                            {
                                                label: 'Peak Temp',
                                                value: accelTempMax !== null ? formatNumber(accelTempMax, ' °C') : '—',
                                                help: `Maximum ${accelLabel} temperature (°C) recorded during the run.`,
                                                formula: 'max_scrapes(Device_Temperature_C(t))',
                                            },
                                            {
                                                label: 'Tracked Pods',
                                                value: formatCount(perPod.length),
                                                help: 'Count of serving pods included in per-pod telemetry aggregation (excluding benchmark runner pods).',
                                                formula: 'Count(Serving_Pods \\ Benchmark_Runner_Pods)',
                                            },
                                        ]}
                                    />
                                </div>

                                <div className="grid gap-6 xl:grid-cols-2">
                                    <ChartContainer
                                        title={`Serving ${accelLabel}s · Power (W)`}
                                        subtitle="Device power draw over time"
                                        help={`Device power draw (W) over elapsed time across serving ${accelLabel}s, tracking active prefill/decode load against idle baseline.`}
                                        helpFormula="Power(t) = Mean_devices(Instantaneous_Device_Power_W(t))"
                                        helpAlign="start"
                                    >
                                        {hasPowerSeries ? (
                                            <div className="h-52 w-full">
                                                <ResponsiveContainer width="100%" height="100%">
                                                    <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                        <YAxis width={56} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'W', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                        <Tooltip content={<WattsTooltip />} />
                                                        <Line type="monotone" dataKey="accel_power_w" name={`${accelLabel} Power (W)`} stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'accel_power_w', CHART_SERIES[2])} isAnimationActive={false} />
                                                    </LineChart>
                                                </ResponsiveContainer>
                                            </div>
                                        ) : (
                                            <EmptyState icon={<Zap className="h-6 w-6" />} title={`No ${accelLabel.toLowerCase()} power series`} message="Device power telemetry series were not reported for this run." />
                                        )}
                                    </ChartContainer>

                                    <ChartContainer
                                        title={`Serving ${accelLabel}s · Utilization & Memory Bandwidth`}
                                        subtitle="Compute utilization % and HBM bandwidth utilization %"
                                        help={`Serving ${accelLabel} compute utilization (%) and HBM memory bandwidth utilization (%) over elapsed time to distinguish compute-bound prefill from bandwidth-bound decode.`}
                                        helpFormula="Compute_Util(t) = Mean_devices(Compute_Busy_%); Mem_BW(t) = Mean_devices(HBM_BW_Util_%)"
                                        helpAlign="end"
                                    >
                                        {hasUtilBwSeries ? (
                                            <>
                                                <div className="h-52 w-full">
                                                    <ResponsiveContainer width="100%" height="100%">
                                                        <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                                            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                                            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                                            <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                                            <Tooltip content={<PctTooltip />} />
                                                            <Line type="monotone" dataKey="accel_util" name={`${accelLabel} Util %`} stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(series, 'accel_util', CHART_SERIES[0])} isAnimationActive={false} />
                                                            <Line type="monotone" dataKey="accel_mem_bw" name="Mem Bandwidth %" stroke={CHART_SERIES[1]} strokeWidth={1.75} strokeDasharray="4 3" connectNulls dot={sparseDotProp(series, 'accel_mem_bw', CHART_SERIES[1])} isAnimationActive={false} />
                                                        </LineChart>
                                                    </ResponsiveContainer>
                                                </div>
                                                <ChartLegend
                                                    className="mt-3"
                                                    entries={[
                                                        { label: `${accelLabel} Util %`, color: CHART_SERIES[0] },
                                                        { label: 'Mem Bandwidth %', color: CHART_SERIES[1] },
                                                    ]}
                                                />
                                            </>
                                        ) : (
                                            <EmptyState icon={<Cpu className="h-6 w-6" />} title={`No ${accelLabel.toLowerCase()} utilization series`} message="Device utilization and memory bandwidth series were not reported." />
                                        )}
                                    </ChartContainer>
                                </div>

                                {(allocatedDevices.length > 0 || Object.keys(metricSources).length > 0) && (
                                    <details className="rounded-lg border border-theme-border bg-theme-bg/60 px-3.5 py-2.5 text-xs">
                                        <summary className="cursor-pointer font-semibold text-theme-text">
                                            Telemetry Sources &amp; Allocated Devices ({formatCount(allocatedDevices.length)} devices, {formatCount(Object.keys(metricSources).length)} sources)
                                        </summary>
                                        <div className="mt-3 space-y-3">
                                            {allocatedDevices.length > 0 && (
                                                <div className="overflow-x-auto lg:overflow-visible rounded border border-theme-border">
                                                    <table className="w-full text-left text-xs">
                                                        <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                                                            <tr>
                                                                <th className="px-3 py-1.5">
                                                                    <span className="inline-flex items-center gap-1">
                                                                        Pod
                                                                        <HelpTooltip label="Device Pod column help" align="start" iconSize={11}>
                                                                            Serving pod to which this hardware accelerator device is allocated.
                                                                        </HelpTooltip>
                                                                    </span>
                                                                </th>
                                                                <th className="px-3 py-1.5">
                                                                    <span className="inline-flex items-center gap-1">
                                                                        Node
                                                                        <HelpTooltip label="Device Node column help" align="center" iconSize={11}>
                                                                            Cluster node hosting the serving pod and physical accelerator device.
                                                                        </HelpTooltip>
                                                                    </span>
                                                                </th>
                                                                <th className="px-3 py-1.5">
                                                                    <span className="inline-flex items-center gap-1">
                                                                        PCI BDF / Device
                                                                        <HelpTooltip label="PCI BDF or Device column help" align="end" iconSize={11}>
                                                                            Hardware device identifier or PCI Bus-Device-Function (BDF) address used to scope telemetry queries.
                                                                        </HelpTooltip>
                                                                    </span>
                                                                </th>
                                                            </tr>
                                                        </thead>
                                                        <tbody className="divide-y divide-theme-border">
                                                            {allocatedDevices.map((dev, idx) => (
                                                                <tr key={`${dev.pod || 'dev'}-${dev.pci_bdf || idx}`} className="text-theme-text">
                                                                    <td className="px-3 py-1.5 font-mono">{dev.pod || '—'}</td>
                                                                    <td className="px-3 py-1.5 font-mono">{dev.node || '—'}</td>
                                                                    <td className="px-3 py-1.5 font-mono">{dev.pci_bdf || dev.device || '—'}</td>
                                                                </tr>
                                                            ))}
                                                        </tbody>
                                                    </table>
                                                </div>
                                            )}
                                            {Object.keys(metricSources).length > 0 && (
                                                <div className="grid gap-1.5 sm:grid-cols-2">
                                                    {Object.entries(metricSources).map(([metricKey, srcLabel]) => (
                                                        <div key={metricKey} className="flex items-center justify-between gap-2 rounded border border-theme-border bg-theme-card px-2.5 py-1.5">
                                                            <span className="font-mono text-[11px] text-theme-text">{metricKey}</span>
                                                            <span className="truncate text-[11px] text-theme-muted">{String(srcLabel)}</span>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    </details>
                                )}
                            </Panel>
                        </section>
                    )}
                </>
            )}

            {/* #s-grafana: Grafana Deep Links */}
            {showSection('grafana') && (
                <section id="s-grafana">
                    <Panel
                        title="Grafana Deep Links"
                        help="External Grafana dashboards pre-scoped to this simulation run's execution window, namespace, and deployment for deeper cluster inspection."
                        helpAlign="start"
                        subtitle="Cluster Grafana dashboards scoped to this simulation run"
                    >
                        {grafanaLinks.length > 0 ? (
                            <div className="flex flex-wrap gap-3">
                                {grafanaLinks.map((link, idx) => (
                                    <a
                                        key={`${link.url}-${idx}`}
                                        href={link.url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="inline-flex items-center gap-2 rounded-lg border border-theme-border bg-theme-card px-3.5 py-2 text-xs font-semibold text-theme-text transition-colors hover:border-emerald-500/50 hover:text-emerald-600 dark:hover:text-emerald-300"
                                    >
                                        <span>{link.label || link.title || `Dashboard ${idx + 1}`}</span>
                                        <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                                    </a>
                                ))}
                            </div>
                        ) : (
                            <p className="text-xs text-theme-muted">
                                Grafana deep links are unavailable for this target (no cluster Grafana URL configured or external endpoint).
                            </p>
                        )}
                    </Panel>
                </section>
            )}
        </div>
    );
}

export default ServingTab;
