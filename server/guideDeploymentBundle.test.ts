import { seedHardwareProfiles } from './testing/hardwareProfiles.ts';
await seedHardwareProfiles();
import assert from 'node:assert/strict';
import test from 'node:test';
import yaml from 'js-yaml';
import { buildGuideDeploymentBundle } from './guideDeploymentBundle.ts';

test('P/D bundle adapts legacy plugins for Router 0.11 and retains original source layers', async () => {
    const legacy = { plugins: [
        { type: 'disagg-headers-handler' },
        { type: 'always-disagg-pd-decider', name: 'force-prefill' },
        { type: 'disagg-profile-handler', parameters: { deciderPluginName: 'force-prefill' } },
        { type: 'prefill-filter' },
    ], schedulingProfiles: [{ name: 'prefill', plugins: [{ pluginRef: 'prefill-filter' }] }] };
    const original = yaml.dump({ router: { epp: { pluginsConfigFile: 'pd.yaml', pluginsCustomConfig: { 'pd.yaml': yaml.dump(legacy) } } } });
    const build = (routerValues = '') => buildGuideDeploymentBundle({
        guide: 'pd-disaggregation', source: { commit: 'a'.repeat(40) }, model: 'Test/Model', routerValues,
        readSource: async path => path.includes('base.values') ? 'router: {}' : original,
        renderSource: async () => '',
    });
    const bundle = await build();
    assert.equal(bundle.helm.values[1].content, original);
    const effective = yaml.load(bundle.helm.values.at(-1)!.content);
    const config = yaml.load(effective.router.epp.pluginsCustomConfig['pd.yaml']);
    assert.deepEqual(config.plugins.map(p => p.type), ['always-disagg-pd-decider', 'disagg-profile-handler', 'prefill-filter']);
    assert.deepEqual(config.plugins[1].parameters, { deciders: { prefill: 'force-prefill' } });
    assert.deepEqual(config.schedulingProfiles, legacy.schedulingProfiles);
    const older = await build('router:\n  epp:\n    image:\n      tag: v0.10.0');
    const olderValues = yaml.load(older.helm.values.at(-1)!.content);
    assert.deepEqual(yaml.load(olderValues.router.epp.pluginsCustomConfig['pd.yaml']), legacy);
});

test('precise bundle pins auxiliary inputs and aligns tokenizer and index with model server', async () => {
    const source = { commit: 'a'.repeat(40), repository: 'llm-d/llm-d' };
    const values = yaml.dump({ router: { epp: { pluginsConfigFile: 'plugins.yaml', pluginsCustomConfig: { 'plugins.yaml': yaml.dump({ plugins: [
        { type: 'token-producer', parameters: { modelName: 'Old/Model' } },
        { type: 'precise-prefix-cache-producer', parameters: { tokenProcessorConfig: { blockSizeTokens: 64 } } },
    ] }) } } } });
    const bundle = await buildGuideDeploymentBundle({ guide: 'precise-prefix-cache-routing', source, model: 'New/Model', blockSize: 32, routerValues: 'router:\n  epp:\n    replicas: 1',
        readSource: async (path) => path.endsWith('.sh') ? 'echo calibration' : path.includes('base.values') ? 'router: {}' : values,
        renderSource: async () => 'apiVersion: v1\nkind: Service\nmetadata:\n  name: render\n',
    });
    assert.equal(bundle.sourceCommit, 'a'.repeat(40));
    assert.equal(bundle.resources.length, 2);
    assert.equal(yaml.load(bundle.resources[1].content).metadata.name, 'precise-prefix-cache-routing-baseline');
    assert.equal(bundle.calibration!.length, 2);
    assert.equal(bundle.calibration![0].content, 'echo calibration');
    assert.equal(bundle.calibration![1].name, 'calibration-peak-throughput.yaml');
    const effective = yaml.load(bundle.helm.values.at(-1)!.content);
    const plugins = yaml.load(effective.router.epp.pluginsCustomConfig['plugins.yaml']).plugins;
    assert.equal(plugins[0].parameters.modelName, 'New/Model');
    assert.equal(plugins[1].parameters.tokenProcessorConfig.blockSizeTokens, 32);
    assert.match(bundle.helm.values[0].checksum, /^sha256:[a-f0-9]{64}$/);
});

test('invalid router settings cannot be published as valid bundles', async () => {
    await assert.rejects(buildGuideDeploymentBundle({ guide: 'optimized-baseline', source: { commit: 'a'.repeat(40) }, model: 'New/Model', routerValues: '[1,2]', readSource: async () => 'router: {}', renderSource: async () => '' }), /mapping/);
});

test('router values are read from the single-host topology directory first, falling back to the flat legacy path', async () => {
    const requested: string[] = [];
    const bundle = await buildGuideDeploymentBundle({
        guide: 'tiered-prefix-cache', source: { commit: 'a'.repeat(40) }, model: 'New/Model',
        readSource: async (path) => {
            requested.push(path);
            if (path.includes('/single-host/')) throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
            return 'router: {}';
        },
        renderSource: async () => '',
    });
    assert.ok(requested.includes('guides/tiered-prefix-cache/router/single-host/tiered-prefix-cache-cpu.values.yaml'));
    assert.ok(requested.includes('guides/tiered-prefix-cache/router/tiered-prefix-cache-cpu.values.yaml'));
    assert.match(bundle.helm.values[0].checksum, /^sha256:[a-f0-9]{64}$/);
});

test('router values are read straight from the topology directory when it is present', async () => {
    const requested: string[] = [];
    await buildGuideDeploymentBundle({
        guide: 'tiered-prefix-cache', source: { commit: 'a'.repeat(40) }, model: 'New/Model',
        readSource: async (path) => { requested.push(path); return 'router: {}'; },
        renderSource: async () => '',
    });
    assert.deepEqual(requested, [
        'guides/recipes/router/base.values.yaml',
        'guides/tiered-prefix-cache/router/single-host/tiered-prefix-cache-cpu.values.yaml',
    ]);
});

test('router topology comes from the live registry, including plugin profiles', async () => {
    const { hardwareProfileSnapshot } = await import('./hardwareProfiles.ts');
    const profiles = hardwareProfileSnapshot();
    const plugin = { id: 'test-plugin', accelerator_keys: ['test-accelerator'], deployment: { router_topology: 'multi-host' } };
    profiles.push(plugin);
    try {
        const requested: string[] = [];
        await buildGuideDeploymentBundle({
            guide: 'tiered-prefix-cache', source: { commit: 'a'.repeat(40) }, model: 'New/Model', accelerator: 'test-accelerator',
            readSource: async (path) => { requested.push(path); return 'router: {}'; },
            renderSource: async () => '',
        });
        assert.ok(requested.includes('guides/tiered-prefix-cache/router/multi-host/tiered-prefix-cache-cpu.values.yaml'));
        assert.ok(!requested.some(path => path.includes('/single-host/')));
    } finally {
        profiles.splice(profiles.indexOf(plugin), 1);
    }
});
