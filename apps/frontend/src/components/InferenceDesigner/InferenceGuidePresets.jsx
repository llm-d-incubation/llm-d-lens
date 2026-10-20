import { selectionConflicts } from '../../features/inference-designer/deploymentSelection';
import { useState } from 'react';
import { Button, Input, Select, Badge } from '../../shared/ui/index';
import { MultiSelectDropdown } from '../../shared/ui/MultiSelectDropdown';
import { guideVariables } from '../../features/inference-designer/guidePlan';

/** Official presets and independent optimization choices share one validated intent. */
export function InferenceGuidePresets({ catalog, draft, change, plan, onFocus, includedGuides = [] }) {
    const [expanded, setExpanded] = useState('');
    if (!catalog) return <p className="inference-empty">Choose a cluster to load its versioned Guide index.</p>;
    const official = catalog.officialGuides || [];
    const selected = draft.selectedGuides || [];
    const selectedSet = new Set(includedGuides);
    const entries = official.map(item => ({ ...catalog.guides.find(guide => guide.id === item.id), ...item }));
    const unsupported = new Set(entries.filter(guide => ['external', 'operation'].includes(guide.disposition?.kind)).map(guide => guide.id));
    const conflicts = Object.fromEntries(entries.filter(guide => !selectedSet.has(guide.id)).map(guide => [guide.id, selectionConflicts(catalog, draft, { selectedGuides: [...selected, guide.id] }).join(' ')]));
    const automatic = new Set(includedGuides.filter(id => !selected.includes(id)));
    const status = guide => unsupported.has(guide.id) ? 'No YAML generator' : guide.disposition?.kind === 'category' ? 'Choose a deployment strategy' : 'Source deployment adapter';
    const configured = [...new Set([...selected, ...(plan?.guides || []), ...(plan?.choices || []).map(choice => choice.guide)])];
    const cards = configured.map(id => ({ ...catalog.guides.find(guide => guide.id === id), ...official.find(guide => guide.id === id) })).filter(guide => guide.id);
    const setVariable = (guide, key, value) => change({ sourceVariables: { ...draft.sourceVariables, [guide]: { ...draft.sourceVariables?.[guide], [key]: value } } });
    return <section className="inference-presets">
        <div className="inference-presets-summary"><strong>{includedGuides.length} Guides</strong><Badge>{catalog.commit.slice(0, 8)}</Badge></div>
        <MultiSelectDropdown label="Guides" options={entries.map(guide => guide.id)} selected={selectedSet} searchable inlineMenu
            searchPlaceholder="Find an official Guide" clearLabel="Reset to baseline" emptyLabel="Choose Guides"
            disabledOptions={new Set([...unsupported, ...automatic, ...Object.keys(conflicts).filter(id => conflicts[id])])} formatLabel={id => entries.find(guide => guide.id === id)?.title || id}
            optionGroup={id => entries.find(guide => guide.id === id)?.group}
            optionDescription={id => { const guide = entries.find(guide => guide.id === id); return conflicts[id] || `${automatic.has(id) ? id === 'optimized-baseline' ? 'Default serving Guide' : (plan?.reasons?.[id] || ['Included by the current plan']).join('; ') : status(guide)}${['rollouts', 'multi-model-routing'].includes(id) ? ' · Requires existing deployment' : ''}`; }}
            onChange={id => change(!id ? { selectedGuides: [], capabilityOverrides: {}, guideChoices: {}, sourceVariables: {}, sourceUnits: [], sourceRouter: { ...draft.sourceRouter, values: [] } } : { sourceMode: true, selectedGuides: selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id] })} />
        {!official.length && <p role="alert">This source revision has no readable official Guide index. Reload the source catalog.</p>}
        <p className="text-xs text-theme-muted">Guide presets include their capabilities. Tiers can customize them individually. Choose every required variant before generation.</p>
        {cards.map(guide => {
            const choices = (plan?.choices || []).filter(choice => choice.guide === guide.id);
            const problems = (plan?.errors || []).filter(error => error.includes(guide.id));
            const active = expanded === guide.id || choices.some(choice => !choice.value?.length);
            const settings = plan?.variables?.[guide.id] || guideVariables(catalog, guide, draft.sourceVariables?.[guide.id]);
            const explicit = selected.includes(guide.id);
            const existing = guide.id === 'multi-model-routing' || guide.id === 'workload-autoscaling/multi-inference-pool' || plan?.workloadPatches?.some(patch => patch.guide === guide.id);
            return <article className="inference-preset" key={guide.id} data-state={explicit ? 'selected' : 'required'}>
                <div className="inference-preset-title"><strong className="text-xs text-theme-text break-words">{guide.title || guide.id}</strong><Badge size="xs" tone={problems.length ? 'warning' : 'brand'}>{problems.length ? 'Needs choices' : explicit ? 'Preset' : 'Component source'}</Badge><Button variant="ghost" size="xs" aria-label={`Configure ${guide.id}`} aria-expanded={active} onClick={() => { setExpanded(active ? '' : guide.id); onFocus?.(guide.id); }}>{active ? '−' : '+'}</Button></div>
                {existing && <Badge size="xs" tone="warning">Requires existing deployment</Badge>}
                {active && <div className="inference-preset-options">
                    {choices.map(choice => <label key={choice.key}>{choice.label}{choice.multiple ? <MultiSelectDropdown label={choice.label} options={choice.options.map(option => option.value)} selected={new Set(choice.value)} formatLabel={id => choice.options.find(option => option.value === id)?.label || id} clearLabel="Clear strategies" emptyLabel="Choose strategies" inlineMenu onChange={id => change({ guideChoices: { ...draft.guideChoices, [guide.id]: !id ? [] : choice.value.includes(id) ? choice.value.filter(value => value !== id) : [...choice.value, id] } })} /> : <Select aria-label={`${guide.id} ${choice.key}`} value={choice.value} onChange={event => setVariable(guide.id, choice.key, event.target.value)}><option value="">Choose explicitly</option>{choice.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</Select>}</label>)}
                    {Object.entries(guide.variables || {}).filter(([key]) => !choices.some(choice => choice.key === key) && !['REPO_ROOT', 'HF_TOKEN', 'GUIDE_NAME', 'NAMESPACE', 'MODEL', 'MODEL_NAME', 'ACCELERATOR_TYPE'].includes(key)).map(([key, spec]) => <label key={key}>{key}{spec?.values ? <Select aria-label={`${guide.id} ${key}`} value={settings[key] || ''} onChange={event => setVariable(guide.id, key, event.target.value)}>{spec.values.map(value => <option key={String(value)} value={String(value)}>{String(value)}</option>)}</Select> : <Input aria-label={`${guide.id} ${key}`} value={settings[key] || ''} onChange={event => setVariable(guide.id, key, event.target.value)} />}</label>)}
                    {problems.map(error => <p className="text-theme-danger" key={error}>{error}</p>)}
                    <a href={`https://github.com/llm-d/llm-d/blob/${catalog.commit}/guides/${guide.id}/README.md`} target="_blank" rel="noreferrer">Guide instructions ↗</a>
                </div>}
            </article>;
        })}
    </section>;
}
