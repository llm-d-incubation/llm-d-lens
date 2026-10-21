import assert from 'node:assert/strict';
import test from 'node:test';
import { canAccessView } from '../../pages/auth/permissions.js';
import { firstAccessibleView, NAVIGATION_GROUPS, resolveView } from './views.js';

test('landing page follows the first accessible sidebar entry, including custom grants', () => {
  for (const [permissions, expected] of [
    [['*:*:*'], 'welcome'],
    [['deployment:run:read', 'cluster:cluster:read'], 'welcome'],
    [['model-service:group:read', 'model-service:usage:read'], 'admin/model-service'],
    [['evaluate:run:read', 'model-service:usage:read'], 'optimization-evaluate'],
    [['model-service:usage:read', 'model-service:token:manage', 'model-service:inference:use'], 'usage'],
    [['model-service:token:manage'], 'api-keys'],
    [['cluster:cluster:read', 'user:user:read'], 'welcome'],
    [['user:user:read'], 'admin/users'],
    [[], ''],
  ]) {
    assert.equal(firstAccessibleView(view => canAccessView(permissions, view)), expected);
  }
});

test('every sidebar entry can be the first accessible destination without a separate priority list', () => {
  for (const item of NAVIGATION_GROUPS.flatMap(group => group.items)) {
    assert.equal(firstAccessibleView(view => view === item.view), item.view);
    assert.equal(resolveView(item.view), item.view);
  }
});

test('default URLs use the accessible landing page but supported deep links remain intact', () => {
  const fallback = firstAccessibleView(view => canAccessView(['model-service:usage:read'], view));
  for (const view of [null, '', 'unknown']) assert.equal(resolveView(view, fallback), 'usage');
  assert.equal(resolveView('admin/model-service', fallback), 'admin/model-service');
  assert.equal(resolveView(null, ''), '');
});

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
  assert.equal(resolveView('dashboard'), 'welcome');
  assert.equal(resolveView('dashboard/alerts'), 'welcome/issues');
  assert.equal(resolveView('simulation'), 'optimization-simulate');
  assert.equal(resolveView('unknown'), 'model-market');
});