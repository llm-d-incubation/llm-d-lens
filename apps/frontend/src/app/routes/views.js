const VIEW_ALIASES = {
  home: 'clusters',
  simulation: 'optimization-simulate',
  'opt-simulation': 'optimization-simulate',
  'optimization-cluster': 'clusters',
  'optimization-explore': 'optimization-evaluate-new',
};

const SUPPORTED_VIEWS = new Set(['cluster-monitoring-stack', 'clusters', 'storage-management', 'model-cache', 'ai-providers', 'model-service', 'api-keys', 'usage', 'playground', 'optimization-deployments', 'model-market', 'optimization-evaluate', 'optimization-evaluate-new', 'optimization-evaluation-details', 'optimization-deploy', 'optimization-workspace', 'optimization-simulate', 'optimization-simulate-details', 'opt-define', 'opt-search', 'opt-deploy', 'opt-benchmark', 'opt-performance', 'admin/users', 'admin/groups', 'admin/roles', 'admin/identity-providers', 'admin/master-key', 'admin/sessions', 'admin/audit', 'admin/model-service', 'admin/usage']);

export function resolveView(view, fallback = 'model-market') {
  const resolved = VIEW_ALIASES[view] || view;
  return SUPPORTED_VIEWS.has(resolved) ? resolved : fallback;
}
export const NAVIGATION_GROUPS = [
  {
    id: 'services',
    title: 'Services',
    items: [
      { id: 'model-market', label: 'Model market', icon: 'Database', view: 'model-market', iconColor: 'text-white' },
      { id: 'optimization-deployments', label: 'Deployments', icon: 'Rocket', view: 'optimization-deployments', iconColor: 'text-white' },
      { id: 'admin-model-service', label: 'Model services', icon: 'Boxes', view: 'admin/model-service', iconColor: 'text-white', badge: 'Exp' },
      { id: 'admin-usage', label: 'Usage reports', icon: 'Activity', view: 'admin/usage', iconColor: 'text-white' },
      { id: 'optimization-evaluate', label: 'Evaluation', icon: 'Gauge', view: 'optimization-evaluate', iconColor: 'text-white' },
      { id: 'optimization-simulate', label: 'Simulation', icon: 'SimulationIcon', view: 'optimization-simulate', iconColor: 'text-white' },
      { id: 'playground-chat', label: 'Lens Assistant', icon: 'Bot', view: 'playground', iconColor: 'text-white', badge: 'Exp' },
    ],
  },
  {
    id: 'user-services',
    title: 'User services',
    items: [
      { id: 'usage', label: 'My usage', icon: 'Activity', view: 'usage', iconColor: 'text-white' },
      { id: 'api-keys', label: 'API keys', icon: 'KeyRound', view: 'api-keys', iconColor: 'text-white' },
      { id: 'model-service', label: 'My services', icon: 'Waypoints', view: 'model-service', iconColor: 'text-white', badge: 'Exp' },
    ],
  },
  {
    id: 'resources',
    title: 'Resources',
    items: [
      { id: 'clusters', label: 'Clusters', icon: 'Network', view: 'clusters', iconColor: 'text-white' },
      { id: 'storage', label: 'Storage', icon: 'HardDrive', view: 'storage-management', iconColor: 'text-white' },
      { id: 'model-cache', label: 'Model cache', icon: 'Download', view: 'model-cache', iconColor: 'text-white' },
      { id: 'ai-providers', label: 'External providers', icon: 'Plug', view: 'ai-providers', iconColor: 'text-white' },
      { id: 'cluster-monitoring-stack', label: 'Observability', icon: 'Webcam', view: 'cluster-monitoring-stack', iconColor: 'text-white' },
    ],
  },
  {
    id: 'administration',
    title: 'Administration',
    items: [
      { id: 'admin-users', label: 'Users', icon: 'Users', view: 'admin/users', iconColor: 'text-white' },
      { id: 'admin-groups', label: 'Groups', icon: 'UserCog', view: 'admin/groups', iconColor: 'text-white' },
      { id: 'admin-roles', label: 'Roles', icon: 'ShieldCheck', view: 'admin/roles', iconColor: 'text-white' },
      { id: 'admin-identity-providers', label: 'Identity providers', icon: 'KeyRound', view: 'admin/identity-providers', iconColor: 'text-white', badge: 'Exp' },
      { id: 'admin-master-key', label: 'Master key', icon: 'KeyRound', view: 'admin/master-key', iconColor: 'text-white' },
      { id: 'admin-audit', label: 'Audit log', icon: 'ScrollText', view: 'admin/audit', iconColor: 'text-white' },
      { id: 'admin-sessions', label: 'Sessions', icon: 'Monitor', view: 'admin/sessions', iconColor: 'text-white' },
    ],
  },
];

export function firstAccessibleView(canView) {
  return NAVIGATION_GROUPS.flatMap(group => group.items)
    .find(item => !item.disabled && canView(item.view))?.view || '';
}
