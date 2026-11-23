import { useEffect, useState } from 'react';
import { Badge, Button, Input, Select } from '../../../../shared/ui/index';
import { listDeploymentNodeLabels } from '../../../optimization/ui/OptimizationWorkspace/remoteDeployBackend';
import { automaticSliceSizing, matchesSliceRecommendation, podPlacementConflict, sliceTopologyKeys } from '../../model/inference-designer/sliceLayout';

const TYPES = [
    ['None', 'None: scheduler decides'],
    ['ExclusiveSlice', 'ExclusiveSlice: co-locate each slice, spread slices'],
    ['ExclusiveTopology', 'ExclusiveTopology: one slice per domain, no sharing'],
];
const FIELDS = [
    ['slices', 'Slices', 'Copies of the whole prefill + decode topology.'],
    ['prefillReplicas', 'Prefill replicas / slice', ''],
    ['prefillTp', 'Prefill TP', ''],
    ['replicas', 'Decode replicas / slice', ''],
    ['tp', 'Decode TP', ''],
];

/** P/D slice sizing and placement; omitted values are recommended from current cluster topology. */
export function InferenceSliceParameters({ draft, change, recommendation, disabled }) {
    const runtime = draft.runtimeConfiguration || {};
    const [keys, setKeys] = useState([]), [error, setError] = useState('');
    useEffect(() => {
        const controller = new AbortController();
        listDeploymentNodeLabels(draft.clusterId, controller.signal)
            .then(items => { setKeys(sliceTopologyKeys(items)); setError(''); })
            .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
        return () => controller.abort();
    }, [draft.clusterId]);
    const policy = runtime.placementPolicy || { type: 'None' };
    const update = values => change({ runtimeConfiguration: { ...runtime, ...values } });
    const number = (key, input) => {
        if (key === 'slices' && input.value.trim() === '') {
            setError(''); update(automaticSliceSizing(runtime)); return;
        }
        const value = Number(input.value);
        if (!Number.isSafeInteger(value) || value < 1 || (key === 'slices' && value > 64)) {
            setError(`${key} must be a whole number${key === 'slices' ? ' from 1 to 64' : ' of at least 1'}; kept ${runtime[key] ?? 'Auto'}.`);
            input.value = runtime[key] ?? '';
            return;
        }
        setError(''); update({ [key]: value });
    };
    const devices = (runtime.slices || 1) * ((runtime.prefillReplicas || 0) * (runtime.prefillTp || 1) + (runtime.replicas || 1) * (runtime.tp || 1));
    const matches = matchesSliceRecommendation(runtime, recommendation);
    const auto = runtime.slices === undefined && !runtime.placementPolicy;
    const conflict = podPlacementConflict(runtime.podPlacement, runtime.slices || 1, policy);
    const hostnameBlocked = podPlacementConflict(runtime.podPlacement, runtime.slices || 1, { type: 'ExclusiveSlice', topology: 'kubernetes.io/hostname' });
    const firstKey = keys.find(key => !(hostnameBlocked && key === 'kubernetes.io/hostname'));
    return <section aria-label="P/D slice settings" className="space-y-3 rounded-lg border border-theme-border p-3">
        <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-sm font-semibold">P/D slices and placement</h4><Badge tone={matches || auto ? 'success' : 'neutral'} size="xs">{auto ? 'Auto' : matches ? 'Recommended' : 'Custom'}</Badge></div>
        {recommendation && <div className="space-y-2 text-xs text-theme-muted">
            <p>Recommended: {recommendation.slices} slice{recommendation.slices > 1 ? 's' : ''} × {recommendation.prefillReplicas}P/{recommendation.replicas}D · {recommendation.placementPolicy?.type}{recommendation.placementPolicy?.topology ? ` on ${recommendation.placementPolicy.topology}` : ''}. {recommendation.reason}</p>
            {!matches && <Button size="xs" variant="secondary" disabled={disabled} onClick={() => update({ slices: recommendation.slices, prefillReplicas: recommendation.prefillReplicas, replicas: recommendation.replicas, placementPolicy: recommendation.placementPolicy })}>Use recommendation</Button>}
        </div>}
        <div className="grid gap-3 sm:grid-cols-2">
            {FIELDS.map(([key, label, hint]) => <label key={key} className="block text-sm">{label}
                <Input key={`${key}:${runtime[key]}`} className="mt-1" type="number" min="1" max={key === 'slices' ? 64 : undefined} placeholder={key === 'slices' ? 'Auto (recommended on apply)' : undefined} disabled={disabled} defaultValue={runtime[key] ?? ''} onBlur={event => { if (!(event.target.value === '' && runtime[key] === undefined) && Number(event.target.value) !== runtime[key]) number(key, event.target); }} />
                {hint && <span className="mt-1 block text-xs text-theme-muted">{hint}</span>}
            </label>)}
            <label className="block text-sm">Slice placement policy
                <Select className="mt-1" aria-label="Slice placement policy" disabled={disabled} value={policy.type} onChange={event => update({ placementPolicy: event.target.value === 'None' ? { type: 'None' } : { type: event.target.value, topology: policy.topology || recommendation?.placementPolicy?.topology || firstKey || 'kubernetes.io/hostname' } })}>
                    {TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </Select>
            </label>
            {policy.type !== 'None' && <label className="block text-sm">Topology key
                <Select className="mt-1" aria-label="Slice topology key" disabled={disabled} value={policy.topology || ''} onChange={event => update({ placementPolicy: { ...policy, topology: event.target.value } })}>
                    {[...new Set([policy.topology, ...keys].filter(Boolean))].map(key => <option key={key} value={key} disabled={Boolean(hostnameBlocked) && key === 'kubernetes.io/hostname'}>{key}{hostnameBlocked && key === 'kubernetes.io/hostname' ? ' (conflicts with Pod placement)' : ''}</option>)}
                </Select>
            </label>}
        </div>
        <p className="text-xs text-theme-muted">Total: {devices} accelerators. Non-None placement is a hard scheduling rule; Pods stay Pending if no domain can hold a whole slice. Apply changes to re-check resources.</p>
        <p className="text-xs text-theme-muted">Pod placement (chosen at setup): {runtime.podPlacement || 'prefer-same-node'}.{hostnameBlocked ? ' Node-scoped slice placement is unavailable with this choice.' : ''}</p>
        {conflict && <p role="alert" className="text-xs">{conflict}</p>}
        {error && <p role="alert" className="text-xs">{error}</p>}
    </section>;
}
