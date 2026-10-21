import assert from 'node:assert/strict';
import test from 'node:test';
import { visibleWelcomeActions } from './quickActions.js';

test('Welcome quick actions require both destination access and the action permission', () => {
    const all = visibleWelcomeActions(() => true, () => true);
    assert.deepEqual(all.map((action) => action.id), [
        'token-usage-report',
        'new-deployment',
        'add-model-service',
        'launch-benchmark',
        'replay-traces',
        'manage-storages',
        'cache-model',
        'create-cluster',
    ]);

    const readOnly = visibleWelcomeActions(() => false, () => true);
    assert.deepEqual(readOnly, []);

    const noModelServiceManage = visibleWelcomeActions(
        (permission) => permission !== 'model-service:group:manage',
        () => true,
    );
    assert.deepEqual(noModelServiceManage.map((action) => action.id), [
        'token-usage-report',
        'new-deployment',
        'launch-benchmark',
        'replay-traces',
        'manage-storages',
        'cache-model',
        'create-cluster',
    ]);
});
