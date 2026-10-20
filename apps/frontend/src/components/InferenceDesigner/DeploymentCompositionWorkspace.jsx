import React, { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, Settings2 } from 'lucide-react';
import AgenticDeploymentWorkspace from '../../pages/deployment/ui/AgenticDeploymentWorkspace';
import { Badge, Button, Panel, LoadingState, Modal } from '../../shared/ui/index';
import { useAuth } from '../../pages/auth/useAuth';
import { inferenceScene } from '../../features/inference-designer/sceneGraph';
import { compositionDisplayDraft } from '../../features/inference-designer/compositionPolicy';
import { generateAutomaticComposition, deploySourceBundle } from './sourceCompositionBackend';
import { InferenceGenerationResult } from './InferenceGenerationResult';
import { downloadBlob } from '../../shared/lib/download';
import { useClipboard } from '../../shared/lib/useClipboard';
const InferenceDesigner = lazy(() => import('./InferenceDesigner').then(module => ({ default: module.InferenceDesigner })));

/** Two selection policies share one immutable artifact, diagram and detailed editor. */
export function DeploymentCompositionWorkspace({ draft, mode, onNavigate, onDeploymentStarted, vllmParameterControls }) {
    const { can } = useAuth();
    const [run, setRun] = useState(null);
    const [plan, setPlan] = useState(null);
    const [busy, setBusy] = useState(false);
    const [editing, setEditing] = useState(false);
    const [error, setError] = useState('');
    const [fileIndex, setFileIndex] = useState(0);
    const [deploying, setDeploying] = useState(false);
    const operation = useRef(0);
    const deployLock = useRef(false);
    const { copy, copied } = useClipboard();
    const inputKey = JSON.stringify(draft);
    const previousInput = useRef(inputKey);
    useEffect(() => {
        if (previousInput.current === inputKey) return;
        previousInput.current = inputKey;
        operation.current += 1;
        setRun(null); setPlan(null); setEditing(false); setBusy(false); setError('');
    }, [inputKey]);
    useEffect(() => () => { operation.current += 1; }, []);
    const scene = useMemo(() => plan ? inferenceScene(compositionDisplayDraft(plan.draft, plan.result), plan.result.target.clusterName) : null, [plan]);
    const generate = async (recommendation) => {
        const id = ++operation.current;
        setRun(recommendation); setBusy(true); setError(''); setPlan(null);
        try {
            const result = await generateAutomaticComposition(recommendation.id);
            if (id === operation.current) { setPlan(result); setFileIndex(0); }
        } catch (reason) { if (id === operation.current) setError(reason.message); }
        finally { if (id === operation.current) setBusy(false); }
    };
    const deploy = async (endpoint, name) => {
        if (deployLock.current || !plan) return;
        deployLock.current = true; setDeploying(true); setError('');
        try {
            await deploySourceBundle(plan.result, endpoint, name);
            onDeploymentStarted();
        } catch (reason) { setError(reason.message); }
        finally { deployLock.current = false; setDeploying(false); }
    };
    const review = ({ draft: editedDraft, result }) => {
        const graph = inferenceScene(editedDraft);
        const decisions = graph.capabilities.map(cap => ({ id: cap.id, label: cap.label,
            state: cap.enabled ? 'enabled' : 'not_selected', reason: 'User-adjusted configuration; previous recommendation evidence is no longer a validation of these parameters.' }));
        const next = { ...plan, draft: editedDraft, result, decisions, edited: true,
            sla: { status: 'unverified', reason: 'The edited configuration requires a new benchmark to verify the SLA.' } };
        result.files = [...result.files.filter(file => file.name !== 'optimization-plan.json'),
            { name: 'optimization-plan.json', content: JSON.stringify({ mode, decisions, sla: next.sla, edited: true }, null, 2) }];
        setPlan(next); setEditing(false); setFileIndex(0);
    };
    if (editing && plan) return <Modal isOpen onClose={() => setEditing(false)} title="Deployment optimization map" size="xl">
        <Suspense fallback={<LoadingState label="Loading 3D configuration" />}><InferenceDesigner initialDraft={plan.draft} initialResult={plan.result} onNavigate={onNavigate}
            onClose={() => setEditing(false)} onReview={review} /></Suspense>
    </Modal>;
    if (!run) return <>
        <Panel title={mode === 'advanced' ? 'Advanced deployment requirements' : 'Agentic deployment requirements'}>
            <p className="text-sm text-theme-muted">{mode === 'advanced'
                ? 'Enable all compatible optimizations. Conflicts are resolved by SLA first, then accelerator and resource cost.'
                : 'Select a recommended optimization path for your SLA and workload, then minimize resource cost.'}</p>
        </Panel>
        <AgenticDeploymentWorkspace draft={draft} embedded compositionMode={mode} onPlanReady={generate}
            onBack={() => onNavigate('model-market')} onDeploymentStarted={onDeploymentStarted} vllmParameterControls={vllmParameterControls} />
    </>;
    return <div className="space-y-4">
        <Panel title={mode === 'advanced' ? 'Advanced deployment plan' : 'Agentic deployment plan'} actions={<>
            <Button variant="secondary" disabled={busy || deploying} onClick={() => { operation.current += 1; setRun(null); setPlan(null); setError(''); }}>Change requirements</Button>
            {plan && <Button variant="secondary" disabled={deploying} onClick={() => setEditing(true)}><Settings2 size={15} />Reasons & 3D configuration</Button>}
        </>}>
            {busy && <LoadingState label="Composing source optimizations and validating the target cluster" />}
            {error && <p role="alert" className="text-theme-danger">{error}</p>}
            {!busy && !plan && <Button onClick={() => generate(run)}>Retry composition</Button>}
            {plan && <>
                <p className="text-sm text-theme-muted">{plan.selection?.rationale}</p>
                <div className="mt-4 flex flex-wrap gap-3" aria-label="Enabled optimization flow">
                    {scene.nodes.filter(node => node.tier === 'flow').map((node, index) => <div key={node.id} className="flex items-center gap-3">
                        {index > 0 && <ArrowRight size={16} aria-hidden="true" />}<div className="rounded-lg border border-theme-border p-3"><strong className="text-sm">{node.label}</strong><div className="mt-2 flex flex-wrap gap-1">{scene.capabilities.filter(cap => cap.stage === node.id && cap.enabled).map(cap => <Badge key={cap.id} tone="success">{cap.label}</Badge>)}</div></div>
                    </div>)}
                </div>
                <div className="mt-3 flex flex-wrap gap-2">{scene.capabilities.filter(cap => cap.enabled && !scene.nodes.some(node => node.tier === 'flow' && node.id === cap.stage)).map(cap => <Badge key={cap.id} tone="success">{cap.label}</Badge>)}</div>
                <p className="mt-3 text-sm text-theme-muted">{plan.sla.reason}</p>
                <details className="mt-3"><summary>Optimization decisions ({plan.decisions.length})</summary><ul className="mt-2 space-y-2">{plan.decisions.map(item => <li key={item.id} className="text-sm"><strong>{item.label}</strong> · {item.state.replaceAll('_', ' ')}<p className="text-theme-muted">{item.reason}</p></li>)}</ul></details>
            </>}
        </Panel>
        {plan && <InferenceGenerationResult busy={false} errors={[]} error="" result={plan.result} fileIndex={fileIndex} setFileIndex={setFileIndex}
            onBack={() => setEditing(true)} onEdit={() => setEditing(true)} onManage={() => onNavigate('optimization-deployments')}
            onDeploy={deploy} deploying={deploying} canDeploy={can('deployment:run:create') && can('configuration:artifact:save') && can('configuration:artifact:render')}
            canSave={false} onCopy={() => copy(plan.result.files[fileIndex].content)} copied={copied}
            onDownload={() => downloadBlob(new Blob([plan.result.files[fileIndex].content], { type: 'text/plain' }), plan.result.files[fileIndex].name)}
            onRetryResources={() => generate(run)} />}
    </div>;
}
