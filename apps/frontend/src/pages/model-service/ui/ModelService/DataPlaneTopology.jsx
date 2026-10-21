import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
    Boxes, Cpu, GitBranch, Layers, Menu, Network, Play, Plus, RotateCw, Router, ShieldCheck, Square, Settings, Terminal, Trash2, Unlink, Waypoints,
} from 'lucide-react';
import { cn } from '../../../../shared/lib/cn';
import { Badge, StatusChip } from '../../../../shared/ui/Badge';
import { Button } from '../../../../shared/ui/Button';
import { HelpTooltip } from '../../../../shared/ui/FormControls';
import { Panel } from '../../../../shared/ui/Panel';
import { PermissionGate, useAuth } from '../../../auth';
import { trafficShare, trafficTotal } from '../../model/modelService';

// Canonical llm-d Gateway Mode data plane.
//   Lens control plane           (config publishing)
//            |
//   Clients -> Gateway -> HTTPRoute -> InferencePool -> EPP -> model server
//            |
//   IPP (ext_proc: body.model -> X-Gateway-Base-Model-Name)
//
// Each cluster has its own Gateway + IPP. A model service has one HTTPRoute
// and deployment-owned pools/EPPs; Lens publishes the positive-weight pools.
const COL = { httpRoute: 560, pool: 896, epp: 1108, model: 1320 };
const WB = { httpRoute: 200, pool: 200, epp: 200, model: 200 };
const BOX_H = 64;
const DEPLOYMENT = { x: COL.pool - 16, w: COL.model + WB.model - COL.pool + 32, header: 36 };
const DEPLOYMENT_H = DEPLOYMENT.header + BOX_H + 12;
const MODEL = { x: COL.httpRoute - 16, w: DEPLOYMENT.x + DEPLOYMENT.w - COL.httpRoute + 32, header: 36 };
const ROUTE_BRANCH_X = (COL.httpRoute + WB.httpRoute + DEPLOYMENT.x) / 2 - MODEL.x;
const MODEL_H = MODEL.header + DEPLOYMENT_H + 12;
const COMPONENT_Y = MODEL.header + DEPLOYMENT.header;
const DEPLOYMENT_GAP = 16;
const GW = { x: 300, w: 200, h: BOX_H };
const IPP = { ...GW };
const CP = { ...GW, y: 12 };
const CLIENTS = { x: 8, y: 12, w: 128, h: BOX_H };
const CLIENT_BUS = 240;
const CP_TRUNK_X = 270;
const GW_BUS = 520;
const CLUSTER = { x: GW.x - 16, w: MODEL.x + MODEL.w - GW.x + 32, header: 48, bottom: 16 };
const SECTION_GAP = 40;
const WIDTH = MODEL.x + MODEL.w + 24;
const TRAFFIC_HELP = 'Configure relative traffic weights across this model’s deployment providers. Weight 0 keeps a provider on standby; offline providers receive no traffic.';

function Box({ x, y, w = 180, h = BOX_H, label, help, helpSource, helpPlacement = 'top', sub, icon, status, statusText, crd, menuOpen, onToggleMenu, menuPermissions = ['model-service:gateway:manage'] }) {
    const { can } = useAuth();
    const canOpenMenu = menuPermissions.some((permission) => can(permission));
    return (
        <div className="absolute" style={{ left: x, top: y, width: w, height: h }}>
            <div className={cn(
                'flex h-full flex-col justify-center rounded-lg border bg-slate-950/95 px-2.5 shadow-lg',
                menuOpen ? 'border-sky-500/70' : 'border-slate-700/80',
            )}>
                <div className="flex items-center gap-1 pr-1">
                    {status && <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s]', status)} title={statusText} />}
                    {icon}
                    <span className="text-xs font-semibold text-slate-200">{label}</span>
                    {help && (
                        <HelpTooltip label={`${label} help`} className="relative"
                            tooltipClassName={cn('right-0 w-80', helpPlacement === 'bottom' ? 'top-full pt-2' : 'bottom-full pb-2')}>
                            {help}
                            {helpSource && (
                                <a className="mt-2 inline-block underline underline-offset-2"
                                    href={helpSource} target="_blank" rel="noopener noreferrer">
                                    Official {label} documentation
                                </a>
                            )}
                        </HelpTooltip>
                    )}
                    {crd && <Badge tone="neutral" size="xxs">CRD</Badge>}
                    {onToggleMenu && canOpenMenu && (
                        <span className="ml-auto inline-flex">
                            <Button variant="ghost" size="icon" aria-label={`${label} actions`} aria-haspopup="menu" aria-expanded={menuOpen} onClick={onToggleMenu}>
                                <Menu size={12} />
                            </Button>
                        </span>
                    )}
                </div>
                {sub && <span className="mt-0.5 truncate text-[9.5px] leading-tight text-slate-500" title={sub}>{sub}</span>}
            </div>
        </div>
    );
}

function MenuAction({ icon, label, onClick, disabled = false, title }) {
    return (
        <Button type="button" variant="ghost" size="sm" role="menuitem" onClick={onClick}
            disabled={disabled} title={title}
            className="w-full justify-start gap-2 rounded-md px-2 py-1.5 text-left font-normal disabled:pointer-events-auto disabled:cursor-not-allowed disabled:hover:bg-transparent">
            {icon} {label}
        </Button>
    );
}

export function PublicationToggleAction({ kind, stopped, pending, onClick }) {
    const model = kind === 'model';
    const label = model
        ? (stopped ? 'Resume serving' : 'Stop serving')
        : (stopped ? 'Bring online' : 'Take offline');
    return (
        <MenuAction icon={stopped ? <Play size={12} /> : <Square size={12} />}
            label={label} disabled={pending} onClick={onClick}
            title={model
                ? 'Control this public model without changing provider states, weights or running Pods.'
                : 'Control routing to this deployment without changing its weight or scaling its Pods. Weight 0 remains standby.'} />
    );
}

export function ProvidersToggleAction({ members, pending, onClick }) {
    const online = members.length > 0 && members.every((member) => member.status === 'disabled');
    return <MenuAction icon={online ? <Play size={12} /> : <Square size={12} />}
        label={online ? 'Bring providers online' : 'Take providers offline'}
        disabled={pending || members.length === 0}
        title={online
            ? 'Bring all providers online. Every deployment must be ready; weights, model state and Pods are unchanged.'
            : 'Take all providers offline, including a mix of online and offline providers. Weights, model state and Pods are unchanged.'}
        onClick={() => onClick(online)} />;
}

export function ManageDeploymentAction({ deployment, onNavigate }) {
    return (
        <PermissionGate permission="deployment:run:read">
            <MenuAction icon={<Settings size={12} />} label="Manage"
                disabled={!deployment.namespace}
                title={deployment.namespace ? 'Find this deployment by namespace.' : 'Deployment namespace is unavailable.'}
                onClick={() => onNavigate('optimization-deployments', { deploymentSearch: deployment.namespace })} />
        </PermissionGate>
    );
}

export function ComponentLogAction({ component, cluster, onClick }) {
    const gateway = component === 'gateway';
    const label = gateway ? 'Gateway' : 'IPP';
    const installed = (gateway ? cluster.gatewayState : cluster.ippState) === 'installed';
    const running = installed && (gateway ? cluster.gatewayRunning !== false : Boolean(cluster.ippReady));
    const reason = running ? undefined : `${installed ? 'Start' : 'Install'} ${label} before viewing logs.`;
    return (
        <MenuAction icon={<Terminal size={12} />} label="View logs" disabled={!running} title={reason}
            onClick={running ? onClick : undefined} />
    );
}

// "Stop" scales the Gateway's data-plane Deployment to zero but leaves the
// Gateway resource in place, so `gatewayState` stays `installed` and the
// `Programmed` condition behind `gatewayReady` can stay true. `gatewayRunning`
// reports the Deployment's ready replicas, which is what separates a stopped
// Gateway (grey) from a running one (green) or a broken one (amber).
function clusterDot(cluster) {
    if (cluster.gatewayState !== 'installed') return 'unhealthy';
    if (cluster.gatewayRunning === false) return 'stopped';
    return cluster.gatewayReady ? 'active' : 'degraded';
}

function gatewayDetail(cluster) {
    if (cluster.gatewayState !== 'installed') return 'not installed';
    if (cluster.gatewayRunning === false) return 'installed, stopped';
    return cluster.gatewayReady ? 'ready' : 'installed, not ready';
}

// `ippState` only says the Deployment object exists, so "Stop IPP" (which
// scales it to zero replicas) leaves it `installed`. Pair it with `ippReady`
// the same way the Gateway box pairs `gatewayState` with `gatewayReady`, so a
// stopped processor is not reported as running.
function ippDot(cluster) {
    if (cluster.ippState !== 'installed') return 'missing';
    return cluster.ippReady ? 'active' : 'stopped';
}

const STATUS_LABEL = {
    active: 'active',
    deployed: 'deployed',
    ready: 'ready',
    running: 'running',
    installed: 'installed',
    missing: 'not installed',
    unhealthy: 'unhealthy',
    degraded: 'degraded',
    external: 'external',
    unknown: 'unknown',
    stopped: 'stopped',
    standby: 'standby (0 weight; not routed)',
    absent: 'no members',
    disabled: 'disabled',
};

function statusLabel(name) {
    return STATUS_LABEL[name] || name || 'unknown';
}

// Per-component health reported by the backend (healthJson.components) so each
// box reflects its own resource instead of one shared member status.
const COMPONENT_STATE = { ready: 'active', standby: 'standby', degraded: 'degraded', missing: 'unhealthy', unknown: 'unknown' };

function componentName(member, key) {
    const state = member.components?.[key];
    if (!state || state === 'n/a') return member.status || 'unknown';
    return COMPONENT_STATE[state] || 'unknown';
}

export function DataPlaneTopology({
    clusters = [],
    memberGroups = [],
    statusDot,
    onComponentAction,
    onEditTraffic,
    onToggleService,
    onSetProvidersStatus,
    onRemoveModel,
    onManageAccess,
    onToggleDeployment,
    onNavigate,
    publicationPending = false,
    onDeleteDeployment,
}) {
    const [menu, setMenu] = useState(null);
    const dragRef = useRef(null);

    const finishDrag = (event) => {
        if (dragRef.current?.pointerId !== event.pointerId) return;
        dragRef.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
        }
    };
    const startDrag = (event) => {
        if (event.button !== 0 || event.pointerType !== 'mouse'
            || event.target.closest('button, a, input, select, textarea, summary, [role="button"], [role="link"]')) return;
        const viewport = event.currentTarget;
        dragRef.current = {
            pointerId: event.pointerId, x: event.clientX, y: event.clientY,
            left: viewport.scrollLeft, top: viewport.scrollTop,
        };
        viewport.focus({ preventScroll: true });
        viewport.setPointerCapture(event.pointerId);
        event.preventDefault();
    };
    const moveDrag = (event) => {
        const start = dragRef.current;
        if (!start || start.pointerId !== event.pointerId) return;
        if (!(event.buttons & 1)) { finishDrag(event); return; }
        event.currentTarget.scrollLeft = start.left + start.x - event.clientX;
        event.currentTarget.scrollTop = start.top + start.y - event.clientY;
    };

    const layout = useMemo(() => {
        const sections = clusters.map((cluster) => {
            const rows = memberGroups.flatMap((group) => {
                const members = group.clusters
                    .filter((gc) => gc.clusterId === cluster.clusterId)
                    .flatMap((gc) => gc.deployments);
                if (!group.clusters.some((gc) => gc.clusterId === cluster.clusterId)) return [];
                return [{
                    serviceId: group.id,
                    serviceName: group.name,
                    serviceStatus: group.status || 'active',
                    members,
                    height: MODEL_H + Math.max(0, members.length - 1) * (DEPLOYMENT_H + DEPLOYMENT_GAP),
                    routeY: COMPONENT_Y + Math.max(0, members.length - 1) * (DEPLOYMENT_H + DEPLOYMENT_GAP) / 2,
                }];
            });
            return { cluster, rows };
        });
        const heights = sections.map((section) =>
            Math.max(section.rows.reduce((sum, row, index) => sum + row.height + (index ? 16 : 0), 0), GW.h + IPP.h + 12));
        const base = CP.y + CP.h + 40;
        const starts = heights.map((_, index) =>
            base + heights.slice(0, index).reduce((sum, height) => sum + CLUSTER.header + height + CLUSTER.bottom + SECTION_GAP, 0));
        return sections.map((section, index) => {
            const headerY = starts[index];
            const sectionTop = headerY + CLUSTER.header;
            const bodyH = heights[index];
            const gwY = sectionTop + (bodyH - GW.h - IPP.h - 12) / 2;
            const ippY = gwY + GW.h + 12;
            let nextY = sectionTop;
            const rows = section.rows.map((row) => {
                const positioned = { ...row, y: nextY };
                nextY += row.height + 16;
                return positioned;
            });
            return { cluster: section.cluster, headerY, height: CLUSTER.header + bodyH + CLUSTER.bottom, gwY, ippY, rows };
        });
    }, [clusters, memberGroups]);

    // Keep enough room for the always-rendered control plane/clients row even
    // before clusters resolve, so the absolutely positioned boxes are not
    // clipped to a thin strip while the first load is in flight.
    const emptyHeight = CP.y + CP.h + CLUSTER.header + GW.h + IPP.h + 12;
    const height = Math.max(layout.reduce((max, section) =>
        Math.max(max, section.headerY + section.height), 0), emptyHeight) + 24;
    const lastGwMid = layout.length ? layout[layout.length - 1].gwY + GW.h / 2 : 0;

    const toggleMenu = (key, event) => {
        if (menu?.key === key) { setMenu(null); return; }
        const rect = event.currentTarget.getBoundingClientRect();
        setMenu({ key, top: rect.bottom + 4, right: Math.max(8, window.innerWidth - rect.right) });
    };
    const choose = (fn) => { setMenu(null); fn(); };

    useEffect(() => {
        if (!menu) return undefined;
        const close = () => setMenu(null);
        const onKey = (event) => { if (event.key === 'Escape') setMenu(null); };
        window.addEventListener('keydown', onKey);
        window.addEventListener('resize', close);
        window.addEventListener('scroll', close, true);
        return () => {
            window.removeEventListener('keydown', onKey);
            window.removeEventListener('resize', close);
            window.removeEventListener('scroll', close, true);
        };
    }, [menu]);

    const menuItems = () => {
        if (!menu) return null;
        if (menu.key.startsWith('model:')) {
            const section = layout.find((item) => item.rows.some((row) => menu.key === `model:${item.cluster.clusterId}:${row.serviceId}`));
            const row = section?.rows.find((item) => menu.key === `model:${section.cluster.clusterId}:${item.serviceId}`);
            if (!row) return null;
            return (
                <>
                    {onToggleService && <PermissionGate permission="model-service:group:toggle">
                        <PublicationToggleAction kind="model" stopped={row.serviceStatus === 'disabled'}
                            pending={publicationPending}
                            onClick={() => choose(() => onToggleService({ id: row.serviceId, name: row.serviceName }))} />
                    </PermissionGate>}
                    {onSetProvidersStatus && <PermissionGate permission="model-service:member:manage">
                        <ProvidersToggleAction members={row.members} pending={publicationPending}
                            onClick={(online) => choose(() => onSetProvidersStatus(row.serviceId, online))} />
                    </PermissionGate>}
                    {onEditTraffic && <PermissionGate permission="model-service:member:manage">
                        <MenuAction icon={<GitBranch size={12} />} label="Traffic distribution"
                            title={TRAFFIC_HELP} disabled={publicationPending}
                            onClick={() => choose(() => onEditTraffic(row.serviceId))} />
                    </PermissionGate>}
                    {onRemoveModel && <PermissionGate permission="model-service:group:remove">
                        <MenuAction icon={<Unlink size={12} />} label="Remove model"
                            disabled={publicationPending || row.members.length > 0}
                            title={row.members.length ? 'Remove every deployment from this model first.' : 'Safely remove this empty model and its HTTPRoute.'}
                            onClick={() => choose(() => onRemoveModel(row.serviceId))} />
                    </PermissionGate>}
                    {onManageAccess && <PermissionGate permission="model-service:group:share">
                        <MenuAction icon={<ShieldCheck size={12} />} label="Manage access"
                            title="Grant or revoke model inference for users and groups in this cluster."
                            onClick={() => choose(() => onManageAccess(row.serviceId))} />
                    </PermissionGate>}
                </>
            );
        }
        if (menu.key.startsWith('deployment:')) {
            const memberId = menu.key.slice('deployment:'.length);
            const row = layout.flatMap((section) => section.rows)
                .find((item) => item.members.some((member) => member.memberId === memberId));
            const member = row?.members.find((item) => item.memberId === memberId);
            if (!member) return null;
            return (
                <>
                    <PermissionGate permission="model-service:member:manage">
                        {onToggleDeployment && <PublicationToggleAction kind="deployment" stopped={member.status === 'disabled'}
                            pending={publicationPending} onClick={() => choose(() => onToggleDeployment(member))} />}
                    </PermissionGate>
                    {onNavigate && <ManageDeploymentAction deployment={member}
                        onNavigate={(view, params) => choose(() => onNavigate(view, params))} />}
                    <PermissionGate permission="model-service:member:manage">
                        {onDeleteDeployment && <MenuAction icon={<Unlink size={12} />} label="Remove from model"
                            title="Detach this provider from the model without deleting its deployment or Pods."
                            onClick={() => choose(() => onDeleteDeployment(member, row.serviceName))} />}
                    </PermissionGate>
                </>
            );
        }
        if (menu.key.startsWith('gw:')) {
            const cluster = clusters.find((item) => item.clusterId === menu.key.slice(3));
            if (!cluster) return null;
            const installed = cluster.gatewayState === 'installed';
            const running = installed && cluster.gatewayRunning !== false;
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{cluster.clusterName || cluster.clusterId} · {cluster.gatewayProvider || 'gateway'}</div>
                    <PermissionGate permission="cluster:cluster:update">
                        <MenuAction icon={<Settings size={12} />} label="Configure"
                            onClick={() => choose(() => onComponentAction(cluster, 'gateway', 'configure'))} />
                    </PermissionGate>
                    <PermissionGate permission="model-service:gateway:manage">
                        {installed && (
                            <MenuAction icon={running ? <Square size={12} /> : <Play size={12} />} label={running ? 'Stop' : 'Start'}
                                onClick={() => choose(() => onComponentAction(cluster, 'gateway', running ? 'stop' : 'start'))} />
                        )}
                    </PermissionGate>
                    <PermissionGate permission="model-service:gateway:reconcile">
                        <MenuAction icon={<RotateCw size={12} />} label="Reconcile" onClick={() => choose(() => onComponentAction(cluster, 'gateway', 'reconcile'))} />
                    </PermissionGate>
                    <PermissionGate permission="model-service:gateway:manage">
                        <MenuAction icon={installed ? <Trash2 size={12} /> : <Plus size={12} />} label={installed ? 'Uninstall' : 'Install'}
                            onClick={() => choose(() => onComponentAction(cluster, 'gateway', installed ? 'uninstall' : 'install'))} />
                    </PermissionGate>
                    <ComponentLogAction component="gateway" cluster={cluster}
                        onClick={() => choose(() => onComponentAction(cluster, 'gateway', 'logs'))} />
                </>
            );
        }
        if (menu.key.startsWith('ipp:')) {
            const cluster = clusters.find((item) => item.clusterId === menu.key.slice(4));
            if (!cluster) return null;
            const installed = cluster.ippState === 'installed';
            const running = installed && Boolean(cluster.ippReady);
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{cluster.clusterName || cluster.clusterId} · IPP</div>
                    {installed && (
                        <MenuAction icon={running ? <Square size={12} /> : <Play size={12} />} label={running ? 'Stop' : 'Start'}
                            onClick={() => choose(() => onComponentAction(cluster, 'ipp', running ? 'stop' : 'start'))} />
                    )}
                    <MenuAction icon={<Settings size={12} />} label="Configure" onClick={() => choose(() => onComponentAction(cluster, 'ipp', 'configure'))} />
                    <MenuAction icon={installed ? <Trash2 size={12} /> : <Plus size={12} />} label={installed ? 'Uninstall' : 'Install'}
                        onClick={() => choose(() => onComponentAction(cluster, 'ipp', installed ? 'uninstall' : 'install'))} />
                    <ComponentLogAction component="ipp" cluster={cluster}
                        onClick={() => choose(() => onComponentAction(cluster, 'ipp', 'logs'))} />
                </>
            );
        }
        if (menu.key.startsWith('route:')) {
            const section = layout.find((item) => item.rows.some((row) => menu.key === `route:${item.cluster.clusterId}:${row.serviceId}`));
            const row = section?.rows.find((item) => menu.key === `route:${section.cluster.clusterId}:${item.serviceId}`);
            if (!row) return null;
            const cluster = section.cluster;
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{row.serviceName} · HTTPRoute</div>
                    <MenuAction icon={<RotateCw size={12} />} label="Reconcile route" onClick={() => choose(() => onComponentAction(cluster, 'gateway', 'reconcile'))} />
                </>
            );
        }
        if (menu.key.startsWith('epp:')) {
            const memberId = menu.key.slice(4);
            const member = layout.flatMap((section) => section.rows.flatMap((row) => row.members)).find((item) => item.memberId === memberId);
            if (!member) return null;
            const running = componentName(member, 'epp') === 'active';
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{member.name}</div>
                    <MenuAction icon={running ? <Square size={12} /> : <Play size={12} />} label={running ? 'Stop' : 'Start'}
                        onClick={() => choose(() => onComponentAction({ clusterId: member.clusterId }, 'epp', running ? 'stop' : 'start', null, member))} />
                    <MenuAction icon={<Terminal size={12} />} label="View logs" onClick={() => choose(() => onComponentAction({ clusterId: member.clusterId }, 'epp', 'logs', null, member))} />
                </>
            );
        }
        return null;
    };

    return (
        <div className="flex h-full min-h-0 w-full flex-col gap-3">
            <div role="region" aria-label="Service flow canvas" tabIndex={0}
                aria-description="Drag with the left mouse button to pan, or use the scrollbars and arrow keys."
                className="min-h-0 w-full flex-1 cursor-grab select-none overflow-auto focus-visible:outline-2 focus-visible:outline-theme-border active:cursor-grabbing"
                onPointerDown={startDrag} onPointerMove={moveDrag}
                onPointerUp={finishDrag} onPointerCancel={finishDrag} onLostPointerCapture={finishDrag}>
                <div className="mx-auto w-max">
                <div className="relative" style={{ width: WIDTH, height, minWidth: WIDTH }}>
                    {layout.map((section) => (
                        <Panel key={section.cluster.clusterId} padding="none" tone="subtle"
                            className="pointer-events-none absolute rounded-2xl shadow-none" aria-hidden="true"
                            style={{ left: CLUSTER.x, top: section.headerY, width: CLUSTER.w, height: section.height }} />
                    ))}
                    <svg className="pointer-events-none absolute inset-0" width={WIDTH} height={height} viewBox={`0 0 ${WIDTH} ${height}`}>
                        {layout.length > 0 && (
                            <>
                                <Flow width={1.4} points={[
                                    [CLIENTS.x + CLIENTS.w, CLIENTS.y + CLIENTS.h / 2],
                                    [CLIENT_BUS, CLIENTS.y + CLIENTS.h / 2],
                                    [CLIENT_BUS, lastGwMid - 14],
                                    [GW.x, lastGwMid - 14],
                                ]} />
                                {layout.slice(0, -1).map((section) => (
                                    <Flow key={`c-${section.cluster.clusterId}`} width={1.4} d={`M ${CLIENT_BUS} ${section.gwY + GW.h / 2 - 14} H ${GW.x}`} />
                                ))}
                                <Flow tone="green" points={[
                                    [CP.x, CP.y + CP.h / 2],
                                    [CP_TRUNK_X, CP.y + CP.h / 2],
                                    [CP_TRUNK_X, lastGwMid + 14],
                                    [GW.x, lastGwMid + 14],
                                ]} />
                                {layout.slice(0, -1).map((section) => (
                                    <Flow key={`cp-${section.cluster.clusterId}`} tone="green"
                                        d={`M ${CP_TRUNK_X} ${section.gwY + GW.h / 2 + 14} H ${GW.x}`} />
                                ))}
                            </>
                        )}
                        {layout.map((section) => {
                            const gwMid = section.gwY + GW.h / 2;
                            const gwRight = GW.x + GW.w;
                            return (
                                <g key={section.cluster.clusterId}>
                                    <Flow tone="green" d={`M ${IPP.x + IPP.w / 2} ${section.ippY} V ${section.gwY + GW.h}`} />
                                    {section.rows.length > 0 && <Flow width={1.4} d={`M ${gwRight} ${gwMid} H ${GW_BUS}`} />}
                                    {section.rows.map((row) => (
                                        <Flow key={row.serviceId} width={1.4}
                                            dotted={row.serviceStatus === 'disabled' || trafficTotal(row.members) === 0}
                                            tone={row.serviceStatus === 'disabled' || trafficTotal(row.members) === 0 ? 'slate' : 'blue'}
                                            points={[[GW_BUS, gwMid], [GW_BUS, row.y + row.routeY + BOX_H / 2], [MODEL.x, row.y + row.routeY + BOX_H / 2]]} />
                                    ))}
                                </g>
                            );
                        })}
                    </svg>

                    <Box x={CP.x} y={CP.y} w={CP.w} h={CP.h} label="Lens control plane" icon={<Waypoints size={13} className="shrink-0 text-slate-400" />}
                        help="Lens manages the model catalog, API tokens and access permissions, and reconciles model publication into cluster routing configuration. It is the management layer, not the inference proxy: client inference traffic goes to the cluster Gateway."
                        helpPlacement="bottom"
                        status="bg-emerald-400" statusText="Lens control plane: connected" />
                    <Box x={CLIENTS.x} y={CLIENTS.y} w={CLIENTS.w} h={CLIENTS.h} label="Clients / SDK" icon={<Waypoints size={13} className="shrink-0 text-slate-400" />} sub="Gateway URL + token" />

                    {layout.map((section) => {
                        const cluster = section.cluster;
                        const ippState = ippDot(cluster);
                        const ippRunning = ippState === 'active';
                        const ippDetail = cluster.ippState !== 'installed'
                            ? statusLabel(cluster.ippState)
                            : ippRunning ? 'running' : 'installed, stopped';
                        const gwState = clusterDot(cluster);
                        const gwDetail = gatewayDetail(cluster);
                        return (
                            <div key={cluster.clusterId} role="group" aria-label={`Cluster ${cluster.clusterName || cluster.clusterId}`}>
                                <div className="absolute flex items-center gap-2.5 px-4"
                                    style={{ left: CLUSTER.x, top: section.headerY, width: CLUSTER.w, height: CLUSTER.header }}>
                                    <Boxes size={16} className="shrink-0 text-theme-muted" aria-hidden="true" />
                                    <span className="truncate text-sm font-semibold text-theme-text" title={cluster.clusterName || cluster.clusterId}>
                                        {cluster.clusterName || cluster.clusterId}
                                    </span>
                                </div>
                                <Box x={GW.x} y={section.gwY} w={GW.w} label="Gateway" icon={<Network size={13} className="shrink-0 text-slate-400" />}
                                    help="Gateway exposes the inference endpoint, routes requests to InferencePools using HTTPRoute rules, and works with EPP to select model-server endpoints."
                                    helpSource="https://github.com/llm-d/llm-d/blob/f83d4f331ec87a82fa11c84449738705ac4d1424/docs/infrastructure/gateway/README.md"
                                    status={statusDot[gwState] || statusDot.unknown}
                                    statusText={`Gateway: ${gwDetail} (${statusLabel(gwState)})`}
                                    menuOpen={menu?.key === `gw:${cluster.clusterId}`} onToggleMenu={(event) => toggleMenu(`gw:${cluster.clusterId}`, event)}
                                    menuPermissions={['model-service:gateway:manage', 'model-service:gateway:reconcile']} />
                                <Box x={IPP.x} y={section.ippY} w={IPP.w} label="IPP" icon={<Waypoints size={13} className="shrink-0 text-slate-400" />}
                                    help={
                                        <>
                                            <span className="block font-semibold">Inference Payload Processor (IPP)</span>
                                            <span className="mt-1 block">
                                                The Inference Payload Processor (IPP) is a pluggable framework for processing inference request and response payloads before and after routing decisions.
                                            </span>
                                            <span className="mt-2 block">
                                                It integrates with the proxy via Envoy&apos;s ext-proc protocol to inspect and modify headers, bodies, and trailers. Model-aware routing is one use case; EPP selects the pod within the chosen pool.
                                            </span>
                                        </>
                                    }
                                    helpSource="https://github.com/llm-d/llm-d/blob/f83d4f331ec87a82fa11c84449738705ac4d1424/docs/architecture/router/ipp.md"
                                    status={statusDot[ippState] || statusDot.unknown}
                                    statusText={`IPP: ${ippDetail} (${statusLabel(ippState)})`}
                                    menuOpen={menu?.key === `ipp:${cluster.clusterId}`} onToggleMenu={(event) => toggleMenu(`ipp:${cluster.clusterId}`, event)} />
                                {section.rows.map((row) => {
                                    const routeKey = `route:${cluster.clusterId}:${row.serviceId}`;
                                    const modelKey = `model:${cluster.clusterId}:${row.serviceId}`;
                                    const totalWeight = trafficTotal(row.members);
                                    const stopped = row.serviceStatus === 'disabled';
                                    const routed = !stopped && totalWeight !== 0;
                                    const routedMember = row.members.find((member) => member.status !== 'disabled' && member.weight > 0);
                                    const routeState = routed ? componentName(routedMember || row.members[0], 'httpRoute') : 'stopped';
                                    return (
                                        <Panel key={row.serviceId} padding="none" tone="soft" className="model-service-node absolute shadow-none"
                                            role="group" aria-label={`Model ${row.serviceName}`}
                                            style={{ left: MODEL.x, top: row.y, width: MODEL.w, height: row.height }}>
                                            <svg className="pointer-events-none absolute inset-0" width={MODEL.w} height={row.height}>
                                                <Flow width={1.4} dotted={!routed} tone={routed ? 'blue' : 'slate'}
                                                    d={`M ${COL.httpRoute + WB.httpRoute - MODEL.x} ${row.routeY + BOX_H / 2} H ${ROUTE_BRANCH_X}`} />
                                                {row.members.map((member, index) => (
                                                    <g key={member.memberId}>
                                                        <Flow width={1.4} dotted={!routed || member.weight === 0 || member.status === 'disabled'}
                                                            tone={!routed || member.weight === 0 || member.status === 'disabled' ? 'slate' : 'blue'}
                                                            points={[[ROUTE_BRANCH_X, row.routeY + BOX_H / 2], [ROUTE_BRANCH_X, COMPONENT_Y + index * (DEPLOYMENT_H + DEPLOYMENT_GAP) + BOX_H / 2], [DEPLOYMENT.x - MODEL.x, COMPONENT_Y + index * (DEPLOYMENT_H + DEPLOYMENT_GAP) + BOX_H / 2]]} />
                                                        {totalWeight !== null && <text
                                                            x={(ROUTE_BRANCH_X + DEPLOYMENT.x - MODEL.x) / 2}
                                                            y={COMPONENT_Y + index * (DEPLOYMENT_H + DEPLOYMENT_GAP) + BOX_H / 2 - 8}
                                                            textAnchor="middle" className="fill-current text-[10px] text-theme-muted">
                                                            <title>Configured request share; not measured traffic</title>
                                                            {trafficShare(member, totalWeight)}
                                                        </text>}
                                                    </g>
                                                ))}
                                            </svg>
                                            {onEditTraffic && row.members.length > 0 && (
                                                <PermissionGate permission="model-service:member:manage">
                                                    <Button variant="sky" size="icon"
                                                        className="absolute z-10 h-7 w-7 -translate-x-1/2 -translate-y-1/2 rounded-full p-0"
                                                        style={{ left: ROUTE_BRANCH_X, top: row.routeY + BOX_H / 2 }}
                                                        aria-label={`${row.serviceName} traffic distribution`}
                                                        title={TRAFFIC_HELP} disabled={publicationPending}
                                                        onClick={() => onEditTraffic(row.serviceId)}>
                                                        <GitBranch size={14} aria-hidden="true" />
                                                    </Button>
                                                </PermissionGate>
                                            )}
                                            <div className="flex h-9 items-center gap-2 px-4">
                                                <span className="truncate text-xs font-semibold text-theme-text" title={row.serviceName}>{row.serviceName}</span>
                                                <StatusChip variant="compact" className="shrink-0" status={routed ? 'active' : 'inactive'}
                                                    label={stopped ? 'Stopped' : routed ? 'Serving' : 'No traffic'} />
                                                {(onEditTraffic || onToggleService || onSetProvidersStatus || onRemoveModel || onManageAccess) && <PermissionGate permission={[
                                                    ...(onEditTraffic || onSetProvidersStatus ? ['model-service:member:manage'] : []),
                                                    ...(onToggleService || onRemoveModel ? ['model-service:group:manage'] : []),
                                                    ...(onManageAccess ? ['model-service:group:share'] : []),
                                                ]}>
                                                    <Button variant="ghost" size="icon" className="shrink-0"
                                                        aria-label={`${row.serviceName} actions`} aria-haspopup="menu"
                                                        aria-expanded={menu?.key === modelKey}
                                                        onClick={(event) => toggleMenu(modelKey, event)}>
                                                        <Menu size={12} />
                                                    </Button>
                                                </PermissionGate>}
                                            </div>
                                            <Box x={COL.httpRoute - MODEL.x} y={row.routeY} w={WB.httpRoute} label="HTTPRoute" icon={<GitBranch size={13} className="shrink-0 text-slate-400" />} crd
                                                help="HTTPRoute defines rules for routing HTTP requests from a Gateway listener to backend resources."
                                                helpSource="https://github.com/kubernetes-sigs/gateway-api/blob/main/site/content/en/reference/api-types/httproute.md"
                                                status={statusDot[routeState] || statusDot.unknown} statusText={`HTTPRoute: ${statusLabel(routeState)}`}
                                                menuOpen={menu?.key === routeKey} onToggleMenu={(event) => toggleMenu(routeKey, event)} />
                                            {row.members.length === 0 && <p className="absolute text-xs text-theme-muted"
                                                style={{ left: DEPLOYMENT.x - MODEL.x, top: COMPONENT_Y, width: DEPLOYMENT.w }}>
                                                No providers. Add providers through Traffic distribution, or remove this model.
                                            </p>}
                                            {row.members.map((member, index) => {
                                                const name = (key) => componentName(member, key);
                                                const dot = (key) => statusDot[name(key)] || statusDot.unknown;
                                                const eppKey = `epp:${member.memberId}`;
                                                const deploymentKey = `deployment:${member.memberId}`;
                                                return (
                                                    <Panel key={member.memberId} padding="none" tone="inset" className="model-service-node absolute shadow-none"
                                                        role="group" aria-label={`Deployment ${member.name}`}
                                                        style={{ left: DEPLOYMENT.x - MODEL.x, top: MODEL.header + index * (DEPLOYMENT_H + DEPLOYMENT_GAP), width: DEPLOYMENT.w, height: DEPLOYMENT_H }}>
                                                        <div className="flex h-9 items-center gap-2 px-4">
                                                            <span className="truncate text-xs font-semibold text-theme-text" title={member.name}>{member.name}</span>
                                                            {(onNavigate || onToggleDeployment || onDeleteDeployment) && <PermissionGate permission={[
                                                                ...(onNavigate ? ['deployment:run:read'] : []),
                                                                ...(onToggleDeployment || onDeleteDeployment ? ['model-service:member:manage'] : []),
                                                            ]}>
                                                                <Button variant="ghost" size="icon" className="shrink-0"
                                                                    aria-label={`${member.name} deployment actions`} aria-haspopup="menu"
                                                                    aria-expanded={menu?.key === deploymentKey}
                                                                    onClick={(event) => toggleMenu(deploymentKey, event)}>
                                                                    <Menu size={12} />
                                                                </Button>
                                                            </PermissionGate>}
                                                            <StatusChip variant="compact" className="ml-auto"
                                                                status={member.status === 'disabled' || member.weight === 0 ? 'inactive' : 'active'}
                                                                label={member.status === 'disabled' ? 'Offline' : member.weight === 0 ? 'Standby' : 'Online'} />
                                                        </div>
                                                        <Box x={COL.pool - DEPLOYMENT.x} y={DEPLOYMENT.header} w={WB.pool} label="InferencePool" icon={<Layers size={13} className="shrink-0 text-slate-400" />} crd
                                                            help="The InferencePool API defines a group of Pods (containers) dedicated to serving AI models."
                                                            helpSource="https://github.com/kubernetes-sigs/gateway-api-inference-extension/blob/db75ec81b80f8728835b56cdb445e8ff1fbfd430/site-src/api-types/inferencepool.md"
                                                            status={dot('inferencePool')} statusText={`InferencePool: ${statusLabel(name('inferencePool'))}`} />
                                                        <Box x={COL.epp - DEPLOYMENT.x} y={DEPLOYMENT.header} w={WB.epp} label="EPP" icon={<Router size={13} className="shrink-0 text-slate-400" />} status={dot('epp')}
                                                            help="EPP selects the model-server pod within an InferencePool that should process each request, using KV-cache utilization, prefix-cache locality, queue depth, and active request counts."
                                                            helpSource="https://github.com/llm-d/llm-d/blob/f83d4f331ec87a82fa11c84449738705ac4d1424/docs/architecture/router/epp.md"
                                                            statusText={`EPP: ${statusLabel(name('epp'))}`} menuOpen={menu?.key === eppKey} onToggleMenu={(event) => toggleMenu(eppKey, event)} />
                                                        <Box x={COL.model - DEPLOYMENT.x} y={DEPLOYMENT.header} w={WB.model} label="model server" icon={<Cpu size={13} className="shrink-0 text-slate-400" />} status={dot('modelServer')}
                                                            help="Model servers load model weights, execute prefill and decode forward passes, manage the local KV cache, and expose an OpenAI-compatible HTTP API."
                                                            helpSource="https://github.com/llm-d/llm-d/blob/f83d4f331ec87a82fa11c84449738705ac4d1424/docs/architecture/model-servers/README.md"
                                                            statusText={`model server: ${statusLabel(name('modelServer'))}`} />
                                                    </Panel>
                                                );
                                            })}
                                        </Panel>
                                    );
                                })}
                            </div>
                        );
                    })}

                    {clusters.length === 0 && (
                        <div className="absolute text-xs text-slate-400" style={{ left: GW.x, top: CP.y + CP.h + 40 }}>
                            No cluster with model services yet
                        </div>
                    )}
                </div>
                </div>
            </div>

            {menu && typeof document !== 'undefined' && createPortal(
                <>
                    <button type="button" aria-label="Close menu" className="fixed inset-0 z-[100] cursor-default" onClick={() => setMenu(null)} />
                    <div role="menu" style={{ top: menu.top, right: menu.right }} className="fixed z-[110] w-56 rounded-lg border border-slate-700/60 bg-slate-900 p-1 shadow-2xl">
                        {menuItems()}
                    </div>
                </>,
                document.body,
            )}
        </div>
    );
}

export default DataPlaneTopology;

const FLOW_TONES = {
    blue: {
        base: 'stroke-blue-500',
        trail: 'stroke-sky-500 dark:stroke-sky-400',
        head: 'stroke-sky-600 dark:stroke-sky-100',
    },
    green: {
        base: 'stroke-emerald-500',
        trail: 'stroke-emerald-500 dark:stroke-emerald-400',
        head: 'stroke-emerald-600 dark:stroke-emerald-100',
    },
    slate: {
        base: 'stroke-slate-600',
    },
};

function roundedPath(points) {
    const vertices = points.filter((point, index) => index === 0 || point.some((value, axis) => value !== points[index - 1][axis]));
    let cursor = vertices[0];
    const commands = [`M ${cursor[0]} ${cursor[1]}`];
    const lineTo = (point) => {
        if (point[1] === cursor[1]) commands.push(`H ${point[0]}`);
        else if (point[0] === cursor[0]) commands.push(`V ${point[1]}`);
        else commands.push(`L ${point[0]} ${point[1]}`);
        cursor = point;
    };
    for (let index = 1; index < vertices.length - 1; index += 1) {
        const previous = vertices[index - 1];
        const corner = vertices[index];
        const next = vertices[index + 1];
        const incoming = Math.hypot(corner[0] - previous[0], corner[1] - previous[1]);
        const outgoing = Math.hypot(next[0] - corner[0], next[1] - corner[1]);
        const radius = Math.min(12, incoming / 2, outgoing / 2);
        const start = corner.map((value, axis) => value + (previous[axis] - value) * radius / incoming);
        const end = corner.map((value, axis) => value + (next[axis] - value) * radius / outgoing);
        lineTo(start);
        commands.push(`Q ${corner[0]} ${corner[1]} ${end[0]} ${end[1]}`);
        cursor = end;
    }
    if (vertices.length > 1) lineTo(vertices.at(-1));
    return commands.join(' ');
}

export function Flow({ d, points, dashed = false, dotted = false, tone = 'blue', width = 1.1 }) {
    const colors = FLOW_TONES[tone] || FLOW_TONES.blue;
    const path = points ? roundedPath(points) : d;
    const dash = dotted ? '1 4' : dashed ? '4 4' : undefined;
    const animated = !dotted && tone !== 'slate';
    const motion = 'lens-chain-flow motion-reduce:hidden';
    const phase = points ? (points[0][0] + points[0][1]) % 160 : 0;
    return (
        <g fill="none" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
            style={{ '--flow-delay': `${-phase / 40}s` }}>
            <path d={path} className={colors.base} strokeWidth={width} strokeDasharray={dash}
                opacity={dotted || dashed ? 0.65 : 0.25} />
            {animated && (
                <>
                    <path d={path} className={cn(motion, colors.trail, 'blur-[2px]')}
                        strokeDasharray="21 139" strokeWidth={width + 2.5} opacity="0.15" />
                    {[0.1, 0.25, 0.5].map((opacity, index) => (
                        <path key={index} d={path} className={cn(motion, colors.trail)}
                            style={{ '--flow-offset': `${-index * 6}px` }}
                            strokeDasharray="6 154" strokeWidth={width} opacity={opacity} />
                    ))}
                    <path d={path} className={cn(motion, 'lens-chain-flow-head', colors.head)}
                        strokeDasharray="3 157" strokeWidth={width + 0.3} opacity="0.9" />
                </>
            )}
        </g>
    );
}
