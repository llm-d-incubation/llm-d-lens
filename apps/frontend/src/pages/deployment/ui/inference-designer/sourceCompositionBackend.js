import { selectCluster } from '../../../optimization/ui/OptimizationWorkspace/clusterBackend';
import { requestJson, postJson } from '../../../../shared/api/httpClient';
import { compositionRequest, compositionResult } from '../../model/inference-designer/compositionPolicy';
import { adaptMarkdownGuide, guideDisposition } from '../../model/inference-designer/guideAdapters';
import { officialGuideIndex } from '../../model/inference-designer/officialGuides';

export async function loadSourceCatalog(clusterId) {
    const catalog = await requestJson(`/api/guide-planning/source-catalog?${new URLSearchParams({ clusterId })}`);
    const guides = catalog.guides.map(adaptMarkdownGuide);
    return { ...catalog, guides: guides.map(guide => ({ ...guide, disposition: guideDisposition(guide, catalog) })),
        officialGuides: catalog.officialGuides || officialGuideIndex(catalog.indexText || '', guides) };
}

export const previewSourceComponent = (clusterId, commit, unit) => postJson('/api/guide-planning/source-preview', { clusterId, commit, unit });

/** Preview only. Deployment remains exclusively in the workflow's checksum approval. */
export async function generateSourceBundle(draft) {
    const session = await selectCluster(draft.clusterId);
    const bundle = await postJson('/api/guide-planning/compose', compositionRequest(draft, session?.sessionId));
    return compositionResult(bundle, draft, session.cluster?.name);
}
