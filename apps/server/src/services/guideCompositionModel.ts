import yaml from 'js-yaml';
import { internalHeadersFor } from '../middleware/internalAuth.ts';
import { fetchJsonWithTimeout } from '../infrastructure/http.ts';
import { configureModelServer } from './modelServerConfiguration.ts';
import { applyRuntimeOverrides } from './configurationOverrides.ts';
import { compositionWorkloads } from './guideCompositionReferences.ts';

/* Source manifests and backend resource payloads are schema-dynamic. */
/* eslint-disable @typescript-eslint/no-explicit-any */
const fail = (message: string, status = 409) => Object.assign(new Error(message), { status });

/** Preserve the authenticated caller across composition domain service requests. */
export async function compositionBackendRequest(route: string, body?: unknown) {
    const method = body === undefined ? 'GET' : 'POST';
    const base = process.env.SIMULATION_API_URL || process.env.OPTIMALBENCH_URL || 'http://127.0.0.1:8081';
    const { response, payload } = await fetchJsonWithTimeout(new URL(route, base).href, {
        method, headers: { accept: 'application/json', 'content-type': 'application/json', ...internalHeadersFor(method, route) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }, body === undefined ? 15000 : 180000);
    if (!response.ok) throw fail(String(payload.detail || payload.error || 'Composition service request failed'), response.status);
    return payload as any;
}

/** Re-read scoped backend records; browser mount paths and model identities are never trusted. */
export async function loadCompositionModel(clusterId: string, entryId: string, namespace: string) {
    const entry = await compositionBackendRequest(`/api/v1/model-cache/entries/${encodeURIComponent(entryId)}`);
    const volume = await compositionBackendRequest(`/api/v1/storage/volumes/${encodeURIComponent(entry.storageVolumeId)}`);
    return compositionModelBinding(clusterId, namespace, entry, volume);
}

/** Generation uses an existing PVC in its home namespace; it never provisions storage. */
export function compositionModelBinding(clusterId: string, namespace: string, entry: any, volume: any) {
    if (entry.clusterId !== clusterId || volume.clusterId !== clusterId || entry.storageVolumeId !== volume.id) throw fail('Model cache and storage must belong to the target cluster.');
    if (entry.status !== 'ready' || volume.status !== 'ready' || entry.pendingSyncNodes?.length || entry.nodeProgress?.some((node: any) => node.status !== 'ready')) throw fail('The model and its storage must be ready and synchronized before generation.');
    if (entry.source?.kind !== 'huggingface' || !entry.source.huggingface?.repoId) throw fail('This cached model source does not expose a deployable repository identity.');
    const storageNamespace = volume.dynamicPvc?.namespace || 'llm-d-bench-storage';
    if (!volume.pvcName || namespace !== storageNamespace) throw fail(`Use the selected storage namespace ${storageNamespace}; generation does not create cross-namespace storage mounts.`);
    return { entryId: entry.id, storageVolumeId: volume.id, model: entry.source.huggingface.repoId, revision: entry.source.huggingface.revision || 'main', namespace, pvcName: volume.pvcName };
}

/** Bind cached weights to each model role, preserving engine and parallelism from the Guide. */
export function bindCompositionModel(documents: any[], binding: ReturnType<typeof compositionModelBinding>, runtimeImage?: string) {
    let count = 0;
    for (const { doc, template } of compositionWorkloads(documents)) {
        const spec = template.spec;
        for (const container of spec.containers || []) {
            if (container.name !== 'modelserver' && ![...(container.command || []), ...(container.args || [])].some((arg: string) => /\bvllm\s+serve\b/.test(arg))) continue;
            if ((doc.metadata?.namespace || binding.namespace) !== binding.namespace) throw fail('Model workloads and the selected cache PVC must share a namespace.');
            configureModelServer(container, binding.model, 0, binding.model, true);
            applyRuntimeOverrides(container, [{ target: 'both', kind: 'argument', name: 'revision', value: binding.revision }]);
            if (runtimeImage) container.image = runtimeImage;
            const name = 'lens-model-cache';
            const existing = (spec.volumes || []).find((volume: any) => volume.name === name);
            if (existing && existing.persistentVolumeClaim?.claimName !== binding.pvcName) throw fail('The Guide already defines a conflicting lens-model-cache volume.');
            if ((container.volumeMounts || []).some((mount: any) => mount.mountPath === '/model-cache')) throw fail('The Guide already mounts /model-cache; remove the conflicting source mount before binding a cached model.');
            if (!existing) spec.volumes = [...(spec.volumes || []), { name, persistentVolumeClaim: { claimName: binding.pvcName, readOnly: true } }];
            container.volumeMounts = [...(container.volumeMounts || []), { name, mountPath: '/model-cache', readOnly: true }];
            const environment = { HF_HOME: '/model-cache', HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1' };
            container.env = [...(container.env || []).filter((env: any) => !Object.hasOwn(environment, env.name) && !['HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'].includes(env.name)), ...Object.entries(environment).map(([name, value]) => ({ name, value }))];
            count++;
        }
    }
    // Model identities also feed router plugins and autoscaler metrics.
    const updateIdentity = (value: any): void => {
        if (!value || typeof value !== 'object') return;
        for (const [key, child] of Object.entries(value)) {
            if (['model', 'modelName', 'model_name', 'baseModel', 'llm-d.ai/model-id'].includes(key) && typeof child === 'string') value[key] = binding.model;
            else if (key === 'llm-d.ai/model' && typeof child === 'string') value[key] = binding.model.split('/').at(-1)!.replace(/[^A-Za-z0-9_.-]/g, '-').slice(0, 63).replace(/[^A-Za-z0-9]+$/, '');
            else updateIdentity(child);
        }
        if (['MODEL', 'MODEL_NAME', 'MODEL_ID'].includes(value.name) && typeof value.value === 'string') value.value = binding.model;
    };
    updateIdentity(documents);
    for (const doc of documents.filter(doc => doc.kind === 'ConfigMap')) for (const [key, text] of Object.entries(doc.data || {})) {
        if (typeof text !== 'string' || !/\.(ya?ml|json)$/.test(key)) continue;
        try {
            const config = yaml.load(text, { schema: yaml.JSON_SCHEMA });
            if (!config || typeof config !== 'object') continue;
            updateIdentity(config);
            doc.data[key] = key.endsWith('.json') ? JSON.stringify(config) : yaml.dump(config, { noRefs: true, lineWidth: -1 });
        } catch { /* Non-YAML runtime templates retain source syntax. */ }
    }
    if (!count) throw fail('No supported model-server command was found for the selected cached model.');
    return count;
}
