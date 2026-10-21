import { Activity, Boxes, Download, Gauge, HardDrive, Plus, Rocket, Waypoints } from 'lucide-react';

export const WELCOME_ACTIONS = [
    {
        id: 'token-usage-report',
        label: 'Token usage analytics',
        description: 'Review recorded token consumption.',
        icon: Activity,
        view: 'admin/usage',
        permission: 'usage:report:read',
    },
    {
        id: 'new-deployment',
        label: 'Add a model deployment',
        description: 'Prepare a model for serving.',
        icon: Rocket,
        view: 'model-market',
        permission: 'deployment:run:create',
    },
    {
        id: 'add-model-service',
        label: 'Publish a model service',
        description: 'Publish a deployment as a model service.',
        icon: Boxes,
        view: 'admin/model-service',
        permission: 'model-service:group:manage',
        params: { action: 'add' },
    },
    {
        id: 'launch-benchmark',
        label: 'Launch benchmark',
        description: 'Run end-to-end performance tests.',
        icon: Gauge,
        view: 'optimization-evaluate-new',
        permission: ['evaluate:workflow:create', 'evaluate:run:create'],
    },
    {
        id: 'replay-traces',
        label: 'Replay traces',
        description: 'Replay trace data.',
        icon: Waypoints,
        view: 'optimization-simulate',
        permission: 'simulation:task:create',
        params: { action: 'new' },
    },
    {
        id: 'manage-storages',
        label: 'Manage storages',
        description: 'Manage cluster storage volumes.',
        icon: HardDrive,
        view: 'storage-management',
        permission: 'storage:volume:read',
    },
    {
        id: 'cache-model',
        label: 'Cache a new model',
        description: 'Download model files to storage.',
        icon: Download,
        view: 'model-cache',
        permission: 'model-cache:entry:create',
        params: { action: 'download' },
    },
    {
        id: 'create-cluster',
        label: 'Create a new cluster',
        description: 'Register or bootstrap a cluster.',
        icon: Plus,
        view: 'clusters',
        permission: 'cluster:cluster:create',
        params: { action: 'create' },
    },
];

export function visibleWelcomeActions(can, canView) {
    return WELCOME_ACTIONS.filter((action) => {
        const permissions = Array.isArray(action.permission) ? action.permission : [action.permission];
        return canView(action.view) && permissions.some((permission) => can(permission));
    });
}
