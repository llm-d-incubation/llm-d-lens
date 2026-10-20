import test from 'node:test';
import assert from 'node:assert/strict';
import { compositionReferences } from '../../src/services/guideCompositionReferences.ts';
const worker = { kind: 'Deployment', metadata: { name: 'workers', namespace: 'serving' }, spec: { template: { metadata: { labels: { app: 'engine' } }, spec: { containers: [{ env: [{ valueFrom: { secretKeyRef: { name: 'model-token', key: 'token' } } }] }] } } } };
test('checks pool selectors and tracks required secrets without exposing their values', () => {
    const pool = { kind: 'InferencePool', metadata: { name: 'pool', namespace: 'serving' }, spec: { selector: { matchLabels: { app: 'engine' } } } };
    const refs = compositionReferences([worker, pool]);
    assert.deepEqual(refs.errors, []);
    assert.deepEqual(refs.external.map(ref => ref.name), ['model-token']);
    assert.match(compositionReferences([worker, { ...pool, spec: { selector: { matchLabels: { app: 'other' } } } }]).errors[0], /selects no/);
});
test('two autoscalers cannot write one workload replica count', () => {
    const scale = { kind: 'ScaledObject', metadata: { name: 'queue', namespace: 'serving' }, spec: { scaleTargetRef: { name: 'workers' } } };
    const refs = compositionReferences([worker, scale, { ...scale, metadata: { ...scale.metadata, name: 'tokens' } }]);
    assert.match(refs.errors[0], /both control/);
    assert.ok(!refs.external.some(ref => ref.name === 'workers'));
});
