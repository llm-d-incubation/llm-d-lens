const VIEW_ALIASES = {
  home: 'clusters',
  dashboard: 'welcome',
  'dashboard/alerts': 'welcome/issues',
  simulation: 'optimization-simulate',
  'opt-simulation': 'optimization-simulate',
  'optimization-cluster': 'clusters',
  'optimization-explore': 'optimization-evaluate-new',
};

const SUPPORTED_VIEWS = new Set(['welcome', 'welcome/issues', 'cluster-monitoring-stack', 'clusters', 'storage-management', 'model-cache', 'ai-providers', 'model-service', 'api-keys', 'usage', 'playground', 'public-rag', 'optimization-deployments', 'model-market', 'optimization-evaluate', 'optimization-evaluate-new', 'optimization-evaluation-details', 'optimization-simulate', 'optimization-simulate-details', 'admin/users', 'admin/groups', 'admin/roles', 'admin/identity-providers', 'admin/master-key', 'admin/sessions', 'admin/audit', 'admin/model-service', 'admin/usage']);

export function resolveView(view, fallback = 'model-market') {
  const resolved = VIEW_ALIASES[view] || view;
  return SUPPORTED_VIEWS.has(resolved) ? resolved : fallback;
}
export const NAVIGATION_GROUPS = [
  {
    id: 'overview',
    items: [
      { id: 'welcome', label: 'Welcome', icon: 'Sparkles', view: 'welcome', iconColor: 'text-[#037ffc] dark:text-white' },
    ],
  },
  {
    id: 'inference',
    title: 'Inference',
    items: [
      { id: 'optimization-deployments', label: 'Deployments', icon: 'Rocket', view: 'optimization-deployments', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'admin-model-service', label: 'Model Services', icon: 'Boxes', view: 'admin/model-service', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'model-market', label: 'Model Catalog', icon: 'Database', view: 'model-market', iconColor: 'text-[#037ffc] dark:text-white' },
    ],
  },
  {
    id: 'performance',
    title: 'Performance',
    items: [
      { id: 'optimization-evaluate', label: 'Evaluation', icon: 'Gauge', view: 'optimization-evaluate', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'optimization-simulate', label: 'Trace Replayer', icon: 'SimulationIcon', view: 'optimization-simulate', iconColor: 'text-[#037ffc] dark:text-white' },
    ],
  },
  {
    id: 'infrastructure',
    title: 'Infrastructure',
    items: [
      { id: 'clusters', label: 'Clusters', icon: 'Network', view: 'clusters', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'cluster-monitoring-stack', label: 'Observability', icon: 'Webcam', view: 'cluster-monitoring-stack', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'storage', label: 'Storage', icon: 'HardDrive', view: 'storage-management', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'model-cache', label: 'Model Cache', icon: 'Download', view: 'model-cache', iconColor: 'text-[#037ffc] dark:text-white' },
    ],
  },
  {
    id: 'knowledge',
    title: 'Knowledge',
    items: [
      { id: 'public-rag', label: 'Document Search', icon: 'Database', view: 'public-rag', iconColor: 'text-theme-text' },
    ],
  },
  {
    id: 'my-resources',
    title: 'My Resources',
    items: [
      { id: 'model-service', label: 'Services', icon: 'Waypoints', view: 'model-service', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'api-keys', label: 'API Keys', icon: 'KeyRound', view: 'api-keys', iconColor: 'text-[#037ffc] dark:text-white' },
      { id: 'usage', label: 'Usage', icon: 'Activity', view: 'usage', iconColor: 'text-[#037ffc] dark:text-white' },
    ],
  },
  {
    id: 'administration',
    title: 'Administration',
    items: [
      { id: 'admin-usage', label: 'Model Usage', icon: 'Activity', view: 'admin/usage', iconColor: 'text-[#037ffc] dark:text-white', section: 'Platform Management' },
      { id: 'ai-providers', label: 'External Providers', icon: 'Plug', view: 'ai-providers', iconColor: 'text-[#037ffc] dark:text-white', section: 'Platform Management' },
      { id: 'admin-users', label: 'Users', icon: 'Users', view: 'admin/users', iconColor: 'text-[#037ffc] dark:text-white', section: 'Access Control' },
      { id: 'admin-groups', label: 'Groups', icon: 'UserCog', view: 'admin/groups', iconColor: 'text-[#037ffc] dark:text-white', section: 'Access Control' },
      { id: 'admin-roles', label: 'Roles', icon: 'ShieldCheck', view: 'admin/roles', iconColor: 'text-[#037ffc] dark:text-white', section: 'Access Control' },
      { id: 'admin-identity-providers', label: 'Identity Providers', icon: 'Fingerprint', view: 'admin/identity-providers', iconColor: 'text-[#037ffc] dark:text-white', badge: 'Exp', section: 'Access Control' },
      { id: 'admin-master-key', label: 'Master Key', icon: 'KeyRound', view: 'admin/master-key', iconColor: 'text-[#037ffc] dark:text-white', section: 'Security & Audit' },
      { id: 'admin-audit', label: 'Audit Log', icon: 'ScrollText', view: 'admin/audit', iconColor: 'text-[#037ffc] dark:text-white', section: 'Security & Audit' },
      { id: 'admin-sessions', label: 'Sessions', icon: 'Monitor', view: 'admin/sessions', iconColor: 'text-[#037ffc] dark:text-white', section: 'Security & Audit' },
    ],
  },
  {
    id: 'assistant',
    placement: 'bottom',
    items: [
      { id: 'playground-chat', label: 'Lens Assistant', icon: 'Bot', view: 'playground', iconColor: 'text-[#037ffc] dark:text-white', badge: 'Exp' },
    ],
  },
];

export function firstAccessibleView(canView) {
  return NAVIGATION_GROUPS.flatMap(group => group.items)
    .find(item => !item.disabled && canView(item.view))?.view || '';
}

export function viewTitle(view) {
  const item = NAVIGATION_GROUPS.flatMap(group => group.items).find(item => item.view === view);
  if (item) return item.label;
  return {
    'welcome/issues': 'Issues',
    'optimization-evaluate-new': 'New Benchmark Task',
    'optimization-evaluation-details': 'Benchmark details',
    'optimization-simulate-details': 'Simulation task details',
  }[view] || 'Lens';
}
