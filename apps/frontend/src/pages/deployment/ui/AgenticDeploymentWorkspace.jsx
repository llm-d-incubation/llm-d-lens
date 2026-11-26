import { lazy, Suspense, useEffect, useEffectEvent, useRef, useState } from 'react';
import { AlertTriangle, ArrowLeft, ArrowRight, Box, CheckCircle2, ChevronDown, Circle, Cpu, Download, HardDrive, Info, Layers, Maximize2, Rocket, ScrollText, Server, ShieldCheck, SlidersHorizontal, Sparkles } from 'lucide-react';
import { Badge, Button, EmptyState, LoadingState, Modal, ModulePage, Panel, Select, Spinner, SummaryStrip, WorkspaceHeader } from '../../../shared/ui';
import { downloadBlob } from '../../../shared/lib/download';
import { cn } from '../../../shared/lib/cn';
import { FormError } from '../../../shared/ui/FormError';
import { requestJson } from '../../../shared/api/httpClient';
import { usePolling } from '../../../shared/lib/usePolling';
import { historicalBenchmarkLabel, publicRagMatchSummary, retrievalBranchSummaries } from '../lib/agenticPresentation';
import { useAuth } from '../../auth';
import { getLocalDeploymentRun } from '../../optimization';
import { DeploymentPodsLogsModal } from './DeploymentManagement/DeploymentPodsLogsModal';
import { formatTimestamp } from '../../../shared/lib/formatTimestamp';
import { listAIProviders } from '../../ai-providers';
import { DeploymentRequirementsForm, PlanningProviderSelect } from './DeploymentRequirementsForm';
import { DeploymentPermissionHandoff, DeploymentPlanReview } from './DeploymentPlanReview';
import { MeasuredOptimizationControls } from './MeasuredOptimizationControls';
import { OptimizationTable } from './OptimizationTable';
import { optimizationRows } from '../model/deploymentPlan';
import {
    candidateEvidence,
    candidateLabel,
    checkDeploymentPermissions,
    defaultRequirements,
    deploymentPermissionPayload,
    deploymentPlanningRequest,
    generateDeploymentBootstrapScript,
    isDeploymentAdminSetupError,
    OPTIMIZATION_CONTROLS,
    requirementsFromRun,
    streamAgenticRecommendation,
    verifyDeploymentBootstrap,
} from '../api/agenticDeploymentBackend';
import { catalogCapability, deploymentEditorSnapshot, deploymentPlanManifest, renderDeploymentPlan, requirementCapability } from '../model/inference-designer/deploymentPlanning';
import { defaultInferenceEndpoint } from '../model/inference-designer/compositionDeployment';
import { PLANNING_STEPS, planningTimeline, rememberPlanningDurations } from '../model/planningSteps';

const InferenceDesigner = lazy(() => import('./inference-designer/InferenceDesigner').then(module => ({ default: module.InferenceDesigner })));

/** Clickable planning steps: each one reveals what runs internally and the events it reported. */
export function PlanningTimeline({ events, live = false }) {
    const [now, setNow] = useState(() => Date.now());
    const [open, setOpen] = useState(null);
    useEffect(() => { if (!live) return undefined; const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, [live]);
    const steps = planningTimeline(events, live ? now : events.at(-1)?.at ?? now);
    const current = open ?? (live ? steps.findIndex(step => step.state === 'active') : null);
    const step = current == null || current < 0 ? null : steps[current];
    const started = events[0]?.at;
    return <div className="space-y-3">
        <ol className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{steps.map((item, index) => <li key={item.label}>
            <button type="button" className="plan-step-button" aria-expanded={current === index} onClick={() => setOpen(current === index ? -1 : index)}>
                <span className={cn('plan-step-icon', item.state === 'done' && 'is-done', item.state === 'active' && live && 'is-active')}>{item.state === 'done' || (!live && item.state === 'active') ? <CheckCircle2 size={16} /> : item.state === 'active' ? <Spinner size="sm" /> : <Circle size={14} />}</span>
                <span className="min-w-0 flex-1"><span className={cn('block text-sm', item.state === 'active' && 'font-semibold')}>{item.label}</span>
                    <span className="block text-xs tabular-nums text-theme-muted">{item.seconds != null ? `${item.seconds}s${item.state === 'active' && live ? ` of ~${item.estimate}s` : ''}` : `~${item.estimate}s ${item.measured ? 'typical' : 'est.'}`}{item.events.length ? ` · ${item.events.length} events` : ''}</span></span>
                <ChevronDown size={14} className={cn('shrink-0 text-theme-muted transition', current === index && 'rotate-180')} aria-hidden="true" />
            </button>
        </li>)}</ol>
        {step && <div className="rounded-xl border border-theme-border p-4 text-sm" aria-label={`${step.label} details`}>
            <p className="font-semibold">{step.label}</p>
            <p className="mt-1 text-xs leading-relaxed text-theme-muted">{step.detail}</p>
            {step.events.length ? <ol className="mt-3 max-h-64 divide-y divide-theme-border overflow-auto rounded-lg border border-theme-border text-xs">{step.events.map((event, index) => <li key={index} className="space-y-1 px-3 py-2">
                <span className="flex items-center gap-2"><Badge size="xxs" tone={event.status === 'complete' ? 'success' : event.status === 'failed' ? 'danger' : 'neutral'}>{event.phase.replaceAll('_', ' ')}</Badge><span className="ml-auto tabular-nums text-theme-muted">{started && event.at ? `+${Math.round((event.at - started) / 1000)}s` : ''}</span></span>
                {String(event.message || '').length > 160
                    ? <details className="group/message"><summary className="cursor-pointer list-none break-words leading-relaxed"><span className="group-open/message:hidden">{event.message.slice(0, 150).replace(/\s+\S*$/, '')}… <span className="text-theme-muted underline">More</span></span><span className="hidden whitespace-pre-wrap group-open/message:inline">{event.message}</span></summary></details>
                    : <p className="break-words leading-relaxed">{event.message}</p>}
            </li>)}</ol> : <p className="mt-3 text-xs text-theme-muted">{step.state === 'pending' ? 'Not started yet.' : 'No separate events were reported for this step.'}</p>}
        </div>}
    </div>;
}

/** Stepped progress for one planning request; evidence steps without events are treated as passed once later steps start. */
export function PlanningProgress({ events, onCancel }) {
    const [seconds, setSeconds] = useState(0);
    useEffect(() => { const timer = setInterval(() => setSeconds(value => value + 1), 1000); return () => clearInterval(timer); }, []);
    const steps = planningTimeline(events);
    const reached = Math.max(0, steps.findIndex(step => step.state === 'active'));
    const percent = Math.min(96, Math.round(((reached + 0.5) / PLANNING_STEPS.length) * 100));
    const remaining = Math.max(0, steps.slice(reached).reduce((sum, step) => sum + step.estimate, 0) - (steps[reached]?.seconds || 0));
    return <Panel className="shadow-none" aria-label="Planning progress">
        <div className="flex flex-wrap items-start justify-between gap-3">
            <div role="status"><p className="text-lg font-semibold">Building your deployment plan</p><p className="mt-1 text-sm text-theme-muted">{events.at(-1)?.message || 'Starting…'}</p></div>
            <div className="flex items-center gap-3"><span className="text-sm tabular-nums text-theme-muted">{percent}% · {seconds}s{remaining > 0 ? ` · ~${remaining}s left` : ''}</span>{onCancel && <Button size="sm" variant="secondary" onClick={onCancel}>Cancel</Button>}</div>
        </div>
        <div className="plan-progress-track mt-4"><div className="plan-progress-fill" style={{ width: `${percent}%` }} /><div className="plan-progress-indeterminate" /></div>
        <div className="mt-5"><PlanningTimeline events={events} live /></div>
    </Panel>;
}

const api = (path, body) => requestJson(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

function slaSummary(run) {
    const targets = run.request?.requirements?.performance_targets || {};
    if (!targets.ttftMs && !targets.tpotMs) return 'Performance not benchmarked';
    const status = run.candidates?.find(item => item.id === run.selected_candidate?.id)?.slo_status;
    return status === 'satisfied' ? 'SLA met by AIC estimate' : status === 'not_satisfied' ? 'SLA not met by AIC estimate' : 'SLA not evaluated by AIC';
}

export function RetrievalEvidenceSources({ run }) {
    const usedIds = new Set(run.decision_evidence_ids || []);
    const usage = (ids) => ids.some((id) => usedIds.has(id)) ? 'used for selection' : 'context only';
    const caseReferences = (item) => [
        item.case_id,
        ...(item.measurements || []).map((measurement) => measurement.evidence_id),
        ...(item.provenance || []).map((citation) => `benchmark:${citation.benchmark_id}`),
    ];
    return retrievalBranchSummaries(run).map((branch) => {
        const details = branch.evidence?.details || {};
        const observations = details.response?.evidence || [];
        const matches = new Map((details.matches || []).map((match) => [match.evidence_id, match]));
        const sourceUsage = usage([
            branch.evidence?.id,
            ...observations.map((observation) => `public-rag:${observation.item_id}`),
            ...(branch.cases || []).flatMap(caseReferences),
        ]);
        return <details key={branch.id} className="group p-3 text-theme-text">
            <summary className="cursor-pointer list-none">
                <span className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-2 text-xs"><ChevronDown size={14} className="-rotate-90 group-open:rotate-0" />{branch.label}</span>
                    <Badge tone={branch.status === 'available' ? 'info' : 'neutral'} size="xs">{branch.status.replaceAll('_', ' ')}</Badge>
                </span>
                <span className="mt-1 block text-[11px] text-theme-muted">{branch.summary}</span>
            </summary>
            <div className="mt-3 space-y-2 text-[11px] text-theme-muted">
                <Badge tone={sourceUsage === 'used for selection' ? 'success' : 'neutral'} size="xs">{sourceUsage}</Badge>
                {branch.warning && <p>{branch.warning}</p>}
                <p>Historical measurements apply to their reported conditions, not current deployment SLO compliance. Compare model, hardware, runtime and workload before using them alongside AIC estimates.</p>
                {details.message && <p>{details.message}</p>}
                {details.retrieval?.mode && <p>Retrieval: {details.retrieval.mode.replaceAll('_', ' ')}.</p>}
                {details.retrieval?.model_identity_aliases && <p>Query model name aliases: {details.retrieval.model_identity_aliases.join(', ')}. Stored names use the same rule; source labels and citations are unchanged.</p>}
                {branch.id === 'public_sql' && <ul className="space-y-3">
                    {observations.map((observation) => <li key={observation.item_id}>
                        <p>{observation.metric} {observation.statistic}: {observation.value} {observation.unit}</p>
                        <Badge tone={usedIds.has(`public-rag:${observation.item_id}`) ? 'success' : 'neutral'} size="xs">{usage([`public-rag:${observation.item_id}`])}</Badge>
                        {publicRagMatchSummary(matches.get(`public-rag:${observation.item_id}`)).map((line) => <p key={line}>{line}</p>)}
                        <p className="break-all">{observation.citation?.source_path} @ {observation.citation?.source_revision}</p>
                        <p className="break-all">SHA-256: {observation.citation?.sha256}</p>
                    </li>)}
                </ul>}
                {branch.id === 'internal_sql' && <ul className="space-y-3">
                    {branch.cases.map((item) => <li key={item.case_id}>
                        <p className="font-semibold text-theme-text">{item.model} / {item.hardware} / {item.experiment_id}</p>
                        <Badge tone={usage(caseReferences(item)) === 'used for selection' ? 'success' : 'neutral'} size="xs">{usage(caseReferences(item))}</Badge>
                        <p>Applicability: {item.applicability}. Historical latency target status: {item.latency_status}.</p>
                        <p>Configuration: {JSON.stringify(item.configuration)}</p>
                        <p>Workload: {JSON.stringify(item.workload)}</p>
                        {item.known_differences?.length > 0 && <p>Known differences: {item.known_differences.join(', ')}.</p>}
                        {item.unknown_fields?.length > 0 && <p>Unknown fields: {item.unknown_fields.join(', ')}.</p>}
                        {item.ranking?.queue === 'fallback' && <p>Fallback reference only, not low-latency evidence.</p>}
                        <ul>{item.measurements?.map((measurement) => <li key={measurement.evidence_id}>{measurement.metric} {measurement.statistic}: {measurement.value} {measurement.unit} ({measurement.quality})</li>)}</ul>
                        {item.provenance?.map((citation, index) => <p key={index} className="break-all">Benchmark: {citation.benchmark_id}; {citation.source_path} @ {citation.source_revision}</p>)}
                        {item.validation_required?.length > 0 && <p>Validation required: {item.validation_required.join(', ')}.</p>}
                    </li>)}
                </ul>}
                {details.response?.truncated && <p>More observations matched; only the bounded retrieved subset was supplied to planning.</p>}
                {!observations.length && !branch.cases?.length && <p>No retained evidence details for this source.</p>}
            </div>
        </details>;
    });
}

export function DeploymentSummary({
    run,
    draft,
    deploymentRun,
    busy,
    locked,
    deployBlocked,
    editing,
    cleanupRequired,
    error = '',
    onDeploy,
    onCheck,
    onRequirements,
    onOpen,
    endpoint,
    onEndpointChange,
    confirmLabel = 'Deploy',
    adminHandoff = null,
    onGenerateBootstrapScript,
    onVerifyBootstrapAndDeploy,
    onSubjectOverrideChange,
}) {
    const plan = run.configuration_plan;
    const checked = Boolean(deploymentPlanManifest(plan)) && plan?.validation?.status === 'resources_checked';
    const submitted = Boolean(run.deployment_run_id);
    const ready = deploymentRun?.status === 'succeeded' && Boolean(deploymentRun.cases?.length) && deploymentRun.cases.every(item => item.status === 'ready');
    const needsAttention = ['failed', 'partially_succeeded', 'cancelled', 'cleaned'].includes(deploymentRun?.status);
    const title = submitted ? ready ? 'Deployment ready' : needsAttention ? 'Review deployment status' : 'Deployment submitted'
        : cleanupRequired ? 'Clean up test resources' : busy ? 'Updating your plan' : editing ? 'Finish editing to continue' : checked ? 'Ready for your confirmation' : 'Resource checks required';
    const candidate = run.selected_candidate;
    const resources = plan?.resource_estimate || {};
    const runtime = plan?.source_draft?.runtimeConfiguration;
    const required = resources.accelerators ?? (candidate ? candidate.replicas * candidate.tensor_parallel_size + (candidate.prefill_replicas || 0) * (candidate.prefill_tensor_parallel_size || 0) : null);
    const tone = submitted ? ready ? 'success' : needsAttention ? 'danger' : 'info' : cleanupRequired ? 'danger' : checked && !editing ? 'success' : 'warning';
    const HeroIcon = tone === 'success' ? ShieldCheck : tone === 'info' ? Rocket : AlertTriangle;
    const metrics = [
        [<Cpu key="cpu" size={16} />, 'Accelerators', required == null ? 'Unknown' : `${required}${resources.available_accelerators != null ? ` / ${resources.available_accelerators}` : ''}`, resources.available_accelerators != null ? 'required / available' : 'required', required != null && resources.available_accelerators != null && required > resources.available_accelerators ? 'danger' : 'info'],
        [<Layers key="layers" size={16} />, candidate?.prefill_replicas ? 'Prefill / Decode' : 'Serving', candidate ? candidate.prefill_replicas ? `${candidate.prefill_replicas}×TP${candidate.prefill_tensor_parallel_size} / ${candidate.replicas}×TP${candidate.tensor_parallel_size}` : `${candidate.replicas} × TP${candidate.tensor_parallel_size}` : 'Unknown', runtime?.slices > 1 ? `${runtime.slices} slices` : 'replicas × tensor parallel', 'info'],
        [<Server key="server" size={16} />, 'Cluster', draft.clusterName || draft.clusterId, resources.checked_at ? `checked ${formatTimestamp(resources.checked_at)}` : 'capacity re-checked on deploy', 'info'],
        [<HardDrive key="cache" size={16} />, 'Model cache', draft.storageVolumeName || draft.storageVolumeId, candidate?.native_cpu_cache_gib ? `CPU KV cache ${candidate.native_cpu_cache_gib} GiB / replica` : run.request?.placement_replicas ? `placement: ${run.request.placement_replicas}${run.request.placement_node_label ? ` (${run.request.placement_node_label.value})` : ''}` : '', 'info'],
    ];
    const checks = [
        [checked, checked ? 'Configuration and resource checks passed' : 'Configuration and resource checks pending'],
        [submitted ? ready : null, ready ? 'Serving components ready' : submitted ? 'Startup in progress; see the Deployments list' : 'Cluster admission and startup happen after confirmation'],
        [null, 'Readiness does not verify workload performance'],
    ];
    const endpointMissing = checked && Boolean(plan.source_bundle) && !plan.endpoints?.some(item => item.value === endpoint);
    const enabledOptimizations = plan?.decisions?.length ? optimizationRows(plan, candidate).filter(row => row.state === 'Enabled') : null;
    // The Deploy button and readiness checks already say a checked plan awaits confirmation.
    const showHero = submitted || cleanupRequired || busy || editing || !checked;
    return <section id="deployment-summary" className="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1fr)_300px]" aria-label="Deployment summary">
        <div className="min-w-0 space-y-4">
            {showHero && <div className={cn('deployment-hero', `deployment-tone-${tone}`)}>
                <span className="deployment-hero-icon"><HeroIcon size={22} /></span>
                <div className="min-w-0 flex-1"><p role="status" className="text-lg font-semibold text-theme-text">{title}</p>
                    <p className="text-sm text-theme-muted">{submitted ? ready ? 'Open the deployment for endpoints, monitoring and evaluation.' : 'Submission alone does not mean the model is ready.' : 'Confirm the footprint below. Capacity is checked again on deploy.'}</p></div>
            </div>}
            <div className="grid gap-3 sm:grid-cols-2">{metrics.map(([icon, label, value, detail, metricTone]) => <div key={label} className={cn('deployment-metric', `deployment-tone-${metricTone}`)}>
                <div className="flex items-start gap-3"><span className="deployment-metric-icon">{icon}</span><div className="min-w-0"><p className="text-xs text-theme-muted">{label}</p><p className="truncate text-base font-semibold" title={String(value)}>{value}</p>{detail && <p className="truncate text-xs text-theme-muted" title={detail}>{detail}</p>}</div></div>
            </div>)}</div>
            {enabledOptimizations && <div aria-label="Enabled optimizations" className="rounded-xl border border-theme-border p-3">
                <p className="text-xs text-theme-muted">Enabled in this YAML ({enabledOptimizations.length})</p>
                {enabledOptimizations.length ? <ul className="mt-2 flex flex-wrap gap-1.5">{enabledOptimizations.map(row => <li key={row.id}><Badge tone="info" size="md">{row.label}</Badge></li>)}</ul>
                    : <p className="mt-1 text-sm">No optimizations are enabled.</p>}
            </div>}
            <ul className="grid gap-2" aria-label="Deployment readiness">{checks.map(([state, label]) => <li key={label} className={cn('deployment-check', `deployment-tone-${state ? 'success' : state === null ? 'neutral' : 'warning'}`)}>
                {state ? <CheckCircle2 size={16} aria-hidden="true" /> : state === null ? <Info size={16} aria-hidden="true" /> : <Circle size={16} aria-hidden="true" />}<span>{label}</span>
            </li>)}</ul>
            {plan?.validation?.warnings?.length > 0 && <div className="deployment-check deployment-tone-warning flex-col"><p className="flex items-center gap-2 font-medium"><AlertTriangle size={16} />{plan.validation.warnings.length} configuration warning{plan.validation.warnings.length === 1 ? '' : 's'}</p><ul className="list-disc space-y-1 pl-6 text-theme-muted">{plan.validation.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
            {!submitted && adminHandoff && (
                <DeploymentPermissionHandoff
                    mode="bootstrap"
                    report={adminHandoff.report}
                    handoff={adminHandoff.verifyResult}
                    namespace={adminHandoff.namespace}
                    script={adminHandoff.script}
                    filename={adminHandoff.filename}
                    instructions={adminHandoff.instructions}
                    warnings={adminHandoff.warnings}
                    error={adminHandoff.error}
                    busy={busy}
                    verifying={Boolean(adminHandoff.verifying)}
                    generating={Boolean(adminHandoff.generating)}
                    identity={adminHandoff.identity}
                    subjectOverride={adminHandoff.subjectOverride}
                    onSubjectOverrideChange={onSubjectOverrideChange}
                    onGenerateScript={onGenerateBootstrapScript}
                    onPrimaryAction={onVerifyBootstrapAndDeploy}
                    primaryActionLabel="Re-check & Deploy"
                />
            )}
            {run.error && <p role="alert" className="text-sm">{run.error}</p>}
            {cleanupRequired && <p role="alert" className="text-sm">Clean up interrupted benchmark resources before revising or deploying.</p>}
        </div>
        <aside className="h-fit space-y-3 rounded-xl border border-theme-border bg-theme-bg/40 p-4">
            {!submitted && plan?.source_bundle && (plan.endpoints?.length === 1 && endpoint === plan.endpoints[0].value
                ? <div className="text-sm"><p className="font-medium">Inference Service</p><p className="mt-1 break-all font-mono text-xs">{endpoint}</p><p className="mt-1 text-xs text-theme-muted">Kubernetes Service that receives requests and is probed for readiness.</p></div>
                : <label className="block text-sm font-medium">Inference Service
                <Select className="mt-1" aria-label="Inference Service" value={endpoint || ''} disabled={locked || editing} onChange={event => onEndpointChange?.(event.target.value)}>
                    <option value="">Select the traffic entry Service</option>{plan.endpoints?.map(item => <option key={item.value} value={item.value}>{item.value}</option>)}
                </Select><span className="mt-1 block text-xs font-normal text-theme-muted">The Kubernetes Service that receives requests and is probed for readiness. Lens picks the router entry. The other choices are every port of every generated Service (for example EPP control or metrics ports); picking one can fail the readiness probe or bypass routing.</span>
            </label>)}
            {error && <FormError message={error} />}
            {submitted ? onOpen && <Button size="md" className="w-full" onClick={onOpen}>Open deployment</Button> : <>
                <Button size="md" className="w-full" disabled={locked || editing || !plan || endpointMissing || Boolean(adminHandoff) || (checked && deployBlocked)} isLoading={busy} onClick={checked ? onDeploy : onCheck}>{checked ? confirmLabel : 'Retry checks'}</Button>
                {checked && <Button variant="secondary" className="w-full" disabled={locked || editing || busy || Boolean(adminHandoff?.verifying) || Boolean(adminHandoff?.generating)} onClick={onCheck}>Recheck resources</Button>}
                <Button variant="ghost" className="w-full" disabled={locked || editing} onClick={onRequirements}>Change workload &amp; limits</Button>
                <p className="text-xs leading-relaxed text-theme-muted">Deploys exactly this configuration.</p>
            </>}
        </aside>
    </section>;
}

export function DeploymentWorkflow({ draft, mode = 'agentic', embedded = false, onBack, onDeploymentStarted, onSwitchToManual, onRestorePlan, restoreLabel = 'Return to previous plan' }) {
    const { can } = useAuth();
    const permitted = can('deployment:run:create');
    const [run, setRun] = useState(null), [requirements, setRequirements] = useState(() => ({ ...defaultRequirements(), objective: draft?.initialObjective || 'balanced', ...draft?.initialRequirements }));
    const [busy, setBusy] = useState(false), [building, setBuilding] = useState(false), [error, setError] = useState(''), [events, setEvents] = useState([]);
    const [reviewTab, setReviewTab] = useState('summary'), [logOpen, setLogOpen] = useState(false);
    const [providers, setProviders] = useState([]), [provider, setProvider] = useState(draft?.initialProvider || '');
    const [focus, setFocus] = useState('');
    const [restoring, setRestoring] = useState(true), [approval, setApproval] = useState(null);
    const [editing, setEditing] = useState(false);
    const [endpoint, setEndpoint] = useState('');
    const [sourceProfiles, setSourceProfiles] = useState([]), [profileId, setProfileId] = useState('');
    const [workspaceView, setWorkspaceView] = useState('review');
    const [primaryPanel, setPrimaryPanel] = useState('optimizations'), [flowToggle, setFlowToggle] = useState(null);
    const [changingRequirements, setChangingRequirements] = useState(false);
    const [trialBusy, setTrialBusy] = useState(false);
    const [deploymentRun, setDeploymentRun] = useState(null), [logsDeployment, setLogsDeployment] = useState(null);
    const [adminHandoff, setAdminHandoff] = useState(null);
    const requestVersion = useRef(0);
    const abort = useRef(null), mounted = useRef(true);
    const autoStarted = useRef(null);
    const key = `lens-deployment-plan:${mode}:${draft?.clusterId}:${draft?.model}:${draft?.deploymentName}:${draft?.storageVolumeId}${draft?.initialObjective ? `:${draft.initialObjective}` : ''}${draft?.planningAttemptId ? `:${draft.planningAttemptId}` : ''}`;
    useEffect(() => {
        mounted.current = true;
        return () => { mounted.current = false; abort.current?.abort(); };
    }, []);
    useEffect(() => {
        let active = true;
        requestVersion.current += 1; abort.current?.abort();
        setRun(null); setDeploymentRun(null); setLogsDeployment(null); setApproval(null); setAdminHandoff(null); setEndpoint(''); setRestoring(true); setBusy(false); setError(''); setEvents([]); setChangingRequirements(false);
        setRequirements({ ...defaultRequirements(), objective: draft?.initialObjective || 'balanced', ...draft?.initialRequirements }); setProvider(draft?.initialProvider || '');
        const id = draft?.resumeRunId || sessionStorage.getItem(key);
        if (!id) { setRestoring(false); return undefined; }
        api(`/api/agentic-deployments/${encodeURIComponent(id)}`).then(value => {
            if (active) {
                setRequirements(requirementsFromRun(value));
                setProvider(value.request.ai_provider_id || '');
                setRun(value);
                setEndpoint(defaultInferenceEndpoint(value.configuration_plan?.endpoints || []));
                setWorkspaceView(value.deployment_run_id ? 'execution' : value.status === 'optimizing' ? 'trials' : 'review');
            }
        }).catch(failure => { if (active) setError(`Unable to restore the saved plan: ${failure.message}`); })
            .finally(() => { if (active) setRestoring(false); });
        return () => { active = false; };
    }, [key, draft?.contextLength, draft?.initialObjective, draft?.initialRequirements, draft?.initialProvider, draft?.resumeRunId, mode]);
    useEffect(() => {
        if (mode !== 'agentic') return undefined;
        let active = true;
        listAIProviders().then(items => { if (active) { setProviders(items); setProvider(current => current || items[0]?.id || ''); } })
            .catch(failure => { if (active) setError(`Unable to load AI providers: ${failure.message}`); });
        return () => { active = false; };
    }, [mode]);
    const accept = (value, keepReview = false) => {
        if (!mounted.current) return;
        const nextChecksum = value.configuration_plan?.configuration?.checksum;
        setRun(value);
        setApproval(current => keepReview && current ? {
            runId: value.id,
            checksum: nextChecksum,
            ...(current.checksum === nextChecksum && current.namespace ? { namespace: current.namespace } : {}),
            ...(current.checksum === nextChecksum && current.prepared ? { prepared: current.prepared } : {}),
        } : null);
        setAdminHandoff(current => (keepReview && current && current.runId === value.id && current.checksum === nextChecksum)
            ? { ...current, generating: false, verifying: false }
            : null);
        const endpoints = value.configuration_plan?.endpoints || [];
        setEndpoint(current => endpoints.some(item => item.value === current) ? current : defaultInferenceEndpoint(endpoints));
        sessionStorage.setItem(key, value.id);
    };
    const active = run?.status === 'optimizing' || run?.status === 'deploying';
    usePolling(async () => {
        if (!run) return;
        const version = requestVersion.current;
        try {
            const value = await api(`/api/agentic-deployments/${encodeURIComponent(run.id)}`);
            if (mounted.current && version === requestVersion.current) setRun(value);
            if (value.deployment_run_id) {
                const execution = await getLocalDeploymentRun(value.deployment_run_id);
                if (mounted.current && version === requestVersion.current) setDeploymentRun(execution);
            }
        } catch (failure) { if (mounted.current && version === requestVersion.current) setError(failure.message); }
    }, { enabled: Boolean(run) && !busy && !trialBusy, intervalMs: active ? 5000 : 15000, immediate: true });
    const generate = async value => {
        const version = ++requestVersion.current;
        setBusy(true); setBuilding(true); setError(''); setApproval(null); setEvents([]);
        const controller = new AbortController();
        abort.current = controller;
        try {
            const session = await api(`/api/cluster/session?clusterId=${encodeURIComponent(draft.clusterId)}`);
            const payload = deploymentPlanningRequest(run ? {
                ...draft, contextLength: run.request.max_model_len, gpuMemoryUtilization: run.request.gpu_memory_utilization,
                maxNumSeqs: run.request.max_num_seqs, maxNumBatchedTokens: run.request.max_num_batched_tokens,
            } : draft, value, session.sessionId || session.session_id, mode, provider);
            let result;
            await streamAgenticRecommendation(payload, (type, data) => {
                if (type === 'progress' && mounted.current && version === requestVersion.current) setEvents(current => [...current.slice(-79), { ...data, at: Date.now() }]);
                if (type === 'complete') result = data;
                if (type === 'error') throw new Error(data.message || 'Planning failed.');
            }, controller.signal);
            if (!result) throw new Error('Planning ended without a validated configuration.');
            if (!mounted.current || version !== requestVersion.current) return;
            accept(result); setRequirements(requirementsFromRun(result)); setChangingRequirements(false); setWorkspaceView('review');
            {
                const progress = message => { if (mounted.current && version === requestVersion.current) setEvents(current => [...current, { phase: 'resources', status: 'running', message, at: Date.now() }]); };
                try {
                    const rendered = await renderDeploymentPlan(result, { compare: true, profileId, onProgress: progress });
                    if (!mounted.current || version !== requestVersion.current) return;
                    accept(rendered);
                    setEvents(current => { const next = [...current, { phase: 'resources', status: 'complete', message: 'Configuration checked. Ready for review.', at: Date.now() }]; rememberPlanningDurations(next); return next; });
                } catch (failure) {
                    if (failure.profiles && mounted.current) setSourceProfiles(failure.profiles);
                    if (mounted.current && version === requestVersion.current) setError(`The recommendation is preserved, but source resource checks failed: ${failure.message}. Resolve the source requirements and retry; deployment remains blocked.`);
                }
            }
            return true;
        } catch (failure) {
            if (mounted.current && version === requestVersion.current) setError(controller.signal.aborted ? 'Planning cancelled. No deployment was started.' : failure.message);
            return false;
        } finally { if (mounted.current && version === requestVersion.current) { setBusy(false); setBuilding(false); abort.current = null; } }
    };
    const autoGenerate = mode === 'manual' || Boolean(draft?.initialObjective);
    const startPlanning = useEffectEvent(() => generate({ ...defaultRequirements(), objective: draft?.initialObjective || 'balanced', ...draft?.initialRequirements }));
    useEffect(() => {
        if (autoGenerate && !restoring && !run && !error && permitted && autoStarted.current !== key) {
            autoStarted.current = key;
            startPlanning();
        }
    }, [autoGenerate, restoring, run, error, permitted, key]);
    const mutate = async (action, body, checkResources = false) => {
        const version = ++requestVersion.current;
        setBusy(true); setError('');
        try {
            const url = `/api/agentic-deployments/${encodeURIComponent(run.id)}/${action}`;
            let value = await api(url, body);
            if (!mounted.current || version !== requestVersion.current) return null;
            const prepared = value.configuration_plan?.validation?.preparedDeployment;
            if (action === 'approve' && !body.prepared_plan_id && !value.deployment_run_id && prepared?.preflight.status === 'pass') {
                value = await api(url, { ...body, prepared_plan_id: prepared.id, expected_plan_digest: prepared.digest, acknowledge_unknown: false });
                if (!mounted.current || version !== requestVersion.current) return null;
            }
            accept(value);
            if (checkResources) {
                try {
                    const checkedRun = value.configuration_plan?.source_bundle ? value : await renderDeploymentPlan(value, { profileId });
                    if (!mounted.current || version !== requestVersion.current) return null;
                    accept(checkedRun);
                    return checkedRun;
                } catch (failure) {
                    if (mounted.current && version === requestVersion.current) setError(`The change is saved, but resource checks failed: ${failure.message}. Deployment is blocked until checks pass.`);
                }
            }
            return value;
        } catch (failure) {
            if (mounted.current && version === requestVersion.current) setError(failure.message);
            throw failure;
        } finally { if (mounted.current && version === requestVersion.current) setBusy(false); }
    };
    const choose = async id => { try { if (await mutate('select', { candidate_id: id }, true)) setWorkspaceView('review'); } catch { /* mutate presents the error. */ } };
    const renderSource = async ({ draft: editedDraft, compare = false } = {}) => {
        const version = ++requestVersion.current;
        setBusy(true); setError('');
        try {
            const value = await renderDeploymentPlan(run, { editedDraft, compare, profileId });
            if (mounted.current && version === requestVersion.current) { accept(value, true); setEditing(false); }
            return value;
        } catch (failure) {
            if (mounted.current && version === requestVersion.current) { setError(failure.message); if (failure.profiles) setSourceProfiles(failure.profiles); }
            throw failure;
        } finally { if (mounted.current && version === requestVersion.current) setBusy(false); }
    };
    const render = async () => { try { await renderSource(); } catch { /* The source error remains visible; no legacy fallback is deployed. */ } };
    const edit = ({ node, patch }) => mutate('select', {
        candidate_id: run.selected_candidate.id, role: node.role === 'prefill' ? 'prefill' : 'decode',
        replicas: patch.replicas, tensor_parallel_size: patch.tensorParallelSize,
        max_model_len: patch.max_model_len, gpu_memory_utilization: patch.gpu_memory_utilization,
        max_num_seqs: patch.max_num_seqs, max_num_batched_tokens: patch.max_num_batched_tokens,
        native_cpu_cache_gib: patch.native_cpu_cache_gib,
    }, true);
    const buildPermissionPayload = (extra = {}) => {
        const currentPlan = run?.configuration_plan;
        const runtimeLabels = currentPlan?.source_draft?.runtimeConfiguration?.namespaceLabels;
        const sourceLabels = currentPlan?.source_draft?.sourcePlan?.namespaceLabels;
        const rawLabels = (runtimeLabels && Object.keys(runtimeLabels).length ? runtimeLabels : null)
            || (sourceLabels && Object.keys(sourceLabels).length ? sourceLabels : null);
        return deploymentPermissionPayload({
            clusterId: draft.clusterId,
            manifest: deploymentPlanManifest(currentPlan),
            namespaceLabels: rawLabels,
            agenticDeploymentRunId: run?.id,
            preparedPlanId: approval?.prepared?.id,
            deploymentName: draft.deploymentName,
            providerRef: run?.selected_candidate?.provider_ref,
            storageVolumeId: draft.storageVolumeId,
            usesStorage: Boolean(draft.storageVolumeId),
            ...extra,
        });
    };
    const generateBootstrapScript = async (overrideArg = null, seed = null) => {
        const current = seed || adminHandoff;
        const version = requestVersion.current;
        const override = overrideArg || current?.subjectOverride || null;
        const validOverride = override?.name?.trim()
            ? {
                kind: override.kind || 'ServiceAccount',
                name: override.name.trim(),
                ...(override.kind === 'ServiceAccount' && override.namespace?.trim()
                    ? { namespace: override.namespace.trim() }
                    : {}),
            }
            : null;
        setAdminHandoff(prev => (prev ? { ...prev, generating: true, error: '', ...(validOverride ? { subjectOverride: validOverride } : {}) } : prev));
        try {
            const remediation = await generateDeploymentBootstrapScript(buildPermissionPayload({
                namespace: current?.namespace || approval?.namespace || approval?.prepared?.cases?.[0]?.namespace || undefined,
                preparedPlanId: current?.preparedPlanId || approval?.prepared?.id || undefined,
                subjectOverride: validOverride,
            }));
            if (!mounted.current || version !== requestVersion.current) return;
            setAdminHandoff(prev => (prev ? {
                ...prev,
                generating: false,
                script: remediation.script || '',
                filename: remediation.scriptFilename || remediation.filename || 'lens-deployment-bootstrap.sh',
                instructions: remediation.instructions || '',
                bootstrapDigest: remediation.bootstrapDigest || remediation.digest || null,
                namespace: remediation.namespace || prev.namespace,
                identity: remediation.identity || prev.identity,
                warnings: Array.isArray(remediation.warnings) ? remediation.warnings : prev.warnings,
            } : prev));
        } catch (failure) {
            if (!mounted.current || version !== requestVersion.current) return;
            setAdminHandoff(prev => (prev ? { ...prev, generating: false, error: failure.message || 'Could not generate Orchestration administrator script.' } : prev));
        }
    };
    const openAdminSetupFromError = (failure, fallbackNamespace = '', showFailureError = false) => {
        const rawBody = failure?.body && typeof failure.body === 'object' ? failure.body : {};
        const body = rawBody.detail && typeof rawBody.detail === 'object' && !Array.isArray(rawBody.detail)
            ? { ...rawBody.detail, ...rawBody }
            : rawBody;
        const seed = {
            runId: run.id,
            checksum: approval.checksum,
            preparedPlanId: body.preparedPlanId || body.prepared_plan_id || failure?.preparedPlanId || approval?.prepared?.id || null,
            report: body,
            verifyResult: null,
            namespace: body.namespace || failure?.preparedNamespace || fallbackNamespace || approval?.prepared?.cases?.[0]?.namespace || '',
            identity: body.identity || null,
            subjectOverride: adminHandoff?.subjectOverride || null,
            script: typeof body.script === 'string' ? body.script : '',
            filename: body.scriptFilename || 'lens-deployment-bootstrap.sh',
            instructions: body.instructions || '',
            bootstrapDigest: body.bootstrapDigest || body.digest || null,
            warnings: Array.isArray(body.warnings) ? body.warnings : [],
            generating: false,
            verifying: false,
            error: showFailureError ? (failure.message || (typeof body.detail === 'string' ? body.detail : '') || '') : '',
        };
        setError('');
        setAdminHandoff(seed);
        setBusy(false);
        return seed;
    };
    const executeApprove = async (targetNamespace = '', activeVersion = requestVersion.current) => {
        const normalizedNamespace = typeof targetNamespace === 'string' ? targetNamespace.trim() : '';
        const basePayload = {
            configuration_checksum: approval.checksum,
            ...(plan?.source_bundle ? { endpoint } : {}),
            ...(normalizedNamespace ? { namespace: normalizedNamespace } : {}),
        };
        let result = await api(`/api/agentic-deployments/${encodeURIComponent(run.id)}/approve`, {
            ...basePayload,
            ...(approval?.prepared ? {
                prepared_plan_id: approval.prepared.id,
                expected_plan_digest: approval.prepared.digest,
                acknowledge_unknown: approval.prepared.preflight?.status === 'unknown',
            } : {}),
        });
        if (!mounted.current || activeVersion !== requestVersion.current) return null;
        let prepared = result?.configuration_plan?.validation?.preparedDeployment;
        if (!approval?.prepared && prepared && !result?.deployment_run_id && prepared.preflight?.status === 'pass') {
            const frozenNamespace = prepared.cases?.[0]?.namespace || normalizedNamespace || '';
            setRun(result);
            setApproval(prev => ({
                runId: result.id,
                checksum: approval.checksum,
                prepared,
                ...(frozenNamespace || prev?.namespace ? { namespace: frozenNamespace || prev.namespace } : {}),
            }));
            try {
                result = await api(`/api/agentic-deployments/${encodeURIComponent(run.id)}/approve`, {
                    ...basePayload,
                    ...(frozenNamespace ? { namespace: frozenNamespace } : {}),
                    prepared_plan_id: prepared.id,
                    expected_plan_digest: prepared.digest,
                    acknowledge_unknown: false,
                });
            } catch (step2Failure) {
                if (step2Failure && typeof step2Failure === 'object') {
                    step2Failure.preparedPlanId = prepared.id;
                    if (frozenNamespace) step2Failure.preparedNamespace = frozenNamespace;
                }
                throw step2Failure;
            }
            if (!mounted.current || activeVersion !== requestVersion.current) return null;
            prepared = result?.configuration_plan?.validation?.preparedDeployment;
        }
        if (prepared && !result?.deployment_run_id) {
            const frozenNamespace = prepared.cases?.[0]?.namespace || normalizedNamespace || '';
            setRun(result);
            setApproval(prev => ({
                runId: result.id,
                checksum: approval.checksum,
                prepared,
                ...(frozenNamespace || prev?.namespace ? { namespace: frozenNamespace || prev.namespace } : {}),
            }));
            setAdminHandoff(null);
            return result;
        }
        if (!result?.deployment_run_id) {
            accept(result, true);
            setAdminHandoff(null);
            return result;
        }
        accept(result, false);
        setApproval(null);
        setAdminHandoff(null);
        // Execution status lives in the Deployments job list, not inside the plan.
        if (onDeploymentStarted) onDeploymentStarted(result); else setWorkspaceView('execution');
        return result;
    };
    const deploy = async () => {
        if (!approval || approval.runId !== run.id || approval.checksum !== run.configuration_plan?.configuration?.checksum) {
            setApproval(null); setAdminHandoff(null); setError('The configuration changed. Review the current plan before deploying.'); return;
        }
        const version = ++requestVersion.current;
        let checkedNamespace = approval.namespace || approval.prepared?.cases?.[0]?.namespace || '';
        setBusy(true); setError('');
        try {
            const checkResult = await checkDeploymentPermissions(buildPermissionPayload({
                ...(checkedNamespace ? { namespace: checkedNamespace } : {}),
                ...(approval.prepared?.id ? { preparedPlanId: approval.prepared.id } : {}),
            }));
            if (!mounted.current || version !== requestVersion.current) return;
            checkedNamespace = checkResult?.namespace || checkedNamespace;
            if (checkResult?.sufficient || checkResult?.status === 'sufficient') {
                const approvedRun = await executeApprove(checkedNamespace, version);
                if (!mounted.current || version !== requestVersion.current) return;
                if (!approvedRun?.deployment_run_id && !approvedRun?.configuration_plan?.validation?.preparedDeployment) {
                    setError('Deployment approval did not start an execution run. Try again.');
                }
                return;
            }
            const seed = {
                runId: run.id,
                checksum: approval.checksum,
                preparedPlanId: approval.prepared?.id || null,
                report: checkResult,
                verifyResult: null,
                namespace: checkedNamespace,
                identity: checkResult?.identity || null,
                subjectOverride: null,
                script: '',
                filename: 'lens-deployment-bootstrap.sh',
                instructions: '',
                bootstrapDigest: checkResult?.marker?.bootstrapDigest || null,
                warnings: Array.isArray(checkResult?.warnings) ? checkResult.warnings : [],
                generating: false,
                verifying: false,
                error: '',
            };
            setAdminHandoff(seed);
            setBusy(false);
            if (!checkResult?.identity?.overrideRequired && checkResult?.identity?.kind !== 'unknown') {
                await generateBootstrapScript(null, seed);
            }
        } catch (failure) {
            if (!mounted.current || version !== requestVersion.current) return;
            if (isDeploymentAdminSetupError(failure)) {
                const seed = openAdminSetupFromError(failure, checkedNamespace, false);
                if (!seed.script && !seed.identity?.overrideRequired && !seed.subjectOverride?.name && seed.identity?.kind !== 'unknown') {
                    await generateBootstrapScript(null, seed);
                }
                return;
            }
            setError(failure.message || 'Deployment failed.');
            setBusy(false);
        } finally {
            if (mounted.current && version === requestVersion.current) setBusy(false);
        }
    };
    const verifyBootstrapAndDeploy = async () => {
        if (!adminHandoff || adminHandoff.verifying) return;
        if (!approval || approval.runId !== run.id || approval.checksum !== run.configuration_plan?.configuration?.checksum) {
            setApproval(null); setAdminHandoff(null); setError('The configuration changed. Review the current plan before deploying.'); return;
        }
        const version = ++requestVersion.current;
        const handoffSnapshot = adminHandoff;
        const override = handoffSnapshot.subjectOverride?.name?.trim()
            ? {
                kind: handoffSnapshot.subjectOverride.kind || 'ServiceAccount',
                name: handoffSnapshot.subjectOverride.name.trim(),
                ...(handoffSnapshot.subjectOverride.kind === 'ServiceAccount' && handoffSnapshot.subjectOverride.namespace?.trim()
                    ? { namespace: handoffSnapshot.subjectOverride.namespace.trim() }
                    : {}),
            }
            : null;
        setBusy(true);
        setError('');
        setAdminHandoff(prev => (prev ? { ...prev, verifying: true, error: '' } : prev));
        let verifyResult = null;
        try {
            verifyResult = await verifyDeploymentBootstrap(buildPermissionPayload({
                namespace: handoffSnapshot.namespace || approval?.namespace || approval?.prepared?.cases?.[0]?.namespace || undefined,
                preparedPlanId: handoffSnapshot.preparedPlanId || approval?.prepared?.id || undefined,
                subjectOverride: override,
                expectedDigest: handoffSnapshot.bootstrapDigest || undefined,
            }));
            if (!mounted.current || version !== requestVersion.current) return;
            if (verifyResult?.verified) {
                const approvedNamespace = verifyResult?.namespace || handoffSnapshot.namespace || approval?.namespace || approval?.prepared?.cases?.[0]?.namespace || '';
                if (approvedNamespace) setApproval(prev => (prev ? { ...prev, namespace: approvedNamespace } : prev));
                setAdminHandoff(prev => (prev ? { ...prev, verifyResult, namespace: approvedNamespace || prev.namespace } : prev));
                const approvedRun = await executeApprove(approvedNamespace, version);
                if (!mounted.current || version !== requestVersion.current) return;
                if (!approvedRun?.deployment_run_id && !approvedRun?.configuration_plan?.validation?.preparedDeployment) {
                    setAdminHandoff(null);
                    setError('Deployment approval did not start an execution run. Try again.');
                }
                return;
            }
            setAdminHandoff(prev => (prev ? {
                ...prev,
                verifying: false,
                verifyResult,
                namespace: verifyResult?.namespace || prev.namespace,
                identity: verifyResult?.identity || prev.identity,
                warnings: Array.isArray(verifyResult?.warnings) ? verifyResult.warnings : prev.warnings,
                error: verifyResult?.message || 'Orchestration administrator namespace setup is not yet verified. Run the script and try again.',
            } : prev));
        } catch (failure) {
            if (!mounted.current || version !== requestVersion.current) return;
            if (isDeploymentAdminSetupError(failure)) {
                const fallbackNs = verifyResult?.namespace || handoffSnapshot.namespace || '';
                const seed = openAdminSetupFromError(failure, fallbackNs, true);
                if (!seed.script && (override || (!seed.identity?.overrideRequired && seed.identity?.kind !== 'unknown'))) {
                    await generateBootstrapScript(override, seed);
                }
                return;
            }
            if (verifyResult?.verified) {
                setAdminHandoff(null);
                setError(failure.message || 'Deployment approval failed.');
                return;
            }
            setAdminHandoff(prev => (prev ? { ...prev, verifying: false, error: failure.message || 'Verification failed.' } : prev));
        } finally {
            if (mounted.current && version === requestVersion.current) setBusy(false);
        }
    };
    const reset = () => { setChangingRequirements(true); setApproval(null); setAdminHandoff(null); setError(''); setEvents([]); };
    const toggleOptimization = (id, enabled) => generate({
        ...requirements,
        requiredFeatures: requirements.requiredFeatures.filter(item => item !== id),
        forbiddenFeatures: enabled ? requirements.forbiddenFeatures.filter(item => item !== id) : [...new Set([...requirements.forbiddenFeatures, id])],
    });
    const toggleRow = async (id, enabled) => {
        const requirement = OPTIMIZATION_CONTROLS.some(([control]) => control === id) ? id : requirementCapability(catalogCapability(id));
        if (requirement) return toggleOptimization(requirement, enabled);
        setFlowToggle({ id: catalogCapability(id), enabled, at: Date.now() });
        return true;
    };
    const plan = run?.configuration_plan;
    const manifest = deploymentPlanManifest(plan);
    const checked = Boolean(manifest) && plan?.validation?.status === 'resources_checked';
    const cleanupRequired = ['interrupted', 'cleanup_failed'].includes(run?.optimization?.status);
    const locked = busy || trialBusy || cleanupRequired || active || !permitted || (run && run.status !== 'awaiting_approval');
    const reviewBlocked = !run ? '' : !permitted ? 'Deployment permission required.'
        : busy ? 'Updating the plan…' : trialBusy ? 'Benchmark tests are starting.'
            : cleanupRequired ? 'Clean up interrupted test resources first.'
                : active ? `Plan is ${run.status === 'optimizing' ? 'running benchmark tests' : 'deploying'}.`
                    : run.status !== 'awaiting_approval' ? `Plan status: ${String(run.status).replaceAll('_', ' ')}.`
                        : editing ? 'Apply or discard your changes first.' : !plan ? 'No configuration generated yet.' : '';
    const enabled = plan?.decisions?.filter(item => ['enabled', 'dependency'].includes(item.status)) || [];
    const sourceReview = Boolean(plan?.source_bundle && plan.source_draft);
    const frozenPlan = approval?.prepared;
    const preflightStatus = frozenPlan?.preflight?.status;
    const summary = run && <>
        {frozenPlan && <section className="space-y-2 text-sm">
            <strong>{preflightStatus === 'unknown' ? 'Deployment not started: scheduling constraints need review' : `Deployment preflight: ${preflightStatus}`}</strong>
            {preflightStatus === 'unknown' && <ul className="list-disc pl-5">{frozenPlan.preflight.unmodeled?.map(reason => <li key={reason}>{reason}</li>)}</ul>}
            <code className="block break-all">{frozenPlan.digest}</code>
            <details><summary>Frozen resources and placement checks</summary><pre className="max-h-80 overflow-auto">{JSON.stringify({ cases: frozenPlan.cases, preflight: frozenPlan.preflight }, null, 2)}</pre></details>
            {preflightStatus === 'fail' && <ul role="alert" className="list-disc pl-5">{frozenPlan.preflight.errors?.map((error, index) => <li key={index}>{error.message || error.code}</li>)}</ul>}
        </section>}
        <DeploymentSummary
            run={run}
            draft={draft}
            deploymentRun={deploymentRun}
            busy={busy}
            locked={locked || approval?.checksum !== plan?.configuration?.checksum}
            deployBlocked={Boolean(frozenPlan) && !['unknown', 'pass'].includes(preflightStatus)}
            editing={editing}
            cleanupRequired={cleanupRequired}
            error={error}
            endpoint={endpoint}
            onEndpointChange={value => {
                setEndpoint(value);
                if (frozenPlan || adminHandoff) {
                    setAdminHandoff(null);
                    if (frozenPlan) {
                        setApproval(prev => ({
                            runId: run.id,
                            checksum: approval.checksum,
                            ...(prev?.namespace ? { namespace: prev.namespace } : {}),
                        }));
                    }
                }
            }}
            onDeploy={deploy}
            confirmLabel={preflightStatus === 'unknown' ? 'Accept risks and deploy' : 'Confirm deployment'}
            onCheck={render}
            onRequirements={reset}
            adminHandoff={adminHandoff}
            onSubjectOverrideChange={(next) => setAdminHandoff(prev => (prev ? { ...prev, subjectOverride: next } : prev))}
            onGenerateBootstrapScript={(overrideArg) => generateBootstrapScript(overrideArg)}
            onVerifyBootstrapAndDeploy={verifyBootstrapAndDeploy}
        />
    </>;
    const reviewFooter = <div className="flex w-full flex-wrap justify-between gap-2">
        <Button variant="ghost" disabled={busy || Boolean(adminHandoff?.verifying) || Boolean(adminHandoff?.generating)} onClick={() => { setApproval(null); setAdminHandoff(null); }}><ArrowLeft size={15} />Back to edit</Button>
        {manifest && <Button variant="secondary" onClick={() => downloadBlob(new Blob([manifest], { type: 'text/yaml' }), `${draft.deploymentName}.yaml`)}><Download size={15} />Download YAML</Button>}
    </div>;
    const content = <section className="space-y-5">
        <WorkspaceHeader title={draft.modelTitle || draft.model?.split('/').at(-1) || 'New deployment'} icon={<Box size={20} />}
            badge={<Badge tone={mode === 'manual' ? 'info' : 'violet'} size="md">{mode === 'manual' ? <SlidersHorizontal size={12} /> : <Sparkles size={12} />}{mode === 'manual' ? 'Manual' : 'Agentic'} plan</Badge>}
            subtitle={<><Server size={12} aria-hidden="true" /><span className="truncate">{draft.clusterName || draft.clusterId}</span>{draft.deploymentName && <><span aria-hidden="true">·</span><span className="truncate font-mono">{draft.deploymentName}</span></>}</>}
            backAction={onBack && <Button variant="ghost" size="sm" aria-label="Change setup" title={trialBusy ? 'Benchmark tests are starting; wait before leaving.' : 'Change setup'} disabled={trialBusy || (busy && Boolean(approval))} onClick={() => { if (editing && !window.confirm('Discard unapplied changes and return to setup?')) return; onBack(run?.deployment_run_id ? null : run); }}><ArrowLeft size={16} /></Button>}
            actions={<>{onRestorePlan && !run?.deployment_run_id && <Button variant="secondary" size="sm" disabled={trialBusy || (busy && Boolean(approval))} onClick={() => { if (editing && !window.confirm('Discard unapplied changes?')) return; onRestorePlan(run); }}><ArrowLeft size={14} />{restoreLabel}</Button>}
                {!building && events.length > 0 && <Button variant="ghost" size="sm" onClick={() => setLogOpen(true)}><ScrollText size={14} />Planning log</Button>}
                {run?.deployment_run_id ? onDeploymentStarted && <Button onClick={() => onDeploymentStarted(run)}>Open deployment</Button> : run && <>
                    {reviewBlocked && <span className="max-w-xs text-xs text-theme-muted" role="status">{reviewBlocked}{editing && <> <Button size="xs" variant="link" onClick={() => { setPrimaryPanel('flow'); document.getElementById('deployment-unapplied')?.scrollIntoView({ behavior: 'smooth', block: 'center' }); }}>Show</Button></>}</span>}
                    <Button id="deployment-review" size="md" disabled={Boolean(reviewBlocked)} title={reviewBlocked || undefined} onClick={() => { setWorkspaceView('review'); setReviewTab(sourceReview ? 'overview' : 'summary'); setError(''); setAdminHandoff(null); setApproval({ runId: run.id, checksum: plan.configuration?.checksum }); }}>Review &amp; deploy<ArrowRight size={15} /></Button></>}</>} />
        {run && !building && <SummaryStrip id="deployment-plan-summary" items={[
            { icon: Layers, tone: 'info', label: 'Topology', value: run.selected_candidate?.prefill_replicas ? 'Prefill / Decode' : 'Combined serving', detail: run.selected_candidate?.prefill_replicas
                ? `Prefill ${run.selected_candidate.prefill_replicas} × TP ${run.selected_candidate.prefill_tensor_parallel_size || 1} · Decode ${run.selected_candidate.replicas || 1} × TP ${run.selected_candidate.tensor_parallel_size || 1}${plan?.source_draft?.runtimeConfiguration?.slices > 1 ? ` · ${plan.source_draft.runtimeConfiguration.slices} slices` : ''}`
                : `${run.selected_candidate?.replicas || 1} serving replicas · TP ${run.selected_candidate?.tensor_parallel_size || 1}` },
            { icon: Cpu, tone: 'brand', label: 'Accelerators', value: `${plan?.resource_estimate?.accelerators ?? '?'} of ${plan?.resource_estimate?.available_accelerators ?? '?'} free`, detail: draft.clusterName || draft.clusterId },
            { icon: Sparkles, tone: 'violet', label: 'Optimizations', value: `${enabled.length} enabled`, detail: mode === 'manual' ? 'Compatible defaults' : `Objective: ${requirements.objective}` },
            { icon: editing ? AlertTriangle : checked ? ShieldCheck : AlertTriangle, tone: editing || !checked ? 'warning' : 'success', label: 'Status', value: editing ? 'Unapplied changes' : checked ? 'Ready to deploy' : 'Checks pending', detail: slaSummary(run) },
        ]} />}
        {error && <div className="space-y-2"><p className="text-sm font-semibold text-theme-text">{run?.deployment_run_id ? 'Deployment update needs attention' : run ? 'Your saved plan is preserved' : 'Unable to prepare the deployment plan'}</p><FormError message={error} /><p className="text-sm text-theme-muted">{run?.deployment_run_id ? 'The deployment may still be running. Check its execution status before taking further action.' : run ? 'Review the message, retry resource checks or adjust the workload and limits. No deployment starts without confirmation.' : 'Check the target and inputs, then build the plan again.'}</p>{mode === 'agentic' && onSwitchToManual && !run?.deployment_run_id && <Button variant="secondary" disabled={busy} onClick={onSwitchToManual}><SlidersHorizontal size={14} />Use Manual plan</Button>}</div>}
        {!permitted && <EmptyState title="Deployment permission required" message="Ask an administrator for deployment-create access to this cluster." />}
        {restoring ? <LoadingState label="Restoring deployment plan" /> : ((!run && ((!autoGenerate && mode === 'agentic') || (!busy && error))) || changingRequirements) && <DeploymentRequirementsForm mode={mode} initialValue={requirements} runtime={run?.request} resourceEstimate={plan?.resource_estimate} onSubmit={generate} disabled={busy || !permitted} aiProviderConfigured={Boolean(provider)}>
            <PlanningProviderSelect provider={provider} providers={providers} onChange={setProvider} />
        </DeploymentRequirementsForm>}
        {changingRequirements && <Button variant="secondary" disabled={busy} onClick={() => { setRequirements(requirementsFromRun(run)); setProvider(run.request.ai_provider_id || ''); setChangingRequirements(false); }}>Keep existing plan</Button>}
        {building && <PlanningProgress events={events} onCancel={!run ? () => abort.current?.abort() : undefined} />}
        <Modal isOpen={logOpen} onClose={() => setLogOpen(false)} title="Planning log" subtitle="Select a step to see what ran and what it reported." size="xl"><PlanningTimeline events={events} /></Modal>
        {run && !changingRequirements && !building && <>
            {run.request.requirements?.workload?.interpretationSource === 'ai_goal' && <p className="text-sm text-theme-muted">AI workload interpretation: shared prompts {run.request.requirements.workload.prefixReuse == null ? 'unknown' : run.request.requirements.workload.prefixReuse ? 'yes' : 'no'}; long inputs with short answers {run.request.requirements.workload.prefillHeavy == null ? 'unknown' : run.request.requirements.workload.prefillHeavy ? 'yes' : 'no'}. Correct these in Deployment settings.</p>}
            {cleanupRequired && <Panel title="Test resources require cleanup"><p role="alert" className="text-sm">Resolve the interrupted or failed cleanup in Real benchmark tests before revising or deploying this plan.</p><Button className="mt-3" variant="secondary" disabled={busy || trialBusy || editing} onClick={() => setWorkspaceView('trials')}>Open test cleanup</Button></Panel>}
            {(() => {
                const views = [
                    ["review", run.deployment_run_id ? 'Submitted plan' : 'Plan', ''],
                    ...(mode === 'agentic' && !run.deployment_run_id && run.candidates.length > 1 ? [["compare", `Alternatives (${run.candidates.length})`, 'Other plans the recommender produced; compare their resources and evidence and switch to one.']] : []),
                    ...(mode === 'agentic' && (!run.deployment_run_id || run.optimization) ? [["trials", 'Benchmark tests', 'Optional: temporarily deploy candidates and measure them on the cluster to pick a winner.']] : []),
                    ...(run.deployment_run_id ? [["execution", 'Deployment execution', 'Live component status of the submitted deployment.']] : [])];
                const blocked = editing ? 'Apply or discard your changes first.' : busy || trialBusy ? 'Wait for the current update to finish.' : '';
                if (views.length === 1) return null;
                return <nav className="flex flex-wrap items-center gap-x-3 gap-y-2" aria-label="Planning workspace">
                    <div className="flex flex-wrap gap-1 rounded-lg border border-theme-border p-1">{views.map(([id, label, hint]) => <Button key={id} size="sm" variant={workspaceView === id ? 'secondary' : 'ghost'} aria-pressed={workspaceView === id} title={blocked || hint || undefined} disabled={Boolean(blocked) && workspaceView !== id} onClick={() => setWorkspaceView(id)}>{label}</Button>)}</div>
                    <span className="text-xs text-theme-muted">{blocked || views.find(([id]) => id === workspaceView)?.[2]}</span>
                </nav>;
            })()}
            <div className="grid min-w-0 items-start gap-5">
            <div className="min-w-0 space-y-4">
            {workspaceView === 'review' && (sourceReview
                ? (() => {
                    const pip = !run.deployment_run_id, flowSmall = pip && primaryPanel === 'optimizations';
                    const table = compact => <OptimizationTable compact={compact} plan={plan} candidate={run.selected_candidate} mode={mode} disabled={locked || editing}
                        onToggle={sourceReview ? toggleRow : toggleOptimization} onOpen={id => { setPrimaryPanel('flow'); setFocus(catalogCapability(id)); }} onExpand={() => setPrimaryPanel('optimizations')} />;
                    return <div className="relative">
                        <div className={flowSmall ? 'relative mb-3 h-44 w-72 overflow-hidden rounded-xl border border-theme-border bg-theme-card shadow-lg ring-1 ring-[color-mix(in_srgb,var(--brand-accent)_25%,transparent)] lg:absolute lg:right-0 lg:top-0 lg:z-10 lg:mb-0' : undefined}>
                            <div inert={flowSmall} style={flowSmall ? { width: '400%', transform: 'scale(0.25)', transformOrigin: 'top left' } : undefined}>
                                <Suspense fallback={<LoadingState label="Loading deployment designer" />}><InferenceDesigner key={`${run.id}:${run.revision}:${plan.configuration?.checksum}`} initialDraft={deploymentEditorSnapshot(run).draft} initialResult={deploymentEditorSnapshot(run).result} clusterName={draft.clusterName || draft.clusterId} disabled={locked} onReview={renderSource} onEditingChange={setEditing} focus={focus} onFocusOpened={() => setFocus('')}
                                    configuredCount={enabled.length} onToggleRequirement={toggleOptimization} toggleRequest={flowToggle} onToggleHandled={() => setFlowToggle(null)} flowOverlay={pip && !flowSmall ? table(true) : null} review={{ open: Boolean(approval), tab: reviewTab, onTab: setReviewTab, onClose: () => { setApproval(null); setAdminHandoff(null); }, overview: summary, footer: reviewFooter }} /></Suspense>
                            </div>
                            {flowSmall && <button type="button" className="group absolute inset-0 flex cursor-zoom-in items-start justify-between bg-gradient-to-b from-[var(--bg-card)] via-transparent to-transparent p-2 text-left" aria-label="Show serving flow in the center" title="Show serving flow" onClick={() => setPrimaryPanel('flow')}>
                                <span className="inline-flex items-center gap-1.5 rounded-md bg-theme-card/90 px-2 py-1 text-xs font-semibold shadow-sm">Serving flow</span>
                                <span className="rounded-md bg-theme-card/90 p-1 text-theme-muted shadow-sm group-hover:text-theme-text"><Maximize2 size={13} aria-hidden="true" /></span>
                            </button>}
                        </div>
                        {flowSmall && <div className="lg:min-h-44 lg:pr-[19.5rem]">{table(false)}</div>}
                    </div>;
                })()
                : <>
                    {!run.deployment_run_id && <OptimizationTable plan={plan} candidate={run.selected_candidate} mode={mode} disabled={locked || editing} onToggle={toggleOptimization} onOpen={id => setFocus(catalogCapability(id))} />}
                    <Panel title="Source-backed Guide editor">
                        <p className="text-sm text-theme-muted">Load this cluster's pinned source catalog to open the 2D optimization flow and edit its parameters. A legacy plan is not evidence that every Guide can be composed.</p>
                        {sourceProfiles.length > 0 && <label className="mt-3 block text-sm">Accelerator profile<Select value={profileId} onChange={event => setProfileId(event.target.value)}><option value="">Choose discovered hardware</option>{sourceProfiles.map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</Select></label>}
                        <Button className="mt-3" disabled={locked || editing} onClick={render}>Load Guides and build source configuration</Button>
                    </Panel>
                    <details><summary className="cursor-pointer text-sm text-theme-muted">Legacy plan preview</summary><DeploymentPlanReview content={manifest} candidate={run.selected_candidate} model={draft.model} mode={mode} decisions={plan?.decisions || []} onEdit={edit} onEditingChange={setEditing} onRequirementsChange={value => generate({ ...requirements, ...value })} onCheck={render} onBenchmark={mode === 'agentic' ? () => setWorkspaceView('trials') : undefined} onWorkload={reset} requirements={requirements} disabled={locked} validation={plan?.validation} revision={run.revision} runtime={run.request} maxAccelerators={plan?.requirements?.hard_constraints?.maxAccelerators} resourceEstimate={plan?.resource_estimate} evidence={candidateEvidence(run)} rationale={run.decision?.rationale} /></details>
                </>)}
            {(run.generator_fallback_reason || run.planner_fallback_reason) && <details className="text-sm text-theme-muted"><summary className="cursor-pointer">Rules were used for part of this recommendation</summary><p role="status" className="mt-2">An AI step was unavailable. Resource checks and confirmation are still required; this is not a performance guarantee.</p><p className="mt-2">{run.generator_fallback_reason || run.planner_fallback_reason}</p></details>}
            {workspaceView === 'compare' && <section aria-label="Candidate comparison" className="space-y-4">
                <div><h3 className="text-lg font-semibold">Compare deployment plans</h3><p className="mt-1 text-sm text-theme-muted">Compare resource needs and evidence, not just the number of optimizations. Selecting a plan automatically reruns its resource checks.</p></div>
                <fieldset disabled={locked || editing} className="grid min-w-0 gap-4 lg:grid-cols-3">
                    {run.candidates.map((item, index) => <Panel key={item.id} title={`Plan ${index + 1}`} className="flex min-w-0 flex-col shadow-none" actions={run.selected_candidate.id === item.id && <Badge tone="info">Selected</Badge>}>
                        <h4 className="text-base font-semibold">{plan?.source_candidates?.[item.id] ? 'Source-composed configuration' : candidateLabel(item)}</h4>
                        <p className="my-4 text-2xl font-semibold">{item.required_gpus} <span className="text-sm font-normal text-theme-muted">accelerators</span></p>
                        <p className="text-sm text-theme-muted">{item.replicas} serving replicas / TP {item.tensor_parallel_size}{item.prefill_replicas ? `; prefill ${item.prefill_replicas} / TP ${item.prefill_tensor_parallel_size}` : ''}</p>
                        {item.native_cpu_cache_gib && <p className="mt-2 text-sm text-theme-muted">CPU KV cache: {item.native_cpu_cache_gib} GiB per replica</p>}
                        <p className="my-4 text-sm">{item.slo_status === 'satisfied' ? 'Targets supported by prediction or history; not verified for this deployment.' : item.slo_status === 'not_satisfied' ? 'Available evidence does not meet your targets.' : 'Performance for your targets is not established.'}</p>
                        <details className="mb-5 text-sm"><summary className="cursor-pointer text-theme-muted">Technical details and evidence</summary>
                            <p className="mt-3 break-all">{plan?.source_candidates?.[item.id] ? 'Identity and optimizations come from the reviewed source resources.' : item.provider_ref}</p>
                            <p className="mt-2 text-theme-muted">{item.performance_estimate_source || 'No performance prediction'}</p>
                            {item.performance_estimate && <dl className="mt-2 space-y-2">{[['TTFT (ms)', 'ttft_ms'], ['TPOT (ms)', 'tpot_ms'], ['Throughput (tokens/s)', 'throughput_tokens_per_sec']].map(([label, field]) => <div key={field}><dt className="inline text-theme-muted">{label}: </dt><dd className="inline">{Number.isFinite(item.performance_estimate[field]) ? item.performance_estimate[field].toFixed(2) : 'Unknown'}</dd></div>)}</dl>}
                            {item.historical_benchmarks?.length > 0 && <ul aria-label="Historical measurements" className="mt-3 space-y-2">{item.historical_benchmarks.map(sample => <li key={sample.benchmark_id}>{historicalBenchmarkLabel(sample)}</li>)}</ul>}
                        </details>
                        <Button variant="secondary" className="mt-auto w-full" disabled={locked || run.selected_candidate.id === item.id} onClick={() => choose(item.id)}>{run.selected_candidate.id === item.id ? 'Selected' : `Review plan ${index + 1}`}</Button>
                    </Panel>)}
                </fieldset>
                {run.rejected_candidates?.length > 0 && <details className="mt-3 text-sm"><summary>Excluded candidates ({run.rejected_candidates.length})</summary><ul className="mt-2 space-y-2">{run.rejected_candidates.map(item => <li key={item.id}>{item.provider_ref}: {item.rejection_reasons.join('; ')}</li>)}</ul></details>}
                <details className="mt-4"><summary className="cursor-pointer text-sm font-semibold">Planning evidence ({run.planning_evidence?.length || 0})</summary>{retrievalBranchSummaries(run).length > 0 && <div className="mt-3 divide-y divide-theme-border rounded border border-theme-border"><RetrievalEvidenceSources run={run} /></div>}<ul className="mt-3 space-y-3">{run.planning_evidence?.map(item => <li key={item.id} className="rounded border border-theme-border p-3 text-sm"><div className="flex flex-wrap gap-2"><strong>{item.source}</strong><Badge tone="neutral">{item.status}</Badge><Badge tone="neutral">{run.decision_evidence_ids?.includes(item.id) ? 'Used in decision' : 'Context'}</Badge></div><p className="mt-1 break-all text-xs text-theme-muted">{item.id}</p></li>)}</ul></details>
                <div className="mt-4 flex justify-end"><Button disabled={locked || editing} onClick={() => setWorkspaceView('review')}>Review selected configuration</Button></div>
            </section>}
            {workspaceView === 'execution' && run.deployment_run_id && <Panel title="Live deployment execution" actions={<Badge tone="neutral">{deploymentRun?.status || 'Loading execution'}</Badge>}>
                {deploymentRun ? <>
                    <dl className="mb-4 grid gap-3 text-sm md:grid-cols-3">{[['Created', deploymentRun.created_at], ['Started', deploymentRun.started_at], ['Finished', deploymentRun.finished_at]].map(([label, value]) => <div key={label}><dt className="text-xs text-theme-muted">{label}</dt><dd>{value ? formatTimestamp(value) : 'Not yet reported'}</dd></div>)}</dl>
                    <div className="overflow-auto"><table className="w-full text-left text-sm"><thead><tr className="border-b border-theme-border"><th className="p-2">Component</th><th className="p-2">Provider</th><th className="p-2">Reported status</th><th className="p-2">Attempt</th><th className="p-2">Diagnostics</th></tr></thead><tbody>{deploymentRun.cases?.map(item => <tr key={item.id} className="border-b border-theme-border"><td className="p-2">{item.component}</td><td className="p-2">{item.provider_ref}</td><td className="p-2"><Badge tone={item.status === 'ready' ? 'success' : item.status === 'failed' ? 'danger' : 'neutral'}>{item.status}</Badge></td><td className="p-2">{item.attempt}</td><td className="p-2">{item.failure && <p role="alert">{item.failure.title}: {item.failure.detail}</p>}{item.execution_id ? <Button size="sm" variant="secondary" onClick={() => setLogsDeployment({ execution_id: item.execution_id, name: run.request.deployment_name, cluster_id: draft.clusterId })}>View Pods &amp; Logs</Button> : <span className="text-theme-muted">Waiting for execution record</span>}</td></tr>)}</tbody></table></div>
                    <p className="mt-3 text-xs text-theme-muted">States are reported by Deploy and refreshed automatically. Readiness does not establish workload performance. Open the deployment to monitor metrics or run Evaluate.</p>
                </> : <LoadingState label="Reading deployment execution" />}
            </Panel>}
            {mode === 'agentic' && workspaceView === 'trials' && !run.request.execution_policy.allow_trials && !run.optimization && <Panel title="Optional benchmark tests">
                <p className="text-sm text-theme-muted">Prepare a test-enabled plan, then choose the workload and resource budget. Tests require a separate confirmation before using cluster resources.</p>
                <Button className="mt-3" disabled={locked || editing} onClick={async () => { if (await generate({ ...requirements, allowTrial: true })) setWorkspaceView('trials'); }}>Prepare benchmark plan</Button>
            </Panel>}
            {mode === 'agentic' && (run.request.execution_policy.allow_trials || run.optimization) && <div hidden={workspaceView !== 'trials'}><MeasuredOptimizationControls key={run.id} onBusyChange={setTrialBusy} run={run} onUpdate={value => { requestVersion.current += 1; accept(value); }} onReview={choose} onCheckCandidates={async () => { try { await renderSource({ compare: true }); } catch { /* Workflow reports source errors. */ } }} disabled={busy || !permitted || editing} /></div>}
            </div>

            </div>
        </>}
        <Modal isOpen={Boolean(approval) && !sourceReview} onClose={() => { setApproval(null); setAdminHandoff(null); }} closeOnBackdrop={!busy && !adminHandoff?.verifying && !adminHandoff?.generating} closeOnEscape={!busy && !adminHandoff?.verifying && !adminHandoff?.generating} title="Review & deploy" size="xxl" footer={reviewFooter}>
            {run && <div className="space-y-4">
                <div role="tablist" aria-label="Review views" className="deployment-tabs">
                    {[['summary', 'Overview'], ['yaml', 'YAML']].map(([id, label]) => <Button key={id} role="tab" size="sm" variant="ghost" aria-selected={reviewTab === id} onClick={() => setReviewTab(id)}>{label}</Button>)}
                </div>
                {reviewTab === 'summary' ? summary : <pre className="max-h-[60vh] overflow-auto rounded-lg bg-theme-bg p-4 text-xs">{manifest || 'Generate and check the source configuration first.'}</pre>}
            </div>}
        </Modal>
        {logsDeployment && <DeploymentPodsLogsModal deployment={logsDeployment} onClose={() => setLogsDeployment(null)} />}
    </section>;
    return embedded ? content : <ModulePage>{content}</ModulePage>;
}

export default function AgenticDeploymentWorkspace(props) {
    return <DeploymentWorkflow {...props} mode="agentic" />;
}
