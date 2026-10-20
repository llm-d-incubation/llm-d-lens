import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CHART_SERIES } from '../../shared/ui/charts/palette';
import { sceneRelations } from '../../features/inference-designer/sceneGraph';

/** Scene-only rendering. React owns the Tiers tree and anchored information card. */
export function createInferenceScene(host, graph, onSelect, previousCamera, detailElement, onHover) {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 300);
    const home = new THREE.Vector3(19, 35, 68), center = new THREE.Vector3(0, 10, 0);
    camera.position.fromArray(previousCamera?.position || home.toArray());
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.domElement.setAttribute('aria-label', '3D inference deployment scene');
    renderer.domElement.setAttribute('role', 'img');
    host.append(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.fromArray(previousCamera?.target || center.toArray());
    controls.enableDamping = true; controls.minDistance = 10; controls.maxDistance = 140;
    controls.minPolarAngle = 0.18; controls.maxPolarAngle = Math.PI / 2 - 0.05;
    controls.update();
    scene.add(new THREE.AmbientLight(0xffffff, 2));
    const light = new THREE.DirectionalLight(0xffffff, 3); light.position.set(12, 40, 20); scene.add(light);
    const root = new THREE.Group(); scene.add(root);
    const meshes = new Map(), connections = [], tiers = new Map(), planes = new Map();
    const css = getComputedStyle(document.documentElement);
    const neutral = css.getPropertyValue('--text-muted').trim() || 'slategray';
    const card = css.getPropertyValue('--bg-card').trim() || 'white';
    const accent = css.getPropertyValue('--brand-accent').trim() || CHART_SERIES[0];
    const all = [...graph.nodes, ...graph.capabilities.filter(item => item.previewVisible !== false)];
    const meshFor = id => meshes.get(id) || meshes.get(graph.capabilities.find(item => item.id === id)?.owner);
    const labels = [], pulses = [];
    const colorFor = item => CHART_SERIES[item.colorIndex ?? 0];
    graph.tiers.forEach((tier, index) => {
        const group = new THREE.Group(); root.add(group); tiers.set(tier.id, group);
        const plane = new THREE.Mesh(new THREE.BoxGeometry(52, 0.15, 28), new THREE.MeshStandardMaterial({ color: CHART_SERIES[index], transparent: true, opacity: 0.07, depthWrite: false }));
        plane.userData.id = tier.id;
        plane.position.set(0, tier.y - 0.7, 0); group.add(plane); planes.set(tier.id, plane);
        const rim = new THREE.LineSegments(new THREE.EdgesGeometry(plane.geometry), new THREE.LineBasicMaterial({ color: CHART_SERIES[index], transparent: true, opacity: 0.3 }));
        rim.position.copy(plane.position); group.add(rim);
    });
    const curveFor = edge => {
        const a = graph.nodes.find(node => node.id === edge.from), b = graph.nodes.find(node => node.id === edge.to);
        const start = new THREE.Vector3(...a.at), end = new THREE.Vector3(...b.at); start.y += 0.5; end.y += 0.5;
        const middle = start.clone().lerp(end, 0.5); middle.y += edge.kind === 'placement' ? 0 : 1.8;
        if (edge.kind === 'response') { middle.z = -12; middle.y += 2; }
        if (edge.kind === 'iteration') return new THREE.CubicBezierCurve3(start, start.clone().add(new THREE.Vector3(4, 3, -4)), end.clone().add(new THREE.Vector3(-4, 3, -4)), end);
        return new THREE.QuadraticBezierCurve3(start, middle, end);
    };
    all.forEach(item => {
        const group = tiers.get(item.tier), capability = item.kind === 'capability';
        const geometry = capability ? new THREE.OctahedronGeometry(0.55) : new THREE.BoxGeometry(2.1, 1.7, 2.1);
        const enabled = capability ? item.enabled || item.inherited : item.inPlan;
        const material = new THREE.MeshStandardMaterial({ color: item.unavailable ? neutral : colorFor(item), metalness: 0.12, roughness: 0.45, transparent: true, opacity: enabled ? 0.95 : 0.48, emissive: enabled && capability ? accent : card, emissiveIntensity: enabled && capability ? 0.18 : 0.02 });
        if (!capability) {
            const attached = graph.capabilities.filter(cap => cap.owner === item.id);
            const depth = attached.length ? 8.4 : 4.4;
            const tile = new THREE.Mesh(new THREE.BoxGeometry(6.8, 0.12, depth), new THREE.MeshStandardMaterial({ color: enabled ? colorFor(item) : neutral, transparent: true, opacity: 0.1, depthWrite: false }));
            tile.position.set(item.at[0], item.at[1] - 0.42, item.at[2] + depth / 2 - 2);
            group.add(tile);
            const border = new THREE.LineSegments(new THREE.EdgesGeometry(tile.geometry), new THREE.LineBasicMaterial({ color: enabled ? colorFor(item) : neutral, transparent: true, opacity: 0.3 }));
            border.position.copy(tile.position); group.add(border);
        }
        const mesh = new THREE.Mesh(geometry, material);
        if (item.edge) mesh.position.copy(curveFor(graph.edges.find(edge => edge.id === item.edge)).getPoint(0.5));
        else { mesh.position.set(...item.at); mesh.position.y += capability ? 0 : 0.45; }
        mesh.userData.id = item.id; group.add(mesh); meshes.set(item.id, mesh);
        if (item.enabled && capability || item.inPlan) {
            const ring = new THREE.Mesh(new THREE.RingGeometry(capability ? 0.7 : 1.55, capability ? 0.83 : 1.68, 32), new THREE.MeshBasicMaterial({ color: colorFor(item), transparent: true, opacity: 0.45, side: THREE.DoubleSide }));
            ring.rotation.x = -Math.PI / 2; ring.position.copy(mesh.position); ring.position.y = item.at[1] - 0.28; group.add(ring);
            pulses.push({ ring, item });
        }
        if (!capability) {
            const label = document.createElement('div');
            label.className = 'inference-scene-label';
            label.setAttribute('aria-hidden', 'true');
            label.style.setProperty('--node-color', colorFor(item));
            const title = document.createElement('span'), caption = document.createElement('small');
            title.textContent = item.label;
            caption.textContent = item.previewOverflow ? `+${item.previewOverflow} more · Tiers` : item.caption;
            label.append(title, caption); host.append(label);
            labels.push({ label, item, mesh });
        }
        const outline = new THREE.LineSegments(new THREE.EdgesGeometry(geometry), new THREE.LineBasicMaterial({ color: enabled ? colorFor(item) : neutral, transparent: true, opacity: 0.65 })); mesh.add(outline);
        if (capability && item.owner) {
            const owner = graph.nodes.find(node => node.id === item.owner);
            const tether = new THREE.Line(new THREE.BufferGeometry().setFromPoints([mesh.position.clone(), new THREE.Vector3(...owner.at)]), new THREE.LineDashedMaterial({ color: enabled ? colorFor(item) : neutral, dashSize: 0.2, gapSize: 0.25, transparent: true, opacity: 0.2 })); tether.computeLineDistances(); group.add(tether);
        }
    });
    graph.edges.forEach(edge => {
        const a = graph.nodes.find(node => node.id === edge.from), b = graph.nodes.find(node => node.id === edge.to), curve = curveFor(edge);
        const color = edge.kind === 'request' ? CHART_SERIES[0] : edge.kind === 'state' ? CHART_SERIES[1] : neutral;
        const material = edge.kind === 'request' ? new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.6 }) : new THREE.LineDashedMaterial({ color, dashSize: 0.35, gapSize: 0.25, transparent: true, opacity: 0.3 });
        const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(40)), material); line.computeLineDistances(); root.add(line);
        const packet = new THREE.Mesh(new THREE.SphereGeometry(0.12, 6, 6), new THREE.MeshBasicMaterial({ color })); root.add(packet);
        connections.push({ line, packet, edge, a, b, curve });
    });
    let selected = null, hovered = null, hidden = new Set(), goal = null, tierFlash = null;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    const highlight = () => {
        const id = selected || hovered;
        const relation = sceneRelations(graph, id), visibleIds = new Set(relation.visibleEdges.map(edge => edge.id));
        for (const item of all) {
            const mesh = meshes.get(item.id), active = item.id === id || mesh === meshFor(id);
            mesh.scale.setScalar(active ? 1.18 : 1);
            mesh.material.emissive.set(item.enabled || item.inPlan ? accent : card);
            mesh.material.emissiveIntensity = active ? 0.7 : relation.highlighted.has(item.id) ? 0.2 : item.enabled || item.inPlan ? 0.3 : 0.02;
        }
        connections.forEach(({ line, packet, edge, a, b }) => {
            line.visible = visibleIds.has(edge.id) && !hidden.has(a.tier) && !hidden.has(b.tier);
            packet.visible = line.visible && edge.inPlan && edge.enabled !== false && edge.kind !== 'platform' && edge.kind !== 'placement';
            line.material.color.set(edge.inPlan ? accent : neutral);
            line.material.opacity = edge.inPlan ? 0.85 : all.find(item => item.id === id)?.edge === edge.id ? 0.9 : edge.enabled === false ? 0.15 : 0.4;
        });
    };
    const select = id => { selected = id; planes.forEach((plane, key) => { plane.material.opacity = key === id ? 0.16 : 0.07; }); highlight(); };
    const hover = id => { if (id === hovered) return; hovered = id; renderer.domElement.style.cursor = id ? 'pointer' : 'grab'; onHover?.(id); highlight(); };
    const moveTo = (target, distance) => {
        const direction = camera.position.clone().sub(controls.target).normalize();
        goal = { target, position: target.clone().addScaledVector(direction, distance) };
        if (motion.matches) { camera.position.copy(goal.position); controls.target.copy(goal.target); controls.update(); goal = null; }
    };
    const pointer = new THREE.Vector2(), raycaster = new THREE.Raycaster(); let down = null;
    const hitAt = event => {
        const rect = renderer.domElement.getBoundingClientRect(); pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
        raycaster.setFromCamera(pointer, camera);
        return raycaster.intersectObjects([...meshes.values()].filter(mesh => mesh.parent.visible), false)[0]?.object.userData.id || raycaster.intersectObjects([...planes.values()].filter(mesh => mesh.parent.visible), false)[0]?.object.userData.id || null;
    };
    const pointerDown = event => { down = [event.clientX, event.clientY, event.button]; };
    const pointerUp = event => {
        const start = down; down = null;
        if (!start || start[2] !== 0 || Math.hypot(event.clientX - start[0], event.clientY - start[1]) > 5) return;
        onSelect(hitAt(event));
    };
    const pointerMove = event => { if (!down) hover(hitAt(event)); };
    const pointerLeave = () => { down = null; hover(null); };
    const stopMotion = () => { goal = null; hover(null); };
    controls.addEventListener('start', stopMotion);
    const events = { pointerdown: pointerDown, pointerup: pointerUp, pointermove: pointerMove, pointerleave: pointerLeave };
    Object.entries(events).forEach(([name, handler]) => renderer.domElement.addEventListener(name, handler));
    const resize = () => { const width = host.clientWidth, height = host.clientHeight; if (!width || !height) return; camera.aspect = width / height; camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(19)) * Math.max(1, 1.2 / camera.aspect))); camera.updateProjectionMatrix(); renderer.setSize(width, height); };
    const observer = new ResizeObserver(resize); observer.observe(host); resize();
    const anchor = new THREE.Vector3();
    const positionDetail = () => {
        if (!detailElement) return;
        const mesh = meshFor(selected || hovered);
        detailElement.style.visibility = mesh?.parent.visible ? 'visible' : 'hidden';
        if (!mesh?.parent.visible) return;
        mesh.getWorldPosition(anchor); anchor.y += 0.6; anchor.project(camera);
        if (anchor.z < -1 || anchor.z > 1) { detailElement.style.visibility = 'hidden'; return; }
        const width = host.clientWidth, height = host.clientHeight;
        const x = (anchor.x + 1) * width / 2, y = (1 - anchor.y) * height / 2;
        const cardWidth = detailElement.offsetWidth, cardHeight = detailElement.offsetHeight, left = x > width / 2;
        detailElement.dataset.side = left ? 'left' : 'right';
        detailElement.style.left = `${Math.max(8, Math.min(width - cardWidth - 8, left ? x - cardWidth - 22 : x + 22))}px`;
        detailElement.style.top = `${Math.max(8, Math.min(height - cardHeight - 8, y - 24))}px`;
    };
    let previousTime = performance.now();
    renderer.setAnimationLoop(time => {
        if (document.hidden) { previousTime = time; return; }
        const delta = Math.min((time - previousTime) / 1000, 0.05); previousTime = time;
        if (goal) {
            const factor = Math.min(1, delta * 4);
            camera.position.lerp(goal.position, factor); controls.target.lerp(goal.target, factor);
            if (camera.position.distanceTo(goal.position) < 0.15 && controls.target.distanceTo(goal.target) < 0.1) { camera.position.copy(goal.position); controls.target.copy(goal.target); goal = null; }
        }
        if (tierFlash) {
            const age = (time - tierFlash.start) / 1000;
            const plane = planes.get(tierFlash.id);
            const idleOpacity = selected === tierFlash.id ? 0.16 : 0.07;
            plane.material.opacity = age < 2 && !motion.matches ? 0.07 + (1 - age / 2) * (0.07 + 0.04 * Math.sin(age * Math.PI * 3)) : idleOpacity;
            if (age >= 2) tierFlash = null;
        }
        connections.forEach(({ packet, curve }, index) => packet.position.copy(curve.getPoint(motion.matches ? 0.8 : ((time / 5000) + index * 0.13) % 1)));
        pulses.forEach(({ ring, item }, index) => {
            const pulse = !motion.matches && (item.id === 'decode' || item.kind === 'capability') ? (Math.sin(time / 950 + index) + 1) / 2 : 0;
            ring.scale.setScalar(1 + pulse * 0.12);
            ring.material.opacity = 0.3 + pulse * 0.2;
        });
        controls.enableDamping = !motion.matches; controls.update(); renderer.render(scene, camera); positionDetail();
        const occupied = [];
        labels.forEach(({ label, mesh, item }) => {
            mesh.getWorldPosition(anchor); anchor.z += 1.8; anchor.y -= 0.6; anchor.project(camera);
            const x = (anchor.x + 1) * host.clientWidth / 2, y = (1 - anchor.y) * host.clientHeight / 2;
            const width = 112, height = 30;
            const overlaps = occupied.some(rect => Math.abs(rect.x - x) < width && Math.abs(rect.y - y) < height);
            const shown = mesh.parent.visible && anchor.z > -1 && anchor.z < 1 && x > width / 2 && x < host.clientWidth - width / 2 && y > 100 && y < host.clientHeight - 55 && !overlaps;
            label.hidden = !shown;
            if (shown) { occupied.push({ x, y }); label.style.transform = `translate(${x - width / 2}px, ${y}px)`; }
            label.dataset.active = String(item.id === selected || item.id === hovered);
        });
    });
    highlight();
    return {
        select,
        showTiers(ids) { hidden = new Set(graph.tiers.filter(tier => !ids.includes(tier.id)).map(tier => tier.id)); tiers.forEach((group, id) => { group.visible = !hidden.has(id); }); if (hovered && hidden.has(all.find(item => item.id === hovered)?.tier)) hover(null); highlight(); },
        reset() { goal = { position: home.clone(), target: center.clone() }; if (motion.matches) { camera.position.copy(home); controls.target.copy(center); goal = null; } },
        focus(id) {
            const tier = graph.tiers.find(item => item.id === id);
            if (tier) { moveTo(new THREE.Vector3(0, tier.y, 0), 53); if (tierFlash) planes.get(tierFlash.id).material.opacity = 0.07; tierFlash = { id, start: performance.now() }; }
            else if (meshFor(id)) moveTo(meshFor(id).position.clone(), 22);
        },
        zoom(factor) { goal = null; camera.position.sub(controls.target).multiplyScalar(factor).add(controls.target); controls.update(); },
        rotate(angle) { goal = null; camera.position.sub(controls.target).applyAxisAngle(new THREE.Vector3(0, 1, 0), angle).add(controls.target); controls.update(); },
        dispose() {
            const state = { position: camera.position.toArray(), target: controls.target.toArray() };
            observer.disconnect(); renderer.setAnimationLoop(null); controls.removeEventListener('start', stopMotion); controls.dispose();
            Object.entries(events).forEach(([name, handler]) => renderer.domElement.removeEventListener(name, handler));
            root.traverse(object => { object.geometry?.dispose(); if (Array.isArray(object.material)) object.material.forEach(material => material.dispose()); else object.material?.dispose(); });
            renderer.dispose(); renderer.forceContextLoss(); host.replaceChildren(); return state;
        },
    };
}
