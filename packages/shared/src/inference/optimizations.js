import definition from '../../../../apps/backend/src/llm_d_lens/configuration/optimization_catalog.json' with { type: 'json' };

const define = (id, label, stage, guide, detail, extra = {}) => ({
    id, label, stage, guide, detail, inspector: id, fields: [], available: false,
    selection: 'guide', ...extra,
});

export const OPTIMIZATIONS = definition.items.map(item => define(...item));

export function optimizationStage(id) {
    if (id.startsWith('models/')) return 'decode';
    if (id.startsWith('batch-serving/')) return 'request';
    if (id === 'workload-autoscaling/multi-inference-pool') return 'routing';
    return OPTIMIZATIONS.filter(item => item.guide && (id === item.guide || id.startsWith(`${item.guide}/`)))
        .sort((a, b) => b.guide.length - a.guide.length)[0]?.stage || 'pods';
}

/** Resolve renamed Guides against this checkout, never against an assumed latest version. */
export function optimizationCatalog(catalog) {
    const guides = catalog.guides || [];
    const items = OPTIMIZATIONS.map(item => {
        const guide = [item.guide, ...(item.guideAliases || [])].find(id => guides.some(source => source.id === id)) || item.guide;
        const sourceGuide = guides.find(source => source.id === guide);
        const present = !!sourceGuide;
        return { ...item, guide, ...(guide ? { source: `https://github.com/llm-d/llm-d/blob/${catalog.commit || 'a9cf07a77a3016db21ae87134e5745b3a2d45ba6'}/guides/${guide}/README.md` } : {}),
            present: present || item.selection === 'recipe' || item.selection === 'switch',
            hardwareSupport: sourceGuide?.support || null,
            generation: item.generation || (item.selection === 'recipe' ? 'recipe-only' : present || item.selection === 'switch' ? 'source-dependent' : 'unavailable'),
            reason: item.reason || (present || item.selection === 'switch' ? 'Generation is checked against the selected recipe and target; catalog presence is not deployment validation.' : item.selection === 'recipe' ? 'No independent switch; choose a recipe that configures this mechanism.' : 'Guide absent from this source revision.') };
    });
    for (const guide of guides) {
        if (items.some(item => item.guide === guide.id) || guide.disposition?.kind === 'category') continue;
        const external = ['external', 'operation'].includes(guide.disposition?.kind);
        const deprecated = /\bdeprecated\b/i.test((guide.instructions || '').slice(0, 3000));
        items.push(define(`guide:${guide.id}`, (guide.title || guide.id).replace(/[*`]/g, ''), optimizationStage(guide.id), guide.id,
            guide.description || `Source recipe: ${guide.id}`, { present: true, generation: external ? 'external-operation' : 'source-dependent',
                hardwareSupport: guide.support || null,
                maturity: deprecated ? 'deprecated' : 'source-defined',
                source: `https://github.com/llm-d/llm-d/blob/${catalog.commit || 'a9cf07a77a3016db21ae87134e5745b3a2d45ba6'}/guides/${guide.id}/README.md`,
                reason: deprecated ? 'Deprecated by this source revision; use a recommended replacement.' : guide.disposition?.reason || 'Recipe contents and deployment requirements remain source-owned.' }));
    }
    return { schemaVersion: 'inference-optimizations.v1', commit: catalog.commit || '', items };
}

const detectors = definition.detectors.map(([id, pattern]) => [id, new RegExp(pattern)]);
const implementations = definition.implementations.map(([id, pattern]) => [id, new RegExp(pattern)]);

/** Extract facts from parsed YAML, not README prose or comments. No source code is executed. */
export function optimizationSourceFacts(path, documents) {
    const strings = [], roles = new Set(), references = [];
    const visit = value => {
        if (typeof value === 'string') strings.push(value);
        else if (Array.isArray(value)) {
            if (value.every(item => typeof item === 'string')) strings.push(value.join(' '));
            value.forEach(visit);
        } else if (value && typeof value === 'object') {
            if (Array.isArray(value.roles)) for (const role of value.roles) {
                if (['prefill', 'decode', 'encode'].includes(role.name)) roles.add(role.name);
            }
            const role = value['llm-d.ai/role'] || value.role;
            if (['prefill', 'decode', 'encode'].includes(role)) roles.add(role);
            Object.values(value).forEach(visit);
        }
    };
    for (const doc of documents) {
        visit(doc);
        if (['Kustomization', 'Component'].includes(doc?.kind)) {
            references.push(...(doc.resources || []), ...(doc.components || []), ...(doc.bases || []),
                ...(doc.patches || []).flatMap(patch => typeof patch === 'string' ? [patch] : patch.path ? [patch.path] : []),
                ...(doc.patchesStrategicMerge || []).filter(value => typeof value === 'string' && !value.includes('\n')));
        }
    }
    const text = strings.join('\n');
    const features = detectors.filter(([, pattern]) => pattern.test(text)).map(([id]) => id);
    const disabled = [['prefix', /--no-enable-prefix-caching\b|--enable-prefix-caching[= ]false\b/],
        ['chunked', /--no-enable-chunked-prefill\b|--enable-chunked-prefill[= ]false\b/]]
        .filter(([, pattern]) => pattern.test(text)).map(([id]) => id);
    return { path, references, features: features.filter(id => !disabled.includes(id)), disabled, roles: [...roles],
        implementations: implementations.filter(([, pattern]) => pattern.test(text)).map(([name]) => name) };
}

const normalize = path => {
    const parts = [];
    for (const part of path.split('/')) {
        if (part === '..') parts.pop();
        else if (part && part !== '.') parts.push(part);
    }
    return parts.join('/');
};

/** Follow only selected local source references; conflicting patches stay unknown until rendering. */
export function resolveOptimizationFeatures(catalog, plan) {
    const sources = catalog.optimizations?.sources || [];
    const byPath = new Map(sources.map(source => [source.path, source]));
    const included = new Map(), disabled = new Set(), roles = new Set(), visited = new Set(), unresolved = [], implementations = new Set();
    const visit = path => {
        const source = byPath.get(path) || byPath.get(`${path}/kustomization.yaml`) || byPath.get(`${path}/kustomization.yml`);
        if (!source) { unresolved.push(path); return; }
        if (visited.has(source.path)) return;
        visited.add(source.path);
        for (const feature of source.features) included.set(feature, [...(included.get(feature) || []), source.path]);
        source.disabled.forEach(id => disabled.add(id));
        source.roles.forEach(role => roles.add(role));
        source.implementations?.forEach(name => implementations.add(name));
        const directory = source.path.slice(0, source.path.lastIndexOf('/'));
        for (const ref of source.references) {
            if (typeof ref !== 'string' || ref.includes('://') || ref.includes('?') || ref.includes('$')) { unresolved.push(String(ref)); continue; }
            visit(normalize(`${directory}/${ref}`));
        }
    };
    const selections = [...(plan.units || []), ...(plan.router?.values || [])];
    const edited = plan.finalConfiguration !== undefined || selections.some(unit => unit.replacementContent !== undefined || unit.content !== undefined || unit.edits?.length);
    for (const unit of selections) visit(unit.path);
    if (roles.has('prefill') && roles.has('decode')) included.set('transfer', [...visited].filter(path => byPath.get(path)?.roles.length));
    const features = [...included].map(([id, paths]) => ({
        id, state: disabled.has(id) || edited || unresolved.length ? 'unknown' : 'included', evidence: 'selected-source',
        paths: [...new Set(paths)], reason: disabled.has(id) || edited || unresolved.length ? 'Source layers are edited, conflicting or incomplete; inspect the rendered configuration.' : 'Declared by the selected source graph, not verified runtime state.',
    }));
    return { features, roles: [...roles], implementations: [...implementations], unresolved: [...new Set(unresolved)] };
}
