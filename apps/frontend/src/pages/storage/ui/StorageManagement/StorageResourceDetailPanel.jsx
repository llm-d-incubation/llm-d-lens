import { formatTimestamp } from '../../../../shared/lib/formatTimestamp';
import { ChevronDown, Info, Loader2, RefreshCw } from 'lucide-react';
import {
    capacityLabel,
    effectiveStorageStatus,
    hasNodeDrift,
    isCapacityDeclaredOnly,
    kindLabel,
    nodeDriftMessage,
    purposeLabel,
    resourcePhaseDotClass,
    shortId,
    statusLabel,
    volumeLocation,
    volumeTitle,
} from './storagePresentation';

// Right-hand detail panels for the currently selected Storage volume's live
// PVC and PV objects. The summary answers "is it healthy and what is it?";
// low-frequency Kubernetes fields stay behind a disclosure.

const PHASE_CHIP_STATUS = {
    Bound: 'ready',
    Available: 'ready',
    Pending: 'pending',
    Released: 'pending',
    Lost: 'failed',
};

// Deliberate light/dark palettes. The light badges use tinted paper surfaces
// and dark ink instead of reusing translucent dark-mode fills.
const BADGE_TONES = {
    ready: 'border-emerald-300 bg-white text-emerald-700 dark:border-emerald-500/25 dark:bg-emerald-500/10 dark:text-emerald-300',
    pending: 'border-amber-300 bg-white text-amber-700 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300',
    attention: 'border-orange-300 bg-white text-orange-700 dark:border-orange-500/25 dark:bg-orange-500/10 dark:text-orange-300',
    failed: 'border-rose-300 bg-white text-rose-700 dark:border-rose-500/25 dark:bg-rose-500/10 dark:text-rose-300',
    inactive: 'border-slate-300 bg-white text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400',
};

function Badge({ tone, children }) {
    return (
        <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${BADGE_TONES[tone] || BADGE_TONES.inactive}`}>
            {children}
        </span>
    );
}

function PhaseDot({ phase }) {
    const label = phase || 'Unknown';
    return (
        <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${resourcePhaseDotClass(label)}`}
            title={label}
            aria-label={label}
        />
    );
}

function StatusBadge({ status, label }) {
    return <Badge tone={status}>{label || statusLabel(status)}</Badge>;
}

function PhaseBadge({ phase }) {
    return <Badge tone={PHASE_CHIP_STATUS[phase] || 'inactive'}>{phase || 'Not found'}</Badge>;
}

function FieldRow({ label, children, mono = false }) {
    return (
        <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-3 py-2 text-xs">
            <dt className="text-[11px] font-medium leading-5 text-slate-500">{label}</dt>
            <dd className={`min-w-0 break-words leading-5 text-slate-700 dark:text-slate-300 ${mono ? 'font-mono' : ''}`}>
                {children}
            </dd>
        </div>
    );
}

function ResourceFields({ resource }) {
    if (!resource) {
        return <p className="text-xs text-slate-500">Not found</p>;
    }
    const createdAt = formatTimestamp(resource.createdAt, { invalid: (value) => value });
    return (
        <div className="grid gap-x-8 gap-y-5 md:grid-cols-2">
            <section className="min-w-0">
                <h4 className="mb-1 text-xs font-semibold text-slate-700 dark:text-slate-300">Identity</h4>
                <dl className="divide-y divide-slate-200 dark:divide-slate-800/60">
                    <FieldRow label="Name" mono>{resource.name || '—'}</FieldRow>
                    {resource.namespace && <FieldRow label="Namespace" mono>{resource.namespace}</FieldRow>}
                    {resource.uid && <FieldRow label="UID" mono>{resource.uid}</FieldRow>}
                </dl>
            </section>

            <section className="min-w-0">
                <h4 className="mb-1 text-xs font-semibold text-slate-700 dark:text-slate-300">Capacity and access</h4>
                <dl className="divide-y divide-slate-200 dark:divide-slate-800/60">
                    <FieldRow label="Capacity">{resource.capacity || '—'}</FieldRow>
                    <FieldRow label="Access modes">{(resource.accessModes || []).join(', ') || '—'}</FieldRow>
                    <FieldRow label="Volume mode">{resource.volumeMode || '—'}</FieldRow>
                    <FieldRow label="Storage class">{resource.storageClassName || '—'}</FieldRow>
                    {resource.reclaimPolicy && <FieldRow label="Reclaim policy">{resource.reclaimPolicy}</FieldRow>}
                </dl>
            </section>

            <section className="min-w-0 md:col-span-2">
                <h4 className="mb-1 text-xs font-semibold text-slate-700 dark:text-slate-300">Binding and lifecycle</h4>
                <dl className="divide-y divide-slate-200 dark:divide-slate-800/60">
                    {resource.source && (
                        <FieldRow label="Source" mono>
                            {resource.source.type}: {resource.source.detail || '—'}
                        </FieldRow>
                    )}
                    {resource.volumeName && <FieldRow label="Bound volume" mono>{resource.volumeName}</FieldRow>}
                    {createdAt && <FieldRow label="Created">{createdAt}</FieldRow>}
                </dl>
            </section>
        </div>
    );
}

function PurposeBadges({ purposes }) {
    if (!Array.isArray(purposes) || purposes.length === 0) return null;
    return (
        <div className="flex flex-wrap gap-1">
            {purposes.map((purpose) => (
                <span
                    key={purpose}
                    className="storage-purpose-tag inline-flex items-center rounded-full border border-sky-400/30 bg-sky-400/10 px-1.5 py-0.5 text-[10px] text-sky-200"
                >
                    {purposeLabel(purpose)}
                </span>
            ))}
        </div>
    );
}

function VolumeSummary({ item, pvc, clusterNameById, mutatingId, onAcknowledgeNodes }) {
    const effectiveStatus = effectiveStorageStatus(item, pvc, pvc?.persistentVolume || null);
    const drift = hasNodeDrift(item);
    const acknowledging = mutatingId === item.id;
    const accessModes = (pvc?.accessModes || []).join(', ') || '—';
    const storageClass = pvc?.storageClassName || pvc?.persistentVolume?.storageClassName || '—';

    return (
        <div className="storage-detail-card shrink-0 overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-800/60 dark:bg-slate-950/40">
            <div className="p-4 pb-6">
                <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={effectiveStatus} />
                    <PurposeBadges purposes={item.purposes} />
                </div>
                <h2 className="mt-2 truncate text-xl font-semibold text-slate-900 dark:text-slate-100" title={volumeTitle(item)}>
                    {volumeTitle(item)}
                </h2>
                <p className="mt-1 truncate text-xs text-slate-500">
                    {kindLabel(item.kind)} · {clusterNameById[item.clusterId] || item.clusterId || '—'} · #{shortId(item.id)}
                </p>

                {item.status === 'failed' && item.failureDetail && (
                    <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-2.5 text-[11px] leading-5 text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/5 dark:text-rose-300">
                        {item.failureDetail}
                    </p>
                )}
                {drift && (
                    <div className="mt-3 flex flex-wrap items-start gap-2 rounded-lg border border-orange-200 bg-orange-50 p-2.5 text-[11px] leading-5 text-orange-800 dark:border-orange-500/20 dark:bg-orange-500/5 dark:text-orange-200">
                        <Info size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <p className="min-w-0 flex-1">{nodeDriftMessage(item)}</p>
                        <button
                            type="button"
                            onClick={() => onAcknowledgeNodes?.(item)}
                            disabled={acknowledging}
                            className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-orange-300 px-2 py-1 text-[11px] font-semibold text-orange-800 transition hover:bg-orange-100 disabled:opacity-50 dark:border-orange-400/40 dark:text-orange-100 dark:hover:bg-orange-400/20"
                        >
                            {acknowledging ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                            Acknowledge
                        </button>
                    </div>
                )}

                <dl className="mt-4 grid gap-4 border-t border-slate-200 pt-4 dark:border-slate-800/60 sm:grid-cols-3">
                    <div>
                        <dt className="text-[11px] font-medium text-slate-500">Capacity</dt>
                        <dd className="mt-1 flex items-center gap-1 text-lg font-semibold leading-none text-slate-900 dark:text-slate-100">
                            {capacityLabel(item)}
                            {isCapacityDeclaredOnly(item) && (
                                <span className="inline-flex shrink-0 items-center" title="Declared capacity, not enforced by the backend">
                                    <Info size={11} className="text-slate-500" aria-hidden="true" />
                                </span>
                            )}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-[11px] font-medium text-slate-500">Access modes</dt>
                        <dd className="mt-1 text-xs text-slate-700 dark:text-slate-300">{accessModes}</dd>
                    </div>
                    <div>
                        <dt className="text-[11px] font-medium text-slate-500">Storage class</dt>
                        <dd className="mt-1 truncate text-xs text-slate-700 dark:text-slate-300" title={storageClass}>{storageClass}</dd>
                    </div>
                    <div className="col-span-full min-w-0">
                        <dt className="text-[11px] font-medium text-slate-500">Location</dt>
                        <dd className="mt-1 truncate font-mono text-xs text-slate-700 dark:text-slate-300" title={volumeLocation(item)}>
                            {volumeLocation(item)}
                        </dd>
                    </div>
                </dl>
            </div>
        </div>
    );
}

function BindingRow({ label, resource }) {
    const phase = resource?.phase;
    return (
        <details className="group/binding">
            <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-2 bg-white px-4 py-3 transition-colors marker:content-none hover:bg-slate-50 dark:bg-transparent dark:hover:bg-slate-900/30">
                <span className="w-10 shrink-0 text-[11px] font-semibold text-slate-500">{label}</span>
                <PhaseDot phase={phase} />
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-slate-800 dark:text-slate-200" title={resource?.name || 'Not found'}>
                    {resource?.name || 'Not found'}
                </span>
                <PhaseBadge phase={phase} />
                {resource?.capacity && <span className="text-[11px] text-slate-500">{resource.capacity}</span>}
                <ChevronDown size={14} className="shrink-0 text-slate-400 transition-transform group-open/binding:rotate-180" aria-hidden="true" />
            </summary>
            <div className="border-t border-slate-200 bg-slate-50/60 p-4 dark:border-slate-800/60 dark:bg-slate-900/20">
                <ResourceFields resource={resource} />
            </div>
        </details>
    );
}

export function StorageResourceDetailPanel({ item, resourceStatuses = {}, clusterNameById = {}, mutatingId, onAcknowledgeNodes }) {
    if (!item) {
        return (
            <div className="storage-detail-card flex h-full min-h-[16rem] items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50 p-4 text-center text-xs text-slate-500 dark:border-slate-800/60 dark:bg-slate-950/20">
                Select a storage volume to view its PVC and PV status.
            </div>
        );
    }
    const claims = (resourceStatuses[item.id] || {}).persistentVolumeClaims || [];
    const pvc = claims.length ? claims[0] : null;
    const pv = pvc ? pvc.persistentVolume : null;
    return (
        <div className="flex h-full flex-col gap-3">
            <VolumeSummary
                item={item}
                pvc={pvc}
                clusterNameById={clusterNameById}
                mutatingId={mutatingId}
                onAcknowledgeNodes={onAcknowledgeNodes}
            />
            <section className="storage-detail-card shrink-0 overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-800/60 dark:bg-slate-950/40">
                <div className="flex items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800/60 dark:bg-transparent">
                    <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Storage binding</h3>
                    <span className="text-[11px] text-slate-500">PVC → PV</span>
                </div>
                <div className="divide-y divide-slate-200 dark:divide-slate-800/60">
                    <BindingRow label="PVC" resource={pvc} />
                    <BindingRow label="PV" resource={pv} />
                </div>
            </section>
        </div>
    );
}

export default StorageResourceDetailPanel;
