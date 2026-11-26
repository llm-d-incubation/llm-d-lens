import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { AuthContext } from '../../auth/AuthContext.js';
import { EVALUATION_TRANSFER_KEYS } from '../model/transfer.js';
import EvaluationTaskWizard, { DeploymentPlanReview } from './EvaluationTaskWizard.jsx';

function renderWizard(t, intent, can = () => true) {
    const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        value: {
            getItem: (key) => key === EVALUATION_TRANSFER_KEYS.INTENT ? JSON.stringify(intent) : null,
            removeItem() {},
        },
    });
    t.after(() => {
        if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage);
        else delete globalThis.sessionStorage;
    });
    return renderToStaticMarkup(createElement(AuthContext.Provider, { value: { can } },
        createElement(EvaluationTaskWizard, { onNavigate() {} })));
}

function configurationStep(html) {
    const progress = html.match(/<ol\b[^>]*aria-label="Task creation progress"[^>]*>(.*?)<\/ol>/s);
    assert.ok(progress);
    return [...progress[1].matchAll(/<button\b([^>]*)>(.*?)<\/button>/gs)]
        .find((match) => match[2].includes('Configurations'))[1];
}

test('Model Market handoff starts the shared configuration step with a model-card return', (t) => {
    const html = renderWizard(t, {
        entrySource: 'model-market',
        operation: 'create-evaluation',
        initial_step: 'configurations',
        name: 'market-benchmark',
        deployment_name: 'market-serving',
        cluster_id: 'test-cluster',
        workloads: [{ model: 'Qwen/Qwen3-8B', replicas: 3, tensor_parallel_size: 2 }],
        runtime: { max_model_len: 8192 },
    });
    assert.match(html, /New Benchmark Task/);
    assert.doesNotMatch(configurationStep(html), /disabled=/);
    assert.match(configurationStep(html), /ring-cyan-400\/25/);
    assert.match(html, /Back to model card/);
    assert.doesNotMatch(html, /Continue to Benchmark|Execution plan/);
});

test('ordinary benchmark creation still starts at Evaluation Setup', (t) => {
    const html = renderWizard(t, null);
    assert.match(html, /New Benchmark Task/);
    assert.match(configurationStep(html), /disabled=/);
});

test('evaluation configuration links open the shared editor and retain benchmark steps', (testContext) => {
    const html = renderWizard(testContext, {
        entrySource: 'evaluation-setup',
        operation: 'create-evaluation',
        initial_step: 'configurations',
        return_target: 'optimization-evaluate-new',
        name: 'existing-evaluation',
        selected_artifact_ids: ['existing-configuration'],
    });
    assert.match(configurationStep(html), /ring-cyan-400\/25/);
    assert.match(html, /Continue to Benchmark/);
    assert.match(html, /Execution plan/);
    assert.doesNotMatch(html, /Back to model card/);
});

test('explicit Model Market source uses the shared configuration wizard, not the legacy screen', (testContext) => {
    const html = renderWizard(testContext, { entrySource: 'model-market', operation: 'deploy-only' });
    assert.match(html, /New Benchmark Task/);
    assert.match(configurationStep(html), /ring-cyan-400\/25/);
    assert.doesNotMatch(html, /Configure deployment/);
    assert.match(html, /Back to model card/);
    assert.doesNotMatch(html, /Execution plan/);
});

test('explicit Evaluation source overrides legacy operation and starts at Setup', (testContext) => {
    const html = renderWizard(testContext, { entrySource: 'evaluation-setup', operation: 'deploy-only' });
    assert.match(html, /New Benchmark Task/);
    assert.match(configurationStep(html), /disabled=/);
    assert.doesNotMatch(html, /Configure deployment/);
    assert.doesNotMatch(html, /Back to model card/);
    assert.match(html, /Execution plan/);
});

test('configuration handoff does not bypass benchmark-only permissions', (t) => {
    const html = renderWizard(t, {
        entrySource: 'model-market', operation: 'create-evaluation', initial_step: 'configurations',
    }, (permission) => permission === 'evaluate:run:create');
    assert.match(configurationStep(html), /disabled=/);
});

test('legacy deploy-only handoffs retain their deployment screen', (t) => {
    const html = renderWizard(t, { operation: 'deploy-only' });
    assert.match(html, /Configure deployment/);
    assert.doesNotMatch(html, /New Benchmark Task|Task creation progress/);
});

test('EvaluationTaskWizard DeploymentPlanReview renders Orchestration administrator handoff and disables Deploy when adminHandoff is active', () => {
    const html = renderToStaticMarkup(createElement(DeploymentPlanReview, {
        preparedReview: {
            plan: {
                id: 'plan-1',
                digest: 'sha256:plan-1',
                cases: [{ namespace: 'llmd-ds-1', manifest: 'kind: DisaggregatedSet' }],
                preflight: { status: 'pass', errors: [], unmodeled: [] },
            },
        },
        acknowledgeUnknown: false,
        setAcknowledgeUnknown() {},
        onClose() {},
        onConfirm() {},
        adminHandoff: {
            namespace: 'llmd-ds-1',
            report: { status: 'admin_required', adminOperations: [{ id: 'ns.create', title: 'Create namespace' }] },
            script: '#!/usr/bin/env bash\nkubectl apply -f -',
            identity: { kind: 'ServiceAccount', name: 'lens-edit', namespace: 'lens-edit', overrideRequired: false },
        },
        onVerifyBootstrap() {},
        onRegenerateBootstrap() {},
        onDismissAdminHandoff() {},
    }));
    assert.match(html, /Orchestration administrator namespace setup/);
    assert.match(html, /Re-check &amp; Deploy/);
    assert.match(html, />Dismiss<\/button>/);
    const deployButton = [...html.matchAll(/<button\b[^]*?<\/button>/g)].map((m) => m[0]).find((b) => b.includes('Deploy</button>') && !b.includes('Re-check'));
    assert.ok(deployButton);
    assert.match(deployButton, /disabled=""/);
});

function createWizardTestDom() {
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
            if (this.nodeType === 3) { this.nodeValue = text; return; }
            this.childNodes = text ? [doc.createTextNode(text)] : [];
            for (const child of this.childNodes) child.parentNode = this;
        },
        addEventListener() {},
        removeEventListener() {},
        setAttribute(key, value) { this.attributes[key] = String(value); },
        removeAttribute(key) { delete this.attributes[key]; },
        getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; },
        hasAttribute(key) { return key in this.attributes; },
        scrollIntoView() {},
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
            if (index !== -1) { this.childNodes.splice(index, 1); child.parentNode = null; }
            return child;
        },
        get firstChild() { return this.childNodes[0] || null; },
        get options() { return findDomElements(this, (el) => el.tagName === 'OPTION'); },
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
        setTimeout,
        clearTimeout,
        scrollTo() {},
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
        createElementNS(ns, tag) { const node = makeNode(tag, doc); node.namespaceURI = ns; return node; },
        createTextNode(text) { const node = makeNode('#text', doc); node.nodeValue = String(text); return node; },
        createComment(text) { const node = makeNode('#comment', doc); node.nodeValue = String(text); return node; },
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

function findDomElements(root, predicate, acc = []) {
    if (!root || root.nodeType !== 1) return acc;
    if (predicate(root)) acc.push(root);
    for (const child of root.childNodes) findDomElements(child, predicate, acc);
    return acc;
}

function domReactProps(element) {
    const key = Object.keys(element).find((name) => name.startsWith('__reactProps$'));
    return key ? element[key] : {};
}

function findFiberOnPublished(node) {
    const fiberKey = Object.keys(node).find((name) => name.startsWith('__reactFiber$'));
    if (!fiberKey) return null;
    const queue = [node[fiberKey]];
    const seen = new Set();
    while (queue.length) {
        const fiber = queue.shift();
        if (!fiber || seen.has(fiber)) continue;
        seen.add(fiber);
        if (typeof fiber.memoizedProps?.onPublished === 'function') return fiber.memoizedProps.onPublished;
        if (fiber.child) queue.push(fiber.child);
        if (fiber.sibling) queue.push(fiber.sibling);
    }
    return null;
}

async function withWizardDom(t, intent, fetchHandler, runScenario) {
    const prevAct = globalThis.IS_REACT_ACT_ENVIRONMENT;
    const prevDoc = globalThis.document;
    const prevWin = globalThis.window;
    const prevHtml = globalThis.HTMLElement;
    const prevSvg = globalThis.SVGElement;
    const prevIframe = globalThis.HTMLIFrameElement;
    const prevFetch = globalThis.fetch;
    const prevStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    const store = new Map([[EVALUATION_TRANSFER_KEYS.INTENT, JSON.stringify(intent)]]);
    const { doc, win } = createWizardTestDom();
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
    const findButton = (labelPattern) => findDomElements(
        doc.body,
        (el) => el.tagName === 'BUTTON' && labelPattern.test(el.textContent.trim()),
    )[0] || null;
    const clickButton = async (labelPattern) => {
        const button = findButton(labelPattern);
        assert.ok(button, `Expected button matching ${labelPattern}`);
        const props = domReactProps(button);
        assert.equal(Boolean(props.disabled), false, `Expected button ${labelPattern} to be enabled`);
        await act(async () => {
            await props.onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
    };
    await runScenario({ root, doc, findButton, clickButton });
}

function wizardJson(body, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

function baseWizardFetch(url) {
    if (url.startsWith('/api/cluster/clusters')) {
        return wizardJson({
            items: [{ id: 'cluster-a', name: 'Cluster A', ready: true, llmDRepoPath: '/opt/llm-d', llmDRef: 'v1' }],
        });
    }
    if (url.startsWith('/api/cluster/session')) {
        return wizardJson({ sessionId: 'sess-1', cluster: { id: 'cluster-a', name: 'Cluster A' } });
    }
    if (url.startsWith('/api/cluster/overview')) {
        return wizardJson({ kubernetes: { hardware: { kind: 'gpu' }, nodes: [] } });
    }
    if (url.startsWith('/api/guide-planning/catalog')) {
        return wizardJson({ guides: [{ id: 'pd-disaggregation', label: 'P/D Disaggregation' }] });
    }
    if (url.startsWith('/api/v1/configurations/capabilities')) {
        return wizardJson({ providers: [{ id: 'pd-disaggregation', label: 'P/D Disaggregation' }] });
    }
    if (url.startsWith('/api/v1/configurations/artifacts')) return wizardJson([]);
    if (url.startsWith('/api/v1/model-service/models')) return wizardJson({ items: [] });
    if (url.startsWith('/api/v1/storage/volumes')) return wizardJson({ items: [] });
    if (url.startsWith('/api/v1/hardware/profiles')) return wizardJson({ items: [] });
    if (url.startsWith('/api/v1/deployments/node-labels')) return wizardJson({ items: [] });
    return null;
}

const dsArtifact = {
    artifact_id: 'art-ds-1',
    deployable_configuration: {
        provider_ref: 'pd-disaggregation',
        content: {
            model: { name: 'Qwen/Qwen3-8B' },
            runtime: { storageVolumeId: 'vol-cache-1' },
            disaggregatedSet: { slices: 2 },
            officialGuide: { renderedManifest: 'kind: DisaggregatedSet\nmetadata:\n  name: ds-test\n' },
        },
        provenance: { placement: { replicas: 'prefer-same-node' } },
    },
};

test('EvaluationTaskWizard catches 403 deployment_admin_setup_required on executePreparedDeployment, renders Orchestration administrator script, and deploys after verify', async (t) => {
    const remediationPayloads = [];
    const verifyPayloads = [];
    const runPayloads = [];
    const navigated = [];
    let runAttempts = 0;

    await withWizardDom(
        t,
        {
            entrySource: 'model-market',
            operation: 'create-evaluation',
            initial_step: 'configurations',
            name: 'ds-deploy',
            deployment_name: 'qwen-pd',
            cluster_id: 'cluster-a',
            workloads: [{ model: 'Qwen/Qwen3-8B', guide: 'pd-disaggregation', replicas: 1, tensor_parallel_size: 1 }],
        },
        async (url, options = {}) => {
            const base = baseWizardFetch(url);
            if (base) return base;
            if (url === '/api/v1/deployments/plans') {
                return wizardJson({
                    id: 'plan-ds-1',
                    digest: 'sha256:plan-ds-1',
                    preflight: { status: 'pass', errors: [], unmodeled: [] },
                    cases: [{ namespace: 'llmd-ds-1', manifest: 'kind: DisaggregatedSet\nmetadata:\n  name: ds-test\n' }],
                });
            }
            if (url === '/api/v1/deployments/runs') {
                runAttempts += 1;
                runPayloads.push(JSON.parse(options.body));
                if (runAttempts === 1) {
                    return wizardJson({
                        code: 'deployment_admin_setup_required',
                        detail: 'Orchestration administrator setup is required before deploying.',
                        namespace: 'llmd-ds-1',
                        identity: { kind: 'ServiceAccount', name: 'lens-edit', namespace: 'lens-edit', overrideRequired: false },
                        adminOperations: [{ id: 'ns.create', title: 'Create namespace and RBAC' }],
                    }, 403);
                }
                return wizardJson({ id: 'deploy-run-1', status: 'queued' }, 202);
            }
            if (url === '/api/v1/deployments/permission-remediation') {
                remediationPayloads.push(JSON.parse(options.body));
                return wizardJson({
                    script: '#!/usr/bin/env bash\nkubectl apply -f -',
                    namespace: 'llmd-ds-1',
                    bootstrapDigest: 'digest-ds-1',
                });
            }
            if (url === '/api/v1/deployments/admin-handoff/verify') {
                verifyPayloads.push(JSON.parse(options.body));
                return wizardJson({
                    verified: true,
                    status: 'verified',
                    namespace: 'llmd-ds-1',
                });
            }
            return wizardJson({});
        },
        async ({ root, doc, findButton, clickButton }) => {
            await act(async () => {
                root.render(
                    createElement(
                        AuthContext.Provider,
                        { value: { can: () => true } },
                        createElement(EvaluationTaskWizard, { onNavigate: (view) => navigated.push(view) }),
                    ),
                );
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 25));
            });

            const editorHost = findDomElements(doc.body, (el) => el.hasAttribute('data-evaluation-configuration-editor'))[0];
            assert.ok(editorHost, 'Expected EvaluationConfigurationEditor container');
            const onPublished = findFiberOnPublished(editorHost);
            assert.ok(onPublished, 'Expected onPublished handler on EvaluationConfigurationEditor');

            await act(async () => {
                await onPublished({ artifacts: [dsArtifact] });
            });

            assert.equal(runAttempts, 1);
            assert.equal(remediationPayloads.length, 1);
            assert.equal(remediationPayloads[0].cluster_id, 'cluster-a');
            assert.equal(remediationPayloads[0].namespace, 'llmd-ds-1');
            assert.equal(remediationPayloads[0].prepared_plan_id, 'plan-ds-1');
            assert.equal(remediationPayloads[0].storage_volume_id, 'vol-cache-1');
            assert.match(remediationPayloads[0].manifest, /kind: DisaggregatedSet/);
            assert.match(doc.body.textContent, /Orchestration administrator namespace setup/);
            assert.ok(findButton(/Re-check & Deploy/));

            await clickButton(/Re-check & Deploy/);

            assert.equal(verifyPayloads.length, 1);
            assert.equal(verifyPayloads[0].prepared_plan_id, 'plan-ds-1');
            assert.equal(verifyPayloads[0].expected_digest, 'digest-ds-1');
            assert.equal(runAttempts, 2);
            assert.equal(runPayloads[1].prepared_plan_id, 'plan-ds-1');
            assert.equal(runPayloads[1].expected_plan_digest, 'sha256:plan-ds-1');
            assert.equal(runPayloads[1].acknowledge_unknown, false);
            assert.deepEqual(navigated, ['optimization-deployments']);
        },
    );
});

test('EvaluationTaskWizard preserves acknowledgeUnknown=true across 403 bootstrap handoff and verify retry', async (t) => {
    const runPayloads = [];
    const verifyPayloads = [];
    const navigated = [];
    let runAttempts = 0;

    await withWizardDom(
        t,
        {
            entrySource: 'model-market',
            operation: 'create-evaluation',
            initial_step: 'configurations',
            name: 'ds-unknown',
            deployment_name: 'qwen-pd-unk',
            cluster_id: 'cluster-a',
            workloads: [{ model: 'Qwen/Qwen3-8B', guide: 'pd-disaggregation', replicas: 1, tensor_parallel_size: 1 }],
        },
        async (url, options = {}) => {
            const base = baseWizardFetch(url);
            if (base) return base;
            if (url === '/api/v1/deployments/plans') {
                return wizardJson({
                    id: 'plan-ds-unk',
                    digest: 'sha256:plan-ds-unk',
                    preflight: { status: 'unknown', errors: [], unmodeled: ['RuntimeClass inventory restricted'] },
                    cases: [{ namespace: 'llmd-ds-unk', manifest: 'kind: DisaggregatedSet\nmetadata:\n  name: ds-unk\n' }],
                });
            }
            if (url === '/api/v1/deployments/runs') {
                runAttempts += 1;
                runPayloads.push(JSON.parse(options.body));
                if (runAttempts === 1) {
                    return wizardJson({
                        code: 'deployment_admin_setup_required',
                        detail: 'Orchestration administrator setup required.',
                        identity: { kind: 'ServiceAccount', name: 'lens-edit', namespace: 'lens-edit', overrideRequired: false },
                        script: '#!/usr/bin/env bash\nkubectl apply -f -',
                    }, 403);
                }
                return wizardJson({ id: 'deploy-run-unk', status: 'queued' }, 202);
            }
            if (url === '/api/v1/deployments/admin-handoff/verify') {
                verifyPayloads.push(JSON.parse(options.body));
                return wizardJson({ verified: true, status: 'verified', namespace: 'llmd-ds-unk' });
            }
            return wizardJson({});
        },
        async ({ root, doc, clickButton }) => {
            await act(async () => {
                root.render(
                    createElement(
                        AuthContext.Provider,
                        { value: { can: () => true } },
                        createElement(EvaluationTaskWizard, { onNavigate: (view) => navigated.push(view) }),
                    ),
                );
            });
            await act(async () => {
                await new Promise((resolve) => setTimeout(resolve, 25));
            });

            const editorHost = findDomElements(doc.body, (el) => el.hasAttribute('data-evaluation-configuration-editor'))[0];
            const onPublished = findFiberOnPublished(editorHost);
            await act(async () => {
                await onPublished({ artifacts: [dsArtifact] });
            });

            const checkbox = findDomElements(doc.body, (el) => el.tagName === 'INPUT' && (el.type === 'checkbox' || el.getAttribute('type') === 'checkbox'))[0];
            assert.ok(checkbox, 'Expected unknown acknowledgement checkbox');
            await act(async () => {
                domReactProps(checkbox).onChange({ target: { checked: true } });
            });
            await clickButton(/^Deploy$/);
            assert.equal(runAttempts, 1);

            const handoffCheckbox = findDomElements(doc.body, (el) => el.tagName === 'INPUT' && (el.type === 'checkbox' || el.getAttribute('type') === 'checkbox'))[0];
            assert.equal(Boolean(domReactProps(handoffCheckbox).disabled), true, 'Expected acknowledgeUnknown checkbox to be disabled while adminHandoff is open');

            await clickButton(/Re-check & Deploy/);
            assert.equal(verifyPayloads.length, 1);
            assert.equal(verifyPayloads[0].namespace, 'llmd-ds-unk');
            assert.equal(runAttempts, 2);
            assert.equal(runPayloads[1].acknowledge_unknown, true);
            assert.equal(runPayloads[1].prepared_plan_id, 'plan-ds-unk');
            assert.deepEqual(navigated, ['optimization-deployments']);
        },
    );
});