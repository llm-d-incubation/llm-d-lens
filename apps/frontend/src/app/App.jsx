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

import { lazy, Suspense, useState, useRef, useEffect } from 'react';
import ErrorBoundary from './providers/ErrorBoundary';
import { firstAccessibleView, resolveView } from './routes/views';
import { ClusterMonitoringStackDashboard, OptimizationClusterOverview } from '../pages/clusters';
import { WelcomeIssuesPage, WelcomePage } from '../pages/welcome';
import { DeploymentManagementPage } from '../pages/deployment';
import { ModelMarketPage } from '../pages/model-market';
import { StorageManagementPage } from '../pages/storage';
import { ModelCachePage } from '../pages/model-cache';
import { AIProvidersPage } from '../pages/ai-providers';
import { ApiKeysPage, ModelServiceAdminPage, ModelServicePage, ModelUsagePage } from '../pages/model-service';
import { PlaygroundPage } from '../pages/playground';
import {
  OptimizationEvaluateWorkspace,
  OptimizationOverview,
  OptimizationStage,
  WorkflowProvider,
} from '../pages/optimization';
import { EvaluationDashboardEntry, EvaluationTaskWizard, OptimizationEvaluationDetails } from '../pages/evaluation';
import { SimulationDashboard, SimulationTaskDetails } from '../pages/simulation';
const InferenceDesigner = lazy(() => import('../components/InferenceDesigner/InferenceDesigner').then((module) => ({ default: module.InferenceDesigner })));

import LeftNavigation from './ui/LeftNavigation';
import { EmptyState, LoadingState } from '../shared/ui';
import { AuthProvider, ChangePasswordPage, LoginPage, useAuth } from '../pages/auth';
import { canAccessView } from '../pages/auth/permissions';
import {
  AuditLogsPage,
  GroupsPage,
  IdentityProvidersPage,
  RolesPage,
  SecretKeyPage,
  SessionsPage,
  UsersPage,
} from '../pages/administration';

function AuthenticatedApp({ initialView = null }) {
  const { canView } = useAuth();
  const mainRef = useRef(null);
  const [currentView, setCurrentView] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    return initialView ?? resolveView(params.get('view'), firstAccessibleView(canView));
  });

  const [navigationParams, setNavigationParams] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    let profileDeployment = null;
    try {
      const raw = params.get('profileDeployment');
      profileDeployment = raw ? JSON.parse(raw) : null;
    } catch {
      profileDeployment = null;
    }
    return {
      evaluationId: params.get('evaluationId') || null,
      runKind: params.get('runKind') || null,
      taskId: params.get('taskId') || null,
      clusterId: params.get('clusterId') || null,
      deploymentSearch: params.get('deploymentSearch') || '',
      profileDeployment,
      action: params.get('action') || null,
      groupId: params.get('groupId') || null,
      executionId: params.get('executionId') || null,
      volumeId: params.get('volumeId') || null,
      entryId: params.get('entryId') || null,
      purpose: params.get('purpose') || null,
      severity: params.get('severity') || '',
      category: params.get('category') || '',
      issueSearch: params.get('search') || '',
      usageWindow: params.get('usageWindow') || '24h',
      since: params.get('since') || null,
      until: params.get('until') || null,
      userId: params.get('userId') || null,
    };
  });

  const [welcomeClusterId, setWelcomeClusterId] = useState(() => (
    currentView === 'welcome' ? navigationParams.clusterId || '' : ''
  ));
  const [isMobileNavOpen, setIsMobileNavOpen] = useState(false);
  const [cacheFromModelMarket, setCacheFromModelMarket] = useState(false);
  const [isSidebarExpanded, setIsSidebarExpanded] = useState(() => {
    const saved = localStorage.getItem('prism_sidebar_expanded');
    return saved !== null ? saved === 'true' : true;
  });
  useEffect(() => {
    const root = window.document.documentElement;
    root.classList.add('dark');
    localStorage.setItem('app-theme', 'dark');
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if ((params.get('view') || '') === currentView) return;
    if (currentView) params.set('view', currentView);
    else params.delete('view');
    window.history.replaceState({}, '', `${window.location.pathname}?${params.toString()}`);
  }, [currentView]);

  useEffect(() => {
    const onPopState = () => {
      const params = new URLSearchParams(window.location.search);
      const resolvedView = resolveView(params.get('view'), firstAccessibleView(canView));
      let profileDeployment = null;
      try {
        const raw = params.get('profileDeployment');
        profileDeployment = raw ? JSON.parse(raw) : null;
      } catch {
        profileDeployment = null;
      }
      setCurrentView(resolvedView);
      setNavigationParams((previous) => ({
        ...previous,
        profileDeployment,
        clusterId: params.get('clusterId') || null,
        deploymentSearch: params.get('deploymentSearch') || '',
        action: params.get('action') || null,
        groupId: params.get('groupId') || null,
        executionId: params.get('executionId') || null,
        volumeId: params.get('volumeId') || null,
        entryId: params.get('entryId') || null,
        purpose: params.get('purpose') || null,
        severity: params.get('severity') || '',
        category: params.get('category') || '',
        issueSearch: params.get('search') || '',
        usageWindow: params.get('usageWindow') || '24h',
        since: params.get('since') || null,
        until: params.get('until') || null,
        userId: params.get('userId') || null,
      }));
      if (!['model-market', 'model-cache'].includes(resolvedView)) {
        setCacheFromModelMarket(false);
      }
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [canView]);


  const handleNavigate = (requestedView, extraParams = {}) => {
    const view = resolveView(requestedView, firstAccessibleView(canView));
    setCacheFromModelMarket((current) => (
      (currentView === 'model-market' && view === 'model-cache')
      || (current && ['model-market', 'model-cache'].includes(view))
    ));
    setCurrentView(view);
    if (view === 'welcome') setWelcomeClusterId(extraParams?.clusterId || '');
    setIsMobileNavOpen(false); // Close mobile nav on navigation

    // Update URL to reflect the current view
    const params = new URLSearchParams(window.location.search);
    params.set('view', view);
    params.delete('workload');
    params.delete('intent');
    const resolvedParams = {
      downloadModel: view === 'model-cache' ? extraParams.downloadModel || null : null,
    };
    if (view === 'optimization-deployments' && extraParams?.deploymentSearch) {
      params.set('deploymentSearch', extraParams.deploymentSearch);
      resolvedParams.deploymentSearch = extraParams.deploymentSearch;
    } else {
      params.delete('deploymentSearch');
      resolvedParams.deploymentSearch = '';
    }
    if (view === 'optimization-evaluation-details' && extraParams?.evaluationId) {
      params.set('evaluationId', extraParams.evaluationId);
      resolvedParams.evaluationId = extraParams.evaluationId;
    } else {
      params.delete('evaluationId');
      resolvedParams.evaluationId = null;
    }
    if (view === 'optimization-evaluation-details' && extraParams?.runKind) {
      params.set('runKind', extraParams.runKind);
      resolvedParams.runKind = extraParams.runKind;
    } else {
      params.delete('runKind');
      resolvedParams.runKind = null;
    }
    if (view === 'optimization-simulate-details' && extraParams?.taskId) {
      params.set('taskId', extraParams.taskId);
      resolvedParams.taskId = extraParams.taskId;
    } else {
      params.delete('taskId');
      resolvedParams.taskId = null;
    }
    if (
      ['welcome', 'welcome/issues', 'admin/usage', 'storage-management', 'model-cache',
        'clusters', 'cluster-monitoring-stack', 'optimization-deployments', 'admin/model-service'].includes(view)
      && extraParams?.clusterId
    ) {
      params.set('clusterId', extraParams.clusterId);
      resolvedParams.clusterId = extraParams.clusterId;
    } else {
      params.delete('clusterId');
      resolvedParams.clusterId = null;
    }
    const profileDeployment = extraParams?.profileDeployment
      || (view === 'cluster-monitoring-stack' && extraParams?.executionId
        ? {
          execution_id: extraParams.executionId,
          display_name: extraParams.executionName || extraParams.executionId,
        }
        : null);
    if (view === 'cluster-monitoring-stack' && profileDeployment) {
      params.set('profileDeployment', JSON.stringify(profileDeployment));
      resolvedParams.profileDeployment = profileDeployment;
    } else {
      params.delete('profileDeployment');
      resolvedParams.profileDeployment = null;
    }
    const assign = (key, value, urlKey = key) => {
      if (value) params.set(urlKey, value);
      else params.delete(urlKey);
      resolvedParams[key] = value || null;
    };
    assign('action', ['clusters', 'admin/model-service', 'model-cache', 'optimization-simulate', 'ai-providers', 'storage-management'].includes(view) ? extraParams?.action || null : null);
    assign('groupId', view === 'admin/model-service' ? extraParams?.groupId || null : null);
    assign('executionId', ['cluster-monitoring-stack', 'admin/model-service'].includes(view) ? extraParams?.executionId || null : null);
    assign('volumeId', view === 'storage-management' ? extraParams?.volumeId || null : null);
    assign('purpose', view === 'storage-management' ? extraParams?.purpose || null : null);
    assign('entryId', view === 'model-cache' ? extraParams?.entryId || null : null);
    assign('severity', view === 'welcome/issues' ? extraParams?.severity || null : null);
    assign('category', view === 'welcome/issues' ? extraParams?.category || null : null);
    assign('issueSearch', view === 'welcome/issues' ? extraParams?.search || null : null, 'search');
    assign('usageWindow', view === 'admin/usage' ? extraParams?.usageWindow || null : null);
    assign('since', view === 'admin/usage' ? extraParams?.since || null : null);
    assign('until', view === 'admin/usage' ? extraParams?.until || null : null);
    assign('userId', ['admin/usage', 'admin/users'].includes(view) ? extraParams?.userId || null : null);
    setNavigationParams(resolvedParams);

    window.history.pushState({}, '', `${window.location.pathname}?${params.toString()}`);

    // Reset scroll position on navigation
    window.scrollTo(0, 0);
    if (mainRef.current) {
      mainRef.current.scrollTop = 0;
    }
  };

  const viewAllowed = Boolean(currentView) && canView(currentView);

  return (
      <div className="min-h-screen bg-slate-950 w-full overflow-hidden font-sans relative flex flex-col">
        <a
          href="https://llm-d.ai"
          target="_blank"
          rel="noopener noreferrer"
          className={`fixed left-4 top-5 z-[60] hidden md:inline-flex ${
            isSidebarExpanded ? 'items-center gap-2' : 'flex-col items-start gap-0.5'
          }`}
          aria-label="llm-d"
        >
          <img src="https://llm-d.ai/img/llm-d-logotype-and-icon.png" alt="llm-d" className="h-6 w-auto object-contain" />
          <span className="select-none bg-gradient-to-r from-sky-500 via-cyan-400 to-teal-400 bg-clip-text text-xl font-bold tracking-wide text-transparent">Lens</span>
        </a>
        <LeftNavigation currentView={currentView} onNavigate={handleNavigate} isMobileOpen={isMobileNavOpen} isExpanded={isSidebarExpanded} onExpandedChange={setIsSidebarExpanded} />
        <main ref={mainRef} className={`relative flex h-screen min-w-0 flex-1 flex-col overflow-y-auto transition-[margin] duration-300 ${isSidebarExpanded ? 'md:ml-[18rem]' : 'md:ml-24'}`}>
          {canView('welcome') && (
            <div className={currentView === 'welcome' ? '' : 'hidden'} aria-hidden={currentView !== 'welcome'}>
              <WelcomePage
                onNavigate={handleNavigate}
                onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)}
                initialClusterId={welcomeClusterId}
                isActive={currentView === 'welcome'}
              />
            </div>
          )}
          {!viewAllowed ? (
            <div className="flex flex-1 items-center justify-center p-6">
              <EmptyState
                title="Access denied"
                message="You do not have permission to view this page."
              />
            </div>
          ) : (
          <WorkflowProvider>
            {currentView === 'welcome/issues' && <WelcomeIssuesPage onNavigate={handleNavigate} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} initialClusterId={navigationParams.clusterId || ''} initialSeverity={navigationParams.severity || ''} initialCategory={navigationParams.category || ''} initialSearch={navigationParams.issueSearch || ''} />}
            {currentView === 'cluster-monitoring-stack' && <ClusterMonitoringStackDashboard onNavigateBack={() => handleNavigate('home')} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} initialClusterId={navigationParams.clusterId} initialProfileDeployment={navigationParams.profileDeployment} />}
            {currentView === 'clusters' && <OptimizationClusterOverview onNavigate={handleNavigate} initialClusterId={navigationParams.clusterId || ''} initialAction={navigationParams.action || ''} onNavigateBack={() => handleNavigate('home')} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'storage-management' && <StorageManagementPage onNavigate={handleNavigate} initialClusterId={navigationParams.clusterId || ''} initialVolumeId={navigationParams.volumeId || ''} initialAction={navigationParams.action || ''} initialPurpose={navigationParams.purpose || ''} />}
            {currentView === 'model-cache' && <ModelCachePage onNavigate={handleNavigate} initialClusterId={navigationParams.clusterId || ''} initialEntryId={navigationParams.entryId || ''} initialAction={navigationParams.action || ''} initialDownload={navigationParams.downloadModel} onDownloadHandled={() => setNavigationParams((current) => ({ ...current, downloadModel: null }))} onNavigateBack={cacheFromModelMarket ? () => handleNavigate('model-market') : undefined} />}
            {currentView === 'ai-providers' && <AIProvidersPage initialAction={navigationParams.action || ''} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'model-service' && <ModelServicePage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'api-keys' && <ApiKeysPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'usage' && <ModelUsagePage mode="self" onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'playground' && <PlaygroundPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'custom-deployment-configuration' && <Suspense fallback={<LoadingState />}><InferenceDesigner onNavigate={handleNavigate} /></Suspense>}
            {currentView === 'optimization-deployments' && <DeploymentManagementPage key={JSON.stringify([navigationParams.deploymentSearch, navigationParams.clusterId])} initialSearch={navigationParams.deploymentSearch} initialClusterId={navigationParams.clusterId || ''} onNavigate={handleNavigate} />}
            {(currentView === 'model-market' || (currentView === 'model-cache' && cacheFromModelMarket)) && (
              <div hidden={currentView !== 'model-market'}>
                <ModelMarketPage onNavigate={handleNavigate} isActive={currentView === 'model-market'} />
              </div>
            )}
            {currentView === 'optimization-evaluate' && <EvaluationDashboardEntry onNavigate={handleNavigate} />}
            {currentView === 'optimization-evaluate-new' && <EvaluationTaskWizard onNavigate={handleNavigate} />}
            {currentView === 'optimization-evaluation-details' && <OptimizationEvaluationDetails onNavigate={handleNavigate} />}
            {currentView === 'optimization-deploy' && <OptimizationEvaluateWorkspace onNavigate={handleNavigate} />}
            {currentView === 'optimization-workspace' && <OptimizationOverview onNavigateBack={() => handleNavigate('home')} onNavigate={handleNavigate} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'optimization-simulate' && <SimulationDashboard onNavigate={handleNavigate} onNavigateBack={() => handleNavigate('home')} initialAction={navigationParams.action || ''} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'optimization-simulate-details' && <SimulationTaskDetails onNavigate={handleNavigate} />}
            {['opt-define', 'opt-search', 'opt-deploy', 'opt-benchmark', 'opt-performance'].includes(currentView) && (
              <OptimizationStage stageView={currentView} onNavigateBack={() => handleNavigate('home')} onNavigate={handleNavigate} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />
            )}
            {currentView === 'admin/users' && <UsersPage initialUserId={navigationParams.userId || ''} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/groups' && <GroupsPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/roles' && <RolesPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/identity-providers' && <IdentityProvidersPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/master-key' && <SecretKeyPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/sessions' && <SessionsPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/audit' && <AuditLogsPage onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/model-service' && <ModelServiceAdminPage key={navigationParams.clusterId || ''} initialClusterId={navigationParams.clusterId || ''} initialGroupId={navigationParams.groupId || ''} initialAction={navigationParams.action || ''} initialExecutionId={navigationParams.executionId || ''} onNavigate={handleNavigate} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
            {currentView === 'admin/usage' && <ModelUsagePage mode="admin" initialClusterId={navigationParams.clusterId || ''} initialGroupId={navigationParams.groupId || ''} initialUserId={navigationParams.userId || ''} initialSince={navigationParams.since} initialUntil={navigationParams.until} initialUsageWindow={navigationParams.usageWindow || ''} onToggleMobileNav={() => setIsMobileNavOpen(!isMobileNavOpen)} />}
          </WorkflowProvider>
          )}

        </main>
      </div>
  );
}

function AuthGate() {
  const { status, principal } = useAuth();
  const [loginLandingView, setLoginLandingView] = useState(null);
  if (status === 'loading') {
    return <LoadingState fullPage label="Checking your session" />;
  }
  if (status === 'anonymous') {
    return <LoginPage onSignedIn={(signedInPrincipal) => {
      const view = firstAccessibleView((candidate) => canAccessView(signedInPrincipal.permissions || [], candidate));
      const params = new URLSearchParams();
      if (view) params.set('view', view);
      window.history.replaceState({}, '', `${window.location.pathname}${params.size ? `?${params}` : ''}`);
      setLoginLandingView(view);
    }} />;
  }
  if (principal?.mustChangePassword) {
    return <ChangePasswordPage />;
  }
  return <AuthenticatedApp key={loginLandingView ?? 'session'} initialView={loginLandingView} />;
}

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <AuthGate />
      </AuthProvider>
    </ErrorBoundary>
  );
}
