import {
    deploymentBenchmarkRequest,
    derivePodStatusGroup,
    effectiveStatusGroup,
    effectiveStatusLabel,
    expectedModelPodCount,
    formatPodStatusLabel,
    isFailingPod,
    successfulPagePods,
    summarizeStatuses,
} from './deploymentPresentation.js';

test('summary receives only successful results from the currently polled page', () => {
    const cached = { current: [], failed: [], stale: [] };
    assert.deepEqual(successfulPagePods(['current', 'failed', 'unpolled'], cached, { failed: 'Forbidden' }), { current: [] });
    assert.deepEqual(successfulPagePods([], cached, {}), {});
    assert.deepEqual(successfulPagePods(['failed'], cached, {}), { failed: [] });
});
import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { DeleteDeploymentModal, executeDeploymentDeleteAction } from './DeleteDeploymentModal.jsx';
import { PodStatusBadge } from './PodStatusBadge.jsx';


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

test('DeleteDeploymentModal intercepts 409 deployment_cleanup_permission_denied and renders cleanup handoff modal', async () => {
    let capturedHandoff = null;
    let capturedError = '';
    const outcome = await executeDeploymentDeleteAction({
        deleteNamespace: true,
        onDelete: async () => {
            throw Object.assign(new Error('Orchestration administrator cleanup required'), {
                status: 409,
                code: 'deployment_cleanup_permission_denied',
                body: {
                    status: 409,
                    code: 'deployment_cleanup_permission_denied',
                    executionId: 'exec-12345678',
                    clusterId: 'cluster-1',
                    namespace: 'llmd-qwen-prod',
                    phase1Completed: true,
                    deletedInNamespace: ['Deployment/qwen-prod'],
                    adminCleanup: {
                        cleanupDigest: 'digest-1',
                        script: '#!/usr/bin/env bash\nkubectl delete ns llmd-qwen-prod',
                        scriptFilename: 'cleanup-llmd-qwen-prod.sh',
                        operations: [{ id: 'delete-ns', kind: 'Namespace', name: 'llmd-qwen-prod' }],
                        completedOperations: ['Deployment/qwen-prod'],
                        marker: { namespace: 'lens-edit', name: 'lens-deploy-cleanup-1', status: 'missing' },
                        canVerifyMarker: true,
                        confirmRequired: false,
                    },
                },
            });
        },
        onCleanupHandoff: (handoff) => { capturedHandoff = handoff; },
        onError: (msg) => { capturedError = msg; },
    });
    assert.equal(outcome.status, 'handoff');
    assert.equal(capturedError, '');
    assert.equal(capturedHandoff?.namespace, 'llmd-qwen-prod');
    assert.equal(capturedHandoff?.canVerifyMarker, true);

    const deployment = {
        execution_id: 'exec-12345678',
        display_name: 'qwen-prod',
        cluster_id: 'cluster-1',
        namespace: 'llmd-qwen-prod',
        status: 'ready',
    };
    const handoffHtml = renderToStaticMarkup(
        React.createElement(DeleteDeploymentModal, {
            deployment,
            inline: true,
            onCancel: () => {},
            onDelete: async () => {},
            initialCleanupHandoff: capturedHandoff,
        }),
    );
    assert.match(handoffHtml, /Orchestration administrator namespace cleanup/);
    assert.match(handoffHtml, /Re-check &amp; Delete/);
    assert.match(handoffHtml, /Download \.sh/);
    assert.match(handoffHtml, /Copy script/);
    assert.match(handoffHtml, />Remaining</);
    assert.doesNotMatch(handoffHtml, />Done</);
    assert.doesNotMatch(handoffHtml, /Confirm &amp; Delete Record/);
    assert.match(handoffHtml, /<details class="group\/details[^"]*" open="">/);
    assert.match(handoffHtml, /<details class="group\/handoff-op">/);
    assert.match(handoffHtml, /group-open\/handoff-op:rotate-90/);
    assert.doesNotMatch(handoffHtml, /<details class="group"/);
    assert.doesNotMatch(handoffHtml, /<details class="group\/handoff-op"[^>]*open/);

    const partialRecheckHtml = renderToStaticMarkup(
        React.createElement(DeleteDeploymentModal, {
            deployment,
            inline: true,
            onCancel: () => {},
            onDelete: async () => {},
            initialCleanupHandoff: {
                ...capturedHandoff,
                operations: [
                    { id: 'delete-crb', kind: 'ClusterRoleBinding', name: 'epp-crb' },
                    { id: 'delete-ns', kind: 'Namespace', name: 'llmd-qwen-prod' },
                ],
                remainingOperations: ['delete-ns'],
            },
        }),
    );
    assert.match(partialRecheckHtml, />Done</);
    assert.match(partialRecheckHtml, />Remaining</);
    assert.match(partialRecheckHtml, /<details class="group\/details[^"]*" open="">/);
    assert.match(partialRecheckHtml, /<details class="group\/handoff-op">/);
    assert.doesNotMatch(partialRecheckHtml, /<details class="group"/);
    assert.doesNotMatch(partialRecheckHtml, /<details class="group\/handoff-op"[^>]*open/);

    const fallbackHtml = renderToStaticMarkup(
        React.createElement(DeleteDeploymentModal, {
            deployment,
            inline: true,
            onCancel: () => {},
            onDelete: async () => {},
            initialCleanupHandoff: {
                ...capturedHandoff,
                canVerifyMarker: false,
                confirmRequired: true,
            },
        }),
    );
    assert.match(fallbackHtml, /Confirm &amp; Delete Record/);
    assert.match(fallbackHtml, /<details class="group\/details[^"]*" open="">/);
    assert.match(fallbackHtml, /<details class="group\/handoff-op">/);
    assert.doesNotMatch(fallbackHtml, /<details class="group"/);
    assert.doesNotMatch(fallbackHtml, /<details class="group\/handoff-op"[^>]*open/);

    let genericError = '';
    const errOutcome = await executeDeploymentDeleteAction({
        deleteNamespace: true,
        onDelete: async () => { throw new Error('Cluster unreachable'); },
        onCleanupHandoff: () => { throw new Error('should not be called'); },
        onError: (msg) => { genericError = msg; },
    });
    assert.equal(errOutcome.status, 'error');
    assert.equal(genericError, 'Cluster unreachable');
});

test('isFailingPod handles Init:-prefixed failure reasons and ignores startup/terminal states', () => {
    for (const status_reason of ['Init:0/1', 'Init:1/2', 'PodInitializing', 'ContainerCreating', 'Starting', 'Running', 'Completed', 'Terminating']) {
        const candidate = pod({ phase: 'Pending', ready: false, status_reason, restarts: 5 });
        assert.equal(isFailingPod(candidate), false, `${status_reason} should not be failing`);
        assert.equal(effectiveStatusGroup({ status: 'deploying' }, [candidate]), 'In progress');
    }

    for (const status_reason of ['Init:ImagePullBackOff', 'Init:OOMKilled', 'Init:ErrImagePull']) {
        const candidate = pod({ phase: 'Pending', ready: false, status_reason, restarts: 0 });
        assert.equal(isFailingPod(candidate), true, `${status_reason} should fail immediately`);
        assert.equal(effectiveStatusGroup({ status: 'deploying' }, [candidate]), 'Failed');
    }

    for (const status_reason of ['Init:CrashLoopBackOff', 'Init:Error', 'Init:ExitCode:1', 'Init:Signal:9']) {
        const belowThreshold = pod({ phase: 'Pending', ready: false, status_reason, restarts: 2 });
        assert.equal(isFailingPod(belowThreshold), false, `${status_reason} below threshold`);
        assert.equal(effectiveStatusGroup({ status: 'deploying' }, [belowThreshold]), 'In progress');

        const atThreshold = pod({ phase: 'Pending', ready: false, status_reason, restarts: 3 });
        assert.equal(isFailingPod(atThreshold), true, `${status_reason} at threshold`);
        assert.equal(effectiveStatusGroup({ status: 'deploying' }, [atThreshold]), 'Failed');
    }
});

test('formatPodStatusLabel shows Starting only for Running non-ready pods without a distinct reason', () => {
    assert.equal(formatPodStatusLabel({ phase: 'Running', ready: false, status_reason: 'Running' }), 'Starting');
    assert.equal(formatPodStatusLabel({ phase: 'Running', ready: false, reason: 'Running' }), 'Starting');
    assert.equal(formatPodStatusLabel({ phase: 'Running', ready: false, status_reason: '' }), 'Starting');
    assert.equal(formatPodStatusLabel({ phase: 'Running', ready: true, status_reason: 'Running' }), 'Running');
    assert.equal(formatPodStatusLabel({ phase: 'Pending', ready: false, status_reason: 'Init:0/1' }), 'Init:0/1');
    assert.equal(formatPodStatusLabel({ phase: 'Pending', ready: false, status_reason: 'ContainerCreating' }), 'ContainerCreating');
    assert.equal(formatPodStatusLabel({ phase: 'Succeeded', ready: false, status_reason: 'Completed' }), 'Completed');
    assert.equal(formatPodStatusLabel({ phase: 'Running', ready: false, status_reason: 'Terminating' }), 'Terminating');
    assert.equal(formatPodStatusLabel({ phase: 'Running', ready: false, status_reason: 'CrashLoopBackOff' }), 'CrashLoopBackOff');
});

test('PodStatusBadge renders accurate colors, precedence, and Starting tooltip', () => {
    const renderBadge = (props) => renderToStaticMarkup(React.createElement(PodStatusBadge, props));

    const startingHtml = renderBadge({ phase: 'Running', reason: 'Running', ready: false });
    assert.match(startingHtml, /bg-sky-400 animate-pulse/);
    assert.match(startingHtml, /ring-sky-500\/30/);
    assert.doesNotMatch(startingHtml, /bg-amber-400/);
    assert.match(startingHtml, /title="Status: Starting"/);
    assert.match(startingHtml, />Starting</);

    for (const reason of ['CrashLoopBackOff', 'OOMKilled', 'ImagePullBackOff', 'Error']) {
        const runningFailureHtml = renderBadge({ phase: 'Running', reason, ready: false });
        assert.match(runningFailureHtml, /bg-rose-500/, `${reason} in Running phase should render rose`);
        assert.match(runningFailureHtml, /ring-rose-500\/30/);
        assert.doesNotMatch(runningFailureHtml, /bg-amber-400/);
        assert.match(runningFailureHtml, new RegExp(`title="Status: ${reason}"`));
    }

    for (const reason of ['Init:0/1', 'ContainerCreating', 'PodInitializing']) {
        const pendingHtml = renderBadge({ phase: 'Pending', reason, ready: false });
        assert.match(pendingHtml, /bg-cyan-400 animate-pulse/, `${reason} should render pulsing cyan`);
        assert.match(pendingHtml, /ring-cyan-500\/30/);
    }

    for (const reason of ['Init:ImagePullBackOff', 'Init:OOMKilled', 'Init:CrashLoopBackOff', 'Init:ExitCode:1', 'Init:Signal:9']) {
        const initFailureHtml = renderBadge({ phase: 'Pending', reason, ready: false });
        assert.match(initFailureHtml, /bg-rose-500/, `${reason} should render rose`);
        assert.match(initFailureHtml, /ring-rose-500\/30/);
    }

    const terminatingHtml = renderBadge({ phase: 'Running', reason: 'Terminating', ready: false });
    assert.match(terminatingHtml, /bg-slate-400 animate-pulse/);
    assert.match(terminatingHtml, /ring-slate-400\/30/);

    const completedHtml = renderBadge({ phase: 'Succeeded', reason: 'Completed', ready: false });
    assert.match(completedHtml, /bg-slate-400 /);
    assert.doesNotMatch(completedHtml, /animate-pulse/);
    assert.doesNotMatch(completedHtml, /bg-amber-400/);
    assert.match(completedHtml, /ring-slate-400\/30/);
    assert.match(completedHtml, />Completed</);

    const readyHtml = renderBadge({ phase: 'Running', reason: 'Running', ready: true });
    assert.match(readyHtml, /bg-emerald-400/);
    assert.match(readyHtml, /ring-emerald-500\/30/);
    assert.match(readyHtml, /title="Status: Running \(Ready\)"/);
    assert.match(readyHtml, />Running \(Ready\)</);
});
