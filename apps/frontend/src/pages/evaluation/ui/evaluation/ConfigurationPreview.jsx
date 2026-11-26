// Copyright 2026 Google LLC
// SPDX-License-Identifier: Apache-2.0

import { useContext, useState } from 'react';
import { ArrowRight, ChevronRight, Download, FileCode } from 'lucide-react';
import { Input, Select, Textarea } from '../../../../shared/ui/FormControls';

import { Button } from '../../../../shared/ui/Button';
import { Badge } from '../../../../shared/ui/Badge';
import { Panel } from '../../../../shared/ui/Panel';
import { paginate } from '../../../../shared/lib/pagination';
import { downloadBlob } from '../../../../shared/lib/download';
import { PaginationControls } from '../../../../shared/ui/PaginationControls';
import { FormError } from '../../../../shared/ui/FormError';
import ConfigurationField from './ConfigurationField';
import { ConfigurationValidationContext } from './configurationFieldState';
import { analyzeConfigurationSweep, MAX_EVALUATION_CONFIGURATIONS } from '../../model/configuration';

// Render tokens as React text so manifest contents are never interpreted as HTML.
function yamlLine(line) {
    return line.split(/("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|(?:^|\s)#[^\n]*$|[\w./-]+(?=:\s|:$)|\b(?:true|false|null|\d+(?:\.\d+)?)\b)/g).map((token, index) => {
        const color = /^\s*#/.test(token) ? 'text-slate-500' : /^["']/.test(token) ? 'text-emerald-300' : /^(true|false|null|\d+(\.\d+)?)$/.test(token) ? 'text-amber-300' : /^[\w./-]+$/.test(token) ? 'text-cyan-300' : 'text-slate-300';
        return <span key={index} className={color}>{token}</span>;
    });
}

export function YamlPreview({ content = '', title = 'Deployment YAML', defaultOpen = true, prominent = false, onChange, disabled = false }) {
    return <details open={defaultOpen} className={`group/yaml mt-3 overflow-hidden rounded-xl border ${prominent ? 'border-cyan-500/40 shadow-lg shadow-cyan-950/10' : 'border-slate-700/60'}`}>
        <summary className={`flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-xs font-medium [&::-webkit-details-marker]:hidden ${prominent ? 'bg-cyan-500/10 text-cyan-200' : 'bg-slate-900/40 text-slate-300'}`}>
            <ChevronRight className="h-3.5 w-3.5 shrink-0 transition-transform group-open/yaml:rotate-90" aria-hidden="true" />
            <FileCode className="h-4 w-4 shrink-0" aria-hidden="true" /><span className="min-w-0 break-all">{title}</span>
            <span className="ml-auto text-[10px] font-normal text-slate-400">YAML</span>
        </summary>
        {onChange ? <Textarea aria-label="Editable deployment YAML" value={content} onChange={event => onChange(event.target.value)} disabled={disabled} spellCheck={false} className="h-[36rem] w-full resize-y font-mono text-xs leading-6" /> : <pre aria-label={title === 'Deployment YAML' ? 'Generated YAML' : title} tabIndex={0} className="max-h-[36rem] overflow-auto bg-slate-950 p-4 font-mono text-xs leading-6 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-400"><code>{content.split('\n').map((line, index, lines) => <span key={index}>{yamlLine(line)}{index < lines.length - 1 ? '\n' : ''}</span>)}</code></pre>}
    </details>;
}

export function TopologyPreview({ variants = [], sweep, analysis, isPd = false, onChange, maxConfigurations = MAX_EVALUATION_CONFIGURATIONS, perSlice = false, suggestion }) {
    const { errors, submitted } = useContext(ConfigurationValidationContext);
    const [sort, setSort] = useState('parameters');
    const [pageState, setPageState] = useState({ key: '', page: 0 });
    const editable = Boolean(isPd && sweep && onChange);
    const result = analysis || (editable ? analyzeConfigurationSweep(sweep, maxConfigurations) : null);
    const rows = result?.variants || variants;
    const sorted = sort === 'parameters' ? rows : [...rows].sort((a, b) => (a.gpuCount - b.gpuCount) * (sort === 'accelerators-desc' ? -1 : 1));
    const pageKey = JSON.stringify([sweep || variants, sort]);
    const page = paginate(sorted, pageState.key === pageKey ? pageState.page : 0, 10);
    const groupErrors = [...(result?.errors || []), ...(submitted ? errors : [])].filter(error => error.field === 'topologies');
    const change = (field, value) => onChange({ ...sweep, [field]: value });
    const exportCsv = () => downloadBlob(new Blob(['\uFEFF', [
        [...(isPd ? ['Prefill replicas', 'Prefill TP'] : []), 'Decode replicas', 'Decode TP', perSlice ? 'Accelerators per Slice' : 'Accelerators'].join(','),
        ...sorted.map(row => [...(isPd ? [row.prefillReplicaCount, row.prefillTpCount] : []), row.replicaCount, row.tpCount, row.gpuCount].join(',')),
    ].join('\r\n')], { type: 'text/csv;charset=utf-8' }), 'evaluation-configurations.csv');
    if (!editable && !rows.length) return null;
    return <section aria-label={editable ? 'Topology parameter sweep' : 'Topology preview'} data-configuration-field="topologies" tabIndex={-1} className="mt-4 space-y-4">
        {editable && <>
            <Panel padding="sm" title="1. Parameter values" className="shadow-none">
                <div className="grid items-center gap-3 lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
                    {[
                        { name: 'Prefill', tone: 'info', replicaField: 'prefillReplicaVariants', tpField: 'prefillTensorParallelVariants' },
                        { name: 'Decode', tone: 'violet', replicaField: 'replicaVariants', tpField: 'tensorParallelVariants' },
                    ].map((role, index) => <div key={role.name} className="contents">
                        {index === 1 && <ArrowRight aria-label="Prefill to Decode" className="hidden h-5 w-5 text-theme-muted lg:block" />}
                        <fieldset className="min-w-0 rounded-lg bg-theme-bg p-3">
                            <legend className="px-1"><Badge tone={role.tone}>{role.name}</Badge></legend>
                            <div className="grid gap-3 sm:grid-cols-2">
                                {[[role.replicaField, 'Replicas'], [role.tpField, 'TP']].map(([field, label]) => <ConfigurationField key={field} field={`sweep.${field}`}>
                                    <span className="mb-1.5 flex items-center justify-between gap-2 text-xs text-theme-muted">{label}<span>{result.values[field]?.length ?? '—'} values</span></span>
                                    <Input aria-label={`${role.name} ${label} values`} value={sweep[field]} onChange={event => change(field, event.target.value)} placeholder={label === 'TP' ? '1,2,4' : '1-8'} spellCheck={false} className="font-mono" />
                                </ConfigurationField>)}
                            </div>
                        </fieldset>
                    </div>)}
                </div>
                <p className="mt-3 text-xs text-theme-muted">One value: 2 · List: 1,2,4 · Range: 1-8 · Step: 1-8/2. TP is accelerators per replica. Four single values produce one configuration.</p>
                {suggestion}
            </Panel>
            <Panel padding="sm" title="2. Filter combinations (optional)" className="shadow-none" actions={<Button type="button" variant="link" size="xs" onClick={() => onChange({ ...sweep, relation: 'all', maxAccelerators: '', fixedAccelerators: '' })}>Clear filters</Button>}>
                <div className="grid gap-3 sm:grid-cols-3">
                    <ConfigurationField field="sweep.relation"><span className="mb-1.5 block text-xs text-theme-muted">P/D replica relationship</span><Select aria-label="P/D replica relationship" value={sweep.relation || 'all'} onChange={event => change('relation', event.target.value)}>
                        <option value="all">All combinations</option><option value="equal">P = D</option><option value="decode">P ≤ D</option><option value="prefill">P ≥ D</option>
                    </Select></ConfigurationField>
                    {[[ 'maxAccelerators', 'Maximum accelerators'], ['fixedAccelerators', 'Exact accelerator count']].map(([field, label]) => <ConfigurationField key={field} field={`sweep.${field}`}><span className="mb-1.5 block text-xs text-theme-muted">{label}{perSlice ? ' / Slice' : ''}</span><Input type="number" min="1" step="1" aria-label={label} value={sweep[field] ?? ''} placeholder="No limit" onChange={event => change(field, event.target.value)} /></ConfigurationField>)}
                </div>
                <dl aria-live="polite" className="mt-4 grid grid-cols-3 gap-3 border-t border-theme-border pt-4 text-theme-text">
                    {[[ 'Raw combinations', result.rawCount], ['Matching configurations', rows.length], [perSlice ? 'Max accelerators / Slice' : 'Max accelerators', rows.length ? rows.reduce((max, row) => Math.max(max, row.gpuCount), 0) : '—']].map(([label, value]) => <div key={label}><dt className="text-xs text-theme-muted">{label}</dt><dd className="mt-1 text-2xl font-semibold tabular-nums">{value.toLocaleString('en-US')}</dd></div>)}
                </dl>
                <p className="mt-3 text-xs text-theme-muted">{perSlice ? 'Replica counts and resource filters are per Slice. ' : ''}Accelerators = P replicas × P TP + D replicas × D TP. {maxConfigurations === 1 ? 'Direct deployment requires one matching configuration.' : `Save up to ${maxConfigurations} configurations, including any Slice combinations.`} Guide and model compatibility are checked when generating YAML.</p>
            </Panel>
        </>}
        <FormError message={[...new Set(groupErrors.map(error => error.message))].join(' ')} />
        <Panel padding="sm" title={editable ? '3. Configuration preview' : 'Configuration preview'} className="shadow-none" actions={<div className="flex flex-wrap items-center gap-2">
            <Select aria-label="Configuration sort order" value={sort} onChange={event => setSort(event.target.value)} className="h-8 w-auto py-1 text-xs"><option value="parameters">Parameter order</option><option value="accelerators">Accelerators: low to high</option><option value="accelerators-desc">Accelerators: high to low</option></Select>
            <Button type="button" variant="secondary" size="xs" aria-label="Export CSV" disabled={!rows.length} onClick={exportCsv}><Download size={13} aria-hidden="true" />Export CSV</Button>
        </div>}>
            <div className="overflow-x-auto"><table aria-label="Topology configurations" className="w-full whitespace-nowrap text-left text-xs tabular-nums">
                <thead className="border-b border-theme-border text-theme-muted"><tr>
                    {['Configuration', ...(isPd ? ['Prefill replicas', 'Prefill TP'] : []), isPd ? 'Decode replicas' : 'Replicas', isPd ? 'Decode TP' : 'TP', perSlice ? 'Accelerators / Slice' : 'Accelerators'].map(label => <th key={label} scope="col" className="px-3 py-2 font-medium">{label}</th>)}
                </tr></thead>
                <tbody>{page.pagedItems.map((row, index) => <tr key={index} className="border-b border-theme-border text-theme-text">
                    <th scope="row" className="px-3 py-2 font-medium">#{page.pageStart + index}</th>
                    {[...(isPd ? [row.prefillReplicaCount, row.prefillTpCount] : []), row.replicaCount, row.tpCount, row.gpuCount].map((value, fieldIndex) => <td key={fieldIndex} className="px-3 py-2">{value}</td>)}
                </tr>)}{!rows.length && <tr><td colSpan={isPd ? 6 : 4} className="px-3 py-8 text-center text-theme-muted">No matching configurations. Check the parameter values and filters.</td></tr>}</tbody>
            </table></div>
            <PaginationControls page={page.currentPage} totalPages={page.totalPages} total={rows.length} pageStart={page.pageStart} pageEnd={page.pageEnd} itemLabel="configurations" onPageChange={next => setPageState({ key: pageKey, page: next })} className="mt-3" />
        </Panel>
    </section>;
}
