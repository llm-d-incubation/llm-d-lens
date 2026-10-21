const numberFormat = new Intl.NumberFormat();

export const SEVERITY_ORDER = { critical: 0, warning: 1, info: 2 };

export const ACTION_LABELS = {
    open_cluster: 'To check',
    inspect_providers: 'To check',
    manage_providers: 'To fix',
    inspect_bottleneck: 'To check',
    view_deployment: 'To check',
    open_storage: 'To resolve',
    create_storage: 'To fix',
    open_cache: 'To resolve',
    open_observability: 'To check',
    open_model_services: 'To check',
    create_cluster: 'To fix',
    edit_cluster: 'To resolve',
    open_external_providers: 'To fix',
    manage_user_roles: 'To fix',
    open_groups: 'To check',
    open_usage: 'To check',
};

export const CATEGORY_LABELS = {
    cluster: 'Cluster',
    'model-service': 'Model service',
    deployment: 'Deployment',
    storage: 'Storage',
    'model-cache': 'Model cache',
    monitoring: 'Monitoring',
    'ai-provider': 'AI provider',
    access: 'Access',
};

export const AVAILABILITY_LABELS = {
    available: 'Available',
    degraded: 'Degraded',
    unavailable: 'Unavailable',
    disabled: 'Disabled',
    unknown: 'Unknown',
};

const AVAILABILITY_TONES = {
    available: 'success',
    degraded: 'warning',
    unavailable: 'danger',
    disabled: 'neutral',
    unknown: 'neutral',
};

export function availabilityTone(value) {
    return AVAILABILITY_TONES[value] || 'neutral';
}

export function severityTone(value) {
    return value === 'critical' ? 'danger' : value === 'warning' ? 'warning' : 'info';
}

export function clusterStatusTone(value) {
    return value === 'critical' ? 'danger' : value === 'warning' ? 'warning' : value === 'healthy' ? 'success' : 'neutral';
}

export function pressureTone(value) {
    return value === 'elevated' ? 'warning' : value === 'normal' ? 'success' : 'neutral';
}

export function actionLabel(intent) {
    return ACTION_LABELS[intent] || 'To check';
}

export function formatNumber(value) {
    return numberFormat.format(Number(value) || 0);
}

export function formatPercent(value, digits = 0) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
    return `${value.toFixed(digits)}%`;
}

export function formatAge(value) {
    if (!value) return 'never';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

export function evidenceRows(issue, limit = 4) {
    const rows = [];
    const evidence = issue?.evidence || {};
    const labels = {
        waitingCurrent: 'Waiting now',
        waitingPeak: 'Waiting peak',
        waitingFraction: 'Waiting window',
        kvCacheUsagePercent: 'KV cache',
        kvCacheFraction: 'KV window',
        errorRate: 'Error rate',
        outcomes: 'Outcomes',
        affectedInstances: 'Affected instances',
        node: 'Node',
        nodesReady: 'Nodes ready',
        nodesTotal: 'Nodes total',
        diskUsagePercent: 'Disk usage',
        memoryUsagePercent: 'Memory usage',
        gpuTotal: 'Accelerators',
        gpuAllocated: 'Allocated',
        gpuAvailable: 'Unallocated',
        status: 'Status',
        reason: 'Reason',
        providers: 'Providers',
        unhealthyProviders: 'Unhealthy providers',
        notReadyProviders: 'Not ready providers',
        gateway: 'Gateway',
        component: 'Component',
        serviceCount: 'Services',
        pendingNodes: 'Pending nodes',
        failedNodes: 'Failed nodes',
    };
    const priority = ['component', 'status', 'unhealthyProviders', 'notReadyProviders', 'node', 'diskUsagePercent'];
    const entries = Object.entries(evidence).sort(([left], [right]) => {
        const leftIndex = priority.indexOf(left);
        const rightIndex = priority.indexOf(right);
        if (leftIndex === -1 && rightIndex === -1) return 0;
        return (leftIndex === -1 ? Number.MAX_SAFE_INTEGER : leftIndex)
            - (rightIndex === -1 ? Number.MAX_SAFE_INTEGER : rightIndex);
    });
    entries.forEach(([key, value]) => {
        if (rows.length >= limit || value === null || value === undefined || value === '') return;
        if (typeof value === 'object') return;
        let rendered = value;
        if (key.endsWith('Percent')) rendered = `${Number(value).toFixed(1)}%`;
        else if (key.endsWith('Fraction') && typeof value === 'number') rendered = `${Math.round(value * 100)}%`;
        else if (key === 'errorRate' && typeof value === 'number') rendered = `${(value * 100).toFixed(2)}%`;
        else if (typeof value === 'number') rendered = formatNumber(value);
        rows.push({ key, label: labels[key] || key, value: rendered });
    });
    return rows;
}

export function sortIssues(items = []) {
    return [...items].sort((a, b) => (
        (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9)
        || String(b.observedAt || '').localeCompare(String(a.observedAt || ''))
        || String(a.id).localeCompare(String(b.id))
    ));
}
