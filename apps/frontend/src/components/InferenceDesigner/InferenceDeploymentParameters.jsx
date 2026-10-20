import yaml from 'js-yaml';
import { useEffect, useRef, useState } from 'react';
import { Button, Input, Textarea, LoadingState } from '../../shared/ui/index';
import { generateSourceBundle } from './sourceCompositionBackend';
import { sourceParameterLeaves, sourceParameterEdit } from '../../features/inference-designer/sourceParameters';
import { InferenceParameterField } from './InferenceParameterField';
import { InferenceParameterInput } from './InferenceParameterInput';

/** Edit the actual rendered deployment, tied to the current selection checksum. */
export function InferenceDeploymentParameters({ draft, change }) {
    const [snapshot, setSnapshot] = useState(null), [busy, setBusy] = useState(true), [error, setError] = useState('');
    const [query, setQuery] = useState(''), [tab, setTab] = useState('parameters'), [reload, setReload] = useState(0);
    const latest = useRef({ draft, change }); useEffect(() => { latest.current = { draft, change }; });
    const selection = JSON.stringify({ ...draft, finalConfiguration: undefined });
    useEffect(() => {
        let active = true;
        Promise.resolve().then(() => {
            if (!active) return null;
            setBusy(true); setError(''); setSnapshot(null);
            return generateSourceBundle({ ...latest.current.draft, finalConfiguration: undefined, sourceValidate: false });
        }).then(result => {
            if (!active || !result) return;
            const saved = latest.current.draft.finalConfiguration;
            const content = saved?.baseChecksum === result.compositionChecksum ? saved.content : result.files.find(file => file.name === 'deployment.yaml').content;
            setSnapshot({ baseChecksum: result.compositionChecksum, content });
            if (saved && saved.baseChecksum !== result.compositionChecksum) setError('The deployment changed. Previous final edits are stale; edit the refreshed configuration before generating.');
        }).catch(reason => { if (active) setError(reason.message); }).finally(() => { if (active) setBusy(false); });
        return () => { active = false; };
    }, [selection, reload]);
    const update = content => {
        const next = { ...snapshot, content }; setSnapshot(next);
        latest.current.change({ finalConfiguration: next });
    };
    let documents = [], fields = [], parseError = '';
    try { if (snapshot) { documents = yaml.loadAll(snapshot.content).filter(Boolean); fields = sourceParameterLeaves(documents); } } catch (reason) { parseError = reason.message; }
    return <section className="space-y-4" aria-label="Final deployment configuration">
        <p className="text-sm text-theme-muted">Final configuration for the selected optimizations. Changes are validated when you generate. Changing an optimization clears these final edits.</p>
        {busy && <LoadingState label="Rendering selected deployment" />}
        {error && <p role="alert">{error} <Button size="xs" variant="secondary" onClick={() => setReload(value => value + 1)}>Reload configuration</Button></p>}
        {snapshot && <><div role="tablist" aria-label="Final configuration view">{['parameters', 'yaml'].map(value => <Button key={value} role="tab" aria-selected={tab === value} variant="ghost" onClick={() => setTab(value)}>{value === 'yaml' ? 'Final YAML' : 'All parameters'}</Button>)}</div>
        {parseError && <p role="alert">{parseError}</p>}
        {tab === 'yaml' ? <Textarea aria-label="Final deployment YAML" rows={24} value={snapshot.content} onChange={event => update(event.target.value)} /> : <><Input aria-label="Search deployment parameters" placeholder="Find a parameter or resource" value={query} onChange={event => setQuery(event.target.value)} />
        {fields.filter(field => `${documents[field.path[0]]?.kind} ${documents[field.path[0]]?.metadata?.name} ${field.key}`.toLowerCase().includes(query.toLowerCase())).map(field => <InferenceParameterField key={field.key} field={{ ...field, title: field.path.at(-1), section: `${documents[field.path[0]]?.kind} / ${documents[field.path[0]]?.metadata?.name}`, hint: field.path.slice(1).join(' › ') }}><InferenceParameterInput key={JSON.stringify([field.key, field.value])} leaf={field} onCommit={raw => {
            try { const edit = sourceParameterEdit(field, raw); const next = structuredClone(documents); let owner = next; for (const key of edit.path.slice(0, -1)) owner = owner[key]; owner[edit.path.at(-1)] = edit.value; update(next.map(doc => yaml.dump(doc, { noRefs: true, lineWidth: -1 })).join('---\n')); setError(''); } catch (reason) { setError(reason.message); }
        }} /></InferenceParameterField>)}</>}</>}
    </section>;
}
