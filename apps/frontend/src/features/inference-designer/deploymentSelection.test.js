import assert from 'node:assert/strict';
import test from 'node:test';
import { deploymentSelection, nodeParameterPaths, capabilityTogglePatch, selectionConflicts, resetDeploymentConfiguration } from './deploymentSelection.js';
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

test('configuration reset clears selections and edits while retaining target identity', () => {
    const draft = { clusterId: 'cluster', model: 'model', modelCacheEntryId: 'cached', storageVolumeId: 'storage',
        profileId: 'profile', sourceCommit: 'pinned', sourceGuides: [{ id: 'guide' }], image: 'custom',
        selectedGuides: ['pd-disaggregation'], capabilityOverrides: { cache: true }, guideChoices: { guide: ['child'] },
        sourceVariables: { guide: { VARIANT: 'custom' } }, sourceUnits: [{ path: 'custom', edits: [1] }],
        sourceNamespace: 'custom', sourceValidate: false, sourceRouter: { mode: 'gateway', version: 'custom', values: [1] } };
    const reset = resetDeploymentConfiguration(draft);
    for (const key of ['clusterId', 'model', 'modelCacheEntryId', 'storageVolumeId', 'profileId', 'sourceCommit', 'sourceGuides']) assert.deepEqual(reset[key], draft[key]);
    for (const key of ['selectedGuides', 'sourceUnits']) assert.deepEqual(reset[key], []);
    for (const key of ['capabilityOverrides', 'guideChoices', 'sourceVariables']) assert.deepEqual(reset[key], {});
    assert.deepEqual(reset.sourceRouter, { mode: 'standalone', release: 'llm-d', version: '', values: [] });
    assert.equal(reset.image, '');
    assert.equal(reset.sourceNamespace, 'llm-d');
    assert.equal(reset.sourceValidate, true);
    assert.deepEqual(resetDeploymentConfiguration(reset), reset);
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
