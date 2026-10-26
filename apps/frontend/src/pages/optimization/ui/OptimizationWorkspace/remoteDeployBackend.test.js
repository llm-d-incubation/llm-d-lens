import assert from 'node:assert/strict';
import test from 'node:test';
import { listDeploymentExecutions, getDeploymentExecutions, listDeploymentNodeLabels, startLocalDeployment, stopLocalDeploymentCase, deleteDeploymentExecution } from './remoteDeployBackend.js';

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
