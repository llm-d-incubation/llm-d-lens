import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EvaluationConfigurationEditor from './EvaluationConfigurationEditor.jsx';
import { RuntimeVerificationResults } from './OptimizationEvaluationDetails.jsx';
import { DisaggregatedSetSettings } from '../../optimization/ui/OptimizationConfiguration.jsx';
import { DeploymentPlanReview, SliceDeploymentPreview } from './EvaluationTaskWizard.jsx';

test('deployment checks stay inline and preserve preflight execution gates', () => {
    for (const [status, acknowledged, blocked] of [['pass', false, false], ['fail', true, true], ['unknown', false, true], ['unknown', true, false]]) {
        const html = renderToStaticMarkup(createElement(DeploymentPlanReview, {
            preparedReview: { plan: { digest: 'sha256:plan', cases: [], preflight: { status, errors: [], unmodeled: ['Unverified topology'] } } },
            acknowledgeUnknown: acknowledged,
            setAcknowledgeUnknown() {}, onClose() {}, onConfirm() {},
        }));
        assert.match(html, /<section aria-label="Deployment checks"/);
        assert.doesNotMatch(html, /role="dialog"|aria-modal|Review deployment plan|Deploy reviewed plan/);
        assert.match(html, /<details><summary[^>]*>Deployment resources/);
        const deployButton = [...html.matchAll(/<button\b[^]*?<\/button>/g)].map(match => match[0]).find(button => button.includes('Deploy</button>'));
        assert.ok(deployButton);
        assert.equal(deployButton.includes('disabled=""'), blocked);
        assert.equal(html.includes('type="checkbox"'), status === 'unknown');
    }
    assert.equal(renderToStaticMarkup(createElement(DeploymentPlanReview, {})), '');
});

test('runtime evidence keeps HTTP success separate from unverified KV transfer', () => {
    const html = renderToStaticMarkup(createElement(RuntimeVerificationResults, {
        report: {
            kubernetes: { status: 'pass', reasons: [] },
            placement: { status: 'fail', reasons: ['Slice spans topology domains'] },
            inference: { status: 'pass' },
            kvTransfer: { status: 'unknown', reasons: ['No request-correlated transfer evidence'] },
        },
    }));
    assert.match(html, /Kubernetes readiness/);
    assert.match(html, /Slice spans topology domains/);
    assert.match(html, /P\/D routing/);
    assert.match(html, /KV transfer/);
    assert.match(html, /No request-correlated transfer evidence/);
    assert.equal((html.match(/>Passed</g) || []).length, 1);
    assert.equal((html.match(/>Failed</g) || []).length, 1);
    assert.equal((html.match(/>Unknown</g) || []).length, 2);
    assert.equal(renderToStaticMarkup(createElement(RuntimeVerificationResults, {})), '');
});

for (const status of ['idle', 'checking', 'ready', 'failed']) {
    test(`Slice controls remain visible but disabled when source support is ${status}`, () => {
        const html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, {
            available: false,
            preparation: { status, error: 'Guide preparation failed' },
            canRetry: true,
            onRetry() {},
            disaggregatedSet: {},
            setDisaggregatedSet() {},
        }));
        assert.match(html, /Slice Configuration/);
        assert.match(html, /<fieldset disabled=""/);
        assert.match(html, /aria-label="spec.slices"/);
        assert.match(html, /Recheck Slice support/);
        if (status === 'ready') assert.match(html, /This Guide renders no DisaggregatedSet/);
        if (status === 'checking') assert.match(html, /Checking Guide Slice support/);
        if (status === 'failed') assert.match(html, /role="alert"[^>]*>Guide preparation failed/);
    });
}

test('confirmed DS support enables Slice values and per-Slice resource totals', () => {
    const html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, {
        available: true,
        preparation: { status: 'ready', error: '' },
        canRetry: true,
        onRetry() {},
        disaggregatedSet: { slices: 3, placementPolicy: { type: 'ExclusiveSlice', topology: 'example.com/fabric' }, sliceRouting: 'prefer' },
        setDisaggregatedSet() {},
        perSlice: { prefillReplicas: 1, prefillTp: 1, decodeReplicas: 1, decodeTp: 1 },
        workloadSummary: { podCount: 6, resourceRequests: { 'example.com/device': 6 } },
    }));
    assert.doesNotMatch(html, /<fieldset disabled/);
    assert.match(html, /value="3"/);
    assert.match(html, /value="example.com\/fabric"/);
    assert.match(html, /aria-pressed="true"[^>]*>Scorer/);
    assert.match(html, /6 accelerators/);
    assert.match(html, /1 Slice = 1P×TP1 \+ 1D×TP1 · 2 accelerators/);
    assert.match(html, /6 Pods in total/);
    assert.match(html, /No Evaluation data yet/);
    assert.doesNotMatch(html, /aria-label="Target load"/);
    assert.match(html, /Cluster inventory unavailable; key not validated/);
});

const capacity = {
    podsPerSlice: 2,
    sliceRequests: { 'example.com/device': 2, cpu: 4 },
    acceleratorResources: ['example.com/device'],
    maxSlices: { None: 3 },
    schedulableNodes: 3,
    topologyKeys: [{
        key: 'topology.kubernetes.io/zone', wellKnown: true, labeledNodes: 3, domainCount: 3, feasibleDomains: 2, exclusiveTopologyDomains: 1,
        domains: { a: 1, b: 1, c: 1 },
        domainDetails: [
            { value: 'a', nodeCount: 1, freeResources: { 'example.com/device': 2 }, fitsSlice: true, occupied: false },
            { value: 'b', nodeCount: 1, freeResources: { 'example.com/device': 2 }, fitsSlice: true, occupied: true },
            { value: 'c', nodeCount: 1, freeResources: { 'example.com/device': 0 }, fitsSlice: false, occupied: false },
        ],
    }],
};
const recommendation = {
    source: 'capacity',
    guideDefault: { slices: 1, placementPolicy: { type: 'None' } },
    maxSlices: 2,
    placementPolicy: { type: 'ExclusiveSlice', topology: 'topology.kubernetes.io/zone' },
    reasons: [],
    topologyKeys: capacity.topologyKeys,
    capacity,
    history: [],
};

test('placement drives the Slice ceiling, domain cards and a discovery suggestion', () => {
    const props = { available: true, preparation: { status: 'ready', error: '' }, canRetry: false, setDisaggregatedSet() {}, recommendation, capacity };
    let html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, { ...props, disaggregatedSet: {} }));
    assert.match(html, /Max feasible<\/p><p[^>]*>3</);
    assert.match(html, /None · Guide default roles/);
    assert.match(html, /Suggested: ExclusiveSlice on topology.kubernetes.io\/zone/);
    assert.match(html, /ExclusiveSlice · 2/);
    assert.match(html, /ExclusiveTopology · 1/);
    html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, { ...props, capacityBasis: 'plan', disaggregatedSet: { slices: 3, placementPolicy: recommendation.placementPolicy } }));
    assert.match(html, /Max feasible<\/p><p[^>]*>2</);
    assert.match(html, /generated roles/);
    assert.match(html, /3 Slices exceed the estimated maximum of 2/);
    assert.match(html, /data-topology-domains/);
    assert.match(html, /Well-known/);
    assert.match(html, /a · 1n · 2 free · Fits a Slice/);
    assert.match(html, /Too small/);
    assert.match(html, /6 accelerators · 6 Pods/);
    assert.doesNotMatch(html, /Suggested:/);
    html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, { ...props, disaggregatedSet: { placementPolicy: { type: 'ExclusiveTopology', topology: 'topology.kubernetes.io/zone' } } }));
    assert.match(html, /In use/);
    const dra = {
        ...capacity,
        acceleratorResources: [],
        unmodeled: ['DRA'],
        topologyKeys: [{ ...capacity.topologyKeys[0], domainDetails: [{ value: 'a', nodeCount: 1, freeResources: { cpu: 8, memory: 2 ** 34 }, fitsSlice: true, occupied: false }] }],
    };
    html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, { ...props, capacity: dra, disaggregatedSet: { placementPolicy: recommendation.placementPolicy } }));
    assert.match(html, /a · 1n · 8 CPU · 16 GiB free · Fits a Slice/);
    assert.match(html, /requested through DRA claims/);
    assert.doesNotMatch(html, /0 free/);
});

test('history is evidence: same-shape runs are marked and none is labeled best by throughput', () => {
    const history = [
        { caseId: 'case-1', finishedAt: '2026-11-10T00:00:00Z', slices: 2, placementPolicy: { type: 'ExclusiveSlice', topology: 'zone' }, prefillReplicas: 1, prefillTensorParallelSize: 2, decodeReplicas: 2, decodeTensorParallelSize: 2, accelerators: 12, maximumStableQps: 9, throughputTps: 123.5 },
        { caseId: 'case-2', slices: 1, prefillReplicas: 3, prefillTensorParallelSize: 1, decodeReplicas: 1, decodeTensorParallelSize: 1, throughputTps: 50 },
    ];
    const html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, {
        available: true,
        preparation: { status: 'ready', error: '' },
        canRetry: false,
        disaggregatedSet: { slices: 4 },
        setDisaggregatedSet() {},
        recommendation: { ...recommendation, history },
        capacity,
        perSlice: { prefillReplicas: 1, prefillTp: 2, decodeReplicas: 2, decodeTp: 2 },
        onApplyHistory() {},
    }));
    assert.match(html, /data-history-recommendations/);
    assert.match(html, /123.5/);
    assert.doesNotMatch(html, />best</);
    assert.match(html, /1×2 \/ 2×2/);
    assert.match(html, /Same shape/);
    assert.match(html, /Reference/);
    assert.match(html, /Evaluation evidence \(2\)/);
    assert.match(html, /1 matching run\(s\); enter a target/);
    assert.match(html, /aria-label="Target load"/);
});

test('Slice sweep offers feasible candidates, flags measured counts and selects a workload/SLO evidence profile', () => {
    const history = [{ caseId: 'h', finishedAt: '2026-11-10', slices: 1, prefillReplicas: 1, prefillTensorParallelSize: 2, decodeReplicas: 1, decodeTensorParallelSize: 3, accelerators: 5, maximumStableQps: 5, evidenceKey: 'k', workloadLabel: 'shared-prefix 4x2', slaTargets: { ttft_ms: 500 } }];
    const props = {
        available: true, preparation: { status: 'ready', error: '' }, canRetry: false, disaggregatedSet: {}, setDisaggregatedSet() {},
        recommendation: { ...recommendation, history }, capacity, perSlice: { prefillReplicas: 1, prefillTp: 2, decodeReplicas: 1, decodeTp: 3 },
    };
    assert.doesNotMatch(renderToStaticMarkup(createElement(DisaggregatedSetSettings, props)), /Enable Slice Sweep/);
    let html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, { ...props, sweep: { enabled: false, counts: [] }, onSweepChange() {} }));
    assert.match(html, /Enable Slice Sweep/);
    assert.doesNotMatch(html, /data-slice-sweep/);
    assert.match(html, /aria-label="Evidence workload and SLO"/);
    assert.match(html, /shared-prefix 4x2 · TTFT ≤ 500 ms/);
    html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, { ...props, sweep: { enabled: true, counts: [1, 2, 3] }, onSweepChange() {} }));
    assert.match(html, /data-slice-sweep/);
    assert.match(html, /aria-label="Sweep from"[^>]*max="3"[^>]*value="1"/);
    assert.match(html, /aria-label="Sweep to"[^>]*value="3"/);
    assert.match(html, /1 Slice · 5 accelerators · measured before/);
    assert.match(html, /2 Slices · 10 accelerators/);
    assert.match(html, /3 configurations, deployed and benchmarked one after another/);
    assert.match(html, /aria-label="spec.slices"[^>]*max="3"/);
});

test('deployment review previews Slices, resources, modeled domains and blocks infeasible placement', () => {
    const preview = {
        caseIndex: 0, workload: 'pd', slices: 2, maxFeasibleSlices: 2,
        placementPolicy: { type: 'ExclusiveSlice', topology: 'zone' }, sliceRouting: 'prefer',
        roles: [{ name: 'prefill', replicas: 1, groupSize: 1 }, { name: 'decode', replicas: 1, groupSize: 1 }],
        sliceRequests: { 'example.com/device': 5 }, acceleratorResources: ['example.com/device'],
        placement: [
            { slice: 0, pods: [{ role: 'prefill', group: 0, rank: 0, node: 'a1', domain: 'a' }, { role: 'decode', group: 0, rank: 0, node: 'a2', domain: 'a' }] },
            { slice: 1, pods: [{ role: 'prefill', group: 0, rank: 0, node: 'b1', domain: 'b' }] },
        ],
    };
    let html = renderToStaticMarkup(createElement(SliceDeploymentPreview, { preview, preflight: { status: 'unknown', modeledConstraints: 'pass', errors: [] } }));
    assert.match(html, /Selected 2 \/ Max feasible 2/);
    assert.match(html, /1 prefill \+ 1 decode/);
    assert.match(html, /10 requested \(5 per Slice\)/);
    assert.match(html, /ExclusiveSlice · zone/);
    assert.match(html, /EPP Topology Affinity/);
    assert.match(html, />Scorer</);
    assert.match(html, /Slice 0 → a/);
    assert.match(html, /decode 0 → a2/);
    assert.match(html, /Pass · unverified constraints/);
    html = renderToStaticMarkup(createElement(SliceDeploymentPreview, { preview: { ...preview, slices: 3, placement: [] }, preflight: { status: 'fail', errors: [{ code: 'NO_FEASIBLE_PLACEMENT' }] } }));
    assert.match(html, />Fail</);
    assert.match(html, /fits 2 Slice\(s\)/);
    assert.match(html, /No modeled placement/);
});

for (const [mode, label] of [[undefined, 'Scorer'], ['inherit', 'Scorer'], ['prefer', 'Scorer'], ['filter', 'Filter']]) {
    test(`EPP Topology Affinity edits ${mode ?? 'unset'} as ${label} and preserves the recorded preview`, () => {
        const html = renderToStaticMarkup(createElement(DisaggregatedSetSettings, {
            available: true,
            preparation: { status: 'ready', error: '' },
            disaggregatedSet: { sliceRouting: mode },
            setDisaggregatedSet() {},
        }));
        assert.match(html, /EPP Topology Affinity/);
        const controls = html.match(/data-configuration-field="disaggregatedSet.sliceRouting"[^]*?<\/div>/)?.[0];
        assert.ok(controls);
        assert.match(controls, new RegExp(`aria-pressed="true"[^>]*>${label}<`));
        assert.match(controls, />Filter</);
        assert.doesNotMatch(controls, /disabled|Require|unsupported|Default/);
        assert.equal((controls.match(/<button\b/g) || []).length, 2);
        const activeButton = controls.match(/<button[^>]*aria-pressed="true"[^]*?<\/button>/)?.[0];
        const inactiveButton = controls.match(/<button[^>]*aria-pressed="false"[^]*?<\/button>/)?.[0];
        assert.match(activeButton, /ring-2 ring-inset ring-\[var\(--brand-accent\)\]/);
        assert.match(activeButton, /lucide-check/);
        assert.doesNotMatch(activeButton, /invisible/);
        assert.match(inactiveButton, /lucide-check[^>]*invisible/);
        assert.doesNotMatch(inactiveButton, /ring-inset/);
        const preview = renderToStaticMarkup(createElement(SliceDeploymentPreview, {
            preview: { slices: 1, roles: [], sliceRequests: {}, acceleratorResources: [], placement: [], sliceRouting: mode },
            preflight: { status: 'pass', errors: [] },
        }));
        assert.match(preview, /EPP Topology Affinity/);
        assert.match(preview, new RegExp(`>${mode === undefined || mode === 'inherit' ? 'Default' : label}<`));
    });
}

test('configuration editor renders after environment setup with auto-cache selected', (t) => {
    const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        value: { getItem: () => null, removeItem() {} },
    });
    t.after(() => {
        if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage);
        else delete globalThis.sessionStorage;
    });
    const html = renderToStaticMarkup(createElement(EvaluationConfigurationEditor, {
        sharedContext: {
            model: 'Qwen/Qwen3-0.6B',
            modelSource: 'auto-cache',
            cluster: { id: 'test-cluster', name: 'Test cluster', sessionId: 'test-session' },
            modelServer: 'vllm',
            image: 'runtime:v1',
            storageVolumeId: 'test-volume',
            cacheValidation: { state: 'ready' },
            replicas: 3,
            tensorParallelSize: 2,
            maxModelLen: 8192,
        },
    }));

    assert.match(html, /Qwen\/Qwen3-0.6B/);
    assert.match(html, /Test cluster/);
    assert.match(html, /Generate YAML/);
    assert.match(html, /value="3"/);
    assert.match(html, /value="2"/);
    assert.doesNotMatch(html, /Use AIC for PD|AIC P\/D recommendation|Apply P:D ratios/);
});

for (const entrySource of ['model-market', 'evaluation-setup']) {
    test(`${entrySource} retains the full editor layout with source-specific comparisons`, (testContext) => {
        const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
        Object.defineProperty(globalThis, 'sessionStorage', {
            configurable: true,
            value: { getItem: () => null, removeItem() {} },
        });
        testContext.after(() => {
            if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage);
            else delete globalThis.sessionStorage;
        });
        const html = renderToStaticMarkup(createElement(EvaluationConfigurationEditor, {
            sharedContext: {
                entrySource,
                model: 'test/model',
                cluster: { id: 'cluster', name: 'Test cluster', session_id: 'session' },
                modelServer: 'vllm',
                image: 'runtime:v1',
                replicas: 3,
                tensorParallelSize: 2,
            },
            deployOnPublish: entrySource === 'model-market',
            initialOptimizationSelection: ['direct-vllm'],
            onPublished() {},
            onEditSetup() {},
        }));
        assert.match(html, /id="configuration-cluster"/);
        assert.match(html, /id="configuration-model"/);
        assert.match(html, /data-configuration-field="replicaVariants"/);
        assert.match(html, /data-configuration-field="tensorParallelVariants"/);
        assert.match(html, /value="3"/);
        assert.match(html, /value="2"/);
        assert.match(html, /aria-label="Edit setup"/);
        assert.match(html, /Generate YAML/);
        assert.match(html, /Configuration source/);
        assert.doesNotMatch(html, /Generate complete plan|Deployment requirements and plan/);
        if (entrySource === 'model-market') assert.doesNotMatch(html, /id="optimizationSelection"/);
        else {
            assert.match(html, /id="optimizationSelection"/);
            assert.match(html, /aria-pressed="true" aria-label="Compare Direct vLLM:/);
            assert.match(html, /1 scenarios selected/);
        }
    });
}
