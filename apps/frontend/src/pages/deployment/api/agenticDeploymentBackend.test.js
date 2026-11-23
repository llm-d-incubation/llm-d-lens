import assert from 'node:assert/strict';
import test from 'node:test';
import { candidateEvidence, defaultRequirements, DEFAULT_REQUIREMENTS, deploymentPlanningRequest, requirementsFromRun, streamAgenticRecommendation, streamAgenticRefinement } from './agenticDeploymentBackend.js';

const draft = { model: 'test/model', clusterId: 'cluster-a', deploymentName: 'test', storageVolumeId: 'cache-a', contextLength: 40960 };

test('restoring a saved plan preserves requirements, unknowns and trial limits', () => {
    for (const values of [
        DEFAULT_REQUIREMENTS,
        { ...DEFAULT_REQUIREMENTS, objective: 'latency', maxAccelerators: '8', ttftMs: '200', tpotMs: '40',
            inputTokens: '1024', outputTokens: '256', requestRate: '0.5', prefixReuse: 'no',
            useCase: 'summarization', requiredFeatures: ['pd_disaggregation'],
            forbiddenFeatures: ['external_kv_cache'], maxCandidates: '5', maxDuration: '20', allowTrial: true },
    ]) {
        const request = deploymentPlanningRequest(draft, values, 'session-a', 'agentic', 'provider-a');
        assert.deepEqual(requirementsFromRun({ request }), values);
    }
});

test('both modes leave workload traits unspecified by default', () => {
    for (const mode of ['manual', 'agentic']) {
        const request = deploymentPlanningRequest(draft, DEFAULT_REQUIREMENTS, 'session-a', mode, 'provider');
        assert.equal(request.planning_mode, mode);
        assert.equal(request.max_model_len, 40960);
        assert.equal(request.planning_facts.workload_profile.mean_input_tokens, null);
        assert.equal(request.planning_facts.workload_profile.mean_output_tokens, null);
        assert.equal(request.planning_facts.workload_profile.shared_prefix_ratio, null);
        assert.equal(request.planning_facts.ttft_slo_ms, null);
        assert.equal(request.requirements.workload.prefixReuse, null);
        assert.equal(request.requirements.workload.prefillHeavy, null);
        assert.equal(request.requirements.objective, 'balanced');
        assert.equal(request.requirements.workload.concurrency, null);
        assert.equal(request.ai_provider_id, mode === 'manual' ? null : 'provider');
        assert.equal(request.execution_policy.mode, 'approval_required');
        assert.equal(request.planning_facts.max_candidates, mode === 'manual' ? 10 : 3);
    }
});

test('Manual and Agentic send the same workload, request shape and SLA fields', () => {
    const values = { ...DEFAULT_REQUIREMENTS, objective: 'throughput', concurrency: '64', inputTokens: '2048', outputTokens: '128', requestRate: '2', ttftMs: '300', useCase: 'long-inputs', prefixReuse: 'yes' };
    const requests = ['manual', 'agentic'].map(mode => deploymentPlanningRequest(draft, { ...values, requestRate: '' }, 'session-a', mode, 'provider'));
    for (const request of requests) {
        assert.equal(request.requirements.objective, 'throughput');
        assert.equal(request.requirements.workload.concurrency, 64);
        assert.equal(request.requirements.workload.inputTokens, 2048);
        assert.equal(request.requirements.workload.outputTokens, 128);
        assert.equal(request.requirements.workload.prefillHeavy, true);
        assert.equal(request.requirements.workload.prefixReuse, true);
        assert.equal(request.requirements.performance_targets.ttftMs, 300);
    }
    assert.deepEqual(requests[0].requirements.workload, requests[1].requirements.workload);
    assert.equal(deploymentPlanningRequest(draft, { ...values, concurrency: '' }, 'session-a', 'manual').requirements.workload.requestRate, 2);
    assert.throws(() => deploymentPlanningRequest(draft, values, 'session-a', 'manual'), /concurrency or request rate/);
});

test('Agentic planning requires an AI provider', () => {
    assert.throws(() => deploymentPlanningRequest(draft, DEFAULT_REQUIREMENTS, 'session-a', 'agentic'), /requires an AI provider/);
});

test('model context is not mistaken for observed request length', () => {
    for (const context of [256, 512, 1024, 1280, 40960]) {
        const values = defaultRequirements(context);
        assert.equal(values.inputTokens, '');
        assert.equal(values.outputTokens, '');
        const request = deploymentPlanningRequest({ ...draft, contextLength: context }, values, 'session-a', 'manual');
        assert.equal(request.max_model_len, context);
    }
    assert.deepEqual(defaultRequirements(undefined), DEFAULT_REQUIREMENTS);
});

test('notes do not become intent and natural language requires an AI provider', () => {
    const target = { ...draft, description: 'Keep for next month' };
    assert.equal(deploymentPlanningRequest(target, DEFAULT_REQUIREMENTS, 'session-a', 'manual').planner_prompt, null);
    const values = { ...DEFAULT_REQUIREMENTS, plannerPrompt: 'Long documents with short answers' };
    assert.throws(() => deploymentPlanningRequest(target, values, 'session-a', 'agentic'), /Select an AI provider/);
    const request = deploymentPlanningRequest(target, values, 'session-a', 'agentic', 'ai');
    assert.equal(request.planner_prompt, values.plannerPrompt);
    assert.equal(requirementsFromRun({ request }).plannerPrompt, values.plannerPrompt);
    assert.equal(requirementsFromRun({ request: { ...request, planner_prompt: 'balanced priority.' } }).plannerPrompt, '');
});

test('cleared values and unspecified traits remain unspecified', () => {
    const values = { ...DEFAULT_REQUIREMENTS, inputTokens: '', outputTokens: '', useCase: 'unknown', prefixReuse: 'unknown' };
    const request = deploymentPlanningRequest(draft, values, 'session-a', 'manual');
    assert.equal(request.requirements.workload.inputTokens, null);
    assert.equal(request.requirements.workload.prefixReuse, null);
    assert.equal(request.requirements.workload.prefillHeavy, null);
    assert.deepEqual(requirementsFromRun({ request }), { ...values, maxCandidates: '10' });
});

test('legacy plans without structured requirements restore editable defaults', () => {
    assert.deepEqual(requirementsFromRun({ request: {
        planner_prompt: 'latency priority. Prefer stable serving.',
        planning_facts: { max_candidates: 5 },
        execution_policy: { max_total_duration_minutes: 30, allow_trials: true },
    } }), {
        ...DEFAULT_REQUIREMENTS,
        plannerPrompt: 'Prefer stable serving.',
        maxCandidates: '5',
        maxDuration: '30',
        allowTrial: true,
        requiredFeatures: [],
        forbiddenFeatures: [],
    });
});
test('request mapping rejects invalid budgets instead of applying silent defaults', () => {
    for (const values of [
        { maxAccelerators: '0' }, { maxAccelerators: '1.5' }, { maxAccelerators: 'Infinity' },
        { concurrency: '-4' }, { maxCandidates: '11' },
    ]) assert.throws(() => deploymentPlanningRequest(draft, { ...DEFAULT_REQUIREMENTS, ...values }, 'session-a', 'manual'));
    for (const values of [{ inputTokens: '-4' }, { concurrency: '4', requestRate: '2' }]) {
        assert.throws(() => deploymentPlanningRequest(draft, { ...DEFAULT_REQUIREMENTS, ...values }, 'session-a', 'agentic', 'ai'));
    }
    const request = deploymentPlanningRequest(draft, { ...DEFAULT_REQUIREMENTS, maxAccelerators: '2', forbiddenFeatures: ['pd_disaggregation'] }, 'session-a', 'agentic', 'ai');
    assert.equal(request.requirements.hard_constraints.maxAccelerators, 2);
    assert.deepEqual(request.requirements.forbidden_features, ['pd_disaggregation']);
});

test('only the measured winner is labelled best, not every ranked candidate', () => {
    const run = { selected_candidate: { id: 'a' }, candidates: [], optimization: {
        status: 'completed', best_candidate_id: 'b', ranking: [{ candidate_id: 'a' }, { candidate_id: 'b' }],
    } };
    assert.match(candidateEvidence(run), /not the verified winner/);
    run.selected_candidate.id = 'b';
    assert.match(candidateEvidence(run), /Best verified candidate/);
    run.selected_candidate.id = 'c';
    assert.match(candidateEvidence(run), /performance not verified/);
});

test('agentic recommendation stream preserves events across response chunks', async (t) => {
    const signal = new AbortController().signal;
    const encoder = new TextEncoder();
    const previousDocument = globalThis.document;
    globalThis.document = { cookie: 'prism_csrf=csrf-value-123' };
    t.after(() => { globalThis.document = previousDocument; });
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/agentic-deployments/stream');
        assert.equal(options.signal, signal);
        assert.equal(options.headers['X-Prism-CSRF'], 'csrf-value-123');
        assert.deepEqual(JSON.parse(options.body), { model: 'Qwen/test' });
        return new Response(new ReadableStream({
            start(controller) {
                controller.enqueue(encoder.encode('event: progress\ndata: {"phase":"ai","message":"Wait'));
                controller.enqueue(encoder.encode('ing"}\n\nevent: complete\ndata: {"id":"run-1"}\n\n'));
                controller.close();
            },
        }));
    });
    const events = [];

    await streamAgenticRecommendation(
        { model: 'Qwen/test' }, (...event) => events.push(event), signal,
    );

    assert.deepEqual(events, [
        ['progress', { phase: 'ai', message: 'Waiting' }],
        ['complete', { id: 'run-1' }],
    ]);
});

test('agentic recalculation streams through the run-specific refine endpoint', async (t) => {
    const encoder = new TextEncoder();
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/agentic-deployments/run%2F1/refine/stream');
        assert.deepEqual(JSON.parse(options.body), { planner_prompt: 'prefer latency' });
        return new Response(new ReadableStream({
            start(controller) {
                controller.enqueue(encoder.encode('event: progress\ndata: {"phase":"scoring","status":"running"}\n\n'));
                controller.enqueue(encoder.encode('event: complete\ndata: {"id":"run/1","status":"awaiting_approval"}\n\n'));
                controller.close();
            },
        }));
    });
    const events = [];

    await streamAgenticRefinement(
        'run/1', { planner_prompt: 'prefer latency' }, (...event) => events.push(event),
    );

    assert.deepEqual(events, [
        ['progress', { phase: 'scoring', status: 'running' }],
        ['complete', { id: 'run/1', status: 'awaiting_approval' }],
    ]);
});


test('explicit method and SLOs survive planning and restoration', () => {
    const values = { ...DEFAULT_REQUIREMENTS, providerRef: 'optimized-baseline', tpotMs: '40',
        plannerPrompt: 'Prefer simpler operations.' };
    const request = deploymentPlanningRequest(draft, values, 'session-a', 'agentic', 'ai');
    assert.equal(request.requirements.hard_constraints.providerRef, 'optimized-baseline');
    assert.equal(request.planning_facts.workload_profile.mean_input_tokens, null);
    assert.equal(request.planning_facts.tpot_slo_ms, 40);
    assert.deepEqual(requirementsFromRun({ request }), values);
    for (const invalid of [{ tpotMs: '0' }, { plannerPrompt: 'x'.repeat(1801) }]) {
        assert.throws(() => deploymentPlanningRequest(draft, { ...values, ...invalid }, 'session-a', 'agentic'));
    }
});
