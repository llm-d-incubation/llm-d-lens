import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import PodPlacementControl, { RuntimeClassControl } from './PodPlacementControl';

test('RuntimeClass selection preserves an explicit unverified choice and offers node default', () => {
    const markup = renderToStaticMarkup(<RuntimeClassControl value={{ mode: 'named', name: 'custom-runtime' }} onChange={() => {}} clusterId="cluster-a" />);
    assert.match(markup, /Automatic/);
    assert.match(markup, /Node default runtime/);
    assert.match(markup, /value="named:custom-runtime" selected=""/);
    assert.match(markup, /custom-runtime \(not verified\)/);
});

test('Standard placement uses the same collapsed button layout as neighboring sections', () => {
    const markup = renderToStaticMarkup(<PodPlacementControl value="prefer-same-node" onChange={() => {}} />);
    assert.match(markup, /<section[^>]*><button type="button" aria-expanded="false" class="flex w-full items-center justify-between text-sm font-medium text-white">/);
    assert.doesNotMatch(markup, /Replica placement/);
});

test('Agentic placement matches neighboring details and keeps the node picker', () => {
    const markup = renderToStaticMarkup(<PodPlacementControl compact value="prefer-same-node" onChange={() => {}} />);
    assert.match(markup, /<details[^>]*><summary class="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold text-slate-300">/);
    assert.match(markup, /<option value="prefer-same-node" selected="">Prefer one node \(default\)<\/option>/);
    assert.match(markup, /Prefer co-locating replicas/);
    assert.match(renderToStaticMarkup(<PodPlacementControl compact value="spread" onChange={() => {}} />), /Extra replicas remain Pending/);
    const sameNode = renderToStaticMarkup(<PodPlacementControl compact value="same-node" clusterId="cluster-a" nodeName="" onChange={() => {}} onNodeChange={() => {}} />);
    assert.match(sameNode, /Node \(optional\)/);
    assert.match(sameNode, /Any eligible node/);
    assert.match(sameNode, /dark:bg-slate-950 dark:text-slate-200/);
});
