/* eslint-disable @typescript-eslint/no-explicit-any */
const scoped = (doc: any) => doc.metadata?.namespace || 'default';
export const compositionWorkloads = (documents: any[]) => documents.flatMap(doc => {
    const templates = doc.kind === 'DisaggregatedSet' ? (doc.spec?.roles || []).flatMap((role: any) => [role.spec?.leaderWorkerTemplate?.leaderTemplate, role.spec?.leaderWorkerTemplate?.workerTemplate]) : doc.kind === 'Pod' ? [{ metadata: doc.metadata, spec: doc.spec }] : doc.kind === 'LeaderWorkerSet' ? [doc.spec?.leaderWorkerTemplate?.leaderTemplate, doc.spec?.leaderWorkerTemplate?.workerTemplate] : doc.kind === 'Job' ? [doc.spec?.template] : [doc.spec?.template];
    return templates.filter(template => template?.spec).map(template => ({ doc, template }));
});

/** Validate internal wiring and collect external resource references for target-cluster checks. */
export function compositionReferences(documents: any[], existingPods: any[] = []) {
    const errors: string[] = [], external = new Map<string, any>();
    const pods = compositionWorkloads([...documents, ...existingPods]);
    const requireResource = (kind: string, name: string, namespace: string, owner: string) => {
        if (!name || documents.some(doc => doc.kind === kind && doc.metadata?.name === name && scoped(doc) === namespace)) return;
        const key = `${kind}/${namespace}/${name}`;
        external.set(key, { kind, name, namespace, owner });
    };
    for (const { doc, template } of pods) {
        const namespace = scoped(doc), owner = `${doc.kind}/${doc.metadata.name}`;
        const spec = template.spec;
        if (spec.serviceAccountName && spec.serviceAccountName !== 'default') requireResource('ServiceAccount', spec.serviceAccountName, namespace, owner);
        for (const volume of spec.volumes || []) {
            if (volume.secret && !volume.secret.optional) requireResource('Secret', volume.secret.secretName, namespace, owner);
            if (volume.configMap && !volume.configMap.optional) requireResource('ConfigMap', volume.configMap.name, namespace, owner);
            if (volume.persistentVolumeClaim) requireResource('PersistentVolumeClaim', volume.persistentVolumeClaim.claimName, namespace, owner);
        }
        for (const container of [...(spec.containers || []), ...(spec.initContainers || [])]) {
            for (const env of container.env || []) for (const [key, kind] of [['secretKeyRef', 'Secret'], ['configMapKeyRef', 'ConfigMap']]) {
                const ref = env.valueFrom?.[key]; if (ref && !ref.optional) requireResource(kind, ref.name, namespace, owner);
            }
            for (const env of container.envFrom || []) for (const [key, kind] of [['secretRef', 'Secret'], ['configMapRef', 'ConfigMap']]) {
                const ref = env[key]; if (ref && !ref.optional) requireResource(kind, ref.name, namespace, owner);
            }
        }
    }
    const scaling = new Map<string, string>();
    for (const doc of documents) {
        const namespace = scoped(doc), owner = `${doc.kind}/${doc.metadata.name}`;
        if (['ScaledObject', 'HorizontalPodAutoscaler'].includes(doc.kind)) {
            const target = doc.spec?.scaleTargetRef;
            if (target?.name) {
                const key = `${namespace}/${target.kind || 'Deployment'}/${target.name}`;
                if (scaling.has(key)) errors.push(`${owner} and ${scaling.get(key)} both control ${key}. Select one autoscaling policy.`);
                scaling.set(key, owner);
                requireResource(target.kind || 'Deployment', target.name, namespace, owner);
            }
        }
        if (['TriggerAuthentication', 'ClusterTriggerAuthentication'].includes(doc.kind)) for (const ref of doc.spec?.secretTargetRef || []) requireResource('Secret', ref.name, namespace, owner);
        if (doc.kind === 'HTTPRoute') {
            for (const ref of doc.spec?.parentRefs || []) requireResource(ref.kind || 'Gateway', ref.name, ref.namespace || namespace, owner);
            for (const rule of doc.spec?.rules || []) for (const ref of rule.backendRefs || []) requireResource(ref.kind || 'Service', ref.name, ref.namespace || namespace, owner);
        }
        if (doc.kind === 'InferencePool') {
            const selector = doc.spec?.selector?.matchLabels || doc.spec?.selector;
            if (selector && Object.values(selector).every(value => typeof value === 'string') && !pods.some(({ doc: workload, template }) => scoped(workload) === namespace && Object.entries(selector).every(([key, value]) => template.metadata?.labels?.[key] === value))) errors.push(`${owner} selects no generated model workload in ${namespace}. Check its selector and the workload labels.`);
        }
    }
    return { errors, external: [...external.values()] };
}
