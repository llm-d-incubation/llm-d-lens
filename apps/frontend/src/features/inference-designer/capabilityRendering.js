import yaml from 'js-yaml';
import { parse } from 'shell-quote';

/** Apply explicit engine switches to rendered source resources, never to sidecars. */
export function applyEngineCapabilities(documents, overrides = {}) {
    const toggles = [['prefix', 'prefix-caching'], ['chunked', 'chunked-prefill']].filter(([id]) => Object.hasOwn(overrides, id));
    if (!toggles.length) return documents;
    const result = structuredClone(documents);
    const visit = value => {
        if (!value || typeof value !== 'object') return;
        if (Array.isArray(value.containers)) for (const container of value.containers) {
            const identity = [container.name, container.image, ...(container.command || []), ...(container.args || [])].join(' ');
            if (!/vllm/i.test(identity) || /sidecar|router|proxy/i.test(container.name || '')) continue;
            if (!Array.isArray(container.args)) throw new Error('The selected engine uses no editable argument list; configure its source before changing engine capabilities.');
            if (container.command?.some(arg => /(?:^|\/)b?a?sh$/.test(arg))) {
                const script = container.args.length === 1 ? container.args[0] : '';
                const words = parse(script.split(String.fromCharCode(92, 10)).join(' '));
                if (words[0] === 'exec') words.shift();
                if (!script || /[$`]/.test(script) || words.some(word => typeof word !== 'string') || !/(?:^|\/)vllm$/.test(words[0] || '') || words[1] !== 'serve') throw new Error('This engine launch script contains shell logic. Edit its source explicitly before changing engine switches.');
                container.command = words.slice(0, 2);
                container.args = words.slice(2);
            }
            for (const [id, flag] of toggles) {
                container.args = container.args.filter(arg => !new RegExp(`^--(?:no-)?enable-${flag}(?:=|$)`).test(arg));
                container.args.push(`--${overrides[id] ? '' : 'no-'}enable-${flag}`);
            }
        }
        for (const child of Object.values(value)) visit(child);
    };
    visit(result);
    return result;
}

/** Resolve baseline plugin definitions from the same source revision. */
export function capabilityPolicyDefaults(catalog) {
    return (catalog?.assets || []).filter(asset => asset.guide === 'optimized-baseline' && asset.kind === 'values').flatMap(asset => {
        const values = yaml.load(asset.content, { schema: yaml.JSON_SCHEMA });
        return Object.values(values?.router?.epp?.pluginsCustomConfig || {}).flatMap(text => yaml.load(text, { schema: yaml.JSON_SCHEMA })?.plugins || []);
    });
}
/** Read the active plugin file after source values precedence, rather than guessing from Guide names. */
export function sourceRoutingCapabilities(layers) {
    let active = '', configs = {};
    for (const layer of layers) {
        const epp = yaml.load(layer.content || '{}', { schema: yaml.JSON_SCHEMA })?.router?.epp;
        active = epp?.pluginsConfigFile || active;
        configs = { ...configs, ...epp?.pluginsCustomConfig };
    }
    const plugins = active && configs[active] ? yaml.load(configs[active], { schema: yaml.JSON_SCHEMA })?.plugins || [] : [];
    return { affinity: plugins.some(plugin => plugin.type === 'prefix-cache-affinity-filter'), 'load-routing': plugins.some(plugin => plugin.type === 'token-load-scorer') };
}

/** Remove a disabled policy and its profile references, preserving shared producers. */
export function applyRouterCapabilities(content, overrides = {}, defaults = []) {
    if (!['affinity', 'load-routing', 'tracing'].some(id => Object.hasOwn(overrides, id))) return content;
    const values = yaml.load(content, { schema: yaml.JSON_SCHEMA }) || {};
    if (Object.hasOwn(overrides, 'tracing')) { values.router ||= {}; values.router.tracing = { ...values.router.tracing, enabled: overrides.tracing }; }
    const configs = values.router?.epp?.pluginsCustomConfig;
    for (const [file, text] of Object.entries(configs || {})) {
        const config = yaml.load(text, { schema: yaml.JSON_SCHEMA });
        if (!Array.isArray(config?.plugins)) continue;
        for (const [id, types] of [['affinity', ['approx-prefix-cache-producer', 'prefix-cache-affinity-filter']], ['load-routing', ['inflight-load-producer', 'token-load-scorer']]]) {
            if (overrides[id] !== true) continue;
            for (const type of types) {
                let plugin = config.plugins.find(item => item.type === type);
                if (!plugin) {
                    const definition = defaults.find(item => item.type === type);
                    if (!definition) throw new Error(`${id}: source plugin ${type} is unavailable in this revision.`);
                    plugin = structuredClone(definition); config.plugins.push(plugin);
                }
                if (type === types.at(-1)) for (const profile of config.schedulingProfiles || []) {
                    profile.plugins ||= [];
                    const ref = plugin.name || plugin.type;
                    if (!profile.plugins.some(item => item.pluginRef === ref)) profile.plugins.push({ pluginRef: ref });
                }
            }
        }
        const removed = new Set();
        const disable = plugin => overrides.affinity === false && /prefix-cache-affinity-filter/.test(plugin.type) || overrides['load-routing'] === false && /token-load-scorer/.test(plugin.type);
        config.plugins = config.plugins.filter(plugin => { if (!disable(plugin)) return true; removed.add(plugin.name || plugin.type); return false; });
        for (const profile of config.schedulingProfiles || []) {
            profile.plugins = (profile.plugins || []).filter(plugin => !removed.has(plugin.pluginRef));
            if (!profile.plugins.length) throw new Error(`Routing profile ${profile.name} would have no scheduling policy. Keep a routing policy enabled.`);
        }
        configs[file] = yaml.dump(config, { noRefs: true, lineWidth: -1 });
    }
    return yaml.dump(values, { noRefs: true, lineWidth: -1 });
}

/** Reject unsupported switches instead of emitting a bundle that ignores user intent. */
export function assertRenderedCapabilities(documents, overrides = {}) {
    const containers = [], configs = [];
    const visit = value => {
        if (!value || typeof value !== 'object') return;
        if (Array.isArray(value.containers)) containers.push(...value.containers);
        if (value.kind === 'ConfigMap') for (const content of Object.values(value.data || {})) {
            try { const config = yaml.load(content, { schema: yaml.JSON_SCHEMA }); if (config?.kind === 'EndpointPickerConfig') configs.push(config); } catch { /* Other ConfigMap formats do not define EPP policies. */ }
        }
        for (const child of Object.values(value)) visit(child);
    };
    visit(documents);
    for (const [id, flag] of [['prefix', 'prefix-caching'], ['chunked', 'chunked-prefill']]) {
        if (!Object.hasOwn(overrides, id)) continue;
        const engines = containers.filter(container => /vllm/i.test([container.name, container.image, ...(container.command || []), ...(container.args || [])].join(' ')) && !/sidecar|router|proxy/.test(container.name || ''));
        const expected = `--${overrides[id] ? '' : 'no-'}enable-${flag}`;
        if (!engines.length || engines.some(container => !container.args?.includes(expected))) throw new Error(`${id}: this runtime has no supported independent switch. Select a compatible direct-argument vLLM source before generation.`);
    }
    for (const [id, type] of [['affinity', 'prefix-cache-affinity-filter'], ['load-routing', 'token-load-scorer']]) {
        if (!Object.hasOwn(overrides, id)) continue;
        const present = configs.some(config => config.plugins?.some(plugin => plugin.type === type));
        if (present !== overrides[id]) throw new Error(`${id}: the rendered EPP configuration does not match the selected optimization. Choose a compatible routing source.`);
    }
    const owners = new Set();
    for (const doc of documents) {
        if (!['ScaledObject', 'HorizontalPodAutoscaler'].includes(doc.kind)) continue;
        const target = doc.spec?.scaleTargetRef;
        if (!target?.name) continue;
        const key = `${doc.metadata?.namespace || ''}/${target.kind || 'Deployment'}/${target.name}`;
        if (owners.has(key)) throw new Error(`Multiple autoscaling controllers target ${key}. Choose one strategy for that workload.`);
        owners.add(key);
    }
}
