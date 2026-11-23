import { PARAMETER_FIELDS } from './capabilityCatalog.js';

export function createBlueprint() {
    return { ...Object.fromEntries(PARAMETER_FIELDS.map(field => [field.key, ''])), schemaVersion: 'inference-blueprint.v1', clusterId: '', profileId: '', model: '', image: '',
        sourceMode: false, sourceValidate: true, sourceCommit: '', sourceNamespace: 'llm-d', sourceUnits: [], sourceVariables: {}, sourceRouter: { mode: 'standalone', release: 'llm-d', version: '', values: [] }, sourceGuides: [],
        pd: false, precise: false, cache: 'none', variant: '', replicas: '1', tp: '1', prefillReplicas: '1', prefillTp: '1',
        maxModelLen: '', maxNumSeqs: '', blockSize: '', maxNumBatchedTokens: '', gpuMemoryUtilization: '', cacheCpuGiB: '16', routerSourceCommit: '', routerSourceKey: '', routerValues: '', engineOverrides: '' };
}
/** Resolve only hardware reported by the selected cluster; unknown IDs stay unresolved. */
export function blueprintClusterProfiles(overview, profiles) {
    const summary = overview?.kubernetes?.hardware?.accelerators || [];
    const ids = [...new Set(summary.length ? summary.filter(item => item.gpuCount > 0).map(item => item.id)
        : (overview?.kubernetes?.nodes || []).flatMap(node => Object.entries(node.gpuByProfile || {}).filter(([, count]) => count > 0).map(([id]) => id)))];
    return ids.map(id => profiles.find(profile => profile.id === id) || { id, display_name: id });
}
