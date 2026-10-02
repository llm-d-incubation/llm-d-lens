import { downloadClusterSoftware, listLensHosts } from './OptimizationWorkspace/clusterBackend';
import { requestJson } from '../api/httpClient';
// Multi-step "Create Cluster" wizard.
//
// See docs/design/cluster-creation-wizard-design.md for the full design.
// Step 1 creates the cluster immediately (so Step 2-4 have a real
// `cluster_id` to attach kubeconfig-backed operations to); Step 2/3 PATCH
// the cluster's proxy/version-pin settings; Step 4 (optional) prewarms a
// Model Cache entry by: creating a standalone HF_TOKEN secret, creating or
// selecting a Storage volume, then creating a Model Cache entry that
// references that secret via the existing `existing-secret` token source
// (no new token source mode -- see section 4.4 of the design doc).
import { useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle,
    ArrowRight,
    Check,
    CheckCircle2,
    Cpu,
    Database,
    Globe,
    Loader2,
    Rocket,
    Server,
    Tag,
    Upload,
    Waypoints,
} from 'lucide-react';
import { Badge, Button, Checkbox, Input, Label, Modal, Select, Textarea } from './ui';
import {
    createStorageVolume,
    getStorageVolume,
    listStorageNodes,
} from './StorageManagement/storageManagementBackend';
import { errorMessage } from './StorageManagement/storagePresentation';
import { createModelCacheEntry } from './ModelCache/modelCacheBackend';
import { getGpuDriverStatus, installGpuDriver, waitForGpuDriverReady } from './ClusterMonitoringStack/gpuDriverBackend';
import { getHardwareProfiles, acceleratorVendorsFromProfiles, accessModesForVendor } from './ClusterMonitoringStack/hardwareProfilesBackend';
import { BootstrapNodesPanel } from './ClusterBootstrap/BootstrapNodesPanel';

const STEPS = [
    { id: 'basics', label: 'Basic Info', icon: Server },
    { id: 'proxy', label: 'Network Proxy', icon: Globe },
    { id: 'gateway', label: 'Model gateway', icon: Waypoints },
    { id: 'versions', label: 'llm-d versions', icon: Tag },
    { id: 'accelerator', label: 'Accelerator', icon: Cpu },
    { id: 'modelCache', label: 'Model Cache', icon: Database },
    { id: 'confirm', label: 'Confirm', icon: Rocket },
];

const ACCESS_MODE_LABEL = { dra: 'DRA (Dynamic Resource Allocation)', plugin: 'Device plugin' };

const ACCELERATOR_VENDORS = [
    { id: 'intel', label: 'Intel GPU', disabled: false, accessModes: ['dra', 'plugin'] },
    { id: 'nvidia', label: 'Nvidia GPU', disabled: true, accessModes: ['dra', 'plugin'] },
];

const CAPACITY_PATTERN = /^\d+(Gi|Mi|Ti)$/;
const REPO_ID_PATTERN = /^[\w.-]+\/[\w.-]+$/;

function shortId() {
    return Math.random().toString(16).slice(2, 10);
}

function request(path, options = {}) {
    return requestJson(path, { headers: { 'Content-Type': 'application/json' }, ...options });
}

// Polls a just-created Storage volume until it leaves the "pending"
// provisioning state (or a timeout elapses), since Model Cache downloads
// require a READY volume (see model_cache/service.py::get_ready_volume).
async function waitForVolumeReady(volumeId, { timeoutMs = 120000, intervalMs = 2000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        const volume = await getStorageVolume(volumeId);
        if (volume.status === 'ready') return volume;
        if (volume.status === 'failed') {
            throw new Error(volume.failureDetail || 'Storage volume provisioning failed');
        }
        if (Date.now() > deadline) {
            throw new Error('Timed out waiting for the storage volume to become ready');
        }
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
}

// Polls the llm-d versions step's real llm-d / llm-d-benchmark downloads
// (see llm_d_bench.cluster.repo_downloads on the backend) until both refs
// that were requested reach a terminal state (ready/failed), reporting
// intermediate progress via `onUpdate` so the UI can show live status.


export function CreateClusterWizard({ onClose, onCreated }) {
    // The application database is configured once at install time (see
    // scripts/LensInstaller-Ubuntu-x86_64.sh and
    // llm_d_bench/db/bootstrap_cli.py), not here -- this is just a sanity
    // check that surfaces a clear error if that step was ever skipped.
    const [dbStatus, setDbStatus] = useState(null); // null while loading, else DatabaseStatus
    const [dbStatusError, setDbStatusError] = useState('');
    useEffect(() => {
        let cancelled = false;
        request('/api/v1/system/database')
            .then((status) => {
                if (!cancelled) setDbStatus(status);
            })
            .catch((err) => {
                if (!cancelled) setDbStatusError(errorMessage(err));
            });
        return () => {
            cancelled = true;
        };
    }, []);

    const [stepIndex, setStepIndex] = useState(0);
    const [cluster, setCluster] = useState(null); // created after Step 1
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    // Step 1: basic info
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [file, setFile] = useState(null);
    // "I have kubeconfig" (upload) vs "Bootstrap via Kubespray" -- see
    // docs/design/CLUSTER_BOOTSTRAP_DESIGN.md. Bootstrapping only ever produces a
    // kubeconfig string that gets wrapped into a synthetic File below, so
    // every other Step 1 field/validation/submit path stays untouched.
    const [kubeconfigSource, setKubeconfigSource] = useState('upload'); // 'upload' | 'bootstrap'

    // Step 2: proxy
    const [proxyMode, setProxyMode] = useState('auto');
    const [httpProxy, setHttpProxy] = useState('');
    const [httpsProxy, setHttpsProxy] = useState('');
    const [noProxy, setNoProxy] = useState('');

    // Step 4: versions
    const [llmDRef, setLlmDRef] = useState('main');
    const [llmDBenchmarkRef, setLlmDBenchmarkRef] = useState('main');
    // Shared llm-d Gateway provider installed once at cluster creation (the
    // model service then publishes HTTPRoute/IPP against it).
    const [gatewayProvider, setGatewayProvider] = useState('istio');
    const [gatewayPort, setGatewayPort] = useState('');
    // Optional externally reachable Gateway host/IP (or full URL); empty lets
    // Lens derive one from the cluster.
    const [gatewayPublicUrl, setGatewayPublicUrl] = useState('');
    const [gatewayAuthzHost, setGatewayAuthzHost] = useState('');
    // Per-uid inotify instance limit Lens applies to every node; kind's 128
    // default is exhausted by a busy node ("too many open files").
    const [inotifyMaxUserInstances, setInotifyMaxUserInstances] = useState('8192');
    const [lensHosts, setLensHosts] = useState([]);
    const [ippVersion, setIppVersion] = useState('v0.1.0');
    // Real download progress for the refs above (see repo_downloads on the
    // backend) -- populated while goNext() from this step is in flight.
    const [versionsDownloading, setVersionsDownloading] = useState(false);
    const [versionsDownloadStatus, setVersionsDownloadStatus] = useState(null); // { llmD, llmDBenchmark }

    // Step 4: accelerator (optional). Installs the Intel GPU DRA driver or
    // device plugin itself -- detection scans every namespace, so there is
    // no namespace for the user to pick.
    const [acceleratorVendors, setAcceleratorVendors] = useState(ACCELERATOR_VENDORS);
    const [acceleratorVendor, setAcceleratorVendor] = useState('intel');
    const [acceleratorEnabled, setAcceleratorEnabled] = useState(false);
    const [acceleratorAccessMode, setAcceleratorAccessMode] = useState('dra');
    const [acceleratorStatus, setAcceleratorStatus] = useState(null);
    const [acceleratorStatusLoading, setAcceleratorStatusLoading] = useState(false);
    const [acceleratorStatusError, setAcceleratorStatusError] = useState(null);

    // Accelerator vendor tabs come from the registered hardware profiles; the
    // static list is the fallback when the registry is unavailable.
    useEffect(() => {
        const controller = new AbortController();
        getHardwareProfiles({ signal: controller.signal })
            .then((payload) => {
                if (controller.signal.aborted) return;
                const vendors = acceleratorVendorsFromProfiles(payload?.profiles, ACCELERATOR_VENDORS);
                setAcceleratorVendors(vendors);
                setAcceleratorVendor((current) => (
                    vendors.some((vendor) => vendor.id === current && !vendor.disabled)
                        ? current
                        : (vendors.find((vendor) => !vendor.disabled)?.id || current)
                ));
            })
            .catch(() => {});
        return () => controller.abort();
    }, []);

    // Hardware profile id for the selected vendor, passed to the gpu-driver API so
    // a multi-vendor cluster installs the right driver (undefined → backend default).
    const selectedAcceleratorProfileId = acceleratorVendors.find((vendor) => vendor.id === acceleratorVendor)?.profileId;
    const selectedAcceleratorLabel = acceleratorVendors.find((vendor) => vendor.id === acceleratorVendor)?.label || 'accelerator';

    // Keep the access mode valid for the selected vendor (profiles declare their
    // supported modes; fall back to dra/plugin).
    useEffect(() => {
        const modes = accessModesForVendor(acceleratorVendors, acceleratorVendor);
        setAcceleratorAccessMode((current) => (modes.includes(current) ? current : modes[0]));
    }, [acceleratorVendors, acceleratorVendor]);

    // Step 5: model cache (optional)
    const [modelCacheEnabled, setModelCacheEnabled] = useState(false);
    const [prewarmEnabled, setPrewarmEnabled] = useState(false);
    const [secretNamespace, setSecretNamespace] = useState('llm-d-bench-storage');
    const [secretNamespaceTouched, setSecretNamespaceTouched] = useState(false);
    const [secretName, setSecretName] = useState(() => `hf-token-${shortId()}`);
    const [hfToken, setHfToken] = useState('');
    // Set once the Step-5 HF_TOKEN secret has actually been created on the
    // cluster (see goNext's stepIndex === 4 branch below); recorded on the
    // cluster itself so later Deploy/Evaluate flows can reuse it without the
    // user re-entering a token for every deployment.
    const [secretRef, setSecretRef] = useState(null);
    const [repoId, setRepoId] = useState('');
    const [revision, setRevision] = useState('main');
    const [storageKind, setStorageKind] = useState('local-disk');
    const [storageName, setStorageName] = useState('');
    const [storageCapacity, setStorageCapacity] = useState('');
    const [hostPath, setHostPath] = useState('');
    const [nfsServer, setNfsServer] = useState('');
    const [nfsPath, setNfsPath] = useState('');
    const [storageNodes, setStorageNodes] = useState([]);

    // Step 5: finishing progress
    const [finishing, setFinishing] = useState(false);
    const [finishStatus, setFinishStatus] = useState([]); // [{label, state: 'pending'|'running'|'done'|'failed', detail}]
    const [finished, setFinished] = useState(false);

    const firstFieldRef = useRef(null);
    useEffect(() => {
        firstFieldRef.current?.focus();
    }, [stepIndex]);

    // Draft cleanup on hard navigation away (tab close/refresh/browser back).
    // handleClose below covers the in-app cancel/close paths, but those never
    // run if the user leaves the page outright, so the Step-1 draft would
    // linger forever and permanently squat its name (see cluster_name_conflict
    // in llm_d_bench/cluster/registry.py). `keepalive: true` lets the DELETE
    // survive the page unloading, unlike a normal fetch.
    const draftCleanupRef = useRef({ cluster: null, finished: false });
    useEffect(() => {
        draftCleanupRef.current = { cluster, finished };
    }, [cluster, finished]);
    useEffect(() => {
        const cleanupDraft = () => {
            const { cluster: currentCluster, finished: currentFinished } = draftCleanupRef.current;
            if (currentCluster && !currentFinished) {
                requestJson(`/api/cluster/clusters/${encodeURIComponent(currentCluster.id)}`, { method: 'DELETE', keepalive: true }).catch(() => {});
            }
        };
        window.addEventListener('pagehide', cleanupDraft);
        return () => window.removeEventListener('pagehide', cleanupDraft);
    }, []);
    useEffect(() => {
        let alive = true;
        listLensHosts().then((items) => {
            if (!alive) return;
            setLensHosts(items);
            setGatewayAuthzHost((current) => current || items[0] || '');
        }).catch(() => {});
        return () => { alive = false; };
    }, []);

    // Default HF_TOKEN secret namespace is per-cluster to avoid collisions
    // across clusters sharing the same underlying k8s API server; keeps in
    // sync with the cluster id unless the user has typed their own value.
    useEffect(() => {
        if (secretNamespaceTouched || !cluster?.id) return;
        setSecretNamespace(`llm-d-bench-storage-${cluster.id}`);
    }, [cluster?.id, secretNamespaceTouched]);

    useEffect(() => {
        if (stepIndex !== 4 || !modelCacheEnabled || !cluster || storageKind !== 'local-disk') return undefined;
        const controller = new AbortController();
        (async () => {
            try {
                const items = await listStorageNodes({ clusterId: cluster.id, signal: controller.signal });
                setStorageNodes(items.filter((node) => node.schedulable !== false));
            } catch {
                // Non-fatal: fields fall back to free text entry.
            }
        })();
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [stepIndex, modelCacheEnabled, storageKind, cluster]);

    // Step 4: probe whether the selected access mode's driver/plugin is
    // already installed and ready on the target cluster (so we can offer
    // "reuse" instead of forcing an install). Re-checks whenever the access
    // mode changes.
    useEffect(() => {
        if (stepIndex !== 3 || !acceleratorEnabled || !cluster) return undefined;
        const controller = new AbortController();
        setAcceleratorStatusLoading(true);
        setAcceleratorStatusError(null);
        const timer = window.setTimeout(() => {
            (async () => {
                try {
                    const snapshot = await getGpuDriverStatus(acceleratorAccessMode, {
                        clusterId: cluster.id,
                        hardware: selectedAcceleratorProfileId,
                        signal: controller.signal,
                    });
                    setAcceleratorStatus(snapshot);
                } catch (statusError) {
                    if (statusError.name !== 'AbortError') setAcceleratorStatusError(statusError);
                } finally {
                    if (!controller.signal.aborted) setAcceleratorStatusLoading(false);
                }
            })();
        }, 400);
        return () => {
            window.clearTimeout(timer);
            controller.abort();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [stepIndex, acceleratorEnabled, acceleratorAccessMode, selectedAcceleratorProfileId, cluster]);

    const basicsValid = name.trim().length > 0 && !!file;
    const proxyValid = proxyMode === 'auto' || Boolean(httpProxy.trim() || httpsProxy.trim() || noProxy.trim());
    const modelCacheStorageSpecValid = useMemo(() => {
        if (storageKind === 'local-disk') return hostPath.trim().startsWith('/');
        if (storageKind === 'nfs') return Boolean(nfsServer.trim() && nfsPath.trim().startsWith('/'));
        return false;
    }, [storageKind, hostPath, nfsServer, nfsPath]);
    // The HF_TOKEN secret is always required (independent of Model Cache) --
    // Deploy/Evaluate default to reusing it (see llm_d_bench/deploy/service.py's
    // ``_default_model_secret``) so any deployment on this cluster can pull
    // gated models without the user re-entering a token every time.
    const hfTokenSecretValid = Boolean(secretNamespace.trim() && secretName.trim() && hfToken.trim());
    const modelCacheValid =
        !modelCacheEnabled ||
        (modelCacheStorageSpecValid &&
            CAPACITY_PATTERN.test(storageCapacity.trim()) &&
            (!prewarmEnabled ||
                (REPO_ID_PATTERN.test(repoId.trim()) &&
                    revision.trim())));

    const goNext = async () => {
        setError('');
        if (stepIndex === 0) {
            if (!basicsValid || busy) return;
            setBusy(true);
            try {
                const body = new FormData();
                body.append('name', name.trim());
                body.append('description', description.trim());
                body.append('file', file);
                // Created as a draft: hidden from the main cluster list and
                // excluded from the "has deployments/storage" delete guards
                // until Finish flips it to a real cluster below. If the
                // wizard is cancelled first, onClose deletes this draft.
                body.append('draft', 'true');
                const payload = await requestJson('/api/cluster/clusters', { method: 'POST', body });
                setCluster(payload.cluster);
                setStepIndex(1);
            } catch (nextError) {
                setError(nextError.message);
            } finally {
                setBusy(false);
            }
            return;
        }
        if (stepIndex === 1) {
            if (!proxyValid || busy) return;
            setBusy(true);
            try {
                const payload = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}`, {
                    method: 'PATCH',
                    body: JSON.stringify({
                        proxy:
                            proxyMode === 'custom'
                                ? { mode: 'custom', httpProxy: httpProxy.trim() || null, httpsProxy: httpsProxy.trim() || null, noProxy: noProxy.trim() || null }
                                : { mode: 'auto' },
                    }),
                });
                setCluster(payload.cluster);
                setStepIndex(2);
            } catch (nextError) {
                setError(nextError.message);
            } finally {
                setBusy(false);
            }
            return;
        }
        if (stepIndex === 2) {
            if (busy) return;
            setBusy(true);
            try {
                const payload = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}`, {
                    method: 'PATCH',
                    body: JSON.stringify({ gatewayProvider: gatewayProvider || null, gatewayPort: gatewayPort.trim() ? Number(gatewayPort.trim()) : null, gatewayPublicUrl: gatewayPublicUrl.trim() || null, gatewayAuthzHost: gatewayAuthzHost.trim() || null, inotifyMaxUserInstances: inotifyMaxUserInstances.trim() ? Number(inotifyMaxUserInstances.trim()) : null, ippVersion: ippVersion.trim() || null }),
                });
                setCluster(payload.cluster);
                setStepIndex(3);
            } catch (nextError) {
                setError(nextError.message);
            } finally {
                setBusy(false);
            }
            return;
        }
        if (stepIndex === 3) {
            if (busy) return;
            setBusy(true);
            try {
                const trimmedLlmDRef = llmDRef.trim();
                const trimmedBenchmarkRef = llmDBenchmarkRef.trim();
                const payload = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}`, {
                    method: 'PATCH',
                    body: JSON.stringify({
                        llmDRef: trimmedLlmDRef || null,
                        llmDBenchmarkRef: trimmedBenchmarkRef || null,
                    }),
                });
                let nextCluster = payload.cluster;
                setCluster(nextCluster);
                if (trimmedLlmDRef || trimmedBenchmarkRef) {
                    setVersionsDownloading(true);
                    setVersionsDownloadStatus(null);
                    nextCluster = await downloadClusterSoftware(nextCluster, {
                        llmDRef: trimmedLlmDRef || null,
                        llmDBenchmarkRef: trimmedBenchmarkRef || null,
                    }, { onUpdate: setVersionsDownloadStatus });
                    setCluster(nextCluster);
                }
                setStepIndex(4);
            } catch (nextError) {
                setError(nextError.message);
            } finally {
                setVersionsDownloading(false);
                setBusy(false);
            }
            return;
        }
        if (stepIndex === 4) {
            setStepIndex(5);
            return;
        }
        if (stepIndex === 5) {
            if (!hfTokenSecretValid || !modelCacheValid || busy) return;
            setBusy(true);
            try {
                // Create the HF_TOKEN secret now (before advancing), not
                // deferred to Finish, and record it on the cluster so
                // Deploy/Evaluate can default to reusing it later -- see
                // llm_d_bench/cluster/service.py's create_hf_token_secret.
                if (!secretRef) {
                    const created = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}/hf-token-secrets`, {
                        method: 'POST',
                        body: JSON.stringify({ namespace: secretNamespace.trim(), name: secretName.trim(), token: hfToken.trim() }),
                    });
                    setSecretRef(created);
                }
                setStepIndex(6);
            } catch (nextError) {
                setError(errorMessage(nextError, 'Failed to create the HF_TOKEN secret'));
            } finally {
                setBusy(false);
            }
            return;
        }
    };

    const goBack = () => {
        setError('');
        if (stepIndex > 0) setStepIndex(stepIndex - 1);
    };

    const acceleratorNeedsInstall = acceleratorEnabled && !acceleratorStatus?.ready;

    // Flips the draft cluster created in Step 1 into a real, listed cluster.
    // Only called once every remaining wizard step has succeeded.
    const finalizeDraftCluster = async () => {
        const payload = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}`, {
            method: 'PATCH',
            body: JSON.stringify({ draft: false }),
        });
        setCluster(payload.cluster);
        return payload.cluster;
    };

    const runFinish = async () => {
        if (finishing || finished) return;
        setFinishing(true);
        setError('');
        if (!modelCacheEnabled && !acceleratorNeedsInstall) {
            const finalCluster = await finalizeDraftCluster();
            setFinishStatus([]);
            setFinished(true);
            setFinishing(false);
            onCreated?.(finalCluster);
            return;
        }
        const steps = [
            ...(acceleratorNeedsInstall ? [{ key: 'accelerator', label: 'Install accelerator component', state: 'pending' }] : []),
            ...(modelCacheEnabled
                ? [
                    { key: 'storage', label: 'Create storage volume', state: 'pending' },
                    ...(prewarmEnabled ? [{ key: 'model-cache', label: 'Start model cache download', state: 'pending' }] : []),
                ]
                : []),
        ];
        setFinishStatus(steps);
        const setStepState = (key, state, detail) =>
            setFinishStatus((current) => current.map((item) => (item.key === key ? { ...item, state, detail } : item)));
        try {
            if (acceleratorNeedsInstall) {
                if (!selectedAcceleratorProfileId) {
                    throw new Error('Accelerator hardware profiles are still loading; go back to the Accelerator step and reselect your GPU vendor.');
                }
                setStepState('accelerator', 'running');
                await installGpuDriver(acceleratorAccessMode, { clusterId: cluster.id, hardware: selectedAcceleratorProfileId });
                await waitForGpuDriverReady(acceleratorAccessMode, { clusterId: cluster.id, hardware: selectedAcceleratorProfileId });
                setStepState('accelerator', 'done');
            }

            if (!modelCacheEnabled) {
                const finalCluster = await finalizeDraftCluster();
                setFinished(true);
                setFinishing(false);
                onCreated?.(finalCluster);
                return;
            }

            let volumeId;
            {
                setStepState('storage', 'running');
                const volumePayload = {
                    clusterId: cluster.id,
                    name: storageName.trim(),
                    kind: storageKind,
                    capacity: storageCapacity.trim(),
                    readOnly: false,
                    purposes: ['model-cache'],
                    ...(storageKind === 'local-disk' ? { localDisk: { hostPath: hostPath.trim() } } : {}),
                    ...(storageKind === 'nfs' ? { nfs: { server: nfsServer.trim(), path: nfsPath.trim() } } : {}),
                };
                const created = await createStorageVolume(volumePayload);
                volumeId = created.id;
                const ready = await waitForVolumeReady(volumeId);
                volumeId = ready.id;
                setStepState('storage', 'done');
            }

            if (prewarmEnabled) {
                setStepState('model-cache', 'running');
                await createModelCacheEntry({
                    clusterId: cluster.id,
                    storageVolumeId: volumeId,
                    source: { kind: 'huggingface', huggingface: { repoId: repoId.trim(), revision: revision.trim() } },
                    tokenSource: { mode: 'existing-secret', namespace: secretRef.namespace, name: secretRef.name },
                });
                setStepState('model-cache', 'done');
            }
            setFinished(true);
            onCreated?.(await finalizeDraftCluster());
        } catch (nextError) {
            const failingKey = finishStatus.find((item) => item.state === 'running')?.key;
            if (failingKey) setStepState(failingKey, 'failed', errorMessage(nextError));
            setError(errorMessage(nextError, 'Failed to finish cluster setup'));
        } finally {
            setFinishing(false);
        }
    };

    const canGoNext =
        (stepIndex === 0 && basicsValid) ||
        (stepIndex === 1 && proxyValid) ||
        (stepIndex === 2 && (gatewayProvider === 'gke' || Boolean(gatewayAuthzHost))) ||
        stepIndex === 3 ||
        stepIndex === 4 ||
        (stepIndex === 5 && hfTokenSecretValid && modelCacheValid);

    // Best-effort cleanup: the cluster created at Step 1 is a draft (hidden
    // from the cluster list) until Finish flips it to real. If the wizard
    // is cancelled/closed before that, delete the leftover draft instead of
    // letting it linger.
    const handleClose = () => {
        if (cluster && !finished) {
            request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}`, { method: 'DELETE' }).catch(() => {});
        }
        onClose?.();
    };

    if (dbStatus === null) {
        return (
            <Modal isOpen onClose={onClose} title="Add a Kubernetes cluster" size="lg">
                <div className="flex flex-col items-center gap-3 py-10 text-sm text-theme-muted">
                    {dbStatusError ? (
                        <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {dbStatusError}
                        </div>
                    ) : (
                        <>
                            <Loader2 className="h-6 w-6 animate-spin text-sky-400" />
                            Checking database configuration…
                        </>
                    )}
                </div>
            </Modal>
        );
    }

    if (!dbStatus.configured) {
        // The database is now configured once at install time (see
        // scripts/LensInstaller-Ubuntu-x86_64.sh and
        // llm_d_bench/db/bootstrap_cli.py) rather than lazily from this
        // wizard, so reaching this should only ever happen if the installer
        // was skipped or the bootstrap file was manually removed.
        return (
            <Modal isOpen onClose={onClose} title="Add a Kubernetes cluster" size="lg">
                <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                    Lens's database is not configured yet. Re-run the installer
                    (<code>LensInstaller-Ubuntu-x86_64.sh</code>) to set it up, then reopen this wizard.
                </div>
            </Modal>
        );
    }

    return (
        <Modal
            isOpen
            onClose={finishing ? undefined : handleClose}
            title="Add a Kubernetes cluster"
            subtitle={`Step ${stepIndex + 1} of ${STEPS.length} · ${STEPS[stepIndex].label}`}
            size="xl"
            variant="drawer"
            closeOnEscape={!finishing}
            footer={
                <>
                    <Button variant="outline" onClick={handleClose} disabled={finishing}>Cancel</Button>
                    {stepIndex > 0 && stepIndex < 6 && (
                        <Button variant="outline" onClick={goBack} disabled={busy}>Back</Button>
                    )}
                    {stepIndex === 6 && !finished && (
                        <Button variant="outline" onClick={() => setStepIndex(5)} disabled={finishing}>Back</Button>
                    )}
                    {stepIndex < 6 && (
                        <Button variant="sky" onClick={goNext} isLoading={busy} disabled={!canGoNext}>
                            {stepIndex === 5 ? 'Review' : 'Next'} <ArrowRight className="h-3.5 w-3.5" />
                        </Button>
                    )}
                    {stepIndex === 6 && !finished && (
                        <Button variant="sky" onClick={runFinish} isLoading={finishing}>
                            <Rocket className="h-3.5 w-3.5" /> Finish
                        </Button>
                    )}
                    {stepIndex === 6 && finished && (
                        <Button variant="sky" onClick={onClose}>
                            <Check className="h-3.5 w-3.5" /> Done
                        </Button>
                    )}
                </>
            }
        >
            <div className="mx-auto max-w-4xl space-y-6">
                <ol className="grid gap-2" style={{ gridTemplateColumns: `repeat(${STEPS.length}, minmax(0, 1fr))` }} aria-label="Cluster creation progress">
                    {STEPS.map((step, index) => (
                        <li key={step.id} className="min-w-0">
                            <div className={`h-1.5 rounded-full transition-colors ${index <= stepIndex ? 'bg-sky-400' : 'bg-slate-800'}`} />
                            <div className={`mt-2 truncate text-[10px] font-bold uppercase tracking-wider ${index === stepIndex ? 'text-sky-300' : 'text-slate-500'}`}>
                                {index + 1}. {step.label}
                            </div>
                        </li>
                    ))}
                </ol>

                {error && (
                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
                    </div>
                )}

            {stepIndex === 0 && (
                <section aria-labelledby="wizard-basics-heading">
                    <h2 id="wizard-basics-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                        <Server className="h-5 w-5 text-sky-400" /> Basic Info
                    </h2>
                <div className="space-y-4">
                    <div>
                        <Label htmlFor="wizard-cluster-name">Cluster name</Label>
                        <Input id="wizard-cluster-name" ref={firstFieldRef} value={name} onChange={(event) => setName(event.target.value)} autoFocus placeholder="my-cluster" />
                    </div>
                    <div>
                        <Label htmlFor="wizard-cluster-description">Description</Label>
                        <Textarea id="wizard-cluster-description" value={description} onChange={(event) => setDescription(event.target.value)} rows={3} placeholder="Optional description" />
                    </div>
                    <div>
                        <Label>Kubeconfig file</Label>
                        <div className="mb-2 flex gap-2 text-xs">
                            <label className="flex items-center gap-1.5">
                                <input type="radio" name="kubeconfig-source" checked={kubeconfigSource === 'upload'} onChange={() => setKubeconfigSource('upload')} />
                                I already have a kubeconfig
                            </label>
                            <label className="flex items-center gap-1.5">
                                <input type="radio" name="kubeconfig-source" checked={kubeconfigSource === 'bootstrap'} onChange={() => setKubeconfigSource('bootstrap')} />
                                Bootstrap a new cluster via Kubespray
                                <Badge tone="warning" size="xs">Experimental</Badge>
                            </label>
                        </div>
                        {kubeconfigSource === 'bootstrap' && !file && (
                            <BootstrapNodesPanel
                                onKubeconfigReady={(kubeconfigText) => {
                                    setFile(new File([kubeconfigText], 'bootstrapped-kubeconfig.yaml', { type: 'text/plain' }));
                                    setKubeconfigSource('upload');
                                }}
                                onCancel={() => setKubeconfigSource('upload')}
                            />
                        )}
                        {(kubeconfigSource === 'upload' || file) && (
                            <div className="flex items-center gap-2 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2">
                                <Upload size={15} className="shrink-0 text-slate-400 dark:text-slate-500" />
                                <span className="flex-1 truncate text-sm text-theme-muted">{file ? file.name : 'Choose a kubeconfig file'}</span>
                                <input type="file" onChange={(event) => setFile(event.target.files?.[0] || null)} className="hidden" id="wizard-kubeconfig-file" />
                                <label htmlFor="wizard-kubeconfig-file" className="cursor-pointer rounded-md border border-emerald-500/40 px-2 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/10">Browse</label>
                            </div>
                        )}
                    </div>
                </div>
                </section>
            )}

            {stepIndex === 1 && (
                <section aria-labelledby="wizard-proxy-heading">
                    <h2 id="wizard-proxy-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                        <Globe className="h-5 w-5 text-sky-400" /> Network Proxy
                    </h2>
                <div className="space-y-4">
                    <div className="space-y-2">
                        <label className="flex items-start gap-2 rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5">
                            <input type="radio" className="mt-1" name="proxy-mode" checked={proxyMode === 'auto'} onChange={() => setProxyMode('auto')} />
                            <span>
                                <span className="block text-sm font-medium text-theme-text">Auto-detect this cluster's proxy (recommended)</span>
                                <span className="block text-xs text-theme-muted mt-0.5">
                                    Automatic detection from the target cluster is not implemented yet; until then this
                                    falls back to the Lens backend's own proxy environment variables (pre-wizard
                                    behavior). Use "Custom" below if this cluster needs a different proxy.
                                </span>
                            </span>
                        </label>
                        <label className="flex items-start gap-2 rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5">
                            <input type="radio" className="mt-1" name="proxy-mode" checked={proxyMode === 'custom'} onChange={() => setProxyMode('custom')} />
                            <span className="block text-sm font-medium text-theme-text">Custom proxy for this cluster</span>
                        </label>
                    </div>
                    {proxyMode === 'custom' && (
                        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                            <div>
                                <Label htmlFor="wizard-http-proxy">HTTP_PROXY</Label>
                                <Input id="wizard-http-proxy" value={httpProxy} onChange={(event) => setHttpProxy(event.target.value)} placeholder="http://proxy:3128" />
                            </div>
                            <div>
                                <Label htmlFor="wizard-https-proxy">HTTPS_PROXY</Label>
                                <Input id="wizard-https-proxy" value={httpsProxy} onChange={(event) => setHttpsProxy(event.target.value)} placeholder="http://proxy:3128" />
                            </div>
                            <div>
                                <Label htmlFor="wizard-no-proxy">NO_PROXY</Label>
                                <Input id="wizard-no-proxy" value={noProxy} onChange={(event) => setNoProxy(event.target.value)} placeholder="localhost,.svc" />
                            </div>
                        </div>
                    )}
                </div>
                </section>
            )}

            {stepIndex === 2 && (
                <section aria-labelledby="wizard-gateway-heading">
                    <h2 id="wizard-gateway-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                        <Waypoints className="h-5 w-5 text-sky-400" /> Model gateway
                    </h2>
                    <div className="space-y-4">
                        <div>
                            <Label htmlFor="wizard-gateway-provider">Provider</Label>
                            <Select id="wizard-gateway-provider" value={gatewayProvider}
                                onChange={(event) => setGatewayProvider(event.target.value)}>
                                <option value="istio">Istio</option>
                                <option value="envoy-ai-gateway" disabled>Envoy AI Gateway (not supported yet)</option>
                                <option value="agentgateway" disabled>agentgateway (not supported yet)</option>
                                <option value="gke" disabled>GKE Gateway (not supported yet)</option>
                            </Select>
                            <p className="mt-1 text-[11px] text-theme-muted">
                                Installed once for the whole cluster at creation. Deployments keep their own
                                InferencePool + EPP (no per-deployment gateway); each model service publishes an
                                HTTPRoute/IPP against this shared Gateway.
                            </p>
                        </div>
                        <div>
                            <Label htmlFor="wizard-gateway-port">Gateway exposed port (NodePort, optional)</Label>
                            <Input id="wizard-gateway-port" type="number" min="30000" max="32767" value={gatewayPort}
                                onChange={(event) => setGatewayPort(event.target.value)} placeholder="Optional, e.g. 30080" />
                            <p className="mt-1 text-[11px] text-theme-muted">
                                NodePort the shared Gateway is exposed on. Leave empty and the cluster assigns a
                                random one — the actual port is shown in the Gateway status. Clients reach it at
                                http://&lt;node-ip&gt;:&lt;port&gt;/v1; bind your domain to that IP:PORT outside Lens.
                            </p>
                        </div>
                        <div>
                            <Label htmlFor="wizard-gateway-public-url">Gateway host/IP (optional)</Label>
                            <Input id="wizard-gateway-public-url" value={gatewayPublicUrl}
                                onChange={(event) => setGatewayPublicUrl(event.target.value)}
                                placeholder="e.g. 10.112.229.74" />
                            <p className="mt-1 text-[11px] text-theme-muted">
                                Externally reachable host/IP clients call to reach the Gateway (Lens combines it with the
                                exposed port). Leave empty and Lens derives one from the exposed port and the cluster node.
                            </p>
                        </div>
                        <div>
                            <Label htmlFor="wizard-gateway-authz-host">Lens address</Label>
                            <Input id="wizard-gateway-authz-host" list="wizard-lens-hosts" value={gatewayAuthzHost}
                                onChange={(event) => setGatewayAuthzHost(event.target.value)}
                                placeholder="Pick one or type a host/IP" disabled={gatewayProvider === 'gke'} />
                            <datalist id="wizard-lens-hosts">
                                {lensHosts.map((host) => <option key={host} value={host} />)}
                            </datalist>
                            <p className="mt-1 text-[11px] text-theme-muted">
                                {gatewayProvider === 'gke'
                                    ? 'GKE Gateway has no Lens address hook, so callers are not checked.'
                                    : 'The host or IP this cluster uses to reach Lens (Lens adds its own port). Auto-filled; leave empty and Lens fills it in.'}
                            </p>
                        </div>
                        <div>
                            <Label htmlFor="wizard-ipp-version">IPP version (payload-processor chart)</Label>
                            <Input id="wizard-ipp-version" value={ippVersion}
                                onChange={(event) => setIppVersion(event.target.value)} placeholder="v0.1.0" />
                            <p className="mt-1 text-[11px] text-theme-muted">
                                Version used when installing the Inference Payload Processor for this cluster
                                (default v0.1.0; the chart itself is LENS_IPP_CHART).
                            </p>
                        </div>
                        <div>
                            <Label htmlFor="wizard-inotify-limit">Node inotify limit (fs.inotify.max_user_instances)</Label>
                            <Input id="wizard-inotify-limit" type="number" min="1" value={inotifyMaxUserInstances}
                                onChange={(event) => setInotifyMaxUserInstances(event.target.value)} placeholder="8192" />
                            <p className="mt-1 text-[11px] text-theme-muted">
                                Applied to every node so a busy node does not exhaust inotify (kind defaults to 128,
                                which causes "too many open files"). 8192 is recommended.
                            </p>
                        </div>
                    </div>
                </section>
            )}

            {stepIndex === 3 && (
                <section aria-labelledby="wizard-versions-heading">
                    <h2 id="wizard-versions-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                        <Tag className="h-5 w-5 text-sky-400" /> llm-d versions
                    </h2>
                <div className="space-y-4">
                    <div>
                        <Label htmlFor="wizard-llmd-ref">llm-d version (tag or commit SHA)</Label>
                        <Input id="wizard-llmd-ref" value={llmDRef} onChange={(event) => setLlmDRef(event.target.value)} placeholder="main, v0.2.0, or a commit SHA" disabled={versionsDownloading} />
                        {cluster?.llmDRepoPath && cluster.llmDRef === llmDRef.trim() && (
                            <p className="mt-1 text-[11px] text-emerald-400">Downloaded to {cluster.llmDRepoPath}</p>
                        )}
                    </div>
                    <div>
                        <Label htmlFor="wizard-llmd-benchmark-ref">llm-d-benchmark version (tag or commit SHA)</Label>
                        <Input id="wizard-llmd-benchmark-ref" value={llmDBenchmarkRef} onChange={(event) => setLlmDBenchmarkRef(event.target.value)} placeholder="main, v0.2.0, or a commit SHA" disabled={versionsDownloading} />
                        {cluster?.llmDBenchmarkRepoPath && cluster.llmDBenchmarkRef === llmDBenchmarkRef.trim() && (
                            <p className="mt-1 text-[11px] text-emerald-400">Downloaded to {cluster.llmDBenchmarkRepoPath}</p>
                        )}
                    </div>
                    <p className="text-xs text-theme-muted">
                        Both are optional; leave blank to skip downloading that module. Clicking Next actually clones the
                        selected ref into ~/.llm-d-lens/repos on the backend host -- evaluation, monitoring, and
                        other modules reuse that cached checkout automatically afterwards.
                    </p>
                    {versionsDownloading && (
                        <div className="space-y-1.5 rounded-lg border border-slate-700/70 bg-slate-800/40 px-4 py-3 text-xs">
                            {llmDRef.trim() && (
                                <p className="flex items-center gap-2 text-theme-muted">
                                    {versionsDownloadStatus?.llmD?.state === 'ready' ? (
                                        <Check size={13} className="text-emerald-400" />
                                    ) : (
                                        <Loader2 size={13} className="animate-spin" />
                                    )}
                                    llm-d @ {llmDRef.trim()}: {versionsDownloadStatus?.llmD?.state || 'starting'}...
                                </p>
                            )}
                            {llmDBenchmarkRef.trim() && (
                                <p className="flex items-center gap-2 text-theme-muted">
                                    {versionsDownloadStatus?.llmDBenchmark?.state === 'ready' ? (
                                        <Check size={13} className="text-emerald-400" />
                                    ) : (
                                        <Loader2 size={13} className="animate-spin" />
                                    )}
                                    llm-d-benchmark @ {llmDBenchmarkRef.trim()}: {versionsDownloadStatus?.llmDBenchmark?.state || 'starting'}...
                                </p>
                            )}
                        </div>
                    )}
                </div>
                </section>
            )}

            {stepIndex === 4 && (
                <section aria-labelledby="wizard-accelerator-heading">
                    <h2 id="wizard-accelerator-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                        <Cpu className="h-5 w-5 text-sky-400" /> Accelerator
                    </h2>
                <div className="space-y-4">
                    <nav className="flex overflow-x-auto border-b border-slate-700/70" role="tablist" aria-label="Accelerator vendor">
                        {acceleratorVendors.map((vendor) => (
                            <button
                                key={vendor.id}
                                type="button"
                                role="tab"
                                aria-selected={acceleratorVendor === vendor.id}
                                disabled={vendor.disabled}
                                title={vendor.disabled ? 'Not supported yet' : undefined}
                                onClick={() => !vendor.disabled && setAcceleratorVendor(vendor.id)}
                                className={`relative flex shrink-0 items-center gap-1.5 px-4 py-2.5 text-sm font-semibold transition-colors ${
                                    vendor.disabled
                                        ? 'cursor-not-allowed text-slate-600'
                                        : acceleratorVendor === vendor.id
                                            ? 'text-sky-300'
                                            : 'text-slate-400 hover:text-slate-200'
                                }`}
                            >
                                {vendor.label}
                                {vendor.disabled && (
                                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-slate-500">
                                        Coming soon
                                    </span>
                                )}
                                {!vendor.disabled && acceleratorVendor === vendor.id && (
                                    <span className="absolute inset-x-1 bottom-0 h-0.5 rounded-full bg-sky-400" />
                                )}
                            </button>
                        ))}
                    </nav>
                    <Checkbox
                        id="wizard-accelerator-enabled"
                        checked={acceleratorEnabled}
                        onChange={(event) => setAcceleratorEnabled(event.target.checked)}
                        label={`Install the ${selectedAcceleratorLabel} DRA driver or device plugin on this cluster`}
                    />
                    {acceleratorEnabled && (
                        <div className="space-y-4 rounded-lg border border-slate-700/70 bg-slate-800/40 px-4 py-4">
                            <div>
                                <Label>GPU access mode</Label>
                                <div className="mt-1.5 flex gap-3">
                                    {accessModesForVendor(acceleratorVendors, acceleratorVendor).map((mode) => (
                                        <label key={mode} className="flex items-center gap-1.5 text-sm">
                                            <input
                                                type="radio"
                                                name="accelerator-access-mode"
                                                checked={acceleratorAccessMode === mode}
                                                onChange={() => setAcceleratorAccessMode(mode)}
                                            />
                                            {ACCESS_MODE_LABEL[mode]}
                                        </label>
                                    ))}
                                </div>
                            </div>

                            <p className="text-[11px] text-theme-muted">
                                Detection scans every namespace, so if the driver or plugin is already present anywhere on
                                the cluster it will be reused as-is -- no reinstall, no namespace to pick. Otherwise it is
                                installed from the vendor's official upstream manifests when you finish the wizard. Both
                                projects are beta-quality upstream; avoid this on production clusters.
                            </p>

                            {acceleratorStatusLoading && (
                                <p className="flex items-center gap-2 text-xs text-theme-muted">
                                    <Loader2 size={13} className="animate-spin" /> Checking whether it is already installed on the cluster...
                                </p>
                            )}
                            {acceleratorStatusError && (
                                <p className="text-xs text-rose-300">Could not check current status: {errorMessage(acceleratorStatusError)}. You can still proceed; installation will be attempted on Finish.</p>
                            )}
                            {!acceleratorStatusLoading && !acceleratorStatusError && acceleratorStatus && (
                                <>
                                    {!acceleratorStatus.cluster_reachable && (
                                        <p className="text-xs text-amber-400">
                                            Could not reach the cluster to check current status. Installation will still be attempted on Finish.
                                        </p>
                                    )}
                                    {acceleratorStatus.cluster_reachable && acceleratorStatus.ready && (
                                        <p className="flex items-center gap-2 text-xs text-emerald-400">
                                            <Check size={13} /> Already installed and ready. It will be reused as-is -- no reinstall needed.
                                        </p>
                                    )}
                                    {acceleratorStatus.cluster_reachable && acceleratorStatus.installed && !acceleratorStatus.ready && (
                                        <p className="text-[11px] text-amber-400">{acceleratorStatus.message}</p>
                                    )}
                                    {acceleratorStatus.cluster_reachable && !acceleratorStatus.installed && (
                                        <p className="text-[11px] text-theme-muted">
                                            Not currently installed -- it will be installed with this access mode when you finish the wizard.
                                        </p>
                                    )}
                                </>
                            )}
                        </div>
                    )}
                </div>
                </section>
            )}


            {stepIndex === 5 && (
                <section aria-labelledby="wizard-modelcache-heading">
                    <h2 id="wizard-modelcache-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                        <Database className="h-5 w-5 text-sky-400" /> Model Cache
                    </h2>
                <div className="space-y-4">
                    <div className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-4 py-4">
                        <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-400">HF_TOKEN secret</p>
                        <p className="mb-3 text-[11px] leading-relaxed text-slate-400">
                            Always required, independent of Model Cache below -- Deploy/Evaluate default to reusing
                            this secret on this cluster so gated models can be pulled without asking for a token again.
                            Created as soon as you click Next.
                        </p>
                        <div className="space-y-4">
                            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                                <div>
                                    <Label htmlFor="wizard-secret-namespace">HF_TOKEN secret namespace</Label>
                                    <Input id="wizard-secret-namespace" value={secretNamespace} onChange={(event) => { setSecretNamespaceTouched(true); setSecretNamespace(event.target.value); }} placeholder="llm-d-bench-storage-<cluster id>" disabled={Boolean(secretRef)} />
                                </div>
                                <div>
                                    <Label htmlFor="wizard-secret-name">Secret name</Label>
                                    <Input id="wizard-secret-name" value={secretName} onChange={(event) => setSecretName(event.target.value)} disabled={Boolean(secretRef)} />
                                </div>
                            </div>
                            <div>
                                <Label htmlFor="wizard-hf-token">HuggingFace token</Label>
                                <Input id="wizard-hf-token" type="password" value={hfToken} onChange={(event) => setHfToken(event.target.value)} placeholder="hf_..." disabled={Boolean(secretRef)} />
                            </div>
                            {secretRef && (
                                <p className="text-xs text-emerald-300">
                                    Created {secretRef.namespace}/{secretRef.name}
                                </p>
                            )}
                        </div>
                    </div>
                    <Checkbox
                        id="wizard-model-cache-enabled"
                        checked={modelCacheEnabled}
                        onChange={(event) => setModelCacheEnabled(event.target.checked)}
                        label="Create a storage volume for model-cache on this cluster"
                    />
                    {modelCacheEnabled && (
                        <div className="space-y-4 rounded-lg border border-slate-700/70 bg-slate-800/40 px-4 py-4">
                            <div className="space-y-3">
                                <Label>Storage volume</Label>
                                <p className="text-[11px] leading-relaxed text-slate-400">
                                    Same fields as the Storage page's manual "New storage volume" form -- there are
                                    no default values, fill in everything below yourself.
                                </p>
                                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                                    <div>
                                        <Label htmlFor="wizard-storage-kind">Type</Label>
                                        <Select id="wizard-storage-kind" value={storageKind} onChange={(event) => setStorageKind(event.target.value)}>
                                            <option value="local-disk">hostPath</option>
                                            <option value="nfs">NFS</option>
                                        </Select>
                                    </div>
                                    <div>
                                        <Label htmlFor="wizard-storage-capacity">Capacity</Label>
                                        <Input id="wizard-storage-capacity" value={storageCapacity} onChange={(event) => setStorageCapacity(event.target.value.trim())} placeholder="100Gi" error={Boolean(storageCapacity) && !CAPACITY_PATTERN.test(storageCapacity.trim())} />
                                    </div>
                                </div>
                                <div>
                                    <Label htmlFor="wizard-storage-name">Name (optional)</Label>
                                    <Input id="wizard-storage-name" value={storageName} onChange={(event) => setStorageName(event.target.value)} placeholder="model-cache" />
                                </div>
                                {storageKind === 'local-disk' && (
                                    <div>
                                        <Label htmlFor="wizard-host-path">Host path</Label>
                                        <Input id="wizard-host-path" value={hostPath} onChange={(event) => setHostPath(event.target.value.trim())} placeholder="/data/model-cache" />
                                        {storageNodes.length > 0 && (
                                            <div className="mt-1.5 flex flex-wrap gap-1.5">
                                                {storageNodes.map((node) => (
                                                    <span key={node.name} className="rounded-full border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300">{node.name}</span>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                )}
                                {storageKind === 'nfs' && (
                                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                                        <div>
                                            <Label htmlFor="wizard-nfs-server">Server</Label>
                                            <Input id="wizard-nfs-server" value={nfsServer} onChange={(event) => setNfsServer(event.target.value.trim())} placeholder="nfs.example.com" />
                                        </div>
                                        <div>
                                            <Label htmlFor="wizard-nfs-path">Export path</Label>
                                            <Input id="wizard-nfs-path" value={nfsPath} onChange={(event) => setNfsPath(event.target.value.trim())} placeholder="/export/models" />
                                        </div>
                                    </div>
                                )}
                            </div>

                            <div className="border-t border-slate-700/60 pt-4">
                                <Checkbox
                                    id="wizard-prewarm-enabled"
                                    checked={prewarmEnabled}
                                    onChange={(event) => setPrewarmEnabled(event.target.checked)}
                                    label="Also pre-download (prewarm) a model into this cache now"
                                />
                                {prewarmEnabled && (
                                    <div className="mt-4 space-y-4">
                                        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                                            <div>
                                                <Label htmlFor="wizard-repo-id">Model repo id</Label>
                                                <Input id="wizard-repo-id" value={repoId} onChange={(event) => setRepoId(event.target.value)} placeholder="meta-llama/Llama-3.1-8B-Instruct" error={Boolean(repoId) && !REPO_ID_PATTERN.test(repoId.trim())} />
                                            </div>
                                            <div>
                                                <Label htmlFor="wizard-revision">Revision</Label>
                                                <Input id="wizard-revision" value={revision} onChange={(event) => setRevision(event.target.value)} placeholder="main" />
                                            </div>
                                        </div>
                                    </div>
                                )}
                            </div>
                        </div>
                    )}
                </div>
                </section>
            )}

            {stepIndex === 6 && (
                <section aria-labelledby="wizard-confirm-heading">
                    <h2 id="wizard-confirm-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                        <Rocket className="h-5 w-5 text-sky-400" /> Confirm
                    </h2>
                <div className="space-y-4">
                    <div className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-4 py-3 text-sm">
                        <div className="font-semibold text-theme-text">{cluster?.name}</div>
                        <div className="mt-1 text-xs text-theme-muted">
                            Proxy: {proxyMode === 'custom' ? 'custom' : 'auto (falls back to backend env for now)'} &middot;
                            {' '}Model gateway: {gatewayProvider} &middot;
                            {' '}llm-d: {llmDRef.trim() || 'default'}{cluster?.llmDRepoPath ? ` (downloaded to ${cluster.llmDRepoPath})` : ''}
                            {' '}&middot; llm-d-benchmark: {llmDBenchmarkRef.trim() || 'default'}{cluster?.llmDBenchmarkRepoPath ? ` (downloaded to ${cluster.llmDBenchmarkRepoPath})` : ''}
                        </div>
                        <div className="mt-1 text-xs text-theme-muted">
                            Accelerator: {acceleratorEnabled
                                ? `${ACCESS_MODE_LABEL[acceleratorAccessMode]} (${acceleratorNeedsInstall ? 'install' : 'already ready, reuse'})`
                                : 'skipped'}
                        </div>
                        <div className="mt-1 text-xs text-theme-muted">
                            HF_TOKEN secret: {secretRef ? `${secretRef.namespace}/${secretRef.name}` : 'not created'}
                        </div>
                        <div className="mt-1 text-xs text-theme-muted">
                            Model cache: {modelCacheEnabled
                                ? (prewarmEnabled ? `storage + prewarm ${repoId.trim()}@${revision.trim()}` : 'storage volume (no prewarm)')
                                : 'skipped'}
                        </div>
                    </div>
                    {finishStatus.length > 0 && (
                        <ul className="space-y-1.5">
                            {finishStatus.map((item) => (
                                <li key={item.key} className="flex items-center gap-2 text-sm">
                                    {item.state === 'running' && <Loader2 size={14} className="animate-spin text-sky-400" />}
                                    {item.state === 'done' && <Check size={14} className="text-emerald-400" />}
                                    {item.state === 'failed' && <span className="text-rose-400">&#10007;</span>}
                                    {item.state === 'pending' && <span className="h-2 w-2 rounded-full bg-slate-600" />}
                                    <span className={item.state === 'failed' ? 'text-rose-300' : 'text-theme-text'}>{item.label}</span>
                                    {item.detail && <span className="text-xs text-rose-300">— {item.detail}</span>}
                                </li>
                            ))}
                        </ul>
                    )}
                    {finished && (
                        <p className="flex items-center gap-2 text-sm text-emerald-400">
                            <CheckCircle2 className="h-4 w-4" /> Cluster setup complete.
                        </p>
                    )}
                </div>
                </section>
            )}
            </div>
        </Modal>
    );
}

export default CreateClusterWizard;
