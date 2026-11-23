import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { InferenceFlowMap2D } from './InferenceFlowMap2D';
import { DeploymentPlanGraph } from '../DeploymentPlanGraph';
import { inferenceScene, servingFlowDiagram } from '../../model/inference-designer/sceneGraph';
import { createBlueprint } from '../../model/inference-designer/blueprint';

const graph = inferenceScene(createBlueprint(), 'Fixture cluster');
const render = props => renderToStaticMarkup(<InferenceFlowMap2D graph={graph} canConfigure={() => true} onSelect={() => {}} onConfigure={() => {}} onActivate={() => {}} {...props} />);

test('overview exposes every functional group without a second settings table or global visibility gate', () => {
    const html = render({});
    assert.equal([...html.matchAll(/aria-label="Expand [^"]+ settings"/g)].length, 9);
    assert.match(html, /Expand KV cache index settings/);
    assert.match(html, /Expand Service &amp; platform settings/);
    assert.match(html, /Expand Compute configuration settings/);
    assert.match(html, /Model server optimization relationships/);
    assert.doesNotMatch(html, /All settings|Stage configuration|deployment-stage-settings|<canvas/);
});

test('the displayed enabled count can use the deployment plan decision count', () => {
    const html = render({ configuredCount: 6 });
    assert.match(html, />6 enabled</);
});

test('P/D boundaries and shared relations are explicit while settings stay collapsed', () => {
    const html = render({ graph: inferenceScene({ ...createBlueprint(), pd: true }) });
    assert.match(html, /Prefill model server/);
    assert.match(html, /Decode model server/);
    assert.match(html, /Prefill: KV transfer → Decode/);
    assert.match(html, /Compute configuration: configures → Decode/);
    assert.doesNotMatch(html, /aria-label="Configure /);
});

test('expanded group offers direct configuration entry and preserves unavailable statuses', () => {
    const plan = servingFlowDiagram(graph, { prefix: 'Hardware prerequisites are unknown.' }, () => true, { expandedGroup: 'prefill' });
    const html = renderToStaticMarkup(<DeploymentPlanGraph plan={plan} onSelect={() => {}} />);
    assert.match(html, /aria-label="Collapse Prefill settings"/);
    assert.match(html, /aria-label="Configure Prefix caching"/);
    assert.match(html, /Hardware prerequisites are unknown/);
    assert.doesNotMatch(html, /aria-label="Configure Token load scorer/);
});

test('dense groups expose accessible local page controls rather than growing without bounds', () => {
    const plan = servingFlowDiagram(graph, {}, () => true, { expandedGroup: 'routing' });
    const html = renderToStaticMarkup(<DeploymentPlanGraph plan={plan} onSelect={() => {}} />);
    assert.match(html, /Previous Endpoint selection settings/);
    assert.match(html, /Next Endpoint selection settings/);
    assert.ok([...html.matchAll(/is-optimization/g)].length <= 4);
    assert.match(html, /Configure Endpoint selection parameters/);
});
