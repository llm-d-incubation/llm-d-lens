import assert from 'node:assert/strict';
import test from 'node:test';
import { deploymentRequiresReview, prepareLocalDeployment, executePreparedDeployment } from './remoteDeployBackend.js';

test('DS plans require review and execution preserves the reviewed digest', async t => {
    assert.equal(deploymentRequiresReview([{ content: { officialGuide: { renderedManifest: 'kind: List\nitems:\n- kind: DisaggregatedSet\n' } } }]), true);
    assert.equal(deploymentRequiresReview([{ content: { officialGuide: { renderedManifest: 'kind: Deployment\n' } } }]), false);
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        calls.push([url, JSON.parse(options.body)]);
        return Response.json({ id: 'plan', digest: 'sha256:reviewed' });
    });
    const request = { configurations: [{ content: { disaggregatedSet: { slices: 2 } } }], provenance: { cluster_server_id: 'cluster' } };
    const plan = await prepareLocalDeployment(request);
    await executePreparedDeployment(request, plan, true);
    assert.equal(calls[0][0], '/api/v1/deployments/plans');
    assert.equal(calls[1][1].expected_plan_digest, 'sha256:reviewed');
    assert.equal(calls[1][1].acknowledge_unknown, true);
    assert.deepEqual(calls[1][1].configurations, request.configurations);
});
import { listDeploymentExecutions, getDeploymentExecutions, listDeploymentNodeLabels, startLocalDeployment, stopLocalDeploymentCase, deleteDeploymentExecution, prepareDeploymentRestart } from './remoteDeployBackend.js';

test('Advanced deployment binds placement and loads labels for the selected cluster', async (t) => {
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        calls.push([url, options]);
        return Response.json(url.includes('node-labels') ? { items: [{ key: 'pool', value: 'gpu' }] } : { id: 'run-1' });
    });
    assert.deepEqual(await listDeploymentNodeLabels('cluster/a'), [{ key: 'pool', value: 'gpu' }]);
    assert.match(calls[0][0], /cluster_id=cluster%2Fa/);
    await startLocalDeployment([{ provider_ref: 'baseline-vllm' }], { cluster_server_id: 'cluster/a' }, {
        replicas: 'same-node', nodeLabel: { key: 'pool', value: 'gpu' },
    });
    assert.deepEqual(JSON.parse(calls[1][1].body).runtime_binding.placement, {
        replicas: 'same-node', nodeLabel: { key: 'pool', value: 'gpu' },
    });
    const configurations = [
        { provider_ref: 'baseline-vllm', content: { serving: { replicas: 1 } } },
        { provider_ref: 'baseline-vllm', content: { serving: { replicas: 2 } } },
    ];
    await startLocalDeployment(configurations, { cluster_server_id: 'cluster/a' }, { replicas: 'balanced' });
    assert.equal(calls[2][0], '/api/v1/deployments/runs');
    assert.deepEqual(JSON.parse(calls[2][1].body).configurations, configurations);
    assert.deepEqual(JSON.parse(calls[2][1].body).runtime_binding.placement, { replicas: 'balanced' });
});

test('deployment list preserves repeated status filters and response validation', async (t) => {
    const signal = new AbortController().signal;
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        const query = new URL(url, 'http://localhost').searchParams;
        assert.deepEqual(query.getAll('statuses'), ['ready', 'failed']);
        assert.equal(options.signal, signal);
        return Response.json({ items: 'invalid' });
    });
    await assert.rejects(listDeploymentExecutions({ statuses: ['ready', 'failed'], signal }), /list response is invalid/);
});

test('endpoint reconnection failure retains ready deployment', async (t) => {
    t.mock.method(globalThis, 'fetch', async url => url.includes('/endpoint')
        ? Response.json({ detail: 'offline' }, { status: 503 })
        : Response.json({ items: [{ execution_id: 'one', endpoint: 'http://service' }] }));
    assert.deepEqual(await getDeploymentExecutions({ status: 'ready' }), [{ execution_id: 'one', endpoint: 'http://service', endpoint_health: 'unknown' }]);
});

test('stop binds session before action', async (t) => {
    const original = globalThis.sessionStorage;
    globalThis.sessionStorage = { getItem: () => 'session-1' };
    t.after(() => { if (original === undefined) delete globalThis.sessionStorage; else globalThis.sessionStorage = original; });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push([url, JSON.parse(options.body)]); return Response.json({}); });
    await stopLocalDeploymentCase('run/a', 'case/b');
    assert.match(calls[0][0], /run%2Fa\/cluster-session$/);
    assert.deepEqual(calls[0][1], { cluster_session_id: 'session-1' });
    assert.match(calls[1][0], /case%2Fb\/stop$/);
    assert.equal(calls.length, 2);
});

test('delete retains strict 204 contract and structured errors', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }));
    assert.equal(await deleteDeploymentExecution('one'), undefined);
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { message: 'Busy', code: 'busy' } }, { status: 409 }));
    await assert.rejects(deleteDeploymentExecution('one'), error => error.status === 409 && error.code === 'busy');
    t.mock.method(globalThis, 'fetch', async () => Response.json({}));
    await assert.rejects(deleteDeploymentExecution('one'), error => error.status === 200);
});

test('delete rebinds the cluster session first when a run id is given', async (t) => {
    const original = globalThis.sessionStorage;
    globalThis.sessionStorage = { getItem: () => 'session-1' };
    t.after(() => { if (original === undefined) delete globalThis.sessionStorage; else globalThis.sessionStorage = original; });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url) => {
        calls.push(url);
        return url.includes('/cluster-session') ? Response.json({}) : new Response(null, { status: 204 });
    });
    await deleteDeploymentExecution('one', { runId: 'run/a' });
    assert.match(calls[0], /run%2Fa\/cluster-session$/);
    assert.match(calls[1], /executions\/one$/);
});

test('delete without a run id skips rebinding, matching existing no-arg callers', async (t) => {
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url) => { calls.push(url); return new Response(null, { status: 204 }); });
    await deleteDeploymentExecution('one');
    assert.equal(calls.length, 1);
    assert.match(calls[0], /executions\/one$/);
});

test('pending deployment run lookup forwards caller cancellation', async (t) => {
    const { searchLocalDeploymentRuns } = await import('./remoteDeployBackend.js');
    const controller = new AbortController();
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.match(url, /query=a%20b/);
        assert.equal(options.signal, controller.signal);
        return Response.json([]);
    });
    assert.deepEqual(await searchLocalDeploymentRuns('a b', { signal: controller.signal }), []);
});

test('executePreparedDeployment preserves 403 deployment_admin_setup_required status, code, and body', async (t) => {
    const body = {
        code: 'deployment_admin_setup_required',
        detail: 'Orchestration administrator setup is required before deploying.',
        namespace: 'llmd-ds-1',
        identity: { kind: 'ServiceAccount', name: 'lens-edit', namespace: 'lens-edit', overrideRequired: false },
    };
    t.mock.method(globalThis, 'fetch', async () => Response.json(body, { status: 403 }));
    await assert.rejects(
        executePreparedDeployment(
            { configurations: [{ content: { disaggregatedSet: { slices: 2 } } }], provenance: { cluster_server_id: 'cluster-a' } },
            { id: 'plan-1', digest: 'sha256:plan-1' },
            false,
        ),
        (error) => error.status === 403 && error.code === 'deployment_admin_setup_required' && error.body?.namespace === 'llmd-ds-1',
    );
});

function mockSessionStorage(t, initial = {}) {
    const original = globalThis.sessionStorage;
    const store = new Map(Object.entries(initial));
    const setCalls = [];
    globalThis.sessionStorage = {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => {
            setCalls.push([key, value]);
            store.set(key, String(value));
        },
        removeItem: (key) => store.delete(key),
    };
    t.after(() => {
        if (original === undefined) delete globalThis.sessionStorage;
        else globalThis.sessionStorage = original;
    });
    return { store, setCalls };
}

test('delete acquires target cluster session when sessionStorage points to another cluster without clobbering it', async (t) => {
    const { store, setCalls } = mockSessionStorage(t, {
        prism_cluster_session_id: 'session-b',
        prism_cluster_server_id: 'cluster-b',
    });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        calls.push([url, options.method || 'GET', options.body ? JSON.parse(options.body) : null]);
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-a' });
        }
        if (url === '/api/v1/deployments/runs/run-1/cluster-session') {
            return new Response(null, { status: 204 });
        }
        if (url === '/api/v1/deployments/executions/exec-1') {
            return new Response(null, { status: 204 });
        }
        throw new Error(`Unexpected fetch: ${url}`);
    });

    await deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' });

    assert.deepEqual(calls, [
        ['/api/cluster/session?clusterId=cluster-a', 'GET', null],
        ['/api/v1/deployments/runs/run-1/cluster-session', 'POST', { cluster_session_id: 'session-a' }],
        ['/api/v1/deployments/executions/exec-1', 'DELETE', null],
    ]);
    assert.deepEqual(setCalls, []);
    assert.equal(store.get('prism_cluster_session_id'), 'session-b');
    assert.equal(store.get('prism_cluster_server_id'), 'cluster-b');
});

test('delete falls back once to fresh cluster session when stored session is stale', async (t) => {
    mockSessionStorage(t, {
        prism_cluster_session_id: 'session-stale',
        prism_cluster_server_id: 'cluster-a',
    });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        calls.push([url, options.method || 'GET', body]);
        if (url === '/api/v1/deployments/runs/run-1/cluster-session' && body?.cluster_session_id === 'session-stale') {
            return Response.json({ detail: 'cluster session is no longer active' }, { status: 409 });
        }
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ session_id: 'session-fresh' });
        }
        if (url === '/api/v1/deployments/runs/run-1/cluster-session' && body?.cluster_session_id === 'session-fresh') {
            return new Response(null, { status: 204 });
        }
        if (url === '/api/v1/deployments/executions/exec-1') {
            return new Response(null, { status: 204 });
        }
        throw new Error(`Unexpected fetch: ${url}`);
    });

    await deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' });

    assert.deepEqual(calls, [
        ['/api/v1/deployments/runs/run-1/cluster-session', 'POST', { cluster_session_id: 'session-stale' }],
        ['/api/cluster/session?clusterId=cluster-a', 'GET', null],
        ['/api/v1/deployments/runs/run-1/cluster-session', 'POST', { cluster_session_id: 'session-fresh' }],
        ['/api/v1/deployments/executions/exec-1', 'DELETE', null],
    ]);
});

test('stop without clusterId resolves run provenance cluster on 409 mismatch and retries once', async (t) => {
    mockSessionStorage(t, {
        prism_cluster_session_id: 'session-b',
        prism_cluster_server_id: 'cluster-b',
    });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        calls.push([url, options.method || 'GET', body]);
        if (url === '/api/v1/deployments/runs/run%2Fa/cluster-session' && body?.cluster_session_id === 'session-b') {
            return Response.json(
                { detail: "active cluster session is not connected to this deployment's original cluster" },
                { status: 409 },
            );
        }
        if (url === '/api/v1/deployments/runs/run%2Fa') {
            return Response.json({ id: 'run/a', provenance: { cluster_server_id: 'cluster-a' } });
        }
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-a' });
        }
        if (url === '/api/v1/deployments/runs/run%2Fa/cluster-session' && body?.cluster_session_id === 'session-a') {
            return new Response(null, { status: 204 });
        }
        if (url === '/api/v1/deployments/runs/run%2Fa/cases/case%2Fb/stop') {
            return Response.json({ status: 'stopped' });
        }
        throw new Error(`Unexpected fetch: ${url}`);
    });

    await stopLocalDeploymentCase('run/a', 'case/b');

    assert.deepEqual(calls, [
        ['/api/v1/deployments/runs/run%2Fa/cluster-session', 'POST', { cluster_session_id: 'session-b' }],
        ['/api/v1/deployments/runs/run%2Fa', 'GET', null],
        ['/api/cluster/session?clusterId=cluster-a', 'GET', null],
        ['/api/v1/deployments/runs/run%2Fa/cluster-session', 'POST', { cluster_session_id: 'session-a' }],
        ['/api/v1/deployments/runs/run%2Fa/cases/case%2Fb/stop', 'POST', {}],
    ]);
});

test('prepareDeploymentRestart acquires target cluster session when sessionStorage is on another cluster', async (t) => {
    const { store, setCalls } = mockSessionStorage(t, {
        prism_cluster_session_id: 'session-b',
        prism_cluster_server_id: 'cluster-b',
    });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        calls.push([url, options.method || 'GET', body]);
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-a' });
        }
        if (url === '/api/v1/deployments/executions/exec-1/restart-plan') {
            return Response.json({ status: 'awaiting_review' });
        }
        throw new Error(`Unexpected fetch: ${url}`);
    });

    const result = await prepareDeploymentRestart('exec-1', { clusterId: 'cluster-a' });

    assert.deepEqual(result, { status: 'awaiting_review' });
    assert.deepEqual(calls, [
        ['/api/cluster/session?clusterId=cluster-a', 'GET', null],
        ['/api/v1/deployments/executions/exec-1/restart-plan', 'POST', { cluster_session_id: 'session-a' }],
    ]);
    assert.deepEqual(setCalls, []);
    assert.equal(store.get('prism_cluster_session_id'), 'session-b');
});

test('rebind does not swallow 403 permission errors or loop when retry fails', async (t) => {
    mockSessionStorage(t, {
        prism_cluster_session_id: 'session-a',
        prism_cluster_server_id: 'cluster-a',
    });
    let calls = 0;
    t.mock.method(globalThis, 'fetch', async () => {
        calls += 1;
        return Response.json({ detail: 'Forbidden' }, { status: 403 });
    });
    await assert.rejects(
        deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' }),
        (error) => error.status === 403,
    );
    assert.equal(calls, 1);

    let retryCalls = 0;
    t.mock.method(globalThis, 'fetch', async (url) => {
        retryCalls += 1;
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-fresh' });
        }
        return Response.json({ detail: 'cluster session is no longer active' }, { status: 409 });
    });
    await assert.rejects(
        deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' }),
        (error) => error.status === 409,
    );
    assert.equal(retryCalls, 3);
});

test('unrelated 404/409 errors do not trigger cluster session acquisition or retry', async (t) => {
    mockSessionStorage(t, {
        prism_cluster_session_id: 'session-a',
        prism_cluster_server_id: 'cluster-a',
    });
    for (const [status, body] of [
        [404, { detail: 'deployment run not found' }],
        [409, { code: 'deployment_in_use', detail: 'deployment is in use by active evaluation' }],
        [409, { detail: 'deployment run is no longer active' }],
    ]) {
        const urls = [];
        t.mock.method(globalThis, 'fetch', async (url) => {
            urls.push(url);
            return Response.json(body, { status });
        });
        await assert.rejects(
            deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' }),
            (error) => error.status === status,
        );
        assert.deepEqual(urls, ['/api/v1/deployments/runs/run-1/cluster-session']);
    }
});

test('prepareDeploymentRestart without clusterId recovers via execution lookup for foreign and empty sessionStorage', async (t) => {
    const { store } = mockSessionStorage(t, {
        prism_cluster_session_id: 'session-b',
        prism_cluster_server_id: 'cluster-b',
    });
    const foreignCalls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        foreignCalls.push([url, options.method || 'GET', body]);
        if (url === '/api/v1/deployments/executions/exec-1/restart-plan' && body?.cluster_session_id === 'session-b') {
            return Response.json(
                { detail: "active cluster session is not connected to this deployment's original cluster" },
                { status: 409 },
            );
        }
        if (url === '/api/v1/deployments/executions/exec-1') {
            return Response.json({ execution_id: 'exec-1', cluster_id: 'cluster-a' });
        }
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-a' });
        }
        if (url === '/api/v1/deployments/executions/exec-1/restart-plan' && body?.cluster_session_id === 'session-a') {
            return Response.json({ status: 'awaiting_review' });
        }
        throw new Error(`Unexpected fetch: ${url}`);
    });
    assert.deepEqual(await prepareDeploymentRestart('exec-1'), { status: 'awaiting_review' });
    assert.deepEqual(foreignCalls, [
        ['/api/v1/deployments/executions/exec-1/restart-plan', 'POST', { cluster_session_id: 'session-b' }],
        ['/api/v1/deployments/executions/exec-1', 'GET', null],
        ['/api/cluster/session?clusterId=cluster-a', 'GET', null],
        ['/api/v1/deployments/executions/exec-1/restart-plan', 'POST', { cluster_session_id: 'session-a' }],
    ]);

    store.clear();
    const emptyCalls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        emptyCalls.push([url, options.method || 'GET', body]);
        if (url === '/api/v1/deployments/executions/exec-1/restart-plan' && body === null) {
            return Response.json({ detail: 'cluster session is no longer active' }, { status: 409 });
        }
        if (url === '/api/v1/deployments/executions/exec-1') {
            return Response.json({ execution_id: 'exec-1', cluster_id: 'cluster-a' });
        }
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-a' });
        }
        if (url === '/api/v1/deployments/executions/exec-1/restart-plan' && body?.cluster_session_id === 'session-a') {
            return Response.json({ status: 'awaiting_review' });
        }
        throw new Error(`Unexpected fetch: ${url}`);
    });
    assert.deepEqual(await prepareDeploymentRestart('exec-1'), { status: 'awaiting_review' });
    assert.deepEqual(emptyCalls, [
        ['/api/v1/deployments/executions/exec-1/restart-plan', 'POST', null],
        ['/api/v1/deployments/executions/exec-1', 'GET', null],
        ['/api/cluster/session?clusterId=cluster-a', 'GET', null],
        ['/api/v1/deployments/executions/exec-1/restart-plan', 'POST', { cluster_session_id: 'session-a' }],
    ]);
});

test('deleteDeploymentExecution with deleteNamespace=false skips cluster session rebind', async (t) => {
    mockSessionStorage(t, {
        prism_cluster_session_id: 'session-b',
        prism_cluster_server_id: 'cluster-b',
    });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        calls.push([url, options.method || 'GET']);
        return new Response(null, { status: 204 });
    });

    await deleteDeploymentExecution('exec-1', { deleteNamespace: false, runId: 'run-1', clusterId: 'cluster-a' });

    assert.deepEqual(calls, [['/api/v1/deployments/executions/exec-1?delete_namespace=false', 'DELETE']]);
});

test('delete with empty sessionStorage acquires session proactively, skips rebind on empty payload, and skips duplicate retry', async (t) => {
    const { store } = mockSessionStorage(t, {});
    const proactiveCalls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        proactiveCalls.push([url, options.method || 'GET', options.body ? JSON.parse(options.body) : null]);
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-a' });
        }
        return new Response(null, { status: 204 });
    });
    await deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' });
    assert.deepEqual(proactiveCalls, [
        ['/api/cluster/session?clusterId=cluster-a', 'GET', null],
        ['/api/v1/deployments/runs/run-1/cluster-session', 'POST', { cluster_session_id: 'session-a' }],
        ['/api/v1/deployments/executions/exec-1', 'DELETE', null],
    ]);

    const emptySessionCalls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        emptySessionCalls.push([url, options.method || 'GET']);
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({});
        }
        return new Response(null, { status: 204 });
    });
    await deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' });
    assert.deepEqual(emptySessionCalls, [
        ['/api/cluster/session?clusterId=cluster-a', 'GET'],
        ['/api/v1/deployments/executions/exec-1', 'DELETE'],
    ]);

    store.set('prism_cluster_session_id', 'session-a');
    store.set('prism_cluster_server_id', 'cluster-a');
    const sameSessionCalls = [];
    t.mock.method(globalThis, 'fetch', async (url) => {
        sameSessionCalls.push(url);
        if (url === '/api/cluster/session?clusterId=cluster-a') {
            return Response.json({ sessionId: 'session-a' });
        }
        return Response.json({ detail: 'cluster session is no longer active' }, { status: 409 });
    });
    await assert.rejects(
        deleteDeploymentExecution('exec-1', { runId: 'run-1', clusterId: 'cluster-a' }),
        (error) => error.status === 409,
    );
    assert.deepEqual(sameSessionCalls, [
        '/api/v1/deployments/runs/run-1/cluster-session',
        '/api/cluster/session?clusterId=cluster-a',
    ]);
});
