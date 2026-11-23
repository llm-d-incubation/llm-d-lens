const HOSTNAME = 'kubernetes.io/hostname';
const INTERMEDIATE = /(nvlink|clique|rack|block|superpod|subblock|switch|domain)/i;
const WIDE = ['topology.kubernetes.io/zone', 'topology.kubernetes.io/region'];

/** Node-label keys usable as a slice domain, narrowest first; mirrors the server recommendation order. */
export function sliceTopologyKeys(labels) {
    const keys = new Set(labels.map(item => item.key));
    const intermediate = [...keys].filter(key => INTERMEDIATE.test(key) && key !== HOSTNAME && !WIDE.includes(key)).sort();
    const preferred = [HOSTNAME, ...intermediate, ...WIDE].filter(key => keys.has(key));
    return [...preferred, ...[...keys].filter(key => !preferred.includes(key)).sort()];
}

export const isDisaggregatedDraft = draft => Boolean(draft?.runtimeConfiguration?.prefillReplicas);

export function automaticSliceSizing(runtime) {
    const slices = runtime.slices ?? 1;
    return { slices: undefined, prefillReplicas: runtime.prefillReplicas == null ? undefined : runtime.prefillReplicas * slices, replicas: runtime.replicas == null ? undefined : runtime.replicas * slices };
}

export function matchesSliceRecommendation(runtime, recommendation) {
    const policy = runtime.placementPolicy || { type: 'None' };
    return Boolean(recommendation && ['slices', 'prefillReplicas', 'replicas'].every(key => recommendation[key] === runtime[key])
        && recommendation.placementPolicy?.type === policy.type && (recommendation.placementPolicy?.topology || '') === (policy.topology || ''));
}

/** Mirrors the server rule: exclusive slices conflict with pinning all Pods to one node; node-scoped slices also conflict with separating Pods. */
export function podPlacementConflict(podPlacement, slices, policy) {
    if (!policy || policy.type === 'None') return '';
    if (podPlacement === 'same-node' && slices > 1) return `Pod placement 'same-node' puts every Pod on one node, but slice placement on ${policy.topology} puts each slice in a different domain. Use one slice, slice placement None, or another Pod placement.`;
    if (policy.topology !== HOSTNAME) return '';
    if (['balanced', 'spread'].includes(podPlacement)) return `Pod placement '${podPlacement}' separates model-server Pods across nodes, but slice placement on ${HOSTNAME} keeps each slice's Prefill and Decode on one node. Choose a wider topology key or None.`;
    return '';
}
