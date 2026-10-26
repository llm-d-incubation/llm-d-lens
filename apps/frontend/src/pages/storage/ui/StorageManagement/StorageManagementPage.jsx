import { useDebouncedValue } from '../../../../shared/lib/useDebouncedValue';
import { cn } from '../../../../shared/lib/cn';
import { useNotice } from '../../../../shared/lib/useNotice';
import { paginate } from '../../../../shared/lib/pagination';
import { PaginationControls } from '../../../../shared/ui/PaginationControls';
import { useClusterNames } from '../../../optimization';
import { useResourceList } from '../../../../shared/lib/useResourceList';
import { AsyncState } from '../../../../shared/ui/AsyncState';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePolling } from '../../../../shared/lib/usePolling';
import { CheckCircle2, HardDrive, Info, PanelLeftClose, PanelLeftOpen, Plus, RotateCcw, Search, Server, ServerCog, Tags } from 'lucide-react';
import {
    createStorageVolume,
    acknowledgeStorageVolumeNodes,
    deleteStorageVolume,
    getStorageResourceStatuses,
    listStorageVolumes,
} from './storageManagementBackend';
import { EmptyState } from '../../../../shared/ui/EmptyState';
import { Button, RefreshButton } from '../../../../shared/ui/Button';
import { StatCardRow } from '../../../../shared/ui/StatCard';
import { Input, Select } from '../../../../shared/ui/FormControls';
import { SectionLabel } from '../../../../shared/ui/SectionLabel';
import { ModuleHeader } from '../../../../shared/ui/ModuleHeader';
import { ModulePage } from '../../../../shared/ui/ModulePage';
import { Panel } from '../../../../shared/ui/Panel';
import { StorageVolumeTable } from './StorageVolumeTable';
import { StorageResourceDetailPanel } from './StorageResourceDetailPanel';
import { CreateStorageVolumeModal } from './CreateStorageVolumeModal';
import { DeleteStorageVolumeModal } from './DeleteStorageVolumeModal';
import { PermissionGate } from '../../../auth';
import { PURPOSE_OPTIONS, STORAGE_KIND_OPTIONS, clusterOptions, kindLabel, purposeLabel, summarizeStatuses } from './storagePresentation';

const SEARCH_DEBOUNCE_MS = 300;
const DEFAULT_PAGE_SIZE = 10;
const RESOURCE_STATUS_POLL_INTERVAL_MS = 5000;

function StatCard({ label, value, tone = 'text-white', hint }) {
    return (
        <div className="flex h-full flex-col gap-2 rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 text-left shadow-lg backdrop-blur-xl">
            <div className="flex items-center gap-1 truncate text-xs font-medium text-slate-400">
                {label}
                {hint && (
                    <span className="inline-flex shrink-0 items-center" title={hint}>
                        <Info size={12} className="text-slate-500" aria-hidden="true" />
                        <span className="sr-only">{hint}</span>
                    </span>
                )}
            </div>
            <div className={`truncate text-3xl font-bold leading-none ${tone}`}>{value}</div>
        </div>
    );
}

export function StorageManagementPage({ initialClusterId = '', initialVolumeId = '', initialAction = '', initialPurpose = '' } = {}) {
    const [resourceStatuses, setResourceStatuses] = useState({});
    const [selectedResourceId, setSelectedResourceId] = useState(initialVolumeId || null);
    const [searchInput, setSearchInput] = useState('');
    const query = useDebouncedValue(searchInput, SEARCH_DEBOUNCE_MS).trim();
    const [kind, setKind] = useState('');
    const [purpose, setPurpose] = useState('');
    const [clusterId, setClusterId] = useState(initialClusterId || '');
    const [dialog, setDialog] = useState(null);
    const [createPurpose, setCreatePurpose] = useState('');
    const [selected, setSelected] = useState(null);
    const [mutatingId, setMutatingId] = useState(null);
    const [notice, setNotice] = useNotice();
    const [page, setPage] = useState(0);
    const [listOpen, setListOpen] = useState(true);
    const pageSize = DEFAULT_PAGE_SIZE;

    const statusControllerRef = useRef(null);
    const focusedVolumeId = useRef('');
    const handledInitialAction = useRef(false);
    // Cluster choices come from the unfiltered list so narrowing by cluster
    // never removes the option the user just picked.
    const [knownClusters, setKnownClusters] = useState([]);
    const { clusters, clusterNameById } = useClusterNames();


    const fetchItems = useCallback(({ signal }) => listStorageVolumes({ query, kind, purpose, clusterId, signal }),
        [query, kind, purpose, clusterId]);
    const rememberClusters = useCallback(results => {
        if (!query && !kind && !clusterId) setKnownClusters(clusterOptions(results));
    }, [query, kind, clusterId]);
    const { items, setItems, loading, refreshing, error, load } = useResourceList(fetchItems, {
        onLoaded: rememberClusters, errorFallback: 'Failed to load storage volumes',
    });

    const hasInFlightVolume = items.some((item) => item.status === 'pending' || item.status === 'deleting');
    usePolling(() => load({ quiet: true }), { intervalMs: RESOURCE_STATUS_POLL_INTERVAL_MS, enabled: hasInFlightVolume });

    useEffect(() => {
        setClusterId(initialClusterId || '');
    }, [initialClusterId]);

    useEffect(() => {
        if (initialAction !== 'create' || handledInitialAction.current) return;
        handledInitialAction.current = true;
        setCreatePurpose(initialPurpose);
        setDialog('create');
    }, [initialAction, initialPurpose]);

    useEffect(() => {
        if (!initialVolumeId || focusedVolumeId.current === initialVolumeId) return;
        const index = items.findIndex((item) => item.id === initialVolumeId);
        if (index >= 0) {
            focusedVolumeId.current = initialVolumeId;
            setPage(Math.floor(index / pageSize));
            setSelectedResourceId(initialVolumeId);
        }
    }, [initialVolumeId, items, pageSize]);

    useEffect(() => {
        const volumeIds = items.map((item) => item.id);
        if (!volumeIds.length) {
            setResourceStatuses({});
            return undefined;
        }
        let cancelled = false;
        const refreshResourceStatuses = async () => {
            statusControllerRef.current?.abort();
            const controller = new AbortController();
            statusControllerRef.current = controller;
            try {
                const statuses = await getStorageResourceStatuses(volumeIds, { signal: controller.signal });
                if (!cancelled) setResourceStatuses(statuses);
            } catch (failure) {
                if (failure?.name !== 'AbortError') {
                    // Preserve the most recently observed Kubernetes states.
                }
            }
        };
        refreshResourceStatuses();
        const timer = setInterval(refreshResourceStatuses, RESOURCE_STATUS_POLL_INTERVAL_MS);
        return () => {
            cancelled = true;
            clearInterval(timer);
            statusControllerRef.current?.abort();
        };
    }, [items]);

    useEffect(() => () => {
        statusControllerRef.current?.abort();
    }, []);


    const summary = useMemo(() => summarizeStatuses(items, resourceStatuses), [items, resourceStatuses]);
    const mergedClusters = useMemo(() => {
        const merged = new Set([...knownClusters, ...clusterOptions(items)]);
        if (clusterId) merged.add(clusterId);
        return [...merged].sort();
    }, [knownClusters, items, clusterId]);

    const hasFilters = Boolean(query || kind || purpose || clusterId);
    const clearFilters = () => {
        setSearchInput('');
        setKind('');
        setPurpose('');
        setClusterId('');
    };

    useEffect(() => {
        setPage(0);
    }, [query, kind, purpose, clusterId, items.length]);

    const { totalPages, currentPage, pagedItems } = useMemo(
        () => paginate(items, page, pageSize),
        [items, page, pageSize]
    );

    // Keep the side detail panel pointed at a row on the current page: default
    // to the first row, and fall back to it whenever the previous selection
    // scrolls out of view (filter change, deletion, pagination, ...).
    useEffect(() => {
        if (pagedItems.length === 0) {
            if (selectedResourceId !== null) setSelectedResourceId(null);
            return;
        }
        if (!pagedItems.some((item) => item.id === selectedResourceId)) {
            setSelectedResourceId(pagedItems[0].id);
        }
    }, [pagedItems, selectedResourceId]);

    const selectedResourceItem = pagedItems.find((item) => item.id === selectedResourceId) || null;

    const closeDialog = () => {
        setDialog(null);
        setSelected(null);
        setCreatePurpose('');
    };

    const openCreate = () => {
        setCreatePurpose('');
        setDialog('create');
    };

    const openDelete = (item) => {
        setSelected(item);
        setDialog('delete');
    };

    const handleCreate = async (payload) => {
        const created = await createStorageVolume(payload);
        setItems((current) => [created, ...current]);
        setNotice('Storage volume creation started');
        closeDialog();
    };

    const handleDelete = async (keepModelFiles) => {
        const volumeId = selected.id;
        setMutatingId(volumeId);
        try {
            const updated = await deleteStorageVolume(volumeId, { keepModelFiles });
            if (updated?.id) {
                setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
            } else {
                // Older backend still returning 204: the volume is already gone.
                setItems((current) => current.filter((item) => item.id !== volumeId));
            }
            setNotice('Storage volume deletion started');
            closeDialog();
        } finally {
            setMutatingId(null);
        }
    };

    const handleAcknowledgeNodes = async (item) => {
        setMutatingId(item.id);
        try {
            const updated = await acknowledgeStorageVolumeNodes(item.id);
            setItems((current) => current.map((entry) => (entry.id === updated.id ? updated : entry)));
            setNotice('New nodes acknowledged');
        } finally {
            setMutatingId(null);
        }
    };

    return (
        <ModulePage className="storage-page" contentClassName="flex flex-col gap-4 lg:h-[var(--workspace-height)] lg:pb-4">
            <ModuleHeader
                icon={HardDrive}
                title="Storage"
                description="Manage registered storage volumes for model cache, datasets, and deployment artifacts across clusters."
            />
            <StatCardRow
                actions={notice && (
                            <span role="status" className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-2.5 py-1.5 text-[11px] text-emerald-200">
                                <CheckCircle2 size={13} aria-hidden="true" />
                                {notice}
                            </span>
                )}
            >
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                    <StatCard label="Total" value={summary.total} />
                    <StatCard label="Ready" value={summary.ready} />
                    <StatCard
                        label="Pending"
                        value={summary.pending}
                        hint="Volume created; Kubernetes PV/PVC objects are still being provisioned in the background. Becomes Ready or Failed automatically once provisioning finishes."
                    />
                    <StatCard label="Attention" value={summary.attention} tone={summary.attention > 0 ? 'text-orange-300' : 'text-white'} />
                    <StatCard label="Failed" value={summary.failed} tone={summary.failed > 0 ? 'text-rose-300' : 'text-white'} />
                </div>
            </StatCardRow>

            <Panel
                padding="none"
                aria-busy={loading || refreshing}
                className="storage-card flex flex-col lg:min-h-[28rem] lg:flex-1"
            >
                <div className="shrink-0 space-y-3 border-b border-theme-border p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                            <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => setListOpen((open) => !open)}
                                aria-expanded={listOpen}
                                aria-controls="storage-volume-list"
                                aria-label={listOpen ? 'Collapse storage list' : 'Expand storage list'}
                                title={listOpen ? 'Collapse storage list' : 'Expand storage list'}
                            >
                                {listOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
                            </Button>
                            <SectionLabel className="mb-0 text-sm font-semibold normal-case tracking-normal">{`Storage volumes (${items.length})`}</SectionLabel>
                        </div>
                        <div className="ml-auto flex items-center gap-2">
                            <RefreshButton onClick={load} disabled={refreshing} refreshing={refreshing} label="Refresh storage volumes" />
                            <PermissionGate permission="storage:volume:create">
                                <Button variant="sky" size="sm" onClick={openCreate}>
                                    <Plus size={14} aria-hidden="true" /> New storage volume
                                </Button>
                            </PermissionGate>
                        </div>
                    </div>
                    <div className="grid items-center gap-2 sm:grid-cols-2 xl:grid-cols-[minmax(12rem,1fr)_11rem_9rem_10rem_auto]">
                        <Input
                            variant="toolbar"
                            icon={Search}
                            type="search"
                            value={searchInput}
                            onChange={(event) => setSearchInput(event.target.value)}
                            placeholder="Search by name"
                            aria-label="Search storage volumes"
                            className="h-9 min-w-0"
                        />
                        <Select
                            variant="toolbar"
                            icon={Server}
                            id="storage-cluster-filter"
                            aria-label="Filter by cluster"
                            value={clusterId}
                            onChange={(event) => setClusterId(event.target.value)}
                            className="h-9 min-w-0 text-xs"
                        >
                            <option value="">All clusters</option>
                            {mergedClusters.map((cluster) => (
                                <option key={cluster} value={cluster}>{clusterNameById[cluster] || cluster}</option>
                            ))}
                        </Select>
                        <Select
                            variant="toolbar"
                            icon={HardDrive}
                            value={kind}
                            onChange={(event) => setKind(event.target.value)}
                            aria-label="Filter by type"
                            className="h-9 min-w-0 text-xs"
                        >
                            <option value="">All types</option>
                            {STORAGE_KIND_OPTIONS.map((option) => (
                                <option key={option} value={option}>{kindLabel(option)}</option>
                            ))}
                        </Select>
                        <Select
                            variant="toolbar"
                            icon={Tags}
                            value={purpose}
                            onChange={(event) => setPurpose(event.target.value)}
                            aria-label="Filter by purpose"
                            className="h-9 min-w-0 text-xs"
                        >
                            <option value="">All purposes</option>
                            {PURPOSE_OPTIONS.map((option) => (
                                <option key={option} value={option}>{purposeLabel(option)}</option>
                            ))}
                        </Select>
                        {hasFilters && (
                            <Button variant="ghost" size="xs" onClick={clearFilters}>
                                <RotateCcw size={11} aria-hidden="true" /> Clear filters
                            </Button>
                        )}
                    </div>
                </div>
                <div className={cn('grid min-h-0 flex-1', listOpen && 'lg:grid-cols-[minmax(16rem,1fr)_minmax(0,2fr)]')}>
                <section id="storage-volume-list" aria-label="Storage volume list" hidden={!listOpen}
                    className={cn('min-h-0 min-w-0 flex-col border-b border-theme-border p-4 lg:border-b-0 lg:border-r', listOpen ? 'flex' : 'hidden')}>
                <div className="max-h-96 min-h-0 flex-1 overflow-y-auto lg:max-h-none">
                <AsyncState loading={loading} error={error} empty={items.length === 0}
                    loadingContent={(
                    <p className="px-4 py-12 text-center text-xs text-slate-500">Loading storage volumes…</p>
                    )}
                    errorContent={(
                    <EmptyState
                        icon={<ServerCog size={28} />}
                        title="Could not load storage volumes"
                        message={error}
                        action={<Button variant="secondary" onClick={load}>Try again</Button>}
                    />
                    )}
                    emptyContent={(
                    <EmptyState
                        icon={<HardDrive size={28} />}
                        title={hasFilters ? 'No storage volumes match your filters' : 'No storage volumes yet'}
                        message={
                            hasFilters
                                ? 'Adjust the search text, type or cluster filter to widen the results.'
                                : 'Register a hostPath, NFS share or dynamic PVC that Deployments can mount as a model cache.'
                        }
                        action={
                            hasFilters ? (
                                <Button
                                    variant="secondary"
                                    onClick={clearFilters}
                                >
                                    Clear filters
                                </Button>
                            ) : undefined
                        }
                    />
                    )}
                >

                    <StorageVolumeTable
                        items={pagedItems}
                        resourceStatuses={resourceStatuses}
                        mutatingId={mutatingId}
                        clusterNameById={clusterNameById}
                        onDelete={openDelete}
                        selectedId={selectedResourceId}
                        onSelectRow={setSelectedResourceId}
                    />
                </AsyncState>
                </div>
                {!loading && !error && totalPages > 1 && (
                    <div className="mt-3 flex shrink-0 items-center justify-center gap-2 border-t border-theme-border pt-3 text-xs text-theme-muted">
                            <PaginationControls page={currentPage} totalPages={totalPages} onPageChange={setPage} />
                    </div>
                )}
                </section>

            <section aria-label="Storage details" className="flex min-h-0 min-w-0 flex-col p-4">
                <SectionLabel className="shrink-0 text-sm font-semibold normal-case tracking-normal">Storage details</SectionLabel>
                <div className="mt-3 min-h-0 flex-1 overflow-y-auto pr-1">
                    <StorageResourceDetailPanel
                        item={selectedResourceItem}
                        resourceStatuses={resourceStatuses}
                        clusterNameById={clusterNameById}
                        mutatingId={mutatingId}
                        onAcknowledgeNodes={handleAcknowledgeNodes}
                    />
                </div>
            </section>
                </div>
            </Panel>

            {dialog === 'create' && (
                <CreateStorageVolumeModal
                    clusters={clusters}
                    defaultClusterId={clusterId}
                    defaultPurpose={createPurpose}
                    onCancel={closeDialog}
                    onCreate={handleCreate}
                />
            )}
            {dialog === 'delete' && <DeleteStorageVolumeModal volume={selected} onCancel={closeDialog} onDelete={handleDelete} />}
        </ModulePage>
    );
}

export default StorageManagementPage;
