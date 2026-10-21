import assert from 'node:assert/strict';
import test from 'node:test';
import {
    actionLabel,
    availabilityTone,
    evidenceRows,
    sortIssues,
    severityTone,
} from './welcomePresentation.js';

test('severity and availability map to text-bearing tones', () => {
    assert.equal(severityTone('critical'), 'danger');
    assert.equal(severityTone('warning'), 'warning');
    assert.equal(severityTone('info'), 'info');
    assert.equal(availabilityTone('available'), 'success');
    assert.equal(availabilityTone('unavailable'), 'danger');
    assert.equal(availabilityTone('unknown'), 'neutral');
});

test('issue ordering puts critical before warning before info and keeps a stable tie-breaker', () => {
    const rows = sortIssues([
        { id: 'b', severity: 'warning', observedAt: '2026-10-18T10:00:00Z' },
        { id: 'a', severity: 'warning', observedAt: '2026-10-18T10:00:00Z' },
        { id: 'c', severity: 'critical', observedAt: '2026-10-18T09:00:00Z' },
        { id: 'd', severity: 'info', observedAt: '2026-10-18T11:00:00Z' },
    ]);
    assert.deepEqual(rows.map(row => row.id), ['c', 'a', 'b', 'd']);
});

test('evidence formatting preserves units and omits object payloads', () => {
    const rows = evidenceRows({
        evidence: {
            waitingCurrent: 4,
            kvCacheUsagePercent: 96.2,
            errorRate: 0.023,
            gateway: { state: 'missing' },
            unhealthyProviders: 'qwen-0-6b-a',
        },
    });
    assert.deepEqual(rows.map(row => row.label), ['Unhealthy providers', 'Waiting now', 'KV cache', 'Error rate']);
    assert.equal(rows[0].value, 'qwen-0-6b-a');
    assert.equal(rows[2].value, '96.2%');
    assert.equal(rows[3].value, '2.30%');
});

test('action labels stay concise', () => {
    assert.equal(actionLabel('inspect_bottleneck'), 'To check');
    assert.equal(actionLabel('create_storage'), 'To fix');
    assert.equal(actionLabel('open_storage'), 'To resolve');
    assert.equal(actionLabel('unknown'), 'To check');
});
