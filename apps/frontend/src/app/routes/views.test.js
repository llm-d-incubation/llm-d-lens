import assert from 'node:assert/strict';
import test from 'node:test';
import { canAccessView } from '../../pages/auth/permissions.js';
import { resolveView } from './views.js';

test('legacy configuration URLs resolve to the shared evaluation wizard', () => {
  assert.equal(resolveView('optimization-explore'), 'optimization-evaluate-new');
  assert.equal(resolveView('optimization-evaluate-new'), 'optimization-evaluate-new');
});

test('legacy configuration URLs use the destination permissions', () => {
  const view = resolveView('optimization-explore');
  assert.equal(canAccessView(['configuration:artifact:read'], view), false);
  assert.equal(canAccessView(['deployment:run:create'], view), true);
  assert.equal(canAccessView(['evaluate:workflow:create'], view), true);
  assert.equal(canAccessView(['evaluate:run:create'], view), true);
});

test('other optimization views and existing aliases remain available', () => {
  for (const view of ['optimization-deploy', 'optimization-workspace', 'opt-define', 'opt-search', 'opt-deploy', 'opt-benchmark', 'opt-performance']) {
    assert.equal(resolveView(view), view);
  }
  assert.equal(resolveView('home'), 'clusters');
  assert.equal(resolveView('simulation'), 'optimization-simulate');
  assert.equal(resolveView('unknown'), 'model-market');
});