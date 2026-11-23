import { useState } from 'react';
import { ArrowRight } from 'lucide-react';
import { Badge, Button, Checkbox, Input, Panel, Select, Textarea, ToggleGroup } from '../../../shared/ui';
import { DEFAULT_REQUIREMENTS, OPTIMIZATION_CONTROLS } from '../api/agenticDeploymentBackend';
import { optimizationChoiceState } from '../model/deploymentPlan';
import { OPTIMIZATION_OBJECTIVES } from '../model/inference-designer/optimizationPolicy';

// Offload depends on the generated topology (not with P/D), so it is only decided and shown after generation.
const PRE_PLAN_HIDDEN = new Set(['external_kv_cache']);

/** Checked optimizations are attempted; unchecked ones become forbidden features. */
export function OptimizationChecklist({ choices = [], forbidden = [], onChange, disabled = false }) {
    const byId = new Map(choices.map(item => [item.capability_id, item]));
    return <ul className="space-y-3" aria-label="Optimizations">{OPTIMIZATION_CONTROLS.filter(([id]) => !PRE_PLAN_HIDDEN.has(id)).map(([id, label]) => {
        const checked = !forbidden.includes(id);
        const choice = byId.get(id);
        const state = optimizationChoiceState(choice?.checked === checked ? choice : null, checked);
        return <li key={id} className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
            <div className="min-w-0 flex-1">
                <Checkbox label={label} checked={checked} disabled={disabled}
                    onChange={event => onChange(event.target.checked ? forbidden.filter(item => item !== id) : [...forbidden, id])} />
                {choice?.checked === checked && <p className="ml-6 mt-0.5 text-xs text-theme-muted">{choice.reason}</p>}
            </div>
            <Badge size="xs" tone={state.tone}>{state.label}</Badge>
        </li>;
    })}</ul>;
}

export function DeploymentRequirementsForm({ mode = 'manual', initialValue, onSubmit, disabled = false, runtime, resourceEstimate, aiProviderConfigured = false, submitLabel, children }) {
    const [value, setValue] = useState({ ...DEFAULT_REQUIREMENTS, ...initialValue });
    const [loadTarget, setLoadTarget] = useState(initialValue?.requestRate ? 'requestRate' : 'concurrency');
    const patch = key => event => setValue(current => ({ ...current, [key]: event.target.value }));
    const hasValues = keys => keys.some(key => initialValue?.[key] !== '' && initialValue?.[key] != null);
    const number = (key, label, placeholder, max) => <label className="block text-sm text-theme-text">{label}
        <Input className="mt-1.5" type="number" min={key === 'requestRate' ? '0.01' : '1'} step={key === 'requestRate' ? 'any' : '1'} max={max} value={value[key]} placeholder={placeholder} onChange={patch(key)} />
    </label>;
    const objectives = OPTIMIZATION_OBJECTIVES.filter(([id]) => ['balanced', 'latency', 'throughput', value.objective].includes(id));
    return <form id="deployment-requirements" onInvalid={event => event.target.closest('details')?.setAttribute('open', '')} onSubmit={event => { event.preventDefault(); onSubmit(value); }}>
        <Panel title={mode === 'manual' ? 'Manual setup' : 'What should Agentic prioritize?'} actions={<Badge tone="info">No required parameters</Badge>}>
            <fieldset disabled={disabled} className="space-y-5">
                {mode === 'agentic' ? <div className="space-y-3"><ToggleGroup className="flex-wrap" size="sm" options={objectives.map(([id, label]) => ({ value: id, label, disabled }))} value={value.objective} onChange={objective => setValue(current => ({ ...current, objective }))} /><p className="text-sm text-theme-muted">{aiProviderConfigured ? 'AI-assisted recommendation' : 'Select an AI provider; Agentic does not use rule-based fallbacks'}. Performance is not verified by planning.</p></div>
                    : <div className="space-y-3"><p className="text-sm text-theme-muted">Rule-based plan: checked optimizations, including P/D separation, are enabled when compatible. With a request shape, AIC decides P/D and checks the SLA. Your goal chooses the topology.</p><ToggleGroup className="flex-wrap" size="sm" options={objectives.map(([id, label]) => ({ value: id, label, disabled }))} value={value.objective} onChange={objective => setValue(current => ({ ...current, objective }))} /></div>}
                {(runtime?.max_model_len || resourceEstimate?.available_accelerators != null) && <p className="text-xs text-theme-muted">
                    {runtime?.max_model_len ? `Context ${runtime.max_model_len.toLocaleString()} tokens` : ''}
                    {runtime?.max_model_len && resourceEstimate?.available_accelerators != null ? ' · ' : ''}
                    {resourceEstimate?.available_accelerators != null ? `${resourceEstimate.available_accelerators} accelerators available` : ''}
                </p>}
                {mode === 'agentic' && <details open={Boolean(initialValue?.plannerPrompt)}>
                    <summary className="cursor-pointer text-sm font-medium">Describe a goal <span className="font-normal text-theme-muted">/ optional AI assistance</span></summary>
                    <div className="mt-3 space-y-4">
                        {children}
                        <label className="block text-sm">Goal for the AI planner (optional)
                            <Textarea className="mt-1.5" value={value.plannerPrompt} maxLength={1800} onChange={patch('plannerPrompt')} placeholder="For example: serve document analysis with short responses." />
                        </label>
                        <p className="text-xs text-theme-muted">A written goal requires an AI provider. Enter numeric limits explicitly in the sections below.</p>
                    </div>
                </details>}
                <details open={hasValues(['maxAccelerators', 'concurrency', 'requestRate'])}>
                    <summary className="cursor-pointer text-sm font-medium">Resource limit and workload <span className="font-normal text-theme-muted">/ optional</span></summary>
                    <div className="mt-3 grid items-start gap-4 md:grid-cols-2">
                        {number('maxAccelerators', 'Accelerator limit', 'Use available capacity')}
                        <div className="text-sm text-theme-text">
                            <div className="flex flex-wrap items-center justify-between gap-2"><span>Load target</span><ToggleGroup size="xs" value={loadTarget} options={[{ value: 'concurrency', label: 'Concurrency' }, { value: 'requestRate', label: 'QPS' }]} onChange={target => { setLoadTarget(target); setValue(current => ({ ...current, concurrency: '', requestRate: '' })); }} /></div>
                            <Input className="mt-1.5" type="number" aria-label={loadTarget === 'concurrency' ? 'Concurrent requests' : 'Request rate (QPS)'} min={loadTarget === 'requestRate' ? '0.01' : '1'} step={loadTarget === 'requestRate' ? 'any' : '1'} value={value[loadTarget]} placeholder="Not set" onChange={patch(loadTarget)} />
                        </div>
                    </div>
                    <p className="mt-3 text-xs text-theme-muted">Concurrency sizes serving replicas from per-replica KV capacity; QPS requires AIC prediction support. The resource limit constrains the topology.</p>
                </details>
                <details open={hasValues(['inputTokens', 'outputTokens'])}>
                    <summary className="cursor-pointer text-sm font-medium">Request shape <span className="font-normal text-theme-muted">/ optional size hints</span></summary>
                    <div className="mt-3 grid gap-4 md:grid-cols-2">
                        {number('inputTokens', 'Mean input tokens', 'Optional estimate')}
                        {number('outputTokens', 'Mean output tokens', 'Optional estimate')}
                    </div>
                    <p className="mt-3 text-xs text-theme-muted">{mode === 'manual' ? 'With mean input and output tokens, AIC compares P/D with combined serving.' : 'The AI infers prefix reuse and prefill-heavy traffic from your goal and these token counts; correct them after planning in Deployment settings.'}</p>
                </details>
                <details open={hasValues(['ttftMs', 'tpotMs'])}>
                    <summary className="cursor-pointer text-sm font-medium">SLA targets <span className="font-normal text-theme-muted">/ optional P95 latency</span></summary>
                    <div className="mt-3 grid gap-4 md:grid-cols-2">
                        {number('ttftMs', 'P95 TTFT (ms)', 'No target')}
                        {number('tpotMs', 'P95 TPOT (ms)', 'No target')}
                    </div>
                    <p className="mt-2 text-xs text-theme-muted">These are requirements, not predicted results. Blank targets stay unspecified.</p>
                </details>
                {mode === 'manual' && <details open={Boolean(value.forbiddenFeatures?.length)}>
                    <summary className="cursor-pointer text-sm font-medium">Optimizations <span className="font-normal text-theme-muted">/ {value.forbiddenFeatures?.length ? `${value.forbiddenFeatures.length} turned off` : 'all on'}</span></summary>
                    <div className="mt-3"><OptimizationChecklist forbidden={value.forbiddenFeatures || []} disabled={disabled} onChange={forbiddenFeatures => setValue(current => ({ ...current, forbiddenFeatures, requiredFeatures: (current.requiredFeatures || []).filter(item => !forbiddenFeatures.includes(item)) }))} /></div>
                </details>}
                {value.providerRef && <p role="status" className="text-sm">This saved plan pins a deployment method. <Button type="button" size="xs" variant="link" onClick={() => setValue(current => ({ ...current, providerRef: '' }))}>Remove method restriction</Button></p>}
                <div id="deployment-generate" className="flex flex-wrap items-center justify-between gap-3 border-t border-theme-border pt-4">
                    <p className="text-xs text-theme-muted">No live deployment is changed.</p>
                    <Button type="submit" isLoading={disabled} disabled={disabled}>{submitLabel || (runtime ? 'Rebuild plan' : mode === 'manual' ? 'Generate plan' : 'Generate plan with AI')}<ArrowRight size={15} /></Button>
                </div>
            </fieldset>
        </Panel>
    </form>;
}

export function PlanningProviderSelect({ provider, providers, onChange }) {
    return <label className="block text-sm">AI provider<Select className="mt-1" value={provider} required onChange={event => onChange(event.target.value)}>
        <option value="" disabled>{providers.length ? 'Select an AI provider' : 'No AI provider configured'}</option>
        {providers.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </Select><span className="mt-2 block text-xs text-theme-muted">Agentic always uses an AI provider. If the AI request fails, planning stops; use Manual for rule-based plans.</span></label>;
}
