import { Database, HardDrive, Info, Loader2, Network, Trash2 } from 'lucide-react';
import { PermissionGate } from '../../../auth';
import {
    deletionBlockedReason,
    effectiveStorageStatus,
    kindLabel,
    nodeDriftMessage,
    purposeLabel,
    resourcePhaseDotClass,
    statusDotClass,
    statusLabel,
    volumeLocation,
    volumeTitle,
} from './storagePresentation';

const KIND_ICONS = {
    'local-disk': HardDrive,
    nfs: Network,
    'dynamic-pvc': Database,
};

function KindIcon({ kind }) {
    const Icon = KIND_ICONS[kind] || HardDrive;
    return <Icon size={13} className="shrink-0 text-slate-500" aria-hidden="true" />;
}

function InUseBadge({ item }) {
    const blocked = deletionBlockedReason(item);
    if (!blocked) return null;
    return (
        <span
            className="storage-inuse-tag inline-flex items-center gap-1 rounded-full border border-amber-700/60 bg-amber-900/20 px-1.5 py-0.5 text-[10px] text-amber-300"
            title={blocked}
        >
            <Info size={11} className="shrink-0" aria-hidden="true" />
            In use
        </span>
    );
}

function StatusDot({ status, title }) {
    const label = title || statusLabel(status);
    return (
        <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${statusDotClass(status)}`}
            title={label}
            aria-label={label}
        />
    );
}

function PurposeBadges({ item, purposes, inline = false }) {
    const hasPurposes = Array.isArray(purposes) && purposes.length > 0;
    if (!item && !hasPurposes) return null;
    return (
        <div className={`flex flex-wrap items-center gap-1 ${inline ? 'mt-1.5' : 'mt-1'}`}>
            {hasPurposes && purposes.map((purpose) => (
                <span
                    key={purpose}
                    className="storage-purpose-tag inline-flex items-center rounded-full border border-sky-400/30 bg-sky-400/10 px-1.5 py-0.5 text-[10px] text-sky-200"
                >
                    {purposeLabel(purpose)}
                </span>
            ))}
            {item && <InUseBadge item={item} />}
        </div>
    );
}

function ResourceDetail({ resource }) {
    if (!resource) return <span className="text-[#1f2937] dark:text-slate-600">Not found</span>;
    return (
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px] text-[#1f2937] dark:text-slate-400">
            <span
                className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${resourcePhaseDotClass(resource.phase)}`}
                title={resource.phase}
                aria-label={resource.phase}
            />
            {resource.namespace ? `${resource.namespace}/` : ''}{resource.name}
            <span className="ml-2 text-slate-500">
                Capacity: {resource.capacity || '—'} · Access: {(resource.accessModes || []).join(', ') || '—'}
            </span>
        </span>
    );
}

// Human-readable tooltip explaining why the status dot shows a different
// color than the volume's own `status` field (see effectiveStorageStatus).
function resourceStatusTitle(item, pvc, pv) {
    const effective = effectiveStorageStatus(item, pvc, pv);
    if (effective === item.status) return undefined;
    if (effective === 'attention') return nodeDriftMessage(item);
    const pvcText = pvc ? pvc.phase || 'Unknown' : 'not found';
    const pvText = pv ? pv.phase || 'Unknown' : 'not found';
    return `${statusLabel(item.status)} (PVC: ${pvcText}, PV: ${pvText})`;
}

function RowActions({ item, busy, onDelete }) {
    const blocked = deletionBlockedReason(item);
    const name = volumeTitle(item);
    return (
        <div className="flex items-center justify-end gap-1">
            {busy && <Loader2 size={14} className="mr-1 animate-spin text-cyan-300" aria-hidden="true" />}
            <PermissionGate permission="storage:volume:delete">
                <button
                    type="button"
                    onClick={(event) => {
                        event.stopPropagation();
                        onDelete(item);
                    }}
                    disabled={busy || Boolean(blocked)}
                    aria-label={`Delete ${name}`}
                    title={blocked || 'Delete storage volume'}
                    className="rounded-lg p-1.5 text-slate-400 transition hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-40"
                >
                    <Trash2 size={14} />
                </button>
            </PermissionGate>
        </div>
    );
}

export function StorageVolumeTable({ items, resourceStatuses = {}, mutatingId, clusterNameById = {}, onDelete, selectedId, onSelectRow }) {
    return (
        <>
            {/* Desktop table */}
            <div className="hidden overflow-hidden rounded-xl border border-slate-800/60 lg:block">
                <table className="w-full table-fixed border-collapse text-left text-xs">
                    <thead className="bg-slate-950/60 text-slate-400">
                        <tr>
                            <th scope="col" className="w-auto px-4 py-2 text-[9px] font-medium">Name</th>
                            <th scope="col" className="w-16 px-3 py-2 text-right text-[9px] font-medium">Actions</th>
                        </tr>
                    </thead>
                    <tbody className="storage-volume-rows divide-y divide-slate-800/70">
                        {items.map((item) => {
                            const busy = mutatingId === item.id;
                            const claims = (resourceStatuses[item.id] || {}).persistentVolumeClaims || [];
                            const pvc = claims.length ? claims[0] : null;
                            const pv = pvc ? pvc.persistentVolume : null;
                            const isSelected = selectedId === item.id;
                            return (
                                <tr
                                    key={item.id}
                                    aria-busy={busy}
                                    aria-selected={isSelected}
                                    onClick={() => onSelectRow?.(item.id)}
                                    className={`cursor-pointer align-top transition-colors ${
                                        isSelected ? 'bg-cyan-500/10 hover:bg-cyan-500/15' : 'hover:bg-slate-800/40'
                                    }`}
                                >
                                    <td className={`min-w-0 border-l-2 px-4 py-3.5 ${isSelected ? 'border-l-[#037ffc]' : 'border-l-transparent'}`}>
                                        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                                            <StatusDot
                                                status={effectiveStorageStatus(item, pvc, pv)}
                                                title={resourceStatusTitle(item, pvc, pv)}
                                            />
                                            <KindIcon kind={item.kind} />
                                            <span
                                                className="min-w-0 truncate text-sm font-semibold text-slate-100"
                                                title={`${volumeTitle(item)} (${kindLabel(item.kind)})`}
                                            >
                                                {volumeTitle(item)}
                                            </span>
                                        </div>
                                        <PurposeBadges item={item} purposes={item.purposes} inline />
                                        {item.status === 'failed' && item.failureDetail && (
                                            <div className="mt-0.5 line-clamp-2 text-[11px] text-rose-300" title={item.failureDetail}>
                                                {item.failureDetail}
                                            </div>
                                        )}
                                    </td>
                                    <td className="border-l-2 border-l-transparent px-3 py-3.5">
                                        <RowActions item={item} busy={busy} onDelete={onDelete} />
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            {/* Narrow-screen cards */}
            <div className="flex flex-col gap-3 p-3 lg:hidden">
                {items.map((item) => {
                    const busy = mutatingId === item.id;
                    const claims = (resourceStatuses[item.id] || {}).persistentVolumeClaims || [];
                    const pvc = claims.length ? claims[0] : null;
                    const pv = pvc ? pvc.persistentVolume : null;
                    return (
                        <div key={item.id} aria-busy={busy} className={`rounded-xl border bg-slate-950/40 p-3 ${item.id === selectedId ? 'border-[#037ffc]' : 'border-slate-800/60'}`}>
                            <div className="flex items-start justify-between gap-2">
                                <div className="min-w-0">
                                    <div className="flex items-center gap-2">
                                        <StatusDot
                                            status={effectiveStorageStatus(item, pvc, pv)}
                                            title={resourceStatusTitle(item, pvc, pv)}
                                        />
                                        <span className="truncate text-sm font-semibold text-slate-100">{volumeTitle(item)}</span>
                                    </div>
                                    <div className="mt-0.5 text-[11px] text-slate-500">{kindLabel(item.kind)}</div>
                                    <PurposeBadges item={item} purposes={item.purposes} />
                                </div>
                            </div>
                            <dl className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
                                <div><dt className="text-[#9ca3af] dark:text-slate-600">Cluster</dt><dd className="truncate text-[#1f2937] dark:text-slate-300">{clusterNameById[item.clusterId] || item.clusterId || '—'}</dd></div>
                                <div className="col-span-2"><dt className="text-[#9ca3af] dark:text-slate-600">Location</dt><dd className="truncate font-mono text-[#1f2937] dark:text-slate-300">{volumeLocation(item)}</dd></div>
                            </dl>
                            <dl className="mt-3 space-y-2 border-t border-slate-800/60 pt-3 text-[11px]">
                                <div>
                                    <dt className="font-semibold text-[#9ca3af] dark:text-slate-400">PVC</dt>
                                    <dd className="mt-1"><ResourceDetail resource={pvc} /></dd>
                                    <div className="mt-2 pl-3">
                                        <dt className="font-semibold text-[#9ca3af] dark:text-slate-500">↳ PV</dt>
                                        <dd className="mt-1"><ResourceDetail resource={pv} /></dd>
                                    </div>
                                </div>
                            </dl>
                            <div className="mt-3 border-t border-slate-800/60 pt-2">
                                <RowActions item={item} busy={busy} onDelete={onDelete} />
                            </div>
                        </div>
                    );
                })}
            </div>
        </>
    );
}

export default StorageVolumeTable;
