export const MAX_TRAFFIC_WEIGHT = 1_000_000;

export function availableModelProviders(deployments, members, clusterId) {
    const published = new Set(
        members.filter((member) => member.clusterId === clusterId).map((member) => member.executionId),
    );
    return deployments.filter((deployment) => (
        deployment.clusterId === clusterId && deployment.status === 'ready'
        && deployment.endpointKind === 'llm-d-epp'
        && !published.has(deployment.executionId)
    ));
}

export function filterServiceFlow(clusters, memberGroups, { clusterId = '', modelQuery = '', status = '' } = {}) {
    const query = modelQuery.trim().toLowerCase();
    const selectedClusters = clusters.filter((cluster) => !clusterId || cluster.clusterId === clusterId);
    const selectedIds = new Set(selectedClusters.map((cluster) => cluster.clusterId));
    const groups = memberGroups
        .filter((group) => (!status || group.status === status) && group.name.toLowerCase().includes(query))
        .map((group) => ({
            ...group,
            clusters: group.clusters.filter((cluster) => selectedIds.has(cluster.clusterId)),
        }))
        .filter((group) => group.clusters.length > 0);
    const matchingIds = new Set(groups.flatMap((group) => group.clusters.map((cluster) => cluster.clusterId)));
    return {
        clusters: (query || status)
            ? selectedClusters.filter((cluster) => matchingIds.has(cluster.clusterId))
            : selectedClusters,
        memberGroups: groups,
    };
}

export function memberIsRoutable(member) {
    if (!member || member.status === 'disabled' || Number(member.weight) <= 0) return false;
    if (member.endpointKind && member.endpointKind !== 'llm-d-epp') return false;
    const components = member.components || {};
    return !['epp', 'inferencePool', 'modelServer'].some((key) => components[key] === 'missing');
}

export function routedTrafficTotal(members) {
    if (!Array.isArray(members)) return 0;
    return members.reduce((sum, member) => (
        memberIsRoutable(member) ? sum + Number(member.weight || 0) : sum
    ), 0);
}

export function effectiveTrafficShare(member, total) {
    if (!memberIsRoutable(member) || !total) return '0%';
    const weight = Number(member.weight || 0);
    const share = weight / total * 100;
    return share < 0.01 ? '<0.01%' : `${Number(share.toFixed(2))}%`;
}

export function parseTrafficWeight(value) {
    if (!/^\d+$/.test(String(value))) return null;
    const weight = Number(value);
    return Number.isInteger(weight) && weight >= 0 && weight <= MAX_TRAFFIC_WEIGHT ? weight : null;
}

export function trafficDistributionError(rows) {
    if (!rows.length || rows.length > 16) return 'Choose between 1 and 16 deployment providers.';
    if (new Set(rows.map((row) => row.executionId)).size !== rows.length) return 'Each deployment can be added only once.';
    if (rows.some((row) => parseTrafficWeight(row.weight) === null)) return 'Weights must be whole numbers from 0 to 1000000.';
    if (!rows.some((row) => row.status !== 'disabled' && parseTrafficWeight(row.weight) > 0)) {
        return 'At least one enabled deployment must have a positive weight.';
    }
    return '';
}

export function trafficTotal(rows) {
    if (rows.some((row) => parseTrafficWeight(row.weight) === null)) return null;
    return rows.reduce((sum, row) => sum + (row.status === 'disabled' ? 0 : parseTrafficWeight(row.weight)), 0);
}

export function trafficShare(row, total) {
    const weight = parseTrafficWeight(row.weight);
    if (weight === null || total === null) return 'Unknown';
    if (row.status === 'disabled' || total === 0 || weight === 0) return '0%';
    const percent = weight / total * 100;
    return percent < 0.01 ? '<0.01%' : `${Number(percent.toFixed(2))}%`;
}
