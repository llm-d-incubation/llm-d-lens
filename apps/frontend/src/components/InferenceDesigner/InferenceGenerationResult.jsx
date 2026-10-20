import { useMemo, useState } from 'react';
import { ArrowLeft, ArrowRight, Download, FileCode, Rocket, Server, Database } from 'lucide-react';
import { Badge, Button, Input, Panel, Select, LoadingState } from '../../shared/ui/index';
import { compositionDeployment } from '../../features/inference-designer/compositionDeployment';

/** The generated snapshot is the deployment source; provenance stays available on demand. */
export function InferenceGenerationResult({ busy, errors, error, result, fileIndex, setFileIndex, onBack, onEdit, onExport, exporting, canSave, onCopy, copied, onDownload, onRetryResources, loadError, onDeploy, deploying, canDeploy, onManage }) {
    const [name, setName] = useState(null);
    const [endpoint, setEndpoint] = useState('');
    const summary = useMemo(() => result?.sourceComposition ? compositionDeployment(result) : busy ? { documents: [], services: [], workloads: [], provenance: {}, errors: [] } : null, [result, busy]);
    const selectedEndpoint = endpoint || (summary?.services.length === 1 ? summary.services[0].value : '');
    const file = result?.files[fileIndex];
    const validation = result?.plan.validation;
    const warnings = validation?.warnings || [];
    const deploymentName = name ?? result?.target?.model?.slice(0, 120) ?? '';
    return <section className="inference-result" aria-label="Generation results" aria-busy={busy}>
        <header><Button variant="ghost" disabled={deploying} onClick={onBack}><ArrowLeft size={15} />Back to design</Button><Button variant="ghost" onClick={onManage}>Deployments<ArrowRight size={15} /></Button></header>
        <div className="inference-result-title"><div><p className="text-sm text-theme-muted">Custom deployment</p><h1>{result ? 'Your deployment is generated' : busy ? 'Generating deployment' : 'Configuration checks'}</h1><p className="text-sm text-theme-muted">{result ? 'Review the resources, then deploy and manage them in Deployments.' : 'Preparing the selected source configuration.'}</p></div>{result && <Badge tone={validation?.clusterValidated ? 'success' : 'warning'}>{validation?.clusterValidated ? 'Admission check passed' : 'Cluster check not run'}</Badge>}</div>
        {!busy && (errors.length > 0 || error || loadError) && <Panel title="Items to resolve" actions={<Button disabled={deploying} onClick={onEdit}>Edit settings</Button>}>
            <ul className="list-disc pl-5 space-y-2">{errors.map(message => <li key={message}>{message}</li>)}</ul>
            {error && <p role="alert">{error}</p>}
            {loadError && <p role="alert">{loadError} <Button variant="secondary" onClick={onRetryResources}>Retry loading resources</Button></p>}
        </Panel>}
        {(result || busy) && <>
            {summary && <Panel title={result?.target?.clusterName || 'Deployment target'} actions={<Badge tone="neutral">{busy ? 'Rendering resources…' : `${summary.documents.length} resources`}</Badge>}>
                <div className="inference-bundle-flow" aria-label="Generated resource groups">
                    <div><Server size={22} /><strong>Services & routing</strong><span>{busy ? 'Reading routing resources…' : `${summary.services.length} service ${summary.services.length === 1 ? 'port' : 'ports'}`}</span><small>{summary.services.map(service => service.value).join(' · ') || (busy ? 'Available after rendering' : 'No Service generated')}</small></div>
                    <ArrowRight className="inference-bundle-arrow" size={24} aria-hidden="true" />
                    <div><Rocket size={22} /><strong>Workloads</strong><span>{busy ? 'Rendering workloads…' : `${summary.workloads.length} ${summary.workloads.length === 1 ? 'deployment' : 'deployments'}`}</span><small>{summary.workloads.map(workload => workload.metadata.name).join(' · ')}</small></div>
                    <ArrowRight className="inference-bundle-arrow" size={24} aria-hidden="true" />
                    <div><Database size={22} /><strong>Cached model</strong><span>{summary.provenance.modelBinding?.model || result?.target?.model || (busy ? 'Reading model binding…' : '')}</span><small>{summary.provenance.modelBinding?.pvcName}</small></div>
                </div>
                <div className="inference-bundle-launch">
                    <label className="text-sm">Deployment name<Input aria-label="Deployment name" value={deploymentName} disabled={busy || deploying} onChange={event => setName(event.target.value)} maxLength={120} /></label>
                    <label className="text-sm">Inference endpoint<Select aria-label="Inference endpoint" disabled={busy || deploying} value={selectedEndpoint} onChange={event => setEndpoint(event.target.value)}><option value="">Choose a generated Service</option>{summary.services.map(service => <option key={service.value} value={service.value}>{service.value}</option>)}</Select></label>
                    <Button onClick={() => onDeploy(selectedEndpoint, deploymentName)} isLoading={deploying} disabled={busy || !canDeploy || deploying || !selectedEndpoint || !deploymentName.trim() || summary.errors.length > 0}><Rocket size={16} />Deploy</Button>
                </div>
                <p className="text-xs text-theme-muted mt-3">Deploy creates an isolated namespace and mounts the selected model cache. Follow startup, logs and status in Deployments.</p>
                {!busy && !canDeploy && <p className="text-sm text-theme-muted mt-2">Configuration save and deployment create permissions are required.</p>}
                {summary.errors.map(message => <p key={message} role="alert" className="text-sm mt-2">{message}</p>)}
            </Panel>}
            <Panel title="Generated files" actions={file && <><Button variant="ghost" onClick={onCopy}>{copied ? 'Copied' : 'Copy'}</Button><Button variant="secondary" onClick={onDownload}><Download size={15} />Download</Button>{canSave && <Button variant="secondary" disabled={exporting} isLoading={exporting} onClick={onExport}>Download ZIP</Button>}</>}>
                {busy ? <div role="status" aria-live="polite"><LoadingState label="Rendering and checking resources" /></div> : <>
                <div className="inference-bundle-files"><FileCode size={18} /><Select aria-label="Generated file" value={fileIndex} onChange={event => setFileIndex(Number(event.target.value))}>{result.files.map((item, index) => <option key={item.name} value={index}>{item.name.endsWith('.json') ? `${item.name} · Source record` : item.name}</option>)}</Select></div>
                <p className="text-xs text-theme-muted mt-3">{file?.name === 'provenance.json' ? 'Source revision, component checksums and model binding. Kept for traceability; Kubernetes does not apply this file.' : result.sourceComposition ? 'Complete Kubernetes resources, including the router. Deployment rebinds namespace references to an isolated namespace.' : 'Generated deployment input. Router values are applied with their recorded Helm chart.'}</p>
                <pre className="mt-4 overflow-auto text-xs whitespace-pre">{file?.content}</pre></>}
            </Panel>
            {result && <details className="inference-bundle-details"><summary>Source & checks{warnings.length > 0 ? ` · ${warnings.length} notes` : ''}</summary>
                <dl className="inference-result-facts"><div><dt>Source commit</dt><dd>{result.plan.source.commit}</dd></div><div><dt>Router chart</dt><dd>{summary?.provenance.router?.version || result.plan.deploymentBundle.helm.version}</dd></div><div><dt>Validation</dt><dd>{validation?.clusterValidated ? 'Kubernetes accepted a dry-run. Runtime readiness is checked after deployment.' : 'Resources rendered. Kubernetes admission and runtime readiness are not yet verified.'}</dd></div></dl>
                {warnings.length > 0 && <ul className="list-disc pl-5 mt-4 space-y-2 text-sm text-theme-muted">{warnings.map((message, index) => <li key={index}>{message}</li>)}</ul>}
            </details>}
        </>}
    </section>;
}
