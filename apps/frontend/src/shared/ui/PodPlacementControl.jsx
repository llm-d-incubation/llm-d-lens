import React, { useEffect, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Select } from './FormControls';
import { cn } from '../lib/cn';

export function RuntimeClassControl({ value, onChange, clusterId, listRuntimeClasses }) {
    const [discovery, setDiscovery] = useState({ clusterId: null, items: [], error: '' });
    useEffect(() => {
        if (!clusterId) return undefined;
        const controller = new AbortController();
        listRuntimeClasses(clusterId, controller.signal).then((result) => {
            if (!controller.signal.aborted) setDiscovery({ clusterId, items: result.items, error: '' });
        }).catch((error) => {
            if (!controller.signal.aborted) setDiscovery({ clusterId, items: [], error: error.message });
        });
        return () => controller.abort();
    }, [clusterId, listRuntimeClasses]);
    const items = discovery.clusterId === clusterId ? discovery.items : [];
    const selected = value?.mode === 'named' ? `named:${value.name}` : value?.mode || 'auto';
    return <div className="grid gap-2 text-theme-text">
        <label className="text-xs">RuntimeClass
            <Select className="mt-1" value={selected} onChange={(event) => {
                const selection = event.target.value;
                onChange(selection.startsWith('named:') ? { mode: 'named', name: selection.slice(6) } : { mode: selection });
            }}>
                <option value="auto">Automatic</option>
                <option value="node-default">Node default runtime</option>
                {value?.mode === 'named' && !items.some((item) => item.name === value.name) && <option value={selected}>{value.name} (not verified)</option>}
                {items.map((item) => <option key={item.name} value={`named:${item.name}`}>{item.name}</option>)}
            </Select>
        </label>
        {clusterId && discovery.clusterId !== clusterId && <span role="status" className="text-xs text-theme-muted">Loading RuntimeClasses...</span>}
        {discovery.clusterId === clusterId && discovery.error && <span role="alert" className="text-xs text-theme-muted">RuntimeClass discovery failed: {discovery.error}</span>}
    </div>;
}

const descriptions = {
    'prefer-same-node': 'Prefer co-locating replicas, but allow other nodes when needed.',
    'same-node': 'Require all replicas on one eligible node. Replicas may remain Pending if it cannot fit them.',
    balanced: 'Distribute replicas evenly across eligible nodes. Insufficient capacity may leave replicas Pending.',
    spread: 'Require a separate eligible node for each replica. Extra replicas remain Pending if there are too few nodes.',
};

export default function PodPlacementControl({ value, onChange, clusterId, nodeName, onNodeChange, listNodeLabels, className = '', compact = false }) {
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

    if (compact) return <details className={className}>
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
