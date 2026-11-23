import { useState } from 'react';
import { Badge, Button, Panel } from '../../../shared/ui';
import { cn } from '../../../shared/lib/cn';
import { sliceGroupRecommendation, sloTargetsLabel } from '../model/configuration';

const inputClass = 'mt-1 h-9 w-36 border border-slate-700 bg-slate-950 px-2 text-xs text-slate-100 outline-none focus:border-cyan-500';

function policyLabel(policy) {
    return policy?.type && policy.type !== 'None' ? `${policy.type} · ${policy.topology}` : 'None';
}

function rowResult(row, target) {
    if (row.status !== 'succeeded') return ['neutral', row.status || 'pending'];
    if (row.maximumStableQps == null) return ['neutral', 'No SLO-stable rate'];
    if (!(Number(target) > 0)) return ['neutral', 'Enter a target'];
    return row.maximumStableQps >= Number(target) ? ['success', 'Meets target'] : ['warning', 'Below target'];
}

function SliceGroup({ group, canDeploy, onUseConfiguration }) {
    const [target, setTarget] = useState('');
    const advice = sliceGroupRecommendation(group, target);
    const [prefill, prefillTp, decode, decodeTp] = group.shape;
    return <Panel data-slice-group className="space-y-3 shadow-none">
        <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
                <h3 className="text-sm font-semibold text-theme-text">Slice Optimization Results</h3>
                <p className="mt-1 break-all text-[11px] text-theme-muted">{`${group.model} · ${group.guide} · per Slice ${prefill}P×TP${prefillTp} / ${decode}D×TP${decodeTp} · ${policyLabel(group.placementPolicy)} · EPP ${group.sliceRouting === 'prefer' ? 'prefer same Slice' : 'Guide default'}`}</p>
                <p className="mt-0.5 text-[11px] text-theme-muted">{`SLO: ${sloTargetsLabel(group.slaTargets)}`}</p>
            </div>
            <label className="block text-[11px] text-theme-muted">Target load (req/s)
                <input aria-label="Target load" type="number" min="0" step="any" className={cn(inputClass, 'block')} value={target} placeholder="e.g. 8" onChange={event => setTarget(event.target.value)} />
            </label>
        </div>
        <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
                <thead><tr>{['Slices', 'Accelerators', 'SLO-stable req/s', 'req/s per accelerator', 'tok/s', 'Result'].map(label => <th key={label} className="p-2 font-medium text-theme-muted">{label}</th>)}</tr></thead>
                <tbody>
                    {group.rows.map(row => {
                        const [tone, label] = rowResult(row, target);
                        return <tr key={row.caseId} className={cn('border-t border-theme-border', advice.row === row && 'bg-emerald-500/5')}>
                            <td className="p-2 font-semibold">{row.slices}</td>
                            <td className="p-2">{row.accelerators}</td>
                            <td className="p-2">{row.maximumStableQps ?? '—'}</td>
                            <td className="p-2">{row.maximumStableQps != null ? Number((row.maximumStableQps / row.accelerators).toFixed(3)) : '—'}</td>
                            <td className="p-2">{row.throughputTps != null ? Number(row.throughputTps.toFixed(1)) : '—'}</td>
                            <td className="p-2"><Badge tone={tone} size="xs">{label}</Badge></td>
                        </tr>;
                    })}
                </tbody>
            </table>
        </div>
        <div data-slice-recommendation className="flex flex-wrap items-center justify-between gap-3 rounded-md bg-slate-100 px-3 py-2 text-xs dark:bg-slate-900/60">
            <p className="text-theme-text">{{
                verified: advice.row && `Recommended: ${advice.row.slices} Slice${advice.row.slices === 1 ? '' : 's'} — the fewest accelerators (${advice.row.accelerators}) whose SLO-stable rate meets ${target} req/s.`,
                'needs-target': 'Enter a target load to choose the lowest-cost Slice count that meets it.',
                unmet: `No tested Slice count reached ${target} req/s under this SLO.`,
                'no-slo-rate': 'No SLO-stable rate was recorded (the benchmark had no request-rate stages). Throughput alone is not used to recommend a Slice count.',
            }[advice.status]}</p>
            {advice.status === 'verified' && canDeploy && <Button size="xs" disabled={!advice.row.artifactId} onClick={() => onUseConfiguration(advice.row)}>Use Recommended Configuration</Button>}
        </div>
        <p className="text-[10px] text-theme-muted">More Slices also add accelerators, so this compares capacity scaling, not topology effects at an equal budget. These results stay with the Evaluation and are listed as evidence in the Slice panel for the same model, Guide and cluster. Using a configuration opens it in a new Evaluation, where the Slice panel shows the current capacity.</p>
    </Panel>;
}

export default function SliceOptimizationResults({ groups, canDeploy = false, onUseConfiguration }) {
    return <section data-slice-optimization className="min-w-0 space-y-4">
        {groups.map(group => <SliceGroup key={group.key} group={group} canDeploy={canDeploy} onUseConfiguration={onUseConfiguration} />)}
    </section>;
}
