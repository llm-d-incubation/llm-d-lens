// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { useMonitoringOperation } from './useMonitoringOperation';
import { openMonitoringLink } from './monitoringLinks';
import React, { useCallback, useEffect, useState } from 'react';
import {
    AlertOctagon,
    AlertTriangle,
    CheckCircle2,
    Gauge,
    RefreshCw,
    X,
} from 'lucide-react';
import { Button, EmptyState, LoadingState, SectionLabel, ToggleGroup } from '../../../../shared/ui';
import { ClusterStackComponentTable } from './ClusterStackComponentTable';
import { ClusterStackOperationProgress } from './ClusterStackOperationProgress';
import { InstallAcceleratorModal } from './InstallAcceleratorModal';
import {
    getAcceleratorCapabilities,
    getAcceleratorLinks,
    getAcceleratorOperation,
} from './acceleratorObservabilityBackend';
import { accessModeLabel } from './acceleratorLabels';
import { useAcceleratorObservability } from './useAcceleratorObservability';


export function PrometheusLogo({ className }) {
    return <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true"><path d="M12 0C5.373 0 0 5.372 0 12c0 6.627 5.373 12 12 12s12-5.373 12-12c0-6.628-5.373-12-12-12zm0 22.46c-1.885 0-3.414-1.26-3.414-2.814h6.828c0 1.553-1.528 2.813-3.414 2.813zm5.64-3.745H6.36v-2.046h11.28v2.046zm-.04-3.098H6.391c-.037-.043-.075-.086-.111-.13-1.155-1.401-1.427-2.133-1.69-2.879-.005-.025 1.4.287 2.395.511 0 0 .513.119 1.262.255-.72-.843-1.147-1.915-1.147-3.01 0-2.406 1.845-4.508 1.18-6.207.648.053 1.34 1.367 1.387 3.422.689-.951.977-2.69.977-3.755 0-1.103.727-2.385 1.454-2.429-.648 1.069.168 1.984.894 4.256.272.854.237 2.29.447 3.201.07-1.892.395-4.652 1.595-5.605-.529 1.2.079 2.702.494 3.424.671 1.164 1.078 2.047 1.078 3.716a4.642 4.642 0 01-1.11 2.996c.792-.149 1.34-.283 1.34-.283l2.573-.502s-.374 1.538-1.81 3.019z" /></svg>;
}

export function GrafanaLogo({ className }) {
    return <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true"><path d="M23.02 10.59a8.578 8.578 0 0 0-.862-3.034 8.911 8.911 0 0 0-1.789-2.445c.337-1.342-.413-2.505-.413-2.505-1.292-.08-2.113.4-2.416.62-.052-.02-.102-.044-.154-.064-.22-.089-.446-.172-.677-.247-.231-.073-.47-.14-.711-.197a9.867 9.867 0 0 0-.875-.161C14.557.753 12.94 0 12.94 0c-1.804 1.145-2.147 2.744-2.147 2.744l-.018.093c-.098.029-.2.057-.298.088-.138.042-.275.094-.413.143-.138.055-.275.107-.41.166a8.869 8.869 0 0 0-1.557.87l-.063-.029c-2.497-.955-4.716.195-4.716.195-.203 2.658.996 4.33 1.235 4.636a11.608 11.608 0 0 0-.607 2.635C1.636 12.677.953 15.014.953 15.014c1.926 2.214 4.171 2.351 4.171 2.351.003-.002.006-.002.006-.005.285.509.615.994.986 1.446.156.19.32.371.488.548-.704 2.009.099 3.68.099 3.68 2.144.08 3.553-.937 3.849-1.173a9.784 9.784 0 0 0 3.164.501h.08l.055-.003.107-.002.103-.005.003.002c1.01 1.44 2.788 1.646 2.788 1.646 1.264-1.332 1.337-2.653 1.337-2.94v-.058c0-.02-.003-.039-.003-.06.265-.187.52-.387.758-.6a7.875 7.875 0 0 0 1.415-1.7c1.43.083 2.437-.885 2.437-.885-.236-1.49-1.085-2.216-1.264-2.354l-.018-.013-.016-.013a.217.217 0 0 1-.031-.02c.008-.092.016-.18.02-.27.011-.162.016-.323.016-.48v-.253l-.005-.098-.008-.135a1.891 1.891 0 0 0-.01-.13c-.003-.042-.008-.083-.013-.125l-.016-.124-.018-.122a6.215 6.215 0 0 0-2.032-3.73 6.015 6.015 0 0 0-3.222-1.46 6.292 6.292 0 0 0-.85-.048l-.107.002h-.063l-.044.003-.104.008a4.777 4.777 0 0 0-3.335 1.695c-.332.4-.592.84-.768 1.297a4.594 4.594 0 0 0-.312 1.817l.003.091c.005.055.007.11.013.164a3.615 3.615 0 0 0 .698 1.82 3.53 3.53 0 0 0 1.827 1.282c.33.098.66.14.971.137.039 0 .078 0 .114-.002l.063-.003c.02 0 .041-.003.062-.003.034-.002.065-.007.099-.01.007 0 .018-.003.028-.003l.031-.005.06-.008a1.18 1.18 0 0 0 .112-.02c.036-.008.072-.013.109-.024a2.634 2.634 0 0 0 .914-.415c.028-.02.056-.041.085-.065a.248.248 0 0 0 .039-.35.244.244 0 0 0-.309-.06l-.078.042c-.09.044-.184.083-.283.116a2.476 2.476 0 0 1-.475.096c-.028.003-.054.006-.083.006l-.083.002c-.026 0-.054 0-.08-.002l-.102-.006h-.012l-.024.006c-.016-.003-.031-.003-.044-.006-.031-.002-.06-.007-.091-.01a2.59 2.59 0 0 1-.724-.213 2.557 2.557 0 0 1-.667-.438 2.52 2.52 0 0 1-.805-1.475 2.306 2.306 0 0 1-.029-.444l.006-.122v-.023l.002-.031c.003-.021.003-.04.005-.06a3.163 3.163 0 0 1 1.352-2.29 3.12 3.12 0 0 1 .937-.43 2.946 2.946 0 0 1 .776-.101h.06l.07.002.045.003h.026l.07.005a4.041 4.041 0 0 1 1.635.49 3.94 3.94 0 0 1 1.602 1.662 3.77 3.77 0 0 1 .397 1.414l.005.076.003.075c.002.026.002.05.002.075 0 .024.003.052 0 .07v.065l-.002.073-.008.174a6.195 6.195 0 0 1-.08.639 5.1 5.1 0 0 1-.267.927 5.31 5.31 0 0 1-.624 1.13 5.052 5.052 0 0 1-3.237 2.014 4.82 4.82 0 0 1-.649.066l-.039.003h-.287a6.607 6.607 0 0 1-1.716-.265 6.776 6.776 0 0 1-3.4-2.274 6.75 6.75 0 0 1-.746-1.15 6.616 6.616 0 0 1-.714-2.596l-.005-.083-.002-.02v-.056l-.003-.073v-.096l-.003-.104v-.07l.003-.163c.008-.22.026-.45.054-.678a8.707 8.707 0 0 1 .28-1.355c.128-.444.286-.872.473-1.277a7.04 7.04 0 0 1 1.456-2.1 5.925 5.925 0 0 1 .953-.763c.169-.111.343-.213.524-.306.089-.05.182-.091.273-.135.047-.02.093-.042.138-.062a7.177 7.177 0 0 1 .714-.267l.145-.045c.049-.015.098-.026.148-.041.098-.029.197-.052.296-.076.049-.013.1-.02.15-.033l.15-.032.151-.028.076-.013.075-.01.153-.024c.057-.01.114-.013.171-.023l.169-.021c.036-.003.073-.008.106-.01l.073-.008.036-.003.042-.002c.057-.003.114-.008.171-.01l.086-.006h.023l.037-.003.145-.007a7.999 7.999 0 0 1 1.708.125 7.917 7.917 0 0 1 2.048.68 8.253 8.253 0 0 1 1.672 1.09l.09.077.089.078c.06.052.114.107.171.159.057.052.112.106.166.16.052.055.107.107.159.164a8.671 8.671 0 0 1 1.41 1.978c.012.026.028.052.04.078l.04.078.075.156c.023.051.05.1.07.153l.065.15a8.848 8.848 0 0 1 .45 1.34.19.19 0 0 0 .201.142.186.186 0 0 0 .172-.184c.01-.246.002-.532-.024-.856z" /></svg>;
}

function display(value) {
    return String(value || 'unknown').replace(/_/g, ' ');
}

export function AcceleratorObservabilityPanel({ clusterId }) {
    const [capabilities, setCapabilities] = useState([]);
    const [capabilitiesError, setCapabilitiesError] = useState(null);
    const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
    // Selected accelerator comes from the capabilities payload (every hardware
    // profile registers one); nothing is assumed about which vendor is present.
    const [activeType, setActiveType] = useState(null);
    const [namespace, setNamespace] = useState(null);
    const [accessMode, setAccessMode] = useState(null);
    const [installOpen, setInstallOpen] = useState(false);
    const [operation, setOperation] = useState(null);
    const [operationError, setOperationError] = useState(null);
    const [installSuccess, setInstallSuccess] = useState(null);
    const [completedOperationId, setCompletedOperationId] = useState(null);
    const [linksError, setLinksError] = useState(null);
    const [openingLink, setOpeningLink] = useState(null);

    const status = useAcceleratorObservability(activeType, namespace, accessMode, clusterId);
    const snapshot = status.data;
    const refreshStatus = status.refresh;
    const snapshotOperationId = snapshot?.active_operation_id;
    const operationId = operation?.operation_id
        || (snapshotOperationId !== completedOperationId ? snapshotOperationId : null);

    useEffect(() => {
        let cancelled = false;
        getAcceleratorCapabilities()
            .then((payload) => {
                if (cancelled) return;
                const accelerators = payload?.accelerators || [];
                setCapabilities(accelerators);
                if (accelerators.length) {
                    setActiveType((current) => (
                        current && accelerators.some((entry) => entry.type === current)
                            ? current
                            : accelerators[0].type
                    ));
                }
            })
            .catch((error) => {
                if (!cancelled) setCapabilitiesError(error);
            })
            .finally(() => {
                if (!cancelled) setCapabilitiesLoading(false);
            });
        return () => { cancelled = true; };
    }, []);

    // Keep the namespace in sync with the selected accelerator's default; a
    // running operation may temporarily point at a different one.
    useEffect(() => {
        const capability = capabilities.find((entry) => entry.type === activeType);
        if (capability?.default_namespace) setNamespace(capability.default_namespace);
    }, [activeType, capabilities]);

    const loadOperation = useCallback((id, options) => getAcceleratorOperation(activeType, id, options), [activeType]);
    useMonitoringOperation({
        operationId, operationStatus: operation?.status, completedOperationId, namespace,
        loadOperation: loadOperation, refreshStatus, setOperation, setOperationError,
        setCompletedOperationId, setInstallSuccess,
    });

    useEffect(() => {
        if (!installSuccess) return undefined;
        const timer = window.setTimeout(() => setInstallSuccess(null), 10000);
        return () => window.clearTimeout(timer);
    }, [installSuccess]);

    const selectAccelerator = (type) => {
        const capability = capabilities.find((entry) => entry.type === type);
        setActiveType(type);
        setNamespace(capability?.default_namespace || '');
        setAccessMode(null);
        setOperation(null);
        setOperationError(null);
        setInstallSuccess(null);
        setCompletedOperationId(null);
    };

    const startOperation = (nextOperation) => {
        if (nextOperation.namespace && nextOperation.namespace !== namespace) {
            setNamespace(nextOperation.namespace);
        }
        setOperation(nextOperation);
        setOperationError(null);
        setInstallSuccess(null);
        setCompletedOperationId(null);
        refreshStatus();
    };

    const openLink = async (kind) => {
        setOpeningLink(kind);
        setLinksError(null);
        try {
            const payload = await getAcceleratorLinks(activeType, { clusterId });
            openMonitoringLink(payload, kind);
        } catch (error) {
            setLinksError(error.message || `Unable to open ${kind} dashboard`);
        } finally {
            setOpeningLink(null);
        }
    };

    const capability = capabilities.find((entry) => entry.type === activeType) || null;
    const displayName = capability?.display_name || display(activeType);
    const releaseName = capability?.release_name || displayName;

    const accessModes = (snapshot?.access_modes || []).map((entry) => ({
        value: entry.mode,
        label: accessModeLabel(entry.mode),
        disabled: !entry.available,
    }));
    // Prefer the mode actually detected on the cluster over a hardcoded default:
    // an accelerator that only ships a device plugin (or only a DRA driver)
    // shouldn't surface the other mode's "deploy it first" hint on open.
    const firstAvailableMode = (snapshot?.access_modes || []).find((entry) => entry.available)?.mode;
    const effectiveAccessMode = accessMode
        || snapshot?.access_mode
        || firstAvailableMode
        || snapshot?.access_modes?.[0]?.mode
        || 'dra';
    const unavailableMessages = (snapshot?.access_modes || [])
        .filter((entry) => entry.mode === effectiveAccessMode && !entry.available && entry.message)
        .map((entry) => entry.message);
    const canInstall = !operationId && ['absent', 'degraded', 'ready'].includes(snapshot?.status);

    // Accelerator tabs come entirely from the backend capabilities registry
    // (which now lists every registered provider); no hardcoded placeholders.
    const tabOptions = capabilities.map((entry) => ({
        value: entry.type,
        label: entry.display_name,
    }));

    if (!clusterId) {
        return (
            <div>
                <EmptyState
                    icon={<Gauge className="h-10 w-10" />}
                    title="No ready cluster selected"
                    message="Accelerator observability requires a ready Kubernetes cluster. No status is shown until a ready cluster is available."
                />
            </div>
        );
    }

    return (
        <div>
            <div className="mb-4" role="tablist" aria-label="Accelerator type">
                <ToggleGroup options={tabOptions} value={activeType} onChange={selectAccelerator} activeClassName="accelerator-tab-active bg-sky-500/20 dark:bg-sky-500/20 text-white ring-1 ring-sky-400/30 shadow-none" />
            </div>

            {capabilitiesError && (
                <div className="rounded-xl border border-red-500/40 bg-red-950/60 p-4 text-xs text-red-200">
                    {capabilitiesError.message || 'Unable to load accelerator capabilities'}
                </div>
            )}

            {capabilitiesLoading ? (
                    <LoadingState label="Loading accelerators…" />
                ) : status.loading && !snapshot ? (
                    <LoadingState label="Discovering accelerator observability…" />
                ) : status.error ? (
                    <div className="rounded-xl border border-red-500/40 bg-red-950/60 p-4">
                        <div className="flex items-start justify-between gap-4">
                            <div className="flex items-start gap-3">
                                <AlertOctagon className="mt-0.5 h-5 w-5 shrink-0 text-red-300" />
                                <div>
                                    <div className="text-sm font-bold text-red-200">Accelerator observability service is not responding normally</div>
                                    <p className="mt-1 text-xs text-red-200/75">{status.error.message}</p>
                                </div>
                            </div>
                            <Button variant="secondary" size="sm" onClick={refreshStatus} isLoading={status.loading}>
                                <RefreshCw className="h-3.5 w-3.5" /> Retry
                            </Button>
                        </div>
                    </div>
                ) : snapshot ? (
                    <>
                        {snapshot.status !== 'ready' && (
                            <div className="rounded-xl border border-slate-800/80 bg-slate-950/55 p-4">
                                <p className="text-xs leading-relaxed text-slate-400">
                                    {snapshot.message || `${displayName} telemetry collected through the ${releaseName} DaemonSet.`}
                                </p>
                            </div>
                        )}

                        <div className="mt-4">
                            <SectionLabel tone="sky" className="text-[#037ffc]">GPU access mode</SectionLabel>
                            <p className="mt-1 text-xs text-slate-500">
                                Detected from the environment. Choose the access mode used to expose {displayName} metrics.
                            </p>
                            <div className="mt-3">
                                <ToggleGroup
                                    options={accessModes}
                                    value={effectiveAccessMode}
                                    onChange={(mode) => setAccessMode(mode)}
                                    fullWidth
                                    activeClassName="accelerator-tab-active bg-sky-500/20 dark:bg-sky-500/20 text-white ring-1 ring-sky-400/30 shadow-none"
                                />
                                {unavailableMessages.length > 0 && (
                                    <div className="mt-2 space-y-1.5">
                                        {unavailableMessages.map((message, index) => (
                                            <div key={`${message}-${index}`} className="flex items-start gap-2 text-[11px] text-amber-300/90">
                                                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                                                <span>{message}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>

                        <div className="accelerator-observability-ui relative mt-4 rounded-xl border border-slate-800/80 bg-slate-950/55 p-4 shadow-inner">
                            <SectionLabel tone="sky" className="text-[#037ffc]">Observability UI</SectionLabel>
                            {snapshot.cluster_reachable !== false && (
                                <div className="mt-3 flex flex-wrap items-center gap-2">
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => openLink('prometheus')}
                                        isLoading={openingLink === 'prometheus'}
                                        disabled={openingLink !== null}
                                    >
                                        <PrometheusLogo className="h-3.5 w-3.5 text-[#E6522C]" /> Prometheus
                                    </Button>
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => openLink('grafana')}
                                        isLoading={openingLink === 'grafana'}
                                        disabled={openingLink !== null}
                                    >
                                        <GrafanaLogo className="h-3.5 w-3.5 text-[#F46800]" /> Grafana
                                    </Button>
                                    {linksError && (
                                        <span className="flex items-center gap-1.5 text-[11px] text-red-300/90">
                                            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                                            {linksError}
                                        </span>
                                    )}
                                </div>
                            )}
                        </div>

                        {installSuccess && (
                            <div className="mt-3 flex items-start justify-between gap-3 rounded-xl border border-emerald-500/35 bg-emerald-950/50 p-4">
                                <div className="flex items-start gap-3">
                                    <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" />
                                    <div>
                                        <div className="text-sm font-bold text-emerald-200">{displayName} observability installed successfully</div>
                                        <p className="mt-1 text-xs text-emerald-200/70">
                                            {releaseName} in <span className="font-mono">{installSuccess.namespace}</span> passed readiness verification.
                                        </p>
                                    </div>
                                </div>
                                <button type="button" onClick={() => setInstallSuccess(null)} className="rounded-lg p-1.5 text-emerald-300/60 hover:bg-emerald-500/10" aria-label="Dismiss">
                                    <X className="h-4 w-4" />
                                </button>
                            </div>
                        )}

                        <div className="mt-4">
                            <ClusterStackOperationProgress
                                operation={operation}
                                error={operationError}
                                onRetryPreflight={canInstall ? () => setInstallOpen(true) : null}
                            />
                        </div>

                        <div className="mt-4">
                            <ClusterStackComponentTable
                                components={snapshot.components}
                                actions={snapshot.status === 'ready' && !operationId ? (
                                    <Button variant="sky" size="sm" onClick={() => setInstallOpen(true)}>
                                        <RefreshCw className="h-3.5 w-3.5" /> Reinstall
                                    </Button>
                                ) : null}
                            />
                        </div>

                        {snapshot.status === 'absent' && !operationId && (
                            <div className="accelerator-empty-panel mt-4 rounded-xl border border-emerald-500/20 bg-emerald-950/30 p-2">
                                <EmptyState
                                    icon={<Gauge className="h-10 w-10" />}
                                    iconClassName="accelerator-empty-icon"
                                    title={`${displayName} observability is not installed`}
                                    message={`Run preflight checks, pick a detected GPU access mode, then install the ${releaseName} chart. Prometheus and Grafana reuse the existing Cluster infrastructure stack.`}
                                    action={<Button onClick={() => setInstallOpen(true)}>Install accelerator observability</Button>}
                                />
                            </div>
                        )}

                        {snapshot.status === 'degraded' && (
                            <div className="mt-4 rounded-xl border border-amber-500/20 bg-amber-950/30 p-2">
                                <EmptyState
                                    icon={<AlertTriangle className="h-10 w-10" />}
                                    title={`${displayName} observability is incomplete`}
                                    message={`Some ${releaseName} resources are missing. Run preflight and reinstall to reconcile the chart; Prometheus and Grafana reuse the existing Cluster infrastructure stack.`}
                                    action={(
                                        <div className="flex items-center justify-center gap-2">
                                            <Button onClick={() => setInstallOpen(true)}>Repair / reinstall</Button>
                                            <Button variant="outline" onClick={refreshStatus}>Refresh status</Button>
                                        </div>
                                    )}
                                />
                            </div>
                        )}
                    </>
                ) : (
                    <EmptyState
                        title="Accelerator status unavailable"
                        message="Check access to the current Kubernetes context and try again."
                        action={<Button variant="outline" onClick={refreshStatus}>Retry</Button>}
                    />
                )}

            <InstallAcceleratorModal
                isOpen={installOpen}
                accelerator={activeType}
                defaultNamespace={namespace}
                status={snapshot}
                supportedAccessModes={capability?.access_modes || []}
                initialAccessMode={accessMode}
                clusterId={clusterId}
                onClose={() => setInstallOpen(false)}
                onOperationStarted={startOperation}
            />
        </div>
    );
}
