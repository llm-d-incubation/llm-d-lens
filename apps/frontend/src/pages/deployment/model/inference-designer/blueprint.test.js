import test from 'node:test';
import assert from 'node:assert/strict';
import { blueprintClusterProfiles } from './blueprint.js';
const profile = { id: 'test-profile', upstream_variant: 'test-accelerator', deployment: { runtime_image: 'example.test/engine:1' } };

test('cluster hardware uses summary profiles, retains unknown identities and falls back to positive node counts', () => {
    const overview = { kubernetes: { hardware: { accelerators: [{ id: profile.id, gpuCount: 2 }] }, nodes: [{ gpuByProfile: { stale: 4 } }] } };
    assert.deepEqual(blueprintClusterProfiles(overview, [profile]), [profile]);
    assert.deepEqual(blueprintClusterProfiles(null, [profile]), []);
    assert.deepEqual(blueprintClusterProfiles({ kubernetes: { nodes: [{ gpuByProfile: { [profile.id]: 2, absent: 0 } }] } }, [profile]), [profile]);
    overview.kubernetes.hardware.accelerators.push({ id: 'unknown', gpuCount: 1 });
    assert.deepEqual(blueprintClusterProfiles(overview, [profile]).map(item => item.id), [profile.id, 'unknown']);
    assert.equal(blueprintClusterProfiles(overview, [profile])[1].upstream_variant, undefined);
});
