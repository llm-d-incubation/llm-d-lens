import { CAPABILITIES } from './capabilityCatalog.js';
import { deploymentSelection } from './deploymentSelection.js';
import yaml from 'js-yaml';

/** The source revision, not a page-specific preset, owns the optimization inventory. */
export function compositionOptions(catalog) {
    const options = CAPABILITIES.filter(item => item.id !== 'parallel').map(({ id, label, guide, detail }) => ({ id, label, guide, detail }));
    for (const guide of catalog.guides) {
        if (guide.disposition?.kind !== 'recipe' || options.some(item => item.guide === guide.id)) continue;
        options.push({ id: `guide:${guide.id}`, label: guide.title || guide.id, guide: guide.id, detail: guide.description || '' });
    }
    return options;
}

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

/** One transport contract serves Custom, Advanced and Agentic. */
export function compositionRequest(draft, sessionId) {
    return { clusterId: draft.clusterId, clusterSessionId: sessionId, modelCacheEntryId: draft.modelCacheEntryId,
        validateCluster: draft.sourceValidate, commit: draft.sourceCommit, namespace: draft.sourceNamespace,
        guideSelection: draft.capabilityOverrides !== undefined || draft.selectedGuides?.length
            ? { guides: draft.selectedGuides, capabilities: draft.capabilityOverrides, guideChoices: draft.guideChoices, mode: draft.sourceRouter.mode, accelerator: draft.accelerator } : undefined,
        finalConfiguration: draft.finalConfiguration, units: draft.sourceUnits, variables: draft.sourceVariables,
        runtimeImage: draft.image || undefined, runtimeImageOverride: draft.runtimeImageExplicit || false,
        runtimeConfiguration: draft.runtimeConfiguration,
        router: draft.sourceRouter.values.length ? draft.sourceRouter : undefined };
}

/** Preserve the same bundle snapshot across preview, detailed editing and deployment. */
export function compositionResult(bundle, draft, clusterName = '') {
    return { files: bundle.files, compositionChecksum: bundle.compositionChecksum, sourceComposition: true,
        target: { clusterId: draft.clusterId, clusterName: clusterName || draft.clusterId, model: draft.model, accelerator: draft.accelerator },
        plan: { source: { commit: bundle.commit }, validation: bundle.validation,
            deploymentBundle: { helm: { version: draft.sourceRouter.version || 'Not selected' }, resources: [bundle.files[0]] } } };
}

/** Read display topology from the generated resources, including final parameter edits. */
export function compositionDisplayDraft(draft, result) {
    if (!result?.sourceComposition) return draft;
    const documents = yaml.loadAll(result.files.find(file => file.name === 'deployment.yaml')?.content || '').filter(Boolean);
    const display = { ...draft };
    for (const doc of documents.filter(item => item.kind === 'Deployment')) {
        const container = doc.spec?.template?.spec?.containers?.find(item => item.name === 'modelserver');
        if (!container) continue;
        const role = /prefill/.test(`${doc.metadata?.name} ${JSON.stringify(doc.spec.template.metadata?.labels || {})}`) ? 'prefill' : 'decode';
        const command = [...(container.command || []), ...(container.args || [])].join(' ');
        const tp = command.match(/--tensor-parallel-size(?:=|\s+)(\d+)/)?.[1];
        if (role === 'prefill') { display.prefillReplicas = String(doc.spec.replicas ?? 1); if (tp) display.prefillTp = tp; }
        else { display.replicas = String(doc.spec.replicas ?? 1); if (tp) display.tp = tp; }
    }
    return display;
}
