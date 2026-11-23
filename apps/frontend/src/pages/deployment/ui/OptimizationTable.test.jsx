import assert from 'node:assert/strict';
import test from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { OptimizationTable } from './OptimizationTable.jsx';
import { optimizationRows } from '../model/deploymentPlan.js';
import { OPTIMIZATIONS } from '@repo/shared/inference/optimizations';

const choice = (capability_id, extra = {}) => ({ capability_id, label: capability_id, checked: true, auto: false, intended: true, reason: 'Planned.', ...extra });
const plan = {
    validation: { status: 'resources_checked' },
    requirements: { forbidden_features: ['chunked_prefill'] },
    optimization_choices: [
        choice('prefix_routing', { label: 'Prefix-aware routing' }),
        choice('prefix_cache'),
        choice('chunked_prefill', { label: 'Chunked prefill', checked: false, intended: false, reason: 'Turned off by you.' }),
        choice('external_kv_cache', { label: 'KV cache offload' }),
        choice('pd_disaggregation', { auto: true, intended: false, reason: 'AIC predicts combined serving is faster.' }),
    ],
    decisions: [
        { capability_id: 'prefix_routing', status: 'enabled', reason: 'Established by final manifest.' },
        { capability_id: 'prefix_cache', status: 'enabled', reason: 'Established.' },
        { capability_id: 'external_kv_cache', status: 'unknown', reason: 'No host-memory budget.' },
        { capability_id: 'flow-control', label: 'Admission / fair queuing', status: 'enabled', reason: 'Established.' },
        { capability_id: 'tracing', label: 'Distributed tracing', status: 'unknown', reason: 'Not configured.' },
    ],
};

test('rows separate enabled, turned off, Auto off and degraded optimizations', () => {
    const rows = Object.fromEntries(optimizationRows(plan, { replicas: 2, tensor_parallel_size: 1, prefix_routing: true }).map(row => [row.id, row]));
    assert.equal(rows.prefix_routing.state, 'Enabled');
    assert.deepEqual(rows.prefix_routing.parameters, [['Scorer', 'Prefix affinity + load']]);
    assert.equal(rows.chunked_prefill.state, 'Off');
    assert.equal(rows.pd_disaggregation.state, 'Auto: off');
    assert.equal(rows.external_kv_cache.state, 'Degraded');
    assert.equal(rows.external_kv_cache.reason, 'No host-memory budget.');
    assert.equal(rows['flow-control'].selectable, false);
    assert.equal(rows.tracing, undefined);
});

test('unchecked plans report pending checks and Agentic reports unselected optimizations neutrally', () => {
    const pending = optimizationRows({ ...plan, validation: { status: 'planning_checked' } }, null);
    assert.equal(pending.find(row => row.id === 'external_kv_cache').state, 'Checks pending');
    const agentic = optimizationRows({ ...plan, decisions: [] }, null, 'agentic');
    assert.equal(agentic.find(row => row.id === 'pd_disaggregation').state, 'Not selected');
});

test('legacy plans without choices fall back to forbidden features', () => {
    const rows = optimizationRows({ requirements: { forbidden_features: ['pd_disaggregation'] }, decisions: [] }, null);
    assert.equal(rows.length, 5);
    assert.equal(rows.find(row => row.id === 'pd_disaggregation').state, 'Off');
});

test('the table lists every optimization with a checkbox and explains the ones not applied inline', () => {
    const html = renderToStaticMarkup(<OptimizationTable plan={plan} candidate={{ replicas: 1, tensor_parallel_size: 1 }} onOpen={() => {}} onToggle={async () => true} />);
    const table = html.slice(html.indexOf('<table'), html.indexOf('</table>'));
    assert.match(table, /aria-label="Disable Prefix-aware routing"[^>]*checked=""|checked=""[^>]*aria-label="Disable Prefix-aware routing"/);
    assert.match(table, /aria-label="Enable Chunked prefill"/);
    assert.match(table, /aria-label="Adjust Prefix-aware routing"/);
    assert.match(table, /Admission \/ fair queuing/);
    assert.match(table, /aria-label="Disable Admission \/ fair queuing"/);
    assert.match(html, /3 of 6 optimizations enabled/);
    assert.match(table, /KV cache offload[\s\S]*Not applied[\s\S]*No host-memory budget\./);
    assert.match(table, /Not needed[\s\S]*AIC predicts combined serving is faster\./);
    assert.doesNotMatch(table, />Status<|>Why<|Turn on/);
    assert.doesNotMatch(html, /is not used in this plan|role="dialog"/);
});

test('the compact corner list shows only optimization names and settings', () => {
    const html = renderToStaticMarkup(<OptimizationTable compact plan={plan} candidate={null} onOpen={() => {}} onToggle={async () => true} onExpand={() => {}} />);
    assert.match(html, /aria-label="Adjust Prefix-aware routing"/);
    assert.match(html, /aria-label="Adjust Chunked prefill"/);
    assert.match(html, /Show optimizations in the center/);
    assert.doesNotMatch(html, /<table|type="checkbox"|Parameters/);
});

test('recipe-controlled capabilities expose settings instead of enabled no-op checkboxes', () => {
    const recipes = OPTIMIZATIONS.filter(option => option.selection === 'recipe');
    assert.ok(recipes.length > 0);
    const recipePlan = { ...plan, decisions: recipes.map(option => ({ capability_id: option.id, label: option.label, status: 'enabled' })) };
    const rows = optimizationRows(recipePlan, null).filter(row => recipes.some(option => option.id === row.id));
    assert.ok(rows.every(row => row.toggleable === false));
    const html = renderToStaticMarkup(<OptimizationTable plan={recipePlan} onOpen={() => {}} onToggle={async () => true} />);
    const inputs = html.match(/<input[^>]*title="Controlled by source settings[^>]*>/g) || [];
    assert.equal(inputs.length, recipes.length);
    assert.ok(inputs.every(input => input.includes('disabled=""')));
});
