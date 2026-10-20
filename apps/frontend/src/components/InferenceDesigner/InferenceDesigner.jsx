import { cloneElement, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Play, Eye, EyeOff, Settings2, Search } from 'lucide-react';
import { Badge, Button, Checkbox, Input, Select, Textarea, Modal } from '../../shared/ui/index';
import { InferenceTargetSetup } from './InferenceTargetSetup';
import { InferenceGuidePresets } from './InferenceGuidePresets';
import { capabilitySources } from '../../features/inference-designer/capabilitySources';
import { deploymentSelection, nodeParameterPaths, capabilityTogglePatch, selectionConflicts, resetDeploymentConfiguration } from '../../features/inference-designer/deploymentSelection';
import { InferenceDeploymentParameters } from './InferenceDeploymentParameters';
import { InferenceSourceComposer } from './InferenceSourceComposer';
import { loadSourceCatalog, generateSourceBundle, deploySourceBundle } from './sourceCompositionBackend';
import { InferenceRouterParameters } from './InferenceRouterParameters';
import { InferenceParameters, InferenceEngineOverrides } from './InferenceParameters';
import { PARAMETER_FIELDS } from '../../features/inference-designer/capabilityCatalog';
import { InferenceGenerationResult } from './InferenceGenerationResult';
import { InferenceFlowMap } from './InferenceFlowMap';
import { NODES, createBlueprint, blueprintGuide, blueprintVariants, blueprintErrors, blueprintClusterProfiles } from '../../features/inference-designer/blueprint';
import { getHardwareProfiles } from '../../pages/clusters/ui/ClusterMonitoringStack/hardwareProfilesBackend';
import { loadClusters, selectCluster, loadClusterOverview } from '../../pages/optimization/ui/OptimizationWorkspace/clusterBackend';
import { loadConfigurationCapabilities, saveConfiguration } from '../../pages/optimization/ui/OptimizationWorkspace/configurationBackend';
import { generateBlueprint } from './blueprintBackend';
import { downloadBlob, downloadFile } from '../../shared/lib/download';
import { inferenceScene, TIERS } from '../../features/inference-designer/sceneGraph';
import { compositionDisplayDraft } from '../../features/inference-designer/compositionPolicy';
import { useAuth } from '../../pages/auth/useAuth';
import './inferenceDesigner.css';
import { CHART_SERIES } from '../../shared/ui/charts/palette';
import { useClipboard } from '../../shared/lib/useClipboard';

function Field({ label, children, hint }) {
    const id = useId();
    return <div className="space-y-1 text-sm text-theme-text"><label htmlFor={id}>{label}</label>{cloneElement(children, { id, 'aria-describedby': hint ? `${id}-hint` : undefined })}{hint && <p id={`${id}-hint`} className="text-xs text-theme-muted">{hint}</p>}</div>;
}

export function InferenceDesigner({ onNavigate, initialDraft, initialResult, onReview, onClose }) {
    const [setupOpen, setSetupOpen] = useState(!initialDraft);
    const [intent, setDraft] = useState(() => ({ ...createBlueprint(), sourceMode: true, clusterId: sessionStorage.getItem('prism_cluster_server_id') || '', selectedGuides: [], capabilityOverrides: {}, guideChoices: {}, ...initialDraft }));
    const [settingsTab, setSettingsTab] = useState('target');
    const [navigationTab, setNavigationTab] = useState('tiers');
    const [expandedStage, setExpandedStage] = useState('');
    const [clusterOverview, setClusterOverview] = useState(null);
    const [selectedObject, setSelectedObject] = useState(null);
    const [editing, setEditing] = useState(false);
    const [allParameters, setAllParameters] = useState(false);
    const [catalogQuery, setCatalogQuery] = useState('');
    const [searchOpen, setSearchOpen] = useState(false);
    const [visible, setVisible] = useState(TIERS.map(tier => tier.id));
    const [focus, setFocus] = useState(null);
    const selectObject = useCallback(id => {
        setSelectedObject(id);
        setEditing(false); setAllParameters(false);
        if (id) setFocus({ id });
        if (TIERS.some(tier => tier.id === id)) setNavigationTab('tiers');
    }, []);
    const { can } = useAuth();
    const canGenerate = can('guide:plan:execute') && can('configuration:artifact:render');
    const [showOutput, setShowOutput] = useState(false);
    const [deploying, setDeploying] = useState(false);
    const deployLock = useRef(false);
    const [resources, setResources] = useState({ profiles: [], providers: [], clusters: [] });
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState('');
    const [error, setError] = useState('');
    const [selectionError, setSelectionError] = useState('');
    const [routerInputError, setRouterInputError] = useState('');
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState(initialResult || null);
    const [fileIndex, setFileIndex] = useState(0);
    const [reload, setReload] = useState(0);
    const generation = useRef(0);
    const [sourceCatalog, setSourceCatalog] = useState(null);
    const [sourceLoading, setSourceLoading] = useState(false);
    const [sourceError, setSourceError] = useState('');
    const clusterProfiles = useMemo(() => blueprintClusterProfiles(clusterOverview?.cluster?.id === intent.clusterId ? clusterOverview : null, resources.profiles), [clusterOverview, intent.clusterId, resources.profiles]);
    const profile = clusterProfiles.length === 1 ? clusterProfiles[0] : clusterProfiles.find(item => item.id === intent.profileId);
    const profileId = profile?.upstream_variant ? profile.id : '';
    const plan = useMemo(() => sourceCatalog ? deploymentSelection(sourceCatalog, intent.selectedGuides, { model: intent.model, variables: intent.sourceVariables, namespace: intent.sourceNamespace,
        capabilities: intent.capabilityOverrides, guideChoices: intent.guideChoices, mode: intent.sourceRouter.mode, accelerator: profile?.upstream_variant,
    }) : null, [sourceCatalog, intent, profile]);
    const includedGuides = [...new Set([...intent.selectedGuides, ...(plan?.guides || [])])];
    const draft = useMemo(() => !plan ? { ...intent, profileId } : ({ ...intent, profileId, accelerator: profile?.upstream_variant, sourceMode: true, sourcePlan: plan, selectedGuides: intent.selectedGuides, runtimeImageExplicit: !!intent.image, image: intent.image || profile?.deployment?.runtime_image || '',
        sourceUnits: plan.units.map(unit => ({ ...unit, ...intent.sourceUnits.find(item => item.path === unit.path) })),
        sourceRouter: { ...plan.router, values: plan.router.values.map(layer => ({ ...layer, ...intent.sourceRouter.values.find(item => item.path === layer.path) })) },
    }), [intent, plan, profile, profileId]);
    useEffect(() => {
        let active = true;
        setClusterOverview(null);
        if (!intent.clusterId) return;
        Promise.all([selectCluster(intent.clusterId), loadClusterOverview(intent.clusterId)]).then(([session, overview]) => {
            if (!active) return;
            if (session.sessionId) {
                sessionStorage.setItem('prism_cluster_server_id', intent.clusterId);
                sessionStorage.setItem('prism_cluster_session_id', session.sessionId);
                sessionStorage.removeItem('prism_cluster_connection');
            }
            setClusterOverview(overview);
        }).catch(reason => { if (active) setSourceError(reason.message); });
        return () => { active = false; };
    }, [intent.clusterId, reload]);

    useEffect(() => {
        let active = true;
        setSourceCatalog(null); setSourceError('');
        if (!draft.clusterId) return;
        setSourceLoading(true);
        loadSourceCatalog(draft.clusterId).then(catalog => {
            if (!active) return;
            if (!Array.isArray(catalog.guides) || !Array.isArray(catalog.assets) || !/^[a-f0-9]{40}$/.test(catalog.commit || '')) throw new Error('Invalid source catalog response.');
            setSourceCatalog(catalog);
            setDraft(current => ({ ...current, sourceGuides: catalog.guides.map(item => ({ id: item.id, title: item.title, disposition: item.disposition, hasAssets: catalog.assets.some(asset => asset.guide === item.id || asset.guide.startsWith(`${item.id}/`)) })), sourceCommit: current.sourceCommit || catalog.commit }));
        }).catch(reason => { if (active) setSourceError(reason.message); }).finally(() => { if (active) setSourceLoading(false); });
        return () => { active = false; };
    }, [draft.clusterId, reload]);
    const [exporting, setExporting] = useState(false);
    const { copied, copy } = useClipboard();
    useEffect(() => {
        let active = true;
        setLoading(true); setLoadError('');
        Promise.all([getHardwareProfiles(), loadConfigurationCapabilities({ refresh: true }), loadClusters()])
            .then(([hardware, capabilities, clusters]) => { if (active) setResources({ profiles: hardware.profiles || [], providers: capabilities.providers || [], clusters: clusters.items || [] }); })
            .catch(reason => { if (active) setLoadError(reason.message); })
            .finally(() => { if (active) setLoading(false); });
        return () => { active = false; };
    }, [reload]);
    useEffect(() => () => { generation.current += 1; }, []);
    const clusterName = resources.clusters.find(item => item.id === draft.clusterId)?.name || '';
    const displayDraft = useMemo(() => compositionDisplayDraft(draft, result), [draft, result]);
    const graph = useMemo(() => inferenceScene(displayDraft, clusterName), [displayDraft, clusterName]);
    const entity = [...graph.nodes, ...graph.capabilities].find(item => item.id === selectedObject);
    const selected = editing ? allParameters ? 'all' : entity?.inspector : null;
    const capability = graph.capabilities.find(item => item.id === entity?.id);
    const matched = graph.capabilities.filter(item => `${item.label} ${item.detail} ${item.guide}`.toLowerCase().includes(catalogQuery.toLowerCase()));
    const node = NODES.find(item => item.id === selected);
    const parameterPaths = nodeParameterPaths(entity, draft);
    const canConfigureNode = item => nodeParameterPaths(item, draft).length > 0 || (plan?.choices || []).some(choice => choice.guide === item?.guide) || ['prefix', 'chunked'].includes(item?.id);
    const blockedCapabilities = useMemo(() => Object.fromEntries(graph.capabilities.filter(cap => !cap.enabled).map(cap => {
        const patch = capabilityTogglePatch(cap, draft);
        const reason = !sourceCatalog ? 'Load the source catalog before selecting optimizations.' : patch ? selectionConflicts(sourceCatalog, draft, patch).join(' ') : '';
        return [cap.id, reason];
    })), [graph, sourceCatalog, draft]);
    const errors = [...blueprintErrors(draft, resources.providers, profile), ...(plan?.errors || []), ...(sourceError ? [sourceError] : [])];
    const change = patch => {
        const conflicts = selectionConflicts(sourceCatalog, draft, patch);
        const existing = selectionConflicts(sourceCatalog, draft);
        const introduced = conflicts.filter(message => !existing.includes(message));
        if (introduced.length) { setSelectionError(introduced.join(' ')); return; }
        setSelectionError('');
        generation.current += 1; setBusy(false); setResult(null); setShowOutput(false); setError('');
        if (patch.selectedGuides?.length === 0 && intent.selectedGuides?.length) patch = { ...patch, sourceUnits: [], sourceRouter: { ...(patch.sourceRouter || intent.sourceRouter), values: [] } };
        if (patch.clusterId !== undefined && patch.clusterId !== draft.clusterId) patch = { ...patch, modelCacheEntryId: '', model: '', storageVolumeId: '', sourceCommit: '', sourceUnits: [], sourceGuides: [], sourceVariables: {}, profileId: '', selectedGuides: [], capabilityOverrides: {}, guideChoices: {}, sourceRouter: { ...draft.sourceRouter, values: [] } };
        const addedGuide = patch.selectedGuides?.find(id => !draft.selectedGuides.includes(id));
        const addedNode = graph.capabilities.find(item => item.guide === addedGuide);
        if (addedNode) selectObject(addedNode.id);
        setDraft(current => ({ ...current, finalConfiguration: undefined, ...patch }));
    };

    const resetConfiguration = () => {
        change(resetDeploymentConfiguration(intent));
        setRouterInputError(''); setEditing(false); setSelectedObject(null); setFocus(null);
    };
    const inspectorRef = useRef(null);
    useEffect(() => {
        if (navigationTab !== 'tiers') return;
        inspectorRef.current?.querySelector('[data-focused="true"]')?.scrollIntoView({ block: 'nearest', behavior: 'instant' });
    }, [selectedObject, expandedStage, navigationTab]);

    const activateObject = id => {
        const cap = graph.capabilities.find(item => item.id === id);
        const patch = capabilityTogglePatch(cap, draft, sourceCatalog);
        if (blockedCapabilities[id]) setSelectionError(blockedCapabilities[id]);
        else if (patch) change(patch);
        const object = cap || graph.nodes.find(item => item.id === id);
        if (object) { setExpandedStage(object.owner || object.stage || object.id); setNavigationTab('tiers'); setCatalogQuery(''); }
        if (cap) setVisible(current => current.includes(cap.tier) ? current : [...current, cap.tier]);
        selectObject(id);
    };
    const generate = async () => {
        if (!draft.modelCacheEntryId || !draft.profileId) { setSetupOpen(true); return; }
        const id = ++generation.current;
        setShowOutput(true); setError(''); setResult(null);
        if (errors.length || loadError || routerInputError) { setError(routerInputError); return; }
        setBusy(true);
        try {
            const generated = draft.sourceMode ? await generateSourceBundle(draft) : await generateBlueprint(draft, resources.providers, profile);
            if (generation.current === id) {
                setResult(generated); setFileIndex(0);
                if (onReview) onReview({ draft, result: generated });
                else setShowOutput(true);
            }
        } catch (reason) { if (generation.current === id) setError(reason.message); }
        finally { if (generation.current === id) setBusy(false); }
    };
    const readRouterSource = async () => {
        const id = ++generation.current;
        setBusy(true); setError('');
        try {
            const snapshot = await generateBlueprint({ ...draft, routerValues: '', routerSourceCommit: '', routerSourceKey: '' }, resources.providers, profile);
            const source = snapshot.files.find(item => item.name === 'router-effective.yaml');
            if (!source) throw new Error('The planner did not return effective router values.');
            if (generation.current === id) change({
                ...Object.fromEntries(PARAMETER_FIELDS.filter(field => field.output.startsWith('router.')).map(field => [field.key, ''])),
                routerValues: source.content, routerSourceCommit: snapshot.plan.source.commit,
                routerSourceKey: `${blueprintGuide(draft)}:${draft.variant}:${draft.profileId}`,
            });
        } catch (reason) { if (generation.current === id) setError(reason.message); }
        finally { if (generation.current === id) setBusy(false); }
    };
    const download = (name, content) => downloadBlob(new Blob([content], { type: 'text/plain;charset=utf-8' }), name);
    const file = result?.files[fileIndex];
    const exportBundle = async () => {
        setExporting(true); setError('');
        try {
            const saved = await saveConfiguration(result.configuration, 'inference-blueprint.yaml');
            await downloadFile(`/api/v1/configurations/artifacts/${encodeURIComponent(saved.artifact.artifact_id)}/bundle`, { fallbackName: 'inference-deployment.zip' });
        } catch (reason) { setError(reason.message); }
        finally { setExporting(false); }
    };
    const deploy = async (endpoint, name) => {
        if (deployLock.current) return;
        deployLock.current = true; setDeploying(true); setError('');
        try { await deploySourceBundle(result, endpoint, name); onNavigate('optimization-deployments'); }
        catch (reason) { setError(reason.message); }
        finally { deployLock.current = false; setDeploying(false); }
    };
    const openSettings = () => { setSelectedObject('request'); setAllParameters(true); setEditing(true); setSettingsTab('parameters'); };
    const focusObject = (item) => {
        setVisible(current => current.includes(item.tier) ? current : [...current, item.tier]);
        setExpandedStage(item.owner || item.stage || item.id);
        selectObject(item.id);
    };
    if (showOutput && !onReview) return <InferenceGenerationResult busy={busy} errors={errors} error={error} result={result} fileIndex={fileIndex} setFileIndex={setFileIndex}
        onDeploy={deploy} deploying={deploying} canDeploy={can('deployment:run:create') && can('configuration:artifact:save') && can('configuration:artifact:render')} onManage={() => onNavigate('optimization-deployments')}
        onBack={() => setShowOutput(false)} onEdit={() => { setShowOutput(false); openSettings(); }} onExport={exportBundle} exporting={exporting} canSave={can('configuration:artifact:save') && !!result?.configuration}
        onCopy={async () => { if (!await copy(file.content)) setError('Could not copy this file. Use Download file.'); }} copied={copied} onDownload={() => download(file.name, file.content)}
        onRetryResources={() => setReload(value => value + 1)} loadError={loadError} />;
    return <div className="inference-designer" data-editing={false} onKeyDown={event => { if (event.key === 'Escape' && !editing) selectObject(null); }}>
        <InferenceTargetSetup key={draft.clusterId} open={setupOpen} draft={{ ...draft, image: intent.image }} resources={{ ...resources, profiles: clusterProfiles }} hardwareLoading={!!draft.clusterId && clusterOverview?.cluster?.id !== draft.clusterId} change={change} onClose={() => setSetupOpen(false)} onNavigate={onNavigate} />
        <div className="inference-context inference-glass">{onClose && <Button variant="secondary" size="sm" onClick={onClose}>Back to plan</Button>}<Button variant="ghost" size="sm" onClick={() => setSetupOpen(true)}>{clusterName || 'Choose target'}<span> / </span>{draft.model || 'Choose downloaded model'}</Button><Badge tone={draft.modelCacheEntryId ? 'success' : 'warning'}>{draft.modelCacheEntryId ? 'Cached model' : 'Setup required'}</Badge>{error && <span role="alert">{error}</span>}</div>
        <InferenceFlowMap draft={displayDraft} clusterName={clusterName} selected={allParameters && editing ? null : selectedObject} onSelect={selectObject} visible={visible} focus={focus} cluster={resources.clusters.find(item => item.id === draft.clusterId)} profile={profile} result={result} catalog={sourceCatalog} blockedReason={blockedCapabilities[selectedObject]} onActivate={activateObject} canConfigure={canConfigureNode} onConfigure={() => { setEditing(true); setAllParameters(false); }} onTarget={() => setSetupOpen(true)} />
        <aside ref={inspectorRef} className="inference-inspector inference-glass" aria-label="Configuration inspector">
                <div className="inference-panel-heading"><span>Tiers</span><div className="flex items-center gap-1">
                    <Button variant="ghost" size="icon" aria-label="Search tiers" title="Search tiers" onClick={() => { setSearchOpen(value => !value); setCatalogQuery(''); }}><Search size={14} /></Button>
                    <Button variant="ghost" size="icon" aria-label="Deployment settings" title="Deployment settings" onClick={openSettings}><Settings2 size={14} /></Button>
                    <Button size="xs" onClick={generate} disabled={!canGenerate || loading || busy}><Play size={12} />Generate</Button>
                </div></div>
                <Button variant="ghost" size="xs" onClick={resetConfiguration}>Reset configuration</Button>
                {selectionError && <p role="alert" className="text-xs text-theme-danger">{selectionError}</p>}
                {searchOpen && <div className="inference-catalog-search"><Input aria-label="Search tiers and optimizations" placeholder="Search" value={catalogQuery} onChange={event => setCatalogQuery(event.target.value)} /></div>}
                {plan?.choices?.some(choice => !choice.value?.length) && <Button variant="secondary" size="xs" onClick={() => setNavigationTab('guides')}>Choose required variants</Button>}
                <div className="inference-settings-tabs" role="tablist" aria-label="Map navigation"><Button role="tab" aria-selected={navigationTab === 'tiers'} variant="ghost" size="xs" onClick={() => setNavigationTab('tiers')}>Tiers</Button><Button role="tab" aria-selected={navigationTab === 'guides'} variant="ghost" size="xs" onClick={() => setNavigationTab('guides')}>Guides <small>{includedGuides.length}</small></Button></div>
                {navigationTab === 'guides' && <InferenceGuidePresets includedGuides={includedGuides} catalog={sourceCatalog} draft={draft} change={change} plan={plan} onFocus={id => { const item = graph.capabilities.find(cap => cap.guide === id); if (item) focusObject(item); }} />}
                {navigationTab === 'tiers' && <div className="inference-selection-key"><span>● Selected</span><span>↳ Required</span><span>○ Available</span></div>}
                {navigationTab === 'tiers' && graph.tiers.map((layer, index) => <section className="inference-layer-group" style={{ '--tier-color': CHART_SERIES[index] }} key={layer.id} data-hidden={!visible.includes(layer.id)} data-selected={selectedObject === layer.id || entity?.tier === layer.id} data-enabled={graph.nodes.some(item => item.tier === layer.id && item.inPlan)}>
                    <div className="inference-layer-heading">
                        <Button variant="ghost" size="xs" aria-pressed={selectedObject === layer.id} onClick={() => { setVisible(current => current.includes(layer.id) ? current : [...current, layer.id]); selectObject(layer.id); }}><span className="inference-layer-dot" aria-hidden="true" />{layer.label}</Button>
                        <Button variant="ghost" size="icon" aria-label={`Toggle ${layer.label}`} aria-pressed={visible.includes(layer.id)} onClick={() => {
                            setVisible(current => current.includes(layer.id) ? current.filter(id => id !== layer.id) : [...current, layer.id]);
                            if (entity?.tier === layer.id || selectedObject === layer.id) selectObject(null);
                        }}>{visible.includes(layer.id) ? <Eye size={13} /> : <EyeOff size={13} />}</Button>
                    </div>
                    <div className="inference-layer-items">
                        {graph.nodes.filter(item => item.tier === layer.id && (!catalogQuery || item.optimizations.some(id => matched.some(cap => cap.id === id)))).map(item => <div key={item.id} style={{ '--tier-color': CHART_SERIES[item.colorIndex] }}>
                            <div className="inference-stage-row"><Button variant="ghost" size="xs" className="inference-layer-item" data-enabled={item.inPlan} aria-expanded={expandedStage === item.id} data-focused={item.id === selectedObject} aria-pressed={item.id === selectedObject} onClick={() => focusObject(item)}><span className="inference-item-dot" aria-hidden="true" /><span>{item.label}</span><small>{graph.capabilities.filter(cap => item.optimizations.includes(cap.id) && cap.enabled).length}/{item.optimizations.length}</small></Button></div>
                            {(catalogQuery || expandedStage === item.id) && <div className="inference-optimization-list" role="group" aria-label={`${item.label} optimizations`}>{matched.filter(cap => item.optimizations.includes(cap.id)).map(cap => <div className="inference-stage-row" key={cap.id}><Button variant="ghost" size="xs" className="inference-layer-item inference-capability-row" data-enabled={cap.enabled} data-focused={cap.id === selectedObject} aria-pressed={!!cap.enabled} aria-label={cap.label} aria-disabled={!!blockedCapabilities[cap.id] || cap.required || cap.id === 'parallel'} title={blockedCapabilities[cap.id] || (cap.required ? "Required by selected Guide" : `${cap.enabled ? "Disable" : "Enable"} ${cap.label}`)} onClick={() => activateObject(cap.id)}><span className="inference-item-dot" aria-hidden="true" /><span>{cap.label}</span><small>{blockedCapabilities[cap.id] ? 'Conflict' : cap.required ? 'Required' : cap.enabled ? 'Selected' : 'Off'}</small></Button>{canConfigureNode(cap) && <Button variant="ghost" size="icon" aria-label={`Configure ${cap.label} parameters`} title="Configure parameters without changing selection" onClick={() => { focusObject(cap); setEditing(true); }}><Settings2 size={12} /></Button>}</div>)}</div>}
                        </div>)}
                    </div>
                </section>)}
        </aside>
        <Modal isOpen={editing} onClose={() => setEditing(false)} title={allParameters ? 'Deployment settings' : `${entity?.label || 'Node'} parameters`} subtitle="Settings from the selected source revision" size="xl">
                {editing && <div className="inference-parameter-fields space-y-4">
                {selectionError && <p role="alert" className="text-xs text-theme-danger">{selectionError}</p>}
                {allParameters && <Button variant="secondary" size="sm" onClick={resetConfiguration}>Reset configuration</Button>}
                {allParameters && <>
                    <div className="inference-settings-tabs" role="tablist" aria-label="Deployment sections">{['parameters', 'target'].map(tab => <Button key={tab} role="tab" aria-selected={settingsTab === tab} variant="ghost" size="sm" onClick={() => setSettingsTab(tab)}>{tab[0].toUpperCase() + tab.slice(1)}</Button>)}</div>
                                        {settingsTab === 'parameters' && <InferenceDeploymentParameters draft={draft} change={change} />}
                    {settingsTab === 'target' && <section className="inference-target-card"><h3>Deployment target</h3><p>Uses the same cluster and source checkout as Clusters and Deployments.</p><details className="inference-expert"><summary>Advanced runtime settings</summary><Field label="Runtime image override" hint="Optional for this deployment only. Leave blank to preserve Guide images and use the hardware profile for unresolved image placeholders. Cluster image preparation uses the same hardware registry; cached images do not select a deployment version."><Input aria-label="Runtime image override" value={intent.image} placeholder="Use Guide and hardware defaults" onChange={event => change({ image: event.target.value })} /></Field></details><Field label="Namespace"><Input value={draft.sourceNamespace} onChange={event => change({ sourceNamespace: event.target.value })} /></Field><Checkbox label="Validate with the target Kubernetes API" checked={draft.sourceValidate} onChange={event => change({ sourceValidate: event.target.checked })} /><dl><dt>Source</dt><dd>{sourceLoading ? 'Loading…' : sourceCatalog?.commit.slice(0, 12) || 'Not loaded'}</dd><dt>Cluster nodes</dt><dd>{clusterOverview?.kubernetes?.nodes?.length ?? 'Unknown'}</dd></dl>{sourceError && <p role="alert">{sourceError}</p>}</section>}
                </>}
                <div className="space-y-4">


                {capability && <details className="inference-binding"><summary>Source reference</summary><span>{capability.detail}</span>{capabilitySources(capability, sourceCatalog, draft).map(link => <a key={link.href} href={link.href} target="_blank" rel="noreferrer">{link.label} ↗</a>)}</details>}
                {['request', 'cluster', 'workers', 'all'].includes(selected) && (!allParameters || settingsTab === 'target') && <Button variant="secondary" onClick={() => setSetupOpen(true)}>Change cluster or model</Button>}
                <div hidden={allParameters && settingsTab !== 'parameters'} className="space-y-4">
                {initialDraft?.runtimeConfiguration && !allParameters && <InferenceDeploymentParameters draft={draft} change={change} />}
                {!draft.sourceMode && ['transfer', 'all'].includes(selected) && <>
                    <Checkbox label="Enable Prefill / Decode separation" checked={draft.pd} onChange={event => change({ pd: event.target.checked, variant: '' })} />
                    {draft.pd && <Field label="P/D variant"><Select value={draft.variant} onChange={event => change({ variant: event.target.value })}><option value="">Select variant</option>{blueprintVariants(draft, resources.providers, profile).map(value => <option key={value} value={value}>{value}</option>)}</Select></Field>}
                </>}
                {!draft.sourceMode && ['index', 'all'].includes(selected) && <Checkbox label="Enable precise prefix-cache routing" checked={draft.precise} onChange={event => change({ precise: event.target.checked })} />}
                {!draft.sourceMode && ['cache', 'all'].includes(selected) && <Field label="External cache tier"><Select value={draft.cache} onChange={event => change({ cache: event.target.value })}><option value="none">None · inherit engine cache</option><option value="native">Native CPU offload</option><option value="lmcache">LMCache CPU offload</option></Select></Field>}
                {!initialDraft?.runtimeConfiguration && (capability && (!capability.available || draft.sourceMode) ? <InferenceSourceComposer draft={draft} change={change} catalog={sourceCatalog} guide={capability.guide} entity={entity} assetPaths={parameterPaths} loading={sourceLoading} error={sourceError} /> : !draft.sourceMode && <InferenceParameters draft={draft} change={change} all={allParameters} fields={capability?.fields || (selected === 'routing' || selected === 'service' ? PARAMETER_FIELDS.filter(field => field.group === 'router').map(field => field.key) : selected === 'prefill' ? ['prefillReplicas', 'prefillTp', 'maxNumBatchedTokens', 'maxNumSeqs', 'prefixCaching', 'chunkedPrefill'] : ['decode', 'pods'].includes(selected) ? ['replicas', 'tp', 'maxNumSeqs', 'maxModelLen', 'gpuMemoryUtilization'] : selected === 'response' ? ['tracingEnabled', 'otelEndpoint', 'sampler', 'samplerArg'] : [])} />)}
                {!initialDraft?.runtimeConfiguration && draft.sourceMode && !capability && !allParameters && parameterPaths.length > 0 && <InferenceSourceComposer draft={draft} change={change} catalog={sourceCatalog} entity={entity} assetPaths={parameterPaths} loading={sourceLoading} error={sourceError} />}
                {!draft.sourceMode && ['parallel', 'prefix', 'chunked', 'prefill', 'decode', 'all'].includes(selected) && <InferenceEngineOverrides draft={draft} change={change} />}
                {!draft.sourceMode && ['routing', 'all'].includes(selected) && <><InferenceRouterParameters onError={setRouterInputError} draft={draft} change={change} onLoad={readRouterSource} busy={busy} canLoad={canGenerate && !loading && !loadError && !blueprintErrors({ ...draft, routerValues: '', routerSourceCommit: '', routerSourceKey: '' }, resources.providers, profile).length} /><Button variant="ghost" size="xs" onClick={() => change({ routerValues: '', routerSourceCommit: '', routerSourceKey: '' })}>Clear router overlay & source snapshot</Button></>}
                {!draft.sourceMode && ['routing', 'all'].includes(selected) && <details className="inference-expert"><summary>Router plugin & Helm values overlay</summary><Field label="Router Helm values overlay" hint="Use the full plugin configuration from your pinned source. Arrays replace arrays; producers, filters, scorers and profiles must stay consistent. Conflicts with structured fields block generation."><Textarea rows={14} value={draft.routerValues} onChange={event => change({ routerValues: event.target.value })} /></Field></details>}
                {node?.unavailable && !capability && <Badge tone="warning">No generator registered</Badge>}
                </div></div></div>}

        </Modal>
        {(loadError || sourceError) && <div className="inference-resource-error" role="alert">{loadError || sourceError}<Button size="xs" onClick={() => setReload(value => value + 1)}>Retry</Button></div>}
    </div>;
}
