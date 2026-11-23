import { useEffect, useEffectEvent, useState } from 'react';
import { Badge, Button, Modal } from '../../../../shared/ui';
import { DeploymentPlanGraph } from '../DeploymentPlanGraph';
import { OPTIMIZATION_STATES } from '../../model/deploymentPlan';
import { servingFlowDiagram } from '../../model/inference-designer/sceneGraph';

/** The diagram owns progressive disclosure and entry into the source parameter editor. */
export function InferenceFlowMap2D({ graph, selected, onSelect, onActivate, onConfigure, canConfigure, blocked = {}, disabled = false, inspector, inspectorFooter, onCloseInspector, toolbar, focus, onFocusOpened, configuredCount, overlay }) {
    const [expandedGroup, setExpandedGroup] = useState(null), [pages, setPages] = useState({}), [inspecting, setInspecting] = useState(false);
    const diagram = servingFlowDiagram(graph, blocked, canConfigure, { expandedGroup, pages });
    const capability = diagram.groups.flatMap(group => group.capabilities).find(cap => cap.id === selected);
    const entity = capability || graph.nodes.find(node => node.id === selected);
    const targets = diagram.edges.filter(edge => edge.hidden && edge.from === selected).map(edge => diagram.nodes.find(node => node.id === edge.to)?.name);
    const close = () => { setInspecting(false); onCloseInspector?.(); };
    const inspectorContent = typeof inspector === 'function' ? inspector({ close }) : inspector;
    const footerContent = typeof inspectorFooter === 'function' ? inspectorFooter({ close }) : inspectorFooter;
    const inspect = id => {
        const cap = diagram.groups.flatMap(group => group.capabilities).find(item => item.id === id);
        const stage = graph.nodes.find(node => node.id === id);
        onSelect?.(id);
        if (!cap && stage && !disabled && canConfigure?.(stage)) onConfigure?.(id);
        if (cap && !disabled && !cap.editDisabled && cap.status !== 'unsupported') onConfigure?.(id);
        setInspecting(true);
    };
    // Opens the settings drawer for an optimization chosen outside the diagram, once editing is possible.
    const openFocus = useEffectEvent(() => { inspect(focus); onFocusOpened?.(); });
    useEffect(() => { if (focus && !disabled) openFocus(); }, [focus, disabled]);
    const settings = <Modal isOpen={inspecting && Boolean(entity)} variant="drawer" size="lg" title={entity?.label} onClose={close}
        subtitle={targets.length ? `Applies to ${targets.join(' + ')}` : undefined} footer={footerContent || <Button variant="secondary" onClick={close}>Close</Button>}>
        {inspectorContent || (entity && <div className="space-y-4">
            {capability && <Badge tone={OPTIMIZATION_STATES[capability.status]?.tone || 'neutral'}>{OPTIMIZATION_STATES[capability.status]?.label || capability.status}</Badge>}
            <p className="text-sm">{blocked[selected] || entity.reason || entity.detail}</p>
            <div className="flex gap-2"><Button variant="secondary" disabled={disabled || entity.editDisabled || entity.status === 'unsupported'} onClick={() => onConfigure?.(selected)}>Edit parameters</Button>
                {capability && <Button variant="secondary" disabled={disabled || capability.toggleDisabled || capability.status === 'unsupported'} onClick={() => onActivate?.(selected)}>{capability.enabled ? 'Disable' : 'Enable'} {capability.label}</Button>}</div>
        </div>)}
    </Modal>;
    return <section id="deployment-serving-flow" className="inference-map inference-map-2d" style={{ position: 'relative' }} aria-label="Model serving flow">
        <header className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold">Serving flow</h3><Badge tone="brand">{configuredCount ?? diagram.configuredCount} enabled</Badge>{diagram.serving && <Badge>{diagram.serving}</Badge>}
            {toolbar}
        </header>
        {/* Floats over the empty top-right margin, outside the diagram border, on wide screens. */}
        {overlay && <div className="mt-3 flex max-h-[8.5rem] xl:absolute xl:-right-10 xl:-top-3 xl:z-20 xl:mt-0 xl:w-64">{overlay}</div>}
        <div id="deployment-serving-stages" className="mt-3"><DeploymentPlanGraph plan={diagram} selected={selected} onSelect={inspect}
            onToggleGroup={id => { setExpandedGroup(current => current === id ? null : id); onSelect?.(id); }}
            onGroupPage={(id, page) => setPages(current => ({ ...current, [id]: page }))} disabled={disabled} /></div>
        <div className="mt-2 text-xs text-theme-muted">
            <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label="Optimization status legend">
                {['enabled', 'dependency', 'user_disabled', 'conflict', 'source_controlled', 'unsupported'].map(id => <li key={id} className="flex items-center gap-1.5" title={OPTIMIZATION_STATES[id].action}><span className="inference-status-dot" style={{ background: OPTIMIZATION_STATES[id].color }} />{OPTIMIZATION_STATES[id].label}</li>)}
            </ul>
        </div>
        {settings}
    </section>;
}
