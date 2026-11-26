import assert from 'node:assert/strict';
import test from 'node:test';
import * as configuration from './configuration.js';

test('explicit P:D pairs drive the same expanded output including TP and GPU budget', () => {
    const rows = configuration.expandConfigurationTopologies({ guide: 'pd-disaggregation', replicaVariants: '99', tensorParallelVariants: '1,2', prefillReplicaVariants: '99', prefillTensorParallelVariants: '1', pdTopologyVariants: '1:1,1:3' });
    assert.deepEqual(rows.map(row => [row.prefillReplicaCount, row.replicaCount, row.tpCount, row.gpuCount]), [[1,1,1,2],[1,1,2,3],[1,3,1,4],[1,3,2,7]]);
});
test('topology parsing rejects malformed, nonpositive and over-limit expanded sweeps', () => {
    const base = { guide: 'pd-disaggregation', replicaVariants: '1', tensorParallelVariants: '1,2,3,4', prefillReplicaVariants: '1', prefillTensorParallelVariants: '1,2,3,4' };
    for (const pdTopologyVariants of ['0:1', '1:x', '1:1,1:2']) assert.throws(() => configuration.expandConfigurationTopologies({ ...base, pdTopologyVariants }));
    assert.throws(() => configuration.expandConfigurationTopologies({ ...base, guide: 'optimized-baseline', tensorParallelVariants: '1,bad' }));
});
test('Slice ceilings follow the selected placement policy and stay unknown for undiscovered keys', () => {
    const capacity = { maxSlices: { None: 3 }, topologyKeys: [{ key: 'zone', feasibleDomains: 2, exclusiveTopologyDomains: 1 }] };
    assert.equal(configuration.maxFeasibleSlices(capacity, undefined), 3);
    assert.equal(configuration.maxFeasibleSlices(capacity, { type: 'ExclusiveSlice', topology: 'zone' }), 2);
    assert.equal(configuration.maxFeasibleSlices(capacity, { type: 'ExclusiveTopology', topology: 'zone' }), 1);
    assert.equal(configuration.maxFeasibleSlices(capacity, { type: 'ExclusiveSlice', topology: 'rack' }), null);
    assert.equal(configuration.maxFeasibleSlices(null, undefined), null);
});
test('Slice recommendation needs a target and picks the fewest feasible Slices with the same per-Slice shape', () => {
    const shape = { prefillReplicas: 1, prefillTp: 2, decodeReplicas: 1, decodeTp: 2, accelerator: 'gpu' };
    const run = (slices, maximumStableQps, extra = {}) => ({ caseId: `s${slices}-${maximumStableQps}`, slices, maximumStableQps, prefillReplicas: 1, prefillTensorParallelSize: 2, decodeReplicas: 1, decodeTensorParallelSize: 2, accelerator: 'gpu', ...extra });
    const history = [run(1, 5), run(2, 10), run(3, 14), run(1, 50, { decodeReplicas: 3 })];
    assert.equal(configuration.sliceRecommendation(history, shape).status, 'needs-target');
    assert.equal(configuration.sliceRecommendation(history, shape).matched.length, 3);
    assert.equal(configuration.sliceRecommendation(history, shape, { targetRps: 8 }).entry.slices, 2);
    assert.equal(configuration.sliceRecommendation(history, shape, { targetRps: 12, maxSlices: 2 }).status, 'exceeds-capacity');
    assert.equal(configuration.sliceRecommendation(history, shape, { targetRps: 20 }).status, 'unmet');
    assert.equal(configuration.sliceRecommendation([run(2, null)], shape, { targetRps: 1 }).status, 'no-evidence');
    assert.equal(configuration.sliceRecommendation(history, { ...shape, accelerator: 'other' }).matched.length, 0);
    const profiled = [run(1, 9, { evidenceKey: 'a', finishedAt: '2026-01-02' }), run(2, 20, { evidenceKey: 'b', finishedAt: '2026-01-01', workloadLabel: 'w', slaTargets: { ttft_ms: 300 } })];
    assert.equal(configuration.sliceRecommendation(profiled, shape, { targetRps: 8, evidenceKey: 'b' }).entry.slices, 2);
    assert.deepEqual(configuration.sliceEvidenceProfiles(profiled).map(item => item.key), ['a', 'b']);
    assert.equal(configuration.sliceEvidenceProfiles(profiled)[1].label, 'w · TTFT ≤ 300 ms');
});
test('Slice sweep expansion multiplies topologies by Slice counts within the limit', () => {
    const rows = [{ replicaCount: 1 }, { replicaCount: 2 }];
    assert.deepEqual(configuration.expandSliceVariants(rows, []).map(row => row.slices), [null, null]);
    assert.deepEqual(configuration.expandSliceVariants(rows, [2, 1, 2]).map(row => [row.replicaCount, row.slices]), [[1, 1], [1, 2], [2, 1], [2, 2]]);
    assert.throws(() => configuration.expandSliceVariants(Array(9).fill({}), [1, 2]), /16 combinations/);
});
test('Evaluation cases that differ only by Slice count form a group with a cost-based recommendation', () => {
    const entry = (id, slices, maximum_stable_qps, extra = {}) => ({ case: {
        id, kind: 'guide', status: 'succeeded', configuration_artifact_id: `artifact-${id}`, scenario_id: 's', sla_targets: { ttft_ms: 500 },
        metrics: { maximum_stable_qps, throughput_tps: 100 * slices },
        deployment_configuration: { provider_ref: 'pd-disaggregation', content: {
            model: { name: 'm' }, disaggregatedSet: { slices }, prefill: { replicaCount: 1, tensorParallelSize: 2 }, decode: { replicaCount: 1, tensorParallelSize: 3 }, ...extra,
        } },
    } });
    const groups = configuration.sliceOptimizationGroups([entry('a', 2, 10), entry('b', 1, 5), entry('c', 1, 50, { decode: { replicaCount: 2, tensorParallelSize: 3 } })]);
    assert.equal(groups.length, 1);
    assert.deepEqual(groups[0].rows.map(row => [row.slices, row.accelerators]), [[1, 5], [2, 10]]);
    assert.equal(configuration.sliceGroupRecommendation(groups[0], '').status, 'needs-target');
    assert.equal(configuration.sliceGroupRecommendation(groups[0], 8).row.artifactId, 'artifact-a');
    assert.equal(configuration.sliceGroupRecommendation(groups[0], 4).row.slices, 1);
    assert.equal(configuration.sliceGroupRecommendation(groups[0], 11).status, 'unmet');
    const unmeasured = configuration.sliceOptimizationGroups([entry('a', 2, undefined), entry('b', 1, undefined)]);
    assert.equal(configuration.sliceGroupRecommendation(unmeasured[0], 1).status, 'no-slo-rate');
});
test('loading custom rows and changing one preserves the other saved overrides', () => {
    const rows = configuration.configurationCustomRows({ customParameters: [
        { target: 'both', kind: 'argument', name: 'block-size', value: '64' },
        { target: 'prefill', kind: 'environment', name: 'UCX_MEMTYPE_CACHE', value: 'n' },
        { target: 'decode', kind: 'argument', name: 'download-dir', value: '/cache' },
    ] });
    assert.equal(rows.length, 2);
    const next = rows.map((row, index) => index === 1 ? { ...row, value: '/new' } : row);
    assert.deepEqual(next[0], { target: 'prefill', kind: 'environment', name: 'UCX_MEMTYPE_CACHE', value: 'n' });
    assert.equal(configuration.configurationCustomRows({}).length, 0);
});

test('comparison draft inherits runtime controls but uses target Guide variant and fresh manifest', () => {
    const source = { deployable_configuration: { content: {
        model: { name: 'model' }, decode: { replicaCount: 2, tensorParallelSize: 4, maxModelLen: 8192 },
        guideVariant: 'vllm-rdma', officialGuide: { renderedManifest: 'old' },
        runtime: { image: 'same', storageVolumeId: 'volume' },
        customParameters: [{ target: 'both', kind: 'environment', name: 'FLAG', value: '1' }],
    } } };
    const draft = configuration.comparisonConfigurationDraft(source, { id: 'tiered-prefix-cache', evaluation: { default_variant: 'base' }, variants: ['base'] });
    assert.equal(draft.deployable_configuration.content.guideVariant, 'base');
    assert.equal(draft.deployable_configuration.content.officialGuide, undefined);
    assert.deepEqual(draft.deployable_configuration.content.decode, source.deployable_configuration.content.decode);
    assert.equal(draft.deployable_configuration.content.runtime.storageVolumeId, 'volume');
    draft.deployable_configuration.content.decode.replicaCount = 3;
    assert.equal(source.deployable_configuration.content.decode.replicaCount, 2);
});

test('comparison draft carries the original hardware selection forward instead of dropping it with officialGuide', () => {
    const source = { deployable_configuration: { content: {
        model: { name: 'model' }, decode: { replicaCount: 1 },
        officialGuide: { source: { accelerator: 'gpu' }, renderedManifest: 'old' },
    } } };
    const draft = configuration.comparisonConfigurationDraft(source, { id: 'precise-prefix-cache-routing', evaluation: { default_variant: 'base' }, variants: ['base'] });
    assert.equal(draft.deployable_configuration.content.officialGuide, undefined);
    assert.equal(draft.deployable_configuration.content.accelerator, 'gpu');
});

test('single-pool comparison inherits decode controls and omits prefill-only overrides', () => {
    const draft = configuration.comparisonConfigurationDraft({ deployable_configuration: { content: { customParameters: [
        { target: 'prefill', kind: 'environment', name: 'PREFILL_ONLY', value: '1' },
        { target: 'decode', kind: 'environment', name: 'DECODE_ONLY', value: '2' },
    ] } } }, { id: 'optimized-baseline' });
    assert.deepEqual(draft.deployable_configuration.content.customParameters.map(row => row.name), ['DECODE_ONLY']);
});

test('blank runtime controls inherit source values and explicit values are serialized', async () => {
    const { runtimeControlOverrides } = await import('./configuration.js');
    assert.deepEqual(runtimeControlOverrides({ blockSize: '', maxNumBatchedTokens: '', maxModelLen: '', maxNumSeqs: '', gpuMemoryUtilization: '' }, 'vllm'), []);
    assert.deepEqual(runtimeControlOverrides({ blockSize: '32' }, 'vllm'), [{ target: 'both', kind: 'argument', name: 'block-size', value: '32' }]);
    assert.deepEqual(runtimeControlOverrides({ maxModelLen: 4096 }, 'vllm'), [{ target: 'both', kind: 'argument', name: 'max-model-len', value: '4096' }]);
    assert.deepEqual(runtimeControlOverrides({ blockSize: '32' }, 'sglang'), []);
});

test('role-specific tuning stays in the editable overrides on reload', () => {
    const parameter = { target: 'prefill', kind: 'argument', name: 'max-num-seqs', value: '128' };
    assert.deepEqual(configuration.configurationCustomRows({ customParameters: [parameter] }), [parameter]);
});

test('historical decode tuning facts do not become conflicting shared overrides on reload', async () => {
    const { configurationRuntimeValues } = await import('./configuration.js');
    const content = { decode: { maxNumSeqs: 64, maxModelLen: 8192 }, customParameters: [
        { target: 'decode', kind: 'argument', name: 'max-num-seqs', value: '64' },
        { target: 'prefill', kind: 'argument', name: 'max-model-len', value: '4096' },
    ] };
    assert.equal(configurationRuntimeValues(content).maxNumSeqs, '');
    assert.equal(configurationRuntimeValues(content).maxModelLen, '');
});

test('non-vLLM native custom arguments are preserved when reopening a configuration', () => {
    const parameter = { target: 'both', kind: 'argument', name: 'max-num-seqs', value: '128' };
    assert.deepEqual(configuration.configurationCustomRows({ runtime: { modelServer: 'sglang' }, customParameters: [parameter] }), [parameter]);
});

test('edited YAML keeps provenance and recalculates manifest and configuration checksums', async () => {
    const { editedEvaluationConfiguration } = await import('./configuration.js');
    const { configurationChecksum, sha256 } = await import('./checksum.js');
    const artifact = {artifact_id: 'original', deployable_configuration: {provider_ref: 'optimized-baseline', content: {model: {name: 'model'}, officialGuide: {variant: 'base'}}, provenance: {cluster_ref: {id: 'cluster'}}}};
    const before = structuredClone(artifact);
    const result = await editedEvaluationConfiguration(artifact, 'kind: Service\n', {now: () => 'edited-time'});
    assert.deepEqual(artifact, before);
    assert.equal(result.content.officialGuide.renderedManifest, 'kind: Service\n');
    assert.equal(result.content.officialGuide.manifestChecksum, await sha256('kind: Service\n'));
    assert.equal(result.checksum, await configurationChecksum(result.content));
    assert.equal(result.provenance.edited_from_artifact_id, 'original');
    assert.equal(result.provenance.edited_at, 'edited-time');
    assert.deepEqual(result.provenance.cluster_ref, {id: 'cluster'});
});


test('legacy paired replica inputs match generated topologies', () => {
    for (const pairs of ['2:2', '1:3,3:1', ' 2 : 2 ']) {
        const parsed = configuration.parsePdReplicaPairs(pairs);
        const topologies = configuration.expandConfigurationTopologies({ guide: 'pd-disaggregation', pdTopologyVariants: pairs, prefillReplicaVariants: '1', replicaVariants: '1', prefillTensorParallelVariants: '1', tensorParallelVariants: '1' });
        assert.deepEqual(parsed, topologies.map(row => [row.prefillReplicaCount, row.replicaCount]));
    }
    for (const value of ['2:', '0:2', '9007199254740992:2']) assert.throws(() => configuration.parsePdReplicaPairs(value));
});


test('explicit PD rows generate only the entered configurations with independent replica and TP values', () => {
    const topologies = [
        { id: 'first', prefillReplicaCount: '2', prefillTpCount: '1', replicaCount: '2', tpCount: '4' },
        { id: 'second', prefillReplicaCount: '1', prefillTpCount: '2', replicaCount: '3', tpCount: '1' },
    ];
    const inputs = { guide: 'pd-disaggregation', topologies };
    const expanded = configuration.expandConfigurationTopologies(inputs);
    assert.deepEqual(expanded, [
        { prefillReplicaCount: 2, prefillTpCount: 1, replicaCount: 2, tpCount: 4, gpuCount: 10 },
        { prefillReplicaCount: 1, prefillTpCount: 2, replicaCount: 3, tpCount: 1, gpuCount: 5 },
    ]);
    assert.deepEqual(configuration.expandConfigurationTopologies({ ...inputs, topologies: [topologies[0]] }), [expanded[0]]);
    assert.deepEqual(configuration.expandSliceVariants(expanded, [1, 2]).map(row => [row.prefillReplicaCount, row.replicaCount, row.slices]), [[2, 2, 1], [2, 2, 2], [1, 3, 1], [1, 3, 2]]);
    assert.equal(topologies[0].prefillReplicaCount, '2');
});

test('explicit rows reject invalid cells and enforce the configuration limit', () => {
    const row = { prefillReplicaCount: 1, prefillTpCount: 1, replicaCount: 1, tpCount: 1 };
    const expand = topologies => configuration.expandConfigurationTopologies({ guide: 'pd-disaggregation', topologies });
    assert.equal(expand(Array(16).fill(row)).length, 16);
    assert.throws(() => expand(Array(17).fill(row)), /1 and 16/);
    assert.throws(() => expand([]), /1 and 16/);
    for (const field of Object.keys(row)) {
        for (const value of ['', ' ', '1,2', 0, -1, 1.5, NaN, Infinity, true, null, undefined, 9007199254740992]) {
            assert.throws(() => expand([row, { ...row, [field]: value }]), /positive whole number/, `${field}: ${value}`);
        }
    }
});


test('PD ranges, lists and stepped ranges expand into one deduplicated parameter space', () => {
    assert.deepEqual(configuration.parseTopologyValues('1-8/2,2，4,1'), [1, 2, 3, 4, 5, 7]);
    const sweep = { ...configuration.topologySweepFromConfiguration(), prefillReplicaVariants: '1-4', replicaVariants: '1-8', prefillTensorParallelVariants: '1,2', tensorParallelVariants: '1,2' };
    const result = configuration.analyzeConfigurationSweep(sweep);
    assert.equal(result.rawCount, 128);
    assert.equal(result.variants.length, 128);
    assert.deepEqual(result.errors, []);
    const filtered = { ...sweep, maxAccelerators: '8' };
    const preview = configuration.analyzeConfigurationSweep(filtered);
    assert.equal(preview.rawCount, 128);
    assert.equal(preview.variants.length, 50);
    assert.deepEqual(configuration.expandConfigurationTopologies({ guide: 'pd-disaggregation', sweep: filtered }), preview.variants);
    assert.equal(configuration.analyzeConfigurationSweep({ ...sweep, relation: 'equal' }).variants.length, 16);
    const exact = configuration.analyzeConfigurationSweep({ ...sweep, fixedAccelerators: '8', relation: 'decode' });
    assert.ok(exact.variants.length > 0);
    assert.ok(exact.variants.every(row => row.gpuCount === 8 && row.prefillReplicaCount <= row.replicaCount));
});

test('PD sweeps bound parsing and expansion without silently truncating results', () => {
    for (const value of ['', '1,', '0', '8-1', '1-8/0', '1.5', '1-129', '9007199254740992']) assert.throws(() => configuration.parseTopologyValues(value));
    const single = configuration.topologySweepFromConfiguration({ prefillReplicaCount: 2, replicaCount: 2, prefillTpCount: 1, tpCount: 1 });
    assert.deepEqual(configuration.analyzeConfigurationSweep(single, 1).variants, [{ prefillReplicaCount: 2, replicaCount: 2, prefillTpCount: 1, tpCount: 1, gpuCount: 4 }]);
    assert.match(configuration.analyzeConfigurationSweep({ ...single, replicaVariants: '1,2' }, 1).errors[0].message, /one configuration/);
    assert.match(configuration.analyzeConfigurationSweep({ ...single, fixedAccelerators: '8', maxAccelerators: '4' }).errors[0].message, /No configurations/);
    assert.match(configuration.analyzeConfigurationSweep({ ...single, prefillReplicaVariants: '1-128', replicaVariants: '1-128', tensorParallelVariants: '1-128' }).errors[0].message, /100,000/);
    const large = { ...single, prefillReplicaVariants: '1-16', replicaVariants: '1-17' };
    assert.equal(configuration.analyzeConfigurationSweep(large).variants.length, 272);
    assert.throws(() => configuration.expandConfigurationTopologies({ sweep: large }), /256/);
    assert.equal(configuration.analyzeConfigurationSweep({ ...large, relation: 'equal' }).errors.length, 0);
    const rows = configuration.analyzeConfigurationSweep({ ...single, prefillReplicaVariants: '1-128' }).variants;
    assert.equal(configuration.expandSliceVariants(rows, [1, 2], configuration.MAX_EVALUATION_CONFIGURATIONS).length, 256);
    assert.throws(() => configuration.expandSliceVariants(rows, [1, 2, 3], configuration.MAX_EVALUATION_CONFIGURATIONS), /256/);
    assert.throws(() => configuration.expandSliceVariants(Array(257).fill({}), [], configuration.MAX_EVALUATION_CONFIGURATIONS), /256/);
});

test('accepted AIC and history topologies hydrate the same scalar range fields with clear filters', () => {
    const sweep = configuration.topologySweepFromConfiguration({ prefillReplicaCount: 2, prefillTpCount: 4, replicaCount: 3, tpCount: 2 });
    assert.equal(sweep.relation, 'all');
    assert.equal(sweep.maxAccelerators, '');
    assert.equal(sweep.fixedAccelerators, '');
    assert.deepEqual(configuration.expandConfigurationTopologies({ sweep }), [{ prefillReplicaCount: 2, prefillTpCount: 4, replicaCount: 3, tpCount: 2, gpuCount: 14 }]);
});
