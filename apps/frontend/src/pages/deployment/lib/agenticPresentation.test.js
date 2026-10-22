import assert from 'node:assert/strict';
import test from 'node:test';
import { candidateAnalysis, generatorLabel, historicalBenchmarkLabel, historicalEvidenceSummary, publicRagMatchSummary, relaxedAicCandidates } from './agenticPresentation.js';

test('public RAG match presentation preserves differences, unknowns, statistics and configuration basis', () => {
    assert.deepEqual(publicRagMatchSummary(null), []);
    const lines = publicRagMatchSummary({
        workload: {
            fields: { mean_input_tokens: { requested: 4000, reported: 7200, difference: 3200, basis: 'nominal_length_proxy' } },
            unknown_fields: ['p95_input_tokens'], similarity: 0.75, coverage: 2 / 3,
            nominal_prompt_shape: { shared_prefix_tokens: 6000, question_tokens: 1200, output_tokens: 1000 },
        },
        latency: { status: 'unknown', fields: {
            ttft: { target_ms: 2000, observations: [{ value_ms: 900, statistic: 'p90', relative_deviation: -0.55 }] },
            tpot: { target_ms: 30, observations: [] },
        } },
        configuration_basis: { configuration: { tp: 2, replicas: 8 } },
    }).join('\n');
    assert.match(lines, /requested 4000, reported 7200, difference 3200/);
    assert.match(lines, /Unknown workload fields: p95_input_tokens/);
    assert.match(lines, /coverage: 67%/);
    assert.match(lines, /Not a measured mean or P95/);
    assert.match(lines, /900 ms \(p90; deviation -55.0%\)/);
    assert.match(lines, /TPOT target 30 ms: unknown/);
    assert.match(lines, /Not current deployment SLO compliance/);
    assert.match(lines, /"tp":2,"replicas":8/);
});

test('historical evidence presentation distinguishes empty, successful, and failed records', () => {
    assert.equal(historicalEvidenceSummary([]), null);
    assert.deepEqual(historicalEvidenceSummary([
        { outcome: 'succeeded' }, { outcome: 'failed' }, { outcome: 'succeeded' },
    ]), { sampleCount: 3, successfulCount: 2, failedCount: 1 });
});

test('historical benchmark label preserves measured metrics, recency, relevance, and failure reasons', () => {
    const label = historicalBenchmarkLabel({
        benchmark_id: 'bench-7', observed_at: '2026-09-20T10:00:00+00:00', outcome: 'failed',
        match_confidence: 0.875, ttft_p95_ms: 410, tpot_p95_ms: 31,
        throughput_tokens_per_s: 720, failure_reason: 'OOM',
    });

    assert.match(label, /^bench-7 \| .*2026.* \| failed \| 88% relevance \| TTFT 410 ms, TPOT 31 ms, 720 tokens\/s, failure: OOM$/);
});

test('generator presentation distinguishes AI MCP and deterministic candidate generation', () => {
    assert.equal(generatorLabel('ai-mcp'), 'AI MCP generator');
    assert.equal(generatorLabel('deterministic'), 'Deterministic generator');
    assert.equal(generatorLabel('unknown'), 'Deterministic generator');
});

test('relaxed AIC candidates remain visible even when no prediction survives planning evidence filtering', () => {
    const candidates = [{topologyMode: 'agg', decodeTp: 1, decodeReplicas: 1, predicted: {ttftMs: 44.872, tpotMs: 6.454}}];
    assert.deepEqual(relaxedAicCandidates({planning_evidence: [], generator_tool_trace: [
        {tool: 'search_candidates', status: 'success', result: {candidates: []}},
        {tool: 'search_aic_with_relaxation', status: 'success', result: {candidates}},
    ]}), candidates);
    assert.deepEqual(relaxedAicCandidates({generator_tool_trace: [
        {tool: 'search_aic_with_relaxation', status: 'error', result: {candidates}},
    ]}), []);
});

test('candidate analysis displays a fixed-topology AIC estimate when search returned no candidates', () => {
    const candidate = {
        provider_ref: 'baseline-vllm', replicas: 1, tensor_parallel_size: 1, required_gpus: 1,
        slo_status: 'not_satisfied', performance_estimate_source: 'aic_estimate',
        performance_estimate: { ttft_ms: 222.535, tpot_ms: 47.497, throughput_tokens_per_sec: 20.674 },
    };
    const analysis = candidateAnalysis(candidate, 0, [], [], null);

    assert.match(analysis.performance, /AIC estimate \(exact topology; original SLO not met\): TTFT 222\.5 ms, TPOT 47\.5 ms, throughput 20\.7 tokens\/s/);
    assert.match(analysis.ranking, /fixed-topology estimate informs the SLO assessment/);
    assert.match(candidateAnalysis({ ...candidate, performance_estimate: null }, 0, [], [], null).performance,
        /No exact AIC topology prediction is available/);
});