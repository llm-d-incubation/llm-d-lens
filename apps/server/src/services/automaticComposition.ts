import yaml from 'js-yaml';
import { createBlueprint, blueprintClusterProfiles } from '../../../frontend/src/features/inference-designer/blueprint.js';
import { compositionOptions, compositionDefaults, resolveCompositionDraft, compositionRequest, compositionResult } from '../../../frontend/src/features/inference-designer/compositionPolicy.js';
import { compositionDeployment } from '../../../frontend/src/features/inference-designer/compositionDeployment.js';
import { compositionBackendRequest, loadCompositionModel } from './guideCompositionModel.ts';
import { guideSourceInventory, generateSourceComposition, validateSourceComposition } from './guideComposition.ts';
import { loadClusterSources } from './clusterSources.ts';
import { clusterSessionKubeconfig } from './clusterSession.ts';
import { runtimeArgument } from './modelServerConfiguration.ts';

/* Source inventories and Kubernetes payloads are schema-dynamic. */
/* eslint-disable @typescript-eslint/no-explicit-any */
const fail = (message: string, status = 409) => Object.assign(new Error(message), { status });

/** Check the actual bundle against the managed lifecycle and the live planning budget. */
export function validateAutomaticBundle(bundle: any, allowedKinds: string[], budget: number, profile: any) {
    const documents: any[] = yaml.loadAll(bundle.files.find((file: any) => file.name === 'deployment.yaml')?.content || '').filter(Boolean);
    const unsupported = documents.filter(doc => doc.kind !== 'Namespace' && !allowedKinds.includes(doc.kind)).map(doc => doc.kind);
    if (unsupported.length) throw fail(`Managed deployment does not support: ${[...new Set(unsupported)].join(', ')}.`);
    const matches = (name: string) => (profile.resource_prefixes || []).some((prefix: string) => name === prefix || name.startsWith(`${prefix}/`));
    let demand = 0;
    for (const doc of documents.filter(doc => doc.kind === 'Deployment')) {
        const pod = doc.spec?.template?.spec || {};
        const cards = (container: any) => Object.entries({ ...container.resources?.limits, ...container.resources?.requests })
            .filter(([key]) => matches(key)).reduce((sum, [, value]) => sum + Number(value), 0);
        let sidecars = 0, peak = 0;
        for (const init of pod.initContainers || []) {
            if (init.restartPolicy === 'Always') sidecars += cards(init);
            peak = Math.max(peak, sidecars + (init.restartPolicy === 'Always' ? 0 : cards(init)));
        }
        const ordinary = (pod.containers || []).reduce((sum: number, container: any) => sum + cards(container), 0);
        const claims = (pod.resourceClaims || []).reduce((sum: number, ref: any) => {
            const claim = documents.find(item => ['ResourceClaim', 'ResourceClaimTemplate'].includes(item.kind)
                && item.metadata?.name === (ref.resourceClaimTemplateName || ref.resourceClaimName));
            const spec = claim?.kind === 'ResourceClaimTemplate' ? claim.spec?.spec : claim?.spec;
            return sum + (spec?.devices?.requests || []).reduce((total: number, request: any) => total + Math.max(0,
                ...(request.exactly ? [request.exactly] : request.firstAvailable || [request])
                    .filter((item: any) => (profile.device_classes || []).includes(item.deviceClassName)).map((item: any) => Number(item.count ?? 1))), 0);
        }, 0);
        const scaleLimit = Math.max(Number(doc.spec?.replicas ?? 1), ...documents.filter(item => ['HorizontalPodAutoscaler', 'ScaledObject'].includes(item.kind)
            && item.spec?.scaleTargetRef?.name === doc.metadata?.name).map(item => Number(item.spec.maxReplicas ?? item.spec.maxReplicaCount ?? doc.spec?.replicas ?? 1)));
        demand += scaleLimit * (Math.max(ordinary + sidecars, peak) + claims);
    }
    if (!Number.isFinite(demand) || demand < 1 || demand > budget) throw fail(`Generated resources need ${demand} accelerator cards; the current budget is ${budget}.`);
    return { documents, acceleratorCount: demand };
}

/** Revalidate final edited YAML against live capacity; planning estimates cannot override its arguments. */
export async function validateCompositionTarget(bundle: any, clusterId: string, binding: any, profileId: string, context?: any) {
    const [hardware, overview, capabilities] = context ? context : await Promise.all([
        compositionBackendRequest('/api/v1/hardware/capabilities'),
        compositionBackendRequest(`/api/cluster/overview?clusterId=${encodeURIComponent(clusterId)}`),
        compositionBackendRequest('/api/v1/configurations/capabilities'),
    ]);
    const profile = blueprintClusterProfiles(overview, hardware.profiles || []).find((item: any) => item.id === profileId);
    if (!profile) throw fail('The selected hardware profile is absent from the target cluster.');
    const capacity = overview.kubernetes?.hardware || {};
    const checked = validateAutomaticBundle(bundle, capabilities.composition_resource_kinds || [], Number(capacity.availableGpuCount || 0), profile);
    const memory = Number(capacity.vramBytes) / Number(capacity.gpuCount) / 1024 ** 3;
    if (!(memory > 0)) throw fail('Live accelerator memory is unavailable.');
    for (const doc of checked.documents.filter((item: any) => item.kind === 'Deployment')) for (const container of doc.spec?.template?.spec?.containers || []) {
        if (container.name !== 'modelserver') continue;
        const contextLength = Number(runtimeArgument(container, 'max-model-len'));
        const memoryUtilization = Number(runtimeArgument(container, 'gpu-memory-utilization'));
        const tp = Number(runtimeArgument(container, 'tensor-parallel-size'));
        if (!(contextLength > 0 && memoryUtilization > 0 && tp > 0)) throw fail('Planned model servers must retain explicit context length, memory utilization and tensor parallelism.');
        const result = await compositionBackendRequest('/api/v1/capacity/estimate', {
            model: binding.model, gpu_memory_gib: memory, tensor_parallel_size: tp,
            replicas: Number(doc.spec.replicas || 1), max_model_len: contextLength,
            gpu_memory_utilization: memoryUtilization,
        });
        if (!result.is_deployable) throw fail(`Final model-server capacity check failed: ${(result.rejection_reasons || []).join('; ')}`);
    }
}

/** A maximal compatible set in Advanced; an explicitly selected subset in Agentic. */
export async function composeOptimizationPolicy({ catalog, draft, mode, selection, render }: any) {
    let current = resolveCompositionDraft(catalog, draft);
    let bundle = await render(current);
    const decisions: any[] = [];
    const selected = new Set(selection.enabled);
    const initial = new Set(current.sourcePlan.enabledCapabilities || []);
    for (const option of compositionOptions(catalog)) {
        if (initial.has(option.id)) {
            decisions.push({ ...option, state: 'enabled', reason: 'Included by the selected serving recipe and its dependencies.' });
            continue;
        }
        if (mode === 'agentic' && !selected.has(option.id)) {
            decisions.push({ ...option, state: 'not_selected', reason: selection.rationale });
            continue;
        }
        try {
            // Optional protocols and model-specific recipes require explicit intent, not just free GPUs.
            if (['batch', 'agentic', 'payload', 'encode-split', 'omni', 'multi-model'].includes(option.id)
                && mode === 'advanced' && !draft.requestedCapabilities?.includes(option.id)) {
                throw fail('This changes the API protocol or model modality. Select it explicitly in the 3D editor if required.');
            }
            if (option.guide.startsWith('models/') && !draft.model.toLowerCase().includes(option.guide.slice(7).toLowerCase())) {
                throw fail('This recipe is specific to a different model.');
            }
            if (option.id === 'tracing' && !draft.otelEndpoint) throw fail('A reachable tracing collector has not been configured.');
            const next = resolveCompositionDraft(catalog, { ...current, finalConfiguration: undefined,
                capabilityOverrides: { ...current.capabilityOverrides, [option.id]: true, ...(option.id === 'disk-cache' ? { cache: true } : {}) } });
            const nextBundle = await render(next);
            current = next; bundle = nextBundle;
            decisions.push({ ...option, state: 'enabled', reason: mode === 'advanced'
                ? 'Compatible with the selected topology, resource budget and target admission checks.' : selection.rationale });
        } catch (error: any) {
            if ([401, 403].includes(error.status)) throw error;
            decisions.push({ ...option, state: 'blocked', reason: error.message });
        }
    }
    // Required capabilities can have been added while processing another optimization.
    const enabled = new Set(current.sourcePlan.enabledCapabilities || []);
    for (const decision of decisions) if (enabled.has(decision.id)) {
        decision.state = 'enabled';
        if (!initial.has(decision.id) && !current.capabilityOverrides[decision.id]) decision.reason = 'Required by another enabled optimization.';
    }
    return { draft: current, bundle, decisions };
}

/** Shared generation API: resolve trusted plan inputs, compose, validate and return an editable snapshot. */
export async function automaticComposition(input: any) {
    if (typeof input.runId !== 'string' || input.runId.length > 100) throw fail('A planning run ID is required.', 400);
    const run = await compositionBackendRequest(`/api/agentic-deployments/${encodeURIComponent(input.runId)}`);
    const request = run.request;
    if (!['advanced', 'agentic'].includes(request.composition_mode) || run.status !== 'awaiting_approval') throw fail('A pending composition plan is required.');
    const source = await loadClusterSources({ clusterSessionId: request.cluster_session_id });
    const [catalog, hardware, overview, cache, volume, capabilities] = await Promise.all([
        guideSourceInventory(source.llmDRepoPath!), compositionBackendRequest('/api/v1/hardware/capabilities'),
        compositionBackendRequest(`/api/cluster/overview?clusterId=${encodeURIComponent(source.id)}`),
        compositionBackendRequest(`/api/v1/model-cache/entries?cluster_id=${encodeURIComponent(source.id)}&storage_volume_id=${encodeURIComponent(request.storage_volume_id)}`),
        compositionBackendRequest(`/api/v1/storage/volumes/${encodeURIComponent(request.storage_volume_id)}`),
        compositionBackendRequest('/api/v1/configurations/capabilities'),
    ]);
    const profiles = blueprintClusterProfiles(overview, hardware.profiles || []);
    if (profiles.length !== 1 || !profiles[0].upstream_variant) throw fail('Automatic composition requires one resolved cluster hardware profile. Use the 3D editor for heterogeneous placement.');
    const profile = profiles[0];
    const entry = cache.items?.find((item: any) => item.status === 'ready' && item.source?.huggingface?.repoId === request.model);
    if (!entry) throw fail('The selected model cache is no longer ready.');
    const namespace = volume.dynamicPvc?.namespace || 'llm-d-bench-storage';
    const binding = await loadCompositionModel(source.id, entry.id, namespace);
    const candidates = (run.candidates || []).filter((candidate: any) => candidate.deployable && candidate.slo_status !== 'not_satisfied');
    const candidate = candidates.find((item: any) => item.id === (input.candidateId || run.selected_candidate.id));
    if (!candidate) throw fail('Select a resource-valid candidate without a known SLA failure.');
    const budget = Number(overview.kubernetes?.hardware?.availableGpuCount || 0);
    if (candidate.required_gpus > budget) throw fail('Cluster capacity changed. Recalculate the recommendation.');
    const selectedGuide = candidate.provider_ref === 'baseline-vllm' ? 'optimized-baseline' : candidate.provider_ref;
    const draft: any = { ...createBlueprint(), sourceMode: true, sourceValidate: true,
        clusterId: source.id, model: binding.model, modelCacheEntryId: entry.id, storageVolumeId: volume.id,
        profileId: profile.id, accelerator: profile.upstream_variant, image: profile.deployment?.runtime_image || '',
        runtimeImageExplicit: false, sourceNamespace: namespace, sourceCommit: catalog.commit,
        selectedGuides: [selectedGuide], capabilityOverrides: {}, guideChoices: {}, sourceVariables: compositionDefaults(catalog),
        sourceGuides: catalog.guides.map((guide: any) => ({ id: guide.id, title: guide.title, disposition: guide.disposition,
            hasAssets: catalog.assets.some((asset: any) => asset.guide === guide.id || asset.guide.startsWith(`${guide.id}/`)) })),
        replicas: String(candidate.replicas), tp: String(candidate.tensor_parallel_size),
        prefillReplicas: String(candidate.prefill_replicas || 1), prefillTp: String(candidate.prefill_tensor_parallel_size || 1),
        runtimeConfiguration: { profileId: profile.id, replicas: candidate.replicas, tp: candidate.tensor_parallel_size,
            prefillReplicas: candidate.prefill_replicas, prefillTp: candidate.prefill_tensor_parallel_size,
            maxModelLen: request.max_model_len, gpuMemoryUtilization: request.gpu_memory_utilization,
            maxNumSeqs: request.max_num_seqs, maxNumBatchedTokens: request.max_num_batched_tokens,
            customParameters: (request.vllm_arguments || []).map((item: any) => ({ ...item, kind: 'argument', target: 'decode' })) },
    };
    const selection = await compositionBackendRequest(`/api/agentic-deployments/${encodeURIComponent(run.id)}/composition-selection`, { options: compositionOptions(catalog) });
    const kubeconfig = clusterSessionKubeconfig(request.cluster_session_id);
    if (!kubeconfig) throw fail('Reconnect the selected cluster before composition.');
    const render = async (next: any) => {
        const bundle = await generateSourceComposition(source.llmDRepoPath!, compositionRequest(next, request.cluster_session_id), binding, kubeconfig);
        await validateCompositionTarget(bundle, source.id, binding, profile.id, [hardware, overview, capabilities]);
        const errors = compositionDeployment(compositionResult(bundle, next)).errors;
        if (errors.length) throw fail(errors.join(' '));
        await validateSourceComposition(bundle.files[0].content, kubeconfig, namespace);
        bundle.validation.clusterValidated = true; bundle.validation.level = 'server-dry-run';
        return bundle;
    };
    const planned = await composeOptimizationPolicy({ catalog, draft, mode: request.composition_mode, selection, render });
    const result = compositionResult(planned.bundle, planned.draft, overview.cluster?.name);
    const policy = { mode: request.composition_mode, objective: 'SLA first, then GPU and resource cost',
        runId: run.id, candidateId: candidate.id, selection, decisions: planned.decisions,
        sla: { status: 'unverified', topologyEvidence: candidate.slo_status,
            reason: 'SLA evidence ranks the initial topology. The composed configuration requires a benchmark to verify its SLA.' } };
    result.files.push({ name: 'optimization-plan.json', content: JSON.stringify(policy, null, 2) });
    return { ...policy, draft: planned.draft, result };
}
