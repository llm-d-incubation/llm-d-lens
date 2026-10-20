import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { guideSourceInventory, editSourceDocuments, composeSourceDocuments, generateSourceComposition } from '../../src/services/guideComposition.ts';

const deployment = (replicas = 1) => ({ apiVersion: 'apps/v1', kind: 'Deployment', metadata: { name: 'engine' }, spec: { replicas } });
test('composition accepts identical resources but rejects conflicting definitions', () => {
    const a = { name: 'guide-a', documents: [deployment()] };
    assert.equal(composeSourceDocuments([a, a], 'serving').documents.length, 1);
    assert.throws(() => composeSourceDocuments([a, { name: 'guide-b', documents: [deployment(2)] }], 'serving'), /Resource conflict/);
    assert.throws(() => composeSourceDocuments([a], '../outside'), /namespace/);
});
test('parameter changes preserve types, refuse stale paths and block prototype mutation', () => {
    const original = [deployment()];
    assert.equal(editSourceDocuments(original, [{ path: [0, 'spec', 'replicas'], value: 3 }])[0].spec.replicas, 3);
    assert.equal(original[0].spec.replicas, 1);
    assert.throws(() => editSourceDocuments(original, [{ path: [0, 'spec', 'replicas'], value: '3' }]), /type/);
    assert.throws(() => editSourceDocuments(original, [{ path: [0, 'spec', 'unknown'], value: 3 }]), /no longer exists/);
    assert.throws(() => editSourceDocuments(original, [{ path: [0, '__proto__', 'injected'], value: true }]), /Invalid parameter/);
});
test('composition preserves explicit namespaces and rejects incomplete templates', () => {
    const ns = { apiVersion: 'v1', kind: 'Namespace', metadata: { name: 'custom' } };
    const output = composeSourceDocuments([{ name: 'source', documents: [ns, deployment()] }], 'serving');
    assert.equal(output.documents[0].metadata.namespace, undefined);
    assert.equal(output.documents[1].metadata.namespace, 'serving');
    assert.throws(() => composeSourceDocuments([{ name: 'source', documents: [{ ...deployment(), spec: { model: '${MODEL}' } }] }], 'serving'), /unresolved/);
    assert.throws(() => composeSourceDocuments([{ name: 'patch', documents: [{ spec: { replicas: 2 } }] }], 'serving'), /complete resource/);
});
test('source inventory includes nested Guides and renders selected assets at a pinned commit', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'guide-composition-test-'));
    try {
        await fs.mkdir(path.join(root, 'guides/scaling/queue'), { recursive: true });
        await fs.writeFile(path.join(root, 'guides/scaling/README.md'), '# Scaling\n');
        await fs.writeFile(path.join(root, 'guides/scaling/queue/guide.yaml'), 'name: queue\nenv:\n  static:\n    THRESHOLD: {default: 8}\n');
        await fs.writeFile(path.join(root, 'guides/scaling/queue/resource.yaml'), JSON.stringify(deployment()));
        const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args], { stdio: 'pipe' });
        git('init'); git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Fixture');
        const catalog = await guideSourceInventory(root);
        assert.deepEqual(catalog.guides.map(g => g.id).sort(), ['scaling', 'scaling/queue']);
        assert.equal(catalog.guides.find(g => g.id === 'scaling/queue')?.variables.THRESHOLD.default, 8);
        const request = { commit: catalog.commit, namespace: 'serving', units: [{ path: 'guides/scaling/queue/resource.yaml', edits: [{ path: [0, 'spec', 'replicas'], value: 4 }] }] };
        const result = await generateSourceComposition(root, request);
        assert.match(result.files[0].content, /replicas: 4/);
        assert.equal(result.validation.clusterValidated, false);
        assert.match(result.compositionChecksum, /^[a-f0-9]{64}$/);
        const finalConfiguration = { baseChecksum: result.compositionChecksum, content: result.files[0].content.replace('replicas: 4', 'replicas: 7') };
        const edited = await generateSourceComposition(root, { ...request, finalConfiguration });
        assert.match(edited.files[0].content, /replicas: 7/);
        assert.equal(JSON.parse(edited.files[1].content).finalConfiguration.baseChecksum, result.compositionChecksum);
        await assert.rejects(generateSourceComposition(root, { ...request, finalConfiguration: { ...finalConfiguration, baseChecksum: 'stale' } }), /deployment changed/);
        await assert.rejects(generateSourceComposition(root, { ...request, finalConfiguration: { ...finalConfiguration, content: 'spec: {}' } }), /complete resource/);
        await assert.rejects(generateSourceComposition(root, { ...request, finalConfiguration: { ...finalConfiguration, content: '' } }), /nonempty/);

        const replacementContent = JSON.stringify({ ...deployment(2), spec: { replicas: 2, template: { spec: { containers: [{ name: 'engine', args: ['--enable-auto-tool-choice', '--tool-call-parser', 'custom-parser'] }] } } } });
        const structural = await generateSourceComposition(root, { ...request, units: [{ path: request.units[0].path, replacementContent, edits: [] }] });
        assert.match(structural.files[0].content, /enable-auto-tool-choice/);
        assert.match(structural.files[0].content, /custom-parser/);
        await assert.rejects(generateSourceComposition(root, { ...request, units: [{ path: request.units[0].path, replacementContent: 'spec: {}', edits: [] }] }), /named Kubernetes resources/);

        await assert.rejects(generateSourceComposition(root, { ...request, commit: '0'.repeat(40) }), /commit changed/);
        await assert.rejects(generateSourceComposition(root, { ...request, units: [{ path: '../../etc/passwd' }] }), /Select a Kubernetes/);
        await fs.appendFile(path.join(root, 'guides/scaling/queue/resource.yaml'), '\n# Changed\n');
        await assert.rejects(guideSourceInventory(root), /local modifications/);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
});


test('runtime shell variables survive while resource template fields require resolution', () => {
    const doc = { ...deployment(), spec: { template: { spec: { containers: [{ name: 'engine', command: ['bash', '-c'], args: ['exec vllm serve model --data-parallel-address ${LWS_LEADER_ADDRESS}'] }] } } } };
    assert.equal(composeSourceDocuments([{ name: 'wide-ep', documents: [doc] }], 'serving').documents.length, 1);
    const unresolved = { ...deployment(), spec: { replicas: '${DECODE_REPLICAS}' } };
    assert.throws(() => composeSourceDocuments([{ name: 'recipe', documents: [unresolved] }], 'serving'), /unresolved/);
});

test('server recomputes required preset resources instead of trusting client component removal', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'guide-plan-server-'));
    try {
        await fs.mkdir(path.join(root, 'guides/optimized-baseline/modelserver'), { recursive: true });
        await fs.writeFile(path.join(root, 'guides/optimized-baseline/guide.yaml'), 'name: optimized-baseline\ndeploy:\n  modelserver:\n    - run: kubectl apply -f ${REPO_ROOT}/guides/optimized-baseline/modelserver/workload.yaml\n');
        await fs.writeFile(path.join(root, 'guides/optimized-baseline/modelserver/workload.yaml'), JSON.stringify(deployment()));
        const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args], { stdio: 'pipe' });
        git('init'); git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Fixture');
        const catalog = await guideSourceInventory(root);
        const result = await generateSourceComposition(root, { commit: catalog.commit, namespace: 'serving', guideSelection: { guides: ['optimized-baseline'] }, units: [] });
        assert.match(result.files[0].content, /name: engine/);
        const provenance = JSON.parse(result.files[1].content);
        assert.deepEqual(provenance.guidePlan.guides, ['optimized-baseline']);
        assert.equal(provenance.components.length, 1);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('independent capability intent reaches generated YAML and provenance', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'capability-render-test-'));
    try {
        await fs.mkdir(path.join(root, 'guides/optimized-baseline/modelserver'), { recursive: true });
        await fs.writeFile(path.join(root, 'guides/README.md'), '## Routing\n* [Optimized Baseline](./optimized-baseline/README.md)\n');
        await fs.writeFile(path.join(root, 'guides/optimized-baseline/README.md'), '# Optimized Baseline\n');
        await fs.writeFile(path.join(root, 'guides/optimized-baseline/guide.yaml'), 'name: optimized-baseline\ndeploy:\n  worker:\n    run: kubectl apply -f SOURCE/guides/optimized-baseline/modelserver/worker.yaml\n');
        await fs.writeFile(path.join(root, 'guides/optimized-baseline/modelserver/worker.yaml'), JSON.stringify({ ...deployment(), spec: { template: { spec: { containers: [{ name: 'vllm', image: 'example.test/vllm:pinned', args: ['--enable-prefix-caching', '--max-num-seqs=32'] }] } } } }));
        const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args], { stdio: 'pipe' });
        git('init'); git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Fixture');
        const catalog = await guideSourceInventory(root);
        assert.equal(catalog.officialGuides[0].title, 'Optimized Baseline');
        const result = await generateSourceComposition(root, { commit: catalog.commit, namespace: 'serving', guideSelection: { guides: [], capabilities: { prefix: false, chunked: true } } });
        assert.match(result.files[0].content, /--no-enable-prefix-caching/);
        assert.match(result.files[0].content, /--enable-chunked-prefill/);
        assert.match(result.files[0].content, /--max-num-seqs=32/);
        const provenance = JSON.parse(result.files[1].content);
        assert.equal(provenance.guidePlan.capabilityOverrides.prefix, false);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
});
