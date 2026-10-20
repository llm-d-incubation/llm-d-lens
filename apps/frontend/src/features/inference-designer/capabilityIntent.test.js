import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { expandCapabilityIntent } from './capabilityIntent.js';
import { officialGuideIndex } from './officialGuides.js';
import { applyEngineCapabilities, applyRouterCapabilities } from './capabilityRendering.js';
import { capabilitySources } from './capabilitySources.js';

const guide = (id, variables = {}) => ({ id, title: id, variables, disposition: { kind: 'recipe' }, steps: { deploy: { main: [] } } });
const catalog = { guides: [guide('optimized-baseline'), guide('tiered-prefix-cache', { CONNECTOR: { default: 'native', values: ['native', 'lmcache-connector'] }, VARIANT: { default: 'cpu', values: ['cpu', 'fs'] } }), guide('predicted-latency-routing')], assets: [] };
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
test('router deselection removes only its plugin and references, keeping shared producers', () => {
    const config = { plugins: [{ type: 'approx-prefix-cache-producer' }, { type: 'prefix-cache-affinity-filter', name: 'affinity' }, { type: 'token-load-scorer' }], schedulingProfiles: [{ name: 'default', plugins: [{ pluginRef: 'affinity' }, { pluginRef: 'token-load-scorer' }] }] };
    const source = yaml.dump({ router: { epp: { pluginsCustomConfig: { 'source.yaml': yaml.dump(config) } } } });
    const result = yaml.load(yaml.load(applyRouterCapabilities(source, { affinity: false })).router.epp.pluginsCustomConfig['source.yaml']);
    assert.deepEqual(result.plugins.map(plugin => plugin.type), ['approx-prefix-cache-producer', 'token-load-scorer']);
    assert.deepEqual(result.schedulingProfiles[0].plugins, [{ pluginRef: 'token-load-scorer' }]);
    assert.throws(() => applyRouterCapabilities(source, { affinity: false, 'load-routing': false }), /no scheduling policy/);
});
test('capability links pin the actual file and explanation line to the selected revision', () => {
    const source = { commit: 'a'.repeat(40), guides: [{ ...guide('optimized-baseline'), instructions: '# Baseline\n\n## Prefix cache affinity\nDetails' }], assets: [{ path: 'guides/optimized-baseline/router/values.yaml', file: 'guides/optimized-baseline/router/values.yaml', guide: 'optimized-baseline', content: 'router:\n  prefix-cache-affinity-filter: {}' }] };
    const links = capabilitySources({ id: 'affinity', guide: 'optimized-baseline', kind: 'capability' }, source, { sourceRouter: { values: [{ path: source.assets[0].path }] } });
    assert.ok(links.some(link => link.href.endsWith('README.md#L3')));
    assert.ok(links.some(link => link.href.endsWith('values.yaml#L2')));
    assert.ok(links.every(link => link.href.includes(source.commit)));
});

test('render validation rejects silently ignored engine switches and competing scaling controllers', async () => {
    const { assertRenderedCapabilities } = await import('./capabilityRendering.js');
    assert.throws(() => assertRenderedCapabilities([], { prefix: true }), /no supported independent switch/);
    const target = { name: 'worker', kind: 'Deployment' };
    assert.throws(() => assertRenderedCapabilities([{ kind: 'ScaledObject', metadata: { namespace: 'models' }, spec: { scaleTargetRef: target } }, { kind: 'HorizontalPodAutoscaler', metadata: { namespace: 'models' }, spec: { scaleTargetRef: target } }]), /Multiple autoscaling controllers/);
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
