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

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    Area,
    CartesianGrid,
    ComposedChart,
    Line,
    ReferenceLine,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import {
    Activity,
    AlertTriangle,
    Check,
    ChevronDown,
    ChevronLeft,
    ChevronRight,
    ChevronUp,
    Copy,
    ExternalLink,
    FileText,
    Search,
    Square,
    Terminal,
} from 'lucide-react';
import {
    Badge,
    Button,
    ChartContainer,
    ChartLegend,
    ChartTooltip,
    ChartTooltipRow,
    CHART_SERIES,
    Checkbox,
    EmptyState,
    HelpTooltip,
    Input,
    Modal,
    Panel,
    RefreshButton,
    Select,
    Spinner,
    StatCard,
    StatusChip,
    ToggleGroup,
} from '../../../../shared/ui';
import { cn } from '../../../../shared/lib/cn';
import { useClipboard } from '../../../../shared/lib/useClipboard';
import { usePolling } from '../../../../shared/lib/usePolling';
import {
    DeploymentProfilingPanel,
    getDeploymentMonitoring,
    manageDeploymentMonitoring,
} from '../../../clusters';
import { getResponseCodeIssues } from '../../model/client';
import {
    STOPPABLE_STATUSES,
    alertSeverityProps,
    backendOptionLabel,
    backendOptionValue,
    clampPercent,
    displayName,
    formatCount,
    formatDecimal,
    formatDriftMs,
    formatDuration,
    formatDurationSeconds,
    formatErrorCategory,
    formatMilliseconds,
    formatNumber,
    formatPhase,
    formatWatts,
    numberAt,
    responseStatusGuidance,
    scenarioDisplayName,
    taskBackend,
    taskDataset,
} from '../../model/presentation';
import { ConfigurationGroup } from './ConfigurationGroup';
import { resolveConfiguredConcurrency } from './OverviewTab';

const ERROR_INSPECTOR_PAGE_SIZE = 50;
const EMPTY_ARRAY = [];
const LINE_TOOLTIP_CURSOR = { stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' };
const ANSI_PATTERN = new RegExp(`${String.fromCharCode(27)}\\[[0-9;]*[A-Za-z]`, 'g');

const EXPLICIT_LOG_LEVEL_PATTERNS = [
    // Time-first (AIPerf stdout / rich)
    /^\s*\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\b/,
    // ISO/date-time dash format (AIPerf aiperf.log / Python logging)
    /^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s*[+-]\d{2}:?\d{2}|Z)?\s+-\s+\S+(?:\s+\S+)*?\s+-\s+(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+-/,
    // ISO/date-time space/bracket format
    /^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s*[+-]\d{2}:?\d{2}|Z)?\s+\[?(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\]?\b/,
    // Bracketed/colon/uvicorn uppercase prefix
    /^\s*\[?(DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\]?(?:\s*:|\s+-|\s{2,})/,
    // Bracketed case-insensitive lifecycle tag (e.g. [error], [warn], [info])
    /^\s*\[(debug|trace|info|notice|warning|warn|error|critical|fatal)\](?:\s|$|:)/i,
    // Logfmt level=... (require leading boundary so flags like --log-level=error never match)
    /(?:^|[\s,{])level=["']?(debug|trace|info|notice|warn|warning|error|critical|fatal)["']?\b/i,
];

const TRACEBACK_START_PATTERN = /Traceback \(most recent call last\):/;
const TRACEBACK_INDENTED_PATTERN = /^\s+\S/;
const TRACEBACK_CHAIN_PATTERN = /^\s*(?:During handling of the above exception, another exception occurred:|The above exception was the direct cause of the following exception:)\s*$/;
const TRACEBACK_EXCEPTION_PATTERN = /^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)\b/;

const LOG_ZERO_SUMMARY_PATTERN = /^\s*(?:[│|]\s*)?(?:(?:total\s+)?(?:error(?:s|\s+records?|\s+count|\s+rate)?|warning(?:s|\s+count)?|failed(?:\s+requests?)?)\s*[│|:=][│|:=\s]*0(?:\.0+)?%?|0\s+(?:errors?|warnings?|failed(?:\s+requests?)?|error\s+records?))(?=$|[\s│|])(?:\s*[│|])*\s*$/i;
const LOG_CLI_CONFIG_PATTERN = /^\s*(?:CLI Command\s*:|(?:--)?(?:error_rate_slo|request_timeout|early_stop_error_threshold)\s*[:=])/i;
const LOG_WARNING_UNSTRUCTURED_PATTERN = /^\s*(?:Warning|[A-Za-z_]\w*Warning)\s*:/i;
const LOG_ERROR_UNSTRUCTURED_PATTERNS = [
    TRACEBACK_START_PATTERN,
    TRACEBACK_CHAIN_PATTERN,
    /(?:^|[\s;|])(?:Error|Exception|[A-Za-z_][\w.]*(?:Error|Exception))\s*:/,
    /\b(?:FATAL|CRITICAL|Segmentation fault|Killed|OOMKilled)\b/,
    /\b(?:connection refused|connection reset|timed out|read timeout|connect timeout|request timed out)\b/i,
    /\bexited with (?:non-zero )?(?:code|status)\s+[1-9]\d*\b/i,
    /\bHTTP\/\d(?:\.\d)?["'\s]+[45]\d\d\b/i,
    /\bstatus(?:_code)?\s*[=:]\s*[45]\d\d\b/i,
    /^\s*Task failed\b/i,
    /\b(?:subprocess|runner|benchmark)\s+failed\b/i,
];

const LIVE_BACKEND_OPTION_HELP = {
    warmup_seconds: {
        text: 'Initial replay seconds excluded from steady-state KPIs while caches and queues warm up.',
        formula: 'Profiling_Window = [Warmup_s, Run_Duration_s + Grace_Period_s]',
    },
    max_concurrency: {
        text: 'Maximum number of simultaneous in-flight requests allowed during replay.',
        formula: 'Active_InFlight_Slots ≤ Max_Concurrency',
    },
    concurrency: {
        text: 'Maximum number of simultaneous in-flight requests allowed by the backend.',
        formula: 'Active_InFlight_Slots ≤ Concurrency',
    },
    random_seed: 'Deterministic PRNG seed (integer ≥ 0, default 666) controlling prompt generation or sampling.',
    num_profile_runs: 'Number of consecutive profiling iterations configured for this task.',
    profile_run_cooldown_seconds: {
        text: 'Cooldown pause in seconds between repeated profiling runs.',
        formula: 'Total_Elapsed_s ≈ num_runs × Duration_s + (num_runs − 1) × Cooldown_s',
    },
    max_context_length: {
        text: 'Upper token-length limit enforced across prompt plus output tokens.',
        formula: 'Prompt_Tokens + Output_Tokens ≤ Max_Context_Length',
    },
    trace_idle_gap_cap_seconds: {
        text: 'Maximum idle gap (s) preserved between trace bursts before clamping waits.',
        formula: 'Effective_Idle_Gap_s = min(Source_Δt_s, Idle_Gap_Cap_s)',
    },
    collect_server_telemetry: 'Whether Prometheus serving engine and accelerator hardware metrics are collected.',
    e2e_slo: {
        text: 'End-to-end request latency Service Level Objective threshold in seconds.',
        formula: 'Pass iff Request_E2E_ms ≤ E2E_SLO_s × 1000',
    },
};

function liveBackendOptionHelp(key) {
    return LIVE_BACKEND_OPTION_HELP[key] || `Backend-specific execution parameter (${displayName(key)}) active for this run.`;
}

// eslint-disable-next-line react-refresh/only-export-components
export function stripAnsi(line) {
    return String(line ?? '').replace(ANSI_PATTERN, '');
}

function normalizeRawLogLine(rawLine) {
    return stripAnsi(typeof rawLine === 'string' || rawLine == null ? rawLine : JSON.stringify(rawLine));
}

function matchExplicitLogLevel(text) {
    for (const pattern of EXPLICIT_LOG_LEVEL_PATTERNS) {
        const match = pattern.exec(text);
        if (match) {
            const level = String(match[1] || '').toUpperCase();
            if (level === 'ERROR' || level === 'CRITICAL' || level === 'FATAL') return 'error';
            if (level === 'WARNING' || level === 'WARN') return 'warning';
            return 'info';
        }
    }
    return null;
}

function classifyUnstructuredLogLine(text) {
    if (LOG_ZERO_SUMMARY_PATTERN.test(text) || LOG_CLI_CONFIG_PATTERN.test(text)) {
        return 'info';
    }
    if (LOG_WARNING_UNSTRUCTURED_PATTERN.test(text)) {
        return 'warning';
    }
    for (const pattern of LOG_ERROR_UNSTRUCTURED_PATTERNS) {
        if (pattern.test(text)) {
            return 'error';
        }
    }
    return 'info';
}

// eslint-disable-next-line react-refresh/only-export-components
export function isLogErrorOrWarning(severity) {
    return severity === 'error' || severity === 'warning';
}

// eslint-disable-next-line react-refresh/only-export-components
export function classifyLogLine(rawLine) {
    const text = normalizeRawLogLine(rawLine);
    if (LOG_CLI_CONFIG_PATTERN.test(text)) {
        return 'info';
    }
    const explicitSeverity = matchExplicitLogLevel(text);
    if (explicitSeverity !== null) {
        if (explicitSeverity !== 'info' && TRACEBACK_START_PATTERN.test(text)) {
            return 'error';
        }
        return explicitSeverity;
    }
    return classifyUnstructuredLogLine(text);
}

// eslint-disable-next-line react-refresh/only-export-components
export function classifyLogLines(lines) {
    if (!Array.isArray(lines)) return [];
    let inTraceback = false;
    return lines.map((rawLine) => {
        const text = normalizeRawLogLine(rawLine);
        const explicitSeverity = matchExplicitLogLevel(text);

        if (inTraceback) {
            if (explicitSeverity !== null) {
                inTraceback = false;
                if (LOG_CLI_CONFIG_PATTERN.test(text)) {
                    return { text, severity: 'info' };
                }
                if (explicitSeverity !== 'info' && TRACEBACK_START_PATTERN.test(text)) {
                    inTraceback = true;
                    return { text, severity: 'error' };
                }
                return { text, severity: explicitSeverity };
            }
            if (TRACEBACK_INDENTED_PATTERN.test(text) || TRACEBACK_CHAIN_PATTERN.test(text)) {
                return { text, severity: 'error' };
            }
            if (TRACEBACK_EXCEPTION_PATTERN.test(text)) {
                inTraceback = false;
                return { text, severity: 'error' };
            }
            inTraceback = false;
        }

        if (LOG_CLI_CONFIG_PATTERN.test(text)) {
            return { text, severity: 'info' };
        }

        if (explicitSeverity !== null) {
            if (explicitSeverity !== 'info' && TRACEBACK_START_PATTERN.test(text)) {
                inTraceback = true;
                return { text, severity: 'error' };
            }
            return { text, severity: explicitSeverity };
        }

        if (TRACEBACK_START_PATTERN.test(text)) {
            inTraceback = true;
            return { text, severity: 'error' };
        }

        return { text, severity: classifyUnstructuredLogLine(text) };
    });
}

function formatWallClockOffset(taskStartedAt, offsetSeconds) {
    if (!taskStartedAt || offsetSeconds === null || offsetSeconds === undefined) return null;
    const baseMs = Date.parse(taskStartedAt);
    const offset = Number(offsetSeconds);
    if (!Number.isFinite(baseMs) || !Number.isFinite(offset)) return null;
    return new Date(baseMs + offset * 1000).toLocaleTimeString();
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

function hasFiniteSeriesValue(data, keys) {
    if (!Array.isArray(data) || !data.length) return false;
    return data.some((row) => keys.some((key) => {
        const val = row?.[key];
        return val !== null && val !== undefined && val !== '' && Number.isFinite(Number(val));
    }));
}

function LiveChartTooltip({ active, payload, label, units = {} }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((entry) => entry.value !== null && entry.value !== undefined);
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)}s`}>
            {rows.map((entry) => {
                const unit = units[entry.dataKey] || '';
                const formattedValue = unit === ' ms'
                    ? formatMilliseconds(entry.value)
                    : unit === ' ms/tok'
                        ? (() => {
                            const msText = formatMilliseconds(entry.value);
                            return msText === '—' ? '—' : `${msText}/tok`;
                        })()
                        : formatNumber(entry.value);
                const displayUnit = unit === ' ms' || unit === ' ms/tok' ? '' : unit;
                return (
                    <ChartTooltipRow
                        key={entry.dataKey}
                        color={entry.color}
                        label={entry.name}
                        value={formattedValue}
                        unit={displayUnit}
                    />
                );
            })}
        </ChartTooltip>
    );
}

export function ErrorInspectorModal({
    isOpen,
    onClose,
    taskId,
    initialFilter = 'all',
    isRunning = false,
    taskStartedAt = null,
}) {
    const [filterCode, setFilterCode] = useState(initialFilter || 'all');
    const [page, setPage] = useState(0);
    const [autoRefresh, setAutoRefresh] = useState(Boolean(isRunning));
    const [issues, setIssues] = useState([]);
    const [total, setTotal] = useState(0);
    const [countsByCode, setCountsByCode] = useState({});
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const { copiedValue, copy } = useClipboard(1500);
    const requestSeq = useRef(0);

    useEffect(() => {
        if (!isOpen) return;
        setFilterCode(initialFilter || 'all');
        setPage(0);
        setAutoRefresh(Boolean(isRunning));
    }, [initialFilter, isOpen, isRunning]);

    const loadIssues = useCallback(async ({ quiet = false } = {}) => {
        if (!taskId || !isOpen) return;
        const seq = ++requestSeq.current;
        if (!quiet) {
            setLoading(true);
            setError('');
        }
        try {
            const payload = await getResponseCodeIssues(taskId, {
                statusCode: filterCode,
                offset: page * ERROR_INSPECTOR_PAGE_SIZE,
                limit: ERROR_INSPECTOR_PAGE_SIZE,
                sort: 'newest',
            });
            if (seq !== requestSeq.current) return;
            setIssues(Array.isArray(payload?.issues) ? payload.issues : []);
            setTotal(Number(payload?.total ?? 0));
            setCountsByCode(payload?.counts_by_code && typeof payload.counts_by_code === 'object'
                ? payload.counts_by_code
                : {});
            setError('');
        } catch (nextError) {
            if (seq !== requestSeq.current) return;
            setError(`Unable to load request errors: ${nextError.message}`);
        } finally {
            if (seq === requestSeq.current && !quiet) setLoading(false);
        }
    }, [filterCode, isOpen, page, taskId]);

    useEffect(() => {
        if (!isOpen) return;
        void loadIssues({ quiet: false });
    }, [isOpen, loadIssues]);

    usePolling(
        () => loadIssues({ quiet: true }),
        {
            intervalMs: 3000,
            enabled: Boolean(isOpen && taskId && isRunning && autoRefresh && page === 0),
        },
    );

    const filterPills = useMemo(() => {
        const baseKeys = ['all', '4xx', '5xx', 'timeout', 'transport'];
        const extraKeys = Object.keys(countsByCode)
            .filter((key) => !baseKeys.includes(key) && Number(countsByCode[key]) > 0)
            .sort((a, b) => {
                const aNum = Number(a);
                const bNum = Number(b);
                if (Number.isFinite(aNum) && Number.isFinite(bNum)) return aNum - bNum;
                return a.localeCompare(b);
            });
        const keys = [...baseKeys, ...extraKeys];
        if (filterCode && !keys.includes(filterCode)) keys.push(filterCode);
        return keys.map((key) => ({
            key,
            label: formatErrorCategory(key),
            count: countsByCode[key],
        }));
    }, [countsByCode, filterCode]);

    const startItem = total === 0 ? 0 : page * ERROR_INSPECTOR_PAGE_SIZE + 1;
    const endItem = Math.min(total, page * ERROR_INSPECTOR_PAGE_SIZE + issues.length);

    return (
        <Modal
            isOpen={isOpen}
            onClose={onClose}
            title="Live Error Inspector"
            subtitle={`${formatCount(total)} matching errors · ${formatErrorCategory(filterCode)}`}
            size="xl"
            className="simulation-error-inspector-modal"
        >
            <div className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter errors by category or HTTP status">
                        {filterPills.map((pill) => {
                            const active = filterCode === pill.key;
                            const pillTone = pill.key === '5xx' || pill.key === 'transport' || /^5\d\d$/.test(pill.key)
                                ? 'error'
                                : pill.key === '4xx' || pill.key === 'timeout' || /^4\d\d$/.test(pill.key)
                                    ? 'warn'
                                    : undefined;
                            return (
                                <button
                                    key={pill.key}
                                    type="button"
                                    aria-pressed={active}
                                    data-selected={active ? 'true' : 'false'}
                                    data-tone={pillTone}
                                    onClick={() => {
                                        setFilterCode(pill.key);
                                        setPage(0);
                                    }}
                                    className={cn(
                                        'simulation-filter-pill inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors',
                                        active
                                            ? 'border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300'
                                            : 'border-theme-border bg-theme-bg text-theme-muted hover:text-theme-text',
                                    )}
                                >
                                    <span>{pill.label}</span>
                                    {pill.count !== undefined && (
                                        <span className="font-mono text-[10px] opacity-80">({formatCount(pill.count)})</span>
                                    )}
                                </button>
                            );
                        })}
                        <HelpTooltip label="Error category filters help" align="start" iconSize={12}>
                            Filter failed request records by HTTP status class (4xx client, 5xx server), client timeout, transport failure, or specific HTTP code.
                        </HelpTooltip>
                    </div>
                    <div className="flex items-center gap-2">
                        {isRunning && (
                            <div className="flex items-center gap-2">
                                <Checkbox
                                    checked={autoRefresh}
                                    onChange={(event) => setAutoRefresh(event.target.checked)}
                                    label="Auto-refresh (3s)"
                                />
                                {autoRefresh && page > 0 && (
                                    <Badge tone="warning" size="xs">Paused on page {page + 1}</Badge>
                                )}
                            </div>
                        )}
                        <RefreshButton
                            onClick={() => void loadIssues({ quiet: false })}
                            refreshing={loading}
                            label="Refresh errors"
                        />
                    </div>
                </div>

                {error && (
                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-700 dark:text-red-300">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>{error}</span>
                    </div>
                )}

                {loading && issues.length === 0 ? (
                    <div className="flex items-center justify-center gap-2 py-12 text-xs text-theme-muted">
                        <Spinner /> Loading error records…
                    </div>
                ) : issues.length === 0 ? (
                    <EmptyState
                        icon={<AlertTriangle className="h-8 w-8" />}
                        title="No matching errors"
                        message="No failed requests matched the selected filter."
                    />
                ) : (
                    <>
                        <div className="max-lg:overflow-x-auto rounded-xl border border-theme-border">
                            <table className="w-full min-w-[640px] lg:min-w-0 lg:table-fixed text-left text-xs">
                                <colgroup>
                                    <col className="w-[15%]" />
                                    <col className="w-[11%]" />
                                    <col className="w-[13%]" />
                                    <col className="w-[32%]" />
                                    <col className="w-[12%]" />
                                    <col className="w-[17%]" />
                                </colgroup>
                                <thead className="border-b border-theme-border bg-theme-bg text-[10px] font-bold uppercase tracking-wider text-theme-muted">
                                    <tr>
                                        <th className="px-3 py-2.5">
                                            <span className="inline-flex items-center gap-1">
                                                <span>Request</span>
                                                <HelpTooltip label="Request column help" align="start" iconSize={11}>
                                                    1-based dispatch sequence number and optional client request ID for the failed request.
                                                </HelpTooltip>
                                            </span>
                                        </th>
                                        <th className="px-3 py-2.5">
                                            <span className="inline-flex items-center gap-1">
                                                <span>Time</span>
                                                <HelpTooltip label="Time column help" align="start" iconSize={11}>
                                                    Dispatch offset in seconds relative to run start and corresponding local wall-clock timestamp.
                                                </HelpTooltip>
                                            </span>
                                        </th>
                                        <th className="px-3 py-2.5">
                                            <span className="inline-flex items-center gap-1">
                                                <span>Status</span>
                                                <HelpTooltip label="Status column help" align="start" iconSize={11}>
                                                    HTTP status code or transport/timeout failure category returned by the endpoint or client.
                                                </HelpTooltip>
                                            </span>
                                        </th>
                                        <th className="px-3 py-2.5">
                                            <span className="inline-flex items-center gap-1">
                                                <span>Error Message</span>
                                                <HelpTooltip label="Error Message column help" align="start" iconSize={11}>
                                                    Sanitized error payload or diagnostic guidance with sensitive headers redacted.
                                                </HelpTooltip>
                                            </span>
                                        </th>
                                        <th className="px-3 py-2.5 text-right">
                                            <span className="inline-flex items-center justify-end gap-1">
                                                <span>Latency</span>
                                                <HelpTooltip label="Latency column help" align="end" iconSize={11} formula="Failure_Latency (ms) = (t_error_or_timeout − t_request_dispatch) × 1000">
                                                    Elapsed time (ms) from request dispatch until the error or timeout occurred.
                                                </HelpTooltip>
                                            </span>
                                        </th>
                                        <th className="px-3 py-2.5 text-right">
                                            <span className="inline-flex items-center justify-end gap-1">
                                                <span>Prompt / Output Tokens</span>
                                                <HelpTooltip label="Prompt / Output Tokens column help" align="end" iconSize={11}>
                                                    Prompt input tokens sent and partial output tokens received before the request failed.
                                                </HelpTooltip>
                                            </span>
                                        </th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-theme-border">
                                    {issues.map((issue, idx) => {
                                        const categoryKey = issue.status_code ?? issue.category ?? 'unknown';
                                        const messageText = issue.error || responseStatusGuidance(issue.status_code);
                                        const wallTime = formatWallClockOffset(taskStartedAt, issue.started_at_seconds);
                                        const isCopied = copiedValue === messageText;
                                        return (
                                            <tr key={`${issue.request_number ?? idx}-${issue.request_id || ''}`} className="align-top hover:bg-theme-bg/50">
                                                <td className="px-3 py-2.5 font-mono text-theme-text">
                                                    <div>#{issue.request_number ?? idx + 1}</div>
                                                    {issue.request_id !== null && issue.request_id !== undefined && issue.request_id !== '' && (
                                                        <div
                                                            className="mt-0.5 break-all text-[10px] text-theme-muted"
                                                            title={String(issue.request_id)}
                                                        >
                                                            {String(issue.request_id)}
                                                        </div>
                                                    )}
                                                </td>
                                                <td className="px-3 py-2.5 font-mono tabular-nums text-theme-muted">
                                                    <div>
                                                        {(() => {
                                                            const formattedOffset = formatDecimal(issue.started_at_seconds);
                                                            if (formattedOffset === '—') return '—';
                                                            const numericOffset = Number(issue.started_at_seconds);
                                                            const prefix = Math.abs(numericOffset) >= 0.005 && numericOffset > 0 ? '+' : '';
                                                            return `${prefix}${formattedOffset}s`;
                                                        })()}
                                                    </div>
                                                    {wallTime && <div className="mt-0.5 text-[10px] opacity-80">{wallTime}</div>}
                                                </td>
                                                <td className="px-3 py-2.5">
                                                    <Badge
                                                        tone="danger"
                                                        size="xs"
                                                        className="max-w-full whitespace-normal break-words text-left leading-tight"
                                                    >
                                                        {formatErrorCategory(categoryKey)}
                                                    </Badge>
                                                </td>
                                                <td className="px-3 py-2.5">
                                                    <div className="flex min-w-0 items-start justify-between gap-2">
                                                        <p className="min-w-0 flex-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-theme-text">
                                                            {messageText}
                                                        </p>
                                                        <Button
                                                            variant="ghost"
                                                            size="xs"
                                                            className="shrink-0"
                                                            onClick={() => void copy(messageText)}
                                                            aria-label="Copy error message"
                                                            title="Copy error message"
                                                        >
                                                            {isCopied ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
                                                        </Button>
                                                    </div>
                                                </td>
                                                <td className="px-3 py-2.5 text-right font-mono tabular-nums text-theme-muted">
                                                    {issue.latency_ms == null ? '—' : formatMilliseconds(issue.latency_ms)}
                                                </td>
                                                <td className="px-3 py-2.5 text-right font-mono tabular-nums text-theme-muted">
                                                    {issue.input_tokens == null ? '—' : formatCount(issue.input_tokens)}
                                                    {' / '}
                                                    {issue.output_tokens == null ? '—' : formatCount(issue.output_tokens)}
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                        <div className="flex items-center justify-between gap-3 text-xs text-theme-muted">
                            <span>
                                {formatCount(startItem)}–{formatCount(endItem)} of {formatCount(total)}
                            </span>
                            <div className="flex items-center gap-2">
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    disabled={page === 0}
                                    onClick={() => setPage((current) => Math.max(0, current - 1))}
                                >
                                    <ChevronLeft className="h-3.5 w-3.5" /> Previous
                                </Button>
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    disabled={(page + 1) * ERROR_INSPECTOR_PAGE_SIZE >= total}
                                    onClick={() => setPage((current) => current + 1)}
                                >
                                    Next <ChevronRight className="h-3.5 w-3.5" />
                                </Button>
                            </div>
                        </div>
                    </>
                )}
            </div>
        </Modal>
    );
}

function DeploymentFlowCard({ deployment, fallbackClusterId }) {
    const [monitoring, setMonitoring] = useState(null);
    const [error, setError] = useState('');
    const [enabling, setEnabling] = useState(false);
    const [revision, setRevision] = useState(0);
    const mounted = useRef(false);
    const executionId = deployment.execution_id;
    const clusterId = deployment.cluster_id || fallbackClusterId || undefined;

    useEffect(() => {
        mounted.current = true;
        return () => { mounted.current = false; };
    }, []);

    useEffect(() => {
        if (!executionId) return undefined;
        const controller = new AbortController();
        let timer = null;
        async function load() {
            try {
                const value = await getDeploymentMonitoring(executionId, { signal: controller.signal, clusterId });
                if (!controller.signal.aborted) {
                    setMonitoring(value);
                    setError('');
                }
            } catch (nextError) {
                if (!controller.signal.aborted) setError(nextError.message);
            }
            if (!controller.signal.aborted) {
                timer = window.setTimeout(load, 10000);
            }
        }
        void load();
        return () => {
            controller.abort();
            if (timer) window.clearTimeout(timer);
        };
    }, [clusterId, executionId, revision]);

    async function enable() {
        setEnabling(true);
        setError('');
        try {
            const value = await manageDeploymentMonitoring(executionId, 'enable', { clusterId });
            if (mounted.current) {
                setMonitoring(value);
                setRevision((current) => current + 1);
            }
        } catch (nextError) {
            if (mounted.current) setError(nextError.message);
        } finally {
            if (mounted.current) setEnabling(false);
        }
    }

    const enabled = monitoring?.enabled === true;
    const state = enabling
        ? 'Enabling monitoring'
        : error
            ? 'Monitoring check failed'
            : !monitoring
                ? 'Checking monitoring'
                : enabled
                    ? 'Monitoring enabled'
                    : monitoring.status === 'disabled'
                        ? 'Monitoring disabled'
                        : 'Monitoring unavailable';

    return (
        <div className="space-y-4">
            <Panel padding="sm">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <p role="status" className="flex items-center gap-2 text-xs text-theme-text">
                        <Activity className={cn('h-4 w-4', enabled && !error ? 'text-emerald-500' : 'text-amber-500')} />
                        <span>{state}</span>
                    </p>
                    <div className="flex items-center gap-2">
                        <RefreshButton
                            onClick={() => setRevision((current) => current + 1)}
                            refreshing={!monitoring && !error}
                            label="Refresh monitoring status"
                        />
                        {!enabled && (
                            <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => void enable()}
                                disabled={enabling || !monitoring}
                                isLoading={enabling}
                            >
                                Enable monitoring / retry
                            </Button>
                        )}
                    </div>
                </div>
                {!enabled && monitoring?.message && (
                    <p className="mt-2 text-xs text-theme-muted">{monitoring.message}</p>
                )}
                {monitoring?.stack_ready === false && (
                    <p className="mt-2 text-xs text-amber-600 dark:text-amber-300">
                        The cluster monitoring stack is not ready. Enable it in Cluster observability before retrying deployment monitoring.
                    </p>
                )}
                {error && (
                    <p role="alert" className="mt-2 text-xs text-red-600 dark:text-red-300">{error}</p>
                )}
            </Panel>
            {enabled && !enabling && (
                <DeploymentProfilingPanel
                    deployment={deployment}
                    clusterId={clusterId}
                    refreshMs={5000}
                />
            )}
        </div>
    );
}

function LiveDeploymentFlowTab({ targetDeployments, fallbackClusterId }) {
    const [selectedId, setSelectedId] = useState('');
    const selected = targetDeployments.find((item) => item.execution_id === selectedId) || targetDeployments[0] || null;

    if (!selected) {
        return (
            <EmptyState
                icon={<Activity className="h-8 w-8" />}
                title="Live Deployment Flow Unavailable"
                message="Live Deployment Flow requires a cluster-backed deployment or Model Service. Replay Monitor remains fully active for this run."
            />
        );
    }

    return (
        <section className="space-y-4" aria-label="Live deployment monitoring">
            <Panel padding="sm">
                <div className="flex flex-wrap items-end justify-between gap-4">
                    <div>
                        <h3 className="flex items-center gap-2 text-sm font-semibold text-theme-text">
                            <Activity className="h-4 w-4 text-emerald-500" />
                            <span>Live deployment flow</span>
                            <HelpTooltip label="Live deployment flow help" align="start" iconSize={12}>
                                Interactive serving topology graph for cluster-managed deployments, refreshing every 5 seconds with per-pod queue and accelerator metrics.
                            </HelpTooltip>
                        </h3>
                        <p className="mt-1 text-xs text-theme-muted">
                            Real-time serving topology flow · updates every 5s · click any node to inspect pod and accelerator telemetry.
                        </p>
                    </div>
                    {targetDeployments.length > 0 && (
                        <div className="min-w-[220px]">
                            <div className="mb-1 flex items-center gap-1">
                                <label htmlFor="simulation-live-flow-deployment" className="block text-[11px] font-semibold text-theme-muted">
                                    Deployment execution
                                </label>
                                <HelpTooltip label="Deployment execution help" align="end" iconSize={11}>
                                    Select which target deployment execution (e.g., prefill or decode pool) to inspect in the live topology view.
                                </HelpTooltip>
                            </div>
                            <Select
                                id="simulation-live-flow-deployment"
                                aria-label="Deployment execution"
                                value={selected.execution_id}
                                onChange={(event) => setSelectedId(event.target.value)}
                                className="h-9 text-xs"
                            >
                                {targetDeployments.map((item) => (
                                    <option key={item.execution_id} value={item.execution_id}>
                                        {item.name || item.guide || item.execution_id}
                                        {item.namespace ? ` · ${item.namespace}` : ''}
                                        {item.status ? ` · ${item.status}` : ''}
                                    </option>
                                ))}
                            </Select>
                        </div>
                    )}
                </div>
            </Panel>
            <DeploymentFlowCard
                key={selected.execution_id}
                deployment={selected}
                fallbackClusterId={fallbackClusterId}
            />
        </section>
    );
}

function LiveLogPanel({ logs, defaultExpanded = false }) {
    const [userExpanded, setUserExpanded] = useState(null);
    const [query, setQuery] = useState('');
    const [errorsOnly, setErrorsOnly] = useState(false);

    const cleanedLogs = useMemo(
        () => (Array.isArray(logs) ? logs : []).map((line) => stripAnsi(typeof line === 'string' ? line : JSON.stringify(line))),
        [logs],
    );
    const classifiedLogs = useMemo(
        () => classifyLogLines(cleanedLogs),
        [cleanedLogs],
    );
    const hasErrorLines = useMemo(
        () => classifiedLogs.some((entry) => entry.severity === 'error'),
        [classifiedLogs],
    );
    const hasWarningLines = useMemo(
        () => classifiedLogs.some((entry) => entry.severity === 'warning'),
        [classifiedLogs],
    );
    const expanded = userExpanded ?? (defaultExpanded || hasErrorLines);

    const filteredLogs = useMemo(() => {
        const needle = query.trim().toLowerCase();
        return classifiedLogs.filter((entry) => {
            if (errorsOnly && !isLogErrorOrWarning(entry.severity)) return false;
            if (needle && !entry.text.toLowerCase().includes(needle)) return false;
            return true;
        });
    }, [classifiedLogs, errorsOnly, query]);

    return (
        <Panel
            title="Runner & Task Log Tail"
            help="Streams recent stdout/stderr lines from the benchmark runner container and task lifecycle events. Warning and error lines are highlighted."
            actions={(
                <div className="flex items-center gap-2">
                    <Badge tone={hasErrorLines ? 'danger' : hasWarningLines ? 'warning' : 'neutral'} size="xs">
                        {formatCount(cleanedLogs.length)} lines
                    </Badge>
                    <Button
                        variant="ghost"
                        size="xs"
                        onClick={() => setUserExpanded(!expanded)}
                        aria-expanded={expanded}
                    >
                        {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                        {expanded ? 'Collapse' : 'Expand'}
                    </Button>
                </div>
            )}
        >
            {expanded ? (
                <div className="space-y-3">
                    <div className="flex flex-wrap items-center gap-3">
                        <div className="min-w-[200px] flex-1">
                            <Input
                                icon={Search}
                                value={query}
                                onChange={(event) => setQuery(event.target.value)}
                                placeholder="Filter log lines..."
                                aria-label="Filter log lines"
                                className="h-8 text-xs"
                            />
                        </div>
                        <Checkbox
                            checked={errorsOnly}
                            onChange={(event) => setErrorsOnly(event.target.checked)}
                            label="Errors & warnings only"
                        />
                    </div>
                    {filteredLogs.length > 0 ? (
                        <div className="max-h-72 overflow-auto rounded-lg border border-theme-border bg-theme-bg p-3 font-mono text-[11px] leading-relaxed text-theme-text">
                            {filteredLogs.map((entry, idx) => (
                                <div
                                    key={`${idx}-${entry.text.slice(0, 32)}`}
                                    className={cn(
                                        'whitespace-pre-wrap break-words py-0.5',
                                        entry.severity === 'error' && 'text-red-600 dark:text-red-300',
                                        entry.severity === 'warning' && 'text-amber-700 dark:text-amber-300',
                                    )}
                                >
                                    {entry.text}
                                </div>
                            ))}
                        </div>
                    ) : (
                        <p className="py-6 text-center text-xs text-theme-muted">
                            {cleanedLogs.length ? 'No log lines match the current filter.' : 'No log output has arrived yet.'}
                        </p>
                    )}
                </div>
            ) : (
                <p className="text-xs text-theme-muted">
                    Log tail collapsed ({formatCount(cleanedLogs.length)} lines). Expand to inspect or filter live runner output.
                </p>
            )}
        </Panel>
    );
}

function ServerFallbackNotice() {
    return (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <Badge tone="info" size="xs">Live engine telemetry</Badge>
            <span className="text-[11px] text-theme-muted">Client trace flushes on completion</span>
        </div>
    );
}

function LiveChartsGrid({
    series,
    warmupSeconds,
    ttftSloMs,
    tpotSloMs,
    prometheusAvailable,
    topologyMode,
    acceleratorLabel,
    clientTelemetrySource = 'none',
    configuredConcurrency = null,
    initialThroughputMode = 'rps',
}) {
    const [throughputMode, setThroughputMode] = useState(initialThroughputMode);
    const [latencyMode, setLatencyMode] = useState('ttft');
    const [pressureMode, setPressureMode] = useState('pct');

    const isServerFallback = clientTelemetrySource === 'server_fallback';
    const throughputData = Array.isArray(series?.client_throughput) ? series.client_throughput : EMPTY_ARRAY;
    const latencyData = Array.isArray(series?.rolling_latency) ? series.rolling_latency : [];
    const serverData = Array.isArray(series?.server_load) ? series.server_load : [];
    const kvData = (Array.isArray(series?.kv_and_accelerator) ? series.kv_and_accelerator : []).map((point) => ({
        ...point,
        kv_cache_peak_pct: clampPercent(point.kv_cache_peak_pct),
        prefix_cache_hit_pct: clampPercent(point.prefix_cache_hit_pct),
        accelerator_util_pct: clampPercent(point.accelerator_util_pct),
    }));

    const maxObservedLiveInflight = useMemo(() => {
        let maxVal = 0;
        for (const point of throughputData) {
            const raw = point?.in_flight;
            if (raw === null || raw === undefined || raw === '' || typeof raw === 'boolean') continue;
            const val = Number(raw);
            if (Number.isFinite(val)) maxVal = Math.max(maxVal, val);
        }
        return maxVal;
    }, [throughputData]);
    const showLiveConfiguredConcurrency = throughputMode === 'inflight' && configuredConcurrency !== null;
    const liveInflightYMax = showLiveConfiguredConcurrency
        ? Math.max(configuredConcurrency + 1, Math.ceil(Math.max(maxObservedLiveInflight, configuredConcurrency) * 1.12))
        : null;

    const isDisaggregated = topologyMode === 'disaggregated' || serverData.some(
        (point) => point.prefill_running != null || point.decode_running != null,
    );

    const serverLegend = isDisaggregated
        ? [
            { label: 'Prefill Running', color: CHART_SERIES[0] },
            { label: 'Prefill Waiting', color: CHART_SERIES[2] },
            { label: 'Decode Running', color: CHART_SERIES[1] },
            { label: 'Decode Waiting', color: CHART_SERIES[4] },
            { label: 'EPP In-Flight', color: CHART_SERIES[3] },
        ]
        : [
            { label: 'vLLM Running Requests', color: CHART_SERIES[0] },
            { label: 'vLLM Waiting Requests', color: CHART_SERIES[2] },
            { label: 'EPP In-Flight', color: CHART_SERIES[1] },
        ];

    const hasThroughputModeSamples = throughputMode === 'rps'
        ? hasFiniteSeriesValue(throughputData, ['rps', 'failed_rps'])
        : throughputMode === 'tok'
            ? hasFiniteSeriesValue(throughputData, ['output_tok_s'])
            : hasFiniteSeriesValue(throughputData, ['in_flight']);

    const latencySlo = latencyMode === 'ttft' ? ttftSloMs : latencyMode === 'tpot' ? tpotSloMs : null;
    const hasLatencyModeSamples = latencyMode === 'ttft'
        ? hasFiniteSeriesValue(latencyData, ['ttft_p50_ms', 'ttft_p90_ms'])
        : latencyMode === 'tpot'
            ? hasFiniteSeriesValue(latencyData, ['tpot_p50_ms', 'tpot_p90_ms'])
            : hasFiniteSeriesValue(latencyData, ['drift_p90_ms']);

    // On a zero-width vertical ReferenceLine, Recharts 'insideTopRight' renders at x - offset
    // (to the LEFT of the line, textAnchor="end"), keeping 'Warmup' on the left of the warmup line.
    const warmupLabelPosition = 'insideTopRight';

    return (
        <div className="grid gap-5 xl:grid-cols-2">
            <ChartContainer
                title="Client Throughput & Concurrency"
                subtitle={isServerFallback
                    ? 'Live engine telemetry (1m rate; client trace flushes on completion)'
                    : '15s rolling completion rate, token throughput, and active concurrency'}
                help="Streams 15-second rolling request completion rate (req/s), failed request rate, output throughput (tok/s), and active client in-flight requests. Drops in completion rate with rising in-flight concurrency indicate server backpressure."
                helpFormula="Rolling_RPS = N_15s / Window_Span_s;  Output_TokS = Σ Output_Tokens_15s / Window_Span_s;  InFlight = Client_Active(t)"
                helpAlign="start"
                actions={(
                    <>
                        {isServerFallback && <Badge tone="info" size="xs">Live engine telemetry</Badge>}
                        <ToggleGroup
                            size="xs"
                            value={throughputMode}
                            onChange={setThroughputMode}
                            options={[
                                { value: 'rps', label: 'QPS' },
                                { value: 'tok', label: 'Output' },
                                { value: 'inflight', label: 'In-Flight' },
                            ]}
                        />
                    </>
                )}
            >
                {hasThroughputModeSamples ? (
                    <>
                        <div className="h-64 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <ComposedChart data={throughputData} margin={{ top: warmupSeconds > 0 ? 22 : 8, right: 16, left: 4, bottom: 4 }}>
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
                                        tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
                                        stroke="var(--border-color)"
                                        tickFormatter={(value) => formatNumber(value)}
                                        {...(liveInflightYMax !== null ? { domain: [0, liveInflightYMax] } : {})}
                                        label={{
                                            value: throughputMode === 'rps' ? 'req/s' : throughputMode === 'tok' ? 'tok/s' : 'requests',
                                            angle: -90,
                                            position: 'insideLeft',
                                            fill: 'var(--text-muted)',
                                            fontSize: 10,
                                        }}
                                    />
                                    <Tooltip
                                        cursor={LINE_TOOLTIP_CURSOR}
                                        content={(
                                            <LiveChartTooltip
                                                units={{
                                                    rps: ' req/s',
                                                    failed_rps: ' req/s',
                                                    output_tok_s: ' tok/s',
                                                    in_flight: ' req',
                                                }}
                                            />
                                        )}
                                    />
                                    {warmupSeconds > 0 && (
                                        <ReferenceLine
                                            x={warmupSeconds}
                                            stroke={CHART_SERIES[2]}
                                            strokeDasharray="4 4"
                                            label={{ value: 'Warmup', fill: 'var(--text-muted)', fontSize: 10, position: warmupLabelPosition, dy: -18 }}
                                        />
                                    )}
                                    {throughputMode === 'rps' && (
                                        <>
                                            <Area
                                                type="monotone"
                                                dataKey="rps"
                                                name="Completed req/s"
                                                stroke={CHART_SERIES[0]}
                                                fill={CHART_SERIES[0]}
                                                fillOpacity={0.18}
                                                strokeWidth={2}
                                                connectNulls
                                                dot={sparseDotProp(throughputData, 'rps', CHART_SERIES[0])}
                                                isAnimationActive={false}
                                            />
                                            <Line
                                                type="monotone"
                                                dataKey="failed_rps"
                                                name="Failed req/s"
                                                stroke={CHART_SERIES[4]}
                                                strokeWidth={1.75}
                                                connectNulls
                                                dot={sparseDotProp(throughputData, 'failed_rps', CHART_SERIES[4])}
                                                isAnimationActive={false}
                                            />
                                        </>
                                    )}
                                    {throughputMode === 'tok' && (
                                        <Area
                                            type="monotone"
                                            dataKey="output_tok_s"
                                            name="Output Throughput (tok/s)"
                                            stroke={CHART_SERIES[1]}
                                            fill={CHART_SERIES[1]}
                                            fillOpacity={0.18}
                                            strokeWidth={2}
                                            connectNulls
                                            dot={sparseDotProp(throughputData, 'output_tok_s', CHART_SERIES[1])}
                                            isAnimationActive={false}
                                        />
                                    )}
                                    {throughputMode === 'inflight' && (
                                        <>
                                            {showLiveConfiguredConcurrency && (
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
                                            <Area
                                                type="monotone"
                                                dataKey="in_flight"
                                                name={isServerFallback ? 'Engine In-Flight Requests' : 'Client In-Flight Requests'}
                                                stroke={CHART_SERIES[3]}
                                                fill={CHART_SERIES[3]}
                                                fillOpacity={0.18}
                                                strokeWidth={2}
                                                connectNulls
                                                dot={sparseDotProp(throughputData, 'in_flight', CHART_SERIES[3])}
                                                isAnimationActive={false}
                                            />
                                        </>
                                    )}
                                </ComposedChart>
                            </ResponsiveContainer>
                        </div>
                        {throughputMode === 'rps' && (
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'Completed req/s', color: CHART_SERIES[0] },
                                    { label: 'Failed req/s', color: CHART_SERIES[4] },
                                ]}
                            />
                        )}
                        {showLiveConfiguredConcurrency && (
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    {
                                        label: isServerFallback ? 'Engine In-Flight Requests' : 'Client In-Flight Requests',
                                        color: CHART_SERIES[3],
                                    },
                                    {
                                        label: `Configured Concurrency (${formatCount(configuredConcurrency)})`,
                                        color: CHART_SERIES[2],
                                    },
                                ]}
                            />
                        )}
                    </>
                ) : isServerFallback ? (
                    <EmptyState
                        title="Waiting for engine rate samples"
                        message="Live engine throughput rates will appear after Prometheus completes two scrapes."
                    />
                ) : (
                    <EmptyState
                        title="Waiting for client samples"
                        message="Live throughput points will stream here once requests begin completing."
                    />
                )}
            </ChartContainer>

            <ChartContainer
                title="Rolling Latency & Schedule Drift"
                subtitle={isServerFallback
                    ? 'Live engine telemetry (1m P50/P90; client trace flushes on completion)'
                    : '15s rolling P50/P90 envelope and client dispatch schedule fidelity'}
                help="Tracks 15-second rolling P50 and P90 TTFT (ms) or TPOT (ms/tok) latency against configured SLO reference lines, or P90 dispatch schedule drift (ms). High drift means the load generator is falling behind trace arrival timestamps."
                helpFormula="Pq = Quantile_q(Window_15s);  Drift_P90 (ms) = Quantile_0.90((t_actual_send − t_target_schedule) × 1000)"
                helpAlign="end"
                actions={(
                    <>
                        {isServerFallback && <Badge tone="info" size="xs">Live engine telemetry</Badge>}
                        <ToggleGroup
                            size="xs"
                            value={latencyMode}
                            onChange={setLatencyMode}
                            options={[
                                { value: 'ttft', label: 'TTFT' },
                                { value: 'tpot', label: 'TPOT' },
                                { value: 'drift', label: 'Schedule Drift' },
                            ]}
                        />
                    </>
                )}
            >
                {hasLatencyModeSamples ? (
                    <>
                        <div className="h-64 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <ComposedChart data={latencyData} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
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
                                        width={54}
                                        tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
                                        stroke="var(--border-color)"
                                        label={{ value: latencyMode === 'tpot' ? 'ms/tok' : 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                    />
                                    <Tooltip
                                        cursor={LINE_TOOLTIP_CURSOR}
                                        content={(
                                            <LiveChartTooltip
                                                units={{
                                                    ttft_p50_ms: ' ms',
                                                    ttft_p90_ms: ' ms',
                                                    tpot_p50_ms: ' ms/tok',
                                                    tpot_p90_ms: ' ms/tok',
                                                    drift_p90_ms: ' ms',
                                                }}
                                            />
                                        )}
                                    />
                                    {latencySlo !== null && (
                                        <ReferenceLine
                                            y={latencySlo}
                                            stroke={CHART_SERIES[4]}
                                            strokeDasharray="4 4"
                                            label={{ value: `SLO ${formatMilliseconds(latencySlo)}${latencyMode === 'tpot' ? '/tok' : ''}`, fill: 'var(--text-muted)', fontSize: 10, position: 'insideTopRight' }}
                                        />
                                    )}
                                    {latencyMode === 'ttft' && (
                                        <>
                                            <Line type="monotone" dataKey="ttft_p50_ms" name="TTFT P50" stroke={CHART_SERIES[0]} strokeWidth={2} dot={sparseDotProp(latencyData, 'ttft_p50_ms', CHART_SERIES[0])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="ttft_p90_ms" name="TTFT P90" stroke={CHART_SERIES[1]} strokeWidth={1.75} strokeDasharray="4 2" dot={sparseDotProp(latencyData, 'ttft_p90_ms', CHART_SERIES[1])} connectNulls isAnimationActive={false} />
                                        </>
                                    )}
                                    {latencyMode === 'tpot' && (
                                        <>
                                            <Line type="monotone" dataKey="tpot_p50_ms" name="TPOT P50" stroke={CHART_SERIES[0]} strokeWidth={2} dot={sparseDotProp(latencyData, 'tpot_p50_ms', CHART_SERIES[0])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="tpot_p90_ms" name="TPOT P90" stroke={CHART_SERIES[1]} strokeWidth={1.75} strokeDasharray="4 2" dot={sparseDotProp(latencyData, 'tpot_p90_ms', CHART_SERIES[1])} connectNulls isAnimationActive={false} />
                                        </>
                                    )}
                                    {latencyMode === 'drift' && (
                                        <Area
                                            type="monotone"
                                            dataKey="drift_p90_ms"
                                            name="Schedule Drift P90"
                                            stroke={CHART_SERIES[2]}
                                            fill={CHART_SERIES[2]}
                                            fillOpacity={0.18}
                                            strokeWidth={2}
                                            dot={sparseDotProp(latencyData, 'drift_p90_ms', CHART_SERIES[2])}
                                            connectNulls
                                            isAnimationActive={false}
                                        />
                                    )}
                                </ComposedChart>
                            </ResponsiveContainer>
                        </div>
                        {latencyMode !== 'drift' && (
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: `${latencyMode.toUpperCase()} P50`, color: CHART_SERIES[0] },
                                    { label: `${latencyMode.toUpperCase()} P90`, color: CHART_SERIES[1] },
                                ]}
                            />
                        )}
                    </>
                ) : isServerFallback && latencyMode === 'drift' ? (
                    <EmptyState
                        title="Schedule drift pending trace flush"
                        message="Schedule drift is computed on trace completion when per-request dispatch timestamps are buffered."
                    />
                ) : (
                    <EmptyState
                        title="Waiting for latency samples"
                        message={latencyData.length > 0
                            ? `No ${latencyMode === 'drift' ? 'schedule drift' : latencyMode.toUpperCase()} samples recorded in completed buckets yet.`
                            : 'Rolling latency and replay drift will appear after the first responses arrive.'}
                    />
                )}
            </ChartContainer>

            <ChartContainer
                title="vLLM Engine & EPP Load"
                subtitle={isDisaggregated ? 'Prefill and Decode running/waiting requests with EPP in-flight requests' : 'vLLM running/waiting requests and EPP in-flight requests'}
                help="Plots server-side running and waiting request counts (unified engine or P/D Disaggregation Prefill/Decode pools) alongside EPP in-flight requests from 5-second Prometheus scrapes. Rising waiting requests indicate admission saturation."
                helpFormula="Engine_Load = Running(t) + Waiting(t)  (unified or Prefill + Decode);  EPP_InFlight = Active_Routed(t)"
                helpAlign="start"
            >
                {prometheusAvailable === false ? (
                    <EmptyState
                        title="Server telemetry unavailable"
                        message="Prometheus metrics are not configured for this endpoint or cluster."
                    />
                ) : serverData.length > 0 ? (
                    <>
                        <div className="h-64 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <ComposedChart data={serverData} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
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
                                        allowDecimals={false}
                                        width={48}
                                        tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
                                        stroke="var(--border-color)"
                                        label={{ value: 'requests', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                    />
                                    <Tooltip
                                        cursor={LINE_TOOLTIP_CURSOR}
                                        content={(
                                            <LiveChartTooltip
                                                units={{
                                                    vllm_running: ' req',
                                                    vllm_waiting: ' req',
                                                    epp_running: ' req',
                                                    prefill_running: ' req',
                                                    prefill_waiting: ' req',
                                                    decode_running: ' req',
                                                    decode_waiting: ' req',
                                                }}
                                            />
                                        )}
                                    />
                                    {isDisaggregated ? (
                                        <>
                                            <Line type="monotone" dataKey="prefill_running" name="Prefill Running" stroke={CHART_SERIES[0]} strokeWidth={2} dot={sparseDotProp(serverData, 'prefill_running', CHART_SERIES[0])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="prefill_waiting" name="Prefill Waiting" stroke={CHART_SERIES[2]} strokeWidth={2} dot={sparseDotProp(serverData, 'prefill_waiting', CHART_SERIES[2])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="decode_running" name="Decode Running" stroke={CHART_SERIES[1]} strokeWidth={2} dot={sparseDotProp(serverData, 'decode_running', CHART_SERIES[1])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="decode_waiting" name="Decode Waiting" stroke={CHART_SERIES[4]} strokeWidth={2} dot={sparseDotProp(serverData, 'decode_waiting', CHART_SERIES[4])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="epp_running" name="EPP In-Flight" stroke={CHART_SERIES[3]} strokeWidth={1.75} strokeDasharray="4 2" dot={sparseDotProp(serverData, 'epp_running', CHART_SERIES[3])} connectNulls isAnimationActive={false} />
                                        </>
                                    ) : (
                                        <>
                                            <Line type="monotone" dataKey="vllm_running" name="vLLM Running Requests" stroke={CHART_SERIES[0]} strokeWidth={2} dot={sparseDotProp(serverData, 'vllm_running', CHART_SERIES[0])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="vllm_waiting" name="vLLM Waiting Requests" stroke={CHART_SERIES[2]} strokeWidth={2} dot={sparseDotProp(serverData, 'vllm_waiting', CHART_SERIES[2])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="epp_running" name="EPP In-Flight" stroke={CHART_SERIES[1]} strokeWidth={1.75} strokeDasharray="4 2" dot={sparseDotProp(serverData, 'epp_running', CHART_SERIES[1])} connectNulls isAnimationActive={false} />
                                        </>
                                    )}
                                </ComposedChart>
                            </ResponsiveContainer>
                        </div>
                        <ChartLegend className="mt-3" entries={serverLegend} />
                    </>
                ) : (
                    <EmptyState
                        title="Waiting for server samples"
                        message="Server running/waiting requests and EPP load will appear after the first 5s Prometheus scrape."
                    />
                )}
            </ChartContainer>

            <ChartContainer
                title={`KV Cache & ${acceleratorLabel} Pressure`}
                subtitle={`Peak KV cache usage, prefix cache hit rate, and ${acceleratorLabel.toLowerCase()} utilization / power`}
                help={`Displays peak KV cache usage (%, with a 90% eviction risk line), 30-second prefix cache hit rate (%), and ${acceleratorLabel} utilization (%) or power draw (W) from Prometheus.`}
                helpFormula="Peak_KV (%) = max_pods((Used_KV_Blocks / Total_KV_Blocks) × 100);  Hit_30s (%) = (ΔCached_Prompt_30s / ΔTotal_Prompt_30s) × 100"
                helpAlign="end"
                actions={(
                    <ToggleGroup
                        size="xs"
                        value={pressureMode}
                        onChange={setPressureMode}
                        options={[
                            { value: 'pct', label: 'Cache & Util (%)' },
                            { value: 'power', label: 'Power (W)' },
                        ]}
                    />
                )}
            >
                {prometheusAvailable === false ? (
                    <EmptyState
                        title="Server telemetry unavailable"
                        message="KV cache and hardware telemetry require Prometheus access."
                    />
                ) : kvData.length > 0 ? (
                    <>
                        <div className="h-64 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <ComposedChart data={kvData} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
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
                                        width={50}
                                        domain={pressureMode === 'pct' ? [0, 100] : [0, 'auto']}
                                        ticks={pressureMode === 'pct' ? [0, 25, 50, 75, 100] : undefined}
                                        allowDataOverflow={pressureMode === 'pct'}
                                        tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
                                        stroke="var(--border-color)"
                                        label={{ value: pressureMode === 'pct' ? '%' : 'W', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                    />
                                    <Tooltip
                                        cursor={LINE_TOOLTIP_CURSOR}
                                        content={(
                                            <LiveChartTooltip
                                                units={{
                                                    kv_cache_peak_pct: '%',
                                                    prefix_cache_hit_pct: '%',
                                                    accelerator_util_pct: '%',
                                                    accelerator_power_watts: ' W',
                                                }}
                                            />
                                        )}
                                    />
                                    {pressureMode === 'pct' ? (
                                        <>
                                            <ReferenceLine
                                                y={90}
                                                stroke={CHART_SERIES[4]}
                                                strokeDasharray="4 4"
                                                label={{ value: '90% KV alert', fill: 'var(--text-muted)', fontSize: 10, position: 'insideTopRight' }}
                                            />
                                            <Line type="monotone" dataKey="kv_cache_peak_pct" name="Peak KV Cache Usage" stroke={CHART_SERIES[2]} strokeWidth={2} dot={sparseDotProp(kvData, 'kv_cache_peak_pct', CHART_SERIES[2])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="prefix_cache_hit_pct" name="Prefix Cache Hit Rate" stroke={CHART_SERIES[0]} strokeWidth={2} dot={sparseDotProp(kvData, 'prefix_cache_hit_pct', CHART_SERIES[0])} connectNulls isAnimationActive={false} />
                                            <Line type="monotone" dataKey="accelerator_util_pct" name={`${acceleratorLabel} Util`} stroke={CHART_SERIES[1]} strokeWidth={2} dot={sparseDotProp(kvData, 'accelerator_util_pct', CHART_SERIES[1])} connectNulls isAnimationActive={false} />
                                        </>
                                    ) : (
                                        <Area
                                            type="monotone"
                                            dataKey="accelerator_power_watts"
                                            name={`${acceleratorLabel} Power`}
                                            stroke={CHART_SERIES[3]}
                                            fill={CHART_SERIES[3]}
                                            fillOpacity={0.18}
                                            strokeWidth={2}
                                            dot={sparseDotProp(kvData, 'accelerator_power_watts', CHART_SERIES[3])}
                                            connectNulls
                                            isAnimationActive={false}
                                        />
                                    )}
                                </ComposedChart>
                            </ResponsiveContainer>
                        </div>
                        {pressureMode === 'pct' && (
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'Peak KV Cache Usage', color: CHART_SERIES[2] },
                                    { label: 'Prefix Cache Hit Rate', color: CHART_SERIES[0] },
                                    { label: `${acceleratorLabel} Util`, color: CHART_SERIES[1] },
                                ]}
                            />
                        )}
                    </>
                ) : (
                    <EmptyState
                        title="Waiting for pressure telemetry"
                        message="KV cache and hardware metrics will appear once the server scrape completes."
                    />
                )}
            </ChartContainer>
        </div>
    );
}

export function LiveMonitorPanel({
    task,
    liveTelemetry,
    liveError = '',
    onStop,
    stopping = false,
    durationClockMs = null,
    onOpenErrorInspector,
}) {
    const [runtimeTab, setRuntimeTab] = useState('replay');
    const [internalInspectorFilter, setInternalInspectorFilter] = useState(null);

    const taskId = task?.id || task?.task_id || '';
    const taskStatus = String(task?.status || '').toLowerCase();
    const openErrorInspector = useCallback((filter = 'all') => {
        if (onOpenErrorInspector) {
            onOpenErrorInspector(filter);
        } else {
            setInternalInspectorFilter(filter);
        }
    }, [onOpenErrorInspector]);

    const kpis = liveTelemetry?.kpis || {};
    const progressInfo = liveTelemetry?.progress || {};
    const fidelity = liveTelemetry?.fidelity || {};
    const telemetryStatus = liveTelemetry?.telemetry_status || {};
    const clientTelemetrySource = telemetryStatus.client_telemetry_source || 'none';
    const isServerFallback = clientTelemetrySource === 'server_fallback';
    const rawAlerts = liveTelemetry?.alerts;
    const sortedAlerts = useMemo(() => {
        const alerts = Array.isArray(rawAlerts) ? rawAlerts : [];
        const rank = { crit: 0, critical: 0, warn: 1, warning: 1, info: 2 };
        return [...alerts].sort((a, b) => (rank[String(a?.severity || '').toLowerCase()] ?? 3) - (rank[String(b?.severity || '').toLowerCase()] ?? 3));
    }, [rawAlerts]);

    const phase = formatPhase(liveTelemetry?.live_phase || (task?.execution_started_at ? 'Profiling' : 'Preparing'));
    const percent = Math.max(0, Math.min(100, Number(progressInfo.percent ?? task?.progress_percent ?? 0)));
    const elapsedSeconds = liveTelemetry?.elapsed_seconds;
    const durationSeconds = liveTelemetry?.duration_seconds ?? numberAt(task?.simulation || {}, ['duration_seconds']);
    const warmupSeconds = liveTelemetry?.warmup_seconds ?? numberAt(task?.simulation?.backend_options || {}, ['warmup_seconds']) ?? 0;
    const etaSeconds = liveTelemetry?.eta_seconds;

    const dispatched = progressInfo.dispatched ?? kpis.dispatched ?? null;
    const completed = progressInfo.completed ?? kpis.total_requests ?? null;
    const failed = progressInfo.failed ?? kpis.error_counts?.all ?? 0;
    const inFlight = progressInfo.in_flight ?? (
        dispatched !== null && completed !== null ? Math.max(0, dispatched - completed - Number(failed || 0)) : null
    );

    const acceleratorLabel = kpis.accelerator_label || telemetryStatus.accelerator_label || 'Accelerator';
    const backendOptions = task?.simulation?.backend_options || task?.backend_options || {};
    const ttftSloSeconds = numberAt(backendOptions, ['ttft_slo']);
    const tpotSloSeconds = numberAt(backendOptions, ['tpot_slo']);
    const ttftSloMs = ttftSloSeconds === null ? null : ttftSloSeconds * 1000;
    const tpotSloMs = tpotSloSeconds === null ? null : tpotSloSeconds * 1000;

    const errorCounts = kpis.error_counts || {};
    const totalErrorCount = Number(errorCounts.all ?? failed ?? 0);
    const errorSubCategories = [
        ['4xx', '4xx', errorCounts['4xx']],
        ['5xx', '5xx', errorCounts['5xx']],
        ['timeout', 'Timeout', errorCounts.timeout],
        ['transport', 'Transport', errorCounts.transport],
    ];

    const fidelityStatus = String(fidelity.status || 'unknown').toLowerCase();
    const fidelityTone = fidelityStatus === 'on_schedule'
        ? 'success'
        : fidelityStatus === 'mild_drift'
            ? 'warning'
            : fidelityStatus === 'lagging'
                ? 'danger'
                : 'neutral';
    const fidelityText = fidelity.label || (
        fidelity.drift_p90_ms !== null && fidelity.drift_p90_ms !== undefined
            ? `${fidelityStatus === 'lagging' ? 'Lagging' : fidelityStatus === 'mild_drift' ? 'Mild Drift' : 'On-Schedule'} (P90 ${formatDriftMs(fidelity.drift_p90_ms)})`
            : 'Schedule fidelity pending'
    );

    const liveTargetDeployments = liveTelemetry?.target_deployments;
    const taskExecutionId = task?.endpoint_deployment_execution_id;
    const taskDeploymentName = task?.endpoint_deployment_name;
    const taskNamespace = task?.endpoint_namespace;
    const taskClusterId = task?.endpoint_cluster_id;
    const targetDeployments = useMemo(() => {
        if (Array.isArray(liveTargetDeployments) && liveTargetDeployments.length > 0) {
            return liveTargetDeployments;
        }
        if (taskExecutionId) {
            return [{
                execution_id: taskExecutionId,
                name: taskDeploymentName || taskExecutionId,
                namespace: taskNamespace || '',
                cluster_id: taskClusterId || '',
                status: 'ready',
            }];
        }
        return [];
    }, [liveTargetDeployments, taskClusterId, taskDeploymentName, taskExecutionId, taskNamespace]);

    const liveRecentLogs = liveTelemetry?.recent_logs;
    const taskLogList = task?.logs;
    const combinedLogs = useMemo(() => {
        if (Array.isArray(liveRecentLogs) && liveRecentLogs.length > 0) {
            return liveRecentLogs;
        }
        return Array.isArray(taskLogList) ? taskLogList : [];
    }, [liveRecentLogs, taskLogList]);

    const configuredConcurrency = resolveConfiguredConcurrency(task, liveTelemetry?.run_manifest);
    const trace = task?.prompt?.trace || {};
    const backendOptionEntries = Object.entries(backendOptions).filter(
        ([key]) => !['ttft_slo', 'tpot_slo', 'error_rate_slo'].includes(key),
    );

    return (
        <div className="space-y-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <ToggleGroup
                    value={runtimeTab}
                    onChange={setRuntimeTab}
                    options={[
                        { value: 'replay', label: 'Replay Monitor' },
                        { value: 'live-flow', label: 'Live Monitoring' },
                        { value: 'logs', label: 'Logs & Configuration' },
                    ]}
                />
                {liveError && (
                    <Badge tone="warning" size="sm">
                        Live update delayed: {liveError}
                    </Badge>
                )}
            </div>

            {runtimeTab === 'replay' && (
                <>
                    <Panel padding="sm">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                            <div className="flex flex-wrap items-center gap-2.5">
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone={phase.tone} size="md">
                                        <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
                                        {phase.label}
                                    </Badge>
                                    <HelpTooltip label="Execution phase help" align="start" iconSize={12} formula="Warmup: t < Warmup_s;  Profiling: Warmup_s ≤ t ≤ Duration_s;  Cooldown: t > Duration_s">
                                        Current live execution stage: Preparing, Warmup (excluded from steady-state KPIs), Profiling, Cooldown, or Collecting Telemetry.
                                    </HelpTooltip>
                                </span>
                                <span className="inline-flex items-center gap-1 font-mono text-xs text-theme-text">
                                    <span>
                                        Elapsed: {elapsedSeconds !== undefined && elapsedSeconds !== null
                                            ? formatDurationSeconds(elapsedSeconds)
                                            : formatDuration(task, liveTelemetry?.live_summary, durationClockMs ?? undefined)}
                                        {durationSeconds ? ` / ${formatDurationSeconds(durationSeconds)}` : ''}
                                        {etaSeconds !== undefined && etaSeconds !== null ? ` (ETA ${formatDurationSeconds(etaSeconds)})` : ''}
                                    </span>
                                    <HelpTooltip label="Elapsed time help" align="start" iconSize={12} formula="Elapsed_s = t_now − t_start;  ETA_s = max(0, Duration_s − Elapsed_s)">
                                        Elapsed replay time (seconds) against the configured active duration and estimated time remaining (ETA).
                                    </HelpTooltip>
                                </span>
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone={fidelityTone} size="sm">{fidelityText}</Badge>
                                    <HelpTooltip label="Schedule fidelity help" align="end" iconSize={12} formula="Drift_P90 (ms) = Quantile_0.90((t_actual_send − t_target_schedule) × 1000)">
                                        Measures 90th percentile client dispatch drift (ms) against trace timestamps. On-Schedule keeps drift low; Lagging indicates the client generator cannot keep pace.
                                    </HelpTooltip>
                                </span>
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone={telemetryStatus.prometheus_available ? 'success' : 'neutral'} size="sm">
                                        Prom {telemetryStatus.prometheus_available ? 'OK' : 'Off'}
                                    </Badge>
                                    <HelpTooltip label="Prometheus telemetry help" align="end" iconSize={12}>
                                        Indicates whether live 5-second Prometheus scrapes of serving engine queues and KV cache are active (OK) or unavailable (Off).
                                    </HelpTooltip>
                                </span>
                                <span className="inline-flex items-center gap-1">
                                    <Badge tone={telemetryStatus.accelerator_available ? 'info' : 'neutral'} size="sm">
                                        {acceleratorLabel} {telemetryStatus.accelerator_available ? 'OK' : 'N/A'}
                                    </Badge>
                                    <HelpTooltip label={`${acceleratorLabel} telemetry help`} align="end" iconSize={12}>
                                        Reports whether live {acceleratorLabel} utilization and power metrics are streaming from cluster hardware exporters.
                                    </HelpTooltip>
                                </span>
                                {onStop && (
                                    <Button
                                        variant="dangerOutline"
                                        size="sm"
                                        onClick={onStop}
                                        disabled={!STOPPABLE_STATUSES.has(taskStatus)}
                                        isLoading={stopping}
                                    >
                                        <Square className="h-3.5 w-3.5" /> Stop
                                    </Button>
                                )}
                            </div>
                        </div>

                        <div className="mt-3">
                            <div className="mb-1.5 flex items-center justify-between gap-4 text-[11px] text-theme-muted">
                                <span>{task?.progress_message || phase.label}</span>
                                <span className="inline-flex items-center gap-1 font-mono font-semibold text-theme-text">
                                    <span>{formatNumber(percent, '%')}</span>
                                    <HelpTooltip label="Replay progress help" align="end" iconSize={11} formula="Progress (%) = clamp(0, 100, (Elapsed_s / Total_Duration_s) × 100)">
                                        Overall completion percentage reported by the simulation runner across the scheduled workload.
                                    </HelpTooltip>
                                </span>
                            </div>
                            <div
                                className="h-2 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800"
                                role="progressbar"
                                aria-valuenow={percent}
                                aria-valuemin="0"
                                aria-valuemax="100"
                            >
                                <div
                                    className="h-full rounded-full bg-emerald-500 transition-all"
                                    style={{ width: `${percent}%` }}
                                />
                            </div>
                        </div>

                        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
                            <span className="inline-flex items-center gap-1 text-theme-muted">
                                <span>Dispatched: <strong className="font-mono text-theme-text">{formatCount(dispatched)}</strong></span>
                                <HelpTooltip label="Dispatched requests help" align="start" iconSize={12} formula="Dispatched = Count(Requests_Sent)">
                                    Total requests sent so far by the replay client toward the inference endpoint.
                                </HelpTooltip>
                            </span>
                            <span className="inline-flex items-center gap-1 text-theme-muted">
                                <span>
                                    Completed:{' '}
                                    <strong className="font-mono text-theme-text">
                                        {isServerFallback && Number(completed || 0) === 0 ? 'Pending flush' : formatCount(completed)}
                                    </strong>
                                </span>
                                <HelpTooltip label="Completed requests help" align="start" iconSize={12} formula="Completed = Count(Successful_Responses)">
                                    Cumulative count of requests that finished successfully with a valid response.
                                </HelpTooltip>
                            </span>
                            <span className="inline-flex items-center gap-1 text-theme-muted">
                                <span>
                                    In-Flight: <strong className="font-mono text-theme-text">{formatCount(inFlight)}</strong>
                                    {isServerFallback ? (
                                        <span className="ml-1 text-[11px]">(Engine)</span>
                                    ) : kpis.server_running !== null && kpis.server_running !== undefined && (
                                        <span className="ml-1 text-[11px]">
                                            (Server: {formatCount(Number(kpis.server_running || 0) + Number(kpis.server_waiting || 0))})
                                        </span>
                                    )}
                                </span>
                                <HelpTooltip label="In-flight requests help" align="start" iconSize={12} formula="Client_InFlight = max(0, Dispatched − Completed − Failed);  Server_Total = Running + Waiting">
                                    Requests currently awaiting completion on the client, alongside server-side running + waiting concurrency when Prometheus is active.
                                </HelpTooltip>
                            </span>
                            <span className="inline-flex items-center gap-1">
                                <button
                                    type="button"
                                    onClick={() => openErrorInspector('all')}
                                    className={cn(
                                        'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-xs font-semibold transition-colors',
                                        totalErrorCount > 0
                                            ? 'border-red-500/40 bg-red-500/10 text-red-700 hover:bg-red-500/20 dark:text-red-300'
                                            : 'border-theme-border bg-theme-bg text-theme-muted hover:text-theme-text',
                                    )}
                                >
                                    Failed: {formatCount(totalErrorCount)}
                                    <ExternalLink className="h-3 w-3" />
                                </button>
                                <HelpTooltip label="Failed requests help" align="start" iconSize={12} formula="Failed = Count(4xx + 5xx + Timeout + Transport)">
                                    Total failed requests (HTTP 4xx/5xx, timeouts, and transport errors). Click the badge to open the Live Error Inspector.
                                </HelpTooltip>
                            </span>
                        </div>
                    </Panel>

                    {sortedAlerts.length > 0 ? (
                        <div className="space-y-2" role="region" aria-label="Live health alerts">
                            {sortedAlerts.map((alert, idx) => {
                                const sev = alertSeverityProps(alert.severity);
                                const isErrorBurst = String(alert.code || '').toUpperCase() === 'ERROR_BURST';
                                return (
                                    <div
                                        key={`${alert.code || 'alert'}-${idx}`}
                                        className={cn(
                                            'flex flex-wrap items-center justify-between gap-3 rounded-xl border p-3 text-xs',
                                            sev.tone === 'danger'
                                                ? 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-200'
                                                : 'border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200',
                                        )}
                                    >
                                        <div className="flex min-w-0 items-center gap-2.5">
                                            <AlertTriangle className="h-4 w-4 shrink-0" />
                                            <Badge tone={sev.tone} size="xs">{sev.label}</Badge>
                                            <span className="font-mono font-semibold">{alert.code}</span>
                                            <span className="truncate">{alert.message || alert.title}</span>
                                        </div>
                                        {isErrorBurst && (
                                            <Button
                                                variant="dangerOutline"
                                                size="xs"
                                                onClick={() => openErrorInspector('all')}
                                            >
                                                Inspect errors
                                            </Button>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    ) : (
                        <div className="flex items-center justify-between gap-3 rounded-xl border border-theme-border bg-theme-card px-4 py-2.5 text-xs">
                            <div className="flex items-center gap-2">
                                <StatusChip status="verified" label="Healthy" size="xs" />
                                <span className="text-theme-muted">No active bottleneck alerts in the live telemetry window.</span>
                            </div>
                        </div>
                    )}

                    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        <StatCard
                            compact
                            title="Throughput"
                            help="Live request completion rate (req/s) across finished requests. Higher values indicate greater serving capacity under the offered load."
                            helpFormula="Rolling_RPS = Samples_15s / Window_Span_s"
                            helpAlign="start"
                            value={formatNumber(kpis.throughput_rps, ' req/s')}
                            details={[
                                {
                                    label: 'Output Throughput',
                                    value: formatNumber(kpis.output_tok_per_s ?? kpis.output_tok_s, ' tok/s'),
                                    help: 'Aggregate generated output tokens per second across completed responses.',
                                    formula: 'Output_Rate (tok/s) = Σ Output_Tokens_15s / Window_Span_s',
                                },
                                {
                                    label: 'Completed',
                                    value: isServerFallback && Number(completed || 0) === 0 ? 'Pending flush' : `${formatCount(completed)} req`,
                                    help: 'Cumulative successful requests completed since the simulation began.',
                                    formula: 'Completed = Count(Successful_Responses)',
                                },
                            ]}
                        >
                            {isServerFallback && <ServerFallbackNotice />}
                        </StatCard>
                        <StatCard
                            compact
                            title={isServerFallback ? 'Live Latency (1m)' : 'Rolling Latency (15s)'}
                            help="Median Time to First Token (TTFT P50) over the trailing 15-second window; compares P90 against configured SLO targets. Lower values mean faster responsiveness."
                            helpFormula="TTFT_P50 = Quantile_0.50(t_first_token − t_request_start) over 15s"
                            helpAlign="start"
                            value={`TTFT ${formatMilliseconds(kpis.ttft_p50_ms)}`}
                            tone={
                                ttftSloMs !== null && kpis.ttft_p90_ms != null
                                    ? (kpis.ttft_p90_ms > ttftSloMs ? 'danger' : 'success')
                                    : 'neutral'
                            }
                            details={[
                                {
                                    label: 'TTFT P90',
                                    value: formatMilliseconds(kpis.ttft_p90_ms),
                                    help: '90th percentile Time to First Token (ms) in the trailing 15s window; captures tail queuing and prefill delay.',
                                    formula: 'TTFT_P90 = Quantile_0.90(t_first_token − t_request_start) over 15s',
                                },
                                {
                                    label: 'TPOT P50 / P90',
                                    value: `${formatMilliseconds(kpis.tpot_p50_ms) === '—' ? '—' : `${formatMilliseconds(kpis.tpot_p50_ms)}/tok`} / ${formatMilliseconds(kpis.tpot_p90_ms) === '—' ? '—' : `${formatMilliseconds(kpis.tpot_p90_ms)}/tok`}`,
                                    help: 'Median and 90th percentile Time per Output Token (ms/tok) in the trailing 15s window; lower values mean steadier streaming.',
                                    formula: 'TPOT = (t_last_token − t_first_token) / max(1, Output_Tokens − 1);  P50/P90 over 15s',
                                },
                            ]}
                        >
                            {(ttftSloMs !== null || tpotSloMs !== null) && (
                                <div className="mt-1.5 flex flex-wrap gap-1.5">
                                    {ttftSloMs !== null && kpis.ttft_p90_ms != null && (
                                        <Badge tone={kpis.ttft_p90_ms > ttftSloMs ? 'danger' : 'success'} size="xs">
                                            TTFT {kpis.ttft_p90_ms > ttftSloMs ? 'Over SLO' : 'Within SLO'}
                                        </Badge>
                                    )}
                                    {tpotSloMs !== null && kpis.tpot_p90_ms != null && (
                                        <Badge tone={kpis.tpot_p90_ms > tpotSloMs ? 'danger' : 'success'} size="xs">
                                            TPOT {kpis.tpot_p90_ms > tpotSloMs ? 'Over SLO' : 'Within SLO'}
                                        </Badge>
                                    )}
                                </div>
                            )}
                            {isServerFallback && <ServerFallbackNotice />}
                        </StatCard>

                        <div
                            className={cn(
                                'relative hover:z-30 focus-within:z-30 flex flex-col justify-between rounded-xl border border-theme-border bg-theme-card p-4 shadow-lg transition-colors dark:border-slate-700/60 dark:bg-slate-800/60',
                                totalErrorCount > 0 && 'border-red-500/40',
                            )}
                        >
                            <button
                                type="button"
                                onClick={() => openErrorInspector('all')}
                                className="w-full cursor-pointer text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50"
                            >
                                <div className="flex items-center justify-between gap-2">
                                    <span className="inline-flex items-center gap-1.5">
                                        <p className="text-xs font-medium text-theme-muted">Error Rate (Click to inspect)</p>
                                        <HelpTooltip label="Error Rate help" align="end" iconSize={12} asSpan formula="Cumulative_Error_Rate (%) = (Total_Failed / Total_Dispatched) × 100">
                                            Cumulative percentage of dispatched requests that failed (HTTP 4xx/5xx, timeout, or transport error). Any non-zero rate warrants inspection.
                                        </HelpTooltip>
                                    </span>
                                    <ExternalLink className="h-3.5 w-3.5 text-theme-muted" />
                                </div>
                                <h3
                                    className={cn(
                                        'mt-0.5 text-lg font-bold',
                                        totalErrorCount > 0 ? 'text-red-600 dark:text-red-300' : 'text-emerald-600 dark:text-emerald-300',
                                    )}
                                >
                                    {formatNumber(kpis.error_rate_pct, '%')}
                                </h3>
                                <div className="mt-0.5 flex items-center gap-1 text-xs text-theme-muted">
                                    <span>15s: {formatNumber(kpis.rolling_error_rate_pct, '%')} · {formatCount(totalErrorCount)} errors</span>
                                    <HelpTooltip label="Rolling 15s error rate help" align="end" iconSize={11} asSpan formula="Rolling_Error_Rate (%) = (Failed_15s / Samples_15s) × 100">
                                        Error percentage over the trailing 15-second window and total cumulative failed request count.
                                    </HelpTooltip>
                                </div>
                            </button>
                            <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                                {errorSubCategories.map(([catKey, catLabel, count]) => (
                                    <button
                                        key={catKey}
                                        type="button"
                                        onClick={() => openErrorInspector(catKey)}
                                        className="rounded border border-theme-border bg-theme-bg px-1.5 py-0.5 font-mono text-[10px] text-theme-muted hover:border-red-500/40 hover:text-theme-text"
                                    >
                                        {catLabel}: {formatCount(count ?? 0)}
                                    </button>
                                ))}
                                <HelpTooltip label="Error breakdown help" align="end" iconSize={11} formula="Total_Failed = Count(4xx) + Count(5xx) + Count(Timeout) + Count(Transport)">
                                    Counts by failure class (4xx client rejection, 5xx server failure, request timeout, or network transport error). Click a chip to filter the Error Inspector.
                                </HelpTooltip>
                            </div>
                        </div>

                        <StatCard
                            compact
                            title="Running & Waiting Requests"
                            help="Live server-side request concurrency reported by Prometheus (running / waiting). Rising waiting counts indicate admission queuing or batch saturation."
                            helpFormula="Engine_Running = Σ_pods Running(t);  Engine_Waiting = Σ_pods Waiting(t)"
                            helpAlign="start"
                            value={
                                telemetryStatus.prometheus_available === false
                                    ? 'N/A'
                                    : `${formatCount(kpis.server_running)} / ${formatCount(kpis.server_waiting)}`
                            }
                            details={[
                                {
                                    label: 'Running / Waiting',
                                    value: `${formatCount(kpis.server_running)} / ${formatCount(kpis.server_waiting)}`,
                                    help: 'Requests currently executing on the serving engine versus queued requests waiting for batch slots.',
                                    formula: 'Running = Σ_pods Active_Requests;  Waiting = Σ_pods Queued_Requests',
                                },
                                {
                                    label: 'EPP In-Flight Requests',
                                    value: formatCount(kpis.epp_running),
                                    help: 'Active requests currently tracked in flight by the Endpoint Picker (EPP) routing layer.',
                                    formula: 'EPP_InFlight = Σ_routes Active_EPP_Requests(t)',
                                },
                            ]}
                        />
                        <StatCard
                            compact
                            title="KV Cache Usage & Prefix Cache Hit Rate"
                            help="Peak KV cache usage (%) across serving pods from Prometheus. Values at or above 90% risk block eviction and request preemption."
                            helpFormula="Peak_KV_Usage (%) = max_pods((Used_KV_Blocks / Total_KV_Blocks) × 100)"
                            helpAlign="start"
                            value={formatNumber(clampPercent(kpis.kv_cache_peak_pct), '%')}
                            tone={kpis.kv_cache_peak_pct >= 90 ? 'warning' : 'neutral'}
                            details={[
                                {
                                    label: 'Prefix Cache Hit Rate (30s)',
                                    value: formatNumber(clampPercent(kpis.prefix_cache_hit_pct), '%'),
                                    help: 'Share of prompt tokens served from the prefix KV cache over the trailing 30 seconds; higher means more prefill work avoided.',
                                    formula: 'Hit_Rate_30s (%) = (ΔCached_Prompt_Tokens_30s / ΔTotal_Prompt_Tokens_30s) × 100',
                                },
                                {
                                    label: 'Telemetry',
                                    value: telemetryStatus.source || (telemetryStatus.prometheus_available ? 'Prometheus' : 'Client only'),
                                    help: 'Active server telemetry source (Prometheus scrape or client-only fallback).',
                                },
                            ]}
                        />
                        <StatCard
                            compact
                            title={`${acceleratorLabel} Util & Power`}
                            help={`Real-time ${acceleratorLabel} compute utilization (%) from cluster hardware telemetry. Sustained near-100% utilization with rising latency signals compute saturation.`}
                            helpFormula="Avg_Util (%) = mean_devices(Device_Compute_Utilization_%)"
                            helpAlign="end"
                            value={formatNumber(clampPercent(kpis.accelerator_util_pct), '%')}
                            details={[
                                {
                                    label: 'Serving power',
                                    value: formatWatts(kpis.accelerator_power_watts ?? kpis.accelerator_power_w),
                                    help: `Instantaneous ${acceleratorLabel} power draw in watts aggregated across serving devices.`,
                                    formula: 'Serving_Power (W) = Σ_devices Instantaneous_Power_W',
                                },
                                {
                                    label: 'Profile',
                                    value: acceleratorLabel,
                                    help: 'Detected hardware accelerator profile label reported by cluster telemetry.',
                                },
                            ]}
                        />
                    </div>

                    <LiveChartsGrid
                        series={liveTelemetry?.series}
                        warmupSeconds={warmupSeconds}
                        ttftSloMs={ttftSloMs}
                        tpotSloMs={tpotSloMs}
                        prometheusAvailable={telemetryStatus.prometheus_available}
                        topologyMode={telemetryStatus.topology_mode}
                        acceleratorLabel={acceleratorLabel}
                        clientTelemetrySource={clientTelemetrySource}
                        configuredConcurrency={configuredConcurrency}
                    />

                    <LiveLogPanel logs={combinedLogs} />
                </>
            )}

            {runtimeTab === 'live-flow' && (
                <LiveDeploymentFlowTab
                    targetDeployments={targetDeployments}
                    fallbackClusterId={task?.endpoint_cluster_id}
                />
            )}

            {runtimeTab === 'logs' && (
                <div className="space-y-5">
                    <LiveLogPanel logs={combinedLogs} defaultExpanded />
                    <Panel
                        title="Run Configuration"
                        help="Active endpoint, workload, and backend settings governing this live simulation run."
                    >
                        <div className="grid gap-4 xl:grid-cols-2">
                            <ConfigurationGroup
                                title="Endpoint"
                                help="Target inference endpoint, model identifier, and response streaming mode."
                                items={[
                                    ['Endpoint mode', task?.endpoint_mode ? displayName(task.endpoint_mode) : 'External', 'Whether requests target a cluster-managed Model Service or an external HTTP(S) URL.'],
                                    ['Endpoint', task?.endpoint_url, 'Target inference URL receiving live benchmark traffic.'],
                                    ['Model', task?.model_name, 'Model identifier sent in each inference request payload.'],
                                    ['Streaming', task?.simulation?.stream === undefined ? null : task.simulation.stream ? 'Enabled' : 'Disabled', 'Whether SSE token streaming is enabled so TTFT and TPOT can be measured.'],
                                ]}
                            />
                            <ConfigurationGroup
                                title="Replay Workload"
                                help="Scenario, dataset, backend engine, and timing window parameters."
                                items={[
                                    ['Scenario', scenarioDisplayName(task?.scenario), 'Workload category (Chat, Tool & API Use, or Code Generation) for this run.'],
                                    ['Dataset', taskDataset(task), 'Trace dataset or corpus being replayed against the target endpoint.'],
                                    ['Backend', displayName(taskBackend(task)), 'Load generator backend executing the trace replay.'],
                                    ['Trace format', trace.format || task?.trace_format, 'Input trace schema parsed by the replay runner.'],
                                    ['Duration', durationSeconds ? `${durationSeconds} s` : null, 'Configured active replay duration before the grace drain window.', 'Effective_Window_s = Run_Duration_s + Grace_Period_s'],
                                    ['Warmup window', warmupSeconds ? `${warmupSeconds} s` : '0 s', 'Initial seconds excluded from steady-state profiling KPIs.', 'Profiling_Window = [Warmup_s, Run_Duration_s + Grace_Period_s]'],
                                ]}
                            />
                            {backendOptionEntries.length > 0 && (
                                <ConfigurationGroup
                                    title="Backend Options"
                                    help="Backend-specific concurrency, threading, and execution parameters for this run."
                                    items={backendOptionEntries.map(([key, value]) => [
                                        backendOptionLabel(key),
                                        backendOptionValue(key, value),
                                        liveBackendOptionHelp(key),
                                    ])}
                                />
                            )}
                        </div>
                    </Panel>
                </div>
            )}

            {!onOpenErrorInspector && (
                <ErrorInspectorModal
                    isOpen={internalInspectorFilter !== null}
                    onClose={() => setInternalInspectorFilter(null)}
                    taskId={taskId}
                    initialFilter={internalInspectorFilter || 'all'}
                    isRunning
                    taskStartedAt={task?.execution_started_at || task?.started_at}
                />
            )}
        </div>
    );
}

export default LiveMonitorPanel;
