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
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
    Activity,
    AlertTriangle,
    ArrowLeft,
    Check,
    ChevronDown,
    Copy,
    Download,
    FileJson,
    FileText,
    GitCompareArrows,
    RefreshCw,
    Search,
    Send,
    Sparkles,
    Square,
    Trash2,
} from 'lucide-react';
import {
    Badge,
    Button,
    Checkbox,
    EmptyState,
    HelpTooltip,
    Input,
    Label,
    Panel,
    RefreshButton,
    Select,
    SideDrawer,
    Spinner,
    StatusChip,
    Textarea,
    ToggleGroup,
} from '../../../shared/ui';
import { PageHeading } from '../../../shared/ui/PageHeader';
import { confirmDelete } from '../../../shared/ui/confirmDelete';
import { stageAssistantDraft } from '../../../shared/lib/assistantHandoff';
import { cn } from '../../../shared/lib/cn';
import { downloadFile } from '../../../shared/lib/download';
import { formatTimestamp } from '../../../shared/lib/formatTimestamp';
import { useClipboard } from '../../../shared/lib/useClipboard';
import { usePolling } from '../../../shared/lib/usePolling';
import { useAuth } from '../../auth';
import { getDeploymentExecutions } from '../../optimization';
import {
    RUNNING_STATUSES,
    STOPPABLE_STATUSES,
    arrayFrom,
    backendOptionLabel,
    backendOptionValue,
    deriveLegacyTelemetryFromTask,
    displayName,
    formatBytes,
    formatCount,
    formatDuration,
    formatNumber,
    numberAt,
    scenarioDisplayName,
    taskBackend,
    taskDataset,
    taskStatusChip,
    unwrapTask,
} from '../model/presentation';
import {
    analyzeSimulationTaskWithAI,
    getLiveTelemetry,
    getTaskTelemetry,
    listAIProviders,
    requestJson,
} from '../model/client';
import { BottlenecksTab } from './Simulation/BottlenecksTab';
import { ConfigurationGroup, formatTaskPacing } from './Simulation/ConfigurationGroup';
import { HardwareTab, sanitizeEndpointUrl } from './Simulation/HardwareTab';
import { JsonBlock } from './Simulation/JsonBlock';
import {
    ErrorInspectorModal,
    LiveMonitorPanel,
    classifyLogLines,
    isLogErrorOrWarning,
    stripAnsi,
} from './Simulation/LiveMonitorPanel';
import { OverviewTab } from './Simulation/OverviewTab';
import { RequestsTab } from './Simulation/RequestsTab';
import { ServingTab } from './Simulation/ServingTab';
import { SystemTab } from './Simulation/SystemTab';
import { WorkloadTab } from './Simulation/WorkloadTab';

const POST_RUN_TABS = [
    { key: 'overview', label: 'Overview' },
    { key: 'workload', label: 'Workload' },
    { key: 'bottlenecks', label: 'Bottlenecks' },
    { key: 'serving', label: 'Serving' },
    { key: 'requests', label: 'Requests' },
    { key: 'system', label: 'System' },
    { key: 'hardware', label: 'Hardware' },
    { key: 'logs', label: 'Logs & Configuration' },
];

const STANDARD_ARTIFACT_KINDS = [
    {
        kind: 'report_html',
        label: 'Offline HTML Report',
        mediaType: 'text/html',
        help: 'Self-contained interactive HTML report with embedded charts, forensics verdict, and KPI summaries for offline sharing.',
    },
    {
        kind: 'report_bundle',
        label: 'Reproducible Bundle (.tar.gz)',
        mediaType: 'application/gzip',
        help: 'Compressed archive bundling the run manifest, normalized summaries, time series, and HTML report for reproducibility.',
    },
    {
        kind: 'forensics',
        label: 'Forensics Analysis (JSON)',
        mediaType: 'application/json',
        help: 'Machine-readable bottleneck verdict, root-cause evidence, and SLO attainment analysis derived from client and server telemetry.',
    },
    {
        kind: 'summary_csv',
        label: 'Summary Metrics (CSV)',
        mediaType: 'text/csv',
        help: 'Tabular CSV export of headline throughput, latency percentiles, error rate, and power/energy metrics for spreadsheets.',
    },
    {
        kind: 'summary',
        label: 'Normalized Summary (JSON)',
        mediaType: 'application/json',
        help: 'Canonical JSON summary of steady-state and full-run KPIs normalized across simulation backends.',
    },
    {
        kind: 'client_series',
        label: 'Client Time Series (JSON)',
        mediaType: 'application/json',
        help: 'Bucketed client-side throughput, TTFT/TPOT percentiles, and schedule drift over elapsed replay time.',
    },
    {
        kind: 'server_telemetry',
        label: 'Server Telemetry (JSON)',
        mediaType: 'application/json',
        help: 'Prometheus-scraped serving engine queues, KV cache usage, Inference Gateway and EPP metrics, and accelerator power/utilization series.',
    },
    {
        kind: 'sessions_summary',
        label: 'Sessions Summary (JSON)',
        mediaType: 'application/json',
        help: 'Per-session turn counts, prefix-cache reuse, and session-level latency breakdowns for multi-turn traces.',
    },
    {
        kind: 'run_manifest',
        label: 'Run Manifest (YAML)',
        mediaType: 'application/yaml',
        help: 'Redacted YAML specification recording exact target, dataset digest, backend version, and replay parameters.',
    },
    {
        kind: 'per_request',
        label: 'Per-Request Records',
        mediaType: 'application/x-ndjson',
        help: 'Newline-delimited JSON (NDJSON) trace of individual request timestamps, token counts, latencies, and error statuses.',
    },
    {
        kind: 'stdout',
        label: 'Runner Stdout Log',
        mediaType: 'text/plain',
        help: 'Raw standard output captured from the benchmark runner process during execution.',
    },
    {
        kind: 'stderr',
        label: 'Runner Stderr Log',
        mediaType: 'text/plain',
        help: 'Raw standard error stream from the benchmark runner process, useful for diagnosing crashes or HTTP failures.',
    },
];

const BACKEND_OPTION_HELP = {
    warmup_seconds: {
        text: 'Initial replay seconds excluded from steady-state KPIs while caches and queues warm up.',
        formula: 'Profiling_Window = [Warmup_s, Run_Duration_s + Grace_Period_s]',
    },
    random_seed: 'Deterministic PRNG seed (integer ≥ 0, default 666) controlling prompt synthesis or sampling repeatability.',
    num_profile_runs: 'Number of consecutive profiling iterations executed for this simulation task.',
    profile_run_cooldown_seconds: {
        text: 'Pause in seconds between repeated profiling runs so server queues drain.',
        formula: 'Total_Elapsed_s ≈ num_runs × Duration_s + (num_runs − 1) × Cooldown_s',
    },
    concurrency: {
        text: 'Maximum number of simultaneous in-flight session/request slots maintained by the benchmark runner (--concurrency).',
        formula: 'Active_InFlight_Slots ≤ Concurrency',
    },
    max_context_length: {
        text: 'Upper token-length cap enforced on replayed prompt plus output tokens.',
        formula: 'Prompt_Tokens + Output_Tokens ≤ Max_Context_Length',
    },
    trace_idle_gap_cap_seconds: {
        text: 'Maximum idle gap (s) preserved between trace bursts before clamping inter-arrival waits.',
        formula: 'Effective_Idle_Gap_s = min(Source_Δt_s, Idle_Gap_Cap_s)',
    },
    trajectory_start_min_ratio: {
        text: 'Minimum normalized progress ratio [0, 1] along each source agentic trajectory where replay sessions may begin (--trajectory-start-min-ratio).',
        formula: '0 ≤ Trajectory_Start_Min_Ratio ≤ Trajectory_Start_Max_Ratio ≤ 1',
    },
    trajectory_start_max_ratio: {
        text: 'Maximum normalized progress ratio [0, 1] along each source agentic trajectory where replay sessions may begin (--trajectory-start-max-ratio).',
        formula: '0 ≤ Trajectory_Start_Min_Ratio ≤ Trajectory_Start_Max_Ratio ≤ 1',
    },
    collect_server_telemetry: 'Whether Prometheus serving engine and accelerator hardware metrics were collected during the run.',
    e2e_slo: {
        text: 'End-to-end request latency Service Level Objective threshold in seconds.',
        formula: 'Pass iff Request_E2E_ms ≤ E2E_SLO_s × 1000',
    },
};

function backendOptionHelp(key) {
    return BACKEND_OPTION_HELP[key] || `Backend-specific execution parameter (${displayName(key)}) configured for this simulation run.`;
}

function nonEmptyHardwareText(value) {
    if (typeof value !== 'string') return null;
    const trimmed = value.trim();
    return trimmed || null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function resolveTaskHardware(task, effectiveTelemetry = null) {
    const direct = nonEmptyHardwareText(task?.hardware);
    if (direct) return direct;

    const serverTelemetry = effectiveTelemetry?.server_telemetry;
    const summary = nonEmptyHardwareText(serverTelemetry?.accelerator_summary);
    if (summary) return summary;

    const profiles = Array.isArray(serverTelemetry?.hardware_profiles)
        ? serverTelemetry.hardware_profiles
        : [];
    for (const profile of profiles) {
        const models = Array.isArray(profile?.models) ? profile.models : [];
        for (const model of models) {
            const resolvedModel = nonEmptyHardwareText(model);
            if (resolvedModel) return resolvedModel;
        }
    }

    const profileName = nonEmptyHardwareText(profiles[0]?.display_name);
    if (profileName) return profileName;

    const manifestProfileName = nonEmptyHardwareText(
        effectiveTelemetry?.run_manifest?.hardware_profile?.display_name,
    );
    if (manifestProfileName) return manifestProfileName;

    return 'External / Not reported';
}

export function LogsAndConfigTab({
    task,
    telemetry,
    artifacts,
    downloadingArtifact,
    artifactDownloadError,
    onDownloadArtifact,
    artifactsHubRef,
}) {
    const [logQuery, setLogQuery] = useState('');
    const [errorsOnly, setErrorsOnly] = useState(false);
    const { copiedValue, copy } = useClipboard(1500);

    const resultPayload = task?.result || task?.results || task?.output || null;
    const result = Array.isArray(resultPayload) ? resultPayload[0] || null : resultPayload;
    const backendMetrics = result?.backend_metrics || result?.backendMetrics || {};
    const backendOptions = task?.simulation?.backend_options || task?.backend_options || task?.config_snapshot?.backend_options || {};
    const backendOptionEntries = Object.entries(backendOptions).filter(
        ([key, value]) => !['ttft_slo', 'tpot_slo', 'error_rate_slo'].includes(key)
            && value !== null
            && value !== undefined
            && value !== '',
    );
    const ttftSloMs = numberAt(backendOptions, ['ttft_slo']);
    const tpotSloMs = numberAt(backendOptions, ['tpot_slo']);
    const e2eSloS = numberAt(backendOptions, ['e2e_slo']);
    const errorRateSlo = numberAt(backendOptions, ['error_rate_slo']);
    const runManifest = telemetry?.run_manifest || null;
    const taskHardware = resolveTaskHardware(task, telemetry);
    const taskPacing = formatTaskPacing(task, telemetry);
    const warmupSeconds = numberAt(backendOptions, ['warmup_seconds'])
        ?? numberAt(runManifest || {}, ['replay.warmup_seconds', 'warmup_seconds'])
        ?? 0;
    const randomSeed = numberAt(runManifest || {}, ['replay.backend_options.random_seed', 'backend_options.random_seed'])
        ?? numberAt(backendOptions, ['random_seed']);
    const trajectoryStartMinRatio = numberAt(runManifest || {}, ['replay.backend_options.trajectory_start_min_ratio', 'backend_options.trajectory_start_min_ratio'])
        ?? numberAt(backendOptions, ['trajectory_start_min_ratio']);
    const trajectoryStartMaxRatio = numberAt(runManifest || {}, ['replay.backend_options.trajectory_start_max_ratio', 'backend_options.trajectory_start_max_ratio'])
        ?? numberAt(backendOptions, ['trajectory_start_max_ratio']);
    const trace = task?.prompt?.trace || {};
    const replayStartSeconds = numberAt(trace, ['start_seconds']) ?? 0;
    const configuredReplayEndSeconds = numberAt(trace, ['end_seconds']);
    const replayDurationSeconds = numberAt(task?.simulation || {}, ['duration_seconds'])
        ?? numberAt(runManifest || {}, ['replay.duration_seconds', 'simulation.duration_seconds', 'duration_seconds']);
    const gracePeriodSeconds = numberAt(runManifest || {}, ['replay.grace_period_seconds', 'simulation.grace_period_seconds', 'grace_period_seconds'])
        ?? numberAt(task?.simulation || {}, ['grace_period_seconds'])
        ?? numberAt(task || {}, ['grace_period_seconds']);
    const replayScaleFactor = numberAt(trace, ['synthesis_speedup_ratio'])
        ?? numberAt(task || {}, ['scale_factor'])
        ?? numberAt(runManifest || {}, ['replay.scale_factor', 'dataset.scale_factor', 'scale_factor'])
        ?? 1;
    const replayEndSeconds = configuredReplayEndSeconds
        ?? (replayDurationSeconds !== null ? replayStartSeconds + replayDurationSeconds * replayScaleFactor : null);

    const rawLogs = useMemo(() => {
        const taskLogLines = Array.isArray(task?.logs) ? task.logs : [];
        const runnerLogs = Array.isArray(telemetry?.runner_logs) ? telemetry.runner_logs : [];
        return [...taskLogLines, ...runnerLogs].map((line) => (
            stripAnsi(typeof line === 'string' ? line : JSON.stringify(line))
        ));
    }, [task?.logs, telemetry?.runner_logs]);

    const classifiedLogs = useMemo(() => classifyLogLines(rawLogs), [rawLogs]);

    const filteredLogs = useMemo(() => {
        const needle = logQuery.trim().toLowerCase();
        return classifiedLogs.filter((entry) => {
            if (errorsOnly && !isLogErrorOrWarning(entry.severity)) return false;
            if (needle && !entry.text.toLowerCase().includes(needle)) return false;
            return true;
        });
    }, [classifiedLogs, errorsOnly, logQuery]);

    const allLogsText = useMemo(
        () => filteredLogs.map((entry) => entry.text).join('\n'),
        [filteredLogs],
    );

    const artifactRows = useMemo(() => {
        const byKind = new Map();
        (Array.isArray(artifacts) ? artifacts : []).forEach((artifact) => {
            if (artifact?.kind) byKind.set(artifact.kind, artifact);
        });
        const merged = STANDARD_ARTIFACT_KINDS.map((entry) => {
            const existing = byKind.get(entry.kind);
            if (existing) byKind.delete(entry.kind);
            return {
                kind: entry.kind,
                label: entry.label,
                help: entry.help,
                media_type: existing?.media_type || entry.mediaType,
                size_bytes: existing?.size_bytes ?? null,
                sha256: existing?.sha256 || existing?.digest || null,
                integrity: existing?.integrity || (existing ? 'verified' : 'on-demand'),
                name: existing?.name || null,
            };
        });
        byKind.forEach((extra, kind) => {
            merged.push({
                kind,
                label: displayName(kind),
                help: `Run artifact (${displayName(kind)}) recorded or generated for this simulation task.`,
                media_type: extra.media_type || 'application/octet-stream',
                size_bytes: extra.size_bytes ?? null,
                sha256: extra.sha256 || extra.digest || null,
                integrity: extra.integrity || 'verified',
                name: extra.name || null,
            });
        });
        return merged;
    }, [artifacts]);

    return (
        <div className="space-y-5">
            <Panel
                title="Run Manifest & Configuration"
                help="Authoritative target, dataset, timing, SLO, and backend settings captured for this simulation run."
            >
                {runManifest && typeof runManifest === 'object' ? (
                    <div className="space-y-4">
                        <div className="grid gap-4 xl:grid-cols-2">
                            <ConfigurationGroup
                                title="Target & Topology"
                                help="Inference endpoint, served model, and cluster topology targeted by the replay."
                                items={[
                                    ['Endpoint mode', runManifest.target?.endpoint_mode || displayName(task?.endpoint_mode), 'Whether requests targeted a cluster-managed Model Service or an external HTTP(S) URL.'],
                                    ['Endpoint', sanitizeEndpointUrl(runManifest.target?.sanitized_endpoint_url) || sanitizeEndpointUrl(task?.endpoint_url), 'Sanitized inference base URL that received benchmark traffic.'],
                                    ['Model', runManifest.target?.model_name || task?.model_name, 'Model identifier passed in completion/chat API requests.'],
                                    ['Hardware', taskHardware, 'Accelerator model or hardware profile detected on the target serving nodes or cluster during the run.'],
                                    ['Cluster / Namespace', [runManifest.target?.cluster_name, runManifest.target?.namespace].filter(Boolean).join(' / ') || null, 'Kubernetes cluster and namespace hosting the target deployment.'],
                                    ['Topology mode', runManifest.target?.topology_mode ? displayName(runManifest.target.topology_mode) : null, 'Serving architecture (Aggregated unified workers vs. P/D Disaggregation (Prefill and Decode pools)).'],
                                ]}
                            />
                            <ConfigurationGroup
                                title="Dataset & Replay Parameters"
                                help="Benchmark backend, workload dataset digest, replay timing windows, and clock sync mode."
                                items={[
                                    ['Backend', `${runManifest.backend || taskBackend(task)} ${runManifest.backend_version || task?.backend_version || ''}`.trim(), 'Load generator tool and version used to execute the replay.'],
                                    ['Dataset', runManifest.dataset?.name || taskDataset(task), 'Trace or prompt corpus replayed against the inference endpoint.'],
                                    ['Pacing', taskPacing.label, 'Replay pacing mode: Fixed Schedule replays source trace timestamps, while Concurrency (N) maintains N simultaneous in-flight session/request slots.', 'Fixed Schedule: t_target = t_trace / Scale_Factor | Concurrency: Active_Slots ≤ N'],
                                    ['Session Concurrency', taskPacing.concurrency != null ? String(taskPacing.concurrency) : null, 'Maximum simultaneous in-flight session/request slots maintained by the benchmark runner (--concurrency).', 'Active_InFlight_Slots ≤ Concurrency'],
                                    ['Dataset SHA-256', runManifest.dataset?.sha256 ? String(runManifest.dataset.sha256).slice(0, 16) : null, 'Truncated SHA-256 content digest verifying exact trace input reproducibility.'],
                                    ['Duration', replayDurationSeconds !== null ? `${replayDurationSeconds} s` : null, 'Active request dispatch duration (seconds) excluding the post-send grace drain window.', 'Replay_Duration_s = Selected_Trace_Span_s / Scale_Factor'],
                                    ['Grace period', gracePeriodSeconds !== null ? `${gracePeriodSeconds} s` : null, 'Post-send drain window (seconds) allowing in-flight requests to complete after duration ends.', 'Effective_Window_s = Run_Duration_s + Grace_Period_s'],
                                    ['Warmup window', `${warmupSeconds} s`, 'Initial seconds excluded from steady-state profiling KPIs so caches and queues stabilize.', 'Profiling_Window = [Warmup_s, Run_Duration_s + Grace_Period_s]'],
                                    ['Random seed', randomSeed !== null ? String(randomSeed) : null, BACKEND_OPTION_HELP.random_seed],
                                    ['Trajectory start min ratio', trajectoryStartMinRatio !== null ? String(trajectoryStartMinRatio) : null, BACKEND_OPTION_HELP.trajectory_start_min_ratio.text, BACKEND_OPTION_HELP.trajectory_start_min_ratio.formula],
                                    ['Trajectory start max ratio', trajectoryStartMaxRatio !== null ? String(trajectoryStartMaxRatio) : null, BACKEND_OPTION_HELP.trajectory_start_max_ratio.text, BACKEND_OPTION_HELP.trajectory_start_max_ratio.formula],
                                    ['Clock alignment', runManifest.clock?.alignment_mode || null, 'Method used to align client timestamps with server-side Prometheus scrape windows.'],
                                ]}
                            />
                        </div>
                        <details>
                            <summary className="cursor-pointer text-xs font-semibold text-theme-muted hover:text-theme-text">
                                View redacted run-manifest.yaml structure
                            </summary>
                            <div className="mt-2">
                                <JsonBlock value={runManifest} />
                            </div>
                        </details>
                    </div>
                ) : (
                    <div className="grid gap-4 xl:grid-cols-2">
                        <ConfigurationGroup
                            title="Endpoint"
                            help="Target inference endpoint, model identifier, and response streaming mode."
                            items={[
                                ['Endpoint mode', task?.endpoint_mode ? displayName(task.endpoint_mode) : 'External', 'Whether requests targeted a cluster-managed Model Service or an external HTTP(S) URL.'],
                                ...(String(task?.endpoint_mode).toLowerCase() === 'deployment'
                                    ? [
                                        ['Cluster', task?.endpoint_cluster_name, 'Kubernetes cluster hosting the target model deployment.'],
                                        ['Deployment', task?.endpoint_deployment_name, 'Deployment or Model Service name receiving benchmark traffic.'],
                                    ]
                                    : []),
                                ['Endpoint', sanitizeEndpointUrl(task?.endpoint_url), 'Inference endpoint URL that received benchmark requests.'],
                                ['Model', task?.model_name, 'Served model name sent in each inference request payload.'],
                                ['Hardware', taskHardware, 'Accelerator model or hardware profile detected on the target serving nodes or cluster during the run.'],
                                ['Streaming', task?.simulation?.stream === undefined ? null : task.simulation.stream ? 'Enabled' : 'Disabled', 'Whether SSE token streaming was enabled; required to measure TTFT and TPOT accurately.'],
                            ]}
                        />
                        <ConfigurationGroup
                            title="Replay Workload"
                            help="Scenario, trace dataset, source time slice, timing windows, and rate scaling."
                            items={[
                                ['Scenario', scenarioDisplayName(task?.scenario), 'Workload category (Chat, Tool & API Use, or Code Generation) used to filter compatible traces.'],
                                ['Dataset', taskDataset(task), 'Trace dataset or corpus replayed during this simulation run.'],
                                ['Trace format', trace.format || task?.trace_format, 'Input trace schema (for example, BailianTrace or Mooncake) parsed by the backend.'],
                                ['Pacing', taskPacing.label, 'Replay pacing mode: Fixed Schedule replays source trace timestamps, while Concurrency (N) maintains N simultaneous in-flight session/request slots.', 'Fixed Schedule: t_target = t_trace / Scale_Factor | Concurrency: Active_Slots ≤ N'],
                                ['Session Concurrency', taskPacing.concurrency != null ? String(taskPacing.concurrency) : null, 'Maximum simultaneous in-flight session/request slots maintained by the benchmark runner (--concurrency).', 'Active_InFlight_Slots ≤ Concurrency'],
                                ['Replay range', replayEndSeconds !== null ? `${formatNumber(replayStartSeconds)}–${formatNumber(replayEndSeconds)} source seconds` : null, 'Start and end timestamps (in source trace seconds) sliced from the trace file.', 'Selected_Span_s = Replay_End − Replay_Start'],
                                ['Duration', replayDurationSeconds !== null ? `${replayDurationSeconds} s` : null, 'Configured active replay duration before the grace drain window.', 'Replay_Duration_s = Selected_Trace_Span_s / Scale_Factor'],
                                ['Grace period', gracePeriodSeconds !== null ? `${gracePeriodSeconds} s` : null, 'Extra seconds after replay duration ends for in-flight requests to complete.', 'Effective_Window_s = Run_Duration_s + Grace_Period_s'],
                                ['Warmup window', `${warmupSeconds} s`, 'Initial seconds excluded from steady-state profiling KPIs.', 'Profiling_Window = [Warmup_s, Run_Duration_s + Grace_Period_s]'],
                                ['Scale factor', replayScaleFactor, 'Time-compression multiplier applied to trace inter-arrival intervals (>1x increases arrival rate).', 'Arrival_Interval = Base_Trace_Δt / Scale_Factor'],
                            ]}
                        />
                        <ConfigurationGroup
                            title="SLO Targets"
                            help="Configured Service Level Objective thresholds used to evaluate goodput and SLO attainment."
                            items={[
                                ['TTFT SLO', ttftSloMs === null ? 'Not set' : `${formatNumber(ttftSloMs * 1000)} ms`, 'Maximum acceptable Time to First Token latency (ms) before a request misses its SLO.', 'Pass iff Request_TTFT_ms ≤ TTFT_SLO_ms'],
                                ['TPOT SLO', tpotSloMs === null ? 'Not set' : `${formatNumber(tpotSloMs * 1000)} ms`, 'Maximum acceptable Time per Output Token (ms) during decode streaming.', 'Pass iff Request_TPOT_ms ≤ TPOT_SLO_ms'],
                                ['E2E SLO', e2eSloS === null ? 'Not set' : `${formatNumber(e2eSloS)} s`, 'Maximum acceptable end-to-end request completion latency (seconds).', 'Pass iff Request_E2E_ms ≤ E2E_SLO_s × 1000'],
                                ['Error rate SLO', errorRateSlo === null ? 'Not set' : `${formatNumber(errorRateSlo)}%`, 'Maximum acceptable percentage of failed requests across the run.', 'Error_Rate (%) = (Failed / Total) × 100 ≤ Error_Rate_SLO'],
                            ]}
                        />
                        {backendOptionEntries.length > 0 && (
                            <ConfigurationGroup
                                title="Backend Options"
                                help="Backend-specific concurrency, threading, and execution tuning options for this run."
                                items={backendOptionEntries.map(([key, value]) => [
                                    backendOptionLabel(key),
                                    backendOptionValue(key, value),
                                    backendOptionHelp(key),
                                ])}
                            />
                        )}
                    </div>
                )}
            </Panel>

            <Panel
                title="Task & Runner Logs"
                help="Combined task lifecycle events and runner stdout/stderr log lines. Filter by keyword or restrict to warnings and HTTP/runtime errors."
                actions={(
                    <div className="flex items-center gap-2">
                        <Badge tone="neutral" size="xs">{formatCount(filteredLogs.length)} lines</Badge>
                        {allLogsText && (
                            <Button variant="ghost" size="xs" onClick={() => void copy(allLogsText)}>
                                {copiedValue === allLogsText ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
                                {copiedValue === allLogsText ? 'Copied' : 'Copy'}
                            </Button>
                        )}
                    </div>
                )}
            >
                <div className="space-y-3">
                    <div className="flex flex-wrap items-center gap-3">
                        <div className="min-w-[200px] flex-1">
                            <Input
                                icon={Search}
                                value={logQuery}
                                onChange={(event) => setLogQuery(event.target.value)}
                                placeholder="Search logs..."
                                aria-label="Search logs"
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
                        <div className="max-h-80 overflow-auto rounded-lg border border-theme-border bg-theme-bg p-3 font-mono text-[11px] leading-relaxed text-theme-text">
                            {filteredLogs.map((entry, index) => (
                                <div
                                    key={`${index}-${entry.text.slice(0, 24)}`}
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
                        <p className="py-8 text-center text-xs text-theme-muted">
                            {rawLogs.length ? 'No log lines match the search filter.' : 'No task log entries were recorded.'}
                        </p>
                    )}
                </div>
            </Panel>

            <div ref={artifactsHubRef}>
                <Panel
                    title="Artifacts Download Hub"
                    help="Downloadable reports, normalized telemetry bundles, per-request traces, and runner logs with SHA-256 integrity status."
                >
                    {artifactDownloadError && (
                        <div role="alert" className="mb-3 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-700 dark:text-red-300">
                            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                            <span>{artifactDownloadError}</span>
                        </div>
                    )}
                    <ul className="grid gap-2.5 md:grid-cols-2">
                        {artifactRows.map((artifact) => (
                            <li
                                key={artifact.kind}
                                className="flex items-center justify-between gap-3 rounded-xl border border-theme-border bg-theme-bg/60 p-3"
                            >
                                <div className="min-w-0">
                                    <div className="flex flex-wrap items-center gap-1.5 text-xs font-semibold text-theme-text">
                                        <FileJson className="h-3.5 w-3.5 shrink-0 text-sky-500" />
                                        <span className="truncate">{artifact.label}</span>
                                        {artifact.help && (
                                            <HelpTooltip label={`${artifact.label} help`} align="start" iconSize={12}>
                                                {artifact.help}
                                            </HelpTooltip>
                                        )}
                                        <Badge tone={artifact.integrity === 'verified' ? 'success' : 'neutral'} size="xxs">
                                            {artifact.integrity}
                                        </Badge>
                                        <HelpTooltip label={`${artifact.label} integrity help`} align="start" iconSize={11}>
                                            {artifact.integrity === 'verified'
                                                ? 'Persisted artifact on disk with verified digest/size metadata.'
                                                : 'Generated dynamically on demand when downloaded.'}
                                        </HelpTooltip>
                                    </div>
                                    <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px] text-theme-muted">
                                        <span>{artifact.media_type}</span>
                                        {artifact.size_bytes !== null && <span>· {formatBytes(artifact.size_bytes)}</span>}
                                        {artifact.sha256 && <span>· sha256:{String(artifact.sha256).slice(0, 12)}</span>}
                                    </div>
                                </div>
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    onClick={() => onDownloadArtifact(artifact)}
                                    disabled={downloadingArtifact === artifact.kind}
                                    isLoading={downloadingArtifact === artifact.kind}
                                >
                                    <Download className="h-3.5 w-3.5" />
                                    Download
                                </Button>
                            </li>
                        ))}
                    </ul>
                    <div className="mt-4 space-y-2 border-t border-theme-border pt-4">
                        <details>
                            <summary className="cursor-pointer text-xs font-semibold text-theme-muted hover:text-theme-text">
                                Raw backend metrics
                            </summary>
                            <div className="mt-3"><JsonBlock value={backendMetrics} /></div>
                        </details>
                        <details>
                            <summary className="cursor-pointer text-xs font-semibold text-theme-muted hover:text-theme-text">
                                Raw normalized result
                            </summary>
                            <div className="mt-3"><JsonBlock value={result} /></div>
                        </details>
                    </div>
                </Panel>
            </div>
        </div>
    );
}

const ASK_AI_QUICK_PROMPTS = [
    'Identify primary bottleneck & vLLM / llm-d tuning parameters',
    'Analyze KV cache, prefix reuse & offload efficiency',
    'Diagnose bottlenecks and propose an end-to-end tuning plan',
];

export const ASK_AI_STORAGE_KEY = 'lens.simulation.askAi.v1';
export const ASK_AI_MAX_SAVED_TASKS = 30;

function getAskAiStorage(storage) {
    if (storage !== undefined) return storage;
    if (typeof window === 'undefined') return null;
    try {
        return window.localStorage || null;
    } catch {
        return null;
    }
}

function normalizeAskAiMessage(message) {
    if (!message || typeof message !== 'object') return null;
    if (message.role !== 'user' && message.role !== 'assistant') return null;
    if (typeof message.content !== 'string' || message.content.length === 0) return null;
    if (message.role === 'user') {
        return { role: 'user', content: message.content };
    }
    return {
        role: 'assistant',
        content: message.content,
        providerName: typeof message.providerName === 'string' ? message.providerName : '',
        model: typeof message.model === 'string' ? message.model : '',
        generatedAt: typeof message.generatedAt === 'string' ? message.generatedAt : '',
        telemetryScope: typeof message.telemetryScope === 'string' ? message.telemetryScope : '',
    };
}

function getLatestAssistantTimestamp(messages) {
    if (!Array.isArray(messages)) return '';
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const msg = messages[i];
        if (msg?.role === 'assistant' && typeof msg.generatedAt === 'string' && msg.generatedAt.trim()) {
            return msg.generatedAt;
        }
    }
    return '';
}

function normalizeAskAiEntry(entry) {
    if (!entry || typeof entry !== 'object' || !Array.isArray(entry.messages)) return null;
    const messages = entry.messages.map(normalizeAskAiMessage).filter(Boolean);
    if (messages.length === 0) return null;
    const updatedAt = typeof entry.updatedAt === 'string' && entry.updatedAt.trim()
        ? entry.updatedAt
        : getLatestAssistantTimestamp(messages);
    return {
        messages,
        providerId: typeof entry.providerId === 'string' ? entry.providerId : '',
        model: typeof entry.model === 'string' ? entry.model : '',
        updatedAt,
    };
}

function readAskAiStore(targetStorage) {
    if (!targetStorage || typeof targetStorage.getItem !== 'function') return {};
    try {
        const raw = targetStorage.getItem(ASK_AI_STORAGE_KEY);
        if (!raw || typeof raw !== 'string') return {};
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
        const cleaned = {};
        for (const [key, value] of Object.entries(parsed)) {
            if (!key) continue;
            const normalized = normalizeAskAiEntry(value);
            if (normalized) cleaned[key] = normalized;
        }
        return cleaned;
    } catch {
        return {};
    }
}

// eslint-disable-next-line react-refresh/only-export-components
export function loadSavedAskAiAnalysis(taskId, storage) {
    const key = String(taskId || '').trim();
    if (!key) return null;
    const targetStorage = getAskAiStorage(storage);
    if (!targetStorage) return null;
    const store = readAskAiStore(targetStorage);
    return store[key] || null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function saveAskAiAnalysis(taskId, entry, storage) {
    const key = String(taskId || '').trim();
    if (!key) return null;
    const targetStorage = getAskAiStorage(storage);
    if (!targetStorage || typeof targetStorage.setItem !== 'function') return null;

    const normalized = normalizeAskAiEntry({
        ...entry,
        updatedAt: typeof entry?.updatedAt === 'string' && entry.updatedAt.trim()
            ? entry.updatedAt
            : new Date().toISOString(),
    });
    if (!normalized) return null;

    const store = readAskAiStore(targetStorage);
    if (Object.prototype.hasOwnProperty.call(store, key)) {
        delete store[key];
    }
    store[key] = normalized;

    const ranked = Object.entries(store).map(([entryKey, value], index) => {
        const parsedTime = Date.parse(value.updatedAt || '');
        return {
            key: entryKey,
            value,
            time: Number.isFinite(parsedTime) ? parsedTime : 0,
            index,
        };
    });
    ranked.sort((a, b) => (a.time !== b.time ? a.time - b.time : a.index - b.index));

    let trimmed = ranked.slice(-ASK_AI_MAX_SAVED_TASKS).map((item) => [item.key, item.value]);
    while (trimmed.length > 0) {
        try {
            targetStorage.setItem(ASK_AI_STORAGE_KEY, JSON.stringify(Object.fromEntries(trimmed)));
            return normalized;
        } catch {
            if (trimmed.length <= 1) return null;
            trimmed = trimmed.slice(1);
        }
    }
    return null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function clearSavedAskAiAnalysis(taskId, storage) {
    const key = String(taskId || '').trim();
    if (!key) return false;
    const targetStorage = getAskAiStorage(storage);
    if (!targetStorage || typeof targetStorage.getItem !== 'function') return false;
    try {
        const raw = targetStorage.getItem(ASK_AI_STORAGE_KEY);
        if (!raw) return true;
        const store = readAskAiStore(targetStorage);
        if (!Object.prototype.hasOwnProperty.call(store, key)) return true;
        delete store[key];
        if (Object.keys(store).length === 0 && typeof targetStorage.removeItem === 'function') {
            targetStorage.removeItem(ASK_AI_STORAGE_KEY);
            return true;
        }
        if (typeof targetStorage.setItem === 'function') {
            targetStorage.setItem(ASK_AI_STORAGE_KEY, JSON.stringify(store));
            return true;
        }
        return false;
    } catch {
        return false;
    }
}

const MAX_HANDOFF_PROMPT_CHARS = 3900;

function buildAssistantHandoffPrompt({ task, taskId, analysisContent }) {
    const modelName = task?.model_name || 'unknown';
    const scenarioName = scenarioDisplayName(task?.scenario) || 'unknown';
    const header = [
        `Analyze simulation task ${taskId}:`,
        `- Model: ${modelName}`,
        `- Scenario: ${scenarioName}`,
        `- Backend: ${displayName(taskBackend(task))}`,
        `- Dataset: ${taskDataset(task)}`,
    ].join('\n');
    const summary = String(analysisContent || '').trim();
    const footer = '\n\nPlease help inspect the simulation telemetry further and propose next tuning steps.';
    if (!summary) return `${header}${footer}`.slice(0, MAX_HANDOFF_PROMPT_CHARS);
    const prefix = '\n\nLatest AI bottleneck analysis:\n';
    const maxSummaryChars = Math.max(0, MAX_HANDOFF_PROMPT_CHARS - header.length - prefix.length - footer.length);
    const clippedSummary = summary.length > maxSummaryChars
        ? `${summary.slice(0, Math.max(0, maxSummaryChars - 3))}...`
        : summary;
    return `${header}${prefix}${clippedSummary}${footer}`.slice(0, MAX_HANDOFF_PROMPT_CHARS);
}

function resolveAskAiProviderSelection(items, preferredProviderId, preferredModel) {
    if (!Array.isArray(items) || items.length === 0) {
        return { providerId: '', model: '' };
    }
    if (preferredProviderId) {
        const matched = items.find((provider) => provider.id === preferredProviderId);
        if (matched) {
            return {
                providerId: matched.id,
                model: preferredModel || matched.model || '',
            };
        }
    }
    const fallback = items[0];
    return {
        providerId: fallback.id,
        model: fallback.model || '',
    };
}

export function AskAiModal({ isOpen, onClose, task, taskId, onNavigate }) {
    const auth = useAuth();
    const canConfigureProviders = auth?.canView ? auth.canView('ai-providers') : true;
    const canOpenAssistant = auth?.canView ? auth.canView('playground') : true;

    const initialSavedRef = useRef(undefined);
    if (initialSavedRef.current === undefined) {
        initialSavedRef.current = isOpen && taskId ? loadSavedAskAiAnalysis(taskId) : null;
    }
    const initialSaved = initialSavedRef.current;

    const [aiProviders, setAiProviders] = useState([]);
    const [providersLoading, setProvidersLoading] = useState(false);
    const [providersReady, setProvidersReady] = useState(false);
    const [providersRestricted, setProvidersRestricted] = useState(false);
    const [providersError, setProvidersError] = useState('');
    const [selectedProviderId, setSelectedProviderId] = useState(() => initialSaved?.providerId || '');
    const [modelOverride, setModelOverride] = useState(() => initialSaved?.model || '');
    const [messages, setMessages] = useState(() => initialSaved?.messages || []);
    const [savedUpdatedAt, setSavedUpdatedAt] = useState(
        () => (initialSaved ? (initialSaved.updatedAt || getLatestAssistantTimestamp(initialSaved.messages) || '') : ''),
    );
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [question, setQuestion] = useState('');
    const [syncedTaskKey, setSyncedTaskKey] = useState(() => (isOpen ? String(taskId || '') : ''));
    const { copiedValue, copy } = useClipboard(1500);

    const providersAbortRef = useRef(null);
    const analysisAbortRef = useRef(null);
    const analysisRequestIdRef = useRef(0);
    const autoTriggeredTaskRef = useRef(initialSaved?.messages?.length ? taskId : '');
    const aiProvidersRef = useRef(aiProviders);
    const providersReadyRef = useRef(providersReady);
    const providersRestrictedRef = useRef(providersRestricted);
    const modelOverrideRef = useRef(modelOverride);
    const messagesRef = useRef(messages);
    const savedSelectionRef = useRef({
        providerId: initialSaved?.providerId || '',
        model: initialSaved?.model || '',
    });
    const messagesEndRef = useRef(null);

    useEffect(() => {
        modelOverrideRef.current = modelOverride;
    }, [modelOverride]);

    useEffect(() => {
        messagesRef.current = messages;
    }, [messages]);

    useEffect(() => {
        analysisAbortRef.current?.abort();
        analysisRequestIdRef.current += 1;
        setLoading(false);
        setError('');
        setQuestion('');

        if (!isOpen) {
            providersReadyRef.current = false;
            providersRestrictedRef.current = false;
            setProvidersReady(false);
            setProvidersRestricted(false);
            setSyncedTaskKey('');
            if (!taskId) {
                setMessages([]);
                messagesRef.current = [];
                setSavedUpdatedAt('');
                savedSelectionRef.current = { providerId: '', model: '' };
                setSelectedProviderId('');
                setModelOverride('');
                autoTriggeredTaskRef.current = '';
            }
            return undefined;
        }

        const saved = taskId ? loadSavedAskAiAnalysis(taskId) : null;
        if (saved && saved.messages.length > 0) {
            const latestAt = getLatestAssistantTimestamp(saved.messages);
            setMessages(saved.messages);
            messagesRef.current = saved.messages;
            setSavedUpdatedAt(saved.updatedAt || latestAt || '');
            savedSelectionRef.current = {
                providerId: saved.providerId || '',
                model: saved.model || '',
            };
            autoTriggeredTaskRef.current = taskId;
        } else {
            setMessages([]);
            messagesRef.current = [];
            setSavedUpdatedAt('');
            savedSelectionRef.current = { providerId: '', model: '' };
            autoTriggeredTaskRef.current = '';
        }

        if (providersReadyRef.current) {
            if (providersRestrictedRef.current) {
                setSelectedProviderId('');
                setModelOverride(savedSelectionRef.current.model || '');
            } else {
                const resolved = resolveAskAiProviderSelection(
                    aiProvidersRef.current,
                    savedSelectionRef.current.providerId,
                    savedSelectionRef.current.model,
                );
                setSelectedProviderId(resolved.providerId);
                setModelOverride(resolved.model);
            }
        } else {
            setSelectedProviderId(savedSelectionRef.current.providerId);
            setModelOverride(savedSelectionRef.current.model);
        }
        setSyncedTaskKey(String(taskId || ''));
        return undefined;
    }, [isOpen, taskId]);

    useEffect(() => () => {
        providersAbortRef.current?.abort();
        analysisAbortRef.current?.abort();
    }, []);

    const loadProviders = useCallback(async () => {
        providersAbortRef.current?.abort();
        const controller = new AbortController();
        providersAbortRef.current = controller;
        const { signal } = controller;

        providersReadyRef.current = false;
        providersRestrictedRef.current = false;
        setProvidersLoading(true);
        setProvidersReady(false);
        setProvidersRestricted(false);
        setProvidersError('');
        try {
            const items = await listAIProviders({ signal });
            if (signal.aborted || providersAbortRef.current !== controller) return;
            aiProvidersRef.current = items;
            providersRestrictedRef.current = false;
            providersReadyRef.current = true;
            setAiProviders(items);
            const resolved = resolveAskAiProviderSelection(
                items,
                savedSelectionRef.current.providerId,
                savedSelectionRef.current.model,
            );
            setSelectedProviderId(resolved.providerId);
            setModelOverride(resolved.model);
            setProvidersReady(true);
        } catch (nextError) {
            if (signal.aborted || nextError?.name === 'AbortError' || providersAbortRef.current !== controller) return;
            if (nextError?.status === 403) {
                aiProvidersRef.current = [];
                providersRestrictedRef.current = true;
                providersReadyRef.current = true;
                setAiProviders([]);
                setSelectedProviderId('');
                setModelOverride(savedSelectionRef.current.model || '');
                setProvidersError('');
                setProvidersRestricted(true);
                setProvidersReady(true);
                return;
            }
            providersReadyRef.current = true;
            setProvidersError(nextError?.message || 'Unable to load External AI Providers.');
            setProvidersReady(true);
        } finally {
            if (!signal.aborted && providersAbortRef.current === controller) {
                setProvidersLoading(false);
            }
        }
    }, []);

    useEffect(() => {
        if (!isOpen) return undefined;
        void loadProviders();
        return () => {
            providersAbortRef.current?.abort();
        };
    }, [isOpen, loadProviders]);

    const selectedProvider = useMemo(
        () => aiProviders.find((provider) => provider.id === selectedProviderId) || aiProviders[0] || null,
        [aiProviders, selectedProviderId],
    );
    const canAnalyze = Boolean(selectedProvider?.id || (providersRestricted && !providersError));
    const selectedProviderType = selectedProvider?.providerType || selectedProvider?.provider_type || '';
    const latestAssistantMessage = useMemo(() => {
        for (let i = messages.length - 1; i >= 0; i -= 1) {
            if (messages[i]?.role === 'assistant') return messages[i];
        }
        return null;
    }, [messages]);

    useEffect(() => {
        if (!isOpen || messages.length === 0) return;
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, [isOpen, messages.length]);

    const runAnalysis = useCallback(async ({
        questionText = '',
        mode = 'append',
        priorMessages = messagesRef.current,
        clearQuestionInput = false,
        signal: externalSignal,
    } = {}) => {
        const targetTaskId = taskId;
        if (!targetTaskId || (!selectedProvider?.id && !providersRestricted)) return false;
        analysisAbortRef.current?.abort();
        const controller = new AbortController();
        if (externalSignal) {
            if (externalSignal.aborted) {
                controller.abort();
            } else {
                externalSignal.addEventListener('abort', () => controller.abort(), { once: true });
            }
        }
        analysisAbortRef.current = controller;
        const requestId = ++analysisRequestIdRef.current;

        const trimmedQuestion = String(questionText || '').trim();
        const providerIdSnapshot = selectedProvider?.id || '';
        const modelToUse = (modelOverrideRef.current || selectedProvider?.model || '').trim();
        const formattedHistory = priorMessages
            .filter((msg) => msg && (msg.role === 'user' || msg.role === 'assistant') && typeof msg.content === 'string')
            .slice(-20)
            .map(({ role, content }) => ({ role, content: content.slice(0, 12000) }));

        const requestBody = {
            provider_id: providerIdSnapshot || null,
            ...(modelToUse ? { model: modelToUse } : {}),
            ...(trimmedQuestion ? { question: trimmedQuestion } : {}),
            ...(formattedHistory.length > 0 ? { messages: formattedHistory } : {}),
        };

        setLoading(true);
        setError('');
        try {
            const payload = await analyzeSimulationTaskWithAI(targetTaskId, requestBody, { signal: controller.signal });
            if (controller.signal.aborted || requestId !== analysisRequestIdRef.current) return false;
            const assistantMessage = {
                role: 'assistant',
                content: String(payload?.analysis || '').trim() || 'No analysis returned by the provider.',
                providerName: payload?.provider_name || payload?.providerName || selectedProvider?.name || 'Default External Provider',
                model: payload?.model || modelToUse || selectedProvider?.model || '',
                generatedAt: payload?.generated_at || payload?.generatedAt || '',
                telemetryScope: payload?.telemetry_scope || payload?.telemetryScope || '',
            };
            let nextMessages;
            if (mode === 'replace-initial') {
                nextMessages = [assistantMessage];
            } else if (mode === 'replace-last-answer') {
                const current = messagesRef.current;
                let lastUserIdx = -1;
                for (let i = current.length - 1; i >= 0; i -= 1) {
                    if (current[i]?.role === 'user') {
                        lastUserIdx = i;
                        break;
                    }
                }
                nextMessages = lastUserIdx >= 0
                    ? [...current.slice(0, lastUserIdx + 1), assistantMessage]
                    : [assistantMessage];
            } else {
                const current = messagesRef.current;
                nextMessages = trimmedQuestion
                    ? [...current, { role: 'user', content: trimmedQuestion }, assistantMessage]
                    : [...current, assistantMessage];
            }
            messagesRef.current = nextMessages;
            setMessages(nextMessages);

            const persistedAt = new Date().toISOString();
            saveAskAiAnalysis(targetTaskId, {
                messages: nextMessages,
                providerId: providerIdSnapshot,
                model: assistantMessage.model || modelToUse,
                updatedAt: persistedAt,
            });
            setSavedUpdatedAt(persistedAt);
            savedSelectionRef.current = {
                providerId: providerIdSnapshot,
                model: assistantMessage.model || modelToUse,
            };

            if (clearQuestionInput) setQuestion('');
            return true;
        } catch (nextError) {
            if (controller.signal.aborted || nextError?.name === 'AbortError' || requestId !== analysisRequestIdRef.current) {
                return false;
            }
            setError(nextError?.message || 'Unable to analyze simulation task with AI.');
            return true;
        } finally {
            if (!controller.signal.aborted && requestId === analysisRequestIdRef.current) {
                setLoading(false);
            }
        }
    }, [providersRestricted, selectedProvider, taskId]);

    useEffect(() => {
        if (!isOpen || !providersReady || providersLoading || providersError) return undefined;
        if (!canAnalyze || !taskId || syncedTaskKey !== String(taskId || '')) return undefined;
        if (messages.length > 0 || error || autoTriggeredTaskRef.current === taskId) return undefined;

        const controller = new AbortController();
        autoTriggeredTaskRef.current = taskId;
        let finished = false;
        void runAnalysis({
            questionText: '',
            mode: 'replace-initial',
            priorMessages: [],
            signal: controller.signal,
        }).then((didFinish) => {
            finished = Boolean(didFinish);
        });

        return () => {
            controller.abort();
            if (!finished && autoTriggeredTaskRef.current === taskId) {
                autoTriggeredTaskRef.current = '';
            }
        };
    }, [canAnalyze, error, isOpen, messages.length, providersError, providersLoading, providersReady, runAnalysis, syncedTaskKey, taskId]);

    const handleProviderChange = useCallback((event) => {
        const nextId = event.target.value;
        setSelectedProviderId(nextId);
        const nextProvider = aiProviders.find((provider) => provider.id === nextId) || null;
        const nextModel = nextProvider?.model || '';
        setModelOverride(nextModel);
        savedSelectionRef.current = { providerId: '', model: '' };
    }, [aiProviders]);

    const handleReanalyze = useCallback(() => {
        if (loading || !canAnalyze) return;
        autoTriggeredTaskRef.current = taskId;
        void runAnalysis({
            questionText: '',
            mode: 'replace-initial',
            priorMessages: [],
        });
    }, [canAnalyze, loading, runAnalysis, taskId]);

    const handleClearAnalysis = useCallback(() => {
        analysisAbortRef.current?.abort();
        analysisRequestIdRef.current += 1;
        autoTriggeredTaskRef.current = taskId || '__cleared__';
        clearSavedAskAiAnalysis(taskId);
        messagesRef.current = [];
        setMessages([]);
        setSavedUpdatedAt('');
        savedSelectionRef.current = { providerId: '', model: '' };
        setError('');
        setQuestion('');
        setLoading(false);
    }, [taskId]);

    const handleSendFollowUp = useCallback(() => {
        const trimmed = question.trim();
        if (!trimmed || loading || !canAnalyze) return;
        autoTriggeredTaskRef.current = taskId;
        void runAnalysis({
            questionText: trimmed,
            mode: 'append',
            priorMessages: messagesRef.current,
            clearQuestionInput: true,
        });
    }, [canAnalyze, loading, question, runAnalysis, taskId]);

    const handleContinueInAssistant = useCallback(() => {
        if (!onNavigate) return;
        try {
            const prompt = buildAssistantHandoffPrompt({
                task,
                taskId,
                analysisContent: latestAssistantMessage?.content || '',
            });
            stageAssistantDraft({
                prompt,
                providerId: selectedProvider?.id || null,
            });
            onClose?.();
            onNavigate('playground');
        } catch (nextError) {
            setError(nextError?.message || 'Unable to stage handoff to Lens Assistant.');
        }
    }, [latestAssistantMessage?.content, onClose, onNavigate, selectedProvider?.id, task, taskId]);

    const latestContent = latestAssistantMessage?.content || '';
    const isLatestCopied = Boolean(latestContent && copiedValue === latestContent);
    const hasMessages = messages.length > 0;
    const displayAnalyzedAt = savedUpdatedAt || latestAssistantMessage?.generatedAt || '';
    const showComposerFooter = Boolean(aiProviders.length > 0 || providersRestricted || hasMessages);

    return (
        <SideDrawer
            isOpen={isOpen}
            onClose={onClose}
            widthClassName="sm:w-[580px] lg:w-[660px] sm:max-w-[92vw]"
            closeLabel="Close Ask AI drawer"
            icon={<Sparkles className="h-4 w-4 text-emerald-500" aria-hidden="true" />}
            title="Ask AI — Bottleneck Diagnosis & Tuning"
            subtitle={`Task ${taskId}${task?.model_name ? ` · ${task.model_name}` : ''}`}
            headerMeta={hasMessages && displayAnalyzedAt ? (
                <div className="flex flex-wrap items-center gap-1.5">
                    <Badge tone="neutral" size="xs">
                        {`Last analyzed · ${formatTimestamp(displayAnalyzedAt)}`}
                    </Badge>
                </div>
            ) : null}
            bodyClassName="flex flex-col overflow-hidden p-0"
            footer={showComposerFooter ? (
                <div className="space-y-2.5">
                    <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[11px] font-medium text-theme-muted">Quick prompts:</span>
                        {ASK_AI_QUICK_PROMPTS.map((chip) => (
                            <Button
                                key={chip}
                                variant="outline"
                                size="xs"
                                disabled={!canAnalyze || loading}
                                onClick={() => {
                                    autoTriggeredTaskRef.current = taskId;
                                    void runAnalysis({
                                        questionText: chip,
                                        mode: 'append',
                                        priorMessages: messagesRef.current,
                                    });
                                }}
                            >
                                {chip}
                            </Button>
                        ))}
                    </div>
                    <div className="flex items-end gap-2">
                        <Textarea
                            aria-label="Follow-up question"
                            rows={2}
                            maxLength={4000}
                            value={question}
                            onChange={(event) => setQuestion(event.target.value)}
                            onKeyDown={(event) => {
                                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                                    event.preventDefault();
                                    handleSendFollowUp();
                                }
                            }}
                            placeholder="Ask a follow-up question about this run..."
                            disabled={loading || !canAnalyze}
                            className="min-h-[56px] flex-1 resize-y font-sans text-xs"
                        />
                        <Button
                            variant="primary"
                            size="sm"
                            onClick={handleSendFollowUp}
                            disabled={!question.trim() || loading || !canAnalyze}
                            isLoading={loading}
                        >
                            <Send className="h-3.5 w-3.5" />
                            Send
                        </Button>
                    </div>
                    <p className="text-[11px] text-theme-muted">
                        Press <kbd className="rounded border border-theme-border px-1 py-0.5 font-mono text-[10px]">Ctrl</kbd>+<kbd className="rounded border border-theme-border px-1 py-0.5 font-mono text-[10px]">Enter</kbd> (or <kbd className="rounded border border-theme-border px-1 py-0.5 font-mono text-[10px]">Cmd</kbd>+<kbd className="rounded border border-theme-border px-1 py-0.5 font-mono text-[10px]">Enter</kbd>) to send. Endpoint secrets and API keys are redacted before sending context.
                    </p>
                </div>
            ) : null}
        >
            {!hasMessages && !providersError && (!providersReady || providersLoading) && aiProviders.length === 0 && !providersRestricted ? (
                <div className="min-h-0 flex-1 overflow-y-auto p-6">
                    <div className="flex items-center justify-center gap-2.5 py-12 text-sm text-theme-muted">
                        <Spinner />
                        <span>Loading External AI Providers…</span>
                    </div>
                </div>
            ) : !hasMessages && providersError ? (
                <div className="min-h-0 flex-1 overflow-y-auto p-6">
                    <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-xs text-red-700 dark:text-red-300">
                        <span className="flex items-center gap-2">
                            <AlertTriangle className="h-4 w-4 shrink-0" />
                            <span>{providersError}</span>
                        </span>
                        <Button variant="ghost" size="xs" onClick={() => void loadProviders()}>
                            Retry
                        </Button>
                    </div>
                </div>
            ) : !hasMessages && providersReady && !providersLoading && !providersError && !providersRestricted && aiProviders.length === 0 ? (
                <div className="min-h-0 flex-1 overflow-y-auto p-6">
                    <EmptyState
                        icon={<Sparkles className="h-6 w-6" />}
                        title="No External AI Provider configured"
                        message="Add an OpenAI-compatible or Anthropic provider in External Providers to analyze simulation bottlenecks and tuning flags."
                        action={onNavigate && canConfigureProviders ? (
                            <Button
                                variant="primary"
                                size="sm"
                                onClick={() => {
                                    onClose?.();
                                    onNavigate('ai-providers');
                                }}
                            >
                                Configure External Providers
                            </Button>
                        ) : null}
                    />
                </div>
            ) : (
                <>
                    <div className="shrink-0 space-y-3 border-b border-theme-border bg-theme-bg/60 px-6 py-4">
                        {providersError ? (
                            <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
                                <span className="flex items-center gap-2">
                                    <AlertTriangle className="h-4 w-4 shrink-0" />
                                    <span>{providersError}</span>
                                </span>
                                <Button variant="ghost" size="xs" onClick={() => void loadProviders()}>
                                    Retry
                                </Button>
                            </div>
                        ) : providersRestricted ? (
                            <div className="space-y-3">
                                <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
                                    Provider listing is restricted for your role, so analysis will use the backend&apos;s default configured External Provider.
                                </p>
                                <div>
                                    <Label
                                        htmlFor="ask-ai-model-input"
                                        help="Optional model override. Leave blank to use the backend default External Provider's configured model."
                                    >
                                        Model Override
                                    </Label>
                                    <Input
                                        id="ask-ai-model-input"
                                        value={modelOverride}
                                        onChange={(event) => setModelOverride(event.target.value)}
                                        placeholder="Optional model override (uses provider default)"
                                        disabled={loading}
                                    />
                                </div>
                            </div>
                        ) : aiProviders.length > 0 ? (
                            <div className="grid gap-3 sm:grid-cols-2">
                                <div>
                                    <Label htmlFor="ask-ai-provider-select">External Provider</Label>
                                    <Select
                                        id="ask-ai-provider-select"
                                        value={selectedProvider?.id || ''}
                                        onChange={handleProviderChange}
                                        disabled={loading}
                                    >
                                        {aiProviders.map((provider) => (
                                            <option key={provider.id} value={provider.id}>
                                                {`${provider.name || provider.id} — ${provider.model || 'default'}`}
                                            </option>
                                        ))}
                                    </Select>
                                </div>
                                <div>
                                    <Label
                                        htmlFor="ask-ai-model-input"
                                        help="Pre-populated with the selected provider's configured model. Override to test another model supported by the same provider."
                                    >
                                        Model Override
                                    </Label>
                                    <Input
                                        id="ask-ai-model-input"
                                        value={modelOverride}
                                        onChange={(event) => setModelOverride(event.target.value)}
                                        placeholder={selectedProvider?.model || 'Provider default model'}
                                        disabled={loading}
                                    />
                                </div>
                            </div>
                        ) : providersLoading ? (
                            <div className="flex items-center gap-2 text-xs text-theme-muted">
                                <Spinner size="sm" />
                                <span>Loading External AI Providers…</span>
                            </div>
                        ) : (
                            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-theme-border bg-theme-card px-3 py-2 text-xs text-theme-muted">
                                <span>No External AI Provider configured.</span>
                                {onNavigate && canConfigureProviders ? (
                                    <Button
                                        variant="outline"
                                        size="xs"
                                        onClick={() => {
                                            onClose?.();
                                            onNavigate('ai-providers');
                                        }}
                                    >
                                        Configure External Providers
                                    </Button>
                                ) : null}
                            </div>
                        )}

                        <div className="flex flex-wrap items-center justify-between gap-2 pt-0.5">
                            <div className="flex flex-wrap items-center gap-1.5">
                                {selectedProviderType && (
                                    <Badge tone="info" size="sm">{selectedProviderType}</Badge>
                                )}
                                {providersRestricted && (
                                    <Badge tone="neutral" size="sm">Default Provider</Badge>
                                )}
                                {latestAssistantMessage?.telemetryScope && (
                                    <Badge tone="brand" size="sm">
                                        {`Scope: ${latestAssistantMessage.telemetryScope}`}
                                    </Badge>
                                )}
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                <Button
                                    variant={hasMessages ? 'secondary' : 'primary'}
                                    size="sm"
                                    onClick={handleReanalyze}
                                    disabled={!canAnalyze || loading}
                                    isLoading={loading}
                                >
                                    {hasMessages
                                        ? <RefreshCw className="h-3.5 w-3.5" />
                                        : <Sparkles className="h-3.5 w-3.5" />}
                                    {hasMessages ? 'Re-analyze' : 'Analyze Bottlenecks & Solutions'}
                                </Button>
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    onClick={() => void copy(latestContent)}
                                    disabled={!latestContent}
                                >
                                    {isLatestCopied
                                        ? <Check className="h-3.5 w-3.5 text-emerald-500" />
                                        : <Copy className="h-3.5 w-3.5" />}
                                    {isLatestCopied ? 'Copied' : 'Copy Analysis'}
                                </Button>
                                {onNavigate && canOpenAssistant && (
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={handleContinueInAssistant}
                                        disabled={loading && !latestAssistantMessage}
                                    >
                                        <Sparkles className="h-3.5 w-3.5" />
                                        Continue in Lens Assistant
                                    </Button>
                                )}
                                {hasMessages && (
                                    <Button
                                        variant="ghost"
                                        size="sm"
                                        onClick={handleClearAnalysis}
                                        disabled={loading}
                                    >
                                        <Trash2 className="h-3.5 w-3.5" />
                                        Clear
                                    </Button>
                                )}
                            </div>
                        </div>
                    </div>

                    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain px-6 py-4">
                        {error && (
                            <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-700 dark:text-red-300">
                                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                                <span>{error}</span>
                            </div>
                        )}

                        {!hasMessages && loading ? (
                            <div className="flex items-center justify-center gap-2.5 py-12 text-xs text-theme-muted">
                                <Spinner size="sm" />
                                <span>Analyzing telemetry, automated forensics, and serving metrics…</span>
                            </div>
                        ) : !hasMessages ? (
                            <p className="py-12 text-center text-xs text-theme-muted">
                                Click <strong>Analyze Bottlenecks &amp; Solutions</strong> or choose a quick prompt below to diagnose this simulation run.
                            </p>
                        ) : (
                            messages.map((msg, index) => (
                                msg.role === 'user' ? (
                                    <div
                                        key={`user-${index}`}
                                        className="ml-auto max-w-[85%] rounded-xl border border-sky-500/30 bg-sky-500/10 px-3.5 py-2.5 text-xs text-theme-text"
                                    >
                                        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-sky-700 dark:text-sky-300">
                                            Question
                                        </div>
                                        <p className="whitespace-pre-wrap leading-relaxed">{msg.content}</p>
                                    </div>
                                ) : (
                                    <article
                                        key={`assistant-${index}`}
                                        className="rounded-xl border border-theme-border bg-theme-bg/40 p-4 text-sm text-theme-text"
                                    >
                                        <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2 border-b border-theme-border pb-2 text-[11px] text-theme-muted">
                                            <div className="flex flex-wrap items-center gap-1.5">
                                                <Sparkles className="h-3.5 w-3.5 text-emerald-500" aria-hidden="true" />
                                                <span className="font-semibold text-theme-text">
                                                    {msg.providerName || 'AI Analysis'}
                                                </span>
                                                {msg.model && <Badge tone="neutral" size="xs">{msg.model}</Badge>}
                                                {msg.telemetryScope && (
                                                    <Badge tone="brand" size="xs">{msg.telemetryScope}</Badge>
                                                )}
                                            </div>
                                            {msg.generatedAt && <span>{formatTimestamp(msg.generatedAt)}</span>}
                                        </div>
                                        <div className="markdown-body text-sm leading-relaxed text-theme-text">
                                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content || ''}</ReactMarkdown>
                                        </div>
                                    </article>
                                )
                            ))
                        )}
                        {hasMessages && loading && (
                            <div className="flex items-center gap-2 rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2 text-xs text-theme-muted">
                                <Spinner size="sm" />
                                <span>Generating response…</span>
                            </div>
                        )}
                        <div ref={messagesEndRef} />
                    </div>
                </>
            )}
        </SideDrawer>
    );
}

export default function SimulationTaskDetails({ onNavigate }) {
    const params = new URLSearchParams(window.location.search);
    const routeTaskId = params.get('taskId') || '';

    const [backends, setBackends] = useState([]);
    const [task, setTask] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [actionError, setActionError] = useState('');
    const [liveTelemetry, setLiveTelemetry] = useState(null);
    const [liveError, setLiveError] = useState('');
    const [telemetry, setTelemetry] = useState(null);
    const [telemetryLoading, setTelemetryLoading] = useState(false);
    const [telemetryNotice, setTelemetryNotice] = useState('');
    const [postTab, setPostTab] = useState('overview');
    const [pendingScrollAnchor, setPendingScrollAnchor] = useState(null);
    const [systemInitialNode, setSystemInitialNode] = useState(undefined);
    const [analysisWindow, setAnalysisWindow] = useState('profiling');
    const [durationClockMs, setDurationClockMs] = useState(Date.now);
    const [stopping, setStopping] = useState(false);
    const [stoppingTaskId, setStoppingTaskId] = useState('');
    const [rerunningTaskId, setRerunningTaskId] = useState('');
    const [deletingTaskId, setDeletingTaskId] = useState('');
    const [downloadingArtifact, setDownloadingArtifact] = useState('');
    const [artifactDownloadError, setArtifactDownloadError] = useState('');
    const [downloadsMenuOpen, setDownloadsMenuOpen] = useState(false);
    const [errorInspectorFilter, setErrorInspectorFilter] = useState(null);
    const [askAiOpen, setAskAiOpen] = useState(false);

    const downloadsMenuRef = useRef(null);
    const artifactsHubRef = useRef(null);
    const taskRef = useRef(task);
    useEffect(() => {
        taskRef.current = task;
    }, [task]);

    const handleSelectTab = useCallback((tab, anchor = null, options = null) => {
        setPostTab(tab);
        setPendingScrollAnchor(anchor || null);
        setSystemInitialNode(tab === 'system' ? (options?.node || undefined) : undefined);
    }, []);

    useEffect(() => {
        if (!pendingScrollAnchor) return undefined;
        if (typeof document === 'undefined' || typeof window === 'undefined') {
            setPendingScrollAnchor(null);
            return undefined;
        }
        const rafId = window.requestAnimationFrame(() => {
            document.getElementById(pendingScrollAnchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            setPendingScrollAnchor(null);
        });
        return () => window.cancelAnimationFrame?.(rafId);
    }, [postTab, pendingScrollAnchor]);

    const taskId = task?.id || task?.task_id || routeTaskId;
    const taskStatus = String(task?.status || '').toLowerCase();
    const isRunning = RUNNING_STATUSES.has(taskStatus);
    const hasTask = Boolean(task);
    const taskStartedAt = task?.started_at;

    const loadBackends = useCallback(async () => {
        try {
            const payload = await requestJson('/api/v1/simulation/backends');
            setBackends(arrayFrom(payload, ['backends', 'items', 'data']));
        } catch {
            setBackends([]);
        }
    }, []);

    const loadTaskDetails = useCallback(async ({ quiet = false } = {}) => {
        if (!routeTaskId) {
            setError('A simulation task ID is required.');
            setLoading(false);
            return;
        }
        if (!quiet) {
            setLoading(true);
            setActionError('');
        }
        try {
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(routeTaskId)}`);
            setTask(unwrapTask(payload));
            setError('');
        } catch (nextError) {
            setError(`Unable to load task details: ${nextError.message}`);
            if (!quiet) setTask(null);
        } finally {
            if (!quiet) setLoading(false);
        }
    }, [routeTaskId]);

    const loadLiveTelemetry = useCallback(async () => {
        if (!routeTaskId) return;
        try {
            const snapshot = await getLiveTelemetry(routeTaskId);
            setLiveTelemetry(snapshot);
            setLiveError('');
        } catch (nextError) {
            if (nextError?.status === 409 || String(nextError?.message || '').includes('409')) {
                void loadTaskDetails({ quiet: true });
                return;
            }
            setLiveError(nextError.message);
        }
    }, [loadTaskDetails, routeTaskId]);

    useEffect(() => {
        setTask(null);
        setLiveTelemetry(null);
        setLiveError('');
        setTelemetry(null);
        setTelemetryNotice('');
        setPostTab('overview');
        setSystemInitialNode(undefined);
        setAnalysisWindow('profiling');
        setActionError('');
        setArtifactDownloadError('');
        setErrorInspectorFilter(null);
        setAskAiOpen(false);
        void loadTaskDetails();
    }, [loadTaskDetails, routeTaskId]);

    useEffect(() => {
        void loadBackends();
    }, [loadBackends]);

    useEffect(() => {
        if (!taskStartedAt || !isRunning) return undefined;
        setDurationClockMs(Date.now());
        const interval = window.setInterval(() => setDurationClockMs(Date.now()), 1000);
        return () => window.clearInterval(interval);
    }, [isRunning, taskStartedAt]);

    usePolling(
        () => loadLiveTelemetry(),
        { intervalMs: 2000, enabled: Boolean(routeTaskId && isRunning), immediate: true },
    );

    usePolling(
        () => loadTaskDetails({ quiet: true }),
        { intervalMs: 3000, enabled: Boolean(routeTaskId && isRunning) },
    );

    useEffect(() => {
        if (!taskId || isRunning || !hasTask) return undefined;
        const controller = new AbortController();
        setTelemetryLoading(true);
        setTelemetryNotice('');
        getTaskTelemetry(taskId, { section: 'all' }, { signal: controller.signal })
            .then((payload) => {
                if (!controller.signal.aborted) {
                    setTelemetry(payload);
                    setTelemetryNotice('');
                }
            })
            .catch((nextError) => {
                if (!controller.signal.aborted) {
                    setTelemetry(deriveLegacyTelemetryFromTask(taskRef.current));
                    setTelemetryNotice(`Loaded fallback client summary (${nextError.message}).`);
                }
            })
            .finally(() => {
                if (!controller.signal.aborted) setTelemetryLoading(false);
            });
        return () => controller.abort();
    }, [hasTask, isRunning, taskId, taskStatus]);

    useEffect(() => {
        if (!downloadsMenuOpen) return undefined;
        const onPointerDown = (event) => {
            if (!downloadsMenuRef.current?.contains(event.target)) setDownloadsMenuOpen(false);
        };
        const onKeyDown = (event) => {
            if (event.key === 'Escape') setDownloadsMenuOpen(false);
        };
        document.addEventListener('pointerdown', onPointerDown);
        document.addEventListener('keydown', onKeyDown);
        return () => {
            document.removeEventListener('pointerdown', onPointerDown);
            document.removeEventListener('keydown', onKeyDown);
        };
    }, [downloadsMenuOpen]);

    const downloadArtifact = useCallback(async (artifact) => {
        if (!taskId || !artifact?.kind) return;
        setDownloadingArtifact(artifact.kind);
        setArtifactDownloadError('');
        try {
            await downloadFile(
                `/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifact.kind)}`,
                { fallbackName: artifact.name || `${taskBackend(task)}-${artifact.kind}-${taskId}` },
            );
        } catch (nextError) {
            setArtifactDownloadError(nextError.message);
        } finally {
            setDownloadingArtifact('');
        }
    }, [task, taskId]);

    const stopTask = useCallback(async () => {
        if (!taskId || !task) return;
        setStopping(true);
        setStoppingTaskId(taskId);
        setActionError('');
        try {
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/stop`, {
                method: 'POST',
            });
            setTask((current) => (current ? {
                ...current,
                status: payload?.status || 'cancelling',
                progress_message: 'Cancellation requested',
            } : current));
        } catch (nextError) {
            setActionError(`Unable to stop simulation: ${nextError.message}`);
        } finally {
            setStopping(false);
            setStoppingTaskId('');
        }
    }, [task, taskId]);

    const resolveLegacyRerunBody = useCallback(async () => {
        if (!task || String(task?.endpoint_mode || '').toLowerCase() !== 'deployment') return null;
        if (task?.endpoint_deployment_execution_id || task?.endpoint_deployment_run_id || task?.endpoint_deployment_case_id) {
            return null;
        }
        const payload = await requestJson('/api/cluster/clusters');
        const clusters = (payload?.items || []).filter((cluster) => cluster.ready);
        if (!clusters.length) return null;
        const cluster = clusters.find((candidate) => candidate.id === task.endpoint_cluster_id) || clusters[0];
        const deployments = (await getDeploymentExecutions({ status: 'ready', clusterId: cluster.id })).filter(
            (deployment) => deployment.execution_id && deployment.endpoint,
        );
        if (!deployments.length) return null;
        const deployment = deployments.find((candidate) => (
            candidate.execution_id === task.endpoint_deployment_execution_id
            || candidate.name === task.endpoint_deployment_name
            || candidate.endpoint === task.endpoint_url
            || candidate.model === task.model_name
        )) || deployments[0];
        return {
            endpoint_deployment_execution_id: deployment.execution_id,
            endpoint_cluster_id: cluster.id,
            endpoint_cluster_name: cluster?.name || null,
            endpoint_deployment_name: deployment?.name || null,
        };
    }, [task]);

    const normalizedTaskBackend = String(taskBackend(task) || '').trim().toLowerCase();
    const isLegacyTraceReplayer = normalizedTaskBackend === 'trace-replayer' || normalizedTaskBackend === 'trace_replayer';

    const rerunSimulationTask = useCallback(async () => {
        if (!task || !taskId || isRunning || isLegacyTraceReplayer) return;
        setRerunningTaskId(taskId);
        setActionError('');
        try {
            const rerunBody = await resolveLegacyRerunBody();
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/rerun`, {
                method: 'POST',
                headers: rerunBody ? { 'Content-Type': 'application/json' } : undefined,
                body: rerunBody ? JSON.stringify(rerunBody) : undefined,
            });
            const rerun = unwrapTask(payload);
            const rerunId = rerun?.id || payload?.task_id;
            if (!rerunId) throw new Error('The rerun response did not include a task ID.');
            onNavigate('optimization-simulate-details', { taskId: rerunId });
        } catch (nextError) {
            setActionError(`Unable to rerun simulation: ${nextError.message}`);
        } finally {
            setRerunningTaskId('');
        }
    }, [isLegacyTraceReplayer, isRunning, onNavigate, resolveLegacyRerunBody, task, taskId]);

    const deleteSimulationTask = useCallback(async () => {
        if (!task || !taskId) return;
        if (isRunning) {
            setActionError('Stop this task before deleting it.');
            return;
        }
        const confirmed = await confirmDelete(`Delete simulation task ${task.name || taskId}? This cannot be undone.`);
        if (!confirmed) return;
        setDeletingTaskId(taskId);
        setActionError('');
        try {
            await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
            if (typeof window !== 'undefined') {
                const nextUrl = new URL(window.location.href);
                const rawCompare = nextUrl.searchParams.get('compare');
                if (rawCompare) {
                    const remaining = rawCompare
                        .split(',')
                        .map((item) => item.trim())
                        .filter((item) => item && item !== taskId);
                    if (remaining.length > 0) nextUrl.searchParams.set('compare', remaining.join(','));
                    else nextUrl.searchParams.delete('compare');
                    window.history.replaceState({}, '', `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
                }
            }
            onNavigate('optimization-simulate');
        } catch (nextError) {
            setActionError(`Unable to delete simulation task: ${nextError.message}`);
        } finally {
            setDeletingTaskId('');
        }
    }, [isRunning, onNavigate, task, taskId]);

    const navigateToCompare = useCallback(() => {
        if (!taskId) return;
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.set('compare', taskId);
        window.history.replaceState({}, '', nextUrl.toString());
        onNavigate?.('optimization-simulate');
    }, [onNavigate, taskId]);

    const resultPayload = task?.result || task?.results || task?.output || null;
    const result = Array.isArray(resultPayload) ? resultPayload[0] || null : resultPayload;
    const artifacts = result?.artifacts || task?.artifacts || [];
    const backendVersion = result?.backend_version || task?.backend_version
        || backends.find((item) => (item.name || item.id || item.value) === taskBackend(task))?.version;
    const statusForChip = taskStatusChip(taskStatus);
    const effectiveTelemetry = useMemo(
        () => telemetry || (task ? deriveLegacyTelemetryFromTask(task) : null),
        [task, telemetry],
    );
    const taskHardware = useMemo(
        () => resolveTaskHardware(task, effectiveTelemetry),
        [effectiveTelemetry, task],
    );
    const taskPacing = useMemo(
        () => formatTaskPacing(task, effectiveTelemetry),
        [effectiveTelemetry, task],
    );
    const profilingFallbackToFull = Boolean(
        effectiveTelemetry?.client_series?.windows?.profiling?.fallback_to_full
        || (effectiveTelemetry?.client_series && Number(effectiveTelemetry.client_series.warmup_seconds || 0) <= 0),
    );
    const resultWarnings = Array.isArray(result?.warnings) ? result.warnings : [];

    return (
        <section className="simulation-details-page flex w-full min-w-0 max-w-none flex-col gap-4 px-4 py-6 text-theme-text sm:px-6 lg:px-8 lg:py-10">
            <PageHeading title={task?.name || routeTaskId || 'Simulation task details'} subtitle={task?.description || 'Simulation'} />
            <header className="grid gap-4 border-b border-theme-border pb-5 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
                <div className="flex min-w-0 flex-1 items-start gap-3">
                    <Button
                        variant="outline"
                        size="icon"
                        title="Back to simulations"
                        aria-label="Back to simulations"
                        onClick={() => onNavigate('optimization-simulate')}
                        className="simulation-back-button"
                    >
                        <ArrowLeft className="h-4 w-4" />
                    </Button>
                    <div className="min-w-0">
                        {task && (
                            <>
                                <div className="flex flex-wrap items-center gap-2 text-xs">
                                    <span className="inline-flex items-center gap-1">
                                        <StatusChip status={statusForChip} label={displayName(taskStatus)} pulse={taskStatus === 'running'} />
                                        <HelpTooltip label="Task status help" align="start" iconSize={12}>
                                            Current lifecycle state of the simulation job (Queued, Running, Completed, Failed, or Cancelled).
                                        </HelpTooltip>
                                    </span>
                                    {(task.artifacts_incomplete || effectiveTelemetry?.partial) && (
                                        <span className="inline-flex items-center gap-1">
                                            <Badge tone="warning" size="xs">Partial Results</Badge>
                                            <HelpTooltip label="Partial results help" align="start" iconSize={12}>
                                                Run ended early or some telemetry streams were incomplete; available client/server data is still shown.
                                            </HelpTooltip>
                                        </span>
                                    )}
                                    {effectiveTelemetry?.legacy && (
                                        <span className="inline-flex items-center gap-1">
                                            <Badge tone="neutral" size="xs">Legacy run (client-only)</Badge>
                                            <HelpTooltip label="Legacy run help" align="start" iconSize={12}>
                                                Recorded before normalized server telemetry persistence; metrics are synthesized from client-side summary data only.
                                            </HelpTooltip>
                                        </span>
                                    )}
                                </div>
                                <dl className="mt-2.5 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-theme-muted">
                                    <div className="flex min-w-0 items-center gap-1.5">
                                        <dt className="inline-flex items-center gap-1">
                                            <span>Task ID</span>
                                            <HelpTooltip label="Task ID help" align="start" iconSize={11}>
                                                Unique simulation task identifier used in API paths, artifact filenames, and URLs.
                                            </HelpTooltip>
                                        </dt>
                                        <dd className="break-all font-mono text-theme-text">{taskId}</dd>
                                    </div>
                                    <div className="flex items-center gap-1.5">
                                        <dt className="inline-flex items-center gap-1">
                                            <span>Created</span>
                                            <HelpTooltip label="Created timestamp help" align="start" iconSize={11}>
                                                Local timestamp when this simulation task was submitted.
                                            </HelpTooltip>
                                        </dt>
                                        <dd className="text-theme-text">{formatTimestamp(task.created_at)}</dd>
                                    </div>
                                    <div className="flex items-center gap-1.5">
                                        <dt className="inline-flex items-center gap-1">
                                            <span>Model</span>
                                            <HelpTooltip label="Model help" align="start" iconSize={11}>
                                                Target model identifier evaluated during this simulation run.
                                            </HelpTooltip>
                                        </dt>
                                        <dd className="text-theme-text">{task.model_name || '—'}</dd>
                                    </div>
                                    <div className="flex items-center gap-1.5">
                                        <dt className="inline-flex items-center gap-1">
                                            <span>Backend</span>
                                            <HelpTooltip label="Backend help" align="start" iconSize={11}>
                                                Load generator backend and version that executed the replay.
                                            </HelpTooltip>
                                        </dt>
                                        <dd className="text-theme-text">{displayName(taskBackend(task))}{backendVersion ? ` ${backendVersion}` : ''}</dd>
                                    </div>
                                    <div className="flex items-center gap-1.5">
                                        <dt className="inline-flex items-center gap-1">
                                            <span>Dataset</span>
                                            <HelpTooltip label="Dataset help" align="start" iconSize={11}>
                                                Trace dataset or corpus replayed against the inference endpoint.
                                            </HelpTooltip>
                                        </dt>
                                        <dd className="text-theme-text">{taskDataset(task)}</dd>
                                    </div>
                                    <div className="flex items-center gap-1.5">
                                        <dt className="inline-flex items-center gap-1">
                                            <span>Hardware</span>
                                            <HelpTooltip label="Hardware help" align="start" iconSize={11}>
                                                Accelerator model or hardware profile detected on the target serving nodes or cluster during the run (or External / Not reported when unavailable).
                                            </HelpTooltip>
                                        </dt>
                                        <dd className="text-theme-text">{taskHardware}</dd>
                                    </div>
                                    {taskPacing.label && (
                                        <div className="flex items-center gap-1.5">
                                            <dt className="inline-flex items-center gap-1">
                                                <span>Pacing</span>
                                                <HelpTooltip label="Pacing help" align="start" iconSize={11} formula="Fixed Schedule: t_target = t_trace / Scale_Factor | Concurrency: Active_Slots ≤ N">
                                                    Replay pacing mode: Fixed Schedule follows trace timestamps, while Concurrency (N) maintains N simultaneous in-flight session/request slots.
                                                </HelpTooltip>
                                            </dt>
                                            <dd className="text-theme-text">{taskPacing.label}</dd>
                                        </div>
                                    )}
                                </dl>
                            </>
                        )}
                    </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <RefreshButton
                        onClick={() => void loadTaskDetails()}
                        refreshing={loading}
                        label="Refresh simulation task details"
                    />
                    {task && isRunning && (
                        <Button
                            variant="dangerOutline"
                            onClick={stopTask}
                            disabled={!STOPPABLE_STATUSES.has(taskStatus)}
                            isLoading={stopping && stoppingTaskId === taskId}
                        >
                            <Square className="h-3.5 w-3.5" /> Stop
                        </Button>
                    )}
                    {task && !isRunning && (
                        <>
                            <Button
                                variant="primary"
                                className="simulation-ask-ai-button"
                                onClick={() => setAskAiOpen(true)}
                            >
                                <Sparkles className="h-4 w-4" /> Ask AI
                            </Button>
                            <Button
                                variant="secondary"
                                className="simulation-rerun-button"
                                onClick={rerunSimulationTask}
                                disabled={isLegacyTraceReplayer}
                                isLoading={rerunningTaskId === taskId}
                                title={isLegacyTraceReplayer ? 'Legacy trace-replayer backend is no longer supported' : 'Rerun simulation task'}
                            >
                                <RefreshCw className="h-3.5 w-3.5" /> Rerun
                            </Button>
                        </>
                    )}
                    {task && (
                        <Button
                            variant="dangerOutline"
                            onClick={deleteSimulationTask}
                            disabled={isRunning}
                            isLoading={deletingTaskId === taskId}
                            title={isRunning ? 'Stop this task before deleting it' : 'Delete task and all backend data'}
                        >
                            <Trash2 className="h-3.5 w-3.5" /> Delete
                        </Button>
                    )}
                </div>
            </header>

            {error && (
                <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-200">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>{error}</span>
                </div>
            )}
            {actionError && (
                <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-200">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>{actionError}</span>
                </div>
            )}
            {artifactDownloadError && (
                <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-200">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>{artifactDownloadError}</span>
                </div>
            )}

            {loading && !task ? (
                <div className="flex items-center gap-2 py-12 text-sm text-theme-muted">
                    <Spinner /> Loading simulation task details…
                </div>
            ) : !task ? (
                <EmptyState
                    icon={<Activity className="h-8 w-8" />}
                    title="Task not found"
                    message="Return to the simulation list and choose another task."
                />
            ) : isRunning ? (
                <LiveMonitorPanel
                    task={task}
                    liveTelemetry={liveTelemetry}
                    liveError={liveError}
                    onStop={stopTask}
                    stopping={stopping && stoppingTaskId === taskId}
                    durationClockMs={durationClockMs}
                    onOpenErrorInspector={(filter = 'all') => setErrorInspectorFilter(filter)}
                />
            ) : (
                <div className="space-y-5">
                    <Panel padding="sm">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                            <div className="flex flex-wrap items-center gap-3">
                                <span className="inline-flex items-center gap-1.5">
                                    <ToggleGroup
                                        value={analysisWindow}
                                        onChange={setAnalysisWindow}
                                        options={[
                                            { value: 'profiling', label: 'Profiling Steady-State' },
                                            { value: 'full', label: 'Full Run' },
                                        ]}
                                    />
                                    <HelpTooltip label="Analysis window help" align="start" iconSize={12} formula="Profiling = [Warmup_s, End]; Full = [0, End]">
                                        Switches post-run KPIs between Profiling Steady-State (excluding the configured warmup window) and Full Run (all requests from t=0).
                                    </HelpTooltip>
                                </span>
                                {profilingFallbackToFull && analysisWindow === 'profiling' && (
                                    <span className="inline-flex items-center gap-1">
                                        <Badge tone="neutral" size="xs">Full Run window active (warmup 0s)</Badge>
                                        <HelpTooltip label="Full run fallback help" align="start" iconSize={12} formula="Active_Window = [0, End]  (when Warmup_s = 0 or slice empty)">
                                            Warmup was 0 seconds or no steady-state slice differed, so Profiling Steady-State displays the full-run window.
                                        </HelpTooltip>
                                    </span>
                                )}
                                <span className="inline-flex items-center gap-1 text-xs text-theme-muted">
                                    <span>Duration: <strong className="font-mono text-theme-text">{formatDuration(task, result?.summary, durationClockMs)}</strong></span>
                                    <HelpTooltip label="Run duration help" align="start" iconSize={12} formula="Run_Duration_s = t_complete − t_execution_start">
                                        Total wall-clock elapsed execution time from benchmark start to completion, including warmup and grace drain.
                                    </HelpTooltip>
                                </span>
                                {telemetryLoading && <Spinner size="sm" />}
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                <Button
                                    variant="primary"
                                    size="sm"
                                    onClick={() => void downloadArtifact({
                                        kind: 'report_html',
                                        name: `${taskBackend(task)}-report-${taskId}.html`,
                                    })}
                                    isLoading={downloadingArtifact === 'report_html'}
                                >
                                    <Download className="h-3.5 w-3.5" />
                                    Download HTML Report
                                </Button>

                                <div ref={downloadsMenuRef} className="relative">
                                    <Button
                                        variant="secondary"
                                        size="sm"
                                        aria-haspopup="menu"
                                        aria-expanded={downloadsMenuOpen}
                                        onClick={() => setDownloadsMenuOpen((open) => !open)}
                                    >
                                        Exports
                                        <ChevronDown className="h-3.5 w-3.5" />
                                    </Button>
                                    {downloadsMenuOpen && (
                                        <div
                                            role="menu"
                                            className="absolute right-0 z-30 mt-1 w-64 rounded-xl border border-theme-border bg-theme-card p-1.5 shadow-xl"
                                        >
                                            {[
                                                ['report_bundle', 'Download Full Bundle (.tar.gz)', `${taskBackend(task)}-bundle-${taskId}.tar.gz`],
                                                ['summary_csv', 'Summary CSV', `${taskBackend(task)}-summary-${taskId}.csv`],
                                                ['forensics', 'Forensics JSON', `forensics-${taskId}.json`],
                                            ].map(([kind, label, name]) => (
                                                <button
                                                    key={kind}
                                                    type="button"
                                                    role="menuitem"
                                                    onClick={() => {
                                                        setDownloadsMenuOpen(false);
                                                        void downloadArtifact({ kind, name });
                                                    }}
                                                    className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs text-theme-text hover:bg-theme-bg"
                                                >
                                                    <FileText className="h-3.5 w-3.5 text-theme-muted" />
                                                    <span>{label}</span>
                                                </button>
                                            ))}
                                            <button
                                                type="button"
                                                role="menuitem"
                                                onClick={() => {
                                                    setDownloadsMenuOpen(false);
                                                    handleSelectTab('logs');
                                                    window.requestAnimationFrame(() => {
                                                        artifactsHubRef.current?.scrollIntoView({ behavior: 'smooth' });
                                                    });
                                                }}
                                                className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs text-theme-text hover:bg-theme-bg"
                                            >
                                                <FileJson className="h-3.5 w-3.5 text-theme-muted" />
                                                <span>Individual Artifacts…</span>
                                            </button>
                                        </div>
                                    )}
                                </div>

                                <Button variant="secondary" size="sm" onClick={navigateToCompare}>
                                    <GitCompareArrows className="h-3.5 w-3.5" />
                                    Compare Runs
                                </Button>
                            </div>
                        </div>
                    </Panel>

                    {telemetryNotice && (
                        <div role="status" className="flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-xs text-amber-800 dark:text-amber-200">
                            <AlertTriangle className="h-4 w-4 shrink-0" />
                            <span>{telemetryNotice}</span>
                        </div>
                    )}

                    {resultWarnings.length > 0 && (
                        <section className="space-y-1.5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4" aria-label="Result warnings">
                            {resultWarnings.map((warning) => (
                                <p key={warning} className="flex items-start gap-2 text-xs text-amber-800 dark:text-amber-100">
                                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                                    <span>{warning}</span>
                                </p>
                            ))}
                        </section>
                    )}

                    {(taskStatus === 'failed' || task.error_message) && (
                        <section className="rounded-xl border border-red-500/40 bg-red-500/10 p-4" aria-labelledby="simulation-failure-heading">
                            <div className="flex items-start gap-3">
                                <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-red-600 dark:text-red-400" />
                                <div className="min-w-0 flex-1">
                                    <h3 id="simulation-failure-heading" className="text-sm font-bold text-red-700 dark:text-red-200">
                                        {taskStatus === 'failed' ? 'Task Failed' : 'Execution Error Reported'}
                                    </h3>
                                    <p className="mt-1 font-mono text-xs text-red-700 dark:text-red-200/90">
                                        {task.error_message || task.progress_message || 'The simulation encountered an error during execution.'}
                                    </p>
                                </div>
                            </div>
                        </section>
                    )}

                    <nav className="flex overflow-x-auto border-b border-theme-border" role="tablist" aria-label="Post-run analysis sections">
                        {POST_RUN_TABS.map((tab) => {
                            const active = postTab === tab.key;
                            return (
                                <button
                                    key={tab.key}
                                    type="button"
                                    role="tab"
                                    aria-selected={active}
                                    onClick={() => handleSelectTab(tab.key)}
                                    className={cn(
                                        'relative shrink-0 px-5 py-3 text-xs font-semibold transition-colors',
                                        active
                                            ? 'simulation-detail-tab-active text-emerald-600 dark:text-emerald-300'
                                            : 'text-theme-muted hover:text-theme-text',
                                    )}
                                >
                                    {tab.label}
                                    {active && (
                                        <span className="simulation-detail-tab-indicator absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-emerald-500" />
                                    )}
                                </button>
                            );
                        })}
                    </nav>

                    {postTab === 'overview' && (
                        <OverviewTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            analysisWindow={analysisWindow}
                            onSelectTab={handleSelectTab}
                            onOpenErrorInspector={(filter = 'all') => setErrorInspectorFilter(filter)}
                        />
                    )}
                    {postTab === 'workload' && (
                        <WorkloadTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            analysisWindow={analysisWindow}
                        />
                    )}
                    {postTab === 'bottlenecks' && (
                        <BottlenecksTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            analysisWindow={analysisWindow}
                            onSelectTab={handleSelectTab}
                        />
                    )}
                    {postTab === 'serving' && (
                        <ServingTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            analysisWindow={analysisWindow}
                            scrollAnchor={pendingScrollAnchor}
                        />
                    )}
                    {postTab === 'requests' && (
                        <RequestsTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            analysisWindow={analysisWindow}
                            onOpenErrorInspector={(filter = 'all') => setErrorInspectorFilter(filter)}
                        />
                    )}
                    {postTab === 'system' && (
                        <SystemTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            analysisWindow={analysisWindow}
                            initialNode={systemInitialNode}
                        />
                    )}
                    {postTab === 'hardware' && (
                        <HardwareTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            onSelectTab={handleSelectTab}
                        />
                    )}
                    {postTab === 'logs' && (
                        <LogsAndConfigTab
                            task={task}
                            telemetry={effectiveTelemetry}
                            artifacts={artifacts}
                            downloadingArtifact={downloadingArtifact}
                            artifactDownloadError={artifactDownloadError}
                            onDownloadArtifact={downloadArtifact}
                            artifactsHubRef={artifactsHubRef}
                        />
                    )}
                </div>
            )}

            <ErrorInspectorModal
                isOpen={errorInspectorFilter !== null}
                onClose={() => setErrorInspectorFilter(null)}
                taskId={taskId}
                initialFilter={errorInspectorFilter || 'all'}
                isRunning={isRunning}
                taskStartedAt={task?.execution_started_at || task?.started_at}
            />

            <AskAiModal
                isOpen={askAiOpen}
                onClose={() => setAskAiOpen(false)}
                task={task}
                taskId={taskId}
                onNavigate={onNavigate}
            />
        </section>
    );
}
