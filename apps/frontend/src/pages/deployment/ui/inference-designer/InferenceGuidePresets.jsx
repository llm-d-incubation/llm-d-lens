import { selectionConflicts } from '../../model/inference-designer/deploymentSelection';
import { useState } from 'react';
import { Button, Input, Select, Badge } from '../../../../shared/ui/index';
import { MultiSelectDropdown } from '../../../../shared/ui/MultiSelectDropdown';
import { guideVariables } from '../../model/inference-designer/guidePlan';
import { optimizationCatalog } from '@repo/shared/inference/optimizations';

// Upstream Guide variables are shell names; show what each one controls.
const VARIABLE_LABELS = {
    MODEL_SERVER: ['Model server', 'Inference engine the Guide deploys.'],
    MONITORING: ['Monitoring', 'Create Prometheus PodMonitor/ServiceMonitor resources.'],
    MONITORING_VALUES: ['Monitoring values file', 'Extra Helm values for monitoring. Leave blank for the Guide default.'],
    ACCELERATOR_VALUES: ['Accelerator values file', 'Extra Helm values for the accelerator. Leave blank for the Guide default.'],
    INFRA_PROVIDER: ['Infrastructure provider', 'Selects the Guide overlay for your platform and network fabric. Use base for generic Kubernetes.'],
    VARIANT: ['Variant', 'Guide variant to render.'],
};
const variableLabel = key => VARIABLE_LABELS[key]?.[0] || key.toLowerCase().replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase());
const variableHint = key => VARIABLE_LABELS[key]?.[1];

/** Official presets and independent optimization choices share one validated intent. */
export function InferenceGuidePresets({ catalog, draft, change, plan, onFocus, includedGuides = [], embedded = false }) {
    const [expanded, setExpanded] = useState('');
    if (!catalog) return <p className="inference-empty">Choose a cluster to load its versioned Guide index.</p>;
    const official = catalog.officialGuides || [];
    const selected = draft.selectedGuides || [];
    const selectedSet = new Set(includedGuides);
    const optimizations = optimizationCatalog(catalog).items;
    const entries = official.map(item => ({ ...catalog.guides.find(guide => guide.id === item.id), ...item,
        optimization: optimizations.find(option => option.guide === item.id) }));
    const unsupported = new Set(entries.filter(guide => ['external', 'operation'].includes(guide.disposition?.kind)
        || guide.optimization?.generation === 'requires-adapter').map(guide => guide.id));
    const conflicts = Object.fromEntries(entries.filter(guide => !selectedSet.has(guide.id)).map(guide => [guide.id, selectionConflicts(catalog, draft, { selectedGuides: [...selected, guide.id] }).join(' ')]));
    const automatic = new Set(includedGuides.filter(id => !selected.includes(id)));
    const status = guide => unsupported.has(guide.id) ? guide.optimization?.reason || 'No YAML generator'
        : guide.optimization?.maturity === 'deprecated' ? 'Deprecated by this source revision'
            : guide.disposition?.kind === 'category' ? 'Choose a deployment strategy' : 'Source recipe; generation checked separately';
    const configured = [...new Set([...selected, ...(plan?.guides || []), ...(plan?.choices || []).map(choice => choice.guide)])];
    const cards = configured.map(id => ({ ...catalog.guides.find(guide => guide.id === id), ...official.find(guide => guide.id === id) })).filter(guide => guide.id);
    const setVariable = (guide, key, value) => change({ sourceVariables: { ...draft.sourceVariables, [guide]: { ...draft.sourceVariables?.[guide], [key]: value } } });
    return <section className="inference-presets">
        {embedded ? <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-theme-muted">Guide recipe · revision {catalog.commit.slice(0, 8)}</p> : <>
        <div className="inference-presets-summary"><strong>{includedGuides.length} Guides in this plan</strong><span>Source revision {catalog.commit.slice(0, 8)}</span></div>
        <p className="mb-3 text-xs leading-relaxed text-theme-muted">Guides are the pinned llm-d recipes rendered into the YAML. Your optimization choices already select them; use this tab only for Guide-level settings such as the model server or infrastructure provider.</p></>}
        <details className="mb-3"><summary className="cursor-pointer text-xs font-medium">Add or remove Guides (advanced)</summary><div className="mt-2">
        <MultiSelectDropdown label="Guides" options={entries.map(guide => guide.id)} selected={selectedSet} searchable inlineMenu defaultOpen
            searchPlaceholder="Find an official Guide" clearLabel="Reset to baseline" emptyLabel="Choose Guides"
            disabledOptions={new Set([...unsupported, ...automatic, ...Object.keys(conflicts).filter(id => conflicts[id])])} formatLabel={id => entries.find(guide => guide.id === id)?.title || id}
            optionGroup={id => entries.find(guide => guide.id === id)?.group}
            optionDescription={id => { const guide = entries.find(guide => guide.id === id); return conflicts[id] || `${automatic.has(id) ? id === 'optimized-baseline' ? 'Default serving Guide' : (plan?.reasons?.[id] || ['Included by the current plan']).join('; ') : status(guide)}${['rollouts', 'multi-model-routing'].includes(id) ? ' · Requires existing deployment' : ''}`; }}
            onChange={id => change(!id ? { selectedGuides: [], capabilityOverrides: {}, guideChoices: {}, sourceVariables: {}, sourceUnits: [], sourceRouter: { ...draft.sourceRouter, values: [] } } : { sourceMode: true, selectedGuides: selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id] })} />
        </div></details>
        {!official.length && <p role="alert">This source revision has no readable official Guide index. Reload the source catalog.</p>}
        {cards.map(guide => {
            const choices = (plan?.choices || []).filter(choice => choice.guide === guide.id);
            const problems = (plan?.errors || []).filter(error => error.includes(guide.id));
            const active = embedded || expanded === guide.id || choices.length > 0;
            const settings = plan?.variables?.[guide.id] || guideVariables(catalog, guide, draft.sourceVariables?.[guide.id]);
            const explicit = selected.includes(guide.id);
            const existing = guide.id === 'multi-model-routing' || guide.id === 'workload-autoscaling/multi-inference-pool' || plan?.workloadPatches?.some(patch => patch.guide === guide.id);
            return <article className="inference-preset" key={guide.id} data-state={explicit ? 'selected' : 'required'}>
                <div className="inference-preset-title"><strong className="text-xs text-theme-text break-words">{guide.title || guide.id}</strong><Badge size="xs" tone={problems.length ? 'warning' : 'brand'}>{problems.length ? 'Needs choices' : explicit ? 'Selected' : 'Included automatically'}</Badge>{!embedded && <Button variant="ghost" size="xs" aria-label={`Configure ${guide.id}`} aria-expanded={active} onClick={() => { setExpanded(active ? '' : guide.id); onFocus?.(guide.id); }}>{active ? 'Hide' : 'Settings'}</Button>}</div>
                {existing && <Badge size="xs" tone="warning">Requires existing deployment</Badge>}
                {active && <div className="inference-preset-options sm:grid-cols-2">
                    {choices.map(choice => <label key={choice.key} title={variableHint(choice.key)}>{VARIABLE_LABELS[choice.key]?.[0] || choice.label || variableLabel(choice.key)}{choice.multiple ? <MultiSelectDropdown label={choice.label} options={choice.options.map(option => option.value)} selected={new Set(choice.value)} formatLabel={id => choice.options.find(option => option.value === id)?.label || id} clearLabel="Clear strategies" emptyLabel="Choose strategies" inlineMenu onChange={id => change({ guideChoices: { ...draft.guideChoices, [guide.id]: !id ? [] : choice.value.includes(id) ? choice.value.filter(value => value !== id) : [...choice.value, id] } })} /> : <Select aria-label={`${guide.id} ${choice.key}`} value={choice.value} onChange={event => setVariable(guide.id, choice.key, event.target.value)}><option value="">Select…</option>{choice.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</Select>}{variableHint(choice.key) && <small>{variableHint(choice.key)}</small>}</label>)}
                    {Object.entries(guide.variables || {}).filter(([key]) => !choices.some(choice => choice.key === key) && !['REPO_ROOT', 'HF_TOKEN', 'GUIDE_NAME', 'NAMESPACE', 'MODEL', 'MODEL_NAME', 'ACCELERATOR_TYPE'].includes(key)).map(([key, spec]) => <label key={key}>{variableLabel(key)}{spec?.values ? <Select aria-label={`${guide.id} ${key}`} value={settings[key] || ''} onChange={event => setVariable(guide.id, key, event.target.value)}>{spec.values.map(value => <option key={String(value)} value={String(value)}>{String(value)}</option>)}</Select> : <Input aria-label={`${guide.id} ${key}`} placeholder="Guide default" value={settings[key] || ''} onChange={event => setVariable(guide.id, key, event.target.value)} />}{variableHint(key) && <small>{variableHint(key)}</small>}</label>)}
                    {problems.map(error => <p className="text-theme-danger" key={error}>{error}</p>)}
                    <a href={`https://github.com/llm-d/llm-d/blob/${catalog.commit}/guides/${guide.id}/README.md`} target="_blank" rel="noreferrer">Guide documentation ↗</a>
                </div>}
            </article>;
        })}
    </section>;
}
