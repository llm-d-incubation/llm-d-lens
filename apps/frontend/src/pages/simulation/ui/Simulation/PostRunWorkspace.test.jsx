import assert from 'node:assert/strict';
import test from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { Area, AreaChart, Bar, BarChart, ComposedChart, Label, Line, LineChart, ReferenceArea, ReferenceLine, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from 'recharts';
import { CHART_SERIES, ChartContainer, ChartLegend, ToggleGroup } from '../../../../shared/ui';
import {
    OverviewTab,
    buildColdComparisonBars,
    buildTimeSeriesPoints,
    computeOverviewLatencyYMax,
    resolveConfiguredConcurrency,
    resolveVisibleLatencyStats,
    toggleAllLatencyStats,
    toggleLatencyStatSelection,
} from './OverviewTab';
import { BottlenecksTab } from './BottlenecksTab';
import { HardwareTab, resolveCacheHierarchy, resolveHardwareSummary, sanitizeEndpointUrl } from './HardwareTab';
import { ServingTab, resolveEffectiveTokenLoadCv, resolveServingLatencySlo } from './ServingTab';
import { RequestsTab } from './RequestsTab';
import { SystemTab } from './SystemTab';
import {
    WorkloadTab,
    classifyIslOslPoints,
    computeNumericDomain,
    resolveWorkloadReplayConfig,
    resolveWorkloadWindowData,
} from './WorkloadTab';
import { LatencyHeatmap } from './LatencyHeatmap';
import { ConfigurationGroup, formatTaskPacing } from './ConfigurationGroup';
import {
    LiveMonitorPanel,
    classifyLogLine,
    classifyLogLines,
    isLogErrorOrWarning,
    stripAnsi,
} from './LiveMonitorPanel';
import { CompareTooltip, MAX_COMPARE_RUNS, OverlaidChart, RunCompareDrawer } from './RunCompareDrawer';
import SimulationDashboard, { createSimulationFormState } from '../SimulationDashboard';
import SimulationTaskDetails from '../SimulationTaskDetails';
import {
    buildRecordedFlowData,
    deriveLegacyTelemetryFromTask,
    isSafeHttpUrl,
} from '../../model/presentation';

function createTestDom() {
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
        get options() { return findElements(this, (el) => el.tagName === 'OPTION'); },
        get nextSibling() {
            if (!this.parentNode) return null;
            const index = this.parentNode.childNodes.indexOf(this);
            return index >= 0 ? this.parentNode.childNodes[index + 1] || null : null;
        },
        getContext() { return null; },
        offsetWidth: 640,
        offsetHeight: 320,
        clientWidth: 640,
        clientHeight: 320,
        getBoundingClientRect() {
            return { top: 0, left: 0, width: 640, height: 320, right: 640, bottom: 320 };
        },
        focus() {},
        blur() {},
        closest() { return null; },
        remove() { this.parentNode?.removeChild(this); },
    });
    const pendingTimers = new Set();
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
    return {
        doc,
        win,
        cleanupTimers() {
            for (const handle of pendingTimers) clearTimeout(handle);
            pendingTimers.clear();
        },
    };
}

function findElement(node, predicate) {
    if (!node) return undefined;
    if (Array.isArray(node)) {
        for (const item of node) {
            const match = findElement(item, predicate);
            if (match) return match;
        }
        return undefined;
    }
    if (typeof node !== 'object') return undefined;
    if (predicate(node)) return node;
    for (const value of [node.props?.actions, node.props?.children]) {
        const match = findElement(value, predicate);
        if (match) return match;
    }
    return undefined;
}

function findAllElements(node, predicate, acc = []) {
    if (!node) return acc;
    if (Array.isArray(node)) {
        for (const item of node) findAllElements(item, predicate, acc);
        return acc;
    }
    if (typeof node !== 'object') return acc;
    if (predicate(node)) acc.push(node);
    for (const value of [node.props?.actions, node.props?.children]) {
        findAllElements(value, predicate, acc);
    }
    return acc;
}

function findElements(root, predicate, acc = []) {
    if (!root || root.nodeType !== 1) return acc;
    if (predicate(root)) acc.push(root);
    for (const child of root.childNodes) findElements(child, predicate, acc);
    return acc;
}

function reactProps(node) {
    const key = Object.keys(node || {}).find((entry) => entry.startsWith('__reactProps$'));
    return key ? node[key] : {};
}

async function withMountedDom(t, fetchHandler, runScenario) {
    const prevAct = globalThis.IS_REACT_ACT_ENVIRONMENT;
    const prevDoc = globalThis.document;
    const prevWin = globalThis.window;
    const prevHtml = globalThis.HTMLElement;
    const prevSvg = globalThis.SVGElement;
    const prevFetch = globalThis.fetch;
    const prevResizeObserver = globalThis.ResizeObserver;
    const prevGetComputedStyle = globalThis.getComputedStyle;

    const { doc, win, cleanupTimers } = createTestDom();
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

    t.after(async () => {
        cleanupTimers();
        await act(async () => { root.unmount(); });
        cleanupTimers();
        globalThis.IS_REACT_ACT_ENVIRONMENT = prevAct;
        if (prevDoc === undefined) delete globalThis.document; else globalThis.document = prevDoc;
        if (prevWin === undefined) delete globalThis.window; else globalThis.window = prevWin;
        if (prevHtml === undefined) delete globalThis.HTMLElement; else globalThis.HTMLElement = prevHtml;
        if (prevSvg === undefined) delete globalThis.SVGElement; else globalThis.SVGElement = prevSvg;
        globalThis.fetch = prevFetch;
        if (prevGetComputedStyle === undefined) delete globalThis.getComputedStyle; else globalThis.getComputedStyle = prevGetComputedStyle;
        if (prevResizeObserver === undefined) delete globalThis.ResizeObserver; else globalThis.ResizeObserver = prevResizeObserver;
    });

    const findButton = (labelPattern) => findElements(
        doc.body,
        (el) => el.tagName === 'BUTTON' && labelPattern.test(el.textContent.trim()),
    )[0] || null;

    const clickButton = async (labelPattern) => {
        const button = findButton(labelPattern);
        assert.ok(button, `Expected button matching ${labelPattern}`);
        const props = reactProps(button);
        assert.equal(Boolean(props.disabled), false, `Expected button ${labelPattern} to be enabled`);
        await act(async () => {
            await props.onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
    };

    const flush = async () => {
        await act(async () => {
            await new Promise((resolve) => setTimeout(resolve, 0));
        });
    };

    await runScenario({ root, doc, findButton, clickButton, flush });
}

function makeRichTask(overrides = {}) {
    return {
        id: 'sim-post-1',
        name: 'qwen-replay-1',
        status: 'completed',
        simulation: {
            duration_seconds: 120,
            backend_options: {
                warmup_seconds: 15,
                ttft_slo: 0.5,
                tpot_slo: 0.05,
                e2e_slo: 4.0,
            },
        },
        ...overrides,
    };
}

function makeRichTelemetry(overrides = {}) {
    return {
        telemetry_scope: 'full',
        run_manifest: { execution_started_at: '2026-01-01T00:00:00Z' },
        forensics: {
            telemetry_scope: 'full',
            verdict: {
                code: 'healthy',
                severity: 'healthy',
                reason: 'All SLOs met during steady-state profiling window.',
                attainment_pct: 96.5,
                throughput_rps: 14.2,
                goodput_rps: 13.7,
                slo: { ttft_s: 0.5, tpot_ms: 50 },
                evidence: [
                    { text: 'TTFT P90 stayed below 500 ms across steady state', target_tab: 'requests' },
                    { text: 'Decode pods showed mild queue growth at 80s', target_tab: 'bottlenecks' },
                ],
            },
            findings: [
                {
                    code: 'DECODE_QUEUE_SPIKE',
                    severity: 'warning',
                    title: 'Decode queue depth rose at 80s',
                    summary: 'Waiting requests peaked at 14 on decode-0.',
                    recommendation: 'Increase decode replica count or lower max-num-seqs.',
                    tab: 'bottlenecks',
                },
                {
                    code: 'KV_PRESSURE_HIGH',
                    severity: 'critical',
                    title: 'KV cache pressure exceeded 90%',
                    summary: 'KV cache peaked at 92.4% on prefill-0.',
                    recommendation: 'Enable KV offloading or reduce max model length.',
                    tab: 'serving',
                },
            ],
            timeline_buckets: [
                { bucket_index: 0, start_seconds: 0, end_seconds: 60, role: 'epp', state: 'healthy', running: 4, waiting: 0 },
                { bucket_index: 0, start_seconds: 0, end_seconds: 60, role: 'decode', state: 'queueing', running: 8, waiting: 12, kv_cache_pct: 74 },
                { bucket_index: 1, start_seconds: 60, end_seconds: 120, role: 'decode', state: 'evicting', running: 10, waiting: 18, kv_cache_pct: 92, preemptions: 3 },
            ],
            ttft_decomposition: {
                vllm_prefill_p50_ms: 95,
                vllm_queue_p50_ms: 35,
                overhead_p50_ms: 15,
                vllm_prefill_p90_ms: 210,
                vllm_queue_p90_ms: 110,
                overhead_p90_ms: 25,
            },
            pod_skew: {
                skew_ratio: 2.4,
                pods: [
                    { pod: 'qwen-prefill-0', role: 'prefill', request_rate_rps: 8.5, avg_waiting: 1.2, peak_kv_pct: 61, prefix_hit_pct: 52 },
                    { pod: 'qwen-decode-0', role: 'decode', request_rate_rps: 14.2, avg_waiting: 6.8, peak_kv_pct: 92.4, prefix_hit_pct: 38 },
                ],
            },
            routing_vs_cache: {
                epp_expected_hit_pct: 64.0,
                vllm_actual_hit_pct: 48.5,
                gap_pct: 15.5,
                engine_cached_tok_s: 3200,
                engine_fresh_tok_s: 1800,
                series: [
                    { time_seconds: 10, epp_cache_hit_pct: 62, vllm_prefix_hit_pct: 47 },
                    { time_seconds: 20, epp_cache_hit_pct: 66, vllm_prefix_hit_pct: 50 },
                ],
            },
        },
        client_series: {
            warmup_seconds: 15,
            windows: {
                profiling: {
                    total_requests: 1200,
                    successful_requests: 1188,
                    failed_requests: 12,
                    good_requests: 1158,
                    throughput_rps: 14.2,
                    output_tok_per_s: 710.5,
                    error_rate_pct: 1.0,
                    ttft_ms: { avg: 140, min: 35, p50: 130, p90: 290, p95: 360, p99: 480, max: 620 },
                    tpot_ms: { avg: 22, min: 8, p50: 20, p90: 38, p95: 44, p99: 52, max: 70 },
                    e2e_ms: { avg: 1200, min: 250, p50: 1100, p90: 2300, p95: 2700, p99: 3400, max: 4100 },
                    duration_seconds: 105,
                },
                full: {
                    total_requests: 1350,
                    successful_requests: 1335,
                    failed_requests: 15,
                    throughput_rps: 12.8,
                    output_tok_per_s: 640.0,
                    error_rate_pct: 1.11,
                    ttft_ms: { p50: 155, p90: 340, p99: 590 },
                    tpot_ms: { p50: 24, p90: 41, p99: 60 },
                    duration_seconds: 120,
                },
                warmup: {
                    total_requests: 150,
                    ttft_ms: { p50: 380, p90: 710 },
                    duration_seconds: 15,
                },
            },
            cold_start_comparison: {
                mode: 'auto_10_percent',
                cold_to_steady_ratio: 2.92,
                warmup: { request_count: 135, duration_seconds: 12, ttft_ms: { p50: 380, p90: 710 } },
                steady_state: { request_count: 1215, duration_seconds: 108, ttft_ms: { p50: 130, p90: 290 } },
            },
            buckets: [
                { time_seconds: 10, target_arrival_rps: 14.0, actual_send_rps: 13.8, arrived_rps: 14.0, completed_rps: 13.5, goodput_rps: 13.1, output_tps: 680, ttft_p50_ms: 140, ttft_p90_ms: 300, s_time_drift_p50_ms: 2.5, s_time_drift_p90_ms: 8.1, client_inflight: 12 },
                { time_seconds: 20, target_arrival_rps: 14.5, actual_send_rps: 14.4, arrived_rps: 14.5, completed_rps: 14.2, goodput_rps: 13.7, output_tps: 710, ttft_p50_ms: 130, ttft_p90_ms: 290, s_time_drift_p50_ms: 1.8, s_time_drift_p90_ms: 6.4, client_inflight: 14 },
            ],
            heatmap: {
                ttft: {
                    cells: [
                        { start_seconds: 0, end_seconds: 60, seq_start: 0, seq_end: 1024, avg_ms: 120, count: 400 },
                        { start_seconds: 60, end_seconds: 120, seq_start: 1024, seq_end: 4096, avg_ms: 280, count: 800 },
                    ],
                },
                tpot: {
                    cells: [
                        { start_seconds: 0, end_seconds: 60, seq_start: 0, seq_end: 512, avg_ms: 19, count: 400 },
                    ],
                },
            },
            error_summary: {
                total_errors: 12,
                counts_by_code: { all: 12, '5xx': 9, 503: 9, timeout: 3 },
            },
        },
        sessions_summary: {
            available: true,
            session_ids: ['chat-alpha', 'chat-beta', 'chat-gamma'],
            correlation: { matched_count: 1100, total_count: 1200, matched_ratio: 0.916 },
            lanes: [
                [0, 1.0, 28.0, 2, 110],
                [1, 5.0, 42.0, 1, 185],
                [2, 10.0, 55.0, 0, 340],
            ],
        },
        server_telemetry: {
            available: true,
            status: 'available',
            topology_mode: 'disaggregated',
            namespace: 'llm-d-sim',
            hardware_profile: {
                display_name: 'Intel Data Center GPU Max',
                metric_group: 'XPU',
            },
            energy: {
                accelerator_avg_power_watts: 310,
                accelerator_peak_power_watts: 420,
                accelerator_energy_wh: 10.3,
                host_avg_power_watts: 245,
            },
            summary: {
                kv_cache_usage_percent: { max: 92.4, mean: 72.1 },
                prefix_cache_hit_percent: { mean: 48.5 },
                gpu_power_watts: { mean: 310, max: 420 },
                gpu_utilization_percent: { mean: 81.2, max: 96.0 },
                epp_inflight_requests: { mean: 12, max: 20 },
            },
            per_pod: [
                { pod: 'qwen-prefill-0', role: 'prefill', request_rate_rps: 8.5, output_tps: 0, running_requests: 4, waiting_requests: 1, kv_cache_usage_percent: 61 },
                { pod: 'qwen-decode-0', role: 'decode', request_rate_rps: 14.2, output_tps: 710, running_requests: 8, waiting_requests: 6, kv_cache_usage_percent: 92.4 },
            ],
            host_metrics: {
                nodes: ['worker-node-01'],
                cpu_avg_pct: 44.5,
                cpu_peak_pct: 68.0,
                memory_available_gb: { mean: 180.5 },
                memory_total_gb: 256,
                disk_free_gb: { min: 420.0 },
                disk_read_mbps: { mean: 125.4 },
                disk_write_mbps: { mean: 48.2 },
                power_watts: { mean: 245 },
                series: [
                    { elapsed_seconds: 10, host_cpu_usage_percent: 42, host_memory_available_gb: 182, host_disk_read_mbps: 120, host_disk_write_mbps: 45, host_power_watts: 240, gpu_power_watts: 300 },
                ],
            },
            grafana_links: [
                { label: 'vLLM Overview', url: 'https://grafana.example.com/d/vllm' },
                { label: 'Local Dashboard', url: 'http://localhost:3000/d/local' },
                { label: 'Blocked JS', url: 'javascript:alert(1)' },
                { label: 'Blocked Data', url: 'data:text/html,<script>alert(1)</script>' },
            ],
            series: [
                { elapsed_seconds: 10, running_requests: 12, waiting_requests: 3, kv_cache_usage_percent: 70, gpu_utilization_percent: 80, gpu_power_watts: 305 },
            ],
        },
        ...overrides,
    };
}

test('OverviewTab renders Verdict Hero Card, SLO & Goodput, Warmup vs Steady-State, Cold Start comparison, and Accelerator Power', () => {
    const selectedTabs = [];
    const openedErrors = [];

    const fullHtml = renderToStaticMarkup(
        <OverviewTab
            task={makeRichTask()}
            telemetry={makeRichTelemetry()}
            analysisWindow="full"
            onSelectTab={(tab) => selectedTabs.push(tab)}
            onOpenErrorInspector={(f) => openedErrors.push(f)}
        />,
    );
    assert.match(fullHtml, /Window Metrics · Full Run/);
    assert.match(fullHtml, /Warmup TTFT P50: 380 ms/);
    assert.match(fullHtml, /SLO Attainment/);
    assert.match(fullHtml, /96\.50%/);
    assert.match(fullHtml, /Goodput/);
    assert.match(fullHtml, /13\.70 req\/s/);
    assert.match(fullHtml, /Auto 10% warmup check/);
    assert.match(fullHtml, /Warmup \/ Steady-State: 2\.92x/);
    assert.match(fullHtml, /XPU Power &amp; Energy/);
    assert.match(fullHtml, /310 W/);
    assert.match(fullHtml, /10\.30 Wh/);

    // Configured warmup mode
    const configuredHtml = renderToStaticMarkup(
        <OverviewTab
            task={makeRichTask()}
            telemetry={makeRichTelemetry({
                client_series: {
                    ...makeRichTelemetry().client_series,
                    cold_start_comparison: {
                        mode: 'configured_warmup',
                        cold_to_steady_ratio: 1.4,
                        warmup: { request_count: 150, duration_seconds: 15, ttft_ms: { p50: 210, p90: 410 } },
                        steady_state: { request_count: 1050, duration_seconds: 105, ttft_ms: { p50: 150, p90: 290 } },
                    },
                },
            })}
            analysisWindow="profiling"
        />,
    );
    assert.match(configuredHtml, /Window Metrics · Profiling Steady-State/);
    assert.match(configuredHtml, /Configured 15 s warmup/);

    // Interactive evidence links via Capture + findElement
    let overviewTree = null;
    function CaptureOverview() {
        overviewTree = OverviewTab({
            task: makeRichTask(),
            telemetry: makeRichTelemetry(),
            analysisWindow: 'profiling',
            onSelectTab: (tab) => selectedTabs.push(tab),
            onOpenErrorInspector: (f) => openedErrors.push(f),
        });
        return null;
    }
    renderToStaticMarkup(<CaptureOverview />);
    const ev1 = findElement(overviewTree, (n) => n.type === 'button' && /TTFT P90 stayed below 500 ms/.test(renderToStaticMarkup(n)));
    const ev2 = findElement(overviewTree, (n) => n.type === 'button' && /Decode pods showed mild queue growth/.test(renderToStaticMarkup(n)));
    assert.ok(ev1 && ev2, 'Expected clickable evidence buttons');
    ev1.props.onClick();
    ev2.props.onClick();
    assert.deepEqual(selectedTabs, ['requests', 'bottlenecks']);
});

test('BottlenecksTab renders ranked findings, quantitative Per-Role Saturation Timeline, side-by-side cache charts, flat ttft_decomposition, pod_skew, and routing_vs_cache', () => {
    const richTelemetry = makeRichTelemetry();
    const html = renderToStaticMarkup(<BottlenecksTab task={makeRichTask()} telemetry={richTelemetry} />);
    assert.match(html, /KV cache pressure exceeded 90%/);
    assert.match(html, /Decode queue depth rose at 80s/);
    // Critical finding appears before warning finding
    assert.ok(html.indexOf('KV cache pressure exceeded 90%') < html.indexOf('Decode queue depth rose at 80s'));
    // EPP summary chip + quantitative Decode Role card (no cryptic OK/Q/KV/EV/ER button table)
    assert.match(html, /EPP Router: Healthy \(0 failed\)/);
    assert.match(html, /Mean EPP load: 4/);
    assert.match(html, /Decode Role/);
    assert.match(html, /Peak Waiting:/);
    assert.match(html, /18 reqs/);
    assert.match(html, /Peak KV Cache:/);
    assert.match(html, /92%/);
    assert.match(html, /Preemptions:/);
    assert.match(html, /Queueing.*50%/);
    assert.match(html, /Evicting.*50%/);
    assert.doesNotMatch(html, /simulation-bucket-cell/);
    // Flat ttft_decomposition + pod_skew + routing_vs_cache
    assert.match(html, /TTFT Stage Decomposition/);
    assert.match(html, /Skew Ratio: 2\.40x/);
    assert.match(html, /qwen-prefill-0/);
    assert.match(html, /qwen-decode-0/);
    assert.match(html, /EPP Predicted/);
    assert.match(html, /64%/);
    assert.match(html, /vLLM Actual/);
    assert.match(html, /48\.50%/);
    assert.match(html, /15\.50%/);

    // Inspect React tree for ComposedChart, dual axes, ReferenceArea bands, ReferenceLine, RoleSaturationTooltip, and t80 side-by-side grid
    let bottlenecksTree = null;
    function CaptureBottlenecks() {
        bottlenecksTree = BottlenecksTab({
            task: makeRichTask(),
            telemetry: {
                ...richTelemetry,
                client_series: {
                    ...richTelemetry.client_series,
                    warmup_seconds: 15,
                    dispatch_duration_seconds: 100,
                    duration_seconds: 120,
                },
            },
        });
        return null;
    }
    renderToStaticMarkup(<CaptureBottlenecks />);

    // Single engine role (Decode) uses full-width grid-cols-1
    const singleRoleGrid = findElement(
        bottlenecksTree,
        (n) => n.type === 'div' && typeof n.props?.className === 'string' && n.props.className.includes('grid-cols-1') && Boolean(findElement(n, (c) => c.type === ComposedChart)),
    );
    assert.ok(singleRoleGrid, 'Expected full-width grid-cols-1 for single engine role');

    const satChart = findElement(
        bottlenecksTree,
        (n) => n.type === ComposedChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'waiting')),
    );
    assert.ok(satChart, 'Expected dual-axis ComposedChart for Decode Role');
    const yAxes = findAllElements(satChart, (n) => n.type === YAxis);
    assert.equal(yAxes.length, 2);
    assert.equal(yAxes[0].props.yAxisId, 'left');
    assert.equal(yAxes[1].props.yAxisId, 'right');
    assert.equal(yAxes[1].props.orientation, 'right');
    assert.deepEqual(yAxes[1].props.domain, [0, 100]);

    const refAreas = findAllElements(satChart, (n) => n.type === ReferenceArea);
    assert.equal(refAreas.length, 2);
    assert.equal(refAreas[0].props.x1, 0);
    assert.equal(refAreas[0].props.x2, 60);
    assert.equal(refAreas[0].props.fill, CHART_SERIES[1]);
    assert.equal(refAreas[1].props.x1, 60);
    assert.equal(refAreas[1].props.x2, 120);
    assert.equal(refAreas[1].props.fill, CHART_SERIES[4]);

    const refLines = findAllElements(satChart, (n) => n.type === ReferenceLine);
    assert.ok(refLines.some((r) => r.props.y === 90 && r.props.yAxisId === 'right' && r.props.label?.value === 'KV 90%'));
    assert.ok(refLines.some((r) => r.props.x === 15 && r.props.label?.value === 'Warmup' && r.props.label?.position === 'insideTopRight'));
    assert.ok(refLines.some((r) => r.props.x === 100 && r.props.label?.value === 'Drain' && r.props.label?.position === 'insideTopLeft'));

    const satTooltip = findElement(satChart, (n) => n.type === Tooltip);
    const satTipHtml = renderToStaticMarkup(
        satTooltip.props.content.type({
            active: true,
            label: 90,
            payload: [
                {
                    dataKey: 'running',
                    payload: {
                        label: '60–120s',
                        status: 'evicting',
                        running: 10,
                        waiting: 18,
                        kv_pct: 92,
                        prefix_hit_pct: 48,
                        preemptions: 3,
                        failed_requests: 1,
                    },
                },
            ],
        }),
    );
    assert.match(satTipHtml, /60–120s/);
    assert.match(satTipHtml, /Evicting/);
    assert.match(satTipHtml, /Running Requests/);
    assert.match(satTipHtml, /Waiting Requests/);
    assert.match(satTipHtml, /KV Cache Usage \(%\)/);
    assert.match(satTipHtml, /92%/);
    assert.match(satTipHtml, /Preemptions/);
    assert.match(satTipHtml, /Failed Requests/);

    // t80: Multi-Turn Prefix Cache Reuse + EPP Predicted vs vLLM Actual share a single lg:grid-cols-2 row
    const cacheRowGrid = findElement(
        bottlenecksTree,
        (n) => n.type === 'div' && typeof n.props?.className === 'string' && n.props.className.includes('lg:grid-cols-2'),
    );
    assert.ok(cacheRowGrid, 'Expected t80 lg:grid-cols-2 row wrapper');
    const rowCharts = findAllElements(cacheRowGrid, (n) => n.type === ChartContainer);
    assert.equal(rowCharts.length, 2, 'Expected both cache ChartContainers inside the same lg:grid-cols-2 grid');
    assert.equal(rowCharts[0].props.title, 'Multi-Turn Prefix Cache Reuse');
    assert.equal(rowCharts[0].props.helpAlign, 'start');
    assert.match(rowCharts[0].props.className, /\bmin-w-0\b/);
    assert.equal(rowCharts[1].props.title, 'EPP Predicted vs vLLM Actual Prefix Cache Hit Rate');
    assert.equal(rowCharts[1].props.helpAlign, 'end');
    assert.match(rowCharts[1].props.className, /\bmin-w-0\b/);

    // Disaggregated P/D (2 engine roles) renders side-by-side xl:grid-cols-2 + contiguous ReferenceArea merging
    let pdTree = null;
    function CapturePdBottlenecks() {
        pdTree = BottlenecksTab({
            telemetry: makeRichTelemetry({
                forensics: {
                    ...richTelemetry.forensics,
                    timeline_buckets: [
                        { bucket_index: 0, start_seconds: 0, end_seconds: 30, role: 'epp', state: 'healthy', running: 2 },
                        { bucket_index: 0, start_seconds: 0, end_seconds: 30, role: 'prefill', state: 'queueing', running: 5, waiting: 4, kv_cache_pct: 45 },
                        { bucket_index: 1, start_seconds: 30, end_seconds: 60, role: 'prefill', state: 'queueing', running: 6, waiting: 7, kv_cache_pct: 55 },
                        { bucket_index: 0, start_seconds: 0, end_seconds: 30, role: 'decode', state: 'healthy', running: 8, waiting: 0, kv_cache_pct: 60 },
                        { bucket_index: 1, start_seconds: 30, end_seconds: 60, role: 'decode', state: 'kv_pressure', running: 10, waiting: 2, kv_cache_pct: 94 },
                    ],
                },
            }),
        });
        return null;
    }
    renderToStaticMarkup(<CapturePdBottlenecks />);
    const pdHtml = renderToStaticMarkup(pdTree);
    assert.match(pdHtml, /Prefill Role/);
    assert.match(pdHtml, /Decode Role/);
    const pdGrid = findElement(
        pdTree,
        (n) => n.type === 'div' && typeof n.props?.className === 'string' && n.props.className.includes('xl:grid-cols-2') && Boolean(findElement(n, (c) => c.type === ComposedChart)),
    );
    assert.ok(pdGrid, 'Expected xl:grid-cols-2 grid for disaggregated Prefill + Decode roles');
    const pdCharts = findAllElements(pdGrid, (n) => n.type === ComposedChart);
    assert.equal(pdCharts.length, 2);
    // Consecutive queueing buckets [0,30] + [30,60] on Prefill merge into 1 contiguous ReferenceArea [0, 60]
    const prefillBands = findAllElements(pdCharts[0], (n) => n.type === ReferenceArea);
    assert.equal(prefillBands.length, 1);
    assert.equal(prefillBands[0].props.x1, 0);
    assert.equal(prefillBands[0].props.x2, 60);

    // Legacy nested { roles: { ... } } format + worker role label
    const legacyHtml = renderToStaticMarkup(
        <BottlenecksTab
            telemetry={makeRichTelemetry({
                forensics: {
                    ...richTelemetry.forensics,
                    timeline_buckets: [
                        {
                            bucket_index: 0,
                            start_seconds: 0,
                            end_seconds: 30,
                            roles: {
                                worker: { status: 'healthy', running: 6, waiting: 0, kv_pct: 38, preemptions: 0 },
                            },
                        },
                    ],
                },
            })}
        />,
    );
    assert.match(legacyHtml, /Aggregated Worker/);
    assert.match(legacyHtml, /Healthy.*100%/);

    // Client-only fallback when Prometheus unavailable (client is sole role)
    const clientFallbackHtml = renderToStaticMarkup(
        <BottlenecksTab
            telemetry={makeRichTelemetry({
                forensics: {
                    ...richTelemetry.forensics,
                    timeline_buckets: [
                        { bucket_index: 0, start_seconds: 0, end_seconds: 30, role: 'client', state: 'healthy', failed_requests: 0 },
                        { bucket_index: 1, start_seconds: 30, end_seconds: 60, role: 'client', state: 'error', failed_requests: 4 },
                    ],
                },
            })}
        />,
    );
    assert.match(clientFallbackHtml, /Client-Only Saturation Fallback/);
    assert.match(clientFallbackHtml, /Server queue depth and KV cache saturation telemetry require Prometheus/);
    assert.match(clientFallbackHtml, /Failed Requests: 4/);

    // Empty timeline_buckets & empty routing_vs_cache state
    const emptyHtml = renderToStaticMarkup(
        <BottlenecksTab telemetry={{ forensics: {}, server_telemetry: {} }} />,
    );
    assert.match(emptyHtml, /No critical bottlenecks detected/);
    assert.match(emptyHtml, /Per-role saturation timeline unavailable/);
    assert.match(emptyHtml, /EPP vs vLLM cache comparison unavailable/);

    // Degenerate 1-bucket start_seconds === end_seconds (0..0s) & string bucket_index: "0"
    let degenerateTree = null;
    function CaptureDegenerateBottlenecks() {
        degenerateTree = BottlenecksTab({
            telemetry: makeRichTelemetry({
                forensics: {
                    ...richTelemetry.forensics,
                    timeline_buckets: [
                        {
                            bucket_index: '0',
                            start_seconds: 0,
                            end_seconds: 0,
                            role: 'worker',
                            state: 'queueing',
                            running: 3,
                            waiting: 2,
                            kv_cache_pct: 40,
                        },
                    ],
                },
            }),
        });
        return null;
    }
    renderToStaticMarkup(<CaptureDegenerateBottlenecks />);
    const degenerateChart = findElement(
        degenerateTree,
        (n) => n.type === ComposedChart && Boolean(findElement(n, (c) => c.type === ReferenceArea)),
    );
    assert.ok(degenerateChart, 'Expected ComposedChart for 1-bucket start_seconds === end_seconds');
    const degXAxis = findElement(degenerateChart, (n) => n.props?.dataKey === 't');
    assert.deepEqual(degXAxis.props.domain, [0, 1]);
    const degBand = findElement(degenerateChart, (n) => n.type === ReferenceArea);
    assert.equal(degBand.props.x1, 0);
    assert.equal(degBand.props.x2, 1);

    // String bucket_index without start_seconds/end_seconds must coerce numerically (not "01" / "00.5")
    let stringIdxTree = null;
    function CaptureStringIdxBottlenecks() {
        stringIdxTree = BottlenecksTab({
            telemetry: makeRichTelemetry({
                forensics: {
                    ...richTelemetry.forensics,
                    timeline_buckets: [
                        { bucket_index: '0', role: 'worker', state: 'healthy', running: 2, waiting: 0 },
                    ],
                },
            }),
        });
        return null;
    }
    renderToStaticMarkup(<CaptureStringIdxBottlenecks />);
    const stringIdxHtml = renderToStaticMarkup(stringIdxTree);
    const strChart = findElement(stringIdxTree, (n) => n.type === ComposedChart);
    assert.equal(strChart.props.data[0].t, 0.5);
    assert.equal(strChart.props.data[0].start_seconds, 0);
    assert.equal(strChart.props.data[0].end_seconds, 1);
    assert.equal(strChart.props.data[0].label, 'B1');
    assert.doesNotMatch(stringIdxHtml, /B01/);
});

test('ServingTab builds recorded FlowMap data, filters Grafana links with isSafeHttpUrl, and handles unavailable Prometheus', () => {
    assert.equal(isSafeHttpUrl('https://grafana.example.com/d/1'), true);
    assert.equal(isSafeHttpUrl('http://localhost:3000/d/1'), true);
    assert.equal(isSafeHttpUrl('javascript:alert(1)'), false);
    assert.equal(isSafeHttpUrl('data:text/html,hi'), false);
    assert.equal(isSafeHttpUrl(''), false);

    const flowData = buildRecordedFlowData(makeRichTelemetry().server_telemetry);
    assert.ok(flowData);
    assert.ok(flowData.components.some((c) => c.role === 'prefill'));
    assert.ok(flowData.components.some((c) => c.role === 'decode'));

    const baseRich = makeRichTelemetry();
    const html = renderToStaticMarkup(
        <ServingTab
            telemetry={{
                ...baseRich,
                task: { model_name: 'Qwen/Qwen3-32B' },
                capabilities: { server_telemetry_note: 'Direct scrape active' },
                server_telemetry: {
                    ...baseRich.server_telemetry,
                    window: {
                        start: '2026-04-01T10:00:00Z',
                        end: '2026-04-01T10:02:00Z',
                    },
                },
            }}
        />,
    );
    assert.match(html, /role="tablist" aria-label="Serving sub-categories"/);
    assert.match(html, /qwen-prefill-0/);
    assert.match(html, /qwen-decode-0/);
    assert.doesNotMatch(html, /Prefill: qwen-prefill-0/);
    assert.doesNotMatch(html, /Decode: qwen-decode-0/);
    assert.doesNotMatch(html, /Topology mode help/);
    assert.doesNotMatch(html, /Window:/);
    assert.doesNotMatch(html, /Window \[/);
    assert.doesNotMatch(html, /2026-04-01T10:00:00Z/);
    assert.doesNotMatch(html, /Telemetry source:/);
    assert.doesNotMatch(html, /Topology:/);
    assert.doesNotMatch(html, /Namespace:/);
    assert.doesNotMatch(html, /Model:/);
    assert.doesNotMatch(html, /Direct scrape active/);
    assert.match(html, /Recorded Deployment Flow Map/);
    assert.match(html, /Cluster Grafana dashboards scoped to this simulation run/);
    assert.match(html, /Requests/);
    assert.match(html, /Tokens/);
    assert.match(html, /https:\/\/grafana\.example\.com\/d\/vllm/);
    assert.match(html, /http:\/\/localhost:3000\/d\/local/);
    assert.doesNotMatch(html, /javascript:alert/);
    assert.doesNotMatch(html, /data:text\/html/);

    // Toggle Requests -> Tokens via Capture + renderToStaticMarkup
    let capturedServingTree = null;
    function CaptureServing() {
        capturedServingTree = ServingTab({ telemetry: makeRichTelemetry() });
        return null;
    }
    renderToStaticMarkup(<CaptureServing />);
    assert.ok(capturedServingTree, 'Expected ServingTab element tree');
    assert.match(html, /metric="request"|Requests/);

    // External / no-Prometheus empty state (no empty header wrapper div above EmptyState)
    const unavailableHtml = renderToStaticMarkup(
        <ServingTab telemetry={{ server_telemetry: { available: false, status: 'unavailable' } }} />,
    );
    assert.match(unavailableHtml, /Server telemetry unavailable/);
    assert.match(unavailableHtml, /Endpoint is external or Prometheus is not configured/);
    assert.doesNotMatch(unavailableHtml, /Telemetry source:/);
    assert.doesNotMatch(unavailableHtml, /space-y-3/);
    assert.doesNotMatch(unavailableHtml, /role="tablist" aria-label="Serving sub-categories"/);
});

test('RequestsTab renders replay fidelity, normalized LatencyHeatmap, Bailian session lanes vs sessionless note, and error drill-down', () => {
    const html = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask()}
            telemetry={makeRichTelemetry()}
            analysisWindow="profiling"
            onOpenErrorInspector={() => {}}
        />,
    );
    assert.match(html, /Trace Replay Fidelity · Rate/);
    assert.match(html, /Schedule Drift \(ms\)/);
    assert.match(html, /TTFT by Time and Input Sequence Length/);
    assert.match(html, /TPOT by Time and Output Sequence Length/);
    assert.match(html, /Sessions over Time/);
    assert.match(html, /Correlated: 1,100 \/ 1,200 \(91\.60%\)/);
    assert.match(html, /One row per concurrent session slot/);
    assert.match(html, /shaded span = warmup/);
    assert.match(html, /cold \(&lt;10%\)/);
    assert.match(html, /partial \(10–70%\)/);
    assert.match(html, /warm \(≥70%\)/);
    assert.match(html, /3 slots at peak · 3 requests · 3 sessions/);
    assert.match(html, />S1</);
    assert.match(html, />S3</);
    assert.match(html, /Error Breakdown &amp; Drill-Down/);
    assert.match(html, /HTTP 503/);

    // Sessionless capability note
    const sessionlessHtml = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask()}
            telemetry={makeRichTelemetry({
                sessions_summary: { available: false, reason: 'Trace format ShareGPT has no session_id.' },
            })}
        />,
    );
    assert.match(sessionlessHtml, /Session swimlane unavailable/);
    assert.match(sessionlessHtml, /Trace format ShareGPT has no session_id\./);

    // Error breakdown drill-down calls onOpenErrorInspector with filter code
    const openedCodes = [];
    let requestsTree = null;
    function CaptureRequests() {
        requestsTree = RequestsTab({
            task: makeRichTask(),
            telemetry: makeRichTelemetry(),
            onOpenErrorInspector: (code) => openedCodes.push(code),
        });
        return null;
    }
    renderToStaticMarkup(<CaptureRequests />);
    const inspectBtn = findElement(requestsTree, (n) => n.type !== undefined && /Inspect Errors/.test(renderToStaticMarkup(n)) && typeof n.props?.onClick === 'function');
    const drillBtn = findElement(requestsTree, (n) => n.type !== undefined && n.props?.children === 'Drill down' && typeof n.props?.onClick === 'function');
    assert.ok(inspectBtn && drillBtn, 'Expected Inspect Errors and Drill down buttons');
    inspectBtn.props.onClick();
    drillBtn.props.onClick({ stopPropagation() {} });
    assert.deepEqual(openedCodes, ['all', '503']);
});

test('SystemTab renders host CPU/Memory/Disk/Power metrics and Non-Admin / missing node_exporter empty state', () => {
    const html = renderToStaticMarkup(<SystemTab telemetry={makeRichTelemetry()} />);
    assert.match(html, /worker-node-01/);
    assert.match(html, /Host CPU Utilization/);
    assert.match(html, /44\.50%/);
    assert.match(html, /Host Memory Usage/);
    assert.match(html, /75\.50 \/ 256 GiB/);
    assert.match(html, /180\.50 GiB/);
    assert.match(html, /Disk I\/O &amp; Capacity/);
    assert.match(html, /125\.40 \/ 48\.20 MiB\/s/);
    assert.match(html, /420 GiB/);
    assert.match(html, /Network Throughput/);
    assert.match(html, /Host Power/);
    assert.match(html, /245 W/);
    assert.match(html, /XPU Power &amp; Thermal/);
    assert.match(html, /310 W/);
    assert.match(html, /Host &amp; XPU Power/);

    const emptyHtml = renderToStaticMarkup(
        <SystemTab telemetry={{ server_telemetry: { available: false, status: 'unavailable' } }} />,
    );
    assert.match(emptyHtml, /Host system metrics unavailable/);
    assert.match(emptyHtml, /node_exporter/);
    assert.match(emptyHtml, /Non-Admin/);
});

test('RunCompareDrawer renders 2-3 task KPI comparison matrix, delta highlighting, and <2 runs empty state', async (t) => {
    const tasksById = {
        'sim-1': makeRichTask({ id: 'sim-1', name: 'baseline-run' }),
        'sim-2': makeRichTask({ id: 'sim-2', name: 'tuned-run' }),
    };

    await withMountedDom(
        t,
        async (url) => {
            const base = makeRichTelemetry();
            const stripSeries = (tel) => ({
                ...tel,
                client_series: { ...tel.client_series, buckets: [] },
                server_telemetry: { ...tel.server_telemetry, series: [] },
            });
            if (url.includes('/sim-1/telemetry')) {
                return Response.json(stripSeries(base));
            }
            if (url.includes('/sim-2/telemetry')) {
                return Response.json(stripSeries({
                    ...base,
                    forensics: {
                        ...base.forensics,
                        verdict: { ...base.forensics.verdict, throughput_rps: 18.0, goodput_rps: 17.5, attainment_pct: 99.0 },
                    },
                    client_series: {
                        ...base.client_series,
                        windows: {
                            ...base.client_series.windows,
                            profiling: {
                                ...base.client_series.windows.profiling,
                                throughput_rps: 18.0,
                                output_tok_per_s: 900.0,
                                ttft_ms: { p50: 100, p90: 220 },
                                tpot_ms: { p50: 16, p90: 30 },
                            },
                        },
                    },
                }));
            }
            return Response.json({});
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(<RunCompareDrawer isOpen onClose={() => {}} taskIds={['sim-1']} tasksById={{}} />);
            });
            assert.match(doc.body.textContent, /Select at least 2 runs/);

            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-1', 'sim-2']}
                        tasksById={tasksById}
                    />,
                );
            });
            await flush();
            assert.match(doc.body.textContent, /Compare Simulation Runs/);
            assert.match(doc.body.textContent, /KPI Comparison Matrix/);
            assert.match(doc.body.textContent, /baseline-run/);
            assert.match(doc.body.textContent, /sim-1 · Baseline/);
            assert.match(doc.body.textContent, /tuned-run/);
            assert.match(doc.body.textContent, /18 req\/s/);
            assert.match(doc.body.textContent, /\+26\.76%/);
        },
    );
});

test('deriveLegacyTelemetryFromTask provides legacy/partial fallback for OverviewTab and RequestsTab', () => {
    const legacyTask = {
        id: 'legacy-sim-1',
        status: 'completed',
        artifacts_incomplete: true,
        simulation: {
            duration_seconds: 60,
            backend_options: { ttft_slo: 0.5, tpot_slo: 0.05 },
        },
        result: {
            summary: {
                total_requests: 200,
                successful_requests: 196,
                failed_requests: 4,
                request_throughput: { avg: 3.33 },
                output_token_throughput: { avg: 180.5 },
                ttft: { mean_ms: 160, p50_ms: 145, p90_ms: 280, p99_ms: 410 },
                tpot: { mean_ms: 22, p50_ms: 20, p90_ms: 35, p99_ms: 49 },
            },
        },
    };
    const legacyTelemetry = deriveLegacyTelemetryFromTask(legacyTask);
    assert.equal(legacyTelemetry.telemetry_scope, 'client_only_legacy');
    assert.equal(legacyTelemetry.partial, true);

    const overviewHtml = renderToStaticMarkup(
        <OverviewTab task={legacyTask} telemetry={legacyTelemetry} analysisWindow="profiling" />,
    );
    assert.match(overviewHtml, /Client-only verdict/);
    assert.match(overviewHtml, /Partial Results/);
    assert.match(overviewHtml, /Legacy run/);
    assert.match(overviewHtml, /3\.33 req\/s/);

    const requestsHtml = renderToStaticMarkup(
        <RequestsTab task={legacyTask} telemetry={legacyTelemetry} analysisWindow="profiling" />,
    );
    assert.match(requestsHtml, /Full Percentile Table/);
    assert.match(requestsHtml, /145 ms/);
    assert.match(requestsHtml, /280 ms/);
});

test('OverviewTab, BottlenecksTab, and RequestsTab handle leading null buckets, sparse gaps, and single-point series visibly', () => {
    const burstBuckets = [
        {
            start_seconds: 0,
            end_seconds: 5,
            mid_seconds: 2.5,
            arrived_requests: 33,
            arrived_rps: 6.6,
            completed_requests: 0,
            throughput_rps: 0,
            goodput_rps: 0,
            output_tok_per_s: 0,
            in_flight_concurrency: 33,
            ttft: { mean: null, p50: null, p90: null, p99: null },
            tpot: { mean: null, p50: null, p90: null, p99: null },
            e2e: { mean: null, p50: null, p90: null, p99: null },
            drift_ms: { mean: 1.5, p50: 1.2, p90: 2.8, p99: 3.4 },
        },
        {
            start_seconds: 5,
            end_seconds: 10,
            mid_seconds: 7.5,
            arrived_requests: 13,
            arrived_rps: 2.6,
            completed_requests: 6,
            throughput_rps: 1.2,
            goodput_rps: 1.2,
            output_tok_per_s: 120,
            in_flight_concurrency: 40,
            ttft: { mean: 210, p50: 200, p90: 320, p99: 350 },
            tpot: { mean: 22, p50: 21, p90: 30, p99: 34 },
            e2e: { mean: 15200, p50: 15000, p90: 18500, p99: 19200 },
            drift_ms: null,
        },
        {
            start_seconds: 10,
            end_seconds: 15,
            mid_seconds: 12.5,
            arrived_requests: 0,
            arrived_rps: 0,
            completed_requests: 5,
            throughput_rps: 1.0,
            goodput_rps: 1.0,
            output_tok_per_s: 110,
            in_flight_concurrency: 35,
            ttft: { mean: 240, p50: 230, p90: 360, p99: 400 },
            tpot: { mean: 24, p50: 23, p90: 33, p99: 36 },
            e2e: { mean: 17100, p50: 16900, p90: 20100, p99: 21000 },
            drift_ms: null,
        },
        {
            start_seconds: 15,
            end_seconds: 20,
            mid_seconds: 17.5,
            arrived_requests: 0,
            arrived_rps: 0,
            completed_requests: 13,
            throughput_rps: 2.6,
            goodput_rps: 2.4,
            output_tok_per_s: 260,
            in_flight_concurrency: 22,
            ttft: { mean: 260, p50: 250, p90: 390, p99: 420 },
            tpot: { mean: 25, p50: 24, p90: 35, p99: 39 },
            e2e: { mean: 19400, p50: 19100, p90: 22500, p99: 23400 },
            drift_ms: null,
        },
        {
            start_seconds: 20,
            end_seconds: 25,
            mid_seconds: 22.5,
            arrived_requests: 0,
            arrived_rps: 0,
            completed_requests: 16,
            throughput_rps: 3.2,
            goodput_rps: 3.0,
            output_tok_per_s: 320,
            in_flight_concurrency: 6,
            ttft: { mean: 220, p50: 215, p90: 340, p99: 380 },
            tpot: { mean: 23, p50: 22, p90: 31, p99: 35 },
            e2e: { mean: 21200, p50: 21000, p90: 23800, p99: 24500 },
            drift_ms: null,
        },
        {
            start_seconds: 25,
            end_seconds: 30,
            mid_seconds: 27.5,
            arrived_requests: 0,
            arrived_rps: 0,
            completed_requests: 5,
            throughput_rps: 1.0,
            goodput_rps: 1.0,
            output_tok_per_s: 95,
            in_flight_concurrency: 1,
            ttft: { mean: 195, p50: 190, p90: 290, p99: 310 },
            tpot: { mean: 20, p50: 19, p90: 27, p99: 30 },
            e2e: { mean: 22800, p50: 22500, p90: 24100, p99: 24600 },
            drift_ms: null,
        },
        {
            start_seconds: 30,
            end_seconds: 31.966,
            mid_seconds: 30.983,
            arrived_requests: 0,
            arrived_rps: 0,
            completed_requests: 1,
            throughput_rps: 0.509,
            goodput_rps: 0.509,
            output_tok_per_s: 45,
            in_flight_concurrency: 0,
            ttft: { mean: 185, p50: 185, p90: 185, p99: 185 },
            tpot: { mean: 18, p50: 18, p90: 18, p99: 18 },
            e2e: { mean: 24000, p50: 24000, p90: 24000, p99: 24000 },
            drift_ms: null,
        },
    ];

    const burstTask = makeRichTask({
        simulation: { duration_seconds: 31.966, backend_options: { warmup_seconds: 0, ttft_slo: 0.5, tpot_slo: 0.05, e2e_slo: 25.0 } },
    });
    const burstTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            bucket_seconds: 5,
            buckets: burstBuckets,
        },
    });

    // 1. Burst arrival shape in OverviewTab: Area bands & Lines have connectNulls, tooltipType="none" on Area bands
    let overviewTree = null;
    function CaptureBurstOverview() {
        overviewTree = OverviewTab({ task: burstTask, telemetry: burstTelemetry, analysisWindow: 'full' });
        return null;
    }
    renderToStaticMarkup(<CaptureBurstOverview />);
    const ttftP50Line = findElement(overviewTree, (n) => n.props?.dataKey === 'ttft_p50' && n.props?.name === 'TTFT P50');
    const ttftP90Line = findElement(overviewTree, (n) => n.props?.dataKey === 'ttft_p90' && n.props?.name === 'TTFT P90');
    const ttftBaseArea = findElement(overviewTree, (n) => n.props?.dataKey === 'ttft_p50' && n.props?.stackId === 'band');
    const ttftBandArea = findElement(overviewTree, (n) => n.props?.dataKey === 'ttft_band' && n.props?.stackId === 'band');
    assert.ok(ttftP50Line && ttftP90Line && ttftBaseArea && ttftBandArea);
    assert.equal(ttftP50Line.props.connectNulls, true);
    assert.equal(ttftP90Line.props.connectNulls, true);
    assert.equal(ttftBaseArea.props.connectNulls, true);
    assert.equal(ttftBandArea.props.connectNulls, true);
    assert.equal(ttftBaseArea.props.tooltipType, 'none');
    assert.equal(ttftBandArea.props.tooltipType, 'none');
    assert.equal(ttftP50Line.props.dot, false);

    // 2. Single non-null point in OverviewTab -> dot prop is a visible circle config
    const singlePointTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            buckets: [
                { mid_seconds: 2.5, arrived_requests: 10, completed_requests: 0, throughput_rps: 0, ttft: { p50: null, p90: null } },
                { mid_seconds: 7.5, arrived_requests: 0, completed_requests: 4, throughput_rps: 0.8, ttft: { p50: 180, p90: 310 } },
                { mid_seconds: 12.5, arrived_requests: 0, completed_requests: 0, throughput_rps: 0, ttft: { p50: null, p90: null } },
            ],
        },
    });
    let singleOverviewTree = null;
    function CaptureSingleOverview() {
        singleOverviewTree = OverviewTab({ task: burstTask, telemetry: singlePointTelemetry, analysisWindow: 'full' });
        return null;
    }
    renderToStaticMarkup(<CaptureSingleOverview />);
    const singleP50Line = findElement(singleOverviewTree, (n) => n.props?.dataKey === 'ttft_p50' && n.props?.name === 'TTFT P50');
    assert.equal(typeof singleP50Line.props.dot, 'object');
    assert.ok(singleP50Line.props.dot.r > 0);

    // 3. Sparse intermediate null bucket in OverviewTab -> dot function renders <circle> ONLY for isolated points
    const sparseTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            buckets: [
                { mid_seconds: 2.5, arrived_requests: 5, completed_requests: 0, throughput_rps: 0, ttft: { p50: null, p90: null } },
                { mid_seconds: 7.5, arrived_requests: 0, completed_requests: 3, throughput_rps: 0.6, ttft: { p50: 150, p90: 260 } },
                { mid_seconds: 12.5, arrived_requests: 0, completed_requests: 0, throughput_rps: 0, ttft: { p50: null, p90: null } },
                { mid_seconds: 17.5, arrived_requests: 0, completed_requests: 2, throughput_rps: 0.4, ttft: { p50: 210, p90: 340 } },
            ],
        },
    });
    let sparseOverviewTree = null;
    function CaptureSparseOverview() {
        sparseOverviewTree = OverviewTab({ task: burstTask, telemetry: sparseTelemetry, analysisWindow: 'full' });
        return null;
    }
    renderToStaticMarkup(<CaptureSparseOverview />);
    const sparseChart = findElement(sparseOverviewTree, (n) => Array.isArray(n.props?.data) && n.props.data.some((pt) => pt.ttft_p50 === 150));
    const sparseP50Line = findElement(sparseOverviewTree, (n) => n.props?.dataKey === 'ttft_p50' && n.props?.name === 'TTFT P50');
    assert.equal(typeof sparseP50Line.props.dot, 'function');
    const dotForIso1 = sparseP50Line.props.dot({ cx: 100, cy: 50, value: 150, payload: sparseChart.props.data[1], index: 1 });
    const dotForNull = sparseP50Line.props.dot({ cx: 150, cy: null, value: null, payload: sparseChart.props.data[2], index: 2 });
    assert.equal(dotForIso1?.type, 'circle');
    assert.equal(dotForNull, null);

    // 4. BottlenecksTab single non-null routing point -> visible dot config
    const singleRoutingTelemetry = makeRichTelemetry({
        forensics: {
            ...makeRichTelemetry().forensics,
            routing_vs_cache: {
                epp_expected_hit_pct: 60,
                vllm_actual_hit_pct: 48,
                gap_pct: 12,
                series: [
                    { time_seconds: 5, epp_cache_hit_pct: null, vllm_prefix_hit_pct: 48 },
                    { time_seconds: 10, epp_cache_hit_pct: null, vllm_prefix_hit_pct: null },
                ],
            },
        },
    });
    let bottleneckTree = null;
    function CaptureSingleBottlenecks() {
        bottleneckTree = BottlenecksTab({ telemetry: singleRoutingTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureSingleBottlenecks />);
    const vllmLine = findElement(bottleneckTree, (n) => n.props?.dataKey === 'vllm_hit_pct');
    assert.equal(typeof vllmLine.props.dot, 'object');
    assert.ok(vllmLine.props.dot.r > 0);

    // 5. RequestsTab uses arrived_rps for actual_rps (not throughput_rps) and renders visible dot for single drift bucket
    let requestsTree = null;
    function CaptureBurstRequests() {
        requestsTree = RequestsTab({ task: burstTask, telemetry: burstTelemetry, analysisWindow: 'full' });
        return null;
    }
    renderToStaticMarkup(<CaptureBurstRequests />);
    const rateChart = findElement(requestsTree, (n) => Array.isArray(n.props?.data) && n.props.data[0]?.actual_rps === 6.6);
    assert.ok(rateChart, 'Expected actual_rps in RequestsTab to use arrived_rps (6.6) rather than completion throughput_rps (0)');
    const driftP50Line = findElement(requestsTree, (n) => n.props?.dataKey === 'drift_p50');
    assert.equal(typeof driftP50Line.props.dot, 'object');
    assert.ok(driftP50Line.props.dot.r > 0);
});

test('ServingTab renders interactive Traffic Replay controls when frames exist and hides FlowMap when no traffic exists', () => {
    const replayTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            buckets: [
                { mid_seconds: 2.5, arrived_rps: 6.6, throughput_rps: 0.0, output_tok_per_s: 0.0, in_flight_concurrency: 33 },
                { mid_seconds: 7.5, arrived_rps: 2.6, throughput_rps: 1.2, output_tok_per_s: 610.0, in_flight_concurrency: 38 },
            ],
        },
        server_telemetry: {
            ...makeRichTelemetry().server_telemetry,
            step_seconds: 5,
            per_pod: [
                { pod: 'llm-d-lens-sim-runner-0', role: 'model-server' },
                { pod: 'qwen-decode-0', role: 'decode', request_rate_rps: { mean: 0.6 }, output_token_rate_tps: { mean: 305 }, kv_cache_usage_percent: { mean: 24 } },
            ],
            series: [
                { elapsed_seconds: 0, request_rate_rps: 0, output_token_rate_tps: 0, running_requests: 0, kv_cache_usage_percent: 0 },
                { elapsed_seconds: 5, request_rate_rps: 1.2, output_token_rate_tps: 610, running_requests: 38, kv_cache_usage_percent: 46.9 },
            ],
            role_series: [
                { decode_request_rate_rps: 0, decode_output_token_rate_tps: 0, decode_running_requests: 0, decode_kv_cache_usage_percent: 0 },
                { decode_request_rate_rps: 1.2, decode_output_token_rate_tps: 610, decode_running_requests: 38, decode_kv_cache_usage_percent: 46.9 },
            ],
        },
    });

    const replayHtml = renderToStaticMarkup(<ServingTab telemetry={replayTelemetry} />);
    assert.match(replayHtml, /Recorded Deployment Flow Map/);
    assert.match(replayHtml, /Replay/);
    assert.match(replayHtml, /Summary/);
    assert.match(replayHtml, /Play/);
    assert.match(replayHtml, /Traffic replay timeline scrubber/);
    assert.match(replayHtml, /46\.90%/);

    // Dead empty map without traffic is hidden
    const noTrafficTelemetry = {
        server_telemetry: {
            available: true,
            status: 'available',
            topology_mode: 'disaggregated',
            per_pod: [{ pod: 'qwen-decode-0', role: 'decode' }],
            series: [{ elapsed_seconds: 0, running_requests: 0 }],
        },
    };
    const hiddenHtml = renderToStaticMarkup(<ServingTab telemetry={noTrafficTelemetry} />);
    assert.doesNotMatch(hiddenHtml, /Recorded Deployment Flow Map/);
});

test('SystemTab does not fall back to container filesystem I/O when host node_exporter disk metrics are absent', () => {
    const containerOnlyTelemetry = {
        server_telemetry: {
            available: true,
            status: 'available',
            summary: {
                filesystem_read_bytes_per_second: { mean: 5_519_972 },
                filesystem_write_bytes_per_second: { mean: 2_034_526 },
                gpu_power_watts: { mean: 110 },
            },
            series: [
                {
                    elapsed_seconds: 5,
                    filesystem_read_bytes_per_second: 5_519_972,
                    filesystem_write_bytes_per_second: 2_034_526,
                    gpu_power_watts: 110,
                },
            ],
        },
    };
    const html = renderToStaticMarkup(<SystemTab telemetry={containerOnlyTelemetry} />);
    assert.match(html, /Disk I\/O &amp; Capacity/);
    assert.match(html, /Unavailable/);
    assert.doesNotMatch(html, /5\.26/);
    assert.match(html, /Memory &amp; disk I\/O unavailable/);
});

test('ServingTab renders sticky sub-category pill bar and all 10 Serving sub-categories with profile-driven accelerator power/utilization charts', () => {
    const richServingTelemetry = makeRichTelemetry({
        server_telemetry: {
            ...makeRichTelemetry().server_telemetry,
            hardware_profile: {
                display_name: 'Generic XPU',
                ui: {
                    labels: {
                        metric_group: 'XPU',
                    },
                },
            },
            metric_sources: {
                vllm: { status: 'available', sample_count: 120 },
                accelerator_telemetry: { status: 'available', sample_count: 60 },
            },
            device_telemetry: {
                devices: [
                    {
                        device_index: 0,
                        pod: 'qwen-decode-0',
                        model: 'Accelerator-Pro',
                        power_watts: { mean: 245.5 },
                        utilization_percent: { mean: 72.4 },
                    },
                ],
            },
            cache_config: {
                hbm_capacity_tokens: 131072,
                effective_capacity_tokens: 118000,
                block_size_tokens: 16,
                num_gpu_blocks: 8192,
                engines: [
                    {
                        pod: 'qwen-decode-0',
                        role: 'decode',
                        block_size_tokens: 16,
                        num_gpu_blocks: 8192,
                        hbm_capacity_tokens: 131072,
                    },
                ],
            },
            router: {
                epp_in_flight_requests: { max: 18, mean: 9.5 },
                epp_scheduler_latency_ms: { p50: 1.8, p99: 4.5 },
                router_plugin_latency_ms: { p50: 0.7, p99: 1.6 },
            },
            per_endpoint: [
                {
                    endpoint: 'qwen-decode-0:8000',
                    role: 'decode',
                    request_share_percent: 100,
                    request_rate_rps: { mean: 1.2 },
                },
            ],
            derived: {
                request_destination_distribution: [
                    { destination: 'decode', share_percent: 100, count: 48 },
                ],
            },
            summary: {
                ...makeRichTelemetry().server_telemetry.summary,
                vllm_ttft_p50_ms: { mean: 142.3 },
                vllm_tpot_p50_ms: { mean: 24.1 },
                vllm_e2e_p50_ms: { mean: 1850.0 },
                vllm_queue_time_p50_ms: { mean: 12.4 },
                vllm_prefill_time_p50_ms: { mean: 129.9 },
                vllm_decode_time_p50_ms: { mean: 1707.7 },
                vllm_queue_time_p90_ms: { mean: 28.0 },
                vllm_prefill_time_p90_ms: { mean: 210.0 },
                vllm_decode_time_p90_ms: { mean: 2400.0 },
                engine_prompt_local_cache_tps: { mean: 480.0 },
                engine_prompt_external_tps: { mean: 120.0 },
                engine_prompt_local_compute_tps: { mean: 600.0 },
                prefix_cache_hit_percent: { mean: 44.4 },
                external_cache_hit_percent: { mean: 10.0 },
                cpu_cache_usage_percent: { mean: 14.2 },
                nixl_transfer_bytes_per_second: { mean: 104857600 },
                nixl_failed_transfers_total: { max: 0 },
                vllm_preemptions_total: { max: 3 },
                vllm_preemptions_per_second: { max: 0.4 },
                gpu_power_watts: { mean: 245.5 },
                gpu_utilization_percent: { mean: 72.4 },
                gpu_memory_bandwidth_utilization_percent: { mean: 61.2 },
            },
            series: [
                {
                    elapsed_seconds: 0,
                    request_rate_rps: 1.2,
                    prompt_token_rate_tps: 1200,
                    output_token_rate_tps: 610,
                    running_requests: 8,
                    waiting_requests: 2,
                    kv_cache_usage_percent: 42.5,
                    vllm_ttft_p50_ms: 140,
                    vllm_ttft_p90_ms: 210,
                    vllm_ttft_p99_ms: 280,
                    vllm_tpot_p50_ms: 24,
                    vllm_tpot_p90_ms: 31,
                    vllm_tpot_p99_ms: 38,
                    vllm_e2e_p50_ms: 1850,
                    vllm_e2e_p90_ms: 2450,
                    vllm_e2e_p99_ms: 3100,
                    vllm_queue_time_p50_ms: 12.4,
                    vllm_prefill_time_p50_ms: 129.9,
                    vllm_decode_time_p50_ms: 1707.7,
                    engine_prompt_local_cache_tps: 480,
                    engine_prompt_external_tps: 120,
                    engine_prompt_local_compute_tps: 600,
                    prefix_cache_hit_percent: 44.4,
                    cpu_cache_usage_percent: 14.2,
                    vllm_preemptions_per_second: 0.4,
                    epp_in_flight_requests: 9,
                    epp_scheduler_latency_ms: 1.8,
                    router_plugin_latency_ms: 0.7,
                    gpu_power_watts: 245.5,
                    gpu_utilization_percent: 72.4,
                    gpu_memory_bandwidth_utilization_percent: 61.2,
                },
            ],
        },
    });

    const html = renderToStaticMarkup(<ServingTab telemetry={richServingTelemetry} />);
    assert.match(html, /role="tablist" aria-label="Serving sub-categories"/);
    for (const label of [
        'All',
        'Topology &amp; Replay',
        'Latency',
        'Engine · vLLM',
        'Load',
        'KV Cache',
        'KV Reuse',
        'KV Offload',
        'KV Eviction',
        'XPU Power',
        'Grafana',
    ]) {
        assert.match(html, new RegExp(label));
    }

    for (const heading of [
        'Topology &amp; Traffic Replay',
        'Latency · Server &amp; Stage Breakdown',
        'Engine · vLLM',
        'Load · Inference Gateway &amp; EPP Routing',
        'KV Cache · Capacity &amp; Usage',
        'KV Reuse · Prefix Cache &amp; Prompt Sources',
        'KV Offload · Multi-Tier Usage &amp; NIXL Transfer',
        'KV Eviction · Block Churn, Residency &amp; Preemptions',
        'XPU Power, Utilization &amp; Memory',
        'Grafana Deep Links',
        'Per-Pod vLLM Summary',
        'Serving XPUs · Power \\(W\\)',
        'Serving XPUs · Utilization &amp; Memory Bandwidth',
        'Telemetry Sources &amp; Allocated Devices',
    ]) {
        assert.match(html, new RegExp(heading));
    }

    assert.doesNotMatch(html, /NVIDIA|H100/);

    let capturedServingTree = null;
    function CaptureServingAll() {
        capturedServingTree = ServingTab({ telemetry: richServingTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureServingAll />);
    const pillTabs = [];
    const walk = (node) => {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node)) {
            node.forEach(walk);
            return;
        }
        if (node.props?.role === 'tab') pillTabs.push(node);
        walk(node.props?.children);
    };
    walk(capturedServingTree);
    assert.equal(pillTabs.length, 11);
});

test('RequestsTab uses fidelity_series and OverviewTab marks send-vs-drain boundary when drain >= 3s', () => {
    const drainTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            dispatch_duration_seconds: 19.674,
            duration_seconds: 49.881,
            windows: {
                profiling: { ...makeRichTelemetry().client_series.windows.profiling, duration_seconds: 49.881 },
                full: { ...makeRichTelemetry().client_series.windows.full, duration_seconds: 49.881 },
            },
            fidelity_series: [
                {
                    start_seconds: 0,
                    end_seconds: 1.0,
                    mid_seconds: 0.5,
                    is_warmup: false,
                    target_arrival_rps: 5.0,
                    arrived_rps: 4.9,
                    arrived_requests: 5,
                    drift_ms: { mean: 1.2, p50: 1.0, p90: 2.4, p99: 3.1 },
                },
                {
                    start_seconds: 18.0,
                    end_seconds: 19.674,
                    mid_seconds: 18.837,
                    is_warmup: false,
                    target_arrival_rps: 5.2,
                    arrived_rps: 5.1,
                    arrived_requests: 9,
                    drift_ms: { mean: 1.5, p50: 1.3, p90: 2.9, p99: 3.8 },
                },
            ],
            buckets: [
                { time_seconds: 5, target_arrival_rps: 5.0, arrived_rps: 5.0, completed_rps: 4.0, goodput_rps: 4.0, output_tps: 200, ttft_p50_ms: 120, ttft_p90_ms: 210, client_inflight: 6 },
                { time_seconds: 45, target_arrival_rps: 0.0, arrived_rps: 0.0, completed_rps: 1.5, goodput_rps: 1.5, output_tps: 90, ttft_p50_ms: 130, ttft_p90_ms: 230, client_inflight: 2 },
            ],
        },
    });
    const drainTask = makeRichTask({ simulation: { duration_seconds: 20, grace_period_seconds: 30, backend_options: { warmup_seconds: 0 } } });

    const reqHtml = renderToStaticMarkup(
        <RequestsTab task={drainTask} telemetry={drainTelemetry} analysisWindow="full" />,
    );
    assert.match(reqHtml, /Send window: 0–19\.67s · Grace drain: 19\.67–49\.88s \(in-flight completion\)/);
    assert.match(reqHtml, /Send: 0–19\.67s/);

    let requestsTree = null;
    function CaptureRequests() {
        requestsTree = RequestsTab({ task: drainTask, telemetry: drainTelemetry, analysisWindow: 'full' });
        return null;
    }
    renderToStaticMarkup(<CaptureRequests />);
    const fidelityChart = findElement(
        requestsTree,
        (n) => Array.isArray(n.props?.data) && n.props.data[0]?.actual_rps === 4.9 && n.props.data[1]?.t === 18.837,
    );
    assert.ok(fidelityChart, 'Expected RequestsTab to plot fidelity_series rather than coarse buckets');
    const xAxisNode = findElement(fidelityChart, (n) => n.props?.dataKey === 't');
    assert.deepEqual(xAxisNode.props.domain, [0, 19.674]);

    const overviewHtml = renderToStaticMarkup(
        <OverviewTab task={drainTask} telemetry={drainTelemetry} analysisWindow="full" />,
    );
    assert.match(overviewHtml, /Drain starts at 19\.67s/);

    let overviewTree = null;
    function CaptureOverview() {
        overviewTree = OverviewTab({ task: drainTask, telemetry: drainTelemetry, analysisWindow: 'full' });
        return null;
    }
    renderToStaticMarkup(<CaptureOverview />);
    const drainLine = findElement(
        overviewTree,
        (n) => n.props?.x === 19.674 && n.props?.label?.value === 'Drain',
    );
    assert.ok(drainLine, 'Expected OverviewTab charts to render Drain ReferenceLine at dispatch_duration_seconds');
});

test('OverviewTab labels Warmup on the LEFT (insideTopRight) and Drain on the RIGHT (insideTopLeft) across all 3 charts', () => {
    const captureOverviewTree = (task, telemetry) => {
        let tree = null;
        function Capture() {
            tree = OverviewTab({ task, telemetry, analysisWindow: 'full' });
            return null;
        }
        renderToStaticMarkup(<Capture />);
        return tree;
    };

    // Case 1: Small warmup (5s of 60s) + near-right-edge Drain (55s of 60s)
    // Warmup must stay LEFT of its line (insideTopRight) and Drain must stay RIGHT of its line (insideTopLeft).
    const normalTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            warmup_seconds: 5,
            dispatch_duration_seconds: 55,
            duration_seconds: 60,
            windows: {
                profiling: { ...makeRichTelemetry().client_series.windows.profiling, duration_seconds: 55 },
                full: { ...makeRichTelemetry().client_series.windows.full, duration_seconds: 60 },
            },
            buckets: [
                { time_seconds: 0, arrived_rps: 4, completed_rps: 3, goodput_rps: 3, output_tps: 150, ttft_p50_ms: 110, ttft_p90_ms: 200, client_inflight: 4 },
                { time_seconds: 60, arrived_rps: 0, completed_rps: 2, goodput_rps: 2, output_tps: 100, ttft_p50_ms: 120, ttft_p90_ms: 210, client_inflight: 2 },
            ],
        },
    });
    const normalTask = makeRichTask({ simulation: { duration_seconds: 55, grace_period_seconds: 5, backend_options: { warmup_seconds: 5 } } });
    const normalTree = captureOverviewTree(normalTask, normalTelemetry);
    const warmupLines = findAllElements(normalTree, (n) => n.props?.x === 5 && n.props?.label?.value === 'Warmup');
    const drainLines = findAllElements(normalTree, (n) => n.props?.x === 55 && n.props?.label?.value === 'Drain');
    assert.equal(warmupLines.length, 3, 'Expected Warmup ReferenceLine on all 3 OverviewTab charts');
    assert.equal(drainLines.length, 3, 'Expected Drain ReferenceLine on all 3 OverviewTab charts');
    for (const line of warmupLines) {
        assert.equal(line.props.label.position, 'insideTopRight');
        assert.equal(line.props.label.fill, 'var(--text-muted)');
        assert.equal(line.props.label.fontSize, 10);
        assert.equal(line.props.label.dy, -18);
    }
    for (const line of drainLines) {
        assert.equal(line.props.label.position, 'insideTopLeft');
        assert.equal(line.props.label.fill, 'var(--text-muted)');
        assert.equal(line.props.label.fontSize, 10);
        assert.equal(line.props.label.dy, -18);
    }
    const timeCharts = findAllElements(
        normalTree,
        (n) => (n.type === ComposedChart || n.type === LineChart) && Array.isArray(n.props?.data) && n.props.data.length === 2,
    );
    assert.equal(timeCharts.length, 3, 'Expected 3 time-series charts in OverviewTab');
    for (const chart of timeCharts) {
        assert.ok(chart.props.margin.top >= 22, 'Expected top margin >= 22 for top-band boundary labels');
        assert.ok(chart.props.margin.right >= 44, 'Expected right margin >= 44 so near-edge Drain label is never clipped');
    }

    // Case 2: Close Warmup (15s) and Drain (20s) out of 60s -> point outward without collision
    const closeTelemetry = makeRichTelemetry({
        client_series: {
            ...normalTelemetry.client_series,
            warmup_seconds: 15,
            dispatch_duration_seconds: 20,
        },
    });
    const closeTree = captureOverviewTree(normalTask, closeTelemetry);
    const closeWarmup = findElement(closeTree, (n) => n.props?.x === 15 && n.props?.label?.value === 'Warmup');
    const closeDrain = findElement(closeTree, (n) => n.props?.x === 20 && n.props?.label?.value === 'Drain');
    assert.equal(closeWarmup.props.label.position, 'insideTopRight');
    assert.equal(closeDrain.props.label.position, 'insideTopLeft');
    assert.equal(closeDrain.props.label.dy, -18);

    // Case 3: Warmup very close to left edge (5s) AND Drain very close to Warmup (10s) -> still point away without staggering
    const tightLeftTelemetry = makeRichTelemetry({
        client_series: {
            ...normalTelemetry.client_series,
            warmup_seconds: 5,
            dispatch_duration_seconds: 10,
        },
    });
    const tightTree = captureOverviewTree(normalTask, tightLeftTelemetry);
    const tightWarmup = findElement(tightTree, (n) => n.props?.x === 5 && n.props?.label?.value === 'Warmup');
    const tightDrain = findElement(tightTree, (n) => n.props?.x === 10 && n.props?.label?.value === 'Drain');
    assert.equal(tightWarmup.props.label.position, 'insideTopRight');
    assert.equal(tightDrain.props.label.position, 'insideTopLeft');
    assert.equal(tightDrain.props.label.dy, -18);

    // Case 4: Warmup near right edge (58s of 60s) with no Drain -> remains insideTopRight (LEFT of line)
    const rightEdgeTelemetry = makeRichTelemetry({
        client_series: {
            ...normalTelemetry.client_series,
            warmup_seconds: 58,
            dispatch_duration_seconds: 60,
        },
    });
    const rightEdgeTree = captureOverviewTree(normalTask, rightEdgeTelemetry);
    const rightEdgeWarmup = findElement(rightEdgeTree, (n) => n.props?.x === 58 && n.props?.label?.value === 'Warmup');
    assert.equal(rightEdgeWarmup.props.label.position, 'insideTopRight');

    // Case 5: Recharts <Label> SVG geometry verification for zero-width vertical ReferenceLine viewBox
    const warmupLineX = 100;
    const drainLineX = 580;
    const plotTop = 22;
    const warmupSvg = renderToStaticMarkup(
        <Label
            viewBox={{ x: warmupLineX, y: plotTop, width: 0, height: 200, upperWidth: 0, lowerWidth: 0 }}
            {...warmupLines[0].props.label}
        />,
    );
    const drainSvg = renderToStaticMarkup(
        <Label
            viewBox={{ x: drainLineX, y: plotTop, width: 0, height: 200, upperWidth: 0, lowerWidth: 0 }}
            {...drainLines[0].props.label}
        />,
    );
    const warmupTextMatch = warmupSvg.match(/<text[^>]*\bx="([0-9.-]+)"[^>]*\by="([0-9.-]+)"[^>]*\btext-anchor="([^"]+)"[^>]*>/);
    const drainTextMatch = drainSvg.match(/<text[^>]*\bx="([0-9.-]+)"[^>]*\by="([0-9.-]+)"[^>]*\btext-anchor="([^"]+)"[^>]*>/);
    assert.ok(warmupTextMatch && drainTextMatch, 'Expected Recharts <Label> to render SVG <text>');
    assert.ok(Number(warmupTextMatch[1]) < warmupLineX, 'Expected Warmup x strictly left of warmup line');
    assert.ok(Number(warmupTextMatch[2]) < plotTop, 'Expected Warmup label above plot top');
    assert.equal(warmupTextMatch[3], 'end');
    assert.ok(Number(drainTextMatch[1]) > drainLineX, 'Expected Drain x strictly right of drain line');
    assert.ok(Number(drainTextMatch[2]) < plotTop, 'Expected Drain label above plot top');
    assert.equal(drainTextMatch[3], 'start');
});

test('ServingTab renders topology-aware multi-tier KV Offload cards on aggregated runs without NIXL Not enabled noise', () => {
    const aggOffloadTelemetry = makeRichTelemetry({
        server_telemetry: {
            ...makeRichTelemetry().server_telemetry,
            topology_mode: 'aggregated',
            per_pod: [
                { pod: 'qwen-worker-0', role: 'model-server', request_rate_rps: { mean: 2.5 }, output_token_rate_tps: { mean: 400 }, kv_cache_usage_percent: { mean: 62 } },
            ],
            derived: {
                offloaded_bytes: 536870912,
                restored_bytes: 134217728,
            },
            summary: {
                ...makeRichTelemetry().server_telemetry.summary,
                cpu_cache_usage_percent: { mean: 24.5, max: 38.2 },
                kv_offload_bytes_per_second: { mean: 52428800, max: 104857600 },
                kv_restore_bytes_per_second: { mean: 26214400, max: 52428800 },
                kv_offload_lookup_latency_p95_ms: { mean: 3.4 },
            },
            series: [
                {
                    elapsed_seconds: 0,
                    request_rate_rps: 2.5,
                    kv_cache_usage_percent: 62,
                    cpu_cache_usage_percent: 24.5,
                    kv_offload_bytes_per_second: 52428800,
                    kv_restore_bytes_per_second: 26214400,
                },
            ],
        },
    });

    const html = renderToStaticMarkup(<ServingTab telemetry={aggOffloadTelemetry} />);
    assert.match(html, /KV Offload · Multi-Tier Usage &amp; Offload/);
    assert.match(html, /P\/D NIXL: N\/A \(Aggregated\)/);
    assert.match(html, /VRAM KV Cache Usage/);
    assert.match(html, /CPU KV Cache Usage/);
    assert.match(html, /NVMe KV Cache Usage/);
    assert.match(html, /24\.50%/);
    assert.match(html, /KV Offload Throughput/);
    assert.match(html, /512 MB|512 MiB/);
    assert.match(html, /KV Restore Throughput/);
    assert.match(html, /128 MB|128 MiB/);
    assert.match(html, /KV Offload Lookup Sync Delay \(P95\)/);
    assert.match(html, /3\.40 ms/);
    assert.doesNotMatch(html, /NIXL KV Transfer Throughput/);
    assert.doesNotMatch(html, /NIXL KV Transfer Failures/);
    assert.doesNotMatch(html, /Not enabled/);
});

test('ServingTab #s-offload separates 3-tier KV cache usage (%) chart from data movement throughput (MiB/s) chart', () => {
    const captureServing = (telemetry) => {
        let tree = null;
        function Capture() {
            tree = ServingTab({ telemetry });
            return null;
        }
        renderToStaticMarkup(<Capture />);
        return tree;
    };

    const multiTierAggTelemetry = makeRichTelemetry({
        server_telemetry: {
            ...makeRichTelemetry().server_telemetry,
            topology_mode: 'aggregated',
            cache_config: {
                hbm_capacity_bytes: 16 * (1024 ** 3),
                cpu_capacity_bytes: 64 * (1024 ** 3),
                nvme_capacity_bytes: 256 * (1024 ** 3),
            },
            summary: {
                ...makeRichTelemetry().server_telemetry.summary,
                kv_cache_usage_percent: { mean: 68.4, max: 82.0 },
                cpu_cache_usage_percent: { mean: 31.2, max: 44.5 },
                disk_cache_usage_percent: { mean: 18.6, max: 27.9 },
                kv_offload_bytes_per_second: { mean: 104857600, max: 209715200 },
                kv_restore_bytes_per_second: { mean: 52428800, max: 104857600 },
            },
            series: [
                {
                    elapsed_seconds: 0,
                    kv_cache_usage_percent: 60.0,
                    cpu_cache_usage_percent: 28.0,
                    disk_cache_usage_percent: 15.0,
                    kv_offload_bytes_per_second: 104857600,
                    kv_restore_bytes_per_second: 52428800,
                },
                {
                    elapsed_seconds: 5,
                    kv_cache_usage_percent: 76.8,
                    cpu_cache_usage_percent: 34.4,
                    disk_cache_usage_percent: 22.2,
                    kv_offload_bytes_per_second: 209715200,
                    kv_restore_bytes_per_second: 104857600,
                },
            ],
        },
    });

    const aggHtml = renderToStaticMarkup(<ServingTab telemetry={multiTierAggTelemetry} />);
    assert.match(aggHtml, /VRAM KV Cache Usage/);
    assert.match(aggHtml, /68\.40%/);
    assert.match(aggHtml, /CPU KV Cache Usage/);
    assert.match(aggHtml, /31\.20%/);
    assert.match(aggHtml, /NVMe KV Cache Usage/);
    assert.match(aggHtml, /18\.60%/);
    assert.match(aggHtml, /KV Cache Usage by Tier/);
    assert.match(aggHtml, /KV Offload &amp; Restore Throughput/);

    const aggTree = captureServing(multiTierAggTelemetry);
    const offloadSectionAgg = findElement(aggTree, (n) => n.type === 'section' && n.props?.id === 's-offload');
    assert.ok(offloadSectionAgg);

    const tierUsageCard = findElement(offloadSectionAgg, (n) => n.type === ChartContainer && n.props?.title === 'KV Cache Usage by Tier');
    assert.ok(tierUsageCard, 'Expected KV Cache Usage by Tier chart in #s-offload');
    const tierYAxes = findAllElements(tierUsageCard, (n) => n.type === YAxis);
    assert.equal(tierYAxes.length, 1, 'Expected single YAxis (%) on KV Cache Usage by Tier chart');
    assert.equal(tierYAxes[0].props.label.value, '%');
    const tierLines = findAllElements(tierUsageCard, (n) => n.type === Line);
    assert.deepEqual(
        tierLines.map((l) => l.props.dataKey),
        ['kv_pct', 'cpu_kv_pct', 'disk_kv_pct'],
    );
    const tierLegend = findElement(tierUsageCard, (n) => n.type === ChartLegend);
    assert.deepEqual(
        tierLegend.props.entries.map((e) => e.label),
        ['VRAM KV Cache Usage (%)', 'CPU KV Cache Usage (%)', 'NVMe KV Cache Usage (%)'],
    );
    const tierTooltip = findElement(tierUsageCard, (n) => n.type === Tooltip);
    const tierTooltipHtml = renderToStaticMarkup(
        React.cloneElement(tierTooltip.props.content, {
            active: true,
            label: 5,
            payload: [
                { dataKey: 'kv_pct', name: 'VRAM KV Cache Usage (%)', value: 76.8, color: CHART_SERIES[0] },
                { dataKey: 'cpu_kv_pct', name: 'CPU KV Cache Usage (%)', value: 34.4, color: CHART_SERIES[1] },
                { dataKey: 'disk_kv_pct', name: 'NVMe KV Cache Usage (%)', value: 22.2, color: CHART_SERIES[2] },
            ],
        }),
    );
    assert.match(tierTooltipHtml, /76\.80%/);
    assert.match(tierTooltipHtml, /34\.40%/);
    assert.match(tierTooltipHtml, /22\.20%/);

    const aggThroughputCard = findElement(offloadSectionAgg, (n) => n.type === ChartContainer && n.props?.title === 'KV Offload & Restore Throughput');
    assert.ok(aggThroughputCard, 'Expected KV Offload & Restore Throughput chart in #s-offload');
    const throughputYAxesAgg = findAllElements(aggThroughputCard, (n) => n.type === YAxis);
    assert.equal(throughputYAxesAgg.length, 1, 'Expected single YAxis (MiB/s) on throughput chart without right % axis');
    assert.equal(throughputYAxesAgg[0].props.label.value, 'MiB/s');
    const throughputLinesAgg = findAllElements(aggThroughputCard, (n) => n.type === Line);
    assert.deepEqual(
        throughputLinesAgg.map((l) => l.props.dataKey),
        ['kv_offload_mbps', 'kv_restore_mbps'],
    );
    assert.equal(throughputLinesAgg.some((l) => l.props.dataKey === 'cpu_kv_pct'), false);
    const throughputTooltip = findElement(aggThroughputCard, (n) => n.type === Tooltip);
    const throughputTooltipHtml = renderToStaticMarkup(
        React.cloneElement(throughputTooltip.props.content, {
            active: true,
            label: 5,
            payload: [
                { dataKey: 'kv_offload_mbps', name: 'KV Offload (MiB/s)', value: 200, color: CHART_SERIES[0] },
                { dataKey: 'kv_restore_mbps', name: 'KV Restore (MiB/s)', value: 100, color: CHART_SERIES[1] },
            ],
        }),
    );
    assert.match(throughputTooltipHtml, /200 MiB\/s/);
    assert.match(throughputTooltipHtml, /100 MiB\/s/);
    assert.doesNotMatch(throughputTooltipHtml, /%/);

    // Disaggregated / NIXL mode also separates charts and includes nixl_mbps only in throughput chart
    const nixlTelemetry = makeRichTelemetry({
        server_telemetry: {
            ...multiTierAggTelemetry.server_telemetry,
            topology_mode: 'disaggregated',
            summary: {
                ...multiTierAggTelemetry.server_telemetry.summary,
                nixl_transfer_bytes_per_second: { mean: 314572800 },
                nixl_failed_transfers_total: { max: 0 },
            },
            series: multiTierAggTelemetry.server_telemetry.series.map((pt, idx) => ({
                ...pt,
                nixl_transfer_bytes_per_second: idx === 0 ? 262144000 : 314572800,
            })),
        },
    });
    const nixlTree = captureServing(nixlTelemetry);
    const offloadSectionNixl = findElement(nixlTree, (n) => n.type === 'section' && n.props?.id === 's-offload');
    const nixlThroughputCard = findElement(
        offloadSectionNixl,
        (n) => n.type === ChartContainer && n.props?.title === 'KV Offload, Restore & NIXL Transfer Throughput',
    );
    assert.ok(nixlThroughputCard);
    assert.equal(findAllElements(nixlThroughputCard, (n) => n.type === YAxis).length, 1);
    assert.deepEqual(
        findAllElements(nixlThroughputCard, (n) => n.type === Line).map((l) => l.props.dataKey),
        ['kv_offload_mbps', 'kv_restore_mbps', 'nixl_mbps'],
    );
});

test('SystemTab renders v3 server_telemetry.system and energy host metrics', () => {
    const sysTelemetry = {
        server_telemetry: {
            available: true,
            status: 'available',
            system: {
                available: true,
                nodes: ['node-a', 'node-b'],
                cpu_avg_pct: 37.2,
                cpu_peak_pct: 71.5,
                mem_total_bytes: 256 * (1024 ** 3),
                mem_avail_bytes: 192 * (1024 ** 3),
                disk_avail_bytes: 500 * (1024 ** 3),
                disk_read_bps: 40 * (1024 ** 2),
                disk_write_bps: 15 * (1024 ** 2),
                host_power_watts: 320,
                host_peak_power_watts: 410,
            },
            energy: {
                accelerator_avg_power_watts: 280,
                host_avg_power_watts: 320,
                host_energy_wh: 4.44,
            },
            series: [
                {
                    elapsed_seconds: 0,
                    host_cpu_usage_percent: 37.2,
                    host_memory_available_bytes: 192 * (1024 ** 3),
                    host_disk_read_bytes_per_second: 40 * (1024 ** 2),
                    host_disk_written_bytes_per_second: 15 * (1024 ** 2),
                    host_power_watts: 320,
                    gpu_power_watts: 280,
                },
            ],
        },
    };
    const html = renderToStaticMarkup(<SystemTab telemetry={sysTelemetry} />);
    assert.match(html, /node-a/);
    assert.match(html, /node-b/);
    assert.match(html, /37\.20%/);
    assert.match(html, /71\.50%/);
    assert.match(html, /64 \/ 256 GiB/);
    assert.match(html, /192 GiB/);
    assert.match(html, /500 GiB/);
    assert.match(html, /40 \/ 15 MiB\/s/);
    assert.match(html, /320 W/);
    assert.match(html, /410 W/);
    assert.match(html, /4\.44 Wh/);
    assert.match(html, /280 W/);
});

test('RunCompareDrawer renders Configuration Comparison with Grace period (including run_manifest.replay) and excludes 404 deleted runs cleanly', async (t) => {
    const tasksById = {
        'sim-1': makeRichTask({ id: 'sim-1', name: 'run-a', simulation: { duration_seconds: 60, grace_period_seconds: 30, backend_options: { warmup_seconds: 5 } } }),
    };
    const prunedCalls = [];

    await withMountedDom(
        t,
        async (url) => {
            const base = makeRichTelemetry();
            if (url.includes('/deleted-run/telemetry')) {
                return Response.json({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
            }
            if (url.includes('/sim-1/telemetry')) {
                return Response.json({
                    ...base,
                    client_series: { ...base.client_series, buckets: [] },
                    server_telemetry: { ...base.server_telemetry, series: [] },
                });
            }
            if (url.includes('/sim-2/telemetry')) {
                return Response.json({
                    ...base,
                    run_manifest: {
                        ...base.run_manifest,
                        name: 'run-b-manifest',
                        backend: 'trace-replayer',
                        replay: { duration_seconds: 60, grace_period_seconds: 45, warmup_seconds: 5, scale_factor: 2 },
                    },
                    client_series: { ...base.client_series, buckets: [] },
                    server_telemetry: { ...base.server_telemetry, series: [] },
                });
            }
            return Response.json({});
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-1', 'sim-2', 'deleted-run']}
                        tasksById={tasksById}
                        onPruneCompareIds={(ids) => prunedCalls.push(ids)}
                    />,
                );
            });
            await flush();
            assert.match(doc.body.textContent, /Configuration Comparison/);
            assert.match(doc.body.textContent, /Grace period/);
            assert.match(doc.body.textContent, /30 s/);
            assert.match(doc.body.textContent, /45 s/);
            assert.match(doc.body.textContent, /2x/);
            assert.match(doc.body.textContent, /Excluded deleted or unavailable run\(s\): deleted-run/);
            assert.deepEqual(prunedCalls, [['deleted-run']]);

            // When only 1 valid run remains after 404 exclusion, show empty state
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-1', 'deleted-run']}
                        tasksById={tasksById}
                    />,
                );
            });
            await flush();
            assert.match(doc.body.textContent, /Select at least 2 runs/);
        },
    );
});

test('SimulationDashboard preserves paginated compare IDs while pruning 404s and prunes on delete', async (t) => {
    const page1Tasks = [
        makeRichTask({ id: 'sim-1', name: 'valid-run-1' }),
    ];
    const page2Task = makeRichTask({ id: 'sim-2', name: 'valid-run-2' });
    let deletedIds = new Set();

    await withMountedDom(
        t,
        async (url, options = {}) => {
            if (url.startsWith('/api/v1/simulation/tasks/sim-1') && options.method === 'DELETE') {
                deletedIds.add('sim-1');
                return Response.json({ deleted: true });
            }
            if (url === '/api/v1/simulation/tasks/stale-deleted') {
                return Response.json({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
            }
            if (url === '/api/v1/simulation/tasks/sim-2') {
                return Response.json({ task: page2Task });
            }
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                const active = page1Tasks.filter((item) => !deletedIds.has(item.id));
                return Response.json({
                    tasks: active,
                    total: active.length + 1,
                    counts: { queued: 0, running: 0, completed: active.length + 1, failed: 0, cancelled: 0 },
                    facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] },
                });
            }
            if (url.includes('/sim-1/telemetry') || url.includes('/sim-2/telemetry')) {
                const base = makeRichTelemetry();
                return Response.json({
                    ...base,
                    client_series: { ...base.client_series, buckets: [] },
                    server_telemetry: { ...base.server_telemetry, series: [] },
                });
            }
            return Response.json({ items: [], backends: [], scenarios: [], datasets: [] });
        },
        async ({ root, doc, flush, clickButton }) => {
            globalThis.window.location = new URL('http://localhost/?compare=stale-deleted,sim-1,sim-2');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };

            await act(async () => {
                root.render(<SimulationDashboard initialAction="new" />);
            });
            await flush();
            await flush();

            // Stale 404 ID pruned, while page-2 sim-2 is preserved across pagination
            assert.doesNotMatch(doc.body.textContent, /stale-deleted/);
            assert.match(doc.body.textContent, /Selected to compare \(2\/3\)/);
            assert.match(doc.body.textContent, /valid-run-2/);
            assert.equal(globalThis.window.location.search, '?compare=sim-1%2Csim-2');

            // Delete sim-1: immediately prunes sim-1 from compareIds, closes RunCompareDrawer (<2 left), and keeps sim-2
            await clickButton(/Delete$/);
            await flush();
            await clickButton(/Delete Task/);
            await flush();

            assert.match(doc.body.textContent, /Selected to compare \(1\/3\)/);
            assert.doesNotMatch(doc.body.textContent, /Compare Simulation Runs/);
            assert.equal(globalThis.window.location.search, '?compare=sim-2');
        },
    );
});

test('SimulationTaskDetails displays Grace period from run_manifest.replay and strips deleted taskId from ?compare=', async (t) => {
    const taskPayload = makeRichTask({
        id: 'sim-grace-1',
        name: 'grace-detail-run',
        simulation: {
            duration_seconds: 20,
            backend_options: {},
        },
    });
    const base = makeRichTelemetry();
    const telemetryPayload = {
        ...base,
        client_series: {
            ...base.client_series,
            buckets: [],
            cold_start_comparison: null,
            windows: {
                profiling: { duration_seconds: 20 },
                full: { duration_seconds: 20 },
            },
        },
        server_telemetry: { ...base.server_telemetry, per_pod: [], series: [] },
        run_manifest: {
            execution_started_at: '2026-01-01T00:00:00Z',
            backend: 'trace-replayer',
            replay: { duration_seconds: 20, grace_period_seconds: 45, warmup_seconds: 5, scale_factor: 1 },
        },
    };
    const navigated = [];

    await withMountedDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/v1/simulation/tasks/sim-grace-1' && options.method === 'DELETE') {
                return Response.json({ deleted: true });
            }
            if (url.includes('/telemetry')) return Response.json(telemetryPayload);
            if (url.includes('/tasks/sim-grace-1')) return Response.json({ task: taskPayload });
            return Response.json({ items: [], backends: [] });
        },
        async ({ root, doc, flush, findButton, clickButton }) => {
            globalThis.window.location = new URL('http://localhost/?taskId=sim-grace-1&compare=sim-grace-1,sim-other');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            globalThis.window.confirm = () => true;
            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={(route) => navigated.push(route)} />);
            });
            await flush();
            await flush();
            await clickButton(/Logs & Configuration/);
            await flush();
            assert.match(doc.body.textContent, /Grace period/);
            assert.match(doc.body.textContent, /45 s/);

            const triggerDeleteBtn = findButton(/Delete$/);
            assert.ok(triggerDeleteBtn, 'Expected Delete button');
            let deletePromise = null;
            await act(async () => {
                deletePromise = reactProps(triggerDeleteBtn).onClick?.({ preventDefault() {}, stopPropagation() {} });
            });
            await flush();
            const deleteButtons = findElements(
                doc.body,
                (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'Delete',
            );
            const confirmBtn = deleteButtons[deleteButtons.length - 1];
            assert.ok(confirmBtn, 'Expected DeleteConfirmation confirm button');
            await act(async () => {
                reactProps(confirmBtn).onClick?.({ preventDefault() {}, stopPropagation() {} });
                await deletePromise;
            });
            await flush();
            assert.equal(globalThis.window.location.search, '?taskId=sim-grace-1&compare=sim-other');
            assert.deepEqual(navigated, ['optimization-simulate']);
        },
    );
});

test('ServingTab renders topology-aware EPP Predicted Prefix Cache Hit Rate on aggregated runs, P/D Routing Ratio on disaggregated runs, and sub-ms EPP latency', () => {
    const base = makeRichTelemetry();
    const aggTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'aggregated',
            router: {
                epp_in_flight_requests: { mean: 4.0, max: 9.0 },
                scheduler_latency_p50_ms: { mean: 0.05 },
                scheduler_latency_p90_ms: { mean: 0.09 },
                plugin_latency_p50_ms: { mean: 0.04 },
                plugin_latency_p90_ms: { mean: 0.08 },
                index_entry_count: { mean: 1248 },
                request_rate_rps: { mean: 3.5 },
            },
            derived: {
                pd_decision_ratio: null,
                avoided_prefill_ratio: 16.879,
            },
            series: [
                {
                    elapsed_seconds: 0,
                    router_scheduler_latency_p50_ms: 0.05,
                    router_scheduler_latency_p90_ms: 0.09,
                    router_plugin_latency_p50_ms: 0.04,
                    router_plugin_latency_p90_ms: 0.08,
                    gpu_utilization_percent: 100.00025421224746,
                    gpu_memory_bandwidth_utilization_percent: 100.00012,
                },
                {
                    elapsed_seconds: 5,
                    router_scheduler_latency_p50_ms: 0.06,
                    router_scheduler_latency_p90_ms: 0.11,
                    router_plugin_latency_p50_ms: 0.05,
                    router_plugin_latency_p90_ms: 0.09,
                    gpu_utilization_percent: 82.5,
                    gpu_memory_bandwidth_utilization_percent: 64.0,
                },
            ],
            summary: {
                ...base.server_telemetry.summary,
                gpu_utilization_percent: { mean: 91.2, max: 100.00025421224746 },
                gpu_memory_bandwidth_utilization_percent: { mean: 82.0, max: 100.00012 },
            },
        },
    };

    const aggHtml = renderToStaticMarkup(<ServingTab telemetry={aggTelemetry} />);
    assert.match(aggHtml, /EPP Predicted Prefix Cache Hit Rate/);
    assert.match(aggHtml, /16\.88%/);
    assert.doesNotMatch(aggHtml, /0\.17%/);
    assert.match(aggHtml, /Aggregated \(100% Worker\)/);
    assert.match(aggHtml, /Prefix Index Entries/);
    assert.match(aggHtml, /1,248/);
    assert.doesNotMatch(aggHtml, /P\/D Routing Ratio/);
    assert.match(aggHtml, /0\.05 ms/);
    assert.match(aggHtml, /0\.09 ms/);
    assert.match(aggHtml, /0\.04 ms/);
    assert.match(aggHtml, /0\.08 ms/);
    assert.doesNotMatch(aggHtml, /EPP latency time series unavailable/);
    assert.doesNotMatch(aggHtml, /100\.000/);

    let capturedAggTree = null;
    function CaptureAggServing() {
        capturedAggTree = ServingTab({ telemetry: aggTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureAggServing />);

    const eppLatencyChart = findElement(
        capturedAggTree,
        (n) => Array.isArray(n.props?.data) && findElement(n, (c) => c.props?.dataKey === 'epp_sched_p50'),
    );
    assert.ok(eppLatencyChart, 'Expected non-empty sub-ms EPP Scheduler & Plugin Latency chart series');
    assert.equal(eppLatencyChart.props.data[0]?.epp_sched_p50, 0.05);
    assert.equal(eppLatencyChart.props.data[0]?.epp_plugin_p90, 0.08);
    const eppYAxis = findElement(eppLatencyChart, (n) => n.props?.allowDecimals === true && typeof n.props?.tickFormatter === 'function');
    assert.ok(eppYAxis, 'Expected EPP latency YAxis to enable decimals and sub-ms tickFormatter');
    assert.equal(eppYAxis.props.tickFormatter(0.05), '0.05');
    assert.equal(eppYAxis.props.tickFormatter(0.09), '0.09');

    const utilChart = findElement(
        capturedAggTree,
        (n) => Array.isArray(n.props?.data) && findElement(n, (c) => c.props?.dataKey === 'accel_util'),
    );
    assert.ok(utilChart, 'Expected Accelerator Utilization & Memory Bandwidth chart');
    assert.equal(utilChart.props.data[0]?.accel_util, 100);
    assert.equal(utilChart.props.data[0]?.accel_mem_bw, 100);
    assert.ok(utilChart, 'Expected 100.00025421224746% GPU utilization jitter to clamp to 100 in chart data');
    const utilYAxis = findElement(
        utilChart,
        (n) => Array.isArray(n.props?.domain) && n.props.domain[0] === 0 && n.props.domain[1] === 100,
    );
    assert.ok(utilYAxis, 'Expected utilization YAxis with [0, 100] domain');
    assert.equal(utilYAxis.props.allowDataOverflow, true);
    assert.deepEqual(utilYAxis.props.ticks, [0, 25, 50, 75, 100]);

    const aggFallbackRouterHitTelemetry = {
        ...aggTelemetry,
        server_telemetry: {
            ...aggTelemetry.server_telemetry,
            derived: { pd_decision_ratio: null },
            router: {
                ...aggTelemetry.server_telemetry.router,
                cached_token_rate_tps: { mean: 250 },
                input_token_rate_tps: { mean: 1000 },
            },
        },
    };
    const fallbackRouterHitHtml = renderToStaticMarkup(<ServingTab telemetry={aggFallbackRouterHitTelemetry} />);
    assert.match(fallbackRouterHitHtml, /EPP Predicted Prefix Cache Hit Rate/);
    assert.match(fallbackRouterHitHtml, /25%/);

    const aggNoRouterTokenHtml = renderToStaticMarkup(
        <ServingTab
            telemetry={{
                ...aggTelemetry,
                server_telemetry: {
                    ...aggTelemetry.server_telemetry,
                    derived: { pd_decision_ratio: null },
                },
            }}
        />,
    );
    assert.match(aggNoRouterTokenHtml, /EPP Predicted Prefix Cache Hit Rate/);
    assert.match(aggNoRouterTokenHtml, /Aggregated/);

    const disaggTelemetry = {
        ...aggTelemetry,
        server_telemetry: {
            ...aggTelemetry.server_telemetry,
            topology_mode: 'disaggregated',
            derived: {
                pd_decision_ratio: 62.5,
                avoided_prefill_ratio: 16.879,
            },
        },
    };
    const disaggHtml = renderToStaticMarkup(<ServingTab telemetry={disaggTelemetry} />);
    assert.match(disaggHtml, /P\/D Routing Ratio/);
    assert.match(disaggHtml, /62\.50%/);
    assert.match(disaggHtml, /P\/D Disaggregation/);
    assert.match(disaggHtml, /16\.88%/);
});

test('BottlenecksTab computes 0..100 epp_hit_pct per series point from router token rates without treating null as zero', () => {
    const base = makeRichTelemetry();
    const bottlenecksTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            routing_vs_cache: {
                available: true,
                epp_expected_hit_pct: 16.879,
                vllm_actual_hit_pct: 12.5,
                gap_pct: 4.38,
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            series: [
                {
                    elapsed_seconds: 0,
                    router_input_token_rate_tps: 1000,
                    router_cached_token_rate_tps: 168.79,
                    prefix_cache_hit_percent: 12.5,
                },
                {
                    elapsed_seconds: 5,
                    router_input_token_rate_tps: 1000,
                    router_cached_token_rate_tps: null,
                    prefix_cache_hit_percent: 15.0,
                },
            ],
        },
    };

    const html = renderToStaticMarkup(<BottlenecksTab telemetry={bottlenecksTelemetry} />);
    assert.match(html, /16\.88%/);
    assert.doesNotMatch(html, /0\.17%/);

    let capturedTree = null;
    function CaptureBottlenecks() {
        capturedTree = BottlenecksTab({ telemetry: bottlenecksTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureBottlenecks />);
    const routingChart = findElement(
        capturedTree,
        (n) => Array.isArray(n.props?.data) && typeof n.props.data[0]?.epp_hit_pct === 'number',
    );
    assert.ok(routingChart, 'Expected Router / EPP Cache View vs vLLM Reality chart');
    assert.ok(Math.abs(routingChart.props.data[0].epp_hit_pct - 16.879) < 1e-6);
    assert.equal(routingChart.props.data[1].epp_hit_pct, null);
    assert.equal(routingChart.props.data[1].vllm_hit_pct, 15.0);
});

function assertNoNestedButtons(html, label) {
    const tagRe = /<\/?button\b[^>]*>/gi;
    let depth = 0;
    let match;
    while ((match = tagRe.exec(html)) !== null) {
        if (match[0].startsWith('</')) {
            depth = Math.max(0, depth - 1);
        } else {
            depth += 1;
            assert.equal(depth, 1, `Expected zero nested <button> tags in ${label}`);
        }
    }
}

test('OverviewTab, BottlenecksTab, RequestsTab, and SystemTab render role="tooltip" help explanations without nested <button> tags', () => {
    const base = makeRichTelemetry();
    const fullTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            cache_behavior: {
                cached_tps: 3200,
                fresh_tps: 1800,
                follow_up_turns: {
                    followup_count: 420,
                    followup_regression_pct: 8.5,
                    first_turn_ttft_p50_ms: 190,
                    followup_ttft_p50_ms: 115,
                },
            },
        },
        client_series: {
            ...base.client_series,
            drift_histogram: [{ label: '0-5ms', count: 950 }],
            prompt_length_buckets: [{ bucket: '<512', p50: 85, p90: 140, count: 400 }],
            cache_hit_buckets: [
                { bucket: 'cold', p50: 210, p90: 360, count: 300 },
                { bucket: 'warm', p50: 95, p90: 165, count: 600 },
            ],
            cdf: { quantiles: [50, 90], ttft_ms: [130, 290] },
        },
        sessions_summary: {
            ...base.sessions_summary,
            turn_distribution: [{ turns: 3, sessions: 120 }],
            tree_depth_distribution: [{ depth: 2, sessions: 95 }],
        },
    };
    const task = makeRichTask();

    // 1. OverviewTab: Verdict banner, 7 StatCards + detail rows, 5 charts, clickable Error Rate card without nested <button>
    const overviewHtml = renderToStaticMarkup(
        <OverviewTab
            task={task}
            telemetry={fullTelemetry}
            analysisWindow="profiling"
            onSelectTab={() => {}}
            onOpenErrorInspector={() => {}}
        />,
    );
    assertNoNestedButtons(overviewHtml, 'OverviewTab');
    const overviewTooltips = (overviewHtml.match(/role="tooltip"/g) || []).length;
    assert.ok(overviewTooltips >= 35, `Expected comprehensive tooltips on OverviewTab, got ${overviewTooltips}`);
    for (const label of [
        'Automated verdict help',
        'SLO Attainment help',
        'Goodput help',
        'Analysis window help',
        'Completed \\/ SLO-Compliant Requests help',
        'Error Rate help',
        'Peak KV Cache Usage help',
        'XPU Power &amp; Energy help',
        'Throughput over Time help',
        'Latency over Time help',
        'In-Flight Concurrency help',
        'Warmup vs Steady-State Latency help',
    ]) {
        assert.match(overviewHtml, new RegExp(`aria-label="${label}"`));
    }

    // 2. BottlenecksTab: Findings, timeline legend + Role header, TTFT decomposition stages, follow-up stats, EPP vs vLLM, per-pod headers
    const bottlenecksHtml = renderToStaticMarkup(
        <BottlenecksTab telemetry={fullTelemetry} onSelectTab={() => {}} />,
    );
    assertNoNestedButtons(bottlenecksHtml, 'BottlenecksTab');
    const bottlenecksTooltips = (bottlenecksHtml.match(/role="tooltip"/g) || []).length;
    assert.ok(bottlenecksTooltips >= 25, `Expected comprehensive tooltips on BottlenecksTab, got ${bottlenecksTooltips}`);
    for (const label of [
        'Ranked Bottleneck Findings help',
        'Per-Role Saturation Timeline help',
        'Serving role help',
        'TTFT Stage Decomposition help',
        'Prefill Time help',
        'Queue Time help',
        'Client / Gateway / Network help',
        'Follow-up turns count help',
        'EPP Predicted hit help',
        'vLLM Actual hit help',
        'EPP vs vLLM cache gap help',
        'Pod column help',
        'Peak KV Cache \\(%\\)|Pod peak KV column help',
    ]) {
        assert.match(bottlenecksHtml, new RegExp(`aria-label="(?:${label})"`));
    }
    assert.match(bottlenecksHtml, /overflow-x-auto lg:overflow-visible/);
    assert.ok(
        bottlenecksHtml.indexOf('aria-label="Serving role help"') < bottlenecksHtml.indexOf('overflow-x-auto'),
        'Expected Serving role help to remain outside any horizontally scrollable wrapper',
    );

    // 3. RequestsTab: Replay rate, drift badges & histogram, sessions, buckets, heatmaps (via prop), CDF, Percentile headers & rows, Error headers
    const requestsHtml = renderToStaticMarkup(
        <RequestsTab
            task={task}
            telemetry={fullTelemetry}
            analysisWindow="profiling"
            onOpenErrorInspector={() => {}}
        />,
    );
    assertNoNestedButtons(requestsHtml, 'RequestsTab');
    const requestsTooltips = (requestsHtml.match(/role="tooltip"/g) || []).length;
    assert.ok(requestsTooltips >= 30, `Expected comprehensive tooltips on RequestsTab, got ${requestsTooltips}`);
    assert.equal((requestsHtml.match(/overflow-x-auto lg:overflow-visible/g) || []).length, 2);
    for (const label of [
        'Trace Replay Fidelity · Rate help',
        'Schedule Drift \\(ms\\) help',
        'Schedule drift P50 help',
        'Schedule Drift Distribution help',
        'Sessions over Time help',
        'TTFT by Input Sequence Length Bucket help',
        'TTFT by Prefix Cache Hit Bucket help',
        'Turns per Session help',
        'Conversation Tree Depth help',
        'Cumulative Distribution Function \\(CDF\\) help',
        'Percentile metric column help',
        'P99 column help',
        'TTFT metric help',
        'Schedule Drift metric help',
        'Error category column help',
        'Error action column help',
    ]) {
        assert.match(requestsHtml, new RegExp(`aria-label="${label}"`));
    }
    let capturedRequestsTree = null;
    function CaptureRequestsHelp() {
        capturedRequestsTree = RequestsTab({
            task,
            telemetry: fullTelemetry,
            analysisWindow: 'profiling',
            onOpenErrorInspector: () => {},
        });
        return null;
    }
    renderToStaticMarkup(<CaptureRequestsHelp />);
    const heatmaps = [];
    const walkHeatmaps = (node) => {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node)) {
            node.forEach(walkHeatmaps);
            return;
        }
        if (node.type === LatencyHeatmap) heatmaps.push(node);
        walkHeatmaps(node.props?.children);
    };
    walkHeatmaps(capturedRequestsTree);
    assert.equal(heatmaps.length, 2);
    assert.ok(heatmaps.every((hm) => typeof hm.props.help === 'string' && hm.props.help.length > 20));

    // 4. SystemTab: Target nodes + Scope selector, 6 KPI StatCards + detail rows, 4 Host charts
    const systemHtml = renderToStaticMarkup(<SystemTab telemetry={fullTelemetry} />);
    assertNoNestedButtons(systemHtml, 'SystemTab');
    const systemTooltips = (systemHtml.match(/role="tooltip"/g) || []).length;
    assert.ok(systemTooltips >= 18, `Expected comprehensive tooltips on SystemTab, got ${systemTooltips}`);
    for (const label of [
        'Target nodes help',
        'Node scope help',
        'Host CPU Utilization help',
        'Peak CPU help',
        'Host Memory Usage help',
        'Peak Usage help',
        'Min Available help',
        'Disk I\\/O &amp; Capacity help',
        'Free \\/ Total Disk help',
        'Network Throughput help',
        'Peak RX \\/ TX help',
        'Host Power help',
        'Peak Power help',
        'XPU Power &amp; Thermal help',
        'Peak Temp help',
        'Host CPU Utilization over Time help',
        'Host Memory &amp; Disk I\\/O help',
        'Network RX \\/ TX Throughput help',
        'Host &amp; XPU Power help',
    ]) {
        assert.match(systemHtml, new RegExp(`aria-label="${label}"`));
    }
});

test('ServingTab renders role="tooltip" help explanations without nested <button> tags', () => {
    const base = makeRichTelemetry();
    const servingTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            device_telemetry: {
                ...(base.server_telemetry?.device_telemetry || {}),
                devices: [{ pod: 'qwen-decode-0', node: 'gpu-node-1', pci_bdf: '0000:18:00.0' }],
            },
        },
    };
    const servingHtml = renderToStaticMarkup(<ServingTab telemetry={servingTelemetry} />);
    assertNoNestedButtons(servingHtml, 'ServingTab');
    const servingTooltips = (servingHtml.match(/role="tooltip"/g) || []).length;
    assert.ok(servingTooltips >= 30, `Expected comprehensive tooltips on ServingTab, got ${servingTooltips}`);
    for (const label of [
        'Topology and traffic replay help',
        'Server latency and stage breakdown help',
        'Engine vLLM help',
        'KV offload help',
        'KV eviction and preemptions help',
        'Device Pod column help',
        'Device Node column help',
        'PCI BDF or Device column help',
    ]) {
        assert.match(servingHtml, new RegExp(`aria-label="${label}"`));
    }
});

test('ServingTab #s-evict renders KV block eviction churn & residency charts when present and guidance empty state when absent', () => {
    const base = makeRichTelemetry();
    const withKvBlocksTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            cache_config: {
                kv_cache_metrics_enabled: true,
                kv_cache_metrics_sample: 0.01,
            },
            derived: {
                ...(base.server_telemetry.derived || {}),
                kv_blocks_evicted_total: 4200,
            },
            summary: {
                ...base.server_telemetry.summary,
                kv_blocks_evicted_per_minute: { mean: 640, max: 1200, min: 120, latest: 800, p50: 600, p95: 1100, p99: 1180 },
                kv_block_idle_before_evict_p50_seconds: { mean: 1.25 },
                kv_block_idle_before_evict_p90_seconds: { mean: 4.5 },
                kv_block_idle_before_evict_p99_seconds: { mean: 9.75 },
                kv_block_idle_before_evict_seconds: { p50: 1.25, p90: 4.5, p99: 9.75 },
                kv_block_lifetime_p50_seconds: { mean: 12.4 },
                kv_block_lifetime_p90_seconds: { mean: 28.6 },
                kv_block_lifetime_p99_seconds: { mean: 45.2 },
                kv_block_lifetime_seconds: { p50: 12.4, p90: 28.6, p99: 45.2 },
                kv_block_reuse_gap_p50_seconds: { mean: 0.35 },
                kv_block_reuse_gap_p90_seconds: { mean: 1.8 },
                kv_block_reuse_gap_p99_seconds: { mean: 3.4 },
                kv_block_reuse_gap_seconds: { p50: 0.35, p90: 1.8, p99: 3.4 },
            },
            series: [
                {
                    elapsed_seconds: 10,
                    vllm_preemptions_per_second: 0.2,
                    kv_blocks_evicted_per_minute: 500,
                    kv_block_idle_before_evict_p50_seconds: 1.1,
                    kv_block_idle_before_evict_p90_seconds: 4.0,
                    kv_block_lifetime_p50_seconds: 11.5,
                    kv_block_lifetime_p90_seconds: 26.0,
                    kv_block_reuse_gap_p50_seconds: 0.3,
                    kv_block_reuse_gap_p90_seconds: 1.5,
                },
                {
                    elapsed_seconds: 20,
                    vllm_preemptions_per_second: 0.5,
                    kv_blocks_evicted_per_minute: 1200,
                    kv_block_idle_before_evict_p50_seconds: 1.4,
                    kv_block_idle_before_evict_p90_seconds: 5.0,
                    kv_block_lifetime_p50_seconds: 13.3,
                    kv_block_lifetime_p90_seconds: 31.2,
                    kv_block_reuse_gap_p50_seconds: 0.4,
                    kv_block_reuse_gap_p90_seconds: 2.1,
                },
            ],
        },
    };

    const presentHtml = renderToStaticMarkup(<ServingTab telemetry={withKvBlocksTelemetry} />);
    assertNoNestedButtons(presentHtml, 'ServingTab #s-evict present');
    assert.match(presentHtml, /KV Eviction · Block Churn, Residency &amp; Preemptions/);
    assert.match(presentHtml, /KV Block Eviction Rate \(blocks\/min\)/);
    assert.match(presentHtml, /KV Block Idle Time Before Eviction \(s\)/);
    assert.match(presentHtml, /KV Block Lifetime \(s\)/);
    assert.match(presentHtml, /KV Block Reuse Gap \(s\)/);
    assert.match(presentHtml, /Request Preemption Rate over Time/);

    // KPI values & details
    assert.match(presentHtml, /1\.20K \/ 640 blocks\/min/);
    assert.match(presentHtml, /4,200 blocks/);
    assert.match(presentHtml, /0\.01/);
    assert.match(presentHtml, /1\.25 s \/ 4\.50 s/);
    assert.match(presentHtml, /9\.75 s/);
    assert.match(presentHtml, /12\.40 s \/ 28\.60 s/);
    assert.match(presentHtml, /45\.20 s/);
    assert.match(presentHtml, /0\.35 s \/ 1\.80 s/);
    assert.match(presentHtml, /3\.40 s/);

    // Help tooltips on all new KPI cards, details, and charts
    for (const label of [
        'KV Block Eviction Rate help',
        'Total Evicted help',
        'Sample Rate help',
        'KV Block Idle Time Before Eviction \\(P50 \\/ P90\\) help',
        'KV Block Lifetime \\(P50 \\/ P90\\) help',
        'KV Block Reuse Gap \\(P50 \\/ P90\\) help',
        'KV Block Eviction Rate \\(blocks\\/min\\) help',
        'KV Block Idle Time Before Eviction \\(s\\) help',
        'KV Block Lifetime \\(s\\) help',
        'KV Block Reuse Gap \\(s\\) help',
    ]) {
        assert.match(presentHtml, new RegExp(`aria-label="${label}"`));
    }

    // Verify chart series mapping & connectNulls
    let capturedPresentTree = null;
    function CaptureKvBlocks() {
        capturedPresentTree = ServingTab({ telemetry: withKvBlocksTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureKvBlocks />);
    for (const dataKey of [
        'kv_blocks_evicted_per_min',
        'kv_idle_evict_p50_s',
        'kv_idle_evict_p90_s',
        'kv_lifetime_p50_s',
        'kv_lifetime_p90_s',
        'kv_reuse_gap_p50_s',
        'kv_reuse_gap_p90_s',
    ]) {
        const line = findElement(capturedPresentTree, (n) => n.props?.dataKey === dataKey);
        assert.ok(line, `Expected Line for ${dataKey}`);
        assert.equal(line.props.connectNulls, true);
    }
    const evictChart = findElement(
        capturedPresentTree,
        (n) => Array.isArray(n.props?.data) && findElement(n, (c) => c.props?.dataKey === 'kv_blocks_evicted_per_min'),
    );
    assert.equal(evictChart.props.data[0]?.kv_blocks_evicted_per_min, 500);
    assert.equal(evictChart.props.data[1]?.kv_reuse_gap_p90_s, 2.1);

    // Partial residency series: only lifetime present -> no broken empty charts for absent histograms
    const partialResidencyTelemetry = {
        ...withKvBlocksTelemetry,
        server_telemetry: {
            ...withKvBlocksTelemetry.server_telemetry,
            series: [
                {
                    elapsed_seconds: 10,
                    kv_blocks_evicted_per_minute: 300,
                    kv_block_lifetime_p50_seconds: 8.0,
                    kv_block_lifetime_p90_seconds: 15.0,
                },
            ],
        },
    };
    const partialHtml = renderToStaticMarkup(<ServingTab telemetry={partialResidencyTelemetry} />);
    assert.match(partialHtml, /KV Block Lifetime \(s\)/);
    assert.match(partialHtml, /No idle-before-evict series/);
    assert.match(partialHtml, /No block reuse gap series/);

    // Absent vllm:kv_block_* metrics -> still renders KV Block Eviction Rate (blocks/min) inside #s-evict with guidance copy
    const absentTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            cache_config: { kv_cache_metrics_enabled: false },
            series: [{ elapsed_seconds: 10, running_requests: 5, kv_cache_usage_percent: 45 }],
        },
    };
    const absentHtml = renderToStaticMarkup(<ServingTab telemetry={absentTelemetry} />);
    assertNoNestedButtons(absentHtml, 'ServingTab #s-evict absent');
    const evictSectionIdx = absentHtml.indexOf('id="s-evict"');
    assert.ok(evictSectionIdx >= 0, 'Expected #s-evict section');
    const evictSectionHtml = absentHtml.slice(evictSectionIdx);
    assert.match(evictSectionHtml, /KV Block Eviction Rate \(blocks\/min\)/);
    assert.match(evictSectionHtml, /vllm:kv_block_idle_before_evict_seconds/);
    assert.match(evictSectionHtml, /vllm:kv_block_lifetime_seconds/);
    assert.match(evictSectionHtml, /vllm:kv_block_reuse_gap_seconds/);
    assert.match(evictSectionHtml, /--kv-cache-metrics/);
    assert.match(evictSectionHtml, /--kv-cache-metrics-sample 0\.01/);
});

test('ServingTab renders stacked AreaCharts with partial-null guard, Total tooltips, 0-100% Prefix Cache Hit + Recompute %, and Prefill/Decode naming', () => {
    const base = makeRichTelemetry();
    const stackedTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'aggregated',
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            per_pod: [
                {
                    pod: 'qwen-prefill-0',
                    role: 'prefill',
                    request_rate_rps: { mean: 4.5 },
                    input_token_rate_tps: { mean: 1800 },
                    output_token_rate_tps: { mean: 120 },
                    running_requests: { mean: 3 },
                    waiting_requests: { mean: 0 },
                    kv_cache_usage_percent: { mean: 38.0 },
                    prefix_cache_hit_percent: { mean: 45.0 },
                },
            ],
            summary: {
                ...base.server_telemetry.summary,
                input_token_rate_tps: { mean: 1800, max: 2400 },
                output_token_rate_tps: { mean: 640, max: 910 },
            },
            series: [
                // t=0: leading empty scrape -> all stacked keys must stay null
                {
                    elapsed_seconds: 0,
                    input_token_rate_tps: 0,
                    output_token_rate_tps: 0,
                },
                // t=10: partial stage + partial prompt cache + branch 1 prefix/ext/recompute
                {
                    elapsed_seconds: 10,
                    vllm_prefill_time_p50_ms: 120,
                    engine_prompt_local_cache_tps: 300,
                    prefix_cache_hit_percent: 45,
                    external_prefix_cache_hit_percent: 15,
                    input_token_rate_tps: 1000,
                    output_token_rate_tps: 400,
                },
                // t=20: full stage + full prompt source + branch 2 (ext only)
                {
                    elapsed_seconds: 20,
                    router_scheduler_latency_p50_ms: 2.5,
                    vllm_queue_time_p50_ms: 15,
                    vllm_prefill_time_p50_ms: 110,
                    vllm_decode_time_p50_ms: 850,
                    engine_prompt_local_cache_tps: 400,
                    engine_prompt_external_tps: 150,
                    engine_prompt_local_compute_tps: 450,
                    external_prefix_cache_hit_percent: 25,
                    input_token_rate_tps: 1000,
                    output_token_rate_tps: 640,
                },
                // t=30: branch 3 (recompute fallback via fresh/input)
                {
                    elapsed_seconds: 30,
                    engine_prompt_local_compute_tps: 600,
                    input_token_rate_tps: 1000,
                    output_token_rate_tps: 500,
                },
                // t=40: trailing empty scrape -> all stacked keys must stay null
                {
                    elapsed_seconds: 40,
                },
                // t=50: negative & non-finite scrape values -> negatives clamp to 0, non-finite siblings default to 0
                {
                    elapsed_seconds: 50,
                    router_scheduler_latency_p50_ms: -4.2,
                    vllm_queue_time_p50_ms: Number.NaN,
                    vllm_prefill_time_p50_ms: Number.POSITIVE_INFINITY,
                    vllm_decode_time_p50_ms: 85,
                    engine_prompt_local_cache_tps: -25,
                    engine_prompt_external_tps: 120,
                    engine_prompt_local_compute_tps: Number.NaN,
                },
                // t=60: all-non-finite (NaN / Infinity / whitespace / boolean) -> all keys stay null
                {
                    elapsed_seconds: 60,
                    router_scheduler_latency_p50_ms: Number.NaN,
                    vllm_queue_time_p50_ms: Number.POSITIVE_INFINITY,
                    vllm_prefill_time_p50_ms: '   ',
                    vllm_decode_time_p50_ms: false,
                    engine_prompt_local_cache_tps: Number.NaN,
                    engine_prompt_external_tps: ' ',
                    engine_prompt_local_compute_tps: false,
                },
            ],
        },
    };

    const html = renderToStaticMarkup(<ServingTab telemetry={stackedTelemetry} />);
    assertNoNestedButtons(html, 'ServingTab stacked & prefill/decode');

    // #s-engine naming harmonization checks (KPIs, chart, legend, table headers, ? tooltips)
    assert.match(html, /Prefill Throughput/);
    assert.match(html, /Decode Throughput/);
    assert.doesNotMatch(html, /Prompt Throughput/);
    assert.doesNotMatch(html, /Generation Throughput/);
    assert.match(html, /Prefill vs Decode Token Throughput/);
    assert.match(html, /vLLM prefill \(input\) vs decode \(output\) token rates \(tok\/s\)/);
    assert.doesNotMatch(html, /Prompt vs Generation Token Throughput/);
    assert.match(html, /Prefill \(tok\/s\)/);
    assert.match(html, /Decode \(tok\/s\)/);
    assert.doesNotMatch(html, /Gen tok\/s/);
    assert.match(html, /aria-label="Prefill Throughput help"/);
    assert.match(html, /aria-label="Decode Throughput help"/);
    assert.match(html, /aria-label="Prefill vs Decode Token Throughput help"/);
    assert.match(html, /aria-label="Prefill (?:\()?tok\/s(?:\))? column help"/);
    assert.match(html, /aria-label="Decode (?:\()?tok\/s(?:\))? column help"/);
    assert.match(html, /Local Prefix Cache · External KV Transfer · Recomputed Prefix · Uncached Prompt \(%\)/);
    assert.match(html, /Recomputed Prefix \(%\)/);
    assert.match(html, /Uncached Prompt \(%\)/);

    let capturedTree = null;
    function CaptureServingStacked() {
        capturedTree = ServingTab({ telemetry: stackedTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureServingStacked />);

    const collectElements = (node, predicate, acc = []) => {
        if (!node) return acc;
        if (Array.isArray(node)) {
            node.forEach((item) => collectElements(item, predicate, acc));
            return acc;
        }
        if (typeof node !== 'object') return acc;
        if (predicate(node)) acc.push(node);
        collectElements(node.props?.actions, predicate, acc);
        collectElements(node.props?.children, predicate, acc);
        return acc;
    };

    // 1. Request Stage Latency Breakdown (#s-latency)
    const stageChart = findElement(
        capturedTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'epp_sched_ms')),
    );
    assert.ok(stageChart, 'Expected AreaChart for Request Stage Latency Breakdown');
    const stageAreas = collectElements(stageChart, (n) => n.type === Area);
    assert.deepEqual(stageAreas.map((a) => a.props.dataKey), ['epp_sched_ms', 'queue_ms', 'prefill_ms', 'decode_ms']);
    for (const area of stageAreas) {
        assert.equal(area.props.stackId, 'stage');
        assert.equal(area.props.fillOpacity, 0.32);
        assert.equal(area.props.strokeWidth, 1.75);
        assert.equal(area.props.fill, area.props.stroke);
        assert.equal(area.props.connectNulls, true);
        assert.equal(area.props.isAnimationActive, false);
    }
    // Partial-null & all-null guard on stageSeries
    assert.deepEqual(
        {
            epp: stageChart.props.data[0].epp_sched_ms,
            queue: stageChart.props.data[0].queue_ms,
            prefill: stageChart.props.data[0].prefill_ms,
            decode: stageChart.props.data[0].decode_ms,
        },
        { epp: null, queue: null, prefill: null, decode: null },
    );
    assert.deepEqual(
        {
            epp: stageChart.props.data[1].epp_sched_ms,
            queue: stageChart.props.data[1].queue_ms,
            prefill: stageChart.props.data[1].prefill_ms,
            decode: stageChart.props.data[1].decode_ms,
        },
        { epp: 0, queue: 0, prefill: 120, decode: 0 },
    );
    assert.deepEqual(
        {
            epp: stageChart.props.data[2].epp_sched_ms,
            queue: stageChart.props.data[2].queue_ms,
            prefill: stageChart.props.data[2].prefill_ms,
            decode: stageChart.props.data[2].decode_ms,
        },
        { epp: 2.5, queue: 15, prefill: 110, decode: 850 },
    );
    assert.equal(stageChart.props.data[4].prefill_ms, null);
    // t=50: negative clamps to 0, NaN/Infinity siblings default to 0 when decode_ms=85 is finite
    assert.deepEqual(
        {
            epp: stageChart.props.data[5].epp_sched_ms,
            queue: stageChart.props.data[5].queue_ms,
            prefill: stageChart.props.data[5].prefill_ms,
            decode: stageChart.props.data[5].decode_ms,
        },
        { epp: 0, queue: 0, prefill: 0, decode: 85 },
    );
    // t=60: all-non-finite (NaN / Infinity / whitespace / boolean) stays null
    assert.deepEqual(
        {
            epp: stageChart.props.data[6].epp_sched_ms,
            queue: stageChart.props.data[6].queue_ms,
            prefill: stageChart.props.data[6].prefill_ms,
            decode: stageChart.props.data[6].decode_ms,
        },
        { epp: null, queue: null, prefill: null, decode: null },
    );
    // Stage tooltip renders individual ms stages + Total row
    const stageTooltip = findElement(stageChart, (n) => n.type === Tooltip);
    const stageTipHtml = renderToStaticMarkup(
        stageTooltip.props.content.type({
            active: true,
            label: 20,
            payload: [
                { dataKey: 'epp_sched_ms', name: 'EPP Scheduling', color: '#8b5cf6', value: 2.5 },
                { dataKey: 'queue_ms', name: 'Queue Time', color: '#f59e0b', value: 15 },
                { dataKey: 'prefill_ms', name: 'Prefill Time', color: '#10b981', value: 110 },
                { dataKey: 'decode_ms', name: 'Decode Time', color: '#0ea5e9', value: 850 },
            ],
        }),
    );
    assert.match(stageTipHtml, /EPP Scheduling/);
    assert.match(stageTipHtml, /Decode Time/);
    assert.match(stageTipHtml, /Total/);
    assert.match(stageTipHtml, /977\.50 ms/);

    // 2. Prompt Token Throughput: Prefix Cache vs Prefill Compute (#s-latency)
    const promptSplitChart = findElement(
        capturedTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'prompt_cached_tps')),
    );
    assert.ok(promptSplitChart, 'Expected AreaChart for Prompt Token Throughput: Prefix Cache vs Prefill Compute');
    const promptSplitAreas = collectElements(promptSplitChart, (n) => n.type === Area);
    assert.deepEqual(promptSplitAreas.map((a) => a.props.dataKey), ['prompt_cached_tps', 'prompt_fresh_tps']);
    for (const area of promptSplitAreas) {
        assert.equal(area.props.stackId, 'promptSource');
        assert.equal(area.props.fillOpacity, 0.32);
        assert.equal(area.props.strokeWidth, 1.75);
        assert.equal(area.props.connectNulls, true);
        assert.equal(area.props.isAnimationActive, false);
    }
    assert.equal(promptSplitChart.props.data[0].prompt_cached_tps, null);
    assert.equal(promptSplitChart.props.data[0].prompt_fresh_tps, null);
    assert.equal(promptSplitChart.props.data[1].prompt_cached_tps, 300);
    assert.equal(promptSplitChart.props.data[1].prompt_fresh_tps, 0);
    assert.equal(promptSplitChart.props.data[2].prompt_cached_tps, 550);
    assert.equal(promptSplitChart.props.data[2].prompt_fresh_tps, 450);
    assert.equal(promptSplitChart.props.data[4].prompt_cached_tps, null);
    // t=50: negative local cache (-25) clamped to 0 so Cache Hit = 0 + 120 = 120; NaN fresh -> 0
    assert.equal(promptSplitChart.props.data[5].prompt_cached_tps, 120);
    assert.equal(promptSplitChart.props.data[5].prompt_fresh_tps, 0);
    // t=60: all-non-finite stays null
    assert.equal(promptSplitChart.props.data[6].prompt_cached_tps, null);
    assert.equal(promptSplitChart.props.data[6].prompt_fresh_tps, null);
    const splitTooltip = findElement(promptSplitChart, (n) => n.type === Tooltip);
    const splitTipHtml = renderToStaticMarkup(
        splitTooltip.props.content.type({
            active: true,
            label: 20,
            payload: [
                { dataKey: 'prompt_cached_tps', name: 'Prefix Cache (tok/s)', color: '#0ea5e9', value: 550 },
                { dataKey: 'prompt_fresh_tps', name: 'Prefill Compute (tok/s)', color: '#f59e0b', value: 450 },
            ],
        }),
    );
    assert.match(splitTipHtml, /Prefix Cache \(tok\/s\)/);
    assert.match(splitTipHtml, /Total/);
    assert.match(splitTipHtml, /1K tok\/s/);

    // 3. Prefix Cache Hit Rate over Time (#s-kvreuse) — 4-way stack
    const prefixHitChart = findElement(
        capturedTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'recompute_pct')),
    );
    assert.ok(prefixHitChart, 'Expected AreaChart for Prefix Cache Hit Rate over Time');
    const prefixHitAreas = collectElements(prefixHitChart, (n) => n.type === Area);
    assert.deepEqual(
        prefixHitAreas.map((a) => a.props.dataKey),
        ['prefix_hit_pct', 'ext_hit_pct', 'recompute_pct', 'cold_new_pct'],
    );
    for (const area of prefixHitAreas) {
        assert.equal(area.props.stackId, 'prefixHit');
        assert.equal(area.props.fillOpacity, 0.32);
        assert.equal(area.props.strokeWidth, 1.75);
        assert.equal(area.props.connectNulls, true);
        assert.equal(area.props.isAnimationActive, false);
    }
    const prefixYAxis = findElement(prefixHitChart, (n) => n.type === YAxis);
    assert.deepEqual(prefixYAxis.props.domain, [0, 100]);
    assert.deepEqual(prefixYAxis.props.ticks, [0, 25, 50, 75, 100]);
    assert.equal(prefixYAxis.props.allowDataOverflow, true);
    // Branch 4 (t=0, t=40): all null
    assert.equal(prefixHitChart.props.data[0].prefix_hit_pct, null);
    assert.equal(prefixHitChart.props.data[0].ext_hit_pct, null);
    assert.equal(prefixHitChart.props.data[0].recompute_pct, null);
    assert.equal(prefixHitChart.props.data[0].cold_new_pct, null);
    // Branch 1 (t=10): 45 + 15 + 0 + 40 = 100
    assert.equal(prefixHitChart.props.data[1].prefix_hit_pct, 45);
    assert.equal(prefixHitChart.props.data[1].ext_hit_pct, 15);
    assert.equal(prefixHitChart.props.data[1].recompute_pct, 0);
    assert.equal(prefixHitChart.props.data[1].cold_new_pct, 40);
    // Branch 2 (t=20): 0 + 25 + 0 + 75 = 100
    assert.equal(prefixHitChart.props.data[2].prefix_hit_pct, 0);
    assert.equal(prefixHitChart.props.data[2].ext_hit_pct, 25);
    assert.equal(prefixHitChart.props.data[2].recompute_pct, 0);
    assert.equal(prefixHitChart.props.data[2].cold_new_pct, 75);
    // Branch 3 (t=30): 0 + 0 + 0 + 100 = 100
    assert.equal(prefixHitChart.props.data[3].prefix_hit_pct, 0);
    assert.equal(prefixHitChart.props.data[3].ext_hit_pct, 0);
    assert.equal(prefixHitChart.props.data[3].recompute_pct, 0);
    assert.equal(prefixHitChart.props.data[3].cold_new_pct, 100);
    const prefixTooltip = findElement(prefixHitChart, (n) => n.type === Tooltip);
    const prefixTipHtml = renderToStaticMarkup(
        prefixTooltip.props.content.type({
            active: true,
            label: 10,
            payload: [
                { dataKey: 'prefix_hit_pct', name: 'Local Prefix Cache (%)', color: '#10b981', value: 45 },
                { dataKey: 'ext_hit_pct', name: 'External KV Transfer (%)', color: '#8b5cf6', value: 15 },
                { dataKey: 'recompute_pct', name: 'Recomputed Prefix (%)', color: '#f59e0b', value: 10 },
                { dataKey: 'cold_new_pct', name: 'Uncached Prompt (%)', color: '#db2777', value: 30 },
            ],
        }),
    );
    assert.match(prefixTipHtml, /Recomputed Prefix \(%\)/);
    assert.match(prefixTipHtml, /Uncached Prompt \(%\)/);
    assert.match(prefixTipHtml, /Total/);
    assert.match(prefixTipHtml, /100 %/);

    // 4. Prompt Token Sources over Time (#s-kvreuse)
    const promptSourceChart = findElement(
        capturedTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'prompt_local_cache_tps')),
    );
    assert.ok(promptSourceChart, 'Expected AreaChart for Prompt Token Sources over Time');
    const promptSourceAreas = collectElements(promptSourceChart, (n) => n.type === Area);
    assert.deepEqual(
        promptSourceAreas.map((a) => a.props.dataKey),
        ['prompt_local_cache_tps', 'prompt_external_tps', 'prompt_fresh_tps'],
    );
    for (const area of promptSourceAreas) {
        assert.equal(area.props.stackId, 'promptSource');
        assert.equal(area.props.fillOpacity, 0.32);
        assert.equal(area.props.strokeWidth, 1.75);
        assert.equal(area.props.connectNulls, true);
        assert.equal(area.props.isAnimationActive, false);
    }
    assert.equal(promptSourceChart.props.data[0].prompt_local_cache_tps, null);
    assert.equal(promptSourceChart.props.data[1].prompt_local_cache_tps, 300);
    assert.equal(promptSourceChart.props.data[1].prompt_external_tps, 0);
    assert.equal(promptSourceChart.props.data[1].prompt_fresh_tps, 0);
    assert.equal(promptSourceChart.props.data[2].prompt_local_cache_tps, 400);
    assert.equal(promptSourceChart.props.data[2].prompt_external_tps, 150);
    assert.equal(promptSourceChart.props.data[2].prompt_fresh_tps, 450);
    assert.equal(promptSourceChart.props.data[5].prompt_local_cache_tps, 0);
    assert.equal(promptSourceChart.props.data[5].prompt_external_tps, 120);
    assert.equal(promptSourceChart.props.data[5].prompt_fresh_tps, 0);
    assert.equal(promptSourceChart.props.data[6].prompt_local_cache_tps, null);
    assert.equal(promptSourceChart.props.data[6].prompt_external_tps, null);
    assert.equal(promptSourceChart.props.data[6].prompt_fresh_tps, null);
    const sourceTooltip = findElement(promptSourceChart, (n) => n.type === Tooltip);
    const sourceTipHtml = renderToStaticMarkup(
        sourceTooltip.props.content.type({
            active: true,
            label: 20,
            payload: [
                { dataKey: 'prompt_local_cache_tps', name: 'Local Prefix Cache', color: '#0ea5e9', value: 400 },
                { dataKey: 'prompt_external_tps', name: 'External KV Transfer', color: '#8b5cf6', value: 150 },
                { dataKey: 'prompt_fresh_tps', name: 'Prefill Compute', color: '#f59e0b', value: 450 },
            ],
        }),
    );
    assert.match(sourceTipHtml, /Local Prefix Cache/);
    assert.match(sourceTipHtml, /External KV Transfer/);
    assert.match(sourceTipHtml, /Prefill Compute/);
    assert.match(sourceTipHtml, /Total/);
    assert.match(sourceTipHtml, /1K tok\/s/);

    // 5. #s-engine Prefill vs Decode Token Throughput series names
    const throughputChart = findElement(
        capturedTree,
        (n) => n.type === LineChart && Boolean(findElement(n, (c) => c.type === Line && c.props?.dataKey === 'input_tps')),
    );
    assert.ok(throughputChart, 'Expected LineChart for Prefill vs Decode Token Throughput');
    const prefillLine = findElement(throughputChart, (n) => n.type === Line && n.props?.dataKey === 'input_tps');
    const decodeLine = findElement(throughputChart, (n) => n.type === Line && n.props?.dataKey === 'output_tps');
    assert.equal(prefillLine.props.name, 'Prefill tok/s');
    assert.equal(decodeLine.props.name, 'Decode tok/s');

    // 6. Single-point series visibility: sparseDotProp produces dot object on Area layers
    const singlePointTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            series: [
                {
                    elapsed_seconds: 5,
                    vllm_prefill_time_p50_ms: 95,
                    engine_prompt_local_cache_tps: 200,
                    prefix_cache_hit_percent: 50,
                },
            ],
        },
    };
    let singleTree = null;
    function CaptureSinglePoint() {
        singleTree = ServingTab({ telemetry: singlePointTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureSinglePoint />);
    const singleStageChart = findElement(
        singleTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'epp_sched_ms')),
    );
    const singleStageArea = findElement(singleStageChart, (n) => n.type === Area && n.props?.dataKey === 'prefill_ms');
    assert.equal(typeof singleStageArea.props.dot, 'object');
    assert.equal(singleStageArea.props.dot.r, 3.5);
});

test('ServingTab #s-engine renders prompt/input token accounting charts (derived, ceiling marker, role sources, and client fallbacks)', () => {
    const base = makeRichTelemetry();
    const derivedTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            derived: {
                token_flow: {
                    role_scope: 'prefill',
                    sent_tokens: 100000,
                    cached_tokens: 42000,
                    recomputed_tokens: 18000,
                    cold_new_tokens: 40000,
                    cached_pct: 42.0,
                    recomputed_pct: 18.0,
                    cold_new_pct: 40.0,
                    theoretical_cached_tokens: 60000,
                    theoretical_ceiling_pct: 60.0,
                },
                prompt_tokens_per_request: {
                    role_scope: 'prefill',
                    mean_prompt_tokens: 1200.5,
                    mean_cached_tokens: 480.2,
                    mean_computed_tokens: 720.3,
                    cached_pct: 40.0,
                    prompt_p50: 1100.0,
                    prompt_p90: 2400.0,
                    computed_p50: 650.0,
                    computed_p90: 1500.0,
                    source: 'vllm_histogram',
                },
                prompt_tokens_by_role_source: [
                    {
                        role: 'Prefill',
                        local_cache_tokens: 28000,
                        external_kv_tokens: 0,
                        local_compute_tokens: 52000,
                        total_tokens: 80000,
                    },
                    {
                        role: 'Decode',
                        local_cache_tokens: 14000,
                        external_kv_tokens: 6000,
                        local_compute_tokens: 0,
                        total_tokens: 20000,
                    },
                    {
                        role: 'epp',
                        local_cache_tokens: 999,
                        external_kv_tokens: 0,
                        local_compute_tokens: 1,
                        total_tokens: 1000,
                    },
                ],
            },
        },
    };

    const html = renderToStaticMarkup(<ServingTab telemetry={derivedTelemetry} />);
    assertNoNestedButtons(html, 'ServingTab #s-engine token accounting');

    const engineStart = html.indexOf('id="s-engine"');
    const perPodIdx = html.indexOf('Per-Pod vLLM Summary');
    assert.ok(engineStart >= 0 && perPodIdx > engineStart, 'Expected #s-engine before Per-Pod vLLM Summary');
    const enginePrePodHtml = html.slice(engineStart, perPodIdx);

    // Exact titles, subtitles, and help tooltips inside #s-engine before Per-Pod vLLM Summary
    assert.match(enginePrePodHtml, /Prompt Token Breakdown/);
    assert.match(enginePrePodHtml, /Local Prefix Cache · Recomputed Prefix · Uncached Prompt \(vs theoretical cache ceiling\)/);
    assert.match(enginePrePodHtml, /aria-label="Prompt Token Breakdown help"/);
    assert.match(enginePrePodHtml, /Prompt Tokens per Request/);
    assert.match(enginePrePodHtml, /Mean prefix-cached vs prefill-computed prompt tokens per request/);
    assert.match(enginePrePodHtml, /aria-label="Prompt Tokens per Request help"/);
    assert.match(enginePrePodHtml, /Prompt Token Sources by Role/);
    assert.match(enginePrePodHtml, /Local Prefix Cache · External KV Transfer · Prefill Compute/);
    assert.match(enginePrePodHtml, /aria-label="Prompt Token Sources by Role help"/);

    // Chart 1 ceiling marker & compact stat strip
    assert.match(enginePrePodHtml, /Ceiling 60%/);
    assert.match(enginePrePodHtml, /left:60%/);
    assert.match(enginePrePodHtml, /Total:.*100,000 tok/);
    assert.match(enginePrePodHtml, /Local Prefix Cache:.*42,000.*\(42%\)/);
    assert.match(enginePrePodHtml, /Recomputed Prefix:.*18,000.*\(18%\)/);
    assert.match(enginePrePodHtml, /Uncached Prompt:.*40,000.*\(40%\)/);
    assert.match(enginePrePodHtml, /Ceiling:.*60%/);

    // Chart 2 stat row including computed percentiles
    assert.match(enginePrePodHtml, /Mean:.*1,200\.50 tok/);
    assert.match(enginePrePodHtml, /Prefix Cache:.*480\.20 tok.*\(40%\)/);
    assert.match(enginePrePodHtml, /Prefill Compute:.*720\.30 tok/);
    assert.match(enginePrePodHtml, /P50 \/ P90 Prompt:.*1,100 \/ 2,400 tok/);
    assert.match(enginePrePodHtml, /P50 \/ P90 Prefill Compute:.*650 \/ 1,500 tok/);

    // Chart 3 role rows (Prefill + Decode, never EPP)
    assert.match(enginePrePodHtml, /Prefill/);
    assert.match(enginePrePodHtml, /80,000 tok.*Prefill Compute 65%/);
    assert.match(enginePrePodHtml, /Decode/);
    assert.match(enginePrePodHtml, /20,000 tok.*Prefill Compute 0%/);
    assert.doesNotMatch(enginePrePodHtml, /999 tok/);

    // Chart 1 omits ceiling marker cleanly when theoretical_ceiling_pct is null, and Chart 2 omits computed P50/P90 when null
    const noCeilingTelemetry = {
        ...derivedTelemetry,
        server_telemetry: {
            ...derivedTelemetry.server_telemetry,
            derived: {
                token_flow: {
                    ...derivedTelemetry.server_telemetry.derived.token_flow,
                    recomputed_tokens: 0,
                    recomputed_pct: 0,
                    cold_new_tokens: 58000,
                    cold_new_pct: 58.0,
                    theoretical_cached_tokens: null,
                    theoretical_ceiling_pct: null,
                },
                prompt_tokens_per_request: {
                    mean_prompt_tokens: 1000,
                    mean_cached_tokens: 420,
                    mean_computed_tokens: 580,
                    cached_pct: 42.0,
                    prompt_p50: 900,
                    prompt_p90: 1800,
                    computed_p50: null,
                    computed_p90: null,
                    source: 'client_fallback',
                },
                prompt_tokens_by_role_source: [
                    {
                        role: 'Worker',
                        local_cache_tokens: 12000,
                        external_kv_tokens: 1500,
                        local_compute_tokens: 26500,
                        total_tokens: 40000,
                    },
                ],
            },
        },
    };
    const noCeilingHtml = renderToStaticMarkup(<ServingTab telemetry={noCeilingTelemetry} />);
    assert.doesNotMatch(noCeilingHtml, /Ceiling /);
    assert.doesNotMatch(noCeilingHtml, /P50 \/ P90 Prefill Compute:/);
    assert.match(noCeilingHtml, /Worker/);
    assert.match(noCeilingHtml, /40,000 tok.*Prefill Compute 66\.25%/);

    // Client-side fallback (aggregated mode) when derived fields are completely absent
    const aggFallbackTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            duration_seconds: 100,
            windows: {
                ...base.client_series.windows,
                full: {
                    ...base.client_series.windows.full,
                    total_requests: 100,
                    total_input_tokens: 100000,
                    input_tokens: { avg: 1000, p50: 950, p90: 1900 },
                },
            },
        },
        sessions_summary: {
            ...base.sessions_summary,
            theoretical_prefix_overlap_pct: 60.0,
        },
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'aggregated',
            step_seconds: 50,
            per_pod: [
                { pod: 'qwen-worker-0', role: 'worker', request_rate_rps: { mean: 1.0 }, input_token_rate_tps: { mean: 1000 } },
            ],
            summary: {
                ...base.server_telemetry.summary,
                prefix_cache_hit_percent: { mean: 40.0 },
            },
            series: [
                { elapsed_seconds: 0, engine_prompt_local_cache_tps: 350, engine_prompt_external_tps: 50, engine_prompt_local_compute_tps: 600 },
                { elapsed_seconds: 100, engine_prompt_local_cache_tps: 350, engine_prompt_external_tps: 50, engine_prompt_local_compute_tps: 600 },
            ],
        },
    };
    const aggFallbackHtml = renderToStaticMarkup(<ServingTab telemetry={aggFallbackTelemetry} />);
    const aggPrePodHtml = aggFallbackHtml.slice(
        aggFallbackHtml.indexOf('id="s-engine"'),
        aggFallbackHtml.indexOf('Per-Pod vLLM Summary'),
    );
    assert.match(aggPrePodHtml, /Total:.*100,000 tok/);
    assert.match(aggPrePodHtml, /Local Prefix Cache:.*40,000.*\(40%\)/);
    assert.match(aggPrePodHtml, /Recomputed Prefix:.*20,000.*\(20%\)/);
    assert.match(aggPrePodHtml, /Uncached Prompt:.*40,000.*\(40%\)/);
    assert.match(aggPrePodHtml, /Ceiling 60%/);
    assert.match(aggPrePodHtml, /Mean:.*1,000 tok/);
    assert.match(aggPrePodHtml, /Prefix Cache:.*400 tok.*\(40%\)/);
    assert.match(aggPrePodHtml, /Prefill Compute:.*600 tok/);
    assert.match(aggPrePodHtml, /P50 \/ P90 Prompt:.*950 \/ 1,900 tok/);
    assert.match(aggPrePodHtml, /Worker/);
    assert.match(aggPrePodHtml, /100,000 tok.*Prefill Compute 60%/);

    // Client-side fallback in disaggregated mode attributes local compute/cache to Prefill and external KV to Decode
    const disaggFallbackTelemetry = {
        ...aggFallbackTelemetry,
        server_telemetry: {
            ...aggFallbackTelemetry.server_telemetry,
            topology_mode: 'disaggregated',
            per_pod: base.server_telemetry.per_pod,
        },
    };
    const disaggFallbackHtml = renderToStaticMarkup(<ServingTab telemetry={disaggFallbackTelemetry} />);
    const disaggPrePodHtml = disaggFallbackHtml.slice(
        disaggFallbackHtml.indexOf('id="s-engine"'),
        disaggFallbackHtml.indexOf('Per-Pod vLLM Summary'),
    );
    assert.match(disaggPrePodHtml, /Prefill/);
    assert.match(disaggPrePodHtml, /95,000 tok.*Prefill Compute 63\.16%/);
    assert.match(disaggPrePodHtml, /Decode/);
    assert.match(disaggPrePodHtml, /5,000 tok.*Prefill Compute 0%/);

    // Empty state inside each card when no data can be derived at all
    const emptyEngineHtml = renderToStaticMarkup(<ServingTab telemetry={base} />);
    assert.match(emptyEngineHtml, /Prompt token breakdown unavailable/);
    assert.match(emptyEngineHtml, /Per-request prompt tokens unavailable/);
    assert.match(emptyEngineHtml, /Prompt token sources by role unavailable/);
});

test('ServingTab #s-kvreuse 4-way Prefix Cache Hit % over Time distinguishes cold Turn-1 from multi-turn recompute & drain fallback', () => {
    const base = makeRichTelemetry();
    const fourWayTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            bucket_seconds: 10,
            buckets: [
                // [0, 10): Turn-1 cold traffic -> reusable_prefix_pct === 0
                { start_seconds: 0, end_seconds: 10, mid_seconds: 5, arrived_requests: 10, input_tokens_total: 10000, reusable_prefix_pct: 0.0 },
                // [10, 20): Multi-turn miss traffic -> reusable_prefix_pct === 75
                { start_seconds: 10, end_seconds: 20, mid_seconds: 15, arrived_requests: 12, input_tokens_total: 12000, reusable_prefix_pct: 75.0 },
                // [20, 30): Multi-turn with vLLM counter override -> reusable_prefix_pct === 50, vllm recomputed = 30%
                { start_seconds: 20, end_seconds: 30, mid_seconds: 25, arrived_requests: 8, input_tokens_total: 8000, reusable_prefix_pct: 50.0 },
                // [30, 40): Drain bucket (no arrivals, reusable_prefix_pct null) -> falls back to theoretical_ceiling_pct = 60%
                { start_seconds: 30, end_seconds: 40, mid_seconds: 35, arrived_requests: 0, input_tokens_total: 0, reusable_prefix_pct: null },
            ],
        },
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'aggregated',
            derived: {
                token_flow: {
                    sent_tokens: 30000,
                    cached_tokens: 10500,
                    recomputed_tokens: 7500,
                    cold_new_tokens: 12000,
                    cached_pct: 35.0,
                    recomputed_pct: 25.0,
                    cold_new_pct: 40.0,
                    theoretical_cached_tokens: 18000,
                    theoretical_ceiling_pct: 60.0,
                },
            },
            series: [
                // t=5 (Turn-1 cold): hit=20%, ext=0%, uncached=80%, reusable=0% -> Recomputed=0, ColdNew=80
                { elapsed_seconds: 5, prefix_cache_hit_percent: 20, external_prefix_cache_hit_percent: 0, input_token_rate_tps: 1000 },
                // t=15 (Multi-turn miss): hit=40%, ext=10%, uncached=50%, reusable=75% -> Recomputed=25, ColdNew=25
                { elapsed_seconds: 15, prefix_cache_hit_percent: 40, external_prefix_cache_hit_percent: 10, input_token_rate_tps: 1000 },
                // t=25 (vLLM counter > session): hit=45%, ext=0%, uncached=55%, session=5, vllm_recomp=300/1000=30 -> Recomputed=30, ColdNew=25
                { elapsed_seconds: 25, prefix_cache_hit_percent: 45, external_prefix_cache_hit_percent: 0, input_token_rate_tps: 1000, engine_prompt_recomputed_tps: 300 },
                // t=35 (Drain fallback): hit=35%, ext=5%, uncached=60%, ceiling=60% -> Recomputed=20, ColdNew=40
                { elapsed_seconds: 35, prefix_cache_hit_percent: 35, external_prefix_cache_hit_percent: 5, input_token_rate_tps: 500 },
            ],
        },
    };

    let capturedTree = null;
    function CaptureFourWay() {
        capturedTree = ServingTab({ telemetry: fourWayTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureFourWay />);

    const prefixHitChart = findElement(
        capturedTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'cold_new_pct')),
    );
    assert.ok(prefixHitChart, 'Expected 4-way Prefix Cache Hit % over Time chart');

    // t=5: Turn-1 cold -> Recomputed = 0, Cold New = 80
    assert.equal(prefixHitChart.props.data[0].prefix_hit_pct, 20);
    assert.equal(prefixHitChart.props.data[0].ext_hit_pct, 0);
    assert.equal(prefixHitChart.props.data[0].recompute_pct, 0);
    assert.equal(prefixHitChart.props.data[0].cold_new_pct, 80);

    // t=15: Multi-turn miss -> Recomputed = 25, Cold New = 25
    assert.equal(prefixHitChart.props.data[1].prefix_hit_pct, 40);
    assert.equal(prefixHitChart.props.data[1].ext_hit_pct, 10);
    assert.equal(prefixHitChart.props.data[1].recompute_pct, 25);
    assert.equal(prefixHitChart.props.data[1].cold_new_pct, 25);

    // t=25: vLLM recomputed counter overrides smaller session_recomp -> Recomputed = 30, Cold New = 25
    assert.equal(prefixHitChart.props.data[2].prefix_hit_pct, 45);
    assert.equal(prefixHitChart.props.data[2].ext_hit_pct, 0);
    assert.equal(prefixHitChart.props.data[2].recompute_pct, 30);
    assert.equal(prefixHitChart.props.data[2].cold_new_pct, 25);

    // t=35: Drain bucket falls back to ceiling 60% -> Recomputed = 20, Cold New = 40
    assert.equal(prefixHitChart.props.data[3].prefix_hit_pct, 35);
    assert.equal(prefixHitChart.props.data[3].ext_hit_pct, 5);
    assert.equal(prefixHitChart.props.data[3].recompute_pct, 20);
    assert.equal(prefixHitChart.props.data[3].cold_new_pct, 40);
});

test('ServingTab fallback honors client_series.token_flow fields, request_count, and omits ceiling when overlap is 0%', () => {
    const base = makeRichTelemetry();
    const clientTokenFlowTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            token_flow: {
                sent_tokens: 50000,
                client_cached_tokens: 15000,
                raw_session_reusable_tokens: 30000,
            },
            windows: {
                full: {
                    request_count: 50,
                    total_input_tokens: 50000,
                    input_tokens: { p50: 900, p90: 1600 },
                },
            },
        },
        sessions_summary: { available: false },
        server_telemetry: {
            ...base.server_telemetry,
            summary: {},
            series: [{ elapsed_seconds: 10, running_requests: 2 }],
        },
    };
    const html = renderToStaticMarkup(<ServingTab telemetry={clientTokenFlowTelemetry} />);
    const prePod = html.slice(html.indexOf('id="s-engine"'), html.indexOf('Per-Pod vLLM Summary'));
    assert.match(prePod, /Total:.*50,000 tok/);
    assert.match(prePod, /Local Prefix Cache:.*15,000.*\(30%\)/);
    assert.match(prePod, /Recomputed Prefix:.*15,000.*\(30%\)/);
    assert.match(prePod, /Uncached Prompt:.*20,000.*\(40%\)/);
    assert.match(prePod, /Ceiling 60%/);
    assert.match(prePod, /Mean:.*1,000 tok/);
    assert.match(prePod, /Prefix Cache:.*300 tok.*\(30%\)/);
    assert.match(prePod, /Prefill Compute:.*700 tok/);

    // Zero overlap omits ceiling cleanly
    const zeroOverlapTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            windows: {
                full: {
                    request_count: 40,
                    input_tokens: { avg: 500, p50: 450, p90: 800 },
                },
            },
        },
        sessions_summary: {
            available: false,
            theoretical_prefix_overlap_pct: 0,
            reusable_prefix_tokens: 0,
        },
        server_telemetry: {
            ...base.server_telemetry,
            summary: {},
            series: [{ elapsed_seconds: 10, running_requests: 2 }],
        },
    };
    const zeroHtml = renderToStaticMarkup(<ServingTab telemetry={zeroOverlapTelemetry} />);
    const zeroPrePod = zeroHtml.slice(zeroHtml.indexOf('id="s-engine"'), zeroHtml.indexOf('Per-Pod vLLM Summary'));
    assert.match(zeroPrePod, /Total:.*20,000 tok/);
    assert.match(zeroPrePod, /Uncached Prompt:.*20,000.*\(100%\)/);
    assert.doesNotMatch(zeroPrePod, /Ceiling /);
});

function makeV7SystemTelemetry({ nodeCount = 2, hardwareProfile = { ui: { labels: { metric_group: 'Gaudi' } } } } = {}) {
    const GiB = 1024 ** 3;
    const MiB = 1024 ** 2;
    const perNode = Array.from({ length: nodeCount }, (_, idx) => {
        const num = idx + 1;
        const name = `smc-${17 + num}`;
        return {
            node: name,
            roles: idx === 0 ? ['prefill'] : ['decode'],
            pod_count: idx === 0 ? 1 : 2,
            pods: idx === 0 ? [`pod-prefill-${idx}`] : [`pod-decode-${idx}a`, `pod-decode-${idx}b`],
            allocated_accelerators: 4 + idx * 4,
            cpu_usage_percent: { mean: 20 + num * 10, max: 40 + num * 12 },
            memory_used_bytes: { mean: (60 + num * 20) * GiB, max: (70 + num * 20) * GiB },
            memory_available_bytes: { mean: (196 - num * 20) * GiB, min: (186 - num * 20) * GiB },
            memory_total_bytes: 256 * GiB,
            memory_usage_percent: { mean: 30 + num * 5, max: 40 + num * 6 },
            disk_read_bytes_per_second: { mean: (10 + num * 5) * MiB, max: (20 + num * 5) * MiB },
            disk_written_bytes_per_second: { mean: (4 + num * 2) * MiB, max: (8 + num * 2) * MiB },
            filesystem_avail_bytes: (300 + num * 50) * GiB,
            filesystem_size_bytes: 1000 * GiB,
            network_receive_bytes_per_second: { mean: (100 + num * 25) * MiB, max: (180 + num * 25) * MiB },
            network_transmit_bytes_per_second: { mean: (80 + num * 15) * MiB, max: (140 + num * 15) * MiB },
            host_power_watts: { mean: 300 + num * 50, max: 400 + num * 50 },
            gpu_utilization_percent: { mean: 55 + num * 10, max: 85 + num * 5 },
            gpu_power_watts: { mean: 250 + num * 30, max: 350 + num * 20 },
            gpu_temperature_celsius: { mean: 58 + num * 3, max: 66 + num * 4 },
            gpu_memory_used_bytes: { mean: 32 * GiB, max: 48 * GiB },
            gpu_memory_total_bytes: 96 * GiB,
        };
    });
    const perNodeSeries = {};
    for (const row of perNode) {
        perNodeSeries[row.node] = [
            {
                timestamp: '2026-01-01T00:00:00Z',
                cpu_usage_percent: row.cpu_usage_percent.mean - 5,
                memory_available_bytes: row.memory_available_bytes.mean,
                memory_used_bytes: row.memory_used_bytes.mean,
                memory_usage_percent: row.memory_usage_percent.mean,
                disk_read_bytes_per_second: row.disk_read_bytes_per_second.mean,
                disk_written_bytes_per_second: row.disk_written_bytes_per_second.mean,
                network_receive_bytes_per_second: row.network_receive_bytes_per_second.mean,
                network_transmit_bytes_per_second: row.network_transmit_bytes_per_second.mean,
                host_power_watts: row.host_power_watts.mean,
                gpu_power_watts: row.gpu_power_watts.mean,
                gpu_utilization_percent: row.gpu_utilization_percent.mean,
            },
            {
                timestamp: '2026-01-01T00:00:10Z',
                cpu_usage_percent: row.cpu_usage_percent.max,
                memory_available_bytes: row.memory_available_bytes.min,
                memory_used_bytes: row.memory_used_bytes.max,
                memory_usage_percent: row.memory_usage_percent.max,
                disk_read_bytes_per_second: row.disk_read_bytes_per_second.max,
                disk_written_bytes_per_second: row.disk_written_bytes_per_second.max,
                network_receive_bytes_per_second: row.network_receive_bytes_per_second.max,
                network_transmit_bytes_per_second: row.network_transmit_bytes_per_second.max,
                host_power_watts: row.host_power_watts.max,
                gpu_power_watts: row.gpu_power_watts.max,
                gpu_utilization_percent: row.gpu_utilization_percent.max,
            },
        ];
    }
    return {
        run_manifest: { execution_started_at: '2026-01-01T00:00:00Z' },
        server_telemetry: {
            available: true,
            status: 'available',
            ...(hardwareProfile ? { hardware_profile: hardwareProfile } : {}),
            system: {
                available: true,
                nodes: perNode.map((r) => r.node),
                cpu_avg_pct: 35.0,
                cpu_peak_pct: 64.0,
                mem_total_bytes: 512 * GiB,
                mem_used_bytes: 180 * GiB,
                mem_avail_bytes: 332 * GiB,
                mem_avail_min_bytes: 312 * GiB,
                mem_usage_pct: 35.2,
                mem_peak_pct: 49.0,
                disk_avail_bytes: 750 * GiB,
                disk_total_bytes: 2000 * GiB,
                disk_read_bps: 27.5 * MiB,
                disk_write_bps: 11 * MiB,
                net_rx_bps: 275 * MiB,
                net_rx_peak_bps: 435 * MiB,
                net_tx_bps: 205 * MiB,
                net_tx_peak_bps: 325 * MiB,
                host_power_watts: 750,
                host_peak_power_watts: 950,
            },
            energy: {
                accelerator_avg_power_watts: 590,
                accelerator_peak_power_watts: 760,
                host_avg_power_watts: 750,
                host_peak_power_watts: 950,
                host_energy_wh: 12.5,
            },
            summary: {
                host_cpu_usage_percent: { mean: 35.0, max: 64.0 },
                host_memory_used_bytes: { mean: 180 * GiB, max: 210 * GiB },
                host_memory_usage_percent: { mean: 35.2, max: 49.0 },
                host_filesystem_size_bytes: { mean: 2000 * GiB },
                host_network_receive_bytes_per_second: { mean: 275 * MiB, max: 435 * MiB },
                host_network_transmit_bytes_per_second: { mean: 205 * MiB, max: 325 * MiB },
                gpu_power_watts: { mean: 590, max: 760 },
                gpu_temperature_celsius: { mean: 62.5, max: 74.0 },
            },
            per_node: perNode,
            per_node_series: perNodeSeries,
            series: [
                {
                    timestamp: '2026-01-01T00:00:00Z',
                    host_cpu_usage_percent: 30,
                    host_memory_used_bytes: 170 * GiB,
                    host_memory_available_bytes: 342 * GiB,
                    host_memory_usage_percent: 33.2,
                    host_disk_read_bytes_per_second: 25 * MiB,
                    host_disk_written_bytes_per_second: 9 * MiB,
                    host_network_receive_bytes_per_second: 240 * MiB,
                    host_network_transmit_bytes_per_second: 180 * MiB,
                    host_power_watts: 700,
                    gpu_power_watts: 540,
                },
                {
                    timestamp: '2026-01-01T00:00:10Z',
                    host_cpu_usage_percent: 58,
                    host_memory_used_bytes: 200 * GiB,
                    host_memory_available_bytes: 312 * GiB,
                    host_memory_usage_percent: 45.0,
                    host_disk_read_bytes_per_second: 38 * MiB,
                    host_disk_written_bytes_per_second: 14 * MiB,
                    host_network_receive_bytes_per_second: 390 * MiB,
                    host_network_transmit_bytes_per_second: 290 * MiB,
                    host_power_watts: 890,
                    gpu_power_watts: 710,
                },
            ],
        },
    };
}

test('SystemTab renders v7 6 KPI cards, Per-Node Summary table, hardware-neutral accelLabel, and 4 charts with multi-node overlays', () => {
    const telemetry = makeV7SystemTelemetry({ nodeCount: 2 });
    const html = renderToStaticMarkup(<SystemTab telemetry={telemetry} />);
    assertNoNestedButtons(html, 'SystemTab v7');

    // 6 KPI cards
    assert.match(html, /Host CPU Utilization/);
    assert.match(html, /35%/);
    assert.match(html, /64%/);
    assert.match(html, /Host Memory Usage/);
    assert.match(html, /180 \/ 512 GiB/);
    assert.match(html, /49%/);
    assert.match(html, /312 GiB/);
    assert.match(html, /Disk I\/O &amp; Capacity/);
    assert.match(html, /27\.50 \/ 11 MiB\/s/);
    assert.match(html, /750 GiB \/ 1\.95 TiB/);
    assert.match(html, /Network Throughput/);
    assert.match(html, /275 \/ 205 MiB\/s/);
    assert.match(html, /435 \/ 325 MiB\/s/);
    assert.match(html, /Host Power/);
    assert.match(html, /750 W/);
    assert.match(html, /950 W/);
    assert.match(html, /12\.50 Wh/);
    assert.match(html, /Gaudi Power &amp; Thermal/);
    assert.match(html, /590 W/);
    assert.match(html, /760 W/);
    assert.match(html, /74 °C/);

    // Hardware neutrality: profile-driven Gaudi label & fallback Accelerator when profile omitted
    assert.match(html, /Host &amp; Gaudi Power/);
    assert.doesNotMatch(html, /NVIDIA/i);
    const neutralHtml = renderToStaticMarkup(
        <SystemTab telemetry={makeV7SystemTelemetry({ nodeCount: 2, hardwareProfile: null })} />,
    );
    assert.match(neutralHtml, /Accelerator Power &amp; Thermal/);
    assert.match(neutralHtml, /Host &amp; Accelerator Power/);
    assert.doesNotMatch(neutralHtml, /NVIDIA|GPU/i);

    // Rich Per-Node Summary table + overflow-x-auto lg:overflow-visible
    assert.match(html, /Per-Node Summary/);
    assert.match(html, /overflow-x-auto lg:overflow-visible/);
    assert.match(html, /smc-18/);
    assert.match(html, /smc-19/);
    assert.match(html, /Prefill/);
    assert.match(html, /Decode/);
    assert.match(html, /pod-prefill-0/);
    assert.match(html, /pod-decode-1a, pod-decode-1b/);
    assert.match(html, /4x · 65% · 280 W · 70 °C/);
    assert.match(html, /8x · 75% · 310 W · 74 °C/);

    // 4 charts + multi-node CPU overlay lines
    assert.match(html, /Host CPU Utilization over Time/);
    assert.match(html, /Host Memory &amp; Disk I\/O/);
    assert.match(html, /Network RX \/ TX Throughput/);
    assert.match(html, /Aggregate CPU %/);
    assert.match(html, /Memory %/);

    // Verify chart overlay lines & LINE_TOOLTIP_CURSOR on captured React tree
    let capturedTree = null;
    function CaptureSystem() {
        capturedTree = SystemTab({ telemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureSystem />);
    const charts = [];
    const walkCharts = (node) => {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node)) {
            node.forEach(walkCharts);
            return;
        }
        if (node.type === LineChart) charts.push(node);
        walkCharts(node.props?.actions);
        walkCharts(node.props?.children);
    };
    walkCharts(capturedTree);
    assert.equal(charts.length, 4, 'Expected 4 LineCharts in SystemTab');

    for (const chart of charts) {
        const tooltip = findElement(chart, (n) => n.type === Tooltip);
        assert.ok(tooltip?.props?.cursor?.stroke, 'Expected LINE_TOOLTIP_CURSOR on chart Tooltip');
    }
    // Chart 1 has Aggregate CPU + 2 per-node overlay lines + clamped 0-100 YAxis
    const cpuYAxis = findElement(charts[0], (n) => n.type === YAxis);
    assert.deepEqual(cpuYAxis?.props?.domain, [0, 100]);
    const cpuLines = [];
    const collectLines = (node) => {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node)) {
            node.forEach(collectLines);
            return;
        }
        if (node.type === Line) cpuLines.push(node);
        collectLines(node.props?.children);
    };
    collectLines(charts[0]);
    assert.equal(cpuLines.length, 3, 'Expected Aggregate CPU % + 2 per-node overlay lines');
    assert.equal(cpuLines[1].props.strokeWidth, 1.25);
    assert.equal(cpuLines[2].props.strokeWidth, 1.25);

    // Legacy payload without per_node hides Per-Node Summary without crashing
    const legacyNoPerNode = makeV7SystemTelemetry({ nodeCount: 2 });
    delete legacyNoPerNode.server_telemetry.per_node;
    delete legacyNoPerNode.server_telemetry.per_node_series;
    const legacyHtml = renderToStaticMarkup(<SystemTab telemetry={legacyNoPerNode} />);
    assert.doesNotMatch(legacyHtml, /Per-Node Summary/);
    assert.match(legacyHtml, /Host CPU Utilization/);
});

test('SystemTab supports ToggleGroup and Select node scope drilldown, clickable Per-Node rows, and stale-node fallback', async (t) => {
    const stripChartSeries = (tel) => ({
        ...tel,
        server_telemetry: {
            ...tel.server_telemetry,
            series: [],
            per_node_series: {},
        },
    });
    const twoNodeTelemetry = stripChartSeries(makeV7SystemTelemetry({ nodeCount: 2 }));

    await withMountedDom(t, async () => Response.json({}), async ({ root, doc, clickButton }) => {
        await act(async () => {
            root.render(<SystemTab telemetry={twoNodeTelemetry} />);
        });

        // Initial All Nodes (Aggregate) view
        assert.match(doc.body.textContent, /All Nodes \(Aggregate\)/);
        assert.match(doc.body.textContent, /180 \/ 512 GiB/);
        assert.match(doc.body.textContent, /590 W/);

        // Click ToggleGroup option for smc-19 -> updates KPIs and chart headers to smc-19
        await clickButton(/^smc-19$/);
        assert.match(doc.body.textContent, /40%/);
        assert.match(doc.body.textContent, /100 \/ 256 GiB/);
        assert.match(doc.body.textContent, /150 \/ 110 MiB\/s/);
        assert.match(doc.body.textContent, /400 W/);
        assert.match(doc.body.textContent, /310 W/);
        assert.match(doc.body.textContent, /74 °C/);
        assert.match(doc.body.textContent, /Host CPU % on smc-19/);
        assert.match(doc.body.textContent, /Network RX \/ TX throughput on smc-19/);

        // Click the selected Per-Node Summary row again -> toggles back to All Nodes (Aggregate)
        const rows = findElements(doc.body, (el) => el.tagName === 'TR' && el.getAttribute('role') === 'button');
        assert.equal(rows.length, 2);
        assert.equal(rows[1].getAttribute('aria-pressed'), 'true');
        await act(async () => {
            reactProps(rows[1]).onClick?.();
        });
        assert.match(doc.body.textContent, /180 \/ 512 GiB/);
        assert.equal(rows[1].getAttribute('aria-pressed'), 'false');

        // Keyboard Enter on row 0 selects smc-18
        await act(async () => {
            reactProps(rows[0]).onKeyDown?.({ key: 'Enter', preventDefault() {} });
        });
        assert.match(doc.body.textContent, /80 \/ 256 GiB/);
        assert.match(doc.body.textContent, /Host CPU % on smc-18/);

        // If selected node (smc-18) disappears from nodeList on update, safely fall back to __all__
        const withoutNode18 = stripChartSeries(makeV7SystemTelemetry({ nodeCount: 2 }));
        withoutNode18.server_telemetry.per_node = withoutNode18.server_telemetry.per_node.filter((r) => r.node !== 'smc-18');
        withoutNode18.server_telemetry.system.nodes = ['smc-19'];
        await act(async () => {
            root.render(<SystemTab telemetry={withoutNode18} />);
        });
        assert.match(doc.body.textContent, /180 \/ 512 GiB/);
        assert.doesNotMatch(doc.body.textContent, /Host CPU % on smc-18/);

        // > 5 nodes renders Select dropdown instead of ToggleGroup for node scope
        const sixNodeTelemetry = stripChartSeries(makeV7SystemTelemetry({ nodeCount: 6 }));
        await act(async () => {
            root.render(<SystemTab telemetry={sixNodeTelemetry} />);
        });
        const selectEl = findElements(
            doc.body,
            (el) => el.tagName === 'SELECT' && el.getAttribute('aria-label') === 'Node scope',
        )[0];
        assert.ok(selectEl, 'Expected <Select aria-label="Node scope"> when nodeList.length > 5');
        assert.equal(selectEl.options.length, 7);
        await act(async () => {
            reactProps(selectEl).onChange?.({ target: { value: 'smc-23' } });
        });
        assert.match(doc.body.textContent, /Host CPU % on smc-23/);
        assert.match(doc.body.textContent, /180 \/ 256 GiB/);
    });
});

test('SystemTab renders CPU empty state cleanly when hostSeries is empty and 2-node per_node_series has null CPU/memory percentages', () => {
    const partialPerNodeOnly = makeV7SystemTelemetry({ nodeCount: 2 });
    partialPerNodeOnly.server_telemetry.series = [];
    partialPerNodeOnly.server_telemetry.per_node_series = {
        'smc-18': [
            {
                timestamp: '2026-01-01T00:00:00Z',
                cpu_usage_percent: null,
                memory_available_bytes: null,
                memory_used_bytes: null,
                memory_usage_percent: null,
                network_receive_bytes_per_second: 120 * (1024 ** 2),
                network_transmit_bytes_per_second: 90 * (1024 ** 2),
                host_power_watts: 320,
                gpu_power_watts: 260,
            },
        ],
        'smc-19': [
            {
                timestamp: '2026-01-01T00:00:00Z',
                cpu_usage_percent: null,
                memory_available_bytes: null,
                memory_used_bytes: null,
                memory_usage_percent: null,
                network_receive_bytes_per_second: 140 * (1024 ** 2),
                network_transmit_bytes_per_second: 100 * (1024 ** 2),
                host_power_watts: 340,
                gpu_power_watts: 280,
            },
        ],
    };

    const html = renderToStaticMarkup(<SystemTab telemetry={partialPerNodeOnly} />);
    assert.match(html, /Host CPU metric unavailable/);
    assert.match(html, /Memory &amp; disk I\/O unavailable/);
    assert.match(html, /Network throughput unavailable/);
});

test('SystemTab synchronizes clickable Target Nodes badges, top Scope, and Charts Scope bar across all 4 chart families', async (t) => {
    const stripChartSeries = (tel) => ({
        ...tel,
        server_telemetry: {
            ...tel.server_telemetry,
            series: [],
            per_node_series: {},
        },
    });
    const fullTwoNodeTelemetry = makeV7SystemTelemetry({ nodeCount: 2 });
    assertNoNestedButtons(renderToStaticMarkup(<SystemTab telemetry={fullTwoNodeTelemetry} />), 'SystemTab 2-node');

    const twoNodeTelemetry = stripChartSeries(fullTwoNodeTelemetry);

    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        await act(async () => {
            root.render(<SystemTab telemetry={twoNodeTelemetry} />);
        });

        assert.equal(
            findElements(doc.body, (el) => el.tagName === 'BUTTON' && el.childNodes.some((c) => findElements(c, (n) => n.tagName === 'BUTTON').length > 0)).length,
            0,
            'Expected zero nested <button> tags in mounted SystemTab DOM',
        );
        assert.match(doc.body.textContent, /Charts Scope:/);

        // Target Nodes clickable <button> wrapping <Badge> (<span>)
        const targetNodeBtns = findElements(
            doc.body,
            (el) => el.tagName === 'BUTTON' && el.hasAttribute('aria-pressed') && /^smc-(18|19)$/.test(el.textContent.trim())
                && findElements(el, (child) => child.tagName === 'SPAN').length > 0,
        );
        // Note: ToggleGroup options also render <button aria-pressed>; distinguish Target Nodes buttons (no lens-toggle-option class)
        const targetBadgeBtns = targetNodeBtns.filter(
            (el) => !String(el.getAttribute('class') || '').includes('lens-toggle-option'),
        );
        const toggleGroups = findElements(
            doc.body,
            (el) => el.getAttribute('role') === 'group' && String(el.getAttribute('class') || '').includes('lens-toggle-group'),
        );
        assert.equal(targetBadgeBtns.length, 2, 'Expected 2 clickable Target Nodes badge buttons');
        assert.ok(toggleGroups.length >= 2, 'Expected both top Scope and Charts Scope ToggleGroups');
        const topScopeGroup = toggleGroups[0];
        const chartsScopeGroup = toggleGroups[toggleGroups.length - 1];

        const pressedLabel = (groupEl) => findElements(
            groupEl,
            (el) => el.tagName === 'BUTTON' && el.getAttribute('aria-pressed') === 'true',
        )[0]?.textContent.trim();

        assert.equal(pressedLabel(topScopeGroup), 'All Nodes (Aggregate)');
        assert.equal(pressedLabel(chartsScopeGroup), 'All Nodes (Aggregate)');

        // Click Target Nodes badge for smc-18 -> synchronizes top Scope, Charts Scope, Per-Node row, and all 4 chart subtitles
        await act(async () => {
            reactProps(targetBadgeBtns[0]).onClick?.();
        });
        assert.equal(targetBadgeBtns[0].getAttribute('aria-pressed'), 'true');
        assert.equal(targetBadgeBtns[1].getAttribute('aria-pressed'), 'false');
        assert.equal(pressedLabel(topScopeGroup), 'smc-18');
        assert.equal(pressedLabel(chartsScopeGroup), 'smc-18');
        assert.match(doc.body.textContent, /Host CPU % on smc-18/);
        assert.match(doc.body.textContent, /Host memory utilization \(%\) · smc-18|Host used vs available memory · smc-18/);
        assert.match(doc.body.textContent, /Network RX \/ TX throughput on smc-18/);
        assert.match(doc.body.textContent, /Host vs gaudi power \(W\) on smc-18/);

        // Click Charts Scope button for smc-19 -> synchronizes back to Target Nodes badge & top Scope
        const chartsSmc19Btn = findElements(
            chartsScopeGroup,
            (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'smc-19',
        )[0];
        assert.ok(chartsSmc19Btn);
        await act(async () => {
            reactProps(chartsSmc19Btn).onClick?.();
        });
        assert.equal(targetBadgeBtns[1].getAttribute('aria-pressed'), 'true');
        assert.equal(pressedLabel(topScopeGroup), 'smc-19');
        assert.equal(pressedLabel(chartsScopeGroup), 'smc-19');
        assert.match(doc.body.textContent, /Host CPU % on smc-19/);
        assert.match(doc.body.textContent, /Network RX \/ TX throughput on smc-19/);
        assert.match(doc.body.textContent, /Host vs gaudi power \(W\) on smc-19/);

        // Clicking active Target Nodes badge smc-19 toggles back to All Nodes (Aggregate)
        await act(async () => {
            reactProps(targetBadgeBtns[1]).onClick?.();
        });
        assert.equal(targetBadgeBtns[1].getAttribute('aria-pressed'), 'false');
        assert.equal(pressedLabel(topScopeGroup), 'All Nodes (Aggregate)');
        assert.equal(pressedLabel(chartsScopeGroup), 'All Nodes (Aggregate)');

        // > 5 nodes keeps Target Nodes clickable badges AND renders synchronized top + Charts Scope <Select> dropdowns
        const sixNodeTelemetry = stripChartSeries(makeV7SystemTelemetry({ nodeCount: 6 }));
        await act(async () => {
            root.render(<SystemTab telemetry={sixNodeTelemetry} />);
        });
        const topSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('aria-label') === 'Node scope')[0];
        const chartsSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('aria-label') === 'Charts node scope')[0];
        assert.ok(topSelect, 'Expected <Select aria-label="Node scope"> for >5 nodes');
        assert.ok(chartsSelect, 'Expected <Select aria-label="Charts node scope"> for >5 nodes');
        assert.equal(topSelect.options.length, 7);
        assert.equal(chartsSelect.options.length, 7);

        await act(async () => {
            reactProps(chartsSelect).onChange?.({ target: { value: 'smc-22' } });
        });
        assert.equal(reactProps(topSelect).value, 'smc-22');
        assert.equal(reactProps(chartsSelect).value, 'smc-22');
        assert.match(doc.body.textContent, /Host CPU % on smc-22/);
        assert.match(doc.body.textContent, /Network RX \/ TX throughput on smc-22/);
    });
});

test('SystemTab renders per-NIC Network RX/TX breakdown, dotted subinterfaces, 5-color By NIC cap, stale-NIC reset, and legacy fallback', async (t) => {
    const MiB = 1024 ** 2;

    // 1. Legacy payload without network_interfaces shows subtle hint and omits NIC selector
    const legacyTelemetry = makeV7SystemTelemetry({ nodeCount: 2 });
    const legacyHtml = renderToStaticMarkup(<SystemTab telemetry={legacyTelemetry} />);
    assert.match(legacyHtml, /Per-interface breakdown is unavailable for this telemetry snapshot \(showing aggregate RX \/ TX\)\./);
    assert.doesNotMatch(legacyHtml, /aria-label="Network interface"/);
    assert.doesNotMatch(legacyHtml, /All NICs \(Total\)/);

    // 2. Per-NIC v10 payload with 6 NICs (including dotted VLAN & InfiniBand subinterfaces) on smc-18 and 1 NIC on smc-19
    const sixNicsSummary = [
        { device: 'ib0', rx_bytes_per_second: { mean: 200 * MiB, max: 300 * MiB }, tx_bytes_per_second: { mean: 160 * MiB, max: 250 * MiB } },
        { device: 'eno1.100', rx_bytes_per_second: { mean: 80 * MiB, max: 120 * MiB }, tx_bytes_per_second: { mean: 60 * MiB, max: 90 * MiB } },
        { device: 'ib0.8001', rx_bytes_per_second: { mean: 50 * MiB, max: 75 * MiB }, tx_bytes_per_second: { mean: 40 * MiB, max: 60 * MiB } },
        { device: 'ens1f0', rx_bytes_per_second: { mean: 30 * MiB, max: 45 * MiB }, tx_bytes_per_second: { mean: 20 * MiB, max: 35 * MiB } },
        { device: 'bond0.200', rx_bytes_per_second: { mean: 12 * MiB, max: 18 * MiB }, tx_bytes_per_second: { mean: 8 * MiB, max: 12 * MiB } },
        { device: 'enp5s0', rx_bytes_per_second: { mean: 4 * MiB, max: 6 * MiB }, tx_bytes_per_second: { mean: 2 * MiB, max: 4 * MiB } },
    ];
    const smc18SeriesNics = {
        ib0: { rx: 200 * MiB, tx: 160 * MiB },
        'eno1.100': { rx: 50 * MiB, tx: 40 * MiB },
        'ib0.8001': { rx: 50 * MiB, tx: 40 * MiB },
        ens1f0: { rx: 30 * MiB, tx: 20 * MiB },
        'bond0.200': { rx: 12 * MiB, tx: 8 * MiB },
        enp5s0: { rx: 4 * MiB, tx: 2 * MiB },
    };
    const smc19SeriesNics = {
        'eno1.100': { rx: 30 * MiB, tx: 20 * MiB },
    };

    const nicTelemetry = makeV7SystemTelemetry({ nodeCount: 2 });
    nicTelemetry.server_telemetry.system.network_interfaces = sixNicsSummary.map((item) => ({ ...item, node_count: 2 }));
    nicTelemetry.server_telemetry.per_node[0].network_interfaces = sixNicsSummary;
    nicTelemetry.server_telemetry.per_node[1].network_interfaces = [
        { device: 'eno1.100', rx_bytes_per_second: { mean: 30 * MiB, max: 45 * MiB }, tx_bytes_per_second: { mean: 20 * MiB, max: 30 * MiB } },
    ];
    nicTelemetry.server_telemetry.per_node_series['smc-18'] = nicTelemetry.server_telemetry.per_node_series['smc-18'].map((pt) => ({
        ...pt,
        network_interfaces: smc18SeriesNics,
    }));
    nicTelemetry.server_telemetry.per_node_series['smc-19'] = nicTelemetry.server_telemetry.per_node_series['smc-19'].map((pt) => ({
        ...pt,
        network_interfaces: smc19SeriesNics,
    }));
    // Leave top-level series without network_interfaces first to verify All-Nodes per_node_series fallback aggregation
    const fallbackHtml = renderToStaticMarkup(<SystemTab telemetry={nicTelemetry} />);
    assertNoNestedButtons(fallbackHtml, 'SystemTab per-NIC');
    assert.match(fallbackHtml, /aria-label="Network interface"/);

    // Populate series network_interfaces too for full v10 fidelity
    nicTelemetry.server_telemetry.series = nicTelemetry.server_telemetry.series.map((pt) => ({
        ...pt,
        network_interfaces: {
            ...smc18SeriesNics,
            'eno1.100': { rx: 80 * MiB, tx: 60 * MiB },
        },
    }));

    // Verify full-series SSR rendering (All NICs, By NIC 5-slot CHART_SERIES cap + Other, and dotted eno1.100 flat dataKeys)
    const fullNicHtml = renderToStaticMarkup(<SystemTab telemetry={nicTelemetry} />);
    assertNoNestedButtons(fullNicHtml, 'SystemTab full per-NIC');
    assert.match(fullNicHtml, /Network RX \(MiB\/s\)/);
    assert.match(fullNicHtml, /Network TX \(MiB\/s\)/);
    assert.doesNotMatch(fullNicHtml, /Per-interface breakdown is unavailable/);

    const byNicHtml = renderToStaticMarkup(<SystemTab telemetry={nicTelemetry} initialNic="__by_nic__" />);
    assertNoNestedButtons(byNicHtml, 'SystemTab By NIC');
    assert.match(byNicHtml, /Top 4 interfaces \+ Other RX \(solid\) and TX \(dashed\) throughput \(MiB\/s\) · All Nodes \(Aggregate\)/);
    assert.match(byNicHtml, /ib0 \(RX solid · TX dashed\)/);
    assert.match(byNicHtml, /eno1\.100 \(RX solid · TX dashed\)/);
    assert.match(byNicHtml, /ib0\.8001 \(RX solid · TX dashed\)/);
    assert.match(byNicHtml, /ens1f0 \(RX solid · TX dashed\)/);
    assert.match(byNicHtml, /Other \(RX solid · TX dashed\)/);
    assert.doesNotMatch(byNicHtml, /bond0\.200 \(RX solid/);
    assert.doesNotMatch(byNicHtml, /enp5s0 \(RX solid/);

    let byNicTree = null;
    function CaptureByNic() {
        byNicTree = SystemTab({ telemetry: nicTelemetry, initialNic: '__by_nic__' });
        return null;
    }
    renderToStaticMarkup(<CaptureByNic />);
    const byNicNetChart = findAllElements(byNicTree, (n) => n.type === LineChart)[2];
    const byNicLines = findAllElements(byNicNetChart, (n) => n.type === Line);
    assert.equal(byNicLines.length, 10, 'Expected 5 slots × (RX + TX) lines in By NIC mode');
    assert.ok(byNicLines.every((l) => !String(l.props.dataKey).includes('.')), 'Expected flat synthetic dataKeys without dots');
    assert.deepEqual(
        byNicLines.filter((_, i) => i % 2 === 0).map((l) => l.props.stroke),
        CHART_SERIES.slice(0, 5),
    );
    assert.ok(byNicLines.filter((_, i) => i % 2 === 1).every((l) => l.props.strokeDasharray === '4 2'));
    assert.equal(byNicNetChart.props.data[0].nic_4_rx, 16, 'Expected Other RX to sum bond0.200 (12) + enp5s0 (4)');
    assert.equal(byNicNetChart.props.data[0].nic_4_tx, 10, 'Expected Other TX to sum bond0.200 (8) + enp5s0 (2)');

    let dottedNicTree = null;
    function CaptureDottedNic() {
        dottedNicTree = SystemTab({ telemetry: nicTelemetry, initialNic: 'eno1.100' });
        return null;
    }
    renderToStaticMarkup(<CaptureDottedNic />);
    const dottedNetChart = findAllElements(dottedNicTree, (n) => n.type === LineChart)[2];
    const dottedLines = findAllElements(dottedNetChart, (n) => n.type === Line);
    assert.deepEqual(dottedLines.map((l) => l.props.dataKey), ['sel_nic_rx', 'sel_nic_tx']);
    assert.equal(dottedNetChart.props.data[0].sel_nic_rx, 80);
    assert.equal(dottedNetChart.props.data[0].sel_nic_tx, 60);

    const mountedNicTelemetry = {
        ...nicTelemetry,
        server_telemetry: {
            ...nicTelemetry.server_telemetry,
            series: [],
            per_node_series: {},
        },
    };

    await withMountedDom(t, async () => Response.json({}), async ({ root, doc, clickButton }) => {
        await act(async () => {
            root.render(<SystemTab telemetry={mountedNicTelemetry} />);
        });

        assert.doesNotMatch(doc.body.textContent, /Per-interface breakdown is unavailable/);
        const nicSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('aria-label') === 'Network interface')[0];
        assert.ok(nicSelect, 'Expected <Select aria-label="Network interface"> when >2 NICs');
        assert.deepEqual(
            nicSelect.options.map((o) => o.textContent),
            ['All NICs (Total)', 'By NIC', 'ib0', 'eno1.100', 'ib0.8001', 'ens1f0', 'bond0.200', 'enp5s0'],
        );

        // Select By NIC -> updates Network ChartContainer subtitle with top 4 + Other
        await act(async () => {
            reactProps(nicSelect).onChange?.({ target: { value: '__by_nic__' } });
        });
        assert.match(doc.body.textContent, /Top 4 interfaces \+ Other RX \(solid\) and TX \(dashed\) throughput · All Nodes \(Aggregate\)/);

        // Select dotted VLAN subinterface eno1.100 -> updates subtitle
        await act(async () => {
            reactProps(nicSelect).onChange?.({ target: { value: 'eno1.100' } });
        });
        assert.match(doc.body.textContent, /Interface eno1\.100 RX \/ TX throughput · All Nodes \(Aggregate\)/);

        // Select ib0, then switch node scope to smc-19 (which only has eno1.100) -> stale NIC resets to All NICs (Total) & switches to ToggleGroup
        await act(async () => {
            reactProps(nicSelect).onChange?.({ target: { value: 'ib0' } });
        });
        assert.match(doc.body.textContent, /Interface ib0 RX \/ TX throughput/);

        await clickButton(/^smc-19$/);
        assert.match(doc.body.textContent, /Network RX \/ TX throughput on smc-19/);
        assert.doesNotMatch(doc.body.textContent, /Interface ib0/);
        // smc-19 has 1 NIC (eno1.100) -> ToggleGroup with All NICs (Total) and eno1.100 (no By NIC option)
        const smc19NicSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('aria-label') === 'Network interface')[0];
        assert.equal(smc19NicSelect, undefined);
        assert.match(doc.body.textContent, /All NICs \(Total\)/);
        await clickButton(/^eno1\.100$/);
        assert.match(doc.body.textContent, /Interface eno1\.100 RX \/ TX throughput · smc-19/);
    });
});

test('SystemTab safely handles prototype-colliding node and device keys without mutating Object or Object.prototype', async (t) => {
    const MiB = 1024 ** 2;
    const base = makeV7SystemTelemetry({ nodeCount: 2 });
    const parsedNics = JSON.parse(
        `{"__proto__":{"rx":${20 * MiB},"tx":${10 * MiB}},"constructor":{"rx":${12 * MiB},"tx":${6 * MiB}}}`,
    );
    const parsedPerNodeSeries = JSON.parse(
        JSON.stringify(
            Object.fromEntries([
                [
                    '__proto__',
                    [
                        {
                            timestamp: '2026-01-01T00:00:00Z',
                            cpu_usage_percent: 38,
                            network_interfaces: parsedNics,
                        },
                        {
                            timestamp: '2026-01-01T00:00:05Z',
                            cpu_usage_percent: 42,
                            network_interfaces: parsedNics,
                        },
                    ],
                ],
            ]),
        ),
    );
    assert.equal(Object.prototype.hasOwnProperty.call(parsedNics, '__proto__'), true);
    assert.equal(Object.prototype.hasOwnProperty.call(parsedPerNodeSeries, '__proto__'), true);
    const protoTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            system: {
                ...base.server_telemetry.system,
                nodes: ['__proto__', 'constructor'],
                network_interfaces: [
                    { device: '__proto__', rx_bytes_per_second: { mean: 20 * MiB, max: 25 * MiB }, tx_bytes_per_second: { mean: 10 * MiB, max: 12 * MiB } },
                    { device: 'constructor', rx_bytes_per_second: { mean: 12 * MiB, max: 15 * MiB }, tx_bytes_per_second: { mean: 6 * MiB, max: 8 * MiB } },
                ],
                series: base.server_telemetry.series.map((pt) => {
                    const copy = { ...pt };
                    delete copy.network_interfaces;
                    return copy;
                }),
            },
            per_node: [
                {
                    ...base.server_telemetry.per_node[0],
                    node: '__proto__',
                    network_interfaces: [
                        { device: '__proto__', rx_bytes_per_second: { mean: 20 * MiB, max: 25 * MiB }, tx_bytes_per_second: { mean: 10 * MiB, max: 12 * MiB } },
                        { device: 'constructor', rx_bytes_per_second: { mean: 12 * MiB, max: 15 * MiB }, tx_bytes_per_second: { mean: 6 * MiB, max: 8 * MiB } },
                    ],
                },
                {
                    ...base.server_telemetry.per_node[1],
                    node: 'constructor',
                    network_interfaces: [
                        { device: 'eth0', rx_bytes_per_second: { mean: 4 * MiB, max: 5 * MiB }, tx_bytes_per_second: { mean: 2 * MiB, max: 3 * MiB } },
                    ],
                },
            ],
            per_node_series: parsedPerNodeSeries,
        },
    };

    const allHtml = renderToStaticMarkup(<SystemTab telemetry={protoTelemetry} />);
    assertNoNestedButtons(allHtml, 'SystemTab proto all');
    const byNicHtml = renderToStaticMarkup(<SystemTab telemetry={protoTelemetry} initialNic="__by_nic__" />);
    assertNoNestedButtons(byNicHtml, 'SystemTab proto by_nic');
    const protoNicHtml = renderToStaticMarkup(<SystemTab telemetry={protoTelemetry} initialNic="__proto__" />);
    assertNoNestedButtons(protoNicHtml, 'SystemTab proto single_nic __proto__');
    const ctorNicHtml = renderToStaticMarkup(<SystemTab telemetry={protoTelemetry} initialNic="constructor" />);
    assertNoNestedButtons(ctorNicHtml, 'SystemTab proto single_nic constructor');
    const ctorNodeHtml = renderToStaticMarkup(<SystemTab telemetry={protoTelemetry} initialNode="constructor" />);
    assertNoNestedButtons(ctorNodeHtml, 'SystemTab proto node constructor');
    const protoNodeHtml = renderToStaticMarkup(<SystemTab telemetry={protoTelemetry} initialNode="__proto__" />);
    assertNoNestedButtons(protoNodeHtml, 'SystemTab proto node __proto__');

    let protoNicTree = null;
    function CaptureProtoNic() {
        protoNicTree = SystemTab({ telemetry: protoTelemetry, initialNic: '__proto__' });
        return null;
    }
    renderToStaticMarkup(<CaptureProtoNic />);
    const protoNetChart = findAllElements(protoNicTree, (n) => n.type === LineChart)[2];
    assert.equal(protoNetChart.props.data[0].sel_nic_rx, 20);
    assert.equal(protoNetChart.props.data[0].sel_nic_tx, 10);

    assert.equal(Object.prototype.rx_mbps, undefined);
    assert.equal(Object.prototype.tx_mbps, undefined);
    assert.equal(Object.prototype.rx, undefined);
    assert.equal(Object.rx_mbps, undefined);
    assert.equal(Object.tx_mbps, undefined);
    assert.equal(Object.rx, undefined);

    const mountedProtoTelemetry = {
        ...protoTelemetry,
        server_telemetry: {
            ...protoTelemetry.server_telemetry,
            system: {
                ...protoTelemetry.server_telemetry.system,
                series: [],
            },
            series: [],
            per_node_series: {},
        },
    };

    await withMountedDom(t, async () => Response.json({}), async ({ root, doc, clickButton }) => {
        await act(async () => {
            root.render(<SystemTab telemetry={mountedProtoTelemetry} />);
        });
        await clickButton(/^By NIC$/);
        await clickButton(/^constructor$/);
        assert.match(doc.body.textContent, /Host CPU % on constructor/);
        await clickButton(/^__proto__$/);
        assert.match(doc.body.textContent, /Host CPU % on __proto__/);
        const nicProtoBtns = findElements(
            doc.body,
            (el) => el.tagName === 'BUTTON' && el.textContent.trim() === '__proto__',
        );
        const lastProtoBtn = nicProtoBtns[nicProtoBtns.length - 1];
        assert.ok(lastProtoBtn, 'Expected __proto__ NIC button');
        await act(async () => {
            await reactProps(lastProtoBtn).onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
        assert.match(doc.body.textContent, /Interface __proto__ RX \/ TX throughput/);
        assert.equal(Object.prototype.rx_mbps, undefined);
        assert.equal(Object.prototype.tx_mbps, undefined);
        assert.equal(Object.rx_mbps, undefined);
        assert.equal(Object.tx_mbps, undefined);
    });
});

test('SystemTab scales KPIs, Per-Node Summary table, and charts adaptively across KiB/MiB/GiB/TiB/PiB and KiB/s..GiB/s (t119)', async (t) => {
    const KiB = 1024;
    const MiB = 1024 ** 2;
    const GiB = 1024 ** 3;
    const TiB = 1024 ** 4;
    const PiB = 1024 ** 5;

    // 1. Multi-terabyte / GiB/s / KiB/s cluster payload
    const largeTelemetry = {
        run_manifest: { execution_started_at: '2026-01-01T00:00:00Z' },
        server_telemetry: {
            available: true,
            status: 'available',
            system: {
                available: true,
                nodes: ['big-mem-node', 'small-io-node'],
                cpu_avg_pct: 42,
                cpu_peak_pct: 70,
                mem_total_bytes: 4 * TiB,
                mem_used_bytes: 1.5 * TiB,
                mem_avail_bytes: 2.5 * TiB,
                mem_avail_min_bytes: 1.25 * TiB,
                mem_peak_pct: 68.75,
                disk_avail_bytes: 2.5 * TiB,
                disk_total_bytes: 8 * TiB,
                disk_read_bps: 1.5 * GiB,
                disk_write_bps: 2 * GiB,
                net_rx_bps: 12.5 * GiB,
                net_tx_bps: 8.2 * GiB,
                net_rx_peak_bps: 20 * GiB,
                net_tx_peak_bps: 15 * GiB,
                host_power_watts: 1200,
                host_peak_power_watts: 1450,
            },
            per_node: [
                {
                    node: 'big-mem-node',
                    roles: ['prefill'],
                    pod_count: 1,
                    pods: ['prefill-0'],
                    cpu_usage_percent: { mean: 50, max: 70 },
                    memory_used_bytes: { mean: 1.5 * TiB },
                    memory_total_bytes: 2 * TiB,
                    memory_available_bytes: { mean: 0.5 * TiB, min: 0.25 * TiB },
                    memory_usage_percent: { max: 87.5 },
                    disk_read_bytes_per_second: { mean: 1.5 * GiB },
                    disk_written_bytes_per_second: { mean: 2 * GiB },
                    filesystem_avail_bytes: 2.5 * TiB,
                    filesystem_size_bytes: 4 * TiB,
                    network_receive_bytes_per_second: { mean: 12.5 * GiB, max: 20 * GiB },
                    network_transmit_bytes_per_second: { mean: 8.2 * GiB, max: 15 * GiB },
                    network_interfaces: [
                        { device: 'ib0', rx_bytes_per_second: { mean: 12.5 * GiB, max: 20 * GiB }, tx_bytes_per_second: { mean: 8.2 * GiB, max: 15 * GiB } },
                        { device: 'eth0', rx_bytes_per_second: { mean: 120 * KiB, max: 240 * KiB }, tx_bytes_per_second: { mean: 64 * KiB, max: 128 * KiB } },
                    ],
                },
                {
                    node: 'small-io-node',
                    roles: ['decode'],
                    pod_count: 1,
                    pods: ['decode-0'],
                    cpu_usage_percent: { mean: 34, max: 55 },
                    memory_used_bytes: { mean: 512 * GiB },
                    memory_total_bytes: 2 * TiB,
                    memory_available_bytes: { mean: 1.5 * TiB, min: 1.25 * TiB },
                    memory_usage_percent: { max: 37.5 },
                    disk_read_bytes_per_second: { mean: 120 * KiB },
                    disk_written_bytes_per_second: { mean: 45 * MiB },
                    filesystem_avail_bytes: 1.25 * PiB,
                    filesystem_size_bytes: 4 * PiB,
                    network_receive_bytes_per_second: { mean: 120 * KiB, max: 240 * KiB },
                    network_transmit_bytes_per_second: { mean: 240 * KiB, max: 512 * KiB },
                    network_interfaces: [
                        { device: 'eth0', rx_bytes_per_second: { mean: 120 * KiB, max: 240 * KiB }, tx_bytes_per_second: { mean: 240 * KiB, max: 512 * KiB } },
                    ],
                },
            ],
            series: [
                {
                    elapsed_seconds: 0,
                    host_cpu_usage_percent: 42,
                    host_memory_used_bytes: 1.5 * TiB,
                    host_memory_available_bytes: 2.5 * TiB,
                    host_disk_read_bytes_per_second: 1.5 * GiB,
                    host_disk_written_bytes_per_second: 2 * GiB,
                    host_network_receive_bytes_per_second: 12.5 * GiB,
                    host_network_transmit_bytes_per_second: 8.2 * GiB,
                    network_interfaces: {
                        ib0: { rx: 12.5 * GiB, tx: 8.2 * GiB },
                        eth0: { rx: 120 * KiB, tx: 64 * KiB },
                    },
                },
            ],
            per_node_series: {
                'big-mem-node': [
                    {
                        elapsed_seconds: 0,
                        cpu_usage_percent: 50,
                        memory_used_bytes: 1.5 * TiB,
                        memory_available_bytes: 512 * GiB,
                        disk_read_bytes_per_second: 1.5 * GiB,
                        disk_written_bytes_per_second: 2 * GiB,
                        network_receive_bytes_per_second: 12.5 * GiB,
                        network_transmit_bytes_per_second: 8.2 * GiB,
                        network_interfaces: {
                            ib0: { rx: 12.5 * GiB, tx: 8.2 * GiB },
                            eth0: { rx: 120 * KiB, tx: 64 * KiB },
                        },
                    },
                ],
                'small-io-node': [
                    {
                        elapsed_seconds: 0,
                        cpu_usage_percent: 34,
                        memory_used_bytes: 512 * GiB,
                        memory_available_bytes: 1.5 * TiB,
                        disk_read_bytes_per_second: 120 * KiB,
                        disk_written_bytes_per_second: 45 * MiB,
                        network_receive_bytes_per_second: 120 * KiB,
                        network_transmit_bytes_per_second: 240 * KiB,
                        network_interfaces: {
                            eth0: { rx: 120 * KiB, tx: 240 * KiB },
                        },
                    },
                ],
            },
        },
    };

    const html = renderToStaticMarkup(<SystemTab telemetry={largeTelemetry} />);
    // KPI cards scale to TiB and GiB/s with t108 0-dec int / 2-dec float precision
    assert.match(html, /1\.50 \/ 4 TiB/);
    assert.match(html, /1\.25 TiB/);
    assert.match(html, /1\.50 \/ 2 GiB\/s/);
    assert.match(html, /2\.50 \/ 8 TiB/);
    assert.match(html, /12\.50 \/ 8\.20 GiB\/s/);
    assert.match(html, /20 \/ 15 GiB\/s/);

    // Per-Node Summary headers remove hardcoded (MiB/s); cells show mixed & PiB units cleanly
    assert.match(html, /<span>Disk I\/O \(R \/ W\)<\/span>/);
    assert.match(html, /<span>Network \(RX \/ TX\)<\/span>/);
    assert.doesNotMatch(html, /Disk I\/O \(R \/ W MiB\/s\)/);
    assert.doesNotMatch(html, /Network \(RX \/ TX MiB\/s\)/);
    assert.match(html, /512 GiB \/ 2 TiB · 37\.50%/);
    assert.match(html, /120 KiB\/s \/ 45 MiB\/s/);
    assert.match(html, /1\.25 PiB/);
    assert.match(html, /120 \/ 240 KiB\/s/);

    // Chart subtitles, Y-axis labels, ChartLegends, scaled series data, and Tooltips
    assert.match(html, /Host used vs available memory \(TiB\) · All Nodes \(Aggregate\)/);
    assert.match(html, /Used Memory \(TiB\)/);
    assert.match(html, /Available Memory \(TiB\)/);
    assert.match(html, /Host network receive and transmit throughput \(GiB\/s\)/);
    assert.match(html, /Network RX \(GiB\/s\)/);
    assert.match(html, /Network TX \(GiB\/s\)/);

    let capturedTree = null;
    function CaptureAllNodes() {
        capturedTree = SystemTab({ telemetry: largeTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureAllNodes />);
    const lineCharts = findAllElements(capturedTree, (n) => n.type === LineChart);
    assert.equal(lineCharts.length, 3, 'Expected 3 active LineCharts (CPU, Memory, Network)');

    const memYAxis = findElement(lineCharts[1], (n) => n.type === YAxis);
    assert.equal(memYAxis.props.label.value, 'TiB');
    assert.equal(memYAxis.props.tickFormatter(0.30000000000000004), '0.30');
    assert.equal(memYAxis.props.tickFormatter(1000), '1,000');
    assert.equal(memYAxis.props.tickFormatter(null), '');
    assert.equal(lineCharts[1].props.data[0].mem_used, 1.5);
    assert.equal(lineCharts[1].props.data[0].mem_avail, 2.5);
    const memTooltip = findElement(lineCharts[1], (n) => n.type === Tooltip);
    const memTipHtml = renderToStaticMarkup(React.cloneElement(memTooltip.props.content, {
        active: true,
        label: 0,
        payload: [
            { dataKey: 'mem_used', name: 'Used Memory', color: CHART_SERIES[0], value: 1.5 },
            { dataKey: 'mem_avail', name: 'Available Memory', color: CHART_SERIES[1], value: 2.5 },
        ],
    }));
    assert.match(memTipHtml, /1\.50 TiB/);
    assert.match(memTipHtml, /2\.50 TiB/);

    // Binary values in [1000..1023.99] must NOT compact into SI K inside UnitTooltip
    const nonCompactedTipHtml = renderToStaticMarkup(React.cloneElement(memTooltip.props.content, {
        active: true,
        label: 0,
        unit: 'GiB',
        payload: [
            { dataKey: 'mem_used', name: 'Used Memory', color: CHART_SERIES[0], value: 1000 },
            { dataKey: 'mem_avail', name: 'Available Memory', color: CHART_SERIES[1], value: 1012.5 },
        ],
    }));
    assert.match(nonCompactedTipHtml, /1,000 GiB/);
    assert.match(nonCompactedTipHtml, /1,012\.50 GiB/);
    assert.doesNotMatch(nonCompactedTipHtml, /1K GiB|1\.01K GiB/);

    const netYAxis = findElement(lineCharts[2], (n) => n.type === YAxis);
    assert.equal(netYAxis.props.label.value, 'GiB/s');
    assert.equal(netYAxis.props.tickFormatter(12.5000000000001), '12.50');
    assert.equal(lineCharts[2].props.data[0].net_rx, 12.5);
    assert.equal(lineCharts[2].props.data[0].net_tx, 8.2);
    const netTooltip = findElement(lineCharts[2], (n) => n.type === Tooltip);
    const netTipHtml = renderToStaticMarkup(React.cloneElement(netTooltip.props.content, {
        active: true,
        label: 0,
        payload: [
            { dataKey: 'net_rx', name: 'Network RX', color: CHART_SERIES[0], value: 12.5 },
            { dataKey: 'net_tx', name: 'Network TX', color: CHART_SERIES[1], value: 8.2 },
        ],
    }));
    assert.match(netTipHtml, /12\.50 GiB\/s/);
    assert.match(netTipHtml, /8\.20 GiB\/s/);

    // Single-NIC eth0 scales dynamically down to KiB/s
    let eth0Tree = null;
    function CaptureEth0() {
        eth0Tree = SystemTab({ telemetry: largeTelemetry, initialNic: 'eth0' });
        return null;
    }
    renderToStaticMarkup(<CaptureEth0 />);
    const eth0Html = renderToStaticMarkup(<SystemTab telemetry={largeTelemetry} initialNic="eth0" />);
    assert.match(eth0Html, /Interface eth0 RX \/ TX throughput \(KiB\/s\) · All Nodes \(Aggregate\)/);
    assert.match(eth0Html, /eth0 RX \(KiB\/s\)/);
    assert.match(eth0Html, /eth0 TX \(KiB\/s\)/);
    const eth0NetChart = findAllElements(eth0Tree, (n) => n.type === LineChart)[2];
    assert.equal(findElement(eth0NetChart, (n) => n.type === YAxis).props.label.value, 'KiB/s');
    assert.equal(eth0NetChart.props.data[0].sel_nic_rx, 120);
    assert.equal(eth0NetChart.props.data[0].sel_nic_tx, 64);

    // By NIC mode scales across ib0 (GiB/s) + eth0 (KiB/s) using dominant GiB/s scale
    let byNicTree = null;
    function CaptureByNicLarge() {
        byNicTree = SystemTab({ telemetry: largeTelemetry, initialNic: '__by_nic__' });
        return null;
    }
    const byNicLargeHtml = renderToStaticMarkup(<SystemTab telemetry={largeTelemetry} initialNic="__by_nic__" />);
    renderToStaticMarkup(<CaptureByNicLarge />);
    assert.match(byNicLargeHtml, /Per-interface RX \(solid\) and TX \(dashed\) throughput \(GiB\/s\) · All Nodes \(Aggregate\)/);
    const byNicLargeNetChart = findAllElements(byNicTree, (n) => n.type === LineChart)[2];
    assert.equal(findElement(byNicLargeNetChart, (n) => n.type === YAxis).props.label.value, 'GiB/s');
    assert.equal(byNicLargeNetChart.props.data[0].nic_0_rx, 12.5);

    // Per-node drilldown + interactive Disk I/O toggle on mounted DOM
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc, clickButton }) => {
        const origCreateElement = doc.createElement.bind(doc);
        doc.createElement = (tag) => {
            const node = origCreateElement(tag);
            node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
            return node;
        };
        await act(async () => {
            root.render(<SystemTab telemetry={largeTelemetry} />);
        });
        await clickButton(/^Disk I\/O$/);
        assert.match(doc.body.textContent, /Block-device read and write throughput \(GiB\/s\) · All Nodes \(Aggregate\)/);
        assert.match(doc.body.textContent, /Disk Read \(GiB\/s\)/);
        assert.match(doc.body.textContent, /Disk Write \(GiB\/s\)/);

        // Switch to small-io-node -> Disk I/O scales to MiB/s (120 KiB/s & 45 MiB/s), Network scales to KiB/s
        await clickButton(/^small-io-node$/);
        assert.match(doc.body.textContent, /512 GiB \/ 2 TiB/);
        assert.match(doc.body.textContent, /120 KiB\/s \/ 45 MiB\/s/);
        assert.match(doc.body.textContent, /120 \/ 240 KiB\/s/);
        assert.match(doc.body.textContent, /Block-device read and write throughput \(MiB\/s\) · small-io-node/);
        assert.match(doc.body.textContent, /Network RX \/ TX throughput \(KiB\/s\) on small-io-node/);
    });

    // 2. Legacy *_gb / *_mbps fields convert cleanly into adaptive TiB & GiB/s, plus single-value fallbacks
    const legacyScaledTelemetry = {
        server_telemetry: {
            available: true,
            status: 'available',
            host_metrics: {
                nodes: ['legacy-node'],
                memory_used_gb: 1536,
                disk_free_gb: 750,
                disk_read_mbps: 1536,
                disk_write_mbps: 2048,
                series: [
                    {
                        elapsed_seconds: 0,
                        host_memory_used_gb: 1536,
                        host_disk_read_mbps: 1536,
                        host_disk_write_mbps: 2048,
                    },
                ],
            },
        },
    };
    const legacyScaledHtml = renderToStaticMarkup(<SystemTab telemetry={legacyScaledTelemetry} />);
    assert.match(legacyScaledHtml, /1\.50 TiB/);
    assert.match(legacyScaledHtml, /1\.50 \/ 2 GiB\/s/);
    assert.match(legacyScaledHtml, /750 GiB/);
    assert.match(legacyScaledHtml, /Host used vs available memory \(TiB\) · All Nodes \(Aggregate\)/);

    // Avail-only fallback for memory and disk capacity
    const availOnlyTelemetry = {
        server_telemetry: {
            available: true,
            status: 'available',
            host_metrics: {
                nodes: ['avail-node'],
                memory_available_gb: 0.439453125, // 450 MiB
                disk_free_gb: 750,
            },
        },
    };
    const availOnlyHtml = renderToStaticMarkup(<SystemTab telemetry={availOnlyTelemetry} />);
    assert.match(availOnlyHtml, /450 MiB avail/);
    assert.match(availOnlyHtml, /750 GiB avail/);
});

test('RequestsTab renders clientSeries.slot_lanes with S1..Sn rows, 3-tier legend, summary strip, and hover ChartTooltip', async (t) => {
    const slotTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            warmup_seconds: 15,
            slot_lanes: {
                peak_slots: 3,
                total_requests: 5,
                session_count: 3,
                sample_stride: 2,
                tier_counts: { cold: 2, partial: 1, warm: 2 },
                items: [
                    { slot: 0, start_s: 0.5, end_s: 4.0, cache_hit_pct: 0.0, tier: 'cold', session_id: 'sess-a', turn: 1, input_tokens: 512, output_tokens: 128, ttft_ms: 210.4 },
                    { slot: 0, start_s: 5.0, end_s: 9.0, cache_hit_pct: 82.5, tier: 'warm', session_id: 'sess-a', turn: 2, input_tokens: 1024, output_tokens: 96, ttft_ms: 95.2 },
                    { slot: 1, start_s: 1.0, end_s: 6.5, cache_hit_pct: 35.0, tier: 'partial', session_id: 'sess-b', turn: 1, input_tokens: 768, output_tokens: 64, ttft_ms: 155.0 },
                    { slot: 2, start_s: 2.0, end_s: 8.0, cache_hit_pct: 5.0, tier: 'cold', session_id: 'sess-c', turn: 1, input_tokens: 400, output_tokens: 110, ttft_ms: 240.0 },
                ],
            },
        },
    });

    const html = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask()}
            telemetry={slotTelemetry}
            analysisWindow="profiling"
            onOpenErrorInspector={() => {}}
        />,
    );
    assert.match(html, /Sessions over Time/);
    assert.match(html, /One row per concurrent session slot · Requests packed into slots · a slot is reused once its last request finished · shaded span = warmup/);
    assert.match(html, /cold \(&lt;10%\)/);
    assert.match(html, /partial \(10–70%\)/);
    assert.match(html, /warm \(≥70%\)/);
    assert.match(html, /warmup/);
    assert.match(html, /3 slots at peak · 5 requests · 3 sessions · 1 in 2 sampled/);
    assert.match(html, />S1</);
    assert.match(html, />S2</);
    assert.match(html, />S3</);

    const domSlotTelemetry = {
        ...slotTelemetry,
        client_series: {
            ...slotTelemetry.client_series,
            buckets: [],
        },
    };
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        await act(async () => {
            root.render(
                <RequestsTab
                    task={makeRichTask()}
                    telemetry={domSlotTelemetry}
                    analysisWindow="profiling"
                    onOpenErrorInspector={() => {}}
                />,
            );
        });
        const svg = findElements(doc.body, (el) => el.tagName === 'SVG' && /concurrent slot swimlane/.test(el.getAttribute('aria-label') || ''))[0];
        assert.ok(svg, 'Expected Sessions over Time SVG swimlane');
        // Here warmup_seconds=15 equals maxTime=15, so the boundary is at the right plot edge and Warmup sits left of the line (textAnchor='end').
        const warmupTextRightEdge = findElements(svg, (el) => el.tagName === 'TEXT' && el.textContent === 'Warmup')[0];
        const warmupLineRightEdge = findElements(svg, (el) => el.tagName === 'LINE' && el.getAttribute('stroke-dasharray') === '4 3')[0];
        assert.ok(warmupTextRightEdge, 'Expected Warmup <text> label inside SlotSwimlaneChart SVG');
        assert.ok(warmupLineRightEdge, 'Expected Warmup dashed <line> inside SlotSwimlaneChart SVG');
        assert.equal(warmupTextRightEdge.getAttribute('fill'), 'var(--text-muted)');
        assert.equal(warmupTextRightEdge.getAttribute('font-size'), '10');
        assert.equal(warmupTextRightEdge.getAttribute('text-anchor'), 'end');
        assert.ok(Number(warmupTextRightEdge.getAttribute('x')) < Number(warmupLineRightEdge.getAttribute('x1')));
        assert.ok(Number(warmupTextRightEdge.getAttribute('x')) <= 44 + 900 - 4);
        const requestBars = findElements(svg, (el) => el.tagName === 'RECT' && typeof reactProps(el).onMouseEnter === 'function');
        assert.equal(requestBars.length, 4);
        assert.ok(
            Number(warmupTextRightEdge.getAttribute('y')) < Number(requestBars[0].getAttribute('y')),
            'Expected Warmup label y to sit strictly above Slot S1 request bars',
        );
        await act(async () => {
            reactProps(requestBars[1]).onMouseEnter({ clientX: 120, clientY: 60 });
        });
        const tooltip = findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip'))[0];
        assert.ok(tooltip, 'Expected ChartTooltip with lens-chart-tooltip on bar hover');
        assert.match(tooltip.textContent, /Slot S1/);
        assert.match(tooltip.textContent, /sess-a · Turn 2/);
        assert.match(tooltip.textContent, /82\.50% \(warm\)/);
        assert.match(tooltip.textContent, /1,024 \/ 96 tok/);
        assert.match(tooltip.textContent, /95\.20 ms/);

        // Hover near bottom edge of container clamps tooltip top inside outer wrapper (outside scroll container)
        await act(async () => {
            reactProps(requestBars[3]).onMouseEnter({ clientX: 590, clientY: 310 });
        });
        const clampedWrapper = findElements(doc.body, (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('pointer-events-none absolute'))[0];
        assert.ok(clampedWrapper, 'Expected outer tooltip wrapper');
        assert.equal(clampedWrapper.style.top, '150px');
        assert.equal(clampedWrapper.style.left, '400px');
    });
});

test('RequestsTab falls back to client-side greedy slot packing for legacy sessions_summary.sessions turns', () => {
    const legacySessionsTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            warmup_seconds: 0,
            slot_lanes: undefined,
        },
        sessions_summary: {
            available: true,
            correlation: { matched_count: 3, total_count: 3, matched_ratio: 1.0 },
            sessions: [
                {
                    session_id: 's-1',
                    turns: [
                        { start_s: 0.0, end_s: 4.0, cache_hit_pct: 5.0, turn: 1, ttft_ms: 200 },
                        { start_s: 5.0, end_s: 9.0, cache_hit_pct: 75.0, turn: 2, ttft_ms: 80 },
                    ],
                },
                {
                    session_id: 's-2',
                    turns: [
                        { start_s: 1.0, end_s: 6.0, cache_hit_pct: 40.0, turn: 1, ttft_ms: 140 },
                    ],
                },
            ],
        },
    });

    const html = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask({ simulation: { duration_seconds: 20, backend_options: { warmup_seconds: 0 } } })}
            telemetry={legacySessionsTelemetry}
            analysisWindow="full"
            onOpenErrorInspector={() => {}}
        />,
    );
    // s-1 turn 2 starts at 5.0 after s-1 turn 1 ended at 4.0 -> reuses S1, so peak is 2 slots for 3 requests across 2 sessions
    assert.match(html, /2 slots at peak · 3 requests · 2 sessions/);
    assert.match(html, />S1</);
    assert.match(html, />S2</);
    assert.doesNotMatch(html, /shaded span = warmup/);
    assert.doesNotMatch(html, />Warmup</);

    // When warmup_seconds (5s) is well inside maxTime (60s), SlotSwimlaneChart still places 'Warmup' on the LEFT (textAnchor="end", x < warmupLineX)
    const wideSlotTelemetry = {
        ...legacySessionsTelemetry,
        client_series: {
            ...legacySessionsTelemetry.client_series,
            warmup_seconds: 5,
            slot_lanes: {
                peak_slots: 1,
                total_requests: 1,
                session_count: 1,
                items: [{ slot: 0, start_s: 1.0, end_s: 60.0, cache_hit_pct: 10.0, tier: 'partial', session_id: 's-1', turn: 1 }],
            },
        },
    };
    const wideHtml = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask({ simulation: { duration_seconds: 60, backend_options: { warmup_seconds: 5 } } })}
            telemetry={wideSlotTelemetry}
            analysisWindow="full"
            onOpenErrorInspector={() => {}}
        />,
    );
    const wideWarmupLineMatch = wideHtml.match(/<line x1="([0-9.]+)"[^>]*stroke-dasharray="4 3"/);
    const wideWarmupTextMatch = wideHtml.match(/<text x="([0-9.]+)"[^>]*text-anchor="end"[^>]*fill="var\(--text-muted\)"[^>]*font-size="10"[^>]*>Warmup<\/text>/);
    assert.ok(wideWarmupLineMatch, 'Expected Warmup dashed line in SlotSwimlaneChart');
    assert.ok(wideWarmupTextMatch, 'Expected Warmup label with text-anchor="end" on left of warmup line');
    assert.ok(Number(wideWarmupTextMatch[1]) < Number(wideWarmupLineMatch[1]), 'Expected Warmup text x < warmupLineX');

    // Very small warmup (0.2s of 60s) stays left of warmupLineX and clamps x >= padLeft (44) so it never clips off x=0
    const tinySlotTelemetry = {
        ...wideSlotTelemetry,
        client_series: {
            ...wideSlotTelemetry.client_series,
            warmup_seconds: 0.2,
        },
    };
    const tinyHtml = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask({ simulation: { duration_seconds: 60, backend_options: { warmup_seconds: 0.2 } } })}
            telemetry={tinySlotTelemetry}
            analysisWindow="full"
            onOpenErrorInspector={() => {}}
        />,
    );
    const tinyWarmupLineMatch = tinyHtml.match(/<line x1="([0-9.]+)"[^>]*stroke-dasharray="4 3"/);
    const tinyWarmupTextMatch = tinyHtml.match(/<text x="([0-9.]+)"[^>]*text-anchor="end"[^>]*fill="var\(--text-muted\)"[^>]*font-size="10"[^>]*>Warmup<\/text>/);
    assert.ok(tinyWarmupLineMatch && tinyWarmupTextMatch);
    assert.ok(Number(tinyWarmupTextMatch[1]) >= 44, 'Expected Warmup x >= padLeft (44) to avoid clipping at x=0');
    assert.ok(Number(tinyWarmupTextMatch[1]) < Number(tinyWarmupLineMatch[1]), 'Expected Warmup x < warmupLineX even for tiny warmup');
});

test('RequestsTab SlotSwimlaneChart renders sibling-matched height without nested border card, centers capped bars, measures width 1:1, and scrolls on high concurrency', async (t) => {
    const makeSlotTelemetry = (peakSlots, items) => makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            warmup_seconds: 0,
            slot_lanes: {
                peak_slots: peakSlots,
                total_requests: items.length,
                session_count: items.length,
                items,
            },
        },
        sessions_summary: {
            available: true,
            correlation: { matched_count: items.length, total_count: items.length, matched_ratio: 1.0 },
        },
    });

    // Low concurrency (peak_slots = 8) uses sibling-matched standard height (>= 220), explicit height attr (not h-auto), centered capped bars, and no nested border card
    const lowSlotTelemetry = makeSlotTelemetry(8, [
        { slot: 0, start_s: 0.5, end_s: 5.0, cache_hit_pct: 5.0, tier: 'cold', session_id: 's-0', turn: 1 },
        { slot: 3, start_s: 1.0, end_s: 6.0, cache_hit_pct: 80.0, tier: 'warm', session_id: 's-3', turn: 1 },
    ]);
    const lowHtml = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask({ simulation: { duration_seconds: 10, backend_options: { warmup_seconds: 0 } } })}
            telemetry={lowSlotTelemetry}
            analysisWindow="full"
            onOpenErrorInspector={() => {}}
        />,
    );
    const lowSvgMatch = lowHtml.match(/<svg[^>]*aria-label="Sessions over time concurrent slot swimlane[^"]*"[^>]*>/);
    assert.ok(lowSvgMatch, 'Expected SlotSwimlaneChart SVG element');
    const lowSvgTag = lowSvgMatch[0];
    const heightMatch = lowSvgTag.match(/height="([0-9.]+)"/);
    assert.ok(heightMatch && Number(heightMatch[1]) >= 220 && Number(heightMatch[1]) <= 260, `Expected standard sibling-matched SVG height in [220, 260], got ${heightMatch?.[1]}`);
    assert.match(lowSvgTag, /width="100%"/);
    assert.match(lowSvgTag, /min-w-\[480px\]/);
    assert.doesNotMatch(lowSvgTag, /\bh-auto\b/);
    assert.doesNotMatch(lowHtml, /max-h-\[420px\][^>]*border-theme-border/);
    assert.doesNotMatch(lowHtml, /max-h-\[360px\]/);
    assert.match(lowHtml, /<div class="overflow-x-auto"><svg/);
    // Bar height for 8 slots (plotHeight=200 -> rowH=25) is capped at 18 and vertically centered: y = padTop(10) + slot*25 + (25-18)/2
    assert.match(lowHtml, /y="13\.5"[^>]*height="18"/);
    assert.match(lowHtml, /y="88\.5"[^>]*height="18"/);

    // High concurrency (peak_slots = 60) expands beyond 360px and enables vertical scrolling without nested border card
    const highItems = Array.from({ length: 60 }, (_, idx) => ({
        slot: idx,
        start_s: 0.5,
        end_s: 8.0,
        cache_hit_pct: idx % 2 === 0 ? 5.0 : 85.0,
        tier: idx % 2 === 0 ? 'cold' : 'warm',
        session_id: `s-${idx}`,
        turn: 1,
    }));
    const highHtml = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask({ simulation: { duration_seconds: 10, backend_options: { warmup_seconds: 0 } } })}
            telemetry={makeSlotTelemetry(60, highItems)}
            analysisWindow="full"
            onOpenErrorInspector={() => {}}
        />,
    );
    const highSvgMatch = highHtml.match(/<svg[^>]*aria-label="Sessions over time concurrent slot swimlane[^"]*"[^>]*>/);
    assert.ok(highSvgMatch);
    const highHeightMatch = highSvgMatch[0].match(/height="([0-9.]+)"/);
    assert.ok(highHeightMatch && Number(highHeightMatch[1]) > 360, `Expected high-concurrency SVG height > 360, got ${highHeightMatch?.[1]}`);
    assert.match(highHtml, /<div class="max-h-\[360px\] overflow-auto"><svg/);

    // ResizeObserver measures container width directly from contentRect.width so viewBox matches CSS pixels 1:1 (and clamps narrow viewports to 480 with min-w-[480px])
    const domTelemetry = {
        ...lowSlotTelemetry,
        client_series: { ...lowSlotTelemetry.client_series, buckets: [] },
    };
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        const observerCallbacks = [];
        globalThis.ResizeObserver = class MockResizeObserver {
            constructor(cb) {
                observerCallbacks.push(cb);
            }
            observe() {}
            unobserve() {}
            disconnect() {}
        };
        await act(async () => {
            root.render(
                <RequestsTab
                    task={makeRichTask({ simulation: { duration_seconds: 10, backend_options: { warmup_seconds: 0 } } })}
                    telemetry={domTelemetry}
                    analysisWindow="full"
                    onOpenErrorInspector={() => {}}
                />,
            );
        });
        const svg = findElements(doc.body, (el) => el.tagName === 'SVG' && /concurrent slot swimlane/.test(el.getAttribute('aria-label') || ''))[0];
        assert.ok(svg);
        assert.equal(svg.getAttribute('viewBox'), '0 0 640 234');
        await act(async () => {
            for (const cb of observerCallbacks) cb([{ contentRect: { width: 320 } }]);
        });
        assert.equal(svg.getAttribute('viewBox'), '0 0 480 234');
        assert.match(String(svg.getAttribute('class') || ''), /min-w-\[480px\]/);
        assert.match(String(svg.parentNode?.getAttribute('class') || ''), /\boverflow-x-auto\b/);
        await act(async () => {
            for (const cb of observerCallbacks) cb([{ contentRect: { width: 1320 } }]);
        });
        assert.equal(svg.getAttribute('viewBox'), '0 0 1320 234');
    });
});

test('ConfigurationGroup, formatTaskPacing, and RunCompareDrawer display Fixed Schedule vs Concurrency (16)', async (t) => {
    const fixedTask = makeRichTask({
        id: 'sim-fixed',
        name: 'fixed-run',
        simulation: { backend: 'aiperf', duration_seconds: 60, grace_period_seconds: 30, backend_options: { warmup_seconds: 5 } },
        prompt: { trace: { format: 'mooncake_trace', fixed_schedule: true } },
    });
    const concTask = makeRichTask({
        id: 'sim-conc',
        name: 'conc-run',
        simulation: { backend: 'aiperf', duration_seconds: 60, grace_period_seconds: 30, backend_options: { warmup_seconds: 5, concurrency: 16 } },
        prompt: { trace: { format: 'mooncake_trace', fixed_schedule: false } },
    });

    const fixedPacing = formatTaskPacing(fixedTask);
    const concPacing = formatTaskPacing(concTask);
    assert.equal(fixedPacing.label, 'Fixed Schedule');
    assert.equal(fixedPacing.concurrency, null);
    assert.equal(concPacing.label, 'Concurrency (16)');
    assert.equal(concPacing.concurrency, 16);

    // Edge cases: empty sim.backend_options must not mask manifest.replay.backend_options.concurrency,
    // trace-replayer always resolves to Fixed Schedule, and empty (null, {}) returns label: null.
    const manifestFallbackPacing = formatTaskPacing(
        { id: 'sim-m', simulation: { backend: 'aiperf', backend_options: {} } },
        { run_manifest: { backend: 'aiperf', replay: { fixed_schedule: false, backend_options: { concurrency: 16 } } } },
    );
    assert.equal(manifestFallbackPacing.label, 'Concurrency (16)');
    assert.equal(manifestFallbackPacing.concurrency, 16);

    const traceReplayerPacing = formatTaskPacing({
        id: 'sim-tr',
        simulation: { backend: 'trace-replayer', backend_options: { concurrency: 8 } },
        prompt: { trace: { format: 'mooncake_trace', fixed_schedule: false } },
    });
    assert.equal(traceReplayerPacing.label, 'Fixed Schedule');
    assert.equal(traceReplayerPacing.concurrency, null);

    const emptyPacing = formatTaskPacing(null, {});
    assert.equal(emptyPacing.label, null);

    const groupHtml = renderToStaticMarkup(
        <ConfigurationGroup
            title="Replay Workload"
            items={[
                ['Pacing', concPacing.label, 'Replay pacing mode.'],
                ['Session Concurrency', String(concPacing.concurrency), 'Concurrent slot count.'],
            ]}
        />,
    );
    assert.match(groupHtml, /Pacing/);
    assert.match(groupHtml, /Concurrency \(16\)/);
    assert.match(groupHtml, /Session Concurrency/);
    assert.match(groupHtml, />16</);

    await withMountedDom(
        t,
        async () => {
            const base = makeRichTelemetry();
            return Response.json({
                ...base,
                client_series: { ...base.client_series, buckets: [] },
                server_telemetry: { ...base.server_telemetry, series: [] },
            });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-fixed', 'sim-conc']}
                        tasksById={{ 'sim-fixed': fixedTask, 'sim-conc': concTask }}
                    />,
                );
            });
            await flush();
            assert.match(doc.body.textContent, /Pacing/);
            assert.match(doc.body.textContent, /Fixed Schedule/);
            assert.match(doc.body.textContent, /Concurrency \(16\)/);
        },
    );
});

test('SimulationDashboard AIPerf Replay Pacing and Session Concurrency form state, Step 2 controls, and payload', async (t) => {
    const defaultForm = createSimulationFormState();
    assert.equal(defaultForm.backend, 'aiperf');
    assert.equal(defaultForm.fixedSchedule, true);
    assert.equal(defaultForm.aiperfConcurrency, '');
    assert.equal(defaultForm.randomSeed, '666');
    assert.equal(Object.hasOwn(defaultForm, 'numProducer'), false);
    assert.equal(Object.hasOwn(defaultForm, 'channelCapacity'), false);
    assert.equal(Object.hasOwn(defaultForm, 'threads'), false);
    assert.equal(Object.hasOwn(defaultForm, 'earlyStopErrorThreshold'), false);

    const zeroSeedForm = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: { random_seed: 0 } },
    });
    assert.equal(zeroSeedForm.randomSeed, '0');

    const customSeedForm = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: { random_seed: 42 } },
    });
    assert.equal(customSeedForm.randomSeed, '42');

    const emptySeedForm = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: { random_seed: '' } },
    });
    assert.equal(emptySeedForm.randomSeed, '666');

    const prefilledConc = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: { concurrency: 1 } },
        prompt: { trace: { format: 'mooncake_trace', fixed_schedule: false } },
    });
    assert.equal(prefilledConc.fixedSchedule, false);
    assert.equal(prefilledConc.aiperfConcurrency, '1');
    assert.equal(prefilledConc.randomSeed, '666');

    const nullFixedSchedule = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: {} },
        prompt: { trace: { format: 'mooncake_trace', fixed_schedule: null } },
    });
    assert.equal(nullFixedSchedule.fixedSchedule, true);
    assert.equal(nullFixedSchedule.aiperfConcurrency, '');

    const explicitConcurrencyOverride = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: { concurrency: 8 } },
        prompt: { trace: { format: 'mooncake_trace', fixed_schedule: true } },
    });
    assert.equal(explicitConcurrencyOverride.fixedSchedule, false);
    assert.equal(explicitConcurrencyOverride.aiperfConcurrency, '8');

    const wekaPrefill = createSimulationFormState({
        simulation: { backend: 'aiperf', backend_options: {} },
        prompt: { trace: { format: 'weka_public_dataset', fixed_schedule: true } },
    });
    assert.equal(wekaPrefill.fixedSchedule, false);

    const traceReplayerPrefill = createSimulationFormState({
        simulation: { backend: 'trace-replayer', backend_options: { concurrency: 8, random_seed: 99 } },
        prompt: { trace: { format: 'mooncake_trace', fixed_schedule: false } },
    });
    assert.equal(traceReplayerPrefill.backend, 'aiperf');
    assert.equal(traceReplayerPrefill.fixedSchedule, true);
    assert.equal(traceReplayerPrefill.aiperfConcurrency, '');
    assert.equal(traceReplayerPrefill.randomSeed, '666');

    const legacyUnderscorePrefill = createSimulationFormState({
        backend: 'trace_replayer',
        backend_options: { concurrency: 4 },
    });
    assert.equal(legacyUnderscorePrefill.backend, 'aiperf');
    assert.equal(legacyUnderscorePrefill.fixedSchedule, true);
    assert.equal(legacyUnderscorePrefill.aiperfConcurrency, '');
    assert.equal(legacyUnderscorePrefill.randomSeed, '666');

    const postedBodies = [];
    await withMountedDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/v1/simulation/tasks' && options.method === 'POST') {
                postedBodies.push(JSON.parse(options.body));
                return Response.json({ task: { id: 'sim-new-1', status: 'queued' } });
            }
            if (url === '/api/v1/simulation/backends') {
                return Response.json({
                    backends: [{
                        name: 'aiperf',
                        display_name: 'AIPerf',
                        available: true,
                        scenarios: [{ name: 'chat', label: 'Chat' }],
                        capabilities: {
                            supports_trace_range: true,
                            supports_concurrency: true,
                            scale_factor_trace_formats: ['baseten_trace'],
                            trace_format_capabilities: {
                                mooncake_trace: { supports_trace_range: true, supports_scale_factor: false, supports_fixed_schedule: true, supports_concurrency: true },
                                weka_public_dataset: { supports_trace_range: false, supports_scale_factor: false, supports_fixed_schedule: false, supports_concurrency: true },
                            },
                            advanced_options: [
                                { name: 'random_seed', label: 'Random seed', minimum: 0 },
                                { name: 'concurrency', label: 'Session concurrency', minimum: 1, default: 1 },
                            ],
                        },
                    }],
                });
            }
            if (url === '/api/v1/simulation/scenarios') {
                return Response.json({ scenarios: [{ value: 'chat', label: 'Chat' }] });
            }
            if (url === '/api/v1/simulation/trace-datasets') {
                return Response.json({
                    datasets: [
                        { name: 'mooncake_demo', display_name: 'Mooncake Demo', scenario: 'chat', trace_format: 'mooncake_trace', downloaded: true, path: '/traces/mooncake.jsonl', supported_backends: ['aiperf'] },
                        { name: 'weka_demo', display_name: 'Weka Demo', scenario: 'chat', trace_format: 'weka_public_dataset', source_type: 'external_dataset', downloaded: true, path: 'weka://demo', supported_backends: ['aiperf'] },
                    ],
                });
            }
            if (url.includes('/timeline')) {
                return Response.json({
                    duration_seconds: 30,
                    request_count: 100,
                    bins: [{ start_seconds: 0, end_seconds: 30, request_count: 100 }],
                });
            }
            if (url === '/api/v1/simulation/models/discover') {
                return Response.json({ models: ['llama-3-8b'] });
            }
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({
                    tasks: [],
                    total: 0,
                    counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 },
                    facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] },
                });
            }
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush, clickButton }) => {
            globalThis.window.location = new URL('http://localhost/');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            await act(async () => {
                root.render(<SimulationDashboard initialAction="new" />);
            });
            await flush();
            await flush();

            // Step 0: verify "Session concurrency" is NOT duplicated inside Advanced backend settings
            const step0ConcurrencyInputs = findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-aiperfConcurrency');
            assert.equal(step0ConcurrencyInputs.length, 0, 'Expected concurrency excluded from Step 0 Advanced backend settings');

            // Switch to External Endpoint URL, enter URL, wait for debounced validation
            const extRadio = findElements(
                doc.body,
                (el) => el.tagName === 'INPUT' && reactProps(el).name === 'simulation-endpoint-mode' && reactProps(el).value === 'external',
            )[0];
            assert.ok(extRadio);
            await act(async () => {
                reactProps(extRadio).onChange?.({ target: { value: 'external' } });
            });
            const endpointInput = findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-endpoint')[0];
            await act(async () => {
                reactProps(endpointInput).onChange?.({ target: { value: 'http://localhost:8000' } });
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 560));
            });
            await flush();

            await clickButton(/Next/);
            await flush();

            // Step 1: pick weka_demo (public dataset -> forces concurrency mode without timeline chart)
            const datasetSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-dataset')[0];
            assert.ok(datasetSelect);
            await act(async () => {
                reactProps(datasetSelect).onChange?.({ target: { value: 'weka_demo' } });
            });
            await flush();

            await clickButton(/Next/);
            await flush();

            // Step 2: weka_public_dataset hides Replay Pacing toggle and shows Session Concurrency defaulting to 16
            assert.doesNotMatch(doc.body.textContent, /Replay Pacing/);
            const concInput = findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-aiperfConcurrency')[0];
            assert.ok(concInput, 'Expected Session Concurrency input for weka_public_dataset');
            assert.equal(reactProps(concInput).value, '16');

            await act(async () => {
                reactProps(concInput).onChange?.({ target: { value: '24' } });
            });
            await flush();

            // Advance to Step 3 and submit
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();

            assert.equal(postedBodies.length, 1);
            assert.equal(postedBodies[0].fixed_schedule, false);
            assert.equal(postedBodies[0].backend_options.concurrency, 24);
        },
    );
});

test('SimulationDashboard file trace Replay Pacing toggles Fixed Schedule vs Concurrency Slots and validates Session Concurrency', async (t) => {
    const postedBodies = [];
    await withMountedDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/v1/simulation/tasks' && options.method === 'POST') {
                postedBodies.push(JSON.parse(options.body));
                return Response.json({ task: { id: `sim-moon-${postedBodies.length}`, status: 'queued' } });
            }
            if (url === '/api/v1/simulation/backends') {
                return Response.json({
                    backends: [{
                        name: 'aiperf',
                        display_name: 'AIPerf',
                        available: true,
                        scenarios: [{ name: 'chat', label: 'Chat' }],
                        capabilities: {
                            supports_trace_range: true,
                            supports_concurrency: true,
                            scale_factor_trace_formats: ['baseten_trace'],
                            trace_format_capabilities: {
                                mooncake_trace: { supports_trace_range: true, supports_scale_factor: false, supports_fixed_schedule: true, supports_concurrency: true },
                            },
                            advanced_options: [
                                { name: 'random_seed', label: 'Random seed', minimum: 0 },
                                { name: 'concurrency', label: 'Session concurrency', minimum: 1, default: 1 },
                            ],
                        },
                    }],
                });
            }
            if (url === '/api/v1/simulation/scenarios') {
                return Response.json({ scenarios: [{ value: 'chat', label: 'Chat' }] });
            }
            if (url === '/api/v1/simulation/trace-datasets') {
                return Response.json({
                    datasets: [
                        { name: 'mooncake_demo', display_name: 'Mooncake Demo', scenario: 'chat', trace_format: 'mooncake_trace', downloaded: true, path: '/traces/mooncake.jsonl', supported_backends: ['aiperf'] },
                    ],
                });
            }
            if (url.includes('/timeline')) {
                return Response.json({
                    duration_seconds: 30,
                    request_count: 100,
                    bins: [{ start_seconds: 0, end_seconds: 30, request_count: 100 }],
                });
            }
            if (url === '/api/v1/simulation/models/discover') {
                return Response.json({ models: ['llama-3-8b'] });
            }
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({
                    tasks: [],
                    total: 0,
                    counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 },
                    facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] },
                });
            }
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush, clickButton }) => {
            globalThis.window.location = new URL('http://localhost/');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            // Keep Recharts ResponsiveContainer from mounting AreaChart SVG in the minimal test DOM
            // while still loading timeline bins for Step 1 validation.
            const origCreateElement = doc.createElement.bind(doc);
            doc.createElement = (tag) => {
                const node = origCreateElement(tag);
                node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
                return node;
            };

            await act(async () => {
                root.render(<SimulationDashboard initialAction="new" />);
            });
            await flush();
            await flush();

            const extRadio = findElements(
                doc.body,
                (el) => el.tagName === 'INPUT' && reactProps(el).name === 'simulation-endpoint-mode' && reactProps(el).value === 'external',
            )[0];
            assert.ok(extRadio);
            await act(async () => {
                reactProps(extRadio).onChange?.({ target: { value: 'external' } });
            });
            const endpointInput = findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-endpoint')[0];
            await act(async () => {
                reactProps(endpointInput).onChange?.({ target: { value: 'http://localhost:8000' } });
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 560));
            });
            await flush();

            await clickButton(/Next/);
            await flush();

            const datasetSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-dataset')[0];
            assert.ok(datasetSelect);
            await act(async () => {
                reactProps(datasetSelect).onChange?.({ target: { value: 'mooncake_demo' } });
            });
            await flush();
            await flush();

            await clickButton(/Next/);
            await flush();

            // Fixed Schedule is default: Replay Pacing ToggleGroup visible, Session Concurrency input hidden
            assert.match(doc.body.textContent, /Replay Pacing/);
            assert.match(doc.body.textContent, /Fixed Schedule/);
            assert.match(doc.body.textContent, /Concurrency Slots/);
            assert.equal(
                findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-aiperfConcurrency').length,
                0,
            );

            // Submit in Fixed Schedule mode -> fixed_schedule: true and no concurrency in backend_options
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();

            assert.equal(postedBodies.length, 1);
            assert.equal(postedBodies[0].fixed_schedule, true);
            assert.equal('concurrency' in postedBodies[0].backend_options, false);

            // Re-open wizard via "New Task" -> Step 2 -> switch to Concurrency Slots -> validate & submit
            await clickButton(/New Task/);
            await flush();
            await clickButton(/Next/);
            await flush();
            await clickButton(/Next/);
            await flush();

            await clickButton(/^Concurrency Slots$/);
            await flush();
            const concInput = findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-aiperfConcurrency')[0];
            assert.ok(concInput, 'Expected Session Concurrency input after switching to Concurrency Slots');
            assert.equal(reactProps(concInput).value, '16');

            // Clearing Session Concurrency triggers validation error on Next
            await act(async () => {
                reactProps(concInput).onChange?.({ target: { value: '' } });
            });
            await clickButton(/Next/);
            await flush();
            assert.match(doc.body.textContent, /Session Concurrency must be an integer of at least 1\./);

            // Set valid concurrency 32 and submit
            await act(async () => {
                reactProps(concInput).onChange?.({ target: { value: '32' } });
            });
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();

            assert.equal(postedBodies.length, 2);
            assert.equal(postedBodies[1].fixed_schedule, false);
            assert.equal(postedBodies[1].backend_options.concurrency, 32);
        },
    );
});

test('SimulationDashboard random_seed defaults to 666, validates integer >= 0, preserves 0 on submit, and restores 666 on reopen', async (t) => {
    const postedBodies = [];
    await withMountedDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/v1/simulation/tasks' && options.method === 'POST') {
                postedBodies.push(JSON.parse(options.body));
                return Response.json({ task: { id: `sim-seed-${postedBodies.length}`, status: 'queued' } });
            }
            if (url === '/api/v1/simulation/backends') {
                return Response.json({
                    backends: [{
                        name: 'aiperf',
                        display_name: 'AIPerf',
                        available: true,
                        scenarios: [{ name: 'chat', label: 'Chat' }],
                        capabilities: {
                            supports_trace_range: false,
                            supports_concurrency: true,
                            trace_format_capabilities: {
                                weka_public_dataset: { supports_trace_range: false, supports_scale_factor: false, supports_fixed_schedule: false, supports_concurrency: true },
                            },
                            advanced_options: [
                                { name: 'random_seed', label: 'Random seed', minimum: 0, default: 666 },
                                { name: 'concurrency', label: 'Session concurrency', minimum: 1, default: 1 },
                            ],
                        },
                    }],
                });
            }
            if (url === '/api/v1/simulation/scenarios') {
                return Response.json({ scenarios: [{ value: 'chat', label: 'Chat' }] });
            }
            if (url === '/api/v1/simulation/trace-datasets') {
                return Response.json({
                    datasets: [
                        { name: 'weka_demo', display_name: 'Weka Demo', scenario: 'chat', trace_format: 'weka_public_dataset', source_type: 'external_dataset', downloaded: true, path: 'weka://demo', supported_backends: ['aiperf'] },
                    ],
                });
            }
            if (url === '/api/v1/simulation/models/discover') {
                return Response.json({ models: ['llama-3-8b'] });
            }
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({
                    tasks: [],
                    total: 0,
                    counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 },
                    facets: { backends: ['aiperf', 'trace-replayer'], models: [], datasets: [], trace_formats: [], endpoints: [] },
                });
            }
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush, clickButton }) => {
            globalThis.window.location = new URL('http://localhost/');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            await act(async () => {
                root.render(<SimulationDashboard initialAction="new" />);
            });
            await flush();
            await flush();

            // Page title "Trace Replayer" is preserved
            assert.match(doc.body.textContent, /Trace Replayer/);

            // Random seed lives in Advanced backend settings with default '666', step '1', placeholder '666'
            const seedInput = () => findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-randomSeed')[0];
            assert.ok(seedInput(), 'Expected Random seed input in Step 0');
            assert.equal(reactProps(seedInput()).value, '666');
            assert.equal(seedInput().getAttribute('step'), '1');
            assert.equal(seedInput().getAttribute('placeholder'), '666');

            // Configure valid external endpoint so only random_seed validation is tested
            const extRadio = findElements(
                doc.body,
                (el) => el.tagName === 'INPUT' && reactProps(el).name === 'simulation-endpoint-mode' && reactProps(el).value === 'external',
            )[0];
            await act(async () => {
                reactProps(extRadio).onChange?.({ target: { value: 'external' } });
            });
            const endpointInput = findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-endpoint')[0];
            await act(async () => {
                reactProps(endpointInput).onChange?.({ target: { value: 'http://localhost:8000' } });
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 560));
            });
            await flush();

            // Float rejected
            await act(async () => {
                reactProps(seedInput()).onChange?.({ target: { value: '1.5' } });
            });
            await clickButton(/Next/);
            await flush();
            assert.match(doc.body.textContent, /Random seed must be an integer\./);

            // Negative rejected
            await act(async () => {
                reactProps(seedInput()).onChange?.({ target: { value: '-1' } });
            });
            await clickButton(/Next/);
            await flush();
            assert.match(doc.body.textContent, /Random seed must be at least 0\./);

            // Explicit 0 accepted and serialized as number 0
            await act(async () => {
                reactProps(seedInput()).onChange?.({ target: { value: '0' } });
            });
            await clickButton(/Next/);
            await flush();
            const datasetSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-dataset')[0];
            await act(async () => {
                reactProps(datasetSelect).onChange?.({ target: { value: 'weka_demo' } });
            });
            await flush();
            await clickButton(/Next/);
            await flush();
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();

            assert.equal(postedBodies.length, 1);
            assert.equal(postedBodies[0].backend, 'aiperf');
            assert.equal(postedBodies[0].backend_options.random_seed, 0);

            // Reopen, clear seed to '', submit (omits random_seed so backend defaults to 666), then reopen -> restored to '666'
            await clickButton(/New Task/);
            await flush();
            assert.equal(reactProps(seedInput()).value, '0');
            await act(async () => {
                reactProps(seedInput()).onChange?.({ target: { value: '' } });
            });
            await clickButton(/Next/);
            await flush();
            await clickButton(/Next/);
            await flush();
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();

            assert.equal(postedBodies.length, 2);
            assert.equal('random_seed' in postedBodies[1].backend_options, false);

            await clickButton(/New Task/);
            await flush();
            assert.equal(reactProps(seedInput()).value, '666');
        },
    );
});

test('SimulationTaskDetails displays Pacing and Session Concurrency in header and Logs & Configuration', async (t) => {
    const concTask = makeRichTask({
        id: 'sim-pacing-1',
        name: 'mooncake-conc-run',
        simulation: {
            backend: 'aiperf',
            duration_seconds: 60,
            grace_period_seconds: 30,
            backend_options: { warmup_seconds: 5, concurrency: 16 },
        },
        prompt: {
            dataset: { name: 'mooncake_demo' },
            trace: { format: 'mooncake_trace', fixed_schedule: false, start_seconds: 0, end_seconds: 60 },
        },
    });
    const telemetryPayload = {
        forensics: {},
        client_series: {
            buckets: [],
            cold_start_comparison: null,
            windows: {
                profiling: { duration_seconds: 60 },
                full: { duration_seconds: 60 },
            },
        },
        server_telemetry: { available: false, per_pod: [], series: [] },
        run_manifest: {
            execution_started_at: '2026-01-01T00:00:00Z',
            backend: 'aiperf',
            dataset: { name: 'mooncake_demo', format: 'mooncake_trace' },
            replay: {
                duration_seconds: 60,
                grace_period_seconds: 30,
                warmup_seconds: 5,
                scale_factor: 1,
                fixed_schedule: false,
                backend_options: { warmup_seconds: 5, concurrency: 16 },
            },
        },
    };

    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/telemetry')) return Response.json(telemetryPayload);
            if (url.includes('/api/v1/simulation/tasks/sim-pacing-1')) return Response.json({ task: concTask });
            return Response.json({ backends: [] });
        },
        async ({ root, doc, flush, clickButton }) => {
            globalThis.window.location = new URL('http://localhost/?taskId=sim-pacing-1');
            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={() => {}} />);
            });
            await flush();
            await flush();

            assert.match(doc.body.textContent, /Pacing/);
            assert.match(doc.body.textContent, /Concurrency \(16\)/);

            await clickButton(/Logs & Configuration/);
            await flush();
            assert.match(doc.body.textContent, /Session Concurrency/);
            assert.match(doc.body.textContent, /Concurrency \(16\)/);
        },
    );
});

test('LatencyHeatmap renders inside ChartContainer with theme-aware SVG, legend, interactive ChartTooltip, normalized p50/p90/max, and EmptyState', async (t) => {
    const heatmapData = {
        sequence_length: 'input',
        time_bin_count: 40,
        sequence_length_bin_count: 28,
        minimum_metric_ms: 40,
        maximum_metric_ms: 440,
        cells: [
            {
                start_seconds: 0,
                end_seconds: 30,
                sequence_length_start: 128,
                sequence_length_end: 512,
                average_metric_ms: 40,
                p50_ms: 38,
                p90_ms: 62,
                max_ms: 85,
                request_count: 18,
            },
            {
                start_seconds: 30,
                end_seconds: 60,
                sequence_length_start: 512,
                sequence_length_end: 2048,
                average_metric_ms: 440,
                p50_ms: 410,
                p90_ms: 520,
                max_ms: 610,
                request_count: 28,
            },
        ],
    };

    const html = renderToStaticMarkup(
        <LatencyHeatmap
            title="TTFT by Time and Input Sequence Length"
            subtitle="Average TTFT (ms) over elapsed time and input tokens"
            metric="TTFT"
            data={heatmapData}
            help="Bins TTFT latency across elapsed time and input sequence length."
        />,
    );
    assert.match(html, /TTFT by Time and Input Sequence Length/);
    assert.match(html, /Average TTFT \(ms\) over elapsed time and input tokens/);
    assert.match(html, /aria-label="TTFT by Time and Input Sequence Length help"/);
    assert.match(html, /rx="1\.5"/);
    assert.match(html, /var\(--bg-card\)/);
    assert.match(html, /var\(--border-color\)/);
    assert.match(html, /var\(--text-muted\)/);
    assert.match(html, /rgb\(14, 165, 233\)/);
    assert.match(html, /rgb\(244, 63, 94\)/);
    assert.match(html, /40 × 28 grid · 46 requests \(legacy binned fallback\)/);
    assert.match(html, /40 ms/);
    assert.match(html, /440 ms/);
    assert.doesNotMatch(html, /<title>/);

    // Graceful fallback when grid bin counts are omitted
    const noGridHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TTFT Heatmap"
            metric="TTFT"
            data={{
                ...heatmapData,
                time_bin_count: undefined,
                sequence_length_bin_count: undefined,
            }}
        />,
    );
    assert.doesNotMatch(noGridHtml, /grid ·/);
    assert.match(noGridHtml, /46 requests \(legacy binned fallback\)/);

    // Empty state via EmptyState inside ChartContainer
    const emptyHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TTFT by Time and Input Sequence Length"
            metric="TTFT"
            data={{ cells: [] }}
        />,
    );
    assert.match(emptyHtml, /TTFT by Time and Input Sequence Length/);
    assert.match(emptyHtml, /TTFT scatter plot unavailable/);
    assert.match(emptyHtml, /No per-request TTFT and sequence-length data are available\./);

    // RequestsTab normalizes legacy time_bins/len_bins and p50_ms/p90_ms/max_ms and passes subtitles
    const normalizedTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            buckets: [],
            slot_lanes: undefined,
            heatmap: {
                ttft: {
                    time_bins: 24,
                    len_bins: 16,
                    cells: [
                        {
                            start_seconds: 0,
                            end_seconds: 60,
                            seq_start: 100,
                            seq_end: 500,
                            avg_ms: 120,
                            p50_ms: 115,
                            p90_ms: 210,
                            max_ms: 290,
                            count: 19,
                        },
                    ],
                },
                tpot: {
                    time_bins: 24,
                    len_bins: 16,
                    cells: [
                        {
                            start_seconds: 0,
                            end_seconds: 60,
                            seq_start: 50,
                            seq_end: 250,
                            avg_ms: 22,
                            p50: 20,
                            p90: 35,
                            count: 19,
                        },
                    ],
                },
            },
        },
        sessions_summary: { available: false, reason: 'No sessions' },
    });
    const requestsHtml = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask()}
            telemetry={normalizedTelemetry}
            analysisWindow="profiling"
            onOpenErrorInspector={() => {}}
        />,
    );
    assert.match(requestsHtml, /Per-request TTFT \(ms\) by elapsed time and input sequence length \(tokens\)/);
    assert.match(requestsHtml, /Per-request TPOT \(ms\/tok\) by elapsed time and output sequence length \(tokens\)/);
    assert.match(requestsHtml, /24 × 16 grid · 19 requests \(legacy binned fallback\)/);

    // Interactive hover tooltip and clamping in mounted DOM
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        await act(async () => {
            root.render(
                <RequestsTab
                    task={makeRichTask()}
                    telemetry={normalizedTelemetry}
                    analysisWindow="profiling"
                    onOpenErrorInspector={() => {}}
                />,
            );
        });
        const ttftSvg = findElements(
            doc.body,
            (el) => el.tagName === 'SVG' && /TTFT legacy binned heatmap by elapsed time/.test(el.getAttribute('aria-label') || ''),
        )[0];
        assert.ok(ttftSvg, 'Expected TTFT heatmap SVG');
        const cellRects = findElements(
            ttftSvg,
            (el) => el.tagName === 'RECT' && typeof reactProps(el).onMouseEnter === 'function',
        );
        assert.equal(cellRects.length, 1);

        await act(async () => {
            reactProps(cellRects[0]).onMouseEnter({ clientX: 600, clientY: 300 });
        });
        const tooltip = findElements(
            doc.body,
            (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip'),
        )[0];
        assert.ok(tooltip, 'Expected ChartTooltip on heatmap cell hover');
        assert.match(tooltip.textContent, /Time 0s – 60s/);
        assert.match(tooltip.textContent, /Input sequence length/);
        assert.match(tooltip.textContent, /100 – 500 tok/);
        assert.match(tooltip.textContent, /Avg TTFT/);
        assert.match(tooltip.textContent, /120 ms/);
        assert.match(tooltip.textContent, /P50 TTFT/);
        assert.match(tooltip.textContent, /115 ms/);
        assert.match(tooltip.textContent, /P90 TTFT/);
        assert.match(tooltip.textContent, /210 ms/);
        assert.match(tooltip.textContent, /Max TTFT/);
        assert.match(tooltip.textContent, /290 ms/);
        assert.match(tooltip.textContent, /Requests19/);
        assert.equal(cellRects[0].getAttribute('stroke'), 'var(--text-primary)');

        const wrapper = findElements(
            doc.body,
            (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('pointer-events-none absolute'),
        )[0];
        assert.ok(wrapper, 'Expected clamped tooltip wrapper');
        assert.equal(wrapper.style.left, '392px');
        assert.equal(wrapper.style.top, '140px');
    });
});

test('LatencyHeatmap and normalizeHeatmapData ignore null/empty bounds and preserve sequence_length and string percentiles on analytics-shaped payloads', async (t) => {
    const nullBoundsHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TTFT Null Bounds"
            metric="TTFT"
            data={{
                sequence_length: 'input',
                minimum_metric_ms: null,
                maximum_metric_ms: '',
                cells: [
                    { start_seconds: 0, end_seconds: 10, sequence_length_start: 100, sequence_length_end: 200, average_metric_ms: 40, request_count: 2 },
                    { start_seconds: 10, end_seconds: 20, sequence_length_start: 200, sequence_length_end: 400, average_metric_ms: 440, request_count: 3 },
                ],
            }}
        />,
    );
    assert.match(nullBoundsHtml, /40 ms/);
    assert.match(nullBoundsHtml, /440 ms/);

    const analyticsShapedTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            buckets: [],
            slot_lanes: undefined,
            heatmap: {
                ttft: {
                    time_bins: 12,
                    len_bins: 8,
                    minimum_metric_ms: null,
                    maximum_metric_ms: '',
                    cells: [
                        {
                            start_seconds: 0,
                            end_seconds: 30,
                            sequence_length_start: 64,
                            sequence_length_end: 256,
                            average_metric_ms: 120,
                            p50: '115',
                            p90: '210',
                            max: '290',
                            request_count: 5,
                        },
                        {
                            start_seconds: 30,
                            end_seconds: 60,
                            sequence_length_start: 256,
                            sequence_length_end: 512,
                            average_metric_ms: 280,
                            request_count: 7,
                        },
                    ],
                },
                tpot: { cells: [] },
            },
        },
        sessions_summary: { available: false, reason: 'No sessions' },
    });

    const html = renderToStaticMarkup(
        <RequestsTab
            task={makeRichTask()}
            telemetry={analyticsShapedTelemetry}
            analysisWindow="profiling"
            onOpenErrorInspector={() => {}}
        />,
    );
    assert.match(html, /TTFT legacy binned heatmap by elapsed time and input sequence length/);
    assert.match(html, /12 × 8 grid · 12 requests \(legacy binned fallback\)/);
    assert.match(html, /120 ms/);
    assert.match(html, /280 ms/);

    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        await act(async () => {
            root.render(
                <RequestsTab
                    task={makeRichTask()}
                    telemetry={analyticsShapedTelemetry}
                    analysisWindow="profiling"
                    onOpenErrorInspector={() => {}}
                />,
            );
        });
        const ttftSvg = findElements(
            doc.body,
            (el) => el.tagName === 'SVG' && /TTFT legacy binned heatmap by elapsed time and input sequence length/.test(el.getAttribute('aria-label') || ''),
        )[0];
        assert.ok(ttftSvg, 'Expected TTFT SVG with input sequence length label');
        const cellRects = findElements(
            ttftSvg,
            (el) => el.tagName === 'RECT' && typeof reactProps(el).onMouseEnter === 'function',
        );
        assert.equal(cellRects.length, 2);

        await act(async () => {
            reactProps(cellRects[0]).onMouseEnter({ clientX: 120, clientY: 120 });
        });
        const tooltip = findElements(
            doc.body,
            (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip'),
        )[0];
        assert.ok(tooltip, 'Expected ChartTooltip with normalized numeric percentiles');
        assert.match(tooltip.textContent, /Input sequence length/);
        assert.match(tooltip.textContent, /P50 TTFT/);
        assert.match(tooltip.textContent, /115 ms/);
        assert.match(tooltip.textContent, /P90 TTFT/);
        assert.match(tooltip.textContent, /210 ms/);
        assert.match(tooltip.textContent, /Max TTFT/);
        assert.match(tooltip.textContent, /290 ms/);
    });
});

test('BottlenecksTab #b-stage-breakdown renders Request Stage Latency Breakdown with P50/P90 toggle, share pills, peak-shift banner, engine-agnostic tooltips, and client-fallback gating', async (t) => {
    const stageBreakdownPayload = {
        available: true,
        source: 'server_telemetry',
        has_p90_series: true,
        dominant_e2e_stage: 'decode_time',
        dominant_ttft_stage: 'prefill_time',
        dominant_e2e_stage_p90: 'queue_time',
        dominant_ttft_stage_p90: 'queue_time',
        p50: {
            available: true,
            epp_scheduling_ms: 2.5,
            queue_time_ms: 15.0,
            prefill_time_ms: 110.0,
            decode_time_ms: 850.0,
            gateway_overhead_ms: 12.5,
            ttft_stage_ms: 140.0,
            total_stage_ms: 990.0,
            share_of_e2e_pct: { epp_scheduling: 0.3, queue_time: 1.5, prefill_time: 11.1, decode_time: 85.9, gateway_overhead: 1.3 },
            share_of_ttft_pct: { epp_scheduling: 1.8, queue_time: 10.7, prefill_time: 78.6, gateway_overhead: 8.9 },
        },
        p90: {
            available: true,
            epp_scheduling_ms: 4.0,
            queue_time_ms: 1400.0,
            prefill_time_ms: 210.0,
            decode_time_ms: 980.0,
            gateway_overhead_ms: 20.0,
            ttft_stage_ms: 1634.0,
            total_stage_ms: 2614.0,
            share_of_e2e_pct: { epp_scheduling: 0.2, queue_time: 53.6, prefill_time: 8.0, decode_time: 37.5, gateway_overhead: 0.8 },
            share_of_ttft_pct: { epp_scheduling: 0.2, queue_time: 85.7, prefill_time: 12.9, gateway_overhead: 1.2 },
        },
        peak_window_shift: {
            detected: true,
            summary: 'Peak window at t=40s shifted dominant stage from Decode Time to Queue Time (62.4% share, 1840 ms).',
        },
        series: [
            { t: 10, epp_sched_ms: 2.0, queue_ms: 12.0, prefill_ms: 100.0, decode_ms: 840.0, epp_sched_p90_ms: 3.5, queue_p90_ms: 420.0, prefill_p90_ms: 190.0, decode_p90_ms: 950.0 },
            { t: 40, epp_sched_ms: 3.0, queue_ms: 18.0, prefill_ms: 120.0, decode_ms: 860.0, epp_sched_p90_ms: 4.5, queue_p90_ms: 1840.0, prefill_p90_ms: 230.0, decode_p90_ms: 1010.0 },
        ],
    };

    const serverTelemetry = makeRichTelemetry({
        forensics: {
            ...makeRichTelemetry().forensics,
            stage_latency_breakdown: stageBreakdownPayload,
            findings: [
                {
                    code: 'stage_dominance',
                    severity: 'info',
                    category: 'Stage Attribution',
                    title: 'Decode Time dominates E2E latency',
                    tab: 'serving',
                    anchor: 's-latency',
                },
            ],
        },
    });

    const html = renderToStaticMarkup(<BottlenecksTab telemetry={serverTelemetry} onSelectTab={() => {}} />);
    assertNoNestedButtons(html, 'BottlenecksTab stage breakdown');
    assert.match(html, /id="b-stage-breakdown"/);
    assert.match(html, /Request Stage Latency Breakdown/);
    assert.match(html, /Stacked P50 stage latencies \(EPP Scheduling \+ Queue Time \+ Prefill Time \+ Decode Time\)/);
    assert.match(html, /Dominant: Decode Time · 85\.90%/);
    assert.match(html, /Peak window at t=40s shifted dominant stage from Decode Time to Queue Time/);
    assert.match(html, /Stage Share Summary \(P50\)/);
    assert.match(html, /Client \/ Gateway \/ Network/);
    assert.match(html, /Serving · Latency/);

    // Verify engine-agnostic tooltips in Request Stage Latency Breakdown (no raw vllm:... metric names)
    const stageSectionStart = html.indexOf('id="b-stage-breakdown"');
    const ttftSectionStart = html.indexOf('TTFT Stage Decomposition');
    const stageCardHtml = html.slice(stageSectionStart, ttftSectionStart);
    assert.doesNotMatch(stageCardHtml, /vllm:/i);
    assert.doesNotMatch(stageCardHtml, /\bRouter\b/i);

    // Verify stacked AreaChart series order & CHART_SERIES[3],[2],[0],[1] colors
    let bottlenecksTree = null;
    function CaptureBottlenecksStage() {
        bottlenecksTree = BottlenecksTab({ telemetry: serverTelemetry, onSelectTab: () => {} });
        return null;
    }
    renderToStaticMarkup(<CaptureBottlenecksStage />);
    const stageChart = findElement(
        bottlenecksTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'epp_sched_ms')),
    );
    assert.ok(stageChart, 'Expected stacked AreaChart in BottlenecksTab #b-stage-breakdown');
    const stageAreas = [];
    const walkAreas = (node) => {
        if (!node) return;
        if (Array.isArray(node)) { node.forEach(walkAreas); return; }
        if (typeof node !== 'object') return;
        if (node.type === Area) stageAreas.push(node);
        walkAreas(node.props?.children);
    };
    walkAreas(stageChart);
    assert.deepEqual(stageAreas.map((a) => a.props.dataKey), ['epp_sched_ms', 'queue_ms', 'prefill_ms', 'decode_ms']);
    assert.deepEqual(stageAreas.map((a) => a.props.stroke), [CHART_SERIES[3], CHART_SERIES[2], CHART_SERIES[0], CHART_SERIES[1]]);

    // Client-fallback mode hides Serving · Latency deep-link and renders client-estimated stages
    const clientOnlyTelemetry = makeRichTelemetry({
        forensics: {
            ...makeRichTelemetry().forensics,
            ttft_decomposition: { available: false },
            routing_vs_cache: {},
            pod_skew: {},
            timeline_buckets: [],
            stage_latency_breakdown: {
                available: true,
                source: 'client_fallback',
                has_p90_series: false,
                dominant_e2e_stage: 'decode_time',
                dominant_e2e_stage_p90: null,
                p50: {
                    available: true,
                    decode_time_ms: 970,
                    ttft_stage_ms: 130,
                    total_stage_ms: 1100,
                    share_of_e2e_pct: { decode_time: 88.2, ttft_unattributed: 11.8 },
                    share_of_ttft_pct: {},
                },
                p90: {
                    available: true,
                    decode_time_ms: 400,
                    ttft_stage_ms: 600,
                    total_stage_ms: 1000,
                    share_of_e2e_pct: { decode_time: 40.0, ttft_unattributed: 60.0 },
                    share_of_ttft_pct: {},
                },
                series: [
                    { t: 10, ttft_ms: 130, decode_ms: 970, ttft_p90_ms: 600, decode_p90_ms: 400 },
                ],
            },
        },
        server_telemetry: { available: false, status: 'unavailable' },
    });
    const clientHtml = renderToStaticMarkup(<BottlenecksTab telemetry={clientOnlyTelemetry} onSelectTab={() => {}} />);
    assert.doesNotMatch(clientHtml, /Serving · Latency/);
    assert.match(clientHtml, /Client TTFT \(Unattributed\)/);
    assert.match(clientHtml, /Decode Time \(Est\.\)/);
    assert.match(clientHtml, /Dominant: Decode Time · 88\.20%/);
    assert.doesNotMatch(clientHtml, /vllm:/i);

    const clientSummaryOnlyTelemetry = {
        ...clientOnlyTelemetry,
        sessions_summary: null,
        client_series: { buckets: [], windows: {} },
        forensics: {
            ...clientOnlyTelemetry.forensics,
            stage_latency_breakdown: {
                ...clientOnlyTelemetry.forensics.stage_latency_breakdown,
                series: [],
            },
            ttft_decomposition: { available: false },
            routing_vs_cache: {},
            pod_skew: {},
            timeline_buckets: [],
        },
    };

    // Interactive P50 -> P90 toggle + finding anchor forwarding + client_fallback dominant_e2e_stage_p90=null guard
    const summaryOnlyTelemetry = {
        ...serverTelemetry,
        sessions_summary: null,
        forensics: {
            ...serverTelemetry.forensics,
            stage_latency_breakdown: {
                ...stageBreakdownPayload,
                p50: {
                    available: true,
                    decode_time_ms: 850.0,
                    total_stage_ms: 990.0,
                    share_of_e2e_pct: { decode_time: 85.9 },
                    share_of_ttft_pct: {},
                },
                p90: {
                    available: true,
                    decode_time_ms: 980.0,
                    total_stage_ms: 2614.0,
                    share_of_e2e_pct: { queue_time: 53.6, decode_time: 37.5 },
                    share_of_ttft_pct: {},
                },
                series: [],
            },
            ttft_decomposition: { available: false },
            routing_vs_cache: {},
            pod_skew: {},
            timeline_buckets: [],
        },
        client_series: { buckets: [], windows: {} },
        server_telemetry: {
            available: true,
            status: 'available',
            per_pod: [],
            series: [],
        },
    };
    const jumps = [];
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc, clickButton, flush }) => {
        await act(async () => {
            root.render(
                <BottlenecksTab
                    telemetry={summaryOnlyTelemetry}
                    onSelectTab={(tab, anchor) => jumps.push({ tab, anchor })}
                />,
            );
        });
        await flush();
        assert.match(doc.body.textContent, /Stage Share Summary \(P50\)/);
        assert.match(doc.body.textContent, /Dominant: Decode Time · 85\.90%/);

        await clickButton(/^P90$/);
        await flush();
        assert.match(doc.body.textContent, /Stacked P90 stage latencies/);
        assert.match(doc.body.textContent, /Stage Share Summary \(P90\)/);
        assert.match(doc.body.textContent, /Dominant: Queue Time · 53\.60%/);

        await clickButton(/Serving · Latency/);
        await clickButton(/Open Serving/);
        assert.deepEqual(jumps, [
            { tab: 'serving', anchor: 's-latency' },
            { tab: 'serving', anchor: 's-latency' },
        ]);

        // Re-render in client_fallback mode where dominant_e2e_stage_p90 is null:
        // P50 shows Dominant: Decode Time · 88.20%, and P90 must NOT fall back to P50's dominant_e2e_stage
        await act(async () => {
            root.render(<BottlenecksTab telemetry={clientSummaryOnlyTelemetry} onSelectTab={() => {}} />);
        });
        await flush();
        await clickButton(/^P50$/);
        await flush();
        assert.match(doc.body.textContent, /Dominant: Decode Time · 88\.20%/);
        await clickButton(/^P90$/);
        await flush();
        assert.doesNotMatch(doc.body.textContent, /Dominant:/);
        assert.match(doc.body.textContent, /Stage Share Summary \(P90\)/);

        // Trigger A: has_p90_series=true with p90.available=false keeps Stage Share Summary (P50) on P90 toggle
        const partialP90SliceTelemetry = {
            ...summaryOnlyTelemetry,
            forensics: {
                ...summaryOnlyTelemetry.forensics,
                stage_latency_breakdown: {
                    ...summaryOnlyTelemetry.forensics.stage_latency_breakdown,
                    has_p90_series: true,
                    p90: { available: false },
                    series: [],
                },
            },
        };
        await act(async () => {
            root.render(<BottlenecksTab telemetry={partialP90SliceTelemetry} onSelectTab={() => {}} />);
        });
        await flush();
        await clickButton(/^P90$/);
        await flush();
        assert.match(doc.body.textContent, /Stage Share Summary \(P50\)/);
        assert.match(doc.body.textContent, /P50 summary shares are shown below/);
    });

    // Trigger B: P90-only payload automatically selects P90 series, subtitle, disabled P50 option, and Stage Share Summary (P90)
    const p90OnlyStageTelemetry = makeRichTelemetry({
        forensics: {
            ...makeRichTelemetry().forensics,
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_p90_series: true,
                dominant_e2e_stage: 'queue_time',
                dominant_e2e_stage_p90: 'queue_time',
                p50: { available: false },
                p90: stageBreakdownPayload.p90,
                series: [
                    { t: 10, epp_sched_p90_ms: 3.5, queue_p90_ms: 420.0, prefill_p90_ms: 190.0, decode_p90_ms: 950.0 },
                ],
            },
        },
    });
    const p90OnlyStageHtml = renderToStaticMarkup(<BottlenecksTab telemetry={p90OnlyStageTelemetry} onSelectTab={() => {}} />);
    assert.match(p90OnlyStageHtml, /Stacked P90 stage latencies/);
    assert.match(p90OnlyStageHtml, /Stage Share Summary \(P90\)/);
    assert.match(p90OnlyStageHtml, /Dominant: Queue Time · 53\.60%/);
    assert.match(p90OnlyStageHtml, /disabled=""[^>]*>P50</);

    // Trigger C: Mean-only client_fallback renders Stage Share Summary (Mean)
    const meanOnlyClientHtml = renderToStaticMarkup(
        <BottlenecksTab
            telemetry={makeRichTelemetry({
                forensics: {
                    ...makeRichTelemetry().forensics,
                    stage_latency_breakdown: {
                        available: true,
                        source: 'client_fallback',
                        has_p90_series: false,
                        dominant_e2e_stage: 'decode_time',
                        p50: { available: false },
                        p90: { available: false },
                        mean: {
                            available: true,
                            decode_time_ms: 750,
                            ttft_stage_ms: 250,
                            total_stage_ms: 1000,
                            share_of_e2e_pct: { decode_time: 75.0, ttft_unattributed: 25.0 },
                            share_of_ttft_pct: {},
                        },
                        series: [],
                    },
                },
                server_telemetry: { available: false, status: 'unavailable' },
            })}
            onSelectTab={() => {}}
        />,
    );
    assert.match(meanOnlyClientHtml, /Stage Share Summary \(Mean\)/);
    assert.match(meanOnlyClientHtml, /Mean summary shares are shown below/);
});

test('ServingTab #s-latency supports P50/P90 toggle, dynamic subtitle, engine-agnostic help, and scrollAnchor category reset', async (t) => {
    const base = makeRichTelemetry();
    const p90ServingTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            per_pod: [],
            series: [
                {
                    elapsed_seconds: 10,
                    router_scheduler_latency_p50_ms: 2.0,
                    vllm_queue_time_p50_ms: 10.0,
                    vllm_prefill_time_p50_ms: 90.0,
                    vllm_decode_time_p50_ms: 500.0,
                    router_scheduler_latency_p90_ms: 5.0,
                    vllm_queue_time_p90_ms: 350.0,
                    vllm_prefill_time_p90_ms: 180.0,
                    vllm_decode_time_p90_ms: 820.0,
                },
            ],
        },
    };

    const html = renderToStaticMarkup(<ServingTab telemetry={p90ServingTelemetry} />);
    assertNoNestedButtons(html, 'ServingTab P50/P90');
    assert.match(html, /id="s-latency" class="space-y-4 scroll-mt-16"/);
    assert.match(html, /Stacked P50 stage latencies \(EPP Scheduling \+ Queue Time \+ Prefill Time \+ Decode Time\)/);
    assert.match(html, /aria-pressed="true"[^>]*>P50</);
    assert.match(html, /aria-pressed="false"[^>]*>P90</);
    assert.doesNotMatch(
        html.slice(html.indexOf('Request Stage Latency Breakdown'), html.indexOf('Request Stage Latency Breakdown') + 1200),
        /vllm:/i,
    );

    // Verify P90 series activates dynamic P90 subtitle and plots P90 stage durations on canonical keys
    const p90OnlyTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            per_pod: [],
            series: [
                {
                    elapsed_seconds: 10,
                    router_scheduler_latency_p90_ms: 5.0,
                    vllm_queue_time_p90_ms: 350.0,
                    vllm_prefill_time_p90_ms: 180.0,
                    vllm_decode_time_p90_ms: 820.0,
                },
            ],
        },
    };
    const p90OnlyHtml = renderToStaticMarkup(<ServingTab telemetry={p90OnlyTelemetry} />);
    assert.match(p90OnlyHtml, /Stacked P90 stage latencies \(EPP Scheduling \+ Queue Time \+ Prefill Time \+ Decode Time\)/);
    assert.match(p90OnlyHtml, /aria-pressed="true"[^>]*>P90</);

    let capturedP90Tree = null;
    function CaptureServingP90() {
        capturedP90Tree = ServingTab({ telemetry: p90OnlyTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureServingP90 />);
    const p90StageChart = findElement(
        capturedP90Tree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'epp_sched_ms')),
    );
    assert.ok(p90StageChart, 'Expected P90 stage AreaChart');
    assert.equal(p90StageChart.props.data[0].epp_sched_ms, 5);
    assert.equal(p90StageChart.props.data[0].queue_ms, 350);
    assert.equal(p90StageChart.props.data[0].prefill_ms, 180);
    assert.equal(p90StageChart.props.data[0].decode_ms, 820);

    // Strip series & flow triggers in withMountedDom so Recharts ResponsiveContainer / FlowMap aren't mounted in mock DOM
    const scrollOnlyTelemetry = {
        ...base,
        client_series: { ...base.client_series, buckets: [], windows: {} },
        server_telemetry: {
            ...base.server_telemetry,
            summary: { kv_cache_usage_percent: { max: 92.4, mean: 72.1 } },
            per_pod: [],
            series: [],
        },
    };
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc, clickButton, flush }) => {
        await act(async () => {
            root.render(<ServingTab telemetry={scrollOnlyTelemetry} />);
        });
        await flush();

        // Switch away from 'all' category so #s-latency unmounts, then trigger scrollAnchor="s-latency"
        await clickButton(/^Engine · vLLM$/);
        await flush();
        assert.equal(findElements(doc.body, (el) => el.getAttribute('id') === 's-latency').length, 0);

        await act(async () => {
            root.render(<ServingTab telemetry={scrollOnlyTelemetry} scrollAnchor="s-latency" />);
        });
        await flush();
        assert.equal(findElements(doc.body, (el) => el.getAttribute('id') === 's-latency').length, 1);
    });
});

test('SimulationTaskDetails deep-links from Bottlenecks Serving · Latency to Serving #s-latency with smooth scroll', async (t) => {
    const task = makeRichTask({ id: 'sim-anchor-1' });
    const baseRich = makeRichTelemetry();
    const telemetryPayload = makeRichTelemetry({
        forensics: {
            ...baseRich.forensics,
            ttft_decomposition: { available: false },
            routing_vs_cache: {},
            pod_skew: {},
            timeline_buckets: [],
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_p90_series: true,
                dominant_e2e_stage: 'decode_time',
                p50: {
                    available: true,
                    decode_time_ms: 700.0,
                    total_stage_ms: 817.0,
                    share_of_e2e_pct: { decode_time: 85.7 },
                    share_of_ttft_pct: {},
                },
                p90: { available: false },
                series: [],
            },
        },
        client_series: {
            ...baseRich.client_series,
            windows: {
                profiling: { duration_seconds: 105 },
                full: { duration_seconds: 120 },
            },
            cold_start_comparison: null,
            buckets: [],
            heatmap: {},
        },
        sessions_summary: { available: false },
        server_telemetry: {
            available: true,
            status: 'available',
            topology_mode: 'disaggregated',
            namespace: 'llm-d-sim',
            summary: { kv_cache_usage_percent: { max: 92.4, mean: 72.1 } },
            per_pod: [],
            series: [],
        },
    });

    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/telemetry')) return Response.json(telemetryPayload);
            if (url.includes('/api/v1/simulation/tasks/sim-anchor-1')) return Response.json({ task });
            return Response.json({ backends: [] });
        },
        async ({ root, doc, clickButton, flush }) => {
            const scrolled = [];
            doc.getElementById = (id) => {
                const match = findElements(doc.body, (el) => el.getAttribute('id') === id)[0] || null;
                if (match && !match.scrollIntoView) {
                    match.scrollIntoView = (opts) => scrolled.push({ id, opts });
                }
                return match;
            };
            globalThis.window.location = new URL('http://localhost/?taskId=sim-anchor-1');
            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={() => {}} />);
            });
            await flush();
            await flush();

            await clickButton(/^Bottlenecks$/);
            await flush();
            await clickButton(/Serving · Latency/);
            await flush();
            await flush();

            assert.ok(scrolled.some((entry) => entry.id === 's-latency' && entry.opts?.block === 'start'));
            assert.match(doc.body.textContent, /Request Stage Latency Breakdown/);
        },
    );
});

function extractTooltipElements(html) {
    const results = [];
    const markerRe = /<span\b[^>]*\brole="tooltip"[^>]*>/g;
    let match;
    while ((match = markerRe.exec(html)) !== null) {
        const openEnd = match.index + match[0].length;
        let depth = 1;
        const tagRe = /<\/?span\b[^>]*>/g;
        tagRe.lastIndex = openEnd;
        let tagMatch;
        let closeStart = -1;
        while ((tagMatch = tagRe.exec(html)) !== null) {
            if (tagMatch[0].startsWith('</')) {
                depth -= 1;
                if (depth === 0) {
                    closeStart = tagMatch.index;
                    break;
                }
            } else if (!tagMatch[0].endsWith('/>')) {
                depth += 1;
            }
        }
        if (closeStart !== -1) {
            const innerHtml = html.slice(openEnd, closeStart);
            const text = innerHtml.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
            results.push({ outerHtml: html.slice(match.index, closeStart + 7), innerHtml, text });
        }
    }
    return results;
}

const ENGINE_SPECIFIC_TOOLTIP_RE = /vllm:|sglang:|trtllm:|--kv-cache-metrics-sample|\bvllm\b/i;

test('All post-run tabs and RunCompareDrawer render .lens-help-formula below prose and contain zero engine-specific identifiers in role="tooltip"', async (t) => {
    const task = makeRichTask();
    const base = makeRichTelemetry();
    const servingRichTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            cache_config: {
                kv_cache_metrics_enabled: true,
                kv_cache_metrics_sample: 0.01,
                hbm_capacity_tokens: 131072,
                effective_capacity_tokens: 120000,
                block_size: 16,
                num_gpu_blocks: 8192,
                kv_cache_max_concurrency: 16,
                cpu_capacity_tokens: 262144,
            },
            derived: {
                ...(base.server_telemetry.derived || {}),
                kv_blocks_evicted_total: 4200,
                avoided_prefill_ratio: 44.0,
                recomputed_token_rate_tps: 12.5,
                pd_decision_ratio: 62.5,
                offloaded_bytes: 1024 * 1024 * 256,
                restored_bytes: 1024 * 1024 * 64,
            },
            summary: {
                ...base.server_telemetry.summary,
                cpu_cache_usage_percent: { mean: 22.0, max: 38.0 },
                kv_offload_bytes_per_second: { mean: 12 * 1024 * 1024, max: 24 * 1024 * 1024 },
                kv_restore_bytes_per_second: { mean: 6 * 1024 * 1024, max: 14 * 1024 * 1024 },
                kv_offload_lookup_latency_p95_ms: { mean: 1.8 },
                nixl_transfer_bytes_per_second: { mean: 48 * 1024 * 1024 },
                nixl_transfer_latency_p95_ms: { mean: 4.2 },
                nixl_failed_transfers_total: { max: 0 },
                kv_blocks_evicted_per_minute: { mean: 640, max: 1200 },
                kv_block_idle_before_evict_p50_seconds: { mean: 1.25 },
                kv_block_idle_before_evict_p90_seconds: { mean: 4.5 },
                kv_block_lifetime_p50_seconds: { mean: 12.4 },
                kv_block_lifetime_p90_seconds: { mean: 28.6 },
                kv_block_reuse_gap_p50_seconds: { mean: 0.35 },
                kv_block_reuse_gap_p90_seconds: { mean: 1.8 },
            },
            device_telemetry: {
                devices: [{ pod: 'qwen-decode-0', node: 'gpu-node-1', pci_bdf: '0000:18:00.0' }],
            },
            series: [
                {
                    elapsed_seconds: 10,
                    running_requests: 12,
                    waiting_requests: 3,
                    kv_cache_usage_percent: 70,
                    gpu_utilization_percent: 80,
                    gpu_power_watts: 305,
                    kv_blocks_evicted_per_minute: 500,
                    kv_block_idle_before_evict_p50_seconds: 1.1,
                    kv_block_idle_before_evict_p90_seconds: 4.0,
                    kv_block_lifetime_p50_seconds: 11.5,
                    kv_block_lifetime_p90_seconds: 26.0,
                    kv_block_reuse_gap_p50_seconds: 0.3,
                    kv_block_reuse_gap_p90_seconds: 1.5,
                    nixl_transfer_bytes_per_second: 42 * 1024 * 1024,
                },
            ],
        },
    };
    const servingAggregatedTelemetry = {
        ...servingRichTelemetry,
        server_telemetry: {
            ...servingRichTelemetry.server_telemetry,
            topology_mode: 'aggregated',
            derived: {
                ...servingRichTelemetry.server_telemetry.derived,
                pd_decision_ratio: null,
            },
            summary: {
                ...servingRichTelemetry.server_telemetry.summary,
                nixl_transfer_bytes_per_second: undefined,
                nixl_transfer_latency_p95_ms: undefined,
                nixl_failed_transfers_total: undefined,
            },
            series: servingRichTelemetry.server_telemetry.series.map((pt) => ({
                ...pt,
                nixl_transfer_bytes_per_second: undefined,
            })),
        },
    };

    const tabRenders = [
        ['OverviewTab', renderToStaticMarkup(<OverviewTab task={task} telemetry={base} analysisWindow="profiling" />)],
        ['BottlenecksTab', renderToStaticMarkup(<BottlenecksTab telemetry={base} onSelectTab={() => {}} />)],
        ['ServingTab (disaggregated)', renderToStaticMarkup(<ServingTab telemetry={servingRichTelemetry} />)],
        ['ServingTab (aggregated)', renderToStaticMarkup(<ServingTab telemetry={servingAggregatedTelemetry} />)],
        ['RequestsTab', renderToStaticMarkup(<RequestsTab task={task} telemetry={base} analysisWindow="profiling" onOpenErrorInspector={() => {}} />)],
        ['SystemTab', renderToStaticMarkup(<SystemTab telemetry={base} />)],
        ['HardwareTab', renderToStaticMarkup(<HardwareTab task={task} telemetry={base} onSelectTab={() => {}} />)],
    ];

    for (const [tabName, html] of tabRenders) {
        assertNoNestedButtons(html, tabName);
        const tooltips = extractTooltipElements(html);
        assert.ok(tooltips.length > 0, `Expected tooltips on ${tabName}`);

        const formulaTooltips = tooltips.filter((tip) => tip.innerHtml.includes('lens-help-formula'));
        assert.ok(formulaTooltips.length >= 5, `Expected .lens-help-formula blocks on ${tabName}, got ${formulaTooltips.length}`);

        for (const tip of formulaTooltips) {
            const formulaTagIdx = tip.innerHtml.search(/<span\b[^>]*\blens-help-formula\b/);
            assert.ok(formulaTagIdx > 0, `Expected non-empty content before .lens-help-formula on ${tabName}: ${tip.text}`);
            const proseBefore = tip.innerHtml.slice(0, formulaTagIdx).replace(/<[^>]+>/g, '').trim();
            assert.ok(proseBefore.length > 0, `Expected non-empty prose above .lens-help-formula on ${tabName}: ${tip.text}`);
            assert.match(tip.innerHtml.slice(formulaTagIdx), />Formula<\/span>\s*<code[^>]*>[^<]+<\/code>/);
        }

        for (const tip of tooltips) {
            assert.doesNotMatch(
                tip.text,
                ENGINE_SPECIFIC_TOOLTIP_RE,
                `Found forbidden engine-specific identifier in ${tabName} tooltip: ${tip.text}`,
            );
        }
    }

    // Verify kvResidencyCharts and ServingTab sample rate formula details explicitly
    const servingHtml = tabRenders[2][1];
    assert.match(servingHtml, /1 \/ Sampling_Fraction/);
    assert.match(servingHtml, /ΔKV_Block_Idle_Before_Evict_Buckets/);
    assert.match(servingHtml, /ΔKV_Block_Lifetime_Buckets/);
    assert.match(servingHtml, /ΔKV_Block_Reuse_Gap_Buckets/);

    // Verify RunCompareDrawer renders .lens-help-formula below prose and zero engine-specific identifiers
    const tasksById = {
        'sim-1': makeRichTask({ id: 'sim-1', name: 'baseline-run' }),
        'sim-2': makeRichTask({ id: 'sim-2', name: 'tuned-run' }),
    };
    await withMountedDom(
        t,
        async () => Response.json({
            ...base,
            client_series: { ...base.client_series, buckets: [] },
            server_telemetry: { ...base.server_telemetry, series: [] },
        }),
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-1', 'sim-2']}
                        tasksById={tasksById}
                    />,
                );
            });
            await flush();

            const tooltips = findElements(doc.body, (el) => el.getAttribute('role') === 'tooltip');
            assert.ok(tooltips.length > 0, 'Expected tooltips in RunCompareDrawer');
            const formulaBlocks = findElements(
                doc.body,
                (el) => String(el.getAttribute('class') || '').includes('lens-help-formula'),
            );
            assert.ok(formulaBlocks.length >= 4, `Expected .lens-help-formula blocks in RunCompareDrawer, got ${formulaBlocks.length}`);

            for (const tip of tooltips) {
                assert.doesNotMatch(
                    tip.textContent,
                    ENGINE_SPECIFIC_TOOLTIP_RE,
                    `Found forbidden engine-specific identifier in RunCompareDrawer tooltip: ${tip.textContent}`,
                );
                const formulaChild = findElements(
                    tip,
                    (el) => String(el.getAttribute('class') || '').includes('lens-help-formula'),
                )[0];
                if (formulaChild) {
                    const formulaText = formulaChild.textContent;
                    const proseLen = tip.textContent.indexOf(formulaText);
                    assert.ok(proseLen > 0, `Expected prose before .lens-help-formula in RunCompareDrawer tooltip: ${tip.textContent}`);
                    assert.match(formulaText, /^Formula/);
                }
            }
        },
    );
});

test('Log severity classifier separates real AIPerf INFO/summary/CLI lines from WARNING, ERROR, and multiline Python tracebacks', () => {
    const esc = String.fromCharCode(27);
    assert.equal(
        stripAnsi(`${esc}[32m10:18:09.451${esc}[0m ${esc}[36mINFO${esc}[0m     OK`),
        '10:18:09.451 INFO     OK',
    );
    assert.equal(isLogErrorOrWarning('error'), true);
    assert.equal(isLogErrorOrWarning('warning'), true);
    assert.equal(isLogErrorOrWarning('info'), false);

    const infoSamples = [
        `${esc}[32m10:18:09.451${esc}[0m INFO     HTTP Request: GET http://localhost:8000/v1/models "HTTP/1.1 200 OK" (_client.py:1025)`,
        '10:18:27.589 INFO     Registered Worker Manager worker-0 (system_controller.py:411)',
        '10:18:25.186 INFO     HTTP Request: HEAD https://huggingface.co/Qwen/Qwen3-8B/resolve/main/added_tokens.json "HTTP/1.1 404 Not Found" (_client.py:1025)',
        '2026-11-25 10:18:07.667 - aiperf - INFO - request_timeout=300, failed=0',
        '2026-11-25T10:18:07.667Z INFO Starting replay worker',
        'INFO:     127.0.0.1:42012 - "GET /health HTTP/1.1" 200',
        '[info] Runner initialized with 16 concurrency slots',
        'ts=2026-11-25T10:18:07Z level=info msg="error_rate_slo=5"',
        'Error Records: 0',
        'Failed Requests: 0',
        'warnings: 0',
        '0 errors',
        '0 warnings',
        '0 failed',
        '│ Error Records │ 0 │',
        'CLI Command: aiperf profile --model Qwen/Qwen3-8B --timeout 30 --request-timeout 300',
        'CLI Command: aiperf profile --error-rate-slo 0.05 --request-timeout 30 --log-level=error',
        'aiperf profile --log-level=error',
        'error_rate_slo=5.0',
        'request_timeout=300',
        'early_stop_error_threshold=0',
        'Subprocess exited with code 0',
        '10:18:28.000 DEBUG    Traceback (most recent call last): synthetic debug marker',
    ];
    for (const line of infoSamples) {
        assert.equal(classifyLogLine(line), 'info', `Expected info for line: ${line}`);
    }

    const warningSamples = [
        '10:18:12.913 WARNING  Warning: You are sending unauthenticated requests to the HF Hub.',
        '2026-11-25 10:18:12.913 - aiperf - WARNING - Warmup latency elevated',
        '[warn] Retrying token probe',
        'level=warning msg="slow scrape"',
        'Warning: You are sending unauthenticated requests to the HF Hub.',
        'DeprecationWarning: pkg_resources is deprecated as an API',
        'UserWarning: Custom tokenizer fallback enabled',
    ];
    for (const line of warningSamples) {
        assert.equal(classifyLogLine(line), 'warning', `Expected warning for line: ${line}`);
    }

    const errorSamples = [
        '10:18:30.000 ERROR    Failed to connect',
        '2026-11-25 10:18:30.000 - aiperf - CRITICAL - Worker crashed',
        '[error] Runner exited with code 1',
        'level=error msg="upstream reset"',
        'HTTP/1.1 503 Service Unavailable',
        'status_code=502 upstream bad gateway',
        'connection refused by 127.0.0.1:8000',
        'Task failed while waiting for runner',
        'Benchmark runner failed unexpectedly',
        'Task failed with 0 warnings',
        'HTTP/1.1 500 Internal Server Error (0 warnings)',
        'Failed Requests: 0; RuntimeError: crash',
        'Failed Requests: 0; HTTP/1.1 500 Internal Server Error',
        'Error Records: 0 | connection refused',
        'Error: failed to bind port 8000',
        'Exception: unexpected EOF',
    ];
    for (const line of errorSamples) {
        assert.equal(classifyLogLine(line), 'error', `Expected error for line: ${line}`);
    }

    const multilineLines = [
        '10:18:09.451 INFO     HTTP Request: GET http://localhost:8000/v1/models "HTTP/1.1 200 OK" (_client.py:1025)',
        '10:18:12.913 WARNING  Warning: You are sending unauthenticated requests to the HF Hub.',
        'Traceback (most recent call last):',
        '  File "/opt/aiperf/runner.py", line 88, in dispatch',
        '    raise RuntimeError("boom")',
        'RuntimeError: boom',
        '10:18:31.000 INFO     Worker restarted cleanly',
        '  indented post-recovery detail should remain info',
        'Traceback (most recent call last):',
        '  File "/opt/aiperf/worker.py", line 12, in step',
        '10:18:32.000 INFO     Next structured INFO header resets truncated traceback state',
        '  another indented info line after reset',
        // Chained Python 3 traceback with blank lines around chain separator
        'Traceback (most recent call last):',
        '  File "a.py", line 1, in <module>',
        '    foo()',
        'ValueError: bad',
        '',
        'During handling of the above exception, another exception occurred:',
        '',
        'Traceback (most recent call last):',
        '  File "b.py", line 2, in <module>',
        '    bar()',
        'RuntimeError: chained',
        // Back-to-back truncated tracebacks without blank separator
        'Traceback (most recent call last):',
        '  File "c.py", line 3, in <module>',
        'Traceback (most recent call last):',
        '  File "d.py", line 4, in <module>',
        'RuntimeError: second',
    ];
    const classified = classifyLogLines(multilineLines);
    assert.deepEqual(
        classified.map((entry) => entry.severity),
        [
            'info',
            'warning',
            'error',
            'error',
            'error',
            'error',
            'info',
            'info',
            'error',
            'error',
            'info',
            'info',
            'error',
            'error',
            'error',
            'error',
            'info',
            'error',
            'info',
            'error',
            'error',
            'error',
            'error',
            'error',
            'error',
            'error',
            'error',
            'error',
        ],
    );

    // 50 KB adversarial ReDoS guard: every line must classify in linear time (< 50 ms total)
    const adversarialLines = [
        `errors ${'='.repeat(50000)}1`,
        `│ errors ${' '.repeat(50000)}= 1`,
        `${' '.repeat(50000)}x`,
        `2026-11-26 00:00:00 - ${' '.repeat(50000)}x`,
        `2026-11-26 00:00:00 - logger ${' '.repeat(50000)}x`,
    ];
    const t0 = performance.now();
    const adversarialResults = classifyLogLines(adversarialLines);
    const elapsedMs = performance.now() - t0;
    assert.equal(adversarialResults.length, adversarialLines.length);
    assert.ok(elapsedMs < 50, `Expected 50 KB adversarial classification in <50ms, took ${elapsedMs.toFixed(2)}ms`);
});

test('LogsAndConfigTab Errors & warnings only toggle hides INFO lines and highlights warnings amber and errors red', async (t) => {
    const logLines = [
        '10:18:09.451 INFO     HTTP Request: GET http://localhost:8000/v1/models "HTTP/1.1 200 OK" (_client.py:1025)',
        '10:18:25.186 INFO     HTTP Request: HEAD .../added_tokens.json "HTTP/1.1 404 Not Found" (_client.py:1025)',
        '2026-11-25 10:18:07.667 - aiperf - INFO - request_timeout=300, failed=0',
        'Error Records: 0',
        'Failed Requests: 0',
        'warnings: 0',
        'CLI Command: aiperf profile --timeout 30',
        '10:18:12.913 WARNING  Warning: You are sending unauthenticated requests to the HF Hub.',
        'DeprecationWarning: legacy flag will be removed',
        '10:18:30.000 ERROR    Failed to connect',
        '[error] Runner exited with code 1',
        'HTTP/1.1 503 Service Unavailable',
        'connection refused',
        'Traceback (most recent call last):',
        '  File "/opt/aiperf/runner.py", line 88, in dispatch',
        '    raise RuntimeError("boom")',
        'RuntimeError: boom',
        '10:18:31.000 INFO     Recovery check passed',
    ];
    const task = makeRichTask({
        id: 'sim-logs-1',
        logs: logLines.slice(0, 9),
    });
    const telemetryPayload = {
        forensics: {},
        client_series: {
            buckets: [],
            cold_start_comparison: null,
            windows: {
                profiling: { duration_seconds: 105 },
                full: { duration_seconds: 120 },
            },
        },
        server_telemetry: { available: false, per_pod: [], series: [] },
        runner_logs: logLines.slice(9),
    };

    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/telemetry')) return Response.json(telemetryPayload);
            if (url.includes('/api/v1/simulation/tasks/sim-logs-1')) return Response.json({ task });
            return Response.json({ backends: [] });
        },
        async ({ root, doc, clickButton, flush }) => {
            globalThis.window.location = new URL('http://localhost/?taskId=sim-logs-1');
            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={() => {}} />);
            });
            await flush();
            await flush();

            await clickButton(/Logs & Configuration/);
            await flush();

            assert.match(doc.body.textContent, /18 lines/);
            assert.match(doc.body.textContent, /HTTP\/1\.1 404 Not Found/);
            assert.match(doc.body.textContent, /Error Records: 0/);
            assert.match(doc.body.textContent, /CLI Command: aiperf profile --timeout 30/);

            const findLogLineEl = (substring) => findElements(
                doc.body,
                (el) => el.tagName === 'DIV'
                    && el.childNodes.length === 1
                    && el.childNodes[0].nodeType === 3
                    && el.textContent.includes(substring),
            )[0] || null;

            const info404El = findLogLineEl('added_tokens.json');
            const warningEl = findLogLineEl('10:18:12.913 WARNING');
            const deprecationEl = findLogLineEl('DeprecationWarning:');
            const errorEl = findLogLineEl('10:18:30.000 ERROR');
            const tracebackFrameEl = findLogLineEl('File "/opt/aiperf/runner.py"');
            const runtimeErrEl = findLogLineEl('RuntimeError: boom');
            const recoveryInfoEl = findLogLineEl('Recovery check passed');

            assert.ok(info404El && warningEl && deprecationEl && errorEl && tracebackFrameEl && runtimeErrEl && recoveryInfoEl);
            assert.doesNotMatch(info404El.getAttribute('class') || '', /text-red-|text-amber-/);
            assert.doesNotMatch(recoveryInfoEl.getAttribute('class') || '', /text-red-|text-amber-/);
            assert.match(warningEl.getAttribute('class') || '', /text-amber-700 dark:text-amber-300/);
            assert.match(deprecationEl.getAttribute('class') || '', /text-amber-700 dark:text-amber-300/);
            assert.match(errorEl.getAttribute('class') || '', /text-red-600 dark:text-red-300/);
            assert.match(tracebackFrameEl.getAttribute('class') || '', /text-red-600 dark:text-red-300/);
            assert.match(runtimeErrEl.getAttribute('class') || '', /text-red-600 dark:text-red-300/);

            const toggleLabel = findElements(
                doc.body,
                (el) => el.tagName === 'LABEL' && el.textContent.includes('Errors & warnings only'),
            )[0];
            assert.ok(toggleLabel, 'Expected Errors & warnings only checkbox label');
            const checkbox = findElements(
                toggleLabel,
                (el) => el.tagName === 'INPUT' && reactProps(el).type === 'checkbox',
            )[0];
            assert.ok(checkbox, 'Expected Errors & warnings only checkbox input');

            await act(async () => {
                reactProps(checkbox).onChange({ target: { checked: true } });
            });
            await flush();

            // 2 warnings + 8 error/traceback lines = 10 lines; all 8 INFO/summary/CLI lines hidden
            assert.match(doc.body.textContent, /10 lines/);
            assert.doesNotMatch(doc.body.textContent, /added_tokens\.json/);
            assert.doesNotMatch(doc.body.textContent, /request_timeout=300/);
            assert.doesNotMatch(doc.body.textContent, /Error Records: 0/);
            assert.doesNotMatch(doc.body.textContent, /Failed Requests: 0/);
            assert.doesNotMatch(doc.body.textContent, /warnings: 0/);
            assert.doesNotMatch(doc.body.textContent, /CLI Command: aiperf profile/);
            assert.doesNotMatch(doc.body.textContent, /Recovery check passed/);

            const filteredWarningEl = findLogLineEl('10:18:12.913 WARNING');
            const filteredDeprecationEl = findLogLineEl('DeprecationWarning:');
            const filteredTracebackEl = findLogLineEl('File "/opt/aiperf/runner.py"');
            const filteredRuntimeErrEl = findLogLineEl('RuntimeError: boom');
            assert.ok(filteredWarningEl && filteredDeprecationEl && filteredTracebackEl && filteredRuntimeErrEl);
            assert.match(filteredWarningEl.getAttribute('class') || '', /text-amber-700 dark:text-amber-300/);
            assert.match(filteredDeprecationEl.getAttribute('class') || '', /text-amber-700 dark:text-amber-300/);
            assert.match(filteredTracebackEl.getAttribute('class') || '', /text-red-600 dark:text-red-300/);
            assert.match(filteredRuntimeErrEl.getAttribute('class') || '', /text-red-600 dark:text-red-300/);
        },
    );
});

test('LiveMonitorPanel LiveLogPanel classifies warning vs error badge tones and filters with Errors & warnings only', async (t) => {
    const infoOnlyAndWarnLogs = [
        '10:18:09.451 INFO     HTTP Request: GET http://localhost:8000/v1/models "HTTP/1.1 200 OK" (_client.py:1025)',
        '10:18:25.186 INFO     HTTP Request: HEAD .../added_tokens.json "HTTP/1.1 404 Not Found" (_client.py:1025)',
        'Warning: You are sending unauthenticated requests to the HF Hub.',
    ];
    const warnHtml = renderToStaticMarkup(
        <LiveMonitorPanel
            task={makeRichTask({ id: 'sim-live-warn', status: 'running', logs: infoOnlyAndWarnLogs })}
            liveTelemetry={{ live_phase: 'Profiling', kpis: {}, progress: {}, alerts: [], recent_logs: [] }}
        />,
    );
    // Warning-only logs keep LiveLogPanel collapsed by default and render warning badge tone (amber)
    assert.match(warnHtml, /Log tail collapsed \(3 lines\)/);
    assert.match(warnHtml, /amber[^>]*>3 lines<\/span>/);

    await withMountedDom(
        t,
        async () => Response.json({}),
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeRichTask({
                            id: 'sim-live-err',
                            status: 'running',
                            logs: [
                                ...infoOnlyAndWarnLogs,
                                '10:18:30.000 ERROR    Failed to connect',
                            ],
                        })}
                        liveTelemetry={{ live_phase: 'Profiling', kpis: {}, progress: {}, alerts: [], recent_logs: [] }}
                    />,
                );
            });
            await flush();

            // Error line auto-expands LiveLogPanel and shows 4 lines
            assert.match(doc.body.textContent, /4 lines/);
            assert.match(doc.body.textContent, /Errors & warnings only/);
            assert.match(doc.body.textContent, /added_tokens\.json/);

            const toggleLabel = findElements(
                doc.body,
                (el) => el.tagName === 'LABEL' && el.textContent.includes('Errors & warnings only'),
            )[0];
            const checkbox = findElements(
                toggleLabel,
                (el) => el.tagName === 'INPUT' && reactProps(el).type === 'checkbox',
            )[0];
            await act(async () => {
                reactProps(checkbox).onChange({ target: { checked: true } });
            });
            await flush();

            assert.doesNotMatch(doc.body.textContent, /added_tokens\.json/);
            assert.match(doc.body.textContent, /Warning: You are sending unauthenticated requests/);
            assert.match(doc.body.textContent, /10:18:30\.000 ERROR\s+Failed to connect/);
        },
    );
});

test('OverviewTab and BottlenecksTab categorical charts render category titles and units while time-series charts preserve Elapsed <t> s', () => {
    const base = makeRichTelemetry();
    const overviewTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            per_pod: [
                {
                    ...base.server_telemetry.per_pod[0],
                    gpu_power_watts: { mean: 280, max: 350 },
                },
                {
                    ...base.server_telemetry.per_pod[1],
                    gpu_power_watts: { mean: 310, max: 420 },
                },
            ],
        },
    };

    let overviewTree = null;
    function CaptureOverviewCharts() {
        overviewTree = OverviewTab({
            task: makeRichTask(),
            telemetry: overviewTelemetry,
            analysisWindow: 'profiling',
        });
        return null;
    }
    renderToStaticMarkup(<CaptureOverviewCharts />);

    // 1. Warmup vs Steady-State Latency (categorical BarChart)
    const warmupChart = findElement(
        overviewTree,
        (n) => n.type === BarChart && Array.isArray(n.props?.data) && n.props.data.some((d) => d.metric === 'TTFT P50'),
    );
    assert.ok(warmupChart, 'Expected Warmup vs Steady-State Latency BarChart');
    const warmupTooltip = findElement(warmupChart, (n) => n.type === Tooltip);
    assert.ok(warmupTooltip, 'Expected Tooltip on Warmup vs Steady-State Latency chart');

    const ttftTipHtml = renderToStaticMarkup(
        warmupTooltip.props.content.type({
            active: true,
            label: 'TTFT P50',
            payload: [
                { dataKey: 'cold', name: 'Warmup Phase', color: CHART_SERIES[2], value: 380, payload: { metric: 'TTFT P50', unit: 'ms' } },
                { dataKey: 'steady', name: 'Steady-State', color: CHART_SERIES[0], value: 130, payload: { metric: 'TTFT P50', unit: 'ms' } },
            ],
        }),
    );
    assert.match(ttftTipHtml, /TTFT P50/);
    assert.match(ttftTipHtml, /380 ms/);
    assert.match(ttftTipHtml, /130 ms/);
    assert.doesNotMatch(ttftTipHtml, /Elapsed|NaN/);

    const tpotTipHtml = renderToStaticMarkup(
        warmupTooltip.props.content.type({
            active: true,
            label: 'TPOT P90',
            payload: [
                { dataKey: 'cold', name: 'Warmup Phase', color: CHART_SERIES[2], value: 45, payload: { metric: 'TPOT P90' } },
                { dataKey: 'steady', name: 'Steady-State', color: CHART_SERIES[0], value: 28, payload: { metric: 'TPOT P90' } },
            ],
        }),
    );
    assert.match(tpotTipHtml, /TPOT P90/);
    assert.match(tpotTipHtml, /45 ms\/tok/);
    assert.match(tpotTipHtml, /28 ms\/tok/);
    assert.doesNotMatch(tpotTipHtml, /Elapsed|NaN/);

    // 2. Accelerator Power & Energy (categorical BarChart)
    const powerChart = findElement(
        overviewTree,
        (n) => n.type === BarChart && Array.isArray(n.props?.data) && n.props.data.some((d) => d.avg_w !== undefined),
    );
    assert.ok(powerChart, 'Expected Accelerator Power & Energy BarChart');
    const powerTooltip = findElement(powerChart, (n) => n.type === Tooltip);
    assert.ok(powerTooltip, 'Expected Tooltip on Accelerator Power & Energy chart');

    const powerTipHtml = renderToStaticMarkup(
        powerTooltip.props.content.type({
            active: true,
            label: 'worker-0',
            payload: [
                { dataKey: 'avg_w', name: 'Average Power', color: CHART_SERIES[0], value: 310, payload: { name: 'worker-0', avg_w: 310, peak_w: 420 } },
                { dataKey: 'peak_w', name: 'Peak Power', color: CHART_SERIES[2], value: 420, payload: { name: 'worker-0', avg_w: 310, peak_w: 420 } },
            ],
        }),
    );
    assert.match(powerTipHtml, /worker-0/);
    assert.match(powerTipHtml, /310 W/);
    assert.match(powerTipHtml, /420 W/);
    assert.doesNotMatch(powerTipHtml, /Elapsed|NaN/);

    // 3. Latency over Time (time-series ComposedChart preserves Elapsed <t> s)
    const latencyTimeChart = findElement(
        overviewTree,
        (n) => n.type === ComposedChart && Boolean(findElement(n, (c) => c.type === Line && c.props?.dataKey === 'ttft_p50')),
    );
    assert.ok(latencyTimeChart, 'Expected Latency over Time ComposedChart');
    const latencyTimeTooltip = findElement(latencyTimeChart, (n) => n.type === Tooltip);
    const latencyTimeTipHtml = renderToStaticMarkup(
        latencyTimeTooltip.props.content.type({
            active: true,
            label: 12.5,
            payload: [
                { dataKey: 'ttft_p50', name: 'TTFT P50', color: CHART_SERIES[0], value: 130 },
            ],
        }),
    );
    assert.match(latencyTimeTipHtml, /Elapsed 12\.50 s/);
    assert.match(latencyTimeTipHtml, /130 ms/);
    assert.doesNotMatch(latencyTimeTipHtml, /NaN/);

    // 4. BottlenecksTab: TTFT Stage Decomposition (categorical) & Request Stage Latency Breakdown (time-series)
    const bottlenecksTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                p50: { available: true, prefill_time_ms: 95, queue_time_ms: 35, decode_time_ms: 400 },
                series: [
                    { t: 12.5, epp_sched_ms: 2, queue_ms: 35, prefill_ms: 95, decode_ms: 400 },
                ],
            },
        },
    };
    let bottlenecksTree = null;
    function CaptureBottlenecksCharts() {
        bottlenecksTree = BottlenecksTab({ telemetry: bottlenecksTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureBottlenecksCharts />);

    const ttftDecompChart = findElement(
        bottlenecksTree,
        (n) => n.type === BarChart && Array.isArray(n.props?.data) && n.props.data.some((d) => d.stat === 'P90'),
    );
    assert.ok(ttftDecompChart, 'Expected TTFT Stage Decomposition BarChart');
    const ttftDecompTooltip = findElement(ttftDecompChart, (n) => n.type === Tooltip);
    const ttftDecompTipHtml = renderToStaticMarkup(
        ttftDecompTooltip.props.content.type({
            active: true,
            label: 'P90',
            payload: [
                { dataKey: 'prefill_ms', name: 'Prefill Time', color: CHART_SERIES[0], value: 210, payload: { stat: 'P90' } },
                { dataKey: 'queue_ms', name: 'Queue Time', color: CHART_SERIES[2], value: 110, payload: { stat: 'P90' } },
                { dataKey: 'overhead_ms', name: 'Client / Gateway / EPP Overhead', color: CHART_SERIES[1], value: 25, payload: { stat: 'P90' } },
            ],
        }),
    );
    assert.match(ttftDecompTipHtml, /P90/);
    assert.match(ttftDecompTipHtml, /210 ms/);
    assert.match(ttftDecompTipHtml, /345 ms/);
    assert.doesNotMatch(ttftDecompTipHtml, /Elapsed|NaN/);

    const stageSeriesChart = findElement(
        bottlenecksTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'prefill_ms')),
    );
    assert.ok(stageSeriesChart, 'Expected Request Stage Latency Breakdown AreaChart');
    const stageSeriesTooltip = findElement(stageSeriesChart, (n) => n.type === Tooltip);
    const stageSeriesTipHtml = renderToStaticMarkup(
        stageSeriesTooltip.props.content.type({
            active: true,
            label: 12.5,
            payload: [
                { dataKey: 'prefill_ms', name: 'Prefill Time', color: CHART_SERIES[0], value: 95 },
                { dataKey: 'decode_ms', name: 'Decode Time', color: CHART_SERIES[1], value: 400 },
            ],
        }),
    );
    assert.match(stageSeriesTipHtml, /Elapsed 12\.50 s/);
    assert.match(stageSeriesTipHtml, /495 ms/);
    assert.doesNotMatch(stageSeriesTipHtml, /NaN/);
});

test('resolveConfiguredConcurrency extracts positive finite concurrency and ignores trace-replayer and invalid values', () => {
    assert.equal(resolveConfiguredConcurrency({ simulation: { backend: 'aiperf', backend_options: { concurrency: 64 } } }), 64);
    assert.equal(resolveConfiguredConcurrency({ simulation: { backend: 'aiperf', backend_options: { max_concurrency: '32' } } }), 32);
    assert.equal(resolveConfiguredConcurrency({ backend_options: { session_concurrency: 16 } }), 16);
    assert.equal(resolveConfiguredConcurrency({}, { backend: 'aiperf', replay: { backend_options: { concurrency: 48 } } }), 48);
    assert.equal(resolveConfiguredConcurrency({}, { backend: 'aiperf', backend_options: { max_concurrency: 24 } }), 24);
    assert.equal(resolveConfiguredConcurrency({ config_snapshot: { backend_options: { concurrency: 12 } } }), 12);
    assert.equal(resolveConfiguredConcurrency({ config_snapshot: { max_concurrency: 10 } }), 10);
    assert.equal(resolveConfiguredConcurrency({ concurrency: 8 }), 8);

    // trace-replayer never surfaces concurrency, even if threads/num_producer/channel_capacity or concurrency exist
    assert.equal(
        resolveConfiguredConcurrency({
            simulation: {
                backend: 'trace-replayer',
                backend_options: { threads: 32, num_producer: 4, channel_capacity: 128, concurrency: 64 },
            },
        }),
        null,
    );
    assert.equal(
        resolveConfiguredConcurrency({
            backend: 'Trace_Replayer',
            backend_options: { threads: 16, max_concurrency: 16 },
        }),
        null,
    );
    assert.equal(
        resolveConfiguredConcurrency(
            { simulation: { backend_options: { concurrency: 64 } } },
            { backend: 'trace-replayer' },
        ),
        null,
    );

    // Non-concurrency options and invalid values return null
    for (const badVal of [0, -4, NaN, Infinity, true, false, '', '   ', null, undefined, {}, []]) {
        assert.equal(
            resolveConfiguredConcurrency({
                simulation: {
                    backend: 'aiperf',
                    backend_options: { concurrency: badVal, threads: 32, num_producer: 4, channel_capacity: 64 },
                },
            }),
            null,
        );
    }
});

test('OverviewTab In-Flight Concurrency chart renders configured concurrency reference line, extended domain, and legend only when configured', () => {
    const base = makeRichTelemetry();
    const renderOverview = (task, telemetryOverride = {}) => {
        let tree = null;
        function CaptureOverview() {
            tree = OverviewTab({
                task,
                telemetry: {
                    ...base,
                    ...telemetryOverride,
                },
                analysisWindow: 'profiling',
            });
            return null;
        }
        renderToStaticMarkup(<CaptureOverview />);
        return tree;
    };

    const findInFlightChart = (tree) => findElement(
        tree,
        (n) => n.type === LineChart && Boolean(findElement(n, (c) => c.type === Line && c.props?.dataKey === 'client_inflight')),
    );

    // 1. Configured concurrency (64) exceeds observed peak (14) -> ReferenceLine, YAxis domain > 64, and legend entry
    const configuredTree = renderOverview(
        makeRichTask({
            simulation: {
                duration_seconds: 120,
                backend: 'aiperf',
                backend_options: {
                    warmup_seconds: 15,
                    concurrency: 64,
                },
            },
        }),
    );
    const configuredChart = findInFlightChart(configuredTree);
    assert.ok(configuredChart, 'Expected In-Flight Concurrency LineChart');
    const configuredYAxis = findElement(configuredChart, (n) => n.type === YAxis);
    assert.ok(configuredYAxis, 'Expected YAxis on In-Flight Concurrency chart');
    assert.ok(Array.isArray(configuredYAxis.props.domain), 'Expected explicit YAxis domain when concurrency is configured');
    assert.equal(configuredYAxis.props.domain[0], 0);
    assert.ok(configuredYAxis.props.domain[1] > 64, `Expected YAxis domain max > 64, got ${configuredYAxis.props.domain[1]}`);

    const configuredRefLine = findElement(
        configuredChart,
        (n) => n.type === ReferenceLine && n.props?.y === 64,
    );
    assert.ok(configuredRefLine, 'Expected horizontal ReferenceLine at y=64');
    assert.equal(configuredRefLine.props.stroke, CHART_SERIES[2]);
    assert.equal(configuredRefLine.props.strokeDasharray, '6 4');
    assert.equal(configuredRefLine.props.strokeWidth, 1.5);
    assert.equal(configuredRefLine.props.ifOverflow, 'extendDomain');
    assert.deepEqual(configuredRefLine.props.label, {
        value: 'Configured: 64',
        position: 'insideTopRight',
        fill: CHART_SERIES[2],
        fontSize: 10,
        fontWeight: 600,
    });

    const configuredLegends = findAllElements(configuredTree, (n) => n.type === ChartLegend);
    const configuredConcurrencyLegend = configuredLegends.find((leg) => leg.props?.entries?.some((e) => e.label === 'Configured Concurrency (64)'));
    assert.ok(configuredConcurrencyLegend, 'Expected Configured Concurrency (64) legend entry');
    assert.deepEqual(configuredConcurrencyLegend.props.entries, [
        { label: 'Client In-Flight Requests', color: CHART_SERIES[0] },
        { label: 'vLLM Running Requests', color: CHART_SERIES[1] },
        { label: 'Configured Concurrency (64)', color: CHART_SERIES[2] },
    ]);

    // 2. Client-only concurrency + configured concurrency -> 2 legend entries (>= 2) still renders ChartLegend
    const clientOnlyConfiguredTree = renderOverview(
        makeRichTask({
            simulation: {
                duration_seconds: 120,
                backend: 'aiperf',
                backend_options: { concurrency: 64 },
            },
        }),
        { server_telemetry: { available: false, status: 'unavailable', series: [] } },
    );
    const clientOnlyLegends = findAllElements(clientOnlyConfiguredTree, (n) => n.type === ChartLegend);
    const clientOnlyConcurrencyLegend = clientOnlyLegends.find((leg) => leg.props?.entries?.some((e) => e.label === 'Configured Concurrency (64)'));
    assert.ok(clientOnlyConcurrencyLegend, 'Expected ChartLegend with client in-flight + configured concurrency');
    assert.deepEqual(clientOnlyConcurrencyLegend.props.entries, [
        { label: 'Client In-Flight Requests', color: CHART_SERIES[0] },
        { label: 'Configured Concurrency (64)', color: CHART_SERIES[2] },
    ]);

    // 3. Unconfigured fixed-schedule run & trace-replayer with threads: 32 -> no ReferenceLine, no explicit domain, no configured legend
    for (const unconfiguredTask of [
        makeRichTask({
            simulation: { duration_seconds: 120, backend: 'aiperf', backend_options: { warmup_seconds: 15 } },
        }),
        makeRichTask({
            simulation: {
                duration_seconds: 120,
                backend: 'trace-replayer',
                backend_options: { warmup_seconds: 15, threads: 32, num_producer: 4, channel_capacity: 64 },
            },
        }),
    ]) {
        const unconfiguredTree = renderOverview(unconfiguredTask);
        const unconfiguredChart = findInFlightChart(unconfiguredTree);
        assert.ok(unconfiguredChart, 'Expected In-Flight Concurrency LineChart');
        const unconfiguredYAxis = findElement(unconfiguredChart, (n) => n.type === YAxis);
        assert.equal(unconfiguredYAxis.props.domain, undefined);
        const horizontalRefLines = findAllElements(
            unconfiguredChart,
            (n) => n.type === ReferenceLine && n.props?.y !== undefined,
        );
        assert.equal(horizontalRefLines.length, 0, 'Expected no horizontal ReferenceLine when concurrency is unconfigured');
        const unconfiguredLegends = findAllElements(unconfiguredTree, (n) => n.type === ChartLegend);
        for (const legend of unconfiguredLegends) {
            for (const entry of legend.props?.entries || []) {
                assert.doesNotMatch(entry.label, /Configured Concurrency/);
            }
        }
    }
});

test('RunCompareDrawer enforces MAX_COMPARE_RUNS === 3 cap and updates <2 runs empty state copy', async (t) => {
    assert.equal(MAX_COMPARE_RUNS, 3);
    const fetchedUrls = [];
    const tasksById = {
        'sim-1': makeRichTask({ id: 'sim-1', name: 'run-alpha' }),
        'sim-2': makeRichTask({ id: 'sim-2', name: 'run-beta' }),
        'sim-3': makeRichTask({ id: 'sim-3', name: 'run-gamma' }),
        'sim-4': makeRichTask({ id: 'sim-4', name: 'run-delta-ignored' }),
    };

    await withMountedDom(
        t,
        async (url) => {
            fetchedUrls.push(url);
            const base = makeRichTelemetry();
            return Response.json({
                ...base,
                client_series: { ...base.client_series, buckets: [] },
                server_telemetry: { ...base.server_telemetry, series: [] },
            });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(<RunCompareDrawer isOpen onClose={() => {}} taskIds={['sim-1']} tasksById={tasksById} />);
            });
            assert.match(doc.body.textContent, /Choose 2 or 3 completed, failed, or cancelled runs to compare side by side\./);
            assert.equal(fetchedUrls.length, 0);

            // Duplicate IDs only -> deduplicates to 1 unique run (<2 empty state, zero telemetry fetches)
            await act(async () => {
                root.render(<RunCompareDrawer isOpen onClose={() => {}} taskIds={['sim-1', ' sim-1 ']} tasksById={tasksById} />);
            });
            assert.match(doc.body.textContent, /Choose 2 or 3 completed, failed, or cancelled runs to compare side by side\./);
            assert.equal(fetchedUrls.length, 0);

            // Duplicates + >3 IDs -> deduplicates before slicing to MAX_COMPARE_RUNS (3 unique runs)
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-1', 'sim-1', 'sim-2', 'sim-3', 'sim-4']}
                        tasksById={tasksById}
                    />,
                );
            });
            await flush();

            assert.equal(fetchedUrls.length, 3);
            assert.ok(fetchedUrls.some((url) => url.includes('/sim-1/telemetry')));
            assert.ok(fetchedUrls.some((url) => url.includes('/sim-2/telemetry')));
            assert.ok(fetchedUrls.some((url) => url.includes('/sim-3/telemetry')));
            assert.ok(!fetchedUrls.some((url) => url.includes('/sim-4/telemetry')));
            assert.match(doc.body.textContent, /across 3 runs/);
            assert.match(doc.body.textContent, /run-alpha/);
            assert.match(doc.body.textContent, /run-beta/);
            assert.match(doc.body.textContent, /run-gamma/);
            assert.doesNotMatch(doc.body.textContent, /run-delta-ignored/);
        },
    );
});

test('RunCompareDrawer renders overflow-proof 3-run tables, charts, legends, and tooltips without clipping', async (t) => {
    const longLabel1 = 'very-long-baseline-simulation-run-name-with-extensive-experiment-parameters-001';
    const longLabel2 = 'very-long-candidate-simulation-run-name-with-extensive-experiment-parameters-002';
    const longLabel3 = 'very-long-candidate-simulation-run-name-with-extensive-experiment-parameters-003';
    const longModel = 'Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8-Long-Serving-Identifier';
    const longDataset = 'sharegpt-production-replay-trace-v4-multiturn-super-long-dataset-name';

    const tasksById = {
        'sim-1': makeRichTask({
            id: 'sim-1',
            name: longLabel1,
            model_name: longModel,
            dataset: longDataset,
            simulation: { duration_seconds: 120, backend: 'aiperf', backend_options: { concurrency: 64 } },
        }),
        'sim-2': makeRichTask({
            id: 'sim-2',
            name: longLabel2,
            model_name: longModel,
            dataset: longDataset,
            simulation: { duration_seconds: 120, backend: 'aiperf', backend_options: { concurrency: 128 } },
        }),
        'sim-3': makeRichTask({
            id: 'sim-3',
            name: longLabel3,
            model_name: longModel,
            dataset: longDataset,
            simulation: { duration_seconds: 120, backend: 'trace-replayer', backend_options: {} },
        }),
    };

    const collectFiberNodes = (rootNode, predicate) => {
        const containerKey = Object.keys(rootNode || {}).find((k) => k.startsWith('__reactContainer$'));
        const startFiber = containerKey ? rootNode[containerKey] : null;
        const matches = [];
        const visited = new Set();
        const walk = (fiber) => {
            if (!fiber || visited.has(fiber)) return;
            visited.add(fiber);
            if (predicate(fiber)) matches.push(fiber);
            walk(fiber.child);
            walk(fiber.sibling);
        };
        walk(startFiber);
        return matches;
    };

    await withMountedDom(
        t,
        async (url) => {
            const base = makeRichTelemetry();
            const mult = url.includes('/sim-2/') ? 1.25 : url.includes('/sim-3/') ? 0.9 : 1;
            return Response.json({
                ...base,
                run_manifest: {
                    ...base.run_manifest,
                    target: { model_name: longModel },
                    dataset: { name: longDataset },
                },
                forensics: {
                    ...base.forensics,
                    verdict: {
                        ...base.forensics.verdict,
                        throughput_rps: 14.2 * mult,
                        goodput_rps: 13.8 * mult,
                        attainment_pct: url.includes('/sim-2/') ? 99.1 : 95.4,
                    },
                },
                client_series: {
                    ...base.client_series,
                    buckets: [],
                    windows: {
                        ...base.client_series.windows,
                        profiling: {
                            ...base.client_series.windows.profiling,
                            throughput_rps: 14.2 * mult,
                            output_tok_per_s: 700 * mult,
                        },
                    },
                },
                server_telemetry: { ...base.server_telemetry, series: [] },
            });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-1', 'sim-2', 'sim-3']}
                        tasksById={tasksById}
                    />,
                );
            });
            await flush();

            const container = doc.body.firstChild;
            const outerWrapper = findElements(
                doc.body,
                (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('space-y-6'),
            )[0];
            assert.ok(outerWrapper, 'Expected outer space-y-6 wrapper inside Modal');
            const outerClass = String(outerWrapper.getAttribute('class') || '');
            assert.ok(outerClass.includes('min-w-0') && outerClass.includes('max-w-full'), `Expected min-w-0 max-w-full on outer wrapper, got: ${outerClass}`);

            const tables = findElements(doc.body, (el) => el.tagName === 'TABLE');
            assert.equal(tables.length, 2, 'Expected KPI Comparison Matrix and Configuration Comparison tables');
            for (const table of tables) {
                const wrapperClass = String(table.parentNode?.getAttribute('class') || '');
                assert.ok(
                    wrapperClass.includes('max-w-full')
                    && wrapperClass.includes('overflow-x-auto')
                    && wrapperClass.includes('lg:overflow-visible'),
                    `Expected overflow wrapper classes on table parent, got: ${wrapperClass}`,
                );
                const tableClass = String(table.getAttribute('class') || '');
                assert.ok(
                    tableClass.includes('w-full')
                    && tableClass.includes('min-w-[560px]')
                    && tableClass.includes('lg:min-w-0')
                    && tableClass.includes('lg:table-fixed'),
                    `Expected responsive table-fixed classes, got: ${tableClass}`,
                );
                const colgroup = findElements(table, (el) => el.tagName === 'COLGROUP')[0];
                assert.ok(colgroup, 'Expected <colgroup> in comparison table');
                const cols = findElements(colgroup, (el) => el.tagName === 'COL');
                assert.equal(cols.length, 4, 'Expected 4 <col> elements for metric/parameter + 3 runs');
                assert.equal(cols[0].getAttribute('class'), 'w-40 sm:w-44');
            }

            // KPI Comparison Matrix headers and cells
            const kpiTable = tables[0];
            const kpiHeaders = findElements(kpiTable, (el) => el.tagName === 'TH');
            assert.equal(kpiHeaders.length, 4);
            for (const th of kpiHeaders) {
                assert.ok(String(th.getAttribute('class') || '').includes('px-3 py-2.5'));
            }
            const kpiHeaderLabels = [longLabel1, longLabel2, longLabel3];
            kpiHeaders.slice(1).forEach((th, idx) => {
                const flexRow = findElements(th, (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('min-w-0'))[0];
                assert.ok(flexRow, 'Expected min-w-0 flex container in KPI run header');
                const labelSpan = findElements(th, (el) => el.tagName === 'SPAN' && el.textContent === kpiHeaderLabels[idx])[0];
                assert.ok(labelSpan, `Expected truncated label span for ${kpiHeaderLabels[idx]}`);
                const labelClass = String(labelSpan.getAttribute('class') || '');
                assert.ok(labelClass.includes('min-w-0') && labelClass.includes('flex-1') && labelClass.includes('truncate'));
                assert.equal(labelSpan.getAttribute('title'), kpiHeaderLabels[idx]);

                const expectedSubTitle = idx === 0 ? 'sim-1 · Baseline' : `sim-${idx + 1}`;
                const subDiv = findElements(th, (el) => el.tagName === 'DIV' && el.textContent === expectedSubTitle)[0];
                assert.ok(subDiv, `Expected subtitle ${expectedSubTitle}`);
                assert.ok(String(subDiv.getAttribute('class') || '').includes('truncate'));
                assert.equal(subDiv.getAttribute('title'), expectedSubTitle);
            });

            const kpiHelpTooltips = collectFiberNodes(
                container,
                (fiber) => fiber.memoizedProps?.label?.endsWith('delta column help'),
            );
            assert.equal(kpiHelpTooltips.length, 3);
            assert.equal(kpiHelpTooltips[0].memoizedProps.align, 'start');
            assert.equal(kpiHelpTooltips[1].memoizedProps.align, 'end');
            assert.equal(kpiHelpTooltips[2].memoizedProps.align, 'end');

            const metricCellWrappers = findElements(
                kpiTable,
                (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('flex-wrap') && String(el.getAttribute('class') || '').includes('gap-x-1.5'),
            );
            assert.ok(metricCellWrappers.length >= 3, 'Expected flex-wrap value/Badge/delta cell wrappers');
            for (const cellDiv of metricCellWrappers) {
                const cls = String(cellDiv.getAttribute('class') || '');
                assert.ok(cls.includes('min-w-0') && cls.includes('items-baseline') && cls.includes('gap-y-0.5'));
            }
            const kpiTds = findElements(kpiTable, (el) => el.tagName === 'TD');
            for (const td of kpiTds) {
                assert.ok(String(td.getAttribute('class') || '').includes('px-3 py-2.5'));
                const spacedChildren = findElements(td, (el) => /\bml-1\.5\b|\bml-2\b/.test(String(el.getAttribute('class') || '')));
                assert.equal(spacedChildren.length, 0, 'Expected no ml-1.5/ml-2 inline margins in KPI table cells');
            }

            // Verdict Badges have max-w-full, title, and inner min-w-0 truncate span
            const verdictRow = findElements(
                kpiTable,
                (el) => el.tagName === 'TR' && el.textContent.includes('Verdict'),
            )[0];
            assert.ok(verdictRow, 'Expected Verdict row in KPI Comparison Matrix');
            const verdictBadges = findElements(
                verdictRow,
                (el) => el.tagName === 'SPAN' && String(el.getAttribute('class') || '').includes('max-w-full'),
            );
            assert.equal(verdictBadges.length, 3, 'Expected 3 max-w-full Verdict badges');
            for (const badge of verdictBadges) {
                assert.ok(badge.getAttribute('title'), 'Expected title attribute on Verdict badge');
                const innerLabel = findElements(
                    badge,
                    (el) => el.tagName === 'SPAN'
                        && String(el.getAttribute('class') || '').includes('min-w-0')
                        && String(el.getAttribute('class') || '').includes('truncate'),
                )[0];
                assert.ok(innerLabel, 'Expected inner min-w-0 truncate span inside Verdict Badge');
                assert.equal(innerLabel.textContent, badge.getAttribute('title'));
            }

            // Configuration Comparison headers and cells
            const configTable = tables[1];
            const configHeaders = findElements(configTable, (el) => el.tagName === 'TH');
            assert.equal(configHeaders.length, 4);
            configHeaders.slice(1).forEach((th, idx) => {
                assert.ok(String(th.getAttribute('class') || '').includes('px-3 py-2.5'));
                const minW0 = findElements(th, (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('min-w-0'))[0];
                assert.ok(minW0, 'Expected min-w-0 container in Configuration Comparison run header');
                const truncated = findElements(minW0, (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('truncate'))[0];
                assert.ok(truncated, 'Expected truncated label div in Configuration Comparison run header');
                assert.equal(truncated.getAttribute('title'), kpiHeaderLabels[idx]);
            });
            const configValueCells = findElements(
                configTable,
                (el) => el.tagName === 'TD' && String(el.getAttribute('class') || '').includes('font-mono'),
            );
            assert.ok(configValueCells.length >= 24);
            for (const td of configValueCells) {
                const cls = String(td.getAttribute('class') || '');
                assert.ok(cls.includes('px-3 py-2.5') && cls.includes('break-words'), `Expected px-3 py-2.5 and break-words on config cell, got: ${cls}`);
                assert.equal(td.getAttribute('title'), td.textContent);
            }

            // Charts, legends, and CompareTooltip
            const chartGrid = findElements(
                doc.body,
                (el) => el.tagName === 'DIV' && String(el.getAttribute('class') || '').includes('lg:grid-cols-2'),
            )[0];
            assert.ok(chartGrid, 'Expected chart grid wrapper');
            assert.ok(String(chartGrid.getAttribute('class') || '').includes('min-w-0'));

            const chartContainers = collectFiberNodes(
                container,
                (fiber) => fiber.elementType?.name === 'ChartContainer',
            );
            assert.equal(chartContainers.length, 4);
            for (const cc of chartContainers) {
                assert.ok(String(cc.memoizedProps.className || '').includes('min-w-0'));
            }
        },
    );

    // Verify OverlaidChart renders ChartContainer min-w-0, overflow-safe ChartLegend, and CompareTooltip max-w-xs
    const chartRuns = [
        { taskId: 'sim-1', label: longLabel1 },
        { taskId: 'sim-2', label: longLabel2 },
        { taskId: 'sim-3', label: longLabel3 },
    ];
    const chartTree = OverlaidChart({
        title: 'Throughput Comparison',
        subtitle: 'Request throughput (req/s) across runs',
        data: [
            { t: 0, run_0: 14.2, run_1: 17.75, run_2: 12.78 },
            { t: 30, run_0: 15.0, run_1: 18.1, run_2: 13.2 },
        ],
        runs: chartRuns,
        unit: ' req/s',
    });
    assert.ok(String(chartTree.props?.className || '').includes('min-w-0'));

    const chartLegendEl = findElement(chartTree, (n) => n.type === ChartLegend);
    assert.ok(chartLegendEl, 'Expected ChartLegend inside OverlaidChart');
    const legendClass = String(chartLegendEl.props?.className || '');
    assert.ok(
        legendClass.includes('max-w-full')
        && legendClass.includes('gap-x-3')
        && legendClass.includes('gap-y-1.5')
        && legendClass.includes('[&>span]:min-w-0')
        && legendClass.includes('[&>span]:max-w-full')
        && legendClass.includes('[&>span]:wrap-anywhere'),
        `Expected overflow-safe ChartLegend class, got: ${legendClass}`,
    );

    const chartHtml = renderToStaticMarkup(
        <OverlaidChart
            title="Throughput Comparison"
            subtitle="Request throughput (req/s) across runs"
            data={[{ t: 0, run_0: 14.2, run_1: 17.75, run_2: 12.78 }]}
            runs={chartRuns}
            unit=" req/s"
        />,
    );
    assert.match(chartHtml, /max-w-full/);
    assert.match(chartHtml, /wrap-anywhere/);
    assert.match(chartHtml, new RegExp(longLabel1));
    assert.match(chartHtml, new RegExp(longLabel2));
    assert.match(chartHtml, new RegExp(longLabel3));

    const chartTooltipEl = findElement(chartTree, (n) => n.type === Tooltip);
    assert.ok(chartTooltipEl?.props?.content, 'Expected Recharts Tooltip in OverlaidChart');
    const tooltipHtml = renderToStaticMarkup(
        <CompareTooltip
            active
            label={30}
            unit=" req/s"
            payload={[
                { dataKey: 'run_0', name: longLabel1, color: CHART_SERIES[0], value: 14.2 },
                { dataKey: 'run_1', name: longLabel2, color: CHART_SERIES[1], value: 17.75 },
                { dataKey: 'run_2', name: longLabel3, color: CHART_SERIES[2], value: 12.78 },
            ]}
        />,
    );
    assert.match(tooltipHtml, /max-w-xs/);
    assert.match(tooltipHtml, /truncate/);
    assert.match(tooltipHtml, /Elapsed 30s/);
});

function makeMax3CompareDashboardFixtures() {
    const longTaskNames = {
        'sim-1': 'experiment-run-one-with-extremely-long-descriptive-benchmark-label-alpha',
        'sim-2': 'experiment-run-two-with-extremely-long-descriptive-benchmark-label-beta',
        'sim-3': 'experiment-run-three-with-extremely-long-descriptive-benchmark-label-gamma',
        'sim-4': 'experiment-run-four-with-extremely-long-descriptive-benchmark-label-delta',
    };
    const allTasks = Object.entries(longTaskNames).map(([id, name]) => makeRichTask({ id, name }));
    const fetchHandler = async (url) => {
        if (url.startsWith('/api/v1/simulation/tasks?')) {
            return Response.json({
                tasks: allTasks,
                total: allTasks.length,
                counts: { queued: 0, running: 0, completed: allTasks.length, failed: 0, cancelled: 0 },
                facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] },
            });
        }
        if (/\/api\/v1\/simulation\/tasks\/sim-[1-4]$/.test(url)) {
            const id = url.split('/').pop();
            return Response.json({ task: allTasks.find((item) => item.id === id) });
        }
        if (url.includes('/telemetry')) {
            const base = makeRichTelemetry();
            return Response.json({
                ...base,
                client_series: { ...base.client_series, buckets: [] },
                server_telemetry: { ...base.server_telemetry, series: [] },
            });
        }
        return Response.json({ items: [], backends: [], scenarios: [], datasets: [] });
    };
    return { longTaskNames, fetchHandler };
}

test('SimulationDashboard slices ?compare=a,b,c,d query parameter to max 3 compare runs', async (t) => {
    const { fetchHandler } = makeMax3CompareDashboardFixtures();

    await withMountedDom(
        t,
        fetchHandler,
        async ({ root, doc, flush }) => {
            globalThis.window.location = new URL('http://localhost/?compare=sim-1,sim-2,sim-3,sim-4');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };

            await act(async () => {
                root.render(<SimulationDashboard initialAction="new" />);
            });
            await flush();
            await flush();

            assert.equal(globalThis.window.location.search, '?compare=sim-1%2Csim-2%2Csim-3');
            assert.match(doc.body.textContent, /Compare Runs \(3\/3\)/);
            assert.match(doc.body.textContent, /Selected to compare \(3\/3\):/);
            assert.match(doc.body.textContent, /across 3 runs/);
        },
    );
});

test('SimulationDashboard enforces max 3 row compare checkboxes, truncated badges, and re-enables 4th row on uncheck', async (t) => {
    const { longTaskNames, fetchHandler } = makeMax3CompareDashboardFixtures();

    await withMountedDom(
        t,
        fetchHandler,
        async ({ root, doc, flush }) => {
            globalThis.window.location = new URL('http://localhost/');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };

            await act(async () => {
                root.render(<SimulationDashboard initialAction="new" />);
            });
            await flush();

            const findCheckbox = (id) => findElements(
                doc.body,
                (el) => el.tagName === 'INPUT'
                    && reactProps(el).type === 'checkbox'
                    && el.getAttribute('aria-label') === `Select ${longTaskNames[id]} for comparison`,
            )[0] || null;

            for (const id of ['sim-1', 'sim-2', 'sim-3']) {
                const cb = findCheckbox(id);
                assert.ok(cb, `Expected compare checkbox for ${id}`);
                await act(async () => {
                    reactProps(cb).onChange({ target: { checked: true } });
                });
            }
            await flush();

            assert.match(doc.body.textContent, /Compare Runs \(3\/3\)/);
            assert.match(doc.body.textContent, /Selected to compare \(3\/3\):/);

            const badges = findElements(
                doc.body,
                (el) => el.tagName === 'SPAN' && String(el.getAttribute('class') || '').includes('max-w-[220px]'),
            );
            assert.equal(badges.length, 3, 'Expected 3 truncated badges in selection banner');
            badges.forEach((badge, idx) => {
                assert.equal(badge.getAttribute('title'), longTaskNames[`sim-${idx + 1}`]);
                const innerLabel = findElements(
                    badge,
                    (el) => el.tagName === 'SPAN'
                        && String(el.getAttribute('class') || '').includes('min-w-0')
                        && String(el.getAttribute('class') || '').includes('truncate'),
                )[0];
                assert.ok(innerLabel, 'Expected inner min-w-0 truncate span inside selection banner Badge');
                assert.equal(innerLabel.textContent, longTaskNames[`sim-${idx + 1}`]);
                const leftFlex = badge.parentNode;
                assert.ok(String(leftFlex?.getAttribute('class') || '').includes('min-w-0'));
            });

            assert.equal(Boolean(reactProps(findCheckbox('sim-4')).disabled), true, 'Expected 4th row compare checkbox to be disabled at 3/3');

            // Uncheck sim-2 -> drops to 2/3 and re-enables sim-4
            await act(async () => {
                reactProps(findCheckbox('sim-2')).onChange({ target: { checked: false } });
            });
            await flush();

            assert.match(doc.body.textContent, /Compare Runs \(2\/3\)/);
            assert.match(doc.body.textContent, /Selected to compare \(2\/3\):/);
            assert.equal(Boolean(reactProps(findCheckbox('sim-4')).disabled), false, 'Expected 4th row compare checkbox to re-enable after unchecking one');
        },
    );
});

test('ServingTab & OverviewTab render SLO reference lines with Y-axis headroom, omit when absent, and format TPOT SLO strictly in ms (t82, t84)', async (t) => {
    const base = makeRichTelemetry();
    const latencySeries = [
        {
            elapsed_seconds: 5,
            ttft_p50_ms: 80,
            ttft_p90_ms: 150,
            tpot_p50_ms: 18,
            tpot_p90_ms: 32,
            e2e_p50_ms: 420,
            e2e_p90_ms: 680,
        },
        {
            elapsed_seconds: 10,
            ttft_p50_ms: 95,
            ttft_p90_ms: 140,
            tpot_p50_ms: 21,
            tpot_p90_ms: 35,
            e2e_p50_ms: 460,
            e2e_p90_ms: 710,
        },
    ];

    const verdictSloTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            verdict: {
                ...base.forensics.verdict,
                slo: { ttft_s: 1.0, tpot_ms: 50, e2e_s: 2.0 },
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            summary: {},
            flow_components: [],
            per_pod: [],
            per_endpoint: [],
            router: {},
            series: latencySeries,
        },
    };

    // 1. Inspect ServingTab React tree with verdict.slo where SLO (1000 ms) > observed P90 (150 ms)
    let capturedServingTree = null;
    function CaptureServingSlo() {
        capturedServingTree = ServingTab({ task: null, telemetry: verdictSloTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureServingSlo />);

    const serverLatCard = findElement(
        capturedServingTree,
        (n) => n.type === ChartContainer && n.props?.title === 'Server Latency over Time',
    );
    assert.ok(serverLatCard, 'Expected Server Latency over Time ChartContainer');
    const serverLatChart = findElement(serverLatCard, (n) => n.type === ComposedChart);
    const serverYAxis = findElement(serverLatChart, (n) => n.type === YAxis);
    const serverSloLine = findElement(serverLatChart, (n) => n.type === ReferenceLine);
    const serverLegend = findElement(serverLatCard, (n) => n.type === ChartLegend);

    assert.deepEqual(serverYAxis.props.domain, [0, Math.ceil(1000 * 1.12)], 'Expected YAxis domain headroom above 1000 ms TTFT SLO');
    assert.ok(serverSloLine, 'Expected SLO ReferenceLine on Server Latency over Time');
    assert.equal(serverSloLine.props.y, 1000);
    assert.equal(serverSloLine.props.ifOverflow, 'extendDomain');
    assert.match(serverSloLine.props.label.value, /^SLO: 1,?000 ms$/);
    assert.ok(
        serverLegend.props.entries.some((entry) => /^TTFT SLO \(1,?000 ms\)$/.test(entry.label)),
        'Expected TTFT SLO legend entry',
    );

    // 2. Fallback to task.simulation.backend_options when verdict.slo is absent
    const backendOptsTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            verdict: { ...base.forensics.verdict, slo: undefined },
        },
        run_manifest: { ...(base.run_manifest || {}), slo: undefined },
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            per_pod: [],
            series: latencySeries,
        },
    };
    const backendOptsTask = {
        id: 'sim-slo-backend-opts',
        simulation: {
            backend_options: { ttft_slo: 0.8, tpot_slo: 0.045, e2e_slo: 2.5 },
        },
    };
    let capturedFallbackTree = null;
    function CaptureServingBackendOpts() {
        capturedFallbackTree = ServingTab({ task: backendOptsTask, telemetry: backendOptsTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureServingBackendOpts />);
    const fallbackCard = findElement(
        capturedFallbackTree,
        (n) => n.type === ChartContainer && n.props?.title === 'Server Latency over Time',
    );
    const fallbackSloLine = findElement(fallbackCard, (n) => n.type === ReferenceLine);
    assert.ok(fallbackSloLine);
    assert.equal(fallbackSloLine.props.y, 800);

    // 3. No SLO configured (and non-positive 0 SLO) -> omit ReferenceLine & SLO legend entry
    const noSloTelemetry = {
        ...backendOptsTelemetry,
        forensics: {
            ...base.forensics,
            verdict: { ...base.forensics.verdict, slo: { ttft_s: 0, tpot_ms: 0, e2e_s: -1 } },
        },
    };
    let capturedNoSloTree = null;
    function CaptureServingNoSlo() {
        capturedNoSloTree = ServingTab({ task: null, telemetry: noSloTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureServingNoSlo />);
    const noSloCard = findElement(
        capturedNoSloTree,
        (n) => n.type === ChartContainer && n.props?.title === 'Server Latency over Time',
    );
    assert.equal(findElement(noSloCard, (n) => n.type === ReferenceLine), undefined);
    assert.equal(findElement(noSloCard, (n) => n.type === YAxis).props.domain, undefined);
    const noSloLegend = findElement(noSloCard, (n) => n.type === ChartLegend);
    assert.equal(noSloLegend.props.entries.some((e) => e.label.includes('SLO')), false);

    // 4. Verify resolveServingLatencySlo across TTFT, TPOT, and E2E (always formatted in ms, never ms/tok)
    const ttftResolved = resolveServingLatencySlo(backendOptsTask, verdictSloTelemetry, 'ttft');
    const tpotResolved = resolveServingLatencySlo(backendOptsTask, verdictSloTelemetry, 'tpot');
    const e2eResolved = resolveServingLatencySlo(backendOptsTask, verdictSloTelemetry, 'e2e');
    assert.equal(ttftResolved.activeServerSloMs, 1000);
    assert.match(ttftResolved.sloDisplayText, /^1,?000 ms$/);
    assert.equal(tpotResolved.activeServerSloMs, 50);
    assert.equal(tpotResolved.sloDisplayText, '50 ms');
    assert.doesNotMatch(tpotResolved.sloDisplayText, /ms\/tok/i);
    assert.equal(e2eResolved.activeServerSloMs, 2000);
    assert.match(e2eResolved.sloDisplayText, /s$|ms$/);

    // Individual backend_options key fallback across task.simulation.backend_options, task.backend_options, run_manifest.replay.backend_options
    const partialSplitTask = {
        simulation: { backend_options: { ttft_slo: 0.6 } },
        backend_options: { tpot_slo: 0.04 },
    };
    const partialSplitTel = {
        run_manifest: { replay: { backend_options: { e2e_slo: 1.5 } } },
    };
    assert.equal(resolveServingLatencySlo(partialSplitTask, partialSplitTel, 'ttft').activeServerSloMs, 600);
    assert.equal(resolveServingLatencySlo(partialSplitTask, partialSplitTel, 'tpot').activeServerSloMs, 40);
    assert.equal(resolveServingLatencySlo(partialSplitTask, partialSplitTel, 'e2e').activeServerSloMs, 1500);

    // 5. Mounted DOM: SimulationTaskDetails Logs & Configuration displays TPOT SLO in ms (never ms/tok)
    const detailsTask = makeRichTask({
        id: 'sim-tpot-slo-details',
        simulation: { backend_options: { ttft_slo: 0.5, tpot_slo: 0.05, e2e_slo: 2.0 } },
    });
    const emptySeriesTelemetry = {
        ...base,
        run_manifest: null,
        client_series: {
            ...base.client_series,
            buckets: [],
            cold_start_comparison: null,
            windows: {
                profiling: { duration_seconds: 20 },
                full: { duration_seconds: 20 },
            },
        },
        server_telemetry: { ...base.server_telemetry, flow_components: [], per_pod: [], per_endpoint: [], router: {}, series: [] },
    };
    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/telemetry')) return Response.json(emptySeriesTelemetry);
            return Response.json({ task: detailsTask });
        },
        async ({ root, doc, clickButton, flush }) => {
            globalThis.window.location = new URL(`http://localhost/?taskId=${detailsTask.id}`);
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={() => {}} />);
            });
            await flush();
            await flush();
            await clickButton(/Logs & Configuration/);
            await flush();
            assert.match(doc.body.textContent, /SLO Targets/);
            assert.match(doc.body.textContent, /TPOT SLO.*50 ms/);
            assert.doesNotMatch(doc.body.textContent, /50 ms\/tok/i);
        },
    );

    // Verify OverviewTab YAxis headroom, ReferenceLine label, and ms-only SLO legend (never ms/tok)
    let capturedOverviewTree = null;
    function CaptureOverviewSlo() {
        capturedOverviewTree = OverviewTab({
            task: { simulation: { backend_options: { ttft_slo: 0.5, tpot_slo: 0.25 } } },
            telemetry: {
                ...base,
                forensics: {
                    ...base.forensics,
                    verdict: { ...base.forensics.verdict, slo: { ttft_s: 0.5, tpot_ms: 250 } },
                },
                client_series: {
                    ...base.client_series,
                    buckets: [
                        {
                            start_seconds: 0,
                            end_seconds: 5,
                            ttft_p50_ms: 80,
                            ttft_p90_ms: 140,
                            tpot_p50_ms: 12,
                            tpot_p90_ms: 25,
                        },
                    ],
                },
            },
            analysisWindow: 'profiling',
        });
        return null;
    }
    renderToStaticMarkup(<CaptureOverviewSlo />);
    const overviewLatCard = findElement(
        capturedOverviewTree,
        (n) => n.type === ChartContainer && n.props?.title === 'Latency over Time',
    );
    const overviewYAxis = findElement(overviewLatCard, (n) => n.type === YAxis);
    assert.deepEqual(overviewYAxis.props.domain, [0, Math.ceil(500 * 1.12)]);
    const overviewSloLine = findElement(overviewLatCard, (n) => n.type === ReferenceLine && n.props?.y === 500);
    assert.ok(overviewSloLine, 'Expected OverviewTab SLO ReferenceLine');
    assert.equal(overviewSloLine.props.ifOverflow, 'extendDomain');
    assert.match(overviewSloLine.props.label.value, /^SLO: 500 ms$/);
    const overviewSloLegend = findElement(overviewLatCard, (n) => n.type === ChartLegend);
    const overviewSloEntry = overviewSloLegend.props.entries.find((e) => e.label.startsWith('SLO ('));
    assert.ok(overviewSloEntry, 'Expected OverviewTab SLO legend entry');
    assert.match(overviewSloEntry.label, /^SLO \(500 ms\)$/);
    assert.doesNotMatch(overviewSloEntry.label, /ms\/tok/i);
});

test('ServingTab #s-cache renders HBM KV Cache Usage over Time and KV Cache Tier Share (HBM · Host Memory · NVMe) for HBM-only and multi-tier runs (t83)', () => {
    const base = makeRichTelemetry();

    // 1. HBM-only run (including t=0 idle sample with kv_pct = 0)
    const hbmOnlyTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            per_pod: [],
            cache_config: {
                hbm_capacity_tokens: 16384,
                block_size: 16,
                num_gpu_blocks: 1024,
            },
            series: [
                { elapsed_seconds: 0, kv_cache_usage_percent: 0 },
                { elapsed_seconds: 5, kv_cache_usage_percent: 42.5 },
                { elapsed_seconds: 10, kv_cache_usage_percent: 68.0 },
            ],
        },
    };

    const hbmOnlyHtml = renderToStaticMarkup(<ServingTab telemetry={hbmOnlyTelemetry} />);
    assertNoNestedButtons(hbmOnlyHtml, 'ServingTab #s-cache HBM-only');
    assert.match(hbmOnlyHtml, /HBM KV Cache Usage over Time/);
    assert.match(hbmOnlyHtml, /Accelerator HBM KV cache usage \(%\) with 90% pressure threshold/);
    assert.match(hbmOnlyHtml, /KV Cache Tier Share \(HBM · Host Memory · NVMe\)/);
    assert.match(hbmOnlyHtml, /100%/);
    assert.match(hbmOnlyHtml, /0%/);
    assert.match(hbmOnlyHtml, /Not configured/);
    const sCacheSlice = hbmOnlyHtml.slice(hbmOnlyHtml.indexOf('id="s-cache"'), hbmOnlyHtml.indexOf('id="s-kvreuse"'));
    for (const tip of extractTooltipElements(sCacheSlice)) {
        assert.doesNotMatch(tip.text, /\bvllm\b|vllm:/i);
    }
    const cacheChartsSlice = sCacheSlice.slice(sCacheSlice.indexOf('HBM KV Cache Usage over Time'));
    assert.doesNotMatch(cacheChartsSlice, /\bvllm\b|vllm:/i);

    let capturedHbmTree = null;
    function CaptureHbmOnly() {
        capturedHbmTree = ServingTab({ telemetry: hbmOnlyTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureHbmOnly />);
    const hbmTierCard = findElement(
        capturedHbmTree,
        (n) => n.type === ChartContainer && n.props?.title === 'KV Cache Tier Share (HBM · Host Memory · NVMe)',
    );
    assert.ok(hbmTierCard, 'Expected KV Cache Tier Share ChartContainer');
    const hbmAreaChart = findElement(hbmTierCard, (n) => n.type === AreaChart);
    assert.ok(hbmAreaChart, 'Expected AreaChart in Share (%) mode');
    assert.equal(hbmAreaChart.props.data[0].hbm_share_pct, 100, 'Idle t=0 on HBM-only run should default to 100% HBM share');
    assert.equal(hbmAreaChart.props.data[0].host_share_pct, 0);
    assert.equal(hbmAreaChart.props.data[0].nvme_share_pct, 0);

    // 2. Multi-tier run (HBM + Host Memory + NVMe) with byte capacities
    const multiTierTelemetry = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            flow_components: [],
            per_pod: [],
            per_endpoint: [],
            router: {},
            summary: {
                kv_cache_usage_percent: { mean: 60, max: 80 },
                cpu_cache_usage_percent: { mean: 30, max: 40 },
                disk_cache_usage_percent: { mean: 15, max: 20 },
            },
            cache_config: {
                hbm_capacity_bytes: 10 * (1024 ** 3),
                cpu_capacity_bytes: 40 * (1024 ** 3),
                nvme_capacity_bytes: 100 * (1024 ** 3),
                hbm_capacity_tokens: 32768,
                cpu_capacity_tokens: 131072,
                host_kv_configured: true,
                nvme_kv_configured: true,
            },
            series: [
                {
                    elapsed_seconds: 0,
                    kv_cache_usage_percent: 0,
                    cpu_cache_usage_percent: 0,
                    disk_cache_usage_percent: 0,
                },
                {
                    elapsed_seconds: 5,
                    // Weighted active bytes: HBM=0.8*10=8, Host=0.3*40=12, NVMe=0.2*100=20 => total=40 => 20% / 30% / 50%
                    kv_cache_usage_percent: 80,
                    cpu_cache_usage_percent: 30,
                    disk_cache_usage_percent: 20,
                },
            ],
        },
    };

    let capturedMultiTree = null;
    function CaptureMultiTier() {
        capturedMultiTree = ServingTab({ telemetry: multiTierTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureMultiTier />);
    const multiTierCard = findElement(
        capturedMultiTree,
        (n) => n.type === ChartContainer && n.props?.title === 'KV Cache Tier Share (HBM · Host Memory · NVMe)',
    );
    const multiAreaChart = findElement(multiTierCard, (n) => n.type === AreaChart);
    const pt1 = multiAreaChart.props.data[1];
    assert.ok(Math.abs(pt1.hbm_share_pct - 20) < 1e-6, `Expected 20% HBM share, got ${pt1.hbm_share_pct}`);
    assert.ok(Math.abs(pt1.host_share_pct - 30) < 1e-6, `Expected 30% Host Memory share, got ${pt1.host_share_pct}`);
    assert.ok(Math.abs(pt1.nvme_share_pct - 50) < 1e-6, `Expected 50% NVMe share, got ${pt1.nvme_share_pct}`);

    // Idle t=0 with known provisioned capacities: 10 / 150, 40 / 150, 100 / 150
    const pt0 = multiAreaChart.props.data[0];
    assert.ok(Math.abs(pt0.hbm_share_pct - (10 / 150) * 100) < 1e-6);
    assert.ok(Math.abs(pt0.host_share_pct - (40 / 150) * 100) < 1e-6);
    assert.ok(Math.abs(pt0.nvme_share_pct - (100 / 150) * 100) < 1e-6);

    // Custom Tooltip displays both % share and % usage
    const tierTooltipEl = findElement(multiAreaChart, (n) => n.type === Tooltip);
    assert.ok(tierTooltipEl?.props?.content);
    const tierTooltipHtml = renderToStaticMarkup(
        React.cloneElement(tierTooltipEl.props.content, {
            active: true,
            label: 5,
            payload: [{ dataKey: 'hbm_share_pct', payload: pt1 }],
        }),
    );
    assert.match(tierTooltipHtml, /HBM/);
    assert.match(tierTooltipHtml, /20% share · 80% usage/);
    assert.match(tierTooltipHtml, /Host Memory/);
    assert.match(tierTooltipHtml, /30% share · 30% usage/);
    assert.match(tierTooltipHtml, /NVMe/);
    assert.match(tierTooltipHtml, /50% share · 20% usage/);

    // Render HTML for multi-tier summary tiles + Share (%) legend + Tier Usage (%) ToggleGroup option
    const multiTierHtml = renderToStaticMarkup(<ServingTab telemetry={multiTierTelemetry} />);
    assertNoNestedButtons(multiTierHtml, 'ServingTab #s-cache multi-tier');
    assert.match(multiTierHtml, /Active KV cache distribution across HBM, Host Memory, and NVMe \(%\)/);
    assert.match(multiTierHtml, /Mean Usage: 60%/);
    assert.match(multiTierHtml, /Mean Usage: 30%/);
    assert.match(multiTierHtml, /Mean Usage: 15%/);
    assert.match(multiTierHtml, /aria-pressed="true"[^>]*>Share \(%\)</);
    assert.match(multiTierHtml, /aria-pressed="false"[^>]*>Tier Usage \(%\)</);
    assert.match(multiTierHtml, /Host Memory \(Offload\)/);
    assert.match(multiTierHtml, /NVMe \(Offload\)/);
});

test('LatencyHeatmap renders semantic Color legend for TTFT/TPOT and handles uniform single-value ranges', () => {
    const tpotHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TPOT by Time and Output Sequence Length"
            metric="TPOT"
            data={{
                sequence_length: 'output',
                minimum_metric_ms: 12,
                maximum_metric_ms: 48,
                cells: [
                    { start_seconds: 0, end_seconds: 10, sequence_length_start: 32, sequence_length_end: 128, average_metric_ms: 12, request_count: 4 },
                    { start_seconds: 10, end_seconds: 20, sequence_length_start: 128, sequence_length_end: 512, average_metric_ms: 48, request_count: 6 },
                ],
            }}
        />,
    );
    assert.match(tpotHtml, /Color: Avg TPOT \(ms\/tok\)/);
    assert.match(tpotHtml, /Low \(12 ms\/tok\)/);
    assert.match(tpotHtml, /High \(48 ms\/tok\)/);

    const uniformTtftHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TTFT by Time and Input Sequence Length"
            metric="TTFT"
            data={{
                sequence_length: 'input',
                minimum_metric_ms: 120,
                maximum_metric_ms: 120,
                cells: [
                    { start_seconds: 0, end_seconds: 10, sequence_length_start: 64, sequence_length_end: 256, average_metric_ms: 120, request_count: 5 },
                ],
            }}
        />,
    );
    assert.match(uniformTtftHtml, /Color: Avg TTFT \(ms\)/);
    assert.match(uniformTtftHtml, /Uniform \(120 ms\)/);
    assert.doesNotMatch(uniformTtftHtml, /Low \(120 ms\)/);
    assert.doesNotMatch(uniformTtftHtml, /High \(120 ms\)/);
});

test('RequestsTab Turns per Session formats singular/plural turn ticks and sets X/Y axis labels', () => {
    const telemetry = makeRichTelemetry({
        sessions_summary: {
            available: true,
            correlation: { matched_count: 64, total_count: 64, matched_ratio: 1 },
            turn_distribution: [
                { turns: 1, sessions: 40 },
                { turns: 2, sessions: 18 },
                { turns: 5, sessions: 6 },
            ],
            tree_depth_distribution: [{ depth: 1, sessions: 40 }],
        },
    });

    let capturedTree = null;
    function CaptureRequests() {
        capturedTree = RequestsTab({
            task: makeRichTask(),
            telemetry,
            analysisWindow: 'profiling',
            onOpenErrorInspector: () => {},
        });
        return null;
    }
    renderToStaticMarkup(<CaptureRequests />);

    const turnsCard = findElement(
        capturedTree,
        (n) => n.type === ChartContainer && n.props?.title === 'Turns per Session',
    );
    assert.ok(turnsCard, 'Expected Turns per Session ChartContainer');
    const barChart = findElement(turnsCard, (n) => n.type === BarChart);
    assert.ok(barChart, 'Expected Turns per Session BarChart');
    assert.deepEqual(
        barChart.props.data.map((d) => d.label),
        ['1 turn', '2 turns', '5 turns'],
    );

    const xAxis = findElement(barChart, (n) => n.type === XAxis);
    const yAxis = findElement(barChart, (n) => n.type === YAxis);
    assert.equal(xAxis.props.label?.value, 'Turns per session');
    assert.equal(yAxis.props.label?.value, 'Sessions');
    assert.equal(xAxis.props.tickFormatter(1), '1 turn');
    assert.equal(xAxis.props.tickFormatter(2), '2 turns');

    const tooltipEl = findElement(barChart, (n) => n.type === Tooltip);
    const tooltipHtml = renderToStaticMarkup(
        React.cloneElement(tooltipEl.props.content, {
            active: true,
            label: '1 turn',
            payload: [{ dataKey: 'count', name: 'Sessions', value: 40, color: CHART_SERIES[0] }],
        }),
    );
    assert.match(tooltipHtml, /1 turn/);
    assert.match(tooltipHtml, /40 sessions/);
});

test('BottlenecksTab renders Mean TTFT bar, granular residual pills/tooltip, caveat banner, no 0 ms phantom pills, KV Transfer (NIXL), and reconciliation note', () => {
    const base = makeRichTelemetry();
    const pdTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_kv_transfer_series: true,
                has_p90_series: true,
                dominant_e2e_stage: 'decode_time',
                dominant_ttft_stage: 'prefill_time',
                p50: {
                    available: true,
                    epp_scheduling_ms: 4.0,
                    queue_time_ms: 25.0,
                    kv_transfer_ms: 18.0,
                    prefill_time_ms: 110.0,
                    decode_time_ms: 500.0,
                    engine_overhead_ms: 12.0,
                    gateway_overhead_ms: null,
                    ttft_stage_ms: 169.0,
                    total_stage_ms: 669.0,
                    reconciliation: {
                        total_stage_ms: 669.0,
                        client_e2e_ms: 850.0,
                        e2e_gap_ms: -181.0,
                        e2e_gap_pct: -21.3,
                        quantile_non_additive: true,
                    },
                    share_of_e2e_pct: { decode_time: 74.7, prefill_time: 16.4, queue_time: 3.7, kv_transfer: 2.7, engine_overhead: 1.8, epp_scheduling: 0.6 },
                    share_of_ttft_pct: { prefill_time: 65.1, queue_time: 14.8, kv_transfer: 10.7, engine_overhead: 7.1, epp_scheduling: 2.4 },
                },
                p90: {
                    available: true,
                    epp_scheduling_ms: 8.0,
                    queue_time_ms: 45.0,
                    kv_transfer_ms: 32.0,
                    prefill_time_ms: 190.0,
                    decode_time_ms: 750.0,
                    engine_overhead_ms: 35.0,
                    gateway_overhead_ms: null,
                    ttft_stage_ms: 310.0,
                    total_stage_ms: 1060.0,
                    share_of_e2e_pct: { decode_time: 70.8 },
                    share_of_ttft_pct: { prefill_time: 61.3 },
                },
                mean: {
                    available: true,
                    epp_scheduling_ms: 5.0,
                    queue_time_ms: 30.0,
                    kv_transfer_ms: 20.0,
                    prefill_time_ms: 120.0,
                    decode_time_ms: 510.0,
                    engine_overhead_ms: 14.0,
                    gateway_overhead_ms: 11.0,
                    ttft_stage_ms: 200.0,
                    total_stage_ms: 710.0,
                    share_of_e2e_pct: { decode_time: 71.8 },
                    share_of_ttft_pct: { prefill_time: 60.0 },
                },
                series: [
                    { t: 5, epp_sched_ms: 4.0, queue_ms: 25.0, kv_transfer_ms: 18.0, prefill_ms: 110.0, decode_ms: 500.0 },
                ],
            },
            ttft_decomposition: {
                available: true,
                source: 'server_telemetry',
                caveat: 'Server TTFT quantile hit the 10 s histogram ceiling; unmeasured engine tail is attributed to Server TTFT Residual.',
                p50: { prefill_ms: 110.0, queue_ms: 25.0, kv_transfer_ms: 18.0, epp_scheduling_ms: 4.0, engine_overhead_ms: 12.0, gateway_overhead_ms: null },
                p90: { prefill_ms: 190.0, queue_ms: 45.0, kv_transfer_ms: 32.0, epp_scheduling_ms: 8.0, engine_overhead_ms: 35.0, gateway_overhead_ms: null },
                mean: { prefill_ms: 120.0, queue_ms: 30.0, kv_transfer_ms: 20.0, epp_scheduling_ms: 5.0, engine_overhead_ms: 14.0, gateway_overhead_ms: 11.0 },
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
        },
    };

    const html = renderToStaticMarkup(<BottlenecksTab telemetry={pdTelemetry} onSelectTab={() => {}} />);
    assert.match(html, /KV Transfer \(NIXL\)/);
    assert.match(html, /E2E reconciliation: stage sum \(669 ms\) vs Client E2E \(850 ms\)/);
    assert.match(html, /Server TTFT quantile hit the 10 s histogram ceiling/);
    assert.match(html, /Engine \/ API Overhead/);
    // Nullable gateway_overhead_ms on P50 must NOT render a phantom 0 ms pill in TTFT Stage Decomposition
    const ttftSection = html.slice(html.indexOf('TTFT Stage Decomposition'));
    const ttftSectionBody = ttftSection.slice(0, ttftSection.indexOf('Multi-Turn Prefix Cache Reuse') > 0 ? ttftSection.indexOf('Multi-Turn Prefix Cache Reuse') : undefined);
    const ttftPillsHtml = ttftSectionBody.slice(ttftSectionBody.indexOf('mt-3 grid'));
    assert.doesNotMatch(ttftPillsHtml, /Client \/ Gateway \/ Network/);
    assert.doesNotMatch(ttftPillsHtml, />\s*0 ms\s*</);

    let capturedTree = null;
    function CaptureBottlenecks() {
        capturedTree = BottlenecksTab({ telemetry: pdTelemetry, onSelectTab: () => {} });
        return null;
    }
    renderToStaticMarkup(<CaptureBottlenecks />);
    const ttftBarChart = findElement(capturedTree, (n) => n.type === BarChart && Array.isArray(n.props?.data) && n.props.data.some((r) => r.stat === 'Mean'));
    assert.ok(ttftBarChart, 'Expected TTFT Stage Decomposition BarChart with Mean slice');
    assert.deepEqual(ttftBarChart.props.data.map((r) => r.stat), ['P50', 'Mean', 'P90']);

    const ttftTooltip = findElement(ttftBarChart, (n) => n.type === Tooltip);
    const meanRow = ttftBarChart.props.data.find((r) => r.stat === 'Mean');
    // Legacy residual_ms caller still renders residual sub-rows
    const tooltipHtml = renderToStaticMarkup(
        React.cloneElement(ttftTooltip.props.content, {
            active: true,
            label: 'Mean',
            payload: [
                { dataKey: 'prefill_ms', name: 'Prefill Time', value: meanRow.prefill_ms, color: CHART_SERIES[0], payload: meanRow },
                { dataKey: 'residual_ms', name: 'Server & Gateway Residual', value: meanRow.residual_ms, color: CHART_SERIES[3], payload: meanRow },
            ],
        }),
    );
    assert.match(tooltipHtml, /Server TTFT Residual/);
    assert.match(tooltipHtml, /Client \/ Gateway \/ Network/);
    assert.match(tooltipHtml, /EPP Scheduling/);

    // Granular non-legacy payload MUST NOT duplicate residual_ms sub-rows and MUST filter 0 ms rows
    const granularTooltipHtml = renderToStaticMarkup(
        React.cloneElement(ttftTooltip.props.content, {
            active: true,
            label: 'Mean',
            payload: [
                { dataKey: 'prefill_ms', name: 'Prefill Time', value: meanRow.prefill_ms, color: CHART_SERIES[0], fillOpacity: 1, payload: meanRow },
                { dataKey: 'queue_ms', name: 'Queue Time', value: 0, color: CHART_SERIES[2], fillOpacity: 1, payload: meanRow },
                { dataKey: 'gateway_overhead_ms', name: 'Client / Gateway / Network', value: meanRow.gateway_overhead_ms, color: CHART_SERIES[3], fillOpacity: 0.45, payload: meanRow },
            ],
        }),
    );
    assert.match(granularTooltipHtml, /Prefill Time/);
    assert.match(granularTooltipHtml, /Client \/ Gateway \/ Network/);
    assert.doesNotMatch(granularTooltipHtml, /Queue Time/);
    assert.doesNotMatch(granularTooltipHtml, /Server TTFT Residual/);
    assert.match(granularTooltipHtml, /opacity:0\.45/);
});

test('BottlenecksTab renders granular P/D TTFT decomposition bars, ChartLegend opacity swatches, zero-suppressed pills, and enriched Stage Share tooltip', () => {
    const base = makeRichTelemetry();
    const pdGranularTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_kv_transfer_series: true,
                p50: {
                    available: true,
                    epp_scheduling_ms: 0.25,
                    queue_time_ms: 0,
                    kv_transfer_ms: 393.896,
                    prefill_time_ms: 366.359,
                    decode_first_step_ms: 38.491,
                    prefill_engine_overhead_ms: 131.055,
                    decode_handoff_overhead_ms: 133.883,
                    engine_overhead_ms: 303.429,
                    gateway_overhead_ms: 0,
                    decode_time_ms: 5552.356,
                    ttft_stage_ms: 1063.959,
                    total_stage_ms: 6616.315,
                    share_of_e2e_pct: { decode_time: 83.9 },
                    share_of_ttft_pct: { prefill_time: 34.4 },
                },
                p90: {
                    available: true,
                    epp_scheduling_ms: 0.446,
                    queue_time_ms: 0,
                    kv_transfer_ms: 1777.468,
                    prefill_time_ms: 2143.608,
                    decode_first_step_ms: 270.0,
                    prefill_engine_overhead_ms: 644.258,
                    decode_handoff_overhead_ms: 750.528,
                    engine_overhead_ms: 1664.786,
                    gateway_overhead_ms: 115.502,
                    decode_time_ms: 11349.861,
                    ttft_stage_ms: 5701.829,
                    total_stage_ms: 17051.69,
                    share_of_e2e_pct: { decode_time: 66.6 },
                    share_of_ttft_pct: { prefill_time: 37.6 },
                },
            },
            ttft_decomposition: {
                available: true,
                source: 'server_telemetry',
                p50: {
                    prefill_ms: 366.359,
                    decode_first_step_ms: 38.491,
                    queue_ms: 0,
                    kv_transfer_ms: 393.896,
                    prefill_engine_overhead_ms: 131.055,
                    decode_handoff_overhead_ms: 133.883,
                    epp_scheduling_ms: 0.25,
                    engine_overhead_ms: 303.429,
                    gateway_overhead_ms: 0,
                },
                p90: {
                    prefill_ms: 2143.608,
                    decode_first_step_ms: 270.0,
                    queue_ms: 0,
                    kv_transfer_ms: 1777.468,
                    prefill_engine_overhead_ms: 644.258,
                    decode_handoff_overhead_ms: 750.528,
                    epp_scheduling_ms: 0.446,
                    engine_overhead_ms: 1664.786,
                    gateway_overhead_ms: 115.502,
                },
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            metric_sources: { vllm_stage_latencies: 'role_separated' },
        },
    };

    const html = renderToStaticMarkup(<BottlenecksTab telemetry={pdGranularTelemetry} onSelectTab={() => {}} />);
    // Enriched Stage Share Summary tooltip for Server TTFT Residual (engine-neutral)
    assert.match(html, /Decode First-Step Compute \+ Prefill Engine \/ API Overhead \+ Decode Handoff \/ SSE Overhead/);
    assert.match(html, /Decode First-Step Compute/);
    assert.match(html, /Prefill Engine \/ API Overhead/);
    assert.match(html, /Decode Handoff \/ SSE Overhead/);
    assert.match(html, /opacity:0\.45/);

    const ttftSection = html.slice(html.indexOf('TTFT Stage Decomposition'));
    const ttftSectionBody = ttftSection.slice(0, ttftSection.indexOf('Multi-Turn Prefix Cache Reuse') > 0 ? ttftSection.indexOf('Multi-Turn Prefix Cache Reuse') : undefined);
    // queue_ms is 0 across both P50 and P90 -> zero-suppressed from chart legend and subtitle
    assert.doesNotMatch(ttftSectionBody, /Queue Time/);
    // gateway_overhead_ms is 0 on P50 (primaryRow) -> suppressed from summary pills (even though P90 has 115.5ms in legend)
    const ttftPillsHtml = ttftSectionBody.slice(ttftSectionBody.indexOf('mt-3 grid'));
    assert.doesNotMatch(ttftPillsHtml, /Client \/ Gateway \/ Network/);
    assert.doesNotMatch(ttftPillsHtml, /Server TTFT Residual/);

    let capturedTree = null;
    function CaptureGranular() {
        capturedTree = BottlenecksTab({ telemetry: pdGranularTelemetry, onSelectTab: () => {} });
        return null;
    }
    renderToStaticMarkup(<CaptureGranular />);
    const ttftBarChart = findElement(capturedTree, (n) => n.type === BarChart && Array.isArray(n.props?.data) && n.props.data.some((r) => r.has_pd_sub_split));
    assert.ok(ttftBarChart, 'Expected granular P/D BarChart');
    const bars = [];
    const walkBars = (node) => {
        if (!node) return;
        if (Array.isArray(node)) { node.forEach(walkBars); return; }
        if (typeof node !== 'object') return;
        if (node.type === Bar) bars.push(node);
        walkBars(node.props?.children);
    };
    walkBars(ttftBarChart);
    assert.deepEqual(
        bars.map((b) => [b.props.dataKey, b.props.fillOpacity]),
        [
            ['prefill_ms', 1],
            ['decode_first_step_ms', 0.45],
            ['kv_transfer_ms', 1],
            ['prefill_engine_overhead_ms', 1],
            ['decode_handoff_overhead_ms', 0.45],
            ['epp_scheduling_ms', 1],
            ['gateway_overhead_ms', 0.45],
        ],
    );
});

test('BottlenecksTab renders 6-slice TTFT Stage Decomposition (Min/P50/Mean/P90/P99/Max), anchors pills to P50, shows slice_note tooltip, and guards EmptyState (t113)', () => {
    const base = makeRichTelemetry();
    const sixSliceTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_kv_transfer_series: true,
                min: { available: true, source: 'client_fallback', ttft_unattributed_ms: 88.25, ttft_stage_ms: 88.25, client_ttft_ms: 88.25, decode_time_ms: 210.0, slice_note: 'Server stage histograms do not expose single-request minimums; showing exact client Min TTFT.' },
                p50: { available: true, epp_scheduling_ms: 0.25, queue_time_ms: 10.0, kv_transfer_ms: 350.0, prefill_time_ms: 300.0, decode_first_step_ms: 38.0, prefill_engine_overhead_ms: 120.0, decode_handoff_overhead_ms: 130.0, engine_overhead_ms: 288.0, gateway_overhead_ms: 0, decode_time_ms: 4500.0 },
                mean: { available: true, epp_scheduling_ms: 0.3, queue_time_ms: 15.0, kv_transfer_ms: 420.0, prefill_time_ms: 400.0, decode_first_step_ms: 42.0, prefill_engine_overhead_ms: 145.0, decode_handoff_overhead_ms: 155.0, engine_overhead_ms: 342.0, gateway_overhead_ms: 0, decode_time_ms: 4800.0 },
                p90: { available: true, epp_scheduling_ms: 0.45, queue_time_ms: 25.0, kv_transfer_ms: 900.0, prefill_time_ms: 1100.0, decode_first_step_ms: 180.0, prefill_engine_overhead_ms: 320.0, decode_handoff_overhead_ms: 410.0, engine_overhead_ms: 910.0, gateway_overhead_ms: 40.0, decode_time_ms: 8000.0 },
                p99: { available: true, epp_scheduling_ms: 0.7, queue_time_ms: 40.0, kv_transfer_ms: 1400.0, prefill_time_ms: 1750.0, decode_first_step_ms: 240.0, prefill_engine_overhead_ms: 450.0, decode_handoff_overhead_ms: 560.0, engine_overhead_ms: 1250.0, gateway_overhead_ms: 85.0, decode_time_ms: 9800.0 },
                max: { available: true, epp_scheduling_ms: 0.9, queue_time_ms: 50.0, kv_transfer_ms: 1650.0, prefill_time_ms: 2050.0, engine_overhead_ms: 2200.0, gateway_overhead_ms: null, decode_time_ms: 11200.0, slice_note: 'Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.' },
            },
            ttft_decomposition: {
                available: true,
                source: 'server_telemetry',
                min: { available: true, source: 'client_fallback', ttft_unattributed_ms: 88.25, client_ttft_ms: 88.25, slice_note: 'Server stage histograms do not expose single-request minimums; showing exact client Min TTFT.' },
                p50: { available: true, prefill_ms: 300.0, decode_first_step_ms: 38.0, queue_ms: 10.0, kv_transfer_ms: 350.0, prefill_engine_overhead_ms: 120.0, decode_handoff_overhead_ms: 130.0, epp_scheduling_ms: 0.25, engine_overhead_ms: 288.0, gateway_overhead_ms: 0 },
                mean: { available: true, prefill_ms: 400.0, decode_first_step_ms: 42.0, queue_ms: 15.0, kv_transfer_ms: 420.0, prefill_engine_overhead_ms: 145.0, decode_handoff_overhead_ms: 155.0, epp_scheduling_ms: 0.3, engine_overhead_ms: 342.0, gateway_overhead_ms: 0 },
                p90: { available: true, prefill_ms: 1100.0, decode_first_step_ms: 180.0, queue_ms: 25.0, kv_transfer_ms: 900.0, prefill_engine_overhead_ms: 320.0, decode_handoff_overhead_ms: 410.0, epp_scheduling_ms: 0.45, engine_overhead_ms: 910.0, gateway_overhead_ms: 40.0 },
                p99: { available: true, prefill_ms: 1750.0, decode_first_step_ms: 240.0, queue_ms: 40.0, kv_transfer_ms: 1400.0, prefill_engine_overhead_ms: 450.0, decode_handoff_overhead_ms: 560.0, epp_scheduling_ms: 0.7, engine_overhead_ms: 1250.0, gateway_overhead_ms: 85.0 },
                max: { available: true, prefill_ms: 2050.0, queue_ms: 50.0, kv_transfer_ms: 1650.0, epp_scheduling_ms: 0.9, engine_overhead_ms: 2200.0, gateway_overhead_ms: null, slice_note: 'Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.' },
            },
        },
    };

    const html = renderToStaticMarkup(<BottlenecksTab telemetry={sixSliceTelemetry} onSelectTab={() => {}} />);
    assert.match(html, /Min, P50, Mean, P90, P99, and Max/);
    assert.match(html, /Unattributed TTFT/);
    // Routine slice_note must NOT hoist into top-level caveat banner
    assert.doesNotMatch(html, /Server stage histograms do not expose single-request minimums/);

    const ttftSection = html.slice(html.indexOf('TTFT Stage Decomposition'));
    const ttftSectionBody = ttftSection.slice(0, ttftSection.indexOf('Multi-Turn Prefix Cache Reuse') > 0 ? ttftSection.indexOf('Multi-Turn Prefix Cache Reuse') : undefined);
    const ttftPillsHtml = ttftSectionBody.slice(ttftSectionBody.indexOf('mt-3 grid'));
    // Primary summary pills MUST remain anchored to P50 (300 ms Prefill, 10 ms Queue, 350 ms NIXL), NOT Min (88.25 ms)
    assert.match(ttftPillsHtml, /300 ms/);
    assert.match(ttftPillsHtml, /350 ms/);
    assert.doesNotMatch(ttftPillsHtml, /88\.25 ms/);

    let capturedTree = null;
    function CaptureSixSlice() {
        capturedTree = BottlenecksTab({ telemetry: sixSliceTelemetry, onSelectTab: () => {} });
        return null;
    }
    renderToStaticMarkup(<CaptureSixSlice />);
    const ttftBarChart = findElement(capturedTree, (n) => n.type === BarChart && Array.isArray(n.props?.data) && n.props.data.some((r) => r.stat === 'P99'));
    assert.ok(ttftBarChart, 'Expected 6-slice TTFT Stage Decomposition BarChart');
    assert.deepEqual(ttftBarChart.props.data.map((r) => r.stat), ['Min', 'P50', 'Mean', 'P90', 'P99', 'Max']);
    assert.equal(ttftBarChart.props.barCategoryGap, '25%');

    const xAxis = findElement(ttftBarChart, (n) => n.type === XAxis);
    assert.equal(xAxis?.props?.interval, 0);

    const bars = [];
    const walkBars = (node) => {
        if (!node) return;
        if (Array.isArray(node)) { node.forEach(walkBars); return; }
        if (typeof node !== 'object') return;
        if (node.type === Bar) bars.push(node);
        walkBars(node.props?.children);
    };
    walkBars(ttftBarChart);
    assert.ok(bars.every((b) => b.props.maxBarSize === 50), 'All 6-slice bars must use maxBarSize={50}');
    const unattrBar = bars.find((b) => b.props.dataKey === 'client_unattributed_ms');
    assert.equal(unattrBar?.props?.fill, CHART_SERIES[2]);
    assert.equal(unattrBar?.props?.fillOpacity, 0.38);

    const ttftTooltip = findElement(ttftBarChart, (n) => n.type === Tooltip);
    const minRow = ttftBarChart.props.data.find((r) => r.stat === 'Min');
    const minTipHtml = renderToStaticMarkup(
        React.cloneElement(ttftTooltip.props.content, {
            active: true,
            label: 'Min',
            payload: [
                { dataKey: 'client_unattributed_ms', name: 'Unattributed TTFT', value: minRow.client_unattributed_ms, color: CHART_SERIES[2], fillOpacity: 0.38, payload: minRow },
            ],
        }),
    );
    assert.match(minTipHtml, /88\.25 ms/);
    assert.match(minTipHtml, /Server stage histograms do not expose single-request minimums/);
    assert.doesNotMatch(minTipHtml, /\bvllm\b|\bsglang\b|\btrtllm\b/i);

    // EmptyState guard: client-only run with ttft_decomposition.available=false renders EmptyState even when client_series has 6 stats
    const unavailableTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            ttft_decomposition: { available: false },
            stage_latency_breakdown: {
                available: true,
                source: 'client_fallback',
                p50: { available: true, ttft_stage_ms: 130, decode_time_ms: 970 },
            },
        },
        server_telemetry: { available: false, status: 'unavailable' },
    };
    const unavailableHtml = renderToStaticMarkup(<BottlenecksTab telemetry={unavailableTelemetry} onSelectTab={() => {}} />);
    assert.match(unavailableHtml, /Stage decomposition unavailable/);
});

test('ServingTab shares Request Stage Latency Breakdown parity, guards legacy pooled P/D KPIs + banner, and blocks client_fallback Decode leak', () => {
    const base = makeRichTelemetry();

    // 1. Legacy pooled P/D guard + NIXL parity
    const pooledPdTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            stage_latency_breakdown: undefined,
        },
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'disaggregated',
            metric_sources: { vllm_stage_latencies: 'pooled_namespace_legacy' },
            summary: {
                ...base.server_telemetry.summary,
                vllm_ttft_p50_ms: { mean: 999 },
                vllm_e2e_p50_ms: { mean: 999 },
                vllm_tpot_p50_ms: { mean: 11.5 },
                nixl_transfer_latency_p50_ms: { mean: 16 },
                nixl_transfer_latency_p90_ms: { mean: 28 },
            },
            series: [
                { elapsed_seconds: 10, router_scheduler_latency_p50_ms: 4, nixl_transfer_latency_p50_ms: 16, vllm_ttft_p50_ms: 999, vllm_tpot_p50_ms: 11.5 },
            ],
        },
    };
    const pooledHtml = renderToStaticMarkup(<ServingTab telemetry={pooledPdTelemetry} />);
    assert.match(pooledHtml, /Stage P50 \(KV Transfer \/ P\/D TTFT \/ Decode Est\.\)/);
    assert.match(pooledHtml, /Legacy pooled P\/D histograms \(unseparated\)/);
    assert.match(pooledHtml, /Legacy P\/D server telemetry pooled Prefill and Decode histograms across roles/);
    assert.match(pooledHtml, /KV Transfer \(NIXL\)/);

    // 2. client_fallback must NOT leak client-estimated Decode into engine Stage P50 KPI tile
    const clientFallbackTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            stage_latency_breakdown: {
                available: true,
                source: 'client_fallback',
                p50: { available: true, decode_time_ms: 880, ttft_stage_ms: 120, total_stage_ms: 1000 },
                series: [{ t: 10, ttft_ms: 120, decode_ms: 880 }],
            },
        },
        server_telemetry: {
            available: true,
            status: 'available',
            summary: { kv_cache_usage_percent: { mean: 30, max: 40 } },
            per_pod: [],
            series: [],
        },
    };
    const clientHtml = renderToStaticMarkup(<ServingTab telemetry={clientFallbackTelemetry} />);
    assert.match(clientHtml, /Stage P50 \(Queue \/ Prefill \/ Decode\)/);
    assert.doesNotMatch(clientHtml, /— \/ — \/ 880 ms/);
});

test('ServingTab & BottlenecksTab heal historical P/D artifacts across A1-A3, B1-B4 without double-counting Decode NIXL (t102)', () => {
    const base = makeRichTelemetry();
    const historicalPdTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            duration_seconds: 150,
            token_flow: {
                sent_tokens: 843008,
                raw_session_reusable_tokens: 439199,
            },
            windows: {
                ...base.client_series.windows,
                full: {
                    ...base.client_series.windows.full,
                    request_count: 120,
                    total_input_tokens: 843008,
                    input_tokens: { avg: 7025.1, p50: 6800, p90: 9200 },
                },
            },
        },
        sessions_summary: {
            ...base.sessions_summary,
            available: true,
            reusable_prefix_tokens: 439199,
        },
        forensics: {
            ...base.forensics,
            routing_vs_cache: {
                epp_expected_hit_pct: 46.58,
                vllm_actual_hit_pct: 89.2,
                gap_pct: -42.62,
                engine_cached_tok_s: 5300.5,
                engine_fresh_tok_s: 2964.4,
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'disaggregated',
            step_seconds: 5,
            metric_sources: { vllm_stage_latencies: 'pooled_namespace_legacy' },
            router: {
                ready_endpoints: { mean: 4 },
                scheduler_latency_p50_ms: { mean: 1.2 },
            },
            cache_config: {
                hbm_capacity_tokens: 693504,
                effective_capacity_tokens: 693504,
                block_size: 16,
                num_gpu_blocks: 43344,
                engines: [
                    { pod: 'p-0', role: 'prefill', kv_cache_size_tokens: 173376 },
                    { pod: 'p-1', role: 'prefill', kv_cache_size_tokens: 173376 },
                    { pod: 'p-2', role: 'prefill', kv_cache_size_tokens: 173376 },
                    { pod: 'd-0', role: 'decode', kv_cache_size_tokens: 173376 },
                ],
            },
            per_pod: [
                { pod: 'p-0', role: 'prefill', request_rate_rps: { mean: 0.31 }, input_token_rate_tps: { mean: 2200 }, output_token_rate_tps: { mean: 0.3 }, kv_cache_usage_percent: { mean: 21.0, max: 35.0 }, prefix_cache_hit_percent: { mean: 48.0 } },
                { pod: 'p-1', role: 'prefill', request_rate_rps: { mean: 0.22 }, input_token_rate_tps: { mean: 1656.25 }, output_token_rate_tps: { mean: 0.2 }, kv_cache_usage_percent: { mean: 19.0, max: 32.0 }, prefix_cache_hit_percent: { mean: 45.0 } },
                { pod: 'p-2', role: 'prefill', request_rate_rps: { mean: 0.22 }, input_token_rate_tps: { mean: 1656.25 }, output_token_rate_tps: { mean: 0.2 }, kv_cache_usage_percent: { mean: 18.0, max: 30.0 }, prefix_cache_hit_percent: { mean: 45.5 } },
                { pod: 'd-0', role: 'decode', request_rate_rps: { mean: 0.75 }, input_token_rate_tps: { mean: 5426 }, output_token_rate_tps: { mean: 374.3 }, kv_cache_usage_percent: { mean: 46.5, max: 97.5 }, prefix_cache_hit_percent: { mean: 98.9 } },
            ],
            per_endpoint: [
                { endpoint: 'p-0-rank-0', role: 'prefill', inflight_token_load: { mean: 2100.0 } },
                { endpoint: 'p-0-rank-1', role: 'prefill', inflight_token_load: { mean: 2111.2 } },
                { endpoint: 'p-1-rank-0', role: 'prefill', inflight_token_load: { mean: 3127.17 } },
                { endpoint: 'p-2-rank-0', role: 'prefill', inflight_token_load: { mean: 8284.69 } },
                { endpoint: 'd-0-rank-0', role: 'decode', inflight_token_load: { mean: 18886.4 } },
            ],
            derived: {
                avoided_prefill_ratio: 46.58,
                recomputed_prefill_tokens: 450000,
                recomputed_token_rate_tps: 3000,
                token_load_cv: 0.999,
                token_flow: {
                    sent_tokens: 843008,
                    cached_tokens: 843008,
                    recomputed_tokens: 0,
                    cold_new_tokens: 0,
                    cached_pct: 100.0,
                    recomputed_pct: 0.0,
                    cold_new_pct: 0.0,
                    theoretical_ceiling_pct: 100.0,
                },
                prompt_tokens_per_request: {
                    mean_prompt_tokens: 6992.4,
                    mean_cached_tokens: 6915.0,
                    mean_computed_tokens: 77.4,
                    cached_pct: 98.9,
                    prompt_p50: 6800.0,
                    prompt_p90: 9200.0,
                    computed_p50: 71.6,
                    computed_p90: 140.0,
                },
                prompt_tokens_by_role_source: [
                    { role: 'Prefill', local_cache_tokens: 392673, external_kv_tokens: 0, local_compute_tokens: 457688, total_tokens: 850361 },
                    { role: 'Decode', local_cache_tokens: 423346, external_kv_tokens: 426944, local_compute_tokens: 0, total_tokens: 850290 },
                ],
                role_summary: {
                    prefill: { input_token_rate_tps: 5512.5, kv_cache_usage_percent: 25.0 },
                    decode: { output_token_rate_tps: 374.3, kv_cache_usage_percent: 46.5 },
                },
                // Combined P+D polluted by sanitize_server_telemetry
                request_destination_distribution: [
                    { endpoint: 'p-0', percent: 20.0, request_rate_rps: 0.415 },
                    { endpoint: 'p-1', percent: 15.0, request_rate_rps: 0.296 },
                    { endpoint: 'p-2', percent: 15.0, request_rate_rps: 0.289 },
                    { endpoint: 'd-0', percent: 50.0, request_rate_rps: 0.750 },
                ],
                prefill_endpoint_distribution: [
                    { endpoint: 'p-0', percent: 20.0, request_rate_rps: 0.415 },
                    { endpoint: 'p-1', percent: 15.0, request_rate_rps: 0.296 },
                    { endpoint: 'p-2', percent: 15.0, request_rate_rps: 0.289 },
                ],
                decode_endpoint_distribution: [
                    { endpoint: 'd-0', percent: 50.0, request_rate_rps: 0.750 },
                ],
            },
            summary: {
                epp_inflight_requests: { mean: 6, max: 12 },
                // Legacy P+D summed/diluted values
                request_rate_rps: { mean: 1.5, max: 2.5 },
                input_token_rate_tps: { mean: 10938.5, max: 24000.0 },
                output_token_rate_tps: { mean: 375.0, max: 740.0 },
                kv_cache_usage_percent: { mean: 28.2, max: 97.5 },
                prefix_cache_hit_percent: { mean: 40.7, max: 65.0 },
                external_prefix_cache_hit_percent: { mean: 50.3, max: 60.0 },
                engine_prompt_local_cache_tps: { mean: 5300.5 },
                engine_prompt_external_tps: { mean: 2846.3 },
                engine_prompt_local_compute_tps: { mean: 2964.4 },
                cpu_cache_usage_percent: { mean: 10.0, max: 18.0 },
                disk_cache_usage_percent: { mean: 5.0, max: 9.0 },
                nixl_transfer_bytes_per_second: { mean: 52428800 },
            },
            series: [
                {
                    timestamp: '2026-01-01T00:00:05Z',
                    elapsed_seconds: 5,
                    request_rate_rps: 1.5,
                    input_token_rate_tps: 10938.5,
                    output_token_rate_tps: 375.0,
                    kv_cache_usage_percent: 28.2,
                    prefix_cache_hit_percent: 40.7,
                    external_prefix_cache_hit_percent: 50.3,
                    engine_prompt_local_cache_tps: 5300.5,
                    engine_prompt_external_tps: 2846.3,
                    engine_prompt_local_compute_tps: 2964.4,
                    cpu_cache_usage_percent: 10.0,
                    disk_cache_usage_percent: 5.0,
                },
                {
                    timestamp: '2026-01-01T00:00:10Z',
                    elapsed_seconds: 10,
                    request_rate_rps: 1.8,
                    input_token_rate_tps: 12000.0,
                    output_token_rate_tps: 400.0,
                    kv_cache_usage_percent: 32.0,
                    prefix_cache_hit_percent: 42.0,
                    external_prefix_cache_hit_percent: 52.0,
                    engine_prompt_local_cache_tps: 5300.5,
                    engine_prompt_external_tps: 2846.3,
                    engine_prompt_local_compute_tps: 2964.4,
                    cpu_cache_usage_percent: 12.0,
                    disk_cache_usage_percent: 6.0,
                },
            ],
            // Intentionally reversed order vs series to test timestamp-map join
            role_series: [
                {
                    timestamp: '2026-01-01T00:00:10Z',
                    elapsed_seconds: 10,
                    prefill_request_rate_rps: 0.9,
                    decode_request_rate_rps: 0.82,
                    prefill_input_token_rate_tps: 6025.0,
                    decode_output_token_rate_tps: 398.6,
                    prefill_kv_cache_usage_percent: 29.0,
                    decode_kv_cache_usage_percent: 60.0,
                    prefill_prefix_cache_hit_percent: 46.2,
                    decode_external_prefix_cache_hit_percent: 50.3,
                },
                {
                    timestamp: '2026-01-01T00:00:05Z',
                    elapsed_seconds: 5,
                    prefill_request_rate_rps: 0.7,
                    decode_request_rate_rps: 0.68,
                    prefill_input_token_rate_tps: 5000.0,
                    decode_output_token_rate_tps: 350.0,
                    prefill_kv_cache_usage_percent: 21.0,
                    decode_kv_cache_usage_percent: 33.0,
                    prefill_prefix_cache_hit_percent: 46.16,
                    decode_external_prefix_cache_hit_percent: 49.0,
                },
            ],
        },
    };

    const servingHtml = renderToStaticMarkup(<ServingTab telemetry={historicalPdTelemetry} />);
    assertNoNestedButtons(servingHtml, 'ServingTab historical P/D healing');

    // A1: Prompt Token Breakdown healed from 100% to 46.18% / 5.92% / 47.90% with 52.10% ceiling
    const sEngineSlice = servingHtml.slice(servingHtml.indexOf('id="s-engine"'), servingHtml.indexOf('id="s-load"'));
    assert.match(sEngineSlice, /Total:.*843,008 tok/);
    assert.match(sEngineSlice, /Local Prefix Cache:.*389,278.*\(46\.18%\)/);
    assert.match(sEngineSlice, /Recomputed Prefix:.*49,921.*\(5\.92%\)/);
    assert.match(sEngineSlice, /Uncached Prompt:.*403,809.*\(47\.90%\)/);
    assert.match(sEngineSlice, /Ceiling:.*52\.10%/);

    // A2: Prompt Tokens per Request agrees 100% (46.18%) and hides corrupted pooled computed_p50/p90
    assert.match(sEngineSlice, /Prefix Cache:.*3,228\.90 tok.*\(46\.18%\)/);
    assert.match(sEngineSlice, /Prefill Compute:.*3,763\.50 tok/);
    assert.doesNotMatch(sEngineSlice, /P50 \/ P90 Prefill Compute:/);

    // B1: Engine KPIs use Prefill input (5,512.5 -> 5.51K tok/s) & Decode output (374.30 tok/s)
    assert.match(sEngineSlice, /5\.51K tok\/s/);
    assert.match(sEngineSlice, /374\.30 tok\/s/);

    // B2: KV Cache Peak/Mean = 97.50% / 46.50%, Prefill/Decode HBM capacities, #s-evict Pressure Detected
    const sCacheSlice = servingHtml.slice(servingHtml.indexOf('id="s-cache"'), servingHtml.indexOf('id="s-kvreuse"'));
    assert.match(sCacheSlice, /97\.50%/);
    assert.match(sCacheSlice, /46\.50%/);
    assert.match(sCacheSlice, /Prefill HBM/);
    assert.match(sCacheSlice, /520,128 tok/);
    assert.match(sCacheSlice, /Decode HBM/);
    assert.match(sCacheSlice, /173,376 tok/);

    const sEvictSlice = servingHtml.slice(servingHtml.indexOf('id="s-evict"'));
    assert.match(sEvictSlice, /Pressure Detected/);
    assert.match(sEvictSlice, /97\.50%/);

    // A3: #s-kvreuse KPIs use Prefill hit rate, Decode External KV Share (NIXL), scaled local cache throughput (2,550.8 -> 2.55K tok/s)
    const sKvReuseSlice = servingHtml.slice(servingHtml.indexOf('id="s-kvreuse"'), servingHtml.indexOf('id="s-offload"'));
    assert.match(sKvReuseSlice, /46\.18%/);
    assert.match(sKvReuseSlice, /Decode External KV Share \(NIXL\)/);
    assert.match(sKvReuseSlice, /2\.55K tok\/s/);
    assert.match(sKvReuseSlice, /Decode External KV \(NIXL\)/);
    assert.match(sKvReuseSlice, /Prefill pool prompt sources \(tok\/s\); Decode NIXL KV transfer tracked separately/);

    // B3 & B4: Endpoint table Role column, per-role 100% shares, joined inflight_token_load (4,211.2 -> 4.21K; 18,886.4 -> 18.89K), intra-role CV
    const sLoadSlice = servingHtml.slice(servingHtml.indexOf('id="s-load"'), servingHtml.indexOf('id="s-cache"'));
    assert.match(sLoadSlice, /Role/);
    assert.match(sLoadSlice, /41\.50%/);
    assert.match(sLoadSlice, /29\.60%/);
    assert.match(sLoadSlice, /28\.90%/);
    assert.match(sLoadSlice, /100%/);
    assert.match(sLoadSlice, /4\.21K/);
    assert.match(sLoadSlice, /18\.89K/);
    assert.match(sLoadSlice, /Intra-Role Load CV/);
    assert.match(sLoadSlice, /0\.43/);

    assert.equal(
        resolveEffectiveTokenLoadCv(
            historicalPdTelemetry.server_telemetry.derived,
            historicalPdTelemetry.server_telemetry.per_endpoint,
            historicalPdTelemetry.server_telemetry.per_pod,
            true,
        ),
        0.42635,
    );
    assert.equal(
        resolveEffectiveTokenLoadCv(
            { token_load_cv: 0.99 },
            [
                { endpoint: 'p-0-rank-0', role: 'prefill', inflight_token_load: { mean: 1200 } },
                { endpoint: 'd-0-rank-0', role: 'decode', inflight_token_load: { mean: 9800 } },
            ],
            [
                { pod: 'p-0', role: 'prefill' },
                { pod: 'd-0', role: 'decode' },
            ],
            true,
        ),
        0.0,
    );

    // Inspect React tree for timestamp-aligned series, no 91% NIXL stack, 2-line HBM KV, 4-line offload tiers
    let pdTree = null;
    function CaptureHistoricalPd() {
        pdTree = ServingTab({ telemetry: historicalPdTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureHistoricalPd />);

    // t=5 aligns to second role_series entry via timestamp map (not idx 0!)
    const throughputLineChart = findElement(
        pdTree,
        (n) => n.type === LineChart && Boolean(findElement(n, (c) => c.type === Line && c.props?.dataKey === 'input_tps')),
    );
    assert.equal(throughputLineChart.props.data[0].input_tps, 5000);
    assert.equal(throughputLineChart.props.data[0].output_tps, 350);
    assert.equal(throughputLineChart.props.data[0].engine_rps, 0.68);
    assert.equal(throughputLineChart.props.data[1].input_tps, 6025);
    assert.equal(throughputLineChart.props.data[1].output_tps, 398.6);
    assert.equal(throughputLineChart.props.data[1].engine_rps, 0.82);

    // Prefix Cache Hit Rate over Time: ext_hit_pct === 0 (never Decode's 50.3%)
    const prefixHitChart = findElement(
        pdTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'recompute_pct')),
    );
    assert.equal(prefixHitChart.props.data[0].prefix_hit_pct, 46.16);
    assert.equal(prefixHitChart.props.data[0].ext_hit_pct, 0);
    assert.equal(prefixHitChart.props.data[1].prefix_hit_pct, 46.2);
    assert.equal(prefixHitChart.props.data[1].ext_hit_pct, 0);

    // Prompt Token Sources over Time: prompt_external_tps === 0, local_cache scaled by prefillLocalCacheShare
    const promptSourceChart = findElement(
        pdTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'prompt_local_cache_tps')),
    );
    assert.equal(promptSourceChart.props.data[0].prompt_external_tps, 0);
    assert.ok(Math.abs(promptSourceChart.props.data[0].prompt_local_cache_tps - 2550.8) < 0.2);

    // HBM KV Cache Usage over Time: Decode + Prefill lines
    const hbmKvChart = findElement(
        pdTree,
        (n) => n.type === LineChart && Boolean(findElement(n, (c) => c.type === Line && c.props?.dataKey === 'decode_kv_pct')),
    );
    assert.ok(hbmKvChart, 'Expected 2-line HBM KV Cache Usage over Time chart in P/D');
    assert.equal(hbmKvChart.props.data[0].decode_kv_pct, 33.0);
    assert.equal(hbmKvChart.props.data[1].decode_kv_pct, 60.0);
    assert.equal(hbmKvChart.props.data[1].prefill_kv_pct, 29.0);

    // #s-offload KV Cache Usage by Tier: 4 lines (Decode VRAM, Prefill VRAM, CPU, NVMe)
    const offloadSection = findElement(pdTree, (n) => n.type === 'section' && n.props?.id === 's-offload');
    const tierUsageCard = findElement(offloadSection, (n) => n.type === ChartContainer && n.props?.title === 'KV Cache Usage by Tier');
    const tierLines = findAllElements(tierUsageCard, (n) => n.type === Line);
    assert.deepEqual(
        tierLines.map((l) => l.props.dataKey),
        ['decode_kv_pct', 'prefill_kv_pct', 'cpu_kv_pct', 'disk_kv_pct'],
    );

    // Tooltip & engine-agnostic check on ServingTab + BottlenecksTab
    for (const tip of extractTooltipElements(servingHtml)) {
        assert.doesNotMatch(tip.text, ENGINE_SPECIFIC_TOOLTIP_RE, `Forbidden engine term in tooltip: ${tip.text}`);
        if (tip.innerHtml.includes('lens-help-formula')) {
            const formulaIdx = tip.innerHtml.search(/<span\b[^>]*\blens-help-formula\b/);
            const proseBefore = tip.innerHtml.slice(0, formulaIdx).replace(/<[^>]+>/g, '').trim();
            assert.ok(proseBefore.length > 0, `Expected prose above formula in tooltip: ${tip.text}`);
        }
    }

    // BottlenecksTab A3 healing: Multi-Turn Prefix Cache Reuse + EPP Predicted vs vLLM Actual
    let bnTree = null;
    function CaptureHistoricalBn() {
        bnTree = BottlenecksTab({ telemetry: historicalPdTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureHistoricalBn />);
    const bnHtml = renderToStaticMarkup(bnTree);
    const cacheBarChart = findElement(
        bnTree,
        (n) => n.type === BarChart && Boolean(findElement(n, (c) => c.type === 'Bar' || c.props?.dataKey === 'cached_tps')),
    );
    assert.ok(cacheBarChart, 'Expected Prompt Token Throughput BarChart in BottlenecksTab');
    assert.ok(Math.abs(cacheBarChart.props.data[0].cached_tps - 2550.8) < 0.2);
    assert.ok(Math.abs(cacheBarChart.props.data[0].fresh_tps - 2964.4) < 0.2);
    assert.match(bnHtml, /46\.18%/);
    assert.match(bnHtml, /0\.40%/);
});

test('ServingTab & BottlenecksTab preserve canonical role_separated P/D and aggregated runs without false healing (t102)', () => {
    const base = makeRichTelemetry();

    // 1. Canonical role_separated P/D
    const canonicalPdTelemetry = {
        ...base,
        forensics: {
            ...base.forensics,
            routing_vs_cache: {
                role_scope: 'prefill',
                epp_expected_hit_pct: 51.0,
                vllm_actual_hit_pct: 49.5,
                gap_pct: 1.5,
                engine_cached_tok_s: 2475,
                engine_fresh_tok_s: 2525,
            },
        },
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'disaggregated',
            metric_sources: { pd_canonical_metrics: 'role_separated' },
            per_pod: [
                { pod: 'p-0', role: 'prefill' },
                { pod: 'd-0', role: 'decode' },
            ],
            summary_by_role: {
                prefill: {
                    input_token_rate_tps: { mean: 5000, max: 7000 },
                    prefix_cache_hit_percent: { mean: 49.5, max: 72.0 },
                    engine_prompt_local_cache_tps: { mean: 2475 },
                    engine_prompt_local_compute_tps: { mean: 2525 },
                },
                decode: {
                    output_token_rate_tps: { mean: 410, max: 620 },
                    external_prefix_cache_hit_percent: { mean: 54.0 },
                    engine_prompt_external_tps: { mean: 2700 },
                    kv_cache_usage_max_percent: { max: 81.2 },
                },
            },
            derived: {
                token_flow: {
                    role_scope: 'prefill',
                    sent_tokens: 100000,
                    cached_tokens: 49500,
                    recomputed_tokens: 5500,
                    cold_new_tokens: 45000,
                    cached_pct: 49.5,
                    recomputed_pct: 5.5,
                    cold_new_pct: 45.0,
                    theoretical_ceiling_pct: 55.0,
                },
                prompt_tokens_per_request: {
                    role_scope: 'prefill',
                    mean_prompt_tokens: 2000,
                    mean_cached_tokens: 990,
                    mean_computed_tokens: 1010,
                    cached_pct: 49.5,
                    prompt_p50: 1900,
                    prompt_p90: 3200,
                    computed_p50: 880,
                    computed_p90: 1640,
                },
                prompt_tokens_by_role_source: [
                    { role: 'Prefill', local_cache_tokens: 49500, external_kv_tokens: 0, local_compute_tokens: 50500, total_tokens: 100000 },
                    { role: 'Decode', local_cache_tokens: 45000, external_kv_tokens: 54000, local_compute_tokens: 0, total_tokens: 99000 },
                ],
            },
            series: [
                {
                    elapsed_seconds: 5,
                    input_token_rate_tps: 5000,
                    output_token_rate_tps: 410,
                    prefix_cache_hit_percent: 49.5,
                    external_prefix_cache_hit_percent: 54.0,
                    engine_prompt_local_cache_tps: 2475,
                    engine_prompt_external_tps: 2700,
                    engine_prompt_local_compute_tps: 2525,
                    kv_cache_usage_percent: 81.2,
                },
            ],
            role_series: [
                {
                    elapsed_seconds: 5,
                    prefill_prefix_cache_hit_percent: 49.5,
                    prefill_external_prefix_cache_hit_percent: 3.0,
                    decode_external_prefix_cache_hit_percent: 54.0,
                    prefill_kv_cache_usage_percent: 25.0,
                    decode_kv_cache_usage_max_percent: 81.2,
                },
            ],
        },
    };
    const canonicalHtml = renderToStaticMarkup(<ServingTab telemetry={canonicalPdTelemetry} />);
    assert.match(canonicalHtml, /P50 \/ P90 Prefill Compute:.*880 \/ 1,640 tok/);
    assert.match(canonicalHtml, /2\.48K tok\/s/);
    assert.match(canonicalHtml, /54%/);
    let canonicalTree = null;
    function CaptureCanonicalPd() {
        canonicalTree = ServingTab({ telemetry: canonicalPdTelemetry });
        return null;
    }
    renderToStaticMarkup(<CaptureCanonicalPd />);
    const canonicalHitChart = findElement(
        canonicalTree,
        (n) => n.type === AreaChart && Boolean(findElement(n, (c) => c.type === Area && c.props?.dataKey === 'recompute_pct')),
    );
    // Uses prefill_external_prefix_cache_hit_percent (3%), NEVER Decode's 54%
    assert.equal(canonicalHitChart.props.data[0].ext_hit_pct, 3.0);

    // 2. Aggregated run with decode_* keys in role_series (3091d0c4 shape) must NOT trigger P/D healing
    const aggWithDecodeRoleSeries = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'disaggregated',
            per_pod: [{ pod: 'worker-0', role: 'worker' }],
            derived: {
                token_flow: {
                    sent_tokens: 100000,
                    cached_tokens: 50200,
                    recomputed_tokens: 43800,
                    cold_new_tokens: 6000,
                    cached_pct: 50.2,
                    recomputed_pct: 43.8,
                    cold_new_pct: 6.0,
                    theoretical_ceiling_pct: 94.0,
                },
                prompt_tokens_per_request: {
                    mean_prompt_tokens: 4000,
                    mean_cached_tokens: 2144,
                    mean_computed_tokens: 1856,
                    cached_pct: 53.6,
                    prompt_p50: 3800,
                    prompt_p90: 6200,
                    computed_p50: 1911.2,
                    computed_p90: 3100,
                },
            },
            summary: {
                kv_cache_usage_percent: { mean: 55.0, max: 78.0 },
                cpu_cache_usage_percent: { mean: 12.0, max: 20.0 },
                disk_cache_usage_percent: { mean: 4.0, max: 8.0 },
                external_prefix_cache_hit_percent: { mean: 11.5 },
            },
            series: [
                { elapsed_seconds: 5, kv_cache_usage_percent: 55.0, cpu_cache_usage_percent: 12.0, disk_cache_usage_percent: 4.0 },
            ],
            role_series: [
                { elapsed_seconds: 5, decode_kv_cache_usage_percent: 55.0, decode_request_rate_rps: 2.0 },
            ],
        },
    };
    const aggGuardHtml = renderToStaticMarkup(<ServingTab telemetry={aggWithDecodeRoleSeries} />);
    assert.match(aggGuardHtml, /Local Prefix Cache:.*50,200.*\(50\.20%\)/);
    assert.match(aggGuardHtml, /P50 \/ P90 Prefill Compute:.*1,911\.20 \/ 3,100 tok/);
    assert.match(aggGuardHtml, /External Prefix Cache Hit Rate/);
    assert.doesNotMatch(aggGuardHtml, /Decode External KV Share \(NIXL\)/);

    // 3. Legacy P/D prompt_tokens_per_request with missing/zero-token Prefill row:
    //    hides corrupted pooled computed_p50/p90, avoids untrusted legacy token_flow overwrite,
    //    and heals from tokenFlow.cached_pct when token_flow has role_scope: 'prefill'
    const missingPrefillUntrustedTf = {
        ...base,
        server_telemetry: {
            ...base.server_telemetry,
            topology_mode: 'disaggregated',
            per_pod: [
                { pod: 'p-0', role: 'prefill' },
                { pod: 'd-0', role: 'decode' },
            ],
            derived: {
                token_flow: {
                    sent_tokens: 50000,
                    cached_tokens: 50000,
                    recomputed_tokens: 0,
                    cold_new_tokens: 0,
                    cached_pct: 100.0,
                    recomputed_pct: 0.0,
                    cold_new_pct: 0.0,
                    theoretical_ceiling_pct: 100.0,
                },
                prompt_tokens_per_request: {
                    mean_prompt_tokens: 1000,
                    mean_cached_tokens: 720,
                    mean_computed_tokens: 280,
                    cached_pct: 72.0,
                    prompt_p50: 950,
                    prompt_p90: 1500,
                    computed_p50: 111.0,
                    computed_p90: 222.0,
                },
                prompt_tokens_by_role_source: [
                    { role: 'Prefill', local_cache_tokens: 0, external_kv_tokens: 0, local_compute_tokens: 0, total_tokens: 0 },
                    { role: 'Decode', local_cache_tokens: 20000, external_kv_tokens: 25000, local_compute_tokens: 0, total_tokens: 45000 },
                ],
            },
        },
    };
    const untrustedHtml = renderToStaticMarkup(<ServingTab telemetry={missingPrefillUntrustedTf} />);
    assert.match(untrustedHtml, /Prefix Cache:.*720 tok.*\(72%\)/);
    assert.doesNotMatch(untrustedHtml, /P50 \/ P90 Prefill Compute:/);
    assert.doesNotMatch(untrustedHtml, /NaN/);

    const missingPrefillTrustedTf = {
        ...missingPrefillUntrustedTf,
        server_telemetry: {
            ...missingPrefillUntrustedTf.server_telemetry,
            derived: {
                ...missingPrefillUntrustedTf.server_telemetry.derived,
                token_flow: {
                    role_scope: 'prefill',
                    sent_tokens: 50000,
                    cached_tokens: 22500,
                    recomputed_tokens: 2500,
                    cold_new_tokens: 25000,
                    cached_pct: 45.0,
                    recomputed_pct: 5.0,
                    cold_new_pct: 50.0,
                    theoretical_ceiling_pct: 50.0,
                },
            },
        },
    };
    const trustedHtml = renderToStaticMarkup(<ServingTab telemetry={missingPrefillTrustedTf} />);
    assert.match(trustedHtml, /Prefix Cache:.*450 tok.*\(45%\)/);
    assert.match(trustedHtml, /Prefill Compute:.*550 tok/);
    assert.doesNotMatch(trustedHtml, /P50 \/ P90 Prefill Compute:/);
    assert.doesNotMatch(trustedHtml, /NaN/);
});

test('SimulationDashboard Step 2 gates trajectory start ratios by scenario support, validates bounds, preserves user edits, and omits cleared keys (t105)', async (t) => {
    const postedBodies = [];
    await withMountedDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/v1/simulation/tasks' && options.method === 'POST') {
                postedBodies.push(JSON.parse(options.body));
                return Response.json({ task: { id: `sim-ratio-${postedBodies.length}`, status: 'queued' } });
            }
            if (url === '/api/v1/simulation/backends') {
                return Response.json({
                    backends: [{
                        name: 'aiperf',
                        display_name: 'AIPerf',
                        available: true,
                        scenarios: [
                            { name: 'chat', label: 'Chat' },
                            { name: 'coding', label: 'Code Generation' },
                        ],
                        capabilities: {
                            supports_trace_range: true,
                            supports_concurrency: true,
                            scale_factor_trace_formats: ['bailian_trace'],
                            inferencex_agentx_mvp_weka_datasets: [
                                'semianalysis_cc_traces_weka_with_subagents_256k',
                            ],
                            trace_format_capabilities: {
                                mooncake_trace: { supports_trace_range: true, supports_scale_factor: false, supports_fixed_schedule: true, supports_concurrency: true },
                                bailian_trace: { supports_trace_range: true, supports_scale_factor: true, supports_fixed_schedule: true, supports_concurrency: true },
                                weka_public_dataset: { supports_trace_range: false, supports_scale_factor: false, supports_fixed_schedule: false, supports_concurrency: true },
                            },
                            advanced_options: [
                                { name: 'random_seed', label: 'Random seed', minimum: 0 },
                                { name: 'concurrency', label: 'Session concurrency', minimum: 1, default: 1 },
                                { name: 'trajectory_start_min_ratio', label: 'Trajectory start min ratio', minimum: 0, maximum: 1, default: 0.25 },
                                { name: 'trajectory_start_max_ratio', label: 'Trajectory start max ratio', minimum: 0, maximum: 1, default: 0.75 },
                            ],
                        },
                    }],
                });
            }
            if (url === '/api/v1/simulation/scenarios') {
                return Response.json({
                    scenarios: [
                        { value: 'chat', label: 'Chat' },
                        { value: 'coding', label: 'Code Generation' },
                    ],
                });
            }
            if (url === '/api/v1/simulation/trace-datasets') {
                return Response.json({
                    datasets: [
                        {
                            name: 'mooncake_demo',
                            display_name: 'Mooncake Demo',
                            scenario: 'chat',
                            trace_format: 'mooncake_trace',
                            downloaded: true,
                            path: '/traces/mooncake.jsonl',
                            supported_backends: ['aiperf'],
                        },
                        {
                            name: 'bailian_demo',
                            display_name: 'Bailian Demo',
                            scenario: 'chat',
                            trace_format: 'bailian_trace',
                            downloaded: true,
                            path: '/traces/bailian.jsonl',
                            supported_backends: ['aiperf'],
                        },
                        {
                            name: 'weka-claude-code-subagents-256k',
                            display_name: 'Weka Subagents 256k',
                            scenario: 'coding',
                            trace_format: 'weka_public_dataset',
                            source_type: 'external_dataset',
                            downloaded: true,
                            path: 'semianalysis_cc_traces_weka_with_subagents_256k',
                            public_dataset: 'semianalysis_cc_traces_weka_with_subagents_256k',
                            filename: 'semianalysis_cc_traces_weka_with_subagents_256k',
                            supported_backends: ['aiperf'],
                        },
                        {
                            name: 'weka-claude-code',
                            display_name: 'Weka No Subagents',
                            scenario: 'coding',
                            trace_format: 'weka_public_dataset',
                            source_type: 'external_dataset',
                            downloaded: true,
                            path: 'semianalysis_cc_traces_weka_no_subagents',
                            public_dataset: 'semianalysis_cc_traces_weka_no_subagents',
                            filename: 'semianalysis_cc_traces_weka_no_subagents',
                            supported_backends: ['aiperf'],
                        },
                    ],
                });
            }
            if (url.includes('/timeline')) {
                return Response.json({
                    duration_seconds: 30,
                    request_count: 100,
                    bins: [{ start_seconds: 0, end_seconds: 30, request_count: 100 }],
                });
            }
            if (url === '/api/v1/simulation/models/discover') {
                return Response.json({ models: ['llama-3-8b'] });
            }
            if (url.startsWith('/api/v1/simulation/tasks?')) {
                return Response.json({
                    tasks: [],
                    total: 0,
                    counts: { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 },
                    facets: { backends: [], models: [], datasets: [], trace_formats: [], endpoints: [] },
                });
            }
            return Response.json({ items: [] });
        },
        async ({ root, doc, flush, clickButton }) => {
            globalThis.window.location = new URL('http://localhost/');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            // Keep Recharts ResponsiveContainer from mounting AreaChart SVG in the minimal test DOM
            // while still loading timeline bins for Step 1 validation.
            const origCreateElement = doc.createElement.bind(doc);
            doc.createElement = (tag) => {
                const node = origCreateElement(tag);
                node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
                return node;
            };
            await act(async () => {
                root.render(<SimulationDashboard initialAction="new" />);
            });
            await flush();
            await flush();

            // Step 0: trajectory ratios must NOT be duplicated in Advanced backend settings
            assert.equal(findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-trajectoryStartMinRatio').length, 0);
            assert.equal(findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-trajectoryStartMaxRatio').length, 0);

            const extRadio = findElements(
                doc.body,
                (el) => el.tagName === 'INPUT' && reactProps(el).name === 'simulation-endpoint-mode' && reactProps(el).value === 'external',
            )[0];
            await act(async () => {
                reactProps(extRadio).onChange?.({ target: { value: 'external' } });
            });
            const endpointInput = findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-endpoint')[0];
            await act(async () => {
                reactProps(endpointInput).onChange?.({ target: { value: 'http://localhost:8000' } });
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 560));
            });
            await flush();

            // Step 1 -> Mooncake Trace (Fixed Schedule -> disabled in Step 2)
            await clickButton(/Next/);
            await flush();

            const selectScenario = async (scenario) => {
                const scenarioSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-scenario')[0];
                await act(async () => {
                    reactProps(scenarioSelect).onChange?.({ target: { value: scenario } });
                });
                await flush();
            };
            const selectDataset = async (dataset) => {
                const datasetSelect = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-dataset')[0];
                await act(async () => {
                    reactProps(datasetSelect).onChange?.({ target: { value: dataset } });
                });
                await flush();
                await flush();
            };

            await selectDataset('mooncake_demo');
            await clickButton(/Next/);
            await flush();

            const getRatioInputs = () => ({
                minEl: findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-trajectoryStartMinRatio')[0],
                maxEl: findElements(doc.body, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'simulation-trajectoryStartMaxRatio')[0],
            });

            // Mooncake Fixed Schedule: disabled with default values '0.25'/'0.75'
            let { minEl, maxEl } = getRatioInputs();
            assert.ok(minEl && maxEl);
            assert.equal(Boolean(reactProps(minEl).disabled), true);
            assert.equal(Boolean(reactProps(maxEl).disabled), true);
            assert.equal(reactProps(minEl).value, '0.25');
            assert.equal(reactProps(maxEl).value, '0.75');
            assert.equal(reactProps(minEl).placeholder, 'Unset (0.0)');
            assert.equal(reactProps(maxEl).placeholder, 'Unset (1.0)');
            assert.match(doc.body.textContent, /Only applies to compatible Weka subagent datasets or Mooncake in Concurrency Slots mode/);

            // Switch Mooncake to Concurrency Slots -> enabled
            await clickButton(/Concurrency Slots/);
            await flush();
            ({ minEl, maxEl } = getRatioInputs());
            assert.equal(Boolean(reactProps(minEl).disabled), false);
            assert.equal(Boolean(reactProps(maxEl).disabled), false);
            assert.match(doc.body.textContent, /--scenario inferencex-agentx-mvp/);

            // Clear max ratio and set min ratio to '0', then toggle back to Fixed Schedule and back to Concurrency Slots -> user values MUST persist!
            await act(async () => {
                reactProps(minEl).onChange?.({ target: { value: '0' } });
                reactProps(maxEl).onChange?.({ target: { value: '' } });
            });
            await flush();
            await clickButton(/Fixed Schedule/);
            await flush();
            await clickButton(/Concurrency Slots/);
            await flush();
            ({ minEl, maxEl } = getRatioInputs());
            assert.equal(reactProps(minEl).value, '0');
            assert.equal(reactProps(maxEl).value, '');

            // Validation checks: <0, >1, min > max
            await act(async () => {
                reactProps(minEl).onChange?.({ target: { value: '-0.1' } });
                reactProps(maxEl).onChange?.({ target: { value: '1.2' } });
            });
            await flush();
            await clickButton(/Next/);
            await flush();
            assert.match(doc.body.textContent, /Trajectory start min ratio must be a number between 0 and 1/);
            assert.match(doc.body.textContent, /Trajectory start max ratio must be a number between 0 and 1/);

            ({ minEl, maxEl } = getRatioInputs());
            await act(async () => {
                reactProps(minEl).onChange?.({ target: { value: '0.8' } });
                reactProps(maxEl).onChange?.({ target: { value: '0.2' } });
            });
            await flush();
            await clickButton(/Next/);
            await flush();
            assert.match(doc.body.textContent, /Trajectory start min ratio must be less than or equal to Trajectory start max ratio/);

            // Valid single-sided '0' min + cleared max -> submits 0 and omits max
            ({ minEl, maxEl } = getRatioInputs());
            await act(async () => {
                reactProps(minEl).onChange?.({ target: { value: '0' } });
                reactProps(maxEl).onChange?.({ target: { value: '' } });
            });
            await flush();
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();
            assert.equal(postedBodies.length, 1);
            assert.equal(postedBodies[0].backend_options.trajectory_start_min_ratio, 0);
            assert.equal(Object.hasOwn(postedBodies[0].backend_options, 'trajectory_start_max_ratio'), false);

            // Reopen New Task -> resets ratios back to '0.25' / '0.75'
            await clickButton(/New Task/);
            await flush();
            await clickButton(/Next/);
            await flush();

            // Select Bailian in Step 1 -> Step 2 Concurrency Slots -> disabled & omitted
            await selectScenario('chat');
            await selectDataset('bailian_demo');
            await clickButton(/Next/);
            await flush();
            await clickButton(/Concurrency Slots/);
            await flush();
            ({ minEl, maxEl } = getRatioInputs());
            assert.equal(Boolean(reactProps(minEl).disabled), true);
            assert.equal(Boolean(reactProps(maxEl).disabled), true);
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();
            assert.equal(postedBodies.length, 2);
            assert.equal(Object.hasOwn(postedBodies[1].backend_options, 'trajectory_start_min_ratio'), false);
            assert.equal(Object.hasOwn(postedBodies[1].backend_options, 'trajectory_start_max_ratio'), false);

            // Reopen New Task -> Weka no-subagents -> disabled & omitted
            await clickButton(/New Task/);
            await flush();
            await clickButton(/Next/);
            await flush();
            await selectScenario('coding');
            await selectDataset('weka-claude-code');
            await clickButton(/Next/);
            await flush();
            ({ minEl, maxEl } = getRatioInputs());
            assert.equal(Boolean(reactProps(minEl).disabled), true);
            assert.equal(Boolean(reactProps(maxEl).disabled), true);
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();
            assert.equal(postedBodies.length, 3);
            assert.equal(Object.hasOwn(postedBodies[2].backend_options, 'trajectory_start_min_ratio'), false);
            assert.equal(Object.hasOwn(postedBodies[2].backend_options, 'trajectory_start_max_ratio'), false);

            // Reopen New Task -> Weka subagents 256k -> enabled with defaults '0.25' / '0.75'
            await clickButton(/New Task/);
            await flush();
            await clickButton(/Next/);
            await flush();
            await selectScenario('coding');
            await selectDataset('weka-claude-code-subagents-256k');
            await clickButton(/Next/);
            await flush();
            ({ minEl, maxEl } = getRatioInputs());
            assert.equal(Boolean(reactProps(minEl).disabled), false);
            assert.equal(Boolean(reactProps(maxEl).disabled), false);
            assert.equal(reactProps(minEl).value, '0.25');
            assert.equal(reactProps(maxEl).value, '0.75');
            await clickButton(/Next/);
            await flush();
            await clickButton(/Create Task/);
            await flush();
            assert.equal(postedBodies.length, 4);
            assert.equal(postedBodies[3].backend_options.trajectory_start_min_ratio, 0.25);
            assert.equal(postedBodies[3].backend_options.trajectory_start_max_ratio, 0.75);
        },
    );
});

test('LogsAndConfigTab displays trajectory start min/max ratios including 0 and omits when unset across manifest and fallback views (t105)', async (t) => {
    const base = makeRichTelemetry();
    const scenarios = [
        {
            id: 'sim-ratio-manifest-zero',
            task: makeRichTask({
                id: 'sim-ratio-manifest-zero',
                simulation: { duration_seconds: 60, backend: 'aiperf', backend_options: { trajectory_start_min_ratio: 0, trajectory_start_max_ratio: 0.9 } },
            }),
            telemetry: {
                ...base,
                client_series: { ...base.client_series, buckets: [], cold_start_comparison: null },
                server_telemetry: { ...base.server_telemetry, per_pod: [], series: [] },
                run_manifest: {
                    execution_started_at: '2026-01-01T00:00:00Z',
                    backend: 'aiperf',
                    replay: {
                        duration_seconds: 60,
                        backend_options: { trajectory_start_min_ratio: 0, trajectory_start_max_ratio: 0.9 },
                    },
                },
            },
        },
        {
            id: 'sim-ratio-manifest-unset',
            task: makeRichTask({
                id: 'sim-ratio-manifest-unset',
                simulation: { duration_seconds: 60, backend: 'aiperf', backend_options: {} },
            }),
            telemetry: {
                ...base,
                client_series: { ...base.client_series, buckets: [], cold_start_comparison: null },
                server_telemetry: { ...base.server_telemetry, per_pod: [], series: [] },
                run_manifest: {
                    execution_started_at: '2026-01-01T00:00:00Z',
                    backend: 'aiperf',
                    replay: { duration_seconds: 60, backend_options: {} },
                },
            },
        },
        {
            id: 'sim-ratio-fallback-zero',
            task: makeRichTask({
                id: 'sim-ratio-fallback-zero',
                simulation: { duration_seconds: 60, backend: 'aiperf', backend_options: { trajectory_start_min_ratio: 0, trajectory_start_max_ratio: 0.85 } },
            }),
            telemetry: {
                ...base,
                client_series: { ...base.client_series, buckets: [], cold_start_comparison: null },
                server_telemetry: { ...base.server_telemetry, per_pod: [], series: [] },
                run_manifest: null,
            },
        },
    ];

    let activeScenario = scenarios[0];
    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/telemetry')) return Response.json(activeScenario.telemetry);
            if (url.includes(`/api/v1/simulation/tasks/${activeScenario.id}`)) return Response.json({ task: activeScenario.task });
            return Response.json({ backends: [] });
        },
        async ({ root, doc, clickButton, flush }) => {
            const origCreateElement = doc.createElement.bind(doc);
            doc.createElement = (tag) => {
                const node = origCreateElement(tag);
                node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
                return node;
            };
            // 1. Manifest view with 0 and 0.9
            globalThis.window.location = new URL(`http://localhost/?taskId=${activeScenario.id}`);
            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={() => {}} />);
            });
            await flush();
            await flush();
            await clickButton(/Logs & Configuration/);
            await flush();

            const findDdForDt = (label) => {
                const dt = findElements(doc.body, (el) => el.tagName === 'DT' && el.textContent.includes(label))[0];
                return dt?.parentNode ? findElements(dt.parentNode, (el) => el.tagName === 'DD')[0] || null : null;
            };
            assert.equal(findDdForDt('Trajectory start min ratio')?.textContent, '0');
            assert.equal(findDdForDt('Trajectory start max ratio')?.textContent, '0.9');

            // 2. Manifest view with unset ratios omits both rows
            activeScenario = scenarios[1];
            globalThis.window.location = new URL(`http://localhost/?taskId=${activeScenario.id}`);
            await act(async () => {
                root.render(<SimulationTaskDetails key={activeScenario.id} onNavigate={() => {}} />);
            });
            await flush();
            await flush();
            await clickButton(/Logs & Configuration/);
            await flush();
            assert.equal(findDdForDt('Trajectory start min ratio'), null);
            assert.equal(findDdForDt('Trajectory start max ratio'), null);

            // 3. Fallback non-manifest view with 0 and 0.85
            activeScenario = scenarios[2];
            globalThis.window.location = new URL(`http://localhost/?taskId=${activeScenario.id}`);
            await act(async () => {
                root.render(<SimulationTaskDetails key={activeScenario.id} onNavigate={() => {}} />);
            });
            await flush();
            await flush();
            await clickButton(/Logs & Configuration/);
            await flush();
            assert.equal(findDdForDt('Trajectory Start Min Ratio')?.textContent, '0');
            assert.equal(findDdForDt('Trajectory Start Max Ratio')?.textContent, '0.85');
        },
    );
});

test('RequestsTab formats min_ms/max_ms drift histogram labels with integer-vs-2-decimal rule', () => {
    const base = makeRichTelemetry();
    let tree = null;
    function CaptureHist() {
        tree = RequestsTab({
            task: makeRichTask(),
            telemetry: {
                ...base,
                client_series: {
                    ...base.client_series,
                    drift_histogram: [
                        { min_ms: 0, max_ms: 5, count: 12 },
                        { min_ms: 5.5, max_ms: 12.346, count: 8 },
                    ],
                },
            },
        });
        return null;
    }
    renderToStaticMarkup(<CaptureHist />);
    const histChart = findElement(
        tree,
        (n) => Array.isArray(n.props?.data) && n.props.data[0]?.label === '0–5ms',
    );
    assert.ok(histChart, 'Expected drift histogram chart with formatted min_ms–max_ms labels');
    assert.equal(histChart.props.data[1].label, '5.50–12.35ms');
});

test('OverviewTab SLO Attainment footer renders hoverable SLOs tooltip for all 4 configured SLOs including decimals and 0 (t112)', async () => {
    const { resolveConfiguredSlos } = await import('./OverviewTab');
    const task = makeRichTask({
        simulation: {
            duration_seconds: 120,
            backend_options: {
                warmup_seconds: 15,
                ttft_slo: 0.12345,
                tpot_slo: 0.0455,
                e2e_slo: 2.5,
                error_rate_slo: 0,
            },
        },
    });
    const telemetry = makeRichTelemetry({
        forensics: {
            ...makeRichTelemetry().forensics,
            verdict: {
                ...makeRichTelemetry().forensics.verdict,
                slo: { ttft_s: 0.12345, tpot_ms: 45.5, e2e_s: 2.5, error_rate_pct: 5 },
            },
        },
    });

    const resolved = resolveConfiguredSlos(task, telemetry);
    assert.equal(resolved.ttftSloMs, 123.45);
    assert.equal(resolved.tpotSloMs, 45.5);
    assert.equal(resolved.e2eSloMs, 2500);
    assert.equal(resolved.errorRateSloPct, 0);
    assert.equal(resolved.targets.length, 4);

    const html = renderToStaticMarkup(<OverviewTab task={task} telemetry={telemetry} />);
    const buttonMatch = html.match(
        /<button\b([^>]*aria-label="Configured SLOs \(4\)"[^>]*)>([\s\S]*?)<\/button>/,
    );
    assert.ok(buttonMatch, 'Expected focusable SLOs trigger button in SLO Attainment footer');
    const [, buttonAttrs, buttonInner] = buttonMatch;
    assert.match(buttonAttrs, /\btype="button"/);
    assert.match(buttonInner, /SLOs/);
    assert.doesNotMatch(buttonInner, /<button\b/i, 'SLOs trigger button must not contain nested buttons');

    const describedByMatch = buttonAttrs.match(/\baria-describedby="([^"]+)"/);
    assert.ok(describedByMatch, 'Expected aria-describedby linking trigger to tooltip');
    const tooltipId = describedByMatch[1];
    const escapedId = tooltipId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    assert.match(html, new RegExp(`<span\\b[^>]*id="${escapedId}"[^>]*role="tooltip"`));
    const tooltipStart = html.indexOf(`id="${tooltipId}"`);
    const nextCardStart = html.indexOf('Request Throughput', tooltipStart);
    assert.ok(tooltipStart !== -1 && nextCardStart > tooltipStart);
    const tooltipText = html.slice(tooltipStart, nextCardStart).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    assert.match(tooltipText, /Configured SLOs/);
    assert.match(tooltipText, /TTFT ≤ 123\.45 ms/);
    assert.match(tooltipText, /TPOT ≤ 45\.50 ms/);
    assert.match(tooltipText, /E2E ≤ 2\.50 s/);
    assert.match(tooltipText, /Error Rate ≤ 0%/);

    // Zero latency SLOs are preserved (0 >= 0) and keep latency chart reference line safe.
    const zeroLatencyTask = makeRichTask({
        simulation: {
            duration_seconds: 120,
            backend_options: { ttft_slo: 0, tpot_slo: 0, e2e_slo: 0, error_rate_slo: 0 },
        },
    });
    const zeroResolved = resolveConfiguredSlos(zeroLatencyTask, makeRichTelemetry());
    assert.equal(zeroResolved.ttftSloMs, 0);
    assert.equal(zeroResolved.tpotSloMs, 0);
    assert.equal(zeroResolved.e2eSloMs, 0);
    assert.equal(zeroResolved.errorRateSloPct, 0);
    const zeroHtml = renderToStaticMarkup(
        <OverviewTab task={zeroLatencyTask} telemetry={makeRichTelemetry()} />,
    );
    assert.match(zeroHtml, /SLO \(0 ms\)/);
    assert.match(zeroHtml, /≤ 0 ms/);
});

test('OverviewTab does not leak forensics default error_rate_pct when run_manifest.slo or backend_options omits error_rate_slo (t112)', async () => {
    const { resolveConfiguredSlos } = await import('./OverviewTab');

    // Case 1: backend_options omits error_rate_slo while verdict.slo has backend default 5.0
    const partialTask = makeRichTask({
        simulation: {
            duration_seconds: 120,
            backend_options: { ttft_slo: 0.5 },
        },
    });
    const telemetryWithDefaultVerdictError = makeRichTelemetry({
        forensics: {
            ...makeRichTelemetry().forensics,
            verdict: {
                ...makeRichTelemetry().forensics.verdict,
                slo: { ttft_s: 0.5, error_rate_pct: 5.0 },
            },
        },
    });
    const fromBackendOpts = resolveConfiguredSlos(partialTask, telemetryWithDefaultVerdictError);
    assert.equal(fromBackendOpts.ttftSloMs, 500);
    assert.equal(fromBackendOpts.errorRateSloPct, null);
    assert.deepEqual(
        fromBackendOpts.targets.map((item) => item.label),
        ['TTFT'],
    );

    // Case 2: run_manifest.slo has error_rate_pct: null while verdict.slo has default 5.0
    const manifestNullErrorTelemetry = makeRichTelemetry({
        run_manifest: {
            execution_started_at: '2026-01-01T00:00:00Z',
            slo: { ttft_s: 0.4, tpot_s: 0.03, e2e_s: null, error_rate_pct: null },
        },
        forensics: {
            ...makeRichTelemetry().forensics,
            verdict: {
                ...makeRichTelemetry().forensics.verdict,
                slo: { ttft_s: 0.4, tpot_ms: 30, error_rate_pct: 5.0 },
            },
        },
    });
    const fromManifestNull = resolveConfiguredSlos({}, manifestNullErrorTelemetry);
    assert.equal(fromManifestNull.ttftSloMs, 400);
    assert.equal(fromManifestNull.tpotSloMs, 30);
    assert.equal(fromManifestNull.e2eSloMs, null);
    assert.equal(fromManifestNull.errorRateSloPct, null);
    assert.deepEqual(
        fromManifestNull.targets.map((item) => item.label),
        ['TTFT', 'TPOT'],
    );
    const partialHtml = renderToStaticMarkup(
        <OverviewTab task={{}} telemetry={manifestNullErrorTelemetry} />,
    );
    const partialBtnMatch = partialHtml.match(
        /<button\b([^>]*aria-label="Configured SLOs \(2\)"[^>]*)>/,
    );
    assert.ok(partialBtnMatch, 'Expected Configured SLOs (2) trigger button');
    const partialTooltipId = partialBtnMatch[1].match(/\baria-describedby="([^"]+)"/)?.[1];
    assert.ok(partialTooltipId);
    const partialEscapedId = partialTooltipId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    assert.match(partialHtml, new RegExp(`<span\\b[^>]*id="${partialEscapedId}"[^>]*role="tooltip"`));
    const partialTooltipStart = partialHtml.indexOf(`id="${partialTooltipId}"`);
    const partialNextCardStart = partialHtml.indexOf('Request Throughput', partialTooltipStart);
    assert.ok(partialTooltipStart !== -1 && partialNextCardStart > partialTooltipStart);
    const partialTooltipText = partialHtml.slice(partialTooltipStart, partialNextCardStart).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    assert.match(partialTooltipText, /TTFT ≤ 400 ms/);
    assert.match(partialTooltipText, /TPOT ≤ 30 ms/);
    assert.doesNotMatch(partialTooltipText, /Error Rate/);

    // Case 3: legacy/verdict-only telemetry (no run_manifest.slo and no backend_options object) falls back to verdict.slo
    const verdictOnlyTelemetry = makeRichTelemetry({
        run_manifest: { execution_started_at: '2026-01-01T00:00:00Z' },
        forensics: {
            ...makeRichTelemetry().forensics,
            verdict: {
                ...makeRichTelemetry().forensics.verdict,
                slo: { ttft_s: 0.6, error_rate_pct: 2.5 },
            },
        },
    });
    const fromVerdictOnly = resolveConfiguredSlos({}, verdictOnlyTelemetry);
    assert.equal(fromVerdictOnly.ttftSloMs, 600);
    assert.equal(fromVerdictOnly.errorRateSloPct, 2.5);
});

test('OverviewTab reads run_manifest.slo when backend_options is absent and renders No SLOs configured when none are set (t112)', async () => {
    const { resolveConfiguredSlos } = await import('./OverviewTab');

    // run_manifest.slo present, backend_options absent, sub-second e2e_s formatted in ms
    const manifestOnlyTelemetry = makeRichTelemetry({
        run_manifest: {
            execution_started_at: '2026-01-01T00:00:00Z',
            slo: { ttft_s: 0.29, tpot_s: 0.025, e2e_s: 0.85, error_rate_pct: 1.5 },
        },
    });
    const manifestResolved = resolveConfiguredSlos({}, manifestOnlyTelemetry);
    assert.equal(manifestResolved.ttftSloMs, 290);
    assert.equal(manifestResolved.tpotSloMs, 25);
    assert.equal(manifestResolved.e2eSloMs, 850);
    assert.equal(manifestResolved.errorRateSloPct, 1.5);

    const manifestHtml = renderToStaticMarkup(
        <OverviewTab task={{}} telemetry={manifestOnlyTelemetry} />,
    );
    assert.match(manifestHtml, /Configured SLOs \(4\)/);
    assert.match(manifestHtml, /290 ms/);
    assert.match(manifestHtml, /25 ms/);
    assert.match(manifestHtml, /850 ms/);
    assert.match(manifestHtml, /1\.50%/);

    // Zero SLOs configured (even when verdict.slo has default error_rate_pct: 5.0 alongside backend_options: {})
    const noSloTask = makeRichTask({
        simulation: { duration_seconds: 120, backend_options: { warmup_seconds: 15 } },
    });
    const noSloTelemetry = makeRichTelemetry({
        run_manifest: {
            execution_started_at: '2026-01-01T00:00:00Z',
            slo: { ttft_s: null, tpot_s: null, e2e_s: null, error_rate_pct: null },
        },
        forensics: {
            ...makeRichTelemetry().forensics,
            verdict: {
                ...makeRichTelemetry().forensics.verdict,
                slo: { error_rate_pct: 5.0 },
            },
        },
    });
    const noSloResolved = resolveConfiguredSlos(noSloTask, noSloTelemetry);
    assert.equal(noSloResolved.targets.length, 0);
    const noSloHtml = renderToStaticMarkup(
        <OverviewTab task={noSloTask} telemetry={noSloTelemetry} />,
    );
    assert.match(noSloHtml, /No SLOs configured/);
    assert.doesNotMatch(noSloHtml, /Configured SLOs/);
});

test('buildTimeSeriesPoints extracts all 6 latency stats (min, p50, mean, p90, p99, max) across ttft, tpot, and e2e (t114)', () => {
    const clientSeries = {
        buckets: [
            {
                mid_seconds: 5,
                ttft: { min: 12.5, p50: 45.25, mean: 51.4, p90: 95.5, p99: 140.75, max: 210.1 },
                tpot: { min_ms: 4.1, p50_ms: 11.2, avg_ms: 12.8, p90_ms: 18.6, p99_ms: 24.3, max_ms: 31.7 },
                e2e_ms: { min: 120, p50: 420, mean: 465, p90: 810, p99: 1150, max: 1620 },
            },
            {
                time_seconds: 10,
                ttft_min_ms: 14,
                ttft_p50_ms: 48,
                ttft_avg_ms: 54,
                ttft_p90_ms: 98,
                ttft_p99_ms: 155,
                ttft_max_ms: 240,
                tpot_min: 5,
                tpot_p50: 12,
                tpot_mean: 13.5,
                tpot_p90: 20,
                tpot_p99: 27,
                tpot_max: 35,
                latency_min_ms: 130,
                latency_p50_ms: 440,
                average_latency_ms: 490,
                latency_p90_ms: 850,
                latency_p99_ms: 1220,
                latency_max_ms: 1700,
            },
            {
                mid_seconds: 15,
                ttft: { min: 0, p50: 10, mean: 8, p90: 5, p99: 12, max: 15 },
                tpot: { min: 0, p50: 0, mean: 0, p90: 0, p99: 0, max: 0 },
            },
        ],
    };

    const points = buildTimeSeriesPoints(clientSeries, {}, {});
    assert.equal(points.length, 3);

    assert.equal(points[0].ttft_min, 12.5);
    assert.equal(points[0].ttft_p50, 45.25);
    assert.equal(points[0].ttft_mean, 51.4);
    assert.equal(points[0].ttft_p90, 95.5);
    assert.equal(points[0].ttft_p99, 140.75);
    assert.equal(points[0].ttft_max, 210.1);
    assert.equal(points[0].ttft_band, 50.25);

    assert.equal(points[0].tpot_min, 4.1);
    assert.equal(points[0].tpot_p50, 11.2);
    assert.equal(points[0].tpot_mean, 12.8);
    assert.equal(points[0].tpot_p90, 18.6);
    assert.equal(points[0].tpot_p99, 24.3);
    assert.equal(points[0].tpot_max, 31.7);
    assert.ok(Math.abs(points[0].tpot_band - 7.4) < 1e-9);

    assert.equal(points[0].e2e_min, 120);
    assert.equal(points[0].e2e_p50, 420);
    assert.equal(points[0].e2e_mean, 465);
    assert.equal(points[0].e2e_p90, 810);
    assert.equal(points[0].e2e_p99, 1150);
    assert.equal(points[0].e2e_max, 1620);
    assert.equal(points[0].e2e_band, 390);

    assert.equal(points[1].ttft_min, 14);
    assert.equal(points[1].ttft_mean, 54);
    assert.equal(points[1].ttft_max, 240);
    assert.equal(points[1].tpot_min, 5);
    assert.equal(points[1].tpot_mean, 13.5);
    assert.equal(points[1].tpot_max, 35);
    assert.equal(points[1].e2e_min, 130);
    assert.equal(points[1].e2e_mean, 490);
    assert.equal(points[1].e2e_p99, 1220);
    assert.equal(points[1].e2e_max, 1700);

    // Zero ms values preserved and negative band (p90 < p50) clamped to 0
    assert.equal(points[2].ttft_min, 0);
    assert.equal(points[2].ttft_band, 0);
    assert.equal(points[2].tpot_min, 0);
    assert.equal(points[2].tpot_p50, 0);
    assert.equal(points[2].tpot_band, 0);
});

test('OverviewTab Latency over Time renders 6 stat pills, default P50/Mean/P90, dynamic Y-axis, legacy disabled pills, and last-stat guard (t114)', () => {
    const sixStatBuckets = [
        {
            mid_seconds: 5,
            ttft: { min: 15, p50: 80.123, mean: 92.456, p90: 140.789, p99: 420.5, max: 980.25 },
            tpot: { p50: 14, p90: 28 },
            e2e: { min: 200, p50: 800, mean: 900, p90: 1500, p99: 2800, max: 4500 },
        },
        {
            mid_seconds: 15,
            ttft: { min: 18, p50: 85, mean: 98, p90: 150, p99: 460, max: 1100 },
            tpot: { p50: 16, p90: 32 },
            e2e: { min: 220, p50: 840, mean: 940, p90: 1560, p99: 3000, max: 4800 },
        },
    ];
    const richSixTask = makeRichTask({
        simulation: {
            duration_seconds: 120,
            backend_options: { warmup_seconds: 0, ttft_slo: 0.2, tpot_slo: 0.05 },
        },
    });
    const richSixTelemetry = makeRichTelemetry({
        forensics: {
            ...makeRichTelemetry().forensics,
            verdict: { ...makeRichTelemetry().forensics.verdict, slo: { ttft_s: 0.2, tpot_ms: 50 } },
        },
        client_series: {
            ...makeRichTelemetry().client_series,
            warmup_seconds: 0,
            buckets: sixStatBuckets,
        },
    });

    // 1. Default active pills, lines, band, palette colors, legend, and 2-decimal tooltip
    let tree = null;
    function CaptureDefault() {
        tree = OverviewTab({ task: richSixTask, telemetry: richSixTelemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureDefault />);
    const latCard = findElement(tree, (n) => n.type === ChartContainer && n.props?.title === 'Latency over Time');
    assert.ok(latCard, 'Expected Latency over Time ChartContainer');

    const statGroup = findElement(latCard, (n) => n.props?.role === 'group' && n.props?.['aria-label'] === 'Latency statistics');
    assert.ok(statGroup, 'Expected Latency statistics pill group');
    const pillButtons = findAllElements(statGroup, (n) => n.type === 'button');
    assert.deepEqual(
        pillButtons.map((b) => renderToStaticMarkup(b).replace(/<[^>]+>/g, '').trim()),
        ['Min', 'P50', 'Mean', 'P90', 'P99', 'Max', 'All'],
    );
    const statPills = pillButtons.slice(0, 6);
    assert.deepEqual(
        statPills.map((b) => b.props['aria-pressed']),
        [false, true, true, true, false, false],
    );
    // Swatch dots use CHART_SERIES[0..4] and matching strokeOpacity
    const pillSwatches = statPills.map((b) => findElement(b, (c) => c.type === 'span' && c.props?.style?.backgroundColor));
    assert.deepEqual(
        pillSwatches.map((s) => ({ color: s.props.style.backgroundColor, opacity: s.props.style.opacity })),
        [
            { color: CHART_SERIES[0], opacity: 0.65 },
            { color: CHART_SERIES[0], opacity: 1 },
            { color: CHART_SERIES[3], opacity: 0.95 },
            { color: CHART_SERIES[1], opacity: 1 },
            { color: CHART_SERIES[4], opacity: 0.95 },
            { color: CHART_SERIES[4], opacity: 0.65 },
        ],
    );

    const defaultLines = findAllElements(latCard, (n) => n.type === Line);
    assert.deepEqual(
        defaultLines.map((l) => ({
            dataKey: l.props.dataKey,
            stroke: l.props.stroke,
            strokeWidth: l.props.strokeWidth,
            strokeOpacity: l.props.strokeOpacity,
            strokeDasharray: l.props.strokeDasharray,
        })),
        [
            { dataKey: 'ttft_p50', stroke: CHART_SERIES[0], strokeWidth: 2, strokeOpacity: 1, strokeDasharray: undefined },
            { dataKey: 'ttft_mean', stroke: CHART_SERIES[3], strokeWidth: 1.75, strokeOpacity: 0.95, strokeDasharray: '6 2' },
            { dataKey: 'ttft_p90', stroke: CHART_SERIES[1], strokeWidth: 1.5, strokeOpacity: 1, strokeDasharray: '4 3' },
        ],
    );
    const bandAreas = findAllElements(latCard, (n) => n.type === Area);
    assert.equal(bandAreas.length, 2, 'Expected P50-P90 Area band when both P50 and P90 are active');

    const defaultLegend = findElement(latCard, (n) => n.type === ChartLegend);
    assert.deepEqual(defaultLegend.props.entries, [
        { label: 'TTFT P50', color: CHART_SERIES[0], opacity: 1 },
        { label: 'TTFT Mean', color: CHART_SERIES[3], opacity: 0.95 },
        { label: 'TTFT P90', color: CHART_SERIES[1], opacity: 1 },
        { label: 'SLO (200 ms)', color: CHART_SERIES[2] },
    ]);

    // Default YAxis domain excludes Max (1100) and P99 (460), zooming onto P50/Mean/P90 & SLO (200 ms -> 224)
    const defaultYAxis = findElement(latCard, (n) => n.type === YAxis);
    assert.deepEqual(defaultYAxis.props.domain, [0, Math.ceil(200 * 1.12)]);

    // 2-decimal tooltip formatting and swatch opacity resolution without synthetic strokeOpacity
    const latTooltip = findElement(latCard, (n) => n.type === Tooltip);
    const tipHtml = renderToStaticMarkup(
        latTooltip.props.content.type({
            active: true,
            label: 5,
            payload: [
                { dataKey: 'ttft_min', name: 'TTFT Min', color: CHART_SERIES[0], value: 15 },
                { dataKey: 'ttft_p50', name: 'TTFT P50', color: CHART_SERIES[0], value: 80.123 },
                { dataKey: 'ttft_mean', name: 'TTFT Mean', color: CHART_SERIES[3], value: 92.456 },
                { dataKey: 'ttft_p90', name: 'TTFT P90', color: CHART_SERIES[1], value: 140.789 },
                { dataKey: 'ttft_max', name: 'TTFT Max', color: CHART_SERIES[4], value: 980.25 },
            ],
        }),
    );
    assert.match(tipHtml, /TTFT Min/);
    assert.match(tipHtml, /15 ms/);
    assert.match(tipHtml, /TTFT P50/);
    assert.match(tipHtml, /80\.12 ms/);
    assert.match(tipHtml, /TTFT Mean/);
    assert.match(tipHtml, /92\.46 ms/);
    assert.match(tipHtml, /TTFT P90/);
    assert.match(tipHtml, /140\.79 ms/);
    assert.match(tipHtml, /TTFT Max/);
    assert.match(tipHtml, /980\.25 ms/);
    assert.equal((tipHtml.match(/opacity:0\.65/g) || []).length, 2, 'Expected 0.65 opacity swatches on Min and Max tooltip rows');

    // 2. computeOverviewLatencyYMax scales strictly from visible stats
    const parsedPoints = buildTimeSeriesPoints(richSixTelemetry.client_series, {}, {});
    assert.equal(
        computeOverviewLatencyYMax({
            timeSeries: parsedPoints,
            latencyMode: 'ttft',
            visibleStats: ['p50', 'mean', 'p90'],
            latencySloMs: 200,
        }),
        Math.ceil(200 * 1.12),
    );
    assert.equal(
        computeOverviewLatencyYMax({
            timeSeries: parsedPoints,
            latencyMode: 'ttft',
            visibleStats: ['p50', 'mean', 'p90', 'p99'],
            latencySloMs: 200,
        }),
        Math.ceil(460 * 1.12),
    );
    assert.equal(
        computeOverviewLatencyYMax({
            timeSeries: parsedPoints,
            latencyMode: 'ttft',
            visibleStats: ['min', 'p50', 'mean', 'p90', 'p99', 'max'],
            latencySloMs: 200,
        }),
        Math.ceil(1100 * 1.12),
    );

    // 3. Stat selection state transitions, All/Default toggle, fallback, and last-stat guard
    const allKeys = ['min', 'p50', 'mean', 'p90', 'p99', 'max'];
    const allSet = new Set(allKeys);
    let selected = ['p50', 'mean', 'p90'];
    selected = toggleAllLatencyStats(false, allKeys);
    assert.deepEqual(selected, allKeys);
    selected = toggleAllLatencyStats(true, allKeys);
    assert.deepEqual(selected, ['p50', 'mean', 'p90']);

    selected = toggleLatencyStatSelection(selected, 'p50', allSet);
    assert.deepEqual(selected, ['mean', 'p90']);
    selected = toggleLatencyStatSelection(selected, 'mean', allSet);
    assert.deepEqual(selected, ['p90']);
    // Guard prevents deselecting the last active stat
    selected = toggleLatencyStatSelection(selected, 'p90', allSet);
    assert.deepEqual(selected, ['p90']);
    // Re-adding preserves canonical order
    selected = toggleLatencyStatSelection(selected, 'min', allSet);
    assert.deepEqual(selected, ['min', 'p90']);

    // Mode-switch fallback when selected stat ('min') is absent in TPOT (only p50, p90 available)
    const tpotAvailable = [{ key: 'p50' }, { key: 'p90' }];
    assert.deepEqual(
        resolveVisibleLatencyStats(['min'], tpotAvailable).map((s) => s.key),
        ['p50', 'p90'],
    );
    // Unavailable stats cannot be toggled
    assert.deepEqual(toggleLatencyStatSelection(['p50', 'p90'], 'min', new Set(['p50', 'p90'])), ['p50', 'p90']);

    // 4. Legacy artifact missing min/max completely disables Min & Max pills without affecting Y-max
    const legacyNoMinMaxTelemetry = makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            buckets: [
                { mid_seconds: 5, ttft: { p50: 100, mean: 120, p90: 180, p99: 260 } },
                { mid_seconds: 15, ttft: { p50: 110, mean: 130, p90: 190, p99: 280 } },
            ],
        },
    });
    let legacyTree = null;
    function CaptureLegacy() {
        legacyTree = OverviewTab({ task: richSixTask, telemetry: legacyNoMinMaxTelemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureLegacy />);
    const legacyCard = findElement(legacyTree, (n) => n.type === ChartContainer && n.props?.title === 'Latency over Time');
    const legacyGroup = findElement(legacyCard, (n) => n.props?.role === 'group' && n.props?.['aria-label'] === 'Latency statistics');
    const legacyPills = findAllElements(legacyGroup, (n) => n.type === 'button').slice(0, 6);
    assert.equal(legacyPills[0].props.disabled, true);
    assert.equal(legacyPills[0].props['aria-disabled'], true);
    assert.equal(legacyPills[0].props.title, 'Not available in legacy telemetry');
    assert.equal(legacyPills[0].props['aria-pressed'], false);
    assert.equal(legacyPills[5].props.disabled, true);
    assert.equal(legacyPills[5].props['aria-disabled'], true);
    assert.equal(legacyPills[5].props.title, 'Not available in legacy telemetry');
    assert.equal(legacyPills[5].props['aria-pressed'], false);
    const legacyLines = findAllElements(legacyCard, (n) => n.type === Line);
    assert.deepEqual(legacyLines.map((l) => l.props.dataKey), ['ttft_p50', 'ttft_mean', 'ttft_p90']);
});

test('buildColdComparisonBars extracts all 6 canonical stats in monotonic order across TTFT, TPOT, and E2E (t115)', () => {
    const modernCold = {
        ttft_ms: { min: 0, p50: 140.25, avg: 165.4, p90: 310.75, p99: 520.125, max: 890 },
        tpot_ms: { min: 8.5, p50: 21.3, mean: 23.8, p90: 39.2, p99: 54.6, max: 78.9 },
        e2e_ms: { min: 320, p50: 980, avg: 1120.5, p90: 1950, p99: 2840, max: 4100 },
    };
    const modernSteady = {
        ttft_ms: { min: 12, p50: 85.5, avg: 94.1, p90: 180.2, p99: 295.4, max: 450 },
        tpot_ms: { min: 5.2, p50: 14.2, mean: 15.6, p90: 25.1, p99: 36.4, max: 52.1 },
        e2e_ms: { min: 210, p50: 640, avg: 710.25, p90: 1250, p99: 1890, max: 2600 },
    };

    const ttftBars = buildColdComparisonBars(modernCold, modernSteady, 'ttft');
    assert.deepEqual(
        ttftBars,
        [
            { metric: 'TTFT Min', stat: 'Min', cold: 0, steady: 12, unit: 'ms' },
            { metric: 'TTFT P50', stat: 'P50', cold: 140.25, steady: 85.5, unit: 'ms' },
            { metric: 'TTFT Mean', stat: 'Mean', cold: 165.4, steady: 94.1, unit: 'ms' },
            { metric: 'TTFT P90', stat: 'P90', cold: 310.75, steady: 180.2, unit: 'ms' },
            { metric: 'TTFT P99', stat: 'P99', cold: 520.125, steady: 295.4, unit: 'ms' },
            { metric: 'TTFT Max', stat: 'Max', cold: 890, steady: 450, unit: 'ms' },
        ],
    );

    const tpotBars = buildColdComparisonBars(modernCold, modernSteady, 'tpot');
    assert.deepEqual(tpotBars.map((b) => b.metric), ['TPOT Min', 'TPOT P50', 'TPOT Mean', 'TPOT P90', 'TPOT P99', 'TPOT Max']);
    assert.ok(tpotBars.every((b) => b.unit === 'ms/tok'));

    const e2eBars = buildColdComparisonBars(modernCold, modernSteady, 'e2e');
    assert.deepEqual(e2eBars.map((b) => b.metric), ['E2E Min', 'E2E P50', 'E2E Mean', 'E2E P90', 'E2E P99', 'E2E Max']);
    assert.ok(e2eBars.every((b) => b.unit === 'ms'));

    // Flat & nested legacy key variants + partial fixtures
    const flatCold = {
        ttft_min_ms: 20,
        ttftP50: 150,
        ttftAvg: 170,
        ttft: { p90_ms: 300, p99_ms: 490 },
        ttft_max_ms: 720,
    };
    const flatSteady = {
        ttft: { min: 10, p50: 90, mean_ms: 105, p90: 190, p99: 310, max: 480 },
    };
    assert.deepEqual(
        buildColdComparisonBars(flatCold, flatSteady, 'ttft').map((b) => ({
            metric: b.metric,
            cold: b.cold,
            steady: b.steady,
        })),
        [
            { metric: 'TTFT Min', cold: 20, steady: 10 },
            { metric: 'TTFT P50', cold: 150, steady: 90 },
            { metric: 'TTFT Mean', cold: 170, steady: 105 },
            { metric: 'TTFT P90', cold: 300, steady: 190 },
            { metric: 'TTFT P99', cold: 490, steady: 310 },
            { metric: 'TTFT Max', cold: 720, steady: 480 },
        ],
    );

    const partialBars = buildColdComparisonBars(
        { ttft_ms: { p50: 210, p90: 410 } },
        { ttft_ms: { p50: 150, p90: 290 } },
        'ttft',
    );
    assert.deepEqual(
        partialBars.map((b) => b.metric),
        ['TTFT P50', 'TTFT P90'],
    );
    assert.deepEqual(
        buildColdComparisonBars(
            { ttft_ms: { p50: 210, p90: 410 } },
            { ttft_ms: { p50: 150, p90: 290 } },
            'tpot',
        ),
        [],
    );
});

test('OverviewTab Chart 4 renders 6 stats, windows fallback, ToggleGroup switching across TTFT/TPOT/E2E, and legacy TTFT-only hiding (t115)', async (t) => {
    const base = makeRichTelemetry();
    const sixStatTask = makeRichTask({
        simulation: {
            duration_seconds: 120,
            backend_options: { warmup_seconds: 12 },
        },
    });
    const fullColdStartTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            warmup_seconds: 12,
            cold_start_comparison: {
                mode: 'configured_warmup',
                cold_to_steady_ratio: 2.5,
                warmup: {
                    request_count: 100,
                    duration_seconds: 12,
                    ttft_ms: { min: 45, p50: 250, avg: 275.345, p90: 480, p99: 710.678, max: 960 },
                    tpot_ms: { min: 10, p50: 30, mean: 33.456, p90: 52, p99: 68.123, max: 95 },
                    e2e_ms: { min: 400, p50: 1200, avg: 1350, p90: 2200, p99: 3100, max: 4400 },
                },
                steady_state: {
                    request_count: 900,
                    duration_seconds: 108,
                    ttft_ms: { min: 20, p50: 100, avg: 112.222, p90: 210, p99: 340.333, max: 510 },
                    tpot_ms: { min: 6, p50: 20, mean: 21.5, p90: 34, p99: 45.25, max: 62 },
                    e2e_ms: { min: 250, p50: 800, avg: 880, p90: 1450, p99: 2100, max: 2900 },
                },
            },
        },
    };

    // 1. Static tree inspection for Chart 4: 6 bars, XAxis tickFormatter, YAxis unit, ToggleGroup options, and 2-decimal tooltips
    let tree = null;
    function CaptureModernChart4() {
        tree = OverviewTab({ task: sixStatTask, telemetry: fullColdStartTelemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureModernChart4 />);
    const chart4Card = findElement(tree, (n) => n.type === ChartContainer && n.props?.title === 'Warmup vs Steady-State Latency');
    assert.ok(chart4Card, 'Expected Chart 4 ChartContainer');
    assert.match(chart4Card.props.subtitle, /Configured 12 s warmup/);
    assert.match(chart4Card.props.subtitle, /TTFT \(ms\)/);
    assert.match(chart4Card.props.help, /Min, P50, Mean, P90, P99, and Max/);
    assert.match(chart4Card.props.helpFormula, /Min, P50, Mean, P90, P99, Max/);

    const toggleGroup = findElement(chart4Card, (n) => n.type === ToggleGroup);
    assert.ok(toggleGroup, 'Expected ToggleGroup when TTFT, TPOT, E2E are all available');
    assert.deepEqual(
        toggleGroup.props.options,
        [
            { value: 'ttft', label: 'TTFT' },
            { value: 'tpot', label: 'TPOT' },
            { value: 'e2e', label: 'E2E' },
        ],
    );

    const chart4BarChart = findElement(chart4Card, (n) => n.type === BarChart);
    assert.deepEqual(
        chart4BarChart.props.data.map((d) => d.metric),
        ['TTFT Min', 'TTFT P50', 'TTFT Mean', 'TTFT P90', 'TTFT P99', 'TTFT Max'],
    );
    const xAxis = findElement(chart4BarChart, (n) => n.type === XAxis);
    assert.deepEqual(
        chart4BarChart.props.data.map((d) => xAxis.props.tickFormatter(d.metric)),
        ['Min', 'P50', 'Mean', 'P90', 'P99', 'Max'],
    );
    const yAxis = findElement(chart4BarChart, (n) => n.type === YAxis);
    assert.equal(yAxis.props.label?.value, 'ms');

    const bars = findAllElements(chart4BarChart, (n) => n.type === Bar);
    assert.equal(bars.length, 2);
    for (const bar of bars) {
        assert.ok(bar.props.maxBarSize <= 50);
        assert.deepEqual(bar.props.radius, [6, 6, 0, 0]);
        assert.equal(bar.props.isAnimationActive, false);
    }

    const chart4Tooltip = findElement(chart4BarChart, (n) => n.type === Tooltip);
    const ttftP99Html = renderToStaticMarkup(
        chart4Tooltip.props.content.type({
            active: true,
            label: 'TTFT P99',
            payload: [
                { dataKey: 'cold', name: 'Warmup Phase', color: CHART_SERIES[2], value: 710.678, payload: { metric: 'TTFT P99', unit: 'ms' } },
                { dataKey: 'steady', name: 'Steady-State', color: CHART_SERIES[0], value: 340.333, payload: { metric: 'TTFT P99', unit: 'ms' } },
            ],
        }),
    );
    assert.match(ttftP99Html, /TTFT P99/);
    assert.match(ttftP99Html, /710\.68 ms/);
    assert.match(ttftP99Html, /340\.33 ms/);

    const tpotMeanHtml = renderToStaticMarkup(
        chart4Tooltip.props.content.type({
            active: true,
            label: 'TPOT Mean',
            payload: [
                { dataKey: 'cold', name: 'Warmup Phase', color: CHART_SERIES[2], value: 33.456, payload: { metric: 'TPOT Mean', unit: 'ms/tok' } },
                { dataKey: 'steady', name: 'Steady-State', color: CHART_SERIES[0], value: 21.5, payload: { metric: 'TPOT Mean', unit: 'ms/tok' } },
            ],
        }),
    );
    assert.match(tpotMeanHtml, /TPOT Mean/);
    assert.match(tpotMeanHtml, /33\.46 ms\/tok/);
    assert.match(tpotMeanHtml, /21\.50 ms\/tok/);

    // 2. Fallback to raw windows.warmup & windows.profiling when cold_start_comparison is absent
    const windowsFallbackTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            warmup_seconds: 12,
            cold_start_comparison: null,
            windows: {
                warmup: {
                    request_count: 80,
                    duration_seconds: 12,
                    ttft_ms: { min: 30, p50: 180, avg: 205, p90: 360, p99: 520, max: 710 },
                    tpot_ms: { min: 9, p50: 24, avg: 26, p90: 40, p99: 55, max: 72 },
                    e2e_ms: { min: 300, p50: 900, avg: 1020, p90: 1700, p99: 2400, max: 3200 },
                },
                profiling: {
                    request_count: 720,
                    duration_seconds: 108,
                    ttft_ms: { min: 15, p50: 90, avg: 102, p90: 190, p99: 280, max: 400 },
                    tpot_ms: { min: 6, p50: 16, avg: 18, p90: 28, p99: 39, max: 50 },
                    e2e_ms: { min: 200, p50: 600, avg: 680, p90: 1150, p99: 1600, max: 2100 },
                },
            },
        },
    };
    let fallbackTree = null;
    function CaptureWindowsFallback() {
        fallbackTree = OverviewTab({ task: sixStatTask, telemetry: windowsFallbackTelemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureWindowsFallback />);
    const fallbackCard = findElement(fallbackTree, (n) => n.type === ChartContainer && n.props?.title === 'Warmup vs Steady-State Latency');
    const fallbackBarChart = findElement(fallbackCard, (n) => n.type === BarChart);
    assert.deepEqual(
        fallbackBarChart.props.data.map((d) => d.metric),
        ['TTFT Min', 'TTFT P50', 'TTFT Mean', 'TTFT P90', 'TTFT P99', 'TTFT Max'],
    );
    const fallbackToggle = findElement(fallbackCard, (n) => n.type === ToggleGroup);
    assert.deepEqual(fallbackToggle.props.options.map((o) => o.label), ['TTFT', 'TPOT', 'E2E']);
    const fallbackHtml = renderToStaticMarkup(<OverviewTab task={sixStatTask} telemetry={windowsFallbackTelemetry} analysisWindow="profiling" />);
    assert.match(fallbackHtml, /Warmup \/ Steady-State: 2x/);

    // 3. Legacy TTFT-only partial fixture hides ToggleGroup and omits missing stats without NaN
    const legacyPartialTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            cold_start_comparison: {
                mode: 'auto_10_percent',
                cold_to_steady_ratio: 1.5,
                warmup: { request_count: 50, duration_seconds: 10, ttft_ms: { p50: 240, p90: 420 } },
                steady_state: { request_count: 450, duration_seconds: 90, ttft_ms: { p50: 160, p90: 300 } },
            },
        },
    };
    let legacyChart4Tree = null;
    function CaptureLegacyChart4() {
        legacyChart4Tree = OverviewTab({ task: sixStatTask, telemetry: legacyPartialTelemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureLegacyChart4 />);
    const legacyChart4Card = findElement(legacyChart4Tree, (n) => n.type === ChartContainer && n.props?.title === 'Warmup vs Steady-State Latency');
    assert.equal(findElement(legacyChart4Card, (n) => n.type === ToggleGroup), undefined, 'Expected ToggleGroup hidden for TTFT-only fixture');
    const legacyBarChart = findElement(legacyChart4Card, (n) => n.type === BarChart);
    assert.deepEqual(
        legacyBarChart.props.data.map((d) => d.metric),
        ['TTFT P50', 'TTFT P90'],
    );

    // 4. Interactive ToggleGroup switching between TTFT, TPOT (ms/tok), and E2E (ms) in mounted DOM
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        const origCreateElement = doc.createElement.bind(doc);
        doc.createElement = (tag) => {
            const node = origCreateElement(tag);
            node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
            return node;
        };
        await act(async () => {
            root.render(<OverviewTab task={sixStatTask} telemetry={fullColdStartTelemetry} analysisWindow="profiling" />);
        });
        assert.match(doc.body.textContent, /TTFT \(ms\) across Min, P50, Mean, P90, P99, Max/);
        assert.match(doc.body.textContent, /Warmup \/ Steady-State: 2\.50x/);

        const chart4TpotButtons = findElements(
            doc.body,
            (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'TPOT',
        );
        const chart4TpotBtn = chart4TpotButtons[chart4TpotButtons.length - 1];
        assert.ok(chart4TpotBtn, 'Expected TPOT toggle button in Chart 4');
        await act(async () => {
            reactProps(chart4TpotBtn).onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
        assert.match(doc.body.textContent, /TPOT \(ms\/tok\) across Min, P50, Mean, P90, P99, Max/);
        assert.match(doc.body.textContent, /Warmup \/ Steady-State: 1\.50x/);
        assert.doesNotMatch(doc.body.textContent, /NaN/);

        const chart4E2eButtons = findElements(
            doc.body,
            (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'E2E',
        );
        const chart4E2eBtn = chart4E2eButtons[chart4E2eButtons.length - 1];
        assert.ok(chart4E2eBtn, 'Expected E2E toggle button in Chart 4');
        await act(async () => {
            reactProps(chart4E2eBtn).onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
        assert.match(doc.body.textContent, /E2E \(ms\) across Min, P50, Mean, P90, P99, Max/);
        assert.match(doc.body.textContent, /Warmup \/ Steady-State: 1\.50x/);
        assert.doesNotMatch(doc.body.textContent, /NaN/);
    });
});

function makeWorkloadTelemetry(overrides = {}) {
    return {
        ...makeRichTelemetry(),
        client_series: {
            ...makeRichTelemetry().client_series,
            warmup_seconds: 15,
            duration_seconds: 120,
            workload: {
                fallback_to_full: false,
                fallback_synthesized: false,
                windows: {
                    profiling: {
                        window_start_seconds: 15,
                        window_end_seconds: 120,
                        duration_seconds: 105,
                        summary: {
                            counts: { completed: 180, failed: 4, total: 184 },
                            total_input_tokens: 245120,
                            total_output_tokens: 61240,
                            isl: { min: 64, p50: 1024, mean: 1332.1739, p90: 3200, p99: 7900, max: 8192, cv: 0.9412 },
                            osl: { min: 8, p50: 256, mean: 332.8261, p90: 768, p99: 1024, max: 1200, cv: 0.7825 },
                            osl_isl_ratio: {
                                sample_count: 184,
                                excluded_zero_isl_count: 0,
                                mean: 0.4215,
                                p50: 0.2812,
                                p90: 1.15,
                                regimes: {
                                    prefill_heavy: { count: 72, share_pct: 39.13 },
                                    balanced: { count: 88, share_pct: 47.83 },
                                    decode_heavy: { count: 24, share_pct: 13.04 },
                                },
                            },
                            concurrency: {
                                mean: 11.425,
                                p50: 11,
                                p90: 17,
                                p99: 21,
                                peak: 22,
                                active_duration_seconds: 104.4,
                                window_duration_seconds: 105.0,
                            },
                            arrivals: {
                                mean_arrival_rps: 2.0444,
                                peak_arrival_rps: 4.2,
                                burstiness_peak_to_mean: 2.0544,
                                inter_arrival_cv: 1.0318,
                            },
                        },
                        isl_histogram: [
                            { bin_start: 0, bin_end: 512, label: '0–512', count: 40, share_pct: 21.74 },
                            { bin_start: 512, bin_end: 2048, label: '512–2K', count: 100, share_pct: 54.35 },
                        ],
                        isl_cdf: [
                            { percentile: 0, value: 64 },
                            { percentile: 50, value: 1024 },
                            { percentile: 90, value: 3200 },
                            { percentile: 100, value: 8192 },
                        ],
                        osl_histogram: [
                            { bin_start: 0, bin_end: 256, label: '0–256', count: 96, share_pct: 52.17 },
                            { bin_start: 256, bin_end: 1024, label: '256–1K', count: 88, share_pct: 47.83 },
                        ],
                        osl_cdf: [
                            { percentile: 0, value: 8 },
                            { percentile: 50, value: 256 },
                            { percentile: 90, value: 768 },
                            { percentile: 100, value: 1200 },
                        ],
                        isl_vs_osl_buckets: [
                            { label: '<512', count: 40, share_pct: 21.74, osl_p50: 192, osl_mean: 215.4, osl_p90: 480, ratio_p50: 0.625, ratio_mean: 0.7412 },
                            { label: '512-2K', count: 100, share_pct: 54.35, osl_p50: 280, osl_mean: 310.25, osl_p90: 640, ratio_p50: 0.28, ratio_mean: 0.34 },
                        ],
                        request_points: [
                            { t: 18.2, isl: 1024, osl: 128, req_osl: 256, ttft_ms: 42.1, tpot_ms: 12.5, ok: true },
                            { t: 22.5, isl: 512, osl: 256, req_osl: 256, ttft_ms: 28.4, tpot_ms: 11.2, ok: true },
                            { t: 31.0, isl: 128, osl: 320, req_osl: 512, ttft_ms: 19.0, tpot_ms: 10.8, ok: true },
                        ],
                        request_points_total: 3,
                        request_points_sampled: 3,
                        concurrency_residency: [
                            { min_level: 0, max_level: 0, label: '0 (idle)', duration_seconds: 0.6, share_pct: 0.57 },
                            { min_level: 1, max_level: 12, label: '1–12', duration_seconds: 64.4, share_pct: 61.33 },
                        ],
                        timeline: [
                            { t: 20, start_seconds: 15, end_seconds: 25, is_warmup: false, in_flight_avg: 10.42, in_flight_max: 14, arrival_count: 22, arrival_rps: 2.2, completed_count: 20, completed_rps: 2.0 },
                            { t: 30, start_seconds: 25, end_seconds: 35, is_warmup: false, in_flight_avg: 12.15, in_flight_max: 18, arrival_count: 25, arrival_rps: 2.5, completed_count: 23, completed_rps: 2.3 },
                        ],
                        multi_turn: {
                            available: true,
                            is_multi_turn: true,
                            reason: null,
                            session_count: 46,
                            max_turns: 5,
                            turns_per_session_mean: 4.0,
                            reusable_prefix_tokens: 118400,
                            reusable_prefix_pct: 48.3,
                            turns_distribution: [
                                { turns: 1, label: '1 turn', sessions: 4, share_pct: 8.7 },
                                { turns: 4, label: '4 turns', sessions: 42, share_pct: 91.3 },
                            ],
                            isl_by_turn: [
                                { label: 'Turn 1', min_turn: 1, max_turn: 1, request_count: 46, isl_p50: 420, isl_mean: 445.2, isl_p90: 810, reusable_prefix_pct: 0 },
                                { label: 'Turn 2', min_turn: 2, max_turn: 2, request_count: 42, isl_p50: 1180, isl_mean: 1210.5, isl_p90: 2150, reusable_prefix_pct: 54.2 },
                            ],
                        },
                    },
                    full: {
                        window_start_seconds: 0,
                        window_end_seconds: 120,
                        duration_seconds: 120,
                        summary: {
                            counts: { completed: 210, failed: 5, total: 215 },
                            total_input_tokens: 280000,
                            total_output_tokens: 70000,
                            isl: { min: 64, p50: 1000, mean: 1302.33, p90: 3100, p99: 7800, max: 8192, cv: 0.92 },
                            osl: { min: 8, p50: 240, mean: 325.58, p90: 750, p99: 1000, max: 1200, cv: 0.77 },
                            osl_isl_ratio: {
                                sample_count: 215,
                                excluded_zero_isl_count: 0,
                                mean: 0.4,
                                p50: 0.25,
                                p90: 1.1,
                                regimes: {
                                    prefill_heavy: { count: 90, share_pct: 41.86 },
                                    balanced: { count: 95, share_pct: 44.19 },
                                    decode_heavy: { count: 30, share_pct: 13.95 },
                                },
                            },
                            concurrency: { mean: 10.5, p50: 10, p90: 16, p99: 21, peak: 22, active_duration_seconds: 119.0, window_duration_seconds: 120.0 },
                            arrivals: { mean_arrival_rps: 1.7917, peak_arrival_rps: 4.2, burstiness_peak_to_mean: 2.34, inter_arrival_cv: 1.05 },
                        },
                        isl_histogram: [{ bin_start: 0, bin_end: 512, label: '0–512', count: 50, share_pct: 23.26 }],
                        isl_cdf: [{ percentile: 0, value: 64 }, { percentile: 50, value: 1000 }, { percentile: 100, value: 8192 }],
                        osl_histogram: [{ bin_start: 0, bin_end: 256, label: '0–256', count: 115, share_pct: 53.49 }],
                        osl_cdf: [{ percentile: 0, value: 8 }, { percentile: 50, value: 240 }, { percentile: 100, value: 1200 }],
                        isl_vs_osl_buckets: [{ label: '<512', count: 50, share_pct: 23.26, osl_p50: 180, osl_mean: 205, osl_p90: 460, ratio_p50: 0.6, ratio_mean: 0.7 }],
                        request_points: [
                            { t: 4.5, isl: 1200, osl: 120, req_osl: 256, ttft_ms: 55.0, tpot_ms: 14.0, ok: true },
                            { t: 18.2, isl: 1024, osl: 128, req_osl: 256, ttft_ms: 42.1, tpot_ms: 12.5, ok: true },
                            { t: 22.5, isl: 512, osl: 256, req_osl: 256, ttft_ms: 28.4, tpot_ms: 11.2, ok: true },
                            { t: 31.0, isl: 128, osl: 320, req_osl: 512, ttft_ms: 19.0, tpot_ms: 10.8, ok: true },
                        ],
                        request_points_total: 4,
                        request_points_sampled: 4,
                        concurrency_residency: [{ min_level: 1, max_level: 12, label: '1–12', duration_seconds: 75.0, share_pct: 62.5 }],
                        timeline: [
                            { t: 5, start_seconds: 0, end_seconds: 10, is_warmup: true, in_flight_avg: 4.5, in_flight_max: 7, arrival_count: 15, arrival_rps: 1.5, completed_count: 12, completed_rps: 1.2 },
                            { t: 20, start_seconds: 15, end_seconds: 25, is_warmup: false, in_flight_avg: 10.42, in_flight_max: 14, arrival_count: 22, arrival_rps: 2.2, completed_count: 20, completed_rps: 2.0 },
                        ],
                        multi_turn: {
                            available: true,
                            is_multi_turn: false,
                            reason: 'Single-turn prompts in full window.',
                            session_count: 215,
                            max_turns: 1,
                            turns_per_session_mean: 1,
                            reusable_prefix_tokens: 42000,
                            reusable_prefix_pct: 15.0,
                            turns_distribution: [],
                            isl_by_turn: [],
                        },
                    },
                },
            },
            ...overrides,
        },
    };
}

test('WorkloadTab renders 6 sections, 0/2-decimal formatting, numeric CDF XAxis, sub-second residency, and separate timelines (t109)', async (t) => {
    const task = makeRichTask();
    const telemetry = makeWorkloadTelemetry();

    const profilingHtml = renderToStaticMarkup(
        <WorkloadTab task={task} telemetry={telemetry} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(profilingHtml, 'WorkloadTab profiling');
    assert.match(profilingHtml, /Workload Profile · Profiling Steady-State/);
    assert.match(profilingHtml, /184 req/);
    assert.match(profilingHtml, /1,024 tok/);
    assert.match(profilingHtml, /1,332\.17 tok/);
    assert.match(profilingHtml, /11\.43 req/);
    assert.match(profilingHtml, /2\.04 req\/s/);
    assert.match(profilingHtml, /4\.20 req\/s/);
    assert.match(profilingHtml, /48\.30%/);
    assert.match(profilingHtml, /Reusable Prefix Share/);
    assert.match(profilingHtml, /Input Sequence Length \(ISL\)/);
    assert.match(profilingHtml, /Output Sequence Length \(OSL\)/);
    assert.match(profilingHtml, /Input Sequence Length vs Output Sequence Length/);
    assert.match(profilingHtml, /OSL by Input Sequence Length Bucket/);
    assert.match(profilingHtml, /Decode Intensity \(OSL \/ ISL Ratio\) by Input Sequence Length/);
    assert.doesNotMatch(profilingHtml, /Prompt Length|Input Length \(ISL\)|Output Length \(OSL\)/);
    assert.doesNotMatch(profilingHtml, /NaN|undefined/);

    // Degenerate-safe numeric domain check
    assert.deepEqual(computeNumericDomain([{ value: 512 }, { value: 512 }], 'value'), [511, 513]);
    assert.deepEqual(computeNumericDomain([{ value: 0 }], 'value'), [0, 1]);
    assert.deepEqual(computeNumericDomain([{ value: 64 }, { value: 8192 }], 'value'), [64, 8192]);

    // Static tree checks: CDF XAxis type="number", single YAxis per chart, separate timelines, sub-second residency tooltip
    let tree = null;
    function CaptureWorkload() {
        tree = WorkloadTab({ task, telemetry, analysisWindow: 'profiling' });
        return null;
    }
    renderToStaticMarkup(<CaptureWorkload />);

    const charts = findAllElements(tree, (n) => n.type === ChartContainer);
    assert.equal(charts.length, 12);
    for (const chart of charts) {
        const yAxes = findAllElements(chart, (n) => n.type === YAxis);
        assert.equal(yAxes.length, 1, `Expected single YAxis on ${chart.props.title}`);
    }

    const scatterCard = charts.find((c) => c.props.title === 'Input Sequence Length vs Output Sequence Length');
    assert.ok(scatterCard, 'Expected scatter ChartContainer');
    const scatterChartEl = findElement(scatterCard, (n) => n.type === ScatterChart);
    assert.ok(scatterChartEl, 'Expected ScatterChart inside ResponsiveContainer');
    assert.equal(scatterChartEl.props.width, undefined);
    assert.equal(scatterChartEl.props.height, undefined);
    assert.ok(findElement(scatterCard, (n) => n.type === ZAxis), 'Expected ZAxis on ScatterChart');
    const scatters = findAllElements(scatterCard, (n) => n.type === Scatter);
    assert.equal(scatters.length, 3);
    assert.equal(scatters[0].props.fill, CHART_SERIES[1]);
    assert.equal(scatters[1].props.fill, CHART_SERIES[0]);
    assert.equal(scatters[2].props.fill, CHART_SERIES[2]);
    const diagRefLine = findElement(scatterCard, (n) => n.type === ReferenceLine);
    assert.ok(diagRefLine, 'Expected OSL = ISL ReferenceLine');
    assert.equal(diagRefLine.props.label?.value, 'OSL = ISL (1:1)');
    const scatterLegend = findElement(scatterCard, (n) => n.type === ChartLegend);
    assert.equal(scatterLegend.props.entries.length, 3);
    const scatterTooltip = findElement(scatterCard, (n) => n.type === Tooltip);
    const scatterTipHtml = renderToStaticMarkup(
        scatterTooltip.props.content.type({
            active: true,
            payload: [{ color: CHART_SERIES[1], payload: { t: 18.2, isl: 1024, osl: 128, ratio: 0.125, regime: 'prefill_heavy', regimeLabel: 'Prefill-Heavy (<0.25×)' } }],
        }),
    );
    assert.match(scatterTipHtml, /1,024 tok/);
    assert.match(scatterTipHtml, /128 tok/);
    assert.match(scatterTipHtml, /0\.13×/);
    assert.match(scatterTipHtml, /Prefill-Heavy/);
    const zeroIslTipHtml = renderToStaticMarkup(
        scatterTooltip.props.content.type({
            active: true,
            payload: [{ color: CHART_SERIES[2], payload: { t: 5.0, isl: 0, osl: 64, ratio: null, regime: 'decode_heavy', regimeLabel: 'Decode-Heavy (>1.0×)' } }],
        }),
    );
    assert.match(zeroIslTipHtml, /0 tok/);
    assert.match(zeroIslTipHtml, /64 tok/);
    assert.match(zeroIslTipHtml, /∞/);
    assert.match(zeroIslTipHtml, /Decode-Heavy/);

    const islCdfCard = charts.find((c) => c.props.title === 'Request ISL CDF');
    const oslCdfCard = charts.find((c) => c.props.title === 'Request OSL CDF');
    assert.equal(findElement(islCdfCard, (n) => n.type === XAxis).props.type, 'number');
    assert.equal(findElement(oslCdfCard, (n) => n.type === XAxis).props.type, 'number');
    assert.deepEqual(findElement(islCdfCard, (n) => n.type === XAxis).props.domain, [64, 8192]);
    assert.deepEqual(findElement(oslCdfCard, (n) => n.type === XAxis).props.domain, [8, 1200]);

    // Residency tooltip uses sub-second formatDecimal (0.60 s, never rounded 1 s)
    const residencyCard = charts.find((c) => c.props.title === 'In-Flight Concurrency Residency');
    const residencyTooltip = findElement(residencyCard, (n) => n.type === Tooltip);
    const residencyTipHtml = renderToStaticMarkup(
        residencyTooltip.props.content.type({
            active: true,
            payload: [{ color: CHART_SERIES[0], payload: { label: '0 (idle)', duration_seconds: 0.6, share_pct: 0.57 } }],
        }),
    );
    assert.match(residencyTipHtml, /0\.60 s/);
    assert.match(residencyTipHtml, /0\.57%/);

    // Full Run window shows Warmup overlay on timelines and single-turn informative panel
    let fullTree = null;
    function CaptureFullWorkload() {
        fullTree = WorkloadTab({ task, telemetry, analysisWindow: 'full' });
        return null;
    }
    renderToStaticMarkup(<CaptureFullWorkload />);
    const fullHtml = renderToStaticMarkup(
        <WorkloadTab task={task} telemetry={telemetry} analysisWindow="full" />,
    );
    assertNoNestedButtons(fullHtml, 'WorkloadTab full');
    assert.match(fullHtml, /Workload Profile · Full Run/);
    assert.match(fullHtml, /215 req/);
    assert.match(fullHtml, /Single-Turn \/ Sessionless Workload/);
    assert.match(fullHtml, /Single-turn prompts in full window\./);
    assert.match(fullHtml, /Reusable Prefix Share: 15%/);

    const fullConcTimeline = findElement(fullTree, (n) => n.type === ChartContainer && cTitleMatch(n, 'In-Flight Concurrency over Time'));
    assert.ok(findElement(fullConcTimeline, (n) => n.type === ReferenceArea), 'Expected warmup ReferenceArea in full window');
    assert.ok(findElement(fullConcTimeline, (n) => n.type === ReferenceLine), 'Expected warmup ReferenceLine in full window');

    // Interactive ToggleGroup switching Count <-> Share % in mounted DOM
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        const origCreateElement = doc.createElement.bind(doc);
        doc.createElement = (tag) => {
            const node = origCreateElement(tag);
            node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
            return node;
        };
        await act(async () => {
            root.render(<WorkloadTab task={task} telemetry={telemetry} analysisWindow="profiling" />);
        });
        assert.match(doc.body.textContent, /Request ISL Distribution/);
        const shareButtons = findElements(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'Share %');
        assert.equal(shareButtons.length, 2);
        await act(async () => {
            reactProps(shareButtons[0]).onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
        assert.doesNotMatch(doc.body.textContent, /NaN/);
    });
});

function cTitleMatch(node, expectedTitle) {
    return node?.type === ChartContainer && node?.props?.title === expectedTitle;
}

test('WorkloadTab handles legacy summary fallback and empty telemetry cleanly (t109)', () => {
    const legacyTask = makeRichTask({
        result: {
            summary: {
                total_requests: 500,
                successful_requests: 490,
                failed_requests: 10,
                throughput_rps: 8.333,
                duration_seconds: 60,
            },
        },
    });
    const legacyTelemetry = deriveLegacyTelemetryFromTask(legacyTask);
    const resolved = resolveWorkloadWindowData(legacyTask, legacyTelemetry, 'profiling');
    assert.ok(resolved);
    assert.equal(resolved.isClientFallback, true);
    const legacyHtml = renderToStaticMarkup(
        <WorkloadTab task={legacyTask} telemetry={legacyTelemetry} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(legacyHtml, 'WorkloadTab legacy fallback');
    assert.match(legacyHtml, /500 req/);
    assert.match(legacyHtml, /Summary fallback \(no raw request export\)/);
    assert.match(legacyHtml, /ISL histogram unavailable/);

    const emptyHtml = renderToStaticMarkup(
        <WorkloadTab task={{}} telemetry={{}} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(emptyHtml, 'WorkloadTab empty');
    assert.match(emptyHtml, /Workload profile unavailable/);

    // Backend-synthesized / missing-metric shapes: regime share_pct: null & missing counts/concurrency must render "—" (never "0 req" or "— req")
    const backendSynthesizedShape = {
        client_series: {
            workload: {
                fallback_to_full: false,
                fallback_synthesized: true,
                windows: {
                    profiling: {
                        duration_seconds: 60,
                        summary: {
                            counts: {},
                            isl: {},
                            osl: {},
                            osl_isl_ratio: {
                                sample_count: 0,
                                mean: null,
                                p50: null,
                                p90: null,
                                regimes: {
                                    prefill_heavy: { count: 0, share_pct: null },
                                    balanced: { count: 0, share_pct: null },
                                    decode_heavy: { count: 0, share_pct: null },
                                },
                            },
                            concurrency: {},
                            arrivals: {},
                        },
                        isl_histogram: [],
                        isl_cdf: [],
                        osl_histogram: [],
                        osl_cdf: [],
                        isl_vs_osl_buckets: [],
                        concurrency_residency: [],
                        timeline: [],
                        multi_turn: { available: false, is_multi_turn: false, reason: 'Unavailable.' },
                    },
                },
            },
        },
    };
    const synthHtml = renderToStaticMarkup(
        <WorkloadTab task={{}} telemetry={backendSynthesizedShape} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(synthHtml, 'WorkloadTab backend-synthesized shape');
    assert.doesNotMatch(synthHtml, /0 req/);
    assert.doesNotMatch(synthHtml, /— req/);
    assert.doesNotMatch(synthHtml, /undefined|NaN/);
});

test('SimulationTaskDetails wires Workload tab right after Overview and updates on window toggle (t109)', async (t) => {
    const task = makeRichTask({ id: 'sim-workload-1', name: 'workload-run' });
    const telemetry = makeWorkloadTelemetry();
    const prevLocation = globalThis.window?.location;
    const prevHistory = globalThis.window?.history;

    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/api/v1/simulation/tasks/sim-workload-1/telemetry')) {
                return Response.json(telemetry);
            }
            if (url.includes('/api/v1/simulation/tasks/sim-workload-1/artifacts')) {
                return Response.json({ artifacts: [] });
            }
            if (url.includes('/api/v1/simulation/tasks/sim-workload-1')) {
                return Response.json({ task });
            }
            return Response.json({ items: [] });
        },
        async ({ root, doc, clickButton, flush }) => {
            const origCreateElement = doc.createElement.bind(doc);
            doc.createElement = (tag) => {
                const node = origCreateElement(tag);
                node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
                return node;
            };
            globalThis.window.location = new URL('http://localhost/?taskId=sim-workload-1');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            t.after(() => {
                if (prevLocation !== undefined && globalThis.window) globalThis.window.location = prevLocation;
                if (prevHistory !== undefined && globalThis.window) globalThis.window.history = prevHistory;
            });

            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={() => {}} />);
            });
            await flush();
            await flush();

            const tabButtons = findElements(doc.body, (el) => el.tagName === 'BUTTON' && el.getAttribute('role') === 'tab');
            assert.deepEqual(
                tabButtons.map((b) => b.textContent.trim()),
                ['Overview', 'Workload', 'Bottlenecks', 'Serving', 'Requests', 'System', 'Hardware', 'Logs & Configuration'],
            );

            await clickButton(/^Workload$/);
            await flush();
            assert.match(doc.body.textContent, /Workload Profile · Profiling Steady-State/);
            assert.match(doc.body.textContent, /184 req/);

            await clickButton(/Full Run/);
            await flush();
            assert.match(doc.body.textContent, /Workload Profile · Full Run/);
            assert.match(doc.body.textContent, /215 req/);
        },
    );
});

test('WorkloadTab classifyIslOslPoints handles zero ISL safely, renders downsampling note and EmptyState, and filters sub-categories (t123)', async (t) => {
    const classified = classifyIslOslPoints([
        { t: 1, isl: 1000, osl: 100 },
        { t: 2, isl: 1000, osl: 250 },
        { t: 3, isl: 1000, osl: 1000 },
        { t: 4, isl: 100, osl: 200 },
        { t: 5, isl: 0, osl: 64 },
        { t: 6, isl: 0, osl: 0 },
        { t: 7, isl: -5, osl: 10 },
        { t: 8, isl: null, osl: 10 },
        { t: 9, isl: 100, osl: 'bad' },
    ]);
    assert.equal(classified.allPoints.length, 6);
    assert.equal(classified.prefillHeavy.length, 1);
    assert.equal(classified.balanced.length, 3);
    assert.equal(classified.decodeHeavy.length, 2);
    const zeroPos = classified.decodeHeavy.find((p) => p.isl === 0);
    assert.equal(zeroPos.ratio, null);
    assert.equal(zeroPos.osl, 64);
    const zeroBoth = classified.balanced.find((p) => p.isl === 0);
    assert.equal(zeroBoth.ratio, 0);

    const task = makeRichTask();
    const base = makeWorkloadTelemetry();
    const downsampledTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            workload: {
                ...base.client_series.workload,
                windows: {
                    ...base.client_series.workload.windows,
                    profiling: {
                        ...base.client_series.workload.windows.profiling,
                        request_points: [{ t: 16.0, isl: 2048, osl: 128 }],
                        request_points_total: 5432,
                        request_points_sampled: 2000,
                    },
                    full: {
                        ...base.client_series.workload.windows.full,
                        request_points: [],
                        request_points_total: 0,
                        request_points_sampled: 0,
                    },
                },
            },
        },
    };

    const downsampledHtml = renderToStaticMarkup(
        <WorkloadTab task={task} telemetry={downsampledTelemetry} analysisWindow="profiling" />,
    );
    assert.match(downsampledHtml, /Showing 2,000 of 5,432 requests/);

    const emptyScatterHtml = renderToStaticMarkup(
        <WorkloadTab task={task} telemetry={downsampledTelemetry} analysisWindow="full" />,
    );
    assert.match(emptyScatterHtml, /Input Sequence Length vs Output Sequence Length unavailable/);
    assert.doesNotMatch(emptyScatterHtml, /Showing 2,000 of 5,432 requests/);

    // Sub-category pill bar filtering in mounted DOM
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        const origCreateElement = doc.createElement.bind(doc);
        doc.createElement = (tag) => {
            const node = origCreateElement(tag);
            node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
            return node;
        };

        await act(async () => {
            root.render(<WorkloadTab task={task} telemetry={base} analysisWindow="profiling" />);
        });

        const tablist = findElements(doc.body, (el) => el.getAttribute?.('role') === 'tablist' && el.getAttribute?.('aria-label') === 'Workload sub-categories')[0];
        assert.ok(tablist, 'Expected Workload sub-categories tablist');
        const pillTabs = findElements(tablist, (el) => el.tagName === 'BUTTON' && el.getAttribute('role') === 'tab');
        assert.deepEqual(
            pillTabs.map((b) => b.textContent.trim()),
            ['All', 'Summary', 'Input Sequence Length', 'Output Sequence Length', 'ISL vs OSL', 'Concurrency & Arrival', 'Multi-Turn & Prefix'],
        );
        assert.equal(pillTabs[0].getAttribute('aria-selected'), 'true');

        const sectionIds = () => findElements(doc.body, (el) => el.tagName === 'SECTION' && Boolean(el.getAttribute('id'))).map((el) => el.getAttribute('id'));
        assert.deepEqual(sectionIds(), ['w-kpis', 'w-isl', 'w-osl', 'w-joint', 'w-conc', 'w-multiturn']);

        const clickPill = async (label) => {
            const btn = pillTabs.find((b) => b.textContent.trim() === label);
            assert.ok(btn, `Expected pill tab ${label}`);
            await act(async () => {
                reactProps(btn).onClick?.({ preventDefault() {}, stopPropagation() {} });
            });
        };

        await clickPill('ISL vs OSL');
        assert.deepEqual(sectionIds(), ['w-joint']);
        assert.equal(pillTabs.find((b) => b.textContent.trim() === 'ISL vs OSL').getAttribute('aria-selected'), 'true');
        assert.match(doc.body.textContent, /Input Sequence Length vs Output Sequence Length/);

        await clickPill('Input Sequence Length');
        assert.deepEqual(sectionIds(), ['w-isl']);

        await clickPill('All');
        assert.deepEqual(sectionIds(), ['w-kpis', 'w-isl', 'w-osl', 'w-joint', 'w-conc', 'w-multiturn']);
    });
});

test('WorkloadTab renders Workload & Replay Configuration summary for concurrency, fixed-schedule, legacy unavailable, and isolated empty renders (t133)', () => {
    const baseTelemetry = makeWorkloadTelemetry();

    // (a) Concurrency-paced run with explicit source window, format, scenario, warmup, canonical simulation.grace_period_seconds, and scale factor
    const concurrencyTask = makeRichTask({
        scenario: 'api-calling',
        dataset: 'weka_chat_corpus',
        trace_format: 'weka_public_dataset',
        scale_factor: 1.5,
        prompt: {
            trace: {
                format: 'weka_public_dataset',
                start_seconds: 120,
                end_seconds: 420,
            },
        },
        simulation: {
            duration_seconds: 180,
            grace_period_seconds: 15,
            backend_options: {
                concurrency: 64,
                warmup_seconds: 20,
            },
        },
    });
    const concurrencyTelemetry = {
        ...baseTelemetry,
        client_series: {
            ...baseTelemetry.client_series,
            warmup_seconds: 20,
        },
        run_manifest: {
            dataset: { name: 'weka_chat_corpus', format: 'weka_public_dataset', scenario: 'api-calling' },
            replay: {
                start_seconds: 120,
                end_seconds: 420,
                duration_seconds: 180,
                warmup_seconds: 20,
                grace_period_seconds: 15,
                scale_factor: 1.5,
                fixed_schedule: false,
                backend_options: { concurrency: 64 },
            },
        },
    };
    const concurrencyHtml = renderToStaticMarkup(
        <WorkloadTab task={concurrencyTask} telemetry={concurrencyTelemetry} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(concurrencyHtml, 'WorkloadTab concurrency config');
    assert.match(concurrencyHtml, /Workload &amp; Replay Configuration/);
    assert.match(concurrencyHtml, /weka_chat_corpus/);
    assert.match(concurrencyHtml, /weka_public_dataset/);
    assert.match(concurrencyHtml, /Tool &amp; API Use/);
    assert.match(concurrencyHtml, /120s – 420s/);
    assert.match(concurrencyHtml, /Duration: 180s · Warmup: 20s · Grace: 15s/);
    assert.match(concurrencyHtml, /Concurrency/);
    assert.match(concurrencyHtml, /Concurrency \(64\)/);
    assert.match(concurrencyHtml, /64 slots/);
    assert.match(concurrencyHtml, /1\.50×/);
    assert.doesNotMatch(concurrencyHtml, /NaN|undefined/);

    // Task-only render without run_manifest resolves canonical simulation.grace_period_seconds & warmup_seconds: 0
    const taskOnlyGraceHtml = renderToStaticMarkup(
        <WorkloadTab
            task={makeRichTask({
                dataset: 'task_only_corpus',
                simulation: {
                    duration_seconds: 180,
                    grace_period_seconds: 15,
                    backend_options: {
                        concurrency: 1,
                        warmup_seconds: 0,
                    },
                },
            })}
            telemetry={{
                ...baseTelemetry,
                client_series: {
                    ...baseTelemetry.client_series,
                    warmup_seconds: 0,
                },
            }}
            analysisWindow="profiling"
        />,
    );
    assert.match(taskOnlyGraceHtml, /Duration: 180s · Warmup: 0s · Grace: 15s/);
    assert.match(taskOnlyGraceHtml, /1 slot(?!s)/);

    // >= 1000s trace slice formats with commas (never Ks)
    const longSliceHtml = renderToStaticMarkup(
        <WorkloadTab
            task={makeRichTask({
                dataset: 'long_slice_corpus',
                prompt: {
                    trace: { format: 'mooncake_trace', start_seconds: 1200, end_seconds: 1800 },
                },
                simulation: {
                    duration_seconds: 1800,
                    backend_options: { warmup_seconds: 0 },
                },
            })}
            telemetry={{
                ...baseTelemetry,
                client_series: { ...baseTelemetry.client_series, warmup_seconds: 0 },
            }}
            analysisWindow="profiling"
        />,
    );
    assert.match(longSliceHtml, /1,200s – 1,800s/);
    assert.match(longSliceHtml, /Duration: 1,800s · Warmup: 0s/);
    assert.doesNotMatch(longSliceHtml, /Ks/);

    // Invalid numeric bounds (scale_factor: 0, negative warmup/start, inverted end <= start) fall back safely
    const invalidBoundsConfig = resolveWorkloadReplayConfig(
        {
            dataset: 'bounds_test',
            scale_factor: 0,
            prompt: {
                trace: { start_seconds: -10, end_seconds: 0 },
            },
            simulation: {
                duration_seconds: -30,
                grace_period_seconds: -5,
                backend_options: { warmup_seconds: -2, concurrency: 0 },
            },
        },
        {},
    );
    assert.equal(invalidBoundsConfig.replayStartSeconds, 0);
    assert.equal(invalidBoundsConfig.replayEndSeconds, null);
    assert.equal(invalidBoundsConfig.replayDurationSeconds, null);
    assert.equal(invalidBoundsConfig.warmupSeconds, null);
    assert.equal(invalidBoundsConfig.graceSeconds, null);
    assert.equal(invalidBoundsConfig.scaleFactor, 1);
    assert.equal(invalidBoundsConfig.explicitConcurrencyCap, null);

    // (b) Fixed-schedule run computing replay end from start + duration * scale_factor, plus unbounded full-trace fallback
    const fixedTask = makeRichTask({
        scenario: 'coding',
        dataset: 'bailian_code_trace',
        trace_format: 'bailian_trace',
        scale_factor: 2,
        prompt: {
            trace: {
                format: 'bailian_trace',
                start_seconds: 30,
                fixed_schedule: true,
            },
        },
        simulation: {
            duration_seconds: 120,
            backend_options: { warmup_seconds: 15 },
        },
    });
    const fixedHtml = renderToStaticMarkup(
        <WorkloadTab task={fixedTask} telemetry={baseTelemetry} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(fixedHtml, 'WorkloadTab fixed-schedule config');
    assert.match(fixedHtml, /bailian_code_trace/);
    assert.match(fixedHtml, /bailian_trace/);
    assert.match(fixedHtml, /Code Generation/);
    assert.match(fixedHtml, /30s – 270s/);
    assert.match(fixedHtml, /Duration: 120s · Warmup: 15s/);
    assert.match(fixedHtml, /Fixed Schedule/);
    assert.match(fixedHtml, /Source Timestamps/);
    assert.match(fixedHtml, /Fixed Schedule \(trace-driven\)/);
    assert.match(fixedHtml, /2×/);

    // Unbounded full-trace fallback + explicit cap when trace-replayer is fixed-schedule
    const unboundedTask = {
        id: 'sim-unbounded-1',
        backend: 'trace-replayer',
        dataset: 'mooncake_full_trace',
        simulation: {
            backend: 'trace-replayer',
            backend_options: { concurrency: 16 },
        },
        prompt: {
            trace: { format: 'mooncake_trace', start_seconds: 0 },
        },
    };
    const unboundedHtml = renderToStaticMarkup(
        <WorkloadTab task={unboundedTask} telemetry={baseTelemetry} analysisWindow="full" />,
    );
    assert.match(unboundedHtml, /From 0s \(full trace\)/);
    assert.match(unboundedHtml, /Fixed Schedule \(trace-driven\)/);
    assert.match(unboundedHtml, /Cap: 16 slots/);

    // (c) Legacy run with unavailable workload (!resolved) still renders config header above EmptyState
    const legacyManifestOnlyTelemetry = {
        run_manifest: {
            dataset: { name: 'legacy_mooncake_slice', format: 'mooncake_trace', scenario: 'chat' },
            replay: {
                start_seconds: 10,
                duration_seconds: 60,
                warmup_seconds: 5,
                scale_factor: 1,
                fixed_schedule: false,
                concurrency: 32,
            },
        },
    };
    const unavailableWithManifestHtml = renderToStaticMarkup(
        <WorkloadTab task={undefined} telemetry={legacyManifestOnlyTelemetry} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(unavailableWithManifestHtml, 'WorkloadTab unavailable with manifest');
    assert.match(unavailableWithManifestHtml, /Workload &amp; Replay Configuration/);
    assert.match(unavailableWithManifestHtml, /legacy_mooncake_slice/);
    assert.match(unavailableWithManifestHtml, /10s – 70s/);
    assert.match(unavailableWithManifestHtml, /32 slots/);
    assert.match(unavailableWithManifestHtml, /Workload profile unavailable/);
    assert.ok(
        unavailableWithManifestHtml.indexOf('Workload &amp; Replay Configuration')
        < unavailableWithManifestHtml.indexOf('Workload profile unavailable'),
    );

    // (d) Isolated render with task={undefined} and empty telemetry does not throw or fabricate defaults
    const isolatedEmptyConfig = resolveWorkloadReplayConfig(undefined, {});
    assert.equal(isolatedEmptyConfig.hasRunContext, false);
    assert.equal(isolatedEmptyConfig.datasetName, '—');
    assert.equal(isolatedEmptyConfig.replayStartSeconds, null);
    assert.equal(isolatedEmptyConfig.scaleFactor, null);

    const isolatedHtml = renderToStaticMarkup(
        <WorkloadTab task={undefined} telemetry={{}} analysisWindow="profiling" />,
    );
    assertNoNestedButtons(isolatedHtml, 'WorkloadTab isolated undefined task');
    assert.match(isolatedHtml, /Workload profile unavailable/);
    assert.doesNotMatch(isolatedHtml, /Workload &amp; Replay Configuration/);
    assert.doesNotMatch(isolatedHtml, /NaN|undefined/);

    const nullTelemetryHtml = renderToStaticMarkup(
        <WorkloadTab task={undefined} telemetry={undefined} analysisWindow="profiling" />,
    );
    assert.match(nullTelemetryHtml, /Workload profile unavailable/);

    const workloadOnlyNoContextHtml = renderToStaticMarkup(
        <WorkloadTab
            task={undefined}
            telemetry={{ client_series: baseTelemetry.client_series }}
            analysisWindow="profiling"
        />,
    );
    assertNoNestedButtons(workloadOnlyNoContextHtml, 'WorkloadTab workload-only no run context');
    assert.match(workloadOnlyNoContextHtml, /Workload Profile · Profiling Steady-State/);
    assert.doesNotMatch(workloadOnlyNoContextHtml, /Workload &amp; Replay Configuration/);
});

test('LatencyHeatmap renders per-request SVG scatter circles, outlier order, integer Y-ticks, 1-point safety, sampled caption, and flicker-free hover tooltip', async (t) => {
    const scatterData = {
        sequence_length: 'input',
        sequence_length_source: 'observed',
        duration_seconds: 10,
        window_start_seconds: 0,
        min_ms: 40.25,
        max_ms: 420.5,
        points_total: 3,
        points_sampled: 3,
        points: [
            { t: 1.25, seq: 512, ms: 420.5 },
            { t: 2.5, seq: 256, ms: 40.25 },
            { t: 5.0, seq: 1024, ms: 180.75 },
        ],
    };

    const html = renderToStaticMarkup(
        <LatencyHeatmap
            title="TTFT by Time and Input Sequence Length"
            metric="TTFT"
            data={scatterData}
        />,
    );
    assert.match(html, /TTFT per-request scatter plot by elapsed time and input sequence length/);
    assert.match(html, /Elapsed time \(s\)/);
    assert.match(html, /Input sequence length \(tokens\)/);
    assert.match(html, /3 requests/);
    assert.match(html, /Color: TTFT \(ms\)/);
    assert.match(html, /Low \(40\.25 ms\)/);
    assert.match(html, /High \(420\.50 ms\)/);
    assert.match(html, /2\.50s/);
    assert.match(html, /7\.50s/);
    assert.doesNotMatch(html, /legacy binned fallback/);
    assert.doesNotMatch(html, /rx="1\.5"/);

    const circles = [...html.matchAll(/<circle\b[^>]*fill-opacity="0\.8"[^>]*>/g)].map((m) => m[0]);
    assert.equal(circles.length, 3);
    assert.match(circles[0], /fill="rgb\(14, 165, 233\)"/);
    assert.match(circles[2], /fill="rgb\(244, 63, 94\)"/);
    assert.match(circles[0], /r="4\.2"/);

    // Sampled caption + invalid point filtering
    const sampledPoints = Array.from({ length: 20 }, (_, i) => ({ t: i + 1, seq: 128 + i * 32, ms: 50 + i * 10 }));
    const sampledHtml = renderToStaticMarkup(
        <LatencyHeatmap
            title="TTFT by Time and Input Sequence Length"
            metric="TTFT"
            data={{
                sequence_length: 'input',
                duration_seconds: 30,
                points_total: 5432,
                points_sampled: 20,
                points: [
                    ...sampledPoints,
                    { t: null, seq: 100, ms: 50 },
                    { t: 1, seq: false, ms: 50 },
                    { t: 1, seq: 100, ms: -5 },
                ],
            }}
        />,
    );
    assert.match(sampledHtml, /Showing 20 of 5,432 requests \(uniformly sampled\)/);

    // 1-point edge cases (t=0, duration=0, seq=0 and seq=1): zero-division-safe & strictly monotonic integer Y ticks
    for (const seqVal of [0, 1]) {
        const singleHtml = renderToStaticMarkup(
            <LatencyHeatmap
                title="TTFT by Time and Input Sequence Length"
                metric="TTFT"
                data={{
                    sequence_length: 'input',
                    duration_seconds: 0,
                    min_ms: 120,
                    max_ms: 120,
                    points_total: 1,
                    points_sampled: 1,
                    points: [{ t: 0, seq: seqVal, ms: 120 }],
                }}
            />,
        );
        assert.doesNotMatch(singleHtml, /NaN/);
        assert.match(singleHtml, /1 request(?!s)/);
        assert.match(singleHtml, /Uniform \(120 ms\)/);
        assert.match(singleHtml, />0<\/text>.*>1<\/text>.*>2<\/text>.*>3<\/text>.*>4<\/text>/);
    }

    // Mounted DOM: circle hover tooltip, highlight ring, background 18px nearest-dot snap, and svg mouseleave clear
    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        await act(async () => {
            root.render(
                <LatencyHeatmap
                    title="TTFT by Time and Input Sequence Length"
                    metric="TTFT"
                    data={scatterData}
                />,
            );
        });
        const svg = findElements(
            doc.body,
            (el) => el.tagName === 'SVG' && /TTFT per-request scatter plot/.test(el.getAttribute('aria-label') || ''),
        )[0];
        assert.ok(svg, 'Expected scatter SVG');
        const dotNodes = findElements(svg, (el) => el.tagName === 'CIRCLE' && typeof reactProps(el).onMouseEnter === 'function');
        assert.equal(dotNodes.length, 3);

        // Hover high-latency outlier (last circle in sorted order: t=1.25, seq=512, ms=420.5)
        await act(async () => {
            reactProps(dotNodes[2]).onMouseEnter({ clientX: 200, clientY: 140 });
        });
        let tooltip = findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip'))[0];
        assert.ok(tooltip, 'Expected ChartTooltip on circle hover');
        assert.match(tooltip.textContent, /Request/);
        assert.match(tooltip.textContent, /1\.25 s/);
        assert.match(tooltip.textContent, /512 tok/);
        assert.match(tooltip.textContent, /420\.50 ms/);
        const ringNodes = findElements(svg, (el) => el.tagName === 'CIRCLE' && el.getAttribute('stroke') === 'var(--text-primary)');
        assert.equal(ringNodes.length, 1);
        assert.equal(ringNodes[0].getAttribute('r'), '6.2');

        // Background rect nearest-dot snap within 18px (middle dot: cx/cy from dotNodes[1])
        const bgRect = findElements(svg, (el) => el.tagName === 'RECT' && typeof reactProps(el).onMouseMove === 'function')[0];
        assert.ok(bgRect, 'Expected interactive plot background rect');
        const midCx = Number(dotNodes[1].getAttribute('cx'));
        const midCy = Number(dotNodes[1].getAttribute('cy'));
        svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 640, height: 256, right: 640, bottom: 256 });
        await act(async () => {
            reactProps(bgRect).onMouseMove({ clientX: midCx + 6, clientY: midCy - 4 });
        });
        tooltip = findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip'))[0];
        assert.ok(tooltip, 'Expected nearest-dot snap tooltip');
        assert.match(tooltip.textContent, /5 s/);
        assert.match(tooltip.textContent, /1,024 tok/);
        assert.match(tooltip.textContent, /180\.75 ms/);

        // Moving >18px away on background rect clears tooltip
        await act(async () => {
            reactProps(bgRect).onMouseMove({ clientX: 600, clientY: 20 });
        });
        assert.equal(findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip')).length, 0);

        // Re-hover dot and leave SVG clears tooltip
        await act(async () => {
            reactProps(dotNodes[0]).onMouseEnter({ clientX: 220, clientY: 160 });
        });
        assert.equal(findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip')).length, 1);
        await act(async () => {
            reactProps(svg).onMouseLeave();
        });
        assert.equal(findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip')).length, 0);
    });
});

test('LatencyHeatmap attaches ResizeObserver when transitioning from empty state to populated scatter', async (t) => {
    const scatterData = {
        sequence_length: 'input',
        sequence_length_source: 'observed',
        duration_seconds: 10,
        window_start_seconds: 0,
        min_ms: 40.25,
        max_ms: 420.5,
        points_total: 2,
        points_sampled: 2,
        points: [
            { t: 1.25, seq: 512, ms: 420.5 },
            { t: 2.5, seq: 256, ms: 40.25 },
        ],
    };

    await withMountedDom(t, async () => Response.json({}), async ({ root, doc }) => {
        const observerCallbacks = [];
        globalThis.ResizeObserver = class MockResizeObserver {
            constructor(cb) {
                observerCallbacks.push(cb);
            }
            observe() {}
            unobserve() {}
            disconnect() {}
        };
        await act(async () => {
            root.render(
                <LatencyHeatmap
                    title="TTFT by Time and Input Sequence Length"
                    metric="TTFT"
                    data={null}
                />,
            );
        });
        assert.match(doc.body.textContent, /TTFT scatter plot unavailable/);
        assert.equal(observerCallbacks.length, 0);

        await act(async () => {
            root.render(
                <LatencyHeatmap
                    title="TTFT by Time and Input Sequence Length"
                    metric="TTFT"
                    data={scatterData}
                />,
            );
        });
        assert.equal(observerCallbacks.length, 1);
        const svg = findElements(
            doc.body,
            (el) => el.tagName === 'SVG' && /TTFT per-request scatter plot/.test(el.getAttribute('aria-label') || ''),
        )[0];
        assert.ok(svg, 'Expected scatter SVG after transitioning from empty state');
        assert.equal(svg.getAttribute('viewBox'), '0 0 640 256');
        await act(async () => {
            observerCallbacks[0]([{ contentRect: { width: 1120 } }]);
        });
        assert.equal(svg.getAttribute('viewBox'), '0 0 1120 256');
    });
});

test('RequestsTab scopes per-request scatter dots to active window and prevents empty steady-state window from leaking warmup dots', () => {
    const base = makeRichTelemetry();
    const windowedScatterTelemetry = {
        ...base,
        client_series: {
            ...base.client_series,
            buckets: [],
            slot_lanes: undefined,
            heatmap: {
                ttft: {
                    sequence_length: 'input',
                    sequence_length_source: 'observed',
                    duration_seconds: 40,
                    window_start_seconds: 0,
                    points_total: 2,
                    points_sampled: 2,
                    points: [
                        { t: 2, seq: 128, ms: 950 },
                        { t: 25, seq: 512, ms: 120 },
                    ],
                },
                tpot: {
                    sequence_length: 'output',
                    sequence_length_source: 'requested',
                    duration_seconds: 40,
                    window_start_seconds: 0,
                    points_total: 2,
                    points_sampled: 2,
                    points: [
                        { t: 2, seq: 64, ms: 85 },
                        { t: 25, seq: 128, ms: 22 },
                    ],
                },
            },
            windows: {
                profiling: {
                    duration_seconds: 30,
                    heatmap: {
                        ttft: {
                            sequence_length: 'input',
                            sequence_length_source: 'observed',
                            duration_seconds: 40,
                            window_start_seconds: 10,
                            points_total: 1,
                            points_sampled: 1,
                            points: [{ t: 25, seq: 512, ms: 120 }],
                        },
                        tpot: {
                            sequence_length: 'output',
                            sequence_length_source: 'requested',
                            duration_seconds: 40,
                            window_start_seconds: 10,
                            points_total: 1,
                            points_sampled: 1,
                            points: [{ t: 25, seq: 128, ms: 22 }],
                        },
                    },
                },
                full: {
                    duration_seconds: 40,
                    heatmap: {
                        ttft: {
                            sequence_length: 'input',
                            sequence_length_source: 'observed',
                            duration_seconds: 40,
                            window_start_seconds: 0,
                            points_total: 2,
                            points_sampled: 2,
                            points: [
                                { t: 2, seq: 128, ms: 950 },
                                { t: 25, seq: 512, ms: 120 },
                            ],
                        },
                        tpot: {
                            sequence_length: 'output',
                            sequence_length_source: 'requested',
                            duration_seconds: 40,
                            window_start_seconds: 0,
                            points_total: 2,
                            points_sampled: 2,
                            points: [
                                { t: 2, seq: 64, ms: 85 },
                                { t: 25, seq: 128, ms: 22 },
                            ],
                        },
                    },
                },
            },
        },
        sessions_summary: { available: false, reason: 'No sessions' },
    };

    const profilingHtml = renderToStaticMarkup(
        <RequestsTab task={makeRichTask()} telemetry={windowedScatterTelemetry} analysisWindow="profiling" />,
    );
    assert.match(profilingHtml, /Uniform \(120 ms\)/);
    assert.doesNotMatch(profilingHtml, /950 ms/);
    assert.match(profilingHtml, /Requested output sequence length \(tokens\)/);

    const fullHtml = renderToStaticMarkup(
        <RequestsTab task={makeRichTask()} telemetry={windowedScatterTelemetry} analysisWindow="full" />,
    );
    assert.match(fullHtml, /High \(950 ms\)/);
    assert.match(fullHtml, /2 requests/);

    // Explicit null on steady-state window must NOT leak warmup/full-run dots
    const emptySteadyTelemetry = {
        ...windowedScatterTelemetry,
        client_series: {
            ...windowedScatterTelemetry.client_series,
            windows: {
                ...windowedScatterTelemetry.client_series.windows,
                profiling: {
                    duration_seconds: 30,
                    heatmap: { ttft: null, tpot: null },
                },
            },
        },
    };
    const emptySteadyHtml = renderToStaticMarkup(
        <RequestsTab task={makeRichTask()} telemetry={emptySteadyTelemetry} analysisWindow="profiling" />,
    );
    assert.match(emptySteadyHtml, /TTFT scatter plot unavailable/);
    assert.match(emptySteadyHtml, /TPOT scatter plot unavailable/);
    assert.doesNotMatch(emptySteadyHtml, /950 ms/);
    assert.doesNotMatch(emptySteadyHtml, /120 ms/);
});

test('RequestsTab SlotSwimlaneChart highlights all turns of a clicked session, draws connectors, enriches tooltip, and supports all deselect paths', async (t) => {
    const makeInteractiveTelemetry = (items, peakSlots = 3) => makeRichTelemetry({
        client_series: {
            ...makeRichTelemetry().client_series,
            warmup_seconds: 0,
            buckets: [],
            slot_lanes: {
                peak_slots: peakSlots,
                total_requests: items.length,
                session_count: new Set(items.map((it) => (it.session_id || '').trim()).filter(Boolean)).size,
                items,
            },
        },
        sessions_summary: {
            available: true,
            correlation: { matched_count: items.length, total_count: items.length, matched_ratio: 1.0 },
        },
    });

    const multiTurnItems = [
        { slot: 0, start_s: 1.0, end_s: 4.0, cache_hit_pct: 5.0, tier: 'cold', session_id: ' sess-multi ', turn: 1, input_tokens: 400, output_tokens: 64, ttft_ms: 210 },
        { slot: 1, start_s: 5.0, end_s: 8.5, cache_hit_pct: 55.0, tier: 'partial', session_id: 'sess-multi', turn: 2, input_tokens: 800, output_tokens: 90, ttft_ms: 140 },
        // Sub-pixel turn duration so highlight width enforces Math.max(4, w)
        { slot: 2, start_s: 10.0, end_s: 10.001, cache_hit_pct: 90.0, tier: 'warm', session_id: 'sess-multi', turn: 3, input_tokens: 1200, output_tokens: 32, ttft_ms: 85 },
        { slot: 0, start_s: 4.5, end_s: 9.0, cache_hit_pct: 20.0, tier: 'partial', session_id: 'sess-other', turn: 1, input_tokens: 500, output_tokens: 50, ttft_ms: 170 },
        { slot: 1, start_s: 0.5, end_s: 2.0, cache_hit_pct: 0.0, tier: 'cold', session_id: null, turn: null, input_tokens: 300, output_tokens: 40, ttft_ms: 190 },
        { slot: 2, start_s: 2.5, end_s: 4.5, cache_hit_pct: 0.0, tier: 'cold', session_id: '', turn: null, input_tokens: 250, output_tokens: 30, ttft_ms: 200 },
    ];

    const telemetry = makeInteractiveTelemetry(multiTurnItems, 3);

    await withMountedDom(t, async () => Response.json({}), async ({ root, doc, clickButton }) => {
        await act(async () => {
            root.render(
                <RequestsTab
                    task={makeRichTask({ simulation: { duration_seconds: 12, backend_options: { warmup_seconds: 0 } } })}
                    telemetry={telemetry}
                    analysisWindow="full"
                    onOpenErrorInspector={() => {}}
                />,
            );
        });

        const getSvg = () => findElements(
            doc.body,
            (el) => el.tagName === 'SVG' && /concurrent slot swimlane/.test(el.getAttribute('aria-label') || ''),
        )[0];
        const getBars = (svg) => findElements(svg, (el) => el.tagName === 'RECT' && typeof reactProps(el).onMouseEnter === 'function');
        const getSelectionPill = () => findElements(doc.body, (el) => el.getAttribute('data-testid') === 'selected-session-pill')[0] || null;
        const stopEvt = () => ({ stopPropagation() {}, preventDefault() {} });

        const svg = getSvg();
        assert.ok(svg, 'Expected SlotSwimlaneChart SVG');

        // Sessionless bars omit data-session-id, role, and aria-pressed; sessioned bars expose a11y attributes
        const initialBars = getBars(svg);
        assert.equal(initialBars.length, 6);
        const multiBarsBefore = initialBars.filter((el) => el.getAttribute('data-session-id') === 'sess-multi');
        assert.equal(multiBarsBefore.length, 3);
        assert.equal(multiBarsBefore[0].getAttribute('role'), 'button');
        assert.equal(multiBarsBefore[0].getAttribute('aria-pressed'), 'false');
        const sessionlessBarsBefore = initialBars.filter((el) => el.getAttribute('data-session-id') === null);
        assert.equal(sessionlessBarsBefore.length, 2);
        assert.equal(sessionlessBarsBefore[0].getAttribute('role'), null);
        assert.equal(sessionlessBarsBefore[0].getAttribute('aria-pressed'), null);

        // Hovering a multi-turn bar before selection shows "Turn k of M" and "Click to highlight all M turns"
        await act(async () => {
            reactProps(multiBarsBefore[1]).onMouseEnter({ clientX: 200, clientY: 90 });
        });
        const preSelectTooltip = findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip'))[0];
        assert.ok(preSelectTooltip);
        assert.match(preSelectTooltip.textContent, /sess-multi · Turn 2 of 3/);
        assert.match(preSelectTooltip.textContent, /Click to highlight all 3 turns/);

        // Clicking a sessionless bar when nothing is selected never selects sessionless bars
        await act(async () => {
            reactProps(sessionlessBarsBefore[0]).onClick(stopEvt());
        });
        assert.equal(getSelectionPill(), null);
        assert.equal(findElements(svg, (el) => el.hasAttribute('data-selected-session')).length, 0);

        // 1. Click Turn 1 of sess-multi -> highlights all 3 turns, dims others, renders connectors + pill
        await act(async () => {
            reactProps(multiBarsBefore[0]).onClick(stopEvt());
        });

        const selectedGroup = findElements(svg, (el) => el.getAttribute('data-selected-session') === 'sess-multi')[0];
        assert.ok(selectedGroup, 'Expected top-layer <g data-selected-session="sess-multi">');
        const highlightedBars = findElements(selectedGroup, (el) => el.tagName === 'RECT' && typeof reactProps(el).onMouseEnter === 'function');
        assert.equal(highlightedBars.length, 3);
        for (const bar of highlightedBars) {
            assert.equal(bar.getAttribute('fill-opacity'), '1');
            assert.equal(bar.getAttribute('stroke'), 'var(--bg-card)');
            assert.equal(bar.getAttribute('stroke-width'), '0.75');
            assert.equal(bar.getAttribute('aria-pressed'), 'true');
            assert.ok(Number(bar.getAttribute('width')) >= 4, `Expected highlighted bar width >= 4, got ${bar.getAttribute('width')}`);
        }
        const haloRects = findElements(selectedGroup, (el) => el.tagName === 'RECT' && el.getAttribute('fill') === 'none');
        assert.equal(haloRects.length, 3);
        for (const halo of haloRects) {
            assert.equal(halo.getAttribute('stroke'), 'var(--text-primary)');
            assert.equal(halo.getAttribute('stroke-opacity'), '0.95');
            assert.equal(halo.getAttribute('stroke-width'), '1.75');
            assert.equal(halo.getAttribute('pointer-events'), 'none');
        }

        const connectorsGroup = findElements(svg, (el) => el.getAttribute('data-session-connectors') === 'sess-multi')[0];
        assert.ok(connectorsGroup, 'Expected <g data-session-connectors="sess-multi">');
        assert.equal(connectorsGroup.getAttribute('pointer-events'), 'none');
        const connectorLines = findElements(connectorsGroup, (el) => el.tagName === 'LINE');
        assert.equal(connectorLines.length, 2);
        for (const line of connectorLines) {
            assert.equal(line.getAttribute('stroke'), 'var(--text-primary)');
            assert.equal(line.getAttribute('stroke-opacity'), '0.55');
            assert.equal(line.getAttribute('stroke-width'), '1.25');
            assert.equal(line.getAttribute('stroke-dasharray'), '3 3');
        }
        // Verify connector endpoints join endX of turn k to startX of turn k+1
        assert.equal(
            Number(connectorLines[0].getAttribute('x1')),
            Number(highlightedBars[0].getAttribute('x')) + Number(highlightedBars[0].getAttribute('width')),
        );
        assert.equal(Number(connectorLines[0].getAttribute('x2')), Number(highlightedBars[1].getAttribute('x')));

        const allBarsDuringSelection = getBars(svg);
        assert.equal(allBarsDuringSelection.length, 6);
        const nonMatchingBars = allBarsDuringSelection.filter((el) => el.getAttribute('data-session-id') !== 'sess-multi');
        assert.equal(nonMatchingBars.length, 3);
        for (const bar of nonMatchingBars) {
            assert.equal(bar.getAttribute('fill-opacity'), '0.16');
        }

        // Hovering a dimmed non-matching bar raises its opacity to 0.45
        await act(async () => {
            reactProps(nonMatchingBars[0]).onMouseEnter({ clientX: 160, clientY: 50 });
        });
        assert.equal(nonMatchingBars[0].getAttribute('fill-opacity'), '0.45');

        // Hovering a selected bar shows "Click again to clear highlight"
        await act(async () => {
            reactProps(highlightedBars[1]).onMouseEnter({ clientX: 200, clientY: 90 });
        });
        const selectedTooltip = findElements(doc.body, (el) => String(el.getAttribute('class') || '').includes('lens-chart-tooltip'))[0];
        assert.match(selectedTooltip.textContent, /Click again to clear highlight/);

        const pill = getSelectionPill();
        assert.ok(pill, 'Expected inline selection pill');
        assert.match(pill.textContent, /Selected session:\s*sess-multi\s*·\s*3 turns/);

        // 2a. Clicking a bar of the selected session toggles selection off
        await act(async () => {
            reactProps(highlightedBars[0]).onClick(stopEvt());
        });
        assert.equal(getSelectionPill(), null);
        assert.equal(findElements(svg, (el) => el.hasAttribute('data-selected-session')).length, 0);
        assert.equal(findElements(svg, (el) => el.hasAttribute('data-session-connectors')).length, 0);

        // 2b. Re-select, then clicking plot background <rect> clears selection
        const reBars1 = getBars(svg);
        await act(async () => {
            reactProps(reBars1.find((el) => el.getAttribute('data-session-id') === 'sess-multi')).onClick(stopEvt());
        });
        assert.ok(getSelectionPill());
        const plotBgRect = findElements(
            svg,
            (el) => el.tagName === 'RECT' && el.getAttribute('fill') === 'var(--bg-card)' && typeof reactProps(el).onClick === 'function',
        )[0];
        assert.ok(plotBgRect, 'Expected plot background <rect> with onClick handler');
        await act(async () => {
            reactProps(plotBgRect).onClick({});
        });
        assert.equal(getSelectionPill(), null);

        // 2c. Re-select, then clicking <svg> clears selection
        const reBars2 = getBars(svg);
        await act(async () => {
            reactProps(reBars2.find((el) => el.getAttribute('data-session-id') === 'sess-multi')).onClick(stopEvt());
        });
        assert.ok(getSelectionPill());
        await act(async () => {
            reactProps(svg).onClick({});
        });
        assert.equal(getSelectionPill(), null);

        // 2d. Re-select, then clicking Clear button clears selection
        const reBars3 = getBars(svg);
        await act(async () => {
            reactProps(reBars3.find((el) => el.getAttribute('data-session-id') === 'sess-multi')).onClick(stopEvt());
        });
        assert.ok(getSelectionPill());
        await clickButton(/^Clear$/);
        assert.equal(getSelectionPill(), null);

        // 2e. Re-select, then pressing Escape on chart container clears selection
        const reBars4 = getBars(svg);
        await act(async () => {
            reactProps(reBars4.find((el) => el.getAttribute('data-session-id') === 'sess-multi')).onClick(stopEvt());
        });
        assert.ok(getSelectionPill());
        const chartWrapper = findElements(doc.body, (el) => el.tagName === 'DIV' && typeof reactProps(el).onKeyDown === 'function')[0];
        assert.ok(chartWrapper, 'Expected focusable chart wrapper with onKeyDown');
        await act(async () => {
            reactProps(chartWrapper).onKeyDown({ key: 'Escape', preventDefault() {} });
        });
        assert.equal(getSelectionPill(), null);

        // 2f. Re-select, then clicking a sessionless bar clears active selection
        const reBars5 = getBars(svg);
        await act(async () => {
            reactProps(reBars5.find((el) => el.getAttribute('data-session-id') === 'sess-multi')).onClick(stopEvt());
        });
        assert.ok(getSelectionPill());
        const sessionlessNow = getBars(svg).find((el) => el.getAttribute('data-session-id') === null);
        await act(async () => {
            reactProps(sessionlessNow).onClick(stopEvt());
        });
        assert.equal(getSelectionPill(), null);

        // 3. Switching runs (items no longer contain selectedSessionId) resolves activeSessionId to null automatically
        const reBars6 = getBars(svg);
        await act(async () => {
            reactProps(reBars6.find((el) => el.getAttribute('data-session-id') === 'sess-multi')).onClick(stopEvt());
        });
        assert.ok(getSelectionPill());
        const switchedTelemetry = makeInteractiveTelemetry([
            { slot: 0, start_s: 0.5, end_s: 3.0, cache_hit_pct: 10.0, tier: 'partial', session_id: 'sess-new', turn: 1 },
        ], 1);
        await act(async () => {
            root.render(
                <RequestsTab
                    task={makeRichTask({ id: 'sim-post-2', simulation: { duration_seconds: 12, backend_options: { warmup_seconds: 0 } } })}
                    telemetry={switchedTelemetry}
                    analysisWindow="full"
                    onOpenErrorInspector={() => {}}
                />,
            );
        });
        assert.equal(getSelectionPill(), null);
        assert.equal(findElements(getSvg(), (el) => el.hasAttribute('data-selected-session')).length, 0);
    });
});

test('HardwareTab renders multi-node P/D inventory, aggregated fallback, KV cache hierarchy, external/legacy graceful EmptyState, and zero nested buttons (t134)', () => {
    const pdTask = makeRichTask({
        endpoint_mode: 'deployment',
        endpoint_cluster_name: 'cluster-alpha',
        endpoint_namespace: 'llm-d-pd',
        endpoint_deployment_name: 'qwen-pd',
        model_name: 'Qwen/Qwen3-32B',
        hardware: 'Intel Data Center Max 1550',
    });
    const pdTelemetry = {
        run_manifest: {
            target: {
                endpoint_mode: 'deployment',
                cluster_name: 'cluster-alpha',
                namespace: 'llm-d-pd',
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
            status: 'available',
            available: true,
            topology_mode: 'disaggregated',
            guide_type: 'pd-disaggregation',
            namespace: 'llm-d-pd',
            accelerator_summary: 'Intel Data Center Max 1550, NVIDIA H100 80GB',
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
            per_node: [
                {
                    node: 'node-prefill-1',
                    roles: ['prefill'],
                    pod_count: 1,
                    pods: ['qwen-prefill-0'],
                    allocated_accelerators: 4,
                    accelerator_capacity: 8,
                    accelerator_allocatable: 8,
                    accelerator_model: 'Intel Data Center Max 1550',
                    gpu_memory_total_bytes: 512 * 1024 ** 3,
                    cpu_capacity_cores: 128,
                    cpu_allocatable_cores: 124.5,
                    memory_capacity_bytes: 1024 * 1024 ** 3,
                    filesystem_size_bytes: 4 * 1024 ** 4,
                    architecture: 'amd64',
                    os_image: 'Ubuntu 24.04 LTS',
                    kernel_version: '6.8.0-45-generic',
                    kubelet_version: 'v1.31.2',
                    container_runtime: 'containerd://1.7.20',
                    network_interfaces: [
                        { device: 'eth0' },
                        { device: 'mlx5_0' },
                        { device: 'mlx5_1' },
                        { device: 'mlx5_2' },
                        { device: 'mlx5_3' },
                    ],
                },
                {
                    node: 'node-decode-2',
                    roles: ['decode'],
                    pod_count: 1,
                    pods: ['qwen-decode-0'],
                    allocated_accelerators: 2,
                    accelerator_capacity: 4,
                    accelerator_allocatable: 4,
                    accelerator_model: 'NVIDIA H100 80GB',
                    gpu_memory_total_bytes: 160 * 1024 ** 3,
                    cpu_capacity_cores: 64,
                    cpu_allocatable_cores: 62,
                    memory_total_bytes: 512 * 1024 ** 3,
                    filesystem_avail_bytes: 1500 * 1024 ** 3,
                    architecture: 'arm64',
                    network_interfaces: [{ device: 'eno1' }],
                },
            ],
            per_pod: [
                {
                    pod: 'qwen-prefill-0',
                    node: 'node-prefill-1',
                    role: 'prefill',
                    allocated_accelerators: 4,
                    cpu_request_cores: 16,
                    cpu_limit_cores: 32,
                    memory_request_bytes: 128 * 1024 ** 3,
                    memory_limit_bytes: 256 * 1024 ** 3,
                    gpu_memory_total_bytes: { mean: 512 * 1024 ** 3 },
                },
                {
                    pod: 'qwen-decode-0',
                    node: 'node-decode-2',
                    role: 'decode',
                    allocated_accelerators: 2,
                    cpu_request_cores: 8,
                    cpu_limit_cores: 16,
                    memory_request_bytes: 64 * 1024 ** 3,
                    memory_limit_bytes: 128 * 1024 ** 3,
                    gpu_memory_total_bytes: { mean: 160 * 1024 ** 3 },
                },
                {
                    pod: 'epp-router-0',
                    role: 'epp',
                    cpu_request_cores: 1,
                    cpu_limit_cores: 2,
                    memory_request_bytes: 2 * 1024 ** 3,
                    memory_limit_bytes: 4 * 1024 ** 3,
                },
            ],
            cache_config: {
                hbm_capacity_bytes: 672 * 1024 ** 3,
                hbm_capacity_tokens: 250000,
                prefill_hbm_capacity_tokens: 100000,
                decode_hbm_capacity_tokens: 150000,
                cpu_capacity_bytes: 512 * 1024 ** 3,
                cpu_capacity_tokens: 120000,
                nvme_capacity_bytes: 2 * 1024 ** 4,
                effective_capacity_tokens: 370000,
                block_size: 16,
                num_gpu_blocks: 15625,
                kv_cache_max_concurrency: 12.5,
                host_kv_configured: true,
                nvme_kv_configured: true,
                engines: [{ pod: 'qwen-prefill-0' }, { pod: 'qwen-decode-0' }],
            },
        },
    };

    const pdHtml = renderToStaticMarkup(<HardwareTab task={pdTask} telemetry={pdTelemetry} onSelectTab={() => {}} />);
    assertNoNestedButtons(pdHtml, 'HardwareTab P/D multi-node');
    assert.match(pdHtml, /Hardware Overview/);
    assert.match(pdHtml, /cluster-alpha \/ llm-d-pd/);
    assert.match(pdHtml, /P\/D Disaggregation/);
    assert.match(pdHtml, /2 nodes · 3 pods/);
    assert.match(pdHtml, /Prefill: 1 · Decode: 1 · Router: 1/);
    assert.match(pdHtml, /6 \/ 12/);
    // Heterogeneous per-device memory (128 GiB vs 80 GiB) reports aggregate 672 GiB without fabricating a uniform per-device size
    assert.match(pdHtml, /672 GiB/);
    assert.doesNotMatch(pdHtml, /6 × 112 GiB/);
    assert.match(pdHtml, /192 cores/);
    assert.match(pdHtml, /186\.50 cores/);
    assert.match(pdHtml, /1\.50 TiB/);
    assert.match(pdHtml, /Node Hardware Specifications/);
    assert.match(pdHtml, /node-prefill-1/);
    assert.match(pdHtml, /node-decode-2/);
    assert.match(pdHtml, /Intel Data Center Max 1550/);
    assert.match(pdHtml, /NVIDIA H100 80GB/);
    assert.match(pdHtml, /\+1/); // 5 NICs -> 4 pills + "+1"
    assert.match(pdHtml, /Serving Pod Hardware Allocation/);
    assert.match(pdHtml, /qwen-prefill-0/);
    assert.match(pdHtml, /qwen-decode-0/);
    assert.match(pdHtml, /epp-router-0/);
    assert.match(pdHtml, /128 \/ 256 GiB/);
    assert.match(pdHtml, /KV Cache &amp; Memory Hierarchy/);
    assert.match(pdHtml, /Device HBM KV Capacity/);
    assert.match(pdHtml, /100,000 tokens/);
    assert.match(pdHtml, /150,000 tokens/);
    assert.match(pdHtml, /512 GiB/);
    assert.match(pdHtml, /2 TiB/);
    assert.match(pdHtml, /370,000 tokens/);

    // Verify clickable node jumps on both Section 2 (Node table) and Section 3 (Pod table)
    const selections = [];
    let pdTree = null;
    function CapturePdHardware() {
        pdTree = HardwareTab({
            task: pdTask,
            telemetry: pdTelemetry,
            onSelectTab: (tab, anchor, opts) => selections.push({ tab, anchor, opts }),
        });
        return null;
    }
    renderToStaticMarkup(<CapturePdHardware />);
    const nodeButtons = findAllElements(
        pdTree,
        (el) => typeof el.type === 'object' || typeof el.type === 'function'
            ? el.props?.variant === 'link' && (el.props?.children === 'node-prefill-1' || el.props?.children === 'node-decode-2')
            : false,
    );
    assert.equal(nodeButtons.length, 4, 'Expected 2 node buttons in Node table + 2 node buttons in Pod table');
    nodeButtons[0].props.onClick();
    nodeButtons[3].props.onClick();
    assert.deepEqual(selections, [
        { tab: 'system', anchor: null, opts: { node: 'node-prefill-1' } },
        { tab: 'system', anchor: null, opts: { node: 'node-decode-2' } },
    ]);

    // Aggregated run without cache_config omits KV Cache Hierarchy
    const aggTelemetry = {
        run_manifest: { target: { endpoint_mode: 'deployment', cluster_name: 'c1', namespace: 'ns1', topology_mode: 'aggregated' } },
        server_telemetry: {
            status: 'available',
            available: true,
            topology_mode: 'aggregated',
            per_node: [{ node: 'worker-node-1', roles: ['worker'], pods: ['w-0'], allocated_accelerators: 2 }],
            per_pod: [{ pod: 'w-0', node: 'worker-node-1', role: 'worker', allocated_accelerators: 2 }],
        },
    };
    const aggHtml = renderToStaticMarkup(<HardwareTab task={{ endpoint_mode: 'deployment' }} telemetry={aggTelemetry} onSelectTab={() => {}} />);
    assertNoNestedButtons(aggHtml, 'HardwareTab aggregated');
    assert.match(aggHtml, /Aggregated/);
    assert.match(aggHtml, /Worker: 1/);
    assert.doesNotMatch(aggHtml, /KV Cache &amp; Memory Hierarchy/);

    // External endpoint fallback preserves target/hardware metadata and renders EmptyState
    const externalHtml = renderToStaticMarkup(
        <HardwareTab
            task={{
                endpoint_mode: 'external',
                endpoint_url: 'https://api.example.com/v1',
                model_name: 'Llama-3.3-70B',
                hardware: '8x H100',
            }}
            telemetry={{ server_telemetry: { available: false, status: 'unavailable' } }}
            onSelectTab={() => {}}
        />,
    );
    assertNoNestedButtons(externalHtml, 'HardwareTab external fallback');
    assert.match(externalHtml, /External Endpoint/);
    assert.match(externalHtml, /https:\/\/api\.example\.com\/v1/);
    assert.match(externalHtml, /Llama-3\.3-70B/);
    assert.match(externalHtml, /8x H100/);
    assert.match(externalHtml, /Node &amp; pod hardware inventory unavailable/);
    assert.match(externalHtml, /external inference endpoint/);

    // Legacy v10 snapshot without per_node/per_pod/cache_config degrades gracefully
    const legacyHtml = renderToStaticMarkup(
        <HardwareTab
            task={{ endpoint_mode: 'deployment', endpoint_cluster_name: 'legacy-c', endpoint_namespace: 'ns' }}
            telemetry={{ server_telemetry: { status: 'available', available: true } }}
            onSelectTab={() => {}}
        />,
    );
    assertNoNestedButtons(legacyHtml, 'HardwareTab legacy fallback');
    assert.match(legacyHtml, /legacy-c \/ ns/);
    assert.match(legacyHtml, /Node &amp; pod hardware inventory unavailable/);
    assert.equal(resolveCacheHierarchy({ cache_config: {} }), null);
    assert.equal(resolveHardwareSummary({}, {}).acceleratorModel, 'Not reported');

    // Edge cases: homogeneous device memory, partial device memory, EPP-only 0 serving pods, fallback nodes without pods, single-node missing model, singular core
    const homogeneousSummary = resolveHardwareSummary(
        { endpoint_cluster_name: 'c-homog' },
        {
            server_telemetry: {
                per_node: [
                    { node: 'n1', allocated_accelerators: 4, gpu_memory_total_bytes: 320 * 1024 ** 3, cpu_capacity_cores: 1 },
                    { node: 'n2', allocated_accelerators: 4, gpu_memory_total_bytes: 320 * 1024 ** 3 },
                ],
            },
        },
    );
    assert.equal(homogeneousSummary.isDeployment, true);
    assert.equal(homogeneousSummary.uniformDeviceMemCount, 8);
    const homogHtml = renderToStaticMarkup(
        <HardwareTab
            task={{ endpoint_cluster_name: 'c-homog' }}
            telemetry={{
                server_telemetry: {
                    per_node: [
                        { node: 'n1', allocated_accelerators: 4, gpu_memory_total_bytes: 320 * 1024 ** 3, cpu_capacity_cores: 1 },
                        { node: 'n2', allocated_accelerators: 4, gpu_memory_total_bytes: 320 * 1024 ** 3 },
                    ],
                },
            }}
            onSelectTab={() => {}}
        />,
    );
    assert.match(homogHtml, /8 × 80 GiB = 640 GiB/);
    assert.match(homogHtml, /1 core/);

    // Partial device memory: n1 reports 320 GiB for 4 devices, n2 has 4 devices with missing memory -> must NOT print 8 × 40 GiB
    const partialMemHtml = renderToStaticMarkup(
        <HardwareTab
            task={{ endpoint_mode: 'deployment' }}
            telemetry={{
                server_telemetry: {
                    per_node: [
                        { node: 'n1', allocated_accelerators: 4, gpu_memory_total_bytes: 320 * 1024 ** 3 },
                        { node: 'n2', allocated_accelerators: 4 },
                    ],
                },
            }}
            onSelectTab={() => {}}
        />,
    );
    assert.match(partialMemHtml, /320 GiB/);
    assert.doesNotMatch(partialMemHtml, /8 × 40 GiB/);

    // EPP-only run preserves Serving Pods: 0; fallback serverTelemetry.nodes omits "0 pods"; single-node missing model shows "—"
    const eppFallbackHtml = renderToStaticMarkup(
        <HardwareTab
            task={{ endpoint_mode: 'deployment' }}
            telemetry={{
                server_telemetry: {
                    nodes: ['fallback-node-1'],
                    per_pod: [{ pod: 'epp-only-0', role: 'epp' }],
                },
            }}
            onSelectTab={() => {}}
        />,
    );
    assertNoNestedButtons(eppFallbackHtml, 'HardwareTab EPP-only + fallback node');
    assert.match(eppFallbackHtml, /Router: 1/);
    assert.match(eppFallbackHtml, /Serving Pods:.*?>0</);
    assert.match(eppFallbackHtml, /fallback-node-1/);
    assert.doesNotMatch(eppFallbackHtml, /0 pods/);
    assert.doesNotMatch(eppFallbackHtml, /<td[^>]*>[\s\S]*Not reported[\s\S]*<\/td>/);
});

test('SimulationTaskDetails wires Hardware tab and jumps from node link to scoped SystemTab and resets on direct tab click (t134)', async (t) => {
    const task = makeRichTask({
        id: 'sim-hw-1',
        name: 'hw-run',
        endpoint_mode: 'deployment',
        endpoint_cluster_name: 'cluster-hw',
        endpoint_namespace: 'ns-hw',
        model_name: 'Qwen/Qwen3-8B',
    });
    const telemetry = makeRichTelemetry({
        server_telemetry: {
            ...makeRichTelemetry().server_telemetry,
            nodes: ['node-a', 'node-b'],
            host_metrics: {
                ...makeRichTelemetry().server_telemetry.host_metrics,
                nodes: ['node-a', 'node-b'],
                per_node: [
                    {
                        node: 'node-a',
                        roles: ['prefill'],
                        pods: ['qwen-prefill-0'],
                        allocated_accelerators: 1,
                        accelerator_capacity: 2,
                        accelerator_model: 'Device A',
                        cpu_capacity_cores: 32,
                        memory_capacity_bytes: 128 * 1024 ** 3,
                    },
                    {
                        node: 'node-b',
                        roles: ['decode'],
                        pods: ['qwen-decode-0'],
                        allocated_accelerators: 1,
                        accelerator_capacity: 2,
                        accelerator_model: 'Device B',
                        cpu_capacity_cores: 32,
                        memory_capacity_bytes: 128 * 1024 ** 3,
                    },
                ],
            },
            per_node: [
                {
                    node: 'node-a',
                    roles: ['prefill'],
                    pods: ['qwen-prefill-0'],
                    allocated_accelerators: 1,
                    accelerator_capacity: 2,
                    accelerator_model: 'Device A',
                    cpu_capacity_cores: 32,
                    memory_capacity_bytes: 128 * 1024 ** 3,
                },
                {
                    node: 'node-b',
                    roles: ['decode'],
                    pods: ['qwen-decode-0'],
                    allocated_accelerators: 1,
                    accelerator_capacity: 2,
                    accelerator_model: 'Device B',
                    cpu_capacity_cores: 32,
                    memory_capacity_bytes: 128 * 1024 ** 3,
                },
            ],
        },
    });

    const prevLocation = globalThis.window?.location;
    const prevHistory = globalThis.window?.history;

    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/api/v1/simulation/tasks/sim-hw-1/telemetry')) {
                return Response.json(telemetry);
            }
            if (url.includes('/api/v1/simulation/tasks/sim-hw-1/artifacts')) {
                return Response.json({ artifacts: [] });
            }
            if (url.includes('/api/v1/simulation/tasks/sim-hw-1')) {
                return Response.json({ task });
            }
            return Response.json({ items: [] });
        },
        async ({ root, doc, clickButton, flush }) => {
            const origCreateElement = doc.createElement.bind(doc);
            doc.createElement = (tag) => {
                const node = origCreateElement(tag);
                node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
                return node;
            };
            globalThis.window.location = new URL('http://localhost/?taskId=sim-hw-1');
            globalThis.window.history = {
                replaceState(_state, _title, nextUrl) {
                    globalThis.window.location = new URL(nextUrl, 'http://localhost');
                },
            };
            t.after(() => {
                if (prevLocation !== undefined && globalThis.window) globalThis.window.location = prevLocation;
                if (prevHistory !== undefined && globalThis.window) globalThis.window.history = prevHistory;
            });

            await act(async () => {
                root.render(<SimulationTaskDetails onNavigate={() => {}} />);
            });
            await flush();
            await flush();

            await clickButton(/^Hardware$/);
            await flush();
            assert.match(doc.body.textContent, /Hardware Overview/);
            assert.match(doc.body.textContent, /Node Hardware Specifications/);
            assert.match(doc.body.textContent, /Device B/);

            // Clicking node-b jumps to SystemTab scoped to node-b
            await clickButton(/^node-b$/);
            await flush();
            assert.match(doc.body.textContent, /Host CPU Utilization/);
            const nodeBBadgeButton = findElements(
                doc.body,
                (el) => el.tagName === 'BUTTON' && el.getAttribute('aria-pressed') === 'true' && /node-b/.test(el.textContent),
            )[0];
            assert.ok(nodeBBadgeButton, 'Expected SystemTab node-b chip to be active after jump from HardwareTab');

            // Clicking Hardware then System directly resets scope back to All Nodes (Aggregate)
            await clickButton(/^Hardware$/);
            await flush();
            await clickButton(/^System$/);
            await flush();
            const allNodesButton = findElements(
                doc.body,
                (el) => el.tagName === 'BUTTON' && el.getAttribute('aria-pressed') === 'true' && /All Nodes \(Aggregate\)/.test(el.textContent),
            )[0];
            assert.ok(allNodesButton, 'Expected direct click on System tab to default to All Nodes (Aggregate)');
        },
    );
});

test('sanitizeEndpointUrl and HardwareTab/SimulationTaskDetails strip credentials, query params, path params, and fragments', () => {
    assert.equal(
        sanitizeEndpointUrl('https://user:supersecret@llm.example.com:8443/v1;jsessionid=tok123?api_key=secret&sig=123#frag'),
        'https://llm.example.com:8443/v1',
    );
    assert.equal(
        sanitizeEndpointUrl('https://user:supersecret@llm.example.com:badport/v1;jsessionid=tok123?api_key=secret#frag'),
        'https://llm.example.com:badport/v1',
    );
    assert.equal(
        sanitizeEndpointUrl('user:supersecret@llm.example.com:8443/v1;p=1?api_key=secret&sig=123#frag'),
        'llm.example.com:8443/v1',
    );
    assert.equal(
        sanitizeEndpointUrl('llm.example.com/v1?sig=123#frag'),
        'llm.example.com/v1',
    );
    assert.equal(sanitizeEndpointUrl('   '), null);
    assert.equal(sanitizeEndpointUrl(null), null);

    const leakyUrl = 'https://user:supersecret@api.example.com:8443/v1;sess=abc?api_key=secret&sig=123#frag';
    const hwMarkup = renderToStaticMarkup(
        <HardwareTab
            task={{
                endpoint_mode: 'external',
                endpoint_url: leakyUrl,
                model_name: 'Llama-3.3-70B',
            }}
            telemetry={{
                run_manifest: {
                    target: {
                        sanitized_endpoint_url: 'user:leak@api.example.com/v1?sig=leak#frag',
                    },
                },
                server_telemetry: { available: false, status: 'unavailable' },
            }}
            onSelectTab={() => {}}
        />,
    );
    assert.match(hwMarkup, /api\.example\.com\/v1/);
    assert.doesNotMatch(hwMarkup, /supersecret|user:leak|api_key|sig=leak|jsessionid|sess=abc|#frag/);

    const fallbackHwMarkup = renderToStaticMarkup(
        <HardwareTab
            task={{
                endpoint_mode: 'external',
                endpoint_url: leakyUrl,
                model_name: 'Llama-3.3-70B',
            }}
            telemetry={{ server_telemetry: { available: false, status: 'unavailable' } }}
            onSelectTab={() => {}}
        />,
    );
    assert.match(fallbackHwMarkup, /https:\/\/api\.example\.com:8443\/v1/);
    assert.doesNotMatch(fallbackHwMarkup, /supersecret|api_key|sig=123|sess=abc|#frag/);
});
