import { optimizationCatalog } from '@repo/shared/inference/optimizations';
import { deploymentSelection } from './deploymentSelection';
import { isServingSourceUnit } from './guidePlan';

export const WORKLOAD_SCENARIOS = [
    ['chat', 'Online chat'], ['agent', 'Long-context / agent workloads'],
    ['batch', 'Offline batch'], ['multimodal', 'Multimodal requests'],
];
export const OPTIMIZATION_OBJECTIVES = [
    ['balanced', 'Balanced'], ['latency', 'Low latency'], ['throughput', 'High throughput'], ['cost', 'Low cost'],
    ['reliability', 'Reliability'],
];
export const DEFAULT_DEPLOYMENT_INTENT = { scenario: 'chat', objective: 'balanced', contextLength: '', concurrency: '', maxDevices: '' };

export function deploymentIntentErrors(intent = DEFAULT_DEPLOYMENT_INTENT) {
    const errors = [];
    if (!WORKLOAD_SCENARIOS.some(([id]) => id === intent.scenario)) errors.push('Select a workload scenario.');
    if (!OPTIMIZATION_OBJECTIVES.some(([id]) => id === intent.objective)) errors.push('Select an optimization objective.');
    for (const [key, label] of [['contextLength', 'Context length'], ['concurrency', 'Maximum engine sequences'], ['maxDevices', 'Device budget']]) {
        if (intent[key] !== '' && intent[key] !== undefined && (!/^[1-9]\d*$/.test(String(intent[key])) || !Number.isSafeInteger(Number(intent[key])))) errors.push(`${label} must be a positive integer.`);
    }
    return errors;
}

const confirmation = {
    inventory: 'Inventory requires explicit controller installation and cluster permissions.',
    payload: 'Confirm model modality and input preprocessing.',
    'encode-split': 'Separate encoders require model, transport and placement validation.',
    'multi-model': 'Requires explicit model/adapter mappings.',
    agentic: 'Stateful Responses API requires a state store and changes the API contract.',
    batch: 'Choose a batch dispatch strategy and queue backend.',
    'turn-priority': 'Experimental policy requires session identity.',
    latency: 'A calibrated latency predictor is required.',
    transfer: 'P/D requires a compatible integrated recipe and verified KV transport.',
    coordinator: 'Choose the coordinator topology and transport explicitly.',
    'wide-ep': 'Expert parallelism must come from a compatible model recipe.',
    speculation: 'Speculation must come from a compatible model recipe.',
    omni: 'Specialized output stages require explicit model intent.',
    'disk-cache': 'Confirm cache storage, capacity and mounts.',
    'shared-cache': 'Requires an explicit storage provisioning adapter.',
    'p2p-cache': 'Verify peer connectivity and the cache implementation.',
    scaling: 'Select an installed controller, metrics and a scaling ceiling.',
    actuation: 'Verify platform support and pre-provisioned workers.',
    keda: 'Requires an installed controller and a scaling ceiling.',
    snapshot: 'Requires checkpoint/restore platform support.',
    weights: 'Requires an accessible seed and peer transport.',
    rollouts: 'Requires existing versions and explicit traffic targets.',
    tracing: 'Configure an existing trace collector before enabling.',
};

/** Fixed Manual order: earlier entries win when two optimizations are mutually exclusive. */
export const MANUAL_PRIORITY = ['prefix', 'chunked', 'affinity', 'load-routing', 'cache', 'flow-control'];
/** Agentic only turns on optimizations expected to help the selected objective. */
export function objectivePriority(intent) {
    if (intent.scenario === 'agent') return ['prefix', 'affinity', 'index', 'chunked', 'load-routing', 'cache'];
    return {
        latency: ['prefix', 'affinity', 'index', 'chunked', 'load-routing'],
        throughput: ['chunked', 'load-routing', 'prefix', 'affinity', 'index', 'cache'],
        cost: ['prefix', 'chunked', 'load-routing', 'affinity'],
        reliability: ['prefix', 'chunked', 'affinity', 'load-routing', 'flow-control'],
    }[intent.objective] || ['prefix', 'chunked', 'affinity', 'load-routing', 'index'];
}
// These replace the serving workload but keep its role shape, so the sized topology still applies.
const SAME_SHAPE_SWAPS = new Set(['index', 'flow-control']);

/** Suggestions never overwrite explicit choices or assume that unknown prerequisites are installed. */
export function recommendOptimizations(catalog, draft, profile, { strictTopology = false } = {}) {
    const intent = draft.deploymentIntent || DEFAULT_DEPLOYMENT_INTENT;
    const manual = draft.planningMode === 'manual';
    const errors = deploymentIntentErrors(intent);
    if (!profile?.upstream_variant) errors.push('Choose a resolved accelerator profile before automatic selection.');
    if (errors.length) throw new Error(errors.join(' '));
    const locks = new Set(draft.optimizationLocks || []);
    const overrides = { ...draft.capabilityOverrides };
    for (const id of draft.automaticCapabilities || []) if (!locks.has(id)) delete overrides[id];
    const options = optimizationCatalog(catalog).items;
    const normalize = value => value.toLowerCase().replace(/[^a-z0-9]/g, '');
    const modelRecipe = catalog.guides.find(guide => guide.id.startsWith('models/') &&
        normalize(draft.model || '').includes(normalize(guide.id.slice(7))) && guide.id.slice(7).length > 3);
    let guides = draft.selectedGuides || [];
    const planFor = capabilities => deploymentSelection(catalog, guides, {
        model: draft.model, namespace: draft.sourceNamespace, accelerator: profile.upstream_variant,
        variables: draft.sourceVariables, capabilities, guideChoices: draft.guideChoices, mode: draft.sourceRouter.mode,
        units: draft.sourceUnits, routerValues: draft.sourceRouter.values, finalConfiguration: draft.finalConfiguration,
    });
    if (!guides.length && modelRecipe) {
        guides = [modelRecipe.id];
        if (planFor(overrides).errors.length) guides = [];
    }
    let current = planFor(overrides);
    if (current.errors.length) throw new Error(current.errors.join(' '));
    const existingRequirements = new Set((current.requirements || []).map(item => JSON.stringify(item)));
    let servingPaths = current.units.filter(isServingSourceUnit).map(unit => unit.path).sort().join('\n');
    const editedEngine = draft.sourceUnits?.some(unit => unit.edits?.length || unit.replacementContent !== undefined);
    const editedRouter = draft.sourceRouter.values.some(layer => layer.content !== undefined);
    const priority = manual ? MANUAL_PRIORITY : objectivePriority(intent);
    const ranked = [...options].sort((a, b) => {
        const score = item => priority.includes(item.id) ? priority.indexOf(item.id) : priority.length;
        return score(a) - score(b);
    });
    const label = id => options.find(item => item.id === id)?.label || id;
    const decisions = [], automatic = [];
    for (const option of ranked) {
        const id = option.id;
        let reason = '';
        if (locks.has(id) || Object.hasOwn(overrides, id)) {
            const seeded = !locks.has(id) && draft.candidateCapabilities?.includes(id);
            decisions.push({ id, state: overrides[id] === false ? 'disabled' : 'selected', reason: seeded
                ? `Set by the selected ${manual ? 'combination' : 'recommendation'}${id === 'affinity' && overrides[id] === false ? '; with one serving replica there is no endpoint choice to steer' : ''}. You can change it.`
                : 'Preserved explicit user choice.' });
            continue;
        }
        if (current.enabledCapabilities.includes(id) || (id === 'cache' && draft.nativeCpuCacheGiB)) {
            decisions.push({ id, state: 'selected', reason: 'Inherited from the selected recipe; inspect rendered evidence.' });
            continue;
        }
        if (option.maturity === 'deprecated' || option.maturity === 'experimental') reason = `${option.maturity}: explicit review required.`;
        else if (['requires-adapter', 'external-operation', 'unavailable'].includes(option.generation)) reason = option.reason;
        else if (option.selection === 'recipe' || option.selection === 'parameter') reason = 'Controlled by source parameters, not an independent switch.';
        else if (manual && id === 'index') reason = 'Not offered in Manual; prefix-aware routing is used instead. Enable it here to override.';
        else if (confirmation[id] && (!manual || !['transfer', 'coordinator'].includes(id))) reason = confirmation[id];
        else if (!manual && id === 'cache') reason = 'Confirm available host RAM and the cache connector before allocating an offload tier.';
        else if ((editedEngine && ['prefix', 'chunked'].includes(id)) || (editedRouter && ['affinity', 'load-routing', 'index'].includes(id)) || draft.finalConfiguration) reason = 'Preserved manually edited configuration.';
        if (reason) { decisions.push({ id, state: 'review', reason }); continue; }
        const next = planFor({ ...overrides, [id]: true });
        const nextServing = next.units.filter(isServingSourceUnit).map(unit => unit.path).sort().join('\n');
        const pdChange = next.enabledCapabilities?.includes('transfer') !== current.enabledCapabilities.includes('transfer');
        if (draft.runtimeConfiguration && nextServing !== servingPaths && (strictTopology || pdChange || !SAME_SHAPE_SWAPS.has(id))) {
            decisions.push({ id, state: 'blocked', reason: pdChange ? 'Changes the prefill/decode topology. Compare the P/D plan or enable it here; replicas and TP are then sized per role.'
                : id === 'cache' && current.enabledCapabilities.includes('transfer') ? 'Enable CPU KV cache and set its per-replica capacity to preserve the current P/D topology.'
                : 'This changes the sized serving topology. Compare a separate candidate or explicitly edit the topology.' });
            continue;
        }
        if (next.errors.length || next.existingModelIntegration || next.existingPoolIntegration || next.workloadPatches?.length
            || next.requirements?.some(item => !existingRequirements.has(JSON.stringify(item))) || next.choices?.some(choice => !choice.value?.length)) {
            const winner = next.errors.length ? [...automatic].reverse().find(other => {
                const without = { ...overrides, [id]: true };
                delete without[other];
                return !planFor(without).errors.length;
            }) : null;
            decisions.push({ id, state: 'blocked', reason: winner
                ? `Mutually exclusive with ${label(winner)}, which has higher ${manual ? 'Manual' : 'objective'} priority. Disable it to enable this optimization.`
                : next.errors.join(' ') || 'Required source choices or external prerequisites need confirmation.' });
            continue;
        }
        if (!manual && !priority.includes(id)) {
            decisions.push({ id, state: 'review', reason: `Compatible, but not expected to improve the ${intent.objective} objective. Enable it manually if needed.` });
            continue;
        }
        overrides[id] = true; automatic.push(id); current = next; servingPaths = nextServing;
        decisions.push({ id, state: 'selected', reason: manual ? `Compatible; enabled by Manual priority ${priority.includes(id) ? priority.indexOf(id) + 1 : priority.length + 1}. Hardware is checked during generation.`
            : `Expected to help ${intent.scenario} / ${intent.objective}; hardware is checked during generation.` });
    }
    return { selectedGuides: guides, capabilityOverrides: overrides, automaticCapabilities: automatic, optimizationDecisions: decisions };
}
