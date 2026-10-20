import yaml from 'js-yaml';
import { PARAMETER_FIELDS } from './capabilityCatalog.js';
import { sourceParameterLeaves } from './sourceParameters.js';

const at = (root, path) => path.reduce((value, key) => value?.[key], root);
const label = value => String(value).replace(/^--/, '').replace(/[-_]/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2').replace(/\b(ttft|tpot|kv|epp)\b/gi, word => word.toUpperCase()).replace(/^./, char => char.toUpperCase());
const routerHints = {
    affinityThreshold: 'Minimum prefix-cache affinity used by this filter to retain an endpoint.',
    maxTTFTPenaltyMs: 'Maximum additional time to first token allowed for cache affinity, in milliseconds.',
    ttftSource: 'Prediction source used for time-to-first-token decisions; keep a source supported by this plugin.',
    ttftWeight: 'Relative importance of predicted time to first token in the latency score.',
    tpotWeight: 'Relative importance of predicted time per output token in the latency score.',
    maxPrefixTokensToMatch: 'Maximum prefix tokens examined during cache matching. Larger limits increase matching work.',
};
const routerScopes = {
    affinity: /prefix|affinity/i, 'load-routing': /token-load|inflight|token-producer/i,
    latency: /latency|predict/i, index: /prefix|cache|kv|token/i,
    'flow-control': /flow|queue|admission/i, coordinator: /coordinator|pipeline/i,
    tracing: /trac|otel|sampl/i, 'multi-model': /model|adapter|lora/i,
};
const engineScopes = {
    parallel: /tensor|pipeline|data-parallel|expert|max-num|max-model|memory-utilization|dtype|block-size|enforce-eager/i,
    prefix: /prefix|block-size|kv-cache-dtype/i,
    chunked: /chunked|max-num-batched|max-num-seqs|prefill/i,
    transfer: /kv-transfer|kv-connector|tensor|pipeline|data-parallel|distributed|nixl|ucx/i,
    cache: /cache|offload|kv-transfer|kv-connector|lmcache|mooncake/i,
    'disk-cache': /cache|offload|kv-transfer|lmcache|mooncake|disk|storage/i,
    'p2p-cache': /cache|offload|kv-transfer|lmcache|mooncake/i,
    index: /kv-events|block-size|prefix|zmq/i,
    speculation: /speculat|draft|mtp/i,
    'wide-ep': /expert|data-parallel|tensor|pipeline|distributed/i,
    payload: /multimodal|mm-|encoder|media/i,
    'encode-split': /multimodal|mm-|encoder|media|kv-transfer/i,
};
export function nodeResourceKinds(entity) {
    if (routerScopes[entity?.id] && entity.id !== 'index') return ['values'];
    if (['request', 'routing', 'epp', 'service'].includes(entity?.id)) return ['values'];
    if (engineScopes[entity?.id] && entity.id !== 'index') return ['kustomize', 'resource', 'chart'];
    return null;
}
const accepts = (entity, key) => {
    if (/model$|served-model|tokenizer$|hf[_-]token|password|secret|credential/i.test(key)) return false;
    if (engineScopes[entity?.id]) return engineScopes[entity.id].test(key);
    if (routerScopes[entity?.id]) return false;
    return true;
};
const describeFlag = flag => PARAMETER_FIELDS.find(field => field.output.match(/--[\w-]+/)?.[0] === flag);
function argumentField(leaf, flag, value, encode, context, readOnly = false) {
    const spec = describeFlag(flag);
    const role = /^(prefill|decode)(?: \/|$)/.exec(context || '')?.[1];
    const title = spec?.label || label(flag);
    const roleTitle = role ? `${label(role)} ${title.replace(/^(?:Serving \/ Decode|Prefill|Decode)\s*/, '')}` : title;
    return { ...leaf, key: `${leaf.key}:${flag}`, value, sourceValue: leaf.value, encode, readOnly,
        title: roleTitle, section: context || 'Engine',
        hint: readOnly ? 'Enabled by this source recipe. Edit this flag in Advanced resource YAML below.' : spec?.hint || `Value passed to ${flag}; keep the format accepted by the selected engine.`, sourceKey: flag };
}
/** Show only runtime fields owned by this node; keep original paths and types for writes. */
export function nodeSourceFields(documents, entity) {
    const moduleScope = { 'cpu-cache': 'cache', 'cache-plane': 'cache', 'index-service': 'index' }[entity?.id];
    if (moduleScope) entity = { ...entity, id: moduleScope, kind: 'capability' };
    const fields = [];
    for (const leaf of sourceParameterLeaves(documents)) {
        const parent = at(documents, leaf.path.slice(0, -1));
        const ancestors = leaf.path.map((_, index) => at(documents, leaf.path.slice(0, index)));
        const roles = ancestors.flatMap(value => [value?.metadata?.labels?.['llm-d.ai/role'], value?.template?.metadata?.labels?.['llm-d.ai/role']]).filter(Boolean);
        const role = roles.at(-1);
        if (role && ['prefill', 'prefill-worker', 'decode', 'decode-worker'].includes(entity?.id) && !entity.id.startsWith(role)) continue;
        const containerIndex = leaf.path.findIndex(key => key === 'containers');
        const container = containerIndex >= 0 ? at(documents, leaf.path.slice(0, containerIndex + 2))?.name : null;
        const context = [role, container].filter(Boolean).join(' / ') || documents[leaf.path[0]]?.kind;
        if (['args', 'command'].includes(leaf.path.at(-2)) && typeof leaf.value === 'string') {
            const arg = leaf.value.match(/^(--[\w-]+)(?:=([\s\S]*))?$/);
            if (arg && accepts(entity, arg[1])) {
                if (arg[2] !== undefined) fields.push(argumentField(leaf, arg[1], arg[2], raw => `${arg[1]}=${raw}`, context));
                else {
                    const next = parent[Number(leaf.path.at(-1)) + 1];
                    if (typeof next === 'string' && !next.startsWith('--')) {
                        const path = [...leaf.path.slice(0, -1), Number(leaf.path.at(-1)) + 1];
                        fields.push(argumentField({ ...leaf, path, key: path.join('.'), value: next }, arg[1], next, raw => raw, context));
                    } else fields.push(argumentField(leaf, arg[1], arg[1].startsWith('--no-') ? 'Disabled' : 'Enabled', raw => raw, context, true));
                }
            } else if (!arg && /\s/.test(leaf.value)) {
                // Expose literal scalar arguments only. Never evaluate shell expressions.
                for (const match of leaf.value.matchAll(/(--[\w-]+)(?:=|\s+)(?:"([^"\n]*)"|'([^'\n]*)'|([^\s\\;|]+))/g)) {
                    const value = match[2] ?? match[3] ?? match[4];
                    if (!accepts(entity, match[1]) || value.startsWith('--') || /[$`]/.test(value)) continue;
                    const offset = match.index + match[0].indexOf(value, match[1].length);
                    const quote = match[2] !== undefined ? '"' : match[3] !== undefined ? "'" : '';
                    fields.push(argumentField(leaf, match[1], value, raw => {
                        if (raw.includes(quote || ' ') || /[\n`$;|]/.test(raw)) throw new Error('Use a literal value; shell syntax belongs in the advanced source editor.');
                        return leaf.value.slice(0, offset) + raw + leaf.value.slice(offset + value.length);
                    }, context));
                }
            }
            continue;
        }
        const key = leaf.path.at(-1), pathText = leaf.path.join('.');
        if (key === 'value' && parent?.name && leaf.path.includes('env') && accepts(entity, parent.name)) {
            fields.push({ ...leaf, title: label(parent.name), section: context || 'Environment', hint: 'Environment value used by this container. Keep the source format.', sourceKey: parent.name });
        } else if (/\.(?:requests|limits)\./.test(pathText) && (!entity || entity.kind === 'module' || ['parallel', 'transfer', 'wide-ep'].includes(entity.id))) {
            fields.push({ ...leaf, title: [role && label(role), `${label(leaf.path.at(-2))}: ${key}`].filter(Boolean).join(' '), section: [role, 'Resources'].filter(Boolean).join(' / '), hint: 'Kubernetes resource quantity; requests reserve capacity and limits cap usage.', sourceKey: pathText });
        } else if (['replicas', 'minReplicas', 'maxReplicas', 'minReplicaCount', 'maxReplicaCount', 'pollingInterval', 'cooldownPeriod', 'threshold', 'activationThreshold', 'concurrency'].includes(key) && (!entity || entity.kind === 'module' || !engineScopes[entity.id] || entity.id === 'transfer') && !routerScopes[entity?.id]) {
            fields.push({ ...leaf, title: [role && label(role), label(key)].filter(Boolean).join(' '), section: [role, 'Capacity & scaling'].filter(Boolean).join(' / '), hint: /replica/i.test(key) ? 'Number of workload instances for this resource.' : /Interval|Period/.test(key) ? 'Controller interval in seconds, as declared by the source.' : 'Controller target from the selected recipe.', sourceKey: pathText });
        } else if (leaf.path[1] === 'data' && accepts(entity, key) && engineScopes[entity?.id]?.test(key)) {
            fields.push({ ...leaf, title: label(key), section: 'Connector configuration', hint: 'Configuration consumed by the selected connector. Preserve its YAML or JSON format.', sourceKey: key });
        }
    }
    return fields;
}
/** Filter actual EPP plugin fields by owner; identity/reference fields are never tuning knobs. */
export function nodeRouterFields(document, entity) {
    return document.leaves.flatMap(leaf => {
        if (entity?.kind === 'module' && ['request', 'service'].includes(entity.id)) {
            if (leaf.embedded || !/^router\.(?:extraServicePorts|gateway|service|httpRoute)\./.test(leaf.key)) return [];
            if (['name', 'kind', 'apiVersion'].includes(leaf.path.at(-1))) return [];
            return [{ ...leaf, title: label(leaf.path.at(-1)), section: 'Service & ingress', sourceKey: leaf.key, hint: 'Endpoint setting from the selected routing configuration.' }];
        }
        if (['apiVersion', 'kind', 'name', 'type', 'pluginRef', 'port', 'modelName', 'pluginsConfigFile'].includes(leaf.path.at(-1))) return [];
        const root = leaf.embedded ? yaml.load(at(document.values, leaf.embedded), { schema: yaml.JSON_SCHEMA }) : document.values;
        const ancestors = leaf.path.map((_, index) => at(root, leaf.path.slice(0, index)));
        const owner = ancestors.filter(value => value && typeof value === 'object' && !Array.isArray(value)).map(value => value.type || value.pluginRef || value.name).filter(Boolean).join(' / ');
        const identity = `${owner} ${leaf.path.join(".")}`;
        const match = routerScopes[entity?.id];
        if (match && !match.test(identity) && !(entity.id === "latency" && ["ttftSource", "maxTTFTPenaltyMs", "ttftWeight", "tpotWeight"].includes(leaf.path.at(-1)))) return [];
        if (entity?.kind === 'capability' && !match && !['coordinator'].includes(entity.id)) return [];
        if (!leaf.embedded && !/(?:epp\.(?:flags|resources|replicas)|tracing|sampling|inferencePool\.failureMode|modelServers\.protocol)/.test(leaf.key)) return [];
        return [{ ...leaf, title: label(leaf.path.at(-1)), section: owner || 'Router', sourceKey: leaf.key,
            hint: routerHints[leaf.path.at(-1)] || (leaf.path.at(-1) === 'weight' ? 'Relative contribution of this plugin to endpoint scoring.' : `Source setting for ${owner || 'the router'}. Unspecified built-in defaults are inherited.`) }];
    });
}
