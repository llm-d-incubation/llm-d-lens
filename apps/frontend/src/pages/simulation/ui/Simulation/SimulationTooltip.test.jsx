import React, { act } from 'react';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { AuthContext } from '../../../auth/AuthContext.js';
import { Badge, ChartContainer, HelpTooltip, Panel, StatCard } from '../../../../shared/ui';
import SimulationDashboard, { createSimulationFormState } from '../SimulationDashboard';
import { SimulationTooltip } from './SimulationTooltip';
import { LatencyMetricTimeline } from './LatencyMetricTimeline';
import { LatencyHeatmap } from './LatencyHeatmap';
import { OverviewTab } from './OverviewTab';
import { BottlenecksTab } from './BottlenecksTab';
import { HardwareTab } from './HardwareTab';
import { RequestsTab } from './RequestsTab';
import { CompareTooltip, RunCompareDrawer } from './RunCompareDrawer';
import { SystemTab } from './SystemTab';
import { WorkloadTab } from './WorkloadTab';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENGINE_LEAK_RE = /vllm:|sglang:|trtllm:|--kv-cache-metrics-sample|\bvllm\b/i;

function extractTooltipBodies(html) {
    const bodies = [];
    const re = /role="tooltip"[^>]*>([\s\S]*?)<\/span>\s*<\/span>/g;
    let match;
    while ((match = re.exec(html)) !== null) {
        bodies.push(match[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim());
    }
    return bodies;
}

function collectElements(node, predicate, out = []) {
    if (!node || typeof node !== 'object') return out;
    if (Array.isArray(node)) {
        node.forEach((child) => collectElements(child, predicate, out));
        return out;
    }
    if (predicate(node)) out.push(node);
    if (node.props?.children) collectElements(node.props.children, predicate, out);
    if (node.props?.actions) collectElements(node.props.actions, predicate, out);
    return out;
}

function makeSliceTelemetry() {
    return {
        telemetry_scope: 'full',
        forensics: {
            telemetry_scope: 'full',
            verdict: {
                dominant_limit: 'healthy',
                severity: 'healthy',
                attainment_pct: 96.5,
                throughput_rps: 14.2,
                goodput_rps: 13.7,
                reason: 'Serving within configured latency SLOs.',
                evidence: [{ text: 'TTFT P50 is 120 ms', tab: 'requests' }],
            },
            findings: [
                {
                    id: 'f1',
                    severity: 'info',
                    category: 'capacity',
                    title: 'Headroom available',
                    description: 'Queue depth stayed low.',
                    evidence: [{ metric: 'queue_ms', value: '8 ms' }],
                    tab: 'bottlenecks',
                },
            ],
            timeline_buckets: [
                {
                    bucket_index: 0,
                    start_seconds: 0,
                    end_seconds: 10,
                    role: 'worker',
                    state: 'healthy',
                    running: 4,
                    waiting: 0,
                    kv_cache_pct: 32,
                    prefix_hit_pct: 64,
                    preemptions: 0,
                },
            ],
            ttft_decomposition: {
                p50: { prefill_ms: 85, queue_ms: 12, overhead_ms: 23 },
                p90: { prefill_ms: 140, queue_ms: 35, overhead_ms: 35 },
            },
            stage_breakdown: {
                source: 'server_telemetry',
                slices: {
                    p50: {
                        available: true,
                        epp_scheduling_ms: 4,
                        queue_time_ms: 12,
                        prefill_time_ms: 85,
                        decode_time_ms: 320,
                        gateway_overhead_ms: 19,
                        share_of_e2e_pct: {
                            epp_scheduling: 0.9,
                            queue_time: 2.7,
                            prefill_time: 19.3,
                            decode_time: 72.7,
                            gateway_overhead: 4.3,
                        },
                        share_of_ttft_pct: {
                            epp_scheduling: 3.3,
                            queue_time: 10.0,
                            prefill_time: 70.8,
                            gateway_overhead: 15.8,
                        },
                    },
                },
                series: [{ t: 5, epp_sched_ms: 4, queue_ms: 12, prefill_ms: 85, decode_ms: 320 }],
            },
            cache_behavior: {
                cached_tps: 3200,
                fresh_tps: 1800,
                follow_up_turns: {
                    followup_count: 120,
                    followup_regression_pct: 6.5,
                    first_turn_ttft_p50_ms: 180,
                    followup_ttft_p50_ms: 95,
                },
            },
            routing_vs_cache: {
                epp_expected_hit_pct: 68.2,
                vllm_actual_hit_pct: 64.0,
                gap_pct: 4.2,
                series: [{ t: 5, epp_hit_pct: 68.2, vllm_hit_pct: 64.0 }],
            },
            pod_skew: {
                skew_ratio: 1.25,
                pods: [
                    { pod: 'worker-0', role: 'worker', request_rate_rps: 7.5, waiting_mean: 0.2, kv_cache_peak_pct: 40, prefix_cache_hit_pct: 65 },
                    { pod: 'worker-1', role: 'worker', request_rate_rps: 6.0, waiting_mean: 0.1, kv_cache_peak_pct: 36, prefix_cache_hit_pct: 62 },
                ],
            },
        },
        client_series: {
            warmup_seconds: 10,
            dispatch_duration_seconds: 60,
            duration_seconds: 65,
            windows: {
                profiling: {
                    request_count: 800,
                    successful_requests: 796,
                    failed_requests: 4,
                    good_requests: 772,
                    throughput_rps: 14.2,
                    output_tok_per_s: 420,
                    ttft_ms: { avg: 130, min: 40, p50: 120, p90: 210, p95: 250, p99: 310, max: 420 },
                    tpot_ms: { avg: 22, min: 10, p50: 20, p90: 32, p95: 38, p99: 45, max: 60 },
                    e2e_ms: { avg: 900, min: 300, p50: 850, p90: 1300, p95: 1500, p99: 1800, max: 2200 },
                    drift_ms: { avg: 2, min: 0, p50: 1.5, p90: 5, p95: 8, p99: 12, max: 20 },
                    input_tokens: { avg: 512, min: 64, p50: 480, p90: 900, p95: 1100, p99: 1500, max: 2048 },
                    output_tokens: { avg: 64, min: 8, p50: 56, p90: 110, p95: 128, p99: 192, max: 256 },
                    tpot_measured_ratio: 1,
                },
                full: {
                    request_count: 900,
                    successful_requests: 895,
                    failed_requests: 5,
                    throughput_rps: 13.8,
                    ttft_ms: { p50: 125, p90: 220 },
                },
            },
            buckets: [
                {
                    t: 5,
                    target_rps: 14,
                    actual_rps: 14,
                    arrived_rps: 14,
                    completed_rps: 13.8,
                    goodput_rps: 13.4,
                    output_tps: 410,
                    ttft_p50_ms: 120,
                    ttft_p90_ms: 210,
                    tpot_p50_ms: 20,
                    tpot_p90_ms: 32,
                    e2e_p50_ms: 850,
                    e2e_p90_ms: 1300,
                    client_inflight: 6,
                    drift_p50_ms: 1.5,
                    drift_p90_ms: 5,
                    drift_p99_ms: 12,
                },
            ],
            drift_histogram: [{ label: '0-5ms', count: 850 }],
            prompt_length_buckets: [{ bucket: '<512', p50: 85, p90: 140, count: 400 }],
            cdf: { quantiles: [50, 90], ttft_ms: [120, 210] },
            ttft_heatmap: {
                sequence_length: 'input',
                time_bin_count: 1,
                sequence_length_bin_count: 1,
                cells: [{ start_seconds: 0, end_seconds: 10, sequence_length_start: 0, sequence_length_end: 512, average_metric_ms: 120, request_count: 50 }],
            },
            tpot_heatmap: {
                sequence_length: 'output',
                time_bin_count: 1,
                sequence_length_bin_count: 1,
                cells: [{ start_seconds: 0, end_seconds: 10, sequence_length_start: 0, sequence_length_end: 128, average_metric_ms: 20, request_count: 50 }],
            },
            error_breakdown: [{ status_code: 503, count: 4, percentage: 100 }],
            cold_start_comparison: {
                cold_to_steady_ratio: 1.35,
                warmup: { request_count: 100, duration_seconds: 10, ttft_ms: { p50: 162, p90: 290 } },
                steady_state: { request_count: 800, duration_seconds: 55, ttft_ms: { p50: 120, p90: 210 } },
            },
            workload: {
                fallback_to_full: false,
                fallback_synthesized: false,
                windows: {
                    profiling: {
                        window_start_seconds: 10,
                        window_end_seconds: 65,
                        duration_seconds: 55,
                        summary: {
                            counts: { completed: 796, failed: 4, total: 800 },
                            total_input_tokens: 409600,
                            total_output_tokens: 51200,
                            isl: { min: 64, p50: 480, mean: 512, p90: 900, p99: 1500, max: 2048, cv: 0.65 },
                            osl: { min: 8, p50: 56, mean: 64, p90: 110, p99: 192, max: 256, cv: 0.58 },
                            osl_isl_ratio: {
                                sample_count: 800,
                                excluded_zero_isl_count: 0,
                                mean: 0.18,
                                p50: 0.14,
                                p90: 0.35,
                                regimes: {
                                    prefill_heavy: { count: 600, share_pct: 75.0 },
                                    balanced: { count: 160, share_pct: 20.0 },
                                    decode_heavy: { count: 40, share_pct: 5.0 },
                                },
                            },
                            concurrency: { mean: 6.25, p50: 6, p90: 9, p99: 11, peak: 12, active_duration_seconds: 54.5, window_duration_seconds: 55 },
                            arrivals: { mean_arrival_rps: 14.55, peak_arrival_rps: 18.0, burstiness_peak_to_mean: 1.24, inter_arrival_cv: 0.92 },
                        },
                        isl_histogram: [{ bin_start: 0, bin_end: 512, label: '0–512', count: 440, share_pct: 55.0 }],
                        isl_cdf: [{ percentile: 0, value: 64 }, { percentile: 50, value: 480 }, { percentile: 100, value: 2048 }],
                        osl_histogram: [{ bin_start: 0, bin_end: 128, label: '0–128', count: 720, share_pct: 90.0 }],
                        osl_cdf: [{ percentile: 0, value: 8 }, { percentile: 50, value: 56 }, { percentile: 100, value: 256 }],
                        isl_vs_osl_buckets: [{ label: '<512', count: 400, share_pct: 50.0, osl_p50: 48, osl_mean: 52.5, osl_p90: 96, ratio_p50: 0.16, ratio_mean: 0.19 }],
                        request_points: [
                            { t: 12.4, isl: 480, osl: 56, req_osl: 128, ttft_ms: 118.0, tpot_ms: 14.2, ok: true },
                            { t: 18.0, isl: 256, osl: 320, req_osl: 512, ttft_ms: 96.0, tpot_ms: 13.8, ok: true },
                        ],
                        request_points_total: 2,
                        request_points_sampled: 2,
                        concurrency_residency: [{ min_level: 1, max_level: 8, label: '1–8', duration_seconds: 44.0, share_pct: 80.0 }],
                        timeline: [{ t: 15, start_seconds: 10, end_seconds: 20, is_warmup: false, in_flight_avg: 6.25, in_flight_max: 10, arrival_count: 145, arrival_rps: 14.5, completed_count: 140, completed_rps: 14.0 }],
                        multi_turn: {
                            available: true,
                            is_multi_turn: true,
                            reason: null,
                            session_count: 60,
                            max_turns: 3,
                            turns_per_session_mean: 2.5,
                            reusable_prefix_tokens: 180000,
                            reusable_prefix_pct: 43.95,
                            turns_distribution: [{ turns: 2, label: '2 turns', sessions: 40, share_pct: 66.67 }],
                            isl_by_turn: [{ label: 'Turn 1', min_turn: 1, max_turn: 1, request_count: 60, isl_p50: 256, isl_mean: 280.5, isl_p90: 480, reusable_prefix_pct: 0 }],
                        },
                    },
                    full: {
                        window_start_seconds: 0,
                        window_end_seconds: 65,
                        duration_seconds: 65,
                        summary: {
                            counts: { completed: 895, failed: 5, total: 900 },
                            total_input_tokens: 460800,
                            total_output_tokens: 57600,
                            isl: { min: 64, p50: 480, mean: 512, p90: 900, p99: 1500, max: 2048, cv: 0.65 },
                            osl: { min: 8, p50: 56, mean: 64, p90: 110, p99: 192, max: 256, cv: 0.58 },
                            osl_isl_ratio: { sample_count: 900, excluded_zero_isl_count: 0, mean: 0.18, p50: 0.14, p90: 0.35, regimes: {} },
                            concurrency: { mean: 6.0, p50: 6, p90: 9, p99: 11, peak: 12 },
                            arrivals: { mean_arrival_rps: 13.85, peak_arrival_rps: 18.0, burstiness_peak_to_mean: 1.3, inter_arrival_cv: 0.95 },
                        },
                        isl_histogram: [],
                        isl_cdf: [],
                        osl_histogram: [],
                        osl_cdf: [],
                        isl_vs_osl_buckets: [],
                        concurrency_residency: [],
                        timeline: [],
                        multi_turn: { available: true, is_multi_turn: false, reason: 'Single-turn trace.' },
                    },
                },
            },
        },
        sessions_summary: {
            available: true,
            correlation: { matched_count: 880, total_count: 900, matched_ratio: 0.978 },
            lanes: [{ slot: 0, items: [{ start_seconds: 0, end_seconds: 2, cache_hit_pct: 75, tier: 'warm' }] }],
            cache_buckets: [
                { bucket: 'cold', ttft_p50_ms: 210, ttft_p90_ms: 350, count: 100 },
                { bucket: 'warm', ttft_p50_ms: 95, ttft_p90_ms: 160, count: 400 },
            ],
            turn_distribution: [{ turns: 2, sessions: 60 }],
            tree_depth_distribution: [{ depth: 2, sessions: 60 }],
            followup_turn_stats: {
                followup_count: 120,
                followup_regression_pct: 6.5,
                first_turn_ttft_p50_ms: 180,
                followup_ttft_p50_ms: 95,
            },
        },
        server_telemetry: {
            status: 'available',
            available: true,
            summary: {
                kv_cache_usage_percent: { max: 42 },
                prefix_cache_hit_percent: { mean: 64 },
                gpu_power_watts: { mean: 210, max: 295 },
            },
            energy: {
                accelerator_avg_power_watts: 210,
                accelerator_peak_power_watts: 295,
                accelerator_energy_wh: 4.5,
                output_tokens_per_wh: 9600,
            },
            per_pod: [
                { pod: 'worker-0', role: 'worker', gpu_power_watts: { mean: 210, max: 295 } },
            ],
            series: [
                { elapsed_seconds: 5, running_requests: 5, prefix_cache_hit_percent: 64 },
            ],
            host_metrics: {
                nodes: ['node-a'],
                summary: {
                    cpu_usage_percent: { mean: 48, max: 72 },
                    memory_total_bytes: 128 * 1024 ** 3,
                    memory_used_bytes: { mean: 40 * 1024 ** 3 },
                    memory_available_bytes: { mean: 88 * 1024 ** 3, min: 80 * 1024 ** 3 },
                    memory_usage_percent: { max: 37.5 },
                    disk_read_bytes_per_second: { mean: 12 * 1024 ** 2 },
                    disk_written_bytes_per_second: { mean: 4 * 1024 ** 2 },
                    filesystem_avail_bytes: 200 * 1024 ** 3,
                    filesystem_size_bytes: 500 * 1024 ** 3,
                    network_receive_bytes_per_second: { mean: 25 * 1024 ** 2, max: 60 * 1024 ** 2 },
                    network_transmit_bytes_per_second: { mean: 18 * 1024 ** 2, max: 45 * 1024 ** 2 },
                    host_power_watts: { mean: 340, max: 420 },
                    host_energy_wh: 6.2,
                    gpu_power_watts: { mean: 210, max: 295 },
                    gpu_temperature_celsius: { max: 62 },
                },
                per_node: [
                    {
                        node: 'node-a',
                        roles: ['worker'],
                        pods: ['worker-0'],
                        cpu_usage_percent: { mean: 48, max: 72 },
                        memory_total_bytes: 128 * 1024 ** 3,
                        memory_used_bytes: { mean: 40 * 1024 ** 3 },
                        memory_available_bytes: { mean: 88 * 1024 ** 3, min: 80 * 1024 ** 3 },
                        disk_read_bytes_per_second: { mean: 12 * 1024 ** 2 },
                        disk_written_bytes_per_second: { mean: 4 * 1024 ** 2 },
                        filesystem_avail_bytes: 200 * 1024 ** 3,
                        network_receive_bytes_per_second: { mean: 25 * 1024 ** 2 },
                        network_transmit_bytes_per_second: { mean: 18 * 1024 ** 2 },
                        host_power_watts: { mean: 340, max: 420 },
                        allocated_accelerators: 1,
                        gpu_utilization_percent: { mean: 68 },
                        gpu_power_watts: { mean: 210 },
                        gpu_temperature_celsius: { max: 62 },
                    },
                ],
                series: [
                    {
                        elapsed_seconds: 5,
                        cpu_usage_percent: 48,
                        mem_used_gib: 40,
                        mem_avail_gib: 88,
                        mem_pct: 31.25,
                        disk_read_mbps: 12,
                        disk_write_mbps: 4,
                        net_rx_mbps: 25,
                        net_tx_mbps: 18,
                        host_power_w: 340,
                        accel_power_w: 210,
                    },
                ],
            },
        },
    };
}

test('tooltip preserves mixed rate and latency units including zero', () => {
    const html = renderToStaticMarkup(<SimulationTooltip active label={0} payload={[
        { dataKey: 'request_arrival_rps', name: 'Request arrival rate', value: 0 },
        { dataKey: 'average_latency_ms', name: 'Average TTFT', value: 25 },
    ]} />);
    assert.match(html, /0 req\/s/);
    assert.match(html, /25 ms/);
});

test('empty latency data retains unavailable text', () => {
    assert.match(renderToStaticMarkup(<LatencyMetricTimeline title="Latency" metric="TTFT" data={[]} color="green" />), /No per-request TTFT timestamps/);
});

test('LatencyMetricTimeline forwards helpFormula and uses CSS tokens without raw hex literals', () => {
    const defaultEl = LatencyMetricTimeline({ title: 'TTFT Timeline', metric: 'TTFT', data: [], color: 'var(--brand-accent)' });
    assert.equal(defaultEl.type, Panel);
    assert.equal(typeof defaultEl.props.help, 'string');
    assert.match(defaultEl.props.helpFormula, /Arrival_Rate.*Quantile_0\.95/);

    const explicitEl = LatencyMetricTimeline({
        title: 'TPOT Timeline',
        metric: 'TPOT',
        data: [{ time_seconds: 1, request_arrival_rps: 10, average_latency_ms: 20, p95_latency_ms: 30 }],
        color: 'var(--brand-accent)',
        help: 'Custom TPOT timeline description.',
        helpFormula: 'TPOT_Custom = (t_last - t_first) / max(1, Out - 1)',
    });
    assert.equal(explicitEl.props.help, 'Custom TPOT timeline description.');
    assert.equal(explicitEl.props.helpFormula, 'TPOT_Custom = (t_last - t_first) / max(1, Out - 1)');

    const source = fs.readFileSync(path.join(HERE, 'LatencyMetricTimeline.jsx'), 'utf8');
    assert.doesNotMatch(source, /#[0-9a-fA-F]{6}/);
    assert.match(source, /var\(--border-color\)/);
    assert.match(source, /var\(--text-muted\)/);
});

test('LatencyHeatmap keeps help as plain string and forwards helpFormula in empty and populated states', () => {
    function renderDirect(props) {
        let out = null;
        function Capture() {
            out = LatencyHeatmap(props);
            return null;
        }
        renderToStaticMarkup(<Capture />);
        return out;
    }

    const emptyEl = renderDirect({ title: 'TTFT Heatmap', metric: 'TTFT', data: { cells: [] } });
    assert.equal(emptyEl.type, ChartContainer);
    assert.equal(typeof emptyEl.props.help, 'string');
    assert.match(emptyEl.props.helpFormula, /Point_i = \(t_i, ISL_i, TTFT_i\)/);

    const popEl = renderDirect({
        title: 'TPOT Heatmap',
        metric: 'TPOT',
        data: {
            sequence_length: 'output',
            points: [{ t: 1.5, seq: 64, ms: 18 }],
        },
        help: 'Caller plain string help for TPOT scatter.',
        helpFormula: 'Point_i = (t_i, OSL_i, TPOT_i)',
    });
    assert.equal(popEl.type, ChartContainer);
    assert.equal(typeof popEl.props.help, 'string');
    assert.equal(popEl.props.help, 'Caller plain string help for TPOT scatter.');
    assert.equal(popEl.props.helpFormula, 'Point_i = (t_i, OSL_i, TPOT_i)');
});

test('OverviewTab, BottlenecksTab, RequestsTab, and SystemTab attach formulas and scrub engine-specific names from tooltips', () => {
    const telemetry = makeSliceTelemetry();
    const task = {
        simulation: {
            backend_options: { ttft_slo: 0.5, tpot_slo: 0.05, e2e_slo: 2.0 },
        },
    };

    const tabs = [
        ['OverviewTab', <OverviewTab task={task} telemetry={telemetry} analysisWindow="profiling" onSelectTab={() => {}} onOpenErrorInspector={() => {}} />, () => OverviewTab({ task, telemetry, analysisWindow: 'profiling', onSelectTab: () => {}, onOpenErrorInspector: () => {} })],
        ['WorkloadTab', <WorkloadTab task={task} telemetry={telemetry} analysisWindow="profiling" />, () => WorkloadTab({ task, telemetry, analysisWindow: 'profiling' })],
        ['BottlenecksTab', <BottlenecksTab telemetry={telemetry} onSelectTab={() => {}} />, () => BottlenecksTab({ telemetry, onSelectTab: () => {} })],
        ['RequestsTab', <RequestsTab task={task} telemetry={telemetry} analysisWindow="profiling" onOpenErrorInspector={() => {}} />, () => RequestsTab({ task, telemetry, analysisWindow: 'profiling', onOpenErrorInspector: () => {} })],
        ['SystemTab', <SystemTab telemetry={telemetry} />, () => SystemTab({ telemetry })],
    ];

    for (const [name, jsx, renderFn] of tabs) {
        const html = renderToStaticMarkup(jsx);
        let tree = null;
        function Capture() {
            tree = renderFn();
            return null;
        }
        renderToStaticMarkup(<Capture />);
        const tooltipBodies = extractTooltipBodies(html);
        assert.ok(tooltipBodies.length > 0, `Expected tooltips in ${name}`);
        for (const body of tooltipBodies) {
            assert.doesNotMatch(body, ENGINE_LEAK_RE, `Engine-specific metric/name leaked in ${name} tooltip: ${body}`);
        }

        const charts = collectElements(tree, (el) => el.type === ChartContainer || el.type === LatencyHeatmap);
        assert.ok(charts.length > 0, `Expected ChartContainer/LatencyHeatmap in ${name}`);
        for (const chart of charts) {
            assert.equal(typeof chart.props.help, 'string', `Expected string help on chart ${chart.props.title} in ${name}`);
            assert.ok(typeof chart.props.helpFormula === 'string' && chart.props.helpFormula.trim().length > 0,
                `Expected non-empty helpFormula on chart ${chart.props.title} in ${name}`);
            assert.doesNotMatch(chart.props.helpFormula, ENGINE_LEAK_RE, `Engine leak in helpFormula for ${chart.props.title}`);
        }

        const statCards = collectElements(tree, (el) => el.type === StatCard);
        for (const card of statCards) {
            assert.ok(typeof card.props.helpFormula === 'string' && card.props.helpFormula.trim().length > 0,
                `Expected helpFormula on StatCard ${card.props.title} in ${name}`);
            assert.doesNotMatch(card.props.helpFormula, ENGINE_LEAK_RE);
            for (const detail of (card.props.details || [])) {
                if (detail.label !== 'Action') {
                    assert.ok(typeof detail.formula === 'string' && detail.formula.trim().length > 0,
                        `Expected formula on StatCard detail ${detail.label} in ${name}`);
                    assert.doesNotMatch(detail.formula, ENGINE_LEAK_RE);
                }
            }
        }

        const tooltips = collectElements(tree, (el) => el.type === HelpTooltip);
        for (const tip of tooltips) {
            if (tip.props.formula) {
                assert.doesNotMatch(tip.props.formula, ENGINE_LEAK_RE, `Engine leak in HelpTooltip formula ${tip.props.label}`);
            }
        }
    }

    // Verify canonical t59 titles and aria-labels remain preserved on BottlenecksTab
    const bottlenecksHtml = renderToStaticMarkup(<BottlenecksTab telemetry={telemetry} />);
    assert.match(bottlenecksHtml, /EPP Predicted vs vLLM Actual Prefix Cache Hit Rate/);
    assert.match(bottlenecksHtml, /aria-label="vLLM Actual hit help"/);
    assert.match(bottlenecksHtml, /aria-label="EPP vs vLLM cache gap help"/);

    // Verify unified prefix cache thresholds on RequestsTab
    let reqTree = null;
    function CaptureReq() {
        reqTree = RequestsTab({ task, telemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureReq />);
    const reqCharts = collectElements(reqTree, (el) => el.type === ChartContainer);
    const sessionsChart = reqCharts.find((c) => c.props.title === 'Sessions over Time');
    const cacheBucketChart = reqCharts.find((c) => c.props.title === 'TTFT by Prefix Cache Hit Bucket');
    assert.match(sessionsChart.props.helpFormula, /Greedy_Slot_Packing/);
    assert.match(sessionsChart.props.helpFormula, /Cold < 10%.*Partial 10–70%.*Warm ≥ 70%/);
    assert.match(cacheBucketChart.props.subtitle, /Cold \(< 10%\).*Partial \(10–70%\).*Warm \(≥ 70%\)/);
    assert.match(cacheBucketChart.props.helpFormula, /Matched_Prefix_Tokens \/ Prompt_Tokens/);
    assert.match(cacheBucketChart.props.helpFormula, /Cold < 10%.*Partial 10–70%.*Warm ≥ 70%/);
    assert.match(cacheBucketChart.props.helpFormula, /completed requests/);

    // Verify zero raw hex colors across all owned JSX files
    for (const file of [
        'OverviewTab.jsx',
        'WorkloadTab.jsx',
        'BottlenecksTab.jsx',
        'RequestsTab.jsx',
        'LatencyHeatmap.jsx',
        'LatencyMetricTimeline.jsx',
        'LiveMonitorPanel.jsx',
        'RunCompareDrawer.jsx',
        'SystemTab.jsx',
        'HardwareTab.jsx',
    ]) {
        const content = fs.readFileSync(path.join(HERE, file), 'utf8');
        assert.doesNotMatch(content, /#[0-9a-fA-F]{6}/, `Unexpected raw hex color in ${file}`);
        if (file === 'WorkloadTab.jsx' || file === 'LiveMonitorPanel.jsx' || file === 'LatencyHeatmap.jsx' || file === 'RequestsTab.jsx' || file === 'RunCompareDrawer.jsx' || file === 'SystemTab.jsx' || file === 'HardwareTab.jsx') {
            assert.doesNotMatch(content, /\bfixed inset-0\b/, `Unexpected fixed inset-0 in ${file}`);
            assert.doesNotMatch(content, /\banimate-spin\b/, `Unexpected animate-spin in ${file}`);
        }
        if (file === 'SystemTab.jsx' || file === 'HardwareTab.jsx') {
            assert.doesNotMatch(content, /\.toFixed\(/, `Unexpected raw .toFixed( in ${file}`);
            assert.doesNotMatch(content, /\(GiB\)|\(MiB\/s\)/, `Unexpected locked (GiB)/(MiB/s) unit in ${file}`);
        }
        if (file === 'WorkloadTab.jsx') {
            assert.doesNotMatch(content, /\.toFixed\(/, `Unexpected raw .toFixed( in ${file}`);
        }
    }

    // Verify WorkloadTab tooltips/formulas contain zero engine/vendor/tool brand leaks
    const workloadBrandRe = /\b(aiperf|vllm|sglang|trt-?llm|prometheus|GPU)\b/i;
    const workloadHtml = renderToStaticMarkup(<WorkloadTab task={task} telemetry={telemetry} analysisWindow="profiling" />);
    for (const body of extractTooltipBodies(workloadHtml)) {
        assert.doesNotMatch(body, workloadBrandRe, `Brand leak in WorkloadTab tooltip: ${body}`);
    }
    const workloadSource = fs.readFileSync(path.join(HERE, 'WorkloadTab.jsx'), 'utf8');
    assert.doesNotMatch(workloadSource, workloadBrandRe, 'Brand leak in WorkloadTab.jsx source');
    assert.match(workloadHtml, /Input Sequence Length vs Output Sequence Length/);
    assert.match(workloadHtml, /aria-label="Input sequence length profile help"/);
    assert.match(workloadHtml, /aria-label="Output sequence length profile help"/);
    assert.match(workloadHtml, /aria-label="ISL vs OSL joint profile help"/);
    assert.match(workloadHtml, /aria-label="Concurrency and arrival dynamics help"/);
});

test('BottlenecksTab renders semantic role badges for P/D findings and omits them when role is absent or unrecognized', () => {
    const base = makeSliceTelemetry();
    const withRolesTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            findings: [
                {
                    id: 'f-prefill',
                    severity: 'warning',
                    category: 'queue',
                    role: 'prefill',
                    title: 'Prefill queueing elevated',
                    description: 'Prefill pods experienced request queueing.',
                },
                {
                    id: 'f-decode',
                    severity: 'critical',
                    category: 'queue',
                    role: 'decode',
                    title: 'Decode queueing saturated',
                    description: 'Decode pods experienced high queue wait.',
                },
                {
                    id: 'f-both',
                    severity: 'warning',
                    category: 'kv_cache',
                    role: 'both',
                    title: 'Both P and D under pressure',
                    description: 'Both prefill and decode pools hit saturation.',
                },
                {
                    id: 'f-none',
                    severity: 'info',
                    category: 'capacity',
                    title: 'Aggregated worker headroom',
                    description: 'No role field on aggregated run finding.',
                },
            ],
        },
    };

    const htmlWithRoles = renderToStaticMarkup(<BottlenecksTab telemetry={withRolesTelemetry} />);
    assert.match(htmlWithRoles, /Prefill \(P\)/);
    assert.match(htmlWithRoles, /Decode \(D\)/);
    assert.match(htmlWithRoles, /P \+ D/);

    let roleTree = null;
    function CaptureRoles() {
        roleTree = BottlenecksTab({ telemetry: withRolesTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureRoles />);
    const roleBadges = collectElements(roleTree, (el) => el.type === Badge)
        .filter((el) => ['Prefill (P)', 'Decode (D)', 'P + D'].includes(el.props.children));
    assert.deepEqual(
        roleBadges.map((el) => ({ label: el.props.children, tone: el.props.tone })),
        [
            { label: 'Decode (D)', tone: 'violet' },
            { label: 'Prefill (P)', tone: 'info' },
            { label: 'P + D', tone: 'neutral' },
        ],
    );

    const withoutRolesTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            findings: [
                {
                    id: 'f-absent',
                    severity: 'info',
                    category: 'capacity',
                    title: 'Aggregated finding without role',
                },
                {
                    id: 'f-null',
                    severity: 'warning',
                    category: 'queue',
                    role: null,
                    title: 'Finding with null role',
                },
                {
                    id: 'f-unknown',
                    severity: 'info',
                    category: 'capacity',
                    role: 'worker',
                    title: 'Finding with unrecognized role',
                },
            ],
        },
    };

    const htmlWithoutRoles = renderToStaticMarkup(<BottlenecksTab telemetry={withoutRolesTelemetry} />);
    assert.doesNotMatch(htmlWithoutRoles, /Prefill \(P\)/);
    assert.doesNotMatch(htmlWithoutRoles, /Decode \(D\)/);
    assert.doesNotMatch(htmlWithoutRoles, /P \+ D/);
});

function collectDomNodes(root, predicate, out = []) {
    if (!root || root.nodeType !== 1) return out;
    if (predicate(root)) out.push(root);
    for (const child of root.childNodes) collectDomNodes(child, predicate, out);
    return out;
}

function getReactProps(node) {
    const key = Object.keys(node || {}).find((entry) => entry.startsWith('__reactProps$'));
    return key ? node[key] : {};
}

async function withSimulationDom(t, fetchHandler, run) {
    const prevAct = globalThis.IS_REACT_ACT_ENVIRONMENT;
    const prevDoc = globalThis.document;
    const prevWin = globalThis.window;
    const prevHtml = globalThis.HTMLElement;
    const prevSvg = globalThis.SVGElement;
    const prevFetch = globalThis.fetch;
    const prevResizeObserver = globalThis.ResizeObserver;
    const prevGetComputedStyle = globalThis.getComputedStyle;
    const pendingTimers = new Set();

    const makeNode = (tag, doc) => ({
        nodeType: tag === '#text' ? 3 : tag === '#comment' ? 8 : 1,
        nodeName: tag.toUpperCase(),
        tagName: tag.toUpperCase(),
        namespaceURI: 'http://www.w3.org/1999/xhtml',
        ownerDocument: doc,
        parentNode: null,
        childNodes: [],
        attributes: {},
        style: {},
        dataset: {},
        nodeValue: '',
        get textContent() {
            if (this.nodeType === 3) return this.nodeValue || '';
            if (this.nodeType === 8) return '';
            return this.childNodes.map((child) => child.textContent).join('');
        },
        set textContent(value) {
            const text = String(value);
            if (this.nodeType === 3) {
                this.nodeValue = text;
                return;
            }
            this.childNodes = text ? [doc.createTextNode(text)] : [];
            for (const child of this.childNodes) child.parentNode = this;
        },
        addEventListener() {},
        removeEventListener() {},
        setAttribute(key, value) { this.attributes[key] = String(value); },
        removeAttribute(key) { delete this.attributes[key]; },
        getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; },
        hasAttribute(key) { return key in this.attributes; },
        appendChild(child) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = this;
            this.childNodes.push(child);
            return child;
        },
        insertBefore(child, reference) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = this;
            const index = reference ? this.childNodes.indexOf(reference) : -1;
            if (index === -1) this.childNodes.push(child);
            else this.childNodes.splice(index, 0, child);
            return child;
        },
        removeChild(child) {
            const index = this.childNodes.indexOf(child);
            if (index !== -1) {
                this.childNodes.splice(index, 1);
                child.parentNode = null;
            }
            return child;
        },
        get firstChild() { return this.childNodes[0] || null; },
        get options() { return collectDomNodes(this, (el) => el.tagName === 'OPTION'); },
        get nextSibling() {
            if (!this.parentNode) return null;
            const index = this.parentNode.childNodes.indexOf(this);
            return index >= 0 ? this.parentNode.childNodes[index + 1] || null : null;
        },
        getBoundingClientRect() { return { top: 0, left: 0, width: 640, height: 320, right: 640, bottom: 320 }; },
        focus() {},
        blur() {},
        closest() { return null; },
        remove() { this.parentNode?.removeChild(this); },
    });

    const trackedSetTimeout = (fn, delay, ...args) => {
        const handle = setTimeout(() => {
            pendingTimers.delete(handle);
            fn(...args);
        }, delay);
        pendingTimers.add(handle);
        return handle;
    };
    const trackedClearTimeout = (handle) => {
        pendingTimers.delete(handle);
        clearTimeout(handle);
    };
    const win = {
        location: new URL('http://localhost/'),
        history: {
            replaceState(_state, _title, nextUrl) {
                win.location = new URL(nextUrl, 'http://localhost');
            },
        },
        addEventListener() {},
        removeEventListener() {},
        setTimeout: trackedSetTimeout,
        clearTimeout: trackedClearTimeout,
        setInterval,
        clearInterval,
        getComputedStyle: () => ({ width: '640px', height: '320px', getPropertyValue: () => '' }),
        requestAnimationFrame: (callback) => trackedSetTimeout(callback, 0),
        cancelAnimationFrame: trackedClearTimeout,
        HTMLIFrameElement: function HTMLIFrameElement() {},
        HTMLElement: function HTMLElement() {},
        SVGElement: function SVGElement() {},
        Node: function Node() {},
    };
    const doc = {
        nodeType: 9,
        hidden: true,
        cookie: '',
        defaultView: win,
        documentElement: null,
        body: null,
        activeElement: null,
        createElement(tag) { return makeNode(tag, doc); },
        createElementNS(ns, tag) {
            const node = makeNode(tag, doc);
            node.namespaceURI = ns;
            return node;
        },
        createTextNode(text) {
            const node = makeNode('#text', doc);
            node.nodeValue = String(text);
            return node;
        },
        createComment(text) {
            const node = makeNode('#comment', doc);
            node.nodeValue = String(text);
            return node;
        },
        addEventListener() {},
        removeEventListener() {},
        getElementById() { return null; },
    };
    win.document = doc;
    doc.documentElement = makeNode('html', doc);
    doc.body = makeNode('body', doc);
    doc.documentElement.appendChild(doc.body);

    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    globalThis.document = doc;
    globalThis.window = win;
    globalThis.HTMLElement = win.HTMLElement;
    globalThis.SVGElement = win.SVGElement;
    globalThis.fetch = fetchHandler;
    globalThis.getComputedStyle = win.getComputedStyle;
    globalThis.ResizeObserver = class ResizeObserver {
        observe() {}
        unobserve() {}
        disconnect() {}
    };

    const container = doc.createElement('div');
    doc.body.appendChild(container);
    const root = createRoot(container);
    const flush = async () => {
        await act(async () => {
            await Promise.resolve();
            await Promise.resolve();
        });
    };

    try {
        await run({ root, doc, flush });
    } finally {
        await act(async () => {
            root.unmount();
        });
        for (const handle of pendingTimers) clearTimeout(handle);
        pendingTimers.clear();
        globalThis.IS_REACT_ACT_ENVIRONMENT = prevAct;
        globalThis.document = prevDoc;
        globalThis.window = prevWin;
        globalThis.HTMLElement = prevHtml;
        globalThis.SVGElement = prevSvg;
        globalThis.fetch = prevFetch;
        globalThis.ResizeObserver = prevResizeObserver;
        globalThis.getComputedStyle = prevGetComputedStyle;
    }
}

test('SimulationDashboard tooltips stay engine-agnostic and token selector enforces ownership, empty CTA, and auth-disabled guard', async (t) => {
    const dashboardSource = fs.readFileSync(path.join(HERE, '../SimulationDashboard.jsx'), 'utf8');
    const helpStrings = [
        ...dashboardSource.matchAll(/<HelpTooltip[^>]*>([\s\S]*?)<\/HelpTooltip>/g),
        ...dashboardSource.matchAll(/\bhelp="([^"]+)"/g),
    ].map((match) => match[1]);
    assert.ok(helpStrings.length > 0);
    for (const copy of helpStrings) {
        assert.doesNotMatch(copy, ENGINE_LEAK_RE, `Engine-specific copy leaked into SimulationDashboard tooltip: ${copy}`);
    }

    const seededForm = createSimulationFormState({ model_access_token_id: 'tok-saved' });
    assert.equal(seededForm.modelAccessTokenId, 'tok-saved');
    assert.equal(Object.hasOwn(seededForm, 'apiKey'), false);

    // 1. Active token selection, discovery, empty-state navigation, and deployment rerun forwarding
    const requests = [];
    const navigated = [];
    await withSimulationDom(
        t,
        async (url, options = {}) => {
            requests.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
            if (url === '/api/v1/model-service/tokens') {
                return Response.json({
                    items: [
                        { id: 'tok-revoked', name: 'Old', status: 'revoked', tokenHint: '0000' },
                        { id: 'tok-expired', name: 'Expired', status: 'active', tokenHint: '1111', expiresAt: '2020-01-01T00:00:00Z' },
                        { id: 'tok-1', name: 'Primary', status: 'active', tokenHint: 'a1b2' },
                    ],
                });
            }
            if (url === '/api/v1/model-service/models') {
                return Response.json({ items: [{ id: 'svc-1', name: 'qwen-svc', baseModel: 'Qwen/Qwen3-8B', clusterName: 'prod' }] });
            }
            if (url === '/api/v1/simulation/models/discover') {
                return Response.json({ models: ['Qwen/Qwen3-8B'] });
            }
            if (url === '/api/v1/simulation/tasks/task-dep-1/rerun') {
                return Response.json({ task: { id: 'task-dep-2', status: 'queued' } });
            }
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({
                    tasks: [{
                        id: 'task-dep-1',
                        name: 'Dep run',
                        status: 'completed',
                        endpoint_mode: 'deployment',
                        model_service_group_id: 'svc-1',
                        model_access_token_id: 'tok-foreign-or-revoked',
                    }],
                    total: 1,
                    counts: { queued: 0, running: 0, completed: 1, failed: 0, cancelled: 0 },
                    facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] },
                });
            }
            if (url === '/api/v1/simulation/backends') {
                return Response.json({ backends: [{ name: 'aiperf', display_name: 'AIPerf', available: true, scenarios: [{ name: 'chat', label: 'Chat' }] }] });
            }
            if (url === '/api/v1/simulation/scenarios') {
                return Response.json({ scenarios: [{ value: 'chat', label: 'Chat' }] });
            }
            if (url === '/api/v1/simulation/trace-datasets') {
                return Response.json({ datasets: [] });
            }
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ principal: { userId: 'u-1' }, can: () => true }}>
                        <SimulationDashboard initialAction="new" onNavigate={(view, params) => navigated.push({ view, params })} />
                    </AuthContext.Provider>,
                );
            });
            await flush();
            await flush();

            const tokenSelect = collectDomNodes(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-api-key')[0];
            assert.ok(tokenSelect, 'Expected Model access token <Select>');
            assert.equal(getReactProps(tokenSelect).value, 'tok-1');
            const optionLabels = tokenSelect.options.map((opt) => opt.textContent);
            assert.deepEqual(optionLabels, ['Primary (lens-mk-…a1b2)']);

            const manageButton = collectDomNodes(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.includes('Manage / Create key in API keys'))[0];
            assert.ok(manageButton);
            await act(async () => {
                getReactProps(manageButton).onClick?.();
            });
            assert.deepEqual(navigated[0], { view: 'api-keys', params: { action: 'create' } });

            // Rerun deployment task with stale/foreign token forwards active tok-1
            const rerunButton = collectDomNodes(doc.body, (el) => el.tagName === 'BUTTON' && el.getAttribute('aria-label') === 'Rerun simulation task Dep run')[0]
                || collectDomNodes(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.includes('Rerun'))[0];
            assert.ok(rerunButton, 'Expected Rerun button on task row');
            await act(async () => {
                getReactProps(rerunButton).onClick?.({ stopPropagation() {} });
            });
            await flush();
            const rerunReq = requests.find((req) => req.url === '/api/v1/simulation/tasks/task-dep-1/rerun');
            assert.ok(rerunReq);
            assert.equal(rerunReq.body.model_access_token_id, 'tok-1');
            assert.equal(Object.hasOwn(rerunReq.body, 'api_key'), false);

            // Switch to external mode and probe endpoint using optional unauthenticated selection
            const extRadio = collectDomNodes(doc.body, (el) => el.tagName === 'INPUT' && getReactProps(el).name === 'simulation-endpoint-mode' && getReactProps(el).value === 'external')[0];
            await act(async () => {
                getReactProps(extRadio).onChange?.({ target: { value: 'external' } });
            });
            await flush();
            const extTokenSelect = collectDomNodes(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-api-key')[0];
            assert.equal(getReactProps(extTokenSelect).value, '');
            assert.ok(extTokenSelect.options.some((opt) => opt.textContent === 'None (unauthenticated endpoint)'));

            const endpointInput = collectDomNodes(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-endpoint')[0];
            await act(async () => {
                getReactProps(endpointInput).onChange?.({ target: { value: 'http://localhost:8000' } });
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 560));
            });
            await flush();
            const discoverReq = requests.find((req) => req.url === '/api/v1/simulation/models/discover');
            assert.ok(discoverReq);
            assert.equal(discoverReq.body.model_access_token_id, null);
            assert.equal(Object.hasOwn(discoverReq.body, 'api_key'), false);
        },
    );

    // 2. Empty active tokens state renders Create API key CTA
    await withSimulationDom(
        t,
        async (url) => {
            if (url === '/api/v1/model-service/tokens') return Response.json({ items: [] });
            if (url === '/api/v1/model-service/models') return Response.json({ items: [] });
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({ tasks: [], total: 0, counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }, facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] } });
            }
            if (url === '/api/v1/simulation/backends') return Response.json({ backends: [] });
            if (url === '/api/v1/simulation/scenarios') return Response.json({ scenarios: [] });
            if (url === '/api/v1/simulation/trace-datasets') return Response.json({ datasets: [] });
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush }) => {
            const emptyNavigated = [];
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ principal: { userId: 'u-1' }, can: () => true }}>
                        <SimulationDashboard initialAction="new" onNavigate={(view, params) => emptyNavigated.push({ view, params })} />
                    </AuthContext.Provider>,
                );
            });
            await flush();
            await flush();
            assert.match(doc.body.textContent, /No active model access tokens/);
            const createBtn = collectDomNodes(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'Create API key')[0];
            assert.ok(createBtn);
            await act(async () => {
                getReactProps(createBtn).onClick?.();
            });
            assert.deepEqual(emptyNavigated[0], { view: 'api-keys', params: { action: 'create' } });
        },
    );

    // 3. Auth-disabled (principal.userId === 'system') skips listModelTokens() and listModelNames()
    const authDisabledUrls = [];
    await withSimulationDom(
        t,
        async (url) => {
            authDisabledUrls.push(url);
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({ tasks: [], total: 0, counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }, facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] } });
            }
            if (url === '/api/v1/simulation/backends') return Response.json({ backends: [] });
            if (url === '/api/v1/simulation/scenarios') return Response.json({ scenarios: [] });
            if (url === '/api/v1/simulation/trace-datasets') return Response.json({ datasets: [] });
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ principal: { userId: 'system' }, can: () => true }}>
                        <SimulationDashboard initialAction="new" />
                    </AuthContext.Provider>,
                );
            });
            await flush();
            await flush();
            assert.equal(authDisabledUrls.includes('/api/v1/model-service/tokens'), false);
            assert.equal(authDisabledUrls.includes('/api/v1/model-service/models'), false);
            assert.match(doc.body.textContent, /PRISM_AUTH_MODE=local/);
        },
    );

    // 4. Missing model-service:token:manage permission & HTTP 403 render amber callout without empty-state CTA
    const forbiddenFetchUrls = [];
    await withSimulationDom(
        t,
        async (url) => {
            forbiddenFetchUrls.push(url);
            if (url === '/api/v1/model-service/models') return Response.json({ items: [] });
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({ tasks: [], total: 0, counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }, facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] } });
            }
            if (url === '/api/v1/simulation/backends') return Response.json({ backends: [] });
            if (url === '/api/v1/simulation/scenarios') return Response.json({ scenarios: [] });
            if (url === '/api/v1/simulation/trace-datasets') return Response.json({ datasets: [] });
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ principal: { userId: 'u-1' }, can: () => false }}>
                        <SimulationDashboard initialAction="new" />
                    </AuthContext.Provider>,
                );
            });
            await flush();
            await flush();
            assert.equal(forbiddenFetchUrls.includes('/api/v1/model-service/tokens'), false);
            assert.match(doc.body.textContent, /model-service:token:manage/);
            assert.doesNotMatch(doc.body.textContent, /No active model access tokens/);
        },
    );

    await withSimulationDom(
        t,
        async (url) => {
            if (url === '/api/v1/model-service/tokens') {
                return Response.json({ detail: 'Forbidden' }, { status: 403 });
            }
            if (url === '/api/v1/model-service/models') return Response.json({ items: [] });
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({ tasks: [], total: 0, counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }, facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] } });
            }
            if (url === '/api/v1/simulation/backends') return Response.json({ backends: [] });
            if (url === '/api/v1/simulation/scenarios') return Response.json({ scenarios: [] });
            if (url === '/api/v1/simulation/trace-datasets') return Response.json({ datasets: [] });
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ principal: { userId: 'u-1' }, can: () => true }}>
                        <SimulationDashboard initialAction="new" />
                    </AuthContext.Provider>,
                );
            });
            await flush();
            await flush();
            assert.match(doc.body.textContent, /model-service:token:manage/);
            assert.doesNotMatch(doc.body.textContent, /No active model access tokens/);
        },
    );

    // 5. Refresh reconciles revoked selected token and ignores out-of-order stale responses
    let tokenCall = 0;
    let resolveSlowRefresh;
    let resolveFastRefresh;
    await withSimulationDom(
        t,
        async (url) => {
            if (url === '/api/v1/model-service/tokens') {
                tokenCall += 1;
                if (tokenCall === 1) {
                    return Response.json({
                        items: [
                            { id: 'tok-a', name: 'Alpha', status: 'active', tokenHint: 'aaaa' },
                            { id: 'tok-b', name: 'Beta', status: 'active', tokenHint: 'bbbb' },
                        ],
                    });
                }
                if (tokenCall === 2) {
                    return new Promise((resolve) => {
                        resolveSlowRefresh = () => resolve(Response.json({
                            items: [
                                { id: 'tok-stale', name: 'Stale', status: 'active', tokenHint: 'cccc' },
                            ],
                        }));
                    });
                }
                return new Promise((resolve) => {
                    resolveFastRefresh = () => resolve(Response.json({
                        items: [
                            { id: 'tok-a', name: 'Alpha', status: 'revoked', tokenHint: 'aaaa' },
                            { id: 'tok-b', name: 'Beta', status: 'active', tokenHint: 'bbbb' },
                        ],
                    }));
                });
            }
            if (url === '/api/v1/model-service/models') return Response.json({ items: [] });
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({ tasks: [], total: 0, counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }, facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] } });
            }
            if (url === '/api/v1/simulation/backends') return Response.json({ backends: [] });
            if (url === '/api/v1/simulation/scenarios') return Response.json({ scenarios: [] });
            if (url === '/api/v1/simulation/trace-datasets') return Response.json({ datasets: [] });
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ principal: { userId: 'u-1' }, can: () => true }}>
                        <SimulationDashboard initialAction="new" />
                    </AuthContext.Provider>,
                );
            });
            await flush();
            await flush();

            let tokenSelect = collectDomNodes(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-api-key')[0];
            assert.equal(getReactProps(tokenSelect).value, 'tok-a');

            // Trigger two overlapping Refresh clicks (ignoring AbortSignal in mock to test requestId guard)
            const refreshButtons = collectDomNodes(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.includes('Refresh'));
            const tokenRefreshBtn = refreshButtons.at(-1);
            assert.ok(tokenRefreshBtn);

            await act(async () => {
                getReactProps(tokenRefreshBtn).onClick?.();
            });
            await act(async () => {
                getReactProps(tokenRefreshBtn).onClick?.();
            });

            // Resolve newer call (#3) first, then older call (#2) second
            await act(async () => {
                resolveFastRefresh();
                await Promise.resolve();
            });
            await flush();
            await act(async () => {
                resolveSlowRefresh();
                await Promise.resolve();
            });
            await flush();

            tokenSelect = collectDomNodes(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-api-key')[0];
            assert.equal(getReactProps(tokenSelect).value, 'tok-b');
            assert.deepEqual(tokenSelect.options.map((opt) => opt.textContent), ['Beta (lens-mk-…bbbb)']);
        },
    );
});

test('OverviewTab syncs Hero Verdict KPIs to active Full Run window, computes steady-state energy tok/Wh, and resolves P/D Peak KV Usage', () => {
    const base = makeSliceTelemetry();
    const series = [];
    for (let t = 0; t <= 145; t += 5) {
        series.push({
            elapsed_seconds: t,
            gpu_power_watts: t < 12 ? 180 : 250,
            running_requests: 5,
        });
    }
    const pdTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            verdict: {
                ...base.forensics.verdict,
                attainment_pct: 30.93,
                throughput_rps: 0.722,
                throughput_per_hr: 2599.2,
                goodput_rps: 0.2233,
                goodput_per_hr: 803.9,
            },
        },
        client_series: {
            ...base.client_series,
            warmup_seconds: 12,
            duration_seconds: 146.35,
            windows: {
                profiling: {
                    request_count: 97,
                    successful_requests: 97,
                    failed_requests: 0,
                    good_requests: 30,
                    slo_attainment_pct: 30.93,
                    throughput_rps: 0.722,
                    throughput_per_hr: 2599.2,
                    goodput_rps: 0.2233,
                    goodput_per_hr: 803.9,
                    total_output_tokens: 42442,
                    output_tok_per_s: 315.91,
                    error_rate_pct: 0,
                    ttft_ms: { p50: 1660.6, p90: 5030.4 },
                    tpot_ms: { p50: 25.2, p90: 45.1 },
                },
                full: {
                    request_count: 120,
                    successful_requests: 120,
                    failed_requests: 0,
                    good_requests: 51,
                    slo_attainment_pct: 42.5,
                    throughput_rps: 0.82,
                    throughput_per_hr: 2952,
                    goodput_rps: 0.3485,
                    goodput_per_hr: 1254.6,
                    total_output_tokens: 58280,
                    output_tok_per_s: 398.23,
                    error_rate_pct: 0,
                    ttft_ms: { p50: 1261.3, p90: 4844.7 },
                    tpot_ms: { p50: 22.1, p90: 41.3 },
                },
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'disaggregated',
            step_seconds: 5,
            summary: {
                ...base.server_telemetry.summary,
                kv_cache_usage_percent: { max: 28.2 },
            },
            per_pod: [
                { pod: 'decode-0', role: 'decode', kv_cache_usage_percent: { max: 97.49 }, gpu_power_watts: { mean: 110, max: 130 } },
                { pod: 'prefill-0', role: 'prefill', kv_cache_usage_percent: { max: 22.3 }, gpu_power_watts: { mean: 60, max: 80 } },
            ],
            energy: {
                accelerator_avg_power_watts: 260.7,
                accelerator_peak_power_watts: 314.8,
                accelerator_energy_wh: 10.55688,
                output_tokens_per_wh: 5520.57,
            },
            series,
        },
    };

    // 1. Profiling Steady-State: steady-state energy integral = 250W * (145 - 15)s / 3600 = 9.0278 Wh; 42442 / 9.0278 = 4701.3 tok/Wh (~4.70K)
    const profilingHtml = renderToStaticMarkup(
        <OverviewTab task={{}} telemetry={pdTelemetry} analysisWindow="profiling" />,
    );
    assert.match(profilingHtml, /30\.93%/);
    assert.match(profilingHtml, /0\.72 req\/s/);
    assert.match(profilingHtml, /0\.22 req\/s/);
    assert.match(profilingHtml, /97\.49%/);
    assert.doesNotMatch(profilingHtml, />28\.20%</);
    assert.match(profilingHtml, /9\.03 Wh/);
    assert.match(profilingHtml, /4\.70K tok\/Wh/);
    assert.doesNotMatch(profilingHtml, /Classification: Profiling Steady-State/);

    // 2. Full Run: Hero Verdict KPIs match Full Run window + label explains steady-state verdict classification; energy uses full run
    const fullHtml = renderToStaticMarkup(
        <OverviewTab task={{}} telemetry={pdTelemetry} analysisWindow="full" />,
    );
    assert.match(fullHtml, /Classification: Profiling Steady-State · KPIs: Full Run/);
    assert.match(fullHtml, /42\.50%/);
    assert.match(fullHtml, /0\.82 req\/s/);
    assert.match(fullHtml, /0\.35 req\/s/);
    assert.match(fullHtml, /10\.56 Wh/);
    assert.match(fullHtml, /5\.52K tok\/Wh/);

    // 3. P/D Peak KV fallback chain: summary_by_role -> role_series -> forensics.peak_kv_cache_pct, while aggregated stays on summary
    const roleSummaryHtml = renderToStaticMarkup(
        <OverviewTab
            task={{}}
            telemetry={{
                ...pdTelemetry,
                server_telemetry: {
                    ...pdTelemetry.server_telemetry,
                    per_pod: [{ pod: 'decode-0', role: 'decode' }],
                    summary_by_role: { decode: { kv_cache_usage_percent: { max: 88.4 } } },
                },
            }}
            analysisWindow="profiling"
        />,
    );
    assert.match(roleSummaryHtml, /88\.40%/);

    const roleSeriesHtml = renderToStaticMarkup(
        <OverviewTab
            task={{}}
            telemetry={{
                ...pdTelemetry,
                server_telemetry: {
                    ...pdTelemetry.server_telemetry,
                    per_pod: [{ pod: 'decode-0', role: 'decode' }],
                    summary_by_role: null,
                    role_series: [
                        { decode_kv_cache_usage_percent: 71.2, prefill_kv_cache_usage_percent: 18.5 },
                        { decode_kv_cache_usage_percent: 93.6, prefill_kv_cache_usage_percent: 21.1 },
                    ],
                },
            }}
            analysisWindow="profiling"
        />,
    );
    assert.match(roleSeriesHtml, /93\.60%/);

    const aggHtml = renderToStaticMarkup(
        <OverviewTab
            task={{}}
            telemetry={{
                ...pdTelemetry,
                server_telemetry: {
                    ...pdTelemetry.server_telemetry,
                    topology_mode: 'aggregated',
                    per_pod: [{ pod: 'worker-0', role: 'worker', kv_cache_usage_percent: { max: 97.49 } }],
                    summary: { kv_cache_usage_percent: { max: 89.9 } },
                },
            }}
            analysisWindow="profiling"
        />,
    );
    assert.match(aggHtml, /89\.90%/);
});

test('RequestsTab uses active window CDF with legacy fallback and LatencyHeatmap reflects sequence_length_source', () => {
    const base = makeSliceTelemetry();
    const windowedTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            cdf: { quantiles: [50, 90], ttft_ms: [493.8, 900.1], tpot_ms: [18, 28], e2e_ms: [900, 1400], drift_ms: [1, 3] },
            windows: {
                profiling: {
                    ...base.client_series.windows.profiling,
                    ttft_ms: { p50: 1234.6, p90: 2500.0 },
                    cdf: { quantiles: [50, 90], ttft_ms: [1234.6, 2500.0], tpot_ms: [24, 39], e2e_ms: [1500, 2900], drift_ms: [2, 6] },
                },
                full: {
                    ...base.client_series.windows.full,
                    ttft_ms: { p50: 493.8, p90: 900.1 },
                    cdf: { quantiles: [50, 90], ttft_ms: [493.8, 900.1], tpot_ms: [18, 28], e2e_ms: [900, 1400], drift_ms: [1, 3] },
                },
            },
            heatmap: {
                ttft: {
                    sequence_length: 'input',
                    sequence_length_source: 'observed',
                    cells: [{ start_seconds: 0, end_seconds: 10, sequence_length_start: 100, sequence_length_end: 500, average_metric_ms: 120, request_count: 10 }],
                },
                tpot: {
                    sequence_length: 'output',
                    sequence_length_source: 'requested',
                    cells: [{ start_seconds: 0, end_seconds: 10, sequence_length_start: 64, sequence_length_end: 256, average_metric_ms: 22, request_count: 10 }],
                },
            },
        },
    };
    delete windowedTelemetry.client_series.ttft_heatmap;
    delete windowedTelemetry.client_series.tpot_heatmap;

    let reqProfTree = null;
    function CaptureReqProf() {
        reqProfTree = RequestsTab({ task: {}, telemetry: windowedTelemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureReqProf />);
    const profHtml = renderToStaticMarkup(<RequestsTab task={{}} telemetry={windowedTelemetry} analysisWindow="profiling" />);
    const cdfChart = collectElements(reqProfTree, (el) => el.type === ChartContainer && el.props.title === 'Cumulative Distribution Function (CDF)')[0];
    assert.equal(cdfChart.props.subtitle, 'Interactive percentile distribution (Profiling Steady-State)');
    const cdfLineChart = collectElements(cdfChart, (el) => Array.isArray(el.props?.data) && el.props.data[0]?.pct === 50)[0];
    assert.deepEqual(cdfLineChart.props.data, [
        { value: 1234.6, pct: 50 },
        { value: 2500.0, pct: 90 },
    ]);

    // Requested output sequence length propagates to RequestsTab TPOT heatmap subtitle and LatencyHeatmap axis/subtitle
    assert.match(profHtml, /Per-request TPOT \(ms\/tok\) by elapsed time and requested output sequence length \(tokens\)/);
    assert.match(profHtml, /Requested output sequence length \(tokens\)/);
    assert.match(profHtml, /Input sequence length \(tokens\)/);

    // Direct LatencyHeatmap scatter checks with points for requested, observed, and legacy omitted sequence_length_source
    const scatterRequestedHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TPOT by Time and Output Sequence Length"
            metric="TPOT"
            data={{
                sequence_length: 'output',
                sequence_length_source: 'requested',
                duration_seconds: 10,
                points: [{ t: 2.5, seq: 128, ms: 20 }],
            }}
        />,
    );
    assert.match(scatterRequestedHtml, /Per-request TPOT across elapsed time and requested output sequence length/);
    assert.match(scatterRequestedHtml, /Requested output sequence length \(tokens\)/);
    assert.match(scatterRequestedHtml, /TPOT per-request scatter plot by elapsed time and requested output sequence length/);

    // Legacy JSON without windows.profiling.cdf falls back cleanly to top-level clientSeries.cdf
    const legacyHtml = renderToStaticMarkup(<RequestsTab task={{}} telemetry={base} analysisWindow="profiling" />);
    assert.match(legacyHtml, /Full Run fallback — legacy data/);

    // Direct LatencyHeatmap checks for requested, observed, and legacy omitted sequence_length_source
    const requestedHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TPOT Heatmap"
            metric="TPOT"
            data={{
                sequence_length: 'output',
                sequence_length_source: 'requested',
                cells: [{ start_seconds: 0, end_seconds: 10, sequence_length_start: 32, sequence_length_end: 128, average_metric_ms: 20, request_count: 5 }],
            }}
        />,
    );
    assert.match(requestedHtml, /Average TPOT across elapsed time and requested output sequence length/);
    assert.match(requestedHtml, /Requested output sequence length \(tokens\)/);

    const legacyHeatmapHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TPOT Heatmap"
            metric="TPOT"
            data={{
                sequence_length: 'output',
                cells: [{ start_seconds: 0, end_seconds: 10, sequence_length_start: 32, sequence_length_end: 128, average_metric_ms: 20, request_count: 5 }],
            }}
        />,
    );
    assert.match(legacyHeatmapHtml, /Average TPOT across elapsed time and output sequence length/);
    assert.match(legacyHeatmapHtml, /Output sequence length \(tokens\)/);
});

test('RunCompareDrawer resolves P/D Peak KV, preserves missing goodput/error rate as dashes, and formats CompareTooltip values', async (t) => {
    const base = makeSliceTelemetry();
    const tasksById = {
        'run-1': { id: 'run-1', name: 'pd-run-missing-fields', status: 'completed' },
        'run-2': { id: 'run-2', name: 'agg-run-explicit-zero', status: 'completed' },
    };
    const stripSeries = (tel) => ({
        ...tel,
        client_series: { ...tel.client_series, buckets: [] },
        server_telemetry: { ...tel.server_telemetry, series: [], role_series: [] },
    });
    const telemetryByRun = {
        'run-1': stripSeries({
            ...base,
            forensics: {
                ...base.forensics,
                verdict: { code: 'kv_cache_eviction', severity: 'critical', throughput_rps: 5.5 },
            },
            client_series: {
                ...base.client_series,
                windows: {
                    profiling: {
                        throughput_rps: 5.5,
                        output_tok_per_s: 210,
                        ttft_ms: { p50: 400, p90: 800, p99: 1200 },
                        tpot_ms: { p50: 25, p90: 40, p99: 55 },
                    },
                },
            },
            server_telemetry: {
                ...base.server_telemetry,
                topology_mode: 'disaggregated',
                summary: { kv_cache_usage_percent: { max: 28.2 } },
                per_pod: [
                    { pod: 'decode-0', role: 'decode', kv_cache_usage_percent: { max: 97.5 } },
                    { pod: 'prefill-0', role: 'prefill', kv_cache_usage_percent: { max: 22.3 } },
                ],
            },
        }),
        'run-2': stripSeries({
            ...base,
            forensics: {
                ...base.forensics,
                verdict: { code: 'healthy', severity: 'healthy', throughput_rps: 10.0, goodput_rps: 9.8, attainment_pct: 98.0 },
            },
            client_series: {
                ...base.client_series,
                windows: {
                    profiling: {
                        throughput_rps: 10.0,
                        goodput_rps: 9.8,
                        error_rate_pct: 0,
                        output_tok_per_s: 450,
                        ttft_ms: { p50: 150, p90: 250, p99: 350 },
                        tpot_ms: { p50: 18, p90: 28, p99: 36 },
                    },
                },
            },
            server_telemetry: {
                ...base.server_telemetry,
                topology_mode: 'aggregated',
                summary: { gpu_cache_usage_percent: { max: 64.2 } },
                per_pod: [],
            },
        }),
    };

    await withSimulationDom(
        t,
        async (url) => {
            if (url.includes('/run-1/telemetry')) return Response.json(telemetryByRun['run-1']);
            if (url.includes('/run-2/telemetry')) return Response.json(telemetryByRun['run-2']);
            return Response.json({});
        },
        async ({ root, doc }) => {
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['run-1', 'run-2']}
                        tasksById={tasksById}
                    />,
                );
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 0));
            });

            const findRow = (label) => collectDomNodes(doc.body, (el) => el.tagName === 'TR' && el.textContent.includes(label))[0];
            const kvRow = findRow('Peak KV Cache Usage');
            assert.match(kvRow.textContent, /97\.50%/);
            assert.match(kvRow.textContent, /64\.20%/);

            const goodputRow = findRow('Goodput');
            const goodputCells = collectDomNodes(goodputRow, (el) => el.tagName === 'TD');
            assert.equal(goodputCells[1].textContent.trim(), '—');
            assert.match(goodputCells[2].textContent, /9\.80 req\/s/);

            const errRow = findRow('Error Rate');
            const errCells = collectDomNodes(errRow, (el) => el.tagName === 'TD');
            assert.equal(errCells[1].textContent.trim(), '—');
            assert.match(errCells[2].textContent, /0%/);
        },
    );

    const msTooltip = renderToStaticMarkup(
        <CompareTooltip
            active
            label={15}
            unit=" ms"
            payload={[
                { dataKey: 'run_0', name: 'Run A', color: 'var(--brand-accent)', value: 1660.6259 },
                { dataKey: 'run_1', name: 'Run B', color: 'var(--brand-accent)', value: null },
            ]}
        />,
    );
    assert.match(msTooltip, /1,660\.63 ms/);
    assert.match(msTooltip, /—/);
    assert.doesNotMatch(msTooltip, /1,660\.6259/);

    const tpotTooltip = renderToStaticMarkup(
        <CompareTooltip
            active
            label={15}
            unit=" ms/tok"
            payload={[{ dataKey: 'run_0', name: 'Run A', color: 'var(--brand-accent)', value: 24.367 }]}
        />,
    );
    assert.match(tpotTooltip, /24\.37 ms\/tok/);
});

test('createSimulationFormState preserves trajectory start ratios across fresh, zero, custom, unset, and legacy tasks', () => {
    const freshForm = createSimulationFormState();
    assert.equal(freshForm.trajectoryStartMinRatio, '0.25');
    assert.equal(freshForm.trajectoryStartMaxRatio, '0.75');

    const zeroForm = createSimulationFormState({
        simulation: {
            backend: 'aiperf',
            backend_options: { trajectory_start_min_ratio: 0, trajectory_start_max_ratio: 0.75 },
        },
    });
    assert.equal(zeroForm.trajectoryStartMinRatio, '0');
    assert.equal(zeroForm.trajectoryStartMaxRatio, '0.75');

    const customForm = createSimulationFormState({
        simulation: {
            backend: 'aiperf',
            backend_options: { trajectory_start_min_ratio: 0.1, trajectory_start_max_ratio: 0.9 },
        },
    });
    assert.equal(customForm.trajectoryStartMinRatio, '0.1');
    assert.equal(customForm.trajectoryStartMaxRatio, '0.9');

    const snapshotForm = createSimulationFormState({
        backend: 'aiperf',
        config_snapshot: {
            backend_options: { trajectory_start_min_ratio: 0.15, trajectory_start_max_ratio: 0.85 },
        },
    });
    assert.equal(snapshotForm.trajectoryStartMinRatio, '0.15');
    assert.equal(snapshotForm.trajectoryStartMaxRatio, '0.85');

    const unsetForm = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: {} },
    });
    assert.equal(unsetForm.trajectoryStartMinRatio, '');
    assert.equal(unsetForm.trajectoryStartMaxRatio, '');

    const nullAndEmptyForm = createSimulationFormState({
        simulation: {
            backend: 'aiperf',
            backend_options: { trajectory_start_min_ratio: null, trajectory_start_max_ratio: '   ' },
        },
    });
    assert.equal(nullAndEmptyForm.trajectoryStartMinRatio, '');
    assert.equal(nullAndEmptyForm.trajectoryStartMaxRatio, '');

    const legacyForm = createSimulationFormState({
        simulation: {
            backend: 'trace-replayer',
            backend_options: { trajectory_start_min_ratio: 0.25, trajectory_start_max_ratio: 0.75 },
        },
    });
    assert.equal(legacyForm.trajectoryStartMinRatio, '');
    assert.equal(legacyForm.trajectoryStartMaxRatio, '');
});

test('RunCompareDrawer formats deltaText percentages with integer-vs-2-decimal rule and near-zero guard', async (t) => {
    const base = makeSliceTelemetry();
    const tasksById = {
        'd-1': { id: 'd-1', name: 'base-run', status: 'completed' },
        'd-2': { id: 'd-2', name: 'cand-run', status: 'completed' },
    };
    const stripSeries = (tel) => ({
        ...tel,
        client_series: { ...tel.client_series, buckets: [] },
        server_telemetry: { ...tel.server_telemetry, series: [], role_series: [] },
    });
    const telemetryByRun = {
        'd-1': stripSeries({
            ...base,
            forensics: { ...base.forensics, verdict: { code: 'healthy', severity: 'healthy', throughput_rps: 10, goodput_rps: 100, attainment_pct: 100 } },
            client_series: {
                ...base.client_series,
                windows: {
                    profiling: {
                        throughput_rps: 10,
                        goodput_rps: 100,
                        output_tok_per_s: 1000,
                        ttft_ms: { p50: 100, p90: 200, p99: 300 },
                        tpot_ms: { p50: 20, p90: 30, p99: 40 },
                    },
                },
            },
        }),
        'd-2': stripSeries({
            ...base,
            forensics: { ...base.forensics, verdict: { code: 'healthy', severity: 'healthy', throughput_rps: 15, goodput_rps: 99.97, attainment_pct: 100.002 } },
            client_series: {
                ...base.client_series,
                windows: {
                    profiling: {
                        throughput_rps: 15,
                        goodput_rps: 99.97,
                        output_tok_per_s: 1125,
                        ttft_ms: { p50: 100.002, p90: 200, p99: 300 },
                        tpot_ms: { p50: 20, p90: 30, p99: 40 },
                    },
                },
            },
        }),
    };

    await withSimulationDom(
        t,
        async (url) => {
            if (url.includes('/d-1/telemetry')) return Response.json(telemetryByRun['d-1']);
            if (url.includes('/d-2/telemetry')) return Response.json(telemetryByRun['d-2']);
            return Response.json({});
        },
        async ({ root, doc }) => {
            await act(async () => {
                root.render(<RunCompareDrawer isOpen onClose={() => {}} taskIds={['d-1', 'd-2']} tasksById={tasksById} />);
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 0));
            });

            const findRow = (label) => collectDomNodes(doc.body, (el) => el.tagName === 'TR' && el.textContent.includes(label))[0];
            // +50% exact integer delta
            assert.match(findRow('Request Throughput').textContent, /\+50%/);
            // +12.50% float delta
            assert.match(findRow('Output Throughput').textContent, /\+12\.50%/);
            // -0.03% small negative float delta
            assert.match(findRow('Goodput').textContent, /-0\.03%/);
            // near-zero delta (< 0.005%) suppresses +/- pill
            const ttftRow = findRow('TTFT P50');
            assert.doesNotMatch(ttftRow.textContent, /[+-]0\.00%/);
        },
    );
});

test('SimulationDashboard Matching Tasks cards render TPOT in ms (with null dash fallback and ms tooltip copy)', async (t) => {
    await withSimulationDom(
        t,
        async (url) => {
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({
                    tasks: [
                        {
                            id: 'task-tpot-p50',
                            name: 'TPOT P50 Run',
                            status: 'completed',
                            result: {
                                summary: {
                                    throughput_rps: 4.5,
                                    ttft: { p50_ms: 120.5 },
                                    tpot: { p50_ms: 11.22 },
                                    total_requests: 100,
                                    failed_requests: 0,
                                },
                            },
                        },
                        {
                            id: 'task-tpot-avg',
                            name: 'TPOT Avg Run',
                            status: 'completed',
                            result: {
                                summary: {
                                    throughput_rps: 3.2,
                                    ttft: { mean_ms: 95.1 },
                                    tpot: { mean_ms: 14.5 },
                                    total_requests: 80,
                                    failed_requests: 0,
                                },
                            },
                        },
                        {
                            id: 'task-tpot-null',
                            name: 'TPOT Missing Run',
                            status: 'completed',
                            result: {
                                summary: {
                                    throughput_rps: 2.0,
                                    ttft: { p50_ms: 80 },
                                    total_requests: 50,
                                    failed_requests: 0,
                                },
                            },
                        },
                    ],
                    total: 3,
                    counts: { queued: 0, running: 0, completed: 3, failed: 0, cancelled: 0 },
                    facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] },
                });
            }
            if (url === '/api/v1/simulation/backends') return Response.json({ backends: [] });
            if (url === '/api/v1/simulation/scenarios') return Response.json({ scenarios: [] });
            if (url === '/api/v1/simulation/trace-datasets') return Response.json({ datasets: [] });
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ principal: { userId: 'system' }, can: () => true }}>
                        <SimulationDashboard />
                    </AuthContext.Provider>,
                );
            });
            await flush();
            await flush();

            assert.match(doc.body.textContent, /Matching Tasks/);
            const cards = collectDomNodes(doc.body, (el) => el.tagName === 'ARTICLE');
            assert.equal(cards.length, 3);

            const findTpotTile = (card) => collectDomNodes(
                card,
                (el) => el.tagName === 'DIV' && el.getAttribute('class')?.includes('sim-kpi-tile') && el.textContent.includes('TPOT'),
            )[0];

            const p50Tile = findTpotTile(cards[0]);
            assert.ok(p50Tile, 'Expected TPOT tile on P50 task card');
            assert.match(p50Tile.textContent, /TPOT P50/);
            assert.match(p50Tile.textContent, /11\.22 ms/);
            assert.doesNotMatch(p50Tile.textContent, /\/tok|ms\/tok/);
            assert.match(p50Tile.textContent, /TPOT P50 \(ms\) = Quantile_0\.50/);
            assert.match(p50Tile.textContent, /Median \(P50\) Time Per Output Token \(ms\)/);

            const avgTile = findTpotTile(cards[1]);
            assert.ok(avgTile, 'Expected TPOT tile on Avg task card');
            assert.match(avgTile.textContent, /TPOT Avg/);
            assert.match(avgTile.textContent, /14\.50 ms/);
            assert.doesNotMatch(avgTile.textContent, /\/tok|ms\/tok/);
            assert.match(avgTile.textContent, /TPOT \(ms\) = mean/);
            assert.match(avgTile.textContent, /Mean Time Per Output Token \(ms\)/);

            const nullTile = findTpotTile(cards[2]);
            assert.ok(nullTile, 'Expected TPOT tile on missing-TPOT task card');
            assert.match(nullTile.textContent, /TPOT P50/);
            const nullValue = collectDomNodes(nullTile, (el) => el.tagName === 'P')[0];
            assert.equal(nullValue.textContent.trim(), '—');
            assert.doesNotMatch(nullTile.textContent, /\/tok|ms\/tok/);
        },
    );
});

test('RequestsTab LatencyHeatmap formulas match per-request Point_i contract for both TTFT and TPOT', () => {
    const telemetry = makeSliceTelemetry();
    let reqTree = null;
    function CaptureReq() {
        reqTree = RequestsTab({ task: {}, telemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureReq />);
    const heatmaps = collectElements(reqTree, (el) => el.type === LatencyHeatmap);
    assert.equal(heatmaps.length, 2);
    const ttftHm = heatmaps.find((h) => h.props.metric === 'TTFT');
    const tpotHm = heatmaps.find((h) => h.props.metric === 'TPOT');
    assert.equal(ttftHm.props.title, 'TTFT by Time and Input Sequence Length');
    assert.equal(ttftHm.props.helpFormula, 'Point_i = (t_i, ISL_i, TTFT_i);  TTFT_i = t_first_token,i − t_request_start,i');
    assert.equal(tpotHm.props.title, 'TPOT by Time and Output Sequence Length');
    assert.equal(tpotHm.props.helpFormula, 'Point_i = (t_i, OSL_i, TPOT_i);  TPOT_i = (t_last_token,i − t_first_token,i) / max(1, Output_Tokens_i − 1)');
});

test('HardwareTab attaches formulas to all StatCards/Panels/HelpTooltips and stays free of brand or GPU leaks', () => {
    const base = makeSliceTelemetry();
    const hardwareTelemetry = {
        ...base,
        run_manifest: {
            target: {
                endpoint_mode: 'deployment',
                cluster_name: 'prod-cluster',
                namespace: 'llm-d-sim',
                deployment_name: 'qwen-pd',
                model_name: 'Qwen/Qwen3-32B',
                topology_mode: 'disaggregated',
            },
            hardware_profile: {
                id: 'intel-xpu',
                display_name: 'Intel Data Center Max',
                vendor: 'Intel',
                metric_group: 'XPU',
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'disaggregated',
            guide_type: 'pd-disaggregation',
            namespace: 'llm-d-sim',
            accelerator_summary: 'Intel Data Center Max 1550',
            hardware_profiles: [
                {
                    id: 'intel-xpu',
                    display_name: 'Intel Data Center Max',
                    vendor: 'Intel',
                    metric_group: 'XPU',
                    models: ['Intel Data Center Max 1550'],
                    allocated_accelerators: 4,
                },
            ],
            cache_config: {
                hbm_capacity_bytes: 512 * 1024 ** 3,
                hbm_capacity_tokens: 120000,
                prefill_hbm_capacity_tokens: 40000,
                decode_hbm_capacity_tokens: 80000,
                cpu_capacity_bytes: 256 * 1024 ** 3,
                cpu_capacity_tokens: 60000,
                nvme_capacity_bytes: 1024 ** 4,
                effective_capacity_tokens: 180000,
                block_size: 16,
                num_gpu_blocks: 7500,
                host_kv_configured: true,
                nvme_kv_configured: true,
                engines: [{ pod: 'prefill-0', role: 'prefill' }, { pod: 'decode-0', role: 'decode' }],
            },
            per_pod: [
                {
                    pod: 'prefill-0',
                    node: 'node-a',
                    role: 'prefill',
                    allocated_accelerators: 2,
                    cpu_request_cores: 8,
                    cpu_limit_cores: 16,
                    memory_request_bytes: 64 * 1024 ** 3,
                    memory_limit_bytes: 128 * 1024 ** 3,
                    gpu_memory_total_bytes: { mean: 256 * 1024 ** 3 },
                },
            ],
            host_metrics: {
                ...base.server_telemetry.host_metrics,
                per_node: [
                    {
                        ...base.server_telemetry.host_metrics.per_node[0],
                        cpu_capacity_cores: 64,
                        cpu_allocatable_cores: 62,
                        memory_capacity_bytes: 512 * 1024 ** 3,
                        accelerator_capacity: 8,
                        accelerator_allocatable: 8,
                        accelerator_model: 'Intel Data Center Max 1550',
                        architecture: 'amd64',
                        os_image: 'Ubuntu 24.04',
                        gpu_memory_total_bytes: 256 * 1024 ** 3,
                    },
                ],
            },
        },
    };
    hardwareTelemetry.server_telemetry.per_node = hardwareTelemetry.server_telemetry.host_metrics.per_node;
    const task = { endpoint_mode: 'deployment', model_name: 'Qwen/Qwen3-32B' };

    const html = renderToStaticMarkup(<HardwareTab task={task} telemetry={hardwareTelemetry} onSelectTab={() => {}} />);
    let tree = null;
    function CaptureHardware() {
        tree = HardwareTab({ task, telemetry: hardwareTelemetry, onSelectTab: () => {} });
        return null;
    }
    renderToStaticMarkup(<CaptureHardware />);

    const brandLeakRe = /\b(aiperf|vllm|sglang|trt-?llm|prometheus|GPU)\b/i;
    const tooltipBodies = extractTooltipBodies(html);
    assert.ok(tooltipBodies.length >= 20, `Expected comprehensive tooltips in HardwareTab, got ${tooltipBodies.length}`);
    for (const body of tooltipBodies) {
        assert.doesNotMatch(body, ENGINE_LEAK_RE, `Engine leak in HardwareTab tooltip: ${body}`);
        assert.doesNotMatch(body, brandLeakRe, `Brand/GPU leak in HardwareTab tooltip: ${body}`);
    }

    const statCards = collectElements(tree, (el) => el.type === StatCard);
    assert.equal(statCards.length, 9, 'Expected 5 Hardware Overview + 4 KV Cache Hierarchy StatCards');
    for (const card of statCards) {
        assert.equal(typeof card.props.help, 'string');
        assert.ok(card.props.help.trim().length > 0);
        assert.ok(typeof card.props.helpFormula === 'string' && card.props.helpFormula.trim().length > 0);
        assert.doesNotMatch(card.props.helpFormula, brandLeakRe);
        for (const detail of (card.props.details || [])) {
            assert.ok(typeof detail.formula === 'string' && detail.formula.trim().length > 0);
            assert.doesNotMatch(detail.formula, brandLeakRe);
        }
    }

    const panels = collectElements(tree, (el) => el.type === Panel);
    assert.equal(panels.length, 2);
    for (const panel of panels) {
        assert.ok(typeof panel.props.help === 'string' && panel.props.help.trim().length > 0);
        assert.ok(typeof panel.props.helpFormula === 'string' && panel.props.helpFormula.trim().length > 0);
        assert.doesNotMatch(panel.props.helpFormula, brandLeakRe);
    }

    const tooltips = collectElements(tree, (el) => el.type === HelpTooltip);
    for (const tip of tooltips) {
        if (tip.props.formula) {
            assert.doesNotMatch(tip.props.formula, brandLeakRe, `Brand/GPU leak in HelpTooltip formula ${tip.props.label}`);
        }
    }

    const hwSource = fs.readFileSync(path.join(HERE, 'HardwareTab.jsx'), 'utf8');
    assert.doesNotMatch(hwSource, brandLeakRe, 'Brand/GPU leak in HardwareTab.jsx source');
});
