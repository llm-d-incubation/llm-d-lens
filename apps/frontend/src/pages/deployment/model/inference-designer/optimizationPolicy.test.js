import test from 'node:test';
import assert from 'node:assert/strict';
import { createBlueprint } from './blueprint';
import { DEFAULT_DEPLOYMENT_INTENT, MANUAL_PRIORITY, deploymentIntentErrors, objectivePriority, recommendOptimizations } from './optimizationPolicy';
import { automaticSliceSizing, isDisaggregatedDraft, matchesSliceRecommendation, sliceTopologyKeys } from './sliceLayout';
import { compositionDisplayDraft } from './compositionPolicy';

const catalog = {
    guides: [{ id: 'optimized-baseline', title: 'Baseline', variables: {}, steps: { deploy: {
        main: [{ run: 'kubectl apply -k ${REPO_ROOT}/guides/optimized-baseline/modelserver/base' }],
    } } }],
    assets: [{ path: 'guides/optimized-baseline/modelserver/base', guide: 'optimized-baseline', kind: 'kustomize' }],
};
const draft = () => ({ ...createBlueprint(), selectedGuides: [], capabilityOverrides: {}, deploymentIntent: { ...DEFAULT_DEPLOYMENT_INTENT } });
const profile = { upstream_variant: 'fixture' };

test('workload inputs reject invalid bounds without inventing hardware defaults', () => {
    assert.deepEqual(deploymentIntentErrors(DEFAULT_DEPLOYMENT_INTENT), []);
    for (const value of ['0', '-1', '1.5', 'NaN', 'Infinity', '9007199254740992']) {
        assert.match(deploymentIntentErrors({ ...DEFAULT_DEPLOYMENT_INTENT, maxDevices: value }).join(' '), /positive integer/);
    }
    assert.throws(() => recommendOptimizations(catalog, draft(), null), /accelerator profile/);
});

test('recommendation preserves manual disable and does not provision external systems', () => {
    const result = recommendOptimizations(catalog, { ...draft(), capabilityOverrides: { prefix: false } }, profile);
    assert.equal(result.capabilityOverrides.prefix, false);
    for (const id of ['agentic', 'batch', 'shared-cache', 'snapshot', 'weights', 'scaling']) assert.notEqual(result.capabilityOverrides[id], true);
    assert.ok(result.optimizationDecisions.every(item => item.reason));
});

test('manual source edits protect engine parameters from automatic switches', () => {
    const result = recommendOptimizations(catalog, { ...draft(), sourceUnits: [{ path: 'fixture', edits: [{ path: [0], value: 1 }] }] }, profile);
    assert.equal(result.capabilityOverrides.chunked, undefined);
    assert.match(result.optimizationDecisions.find(item => item.id === 'chunked').reason, /manually edited/);
});

test('explicit locks survive changed optimization objectives', () => {
    const result = recommendOptimizations(catalog, {
        ...draft(), deploymentIntent: { ...DEFAULT_DEPLOYMENT_INTENT, objective: 'cost' },
        capabilityOverrides: { chunked: false }, automaticCapabilities: ['chunked'], optimizationLocks: ['chunked'],
    }, profile);
    assert.equal(result.capabilityOverrides.chunked, false);
});

test('manual includes compatible additions; agentic keeps only objective-relevant ones', () => {
    const guide = { id: 'fixture-addon', title: 'Revision-owned addition', variables: {}, steps: { deploy: {
        main: [{ run: 'kubectl apply -k SOURCE/guides/fixture-addon/resources' }],
    } } };
    const source = { ...catalog, guides: [...catalog.guides, guide], assets: [...catalog.assets, { path: 'guides/fixture-addon/resources', guide: guide.id, kind: 'kustomize' }] };
    const manual = recommendOptimizations(source, { ...draft(), planningMode: 'manual' }, profile);
    const agentic = recommendOptimizations(source, { ...draft(), planningMode: 'agentic' }, profile);
    assert.equal(manual.capabilityOverrides['guide:fixture-addon'], true);
    assert.notEqual(agentic.capabilityOverrides['guide:fixture-addon'], true);
    assert.match(agentic.optimizationDecisions.find(item => item.id === 'guide:fixture-addon').reason, /not expected to improve the balanced objective/);
    const excluded = recommendOptimizations(source, { ...draft(), planningMode: 'manual', capabilityOverrides: { 'guide:fixture-addon': false } }, profile);
    assert.equal(excluded.capabilityOverrides['guide:fixture-addon'], false);
    assert.notEqual(manual.capabilityOverrides.tracing, true);
});

test('manual uses one fixed priority while agentic follows the objective', () => {
    assert.deepEqual(MANUAL_PRIORITY.slice(0, 5), ['prefix', 'chunked', 'affinity', 'load-routing', 'cache']);
    assert.ok(!MANUAL_PRIORITY.includes('index'));
    assert.equal(objectivePriority({ objective: 'throughput', scenario: 'chat' })[0], 'chunked');
    assert.ok(objectivePriority({ objective: 'reliability', scenario: 'chat' }).includes('flow-control'));
    assert.ok(!objectivePriority({ objective: 'latency', scenario: 'chat' }).includes('flow-control'));
    assert.equal(objectivePriority({ objective: 'latency', scenario: 'agent' })[0], 'prefix');
});

test('slice topology keys run narrow to wide and P/D display reads DisaggregatedSet roles', () => {
    const keys = sliceTopologyKeys([{ key: 'topology.kubernetes.io/zone' }, { key: 'example.com/rack' }, { key: 'kubernetes.io/hostname' }, { key: 'team' }]);
    assert.deepEqual(keys, ['kubernetes.io/hostname', 'example.com/rack', 'topology.kubernetes.io/zone', 'team']);
    assert.equal(isDisaggregatedDraft({ runtimeConfiguration: { prefillReplicas: 1 } }), true);
    assert.equal(isDisaggregatedDraft({ runtimeConfiguration: { replicas: 1 } }), false);
    const role = (name, replicas, tp) => ({ name, spec: { replicas, leaderWorkerTemplate: { workerTemplate: { spec: { containers: [{ name: 'modelserver', args: ['--tensor-parallel-size', String(tp)] }] } } } } });
    const content = `kind: DisaggregatedSet\nspec:\n  slices: 2\n  roles: ${JSON.stringify([role('prefill', 1, 2), role('decode', 3, 4)])}\n`;
    const display = compositionDisplayDraft({}, { sourceComposition: true, files: [{ name: 'deployment.yaml', content }] });
    assert.deepEqual([display.slices, display.prefillReplicas, display.prefillTp, display.replicas, display.tp], ['2', '1', '2', '3', '4']);
});

test('returning to automatic slices preserves total replicas and recommendation tracks both roles', () => {
    const runtime = { slices: 3, prefillReplicas: 2, replicas: 4, placementPolicy: { type: 'None' } };
    const automatic = { ...runtime, ...automaticSliceSizing(runtime) };
    assert.equal(automatic.slices, undefined);
    assert.equal(automatic.prefillReplicas, 6);
    assert.equal(automatic.replicas, 12);
    assert.deepEqual(automaticSliceSizing(automatic), automaticSliceSizing(runtime));
    assert.equal(matchesSliceRecommendation(runtime, runtime), true);
    assert.equal(matchesSliceRecommendation({ ...runtime, prefillReplicas: 3 }, runtime), false);
    assert.equal(matchesSliceRecommendation({ ...runtime, replicas: 5 }, runtime), false);
});
