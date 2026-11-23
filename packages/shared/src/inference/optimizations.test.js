import test from 'node:test';
import assert from 'node:assert/strict';
import { OPTIMIZATIONS, optimizationCatalog, optimizationSourceFacts, resolveOptimizationFeatures } from './optimizations.js';

const guide = (id, extra = {}) => ({ id, title: id, disposition: { kind: 'recipe' }, ...extra });
const root = 'guides/models/example/modelserver';
const facts = (path, docs) => optimizationSourceFacts(path, docs);
const workload = (role, args) => ({ kind: 'DisaggregatedSet', spec: { roles: [{ name: role, spec: {
    leaderWorkerTemplate: { workerTemplate: { spec: { containers: [{ name: 'modelserver', args }] } } },
} }] } });
const fixture = () => ({ optimizations: { sources: [
    facts(`${root}/unified/kustomization.yaml`, [{ kind: 'Kustomization', resources: ['../base.yaml'] }]),
    facts(`${root}/split/kustomization.yaml`, [{ kind: 'Kustomization', resources: ['../base.yaml'], patches: [{ path: '../prefill.yaml' }], components: ['../transport'] }]),
    facts(`${root}/base.yaml`, [workload('decode', ['--speculative-config={"method":"mtp"}', '--data-parallel-size=8', '--enable-expert-parallel', '--enable-prefix-caching'])]),
    facts(`${root}/prefill.yaml`, [workload('prefill', ['--prefill-context-parallel-size=8', '--kv-transfer-config={"kv_connector":"TPURaidenOffloadingConnector"}'])]),
    facts(`${root}/transport/kustomization.yaml`, [{ kind: 'Component', resources: ['../transport.yaml'] }]),
    facts(`${root}/transport.yaml`, [workload('decode', ['--kv-transfer-config={"kv_connector":"TPURaidenConnector"}'])]),
    facts(`${root}/unselected.yaml`, [workload('prefill', ['--quantization=fp8'])]),
] } });

test('catalog covers source Guides, shared mechanisms and meaningful process stages', () => {
    const source = { commit: 'a'.repeat(40), guides: [
        guide('hyperconverged-cephfs'), guide('turn-priority-fairness'), guide('models/example'),
        guide('workload-autoscaling/multi-inference-pool'), guide('rl', { disposition: { kind: 'external' } }),
    ] };
    const catalog = optimizationCatalog(source);
    assert.equal(catalog.schemaVersion, 'inference-optimizations.v1');
    assert.equal(catalog.commit, source.commit);
    assert.equal(new Set(catalog.items.map(item => item.id)).size, catalog.items.length);
    for (const entry of source.guides) assert.ok(catalog.items.some(item => item.guide === entry.id));
    assert.equal(catalog.items.find(item => item.id === 'shared-cache').stage, 'cache-plane');
    assert.equal(catalog.items.find(item => item.id === 'shared-cache').generation, 'requires-adapter');
    assert.equal(catalog.items.find(item => item.id === 'turn-priority').stage, 'routing');
    assert.equal(catalog.items.find(item => item.guide === 'models/example').stage, 'decode');
    assert.equal(catalog.items.find(item => item.guide === 'workload-autoscaling/multi-inference-pool').stage, 'routing');
    assert.equal(catalog.items.find(item => item.guide === 'rl').generation, 'external-operation');
    assert.deepEqual(JSON.parse(JSON.stringify(catalog)), catalog);
});

test('renamed actuation Guides resolve once in both old and new revisions', () => {
    for (const ids of [['fast-model-actuation', 'fast-model-actuation/fast-model-actuation-keda'], ['fast-model-actuation-base', 'fast-model-actuation-keda']]) {
        const items = optimizationCatalog({ guides: ids.map(id => guide(id)) }).items;
        for (const [index, id] of ['actuation', 'keda'].entries()) {
            assert.equal(items.find(item => item.id === id).guide, ids[index]);
            assert.equal(items.filter(item => item.guide === ids[index]).length, 1);
        }
    }
});

test('lifecycle and support remain version-owned and absent support is unknown', () => {
    const support = { accelerators: { custom: { engines: { customEngine: 'community' } } } };
    const items = optimizationCatalog({ guides: [
        guide('workload-autoscaling/wva', { instructions: '# WVA\nDeprecated. Use KEDA.' }),
        guide('optimized-baseline', { support }),
    ] }).items;
    assert.equal(items.find(item => item.guide.endsWith('/wva')).maturity, 'deprecated');
    assert.deepEqual(items.find(item => item.id === 'parallel').hardwareSupport, support);
    assert.equal(items.find(item => item.id === 'cache').hardwareSupport, null);
    assert.equal(items.find(item => item.id === 'actuation').generation, 'unavailable');
});

test('selected variant follows local references and exposes compound model optimizations', () => {
    const catalog = fixture();
    const unified = resolveOptimizationFeatures(catalog, { units: [{ path: `${root}/unified` }] });
    const split = resolveOptimizationFeatures(catalog, { units: [{ path: `${root}/split` }] });
    assert.deepEqual(unified.roles, ['decode']);
    assert.ok(!unified.features.some(item => item.id === 'transfer'));
    for (const id of ['speculation', 'data-parallel', 'wide-ep', 'prefix', 'context-parallel', 'cache', 'transfer', 'kv-transport']) {
        assert.equal(split.features.find(item => item.id === id)?.state, 'included', id);
    }
    assert.ok(!split.features.some(item => item.id === 'quantization'));
    assert.deepEqual(split.unresolved, []);
    assert.deepEqual(split.implementations.sort(), ['TPU Sync transfer', 'TPU host offload']);
});

test('missing references, local edits and conflicting flags never claim effective configuration', () => {
    const catalog = fixture();
    catalog.optimizations.sources.push(facts(`${root}/disabled.yaml`, [workload('decode', ['--no-enable-prefix-caching'])]));
    const plan = { units: [{ path: `${root}/unified` }, { path: `${root}/disabled.yaml` }] };
    assert.equal(resolveOptimizationFeatures(catalog, plan).features.find(item => item.id === 'prefix').state, 'unknown');
    const edited = resolveOptimizationFeatures(catalog, { units: [{ path: `${root}/unified`, replacementContent: 'edited' }] });
    assert.ok(edited.features.every(item => item.state === 'unknown'));
    const final = resolveOptimizationFeatures(catalog, { units: [{ path: `${root}/unified` }], finalConfiguration: { content: 'edited' } });
    assert.ok(final.features.every(item => item.state === 'unknown'));
    const missing = resolveOptimizationFeatures(catalog, { units: [{ path: `${root}/unified` }, { path: 'https://example.test/remote' }] });
    assert.deepEqual(missing.unresolved, ['https://example.test/remote']);
    assert.ok(missing.features.every(item => item.state === 'unknown'));
});

test('facts preserve negative switches, skip singleton parallelism and tolerate reference cycles', () => {
    const source = facts('base.yaml', [workload('decode', ['--enable-prefix-caching=false', '--data-parallel-size=1'])]);
    assert.ok(!source.features.includes('prefix'));
    assert.ok(!source.features.includes('data-parallel'));
    const cycle = facts('cycle/kustomization.yaml', [{ kind: 'Kustomization', resources: ['.'] }]);
    assert.deepEqual(resolveOptimizationFeatures({ optimizations: { sources: [cycle] } }, { units: [{ path: 'cycle' }] }).features, []);
    assert.ok(OPTIMIZATIONS.filter(item => item.selection === 'recipe').every(item => !item.guide));
});
