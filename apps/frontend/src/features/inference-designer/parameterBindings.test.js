import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { createBlueprint, blueprintRequest, parseBlueprint } from './blueprint.js';
import { structuredParameters } from './parameterBindings.js';
import { routerParameterDocument, changeRouterParameter } from './routerParameters.js';
import { CAPABILITIES } from './capabilityCatalog.js';
import { inferenceScene } from './sceneGraph.js';
const profile = { id: 'fixture', upstream_variant: 'fixture' };
const providers = [{ id: 'optimized-baseline', supported: true, accelerators: ['fixture'] }];
const base = () => ({ ...createBlueprint(), model: 'org/model', clusterId: 'cluster', profileId: 'fixture' });
test('structured engine and router settings reach the planner without changing scalar types', () => {
    const draft = { ...base(), prefixCaching: 'enabled', chunkedPrefill: 'enabled', dtype: 'bfloat16', eppReplicas: '2', tracingEnabled: 'true', samplerArg: '0.25' };
    const request = blueprintRequest(draft, providers, profile, 'session');
    assert.deepEqual(request.customParameters.map(item => [item.name, item.value]), [['enable-prefix-caching', ''], ['enable-chunked-prefill', ''], ['dtype', 'bfloat16']]);
    const values = yaml.load(request.guideSettings.routerValues);
    assert.equal(values.router.epp.replicas, 2);
    assert.equal(values.router.tracing.enabled, true);
    assert.equal(values.router.tracing.sampling.samplerArg, '0.25');
    assert.deepEqual(parseBlueprint(yaml.dump(draft)), draft);
});
test('source and expert conflicts cannot silently override dedicated controls', () => {
    assert.throws(() => structuredParameters({ ...base(), eppReplicas: '2', routerValues: 'router: {epp: {replicas: 3}}' }), /conflicts/);
    assert.throws(() => blueprintRequest({ ...base(), prefixCaching: 'enabled', engineOverrides: '- {target: both, kind: argument, name: enable-prefix-caching, value: ""}' }, providers, profile, 'session'), /Conflicts/);
    for (const patch of [{ eppReplicas: '1.2' }, { eppVerbosity: '-1' }, { samplerArg: '2' }, { prefixCaching: 'false' }, { failureMode: 'unknown' }]) assert.throws(() => structuredParameters({ ...base(), ...patch }));
    assert.throws(() => blueprintRequest({ ...base(), routerSourceKey: 'other' }, providers, profile, 'session'), /snapshot/);
});
test('source plugin edits preserve all other plugins, profiles and embedded YAML', () => {
    const plugins = { plugins: [{ type: 'first', parameters: { count: 5, enabled: true } }, { type: 'second', parameters: { count: 9 } }], schedulingProfiles: [{ name: 'default', plugins: [{ pluginRef: 'first', weight: 1 }] }] };
    const text = yaml.dump({ router: { epp: { pluginsConfigFile: 'plugins.yaml', pluginsCustomConfig: { 'plugins.yaml': yaml.dump(plugins) } } } });
    const leaf = routerParameterDocument(text).leaves.find(item => item.key.endsWith('plugins.0.parameters.count'));
    const edited = changeRouterParameter(text, leaf, '12');
    const parsed = yaml.load(yaml.load(edited).router.epp.pluginsCustomConfig['plugins.yaml']);
    assert.equal(parsed.plugins[0].parameters.count, 12);
    assert.deepEqual(parsed.plugins[1], plugins.plugins[1]);
    assert.deepEqual(parsed.schedulingProfiles, plugins.schedulingProfiles);
    assert.throws(() => changeRouterParameter(text, leaf, ''), /finite/);
    assert.throws(() => routerParameterDocument('router: { __proto__: { x: 1 } }'), /Unsafe/);
});
test('every catalog capability has a real scene attachment and pinned reference', () => {
    const graph = inferenceScene(base());
    assert.equal(graph.capabilities.length, CAPABILITIES.length);
    for (const item of graph.capabilities) {
        assert.ok(graph.nodes.some(node => node.id === item.stage));
        assert.match(item.source, /github.com\/llm-d\/llm-d\/blob\/[a-f0-9]{40}\//);
        assert.equal(item.unavailable, !item.available);
    }
    for (const edge of graph.edges) for (const id of [edge.from, edge.to]) assert.ok(graph.nodes.some(node => node.id === id));
    assert.equal(graph.edges.find(item => item.id === 'decode-loop').kind, 'iteration');
    assert.equal(graph.edges.find(item => item.id === 'kv-events').kind, 'state');
});
