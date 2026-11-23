import yaml from 'js-yaml';
import { useEffect, useRef, useState } from 'react';
import { Badge, Button, Textarea, LoadingState } from '../../../../shared/ui/index';
import { generateSourceBundle } from './sourceCompositionBackend';
import { reviewParameterLeaves, sourceParameterEdit, rebaseFinalConfiguration } from '../../model/inference-designer/sourceParameters';
import { InferenceParameterField, ParameterGroups } from './InferenceParameterField';
import { InferenceParameterInput } from './InferenceParameterInput';
import { nodeSourceFields } from '../../model/inference-designer/nodeParameters';

/** Snapshot of an already rendered bundle; avoids a server re-render when the selection is unchanged. */
function reviewedSnapshot(draft, result) {
    if (!result?.files || !result.compositionChecksum) return null;
    try { return rebaseFinalConfiguration(draft.finalConfiguration, result); } catch { return null; }
}

/** Edit the actual rendered deployment, tied to the current selection checksum. */
export function InferenceDeploymentParameters({ draft, change, entity, initialTab = 'parameters', tab: controlledTab, reviewedResult }) {
    const [initial] = useState(() => reviewedSnapshot(draft, reviewedResult));
    const [snapshot, setSnapshot] = useState(initial);
    const [busy, setBusy] = useState(!initial), [error, setError] = useState('');
    const [localTab, setTab] = useState(initialTab), [reload, setReload] = useState(0);
    const tab = controlledTab || localTab;
    const latest = useRef({ draft, change }); useEffect(() => { latest.current = { draft, change }; });
    const selection = JSON.stringify({ ...draft, finalConfiguration: undefined });
    const reviewed = Boolean(initial && reviewedResult) && reload === 0;
    useEffect(() => {
        if (reviewed) return undefined;
        let active = true;
        Promise.resolve().then(() => {
            if (!active) return null;
            setBusy(true); setError(''); setSnapshot(null);
            return generateSourceBundle({ ...latest.current.draft, finalConfiguration: undefined, sourceValidate: false });
        }).then(result => {
            if (!active || !result) return;
            const saved = latest.current.draft.finalConfiguration;
            setSnapshot(rebaseFinalConfiguration(saved, result));
        }).catch(reason => { if (active) setError(reason.message); }).finally(() => { if (active) setBusy(false); });
        return () => { active = false; };
    }, [selection, reload, reviewed]);
    // Re-render when the saved edit is discarded or replaced outside this editor.
    const [written, setWritten] = useState(draft.finalConfiguration?.content);
    if (draft.finalConfiguration?.content !== written) {
        setWritten(draft.finalConfiguration?.content);
        setReload(value => value + 1);
    }
    const update = content => {
        const next = { ...snapshot, content }; setSnapshot(next);
        setWritten(content);
        latest.current.change({ finalConfiguration: next });
    };
    let documents = [], fields = [], parseError = '';
    try { if (snapshot) { documents = yaml.loadAll(snapshot.content).filter(Boolean); fields = entity ? nodeSourceFields(documents, entity).filter(field => !field.readOnly) : reviewParameterLeaves(documents); } } catch (reason) { parseError = reason.message; }
    return <section className="space-y-4" aria-label="Final deployment configuration">
        {entity && <p className="text-sm text-theme-muted">Parameters for this optimization or serving stage.</p>}
        {busy && <LoadingState label="Rendering selected deployment" />}
        {error && <p role="alert">{error} <Button size="xs" variant="secondary" onClick={() => setReload(value => value + 1)}>Reload configuration</Button></p>}
        {snapshot && <>{!entity && !controlledTab && <div role="tablist" aria-label="Final configuration view">{['parameters', 'yaml'].map(value => <Button key={value} role="tab" aria-selected={tab === value} variant="ghost" onClick={() => setTab(value)}>{value === 'yaml' ? 'Final YAML' : 'All parameters'}</Button>)}</div>}
        {parseError && <p role="alert">{parseError}</p>}
        {tab === 'yaml' && !entity ? <Textarea aria-label="Final deployment YAML" rows={26} spellCheck={false} className="font-mono text-xs" value={snapshot.content} onChange={event => update(event.target.value)} /> : <>
        {entity && !fields.length && <p className="text-sm text-theme-muted">No editable parameters are declared for this selection. Enable it and resolve its prerequisites first.</p>}
        {(() => {
            const prepared = fields.map(field => ({ ...field, title: field.title || field.path.at(-1), section: field.section || `${documents[field.path[0]]?.kind} / ${documents[field.path[0]]?.metadata?.name}`, hint: field.hint }));
            const commit = field => raw => {
                try { const edit = sourceParameterEdit(field, field.encode ? field.encode(raw) : raw); const next = structuredClone(documents); let owner = next; for (const key of edit.path.slice(0, -1)) owner = owner[key]; owner[edit.path.at(-1)] = edit.value; update(next.map(doc => yaml.dump(doc, { noRefs: true, lineWidth: -1 })).join('---\n')); setError(''); } catch (reason) { setError(reason.message); }
            };
            const input = field => <InferenceParameterInput key={JSON.stringify([field.key, field.value])} leaf={field} onCommit={commit(field)} />;
            if (entity) return <ParameterGroups fields={prepared} searchLabel="Find a deployment parameter" render={field => <InferenceParameterField key={field.key} field={field}>{input(field)}</InferenceParameterField>} />;
            return <section className="inference-presets">
                <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-theme-muted">Workloads</p>
                {[...new Set(prepared.map(field => field.section))].map(section => {
                    const members = prepared.filter(field => field.section === section);
                    return <article key={section} className="inference-preset" data-state="selected">
                        <div className="inference-preset-title"><strong className="text-xs text-theme-text break-words">{section}</strong><Badge size="xs" tone="brand">{members.length}</Badge></div>
                        <div className="inference-preset-options sm:grid-cols-2">{members.map(field => <label key={field.key}>{field.title}{input(field)}<small className="truncate" title={field.key}>{field.key}</small></label>)}</div>
                    </article>;
                })}
                <p className="mt-2 text-xs text-theme-muted">Other fields (names, labels, ports, probes, volumes) are edited in the YAML tab.</p>
            </section>;
        })()}</>}</>}
    </section>;
}
