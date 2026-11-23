import { deploymentBenchmarkRequest, derivePodStatusGroup, effectiveStatusGroup, effectiveStatusLabel, expectedModelPodCount, successfulPagePods, summarizeStatuses } from './deploymentPresentation.js';

test('summary receives only successful results from the currently polled page', () => {
    const cached = { current: [], failed: [], stale: [] };
    assert.deepEqual(successfulPagePods(['current', 'failed', 'unpolled'], cached, { failed: 'Forbidden' }), { current: [] });
    assert.deepEqual(successfulPagePods([], cached, {}), {});
    assert.deepEqual(successfulPagePods(['failed'], cached, {}), { failed: [] });
});
import assert from 'node:assert/strict';
import test from 'node:test';


test('benchmark requests target the execution with a preset sweep and optional P95 targets', () => {
    const request = deploymentBenchmarkRequest({ execution_id: 'exec-1' }, { preset: 'long-input', ttftMs: '500', tpotMs: '' });
    assert.equal(request.deployment_execution_id, 'exec-1');
    assert.deepEqual(request.matrix, [{ isl: 8192, osl: 256 }]);
    assert.deepEqual(request.concurrency_stages.map((stage) => stage.concurrency), [1, 8, 16]);
    assert.ok(request.concurrency_stages.every((stage) => stage.num_requests >= 20));
    assert.deepEqual(request.sla_targets, { ttft_ms: 500, ttft_percentile: 'p95', tpot_ms: null, tpot_percentile: 'p95' });
    assert.equal('model_service_group_id' in request, false);
});

const pod = (overrides = {}) => ({
    name: 'model',
    phase: 'Running',
    ready: true,
    status_reason: 'Running',
    restarts: 0,
    ...overrides,
});

test('a completed (calibration) pod does not mark a ready deployment not-ready', () => {
    const pods = [
        pod(),
        { name: 'calibrate-peak-throughput-9dgr6', phase: 'Succeeded', ready: false, status_reason: 'Completed', restarts: 0 },
        pod({ name: 'epp' }),
    ];

    assert.equal(derivePodStatusGroup(pods), 'Ready');
    assert.equal(effectiveStatusGroup({ status: 'ready' }, pods), 'Ready');
    assert.equal(effectiveStatusLabel({ status: 'ready' }, pods), 'READY');
});

test('only terminal pods yield no conclusive pod status', () => {
    assert.equal(derivePodStatusGroup([{ phase: 'Succeeded', ready: false }]), null);
});

test('normal model startup stays in progress in the label and summary', () => {
    const deployment = { execution_id: 'starting', status: 'deploying' };
    for (const pods of [[], [pod({ ready: false })], [pod({ phase: 'Pending', ready: false })], [pod({ name: 'model-epp' })]]) {
        assert.equal(effectiveStatusGroup(deployment, pods), 'In progress');
        assert.equal(effectiveStatusLabel(deployment, pods), 'DEPLOYING');
        const summary = summarizeStatuses([deployment], { starting: pods });
        assert.equal(summary['In progress'], 1);
        assert.equal(summary.Failed, 0);
    }
});

test('summary keeps the backend status for deployments whose pods were never polled', () => {
    const summary = summarizeStatuses([{ execution_id: 'other-page', status: 'ready' }, { execution_id: 'visible', status: 'ready' }], { visible: [] });
    assert.equal(summary.Ready, 1);
    assert.equal(summary.Failed, 1);
});

test('genuine pod failures are not hidden during deployment startup', () => {
    for (const status_reason of ['ImagePullBackOff', 'OOMKilled', 'CrashLoopBackOff']) {
        const pods = [pod({ ready: false, status_reason, restarts: 3 })];
        assert.equal(effectiveStatusGroup({ status: 'deploying' }, pods), 'Failed');
        assert.equal(effectiveStatusLabel({ status: 'deploying' }, pods), 'DEPLOYING · POD ERROR');
    }
});

test('a running not-ready pod still downgrades a ready deployment', () => {
    const pods = [pod(), pod({ name: 'epp', ready: false })];

    assert.equal(derivePodStatusGroup(pods), 'In progress');
    assert.equal(effectiveStatusLabel({ status: 'ready' }, pods), 'READY · CAPACITY REDUCED');
});

test('a completed pod does not hide a genuine pod failure', () => {
    const pods = [
        { name: 'calibrate-peak-throughput-9dgr6', phase: 'Succeeded', ready: false },
        pod({ name: 'model', phase: 'Failed', ready: false, status_reason: 'Failed' }),
    ];

    assert.equal(derivePodStatusGroup(pods), 'Failed');
    assert.equal(effectiveStatusLabel({ status: 'ready' }, pods), 'READY · POD ERROR');
});

test('no pods make a ready deployment unavailable', () => {
    assert.equal(effectiveStatusGroup({ status: 'ready' }, []), 'Failed');
    assert.equal(effectiveStatusLabel({ status: 'ready' }, []), 'READY · POD UNAVAILABLE');
});

test('an EPP-only namespace makes a ready deployment unavailable', () => {
    const pods = [pod({ name: 'optimized-baseline-epp-abc', labels: { 'llm-d-router-gateway': 'optimized-baseline-epp' } })];

    assert.equal(effectiveStatusGroup({ status: 'ready' }, pods), 'Failed');
});

test('fewer ready model-server pods than requested replicas is degraded', () => {
    const pods = [
        pod({ name: 'decode-a', labels: { 'llm-d.ai/role': 'decode', 'llm-d.ai/inference-serving': 'true' } }),
        pod({ name: 'decode-b', phase: 'Pending', ready: false, labels: { 'llm-d.ai/role': 'decode' } }),
    ];

    assert.equal(effectiveStatusGroup({ status: 'ready', replicas: 2 }, pods), 'Degraded');
    assert.equal(effectiveStatusLabel({ status: 'ready', replicas: 2 }, pods), 'READY · CAPACITY REDUCED');
});

test('expected model pods include prefill and decode replicas', () => {
    assert.equal(expectedModelPodCount({ replicas: 2 }), 2);
    assert.equal(expectedModelPodCount({ replicas: 2, scale: { prefillReplicas: 1, decodeReplicas: 2 } }), 3);
    assert.equal(expectedModelPodCount({ replicas: 2, scale: { prefillReplicas: 1, decodeReplicas: 2, slices: 2 } }), 6);
    assert.equal(expectedModelPodCount({ replicas: 1, scale: { prefillReplicas: null, decodeReplicas: 1 } }), 1);
});

test('a P/D deployment is degraded when one of three expected model pods is missing', () => {
    const pods = [
        pod({ name: 'prefill-a', labels: { 'llm-d.ai/role': 'prefill', 'llm-d.ai/inference-serving': 'true' } }),
        pod({ name: 'decode-a', labels: { 'llm-d.ai/role': 'decode', 'llm-d.ai/inference-serving': 'true' } }),
        pod({ name: 'decode-b', phase: 'Pending', ready: false, labels: { 'llm-d.ai/role': 'decode' } }),
    ];

    assert.equal(expectedModelPodCount({ replicas: 2, scale: { prefillReplicas: 1, decodeReplicas: 2 } }), 3);
    assert.equal(expectedModelPodCount({ replicas: 2, scale: { prefillReplicas: 1, decodeReplicas: 2, slices: 2 } }), 6);
    assert.equal(effectiveStatusGroup({ status: 'ready', replicas: 2, scale: { prefillReplicas: 1, decodeReplicas: 2 } }, pods), 'Degraded');
});
