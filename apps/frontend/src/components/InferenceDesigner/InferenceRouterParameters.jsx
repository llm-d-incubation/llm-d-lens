import { useEffect, useState } from 'react';
import { routerParameterDocument, changeRouterParameter } from '../../features/inference-designer/routerParameters';
import { nodeRouterFields } from '../../features/inference-designer/nodeParameters';
import { InferenceParameterField } from './InferenceParameterField';
import { Button, Input, Select } from '../../shared/ui/index';

function RouterValue({ leaf, onCommit }) {
    const [value, setValue] = useState(String(leaf.value));
    return leaf.type === 'boolean' ? <Select aria-label={leaf.title || leaf.key} value={String(leaf.value)} onChange={event => onCommit(event.target.value)}><option value="true">true</option><option value="false">false</option></Select> : <Input aria-label={leaf.title || leaf.key} value={value} onChange={event => setValue(event.target.value)} onBlur={() => { if (value !== String(leaf.value)) onCommit(value); }} />;
}

export function InferenceRouterParameters({ draft, change, onLoad, busy, canLoad, onError, entity, embedded = false }) {
    const [query, setQuery] = useState('');
    useEffect(() => () => onError(''), [onError]);
    const [error, setError] = useState('');
    let leaves = [], parseError = '';
    try { const document = routerParameterDocument(draft.routerValues); leaves = entity ? nodeRouterFields(document, entity) : document.leaves.map(leaf => ({ ...leaf, title: leaf.path.at(-1), hint: leaf.key, section: 'Router' })); } catch (reason) { parseError = reason.message; }
    return <section className="inference-router-parameters">
        {!embedded && <h3>Source router parameters</h3>}
        {!embedded && <><details><summary>Source and replacement behavior</summary><p>Reads the selected Guide's router and plugin values. Replaces the existing overlay and incorporates structured router controls.</p></details>
        <Button variant="secondary" size="sm" disabled={!canLoad || busy} isLoading={busy} onClick={onLoad}>Read pinned router parameters</Button>
        {!canLoad && <p>Complete model, cluster, hardware and required Guide settings first.</p>}</>}
        {draft.routerSourceCommit && <p className="inference-flow-help">Source: {draft.routerSourceCommit.slice(0, 12)} · {draft.routerSourceKey}</p>}
        {(parseError || error) && <p role="alert">{parseError || error}</p>}
        {entity && !leaves.length && !parseError && <p className="inference-empty">This source declares no explicit tuning values for this optimization. Plugin defaults remain inherited. Open Source YAML below to add explicit settings supported by this source revision.</p>}
        {leaves.length > 0 && <>{leaves.length > 6 && <Input aria-label="Search source router parameters" placeholder="Find a parameter" value={query} onChange={event => setQuery(event.target.value)} />}
            {leaves.filter(leaf => `${leaf.title || ''} ${leaf.key}`.toLowerCase().includes(query.toLowerCase())).map(leaf => <InferenceParameterField key={leaf.id} field={leaf}><RouterValue key={JSON.stringify([leaf.id, leaf.value])} leaf={leaf} onCommit={value => {
                try { change({ routerValues: changeRouterParameter(draft.routerValues, leaf, value) }); setError(''); onError(''); } catch (reason) { setError(reason.message); onError(reason.message); }
            }} /></InferenceParameterField>)}</>}

    </section>;
}
