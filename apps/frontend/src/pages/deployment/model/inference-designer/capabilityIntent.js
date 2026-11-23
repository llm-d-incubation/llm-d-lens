import { optimizationCatalog } from '@repo/shared/inference/optimizations';
import { guideSubplans } from './officialGuides.js';

const engineOptions = new Set(['prefix', 'chunked', 'tracing', 'affinity', 'load-routing', 'parallel']);
const variantKeys = new Set(['TOPOLOGY', 'CONNECTOR', 'VARIANT', 'SIGNAL', 'MQ_PROVIDER', 'MODEL_SERVER', 'HOST_TYPE', 'DEPLOYMENT', 'ROUTING', 'TASK', 'QUEUE_BACKEND', 'SLO_AWARE']);

/** Limit engine and provider variants to those the source declares for the cluster accelerator. */
export function supportedVariantValues(support, key, values) {
    const all = values.map(String);
    const declared = key === 'MODEL_SERVER' && support?.engines
        ? Object.entries(support.engines).filter(([, state]) => state !== 'unsupported' && state?.status !== 'unsupported').map(([id]) => id)
        : key === 'INFRA_PROVIDER' && support?.providers ? support.providers.map(item => String(item.name)) : null;
    return declared ? all.filter(value => declared.includes(value)) : all;
}

/** Keep whole-Guide presets and individual optimization intent separate. */
export function expandCapabilityIntent(catalog, guides, options) {
    if (!Array.isArray(guides) || guides.some(id => typeof id !== 'string')) throw new Error('Select Guide IDs as an array.');
    const overrides = options.capabilities || {}, choices = [], errors = [], expanded = [];
    if (!overrides || Array.isArray(overrides) || typeof overrides !== 'object') throw new Error('Capability selections must be a boolean mapping.');
    const capabilities = optimizationCatalog(catalog).items;
    const known = new Map(capabilities.map(item => [item.id, item]));
    for (const [id, value] of Object.entries(overrides)) {
        if (typeof value !== 'boolean') throw new Error('Capability selections must be boolean.');
        if (!known.has(id) && !(id.startsWith('guide:') && catalog.guides.some(guide => guide.id === id.slice(6)))) throw new Error(`Unknown optimization: ${id}.`);
        if (known.get(id)?.selection === 'recipe') throw new Error(`${id}: configure this mechanism through its selected recipe, not an independent switch.`);
        if (value && ['external-operation', 'requires-adapter'].includes(known.get(id)?.generation)) errors.push(`${id}: ${known.get(id).reason}`);
    }
    const add = id => {
        const children = guideSubplans(catalog, id);
        if (!children.length) { expanded.push(id); return; }
        const selected = options.guideChoices?.[id];
        const values = Array.isArray(selected) ? selected : selected ? [selected] : [];
        choices.push({ guide: id, key: 'subplan', label: 'Deployment strategy', options: children.map(child => ({ value: child.id, label: child.title })), multiple: true, value: values });
        if (!values.length) errors.push(`${id}: choose its deployment sub-plan before generation.`);
        for (const value of values) {
            if (!children.some(child => child.id === value)) errors.push(`${id}: unknown sub-plan ${value}.`);
            else expanded.push(value);
        }
    };
    guides.forEach(add);
    const wholeGuides = [...new Set(expanded)];
    for (const [id, enabled] of Object.entries(overrides)) {
        if (!enabled || engineOptions.has(id)) continue;
        add(known.get(id)?.guide || id.slice(6));
    }
    const variables = structuredClone(options.variables || {});
    const cpu = overrides.cache, disk = overrides['disk-cache'];
    if (cpu === false && disk === true) errors.push('Disk KV offload requires the CPU KV tier; enable CPU KV offload or disable the disk tier.');
    if (disk === true) variables['tiered-prefix-cache'] = { ...variables['tiered-prefix-cache'], VARIANT: 'fs' };
    else if (cpu === true) variables['tiered-prefix-cache'] = { ...variables['tiered-prefix-cache'], VARIANT: 'cpu' };
    for (const id of [...new Set(expanded)]) {
        const canonical = id === 'multimodal-serving/e-disaggregation' ? 'multimodal-serving' : id;
        const guide = catalog.guides.find(item => item.id === canonical);
        const support = guide?.support?.accelerators?.[options.accelerator];
        for (const [key, spec] of Object.entries(guide?.variables || {})) {
            if (!variantKeys.has(key) || !Array.isArray(spec?.values) || spec.values.length < 2) continue;
            if (choices.some(choice => choice.guide === canonical && choice.key === key)) continue;
            const allowed = supportedVariantValues(support, key, spec.values);
            let value = variables[canonical]?.[key] || '';
            // The cluster's hardware already fixes the accelerator; pick its single or default supported variant.
            if (!value && support && allowed.length && (allowed.length === 1 || allowed.includes(String(spec.default)))) {
                value = allowed.length === 1 ? allowed[0] : String(spec.default);
                variables[canonical] = { ...variables[canonical], [key]: value };
            }
            choices.push({ guide: canonical, key, label: key.replace(/_/g, ' '), options: allowed.map(value => ({ value, label: value })), value });
            if (!value) errors.push(`${canonical}: choose ${key} explicitly before generation.`);
            else if (!spec.values.map(String).includes(String(value))) errors.push(`${canonical}: unsupported ${key} value ${value}.`);
        }
    }
    const enabled = new Set(['parallel', 'affinity', 'load-routing']);
    for (const cap of capabilities) if (cap.guide && wholeGuides.includes(cap.guide) && !cap.id.startsWith('guide:')) enabled.add(cap.id);
    for (const id of wholeGuides) enabled.add(`guide:${id}`);
    for (const [id, value] of Object.entries(overrides)) { if (value) enabled.add(id); else enabled.delete(id); }
    if (wholeGuides.includes('tiered-prefix-cache') && variables['tiered-prefix-cache']?.VARIANT !== 'fs') enabled.delete('disk-cache');
    if (enabled.has('disk-cache')) {
        if (overrides.cache === false) errors.push('Disk KV offload requires CPU KV offload.');
        enabled.add('cache');
    }
    const topology = variables['multimodal-serving']?.TOPOLOGY;
    if (expanded.some(id => id === 'multimodal-serving' || id.startsWith('multimodal-serving/')) && topology && topology !== 'aggregation') {
        enabled.add('encode-split');
        if (overrides['encode-split'] === false) errors.push('Encode disaggregation is required by the selected multimodal topology. Choose aggregation to disable it.');
        if (topology === 'e-p-d') {
            enabled.add('transfer');
            if (overrides.transfer === false) errors.push('P/D separation is required by the E/P/D topology. Choose E/PD or aggregation to disable it.');
        }
    }
    const retained = [...new Set(expanded)].filter(guide => {
        const owned = capabilities.filter(cap => cap.guide === guide && !cap.id.startsWith('guide:') && !engineOptions.has(cap.id));
        return !owned.length || owned.some(cap => enabled.has(cap.id));
    });
    const omitted = expanded.filter(id => !retained.includes(id));
    return { guides: retained, wholeGuides, variables, choices: choices.filter(choice => !omitted.includes(choice.guide)), errors: errors.filter(error => !omitted.some(id => error.startsWith(`${id}:`))), enabled: [...enabled], overrides };
}
