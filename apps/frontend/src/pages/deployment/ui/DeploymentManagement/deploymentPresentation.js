// Presentation mapping for deployment records returned by
// GET /api/v1/deployments/executions. Kept free of JSX and network code so the
// same rules can be reused by the table, the detail modal and the stat cards.

export function successfulPagePods(executionIds, podsById, errorsById) {
    return Object.fromEntries(executionIds.filter(id => id && !errorsById[id] && Object.hasOwn(podsById, id)).map(id => [id, podsById[id]]));
}

export const STATUS_GROUPS = {
    ready: 'Ready',
    succeeded: 'Ready',
    partially_succeeded: 'Ready',

    deploying: 'In progress',
    rendered: 'In progress',
    validated: 'In progress',
    draft: 'In progress',
    rolling_back: 'In progress',
    queued: 'In progress',
    rendering: 'In progress',
    running: 'In progress',
    cancelling: 'In progress',

    failed: 'Failed',

    cleaned: 'Cleaned',
    cleaned_up: 'Cleaned',
    rolled_back: 'Cleaned',
    stopped: 'Cleaned',
    cancelled: 'Cleaned',
};

const STATUS_TONES = {
    Ready: 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200',
    'In progress': 'border-sky-400/30 bg-sky-400/10 text-sky-200',
    Failed: 'border-rose-400/30 bg-rose-400/10 text-rose-200',
    Degraded: 'border-amber-400/30 bg-amber-400/10 text-amber-200',
    Cleaned: 'border-slate-400/30 bg-slate-400/10 text-slate-400',
    Unknown: 'border-slate-400/30 bg-slate-400/10 text-slate-400',
};

// Pulsing status-dot colors, matching the dot used on the Clusters page.
const STATUS_DOT_CLASSES = {
    Ready: 'bg-emerald-400',
    'In progress': 'bg-sky-400',
    Failed: 'bg-red-400',
    Degraded: 'bg-amber-400',
    Cleaned: 'bg-slate-500',
    Unknown: 'bg-slate-500',
};

// Statuses that still have an in-flight lifecycle transition.
const IN_PROGRESS_STATUSES = new Set(['deploying', 'rolling_back']);

export const STATUS_FILTER_OPTIONS = Object.keys(STATUS_GROUPS);

export function statusGroup(status) {
    return STATUS_GROUPS[status] || 'Unknown';
}

// A deployment is only serviceable -- scrapeable by monitoring, port-forwardable,
// profileable -- once its rollout reached the Ready group. Exported so the
// Clusters monitoring menus apply the same rule as the Deployments table.
export function isDeploymentReady(status) {
    return statusGroup(status) === 'Ready';
}

export function statusTone(status) {
    return STATUS_TONES[statusGroup(status)] || STATUS_TONES.Unknown;
}

export function statusLabel(status) {
    if (!status) return 'UNKNOWN';
    return String(status).replace(/_/g, ' ').toUpperCase();
}

export function statusDotClass(status) {
    return STATUS_DOT_CLASSES[statusGroup(status)] || STATUS_DOT_CLASSES.Unknown;
}

export function statusDotClassForGroup(group) {
    return STATUS_DOT_CLASSES[group] || STATUS_DOT_CLASSES.Unknown;
}

export function statusToneForGroup(group) {
    return STATUS_TONES[group] || STATUS_TONES.Unknown;
}

// Container/pod status reasons that indicate the pod is unhealthy regardless
// of restart count, since they don't happen as part of a normal startup
// retry loop (unlike CrashLoopBackOff/Error, which many init containers hit
// intentionally while polling for a dependency to become ready).
const POD_HARD_FAILURE_REASONS = new Set([
    'ImagePullBackOff',
    'ErrImagePull',
    'InvalidImageName',
    'CreateContainerConfigError',
    'CreateContainerError',
    'RunContainerError',
    'OOMKilled',
    'Evicted',
]);

// Reasons that are also failure signals, but only once they've actually
// looped a few times -- a fresh pod can legitimately show these for a bit
// while a slow-loading model or a wait-for-dependency init container retries.
const POD_RETRYABLE_FAILURE_REASONS = new Set(['CrashLoopBackOff', 'Error']);
const RETRYABLE_FAILURE_RESTART_THRESHOLD = 3;

function isFailingPod(pod) {
    if (!pod) return false;
    if (pod.phase === 'Failed') return true;
    if (POD_HARD_FAILURE_REASONS.has(pod.status_reason)) return true;
    if (POD_RETRYABLE_FAILURE_REASONS.has(pod.status_reason)) {
        return (pod.restarts || 0) >= RETRYABLE_FAILURE_RESTART_THRESHOLD;
    }
    return false;
}

// Derive a status group purely from live pod data, independent of the
// deployment's own (potentially stale) `status` field. Returns null when the
// pod list can't tell us anything conclusive (not yet fetched, or empty while
// the deployment is still being created).
function isModelServerPod(pod) {
    const labels = pod?.labels || {};
    return labels['llm-d.ai/inference-serving'] === 'true'
        || labels['llm-d.ai/role'] === 'decode'
        || labels['llm-d.ai/role'] === 'prefill';
}

function isGatewayPod(pod) {
    const labels = pod?.labels || {};
    return Boolean(labels['llm-d-router-gateway'])
        || /(^|-)epp(-|$)/.test(String(pod?.name || '').toLowerCase());
}

export function derivePodStatusGroup(pods, expectedModelPods = 1) {
    if (!Array.isArray(pods) || pods.length === 0) return null;
    // Completed pods (for example the precise-routing calibration Job) are
    // terminal successes, not part of the deployment's running workload, so they
    // must never read as "not ready".
    const livePods = pods.filter((pod) => pod && pod.phase !== 'Succeeded');
    if (livePods.length === 0) return null;
    if (livePods.some(isFailingPod)) return 'Failed';
    const modelPods = livePods.filter(isModelServerPod);
    const expected = Math.max(1, Number(expectedModelPods) || 1);
    // A gateway/EPP-only namespace means the model server is gone even though
    // the remaining pod is healthy.
    if (modelPods.length === 0 && livePods.every(isGatewayPod)) return 'Failed';
    const workload = modelPods.length ? modelPods : livePods;
    const ready = workload.filter((pod) => pod.ready && !pod.deleting).length;
    if (ready === 0) return 'Failed';
    if (ready < expected || ready < workload.length) return 'In progress';
    return 'Ready';
}

// Combine the deployment's own status with what its pods currently report.
// A missing model server is unavailable (red); a partially ready model server
// is degraded (amber); a pod-level failure wins over a stale Ready record.
// Terminal states (Cleaned) are never overridden by transient pod snapshots.
export function expectedModelPodCount(item) {
    const decode = Number(item?.scale?.decodeReplicas ?? item?.replicas);
    const prefill = Number(item?.scale?.prefillReplicas);
    const slices = Number(item?.scale?.slices) || 1;
    const total = (Number.isFinite(decode) ? Math.max(0, decode) : 0)
        + (Number.isFinite(prefill) ? Math.max(0, prefill) : 0);
    return Math.max(1, slices * total);
}

export function effectiveStatusGroup(item, pods) {
    const backendGroup = statusGroup(item?.status);
    if (backendGroup === 'Cleaned') return backendGroup;
    // A Ready record is not shown as Ready until live Pods confirm it.
    if (backendGroup === 'Ready' && (pods === undefined || item?.pending)) return 'In progress';
    if (Array.isArray(pods) && pods.length === 0 && backendGroup === 'Ready') return 'Failed';
    const podGroup = derivePodStatusGroup(pods, expectedModelPodCount(item));
    if (!podGroup) return backendGroup;
    if (backendGroup === 'In progress' && !pods.some(isFailingPod)) return backendGroup;
    if (podGroup === 'Failed') return 'Failed';
    if (podGroup === 'In progress' && backendGroup === 'Ready') return 'Degraded';
    return backendGroup;
}

export function effectiveStatusLabel(item, pods) {
    const backendGroup = statusGroup(item?.status);
    const effective = effectiveStatusGroup(item, pods);
    if (effective === backendGroup) return statusLabel(item?.status);
    if (effective === 'Failed') {
        const podError = Array.isArray(pods) && pods.some(isFailingPod);
        return `${statusLabel(item?.status)} · ${podError ? 'POD ERROR' : 'POD UNAVAILABLE'}`;
    }
    if (effective === 'Degraded') return `${statusLabel(item?.status)} · CAPACITY REDUCED`;
    if (effective === 'In progress' && backendGroup === 'Ready') return `${statusLabel(item?.status)} · ${pods === undefined ? 'CHECKING PODS' : 'STARTING'}`;
    return statusLabel(item?.status);
}

export function deploymentTitle(item) {
    return item?.display_name || item?.name || item?.model || item?.execution_id || 'Deployment';
}

export function deploymentKind(item) {
    const parts = [item?.backend, item?.guide].filter(Boolean);
    return parts.length ? parts.join(' · ') : '—';
}

export function deploymentEndpoint(item) {
    return item?.forwarded_endpoint || item?.endpoint || '';
}

export function deploymentLocalEndpoint(item) {
    return item?.forwarded_endpoint || '';
}

export { formatTimestamp } from '../../../../shared/lib/formatTimestamp';

export function shortId(executionId) {
    return String(executionId || '').slice(-8);
}

// Text the user must retype in the delete dialog. Display name when present so
// the confirmation matches what the row shows, otherwise the id suffix.
export function deleteConfirmationPhrase(item) {
    return item?.display_name?.trim() || shortId(item?.execution_id);
}

// The backend cancels an in-flight deployment and cleans up its cluster
// resources before deleting the record, so deletion is never blocked here.
export function deletionBlockedReason() {
    return '';
}

// True while the deployment is still deploying/rolling back, so the UI can
// warn the user that deleting it now will cancel the in-flight operation.
export function isDeletionInProgressStatus(item) {
    return IN_PROGRESS_STATUSES.has(item?.status);
}

export function summarizeStatuses(items, podsById = {}) {
    const summary = { total: items.length, Ready: 0, 'In progress': 0, Degraded: 0, Failed: 0, Cleaned: 0 };
    for (const item of items) {
        // Only the visible page is polled; other rows keep their backend status.
        const polled = Object.hasOwn(podsById, item?.execution_id ?? '');
        const group = polled || item?.pending ? effectiveStatusGroup(item, podsById[item?.execution_id]) : statusGroup(item?.status);
        if (group in summary) {
            summary[group] += 1;
        } else {
            summary.Cleaned += 1;
        }
    }
    return summary;
}

export function clusterOptions(items) {
    return [...new Set(items.map((item) => item?.cluster_id).filter(Boolean))].sort();
}

export const BENCHMARK_PRESETS = {
    chat: { label: 'Short chat', isl: 1024, osl: 256, concurrency: [1, 8, 32] },
    'long-input': { label: 'Long input, short output', isl: 8192, osl: 256, concurrency: [1, 8, 16] },
    'high-concurrency': { label: 'High concurrency', isl: 1024, osl: 256, concurrency: [32, 64, 128] },
};

/** A benchmark run against one ready execution; the server routes standalone or Gateway deployments itself. */
export function deploymentBenchmarkRequest(deployment, { preset, ttftMs, tpotMs }) {
    const { isl, osl, concurrency } = BENCHMARK_PRESETS[preset];
    const target = (value) => (Number(value) > 0 ? Number(value) : null);
    return {
        deployment_execution_id: deployment.execution_id,
        matrix: [{ isl, osl }],
        concurrency_stages: concurrency.map((value) => ({ concurrency: value, num_requests: Math.max(20, value * 5) })),
        warmup_requests: 2,
        sla_targets: { ttft_ms: target(ttftMs), ttft_percentile: 'p95', tpot_ms: target(tpotMs), tpot_percentile: 'p95' },
    };
}

export { errorMessage } from '../../../../shared/lib/errorMessage';
