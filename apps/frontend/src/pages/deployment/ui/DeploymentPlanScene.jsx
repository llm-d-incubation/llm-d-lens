import React, { useEffect, useRef, useState } from 'react';
import { Focus, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react';
import { Badge, Button } from '../../../shared/ui';
import { CHART_SERIES } from '../../../shared/ui/charts/palette';

/** Optional Three.js projection of the same graph. Selection never mutates configuration. */
export default function DeploymentPlanScene({ plan, selected, onSelect }) {
    const host = useRef(null), callback = useRef(onSelect);
    callback.current = onSelect;
    const selection = useRef(selected), highlight = useRef(null);
    const cameraActions = useRef(null);
    selection.current = selected;
    useEffect(() => { highlight.current?.(); }, [selected]);
    const [error, setError] = useState(''), [ready, setReady] = useState(false);
    useEffect(() => {
        let disposed = false;
        const releases = [];
        const cleanup = () => { highlight.current = null; cameraActions.current = null; releases.splice(0).reverse().forEach(release => release()); };
        setError(''); setReady(false);
        Promise.all([import('three'), import('three/addons/controls/OrbitControls.js')]).then(([T, { OrbitControls }]) => {
            if (disposed) return;
            const element = host.current, renderer = new T.WebGLRenderer({ antialias: true, alpha: true });
            releases.push(() => { renderer.dispose(); renderer.domElement.remove(); });
            renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
            element.appendChild(renderer.domElement);
            const scene = new T.Scene(), camera = new T.OrthographicCamera(-15, 15, 12, -12, 0.1, 2000);
            releases.push(() => scene.traverse(object => { object.geometry?.dispose(); object.material?.dispose(); }));
            const tiers = [0, 1, 2].map(tier => plan.nodes.filter(node => node.tier === tier));
            const columns = Math.min(4, Math.max(1, ...tiers.map(nodes => nodes.length)));
            const rows = Math.max(1, ...tiers.map(nodes => Math.ceil(nodes.length / columns)));
            const width = Math.max(12, columns * 4.5 + 2), depth = Math.max(7, rows * 4 + 2);
            const extent = Math.max(12, width * 0.65 + depth * 0.35);
            camera.position.set(22, 23, 28);
            const controls = new OrbitControls(camera, renderer.domElement);
            releases.push(() => controls.dispose());
            controls.target.set(0, 4, 0); controls.minZoom = 0.4; controls.maxZoom = 4;
            controls.maxPolarAngle = Math.PI / 2.05; controls.update();
            scene.add(new T.AmbientLight(CHART_SERIES[1], 2));
            const light = new T.DirectionalLight(CHART_SERIES[2], 4);
            light.position.set(8, 20, 12); scene.add(light);
            const labels = [], objects = [], lines = [], positions = new Map();
            releases.push(() => labels.forEach(label => label.element.remove()));
            const addLabel = (text, position, layer = false) => {
                const label = document.createElement('span');
                label.textContent = text;
                label.className = layer
                    ? 'absolute pointer-events-none text-[10px] font-semibold uppercase tracking-wider text-theme-muted bg-theme-card/90 rounded px-2 py-1'
                    : 'absolute pointer-events-none text-xs text-theme-text bg-theme-card/95 border border-theme-border shadow-sm rounded px-2 py-1 max-w-40 truncate';
                label.style.transform = 'translate(-50%, -100%)'; label.title = text;
                element.appendChild(label); labels.push({ element: label, position });
            };
            for (const [tier, nodes] of tiers.entries()) {
                if (!nodes.length) continue;
                const y = (2 - tier) * 4;
                const platform = new T.Mesh(new T.BoxGeometry(width, 0.15, depth), new T.MeshBasicMaterial({ color: CHART_SERIES[tier], transparent: true, opacity: 0.07, depthWrite: false }));
                platform.position.y = y - 0.6; scene.add(platform);
                const outline = new T.LineSegments(new T.EdgesGeometry(platform.geometry), new T.LineBasicMaterial({ color: CHART_SERIES[tier], transparent: true, opacity: 0.35 }));
                outline.position.copy(platform.position); scene.add(outline);
                addLabel(['Routing / model', 'Serving workloads', 'Storage / configuration'][tier], new T.Vector3(-width / 2 + 2, y - 0.4, depth / 2), true);
                for (const [index, node] of nodes.entries()) {
                    const rowSize = Math.min(columns, nodes.length - Math.floor(index / columns) * columns);
                    const position = new T.Vector3((index % columns - (rowSize - 1) / 2) * 4.5, y, (Math.floor(index / columns) - (Math.ceil(nodes.length / columns) - 1) / 2) * 4);
                    const mesh = new T.Mesh(new T.BoxGeometry(2.6, 0.9, 1.8), new T.MeshStandardMaterial({ color: CHART_SERIES[tier], emissive: CHART_SERIES[tier], roughness: 0.45, metalness: 0.15 }));
                    const border = new T.LineSegments(new T.EdgesGeometry(mesh.geometry), new T.LineBasicMaterial({ color: CHART_SERIES[tier] }));
                    mesh.add(border);
                    addLabel(node.name, position.clone().add(new T.Vector3(0, 1.2, 0)));
                    mesh.position.copy(position); mesh.userData.id = node.id; scene.add(mesh); objects.push(mesh); positions.set(node.id, position);
                }
            }
            for (const edge of plan.edges) {
                const from = positions.get(edge.from), to = positions.get(edge.to);
                if (!from || !to) continue;
                const middle = from.clone().lerp(to, 0.5).add(new T.Vector3(0, 0, 1));
                const curve = new T.QuadraticBezierCurve3(from, middle, to);
                const line = new T.Line(new T.BufferGeometry().setFromPoints(curve.getPoints(24)), new T.LineBasicMaterial({ color: CHART_SERIES[1], transparent: true, opacity: 0.55 }));
                line.userData = edge; scene.add(line); lines.push(line);
            }
            const draw = () => { if (disposed || document.hidden) return; renderer.render(scene, camera);
                for (const label of labels) { const point = label.position.clone().project(camera); label.element.style.left = `${(point.x + 1) / 2 * element.clientWidth}px`; label.element.style.top = `${(1 - point.y) / 2 * element.clientHeight}px`; label.element.hidden = point.z < -1 || point.z > 1; }
            };
            highlight.current = () => {
                objects.forEach(mesh => { const active = mesh.userData.id === selection.current; mesh.material.emissiveIntensity = active ? 0.65 : 0.1; mesh.scale.setScalar(active ? 1.1 : 1); });
                lines.forEach(line => { line.material.opacity = !selection.current || [line.userData.from, line.userData.to].includes(selection.current) ? 0.85 : 0.15; });
                draw();
            };
            const resize = () => {
                const w = element.clientWidth || 600, h = element.clientHeight || 480, aspect = w / h;
                renderer.setSize(w, h); camera.left = -extent * Math.max(1, aspect); camera.right = -camera.left;
                camera.top = extent * Math.max(1, 1 / aspect); camera.bottom = -camera.top;
                camera.updateProjectionMatrix(); draw();
            };
            cameraActions.current = {
                reset: () => { camera.position.set(22, 23, 28); camera.zoom = 1; controls.target.set(0, 4, 0); camera.updateProjectionMatrix(); controls.update(); draw(); },
                focus: () => { const position = positions.get(selection.current); if (!position) return; camera.position.add(position.clone().sub(controls.target)); controls.target.copy(position); camera.zoom = 1.6; camera.updateProjectionMatrix(); controls.update(); draw(); },
                zoom: factor => { camera.zoom = Math.min(controls.maxZoom, Math.max(controls.minZoom, camera.zoom * factor)); camera.updateProjectionMatrix(); draw(); },
            };
            const observer = new ResizeObserver(resize); observer.observe(element);
            releases.push(() => observer.disconnect());
            const ray = new T.Raycaster(); let start = null;
            const down = e => { start = [e.clientX, e.clientY]; };
            const up = e => { if (!start || Math.hypot(e.clientX - start[0], e.clientY - start[1]) > 5) return; const r = renderer.domElement.getBoundingClientRect(); ray.setFromCamera(new T.Vector2((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1), camera); const hit = ray.intersectObjects(objects)[0]; if (hit) callback.current(hit.object.userData.id); };
            renderer.domElement.style.touchAction = 'none';
            renderer.domElement.addEventListener('pointerdown', down); renderer.domElement.addEventListener('pointerup', up);
            controls.addEventListener('change', draw); document.addEventListener('visibilitychange', draw); resize(); highlight.current(); setReady(true);
            releases.push(() => { document.removeEventListener('visibilitychange', draw); renderer.domElement.removeEventListener('pointerdown', down); renderer.domElement.removeEventListener('pointerup', up); });
        }).catch(reason => { cleanup(); if (!disposed) { setReady(false); setError(`3D unavailable: ${reason.message}. Use the 2D view or component list.`); } });
        return () => { disposed = true; cleanup(); };
    }, [plan]);
    return <div className="overflow-hidden rounded-lg border border-theme-border bg-theme-bg">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-theme-border px-4 py-3">
            <div><p className="text-xs font-semibold">Deployment layers</p><p className="mt-1 text-[11px] text-theme-muted">Select a component to inspect or edit it in the sidebar.</p></div>
            <Badge tone="neutral" size="xs">Planned resources</Badge>
        </div>
        <div className="flex flex-wrap gap-1 border-b border-theme-border px-3 py-2" role="group" aria-label="3D camera controls">
            <Button size="xs" variant="ghost" disabled={!ready} onClick={() => cameraActions.current?.reset()}><RotateCcw size={13} />Reset view</Button>
            <Button size="xs" variant="ghost" disabled={!ready || !selected} onClick={() => cameraActions.current?.focus()}><Focus size={13} />Focus selected</Button>
            <Button size="xs" variant="ghost" aria-label="Zoom in" disabled={!ready} onClick={() => cameraActions.current?.zoom(1.25)}><ZoomIn size={14} /></Button>
            <Button size="xs" variant="ghost" aria-label="Zoom out" disabled={!ready} onClick={() => cameraActions.current?.zoom(0.8)}><ZoomOut size={14} /></Button>
        </div>
        {error && <p role="alert" className="p-4 text-sm">{error}</p>}
        <div ref={host} className="relative h-[30rem] overflow-hidden" aria-label="3D deployment relationships" />
        <p className="border-t border-theme-border px-4 py-3 text-[11px] text-theme-muted">Drag to orbit · Right-drag to pan · Scroll to zoom. Layers show configuration, not physical placement.</p>
    </div>;
}
