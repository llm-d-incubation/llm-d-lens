import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { DeploymentSummary } from '../AgenticDeploymentWorkspace';
import { MeasuredOptimizationControls } from '../MeasuredOptimizationControls';
import { InferenceGuidePresets } from './InferenceGuidePresets';
import { createBlueprint } from '../../model/inference-designer/blueprint';

const endpoint = { value: 'models/inference:8000' };
const run = {
    status: 'awaiting_approval', request: { requirements: {}, execution_policy: { allow_trials: true } },
    selected_candidate: { id: 'a', replicas: 1, tensor_parallel_size: 1 },
    candidates: [{ id: 'a', deployable: true, required_gpus: 1, provider_ref: 'legacy-seed' }],
    configuration_plan: {
        source_bundle: { files: [{ name: 'deployment.yaml', content: 'manifest' }] }, source_candidates: { a: { endpoints: [endpoint] } }, endpoints: [endpoint],
        validation: { status: 'resources_checked' }, resource_estimate: { accelerators: 1 },
        configuration: { checksum: 'checked', content: { officialGuide: { renderedManifest: 'manifest' } } },
    },
};

test('source confirmation requires an explicitly selected reviewed inference Service', () => {
    const render = props => renderToStaticMarkup(<DeploymentSummary run={run} draft={{ clusterName: 'Fixture', storageVolumeId: 'cache' }} {...props} />);
    assert.match(render({}), /Inference Service/);
    assert.match(render({}), /<button[^>]*disabled=""[^>]*>Deploy<\/button>/);
    assert.doesNotMatch(render({ endpoint: endpoint.value }), /<button[^>]*disabled=""[^>]*>Deploy<\/button>/);
    assert.match(render({ endpoint: endpoint.value, editing: true }), /<button[^>]*disabled=""[^>]*>Deploy<\/button>/);
});

test('source benchmark controls do not create hidden cache variants or use seed identities', () => {
    const html = renderToStaticMarkup(<MeasuredOptimizationControls run={run} />);
    assert.match(html, /Check all source candidates/);
    assert.match(html, /Source configuration 1/);
    assert.doesNotMatch(html, /legacy-seed|Optional CPU cache composition/);
    assert.match(html, /no hidden cache variant is added/);
});

test('source benchmark candidate without a reviewed endpoint is blocked', () => {
    const saved = { ...run, optimization: { request: { candidate_ids: ['a'] } } };
    const html = renderToStaticMarkup(<MeasuredOptimizationControls run={saved} />);
    assert.match(html, /Check every selected source candidate and choose its inference Service/);
    assert.match(html, /<button[^>]*disabled=""[^>]*>Start deployment and benchmark tests<\/button>/);
});

test('all Guide options are initially visible, with unsupported reasons and editable selected choices', () => {
    const guides = [
        { id: 'optimized-baseline', title: 'Baseline', variables: {} },
        { id: 'fixture-addon', title: 'Fixture addon', variables: { MODE: { values: ['safe', 'fast'], default: 'safe' } } },
        { id: 'fixture-external', title: 'External tool', variables: {}, disposition: { kind: 'external', reason: 'An external operator is required.' } },
    ];
    const catalog = { commit: 'a'.repeat(40), guides, officialGuides: guides, assets: [] };
    const draft = { ...createBlueprint(), selectedGuides: ['fixture-addon'], capabilityOverrides: {}, guideChoices: {} };
    const plan = { guides: ['fixture-addon'], choices: [{ guide: 'fixture-addon', key: 'MODE', label: 'Mode', value: 'safe', options: [{ value: 'safe', label: 'Safe' }, { value: 'fast', label: 'Fast' }] }] };
    const html = renderToStaticMarkup(<InferenceGuidePresets catalog={catalog} draft={draft} plan={plan} includedGuides={['fixture-addon']} change={() => {}} />);
    assert.match(html, /aria-label="Guides options"/);
    assert.match(html, /External tool/);
    assert.match(html, /external operator is required/);
    assert.match(html, /aria-label="fixture-addon MODE"/);
    assert.match(html, /value="fast"/);
});
