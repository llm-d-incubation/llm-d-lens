import { formatPodStatusLabel, isPodFailureReason } from './deploymentPresentation';

// Small pulsing status dot + tooltip for a single pod, shared by the pods/logs
// modal and the inline pod list under an expanded deployment row.

export function PodStatusBadge({ phase, reason, ready }) {
    let dotColor = 'bg-slate-500';
    let ringColor = 'ring-slate-500/30';
    const textStatus = formatPodStatusLabel({ phase, reason, ready });

    if (phase === 'Failed' || isPodFailureReason(reason)) {
        dotColor = 'bg-rose-500';
        ringColor = 'ring-rose-500/30';
    } else if (reason === 'Terminating') {
        dotColor = 'bg-slate-400 animate-pulse';
        ringColor = 'ring-slate-400/30';
    } else if (phase === 'Succeeded' || reason === 'Completed') {
        dotColor = 'bg-slate-400';
        ringColor = 'ring-slate-400/30';
    } else if (phase === 'Running' && ready) {
        dotColor = 'bg-emerald-400';
        ringColor = 'ring-emerald-500/30';
    } else if (phase === 'Running' && !ready && (!reason || reason === 'Running' || reason === 'Starting')) {
        dotColor = 'bg-sky-400 animate-pulse';
        ringColor = 'ring-sky-500/30';
    } else if (
        phase === 'Pending'
        || reason === 'ContainerCreating'
        || reason === 'PodInitializing'
        || reason?.startsWith('Init:')
    ) {
        dotColor = 'bg-cyan-400 animate-pulse';
        ringColor = 'ring-cyan-500/30';
    }

    return (
        <span
            className="group/status relative inline-flex items-center shrink-0 cursor-help"
            title={`Status: ${textStatus}${ready ? ' (Ready)' : ''}`}
        >
            <span className={`h-2.5 w-2.5 rounded-full ${dotColor} ring-2 ${ringColor} transition-transform group-hover/status:scale-125`} />
            <span className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover/status:flex whitespace-nowrap rounded bg-slate-800 px-2 py-0.5 text-[10px] text-slate-100 shadow-lg border border-slate-700 z-30">
                {textStatus}{ready ? ' (Ready)' : ''}
            </span>
        </span>
    );
}

export default PodStatusBadge;
