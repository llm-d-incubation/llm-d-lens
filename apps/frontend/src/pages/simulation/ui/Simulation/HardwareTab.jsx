// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import React, { useMemo } from 'react';
import {
    Cpu,
    Database,
    HardDrive,
    Layers,
    MemoryStick,
    Server,
} from 'lucide-react';
import {
    Badge,
    Button,
    EmptyState,
    HelpTooltip,
    Panel,
    SectionLabel,
    StatCard,
} from '../../../../shared/ui';
import {
    displayName,
    effectivePodRole,
    formatBytePair,
    formatBytes,
    formatCount,
    formatDecimal,
    isBenchmarkRunnerPod,
    isFiniteMetricValue,
    numberAt,
} from '../../model/presentation';

const EMPTY_OBJ = {};
const EMPTY_ARR = [];
const BYTES_PER_GIB = 1024 ** 3;
const MAX_VISIBLE_NICS = 4;

function nonEmptyText(value) {
    if (typeof value !== 'string') return null;
    const trimmed = value.trim();
    return trimmed || null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function sanitizeEndpointUrl(raw) {
    const trimmed = nonEmptyText(raw);
    if (!trimmed) return null;

    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(trimmed)) {
        try {
            const parsed = new URL(trimmed);
            parsed.username = '';
            parsed.password = '';
            parsed.search = '';
            parsed.hash = '';
            const semiIndex = parsed.pathname.indexOf(';');
            if (semiIndex !== -1) {
                parsed.pathname = parsed.pathname.slice(0, semiIndex);
            }
            if (parsed.hostname) {
                return parsed.toString();
            }
        } catch {
            // Fall through to string-based sanitization when URL parsing fails (e.g. malformed port).
        }
    }

    const stripped = trimmed.replace(/[?#;].*$/, '');
    const schemeMatch = stripped.match(/^([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)(.*)$/);
    const prefix = schemeMatch ? schemeMatch[1] : '';
    const rest = schemeMatch ? schemeMatch[2] : stripped;
    const slashIndex = rest.indexOf('/');
    const authority = slashIndex === -1 ? rest : rest.slice(0, slashIndex);
    const path = slashIndex === -1 ? '' : rest.slice(slashIndex);
    const atIndex = authority.lastIndexOf('@');
    const cleanAuthority = atIndex === -1 ? authority : authority.slice(atIndex + 1);
    if (prefix && !cleanAuthority) return null;
    const cleaned = `${prefix}${cleanAuthority}${path}`.trim();
    return cleaned || null;
}

function firstFinite(...values) {
    for (const value of values) {
        if (isFiniteMetricValue(value)) {
            return Number(value);
        }
    }
    return null;
}

function positiveFinite(...values) {
    for (const value of values) {
        if (isFiniteMetricValue(value)) {
            const numeric = Number(value);
            if (numeric > 0) return numeric;
        }
    }
    return null;
}

function sumFinite(items, getter) {
    let total = 0;
    let count = 0;
    for (const item of items) {
        const val = getter(item);
        if (isFiniteMetricValue(val)) {
            total += Number(val);
            count += 1;
        }
    }
    return count > 0 ? total : null;
}

function legacyGbToBytes(gb) {
    const numeric = firstFinite(gb);
    return numeric !== null ? numeric * BYTES_PER_GIB : null;
}

function roleBadgeTone(role) {
    const normalized = String(role || '').toLowerCase();
    if (normalized === 'prefill') return 'info';
    if (normalized === 'decode') return 'violet';
    if (normalized === 'epp' || normalized === 'router') return 'brand';
    return 'neutral';
}

function roleDisplayLabel(role) {
    const normalized = String(role || '').trim().toLowerCase();
    if (normalized === 'epp') return 'EPP';
    if (normalized === 'router') return 'Router';
    if (normalized === 'prefill') return 'Prefill';
    if (normalized === 'decode') return 'Decode';
    if (normalized === 'worker' || normalized === 'model-server') return 'Worker';
    return normalized ? displayName(normalized) : 'Worker';
}

function extractNicNames(networkInterfaces) {
    if (!Array.isArray(networkInterfaces)) return EMPTY_ARR;
    const seen = new Set();
    const names = [];
    for (const entry of networkInterfaces) {
        const name = typeof entry === 'string'
            ? nonEmptyText(entry)
            : nonEmptyText(entry?.device || entry?.name || entry?.interface);
        if (name && !seen.has(name)) {
            seen.add(name);
            names.push(name);
        }
    }
    return names;
}

function buildRoleBreakdown(perPodRows) {
    const counts = {
        prefill: 0,
        decode: 0,
        worker: 0,
        epp: 0,
    };
    const extra = new Map();
    for (const pod of perPodRows) {
        const role = String(effectivePodRole(pod) || 'worker').trim().toLowerCase();
        if (role === 'prefill') counts.prefill += 1;
        else if (role === 'decode') counts.decode += 1;
        else if (role === 'worker' || role === 'model-server') counts.worker += 1;
        else if (role === 'epp' || role === 'router') counts.epp += 1;
        else extra.set(role, (extra.get(role) || 0) + 1);
    }
    const parts = [];
    if (counts.prefill > 0) parts.push(`Prefill: ${formatCount(counts.prefill)}`);
    if (counts.decode > 0) parts.push(`Decode: ${formatCount(counts.decode)}`);
    if (counts.worker > 0) parts.push(`Worker: ${formatCount(counts.worker)}`);
    if (counts.epp > 0) parts.push(`Router: ${formatCount(counts.epp)}`);
    for (const [role, count] of extra.entries()) {
        parts.push(`${roleDisplayLabel(role)}: ${formatCount(count)}`);
    }
    return {
        label: parts.join(' · ') || null,
        servingPodCount: perPodRows.filter((pod) => {
            const role = String(effectivePodRole(pod) || 'worker').trim().toLowerCase();
            return role !== 'epp' && role !== 'router';
        }).length,
    };
}

// eslint-disable-next-line react-refresh/only-export-components
export function resolveCacheHierarchy(serverTelemetry) {
    const cacheConfig = serverTelemetry?.cache_config || serverTelemetry?.cache_config_info || null;
    if (!cacheConfig || typeof cacheConfig !== 'object') return null;

    const engines = Array.isArray(cacheConfig.engines) ? cacheConfig.engines : EMPTY_ARR;
    const firstEngine = engines[0] || EMPTY_OBJ;

    const hbmBytes = positiveFinite(cacheConfig.hbm_capacity_bytes);
    const hbmTokens = positiveFinite(cacheConfig.hbm_capacity_tokens, firstEngine.kv_cache_size_tokens, firstEngine.hbm_capacity_tokens);
    const prefillHbmTokens = positiveFinite(
        cacheConfig.prefill_hbm_capacity_tokens,
        serverTelemetry?.derived?.prefill_hbm_capacity_tokens,
    );
    const decodeHbmTokens = positiveFinite(
        cacheConfig.decode_hbm_capacity_tokens,
        serverTelemetry?.derived?.decode_hbm_capacity_tokens,
    );
    const cpuBytes = positiveFinite(cacheConfig.cpu_capacity_bytes);
    const cpuTokens = positiveFinite(cacheConfig.cpu_capacity_tokens);
    const nvmeBytes = positiveFinite(cacheConfig.nvme_capacity_bytes, cacheConfig.disk_capacity_bytes);
    const effectiveTokens = positiveFinite(cacheConfig.effective_capacity_tokens, hbmTokens);
    const blockSize = positiveFinite(cacheConfig.block_size, firstEngine.block_size);
    const numDeviceBlocks = positiveFinite(cacheConfig.num_gpu_blocks, firstEngine.num_gpu_blocks);
    const maxKvConcurrency = positiveFinite(cacheConfig.kv_cache_max_concurrency, firstEngine.kv_cache_max_concurrency);
    const hostKvConfigured = Boolean(cacheConfig.host_kv_configured || cpuBytes !== null || cpuTokens !== null);
    const nvmeKvConfigured = Boolean(cacheConfig.nvme_kv_configured || nvmeBytes !== null);

    const hasAnyCacheData = [
        hbmBytes,
        hbmTokens,
        prefillHbmTokens,
        decodeHbmTokens,
        cpuBytes,
        cpuTokens,
        nvmeBytes,
        effectiveTokens,
        blockSize,
        numDeviceBlocks,
    ].some((value) => value !== null) || hostKvConfigured || nvmeKvConfigured;

    if (!hasAnyCacheData) return null;

    return {
        hbmBytes,
        hbmTokens,
        prefillHbmTokens,
        decodeHbmTokens,
        cpuBytes,
        cpuTokens,
        nvmeBytes,
        effectiveTokens,
        blockSize,
        numDeviceBlocks,
        maxKvConcurrency,
        hostKvConfigured,
        nvmeKvConfigured,
        engineCount: engines.length,
    };
}

// eslint-disable-next-line react-refresh/only-export-components
export function resolveHardwareSummary(task, telemetry) {
    const serverTelemetry = telemetry?.server_telemetry ?? EMPTY_OBJ;
    const runManifest = telemetry?.run_manifest ?? EMPTY_OBJ;
    const target = runManifest?.target ?? EMPTY_OBJ;
    const host = serverTelemetry?.host_metrics ?? serverTelemetry?.system ?? serverTelemetry?.node_metrics ?? EMPTY_OBJ;
    const summary = serverTelemetry?.summary ?? EMPTY_OBJ;

    const hwProfiles = Array.isArray(serverTelemetry?.hardware_profiles)
        ? serverTelemetry.hardware_profiles
        : EMPTY_ARR;
    const hwProfile = serverTelemetry?.hardware_profile ?? hwProfiles[0] ?? runManifest?.hardware_profile ?? null;
    const accelLabel = nonEmptyText(hwProfile?.ui?.labels?.metric_group)
        || nonEmptyText(hwProfile?.metric_group)
        || nonEmptyText(hwProfile?.display_name)
        || 'Accelerator';

    const rawPerNode = Array.isArray(serverTelemetry?.per_node)
        ? serverTelemetry.per_node.filter((row) => row && typeof row === 'object')
        : EMPTY_ARR;
    const fallbackNodeNames = [];
    const seenFallbackNodes = new Set();
    for (const source of [serverTelemetry?.nodes, host?.nodes]) {
        if (!Array.isArray(source)) continue;
        for (const name of source) {
            const cleaned = nonEmptyText(name);
            if (cleaned && !seenFallbackNodes.has(cleaned)) {
                seenFallbackNodes.add(cleaned);
                fallbackNodeNames.push(cleaned);
            }
        }
    }
    const perNodeRows = rawPerNode.length > 0
        ? rawPerNode
        : fallbackNodeNames.map((node) => ({ node }));

    const rawPerPod = Array.isArray(serverTelemetry?.per_pod) ? serverTelemetry.per_pod : EMPTY_ARR;
    const perPodRows = rawPerPod.filter((pod) => pod && typeof pod === 'object' && !isBenchmarkRunnerPod(pod));

    const endpointModeRaw = nonEmptyText(target?.endpoint_mode)
        || nonEmptyText(task?.endpoint_mode)
        || (
            task?.endpoint_deployment_name
            || task?.endpoint_cluster_name
            || target?.deployment_name
            || target?.cluster_name
                ? 'deployment'
                : 'external'
        );
    const isDeployment = String(endpointModeRaw).toLowerCase() === 'deployment';
    const endpointModeLabel = isDeployment ? 'Deployment' : 'External Endpoint';

    const clusterName = nonEmptyText(target?.cluster_name) || nonEmptyText(task?.endpoint_cluster_name);
    const namespace = nonEmptyText(serverTelemetry?.namespace)
        || nonEmptyText(target?.namespace)
        || nonEmptyText(task?.endpoint_namespace);
    const deploymentName = nonEmptyText(target?.deployment_name) || nonEmptyText(task?.endpoint_deployment_name);
    const endpointUrl = sanitizeEndpointUrl(target?.sanitized_endpoint_url) || sanitizeEndpointUrl(task?.endpoint_url);
    const modelName = nonEmptyText(target?.model_name) || nonEmptyText(task?.model_name);
    const guideType = nonEmptyText(serverTelemetry?.guide_type) || nonEmptyText(task?.guide_type);

    const hasPrefillPod = perPodRows.some((pod) => effectivePodRole(pod) === 'prefill');
    const topologyRaw = nonEmptyText(serverTelemetry?.topology_mode)
        || nonEmptyText(serverTelemetry?.topology?.mode)
        || nonEmptyText(target?.topology_mode)
        || (hasPrefillPod ? 'disaggregated' : null);
    const topologyLabel = topologyRaw
        ? (String(topologyRaw).toLowerCase() === 'disaggregated' ? 'P/D Disaggregation' : displayName(topologyRaw))
        : null;

    const clusterNamespace = [clusterName, namespace].filter(Boolean).join(' / ') || null;
    const targetHeadline = isDeployment
        ? (clusterNamespace || deploymentName || 'Cluster Deployment')
        : 'External Endpoint';

    const profileModels = [];
    const seenModels = new Set();
    for (const profile of [hwProfile, ...hwProfiles]) {
        const models = Array.isArray(profile?.models) ? profile.models : EMPTY_ARR;
        for (const model of models) {
            const text = nonEmptyText(model);
            if (text && !seenModels.has(text)) {
                seenModels.add(text);
                profileModels.push(text);
            }
        }
    }
    const perNodeModels = [];
    for (const row of perNodeRows) {
        const text = nonEmptyText(row?.accelerator_model);
        if (text && !seenModels.has(text)) {
            seenModels.add(text);
            perNodeModels.push(text);
        }
    }
    const acceleratorModel = nonEmptyText(serverTelemetry?.accelerator_summary)
        || (profileModels.length > 0 ? profileModels.join(', ') : null)
        || (perNodeModels.length > 0 ? perNodeModels.join(', ') : null)
        || nonEmptyText(task?.hardware)
        || nonEmptyText(hwProfile?.display_name)
        || 'Not reported';

    const profileName = nonEmptyText(hwProfile?.display_name)
        || nonEmptyText(hwProfile?.id)
        || nonEmptyText(task?.hardware)
        || null;
    const profileVendor = nonEmptyText(hwProfile?.vendor) || null;

    const { label: roleBreakdownLabel, servingPodCount } = buildRoleBreakdown(perPodRows);
    const manifestPodCount = Array.isArray(target?.pods) ? target.pods.length : 0;
    const nodeCount = perNodeRows.length;
    const podCount = perPodRows.length > 0 ? perPodRows.length : manifestPodCount;

    const totalAllocFromNodes = sumFinite(perNodeRows, (row) => row?.allocated_accelerators);
    const totalAllocFromPods = sumFinite(perPodRows, (pod) => pod?.allocated_accelerators);
    const totalAllocFromProfiles = sumFinite(hwProfiles, (profile) => profile?.allocated_accelerators);
    const totalAlloc = totalAllocFromNodes
        ?? totalAllocFromPods
        ?? totalAllocFromProfiles
        ?? firstFinite(hwProfile?.allocated_accelerators, hwProfile?.device_count);

    const totalAccelCap = sumFinite(perNodeRows, (row) => row?.accelerator_capacity);
    const totalAccelAllocatable = sumFinite(perNodeRows, (row) => row?.accelerator_allocatable);

    const totalDeviceMemFromNodes = sumFinite(perNodeRows, (row) => row?.gpu_memory_total_bytes);
    const totalDeviceMemFromPods = sumFinite(
        perPodRows,
        (pod) => firstFinite(pod?.gpu_memory_total_bytes?.mean, pod?.gpu_memory_total_bytes?.max, pod?.gpu_memory_total_bytes),
    );
    const totalDeviceMemBytes = totalDeviceMemFromNodes
        ?? totalDeviceMemFromPods
        ?? positiveFinite(numberAt(summary, ['gpu_memory_total_bytes.mean', 'accelerator_memory_total_bytes.mean']));

    // Only derive "N × perDevice = total" when all device-bearing rows report device memory
    // and per-device capacities are uniform within 1% relative tolerance.
    let uniformDeviceMemCount = null;
    const hasNodeDeviceRows = perNodeRows.some(
        (row) => (firstFinite(row?.allocated_accelerators) ?? 0) > 0 || (firstFinite(row?.gpu_memory_total_bytes) ?? 0) > 0,
    );
    const uniformSourceRows = hasNodeDeviceRows
        ? perNodeRows.map((row) => ({
            count: firstFinite(row?.allocated_accelerators),
            mem: firstFinite(row?.gpu_memory_total_bytes),
        }))
        : perPodRows.map((pod) => ({
            count: firstFinite(pod?.allocated_accelerators),
            mem: firstFinite(pod?.gpu_memory_total_bytes?.mean, pod?.gpu_memory_total_bytes?.max, pod?.gpu_memory_total_bytes),
        }));
    const deviceRowsWithAnySignal = uniformSourceRows.filter(
        (entry) => (entry.count !== null && entry.count > 0) || (entry.mem !== null && entry.mem > 0),
    );
    if (
        deviceRowsWithAnySignal.length > 0
        && deviceRowsWithAnySignal.every((entry) => entry.count !== null && entry.count > 0 && entry.mem !== null && entry.mem > 0)
    ) {
        const perDeviceSizes = deviceRowsWithAnySignal.map((entry) => entry.mem / entry.count);
        const baseline = perDeviceSizes[0];
        if (perDeviceSizes.every((size) => Math.abs(size - baseline) <= baseline * 0.01)) {
            uniformDeviceMemCount = deviceRowsWithAnySignal.reduce((acc, entry) => acc + entry.count, 0);
        }
    }

    const totalCpuCapCores = sumFinite(perNodeRows, (row) => row?.cpu_capacity_cores);
    const totalCpuAllocCores = sumFinite(perNodeRows, (row) => row?.cpu_allocatable_cores);
    const totalHostMemBytes = sumFinite(
        perNodeRows,
        (row) => firstFinite(row?.memory_capacity_bytes, row?.memory_total_bytes),
    ) ?? firstFinite(
        host?.mem_total_bytes,
        host?.memory_total_bytes,
        numberAt(summary, ['host_memory_total_bytes.mean']),
        legacyGbToBytes(host?.memory_total_gb),
    );
    const totalStorageBytes = sumFinite(perNodeRows, (row) => row?.filesystem_size_bytes)
        ?? firstFinite(
            host?.disk_total_bytes,
            host?.filesystem_size_bytes,
            numberAt(summary, ['host_filesystem_size_bytes.mean', 'host_filesystem_size_bytes.max']),
            legacyGbToBytes(host?.disk_total_gb),
        );

    const cacheHierarchy = resolveCacheHierarchy(serverTelemetry);

    return {
        isDeployment,
        endpointModeLabel,
        targetHeadline,
        clusterNamespace,
        deploymentName,
        endpointUrl,
        modelName,
        guideType,
        topologyLabel,
        accelLabel,
        acceleratorModel,
        profileName,
        profileVendor,
        nodeCount,
        podCount,
        servingPodCount,
        roleBreakdownLabel,
        totalAlloc,
        totalAccelCap,
        totalAccelAllocatable,
        totalDeviceMemBytes,
        uniformDeviceMemCount,
        totalCpuCapCores,
        totalCpuAllocCores,
        totalHostMemBytes,
        totalStorageBytes,
        perNodeRows,
        perPodRows,
        cacheHierarchy,
        serverTelemetry,
    };
}

function formatCores(value) {
    if (!isFiniteMetricValue(value)) return '—';
    const numeric = Number(value);
    return `${formatDecimal(numeric)} ${numeric === 1 ? 'core' : 'cores'}`;
}

function formatDeviceMemorySummary(totalBytes, deviceCount) {
    if (!isFiniteMetricValue(totalBytes) || Number(totalBytes) <= 0) return '—';
    const totalText = formatBytes(totalBytes);
    if (!totalText) return '—';
    if (isFiniteMetricValue(deviceCount) && Number(deviceCount) > 0) {
        const perDeviceText = formatBytes(Number(totalBytes) / Number(deviceCount));
        if (perDeviceText) {
            return `${formatCount(deviceCount)} × ${perDeviceText} = ${totalText}`;
        }
    }
    return totalText;
}

export function HardwareTab({ task, telemetry, onSelectTab }) {
    const info = useMemo(() => resolveHardwareSummary(task, telemetry), [task, telemetry]);
    const hasInventory = info.perNodeRows.length > 0 || info.perPodRows.length > 0 || Boolean(info.cacheHierarchy);

    const allocatedCardValue = useMemo(() => {
        if (info.totalAlloc !== null && info.totalAccelCap !== null && info.totalAccelCap > 0) {
            return `${formatCount(info.totalAlloc)} / ${formatCount(info.totalAccelCap)}`;
        }
        if (info.totalAlloc !== null) {
            return `${formatCount(info.totalAlloc)} allocated`;
        }
        if (info.totalAccelCap !== null && info.totalAccelCap > 0) {
            return `${formatCount(info.totalAccelCap)} capacity`;
        }
        return 'Not reported';
    }, [info.totalAlloc, info.totalAccelCap]);

    const nodeCapacityText = useMemo(() => {
        if (info.totalAccelCap !== null && info.totalAccelAllocatable !== null) {
            return `${formatCount(info.totalAccelCap)} total · ${formatCount(info.totalAccelAllocatable)} allocatable`;
        }
        if (info.totalAccelCap !== null) {
            return `${formatCount(info.totalAccelCap)} total`;
        }
        return '—';
    }, [info.totalAccelCap, info.totalAccelAllocatable]);

    const nodesPodsValue = (info.nodeCount > 0 || info.podCount > 0)
        ? `${formatCount(info.nodeCount)} ${info.nodeCount === 1 ? 'node' : 'nodes'} · ${formatCount(info.podCount)} ${info.podCount === 1 ? 'pod' : 'pods'}`
        : 'Not reported';

    const hostComputeValue = info.totalCpuCapCores !== null
        ? formatCores(info.totalCpuCapCores)
        : info.totalHostMemBytes !== null
            ? (formatBytes(info.totalHostMemBytes) || 'Not reported')
            : 'Not reported';

    const fallbackMessage = !info.isDeployment
        ? 'This benchmark targeted an external inference endpoint without cluster node or pod visibility. Known target metadata and any recorded hardware label remain summarized above.'
        : (info.serverTelemetry?.status === 'unavailable' || info.serverTelemetry?.available === false)
            ? 'Server-side cluster telemetry was disabled or unavailable during this run. Enable cluster telemetry collection to capture node specifications and serving pod hardware allocations.'
            : 'No per-node or per-pod hardware inventory was recorded in this legacy run snapshot. New cluster runs automatically capture node and pod hardware specifications.';

    return (
        <div className="space-y-6">
            {/* Section 1: Hardware Overview (5 StatCards) */}
            <section className="space-y-3" aria-labelledby="hardware-overview-heading">
                <div className="flex items-center gap-1.5">
                    <SectionLabel className="mb-0" tone="cyan">
                        <span id="hardware-overview-heading">Hardware Overview</span>
                    </SectionLabel>
                    <HelpTooltip
                        label="Hardware Overview help"
                        align="start"
                        iconSize={12}
                        formula="Inventory = Target_Metadata ∪ Cluster_Node_Specs ∪ Serving_Pod_Allocations"
                    >
                        Summary of the target environment, accelerator hardware profile, participating cluster nodes and pods, and host compute capacity for this benchmark run.
                    </HelpTooltip>
                </div>

                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                    <StatCard
                        compact
                        icon={<Server className="h-4 w-4" />}
                        title="Target Environment"
                        help="Execution target mode, cluster/namespace or sanitized endpoint, model identifier, and serving topology captured for the run."
                        helpFormula="Target = Deployment(Cluster / Namespace, Topology) | External(URL)"
                        helpAlign="start"
                        value={info.targetHeadline}
                        details={[
                            {
                                label: 'Mode',
                                value: info.endpointModeLabel,
                                help: 'Whether benchmark traffic targeted a cluster-managed deployment or an external HTTP(S) endpoint.',
                                formula: 'Mode ∈ {Deployment, External Endpoint}',
                            },
                            {
                                label: 'Topology',
                                value: info.topologyLabel || '—',
                                help: 'Serving architecture (Aggregated workers vs. Prefill/Decode disaggregation).',
                                formula: 'Topology = Disaggregated iff Prefill_Pool ≠ ∅ else Aggregated',
                            },
                            {
                                label: 'Model',
                                value: info.modelName || '—',
                                help: 'Model identifier served by the target inference endpoint.',
                                formula: 'Model = Run_Manifest.target.model_name',
                            },
                            info.isDeployment
                                ? {
                                    label: 'Guide',
                                    value: info.guideType ? displayName(info.guideType) : (info.deploymentName || '—'),
                                    help: 'Deployment recipe or service name associated with the benchmarked model service.',
                                    formula: 'Guide = Server_Telemetry.guide_type ?? Deployment_Name',
                                }
                                : {
                                    label: 'Endpoint',
                                    value: info.endpointUrl || '—',
                                    help: 'Sanitized base URL targeted by the benchmark client.',
                                    formula: 'Endpoint = Sanitized_URL(target.endpoint_url)',
                                },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<Cpu className="h-4 w-4" />}
                        title="Accelerator Profile"
                        help="Profile-driven accelerator hardware model, vendor, and metric group detected on participating serving nodes or recorded on the task."
                        helpFormula="Profile = Accelerator_Summary ?? Profile_Models ?? Task_Hardware"
                        helpAlign="center"
                        value={info.acceleratorModel}
                        details={[
                            {
                                label: 'Profile',
                                value: info.profileName || '—',
                                help: 'Matched hardware profile display name or identifier from the cluster hardware registry.',
                                formula: 'Profile_Name = Hardware_Profile.display_name ?? Hardware_Profile.id',
                            },
                            {
                                label: 'Vendor',
                                value: info.profileVendor || '—',
                                help: 'Hardware vendor reported by the matched cluster hardware profile.',
                                formula: 'Vendor = Hardware_Profile.vendor',
                            },
                            {
                                label: 'Metric Group',
                                value: info.accelLabel,
                                help: 'Profile-defined accelerator group label used across telemetry charts and tables.',
                                formula: 'Metric_Group = Hardware_Profile.ui.labels.metric_group ?? "Accelerator"',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<Layers className="h-4 w-4" />}
                        title="Serving Nodes & Pods"
                        help="Count of distinct Kubernetes nodes hosting serving pods and total participating pods broken down by serving role."
                        helpFormula="Nodes = |per_node|;  Pods = |{p ∈ per_pod : p.role ≠ runner}|"
                        helpAlign="center"
                        value={nodesPodsValue}
                        details={[
                            {
                                label: 'Role Breakdown',
                                value: info.roleBreakdownLabel || '—',
                                help: 'Pod counts partitioned across Prefill, Decode, Worker, and Router roles.',
                                formula: 'Count(role) = |{p ∈ per_pod : effective_role(p) = role}|',
                            },
                            {
                                label: 'Serving Pods',
                                value: info.perPodRows.length > 0
                                    ? formatCount(info.servingPodCount)
                                    : (info.podCount > 0 ? formatCount(info.podCount) : '—'),
                                help: 'Total model-serving pods excluding gateway routers and benchmark client jobs.',
                                formula: 'Serving_Pods = |{p ∈ per_pod : p.role ∉ {epp, router, runner}}|',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<MemoryStick className="h-4 w-4" />}
                        title="Allocated Accelerators & Device Memory"
                        help="Total accelerator devices allocated to serving pods versus aggregate node device capacity and total reported device memory."
                        helpFormula="Allocated = Σ_nodes Allocated_Devices;  Total_Mem = Σ_nodes Device_Memory_Bytes"
                        helpAlign="center"
                        value={allocatedCardValue}
                        details={[
                            {
                                label: 'Device Memory',
                                value: formatDeviceMemorySummary(info.totalDeviceMemBytes, info.uniformDeviceMemCount),
                                help: 'Total accelerator memory across participating serving devices (adaptively scaled across B to PiB).',
                                formula: 'Total_Device_Mem = Allocated_Devices × Per_Device_Mem  (B..PiB)',
                            },
                            {
                                label: 'Node Capacity',
                                value: nodeCapacityText,
                                help: 'Aggregate node-level accelerator capacity and allocatable device slots reported by Kubernetes.',
                                formula: 'Node_Cap = Σ_nodes status.capacity[accel_resource]',
                            },
                        ]}
                    />

                    <StatCard
                        compact
                        icon={<HardDrive className="h-4 w-4" />}
                        title="Host Compute & Memory"
                        help="Aggregate host CPU core capacity, allocatable CPU cores, host RAM capacity, and local filesystem size across participating nodes."
                        helpFormula="Host_CPU = Σ_nodes CPU_Capacity_Cores;  Host_RAM = Σ_nodes Memory_Bytes"
                        helpAlign="end"
                        value={hostComputeValue}
                        details={[
                            {
                                label: 'Allocatable CPU',
                                value: info.totalCpuAllocCores !== null ? formatCores(info.totalCpuAllocCores) : '—',
                                help: 'Sum of Kubernetes allocatable host CPU cores available to workloads across participating nodes.',
                                formula: 'Allocatable_CPU = Σ_nodes status.allocatable.cpu',
                            },
                            {
                                label: 'Host Memory',
                                value: info.totalHostMemBytes !== null ? (formatBytes(info.totalHostMemBytes) || '—') : '—',
                                help: 'Total host system RAM across participating nodes (adaptively scaled across B to PiB).',
                                formula: 'Host_Mem = Σ_nodes (memory_capacity_bytes ?? memory_total_bytes)',
                            },
                            {
                                label: 'Storage',
                                value: info.totalStorageBytes !== null ? (formatBytes(info.totalStorageBytes) || '—') : '—',
                                help: 'Total local filesystem capacity observed on participating nodes (adaptively scaled across B to PiB).',
                                formula: 'Storage = Σ_nodes filesystem_size_bytes',
                            },
                        ]}
                    />
                </div>
            </section>

            {/* Section 2: Node Hardware Specifications */}
            {info.perNodeRows.length > 0 && (
                <Panel
                    title="Node Hardware Specifications"
                    help="Per-node hardware inventory, accelerator allocation, CPU and host memory capacity, platform metadata, and active network interfaces. Click a node name to inspect its time-series telemetry on the System tab."
                    helpFormula="Node_Spec(n) = {Accelerator, Device_Mem, CPU_Cores, Platform, Host_RAM, Storage, NICs}"
                >
                    <div className="overflow-x-auto lg:overflow-visible">
                        <table className="w-full text-left text-xs">
                            <thead className="border-b border-theme-border text-[11px] uppercase text-theme-muted">
                                <tr>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Node</span>
                                            <HelpTooltip
                                                label="Node column help"
                                                align="start"
                                                iconSize={11}
                                                formula="Click node → SystemTab(initialNode = node)"
                                            >
                                                Kubernetes worker node hosting one or more serving pods. Click a node name to jump to the System tab filtered to that node.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Roles &amp; Pods</span>
                                            <HelpTooltip
                                                label="Roles and Pods column help"
                                                align="start"
                                                iconSize={11}
                                                formula="Roles = distinct(role(p) : p ∈ pods(node))"
                                            >
                                                Serving roles (Prefill, Decode, Worker) and pod count scheduled on this node.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>{info.accelLabel}</span>
                                            <HelpTooltip
                                                label={`${info.accelLabel} column help`}
                                                align="center"
                                                iconSize={11}
                                                formula="Allocation = Allocated_Devices / Node_Capacity"
                                            >
                                                Node-isolated accelerator model name and allocated versus total node device capacity.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Device Memory</span>
                                            <HelpTooltip
                                                label="Device Memory column help"
                                                align="center"
                                                iconSize={11}
                                                formula="Per_Device = Total_Node_Device_Mem / Allocated_Devices"
                                            >
                                                Total reported device memory on this node and derived per-device capacity (adaptively scaled across B to PiB).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Host CPU &amp; Platform</span>
                                            <HelpTooltip
                                                label="Host CPU and Platform column help"
                                                align="center"
                                                iconSize={11}
                                                formula="CPU = Capacity / Allocatable_Cores;  Platform = Arch · OS · Kernel · Runtime"
                                            >
                                                Node CPU capacity and allocatable cores along with CPU architecture, OS image, kernel, kubelet, and container runtime versions.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Host Memory &amp; Storage</span>
                                            <HelpTooltip
                                                label="Host Memory and Storage column help"
                                                align="end"
                                                iconSize={11}
                                                formula="RAM = memory_capacity_bytes ?? memory_total_bytes;  Storage = filesystem_size_bytes"
                                            >
                                                Host system RAM and local filesystem capacity on this node (adaptively scaled across B to PiB).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Active NICs</span>
                                            <HelpTooltip
                                                label="Active NICs column help"
                                                align="end"
                                                iconSize={11}
                                                formula="NICs = {iface.device ∈ per_node.network_interfaces}"
                                            >
                                                Physical or high-speed network interfaces observed on this node during telemetry collection.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-theme-border">
                                {info.perNodeRows.map((row, idx) => {
                                    const nodeName = nonEmptyText(row?.node) || '—';
                                    const roles = Array.isArray(row?.roles) ? row.roles : EMPTY_ARR;
                                    const hasPodsArray = Array.isArray(row?.pods);
                                    const pods = hasPodsArray ? row.pods : EMPTY_ARR;
                                    const podCount = firstFinite(row?.pod_count, hasPodsArray ? pods.length : null);

                                    const nodeModel = nonEmptyText(row?.accelerator_model)
                                        || (info.perNodeRows.length === 1 && info.acceleratorModel !== 'Not reported'
                                            ? nonEmptyText(info.acceleratorModel)
                                            : null);
                                    const nodeAlloc = firstFinite(row?.allocated_accelerators);
                                    const nodeCap = firstFinite(row?.accelerator_capacity);
                                    const nodeAllocatable = firstFinite(row?.accelerator_allocatable);

                                    const nodeDeviceMemBytes = firstFinite(row?.gpu_memory_total_bytes);
                                    const perDeviceBytes = (nodeDeviceMemBytes !== null && nodeDeviceMemBytes > 0 && nodeAlloc !== null && nodeAlloc > 0)
                                        ? nodeDeviceMemBytes / nodeAlloc
                                        : null;

                                    const cpuCap = firstFinite(row?.cpu_capacity_cores);
                                    const cpuAlloc = firstFinite(row?.cpu_allocatable_cores);
                                    const platformPrimary = [nonEmptyText(row?.architecture), nonEmptyText(row?.os_image)]
                                        .filter(Boolean)
                                        .join(' · ');
                                    const platformSecondary = [
                                        nonEmptyText(row?.kernel_version) ? `kernel ${row.kernel_version}` : null,
                                        nonEmptyText(row?.kubelet_version),
                                        nonEmptyText(row?.container_runtime),
                                    ]
                                        .filter(Boolean)
                                        .join(' · ');

                                    const hostMemBytes = firstFinite(row?.memory_capacity_bytes, row?.memory_total_bytes);
                                    const fsSizeBytes = firstFinite(row?.filesystem_size_bytes);
                                    const fsAvailBytes = firstFinite(row?.filesystem_avail_bytes);

                                    const nics = extractNicNames(row?.network_interfaces);
                                    const visibleNics = nics.slice(0, MAX_VISIBLE_NICS);
                                    const overflowNics = nics.length - visibleNics.length;

                                    let allocLabel = '—';
                                    if (nodeAlloc !== null && nodeCap !== null && nodeCap > 0) {
                                        allocLabel = `${formatCount(nodeAlloc)} / ${formatCount(nodeCap)}`;
                                    } else if (nodeAlloc !== null) {
                                        allocLabel = `${formatCount(nodeAlloc)} allocated`;
                                    } else if (nodeCap !== null && nodeCap > 0) {
                                        allocLabel = `${formatCount(nodeCap)} capacity`;
                                    }

                                    return (
                                        <tr
                                            key={nodeName !== '—' ? `${nodeName}-${idx}` : `node-${idx}`}
                                            className="text-theme-text transition-colors hover:bg-slate-50/80 dark:hover:bg-slate-800/40"
                                        >
                                            <td className="px-3 py-2.5 align-top whitespace-nowrap">
                                                {nodeName !== '—' ? (
                                                    <Button
                                                        variant="link"
                                                        size="xs"
                                                        onClick={() => onSelectTab?.('system', null, { node: nodeName })}
                                                        title={`Inspect ${nodeName} metrics on System tab`}
                                                        className="h-auto px-0 py-0 font-mono text-xs font-semibold text-emerald-600 hover:text-emerald-500 dark:text-emerald-300 dark:hover:text-emerald-200"
                                                    >
                                                        {nodeName}
                                                    </Button>
                                                ) : (
                                                    <span className="font-mono text-theme-muted">—</span>
                                                )}
                                            </td>
                                            <td className="px-3 py-2.5 align-top">
                                                {roles.length === 0 && podCount === null ? (
                                                    <span className="font-mono text-theme-muted">—</span>
                                                ) : (
                                                    <>
                                                        <div className="flex flex-wrap items-center gap-1">
                                                            {roles.map((role) => (
                                                                <Badge key={role} tone={roleBadgeTone(role)} size="xs">
                                                                    {roleDisplayLabel(role)}
                                                                </Badge>
                                                            ))}
                                                            {podCount !== null && (
                                                                <span className="text-[11px] text-theme-muted" title={pods.join(', ')}>
                                                                    {formatCount(podCount)} {podCount === 1 ? 'pod' : 'pods'}
                                                                </span>
                                                            )}
                                                        </div>
                                                        {pods.length > 0 && (
                                                            <div className="mt-0.5 max-w-[14rem] truncate font-mono text-[10px] text-theme-muted" title={pods.join(', ')}>
                                                                {pods.join(', ')}
                                                            </div>
                                                        )}
                                                    </>
                                                )}
                                            </td>
                                            <td className="px-3 py-2.5 align-top">
                                                <div className="font-medium text-theme-text">
                                                    {nodeModel || '—'}
                                                </div>
                                                <div className="mt-0.5 font-mono text-[11px] text-theme-muted">
                                                    {allocLabel}
                                                    {nodeAllocatable !== null ? ` · ${formatCount(nodeAllocatable)} allocatable` : ''}
                                                </div>
                                            </td>
                                            <td className="px-3 py-2.5 align-top text-right font-mono whitespace-nowrap">
                                                <div>{nodeDeviceMemBytes !== null ? (formatBytes(nodeDeviceMemBytes) || '—') : '—'}</div>
                                                {perDeviceBytes !== null && (
                                                    <div className="mt-0.5 text-[10px] text-theme-muted">
                                                        {formatCount(nodeAlloc)} × {formatBytes(perDeviceBytes)}
                                                    </div>
                                                )}
                                            </td>
                                            <td className="px-3 py-2.5 align-top">
                                                <div className="font-mono text-theme-text">
                                                    {cpuCap !== null
                                                        ? (cpuAlloc !== null
                                                            ? `${formatCores(cpuCap)} (${formatDecimal(cpuAlloc)} alloc)`
                                                            : formatCores(cpuCap))
                                                        : (cpuAlloc !== null
                                                            ? `${formatCores(cpuAlloc)} allocatable`
                                                            : '—')}
                                                </div>
                                                {platformPrimary && (
                                                    <div className="mt-0.5 text-[11px] text-theme-muted">{platformPrimary}</div>
                                                )}
                                                {platformSecondary && (
                                                    <div className="mt-0.5 font-mono text-[10px] text-theme-muted">{platformSecondary}</div>
                                                )}
                                            </td>
                                            <td className="px-3 py-2.5 align-top text-right font-mono whitespace-nowrap">
                                                <div>{hostMemBytes !== null ? (formatBytes(hostMemBytes) || '—') : '—'}</div>
                                                {fsSizeBytes !== null ? (
                                                    <div className="mt-0.5 text-[10px] text-theme-muted">
                                                        Disk: {formatBytes(fsSizeBytes)}
                                                    </div>
                                                ) : fsAvailBytes !== null ? (
                                                    <div className="mt-0.5 text-[10px] text-theme-muted">
                                                        Avail: {formatBytes(fsAvailBytes)}
                                                    </div>
                                                ) : null}
                                            </td>
                                            <td className="px-3 py-2.5 align-top">
                                                {visibleNics.length > 0 ? (
                                                    <div className="flex flex-wrap items-center gap-1" title={nics.join(', ')}>
                                                        {visibleNics.map((nic) => (
                                                            <Badge key={nic} tone="neutral" size="xs">
                                                                {nic}
                                                            </Badge>
                                                        ))}
                                                        {overflowNics > 0 && (
                                                            <Badge tone="neutral" size="xs">
                                                                +{formatCount(overflowNics)}
                                                            </Badge>
                                                        )}
                                                    </div>
                                                ) : (
                                                    <span className="font-mono text-theme-muted">—</span>
                                                )}
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                </Panel>
            )}

            {/* Section 3: Serving Pod Hardware Allocation */}
            {info.perPodRows.length > 0 && (
                <Panel
                    title="Serving Pod Hardware Allocation"
                    help="Kubernetes resource requests, limits, and accelerator allocations per participating serving pod. Click a scheduled node name to jump to its System metrics."
                    helpFormula="Pod_Alloc(p) = {Node, Role, Allocated_Devices, CPU_Req/Lim, Mem_Req/Lim, Device_Mem}"
                >
                    <div className="overflow-x-auto lg:overflow-visible">
                        <table className="w-full text-left text-xs">
                            <thead className="border-b border-theme-border text-[11px] uppercase text-theme-muted">
                                <tr>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Pod</span>
                                            <HelpTooltip
                                                label="Pod column help"
                                                align="start"
                                                iconSize={11}
                                                formula="Pod = per_pod[i].pod"
                                            >
                                                Kubernetes pod name participating in model serving or request routing during the run.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Node</span>
                                            <HelpTooltip
                                                label="Pod Node column help"
                                                align="start"
                                                iconSize={11}
                                                formula="Click node → SystemTab(initialNode = pod.node)"
                                            >
                                                Scheduled Kubernetes node for this pod. Click to open the System tab scoped to that node.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Role</span>
                                            <HelpTooltip
                                                label="Pod Role column help"
                                                align="center"
                                                iconSize={11}
                                                formula="Role ∈ {Prefill, Decode, Worker, Router}"
                                            >
                                                Serving role assigned to this pod within the deployment topology.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Allocated {info.accelLabel}</span>
                                            <HelpTooltip
                                                label="Pod Allocated Accelerators column help"
                                                align="center"
                                                iconSize={11}
                                                formula="Allocated_Devices = Σ_containers max(limits[res], requests[res])"
                                            >
                                                Number of accelerator devices allocated to this pod from device allocations or container resource declarations.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>CPU (Req / Limit)</span>
                                            <HelpTooltip
                                                label="Pod CPU Requests and Limits column help"
                                                align="center"
                                                iconSize={11}
                                                formula="CPU_Req_or_Lim = Σ_containers spec.containers[].resources.{requests,limits}.cpu"
                                            >
                                                Aggregated container CPU requests and limits (cores) declared on the pod spec.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Memory (Req / Limit)</span>
                                            <HelpTooltip
                                                label="Pod Memory Requests and Limits column help"
                                                align="end"
                                                iconSize={11}
                                                formula="Mem_Req_or_Lim = Σ_containers spec.containers[].resources.{requests,limits}.memory"
                                            >
                                                Aggregated container host memory requests and limits declared on the pod spec (adaptively scaled across B to PiB).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Device Memory</span>
                                            <HelpTooltip
                                                label="Pod Device Memory column help"
                                                align="end"
                                                iconSize={11}
                                                formula="Device_Mem = mean_t (Pod_Device_Memory_Total_Bytes(t))"
                                            >
                                                Observed total accelerator memory capacity on devices assigned to this pod (adaptively scaled across B to PiB).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-theme-border">
                                {info.perPodRows.map((pod, idx) => {
                                    const podName = nonEmptyText(pod?.pod || pod?.name) || `pod-${idx}`;
                                    const podNode = nonEmptyText(pod?.node);
                                    const role = effectivePodRole(pod);
                                    const podAlloc = firstFinite(pod?.allocated_accelerators);
                                    const cpuReq = firstFinite(pod?.cpu_request_cores);
                                    const cpuLim = firstFinite(pod?.cpu_limit_cores);
                                    const memReq = firstFinite(pod?.memory_request_bytes);
                                    const memLim = firstFinite(pod?.memory_limit_bytes);
                                    const podDevMem = firstFinite(
                                        pod?.gpu_memory_total_bytes?.mean,
                                        pod?.gpu_memory_total_bytes?.max,
                                        pod?.gpu_memory_total_bytes,
                                    );

                                    const cpuText = (cpuReq !== null || cpuLim !== null)
                                        ? `${cpuReq !== null ? formatDecimal(cpuReq) : '—'} / ${cpuLim !== null ? formatDecimal(cpuLim) : '—'} cores`
                                        : '—';

                                    return (
                                        <tr
                                            key={`${podName}-${idx}`}
                                            className="text-theme-text transition-colors hover:bg-slate-50/80 dark:hover:bg-slate-800/40"
                                        >
                                            <td className="px-3 py-2.5 font-mono font-semibold whitespace-nowrap">
                                                {podName}
                                            </td>
                                            <td className="px-3 py-2.5 whitespace-nowrap">
                                                {podNode ? (
                                                    <Button
                                                        variant="link"
                                                        size="xs"
                                                        onClick={() => onSelectTab?.('system', null, { node: podNode })}
                                                        title={`Inspect ${podNode} metrics on System tab`}
                                                        className="h-auto px-0 py-0 font-mono text-xs font-medium text-emerald-600 hover:text-emerald-500 dark:text-emerald-300 dark:hover:text-emerald-200"
                                                    >
                                                        {podNode}
                                                    </Button>
                                                ) : (
                                                    <span className="font-mono text-theme-muted">—</span>
                                                )}
                                            </td>
                                            <td className="px-3 py-2.5 whitespace-nowrap">
                                                <Badge tone={roleBadgeTone(role)} size="xs">
                                                    {roleDisplayLabel(role)}
                                                </Badge>
                                            </td>
                                            <td className="px-3 py-2.5 text-right font-mono whitespace-nowrap">
                                                {podAlloc !== null ? formatCount(podAlloc) : '—'}
                                            </td>
                                            <td className="px-3 py-2.5 text-right font-mono whitespace-nowrap">
                                                {cpuText}
                                            </td>
                                            <td className="px-3 py-2.5 text-right font-mono whitespace-nowrap">
                                                {formatBytePair(memReq, memLim) ?? '—'}
                                            </td>
                                            <td className="px-3 py-2.5 text-right font-mono whitespace-nowrap">
                                                {podDevMem !== null ? (formatBytes(podDevMem) || '—') : '—'}
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                </Panel>
            )}

            {/* Section 4: KV Cache & Memory Hierarchy (only when cache_config is present) */}
            {info.cacheHierarchy && (
                <section className="space-y-3" aria-labelledby="hardware-cache-heading">
                    <div className="flex items-center gap-1.5">
                        <SectionLabel className="mb-0" tone="violet">
                            <span id="hardware-cache-heading">KV Cache &amp; Memory Hierarchy</span>
                        </SectionLabel>
                        <HelpTooltip
                            label="KV Cache and Memory Hierarchy help"
                            align="start"
                            iconSize={12}
                            formula="Effective_KV_Tokens = HBM_Tokens + Host_CPU_Tokens + Local_Storage_Tier"
                        >
                            Multi-tier KV cache capacity across device HBM, host CPU memory offload, and local storage tiers reported by the serving engine configuration.
                        </HelpTooltip>
                    </div>

                    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                        <StatCard
                            compact
                            icon={<Database className="h-4 w-4" />}
                            title="Device HBM KV Capacity"
                            help="Device high-bandwidth memory allocated to the KV cache across participating serving engines."
                            helpFormula="HBM_Capacity = Σ_engines HBM_KV_Bytes  |  Σ_engines HBM_KV_Tokens"
                            helpAlign="start"
                            value={
                                info.cacheHierarchy.hbmBytes !== null
                                    ? (formatBytes(info.cacheHierarchy.hbmBytes) || 'Not reported')
                                    : info.cacheHierarchy.hbmTokens !== null
                                        ? `${formatCount(info.cacheHierarchy.hbmTokens)} tokens`
                                        : 'Not reported'
                            }
                            details={[
                                {
                                    label: 'Token Capacity',
                                    value: info.cacheHierarchy.hbmTokens !== null
                                        ? `${formatCount(info.cacheHierarchy.hbmTokens)} tokens`
                                        : '—',
                                    help: 'Total KV token capacity resident in accelerator device memory.',
                                    formula: 'HBM_Tokens = cache_config.hbm_capacity_tokens',
                                },
                                ...(info.cacheHierarchy.prefillHbmTokens !== null ? [{
                                    label: 'Prefill HBM',
                                    value: `${formatCount(info.cacheHierarchy.prefillHbmTokens)} tokens`,
                                    help: 'Device HBM KV token capacity allocated to Prefill pool engines.',
                                    formula: 'Prefill_HBM_Tokens = Σ_{e ∈ prefill} kv_cache_size_tokens(e)',
                                }] : []),
                                ...(info.cacheHierarchy.decodeHbmTokens !== null ? [{
                                    label: 'Decode HBM',
                                    value: `${formatCount(info.cacheHierarchy.decodeHbmTokens)} tokens`,
                                    help: 'Device HBM KV token capacity allocated to Decode pool engines.',
                                    formula: 'Decode_HBM_Tokens = Σ_{e ∈ decode} kv_cache_size_tokens(e)',
                                }] : []),
                                ...(info.cacheHierarchy.numDeviceBlocks !== null ? [{
                                    label: 'KV Blocks',
                                    value: formatCount(info.cacheHierarchy.numDeviceBlocks),
                                    help: 'Total device KV cache blocks provisioned by the serving engine.',
                                    formula: 'KV_Blocks = floor(HBM_KV_Bytes / (Block_Size × Bytes_Per_Token))',
                                }] : []),
                            ]}
                        />

                        <StatCard
                            compact
                            icon={<MemoryStick className="h-4 w-4" />}
                            title="Host CPU KV Offload Capacity"
                            help="Host system RAM provisioned as a secondary KV cache offload tier when configured on serving engines."
                            helpFormula="Host_CPU_KV = cache_config.cpu_capacity_bytes  |  cpu_capacity_tokens"
                            helpAlign="center"
                            value={
                                info.cacheHierarchy.cpuBytes !== null
                                    ? (formatBytes(info.cacheHierarchy.cpuBytes) || 'Not configured')
                                    : info.cacheHierarchy.cpuTokens !== null
                                        ? `${formatCount(info.cacheHierarchy.cpuTokens)} tokens`
                                        : info.cacheHierarchy.hostKvConfigured
                                            ? 'Configured'
                                            : 'Not configured'
                            }
                            details={[
                                {
                                    label: 'Offload Status',
                                    value: info.cacheHierarchy.hostKvConfigured ? 'Configured' : 'Not configured',
                                    help: 'Indicates whether host CPU memory KV offloading is enabled in serving configuration.',
                                    formula: 'Host_Offload = host_kv_configured ∨ (cpu_capacity > 0)',
                                },
                                {
                                    label: 'Token Capacity',
                                    value: info.cacheHierarchy.cpuTokens !== null
                                        ? `${formatCount(info.cacheHierarchy.cpuTokens)} tokens`
                                        : '—',
                                    help: 'Estimated KV token slots available in host CPU offload memory.',
                                    formula: 'CPU_Tokens = cache_config.cpu_capacity_tokens',
                                },
                            ]}
                        />

                        <StatCard
                            compact
                            icon={<HardDrive className="h-4 w-4" />}
                            title="Local Storage KV Capacity"
                            help="Local NVMe or disk capacity provisioned for tertiary KV cache persistence and prefix reuse."
                            helpFormula="Storage_KV = nvme_capacity_bytes ?? disk_capacity_bytes"
                            helpAlign="center"
                            value={
                                info.cacheHierarchy.nvmeBytes !== null
                                    ? (formatBytes(info.cacheHierarchy.nvmeBytes) || 'Not configured')
                                    : info.cacheHierarchy.nvmeKvConfigured
                                        ? 'Configured'
                                        : 'Not configured'
                            }
                            details={[
                                {
                                    label: 'Storage Tier',
                                    value: info.cacheHierarchy.nvmeKvConfigured ? 'Configured' : 'Not configured',
                                    help: 'Indicates whether local NVMe or disk KV cache storage is configured.',
                                    formula: 'Storage_Tier = nvme_kv_configured ∨ (nvme_or_disk_bytes > 0)',
                                },
                            ]}
                        />

                        <StatCard
                            compact
                            icon={<Layers className="h-4 w-4" />}
                            title="Effective KV Cache Capacity"
                            help="Combined KV cache token capacity across active memory tiers, together with block size and reporting engine count."
                            helpFormula="Effective_Tokens = cache_config.effective_capacity_tokens ?? HBM_Tokens"
                            helpAlign="end"
                            value={
                                info.cacheHierarchy.effectiveTokens !== null
                                    ? `${formatCount(info.cacheHierarchy.effectiveTokens)} tokens`
                                    : 'Not reported'
                            }
                            details={[
                                {
                                    label: 'Block Size',
                                    value: info.cacheHierarchy.blockSize !== null
                                        ? `${formatCount(info.cacheHierarchy.blockSize)} tokens`
                                        : '—',
                                    help: 'Number of tokens stored per paged KV cache block.',
                                    formula: 'Block_Size = cache_config.block_size',
                                },
                                {
                                    label: 'Engines',
                                    value: info.cacheHierarchy.engineCount > 0
                                        ? formatCount(info.cacheHierarchy.engineCount)
                                        : '—',
                                    help: 'Number of serving engine instances reporting KV cache configuration.',
                                    formula: 'Engines = |cache_config.engines|',
                                },
                                ...(info.cacheHierarchy.maxKvConcurrency !== null ? [{
                                    label: 'Max KV Concurrency',
                                    value: `${formatDecimal(info.cacheHierarchy.maxKvConcurrency)}×`,
                                    help: 'Theoretical maximum concurrent sequence capacity relative to max model length.',
                                    formula: 'Max_KV_Concurrency = Effective_Tokens / Max_Model_Length',
                                }] : []),
                            ]}
                        />
                    </div>
                </section>
            )}

            {/* Graceful Fallback for External Endpoints / Legacy Runs */}
            {!hasInventory && (
                <Panel>
                    <EmptyState
                        icon={<Server className="h-7 w-7" />}
                        title="Node & pod hardware inventory unavailable"
                        message={fallbackMessage}
                    />
                </Panel>
            )}
        </div>
    );
}

export default HardwareTab;
