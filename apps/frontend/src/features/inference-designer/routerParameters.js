import yaml from 'js-yaml';

/** Enumerate actual scalar leaves, including YAML-encoded EPP plugin configurations. */
export function routerParameterDocument(text) {
    const values = yaml.load(text || '{}', { schema: yaml.JSON_SCHEMA });
    if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error('Router values must be a mapping.');
    const leaves = [];
    const visit = (value, path, embedded = null) => {
        if (value && typeof value === 'object') {
            for (const [key, child] of Object.entries(value)) {
                if (['__proto__', 'prototype', 'constructor'].includes(key)) throw new Error('Unsafe router key.');
                const next = [...path, key];
                if (path.join('.') === 'router.epp.pluginsCustomConfig' && typeof child === 'string') {
                    const parsed = yaml.load(child, { schema: yaml.JSON_SCHEMA });
                    visit(parsed, [], next);
                } else visit(child, next, embedded);
            }
        } else if (['string', 'number', 'boolean'].includes(typeof value)) {
            leaves.push({ path, embedded, value, id: JSON.stringify([embedded, path]), key: [...(embedded || []), ...path].join('.'), type: typeof value });
        }
    };
    visit(values, []);
    return { values, leaves };
}

/** Preserve sibling plugins, array order, and scalar types when changing one source leaf. */
export function changeRouterParameter(text, leaf, raw) {
    const { values, leaves } = routerParameterDocument(text);
    const current = leaves.find(item => item.id === leaf.id);
    if (!current) throw new Error('Router parameter no longer exists.');
    const value = current.type === 'boolean' ? raw === 'true' : current.type === 'number' ? Number(raw) : raw;
    if (current.type === 'number' && (!raw.trim() || !Number.isFinite(value))) throw new Error('Enter a finite number.');
    const get = (root, path) => path.reduce((item, key) => item[key], root);
    const set = (root, path, next) => { get(root, path.slice(0, -1))[path.at(-1)] = next; };
    if (current.embedded) {
        const plugin = yaml.load(get(values, current.embedded), { schema: yaml.JSON_SCHEMA });
        set(plugin, current.path, value);
        set(values, current.embedded, yaml.dump(plugin, { lineWidth: -1, noRefs: true }));
    } else set(values, current.path, value);
    return yaml.dump(values, { lineWidth: -1, noRefs: true });
}
