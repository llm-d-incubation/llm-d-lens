import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceParameterLeaves, sourceParameterEdit } from './sourceParameters.js';
test('source fields retain numeric array indices and parameter types', () => {
    const leaves = sourceParameterLeaves([{ spec: { replicas: 2, enabled: false, args: ['--model', 'org/model'] } }]);
    const replica = leaves.find(x => x.key === '0.spec.replicas');
    assert.deepEqual(sourceParameterEdit(replica, '3'), { path: [0, 'spec', 'replicas'], value: 3 });
    assert.equal(sourceParameterEdit(leaves.find(x => x.key === '0.spec.enabled'), 'true').value, true);
    assert.throws(() => sourceParameterEdit(replica, 'NaN'), /finite/);
    assert.deepEqual(leaves.find(x => x.key === '0.spec.args.1').path, [0, 'spec', 'args', 1]);
});
