import test from 'node:test';
import assert from 'node:assert/strict';
import { createBlueprint } from './blueprint.js';
import { inferenceScene } from './sceneGraph.js';
import { inferenceDetails } from './inferenceDetails.js';

test('hover details distinguish draft geometry from unresolved Kubernetes identity', () => {
    const draft = { ...createBlueprint(), model: 'org/model', pd: true, prefillReplicas: '2', prefillTp: '4', replicas: '3', tp: '2' };
    const graph = inferenceScene(draft);
    const rows = inferenceDetails(graph.nodes.find(node => node.id === 'prefill'), graph, draft, { name: 'Target cluster' });
    assert.equal(rows.find(row => row.label === 'Replicas × TP').value, '2 × 4');
    assert.ok(!rows.some(row => row.label === 'Model'));
    assert.ok(!rows.some(row => row.label.includes('amespace') || row.label === 'Generated resources'));
});
test('generated resource facts come from actual bundle manifests, not assumed names', () => {
    const draft = createBlueprint(), graph = inferenceScene(draft);
    const result = { files: [{ name: 'modelserver.yaml', content: 'kind: Deployment\nmetadata:\n  name: custom-worker\n  namespace: custom-namespace' }], plan: { source: { commit: 'a'.repeat(40) } } };
    const rows = inferenceDetails(graph.nodes.find(node => node.id === 'pods'), graph, draft, null, null, result);
    assert.ok(rows.length <= 3);
    assert.ok(!rows.some(row => ['Bundle namespaces', 'Generated resources', 'Source commit', 'Model', 'Cluster'].includes(row.label)));
});
test('relationship details identify both endpoints and never claim a missing generator is active', () => {
    const draft = createBlueprint(), graph = inferenceScene(draft);
    const transfer = inferenceDetails(graph.capabilities.find(node => node.id === 'transfer'), graph, draft);
    assert.equal(transfer.find(row => row.label === 'Acts on').value, 'Prefill → Decode');
    const unavailable = inferenceDetails(graph.capabilities.find(node => node.id === 'scaling'), graph, draft);
    assert.equal(unavailable.find(row => row.label === 'State').value, 'Off');
});
