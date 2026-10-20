import yaml from 'js-yaml';
import { PARAMETER_FIELDS } from './capabilityCatalog.js';

/** Explicit controls never silently overwrite an expert overlay at the same path. */
export function structuredParameters(draft) {
    const engine = [];
    let router = draft.routerValues?.trim() ? yaml.load(draft.routerValues) : {};
    if (!router || typeof router !== 'object' || Array.isArray(router)) throw new Error('Router values must be a YAML mapping.');
    for (const field of PARAMETER_FIELDS) {
        if (field.key === 'cacheCpuGiB' && draft.cache === 'none') continue;
        if (field.key === 'rdmaNicCount' && (!draft.pd || draft.variant !== 'vllm-rdma')) continue;
        if (['prefillTp', 'prefillReplicas'].includes(field.key) && !draft.pd) continue;
        const raw = draft[field.key] ?? '';
        if (raw === '') continue;
        let value = raw;
        if (['integer', 'number'].includes(field.type)) {
            value = Number(raw);
            if (!Number.isFinite(value) || (field.type === 'integer' && !Number.isSafeInteger(value))
                || (field.min != null && (field.exclusiveMin ? value <= field.min : value < field.min))
                || (field.max != null && value > field.max)) throw new Error(`${field.label}: enter a valid ${field.type} in the documented range.`);
        }
        if (field.options && !field.options.includes(raw)) throw new Error(`${field.label}: unsupported option.`);
        if (field.type === 'flag' && raw !== 'enabled') throw new Error(`${field.label}: choose inherit or enabled.`);
        if (['prefixCaching', 'chunkedPrefill', 'dtype', 'kvCacheDtype', 'eager'].includes(field.key)) {
            engine.push({ target: 'both', kind: 'argument', name: field.output.slice(2), value: field.type === 'flag' ? '' : String(value) });
        }
        if (!field.output.startsWith('router.')) continue;
        if (field.key === 'tracingEnabled') value = raw === 'true';
        if (field.key === 'samplerArg') value = String(value);
        const path = field.output.split('.');
        let cursor = router;
        path.slice(0, -1).forEach(key => {
            if (!Object.hasOwn(cursor, key)) cursor[key] = {};
            if (!cursor[key] || typeof cursor[key] !== 'object' || Array.isArray(cursor[key])) throw new Error(`${field.label}: router overlay has a non-mapping at ${key}.`);
            cursor = cursor[key];
        });
        const key = path.at(-1);
        if (Object.hasOwn(cursor, key)) throw new Error(`${field.label}: conflicts with ${field.output} in the router overlay. Keep one source of value.`);
        cursor[key] = value;
    }
    if (draft.precise && draft.eppReplicas && Number(draft.eppReplicas) !== 1) throw new Error('Precise cache routing requires one EPP replica.');
    return { engine, routerValues: Object.keys(router).length ? yaml.dump(router, { lineWidth: -1, noRefs: true }) : '' };
}
