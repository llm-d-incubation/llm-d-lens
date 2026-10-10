import assert from 'node:assert/strict';
import test from 'node:test';
import { catalogFromPaths, selectManifestPaths } from './guidePlanning.ts';
import { profiles, seedHardwareProfiles } from './testing/hardwareProfiles.ts';
await seedHardwareProfiles();

test('catalog discovers complete entry points and excludes intermediate directories and patches', () => {
    const paths = ['guides/tiered-prefix-cache/modelserver/gpu/vllm/native/cpu/base/kustomization.yaml', 'guides/tiered-prefix-cache/modelserver/gpu/vllm/native/fs/base/kustomization.yaml', 'guides/tiered-prefix-cache/modelserver/gpu/vllm/patch-only/patch.yaml'];
    const catalog = catalogFromPaths(paths, profiles);
    assert.deepEqual(catalog[0].accelerators[0].modelServers[0].variants, ['native/cpu/base', 'native/fs/base']);
    assert.equal(selectManifestPaths(paths, 'tiered-prefix-cache', 'gpu', 'vllm', 'native/fs/base').guidePath, 'guides/tiered-prefix-cache/modelserver/gpu/vllm/native/fs/base');
    assert.throws(() => selectManifestPaths(paths, 'tiered-prefix-cache', 'gpu', 'vllm', 'native'), /Kustomization/);
});

test('new hardware can declare nested identity and a different overlay root', () => {
    const plugin = { id: 'example', upstream_variant: 'device/v2', deployment: { overlay_root: 'guides/{guide}/engines/{variant}/{model_server}' } };
    const catalog = catalogFromPaths(['guides/new/engines/device/v2/engine/custom/kustomization.yaml'], [plugin]);
    assert.equal(catalog[0].id, 'new');
    assert.equal(catalog[0].accelerators[0].id, 'device/v2');
    assert.deepEqual(catalog[0].accelerators[0].modelServers, [{ id: 'engine', variants: ['custom'] }]);
});

test('sibling runtimes support nested source variants without a name whitelist', () => {
    const paths = ['guides/pd-disaggregation/modelserver/gpu/vllm/base/kustomization.yaml', 'guides/pd-disaggregation/modelserver/gpu/vllm-special/custom/deep/kustomization.yaml'];
    const result = selectManifestPaths(paths, 'pd-disaggregation', 'gpu', 'vllm', 'vllm-special/custom/deep');
    assert.equal(result.guidePath, 'guides/pd-disaggregation/modelserver/gpu/vllm-special/custom/deep');
    assert.equal(result.modelServer, 'vllm-special');
});

test('cache and NIC controls follow rendered resource content, not directory names', async () => {
    const { guideSettingsCapabilities, configureCpuCache } = await import('./modelServerConfiguration.ts');
    const container = { name: 'modelserver', args: ['Model', '--kv-transfer-config', JSON.stringify({kv_connector: 'OffloadingConnector', kv_connector_extra_config: {cpu_bytes_to_use: 1}})] };
    const deployment = {kind: 'Deployment', spec: {template: {spec: {containers: [container]}}}};
    const claim = {kind: 'ResourceClaimTemplate', spec: {spec: {devices: {requests: [{exactly: {deviceClassName: 'network.example'}}]}}}};
    assert.deepEqual(guideSettingsCapabilities([deployment, claim], 'network.example'), {cacheCpuGiB: true, rdmaNicCount: true});
    configureCpuCache(container, 2);
    assert.ok(container.args.join(' ').includes('2147483648'));
    assert.deepEqual(guideSettingsCapabilities([{kind: 'Deployment', spec: {template: {spec: {containers: [{name: 'modelserver', args: ['Model']}]}}}}], 'network.example'), {cacheCpuGiB: false, rdmaNicCount: false});
});
