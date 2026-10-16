const VIEW_ALIASES = {
  home: 'clusters',
  simulation: 'optimization-simulate',
  'opt-simulation': 'optimization-simulate',
  'optimization-cluster': 'clusters',
  'optimization-explore': 'optimization-evaluate-new',
};

const SUPPORTED_VIEWS = new Set(['cluster-monitoring-stack', 'clusters', 'storage-management', 'model-cache', 'ai-providers', 'model-service', 'api-keys', 'usage', 'playground', 'optimization-deployments', 'model-market', 'optimization-evaluate', 'optimization-evaluate-new', 'optimization-evaluation-details', 'optimization-deploy', 'optimization-workspace', 'optimization-simulate', 'optimization-simulate-details', 'opt-define', 'opt-search', 'opt-deploy', 'opt-benchmark', 'opt-performance', 'admin/users', 'admin/groups', 'admin/roles', 'admin/identity-providers', 'admin/master-key', 'admin/sessions', 'admin/audit', 'admin/model-service', 'admin/usage']);

export function resolveView(view) {
  const resolved = VIEW_ALIASES[view] || view;
  return SUPPORTED_VIEWS.has(resolved) ? resolved : 'model-market';
}