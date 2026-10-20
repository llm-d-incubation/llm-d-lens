import test from 'node:test';
import assert from 'node:assert/strict';
import { previewSourceComponent, generateSourceBundle } from './sourceCompositionBackend.js';
import { createBlueprint } from '../../features/inference-designer/blueprint.js';

test('source mutation transport sends JSON and maps composition validation accurately', async t => {
    const requests = [];
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        if (String(url).startsWith('/api/cluster/session?')) return Response.json({ sessionId: 'session-a' });
        assert.equal(new Headers(options.headers).get('content-type'), 'application/json');
        const body = JSON.parse(options.body); requests.push({ url, body });
        if (url.endsWith('source-preview')) return Response.json({ documents: [] });
        return Response.json({ commit: 'a'.repeat(40), files: [{ name: 'deployment.yaml', content: 'kind: List\nitems: []' }], validation: { clusterValidated: false, warnings: [], errors: [] } });
    });
    await previewSourceComponent('cluster-a', 'a'.repeat(40), { path: 'guides/a/base' });
    const result = await generateSourceBundle({ ...createBlueprint(), clusterId: 'cluster-a', sourceValidate: false, finalConfiguration: { baseChecksum: 'fixture', content: 'kind: Namespace' }, accelerator: 'cluster-accelerator', selectedGuides: ['addon-without-accelerator'], sourceCommit: 'a'.repeat(40), sourceUnits: [{ path: 'guides/a/base' }] });
    assert.equal(requests[1].body.validateCluster, false);
    assert.deepEqual(requests[1].body.finalConfiguration, { baseChecksum: 'fixture', content: 'kind: Namespace' });
    assert.equal(requests[1].body.guideSelection.accelerator, 'cluster-accelerator');
    assert.equal(requests[1].body.clusterId, 'cluster-a');
    assert.equal(result.plan.validation.clusterValidated, false);
    assert.equal(result.files[0].name, 'deployment.yaml');
});

test('deploy publishes generated YAML and targets its original cluster', async t => {
    const { deploySourceBundle } = await import('./sourceCompositionBackend.js');
    const yaml = (await import('js-yaml')).default;
    const requests = [];
    const docs = [{ apiVersion: 'apps/v1', kind: 'Deployment', metadata: { name: 'model', namespace: 'cache' } }, { apiVersion: 'v1', kind: 'Service', metadata: { name: 'router', namespace: 'cache' }, spec: { ports: [{ port: 80 }] } }];
    const generated = { sourceComposition: true, target: { clusterId: 'original', accelerator: 'test-profile' }, files: [
        { name: 'deployment.yaml', content: docs.map(doc => yaml.dump(doc)).join('---\n') },
        { name: 'provenance.json', content: JSON.stringify({ commit: 'a'.repeat(40), components: [{ path: 'guides/test' }], modelBinding: { model: 'Example/Model', namespace: 'cache', storageVolumeId: 'volume', pvcName: 'weights' } }) },
    ] };
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        if (String(url) === '/api/cluster/clusters') return Response.json({ items: [{ id: 'original', name: 'Original cluster', llmDRepoPath: '/source' }] });
        if (String(url).startsWith('/api/cluster/session?')) { assert.match(String(url), /clusterId=original/); return Response.json({ sessionId: 'original-session' }); }
        const body = JSON.parse(options.body); requests.push({ url, body });
        if (url.endsWith('/render')) return Response.json({ deployable_configuration: { provider_ref: 'source-composition', content: { officialGuide: { renderedManifest: body.render.rendered_manifest } } } });
        if (url.endsWith('/save')) return Response.json({ artifact: { deployable_configuration: body.deployable_configuration } });
        return Response.json({ id: 'managed-run' });
    });
    assert.equal((await deploySourceBundle(generated, 'router:80', 'My model')).id, 'managed-run');
    assert.deepEqual(requests.map(request => request.url), ['/api/v1/configurations/render', '/api/v1/configurations/save', '/api/v1/deployments/runs']);
    assert.equal(requests[2].body.provenance.cluster_server_id, 'original');
    assert.equal(requests[2].body.configurations[0].provider_ref, 'source-composition');
    assert.match(requests[2].body.configurations[0].content.officialGuide.renderedManifest, /name: router/);
});
