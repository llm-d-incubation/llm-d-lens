import { CAPABILITIES, capabilityEnabled } from './capabilityCatalog.js';
/** A desired deployment graph, not discovered cluster inventory. */
export const TIERS = [
    { id: 'flow', label: 'Inference flow', subtitle: 'Logical stages · request order', y: 20 },
    { id: 'runtime', label: 'Serving components', subtitle: 'Processes and shared KV state', y: 10 },
    { id: 'platform', label: 'Kubernetes platform', subtitle: 'Cluster → Service → workload → workers', y: 0 },
];
export function inferenceScene(draft, clusterName = '') {
    if (draft.sourceMode) draft = { ...draft, precise: (draft.sourcePlan?.guides || []).includes('precise-prefix-cache-routing'), cache: (draft.sourcePlan?.guides || []).includes('tiered-prefix-cache') ? draft.sourcePlan.variables['tiered-prefix-cache']?.CONNECTOR || 'native' : 'none', pd: (draft.sourceUnits || []).some(unit => /guides\/(pd-disaggregation|coord-disaggregation)\//.test(unit.path) || /\/e-p-d\//.test(unit.path)) };
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
    const discovered = (draft.sourceGuides || []).filter(guide => guide.hasAssets && (!guide.disposition || guide.disposition.kind === 'recipe') && !guide.id.startsWith('models/') && !['no-kubernetes-deployment', 'models'].includes(guide.id) && !CAPABILITIES.some(cap => cap.guide === guide.id)).map(guide => {
        const parent = CAPABILITIES.find(cap => guide.id.startsWith(`${cap.guide}/`));
        return { id: `guide:${guide.id}`, label: guide.title.replace(/[*`]/g, ''), stage: parent?.stage || 'pods', inspector: 'source', guide: guide.id, detail: `Pinned source recipe: ${guide.id}`, available: false, hasAssets: guide.hasAssets, fields: [] };
    });
    const catalog = [...CAPABILITIES, ...discovered];
    const chosen = new Set(draft.sourcePlan?.guides || []), explicit = new Set(draft.selectedGuides || []);
    const capabilities = catalog.map(item => {
        const owner = item.id === 'parallel' ? worker : item.stage;
        const node = nodes.find(node => node.id === owner);
        const peers = catalog.filter(cap => cap.stage === item.stage);
        const index = peers.findIndex(cap => cap.id === item.id);
        const selectedGuide = chosen.has(item.guide) && (item.id !== 'disk-cache' || draft.sourcePlan?.variables?.['tiered-prefix-cache']?.VARIANT === 'fs');
        return { ...item, tier: node.tier, ...(relations[item.id] ? { edge: relations[item.id] } : { owner }),
            at: [node.at[0] + ((index % 3) - 1) * 1.8, node.at[1] + 0.15, node.at[2] + 3 + Math.floor(index / 3) * 1.8], required: draft.sourcePlan?.enabledCapabilities ? (draft.sourcePlan.requiredCapabilities || []).includes(item.id) || item.id === 'parallel' : chosen.has(item.guide) && !explicit.has(item.guide), enabled: draft.sourcePlan?.enabledCapabilities ? draft.sourcePlan.enabledCapabilities.includes(item.id) : draft.sourceMode ? selectedGuide || (!draft.sourcePlan && (draft.sourceUnits || []).some(unit => unit.path.startsWith(`guides/${item.guide}/`))) : capabilityEnabled(item, draft), inherited: !draft.sourceMode && (['affinity', 'load-routing'].includes(item.id) || (item.id === 'prefix' && !draft.prefixCaching) || (item.id === 'chunked' && !draft.chunkedPrefill)), unavailable: !item.available && !(draft.sourceGuides || []).some(guide => guide.id === item.guide && guide.hasAssets) };
    });
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
/** Selection reveals only related placement edges, never a wall of infrastructure lines. */
export function sceneRelations(graph, selected) {
    const item = [...graph.nodes, ...graph.capabilities].find(node => node.id === selected);
    const related = new Set(item ? [item.id] : []);
    if (item?.owner) related.add(item.owner);
    if (item?.edge) { const edge = graph.edges.find(edge => edge.id === item.edge); if (edge) { related.add(edge.from); related.add(edge.to); } }
    const visible = graph.edges.filter(edge => edge.kind !== 'placement' || related.has(edge.from) || related.has(edge.to));
    for (const edge of visible.filter(edge => edge.kind === 'placement')) { related.add(edge.from); related.add(edge.to); }
    return { visibleEdges: visible, highlighted: related };
}
