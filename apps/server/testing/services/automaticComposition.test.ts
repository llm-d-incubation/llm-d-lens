import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { composeOptimizationPolicy, validateAutomaticBundle } from '../../src/services/automaticComposition.ts';
import { applyCompositionRuntime } from '../../src/services/compositionRuntime.ts';
import { createBlueprint } from '../../../frontend/src/features/inference-designer/blueprint.js';
import { compositionDefaults, compositionRequest } from '../../../frontend/src/features/inference-designer/compositionPolicy.js';

const serving = (id: string) => ({ id, variables: {}, steps: { deploy: { worker: { run: `kubectl apply -k SOURCE/guides/${id}/modelserver/fixture` } } } });
const catalog = { environment: {}, guides: [serving('optimized-baseline'), serving('pd-disaggregation')],
    assets: ['optimized-baseline', 'pd-disaggregation'].map(guide => ({ guide, path: `guides/${guide}/modelserver/fixture`, kind: 'kustomize' })) };
const draft = () => ({ ...createBlueprint(), sourceMode: true, selectedGuides: ['optimized-baseline'], capabilityOverrides: {}, guideChoices: {}, model: 'org/model' });
const profile = { id: 'fixture', resource_prefixes: ['example.com/cards'], device_classes: ['example-cards'] };
const engine = (name = 'decode') => ({ apiVersion: 'apps/v1', kind: 'Deployment', metadata: { name }, spec: { replicas: 1,
    template: { metadata: {}, spec: { containers: [{ name: 'modelserver', command: ['vllm', 'serve'], args: ['org/model', '--tensor-parallel-size=1'],
        resources: { requests: { 'example.com/cards': 1 }, limits: { 'example.com/cards': 1 } } }] } } } });
const bundle = (documents: any[]) => ({ files: [{ name: 'deployment.yaml', content: documents.map(doc => yaml.dump(doc)).join('---\n') }] });

test('Advanced enables compatible switches and reports why an incompatible topology failed', async () => {
    const rendered: any[] = [];
    const result = await composeOptimizationPolicy({ catalog, draft: draft(), mode: 'advanced', selection: { enabled: [], rationale: '' },
        render: async (next: any) => {
            if (next.sourcePlan.guides.includes('pd-disaggregation')) throw new Error('Prefill resources exceed the budget');
            rendered.push(next); return { files: [] };
        } });
    assert.equal(result.draft.capabilityOverrides.prefix, true);
    assert.equal(result.draft.capabilityOverrides.chunked, true);
    assert.equal(result.decisions.find((item: any) => item.id === 'transfer').state, 'blocked');
    assert.match(result.decisions.find((item: any) => item.id === 'transfer').reason, /Prefill|Competing/);
    assert.equal(rendered.at(-1), result.draft);
    assert.equal(draft().capabilityOverrides.prefix, undefined);
});

test('Agentic never adds an optimization outside its selection and source dependencies', async () => {
    const result = await composeOptimizationPolicy({ catalog, draft: draft(), mode: 'agentic',
        selection: { enabled: ['prefix'], rationale: 'Repeated prompt prefixes' }, render: async () => ({ files: [] }) });
    assert.equal(result.draft.capabilityOverrides.prefix, true);
    assert.equal(result.draft.capabilityOverrides.chunked, undefined);
    assert.equal(result.decisions.find((item: any) => item.id === 'chunked').state, 'not_selected');
});

test('authorization errors stop composition rather than becoming optional optimization failures', async () => {
    let calls = 0;
    await assert.rejects(composeOptimizationPolicy({ catalog, draft: draft(), mode: 'advanced', selection: { enabled: [] }, render: async () => {
        if (calls++) throw Object.assign(new Error('Forbidden'), { status: 403 });
        return {};
    } }), /Forbidden/);
});

test('runtime topology changes both model TP and registry-owned device requests', () => {
    const docs = [engine('prefill'), engine('decode')];
    applyCompositionRuntime(docs, { profileId: 'fixture', replicas: 2, tp: 4, prefillReplicas: 1, prefillTp: 2, maxModelLen: 8192 }, profile, 'org/model');
    assert.equal(docs[1].spec.replicas, 2);
    const container = docs[1].spec.template.spec.containers[0];
    assert.equal(container.resources.requests['example.com/cards'], 4);
    assert.ok(container.args.some(arg => arg === '--tensor-parallel-size=4' || arg === '4'));
    assert.ok(container.args.includes('--max-model-len=8192'));
    assert.equal(validateAutomaticBundle(bundle(docs), ['Deployment'], 10, profile).acceleratorCount, 10);
    assert.throws(() => validateAutomaticBundle(bundle(docs), ['Deployment'], 9, profile), /budget is 9/);
    assert.throws(() => applyCompositionRuntime([engine('prefill')], { profileId: 'fixture', replicas: 1, tp: 1 }, profile, 'org/model'), /Prefill/);
});

test('DRA requests count against capacity and unsupported lifecycle kinds cannot be published', () => {
    const doc: any = engine();
    doc.spec.template.spec.containers[0].resources = {};
    doc.spec.template.spec.resourceClaims = [{ name: 'cards', resourceClaimTemplateName: 'claim' }];
    const claim = { kind: 'ResourceClaimTemplate', metadata: { name: 'claim' }, spec: { spec: { devices: { requests: [{ exactly: { deviceClassName: 'example-cards', count: 3 } }] } } } };
    assert.equal(validateAutomaticBundle(bundle([doc, claim]), ['Deployment', 'ResourceClaimTemplate'], 4, profile).acceleratorCount, 3);
    assert.throws(() => validateAutomaticBundle(bundle([{ kind: 'ClusterRole' }]), ['Deployment'], 4, profile), /ClusterRole/);
});

test('source defaults are explicit without evaluating source expressions, and edits share the same transport', () => {
    const defaults = compositionDefaults({ guides: [{ id: 'guide', variables: { MODE: { default: 'base', values: ['base'] }, BAD: { default: 'other', values: ['base'] }, EXEC: { default: '$(id)' } } }] });
    assert.deepEqual(defaults, { guide: { MODE: 'base' } });
    const edited = { ...draft(), runtimeConfiguration: { tp: 2 }, finalConfiguration: { baseChecksum: 'hash', content: 'edited' } };
    const request = compositionRequest(edited, 'session');
    assert.deepEqual(request.finalConfiguration, edited.finalConfiguration);
    assert.deepEqual(request.runtimeConfiguration, edited.runtimeConfiguration);
});
