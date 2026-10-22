import { useCallback, useEffect, useRef, useState } from 'react';
import { Terminal, Waypoints } from 'lucide-react';
import { usePolling } from '../../../../shared/lib/usePolling';
import { useClipboard } from '../../../../shared/lib/useClipboard';
import { AsyncState } from '../../../../shared/ui/AsyncState';
import { Button } from '../../../../shared/ui/Button';
import { EmptyState } from '../../../../shared/ui/EmptyState';
import { Modal } from '../../../../shared/ui/Modal';
import { ModuleHeader } from '../../../../shared/ui/ModuleHeader';
import { ModulePage } from '../../../../shared/ui/ModulePage';
import { SectionLabel } from '../../../../shared/ui/SectionLabel';
import { Panel } from '../../../../shared/ui/Panel';
import { Badge } from '../../../../shared/ui/Badge';
import { cn } from '../../../../shared/lib/cn';
import { getModelServiceConnection, listModelNames } from './modelServiceBackend';
import { CARD } from './modelServiceStyles';

// User-facing "My services" page: lists the model names the caller may call and
// the shared base URL. Personal tokens live on the separate "API keys" page.
export function ModelServicePage({ onToggleMobileNav }) {
    const [models, setModels] = useState([]);
    const [connection, setConnection] = useState({ baseUrl: '', clusters: [] });
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [examplesFor, setExamplesFor] = useState(null);
    const loadedOnceRef = useRef(false);
    const endpointClipboard = useClipboard();

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet && loadedOnceRef.current) setLoading(true);
        try {
            const [modelItems, connectionInfo] = await Promise.all([
                listModelNames(),
                getModelServiceConnection(),
            ]);
            setModels(modelItems);
            setConnection(connectionInfo || { baseUrl: '', clusters: [] });
            setError('');
        } catch (failure) {
            setError(failure?.message || 'Failed to load model services');
        } finally {
            loadedOnceRef.current = true;
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);
    usePolling(() => load({ quiet: true }));

    // Each cluster runs its own Gateway, so use the base URL of the model's cluster.
    // LENS_MODEL_GATEWAY_PUBLIC_URL (when set) overrides every cluster's URL.
    const clusterBaseUrls = Object.fromEntries(
        (connection.clusters || []).map((cluster) => [cluster.clusterId, cluster.baseUrl]),
    );
    const fallbackBase = connection.baseUrl || `http://${window.location.hostname}:8443/v1`;
    const baseUrlFor = (clusterId) => clusterBaseUrls[clusterId] || fallbackBase;
    const clusterNameById = Object.fromEntries(
        (connection.clusters || []).map((cluster) => [cluster.clusterId, cluster.clusterName || cluster.clusterId]),
    );
    const clusterProviderById = Object.fromEntries(
        (connection.clusters || []).map((cluster) => [cluster.clusterId, cluster.provider]),
    );
    // Only Istio / GKE wire the Inference Payload Processor, which turns the
    // request body's `model` into X-Gateway-Base-Model-Name. On other providers
    // (e.g. Envoy AI Gateway) the caller must send that header themselves.
    const autoInjectsBaseModel = (clusterId) => ['istio', 'gke'].includes(clusterProviderById[clusterId]);
    const baseModelHeaderLine = (model) => (
        autoInjectsBaseModel(model.clusterId)
            ? ''
            : ` \\\n  -H "X-Gateway-Base-Model-Name: ${model.baseModel || model.modelRef || model.name}"`
    );

    // Model services are cluster-scoped; group the caller's visible models by cluster.
    const modelsByCluster = models.reduce((acc, model) => {
        const key = model.clusterId || 'unassigned';
        (acc[key] ||= []).push(model);
        return acc;
    }, {});

    const curlSamples = examplesFor ? [
        {
            label: 'OpenAI · chat completions',
            command: `curl ${baseUrlFor(examplesFor.clusterId)}/chat/completions \\\n  -H "Authorization: Bearer lens-mk-..."${baseModelHeaderLine(examplesFor)} \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"${examplesFor.name}","messages":[{"role":"user","content":"Hello"}],"stream":true,"stream_options":{"include_usage":true}}'`,
        },
        {
            label: 'OpenAI · embeddings',
            command: `curl ${baseUrlFor(examplesFor.clusterId)}/embeddings \\\n  -H "Authorization: Bearer lens-mk-..."${baseModelHeaderLine(examplesFor)} \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"${examplesFor.name}","input":"Hello"}'`,
        },
        {
            label: 'Anthropic · messages (requires Anthropic support)',
            command: `curl ${baseUrlFor(examplesFor.clusterId)}/messages \\\n  -H "x-api-key: lens-mk-..." \\\n  -H "anthropic-version: 2023-06-01"${baseModelHeaderLine(examplesFor)} \\\n  -H "content-type: application/json" \\\n  -d '{"model":"${examplesFor.name}","max_tokens":1024,"messages":[{"role":"user","content":"Hello"}]}'`,
        },
    ] : [];

    return (
        <ModulePage className="my-services-page">
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={Waypoints}
                    title="My services"
                    description="Call published models by name with a personal API key. Each cluster has its own Gateway base URL."
                    onToggleMobileNav={onToggleMobileNav}
                />

                {error && (
                    <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {error}
                    </p>
                )}

                {(connection.clusters || []).length > 0 && (
                    <section className={cn(CARD, 'my-services-card w-full max-w-4xl self-start p-5')}>
                        <SectionLabel className="text-[#037ffc]">Gateway connections</SectionLabel>
                        <div className="mt-3 grid gap-3 md:grid-cols-2">
                            {connection.clusters.map((cluster) => (
                                <Panel key={cluster.clusterId} padding="sm" tone="subtle" className="min-w-0 shadow-none">
                                    <div className="mb-3 flex min-w-0 items-center gap-2">
                                        <Waypoints size={18} className="shrink-0 text-sky-500" aria-hidden="true" />
                                        <p className="min-w-0 flex-1 truncate text-sm font-semibold text-theme-text">{cluster.clusterName || cluster.clusterId}</p>
                                        <Badge tone="info" size="xs">{cluster.provider || 'gateway'}</Badge>
                                    </div>
                                    <div className="flex min-w-0 items-center gap-2 rounded-lg border border-theme-border bg-theme-bg p-2.5">
                                        <p className="min-w-0 flex-1 break-all font-mono text-xs text-theme-text">
                                            {cluster.baseUrl || (cluster.ready ? 'address pending' : 'Gateway not ready')}
                                        </p>
                                        {cluster.baseUrl && (
                                            <Button variant="ghost" size="xs" onClick={() => endpointClipboard.copy(cluster.baseUrl)}>
                                                {endpointClipboard.copiedValue === cluster.baseUrl ? 'Copied' : 'Copy'}
                                            </Button>
                                        )}
                                    </div>
                                    <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-theme-muted">
                                        {!!cluster.port && <Badge tone="neutral" size="xs">Port {cluster.port}</Badge>}
                                        {cluster.address && <span className="break-all font-mono">{cluster.address}</span>}
                                    </div>
                                </Panel>
                            ))}
                        </div>
                    </section>
                )}

                <section className={cn(CARD, 'my-services-card p-6')}>
                    <SectionLabel className="text-[#037ffc]">Available models</SectionLabel>
                    <div className="mt-4">
                        <AsyncState loading={loading} error={null} empty={models.length === 0}
                            emptyContent={<EmptyState icon={<Waypoints size={22} />} title="No models available"
                                iconClassName="my-services-empty-icon"
                                message="You do not have access to any published model service yet. Ask an administrator to grant access." />}>
                            <div className="flex flex-col gap-5">
                                {Object.entries(modelsByCluster).map(([clusterId, clusterModels]) => (
                                    <div key={clusterId}>
                                        <p className="mb-2 text-base font-semibold text-slate-900 dark:text-slate-100">
                                            {clusterNameById[clusterId] || clusterId}
                                            {baseUrlFor(clusterId) ? ` · ${baseUrlFor(clusterId)}` : ''}
                                        </p>
                                        <ul className="flex flex-col divide-y divide-slate-200 dark:divide-slate-800/80">
                                            {clusterModels.map((model) => (
                                                <li key={model.id} className="flex items-center justify-between gap-3 py-3">
                                                    <div className="min-w-0">
                                                        <p className="truncate font-mono text-sm text-slate-100">{model.name}</p>
                                                        <p className="truncate text-xs text-slate-400">{model.modelRef}</p>
                                                    </div>
                                                    <div className="flex shrink-0 items-center gap-2">
                                                        <Button variant="ghost" size="xs" onClick={() => setExamplesFor(model)}>
                                                            <Terminal size={12} /> cURL
                                                        </Button>
                                                    </div>
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                ))}
                            </div>
                        </AsyncState>
                    </div>
                </section>
            </div>

            <Modal
                isOpen={Boolean(examplesFor)}
                onClose={() => setExamplesFor(null)}
                title={examplesFor ? `cURL examples — ${examplesFor.name}` : 'cURL examples'}
                variant="drawer"
                size="lg"
            >
                <p className="mb-4 text-xs text-slate-400">
                    Replace <code className="font-mono">lens-mk-...</code> with one of your API keys.
                </p>
                <div className="flex flex-col gap-4">
                    {curlSamples.map((sample) => (
                        <div key={sample.label}>
                            <div className="mb-1 flex items-center justify-between gap-2">
                                <span className="text-xs font-semibold text-slate-200">{sample.label}</span>
                                <Button variant="ghost" size="xs" onClick={() => endpointClipboard.copy(sample.command)}>
                                    {endpointClipboard.copiedValue === sample.command ? 'Copied' : 'Copy'}
                                </Button>
                            </div>
                            <pre className="overflow-x-auto rounded-lg border border-slate-800/80 bg-slate-950/50 p-3 font-mono text-[11px] leading-relaxed text-slate-300 whitespace-pre">
{sample.command}
                            </pre>
                        </div>
                    ))}
                </div>
            </Modal>
        </ModulePage>
    );
}

export default ModelServicePage;
