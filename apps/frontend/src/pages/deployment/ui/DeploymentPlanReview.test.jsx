import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { DeploymentPlanReview } from './DeploymentPlanReview.jsx';
import { DeploymentSummary } from './AgenticDeploymentWorkspace.jsx';
test('default review keeps 2D editing and restores the 3D explorer', () => {
    const html = renderToStaticMarkup(<DeploymentPlanReview content={'apiVersion: v1\nkind: Service\nmetadata:\n  name: endpoint'} />);
    assert.match(html, /3D explorer/);
    assert.match(html, /2D overview/);
    assert.match(html, /aria-label="Deployment settings"/);
    assert.doesNotMatch(html, /Component inspector|Inspect component/);
    assert.match(html, /Service: Service endpoint/);
});
test('agentic review labels candidate evidence before rendering', () => {
    const html = renderToStaticMarkup(<DeploymentPlanReview mode="agentic" model="test/model" candidate={{ provider_ref: 'optimized-baseline', replicas: 1, tensor_parallel_size: 2 }} />);
    assert.match(html, /Checks pending/);
    assert.match(html, /Combined serving/);
    assert.doesNotMatch(html, /optimized-baseline/);
    assert.doesNotMatch(html, /aria-label="Generated YAML"/);
});

const summaryRun = (checked = true) => ({
    status: 'awaiting_approval',
    selected_candidate: { replicas: 2, tensor_parallel_size: 2, prefill_replicas: 1, prefill_tensor_parallel_size: 2, native_cpu_cache_gib: 4 },
    configuration_plan: {
        configuration: { content: { officialGuide: { renderedManifest: checked ? 'manifest' : '' } } },
        resource_estimate: { available_accelerators: 8 },
        validation: { status: checked ? 'resources_checked' : 'pending', warnings: ['Check storage access on each node.'] },
    },
});
const summaryDraft = { clusterId: 'cluster-a', clusterName: 'Research cluster', storageVolumeId: 'cache-a' };

test('deployment summary distinguishes checked resources from startup and performance', () => {
    const html = renderToStaticMarkup(<DeploymentSummary run={summaryRun()} draft={summaryDraft} />);
    assert.doesNotMatch(html, /Ready for your confirmation/);
    assert.match(html, /Accelerators<\/p><p[^>]*>6 \/ 8<\/p><p[^>]*>required \/ available/);
    assert.match(html, /CPU KV cache 4 GiB \/ replica/);
    assert.match(html, /Cluster admission and startup happen after confirmation/);
    assert.match(html, /Readiness does not verify workload performance/);
    assert.match(html, />Deploy<\/button>/);
    assert.match(html, /1 configuration warning</);
});

test('summary cannot submit unchecked, editing, busy or locked plans', () => {
    const pending = renderToStaticMarkup(<DeploymentSummary run={summaryRun(false)} draft={summaryDraft} />);
    assert.match(pending, /Resource checks required/);
    assert.match(pending, />Retry checks<\/button>/);
    assert.doesNotMatch(pending, />Deploy<\/button>/);
    for (const props of [{ editing: true }, { busy: true }, { locked: true }, { locked: true, cleanupRequired: true }]) {
        const html = renderToStaticMarkup(<DeploymentSummary run={summaryRun()} draft={summaryDraft} {...props} />);
        assert.match(html, /<button[^>]*disabled=""[^>]*>(?:<svg[\s\S]*?<\/svg>)?Deploy<\/button>/);
    }
});

test('source-backed summary uses the reviewed bundle and still requires an inference endpoint', () => {
    const run = summaryRun();
    run.configuration_plan.configuration = { checksum: 'reviewed-source' };
    run.configuration_plan.source_bundle = { files: [{ name: 'deployment.yaml', content: 'reviewed manifest' }] };
    run.configuration_plan.endpoints = [{ value: 'inference:80' }];
    run.configuration_plan.decisions = [{ capability_id: 'prefix_cache', label: 'Engine prefix cache', status: 'enabled', reason: 'Configured' }];
    const pending = renderToStaticMarkup(<DeploymentSummary run={run} draft={summaryDraft} />);
    assert.match(pending, /Enabled in this YAML \(1\)[\s\S]*Engine prefix cache/);
    assert.match(pending, /<button[^>]*disabled=""[^>]*>Deploy<\/button>/);
    const selected = renderToStaticMarkup(<DeploymentSummary run={run} draft={summaryDraft} endpoint="inference:80" />);
    assert.match(selected, /Configuration and resource checks passed/);
    assert.doesNotMatch(selected, /<button[^>]*disabled=""[^>]*>Deploy<\/button>/);
    run.configuration_plan.source_bundle.files = [];
    assert.match(renderToStaticMarkup(<DeploymentSummary run={run} draft={summaryDraft} endpoint="inference:80" />), /Resource checks required/);
});

test('capacity summary distinguishes a recorded snapshot from missing history', () => {
    const run = summaryRun();
    run.configuration_plan.resource_estimate = { accelerators: 1, available_accelerators: 0, checked_at: '2026-11-16T08:00:00Z' };
    const recorded = renderToStaticMarkup(<DeploymentSummary run={run} draft={summaryDraft} />);
    assert.match(recorded, />1 \/ 0<\/p><p[^>]*>required \/ available/);
    assert.match(recorded, /checked /);
    assert.match(recorded, /Recheck resources/);
    delete run.configuration_plan.resource_estimate.available_accelerators;
    delete run.configuration_plan.resource_estimate.checked_at;
    const missing = renderToStaticMarkup(<DeploymentSummary run={run} draft={summaryDraft} />);
    assert.match(missing, />1<\/p><p[^>]*>required</);
    assert.match(missing, /capacity re-checked on deploy/);
});

test('summary reports actual execution readiness rather than the planning status', () => {
    const run = { ...summaryRun(), status: 'deploying', deployment_run_id: 'deploy-a' };
    for (const [execution, title] of [
        [null, 'Deployment submitted'],
        [{ status: 'running', cases: [{ status: 'deploying' }] }, 'Deployment submitted'],
        [{ status: 'failed', cases: [{ status: 'failed' }] }, 'Review deployment status'],
        [{ status: 'succeeded', cases: [] }, 'Deployment submitted'],
        [{ status: 'succeeded', cases: [{ status: 'ready' }] }, 'Deployment ready'],
    ]) {
        const html = renderToStaticMarkup(<DeploymentSummary run={run} draft={summaryDraft} deploymentRun={execution} onOpen={() => {}} />);
        assert.match(html, new RegExp(title));
        assert.match(html, />Open deployment<\/button>/);
        assert.doesNotMatch(html, />Deploy<\/button>|>Retry checks<\/button>|>Workload and limits<\/button>/);
    }
});

test('review exposes reasons and preserves technical views without making them the main actions', () => {
    const html = renderToStaticMarkup(<DeploymentPlanReview rationale="Fits the available capacity." evidence="Not benchmarked." />);
    assert.match(html, /Why this plan\?/);
    assert.match(html, /Fits the available capacity/);
    assert.match(html, /aria-label="Technical view"/);
    assert.match(html, /<option value="3d">3D explorer<\/option>/);
    assert.match(html, /Edit plan/);
    assert.doesNotMatch(html, /<button[^>]*>3D explorer<\/button>/);
});

test('goal-based planning requires an AI provider and keeps saved goals visible', async () => {
    const { DeploymentRequirementsForm, PlanningProviderSelect } = await import('./DeploymentRequirementsForm.jsx');
    const html = renderToStaticMarkup(<DeploymentRequirementsForm mode="agentic" />);
    assert.match(html, /Agentic does not use rule-based fallbacks/);
    assert.match(html, /<details><summary[^>]*>Describe a goal/);
    const restored = renderToStaticMarkup(<DeploymentRequirementsForm mode="agentic" initialValue={{ plannerPrompt: 'Analyze documents' }} />);
    assert.match(restored, /<details open=""><summary[^>]*>Describe a goal/);
    assert.match(restored, /Analyze documents/);
    const select = renderToStaticMarkup(<PlanningProviderSelect provider="" providers={[{ id: 'p', name: 'Provider' }]} onChange={() => {}} />);
    assert.doesNotMatch(select, /Rule-based/);
    assert.match(select, /required=""/);
});
test('creation asks for business needs and lists default-on optimizations', async () => {
    const { DeploymentRequirementsForm } = await import('./DeploymentRequirementsForm.jsx');
    const html = renderToStaticMarkup(<DeploymentRequirementsForm />);
    assert.match(html, /Manual setup/);
    assert.match(html, /AIC decides P\/D and checks the SLA/);
    assert.match(html, /SLA targets/);
    assert.doesNotMatch(html, /P95 E2E|Shared prompts|Long inputs with short answers/);
    assert.match(html, /resource limit constrains the topology/);
    assert.match(html, /Optimizations <span[^>]*>\/ all on/);
    assert.equal((html.match(/type="checkbox"[^>]*checked=""/g) || []).length, 4);
    assert.doesNotMatch(html, /KV cache offload/);
    assert.doesNotMatch(html, /placeholder="Unknown"/);
    assert.doesNotMatch(html, /Deployment method|Optimize for|General chat|1024|256/);
});

test('optimization workflow includes all decisions and direct edit/disable actions', () => {
    const html = renderToStaticMarkup(<DeploymentPlanReview
        candidate={{ replicas: 1, tensor_parallel_size: 1 }} onRequirementsChange={() => {}}
        decisions={[
            { capability_id: 'prefix_cache', label: 'Engine prefix cache', status: 'enabled', reason: 'Configured' },
            { capability_id: 'external_kv_cache', label: 'CPU KV cache', status: 'user_disabled', reason: 'Disabled' },
            { capability_id: 'autoscaling', label: 'Automatic scaling', status: 'unsupported', reason: 'No adapter' },
        ]}
    />);
    assert.match(html, /Inference flow/);
    assert.match(html, /Engine prefix cache/);
    assert.match(html, /CPU KV cache/);
    assert.match(html, /Automatic scaling/);
    assert.match(html, /aria-label="Edit Engine prefix cache"/);
    assert.match(html, /aria-label="Disable Engine prefix cache"/);
    assert.match(html, /aria-label="Enable CPU KV cache"/);
    assert.doesNotMatch(html, />(Edit|Disable|Enable)<\/button>/);
    assert.doesNotMatch(html, /Needs information/);
});

test('workload overrides display actual context and capacity without inventing traffic', async () => {
    const { DeploymentRequirementsForm } = await import('./DeploymentRequirementsForm.jsx');
    const html = renderToStaticMarkup(<DeploymentRequirementsForm runtime={{ max_model_len: 8192 }} resourceEstimate={{ available_accelerators: 4 }} initialValue={{ concurrency: '12' }} />);
    assert.match(html, /Context 8,192 tokens/);
    assert.match(html, /4 accelerators available/);
    assert.match(html, /value="12"/);
    assert.match(html, /Rebuild plan/);
    assert.match(html, /Mean input tokens<input[^>]*value=""/);
});

test('measured optimization exposes resource budgets and an explicit deployment action', async () => {
    const { MeasuredOptimizationControls } = await import('./MeasuredOptimizationControls.jsx');
    const html = renderToStaticMarkup(<MeasuredOptimizationControls run={{ id: 'plan', status: 'awaiting_approval', candidates: [] }} onUpdate={() => {}} onReview={() => {}} />);
    assert.match(html, /Maximum accelerators/);
    assert.match(html, /Maximum minutes/);
    assert.match(html, /Repetitions per candidate/);
    assert.match(html, /Start deployment and benchmark tests/);
    assert.match(html, /best tested candidate/);
});


test('trial settings restore recorded workload and prevent restarting deployed or unclean plans', async () => {
    const { MeasuredOptimizationControls } = await import('./MeasuredOptimizationControls.jsx');
    for (const [status, optimizationStatus] of [['deploying', 'completed'], ['awaiting_approval', 'cleanup_failed']]) {
        const run = { id: 'plan', status, candidates: [], optimization: { status: optimizationStatus, request: {
            candidate_ids: ['candidate-a'], max_gpu_count: 3, repetitions: 2,
            benchmark: { matrix: [{ isl: 1024, osl: 64 }], concurrency_stages: [{ concurrency: 8, num_requests: 200 }] },
        } } };
        const html = renderToStaticMarkup(<MeasuredOptimizationControls run={run} />);
        assert.match(html, /<fieldset disabled=""/);
        assert.match(html, /Input tokens<input[^>]*value="1024"/);
        assert.match(html, /Concurrent requests<input[^>]*value="8"/);
        if (optimizationStatus === 'cleanup_failed') assert.match(html, /Clean up interrupted tests/);
    }
});

test('measured trials inherit latency and snapshot capacity and expose confirmation evidence', async () => {
    const { MeasuredOptimizationControls } = await import('./MeasuredOptimizationControls.jsx');
    const run = { id: 'plan', status: 'awaiting_approval', candidates: [], request: { requirements: { objective: 'latency' } }, configuration_plan: { resource_estimate: { available_accelerators: 6 } }, optimization: { status: 'completed', samples: [], confirmation: { status: 'failed', candidate_id: 'candidate-a', samples: [{ candidate_id: 'candidate-a', status: 'failed', benchmark_id: 'confirmation-id', error: 'warm-up failed' }] } } };
    const html = renderToStaticMarkup(<MeasuredOptimizationControls run={run} />);
    assert.match(html, /Maximum accelerators<input[^>]*value="6"/);
    assert.match(html, /Maximum benchmark runs<input[^>]*value="5"/);
    assert.match(html, /Winner confirmation repetitions<input[^>]*value="1"/);
    assert.match(html, /value="latency" selected=""/);
    assert.match(html, /Benchmark: confirmation-id/);
    assert.match(html, /warm-up failed/);
});
