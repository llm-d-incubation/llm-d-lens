/* eslint-disable @typescript-eslint/no-explicit-any */
/** Apply the two Kueue source patches without general-purpose shell or patch execution. */
export function applyKueueWorkloadPatch(workload: any, patch: any) {
    const result = structuredClone(workload);
    if (Object.keys(patch).some(key => key !== 'spec') || Object.keys(patch.spec || {}).some(key => !['strategy', 'template'].includes(key))) throw new Error('Unsupported Kueue workload patch.');
    if (patch.spec.strategy) {
        if (JSON.stringify(patch.spec.strategy) !== JSON.stringify({ rollingUpdate: { maxSurge: 0, maxUnavailable: 1 } })) throw new Error('The Kueue rollout strategy changed; review this source revision.');
        result.spec.strategy = { type: 'RollingUpdate', ...patch.spec.strategy };
    }
    if (patch.spec.template) {
        const labels = patch.spec.template.metadata?.labels;
        if (!labels || Object.keys(labels).some(key => key !== 'kueue.x-k8s.io/queue-name') || typeof labels['kueue.x-k8s.io/queue-name'] !== 'string') throw new Error('Unsupported Kueue queue label patch.');
        result.spec.template.metadata = { ...result.spec.template.metadata, labels: { ...result.spec.template.metadata?.labels, ...labels } };
    }
    delete result.status;
    for (const key of ['managedFields', 'uid', 'resourceVersion', 'creationTimestamp', 'generation']) delete result.metadata[key];
    return result;
}
