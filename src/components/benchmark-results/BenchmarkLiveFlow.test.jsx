import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import BenchmarkLiveFlow from './BenchmarkLiveFlow.jsx';

test('completed resource-only monitoring retains topology and resource values', () => {
    const details = {workflow: {status: 'succeeded'}, cases: [{case: {id: 'case', metrics: {observability: {
        window: {start: '2026-10-09T06:05:52Z', end: '2026-10-09T06:10:55Z'},
        per_pod: [{pod: 'decode-0', role: 'decode', cpu_usage_cores: {mean: 1}}],
    }}}}]};
    const html = renderToStaticMarkup(<BenchmarkLiveFlow details={details} onShowResources={() => {}} />);
    assert.doesNotMatch(html, /Inference traffic monitoring unavailable/);
    assert.match(html, /Inspect decode-0/);
    assert.match(html, /Resources during benchmark/);
    assert.match(html, /Pod resource ranking/);
    assert.match(html, /Request\/token samples are missing/);
    assert.match(html, /View recorded resources/);
});

test('recorded resources remain visible without classified serving pods', () => {
    const details = {cases: [{case: {id: 'unclassified', metrics: {observability: {
        window: {start: '2026-10-09T06:05:52Z', end: '2026-10-09T06:10:55Z'},
        per_pod: [{pod: 'worker-0', role: 'model-server', cpu_usage_cores: {mean: 2, max: 3}}],
    }}}}]};
    const html = renderToStaticMarkup(<BenchmarkLiveFlow details={details} />);
    assert.match(html, /worker-0/);
    assert.match(html, /Pod resource ranking/);
    assert.doesNotMatch(html, /Inference traffic monitoring unavailable/);
});
