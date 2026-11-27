import assert from 'node:assert/strict';
import test from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { ErrorInspectorModal, LiveMonitorPanel } from './LiveMonitorPanel';
import { ReferenceLine, YAxis } from 'recharts';
import { CHART_SERIES, ChartLegend } from '../../../../shared/ui';
import { ConfigurationGroup } from './ConfigurationGroup';
import { DetailCard } from './DetailCard';
import { StatisticsPanel } from './StatisticsPanel';
import { RunCompareDrawer } from './RunCompareDrawer';

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
        getBoundingClientRect() {
            return { top: 0, left: 0, width: 640, height: 320, right: 640, bottom: 320 };
        },
        focus() {},
        blur() {},
    });
    const win = {
        addEventListener() {},
        removeEventListener() {},
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        requestAnimationFrame: (callback) => setTimeout(callback, 0),
        cancelAnimationFrame: clearTimeout,
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
    return { doc, win };
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

    const { doc, win } = createTestDom();
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    globalThis.document = doc;
    globalThis.window = win;
    globalThis.HTMLElement = win.HTMLElement;
    globalThis.SVGElement = win.SVGElement;
    globalThis.fetch = fetchHandler;
    globalThis.ResizeObserver = class ResizeObserver {
        observe() {}
        unobserve() {}
        disconnect() {}
    };

    const container = doc.createElement('div');
    doc.body.appendChild(container);
    const root = createRoot(container);

    t.after(async () => {
        await act(async () => { root.unmount(); });
        globalThis.IS_REACT_ACT_ENVIRONMENT = prevAct;
        if (prevDoc === undefined) delete globalThis.document; else globalThis.document = prevDoc;
        if (prevWin === undefined) delete globalThis.window; else globalThis.window = prevWin;
        if (prevHtml === undefined) delete globalThis.HTMLElement; else globalThis.HTMLElement = prevHtml;
        if (prevSvg === undefined) delete globalThis.SVGElement; else globalThis.SVGElement = prevSvg;
        globalThis.fetch = prevFetch;
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

function makeTask(overrides = {}) {
    return {
        id: 'sim-task-1',
        status: 'running',
        execution_started_at: '2026-01-01T00:00:00Z',
        progress_percent: 42,
        progress_message: 'Replaying trace requests',
        endpoint_mode: 'deployment',
        endpoint_url: 'http://llm-gateway.local/v1',
        model_name: 'Qwen/Qwen3-8B',
        scenario: 'trace_replay',
        simulation: {
            duration_seconds: 120,
            stream: true,
            backend_options: {
                warmup_seconds: 15,
                ttft_slo: 0.5,
                tpot_slo: 0.05,
                max_concurrency: 32,
            },
        },
        prompt: { trace: { format: 'BailianTrace', dataset_name: 'bailian-2026' } },
        ...overrides,
    };
}

function makeLiveTelemetry(overrides = {}) {
    return {
        live_phase: 'Profiling',
        elapsed_seconds: 50,
        duration_seconds: 120,
        warmup_seconds: 15,
        eta_seconds: 70,
        progress: {
            percent: 42,
            dispatched: 120,
            completed: 100,
            failed: 7,
            in_flight: 13,
        },
        fidelity: {
            status: 'on_schedule',
            drift_p90_ms: 4.2,
        },
        telemetry_status: {
            prometheus_available: true,
            accelerator_available: true,
            topology_mode: 'disaggregated',
            accelerator_label: 'XPU',
        },
        kpis: {
            throughput_rps: 12.5,
            output_tok_per_s: 640.2,
            ttft_p50_ms: 145,
            ttft_p90_ms: 320,
            tpot_p50_ms: 18,
            tpot_p90_ms: 34,
            error_rate_pct: 5.8,
            rolling_error_rate_pct: 8.2,
            error_counts: { all: 7, '4xx': 1, '5xx': 4, timeout: 1, transport: 1 },
            server_running: 10,
            server_waiting: 3,
            epp_running: 13,
            kv_cache_peak_pct: 68.5,
            prefix_cache_hit_pct: 44.0,
            accelerator_util_pct: 79.4,
            accelerator_power_watts: 312,
            accelerator_label: 'XPU',
        },
        alerts: [
            { code: 'QUEUE_SATURATION', severity: 'warning', message: 'Waiting queue exceeded 10 requests' },
            { code: 'ERROR_BURST', severity: 'critical', message: 'Rolling 15s error rate 8.2%' },
            { code: 'KV_EVICTION_RISK', severity: 'warning', message: 'KV cache pressure rising' },
            { code: 'SCHEDULE_DRIFT', severity: 'warning', message: 'Replay drift p90 > 50 ms' },
        ],
        target_deployments: [
            { execution_id: 'exec-1', name: 'qwen-prefill', namespace: 'ns-a', cluster_id: 'cluster-1', status: 'ready' },
            { execution_id: 'exec-2', name: 'qwen-decode', namespace: 'ns-a', cluster_id: 'cluster-1', status: 'ready' },
        ],
        recent_logs: ['[INFO] Replay started', '[ERROR] HTTP 503 upstream overloaded'],
        ...overrides,
    };
}

test('LiveMonitorPanel renders all phases, fidelity badges, health ticker alerts, and dynamic hardware label', () => {
    for (const phase of ['Preparing', 'Warmup', 'Profiling', 'Cooldown', 'Collecting Telemetry']) {
        const html = renderToStaticMarkup(
            <LiveMonitorPanel
                task={makeTask()}
                liveTelemetry={makeLiveTelemetry({ live_phase: phase })}
            />,
        );
        assert.match(html, new RegExp(phase));
    }

    const html = renderToStaticMarkup(
        <LiveMonitorPanel
            task={makeTask()}
            liveTelemetry={makeLiveTelemetry({
                fidelity: { status: 'lagging', drift_p90_ms: 185.4 },
            })}
        />,
    );
    assert.match(html, /Replay Monitor/);
    assert.match(html, /Live Monitoring/);
    assert.match(html, /Logs &amp; Configuration/);
    assert.match(html, /Lagging \(P90 \+185\.40 ms\)/);
    assert.match(html, /Running &amp; Waiting Requests/);
    assert.match(html, /KV Cache Usage &amp; Prefix Cache Hit Rate/);
    assert.match(html, /vLLM Engine &amp; EPP Load/);
    assert.match(html, /Output Throughput/);
    assert.match(html, /EPP In-Flight Requests/);
    assert.match(html, /Failed: 7/);
    assert.match(html, /ERROR_BURST/);
    assert.match(html, /QUEUE_SATURATION/);
    assert.match(html, /KV_EVICTION_RISK/);
    assert.match(html, /SCHEDULE_DRIFT/);
    assert.match(html, /XPU OK/);
    assert.match(html, /XPU Util &amp; Power/);
    assert.doesNotMatch(html, /NVIDIA|H100|A100/);

    const gpuHtml = renderToStaticMarkup(
        <LiveMonitorPanel
            task={makeTask()}
            liveTelemetry={makeLiveTelemetry({
                telemetry_status: { prometheus_available: true, accelerator_available: true },
                kpis: { ...makeLiveTelemetry().kpis, accelerator_label: 'GPU' },
            })}
        />,
    );
    assert.match(gpuHtml, /GPU OK/);
    assert.match(gpuHtml, /GPU Util &amp; Power/);

    const fallbackHtml = renderToStaticMarkup(
        <LiveMonitorPanel
            task={makeTask()}
            liveTelemetry={makeLiveTelemetry({
                telemetry_status: { prometheus_available: true, accelerator_available: false },
                kpis: { ...makeLiveTelemetry().kpis, accelerator_label: undefined },
            })}
        />,
    );
    assert.match(fallbackHtml, /Accelerator N\/A/);
    assert.match(fallbackHtml, /Accelerator Util &amp; Power/);
});

test('LiveMonitorPanel degrades gracefully when Prometheus is unavailable', () => {
    const html = renderToStaticMarkup(
        <LiveMonitorPanel
            task={makeTask()}
            liveTelemetry={makeLiveTelemetry({
                alerts: [],
                telemetry_status: {
                    prometheus_available: false,
                    accelerator_available: false,
                    accelerator_label: 'Accelerator',
                },
            })}
        />,
    );
    assert.match(html, /Prom Off/);
    assert.match(html, /Running &amp; Waiting Requests/);
    assert.match(html, /N\/A/);
    assert.match(html, /Client only/);
    assert.match(html, /Healthy/);
    assert.match(html, /No active bottleneck alerts/);
});

test('LiveMonitorPanel switches runtime tabs and routes Failed, ERROR_BURST, Error Rate, and sub-badges to ErrorInspectorModal', async (t) => {
    const openedFilters = [];
    await withMountedDom(
        t,
        async (url) => {
            if (url.includes('/api/v1/monitoring/deployments/exec-1')) {
                return Response.json({ enabled: false, status: 'disabled', message: 'Monitoring is currently disabled.' });
            }
            if (url.includes('/api/v1/monitoring/deployments/exec-2')) {
                return Response.json({ enabled: false, status: 'disabled', message: 'Decode monitoring disabled.' });
            }
            return Response.json({});
        },
        async ({ root, doc, clickButton, flush }) => {
            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask()}
                        liveTelemetry={makeLiveTelemetry()}
                        onOpenErrorInspector={(filter) => openedFilters.push(filter)}
                    />,
                );
            });

            await clickButton(/Failed: 7/);
            await clickButton(/Inspect errors/);
            await clickButton(/Error Rate \(Click to inspect\)/);
            await clickButton(/^4xx: 1$/);
            await clickButton(/^5xx: 4$/);
            await clickButton(/^Timeout: 1$/);
            await clickButton(/^Transport: 1$/);

            assert.deepEqual(openedFilters, ['all', 'all', 'all', '4xx', '5xx', 'timeout', 'transport']);

            // Switch to Live Monitoring sub-tab: multi-member dropdown + Enable monitoring / retry
            await clickButton(/^Live Monitoring$/);
            await flush();
            const select = findElements(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('aria-label') === 'Deployment execution')[0];
            assert.ok(select, 'Expected Deployment execution dropdown');
            assert.equal(select.options.length, 2);
            assert.match(doc.body.textContent, /Enable monitoring \/ retry/);

            // Switch to Logs & Configuration sub-tab
            await clickButton(/^Logs & Configuration$/);
            assert.match(doc.body.textContent, /Run Configuration/);
            assert.match(doc.body.textContent, /Qwen\/Qwen3-8B/);
            assert.match(doc.body.textContent, /BailianTrace/);
        },
    );
});

test('LiveMonitorPanel Live Monitoring sub-tab enables monitoring and renders clean empty state in external mode', async (t) => {
    const calls = [];
    let monitoringEnabled = false;
    await withMountedDom(
        t,
        async (url, options = {}) => {
            calls.push({ url, method: options.method || 'GET' });
            if (url.includes('/api/v1/monitoring/deployments/exec-1/enable') && options.method === 'POST') {
                monitoringEnabled = true;
                return Response.json({ enabled: true, status: 'enabled' });
            }
            if (url.includes('/api/v1/monitoring/deployments/exec-1')) {
                return Response.json(monitoringEnabled
                    ? { enabled: true, status: 'enabled' }
                    : { enabled: false, status: 'disabled', message: 'Enable ServiceMonitor to stream live topology.' });
            }
            if (url.includes('/api/v1/monitoring/')) {
                return Response.json({ components: [] });
            }
            return Response.json({});
        },
        async ({ root, doc, clickButton, flush }) => {
            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask()}
                        liveTelemetry={makeLiveTelemetry({
                            target_deployments: [
                                { execution_id: 'exec-1', name: 'qwen-prefill', namespace: 'ns-a', cluster_id: 'cluster-1', status: 'ready' },
                            ],
                        })}
                    />,
                );
            });
            await clickButton(/^Live Monitoring$/);
            await flush();
            assert.match(doc.body.textContent, /Monitoring disabled/);
            await clickButton(/Enable monitoring \/ retry/);
            await flush();
            assert.ok(calls.some((c) => c.url.includes('/enable') && c.method === 'POST'));
            assert.match(doc.body.textContent, /Monitoring enabled/);

            // External mode (no target_deployments)
            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask({ endpoint_mode: 'external', endpoint_deployment_execution_id: null })}
                        liveTelemetry={makeLiveTelemetry({ target_deployments: [] })}
                    />,
                );
            });
            assert.match(doc.body.textContent, /Live Deployment Flow Unavailable/);
            assert.match(doc.body.textContent, /Replay Monitor remains fully active/);
        },
    );
});

test('ErrorInspectorModal renders filter pills, live counts, redacted errors, and pauses auto-refresh off page 0', async (t) => {
    const requestedUrls = [];
    await withMountedDom(
        t,
        async (url) => {
            requestedUrls.push(url);
            const parsed = new URL(url, 'http://localhost');
            const offset = Number(parsed.searchParams.get('offset') || 0);
            const code = parsed.searchParams.get('status_code') || 'all';
            return Response.json({
                total: 75,
                counts_by_code: { all: 75, '4xx': 5, '5xx': 60, timeout: 6, transport: 4, 503: 45 },
                issues: [
                    {
                        request_number: offset + 1,
                        request_id: `req-${offset + 1}`,
                        started_at_seconds: 12.5,
                        status_code: code === '4xx' ? 429 : 503,
                        error: 'Authorization: [REDACTED] upstream timeout',
                        latency_ms: 420.5,
                        input_tokens: 512,
                        output_tokens: 64,
                    },
                ],
            });
        },
        async ({ root, doc, clickButton, flush }) => {
            await act(async () => {
                root.render(
                    <ErrorInspectorModal
                        isOpen
                        onClose={() => {}}
                        taskId="sim-task-1"
                        initialFilter="all"
                        isRunning
                        taskStartedAt="2026-01-01T00:00:00Z"
                    />,
                );
            });
            await flush();

            assert.match(doc.body.textContent, /Live Error Inspector/);
            assert.match(doc.body.textContent, /All Errors/);
            assert.match(doc.body.textContent, /\(75\)/);
            assert.match(doc.body.textContent, /5xx Server Errors/);
            assert.match(doc.body.textContent, /HTTP 503/);
            assert.match(doc.body.textContent, /Authorization: \[REDACTED\] upstream timeout/);
            assert.match(doc.body.textContent, /\+12\.50s/);
            assert.match(doc.body.textContent, /420\.50 ms/);
            assert.doesNotMatch(doc.body.textContent, /Paused on page/);

            // Click Next to go to page 2 (index 1) -> auto-refresh badge shows Paused on page 2
            await clickButton(/^Next/);
            await flush();
            assert.match(doc.body.textContent, /Paused on page 2/);
            assert.ok(requestedUrls.some((u) => u.includes('offset=50')));

            // Clicking a filter pill resets back to page 0 and fetches with status_code=4xx
            await clickButton(/4xx Client Errors/);
            await flush();
            assert.doesNotMatch(doc.body.textContent, /Paused on page 2/);
            assert.ok(requestedUrls.some((u) => u.includes('status_code=4xx') && u.includes('offset=0')));
        },
    );
});

function findReactElement(node, predicate) {
    if (!node) return undefined;
    if (Array.isArray(node)) {
        for (const item of node) {
            const match = findReactElement(item, predicate);
            if (match) return match;
        }
        return undefined;
    }
    if (typeof node !== 'object') return undefined;
    if (predicate(node)) return node;
    for (const value of [node.props?.actions, node.props?.children]) {
        const match = findReactElement(value, predicate);
        if (match) return match;
    }
    return undefined;
}

test('LiveMonitorPanel renders visible dots for single-point and sparse-null rolling_latency and throughput series', () => {
    // 1. Single completed bucket (bucket 0 empty/null, bucket 1 populated)
    let singleTree = null;
    function CaptureSingleLive() {
        singleTree = LiveMonitorPanel({
            task: makeTask(),
            liveTelemetry: makeLiveTelemetry({
                series: {
                    client_throughput: [
                        { t: 7.5, rps: 1.2, output_tok_s: 90, in_flight: 8, failed_rps: 0 },
                    ],
                    rolling_latency: [
                        { t: 2.5, ttft_p50_ms: null, ttft_p90_ms: null, tpot_p50_ms: null, tpot_p90_ms: null, drift_p90_ms: null },
                        { t: 7.5, ttft_p50_ms: 145, ttft_p90_ms: 310, tpot_p50_ms: 21, tpot_p90_ms: 38, drift_p90_ms: 4.5 },
                    ],
                    server_load: [
                        { t: 5.0, prefill_running: 4, prefill_waiting: 0, decode_running: 4, decode_waiting: 2, epp_running: 8 },
                    ],
                    kv_and_accelerator: [
                        { t: 5.0, kv_cache_peak_pct: 42, prefix_cache_hit_pct: 55, accelerator_util_pct: 70 },
                    ],
                },
            }),
        });
        return null;
    }
    renderToStaticMarkup(<CaptureSingleLive />);

    const chartsGrid = findReactElement(singleTree, (n) => typeof n.type === 'function' && n.type.name === 'LiveChartsGrid');
    assert.ok(chartsGrid, 'Expected LiveChartsGrid element');
    let gridTree = null;
    function CaptureSingleGrid() {
        gridTree = chartsGrid.type(chartsGrid.props);
        return null;
    }
    renderToStaticMarkup(<CaptureSingleGrid />);

    const rpsArea = findReactElement(gridTree, (n) => n.props?.dataKey === 'rps');
    const ttftP50Line = findReactElement(gridTree, (n) => n.props?.dataKey === 'ttft_p50_ms');
    const ttftP90Line = findReactElement(gridTree, (n) => n.props?.dataKey === 'ttft_p90_ms');
    const decodeRunLine = findReactElement(gridTree, (n) => n.props?.dataKey === 'decode_running');
    const kvPeakLine = findReactElement(gridTree, (n) => n.props?.dataKey === 'kv_cache_peak_pct');
    for (const el of [rpsArea, ttftP50Line, ttftP90Line, decodeRunLine, kvPeakLine]) {
        assert.ok(el, 'Expected live chart series element');
        assert.equal(el.props.connectNulls, true);
        assert.equal(typeof el.props.dot, 'object');
        assert.ok(el.props.dot.r > 0);
    }

    // 2. Sparse intermediate null bucket in rolling_latency -> dot function renders <circle> for isolated points
    let sparseTree = null;
    function CaptureSparseLive() {
        sparseTree = LiveMonitorPanel({
            task: makeTask(),
            liveTelemetry: makeLiveTelemetry({
                series: {
                    client_throughput: [
                        { t: 2.5, rps: 1.0, output_tok_s: 80, in_flight: 10, failed_rps: 0 },
                        { t: 7.5, rps: 1.5, output_tok_s: 120, in_flight: 6, failed_rps: 0 },
                    ],
                    rolling_latency: [
                        { t: 2.5, ttft_p50_ms: 120, ttft_p90_ms: 240 },
                        { t: 7.5, ttft_p50_ms: null, ttft_p90_ms: null },
                        { t: 12.5, ttft_p50_ms: 180, ttft_p90_ms: 360 },
                    ],
                    server_load: [],
                    kv_and_accelerator: [],
                },
            }),
        });
        return null;
    }
    renderToStaticMarkup(<CaptureSparseLive />);
    const sparseGrid = findReactElement(sparseTree, (n) => typeof n.type === 'function' && n.type.name === 'LiveChartsGrid');
    let sparseGridTree = null;
    function CaptureSparseGrid() {
        sparseGridTree = sparseGrid.type(sparseGrid.props);
        return null;
    }
    renderToStaticMarkup(<CaptureSparseGrid />);
    const sparseChart = findReactElement(sparseGridTree, (n) => Array.isArray(n.props?.data) && n.props.data.some((pt) => pt.ttft_p50_ms === 120));
    const sparseTtftLine = findReactElement(sparseGridTree, (n) => n.props?.dataKey === 'ttft_p50_ms');
    assert.equal(sparseTtftLine.props.connectNulls, true);
    assert.equal(typeof sparseTtftLine.props.dot, 'function');
    const dot0 = sparseTtftLine.props.dot({ cx: 50, cy: 40, value: 120, payload: sparseChart.props.data[0], index: 0 });
    const dot1 = sparseTtftLine.props.dot({ cx: 100, cy: null, value: null, payload: sparseChart.props.data[1], index: 1 });
    assert.equal(dot0?.type, 'circle');
    assert.equal(dot1, null);
});

test('LiveMonitorPanel renders role="tooltip" help explanations for phase/fidelity strip, all 6 KPI cards & details, 4 live charts, and zero nested buttons', async (t) => {
    await withMountedDom(
        t,
        async () => Response.json({
            total: 1,
            counts_by_code: { all: 1, '5xx': 1 },
            issues: [
                {
                    request_number: 1,
                    request_id: 'req-1',
                    started_at_seconds: 5.0,
                    status_code: 503,
                    error: 'upstream overloaded',
                    latency_ms: 120,
                    input_tokens: 64,
                    output_tokens: 12,
                },
            ],
        }),
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask()}
                        liveTelemetry={makeLiveTelemetry()}
                    />,
                );
            });

            const tooltips = findElements(doc.body, (el) => el.getAttribute('role') === 'tooltip');
            assert.ok(tooltips.length >= 25, `Expected >= 25 role="tooltip" nodes, got ${tooltips.length}`);
            const tooltipTexts = tooltips.map((el) => el.textContent).join('\n');
            const formulaWraps = findElements(
                doc.body,
                (el) => (el.getAttribute('class') || '').includes('lens-help-formula'),
            );
            assert.ok(formulaWraps.length >= 20, `Expected >= 20 .lens-help-formula blocks, got ${formulaWraps.length}`);
            assert.doesNotMatch(tooltipTexts, /\b(vllm:|sglang:|trtllm:|vllm\b)|--kv-cache-metrics-sample/i);
            assert.match(tooltipTexts, /Rolling_RPS = Samples_15s \/ Window_Span_s/);
            assert.match(tooltipTexts, /Drift_P90 \(ms\) = Quantile_0\.90/);
            assert.match(tooltipTexts, /Peak_KV_Usage \(%\) = max_pods/);

            // Phase & fidelity strip badges
            assert.match(tooltipTexts, /Preparing, Warmup \(excluded from steady-state KPIs\), Profiling/);
            assert.match(tooltipTexts, /Elapsed replay time \(seconds\)/);
            assert.match(tooltipTexts, /client dispatch drift \(ms\)/);
            assert.match(tooltipTexts, /5-second Prometheus scrapes/);
            assert.match(tooltipTexts, /live XPU utilization and power metrics/);
            assert.match(tooltipTexts, /Total requests sent so far/);
            assert.match(tooltipTexts, /requests that finished successfully/);
            assert.match(tooltipTexts, /awaiting completion on the client/);
            assert.match(tooltipTexts, /Total failed requests/);

            // All 6 KPI StatCards + detail rows
            assert.match(tooltipTexts, /Live request completion rate \(req\/s\)/);
            assert.match(tooltipTexts, /generated output tokens per second/i);
            assert.match(tooltipTexts, /Cumulative successful requests completed/);
            assert.match(tooltipTexts, /Median Time to First Token \(TTFT P50\)/);
            assert.match(tooltipTexts, /90th percentile Time to First Token/);
            assert.match(tooltipTexts, /Median and 90th percentile Time per Output Token/);
            assert.match(tooltipTexts, /Cumulative percentage of dispatched requests that failed/);
            assert.match(tooltipTexts, /Error percentage over the trailing 15-second window/);
            assert.match(tooltipTexts, /Live server-side request concurrency/);
            assert.match(tooltipTexts, /currently executing on the serving engine/);
            assert.match(tooltipTexts, /Endpoint Picker \(EPP\)/);
            assert.match(tooltipTexts, /Peak KV cache usage/);
            assert.match(tooltipTexts, /prefix KV cache over the trailing 30 seconds/);
            assert.match(tooltipTexts, /Active server telemetry source/);
            assert.match(tooltipTexts, /Real-time XPU compute utilization/);
            assert.match(tooltipTexts, /Instantaneous XPU power draw in watts/);
            assert.match(tooltipTexts, /Detected hardware accelerator profile label/);

            // All 4 Live 2x2 Streaming Charts
            assert.match(tooltipTexts, /15-second rolling request completion rate \(req\/s\), failed request rate/);
            assert.match(tooltipTexts, /15-second rolling P50 and P90 TTFT \(ms\) or TPOT \(ms\/tok\) latency/);
            assert.match(tooltipTexts, /server-side running and waiting request counts/);
            assert.match(tooltipTexts, /peak KV cache usage.*90% eviction risk line/i);

            // Verify zero nested <button> inside any <button> (especially clickable Error Rate card)
            const buttons = findElements(doc.body, (el) => el.tagName === 'BUTTON');
            assert.ok(buttons.length > 0);
            for (const btn of buttons) {
                const nestedButtons = btn.childNodes.flatMap((child) => findElements(child, (el) => el.tagName === 'BUTTON'));
                assert.equal(nestedButtons.length, 0, `Found nested <button> inside button "${btn.textContent}"`);
            }

            // Verify ErrorInspectorModal table headers also expose role="tooltip" help
            await act(async () => {
                root.render(
                    <ErrorInspectorModal
                        isOpen
                        onClose={() => {}}
                        taskId="sim-task-1"
                        initialFilter="all"
                        isRunning
                    />,
                );
            });
            await flush();
            const modalTooltipTexts = findElements(doc.body, (el) => el.getAttribute('role') === 'tooltip')
                .map((el) => el.textContent)
                .join('\n');
            assert.match(modalTooltipTexts, /1-based dispatch sequence number/);
            assert.match(modalTooltipTexts, /Dispatch offset in seconds/);
            assert.match(modalTooltipTexts, /HTTP status code or transport\/timeout/);
            assert.match(modalTooltipTexts, /Sanitized error payload/);
            assert.match(modalTooltipTexts, /Elapsed time \(ms\) from request dispatch/);
            assert.match(modalTooltipTexts, /Prompt input tokens sent/);
        },
    );
});

test('ErrorInspectorModal filter pills expose simulation-filter-pill class, aria-pressed, data-selected, and data-tone hooks', async (t) => {
    await withMountedDom(
        t,
        async () => Response.json({
            total: 12,
            counts_by_code: { all: 12, '4xx': 3, '5xx': 7, timeout: 1, transport: 1, 503: 7 },
            issues: [],
        }),
        async ({ root, doc, findButton, clickButton, flush }) => {
            await act(async () => {
                root.render(
                    <ErrorInspectorModal
                        isOpen
                        onClose={() => {}}
                        taskId="sim-task-1"
                        initialFilter="all"
                        isRunning={false}
                    />,
                );
            });
            await flush();

            const dialog = findElements(doc.body, (el) => el.getAttribute('role') === 'dialog')[0];
            assert.ok(dialog, 'Expected ErrorInspectorModal dialog');
            assert.match(dialog.getAttribute('class') || '', /\bsimulation-error-inspector-modal\b/);

            const allBtn = findButton(/All Errors/);
            const fourXxBtn = findButton(/4xx Client Errors/);
            const fiveXxBtn = findButton(/5xx Server Errors/);
            const timeoutBtn = findButton(/Timeout/);
            const transportBtn = findButton(/Transport/);

            for (const btn of [allBtn, fourXxBtn, fiveXxBtn, timeoutBtn, transportBtn]) {
                assert.ok(btn, 'Expected filter pill button');
                assert.match(btn.getAttribute('class') || '', /\bsimulation-filter-pill\b/);
            }

            assert.equal(allBtn.getAttribute('aria-pressed'), 'true');
            assert.equal(allBtn.getAttribute('data-selected'), 'true');
            assert.equal(allBtn.getAttribute('data-tone'), null);

            assert.equal(fourXxBtn.getAttribute('aria-pressed'), 'false');
            assert.equal(fourXxBtn.getAttribute('data-selected'), 'false');
            assert.equal(fourXxBtn.getAttribute('data-tone'), 'warn');

            assert.equal(timeoutBtn.getAttribute('data-tone'), 'warn');
            assert.equal(fiveXxBtn.getAttribute('data-tone'), 'error');
            assert.equal(transportBtn.getAttribute('data-tone'), 'error');

            await clickButton(/5xx Server Errors/);
            await flush();

            assert.equal(allBtn.getAttribute('aria-pressed'), 'false');
            assert.equal(allBtn.getAttribute('data-selected'), 'false');
            assert.equal(fiveXxBtn.getAttribute('aria-pressed'), 'true');
            assert.equal(fiveXxBtn.getAttribute('data-selected'), 'true');
        },
    );
});

test('ConfigurationGroup, DetailCard, StatisticsPanel, and RunCompareDrawer emit engine-agnostic formulas below tooltip prose', async (t) => {
    const helperHtml = renderToStaticMarkup(
        <div>
            <ConfigurationGroup
                title="Replay Timing"
                help="Timing parameters"
                helpFormula="Effective = Duration + Grace"
                items={[
                    ['Duration', '60 s', 'Active duration', 'Replay_Duration_s = Span_s / Scale_Factor'],
                ]}
            />
            <DetailCard
                label="Goodput"
                value="12.5 req/s"
                accent="text-emerald-300"
                help="SLO-compliant request rate"
                helpFormula="Goodput = Good_Requests / Effective_Window_s"
            />
            <StatisticsPanel
                title="TTFT Distribution"
                help="Time to first token stats"
                helpFormula="TTFT = t_first_token − t_request_start"
                values={{ mean: 120, p50: 100, p90: 210, p95: 250, p99: 340, min: 40, max: 500 }}
            />
        </div>,
    );
    assert.match(helperHtml, /Effective = Duration \+ Grace/);
    assert.match(helperHtml, /Replay_Duration_s = Span_s \/ Scale_Factor/);
    assert.match(helperHtml, /Goodput = Good_Requests \/ Effective_Window_s/);
    assert.match(helperHtml, /TTFT = t_first_token − t_request_start/);
    assert.match(helperHtml, /Mean = \(1 \/ N\) Σ x_i/);
    assert.match(helperHtml, /P99 = Quantile_0\.99\(sorted\(x\)\)/);
    assert.doesNotMatch(helperHtml, /\b(vllm:|sglang:|trtllm:|vllm\b)|--kv-cache-metrics-sample/i);

    await withMountedDom(
        t,
        async (url) => {
            const isRun1 = url.includes('sim-1');
            return Response.json({
                run_manifest: { backend: 'aiperf', target: { model_name: 'Qwen3-8B' }, dataset: { name: 'bailian' } },
                forensics: { verdict: { primary_bottleneck: 'healthy', attainment_pct: isRun1 ? 94 : 98 } },
                client_series: {
                    windows: {
                        profiling: {
                            throughput_rps: isRun1 ? 10 : 12,
                            goodput_rps: isRun1 ? 9.4 : 11.76,
                            output_tok_per_s: isRun1 ? 500 : 600,
                            ttft_ms: { p50: 100, p90: 200, p99: 300 },
                            tpot_ms: { p50: 20, p90: 30, p99: 45 },
                            error_rate_pct: 0.5,
                        },
                    },
                    buckets: [],
                },
                server_telemetry: {
                    summary: { kv_cache_usage_percent: { max: 62 } },
                    series: [],
                },
            });
        },
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <RunCompareDrawer
                        isOpen
                        onClose={() => {}}
                        taskIds={['sim-1', 'sim-2']}
                        tasksById={{
                            'sim-1': { id: 'sim-1', name: 'Baseline', simulation: { duration_seconds: 60, grace_period_seconds: 30 } },
                            'sim-2': { id: 'sim-2', name: 'Candidate', simulation: { duration_seconds: 60, grace_period_seconds: 30 } },
                        }}
                    />,
                );
            });
            await flush();
            await flush();

            const tooltips = findElements(doc.body, (el) => el.getAttribute('role') === 'tooltip');
            assert.ok(tooltips.length >= 25, `Expected >= 25 tooltips in RunCompareDrawer, got ${tooltips.length}`);
            const formulas = findElements(
                doc.body,
                (el) => (el.getAttribute('class') || '').includes('lens-help-formula'),
            );
            assert.ok(formulas.length >= 25, `Expected >= 25 formula blocks in RunCompareDrawer, got ${formulas.length}`);
            const tooltipTexts = tooltips.map((el) => el.textContent).join('\n');
            assert.doesNotMatch(tooltipTexts, /\b(vllm:|sglang:|trtllm:|vllm\b)|--kv-cache-metrics-sample/i);
            assert.match(tooltipTexts, /Δ = Candidate − Baseline/);
            assert.match(tooltipTexts, /Throughput\(t\) = Completed_Requests_in_Bin \/ Bin_Width_s/);
            assert.match(tooltipTexts, /Peak_KV_Usage\(t\) \(%\) = max_pods/);
        },
    );
});

test('LiveMonitorPanel places Warmup ReferenceLine label on the LEFT (insideTopRight) across all throughput spans', () => {
    const captureGrid = (warmupSeconds, throughputPoints) => {
        let liveTree = null;
        function CaptureLive() {
            liveTree = LiveMonitorPanel({
                task: makeTask({ simulation: { duration_seconds: 60, stream: true, backend_options: { warmup_seconds: warmupSeconds } } }),
                liveTelemetry: makeLiveTelemetry({
                    warmup_seconds: warmupSeconds,
                    series: { client_throughput: throughputPoints },
                }),
            });
            return null;
        }
        renderToStaticMarkup(<CaptureLive />);
        const chartsGrid = findReactElement(liveTree, (n) => typeof n.type === 'function' && n.type.name === 'LiveChartsGrid');
        let gridTree = null;
        function CaptureGrid() {
            gridTree = chartsGrid.type(chartsGrid.props);
            return null;
        }
        renderToStaticMarkup(<CaptureGrid />);
        return gridTree;
    };

    // Case 1: Small warmup (5s of 60s) -> insideTopRight places 'Warmup' on the LEFT of the line with top-margin clearance
    const wideGrid = captureGrid(5, [
        { t: 0, rps: 1.0, output_tok_s: 50, in_flight: 2, failed_rps: 0 },
        { t: 60, rps: 5.0, output_tok_s: 250, in_flight: 8, failed_rps: 0 },
    ]);
    const wideWarmup = findReactElement(wideGrid, (n) => n.props?.x === 5 && n.props?.label?.value === 'Warmup');
    assert.ok(wideWarmup, 'Expected Warmup ReferenceLine in LiveChartsGrid');
    assert.equal(wideWarmup.props.label.position, 'insideTopRight');
    assert.equal(wideWarmup.props.label.fill, 'var(--text-muted)');
    assert.equal(wideWarmup.props.label.fontSize, 10);
    assert.equal(wideWarmup.props.label.dy, -18);
    const throughputChart = findReactElement(wideGrid, (n) => Array.isArray(n.props?.data) && n.props.data.length === 2 && n.props?.margin);
    assert.ok(throughputChart && throughputChart.props.margin.top >= 22, 'Expected top margin >= 22 on live throughput chart');

    // Case 2: Warmup at right edge (15s of 15s) -> still insideTopRight (LEFT of the line)
    const rightGrid = captureGrid(15, [
        { t: 0, rps: 1.0, output_tok_s: 50, in_flight: 2, failed_rps: 0 },
        { t: 15, rps: 3.0, output_tok_s: 150, in_flight: 5, failed_rps: 0 },
    ]);
    const rightWarmup = findReactElement(rightGrid, (n) => n.props?.x === 15 && n.props?.label?.value === 'Warmup');
    assert.ok(rightWarmup);
    assert.equal(rightWarmup.props.label.position, 'insideTopRight');
    assert.equal(rightWarmup.props.label.dy, -18);
});

test('LiveMonitorPanel renders server_fallback badges, Pending flush, live TTFT/TPOT, Schedule Drift empty state, scrape #1 waiting state, and preserves t61/t66 invariants', async (t) => {
    const serverFallbackTelemetry = makeLiveTelemetry({
        telemetry_status: {
            prometheus_available: true,
            accelerator_available: true,
            topology_mode: 'disaggregated',
            accelerator_label: 'XPU',
            client_telemetry_source: 'server_fallback',
        },
        progress: {
            percent: 42,
            dispatched: 0,
            completed: 0,
            failed: 0,
            in_flight: 13,
        },
        kpis: {
            ...makeLiveTelemetry().kpis,
            throughput_rps: 14.2,
            output_tok_per_s: 710.5,
            ttft_p50_ms: 135,
            ttft_p90_ms: 290,
            tpot_p50_ms: 16,
            tpot_p90_ms: 31,
            error_rate_pct: 0,
            rolling_error_rate_pct: 0,
            error_counts: { all: 0, '4xx': 0, '5xx': 0, timeout: 0, transport: 0 },
            server_running: 10,
            server_waiting: 3,
        },
        series: {
            client_throughput: [
                { t: 15, rps: 12.1, failed_rps: 0, output_tok_s: 610, in_flight: 11 },
                { t: 30, rps: 14.2, failed_rps: 0, output_tok_s: 710.5, in_flight: 13 },
            ],
            rolling_latency: [
                { t: 15, ttft_p50_ms: 130, ttft_p90_ms: 280, tpot_p50_ms: 15, tpot_p90_ms: 29, drift_p90_ms: null },
                { t: 30, ttft_p50_ms: 135, ttft_p90_ms: 290, tpot_p50_ms: 16, tpot_p90_ms: 31, drift_p90_ms: null },
            ],
            server_load: [
                { t: 30, prefill_running: 4, prefill_waiting: 0, decode_running: 6, decode_waiting: 3, epp_running: 13 },
            ],
            kv_and_accelerator: [
                { t: 30, kv_cache_peak_pct: 64, prefix_cache_hit_pct: 48, accelerator_util_pct: 77 },
            ],
        },
    });

    // 1. Static markup check for server_fallback badges, subtitles, Progress Strip, KPI Cards, and client-backed regression check
    const fallbackHtml = renderToStaticMarkup(
        <LiveMonitorPanel
            task={makeTask({ simulation: { duration_seconds: 120, stream: true, backend: 'aiperf', backend_options: { warmup_seconds: 15 } } })}
            liveTelemetry={serverFallbackTelemetry}
        />,
    );
    assert.match(fallbackHtml, /Completed:\s*<strong[^>]*>Pending flush<\/strong>/);
    assert.match(fallbackHtml, /In-Flight:\s*<strong[^>]*>13<\/strong>.*\(Engine\)/);
    assert.doesNotMatch(fallbackHtml, /\(Server:\s*13\)/);
    assert.match(fallbackHtml, /Pending flush/);
    assert.doesNotMatch(fallbackHtml, /\b0 req\b/);
    assert.match(fallbackHtml, /Live Latency \(1m\)/);
    assert.doesNotMatch(fallbackHtml, /Rolling Latency \(15s\)/);
    assert.match(fallbackHtml, /Live engine telemetry \(1m rate; client trace flushes on completion\)/);
    assert.match(fallbackHtml, /Live engine telemetry \(1m P50\/P90; client trace flushes on completion\)/);
    assert.match(fallbackHtml, /Client trace flushes on completion/);
    assert.equal((fallbackHtml.match(/Live engine telemetry<\/span>/g) || []).length, 4);

    // Client-backed regression check (client_telemetry_source = 'client' and omitted)
    for (const source of ['client', undefined]) {
        const clientTelemetry = makeLiveTelemetry({
            telemetry_status: {
                ...makeLiveTelemetry().telemetry_status,
                ...(source ? { client_telemetry_source: source } : {}),
            },
        });
        const clientHtml = renderToStaticMarkup(
            <LiveMonitorPanel task={makeTask()} liveTelemetry={clientTelemetry} />,
        );
        assert.match(clientHtml, /Rolling Latency \(15s\)/);
        assert.match(clientHtml, /\(Server:\s*13\)/);
        assert.match(clientHtml, /100 req/);
        assert.match(clientHtml, /15s rolling completion rate, token throughput, and active concurrency/);
        assert.match(clientHtml, /15s rolling P50\/P90 envelope and client dispatch schedule fidelity/);
        assert.doesNotMatch(clientHtml, /Live engine telemetry/);
        assert.doesNotMatch(clientHtml, /Pending flush/);
        assert.doesNotMatch(clientHtml, /\(Engine\)/);
        assert.doesNotMatch(clientHtml, /Live Latency \(1m\)/);
    }

    // 2. React element checks: LiveChartsGrid clientTelemetrySource prop, live TTFT curves, and t66 Warmup label under server_fallback
    const captureFallbackGrid = (telemetryOverride = {}) => {
        let liveTree = null;
        function CaptureLive() {
            liveTree = LiveMonitorPanel({
                task: makeTask({ simulation: { duration_seconds: 60, stream: true, backend: 'aiperf', backend_options: { warmup_seconds: telemetryOverride.warmup_seconds ?? 5 } } }),
                liveTelemetry: { ...serverFallbackTelemetry, ...telemetryOverride },
            });
            return null;
        }
        renderToStaticMarkup(<CaptureLive />);
        const chartsGrid = findReactElement(liveTree, (n) => typeof n.type === 'function' && n.type.name === 'LiveChartsGrid');
        assert.ok(chartsGrid, 'Expected LiveChartsGrid element');
        assert.equal(chartsGrid.props.clientTelemetrySource, 'server_fallback');
        let gridTree = null;
        function CaptureGrid() {
            gridTree = chartsGrid.type(chartsGrid.props);
            return null;
        }
        renderToStaticMarkup(<CaptureGrid />);
        return gridTree;
    };

    const gridWide = captureFallbackGrid({
        warmup_seconds: 5,
        series: {
            ...serverFallbackTelemetry.series,
            client_throughput: [
                { t: 0, rps: 10.0, failed_rps: 0, output_tok_s: 500, in_flight: 10 },
                { t: 60, rps: 14.2, failed_rps: 0, output_tok_s: 710, in_flight: 13 },
            ],
        },
    });
    const warmupWide = findReactElement(gridWide, (n) => n.props?.x === 5 && n.props?.label?.value === 'Warmup');
    assert.ok(warmupWide, 'Expected Warmup ReferenceLine under server_fallback');
    assert.equal(warmupWide.props.label.position, 'insideTopRight');
    assert.ok(findReactElement(gridWide, (n) => n.props?.dataKey === 'ttft_p50_ms'));
    assert.ok(findReactElement(gridWide, (n) => n.props?.dataKey === 'ttft_p90_ms'));

    const gridRight = captureFallbackGrid({
        warmup_seconds: 15,
        series: {
            ...serverFallbackTelemetry.series,
            client_throughput: [
                { t: 0, rps: 10.0, failed_rps: 0, output_tok_s: 500, in_flight: 10 },
                { t: 15, rps: 14.2, failed_rps: 0, output_tok_s: 710, in_flight: 13 },
            ],
        },
    });
    const warmupRight = findReactElement(gridRight, (n) => n.props?.x === 15 && n.props?.label?.value === 'Warmup');
    assert.ok(warmupRight);
    assert.equal(warmupRight.props.label.position, 'insideTopRight');

    // 3. Mounted DOM tests: interactive tab switches (TPOT live curves, Schedule Drift empty state with non-empty rolling_latency, scrape #1 waiting state -> In-Flight, and t61 zero-vLLM tooltips)
    await withMountedDom(
        t,
        async () => Response.json({}),
        async ({ root, doc, clickButton }) => {
            // Keep Recharts ResponsiveContainer from mounting ComposedChart SVG in the minimal test DOM
            // while still exercising LiveChartsGrid tab toggles, legends, and EmptyState branches.
            const origCreateElement = doc.createElement.bind(doc);
            doc.createElement = (tag) => {
                const node = origCreateElement(tag);
                node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
                return node;
            };

            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask({ simulation: { duration_seconds: 120, stream: true, backend: 'aiperf', backend_options: { warmup_seconds: 15 } } })}
                        liveTelemetry={serverFallbackTelemetry}
                    />,
                );
            });

            // t61 tooltip invariant preserved under server_fallback
            const tooltips = findElements(doc.body, (el) => el.getAttribute('role') === 'tooltip');
            assert.ok(tooltips.length >= 25, `Expected >= 25 tooltips, got ${tooltips.length}`);
            const tooltipTexts = tooltips.map((el) => el.textContent).join('\n');
            assert.doesNotMatch(tooltipTexts, /\b(vllm:|sglang:|trtllm:|vllm\b)|--kv-cache-metrics-sample/i);
            const formulas = findElements(
                doc.body,
                (el) => (el.getAttribute('class') || '').includes('lens-help-formula'),
            );
            assert.ok(formulas.length >= 20, `Expected >= 20 formula blocks, got ${formulas.length}`);

            // Progress Strip + KPI cards + live TTFT curves on default tab
            assert.match(doc.body.textContent, /Completed:\s*Pending flush/);
            assert.match(doc.body.textContent, /In-Flight:\s*13\s*\(Engine\)/);
            assert.match(doc.body.textContent, /Live Latency \(1m\)/);
            assert.match(doc.body.textContent, /Client trace flushes on completion/);
            assert.match(doc.body.textContent, /TTFT P50/);
            assert.match(doc.body.textContent, /TTFT P90/);
            assert.doesNotMatch(doc.body.textContent, /Waiting for latency samples/);

            // Switch to TPOT tab -> plots live TPOT P50/P90 curves immediately
            await clickButton(/^TPOT$/);
            assert.doesNotMatch(doc.body.textContent, /Waiting for latency samples/);
            assert.match(doc.body.textContent, /TPOT P50/);
            assert.match(doc.body.textContent, /TPOT P90/);

            // Switch to Schedule Drift tab with non-empty rolling_latency (drift_p90_ms: null) -> buffered empty state
            await clickButton(/^Schedule Drift$/);
            assert.match(doc.body.textContent, /Schedule drift pending trace flush/);
            assert.match(doc.body.textContent, /Schedule drift is computed on trace completion when per-request dispatch timestamps are buffered\./);

            // Scrape #1 partial state: in_flight ready before [1m] rate metrics arrive
            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask({ simulation: { duration_seconds: 120, stream: true, backend: 'aiperf', backend_options: { warmup_seconds: 15 } } })}
                        liveTelemetry={{
                            ...serverFallbackTelemetry,
                            kpis: {
                                ...serverFallbackTelemetry.kpis,
                                throughput_rps: null,
                                output_tok_per_s: null,
                                ttft_p50_ms: null,
                                ttft_p90_ms: null,
                                tpot_p50_ms: null,
                                tpot_p90_ms: null,
                            },
                            series: {
                                ...serverFallbackTelemetry.series,
                                client_throughput: [
                                    { t: 5, rps: null, failed_rps: null, output_tok_s: null, in_flight: 13 },
                                ],
                                rolling_latency: [],
                            },
                        }}
                    />,
                );
            });

            assert.match(doc.body.textContent, /Waiting for engine rate samples/);
            assert.match(doc.body.textContent, /Live engine throughput rates will appear after Prometheus completes two scrapes\./);

            await clickButton(/^Output$/);
            assert.match(doc.body.textContent, /Waiting for engine rate samples/);

            await clickButton(/^In-Flight$/);
            assert.doesNotMatch(doc.body.textContent, /Waiting for engine rate samples/);
        },
    );
});

test('LiveMonitorPanel In-Flight concurrency chart shows configured concurrency reference line and legend only when configured', async (t) => {
    const liveSeries = {
        client_throughput: [
            { t: 10, rps: 4.0, failed_rps: 0, output_tok_s: 200, in_flight: 8 },
            { t: 20, rps: 5.0, failed_rps: 0, output_tok_s: 250, in_flight: 12 },
        ],
        rolling_latency: [
            { t: 10, ttft_p50_ms: 120, ttft_p90_ms: 210, tpot_p50_ms: 18, tpot_p90_ms: 28, drift_p90_ms: 2.0 },
        ],
        server_load: [],
        kv_and_accelerator: [],
    };

    const captureInflightGrid = (task, telemetryOverride = {}) => {
        let liveTree = null;
        function CaptureLive() {
            liveTree = LiveMonitorPanel({
                task,
                liveTelemetry: makeLiveTelemetry({ series: liveSeries, ...telemetryOverride }),
            });
            return null;
        }
        renderToStaticMarkup(<CaptureLive />);
        const chartsGrid = findReactElement(liveTree, (n) => typeof n.type === 'function' && n.type.name === 'LiveChartsGrid');
        assert.ok(chartsGrid, 'Expected LiveChartsGrid element');
        let gridTree = null;
        function CaptureGrid() {
            gridTree = chartsGrid.type({ ...chartsGrid.props, initialThroughputMode: 'inflight' });
            return null;
        }
        renderToStaticMarkup(<CaptureGrid />);
        return gridTree;
    };

    // 1. React element assertions for YAxis domain, ReferenceLine, and ChartLegend
    const configuredGrid = captureInflightGrid(
        makeTask({
            simulation: {
                duration_seconds: 120,
                stream: true,
                backend: 'aiperf',
                backend_options: { warmup_seconds: 15, concurrency: 64 },
            },
        }),
    );
    const configuredYAxis = findReactElement(configuredGrid, (n) => n.type === YAxis && Array.isArray(n.props?.domain));
    assert.ok(configuredYAxis, 'Expected YAxis with explicit domain in LiveChartsGrid In-Flight mode');
    assert.equal(configuredYAxis.props.domain[0], 0);
    assert.ok(configuredYAxis.props.domain[1] > 64, `Expected YAxis domain max > 64, got ${configuredYAxis.props.domain[1]}`);

    const configuredRefLine = findReactElement(configuredGrid, (n) => n.type === ReferenceLine && n.props?.y === 64);
    assert.ok(configuredRefLine, 'Expected horizontal ReferenceLine y=64 in LiveChartsGrid In-Flight mode');
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

    const configuredLegend = findReactElement(
        configuredGrid,
        (n) => n.type === ChartLegend && n.props?.entries?.some((e) => e.label === 'Configured Concurrency (64)'),
    );
    assert.ok(configuredLegend);
    assert.deepEqual(configuredLegend.props.entries, [
        { label: 'Client In-Flight Requests', color: CHART_SERIES[3] },
        { label: 'Configured Concurrency (64)', color: CHART_SERIES[2] },
    ]);

    // Server-fallback label variant
    const fallbackGrid = captureInflightGrid(
        makeTask({
            simulation: {
                duration_seconds: 120,
                stream: true,
                backend: 'aiperf',
                backend_options: { warmup_seconds: 15, concurrency: 64 },
            },
        }),
        {
            telemetry_status: {
                ...makeLiveTelemetry().telemetry_status,
                client_telemetry_source: 'server_fallback',
            },
        },
    );
    const fallbackLegend = findReactElement(
        fallbackGrid,
        (n) => n.type === ChartLegend && n.props?.entries?.some((e) => e.label === 'Configured Concurrency (64)'),
    );
    assert.ok(fallbackLegend);
    assert.deepEqual(fallbackLegend.props.entries, [
        { label: 'Engine In-Flight Requests', color: CHART_SERIES[3] },
        { label: 'Configured Concurrency (64)', color: CHART_SERIES[2] },
    ]);

    // Unconfigured and trace-replayer omit ReferenceLine, domain, and legend
    for (const unconfiguredTask of [
        makeTask({
            simulation: {
                duration_seconds: 120,
                stream: true,
                backend: 'aiperf',
                backend_options: { warmup_seconds: 15 },
            },
        }),
        makeTask({
            simulation: {
                duration_seconds: 120,
                stream: true,
                backend: 'trace-replayer',
                backend_options: { warmup_seconds: 15, threads: 32, num_producer: 4, channel_capacity: 64 },
            },
        }),
    ]) {
        const unconfiguredGrid = captureInflightGrid(unconfiguredTask);
        assert.equal(findReactElement(unconfiguredGrid, (n) => n.type === ReferenceLine && n.props?.y !== undefined), undefined);
        assert.equal(findReactElement(unconfiguredGrid, (n) => n.type === YAxis && Array.isArray(n.props?.domain)), undefined);
        assert.equal(
            findReactElement(unconfiguredGrid, (n) => n.type === ChartLegend && n.props?.entries?.some((e) => /Configured Concurrency/.test(e.label))),
            undefined,
        );
    }

    // 2. Mounted DOM assertions clicking the In-Flight toggle button
    await withMountedDom(
        t,
        async () => Response.json({}),
        async ({ root, doc, clickButton }) => {
            const origCreateElement = doc.createElement.bind(doc);
            doc.createElement = (tag) => {
                const node = origCreateElement(tag);
                node.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 });
                return node;
            };

            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask({
                            simulation: {
                                duration_seconds: 120,
                                stream: true,
                                backend: 'aiperf',
                                backend_options: { warmup_seconds: 15, concurrency: 64 },
                            },
                        })}
                        liveTelemetry={makeLiveTelemetry({ series: liveSeries })}
                    />,
                );
            });

            await clickButton(/^In-Flight$/);
            assert.match(doc.body.textContent, /Configured Concurrency \(64\)/);
            assert.match(doc.body.textContent, /Client In-Flight Requests/);

            await act(async () => {
                root.render(
                    <LiveMonitorPanel
                        task={makeTask({
                            simulation: {
                                duration_seconds: 120,
                                stream: true,
                                backend: 'trace-replayer',
                                backend_options: { warmup_seconds: 15, threads: 32 },
                            },
                        })}
                        liveTelemetry={makeLiveTelemetry({ series: liveSeries })}
                    />,
                );
            });
            assert.doesNotMatch(doc.body.textContent, /Configured Concurrency/);
        },
    );
});

test('ErrorInspectorModal contains table width without lg:overflow-visible or min-w-[760px] and wraps request_id and error text', async (t) => {
    const longRequestId = '550e8400-e29b-41d4-a716-446655440000';
    const longError = 'Upstream connection reset at https://gateway.example.internal/v1/chat/completions?trace=abcdef0123456789abcdef0123456789';
    await withMountedDom(
        t,
        async () => Response.json({
            total: 2,
            counts_by_code: { all: 2, '5xx': 1, 503: 1, transport: 1 },
            issues: [
                {
                    request_number: 17,
                    request_id: longRequestId,
                    started_at_seconds: 8.25,
                    status_code: 503,
                    error: longError,
                    latency_ms: 912.4,
                    input_tokens: 1234,
                    output_tokens: 567,
                },
                {
                    request_number: 18,
                    request_id: 'req-transport-18',
                    started_at_seconds: 9.5,
                    category: 'transport',
                    error: 'Stream closed unexpectedly',
                    latency_ms: 45.2,
                    input_tokens: 256,
                    output_tokens: 0,
                },
            ],
        }),
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <ErrorInspectorModal
                        isOpen
                        onClose={() => {}}
                        taskId="sim-task-1"
                        initialFilter="all"
                        isRunning={false}
                        taskStartedAt="2026-01-01T00:00:00Z"
                    />,
                );
            });
            await flush();

            const table = findElements(doc.body, (el) => el.tagName === 'TABLE')[0];
            assert.ok(table, 'Expected ErrorInspectorModal table');
            const wrapperClass = table.parentNode?.getAttribute('class') || '';
            const tableClass = table.getAttribute('class') || '';

            assert.match(wrapperClass, /\bmax-lg:overflow-x-auto\b/);
            assert.doesNotMatch(wrapperClass, /\blg:overflow-visible\b/);
            assert.match(tableClass, /\blg:table-fixed\b/);
            assert.match(tableClass, /\blg:min-w-0\b/);
            assert.doesNotMatch(tableClass, /min-w-\[760px\]/);

            const cols = findElements(table, (el) => el.tagName === 'COL');
            assert.equal(cols.length, 6, 'Expected explicit 6-column colgroup');

            const reqIdNode = findElements(table, (el) => el.textContent === longRequestId)[0];
            assert.ok(reqIdNode, 'Expected request_id node');
            assert.match(reqIdNode.getAttribute('class') || '', /\bbreak-all\b/);
            assert.equal(reqIdNode.getAttribute('title'), longRequestId);

            const errorParagraph = findElements(table, (el) => el.tagName === 'P' && el.textContent === longError)[0];
            assert.ok(errorParagraph, 'Expected error message paragraph');
            assert.match(errorParagraph.getAttribute('class') || '', /\bmin-w-0\b/);
            assert.match(errorParagraph.getAttribute('class') || '', /\bbreak-words\b/);

            const transportBadge = findElements(table, (el) => el.tagName === 'SPAN' && el.textContent === 'Transport / Stream')[0];
            assert.ok(transportBadge, 'Expected Transport / Stream status badge');
            const badgeClass = transportBadge.getAttribute('class') || '';
            assert.match(badgeClass, /\bwhitespace-normal\b/);
            assert.match(badgeClass, /\bbreak-words\b/);
            assert.doesNotMatch(badgeClass, /\bwhitespace-nowrap\b/);

            assert.match(table.textContent, /Prompt \/ Output Tokens/);
            assert.match(table.textContent, /1,234 \/ 567/);
            assert.match(table.textContent, /\+8\.25s/);
            assert.match(table.textContent, /\+9\.50s/);
            assert.match(table.textContent, /912\.40 ms/);
            assert.match(table.textContent, /45\.20 ms/);
        },
    );
});

test('LiveMonitorPanel formats integer and near-zero started_at_seconds cleanly in ErrorInspectorModal', async (t) => {
    await withMountedDom(
        t,
        async () => Response.json({
            total: 2,
            counts_by_code: { all: 2, '5xx': 2 },
            issues: [
                { request_number: 1, started_at_seconds: 5, status_code: 500, error: 'err-int', latency_ms: 100, input_tokens: 10, output_tokens: 5 },
                { request_number: 2, started_at_seconds: -0.001, status_code: 500, error: 'err-zero', latency_ms: 0.004, input_tokens: 10, output_tokens: 5 },
            ],
        }),
        async ({ root, doc, flush }) => {
            await act(async () => {
                root.render(
                    <ErrorInspectorModal
                        isOpen
                        onClose={() => {}}
                        taskId="sim-task-2"
                        initialFilter="all"
                        isRunning={false}
                        taskStartedAt="2026-01-01T00:00:00Z"
                    />,
                );
            });
            await flush();
            assert.match(doc.body.textContent, /\+5s/);
            assert.match(doc.body.textContent, /100 ms/);
            assert.match(doc.body.textContent, /0\.00s/);
            assert.doesNotMatch(doc.body.textContent, /-0\.00s/);
            assert.match(doc.body.textContent, /0\.00 ms/);
        },
    );
});
