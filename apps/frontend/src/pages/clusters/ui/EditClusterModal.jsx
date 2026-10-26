import { downloadClusterSoftware, listLensHosts } from '../../optimization';
import { requestJson } from '../../../shared/api/httpClient';
// "Edit cluster" modal for the Clusters overview page.
//
// Lets an operator update a cluster's name/description, network proxy, and
// pinned llm-d / llm-d-benchmark versions after creation (the wizard only
// captures these once, at creation time). Mirrors the same PATCH
// `/api/cluster/clusters/{id}` + `POST .../software-downloads` flow used by
// CreateClusterWizard's Proxy/Versions steps, so a ref change here actually
// triggers a real (re-)download instead of just updating the pinned ref.
import { useEffect, useState } from 'react';
import { Button, Input, Label, Modal, Textarea } from '../../../shared/ui';

function request(path, options = {}) {
    return requestJson(path, { headers: { 'Content-Type': 'application/json' }, ...options });
}

// Polls the llm-d versions software-downloads endpoint until both refs that
// were requested reach a terminal state (ready/failed) -- see
// CreateClusterWizard.jsx's identically-named helper for the wizard's copy.


export function EditClusterModal({ cluster, onClose, onSaved }) {
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [proxyMode, setProxyMode] = useState('auto');
    const [httpProxy, setHttpProxy] = useState('');
    const [httpsProxy, setHttpsProxy] = useState('');
    const [noProxy, setNoProxy] = useState('');
    const gatewayProvider = cluster.gatewayProvider || 'istio';
    const [gatewayPort, setGatewayPort] = useState('');
    // Optional externally reachable Gateway host/IP (or full URL); empty = derive.
    const [gatewayPublicUrl, setGatewayPublicUrl] = useState('');
    const [gatewayAuthzHost, setGatewayAuthzHost] = useState('');
    const [inotifyMaxUserInstances, setInotifyMaxUserInstances] = useState('8192');
    // Write-only HF_TOKEN rotation: the current token is never read back from
    // the cluster, so an empty field means "leave the existing secret alone".
    const [hfTokenNamespace, setHfTokenNamespace] = useState('');
    const [hfTokenName, setHfTokenName] = useState('');
    const [hfToken, setHfToken] = useState('');
    const [hfTokenSaved, setHfTokenSaved] = useState(false);
    const [portError, setPortError] = useState('');
    const [lensHosts, setLensHosts] = useState([]);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [downloadStatus, setDownloadStatus] = useState(null);

    useEffect(() => {
        if (!cluster) return;
        setName(cluster.name || '');
        setDescription(cluster.description || '');
        setProxyMode(cluster.proxy?.mode === 'custom' ? 'custom' : 'auto');
        setHttpProxy(cluster.proxy?.httpProxy || '');
        setHttpsProxy(cluster.proxy?.httpsProxy || '');
        setNoProxy(cluster.proxy?.noProxy || '');
        setGatewayPort(cluster.gatewayPort ? String(cluster.gatewayPort) : '');
        setGatewayPublicUrl(cluster.gatewayPublicUrl || '');
        setGatewayAuthzHost(cluster.gatewayAuthzHost || '');
        setInotifyMaxUserInstances(
            cluster.inotifyMaxUserInstances != null ? String(cluster.inotifyMaxUserInstances) : '8192',
        );
        setPortError('');
        setError('');
        setDownloadStatus(null);
        // Prefill the coordinates the cluster already points at; fall back to
        // the wizard's step-5 defaults so a cluster created before this field
        // existed (or one whose secret was never created) can get one here.
        setHfTokenNamespace(cluster.hfTokenSecret?.namespace || `llm-d-bench-storage-${cluster.id}`);
        setHfTokenName(cluster.hfTokenSecret?.name || `hf-token-${crypto.randomUUID().slice(0, 8)}`);
        setHfToken('');
        setHfTokenSaved(false);
    }, [cluster]);

    useEffect(() => {
        let alive = true;
        listLensHosts().then((items) => {
            if (!alive) return;
            setLensHosts(items);
            setGatewayAuthzHost((current) => current || items[0] || '');
        }).catch(() => {});
        return () => { alive = false; };
    }, []);

    if (!cluster) return null;

    const checkGatewayPort = async () => {
        const value = gatewayPort.trim();
        if (!value || (cluster.gatewayPort && Number(value) === cluster.gatewayPort)) { setPortError(''); return; }
        try {
            const query = new URLSearchParams({ port: String(Number(value)) });
            const payload = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}/gateway-port-check?${query.toString()}`);
            setPortError(payload.available ? '' : (payload.owner ? `Port ${value} is already used by ${payload.owner}` : (payload.reason || 'Port is not available')));
        } catch (err) {
            setPortError(err?.message || 'Could not check the port');
        }
    };

    const trimmedName = name.trim();
    const proxyValid = proxyMode === 'auto' || Boolean(httpProxy.trim() || httpsProxy.trim() || noProxy.trim());
    const nameValid = Boolean(trimmedName);
    // An empty token means "keep the current secret"; once one is typed the
    // target coordinates have to be complete.
    const hfTokenValid = !hfToken.trim() || Boolean(hfTokenNamespace.trim() && hfTokenName.trim());
    const canSave = nameValid && proxyValid && hfTokenValid && !busy && !portError && (gatewayProvider === 'gke' || Boolean(gatewayAuthzHost));

    const submit = async () => {
        if (!canSave) return;
        setBusy(true);
        setError('');
        try {
            const payload = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}`, {
                method: 'PATCH',
                body: JSON.stringify({
                    name: trimmedName,
                    description: description.trim(),
                    proxy:
                        proxyMode === 'custom'
                            ? { mode: 'custom', httpProxy: httpProxy.trim() || null, httpsProxy: httpsProxy.trim() || null, noProxy: noProxy.trim() || null }
                            : { mode: 'auto' },
                    // Only send gateway fields that actually changed, so editing e.g.
                    // the Lens address never overwrites a provider the user didn't touch.
                    ...(gatewayPort.trim() !== (cluster.gatewayPort ? String(cluster.gatewayPort) : '') && { gatewayPort: gatewayPort.trim() ? Number(gatewayPort.trim()) : null }),
                    ...(gatewayPublicUrl.trim() !== (cluster.gatewayPublicUrl || '') && { gatewayPublicUrl: gatewayPublicUrl.trim() || null }),
                    ...(gatewayAuthzHost.trim() !== (cluster.gatewayAuthzHost || '') && { gatewayAuthzHost: gatewayAuthzHost.trim() || null }),
                    // Always sent: applying the node limit is idempotent, so saving
                    // also (re)applies it to an existing cluster.
                    inotifyMaxUserInstances: inotifyMaxUserInstances.trim() ? Number(inotifyMaxUserInstances.trim()) : null,
                }),
            });
            let nextCluster = payload.cluster;
            // Rotate the HF_TOKEN secret only when a new token was typed: the
            // current value is write-only and is never read back, so an empty
            // field must leave the existing secret untouched.
            if (hfToken.trim()) {
                const ref = await request(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}/hf-token-secret`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        namespace: hfTokenNamespace.trim(),
                        name: hfTokenName.trim(),
                        token: hfToken.trim(),
                    }),
                });
                nextCluster = { ...nextCluster, hfTokenSecret: ref };
                setHfToken('');
                setHfTokenSaved(true);
            }
            // Versions are profile-fixed; only fetch the sources when this
            // cluster has not downloaded them yet.
            if (!nextCluster.llmDRepoPath || !nextCluster.llmDBenchmarkRepoPath) {
                setDownloadStatus({ downloading: true });
                nextCluster = await downloadClusterSoftware(nextCluster, { onUpdate: setDownloadStatus });
            }
            onSaved?.(nextCluster);
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal
            isOpen={!!cluster}
            onClose={() => !busy && onClose?.()}
            title="Edit cluster"
            size="md"
            variant="drawer"
            footer={<>
                <Button variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
                <Button variant="sky" onClick={submit} isLoading={busy} disabled={!canSave}>Save changes</Button>
            </>}
        >
            <div className="space-y-5">
                <div className="space-y-2">
                    <Label htmlFor="edit-cluster-name">Name</Label>
                    <Input id="edit-cluster-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Cluster name" disabled={busy} />
                </div>
                <div className="space-y-2">
                    <Label htmlFor="edit-cluster-description">Description</Label>
                    <Textarea id="edit-cluster-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Optional description" rows={2} disabled={busy} />
                </div>
                <div className="space-y-2">
                    <Label>Network proxy</Label>
                    <label className="flex items-start gap-2 text-sm text-slate-300">
                        <input type="radio" className="mt-1" name="edit-proxy-mode" checked={proxyMode === 'auto'} onChange={() => setProxyMode('auto')} disabled={busy} />
                        Auto (falls back to backend env)
                    </label>
                    <label className="flex items-start gap-2 text-sm text-slate-300">
                        <input type="radio" className="mt-1" name="edit-proxy-mode" checked={proxyMode === 'custom'} onChange={() => setProxyMode('custom')} disabled={busy} />
                        Custom
                    </label>
                    {proxyMode === 'custom' && (
                        <div className="grid gap-3 pl-6 sm:grid-cols-2">
                            <div className="space-y-1.5">
                                <Label htmlFor="edit-http-proxy">HTTP_PROXY</Label>
                                <Input id="edit-http-proxy" value={httpProxy} onChange={(event) => setHttpProxy(event.target.value)} placeholder="http://proxy:3128" disabled={busy} />
                            </div>
                            <div className="space-y-1.5">
                                <Label htmlFor="edit-https-proxy">HTTPS_PROXY</Label>
                                <Input id="edit-https-proxy" value={httpsProxy} onChange={(event) => setHttpsProxy(event.target.value)} placeholder="http://proxy:3128" disabled={busy} />
                            </div>
                            <div className="space-y-1.5 sm:col-span-2">
                                <Label htmlFor="edit-no-proxy">NO_PROXY</Label>
                                <Input id="edit-no-proxy" value={noProxy} onChange={(event) => setNoProxy(event.target.value)} placeholder="localhost,.svc" disabled={busy} />
                            </div>
                        </div>
                    )}
                </div>
                <div className="space-y-2">
                    <Label>HF_TOKEN secret</Label>
                    <p className="text-xs text-slate-500">
                        Hugging Face token this cluster uses to pull gated models. The stored value is never shown;
                        leave the token empty to keep the current one. Deployments and Model Cache downloads copy it
                        into their own namespace when they run, so workloads that are already running keep the
                        previous token until they are recreated.
                    </p>
                    <div className="grid gap-3 sm:grid-cols-2">
                        <div className="space-y-1.5">
                            <Label htmlFor="edit-hf-secret-namespace">Namespace</Label>
                            <Input id="edit-hf-secret-namespace" value={hfTokenNamespace} onChange={(event) => setHfTokenNamespace(event.target.value)} placeholder="llm-d-bench-storage-<cluster id>" disabled={busy} />
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="edit-hf-secret-name">Secret name</Label>
                            <Input id="edit-hf-secret-name" value={hfTokenName} onChange={(event) => setHfTokenName(event.target.value)} placeholder="hf-token-xxxxxxxx" disabled={busy} />
                        </div>
                        <div className="space-y-1.5 sm:col-span-2">
                            <Label htmlFor="edit-hf-token">New token</Label>
                            <Input id="edit-hf-token" type="password" autoComplete="new-password" value={hfToken} onChange={(event) => { setHfToken(event.target.value); setHfTokenSaved(false); }} placeholder={cluster.hfTokenSecret ? 'Leave empty to keep the current token' : 'hf_...'} disabled={busy} />
                        </div>
                    </div>
                    {!hfTokenValid && <p role="alert" className="text-xs text-rose-300">A namespace and secret name are required to save a new token.</p>}
                    {hfTokenSaved && <p className="text-xs text-emerald-300">Token updated in {hfTokenNamespace}/{hfTokenName}</p>}
                    {!cluster.hfTokenSecret && !hfTokenSaved && <p className="text-xs text-amber-300">This cluster has no HF_TOKEN secret yet. Enter a token to create one.</p>}
                </div>
                <div className="space-y-2">
                    <Label htmlFor="edit-gateway-provider">Model gateway provider</Label>
                    <Input id="edit-gateway-provider" value={gatewayProvider} readOnly />
                    <p className="text-xs text-slate-500">Fixed when the cluster was created. Recreate the cluster to use a different Gateway provider.</p>
                    <Label htmlFor="edit-gateway-port">Gateway exposed port (NodePort)</Label>
                    <Input id="edit-gateway-port" type="number" min="30000" max="32767" value={gatewayPort}
                        onChange={(event) => { setGatewayPort(event.target.value); setPortError(''); }}
                        onBlur={checkGatewayPort} placeholder="Optional, e.g. 30080" disabled={busy} />
                    {portError && <p role="alert" className="text-xs text-rose-300">{portError}</p>}
                    <p className="text-xs text-slate-500">NodePort the shared Gateway is exposed on. Leave empty and the cluster assigns a random one — the actual port is shown in the Gateway status. Clients reach it at http://&lt;node-ip&gt;:&lt;port&gt;/v1; bind your domain to that IP:PORT outside Lens.</p>
                    <Label htmlFor="edit-gateway-public-url">Gateway host/IP (optional)</Label>
                    <Input id="edit-gateway-public-url" value={gatewayPublicUrl}
                        onChange={(event) => setGatewayPublicUrl(event.target.value)}
                        placeholder="e.g. 10.112.229.74" disabled={busy} />
                    <p className="text-xs text-slate-500">Externally reachable host/IP clients call to reach the Gateway (Lens combines it with the exposed port). Leave empty and Lens derives one from the exposed port and the cluster node.</p>
                    <Label htmlFor="edit-gateway-authz-host">Lens address</Label>
                    <Input id="edit-gateway-authz-host" list="edit-lens-hosts" value={gatewayAuthzHost} onChange={(event) => setGatewayAuthzHost(event.target.value)} placeholder="Pick one or type a host/IP" disabled={busy || gatewayProvider === 'gke'} />
                    <datalist id="edit-lens-hosts">
                        {lensHosts.map((host) => <option key={host} value={host} />)}
                    </datalist>
                    <p className="text-xs text-slate-500">{gatewayProvider === 'gke' ? 'GKE Gateway has no Lens address hook, so callers are not checked.' : 'The host or IP this cluster uses to reach Lens (Lens adds its own port). Auto-filled; leave empty and Lens fills it in.'}</p>
                    <Label htmlFor="edit-inotify-limit">Node inotify limit (fs.inotify.max_user_instances)</Label>
                    <Input id="edit-inotify-limit" type="number" min="1" value={inotifyMaxUserInstances} onChange={(event) => setInotifyMaxUserInstances(event.target.value)} placeholder="8192" disabled={busy} />
                    <p className="text-xs text-slate-500">Applied to every node so a busy node does not exhaust inotify (kind defaults to 128). 8192 is recommended.</p>
                    <p className="text-xs text-slate-500">
                        llm-d component versions are fixed by this Lens release
                        {cluster.llmDRef ? ` (llm-d ${cluster.llmDRef}${cluster.llmDBenchmarkRef ? `, llm-d-benchmark ${cluster.llmDBenchmarkRef}` : ''})` : ''}.
                        The sources download automatically if missing.
                    </p>
                    {downloadStatus?.llmD && (
                        <p className="text-xs text-cyan-300">llm-d: {downloadStatus.llmD.state}...</p>
                    )}
                    {downloadStatus?.llmDBenchmark && (
                        <p className="text-xs text-cyan-300">llm-d-benchmark: {downloadStatus.llmDBenchmark.state}...</p>
                    )}
                </div>
                {error && <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-400">{error}</p>}
            </div>
        </Modal>
    );
}

export default EditClusterModal;
