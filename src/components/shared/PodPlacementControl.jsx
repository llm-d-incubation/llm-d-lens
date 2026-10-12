import { useEffect, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Select } from '../ui/FormControls';
import { listDeploymentNodeLabels } from '../OptimizationWorkspace/remoteDeployBackend';
import { cn } from '../../utils/cn';

const descriptions = {
    'prefer-same-node': 'Prefer co-locating replicas, but allow other nodes when needed.',
    'same-node': 'Require all replicas on one eligible node. Replicas may remain Pending if it cannot fit them.',
    balanced: 'Distribute replicas evenly across eligible nodes. Insufficient capacity may leave replicas Pending.',
    spread: 'Require a separate eligible node for each replica. Extra replicas remain Pending if there are too few nodes.',
};

export default function PodPlacementControl({ value, onChange, clusterId, nodeName, onNodeChange, className = '', compact = false }) {
    const [nodeNames, setNodeNames] = useState([]);
    const [nodeError, setNodeError] = useState('');
    const [open, setOpen] = useState(false);

    useEffect(() => {
        if (value !== 'same-node' || !clusterId) return undefined;
        const controller = new AbortController();
        listDeploymentNodeLabels(clusterId, controller.signal).then((labels) => {
            setNodeNames([...new Set(labels.filter((label) => label.key === 'kubernetes.io/hostname').map((label) => label.value))].sort());
            setNodeError('');
        }).catch((error) => {
            if (!controller.signal.aborted) { setNodeNames([]); setNodeError(error.message); }
        });
        return () => controller.abort();
    }, [value, clusterId]);

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
