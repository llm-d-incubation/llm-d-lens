import test from 'node:test';
import assert from 'node:assert/strict';
import {
    TTFT_BAR_OPACITY_BY_KEY,
    backendOptionValue,
    buildRecordedFlowData,
    buildRecordedFlowFrames,
    buildStageLatencyView,
    buildTtftDecompositionView,
    clampPercent,
    filterActiveModelTokens,
    formatBytePair,
    formatByteRate,
    formatByteRatePair,
    formatBytes,
    formatCount,
    formatDecimal,
    formatDriftMs,
    formatDuration,
    formatDurationSeconds,
    formatEnergyWh,
    formatMilliseconds,
    formatModelTokenOptionLabel,
    formatNumber,
    formatStageName,
    formatWatts,
    hasServerLatencyTelemetry,
    isLegacyPooledPd,
    isBenchmarkRunnerPod,
    normalizeStageStackSeries,
    numberAt,
    selectByteRateUnitScale,
    selectByteUnitScale,
    stageAreaLayers,
    taskMetrics,
} from './presentation.js';
import { requestJson } from './client.js';

test('zero measurements remain available and invalid aliases are skipped', () => {
    assert.equal(numberAt({ a: null, b: '', c: 'invalid', d: 0 }, ['a', 'b', 'c', 'd']), 0);
    assert.equal(numberAt({}, ['value']), null);
    assert.equal(backendOptionValue('ttft_slo', 0.5), '500 ms');
});
test('running duration begins at execution rather than queued startup', () => {
    assert.equal(formatDuration({ status: 'running', started_at: '2026-01-01' }, {}, 0), 'Preparing…');
    assert.equal(formatDuration({ status: 'running', execution_started_at: '2026-01-01T00:00:00Z' }, {}, Date.parse('2026-01-01T00:00:30Z')), '30 s');
    assert.equal(formatDuration({ status: 'completed' }, { duration_seconds: 0 }), '0 s');
});
test('completion fallback preserves error percentage and SLO units', () => {
    const metrics = taskMetrics({ live_summary: { completion_timeline: [{ completed_requests: 10, failed_requests: 2 }] }, simulation: { backend_options: { ttft_slo: 0.5 } } });
    assert.equal(metrics.errorRate, 20);
    assert.equal(metrics.ttftSloMs, 500);
    assert.equal(metrics.tpot, null);
});
test('Simulation transport retains field errors and abort identity', async t => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: [{ msg: 'Invalid trace' }, { message: 'Missing backend' }] }, { status: 422 }));
    await assert.rejects(requestJson('/api/v1/simulation/tasks'), /Invalid trace; Missing backend/);
    const aborted = new DOMException('cancelled', 'AbortError');
    t.mock.method(globalThis, 'fetch', async () => { throw aborted; });
    await assert.rejects(requestJson('/api/v1/simulation/tasks'), e => e === aborted);
});

test('buildRecordedFlowData excludes benchmark runner pods, classifies EPP, and preserves decode pods when prefill is absent', () => {
    const serverTelemetry = {
        available: true,
        status: 'available',
        topology_mode: 'disaggregated',
        namespace: 'llm-d-sim',
        router: {
            request_rate_rps: { mean: 0.78 },
        },
        summary: {
            request_rate_rps: { mean: 0.59 },
            output_token_rate_tps: { mean: 556.8 },
        },
        per_pod: [
            { pod: 'llm-d-lens-sim-7df77121-d60bc0', role: 'model-server' },
            { pod: 'optimized-baseline-epp-5c569b9f79-rrkvd', role: 'model-server' },
            {
                pod: 'optimized-baseline-xpu-vllm-decode-545f64b875-ldpkq',
                role: 'decode',
                request_rate_rps: { mean: 0.59 },
                output_token_rate_tps: { mean: 556.8 },
                kv_cache_usage_percent: { mean: 20.6 },
            },
        ],
    };

    const flow = buildRecordedFlowData(serverTelemetry);
    assert.ok(flow);
    assert.equal(flow.hasTraffic, true);
    const allPodNames = flow.components.flatMap((c) => (c.instances || []).map((i) => i.name));
    assert.ok(!allPodNames.includes('llm-d-lens-sim-7df77121-d60bc0'));
    assert.ok(!allPodNames.includes('optimized-baseline-epp-5c569b9f79-rrkvd'));
    const decodeComp = flow.components.find((c) => c.role === 'decode');
    assert.ok(decodeComp, 'Expected decode component to be preserved when prefillPods is empty');
    assert.equal(decodeComp.instances[0].name, 'optimized-baseline-xpu-vllm-decode-545f64b875-ldpkq');
    assert.equal(decodeComp.instances[0].request_rate, 0.59);
    assert.ok(flow.components.some((c) => c.role === 'epp'));
    // Defensive historical compatibility: runner pod exclusion still recognizes legacy trace-replayer- prefixes
    assert.equal(isBenchmarkRunnerPod({ pod: 'trace-replayer-runner-0' }), true);
    assert.equal(isBenchmarkRunnerPod({ pod: 'aiperf-runner-0' }), true);
});

test('buildRecordedFlowFrames produces time-stepped traffic frames from server series and role_series', () => {
    const frames = buildRecordedFlowFrames({
        client_series: {
            origin_epoch: 1700000000,
            bucket_seconds: 5,
            buckets: [
                { mid_seconds: 2.5, arrived_rps: 6.6, throughput_rps: 0.0, output_tok_per_s: 0.0, in_flight_concurrency: 33 },
                { mid_seconds: 7.5, arrived_rps: 2.6, throughput_rps: 1.2, output_tok_per_s: 600.0, in_flight_concurrency: 38 },
            ],
        },
        server_telemetry: {
            available: true,
            status: 'available',
            topology_mode: 'disaggregated',
            step_seconds: 5,
            router: { request_rate_rps: { mean: 0.8 } },
            per_pod: [
                { pod: 'llm-d-lens-sim-runner-0', role: 'model-server' },
                { pod: 'decode-pod-0', role: 'decode', request_rate_rps: { mean: 1.0 }, output_token_rate_tps: { mean: 500 } },
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

    assert.equal(frames.length, 2);
    assert.equal(frames[0].t, 0);
    assert.equal(frames[0].runningRequests, 0);
    assert.equal(frames[1].t, 5);
    assert.equal(frames[1].rps, 1.2);
    assert.equal(frames[1].outputTps, 610);
    assert.equal(frames[1].runningRequests, 38);
    assert.equal(frames[1].kvCachePct, 46.9);
    const decodeFrame = frames[1].flowData.components.find((c) => c.role === 'decode');
    assert.equal(decodeFrame.instances[0].request_rate, 1.2);
    assert.equal(decodeFrame.instances[0].kv_cache_usage_perc, 46.9);
});

test('formatMilliseconds preserves integer clarity, formats floats to 2 decimals, and clampPercent bounds 0..100', () => {
    assert.equal(formatMilliseconds(null), '—');
    assert.equal(formatMilliseconds(0), '0 ms');
    assert.equal(formatMilliseconds(0.05), '0.05 ms');
    assert.equal(formatMilliseconds(0.09), '0.09 ms');
    assert.equal(formatMilliseconds(0.0045), '0.00 ms');
    assert.equal(formatMilliseconds(0.7), '0.70 ms');
    assert.equal(formatMilliseconds(3.4), '3.40 ms');
    assert.equal(formatMilliseconds(500), '500 ms');
    assert.equal(formatMilliseconds(2070.816), '2,070.82 ms');
    assert.equal(clampPercent(null), null);
    assert.equal(clampPercent(-0.5), 0);
    assert.equal(clampPercent(100.00025421224746), 100);
    assert.equal(clampPercent(42.5), 42.5);
});

test('formatStageName returns canonical t59 stage labels and safe fallbacks', () => {
    assert.equal(formatStageName('epp_scheduling'), 'EPP Scheduling');
    assert.equal(formatStageName('queue_time'), 'Queue Time');
    assert.equal(formatStageName('prefill_time'), 'Prefill Time');
    assert.equal(formatStageName('decode_time'), 'Decode Time');
    assert.equal(formatStageName('gateway_overhead'), 'Client / Gateway / Network');
    assert.equal(formatStageName('engine_overhead'), 'Server TTFT Residual');
    assert.equal(formatStageName('kv_transfer'), 'KV Transfer (NIXL)');
    assert.equal(formatStageName('ttft_unattributed'), 'Client TTFT');
    assert.equal(formatStageName(''), '—');
    assert.equal(formatStageName(null), '—');
    assert.equal(formatStageName('custom_stage'), 'Custom Stage');
});

test('normalizeStageStackSeries preserves all-null gaps, clamps negatives, and zeroes partial-null siblings', () => {
    const normalized = normalizeStageStackSeries(
        [
            { t: 0, epp_sched_ms: null, queue_ms: null, prefill_ms: null, decode_ms: null },
            { t: 10, epp_sched_ms: -2, queue_ms: Number.NaN, prefill_ms: 120, decode_ms: undefined },
            { t: 20, epp_sched_ms: ' ', queue_ms: false, prefill_ms: Number.POSITIVE_INFINITY, decode_ms: null },
        ],
        ['epp_sched_ms', 'queue_ms', 'prefill_ms', 'decode_ms'],
    );
    assert.deepEqual(normalized[0], { t: 0, epp_sched_ms: null, queue_ms: null, prefill_ms: null, decode_ms: null });
    assert.deepEqual(normalized[1], { t: 10, epp_sched_ms: 0, queue_ms: 0, prefill_ms: 120, decode_ms: 0 });
    assert.deepEqual(normalized[2], { t: 20, epp_sched_ms: null, queue_ms: null, prefill_ms: null, decode_ms: null });
});

test('buildStageLatencyView prefers canonical forensics.stage_latency_breakdown and normalizes p50/p90 series', () => {
    const view = buildStageLatencyView(
        {
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_p90_series: true,
                dominant_e2e_stage: 'decode_time',
                dominant_ttft_stage: 'prefill_time',
                dominant_e2e_stage_p90: 'queue_time',
                dominant_ttft_stage_p90: 'queue_time',
                p50: {
                    available: true,
                    epp_scheduling_ms: 2.4,
                    queue_time_ms: 18.0,
                    prefill_time_ms: 140.0,
                    decode_time_ms: 820.0,
                    gateway_overhead_ms: 14.6,
                    ttft_stage_ms: 175.0,
                    total_stage_ms: 995.0,
                    share_of_e2e_pct: { epp_scheduling: 0.2, queue_time: 1.8, prefill_time: 14.1, decode_time: 82.4, gateway_overhead: 1.5 },
                    share_of_ttft_pct: { epp_scheduling: 1.4, queue_time: 10.3, prefill_time: 80.0, gateway_overhead: 8.3 },
                },
                p90: {
                    available: true,
                    epp_scheduling_ms: 4.0,
                    queue_time_ms: 1400.0,
                    prefill_time_ms: 260.0,
                    decode_time_ms: 950.0,
                    gateway_overhead_ms: 26.0,
                    ttft_stage_ms: 1690.0,
                    total_stage_ms: 2640.0,
                    share_of_e2e_pct: { epp_scheduling: 0.2, queue_time: 53.0, prefill_time: 9.8, decode_time: 36.0, gateway_overhead: 1.0 },
                    share_of_ttft_pct: { epp_scheduling: 0.2, queue_time: 82.8, prefill_time: 15.4, gateway_overhead: 1.5 },
                },
                peak_window_shift: {
                    detected: true,
                    summary: 'Peak window at t=145s shifted dominant stage from Decode Time to Queue Time.',
                },
                series: [
                    { t: 0, epp_sched_ms: null, queue_ms: null, prefill_ms: null, decode_ms: null },
                    { t: 15, epp_sched_ms: 2.1, queue_ms: 14.5, prefill_ms: 112, decode_ms: 840, epp_sched_p90_ms: 4.2, queue_p90_ms: 1400, prefill_p90_ms: 210, decode_p90_ms: 980 },
                ],
            },
        },
        null,
        null,
    );

    assert.equal(view.available, true);
    assert.equal(view.source, 'server_telemetry');
    assert.equal(view.hasP90, true);
    assert.equal(view.dominantE2eStage, 'decode_time');
    assert.equal(view.dominantE2eStageP90, 'queue_time');
    assert.equal(view.peakWindowShift.detected, true);
    assert.equal(view.p50Series[0].decode_ms, null);
    assert.deepEqual(view.p50Series[1], { t: 15, epp_sched_ms: 2.1, queue_ms: 14.5, prefill_ms: 112, decode_ms: 840 });
    assert.deepEqual(view.p90Series[1], { t: 15, epp_sched_ms: 4.2, queue_ms: 1400, prefill_ms: 210, decode_ms: 980 });
});

test('buildStageLatencyView falls back to legacy serverTelemetry without double-counting EPP scheduling', () => {
    const view = buildStageLatencyView(
        {},
        {
            available: true,
            status: 'available',
            router: {
                scheduler_latency_p50_ms: { mean: 3 },
                scheduler_latency_p90_ms: { mean: 6 },
            },
            summary: {
                vllm_queue_time_p50_ms: { mean: 12 },
                vllm_prefill_time_p50_ms: { mean: 85 },
                vllm_decode_time_p50_ms: { mean: 300 },
                vllm_queue_time_p90_ms: { mean: 45 },
                vllm_prefill_time_p90_ms: { mean: 150 },
                vllm_decode_time_p90_ms: { mean: 450 },
            },
            series: [
                { elapsed_seconds: 5, router_scheduler_latency_p50_ms: 3, vllm_queue_time_p50_ms: 12, vllm_prefill_time_p50_ms: 85, vllm_decode_time_p50_ms: 300 },
            ],
        },
        {
            windows: {
                profiling: {
                    ttft_ms: { p50: 110, p90: 220 },
                    e2e_ms: { p50: 410, p90: 670 },
                },
            },
        },
    );

    assert.equal(view.available, true);
    assert.equal(view.source, 'server_telemetry');
    assert.equal(view.hasP90, true);
    // gateway_overhead = max(0, 110 - (3 + 12 + 85)) = 10
    assert.equal(view.slices.p50.gateway_overhead_ms, 10);
    assert.equal(view.slices.p50.ttft_stage_ms, 110);
    assert.equal(view.slices.p50.total_stage_ms, 410);
    assert.equal(view.dominantE2eStage, 'decode_time');
    assert.equal(view.dominantTtftStage, 'prefill_time');
    assert.equal(view.slices.p90.gateway_overhead_ms, 19);
});

test('buildStageLatencyView handles client_fallback math and zero/null guards', () => {
    const clientView = buildStageLatencyView(
        {},
        { available: false },
        {
            windows: {
                profiling: {
                    ttft_ms: { p50: 120, p90: 250, avg: 140 },
                    e2e_ms: { p50: 1000, p90: 1200, avg: 1040 },
                },
            },
            buckets: [
                { mid_seconds: 5, ttft: { p50: 120, p90: 250 }, e2e: { p50: 1000, p90: 1200 } },
                { mid_seconds: 10, ttft_ms: { p50: 300, p90: 400 }, e2e_ms: { p50: 200, p90: 350 } },
            ],
        },
    );

    assert.equal(clientView.available, true);
    assert.equal(clientView.source, 'client_fallback');
    assert.equal(clientView.hasP90, true);
    assert.equal(clientView.slices.p50.decode_time_ms, 880);
    assert.equal(clientView.slices.p50.queue_time_ms, null);
    assert.equal(clientView.slices.p50.share_of_e2e_pct.decode_time, 88);
    assert.equal(clientView.slices.p50.share_of_e2e_pct.ttft_unattributed, 12);
    assert.equal(clientView.dominantE2eStage, 'decode_time');
    assert.equal(clientView.dominantTtftStage, null);
    assert.deepEqual(clientView.p50Series[1], { t: 10, ttft_ms: 300, decode_ms: 0 });

    // Zero-denominator guard & unavailable guard
    const zeroView = buildStageLatencyView(
        {},
        { available: false },
        { windows: { profiling: { ttft_ms: { p50: 0 }, e2e_ms: { p50: 0 } } }, buckets: [] },
    );
    assert.equal(zeroView.available, true);
    assert.equal(zeroView.slices.p50.share_of_e2e_pct.decode_time, null);
    assert.equal(zeroView.slices.p50.share_of_e2e_pct.ttft_unattributed, null);
    assert.equal(zeroView.dominantE2eStage, null);

    const unavailable = buildStageLatencyView({ stage_latency_breakdown: { available: false, source: 'unavailable' } }, null, null);
    assert.equal(unavailable.available, false);
    assert.equal(unavailable.source, 'unavailable');
});

test('filterActiveModelTokens keeps only active, non-expired tokens with IDs', () => {
    const now = Date.parse('2026-11-26T00:00:00Z');
    const filtered = filterActiveModelTokens([
        null,
        { id: '', name: 'empty', status: 'active' },
        { id: 'tok-active', name: 'Primary', status: 'active', tokenHint: 'ab12' },
        { id: 'tok-case', name: 'Upper', status: 'ACTIVE', expiresAt: '2026-11-27T00:00:00Z' },
        { id: 'tok-snake', name: 'Snake', status: 'active', expires_at: '2026-11-26T00:00:01Z' },
        { id: 'tok-expired', name: 'Expired', status: 'active', expiresAt: '2026-11-25T23:59:59Z' },
        { id: 'tok-boundary', name: 'AtNow', status: 'active', expires_at: '2026-11-26T00:00:00Z' },
        { id: 'tok-revoked', name: 'Revoked', status: 'revoked' },
    ], now);
    assert.deepEqual(filtered.map((item) => item.id), ['tok-active', 'tok-case', 'tok-snake']);
    assert.deepEqual(filterActiveModelTokens(null, now), []);
});

test('formatModelTokenOptionLabel displays name and masked hint without secrets', () => {
    assert.equal(formatModelTokenOptionLabel({ id: '1', name: 'ci-key', tokenHint: '9f8e' }), 'ci-key (lens-mk-…9f8e)');
    assert.equal(formatModelTokenOptionLabel({ id: '2', name: 'snake-key', token_hint: '1234' }), 'snake-key (lens-mk-…1234)');
    assert.equal(formatModelTokenOptionLabel({ id: '3', name: 'legacy-key', tokenHint: 'lens-mk-' }), 'legacy-key (lens-mk-…)');
    assert.equal(formatModelTokenOptionLabel({ id: '4', name: 'plain-key' }), 'plain-key');
});

test('formatNumber and sibling numeric formatters reject non-numeric, empty, boolean, NaN, and Infinity inputs', () => {
    for (const invalid of ['TTFT P50', 'worker-0', '', '   ', true, false, NaN, Infinity, -Infinity, null, undefined, {}, []]) {
        assert.equal(formatNumber(invalid), '—', `Expected formatNumber(${String(invalid)}) === '—'`);
        assert.equal(formatNumber(invalid, ' s'), '—');
        assert.equal(formatCount(invalid), '—', `Expected formatCount(${String(invalid)}) === '—'`);
        assert.equal(formatMilliseconds(invalid), '—', `Expected formatMilliseconds(${String(invalid)}) === '—'`);
        assert.equal(formatDurationSeconds(invalid), '—', `Expected formatDurationSeconds(${String(invalid)}) === '—'`);
        assert.equal(formatWatts(invalid), '—', `Expected formatWatts(${String(invalid)}) === '—'`);
        assert.equal(formatEnergyWh(invalid), '—', `Expected formatEnergyWh(${String(invalid)}) === '—'`);
        assert.equal(formatDriftMs(invalid), '—', `Expected formatDriftMs(${String(invalid)}) === '—'`);
        assert.equal(clampPercent(invalid), null, `Expected clampPercent(${String(invalid)}) === null`);
        assert.equal(formatBytes(invalid), null, `Expected formatBytes(${String(invalid)}) === null`);
    }

    assert.equal(formatDecimal(0), '0');
    assert.equal(formatDecimal(-0), '0');
    assert.equal(formatDecimal(42), '42');
    assert.equal(formatDecimal(12.5), '12.50');
    assert.equal(formatDecimal(12.346), '12.35');
    assert.equal(formatDecimal(0.0045), '0.00');
    assert.equal(formatDecimal(-0.001), '0.00');
    assert.equal(formatDecimal(-0.005), '-0.01');
    assert.equal(formatDecimal(2070.816), '2,070.82');

    assert.equal(formatNumber(0), '0');
    assert.equal(formatNumber(12.5, ' s'), '12.50 s');
    assert.equal(formatNumber('12.5', ' req/s'), '12.50 req/s');
    assert.equal(formatNumber(12.346), '12.35');
    assert.equal(formatNumber(1000), '1K');
    assert.equal(formatNumber(2000), '2K');
    assert.equal(formatNumber(1500), '1.50K');
    assert.equal(formatNumber(1234.56), '1.23K');
    assert.equal(formatNumber(999999), '1.00M');
    assert.equal(formatNumber(1000000), '1M');
    assert.equal(formatNumber(-1500), '-1.50K');
    assert.equal(formatNumber(-999999), '-1.00M');
    assert.equal(formatCount(1234.6), '1,235');
    assert.equal(formatCount('42'), '42');
    assert.equal(formatDurationSeconds(0), '0 s');
    assert.equal(formatDurationSeconds(-1), '—');
    assert.equal(formatWatts(310), '310 W');
    assert.equal(formatWatts(310.456), '310.46 W');
    assert.equal(formatWatts(45.5), '45.50 W');
    assert.equal(formatEnergyWh(10.3), '10.30 Wh');
    assert.equal(formatEnergyWh(2000), '2 kWh');
    assert.equal(formatEnergyWh(1500), '1.50 kWh');
    assert.equal(formatDriftMs(0), '0 ms');
    assert.equal(formatDriftMs(-0), '0 ms');
    assert.equal(formatDriftMs(0.004), '0.00 ms');
    assert.equal(formatDriftMs(-0.001), '0.00 ms');
    assert.equal(formatDriftMs(-0.005), '-0.01 ms');
    assert.equal(formatDriftMs(2.5), '+2.50 ms');
    assert.equal(formatDriftMs(-2), '-2 ms');
    assert.equal(formatBytes(0), '0 B');
    assert.equal(formatBytes(512), '512 B');
    assert.equal(formatBytes(1024), '1 KiB');
    assert.equal(formatBytes(1536), '1.50 KiB');
    assert.equal(formatBytes(1024 ** 4), '1 TiB');
    assert.equal(formatBytes(1.5 * (1024 ** 4)), '1.50 TiB');
    assert.equal(formatBytes(2 * (1024 ** 5)), '2 PiB');
    assert.equal(formatBytes(-0.001), '0.00 B');
    assert.equal(formatBytes(-0), '0 B');
    assert.equal(backendOptionValue('ttft_slo', ''), '—');
    assert.equal(backendOptionValue('error_rate_slo', false), '—');
});

test('adaptive binary byte/rate formatters and scale selectors support B..PiB and B/s..PiB/s with t108 precision', () => {
    const KiB = 1024;
    const MiB = 1024 ** 2;
    const GiB = 1024 ** 3;
    const TiB = 1024 ** 4;
    const PiB = 1024 ** 5;

    for (const invalid of [null, undefined, '', '   ', true, false, NaN, Infinity, -Infinity, {}, []]) {
        assert.equal(formatByteRate(invalid), null);
        assert.equal(formatBytePair(invalid, invalid), null);
        assert.equal(formatByteRatePair(invalid, invalid), null);
    }

    assert.equal(formatByteRate(0), '0 B/s');
    assert.equal(formatByteRate(-0), '0 B/s');
    assert.equal(formatByteRate(-0.001), '0.00 B/s');
    assert.equal(formatByteRate(512), '512 B/s');
    assert.equal(formatByteRate(1536), '1.50 KiB/s');
    assert.equal(formatByteRate(50 * MiB), '50 MiB/s');
    assert.equal(formatByteRate(12.5 * GiB), '12.50 GiB/s');
    assert.equal(formatByteRate(2 * TiB), '2 TiB/s');
    assert.equal(formatByteRate(3.25 * PiB), '3.25 PiB/s');

    // formatBytePair: shared vs distinct units, zero handling, missing sides
    assert.equal(formatBytePair(0, 0), '0 / 0 B');
    assert.equal(formatBytePair(0, 2 * TiB), '0 / 2 TiB');
    assert.equal(formatBytePair(2 * TiB, 0), '2 / 0 TiB');
    assert.equal(formatBytePair(1.5 * TiB, 4 * TiB), '1.50 / 4 TiB');
    assert.equal(formatBytePair(512 * GiB, 2 * TiB), '512 GiB / 2 TiB');
    assert.equal(formatBytePair(1.25 * PiB, 4 * PiB), '1.25 / 4 PiB');
    assert.equal(formatBytePair(2 * TiB, null), '2 TiB / —');
    assert.equal(formatBytePair(null, 512 * GiB), '— / 512 GiB');

    // formatByteRatePair: defaults missing side to 0 when peer is finite, shares or splits units
    assert.equal(formatByteRatePair(null, 50 * MiB), '0 / 50 MiB/s');
    assert.equal(formatByteRatePair(50 * MiB, null), '50 / 0 MiB/s');
    assert.equal(formatByteRatePair(0, 0), '0 / 0 B/s');
    assert.equal(formatByteRatePair(12.5 * GiB, 25 * GiB), '12.50 / 25 GiB/s');
    assert.equal(formatByteRatePair(120 * KiB, 12.5 * GiB), '120 KiB/s / 12.50 GiB/s');
    assert.equal(formatByteRatePair(1.5 * TiB, 2 * TiB), '1.50 / 2 TiB/s');

    // selectByteUnitScale & selectByteRateUnitScale
    assert.deepEqual(selectByteUnitScale([]), { unit: 'B', unitIndex: 0, divisor: 1 });
    assert.deepEqual(selectByteUnitScale([null, NaN, '']), { unit: 'B', unitIndex: 0, divisor: 1 });
    assert.deepEqual(selectByteUnitScale([512 * GiB, 2 * TiB]), { unit: 'TiB', unitIndex: 4, divisor: TiB });
    assert.deepEqual(selectByteUnitScale([-5 * PiB, 100 * GiB]), { unit: 'PiB', unitIndex: 5, divisor: PiB });
    assert.deepEqual(selectByteRateUnitScale([120 * KiB, 500 * KiB]), { unit: 'KiB/s', unitIndex: 1, divisor: KiB });
    assert.deepEqual(selectByteRateUnitScale([500 * MiB, 12.5 * GiB]), { unit: 'GiB/s', unitIndex: 3, divisor: GiB });
    assert.deepEqual(selectByteRateUnitScale([2 * TiB]), { unit: 'TiB/s', unitIndex: 4, divisor: TiB });

    // Rounding-boundary promotion and PiB cap
    assert.equal(formatBytes(1023.999), '1.00 KiB');
    assert.equal(formatBytes(1023.999 * GiB), '1.00 TiB');
    assert.equal(formatBytes(-1023.999 * GiB), '-1.00 TiB');
    assert.equal(formatBytes(1024 * PiB), '1,024 PiB');
    assert.equal(formatByteRate(1023.999 * MiB), '1.00 GiB/s');
    assert.equal(formatBytePair(1023.999 * GiB, 2 * TiB), '1.00 / 2 TiB');
    assert.deepEqual(selectByteUnitScale([1023.999 * GiB]), { unit: 'TiB', unitIndex: 4, divisor: TiB });
    assert.deepEqual(selectByteRateUnitScale([1023.999 * MiB]), { unit: 'GiB/s', unitIndex: 3, divisor: GiB });
});

test('isLegacyPooledPd detects pooled P/D fallbacks while clearing role-separated telemetry', () => {
    assert.equal(
        isLegacyPooledPd({ stage_latency_breakdown: { source: 'pd_pooled_fallback' } }, {}),
        true,
    );
    assert.equal(
        isLegacyPooledPd(
            {
                stage_latency_breakdown: {
                    source: 'server_telemetry',
                    role_breakdown: { prefill: { prefill_p50_ms: 80 }, decode: { decode_p50_ms: 200 } },
                },
            },
            { topology_mode: 'disaggregated' },
        ),
        false,
    );
    assert.equal(
        isLegacyPooledPd({}, { available: true, metric_sources: { vllm_stage_latencies: 'pooled_namespace_legacy' } }),
        true,
    );
    assert.equal(
        isLegacyPooledPd({}, { available: true, metric_sources: { vllm_stage_latencies: 'role_separated' } }),
        false,
    );
    assert.equal(
        isLegacyPooledPd(
            {},
            {
                available: true,
                topology_mode: 'disaggregated',
                per_pod: [
                    { pod: 'p-0', role: 'prefill' },
                    { pod: 'd-0', role: 'decode' },
                ],
                summary: { vllm_prefill_time_p50_ms: { mean: 50 } },
            },
        ),
        true,
    );
});

test('stageAreaLayers enforces <=5 layers and reserves colorIndex 1 for Decode', () => {
    for (const [src, kv] of [
        ['server_telemetry', false],
        ['server_telemetry', true],
        ['pd_pooled_fallback', false],
        ['pd_pooled_fallback', true],
        ['client_fallback', false],
    ]) {
        const layers = stageAreaLayers(src, kv);
        assert.ok(layers.length <= 5, `Expected <=5 layers for ${src} (kv=${kv}), got ${layers.length}`);
        const decodeLayer = layers.find((l) => l.key === 'decode_ms');
        assert.ok(decodeLayer, `Expected decode_ms layer for ${src}`);
        assert.equal(decodeLayer.colorIndex, 1, `Expected colorIndex 1 for decode_ms in ${src}`);
        const nonDecodeWithIdx1 = layers.filter((l) => l.key !== 'decode_ms' && l.colorIndex === 1);
        assert.equal(nonDecodeWithIdx1.length, 0, `colorIndex 1 must be exclusive to decode_ms in ${src}`);
    }
});

test('buildStageLatencyView handles 10s TTFT ceiling guard, NIXL, mean slice, reconciliation, and role_breakdown', () => {
    // 1. 10s TTFT ceiling guard + NIXL + mean slice
    const ceilingView = buildStageLatencyView(
        {},
        {
            available: true,
            status: 'available',
            topology_mode: 'disaggregated',
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            router: {
                scheduler_latency_p50_ms: { mean: 4 },
                scheduler_latency_p90_ms: { mean: 8 },
                scheduler_latency_mean_ms: { mean: 5 },
            },
            summary: {
                vllm_queue_time_p50_ms: { mean: 100 },
                vllm_prefill_time_p50_ms: { mean: 200 },
                vllm_decode_time_p50_ms: { mean: 500 },
                vllm_ttft_p50_ms: { mean: 350 },
                vllm_e2e_p50_ms: { mean: 850 },
                vllm_queue_time_p90_ms: { mean: 1500 },
                vllm_prefill_time_p90_ms: { mean: 400 },
                vllm_decode_time_p90_ms: { mean: 800 },
                vllm_ttft_p90_ms: { mean: 9800 },
                vllm_e2e_p90_ms: { mean: 10600 },
                vllm_queue_time_mean_ms: { mean: 300 },
                vllm_prefill_time_mean_ms: { mean: 220 },
                vllm_decode_time_mean_ms: { mean: 520 },
                vllm_ttft_mean_ms: { mean: 550 },
                vllm_e2e_mean_ms: { mean: 1070 },
                nixl_transfer_latency_p50_ms: { mean: 20 },
                nixl_transfer_latency_p90_ms: { mean: 40 },
                nixl_transfer_latency_mean_ms: { mean: 25 },
            },
            summary_by_role: {
                prefill: { vllm_prefill_time_p50_ms: { mean: 200 }, vllm_prefill_time_p90_ms: { mean: 400 } },
                decode: { vllm_decode_time_p50_ms: { mean: 500 }, vllm_decode_time_p90_ms: { mean: 800 } },
            },
            series: [
                {
                    elapsed_seconds: 5,
                    router_scheduler_latency_p50_ms: 4,
                    vllm_queue_time_p50_ms: 100,
                    nixl_transfer_latency_p50_ms: 20,
                    vllm_prefill_time_p50_ms: 200,
                    vllm_decode_time_p50_ms: 500,
                },
            ],
        },
        {
            windows: {
                profiling: {
                    ttft_ms: { p50: 380, p90: 14000, mean: 600 },
                    e2e_ms: { p50: 880, p90: 14800, mean: 1120 },
                },
            },
        },
    );

    assert.equal(ceilingView.available, true);
    assert.equal(ceilingView.source, 'server_telemetry');
    assert.equal(ceilingView.isLegacyPooledPd, false);
    assert.equal(ceilingView.hasKvTransfer, true);
    assert.equal(ceilingView.slices.p50.kv_transfer_ms, 20);
    // p50 not clamped: server_ttft(350) - knownEngine(100) = 250 -> engine_overhead=50, gateway_overhead=26
    assert.equal(ceilingView.slices.p50.engine_overhead_ms, 50);
    assert.equal(ceilingView.slices.p50.gateway_overhead_ms, 26);
    // p90 clamped (>=9500 and client > server + 500): gateway_overhead_ms=null, engine_overhead_ms=totalTtftResidual
    assert.equal(ceilingView.slices.p90.gateway_overhead_ms, null);
    assert.ok(ceilingView.slices.p90.engine_overhead_ms > 0);
    assert.match(ceilingView.slices.p90.caveat || '', /10 s histogram ceiling/i);
    // mean slice present and has reconciliation
    assert.equal(ceilingView.slices.mean.available, true);
    assert.equal(ceilingView.slices.mean.kv_transfer_ms, 25);
    assert.ok(ceilingView.slices.p50.reconciliation);

    // 2. Legacy pd_pooled_fallback with and without NIXL
    const pooledWithNixl = buildStageLatencyView(
        {},
        {
            available: true,
            topology_mode: 'disaggregated',
            per_pod: [{ pod: 'p-0', role: 'prefill' }, { pod: 'd-0', role: 'decode' }],
            router: { scheduler_latency_p50_ms: { mean: 5 }, scheduler_latency_p90_ms: { mean: 10 } },
            summary: { nixl_transfer_latency_p50_ms: { mean: 15 }, nixl_transfer_latency_p90_ms: { mean: 30 } },
            series: [{ elapsed_seconds: 5, router_scheduler_latency_p50_ms: 5, nixl_transfer_latency_p50_ms: 15 }],
        },
        {
            windows: {
                profiling: {
                    ttft_ms: { p50: 120, p90: 240, mean: 130 },
                    e2e_ms: { p50: 520, p90: 840, mean: 550 },
                },
            },
            buckets: [{ mid_seconds: 5, ttft: { p50: 120, p90: 240 }, e2e: { p50: 520, p90: 840 } }],
        },
    );
    assert.equal(pooledWithNixl.source, 'pd_pooled_fallback');
    assert.equal(pooledWithNixl.isLegacyPooledPd, true);
    assert.equal(pooledWithNixl.hasKvTransfer, true);
    assert.equal(pooledWithNixl.slices.p50.kv_transfer_ms, 15);
    assert.equal(pooledWithNixl.slices.p50.ttft_unattributed_ms, 100);
    assert.equal(pooledWithNixl.slices.p50.decode_time_ms, 400);

    const pooledNoNixl = buildStageLatencyView(
        {},
        {
            available: true,
            topology_mode: 'disaggregated',
            per_pod: [{ pod: 'p-0', role: 'prefill' }, { pod: 'd-0', role: 'decode' }],
            router: { scheduler_latency_p50_ms: { mean: 5 } },
            summary: {},
            series: [{ elapsed_seconds: 5, router_scheduler_latency_p50_ms: 5 }],
        },
        {
            windows: { profiling: { ttft_ms: { p50: 100 }, e2e_ms: { p50: 500 } } },
            buckets: [{ mid_seconds: 5, ttft: { p50: 100 }, e2e: { p50: 500 } }],
        },
    );
    assert.equal(pooledNoNixl.source, 'pd_pooled_fallback');
    assert.equal(pooledNoNixl.hasKvTransfer, false);
    assert.equal(pooledNoNixl.slices.p50.kv_transfer_ms, null);
    assert.equal(pooledNoNixl.slices.p50.ttft_unattributed_ms, 95);

    // 3. Canonical role_breakdown pass-through
    const canonicalView = buildStageLatencyView({
        stage_latency_breakdown: {
            available: true,
            source: 'server_telemetry',
            role_breakdown: { prefill: { prefill_p50_ms: 75 }, decode: { decode_p50_ms: 310 } },
            p50: { available: true, epp_scheduling_ms: 2, queue_time_ms: 10, prefill_time_ms: 75, decode_time_ms: 310, engine_overhead_ms: 5, gateway_overhead_ms: 8 },
            mean: { available: true, epp_scheduling_ms: 2.5, queue_time_ms: 12, prefill_time_ms: 80, decode_time_ms: 320, engine_overhead_ms: 6, gateway_overhead_ms: 9 },
            series: [{ t: 5, epp_sched_ms: 2, queue_ms: 10, prefill_ms: 75, decode_ms: 310 }],
        },
    });
    assert.deepEqual(canonicalView.roleBreakdown, { prefill: { prefill_p50_ms: 75 }, decode: { decode_p50_ms: 310 } });
    assert.equal(canonicalView.slices.mean.prefill_time_ms, 80);
});

test('buildTtftDecompositionView preserves nullable values, includes Mean slice, and exposes granular residual fields', () => {
    const view = buildTtftDecompositionView(
        {
            ttft_decomposition: {
                available: true,
                source: 'server_telemetry',
                p50: { prefill_ms: 80, queue_ms: 20, kv_transfer_ms: 15, epp_scheduling_ms: 3, engine_overhead_ms: 12, gateway_overhead_ms: 7 },
                p90: { prefill_ms: 150, queue_ms: 200, kv_transfer_ms: 30, epp_scheduling_ms: 6, engine_overhead_ms: 500, gateway_overhead_ms: null },
                mean: { prefill_ms: 95, queue_ms: 40, kv_transfer_ms: 18, epp_scheduling_ms: 4, engine_overhead_ms: 15, gateway_overhead_ms: 9 },
            },
        },
        { available: true, source: 'server_telemetry', hasKvTransfer: true },
    );

    assert.equal(view.rows.length, 3);
    assert.deepEqual(view.rows.map((r) => r.stat), ['P50', 'Mean', 'P90']);
    assert.equal(view.hasKvTransfer, true);
    assert.equal(view.hasEngineResidual, true);
    assert.equal(view.residualLabel, 'Server & Gateway Residual');
    assert.equal(view.unsplitEngineLabel, 'Engine / API Overhead');
    assert.equal(view.rows[0].residual_ms, 22);
    assert.equal(view.rows[0].has_pd_sub_split, false);
    assert.equal(view.rows[0].engine_overhead_unsplit_ms, 12);
    assert.equal(view.rows[1].prefill_ms, 95);
    // Nullable gateway_overhead_ms preserved on P90
    assert.equal(view.rows[2].gateway_overhead_ms, null);
    assert.equal(view.rows[2].residual_ms, 506);
    assert.deepEqual(
        view.barDefs.map((b) => b.key),
        ['prefill_ms', 'queue_ms', 'kv_transfer_ms', 'engine_overhead_unsplit_ms', 'epp_scheduling_ms', 'gateway_overhead_ms'],
    );
});

test('buildTtftDecompositionView handles explicit P/D sub-fields, historical role_breakdown fallback with 270ms floor bypass, and mixed 10s-clamped slices', () => {
    // 1. Explicit backend P/D sub-fields with zero-suppression (queue_ms=0, gateway_overhead_ms=0 across all slices)
    const explicitPd = buildTtftDecompositionView(
        {
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
                mean: {
                    prefill_ms: 680.037,
                    decode_first_step_ms: 23.585,
                    queue_ms: 0,
                    kv_transfer_ms: 614.656,
                    prefill_engine_overhead_ms: 403.447,
                    decode_handoff_overhead_ms: 348.845,
                    epp_scheduling_ms: 0.222,
                    engine_overhead_ms: 775.877,
                    gateway_overhead_ms: 0,
                },
            },
        },
        null,
    );
    assert.equal(explicitPd.rows.length, 2);
    assert.equal(explicitPd.rows[0].has_pd_sub_split, true);
    assert.equal(explicitPd.rows[0].engine_overhead_unsplit_ms, null);
    assert.equal(explicitPd.rows[1].decode_first_step_ms, 23.585);
    assert.equal(explicitPd.rows[1].prefill_engine_overhead_ms, 403.447);
    assert.equal(explicitPd.rows[1].decode_handoff_overhead_ms, 348.845);
    // queue_ms and gateway_overhead_ms are 0 everywhere -> zero-suppressed from barDefs
    assert.deepEqual(
        explicitPd.barDefs.map((b) => b.key),
        ['prefill_ms', 'decode_first_step_ms', 'kv_transfer_ms', 'prefill_engine_overhead_ms', 'decode_handoff_overhead_ms', 'epp_scheduling_ms'],
    );
    assert.equal(TTFT_BAR_OPACITY_BY_KEY.decode_first_step_ms, 0.45);
    assert.equal(TTFT_BAR_OPACITY_BY_KEY.decode_handoff_overhead_ms, 0.45);
    assert.equal(TTFT_BAR_OPACITY_BY_KEY.gateway_overhead_ms, 0.45);

    // 2. Historical v7/v8 role_breakdown fallback matching real e0a7cde7 P90 (270ms floor bypass on prefill.queue_p90_ms)
    const historicalForensics = {
        schema_version: 8,
        stage_latency_breakdown: {
            available: true,
            source: 'server_telemetry',
            has_kv_transfer_series: true,
            role_breakdown: {
                prefill: {
                    ttft_p50_ms: 877.253,
                    queue_p50_ms: 150.0,
                    prefill_p50_ms: 366.359,
                    ttft_p90_ms: 2787.885,
                    queue_p90_ms: 270.0,
                    prefill_p90_ms: 2143.608,
                    ttft_mean_ms: 1141.335,
                    queue_mean_ms: 0.019,
                    prefill_mean_ms: 680.037,
                },
                decode: {
                    ttft_p50_ms: 794.254,
                    queue_raw_p50_ms: 393.896,
                    prefill_p50_ms: 150.0,
                    ttft_p90_ms: 2797.997,
                    queue_raw_p90_ms: 1777.468,
                    prefill_p90_ms: 270.0,
                    ttft_mean_ms: 1040.473,
                    queue_raw_mean_ms: 614.656,
                    prefill_mean_ms: 26.966,
                },
            },
            p50: { available: true, epp_scheduling_ms: 0.25, queue_time_ms: 0.019, kv_transfer_ms: 393.896, prefill_time_ms: 366.359, engine_overhead_ms: 303.429, gateway_overhead_ms: 0.0, decode_time_ms: 5552.356, server_ttft_clamped: false },
            p90: { available: true, epp_scheduling_ms: 0.446, queue_time_ms: 0.019, kv_transfer_ms: 1777.468, prefill_time_ms: 2143.608, engine_overhead_ms: 1664.786, gateway_overhead_ms: 115.502, decode_time_ms: 11349.861, server_ttft_clamped: false },
            mean: { available: true, epp_scheduling_ms: 0.222, queue_time_ms: 0.019, kv_transfer_ms: 614.656, prefill_time_ms: 680.037, engine_overhead_ms: 775.877, gateway_overhead_ms: 0.0, decode_time_ms: 5804.915, server_ttft_clamped: false },
        },
        ttft_decomposition: {
            available: true,
            source: 'server_telemetry',
            p50: { prefill_ms: 366.359, queue_ms: 0.019, kv_transfer_ms: 393.896, epp_scheduling_ms: 0.25, engine_overhead_ms: 303.429, gateway_overhead_ms: 0.0, server_ttft_clamped: false },
            p90: { prefill_ms: 2143.608, queue_ms: 0.019, kv_transfer_ms: 1777.468, epp_scheduling_ms: 0.446, engine_overhead_ms: 1664.786, gateway_overhead_ms: 115.502, server_ttft_clamped: false },
            mean: { prefill_ms: 680.037, queue_ms: 0.019, kv_transfer_ms: 614.656, epp_scheduling_ms: 0.222, engine_overhead_ms: 775.877, gateway_overhead_ms: 0.0, server_ttft_clamped: false },
        },
    };
    const stageView = buildStageLatencyView(historicalForensics);
    const fallbackPd = buildTtftDecompositionView(historicalForensics, stageView);
    const p90Row = fallbackPd.rows.find((r) => r.stat === 'P90');
    const meanRow = fallbackPd.rows.find((r) => r.stat === 'Mean');
    // P90 bypassed prefill.queue_p90_ms=270 -> 0.019, exact to 0.001 ms
    assert.equal(p90Row.has_pd_sub_split, true);
    assert.equal(p90Row.decode_first_step_ms, 270);
    assert.equal(p90Row.prefill_engine_overhead_ms, 644.258);
    assert.equal(p90Row.decode_handoff_overhead_ms, 750.528);
    assert.ok(Math.abs((p90Row.decode_first_step_ms + p90Row.prefill_engine_overhead_ms + p90Row.decode_handoff_overhead_ms) - 1664.786) <= 0.001);
    assert.equal(meanRow.decode_first_step_ms, 23.585);
    assert.equal(meanRow.prefill_engine_overhead_ms, 403.447);
    assert.equal(meanRow.decode_handoff_overhead_ms, 348.845);
    assert.ok(Math.abs((meanRow.decode_first_step_ms + meanRow.prefill_engine_overhead_ms + meanRow.decode_handoff_overhead_ms) - 775.877) <= 0.001);

    // 3. Mixed slices: Mean P/D split + P90 10s-clamped (P90 stays unsplit as Server TTFT Residual; zero double counting)
    const mixedClamped = buildTtftDecompositionView({
        stage_latency_breakdown: {
            available: true,
            source: 'server_telemetry',
            role_breakdown: historicalForensics.stage_latency_breakdown.role_breakdown,
            p90: { available: true, epp_scheduling_ms: 0.5, queue_time_ms: 1.0, kv_transfer_ms: 1500.0, prefill_time_ms: 2000.0, engine_overhead_ms: 8200.0, gateway_overhead_ms: null, server_ttft_clamped: true },
            mean: { available: true, epp_scheduling_ms: 0.222, queue_time_ms: 0.019, kv_transfer_ms: 614.656, prefill_time_ms: 680.037, engine_overhead_ms: 775.877, gateway_overhead_ms: 0.0, server_ttft_clamped: false },
        },
        ttft_decomposition: {
            available: true,
            source: 'server_telemetry',
            p90: { prefill_ms: 2000.0, queue_ms: 1.0, kv_transfer_ms: 1500.0, epp_scheduling_ms: 0.5, engine_overhead_ms: 8200.0, gateway_overhead_ms: null, server_ttft_clamped: true },
            mean: { prefill_ms: 680.037, queue_ms: 0.019, kv_transfer_ms: 614.656, epp_scheduling_ms: 0.222, engine_overhead_ms: 775.877, gateway_overhead_ms: 0.0, server_ttft_clamped: false },
        },
    });
    assert.equal(mixedClamped.unsplitEngineLabel, 'Server TTFT Residual');
    assert.equal(mixedClamped.rows[0].stat, 'Mean');
    assert.equal(mixedClamped.rows[0].has_pd_sub_split, true);
    assert.equal(mixedClamped.rows[0].engine_overhead_unsplit_ms, null);
    assert.equal(mixedClamped.rows[1].stat, 'P90');
    assert.equal(mixedClamped.rows[1].has_pd_sub_split, false);
    assert.equal(mixedClamped.rows[1].engine_overhead_unsplit_ms, 8200);
    assert.equal(mixedClamped.rows[1].decode_first_step_ms, null);
});

test('buildStageLatencyView and buildTtftDecompositionView normalize v6 slices and handle legacy flat EPP subtraction', () => {
    // 1. v6 unclamped + 10s-clamped normalization inside buildStageLatencyView
    const v6Forensics = {
        schema_version: 6,
        stage_latency_breakdown: {
            available: true,
            source: 'server_telemetry',
            p50: {
                available: true,
                source: 'server_telemetry',
                epp_scheduling_ms: 1.0,
                queue_time_ms: 100.0,
                prefill_time_ms: 400.0,
                decode_time_ms: 900.0,
                gateway_overhead_ms: 500.0,
                server_ttft_ms: 834.5,
                client_ttft_ms: 1001.0,
                ttft_stage_ms: 1001.0,
                total_stage_ms: 1901.0,
                share_of_e2e_pct: { epp_scheduling: 0.1, queue_time: 5.3, prefill_time: 21.0, gateway_overhead: 26.3, decode_time: 47.3 },
                share_of_ttft_pct: { epp_scheduling: 0.1, queue_time: 10.0, prefill_time: 40.0, gateway_overhead: 50.0 },
            },
            p90: {
                available: true,
                source: 'server_telemetry',
                epp_scheduling_ms: 2.0,
                queue_time_ms: 500.0,
                prefill_time_ms: 900.0,
                decode_time_ms: 2000.0,
                gateway_overhead_ms: 12000.0,
                server_ttft_ms: 9800.0,
                client_ttft_ms: 13402.0,
                ttft_stage_ms: 13402.0,
                total_stage_ms: 15402.0,
                share_of_e2e_pct: { gateway_overhead: 77.9 },
                share_of_ttft_pct: { gateway_overhead: 89.5 },
            },
        },
        ttft_decomposition: {
            // Flat v6 entries with stale prefill/overhead must yield to structured normalized slbSlice
            vllm_prefill_p50_ms: 999.0,
            vllm_queue_p50_ms: 999.0,
            overhead_p50_ms: 999.0,
        },
    };
    const v6StageView = buildStageLatencyView(v6Forensics);
    assert.equal(v6StageView.slices.p50.engine_overhead_ms, 334.5);
    assert.equal(v6StageView.slices.p50.gateway_overhead_ms, 165.5);
    assert.equal(v6StageView.slices.p50.share_of_ttft_pct.engine_overhead, 33.4);
    assert.equal(v6StageView.slices.p50.share_of_ttft_pct.gateway_overhead, 16.5);
    // P90 clamped: engine_overhead_ms = 12000, gateway_overhead_ms = null, server_ttft_clamped = true
    assert.equal(v6StageView.slices.p90.engine_overhead_ms, 12000);
    assert.equal(v6StageView.slices.p90.gateway_overhead_ms, null);
    assert.equal(v6StageView.slices.p90.server_ttft_clamped, true);
    assert.equal(v6StageView.slices.p90.share_of_ttft_pct.gateway_overhead, null);

    const v6DecompView = buildTtftDecompositionView(v6Forensics, v6StageView);
    assert.equal(v6DecompView.rows[0].prefill_ms, 400);
    assert.equal(v6DecompView.rows[0].engine_overhead_unsplit_ms, 334.5);
    assert.equal(v6DecompView.rows[0].gateway_overhead_ms, 165.5);
    assert.equal(v6DecompView.rows[1].engine_overhead_unsplit_ms, 12000);
    assert.equal(v6DecompView.rows[1].gateway_overhead_ms, null);
    assert.equal(v6DecompView.unsplitEngineLabel, 'Server TTFT Residual');

    // 2. Explicit engine_overhead_ms: null is NEVER overwritten by v6 normalizer
    const modernNullEng = buildStageLatencyView({
        stage_latency_breakdown: {
            available: true,
            source: 'server_telemetry',
            p50: {
                available: true,
                source: 'server_telemetry',
                queue_time_ms: 10,
                prefill_time_ms: 50,
                decode_time_ms: 200,
                engine_overhead_ms: null,
                gateway_overhead_ms: 20,
                server_ttft_ms: 100,
            },
        },
    });
    assert.equal(modernNullEng.slices.p50.engine_overhead_ms, null);
    assert.equal(modernNullEng.slices.p50.gateway_overhead_ms, 20);

    // 3. Pure-flat legacy format subtracts epp_scheduler_ms from overhead_ms without double-counting
    const pureFlat = buildTtftDecompositionView({
        ttft_decomposition: {
            available: true,
            vllm_prefill_p50_ms: 80,
            vllm_queue_p50_ms: 20,
            epp_scheduler_p50_ms: 5,
            overhead_p50_ms: 30,
        },
    });
    assert.equal(pureFlat.rows[0].epp_scheduling_ms, 5);
    assert.equal(pureFlat.rows[0].gateway_overhead_ms, 25);
    assert.equal(pureFlat.rows[0].residual_ms, 30);
});

test('formatDuration prefers actual benchmark execution duration for finished tasks and wall-clock for running tasks', () => {
    const completedTask = {
        status: 'completed',
        started_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:05:09.7Z',
        result: {
            summary: { duration_seconds: 135.5 },
            client_series: { duration_seconds: 135.528, windows: { full: { duration_seconds: 135.528 } } },
        },
    };
    assert.equal(formatDuration(completedTask, completedTask.result.summary), '2 min 16 s');
    assert.equal(formatDuration(completedTask), '2 min 16 s');

    const clientSeriesOnlyTask = {
        status: 'completed',
        started_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:04:00Z',
        result: {
            client_series: { windows: { full: { duration_seconds: 146.3 } } },
        },
    };
    assert.equal(formatDuration(clientSeriesOnlyTask), '2 min 26 s');

    const wallFallbackTask = {
        status: 'failed',
        started_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:01:05Z',
    };
    assert.equal(formatDuration(wallFallbackTask), '1 min 5 s');

    const runningWithSummary = {
        status: 'running',
        execution_started_at: '2026-01-01T00:00:00Z',
        result: { summary: { duration_seconds: 999 } },
    };
    assert.equal(formatDuration(runningWithSummary, {}, Date.parse('2026-01-01T00:00:45Z')), '45 s');
});

test('taskMetrics prefers normalized benchmark-window throughput and P50 latency with Avg fallback', () => {
    const windowPreferred = taskMetrics({
        status: 'completed',
        result: {
            summary: {
                throughput_rps: 0.5042,
                total_requests: 123,
                failed_requests: 2,
                duration_seconds: 135.517,
                ttft: { mean_ms: 380.5, p50_ms: 206.2 },
                tpot: { mean_ms: 30.6, p50_ms: 28.1 },
                latency: { mean_ms: 13293.4, p50_ms: 9133.5 },
            },
            client_series: {
                windows: {
                    full: {
                        throughput_rps: 0.9076,
                        request_count: 123,
                        failed_requests: 2,
                        ttft_ms: { avg: 380.5, p50: 206.18 },
                        tpot_ms: { avg: 30.61, p50: 28.09 },
                        e2e_ms: { avg: 13293.4, p50: 9133.45 },
                    },
                },
            },
        },
    });
    assert.equal(windowPreferred.requestRate, 0.9076);
    assert.equal(windowPreferred.ttft, 206.18);
    assert.equal(windowPreferred.ttftStat, 'P50');
    assert.equal(windowPreferred.tpot, 28.09);
    assert.equal(windowPreferred.tpotStat, 'P50');
    assert.equal(windowPreferred.e2e, 9133.45);
    assert.equal(windowPreferred.e2eStat, 'P50');

    // Historical saved task without client_series rescues wall-clock-diluted summary.throughput_rps
    const historicalSaved = taskMetrics({
        status: 'completed',
        result: {
            summary: {
                throughput_rps: 0.5042,
                total_requests: 123,
                failed_requests: 2,
                duration_seconds: 135.517,
                ttft: { mean_ms: 380.5 },
                tpot: { mean_ms: 30.6 },
            },
        },
    });
    assert.ok(Math.abs(historicalSaved.requestRate - 123 / 135.517) < 1e-9);
    assert.equal(historicalSaved.ttft, 380.5);
    assert.equal(historicalSaved.ttftStat, 'Avg');
    assert.equal(historicalSaved.tpot, 30.6);
    assert.equal(historicalSaved.tpotStat, 'Avg');
    assert.ok(Math.abs(historicalSaved.errorRate - (2 / 123) * 100) < 1e-9);
});

test('formatDuration and taskMetrics handle queued/pending/partial statuses and zero/missing denominators safely', () => {
    assert.equal(formatDuration({ status: 'queued', started_at: '2026-01-01T00:00:00Z' }, { duration_seconds: 50 }), 'Preparing…');
    assert.equal(formatDuration({ status: 'pending', execution_started_at: '2026-01-01T00:00:00Z' }, {}, Date.parse('2026-01-01T00:00:12Z')), '12 s');
    assert.equal(formatDuration({
        status: 'partial',
        started_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:05:00Z',
        result: { client_series: { duration_seconds: 88.2 } },
    }), '1 min 28 s');

    const zeroDuration = taskMetrics({
        status: 'completed',
        result: {
            summary: {
                throughput_rps: 3.5,
                total_requests: 10,
                failed_requests: 1,
                duration_seconds: 0,
            },
        },
    });
    assert.equal(zeroDuration.requestRate, 3.5);
    assert.equal(zeroDuration.errorRate, 10);

    const zeroTotal = taskMetrics({
        status: 'completed',
        result: {
            summary: {
                total_requests: 0,
                failed_requests: 0,
                duration_seconds: 10,
            },
        },
    });
    assert.equal(zeroTotal.requestRate, null);
    assert.equal(zeroTotal.errorRate, null);

    const missingDuration = taskMetrics({
        status: 'completed',
        result: {
            summary: {
                throughput_rps: 4.2,
                total_requests: 20,
                failed_requests: 5,
            },
        },
    });
    assert.equal(missingDuration.requestRate, 4.2);
    assert.equal(missingDuration.errorRate, 25);
});

test('buildTtftDecompositionView emits canonical 6-slice order, client_unattributed_ms, and guards client_fallback EmptyState', () => {
    const sixSliceDecomp = buildTtftDecompositionView(
        {
            ttft_decomposition: {
                available: true,
                source: 'server_telemetry',
                min: { ttft_unattributed_ms: 82.5, client_ttft_ms: 82.5, slice_note: 'Server stage histograms do not expose single-request minimums; showing exact client Min TTFT.' },
                p50: { prefill_ms: 110, queue_ms: 15, epp_scheduling_ms: 3, engine_overhead_ms: 12, gateway_overhead_ms: 5 },
                mean: { prefill_ms: 130, queue_ms: 25, epp_scheduling_ms: 4, engine_overhead_ms: 16, gateway_overhead_ms: 8 },
                p90: { prefill_ms: 210, queue_ms: 60, epp_scheduling_ms: 6, engine_overhead_ms: 30, gateway_overhead_ms: 12 },
                p99: { prefill_ms: 320, queue_ms: 110, epp_scheduling_ms: 9, engine_overhead_ms: 45, gateway_overhead_ms: 20 },
                max: { prefill_ms: 340, queue_ms: 130, epp_scheduling_ms: 10, engine_overhead_ms: 180, gateway_overhead_ms: null, slice_note: 'Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.' },
            },
        },
        { available: true, source: 'server_telemetry' },
    );

    assert.deepEqual(sixSliceDecomp.rows.map((r) => r.stat), ['Min', 'P50', 'Mean', 'P90', 'P99', 'Max']);
    assert.deepEqual(sixSliceDecomp.rows.map((r) => r.key), ['min', 'p50', 'mean', 'p90', 'p99', 'max']);
    assert.equal(sixSliceDecomp.caveat, null);
    assert.equal(sixSliceDecomp.rows[0].client_unattributed_ms, 82.5);
    assert.equal(sixSliceDecomp.rows[1].client_unattributed_ms, null);
    assert.match(sixSliceDecomp.rows[0].slice_note || '', /single-request minimums/);
    assert.match(sixSliceDecomp.rows[5].slice_note || '', /per-request max/);

    assert.equal(TTFT_BAR_OPACITY_BY_KEY.ttft_unattributed_ms, 1);
    assert.equal(TTFT_BAR_OPACITY_BY_KEY.client_unattributed_ms, 0.38);
    const unattrBar = sixSliceDecomp.barDefs.find((b) => b.key === 'client_unattributed_ms');
    assert.deepEqual(unattrBar, {
        key: 'client_unattributed_ms',
        label: 'Unattributed TTFT',
        colorIndex: 2,
        opacity: 0.38,
    });

    // EmptyState guard: available:false (except pd_pooled_fallback) and client_fallback without decomp
    const unavailableClient = buildTtftDecompositionView(
        { ttft_decomposition: { available: false } },
        { available: true, source: 'client_fallback' },
    );
    assert.equal(unavailableClient.rows.length, 0);
    assert.equal(unavailableClient.barDefs.length, 0);

    const unavailableServer = buildTtftDecompositionView(
        { ttft_decomposition: { available: false } },
        { available: true, source: 'server_telemetry' },
    );
    assert.equal(unavailableServer.rows.length, 0);

    const noDecompClient = buildTtftDecompositionView(
        {},
        { available: true, source: 'client_fallback', slices: { p50: { available: true, ttft_stage_ms: 100 } } },
    );
    assert.equal(noDecompClient.rows.length, 0);

    // pd_pooled_fallback still works when decomp.available === false
    const pdPooledFallback = buildTtftDecompositionView(
        { ttft_decomposition: { available: false } },
        {
            available: true,
            source: 'pd_pooled_fallback',
            slices: {
                min: { available: true, ttft_unattributed_ms: 50, client_ttft_ms: 50 },
                p50: { available: true, ttft_unattributed_ms: 100, kv_transfer_ms: 15, epp_scheduling_ms: 5 },
            },
        },
    );
    assert.deepEqual(pdPooledFallback.rows.map((r) => r.stat), ['Min', 'P50']);
    assert.equal(pdPooledFallback.barDefs.find((b) => b.key === 'ttft_unattributed_ms')?.opacity, 1);
});

test('hasServerLatencyTelemetry gates historical 6-slice synthesis and handles schema-6 NIXL, 10s P99 clamp, and PD-role reconciliation', () => {
    assert.equal(hasServerLatencyTelemetry({ available: true, summary: { kv_cache_usage_percent: { mean: 50 } } }), false);
    assert.equal(hasServerLatencyTelemetry({ available: true, summary: { vllm_prefill_time_p50_ms: { mean: 100 } } }), true);
    assert.equal(hasServerLatencyTelemetry({ available: true, router: { scheduler_latency_p50_ms: { mean: 2 } } }), true);

    // 1. Stub serverTelemetry without latency metrics must NOT synthesize extra slices onto existing slb
    const stubView = buildStageLatencyView(
        {
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                p50: { available: true, prefill_time_ms: 100, queue_time_ms: 20, decode_time_ms: 400, engine_overhead_ms: 10, gateway_overhead_ms: 5 },
                p90: { available: true, prefill_time_ms: 180, queue_time_ms: 40, decode_time_ms: 600, engine_overhead_ms: 20, gateway_overhead_ms: 10 },
            },
        },
        { available: true, summary: { kv_cache_usage_percent: { mean: 50 } } },
        { windows: { profiling: { ttft_ms: { min: 40, avg: 150, p50: 135, p90: 250, p99: 400, max: 520 }, e2e_ms: { min: 200, p50: 535, p90: 850, p99: 1100, max: 1400 } } } },
    );
    assert.equal(stubView.slices.min, null);
    assert.equal(stubView.slices.p99, null);
    assert.equal(stubView.slices.max, null);

    // 2. Legacy schema-6 disaggregated run without summary_by_role:
    //    synthesizes min, p99 (window-P99 with P90 fallback + 10s clamp + NIXL deduction), and max (unbounded tail)
    const schema6Disagg = buildStageLatencyView(
        {
            schema_version: 6,
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_kv_transfer_series: true,
                p50: { available: true, epp_scheduling_ms: 1, queue_time_ms: 20, kv_transfer_ms: 30, prefill_time_ms: 100, gateway_overhead_ms: 49, server_ttft_ms: 180, client_ttft_ms: 200, decode_time_ms: 500 },
                p90: { available: true, epp_scheduling_ms: 2, queue_time_ms: 100, kv_transfer_ms: 80, prefill_time_ms: 300, gateway_overhead_ms: 200, server_ttft_ms: 650, client_ttft_ms: 682, decode_time_ms: 900 },
                mean: { available: true, source: 'client_fallback', ttft_unattributed_ms: 260, ttft_stage_ms: 260, client_ttft_ms: 260, decode_time_ms: 550 },
            },
        },
        {
            available: true,
            status: 'available',
            topology_mode: 'disaggregated',
            metric_sources: { vllm_stage_latencies: 'aggregated_namespace' },
            router: { scheduler_latency_p95_ms: { mean: 4 }, scheduler_latency_p90_ms: { mean: 3 } },
            summary: {
                vllm_queue_time_p90_ms: { mean: 300, max: 900 },
                vllm_prefill_time_p90_ms: { mean: 400, max: 1200 },
                vllm_decode_time_p90_ms: { mean: 1000, max: 1800 },
                nixl_transfer_latency_p99_ms: { mean: 150, max: 250 },
                vllm_ttft_p99_ms: { mean: 5000, max: 9900 },
                vllm_e2e_p99_ms: { mean: 7000, max: 12000 },
            },
            series: [
                { elapsed_seconds: 5, router_scheduler_latency_p95_ms: 1, vllm_queue_time_p90_ms: 50, vllm_prefill_time_p90_ms: 100, nixl_transfer_latency_p99_ms: 20, vllm_ttft_p99_ms: 500 },
                { elapsed_seconds: 20, router_scheduler_latency_p95_ms: 5, vllm_queue_time_p90_ms: 800, vllm_prefill_time_p90_ms: 1100, vllm_decode_time_p90_ms: 1600, nixl_transfer_latency_p99_ms: 220, vllm_ttft_p99_ms: 9850 },
                { elapsed_seconds: 30, router_scheduler_latency_p95_ms: 4, vllm_queue_time_p90_ms: 600, vllm_prefill_time_p90_ms: 900, vllm_decode_time_p90_ms: 1400, nixl_transfer_latency_p99_ms: 180, vllm_ttft_p99_ms: 8500 },
            ],
        },
        {
            warmup_seconds: 15,
            windows: {
                profiling: {
                    ttft_ms: { min: 65, avg: 260, p50: 200, p90: 682, p99: 15000, max: 18000 },
                    e2e_ms: { min: 300, avg: 810, p50: 700, p90: 1582, p99: 17000, max: 20500 },
                },
            },
        },
    );
    assert.equal(schema6Disagg.slices.min.ttft_unattributed_ms, 65);
    assert.equal(schema6Disagg.slices.p99.kv_transfer_ms, 220);
    assert.equal(schema6Disagg.slices.p99.queue_time_ms, 580);
    assert.equal(schema6Disagg.slices.p99.prefill_time_ms, 1100);
    assert.equal(schema6Disagg.slices.p99.server_ttft_clamped, true);
    assert.equal(schema6Disagg.slices.p99.gateway_overhead_ms, null);
    assert.match(schema6Disagg.caveat || '', /10 s histogram ceiling/);
    assert.match(schema6Disagg.slices.p99.slice_note || '', /peak-window P90/);
    assert.equal(schema6Disagg.slices.max.gateway_overhead_ms, null);
    assert.match(schema6Disagg.slices.max.slice_note || '', /per-request max/);

    const schema6Decomp = buildTtftDecompositionView(
        { schema_version: 6, ttft_decomposition: { available: true, source: 'server_telemetry', mean: { available: false, source: 'client_fallback', client_ttft_ms: 260, ttft_unattributed_ms: 260 } } },
        schema6Disagg,
    );
    assert.deepEqual(schema6Decomp.rows.map((r) => r.stat), ['Min', 'P50', 'Mean', 'P90', 'P99', 'Max']);
    assert.equal(schema6Decomp.rows.find((r) => r.stat === 'Mean').client_unattributed_ms, 260);
    assert.equal(schema6Decomp.rows.find((r) => r.stat === 'Min').client_unattributed_ms, 65);

    // 3. PD-role run: synthesizes p99/max with 305ms queue floor bypass, P/D sub-split, and over-allocation guard
    const pdRoleStage = buildStageLatencyView(
        {
            schema_version: 8,
            stage_latency_breakdown: {
                available: true,
                source: 'server_telemetry',
                has_kv_transfer_series: true,
                p50: { available: true, epp_scheduling_ms: 0.3, queue_time_ms: 0.02, kv_transfer_ms: 300, prefill_time_ms: 350, decode_first_step_ms: 35, prefill_engine_overhead_ms: 120, decode_handoff_overhead_ms: 130, engine_overhead_ms: 285, gateway_overhead_ms: 0, decode_time_ms: 4000, server_ttft_clamped: false },
                mean: { available: true, epp_scheduling_ms: 0.3, queue_time_ms: 0.02, kv_transfer_ms: 400, prefill_time_ms: 450, decode_first_step_ms: 40, prefill_engine_overhead_ms: 150, decode_handoff_overhead_ms: 160, engine_overhead_ms: 350, gateway_overhead_ms: 0, decode_time_ms: 4500, server_ttft_clamped: false },
                p90: { available: true, epp_scheduling_ms: 0.5, queue_time_ms: 0.02, kv_transfer_ms: 900, prefill_time_ms: 1200, decode_first_step_ms: 200, prefill_engine_overhead_ms: 400, decode_handoff_overhead_ms: 500, engine_overhead_ms: 1100, gateway_overhead_ms: 50, decode_time_ms: 8000, server_ttft_clamped: false },
            },
        },
        {
            available: true,
            status: 'available',
            topology_mode: 'disaggregated',
            metric_sources: { vllm_stage_latencies: 'role_separated' },
            router: { scheduler_latency_p99_ms: { p99: 1.0, max: 2.0 } },
            summary: {
                nixl_transfer_latency_p99_ms: { p99: 1500, max: 1800 },
                vllm_ttft_p99_ms: { p99: 5200, max: 6000 },
                vllm_e2e_p99_ms: { p99: 14000, max: 16000 },
            },
            summary_by_role: {
                prefill: {
                    vllm_queue_time_mean_ms: { mean: 0.02 },
                    vllm_queue_time_p99_ms: { p99: 297.0, max: 299.0 },
                    vllm_prefill_time_p99_ms: { p99: 2000, max: 2200 },
                    vllm_ttft_p99_ms: { p99: 3100, max: 3500 },
                },
                decode: {
                    vllm_queue_time_mean_ms: { mean: 400 },
                    vllm_queue_time_p99_ms: { p99: 1600, max: 1900 },
                    vllm_prefill_time_p99_ms: { p99: 280, max: 295 },
                    vllm_decode_time_p99_ms: { p99: 9000, max: 10000 },
                    vllm_ttft_p99_ms: { p99: 2400, max: 2800 },
                },
            },
        },
        {
            windows: {
                full: {
                    ttft_ms: { min: 120, avg: 1200.32, p50: 935.32, p90: 3250.52, p99: 3400, max: 5900 },
                    e2e_ms: { min: 900, avg: 5700.32, p50: 4935.32, p90: 11250.52, p99: 12400, max: 15900 },
                },
            },
        },
    );
    // p99: prefill queue 297 <= 305 with mean 0.02 -> bypassed to 0.02; raw known = 1 + (0.02 + 100) + 1500 + 2000 = 3601.02 > 3400 -> scaled to 3400!
    assert.equal(pdRoleStage.slices.p99.ttft_stage_ms, 3400);
    assert.equal(pdRoleStage.slices.p99.engine_overhead_ms, 0);
    assert.equal(pdRoleStage.slices.p99.slice_note, null);
    // max: server_ttft_p99.max (6000) >= client_ttft_max (5900) -> P/D sub-split runs cleanly!
    assert.ok(pdRoleStage.slices.max.decode_first_step_ms > 0);
    assert.ok(pdRoleStage.slices.max.prefill_engine_overhead_ms > 0);
    assert.ok(pdRoleStage.slices.max.decode_handoff_overhead_ms > 0);
});
