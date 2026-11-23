import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { expandCapabilityIntent, supportedVariantValues } from './capabilityIntent.js';
import { officialGuideIndex } from './officialGuides.js';
import { applyEngineCapabilities, applyRouterCapabilities } from './capabilityRendering.js';

const guide = (id, variables = {}) => ({ id, title: id, variables, disposition: { kind: 'recipe' }, steps: { deploy: { main: [] } } });
const catalog = { guides: [guide('optimized-baseline'), guide('tiered-prefix-cache', { CONNECTOR: { default: 'native', values: ['native', 'lmcache-connector'] }, VARIANT: { default: 'cpu', values: ['cpu', 'fs'] } }), guide('predicted-latency-routing')], assets: [] };
test('automatic callers reject recipe-only and adapter-less switches', () => {
    const source = { guides: [
        guide('fast-model-actuation'), guide('turn-priority-fairness'), guide('hyperconverged-cephfs'),
        { ...guide('workload-autoscaling/wva'), instructions: '# WVA\nDeprecated. Use KEDA.' },
        { ...guide('rl'), disposition: { kind: 'external' } },
    ], assets: [] };
    assert.throws(() => expandCapabilityIntent(source, [], { capabilities: { 'context-parallel': true } }), /selected recipe/);
    assert.ok(expandCapabilityIntent(source, [], { capabilities: { 'shared-cache': true } }).errors.some(error => error.includes('no complete YAML adapter')));
});
test('official index retains public names, order, nesting and official external Guides', () => {
    const index = officialGuideIndex('## Operations\n### Integrations\n* [Reinforcement Learning](./rl/README.md) - external\n## Cache\n* [Tiered Cache](./tiered-prefix-cache/README.md) - offload\n* [Helper](../helpers/README.md)\n', [...catalog.guides, guide('rl')]);
    assert.deepEqual(index.map(item => [item.id, item.title, item.group]), [['rl', 'Reinforcement Learning', 'Operations / Integrations'], ['tiered-prefix-cache', 'Tiered Cache', 'Cache']]);
    assert.equal(index[0].indexLine, 3);
});
test('individual engine optimization does not implicitly install the whole cache Guide', () => {
    const plan = expandCapabilityIntent(catalog, [], { capabilities: { prefix: true } });
    assert.deepEqual(plan.guides, []);
    assert.ok(plan.enabled.includes('prefix'));
    assert.ok(!plan.enabled.includes('cache'));
});
test('Guide preset keeps sibling optimizations when one is disabled and requires explicit variants', () => {
    const plan = expandCapabilityIntent(catalog, ['tiered-prefix-cache'], { capabilities: { chunked: false } });
    assert.ok(plan.enabled.includes('cache'));
    assert.ok(!plan.enabled.includes('chunked'));
    assert.ok(plan.errors.some(error => error.includes('CONNECTOR')));
    assert.ok(plan.errors.some(error => error.includes('VARIANT')));
    const ready = expandCapabilityIntent(catalog, ['tiered-prefix-cache'], { capabilities: { chunked: false }, variables: { 'tiered-prefix-cache': { CONNECTOR: 'native', VARIANT: 'cpu' } } });
    assert.deepEqual(ready.errors, []);
    assert.ok(!ready.enabled.includes('disk-cache'));
});
test('disabling a single-policy preset removes its source contribution', () => {
    const plan = expandCapabilityIntent(catalog, ['predicted-latency-routing'], { capabilities: { latency: false } });
    assert.deepEqual(plan.guides, []);
    assert.ok(!plan.enabled.includes('latency'));
});
test('category strategies require a user choice and validate selected children', () => {
    const source = { guides: [{ id: 'workload-autoscaling', disposition: { kind: 'category' } }, guide('workload-autoscaling/keda-epp'), guide('workload-autoscaling/wva')], assets: [] };
    assert.match(expandCapabilityIntent(source, ['workload-autoscaling'], { capabilities: {} }).errors[0], /choose/);
    assert.deepEqual(expandCapabilityIntent(source, ['workload-autoscaling'], { capabilities: {}, guideChoices: { 'workload-autoscaling': ['workload-autoscaling/keda-epp'] } }).guides, ['workload-autoscaling/keda-epp']);
});
test('engine switches change the rendered workload and preserve sidecar and other arguments', () => {
    const docs = [{ kind: 'Deployment', spec: { template: { spec: { containers: [{ name: 'vllm', args: ['--enable-prefix-caching', '--max-num-seqs=128'] }, { name: 'routing-sidecar', args: ['--enable-prefix-caching'] }] } } } }];
    const changed = applyEngineCapabilities(docs, { prefix: false, chunked: true });
    assert.deepEqual(changed[0].spec.template.spec.containers[0].args, ['--max-num-seqs=128', '--no-enable-prefix-caching', '--enable-chunked-prefill']);
    assert.deepEqual(changed[0].spec.template.spec.containers[1], docs[0].spec.template.spec.containers[1]);
    assert.equal(docs[0].spec.template.spec.containers[0].args[0], '--enable-prefix-caching');
});
test('engine switches drop a separate boolean value of the replaced flag only', () => {
    const docs = [{ kind: 'Deployment', spec: { template: { spec: { containers: [{ name: 'vllm', args: ['--enable-prefix-caching', 'false', '--trust-remote-code', 'true'] }] } } } }];
    const changed = applyEngineCapabilities(docs, { prefix: true });
    assert.deepEqual(changed[0].spec.template.spec.containers[0].args, ['--trust-remote-code', 'true', '--enable-prefix-caching']);
});
test('router deselection removes only its plugin and references, keeping shared producers', () => {
    const config = { plugins: [{ type: 'approx-prefix-cache-producer' }, { type: 'prefix-cache-affinity-filter', name: 'affinity' }, { type: 'token-load-scorer' }], schedulingProfiles: [{ name: 'default', plugins: [{ pluginRef: 'affinity' }, { pluginRef: 'token-load-scorer' }] }] };
    const source = yaml.dump({ router: { epp: { pluginsCustomConfig: { 'source.yaml': yaml.dump(config) } } } });
    const result = yaml.load(yaml.load(applyRouterCapabilities(source, { affinity: false })).router.epp.pluginsCustomConfig['source.yaml']);
    assert.deepEqual(result.plugins.map(plugin => plugin.type), ['approx-prefix-cache-producer', 'token-load-scorer']);
    assert.deepEqual(result.schedulingProfiles[0].plugins, [{ pluginRef: 'token-load-scorer' }]);
    assert.throws(() => applyRouterCapabilities(source, { affinity: false, 'load-routing': false }), /no scheduling policy/);
});

test('literal shell launch retains quoted connector JSON and rejects shell expressions', () => {
    const doc = script => [{ kind: 'Deployment', spec: { template: { spec: { containers: [{ name: 'modelserver', command: ['/bin/bash', '-c'], args: [script] }] } } } }];
    const source = doc('exec vllm serve model --kv-transfer-config \'{"kv_connector":"OffloadingConnector"}\'');
    const result = applyEngineCapabilities(source, { prefix: true });
    const container = result[0].spec.template.spec.containers[0];
    assert.deepEqual(container.command, ['vllm', 'serve']);
    assert.equal(container.args[2], '{"kv_connector":"OffloadingConnector"}');
    assert.ok(container.args.includes('--enable-prefix-caching'));
    assert.throws(() => applyEngineCapabilities(doc('exec vllm serve $MODEL'), { prefix: true }), /shell logic/);
});

test('enabling a missing routing policy uses source definitions and preserves existing latency scoring', () => {
    const config = { plugins: [{ type: 'latency-scorer' }], schedulingProfiles: [{ name: 'default', plugins: [{ pluginRef: 'latency-scorer' }] }] };
    const content = yaml.dump({ router: { epp: { pluginsCustomConfig: { 'latency.yaml': yaml.dump(config) } } } });
    const defaults = [{ type: 'inflight-load-producer' }, { type: 'token-load-scorer', parameters: { sourceSetting: 7 } }];
    const changed = yaml.load(yaml.load(applyRouterCapabilities(content, { 'load-routing': true }, defaults)).router.epp.pluginsCustomConfig['latency.yaml']);
    assert.deepEqual(changed.plugins.map(plugin => plugin.type), ['latency-scorer', 'inflight-load-producer', 'token-load-scorer']);
    assert.equal(changed.plugins[2].parameters.sourceSetting, 7);
    assert.deepEqual(changed.schedulingProfiles[0].plugins, [{ pluginRef: 'latency-scorer' }, { pluginRef: 'token-load-scorer' }]);
});

test('accelerator support limits engine/provider variants and picks the supported default', () => {
    const support = { engines: { vllm: 'validated', sglang: 'unsupported' }, providers: [{ name: 'base' }, { name: 'rdma' }] };
    assert.deepEqual(supportedVariantValues(support, 'INFRA_PROVIDER', ['base', 'gke', 'rdma']), ['base', 'rdma']);
    assert.deepEqual(supportedVariantValues(support, 'MODEL_SERVER', ['vllm', 'sglang']), ['vllm']);
    assert.deepEqual(supportedVariantValues(undefined, 'MODEL_SERVER', ['vllm', 'sglang']), ['vllm', 'sglang']);
    const pd = { ...guide('pd', { MODEL_SERVER: { default: 'vllm', values: ['vllm', 'sglang'] } }), support: { accelerators: { xpu: support } } };
    const result = expandCapabilityIntent({ ...catalog, guides: [...catalog.guides, pd] }, ['pd'], { capabilities: {}, accelerator: 'xpu' });
    assert.equal(result.variables.pd.MODEL_SERVER, 'vllm');
    assert.ok(!result.errors.some(error => /MODEL_SERVER/.test(error)));
    const unknown = expandCapabilityIntent({ ...catalog, guides: [...catalog.guides, pd] }, ['pd'], { capabilities: {}, accelerator: 'other' });
    assert.ok(unknown.errors.some(error => /MODEL_SERVER/.test(error)));
});

test('prefix affinity insertion leaves decode scheduling unchanged', () => {
    const config = { plugins: [{ type: 'token-load-scorer' }], schedulingProfiles: ['default', 'prefill', 'decode'].map(name => ({ name, plugins: [{ pluginRef: 'token-load-scorer' }] })) };
    const content = yaml.dump({ router: { epp: { pluginsCustomConfig: { 'pd.yaml': yaml.dump(config) } } } });
    const defaults = [{ type: 'approx-prefix-cache-producer' }, { type: 'prefix-cache-affinity-filter' }];
    const changed = yaml.load(yaml.load(applyRouterCapabilities(content, { affinity: true }, defaults)).router.epp.pluginsCustomConfig['pd.yaml']);
    assert.deepEqual(changed.schedulingProfiles[2], config.schedulingProfiles[2]);
    assert.ok(changed.schedulingProfiles.slice(0, 2).every(profile => profile.plugins.some(plugin => plugin.pluginRef === 'prefix-cache-affinity-filter')));
});
