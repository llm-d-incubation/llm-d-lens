import { useId, useState } from 'react';
import { ArrowRight, Boxes, Cpu, Database, Focus, GitBranch, Layers, MessageSquare, Pencil, Power, RotateCcw, Search, Zap, ZoomIn, ZoomOut } from 'lucide-react';
import { edgeGeometry, layoutPlan, OPTIMIZATION_STATES } from '../model/deploymentPlan';
import { Badge, Button, Input, Panel } from '../../../shared/ui';
import { cn } from '../../../shared/lib/cn';
import { CHART_SERIES } from '../../../shared/ui/charts/palette';

const NODE_ICONS = { request: MessageSquare, routing: GitBranch, prefill: Layers, decode: Zap, response: MessageSquare, cache: Database, pods: Boxes, compute: Cpu };
const EDGE_COLORS = { flow: 0, attachment: 1, dependency: 2, kv: 3 };

/** Native SVG owns the visible geometry; shared buttons supply keyboard/focus behavior. */
function ServingDiagramNode({ node, selected, related, onSelect, onToggleGroup, onGroupPage, onHover, onLeave }) {
    const Icon = NODE_ICONS[node.icon] || Cpu;
    const left = node.x - node.width / 2, top = node.y - node.height / 2;
    const status = OPTIMIZATION_STATES[node.status];
    const active = selected === node.id || node.configureTarget === selected && Boolean(selected);
    const name = node.capability ? `Configure ${node.name}` : `${node.expanded ? 'Collapse' : 'Expand'} ${node.name} settings`;
    const hitHeight = node.container ? node.headerHeight : node.height;
    const inactive = node.capability ? !node.enabled : !node.inPlan;
    return <g className={cn('serving-native-node', active && 'is-selected')} data-node-id={node.id} role="group" aria-label={`${node.kind}: ${node.name}`} onMouseEnter={() => onHover(node.id)} onMouseLeave={onLeave}>
        <rect x={left} y={top} width={node.width} height={node.height} rx={node.capability ? 8 : 14}
            fill={node.capability ? 'var(--bg-primary)' : 'var(--bg-card)'} stroke={active || related ? 'var(--brand-accent)' : 'var(--border-color)'} strokeWidth={active ? 2 : 1} strokeDasharray={inactive ? '4 4' : undefined} />
        {node.capability ? <>
            <circle cx={left + 12} cy={top + 18} r="4" fill={status?.color || 'var(--text-muted)'} />
            <text x={left + 24} y={top + 21} fontSize="12" fontWeight="500" fill="var(--text-primary)" opacity={node.status === 'unsupported' ? 0.6 : 1}>{node.titleLines.map((line, i) => <tspan key={i} x={left + 24} dy={i ? 16 : 0}>{line}</tspan>)}</text>
            <text x={left + 24} y={top + node.height - 10} fontSize="10" fontWeight="600" fill={status?.color || 'var(--text-muted)'}>{status?.label || node.status}</text>
        </> : <>
            <Icon x={left + 16} y={top + 18} width="18" height="18" stroke="var(--brand-accent)" />
            <text x={left + 44} y={top + 32} fontSize="13" fontWeight="600" fill="var(--text-primary)">{node.titleLines.map((line, i) => <tspan key={i} x={left + 44} dy={i ? 17 : 0}>{line}</tspan>)}</text>
            <text x={left + 16} y={top + 66} fontSize="10" fill="var(--text-muted)">{node.detail}</text>
            <text x={left + 16} y={top + 88} fontSize="10" fill="var(--text-primary)">{(node.summaryLines.length ? node.summaryLines : ['No active optimizations']).map((line, i) => <tspan key={i} x={left + 16} dy={i ? 15 : 0}>{line}</tspan>)}</text>
            <text x={left + 16} y={top + 128} fontSize="11" fill="var(--brand-accent)">{node.configured} configured · {node.count} settings</text>
            <text x={left + node.width - 18} y={top + 129} fontSize="16" textAnchor="end" fill="var(--text-muted)">{node.expanded ? '−' : '+'}</text>
        </>}
        <foreignObject x={left} y={top} width={node.width} height={hitHeight}>
            <Button variant="ghost" className={cn('serving-node-hitarea', 'serving-graph-node', node.capability ? 'is-optimization' : 'is-stage')} aria-label={name} aria-pressed={node.capability ? active : undefined} aria-expanded={node.container ? node.expanded : undefined} title={node.detail} onClick={() => node.container ? onToggleGroup?.(node.id) : onSelect(node.id)}><span className="sr-only">{node.name} {status?.label}</span></Button>
        </foreignObject>
        {node.container && node.expanded && <>
            {node.configureTarget && <>
                <text x={left + 16} y={top + node.height - 48} fontSize="11" fill="var(--brand-accent)">Stage parameters ↗</text>
                <foreignObject x={left + 8} y={top + node.height - 68} width={node.width - 16} height="28"><Button variant="ghost" size="xs" className="serving-node-hitarea" aria-label={`Configure ${node.name} parameters`} onClick={() => onSelect(node.configureTarget)}><span className="sr-only">Stage parameters</span></Button></foreignObject>
            </>}
            <text x={node.x} y={top + node.height - 15} textAnchor="middle" fontSize="10" fill="var(--text-muted)">{node.count ? `${node.page * 4 + 1}–${Math.min(node.count, (node.page + 1) * 4)} of ${node.count}` : 'No settings in this snapshot'}</text>
            {node.totalPages > 1 && [['Previous', -1, left + 6], ['Next', 1, left + node.width - 42]].map(([label, delta, x]) => <g key={label}>
                <text x={x + 17} y={top + node.height - 13} textAnchor="middle" fontSize="17" fill="var(--text-muted)">{delta < 0 ? '‹' : '›'}</text>
                <foreignObject x={x} y={top + node.height - 34} width="36" height="32"><Button size="icon" variant="ghost" className="serving-node-hitarea" aria-label={`${label} ${node.name} settings`} disabled={delta < 0 ? node.page === 0 : node.page >= node.totalPages - 1} onClick={() => onGroupPage?.(node.id, node.page + delta)}><span className="sr-only">{label}</span></Button></foreignObject>
            </g>)}
        </>}
    </g>;
}

export function DeploymentPlanGraph({ plan, selected, onSelect, onEdit, onToggle, onToggleGroup, onGroupPage, disabled = false }) {
    const serving = plan.presentation === 'serving';
    const [zoom, setZoom] = useState(1), [query, setQuery] = useState(''), [edgeKey, setEdgeKey] = useState(null), [hovered, setHovered] = useState(null);
    const geometryFor = item => item.path ? { path: item.path, labelX: item.labelX, labelY: item.labelY } : edgeGeometry(item, nodes);
    const keyFor = item => `${item.from}:${item.to}:${item.kind}`;
    const edge = plan.edges.find(item => keyFor(item) === edgeKey);
    const marker = useId().replaceAll(':', '');
    const nodes = layoutPlan(plan), height = plan.height || Math.max(250, ...nodes.map(n => n.y + n.height / 2 + 28));
    const width = plan.width || Math.max(900, ...nodes.map(n => n.x + n.width / 2 + 32));
    const byId = new Map(nodes.map(n => [n.id, n]));
    const focused = hovered || (byId.has(selected) ? selected : nodes.find(node => selected && node.configureTarget === selected)?.id);
    const focusIds = new Set(focused ? [focused, byId.get(focused)?.parentId].filter(Boolean) : []);
    const connected = new Set([...focusIds, ...plan.edges.filter(item => focusIds.has(item.from) || focusIds.has(item.to)).flatMap(item => [item.from, item.to])]);
    const reset = () => { setZoom(1); setQuery(''); setEdgeKey(null); onSelect(''); };
    return <Panel padding="none" className={cn('overflow-hidden', serving && 'serving-diagram')}>
        {!serving && <div className="flex flex-wrap items-center gap-3 border-b border-theme-border px-4 py-3">
            <div className="mr-auto"><p className="text-sm font-semibold">{plan.optimization ? 'Inference flow' : 'Resource map'}</p><p className="text-xs text-theme-muted">{plan.serving || 'Declared resource relationships'}</p></div>
            <div className="w-44"><Input variant="toolbar" icon={Search} type="search" aria-label="Find topology component" placeholder="Find a node" value={query} onChange={event => setQuery(event.target.value)} /></div>
            <Button size="icon" variant="ghost" title="Zoom out" aria-label="Zoom out" disabled={zoom <= 0.75} onClick={() => setZoom(value => value - 0.25)}><ZoomOut size={15} /></Button>
            <Button size="icon" variant="ghost" title="Fit view" aria-label="Fit view" onClick={() => setZoom(1)}><Focus size={15} /></Button>
            <Button size="icon" variant="ghost" title="Zoom in" aria-label="Zoom in" disabled={zoom >= 2} onClick={() => setZoom(value => value + 0.25)}><ZoomIn size={15} /></Button>
            <Button size="icon" variant="ghost" title="Clear selection" aria-label="Clear selection" onClick={reset}><RotateCcw size={15} /></Button>
        </div>
        }
        {query && <div className="flex flex-wrap gap-2 p-3" aria-label="Matching components">{nodes.filter(node => `${node.name} ${node.kind}`.toLowerCase().includes(query.toLowerCase())).map(node => <Button key={node.id} size="xs" variant="secondary" onClick={() => onSelect(node.id)}>{node.name}</Button>)}{!nodes.some(node => `${node.name} ${node.kind}`.toLowerCase().includes(query.toLowerCase())) && <p className="text-xs text-theme-muted">No matching nodes.</p>}</div>}
        <div className={cn('overflow-auto bg-theme-bg', serving ? 'serving-diagram-scroll' : 'max-h-[44rem]')} tabIndex={serving ? 0 : undefined} aria-label={serving ? 'Serving diagram; scroll to explore on small screens' : undefined}>
            <svg xmlns="http://www.w3.org/2000/svg" fontFamily="Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif" role="group" aria-label={serving ? 'Model server optimization relationships' : 'Deployment resource relationships'} viewBox={`${plan.offsetX || 0} 0 ${width} ${height}`} style={{ minWidth: (serving ? Math.min(width, 760) : 960) * zoom, width: `${100 * zoom}%`, ...(serving ? { maxWidth: width, margin: '0 auto' } : {}) }}>
                <defs>{Object.keys(EDGE_COLORS).map(kind => <marker key={kind} id={`${marker}-${kind}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill={CHART_SERIES[EDGE_COLORS[kind]]} /></marker>)}</defs>
                {plan.optimization && !serving && <><text x="32" y="30" fontSize="12" fill="var(--text-muted)">REQUEST PATH</text><text x="32" y="242" fontSize="12" fill="var(--text-muted)">OPTIMIZATIONS</text></>}
                {serving && plan.regions.map(region => <g key={region.id} className="serving-region">
                    <rect x={region.x} y={region.y} width={region.width} height={region.height} rx="18" fill="var(--bg-primary)" stroke="var(--border-color)" strokeDasharray="5 5" />
                    <text x={region.x + 14} y={region.y + 28} fontSize="11" fontWeight="600" fill="var(--text-muted)">{region.label}</text>
                </g>)}
                {plan.edges.map((item, index) => {
                    if (item.hidden) return null;
                    const geometry = geometryFor(item);
                    if (!geometry) return null;
                    const kind = item.kind || 'attachment', color = CHART_SERIES[EDGE_COLORS[kind]];
                    const active = edgeKey === keyFor(item) || [item.from, item.to].some(id => focusIds.has(id));
                    const choose = () => setEdgeKey(edgeKey === keyFor(item) ? null : keyFor(item));
                    return <g key={index} role="button" tabIndex={0} aria-label={`${byId.get(item.from).name}: ${item.label || 'request processing'} → ${byId.get(item.to).name}`} aria-pressed={edgeKey === keyFor(item)} onClick={choose} onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); choose(); } }} opacity={focused && !active ? serving ? kind === 'attachment' ? 0.32 : 0.75 : 0.2 : serving && kind === 'attachment' && !active ? 0.65 : 1} style={{ cursor: 'pointer' }}>
                        <path d={geometry.path} fill="none" stroke="transparent" strokeWidth="16" />
                        <path d={geometry.path} fill="none" stroke={color} strokeWidth={active ? 3.5 : 2.2} strokeDasharray={kind === 'dependency' || item.pending || (serving && kind === 'attachment') ? '6 4' : undefined} markerEnd={`url(#${marker}-${kind})`} />
                        <title>{item.label}</title>
                    </g>;
                })}
                {plan.edges.filter(item => !item.hidden && item.label && (serving ? !item.labelOnSelect : item.kind === 'flow')).map(item => {
                    const geometry = geometryFor(item);
                    if (!geometry) return null;
                    return <g key={`label:${keyFor(item)}`} aria-hidden="true" pointerEvents="none">
                        {serving && <rect x={geometry.labelX - item.labelWidth / 2} y={geometry.labelY - 12} width={item.labelWidth} height="24" rx="12" fill="var(--bg-card)" stroke="var(--border-color)" />}
                        <text x={geometry.labelX} y={serving ? geometry.labelY + 4 : byId.get(item.from).y - 60} textAnchor="middle" fontSize="11" fill="var(--text-primary)">{item.label}</text>
                    </g>;
                })}
                {nodes.map(node => {
                    if (serving) return <ServingDiagramNode key={node.id} node={node} selected={selected} related={focusIds.has(node.id)} onHover={setHovered} onLeave={() => setHovered(null)} onToggleGroup={onToggleGroup} onGroupPage={onGroupPage} onSelect={id => { setEdgeKey(null); onSelect(id); }} />;
                    const status = OPTIMIZATION_STATES[node.status];
                    const inactive = node.capability && !['enabled', 'dependency', 'unknown'].includes(node.status);
                    const name = `${node.kind}: ${node.name}`;
                    const enabled = node.enabled ?? ['enabled', 'dependency'].includes(node.status);
                    const toggleLabel = `${enabled ? 'Disable' : 'Enable'} ${node.name}`;
                    return <g key={node.id} role="group" aria-label={name} onMouseEnter={() => setHovered(node.id)} onMouseLeave={() => setHovered(null)} opacity={focused && !connected.has(node.id) ? serving ? 0.68 : 0.45 : 1}>
                        <foreignObject x={node.x - node.width / 2} y={node.y - node.height / 2} width={node.width} height={node.height}>
                            <Panel padding="sm" className={cn('h-full', inactive && 'border-dashed', selected === node.id && 'ring-2 ring-inset ring-[var(--brand-accent)]')}>
                                <Button variant="ghost" size="sm" className="h-auto w-full justify-start whitespace-normal px-0 py-0 text-left text-sm" title={node.detail} aria-label={name} aria-pressed={selected === node.id} onClick={() => { setEdgeKey(null); onSelect(selected === node.id ? '' : node.id); }}>{node.name}</Button>
                                {node.capability ? <div className="mt-3 flex items-center gap-1">
                                    <Badge tone={status?.tone || 'neutral'}>{status?.label || node.status}</Badge>
                                    <span className="ml-auto" />
                                    {onEdit && <Button size="icon" variant="ghost" title={`Edit ${node.name}`} aria-label={`Edit ${node.name}`} disabled={disabled || node.editDisabled || node.status === 'unsupported'} onClick={() => onEdit(node.capability)}><Pencil size={14} /></Button>}
                                    {onToggle && <Button size="icon" variant="ghost" title={toggleLabel} aria-label={toggleLabel} aria-pressed={enabled} disabled={disabled || node.toggleDisabled || node.status === 'unsupported'} onClick={() => onToggle(node.capability, enabled)}><Power size={15} /></Button>}
                                </div> : <p className="mt-2 text-xs text-theme-muted">{node.detail}</p>}
                            </Panel>
                        </foreignObject>
                    </g>;
                })}
            </svg>
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-theme-border px-4 py-2 text-xs text-theme-muted" aria-label="Relationship legend">
            {(serving ? [['Request processing', 0, false], ['KV cache', 3, false], ['Configuration / management', 1, true]] : plan.optimization ? [['Request / KV flow', 0, false], ['Optimization applies to stage', 1, false], ['Required dependency', 2, true]] : [['Resource reference', 1, false]]).map(([label, color, dashed]) => <span key={label} className="inline-flex items-center gap-2"><svg width="30" height="10" aria-hidden="true"><path d="M0 5 H26 L22 1 M26 5 L22 9" fill="none" stroke={CHART_SERIES[color]} strokeWidth="2" strokeDasharray={dashed ? '4 3' : undefined} /></svg>{label}</span>)}
            {!serving && <span className="ml-auto">Select a node to trace its connections</span>}
        </div>
        {serving ? <div className="serving-connection-summary" aria-label="Selected optimization relationships">
            {edge ? <><strong>{byId.get(edge.from)?.name}</strong><span>{edge.label || 'request processing'}</span><ArrowRight size={14} /><strong>{byId.get(edge.to)?.name}</strong></> : focused ? <><strong>{byId.get(focused)?.name}</strong><span className="text-theme-muted">{byId.get(focused)?.capability ? 'applies to' : 'connected to'}</span>{byId.get(focused)?.capability ? [...new Set(plan.edges.filter(item => item.from === focused || item.to === focused).flatMap(item => [item.from, item.to]).filter(id => id !== focused))].map(id => <Badge key={id} tone="neutral">{byId.get(id)?.name}</Badge>) : <span>{byId.get(focused)?.detail}</span>}</> : <span className="text-theme-muted">Each card is a stage of the request path. Click a card (+) to list its optimizations, then click one to turn it on or off or edit its parameters.</span>}
        </div> : edge && <aside className="flex flex-wrap items-center gap-2 border-t border-theme-border px-4 py-3 text-sm" aria-label="Relationship details"><strong>{byId.get(edge.from)?.name}</strong><ArrowRight size={14} /><strong>{byId.get(edge.to)?.name}</strong><Badge tone="info">{edge.label}</Badge></aside>}
    </Panel>;
}
