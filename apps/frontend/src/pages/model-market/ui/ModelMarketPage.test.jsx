import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MODELS } from '@repo/shared/catalog';
import { AuthContext } from '../../auth/AuthContext.js';
import AgenticDeploymentWorkspace from '../../deployment/ui/AgenticDeploymentWorkspace.jsx';
import ManualDeploymentWorkspace from '../../deployment/ui/ManualDeploymentWorkspace.jsx';
import { DeploymentRequirementsForm } from '../../deployment/ui/DeploymentRequirementsForm.jsx';
import { DeploymentPlanReview } from '../../deployment/ui/DeploymentPlanReview.jsx';
import ModelMarketPage, { ModelCard } from './ModelMarketPage.jsx';
import { servingArguments } from '../../deployment/api/agenticDeploymentBackend.js';
import { standardRecommendation } from '../model/standardRecommendation.js';

const authenticated = child => createElement(AuthContext.Provider, { value: { can: () => true } }, child);

test('model cards retain catalog styling, natural height and full-width model type', () => {
    const html = renderToStaticMarkup(createElement(ModelCard, { model: MODELS[0], onSelect() {} }));
    assert.match(html, /type="button"/);
    assert.match(html, /\bh-auto\b/);
    assert.doesNotMatch(html, /\bh-8\b/);
    assert.match(html, /\bwhitespace-normal\b/);
    assert.match(html, /dark:bg-slate-900/);
    assert.match(html, /dark:hover:border-cyan-400\/60/);
    assert.match(html, /class="col-span-2"><dt[^>]*>Model type/);
});

test('deployment context uses the exact model card metadata for every listed context', () => {
    for (const model of MODELS.filter((model) => model.contextLength != null)) {
        assert.equal(standardRecommendation(model, null).maxModelLen, model.contextLength, model.repository);
    }
    const qwen = MODELS.find((model) => model.repository === 'Qwen/Qwen3-0.6B');
    assert.equal(standardRecommendation(qwen, null).maxModelLen, 40960);
});

test('deployment context keeps the default only when model card context is missing', () => {
    for (const contextLength of [null, undefined]) {
        assert.equal(standardRecommendation({ contextLength, sizeGiB: 1 }, null).maxModelLen, 4096);
    }
});

test('both deployment modes identify the model and cluster without redundant numbered stages', () => {
    for (const Workspace of [ManualDeploymentWorkspace, AgenticDeploymentWorkspace]) {
        const html = renderToStaticMarkup(authenticated(createElement(Workspace, { draft: { model: 'fixture/model', modelTitle: 'Fixture Model', clusterName: 'Research cluster', deploymentName: 'long-generated-name' }, embedded: true })));
        assert.match(html, /<h2[^>]*>Fixture Model<\/h2>/);
        assert.match(html, /(?:Manual|Agentic)(?:<!-- -->)? plan/);
        assert.equal((html.match(/Fixture Model/g) || []).length, 1);
        assert.match(html, /Research cluster/);
        assert.doesNotMatch(html, /Deployment target details|Planning mode<\/dt>/);
        assert.doesNotMatch(html, /aria-label="Deployment workflow"|<h2[^>]*>long-generated-name/);
        assert.doesNotMatch(html, /Custom vLLM parameters|GPU capacity for planning|Template-based deployment/);
    }
});

test('workload inputs are optional hints, not assumed traffic or compatibility gates', () => {
    for (const mode of ['manual', 'agentic']) {
        const html = renderToStaticMarkup(createElement(DeploymentRequirementsForm, { mode }));
        assert.match(html, /aria-label="Concurrent requests"[^>]*value=""/);
        assert.match(html, /Load target/);
        assert.match(html, /sizes serving replicas/);
        assert.match(html, /Mean input tokens<input[^>]*value=""/);
        assert.match(html, /Mean output tokens<input[^>]*value=""/);
        assert.match(html, /<details[^>]*><summary[^>]*>Request shape/);
        assert.match(html, /P95 TTFT/);
        assert.match(html, /QPS requires AIC prediction support/);
        assert.doesNotMatch(html, /P95 E2E|P95 input tokens|not both/);
        assert.doesNotMatch(html, /Long inputs with short answers|Shared prompts or documents/);
        if (mode === 'manual') assert.match(html, /AIC compares P\/D with combined serving/);
        assert.doesNotMatch(html, />Unknown</);
        assert.match(html, /No live deployment is changed/);
        assert.match(html, /resource limit constrains the topology/);
        assert.doesNotMatch(html, /<details[^>]* open|aria-label="Requirement steps"|>Continue</);
        const visible = html.replace(/<details[\s\S]*?<\/details>/g, '');
        assert.doesNotMatch(visible, /<input|<textarea|<select/);
        assert.match(html, /No required parameters/);
        assert.match(visible, />Balanced<|>Low latency<|>High throughput</);
        assert.doesNotMatch(html, /Tensor parallel|gpu-memory-utilization/);
    }
});

test('serving arguments add tool calling and reject plan-managed engine fields', () => {
    assert.deepEqual(servingArguments({ toolCalling: true, toolParser: 'hermes', extra: '--kv-cache-dtype=fp8\nreasoning-parser qwen3' }, [{ name: 'dtype', value: 'auto' }]), [
        { name: 'dtype', value: 'auto' },
        { name: 'enable-auto-tool-choice', value: 'true' }, { name: 'tool-call-parser', value: 'hermes' },
        { name: 'kv-cache-dtype', value: 'fp8' }, { name: 'reasoning-parser', value: 'qwen3' },
    ]);
    assert.deepEqual(servingArguments({ toolCalling: false, toolParser: '', extra: 'dtype=bfloat16' }, [{ name: 'dtype', value: 'auto' }]), [{ name: 'dtype', value: 'bfloat16' }]);
    assert.throws(() => servingArguments({ toolCalling: false, toolParser: '', extra: 'max-model-len=8192' }), /managed by the plan/);
    assert.throws(() => servingArguments({ toolCalling: false, toolParser: '', extra: '--enforce-eager' }), /name=value/);
    assert.throws(() => servingArguments({ toolCalling: true, toolParser: 'bad parser', extra: '' }), /parser/);
    assert.deepEqual(servingArguments({ toolCalling: true, toolParser: 'hermes', extra: 'tool-call-parser=qwen3_coder' }),
        [{ name: 'tool-call-parser', value: 'qwen3_coder' }]);
});

test('Manual review restores 3D topology and allows component editing', () => {
    const html = renderToStaticMarkup(createElement(DeploymentPlanReview, {
        candidate: { id: 'a', replicas: 1, tensor_parallel_size: 1, provider_ref: 'optimized-baseline' },
        model: 'test/model',
        onEdit() {},
        decisions: [
            { capability_id: 'prefix_cache', label: 'Prefix caching', status: 'dependency', reason: 'Required by the cache connector.' },
            { capability_id: 'autoscaling', label: 'Autoscaling', status: 'unknown', reason: 'Missing source support.' },
        ],
    }));
    assert.match(html, /Prefix caching/);
    assert.match(html, /2D overview/);
    assert.match(html, /Inference flow/);
    assert.match(html, /Verify/);
    assert.match(html, /3D explorer/);
    assert.doesNotMatch(html, /Needs information/);
});

test('model catalog preserves search and removes legacy pre-generation runtime controls', t => {
    let hasReturnDraft = false;
    const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        value: {
            getItem: key => hasReturnDraft && key === 'prism_model_market_return_draft' ? JSON.stringify({ model: MODELS[0].repository }) : null,
            removeItem() {},
        },
    });
    t.after(() => { if (original) Object.defineProperty(globalThis, 'sessionStorage', original); else delete globalThis.sessionStorage; });
    const renderPage = canDeploy => renderToStaticMarkup(createElement(AuthContext.Provider, { value: { can: () => canDeploy } }, createElement(ModelMarketPage, { onNavigate() {} })));
    const catalog = renderPage(true);
    assert.match(catalog, /role="tablist" aria-label="Model categories"/);
    assert.match(catalog, /aria-label="Search models"/);
    assert.match(catalog, /class="w-52 sm:w-64"/);
    assert.match(catalog, /lg:w-auto lg:flex-1/);
    assert.match(catalog, /grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3/);
    hasReturnDraft = true;
    const html = renderPage(true);
    assert.match(html, />Model Catalog<\/button>/);
    assert.match(html, />Manual</);
    assert.match(html, />Agentic</);
    assert.match(html, /Rule-based plan, core optimizations on/);
    assert.match(html, /AI recommends a plan for your goal/);
    assert.match(html, /Requires an AI provider/);
    assert.match(html, /<button[^>]*disabled=""[^>]*title="Add an AI provider to use Agentic planning."/);
    assert.match(html, /Planning mode/);
    assert.match(html, /Workload &amp; limits/);
    assert.match(html, /id="deployment-optimizations"[\s\S]*Core llm-d optimizations on/);
    assert.match(html, /id="deployment-objective"[\s\S]*Optimize for[\s\S]*>Low latency<[\s\S]*>High throughput</);
    assert.match(html, /vLLM options/);
    assert.match(html, /Load target[\s\S]*aria-label="Concurrent requests"/);
    assert.doesNotMatch(html, /P95 E2E|P95 input tokens/);
    assert.match(html, /Mean input tokens/);
    assert.match(html, /P95 TTFT/);
    assert.match(html, /aria-label="Cluster"/);
    assert.match(html, /aria-label="Deployment name"/);
    assert.match(html, /aria-label="Pod placement"/);
    assert.doesNotMatch(html, /Change setup|Choose how to build your plan/);
    assert.equal((html.match(/type="submit"/g) || []).length, 1);
    assert.ok(html.indexOf('id="deployment-target"') < html.indexOf('id="deployment-mode"'));
    assert.doesNotMatch(html, /Walkthrough|mock/i);
    assert.match(html, /Connect a cluster to deploy/);
    assert.doesNotMatch(html, /Current setup|Change target|Prepare model files|No storage volumes available/);
    assert.doesNotMatch(html, /aria-current="step"|1\. Target|2\. Deployment plan/);
    assert.match(html, />Generate plan</);
    assert.doesNotMatch(html, /Build Manual plan|Build Agentic plan/);
    assert.doesNotMatch(html, /Name and notes|Notes<textarea|Complete the workload picture/);
    assert.doesNotMatch(html, />Replicas<|>Tensor parallelism<|Custom vLLM parameters|Template-based deployment/);
    assert.match(renderPage(false), /Your role cannot create deployments/);
});

test('saved constraints remain visible and unchanged when the simplified form is reopened', () => {
    const html = renderToStaticMarkup(createElement(DeploymentRequirementsForm, {
        mode: 'agentic', initialValue: { objective: 'reliability', maxAccelerators: '8', ttftMs: '500', tpotMs: '20', inputTokens: '4096', forbiddenFeatures: ['external_kv_cache'] },
    }));
    assert.match(html, /aria-pressed="true"[^>]*>Reliability/);
    assert.match(html, /value="500"/);
    assert.match(html, /value="20"/);
    assert.match(html, /value="4096"/);
    assert.match(html, /<details open="">/);
    assert.doesNotMatch(html, /Predicted TTFT|GPU rental/);
});
