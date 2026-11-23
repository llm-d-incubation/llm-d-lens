import { useState } from 'react';
import { Input } from '../../../../shared/ui/index';

/** A source-owned parameter with its purpose and an optional exact-path disclosure. */
export function InferenceParameterField({ field, children }) {
    return <div className="inference-owned-field">
        <div className="inference-owned-field-label"><strong title={field.title}>{field.title}</strong></div>
        {field.hint && <p className="inference-owned-field-help" title={field.hint}>{field.hint}</p>}
        <div>{children}</div>
        <code className="inference-owned-field-source" title={field.sourceKey || field.key}>{field.sourceKey || field.key}</code>
    </div>;
}

/** Group long parameter lists by section; only the first section starts open. */
export function ParameterGroups({ fields, render, searchLabel = 'Find a parameter' }) {
    const [query, setQuery] = useState('');
    const visible = fields.filter(field => `${field.title || ''} ${field.section || ''} ${field.sourceKey || field.key}`.toLowerCase().includes(query.toLowerCase()));
    const sections = [...new Set(visible.map(field => field.section || 'Parameters'))];
    return <div className="space-y-2">
        {fields.length > 6 && <Input aria-label={searchLabel} placeholder={searchLabel} value={query} onChange={event => setQuery(event.target.value)} />}
        {sections.map((section, index) => {
            const members = visible.filter(field => (field.section || 'Parameters') === section);
            return <details key={section} className="inference-parameter-group" open={index === 0 || Boolean(query)}>
                <summary><span>{section}</span><span className="text-xs text-theme-muted">{members.length}</span></summary>
                <div className="inference-parameter-grid">{members.map(render)}</div>
            </details>;
        })}
        {query && !visible.length && <p className="text-xs text-theme-muted">No parameter matches “{query}”.</p>}
    </div>;
}
