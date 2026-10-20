import { useEffect, useMemo, useRef, useState } from 'react';
import { capabilitySources } from '../../features/inference-designer/capabilitySources';
import { inferenceDetails } from '../../features/inference-designer/inferenceDetails';
import { X, RotateCcw, RotateCw, Plus, Minus, Crosshair, MousePointer2 } from 'lucide-react';
import { inferenceScene, TIERS } from '../../features/inference-designer/sceneGraph';
import { createInferenceScene } from './inferenceSceneRenderer';
import { Button } from '../../shared/ui/index';

export function InferenceFlowMap({ draft, clusterName, selected, onSelect, visible, focus, cluster, profile, result, catalog, blockedReason, onActivate, canConfigure, onConfigure, onTarget }) {
    const host = useRef(null), scene = useRef(null), pose = useRef(null), selection = useRef({ selected, visible: TIERS.map(tier => tier.id) });
    const [failure, setFailure] = useState('');
    const [hovered, setHovered] = useState(null);
    const detail = useRef(null);
    const activateRef = useRef(onActivate);
    useEffect(() => { activateRef.current = onActivate; }, [onActivate]);
    const focusRef = useRef(focus);
    const [theme, setTheme] = useState(0);
    const graph = useMemo(() => inferenceScene(draft, clusterName), [draft, clusterName]);
    const entity = [...graph.nodes, ...graph.capabilities].find(item => item.id === (selected || hovered));
    const recipe = catalog?.guides.find(item => item.id === entity?.guide);
    const sourceLinks = capabilitySources(entity, catalog, draft);
    const enabled = entity?.enabled || entity?.inherited;
    const rows = inferenceDetails(entity, graph, draft, cluster, profile, result);
    useEffect(() => { focusRef.current = focus; if (focus) scene.current?.focus(focus.id); }, [focus]);
    useEffect(() => {
        const observer = new MutationObserver(() => setTheme(value => value + 1));
        observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });
        return () => observer.disconnect();
    }, []);
    useEffect(() => {
        // Initialize on the next frame, after the floating layout has a measured viewport.
        const frame = requestAnimationFrame(() => {
            try {
                scene.current = createInferenceScene(host.current, graph, id => activateRef.current(id), pose.current, detail.current, setHovered);
                scene.current.select(selection.current.selected); scene.current.showTiers(selection.current.visible);
                if (focusRef.current) scene.current.focus(focusRef.current.id);
                setFailure('');
            } catch { setFailure('WebGL is unavailable. Use the layer list on the right to inspect modules and optimizations.'); }
        });
        return () => { cancelAnimationFrame(frame); if (scene.current) { pose.current = scene.current.dispose(); scene.current = null; } };
    }, [graph, onSelect, theme]);
    useEffect(() => { selection.current = { selected, visible }; scene.current?.select(selected); scene.current?.showTiers(visible); }, [selected, visible, graph, theme]);
    return <div className="inference-map" onKeyDown={event => { if (event.key === 'Escape') onSelect(null); }}>
        <div ref={host} className="inference-webgl" />
        <div className="inference-map-tools inference-glass" aria-label="Camera controls">
            <Button variant="ghost" size="icon" title="Zoom in" aria-label="Zoom in" onClick={() => scene.current?.zoom(0.85)}><Plus size={16} /></Button>
            <Button variant="ghost" size="icon" title="Zoom out" aria-label="Zoom out" onClick={() => scene.current?.zoom(1.15)}><Minus size={16} /></Button>
            <Button variant="ghost" size="icon" title="Rotate left" aria-label="Rotate left" onClick={() => scene.current?.rotate(-0.18)}><RotateCcw size={16} /></Button>
            <Button variant="ghost" size="icon" title="Rotate right" aria-label="Rotate right" onClick={() => scene.current?.rotate(0.18)}><RotateCw size={16} /></Button>
            <Button variant="ghost" size="icon" title="Reset camera" aria-label="Reset camera" onClick={() => { onSelect(null); setHovered(null); scene.current?.reset(); }}><Crosshair size={16} /></Button>
        </div>
        <div ref={detail} className="inference-node-detail inference-glass" role="region" aria-label="Node details" data-hover={!selected} hidden={!entity}>
            {entity && <>
                <div className="inference-detail-head"><div><strong>{entity.label}</strong><small>{entity.kind === 'module' ? entity.tier === 'flow' ? 'Logical inference stage' : 'Serving / platform component' : entity.edge ? 'Relationship optimization' : 'Module optimization'}</small></div><Button variant="ghost" size="icon" aria-label="Close node details" onClick={() => { onSelect(null); setHovered(null); }}><X size={14} /></Button></div>
                <dl className="inference-detail-rows">{rows.map(row => <div key={row.label}><dt>{row.label}</dt><dd title={row.value}>{row.value}</dd></div>)}</dl>
                <p className="inference-detail-description">{entity.detail}</p>
                {sourceLinks.length > 0 && <div className="inference-node-sources">{sourceLinks.map(link => <a className="inference-node-source block" key={link.href} href={link.href} target="_blank" rel="noreferrer">{link.label} ↗</a>)}</div>}
                {selected && <div className="inference-popup-actions">
                    {entity.kind === 'capability' && <>
                        {recipe?.disposition?.kind === 'recipe' || entity.id === 'tracing' ? <p className="text-xs text-theme-muted">{blockedReason || (entity.required ? 'Required by another selected optimization. Remove that dependent optimization first.' : enabled ? 'Click again to disable this optimization.' : 'Click to enable this optimization and its dependencies.')}</p> : <p className="text-xs text-theme-muted">{recipe?.disposition?.reason || 'No deployable recipe in this source revision.'}</p>}
                        {blockedReason && <p role="alert" className="text-xs text-theme-danger">{blockedReason}</p>}
                    </>}
                    {canConfigure(entity) && <Button size="sm" variant="secondary" onClick={onConfigure}>Configure parameters</Button>}
                    {['cluster', 'workers'].includes(entity.id) && <Button size="sm" variant="secondary" onClick={onTarget}>Configure deployment target</Button>}
                </div>}
            </>}
        </div>
        <div className="inference-map-hint"><MousePointer2 size={13} aria-hidden="true" /><span>Click a block to explore · Use Tiers to choose optimizations<small>Drag to orbit · Scroll to zoom · Animated paths show planned flow</small></span></div>
        {failure && <div role="alert" className="inference-webgl-error inference-glass">{failure}</div>}

    </div>;
}
