import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { AuthContext } from '../../auth/AuthContext.js';
import { EVALUATION_TRANSFER_KEYS } from '../model/transfer.js';
import EvaluationTaskWizard from './EvaluationTaskWizard.jsx';

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

test('Model Market handoff starts New Benchmark Task at Configurations', (t) => {
    const html = renderWizard(t, {
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
    assert.doesNotMatch(html, /Back to model card/);
});

test('ordinary benchmark creation still starts at Evaluation Setup', (t) => {
    const html = renderWizard(t, null);
    assert.match(html, /New Benchmark Task/);
    assert.match(configurationStep(html), /disabled=/);
});

test('configuration handoff does not bypass benchmark-only permissions', (t) => {
    const html = renderWizard(t, {
        operation: 'create-evaluation', initial_step: 'configurations',
    }, (permission) => permission === 'evaluate:run:create');
    assert.match(configurationStep(html), /disabled=/);
});

test('legacy deploy-only handoffs retain their deployment screen', (t) => {
    const html = renderWizard(t, { operation: 'deploy-only' });
    assert.match(html, /Configure deployment/);
    assert.doesNotMatch(html, /New Benchmark Task|Task creation progress/);
});