import { deploymentSelection } from './deploymentSelection.js';
import yaml from 'js-yaml';

/** Make revision-owned defaults explicit; unresolved source expressions stay unresolved. */
export function compositionDefaults(catalog) {
    return Object.fromEntries(catalog.guides.map(guide => [guide.id, Object.fromEntries(Object.entries(guide.variables || {}).flatMap(([key, spec]) => {
        const value = spec && typeof spec === 'object' ? spec.default : spec;
        if (value === undefined || String(value).includes('$')) return [];
        if (spec?.values && !spec.values.map(String).includes(String(value))) return [];
        return [[key, String(value)]];
    }))]));
}

export function resolveCompositionDraft(catalog, draft) {
    const plan = deploymentSelection(catalog, draft.selectedGuides, {
        model: draft.model, namespace: draft.sourceNamespace, accelerator: draft.accelerator,
        variables: draft.sourceVariables, capabilities: draft.capabilityOverrides,
        guideChoices: draft.guideChoices, mode: draft.sourceRouter.mode,
        units: draft.sourceUnits, routerValues: draft.sourceRouter.values, finalConfiguration: draft.finalConfiguration,
    });
    const errors = [...plan.errors];
    if (plan.existingModelIntegration || plan.existingPoolIntegration || plan.workloadPatches?.length) {
        errors.push('This recipe modifies existing workloads; it cannot create an isolated deployment.');
    }
    if (errors.length) throw new Error(errors.join(' '));
    return { ...draft, sourcePlan: plan, sourceVariables: plan.variables,
        sourceUnits: plan.units.map(unit => ({ ...unit, ...draft.sourceUnits?.find(item => item.path === unit.path) })),
        sourceRouter: { ...plan.router, values: plan.router.values.map(layer => ({ ...layer, ...draft.sourceRouter?.values?.find(item => item.path === layer.path) })) },
    };
}

/** One transport contract serves the configuration editor, Manual and Agentic. */
export function compositionRequest(draft, sessionId) {
    return { clusterId: draft.clusterId, clusterSessionId: sessionId, modelCacheEntryId: draft.modelCacheEntryId,
        validateCluster: draft.sourceValidate, commit: draft.sourceCommit, namespace: draft.sourceNamespace,
        guideSelection: draft.capabilityOverrides !== undefined || draft.selectedGuides?.length
            ? { guides: draft.selectedGuides, capabilities: draft.sourcePlan?.capabilityOverrides || draft.capabilityOverrides, guideChoices: draft.guideChoices, mode: draft.sourceRouter.mode, accelerator: draft.accelerator } : undefined,
        finalConfiguration: draft.finalConfiguration, units: draft.sourceUnits, variables: draft.sourceVariables,
        runtimeImage: draft.image || undefined, runtimeImageOverride: draft.runtimeImageExplicit || false,
        deploymentIntent: draft.deploymentIntent,
        sourcePlan: draft.sourcePlan ? Object.fromEntries(
            ['documents', 'replacements', 'sourceNamespaces', 'namespaceLabels', 'workloadPatches', 'existingModelIntegration', 'existingPoolIntegration']
                .map(key => [key, draft.sourcePlan[key]])) : undefined,
        runtimeConfiguration: draft.runtimeConfiguration,
        nativeCpuCacheGiB: draft.capabilityOverrides?.cache === false || draft.capabilityOverrides?.prefix === false ? undefined : draft.nativeCpuCacheGiB,
        router: draft.sourceRouter.values.length ? draft.sourceRouter : undefined };
}

/** Preserve the same bundle snapshot across preview, detailed editing and deployment. */
export function compositionResult(bundle, draft, clusterName = '') {
    return { files: bundle.files, compositionChecksum: bundle.compositionChecksum, sourceComposition: true,
        optimizations: bundle.optimizations, deploymentIntent: draft.deploymentIntent, sliceLayout: bundle.sliceLayout || null,
        target: { clusterId: draft.clusterId, clusterName: clusterName || draft.clusterId, model: draft.model, accelerator: draft.accelerator },
        plan: { source: { commit: bundle.commit }, validation: bundle.validation,
            deploymentBundle: { helm: { version: draft.sourceRouter.version || 'Not selected' }, resources: [bundle.files[0]] } } };
}

/** Read display topology from the generated resources, including final parameter edits. */
export function compositionDisplayDraft(draft, result) {
    if (!result?.sourceComposition) return draft;
    const documents = yaml.loadAll(result.files.find(file => file.name === 'deployment.yaml')?.content || '').filter(Boolean);
    const display = { ...draft, renderedOptimizations: result.optimizations };
    for (const doc of documents.filter(item => item.kind === 'Deployment')) {
        const container = doc.spec?.template?.spec?.containers?.find(item => item.name === 'modelserver');
        if (!container) continue;
        const role = /prefill/.test(`${doc.metadata?.name} ${JSON.stringify(doc.spec.template.metadata?.labels || {})}`) ? 'prefill' : 'decode';
        const command = [...(container.command || []), ...(container.args || [])].join(' ');
        const tp = command.match(/--tensor-parallel-size(?:=|\s+)(\d+)/)?.[1];
        if (role === 'prefill') { display.prefillReplicas = String(doc.spec.replicas ?? 1); if (tp) display.prefillTp = tp; }
        else { display.replicas = String(doc.spec.replicas ?? 1); if (tp) display.tp = tp; }
    }
    for (const doc of documents.filter(item => item.kind === 'DisaggregatedSet')) {
        display.slices = String(doc.spec?.slices ?? 1);
        for (const role of doc.spec?.roles || []) {
            const container = role.spec?.leaderWorkerTemplate?.workerTemplate?.spec?.containers?.find(item => item.name === 'modelserver');
            if (!container) continue;
            const tp = [...(container.command || []), ...(container.args || [])].join(' ').match(/--tensor-parallel-size(?:=|\s+)(\d+)/)?.[1];
            if (/prefill/.test(role.name || '')) { display.prefillReplicas = String(role.spec.replicas ?? 1); if (tp) display.prefillTp = tp; }
            else { display.replicas = String(role.spec.replicas ?? 1); if (tp) display.tp = tp; }
        }
    }
    return display;
}
