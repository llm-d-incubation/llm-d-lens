import test from 'node:test';
import assert from 'node:assert/strict';
import { compositionModelBinding, bindCompositionModel } from '../../src/services/guideCompositionModel.ts';
import { applyKueueWorkloadPatch } from '../../src/services/guideCompositionInputs.ts';
import { compositionReferences, compositionWorkloads } from '../../src/services/guideCompositionReferences.ts';
const entry = { id: 'cache', clusterId: 'cluster', storageVolumeId: 'volume', status: 'ready', source: { kind: 'huggingface', huggingface: { repoId: 'org/model', revision: 'revision-a' } } };
const volume = { id: 'volume', clusterId: 'cluster', status: 'ready', pvcName: 'cache-pvc', dynamicPvc: { namespace: 'models' } };
const binding = () => compositionModelBinding('cluster', 'models', entry, volume);
const template = () => ({ metadata: { labels: { app: 'model' } }, spec: { containers: [{ name: 'modelserver', command: ['vllm', 'serve'], args: ['old/model', '--served-model-name', 'old/model'], env: [{ name: 'HF_TOKEN', valueFrom: { secretKeyRef: { name: 'hf', key: 'token' } } }] }] } });
test('model binding rejects stale, unsynchronized, cross-cluster and cross-namespace records', () => {
    assert.equal(binding().model, 'org/model');
    assert.throws(() => compositionModelBinding('other', 'models', entry, volume), /target cluster/);
    assert.throws(() => compositionModelBinding('cluster', 'other', entry, volume), /namespace/);
    assert.throws(() => compositionModelBinding('cluster', 'models', { ...entry, status: 'downloading' }, volume), /ready/);
    assert.throws(() => compositionModelBinding('cluster', 'models', { ...entry, pendingSyncNodes: ['new-node'] }, volume), /synchronized/);
    assert.throws(() => compositionModelBinding('cluster', 'models', entry, { ...volume, status: 'failed' }), /ready/);
});
test('model and cached revision reach every disaggregated role without changing parallelism', () => {
    const docs = [{ kind: 'DisaggregatedSet', metadata: { name: 'model', namespace: 'models' }, spec: { roles: [{ spec: { leaderWorkerTemplate: { leaderTemplate: template(), workerTemplate: template() } } }] } }];
    assert.equal(bindCompositionModel(docs, binding()), 2);
    for (const { template: pod } of compositionWorkloads(docs)) {
        const container = pod.spec.containers[0];
        assert.deepEqual(container.args, ['org/model', '--served-model-name', 'org/model', '--revision=revision-a']);
        assert.equal(container.env.find(item => item.name === 'HF_HOME')?.value, '/model-cache');
        assert.equal(container.env.some(item => item.name === 'HF_TOKEN'), false);
        assert.equal(pod.spec.volumes[0].persistentVolumeClaim.claimName, 'cache-pvc');
    }
});
test('Kueue patches preserve workloads, set pod opt-in and strip server-owned fields', () => {
    const doc = { kind: 'Deployment', metadata: { name: 'model', resourceVersion: '12', uid: 'uid' }, spec: { replicas: 3, template: template() }, status: { readyReplicas: 3 } };
    const first = applyKueueWorkloadPatch(doc, { spec: { strategy: { rollingUpdate: { maxSurge: 0, maxUnavailable: 1 } } } });
    const result = applyKueueWorkloadPatch(first, { spec: { template: { metadata: { labels: { 'kueue.x-k8s.io/queue-name': 'model-a' } } } } });
    assert.equal(result.spec.replicas, 3);
    assert.equal(result.spec.template.metadata.labels.app, 'model');
    assert.equal(result.spec.template.metadata.labels['kueue.x-k8s.io/queue-name'], 'model-a');
    assert.equal(result.status, undefined);
    assert.equal(result.metadata.resourceVersion, undefined);
    assert.equal(doc.metadata.resourceVersion, '12');
    assert.throws(() => applyKueueWorkloadPatch(doc, { spec: { replicas: 0 } }), /Unsupported/);
});
test('credential and HTTPRoute dependencies are checked without copying their contents', () => {
    const refs = compositionReferences([{ kind: 'TriggerAuthentication', metadata: { name: 'auth', namespace: 'models' }, spec: { secretTargetRef: [{ name: 'prometheus-token' }] } }, { kind: 'HTTPRoute', metadata: { name: 'route', namespace: 'models' }, spec: { parentRefs: [{ name: 'gateway' }], rules: [{ backendRefs: [{ kind: 'InferencePool', name: 'pool' }] }] } }]);
    assert.deepEqual(refs.external.map(ref => `${ref.kind}/${ref.name}`), ['Secret/prometheus-token', 'Gateway/gateway', 'InferencePool/pool']);
});
