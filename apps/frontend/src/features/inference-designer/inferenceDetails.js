/** Compact facts owned by the selected node; global deployment context lives above the map. */
export function inferenceDetails(entity, graph, draft) {
    if (!entity) return [];
    const rows = [];
    const add = (label, value) => { if (value) rows.push({ label, value: String(value) }); };
    if (entity.kind === 'capability') {
        const edge = graph.edges.find(edge => edge.id === entity.edge);
        add('Acts on', edge ? `${graph.nodes.find(node => node.id === edge.from)?.label} → ${graph.nodes.find(node => node.id === edge.to)?.label}` : graph.nodes.find(node => node.id === entity.owner)?.label);
        add('State', entity.required ? 'Required dependency' : entity.enabled || entity.inherited ? 'Enabled' : 'Off');
    } else {
        const outgoing = graph.edges.filter(edge => edge.from === entity.id && edge.kind === 'request');
        add('Next stage', outgoing.map(edge => graph.nodes.find(node => node.id === edge.to)?.label).join(' → '));
        const stages = graph.edges.filter(edge => edge.to === entity.id && edge.kind === 'placement');
        add('Executes', stages.map(edge => graph.nodes.find(node => node.id === edge.from)?.label).join(' / '));
        add('Optimizations', entity.optimizations?.length ? `${graph.capabilities.filter(cap => entity.optimizations.includes(cap.id) && cap.enabled).length} enabled / ${entity.optimizations.length} available` : null);
        if (!draft.sourceMode && ['prefill', 'decode'].includes(entity.id)) {
            const prefill = entity.id === 'prefill' && draft.pd;
            add('Replicas × TP', `${prefill ? draft.prefillReplicas : draft.replicas} × ${prefill ? draft.prefillTp : draft.tp}`);
        }
    }
    return rows.slice(0, 3);
}
