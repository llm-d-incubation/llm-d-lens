import assert from 'node:assert/strict';
import test, { describe } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { AsyncState } from './AsyncState.jsx';
import { Toast } from './Toast.jsx';
import { Boxes, Search } from 'lucide-react';
import { HelpTooltip, Input, Select } from './FormControls.jsx';

describe("Async states", () => {
    test('state precedence is loading, error, empty, content with domain slots', () => {
        const props = { loadingContent: <p>loading-slot</p>, errorContent: <p>error-slot</p>, emptyContent: <button>Add provider</button>, children: <p>records</p> };
        assert.equal(renderToStaticMarkup(<AsyncState {...props} loading error="failure" empty />), '<p>loading-slot</p>');
        assert.equal(renderToStaticMarkup(<AsyncState {...props} error="failure" empty />), '<p>error-slot</p>');
        assert.equal(renderToStaticMarkup(<AsyncState {...props} empty />), '<button>Add provider</button>');
        assert.equal(renderToStaticMarkup(<AsyncState {...props} />), '<p>records</p>');
    });

    test('default states retain retry and empty labels', () => {
        assert.match(renderToStaticMarkup(<AsyncState error={new Error('Unavailable')} onRetry={() => {}} />), /Unavailable.*Retry/);
        assert.match(renderToStaticMarkup(<AsyncState empty emptyTitle="No records" />), /No records/);
    });
});

describe("Notifications", () => {
    test('notification is portalled outside page layout with fixed, bounded positioning', () => {
        const previous = globalThis.document;
        const body = { nodeType: 1 };
        globalThis.document = { body };
        try {
            let dismissed = false;
            const toast = Toast({
                message: 'Model stopped serving. Provider states, weights and running Pods are unchanged.',
                title: 'Model services',
                onDismiss: () => { dismissed = true; },
            });
            assert.equal(toast.containerInfo, body);
            const html = renderToStaticMarkup(toast.children);
            assert.match(html, /fixed bottom-6 right-4/);
            assert.match(html, /max-w-sm/);
            assert.match(html, /pointer-events-none/);
            assert.match(html, /pointer-events-auto/);
            assert.match(html, /role="status"/);
            assert.match(html, /Model stopped serving/);
            assert.match(html, /bg-theme-card/);
            assert.match(html, /dark:text-sky-400/);
            const panel = toast.children.props.children;
            const close = panel.props.children.at(-1);
            assert.equal(close.props['aria-label'], 'Dismiss notification');
            close.props.onClick();
            assert.equal(dismissed, true);
        } finally {
            if (previous === undefined) delete globalThis.document;
            else globalThis.document = previous;
        }
    });

    test('errors retain full details and alert semantics; empty notifications render nothing', () => {
        const previous = globalThis.document;
        globalThis.document = { body: { nodeType: 1 } };
        try {
            assert.equal(Toast({ message: '' }), null);
            const toast = Toast({ message: 'State saved;\nGateway reconciliation failed', tone: 'danger', onDismiss: () => {} });
            const html = renderToStaticMarkup(toast.children);
            assert.match(html, /role="alert"/);
            assert.match(html, /whitespace-pre-wrap/);
            assert.match(html, /max-h-48 overflow-y-auto/);
            assert.match(html, /Gateway reconciliation failed/);
            assert.match(html, /dark:text-rose-400/);
        } finally {
            if (previous === undefined) delete globalThis.document;
            else globalThis.document = previous;
        }
    });
});

describe("Form controls", () => {
    test('help tooltips support an accessible custom trigger without nesting buttons', () => {
        const html = renderToStaticMarkup(
            <HelpTooltip trigger={<button aria-label="Lens Assistant">Icon</button>}>Lens Assistant</HelpTooltip>,
        );
        assert.equal((html.match(/<button/g) || []).length, 1);
        const tooltipId = html.match(/id="([^"]+)" role="tooltip"/)[1];
        assert.ok(html.includes(`aria-describedby="${tooltipId}"`));
        assert.match(html, /group-hover:visible group-focus-within:visible/);
        assert.match(html, /aria-label="Lens Assistant"/);
        const defaultHelp = renderToStaticMarkup(<HelpTooltip label="Field help">Explanation</HelpTooltip>);
        assert.match(defaultHelp, /aria-label="Field help"/);
        assert.match(defaultHelp, /role="tooltip"[^>]*>Explanation/);
    });

    test('ordinary fields retain their original surfaces and native select markup', () => {
        const input = renderToStaticMarkup(<Input aria-label="Name" value="chat" readOnly />);
        assert.match(input, /bg-white dark:bg-slate-900/);
        assert.match(input, /value="chat"/);
        const select = renderToStaticMarkup(<Select aria-label="Cluster" value="a" onChange={() => {}}><option value="a">Cluster A</option></Select>);
        assert.ok(select.startsWith('<select'));
        assert.doesNotMatch(select, /appearance-none|<svg/);
        assert.match(select, /selected=""/);
    });

    test('toolbar fields have compact transparent surfaces without changing native input behavior', () => {
        const input = renderToStaticMarkup(<Input variant="toolbar" icon={Search} type="search" aria-label="Public model name" list="models" value="chat" onChange={() => {}} />);
        const select = renderToStaticMarkup(<Select variant="toolbar" icon={Boxes} aria-label="Cluster" value="a" onChange={() => {}}><option value="a">Cluster A</option></Select>);
        for (const html of [input, select]) {
            assert.match(html, /h-8/);
            assert.match(html, /dark:bg-transparent/);
            assert.match(html, /pointer-events-none/);
            assert.doesNotMatch(html, /\svariant=/);
        }
        assert.match(input, /type="search"/);
        assert.match(input, /list="models"/);
        assert.match(input, /value="chat"/);
        assert.match(select, /appearance-none/);
        assert.match(select, /<select[^>]*aria-label="Cluster"/);
        assert.match(select, /value="a" selected=""/);
    });
});
