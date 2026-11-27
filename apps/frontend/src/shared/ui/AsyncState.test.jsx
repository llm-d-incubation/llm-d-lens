import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test, { describe } from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { AsyncState } from './AsyncState.jsx';
import { Toast } from './Toast.jsx';
import { Boxes, Search } from 'lucide-react';
import { HelpTooltip, Input, Label, Select } from './FormControls.jsx';
import { StatCard } from './StatCard.jsx';
import { ChartContainer } from './charts/ChartContainer.jsx';
import { ChartTooltip, ChartTooltipRow } from './charts/ChartTooltip.jsx';
import { Panel, SummaryStrip } from './Panel.jsx';
import { ToggleGroup } from './ToggleGroup.jsx';
import { MultiSelectDropdown } from './MultiSelectDropdown.jsx';
import { SideDrawer as IndexSideDrawer } from './index.js';
import { SideDrawer } from './SideDrawer.jsx';
import SimulationTaskDetails, {
    ASK_AI_MAX_SAVED_TASKS,
    ASK_AI_STORAGE_KEY,
    AskAiModal,
    LogsAndConfigTab,
    clearSavedAskAiAnalysis,
    loadSavedAskAiAnalysis,
    resolveTaskHardware,
    saveAskAiAnalysis,
} from '../../pages/simulation/ui/SimulationTaskDetails.jsx';
import SimulationDashboard, {
    SimulationTaskRow,
    buildLiveTaskTimeline,
    buildSimulationTasksQuery,
    mergeSimulationFacets,
    resolveHardwareFacetOptions,
    resolveRowMetrics,
} from '../../pages/simulation/ui/SimulationDashboard.jsx';

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

describe("HelpTooltip and card help integration", () => {
    test('HelpTooltip supports align="end" and asSpan without rendering a button', () => {
        const html = renderToStaticMarkup(
            <HelpTooltip label="Latency help" align="end" asSpan>
                P99 request latency
            </HelpTooltip>,
        );
        assert.equal((html.match(/<button/g) || []).length, 0);
        assert.match(html, /tabindex="0"/);
        assert.match(html, /aria-label="Latency help"/);
        assert.match(html, /role="tooltip"/);
        assert.match(html, /right-0/);
        assert.match(html, /normal-case tracking-normal/);
        assert.match(html, /whitespace-normal/);
        assert.match(html, /z-50/);
        assert.match(html, /P99 request latency/);

        const nowrapHtml = renderToStaticMarkup(
            <HelpTooltip label="Lens Assistant" tooltipClassName="bottom-full pb-2 whitespace-nowrap">
                Lens Assistant
            </HelpTooltip>,
        );
        assert.match(nowrapHtml, /whitespace-nowrap/);
        assert.doesNotMatch(nowrapHtml, /whitespace-normal/);
    });

    test('StatCard renders title and detail help, and avoids nested buttons when clickable', () => {
        const staticCard = renderToStaticMarkup(
            <StatCard
                title="Throughput"
                value="120 tok/s"
                help="Aggregate generated tokens per second"
                details={[{ label: 'Prompt', value: '40 tok/s', help: 'Input token rate' }]}
            />,
        );
        assert.equal((staticCard.match(/<button/g) || []).length, 2);
        assert.match(staticCard, /role="tooltip"[^>]*>Aggregate generated tokens per second/);
        assert.match(staticCard, /role="tooltip"[^>]*>Input token rate/);

        const clickableCard = renderToStaticMarkup(
            <StatCard
                title="Throughput"
                value="120 tok/s"
                help="Aggregate generated tokens per second"
                details={[{ label: 'Prompt', value: '40 tok/s', help: 'Input token rate' }]}
                onClick={() => {}}
            />,
        );
        assert.equal((clickableCard.match(/<button/g) || []).length, 1);
        assert.match(clickableCard, /^<button/);
        assert.match(clickableCard, /tabindex="0"/);
        assert.match(clickableCard, /role="tooltip"[^>]*>Aggregate generated tokens per second/);
        assert.match(clickableCard, /role="tooltip"[^>]*>Input token rate/);
    });

    test('ChartContainer renders help tooltip with typography resets', () => {
        const html = renderToStaticMarkup(
            <ChartContainer title="TTFT Distribution" help="Time to first token across samples">
                <div>chart</div>
            </ChartContainer>,
        );
        assert.match(html, /hover:z-30 focus-within:z-30/);
        assert.match(html, /role="tooltip"/);
        assert.match(html, /font-sans font-normal normal-case tracking-normal/);
        assert.match(html, /Time to first token across samples/);
    });

    test('Panel and SummaryStrip render help tooltips cleanly', () => {
        const panelHtml = renderToStaticMarkup(
            <Panel title="Configuration" help="Active runtime parameters">
                <div>body</div>
            </Panel>,
        );
        assert.match(panelHtml, /hover:z-20 focus-within:z-20/);
        assert.match(panelHtml, /role="tooltip"[^>]*>Active runtime parameters/);

        const stripHtml = renderToStaticMarkup(
            <SummaryStrip
                items={[
                    { label: 'Model', value: 'Llama-3', help: 'Served model identifier' },
                    { label: 'Replicas', value: '2' },
                ]}
            />,
        );
        assert.match(stripHtml, /overflow-visible/);
        assert.doesNotMatch(stripHtml, /overflow-hidden/);
        assert.match(stripHtml, /role="tooltip"[^>]*>Served model identifier/);
    });

    test('HelpTooltip and wrappers render formula block below prose and preserve non-formula output', () => {
        const plainHtml = renderToStaticMarkup(<HelpTooltip label="Plain help">Explanation</HelpTooltip>);
        assert.doesNotMatch(plainHtml, /lens-help-formula/);
        assert.match(plainHtml, /role="tooltip"[^>]*>Explanation<\/span>/);

        const emptyFormulaHtml = renderToStaticMarkup(
            <HelpTooltip label="Plain help" formula="   ">{{ text: 'Explanation only' }}</HelpTooltip>,
        );
        assert.doesNotMatch(emptyFormulaHtml, /lens-help-formula/);
        assert.match(emptyFormulaHtml, /role="tooltip"[^>]*>Explanation only<\/span>/);

        const formulaPropHtml = renderToStaticMarkup(
            <HelpTooltip label="TTFT help" align="start" formula="TTFT = t_first_token − t_request_start">
                Time to first token
            </HelpTooltip>,
        );
        assert.match(formulaPropHtml, /\bw-72\b/);
        assert.match(formulaPropHtml, /role="tooltip"[^>]*>Time to first token<span class="lens-help-formula/);
        assert.match(formulaPropHtml, /Formula<\/span><code[^>]*>TTFT = t_first_token − t_request_start<\/code>/);
        assert.match(formulaPropHtml, /border-theme-border/);
        assert.match(formulaPropHtml, /bg-theme-bg\/70/);

        const objectHelpHtml = renderToStaticMarkup(
            <HelpTooltip label="TPOT help" formula="TPOT_Override">
                {{ text: 'Decode speed', formula: 'TPOT_Default' }}
            </HelpTooltip>,
        );
        assert.match(objectHelpHtml, /Decode speed/);
        assert.match(objectHelpHtml, /TPOT_Override/);
        assert.doesNotMatch(objectHelpHtml, /TPOT_Default/);

        const labelHtml = renderToStaticMarkup(
            <Label htmlFor="grace" help="Drain window" helpFormula="Effective = Duration + Grace">
                Grace period
            </Label>,
        );
        assert.match(labelHtml, /Drain window/);
        assert.match(labelHtml, /Effective = Duration \+ Grace/);
        assert.doesNotMatch(labelHtml, /helpFormula=/);

        const chartHtml = renderToStaticMarkup(
            <ChartContainer title="Latency" help="Observed latency" helpFormula="E2E = t_complete − t_start">
                <div>plot</div>
            </ChartContainer>,
        );
        assert.match(chartHtml, /Observed latency/);
        assert.match(chartHtml, /E2E = t_complete − t_start/);

        const statHtml = renderToStaticMarkup(
            <StatCard
                title="Goodput"
                value="10 req/s"
                help="SLO-compliant throughput"
                helpFormula="Goodput = Good_Requests / Effective_Window_s"
                details={[{ label: 'Attainment', value: '95%', help: 'Share passing SLOs', formula: 'Attainment = Good / Total × 100' }]}
                onClick={() => {}}
            />,
        );
        assert.equal((statHtml.match(/<button/g) || []).length, 1);
        assert.match(statHtml, /Goodput = Good_Requests \/ Effective_Window_s/);
        assert.match(statHtml, /Attainment = Good \/ Total × 100/);

        const panelHtml = renderToStaticMarkup(
            <Panel title="Compare" help="Side-by-side" helpFormula="Δ = Candidate − Baseline">
                <div>rows</div>
            </Panel>,
        );
        assert.match(panelHtml, /Δ = Candidate − Baseline/);
        assert.doesNotMatch(panelHtml, /helpFormula=/);

        const stripHtml = renderToStaticMarkup(
            <SummaryStrip
                items={[{ label: 'Window', value: '60 s', help: 'Effective replay span', formula: 'Effective = Duration + Grace' }]}
            />,
        );
        assert.match(stripHtml, /Effective = Duration \+ Grace/);
    });
});

describe("Selector controls", () => {
    test('ToggleGroup renders semantic classes and aria-pressed state for active and inactive options', () => {
        const html = renderToStaticMarkup(
            <ToggleGroup
                value="tpot"
                onChange={() => {}}
                showSelectionIndicator
                options={[
                    { value: 'ttft', label: 'TTFT' },
                    { value: 'tpot', label: 'TPOT' },
                    { value: 'e2e', label: 'E2E', disabled: true },
                ]}
            />,
        );
        assert.match(html, /<div[^>]*role="group"[^>]*class="[^"]*\blens-toggle-group\b/);
        assert.equal((html.match(/\blens-toggle-option\b/g) || []).length, 3);
        assert.match(html, /aria-pressed="false" class="[^"]*\blens-toggle-option\b[^"]*"[^>]*>TTFT/);
        assert.match(html, /aria-pressed="true" class="[^"]*\blens-toggle-option\b[^"]*"[^>]*>TPOT/);
        assert.match(html, /aria-pressed="false" class="[^"]*\blens-toggle-option\b[^"]*"[^>]*>E2E/);
        assert.match(html, /dark:bg-slate-700/);
        assert.match(html, /dark:text-white/);
        assert.doesNotMatch(html, /data-custom-active/);
    });

    test('ToggleGroup marks active option with data-custom-active only when activeClassName overrides bg/text', () => {
        const customHtml = renderToStaticMarkup(
            <ToggleGroup
                value="a"
                onChange={() => {}}
                activeClassName="accelerator-tab-active bg-sky-500/20 dark:bg-sky-500/20 text-white"
                options={[
                    { value: 'a', label: 'Tab A' },
                    { value: 'b', label: 'Tab B' },
                ]}
            />,
        );
        assert.match(customHtml, /aria-pressed="true"[^>]*data-custom-active="true"[^>]*>Tab A/);
        assert.doesNotMatch(customHtml, /data-custom-active="true"[^>]*>Tab B/);

        const semanticOnlyHtml = renderToStaticMarkup(
            <ToggleGroup
                value="a"
                onChange={() => {}}
                activeClassName="usage-toggle-active"
                options={[
                    { value: 'a', label: 'Tab A' },
                    { value: 'b', label: 'Tab B' },
                ]}
            />,
        );
        assert.doesNotMatch(semanticOnlyHtml, /data-custom-active/);
    });

    test('MultiSelectDropdown renders semantic classes, data-has-selection, count badge, and aria-checked options', () => {
        const emptyHtml = renderToStaticMarkup(
            <MultiSelectDropdown
                label="Status"
                options={['completed', 'failed']}
                selected={new Set()}
                onChange={() => {}}
            />,
        );
        assert.match(emptyHtml, /class="[^"]*\blens-multiselect-trigger\b/);
        assert.match(emptyHtml, /data-has-selection="false"/);
        assert.doesNotMatch(emptyHtml, /\blens-multiselect-count\b/);

        const activeHtml = renderToStaticMarkup(
            <MultiSelectDropdown
                label="Status"
                options={['completed', 'failed']}
                selected={new Set(['completed'])}
                onChange={() => {}}
                defaultOpen
            />,
        );
        assert.match(activeHtml, /class="[^"]*\blens-multiselect-trigger\b/);
        assert.match(activeHtml, /data-has-selection="true"/);
        assert.match(activeHtml, /class="[^"]*\blens-multiselect-count\b[^"]*">1 selected<\/span>/);
        assert.match(activeHtml, /role="group"[^>]*class="[^"]*\blens-multiselect-menu\b/);
        assert.equal((activeHtml.match(/\blens-multiselect-option\b/g) || []).length, 2);
        assert.equal((activeHtml.match(/\blens-multiselect-check\b/g) || []).length, 2);
        assert.match(activeHtml, /role="checkbox" aria-checked="true"[^>]*aria-label="completed"/);
        assert.match(activeHtml, /role="checkbox" aria-checked="false"[^>]*aria-label="failed"/);
        assert.match(activeHtml, /dark:bg-cyan-500 dark:border-cyan-500 dark:text-white/);
    });
});

describe("Theme-safe tooltips and chart cursor CSS contract", () => {
    test('ChartTooltip uses lens-chart-tooltip and theme tokens without white-only bg-white/95', () => {
        const html = renderToStaticMarkup(
            <ChartTooltip title="Elapsed 10 s">
                <ChartTooltipRow color="#10b981" label="Completed" value={42.5} unit=" req/s" />
            </ChartTooltip>,
        );
        assert.match(html, /class="[^"]*\blens-chart-tooltip\b/);
        assert.match(html, /\bbg-theme-card\/95\b/);
        assert.match(html, /\bborder-theme-border\b/);
        assert.match(html, /\btext-theme-text\b/);
        assert.doesNotMatch(html, /\bbg-white\/95\b/);
        assert.match(html, /Elapsed 10 s/);
        assert.match(html, /Completed/);
        assert.match(html, /42\.5 req\/s/);
    });

    test('HelpTooltip attaches lens-help-tooltip to its role="tooltip" surface', () => {
        const html = renderToStaticMarkup(<HelpTooltip label="Metric help">Latency details</HelpTooltip>);
        assert.match(html, /role="tooltip" class="[^"]*\blens-help-tooltip\b/);
        assert.match(html, /Latency details/);
    });

    test('index.css defines dark-mode chart cursor, tooltip, and selector contract rules', () => {
        const cssPath = path.resolve(import.meta.dirname, '../../app/styles/index.css');
        const css = fs.readFileSync(cssPath, 'utf8');
        assert.match(css, /@custom-variant\s+dark\s+\(&:where\(\.dark,\s*\.dark \*\)\);/);
        assert.match(css, /--chart-cursor-fill:\s*rgba\(148,\s*163,\s*184,\s*0\.10\);/);
        assert.match(css, /--chart-cursor-stroke:\s*rgba\(148,\s*163,\s*184,\s*0\.28\);/);
        assert.match(css, /html\.dark\s+\.recharts-rectangle\.recharts-tooltip-cursor/);
        assert.match(css, /html:not\(\.dark\)\s+\.recharts-rectangle\.recharts-tooltip-cursor/);
        assert.match(css, /html\.dark\s+\.recharts-curve\.recharts-tooltip-cursor/);
        assert.match(css, /html\.dark\s+\.recharts-default-tooltip/);
        assert.match(css, /html\.dark\s+\.lens-chart-tooltip/);
        assert.match(css, /html:not\(\.dark\)\s+\.lens-chart-tooltip/);
        assert.match(css, /html\.dark\s+\.lens-help-tooltip/);
        assert.match(css, /html:not\(\.dark\)\s+\.lens-help-tooltip/);
        assert.match(css, /html\.dark\s+\.lens-toggle-group\s+\.lens-toggle-option:not\(\[aria-pressed="true"\]\)/);
        assert.match(css, /html\.dark\s+\.lens-toggle-group\s+\.lens-toggle-option\[aria-pressed="true"\]:not\(\[data-custom-active="true"\]\)/);
        assert.match(css, /html\.dark\s+\.lens-multiselect-menu/);
    });
});

function createMemoryStorage() {
    const map = new Map();
    return {
        getItem(key) { return map.has(key) ? map.get(key) : null; },
        setItem(key, value) { map.set(key, String(value)); },
        removeItem(key) { map.delete(key); },
        clear() { map.clear(); },
    };
}

function createDrawerTestEnv(t, { storage, fetchHandler } = {}) {
    const prevAct = globalThis.IS_REACT_ACT_ENVIRONMENT;
    const prevDoc = globalThis.document;
    const prevWin = globalThis.window;
    const prevHtml = globalThis.HTMLElement;
    const prevSvg = globalThis.SVGElement;
    const prevFetch = globalThis.fetch;
    const prevGetComputedStyle = globalThis.getComputedStyle;

    const listeners = new Map();
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
        isConnected: true,
        get className() { return this.attributes.class || ''; },
        set className(v) { this.attributes.class = String(v); },
        get textContent() {
            if (this.nodeType === 3) return this.nodeValue || '';
            if (this.nodeType === 8) return '';
            return this.childNodes.map((child) => child.textContent).join('');
        },
        set textContent(value) {
            const text = String(value);
            if (this.nodeType === 3) {
                this.nodeValue = text;
                return;
            }
            this.childNodes = text ? [doc.createTextNode(text)] : [];
            for (const child of this.childNodes) child.parentNode = this;
        },
        addEventListener() {},
        removeEventListener() {},
        setAttribute(key, value) { this.attributes[key] = String(value); },
        removeAttribute(key) { delete this.attributes[key]; },
        getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; },
        hasAttribute(key) { return key in this.attributes; },
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
            if (index !== -1) {
                this.childNodes.splice(index, 1);
                child.parentNode = null;
            }
            return child;
        },
        get firstChild() { return this.childNodes[0] || null; },
        get options() { return walk(this, (el) => el.tagName === 'OPTION'); },
        get nextSibling() {
            if (!this.parentNode) return null;
            const index = this.parentNode.childNodes.indexOf(this);
            return index >= 0 ? this.parentNode.childNodes[index + 1] || null : null;
        },
        querySelectorAll() { return []; },
        contains(other) {
            let cur = other;
            while (cur) {
                if (cur === this) return true;
                cur = cur.parentNode;
            }
            return false;
        },
        getClientRects() { return [{ width: 10, height: 10 }]; },
        focus() { doc.activeElement = this; },
        blur() {},
        scrollIntoView() {},
        remove() { this.parentNode?.removeChild(this); },
    });

    const win = {
        localStorage: storage || createMemoryStorage(),
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(fn);
        },
        removeEventListener(type, fn) {
            listeners.get(type)?.delete(fn);
        },
        dispatchKeyDown(event) {
            for (const fn of [...(listeners.get('keydown') || [])]) fn(event);
        },
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        HTMLIFrameElement: function HTMLIFrameElement() {},
        HTMLElement: function HTMLElement() {},
        SVGElement: function SVGElement() {},
        Node: function Node() {},
    };
    const docListeners = new Map();
    const doc = {
        nodeType: 9,
        cookie: '',
        defaultView: win,
        documentElement: null,
        body: null,
        activeElement: null,
        createElement(tag) { return makeNode(tag, doc); },
        createElementNS(ns, tag) {
            const node = makeNode(tag, doc);
            node.namespaceURI = ns;
            return node;
        },
        createTextNode(text) {
            const node = makeNode('#text', doc);
            node.nodeValue = String(text);
            return node;
        },
        createComment(text) {
            const node = makeNode('#comment', doc);
            node.nodeValue = String(text);
            return node;
        },
        addEventListener(type, fn) {
            if (!docListeners.has(type)) docListeners.set(type, new Set());
            docListeners.get(type).add(fn);
        },
        removeEventListener(type, fn) {
            docListeners.get(type)?.delete(fn);
        },
        dispatchEvent(event) {
            for (const fn of [...(docListeners.get(event.type) || [])]) fn(event);
        },
    };
    win.document = doc;
    doc.documentElement = makeNode('html', doc);
    doc.body = makeNode('body', doc);
    doc.documentElement.appendChild(doc.body);

    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    globalThis.document = doc;
    globalThis.window = win;
    globalThis.HTMLElement = win.HTMLElement;
    globalThis.SVGElement = win.SVGElement;
    if (fetchHandler) globalThis.fetch = fetchHandler;
    globalThis.getComputedStyle = win.getComputedStyle;

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
        globalThis.fetch = prevFetch;
        if (prevGetComputedStyle === undefined) delete globalThis.getComputedStyle; else globalThis.getComputedStyle = prevGetComputedStyle;
    });

    const walk = (node, pred, acc = []) => {
        if (!node || node.nodeType !== 1) return acc;
        if (pred(node)) acc.push(node);
        for (const child of node.childNodes) walk(child, pred, acc);
        return acc;
    };
    const reactProps = (node) => {
        const key = Object.keys(node || {}).find((k) => k.startsWith('__reactProps$'));
        return key ? node[key] : {};
    };
    const flush = async () => {
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    };

    return { root, doc, win, listeners, walk, reactProps, flush };
}

describe("SideDrawer primitive", () => {
    test('is exported from index.js, skips SSR without document, and includes textarea in focus trap', () => {
        assert.equal(IndexSideDrawer, SideDrawer);
        assert.equal(renderToStaticMarkup(<SideDrawer title="Evidence" onClose={() => {}}>Body</SideDrawer>), '');

        const sourcePath = path.resolve(import.meta.dirname, './SideDrawer.jsx');
        const source = fs.readFileSync(sourcePath, 'utf8');
        assert.match(source, /textarea:not\(\[disabled\]\)/);
    });

    test('supports isOpen gating, backward-compatible defaults, full-height theme tokens, and slots', async (t) => {
        const { root, doc, win, listeners, walk } = createDrawerTestEnv(t);
        let closedCount = 0;

        await act(async () => {
            root.render(
                <SideDrawer isOpen={false} title="Hidden" onClose={() => { closedCount += 1; }}>
                    Hidden body
                </SideDrawer>,
            );
        });
        assert.equal(walk(doc.body, (el) => el.getAttribute('role') === 'dialog').length, 0);
        assert.notEqual(doc.body.style.overflow, 'hidden');
        assert.equal(listeners.get('keydown')?.size || 0, 0);

        await act(async () => {
            root.render(
                <SideDrawer title="Pod metrics" onClose={() => { closedCount += 1; }}>
                    Legacy content
                </SideDrawer>,
            );
        });
        const legacyDialog = walk(doc.body, (el) => el.getAttribute('role') === 'dialog')[0];
        assert.ok(legacyDialog, 'Expected SideDrawer dialog when isOpen is omitted');
        assert.equal(doc.body.style.overflow, 'hidden');
        assert.match(legacyDialog.className, /\bbenchmark-inspector-drawer\b/);
        assert.match(legacyDialog.className, /\binset-y-0\b/);
        assert.match(legacyDialog.className, /\bright-0\b/);
        assert.match(legacyDialog.className, /\bh-dvh\b/);
        assert.match(legacyDialog.className, /\bbg-theme-card\b/);
        assert.match(legacyDialog.className, /\bborder-theme-border\b/);
        assert.match(legacyDialog.className, /\btext-theme-text\b/);
        assert.match(legacyDialog.textContent, /Recorded evidence/);
        assert.match(legacyDialog.textContent, /Pod metrics/);
        assert.match(legacyDialog.textContent, /Legacy content/);
        const defaultCloseBtn = walk(legacyDialog, (el) => el.tagName === 'BUTTON')[0];
        assert.equal(defaultCloseBtn.getAttribute('aria-label'), 'Close inspector');

        await act(async () => {
            root.render(
                <SideDrawer
                    isOpen
                    title="Custom Drawer"
                    subtitle=""
                    icon={<span data-testid="drawer-icon">Icon</span>}
                    headerMeta={<div>Last analyzed · Today</div>}
                    headerActions={<button type="button">Action</button>}
                    footer={<div>Sticky footer</div>}
                    widthClassName="sm:w-[580px] lg:w-[660px]"
                    closeLabel="Close Ask AI drawer"
                    onClose={() => { closedCount += 1; }}
                >
                    Main body
                </SideDrawer>,
            );
        });
        const customDialog = walk(doc.body, (el) => el.getAttribute('role') === 'dialog')[0];
        assert.match(customDialog.className, /sm:w-\[580px\]/);
        assert.match(customDialog.className, /lg:w-\[660px\]/);
        assert.doesNotMatch(customDialog.textContent, /Recorded evidence/);
        assert.match(customDialog.textContent, /Icon/);
        assert.match(customDialog.textContent, /Last analyzed · Today/);
        assert.match(customDialog.textContent, /Action/);
        assert.match(customDialog.textContent, /Sticky footer/);
        const footerEl = walk(customDialog, (el) => el.tagName === 'FOOTER')[0];
        assert.ok(footerEl, 'Expected footer bar');
        assert.match(footerEl.className, /\bshrink-0\b/);
        assert.match(footerEl.className, /\bborder-t\b/);
        assert.match(footerEl.className, /\bborder-theme-border\b/);
        const customCloseBtn = walk(customDialog, (el) => el.getAttribute('aria-label') === 'Close Ask AI drawer')[0];
        assert.ok(customCloseBtn);

        win.dispatchKeyDown({ key: 'Escape', preventDefault() {}, stopPropagation() {} });
        assert.equal(closedCount, 1);

        await act(async () => {
            root.render(
                <SideDrawer isOpen={false} title="Closed" onClose={() => {}}>
                    Closed
                </SideDrawer>,
            );
        });
        assert.notEqual(doc.body.style.overflow, 'hidden');
    });
});

describe("Ask AI localStorage persistence", () => {
    test('round-trips per taskId, isolates tasks, caps LRU at ASK_AI_MAX_SAVED_TASKS, and clears cleanly', () => {
        assert.equal(ASK_AI_STORAGE_KEY, 'lens.simulation.askAi.v1');
        assert.equal(ASK_AI_MAX_SAVED_TASKS, 30);

        const storage = createMemoryStorage();
        const savedA = saveAskAiAnalysis('task-a', {
            messages: [
                { role: 'assistant', content: 'Decode queue bottleneck', providerName: 'OpenAI', model: 'gpt-4o', generatedAt: '2026-01-01T00:00:00Z' },
                { role: 'user', content: 'How to tune?' },
            ],
            providerId: 'prov-1',
            model: 'gpt-4o',
            updatedAt: '2026-01-01T00:00:00Z',
        }, storage);
        assert.ok(savedA);

        saveAskAiAnalysis('task-b', {
            messages: [{ role: 'assistant', content: 'Prefill bound' }],
            providerId: 'prov-2',
            model: 'claude-3-7',
            updatedAt: '2026-01-02T00:00:00Z',
        }, storage);

        const loadedA = loadSavedAskAiAnalysis('task-a', storage);
        const loadedB = loadSavedAskAiAnalysis('task-b', storage);
        assert.equal(loadedA.messages.length, 2);
        assert.equal(loadedA.messages[0].content, 'Decode queue bottleneck');
        assert.equal(loadedA.providerId, 'prov-1');
        assert.equal(loadedA.model, 'gpt-4o');
        assert.equal(loadedB.messages.length, 1);
        assert.equal(loadedB.messages[0].content, 'Prefill bound');
        assert.equal(loadSavedAskAiAnalysis('missing-task', storage), null);

        assert.equal(clearSavedAskAiAnalysis('task-a', storage), true);
        assert.equal(loadSavedAskAiAnalysis('task-a', storage), null);
        assert.equal(loadSavedAskAiAnalysis('task-b', storage)?.messages[0]?.content, 'Prefill bound');

        const lruStorage = createMemoryStorage();
        for (let i = 1; i <= 35; i += 1) {
            const ts = new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString();
            saveAskAiAnalysis(`sim-${i}`, {
                messages: [{ role: 'assistant', content: `Analysis ${i}` }],
                updatedAt: ts,
            }, lruStorage);
        }
        const storedMap = JSON.parse(lruStorage.getItem(ASK_AI_STORAGE_KEY));
        assert.equal(Object.keys(storedMap).length, ASK_AI_MAX_SAVED_TASKS);
        for (let i = 1; i <= 5; i += 1) {
            assert.equal(loadSavedAskAiAnalysis(`sim-${i}`, lruStorage), null);
        }
        for (let i = 6; i <= 35; i += 1) {
            assert.equal(loadSavedAskAiAnalysis(`sim-${i}`, lruStorage)?.messages[0]?.content, `Analysis ${i}`);
        }
    });

    test('handles corrupted JSON, invalid entries, missing window, and storage errors gracefully', () => {
        assert.equal(loadSavedAskAiAnalysis('task-1', null), null);
        assert.equal(saveAskAiAnalysis('task-1', { messages: [{ role: 'assistant', content: 'hi' }] }, null), null);
        assert.equal(clearSavedAskAiAnalysis('task-1', null), false);

        const corruptStorage = createMemoryStorage();
        corruptStorage.setItem(ASK_AI_STORAGE_KEY, '{not valid json');
        assert.equal(loadSavedAskAiAnalysis('task-1', corruptStorage), null);

        corruptStorage.setItem(ASK_AI_STORAGE_KEY, JSON.stringify({
            'task-empty': { messages: [] },
            'task-bad-msg': { messages: [{ role: 'system', content: 'x' }, { role: 'assistant', content: 123 }] },
        }));
        assert.equal(loadSavedAskAiAnalysis('task-empty', corruptStorage), null);
        assert.equal(loadSavedAskAiAnalysis('task-bad-msg', corruptStorage), null);

        const quotaMap = new Map();
        let failCount = 0;
        const quotaStorage = {
            getItem(k) { return quotaMap.has(k) ? quotaMap.get(k) : null; },
            setItem(k, v) {
                const parsed = JSON.parse(v);
                if (Object.keys(parsed).length > 1) {
                    failCount += 1;
                    const err = new Error('QuotaExceededError');
                    err.name = 'QuotaExceededError';
                    throw err;
                }
                quotaMap.set(k, v);
            },
            removeItem(k) { quotaMap.delete(k); },
        };
        saveAskAiAnalysis('old-task', {
            messages: [{ role: 'assistant', content: 'Old' }],
            updatedAt: '2026-01-01T00:00:00Z',
        }, quotaStorage);
        const savedNew = saveAskAiAnalysis('new-task', {
            messages: [{ role: 'assistant', content: 'New' }],
            updatedAt: '2026-01-02T00:00:00Z',
        }, quotaStorage);
        assert.ok(failCount >= 1);
        assert.ok(savedNew);
        assert.equal(loadSavedAskAiAnalysis('new-task', quotaStorage)?.messages[0]?.content, 'New');

        const throwingStorage = {
            getItem() { throw new Error('SecurityError'); },
            setItem() { throw new Error('SecurityError'); },
            removeItem() { throw new Error('SecurityError'); },
        };
        assert.equal(loadSavedAskAiAnalysis('task-1', throwingStorage), null);
        assert.equal(saveAskAiAnalysis('task-1', { messages: [{ role: 'assistant', content: 'x' }] }, throwingStorage), null);
        assert.equal(clearSavedAskAiAnalysis('task-1', throwingStorage), false);
    });
});

describe("AskAiModal right drawer and restoration integration", () => {
    test('restores saved analysis inside full-height SideDrawer without auto-firing, and clears without re-running', async (t) => {
        const storage = createMemoryStorage();
        saveAskAiAnalysis('sim-saved-1', {
            messages: [
                {
                    role: 'assistant',
                    content: 'Saved diagnosis: KV cache pressure is primary bottleneck.',
                    providerName: 'OpenAI Primary',
                    model: 'gpt-4o-mini',
                    generatedAt: '2026-05-01T10:00:00Z',
                    telemetryScope: 'full',
                },
            ],
            providerId: 'prov-2',
            model: 'gpt-4o-mini',
            updatedAt: '2026-05-01T10:00:00Z',
        }, storage);

        saveAskAiAnalysis('sim-deleted-provider-3', {
            messages: [{ role: 'assistant', content: 'Older analysis with deleted provider.' }],
            providerId: 'prov-deleted',
            model: 'stale-model',
            updatedAt: '2026-05-01T08:00:00Z',
        }, storage);

        const fetchCalls = [];
        const analysisBodies = [];
        const { root, doc, walk, reactProps, flush } = createDrawerTestEnv(t, {
            storage,
            fetchHandler: async (url, init) => {
                fetchCalls.push(String(url));
                if (String(url).includes('/api/v1/ai-providers')) {
                    return Response.json({
                        items: [
                            { id: 'prov-1', name: 'Anthropic', model: 'claude-3-7', provider_type: 'anthropic' },
                            { id: 'prov-2', name: 'OpenAI Primary', model: 'gpt-4o', provider_type: 'openai' },
                        ],
                    });
                }
                if (String(url).includes('/ai-analysis')) {
                    analysisBodies.push(JSON.parse(init?.body || '{}'));
                    return Response.json({
                        analysis: 'Fresh diagnosis for new task.',
                        provider_name: 'Anthropic',
                        model: 'claude-3-7',
                        generated_at: '2026-05-02T12:00:00Z',
                        telemetry_scope: 'full',
                    });
                }
                return Response.json({});
            },
        });

        await act(async () => {
            root.render(
                <AskAiModal
                    isOpen
                    onClose={() => {}}
                    task={{ id: 'sim-saved-1', model_name: 'Qwen/Qwen3-8B' }}
                    taskId="sim-saved-1"
                    onNavigate={() => {}}
                />,
            );
        });
        await flush();

        const dialog = walk(doc.body, (el) => el.getAttribute('role') === 'dialog')[0];
        assert.ok(dialog, 'Expected AskAiModal to render SideDrawer dialog');
        assert.match(dialog.className, /\bbenchmark-inspector-drawer\b/);
        assert.match(dialog.className, /\binset-y-0\b/);
        assert.match(dialog.className, /\bright-0\b/);
        assert.match(dialog.className, /\bh-dvh\b/);
        assert.match(dialog.textContent, /Saved diagnosis: KV cache pressure is primary bottleneck\./);
        assert.match(dialog.textContent, /Last analyzed · /);
        assert.match(dialog.textContent, /Re-analyze/);
        assert.match(dialog.textContent, /Clear/);

        const footer = walk(dialog, (el) => el.tagName === 'FOOTER')[0];
        assert.ok(footer, 'Expected sticky SideDrawer footer composer');
        assert.match(footer.textContent, /Quick prompts:/);
        assert.match(footer.textContent, /Send/);
        assert.ok(walk(footer, (el) => el.tagName === 'TEXTAREA').length === 1);

        assert.equal(fetchCalls.filter((u) => u.includes('/ai-analysis')).length, 0);

        const providerSelect = walk(dialog, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'ask-ai-provider-select')[0];
        assert.equal(providerSelect.value ?? reactProps(providerSelect).value, 'prov-2');
        const modelInput = walk(dialog, (el) => el.tagName === 'INPUT' && el.getAttribute('id') === 'ask-ai-model-input')[0];
        assert.equal(modelInput.value ?? reactProps(modelInput).value, 'gpt-4o-mini');

        const clearBtn = walk(dialog, (el) => el.tagName === 'BUTTON' && /\bClear\b/.test(el.textContent))[0];
        assert.ok(clearBtn, 'Expected Clear button');
        await act(async () => {
            reactProps(clearBtn).onClick?.({ preventDefault() {}, stopPropagation() {} });
        });
        await flush();

        assert.equal(loadSavedAskAiAnalysis('sim-saved-1', storage), null);
        assert.doesNotMatch(doc.body.textContent, /Saved diagnosis: KV cache pressure/);
        assert.equal(fetchCalls.filter((u) => u.includes('/ai-analysis')).length, 0);

        await act(async () => {
            root.render(
                <AskAiModal
                    isOpen
                    onClose={() => {}}
                    task={{ id: 'sim-fresh-2', model_name: 'Qwen/Qwen3-8B' }}
                    taskId="sim-fresh-2"
                    onNavigate={() => {}}
                />,
            );
        });
        await flush();

        assert.equal(fetchCalls.filter((u) => u.includes('/sim-fresh-2/ai-analysis')).length, 1);
        assert.deepEqual(analysisBodies[0], { provider_id: 'prov-1', model: 'claude-3-7' });
        assert.equal(providerSelect.value ?? reactProps(providerSelect).value, 'prov-1');
        assert.equal(modelInput.value ?? reactProps(modelInput).value, 'claude-3-7');
        assert.match(doc.body.textContent, /Fresh diagnosis for new task\./);
        assert.equal(loadSavedAskAiAnalysis('sim-fresh-2', storage)?.messages[0]?.content, 'Fresh diagnosis for new task.');

        await act(async () => {
            root.render(
                <AskAiModal
                    isOpen
                    onClose={() => {}}
                    task={{ id: 'sim-deleted-provider-3', model_name: 'Qwen/Qwen3-8B' }}
                    taskId="sim-deleted-provider-3"
                    onNavigate={() => {}}
                />,
            );
        });
        await flush();

        assert.equal(providerSelect.value ?? reactProps(providerSelect).value, 'prov-1');
        assert.equal(modelInput.value ?? reactProps(modelInput).value, 'claude-3-7');
        assert.match(doc.body.textContent, /Older analysis with deleted provider\./);
        assert.equal(fetchCalls.filter((u) => u.includes('/sim-deleted-provider-3/ai-analysis')).length, 0);
    });

    test('switching taskId while providers are still loading resolves cleanly without wedging', async (t) => {
        const storage = createMemoryStorage();
        let resolveProviders;
        const providersPromise = new Promise((resolve) => {
            resolveProviders = resolve;
        });
        const fetchCalls = [];
        const analysisBodies = [];

        const { root, doc, flush } = createDrawerTestEnv(t, {
            storage,
            fetchHandler: async (url, init) => {
                fetchCalls.push(String(url));
                if (String(url).includes('/api/v1/ai-providers')) {
                    await providersPromise;
                    return Response.json({
                        items: [
                            { id: 'prov-1', name: 'Anthropic', model: 'claude-3-7', provider_type: 'anthropic' },
                        ],
                    });
                }
                if (String(url).includes('/ai-analysis')) {
                    analysisBodies.push(JSON.parse(init?.body || '{}'));
                    return Response.json({
                        analysis: 'Completed after task switch.',
                        provider_name: 'Anthropic',
                        model: 'claude-3-7',
                        generated_at: '2026-05-02T12:05:00Z',
                    });
                }
                return Response.json({});
            },
        });

        await act(async () => {
            root.render(
                <AskAiModal
                    isOpen
                    onClose={() => {}}
                    task={{ id: 'sim-pending-1', model_name: 'Qwen/Qwen3-8B' }}
                    taskId="sim-pending-1"
                    onNavigate={() => {}}
                />,
            );
        });
        assert.match(doc.body.textContent, /Loading External AI Providers/);

        await act(async () => {
            root.render(
                <AskAiModal
                    isOpen
                    onClose={() => {}}
                    task={{ id: 'sim-pending-2', model_name: 'Qwen/Qwen3-8B' }}
                    taskId="sim-pending-2"
                    onNavigate={() => {}}
                />,
            );
        });

        resolveProviders();
        await flush();
        await flush();

        assert.equal(fetchCalls.filter((u) => u.includes('/sim-pending-1/ai-analysis')).length, 0);
        assert.equal(fetchCalls.filter((u) => u.includes('/sim-pending-2/ai-analysis')).length, 1);
        assert.deepEqual(analysisBodies[0], { provider_id: 'prov-1', model: 'claude-3-7' });
        assert.match(doc.body.textContent, /Completed after task switch\./);
        assert.equal(loadSavedAskAiAnalysis('sim-pending-2', storage)?.messages[0]?.content, 'Completed after task switch.');
    });
});

describe("SimulationTaskRow structured layout and live telemetry curve", () => {
    test('renders EvaluationTaskCard-style row, overflow-visible KPI grid, error banner, and prevents <details> click from opening row', async (t) => {
        const completedTask = {
            id: 'sim-done-1',
            name: 'Qwen3-8B Chat Replay',
            status: 'completed',
            scenario: 'chat',
            model_name: 'Qwen/Qwen3-8B',
            target_endpoint_name: 'qwen-prod-svc',
            dataset: 'sharegpt-100',
            created_at: '2026-05-01T12:00:00Z',
            started_at: '2026-05-01T12:00:00Z',
            completed_at: '2026-05-01T12:01:05Z',
            simulation: {
                backend: 'aiperf',
                duration_seconds: 60,
                backend_options: {
                    concurrency: 16,
                    warmup_seconds: 10,
                    ttft_slo: 0.5,
                    tpot_slo: 0.05,
                    error_rate_slo: 5,
                },
            },
            result: {
                summary: {
                    throughput_rps: 18.4,
                    ttft: { mean_ms: 140 },
                    tpot: { mean_ms: 22.5 },
                    total_requests: 1000,
                    failed_requests: 10,
                },
            },
        };

        const html = renderToStaticMarkup(<SimulationTaskRow item={completedTask} />);
        assert.match(html, /\bh-9 w-9\b/);
        assert.match(html, /data-tone="success"/);
        assert.match(html, /simulation-task-card/);
        assert.match(html, /sim-status-badge/);
        assert.match(html, /sim-scenario-chip/);
        assert.match(html, /sim-workload-chip/);
        assert.match(html, /sim-pacing-fact/);
        assert.match(html, /sim-kpi-tile/);
        assert.match(html, /sim-action-menu-trigger/);
        assert.match(html, /sim-action-menu-popover/);
        assert.match(html, /Qwen3-8B Chat Replay/);
        assert.match(html, /Qwen\/Qwen3-8B · qwen-prod-svc/);
        assert.match(html, /Chat/);
        assert.match(html, /Aiperf/);
        assert.match(html, /sharegpt-100/);
        assert.match(html, /Duration 1 min/);
        assert.match(html, /Concurrency 16/);
        assert.match(html, /overflow-visible rounded-lg border/);
        assert.doesNotMatch(html, /grid-cols-2 gap-px overflow-hidden/);
        assert.match(html, /title="Saved summary · 1 min 5 s"/);
        assert.doesNotMatch(html, /<p[^>]*>Saved summary · 1 min 5 s<\/p>/);
        assert.match(html, /<summary[^>]*>Task details<\/summary>/);
        assert.doesNotMatch(html, /Task details · qwen-prod-svc/);
        assert.match(html, /View Results/);
        assert.match(html, /Rerun/);
        assert.match(html, /Delete/);

        const failedHtml = renderToStaticMarkup(
            <SimulationTaskRow
                item={{
                    id: 'sim-fail-1',
                    name: 'Failed Run',
                    status: 'failed',
                    error_message: 'Upstream endpoint returned HTTP 503',
                }}
            />,
        );
        assert.match(failedHtml, /bg-rose-500\/10/);
        assert.match(failedHtml, /Upstream endpoint returned HTTP 503/);

        const { root, doc, walk, reactProps } = createDrawerTestEnv(t);
        const opened = [];
        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={completedTask}
                    onOpen={(row) => opened.push(row.id)}
                    onRerun={() => {}}
                    onDelete={() => {}}
                />,
            );
        });

        const article = walk(doc.body, (el) => el.tagName === 'ARTICLE')[0];
        const detailsEl = walk(article, (el) => el.tagName === 'DETAILS')[0];
        const summaryEl = walk(article, (el) => el.tagName === 'SUMMARY')[0];
        assert.ok(article && detailsEl && summaryEl);

        let detailsStopped = false;
        reactProps(detailsEl).onClick?.({
            target: detailsEl,
            stopPropagation() { detailsStopped = true; },
        });
        assert.equal(detailsStopped, true);
        assert.equal(opened.length, 0);

        let summaryStopped = false;
        reactProps(summaryEl).onClick?.({
            target: summaryEl,
            stopPropagation() { summaryStopped = true; },
        });
        assert.equal(summaryStopped, true);
        assert.equal(opened.length, 0);

        // Guard also checks target.closest('button, details, summary, input, label, a')
        reactProps(article).onClick?.({
            target: { closest: (sel) => (sel.includes('details') ? detailsEl : null) },
        });
        assert.equal(opened.length, 0);

        let anchorChecked = '';
        reactProps(article).onClick?.({
            target: {
                closest: (sel) => {
                    anchorChecked = sel;
                    return sel.includes(', a') ? {} : null;
                },
            },
        });
        assert.match(anchorChecked, /\ba\b/);
        assert.equal(opened.length, 0);

        // Clicking on the row body itself opens details
        reactProps(article).onClick?.({
            target: { closest: () => null },
        });
        assert.deepEqual(opened, ['sim-done-1']);
    });

    test('renders live telemetry curve and server_fallback KPIs for running tasks, and placeholder when timeline is empty', () => {
        const mergedTimeline = buildLiveTaskTimeline({
            completion_timeline: [
                { start_seconds: 0, end_seconds: 5, arrived_requests: 50, completed_requests: 48, successful_requests: 47, failed_requests: 1 },
            ],
            throughput_timeline: [
                { start_seconds: 0, end_seconds: 5, request_completion_rps: 9.6, token_throughput_tps: 310.5 },
            ],
        });
        assert.equal(mergedTimeline.length, 1);
        assert.equal(mergedTimeline[0].request_completion_rps, 9.6);
        assert.equal(mergedTimeline[0].token_throughput_tps, 310.5);
        assert.equal(mergedTimeline[0].arrived_rps, 10);

        // Early-scrape server_fallback points (t=0.05 and t=0.0) prefer explicit rate fields instead of dividing by tiny span
        const earlyScrapeTimeline = buildLiveTaskTimeline({
            completion_timeline: [
                {
                    start_seconds: 0,
                    end_seconds: 0,
                    arrived_requests: 8,
                    completed_requests: 0,
                    successful_requests: 0,
                    failed_requests: 1,
                    request_completion_rps: 0,
                    request_arrival_rps: 1.6,
                    failed_rps: 0.2,
                },
                {
                    start_seconds: 0,
                    end_seconds: 0.05,
                    arrived_requests: 8.5,
                    completed_requests: 1,
                    successful_requests: 1,
                    failed_requests: 0,
                    request_completion_rps: 2.0,
                    request_arrival_rps: 3.6,
                    failed_rps: 0.1,
                },
            ],
            throughput_timeline: [
                { start_seconds: 0, end_seconds: 0, request_completion_rps: 0, request_arrival_rps: 1.6, failed_rps: 0.2, token_throughput_tps: 0 },
                { start_seconds: 0, end_seconds: 0.05, request_completion_rps: 2.0, request_arrival_rps: 3.6, failed_rps: 0.1, token_throughput_tps: 80 },
            ],
        });
        assert.equal(earlyScrapeTimeline[0].arrived_rps, 1.6);
        assert.equal(earlyScrapeTimeline[0].failed_rps, 0.2);
        assert.equal(earlyScrapeTimeline[1].arrived_rps, 3.6);
        assert.equal(earlyScrapeTimeline[1].failed_rps, 0.1);

        const tpOnlyTimeline = buildLiveTaskTimeline({
            throughput_timeline: [
                { start_seconds: 0, end_seconds: 5, request_completion_rps: 12.2, token_throughput_tps: 405 },
            ],
        });
        assert.equal(tpOnlyTimeline.length, 1);
        assert.equal(tpOnlyTimeline[0].request_completion_rps, 12.2);

        const runningServerFallbackTask = {
            id: 'sim-live-aiperf',
            name: 'AIPerf Live Run',
            status: 'running',
            scenario: 'coding',
            model_name: 'Qwen/Qwen3-32B',
            progress_percent: 98,
            progress_message: 'Finalizing telemetry',
            simulation: {
                backend: 'aiperf',
                duration_seconds: 120,
                backend_options: { concurrency: 32 },
            },
            live_summary: {
                telemetry_source: 'server_fallback',
                elapsed_seconds: 24,
                progress_percent: 45,
                throughput_rps: 14.5,
                throughput_tps: 420.2,
                ttft: { p50_ms: 88.4, p90_ms: 132.0 },
                tpot: { p50_ms: 19.2, p90_ms: 28.5 },
                error_rate_pct: 1.5,
                in_flight: 18,
                completion_timeline: [
                    {
                        start_seconds: 0,
                        end_seconds: 10,
                        arrived_requests: 0,
                        completed_requests: 0,
                        successful_requests: 0,
                        failed_requests: 0,
                        request_completion_rps: 14.5,
                        request_arrival_rps: 16.3,
                        failed_rps: 0.2,
                        token_throughput_tps: 420.2,
                        in_flight: 18,
                    },
                ],
                throughput_timeline: [
                    { start_seconds: 0, end_seconds: 10, request_completion_rps: 14.5, token_throughput_tps: 420.2 },
                ],
            },
        };

        const resolved = resolveRowMetrics(
            runningServerFallbackTask,
            'running',
            buildLiveTaskTimeline(runningServerFallbackTask.live_summary),
        );
        assert.equal(resolved.requestRate, 14.5);
        assert.equal(resolved.ttft, 88.4);
        assert.equal(resolved.tpot, 19.2);
        assert.equal(resolved.errorRate, 1.5);

        const liveHtml = renderToStaticMarkup(<SimulationTaskRow item={runningServerFallbackTask} />);
        assert.match(liveHtml, /data-testid="live-telemetry-curve"/);
        assert.match(liveHtml, /\bh-14\b/);
        assert.match(liveHtml, /LIVE/);
        assert.match(liveHtml, /Server telemetry/);
        assert.match(liveHtml, /aria-valuenow="98"/);
        assert.match(liveHtml, /14\.50 req\/s/);
        assert.match(liveHtml, /420\.20 tok\/s/);
        assert.match(liveHtml, /88\.40 ms/);
        assert.match(liveHtml, /19\.20 ms/);
        assert.match(liveHtml, /1\.50%/);
        assert.match(liveHtml, /title="Live telemetry · 24 s · server fallback"/);
        assert.doesNotMatch(liveHtml, /<p[^>]*>Live telemetry · 24 s · server fallback<\/p>/);
        assert.doesNotMatch(liveHtml, /Sampling live curve…/);

        const emptyCurveTask = {
            id: 'sim-live-empty',
            name: 'Warming Up Run',
            status: 'running',
            scenario: 'chat',
            model_name: 'Qwen/Qwen3-8B',
            live_summary: {
                telemetry_source: 'none',
                elapsed_seconds: 3,
                completion_timeline: [],
                throughput_timeline: [],
            },
        };
        const emptyHtml = renderToStaticMarkup(<SimulationTaskRow item={emptyCurveTask} />);
        assert.match(emptyHtml, /data-testid="live-telemetry-curve"/);
        assert.match(emptyHtml, /LIVE/);
        assert.match(emptyHtml, /Sampling live curve…/);
        assert.match(emptyHtml, /title="Live telemetry · 3 s"/);
        assert.doesNotMatch(emptyHtml, /<p[^>]*>Live telemetry · 3 s<\/p>/);

        const queuedHtml = renderToStaticMarkup(
            <SimulationTaskRow
                item={{
                    id: 'sim-queued-1',
                    name: 'Queued Run',
                    status: 'queued',
                    scenario: 'chat',
                    model_name: 'Qwen/Qwen3-8B',
                    progress_percent: 5,
                    progress_message: 'Waiting for runner slot',
                }}
            />,
        );
        assert.doesNotMatch(queuedHtml, /data-testid="live-telemetry-curve"/);
        assert.doesNotMatch(queuedHtml, /LIVE/);
        assert.match(queuedHtml, /title="Queued · waiting to start"/);
        assert.doesNotMatch(queuedHtml, /<p[^>]*>Queued · waiting to start<\/p>/);
        assert.match(queuedHtml, /role="progressbar"/);
        assert.match(queuedHtml, /Stop/);
    });

    test('t74 palette tones, KPI SLO colors, formula tooltips, and CSS tooltip ink guard', () => {
        const toneCases = [
            ['completed', 'success'],
            ['succeeded', 'success'],
            ['failed', 'danger'],
            ['cancelled', 'danger'],
            ['canceled', 'danger'],
            ['running', 'active'],
            ['queued', 'active'],
            ['pending', 'active'],
            ['stopped', 'neutral'],
        ];
        for (const [status, expectedTone] of toneCases) {
            const rowHtml = renderToStaticMarkup(
                <SimulationTaskRow item={{ id: `t-${status}`, name: `Task ${status}`, status }} />,
            );
            assert.match(rowHtml, new RegExp(`data-tone="${expectedTone}"`));
        }

        // Within SLO -> neutral ink (text-slate-900 dark:text-slate-100), not emerald
        const withinSloHtml = renderToStaticMarkup(
            <SimulationTaskRow
                item={{
                    id: 'sim-slo-pass',
                    name: 'SLO Pass',
                    status: 'completed',
                    simulation: {
                        backend: 'aiperf',
                        backend_options: { ttft_slo: 0.5, tpot_slo: 0.05, error_rate_slo: 5 },
                    },
                    result: {
                        summary: {
                            throughput_rps: 20,
                            ttft: { mean_ms: 120 },
                            tpot: { mean_ms: 20 },
                            total_requests: 100,
                            failed_requests: 2,
                        },
                    },
                }}
            />,
        );
        const kpiMatches = [...withinSloHtml.matchAll(/<p class="mt-1\.5 truncate font-mono text-sm font-semibold tabular-nums ([^"]+)">/g)]
            .map((m) => m[1]);
        assert.equal(kpiMatches.length, 4);
        for (const cls of kpiMatches) {
            assert.equal(cls, 'text-slate-900 dark:text-slate-100');
            assert.doesNotMatch(cls, /emerald/);
        }

        // Breach SLO & non-zero error without SLO -> rose ink; null values -> muted ink
        const breachHtml = renderToStaticMarkup(
            <SimulationTaskRow
                item={{
                    id: 'sim-slo-breach',
                    name: 'SLO Breach',
                    status: 'completed',
                    simulation: {
                        backend: 'aiperf',
                        backend_options: { ttft_slo: 0.1, tpot_slo: 0.01 },
                    },
                    result: {
                        summary: {
                            ttft: { mean_ms: 250 },
                            tpot: { mean_ms: 30 },
                            total_requests: 100,
                            failed_requests: 1,
                        },
                    },
                }}
            />,
        );
        const breachMatches = [...breachHtml.matchAll(/<p class="mt-1\.5 truncate font-mono text-sm font-semibold tabular-nums ([^"]+)">/g)]
            .map((m) => m[1]);
        assert.equal(breachMatches[0], 'text-slate-400 dark:text-slate-600');
        assert.equal(breachMatches[1], 'text-rose-600 dark:text-rose-300');
        assert.equal(breachMatches[2], 'text-rose-600 dark:text-rose-300');
        assert.equal(breachMatches[3], 'text-rose-600 dark:text-rose-300');

        // 5 self-evident row/chip HelpTooltips removed; only 4 KPI formula tooltips remain with justify-between and opacity-60
        for (const removedLabel of ['Task status help', 'Scenario help', 'Backend help', 'Dataset help', 'Created help']) {
            assert.doesNotMatch(withinSloHtml, new RegExp(removedLabel));
        }
        assert.match(
            withinSloHtml,
            /<div class="flex min-w-0 items-center gap-2"><h3[^>]*>SLO Pass<\/h3><span class="sim-status-badge[^"]*">Completed<\/span><\/div>/,
        );
        for (const kpiHelpLabel of ['Request throughput help', 'TTFT help', 'TPOT help', 'Error rate help']) {
            assert.match(
                withinSloHtml,
                new RegExp(`<div class="flex items-center justify-between gap-1 text-\\[10px\\] font-medium[^"]*"><span class="truncate">[^<]+<\\/span><span class="group inline-flex relative opacity-60 transition-opacity hover:opacity-100 focus-within:opacity-100"><span[^>]*aria-label="${kpiHelpLabel}"`),
            );
        }
        assert.match(withinSloHtml, /Throughput \(req\/s\) = Completed_Requests \/ Effective_Window_s/);
        assert.match(withinSloHtml, /TTFT \(ms\) = mean\(t_first_token/);
        assert.match(withinSloHtml, /TPOT \(ms\) = mean\(/);
        assert.match(withinSloHtml, /Error_Rate \(%\) = \(Failed_Requests/);

        // CSS tooltip ink guard & no dead action button rules
        const cssPath = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../app/styles/index.css');
        const cssSource = fs.readFileSync(cssPath, 'utf8');
        assert.doesNotMatch(cssSource, /\.sim-scenario-chip\s*\*/);
        assert.doesNotMatch(cssSource, /\.sim-action-(?:retry|cancel|delete)\b/);
        assert.match(cssSource, /\.dark\s+\.simulation-task-card\s*\{\s*background-color:\s*#0c1422;/);
        assert.match(cssSource, /\.dark\s+\.simulation-task-card\s+\.sim-kpi-tile/);
        assert.match(cssSource, /html:not\(\.dark\)\s+\.simulation-page\s+\.simulation-task-card\s+\.sim-action-menu-popover[\s\S]*box-shadow:[^;]+!important;/);
    });

    test('t75 Task details shows only non-duplicated technical facts', () => {
        const html = renderToStaticMarkup(
            <SimulationTaskRow
                item={{
                    id: 'sim-t75-1',
                    name: 'Trace Replay Task',
                    description: 'Check prefix cache hit behavior under load',
                    status: 'completed',
                    scenario: 'chat',
                    endpoint_mode: 'deployment',
                    target_endpoint_name: 'qwen-prod-svc',
                    dataset: 'sharegpt-100',
                    trace_path: '/data/traces/sharegpt-100.jsonl',
                    trace_format: 'sharegpt_jsonl',
                    created_at: '2026-05-01T12:00:00Z',
                    simulation: {
                        backend: 'aiperf',
                        duration_seconds: 60,
                        grace_period_seconds: 15,
                        endpoint_url: 'http://qwen-prod-svc.default.svc:8000',
                        backend_options: {
                            concurrency: 8,
                            warmup_seconds: 10,
                            ttft_slo: 0.5,
                            tpot_slo: 0.05,
                            e2e_slo: 2.5,
                            error_rate_slo: 1,
                            max_context_length: 4096,
                            random_seed: 42,
                            num_profile_runs: 3,
                        },
                    },
                }}
            />,
        );

        const detailsStart = html.indexOf('<details');
        assert.ok(detailsStart > 0, 'Expected <details> block');
        const cardFaceHtml = html.slice(0, detailsStart);
        const detailsMatch = html.match(/<details[^>]*>([\s\S]*?)<\/details>/);
        assert.ok(detailsMatch, 'Expected <details> block');
        const detailsHtml = detailsMatch[1];

        const createdAtFormatted = new Date('2026-05-01T12:00:00Z').toLocaleString();
        assert.doesNotMatch(cardFaceHtml, /Created/);
        assert.match(detailsHtml, /<summary[^>]*>Task details<\/summary>/);

        const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const expectDetail = (source, label, value, extraValueClass = '') => {
            const clsGuard = extraValueClass ? `(?=[^>]*${escapeRegex(extraValueClass)})` : '';
            assert.match(
                source,
                new RegExp(`<span[^>]*>${escapeRegex(label)}:<\\/span><span${clsGuard}[^>]*>${escapeRegex(String(value))}<\\/span>`),
            );
        };

        expectDetail(detailsHtml, 'Created', createdAtFormatted);
        expectDetail(detailsHtml, 'ID', 'sim-t75-1');
        expectDetail(detailsHtml, 'Endpoint mode', 'Published service');
        expectDetail(detailsHtml, 'Schedule', 'Concurrency slots');
        expectDetail(detailsHtml, 'Endpoint URL', 'http://qwen-prod-svc.default.svc:8000', 'sim-endpoint-link');
        expectDetail(detailsHtml, 'Trace path', '/data/traces/sharegpt-100.jsonl');
        expectDetail(detailsHtml, 'Trace format', 'sharegpt_jsonl');
        expectDetail(detailsHtml, 'Warmup', '10 s');
        expectDetail(detailsHtml, 'Drain grace', '15 s');
        expectDetail(detailsHtml, 'SLOs', 'TTFT ≤ 500 ms · TPOT ≤ 50 ms · E2E ≤ 2.50 s · Error ≤ 1%', 'break-words');
        assert.match(detailsHtml, /Max context:<\/span><span[^>]*>4,?096<\/span>/);
        expectDetail(detailsHtml, 'Seed', '42');
        expectDetail(detailsHtml, 'Profile runs', '3');
        expectDetail(detailsHtml, 'Description', 'Check prefix cache hit behavior under load', 'break-words');
        assert.match(detailsHtml, /sm:col-span-2 lg:col-span-3[^>]*><span[^>]*>Description:<\/span>/);

        // Must NOT repeat row-face facts inside <details>
        assert.doesNotMatch(detailsHtml, />Aiperf</);
        assert.doesNotMatch(detailsHtml, /Duration:/);
        assert.doesNotMatch(detailsHtml, /Concurrency 8/);

        // When tracePath === datasetName and endpointUrl === targetEndpoint, omit duplicates
        const dedupHtml = renderToStaticMarkup(
            <SimulationTaskRow
                item={{
                    id: 'sim-t75-2',
                    name: 'Dedup Task',
                    status: 'completed',
                    dataset: 'my-trace.jsonl',
                    trace_path: 'my-trace.jsonl',
                    endpoint_url: 'http://localhost:8000',
                    simulation: {
                        backend: 'trace-replayer',
                        endpoint_url: 'http://localhost:8000',
                        backend_options: { warmup_seconds: 0 },
                        grace_period_seconds: 0,
                    },
                }}
            />,
        );
        const dedupDetails = dedupHtml.match(/<details[^>]*>([\s\S]*?)<\/details>/)[1];
        expectDetail(dedupDetails, 'Schedule', 'Fixed schedule');
        assert.doesNotMatch(dedupDetails, /Endpoint URL:/);
        assert.doesNotMatch(dedupDetails, /Trace path:/);
        assert.doesNotMatch(dedupDetails, /Warmup:/);
        assert.doesNotMatch(dedupDetails, /Drain grace:/);
    });

    test('t76 Menu icon dropdown opens/closes on outside click and Escape, and dispatches actions without row onOpen', async (t) => {
        const { root, doc, walk, reactProps } = createDrawerTestEnv(t);
        const calls = { open: [], rerun: [], del: [], stop: [] };

        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={{ id: 'sim-menu-1', name: 'Menu Test Task', status: 'completed' }}
                    onOpen={(row) => calls.open.push(row.id)}
                    onRerun={(row) => calls.rerun.push(row.id)}
                    onDelete={(row) => calls.del.push(row.id)}
                />,
            );
        });

        const article = walk(doc.body, (el) => el.tagName === 'ARTICLE')[0];
        const trigger = walk(article, (el) => el.tagName === 'BUTTON' && el.getAttribute('aria-haspopup') === 'menu')[0];
        const menu = walk(article, (el) => el.getAttribute('role') === 'menu')[0];
        assert.ok(trigger && menu);
        assert.equal(trigger.getAttribute('aria-expanded'), 'false');
        assert.equal(menu.hasAttribute('hidden'), true);
        assert.doesNotMatch(article.className, /\bz-30\b/);
        assert.match(article.className, /\bhover:z-20\b/);
        assert.match(article.className, /\bfocus-within:z-20\b/);

        // Open menu
        await act(async () => {
            reactProps(trigger).onClick?.({ stopPropagation() {} });
        });
        assert.equal(trigger.getAttribute('aria-expanded'), 'true');
        assert.equal(menu.hasAttribute('hidden'), false);
        assert.match(article.className, /\bz-30\b/);
        assert.doesNotMatch(article.className, /\bhover:z-20\b/);
        assert.doesNotMatch(article.className, /\bfocus-within:z-20\b/);
        assert.equal(calls.open.length, 0);

        // Escape on menu container itself (before stopPropagation) closes and restores focus
        const menuContainer = trigger.parentNode;
        await act(async () => {
            reactProps(menuContainer).onKeyDown?.({ key: 'Escape', preventDefault() {}, stopPropagation() {} });
        });
        assert.equal(trigger.getAttribute('aria-expanded'), 'false');
        assert.equal(menu.hasAttribute('hidden'), true);
        assert.equal(doc.activeElement, trigger);

        // Document Escape also closes and restores focus to trigger
        doc.activeElement = null;
        await act(async () => {
            reactProps(trigger).onClick?.({ stopPropagation() {} });
        });
        await act(async () => {
            doc.dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {} });
        });
        assert.equal(trigger.getAttribute('aria-expanded'), 'false');
        assert.equal(menu.hasAttribute('hidden'), true);
        assert.equal(doc.activeElement, trigger);

        // Reopen and close via outside mousedown
        await act(async () => {
            reactProps(trigger).onClick?.({ stopPropagation() {} });
        });
        assert.equal(trigger.getAttribute('aria-expanded'), 'true');
        await act(async () => {
            doc.dispatchEvent({ type: 'mousedown', target: doc.body });
        });
        assert.equal(trigger.getAttribute('aria-expanded'), 'false');

        // Reopen and click Rerun + Delete + View Results
        const menuItems = () => walk(menu, (el) => el.getAttribute('role') === 'menuitem');
        await act(async () => {
            reactProps(trigger).onClick?.({ stopPropagation() {} });
        });
        const rerunBtn = menuItems().find((el) => el.textContent.includes('Rerun'));
        assert.ok(rerunBtn);
        await act(async () => {
            reactProps(rerunBtn).onClick?.({ stopPropagation() {} });
        });
        assert.deepEqual(calls.rerun, ['sim-menu-1']);
        assert.equal(calls.open.length, 0);
        assert.equal(trigger.getAttribute('aria-expanded'), 'false');

        await act(async () => {
            reactProps(trigger).onClick?.({ stopPropagation() {} });
        });
        const deleteBtn = menuItems().find((el) => el.textContent.includes('Delete'));
        assert.ok(deleteBtn);
        await act(async () => {
            reactProps(deleteBtn).onClick?.({ stopPropagation() {} });
        });
        assert.deepEqual(calls.del, ['sim-menu-1']);
        assert.equal(calls.open.length, 0);
        assert.equal(trigger.getAttribute('aria-expanded'), 'false');

        await act(async () => {
            reactProps(trigger).onClick?.({ stopPropagation() {} });
        });
        const viewBtn = menuItems().find((el) => el.textContent.includes('View Results'));
        assert.ok(viewBtn);
        await act(async () => {
            reactProps(viewBtn).onClick?.({ stopPropagation() {} });
        });
        assert.deepEqual(calls.open, ['sim-menu-1']);
        assert.equal(trigger.getAttribute('aria-expanded'), 'false');

        // Historical trace-replayer tasks disable Rerun with clear tooltip while keeping View Results & Delete enabled
        let legacyRerunCalls = 0;
        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={{
                        id: 'sim-menu-legacy',
                        name: 'Legacy Trace Replayer Task',
                        status: 'completed',
                        simulation: { backend: 'trace-replayer' },
                    }}
                    onOpen={(row) => calls.open.push(row.id)}
                    onRerun={() => { legacyRerunCalls += 1; }}
                    onDelete={(row) => calls.del.push(row.id)}
                />,
            );
        });
        const legacyTrigger = walk(doc.body, (el) => el.tagName === 'BUTTON' && el.getAttribute('aria-haspopup') === 'menu')[0];
        const legacyMenu = walk(doc.body, (el) => el.getAttribute('role') === 'menu')[0];
        await act(async () => {
            reactProps(legacyTrigger).onClick?.({ stopPropagation() {} });
        });
        const legacyItems = walk(legacyMenu, (el) => el.getAttribute('role') === 'menuitem');
        const legacyRerunBtn = legacyItems.find((el) => el.textContent.includes('Rerun'));
        assert.ok(legacyRerunBtn);
        assert.equal(legacyRerunBtn.hasAttribute('disabled'), true);
        assert.equal(legacyRerunBtn.getAttribute('title'), 'Legacy trace-replayer backend is no longer supported');
        await act(async () => {
            reactProps(legacyRerunBtn).onClick?.({ stopPropagation() {} });
        });
        assert.equal(legacyRerunCalls, 0);
        assert.equal(legacyItems.find((el) => el.textContent.includes('View Results')).hasAttribute('disabled'), false);
        assert.equal(legacyItems.find((el) => el.textContent.includes('Delete')).hasAttribute('disabled'), false);

        // Running task renders Live Monitor + Stop
        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={{ id: 'sim-menu-run', name: 'Running Menu Task', status: 'running' }}
                    onOpen={(row) => calls.open.push(row.id)}
                    onStop={(row) => calls.stop.push(row.id)}
                />,
            );
        });
        const runTrigger = walk(doc.body, (el) => el.tagName === 'BUTTON' && el.getAttribute('aria-haspopup') === 'menu')[0];
        const runMenu = walk(doc.body, (el) => el.getAttribute('role') === 'menu')[0];
        await act(async () => {
            reactProps(runTrigger).onClick?.({ stopPropagation() {} });
        });
        const runItems = walk(runMenu, (el) => el.getAttribute('role') === 'menuitem');
        assert.ok(runItems.some((el) => el.textContent.includes('Live Monitor')));
        const stopBtn = runItems.find((el) => el.textContent.includes('Stop'));
        assert.ok(stopBtn);
        await act(async () => {
            reactProps(stopBtn).onClick?.({ stopPropagation() {} });
        });
        assert.deepEqual(calls.stop, ['sim-menu-run']);

        // Disabled states for in-flight rerun/delete/stop and non-stoppable 'stopping' status
        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={{ id: 'sim-menu-busy', name: 'Busy Terminal Task', status: 'completed' }}
                    rerunningTaskId="sim-menu-busy"
                    deletingTaskId="sim-menu-busy"
                />,
            );
        });
        const busyItems = walk(doc.body, (el) => el.getAttribute('role') === 'menuitem');
        assert.equal(busyItems.find((el) => el.textContent.includes('Rerun')).hasAttribute('disabled'), true);
        assert.equal(busyItems.find((el) => el.textContent.includes('Delete')).hasAttribute('disabled'), true);

        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={{ id: 'sim-menu-stopping', name: 'Stopping Task', status: 'stopping' }}
                    stoppingTaskId="sim-menu-stopping"
                />,
            );
        });
        const stoppingItems = walk(doc.body, (el) => el.getAttribute('role') === 'menuitem');
        assert.equal(stoppingItems.find((el) => el.textContent.includes('Stop')).hasAttribute('disabled'), true);
    });

    test('t77 compare checkbox and running-row spacer sit in consistent w-4 shrink-0 slot so status icon and metadata chips left-align across rows', async (t) => {
        const { root, doc, walk } = createDrawerTestEnv(t);
        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={{
                        id: 'sim-align-1',
                        name: 'Alignment Check',
                        status: 'completed',
                        scenario: 'chat',
                        dataset: 'bailian-thinking',
                        simulation: { backend: 'aiperf' },
                    }}
                    onToggleCompare={() => {}}
                />,
            );
        });

        const checkbox = walk(doc.body, (el) => el.tagName === 'INPUT' && (el.type === 'checkbox' || el.getAttribute('type') === 'checkbox' || el.className?.includes('simulation-compare-check')))[0];
        const statusIconBox = walk(doc.body, (el) => el.className?.includes('sim-status-badge') && el.className?.includes('h-9 w-9'))[0];
        const scenarioChip = walk(doc.body, (el) => el.className?.includes('sim-scenario-chip'))[0];
        assert.ok(checkbox && statusIconBox && scenarioChip);

        // Checkbox wrapper's parent is the outer `flex min-w-0 flex-1 items-start gap-3` container,
        // whose second child is the inner `min-w-0 flex-1` column shared by Row 1 (icon) and Row 2 (chips).
        const checkboxWrapper = checkbox.parentNode;
        const outerLeftFlex = checkboxWrapper?.parentNode;
        const innerColumn = statusIconBox.parentNode?.parentNode;
        const chipStrip = scenarioChip.parentNode;

        assert.match(checkboxWrapper.className, /\bw-4\b/);
        assert.match(checkboxWrapper.className, /\bshrink-0\b/);
        assert.notEqual(checkboxWrapper.parentNode, statusIconBox.parentNode);
        assert.equal(innerColumn.parentNode, outerLeftFlex);
        assert.equal(chipStrip.parentNode, innerColumn);
        assert.equal(statusIconBox.parentNode.parentNode, chipStrip.parentNode);

        // Running row renders an aria-hidden w-4 shrink-0 spacer in the exact same slot
        await act(async () => {
            root.render(
                <SimulationTaskRow
                    item={{
                        id: 'sim-align-run',
                        name: 'Running Alignment Check',
                        status: 'running',
                        scenario: 'chat',
                        dataset: 'bailian-thinking',
                        simulation: { backend: 'aiperf' },
                    }}
                />,
            );
        });

        const runCheckboxes = walk(doc.body, (el) => el.tagName === 'INPUT' && (el.type === 'checkbox' || el.getAttribute('type') === 'checkbox'));
        assert.equal(runCheckboxes.length, 0);
        const runStatusIconBox = walk(doc.body, (el) => el.className?.includes('sim-status-badge') && el.className?.includes('h-9 w-9'))[0];
        const runScenarioChip = walk(doc.body, (el) => el.className?.includes('sim-scenario-chip'))[0];
        const runInnerColumn = runStatusIconBox.parentNode?.parentNode;
        const runOuterLeftFlex = runInnerColumn.parentNode;
        const runChipStrip = runScenarioChip.parentNode;
        const runSpacer = runOuterLeftFlex.childNodes[0];
        assert.ok(runSpacer && runSpacer !== runInnerColumn);
        assert.equal(runSpacer.getAttribute('aria-hidden'), 'true');
        assert.match(runSpacer.className, /\bw-4\b/);
        assert.match(runSpacer.className, /\bshrink-0\b/);
        assert.equal(runChipStrip.parentNode, runInnerColumn);
        assert.equal(runStatusIconBox.parentNode.parentNode, runChipStrip.parentNode);

        // Matching Tasks list wrapper uses flex flex-col gap-3
        const dashboardPath = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../pages/simulation/ui/SimulationDashboard.jsx');
        const dashboardSource = fs.readFileSync(dashboardPath, 'utf8');
        assert.match(dashboardSource, /<div className="flex flex-col gap-3">\s*\{tasks\.map\(/);
    });

    test('t85 SimulationTaskRow renders hardware chip when present and omits it when absent or blank', () => {
        const withHardwareHtml = renderToStaticMarkup(
            <SimulationTaskRow
                item={{
                    id: 'sim-hw-1',
                    name: 'Hardware Chip Task',
                    status: 'completed',
                    scenario: 'chat',
                    dataset: 'sharegpt-100',
                    hardware: 'Intel Arc Pro B60',
                    simulation: { backend: 'aiperf' },
                }}
            />,
        );
        assert.match(
            withHardwareHtml,
            /<span class="sim-workload-chip inline-flex max-w-56 items-center gap-1[^"]*" title="Intel Arc Pro B60"><svg[^>]*aria-hidden="true"[^>]*>[\s\S]*?<\/svg><span class="min-w-0 truncate">Intel Arc Pro B60<\/span><\/span>/,
        );
        // Zero '?' help tooltip on chips (t81) and no duplicate Hardware entry in <details> (t75)
        assert.doesNotMatch(withHardwareHtml, /Hardware help/);
        const detailsHtml = withHardwareHtml.match(/<details[^>]*>([\s\S]*?)<\/details>/)[1];
        assert.doesNotMatch(detailsHtml, /Hardware:/);

        for (const absentHardware of [null, undefined, '', '   ']) {
            const withoutHtml = renderToStaticMarkup(
                <SimulationTaskRow
                    item={{
                        id: 'sim-hw-none',
                        name: 'No Hardware Task',
                        status: 'completed',
                        scenario: 'chat',
                        dataset: 'sharegpt-100',
                        hardware: absentHardware,
                        simulation: { backend: 'aiperf' },
                    }}
                />,
            );
            const chipCount = (withoutHtml.match(/class="sim-workload-chip\b/g) || []).length;
            assert.equal(chipCount, 2);
            assert.doesNotMatch(withoutHtml, /lucide-cpu/);
        }
    });
});

describe("t85+t86 Simulation hardware filters and task details resolution", () => {
    test('SimulationDashboard query builder, facets merge, and hardware filter UI wiring', async (t) => {
        const queryWithHw = buildSimulationTasksQuery({
            page: 1,
            pageSize: 10,
            hardware: 'unused',
            advancedFilters: {
                backend: 'aiperf',
                model: 'Qwen/Qwen3-8B',
                dataset: 'sharegpt',
                hardware: 'Intel Arc Pro B60',
                traceFormat: '',
                endpoint: '',
                createdAfter: '',
                createdBefore: '',
            },
        });
        assert.equal(queryWithHw.get('hardware'), 'Intel Arc Pro B60');
        assert.equal(queryWithHw.get('backend'), 'aiperf');
        assert.equal(queryWithHw.get('limit'), '10');
        assert.equal(queryWithHw.get('offset'), '10');

        const queryWithoutHw = buildSimulationTasksQuery({
            advancedFilters: { backend: '', model: '', dataset: '', hardware: '' },
        });
        assert.equal(queryWithoutHw.has('hardware'), false);

        assert.deepEqual(mergeSimulationFacets(null).hardware, []);
        assert.deepEqual(
            mergeSimulationFacets({ hardware: ['Intel Arc Pro B60', 'NVIDIA H100 80GB HBM3'] }).hardware,
            ['Intel Arc Pro B60', 'NVIDIA H100 80GB HBM3'],
        );
        assert.deepEqual(
            resolveHardwareFacetOptions(['Intel Arc Pro B60'], 'NVIDIA H100 80GB HBM3'),
            ['NVIDIA H100 80GB HBM3', 'Intel Arc Pro B60'],
        );
        assert.deepEqual(
            resolveHardwareFacetOptions(['Intel Arc Pro B60'], 'Intel Arc Pro B60'),
            ['Intel Arc Pro B60'],
        );

        const taskRequests = [];
        const { root, doc, win, walk, reactProps, flush } = createDrawerTestEnv(t, {
            fetchHandler: async (url) => {
                const urlStr = String(url);
                if (urlStr.startsWith('/api/v1/simulation/tasks?')) {
                    taskRequests.push(urlStr);
                    return Response.json({
                        tasks: [
                            {
                                id: 'sim-dash-hw-1',
                                name: 'GPU Replay',
                                status: 'completed',
                                scenario: 'chat',
                                dataset: 'sharegpt',
                                hardware: 'NVIDIA H100 80GB HBM3',
                                simulation: { backend: 'aiperf' },
                            },
                        ],
                        total: 1,
                        counts: { queued: 0, running: 0, completed: 1, failed: 0, cancelled: 0 },
                        facets: {
                            backends: ['aiperf'],
                            models: ['Qwen/Qwen3-8B'],
                            datasets: ['sharegpt'],
                            hardware: ['Intel Arc Pro B60', 'NVIDIA H100 80GB HBM3'],
                            trace_formats: [],
                            endpoints: [],
                        },
                    });
                }
                if (urlStr.startsWith('/api/v1/simulation/backends')) return Response.json({ backends: [] });
                if (urlStr.startsWith('/api/v1/simulation/scenarios')) return Response.json({ scenarios: [] });
                if (urlStr.startsWith('/api/v1/simulation/trace-datasets')) return Response.json({ datasets: [] });
                if (urlStr.startsWith('/api/v1/model-services')) return Response.json({ items: [] });
                return Response.json({});
            },
        });
        win.location = {
            href: 'http://localhost/simulation',
            pathname: '/simulation',
            search: '',
            hash: '',
        };
        win.history = { replaceState() {} };
        win.setTimeout = globalThis.setTimeout.bind(globalThis);
        win.clearTimeout = globalThis.clearTimeout.bind(globalThis);

        await act(async () => {
            root.render(<SimulationDashboard onNavigate={() => {}} />);
        });
        await flush();
        await flush();

        const advBtn = walk(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.includes('Advanced Filters'))[0];
        assert.ok(advBtn, 'Expected Advanced Filters button');
        await act(async () => {
            reactProps(advBtn).onClick?.({});
        });

        const hwSelect = walk(doc.body, (el) => el.tagName === 'SELECT' && el.getAttribute('id') === 'simulation-filter-hardware')[0];
        assert.ok(hwSelect, 'Expected #simulation-filter-hardware select');
        const hwOptions = walk(hwSelect, (el) => el.tagName === 'OPTION').map((opt) => ({
            value: opt.getAttribute('value') ?? opt.attributes?.value ?? '',
            label: opt.textContent,
        }));
        assert.deepEqual(hwOptions, [
            { value: '', label: 'All hardware' },
            { value: 'Intel Arc Pro B60', label: 'Intel Arc Pro B60' },
            { value: 'NVIDIA H100 80GB HBM3', label: 'NVIDIA H100 80GB HBM3' },
        ]);

        await act(async () => {
            reactProps(hwSelect).onChange?.({ target: { value: 'Intel Arc Pro B60' } });
        });
        const applyBtn = walk(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.includes('Apply Filters'))[0];
        assert.ok(applyBtn);
        await act(async () => {
            reactProps(applyBtn).onClick?.({});
        });
        await flush();
        await flush();

        assert.ok(
            taskRequests.some((url) => new URL(url, 'http://localhost').searchParams.get('hardware') === 'Intel Arc Pro B60'),
            `Expected hardware query param in requests: ${JSON.stringify(taskRequests)}`,
        );
        assert.match(advBtn.textContent, /Advanced Filters \(1\)/);

        const clearBtn = walk(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'Clear Filters')[0];
        assert.ok(clearBtn, 'Expected Clear Filters button when hardware filter is active');
        await act(async () => {
            reactProps(clearBtn).onClick?.({});
        });
        await flush();
        assert.equal(advBtn.textContent.trim(), 'Advanced Filters');

        // UI ratchet checks: no raw hex colors, fixed inset-0, or animate-spin in SimulationTaskDetails.jsx or our SimulationDashboard hardware UI
        const simUiDir = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../pages/simulation/ui');
        const detailsSource = fs.readFileSync(path.join(simUiDir, 'SimulationTaskDetails.jsx'), 'utf8');
        const dashboardSource = fs.readFileSync(path.join(simUiDir, 'SimulationDashboard.jsx'), 'utf8');
        assert.doesNotMatch(detailsSource, /#[0-9a-fA-F]{6}|fixed inset-0|animate-spin/);
        const hwFilterMatch = dashboardSource.match(/htmlFor="simulation-filter-hardware"[\s\S]*?<\/Select>/);
        assert.ok(hwFilterMatch, 'Expected simulation-filter-hardware block in SimulationDashboard.jsx');
        assert.doesNotMatch(hwFilterMatch[0], /#[0-9a-fA-F]{6}|fixed inset-0|animate-spin/);
    });

    test('SimulationTaskDetails resolves hardware fallback chain and renders header + configuration summary', async (t) => {
        // 1. task.hardware takes precedence
        assert.equal(
            resolveTaskHardware(
                { hardware: 'Intel Arc Pro B60' },
                {
                    server_telemetry: {
                        accelerator_summary: 'NVIDIA H100 80GB HBM3',
                        hardware_profiles: [{ display_name: 'Profile A', models: ['Model X'] }],
                    },
                    run_manifest: { hardware_profile: { display_name: 'Manifest HW' } },
                },
            ),
            'Intel Arc Pro B60',
        );

        // 2. server_telemetry.accelerator_summary
        assert.equal(
            resolveTaskHardware(
                { hardware: null },
                {
                    server_telemetry: {
                        accelerator_summary: 'NVIDIA H100 80GB HBM3',
                        hardware_profiles: [{ display_name: 'Profile A', models: ['Model X'] }],
                    },
                    run_manifest: { hardware_profile: { display_name: 'Manifest HW' } },
                },
            ),
            'NVIDIA H100 80GB HBM3',
        );

        // 3. First non-empty model in server_telemetry.hardware_profiles[*].models
        assert.equal(
            resolveTaskHardware(
                {},
                {
                    server_telemetry: {
                        accelerator_summary: '',
                        hardware_profiles: [
                            { display_name: 'Node Pool 0', models: ['  '] },
                            { display_name: 'Node Pool 1', models: ['Intel Data Center GPU Max 1550'] },
                        ],
                    },
                    run_manifest: { hardware_profile: { display_name: 'Manifest HW' } },
                },
            ),
            'Intel Data Center GPU Max 1550',
        );

        // 4. server_telemetry.hardware_profiles[0].display_name
        assert.equal(
            resolveTaskHardware(
                {},
                {
                    server_telemetry: {
                        hardware_profiles: [{ display_name: 'Intel GPU', models: [] }],
                    },
                    run_manifest: { hardware_profile: { display_name: 'Manifest HW' } },
                },
            ),
            'Intel GPU',
        );

        // 5. run_manifest.hardware_profile.display_name
        assert.equal(
            resolveTaskHardware(
                {},
                {
                    server_telemetry: { hardware_profiles: [] },
                    run_manifest: { hardware_profile: { display_name: 'Cluster Profile X' } },
                },
            ),
            'Cluster Profile X',
        );

        // 6. Default fallback
        assert.equal(resolveTaskHardware({}, null), 'External / Not reported');

        // Configuration tab target summary (with run_manifest and without run_manifest)
        const manifestConfigHtml = renderToStaticMarkup(
            <LogsAndConfigTab
                task={{
                    id: 'sim-cfg-1',
                    model_name: 'Qwen/Qwen3-8B',
                    endpoint_mode: 'deployment',
                    hardware: 'Intel Arc Pro B60',
                    simulation: { backend: 'aiperf' },
                }}
                telemetry={{
                    run_manifest: {
                        backend: 'aiperf',
                        target: { endpoint_mode: 'deployment', model_name: 'Qwen/Qwen3-8B' },
                        replay: { backend_options: { random_seed: 666 } },
                    },
                }}
                artifacts={[]}
            />,
        );
        assert.match(manifestConfigHtml, />Hardware<\/span>[\s\S]*?>Intel Arc Pro B60<\/dd>/);
        assert.match(manifestConfigHtml, />Random seed<\/span>[\s\S]*?>666<\/dd>/);

        const zeroSeedManifestHtml = renderToStaticMarkup(
            <LogsAndConfigTab
                task={{
                    id: 'sim-cfg-zero',
                    model_name: 'Qwen/Qwen3-8B',
                    endpoint_mode: 'deployment',
                    simulation: { backend: 'aiperf', backend_options: { random_seed: 0 } },
                }}
                telemetry={{
                    run_manifest: {
                        backend: 'aiperf',
                        target: { endpoint_mode: 'deployment', model_name: 'Qwen/Qwen3-8B' },
                        replay: { backend_options: { random_seed: 0 } },
                    },
                }}
                artifacts={[]}
            />,
        );
        assert.match(zeroSeedManifestHtml, />Random seed<\/span>[\s\S]*?>0<\/dd>/);

        const noManifestConfigHtml = renderToStaticMarkup(
            <LogsAndConfigTab
                task={{
                    id: 'sim-cfg-2',
                    model_name: 'Qwen/Qwen3-8B',
                    endpoint_mode: 'external',
                    endpoint_url: 'https://api.example.com/v1',
                    simulation: { backend: 'aiperf' },
                }}
                telemetry={{}}
                artifacts={[]}
            />,
        );
        assert.match(noManifestConfigHtml, />Hardware<\/span>[\s\S]*?>External \/ Not reported<\/dd>/);

        // Full SimulationTaskDetails header <dl> renders Hardware alongside Model, Backend, Dataset, and Pacing
        const { root, doc, win, flush } = createDrawerTestEnv(t, {
            fetchHandler: async (url) => {
                const urlStr = String(url);
                if (urlStr.includes('/api/v1/simulation/backends')) return Response.json({ backends: [] });
                if (urlStr.includes('/telemetry')) {
                    return Response.json({
                        server_telemetry: {
                            accelerator_summary: 'Intel Arc Pro B60',
                            hardware_profiles: [{ display_name: 'Intel GPU', models: ['Intel Arc Pro B60'], allocated_accelerators: 2 }],
                        },
                    });
                }
                if (urlStr.includes('/api/v1/simulation/tasks/sim-det-1')) {
                    return Response.json({
                        task: {
                            id: 'sim-det-1',
                            name: 'Hardware Details Run',
                            status: 'completed',
                            model_name: 'Qwen/Qwen3-8B',
                            dataset: 'sharegpt-100',
                            hardware: 'Intel Arc Pro B60',
                            created_at: '2026-05-01T12:00:00Z',
                            simulation: { backend: 'aiperf', duration_seconds: 60 },
                            result: { summary: { total_requests: 10, failed_requests: 0 } },
                        },
                    });
                }
                return Response.json({});
            },
        });
        win.location = {
            href: 'http://localhost/?taskId=sim-det-1',
            pathname: '/',
            search: '?taskId=sim-det-1',
            hash: '',
        };
        win.history = { replaceState() {} };

        await act(async () => {
            root.render(<SimulationTaskDetails onNavigate={() => {}} />);
        });
        await flush();
        await flush();

        const headerText = doc.body.textContent;
        assert.match(headerText, /Model/);
        assert.match(headerText, /Backend/);
        assert.match(headerText, /Dataset/);
        assert.match(headerText, /Hardware/);
        assert.match(headerText, /Pacing/);
        assert.match(headerText, /Intel Arc Pro B60/);
    });

    test('SimulationTaskDetails disables Rerun button on historical trace-replayer tasks', async (t) => {
        const {
            root: legacyRoot,
            doc: legacyDoc,
            win: legacyWin,
            flush: legacyFlush,
        } = createDrawerTestEnv(t, {
            fetchHandler: async (url) => {
                const urlStr = String(url);
                if (urlStr.includes('/api/v1/simulation/backends')) return Response.json({ backends: [{ name: 'aiperf' }] });
                if (urlStr.includes('/telemetry')) return Response.json({ run_manifest: { backend: 'trace-replayer' } });
                if (urlStr.includes('/api/v1/simulation/tasks/sim-legacy-1')) {
                    return Response.json({
                        task: {
                            id: 'sim-legacy-1',
                            name: 'Legacy Trace Replayer Run',
                            status: 'completed',
                            model_name: 'Qwen/Qwen3-8B',
                            dataset: 'sharegpt-100',
                            created_at: '2026-05-01T12:00:00Z',
                            simulation: { backend: 'trace-replayer', duration_seconds: 60 },
                            result: { summary: { total_requests: 10, failed_requests: 0 } },
                        },
                    });
                }
                return Response.json({});
            },
        });
        legacyWin.location = {
            href: 'http://localhost/?taskId=sim-legacy-1',
            pathname: '/',
            search: '?taskId=sim-legacy-1',
            hash: '',
        };
        legacyWin.history = { replaceState() {} };

        await act(async () => {
            legacyRoot.render(<SimulationTaskDetails onNavigate={() => {}} />);
        });
        await legacyFlush();
        await legacyFlush();

        const walkLegacy = (node, pred, out = []) => {
            if (!node) return out;
            if (pred(node)) out.push(node);
            (node.childNodes || []).forEach((c) => walkLegacy(c, pred, out));
            return out;
        };
        const legacyRerunBtn = walkLegacy(
            legacyDoc.body,
            (el) => el.tagName === 'BUTTON' && el.textContent.includes('Rerun'),
        )[0];
        assert.ok(legacyRerunBtn, 'Expected Rerun button on SimulationTaskDetails');
        assert.equal(legacyRerunBtn.hasAttribute('disabled'), true);
        assert.equal(legacyRerunBtn.getAttribute('title'), 'Legacy trace-replayer backend is no longer supported');
    });
});
