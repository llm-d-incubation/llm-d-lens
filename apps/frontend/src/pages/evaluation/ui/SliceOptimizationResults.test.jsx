import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import SliceOptimizationResults from './SliceOptimizationResults.jsx';

const row = (slices, maximumStableQps) => ({
    caseId: `c${slices}`, status: 'succeeded', artifactId: `a${slices}`, slices, accelerators: slices * 5,
    maximumStableQps, throughputTps: slices * 100,
});
const group = {
    key: 'g', model: 'm', guide: 'pd-disaggregation', shape: [1, 2, 1, 3], placementPolicy: { type: 'ExclusiveSlice', topology: 'zone' },
    sliceRouting: 'prefer', slaTargets: { ttft_ms: 500 }, rows: [row(1, 5), row(2, 10)],
};

test('Slice optimization results compare cost and SLO-stable rate without picking by throughput', () => {
    const html = renderToStaticMarkup(createElement(SliceOptimizationResults, { groups: [group], canDeploy: true, onUseConfiguration() {} }));
    assert.match(html, /Slice Optimization Results/);
    assert.match(html, /per Slice 1P×TP2 \/ 1D×TP3 · ExclusiveSlice · zone · EPP prefer same Slice/);
    assert.match(html, /SLO: TTFT ≤ 500 ms/);
    assert.match(html, /Enter a target load to choose the lowest-cost Slice count/);
    assert.doesNotMatch(html, /Use Recommended Configuration/);
    assert.match(html, />10<\/td><td class="p-2">10<\/td><td class="p-2">1</);
    const unmeasured = renderToStaticMarkup(createElement(SliceOptimizationResults, { groups: [{ ...group, rows: [row(1, null), row(2, null)] }] }));
    assert.match(unmeasured, /No SLO-stable rate was recorded/);
});
