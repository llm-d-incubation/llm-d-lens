import assert from 'node:assert/strict';
import test from 'node:test';
import { createBlueprint } from './blueprint.js';
import { inferenceScene, optimizationFlowPlan, servingFlowDiagram } from './sceneGraph.js';
import { OPTIMIZATIONS } from '@repo/shared/inference/optimizations';

test('every catalog capability has a real scene attachment', () => {
    const graph = inferenceScene(createBlueprint());
    assert.equal(graph.capabilities.length, OPTIMIZATIONS.length);
    for (const item of graph.capabilities) {
        assert.ok(graph.nodes.some(node => node.id === item.stage));
        assert.equal(item.unavailable, !item.available);
    }
    assert.equal(graph.edges.find(item => item.id === 'decode-loop').kind, 'iteration');
    assert.equal(graph.edges.find(item => item.id === 'kv-events').kind, 'state');
});

test('optimizations belong to a module or a relationship; they are not request stages', () => {
    const graph = inferenceScene(createBlueprint());
    for (const item of graph.capabilities) {
        assert.ok(Boolean(item.owner) !== Boolean(item.edge), item.id);
        if (item.owner) assert.ok(graph.nodes.some(node => node.id === item.owner));
        if (item.edge) assert.ok(graph.edges.some(edge => edge.id === item.edge));
    }
    assert.deepEqual(graph.nodes.filter(node => node.tier === 'flow').map(node => node.id), ['request', 'prepare', 'routing', 'prefill', 'decode', 'response']);
    for (const edge of graph.edges) {
        assert.ok(graph.nodes.some(node => node.id === edge.from));
        assert.ok(graph.nodes.some(node => node.id === edge.to));
    }
});
test('P/D enabling changes worker topology and transport without inventing a CPU cache hop', () => {
    const combined = inferenceScene(createBlueprint());
    const split = inferenceScene({ ...createBlueprint(), pd: true, prefillReplicas: '2', prefillTp: '4' });
    assert.ok(combined.nodes.some(node => node.id === 'serving-worker'));
    assert.ok(!split.nodes.some(node => node.id === 'serving-worker'));
    assert.match(split.nodes.find(node => node.id === 'prefill-worker').detail, /2 replicas · TP 4/);
    assert.deepEqual(split.edges.find(edge => edge.id === 'kv-wire'), { id: 'kv-wire', inPlan: true, from: 'prefill-worker', to: 'decode-worker', kind: 'state', enabled: true });
    assert.equal(split.capabilities.find(node => node.id === 'cache').enabled, false);
    assert.equal(split.capabilities.find(node => node.id === 'transfer').enabled, true);
});
test('optional components remain distinguishable from enabled deployment components', () => {
    const graph = inferenceScene(createBlueprint(), 'Test cluster');
    assert.equal(graph.nodes.find(node => node.id === 'cluster').label, 'Test cluster');
    assert.equal(graph.nodes.find(node => node.id === 'cpu-cache').enabled, false);
    assert.equal(graph.capabilities.find(node => node.id === 'speculation').unavailable, true);
});

test('2D projection merges combined serving, keeps cache off the request path and expands P/D', () => {
    for (const pd of [false, true]) {
        const scene = inferenceScene({ ...createBlueprint(), pd });
        const plan = optimizationFlowPlan(scene);
        const flow = plan.nodes.filter(node => !node.capability).map(node => node.id);
        assert.deepEqual(flow, pd ? ['request', 'prepare', 'routing', 'prefill', 'decode', 'response']
            : ['request', 'prepare', 'routing', 'serving-worker', 'response']);
        assert.equal(plan.nodes.filter(node => node.capability).length, scene.capabilities.length);
        assert.ok(plan.edges.filter(edge => edge.kind === 'flow').every(edge => flow.includes(edge.from) && flow.includes(edge.to)));
        assert.equal(plan.edges.filter(edge => edge.to === 'cache').length, pd ? 2 : 1);
        assert.ok(plan.nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
        const locations = plan.nodes.map(node => `${node.x},${node.y}`);
        assert.equal(new Set(locations).size, locations.length);
    }
});

test('2D projection distinguishes unsupported, required and conflicted optimizations', () => {
    const scene = inferenceScene(createBlueprint());
    scene.capabilities.find(cap => cap.id === 'prefix').required = true;
    scene.capabilities.find(cap => cap.id === 'prefix').enabled = true;
    const cache = scene.capabilities.find(cap => cap.id === 'cache');
    cache.unavailable = false;
    cache.generation = 'source';
    const plan = optimizationFlowPlan(scene, { cache: 'Insufficient host memory.' }, cap => cap.id !== 'cache');
    assert.equal(plan.nodes.find(node => node.id === 'prefix').status, 'dependency');
    assert.equal(plan.nodes.find(node => node.id === 'prefix').toggleDisabled, true);
    assert.equal(plan.nodes.find(node => node.id === 'cache').status, 'conflict');
    assert.equal(plan.nodes.find(node => node.id === 'cache').editDisabled, true);
    assert.equal(plan.nodes.find(node => node.id === 'speculation').status, 'unsupported');
});

test('a selected CPU cache Guide does not enable an unselected disk tier', () => {
    const graph = inferenceScene({ ...createBlueprint(), sourceMode: true, selectedGuides: ['tiered-prefix-cache'], sourcePlan: { guides: ['tiered-prefix-cache'], variables: { 'tiered-prefix-cache': { CONNECTOR: 'native', VARIANT: 'cpu' } } } });
    assert.equal(graph.capabilities.find(item => item.id === 'cache').enabled, true);
    assert.equal(graph.capabilities.find(item => item.id === 'disk-cache').enabled, false);
    assert.equal(graph.nodes.find(item => item.id === 'cpu-cache').enabled, true);
});

test('unsaved native cache edits remain visible and explicit disabling is respected', () => {
    const draft = { ...createBlueprint(), sourceMode: true, nativeCpuCacheGiB: 6, sourcePlan: { enabledCapabilities: [], guides: [] }, capabilityOverrides: {} };
    assert.equal(inferenceScene(draft).capabilities.find(cap => cap.id === 'cache').enabled, true);
    assert.equal(inferenceScene({ ...draft, capabilityOverrides: { cache: false } }).capabilities.find(cap => cap.id === 'cache').enabled, false);
});

test('inventory-only selection highlights infrastructure without claiming a serving path', () => {
    const graph = inferenceScene({ ...createBlueprint(), sourceMode: true, selectedGuides: ['runtime-inventory'], sourcePlan: { guides: ['runtime-inventory'], variables: {} }, sourceUnits: [{ path: 'guides/runtime-inventory/README.md#chart-0' }] });
    assert.equal(graph.nodes.find(item => item.id === 'cluster').inPlan, true);
    assert.equal(graph.nodes.find(item => item.id === 'prefill').inPlan, false);
});

test('baseline lights the serving path but leaves optional cache components and edges off', () => {
    const graph = inferenceScene({ ...createBlueprint(), sourceMode: true, selectedGuides: ['optimized-baseline'], sourcePlan: { guides: ['optimized-baseline'], variables: {} }, sourceUnits: [{ path: 'guides/optimized-baseline/modelserver/cpu/vllm' }] });
    for (const id of ['request', 'prepare', 'routing', 'prefill', 'decode', 'response', 'epp', 'serving-worker', 'pods']) assert.equal(graph.nodes.find(item => item.id === id).inPlan, true, id);
    for (const id of ['cpu-cache', 'cache-plane', 'index-service']) assert.equal(graph.nodes.find(item => item.id === id).inPlan, false, id);
    for (const id of ['offload', 'kv-events', 'cache-knowledge', 'kv-read-write']) assert.equal(graph.edges.find(item => item.id === id).inPlan, false, id);
    assert.ok(graph.capabilities.filter(item => item.guide !== 'optimized-baseline').every(item => !item.enabled));
});


test('dense source catalogs keep preview markers inside tiles without losing selectable capabilities', () => {
    const sourceGuides = Array.from({ length: 80 }, (_, index) => ({ id: `extra-${index}`, title: `Extra recipe ${index}`, hasAssets: true }));
    const graph = inferenceScene({ ...createBlueprint(), sourceGuides });
    const pods = graph.nodes.find(item => item.id === 'pods');
    const attached = graph.capabilities.filter(item => item.owner === pods.id);
    assert.equal(attached.length, 84);
    assert.equal(pods.previewOverflow, 78);
    assert.equal(attached.filter(item => item.previewVisible).length, 6);
    assert.equal(pods.optimizations.length, 84);
    for (const item of graph.capabilities.filter(item => item.previewVisible)) {
        const owner = graph.nodes.find(node => node.id === item.owner);
        assert.ok(Math.abs(item.at[0] - owner.at[0]) < 3.4);
        assert.ok(item.at[2] - owner.at[2] <= 5.4);
        assert.ok(item.at[2] < 14);
    }
    for (const tier of graph.tiers) {
        assert.ok(new Set(graph.nodes.filter(item => item.tier === tier.id).map(item => item.colorIndex)).size > 1);
    }
});

test('compact overview accounts for every optimization exactly once and keeps them off the request path', () => {
    const graph = inferenceScene({ ...createBlueprint(), prefixCaching: 'enabled', chunkedPrefill: 'enabled' });
    const diagram = servingFlowDiagram(graph);
    assert.equal(diagram.nodes.length, 9);
    assert.ok(diagram.height < 720);
    const ids = diagram.groups.flatMap(group => group.capabilities.map(cap => cap.id));
    assert.equal(ids.length, new Set(ids).size);
    assert.deepEqual(new Set(ids), new Set(graph.capabilities.map(cap => cap.id)));
    assert.equal(diagram.groups.reduce((sum, group) => sum + group.capabilities.filter(cap => cap.enabled).length, 0), diagram.configuredCount);
    assert.equal(diagram.regions.length, 1);
    assert.equal(diagram.edges.find(edge => edge.from === 'prefill' && edge.to === 'decode').label, 'KV cache');
    const prefill = servingFlowDiagram(graph, {}, () => true, { expandedGroup: 'prefill' });
    assert.deepEqual(prefill.edges.filter(edge => edge.from === 'prefix').map(edge => edge.to), ['prefill']);
    const compute = servingFlowDiagram(graph, {}, () => true, { expandedGroup: 'compute-settings' });
    assert.deepEqual(compute.edges.filter(edge => edge.from === 'parallel').map(edge => edge.to), ['prefill', 'decode']);
});

test('expansion reveals inactive entries and shared KV relationships, including source-defined entries', () => {
    const graph = inferenceScene({ ...createBlueprint(), pd: true, sourceGuides: [{ id: 'custom-operation', title: 'Custom operation', hasAssets: true }] });
    const plan = servingFlowDiagram(graph, {}, () => true, { expandedGroup: 'kv-settings' });
    assert.equal(plan.nodes.find(node => node.id === 'cache').enabled, false);
    assert.equal(plan.edges.filter(edge => edge.from === 'cache' && edge.pending).length, 2);
    assert.equal(plan.regions.length, 2);
    assert.equal(plan.edges.find(edge => edge.from === 'prefill' && edge.to === 'decode').label, 'KV transfer');
    const group = plan.groups.find(group => group.capabilities.some(cap => cap.id === 'guide:custom-operation'));
    assert.ok(group);
    const page = Math.floor(group.capabilities.findIndex(cap => cap.id === 'guide:custom-operation') / 4);
    const expanded = servingFlowDiagram(graph, {}, () => true, { expandedGroup: group.id, pages: { [group.id]: page } });
    assert.ok(expanded.nodes.some(node => node.id === 'guide:custom-operation'));
    assert.ok(expanded.edges.every(edge => edge.kind !== 'dependency'));
});

test('every page remains bounded with contained rows, no card collisions and unobstructed connectors', () => {
    const graph = inferenceScene({ ...createBlueprint(), pd: true, sourceGuides: Array.from({ length: 40 }, (_, i) => ({ id: `extra-${i}`, title: `A long deployment recipe title for configuration ${i}`, hasAssets: true })) });
    const overview = servingFlowDiagram(graph);
    for (const group of overview.groups) {
        const discovered = new Set();
        for (let page = 0; page < group.totalPages; page++) {
            const plan = servingFlowDiagram(graph, {}, () => true, { expandedGroup: group.id, pages: { [group.id]: page } });
            assert.ok(plan.height < 1150, `${group.id} is unbounded`);
            const rows = plan.nodes.filter(node => node.capability);
            assert.ok(rows.length <= 4);
            for (const row of rows) {
                discovered.add(row.id);
                const parent = plan.nodes.find(node => node.id === row.parentId);
                assert.ok(row.y - row.height / 2 >= parent.y - parent.height / 2 + parent.headerHeight);
                assert.ok(row.y + row.height / 2 < parent.y + parent.height / 2);
            }
            for (const [i, node] of plan.nodes.entries()) {
                assert.ok(node.x - node.width / 2 >= 0 && node.x + node.width / 2 <= plan.width, node.id);
                assert.ok(node.y - node.height / 2 >= 0 && node.y + node.height / 2 <= plan.height, node.id);
                for (const other of plan.nodes.slice(i + 1).filter(other => other.parentId !== node.id && node.parentId !== other.id)) assert.ok(Math.abs(node.x - other.x) >= (node.width + other.width) / 2 || Math.abs(node.y - other.y) >= (node.height + other.height) / 2, `${node.id} overlaps ${other.id}`);
            }
            for (const edge of plan.edges.filter(edge => edge.points)) for (let i = 1; i < edge.points.length; i++) {
                const [a, b] = [edge.points[i - 1], edge.points[i]];
                for (const node of plan.nodes.filter(node => node.id !== edge.from && node.id !== edge.to && node.parentId !== edge.from && node.parentId !== edge.to)) {
                    assert.ok(Math.max(a[0], b[0]) <= node.x - node.width / 2 || Math.min(a[0], b[0]) >= node.x + node.width / 2 || Math.max(a[1], b[1]) <= node.y - node.height / 2 || Math.min(a[1], b[1]) >= node.y + node.height / 2, `${edge.from}–${edge.to} crosses ${node.id}`);
                }
            }
            for (const edge of plan.edges.filter(edge => !edge.hidden && edge.label && !edge.labelOnSelect)) for (const node of plan.nodes) {
                assert.ok(Math.abs(edge.labelX - node.x) >= (edge.labelWidth + node.width) / 2 || Math.abs(edge.labelY - node.y) >= 12 + node.height / 2, `${edge.label} intersects ${node.id}`);
            }
        }
        assert.deepEqual(discovered, new Set(group.capabilities.map(cap => cap.id)));
    }
});

test('recipe-owned settings are not reported as conflicts', () => {
    const scene = inferenceScene(createBlueprint());
    const cap = scene.capabilities.find(item => item.id === 'cache');
    Object.assign(cap, { unavailable: false, generation: 'source', enabled: false, selection: 'parameter' });
    const plan = optimizationFlowPlan(scene, { cache: 'Controlled by source parameters.' });
    assert.equal(plan.nodes.find(node => node.id === 'cache').status, 'source_controlled');
});
