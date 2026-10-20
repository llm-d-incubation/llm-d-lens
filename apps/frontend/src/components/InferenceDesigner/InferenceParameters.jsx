import { useId, useState } from 'react';
import yaml from 'js-yaml';
import { PARAMETER_FIELDS } from '../../features/inference-designer/capabilityCatalog';
import { blueprintOverrides } from '../../features/inference-designer/blueprint';
import { Button, Input, Select, Textarea } from '../../shared/ui/index';

function Parameter({ field, value, onChange }) {
    const id = useId();
    const common = { id, value: value ?? '', onChange: event => onChange(event.target.value), 'aria-describedby': `${id}-hint` };
    return <div className="inference-parameter">
        <label htmlFor={id}>{field.label}</label>
        <code>{field.output}</code>
        {field.type === 'flag' || field.options ? <Select {...common}><option value="">Inherit from pinned source</option>{(field.options || ['enabled']).map(option => <option key={option} value={option}>{option}</option>)}</Select> : <Input {...common} type={['integer', 'number'].includes(field.type) ? 'number' : 'text'} min={field.min} max={field.max} step={field.type === 'integer' ? 1 : 'any'} placeholder="Inherit from pinned source" />}
        <small id={`${id}-hint`}>{field.hint}</small>
    </div>;
}

/** Typed controls and an explicit escape hatch share the existing draft/validator contract. */
export function InferenceParameters({ draft, change, fields, all = false }) {
    const [query, setQuery] = useState('');
    const items = PARAMETER_FIELDS.filter(field => (all || fields.includes(field.key)) && `${field.label} ${field.output} ${field.group}`.toLowerCase().includes(query.toLowerCase()));
    return <div className="inference-parameters">
        {all && <Input aria-label="Search parameters" placeholder="Search parameter name or output key" value={query} onChange={event => setQuery(event.target.value)} />}
        {all && <p className="text-xs text-theme-muted">{items.length} structured fields · blank values inherit source settings. Inactive P/D and cache fields are retained in the draft only.</p>}
        {[...new Set(items.map(field => field.group))].map(group => <section key={group}><h3 className="inference-parameter-group">{group}</h3>{items.filter(field => field.group === group).map(field => <Parameter key={field.key} field={field} value={draft[field.key]} onChange={value => change({ [field.key]: value })} />)}</section>)}
        {all && !items.length && <p className="text-xs text-theme-muted">No structured parameters match this selection.</p>}
    </div>;
}

export function InferenceEngineOverrides({ draft, change }) {
    let rows = [], invalid = '';
    try { rows = blueprintOverrides(draft.engineOverrides); } catch (error) { invalid = error.message; }
    const update = next => change({ engineOverrides: next.length ? yaml.dump(next, { noRefs: true }) : '' });
    return <details className="inference-expert"><summary>Additional engine arguments & environment</summary>
        <p>Set role-specific runtime options. Dedicated model/TP fields and managed environment variables remain protected.</p>
        {invalid ? <p role="alert">{invalid}</p> : rows.map((row, index) => <div className="inference-override-row" key={index}>
            <Select aria-label={`Override ${index + 1} target`} value={row.target} onChange={event => update(rows.map((item, i) => i === index ? { ...item, target: event.target.value } : item))}><option value="both">Both / serving</option>{draft.pd && <option value="prefill">Prefill</option>}<option value="decode">Decode</option></Select>
            <Select aria-label={`Override ${index + 1} kind`} value={row.kind} onChange={event => update(rows.map((item, i) => i === index ? { ...item, kind: event.target.value } : item))}><option value="argument">Argument</option><option value="environment">Environment</option></Select>
            {['name', 'value'].map(key => <Input key={key} aria-label={`Override ${index + 1} ${key}`} placeholder={key === 'name' ? 'Name without --' : 'Value (empty for flag)'} value={row[key]} onChange={event => update(rows.map((item, i) => i === index ? { ...item, [key]: event.target.value } : item))} />)}
            <Button variant="ghost" size="xs" onClick={() => update(rows.filter((_, i) => i !== index))}>Remove override {index + 1}</Button>
        </div>)}
        <Button variant="secondary" size="sm" disabled={!!invalid} onClick={() => update([...rows, { target: 'both', kind: 'argument', name: '', value: '' }])}>Add engine parameter</Button>
        <details><summary>Raw override YAML</summary><Textarea aria-label="Engine override YAML" rows={8} value={draft.engineOverrides} onChange={event => change({ engineOverrides: event.target.value })} /></details>
    </details>;
}
