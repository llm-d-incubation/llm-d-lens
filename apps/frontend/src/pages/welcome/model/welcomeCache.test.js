import assert from 'node:assert/strict';
import test from 'node:test';
import {
    clearWelcomeOverviewCache,
    readWelcomeOverview,
    welcomeCacheKey,
    writeWelcomeOverview,
} from './welcomeCache.js';

test('Welcome overview cache is scoped by user, authz version and cluster', () => {
    clearWelcomeOverviewCache();
    const key = welcomeCacheKey({ userId: 'u1', authzVersion: 2, clusterId: 'a' });
    assert.notEqual(key, welcomeCacheKey({ userId: 'u2', authzVersion: 2, clusterId: 'a' }));
    assert.notEqual(key, welcomeCacheKey({ userId: 'u1', authzVersion: 3, clusterId: 'a' }));
    assert.notEqual(key, welcomeCacheKey({ userId: 'u1', authzVersion: 2, clusterId: 'b' }));

    writeWelcomeOverview(key, { issues: { data: [] } });
    assert.deepEqual(readWelcomeOverview(key), { issues: { data: [] } });
    assert.equal(readWelcomeOverview(welcomeCacheKey({ userId: 'u2', authzVersion: 2, clusterId: 'a' })), null);
});
