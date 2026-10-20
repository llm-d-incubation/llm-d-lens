import test from 'node:test';
import assert from 'node:assert/strict';
import { generateBlueprint } from './blueprintBackend.js';
import { createBlueprint } from '../../features/inference-designer/blueprint.js';
const profile = { id: 'test', upstream_variant: 'test-accelerator', deployment: { runtime_image: 'example.test/engine:1' } };
const providers = [{ id: 'optimized-baseline', supported: true, accelerators: ['test-accelerator'] }];
const draft = { ...createBlueprint(), clusterId: 'target', profileId: 'test', model: 'org/model', maxModelLen: '4096' };
const commit = 'a'.repeat(40);
const plan = { source: { commit, files: ['guides/source.yaml'] }, validation: { errors: [] }, deployment: {}, plannedDeployment: { content: 'kind: Deployment' }, deploymentBundle: { sourceCommit: commit, helm: { values: [] } } };

test('generation calls both existing validators and never publishes or deploys', async () => {
    const calls = [];
    const original = globalThis.fetch;
    globalThis.fetch = async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        calls.push({ url, body });
        const payload = url.startsWith('/api/cluster/session') ? { sessionId: 'session', cluster: { name: 'Target' } }
            : url === '/api/guide-planning/plan' ? plan : { deployable_configuration: { content: {} } };
        return new Response(JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } });
    };
    try {
        const result = await generateBlueprint(draft, providers, profile);
        assert.equal(calls.length, 3);
        assert.equal(calls[1].body.clusterId, 'target');
        assert.equal(calls[1].body.maxModelLen, 4096);
        assert.equal(calls[2].url, '/api/v1/configurations/render');
        assert.equal(calls[2].body.render.guide_source.commit, commit);
        assert.equal(calls[2].body.candidate_config.custom_parameters[0].value, '4096');
        assert.ok(result.configuration);
    } finally { globalThis.fetch = original; }
});
test('a rejected source plan does not reach render or produce downloadable output', async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => new Response(JSON.stringify(++calls === 1 ? { sessionId: 'session' } : { validation: { errors: ['Unsupported source topology'] } }));
    try {
        await assert.rejects(generateBlueprint(draft, providers, profile), /Unsupported source topology/);
        assert.equal(calls, 2);
    } finally { globalThis.fetch = original; }
});
test('a changed source commit rejects a saved router snapshot before configuration rendering', async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => new Response(JSON.stringify(++calls === 1 ? { sessionId: 'session' } : plan));
    try {
        await assert.rejects(generateBlueprint({ ...draft, routerSourceCommit: 'b'.repeat(40), routerSourceKey: 'optimized-baseline::test' }, providers, profile), /source changed/);
        assert.equal(calls, 2);
    } finally { globalThis.fetch = original; }
});
