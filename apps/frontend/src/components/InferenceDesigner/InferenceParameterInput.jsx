import { useState } from 'react';
import { Input, Select, Textarea } from '../../shared/ui/index';

export function InferenceParameterInput({ leaf, onCommit }) {
    const [value, setValue] = useState(String(leaf.value));
    if (leaf.readOnly) return <span className="text-xs text-theme-muted">{leaf.value}</span>;
    if (String(leaf.value).includes('\n') || String(leaf.value).length > 160) return <Textarea aria-label={leaf.title || leaf.key} rows={4} value={value} onChange={event => setValue(event.target.value)} onBlur={() => { if (value !== String(leaf.value)) onCommit(value); }} />;
    return leaf.type === 'boolean' ? <Select aria-label={leaf.title || leaf.key} value={value} onChange={event => { setValue(event.target.value); onCommit(event.target.value); }}><option value="true">true</option><option value="false">false</option></Select> : <Input aria-label={leaf.title || leaf.key} value={value} onChange={event => setValue(event.target.value)} onBlur={() => { if (value !== String(leaf.value)) onCommit(value); }} />;
}
