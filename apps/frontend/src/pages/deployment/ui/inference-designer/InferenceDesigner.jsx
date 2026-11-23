import { useCallback, useEffect, useEffectEvent, useMemo, useState } from 'react';
import { Settings2 } from 'lucide-react';
import { Badge, Button, Modal, Panel, Spinner } from '../../../../shared/ui';
import { useAuth } from '../../../auth';
import { createBlueprint } from '../../model/inference-designer/blueprint';
import { capabilityTogglePatch, deploymentSelection, nodeParameterPaths, selectionConflicts } from '../../model/inference-designer/deploymentSelection';
import { compositionDisplayDraft } from '../../model/inference-designer/compositionPolicy';
import { inferenceScene, optimizationFlowPlan } from '../../model/inference-designer/sceneGraph';
import { nodeResourceKinds } from '../../model/inference-designer/nodeParameters';
import { OPTIMIZATION_STATES } from '../../model/deploymentPlan';
import { requirementCapability } from '../../model/inference-designer/deploymentPlanning';
import { loadSourceCatalog } from './sourceCompositionBackend';
import { InferenceFlowMap2D } from './InferenceFlowMap2D';
import { InferenceGuidePresets } from './InferenceGuidePresets';
import { InferenceSourceComposer } from './InferenceSourceComposer';
import { InferenceDeploymentParameters } from './InferenceDeploymentParameters';
import { InferenceParameterInput } from './InferenceParameterInput';
import { InferenceSliceParameters } from './InferenceSliceParameters';
import { isDisaggregatedDraft } from '../../model/inference-designer/sliceLayout';
import './inferenceDesigner.css';

const APPLY_STAGES = [[0, 'Rendering the selected Guides'], [6, 'Binding the model cache and runtime'], [14, 'Checking cluster devices and memory'], [30, 'Validating the final configuration']];

function ApplyProgress() {
    const [seconds, setSeconds] = useState(0);
    useEffect(() => { const timer = setInterval(() => setSeconds(value => value + 1), 1000); return () => clearInterval(timer); }, []);
    const stage = APPLY_STAGES.filter(([after]) => seconds >= after).at(-1)[1];
    return <div role="status" className="min-w-0 flex-1 space-y-1.5">
        <div className="flex justify-between text-xs"><span>{stage}…</span><span className="text-theme-muted">{seconds}s</span></div>
        <div className="plan-progress-track"><div className="plan-progress-indeterminate" /></div>
    </div>;
}

function Reason({ text }) {
    if (!text) return null;
    if (text.length <= 160) return <p className="text-sm text-theme-muted">{text}</p>;
    return <details className="text-sm text-theme-muted"><summary className="cursor-pointer">{text.slice(0, 150).replace(/\s+\S*$/, '')}… <span className="text-xs underline">More</span></summary><p className="mt-1">{text}</p></details>;
}

/** Edits one source-backed workflow snapshot; deployment stays approval-gated. */
export function InferenceDesigner({ initialDraft, initialResult, clusterName, disabled, onReview, onEditingChange, review, focus, onFocusOpened, configuredCount, onToggleRequirement, flowOverlay, toggleRequest, onToggleHandled }) {
    const { can } = useAuth();
    const [intent, setIntent] = useState(() => ({ ...createBlueprint(), ...initialDraft }));
    const [catalog, setCatalog] = useState(null);
    const [loading, setLoading] = useState(true), [error, setError] = useState('');
    const [dirty, setDirty] = useState(false), [saving, setSaving] = useState(false);
    const [selectionChanged, setSelectionChanged] = useState(false);
    const [selected, setSelected] = useState('routing'), [parameters, setParameters] = useState(null);
    const [reload, setReload] = useState(0);
    const select = useCallback(id => { setSelected(id); if (!id) setParameters(null); }, []);
    useEffect(() => {
        let active = true;
        Promise.resolve().then(() => { if (active) { setLoading(true); setError(''); } });
        loadSourceCatalog(intent.clusterId)
            .then(source => { if (active) setCatalog(source); })
            .catch(reason => { if (active) setError(reason.message); })
            .finally(() => { if (active) setLoading(false); });
        return () => { active = false; };
    }, [intent.clusterId, reload]);
    useEffect(() => { onEditingChange?.(dirty); return () => onEditingChange?.(false); }, [dirty, onEditingChange]);
    const plan = useMemo(() => catalog ? deploymentSelection(catalog, intent.selectedGuides, {
        model: intent.model, variables: intent.sourceVariables, namespace: intent.sourceNamespace,
        capabilities: intent.capabilityOverrides, guideChoices: intent.guideChoices, mode: intent.sourceRouter.mode,
        accelerator: intent.accelerator, units: intent.sourceUnits, routerValues: intent.sourceRouter.values,
        finalConfiguration: intent.finalConfiguration,
    }) : initialDraft.sourcePlan, [catalog, intent, initialDraft.sourcePlan]);
    const draft = useMemo(() => !plan ? intent : ({ ...intent, sourcePlan: plan,
        sourceGuides: catalog?.guides.map(item => ({ id: item.id, title: item.title, disposition: item.disposition, hasAssets: catalog.assets.some(asset => asset.guide === item.id || asset.guide.startsWith(`${item.id}/`)) })) || intent.sourceGuides,
        sourceUnits: plan.units.map(unit => ({ ...unit, ...intent.sourceUnits.find(item => item.path === unit.path) })),
        sourceRouter: { ...intent.sourceRouter, ...plan.router, values: plan.router.values.map(layer => ({ ...layer, ...intent.sourceRouter.values.find(item => item.path === layer.path) })) },
    }), [intent, plan, catalog]);
    const result = dirty ? null : initialResult;
    const display = useMemo(() => compositionDisplayDraft(draft, result), [draft, result]);
    const graph = useMemo(() => inferenceScene(display, clusterName), [display, clusterName]);
    const entity = [...graph.nodes, ...graph.capabilities].find(item => item.id === selected);
    const stale = catalog && catalog.commit !== draft.sourceCommit;
    const locked = disabled || saving || loading || !catalog || stale;
    const blocked = Object.fromEntries(graph.capabilities.map(cap => {
        const patch = capabilityTogglePatch(cap, draft, catalog);
        return [cap.id, !catalog ? 'Source catalog unavailable.' : cap.id === 'cache' && !cap.enabled && draft.nativeCpuCacheGiB && draft.capabilityOverrides.prefix === false ? 'Enable prefix caching before restoring CPU KV cache.'
            : cap.required ? 'Required by the selected configuration. Change the dependent optimization or source recipe first.'
            : cap.selection === 'recipe' || cap.selection === 'parameter' ? 'Controlled by source parameters; edit this node or its Guide in Technical details.'
                : ['external-operation', 'requires-adapter', 'unavailable'].includes(cap.generation)
                    ? cap.reason : !cap.enabled && patch ? selectionConflicts(catalog, draft, patch).join(' ') : ''];
    }));
    // A compatible optimization that is off can be enabled and edited in one step.
    const enableable = item => item?.kind === 'capability' && !item.enabled && !blocked[item.id] && Boolean(capabilityTogglePatch(item, draft));
    const canConfigure = item => nodeParameterPaths(item, draft).length > 0
        || plan?.choices?.some(choice => choice.guide === item?.guide) || ['prefix', 'chunked'].includes(item?.id)
        || (isDisaggregatedDraft(draft) && item?.id === 'transfer')
        || (Boolean(draft.runtimeConfiguration) && (item?.enabled || item?.inPlan || enableable(item))
            && (item?.kind === 'capability' ? Boolean(nodeResourceKinds(item)) || item.id === 'cache'
                : ['prefill', 'decode', 'prefill-worker', 'decode-worker', 'serving-worker', 'pods'].includes(item?.id)));
    const change = (patch, automatic = false, { silent = false } = {}) => {
        if (locked) return false;
        // Metadata resolved by previews (e.g. chart pins) is not a user edit.
        if (silent) { setIntent(current => ({ ...current, ...patch })); return true; }
        const previous = selectionConflicts(catalog, draft);
        const conflicts = selectionConflicts(catalog, draft, patch).filter(message => !previous.includes(message));
        if (conflicts.length) { setError(conflicts.join(' ')); return false; }
        if (!automatic && patch.capabilityOverrides) patch = { ...patch, optimizationLocks: [...new Set([...(draft.optimizationLocks || []),
            ...Object.keys({ ...draft.capabilityOverrides, ...patch.capabilityOverrides }).filter(id => draft.capabilityOverrides[id] !== patch.capabilityOverrides[id])])] };
        if (patch.selectedGuides?.includes('tiered-prefix-cache')) patch = { ...patch, nativeCpuCacheGiB: undefined };
        const runtime = patch.runtimeConfiguration || draft.runtimeConfiguration;
        const transfer = patch.capabilityOverrides?.transfer;
        if (runtime && transfer === true && !runtime.prefillReplicas) patch = { ...patch, runtimeConfiguration: { ...runtime, prefillReplicas: 1, prefillTp: runtime.tp || 1 } };
        if (runtime && transfer === false && runtime.prefillReplicas) {
            const { prefillReplicas: _prefill, prefillTp: _tp, slices: _slices, placementPolicy: _policy, ...rest } = runtime;
            patch = { ...patch, runtimeConfiguration: rest };
        }
        setError(''); setDirty(true);
        if (Object.keys(patch).some(key => key !== 'finalConfiguration')) setSelectionChanged(true);
        setIntent(current => ({ ...current, ...patch }));
        return true;
    };
    // Core optimizations are plan requirements; toggling them here must rebuild the plan like the checkboxes do.
    const toggleRequirement = (id, enabled) => {
        const requirement = onToggleRequirement && requirementCapability(id);
        if (!requirement) return false;
        if (dirty) setError('Apply or discard your changes first.');
        else onToggleRequirement(requirement, enabled);
        return true;
    };
    const activate = id => {
        select(id);
        const cap = graph.capabilities.find(item => item.id === id);
        if (!cap || locked) return;
        if (toggleRequirement(id, !cap.enabled)) return;
        if (blocked[id]) { setError(blocked[id]); return false; }
        const patch = capabilityTogglePatch(cap, draft, catalog);
        return patch ? change(patch) : false;
    };
    const configure = (id, enable = false) => {
        select(id);
        const item = [...graph.nodes, ...graph.capabilities].find(item => item.id === id);
        const open = !locked && item && !item.unavailable && canConfigure(item) && (enable || !enableable(item));
        if (open && enableable(item) && toggleRequirement(id, true)) return;
        if (open && enableable(item) && !change(capabilityTogglePatch(item, draft))) return;
        setParameters(open ? 'node' : null);
    };
    const save = async () => {
        if (locked) return;
        setSaving(true); setError('');
        try { await onReview({ draft }); }
        catch (reason) { setError(reason.message); }
        finally { setSaving(false); }
    };
    // A checkbox in the Optimizations table toggles here and applies at once, like the core choices.
    const [autoApply, setAutoApply] = useState(false);
    const handleToggleRequest = useEffectEvent(() => {
        const { id, enabled } = toggleRequest;
        onToggleHandled?.();
        if (dirty) { setError('Apply or discard your changes first.'); return; }
        const cap = graph.capabilities.find(item => item.id === id);
        if (!cap) { setError(`${id} is not available in this source revision.`); return; }
        if (cap.enabled !== enabled && activate(id)) setAutoApply(true);
    });
    useEffect(() => { if (toggleRequest && !locked) handleToggleRequest(); }, [toggleRequest, locked]);
    const applyNow = useEffectEvent(() => { setAutoApply(false); save(); });
    useEffect(() => { if (autoApply && dirty) applyNow(); }, [autoApply, dirty]);
    const applyActions = dirty && <div id="deployment-apply" className="flex flex-wrap items-center justify-between gap-3">
            {saving ? <ApplyProgress /> : <p className="text-sm"><span className="font-medium">Unapplied changes.</span> <span className="text-theme-muted">Apply to re-render and check resources, or discard them.</span></p>}
            <div className="flex gap-2">
                <Button variant="ghost" disabled={saving} onClick={() => { setIntent({ ...createBlueprint(), ...initialDraft }); setDirty(false); setSelectionChanged(false); setParameters(null); setError(''); }}>Discard changes</Button>
                <Button disabled={locked || !can('guide:plan:execute') || !can('configuration:artifact:render')} isLoading={saving} onClick={save}>Apply changes and check resources</Button>
            </div>
        </div>;
    const status = entity?.kind === 'capability' ? optimizationFlowPlan(graph, blocked, canConfigure).nodes.find(node => node.id === entity.id)?.status : null;
    const state = status && OPTIMIZATION_STATES[status];
    const reason = entity?.kind === 'capability' && (blocked[entity.id] || entity.sourceReason || draft.optimizationDecisions?.find(item => item.id === entity.id)?.reason || entity.reason);
    const editable = entity && !entity.unavailable && canConfigure(entity);
    const toggleable = entity?.kind === 'capability' && !(parameters === 'node' && ['prefix', 'chunked'].includes(entity.id)) && Boolean(capabilityTogglePatch(entity, draft)) && !blocked[entity.id];
    const inspector = entity && <aside id="deployment-optimization-settings" aria-label="Optimization settings" className="space-y-4">
        {error && <p role="alert" className="text-sm">{error}</p>}
        <div className="space-y-3 rounded-xl border border-theme-border p-4">
            {state && <div className="flex items-center gap-2"><span className="inference-status-dot" style={{ background: state.color }} /><Badge tone={state.tone}>{state.label}</Badge><span className="text-xs text-theme-muted">{state.action}</span></div>}
            {entity.detail && <p className="text-sm">{entity.detail}</p>}
            <Reason text={reason} />
            {(toggleable || (parameters !== 'node' && editable)) && <div className="flex flex-wrap gap-2 pt-1">
                {toggleable && <Button variant={entity.enabled ? 'secondary' : 'primary'} size="sm" disabled={locked} onClick={() => activate(entity.id)}>{entity.enabled ? 'Disable' : 'Enable'}</Button>}
                {parameters !== 'node' && editable && <Button variant="secondary" size="sm" disabled={locked} onClick={() => configure(entity.id, true)}><Settings2 size={14} />{enableable(entity) ? 'Enable and edit parameters' : 'Edit parameters'}</Button>}
            </div>}
        </div>
        {parameters === 'node' && <fieldset disabled={locked} className="min-w-0 space-y-4">
            {entity.id === 'cache' && draft.nativeCpuCacheGiB != null && <label className="block text-sm">CPU KV cache per replica (GiB)
                <InferenceParameterInput key={draft.nativeCpuCacheGiB} leaf={{ key: 'nativeCpuCacheGiB', title: 'CPU KV cache per replica (GiB)', value: draft.nativeCpuCacheGiB, type: 'number' }} onCommit={raw => {
                    const value = Number(raw);
                    if (!Number.isFinite(value) || value <= 0 || value > 1024) { setError('CPU KV cache must be greater than zero and at most 1024 GiB per replica.'); return; }
                    change({ nativeCpuCacheGiB: value });
                }} />
                <span className="mt-1 block text-xs text-theme-muted">Total host memory is checked across all serving replicas when you apply.</span>
            </label>}
            {isDisaggregatedDraft(draft) && entity.id === 'transfer' && <InferenceSliceParameters draft={draft} change={change} disabled={locked} recommendation={initialResult?.sliceLayout?.recommended || draft.sliceRecommendation} />}
            <InferenceSourceComposer key={`source:${entity.id}`} draft={draft} change={change} catalog={catalog} entity={entity} assetPaths={nodeParameterPaths(entity, draft)} loading={loading} error={error} controlsOnly={Boolean(draft.runtimeConfiguration)} />
            {draft.runtimeConfiguration && nodeResourceKinds(entity)?.[0] !== 'values' && <InferenceDeploymentParameters key={`deployment:${entity.id}`} draft={draft} change={change} entity={entity} reviewedResult={selectionChanged ? null : initialResult} />}
        </fieldset>}
    </aside>;
    const reviewTab = review?.tab || 'overview';
    return <section aria-label="Source deployment configuration" className="space-y-3">
        {error && <p role="alert" className="text-sm">{error} {!catalog && <Button size="xs" variant="secondary" onClick={() => setReload(value => value + 1)}>Retry source catalog</Button>}</p>}
        {stale && <div role="alert" className="space-y-2 text-sm"><p>The cluster source revision changed. This saved configuration remains visible, but edits cannot be applied against a different revision.</p><Button variant="secondary" disabled={disabled || saving} onClick={() => {
            setIntent(current => ({ ...current, sourceCommit: catalog.commit, sourceUnits: [], finalConfiguration: undefined, sourceRouter: { ...current.sourceRouter, values: [] } }));
            setDirty(true); setSelectionChanged(true);
        }}>Use current revision and discard source edits</Button></div>}
        <div className="inference-designer inference-workflow inference-workflow-2d">
            <InferenceFlowMap2D graph={graph} selected={selected} onSelect={select} onActivate={activate} onConfigure={configure} canConfigure={canConfigure} blocked={blocked} disabled={locked} focus={focus} onFocusOpened={onFocusOpened} onCloseInspector={() => setParameters(null)} inspectorFooter={parameters === 'node' ? applyActions : null} inspector={inspector || null} configuredCount={configuredCount} overlay={flowOverlay}
                toolbar={<>{loading && <span role="status" className="flex items-center gap-1.5 text-xs text-theme-muted"><Spinner size="xs" />Loading Guides for editing…</span>}{dirty && <Badge tone="warning">Unapplied changes</Badge>}</>} />
        </div>
        {!review?.open && applyActions && <Panel id="deployment-unapplied" padding="sm" className="border-amber-500/40 shadow-none">{applyActions}</Panel>}
        <Modal isOpen={Boolean(review?.open)} onClose={saving ? undefined : review?.onClose} closeOnBackdrop={!saving} closeOnEscape={!saving} title="Review & deploy" subtitle={dirty ? 'You have unapplied edits. Apply them to re-check resources before deploying.' : undefined} size="xxl" footer={dirty ? <div className="w-full">{applyActions}</div> : review?.footer}>
            <div className="space-y-4">
                <div role="tablist" aria-label="Review views" className="deployment-tabs">
                    {[['overview', 'Overview'], ['parameters', 'Parameters'], ['yaml', 'YAML']].map(([id, label]) => <Button key={id} role="tab" size="sm" variant="ghost" aria-selected={reviewTab === id} onClick={() => review.onTab(id)}>{label}</Button>)}
                </div>
                {error && <p role="alert" className="text-sm">{error}</p>}
                {reviewTab === 'overview' && review?.overview}
                {['parameters', 'yaml'].includes(reviewTab) && <fieldset disabled={locked} className="min-w-0 space-y-3">
                    {draft.finalConfiguration && <div className="flex justify-end"><Button size="sm" variant="dangerOutline" onClick={() => change({ finalConfiguration: undefined })}>Discard direct edits</Button></div>}
                    {reviewTab === 'parameters' && <InferenceGuidePresets embedded catalog={catalog} draft={draft} change={change} plan={plan} includedGuides={[...new Set([...(draft.selectedGuides || []), ...(plan?.guides || [])])]} />}
                    <InferenceDeploymentParameters draft={draft} change={change} tab={reviewTab} reviewedResult={selectionChanged ? null : initialResult} />
                </fieldset>}
            </div>
        </Modal>
    </section>;
}
