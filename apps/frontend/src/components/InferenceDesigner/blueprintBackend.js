import { selectCluster } from '../../pages/optimization/ui/OptimizationWorkspace/clusterBackend';
import { planGuideDeployment } from '../../pages/optimization/ui/OptimizationWorkspace/guidePlanningBackend';
import { renderConfiguration } from '../../pages/optimization/ui/OptimizationWorkspace/configurationBackend';
import { runtimeControlOverrides } from '../../pages/evaluation/model/configuration';
import { blueprintRequest, blueprintFiles } from '../../features/inference-designer/blueprint';

/** Both the source planner and the configuration validator must accept the snapshot. */
export async function generateBlueprint(draft, providers, profile) {
    const session = await selectCluster(draft.clusterId);
    const request = blueprintRequest(draft, providers, profile, session.sessionId);
    const plan = await planGuideDeployment(request);
    if (draft.routerSourceCommit && draft.routerSourceCommit !== plan.source?.commit) throw new Error('The pinned Guide source changed. Reload router parameters before generating.');
    const files = blueprintFiles(plan);
    const candidate = {
        schema_version: '1.0', type: draft.pd ? 'pd' : 'baseline', candidate_source: { name: 'manual' },
        target: { model: request.model, cluster_id: draft.clusterId, cluster_name: session.cluster?.name || draft.clusterId },
        serving: { replicas: request.replicas, tensor_parallel_size: request.tensorParallelSize, max_model_len: request.maxModelLen },
        prefill: draft.pd ? { replicas: request.prefillReplicas, tensor_parallel_size: request.prefillTensorParallelSize } : null,
        decode: { replicas: request.replicas, tensor_parallel_size: request.tensorParallelSize, max_model_len: request.maxModelLen, max_num_seqs: request.maxNumSeqs },
        guide_variant: request.guideVariant, guide_settings: request.guideSettings,
        custom_parameters: [...runtimeControlOverrides(draft, 'vllm'), ...request.customParameters],
        runtime: { image: request.runtimeImage, modelServer: 'vllm', modelSource: 'huggingface', resourceBasis: 'available' },
    };
    const rendered = await renderConfiguration(candidate, {
        guide_ref: request.guide, template_ref: plan.source.files[0], guide_source: plan.source,
        rendered_manifest: plan.plannedDeployment.content, deployment_bundle: plan.deploymentBundle,
        cluster_ref: { id: draft.clusterId, name: session.cluster?.name || draft.clusterId, session_id: session.sessionId, connection: 'managed-kubeconfig' },
        deployment: plan.deployment, modelSecret: { mode: 'none' },
    });
    return { plan, files, configuration: rendered.deployable_configuration };
}
