import assert from 'node:assert/strict';
import test from 'node:test';
import { deploymentSelection, nodeParameterPaths, capabilityTogglePatch, selectionConflicts } from './deploymentSelection.js';
import { optimizationSourceFacts } from '@repo/shared/inference/optimizations';
import { inferenceScene } from './sceneGraph.js';
import { createBlueprint } from './blueprint.js';
const serving = id => ({ id, variables: {}, steps: { deploy: { worker: { run: `kubectl apply -k SOURCE/guides/${id}/modelserver/cpu` } } } });
const catalog = { environment: {}, guides: [serving('optimized-baseline'), serving('pd-disaggregation'), { id: 'runtime-inventory', variables: {} }], assets: [{ path: 'guides/optimized-baseline/modelserver/cpu', kind: 'kustomize' }, { path: 'guides/pd-disaggregation/modelserver/cpu', kind: 'kustomize' }, { path: 'inventory-chart', guide: 'runtime-inventory', kind: 'chart' }] };
test('empty selection restores baseline; a serving Guide replaces it, and add-ons keep it', () => {
    assert.deepEqual(deploymentSelection(catalog).requested, ['optimized-baseline']);
    const pd = deploymentSelection(catalog, ['optimized-baseline', 'pd-disaggregation']);
    assert.deepEqual(pd.requested, ['pd-disaggregation']);
    assert.equal(pd.errors.length, 0);
    assert.deepEqual(deploymentSelection(catalog, ['runtime-inventory']).requested, ['optimized-baseline', 'runtime-inventory']);
});
test('node parameter entry exists only for owned selected resources', () => {
    const draft = { sourceUnits: [{ path: 'guides/optimized-baseline/modelserver/cpu', guide: 'optimized-baseline' }], sourceRouter: { values: [{ path: 'guides/recipes/router/base.values.yaml' }] } };
    assert.deepEqual(nodeParameterPaths({ id: 'prepare' }, draft), []);
    assert.deepEqual(nodeParameterPaths({ id: 'epp' }, draft), ['guides/recipes/router/base.values.yaml']);
    assert.deepEqual(nodeParameterPaths({ id: 'decode' }, draft), ['guides/optimized-baseline/modelserver/cpu']);
    assert.deepEqual(nodeParameterPaths({ kind: 'capability', guide: 'pd-disaggregation' }, draft), []);
});

test('direct capability selection changes only its independent override', () => {
    const draft = { selectedGuides: ['tiered-prefix-cache'], capabilityOverrides: {} };
    const cap = { id: 'prefix', guide: 'tiered-prefix-cache', enabled: true };
    const patch = capabilityTogglePatch(cap, draft);
    assert.deepEqual(patch.capabilityOverrides, { prefix: false });
    assert.equal(patch.selectedGuides, undefined);
    assert.equal(capabilityTogglePatch({ ...cap, required: true }, draft), null);
});
test('disk selection explicitly includes its CPU dependency', () => {
    const patch = capabilityTogglePatch({ id: 'disk-cache', enabled: false }, { capabilityOverrides: {} });
    assert.deepEqual(patch.capabilityOverrides, { 'disk-cache': true, cache: true });
});

test('native CPU cache can be disabled and restored without adding a competing serving Guide', () => {
    const draft = { nativeCpuCacheGiB: 6, capabilityOverrides: { prefix: true }, selectedGuides: ['pd-disaggregation'] };
    const disabled = { ...draft, ...capabilityTogglePatch({ id: 'cache', enabled: true }, draft) };
    assert.equal(disabled.nativeCpuCacheGiB, 6);
    assert.equal(disabled.capabilityOverrides.cache, false);
    const restored = { ...disabled, ...capabilityTogglePatch({ id: 'cache', enabled: false }, disabled) };
    assert.deepEqual(restored.capabilityOverrides, { prefix: true });
    assert.equal(restored.nativeCpuCacheGiB, 6);
    assert.deepEqual(restored.selectedGuides, ['pd-disaggregation']);
});

test('monitoring-only add-ons retain the baseline serving fallback', () => {
    const addon = { id: 'monitoring-addon', variables: {}, steps: { deploy: [{ run: 'kubectl apply -k SOURCE/guides/recipes/modelserver/components/monitoring' }] } };
    const fixture = { ...catalog, guides: [...catalog.guides, addon], assets: [...catalog.assets, { path: 'guides/recipes/modelserver/components/monitoring', kind: 'component' }] };
    const plan = deploymentSelection(fixture, [addon.id]);
    assert.deepEqual(plan.errors, []);
    assert.ok(plan.guides.includes('optimized-baseline'));
    assert.ok(plan.units.some(unit => unit.path === 'guides/optimized-baseline/modelserver/cpu'));
});


test('selection guards reuse serving conflicts and keep compatible add-ons selectable', () => {
    const fixture = { ...catalog, guides: [...catalog.guides, serving('tiered-prefix-cache')],
        assets: [...catalog.assets, { path: 'guides/tiered-prefix-cache/modelserver/cpu', kind: 'kustomize' }] };
    const draft = { selectedGuides: ['pd-disaggregation'], capabilityOverrides: {}, sourceRouter: { mode: 'standalone' } };
    const patch = capabilityTogglePatch({ id: 'cache', enabled: false }, draft);
    assert.match(selectionConflicts(fixture, draft, patch)[0], /Competing serving topologies/);
    assert.deepEqual(selectionConflicts(fixture, draft, { selectedGuides: ['pd-disaggregation', 'runtime-inventory'] }), []);
    assert.deepEqual(draft.capabilityOverrides, {});
    assert.deepEqual(selectionConflicts(fixture, draft, { capabilityOverrides: { prefix: true } }), []);
});


test('baseline routing state reads planned values from the pinned catalog and respects overrides', () => {
    const path = 'guides/optimized-baseline/router/standalone.values.yaml';
    const baseline = serving('optimized-baseline');
    baseline.steps.deploy.router = { run: `helm upgrade --install baseline oci://example/llm-d-router-standalone -f SOURCE/${path}` };
    const fixture = { ...catalog, guides: [baseline], assets: [...catalog.assets, { path, kind: 'values', guide: 'optimized-baseline', content: `router:
  epp:
    pluginsConfigFile: default.yaml
    pluginsCustomConfig:
      default.yaml: |
        plugins:
          - type: prefix-cache-affinity-filter
          - type: token-load-scorer
` }] };
    const plan = deploymentSelection(fixture, [], { capabilities: {} });
    assert.ok(plan.enabledCapabilities.includes('affinity'));
    assert.ok(plan.enabledCapabilities.includes('load-routing'));
    assert.ok(plan.enabledCapabilities.includes('parallel'));
    const disabled = deploymentSelection(fixture, [], { capabilities: { affinity: false } });
    assert.ok(!disabled.enabledCapabilities.includes('affinity'));
    assert.ok(disabled.enabledCapabilities.includes('load-routing'));
    assert.equal(plan.router.values[0].content, undefined);
});

test('model recipes expose inherited mechanisms without selecting competing serving Guides', () => {
    const id = 'models/custom';
    const model = { id, variables: {}, steps: { deploy: { worker: { run: `kubectl apply -k SOURCE/guides/${id}/modelserver` } } } };
    const path = `guides/${id}/modelserver`;
    const fixture = { ...catalog, guides: [...catalog.guides, model], assets: [...catalog.assets, { path, kind: 'kustomize', guide: id }],
        optimizations: { sources: [
            optimizationSourceFacts(`${path}/kustomization.yaml`, [{ kind: 'Kustomization', resources: ['worker.yaml'] }]),
            optimizationSourceFacts(`${path}/worker.yaml`, [{ kind: 'DisaggregatedSet', spec: { roles: [
                { name: 'prefill', spec: { args: ['--prefill-context-parallel-size=8', '--kv-transfer-config={"kv_connector":"OffloadingConnector"}'] } },
                { name: 'decode', spec: { args: ['--speculative-config={"method":"mtp"}', '--enable-expert-parallel'] } },
            ] } }]),
        ] } };
    const plan = deploymentSelection(fixture, [id], { capabilities: {} });
    assert.deepEqual(plan.errors, []);
    assert.deepEqual(plan.guides, [id]);
    for (const feature of ['transfer', 'cache', 'speculation', 'wide-ep', 'context-parallel']) {
        assert.ok(plan.enabledCapabilities.includes(feature), feature);
        assert.ok(plan.requiredCapabilities.includes(feature), feature);
    }
    const graph = inferenceScene({ ...createBlueprint(), sourceMode: true, sourcePlan: plan, sourceUnits: plan.units });
    assert.ok(graph.nodes.some(node => node.id === 'prefill-worker'));
    assert.ok(graph.nodes.some(node => node.id === 'decode-worker'));
    assert.ok(!graph.nodes.some(node => node.id === 'serving-worker'));
    const speculation = graph.capabilities.find(item => item.id === 'speculation');
    assert.equal(capabilityTogglePatch(speculation, {}), null);
    assert.deepEqual(nodeParameterPaths(speculation, { sourceUnits: plan.units }), [path]);
    assert.match(deploymentSelection(fixture, [id], { capabilities: { speculation: false } }).errors.join(' '), /declared by the selected recipe/);
    const edited = deploymentSelection(fixture, [id], { capabilities: {}, units: [{ path, replacementContent: 'edited' }] });
    assert.ok(!edited.enabledCapabilities.includes('speculation'));
    assert.equal(edited.sourceFeatures.features.find(item => item.id === 'speculation').state, 'unknown');
});

test('recipe-only and external optimizations are inspectable but not independent toggles', () => {
    for (const capability of [
        { id: 'context-parallel', selection: 'recipe' },
        { id: 'shared-cache', generation: 'requires-adapter' },
        { id: 'rollouts', generation: 'external-operation' },
    ]) assert.equal(capabilityTogglePatch(capability, {}), null);
});
