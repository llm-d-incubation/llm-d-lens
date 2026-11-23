import { sourceRoutingCapabilities } from './capabilityRendering.js';
import { expandCapabilityIntent } from './capabilityIntent.js';
import { nodeResourceKinds } from './nodeParameters.js';
import { resolveGuidePlan, isServingSourceUnit } from './guidePlan.js';
import { optimizationCatalog, resolveOptimizationFeatures } from '@repo/shared/inference/optimizations';

/** Keep a source-backed serving path when users clear optional selections. */
export function deploymentSelection(catalog, guides = [], options = {}) {
    const intent = options.capabilities !== undefined ? expandCapabilityIntent(catalog, guides, options) : null;
    if (intent) options = { ...options, variables: intent.variables };
    const requested = [...new Set(intent?.guides || guides)];
    const alternatives = requested.filter(id => id !== 'optimized-baseline');
    const alternativePlan = resolveGuidePlan(catalog, { ...options, guides: alternatives });
    const ownsServing = alternativePlan.units.some(isServingSourceUnit);
    const existing = alternativePlan.existingModelIntegration || alternativePlan.existingPoolIntegration || alternativePlan.workloadPatches.length > 0;
    const selected = ownsServing || existing ? alternatives : [...new Set(['optimized-baseline', ...requested])];
    const plan = resolveGuidePlan(catalog, { ...options, guides: selected });
    if (!intent) return plan;
    const routing = sourceRoutingCapabilities(plan.router.values.map(layer => ({
        ...layer, content: layer.content ?? catalog.assets.find(asset => asset.path === layer.path)?.content,
    })));
    const enabled = intent.enabled.filter(id => !Object.hasOwn(routing, id) || intent.overrides[id] === true || routing[id]);
    for (const [id, present] of Object.entries(routing)) if (present && intent.overrides[id] !== false && !enabled.includes(id)) enabled.push(id);
    const sourceFeatures = resolveOptimizationFeatures(catalog, { ...plan, finalConfiguration: options.finalConfiguration,
        units: plan.units.map(unit => ({ ...unit, ...options.units?.find(item => item.path === unit.path) })),
        router: { ...plan.router, values: plan.router.values.map(layer => ({ ...layer, ...options.routerValues?.find(item => item.path === layer.path) })) },
    });
    const definitions = optimizationCatalog(catalog).items;
    const inherited = sourceFeatures.features.filter(feature => feature.state === 'included'
        && !enabled.includes(feature.id) && intent.overrides[feature.id] === undefined);
    enabled.push(...inherited.map(feature => feature.id));
    const locked = sourceFeatures.features.filter(feature => feature.state === 'included'
        && definitions.find(item => item.id === feature.id)?.selection !== 'switch'
        && !intent.wholeGuides.includes(definitions.find(item => item.id === feature.id)?.guide)).map(feature => feature.id);
    const errors = locked.filter(id => intent.overrides[id] === false).map(id => `${id} is declared by the selected recipe. Change its source parameters or select another recipe to disable it.`);
    return { ...plan, errors: [...intent.errors, ...plan.errors, ...errors], choices: intent.choices, enabledCapabilities: enabled, sourceFeatures,
        optimizationCatalog: definitions,
        capabilityOverrides: { ...Object.fromEntries(['prefix', 'chunked'].filter(id => intent.wholeGuides.includes('tiered-prefix-cache') && !Object.hasOwn(intent.overrides, id)).map(id => [id, true])), ...intent.overrides }, wholeGuides: intent.wholeGuides, selectedPresets: guides,
        requiredCapabilities: [...new Set([...(intent.enabled.includes('disk-cache') ? ['cache'] : []), ...locked])] };

}

/** Node editors expose only selected source resources belonging to that node. */
export function nodeParameterPaths(entity, draft) {
    if (!entity) return [];
    const units = draft.sourceUnits || [], layers = draft.sourceRouter?.values || [];
    const paths = [...units, ...layers];
    if (entity.kind === 'capability') {
        if (entity.sourcePaths?.length) {
            const owners = new Set(entity.sourcePaths.map(path => path.split('/modelserver/')[0]));
            return paths.filter(item => entity.sourcePaths.includes(item.path) || [...owners].some(owner => item.path.startsWith(`${owner}/`))).map(item => item.path);
        }
        if (['prefix', 'chunked', 'parallel'].includes(entity.id)) return units.filter(item => /\/modelserver(?:\/|$)/.test(item.path)).map(item => item.path);
        if (['affinity', 'load-routing', 'tracing'].includes(entity.id)) return layers.map(item => item.path);
        const kinds = nodeResourceKinds(entity);
        return paths.filter(item => (item.guide === entity.guide || item.path.startsWith(`guides/${entity.guide}/`)) && (!kinds || (layers.includes(item) ? kinds.includes('values') : !kinds.includes('values')))).map(item => item.path);
    }
    if (['request', 'routing', 'epp', 'service'].includes(entity.id)) return layers.map(item => item.path);
    if (['prefill', 'decode', 'prefill-worker', 'decode-worker', 'serving-worker', 'pods'].includes(entity.id)) return units.filter(item => /\/modelserver(?:\/|$)/.test(item.path)).map(item => item.path);
    if (['index-service', 'cpu-cache', 'cache-plane'].includes(entity.id)) return units.filter(item => /guides\/(precise-prefix-cache-routing|tiered-prefix-cache|p2p-kv-cache-sharing)\//.test(item.path)).map(item => item.path);
    return [];
}

/** A direct capability click changes only its own intent and explicit dependencies. */
export function capabilityTogglePatch(capability, draft) {
    if (!capability || capability.id === 'parallel' || capability.required || ['recipe', 'parameter'].includes(capability.selection)
        || ['external-operation', 'requires-adapter', 'unavailable'].includes(capability.generation)) return null;
    if (capability.id === 'cache' && !capability.enabled && draft.nativeCpuCacheGiB) {
        const overrides = { ...draft.capabilityOverrides };
        delete overrides.cache;
        return { sourceMode: true, capabilityOverrides: overrides };
    }
    return { sourceMode: true, capabilityOverrides: { ...draft.capabilityOverrides, [capability.id]: !capability.enabled, ...(capability.id === 'disk-cache' && !capability.enabled ? { cache: true } : {}) } };
}

/** Preview only established source-plan conflicts; incomplete variant choices remain editable. */
export function selectionConflicts(catalog, draft, patch = {}) {
    if (!catalog) return [];
    const next = { ...draft, ...patch };
    return deploymentSelection(catalog, next.selectedGuides, {
        model: next.model, namespace: next.sourceNamespace, variables: next.sourceVariables,
        capabilities: next.capabilityOverrides, guideChoices: next.guideChoices,
        mode: next.sourceRouter?.mode, accelerator: next.accelerator,
        units: next.sourceUnits, routerValues: next.sourceRouter?.values, finalConfiguration: next.finalConfiguration,
    }).errors.filter(message => /^(Competing serving topologies:|Conflicting router releases:|Mutually exclusive source variants:)/.test(message));
}
