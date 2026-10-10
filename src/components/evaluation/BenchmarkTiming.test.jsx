import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import BenchmarkTiming from './BenchmarkTiming.jsx';
import { benchmarkTimeDisplay } from '../../features/evaluation/benchmarkSettings.js';

const start = '2026-10-08T09:00:00Z';
const run = { status: 'running', phase: 'benchmark', benchmark_started_at: start, phase_started_at: start,
    timing: { lower_seconds: 120, upper_seconds: 300, timeout_seconds: 900, timeout_mode: 'auto', measured_requests: 24 } };

test('remaining estimate and hard deadline are separate', () => {
    const display = benchmarkTimeDisplay(run, Date.parse(start) + 60000);
    assert.equal(display.remaining, 'About 1 min to 4 min remaining');
    assert.equal(display.deadline, 'Current phase timeout in 14 min');
    assert.equal(display.estimate, '2 min to 5 min');
});

test('overrun never claims zero remaining and terminal results stop counting', () => {
    assert.equal(benchmarkTimeDisplay(run, Date.parse(start) + 360000).remaining, 'Taking longer than estimated');
    const display = benchmarkTimeDisplay({...run, status:'failed', finished_at:'2026-10-08T09:10:00Z'}, Date.parse(start) + 3600000);
    assert.equal(display.remaining, 'Benchmark failed');
    assert.equal(display.elapsed, '10 min');
    assert.equal(display.deadline, null);
});

test('queued, preparation, unknown workloads and legacy records do not invent progress', () => {
    assert.equal(benchmarkTimeDisplay({status:'queued',timing:run.timing}).remaining, 'Waiting for benchmark execution');
    assert.equal(benchmarkTimeDisplay({...run,phase:'preparing-storage'}).deadline,null);
    assert.equal(benchmarkTimeDisplay({status:'running'}).remaining,'Remaining time unavailable');
    assert.equal(benchmarkTimeDisplay({...run,timing:{upper_seconds:null}}).estimate,'Not available for this workload');
});

test('time block renders counts, automatic budget and explicit warning', () => {
    const html = renderToStaticMarkup(createElement(BenchmarkTiming, {run:{...run,status:'succeeded',timing:{...run.timing,timeout_warning:'Timeout too short'}}}));
    assert.match(html,/Measured requests/);
    assert.match(html,/Automatic.*timeout/);
    assert.match(html,/Timeout too short/);
    assert.doesNotMatch(html,/remaining|Current phase timeout/);
});

test('compact runtime view only renders a remaining-time badge', () => {
    const current = {...run, benchmark_started_at:new Date(Date.now() - 60000).toISOString(),
        timing:{...run.timing, assumptions:'Detailed assumptions', timeout_warning:'Timeout too short'}};
    const html = renderToStaticMarkup(createElement(BenchmarkTiming, {run:current, compact:true}));
    assert.match(html,/Estimated benchmark time remaining/);
    assert.match(html,/About 1 min to 4 min remaining/);
    assert.doesNotMatch(html,/<section|<dl|Measured requests|timeout|Elapsed|Detailed assumptions|Timeout too short/);
});

test('compact runtime view hides terminal runs and handles unavailable estimates', () => {
    for (const status of ['succeeded','failed','cancelled']) {
        assert.equal(renderToStaticMarkup(createElement(BenchmarkTiming, {run:{...run,status},compact:true})), '');
    }
    const html = renderToStaticMarkup(createElement(BenchmarkTiming, {run:{status:'running'},compact:true}));
    assert.match(html,/Remaining time unavailable/);
    assert.doesNotMatch(html,/Not available for this workload|<section/);
});