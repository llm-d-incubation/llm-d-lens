// Orchestration administrator operation handoff for the Create Cluster wizard and day-2
// recovery. The panel consumes OperationReport and AdminHandoffStatusResponse,
// shows only the Orchestration administrator operation script/marker, and never uploads a kubeconfig.
// See docs/design/cluster-permission-preflight-design.md sections 4.5/4.6/8.

import React, { useEffect, useState } from 'react';
import {
    AlertTriangle,
    Check,
    CheckCircle2,
    ChevronRight,
    ClipboardCopy,
    Download,
    RefreshCw,
    ShieldAlert,
    XCircle,
} from 'lucide-react';
import { Badge, Button, Checkbox, Panel, Spinner } from '../../../shared/ui';
import { FormError } from '../../../shared/ui/FormError';
import { cn } from '../../../shared/lib/cn';
import { copyText } from '../../../shared/lib/clipboard';
import { downloadBlob } from '../../../shared/lib/download';

function ruleLabel(rule = {}) {
    const verb = rule.verb || 'use';
    const resource = rule.resource || rule.path || rule.nonResourceURL || 'resource';
    const group = rule.apiGroup || rule.group;
    const target = group ? `${resource}.${group}` : resource;
    const subresource = rule.subresource ? `/${rule.subresource}` : '';
    const scope = rule.namespace ? ` in ${rule.namespace}` : '';
    return `${verb} ${target}${subresource}${scope}`.trim();
}

function operationId(entry) {
    return typeof entry === 'string' ? entry : entry?.id;
}

function operationTitle(entry) {
    if (typeof entry === 'string') return entry;
    return entry?.title || entry?.id || 'Operation';
}

function MissingRules({ rules }) {
    if (!rules.length) return null;
    return (
        <ul className="mt-1.5 space-y-1" aria-label="Missing Kubernetes permissions">
            {rules.map((rule, index) => (
                <li key={`${rule.operationId || rule.resource || 'rule'}-${index}`} className="break-all font-mono text-[11px] text-theme-muted">
                    {ruleLabel(rule)}
                </li>
            ))}
        </ul>
    );
}

function OperationRow({ entry, done = false, tone, statusLabel, ariaLabel, rules, children }) {
    const id = operationId(entry);
    const title = operationTitle(entry);
    const resolvedTone = tone || (done ? 'success' : 'danger');
    const Icon = resolvedTone === 'success' ? CheckCircle2 : resolvedTone === 'warning' ? AlertTriangle : XCircle;
    return (
        <li className={cn(
            'border-l-2 pl-3',
            resolvedTone === 'success'
                ? 'border-emerald-500/70'
                : resolvedTone === 'warning'
                    ? 'border-amber-500/80'
                    : 'border-rose-500/80',
        )}>
            <details className="group">
                <summary
                    title="Expand for details"
                    className="-ml-3 flex cursor-pointer list-none items-start gap-2 py-1 pl-3 pr-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60 [&::-webkit-details-marker]:hidden"
                >
                    <Icon
                        size={14}
                        role="img"
                        aria-label={ariaLabel}
                        className={cn(
                            'mt-0.5 shrink-0',
                            resolvedTone === 'success'
                                ? 'text-emerald-600 dark:text-emerald-400'
                                : resolvedTone === 'warning'
                                    ? 'text-amber-600 dark:text-amber-400'
                                    : 'text-rose-600 dark:text-rose-400',
                        )}
                    />
                    <span className="min-w-0 flex-1">
                        <span className="block text-xs font-medium text-theme-text">{title}</span>
                        <span className="block break-all font-mono text-[11px] text-theme-muted">{id}</span>
                    </span>
                    <span className={cn(
                        'shrink-0 text-[11px] font-medium',
                        resolvedTone === 'success'
                            ? 'text-emerald-700 dark:text-emerald-300'
                            : resolvedTone === 'warning'
                                ? 'text-amber-700 dark:text-amber-300'
                                : 'text-rose-700 dark:text-rose-300',
                    )}>
                        {statusLabel}
                    </span>
                    <ChevronRight
                        size={13}
                        aria-hidden="true"
                        className="mt-0.5 shrink-0 text-theme-muted transition-transform motion-reduce:transition-none group-open:rotate-90"
                    />
                </summary>
                <div className="pb-2">
                    <MissingRules rules={rules} />
                    {children}
                </div>
            </details>
        </li>
    );
}

function useScriptActions({ script, filename, defaultFilename, onDownloadScript, onCopyScript }) {
    const [copied, setCopied] = useState(false);
    const [localError, setLocalError] = useState('');

    const downloadScript = () => {
        try {
            setLocalError('');
            if (onDownloadScript) onDownloadScript(script, filename);
            else downloadBlob(new Blob([script], { type: 'text/x-shellscript' }), filename || defaultFilename);
        } catch {
            setLocalError('Could not download the script. Copy it instead.');
        }
    };

    const copyScript = async () => {
        setLocalError('');
        try {
            if (onCopyScript) await onCopyScript(script);
            else await copyText(script);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch {
            setLocalError('Could not copy the script. Download it instead.');
        }
    };

    return { copied, localError, downloadScript, copyScript };
}

export function PermissionHandoffPanel({
    report,
    handoff,
    script = '',
    filename,
    instructions,
    warning,
    onLoadScript,
    onDownloadScript,
    onCopyScript,
    onRecheck,
    onEditSecretNamespace,
    busy = false,
    error,
    hasToken = false,
    canExecute,
}) {
    const { copied, localError, downloadScript, copyScript } = useScriptActions({
        script,
        filename,
        defaultFilename: 'lens-admin-operations.sh',
        onDownloadScript,
        onCopyScript,
    });

    const status = handoff?.status || report?.status || 'admin_required';
    const blocked = status === 'blocked' || (report?.blockedOperations || []).length > 0;
    const reportAdminById = new Map((report?.adminOperations || []).map((entry) => [operationId(entry), entry]));
    const adminOperations = handoff?.adminOperations?.length
        ? handoff.adminOperations.map((entry) => reportAdminById.get(operationId(entry)) || entry)
        : report?.adminOperations || [];
    const userOperations = report?.userOperations || [];
    const blockedOperations = report?.blockedOperations || [];
    const showAdmin = adminOperations.length > 0;
    const hasHandoff = Boolean(handoff);
    const remaining = new Set(handoff?.remainingOperations || []);
    const marker = handoff?.marker || report?.marker || {};
    const adminReady = hasHandoff && status === 'complete' && remaining.size === 0;
    const markerUnconfirmed = !adminReady && (marker.status === 'unconfirmed' || status === 'unconfirmed');
    const markerStale = !adminReady && (marker.status === 'stale' || status === 'stale');
    const markerPartial = hasHandoff && !adminReady && !markerUnconfirmed && !markerStale && (status === 'partial' || marker.status === 'partial');
    const missing = Array.isArray(report?.missing) ? report.missing : [];
    const userGateOpen = adminReady && hasToken;
    const executeAllowed = canExecute === undefined ? (userGateOpen && !blocked) : Boolean(canExecute);
    const adminDoneCount = hasHandoff
        ? adminOperations.filter((entry) => !remaining.has(operationId(entry))).length
        : 0;
    const blockedHf = blockedOperations.find((entry) => operationId(entry) === 'cache.hf-secret');
    const blockedHfMissing = Array.isArray(blockedHf?.missing) ? blockedHf.missing : [];
    const blockedHfNamespace = blockedHf?.namespace
        || blockedHfMissing.find((rule) => rule?.resource === 'secrets' && rule?.namespace)?.namespace
        || blockedHfMissing.find((rule) => rule?.namespace)?.namespace
        || missing.find((rule) => rule?.operationId === 'cache.hf-secret' && rule?.resource === 'secrets' && rule?.namespace)?.namespace
        || missing.find((rule) => rule?.operationId === 'cache.hf-secret' && rule?.namespace)?.namespace
        || 'the target namespace';
    const nonHfBlockedCount = blockedOperations.filter((entry) => operationId(entry) !== 'cache.hf-secret').length;
    const hasBlockedLens = blockedOperations.some((entry) => entry?.executor === 'lens');
    const noOperations = !showAdmin && userOperations.length === 0 && blockedOperations.length === 0;
    const shouldExpandDetails = blocked || (showAdmin && !userGateOpen);
    const [detailsOpen, setDetailsOpen] = useState(shouldExpandDetails);
    useEffect(() => {
        setDetailsOpen(shouldExpandDetails);
    }, [shouldExpandDetails]);
    const handleDetailsToggle = (event) => {
        if (event.target !== event.currentTarget) return;
        setDetailsOpen(event.currentTarget.open);
    };
    const handoffWarnings = adminReady && Array.isArray(handoff?.warnings)
        ? handoff.warnings.filter((item) => Boolean(item) && item !== warning)
        : [];
    const monitoring = handoff?.monitoring && typeof handoff.monitoring === 'object'
        ? handoff.monitoring
        : null;
    const monitoringPrometheusUrl = monitoring?.prometheusUrl ?? monitoring?.prometheus_url ?? null;
    const monitoringGrafanaUrl = monitoring?.grafanaUrl ?? monitoring?.grafana_url ?? null;
    const monitoringPrometheusNodePort = monitoring?.prometheusNodePort ?? monitoring?.prometheus_node_port ?? null;
    const monitoringGrafanaNodePort = monitoring?.grafanaNodePort ?? monitoring?.grafana_node_port ?? null;
    const monitoringHost = monitoring?.host ?? monitoring?.monitoringHost ?? null;
    const showMonitoringEndpoints = Boolean(
        monitoring
        && (
            monitoringPrometheusUrl
            || monitoringGrafanaUrl
            || monitoringPrometheusNodePort
            || monitoringGrafanaNodePort
            || monitoringHost
            || monitoring.checked
            || typeof monitoring.healthy === 'boolean'
        ),
    );
    const monitoringBadge = monitoring?.healthy === true
        ? { tone: 'success', label: 'Healthy' }
        : monitoring?.healthy === false
            ? { tone: 'warning', label: 'Unreachable' }
            : { tone: 'info', label: 'Discovered' };
    const rulesFor = (id) => missing.filter((rule) => !rule.operationId || rule.operationId === id);

    const detailParts = [];
    if (showAdmin) {
        if (adminReady) detailParts.push(`${adminOperations.length}/${adminOperations.length} Orchestration administrator done`);
        else if (markerUnconfirmed) detailParts.push(`${adminOperations.length} unconfirmed`);
        else if (hasHandoff) detailParts.push(`${adminDoneCount}/${adminOperations.length} Orchestration administrator done`);
        else detailParts.push(`${adminOperations.length} Orchestration administrator`);
    }
    if (blockedOperations.length > 0) {
        detailParts.push(`${blockedOperations.length} blocked`);
    }
    if (userOperations.length > 0) {
        detailParts.push(
            userGateOpen || (!showAdmin && !blocked)
                ? `${userOperations.length} ready`
                : `${userOperations.length} user`,
        );
    }
    const detailSummaryLabel = detailParts.join(' · ');

    const title = blocked ? 'Your access needs attention' : 'Orchestration administrator operations';
    const badge = blocked
        ? 'Blocked'
        : adminReady
            ? 'Verified'
            : markerUnconfirmed
                ? 'Unconfirmed'
                : markerStale
                    ? 'Stale'
                    : 'Awaiting Orchestration administrator';
    const badgeTone = blocked
        ? 'danger'
        : adminReady
            ? 'success'
            : markerStale
                ? 'danger'
                : 'warning';

    return (
        <Panel
            padding="sm"
            tone="soft"
            className={cn(
                'border-l-4 shadow-none',
                blocked
                    ? 'border-l-rose-500'
                    : userGateOpen
                        ? 'border-l-emerald-500'
                        : 'border-l-amber-500',
            )}
            title={(
                <span className="flex items-center gap-2">
                    <ShieldAlert
                        size={15}
                        className={cn(
                            blocked
                                ? 'text-rose-600 dark:text-rose-400'
                                : userGateOpen
                                    ? 'text-emerald-600 dark:text-emerald-400'
                                    : 'text-amber-600 dark:text-amber-400',
                        )}
                        aria-hidden="true"
                    />
                    {title}
                </span>
            )}
            actions={<Badge tone={badgeTone} size="xs">{badge}</Badge>}
        >
            <div className="space-y-3" role="status" aria-live="polite">
                {showAdmin && adminReady && blocked && (
                    <p className="flex items-center gap-2 border-l-2 border-emerald-500 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-800 dark:text-emerald-200">
                        <CheckCircle2 size={14} className="shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                        <span>
                            Orchestration administrator operations verified ({adminOperations.length}/{adminOperations.length} done); {hasBlockedLens ? 'blocked access below still needs attention.' : 'user-owned secret access below still needs attention.'}
                        </span>
                    </p>
                )}

                {blocked && blockedHf && (
                    <div className="space-y-2 border-l-2 border-rose-500 bg-rose-500/10 px-3 py-2.5 text-xs leading-relaxed text-rose-800 dark:text-rose-200">
                        <p>
                            <span className="font-semibold">cache.hf-secret:</span> Lens executes this with your edit identity, not manual kubectl.
                            Your edit ServiceAccount lacks <span className="font-mono">secrets</span> access in <span className="font-mono">{blockedHfNamespace}</span> — choose an existing namespace you can write, or ask an Orchestration administrator to grant <span className="font-mono">secrets</span> access there, then click <span className="font-semibold">Re-check</span>.
                            This operation is never included in the Orchestration administrator script.
                        </p>
                        {onEditSecretNamespace && (
                            <div>
                                <Button variant="outline" size="sm" onClick={onEditSecretNamespace} disabled={busy}>
                                    Change secret namespace
                                </Button>
                            </div>
                        )}
                    </div>
                )}

                {blocked && (!blockedHf || nonHfBlockedCount > 0) && (
                    <p className="border-l-2 border-rose-500 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-800 dark:text-rose-200">
                        {hasBlockedLens ? (
                            <>
                                Fix the blocked access ({blockedOperations.length} operation(s)) or adjust the plan, then click <span className="font-semibold">Re-check</span>. These operations cannot be run in the Orchestration administrator script.
                            </>
                        ) : (
                            <>
                                Fix the blocked user-owned access ({blockedOperations.length} operation(s)), then click <span className="font-semibold">Re-check</span>. Do not send secret operations to an Orchestration administrator.
                            </>
                        )}
                    </p>
                )}

                {warning && (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">{warning}</span>
                    </p>
                )}

                {handoffWarnings.map((item, index) => (
                    <p
                        key={`${index}-${item}`}
                        className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
                    >
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">{item}</span>
                    </p>
                ))}

                {showAdmin && !userGateOpen && markerUnconfirmed && (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            Lens received <span className="font-mono">403 Forbidden</span> reading ConfigMap <span className="font-mono">{marker.name || 'lens-admin-handoff'}</span> in namespace <span className="font-mono">{marker.namespace || 'default'}</span> with your edit kubeconfig, so Orchestration administrator completion cannot be confirmed. Use a readable namespace or ask an Orchestration administrator to grant ConfigMap <span className="font-mono">get</span> access in <span className="font-mono">{marker.namespace || 'default'}</span>, then click <span className="font-semibold">Re-check</span>.
                        </span>
                    </p>
                )}

                {showAdmin && !userGateOpen && markerStale && (
                    <p className="flex items-start gap-2 border-l-2 border-rose-500 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-800 dark:text-rose-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            The completion marker belongs to an older plan. Send the updated Orchestration administrator script below, then click <span className="font-semibold">Re-check</span>.
                        </span>
                    </p>
                )}

                {showAdmin && !userGateOpen && markerPartial && (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            Orchestration administrator handoff is partial ({adminDoneCount}/{adminOperations.length} done). Re-run the Orchestration administrator script for the remaining operation(s), then click <span className="font-semibold">Re-check</span>.
                        </span>
                    </p>
                )}

                {showAdmin && !userGateOpen && !adminReady && (script || onLoadScript) && (
                    <div className="space-y-2 rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2.5">
                        <p className="text-xs font-semibold text-theme-text">Orchestration administrator script</p>
                        <p className="max-w-[68ch] text-xs leading-relaxed text-theme-muted">
                            {instructions || 'Send this idempotent script to an Orchestration administrator to run once, then click Re-check (contains no kubeconfig or user secret).'}
                        </p>
                        <div className="flex flex-wrap gap-2">
                            {script ? (
                                <>
                                    <Button variant="outline" size="sm" onClick={downloadScript} disabled={busy}>
                                        <Download size={13} aria-hidden="true" /> Download
                                    </Button>
                                    <Button variant="outline" size="sm" onClick={copyScript} disabled={busy}>
                                        {copied
                                            ? <Check size={13} className="text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                                            : <ClipboardCopy size={13} aria-hidden="true" />}
                                        {copied ? 'Copied' : 'Copy'}
                                    </Button>
                                </>
                            ) : (
                                <Button variant="outline" size="sm" onClick={() => onLoadScript?.()} isLoading={busy && Boolean(onLoadScript)} disabled={!onLoadScript || busy}>
                                    <Download size={13} aria-hidden="true" /> Generate script
                                </Button>
                            )}
                        </div>
                    </div>
                )}

                {showAdmin && !userGateOpen && !adminReady && !markerUnconfirmed && !markerStale && !markerPartial && !(script || onLoadScript) && (
                    <p className="max-w-[68ch] text-xs leading-relaxed text-theme-muted">
                        Ask an Orchestration administrator to run the Orchestration administrator script, then click Re-check to verify completion.
                    </p>
                )}

                {showAdmin && adminReady && !hasToken && (
                    <p className="border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        Orchestration administrator marker verified. Click <span className="font-semibold">Re-check</span> to acquire the execution token before running user operations.
                    </p>
                )}

                {userGateOpen && !blocked && (
                    <p className="flex items-center gap-2 border-l-2 border-emerald-500 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-800 dark:text-emerald-200">
                        <CheckCircle2 size={14} className="shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                        <span>
                            Orchestration administrator marker verified ({adminOperations.length}/{adminOperations.length} done) and execution token ready.
                            {executeAllowed && (
                                <> Click <span className="font-semibold">Execute</span> below to continue.</>
                            )}
                        </span>
                    </p>
                )}

                {noOperations && (
                    <p className="text-xs text-theme-muted">No operations required.</p>
                )}

                {showMonitoringEndpoints && (
                    <div className="space-y-2 rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2.5">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <p className="text-xs font-semibold text-theme-text">Monitoring endpoints</p>
                            <Badge tone={monitoringBadge.tone} size="xs">{monitoringBadge.label}</Badge>
                        </div>
                        <dl className="grid gap-1 text-[11px] sm:grid-cols-[6rem_1fr]">
                            <dt className="text-theme-muted">Prometheus</dt>
                            <dd className="break-all font-mono text-theme-text">
                                {monitoringPrometheusUrl
                                    || (monitoringPrometheusNodePort ? `NodePort ${monitoringPrometheusNodePort}` : 'Not reported')}
                            </dd>
                            <dt className="text-theme-muted">Grafana</dt>
                            <dd className="break-all font-mono text-theme-text">
                                {monitoringGrafanaUrl
                                    || (monitoringGrafanaNodePort ? `NodePort ${monitoringGrafanaNodePort}` : 'Not reported')}
                            </dd>
                            {monitoringHost && (
                                <>
                                    <dt className="text-theme-muted">Host</dt>
                                    <dd className="break-all font-mono text-theme-text">{monitoringHost}</dd>
                                </>
                            )}
                        </dl>
                    </div>
                )}

                <div className="flex flex-wrap gap-2 border-t border-theme-border pt-3">
                    <Button
                        variant={blocked || (showAdmin && !userGateOpen) ? 'sky' : 'ghost'}
                        size="sm"
                        onClick={onRecheck}
                        disabled={busy || !onRecheck}
                    >
                        <RefreshCw size={13} aria-hidden="true" /> Re-check
                    </Button>
                </div>

                <FormError message={error || localError} />

                {busy && (
                    <p className="flex items-center gap-2 text-[11px] text-theme-muted">
                        <Spinner size="xs" /> Working…
                    </p>
                )}

                {!noOperations && (
                    <details
                        className="group/details border-t border-theme-border pt-3"
                        open={detailsOpen}
                        onToggle={handleDetailsToggle}
                    >
                        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-xs font-medium text-theme-muted hover:text-theme-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60 [&::-webkit-details-marker]:hidden">
                            <span className="flex items-center gap-1.5">
                                <ChevronRight
                                    size={13}
                                    aria-hidden="true"
                                    className="shrink-0 transition-transform motion-reduce:transition-none group-open/details:rotate-90"
                                />
                                View operation details
                            </span>
                            <span className="font-mono text-[11px]">{detailSummaryLabel}</span>
                        </summary>

                        <div className="mt-3 space-y-4">
                            {showAdmin && (
                                <section className="space-y-3" aria-labelledby="handoff-admin-heading">
                                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                                        <h3 id="handoff-admin-heading" className="text-xs font-semibold text-theme-text">Orchestration administrator operations</h3>
                                        <span className={cn(
                                            'text-[11px] font-medium',
                                            adminReady
                                                ? 'text-emerald-700 dark:text-emerald-300'
                                                : markerUnconfirmed
                                                    ? 'text-amber-700 dark:text-amber-300'
                                                    : 'text-rose-700 dark:text-rose-300',
                                        )}>
                                            {adminReady
                                                ? 'Verified'
                                                : markerUnconfirmed
                                                    ? 'Unconfirmed'
                                                    : markerStale
                                                        ? 'Stale'
                                                        : hasHandoff
                                                            ? 'Waiting for marker'
                                                            : 'Not verified'}
                                        </span>
                                    </div>

                                    <ul className="space-y-1">
                                        {adminOperations.map((entry, index) => {
                                            const id = operationId(entry);
                                            const waiting = !hasHandoff || remaining.has(id);
                                            const rowUnconfirmed = !adminReady && markerUnconfirmed;
                                            return (
                                                <OperationRow
                                                    key={`${id || index}-admin`}
                                                    entry={entry}
                                                    done={!rowUnconfirmed && !waiting}
                                                    tone={rowUnconfirmed ? 'warning' : (!waiting ? 'success' : 'danger')}
                                                    statusLabel={
                                                        rowUnconfirmed
                                                            ? 'Unconfirmed'
                                                            : !hasHandoff
                                                                ? 'Not verified'
                                                                : waiting
                                                                    ? 'Remaining'
                                                                    : 'Done'
                                                    }
                                                    ariaLabel={
                                                        rowUnconfirmed
                                                            ? 'Unconfirmed'
                                                            : !hasHandoff
                                                                ? 'Needs attention'
                                                                : waiting
                                                                    ? 'Remaining'
                                                                    : 'Done'
                                                    }
                                                    rules={rulesFor(id)}
                                                >
                                                    <p className="text-[11px] leading-relaxed text-theme-muted">
                                                        {rowUnconfirmed
                                                            ? `Completion cannot be confirmed because your edit identity cannot read ConfigMaps in ${marker.namespace || 'default'}.`
                                                            : waiting
                                                                ? 'Run the Orchestration administrator script, then click Re-check to verify completion.'
                                                                : 'Completed by the Orchestration administrator.'}
                                                    </p>
                                                </OperationRow>
                                            );
                                        })}
                                    </ul>
                                </section>
                            )}

                            {(userOperations.length > 0 || blockedOperations.length > 0) && (
                                <section className="space-y-3" aria-labelledby="handoff-user-heading">
                                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                                        <h3 id="handoff-user-heading" className="text-xs font-semibold text-theme-text">Your operations</h3>
                                        <span className={cn(
                                            'text-[11px] font-medium',
                                            blocked
                                                ? 'text-rose-700 dark:text-rose-300'
                                                : userGateOpen || !showAdmin
                                                    ? 'text-emerald-700 dark:text-emerald-300'
                                                    : 'text-amber-700 dark:text-amber-300',
                                        )}>
                                            {blocked
                                                ? 'Blocked'
                                                : userGateOpen || !showAdmin
                                                    ? 'Ready'
                                                    : adminReady
                                                        ? 'Waiting for verification'
                                                        : 'Waiting for Orchestration administrator'}
                                        </span>
                                    </div>

                                    {showAdmin && !userGateOpen && (
                                        <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-800 dark:text-amber-200">
                                            <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                                            <span className="min-w-0 flex-1">
                                                {adminReady
                                                    ? 'Orchestration administrator marker verified. Click Re-check to acquire the execution token before running your operations.'
                                                    : 'Wait for the Orchestration administrator marker, then click Re-check. Your operations stay disabled until the Orchestration administrator handoff is verified and a token is available.'}
                                            </span>
                                        </p>
                                    )}

                                    <ul className="space-y-1">
                                        {blockedOperations.map((entry, index) => {
                                            const id = operationId(entry);
                                            return (
                                                <OperationRow
                                                    key={`${id || index}-blocked`}
                                                    entry={entry}
                                                    done={false}
                                                    statusLabel="Blocked"
                                                    ariaLabel="Blocked"
                                                    rules={rulesFor(id)}
                                                >
                                                    <p className="text-[11px] leading-relaxed text-theme-muted">
                                                        {entry?.executor === 'lens'
                                                            ? 'Lens runs this operation with your kubeconfig during Complete. Grant the required access (or adjust the plan), then re-check. It cannot be run in the Orchestration administrator script.'
                                                            : 'Fix this user-owned access, then re-check. Do not send it to an Orchestration administrator.'}
                                                    </p>
                                                </OperationRow>
                                            );
                                        })}
                                        {userOperations.map((entry, index) => {
                                            const id = operationId(entry);
                                            return (
                                                <OperationRow
                                                    key={`${id || index}-user`}
                                                    entry={entry}
                                                    done
                                                    statusLabel="Ready"
                                                    ariaLabel="Ready"
                                                    rules={rulesFor(id)}
                                                >
                                                    <p className="text-[11px] leading-relaxed text-theme-muted">
                                                        Your edit identity can run this operation.
                                                    </p>
                                                </OperationRow>
                                            );
                                        })}
                                    </ul>
                                </section>
                            )}
                        </div>
                    </details>
                )}
            </div>
        </Panel>
    );
}

function cleanupStatusMeta(unreachable, markerStatus) {
    if (unreachable) return { badge: 'Unreachable', tone: 'danger' };
    switch (markerStatus) {
        case 'unconfirmed':
            return { badge: 'Unconfirmed', tone: 'warning' };
        case 'stale':
            return { badge: 'Stale', tone: 'danger' };
        case 'partial':
            return { badge: 'Partial', tone: 'warning' };
        case 'still_present':
            return { badge: 'Still present', tone: 'warning' };
        default:
            return { badge: 'Awaiting Orchestration administrator', tone: 'warning' };
    }
}

function cleanupTargetLabel(entry = {}) {
    const kind = entry.kind || 'resource';
    const target = entry.namespace ? `${entry.namespace}/${entry.name}` : entry.name;
    return target ? `${kind}: ${target}` : kind;
}

export function CleanupHandoffPanel({
    handoff,
    confirmed = false,
    onConfirmChange,
    onConfirmDelete,
    onDownloadScript,
    onCopyScript,
    busy = false,
    error = '',
}) {
    const script = typeof handoff?.script === 'string' ? handoff.script : '';
    const filename = handoff?.scriptFilename || 'lens-admin-cleanup.sh';
    const { copied, localError, downloadScript, copyScript } = useScriptActions({
        script,
        filename,
        defaultFilename: 'lens-admin-cleanup.sh',
        onDownloadScript,
        onCopyScript,
    });

    const marker = handoff?.marker || {};
    const unreachable = Boolean(handoff?.unreachable);
    const confirmRequired = Boolean(handoff?.confirmRequired || unreachable);
    const canVerifyMarker = Boolean(handoff?.canVerifyMarker);
    const markerStatus = marker.status || (!unreachable && confirmRequired && !canVerifyMarker ? 'unconfirmed' : 'missing');
    const operations = Array.isArray(handoff?.operations) ? handoff.operations : [];
    const completedOperations = Array.isArray(handoff?.completedOperations) ? handoff.completedOperations : [];
    const warnings = Array.isArray(handoff?.warnings) ? handoff.warnings : [];
    const statusMeta = cleanupStatusMeta(unreachable, markerStatus);
    const isDanger = unreachable || markerStatus === 'stale';
    const detailParts = [`${operations.length} remaining`];
    if (completedOperations.length > 0) detailParts.push(`${completedOperations.length} cleaned`);
    const detailSummaryLabel = detailParts.join(' · ');
    const [detailsOpen, setDetailsOpen] = useState(true);
    const handleDetailsToggle = (event) => {
        if (event.target !== event.currentTarget) return;
        setDetailsOpen(event.currentTarget.open);
    };

    return (
        <Panel
            padding="sm"
            tone="soft"
            className={cn(
                'border-l-4 shadow-none',
                isDanger ? 'border-l-rose-500' : 'border-l-amber-500',
            )}
            title={(
                <span className="flex items-center gap-2">
                    <ShieldAlert
                        size={15}
                        className={cn(
                            isDanger
                                ? 'text-rose-600 dark:text-rose-400'
                                : 'text-amber-600 dark:text-amber-400',
                        )}
                        aria-hidden="true"
                    />
                    {unreachable ? 'Cluster unreachable during cleanup' : 'Orchestration administrator cleanup required'}
                </span>
            )}
            actions={<Badge tone={statusMeta.tone} size="xs">{statusMeta.badge}</Badge>}
        >
            <div className="space-y-3" role="status" aria-live="polite">
                {unreachable ? (
                    <p className="border-l-2 border-rose-500 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-800 dark:text-rose-200">
                        {handoff?.detail || 'Lens could not reach the cluster to finish cleanup.'} Run the offline script below when cluster access is available, or confirm below to delete only the Lens registration record.
                    </p>
                ) : markerStatus === 'unconfirmed' ? (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            {marker.namespace ? (
                                <>
                                    Lens received <span className="font-mono">403 Forbidden</span> reading cleanup marker ConfigMap <span className="font-mono">{marker.name || 'lens-admin-cleanup'}</span> in namespace <span className="font-mono">{marker.namespace}</span> with your edit kubeconfig, so Orchestration administrator cleanup cannot be confirmed automatically. Run the script below or confirm deletion once cleanup is complete.
                                </>
                            ) : (
                                <>
                                    {handoff?.detail || 'No readable marker namespace is available to confirm Orchestration administrator cleanup automatically.'} Ask an Orchestration administrator to run the script below, then confirm deletion below once cleanup is complete.
                                </>
                            )}
                        </span>
                    </p>
                ) : markerStatus === 'stale' ? (
                    <p className="flex items-start gap-2 border-l-2 border-rose-500 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-800 dark:text-rose-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            The cleanup marker belongs to an outdated cleanup inventory. Ask an Orchestration administrator to run the updated script below, then click <span className="font-semibold">Re-check &amp; Delete</span>.
                        </span>
                    </p>
                ) : markerStatus === 'partial' ? (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            Orchestration administrator cleanup is partial. Re-run the cleanup script below for the remaining resource(s), then click <span className="font-semibold">Re-check &amp; Delete</span>.
                        </span>
                    </p>
                ) : markerStatus === 'still_present' ? (
                    <p className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">
                            Target cluster resources are still present. Ask an Orchestration administrator to run the cleanup script below, then click <span className="font-semibold">Re-check &amp; Delete</span>.
                        </span>
                    </p>
                ) : (
                    <p className="max-w-[68ch] text-xs leading-relaxed text-theme-muted">
                        {handoff?.detail || 'Your edit kubeconfig cannot delete the remaining cluster resources. Ask an Orchestration administrator to run the cleanup script below once, then click Re-check & Delete.'}
                    </p>
                )}

                {warnings.map((item, index) => (
                    <p
                        key={`${index}-${item}`}
                        className="flex items-start gap-2 border-l-2 border-amber-500 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
                    >
                        <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 flex-1">{item}</span>
                    </p>
                ))}

                {script && (
                    <div className="space-y-2 rounded-lg border border-theme-border bg-theme-bg/40 px-3 py-2.5">
                        <p className="text-xs font-semibold text-theme-text">Orchestration administrator cleanup script</p>
                        <p className="max-w-[68ch] text-xs leading-relaxed text-theme-muted">
                            {handoff?.instructions || 'Ask an Orchestration administrator to run this idempotent cleanup script once, then click Re-check & Delete.'}
                        </p>
                        <div className="flex flex-wrap gap-2">
                            <Button variant="outline" size="sm" onClick={downloadScript} disabled={busy}>
                                <Download size={13} aria-hidden="true" /> Download
                            </Button>
                            <Button variant="outline" size="sm" onClick={copyScript} disabled={busy}>
                                {copied
                                    ? <Check size={13} className="text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                                    : <ClipboardCopy size={13} aria-hidden="true" />}
                                {copied ? 'Copied' : 'Copy'}
                            </Button>
                        </div>
                    </div>
                )}

                <FormError message={error || localError} />

                {confirmRequired && (
                    <div className="space-y-2.5 rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-2.5">
                        <Checkbox
                            checked={Boolean(confirmed)}
                            onChange={(event) => onConfirmChange?.(event.target.checked)}
                            disabled={busy}
                            label="I confirm Orchestration administrator cleanup has been run (or accept leaving cluster resources behind) and want to delete the Lens cluster record."
                        />
                        <div className="flex flex-wrap gap-2">
                            <Button
                                variant="danger"
                                size="sm"
                                onClick={onConfirmDelete}
                                disabled={!confirmed || busy}
                            >
                                Confirm &amp; Delete Record
                            </Button>
                        </div>
                    </div>
                )}

                {(operations.length > 0 || completedOperations.length > 0 || script || marker.name || marker.namespace) && (
                    <details
                        className="group/details border-t border-theme-border pt-3"
                        open={detailsOpen}
                        onToggle={handleDetailsToggle}
                    >
                        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-xs font-medium text-theme-muted hover:text-theme-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60 [&::-webkit-details-marker]:hidden">
                            <span className="flex items-center gap-1.5">
                                <ChevronRight
                                    size={13}
                                    aria-hidden="true"
                                    className="shrink-0 transition-transform motion-reduce:transition-none group-open/details:rotate-90"
                                />
                                View cleanup operations &amp; script
                            </span>
                            <span className="font-mono text-[11px]">{detailSummaryLabel}</span>
                        </summary>

                        <div className="mt-3 space-y-4">
                            {operations.length > 0 && (
                                <section className="space-y-2" aria-labelledby="cleanup-remaining-heading">
                                    <h3 id="cleanup-remaining-heading" className="text-xs font-semibold text-theme-text">
                                        Remaining Orchestration administrator operations
                                    </h3>
                                    <ul className="space-y-1">
                                        {operations.map((entry, index) => (
                                            <OperationRow
                                                key={`${operationId(entry) || index}-cleanup-remaining`}
                                                entry={entry}
                                                done={false}
                                                tone={markerStatus === 'unconfirmed' ? 'warning' : 'danger'}
                                                statusLabel={markerStatus === 'unconfirmed' ? 'Unconfirmed' : entry.required === false ? 'Best-effort' : 'Remaining'}
                                                ariaLabel={markerStatus === 'unconfirmed' ? 'Unconfirmed' : 'Remaining'}
                                                rules={[]}
                                            >
                                                <p className="break-all font-mono text-[11px] text-theme-muted">
                                                    {cleanupTargetLabel(entry)}
                                                </p>
                                            </OperationRow>
                                        ))}
                                    </ul>
                                </section>
                            )}

                            {completedOperations.length > 0 && (
                                <section className="space-y-2" aria-labelledby="cleanup-completed-heading">
                                    <h3 id="cleanup-completed-heading" className="text-xs font-semibold text-theme-text">
                                        Cleaned with edit kubeconfig
                                    </h3>
                                    <ul className="space-y-1">
                                        {completedOperations.map((entry, index) => (
                                            <OperationRow
                                                key={`${operationId(entry) || index}-cleanup-done`}
                                                entry={entry}
                                                done
                                                tone="success"
                                                statusLabel="Cleaned"
                                                ariaLabel="Done"
                                                rules={[]}
                                            >
                                                <p className="break-all font-mono text-[11px] text-theme-muted">
                                                    {cleanupTargetLabel(entry)}
                                                </p>
                                            </OperationRow>
                                        ))}
                                    </ul>
                                </section>
                            )}

                            <div className="space-y-1 border-t border-theme-border pt-3">
                                <p className="text-xs font-semibold text-theme-text">Completion marker</p>
                                <dl className="grid gap-1 text-[11px] text-theme-muted sm:grid-cols-[8rem_1fr]">
                                    <dt>Namespace</dt><dd className="break-all font-mono">{marker.namespace || 'not available'}</dd>
                                    <dt>ConfigMap</dt><dd className="break-all font-mono">{marker.name || 'not available'}</dd>
                                    <dt>Status</dt><dd>{marker.status || (unreachable ? 'unreachable' : markerStatus)}{marker.verified ? ' · verified' : ''}</dd>
                                </dl>
                            </div>

                            {script && (
                                <div className="space-y-1.5 border-t border-theme-border pt-3">
                                    <p className="text-xs font-semibold text-theme-text">Script preview</p>
                                    <pre className="max-h-60 overflow-auto rounded-lg border border-theme-border bg-theme-bg/70 p-3 font-mono text-[11px] leading-relaxed text-theme-text">
                                        {script}
                                    </pre>
                                </div>
                            )}
                        </div>
                    </details>
                )}
            </div>
        </Panel>
    );
}

export default PermissionHandoffPanel;
