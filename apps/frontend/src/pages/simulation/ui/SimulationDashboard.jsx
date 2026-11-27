import { FALLBACK_SCENARIOS, RUNNING_STATUSES, STOPPABLE_STATUSES, TERMINAL_STATUSES, arrayFrom, scenarioOptions, unwrapTask, formatDuration, formatDurationSeconds, formatNumber, formatCount, formatMilliseconds, formatBytes, displayName, numberAt, taskBackend, taskDataset, taskMetrics, scenarioDisplayName, filterActiveModelTokens, formatModelTokenOptionLabel } from '../model/presentation';
import { requestJson } from '../model/client';

import { PaginationControls } from '../../../shared/ui/PaginationControls';
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
import { createPortal } from 'react-dom';
import {
    Area,
    AreaChart,
    Brush,
    CartesianGrid,
    ComposedChart,
    Line,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import {
    Activity,
    AlertTriangle,
    ArrowRight,
    ArrowUpRight,
    Check,
    CheckCircle2,
    ChevronLeft,
    ChevronRight,
    CircleDashed,
    Cpu,
    Download,
    FileJson,
    Gauge,
    GitCompareArrows,
    Info,
    KeyRound,
    Menu,
    MessageSquareText,
    Plus,
    Play,
    RefreshCw,
    RotateCcw,
    Search,
    Server,
    SlidersHorizontal,
    Square,
    Trash2,
    X,
} from 'lucide-react';
import {
    Badge,
    Button,
    CHART_SERIES,
    Checkbox,
    RefreshButton,
    StatCard,
    StatCardRow,
    EmptyState,
    HelpTooltip,
    Input,
    Label,
    Modal,
    ModuleHeader,
    ModulePage,
    Panel,
    Select,
    Spinner,
    Textarea,
    ToggleGroup,
} from '../../../shared/ui';
import { MultiSelectDropdown } from '../../../shared/ui';
import { cn } from '../../../shared/lib/cn';
import { usePolling } from '../../../shared/lib/usePolling';
import { listModelNames, listModelTokens } from '../../model-service';
import { useAuth } from '../../auth';
import SimulationIcon from './SimulationIcon';
import { MAX_COMPARE_RUNS, RunCompareDrawer } from './Simulation/RunCompareDrawer';

const EMPTY_ADVANCED_FILTERS = {
    backend: '',
    model: '',
    dataset: '',
    hardware: '',
    traceFormat: '',
    endpoint: '',
    createdAfter: '',
    createdBefore: '',
};

// eslint-disable-next-line react-refresh/only-export-components
export function buildSimulationTasksQuery({
    page = 0,
    pageSize = 7,
    statusFilter = '',
    search = '',
    advancedFilters = EMPTY_ADVANCED_FILTERS,
} = {}) {
    const query = new URLSearchParams({
        limit: String(pageSize),
        offset: String(page * pageSize),
    });
    if (statusFilter) query.set('status', statusFilter);
    if (search) query.set('search', search);
    if (advancedFilters.backend) query.set('backend', advancedFilters.backend);
    if (advancedFilters.model) query.set('model', advancedFilters.model);
    if (advancedFilters.dataset) query.set('dataset', advancedFilters.dataset);
    if (advancedFilters.hardware) query.set('hardware', advancedFilters.hardware);
    if (advancedFilters.traceFormat) query.set('trace_format', advancedFilters.traceFormat);
    if (advancedFilters.endpoint?.trim()) query.set('endpoint', advancedFilters.endpoint.trim());
    if (advancedFilters.createdAfter) query.set('created_after', `${advancedFilters.createdAfter}T00:00:00.000Z`);
    if (advancedFilters.createdBefore) query.set('created_before', `${advancedFilters.createdBefore}T23:59:59.999Z`);
    return query;
}

// eslint-disable-next-line react-refresh/only-export-components
export function mergeSimulationFacets(rawFacets) {
    const incoming = rawFacets && typeof rawFacets === 'object' ? rawFacets : {};
    return {
        backends: Array.isArray(incoming.backends) ? incoming.backends : [],
        models: Array.isArray(incoming.models) ? incoming.models : [],
        datasets: Array.isArray(incoming.datasets) ? incoming.datasets : [],
        hardware: Array.isArray(incoming.hardware) ? incoming.hardware : [],
        trace_formats: Array.isArray(incoming.trace_formats) ? incoming.trace_formats : [],
        endpoints: Array.isArray(incoming.endpoints) ? incoming.endpoints : [],
    };
}

// eslint-disable-next-line react-refresh/only-export-components
export function resolveHardwareFacetOptions(facetsHardware, selectedHardware) {
    const options = Array.isArray(facetsHardware) ? facetsHardware.filter(Boolean) : [];
    if (selectedHardware && !options.includes(selectedHardware)) {
        return [selectedHardware, ...options];
    }
    return options;
}

const TASK_WIZARD_STEPS = [
    'Benchmark & Endpoint',
    'Scenario & Prompt',
    'Load & Backend',
    'Task Details',
];

const DEFAULT_RANDOM_SEED = '666';
const DEFAULT_TRAJECTORY_START_MIN_RATIO = '0.25';
const DEFAULT_TRAJECTORY_START_MAX_RATIO = '0.75';
const DEFAULT_INFERENCEX_AGENTX_MVP_WEKA_DATASETS = new Set([
    'semianalysis_cc_traces_weka_with_subagents',
    'semianalysis_cc_traces_weka_with_subagents_256k',
    'semianalysis_cc_traces_weka_with_subagents_060226',
    'semianalysis_cc_traces_weka_with_subagents_060226_256k',
    'semianalysis_cc_traces_weka_with_subagents_060526',
    'semianalysis_cc_traces_weka_with_subagents_060526_256k',
    'semianalysis_cc_traces_weka_with_subagents_060826',
    'semianalysis_cc_traces_weka_with_subagents_060826_256k',
    'semianalysis_cc_traces_weka_061326',
    'semianalysis_cc_traces_weka_061326_256k',
    'semianalysis_cc_traces_weka_061526',
    'semianalysis_cc_traces_weka_061526_256k',
    'semianalysis_cc_traces_weka_062126',
    'semianalysis_cc_traces_weka_062126_256k',
]);
const AIPERF_INTEGER_OPTIONS = new Set(['random_seed', 'num_profile_runs', 'max_context_length']);

const AIPERF_OPTION_FORM_KEYS = {
    random_seed: 'randomSeed',
    num_profile_runs: 'numProfileRuns',
    profile_run_cooldown_seconds: 'profileRunCooldownSeconds',
    concurrency: 'aiperfConcurrency',
    max_context_length: 'maxContextLength',
    trace_idle_gap_cap_seconds: 'traceIdleGapCapSeconds',
    trajectory_start_min_ratio: 'trajectoryStartMinRatio',
    trajectory_start_max_ratio: 'trajectoryStartMaxRatio',
};

const AIPERF_OPTION_HELP = {
    random_seed: 'Deterministic PRNG seed (integer ≥ 0, default 666) controlling prompt synthesis and sampling order so repeated runs are reproducible.',
    num_profile_runs: 'Number of consecutive profiling runs to execute back-to-back within this task.',
    profile_run_cooldown_seconds: {
        text: 'Cooldown pause (seconds) between consecutive profiling runs so server queues and KV cache drain.',
        formula: 'Total_Elapsed_s ≈ num_runs × Duration_s + (num_runs − 1) × Cooldown_s',
    },
    concurrency: {
        text: 'Maximum simultaneous in-flight requests maintained by AIPerf (used for non-fixed-schedule or public datasets).',
        formula: 'Active_InFlight_Slots ≤ Concurrency',
    },
    max_context_length: {
        text: 'Upper token limit enforced on combined prompt input and generated output sequence lengths.',
        formula: 'Prompt_Tokens + Output_Tokens ≤ Max_Context_Length',
    },
    trace_idle_gap_cap_seconds: {
        text: 'Maximum inter-arrival idle gap (seconds) preserved from the source trace before clamping long pauses.',
        formula: 'Effective_Idle_Gap_s = min(Source_Δt_s, Idle_Gap_Cap_s)',
    },
    trajectory_start_min_ratio: {
        text: 'Minimum normalized progress ratio [0, 1] along each source agentic trajectory where replay sessions may begin (--trajectory-start-min-ratio). Clear to use the scenario default (0.0).',
        formula: '0 ≤ Trajectory_Start_Min_Ratio ≤ Trajectory_Start_Max_Ratio ≤ 1',
    },
    trajectory_start_max_ratio: {
        text: 'Maximum normalized progress ratio [0, 1] along each source agentic trajectory where replay sessions may begin (--trajectory-start-max-ratio). Clear to use the scenario default (1.0).',
        formula: '0 ≤ Trajectory_Start_Min_Ratio ≤ Trajectory_Start_Max_Ratio ≤ 1',
    },
};

function aiperfOptionHelp(option) {
    const fallback = AIPERF_OPTION_HELP[option?.name];
    if (option?.description) {
        return fallback && typeof fallback === 'object' && fallback.formula
            ? { text: option.description, formula: fallback.formula }
            : option.description;
    }
    return fallback || `AIPerf backend option (${option?.label || option?.name}).`;
}

function createShortId() {
    return globalThis.crypto?.randomUUID?.().slice(0, 8)
        || Date.now().toString(36).slice(-8);
}

// eslint-disable-next-line react-refresh/only-export-components
export function createSimulationFormState(task = null) {
    const sim = task?.simulation || {};
    const rawBackend = String(sim.backend || task?.backend || '').trim().toLowerCase();
    const isLegacyTraceReplayer = rawBackend === 'trace-replayer' || rawBackend === 'trace_replayer';
    const traceFormat = task?.prompt?.trace?.format || task?.trace_format || '';
    const backendOpts = sim.backend_options || task?.backend_options || task?.config_snapshot?.backend_options || {};
    const rawFixedSchedule = task?.prompt?.trace?.fixed_schedule
        ?? task?.fixed_schedule
        ?? task?.config_snapshot?.fixed_schedule;
    const hasExplicitConcurrency = backendOpts.concurrency !== undefined
        && backendOpts.concurrency !== null
        && backendOpts.concurrency !== '';
    const fixedSchedule = isLegacyTraceReplayer
        ? true
        : (traceFormat === 'weka_public_dataset' || hasExplicitConcurrency)
            ? false
            : (rawFixedSchedule != null ? Boolean(rawFixedSchedule) : true);
    const randomSeed = isLegacyTraceReplayer
        ? DEFAULT_RANDOM_SEED
        : (backendOpts.random_seed != null && backendOpts.random_seed !== ''
            ? String(backendOpts.random_seed)
            : DEFAULT_RANDOM_SEED);
    const resolveTrajectoryRatio = (key, defaultValue) => {
        if (!task) return defaultValue;
        if (isLegacyTraceReplayer) return '';
        const val = backendOpts[key];
        return val != null && String(val).trim() !== '' ? String(val) : '';
    };
    return {
        scenario: task?.scenario || 'chat',
        backend: 'aiperf',
        dataset: task?.dataset || task?.prompt?.dataset?.name || '',
        tracePath: task?.prompt?.trace?.path || task?.trace_path || '',
        traceFormat,
        endpointMode: task?.endpoint_mode || 'deployment',
        endpointModelServiceId: task?.model_service_group_id || '',
        endpointUrl: task?.endpoint_url || '',
        modelAccessTokenId: task?.model_access_token_id || '',
        modelName: task?.model_name || '',
        runName: '',
        description: '',
        durationSeconds: String(sim.duration_seconds ?? 60),
        gracePeriodSeconds: String(sim.grace_period_seconds ?? task?.grace_period_seconds ?? 30),
        scaleFactor: String(task?.prompt?.trace?.synthesis_speedup_ratio ?? task?.scale_factor ?? 1),
        traceStartSeconds: String(task?.prompt?.trace?.start_seconds ?? 0),
        traceEndSeconds: task?.prompt?.trace?.end_seconds != null ? String(task.prompt.trace.end_seconds) : '',
        traceTimeoutSeconds: String(sim.trace_timeout_seconds ?? 3600),
        warmupSeconds: String(backendOpts.warmup_seconds ?? 0),
        ttftSlo: backendOpts.ttft_slo != null ? String(Number(backendOpts.ttft_slo) * 1000) : '',
        tpotSlo: backendOpts.tpot_slo != null ? String(Number(backendOpts.tpot_slo) * 1000) : '',
        e2eSlo: backendOpts.e2e_slo != null ? String(backendOpts.e2e_slo) : '',
        errorRateSlo: backendOpts.error_rate_slo != null ? String(backendOpts.error_rate_slo) : '',
        collectServerTelemetry: backendOpts.collect_server_telemetry ?? true,
        randomSeed,
        numProfileRuns: String(backendOpts.num_profile_runs ?? 1),
        profileRunCooldownSeconds: backendOpts.profile_run_cooldown_seconds != null ? String(backendOpts.profile_run_cooldown_seconds) : '',
        fixedSchedule,
        aiperfConcurrency: (!isLegacyTraceReplayer && hasExplicitConcurrency) ? String(backendOpts.concurrency) : '',
        maxContextLength: backendOpts.max_context_length != null ? String(backendOpts.max_context_length) : '',
        traceIdleGapCapSeconds: backendOpts.trace_idle_gap_cap_seconds != null ? String(backendOpts.trace_idle_gap_cap_seconds) : '',
        trajectoryStartMinRatio: resolveTrajectoryRatio('trajectory_start_min_ratio', DEFAULT_TRAJECTORY_START_MIN_RATIO),
        trajectoryStartMaxRatio: resolveTrajectoryRatio('trajectory_start_max_ratio', DEFAULT_TRAJECTORY_START_MAX_RATIO),
    };
}

function finiteNum(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function buildLiveTaskTimeline(liveSummary) {
    if (!liveSummary || typeof liveSummary !== 'object') return [];
    const completion = Array.isArray(liveSummary.completion_timeline) ? liveSummary.completion_timeline : [];
    const throughput = Array.isArray(liveSummary.throughput_timeline) ? liveSummary.throughput_timeline : [];
    if (completion.length === 0 && throughput.length === 0) return [];

    const tpByWindow = new Map();
    throughput.forEach((point, idx) => {
        if (!point || typeof point !== 'object') return;
        const start = finiteNum(point.start_seconds);
        const end = finiteNum(point.end_seconds);
        const key = start !== null && end !== null ? `${start}:${end}` : `idx:${idx}`;
        tpByWindow.set(key, point);
    });

    const baseList = completion.length > 0 ? completion : throughput;
    return baseList
        .map((point, idx) => {
            if (!point || typeof point !== 'object') return null;
            const start = finiteNum(point.start_seconds);
            const end = finiteNum(point.end_seconds);
            const key = start !== null && end !== null ? `${start}:${end}` : `idx:${idx}`;
            const tp = completion.length > 0 ? (tpByWindow.get(key) || throughput[idx] || null) : point;
            const comp = completion.length > 0 ? point : null;

            const span = start !== null && end !== null && end > start ? end - start : null;
            const completed = finiteNum(comp?.completed_requests) ?? finiteNum(tp?.completed_requests);
            const failed = finiteNum(comp?.failed_requests) ?? 0;
            const successful = finiteNum(comp?.successful_requests)
                ?? (completed !== null ? Math.max(0, completed - failed) : null);
            const rps = finiteNum(comp?.request_completion_rps)
                ?? finiteNum(tp?.request_completion_rps)
                ?? (completed !== null && span !== null ? completed / span : null)
                ?? 0;
            const arrived = finiteNum(comp?.arrived_requests);
            const explicitArrivalRps = finiteNum(comp?.request_arrival_rps) ?? finiteNum(tp?.request_arrival_rps);
            const arrivedRps = explicitArrivalRps
                ?? (arrived !== null ? (span !== null && span > 0 ? arrived / span : arrived) : rps);
            const explicitFailedRps = finiteNum(comp?.failed_rps) ?? finiteNum(tp?.failed_rps);
            const failedRps = explicitFailedRps
                ?? (span !== null && span > 0 ? failed / span : failed);
            const tps = finiteNum(comp?.token_throughput_tps)
                ?? finiteNum(tp?.token_throughput_tps)
                ?? 0;
            const timeSeconds = end ?? start ?? idx;

            return {
                start_seconds: start ?? timeSeconds,
                end_seconds: end ?? timeSeconds,
                time_seconds: timeSeconds,
                arrived_requests: arrived ?? completed ?? rps,
                completed_requests: completed ?? 0,
                successful_requests: successful ?? rps,
                failed_requests: failed,
                arrived_rps: arrivedRps,
                failed_rps: failedRps,
                request_completion_rps: rps,
                token_throughput_tps: tps,
                in_flight: finiteNum(comp?.in_flight) ?? finiteNum(tp?.in_flight) ?? 0,
            };
        })
        .filter(Boolean);
}

// eslint-disable-next-line react-refresh/only-export-components
export function resolveRowMetrics(item, status, itemTimeline = []) {
    const base = taskMetrics(item);
    if (!RUNNING_STATUSES.has(status)) return base;
    const live = item?.live_summary || {};
    const lastPoint = itemTimeline.length > 0 ? itemTimeline[itemTimeline.length - 1] : null;
    const requestRate = base.requestRate
        ?? finiteNum(live.throughput_rps)
        ?? finiteNum(lastPoint?.request_completion_rps);
    const liveTtftP50 = numberAt(live, ['ttft.p50_ms']);
    const liveTtftAvg = numberAt(live, ['ttft.mean_ms']);
    const liveTtftP90 = numberAt(live, ['ttft.p90_ms']);
    const ttft = base.ttft ?? liveTtftP50 ?? liveTtftAvg ?? liveTtftP90;
    const ttftStat = base.ttft !== null
        ? base.ttftStat
        : (liveTtftP50 !== null
            ? 'P50'
            : (liveTtftAvg !== null ? 'Avg' : (liveTtftP90 !== null ? 'P90' : 'P50')));
    const liveTpotP50 = numberAt(live, ['tpot.p50_ms']);
    const liveTpotAvg = numberAt(live, ['tpot.mean_ms']);
    const liveTpotP90 = numberAt(live, ['tpot.p90_ms']);
    const tpot = base.tpot ?? liveTpotP50 ?? liveTpotAvg ?? liveTpotP90;
    const tpotStat = base.tpot !== null
        ? base.tpotStat
        : (liveTpotP50 !== null
            ? 'P50'
            : (liveTpotAvg !== null ? 'Avg' : (liveTpotP90 !== null ? 'P90' : 'P50')));
    const errorRate = base.errorRate
        ?? numberAt(live, ['error_rate_pct', 'error_rate_percent']);
    return {
        ...base,
        requestRate,
        ttft,
        ttftStat,
        tpot,
        tpotStat,
        errorRate,
    };
}

function sloToneClass(value, slo, hasValue) {
    if (!hasValue) return 'text-slate-400 dark:text-slate-600';
    if (slo === null) return 'text-slate-900 dark:text-slate-100';
    return value > slo
        ? 'text-rose-600 dark:text-rose-300'
        : 'text-slate-900 dark:text-slate-100';
}

export function SimulationTaskRow({
    item,
    isCompareSelected = false,
    compareDisabled = false,
    onToggleCompare,
    onOpen,
    onStop,
    onRerun,
    onDelete,
    stoppingTaskId = '',
    rerunningTaskId = '',
    deletingTaskId = '',
}) {
    const id = item.id || item.task_id;
    const status = String(item.status || '').toLowerCase();
    const isRunning = RUNNING_STATUSES.has(status);
    const isQueuedOrPending = status === 'queued' || status === 'pending';
    const liveSummary = item?.live_summary || null;
    const telemetrySource = liveSummary?.telemetry_source || 'none';
    const itemTimeline = buildLiveTaskTimeline(liveSummary);
    const showLiveCurve = status === 'running' || itemTimeline.length > 0;
    const singlePointDot = itemTimeline.length === 1 ? { r: 2.5, strokeWidth: 0 } : false;
    const metrics = resolveRowMetrics(item, status, itemTimeline);

    const [menuOpen, setMenuOpen] = useState(false);
    const menuContainerRef = useRef(null);
    const menuTriggerRef = useRef(null);

    useEffect(() => {
        if (!menuOpen) return undefined;
        const handlePointerDown = (event) => {
            if (menuContainerRef.current && !menuContainerRef.current.contains(event.target)) {
                setMenuOpen(false);
            }
        };
        const handleKeyDown = (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                setMenuOpen(false);
                menuTriggerRef.current?.focus?.();
            }
        };
        document.addEventListener('mousedown', handlePointerDown);
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('mousedown', handlePointerDown);
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [menuOpen]);

    const itemProgress = Math.max(
        0,
        Math.min(
            100,
            Math.max(
                finiteNum(item.progress_percent) ?? finiteNum(item.progress) ?? 0,
                finiteNum(liveSummary?.progress_percent) ?? 0,
            ),
        ),
    );

    const statusTone = (status === 'completed' || status === 'succeeded')
        ? 'success'
        : (status === 'failed' || status === 'cancelled' || status === 'canceled')
            ? 'danger'
            : isRunning
                ? 'active'
                : 'neutral';
    const statusStyle = statusTone === 'success'
        ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
        : statusTone === 'danger'
            ? 'border-rose-500/30 bg-rose-500/10 text-rose-300'
            : statusTone === 'active'
                ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300'
                : 'border-slate-700 text-slate-400';
    const StatusIcon = statusTone === 'success'
        ? CheckCircle2
        : statusTone === 'danger'
            ? AlertTriangle
            : isRunning
                ? Activity
                : CircleDashed;

    const sim = item.simulation || {};
    const backendOpts = sim.backend_options || item.backend_options || {};
    const backendKey = taskBackend(item);
    const normalizedBackend = String(backendKey || '').trim().toLowerCase();
    const isLegacyTraceReplayer = normalizedBackend === 'trace-replayer' || normalizedBackend === 'trace_replayer';
    const backendLabel = displayName(backendKey);
    const datasetName = taskDataset(item);
    const hardwareLabel = typeof item.hardware === 'string' ? item.hardware.trim() : '';
    const tracePath = item.prompt?.trace?.path || item.trace_path || '';
    const traceFormat = item.prompt?.trace?.format || item.trace_format || '';
    const endpointUrl = sim.endpoint_url || item.endpoint_url || '';
    const targetEndpoint = item.target_endpoint_name
        || item.model_service_name
        || sim.endpoint_url
        || item.endpoint_url
        || item.endpoint
        || '';
    const subtitleModel = item.model_name || 'Model not recorded';
    const subtitleText = targetEndpoint ? `${subtitleModel} · ${targetEndpoint}` : subtitleModel;

    const configuredDuration = finiteNum(sim.duration_seconds) ?? finiteNum(item.duration_seconds);
    const warmupSeconds = finiteNum(backendOpts.warmup_seconds);
    const gracePeriodSeconds = finiteNum(sim.grace_period_seconds ?? item.grace_period_seconds);
    const concurrency = finiteNum(backendOpts.concurrency);
    const scaleFactor = finiteNum(item.prompt?.trace?.synthesis_speedup_ratio ?? item.scale_factor);
    const createdAtLabel = item.created_at ? new Date(item.created_at).toLocaleString() : '—';
    const pacingSummary = concurrency !== null
        ? `Concurrency ${formatNumber(concurrency)}`
        : scaleFactor !== null
            ? `Speedup ${formatNumber(scaleFactor)}x`
            : '';

    const rawFixedSchedule = item.prompt?.trace?.fixed_schedule
        ?? item.fixed_schedule
        ?? item.config_snapshot?.fixed_schedule;
    // Defensive historical compatibility: legacy trace-replayer tasks always ran on fixed schedule.
    const isFixedSchedule = isLegacyTraceReplayer
        ? true
        : (traceFormat === 'weka_public_dataset' || concurrency !== null)
            ? false
            : (rawFixedSchedule != null ? Boolean(rawFixedSchedule) : true);
    const e2eSlo = finiteNum(backendOpts.e2e_slo);
    const sloParts = [];
    if (metrics.ttftSloMs !== null) sloParts.push(`TTFT ≤ ${formatMilliseconds(metrics.ttftSloMs)}`);
    if (metrics.tpotSloMs !== null) sloParts.push(`TPOT ≤ ${formatMilliseconds(metrics.tpotSloMs)}`);
    if (e2eSlo !== null) sloParts.push(`E2E ≤ ${formatNumber(e2eSlo, ' s')}`);
    if (metrics.errorRateSlo !== null) sloParts.push(`Error ≤ ${formatNumber(metrics.errorRateSlo, '%')}`);
    const maxContextLength = finiteNum(backendOpts.max_context_length);
    const randomSeed = finiteNum(backendOpts.random_seed);
    const numProfileRuns = finiteNum(backendOpts.num_profile_runs);
    const descriptionText = typeof item.description === 'string' ? item.description.trim() : '';

    const lastPoint = itemTimeline.length > 0 ? itemTimeline[itemTimeline.length - 1] : null;
    const liveRps = metrics.requestRate ?? finiteNum(lastPoint?.request_completion_rps);
    const liveTps = finiteNum(liveSummary?.throughput_tps)
        ?? finiteNum(liveSummary?.output_token_throughput?.avg)
        ?? finiteNum(lastPoint?.token_throughput_tps);

    const elapsedSeconds = finiteNum(liveSummary?.elapsed_seconds);
    const kpiCaption = isRunning
        ? (isQueuedOrPending && itemTimeline.length === 0
            ? 'Queued · waiting to start'
            : `Live telemetry · ${elapsedSeconds !== null ? formatDurationSeconds(elapsedSeconds) : formatDuration(item, liveSummary)}${telemetrySource === 'server_fallback' ? ' · server fallback' : ''}`)
        : `Saved summary · ${formatDuration(item, item?.result?.summary || item?.summary)}`;

    const errorText = item.error || item.error_message || '';
    const errorRateTone = metrics.errorRate === null
        ? 'text-slate-400 dark:text-slate-600'
        : metrics.errorRateSlo === null
            ? (metrics.errorRate > 0 ? 'text-rose-600 dark:text-rose-300' : 'text-slate-900 dark:text-slate-100')
            : (metrics.errorRate > metrics.errorRateSlo ? 'text-rose-600 dark:text-rose-300' : 'text-slate-900 dark:text-slate-100');

    const kpiTiles = [
        {
            label: 'Req/s',
            corners: 'rounded-tl-lg sm:rounded-bl-lg',
            helpLabel: 'Request throughput help',
            formula: 'Throughput (req/s) = Completed_Requests / Effective_Window_s',
            helpBody: 'Average completed requests per second across the run. Higher values indicate greater serving throughput.',
            valueClass: metrics.requestRate === null ? 'text-slate-400 dark:text-slate-600' : 'text-slate-900 dark:text-slate-100',
            value: formatNumber(metrics.requestRate),
        },
        {
            label: `TTFT ${metrics.ttftStat || 'P50'}`,
            corners: 'rounded-tr-lg sm:rounded-tr-none',
            helpLabel: 'TTFT help',
            formula: metrics.ttftStat === 'Avg'
                ? 'TTFT (ms) = mean(t_first_token − t_request_start); Pass iff TTFT ≤ TTFT_SLO'
                : metrics.ttftStat === 'P90'
                    ? 'TTFT P90 (ms) = Quantile_0.90(t_first_token − t_request_start); Pass iff TTFT ≤ TTFT_SLO'
                    : 'TTFT P50 (ms) = Quantile_0.50(t_first_token − t_request_start); Pass iff TTFT ≤ TTFT_SLO',
            helpBody: metrics.ttftStat === 'Avg'
                ? 'Mean Time To First Token (ms) from dispatch to first response token. Neutral within TTFT SLO and highlighted red when over SLO.'
                : metrics.ttftStat === 'P90'
                    ? '90th percentile Time To First Token (ms) from dispatch to first response token. Neutral within TTFT SLO and highlighted red when over SLO.'
                    : 'Median (P50) Time To First Token (ms) from dispatch to first response token. Neutral within TTFT SLO and highlighted red when over SLO.',
            valueClass: sloToneClass(metrics.ttft, metrics.ttftSloMs, metrics.ttft !== null),
            value: formatMilliseconds(metrics.ttft),
        },
        {
            label: `TPOT ${metrics.tpotStat || 'P50'}`,
            corners: 'rounded-bl-lg sm:rounded-bl-none',
            helpLabel: 'TPOT help',
            formula: metrics.tpotStat === 'Avg'
                ? 'TPOT (ms) = mean((t_last_token − t_first_token) / max(1, Output_Tokens − 1)); Pass iff TPOT ≤ TPOT_SLO'
                : metrics.tpotStat === 'P90'
                    ? 'TPOT P90 (ms) = Quantile_0.90((t_last_token − t_first_token) / max(1, Output_Tokens − 1)); Pass iff TPOT ≤ TPOT_SLO'
                    : 'TPOT P50 (ms) = Quantile_0.50((t_last_token − t_first_token) / max(1, Output_Tokens − 1)); Pass iff TPOT ≤ TPOT_SLO',
            helpBody: metrics.tpotStat === 'Avg'
                ? 'Mean Time Per Output Token (ms) during decode streaming. Lower values mean faster token generation; neutral within TPOT SLO and highlighted red when over SLO.'
                : metrics.tpotStat === 'P90'
                    ? '90th percentile Time Per Output Token (ms) during decode streaming. Lower values mean faster token generation; neutral within TPOT SLO and highlighted red when over SLO.'
                    : 'Median (P50) Time Per Output Token (ms) during decode streaming. Lower values mean faster token generation; neutral within TPOT SLO and highlighted red when over SLO.',
            valueClass: sloToneClass(metrics.tpot, metrics.tpotSloMs, metrics.tpot !== null),
            value: formatMilliseconds(metrics.tpot),
        },
        {
            label: 'Error Rate',
            corners: 'rounded-br-lg sm:rounded-tr-lg',
            helpLabel: 'Error rate help',
            formula: 'Error_Rate (%) = (Failed_Requests / Total_Dispatched_Requests) × 100; Pass iff Error_Rate ≤ Error_SLO',
            helpBody: 'Percentage of dispatched requests that failed (HTTP errors, timeouts, or transport failures); neutral within Error Rate SLO and highlighted red when over SLO.',
            valueClass: errorRateTone,
            value: formatNumber(metrics.errorRate, '%'),
        },
    ];

    const detailItems = [
        { label: 'Created', value: createdAtLabel },
        { label: 'ID', value: id || '—' },
        item.endpoint_mode ? {
            label: 'Endpoint mode',
            value: item.endpoint_mode === 'deployment' ? 'Published service' : 'External URL',
        } : null,
        { label: 'Schedule', value: isFixedSchedule ? 'Fixed schedule' : 'Concurrency slots' },
        endpointUrl && endpointUrl !== targetEndpoint ? {
            label: 'Endpoint URL',
            value: endpointUrl,
            valueClassName: 'sim-endpoint-link dark:text-cyan-300',
        } : null,
        tracePath && tracePath !== datasetName ? { label: 'Trace path', value: tracePath } : null,
        traceFormat ? { label: 'Trace format', value: traceFormat } : null,
        warmupSeconds !== null && warmupSeconds > 0 ? { label: 'Warmup', value: formatDurationSeconds(warmupSeconds) } : null,
        gracePeriodSeconds !== null && gracePeriodSeconds > 0 ? { label: 'Drain grace', value: formatDurationSeconds(gracePeriodSeconds) } : null,
        sloParts.length > 0 ? { label: 'SLOs', value: sloParts.join(' · '), wrapWords: true } : null,
        maxContextLength !== null ? { label: 'Max context', value: formatCount(maxContextLength) } : null,
        randomSeed !== null ? { label: 'Seed', value: randomSeed } : null,
        numProfileRuns !== null && numProfileRuns > 1 ? { label: 'Profile runs', value: formatCount(numProfileRuns) } : null,
        descriptionText ? { label: 'Description', value: descriptionText, wrapWords: true, fullWidth: true } : null,
    ].filter(Boolean);

    const handleRowClick = (event) => {
        if (!onOpen || event.target?.closest?.('button, details, summary, input, label, a')) return;
        onOpen(item);
    };

    const handleRowKeyDown = (event) => {
        if ((event.key === 'Enter' || event.key === ' ') && !event.target?.closest?.('button, details, summary, input, label, a')) {
            event.preventDefault();
            onOpen?.(item);
        }
    };

    return (
        <article
            role="button"
            tabIndex={0}
            data-tone={statusTone}
            data-compare-selected={isCompareSelected ? 'true' : 'false'}
            onClick={handleRowClick}
            onKeyDown={handleRowKeyDown}
            className={cn(
                'simulation-task-card relative cursor-pointer overflow-visible rounded-xl border bg-white transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/50',
                menuOpen ? 'z-30' : 'hover:z-20 focus-within:z-20',
                isCompareSelected
                    ? 'border-cyan-500/60 bg-cyan-500/5 dark:border-cyan-500/50 dark:bg-cyan-500/10'
                    : isRunning
                        ? 'border-slate-200 dark:border-cyan-500/35'
                        : 'border-slate-200 hover:border-slate-300 dark:border-slate-800 dark:hover:border-slate-700',
            )}
        >
            <div className="flex flex-col gap-4 p-4 xl:flex-row xl:items-center xl:gap-6">
                <div className="flex min-w-0 flex-1 items-start gap-3">
                    {TERMINAL_STATUSES.has(status) ? (
                        <span
                            onClick={(event) => event.stopPropagation()}
                            onKeyDown={(event) => event.stopPropagation()}
                            className="mt-2.5 flex w-4 shrink-0 items-center justify-center"
                        >
                            <Checkbox
                                className="simulation-compare-check dark:accent-cyan-500"
                                aria-label={`Select ${item.name || id} for comparison`}
                                checked={isCompareSelected}
                                disabled={compareDisabled}
                                onChange={() => onToggleCompare?.(id)}
                            />
                        </span>
                    ) : (
                        <span aria-hidden="true" className="mt-2.5 w-4 shrink-0" />
                    )}
                    <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2.5">
                            <span className={cn('sim-status-badge flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border', statusStyle)}>
                                <StatusIcon size={17} className={isRunning ? 'animate-pulse' : undefined} />
                            </span>
                            <div className="min-w-0 flex-1">
                                <div className="flex min-w-0 items-center gap-2">
                                    <h3 className="truncate text-sm font-semibold leading-snug text-slate-900 dark:text-slate-100">
                                        {item.name || 'Simulation task'}
                                    </h3>
                                    <span className={cn('sim-status-badge shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium leading-tight', statusStyle)}>
                                        {displayName(status)}
                                    </span>
                                </div>
                                <p className="mt-0.5 truncate text-xs text-slate-500 dark:text-slate-400" title={subtitleText}>
                                    {subtitleText}
                                </p>
                            </div>
                        </div>

                        <div className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1.5 text-[11px]">
                            <span className="sim-scenario-chip rounded-md border px-2 py-0.5 font-medium dark:border-violet-500/20 dark:bg-violet-500/[0.07] dark:text-violet-200">
                                {scenarioDisplayName(item.scenario) || '—'}
                            </span>
                            <span className="sim-workload-chip rounded-md border px-2 py-0.5 dark:border-slate-800 dark:bg-transparent dark:text-slate-400">
                                {backendLabel}
                            </span>
                            <span
                                className="sim-workload-chip max-w-56 truncate rounded-md border px-2 py-0.5 dark:border-slate-800 dark:bg-transparent dark:text-slate-400"
                                title={datasetName}
                            >
                                {datasetName}
                            </span>
                            {hardwareLabel && (
                                <span
                                    className="sim-workload-chip inline-flex max-w-56 items-center gap-1 rounded-md border px-2 py-0.5 dark:border-slate-800 dark:bg-transparent dark:text-slate-400"
                                    title={hardwareLabel}
                                >
                                    <Cpu size={11} className="shrink-0" aria-hidden="true" />
                                    <span className="min-w-0 truncate">{hardwareLabel}</span>
                                </span>
                            )}
                            {configuredDuration !== null && (
                                <span
                                    className="font-mono text-[11px] text-slate-500 dark:text-slate-400"
                                    title="Configured benchmark duration"
                                >
                                    Duration {formatDurationSeconds(configuredDuration)}
                                </span>
                            )}
                            {pacingSummary && (
                                <span
                                    className="sim-pacing-fact font-mono text-[11px] font-medium dark:text-cyan-300/80"
                                    title="Configured pacing or concurrency"
                                >
                                    {pacingSummary}
                                </span>
                            )}
                        </div>

                        {isRunning && (
                            <div className="mt-3 space-y-2">
                                {showLiveCurve && (
                                    <div
                                        data-testid="live-telemetry-curve"
                                        className="sim-live-curve-box flex h-14 items-center gap-3 rounded-lg border px-3 py-1.5 dark:border-cyan-500/25"
                                    >
                                        <div className="flex shrink-0 flex-col justify-center gap-0.5">
                                            <div className="flex items-center gap-1.5">
                                                <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-emerald-700 dark:text-emerald-300">
                                                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500 dark:bg-emerald-400" />
                                                    LIVE
                                                </span>
                                                {telemetrySource === 'server_fallback' && (
                                                    <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[9px] font-medium text-sky-700 dark:text-cyan-300">
                                                        Server telemetry
                                                    </span>
                                                )}
                                            </div>
                                            <div className="flex items-center gap-1.5 font-mono text-[10px] tabular-nums text-slate-700 dark:text-slate-200">
                                                <span>{formatNumber(liveRps)} req/s</span>
                                                <span className="text-slate-400 dark:text-slate-600">·</span>
                                                <span>{formatNumber(liveTps)} tok/s</span>
                                            </div>
                                        </div>
                                        <div className="h-full min-w-0 flex-1">
                                            {itemTimeline.length > 0 ? (
                                                <ResponsiveContainer width="100%" height="100%">
                                                    <AreaChart data={itemTimeline} margin={{ top: 4, right: 2, left: 2, bottom: 2 }}>
                                                        <Area
                                                            type="monotone"
                                                            dataKey="arrived_rps"
                                                            stroke={CHART_SERIES[1]}
                                                            strokeWidth={1.25}
                                                            fill={CHART_SERIES[1]}
                                                            fillOpacity={0.14}
                                                            dot={singlePointDot}
                                                            isAnimationActive={false}
                                                        />
                                                        <Area
                                                            type="monotone"
                                                            dataKey="request_completion_rps"
                                                            stroke={CHART_SERIES[0]}
                                                            strokeWidth={1.75}
                                                            fill={CHART_SERIES[0]}
                                                            fillOpacity={0.24}
                                                            dot={singlePointDot}
                                                            isAnimationActive={false}
                                                        />
                                                        <Area
                                                            type="monotone"
                                                            dataKey="failed_rps"
                                                            stroke={CHART_SERIES[4]}
                                                            strokeWidth={1.25}
                                                            fill={CHART_SERIES[4]}
                                                            fillOpacity={0.18}
                                                            dot={singlePointDot}
                                                            isAnimationActive={false}
                                                        />
                                                    </AreaChart>
                                                </ResponsiveContainer>
                                            ) : (
                                                <div className="flex h-full items-center justify-center rounded border border-dashed border-slate-300/80 text-[10px] text-slate-500 dark:border-slate-700/80 dark:text-slate-400">
                                                    Sampling live curve…
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                )}
                                <div>
                                    <div className="mb-1 flex items-center justify-between gap-3 text-[10px] text-slate-500 dark:text-slate-400">
                                        <span className="truncate">{item.progress_message || displayName(status)}</span>
                                        <span className="sim-progress-label shrink-0 font-mono tabular-nums dark:text-cyan-300">
                                            {formatNumber(itemProgress, '%')}
                                        </span>
                                    </div>
                                    <div
                                        className="h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800"
                                        role="progressbar"
                                        aria-valuenow={itemProgress}
                                        aria-valuemin="0"
                                        aria-valuemax="100"
                                    >
                                        <div
                                            className="sim-progress-active h-full rounded-full transition-all duration-700 dark:bg-cyan-400"
                                            style={{ width: `${itemProgress}%` }}
                                        />
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>
                </div>

                <div className="xl:w-[400px] xl:shrink-0" title={kpiCaption}>
                    <div className="grid grid-cols-2 gap-px overflow-visible rounded-lg border border-slate-200 bg-slate-200 dark:border-slate-800 dark:bg-slate-800 sm:grid-cols-4">
                        {kpiTiles.map((tile) => (
                            <div
                                key={tile.label}
                                className={cn('sim-kpi-tile min-w-0 overflow-visible bg-white px-3 py-2.5', tile.corners)}
                            >
                                <div
                                    onClick={(event) => event.stopPropagation()}
                                    onKeyDown={(event) => event.stopPropagation()}
                                    className="flex items-center justify-between gap-1 text-[10px] font-medium text-slate-500 dark:text-slate-400"
                                >
                                    <span className="truncate">{tile.label}</span>
                                    <HelpTooltip
                                        label={tile.helpLabel}
                                        align="end"
                                        iconSize={10}
                                        asSpan
                                        className="opacity-60 transition-opacity hover:opacity-100 focus-within:opacity-100"
                                        formula={tile.formula}
                                    >
                                        {tile.helpBody}
                                    </HelpTooltip>
                                </div>
                                <p className={cn('mt-1.5 truncate font-mono text-sm font-semibold tabular-nums', tile.valueClass)}>
                                    {tile.value}
                                </p>
                            </div>
                        ))}
                    </div>
                </div>

                <div
                    ref={menuContainerRef}
                    onClick={(event) => event.stopPropagation()}
                    onKeyDown={(event) => {
                        if (event.key === 'Escape' && menuOpen) {
                            event.preventDefault();
                            setMenuOpen(false);
                            menuTriggerRef.current?.focus?.();
                        }
                        event.stopPropagation();
                    }}
                    className="relative flex shrink-0 items-center"
                >
                    <button
                        ref={menuTriggerRef}
                        type="button"
                        aria-label={`Actions for ${item.name || id}`}
                        aria-haspopup="menu"
                        aria-expanded={menuOpen}
                        title="Actions"
                        onClick={(event) => {
                            event.stopPropagation();
                            setMenuOpen((open) => !open);
                        }}
                        className="sim-action-menu-trigger inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-300 bg-white text-slate-600 transition-colors hover:border-slate-400 hover:text-slate-900 dark:border-slate-700 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:text-slate-100"
                    >
                        <Menu size={16} aria-hidden="true" />
                    </button>
                    <div
                        role="menu"
                        hidden={!menuOpen}
                        aria-label={`Task actions for ${item.name || id}`}
                        className={cn(
                            'sim-action-menu-popover absolute right-0 top-full z-40 mt-1.5 min-w-[168px] rounded-lg border border-slate-200 bg-white p-1 shadow-lg dark:border-slate-800',
                            !menuOpen && 'hidden',
                        )}
                    >
                        <button
                            type="button"
                            role="menuitem"
                            onClick={(event) => {
                                event.stopPropagation();
                                setMenuOpen(false);
                                onOpen?.(item);
                            }}
                            className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-slate-700 transition-colors hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800/80"
                        >
                            <ArrowUpRight size={13} aria-hidden="true" />
                            <span>{isRunning ? 'Live Monitor' : 'View Results'}</span>
                        </button>
                        {isRunning ? (
                            <button
                                type="button"
                                role="menuitem"
                                disabled={!STOPPABLE_STATUSES.has(status) || stoppingTaskId === id}
                                aria-busy={stoppingTaskId === id || undefined}
                                onClick={(event) => {
                                    event.stopPropagation();
                                    setMenuOpen(false);
                                    onStop?.(item);
                                }}
                                className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-amber-600 transition-colors hover:bg-amber-500/10 disabled:opacity-40 dark:text-amber-300"
                            >
                                <Square size={13} aria-hidden="true" />
                                <span>Stop</span>
                            </button>
                        ) : (
                            <>
                                <button
                                    type="button"
                                    role="menuitem"
                                    disabled={rerunningTaskId === id || isLegacyTraceReplayer}
                                    aria-busy={rerunningTaskId === id || undefined}
                                    title={isLegacyTraceReplayer ? 'Legacy trace-replayer backend is no longer supported' : undefined}
                                    onClick={(event) => {
                                        event.stopPropagation();
                                        setMenuOpen(false);
                                        if (isLegacyTraceReplayer) return;
                                        onRerun?.(item);
                                    }}
                                    className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-slate-700 transition-colors hover:bg-slate-100 disabled:opacity-40 dark:text-slate-200 dark:hover:bg-slate-800/80"
                                >
                                    <RotateCcw size={13} aria-hidden="true" />
                                    <span>Rerun</span>
                                </button>
                                <div role="separator" className="my-1 border-t border-slate-200 dark:border-slate-800" />
                                <button
                                    type="button"
                                    role="menuitem"
                                    disabled={deletingTaskId === id}
                                    aria-busy={deletingTaskId === id || undefined}
                                    title="Delete task and all backend data"
                                    onClick={(event) => {
                                        event.stopPropagation();
                                        setMenuOpen(false);
                                        onDelete?.(item);
                                    }}
                                    className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-rose-600 transition-colors hover:bg-rose-500/10 disabled:opacity-40 dark:text-rose-400"
                                >
                                    <Trash2 size={13} aria-hidden="true" />
                                    <span>Delete</span>
                                </button>
                            </>
                        )}
                    </div>
                </div>
            </div>

            {errorText && (
                <p
                    className="mx-4 mb-3 truncate rounded-md bg-rose-500/10 px-2.5 py-1.5 text-[11px] text-rose-600 dark:text-rose-300"
                    title={String(errorText)}
                >
                    {String(errorText)}
                </p>
            )}

            <details
                onClick={(event) => event.stopPropagation()}
                onKeyDown={(event) => event.stopPropagation()}
                className="border-t border-slate-200/80 dark:border-slate-800/70"
            >
                <summary
                    onClick={(event) => event.stopPropagation()}
                    onKeyDown={(event) => event.stopPropagation()}
                    className="cursor-pointer select-none px-4 py-2 text-[11px] font-medium text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
                >
                    Task details
                </summary>
                <div className="grid gap-x-6 gap-y-1.5 px-4 pb-3 pt-0.5 sm:grid-cols-2 lg:grid-cols-3">
                    {detailItems.map((detail) => (
                        <div
                            key={detail.label}
                            className={cn(
                                'flex min-w-0 items-baseline gap-1.5 text-[11px]',
                                detail.fullWidth && 'sm:col-span-2 lg:col-span-3',
                            )}
                        >
                            <span className="shrink-0 text-slate-400 dark:text-slate-500">{detail.label}:</span>
                            <span
                                className={cn(
                                    'min-w-0 font-mono',
                                    detail.wrapWords ? 'break-words' : 'break-all',
                                    detail.valueClassName || 'text-slate-700 dark:text-slate-300',
                                )}
                            >
                                {detail.value}
                            </span>
                        </div>
                    ))}
                </div>
            </details>
        </article>
    );
}

export default function SimulationDashboard({ onNavigate, onToggleMobileNav, embedded = false, initialAction = '' }) {
    const auth = useAuth();
    const isAuthDisabled = auth?.principal?.userId === 'system';
    const canManageModelTokens = auth?.can ? auth.can('model-service:token:manage') : true;
    const [backends, setBackends] = useState([]);
    const [scenarios, setScenarios] = useState(FALLBACK_SCENARIOS);
    const [datasets, setDatasets] = useState([]);
    const [catalogLoading, setCatalogLoading] = useState(true);
    const [catalogError, setCatalogError] = useState('');
    const [errors, setErrors] = useState([]);
    const [actionError, setActionError] = useState('');
    const [datasetDownloadError, setDatasetDownloadError] = useState('');
    const [downloading, setDownloading] = useState(false);
    const [timeline, setTimeline] = useState(null);
    const [timelineLoading, setTimelineLoading] = useState(false);
    const [timelineError, setTimelineError] = useState('');
    const [timelineSelection, setTimelineSelection] = useState({ startIndex: 0, endIndex: 0 });
    const [modelServices, setModelServices] = useState([]);
    const [modelServicesLoading, setModelServicesLoading] = useState(false);
    const [modelServicesError, setModelServicesError] = useState('');
    const [modelTokens, setModelTokens] = useState([]);
    const [modelTokensLoading, setModelTokensLoading] = useState(!isAuthDisabled && canManageModelTokens);
    const [modelTokensError, setModelTokensError] = useState('');
    const [modelTokensForbidden, setModelTokensForbidden] = useState(false);
    const [hasLoadedModelTokens, setHasLoadedModelTokens] = useState(false);
    const [endpointModels, setEndpointModels] = useState([]);
    const [modelsLoading, setModelsLoading] = useState(false);
    const [modelDiscoveryError, setModelDiscoveryError] = useState('');
    const [verifiedEndpoint, setVerifiedEndpoint] = useState(null);
    const [submitting, setSubmitting] = useState(false);
    const [stoppingTaskId, setStoppingTaskId] = useState('');
    const [rerunningTaskId, setRerunningTaskId] = useState('');
    const [deletingTaskId, setDeletingTaskId] = useState('');
    const [taskPendingDelete, setTaskPendingDelete] = useState(null);
    const [deleteError, setDeleteError] = useState('');
    const [tasks, setTasks] = useState([]);
    const [total, setTotal] = useState(0);
    const [counts, setCounts] = useState({
        queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
    });
    const [listLoading, setListLoading] = useState(true);
    const [hasLoadedTasks, setHasLoadedTasks] = useState(false);
    const [page, setPage] = useState(0);
    const [pageSize, setPageSize] = useState(7);
    const [statusFilter, setStatusFilter] = useState('');
    const [searchInput, setSearchInput] = useState('');
    const [search, setSearch] = useState('');
    const [advancedFilters, setAdvancedFilters] = useState(EMPTY_ADVANCED_FILTERS);
    const [draftAdvancedFilters, setDraftAdvancedFilters] = useState(EMPTY_ADVANCED_FILTERS);
    const [isAdvancedOpen, setIsAdvancedOpen] = useState(false);
    const [facets, setFacets] = useState(() => mergeSimulationFacets());
    const [isNewTaskOpen, setIsNewTaskOpen] = useState(initialAction === 'new');
    const [wizardStep, setWizardStep] = useState(0);
    const [taskNameSuffix, setTaskNameSuffix] = useState('');
    const [compareIds, setCompareIds] = useState(() => {
        if (typeof window === 'undefined') return [];
        const raw = new URLSearchParams(window.location.search).get('compare') || '';
        return [...new Set(raw.split(',').map((item) => item.trim()).filter(Boolean))].slice(0, MAX_COMPARE_RUNS);
    });
    const [compareOpen, setCompareOpen] = useState(() => {
        if (typeof window === 'undefined') return false;
        const raw = new URLSearchParams(window.location.search).get('compare') || '';
        const initial = [...new Set(raw.split(',').map((item) => item.trim()).filter(Boolean))].slice(0, MAX_COMPARE_RUNS);
        return initial.length >= 2;
    });
    const [extraCompareTasksById, setExtraCompareTasksById] = useState({});
    const verifiedCompareIdsRef = useRef(new Set());
    const modelDiscoveryRequestRef = useRef(0);
    const modelTokensRequestRef = useRef(0);
    const modelTokensAbortRef = useRef(null);
    const [form, setForm] = useState(() => createSimulationFormState());

    const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

    const toggleCompareId = useCallback((id) => {
        if (!id) return;
        setCompareIds((current) => {
            if (current.includes(id)) return current.filter((item) => item !== id);
            if (current.length >= MAX_COMPARE_RUNS) return current;
            return [...current, id];
        });
    }, []);

    useEffect(() => {
        if (typeof window === 'undefined') return;
        const url = new URL(window.location.href);
        if (compareIds.length > 0) {
            url.searchParams.set('compare', compareIds.join(','));
        } else {
            url.searchParams.delete('compare');
        }
        const nextHref = `${url.pathname}${url.search}${url.hash}`;
        if (nextHref !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
            window.history.replaceState({}, '', nextHref);
        }
    }, [compareIds]);

    const tasksById = useMemo(
        () => ({
            ...extraCompareTasksById,
            ...Object.fromEntries(tasks.map((item) => [item.id || item.task_id, item]).filter(([id]) => Boolean(id))),
        }),
        [extraCompareTasksById, tasks],
    );

    const pruneCompareIds = useCallback((idsToRemove) => {
        if (!Array.isArray(idsToRemove) || idsToRemove.length === 0) return;
        const removeSet = new Set(idsToRemove);
        idsToRemove.forEach((id) => verifiedCompareIdsRef.current.delete(id));
        setCompareIds((current) => {
            const next = current.filter((id) => !removeSet.has(id));
            return next.length === current.length ? current : next;
        });
    }, []);

    useEffect(() => {
        if (compareOpen && compareIds.length < 2) {
            setCompareOpen(false);
        }
    }, [compareIds.length, compareOpen]);

    const activeAdvancedFilterCount = useMemo(
        () => Object.values(advancedFilters).filter((value) => value.trim()).length,
        [advancedFilters],
    );

    useEffect(() => {
        if (!hasLoadedTasks || listLoading || compareIds.length === 0) return undefined;
        const validPageIds = new Set(tasks.map((item) => item.id || item.task_id).filter(Boolean));
        validPageIds.forEach((id) => verifiedCompareIdsRef.current.add(id));

        const isUnfilteredComplete = !statusFilter
            && !search
            && activeAdvancedFilterCount === 0
            && page === 0
            && tasks.length >= total;

        if (isUnfilteredComplete) {
            const filtered = compareIds.filter((id) => validPageIds.has(id));
            if (filtered.length !== compareIds.length) {
                setCompareIds(filtered);
            }
            return undefined;
        }

        const idsToVerify = compareIds.filter(
            (id) => !validPageIds.has(id) && !verifiedCompareIdsRef.current.has(id),
        );
        if (idsToVerify.length === 0) return undefined;

        const controller = new AbortController();
        Promise.allSettled(
            idsToVerify.map((id) => requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(id)}`, { signal: controller.signal })),
        ).then((settled) => {
            if (controller.signal.aborted) return;
            const notFoundIds = [];
            const foundById = {};
            settled.forEach((result, idx) => {
                const id = idsToVerify[idx];
                if (result.status === 'fulfilled') {
                    verifiedCompareIdsRef.current.add(id);
                    const loadedTask = unwrapTask(result.value);
                    if (loadedTask && typeof loadedTask === 'object') {
                        foundById[id] = loadedTask;
                    }
                } else if (result.reason?.status === 404) {
                    notFoundIds.push(id);
                }
            });
            if (Object.keys(foundById).length > 0) {
                setExtraCompareTasksById((prev) => ({ ...prev, ...foundById }));
            }
            if (notFoundIds.length > 0) {
                pruneCompareIds(notFoundIds);
            }
        });
        return () => controller.abort();
    }, [
        activeAdvancedFilterCount,
        compareIds,
        hasLoadedTasks,
        listLoading,
        page,
        pruneCompareIds,
        search,
        statusFilter,
        tasks,
        total,
    ]);

    const activeModelTokens = useMemo(
        () => filterActiveModelTokens(modelTokens),
        [modelTokens],
    );

    const loadModelTokens = useCallback(async () => {
        const requestId = ++modelTokensRequestRef.current;
        modelTokensAbortRef.current?.abort();
        modelTokensAbortRef.current = null;
        if (isAuthDisabled) {
            setModelTokens([]);
            setModelTokensError('');
            setModelTokensForbidden(false);
            setModelTokensLoading(false);
            setHasLoadedModelTokens(true);
            return [];
        }
        if (!canManageModelTokens) {
            setModelTokens([]);
            setModelTokensError('');
            setModelTokensForbidden(true);
            setModelTokensLoading(false);
            setHasLoadedModelTokens(true);
            return [];
        }
        const controller = new AbortController();
        modelTokensAbortRef.current = controller;
        setModelTokensLoading(true);
        setModelTokensError('');
        setModelTokensForbidden(false);
        try {
            const items = await listModelTokens({ signal: controller.signal });
            if (controller.signal.aborted || modelTokensRequestRef.current !== requestId) return [];
            const safeItems = Array.isArray(items) ? items : [];
            const active = filterActiveModelTokens(safeItems);
            setModelTokens(safeItems);
            setForm((current) => {
                const hasSelected = Boolean(
                    current.modelAccessTokenId
                    && active.some((token) => token.id === current.modelAccessTokenId),
                );
                if (hasSelected) return current;
                const nextId = current.endpointMode === 'deployment' ? (active[0]?.id || '') : '';
                return current.modelAccessTokenId === nextId
                    ? current
                    : { ...current, modelAccessTokenId: nextId };
            });
            return active;
        } catch (error) {
            if (error?.name === 'AbortError' || controller.signal.aborted || modelTokensRequestRef.current !== requestId) {
                return [];
            }
            if (error?.status === 401 || error?.status === 403) {
                setModelTokens([]);
                setModelTokensForbidden(true);
                setModelTokensError('');
            } else {
                setModelTokensError(error?.message || 'Unable to load model access tokens.');
            }
            return [];
        } finally {
            if (modelTokensAbortRef.current === controller) {
                modelTokensAbortRef.current = null;
            }
            if (modelTokensRequestRef.current === requestId) {
                setModelTokensLoading(false);
                setHasLoadedModelTokens(true);
            }
        }
    }, [canManageModelTokens, isAuthDisabled]);

    useEffect(() => () => {
        modelTokensRequestRef.current += 1;
        modelTokensAbortRef.current?.abort();
        modelTokensAbortRef.current = null;
    }, []);

    useEffect(() => {
        if (!hasLoadedModelTokens || modelTokensLoading || modelTokensError) return;
        setForm((current) => {
            if (!current.modelAccessTokenId) {
                if (current.endpointMode === 'deployment' && activeModelTokens.length > 0 && !modelTokensForbidden && !isAuthDisabled) {
                    return { ...current, modelAccessTokenId: activeModelTokens[0].id };
                }
                return current;
            }
            const stillActive = !modelTokensForbidden && !isAuthDisabled
                && activeModelTokens.some((token) => token.id === current.modelAccessTokenId);
            if (stillActive) return current;
            const nextId = (!modelTokensForbidden && !isAuthDisabled && current.endpointMode === 'deployment')
                ? (activeModelTokens[0]?.id || '')
                : '';
            return current.modelAccessTokenId === nextId
                ? current
                : { ...current, modelAccessTokenId: nextId };
        });
    }, [
        activeModelTokens,
        form.endpointMode,
        form.modelAccessTokenId,
        hasLoadedModelTokens,
        isAuthDisabled,
        modelTokensError,
        modelTokensForbidden,
        modelTokensLoading,
    ]);

    const resetModelDiscovery = useCallback(() => {
        modelDiscoveryRequestRef.current += 1;
        setVerifiedEndpoint(null);
        setEndpointModels([]);
        setModelDiscoveryError('');
        setModelsLoading(false);
    }, []);

    const chooseEndpointMode = (value) => {
        setModelServicesError('');
        resetModelDiscovery();
        setForm((current) => ({
            ...current,
            endpointMode: value,
            endpointModelServiceId: '',
            endpointUrl: '',
            modelName: '',
            modelAccessTokenId: value === 'deployment' && !isAuthDisabled && !modelTokensForbidden
                ? (activeModelTokens.some((token) => token.id === current.modelAccessTokenId)
                    ? current.modelAccessTokenId
                    : (activeModelTokens[0]?.id || ''))
                : '',
        }));
        if (value === 'deployment') {
            void loadModelServices();
            if (activeModelTokens.length === 0) void loadModelTokens();
        }
    };

    const endpointReady = form.endpointMode === 'deployment' || (
        verifiedEndpoint?.url === form.endpointUrl.trim()
        && verifiedEndpoint?.modelAccessTokenId === form.modelAccessTokenId
        && !modelsLoading && !modelDiscoveryError
    );

    const discoverEndpointModels = useCallback(async (endpointUrl, modelAccessTokenId) => {
        const requestId = ++modelDiscoveryRequestRef.current;
        setVerifiedEndpoint(null);
        setModelsLoading(true);
        setModelDiscoveryError('');
        setEndpointModels([]);
        try {
            const payload = await requestJson('/api/v1/simulation/models/discover', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    endpoint_url: endpointUrl,
                    model_access_token_id: modelAccessTokenId || null,
                }),
            });
            const models = Array.isArray(payload?.models) ? payload.models : [];
            if (!models.length) throw new Error('The endpoint reported no models.');
            if (modelDiscoveryRequestRef.current !== requestId) return;
            setEndpointModels(models);
            setVerifiedEndpoint({ url: endpointUrl, modelAccessTokenId });
            setForm((current) => (
                current.endpointUrl.trim() === endpointUrl && current.modelAccessTokenId === modelAccessTokenId
                    ? { ...current, modelName: models.length === 1 ? models[0] : '' }
                    : current
            ));
        } catch (error) {
            if (modelDiscoveryRequestRef.current !== requestId) return;
            setModelDiscoveryError(`Endpoint validation failed. Task creation is blocked. ${error.message}`);
        } finally {
            if (modelDiscoveryRequestRef.current === requestId) setModelsLoading(false);
        }
    }, []);

    useEffect(() => {
        if (form.endpointMode === 'deployment') return undefined;
        resetModelDiscovery();
        const endpointUrl = form.endpointUrl.trim();
        if (!endpointUrl) {
            resetModelDiscovery();
            return undefined;
        }
        try {
            const endpoint = new URL(endpointUrl);
            if (!['http:', 'https:'].includes(endpoint.protocol)) return undefined;
        } catch {
            return undefined;
        }
        setModelsLoading(true);
        const timeout = window.setTimeout(() => {
            void discoverEndpointModels(endpointUrl, form.modelAccessTokenId);
        }, 500);
        return () => {
            window.clearTimeout(timeout);
            modelDiscoveryRequestRef.current += 1;
        };
    }, [discoverEndpointModels, form.endpointMode, form.endpointUrl, form.modelAccessTokenId, resetModelDiscovery]);

    const updateExternalEndpoint = (value) => {
        resetModelDiscovery();
        setForm((current) => ({
            ...current,
            endpointUrl: value,
            modelName: '',
        }));
    };

    const loadModelServices = async () => {
        if (isAuthDisabled) {
            setModelServices([]);
            setModelServicesLoading(false);
            setModelServicesError('Published Model Services require user authentication (PRISM_AUTH_MODE=local). Switch to Endpoint URL to benchmark an unauthenticated endpoint.');
            resetModelDiscovery();
            setForm((current) => ({ ...current, endpointModelServiceId: '', endpointUrl: '', modelName: '' }));
            return;
        }
        setModelServicesLoading(true);
        setModelServicesError('');
        try {
            const items = await listModelNames();
            setModelServices(items);
            resetModelDiscovery();
            if (!items.length) {
                setForm((current) => ({ ...current, endpointModelServiceId: '', endpointUrl: '', modelName: '' }));
                setModelServicesError('No model service is published and healthy yet. Publish one from the Model Service page first.');
                return;
            }
            selectModelService(items[0].id, items);
        } catch (error) {
            setModelServices([]);
            setModelServicesError(`Unable to load model services: ${error.message}`);
        } finally {
            setModelServicesLoading(false);
        }
    };

    const selectModelService = (modelServiceId, items = modelServices) => {
        setModelServicesError('');
        resetModelDiscovery();
        const entry = items.find((candidate) => candidate.id === modelServiceId);
        if (!entry) {
            setForm((current) => ({ ...current, endpointModelServiceId: '', endpointUrl: '', modelName: '' }));
            return;
        }
        // The backend resolves the actual in-cluster endpoint from the published
        // Model Service (always the first healthy member); this placeholder URL
        // satisfies request validation and is discarded once the server re-resolves it.
        const modelName = entry.name || entry.baseModel || '';
        setForm((current) => ({
            ...current,
            endpointModelServiceId: modelServiceId,
            endpointUrl: 'http://model-service.internal',
            modelName,
        }));
        if (modelName) setEndpointModels([modelName]);
    };

    const loadTasks = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet) setListLoading(true);
        try {
            const query = buildSimulationTasksQuery({
                page,
                pageSize,
                statusFilter,
                search,
                advancedFilters,
            });
            const payload = await requestJson(`/api/v1/simulation/tasks?${query.toString()}`);
            const nextTasks = arrayFrom(payload, ['tasks', 'items', 'data']);
            setTasks(nextTasks);
            setHasLoadedTasks(true);
            const nextTotal = Number(payload?.total ?? nextTasks.length);
            setTotal(nextTotal);
            if (page > 0 && nextTasks.length === 0 && nextTotal > 0) {
                setPage(Math.max(0, Math.ceil(nextTotal / pageSize) - 1));
            }
            setCounts({
                queued: Number(payload?.counts?.queued ?? nextTasks.filter((item) => item.status === 'queued').length),
                running: Number(payload?.counts?.running ?? nextTasks.filter((item) => item.status === 'running').length),
                completed: Number(payload?.counts?.completed ?? nextTasks.filter((item) => item.status === 'completed').length),
                failed: Number(payload?.counts?.failed ?? nextTasks.filter((item) => item.status === 'failed').length),
                cancelled: Number(payload?.counts?.cancelled ?? nextTasks.filter((item) => ['cancelled', 'canceled', 'stopped'].includes(item.status)).length),
            });
            setFacets(mergeSimulationFacets(payload?.facets));
        } catch (error) {
            setActionError(`Unable to load simulation tasks: ${error.message}`);
        } finally {
            if (!quiet) setListLoading(false);
        }
    }, [advancedFilters, page, pageSize, search, statusFilter]);

    const openTaskDetails = (item) => {
        const id = item?.id || item?.task_id;
        if (!id) return;
        onNavigate?.('optimization-simulate-details', { taskId: id });
    };

    const loadCatalogs = useCallback(async () => {
        setCatalogLoading(true);
        setCatalogError('');
        const results = await Promise.allSettled([
            requestJson('/api/v1/simulation/backends'),
            requestJson('/api/v1/simulation/scenarios'),
            requestJson('/api/v1/simulation/trace-datasets'),
        ]);
        const messages = [];
        if (results[0].status === 'fulfilled') {
            const items = arrayFrom(results[0].value, ['backends', 'items', 'data']);
            setBackends(items);
            setForm((current) => ({
                ...current,
                backend: items.some((item) => (
                    (item.name || item.id || item.value) === current.backend
                ))
                    ? current.backend
                    : items.some((item) => (
                        (item.name || item.id || item.value) === 'aiperf'
                    ))
                        ? 'aiperf'
                        : (items[0]?.name || items[0]?.id || items[0]?.value || ''),
                scenario: current.scenario || 'chat',
            }));
        } else {
            messages.push(`Backends: ${results[0].reason.message}`);
        }
        if (results[1].status === 'fulfilled') {
            setScenarios(scenarioOptions(results[1].value));
        } else {
            setScenarios(FALLBACK_SCENARIOS);
            messages.push(`Scenarios: ${results[1].reason.message}`);
        }
        if (results[2].status === 'fulfilled') {
            setDatasets(arrayFrom(results[2].value, ['datasets', 'items', 'data']));
        } else {
            messages.push(`Trace datasets: ${results[2].reason.message}`);
        }
        setCatalogError(messages.join(' · '));
        setCatalogLoading(false);
    }, []);

    useEffect(() => {
        loadCatalogs();
    }, [loadCatalogs]);

    useEffect(() => {
        void loadModelServices();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useEffect(() => {
        void loadModelTokens();
    }, [loadModelTokens]);

    useEffect(() => {
        const timeout = window.setTimeout(() => {
            setPage(0);
            setSearch(searchInput.trim());
        }, 250);
        return () => window.clearTimeout(timeout);
    }, [searchInput]);

    useEffect(() => {
        loadTasks();
    }, [loadTasks]);

    const hasActiveTasks = counts.queued > 0 || counts.running > 0
        || tasks.some((item) => RUNNING_STATUSES.has(String(item.status || '').toLowerCase()));

    usePolling(() => loadTasks({ quiet: true }), {
        intervalMs: 1000,
        enabled: hasActiveTasks,
    });

    const selectedDataset = useMemo(
        () => datasets.find((item) => (item.name || item.id || item.value) === form.dataset),
        [datasets, form.dataset]
    );
    const selectedBackend = useMemo(
        () => backends.find((item) => (item.name || item.id || item.value) === form.backend),
        [backends, form.backend]
    );
    const selectedModelService = useMemo(
        () => modelServices.find((item) => item.id === form.endpointModelServiceId),
        [modelServices, form.endpointModelServiceId]
    );
    const incompatibleDeploymentGuideError = (
        form.endpointMode === 'deployment'
        && selectedModelService?.guide
        && (selectedBackend?.capabilities?.incompatible_deployment_guides || []).includes(selectedModelService.guide)
    )
        ? `${selectedBackend?.display_name || displayName(form.backend)} does not support the "${selectedModelService.guide}" deployment guide. Choose a different model service or backend.`
        : '';
    const traceFormatCapabilities = (
        selectedBackend?.capabilities?.trace_format_capabilities?.[form.traceFormat] || {}
    );
    const supportsTraceRange = traceFormatCapabilities.supports_trace_range
        ?? selectedBackend?.capabilities?.supports_trace_range === true;
    const traceRangeMode = traceFormatCapabilities.trace_range_mode
        ?? (supportsTraceRange ? 'start_end' : 'none');
    const usesDurationRange = traceRangeMode === 'duration';
    const scaleFactorTraceFormats = selectedBackend?.capabilities?.scale_factor_trace_formats || [];
    const supportsScaleFactor = traceFormatCapabilities.supports_scale_factor
        ?? scaleFactorTraceFormats.includes(form.traceFormat);
    const isPublicDataset = selectedDataset?.source_type === 'external_dataset'
        || form.traceFormat === 'weka_public_dataset';
    const supportsFixedSchedule = !isPublicDataset
        && (traceFormatCapabilities.supports_fixed_schedule ?? true);
    const effectiveFixedSchedule = form.backend === 'aiperf'
        ? (supportsFixedSchedule && Boolean(form.fixedSchedule))
        : true;
    const isConcurrencyMode = form.backend === 'aiperf' && !effectiveFixedSchedule;
    const allowedAgentxWekaDatasets = Array.isArray(selectedBackend?.capabilities?.inferencex_agentx_mvp_weka_datasets)
        ? new Set(selectedBackend.capabilities.inferencex_agentx_mvp_weka_datasets)
        : DEFAULT_INFERENCEX_AGENTX_MVP_WEKA_DATASETS;
    const selectedWekaDatasetId = form.tracePath || selectedDataset?.public_dataset || selectedDataset?.filename || '';
    const supportsAgentxScenario = form.backend === 'aiperf' && (
        (form.traceFormat === 'weka_public_dataset' && allowedAgentxWekaDatasets.has(selectedWekaDatasetId))
        || (form.traceFormat === 'mooncake_trace' && isConcurrencyMode)
    );
    const aiperfAdvancedOptions = (
        selectedBackend?.capabilities?.advanced_options || []
    ).filter((option) => (
        option.name !== 'concurrency'
        && option.name !== 'trajectory_start_min_ratio'
        && option.name !== 'trajectory_start_max_ratio'
        && (!Array.isArray(option.trace_formats) || option.trace_formats.includes(form.traceFormat))
    ));
    const availableScenarios = useMemo(() => {
        const supported = new Set((selectedBackend?.scenarios || []).map((scenario) => (
            scenario.name || scenario.value || scenario.id
        )));
        return supported.size ? scenarios.filter((scenario) => supported.has(scenario.value)) : scenarios;
    }, [scenarios, selectedBackend]);
    const compatibleDatasets = useMemo(() => datasets.filter((dataset) => {
        const supportedBackends = dataset.supported_backends;
        return dataset.scenario === form.scenario
            && (!Array.isArray(supportedBackends) || supportedBackends.includes(form.backend));
    }), [datasets, form.backend, form.scenario]);
    const timelinePoints = useMemo(() => (timeline?.bins || []).map((bin) => ({
        ...bin,
        time_seconds: (bin.start_seconds + bin.end_seconds) / 2,
    })), [timeline]);
    const timelineChartPoints = useMemo(() => (
        usesDurationRange
            ? timelinePoints.slice(0, timelineSelection.endIndex + 1)
            : timelinePoints
    ), [timelinePoints, timelineSelection.endIndex, usesDurationRange]);
    const timelineChartEnd = usesDurationRange && Number(form.traceEndSeconds) > 0
        ? Number(form.traceEndSeconds)
        : null;
    const selectedTimelineRequests = useMemo(() => (
        timelinePoints
            .slice(timelineSelection.startIndex, timelineSelection.endIndex + 1)
            .reduce((totalRequests, point) => totalRequests + point.request_count, 0)
    ), [timelinePoints, timelineSelection]);

    useEffect(() => {
        let active = true;
        if (!form.dataset || selectedDataset?.downloaded !== true || isPublicDataset) {
            setTimeline(null);
            setTimelineError('');
            setTimelineLoading(false);
            return () => {
                active = false;
            };
        }
        const datasetName = form.dataset;
        setTimeline(null);
        setTimelineError('');
        setTimelineLoading(true);
        requestJson(`/api/v1/simulation/trace-datasets/${encodeURIComponent(datasetName)}/timeline?bins=800`)
            .then((payload) => {
                if (!active) return;
                const bins = Array.isArray(payload?.bins) ? payload.bins : [];
                if (!bins.length || !(Number(payload.duration_seconds) > 0)) {
                    throw new Error('The dataset timeline is empty.');
                }
                const duration = Number(payload.duration_seconds);
                setTimeline(payload);
                setTimelineSelection({ startIndex: 0, endIndex: bins.length - 1 });
                setForm((current) => {
                    if (current.dataset !== datasetName) return current;
                    const scale = Number(current.scaleFactor);
                    const effectiveScale = supportsScaleFactor && Number.isFinite(scale) && scale > 0
                        ? scale : 1;
                    return {
                        ...current,
                        scaleFactor: supportsScaleFactor ? current.scaleFactor : '1',
                        traceStartSeconds: '0',
                        traceEndSeconds: supportsTraceRange ? String(duration) : '',
                        durationSeconds: supportsTraceRange
                            ? String(Math.max(1, Math.ceil(duration / effectiveScale)))
                            : current.durationSeconds,
                    };
                });
            })
            .catch((error) => {
                if (active) setTimelineError(`Unable to load dataset timeline: ${error.message}`);
            })
            .finally(() => {
                if (active) setTimelineLoading(false);
            });
        return () => {
            active = false;
        };
    }, [
        form.dataset,
        isPublicDataset,
        selectedDataset?.downloaded,
        supportsScaleFactor,
        supportsTraceRange,
    ]);

    const updateTimelineSelection = ({ startIndex, endIndex }) => {
        if (
            !supportsTraceRange
            ||
            !timelinePoints.length
            || !Number.isInteger(startIndex)
            || !Number.isInteger(endIndex)
        ) return;
        const effectiveStartIndex = usesDurationRange ? 0 : startIndex;
        const start = usesDurationRange ? 0 : timelinePoints[effectiveStartIndex].start_seconds;
        const end = timelinePoints[endIndex].end_seconds;
        const scale = Number(form.scaleFactor);
        setTimelineSelection({ startIndex: effectiveStartIndex, endIndex });
        setForm((current) => ({
            ...current,
            traceStartSeconds: String(start),
            traceEndSeconds: String(end),
            durationSeconds: String(Math.max(1, Math.ceil((end - start) / scale))),
        }));
    };

    const updateScaleFactor = (value) => {
        if (!supportsScaleFactor) return;
        const scale = Number(value);
        setForm((current) => {
            const start = Number(current.traceStartSeconds);
            const end = Number(current.traceEndSeconds);
            return {
                ...current,
                scaleFactor: value,
                durationSeconds: Number.isFinite(scale) && scale > 0 && end > start
                    ? String(Math.max(1, Math.ceil((end - start) / scale)))
                    : current.durationSeconds,
            };
        });
    };

    const updateDuration = (value) => {
        if (!supportsTraceRange) {
            update('durationSeconds', value);
            return;
        }
        const requestedDuration = Number(value);
        const scale = Number(form.scaleFactor);
        const start = Number(form.traceStartSeconds);
        if (
            !timelinePoints.length
            || !Number.isFinite(requestedDuration)
            || requestedDuration < 1
            || !Number.isFinite(scale)
            || scale <= 0
            || !Number.isFinite(start)
        ) {
            update('durationSeconds', value);
            return;
        }
        const maximumEnd = Number(timeline.duration_seconds);
        const end = Math.min(maximumEnd, start + requestedDuration * scale);
        const endIndex = timelinePoints.findIndex((point) => point.end_seconds >= end);
        const boundedEndIndex = endIndex < 0 ? timelinePoints.length - 1 : endIndex;
        setTimelineSelection((current) => ({
            startIndex: current.startIndex,
            endIndex: Math.max(current.startIndex, boundedEndIndex),
        }));
        setForm((current) => ({
            ...current,
            traceEndSeconds: String(end),
            durationSeconds: String(Math.max(1, Math.ceil((end - start) / scale))),
        }));
    };

    const chooseReplayPacing = (mode) => {
        const nextFixed = mode === 'fixed';
        setForm((current) => ({
            ...current,
            fixedSchedule: nextFixed,
            aiperfConcurrency: !nextFixed && (!current.aiperfConcurrency || current.aiperfConcurrency === '1')
                ? '16'
                : current.aiperfConcurrency,
        }));
    };

    const chooseDataset = (value) => {
        setDatasetDownloadError('');
        setTimeline(null);
        setTimelineError('');
        const dataset = datasets.find((item) => (item.name || item.id || item.value) === value);
        const nextFormat = dataset?.trace_format || dataset?.format || '';
        const nextIsPublic = dataset?.source_type === 'external_dataset'
            || nextFormat === 'weka_public_dataset';
        const prevIsPublic = isPublicDataset;
        setForm((current) => ({
            ...current,
            dataset: value,
            tracePath: dataset?.downloaded ? (dataset.path || dataset.file_path || '') : '',
            traceFormat: nextFormat || current.traceFormat,
            scaleFactor: (
                selectedBackend?.capabilities?.scale_factor_trace_formats || []
            ).includes(nextFormat) ? current.scaleFactor : '1',
            fixedSchedule: nextIsPublic ? false : (prevIsPublic ? true : current.fixedSchedule),
            aiperfConcurrency: nextIsPublic && (!current.aiperfConcurrency || current.aiperfConcurrency === '1')
                ? '16'
                : current.aiperfConcurrency,
            traceStartSeconds: '0',
            traceEndSeconds: '',
        }));
    };

    const chooseScenario = (value) => {
        setDatasetDownloadError('');
        setForm((current) => ({
            ...current,
            scenario: value,
            dataset: '',
            tracePath: '',
            traceFormat: '',
            scaleFactor: '1',
            fixedSchedule: true,
            traceStartSeconds: '0',
            traceEndSeconds: '',
        }));
    };

    const chooseBackend = (value) => {
        setDatasetDownloadError('');
        setTimeline(null);
        setTimelineError('');
        const backend = backends.find((item) => (
            (item.name || item.id || item.value) === value
        ));
        const supportedScenarios = (backend?.scenarios || []).map((scenario) => (
            scenario.name || scenario.value || scenario.id
        ));
        setForm((current) => ({
            ...current,
            backend: value,
            scenario: supportedScenarios.includes(current.scenario)
                ? current.scenario
                : supportedScenarios[0] || current.scenario,
            dataset: '',
            tracePath: '',
            traceFormat: '',
            scaleFactor: '1',
            fixedSchedule: true,
            traceStartSeconds: '0',
            traceEndSeconds: '',
        }));
    };

    const downloadDataset = async () => {
        if (!form.dataset) {
            setErrors(['Choose a trace dataset before downloading it.']);
            return;
        }
        setDownloading(true);
        setDatasetDownloadError('');
        try {
            const payload = await requestJson('/api/v1/simulation/trace-datasets/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ dataset: form.dataset, force: selectedDataset?.downloaded === true }),
            });
            const result = payload?.dataset || payload?.data || payload;
            setForm((current) => ({
                ...current,
                tracePath: result.path || result.trace_path || current.tracePath,
                traceFormat: result.trace_format || result.format || current.traceFormat,
            }));
            await loadCatalogs();
        } catch (error) {
            setDatasetDownloadError(`Dataset download failed: ${error.message}`);
        } finally {
            setDownloading(false);
        }
    };

    const validationErrorsForStep = (step) => {
        const next = [];
        if (step === 0) {
            if (!form.backend) next.push('Choose a simulation backend.');
            if (selectedBackend?.available === false) next.push('The selected backend is unavailable.');
            if (!form.endpointUrl.trim()) {
                next.push(form.endpointMode === 'deployment'
                    ? 'Select a published Model Service.'
                    : 'Endpoint URL is required.');
            }
            else {
                try {
                    const endpoint = new URL(form.endpointUrl);
                    if (!['http:', 'https:'].includes(endpoint.protocol)) throw new Error();
                } catch {
                    next.push('Endpoint URL must be a valid HTTP or HTTPS URL.');
                }
            }
            if (form.endpointMode === 'deployment') {
                if (isAuthDisabled) {
                    next.push('Published Model Services and personal model access tokens require user authentication (PRISM_AUTH_MODE=local).');
                } else if (!form.modelAccessTokenId) {
                    if (modelTokensForbidden) {
                        next.push('You lack the model-service:token:manage permission required to select a model access token.');
                    } else if (activeModelTokens.length === 0) {
                        next.push('Create an active model access token on the API keys page before benchmarking a Model Service.');
                    } else {
                        next.push('Select a model access token to call a Model Service through the cluster\'s shared Gateway.');
                    }
                }
            }
            if (!endpointReady) next.push('Endpoint validation must succeed before creating a task.');
            if (!form.modelName.trim()) next.push('Model name is required.');
            if (incompatibleDeploymentGuideError) next.push(incompatibleDeploymentGuideError);
            aiperfAdvancedOptions.forEach((option) => {
                const rawValue = form[AIPERF_OPTION_FORM_KEYS[option.name]];
                const trimmed = String(rawValue ?? '').trim();
                if (trimmed === '') return;
                const numericValue = Number(trimmed);
                if (!Number.isFinite(numericValue)) {
                    next.push(`${option.label} must be a number.`);
                } else if (AIPERF_INTEGER_OPTIONS.has(option.name) && !Number.isInteger(numericValue)) {
                    next.push(`${option.label} must be an integer.`);
                } else if (option.minimum !== undefined && numericValue < option.minimum) {
                    next.push(`${option.label} must be at least ${option.minimum}.`);
                } else if (option.maximum !== undefined && numericValue > option.maximum) {
                    next.push(`${option.label} must be at most ${option.maximum}.`);
                }
            });
        }
        if (step === 1) {
            if (!form.scenario) next.push('Choose a Chat, Tool & API Use, or Code Generation scenario.');
            if (!form.dataset) next.push('Choose a trace dataset.');
            if (form.dataset && selectedDataset?.downloaded !== true) {
                next.push('Download the selected dataset before continuing.');
            } else if (!form.tracePath) {
                next.push('The downloaded dataset path is unavailable.');
            }
            if (!isPublicDataset) {
                if (timelineLoading) next.push('Wait for the dataset timeline to finish loading.');
                else if (timelineError) next.push(timelineError);
                else if (!timeline) next.push('The dataset timeline is unavailable.');
                if (supportsTraceRange && !(Number(form.traceEndSeconds) > Number(form.traceStartSeconds))) {
                    next.push('Select a non-empty replay interval on the workload timeline.');
                }
            }
            if (!(Number(form.scaleFactor) > 0)) next.push('Scale factor must be greater than zero.');
            if (!(Number(form.durationSeconds) >= 1)) next.push('Duration must be at least one second.');
            if (!supportsScaleFactor && Number(form.scaleFactor) !== 1) {
                next.push(`${selectedBackend?.display_name || displayName(form.backend)} does not support a scale factor for this trace format.`);
            }
        }
        if (step === 2) {
            if (isConcurrencyMode) {
                const concurrencyValue = Number(form.aiperfConcurrency);
                if (
                    form.aiperfConcurrency === ''
                    || !Number.isInteger(concurrencyValue)
                    || concurrencyValue < 1
                ) {
                    next.push('Session Concurrency must be an integer of at least 1.');
                }
            }
            if (supportsAgentxScenario) {
                const minTrimmed = String(form.trajectoryStartMinRatio ?? '').trim();
                const maxTrimmed = String(form.trajectoryStartMaxRatio ?? '').trim();
                const minVal = minTrimmed !== '' ? Number(minTrimmed) : null;
                const maxVal = maxTrimmed !== '' ? Number(maxTrimmed) : null;
                const minValid = minVal === null || (Number.isFinite(minVal) && minVal >= 0 && minVal <= 1);
                const maxValid = maxVal === null || (Number.isFinite(maxVal) && maxVal >= 0 && maxVal <= 1);
                if (!minValid) {
                    next.push('Trajectory start min ratio must be a number between 0 and 1.');
                }
                if (!maxValid) {
                    next.push('Trajectory start max ratio must be a number between 0 and 1.');
                }
                if (minVal !== null && maxVal !== null && minValid && maxValid && minVal > maxVal) {
                    next.push('Trajectory start min ratio must be less than or equal to Trajectory start max ratio.');
                }
            }
            if (!(Number(form.traceTimeoutSeconds) >= 1)) next.push('Trace timeout must be at least one second.');
            if (form.gracePeriodSeconds !== '') {
                const grace = Number(form.gracePeriodSeconds);
                if (!Number.isFinite(grace) || grace < 0 || grace > 3600) {
                    next.push('Grace period must be between 0 and 3600 seconds.');
                }
            }
            if (form.warmupSeconds !== '') {
                const warmup = Number(form.warmupSeconds);
                const duration = Number(form.durationSeconds);
                if (!Number.isFinite(warmup) || warmup < 0) {
                    next.push('Warmup window must be a non-negative number.');
                } else if (Number.isFinite(duration) && duration > 0 && warmup >= duration) {
                    next.push('Warmup window must be shorter than the task duration.');
                }
            }
            [
                ['TTFT SLO', form.ttftSlo],
                ['TPOT SLO', form.tpotSlo],
                ['E2E Latency SLO', form.e2eSlo],
            ].forEach(([label, value]) => {
                if (value !== '' && (!Number.isFinite(Number(value)) || Number(value) < 0)) {
                    next.push(`${label} must be a non-negative number.`);
                }
            });
            if (form.errorRateSlo !== '') {
                const errorRateSlo = Number(form.errorRateSlo);
                if (!Number.isFinite(errorRateSlo) || errorRateSlo < 0 || errorRateSlo > 100) {
                    next.push('Error Rate SLO must be a percentage from 0 to 100.');
                }
            }
        }
        if (step === 3 && !form.runName.trim()) next.push('Task name is required.');
        return next;
    };

    const validateStep = (step) => {
        const next = validationErrorsForStep(step);
        setErrors(next);
        return next.length === 0;
    };

    const validate = () => {
        const next = TASK_WIZARD_STEPS.flatMap((_, step) => validationErrorsForStep(step));
        setErrors(next);
        return next.length === 0;
    };

    const generateTaskIdentity = () => {
        const model = form.modelName.trim().split('/').filter(Boolean).pop() || 'model';
        const scenario = scenarioDisplayName(form.scenario);
        const dataset = selectedDataset?.display_name || displayName(form.dataset);
        const endpoint = form.endpointUrl.trim();
        const sla = [
            form.ttftSlo !== '' ? `TTFT SLO ${form.ttftSlo} ms` : '',
            form.tpotSlo !== '' ? `TPOT SLO ${form.tpotSlo} ms` : '',
            form.errorRateSlo !== '' ? `Error Rate SLO ${form.errorRateSlo}%` : '',
        ].filter(Boolean);
        return {
            runName: `${model} · ${scenario} · ${dataset} · ${taskNameSuffix}`,
            description: [
                `Replay the ${dataset} trace for the ${scenario} scenario against ${model} at ${endpoint}.`,
                form.endpointMode === 'deployment'
                    ? `The endpoint is the published Model Service ${selectedModelService?.name || form.endpointModelServiceId}.`
                    : 'The endpoint is external.',
                supportsTraceRange
                    ? `Replay source seconds ${Math.round(Number(form.traceStartSeconds))}–${Math.round(Number(form.traceEndSeconds))} with ${displayName(form.backend)} for ${form.durationSeconds}s (${effectiveFixedSchedule ? `${form.scaleFactor}x scale` : `concurrency ${form.aiperfConcurrency || 16}`}).`
                    : `Replay with ${displayName(form.backend)} for ${form.durationSeconds}s (${effectiveFixedSchedule ? `${form.scaleFactor}x scale` : `concurrency ${form.aiperfConcurrency || 16}`}).`,
                sla.length ? `${sla.join(' and ')}.` : '',
            ].filter(Boolean).join(' '),
        };
    };

    const advanceWizard = () => {
        if (!validateStep(wizardStep)) return;
        if (wizardStep === 2) {
            setForm((current) => ({ ...current, ...generateTaskIdentity() }));
        }
        setErrors([]);
        setWizardStep((current) => Math.min(current + 1, TASK_WIZARD_STEPS.length - 1));
    };

    const startTask = async () => {
        if (!validate()) return;
        setSubmitting(true);
        setActionError('');
        const backendOptions = {};
        aiperfAdvancedOptions.forEach((option) => {
            const rawValue = form[AIPERF_OPTION_FORM_KEYS[option.name]];
            const trimmed = String(rawValue ?? '').trim();
            if (trimmed !== '') backendOptions[option.name] = Number(trimmed);
        });
        if (isConcurrencyMode) {
            backendOptions.concurrency = Number(form.aiperfConcurrency);
        }
        if (supportsAgentxScenario) {
            const minTrimmed = String(form.trajectoryStartMinRatio ?? '').trim();
            const maxTrimmed = String(form.trajectoryStartMaxRatio ?? '').trim();
            if (minTrimmed !== '') backendOptions.trajectory_start_min_ratio = Number(minTrimmed);
            if (maxTrimmed !== '') backendOptions.trajectory_start_max_ratio = Number(maxTrimmed);
        }
        if (form.ttftSlo !== '') backendOptions.ttft_slo = Number(form.ttftSlo) / 1000;
        if (form.tpotSlo !== '') backendOptions.tpot_slo = Number(form.tpotSlo) / 1000;
        if (form.e2eSlo !== '') backendOptions.e2e_slo = Number(form.e2eSlo);
        if (form.errorRateSlo !== '') backendOptions.error_rate_slo = Number(form.errorRateSlo);
        if (form.warmupSeconds !== '') backendOptions.warmup_seconds = Number(form.warmupSeconds);
        backendOptions.collect_server_telemetry = Boolean(form.collectServerTelemetry);
        const body = {
            name: form.runName.trim() || `${scenarioDisplayName(form.scenario)} simulation`,
            description: form.description.trim()
                || `${scenarioDisplayName(form.scenario)} scenario using ${form.dataset}. Scenario is a label; dataset selected independently.`,
            scenario: form.scenario,
            backend: form.backend || 'aiperf',
            endpoint_mode: form.endpointMode,
            endpoint_namespace: null,
            endpoint_service: null,
            endpoint_deployment_execution_id: null,
            endpoint_cluster_id: null,
            endpoint_cluster_name: null,
            endpoint_deployment_name: null,
            model_service_group_id: form.endpointMode === 'deployment' ? (form.endpointModelServiceId || null) : null,
            endpoint_url: form.endpointUrl.trim(),
            model_access_token_id: form.modelAccessTokenId || null,
            model_name: form.modelName.trim(),
            trace_dataset: form.dataset,
            trace_path: form.tracePath,
            duration_seconds: Number(form.durationSeconds),
            grace_period_seconds: form.gracePeriodSeconds === '' ? 30 : Number(form.gracePeriodSeconds),
            scale_factor: (supportsScaleFactor && effectiveFixedSchedule) ? Number(form.scaleFactor) : 1,
            trace_start_seconds: supportsTraceRange ? Number(form.traceStartSeconds) : 0,
            trace_end_seconds: supportsTraceRange ? Number(form.traceEndSeconds) : null,
            trace_timeout_seconds: Number(form.traceTimeoutSeconds),
            fixed_schedule: effectiveFixedSchedule,
            stream: true,
            backend_options: backendOptions,
        };
        try {
            const payload = await requestJson('/api/v1/simulation/tasks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const nextTask = unwrapTask(payload);
            const nextId = payload?.task_id || nextTask?.id || nextTask?.task_id;
            const createdTask = { ...nextTask, id: nextTask?.id || nextId, status: nextTask?.status || payload?.status || 'queued' };
            setTasks((current) => [createdTask, ...current.filter((item) => (item.id || item.task_id) !== nextId)]);
            setIsNewTaskOpen(false);
            setErrors([]);
            await loadTasks({ quiet: true });
            if (nextId) onNavigate?.('optimization-simulate-details', { taskId: nextId });
        } catch (error) {
            setActionError(`Unable to start simulation: ${error.message}`);
        } finally {
            setSubmitting(false);
        }
    };

    const stopTask = async (item) => {
        const id = item?.id || item?.task_id;
        if (!id) return;
        setStoppingTaskId(id);
        setActionError('');
        try {
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(id)}/stop`, {
                method: 'POST',
            });
            setTasks((current) => current.map((item) => (
                (item.id || item.task_id) === id
                    ? { ...item, status: payload?.status || 'cancelling', progress_message: 'Cancellation requested' }
                    : item
            )));
        } catch (error) {
            setActionError(`Unable to stop simulation: ${error.message}`);
        } finally {
            setStoppingTaskId('');
        }
    };

    const deleteSimulationTask = (item) => {
        const id = item?.id || item?.task_id;
        if (!id) return;
        const status = String(item.status || '').toLowerCase();
        if (RUNNING_STATUSES.has(status)) {
            setActionError('Stop the simulation task before deleting it.');
            return;
        }
        setDeleteError('');
        setTaskPendingDelete(item);
    };

    const closeDeleteConfirmation = () => {
        setTaskPendingDelete(null);
        setDeleteError('');
    };

    const confirmDeleteSimulationTask = async () => {
        const id = taskPendingDelete?.id || taskPendingDelete?.task_id;
        if (!id) return;
        setDeletingTaskId(id);
        setDeleteError('');
        try {
            await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
            setTasks((current) => current.filter((candidate) => (candidate.id || candidate.task_id) !== id));
            const nextCompareIds = compareIds.filter((item) => item !== id);
            if (nextCompareIds.length !== compareIds.length) {
                setCompareIds(nextCompareIds);
                if (compareOpen && nextCompareIds.length < 2) {
                    setCompareOpen(false);
                }
            }
            setTaskPendingDelete(null);
            await loadTasks({ quiet: true });
        } catch (error) {
            setDeleteError(`Unable to delete simulation task: ${error.message}`);
        } finally {
            setDeletingTaskId('');
        }
    };

    const rerunSimulationTask = async (item) => {
        const id = item?.id || item?.task_id;
        if (!id || RUNNING_STATUSES.has(String(item.status || '').toLowerCase())) return;
        setRerunningTaskId(id);
        setActionError('');
        try {
            // Legacy deployment tasks created before deployment identity was persisted
            // carry no deployment reference, so a rerun would replay their stale endpoint
            // URL. Fall back to the currently selected Model Service so the backend can
            // re-resolve a fresh, health-probed endpoint.
            const isDeploymentTask = String(item?.endpoint_mode || '').toLowerCase() === 'deployment'
                || Boolean(item?.model_service_group_id);
            const rerunPayload = {};
            if (String(item?.endpoint_mode || '').toLowerCase() === 'deployment'
                && !item?.endpoint_deployment_execution_id
                && !item?.endpoint_deployment_run_id
                && !item?.endpoint_deployment_case_id
                && form.endpointModelServiceId) {
                rerunPayload.model_service_group_id = form.endpointModelServiceId;
            }
            if (isDeploymentTask) {
                const itemTokenActive = Boolean(
                    item?.model_access_token_id
                    && activeModelTokens.some((token) => token.id === item.model_access_token_id),
                );
                const formTokenActive = Boolean(
                    form.modelAccessTokenId
                    && activeModelTokens.some((token) => token.id === form.modelAccessTokenId),
                );
                const rerunTokenId = itemTokenActive
                    ? item.model_access_token_id
                    : formTokenActive
                        ? form.modelAccessTokenId
                        : (activeModelTokens[0]?.id || '');
                if (rerunTokenId) {
                    rerunPayload.model_access_token_id = rerunTokenId;
                }
            }
            const rerunBody = Object.keys(rerunPayload).length > 0 ? rerunPayload : null;
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(id)}/rerun`, {
                method: 'POST',
                headers: rerunBody ? { 'Content-Type': 'application/json' } : undefined,
                body: rerunBody ? JSON.stringify(rerunBody) : undefined,
            });
            const rerun = unwrapTask(payload);
            const rerunId = rerun?.id || payload?.task_id;
            const nextTask = { ...rerun, id: rerun?.id || rerunId, status: rerun?.status || 'queued' };
            setTasks((current) => [nextTask, ...current.filter((candidate) => (
                (candidate.id || candidate.task_id) !== rerunId
            ))]);
            await loadTasks({ quiet: true });
            if (rerunId) onNavigate?.('optimization-simulate-details', { taskId: rerunId });
        } catch (error) {
            setActionError(`Unable to rerun simulation: ${error.message}`);
        } finally {
            setRerunningTaskId('');
        }
    };

    const refreshAll = async () => {
        setActionError('');
        await Promise.all([
            loadCatalogs(),
            loadTasks(),
        ]);
    };

    const openNewTask = () => {
        setErrors([]);
        setActionError('');
        setDatasetDownloadError('');
        setWizardStep(0);
        setTaskNameSuffix(createShortId());
        setForm((current) => ({
            ...current,
            runName: '',
            description: '',
            randomSeed: String(current.randomSeed ?? '').trim() === '' ? DEFAULT_RANDOM_SEED : current.randomSeed,
            trajectoryStartMinRatio: DEFAULT_TRAJECTORY_START_MIN_RATIO,
            trajectoryStartMaxRatio: DEFAULT_TRAJECTORY_START_MAX_RATIO,
        }));
        setIsNewTaskOpen(true);
        void loadModelTokens();
    };

    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const pageStart = total === 0 ? 0 : page * pageSize + 1;
    const pageEnd = Math.min(total, (page + 1) * pageSize);
    const advancedDateInvalid = Boolean(
        draftAdvancedFilters.createdAfter
        && draftAdvancedFilters.createdBefore
        && draftAdvancedFilters.createdAfter > draftAdvancedFilters.createdBefore
    );
    const hardwareFilterOptions = useMemo(
        () => resolveHardwareFacetOptions(facets.hardware, draftAdvancedFilters.hardware),
        [draftAdvancedFilters.hardware, facets.hardware],
    );
    const openAdvancedFilters = () => {
        setDraftAdvancedFilters({ ...advancedFilters });
        setIsAdvancedOpen(true);
    };
    const clearAllFilters = () => {
        setPage(0);
        setStatusFilter('');
        setSearchInput('');
        setSearch('');
        setAdvancedFilters({ ...EMPTY_ADVANCED_FILTERS });
        setDraftAdvancedFilters({ ...EMPTY_ADVANCED_FILTERS });
    };

    return (
        <ModulePage className={embedded ? 'simulation-page min-h-0' : 'simulation-page'} contentClassName={`space-y-5 ${embedded ? 'py-0' : ''}`}>
                {!embedded && (
                    <ModuleHeader
                        icon={SimulationIcon}
                        title="Trace Replayer"
                        description="Replay production traces against an inference endpoint."
                        onToggleMobileNav={onToggleMobileNav}
                    />
                )}
                {catalogError && (
                    <div role="status" className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>Some simulation catalogs could not be loaded. {catalogError}</span>
                    </div>
                )}
                {actionError && (
                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {actionError}
                    </div>
                )}

                <section aria-label="Simulation task summary">
                    <StatCardRow>
                        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                            <StatCard
                                compact
                                title="Simulation Tasks"
                                help="Total number of simulation replay tasks matching the active search and advanced filters. Click to clear the status filter."
                                helpFormula="Total_Tasks = Count(Tasks matching active search & filters)"
                                helpAlign="start"
                                value={formatNumber(total)}
                                active={!statusFilter}
                                onClick={() => { setPage(0); setStatusFilter(''); }}
                                className="simulation-choice-card h-full p-3 shadow-none"
                            >
                                <p className="text-[10px] text-theme-muted">All recorded runs</p>
                            </StatCard>
                            {[
                                ['completed', 'Completed', counts.completed, 'success', 'Tasks that finished their replay window and saved telemetry artifacts. Click to filter by Completed.', 'Count(Tasks where status = completed)'],
                                ['running', 'Running', counts.running, 'info', 'Tasks currently executing against an inference endpoint with live streaming telemetry. Click to filter by Running.', 'Count(Tasks where status = running)'],
                                ['queued', 'Queued', counts.queued, 'warning', 'Tasks waiting in the execution queue for a worker slot or environment setup. Click to filter by Queued.', 'Count(Tasks where status = queued)'],
                                ['failed', 'Failed', counts.failed, 'danger', 'Tasks that terminated abnormally due to runner, validation, or upstream errors. Click to filter by Failed.', 'Count(Tasks where status = failed)'],
                                ['cancelled', 'Cancelled', counts.cancelled, 'neutral', 'Tasks stopped manually before completion; partial telemetry may still be inspectable. Click to filter by Cancelled.', 'Count(Tasks where status = cancelled)'],
                            ].map(([key, label, value, tone, helpText, formulaText]) => (
                                <StatCard
                                    key={key}
                                    compact
                                    title={label}
                                    help={helpText}
                                    helpFormula={formulaText}
                                    helpAlign={key === 'cancelled' ? 'end' : 'start'}
                                    value={formatNumber(value)}
                                    tone={tone}
                                    active={statusFilter === key}
                                    onClick={() => { setPage(0); setStatusFilter(statusFilter === key ? '' : key); }}
                                    className="simulation-choice-card h-full p-3 shadow-none"
                                />
                            ))}
                        </div>
                    </StatCardRow>
                </section>

                <section
                    aria-labelledby="simulation-tasks-heading"
                    className="relative z-10 flex flex-col gap-3.5 rounded-3xl border border-slate-900/90 bg-[#070b13]/65 p-5 shadow-2xl backdrop-blur-md"
                >
                    <div className="flex min-h-[52px] flex-wrap items-center justify-between gap-3 border-b border-theme-border py-2.5">
                        <div className="flex flex-wrap items-center gap-2">
                            <Server className="h-4 w-4 text-cyan-600 dark:text-cyan-400" />
                            <h2 id="simulation-tasks-heading" className="text-sm font-bold text-theme-text">Matching Tasks</h2>
                            <span className="rounded-md bg-theme-bg px-2 py-0.5 text-[9px] text-theme-muted">{formatNumber(total)} matched</span>
                        </div>
                        <div className="ml-auto flex items-center gap-2">
                            {compareIds.length > 0 && (
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    disabled={compareIds.length < 2}
                                    onClick={() => setCompareOpen(true)}
                                >
                                    <GitCompareArrows className="h-3.5 w-3.5" />
                                    Compare Runs ({compareIds.length}/{MAX_COMPARE_RUNS})
                                </Button>
                            )}
                            <RefreshButton onClick={refreshAll} disabled={catalogLoading || listLoading} refreshing={catalogLoading || listLoading} label="Refresh simulation data" />
                            <Button variant="sky" size="sm" onClick={openNewTask}><Plus size={14} /> New Task</Button>
                        </div>
                    </div>
                    {compareIds.length > 0 && (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-theme-border bg-theme-card px-3.5 py-2 text-xs">
                            <div className="flex min-w-0 flex-wrap items-center gap-2">
                                <span className="font-semibold text-theme-text">
                                    Selected to compare ({compareIds.length}/{MAX_COMPARE_RUNS}):
                                </span>
                                {compareIds.map((selectedId) => (
                                    <Badge
                                        key={selectedId}
                                        tone="info"
                                        size="xs"
                                        className="max-w-[220px]"
                                        title={tasksById[selectedId]?.name || selectedId}
                                    >
                                        <span className="min-w-0 truncate">
                                            {tasksById[selectedId]?.name || selectedId}
                                        </span>
                                    </Badge>
                                ))}
                                {compareIds.length < 2 && (
                                    <span className="text-theme-muted">Select at least one more completed, failed, or cancelled task.</span>
                                )}
                            </div>
                            <div className="flex items-center gap-2">
                                <Button
                                    variant="primary"
                                    size="xs"
                                    disabled={compareIds.length < 2}
                                    onClick={() => setCompareOpen(true)}
                                >
                                    <GitCompareArrows className="h-3 w-3" /> Open comparison
                                </Button>
                                <Button variant="ghost" size="xs" onClick={() => setCompareIds([])}>
                                    Clear
                                </Button>
                            </div>
                        </div>
                    )}
                    <div className="relative z-20 flex flex-wrap items-center gap-3" role="group" aria-label="Trace Replayer filters">
                        <div className="min-w-0 flex-[1_1_15rem]">
                                <Input
                                    icon={Search}
                                    id="simulation-task-search"
                                    className="h-9 text-xs"
                                    type="search"
                                    aria-label="Search replay tasks"
                                    placeholder="Search task name, scenario, backend, or dataset..."
                                    value={searchInput}
                                    onChange={(event) => setSearchInput(event.target.value)}
                                />
                        </div>
                        <div id="simulation-model-filter" className="w-full shrink-0 sm:w-48">
                            <MultiSelectDropdown
                                label="Model"
                                options={facets.models}
                                selected={advancedFilters.model ? new Set([advancedFilters.model]) : new Set()}
                                onChange={(value) => {
                                    setPage(0);
                                    setAdvancedFilters((current) => ({
                                        ...current,
                                        model: value === current.model ? '' : value,
                                    }));
                                }}
                                formatLabel={(value) => value}
                            />
                        </div>
                        <div id="simulation-dataset-filter" className="w-full shrink-0 sm:w-48">
                            <MultiSelectDropdown
                                label="Dataset"
                                options={facets.datasets}
                                selected={advancedFilters.dataset ? new Set([advancedFilters.dataset]) : new Set()}
                                onChange={(value) => {
                                    setPage(0);
                                    setAdvancedFilters((current) => ({
                                        ...current,
                                        dataset: value === current.dataset ? '' : value,
                                    }));
                                }}
                                formatLabel={(value) => value}
                            />
                        </div>
                        <Button variant="secondary" size="sm" onClick={openAdvancedFilters}>
                            <SlidersHorizontal className="h-3.5 w-3.5" />
                            Advanced Filters{activeAdvancedFilterCount > 0 ? ` (${activeAdvancedFilterCount})` : ''}
                        </Button>
                            {(statusFilter || searchInput || activeAdvancedFilterCount > 0) && (
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    onClick={clearAllFilters}
                                >
                                    Clear Filters
                                </Button>
                            )}
                    </div>
                    {listLoading ? (
                        <div className="flex items-center justify-center gap-2 rounded-2xl border border-slate-800 bg-slate-900/80 py-12 text-sm text-slate-400">
                            <Spinner /> Loading tasks…
                        </div>
                    ) : tasks.length === 0 ? (
                        <div className="rounded-2xl border border-slate-800 bg-slate-900/80">
                            <EmptyState
                                icon={<Activity className="h-8 w-8" />}
                                iconClassName="simulation-empty-icon"
                                title="No simulation tasks found"
                                message={statusFilter || search || activeAdvancedFilterCount > 0 ? 'Adjust the filters or create a new task.' : 'Create a task to replay a production trace.'}
                                action={<Button variant="sky" onClick={openNewTask}><Plus className="h-3.5 w-3.5" /> New Task</Button>}
                            />
                        </div>
                    ) : (
                        <div className="flex flex-col gap-3">
                            {tasks.map((item) => {
                                const id = item.id || item.task_id;
                                const isCompareSelected = compareIds.includes(id);
                                return (
                                    <SimulationTaskRow
                                        key={id}
                                        item={item}
                                        isCompareSelected={isCompareSelected}
                                        compareDisabled={!isCompareSelected && compareIds.length >= MAX_COMPARE_RUNS}
                                        onToggleCompare={toggleCompareId}
                                        onOpen={openTaskDetails}
                                        onStop={stopTask}
                                        onRerun={rerunSimulationTask}
                                        onDelete={deleteSimulationTask}
                                        stoppingTaskId={stoppingTaskId}
                                        rerunningTaskId={rerunningTaskId}
                                        deletingTaskId={deletingTaskId}
                                    />
                                );
                            })}
                            <PaginationControls className="mt-2" page={page} totalPages={totalPages} onPageChange={setPage}
                                total={total} pageStart={pageStart} pageEnd={pageEnd} itemLabel="tasks" formatValue={formatNumber}
                                pageSize={pageSize} pageSizeOptions={[5, 7, 10, 20, 50, 100]} onPageSizeChange={setPageSize}
                                pageSizeId="simulation-page-size" />
                        </div>
                    )}
                </section>

                {typeof document !== 'undefined' && isAdvancedOpen && createPortal(
                    <div
                        className="fixed inset-0 z-[55] cursor-pointer bg-black/40 backdrop-blur-[1.5px]"
                        onClick={() => setIsAdvancedOpen(false)}
                    />,
                    document.body,
                )}
                {typeof document !== 'undefined' && createPortal(
                    <aside
                        aria-label="Advanced simulation task filters"
                        className={`fixed right-4 top-20 z-[60] flex h-[calc(100vh-6rem)] w-[420px] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-3xl border border-slate-900 bg-slate-950/95 shadow-2xl backdrop-blur-xl transition-transform duration-300 ${
                            isAdvancedOpen ? 'translate-x-0' : 'translate-x-[calc(100%+2rem)]'
                        }`}
                    >
                        <div className="flex items-center justify-between border-b border-slate-900/60 bg-slate-950/40 p-4">
                            <div className="flex items-center gap-2">
                                <SlidersHorizontal className="h-4 w-4 text-cyan-400" />
                                <span className="text-sm font-bold tracking-wide text-white">Advanced Filters</span>
                            </div>
                            <button
                                type="button"
                                aria-label="Close advanced filters"
                                onClick={() => setIsAdvancedOpen(false)}
                                className="cursor-pointer rounded-lg p-1 text-slate-500 transition-all hover:bg-slate-900 hover:text-white"
                            >
                                <X className="h-4 w-4" />
                            </button>
                        </div>

                        <div className="custom-scrollbar flex-1 space-y-6 overflow-y-auto p-5">
                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Execution</h3>
                                <div>
                                    <Label htmlFor="simulation-filter-backend" help="Filter tasks by the load-generation backend (e.g., AIPerf or archived legacy backends) that executed the run.">Backend</Label>
                                    <Select
                                        id="simulation-filter-backend"
                                        value={draftAdvancedFilters.backend}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, backend: event.target.value }))}
                                    >
                                        <option value="">Any backend</option>
                                        {facets.backends.map((value) => <option key={value} value={value}>{displayName(value)}</option>)}
                                    </Select>
                                </div>
                                <div>
                                    <Label htmlFor="simulation-filter-model" help="Filter tasks by the served model name evaluated during simulation.">Model</Label>
                                    <Select
                                        id="simulation-filter-model"
                                        value={draftAdvancedFilters.model}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, model: event.target.value }))}
                                    >
                                        <option value="">Any model</option>
                                        {facets.models.map((value) => <option key={value} value={value}>{value}</option>)}
                                    </Select>
                                </div>
                            </section>

                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Workload</h3>
                                <div>
                                    <Label htmlFor="simulation-filter-dataset" help="Filter tasks by the trace dataset or prompt corpus name.">Dataset</Label>
                                    <Select
                                        id="simulation-filter-dataset"
                                        value={draftAdvancedFilters.dataset}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, dataset: event.target.value }))}
                                    >
                                        <option value="">Any dataset</option>
                                        {facets.datasets.map((value) => <option key={value} value={value}>{displayName(value)}</option>)}
                                    </Select>
                                </div>
                                <div>
                                    <Label htmlFor="simulation-filter-trace-format" help="Filter tasks by the input trace file schema (such as BailianTrace or Mooncake).">Trace format</Label>
                                    <Select
                                        id="simulation-filter-trace-format"
                                        value={draftAdvancedFilters.traceFormat}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, traceFormat: event.target.value }))}
                                    >
                                        <option value="">Any trace format</option>
                                        {facets.trace_formats.map((value) => <option key={value} value={value}>{displayName(value)}</option>)}
                                    </Select>
                                </div>
                            </section>

                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Target</h3>
                                <div>
                                    <Label htmlFor="simulation-filter-hardware" help="Filter tasks by the accelerator model or hardware profile detected on the target serving nodes.">Hardware</Label>
                                    <Select
                                        id="simulation-filter-hardware"
                                        value={draftAdvancedFilters.hardware}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, hardware: event.target.value }))}
                                    >
                                        <option value="">All hardware</option>
                                        {hardwareFilterOptions.map((value) => <option key={value} value={value}>{value}</option>)}
                                    </Select>
                                </div>
                                <div>
                                    <Label htmlFor="simulation-filter-endpoint" help="Substring match against the target inference endpoint hostname or base URL.">Endpoint contains</Label>
                                    <Input
                                        id="simulation-filter-endpoint"
                                        list="simulation-endpoint-options"
                                        placeholder="Host or URL"
                                        value={draftAdvancedFilters.endpoint}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, endpoint: event.target.value }))}
                                    />
                                    <datalist id="simulation-endpoint-options">
                                        {facets.endpoints.map((value) => <option key={value} value={value} />)}
                                    </datalist>
                                </div>
                            </section>

                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Created Time</h3>
                                <div className="grid grid-cols-2 gap-3">
                                    <div>
                                        <Label htmlFor="simulation-filter-created-after" help="Include tasks created on or after this UTC calendar date.">From</Label>
                                        <Input
                                            id="simulation-filter-created-after"
                                            type="date"
                                            value={draftAdvancedFilters.createdAfter}
                                            onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, createdAfter: event.target.value }))}
                                        />
                                    </div>
                                    <div>
                                        <Label htmlFor="simulation-filter-created-before" help="Include tasks created on or before this UTC calendar date.">To</Label>
                                        <Input
                                            id="simulation-filter-created-before"
                                            type="date"
                                            value={draftAdvancedFilters.createdBefore}
                                            onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, createdBefore: event.target.value }))}
                                        />
                                    </div>
                                </div>
                                {advancedDateInvalid && <p className="text-xs text-red-400">The From date must not be later than the To date.</p>}
                            </section>
                        </div>

                        <div className="flex items-center justify-between border-t border-slate-900/60 bg-slate-950/60 p-4">
                            <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => setDraftAdvancedFilters({ ...EMPTY_ADVANCED_FILTERS })}
                            >
                                Clear All
                            </Button>
                            <div className="flex gap-2">
                                <Button variant="secondary" size="sm" onClick={() => setIsAdvancedOpen(false)}>Cancel</Button>
                                <Button
                                    variant="sky"
                                    size="sm"
                                    disabled={advancedDateInvalid}
                                    onClick={() => {
                                        setPage(0);
                                        setAdvancedFilters({ ...draftAdvancedFilters });
                                        setIsAdvancedOpen(false);
                                    }}
                                >
                                    Apply Filters
                                </Button>
                            </div>
                        </div>
                    </aside>,
                    document.body,
                )}

                <div className="space-y-6">
                    <Modal
                        isOpen={Boolean(taskPendingDelete)}
                        onClose={deletingTaskId ? undefined : closeDeleteConfirmation}
                        closeOnBackdrop={!deletingTaskId}
                        closeOnEscape={!deletingTaskId}
                        size="md"
                        title={(
                            <span className="flex items-center gap-3">
                                <span className="rounded-lg bg-rose-500/15 p-2 text-rose-400">
                                    <Trash2 className="h-5 w-5" />
                                </span>
                                Delete Simulation Task
                            </span>
                        )}
                        subtitle="This action cannot be undone."
                        footer={(
                            <>
                                <Button variant="secondary" onClick={closeDeleteConfirmation} disabled={Boolean(deletingTaskId)}>
                                    Cancel
                                </Button>
                                <Button variant="dangerOutline" onClick={confirmDeleteSimulationTask} isLoading={Boolean(deletingTaskId)}>
                                    <Trash2 className="h-3.5 w-3.5" /> Delete Task
                                </Button>
                            </>
                        )}
                    >
                        <div className="space-y-4">
                            <div className="rounded-xl border border-rose-500/25 bg-rose-500/10 p-4">
                                <div className="flex items-start gap-3">
                                    <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-rose-400" />
                                    <div>
                                        <p className="text-sm font-semibold text-rose-100">
                                            Delete “{taskPendingDelete?.name || taskPendingDelete?.id || taskPendingDelete?.task_id}”?
                                        </p>
                                        <p className="mt-1 text-xs leading-relaxed text-rose-200/70">
                                            The task record, results, logs, and all backend artifacts will be permanently removed.
                                        </p>
                                    </div>
                                </div>
                            </div>
                            {deleteError && (
                                <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                    {deleteError}
                                </p>
                            )}
                        </div>
                    </Modal>

                    <Modal
                        isOpen={isNewTaskOpen}
                        onClose={submitting ? undefined : () => setIsNewTaskOpen(false)}
                        title="New Simulation Task"
                        subtitle={`Step ${wizardStep + 1} of ${TASK_WIZARD_STEPS.length} · ${TASK_WIZARD_STEPS[wizardStep]}`}
                        size="xl"
                        className="simulation-task-dialog"
                        closeOnBackdrop={!submitting}
                        closeOnEscape={!submitting}
                        footer={(
                            <>
                                <Button variant="outline" onClick={() => setIsNewTaskOpen(false)} disabled={submitting}>Cancel</Button>
                                {wizardStep > 0 && (
                                    <Button
                                        variant="outline"
                                        disabled={submitting}
                                        onClick={() => {
                                            setErrors([]);
                                            setWizardStep((current) => current - 1);
                                        }}
                                    >
                                        Back
                                    </Button>
                                )}
                                {wizardStep < TASK_WIZARD_STEPS.length - 1 ? (
                                    <Button variant="sky" onClick={advanceWizard} disabled={downloading || (wizardStep === 0 && !endpointReady)}>
                                        Next <ArrowRight className="h-3.5 w-3.5" />
                                    </Button>
                                ) : (
                                    <Button variant="sky" onClick={startTask} isLoading={submitting} disabled={!endpointReady}>
                                        <Play className="h-3.5 w-3.5" /> Create Task
                                    </Button>
                                )}
                            </>
                        )}
                    >
                    <div className="mx-auto max-w-4xl space-y-6">
                        <ol className="grid grid-cols-4 gap-2" aria-label="Task creation progress">
                            {TASK_WIZARD_STEPS.map((label, index) => (
                                <li key={label} className="min-w-0">
                                    <div className={`h-1.5 rounded-full transition-colors ${index <= wizardStep ? 'wizard-step-fill bg-cyan-400' : 'bg-slate-800'}`} />
                                    <div className={`mt-2 truncate text-[10px] font-bold uppercase tracking-wider ${index === wizardStep ? 'wizard-step-label-active text-cyan-300' : 'text-slate-500'}`}>
                                        {index + 1}. {label}
                                    </div>
                                </li>
                            ))}
                        </ol>
                        {actionError && (
                            <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {actionError}
                            </div>
                        )}
                        {wizardStep === 3 && <section aria-labelledby="basic-information-heading">
                            <h2 id="basic-information-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <Info className="h-5 w-5 text-cyan-400" /> Basic Information
                            </h2>
                            <div className="space-y-4">
                                <div>
                                    <Label htmlFor="simulation-run-name" help="Human-readable identifier shown in the task list, live monitor, and run comparison matrix.">Task name</Label>
                                    <Input id="simulation-run-name" value={form.runName} onChange={(event) => update('runName', event.target.value)} />
                                </div>
                                <div>
                                    <Label htmlFor="simulation-description" help="Optional notes describing the workload, endpoint configuration, or hypothesis tested by this run.">Description</Label>
                                    <Textarea
                                        id="simulation-description"
                                        rows={2}
                                        placeholder="Task description..."
                                        value={form.description}
                                        onChange={(event) => update('description', event.target.value)}
                                    />
                                </div>
                                <p className="text-xs text-slate-500">
                                    Data will be saved under <code className="rounded bg-slate-950/70 px-1.5 py-0.5">~/.llm-d-lens/simulations/&#123;task-id&#125;/</code>
                                </p>
                            </div>
                        </section>}

                        {wizardStep === 0 && <section aria-labelledby="benchmark-endpoint-heading">
                            <h2 id="benchmark-endpoint-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <Gauge className="h-5 w-5 text-cyan-400" /> Benchmark &amp; Endpoint
                            </h2>
                            <div className="space-y-5">
                                <div className="rounded-lg border border-violet-500/25 bg-violet-500/5 p-4">
                                    <div className="mb-3">
                                        <h3 className="text-sm font-semibold text-[#037ffc] dark:text-violet-200">Benchmark Backend</h3>
                                        <p className="mt-1 text-[11px] text-slate-500">Select the tool that will generate and replay benchmark traffic.</p>
                                    </div>
                                    <Label htmlFor="simulation-backend" help="Load generator engine that replays trace arrival timings against the inference endpoint and records client-side TTFT, TPOT, and throughput.">Benchmark tool</Label>
                                    <Select
                                        id="simulation-backend"
                                        value={form.backend}
                                        onChange={(event) => chooseBackend(event.target.value)}
                                    >
                                        {backends.map((backend) => {
                                            const value = backend.name || backend.id || backend.value;
                                            return (
                                                <option
                                                    key={value}
                                                    value={value}
                                                    disabled={backend.available === false}
                                                >
                                                    {backend.display_name || backend.label || displayName(value)}
                                                    {backend.available === false ? ' (Unavailable)' : ''}
                                                </option>
                                            );
                                        })}
                                    </Select>
                                    {selectedBackend && (
                                        <p className="mt-1 text-[10px] text-slate-500">
                                            {selectedBackend.version && `Version ${selectedBackend.version} · `}
                                            {selectedBackend.available === false
                                                ? selectedBackend.unavailable_reason || 'Unavailable'
                                                : `${selectedBackend.display_name || displayName(form.backend)} is ready`}
                                        </p>
                                    )}
                                    <details className="mt-4 rounded-lg border border-violet-500/20 bg-slate-950/30">
                                        <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-4 py-3 text-xs font-bold uppercase tracking-wider text-slate-400 hover:text-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/50">
                                            <SlidersHorizontal className="h-4 w-4" /> Advanced backend settings
                                        </summary>
                                        <div className="grid gap-4 border-t border-violet-500/15 p-4 sm:grid-cols-2 lg:grid-cols-3">
                                            <div>
                                                <Label
                                                    htmlFor="simulation-timeout"
                                                    help="Hard wall-clock timeout in seconds before Prism terminates a stalled or overly long backend subprocess."
                                                    helpFormula="Effective_Cutoff = min(Run_Completion, Timeout_s)"
                                                >
                                                    Prism execution timeout (seconds)
                                                </Label>
                                                <Input id="simulation-timeout" type="number" min="1" value={form.traceTimeoutSeconds} onChange={(event) => update('traceTimeoutSeconds', event.target.value)} />
                                            </div>
                                            {aiperfAdvancedOptions.map((option) => {
                                                const formKey = AIPERF_OPTION_FORM_KEYS[option.name];
                                                return (
                                                    <div key={option.name}>
                                                        <Label htmlFor={`simulation-${formKey}`} help={aiperfOptionHelp(option)}>{option.label}</Label>
                                                        <Input
                                                            id={`simulation-${formKey}`}
                                                            type="number"
                                                            min={option.minimum}
                                                            max={option.maximum}
                                                            step={AIPERF_INTEGER_OPTIONS.has(option.name) ? '1' : 'any'}
                                                            placeholder={option.default === undefined ? 'Optional' : String(option.default)}
                                                            value={form[formKey]}
                                                            onChange={(event) => update(formKey, event.target.value)}
                                                        />
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    </details>
                                </div>

                                <div className="rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-4">
                                <div className="mb-4 flex items-center gap-2">
                                    <Server className="h-4 w-4 text-[#6b7280] dark:text-emerald-400" />
                                    <div>
                                        <div className="flex items-center gap-1.5">
                                            <h3 className="text-sm font-semibold text-[#037ffc] dark:text-emerald-200">Inference Endpoint</h3>
                                            <HelpTooltip label="Inference endpoint help" align="start" iconSize={12}>
                                                Target either a cluster-managed Model Service (automatically resolved via Gateway with Prometheus telemetry) or any external OpenAI-compatible HTTP(S) endpoint.
                                            </HelpTooltip>
                                        </div>
                                        <p className="mt-0.5 text-[11px] text-slate-500">Configure the model-serving endpoint that receives benchmark requests.</p>
                                    </div>
                                </div>
                                <div className="mb-4 grid gap-3 border-b border-cyan-500/15 pb-4 sm:grid-cols-2">
                                    {[
                                        {
                                            value: 'deployment',
                                            label: 'Model Service',
                                            helpLabel: 'Model Service mode help',
                                            helpText: 'Routes benchmark traffic to a published Lens Model Service in your Kubernetes cluster and links server-side Prometheus telemetry.',
                                        },
                                        {
                                            value: 'external',
                                            label: 'Endpoint URL',
                                            helpLabel: 'Endpoint URL mode help',
                                            helpText: 'Targets any arbitrary HTTP or HTTPS OpenAI-compatible inference server URL; validated via /v1/models discovery.',
                                        },
                                    ].map((option) => {
                                        const isSelected = form.endpointMode === option.value;
                                        return (
                                            <label
                                                key={option.value}
                                                data-selected={isSelected ? 'true' : 'false'}
                                                className={cn(
                                                    'simulation-choice-card flex cursor-pointer items-center gap-2.5 rounded-lg border px-3.5 py-2.5 text-sm transition-colors',
                                                    isSelected
                                                        ? 'border-emerald-500/50 bg-emerald-500/10 text-slate-100'
                                                        : 'border-slate-800 bg-slate-950/30 text-slate-400 hover:border-slate-700 hover:text-slate-200',
                                                )}
                                            >
                                                <input
                                                    type="radio"
                                                    name="simulation-endpoint-mode"
                                                    value={option.value}
                                                    checked={isSelected}
                                                    onChange={(event) => chooseEndpointMode(event.target.value)}
                                                    className="accent-emerald-500 not-dark:accent-[var(--brand-accent)]"
                                                />
                                                <span className="simulation-choice-title font-medium">{option.label}</span>
                                                <HelpTooltip label={option.helpLabel} align="start" iconSize={11} asSpan>
                                                    {option.helpText}
                                                </HelpTooltip>
                                                <span
                                                    aria-hidden="true"
                                                    className={cn(
                                                        'simulation-choice-badge ml-auto inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border transition-colors',
                                                        isSelected
                                                            ? 'border-emerald-500 bg-emerald-500 text-white'
                                                            : 'border-slate-600 text-transparent',
                                                    )}
                                                >
                                                    <Check className="h-2.5 w-2.5" strokeWidth={3} />
                                                </span>
                                            </label>
                                        );
                                    })}
                                </div>

                                {form.endpointMode === 'deployment' ? (
                                    <div className="mb-4 rounded-lg border border-emerald-500/20 bg-slate-950/35 p-4">
                                        <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
                                            <div>
                                                <Label htmlFor="simulation-endpoint-model-service" help="Published healthy Model Service whose Gateway endpoint and backend pods will be benchmarked.">Model service</Label>
                                                <Select
                                                    id="simulation-endpoint-model-service"
                                                    value={form.endpointModelServiceId}
                                                    onChange={(event) => selectModelService(event.target.value)}
                                                    disabled={modelServicesLoading || modelServices.length === 0}
                                                    className="h-9"
                                                >
                                                    <option value="">
                                                        {modelServicesLoading
                                                            ? 'Loading model services…'
                                                            : modelServices.length
                                                                ? 'Choose a model service…'
                                                                : 'No published model service'}
                                                    </option>
                                                    {modelServices.map((item) => (
                                                        <option key={item.id} value={item.id}>
                                                            {item.name}{item.baseModel && item.baseModel !== item.name ? ` · ${item.baseModel}` : ''}{item.clusterName ? ` (${item.clusterName})` : ''}
                                                        </option>
                                                    ))}
                                                </Select>
                                            </div>
                                            <Button
                                                variant="secondary"
                                                onClick={() => void loadModelServices()}
                                                isLoading={modelServicesLoading}
                                                className="h-9"
                                            >
                                                <RefreshCw className="h-3.5 w-3.5" /> Refresh
                                            </Button>
                                        </div>
                                        {modelServicesError && (
                                            <p role={isAuthDisabled ? 'status' : 'alert'} className={cn('mt-3 text-xs', isAuthDisabled ? 'text-sky-700 dark:text-sky-200' : 'text-rose-600 dark:text-rose-300')}>{modelServicesError}</p>
                                        )}
                                        {form.endpointModelServiceId && (
                                            <div className="mt-3 rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-xs">
                                                <div className="font-semibold text-emerald-300">Model service ready</div>
                                                <div className="mt-1 font-mono text-slate-400">{selectedModelService?.name || form.endpointModelServiceId}</div>
                                            </div>
                                        )}
                                    </div>
                                ) : (
                                    <div className="mb-4">
                                        <Label htmlFor="simulation-endpoint" help="Base HTTP or HTTPS URL of the OpenAI-compatible inference server (e.g., http://host:8000). Verified against /v1/models before submission.">Endpoint URL</Label>
                                        <Input id="simulation-endpoint" type="url" placeholder="http://inference.example:8000" value={form.endpointUrl} onChange={(event) => updateExternalEndpoint(event.target.value)} />
                                    </div>
                                )}

                                <div className="mb-4">
                                    <Label
                                        htmlFor="simulation-api-key"
                                        help="Personal API key used to mint a short-lived delegated bearer token for calling a shared Gateway Model Service or protected endpoint. The raw secret is never stored or displayed here."
                                    >
                                        Model access token{form.endpointMode === 'deployment' ? '' : ' (optional)'}
                                    </Label>
                                    {isAuthDisabled ? (
                                        <div
                                            role="status"
                                            className="flex items-start gap-2.5 rounded-lg border border-sky-500/30 bg-sky-500/10 p-3 text-xs text-sky-900 dark:text-sky-200"
                                        >
                                            <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                                            <div>
                                                <p className="font-medium">
                                                    Personal model access tokens require user authentication (<code className="font-mono">PRISM_AUTH_MODE=local</code>).
                                                </p>
                                                <p className="mt-0.5 text-[11px] text-sky-800 dark:text-sky-300">
                                                    {form.endpointMode === 'deployment'
                                                        ? 'Published Model Services cannot be benchmarked while authentication is disabled. Switch to Endpoint URL to test an unauthenticated endpoint.'
                                                        : 'You can still benchmark an unauthenticated Endpoint URL without selecting a token.'}
                                                </p>
                                            </div>
                                        </div>
                                    ) : modelTokensForbidden ? (
                                        <div
                                            role="status"
                                            className="flex items-start gap-2.5 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-900 dark:text-amber-200"
                                        >
                                            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                                            <div>
                                                <p className="font-medium">
                                                    Permission <code className="font-mono">model-service:token:manage</code> is required to select a personal model access token.
                                                </p>
                                                <p className="mt-0.5 text-[11px] text-amber-800 dark:text-amber-300">
                                                    {form.endpointMode === 'deployment'
                                                        ? 'Ask an administrator for API key access to benchmark a published Model Service, or switch to Endpoint URL for an unauthenticated endpoint.'
                                                        : 'You can still benchmark an unauthenticated Endpoint URL without selecting a key.'}
                                                </p>
                                            </div>
                                        </div>
                                    ) : (!hasLoadedModelTokens || modelTokensLoading) && activeModelTokens.length === 0 ? (
                                        <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-center">
                                            <Select id="simulation-api-key" value="" disabled className="h-9">
                                                <option value="">Loading model access tokens…</option>
                                            </Select>
                                            <Button
                                                type="button"
                                                variant="secondary"
                                                onClick={() => void loadModelTokens()}
                                                isLoading={modelTokensLoading}
                                                className="h-9"
                                            >
                                                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Refresh
                                            </Button>
                                        </div>
                                    ) : modelTokensError ? (
                                        <div
                                            role="alert"
                                            className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-900 dark:text-rose-200"
                                        >
                                            <div className="flex items-start gap-2">
                                                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                                                <span>{modelTokensError}</span>
                                            </div>
                                            <Button
                                                type="button"
                                                variant="outline"
                                                size="xs"
                                                onClick={() => void loadModelTokens()}
                                            >
                                                Retry
                                            </Button>
                                        </div>
                                    ) : activeModelTokens.length === 0 ? (
                                        <div className="flex flex-col gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 sm:flex-row sm:items-center sm:justify-between">
                                            <div className="flex items-start gap-2.5">
                                                <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-300" aria-hidden="true" />
                                                <div>
                                                    <p className="text-xs font-semibold text-theme-text">No active model access tokens</p>
                                                    <p className="mt-0.5 text-[11px] text-theme-muted">
                                                        {form.endpointMode === 'deployment'
                                                            ? 'Create an API key to authenticate against a published Model Service.'
                                                            : 'Create an API key if this endpoint requires a Bearer token, or proceed without one for an unauthenticated endpoint.'}
                                                    </p>
                                                </div>
                                            </div>
                                            <div className="flex flex-wrap items-center gap-2">
                                                <Button
                                                    type="button"
                                                    variant="sky"
                                                    size="sm"
                                                    onClick={() => onNavigate?.('api-keys', { action: 'create' })}
                                                >
                                                    Create API key
                                                </Button>
                                                <Button
                                                    type="button"
                                                    variant="secondary"
                                                    size="sm"
                                                    onClick={() => void loadModelTokens()}
                                                    isLoading={modelTokensLoading}
                                                >
                                                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Refresh
                                                </Button>
                                            </div>
                                        </div>
                                    ) : (
                                        <div>
                                            <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-center">
                                                <Select
                                                    id="simulation-api-key"
                                                    value={activeModelTokens.some((token) => token.id === form.modelAccessTokenId) ? form.modelAccessTokenId : ''}
                                                    onChange={(event) => {
                                                        resetModelDiscovery();
                                                        update('modelAccessTokenId', event.target.value);
                                                    }}
                                                    className="h-9"
                                                >
                                                    {form.endpointMode !== 'deployment' ? (
                                                        <option value="">None (unauthenticated endpoint)</option>
                                                    ) : (
                                                        !activeModelTokens.some((token) => token.id === form.modelAccessTokenId) && (
                                                            <option value="">Choose a model access token…</option>
                                                        )
                                                    )}
                                                    {activeModelTokens.map((token) => (
                                                        <option key={token.id} value={token.id}>
                                                            {formatModelTokenOptionLabel(token)}
                                                        </option>
                                                    ))}
                                                </Select>
                                                <Button
                                                    type="button"
                                                    variant="secondary"
                                                    onClick={() => void loadModelTokens()}
                                                    isLoading={modelTokensLoading}
                                                    className="h-9"
                                                >
                                                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Refresh
                                                </Button>
                                            </div>
                                            <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2">
                                                <p className="text-[11px] text-theme-muted">
                                                    Mints a short-lived delegated token at runtime; the raw key is never stored in task records.
                                                </p>
                                                <Button
                                                    type="button"
                                                    variant="link"
                                                    size="xs"
                                                    onClick={() => onNavigate?.('api-keys', { action: 'create' })}
                                                >
                                                    Manage / Create key in API keys →
                                                </Button>
                                            </div>
                                        </div>
                                    )}
                                </div>

                                <div>
                                        <Label htmlFor="simulation-model" help="Model identifier sent in the JSON payload of each chat/completion request; auto-populated when the endpoint exposes a single model.">Model</Label>
                                        {modelsLoading ? (
                                            <div className="flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 bg-slate-900 px-3 text-xs text-slate-400">
                                                <Spinner /> Loading models from /v1/models…
                                            </div>
                                        ) : endpointModels.length > 1 ? (
                                            <Select id="simulation-model" value={form.modelName} onChange={(event) => update('modelName', event.target.value)}>
                                                <option value="">Choose a model…</option>
                                                {endpointModels.map((model) => <option key={model} value={model}>{model}</option>)}
                                            </Select>
                                        ) : endpointModels.length === 1 ? (
                                            <Input id="simulation-model" value={form.modelName} readOnly />
                                        ) : (
                                            <Input id="simulation-model" placeholder="meta-llama/Llama-3.1-8B-Instruct" value={form.modelName} onChange={(event) => update('modelName', event.target.value)} />
                                        )}
                                        {endpointModels.length === 1 && (
                                            <p className="mt-1 text-[10px] text-emerald-400">Automatically selected from the endpoint.</p>
                                        )}
                                        {modelDiscoveryError && (
                                            <div role="alert" className="mt-1 text-xs text-theme-muted">
                                                <p>{modelDiscoveryError}</p>
                                                <Button variant="outline" size="xs" onClick={() => discoverEndpointModels(form.endpointUrl.trim(), form.modelAccessTokenId)}>
                                                    Retry endpoint validation
                                                </Button>
                                            </div>
                                        )}
                                </div>
                            </div>
                            </div>
                        </section>}

                        {wizardStep === 1 && <section aria-labelledby="scenario-configuration-heading">
                            <h2 id="scenario-configuration-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <MessageSquareText className="h-5 w-5 text-[#037ffc] dark:text-cyan-400" /> Scenario
                            </h2>
                            <div className="rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-4">
                                <fieldset>
                                    <legend className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-slate-400">
                                        <span>Scenarios supported by {selectedBackend?.display_name || displayName(form.backend)}</span>
                                        <HelpTooltip label="Scenario help" align="start" iconSize={12}>
                                            Categorizes the workload shape (Chat, Tool &amp; API Use, or Code Generation) and filters the compatible trace datasets below.
                                        </HelpTooltip>
                                    </legend>
                                    <ToggleGroup
                                        options={availableScenarios}
                                        value={form.scenario}
                                        onChange={chooseScenario}
                                        fullWidth
                                        activeClassName="simulation-choice-active"
                                    />
                                </fieldset>
                                <p className="mt-3 text-xs text-slate-500">
                                    The backend defines the scenario abstraction. Selecting a scenario determines which trace datasets are available.
                                </p>
                            </div>
                        </section>}

                        {wizardStep === 1 && <section aria-labelledby="workload-configuration-heading">
                            <h2 id="workload-configuration-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <MessageSquareText className="h-5 w-5 text-[#037ffc] dark:text-violet-400" /> Workload
                            </h2>
                            <div className="rounded-lg border border-violet-500/20 bg-violet-500/5 p-4">
                                <label className="mb-4 flex items-center gap-2 text-sm font-medium text-slate-200">
                                    <input type="radio" checked readOnly className="accent-[#037ffc] dark:accent-violet-500" />
                                    <span>Trace replay</span>
                                    <HelpTooltip label="Trace replay schedule help" align="start" iconSize={12} asSpan>
                                        Replays recorded production request timestamps on a fixed schedule so arrival bursts and prefix patterns match real traffic.
                                    </HelpTooltip>
                                </label>
                                {datasetDownloadError && (
                                    <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                                        <span>{datasetDownloadError}</span>
                                    </div>
                                )}
                                {catalogLoading ? (
                                    <div className="flex items-center gap-2 py-6 text-sm text-slate-400"><Spinner /> Loading datasets…</div>
                                ) : !form.scenario ? (
                                    <EmptyState
                                        icon={<MessageSquareText className="h-8 w-8" />}
                                        title="Choose a scenario first"
                                        message="The selected backend and scenario determine the available trace datasets."
                                    />
                                ) : compatibleDatasets.length ? (
                                    <div className="space-y-4">
                                        <div>
                                            <Label htmlFor="simulation-dataset" help="Production trace or benchmark corpus providing request timestamps, input/output token counts, and session structure.">Trace dataset</Label>
                                            <Select id="simulation-dataset" value={form.dataset} onChange={(event) => chooseDataset(event.target.value)}>
                                                <option value="">Choose a dataset…</option>
                                                {compatibleDatasets.map((item) => {
                                                    const value = item.name || item.id || item.value;
                                                    return <option key={value} value={value}>{item.display_name || item.label || displayName(value)}</option>;
                                                })}
                                            </Select>
                                        </div>
                                        {selectedDataset && (
                                            <div className="rounded-lg border border-violet-500/20 bg-slate-950/40 p-3">
                                                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                                                    <div className="min-w-0">
                                                        <div className="flex flex-wrap items-center gap-2">
                                                            <span className="text-sm font-semibold text-slate-200">{selectedDataset.display_name || displayName(form.dataset)}</span>
                                                            <Badge tone={selectedDataset.downloaded ? 'success' : 'neutral'}>
                                                                {isPublicDataset
                                                                    ? 'Managed by AIPerf'
                                                                    : selectedDataset.downloaded ? 'Downloaded' : 'Not downloaded'}
                                                            </Badge>
                                                            {selectedDataset.license && <Badge>{selectedDataset.license}</Badge>}
                                                        </div>
                                                        <p className="mt-1 text-xs text-slate-400">{selectedDataset.description}</p>
                                                        <p className="mt-2 break-all font-mono text-[10px] text-slate-500">
                                                            {form.tracePath || selectedDataset.path}
                                                            {formatBytes(selectedDataset.size_bytes) && ` · ${formatBytes(selectedDataset.size_bytes)}`}
                                                        </p>
                                                    </div>
                                                    {!isPublicDataset && <Button className="w-full sm:w-auto" onClick={downloadDataset} isLoading={downloading} variant={selectedDataset.downloaded ? 'secondary' : 'sky'}>
                                                        <Download className="h-3.5 w-3.5" />
                                                        {selectedDataset.downloaded ? 'Download again' : 'Download dataset'}
                                                    </Button>}
                                                </div>
                                            </div>
                                        )}
                                        {isPublicDataset && (
                                            <div className="rounded-xl border border-violet-500/20 bg-slate-950/60 p-4">
                                                <h3 className="text-sm font-semibold text-slate-200">AIPerf public dataset</h3>
                                                <p className="mt-1 text-[11px] text-slate-500">
                                                    AIPerf downloads and caches this corpus when the task starts. Weka replay supports duration, but not source start/end or scale factor.
                                                </p>
                                                <div className="mt-4 grid gap-4 sm:grid-cols-2">
                                                    <div>
                                                        <Label
                                                            htmlFor="simulation-public-duration"
                                                            help="Active replay duration in seconds before the post-send grace drain window."
                                                            helpFormula="Effective_Window_s = Run_Duration_s + Grace_Period_s"
                                                        >
                                                            Duration (seconds)
                                                        </Label>
                                                        <Input id="simulation-public-duration" type="number" min="1" step="1" value={form.durationSeconds} onChange={(event) => updateDuration(event.target.value)} />
                                                    </div>
                                                    <div>
                                                        <Label
                                                            htmlFor="simulation-public-scale"
                                                            help="Fixed at 1x for AIPerf-managed public datasets; time compression is not supported for this corpus."
                                                            helpFormula="Arrival_Interval = Base_Trace_Δt / 1.0  (fixed 1x)"
                                                        >
                                                            Scale factor
                                                        </Label>
                                                        <Input id="simulation-public-scale" type="number" value="1" disabled />
                                                    </div>
                                                </div>
                                            </div>
                                        )}
                                        {selectedDataset?.downloaded && !isPublicDataset && (
                                            <div className="rounded-xl border border-violet-500/20 bg-slate-950/60 p-4">
                                                <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                                                    <div>
                                                        <div className="flex items-center gap-1.5">
                                                            <h3 className="text-sm font-semibold text-slate-200">Request Timeline</h3>
                                                            <HelpTooltip
                                                                label="Request timeline help"
                                                                align="start"
                                                                iconSize={12}
                                                                formula="Selected_Span_s = Replay_End − Replay_Start; Bucket_Rate = Requests_in_Bin / Bin_Width_s"
                                                            >
                                                                Plots source request arrival density over trace time. Drag the brush handles or slider to select which source time range to replay.
                                                            </HelpTooltip>
                                                        </div>
                                                        <p className="mt-1 text-[11px] text-slate-500">
                                                            {usesDurationRange
                                                                ? 'Drag the end handle below the chart; replay always starts at 0 seconds.'
                                                                : 'Drag either handle below the chart to choose the source replay interval.'}
                                                        </p>
                                                    </div>
                                                    {timeline && (
                                                        <div className="text-right text-[11px] text-slate-400">
                                                            <div><span className="font-semibold text-cyan-300">≈ {formatNumber(selectedTimelineRequests)}</span> selected requests</div>
                                                            <div>{formatNumber(timeline.request_count)} total · {formatBytes(timeline.source_size_bytes)}</div>
                                                        </div>
                                                    )}
                                                </div>
                                                {timelineLoading ? (
                                                    <div className="flex h-64 items-center justify-center gap-2 text-sm text-slate-400">
                                                        <Spinner /> Building request timeline…
                                                    </div>
                                                ) : timelineError ? (
                                                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {timelineError}
                                                    </div>
                                                ) : timelinePoints.length > 0 ? (
                                                    <>
                                                        <div className="simulation-timeline h-72 w-full">
                                                            <ResponsiveContainer width="100%" height="100%">
                                                                <AreaChart data={timelineChartPoints} margin={{ top: 8, right: 12, left: 0, bottom: 4 }}>
                                                                    <defs>
                                                                        <linearGradient id="simulationRequestDensity" x1="0" y1="0" x2="0" y2="1">
                                                                            <stop offset="5%" stopColor="#22d3ee" stopOpacity={0.55} />
                                                                            <stop offset="95%" stopColor="#22d3ee" stopOpacity={0.04} />
                                                                        </linearGradient>
                                                                    </defs>
                                                                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                                                    <XAxis
                                                                        dataKey="time_seconds"
                                                                        type="number"
                                                                        domain={timelineChartEnd === null
                                                                            ? ['dataMin', 'dataMax']
                                                                            : [0, timelineChartEnd]}
                                                                        tickFormatter={(value) => `${formatNumber(value)}s`}
                                                                        tick={{ fill: '#64748b', fontSize: 10 }}
                                                                        stroke="#334155"
                                                                    />
                                                                    <YAxis
                                                                        allowDecimals={false}
                                                                        width={48}
                                                                        tick={{ fill: '#64748b', fontSize: 10 }}
                                                                        stroke="#334155"
                                                                    />
                                                                    <Tooltip
                                                                        labelFormatter={(value) => `Trace time ${formatNumber(value)}s`}
                                                                        formatter={(value) => [formatNumber(value), 'Requests']}
                                                                        contentStyle={{ background: '#020617', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
                                                                    />
                                                                    <Area
                                                                        type="stepAfter"
                                                                        dataKey="request_count"
                                                                        name="Requests"
                                                                        stroke="#22d3ee"
                                                                        fill="url(#simulationRequestDensity)"
                                                                        isAnimationActive={false}
                                                                    />
                                                                    {supportsTraceRange && !usesDurationRange && <Brush
                                                                        dataKey="time_seconds"
                                                                        height={34}
                                                                        stroke="#22d3ee"
                                                                        fill="#0f172a"
                                                                        travellerWidth={10}
                                                                        startIndex={timelineSelection.startIndex}
                                                                        endIndex={timelineSelection.endIndex}
                                                                        onChange={updateTimelineSelection}
                                                                        tickFormatter={(value) => `${formatNumber(value)}s`}
                                                                    />}
                                                                </AreaChart>
                                                            </ResponsiveContainer>
                                                        </div>
                                                        {supportsTraceRange && usesDurationRange && (
                                                            <div className="mt-3 rounded-lg border border-slate-700/70 bg-slate-950/70 px-3 py-3">
                                                                <div className="mb-2 flex items-center justify-between text-[11px] text-slate-400">
                                                                    <span>Fixed start: 0s</span>
                                                                    <span>End: {Math.round(Number(form.traceEndSeconds))}s</span>
                                                                </div>
                                                                <input
                                                                    aria-label="Replay end time"
                                                                    type="range"
                                                                    min="0"
                                                                    max={timelinePoints.length - 1}
                                                                    step="1"
                                                                    value={timelineSelection.endIndex}
                                                                    onChange={(event) => updateTimelineSelection({
                                                                        startIndex: 0,
                                                                        endIndex: Number(event.target.value),
                                                                    })}
                                                                    className="simulation-rate-range h-2 w-full cursor-pointer accent-[#037ffc] dark:accent-cyan-400"
                                                                />
                                                            </div>
                                                        )}
                                                        <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                                                            {supportsTraceRange && <div>
                                                                <div className="flex items-center gap-1 text-xs font-semibold text-[#6b7280] dark:text-slate-400">
                                                                    <span>Replay start (seconds)</span>
                                                                    <HelpTooltip label="Replay start help" align="start" iconSize={12}>
                                                                        Start offset in source trace seconds where request replay begins.
                                                                    </HelpTooltip>
                                                                </div>
                                                                <div className="mt-2 font-mono text-lg font-semibold text-[#037ffc] dark:text-cyan-300">
                                                                    {Math.round(Number(form.traceStartSeconds))}
                                                                </div>
                                                            </div>}
                                                            {supportsTraceRange && <div>
                                                                <div className="flex items-center gap-1 text-xs font-semibold text-[#6b7280] dark:text-slate-400">
                                                                    <span>Replay end (seconds)</span>
                                                                    <HelpTooltip label="Replay end help" align="start" iconSize={12}>
                                                                        End offset in source trace seconds where request replay stops.
                                                                    </HelpTooltip>
                                                                </div>
                                                                <div className="mt-2 font-mono text-lg font-semibold text-[#037ffc] dark:text-cyan-300">
                                                                    {Math.round(Number(form.traceEndSeconds))}
                                                                </div>
                                                            </div>}
                                                            <div>
                                                                <Label
                                                                    htmlFor="simulation-scale"
                                                                    help="Time-compression multiplier on source trace inter-arrival times; values above 1x speed up arrivals and increase request rate."
                                                                    helpFormula="Arrival_Interval = Base_Trace_Δt / Scale_Factor"
                                                                >
                                                                    Scale factor
                                                                </Label>
                                                                <Input
                                                                    id="simulation-scale"
                                                                    type="number"
                                                                    min="0.01"
                                                                    step="0.01"
                                                                    value={form.scaleFactor}
                                                                    disabled={!supportsScaleFactor}
                                                                    onChange={(event) => updateScaleFactor(event.target.value)}
                                                                />
                                                                {!supportsScaleFactor && (
                                                                    <p className="mt-1 text-[11px] text-slate-500">
                                                                        This backend only supports 1x replay for this trace format.
                                                                    </p>
                                                                )}
                                                            </div>
                                                            <div>
                                                                <Label
                                                                    htmlFor="simulation-duration"
                                                                    help="Active wall-clock replay duration in seconds (computed from the selected trace range divided by scale factor)."
                                                                    helpFormula="Replay_Duration_s = (Replay_End − Replay_Start) / Scale_Factor"
                                                                >
                                                                    Duration (seconds)
                                                                </Label>
                                                                <Input id="simulation-duration" type="number" min="1" step="1" value={form.durationSeconds} onChange={(event) => updateDuration(event.target.value)} />
                                                            </div>
                                                            {form.backend === 'aiperf' && supportsTraceRange && (
                                                                <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs leading-relaxed text-amber-100 sm:col-span-2 lg:col-span-4">
                                                                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
                                                                    <p>
                                                                        AIPerf uses a half-open replay range: start is included, but end is excluded. Requests exactly at the selected end time are not sent, so the executed request count can be lower than the Request Timeline selection.
                                                                    </p>
                                                                </div>
                                                            )}
                                                        </div>
                                                    </>
                                                ) : null}
                                            </div>
                                        )}
                                        <p className="flex items-start gap-2 text-xs leading-relaxed text-slate-500 dark:text-violet-200/70">
                                            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                                            Showing datasets compatible with {selectedBackend?.display_name || displayName(form.backend)} and {scenarioDisplayName(form.scenario)}.
                                        </p>
                                    </div>
                                ) : (
                                    <EmptyState
                                        icon={<FileJson className="h-8 w-8" />}
                                        title="No compatible trace datasets"
                                        message="This backend and scenario combination has no registered dataset."
                                    />
                                )}
                            </div>
                        </section>}

                        {wizardStep === 2 && <section aria-labelledby="load-sla-heading">
                            <h2 id="load-sla-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <Gauge className="h-5 w-5 text-[#037ffc] dark:text-amber-400" /> Load &amp; SLA Parameters
                            </h2>
                            <div className="grid gap-6 lg:grid-cols-2">
                                <div className="simulation-load-card rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
                                    <h3 className="mb-3 text-sm font-semibold text-[#037ffc] dark:text-amber-300">Replay Parameters</h3>
                                    <div className="grid gap-4 sm:grid-cols-2">
                                        <div className="flex min-h-10 items-center gap-2 text-sm text-slate-300 sm:col-span-2">
                                            <label className="flex cursor-not-allowed items-center gap-2">
                                                <input type="checkbox" checked disabled readOnly className="h-4 w-4 accent-[#037ffc] dark:accent-emerald-500" />
                                                <span>Stream responses</span>
                                            </label>
                                            <HelpTooltip label="Stream responses help" align="start" iconSize={12}>
                                                Enables Server-Sent Events (SSE) token streaming so the runner can measure per-request TTFT and TPOT accurately.
                                            </HelpTooltip>
                                        </div>
                                        {form.backend === 'aiperf' && supportsFixedSchedule && (
                                            <div className="sm:col-span-2">
                                                <fieldset>
                                                    <legend className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-slate-600 dark:text-slate-400">
                                                        <span>Replay Pacing</span>
                                                        <HelpTooltip
                                                            label="Replay pacing help"
                                                            align="start"
                                                            iconSize={12}
                                                            formula="Fixed Schedule: t_target = t_trace / Scale_Factor | Concurrency: Active_Slots ≤ N"
                                                        >
                                                            Fixed Schedule replays original trace arrival timestamps; Concurrency Slots maintains a fixed number of simultaneous in-flight session/request slots via --concurrency.
                                                        </HelpTooltip>
                                                    </legend>
                                                    <ToggleGroup
                                                        options={[
                                                            { value: 'fixed', label: 'Fixed Schedule' },
                                                            { value: 'concurrency', label: 'Concurrency Slots' },
                                                        ]}
                                                        value={form.fixedSchedule ? 'fixed' : 'concurrency'}
                                                        onChange={chooseReplayPacing}
                                                        fullWidth
                                                        activeClassName="simulation-choice-active"
                                                    />
                                                </fieldset>
                                                <p className="mt-1 text-[11px] text-theme-muted">
                                                    {form.fixedSchedule
                                                        ? 'Replays recorded trace timestamps so arrival bursts match the source schedule.'
                                                        : 'Maintains N concurrent in-flight session/request slots via --concurrency.'}
                                                </p>
                                            </div>
                                        )}
                                        {isConcurrencyMode && (
                                            <div className="sm:col-span-2">
                                                <Label
                                                    htmlFor="simulation-aiperfConcurrency"
                                                    help="Maximum simultaneous in-flight session/request slots maintained by AIPerf (--concurrency)."
                                                    helpFormula="Active_InFlight_Slots ≤ Concurrency"
                                                >
                                                    Session Concurrency
                                                </Label>
                                                <Input
                                                    id="simulation-aiperfConcurrency"
                                                    type="number"
                                                    min="1"
                                                    step="1"
                                                    placeholder="16"
                                                    value={form.aiperfConcurrency}
                                                    onChange={(event) => update('aiperfConcurrency', event.target.value)}
                                                />
                                                <p className="mt-1 text-[11px] text-theme-muted">
                                                    {isPublicDataset
                                                        ? 'AIPerf public datasets always run in concurrency-slot mode (fixed schedule is not supported).'
                                                        : 'Simultaneous in-flight session/request slots maintained by AIPerf (--concurrency).'}
                                                </p>
                                            </div>
                                        )}
                                        {form.backend === 'aiperf' && (
                                            <div className="sm:col-span-2">
                                                <div className="grid gap-4 sm:grid-cols-2">
                                                    <div>
                                                        <Label
                                                            htmlFor="simulation-trajectoryStartMinRatio"
                                                            help={AIPERF_OPTION_HELP.trajectory_start_min_ratio.text}
                                                            helpFormula={AIPERF_OPTION_HELP.trajectory_start_min_ratio.formula}
                                                        >
                                                            Trajectory start min ratio
                                                        </Label>
                                                        <Input
                                                            id="simulation-trajectoryStartMinRatio"
                                                            type="number"
                                                            min={0}
                                                            max={1}
                                                            step="any"
                                                            placeholder="Unset (0.0)"
                                                            disabled={!supportsAgentxScenario}
                                                            value={form.trajectoryStartMinRatio}
                                                            onChange={(event) => update('trajectoryStartMinRatio', event.target.value)}
                                                        />
                                                    </div>
                                                    <div>
                                                        <Label
                                                            htmlFor="simulation-trajectoryStartMaxRatio"
                                                            help={AIPERF_OPTION_HELP.trajectory_start_max_ratio.text}
                                                            helpFormula={AIPERF_OPTION_HELP.trajectory_start_max_ratio.formula}
                                                        >
                                                            Trajectory start max ratio
                                                        </Label>
                                                        <Input
                                                            id="simulation-trajectoryStartMaxRatio"
                                                            type="number"
                                                            min={0}
                                                            max={1}
                                                            step="any"
                                                            placeholder="Unset (1.0)"
                                                            disabled={!supportsAgentxScenario}
                                                            value={form.trajectoryStartMaxRatio}
                                                            onChange={(event) => update('trajectoryStartMaxRatio', event.target.value)}
                                                        />
                                                    </div>
                                                </div>
                                                <p className="mt-1 text-[11px] text-theme-muted">
                                                    {supportsAgentxScenario
                                                        ? 'Applies to --scenario inferencex-agentx-mvp (default 0.25–0.75). Clear either field to omit that flag so AIPerf uses its scenario default (0.0–1.0).'
                                                        : 'Only applies to compatible Weka subagent datasets or Mooncake in Concurrency Slots mode; omitted from the request.'}
                                                </p>
                                            </div>
                                        )}
                                        <div className="sm:col-span-2">
                                            <Label
                                                htmlFor="simulation-warmupSeconds"
                                                help="Initial replay seconds excluded from steady-state KPI calculations so model caches and batch queues reach equilibrium."
                                                helpFormula="Profiling_Window = [Warmup_s, Run_Duration_s + Grace_Period_s]"
                                            >
                                                Warmup Window (seconds)
                                            </Label>
                                            <div className="flex flex-wrap items-center gap-2">
                                                <div className="min-w-[140px] flex-1">
                                                    <Input
                                                        id="simulation-warmupSeconds"
                                                        type="number"
                                                        min="0"
                                                        step="1"
                                                        value={form.warmupSeconds}
                                                        onChange={(event) => update('warmupSeconds', event.target.value)}
                                                        placeholder="0"
                                                    />
                                                </div>
                                                <Button
                                                    type="button"
                                                    variant={form.warmupSeconds === '0' ? 'primary' : 'secondary'}
                                                    size="sm"
                                                    aria-pressed={form.warmupSeconds === '0'}
                                                    data-selected={form.warmupSeconds === '0' ? 'true' : 'false'}
                                                    className="simulation-choice-pill"
                                                    onClick={() => update('warmupSeconds', '0')}
                                                >
                                                    0s
                                                </Button>
                                                <Button
                                                    type="button"
                                                    variant={
                                                        form.warmupSeconds === String(Math.max(0, Math.floor(Number(form.durationSeconds || 60) * 0.1)))
                                                        && Number(form.warmupSeconds) > 0
                                                            ? 'primary'
                                                            : 'secondary'
                                                    }
                                                    size="sm"
                                                    aria-pressed={
                                                        form.warmupSeconds === String(Math.max(0, Math.floor(Number(form.durationSeconds || 60) * 0.1)))
                                                        && Number(form.warmupSeconds) > 0
                                                    }
                                                    data-selected={
                                                        form.warmupSeconds === String(Math.max(0, Math.floor(Number(form.durationSeconds || 60) * 0.1)))
                                                        && Number(form.warmupSeconds) > 0
                                                            ? 'true'
                                                            : 'false'
                                                    }
                                                    className="simulation-choice-pill"
                                                    onClick={() => {
                                                        const tenPercent = Math.max(1, Math.floor(Number(form.durationSeconds || 60) * 0.1));
                                                        update('warmupSeconds', String(tenPercent));
                                                    }}
                                                >
                                                    10%
                                                </Button>
                                            </div>
                                            <p className="mt-1 text-[11px] text-theme-muted">
                                                Excluded from steady-state KPIs; can still be toggled after completion.
                                            </p>
                                        </div>
                                        <div className="sm:col-span-2">
                                            <Label
                                                htmlFor="simulation-gracePeriodSeconds"
                                                help="Extra drain window (0–3600 seconds) after active duration ends so already-dispatched in-flight requests can finish cleanly."
                                                helpFormula="Effective_Window_s = Run_Duration_s + Grace_Period_s"
                                            >
                                                Grace period (s)
                                            </Label>
                                            <Input
                                                id="simulation-gracePeriodSeconds"
                                                type="number"
                                                min={0}
                                                max={3600}
                                                step="1"
                                                value={form.gracePeriodSeconds}
                                                onChange={(event) => update('gracePeriodSeconds', event.target.value)}
                                                placeholder="30"
                                            />
                                            <p className="mt-1 text-[11px] text-theme-muted">
                                                Post-send drain window (0–3600 s) for in-flight requests to complete after duration elapses.
                                            </p>
                                        </div>
                                        <div className="flex items-center gap-2 sm:col-span-2">
                                            <Checkbox
                                                id="simulation-collectServerTelemetry"
                                                checked={form.collectServerTelemetry}
                                                onChange={(event) => update('collectServerTelemetry', event.target.checked)}
                                                label="Collect Server Telemetry (Prometheus & hardware metrics)"
                                            />
                                            <HelpTooltip label="Collect Server Telemetry help" align="start" iconSize={12}>
                                                Scrapes serving engine queues, KV cache occupancy, router metrics, and accelerator power/utilization from Prometheus during the run.
                                            </HelpTooltip>
                                        </div>
                                    </div>
                                </div>

                                <div className="simulation-load-card rounded-lg border border-rose-500/20 bg-rose-500/5 p-4">
                                    <h3 className="mb-3 text-sm font-semibold text-[#037ffc] dark:text-rose-300">SLA Targets</h3>
                                    <div className="grid gap-4 sm:grid-cols-2">
                                        <div>
                                            <Label
                                                htmlFor="simulation-ttftSlo"
                                                help="Target upper bound for Time To First Token in milliseconds; requests exceeding this fail TTFT SLO attainment."
                                                helpFormula="Pass iff Request_TTFT_ms ≤ TTFT_SLO_ms"
                                            >
                                                TTFT SLO (milliseconds)
                                            </Label>
                                            <Input id="simulation-ttftSlo" type="number" min="0" step="1" value={form.ttftSlo} onChange={(event) => update('ttftSlo', event.target.value)} placeholder="Optional" />
                                        </div>
                                        <div>
                                            <Label
                                                htmlFor="simulation-tpotSlo"
                                                help="Target upper bound for Time Per Output Token in milliseconds; requests exceeding this fail TPOT SLO attainment."
                                                helpFormula="Pass iff Request_TPOT_ms ≤ TPOT_SLO_ms"
                                            >
                                                TPOT SLO (milliseconds)
                                            </Label>
                                            <Input id="simulation-tpotSlo" type="number" min="0" step="1" value={form.tpotSlo} onChange={(event) => update('tpotSlo', event.target.value)} placeholder="Optional" />
                                        </div>
                                        <div>
                                            <Label
                                                htmlFor="simulation-e2eSlo"
                                                help="Target upper bound for total end-to-end request latency in seconds from dispatch to final token."
                                                helpFormula="Pass iff Request_E2E_ms ≤ E2E_SLO_s × 1000"
                                            >
                                                E2E Latency SLO (seconds)
                                            </Label>
                                            <Input id="simulation-e2eSlo" type="number" min="0" step="0.1" value={form.e2eSlo} onChange={(event) => update('e2eSlo', event.target.value)} placeholder="Optional" />
                                        </div>
                                        <div>
                                            <Label
                                                htmlFor="simulation-errorRateSlo"
                                                help="Maximum acceptable percentage (0–100%) of failed requests before the run is flagged as breaching its error budget."
                                                helpFormula="Error_Rate (%) = (Failed / Total) × 100 ≤ Error_Rate_SLO"
                                            >
                                                Error Rate SLO (%)
                                            </Label>
                                            <Input id="simulation-errorRateSlo" type="number" min="0" max="100" step="0.1" value={form.errorRateSlo} onChange={(event) => update('errorRateSlo', event.target.value)} placeholder="Optional" />
                                        </div>
                                    </div>
                                </div>
                            </div>

                        </section>}

                        {errors.length > 0 && (
                            <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-4">
                                <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-rose-200">
                                    <AlertTriangle className="h-4 w-4" /> Check the simulation configuration
                                </div>
                                <ul className="list-disc space-y-1 pl-5 text-xs text-rose-200/80">
                                    {errors.map((error) => <li key={error}>{error}</li>)}
                                </ul>
                            </div>
                        )}
                    </div>
                    </Modal>

                    <RunCompareDrawer
                        isOpen={compareOpen}
                        onClose={() => setCompareOpen(false)}
                        taskIds={compareIds}
                        tasksById={tasksById}
                        onPruneCompareIds={pruneCompareIds}
                    />

                </div>
        </ModulePage>
    );
}
