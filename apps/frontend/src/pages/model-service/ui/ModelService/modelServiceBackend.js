// API client for the model service domain (tokens, visible models, usage).
import { requestJson } from '../../../../shared/api/httpClient';
import { streamSseRequest } from '../../../../shared/api/sse.js';

const base = '/api/v1/model-service';
const tokenPath = (tokenId) => `${base}/tokens/${encodeURIComponent(tokenId)}`;
const jsonHeaders = { 'Content-Type': 'application/json' };

export async function listModelTokens({ signal } = {}) {
    const payload = await requestJson(`${base}/tokens`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Model token list response is invalid');
    return payload.items;
}

export function createModelToken(name) {
    return requestJson(`${base}/tokens`, {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify({ name }),
    });
}

export function regenerateModelToken(name) {
    return requestJson(`${base}/tokens/regenerate`, {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify({ name }),
    });
}

export async function revokeModelToken(tokenId) {
    await requestJson(tokenPath(tokenId), { method: 'DELETE' });
}

export async function listModelNames({ signal } = {}) {
    const payload = await requestJson(`${base}/models`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Model list response is invalid');
    return payload.items;
}

export function getModelServiceConnection({ signal } = {}) {
    return requestJson(`${base}/connection`, { signal });
}

export async function listUsageRecords({ limit = 100, signal } = {}) {
    const payload = await requestJson(`${base}/usage/records?limit=${encodeURIComponent(limit)}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Usage response is invalid');
    return payload.items;
}

export async function listUsageTimeseries({ days = 30, signal } = {}) {
    const payload = await requestJson(`${base}/usage/timeseries?days=${encodeURIComponent(days)}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Usage timeseries response is invalid');
    return payload.items;
}

// --- admin -------------------------------------------------------------------

const admin = `${base}/admin`;
const accessPath = (groupId) => `${admin}/groups/${encodeURIComponent(groupId)}/access`;

export async function getModelAccess(groupId, { signal } = {}) {
    const [grants, options] = await Promise.all([
        requestJson(accessPath(groupId), { signal }),
        requestJson(`${accessPath(groupId)}/options`, { signal }),
    ]);
    if (!Array.isArray(grants?.items) || !Array.isArray(options?.users) || !Array.isArray(options?.groups)) {
        throw new Error('Model access response is invalid');
    }
    return { grants: grants.items, users: options.users, groups: options.groups };
}

export function grantModelAccess(groupId, { subjectType, subjectId }) {
    return requestJson(accessPath(groupId), {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ subjectType, subjectId }),
    });
}

export function revokeModelAccess(groupId, bindingId) {
    return requestJson(`${accessPath(groupId)}/${encodeURIComponent(bindingId)}`, { method: 'DELETE' });
}

export async function listGroups({ signal } = {}) {
    const payload = await requestJson(`${admin}/groups`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Group list response is invalid');
    return payload.items;
}

export function createGroup(body) {
    return requestJson(`${admin}/groups`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) });
}

export async function addModel({ name, clusterId, executionId }) {
    const publicName = name.trim();
    const [groups, members] = await Promise.all([listGroups(), listMembers()]);
    if (members.some((member) => member.clusterId === clusterId && member.executionId === executionId)) {
        throw new Error('This deployment has already been added to a model. Select another provider.');
    }
    const group = groups.find((item) => item.clusterId === clusterId && item.name === publicName);
    if (!group) {
        return { group: await createGroup({ name: publicName, clusterId, executionId }), created: true };
    }
    const providers = members.filter((member) => member.groupId === group.id);
    const weight = 1;
    const result = await updateTrafficDistribution(group.id, {
        expectedWeights: Object.fromEntries(providers.map((member) => [member.executionId, member.weight])),
        members: [
            ...providers.map((member) => ({ executionId: member.executionId, weight: member.weight })),
            { executionId, weight },
        ],
    });
    return { group, created: false, weight, operation: result.operation };
}

export function updateGroup(groupId, body) {
    return requestJson(`${admin}/groups/${encodeURIComponent(groupId)}`, {
        method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(body),
    });
}

export async function deleteGroup(groupId) {
    await requestJson(`${admin}/groups/${encodeURIComponent(groupId)}`, { method: 'DELETE' });
}

export async function setProvidersStatus(groupId, status) {
    const payload = await requestJson(`${admin}/groups/${encodeURIComponent(groupId)}/providers/status`, {
        method: 'PUT',
        headers: jsonHeaders,
        body: JSON.stringify({ status }),
    });
    if (!Array.isArray(payload?.items)) throw new Error('Provider status response is invalid; refresh before retrying.');
    return payload.items;
}

export async function listMembers(groupId, { signal } = {}) {
    const query = groupId ? `?group_id=${encodeURIComponent(groupId)}` : '';
    const payload = await requestJson(`${admin}/members${query}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Member list response is invalid');
    return payload.items;
}

export async function listPublishableDeployments({ signal } = {}) {
    const payload = await requestJson(`${admin}/deployments`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Deployments response is invalid');
    return payload.items;
}

export function createMember(body) {
    return requestJson(`${admin}/members`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) });
}

export function updateMember(memberId, body) {
    return requestJson(`${admin}/members/${encodeURIComponent(memberId)}`, {
        method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(body),
    });
}

export async function updateTrafficDistribution(groupId, body) {
    const payload = await requestJson(`${admin}/groups/${encodeURIComponent(groupId)}/traffic`, {
        method: 'PUT', headers: jsonHeaders, body: JSON.stringify(body),
    });
    if (!Array.isArray(payload?.items) || !payload?.operation?.status) {
        throw new Error('Traffic distribution response is invalid; refresh before retrying.');
    }
    return payload;
}

export async function deleteMember(memberId) {
    await requestJson(`${admin}/members/${encodeURIComponent(memberId)}`, { method: 'DELETE' });
}

export function getUsageAnalytics({ since, until, interval = 'day', groupBy = 'model', userIds, groupIds, clusterIds, signal } = {}) {
    const params = new URLSearchParams({ interval, group_by: groupBy });
    if (since) params.set('since', since);
    if (until) params.set('until', until);
    (userIds || []).forEach((id) => params.append('user_id', id));
    (groupIds || []).forEach((id) => params.append('group_id', id));
    (clusterIds || []).forEach((id) => params.append('cluster_id', id));
    return requestJson(`${admin}/usage/analytics?${params.toString()}`, { signal });
}

export async function listAllUsage({ groupIds, clusterIds, limit = 100, signal } = {}) {
    const params = new URLSearchParams();
    (groupIds || []).forEach((id) => params.append('group_id', id));
    (clusterIds || []).forEach((id) => params.append('cluster_id', id));
    params.set('limit', String(limit));
    const payload = await requestJson(`${admin}/usage?${params.toString()}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Admin usage response is invalid');
    return payload.items;
}

// --- gateway operations ------------------------------------------------------

const gateway = `${admin}/gateway`;

export function getGatewayStatus({ signal } = {}) {
    return requestJson(`${gateway}/status`, { signal });
}

// Reconcile every cluster with members: ensure the shared Gateway and each
// model service's HTTPRoute/IPP. Publishing/removing members triggers this
// automatically; this is the manual retry.
export function reconcileGateway() {
    return requestJson(`${gateway}/reconcile`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify({}) });
}

export function probeGateway() {
    return requestJson(`${gateway}/probe`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify({}) });
}

export function preflightCluster(clusterId) {
    return requestJson(`${gateway}/preflight`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId }),
    });
}

// Install (or reuse) the cluster's shared Gateway using the user-selected provider.
export function installGateway(clusterId, provider) {
    return requestJson(`${gateway}/install`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId, provider }),
    });
}

// Read a cluster's live Inference Payload Processor config (from its ConfigMap).
export function getGatewayIppConfig(clusterId) {
    const query = new URLSearchParams({ clusterId });
    return requestJson(`${gateway}/ipp-config?${query.toString()}`);
}

// Save a cluster's Inference Payload Processor config (PayloadProcessorConfig body).
export function setGatewayIppConfig(clusterId, config) {
    return requestJson(`${gateway}/ipp-config`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId, config }),
    });
}

// Start/stop a data-plane component Deployment (Gateway data plane / EPP / model server).
export function scaleComponent({ clusterId, namespace, name, replicas, kind = 'deployment' }) {
    return requestJson(`${gateway}/scale`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId, namespace, name, replicas, kind }),
    });
}

// Run any data-plane operation and stream its log lines (SSE `log`/`complete`/`error`).
export function streamGatewayOperation(payload, onEvent, signal) {
    return streamSseRequest(`${gateway}/stream`, payload, onEvent, signal);
}

export function streamComponentLogs(payload, onEvent, signal) {
    return streamSseRequest(`${gateway}/logs`, payload, onEvent, signal);
}
