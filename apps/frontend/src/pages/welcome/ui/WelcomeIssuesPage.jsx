import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, Filter, Search } from 'lucide-react';
import { usePolling } from '../../../shared/lib/usePolling';
import { errorMessage } from '../../../shared/lib/errorMessage';
import { PageHeader } from '../../../shared/ui/PageHeader';
import { Button } from '../../../shared/ui/Button';
import { EmptyState } from '../../../shared/ui/EmptyState';
import { Input, Select } from '../../../shared/ui/FormControls';
import { LoadingState } from '../../../shared/ui/Spinner';
import { Panel } from '../../../shared/ui/Panel';
import { requestJson } from '../../../shared/api/httpClient';
import { CATEGORY_LABELS } from '../model/welcomePresentation';
import { getWelcomeIssues } from '../api/welcomeBackend';
import { IssueList } from './IssueList';
import { WELCOME_CARD } from './welcomeStyles';

const SEVERITIES = [
    { value: '', label: 'All severities' },
    { value: 'critical', label: 'Critical' },
    { value: 'warning', label: 'Warning' },
    { value: 'info', label: 'Info' },
];

const CATEGORIES = [
    { value: '', label: 'All categories' },
    ...Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label })),
];

export default function WelcomeIssuesPage({
    onNavigate,
    onToggleMobileNav,
    initialClusterId = '',
    initialSeverity = '',
    initialCategory = '',
    initialSearch = '',
}) {
    const clusterId = initialClusterId || '';
    const severity = initialSeverity || '';
    const category = initialCategory || '';
    const search = initialSearch || '';
    const [clusters, setClusters] = useState([]);
    const [items, setItems] = useState([]);
    const [total, setTotal] = useState(0);
    const [nextCursor, setNextCursor] = useState(null);
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [error, setError] = useState('');
    const [scopeLost, setScopeLost] = useState(false);
    const [notice, setNotice] = useState('');
    const [searchInput, setSearchInput] = useState(search);
    const [expandedIds, setExpandedIds] = useState(() => new Set());
    const generation = useRef(0);
    const hasFilters = Boolean(clusterId || severity || category || search);

    useEffect(() => {
        let cancelled = false;
        requestJson('/api/cluster/clusters')
            .then((payload) => {
                if (!cancelled) setClusters(payload?.items || []);
            })
            .catch(() => {
                if (!cancelled) setClusters([]);
            });
        return () => { cancelled = true; };
    }, []);

    const loadFirst = useCallback(async ({ quiet = false } = {}) => {
        const requestGeneration = ++generation.current;
        if (!quiet) setLoading(true);
        try {
            const payload = await getWelcomeIssues({ clusterId, severity, category, search, limit: 25 });
            if (requestGeneration !== generation.current) return;
            setItems(payload?.items || []);
            setTotal(payload?.total || 0);
            setNextCursor(payload?.nextCursor || null);
            setError('');
            setScopeLost(false);
            setNotice(payload?.restarted ? 'The issue list changed; showing from the first page.' : '');
        } catch (failure) {
            if (requestGeneration !== generation.current) return;
            if (failure?.status === 403 && clusterId) {
                setScopeLost(true);
                setError('The selected cluster is no longer available to this account.');
            } else {
                setError(errorMessage(failure, 'Failed to load issues'));
            }
        } finally {
            if (requestGeneration === generation.current) setLoading(false);
        }
    }, [category, clusterId, search, severity]);

    useEffect(() => {
        setSearchInput(search);
    }, [search]);

    useEffect(() => {
        loadFirst();
    }, [loadFirst]);

    usePolling(() => loadFirst({ quiet: true }), { intervalMs: 15000, enabled: !nextCursor });

    const loadMore = async () => {
        if (!nextCursor || loadingMore) return;
        setLoadingMore(true);
        try {
            const payload = await getWelcomeIssues({
                clusterId,
                severity,
                category,
                search,
                cursor: nextCursor,
                limit: 25,
            });
            if (payload?.restarted) {
                setItems(payload?.items || []);
                setNotice('The issue list changed; showing from the first page.');
            } else {
                setItems((current) => [...current, ...(payload?.items || [])]);
            }
            setTotal(payload?.total || 0);
            setNextCursor(payload?.nextCursor || null);
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load more issues'));
        } finally {
            setLoadingMore(false);
        }
    };

    const updateFilters = (next) => {
        onNavigate('welcome/issues', {
            clusterId: next.clusterId ?? clusterId,
            severity: next.severity ?? severity,
            category: next.category ?? category,
            search: next.search ?? search,
        });
    };

    const clusterOptions = useMemo(() => [
        { value: '', label: 'All accessible clusters' },
        ...clusters.map((cluster) => ({ value: cluster.id, label: cluster.name || cluster.id })),
    ], [clusters]);

    const toggleIssue = (id) => {
        setExpandedIds((current) => {
            const next = new Set(current);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    return (
        <div className="welcome-page relative min-h-full bg-theme-bg px-4 py-6 sm:px-6 lg:px-12 lg:py-10">
            <div className="pointer-events-none absolute inset-0 hidden overflow-hidden dark:block" aria-hidden="true">
                <div className="absolute -top-40 left-1/4 h-96 w-[42rem] rounded-full bg-sky-400/10 blur-3xl dark:bg-sky-500/10" />
            </div>
            <PageHeader
                className="relative bg-transparent px-0 pt-0 sm:px-0"
                title="Issues"
                subtitle={`${total} current finding${total === 1 ? '' : 's'} in the selected scope`}
                onNavigateBack={() => onNavigate('welcome', { clusterId })}
                onToggleMobileNav={onToggleMobileNav}
            />
            <div className="relative w-full space-y-4 pt-6">
                <Panel title="Filters" className={WELCOME_CARD}>
                    <div className="grid gap-3 md:grid-cols-4">
                        <Select
                            aria-label="Cluster scope"
                            value={clusterId}
                            onChange={(event) => updateFilters({ clusterId: event.target.value })}
                        >
                            {clusterOptions.map((option) => (
                                <option key={option.value || 'all'} value={option.value}>{option.label}</option>
                            ))}
                        </Select>
                        <Select
                            aria-label="Severity"
                            value={severity}
                            onChange={(event) => updateFilters({ severity: event.target.value })}
                        >
                            {SEVERITIES.map((option) => (
                                <option key={option.value || 'all'} value={option.value}>{option.label}</option>
                            ))}
                        </Select>
                        <Select
                            aria-label="Category"
                            value={category}
                            onChange={(event) => updateFilters({ category: event.target.value })}
                        >
                            {CATEGORIES.map((option) => (
                                <option key={option.value || 'all'} value={option.value}>{option.label}</option>
                            ))}
                        </Select>
                        <form
                            className="flex items-center gap-2"
                            onSubmit={(event) => {
                                event.preventDefault();
                                updateFilters({ search: searchInput.trim() });
                            }}
                        >
                            <Input
                                aria-label="Search issues"
                                icon={Search}
                                value={searchInput}
                                onChange={(event) => setSearchInput(event.target.value)}
                                placeholder="Search resource or issue"
                            />
                            <Button type="submit" variant="secondary" size="sm" aria-label="Apply search">
                                <Filter size={14} />
                            </Button>
                        </form>
                    </div>
                    {notice && <p className="mt-3 text-[11px] text-amber-600 dark:text-amber-300">{notice}</p>}
                    {error && (
                        <div className="mt-3 flex flex-wrap items-center gap-3 text-[11px] text-red-600 dark:text-red-300">
                            <span className="flex items-center gap-1.5">
                                <AlertCircle size={13} /> {error}
                            </span>
                            {scopeLost && (
                                <Button variant="secondary" size="xs" onClick={() => updateFilters({ clusterId: '' })}>
                                    Return to all accessible clusters
                                </Button>
                            )}
                        </div>
                    )}
                </Panel>
                {loading ? (
                    <LoadingState label="Loading issues" />
                ) : (
                    <IssueList
                        issues={items}
                        onNavigate={onNavigate}
                        expandable
                        expandedIds={expandedIds}
                        onToggle={toggleIssue}
                        emptyTitle={hasFilters ? 'No matching findings' : 'All systems operational'}
                        emptyMessage={
                            hasFilters
                                ? 'Adjust the filters to review other findings.'
                                : 'Health checks and telemetry across monitored clusters are reporting normally. No action is required.'
                        }
                    />
                )}
                {nextCursor && (
                    <div className="flex justify-center">
                        <Button variant="secondary" onClick={loadMore} isLoading={loadingMore}>
                            Load more issues
                        </Button>
                    </div>
                )}
                {!nextCursor && items.length > 0 && (
                    <p className="text-center text-[11px] text-theme-muted">End of current findings.</p>
                )}
            </div>
        </div>
    );
}
