import yaml from 'js-yaml';
import { PARAMETER_FIELDS } from './capabilityCatalog.js';
import { structuredParameters } from './parameterBindings.js';
import { validateConfigurationInputs } from '../../pages/evaluation/model/configurationValidation';
import { normalizeGuideSettings } from '@repo/shared/evaluation/guide-settings';

// The map describes deployment intent. Only the existing planner emits manifests.
export const NODES = [
    { id: 'request', label: 'Request & model', description: 'Model identity, target cluster and versioned Guide source.' },
    { id: 'routing', label: 'Routing / EPP', description: 'Endpoint selection, prefix affinity and load-aware scheduling.' },
    { id: 'prefill', label: 'Prefill', description: 'Process prompt tokens and produce KV state.' },
    { id: 'decode', label: 'Decode', description: 'Generate output tokens. In a model server, prefill and decode share the serving workload.' },
    { id: 'payload', label: 'Payload processing', description: 'Multimodal preprocessing and payload offload. No validated composition adapter is registered yet.', unavailable: true },
    { id: 'index', label: 'KV cache indexer', description: 'Precise cache knowledge feeds the routing policy; this is a background data path.' },
    { id: 'transfer', label: 'KV transfer', description: 'Transport KV state between Prefill and Decode model servers. Coupled to the P/D Guide.' },
    { id: 'cache', label: 'Cache tiers', description: 'Reuse KV locally or offload it to a supported CPU cache tier.' },
    { id: 'parallel', label: 'Parallelism & engine', description: 'Tensor parallelism, batching, memory and context limits.' },
    { id: 'speculation', label: 'Speculative decode', description: 'Requires a compatible draft model and engine-specific validation; not wired into this compiler.', unavailable: true },
    { id: 'scaling', label: 'Autoscaling', description: 'Requires a controller, metrics and scaling policy adapter. Not currently generated.', unavailable: true },
    { id: 'observe', label: 'Observability / async', description: 'Cross-cutting operational capabilities. Monitoring remains in the existing deployment tools; async orchestration is not generated here.', unavailable: true },
];
export function createBlueprint() {
    return { ...Object.fromEntries(PARAMETER_FIELDS.map(field => [field.key, ''])), schemaVersion: 'inference-blueprint.v1', clusterId: '', profileId: '', model: '', image: '',
        sourceMode: false, sourceValidate: true, sourceCommit: '', sourceNamespace: 'llm-d', sourceUnits: [], sourceVariables: {}, sourceRouter: { mode: 'standalone', release: 'llm-d', version: '', values: [] }, sourceGuides: [],
        pd: false, precise: false, cache: 'none', variant: '', replicas: '1', tp: '1', prefillReplicas: '1', prefillTp: '1',
        maxModelLen: '', maxNumSeqs: '', blockSize: '', maxNumBatchedTokens: '', gpuMemoryUtilization: '', cacheCpuGiB: '16', routerSourceCommit: '', routerSourceKey: '', routerValues: '', engineOverrides: '' };
}
export function blueprintGuide(draft) {
    return draft.pd ? 'pd-disaggregation' : draft.precise ? 'precise-prefix-cache-routing' : draft.cache !== 'none' ? 'tiered-prefix-cache' : 'optimized-baseline';
}
export function blueprintVariants(draft, providers, profile) {
    const provider = providers.find(item => item.id === blueprintGuide(draft));
    return provider?.variants_by_accelerator?.[profile?.upstream_variant] ?? provider?.variants ?? [];
}
export function blueprintErrors(draft, providers, profile) {
    const errors = [];
    if (draft.sourceMode) {
        if (!draft.clusterId || !draft.sourceCommit) errors.push('Select a cluster and load its source catalog.');
        if (!draft.sourceUnits?.length && !draft.sourcePlan?.existingPoolIntegration) errors.push('Select source workload or supporting components.');
        if (!draft.sourceNamespace) errors.push('Enter the target namespace.');
        if (draft.sourceRouter?.values?.length && !draft.sourceRouter.version) errors.push('Enter the source router chart version.');
        return errors;
    }
    if (!draft.clusterId) errors.push('Select a target cluster with downloaded llm-d software sources.');
    if (!profile?.upstream_variant) errors.push('Select a registered hardware profile.');
    if (!draft.model.trim()) errors.push('Enter the model repository ID.');
    const mechanisms = Number(draft.pd) + Number(draft.precise) + Number(draft.cache !== 'none');
    if (mechanisms > 1) errors.push('P/D, precise indexing and external cache tiers do not yet have a validated combined adapter. Keep one mechanism enabled. Routing values and engine controls remain composable with each.');
    const guide = blueprintGuide(draft);
    if (draft.routerSourceKey && draft.routerSourceKey !== `${guide}:${draft.variant}:${draft.profileId}`) errors.push('The source router snapshot belongs to another Guide, variant or hardware profile. Reload source parameters or clear the snapshot.');
    const provider = providers.find(item => item.id === guide && item.supported);
    if (!provider) errors.push('The backend has not advertised support for this serving topology.');
    if (provider && profile && !provider.accelerators?.includes(profile.upstream_variant)) errors.push('The selected provider does not support this hardware profile.');
    if (draft.pd && !blueprintVariants(draft, providers, profile).includes(draft.variant)) errors.push('Select a supported P/D variant for this hardware profile.');
    for (const key of ['replicas', 'tp', ...(draft.pd ? ['prefillReplicas', 'prefillTp'] : []), 'maxModelLen', 'maxNumSeqs', 'blockSize', 'maxNumBatchedTokens']) {
        if (draft[key] === '' && !['replicas', 'tp', 'prefillReplicas', 'prefillTp'].includes(key)) continue;
        if (!Number.isSafeInteger(Number(draft[key])) || Number(draft[key]) < 1) errors.push(`${key} must be a positive integer.`);
    }
    if (draft.gpuMemoryUtilization !== '' && !(Number(draft.gpuMemoryUtilization) > 0 && Number(draft.gpuMemoryUtilization) <= 1)) errors.push('Memory utilization must be greater than zero and at most one.');
    if (draft.cache !== 'none' && !(Number(draft.cacheCpuGiB) > 0 && Number.isFinite(Number(draft.cacheCpuGiB)))) errors.push('CPU cache capacity must be positive and finite.');
    if (draft.routerValues.trim()) {
        try { const values = yaml.load(draft.routerValues); if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error('Expected a mapping'); }
        catch (error) { errors.push(`Router values: ${error.message}`); }
    }
    try {
        const customRows = [...structuredParameters(draft).engine, ...blueprintOverrides(draft.engineOverrides)];
        normalizeGuideSettings(guide, draft.variant, draft.pd && draft.variant === 'vllm-rdma' ? { rdmaNicCount: draft.rdmaNicCount } : {});
        errors.push(...validateConfigurationInputs({
            guide, modelServer: 'vllm', guideVariant: draft.variant, guideVariants: [],
            replicaVariants: draft.replicas, tensorParallelVariants: draft.tp,
            prefillReplicaVariants: draft.prefillReplicas, prefillTensorParallelVariants: draft.prefillTp,
            runtime: draft, customRows,
        }).filter(item => item.field.startsWith('custom.')).map(item => item.message));
    } catch (error) { errors.push(error.message); }
    return [...new Set(errors)];
}
export function blueprintRequest(draft, providers, profile, sessionId) {
    const errors = blueprintErrors(draft, providers, profile);
    if (errors.length) throw new Error(errors.join(' '));
    if (!sessionId) throw new Error('An active target cluster session is required.');
    const guide = blueprintGuide(draft);
    const guideVariant = draft.pd ? draft.variant : draft.cache === 'native' ? 'native/cpu/base' : draft.cache === 'lmcache' ? 'lmcache-connector/cpu/base' : '';
    return {
        guide, guideVariant, accelerator: profile.upstream_variant, modelServer: 'vllm', model: draft.model.trim(),
        clusterId: draft.clusterId, clusterSessionId: sessionId, source: { mode: 'official' },
        environment: { mode: 'current', kubernetesMode: 'required' }, modelSource: 'huggingface', resourceBasis: 'available',
        replicas: Number(draft.replicas), tensorParallelSize: Number(draft.tp),
        decodeReplicas: Number(draft.replicas), decodeTensorParallelSize: Number(draft.tp),
        prefillReplicas: draft.pd ? Number(draft.prefillReplicas) : 0, prefillTensorParallelSize: draft.pd ? Number(draft.prefillTp) : 0,
        ...Object.fromEntries(['maxModelLen', 'maxNumSeqs', 'blockSize', 'maxNumBatchedTokens', 'gpuMemoryUtilization'].filter(key => draft[key] !== '').map(key => [key, Number(draft[key])])),
        customParameters: [...structuredParameters(draft).engine, ...blueprintOverrides(draft.engineOverrides)],
        runtimeImage: draft.image.trim() || profile.deployment?.runtime_image || '',
        guideSettings: normalizeGuideSettings(guide, guideVariant, { ...(draft.cache !== 'none' ? { cacheCpuGiB: draft.cacheCpuGiB } : {}), ...(draft.pd && draft.variant === 'vllm-rdma' ? { rdmaNicCount: draft.rdmaNicCount } : {}), routerValues: structuredParameters(draft).routerValues }),
    };
}
export function blueprintFiles(plan) {
    if (plan?.validation?.errors?.length) throw new Error(plan.validation.errors.join(' '));
    if (!plan?.plannedDeployment?.content || !plan?.deploymentBundle) throw new Error('The planner did not return a complete valid deployment bundle.');
    if (!/^[a-f0-9]{40}$/.test(plan.source?.commit || '') || plan.deploymentBundle.sourceCommit !== plan.source.commit) throw new Error('The generated source provenance is missing or inconsistent.');
    const bundle = plan.deploymentBundle;
    return [
        { name: 'modelserver.yaml', content: plan.plannedDeployment.content },
        ...(bundle.helm?.values || []), ...(bundle.resources || []),
        ...(Array.isArray(bundle.calibration) ? bundle.calibration : bundle.calibration ? [bundle.calibration] : []),
        { name: 'deployment-bundle.json', content: JSON.stringify(bundle, null, 2) },
        { name: 'provenance.json', content: JSON.stringify({ source: plan.source, validation: plan.validation, deployment: plan.deployment }, null, 2) },
    ];
}

/** Imported drafts are intent only; generated files and claims of validity are never trusted. */
export function parseBlueprint(text) {
    const parsed = yaml.load(text, { schema: yaml.JSON_SCHEMA });
    const defaults = createBlueprint();
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object' || parsed.schemaVersion !== defaults.schemaVersion) throw new Error('Unsupported blueprint schema.');
    for (const [key, value] of Object.entries(parsed)) {
        if (!Object.hasOwn(defaults, key) || typeof value !== typeof defaults[key]) throw new Error(`Invalid blueprint field: ${key}`);
    }
    if (!['none', 'native', 'lmcache'].includes(parsed.cache ?? defaults.cache)) throw new Error('Unsupported cache tier.');
    return { ...defaults, ...parsed };
}

/** Advanced role-specific overrides retain the existing configuration contract. */
export function blueprintOverrides(text = '') {
    if (!text.trim()) return [];
    const rows = yaml.load(text, { schema: yaml.JSON_SCHEMA });
    if (!Array.isArray(rows) || rows.some(row => !row || typeof row !== 'object' || Array.isArray(row)
        || Object.keys(row).some(key => !['target', 'kind', 'name', 'value'].includes(key))
        || ['target', 'kind', 'name', 'value'].some(key => typeof row[key] !== 'string'))) {
        throw new Error('Engine overrides must be a YAML list with string target, kind, name and value fields.');
    }
    return rows;
}

/** Resolve only hardware reported by the selected cluster; unknown IDs stay unresolved. */
export function blueprintClusterProfiles(overview, profiles) {
    const summary = overview?.kubernetes?.hardware?.accelerators || [];
    const ids = [...new Set(summary.length ? summary.filter(item => item.gpuCount > 0).map(item => item.id)
        : (overview?.kubernetes?.nodes || []).flatMap(node => Object.entries(node.gpuByProfile || {}).filter(([, count]) => count > 0).map(([id]) => id)))];
    return ids.map(id => profiles.find(profile => profile.id === id) || { id, display_name: id });
}
