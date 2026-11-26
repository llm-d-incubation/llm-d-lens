import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { AuthContext } from '../../auth/AuthContext.js';
import { DeploymentPermissionHandoff, DeploymentPlanReview } from './DeploymentPlanReview.jsx';
import { DeploymentSummary, DeploymentWorkflow } from './AgenticDeploymentWorkspace.jsx';
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

test('DeploymentPermissionHandoff renders bootstrap setup, collapsible details, and optional subject override', () => {
    const html = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="bootstrap"
            namespace="llmd-qwen"
            report={{
                status: 'admin_required',
                sufficient: false,
                namespace: 'llmd-qwen',
                identity: { kind: 'unknown', overrideRequired: true },
                adminOperations: [{ id: 'ns.create', title: 'Create namespace and RBAC' }],
                userOperations: [{ id: 'deploy.apply', title: 'Apply deployment manifests' }],
                missing: [{ operationId: 'ns.create', verb: 'create', resource: 'namespaces' }],
                warnings: ['EPP metrics require ClusterRoleBinding'],
                marker: { namespace: 'llmd-qwen', name: 'lens-deployment-bootstrap', status: 'missing' },
            }}
            script={'#!/usr/bin/env bash\nkubectl apply -f -'}
            filename="llmd-qwen-bootstrap.sh"
            onGenerateScript={() => {}}
            onPrimaryAction={() => {}}
            primaryActionLabel="Re-check &amp; Deploy"
        />,
    );
    assert.match(html, /Orchestration administrator namespace setup/);
    assert.match(html, /llmd-qwen/);
    assert.match(html, /Awaiting Orchestration administrator/);
    assert.match(html, /Kubernetes identity override/);
    assert.match(html, /placeholder="team-namespace"/);
    assert.doesNotMatch(html, /placeholder="lens-edit"/);
    assert.match(html, /Download \.sh/);
    assert.match(html, /Copy script/);
    assert.match(html, /Re-check &amp; Deploy/);
    assert.match(html, /View operation details/);
    assert.match(html, /<details class="group\/details[^"]*" open="">/);
    assert.match(html, /<details class="group">/);
    assert.doesNotMatch(html, /<details class="group"[^>]*open/);
    assert.match(html, /Create namespace and RBAC/);
    assert.match(html, /Apply deployment manifests/);
    assert.match(html, /create namespaces/);
    assert.match(html, /lens-deployment-bootstrap/);
    assert.doesNotMatch(html, /\(s\)/);
});

test('DeploymentPermissionHandoff hides subject override when identity is detected and renders cleanup fallback only when unconfirmable', () => {
    const detectedBootstrap = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="bootstrap"
            namespace="llmd-qwen"
            identity={{ kind: 'ServiceAccount', name: 'lens-sa', namespace: 'lens-edit', overrideRequired: false }}
            script="#!/usr/bin/env bash"
            onPrimaryAction={() => {}}
        />,
    );
    assert.doesNotMatch(detectedBootstrap, /Kubernetes identity override/);
    assert.match(detectedBootstrap, /<details class="group\/details[^"]*" open="">/);

    const verifiableCleanup = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="cleanup"
            namespace="llmd-qwen"
            handoff={{
                canVerifyMarker: true,
                confirmRequired: false,
                script: '#!/usr/bin/env bash\nkubectl delete ns llmd-qwen',
                operations: [{ id: 'delete-ns', kind: 'Namespace', name: 'llmd-qwen' }],
                completedOperations: ['Deployment/qwen'],
                marker: { namespace: 'lens-edit', name: 'lens-deploy-cleanup-1', status: 'missing' },
            }}
            onPrimaryAction={() => {}}
            onConfirmDelete={() => {}}
        />,
    );
    assert.match(verifiableCleanup, /Orchestration administrator namespace cleanup/);
    assert.match(verifiableCleanup, /Re-check &amp; Delete/);
    assert.doesNotMatch(verifiableCleanup, /Confirm &amp; Delete Record/);
    assert.match(verifiableCleanup, /<details class="group\/details[^"]*" open="">/);
    assert.match(verifiableCleanup, /<details class="group">/);
    assert.doesNotMatch(verifiableCleanup, /<details class="group"[^>]*open/);

    const unconfirmableCleanup = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="cleanup"
            namespace="llmd-qwen"
            handoff={{
                canVerifyMarker: false,
                confirmRequired: true,
                script: '#!/usr/bin/env bash\nkubectl delete ns llmd-qwen',
                operations: ['delete-namespace'],
            }}
            onPrimaryAction={() => {}}
            onConfirmDelete={() => {}}
        />,
    );
    assert.match(unconfirmableCleanup, /Unconfirmed/);
    assert.match(unconfirmableCleanup, /Confirm &amp; Delete Record/);
    assert.match(unconfirmableCleanup, /<details class="group\/details[^"]*" open="">/);
    assert.match(unconfirmableCleanup, /<details class="group">/);
    assert.doesNotMatch(unconfirmableCleanup, /<details class="group"[^>]*open/);
    assert.doesNotMatch(unconfirmableCleanup, /\(s\)/);
});

test('DeploymentPermissionHandoff collapses operation details when verified in bootstrap and cleanup modes', () => {
    const verifiedBootstrap = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="bootstrap"
            namespace="llmd-qwen"
            report={{
                status: 'verified',
                sufficient: true,
                adminOperations: [{ id: 'ns.create', title: 'Create namespace' }],
                userOperations: [{ id: 'deploy.apply', title: 'Apply manifests' }],
            }}
            handoff={{
                verified: true,
                status: 'verified',
                script: '#!/usr/bin/env bash',
            }}
            onPrimaryAction={() => {}}
        />,
    );
    assert.match(verifiedBootstrap, /<details class="group\/details/);
    assert.doesNotMatch(verifiedBootstrap, /<details[^>]*open/);

    const sufficientBootstrap = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="bootstrap"
            namespace="llmd-qwen"
            report={{
                sufficient: true,
                adminOperations: [{ id: 'ns.create', title: 'Create namespace' }],
            }}
            onPrimaryAction={() => {}}
        />,
    );
    assert.match(sufficientBootstrap, /<details class="group\/details/);
    assert.doesNotMatch(sufficientBootstrap, /<details[^>]*open/);

    const verifiedCleanup = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="cleanup"
            namespace="llmd-qwen"
            handoff={{
                verified: true,
                status: 'verified',
                operations: [{ id: 'delete-ns', kind: 'Namespace', name: 'llmd-qwen' }],
            }}
            onPrimaryAction={() => {}}
        />,
    );
    assert.match(verifiedCleanup, /<details class="group\/details/);
    assert.doesNotMatch(verifiedCleanup, /<details[^>]*open/);

    const deletedCleanup = renderToStaticMarkup(
        <DeploymentPermissionHandoff
            mode="cleanup"
            namespace="llmd-qwen"
            handoff={{
                status: 'deleted',
                operations: [{ id: 'delete-ns', kind: 'Namespace', name: 'llmd-qwen' }],
            }}
            onPrimaryAction={() => {}}
        />,
    );
    assert.match(deletedCleanup, /<details class="group\/details/);
    assert.doesNotMatch(deletedCleanup, /<details[^>]*open/);
});

test('DeploymentSummary renders Orchestration administrator namespace setup when adminHandoff is active and disables Deploy button', () => {
    const html = renderToStaticMarkup(
        <DeploymentSummary
            run={summaryRun()}
            draft={summaryDraft}
            adminHandoff={{
                namespace: 'llmd-test',
                report: { status: 'admin_required', adminOperations: [{ id: 'ns.create', title: 'Create namespace' }] },
                script: '#!/usr/bin/env bash',
                identity: { kind: 'ServiceAccount', overrideRequired: false },
            }}
            onVerifyBootstrapAndDeploy={() => {}}
        />,
    );
    assert.match(html, /Orchestration administrator namespace setup/);
    assert.match(html, /Re-check &amp; Deploy/);
    assert.match(html, /<button[^>]*disabled=""[^>]*>Deploy<\/button>/);
});

test('DeploymentSummary renders error prominently via FormError alert', () => {
    const html = renderToStaticMarkup(
        <DeploymentSummary
            run={summaryRun()}
            draft={summaryDraft}
            confirmLabel="Confirm deployment"
            error="Insufficient free accelerators on cluster."
        />,
    );
    assert.match(html, /<p role="alert"[^>]*>Insufficient free accelerators on cluster\.<\/p>/);
    assert.match(html, /Confirm deployment/);
});

function createTestDom() {
    const makeNode = (tag, doc) => ({
        nodeType: tag === '#text' ? 3 : tag === '#comment' ? 8 : 1,
        nodeName: tag.toUpperCase(),
        tagName: tag.toUpperCase(),
        namespaceURI: 'http://www.w3.org/1999/xhtml',
        ownerDocument: doc,
        parentNode: null,
        childNodes: [],
        attributes: {},
        style: {},
        dataset: {},
        nodeValue: '',
        get textContent() {
            if (this.nodeType === 3) return this.nodeValue || '';
            if (this.nodeType === 8) return '';
            return this.childNodes.map((child) => child.textContent).join('');
        },
        set textContent(value) {
            const text = String(value);
            if (this.nodeType === 3) {
                this.nodeValue = text;
                return;
            }
            this.childNodes = text ? [doc.createTextNode(text)] : [];
            for (const child of this.childNodes) child.parentNode = this;
        },
        addEventListener() {},
        removeEventListener() {},
        setAttribute(key, value) { this.attributes[key] = String(value); },
        removeAttribute(key) { delete this.attributes[key]; },
        getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; },
        hasAttribute(key) { return key in this.attributes; },
        appendChild(child) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = this;
            this.childNodes.push(child);
            return child;
        },
        insertBefore(child, reference) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = this;
            const index = reference ? this.childNodes.indexOf(reference) : -1;
            if (index === -1) this.childNodes.push(child);
            else this.childNodes.splice(index, 0, child);
            return child;
        },
        removeChild(child) {
            const index = this.childNodes.indexOf(child);
            if (index !== -1) {
                this.childNodes.splice(index, 1);
                child.parentNode = null;
            }
            return child;
        },
        get firstChild() { return this.childNodes[0] || null; },
        get options() { return findElements(this, (el) => el.tagName === 'OPTION'); },
        get nextSibling() {
            if (!this.parentNode) return null;
            const index = this.parentNode.childNodes.indexOf(this);
            return index >= 0 ? this.parentNode.childNodes[index + 1] || null : null;
        },
        focus() {},
        blur() {},
    });
    const win = {
        addEventListener() {},
        removeEventListener() {},
        setInterval,
        clearInterval,
        requestAnimationFrame: (callback) => setTimeout(callback, 0),
        cancelAnimationFrame: clearTimeout,
        HTMLIFrameElement: function HTMLIFrameElement() {},
        HTMLElement: function HTMLElement() {},
        SVGElement: function SVGElement() {},
        Node: function Node() {},
    };
    const doc = {
        nodeType: 9,
        hidden: true,
        cookie: '',
        defaultView: win,
        documentElement: null,
        body: null,
        activeElement: null,
        createElement(tag) { return makeNode(tag, doc); },
        createElementNS(ns, tag) {
            const node = makeNode(tag, doc);
            node.namespaceURI = ns;
            return node;
        },
        createTextNode(text) {
            const node = makeNode('#text', doc);
            node.nodeValue = String(text);
            return node;
        },
        createComment(text) {
            const node = makeNode('#comment', doc);
            node.nodeValue = String(text);
            return node;
        },
        addEventListener() {},
        removeEventListener() {},
        getElementById() { return null; },
    };
    win.document = doc;
    doc.documentElement = makeNode('html', doc);
    doc.body = makeNode('body', doc);
    doc.documentElement.appendChild(doc.body);
    return { doc, win };
}

function findElements(root, predicate, acc = []) {
    if (!root || root.nodeType !== 1) return acc;
    if (predicate(root)) acc.push(root);
    for (const child of root.childNodes) findElements(child, predicate, acc);
    return acc;
}

function reactProps(element) {
    const key = Object.keys(element).find((name) => name.startsWith('__reactProps$'));
    return key ? element[key] : {};
}

async function withWorkflowDom(t, fetchHandler, runScenario) {
    const prevAct = globalThis.IS_REACT_ACT_ENVIRONMENT;
    const prevDoc = globalThis.document;
    const prevWin = globalThis.window;
    const prevHtml = globalThis.HTMLElement;
    const prevSvg = globalThis.SVGElement;
    const prevIframe = globalThis.HTMLIFrameElement;
    const prevFetch = globalThis.fetch;
    const prevStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    const store = new Map();
    const { doc, win } = createTestDom();
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    globalThis.document = doc;
    globalThis.window = win;
    globalThis.HTMLElement = win.HTMLElement;
    globalThis.SVGElement = win.SVGElement;
    globalThis.HTMLIFrameElement = win.HTMLIFrameElement;
    globalThis.fetch = fetchHandler;
    Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        value: {
            getItem: (key) => (store.has(key) ? store.get(key) : null),
            setItem: (key, val) => { store.set(key, String(val)); },
            removeItem: (key) => { store.delete(key); },
        },
    });
    const container = doc.createElement('div');
    doc.body.appendChild(container);
    const root = createRoot(container);
    t.after(async () => {
        await act(async () => { root.unmount(); });
        globalThis.IS_REACT_ACT_ENVIRONMENT = prevAct;
        if (prevDoc === undefined) delete globalThis.document; else globalThis.document = prevDoc;
        if (prevWin === undefined) delete globalThis.window; else globalThis.window = prevWin;
        if (prevHtml === undefined) delete globalThis.HTMLElement; else globalThis.HTMLElement = prevHtml;
        if (prevSvg === undefined) delete globalThis.SVGElement; else globalThis.SVGElement = prevSvg;
        if (prevIframe === undefined) delete globalThis.HTMLIFrameElement; else globalThis.HTMLIFrameElement = prevIframe;
        globalThis.fetch = prevFetch;
        if (prevStorage) Object.defineProperty(globalThis, 'sessionStorage', prevStorage);
        else delete globalThis.sessionStorage;
    });
    const findButton = (labelPattern) => findElements(
        doc.body,
        (el) => el.tagName === 'BUTTON' && labelPattern.test(el.textContent.trim()),
    )[0] || null;
    const clickButton = async (labelPattern) => {
        const button = findButton(labelPattern);
        assert.ok(button, `Expected button matching ${labelPattern}`);
        const props = reactProps(button);
        assert.equal(Boolean(props.disabled), false, `Expected button ${labelPattern} to be enabled`);
        await act(async () => {
            await props.onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
    };
    const alerts = () => findElements(
        doc.body,
        (el) => el.getAttribute('role') === 'alert',
    ).map((el) => el.textContent.trim());
    await runScenario({ root, doc, findButton, clickButton, alerts });
}

function makeRestoredPlanRun() {
    return {
        id: 'run-plan-1',
        revision: 1,
        status: 'awaiting_approval',
        request: {
            deployment_name: 'qwen-serving',
            ai_provider_id: 'prov-1',
            execution_policy: { allow_trials: false },
            requirements: {
                objective: 'balanced',
                workload: {},
                hard_constraints: {},
                performance_targets: {},
                required_features: [],
                forbidden_features: [],
            },
        },
        selected_candidate: { id: 'cand-1', replicas: 1, tensor_parallel_size: 1, provider_ref: 'optimized-baseline' },
        candidates: [{ id: 'cand-1', replicas: 1, tensor_parallel_size: 1, required_gpus: 1, provider_ref: 'optimized-baseline' }],
        configuration_plan: {
            configuration: {
                checksum: 'checksum-1',
                content: { officialGuide: { renderedManifest: 'apiVersion: v1\nkind: Service\nmetadata:\n  name: inference-gateway' } },
            },
            source_bundle: {
                files: [{ name: 'deployment.yaml', content: 'apiVersion: v1\nkind: Service\nmetadata:\n  name: inference-gateway' }],
            },
            endpoints: [{ value: 'inference-gateway:80', serviceName: 'inference-gateway', port: 80 }],
            resource_estimate: { accelerators: 1, available_accelerators: 4 },
            validation: { status: 'resources_checked', warnings: [] },
        },
    };
}

const workflowDraft = {
    clusterId: 'cluster-a',
    clusterName: 'Cluster A',
    model: 'Qwen/Qwen3-8B',
    deploymentName: 'qwen-serving',
    storageVolumeId: 'vol-1',
    resumeRunId: 'run-plan-1',
};

function jsonResponse(body, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

test('DeploymentWorkflow verifyBootstrapAndDeploy forwards verified namespace and initialized endpoint to approve', async (t) => {
    const planRun = makeRestoredPlanRun();
    const approvePayloads = [];
    const startedRuns = [];
    await withWorkflowDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/agentic-deployments/run-plan-1') return jsonResponse(planRun);
            if (url === '/api/v1/deployments/permission-check') {
                return jsonResponse({
                    sufficient: false,
                    status: 'admin_required',
                    namespace: 'llmd-pending',
                    identity: { kind: 'ServiceAccount', name: 'deployer', namespace: 'team-ns', overrideRequired: false },
                    adminOperations: [{ id: 'ns.create', title: 'Create namespace' }],
                });
            }
            if (url === '/api/v1/deployments/permission-remediation') {
                return jsonResponse({
                    script: '#!/usr/bin/env bash\nkubectl apply -f -',
                    namespace: 'llmd-bootstrapped-1',
                    bootstrapDigest: 'digest-1',
                });
            }
            if (url === '/api/v1/deployments/admin-handoff/verify') {
                return jsonResponse({
                    verified: true,
                    status: 'verified',
                    namespace: 'llmd-bootstrapped-1',
                });
            }
            if (url === '/api/agentic-deployments/run-plan-1/approve') {
                approvePayloads.push(JSON.parse(options.body));
                return jsonResponse({ ...planRun, status: 'deploying', deployment_run_id: 'exec-run-1' }, 202);
            }
            throw new Error(`Unexpected fetch: ${url}`);
        },
        async ({ root, clickButton }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ can: () => true }}>
                        <DeploymentWorkflow
                            draft={workflowDraft}
                            mode="manual"
                            embedded
                            onDeploymentStarted={(result) => startedRuns.push(result)}
                        />
                    </AuthContext.Provider>,
                );
            });
            await clickButton(/Review & deploy/);
            await clickButton(/^Confirm deployment$/);
            await clickButton(/Re-check & Deploy/);

            assert.equal(approvePayloads.length, 1);
            assert.deepEqual(approvePayloads[0], {
                configuration_checksum: 'checksum-1',
                endpoint: 'inference-gateway:80',
                namespace: 'llmd-bootstrapped-1',
            });
            assert.equal(startedRuns.length, 1);
            assert.equal(startedRuns[0].deployment_run_id, 'exec-run-1');
        },
    );
});

test('DeploymentWorkflow verifyBootstrapAndDeploy surfaces 409 approve conflict prominently and re-enables Confirm deployment', async (t) => {
    const planRun = makeRestoredPlanRun();
    let approveCalls = 0;
    await withWorkflowDom(
        t,
        async (url) => {
            if (url === '/api/agentic-deployments/run-plan-1') return jsonResponse(planRun);
            if (url === '/api/v1/deployments/permission-check') {
                return jsonResponse({
                    sufficient: false,
                    status: 'admin_required',
                    namespace: 'llmd-bootstrapped-1',
                    identity: { kind: 'ServiceAccount', name: 'deployer', namespace: 'team-ns', overrideRequired: false },
                    adminOperations: [{ id: 'ns.create', title: 'Create namespace' }],
                });
            }
            if (url === '/api/v1/deployments/permission-remediation') {
                return jsonResponse({
                    script: '#!/usr/bin/env bash\nkubectl apply -f -',
                    namespace: 'llmd-bootstrapped-1',
                });
            }
            if (url === '/api/v1/deployments/admin-handoff/verify') {
                return jsonResponse({
                    verified: true,
                    status: 'verified',
                    namespace: 'llmd-bootstrapped-1',
                });
            }
            if (url === '/api/agentic-deployments/run-plan-1/approve') {
                approveCalls += 1;
                return jsonResponse({ detail: 'Model cache PVC is not bound in target namespace.' }, 409);
            }
            throw new Error(`Unexpected fetch: ${url}`);
        },
        async ({ root, findButton, clickButton, alerts }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ can: () => true }}>
                        <DeploymentWorkflow draft={workflowDraft} mode="manual" embedded />
                    </AuthContext.Provider>,
                );
            });
            await clickButton(/Review & deploy/);
            await clickButton(/^Confirm deployment$/);
            assert.equal(Boolean( reactProps(findButton(/^Confirm deployment$/)).disabled ), true);

            await clickButton(/Re-check & Deploy/);
            assert.equal(approveCalls, 1);
            assert.ok(
                alerts().some((text) => text.includes('Model cache PVC is not bound in target namespace.')),
                `Expected conflict detail in alerts, got: ${JSON.stringify(alerts())}`,
            );
            const confirmBtn = findButton(/^Confirm deployment$/);
            assert.ok(confirmBtn);
            assert.equal(Boolean(reactProps(confirmBtn).disabled), false);
        },
    );
});

test('DeploymentWorkflow deploy passes checked namespace to approve and opens adminHandoff on 409 deployment_admin_setup_required', async (t) => {
    const planRun = makeRestoredPlanRun();
    const approvePayloads = [];
    await withWorkflowDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/agentic-deployments/run-plan-1') return jsonResponse(planRun);
            if (url === '/api/v1/deployments/permission-check') {
                return jsonResponse({
                    sufficient: true,
                    status: 'sufficient',
                    namespace: 'llmd-bootstrapped-1',
                });
            }
            if (url === '/api/agentic-deployments/run-plan-1/approve') {
                approvePayloads.push(JSON.parse(options.body));
                return jsonResponse({
                    code: 'deployment_admin_setup_required',
                    detail: 'Namespace setup is required before deploying.',
                    namespace: 'llmd-bootstrapped-1',
                    identity: { kind: 'ServiceAccount', name: 'deployer', namespace: 'team-ns', overrideRequired: false },
                    adminOperations: [{ id: 'ns.create', title: 'Create namespace and RBAC' }],
                    script: '#!/usr/bin/env bash\nkubectl apply -f -',
                }, 409);
            }
            throw new Error(`Unexpected fetch: ${url}`);
        },
        async ({ root, doc, findButton, clickButton }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ can: () => true }}>
                        <DeploymentWorkflow draft={workflowDraft} mode="manual" embedded />
                    </AuthContext.Provider>,
                );
            });
            await clickButton(/Review & deploy/);
            await clickButton(/^Confirm deployment$/);

            assert.equal(approvePayloads.length, 1);
            assert.equal(approvePayloads[0].namespace, 'llmd-bootstrapped-1');
            assert.match(doc.body.textContent, /Orchestration administrator namespace setup/);
            assert.ok(findButton(/Re-check & Deploy/), 'Expected adminHandoff Re-check & Deploy button to render');
            assert.equal(Boolean(reactProps(findButton(/^Confirm deployment$/)).disabled), true);
        },
    );
});

test('DeploymentWorkflow executeApprove auto-confirms prepared plan when preflight status is pass', async (t) => {
    const planRun = makeRestoredPlanRun();
    const approvePayloads = [];
    const startedRuns = [];
    await withWorkflowDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/agentic-deployments/run-plan-1') return jsonResponse(planRun);
            if (url === '/api/v1/deployments/permission-check') {
                return jsonResponse({
                    sufficient: true,
                    status: 'sufficient',
                    namespace: 'llmd-pass-1',
                });
            }
            if (url === '/api/agentic-deployments/run-plan-1/approve') {
                const payload = JSON.parse(options.body);
                approvePayloads.push(payload);
                if (!payload.prepared_plan_id) {
                    return jsonResponse({
                        ...planRun,
                        configuration_plan: {
                            ...planRun.configuration_plan,
                            validation: {
                                ...planRun.configuration_plan.validation,
                                preparedDeployment: {
                                    id: 'prep-pass-1',
                                    digest: 'sha256:prep-pass-1',
                                    preflight: { status: 'pass', errors: [], unmodeled: [] },
                                    cases: [{ namespace: 'llmd-pass-1' }],
                                },
                            },
                        },
                    });
                }
                return jsonResponse({ ...planRun, status: 'deploying', deployment_run_id: 'exec-pass-1' }, 202);
            }
            throw new Error(`Unexpected fetch: ${url}`);
        },
        async ({ root, clickButton }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ can: () => true }}>
                        <DeploymentWorkflow
                            draft={workflowDraft}
                            mode="manual"
                            embedded
                            onDeploymentStarted={(result) => startedRuns.push(result)}
                        />
                    </AuthContext.Provider>,
                );
            });
            await clickButton(/Review & deploy/);
            await clickButton(/^Confirm deployment$/);

            assert.equal(approvePayloads.length, 2);
            assert.deepEqual(approvePayloads[0], {
                configuration_checksum: 'checksum-1',
                endpoint: 'inference-gateway:80',
                namespace: 'llmd-pass-1',
            });
            assert.deepEqual(approvePayloads[1], {
                configuration_checksum: 'checksum-1',
                endpoint: 'inference-gateway:80',
                namespace: 'llmd-pass-1',
                prepared_plan_id: 'prep-pass-1',
                expected_plan_digest: 'sha256:prep-pass-1',
                acknowledge_unknown: false,
            });
            assert.equal(startedRuns.length, 1);
            assert.equal(startedRuns[0].deployment_run_id, 'exec-pass-1');
        },
    );
});

test('DeploymentWorkflow Non-Admin Re-check & Deploy clears adminHandoff on unknown preflight and completes step 2 with frozen namespace', async (t) => {
    const planRun = makeRestoredPlanRun();
    const permissionCheckPayloads = [];
    const approvePayloads = [];
    const startedRuns = [];
    await withWorkflowDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/agentic-deployments/run-plan-1') return jsonResponse(planRun);
            if (url === '/api/v1/deployments/permission-check') {
                const payload = JSON.parse(options.body);
                permissionCheckPayloads.push(payload);
                if (payload.prepared_plan_id === 'prep-unk-1') {
                    return jsonResponse({
                        sufficient: true,
                        status: 'sufficient',
                        namespace: 'llmd-frozen-1',
                    });
                }
                return jsonResponse({
                    sufficient: false,
                    status: 'admin_required',
                    namespace: 'llmd-bootstrapped-1',
                    identity: { kind: 'ServiceAccount', name: 'deployer', namespace: 'team-ns', overrideRequired: false },
                    adminOperations: [{ id: 'ns.create', title: 'Create namespace' }],
                });
            }
            if (url === '/api/v1/deployments/permission-remediation') {
                return jsonResponse({
                    script: '#!/usr/bin/env bash\nkubectl apply -f -',
                    namespace: 'llmd-bootstrapped-1',
                    bootstrapDigest: 'digest-unk-1',
                });
            }
            if (url === '/api/v1/deployments/admin-handoff/verify') {
                return jsonResponse({
                    verified: true,
                    status: 'verified',
                    namespace: 'llmd-bootstrapped-1',
                });
            }
            if (url === '/api/agentic-deployments/run-plan-1/approve') {
                const payload = JSON.parse(options.body);
                approvePayloads.push(payload);
                if (!payload.prepared_plan_id) {
                    return jsonResponse({
                        ...planRun,
                        configuration_plan: {
                            ...planRun.configuration_plan,
                            validation: {
                                ...planRun.configuration_plan.validation,
                                preparedDeployment: {
                                    id: 'prep-unk-1',
                                    digest: 'sha256:prep-unk-1',
                                    preflight: {
                                        status: 'unknown',
                                        errors: [],
                                        unmodeled: ['RuntimeClass inspection is restricted (403)'],
                                    },
                                    cases: [{ namespace: 'llmd-frozen-1' }],
                                },
                            },
                        },
                    });
                }
                return jsonResponse({ ...planRun, status: 'deploying', deployment_run_id: 'exec-unk-1' }, 202);
            }
            throw new Error(`Unexpected fetch: ${url}`);
        },
        async ({ root, findButton, clickButton }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ can: () => true }}>
                        <DeploymentWorkflow
                            draft={workflowDraft}
                            mode="manual"
                            embedded
                            onDeploymentStarted={(result) => startedRuns.push(result)}
                        />
                    </AuthContext.Provider>,
                );
            });
            await clickButton(/Review & deploy/);
            await clickButton(/^Confirm deployment$/);
            await clickButton(/Re-check & Deploy/);

            assert.equal(findButton(/Re-check & Deploy/), null, 'Expected adminHandoff to clear after unknown prepared plan returns');
            const acceptBtn = findButton(/^Accept risks and deploy$/);
            const backBtn = findButton(/Back to edit/);
            assert.ok(acceptBtn);
            assert.ok(backBtn);
            assert.equal(Boolean(reactProps(acceptBtn).disabled), false);
            assert.equal(Boolean(reactProps(backBtn).disabled), false);

            await clickButton(/^Accept risks and deploy$/);

            assert.equal(permissionCheckPayloads.length, 2);
            assert.equal(permissionCheckPayloads[1].prepared_plan_id, 'prep-unk-1');
            assert.equal(permissionCheckPayloads[1].namespace, 'llmd-frozen-1');
            assert.equal(approvePayloads.length, 2);
            assert.deepEqual(approvePayloads[1], {
                configuration_checksum: 'checksum-1',
                endpoint: 'inference-gateway:80',
                namespace: 'llmd-frozen-1',
                prepared_plan_id: 'prep-unk-1',
                expected_plan_digest: 'sha256:prep-unk-1',
                acknowledge_unknown: true,
            });
            assert.equal(startedRuns.length, 1);
            assert.equal(startedRuns[0].deployment_run_id, 'exec-unk-1');
        },
    );
});

test('DeploymentWorkflow clears adminHandoff and blocks confirm when prepared plan preflight fails', async (t) => {
    const planRun = makeRestoredPlanRun();
    await withWorkflowDom(
        t,
        async (url) => {
            if (url === '/api/agentic-deployments/run-plan-1') return jsonResponse(planRun);
            if (url === '/api/v1/deployments/permission-check') {
                return jsonResponse({
                    sufficient: false,
                    status: 'admin_required',
                    namespace: 'llmd-bootstrapped-1',
                    identity: { kind: 'ServiceAccount', name: 'deployer', namespace: 'team-ns', overrideRequired: false },
                    adminOperations: [{ id: 'ns.create', title: 'Create namespace' }],
                });
            }
            if (url === '/api/v1/deployments/permission-remediation') {
                return jsonResponse({ script: '#!/usr/bin/env bash', namespace: 'llmd-bootstrapped-1' });
            }
            if (url === '/api/v1/deployments/admin-handoff/verify') {
                return jsonResponse({ verified: true, status: 'verified', namespace: 'llmd-bootstrapped-1' });
            }
            if (url === '/api/agentic-deployments/run-plan-1/approve') {
                return jsonResponse({
                    ...planRun,
                    configuration_plan: {
                        ...planRun.configuration_plan,
                        validation: {
                            ...planRun.configuration_plan.validation,
                            preparedDeployment: {
                                id: 'prep-fail-1',
                                digest: 'sha256:prep-fail-1',
                                preflight: {
                                    status: 'fail',
                                    errors: [{ code: 'REQUIRED_CRD_UNAVAILABLE', message: 'DisaggregatedSet CRD is not installed' }],
                                    unmodeled: [],
                                },
                                cases: [{ namespace: 'llmd-bootstrapped-1' }],
                            },
                        },
                    },
                });
            }
            throw new Error(`Unexpected fetch: ${url}`);
        },
        async ({ root, findButton, clickButton, alerts }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ can: () => true }}>
                        <DeploymentWorkflow draft={workflowDraft} mode="manual" embedded />
                    </AuthContext.Provider>,
                );
            });
            await clickButton(/Review & deploy/);
            await clickButton(/^Confirm deployment$/);
            await clickButton(/Re-check & Deploy/);

            assert.equal(findButton(/Re-check & Deploy/), null);
            assert.equal(Boolean(reactProps(findButton(/^Confirm deployment$/)).disabled), true);
            assert.equal(Boolean(reactProps(findButton(/Back to edit/)).disabled), false);
            assert.ok(alerts().some((text) => text.includes('DisaggregatedSet CRD is not installed')));
        },
    );
});

test('DeploymentPermissionHandoff renders optional Dismiss button only when onDismiss is provided', () => {
    const withoutDismiss = renderToStaticMarkup(
        <DeploymentPermissionHandoff mode="bootstrap" namespace="llmd-test" onPrimaryAction={() => {}} />,
    );
    assert.doesNotMatch(withoutDismiss, />Dismiss<\/button>/);

    const withDismiss = renderToStaticMarkup(
        <DeploymentPermissionHandoff mode="bootstrap" namespace="llmd-test" onPrimaryAction={() => {}} onDismiss={() => {}} />,
    );
    assert.match(withDismiss, />Dismiss<\/button>/);
});

test('DeploymentWorkflow step-2 auto-confirm forwards frozenNamespace and preserves preparedPlanId on 403 deployment_admin_setup_required', async (t) => {
    const planRun = makeRestoredPlanRun();
    const approvePayloads = [];
    const remediationPayloads = [];
    const verifyPayloads = [];
    const startedRuns = [];
    let step2Attempts = 0;
    await withWorkflowDom(
        t,
        async (url, options = {}) => {
            if (url === '/api/agentic-deployments/run-plan-1') return jsonResponse(planRun);
            if (url === '/api/v1/deployments/permission-check') {
                return jsonResponse({
                    sufficient: true,
                    status: 'sufficient',
                    namespace: 'llmd-precheck-1',
                });
            }
            if (url === '/api/agentic-deployments/run-plan-1/approve') {
                const payload = JSON.parse(options.body);
                approvePayloads.push(payload);
                if (!payload.prepared_plan_id) {
                    return jsonResponse({
                        ...planRun,
                        configuration_plan: {
                            ...planRun.configuration_plan,
                            validation: {
                                ...planRun.configuration_plan.validation,
                                preparedDeployment: {
                                    id: 'prep-frozen-pass',
                                    digest: 'sha256:prep-frozen-pass',
                                    preflight: { status: 'pass', errors: [], unmodeled: [] },
                                    cases: [{ namespace: 'llmd-frozen-pass' }],
                                },
                            },
                        },
                    });
                }
                step2Attempts += 1;
                if (step2Attempts === 1) {
                    return jsonResponse({
                        code: 'deployment_admin_setup_required',
                        detail: 'Orchestration administrator setup required.',
                        identity: { kind: 'ServiceAccount', name: 'lens-edit', namespace: 'lens-edit', overrideRequired: false },
                        adminOperations: [{ id: 'ns.create', title: 'Create namespace' }],
                    }, 403);
                }
                return jsonResponse({ ...planRun, status: 'deploying', deployment_run_id: 'exec-frozen-pass' }, 202);
            }
            if (url === '/api/v1/deployments/permission-remediation') {
                remediationPayloads.push(JSON.parse(options.body));
                return jsonResponse({
                    script: '#!/usr/bin/env bash\nkubectl apply -f -',
                    namespace: 'llmd-frozen-pass',
                    bootstrapDigest: 'digest-frozen-pass',
                });
            }
            if (url === '/api/v1/deployments/admin-handoff/verify') {
                verifyPayloads.push(JSON.parse(options.body));
                return jsonResponse({
                    verified: true,
                    status: 'verified',
                    namespace: 'llmd-frozen-pass',
                });
            }
            throw new Error(`Unexpected fetch: ${url}`);
        },
        async ({ root, findButton, clickButton }) => {
            await act(async () => {
                root.render(
                    <AuthContext.Provider value={{ can: () => true }}>
                        <DeploymentWorkflow
                            draft={workflowDraft}
                            mode="manual"
                            embedded
                            onDeploymentStarted={(result) => startedRuns.push(result)}
                        />
                    </AuthContext.Provider>,
                );
            });
            await clickButton(/Review & deploy/);
            await clickButton(/^Confirm deployment$/);

            assert.equal(approvePayloads.length, 2);
            assert.equal(approvePayloads[1].namespace, 'llmd-frozen-pass');
            assert.equal(approvePayloads[1].prepared_plan_id, 'prep-frozen-pass');
            assert.equal(remediationPayloads.length, 1);
            assert.equal(remediationPayloads[0].prepared_plan_id, 'prep-frozen-pass');
            assert.equal(remediationPayloads[0].namespace, 'llmd-frozen-pass');
            assert.ok(findButton(/Re-check & Deploy/));

            await clickButton(/Re-check & Deploy/);

            assert.equal(verifyPayloads.length, 1);
            assert.equal(verifyPayloads[0].prepared_plan_id, 'prep-frozen-pass');
            assert.equal(verifyPayloads[0].namespace, 'llmd-frozen-pass');
            assert.equal(approvePayloads.length, 3);
            assert.equal(approvePayloads[2].prepared_plan_id, 'prep-frozen-pass');
            assert.equal(startedRuns.length, 1);
            assert.equal(startedRuns[0].deployment_run_id, 'exec-frozen-pass');
        },
    );
});

test('DeploymentSummary disables Recheck resources while adminHandoff is generating or verifying', () => {
    for (const flag of [{ generating: true }, { verifying: true }]) {
        const html = renderToStaticMarkup(
            <DeploymentSummary
                run={summaryRun()}
                draft={summaryDraft}
                adminHandoff={{
                    namespace: 'llmd-test',
                    report: { status: 'admin_required', adminOperations: [{ id: 'ns.create', title: 'Create namespace' }] },
                    ...flag,
                }}
            />,
        );
        assert.match(html, /<button[^>]*disabled=""[^>]*>Recheck resources<\/button>/);
    }
});
