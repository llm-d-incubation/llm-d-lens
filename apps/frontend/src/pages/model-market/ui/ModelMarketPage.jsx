import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Box, CheckCircle2, ChevronDown, Database, HardDrive, Layers, Network, Rocket, Search, Server, SlidersHorizontal, Sparkles, Tag, Wrench } from 'lucide-react';
import { MODELS, defaultServing, filterCatalogModels } from '@repo/shared/catalog';
import { useModelCatalog } from '../../../shared/lib/useModelCatalog';
import CatalogModelEditor from './CatalogModelEditor';
import ModelParametersForm from './ModelParametersForm';
import { requestJson } from '../../../shared/api/httpClient';
import { usePolling } from '../../../shared/lib/usePolling';
import { Badge, Button, EmptyState, Input, ModuleHeader, ModulePage, Panel, Select, Spinner, StorageVolumeSelect, Textarea, ToggleGroup } from '../../../shared/ui';
import { cn } from '../../../shared/lib/cn';
import { PickerSelect } from '../../../shared/ui/FormControls';
import { FormError } from '../../../shared/ui/FormError';
import { listStorageVolumes } from '../../storage';
import { listModelCacheEntries } from '../../model-cache';
import { AgenticDeploymentWorkspace, HIGHLIGHT_DEPLOYMENT_KEY, ManualDeploymentWorkspace, OptimizationChecklist, previewOptimizations, servingArguments } from '../../deployment';
import { useAuth } from '../../auth';
import { listAIProviders } from '../../ai-providers';
import { consumePendingModelMarketCluster } from '../model/transfer';
import { standardRecommendation } from '../model/standardRecommendation';
import PodPlacementControl from '../../../shared/ui/PodPlacementControl';
import { listDeploymentNodeLabels } from '../../optimization/ui/OptimizationWorkspace/remoteDeployBackend';

function SetupField({ icon, label, children }) {
    return <div className="deployment-setup-field"><span className="deployment-section-label">{icon}{label}</span>{children}</div>;
}

function WorkloadGroup({ title, hint, children }) {
    return <div className="deployment-workload-group space-y-3">
        <div><p className="text-sm font-medium text-theme-text">{title}</p>{hint && <p className="mt-0.5 text-xs text-theme-muted">{hint}</p>}</div>
        {children}
    </div>;
}

function OptionSummary({ icon, title, detail }) {
    return <summary>
        <span className="deployment-workload-icon">{icon}</span>
        <span className="min-w-0 flex-1"><span className="block text-sm font-medium text-theme-text">{title}</span><span className="block truncate text-xs text-theme-muted">{detail}</span></span>
        <span className="text-xs font-medium text-[var(--brand-accent)]">Customize</span><ChevronDown size={15} className="deployment-workload-chevron" aria-hidden="true" />
    </summary>;
}

const OBJECTIVES = [{ value: 'balanced', label: 'Balanced' }, { value: 'latency', label: 'Low latency' }, { value: 'throughput', label: 'High throughput' }];
const OBJECTIVE_HINTS = {
    balanced: 'Uses the fewest accelerators that fit the model.',
    latency: 'Prefers wider tensor parallelism and may use every free accelerator within the limit.',
    throughput: 'Prefers more serving replicas; a concurrency target sizes the replica count.',
};

function deploymentName(model) {
    const suffix = crypto.randomUUID().slice(0, 8);
    return `${String(model.id || 'model').slice(0, 54)}-${suffix}`;
}

export function ModelCard({ model, onSelect }) {
    return (
        <Button type="button" variant="catalogCard" onClick={() => onSelect(model)}
            className="group flex h-auto min-h-52 flex-col items-stretch justify-start gap-0 whitespace-normal rounded-lg p-5 text-left font-normal">
            <div className="flex items-start justify-between gap-4">
                <div>
                    <p className="text-xs font-medium uppercase text-[var(--brand-accent)] dark:text-cyan-300">{model.family}</p>
                    <h2 className="mt-2 text-lg font-semibold text-slate-900 dark:text-white">{model.name}</h2>
                </div>
                <Box size={19} className="shrink-0 text-[var(--brand-accent)] transition dark:text-slate-500 dark:group-hover:text-cyan-300" aria-hidden="true" />
            </div>
            <dl className="mt-5 grid grid-cols-2 gap-x-3 gap-y-4 text-xs">
                <div><dt className="text-slate-500">Released</dt><dd className="mt-1 text-slate-700 dark:text-slate-200">{model.releaseDate || 'Not listed'}</dd></div>
                <div><dt className="text-slate-500">Precision</dt><dd className="mt-1 text-slate-700 dark:text-slate-200">{model.dtype}</dd></div>
                <div><dt className="text-slate-500">Weights</dt><dd className="mt-1 text-slate-700 dark:text-slate-200">{model.weightGiB ? `${model.weightGiB} GiB` : 'Not listed'}</dd></div>
                <div><dt className="text-slate-500">Context</dt><dd className="mt-1 text-slate-700 dark:text-slate-200">{model.contextLength ? model.contextLength.toLocaleString() : 'Not listed'}</dd></div>
                <div className="col-span-2"><dt className="text-slate-500">Model type</dt><dd className="mt-1 text-slate-700 dark:text-slate-200">{model.modelType}</dd></div>
            </dl>
        </Button>
    );
}

export default function ModelMarketPage({ onNavigate, isActive = true }) {
    const { can } = useAuth();
    const catalog = useModelCatalog(isActive);
    const [editingModel, setEditingModel] = useState(undefined);
    const [selected, setSelected] = useState(() => {
        const saved = sessionStorage.getItem('prism_model_market_return_draft');
        if (!saved) return null;
        try { return MODELS.find(model => model.repository === JSON.parse(saved)?.model) || null; }
        catch { return null; }
    });
    const [category, setCategory] = useState('All'), [search, setSearch] = useState('');
    const [publisher, setPublisher] = useState(''), [sort, setSort] = useState('newest');
    const [name, setName] = useState(''), [description, setDescription] = useState('');
    const [cluster, setCluster] = useState(''), [clusters, setClusters] = useState([]);
    const [storage, setStorage] = useState(''), [volumes, setVolumes] = useState([]);
    const [loadingStorage, setLoadingStorage] = useState(false), [cacheReady, setCacheReady] = useState(false);
    const [checkingCache, setCheckingCache] = useState(false), [error, setError] = useState('');
    const [mode, setMode] = useState('manual'), [draft, setDraft] = useState(null);
    const [objective, setObjective] = useState('balanced');
    const [placement, setPlacement] = useState('prefer-same-node'), [placementNode, setPlacementNode] = useState('');
    const [workload, setWorkload] = useState({ contextLength: '', maxAccelerators: '', concurrency: '', requestRate: '', inputTokens: '', outputTokens: '', ttftMs: '', tpotMs: '' });
    const [loadTarget, setLoadTarget] = useState('concurrency');
    const [serving, setServing] = useState(() => defaultServing(selected));
    const [planner, setPlanner] = useState({ provider: '', prompt: '' }), [providers, setProviders] = useState([]);
    const [forbidden, setForbidden] = useState([]), [choices, setChoices] = useState([]);
    const [previousPlans, setPreviousPlans] = useState([]);
    const [pendingCluster] = useState(() => consumePendingModelMarketCluster());
    const [hardware, setHardware] = useState(null);
    const cacheRequest = useRef(0);
    useEffect(() => {
        if (!selected) return;
        const saved = sessionStorage.getItem('prism_model_market_return_draft_values');
        sessionStorage.removeItem('prism_model_market_return_draft');
        sessionStorage.removeItem('prism_model_market_return_draft_values');
        if (!saved) { setName(deploymentName(selected)); return; }
        try {
            const values = JSON.parse(saved);
            setName(values.deployName || deploymentName(selected));
            setDescription(values.description || '');
            setCluster(values.cluster || '');
        } catch { setError('The saved model draft could not be restored. Choose deployment requirements again.'); }
    }, [selected]);
    const loadClusters = useCallback(async () => {
        try {
            const payload = await requestJson('/api/cluster/clusters');
            const items = payload.items || [];
            setClusters(items);
            setCluster(current => current || (items.some(item => item.id === pendingCluster) ? pendingCluster : items.find(item => item.ready)?.id) || '');
        } catch (failure) { setError(failure.message); }
    }, [pendingCluster]);
    useEffect(() => { if (isActive) loadClusters(); }, [loadClusters, isActive]);
    useEffect(() => {
        if (!isActive) return undefined;
        let active = true;
        listAIProviders().then(items => {
            if (!active) return;
            setProviders(items);
            setPlanner(current => (current.provider || !items.length ? current : { ...current, provider: items[0].id }));
        }).catch(failure => { if (active) setError(`Unable to load AI providers: ${failure.message}`); });
        return () => { active = false; };
    }, [isActive]);
    useEffect(() => {
        if (!isActive || !selected || mode !== 'manual') return undefined;
        let active = true;
        const timer = setTimeout(() => {
            previewOptimizations({
                model: selected.repository, objective, maxAccelerators: workload.maxAccelerators,
                inputTokens: workload.inputTokens, outputTokens: workload.outputTokens,
                forbiddenFeatures: forbidden, availableAccelerators: hardware?.usableGpuCount,
            }).then(items => { if (active) setChoices(items); }).catch(() => { if (active) setChoices([]); });
        }, 300);
        return () => { active = false; clearTimeout(timer); };
    }, [isActive, selected, mode, objective, workload.maxAccelerators, workload.inputTokens, workload.outputTokens, forbidden, hardware?.usableGpuCount]);
    usePolling(loadClusters, { enabled: isActive });
    useEffect(() => {
        let active = true;
        setHardware(null); setVolumes([]); setStorage(''); setCacheReady(false); setPlacementNode('');
        if (!cluster || !isActive) return undefined;
        setLoadingStorage(true);
        requestJson(`/api/cluster/overview?clusterId=${encodeURIComponent(cluster)}`)
            .then(overview => { if (active) setHardware(overview.kubernetes?.hardware || null); })
            .catch(failure => { if (active) setError(failure.message); });
        listStorageVolumes({ clusterId: cluster, purpose: 'model-cache' }).then(items => {
            if (!active) return;
            setVolumes(items);
            setStorage(items.find(item => item.status === 'ready')?.id || '');
        }).catch(failure => { if (active) setError(failure.message); })
            .finally(() => { if (active) setLoadingStorage(false); });
        return () => { active = false; };
    }, [cluster, isActive]);
    const checkCache = useCallback(async () => {
        const request = ++cacheRequest.current;
        if (!selected || !storage || !cluster) { setCacheReady(false); return; }
        setCheckingCache(true);
        try {
            const entries = await listModelCacheEntries({ clusterId: cluster, storageVolumeId: storage });
            if (request === cacheRequest.current) setCacheReady(entries.some(entry => entry.status === 'ready' && entry.source?.kind === 'huggingface' && String(entry.source.huggingface.repoId || '').toLowerCase() === selected.repository.toLowerCase() && (entry.source.huggingface.revision || 'main') === selected.revision));
        } catch (failure) { if (request === cacheRequest.current) { setCacheReady(false); setError(failure.message); } }
        finally { if (request === cacheRequest.current) setCheckingCache(false); }
    }, [selected, storage, cluster]);
    useEffect(() => { setCacheReady(false); if (isActive) checkCache(); return () => { cacheRequest.current += 1; }; }, [checkCache, isActive]);
    usePolling(checkCache, { enabled: isActive && Boolean(selected && storage && cluster), intervalMs: 10000 });
    const choose = model => { setSelected(model); setDescription(''); setDraft(null); setMode('manual'); setPreviousPlans([]); setServing(defaultServing(model)); setForbidden([]); };
    const begin = event => {
        event.preventDefault(); setError('');
        if (!cacheReady) { setError('The selected model must be ready in the selected model cache.'); return; }
        const invalid = Object.entries(workload).find(([key, value]) => value !== '' && (key === 'requestRate' ? !(Number(value) > 0) : !/^[1-9]\d*$/.test(String(value))));
        if (invalid) { setError(`${invalid[0]} must be a positive ${invalid[0] === 'requestRate' ? '' : 'whole '}number.`); return; }
        if (mode === 'agentic' && !planner.provider) { setError('Agentic planning requires an AI provider. Add one under AI Providers, or use Manual.'); return; }
        const plannerPrompt = mode === 'agentic' ? planner.prompt.trim() : '';
        let vllmArguments;
        try { vllmArguments = servingArguments(serving, selected.vllmArguments || []); }
        catch (failure) { setError(failure.message); return; }
        const defaults = standardRecommendation(selected, hardware);
        const { contextLength, ...limits } = workload;
        setDraft({
            modelCatalogId: selected.id, modelCatalogVersion: selected.version, modelRevision: selected.revision, servingOverrides: { ...serving },
            model: selected.repository, modelTitle: selected.name, modelWeightGiB: selected.weightGiB || selected.sizeGiB,
            deploymentName: name, description, initialObjective: objective, clusterId: cluster, clusterName: clusters.find(item => item.id === cluster)?.name, storageVolumeId: storage, storageVolumeName: volumes.find(item => item.id === storage)?.name,
            contextLength: contextLength ? Number(contextLength) : defaults.maxModelLen, gpuMemoryUtilization: 0.9,
            maxNumSeqs: 256, maxNumBatchedTokens: 8192, vllmArguments,
            placementReplicas: placement, placementNodeLabel: placement === 'same-node' && placementNode ? { key: 'kubernetes.io/hostname', value: placementNode } : null,
            initialProvider: mode === 'agentic' ? planner.provider : '',
            initialRequirements: {
                ...Object.fromEntries(Object.entries(limits).filter(([, value]) => value !== '')),
                ...(mode === 'agentic' ? { plannerPrompt } : { forbiddenFeatures: forbidden }),
            },
        });
    };
    const publishers = [...new Set(catalog.models.filter(model => category === 'All' || model.category === category).map(model => model.family))].sort((a, b) => a.localeCompare(b));
    const models = filterCatalogModels(catalog.models, { category, publisher, search, sort });
    if (!selected) return <ModulePage contentClassName="flex flex-col gap-4 py-4 lg:py-5">
        <ModuleHeader icon={Database} title="Model Catalog" actions={can('model-catalog:entry:create') ? <Button onClick={() => setEditingModel(null)}>Add custom model</Button> : undefined} />
        <FormError message={catalog.error} />
        {editingModel !== undefined && <CatalogModelEditor model={editingModel} canDelete={can('model-catalog:entry:delete')} onClose={() => setEditingModel(undefined)} onSaved={() => { setEditingModel(undefined); catalog.reload(); }} />}
        <p className="text-sm text-theme-muted">Choose a model to deploy.</p>
        <div className="flex flex-wrap items-center gap-3">
            <div className="flex min-w-0 w-full flex-wrap gap-2 lg:w-auto lg:flex-1" role="tablist" aria-label="Model categories">{catalog.categories.map(item => <Button type="button" key={item} role="tab" size="sm" variant="catalogTab" className="h-auto rounded-md px-3 py-1.5 font-normal" aria-selected={category === item} onClick={() => { setCategory(item); setPublisher(''); }}>{item}</Button>)}</div>
            <div className="ml-auto flex max-w-full flex-wrap items-center gap-2">
                <label className="text-xs text-theme-muted"><Select aria-label="Filter by publisher" value={publisher} onChange={event => setPublisher(event.target.value)}><option value="">All publishers</option>{publishers.map(value => <option key={value} value={value}>{value}</option>)}</Select></label>
                <label className="text-xs text-theme-muted"><Select aria-label="Sort models" value={sort} onChange={event => setSort(event.target.value)}><option value="newest">Release date: newest first</option><option value="oldest">Release date: oldest first</option><option value="name">Name: A–Z</option></Select></label>
                <span role="status" className="text-xs text-theme-muted">{models.length} models</span>
                <div className="w-52 sm:w-64">
                    <Input type="search" icon={Search} aria-label="Search models" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search models, publishers, repositories, types, or precision..." />
                </div>
                {(search || publisher || category !== 'All') && <Button type="button" size="sm" variant="secondary" onClick={() => { setSearch(''); setPublisher(''); setCategory('All'); }}>Clear filters</Button>}
            </div>
        </div>
        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">{models.map(model => <div key={model.id} className="flex flex-col gap-2"><ModelCard model={model} onSelect={choose} />
            <div className="flex items-center justify-between gap-2"><Badge tone="neutral">{model.source === 'custom' ? 'Custom' : 'Built-in'}</Badge>
            {(model.source === 'custom' ? can('model-catalog:entry:update') : can('model-catalog:entry:create')) && <Button size="xs" variant="ghost" onClick={() => setEditingModel(model)}>{model.source === 'custom' ? 'Edit defaults' : 'Copy & customize'}</Button>}</div></div>)}{!models.length && <EmptyState className="col-span-full" icon={<Search size={22} aria-hidden="true" />} title="No matching models" message="Try another search, publisher, or model category." />}</section>
    </ModulePage>;
    const allowed = can('deployment:run:create');
    // Swap with the current plan so users can move back and forth without losing either.
    const restorePlan = run => {
        const previous = previousPlans.at(-1);
        if (!previous) return;
        setPreviousPlans(current => [...current.slice(0, -1), ...(draft && run?.id ? [{ mode, draft: { ...draft, resumeRunId: run.id } }] : [])]);
        setMode(previous.mode); setDraft(previous.draft);
    };
    const workloadField = (key, label, placeholder) => <label key={key} className="block text-xs font-medium text-theme-text">{label}<Input className="mt-1.5" type="number" min={key === 'requestRate' ? '0.01' : '1'} step={key === 'requestRate' ? 'any' : '1'} value={workload[key]} placeholder={placeholder} onChange={event => setWorkload(current => ({ ...current, [key]: event.target.value }))} /></label>;
    const customCount = Object.values(workload).filter(Boolean).length;
    let toolSummary = 'Check model parameters';
    try {
        const effective = new Map(servingArguments(serving).map(item => [item.name, item.value]));
        toolSummary = effective.get('enable-auto-tool-choice') === 'true' ? `Tool calling on · ${effective.get('tool-call-parser')}` : 'Tool calling off';
    } catch { /* Invalid settings are reported when the user generates a plan. */ }
    const restoreLabel = previousPlans.length ? `Back to ${previousPlans.at(-1).mode === 'manual' ? 'Manual' : 'Agentic'} plan` : '';
    const Workspace = mode === 'manual' ? ManualDeploymentWorkspace : AgenticDeploymentWorkspace;
    return <ModulePage contentClassName="mx-auto flex max-w-7xl flex-col gap-5">
        {!draft && <ModuleHeader icon={Rocket} title="Create deployment" />}
        {!draft && <Button className="self-start" variant="ghost" onClick={() => { setSelected(null); setDraft(null); setPreviousPlans([]); }}><ArrowLeft size={15} />Model Catalog</Button>}
        {!allowed ? <EmptyState title="Your role cannot create deployments" message="Use an existing deployment or request deployment-create permission." action={<Button onClick={() => onNavigate('optimization-deployments')}>Open Deployments</Button>} /> : draft ? <>
            <Workspace key={`${mode}:${draft.clusterId}:${draft.model}:${draft.deploymentName}:${draft.planningAttemptId || 'initial'}`} mode={mode} embedded draft={draft} onBack={run => { if (run?.id) setPreviousPlans(current => [...current, { mode, draft: { ...draft, resumeRunId: run.id } }]); setDraft(null); }}
                onSwitchToManual={mode === 'agentic' ? () => { setMode('manual'); setDraft({ ...draft, initialProvider: '', resumeRunId: undefined, planningAttemptId: crypto.randomUUID() }); } : undefined}
                onRestorePlan={previousPlans.length ? restorePlan : undefined} restoreLabel={restoreLabel} onDeploymentStarted={run => { sessionStorage.setItem(HIGHLIGHT_DEPLOYMENT_KEY, run?.request?.deployment_name || draft.deploymentName); onNavigate('optimization-deployments'); }} />
        </> : <form onSubmit={begin} onInvalid={event => event.target.closest('details')?.setAttribute('open', '')} className="space-y-5">
            {previousPlans.length > 0 && <Panel padding="sm" tone="soft" className="flex flex-wrap items-center justify-between gap-3 shadow-none"><span className="text-sm text-theme-muted">Your {previousPlans.at(-1).mode === 'manual' ? 'Manual' : 'Agentic'} plan is saved.</span><Button type="button" size="sm" variant="secondary" onClick={() => restorePlan()}><ArrowLeft size={14} />{restoreLabel}</Button></Panel>}
            <Panel id="deployment-target" padding="none" tone="soft">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-theme-border px-5 py-4">
                    <div className="flex min-w-0 items-center gap-3"><span className="deployment-mode-icon"><Box size={20} /></span><div className="min-w-0"><p className="text-xs font-semibold uppercase tracking-wider text-theme-muted">{[selected.family, selected.dtype, selected.weightGiB && `${selected.weightGiB} GiB`].filter(Boolean).join(' · ')}</p><h2 className="truncate text-xl font-semibold tracking-tight">{selected.name}</h2></div></div>
                    <span role="status">{!cluster ? null : loadingStorage || (checkingCache && !cacheReady) ? <Badge tone="info" size="md"><Spinner size="sm" />Checking model cache</Badge>
                        : cacheReady ? <Badge tone="success" size="md"><CheckCircle2 size={13} />Model cache ready</Badge> : <Badge tone="warning" size="md">Model cache not ready</Badge>}</span>
                </div>
                <div className="grid gap-4 p-5 sm:grid-cols-2 xl:grid-cols-4">
                    <SetupField icon={<Server size={13} aria-hidden="true" />} label="Cluster"><PickerSelect aria-label="Cluster" value={cluster} onChange={setCluster} placeholder={clusters.length ? 'Select cluster' : 'No cluster connected'} options={clusters.map(item => ({ value: item.id, label: item.name, detail: item.ready ? undefined : 'Not ready', disabled: !item.ready }))} /></SetupField>
                    <SetupField icon={<Tag size={13} aria-hidden="true" />} label="Deployment name"><Input aria-label="Deployment name" required value={name} onChange={event => setName(event.target.value)} /></SetupField>
                    <SetupField icon={<HardDrive size={13} aria-hidden="true" />} label="Model cache">{cluster ? <StorageVolumeSelect value={storage} onChange={setStorage} volumes={volumes} loading={loadingStorage} placeholder="Select model cache" /> : <p className="deployment-setup-value text-theme-muted">Select a cluster first</p>}</SetupField>
                    <SetupField icon={<Network size={13} aria-hidden="true" />} label="Pod placement"><PodPlacementControl inline value={placement} onChange={setPlacement} clusterId={cluster} nodeName={placementNode} onNodeChange={setPlacementNode} listNodeLabels={listDeploymentNodeLabels} /></SetupField>
                </div>
                {!clusters.length && <div className="flex flex-wrap items-center justify-between gap-3 border-t border-theme-border px-5 py-3 text-sm"><span className="text-theme-muted">Connect a cluster to deploy.</span><Button type="button" size="sm" variant="secondary" onClick={() => onNavigate('clusters')}>Connect cluster<ArrowRight size={14} /></Button></div>}
                {cluster && !cacheReady && !loadingStorage && !checkingCache && <div className="flex flex-wrap items-center justify-between gap-3 border-t border-theme-border px-5 py-3 text-sm"><span className="text-theme-muted">Download {selected.repository} into this model cache before building a plan.</span><Button type="button" size="sm" variant="secondary" onClick={() => onNavigate('model-cache', { downloadModel: { clusterId: cluster, repoId: selected.repository, revision: selected.revision, storageVolumeId: storage } })}>Prepare model cache<ArrowRight size={14} /></Button></div>}
            </Panel>
            <Panel id="deployment-mode" padding="none" aria-label="Planning mode">
                <div className="space-y-6 p-5 sm:p-6">
                    <section>
                    <p className="deployment-section-label">Planning mode</p>
                    <div className="mt-3 grid gap-3 md:grid-cols-2">
                        {[
                            ['manual', 'Manual', 'Rule-based plan, core optimizations on', <SlidersHorizontal key="manual" size={18} />,
                                <p key="body" className="text-sm text-theme-muted">Turns on the checked llm-d optimizations when compatible; P/D separation is Auto. Edit any setting before deploying.</p>],
                            ['agentic', 'Agentic', 'AI recommends a plan for your goal', <Sparkles key="agentic" size={18} />,
                                providers.length ? <p key="body" className="text-sm text-theme-muted">Chooses optimizations and topology for your goal and workload.</p>
                                    : <div key="body" className="space-y-2"><p className="text-sm text-theme-text">Requires an AI provider. Add an OpenAI-compatible provider in External providers, then return here.</p><Button type="button" size="sm" variant="secondary" onClick={event => { event.stopPropagation(); onNavigate('ai-providers'); }}>Add AI provider<ArrowRight size={14} /></Button></div>],
                        ].map(([id, label, hint, icon, body]) => {
                            const unavailable = id === 'agentic' && !providers.length;
                            return <div key={id} className={cn('deployment-mode-option', unavailable ? 'is-unavailable' : 'cursor-pointer', mode === id && 'is-selected')} onClick={() => { if (!unavailable) setMode(id); }}>
                                <Button type="button" variant="ghost" className="deployment-mode-head" aria-pressed={mode === id} disabled={unavailable} title={unavailable ? 'Add an AI provider to use Agentic planning.' : undefined} onClick={() => setMode(id)}>
                                    <span className="deployment-mode-radio" aria-hidden="true" />
                                    <span className="deployment-mode-icon">{icon}</span>
                                    <span className="min-w-0"><span className="block text-base font-semibold text-theme-text">{label}</span><span className="block text-xs font-normal text-theme-muted">{hint}</span></span>
                                </Button>
                                <div className="deployment-mode-body">{body}</div>
                            </div>;
                        })}
                    </div>
                    </section>
                    <section id="deployment-objective" className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-t border-theme-border pt-5">
                        <div className="min-w-0">
                            <p className="deployment-section-label">Optimize for</p>
                            <p className="mt-1 text-sm text-theme-muted">{OBJECTIVE_HINTS[objective]}</p>
                        </div>
                        <ToggleGroup value={objective} onChange={setObjective} options={OBJECTIVES} />
                    </section>
                    {mode === 'agentic' && <section id="deployment-planner" className="border-t border-theme-border pt-5">
                        <p className="deployment-section-label"><Sparkles size={13} aria-hidden="true" />AI planner</p>
                        <div className="mt-3 grid gap-4 md:grid-cols-[minmax(0,16rem)_minmax(0,1fr)]">
                            <div>
                                <label className="block text-xs font-medium text-theme-text">AI provider<Select className="mt-1.5" aria-label="AI provider" required value={planner.provider} onChange={event => setPlanner(current => ({ ...current, provider: event.target.value }))}>
                                    <option value="" disabled>Select an AI provider</option>
                                    {providers.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
                                </Select></label>
                                <button type="button" className="mt-1.5 text-xs text-orange-500 underline underline-offset-2 hover:text-orange-600" onClick={() => onNavigate('ai-providers')}>Open External providers</button>
                            </div>
                            <label className="block text-xs font-medium text-theme-text">Goal (optional)<Textarea className="mt-1.5" rows={3} maxLength={1800} value={planner.prompt} placeholder="For example: serve document analysis with long inputs and short responses." onChange={event => setPlanner(current => ({ ...current, prompt: event.target.value }))} /></label>
                        </div>
                    </section>}
                    <section className="border-t border-theme-border pt-5">
                    <p className="deployment-section-label">Optional settings</p>
                    <div className="deployment-options mt-3">
                    <details id="deployment-workload" className="deployment-workload">
                        <OptionSummary icon={<SlidersHorizontal size={15} aria-hidden="true" />} title="Workload & limits"
                            detail={customCount ? `${customCount} custom value${customCount === 1 ? '' : 's'}` : 'Using model and cluster defaults'} />
                        <div className="deployment-workload-body space-y-4">
                            <WorkloadGroup title="Limits" hint="Concurrency sizes serving replicas from per-replica KV capacity; QPS is the AIC load target.">
                                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                                    {workloadField('contextLength', 'Context length (tokens)', `${(selected.contextLength ?? 4096).toLocaleString()} (model max)`)}
                                    {workloadField('maxAccelerators', 'Accelerator limit', hardware?.usableGpuCount != null ? `All ${hardware.usableGpuCount} free` : 'All free')}
                                    <div className="text-xs font-medium text-theme-text">
                                        <div className="flex flex-wrap items-center justify-between gap-2"><span>Load target</span><ToggleGroup size="xs" value={loadTarget} options={[{ value: 'concurrency', label: 'Concurrency' }, { value: 'requestRate', label: 'QPS' }]} onChange={target => { setLoadTarget(target); setWorkload(current => ({ ...current, concurrency: '', requestRate: '' })); }} /></div>
                                        <Input className="mt-1.5" type="number" aria-label={loadTarget === 'concurrency' ? 'Concurrent requests' : 'Request rate (QPS)'} min={loadTarget === 'requestRate' ? '0.01' : '1'} step={loadTarget === 'requestRate' ? 'any' : '1'} value={workload[loadTarget]} placeholder="Not set" onChange={event => setWorkload(current => ({ ...current, [loadTarget]: event.target.value }))} />
                                    </div>
                                </div>
                            </WorkloadGroup>
                            <WorkloadGroup title="Request shape" hint={mode === 'manual' ? 'With mean input and output tokens, AIC compares P/D with combined serving.' : 'The AI infers prefix reuse and prefill-heavy traffic from your goal and these token counts.'}>
                                <div className="grid gap-4 sm:grid-cols-2">
                                    {workloadField('inputTokens', 'Mean input tokens', 'Full context')}
                                    {workloadField('outputTokens', 'Mean output tokens', 'Full context')}
                                </div>
                            </WorkloadGroup>
                            <WorkloadGroup title="SLA targets" hint={mode === 'manual' ? 'AIC checks each candidate topology against the TTFT and TPOT targets; the plan shows whether the selected one meets them.' : 'Requirements, not predicted results. Blank targets stay unspecified.'}>
                                <div className="grid gap-4 sm:grid-cols-2">
                                    {workloadField('ttftMs', 'P95 TTFT target (ms)', 'No target')}
                                    {workloadField('tpotMs', 'P95 TPOT target (ms)', 'No target')}
                                </div>
                            </WorkloadGroup>
                            {customCount > 0 && <Button type="button" size="xs" variant="ghost" onClick={() => setWorkload(Object.fromEntries(Object.keys(workload).map(key => [key, ''])))}>Reset to defaults</Button>}
                        </div>
                    </details>
                    {mode === 'manual' && <details id="deployment-optimizations" className="deployment-workload">
                        <OptionSummary icon={<Layers size={15} aria-hidden="true" />} title="Optimizations"
                            detail={forbidden.length ? `${forbidden.length} turned off` : 'Core llm-d optimizations on'} />
                        <div className="deployment-workload-body space-y-3">
                            <p className="text-xs text-theme-muted">Checked optimizations are enabled when the topology, hardware and pinned source support them; otherwise the plan reports them as degraded with a reason. Unchecked optimizations are never enabled.</p>
                            <OptimizationChecklist choices={choices} forbidden={forbidden} onChange={setForbidden} />
                        </div>
                    </details>}
                    <details id="deployment-serving" className="deployment-workload">
                        <OptionSummary icon={<Wrench size={15} aria-hidden="true" />} title="Model parameters"
                            detail={`${toolSummary} · ${JSON.stringify(serving) === JSON.stringify(defaultServing(selected)) ? 'Model defaults' : 'Deployment overrides'}`} />
                        <div className="deployment-workload-body space-y-4">
                            <ModelParametersForm value={serving} onChange={setServing} />
                            <p className="text-xs text-theme-muted">Changes apply only to this deployment. Catalog defaults stay unchanged.</p>
                            <Button type="button" size="xs" variant="ghost" onClick={() => setServing(defaultServing(selected))}>Restore model defaults</Button>
                        </div>
                    </details>
                    </div>
                    </section>
                </div>
                <div id="deployment-continue" className="flex flex-wrap items-center justify-between gap-3 border-t border-theme-border px-5 py-4">
                    <p className="text-sm text-theme-muted">Nothing is deployed until you confirm the reviewed plan.</p>
                    <Button type="submit" size="md" disabled={!cluster || !name || !cacheReady || checkingCache || !selected.vllmSupported || (mode === 'agentic' && !planner.provider)}>{mode === 'manual' ? 'Generate plan' : 'Generate plan with AI'}<ArrowRight size={15} /></Button>
                </div>
            </Panel>
            <FormError message={error} />
            {!selected.vllmSupported && <p role="alert">This model requires a runtime not supported by this workflow.</p>}
        </form>}
    </ModulePage>;
}
