import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EvaluationConfigurationEditor from './EvaluationConfigurationEditor.jsx';

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
        if (entrySource === 'model-market') assert.doesNotMatch(html, /id="optimizationSelection"/);
        else assert.match(html, /id="optimizationSelection"/);
    });
}
