import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { createBlueprint, blueprintGuide, blueprintRequest, blueprintErrors, blueprintFiles, parseBlueprint } from './blueprint.js';
const profile = { id: 'test-profile', upstream_variant: 'test-accelerator', deployment: { runtime_image: 'example.test/engine:1' } };
const providers = ['optimized-baseline', 'pd-disaggregation', 'precise-prefix-cache-routing', 'tiered-prefix-cache'].map(id => ({ id, supported: true, accelerators: ['test-accelerator'], variants: ['vllm'], variants_by_accelerator: { 'test-accelerator': ['base'] } }));
const draft = () => ({ ...createBlueprint(), clusterId: 'cluster', profileId: profile.id, model: 'org/model' });
test('neutral defaults require explicit cluster, hardware and model', () => {
    assert.ok(blueprintErrors(createBlueprint(), providers, undefined).length >= 3);
    assert.throws(() => blueprintRequest(draft(), providers, undefined, 'session'), /hardware profile/);
});
test('compose cache, engine and router settings through the existing planner contract', () => {
    const request = blueprintRequest({ ...draft(), cache: 'native', cacheCpuGiB: '12.5', maxModelLen: '8192', routerValues: 'router:\n  epp:\n    replicas: 1' }, providers, profile, 'session');
    assert.equal(request.guide, 'tiered-prefix-cache');
    assert.equal(request.guideVariant, 'native/cpu/base');
    assert.equal(request.guideSettings.cacheCpuGiB, 12.5);
    assert.equal(request.maxModelLen, 8192);
    assert.equal(request.runtimeImage, profile.deployment.runtime_image);
    assert.equal(request.environment.kubernetesMode, 'required');
});
test('unsupported mechanism combinations are rejected rather than silently selecting a Guide', () => {
    for (const patch of [{ pd: true, precise: true }, { pd: true, cache: 'native' }, { precise: true, cache: 'lmcache' }]) {
        assert.throws(() => blueprintRequest({ ...draft(), variant: 'base', ...patch }, providers, profile, 'session'), /combined adapter/);
    }
});
test('P/D uses profile-derived variants and distinct role geometry', () => {
    assert.throws(() => blueprintRequest({ ...draft(), pd: true, variant: 'vllm' }, providers, profile, 'session'), /variant/);
    const request = blueprintRequest({ ...draft(), pd: true, variant: 'base', prefillTp: '2', tp: '4' }, providers, profile, 'session');
    assert.equal(request.prefillTensorParallelSize, 2);
    assert.equal(request.decodeTensorParallelSize, 4);
});
test('malformed settings fail before transport', () => {
    for (const patch of [{ tp: '0' }, { replicas: '1.5' }, { gpuMemoryUtilization: '1.1' }, { routerValues: '- array' }, { maxModelLen: '-1' }, { cache: 'native', cacheCpuGiB: 'Infinity' }]) {
        assert.throws(() => blueprintRequest({ ...draft(), ...patch }, providers, profile, 'session'));
    }
});
test('draft round trip preserves disabled settings but never imports validity or generated assets', () => {
    const source = { ...draft(), routerValues: 'router:\n  epp:\n    replicas: 2', prefillTp: '4' };
    assert.deepEqual(parseBlueprint(yaml.dump(source)), source);
    assert.equal(blueprintGuide(source), 'optimized-baseline');
    for (const extra of ['valid: true', 'pd: yes', '__proto__: {}']) assert.throws(() => parseBlueprint(`schemaVersion: inference-blueprint.v1\n${extra}`));
});
test('export requires a complete consistent source-pinned bundle', () => {
    assert.throws(() => blueprintFiles({ validation: { errors: ['capacity exceeded'] } }), /capacity exceeded/);
    const commit = 'a'.repeat(40);
    const plan = { source: { commit }, validation: { errors: [] }, plannedDeployment: { content: 'kind: Deployment' }, deploymentBundle: { sourceCommit: commit, helm: { values: [{ name: 'router-effective.yaml', content: 'router: {}' }] }, resources: [{ name: 'render.yaml', content: 'kind: Service' }] } };
    assert.deepEqual(blueprintFiles(plan).map(file => file.name), ['modelserver.yaml', 'router-effective.yaml', 'render.yaml', 'deployment-bundle.json', 'provenance.json']);
    assert.throws(() => blueprintFiles({ ...plan, source: { commit: 'b'.repeat(40) } }), /provenance/);
});
test('advanced overrides share protected fields and overlap validation with the existing editor', () => {
    const encode = rows => yaml.dump(rows);
    const override = { target: 'both', kind: 'argument', name: 'enable-prefix-caching', value: '' };
    const request = blueprintRequest({ ...draft(), engineOverrides: encode([override]) }, providers, profile, 'session');
    assert.deepEqual(request.customParameters, [override]);
    for (const rows of [[{ ...override, name: 'model' }], [{ ...override, target: 'prefill' }], [override, override], [{ ...override, value: 3 }]]) {
        assert.throws(() => blueprintRequest({ ...draft(), engineOverrides: encode(rows) }, providers, profile, 'session'));
    }
    assert.throws(() => blueprintRequest({ ...draft(), maxModelLen: '100', engineOverrides: encode([{ ...override, name: 'max-model-len', value: '200' }]) }, providers, profile, 'session'), /Conflicts/);
});

test('cluster hardware uses summary profiles, retains unknown identities and falls back to positive node counts', async () => {
    const { blueprintClusterProfiles } = await import('./blueprint.js');
    const overview = { kubernetes: { hardware: { accelerators: [{ id: profile.id, gpuCount: 2 }] }, nodes: [{ gpuByProfile: { stale: 4 } }] } };
    assert.deepEqual(blueprintClusterProfiles(overview, [profile]), [profile]);
    assert.deepEqual(blueprintClusterProfiles(null, [profile]), []);
    assert.deepEqual(blueprintClusterProfiles({ kubernetes: { nodes: [{ gpuByProfile: { [profile.id]: 2, absent: 0 } }] } }, [profile]), [profile]);
    overview.kubernetes.hardware.accelerators.push({ id: 'unknown', gpuCount: 1 });
    assert.deepEqual(blueprintClusterProfiles(overview, [profile]).map(item => item.id), [profile.id, 'unknown']);
    assert.equal(blueprintClusterProfiles(overview, [profile])[1].upstream_variant, undefined);
});
