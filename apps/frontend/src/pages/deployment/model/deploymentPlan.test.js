import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { deploymentPlan, candidatePlan, overviewPlan, optimizationPlan, layoutPlan, edgeGeometry } from './deploymentPlan.js';

const service = namespace => ({ apiVersion: 'v1', kind: 'Service', metadata: { name: 'inference', namespace }, spec: { selector: { app: 'worker' } } });
const worker = namespace => ({ apiVersion: 'apps/v1', kind: 'Deployment', metadata: { name: 'worker', namespace }, spec: { template: { metadata: { labels: { app: 'worker' } }, spec: { containers: [{ name: 'modelserver' }], volumes: [{ persistentVolumeClaim: { claimName: 'weights' } }] } } } });
test('relationships preserve namespace scope and only use declared references', () => {
    const docs = [service('a'), worker('a'), worker('b'), { apiVersion: 'v1', kind: 'PersistentVolumeClaim', metadata: { name: 'weights', namespace: 'a' } }];
    const plan = deploymentPlan(yaml.dump({ apiVersion: 'v1', kind: 'List', items: docs }));
    assert.equal(plan.nodes.length, 4);
    assert.equal(plan.edges.length, 2);
    assert.ok(plan.edges.every(e => !e.to.includes('/b/')));
});
test('empty selectors do not invent service relationships', () => {
    const svc = service('a'); svc.spec.selector = {};
    assert.equal(deploymentPlan(yaml.dump({ kind: 'List', items: [svc, worker('a')] })).edges.length, 0);
});
test('overview hides supporting objects without losing them from resource details', () => {
    const docs = [service('a'), worker('a'), { apiVersion: 'v1', kind: 'ConfigMap', metadata: { name: 'configuration' } }];
    const full = deploymentPlan(yaml.dump({ kind: 'List', items: docs }));
    const overview = overviewPlan(full);
    assert.equal(full.nodes.length, 3);
    assert.equal(overview.nodes.length, 2);
    assert.equal(overview.edges.length, 1);
    assert.deepEqual(overview.nodes.map(node => node.name), ['Service endpoint', 'Model serving']);
    assert.ok(overview.nodes.every(node => full.nodes.some(original => original.id === node.id)));
});
test('malformed YAML remains an error, not an empty valid plan', () => {
    assert.throws(() => deploymentPlan('items: [broken'));
});
test('candidate review describes intent without invented services or manifests', () => {
    const plan = candidatePlan({ provider_ref: 'pd-disaggregation', replicas: 2, tensor_parallel_size: 4, prefill_replicas: 1, prefill_tensor_parallel_size: 2 }, 'test/model');
    assert.equal(plan.nodes.length, 3);
    assert.equal(plan.content, '');
    assert.ok(plan.nodes.every(n => !n.document));
});

test('DisaggregatedSet exposes separately inspectable model roles', () => {
    const document = { apiVersion: 'disaggregatedset.x-k8s.io/v1', kind: 'DisaggregatedSet', metadata: { name: 'pd' }, spec: { roles: ['prefill', 'decode'].map(name => ({ name, spec: { replicas: 2, leaderWorkerTemplate: { workerTemplate: { spec: { containers: [{ name: 'modelserver' }] } } } } })) } };
    const plan = deploymentPlan(yaml.dump(document));
    assert.equal(plan.nodes.length, 3);
    assert.deepEqual(plan.nodes.filter(n => n.owner).map(n => n.role), ['prefill', 'decode']);
    assert.equal(plan.edges.filter(e => e.label === 'owns').length, 2);
});


test('P/D composition shows independent local caches and a planned transport', () => {
    const plan = candidatePlan({ provider_ref: 'pd-disaggregation', replicas: 2, prefill_replicas: 1, native_cpu_cache_gib: 4 }, 'test');
    assert.equal(plan.nodes.filter(n => n.kind === 'Planned local cache').length, 2);
    assert.ok(plan.edges.some(e => e.from === 'prefill' && e.to === 'decode'));
});

test('manifest review exposes ordered transport and local cache children', () => {
    const doc = worker('a');
    doc.spec.template.spec.containers[0].args = ['--kv-transfer-config', JSON.stringify({ kv_connector: 'MultiConnector', kv_connector_extra_config: { connectors: [{ kv_connector: 'NixlConnector' }, { kv_connector: 'OffloadingConnector', kv_connector_extra_config: { cpu_bytes_to_use: 4 * 1024 ** 3 } }] } })];
    const plan = deploymentPlan(yaml.dump(doc));
    assert.equal(plan.nodes.length, 3);
    assert.deepEqual(plan.edges.map(e => e.label), ['transfers KV', 'offloads KV locally']);
});

test('optimization overview separates request flow, ownership and real cache dependencies', () => {
    const decisions = ['prefix_routing', 'prefix_cache', 'external_kv_cache', 'chunked_prefill', 'pd_disaggregation'].map(capability_id => ({ capability_id, label: capability_id, status: 'enabled' }));
    decisions.push({ capability_id: 'autoscaling', label: 'Autoscaling', status: 'unsupported' });
    const plan = optimizationPlan({ prefill_replicas: 1, replicas: 2 }, decisions);
    assert.deepEqual(plan.edges.filter(edge => edge.kind === 'flow').map(edge => [edge.from, edge.to]), [
        ['requests', 'routing'], ['routing', 'prefill'], ['prefill', 'decode'], ['decode', 'responses'],
    ]);
    assert.ok(plan.edges.some(edge => edge.label === 'KV transfer'));
    assert.equal(plan.edges.filter(edge => edge.kind === 'dependency').length, 2);
    assert.ok(!plan.edges.some(edge => edge.to === 'optimization:autoscaling'));
    assert.equal(optimizationPlan({ replicas: 1 }, []).edges[2].label, 'shared KV');
    const nodes = layoutPlan(plan);
    assert.deepEqual(layoutPlan(plan), nodes);
    for (const a of nodes) for (const b of nodes) {
        if (a.id === b.id) continue;
        assert.ok(Math.abs(a.x - b.x) >= (a.width + b.width) / 2 || Math.abs(a.y - b.y) >= (a.height + b.height) / 2, `${a.id} overlaps ${b.id}`);
    }
    for (const edge of plan.edges) {
        const geometry = edgeGeometry(edge, nodes);
        assert.ok(geometry);
        for (let i = 1; i < geometry.points.length; i++) {
            const [x1, y1] = geometry.points[i - 1], [x2, y2] = geometry.points[i];
            assert.ok(x1 === x2 || y1 === y2, 'Edges must be orthogonal');
            for (const node of nodes.filter(item => item.id !== edge.from && item.id !== edge.to)) {
                const left = node.x - node.width / 2, right = node.x + node.width / 2;
                const top = node.y - node.height / 2, bottom = node.y + node.height / 2;
                const intersects = x1 === x2
                    ? x1 > left && x1 < right && Math.max(y1, y2) > top && Math.min(y1, y2) < bottom
                    : y1 > top && y1 < bottom && Math.max(x1, x2) > left && Math.min(x1, x2) < right;
                assert.equal(intersects, false, `${edge.from} -> ${edge.to} crosses ${node.id}`);
            }
        }
    }
});
