import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeGuideSettings } from './guideSettings.js';

test('guide options validate values while source manifests determine variant support', () => {
    assert.throws(() => normalizeGuideSettings('optimized-baseline', '', { cacheCpuGiB: 20 }));
    assert.deepEqual(normalizeGuideSettings('pd-disaggregation', 'custom/deep', { rdmaNicCount: 2 }), { rdmaNicCount: 2 });
    assert.deepEqual(normalizeGuideSettings('tiered-prefix-cache', 'custom-cache', { cacheCpuGiB: 20 }), { cacheCpuGiB: 20 });
    assert.throws(() => normalizeGuideSettings('pd-disaggregation', 'vllm-rdma', { rdmaNicCount: 1.5 }));
    assert.deepEqual(normalizeGuideSettings('tiered-prefix-cache', 'native/cpu/base', { cacheCpuGiB: '20', routerValues: 'router: {}' }), { cacheCpuGiB: 20, routerValues: 'router: {}' });
    assert.deepEqual(normalizeGuideSettings('optimized-baseline', '', { cacheCpuGiB: '', rdmaNicCount: '' }), {});
});
