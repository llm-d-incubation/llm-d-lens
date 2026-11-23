import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { sourceParameterLeaves, sourceParameterEdit, rebaseFinalConfiguration } from './sourceParameters.js';
test('source fields retain numeric array indices and parameter types', () => {
    const leaves = sourceParameterLeaves([{ spec: { replicas: 2, enabled: false, args: ['--model', 'org/model'] } }]);
    const replica = leaves.find(x => x.key === '0.spec.replicas');
    assert.deepEqual(sourceParameterEdit(replica, '3'), { path: [0, 'spec', 'replicas'], value: 3 });
    assert.equal(sourceParameterEdit(leaves.find(x => x.key === '0.spec.enabled'), 'true').value, true);
    assert.throws(() => sourceParameterEdit(replica, 'NaN'), /finite/);
    assert.deepEqual(leaves.find(x => x.key === '0.spec.args.1').path, [0, 'spec', 'args', 1]);
});

const manifest = (replicas = 1, args = ['--max-model-len', '4096']) => yaml.dump({
    apiVersion: 'apps/v1', kind: 'Deployment', metadata: { name: 'model', namespace: 'models' },
    spec: { replicas, template: { spec: { containers: [{ name: 'modelserver', args }] } } },
});
const result = (content, compositionChecksum = 'new') => ({ compositionChecksum, files: [{ name: 'deployment.yaml', content }] });

test('optimization changes retain edited replicas and arguments when flag positions shift', () => {
    const saved = { baseChecksum: 'old', baseContent: manifest(), content: manifest(2, ['--max-model-len', '8192']) };
    const next = result(manifest(1, ['--enable-prefix-caching', '--max-model-len', '4096', '--enable-chunked-prefill']));
    const rebased = rebaseFinalConfiguration(saved, next);
    const doc = yaml.load(rebased.content);
    assert.equal(doc.spec.replicas, 2);
    assert.deepEqual(doc.spec.template.spec.containers[0].args, ['--enable-prefix-caching', '--max-model-len', '8192', '--enable-chunked-prefill']);
    assert.equal(rebased.baseChecksum, 'new');
    assert.equal(rebased.baseContent, next.files[0].content);
    assert.equal(saved.content, manifest(2, ['--max-model-len', '8192']));
});

test('unrelated resource additions and order changes preserve named container edits', () => {
    const base = yaml.load(manifest());
    const edited = structuredClone(base);
    edited.spec.template.spec.containers[0].args = ['--max-model-len', '8192'];
    const next = structuredClone(base);
    next.spec.template.spec.containers.unshift({ name: 'sidecar', args: ['--port=9090'] });
    const service = { apiVersion: 'v1', kind: 'Service', metadata: { name: 'inference' }, spec: { ports: [{ port: 8000 }] } };
    const saved = { baseChecksum: 'old', baseContent: yaml.dump(base), content: yaml.dump(edited) };
    const rebased = rebaseFinalConfiguration(saved, result(yaml.dump(service) + '---\n' + yaml.dump(next)));
    const docs = yaml.loadAll(rebased.content);
    assert.equal(docs[0].kind, 'Service');
    assert.equal(docs[1].spec.template.spec.containers[0].name, 'sidecar');
    assert.deepEqual(docs[1].spec.template.spec.containers[1].args, ['--max-model-len', '8192']);
});

test('conflicting parameter or topology edits are rejected, not silently discarded', () => {
    const saved = { baseChecksum: 'old', baseContent: manifest(), content: manifest(2, ['--max-model-len', '8192']) };
    assert.throws(() => rebaseFinalConfiguration(saved, result(manifest(3))), /conflicts.*replicas/);
    assert.throws(() => rebaseFinalConfiguration(saved, result(manifest(1, ['--max-model-len', '16384']))), /conflicts.*--max-model-len/);
    assert.throws(() => rebaseFinalConfiguration(saved, result(manifest(1, ['--enable-prefix-caching']))), /conflicts.*--max-model-len/);
    assert.throws(() => rebaseFinalConfiguration(saved, result('apiVersion: v1\nkind: Service\nmetadata:\n  name: changed\n')), /conflicts/);
});

test('legacy saved edits require an explicit resolution only when the base changed', () => {
    const saved = { baseChecksum: 'old', content: manifest(2) };
    assert.equal(rebaseFinalConfiguration(saved, result(manifest(), 'old')).content, saved.content);
    assert.throws(() => rebaseFinalConfiguration(saved, result(manifest())), /no edit baseline/);
    assert.throws(() => rebaseFinalConfiguration(null, { files: [] }), /missing deployment.yaml/);
});

test('successive rebases retain edits without reintroducing a disabled optimization', () => {
    const base = result(manifest(1, ['--enable-prefix-caching', '--max-model-len', '4096']), 'first');
    const saved = { ...rebaseFinalConfiguration(null, base), content: manifest(2, ['--enable-prefix-caching', '--max-model-len', '8192']) };
    const disabled = rebaseFinalConfiguration(saved, result(manifest(), 'second'));
    const next = rebaseFinalConfiguration(disabled, result(manifest(1, ['--max-model-len', '4096', '--enable-chunked-prefill']), 'third'));
    const doc = yaml.load(next.content);
    assert.equal(doc.spec.replicas, 2);
    assert.deepEqual(doc.spec.template.spec.containers[0].args, ['--max-model-len', '8192', '--enable-chunked-prefill']);
});
