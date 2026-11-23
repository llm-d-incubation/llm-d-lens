import React, { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, Clock3, Download, Pencil, Settings } from 'lucide-react';
import { Badge, Button, Input, Modal, Panel, Select } from '../../../shared/ui';
import { downloadBlob } from '../../../shared/lib/download';
import { deploymentPlan, candidatePlan, overviewPlan, optimizationPlan, OPTIMIZATION_STATES } from '../model/deploymentPlan';
import { candidateLabel, OPTIMIZATION_CONTROLS } from '../api/agenticDeploymentBackend';
import { DeploymentPlanGraph } from './DeploymentPlanGraph';
import DeploymentPlanScene from './DeploymentPlanScene';

const runtimeFields = [
    ['max_model_len', 'Maximum context tokens', 256, 1048576, 1],
    ['gpu_memory_utilization', 'Accelerator memory utilization', 0.5, 0.98, 0.01],
    ['max_num_seqs', 'Maximum sequences', 1, 4096, 1],
    ['max_num_batched_tokens', 'Maximum batched tokens', 256, 131072, 1],
];

export function DeploymentPlanReview({ content = '', candidate, model, mode = 'manual', decisions = [], onEdit, onEditingChange, onRequirementsChange, onCheck, onBenchmark, onWorkload, requirements, disabled = false, validation, revision = 1, maxAccelerators, runtime, resourceEstimate, evidence, rationale }) {
    const [view, setView] = useState('2d'), [selected, setSelected] = useState('');
    const [editing, setEditing] = useState(false), [replicas, setReplicas] = useState(''), [tp, setTp] = useState('');
    const [error, setError] = useState(''), [applying, setApplying] = useState(false);
    const [runtimeDraft, setRuntimeDraft] = useState({});
    const [settingsTab, setSettingsTab] = useState('optimizations'), [intentDraft, setIntentDraft] = useState(null);
    const [focusedCapability, setFocusedCapability] = useState(''), [cacheGiB, setCacheGiB] = useState('');
    useEffect(() => { onEditingChange?.(editing); }, [editing, onEditingChange]);
    const parsed = useMemo(() => {
        try { return { plan: content ? deploymentPlan(content) : candidatePlan(candidate, model) }; }
        catch (failure) { return { error: failure.message, plan: { nodes: [], edges: [] } }; }
    }, [content, candidate, model]);
    const plan = parsed.plan;
    const overview = useMemo(() => decisions.length ? optimizationPlan(candidate, decisions) : overviewPlan(plan), [plan, candidate, decisions]);
    const enabled = decisions.filter(item => ['enabled', 'dependency'].includes(item.status));
    const overviewNode = overview.nodes.find(item => item.id === selected);
    const node = plan.nodes.find(item => item.id === selected)
        || (overviewNode?.role && plan.nodes.find(item => item.role === overviewNode.role))
        || plan.nodes.find(item => item.tier === 1 && item.role !== 'encode' && (!content || item.containers?.some(container => container.name === 'modelserver'))) || plan.nodes[0];
    const selectedNode = view === 'resources' ? plan.nodes.find(item => item.id === selected) : overviewNode;
    const select = id => {
        if (editing && dirty) { setError('Apply or cancel the pending edit before selecting another component.'); return; }
        setSelected(id); setEditing(false); setError('');
    };
    const prefix = node?.role === 'prefill' ? 'prefill_' : '';
    const previousReplicas = candidate?.[`${prefix}replicas`] ?? node?.document?.spec?.replicas;
    const previousTp = candidate?.[`${prefix}tensor_parallel_size`];
    const dirty = (replicas !== '' && Number(replicas) !== previousReplicas)
        || (tp !== '' && Number(tp) !== previousTp)
        || (cacheGiB !== '' && Number(cacheGiB) !== candidate?.native_cpu_cache_gib)
        || Object.entries(runtimeDraft).some(([key, value]) => value !== '' && Number(value) !== runtime?.[key]);
    const changedResources = previousReplicas && previousTp && (replicas || tp)
        ? Number(replicas || previousReplicas) * Number(tp || previousTp) - previousReplicas * previousTp : null;
    const currentResources = candidate ? candidate.replicas * candidate.tensor_parallel_size + (candidate.prefill_replicas || 0) * (candidate.prefill_tensor_parallel_size || 0) : null;
    const overBudget = maxAccelerators != null && changedResources != null && currentResources + changedResources > maxAccelerators;
    const apply = async () => {
        const patch = {};
        for (const [key, value] of [['replicas', replicas], ['tensorParallelSize', tp]]) {
            if (value === '' || Number(value) === (key === 'replicas' ? previousReplicas : previousTp)) continue;
            if (!Number.isSafeInteger(Number(value)) || Number(value) < 1 || Number(value) > 128) {
                setError('Enter whole numbers between 1 and 128.'); return;
            }
            patch[key] = Number(value);
        }
        for (const [key, label, min, max, step] of runtimeFields) {
            if (runtimeDraft[key] == null || runtimeDraft[key] === '' || Number(runtimeDraft[key]) === runtime?.[key]) continue;
            const value = Number(runtimeDraft[key]);
            if (!Number.isFinite(value) || value < min || value > max || (step === 1 && !Number.isSafeInteger(value))) {
                setError(`${label} must be ${step === 1 ? 'a whole number ' : ''}between ${min} and ${max}.`); return;
            }
            patch[key] = value;
        }
        if (cacheGiB !== '' && Number(cacheGiB) !== candidate?.native_cpu_cache_gib) {
            if (!Number.isFinite(Number(cacheGiB)) || Number(cacheGiB) <= 0 || Number(cacheGiB) > 1024) {
                setError('CPU KV cache must be greater than zero and at most 1024 GiB per replica.'); return;
            }
            patch.native_cpu_cache_gib = Number(cacheGiB);
        }
        if (!Object.keys(patch).length || overBudget) return;
        setApplying(true); setError('');
        try {
            await onEdit({ node, patch });
            setEditing(false); setReplicas(''); setTp(''); setRuntimeDraft({});
        } catch (failure) { setError(failure.message || 'Unable to update configuration.'); }
        finally { setApplying(false); }
    };
    const beginEdit = (tab = 'optimizations', capability = '') => {
        setReplicas(String(previousReplicas ?? '')); setTp(String(previousTp ?? ''));
        setRuntimeDraft(Object.fromEntries(runtimeFields.map(([key]) => [key, String(runtime?.[key] ?? '')])));
        setIntentDraft(requirements ? { ...requirements, requiredFeatures: [...requirements.requiredFeatures], forbiddenFeatures: [...requirements.forbiddenFeatures] } : null);
        setFocusedCapability(capability); setCacheGiB(String(candidate?.native_cpu_cache_gib ?? ''));
        setSettingsTab(tab);
        setEditing(true); setError('');
    };
    const control = (id, setting) => setIntentDraft(current => ({
        ...current,
        requiredFeatures: [...current.requiredFeatures.filter(item => item !== id), ...(setting === 'required' ? [id] : [])],
        forbiddenFeatures: [...current.forbiddenFeatures.filter(item => item !== id), ...(setting === 'forbidden' ? [id] : [])],
    }));
    const intentDirty = intentDraft && JSON.stringify(intentDraft) !== JSON.stringify(requirements);
    const applyIntent = async () => {
        setApplying(true); setError('');
        try { if (await onRequirementsChange(intentDraft)) setEditing(false); else setError('Unable to rebuild the plan. Your settings are preserved; see the planning error.'); }
        catch (failure) { setError(failure.message); }
        finally { setApplying(false); }
    };
    const editCapability = id => beginEdit(id === 'external_kv_cache' && candidate?.native_cpu_cache_gib ? 'component' : 'optimizations', id);
    const toggleCapability = async (id, disable) => {
        setApplying(true); setError('');
        try {
            if (!await onRequirementsChange({
                ...requirements,
                requiredFeatures: requirements.requiredFeatures.filter(item => item !== id),
                forbiddenFeatures: [...requirements.forbiddenFeatures.filter(item => item !== id), ...(disable ? [id] : [])],
            })) setError('The optimization change could not be applied. See the planning error and retry.');
        } catch (failure) { setError(failure.message); }
        finally { setApplying(false); }
    };
    const editable = onEdit && node?.tier === 1 && node.role !== 'encode' && (!content || node.containers?.some(container => container.name === 'modelserver'));
    return <Panel title="Deployment plan" actions={<Badge tone={validation?.status === 'resources_checked' ? 'success' : 'neutral'}>{validation?.status === 'resources_checked' ? 'Configuration checked' : 'Checks pending'}</Badge>} className="min-w-0 shadow-none">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0"><h3 className="text-lg font-semibold">{candidateLabel(candidate)}</h3><p className="mt-1 text-sm text-theme-muted">{resourceEstimate?.accelerators ?? currentResources ?? 'Unknown'} accelerators{resourceEstimate?.available_accelerators != null ? ` / ${resourceEstimate.available_accelerators} available` : ''} / {enabled.length} enabled optimizations</p></div>
            <Button variant="secondary" aria-label="Deployment settings" title="Deployment settings" disabled={disabled} onClick={() => beginEdit(requirements ? 'optimizations' : 'component')}><Settings size={16} />Edit plan</Button>
        </div>
        <p className="mb-4 text-sm leading-relaxed text-theme-muted">{mode === 'manual' ? 'This strategy prioritizes compatible optimization coverage, not measured performance.' : 'This is a recommendation within your workload and resource limits, not a performance guarantee.'}</p>
        {(rationale || evidence) && <details className="mb-5 text-sm"><summary className="cursor-pointer font-medium">Why this plan?</summary><p className="mt-2 leading-relaxed text-theme-muted">{rationale || 'Selected from validated configurations.'}</p>{evidence && <p className="mt-2 text-theme-muted">{evidence}</p>}</details>}
        <div className="mb-5 flex flex-wrap items-center gap-2 border-b border-theme-border pb-4" role="group" aria-label="Configuration view">
            {[['2d', '2D overview'], ['reason', 'Optimization details'], ['validation', 'Checks']].map(([id, label]) => <Button key={id} variant={view === id ? 'secondary' : 'ghost'} aria-pressed={view === id} onClick={() => setView(id)}>{label}</Button>)}
            <Select className="w-auto" aria-label="Technical view" value={['3d', 'yaml', 'resources'].includes(view) ? view : ''} onChange={event => setView(event.target.value)}>
                <option value="" disabled>Technical views</option>
                {content && <><option value="resources">Resources</option><option value="yaml">YAML</option></>}
                <option value="3d">3D explorer</option>
            </Select>
            {content && view === 'yaml' && <Button size="sm" variant="ghost" className="ml-auto" onClick={() => downloadBlob(new Blob([content], { type: 'application/yaml' }), `deployment-v${revision}.yaml`)}><Download size={14} />Download YAML</Button>}
        </div>
        {parsed.error && <p role="alert">Unable to display resources: {parsed.error}. Do not use the graph as a configuration reference.</p>}
        {error && !editing && <p role="alert" className="mb-3 text-sm">{error}</p>}
        {view === 'reason' && <section className="divide-y divide-theme-border" aria-label="Optimization decisions">
            {decisions.length ? decisions.map((item, index) => {
                const state = OPTIMIZATION_STATES[item.status];
                return <div key={item.capability_id || item.id || index} className="flex items-start gap-3 py-3">
                    <Badge tone={state?.tone || 'neutral'}>{state?.label || item.status}</Badge>
                    <details className="min-w-0 flex-1">
                        <summary className="cursor-pointer text-sm font-medium">{item.label || item.capability_id || item.id}<span className="ml-3 text-xs font-normal text-theme-muted">{state?.action || 'Review evidence'}</span></summary>
                        <p className="mt-2 text-sm text-theme-muted">{item.reason}</p>
                    </details>
                    {item.status === 'unknown' && <Button size="xs" variant="secondary" onClick={() => setView('validation')}>View checks</Button>}
                    {onRequirementsChange && item.status !== 'unsupported' && <Button size="icon" variant="ghost" disabled={disabled} title={`Edit ${item.label}`} aria-label={`Edit ${item.label}`} onClick={() => editCapability(item.capability_id)}><Pencil size={14} /></Button>}
                </div>;
            }) : <p className="py-4 text-sm text-theme-muted">No optimization decisions recorded.</p>}
        </section>}
        {view === 'validation' && <section aria-label="Configuration validation" className="space-y-4">
            <div className="divide-y divide-theme-border">
                {[
                    { title: 'Configuration', detail: 'Memory, accelerators, dependencies and generated resources.', passed: validation?.status === 'resources_checked',
                        status: validation?.status === 'resources_checked' ? 'Passed' : 'Needs check', action: onCheck && <Button size="sm" variant="secondary" disabled={disabled} onClick={onCheck}>{validation?.status === 'resources_checked' ? 'Recheck' : 'Check now'}</Button> },
                    { title: 'Cluster and startup', detail: 'Admission and pod readiness. Inspect failures in Pods & Logs.', status: 'After approval', action: <span className="text-xs text-theme-muted">Confirm deployment to run</span> },
                    { title: 'Workload performance', detail: 'Measure latency and throughput with representative requests.', status: 'Benchmark required',
                        action: onBenchmark ? <Button size="sm" variant="secondary" disabled={disabled} onClick={onBenchmark}>Set up benchmark</Button> : <span className="text-xs text-theme-muted">Evaluate after deployment</span> },
                ].map(check => <div key={check.title} className="flex flex-wrap items-center gap-3 py-4">
                    {check.passed ? <CheckCircle2 size={18} aria-label="Passed" /> : <Clock3 size={18} className="text-theme-muted" aria-hidden="true" />}
                    <div className="min-w-48 flex-1"><h4 className="text-sm font-medium">{check.title}</h4><p className="mt-1 text-xs text-theme-muted">{check.detail}</p></div>
                    <Badge tone={check.passed ? 'success' : 'neutral'}>{check.status}</Badge>{check.action}
                </div>)}
            </div>
            {validation?.warnings?.length > 0 && <details><summary className="cursor-pointer text-sm"><Badge tone="warning">{validation.warnings.length} warnings</Badge></summary><ul className="mt-2 space-y-2 text-sm">{validation.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul></details>}
            <details><summary className="cursor-pointer text-xs text-theme-muted">Technical evidence / revision {revision}</summary>
                <div className="mt-3 grid gap-4 text-xs md:grid-cols-2">
                    <div><h4 className="font-medium">Recorded checks</h4><ul className="mt-2 space-y-2 text-theme-muted">{validation?.checks?.length ? validation.checks.map(check => <li key={check}>{check}</li>) : <li>No checks recorded.</li>}</ul></div>
                    <div><h4 className="font-medium">Still unverified</h4><ul className="mt-2 space-y-2 text-theme-muted">{(validation?.pending || ['Cluster admission', 'Runtime readiness', 'Workload performance']).map(check => <li key={check}>{check}</li>)}</ul></div>
                </div>
                <p className="mt-3 text-xs text-theme-muted">{evidence} {mode === 'agentic' ? rationale : ''}</p>
                <p className="mt-2 text-xs text-theme-muted">Source: {candidate?.provider_ref || 'Not recorded'}. Editing requires new checks and approval.</p>
            </details>
            {onWorkload && <Button size="sm" variant="link" disabled={disabled} onClick={onWorkload}>Edit workload and limits</Button>}
        </section>}
        {view === 'reason' || view === 'validation' ? null : view === 'yaml'
            ? <pre tabIndex={0} aria-label="Generated YAML" className="max-h-[36rem] overflow-auto rounded-lg border border-theme-border bg-theme-bg p-5 text-xs leading-relaxed">{content}</pre>
            : view === '3d'
                ? <DeploymentPlanScene plan={overview} selected={selected} onSelect={select} />
            : !parsed.error && <DeploymentPlanGraph plan={view === 'resources' ? plan : overview} selected={selected} onSelect={select} onEdit={view === '2d' && onRequirementsChange ? editCapability : undefined} onToggle={view === '2d' && onRequirementsChange ? toggleCapability : undefined} disabled={disabled || applying} />}
        {['3d', '2d', 'resources'].includes(view) && selectedNode && <div className="mt-3 flex flex-wrap items-center gap-2 text-xs"><span className="text-theme-muted">{selectedNode.name}: {selectedNode.detail}</span>{!selectedNode.capability && (selectedNode.role || selectedNode.id === node?.id) && editable && <Button size="xs" variant="link" disabled={disabled} onClick={() => beginEdit('component')}>Component settings</Button>}</div>}
        <Modal isOpen={editing} onClose={() => setEditing(false)} closeOnBackdrop={!applying} closeOnEscape={!applying} title={decisions.find(item => item.capability_id === focusedCapability)?.label || 'Deployment settings'} variant="drawer" size="sm" footer={<>
            <Button variant="secondary" disabled={applying} onClick={() => setEditing(false)}>Cancel</Button>
            <Button isLoading={applying} disabled={disabled || (settingsTab === 'optimizations' ? !intentDirty : !dirty || overBudget || !editable)} onClick={settingsTab === 'optimizations' ? applyIntent : apply}>Apply and check</Button>
        </>}>
            <div className="mb-4 flex gap-2">
                {onRequirementsChange && <Button size="sm" variant={settingsTab === 'optimizations' ? 'primary' : 'secondary'} disabled={applying || dirty} onClick={() => setSettingsTab('optimizations')}>Optimizations</Button>}
                {onEdit && <Button size="sm" variant={settingsTab === 'component' ? 'primary' : 'secondary'} disabled={applying || intentDirty} onClick={() => setSettingsTab('component')}>Components</Button>}
            </div>
            <fieldset disabled={disabled || applying} className="space-y-4">
                {settingsTab === 'optimizations' && intentDraft ? <>
                    <p className="text-xs text-theme-muted">Automatic includes applicable compatible optimizations in Manual; Agentic chooses for your goal. Required options must pass checks.</p>
                    {OPTIMIZATION_CONTROLS.filter(([id]) => !focusedCapability || focusedCapability === id).map(([id, label]) => <label key={id} className="block text-sm">{label}<Select className="mt-1" value={intentDraft.requiredFeatures.includes(id) ? 'required' : intentDraft.forbiddenFeatures.includes(id) ? 'forbidden' : 'auto'} onChange={event => control(id, event.target.value)}><option value="auto">Automatic</option><option value="required">Required</option><option value="forbidden">Disabled</option></Select></label>)}
                    {focusedCapability && <p className="text-xs text-theme-muted">{decisions.find(item => item.capability_id === focusedCapability)?.reason} Use Components for replicas, parallelism and engine parameters. Dependencies are rechecked on apply.</p>}
                    <details><summary className="cursor-pointer text-sm">Optional benefit hints and tradeoffs</summary><div className="mt-3 space-y-3">
                        <p className="text-xs text-theme-muted">These hints inform expected benefit, not technical compatibility. Not specified lets the AI goal or token counts decide.</p>
                        <label className="block text-sm">Shared prompts or documents<Select value={intentDraft.prefixReuse} onChange={event => setIntentDraft(current => ({ ...current, prefixReuse: event.target.value }))}><option value="yes">Yes</option><option value="no">No</option><option value="unknown">Not specified</option></Select></label>
                        <label className="block text-sm">Long inputs with short answers<Select value={intentDraft.useCase} onChange={event => setIntentDraft(current => ({ ...current, useCase: event.target.value }))}><option value="long-inputs">Yes</option><option value="general-chat">No</option><option value="unknown">Not specified</option>{intentDraft.useCase === 'summarization' && <option value="summarization">Document summarization</option>}</Select></label>
                        <label className="block text-sm">Optimize for (topology and alternatives)<Select value={intentDraft.objective} onChange={event => setIntentDraft(current => ({ ...current, objective: event.target.value }))}><option value="balanced">No preference</option><option value="throughput">Throughput</option><option value="latency">Latency</option><option value="cost">Fewer resources</option><option value="reliability">Availability</option></Select></label>
                    </div></details>
                </> : <>
                    <label className="block text-sm">Component<Select aria-label="Inspect component" value={node?.id || ''} disabled={dirty} onChange={event => { setSelected(event.target.value); setReplicas(''); setTp(''); setRuntimeDraft({}); }}>{plan.nodes.filter(item => item.tier === 1 && item.role !== 'encode' && (!content || item.containers?.some(container => container.name === 'modelserver'))).map(item => <option key={item.id} value={item.id}>{item.role}: {item.name}</option>)}</Select></label>
                    {editable ? <>
                        {candidate?.native_cpu_cache_gib && <label className="block text-sm">CPU KV cache (GiB per model replica)<Input type="number" min="0.01" max="1024" step="any" value={cacheGiB} onChange={event => setCacheGiB(event.target.value)} /><span className="text-xs text-theme-muted">All serving roles; limited to 25% of discovered free host memory in total.</span></label>}
                        <label className="block text-sm">Replicas<Input type="number" min="1" max="128" step="1" placeholder={String(previousReplicas || '')} value={replicas} onChange={event => setReplicas(event.target.value)} /></label>
                        <label className="block text-sm">Accelerators per replica (tensor parallelism)<Input type="number" min="1" max="128" step="1" placeholder={String(previousTp || '')} value={tp} onChange={event => setTp(event.target.value)} /></label>
                        {runtime && <details><summary className="cursor-pointer text-sm">Runtime parameters</summary><div className="mt-3 space-y-3">{runtimeFields.map(([key, label, min, max, step]) => <label key={key} className="block text-sm">{label}<Input type="number" min={min} max={max} step={step} placeholder={String(runtime[key] ?? '')} value={runtimeDraft[key] ?? ''} onChange={event => setRuntimeDraft(current => ({ ...current, [key]: event.target.value }))} /></label>)}</div></details>}
                        <p className="text-xs text-theme-muted">{changedResources == null ? 'Resources are checked on apply.' : `Planned accelerators after this edit: ${currentResources + changedResources}.`}</p>
                        {overBudget && <p role="alert">Exceeds the {maxAccelerators}-accelerator limit.</p>}
                    </> : <p className="text-sm">Select a model-serving component to edit.</p>}
                </>}
                {error && <p role="alert" className="text-sm">{error}</p>}
            </fieldset>
        </Modal>
    </Panel>;
}
