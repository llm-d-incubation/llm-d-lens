import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MODELS } from '@repo/shared/catalog';
import { AuthContext } from '../../auth/AuthContext.js';
import ModelMarketPage from './ModelMarketPage.jsx';

test('model cards offer Manual mode and an explicit Configure deployment action', (testContext) => {
    const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        value: {
            getItem: (key) => key === 'prism_model_market_return_draft'
                ? JSON.stringify({ model: MODELS[0].repository }) : null,
            removeItem() {},
        },
    });
    testContext.after(() => {
        if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage);
        else delete globalThis.sessionStorage;
    });
    const html = renderToStaticMarkup(createElement(AuthContext.Provider, { value: { can: () => true } },
        createElement(ModelMarketPage, { onNavigate() {} })));
    assert.match(html, />Manual</);
    assert.match(html, /Template-based deployment/);
    assert.match(html, /Configure deployment/);
    assert.doesNotMatch(html, /Template-based benchmark configuration|>Advanced</);
});