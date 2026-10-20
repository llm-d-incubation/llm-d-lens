import { loadCluster, selectCluster } from '../../pages/optimization/ui/OptimizationWorkspace/clusterBackend';
import { requestJson, postJson } from '../../shared/api/httpClient';
import { renderConfiguration, saveConfiguration } from '../../pages/optimization/ui/OptimizationWorkspace/configurationBackend';
import { startLocalDeployment } from '../../pages/optimization/ui/OptimizationWorkspace/remoteDeployBackend';
import { compositionRenderRequest } from '../../features/inference-designer/compositionDeployment';
import { compositionRequest, compositionResult } from '../../features/inference-designer/compositionPolicy';

export const loadSourceCatalog = clusterId => requestJson(`/api/guide-planning/source-catalog?${new URLSearchParams({ clusterId })}`);
export const previewSourceComponent = (clusterId, commit, unit) => postJson('/api/guide-planning/source-preview', { clusterId, commit, unit });
export async function generateSourceBundle(draft) {
    const session = await selectCluster(draft.clusterId);
    const bundle = await postJson('/api/guide-planning/compose', compositionRequest(draft, session?.sessionId));
    return compositionResult(bundle, draft, session.cluster?.name);
}

export const generateAutomaticComposition = (runId, candidateId) => postJson('/api/guide-planning/automatic-composition', { runId, candidateId });

/** Publish the exact generated snapshot and use the same run API as Evaluation. */
export async function deploySourceBundle(result, endpoint, name) {
    const cluster = await loadCluster(result.target.clusterId);
    const session = await selectCluster(result.target.clusterId);
    const snapshot = { ...result, target: { ...result.target, repository: cluster.llmDRepoUrl || cluster.llmDRepoPath, ref: cluster.llmDRef } };
    const prepared = compositionRenderRequest(snapshot, endpoint);
    const rendered = await renderConfiguration(prepared.candidate, { ...prepared.render,
        cluster_ref: { id: cluster.id, name: cluster.name, session_id: session.sessionId, connection: 'managed-kubeconfig' },
        deployment: prepared.deployment, deploymentName: name.trim(), modelSecret: { mode: 'none' },
    });
    const saved = await saveConfiguration({ ...rendered.deployable_configuration, provenance: {
        ...rendered.deployable_configuration.provenance, composition: prepared.documents.length,
        source_composition: prepared.provenance,
    } }, 'source-composition.yaml');
    return startLocalDeployment([saved.artifact.deployable_configuration], {
        cluster_session_id: session.sessionId, cluster_server_id: cluster.id,
        deployment_name: name.trim(), deployment_source: { kind: 'source-composition', resolved_repository: cluster.llmDRepoPath, ref: cluster.llmDRef },
    });
}
