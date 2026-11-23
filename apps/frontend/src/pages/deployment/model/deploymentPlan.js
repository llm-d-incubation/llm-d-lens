import yaml from 'js-yaml';
import { OPTIMIZATIONS } from '@repo/shared/inference/optimizations';
import { OPTIMIZATION_CONTROLS } from '../api/agenticDeploymentBackend';
import { capabilityTogglePatch } from './inference-designer/deploymentSelection';

/** Presentation of one optimization choice from the preview API or a plan's optimization_choices. */
export function optimizationChoiceState(choice, checked = choice?.checked ?? true) {
    if (!checked) return { tone: 'neutral', label: 'Off' };
    if (!choice) return { tone: 'neutral', label: 'On' };
    if (choice.intended) return { tone: 'success', label: choice.auto ? 'Auto: on' : 'On' };
    return choice.auto ? { tone: 'neutral', label: 'Auto: off' } : { tone: 'warning', label: 'Degraded' };
}

const ACTIVE_DECISIONS = new Set(['enabled', 'dependency']);

function optimizationParameters(id, candidate) {
    if (!candidate) return [];
    if (id === 'pd_disaggregation') {
        return candidate.prefill_replicas
            ? [['Prefill', `${candidate.prefill_replicas} × TP${candidate.prefill_tensor_parallel_size || 1}`], ['Decode', `${candidate.replicas || 1} × TP${candidate.tensor_parallel_size || 1}`]]
            : [['Serving', `${candidate.replicas || 1} × TP${candidate.tensor_parallel_size || 1}`]];
    }
    if (id === 'external_kv_cache' && candidate.native_cpu_cache_gib) return [['CPU cache', `${candidate.native_cpu_cache_gib} GiB per replica`]];
    if (id === 'prefix_routing' && candidate.prefix_routing != null) return [['Scorer', candidate.prefix_routing ? 'Prefix affinity + load' : 'Load only']];
    return [];
}

/** One row per user-selectable optimization, plus other optimizations the plan enabled. */
export function optimizationRows(plan, candidate, mode = 'manual') {
    const decisions = plan?.decisions || [];
    const checked = plan?.validation?.status === 'resources_checked';
    const choices = new Map((plan?.optimization_choices || []).map(item => [item.capability_id, item]));
    const forbidden = new Set(plan?.requirements?.forbidden_features || []);
    const rows = OPTIMIZATION_CONTROLS.map(([id, fallbackLabel]) => {
        const choice = choices.get(id);
        const decision = decisions.find(item => item.capability_id === id);
        const row = { id, label: fallbackLabel, selectable: true, toggleable: true, checked: choice ? choice.checked : !forbidden.has(id), parameters: optimizationParameters(id, candidate) };
        if (!row.checked) return { ...row, state: 'Off', tone: 'neutral', reason: choice?.reason || 'Turned off by you.' };
        if (ACTIVE_DECISIONS.has(decision?.status)) return { ...row, state: 'Enabled', tone: 'success', reason: decision.reason };
        if (mode === 'manual' && choice?.auto && !choice.intended) return { ...row, state: 'Auto: off', tone: 'neutral', reason: choice.reason };
        if (!checked && (!decision || decision.status === 'unknown')) return { ...row, state: 'Checks pending', tone: 'info', reason: decision?.reason || choice?.reason || 'Resource checks have not run yet.' };
        if (mode === 'agentic') return { ...row, state: 'Not selected', tone: 'neutral', reason: decision?.reason || 'The AI recommendation does not include it.' };
        return { ...row, state: 'Degraded', tone: 'warning', reason: decision?.reason || choice?.reason || 'Not established by the checked configuration.' };
    });
    const selectable = new Set(rows.map(row => row.id));
    // Other optimizations are toggled in the source flow; keep turned-off ones listed so they can be re-enabled.
    const others = decisions.filter(item => (ACTIVE_DECISIONS.has(item.status) || item.status === 'user_disabled') && !selectable.has(item.capability_id))
        .map(item => {
            const on = ACTIVE_DECISIONS.has(item.status);
            const option = OPTIMIZATIONS.find(option => option.id === (item.catalog_id || item.capability_id));
            const toggleable = Boolean(capabilityTogglePatch({ ...option, id: option?.id || item.capability_id, enabled: on }, plan?.source_draft || {}));
            return { id: item.capability_id, label: item.label, selectable: false, toggleable, checked: on, state: on ? 'Enabled' : 'Off', tone: on ? 'success' : 'neutral', reason: item.reason, parameters: [], detail: option?.detail };
        });
    return [...rows, ...others];
}

/** Graph identities include scope; edges are derived from resource references, never invented traffic. */
export function deploymentPlan(content = '') {
    const flatten = docs => docs.flatMap(doc => doc?.kind === 'List' ? flatten(doc.items || []) : doc ? [doc] : []);
    const documents = flatten(yaml.loadAll(content));
    const nodes = documents.filter(doc => doc.kind && doc.metadata?.name).map(doc => {
        const namespace = doc.metadata.namespace || '';
        const templates = doc.kind === 'Pod' ? [doc] : doc.kind === 'CronJob' ? [doc.spec?.jobTemplate?.spec?.template] : doc.kind === 'LeaderWorkerSet' ? [doc.spec?.leaderWorkerTemplate?.leaderTemplate, doc.spec?.leaderWorkerTemplate?.workerTemplate] : [doc.spec?.template];
        const containers = templates.flatMap(t => t?.spec?.containers || []);
        const kind = doc.kind;
        const labels = { ...templates.find(t => t?.metadata?.labels)?.metadata?.labels, ...doc.metadata.labels };
        const role = ['prefill', 'decode', 'encode'].find(value => labels['llm-d.ai/role'] === value || doc.metadata.name.toLowerCase().includes(value)) || 'serving';
        const tier = /Deployment|StatefulSet|DaemonSet|Pod|Job|LeaderWorkerSet/.test(kind) ? 1 : /PersistentVolume|ConfigMap|Secret|StorageClass/.test(kind) ? 2 : 0;
        return { id: `${doc.apiVersion}/${kind}/${namespace}/${doc.metadata.name}`, name: doc.metadata.name, namespace, kind, tier, role, document: doc, templates: templates.filter(Boolean), containers,
            detail: containers.length ? `${doc.spec?.replicas ?? 1} replicas · ${containers.length} containers` : namespace || 'Scope inherited at deployment' };
    });
    // DisaggregatedSet owns independently configurable prefill/decode role templates.
    for (const owner of [...nodes]) if (owner.kind === 'DisaggregatedSet') {
        for (const role of owner.document.spec?.roles || []) {
            const templates = [role.spec?.leaderWorkerTemplate?.leaderTemplate, role.spec?.leaderWorkerTemplate?.workerTemplate].filter(Boolean);
            nodes.push({ id: `${owner.id}/role/${role.name}`, owner: owner.id, role: role.name,
                name: `${owner.name}/${role.name}`, namespace: owner.namespace, kind: 'Model role', tier: 1,
                document: owner.document, templates, containers: templates.flatMap(t => t.spec?.containers || []),
                detail: `${role.spec?.replicas ?? 1} replicas · ${role.name} role` });
        }
    }
    const edges = [];
    const link = (a, b, label) => { if (a.id !== b.id && !edges.some(e => e.from === a.id && e.to === b.id && e.label === label)) edges.push({ from: a.id, to: b.id, label }); };
    const matches = (selector, labels) => selector && Object.keys(selector).length && Object.entries(selector).every(([k, v]) => labels?.[k] === v);
    for (const node of nodes) {
        if (node.owner) link(nodes.find(n => n.id === node.owner), node, 'owns');
        const doc = node.document;
        if (doc.kind === 'Service' || doc.kind === 'InferencePool') {
            const selector = doc.spec?.selector?.matchLabels || doc.spec?.selector;
            for (const target of nodes.filter(n => n.namespace === node.namespace && n.tier === 1)) if (target.templates.some(t => matches(selector, t.metadata?.labels))) link(node, target, 'selects');
        }
        for (const template of node.templates) {
            for (const volume of template.spec?.volumes || []) {
                const refs = [['PersistentVolumeClaim', volume.persistentVolumeClaim?.claimName], ['ConfigMap', volume.configMap?.name], ['Secret', volume.secret?.secretName]];
                for (const [kind, name] of refs) for (const target of nodes.filter(n => n.kind === kind && n.name === name && n.namespace === node.namespace)) link(target, node, 'mounted by');
            }
        }
        for (const rule of doc.spec?.rules || []) for (const ref of rule.backendRefs || []) {
            for (const target of nodes.filter(n => n.name === ref.name && n.kind === (ref.kind || 'Service') && n.namespace === (ref.namespace || node.namespace))) link(node, target, 'routes to');
        }
    }
    for (const node of [...nodes]) for (const [index, container] of (node.containers || []).entries()) {
        const args = [...(container.command || []), ...(container.args || [])];
        const position = args.findIndex(token => token === '--kv-transfer-config' || token.startsWith('--kv-transfer-config='));
        if (position < 0) continue;
        let config;
        try { config = JSON.parse(args[position].includes('=') ? args[position].slice(args[position].indexOf('=') + 1) : args[position + 1]); } catch { continue; }
        const connectors = config?.kv_connector === 'MultiConnector' ? config.kv_connector_extra_config?.connectors : [config];
        if (!Array.isArray(connectors)) continue;
        for (const [childIndex, connector] of connectors.entries()) {
            if (!connector?.kv_connector) continue;
            const cache = connector.kv_connector === 'OffloadingConnector';
            const bytes = connector.kv_connector_extra_config?.cpu_bytes_to_use;
            const child = { id: `${node.id}/connector/${index}/${childIndex}`, name: `${node.role}: ${connector.kv_connector}`,
                kind: cache ? 'Local KV cache' : 'KV transport', tier: 2, role: node.role, namespace: node.namespace,
                detail: cache ? `${bytes ? bytes / 1024 ** 3 : 'Source-defined'} GiB per model pod` : 'Transport configuration retained; runtime validation required' };
            nodes.push(child);
            link(node, child, cache ? 'offloads KV locally' : 'transfers KV');
        }
    }
    return { nodes, edges, content };
}

export function candidatePlan(candidate, model) {
    if (!candidate) return { nodes: [], edges: [], content: '' };
    const nodes = [{ id: 'model', name: model || 'Selected model', kind: 'Model', tier: 0, detail: 'Deployment target' }];
    if (candidate.prefill_replicas) nodes.push({ id: 'prefill', role: 'prefill', name: 'Prefill', kind: 'Planned workload', tier: 1, detail: `${candidate.prefill_replicas} replicas × TP ${candidate.prefill_tensor_parallel_size}` });
    nodes.push({ id: 'decode', role: 'decode', name: candidate.prefill_replicas ? 'Decode' : 'Model server', kind: 'Planned workload', tier: 1, detail: `${candidate.replicas} replicas × TP ${candidate.tensor_parallel_size}` });
    const edges = nodes.slice(1).map(n => ({ from: 'model', to: n.id, label: 'served by' }));
    if (candidate.native_cpu_cache_gib) {
        for (const role of candidate.prefill_replicas ? ['prefill', 'decode'] : ['decode']) {
            nodes.push({ id: `${role}-cpu-cache`, name: `${role}: CPU KV cache`, kind: 'Planned local cache', tier: 2, detail: `${candidate.native_cpu_cache_gib} GiB per model pod · runtime validation pending` });
            edges.push({ from: role, to: `${role}-cpu-cache`, label: 'offloads KV locally' });
        }
        if (candidate.prefill_replicas) edges.push({ from: 'prefill', to: 'decode', label: 'NIXL transfer (planned)' });
    }
    return { nodes, edges, content: '' };
}

export function overviewPlan(plan) {
    const nodes = plan.nodes.filter(node => !node.document
        || ['Gateway', 'HTTPRoute', 'Service', 'InferencePool'].includes(node.kind)
        || node.containers?.some(container => container.name === 'modelserver')).map(node => ({
            ...node,
            name: node.document
                ? node.containers?.some(container => container.name === 'modelserver')
                    ? node.role === 'prefill' ? 'Prefill' : node.role === 'decode' ? 'Decode' : 'Model serving'
                    : ({ Gateway: 'Request gateway', HTTPRoute: 'Request route', Service: 'Service endpoint', InferencePool: 'Inference pool' }[node.kind] || node.name)
                : node.name,
        }));
    const ids = new Set(nodes.map(node => node.id));
    return { ...plan, nodes, edges: plan.edges.filter(edge => ids.has(edge.from) && ids.has(edge.to)) };
}

export function optimizationPlan(candidate, decisions = []) {
    const pd = Boolean(candidate?.prefill_replicas);
    const placement = (column, row = 0) => ({ x: 130 + column * 248, y: 118 + row * 208 });
    const nodes = [
        { id: 'requests', name: 'API gateway', kind: 'Request path', tier: 0, detail: 'Receive requests', ...placement(0) },
        { id: 'routing', name: 'Endpoint selection', kind: 'Request path', tier: 0, detail: 'Route to a model server', ...placement(1) },
        { id: 'prefill', name: 'Prefill', kind: 'Request path', tier: 1, role: 'prefill',
            detail: pd ? `${candidate.prefill_replicas} replicas / TP ${candidate.prefill_tensor_parallel_size}` : 'Process prompt / write KV', ...placement(2) },
        { id: 'decode', name: 'Decode', kind: 'Request path', tier: 1, role: 'decode',
            detail: `${candidate?.replicas ?? 1} replicas / TP ${candidate?.tensor_parallel_size ?? 1}`, ...placement(3) },
        { id: 'responses', name: 'Response stream', kind: 'Request path', tier: 0, detail: 'Return generated tokens', ...placement(4) },
    ];
    const edges = [
        { from: 'requests', to: 'routing', label: 'request', kind: 'flow' },
        { from: 'routing', to: 'prefill', label: 'dispatch', kind: 'flow' },
        { from: 'prefill', to: 'decode', label: pd ? 'KV transfer' : 'shared KV', kind: 'flow' },
        { from: 'decode', to: 'responses', label: 'tokens', kind: 'flow' },
    ];
    const positions = {
        prefix_routing: [1, 1, 'routing', 'selects by cache'],
        pd_disaggregation: [2, 1, 'prefill', 'separates roles'],
        prefix_cache: [3, 1, 'prefill', 'reuses prefixes'],
        chunked_prefill: [2, 2, 'prefill', 'batches input'],
        external_kv_cache: [3, 2, 'decode', 'offloads KV'],
        autoscaling: [0, 1, 'routing', 'scales replicas'],
        speculative_decoding: [4, 1, 'decode', 'accelerates output'],
    };
    for (const [index, decision] of decisions.entries()) {
        const id = `optimization:${decision.capability_id}`;
        const [column, row, owner, label] = positions[decision.capability_id] || [index % 5, 3 + Math.floor(index / 5), 'decode', 'configures'];
        nodes.push({
            id, name: decision.label, kind: 'Optimization', tier: 2, ...placement(column, row),
            capability: decision.capability_id, status: decision.status, detail: decision.reason,
        });
        const included = ['enabled', 'dependency'].includes(decision.status);
        if (included || decision.status === 'unknown') edges.push({ from: owner, to: id, label, kind: 'attachment', pending: !included });
    }
    if (decisions.some(item => item.capability_id === 'prefix_cache' && ['enabled', 'dependency'].includes(item.status))
        && decisions.some(item => item.capability_id === 'external_kv_cache' && ['enabled', 'dependency'].includes(item.status))) {
        edges.push({ from: 'optimization:prefix_cache', to: 'optimization:external_kv_cache', label: 'required by', kind: 'dependency' });
    }
    if (decisions.some(item => item.capability_id === 'prefix_cache' && ['enabled', 'dependency'].includes(item.status))
        && decisions.some(item => item.capability_id === 'prefix_routing' && ['enabled', 'dependency'].includes(item.status))) {
        edges.push({ from: 'optimization:prefix_cache', to: 'optimization:prefix_routing', label: 'required by', kind: 'dependency' });
    }
    return { nodes, edges, content: '', optimization: true, serving: pd ? 'Separate prefill / decode servers' : 'Prefill + decode share one server' };
}

export function layoutPlan(plan) {
    return plan.nodes.map((node, index) => ({
        ...node, width: node.width ?? 196, height: node.height ?? (node.capability ? 112 : 96),
        x: node.x ?? 140 + node.tier * 300,
        y: node.y ?? 84 + plan.nodes.slice(0, index).filter(n => n.tier === node.tier).length * 176,
    }));
}

export function edgeGeometry(edge, nodes) {
    const a = nodes.find(node => node.id === edge.from), b = nodes.find(node => node.id === edge.to);
    if (!a || !b) return null;
    let points;
    if (a.y === b.y && a.x !== b.x) {
        const direction = Math.sign(b.x - a.x);
        const blocked = nodes.some(node => node.id !== a.id && node.id !== b.id
            && Math.abs(node.y - a.y) < node.height / 2 && node.x > Math.min(a.x, b.x) && node.x < Math.max(a.x, b.x));
        const channel = a.y - Math.max(a.height, b.height) / 2 - 34;
        points = blocked
            ? [[a.x, a.y - a.height / 2], [a.x, channel], [b.x, channel], [b.x, b.y - b.height / 2]]
            : [[a.x + direction * a.width / 2, a.y], [b.x - direction * b.width / 2, b.y]];
    } else if (a.x === b.x) {
        const direction = Math.sign(b.y - a.y);
        const blocked = nodes.some(node => node.id !== a.id && node.id !== b.id
            && Math.abs(node.x - a.x) < node.width / 2 && node.y > Math.min(a.y, b.y) && node.y < Math.max(a.y, b.y));
        points = blocked
            ? [[a.x + a.width / 2, a.y + a.height / 4], [a.x + a.width / 2 + 26, a.y + a.height / 4],
                [b.x + b.width / 2 + 26, b.y], [b.x + b.width / 2, b.y]]
            : [[a.x, a.y + direction * a.height / 2], [b.x, b.y - direction * b.height / 2]];
    } else {
        const direction = Math.sign(b.y - a.y);
        const start = a.y + direction * a.height / 2, end = b.y - direction * b.height / 2;
        const channel = (start + end) / 2;
        points = [[a.x, start], [a.x, channel], [b.x, channel], [b.x, end]];
    }
    const middle = Math.floor((points.length - 1) / 2), p = points[middle], q = points[middle + 1];
    return {
        path: points.map(([x, y], i) => `${i ? 'L' : 'M'} ${x} ${y}`).join(' '),
        points, labelX: (p[0] + q[0]) / 2, labelY: (p[1] + q[1]) / 2 - 10,
    };
}

export const OPTIMIZATION_STATES = {
    enabled: { label: 'Enabled', tone: 'success', action: 'Edit settings', color: '#10b981' },
    dependency: { label: 'Required', tone: 'info', action: 'Used by another optimization', color: '#3b82f6' },
    unknown: { label: 'Verify', tone: 'warning', action: 'Run source and runtime checks', color: '#eab308' },
    conflict: { label: 'Conflict', tone: 'warning', action: 'Conflicts with the current selection or prerequisites', color: '#f97316' },
    source_controlled: { label: 'Recipe setting', tone: 'neutral', action: 'Set through the selected recipe parameters', color: '#a855f7' },
    user_disabled: { label: 'Disabled', tone: 'neutral', action: 'Enable to include again', color: '#94a3b8' },
    unsupported: { label: 'Unavailable', tone: 'neutral', action: 'No deployable implementation in this source', color: '#475569' },
    alternative: { label: 'Alternative', tone: 'neutral', action: 'Available in another combination' },
    resource_limited: { label: 'Limited', tone: 'warning', action: 'Review resource or source limits' },
    excluded: { label: 'Not selected', tone: 'neutral', action: 'Review selection details' },
};
