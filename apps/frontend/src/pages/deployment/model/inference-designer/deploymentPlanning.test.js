import test from 'node:test';
import assert from 'node:assert/strict';
import { candidateCompositionDraft, catalogCapability, combinationKey, deploymentEditorSnapshot, deploymentPlanManifest } from './deploymentPlanning';
import { compositionRequest } from './compositionPolicy';

const catalog = {
    commit: 'a'.repeat(40),
    guides: [{ id: 'optimized-baseline', title: 'Baseline', variables: {}, steps: { deploy: {
        main: [{ run: 'kubectl apply -k ${REPO_ROOT}/guides/optimized-baseline/modelserver/base' }],
    } } }],
    assets: [{ path: 'guides/optimized-baseline/modelserver/base', guide: 'optimized-baseline', kind: 'kustomize' }],
};
const run = { request: {
    model: 'test/model', planning_mode: 'manual', max_model_len: 8192, max_num_seqs: 32,
    max_num_batched_tokens: 4096, gpu_memory_utilization: 0.9, vllm_arguments: [],
    requirements: { cluster_id: 'cluster', objective: 'cost', workload: { concurrency: 100 },
        hard_constraints: { maxAccelerators: 4 }, required_features: [], forbidden_features: ['prefix_cache'] },
} };
const candidate = { id: 'a', provider_ref: 'optimized-baseline', tensor_parallel_size: 2, replicas: 2 };
const profile = { id: 'fixture', upstream_variant: 'fixture' };
const entry = { id: 'cache' }, volume = { id: 'volume', dynamicPvc: { namespace: 'models' } };

test('review reads the final source manifest without falling back to stale legacy output', () => {
    const legacy = { configuration: { content: { officialGuide: { renderedManifest: 'legacy' } } } };
    assert.equal(deploymentPlanManifest(legacy), 'legacy');
    assert.equal(deploymentPlanManifest({ ...legacy, source_bundle: { files: [{ name: 'deployment.yaml', content: 'final source' }] } }), 'final source');
    assert.equal(deploymentPlanManifest({ ...legacy, source_bundle: { files: [] } }), '');
    assert.equal(deploymentPlanManifest(undefined), '');
});

test('only P/D CPU cache candidates are keyed by pinned source and accelerator for early skipping', () => {
    const pd = { provider_ref: 'pd-disaggregation', native_cpu_cache_gib: 8, guide_variant: 'nixl' };
    assert.equal(combinationKey('c', 'xpu', pd), 'c:xpu:pd-disaggregation:nixl:cpu-cache');
    assert.notEqual(combinationKey('d', 'xpu', pd), combinationKey('c', 'xpu', pd));
    assert.equal(combinationKey('c', 'xpu', { ...pd, native_cpu_cache_gib: undefined }), null);
    assert.equal(combinationKey('c', 'xpu', { ...candidate, native_cpu_cache_gib: 8 }), null);
});

test('candidate bridge preserves user exclusions and separates traffic from engine limits', () => {
    const draft = candidateCompositionDraft(run, candidate, catalog, profile, entry, volume);
    assert.equal(draft.capabilityOverrides.prefix, false);
    assert.equal(draft.runtimeConfiguration.tp, 2);
    assert.equal(draft.deploymentIntent.concurrency, '32');
    assert.equal(draft.sourceNamespace, 'models');
    const body = compositionRequest(draft, 'session');
    assert.equal(body.runtimeConfiguration.replicas, 2);
    assert.equal(body.modelCacheEntryId, 'cache');
    assert.equal(body.deploymentIntent.maxDevices, '4');
});

test('tool calling becomes a bare source flag while its parser keeps a value', () => {
    const tools = { request: { ...run.request, vllm_arguments: [{ name: 'enable-auto-tool-choice', value: 'true' }, { name: 'tool-call-parser', value: 'hermes' }] } };
    const parameters = candidateCompositionDraft(tools, candidate, catalog, profile, entry, volume).runtimeConfiguration.customParameters;
    assert.deepEqual(parameters.map(item => [item.name, item.value]), [['enable-auto-tool-choice', ''], ['tool-call-parser', 'hermes']]);
});

test('CPU cache composition cannot override a forbidden prefix cache', () => {
    assert.throws(() => candidateCompositionDraft(run, { ...candidate, native_cpu_cache_gib: 1 }, catalog, profile, entry, volume), /forbidden/);
    assert.equal(catalogCapability('pd_disaggregation'), 'transfer');
    assert.equal(catalogCapability('prefix_routing'), 'affinity');
    assert.equal(catalogCapability('tracing'), 'tracing');
});

test('editor requires persisted source output, not an inferred provider preset', () => {
    assert.throws(() => deploymentEditorSnapshot({ configuration_plan: {} }), /Generate/);
    const draft = candidateCompositionDraft(run, candidate, catalog, profile, entry, volume);
    const source_bundle = { files: [{ name: 'deployment.yaml', content: 'fixture' }], optimizations: { features: [] } };
    const restored = deploymentEditorSnapshot({ configuration_plan: { source_draft: draft, source_bundle } });
    assert.equal(restored.draft, draft);
    assert.equal(restored.result.files, source_bundle.files);
});

test('seed routing cannot override a user exclusion or disable manual compatible coverage', () => {
    const excluded = { ...run, request: { ...run.request, requirements: { ...run.request.requirements, forbidden_features: ['prefix_routing'] } } };
    assert.equal(candidateCompositionDraft(excluded, { ...candidate, prefix_routing: true }, catalog, profile, entry, volume).capabilityOverrides.affinity, false);
    const manual = candidateCompositionDraft(run, { ...candidate, prefix_routing: false }, catalog, profile, entry, volume);
    assert.notEqual(manual.capabilityOverrides.affinity, false);
    assert.equal(manual.image, '');
});

test('unset candidate routing does not become a non-boolean capability selection', () => {
    for (const planning_mode of ['manual', 'agentic']) {
        const planningRun = { ...run, request: { ...run.request, planning_mode } };
        for (const prefix_routing of [null, undefined]) {
            const draft = candidateCompositionDraft(planningRun, { ...candidate, prefix_routing }, catalog, profile, entry, volume);
            assert.ok(Object.values(draft.capabilityOverrides).every(value => typeof value === 'boolean'));
            assert.ok(!draft.candidateCapabilities.includes('affinity'));
        }
    }
    const agentic = { ...run, request: { ...run.request, planning_mode: 'agentic' } };
    for (const prefix_routing of [false, true]) {
        const draft = candidateCompositionDraft(agentic, { ...candidate, prefix_routing }, catalog, profile, entry, volume);
        assert.equal(draft.capabilityOverrides.affinity, prefix_routing);
    }
});

test('disabling source cache cannot retain a native cache donor from the seed plan', () => {
    const draft = candidateCompositionDraft(run, candidate, catalog, profile, entry, volume);
    for (const capabilityOverrides of [{ cache: false }, { prefix: false }]) {
        const request = compositionRequest({ ...draft, capabilityOverrides, nativeCpuCacheGiB: 4 }, 'session');
        assert.equal(request.nativeCpuCacheGiB, undefined);
    }
});

test('automatic additions preserve sized candidate topology and do not install inventory controllers', () => {
    const coordinator = { id: 'coord-disaggregation', variables: {}, steps: { deploy: {
        modelserver: [{ run: 'kubectl apply -k SOURCE/guides/coord-disaggregation/modelserver/other-hardware/base' }],
    } } };
    const addon = { id: 'fixture-addon', variables: {}, steps: { deploy: {
        main: [{ run: 'kubectl apply -k SOURCE/guides/fixture-addon/resources' }],
    } } };
    const source = { ...catalog,
        guides: [...catalog.guides, coordinator, addon, { id: 'runtime-inventory', variables: {} }],
        assets: [...catalog.assets,
            { path: 'guides/coord-disaggregation/modelserver/other-hardware/base', guide: coordinator.id, kind: 'kustomize' },
            { path: 'guides/fixture-addon/resources', guide: addon.id, kind: 'kustomize' },
            { path: 'guides/runtime-inventory/README.md#chart-0', guide: 'runtime-inventory', kind: 'chart' }],
    };
    const allowed = { ...run, request: { ...run.request, requirements: { ...run.request.requirements, forbidden_features: [] } } };
    for (const cache of [undefined, 8]) {
        const draft = candidateCompositionDraft(allowed, { ...candidate, native_cpu_cache_gib: cache }, source, profile, entry, volume);
        assert.ok(draft.sourcePlan.guides.includes('optimized-baseline'));
        assert.ok(!draft.sourcePlan.guides.includes('coord-disaggregation'));
        assert.ok(!draft.sourcePlan.guides.includes('runtime-inventory'));
        assert.ok(draft.sourcePlan.guides.includes('fixture-addon'));
        assert.equal(draft.runtimeConfiguration.prefillTp, undefined);
        assert.equal(draft.nativeCpuCacheGiB, cache);
        assert.match(draft.optimizationDecisions.find(item => item.id === 'coordinator').reason, /sized serving topology/);
    }
});

test('candidate bridge resolves sourceNamespace from localDisk, nfs, or default fallback', () => {
    const localDiskDraft = candidateCompositionDraft(
        run,
        candidate,
        catalog,
        profile,
        entry,
        { id: 'v-local', localDisk: { hostPath: '/models', namespace: 'lens-edit' } },
    );
    assert.equal(localDiskDraft.sourceNamespace, 'lens-edit');

    const nfsDraft = candidateCompositionDraft(
        run,
        candidate,
        catalog,
        profile,
        entry,
        { id: 'v-nfs', nfs: { server: 'nfs.example', path: '/models', namespace: 'team-nfs' } },
    );
    assert.equal(nfsDraft.sourceNamespace, 'team-nfs');

    const fallbackDraft = candidateCompositionDraft(
        run,
        candidate,
        catalog,
        profile,
        entry,
        { id: 'v-default', localDisk: { hostPath: '/models' } },
    );
    assert.equal(fallbackDraft.sourceNamespace, 'llm-d-bench-storage');
});

test('explicitly disabled tool calling becomes a disabling source flag', () => {
    const tools = { request: { ...run.request, vllm_arguments: [{ name: 'enable-auto-tool-choice', value: 'false' }] } };
    const parameters = candidateCompositionDraft(tools, candidate, catalog, profile, entry, volume).runtimeConfiguration.customParameters;
    assert.deepEqual(parameters.map(item => [item.name, item.value]), [['no-enable-auto-tool-choice', '']]);
});
