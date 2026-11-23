import React, { useEffect, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { PickerSelect, Select } from './FormControls';
import { cn } from '../lib/cn';

const descriptions = {
    'prefer-same-node': 'Prefer co-locating replicas, but allow other nodes when needed.',
    'same-node': 'Require all replicas on one eligible node. Replicas may remain Pending if it cannot fit them.',
    balanced: 'Distribute replicas evenly across eligible nodes. Insufficient capacity may leave replicas Pending.',
    spread: 'Require a separate eligible node for each replica. Extra replicas remain Pending if there are too few nodes.',
};

export default function PodPlacementControl({ value, onChange, clusterId, nodeName, onNodeChange, listNodeLabels, className = '', compact = false, defaultOpen = false, inline = false }) {
    const [nodeNames, setNodeNames] = useState([]);
    const [nodeError, setNodeError] = useState('');
    const [open, setOpen] = useState(false);

    useEffect(() => {
        if (value !== 'same-node' || !clusterId || !listNodeLabels) return undefined;
        const controller = new AbortController();
        listNodeLabels(clusterId, controller.signal).then((labels) => {
            setNodeNames([...new Set(labels.filter((label) => label.key === 'kubernetes.io/hostname').map((label) => label.value))].sort());
            setNodeError('');
        }).catch((error) => {
            if (!controller.signal.aborted) { setNodeNames([]); setNodeError(error.message); }
        });
        return () => controller.abort();
    }, [value, clusterId, listNodeLabels]);

    const fields = <div className="mt-3 grid gap-3">
        <label className="block text-xs text-slate-400">Replica placement
            <Select className="mt-1 h-9 px-2 dark:bg-slate-950 dark:text-slate-200" value={value} onChange={(event) => { onChange(event.target.value); onNodeChange(''); }}>
                <option value="prefer-same-node">Prefer one node (default)</option>
                <option value="same-node">Require one node</option>
                <option value="balanced">Distribute evenly across nodes</option>
                <option value="spread">Require separate nodes</option>
            </Select>
        </label>
        {value === 'same-node' && <label className="block text-xs text-slate-400">Node (optional)
            <Select className="mt-1 h-9 px-2 dark:bg-slate-950 dark:text-slate-200" value={nodeName} onChange={(event) => onNodeChange(event.target.value)} disabled={!clusterId || Boolean(nodeError)}>
                <option value="">Any eligible node</option>
                {nodeNames.map((name) => <option key={name} value={name}>{name}</option>)}
            </Select>
            {nodeError && <span role="alert" className="mt-1 block text-xs text-red-300">Unable to load nodes: {nodeError}</span>}
        </label>}
        <p className="text-xs leading-5 text-slate-400">{descriptions[value]}</p>
    </div>;

    if (inline) return <div className={cn('grid gap-2', className)}>
        <PickerSelect aria-label="Pod placement" title={descriptions[value]} value={value} onChange={(next) => { onChange(next); onNodeChange(''); }} options={[
            { value: 'prefer-same-node', label: 'Prefer one node' },
            { value: 'same-node', label: 'Require one node' },
            { value: 'balanced', label: 'Distribute evenly' },
            { value: 'spread', label: 'Require separate nodes' },
        ]} />
        {['balanced', 'spread'].includes(value) && <p className="text-xs text-amber-600 dark:text-amber-300">Not compatible with P/D slices: Prefill and Decode Pods are split across nodes.</p>}
        {value === 'same-node' && <PickerSelect aria-label="Placement node" value={nodeName} onChange={onNodeChange} disabled={!clusterId || Boolean(nodeError)}
            options={[{ value: '', label: 'Any eligible node' }, ...nodeNames.map((name) => ({ value: name, label: name }))]} />}
        {nodeError && <span role="alert" className="text-xs text-red-300">Unable to load nodes: {nodeError}</span>}
    </div>;

    if (compact) return <details className={className} open={defaultOpen || undefined}>
        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold text-slate-300"><ChevronDown size={14} aria-hidden="true" />Pod placement</summary>
        {fields}
    </details>;

    return <section className={className}>
        <button type="button" onClick={() => setOpen((current) => !current)} aria-expanded={open} className="flex w-full items-center justify-between text-sm font-medium text-white">
            <span>Pod placement</span><ChevronDown size={16} className={cn('transition', open && 'rotate-180')} aria-hidden="true" />
        </button>
        {open && fields}
    </section>;
}
