import { useEffect, useRef, useState } from 'react';
import { Badge, Button, Modal, Select } from '../../shared/ui/index';
import { listModelCacheEntries } from '../../pages/model-cache/ui/ModelCache/modelCacheBackend';
import { sourceRepository, hasPendingSync } from '../../pages/model-cache/ui/ModelCache/modelCachePresentation';
import { listStorageVolumes } from '../../pages/storage/ui/StorageManagement/storageManagementBackend';
import { usePolling } from '../../shared/lib/usePolling';

/** Select existing cluster-owned cache and storage records; never invent mount paths. */
export function InferenceTargetSetup({ open, draft, resources, hardwareLoading, change, onClose, onNavigate }) {
    const [inventory, setInventory] = useState({ clusterId: '', entries: [], volumes: [] });
    const [error, setError] = useState('');
    const request = useRef(0);
    const refresh = async () => {
        if (!draft.clusterId) return;
        const id = ++request.current;
        try {
            const [entries, volumes] = await Promise.all([listModelCacheEntries({ clusterId: draft.clusterId }), listStorageVolumes({ clusterId: draft.clusterId })]);
            if (id === request.current) { setInventory({ clusterId: draft.clusterId, entries, volumes }); setError(''); }
        } catch (reason) { if (id === request.current) setError(reason.message); }
    };
    useEffect(() => () => { request.current += 1; }, []);
    usePolling(refresh, { enabled: open && !!draft.clusterId, immediate: true });
    const volumes = inventory.clusterId === draft.clusterId ? inventory.volumes : [];
    const entries = inventory.clusterId === draft.clusterId ? inventory.entries.filter(entry => entry.clusterId === draft.clusterId && entry.status === 'ready' && !hasPendingSync(entry) && entry.source?.kind === 'huggingface' && volumes.some(volume => volume.id === entry.storageVolumeId && volume.clusterId === draft.clusterId && volume.status === 'ready')) : [];
    const selected = entries.find(entry => entry.id === draft.modelCacheEntryId);
    const volume = volumes.find(item => item.id === selected?.storageVolumeId);
    const ready = !!draft.clusterId && !!draft.profileId && !!selected && !!volume && !error;
    const selectModel = id => {
        const entry = entries.find(item => item.id === id), storage = volumes.find(item => item.id === entry?.storageVolumeId);
        change({ modelCacheEntryId: id, model: sourceRepository(entry), storageVolumeId: storage?.id || '', sourceNamespace: storage?.dynamicPvc?.namespace || 'llm-d-bench-storage' });
    };
    return <Modal isOpen={open} onClose={onClose} title="Choose your deployment target" subtitle="1 · Target and model     2 · Select optimizations     3 · Generate and validate" size="lg" closeOnBackdrop={false} footer={<Button disabled={!ready} onClick={onClose}>Continue to optimization map</Button>}>
        <div className="inference-setup-grid">
            <label>Target cluster<Select aria-label="Setup target cluster" value={draft.clusterId} onChange={event => change({ clusterId: event.target.value })}><option value="">Select a cluster</option>{resources.clusters.map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</Select><small>Shares the selected cluster with Clusters, Storage and Model Cache.</small></label>
            <div><span>Accelerator</span>{resources.profiles.length > 1 ? <Select aria-label="Cluster accelerator" value={draft.profileId} onChange={event => change({ profileId: event.target.value, variant: '', sourceUnits: [], sourceVariables: {}, sourceRouter: { ...draft.sourceRouter, values: [] } })}><option value="">Select a cluster accelerator</option>{resources.profiles.map(item => <option key={item.id} value={item.id} disabled={!item.upstream_variant}>{item.display_name || item.id}</option>)}</Select> : <p>{resources.profiles[0]?.display_name || resources.profiles[0]?.id || (hardwareLoading ? 'Loading cluster accelerator…' : 'No accelerator available')}</p>}<small>{draft.profileId ? 'Inherited from the selected cluster. YAML uses the matching llm-d Accelerator configuration.' : hardwareLoading ? 'Reading cluster hardware.' : resources.profiles.length > 1 ? 'This cluster has multiple accelerator types. Choose one for this deployment.' : 'Configure a supported Accelerator in Clusters before continuing.'}</small></div>
            <label className="inference-setup-wide">Downloaded model<Select aria-label="Downloaded model" value={draft.modelCacheEntryId || ''} onChange={event => selectModel(event.target.value)}><option value="">Select a ready model from Model Cache</option>{entries.map(entry => <option key={entry.id} value={entry.id}>{sourceRepository(entry)} · {entry.source.huggingface.revision} · {volumes.find(item => item.id === entry.storageVolumeId)?.name}</option>)}</Select><small>Only ready models synchronized to this cluster are available. Repository and storage are inherited.</small></label>
            {volume && <div className="inference-setup-wide inference-target-summary"><Badge tone="success">Cache ready</Badge><span>{volume.name} · {volume.kind}</span><span>Namespace: {draft.sourceNamespace}</span></div>}
            {draft.clusterId && !entries.length && <p className="inference-setup-wide">No ready model is available. Download a model in Model Cache; this list refreshes automatically.</p>}
            <div className="inference-setup-wide flex gap-2"><Button variant="secondary" size="sm" onClick={() => onNavigate?.('model-cache')}>Open Model Cache</Button><Button variant="ghost" size="sm" onClick={() => onNavigate?.('storage-management')}>Open Storage</Button></div>
            {error && <p className="inference-setup-wide" role="alert">{error}</p>}
        </div>
    </Modal>;
}
