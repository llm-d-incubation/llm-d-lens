import { configureModelServer } from './modelServerConfiguration.ts';
import { applyRuntimeOverrides, runtimeOverrides } from './configurationOverrides.ts';
import { compositionWorkloads } from './guideCompositionReferences.ts';

/* Kubernetes and hardware profile payloads are schema-dynamic. */
/* eslint-disable @typescript-eslint/no-explicit-any */

/** Apply the planned topology to actual source workloads using registry-owned resources. */
export function applyCompositionRuntime(documents: any[], input: any, profile: any, model: string) {
    if (!input) return;
    if (!profile?.id || profile.id !== input.profileId) throw new Error('A registered hardware profile is required.');
    const positive = (value: any, label: string) => {
        if (!Number.isSafeInteger(value) || value < 1) throw new Error(`${label} must be a positive integer.`);
        return value;
    };
    const decode = { replicas: positive(input.replicas, 'Replicas'), tp: positive(input.tp, 'Tensor parallelism') };
    const prefill = input.prefillReplicas == null ? null : {
        replicas: positive(input.prefillReplicas, 'Prefill replicas'), tp: positive(input.prefillTp, 'Prefill tensor parallelism'),
    };
    const overrides = runtimeOverrides({ ...input, modelServer: 'vllm' });
    const resourceMatches = (key: string) => (profile.resource_prefixes || []).some((prefix: string) => key === prefix || key.startsWith(`${prefix}/`));
    let servingCount = 0, prefillCount = 0;
    for (const { doc, template } of compositionWorkloads(documents)) {
        const engines = (template.spec.containers || []).filter((container: any) => container.name === 'modelserver'
            || [...(container.command || []), ...(container.args || [])].some((arg: string) => /\bvllm\s+serve\b/.test(arg)));
        if (!engines.length) continue;
        if (doc.kind !== 'Deployment') throw new Error('Automatic composition requires Deployment model workloads.');
        const isPrefill = /prefill/.test(`${doc.metadata?.name} ${JSON.stringify(template.metadata?.labels || {})}`);
        if (isPrefill && !prefill) throw new Error('The source has a Prefill role absent from the planned topology.');
        const role = isPrefill ? prefill! : decode;
        if (isPrefill) prefillCount++; else servingCount++;
        doc.spec.replicas = role.replicas;
        for (const container of engines) {
            configureModelServer(container, model, role.tp, model, true);
            applyRuntimeOverrides(container, overrides.filter(row => row.target === 'both' || row.target === (isPrefill ? 'prefill' : 'decode')));
            for (const group of ['requests', 'limits']) for (const key of Object.keys(container.resources?.[group] || {})) {
                if (resourceMatches(key)) container.resources[group][key] = role.tp;
            }
        }
        for (const ref of template.spec.resourceClaims || []) {
            const claim = documents.find(item => ['ResourceClaimTemplate', 'ResourceClaim'].includes(item.kind)
                && item.metadata?.name === (ref.resourceClaimTemplateName || ref.resourceClaimName));
            const spec = claim?.kind === 'ResourceClaimTemplate' ? claim.spec?.spec : claim?.spec;
            for (const request of spec?.devices?.requests || []) for (const selected of request.exactly ? [request.exactly] : request.firstAvailable || [request]) {
                if ((profile.device_classes || []).includes(selected.deviceClassName)) selected.count = role.tp;
            }
        }
    }
    if (servingCount !== 1 || (prefill ? prefillCount !== 1 : prefillCount !== 0)) {
        throw new Error('The rendered serving roles do not match the planned topology. Inspect this recipe in the 3D editor.');
    }
}
