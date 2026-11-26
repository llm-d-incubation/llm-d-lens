import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle,
    Check,
    CheckCircle2,
    ChevronRight,
    ClipboardCopy,
    Clock3,
    Download,
    Pencil,
    RefreshCw,
    Settings,
    ShieldAlert,
    XCircle,
} from 'lucide-react';
import { Badge, Button, Checkbox, Input, Label, Modal, Panel, Select, Spinner } from '../../../shared/ui';
import { FormError } from '../../../shared/ui/FormError';
import { cn } from '../../../shared/lib/cn';
import { copyText } from '../../../shared/lib/clipboard';
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

function handoffRuleLabel(rule = {}) {
    const verb = rule.verb || 'use';
    const resource = rule.resource || rule.path || rule.nonResourceURL || 'resource';
    const group = rule.apiGroup || rule.group;
    const target = group ? `${resource}.${group}` : resource;
    const subresource = rule.subresource ? `/${rule.subresource}` : '';
    const scope = rule.namespace ? ` in ${rule.namespace}` : '';
    return `${verb} ${target}${subresource}${scope}`.trim();
}

function handoffOperationId(entry) {
    return typeof entry === 'string' ? entry : entry?.id;
}

function handoffOperationTitle(entry) {
    if (typeof entry === 'string') return entry;
    return entry?.title || entry?.id || 'Operation';
}

function handoffTargetLabel(entry = {}) {
    if (typeof entry === 'string') return entry;
    const kind = entry.kind || 'resource';
    const target = entry.namespace ? `${entry.namespace}/${entry.name}` : entry.name;
    return target ? `${kind}: ${target}` : kind;
}

function pluralCount(count, singular, plural = `${singular}s`) {
    return `${count} ${count === 1 ? singular : plural}`;
}

function HandoffOperationRow({ entry, tone = 'danger', statusLabel, ariaLabel, rules = [], children }) {
    const id = handoffOperationId(entry);
    const title = handoffOperationTitle(entry);
    const Icon = tone === 'success' ? CheckCircle2 : tone === 'warning' ? AlertTriangle : XCircle;
    return (
        <li className={cn(
            'border-l-2 pl-3',
            tone === 'success'
                ? 'border-emerald-500/70'
                : tone === 'warning'
                    ? 'border-amber-500/80'
                    : 'border-rose-500/80',
        )}>
            <details className="group">
                <summary
                    title="Expand for details"
                    className="-ml-3 flex cursor-pointer list-none items-start gap-2 py-1 pl-3 pr-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60 [&::-webkit-details-marker]:hidden"
                >
                    <Icon
                        size={14}
                        role="img"
                        aria-label={ariaLabel || statusLabel}
                        className={cn(
                            'mt-0.5 shrink-0',
                            tone === 'success'
                                ? 'text-emerald-600 dark:text-emerald-400'
                                : tone === 'warning'
                                    ? 'text-amber-600 dark:text-amber-400'
                                    : 'text-rose-600 dark:text-rose-400',
                        )}
                    />
                    <span className="min-w-0 flex-1">
                        <span className="block text-xs font-medium text-theme-text">{title}</span>
                        {id && id !== title && (
                            <span className="block break-all font-mono text-[11px] text-theme-muted">{id}</span>
                        )}
                    </span>
                    <span className={cn(
                        'shrink-0 text-[11px] font-medium',
                        tone === 'success'
                            ? 'text-emerald-700 dark:text-emerald-300'
                            : tone === 'warning'
                                ? 'text-amber-700 dark:text-amber-300'
                                : 'text-rose-700 dark:text-rose-300',
                    )}>
                        {statusLabel}
                    </span>
                    <ChevronRight
                        size={13}
                        aria-hidden="true"
                        className="mt-0.5 shrink-0 text-theme-muted transition-transform motion-reduce:transition-none group-open:rotate-90"
                    />
                </summary>
                <div className="pb-2">
                    {rules.length > 0 && (
                        <ul className="mt-1.5 space-y-1" aria-label="Missing Kubernetes permissions">
                            {rules.map((rule, index) => (
                                <li key={`${rule.operationId || rule.resource || 'rule'}-${index}`} className="break-all font-mono text-[11px] text-theme-muted">
                                    {handoffRuleLabel(rule)}
                                </li>
                            ))}
                        </ul>
                    )}
                    {children}
                </div>
            </details>
        </li>
    );
}

/**
 * Reusable Orchestration administrator script handoff for non-admin deployment creation
 * (mode="bootstrap") and two-phase deployment deletion (mode="cleanup").
 */
export function DeploymentPermissionHandoff({
    mode = 'bootstrap',
    report = null,
    handoff = null,
    namespace = '',
    script = '',
    filename,
    instructions = '',
    warnings = [],
    error = '',
    busy = false,
    verifying = false,
    generating = false,
    identity = null,
    subjectOverride = null,
    onSubjectOverrideChange,
    onGenerateScript,
    onDownloadScript,
    onCopyScript,
    onPrimaryAction,
    primaryActionLabel,
    onDismiss,
    confirmed = false,
    onConfirmChange,
    onConfirmDelete,
}) {
    const [copied, setCopied] = useState(false);
    const [localError, setLocalError] = useState('');
    const copyTimerRef = useRef(null);

    useEffect(() => () => {
        if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    }, []);

    const isCleanup = mode === 'cleanup';
    const resolvedNamespace = namespace || handoff?.namespace || report?.namespace || '';
    const resolvedScript = typeof script === 'string' && script
        ? script
        : (typeof handoff?.script === 'string' ? handoff.script : '');
    const defaultFilename = isCleanup ? 'lens-deployment-cleanup.sh' : 'lens-deployment-bootstrap.sh';
    const resolvedFilename = filename || handoff?.scriptFilename || defaultFilename;
    const resolvedInstructions = instructions || handoff?.instructions || (
        isCleanup
            ? 'Ask an Orchestration administrator to run this idempotent cleanup script once, then click Re-check & Delete.'
            : 'Ask an Orchestration administrator to run this idempotent setup script once, then click Re-check & Deploy. It contains no kubeconfig or user secrets.'
    );
    const resolvedIdentity = identity || handoff?.identity || report?.identity || null;
    const overrideRequired = !isCleanup && Boolean(
        resolvedIdentity?.overrideRequired
        || resolvedIdentity?.kind === 'unknown'
        || resolvedIdentity?.source === 'override',
    );
    const overrideKind = subjectOverride?.kind || (resolvedIdentity?.source === 'override' ? resolvedIdentity.kind : 'ServiceAccount') || 'ServiceAccount';
    const overrideName = subjectOverride?.name ?? (resolvedIdentity?.source === 'override' ? resolvedIdentity.name || '' : '');
    const overrideNamespace = subjectOverride?.namespace ?? (resolvedIdentity?.source === 'override' ? resolvedIdentity.namespace || '' : '');
    const overrideComplete = Boolean(overrideName.trim()) && (overrideKind !== 'ServiceAccount' || Boolean(overrideNamespace.trim()));

    const marker = handoff?.marker || report?.marker || {};
    const canVerifyMarker = Boolean(handoff?.canVerifyMarker ?? true);
    const confirmRequired = isCleanup && Boolean(handoff?.confirmRequired || handoff?.unreachable || !canVerifyMarker);
    const verifyStatus = handoff?.status || report?.status || (isCleanup ? 'pending_admin_cleanup' : 'admin_required');
    const markerStatus = marker.status || (isCleanup && !canVerifyMarker ? 'unconfirmed' : verifyStatus);
    const verified = Boolean(handoff?.verified || report?.sufficient || verifyStatus === 'verified' || verifyStatus === 'deleted');
    const isStale = !verified && (markerStatus === 'stale' || verifyStatus === 'stale');
    const isPartial = !verified && (markerStatus === 'partial' || verifyStatus === 'partial');
    const isUnconfirmed = isCleanup && !verified && (!canVerifyMarker || markerStatus === 'unconfirmed');
    const shouldExpandDetails = !verified;
    const [detailsOpen, setDetailsOpen] = useState(shouldExpandDetails);

    useEffect(() => {
        setDetailsOpen(shouldExpandDetails);
    }, [shouldExpandDetails]);

    const handleDetailsToggle = (event) => {
        if (event.target !== event.currentTarget) return;
        setDetailsOpen(event.currentTarget.open);
    };

    const adminOperations = Array.isArray(report?.adminOperations) && report.adminOperations.length
        ? report.adminOperations
        : (Array.isArray(handoff?.operations) ? handoff.operations : []);
    const userOperations = Array.isArray(report?.userOperations)
        ? report.userOperations
        : (Array.isArray(handoff?.completedOperations) ? handoff.completedOperations : []);
    const missingRules = Array.isArray(handoff?.missing)
        ? handoff.missing
        : (Array.isArray(report?.missing) ? report.missing : []);
    const remainingIds = new Set(
        (Array.isArray(handoff?.remainingOperations) ? handoff.remainingOperations : []).map(handoffOperationId),
    );
    const hasVerifyCheck = Array.isArray(handoff?.remainingOperations);

    const mergedWarnings = [];
    for (const list of [warnings, report?.warnings, handoff?.warnings]) {
        if (!Array.isArray(list)) continue;
        for (const item of list) {
            if (item && !mergedWarnings.includes(item)) mergedWarnings.push(item);
        }
    }

    const badgeMeta = verified
        ? { label: 'Verified', tone: 'success' }
        : isStale
            ? { label: 'Stale', tone: 'danger' }
            : isPartial
                ? { label: 'Partial', tone: 'warning' }
                : isUnconfirmed
                    ? { label: 'Unconfirmed', tone: 'warning' }
                    : { label: 'Awaiting Orchestration administrator', tone: 'warning' };

    const activeBusy = busy || verifying || generating;
    const primaryLabel = primaryActionLabel || (isCleanup ? 'Re-check & Delete' : 'Re-check & Deploy');

    const rulesFor = (id) => missingRules.filter((rule) => !rule.operationId || rule.operationId === id);

    const detailParts = [];
    if (adminOperations.length > 0) {
        if (isCleanup) {
            detailParts.push(`${adminOperations.length} remaining`);
        } else if (verified) {
            detailParts.push(`${adminOperations.length}/${adminOperations.length} done`);
        } else {
            detailParts.push(`${adminOperations.length} Orchestration administrator`);
        }
    }
    if (userOperations.length > 0) {
        detailParts.push(isCleanup ? `${userOperations.length} cleaned` : `${userOperations.length} ready`);
    }
    const detailSummaryLabel = detailParts.join(' · ');

    const handleDownload = () => {
        try {
            setLocalError('');
            if (onDownloadScript) onDownloadScript(resolvedScript, resolvedFilename);
            else downloadBlob(new Blob([resolvedScript], { type: 'text/x-shellscript' }), resolvedFilename);
        } catch {
            setLocalError('Could not download the script. Copy it instead.');
        }
    };

    const handleCopy = async () => {
        setLocalError('');
        try {
            if (onCopyScript) await onCopyScript(resolvedScript);
            else await copyText(resolvedScript);
            setCopied(true);
            if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
            copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
        } catch {
            setLocalError('Could not copy the script. Download it instead.');
        }
    };

    const updateOverride = (patch) => {
        onSubjectOverrideChange?.({
            kind: overrideKind,
            name: overrideName,
            namespace: overrideNamespace,
            ...patch,
        });
    };

    return (
        <Panel
            padding="sm"
            tone="soft"
            className={cn(
                'border-l-4 shadow-none',
                verified
                    ? 'border-l-emerald-500'
                    : isStale
                        ? 'border-l-rose-500'
                        : 'border-l-amber-500',
            )}
            title={(
                <span className="flex items-center gap-2">
                    <ShieldAlert
                        size={15}
                        className={cn(
                            verified
                                ? 'text-emerald-600 dark:text-emerald-400'
                                : isStale
                                    ? 'text-rose-600 dark:text-rose-400'
                                    : 'text-amber-600 dark:text-amber-400',
                        )}
                        aria-hidden="true"
                    />
                    {isCleanup ? 'Orchestration administrator namespace cleanup' : 'Orchestration administrator namespace setup'}
                </span>
            )}
            actions={(
                <div className="flex flex-wrap items-center gap-1.5">
                    {resolvedNamespace && (
                        <Badge tone="neutral" size="xs">
                            <span className="font-mono">{resolvedNamespace}</span>
                        </Badge>
                    )}
                    <Badge tone={badgeMeta.tone} size="xs">{badgeMeta.label}</Badge>
                </div>
            )}
        >
            <div className="space-y-3" role="status" aria-live="polite">
                {verified ? (
                    <p className="flex items-center gap-2 border-l-2 border-emerald-500 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-800 dark:text-emerald-200">
                        <CheckCircle2 size={14} className="shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                        <span>
                            {isCleanup
                                ? 'Orchestration administrator cleanup verified. The deployment record can now be deleted.'
                                : 'Orchestration administrator namespace setup verified. Deployment can proceed now.'}
                        </span>
                    </p>
                ) : isStale ? (
                    <p className="flex items-start gap-2 border-l-2 border-rose-500 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-800 dark:text-rose-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            {isCleanup
                                ? `The cleanup marker belongs to an outdated cleanup plan. Ask an Orchestration administrator to run the updated script below, then click ${primaryLabel}.`
                                : `The setup marker belongs to an older deployment plan. Ask an Orchestration administrator to run the updated script below, then click ${primaryLabel}.`}
                        </span>
                    </p>
                ) : isPartial ? (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            Orchestration administrator action is partial. Re-run the script below for the remaining work, then click <span className="font-semibold">{primaryLabel}</span>.
                        </span>
                    </p>
                ) : isUnconfirmed ? (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            {handoff?.detail || 'No readable marker namespace is available to confirm Orchestration administrator cleanup automatically.'} Ask an Orchestration administrator to run the cleanup script below, then re-check or confirm record deletion below once cleanup is complete.
                        </span>
                    </p>
                ) : (
                    <p className="max-w-[68ch] text-xs leading-relaxed text-theme-muted">
                        {isCleanup
                            ? (handoff?.detail || 'In-namespace workloads were cleaned with your kubeconfig, but deleting the Kubernetes namespace and cluster role binding requires an Orchestration administrator.')
                            : `Your kubeconfig cannot create or configure namespace ${resolvedNamespace || 'for this deployment'} directly. Have an Orchestration administrator run the setup script once, then click ${primaryLabel} so Lens can deploy the model and copy secrets.`}
                    </p>
                )}

                {mergedWarnings.map((item, index) => (
                    <p
                        key={`${index}-${item}`}
                        className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
                    >
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">{item}</span>
                    </p>
                ))}

                {overrideRequired && (
                    <div className="space-y-2.5 rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2.5">
                        <p className="text-xs font-semibold text-theme-text">Kubernetes identity override</p>
                        <p className="text-xs leading-relaxed text-theme-muted">
                            Lens could not automatically detect the Kubernetes subject for your kubeconfig. Enter the ServiceAccount, User, or Group that should receive namespace permissions.
                        </p>
                        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                            <div>
                                <Label htmlFor="deploy-subject-kind">Subject kind</Label>
                                <Select
                                    id="deploy-subject-kind"
                                    value={overrideKind}
                                    disabled={activeBusy}
                                    onChange={(event) => updateOverride({ kind: event.target.value })}
                                >
                                    <option value="ServiceAccount">ServiceAccount</option>
                                    <option value="User">User</option>
                                    <option value="Group">Group</option>
                                </Select>
                            </div>
                            <div>
                                <Label htmlFor="deploy-subject-name">Subject name</Label>
                                <Input
                                    id="deploy-subject-name"
                                    value={overrideName}
                                    disabled={activeBusy}
                                    placeholder={overrideKind === 'ServiceAccount' ? 'lens-deployer' : 'user@example.com'}
                                    onChange={(event) => updateOverride({ name: event.target.value })}
                                />
                            </div>
                            {overrideKind === 'ServiceAccount' && (
                                <div>
                                    <Label htmlFor="deploy-subject-namespace">ServiceAccount namespace</Label>
                                    <Input
                                        id="deploy-subject-namespace"
                                        value={overrideNamespace}
                                        disabled={activeBusy}
                                        placeholder="team-namespace"
                                        onChange={(event) => updateOverride({ namespace: event.target.value })}
                                    />
                                </div>
                            )}
                        </div>
                        {onGenerateScript && (
                            <div>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    isLoading={generating}
                                    disabled={activeBusy || !overrideComplete}
                                    onClick={() => onGenerateScript({
                                        kind: overrideKind,
                                        name: overrideName.trim(),
                                        ...(overrideKind === 'ServiceAccount' ? { namespace: overrideNamespace.trim() } : {}),
                                    })}
                                >
                                    <Download size={13} aria-hidden="true" /> {resolvedScript ? 'Regenerate script' : 'Generate script'}
                                </Button>
                            </div>
                        )}
                    </div>
                )}

                {!verified && (resolvedScript || (onGenerateScript && !overrideRequired)) && (
                    <div className="space-y-2 rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2.5">
                        <p className="text-xs font-semibold text-theme-text">
                            {isCleanup ? 'Orchestration administrator cleanup script' : 'Orchestration administrator setup script'}
                        </p>
                        <p className="max-w-[68ch] text-xs leading-relaxed text-theme-muted">{resolvedInstructions}</p>
                        <div className="flex flex-wrap gap-2">
                            {resolvedScript ? (
                                <>
                                    <Button variant="outline" size="sm" onClick={handleDownload} disabled={activeBusy}>
                                        <Download size={13} aria-hidden="true" /> Download .sh
                                    </Button>
                                    <Button variant="outline" size="sm" onClick={handleCopy} disabled={activeBusy}>
                                        {copied
                                            ? <Check size={13} className="text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                                            : <ClipboardCopy size={13} aria-hidden="true" />}
                                        {copied ? 'Copied' : 'Copy script'}
                                    </Button>
                                </>
                            ) : (
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => onGenerateScript?.()}
                                    isLoading={generating}
                                    disabled={activeBusy || !onGenerateScript}
                                >
                                    <Download size={13} aria-hidden="true" /> Generate script
                                </Button>
                            )}
                        </div>
                    </div>
                )}

                <div className="flex flex-wrap items-center gap-2 border-t border-theme-border pt-3">
                    <Button
                        variant="sky"
                        size="sm"
                        onClick={onPrimaryAction}
                        isLoading={verifying}
                        disabled={activeBusy || !onPrimaryAction}
                    >
                        <RefreshCw size={13} aria-hidden="true" /> {primaryLabel}
                    </Button>
                    {onDismiss && (
                        <Button
                            variant="ghost"
                            size="sm"
                            onClick={onDismiss}
                            disabled={activeBusy}
                        >
                            Dismiss
                        </Button>
                    )}
                </div>

                <FormError message={error || localError} />

                {(verifying || generating) && (
                    <p className="flex items-center gap-2 text-[11px] text-theme-muted">
                        <Spinner size="xs" /> {generating ? 'Generating Orchestration administrator script…' : 'Checking cluster status…'}
                    </p>
                )}

                {isCleanup && !canVerifyMarker && confirmRequired && (
                    <div className="space-y-2.5 rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-2.5">
                        <Checkbox
                            id="confirm-deployment-admin-cleanup"
                            checked={Boolean(confirmed)}
                            onChange={(event) => onConfirmChange?.(event.target.checked)}
                            disabled={activeBusy}
                            label="I confirm Orchestration administrator cleanup has been run (or accept leaving cluster resources behind) and want to delete the Lens deployment record."
                        />
                        <div className="flex flex-wrap gap-2">
                            <Button
                                variant="danger"
                                size="sm"
                                onClick={onConfirmDelete}
                                disabled={!confirmed || activeBusy}
                            >
                                Confirm &amp; Delete Record
                            </Button>
                        </div>
                    </div>
                )}

                {(adminOperations.length > 0 || userOperations.length > 0 || resolvedScript || marker.name || marker.namespace) && (
                    <details
                        className="group/details border-t border-theme-border pt-3"
                        open={detailsOpen}
                        onToggle={handleDetailsToggle}
                    >
                        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-xs font-medium text-theme-muted hover:text-theme-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60 [&::-webkit-details-marker]:hidden">
                            <span className="flex items-center gap-1.5">
                                <ChevronRight
                                    size={13}
                                    aria-hidden="true"
                                    className="shrink-0 transition-transform motion-reduce:transition-none group-open/details:rotate-90"
                                />
                                View operation details
                            </span>
                            {detailSummaryLabel && <span className="font-mono text-[11px]">{detailSummaryLabel}</span>}
                        </summary>

                        <div className="mt-3 space-y-4">
                            {adminOperations.length > 0 && (
                                <section className="space-y-2" aria-labelledby="deploy-handoff-admin-heading">
                                    <h4 id="deploy-handoff-admin-heading" className="text-xs font-semibold text-theme-text">
                                        {isCleanup ? 'Remaining Orchestration administrator operations' : `Orchestration administrator operations (${pluralCount(adminOperations.length, 'operation')})`}
                                    </h4>
                                    <ul className="space-y-1">
                                        {adminOperations.map((entry, index) => {
                                            const id = handoffOperationId(entry);
                                            const isDone = verified || (!isUnconfirmed && !isStale && hasVerifyCheck && Boolean(id) && !remainingIds.has(id));
                                            const rowTone = isDone ? 'success' : isUnconfirmed ? 'warning' : 'danger';
                                            const statusLabel = isDone
                                                ? 'Done'
                                                : isUnconfirmed
                                                    ? 'Unconfirmed'
                                                    : 'Remaining';
                                            return (
                                                <HandoffOperationRow
                                                    key={`${id || index}-admin`}
                                                    entry={entry}
                                                    tone={rowTone}
                                                    statusLabel={statusLabel}
                                                    rules={rulesFor(id)}
                                                >
                                                    <p className="text-[11px] leading-relaxed text-theme-muted">
                                                        {isCleanup
                                                            ? handoffTargetLabel(entry)
                                                            : (isDone ? 'Verified in the target namespace.' : 'Applied by the Orchestration administrator setup script.')}
                                                    </p>
                                                </HandoffOperationRow>
                                            );
                                        })}
                                    </ul>
                                </section>
                            )}

                            {userOperations.length > 0 && (
                                <section className="space-y-2" aria-labelledby="deploy-handoff-user-heading">
                                    <h4 id="deploy-handoff-user-heading" className="text-xs font-semibold text-theme-text">
                                        {isCleanup ? 'Cleaned with your kubeconfig' : 'Your operations'}
                                    </h4>
                                    <ul className="space-y-1">
                                        {userOperations.map((entry, index) => {
                                            const id = handoffOperationId(entry);
                                            return (
                                                <HandoffOperationRow
                                                    key={`${id || index}-user`}
                                                    entry={entry}
                                                    tone="success"
                                                    statusLabel={isCleanup ? 'Cleaned' : 'Ready'}
                                                    rules={rulesFor(id)}
                                                >
                                                    <p className="text-[11px] leading-relaxed text-theme-muted">
                                                        {isCleanup ? handoffTargetLabel(entry) : 'Runs with your kubeconfig after namespace setup.'}
                                                    </p>
                                                </HandoffOperationRow>
                                            );
                                        })}
                                    </ul>
                                </section>
                            )}

                            {(marker.namespace || marker.name || marker.status) && (
                                <div className="space-y-1 border-t border-theme-border pt-3">
                                    <p className="text-xs font-semibold text-theme-text">Completion marker</p>
                                    <dl className="grid gap-1 text-[11px] text-theme-muted sm:grid-cols-[8rem_1fr]">
                                        <dt>Namespace</dt><dd className="break-all font-mono">{marker.namespace || 'not available'}</dd>
                                        <dt>ConfigMap</dt><dd className="break-all font-mono">{marker.name || 'not available'}</dd>
                                        <dt>Status</dt><dd>{marker.status || markerStatus}{marker.verified ? ' · verified' : ''}</dd>
                                    </dl>
                                </div>
                            )}

                            {resolvedScript && (
                                <div className="space-y-1.5 border-t border-theme-border pt-3">
                                    <p className="text-xs font-semibold text-theme-text">Script preview</p>
                                    <pre className="max-h-60 overflow-auto rounded-lg border border-theme-border bg-theme-bg/70 p-3 font-mono text-[11px] leading-relaxed text-theme-text">
                                        {resolvedScript}
                                    </pre>
                                </div>
                            )}
                        </div>
                    </details>
                )}
            </div>
        </Panel>
    );
}
