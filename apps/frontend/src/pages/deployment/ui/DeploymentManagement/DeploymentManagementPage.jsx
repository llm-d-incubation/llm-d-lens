import { useDebouncedValue } from '../../../../shared/lib/useDebouncedValue';
import { useNotice } from '../../../../shared/lib/useNotice';
import { paginate } from '../../../../shared/lib/pagination';
import { AsyncState } from '../../../../shared/ui/AsyncState';
import { PaginationControls } from '../../../../shared/ui/PaginationControls';
import { useClusterNames } from '../../../optimization';
import { useResourceList } from '../../../../shared/lib/useResourceList';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { CheckCircle2, Plus, Rocket, Search, ServerCog } from 'lucide-react';
import {
    deleteDeploymentExecution,
    connectDeploymentExecution,
    listDeploymentExecutions,
    pendingDeploymentExecutions,
    searchLocalDeploymentRuns,
    updateDeploymentMetadata,
} from '../../../optimization';
import { manageDeploymentMonitoring } from '../../../clusters';
import { MultiSelectDropdown } from '../../../../shared/ui/MultiSelectDropdown';
import { EmptyState } from '../../../../shared/ui/EmptyState';
import { Button, RefreshButton } from '../../../../shared/ui/Button';
import { StatCardRow } from '../../../../shared/ui/StatCard';
import { Input, Select } from '../../../../shared/ui/FormControls';
import { SectionLabel } from '../../../../shared/ui/SectionLabel';
import { ModuleHeader } from '../../../../shared/ui/ModuleHeader';
import { ModulePage } from '../../../../shared/ui/ModulePage';
import { DeploymentTable } from './DeploymentTable';
import { DeploymentDetailsModal } from './DeploymentDetailsModal';
import { EditDeploymentModal } from './EditDeploymentModal';
import { DeleteDeploymentModal } from './DeleteDeploymentModal';
import { DeploymentPodsLogsModal } from './DeploymentPodsLogsModal';
import { useAuth } from '../../../auth';
import { usePolling } from '../../../../shared/lib/usePolling';
import {
    STATUS_FILTER_OPTIONS,
    clusterOptions,
    errorMessage,
    statusLabel,
    summarizeStatuses,
} from './deploymentPresentation';

const SEARCH_DEBOUNCE_MS = 300;
const DEPLOYMENT_REFRESH_INTERVAL_MS = 5000;
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

function StatCard({ label, value, tone = 'text-white' }) {
    return (
        <div className="flex h-full flex-col gap-2 rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 text-left shadow-lg backdrop-blur-xl">
            <div className="truncate text-xs font-medium text-slate-400">{label}</div>
            <div className={`truncate text-3xl font-bold leading-none ${tone}`}>{value}</div>
        </div>
    );
}

export function DeploymentManagementPage({ onNavigate, initialSearch = '', initialClusterId = '' } = {}) {
    const [searchInput, setSearchInput] = useState(initialSearch);
    const query = useDebouncedValue(searchInput, SEARCH_DEBOUNCE_MS).trim();
    const [statuses, setStatuses] = useState(new Set());
    const [clusterId, setClusterId] = useState(initialClusterId);
    const [dialog, setDialog] = useState(null);
    const [selected, setSelected] = useState(null);
    const { can } = useAuth();
    const [mutatingId, setMutatingId] = useState(null);
    const [connectingEndpointId, setConnectingEndpointId] = useState('');
    const [notice, setNotice] = useNotice();
    const [page, setPage] = useState(0);
    const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

    // Cluster choices come from the unfiltered list so narrowing by cluster
    // never removes the option the user just picked.
    const [knownClusters, setKnownClusters] = useState([]);
    const { clusterNameById } = useClusterNames();


    const fetchItems = useCallback(async ({ signal }) => {
        const executions = await listDeploymentExecutions({ query, statuses: [...statuses], clusterId, signal });
        signal.throwIfAborted();
        const runs = await searchLocalDeploymentRuns(query, { signal });
        const pending = pendingDeploymentExecutions(runs, new Set(executions.map(item => item.execution_id)))
            .filter(item => !clusterId || item.cluster_id === clusterId)
            .filter(item => statuses.size === 0 || statuses.has(item.status));
        return [...pending, ...executions];
    }, [query, statuses, clusterId]);
    const rememberClusters = useCallback(results => {
        if (!query && statuses.size === 0 && !clusterId) setKnownClusters(clusterOptions(results));
    }, [query, statuses, clusterId]);
    const { items, setItems, loading, refreshing, error, setError, load } = useResourceList(fetchItems, {
        onLoaded: rememberClusters, errorFallback: 'Failed to load deployments',
    });
    usePolling(() => load({ quiet: true }), { intervalMs: DEPLOYMENT_REFRESH_INTERVAL_MS });




    const summary = useMemo(() => summarizeStatuses(items), [items]);
    const clusters = useMemo(() => {
        const merged = new Set([...knownClusters, ...clusterOptions(items)]);
        if (clusterId) merged.add(clusterId);
        return [...merged].sort();
    }, [knownClusters, items, clusterId]);

    const hasFilters = Boolean(query || statuses.size || clusterId);

    useEffect(() => {
        setPage(0);
    }, [query, statuses, clusterId, items.length]);

    const { totalPages, currentPage, pageStart, pageEnd, pagedItems } = useMemo(
        () => paginate(items, page, pageSize),
        [items, page, pageSize]
    );

    const closeDialog = () => {
        setDialog(null);
        setSelected(null);
    };

    const openDialog = (kind) => (item) => {
        setSelected(item);
        setDialog(kind);
    };

    const handleSave = async ({ displayName, description }) => {
        if (selected.pending) return;
        const executionId = selected.execution_id;
        setMutatingId(executionId);
        try {
            const updated = await updateDeploymentMetadata(executionId, { displayName, description });
            setItems((current) => current.map((item) => (item.execution_id === executionId ? updated : item)));
            setNotice('Deployment updated');
            closeDialog();
        } finally {
            setMutatingId(null);
        }
    };

    const handleDelete = async ({ deleteNamespace = false } = {}) => {
        if (selected.pending) return;
        const executionId = selected.execution_id;
        setMutatingId(executionId);
        try {
            try {
                await manageDeploymentMonitoring(executionId, 'uninstall', { clusterId: selected.cluster_id });
            } catch (monitoringError) {
                // Monitoring may not be installed for this deployment; don't block deletion on it.
                console.warn('Failed to uninstall deployment monitoring before delete', monitoringError);
            }
            await deleteDeploymentExecution(executionId, { deleteNamespace, runId: selected.run_id });
            setItems((current) => current.filter((item) => item.execution_id !== executionId));
            setNotice('Deployment deleted');
            closeDialog();
        } finally {
            setMutatingId(null);
        }
    };

    const handleConnectLocalEndpoint = async (item) => {
        if (!item?.execution_id) return;
        setConnectingEndpointId(item.execution_id);
        setError('');
        try {
            const result = await connectDeploymentExecution(item.execution_id);
            const forwarded = result?.forwarded_endpoint || result?.endpoint || '';
            if (forwarded) {
                setItems((current) => current.map((deployment) => (
                    deployment.execution_id === item.execution_id
                        ? { ...deployment, forwarded_endpoint: forwarded }
                        : deployment
                )));
                setNotice('Local endpoint connected');
            }
        } catch (failure) {
            setError(errorMessage(failure, 'Unable to connect local endpoint'));
        } finally {
            setConnectingEndpointId('');
        }
    };

    const toggleStatus = (value) => {
        // MultiSelectDropdown emits '' for the "All" entry.
        if (!value) {
            setStatuses(new Set());
            return;
        }
        setStatuses((current) => {
            const next = new Set(current);
            if (next.has(value)) next.delete(value);
            else next.add(value);
            return next;
        });
    };

    return (
        <ModulePage className="deployment-page" contentClassName="flex flex-col gap-4">
            <ModuleHeader
                icon={Rocket}
                title="Deployments"
                description="Track active model serving deployments, inspect endpoints, and update or remove existing services."
            />
            <StatCardRow
                actions={(
                    <>
                        {notice && (
                            <span role="status" className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-2.5 py-1.5 text-[11px] text-emerald-200">
                                <CheckCircle2 size={13} aria-hidden="true" />
                                {notice}
                            </span>
                        )}
                        <RefreshButton onClick={load} disabled={refreshing} refreshing={refreshing} label="Refresh deployments" />
                        {can('deployment:run:create') && (
                            <Button variant="sky" size="sm" onClick={() => onNavigate?.('model-market')}>
                                <Plus size={14} /> Create
                            </Button>
                        )}
                    </>
                )}
            >
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                    <StatCard label="Total" value={summary.total} />
                    <StatCard label="Ready" value={summary.Ready} />
                    <StatCard label="In progress" value={summary['In progress']} />
                    <StatCard label="Failed" value={summary.Failed} tone={summary.Failed > 0 ? 'text-rose-500 font-extrabold' : 'text-white'} />
                    <StatCard label="Cleaned" value={summary.Cleaned} />
                </div>
            </StatCardRow>

            <section
                aria-busy={loading || refreshing}
                className="relative rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/90 via-slate-900/50 to-slate-950/90 shadow-2xl backdrop-blur-xl"
            >
              <div className="relative overflow-visible p-6">
                <SectionLabel className="text-[#037ffc] dark:text-cyan-400">{`Deployments (${items.length})`}</SectionLabel>
                <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-[1fr_220px_220px]">
                    <div className="relative">
                        <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" aria-hidden="true" />
                        <Input
                            type="search"
                            value={searchInput}
                            onChange={(event) => setSearchInput(event.target.value)}
                            placeholder="Search by name, description, model, namespace, guide or ID"
                            aria-label="Search deployments"
                            className="h-10 py-2 pl-9 text-xs"
                        />
                    </div>
                    <MultiSelectDropdown
                        label="Status"
                        options={STATUS_FILTER_OPTIONS}
                        selected={statuses}
                        onChange={toggleStatus}
                        formatLabel={statusLabel}
                        className="h-10"
                    />
                    <select
                        value={clusterId}
                        onChange={(event) => setClusterId(event.target.value)}
                        aria-label="Filter by cluster"
                        className="h-10 w-full rounded-xl border border-slate-800/40 bg-[#0b0f17] px-3 text-xs text-slate-300 outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/10"
                    >
                        <option value="">All clusters</option>
                        {clusters.map((cluster) => (
                            <option key={cluster} value={cluster}>{clusterNameById[cluster] || cluster}</option>
                        ))}
                    </select>
                </div>
                <div className="mt-3">
                <AsyncState loading={loading} error={error} empty={items.length === 0}
                    loadingContent={(
                    <p className="px-4 py-12 text-center text-xs text-slate-500">Loading deployments…</p>
                    )}
                    errorContent={(
                    <EmptyState
                        icon={<ServerCog size={28} />}
                        title="Could not load deployments"
                        message={error}
                        action={<Button variant="secondary" onClick={load}>Try again</Button>}
                    />
                    )}
                    emptyContent={(
                    <EmptyState
                        icon={<Rocket size={28} />}
                        iconClassName="deployments-empty-icon"
                        title={hasFilters ? 'No deployments match your filters' : 'No deployments yet'}
                        message={
                            hasFilters
                                ? 'Adjust the search text, status or cluster filter to widen the results.'
                                : 'Deployments created from the Evaluation workspace will appear here.'
                        }
                        action={
                            hasFilters ? (
                                <Button
                                    variant="secondary"
                                    onClick={() => {
                                        setSearchInput('');
                                        setStatuses(new Set());
                                        setClusterId('');
                                    }}
                                >
                                    Clear filters
                                </Button>
                            ) : undefined
                        }
                    />
                    )}
                >

                    <>
                    <DeploymentTable
                        items={pagedItems}
                        mutatingId={mutatingId}
                        connectingEndpointId={connectingEndpointId}
                        clusterNameById={clusterNameById}
                        onInspect={openDialog('details')}
                        onViewPodsLogs={openDialog('pods-logs')}
                        onEdit={openDialog('edit')}
                        onDelete={openDialog('delete')}
                        onNavigate={onNavigate}
                        onConnectLocalEndpoint={handleConnectLocalEndpoint}
                    />
                    <PaginationControls className="mt-2" page={currentPage} totalPages={totalPages} onPageChange={setPage}
                        total={items.length} pageStart={pageStart} pageEnd={pageEnd} itemLabel="deployments"
                        pageSize={pageSize} pageSizeOptions={PAGE_SIZE_OPTIONS} onPageSizeChange={setPageSize} pageSizeId="deployments-page-size" />
                    </>
                </AsyncState>
                </div>
              </div>
            </section>

            {dialog === 'details' && <DeploymentDetailsModal deployment={selected} clusterNameById={clusterNameById} onClose={closeDialog} onViewPodsLogs={openDialog('pods-logs')} />}
            {dialog === 'edit' && <EditDeploymentModal deployment={selected} onCancel={closeDialog} onSave={handleSave} />}
            {dialog === 'delete' && <DeleteDeploymentModal deployment={selected} clusterNameById={clusterNameById} onCancel={closeDialog} onDelete={handleDelete} />}
            {dialog === 'pods-logs' && <DeploymentPodsLogsModal deployment={selected} onClose={closeDialog} />}
        </ModulePage>
    );
}

export default DeploymentManagementPage;
