import { capabilityEnabled } from './capabilityCatalog.js';
import { optimizationCatalog } from '@repo/shared/inference/optimizations';
/** A desired deployment graph, not discovered cluster inventory. */
export const TIERS = [
    { id: 'flow', label: 'Inference flow', subtitle: 'Logical stages · request order', y: 20 },
    { id: 'runtime', label: 'Serving components', subtitle: 'Processes and shared KV state', y: 10 },
    { id: 'platform', label: 'Kubernetes platform', subtitle: 'Cluster → Service → workload → workers', y: 0 },
];
export function inferenceScene(draft, clusterName = '') {
    if (draft.sourceMode) {
        const enabled = new Set(draft.sourcePlan?.enabledCapabilities || []);
        if (draft.nativeCpuCacheGiB && draft.capabilityOverrides?.cache !== false && draft.capabilityOverrides?.prefix !== false) enabled.add('cache');
        for (const fact of draft.renderedOptimizations?.features || []) {
            if (['configured', 'mixed'].includes(fact.state)) enabled.add(fact.id);
            else enabled.delete(fact.id);
        }
        draft = { ...draft, sourcePlan: draft.sourcePlan?.enabledCapabilities ? { ...draft.sourcePlan, enabledCapabilities: [...enabled] } : draft.sourcePlan,
            precise: enabled.has('index') || (draft.sourcePlan?.guides || []).includes('precise-prefix-cache-routing'),
            cache: enabled.has('cache') ? 'source-configured' : !draft.renderedOptimizations && (draft.sourcePlan?.guides || []).includes('tiered-prefix-cache') ? draft.sourcePlan.variables['tiered-prefix-cache']?.CONNECTOR || 'native' : 'none',
            pd: draft.renderedOptimizations ? draft.renderedOptimizations.roles.includes('prefill') && draft.renderedOptimizations.roles.includes('decode') :
                enabled.has('transfer') || (draft.sourceUnits || []).some(unit => /guides\/(pd-disaggregation|coord-disaggregation)\//.test(unit.path) || /\/e-p-d\//.test(unit.path)) };
    }
    const nodes = [
        { id: 'request', label: 'API / gateway', tier: 'flow', at: [-20, 20, 0], detail: '1 · Receive the request; match the HTTPRoute and serving pool', inspector: 'request' },
        { id: 'prepare', label: 'Prepare input', tier: 'flow', at: [-12, 20, 0], detail: '2 · Resolve model / adapter; tokenize input; encode media only for multimodal models', inspector: 'prepare' },
        { id: 'routing', label: 'Endpoint selection', tier: 'flow', at: [-4, 20, 0], detail: '3 · Admit / queue → filter → score → select an endpoint', inspector: 'routing' },
        { id: 'prefill', label: 'Prefill', tier: 'flow', at: [4, 20, 0], detail: '4 · Lookup prefix blocks → compute uncached prompt tokens → write KV', inspector: 'prefill' },
        { id: 'decode', label: 'Decode', tier: 'flow', at: [12, 20, 0], detail: '5 · Read KV → batch decode → sample a token → append KV; repeat until stop', inspector: 'decode' },
        { id: 'response', label: 'Stream response', tier: 'flow', at: [20, 20, 0], detail: '6 · Detokenize output and stream through the gateway to the client', inspector: 'response' },
        { id: 'epp', label: 'Endpoint Picker (EPP)', tier: 'runtime', at: [-14, 10, -1], detail: 'Endpoint discovery + scheduling policy', inspector: 'routing' },
        { id: 'index-service', label: 'KV cache indexer', tier: 'runtime', at: [-5, 10, 4], detail: draft.precise ? 'Enabled · feeds cache location to EPP' : 'Optional · precise cache knowledge', inspector: 'index', optional: true, enabled: draft.precise },
        ...(draft.pd ? [
            { id: 'prefill-worker', label: 'Prefill model servers', tier: 'runtime', at: [3, 10, -2], detail: `${draft.prefillReplicas} replicas · TP ${draft.prefillTp}`, inspector: 'prefill' },
            { id: 'decode-worker', label: 'Decode model servers', tier: 'runtime', at: [13, 10, -2], detail: `${draft.replicas} replicas · TP ${draft.tp}`, inspector: 'decode' },
        ] : [{ id: 'serving-worker', label: 'Model server', tier: 'runtime', at: [6, 10, -2], detail: `${draft.replicas} replicas · Prefill + Decode · TP ${draft.tp}`, inspector: 'decode' }]),
        { id: 'cpu-cache', label: 'CPU RAM cache', tier: 'runtime', at: [15, 10, 5], detail: draft.cache !== 'none' ? `${draft.cacheCpuGiB} GiB · ${draft.cache}` : 'Optional · offload / restore KV', inspector: 'cache', optional: true, enabled: draft.cache !== 'none' },
        { id: 'cluster', label: clusterName || 'Target cluster', tier: 'platform', at: [-15, 0, 0], detail: 'Kubernetes cluster boundary', inspector: 'cluster' },
        { id: 'service', label: 'Kubernetes Service', tier: 'platform', at: [-5, 0, 0], detail: 'Stable endpoint + workload selectors', inspector: 'service' },
        { id: 'pods', label: 'Model-server Pods', tier: 'platform', at: [5, 0, 0], detail: 'Desired workloads · no live pod inventory', inspector: 'pods' },
        { id: 'workers', label: 'Kubernetes nodes', tier: 'platform', at: [15, 0, 0], detail: 'Hardware profile + device resources', inspector: 'workers' },
    ];
    nodes.push({ id: 'cache-plane', label: 'KV cache', tier: 'runtime', at: [0, 10, 7], detail: 'Conceptual cache group, not a separate service: prefix caching, offloading and P2P KV cache sharing', inspector: 'cache', optional: true, enabled: draft.cache !== 'none' || draft.precise });
    const worker = draft.pd ? 'decode-worker' : 'serving-worker';
    const edges = [
        { id: 'prompt', from: 'request', to: 'prepare', kind: 'request' },
        { id: 'prepared', from: 'prepare', to: 'routing', kind: 'request' },
        { id: 'dispatch', from: 'routing', to: 'prefill', kind: 'request' },
        { id: 'handoff', from: 'prefill', to: 'decode', kind: 'request' },
        { id: 'output', from: 'decode', to: 'response', kind: 'request' },
        { id: 'response', from: 'response', to: 'request', kind: 'response' },
        { id: 'decode-loop', from: 'decode', to: 'decode', kind: 'iteration' },
        { id: 'kv-read-write', from: worker, to: 'cache-plane', kind: 'state' },
        { id: 'kv-events', from: worker, to: 'index-service', kind: 'state', enabled: draft.precise },
        { id: 'cache-knowledge', from: 'index-service', to: 'epp', kind: 'state', enabled: draft.precise },
        { id: 'offload', from: worker, to: 'cpu-cache', kind: 'state', enabled: draft.cache !== 'none' },
        ...(draft.pd ? [{ id: 'kv-wire', from: 'prefill-worker', to: 'decode-worker', kind: 'state', enabled: true }] : []),
        { id: 'route-owner', from: 'routing', to: 'epp', kind: 'placement' },
        { id: 'prefill-owner', from: 'prefill', to: draft.pd ? 'prefill-worker' : worker, kind: 'placement' },
        { id: 'decode-owner', from: 'decode', to: worker, kind: 'placement' },
        { id: 'router-service', from: 'epp', to: 'service', kind: 'placement' },
        { id: 'worker-pods', from: worker, to: 'pods', kind: 'placement' },
        ...(draft.pd ? [{ id: 'prefill-pods', from: 'prefill-worker', to: 'pods', kind: 'placement' }] : []),
        { id: 'cluster-service', from: 'cluster', to: 'service', kind: 'platform' },
        { id: 'service-pods', from: 'service', to: 'pods', kind: 'platform' },
        { id: 'pod-placement', from: 'pods', to: 'workers', kind: 'platform' },
    ];
    const relations = { transfer: 'handoff', index: 'cache-knowledge', cache: 'offload', payload: 'prompt' };
    const catalog = draft.sourcePlan?.optimizationCatalog || optimizationCatalog({ guides: draft.sourceGuides || [] }).items;
    const chosen = new Set(draft.sourcePlan?.guides || []), explicit = new Set(draft.selectedGuides || []);
    const capabilities = catalog.map(item => {
        const fact = draft.sourcePlan?.sourceFeatures?.features.find(feature => feature.id === item.id);
        const rendered = draft.renderedOptimizations?.features.find(feature => feature.id === item.id);
        const owner = item.id === 'parallel' ? worker : item.stage;
        const node = nodes.find(node => node.id === owner);
        const peers = catalog.filter(cap => cap.stage === item.stage);
        const index = peers.findIndex(cap => cap.id === item.id);
        const selectedGuide = chosen.has(item.guide) && (item.id !== 'disk-cache' || draft.sourcePlan?.variables?.['tiered-prefix-cache']?.VARIANT === 'fs');
        return { ...item, sourcePaths: fact?.paths, sourceState: fact?.state, configurationState: rendered?.state, sourceReason: rendered?.reason || fact?.reason,
            tier: node.tier, ...(relations[item.id] ? { edge: relations[item.id] } : { owner }),
            at: [node.at[0] + ((index % 3) - 1) * 1.8, node.at[1] + 0.15, node.at[2] + 3 + Math.floor(index / 3) * 1.8], required: draft.sourcePlan?.enabledCapabilities ? (draft.sourcePlan.requiredCapabilities || []).includes(item.id) || item.id === 'parallel' : chosen.has(item.guide) && !explicit.has(item.guide), enabled: draft.sourcePlan?.enabledCapabilities ? draft.sourcePlan.enabledCapabilities.includes(item.id) : draft.sourceMode ? selectedGuide || (!draft.sourcePlan && (draft.sourceUnits || []).some(unit => unit.path.startsWith(`guides/${item.guide}/`))) : capabilityEnabled(item, draft), inherited: !draft.sourceMode && (['affinity', 'load-routing'].includes(item.id) || (item.id === 'prefix' && !draft.prefixCaching) || (item.id === 'chunked' && !draft.chunkedPrefill)),
            unavailable: fact?.state !== 'included' && !item.available && !(draft.sourceGuides || []).some(guide => guide.id === item.guide && guide.hasAssets) };
    });
    for (const capability of capabilities) {
        if (capability.configurationState) {
            capability.enabled = ['configured', 'mixed'].includes(capability.configurationState);
            if (capability.enabled) capability.unavailable = false;
        }
    }
    const roles = {
        request: [1, 'Ingress'], prepare: [3, 'Input processing'], routing: [2, 'Scheduling'],
        prefill: [0, 'Prompt compute'], decode: [3, 'Token generation'], response: [1, 'Streaming'],
        epp: [2, 'Scheduling'], index: [2, 'Cache discovery'], cache: [1, 'KV storage'],
        cluster: [1, 'Cluster boundary'], service: [2, 'Service discovery'],
        pods: [0, 'Model workloads'], workers: [3, 'Compute resources'],
    };
    for (const node of nodes) {
        [node.colorIndex, node.caption] = roles[node.inspector] || [0, 'Serving'];
        node.optimizations = capabilities.filter(item => (item.owner || item.stage) === node.id).map(item => item.id);
        const attached = capabilities.filter(item => item.owner === node.id);
        node.previewOverflow = Math.max(0, attached.length - 6);
        attached.forEach((item, index) => {
            item.previewVisible = index < 6;
            item.at = [node.at[0] + ((index % 3) - 1) * 1.8, node.at[1] + 0.15, node.at[2] + 3.6 + Math.floor((index % 6) / 3) * 1.8];
        });
    }
    for (const item of capabilities) item.colorIndex = nodes.find(node => node.id === (item.owner || item.stage))?.colorIndex ?? 0;

    const activeStages = new Set(capabilities.filter(cap => cap.enabled).map(cap => cap.stage));
    const hasServing = (draft.sourceUnits || []).some(unit => /\/modelserver(?:\/|$)/.test(unit.path));
    if (hasServing || !draft.sourceMode) for (const node of nodes.filter(node => !node.optional || node.enabled)) activeStages.add(node.id);
    if (hasServing || !draft.sourceMode) for (const node of nodes.filter(node => node.tier === 'flow' || node.tier === 'platform')) activeStages.add(node.id);
    for (const node of nodes) node.inPlan = activeStages.has(node.id) || capabilities.some(cap => cap.enabled && cap.owner === node.id);
    for (const edge of edges) edge.inPlan = edge.enabled !== false && (activeStages.has(edge.from) && activeStages.has(edge.to) || capabilities.some(cap => cap.enabled && cap.edge === edge.id));
    return { tiers: TIERS, nodes: nodes.map(node => ({ ...node, kind: 'module' })), edges, capabilities: capabilities.map(node => ({ ...node, kind: 'capability' })) };
}
/** Project source-owned capabilities onto the request path, not extra request hops. */
export function optimizationFlowPlan(graph, blocked = {}, canConfigure = () => true) {
    const split = graph.nodes.some(node => node.id === 'prefill-worker');
    const stages = graph.nodes.filter(node => node.tier === 'flow' && (split || !['prefill', 'decode'].includes(node.id)));
    if (!split) stages.splice(stages.findIndex(node => node.id === 'response'), 0, {
        ...graph.nodes.find(node => node.id === 'serving-worker'), id: 'serving-worker', label: 'Prefill + Decode',
    });
    const columns = new Map(stages.map((node, index) => [node.id, index]));
    const stageFor = id => {
        if (columns.has(id)) return id;
        if (['prefill', 'prefill-worker'].includes(id)) return split ? 'prefill' : 'serving-worker';
        if (['decode', 'decode-worker', 'cpu-cache', 'cache-plane'].includes(id)) return split ? 'decode' : 'serving-worker';
        if (['epp', 'index-service', 'service'].includes(id)) return 'routing';
        return split ? 'decode' : 'serving-worker';
    };
    const nodes = stages.map((node, index) => ({
        id: node.id, name: node.label, kind: 'Request path', detail: node.detail, tier: 0,
        x: 130 + index * 248, y: 118,
    }));
    const edges = stages.slice(1).map((node, index) => ({
        from: stages[index].id, to: node.id, kind: 'flow',
        label: stages[index].id === 'prefill' ? 'KV transfer' : node.id === 'response' ? 'tokens' : 'request',
    }));
    const rows = new Map();
    for (const cap of graph.capabilities) {
        const relation = graph.edges.find(edge => edge.id === cap.edge);
        const owner = stageFor(cap.owner || relation?.from || cap.stage);
        const row = (rows.get(owner) || 0) + 1;
        rows.set(owner, row);
        const unavailable = !cap.enabled && (cap.unavailable || ['external-operation', 'requires-adapter', 'unavailable'].includes(cap.generation));
        const status = cap.configurationState === 'mixed' ? 'unknown' : cap.enabled ? cap.required ? 'dependency' : 'enabled'
            : unavailable ? 'unsupported' : ['recipe', 'parameter'].includes(cap.selection) ? 'source_controlled'
                : blocked[cap.id] ? 'conflict' : 'user_disabled';
        nodes.push({
            id: cap.id, name: cap.label, kind: 'Optimization', capability: cap.id, tier: 1, status, enabled: cap.enabled,
            detail: blocked[cap.id] || cap.sourceReason || cap.detail, x: 130 + columns.get(owner) * 248, y: 118 + row * 170,
            editDisabled: !canConfigure(cap),
            toggleDisabled: Boolean(blocked[cap.id]) || cap.required || cap.selection === 'recipe' || cap.selection === 'parameter' || unavailable,
        });
        edges.push({ from: owner, to: cap.id, kind: 'attachment', label: 'optimizes', pending: !cap.enabled });
        if (cap.id === 'cache') edges.push({
            from: split ? 'prefill' : 'serving-worker', to: cap.id, kind: 'attachment', label: 'shared KV cache', pending: !cap.enabled,
        });
    }
    return { nodes, edges: edges.filter((edge, index) => edges.findIndex(item => item.from === edge.from && item.to === edge.to) === index),
        optimization: true, serving: split ? 'Separate prefill / decode servers' : 'Prefill + decode share one server' };
}

/** Wrap before laying out SVG text, so labels and cards use the same dimensions. */
function diagramTextLines(value, limit = 30) {
    const words = String(value).split(/\s+/).flatMap(word => word.length > limit ? word.match(new RegExp(`.{1,${limit}}`, 'g')) : [word]);
    const lines = [];
    for (const word of words) {
        const last = lines.at(-1);
        if (last && last.length + word.length + 1 <= limit) lines[lines.length - 1] += ` ${word}`;
        else lines.push(word);
    }
    return lines;
}

/** Compact overview with bounded, in-place expansion of source-owned settings. */
export function servingFlowDiagram(graph, blocked = {}, canConfigure = () => true, { expandedGroup, pages = {} } = {}) {
    const split = graph.nodes.some(node => node.id === 'prefill-worker');
    const catalog = optimizationFlowPlan(graph, blocked, canConfigure).nodes.filter(node => node.capability);
    const laneFor = cap => cap.id === 'index' ? 'cache-index'
        : ['cache-plane', 'cpu-cache'].includes(cap.stage) ? 'kv-settings'
            : ['parallel', 'transfer'].includes(cap.id) ? 'compute-settings'
                : ['request', 'prepare'].includes(cap.stage) ? 'request'
                    : ['routing', 'prefill', 'decode', 'response'].includes(cap.stage) ? cap.stage : 'pods';
    // P/D engines carry prefix caching and chunked prefill on both roles.
    const inLane = (cap, id) => laneFor(cap) === id || (split && id === 'decode' && ['prefix', 'chunked'].includes(cap.id));
    const definitions = [
        ['request', 'Request', 'API / input processing', 'request', 140],
        ['routing', 'Endpoint selection', 'Endpoint Picker (EPP)', 'routing', 410],
        ['prefill', 'Prefill', 'Process input tokens', 'prefill', 680],
        ['decode', 'Decode', 'Generate output tokens', 'decode', 950],
        ['response', 'Response', 'Stream output tokens', 'response', 1220],
        ['cache-index', 'KV cache index', 'Cache location information', 'cache', 410],
        ['pods', 'Service & platform', 'Model-pool lifecycle', 'pods', 950],
        ['kv-settings', 'KV cache', 'Reuse / offload / sharing', 'cache', 680],
        ['compute-settings', 'Compute configuration', split ? 'Separate prefill / decode' : 'Shared by Prefill + Decode', 'compute', 950],
    ];
    const groups = definitions.map(([id, name, detail, icon, x]) => {
        const capabilities = graph.capabilities.filter(cap => inLane(cap, id))
            .map(cap => ({ ...cap, ...catalog.find(item => item.id === cap.id) }))
            .sort((a, b) => Number(b.enabled) - Number(a.enabled));
        const totalPages = Math.max(1, Math.ceil(capabilities.length / 4));
        const page = Math.min(Math.max(0, pages[id] || 0), totalPages - 1);
        const expanded = expandedGroup === id;
        const target = ({ 'compute-settings': split ? 'pods' : 'serving-worker', 'kv-settings': 'cache-plane', 'cache-index': 'index-service' })[id] || id;
        const entity = graph.nodes.find(node => node.id === target);
        const configureTarget = entity && canConfigure(entity) ? target : null;
        const rows = expanded ? capabilities.slice(page * 4, (page + 1) * 4).map(cap => ({ ...cap, titleLines: diagramTextLines(cap.label, 24), height: diagramTextLines(cap.label, 24).length * 16 + 30 })) : [];
        return { id, name, detail, icon, x, capabilities, rows, expanded, page, totalPages, configureTarget,
            height: 144 + (expanded ? rows.reduce((sum, row) => sum + row.height + 6, 0) + (rows.length ? 38 : 30) + (configureTarget ? 32 : 0) : 0) };
    });
    const get = id => groups.find(group => group.id === id);
    const topHeight = Math.max(get('cache-index').height, get('pods').height);
    const mainTop = 30 + topHeight + 96;
    const mainHeight = Math.max(...groups.slice(0, 5).map(group => group.height));
    // The lower row sits under Prefill/Decode only, so other expanded stages extend beside it.
    const lowerTop = mainTop + Math.max(get('prefill').height, get('decode').height) + 100;
    const nodes = [], edges = [], regions = [];
    for (const group of groups) {
        const { id, name, detail, icon, x, height, rows, capabilities, expanded, page, totalPages, configureTarget } = group;
        const top = ['cache-index', 'pods'].includes(id) ? 30 + topHeight - height : ['kv-settings', 'compute-settings'].includes(id) ? lowerTop : mainTop;
        const targets = id === 'cache-index' ? ['routing'] : ['kv-settings', 'compute-settings', 'pods'].includes(id) ? ['prefill', 'decode'] : [id];
        nodes.push({ id, name, detail, icon, x, y: top + height / 2, width: 220, height, headerHeight: 144, container: true, expanded, page, totalPages, configureTarget,
            summaryLines: capabilities.filter(cap => cap.enabled).slice(0, 2).map(cap => cap.label.length > 32 ? `${cap.label.slice(0, 30)}…` : cap.label),
            kind: 'Serving stage', titleLines: diagramTextLines(name, 22), count: capabilities.length, configured: capabilities.filter(cap => cap.enabled).length,
            inPlan: groups.slice(0, 5).some(item => item.id === id) || capabilities.some(cap => cap.enabled) });
        let rowTop = top + 144;
        for (const row of rows) {
            nodes.push({ ...row, parentId: id, icon, x, y: rowTop + row.height / 2, width: 200 });
            for (const target of targets) edges.push({ from: row.id, to: target, kind: 'attachment', label: 'applies to', pending: !row.enabled, hidden: true });
            rowTop += row.height + 6;
        }
    }
    const byId = new Map(nodes.map(node => [node.id, node]));
    const box = id => { const node = byId.get(id); return { ...node, top: node.y - node.height / 2, bottom: node.y + node.height / 2 }; };
    const connect = (from, to, kind, label, points, labelX, labelY) => edges.push({ from, to, kind, label, points,
        pending: !byId.get(from).inPlan, labelOnSelect: from === 'prefill' && to === 'decode', path: points.map(([x, y], i) => `${i ? 'L' : 'M'} ${x} ${y}`).join(' '),
        labelX, labelY, labelWidth: label.length * 5.8 + 18 });
    const flowY = mainTop + 52;
    for (const [from, to] of [['request', 'routing'], ['routing', 'prefill'], ['prefill', 'decode'], ['decode', 'response']]) {
        connect(from, to, from === 'prefill' ? 'kv' : 'flow', from === 'prefill' ? split ? 'KV transfer' : 'KV cache' : '',
            [[box(from).x + 110, flowY], [box(to).x - 110, flowY]], (box(from).x + box(to).x) / 2, flowY - 24);
    }
    connect('cache-index', 'routing', 'attachment', 'cache locality', [[410, box('cache-index').bottom], [410, mainTop]], 410, mainTop - 45);
    for (const [target, startX, endX, label] of [['prefill', 900, 760, 'pool lifecycle'], ['decode', 1000, 990, '']]) {
        const y = mainTop - 50;
        connect('pods', target, 'attachment', label, [[startX, box('pods').bottom], [startX, y], [endX, y], [endX, mainTop]], 830, y);
    }
    // Shared optimizations attach to both processing stages; they are not request hops.
    connect('kv-settings', 'prefill', 'kv', 'reuse KV', [[640, lowerTop], [640, box('prefill').bottom]], 640, lowerTop - 32);
    connect('kv-settings', 'decode', 'kv', 'KV state', [[720, lowerTop], [720, lowerTop - 64], [910, lowerTop - 64], [910, box('decode').bottom]], 810, lowerTop - 64);
    connect('compute-settings', 'decode', 'attachment', 'configures', [[1000, lowerTop], [1000, box('decode').bottom]], 1000, lowerTop - 32);
    connect('compute-settings', 'prefill', 'attachment', '', [[910, lowerTop], [910, lowerTop - 28], [750, lowerTop - 28], [750, box('prefill').bottom]], 0, 0);
    if (split) for (const id of ['prefill', 'decode']) regions.push({ id: `${id}-server`, label: `${id === 'prefill' ? 'Prefill' : 'Decode'} model server`, x: box(id).x - 124, y: mainTop - 38, width: 248, height: box(id).height + 54 });
    else regions.push({ id: 'model-server', label: 'Model server · Prefill + Decode', x: 556, y: mainTop - 38, width: 518, height: Math.max(box('prefill').height, box('decode').height) + 54 });
    return { presentation: 'serving', optimization: true, nodes, edges, regions, groups, offsetX: 0, width: 1360,
        height: Math.max(lowerTop + Math.max(get('kv-settings').height, get('compute-settings').height), mainTop + mainHeight) + 26,
        serving: split ? 'Separate prefill / decode servers' : 'Prefill + decode share one server',
        configuredCount: graph.capabilities.filter(cap => cap.enabled).length, totalCount: graph.capabilities.length };
}
