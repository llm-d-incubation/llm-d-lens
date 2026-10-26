import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, ChevronLeft, Plug, Plus, RefreshCw, Send, Server } from 'lucide-react';
import { usePolling } from '../../../shared/lib/usePolling';
import { errorMessage } from '../../../shared/lib/errorMessage';
import { cn } from '../../../shared/lib/cn';
import { Button } from '../../../shared/ui/Button';
import { Panel } from '../../../shared/ui/Panel';
import { Modal } from '../../../shared/ui/Modal';
import { AnimatedBotIcon } from '../../../shared/ui/AnimatedBotIcon';
import { HelpTooltip, Label, Select, Textarea } from '../../../shared/ui/FormControls';
import { Spinner } from '../../../shared/ui/Spinner';
import { useAuth } from '../../auth';
import { listAIProviders } from '../../ai-providers';
import { getWelcomeOverview } from '../api/welcomeBackend';
import { MAX_PROMPT_LENGTH, stageAssistantDraft } from '../../../shared/lib/assistantHandoff';
import { readWelcomeOverview, welcomeCacheKey, writeWelcomeOverview } from '../model/welcomeCache';
import { IssueList } from './IssueList';
import { ProviderSelect } from './ProviderSelect';
import { QuickActions } from './QuickActions';
import { WELCOME_CARD } from './welcomeStyles';
import { visibleWelcomeActions } from '../model/quickActions';
import { formatAge } from '../model/welcomePresentation';

function scopeOptions(scope) {
    return [
        { value: '', label: 'All accessible clusters' },
        ...(scope?.clusters || []).map((cluster) => ({ value: cluster.id, label: cluster.name || cluster.id })),
    ];
}

function SystemBrief({ clusters, clusterId, issueCounts, clusterIssueCounts, lastChecked, loading, onSelectCluster }) {
    const critical = issueCounts.critical || 0;
    const warning = issueCounts.warning || 0;
    const total = issueCounts.total || 0;
    const stateColor = critical ? 'bg-rose-500' : warning ? 'bg-amber-500' : 'bg-emerald-500';
    const headline = critical
        ? `${critical} critical signal${critical === 1 ? '' : 's'} need${critical === 1 ? 's' : ''} attention`
        : warning
            ? `${warning} signal${warning === 1 ? '' : 's'} need${warning === 1 ? 's' : ''} attention`
            : 'All monitored systems are operational';
    const detail = critical || warning
        ? 'Start with the highest-severity signal below, or ask Lens Assistant to investigate a cluster.'
        : 'Health checks and telemetry are reporting normally. You can ask Lens Assistant to summarize the fleet.';
    return (
        <section className="relative overflow-hidden rounded-2xl border border-slate-200 bg-white dark:border-slate-800/70 dark:bg-slate-900/70">
            <span className={`absolute inset-y-0 left-0 w-1 ${stateColor}`} aria-hidden="true" />
            <div className="flex flex-col gap-4 p-4 pl-5 xl:flex-row xl:items-center xl:justify-between">
                <div className="min-w-0">
                    <div className="flex items-center gap-2 text-xs font-medium text-slate-500">
                        <span className={`h-1.5 w-1.5 rounded-full ${stateColor} motion-safe:animate-pulse [animation-duration:2.4s]`} aria-hidden="true" />
                        {loading ? 'Checking fleet status' : `Live · checked ${formatAge(lastChecked)}`}
                    </div>
                    <h2 className="mt-1.5 text-lg font-semibold leading-snug tracking-tight text-theme-text">
                        {headline}
                    </h2>
                    <p className="mt-1 max-w-2xl text-xs leading-5 text-theme-muted">{detail}</p>
                </div>
                <div className="grid shrink-0 grid-cols-3 gap-4 border-t border-theme-border pt-3 xl:border-t-0 xl:pt-0">
                    <div>
                        <div className="text-xl font-semibold tabular-nums text-theme-text">{clusters.length}</div>
                        <div className="mt-0.5 text-[11px] text-slate-500">clusters</div>
                    </div>
                    <div>
                        <div className="text-xl font-semibold tabular-nums text-theme-text">{total}</div>
                        <div className="mt-0.5 text-[11px] text-slate-500">signals</div>
                    </div>
                    <div>
                        <div className={cn('text-xl font-semibold tabular-nums', critical ? 'text-rose-600 dark:text-rose-300' : 'text-theme-text')}>{critical}</div>
                        <div className="mt-0.5 text-[11px] text-slate-500">critical</div>
                    </div>
                </div>
            </div>
            {clusters.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5 border-t border-theme-border px-4 py-3 pl-5">
                    <button
                        type="button"
                        onClick={() => onSelectCluster('')}
                        aria-pressed={!clusterId}
                        className={`cluster-scope-chip rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${!clusterId ? 'border-[#037ffc] bg-[#037ffc] text-[#ffffff]' : 'border-slate-200 bg-white text-slate-600 hover:border-[#037ffc] hover:text-[#037ffc] dark:border-slate-700 dark:bg-transparent dark:text-slate-300 dark:hover:border-sky-500/50 dark:hover:text-sky-300'}`}
                    >
                        All clusters
                    </button>
                    {clusters.map((cluster) => {
                        const clusterIdValue = cluster.id || cluster.clusterId;
                        const selected = clusterId === clusterIdValue;
                        const issueCount = clusterIssueCounts[clusterIdValue] || 0;
                        return (
                            <button
                                key={clusterIdValue}
                                type="button"
                                onClick={() => onSelectCluster(clusterIdValue)}
                                aria-pressed={selected}
                                className={`cluster-scope-chip inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${selected ? 'border-[#037ffc] bg-[#037ffc] text-[#ffffff]' : 'border-slate-200 bg-white text-slate-600 hover:border-[#037ffc] hover:text-[#037ffc] dark:border-slate-700 dark:bg-transparent dark:text-slate-300 dark:hover:border-sky-500/50 dark:hover:text-sky-300'}`}
                            >
                                <span className={`h-1.5 w-1.5 rounded-full ${issueCount ? 'bg-amber-500' : 'bg-emerald-500'}`} aria-hidden="true" />
                                {cluster.name || clusterIdValue}
                                {issueCount > 0 && <span className={selected ? 'text-white/75' : 'text-slate-400'}>{issueCount}</span>}
                            </button>
                        );
                    })}
                </div>
            )}
        </section>
    );
}

export default function WelcomePage({
    onNavigate,
    initialClusterId = '',
    isActive = true,
}) {
    const { principal, can, canView } = useAuth();
    const clusterId = initialClusterId || '';
    const canChat = can('playground:chat:use') && canView('playground');
    const canReadProviders = canChat && can('ai-provider:provider:read') && canView('ai-providers');
    const canCreateCluster = can('cluster:cluster:create') && canView('clusters');

    const cacheKey = useMemo(
        () => welcomeCacheKey({
            userId: principal?.userId,
            authzVersion: principal?.authzVersion,
            clusterId,
        }),
        [principal?.userId, principal?.authzVersion, clusterId],
    );
    const [overview, setOverview] = useState(() => readWelcomeOverview(cacheKey));
    const [issuesLoading, setIssuesLoading] = useState(() => !readWelcomeOverview(cacheKey));
    const [issuesError, setIssuesError] = useState('');
    const [providers, setProviders] = useState([]);
    const [providersLoading, setProvidersLoading] = useState(canReadProviders);
    const [providersError, setProvidersError] = useState('');
    const [selectedProviderId, setSelectedProviderId] = useState('');
    const [prompt, setPrompt] = useState('');
    const [composerError, setComposerError] = useState('');
    const [handoffPending, setHandoffPending] = useState(false);
    const [quickActionsCollapsed, setQuickActionsCollapsed] = useState(false);
    const [severity, setSeverity] = useState('');
    const [showCreateClusterPrompt, setShowCreateClusterPrompt] = useState(false);
    const issueGeneration = useRef(0);

    const loadIssues = useCallback(async ({ quiet = false, force = false } = {}) => {
        const requestGeneration = ++issueGeneration.current;
        if (!quiet) setIssuesLoading(true);
        try {
            const payload = await getWelcomeOverview({ clusterId, refresh: force });
            if (requestGeneration !== issueGeneration.current) return;
            setOverview(payload);
            writeWelcomeOverview(cacheKey, payload);
            setIssuesError('');
        } catch (failure) {
            if (requestGeneration !== issueGeneration.current) return;
            setIssuesError(errorMessage(failure, 'Failed to load cluster issues'));
        } finally {
            if (requestGeneration === issueGeneration.current) setIssuesLoading(false);
        }
    }, [cacheKey, clusterId]);

    const loadProviders = useCallback(async () => {
        if (!canReadProviders) {
            setProviders([]);
            setProvidersLoading(false);
            return;
        }
        setProvidersLoading(true);
        setProvidersError('');
        try {
            const items = await listAIProviders();
            const compatible = items.filter((provider) => provider.providerType === 'openai');
            setProviders(compatible);
            setSelectedProviderId((current) => (
                current && compatible.some((provider) => provider.id === current)
                    ? current
                    : compatible[0]?.id || ''
            ));
        } catch (failure) {
            setProvidersError(errorMessage(failure, 'Failed to load AI providers'));
        } finally {
            setProvidersLoading(false);
        }
    }, [canReadProviders]);

    useEffect(() => {
        const cached = readWelcomeOverview(cacheKey);
        setOverview(cached);
        setIssuesError('');
        setIssuesLoading(!cached);
        loadIssues({ quiet: Boolean(cached) });
    }, [cacheKey, loadIssues]);

    useEffect(() => {
        if (isActive) loadProviders();
    }, [isActive, loadProviders]);

    useEffect(() => {
        // The page stays mounted while hidden so issue polling continues. Once
        // the handoff navigates to Lens Assistant, clear the composer's pending
        // state; otherwise the Send button keeps spinning when the user returns.
        if (!isActive) setHandoffPending(false);
    }, [isActive]);

    useEffect(() => {
        if (!isActive || !overview || !canCreateCluster) return;
        if ((overview.scope?.clusters || []).length > 0) return;
        if (sessionStorage.getItem('prism_welcome_create_cluster_prompted') === '1') return;
        setShowCreateClusterPrompt(true);
    }, [canCreateCluster, isActive, overview]);

    const dismissCreateClusterPrompt = () => {
        sessionStorage.setItem('prism_welcome_create_cluster_prompted', '1');
        setShowCreateClusterPrompt(false);
    };

    const openCreateCluster = () => {
        dismissCreateClusterPrompt();
        onNavigate('clusters', { action: 'create' });
    };

    usePolling(() => loadIssues({ quiet: true }), { intervalMs: 15000 });

    const allIssues = useMemo(() => overview?.issues?.data || [], [overview]);
    const filteredIssues = useMemo(
        () => (severity ? allIssues.filter((issue) => issue.severity === severity) : allIssues),
        [allIssues, severity],
    );
    const issueCounts = useMemo(() => ({
        total: allIssues.length,
        critical: allIssues.filter((issue) => issue.severity === 'critical').length,
        warning: allIssues.filter((issue) => issue.severity === 'warning').length,
        info: allIssues.filter((issue) => issue.severity === 'info').length,
    }), [allIssues]);
    const clusterIssueCounts = useMemo(() => {
        const counts = {};
        allIssues.forEach((issue) => {
            if (!issue.clusterId) return;
            counts[issue.clusterId] = (counts[issue.clusterId] || 0) + 1;
        });
        return counts;
    }, [allIssues]);
    const askAboutIssue = useCallback((issue) => {
        const draft = `Investigate this issue and explain how to resolve it using the Lens UI. Give step-by-step page navigation and the controls or fields to use. Do not answer with API requests or shell commands.\n\n${issue.title}\n${issue.message}`;
        setPrompt(draft.slice(0, MAX_PROMPT_LENGTH));
        requestAnimationFrame(() => {
            const input = document.getElementById('welcome-assistant-prompt');
            input?.focus();
            input?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        });
    }, []);
    const quickActions = visibleWelcomeActions(can, canView);
    const hasRightRail = canChat || quickActions.length > 0;
    const rightRailCollapsed = !canChat && quickActionsCollapsed;
    const issueCount = filteredIssues.length;
    const issuesChecking = issuesLoading && !overview?.issues;
    const issuesPartial = Boolean(
        overview?.issues && !issuesChecking && (overview.issues.stale || overview.issues.state !== 'complete')
    );
    const selectedProvider = providers.find((provider) => provider.id === selectedProviderId) || null;
    const hasCompatibleProviders = providers.length > 0;
    const canSend = canChat
        && hasCompatibleProviders
        && Boolean(selectedProvider)
        && prompt.trim().length > 0
        && !handoffPending;

    const send = () => {
        if (!canSend) return;
        setComposerError('');
        setHandoffPending(true);
        try {
            stageAssistantDraft({ prompt, providerId: selectedProviderId });
            setPrompt('');
            onNavigate('playground');
        } catch (failure) {
            setComposerError(errorMessage(failure, 'Could not prepare the message'));
            setHandoffPending(false);
        }
    };

    const composer = !canChat ? (
        <Panel title="Lens Assistant" className={WELCOME_CARD}>
            <p className="text-sm text-theme-muted">Lens Assistant is not available for this account.</p>
        </Panel>
    ) : (
        <Panel
            title={(
                <span className="inline-flex items-center gap-2">
                    <AnimatedBotIcon className="h-5 w-5 text-[#037ffc] dark:text-sky-400" />
                    Ask Lens Assistant
                    <HelpTooltip
                        label="How Ask Lens Assistant works"
                        className="relative inline-flex"
                        tooltipClassName="left-0 top-full w-72 pt-1 z-30"
                    >
                        <span className="block space-y-1.5">
                            <span className="block">Choose an AI provider, type a message, then press <kbd className="rounded border border-theme-border px-1 py-0.5 font-mono text-[10px]">Enter</kbd>. Use <kbd className="rounded border border-theme-border px-1 py-0.5 font-mono text-[10px]">Shift+Enter</kbd> for a new line.</span>
                            <span className="block">Sending opens Lens Assistant with the same provider selected and submits the message automatically.</span>
                            <span className="block">Tool calls still follow Lens Assistant&apos;s confirmation flow. The prompt is passed through temporary browser storage, not the URL.</span>
                            <span className="block pt-1 text-theme-muted">
                                If no compatible provider is available, add one under{' '}
                                {canView('ai-providers') ? (
                                    <button
                                        type="button"
                                        onClick={() => onNavigate('ai-providers')}
                                        className="font-semibold text-sky-600 underline underline-offset-2 hover:text-sky-500 dark:text-sky-300"
                                    >
                                        External providers
                                    </button>
                                ) : (
                                    'External providers'
                                )}
                                .
                            </span>
                            <span className="block pt-1 font-semibold text-amber-600 dark:text-amber-300">Lens Assistant is AI and can make mistakes. Verify important results.</span>
                        </span>
                    </HelpTooltip>
                </span>
            )}
            padding="sm"
            className="flex w-full flex-col shadow-none"
        >
            <div>
                <Label htmlFor="welcome-assistant-prompt">Message</Label>
                <Textarea
                    id="welcome-assistant-prompt"
                    aria-label="Message Lens Assistant"
                    rows={5}
                    value={prompt}
                    maxLength={MAX_PROMPT_LENGTH}
                    onChange={(event) => setPrompt(event.target.value)}
                    onKeyDown={(event) => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                            event.preventDefault();
                            send();
                        }
                    }}
                    placeholder="Ask anything"
                    className="min-h-32 resize-y font-sans text-sm"
                />
                <div className="mt-3 flex items-center justify-between gap-2 border-t border-theme-border pt-3">
                    <div className="min-w-0">
                        <ProviderSelect
                            providers={providers}
                            value={selectedProviderId}
                            onChange={setSelectedProviderId}
                            disabled={!canReadProviders || providersLoading}
                            loading={providersLoading}
                            emptyLabel={!canReadProviders ? 'Provider access required' : 'No compatible provider'}
                            onAddProvider={canView('ai-providers') ? () => onNavigate('ai-providers', { action: 'add' }) : undefined}
                        />
                    </div>
                    <Button
                        variant="ghost"
                        size="icon"
                        onClick={send}
                        disabled={!canSend}
                        isLoading={handoffPending}
                        aria-label="Send message to Lens Assistant"
                        title="Send"
                        className="shrink-0 text-slate-500 hover:bg-slate-900/10 hover:text-emerald-600 dark:text-slate-400 dark:hover:bg-slate-800/60 dark:hover:text-emerald-300"
                    >
                        {!handoffPending && <Send size={15} aria-hidden="true" />}
                    </Button>
                </div>
            </div>
            {prompt.length > MAX_PROMPT_LENGTH - 500 && (
                <p className="mt-1.5 text-right text-[10px] text-theme-muted">
                    {prompt.length}/{MAX_PROMPT_LENGTH}
                </p>
            )}
            {providersError && (
                <div className="mt-3 flex flex-wrap items-center gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-600 dark:text-rose-300">
                    <span className="flex items-center gap-1.5"><AlertCircle size={13} /> {providersError}</span>
                    <Button variant="ghost" size="xs" onClick={loadProviders}>Retry</Button>
                </div>
            )}
            {!providersError && canReadProviders && !providersLoading && !hasCompatibleProviders && (
                <div className="mt-3 flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-300">
                    <span className="flex items-center gap-1.5">
                        <Plug size={13} /> No AI provider is available for Lens Assistant. Add an OpenAI-compatible provider in External providers.
                    </span>
                    {canView('ai-providers') && (
                        <Button variant="secondary" size="xs" onClick={() => onNavigate('ai-providers')}>
                            Open External providers
                        </Button>
                    )}
                </div>
            )}
            {!canReadProviders && (
                <p className="mt-3 text-[11px] text-theme-muted">
                    AI provider information is not available for this account.
                </p>
            )}
            {composerError && (
                <p className="mt-2 flex items-center gap-1.5 text-[11px] text-rose-600 dark:text-rose-300">
                    <AlertCircle size={13} /> {composerError}
                </p>
            )}
        </Panel>
    );

    return (
        <div className="welcome-page relative min-h-full bg-theme-bg">
            <div className="pointer-events-none absolute inset-0 hidden overflow-hidden dark:block" aria-hidden="true">
                <div className="absolute -top-40 left-1/4 h-96 w-[42rem] rounded-full bg-sky-400/10 blur-3xl dark:bg-sky-500/10" />
                <div className="absolute -bottom-48 right-0 h-80 w-[32rem] rounded-full bg-cyan-400/10 blur-3xl dark:bg-cyan-400/5" />
            </div>
            <div className="relative z-10 w-full px-4 py-6 sm:px-6 lg:px-12 lg:py-10">
                <div className={cn('grid items-start gap-4', hasRightRail && (rightRailCollapsed ? 'lg:grid-cols-[minmax(0,1fr)_2.5rem]' : 'lg:grid-cols-[minmax(0,1fr)_22rem] xl:grid-cols-[minmax(0,1fr)_24rem]'))}>
                    <div className={cn('flex min-h-0 flex-col gap-4')}>
                        <SystemBrief
                            clusters={overview?.scope?.clusters || []}
                            clusterId={clusterId}
                            issueCounts={issueCounts}
                            clusterIssueCounts={clusterIssueCounts}
                            lastChecked={overview?.issues?.observedAt || overview?.issues?.generatedAt || overview?.generatedAt}
                            loading={issuesLoading && !overview}
                            onSelectCluster={(value) => onNavigate('welcome', { clusterId: value })}
                        />
                        <Panel
                            title={(
                                <span className="inline-flex items-center gap-1.5">
                                    Needs attention
                                    <HelpTooltip
                                        label="What Needs attention checks"
                                        className="relative inline-flex"
                                        tooltipClassName="left-0 top-full w-80 pt-1 z-30"
                                    >
                                        <span className="block space-y-1.5">
                                            <span className="block font-semibold">Readiness</span>
                                            <span className="block">No accessible clusters, a missing HF_TOKEN Secret, missing model-cache storage, an empty model-cache volume, no ready model in a cluster&apos;s cache, or no compatible External AI provider.</span>
                                            <span className="block pt-1 font-semibold">Cluster and hardware</span>
                                            <span className="block">Node readiness, worst-node disk (75% warning / 90% critical), memory pressure, missing accelerator drivers for present hardware, monitoring health (missing dashboards alone are informational) and fully allocated GPUs.</span>
                                            <span className="block pt-1 font-semibold">Model services</span>
                                            <span className="block">Enabled services without a serving target, provider readiness, Gateway/IPP availability and sustained serving pressure.</span>
                                            <span className="block pt-1 font-semibold">Deployments and dependencies</span>
                                            <span className="block">Deployment failures in the last 24 hours, missing or under-replicated model-server Pods, storage provisioning/capacity and model-cache download or node-sync failures.</span>
                                            <span className="block pt-1 font-semibold">Access</span>
                                            <span className="block">Active users with no direct role and no role inherited from their groups, or groups with no role binding.</span>
                                            <span className="block pt-1 text-theme-muted">High utilization alone is not an issue. Missing telemetry is shown as unknown or stale.</span>
                                        </span>
                                    </HelpTooltip>
                                    {issuesChecking && (
                                        <span className="ml-1 inline-flex items-center gap-1.5 rounded-full border border-sky-500/20 bg-sky-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-sky-600 dark:text-sky-300">
                                            <Spinner size="xs" />
                                            Checking
                                        </span>
                                    )}
                                    {issuesPartial && (
                                        <span className="ml-1 inline-flex items-center rounded-full border border-amber-500/25 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-300">
                                            Partial
                                        </span>
                                    )}
                                </span>
                            )}
                            padding="sm"
                            className="flex min-h-0 flex-1 flex-col border-0 bg-transparent shadow-none backdrop-blur-none"
                            actions={(
                                <div className="flex flex-wrap items-center gap-2">
                                    <Select
                                        aria-label="Severity filter for issues"
                                        value={severity}
                                        onChange={(event) => setSeverity(event.target.value)}
                                        className="w-36"
                                        variant="toolbar"
                                    >
                                        <option value="">All severities</option>
                                        <option value="critical">Critical</option>
                                        <option value="warning">Warning</option>
                                        <option value="info">Info</option>
                                    </Select>
                                    <Select
                                        aria-label="Cluster scope for issues"
                                        value={clusterId}
                                        onChange={(event) => onNavigate('welcome', { clusterId: event.target.value })}
                                        className="w-44"
                                        variant="toolbar"
                                    >
                                        {scopeOptions(overview?.scope).map((option) => (
                                            <option key={option.value || 'all'} value={option.value}>{option.label}</option>
                                        ))}
                                    </Select>
                                    <Button variant="ghost" size="xs" onClick={() => loadIssues({ quiet: true, force: true })}>
                                        <RefreshCw size={13} /> Refresh
                                    </Button>
                                </div>
                            )}
                        >
                            <div className="flex min-h-0 flex-1 flex-col">
                            {issuesError && (
                                <p className="mb-3 flex items-center gap-1.5 text-[11px] text-rose-600 dark:text-rose-300">
                                    <AlertCircle size={13} /> {issuesError}
                                </p>
                            )}
                            {issuesLoading && !overview ? (
                                <div className="flex items-center justify-center py-10 text-xs text-theme-muted">
                                    <Spinner size="xs" className="mr-2" /> Loading cluster issues…
                                </div>
                            ) : (
                                <div className="min-h-0 flex-1 overflow-y-auto">
                                    <IssueList
                                        issues={filteredIssues}
                                        onNavigate={onNavigate}
                                        onAskAI={canChat ? askAboutIssue : undefined}
                                        limit={8}
                                        emptyTitle={severity
                                            ? `No ${severity} issues`
                                            : overview?.scope?.clusters?.length ? 'All systems operational' : 'Cluster access needed'}
                                        emptyMessage={severity
                                            ? 'Adjust the severity filter to see other findings.'
                                            : overview?.scope?.clusters?.length
                                                ? 'Health checks and telemetry across monitored clusters are reporting normally. No action is required.'
                                                : 'Your account does not have access to a registered cluster.'}
                                    />
                                </div>
                            )}
                            {issueCount > 8 && (
                                <div className="mt-3 flex shrink-0 justify-end">
                                    <Button variant="ghost" size="xs" onClick={() => onNavigate('welcome/issues', { clusterId, severity })}>
                                        View all issues ({issueCount})
                                    </Button>
                                </div>
                            )}
                            </div>
                        </Panel>
                    </div>
                    {hasRightRail && (
                        <div className="flex min-h-0 flex-col gap-4">
                            {composer}
                            {quickActions.length > 0 && (
                                quickActionsCollapsed ? (
                                    <div className="flex justify-end">
                                        <Button
                                            variant="secondary"
                                            size="icon"
                                            onClick={() => setQuickActionsCollapsed(false)}
                                            aria-label="Expand quick actions"
                                            title="Expand quick actions"
                                            className="h-10 w-10 text-sky-600 dark:text-sky-300"
                                        >
                                            <ChevronLeft size={16} aria-hidden="true" />
                                        </Button>
                                    </div>
                                ) : (
                                    <div className="flex min-h-0 flex-col">
                                        <QuickActions onNavigate={onNavigate} onCollapse={() => setQuickActionsCollapsed(true)} />
                                    </div>
                                )
                            )}
                        </div>
                    )}
                </div>
            </div>

            <Modal
                isOpen={isActive && showCreateClusterPrompt}
                onClose={dismissCreateClusterPrompt}
                title="Connect your first cluster"
                subtitle="This takes about a minute."
                size="sm"
                footer={(
                    <>
                        <Button variant="secondary" onClick={dismissCreateClusterPrompt}>Maybe later</Button>
                        <Button variant="sky" onClick={openCreateCluster}>
                            <Plus size={14} aria-hidden="true" />
                            Create cluster
                        </Button>
                    </>
                )}
            >
                <div className="flex items-start gap-3.5">
                    <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-sky-500/15 to-cyan-400/10 text-sky-600 ring-1 ring-sky-500/20 dark:text-sky-300">
                        <Server size={22} strokeWidth={1.8} aria-hidden="true" />
                    </div>
                    <div className="min-w-0 flex-1">
                        <p className="text-sm leading-relaxed text-theme-muted">
                            No cluster is connected to this account yet. Add one to unlock:
                        </p>
                        <ul className="mt-3 space-y-2">
                            {['Deploy and publish models', 'Cache models and manage storage', 'Monitor services and inspect usage'].map((item) => (
                                <li key={item} className="flex items-center gap-2 text-xs text-theme-text">
                                    <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">
                                        <CheckCircle2 size={12} aria-hidden="true" />
                                    </span>
                                    {item}
                                </li>
                            ))}
                        </ul>
                    </div>
                </div>
                <p className="mt-4 rounded-xl border border-sky-500/15 bg-sky-500/5 px-3 py-2 text-[11px] leading-relaxed text-theme-muted">
                    You will choose the kubeconfig and cluster details in the next step.
                </p>
            </Modal>
        </div>
    );
}
