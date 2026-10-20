import { adaptMarkdownGuide, guideDisposition } from './guideAdapters.js';
import yaml from 'js-yaml';
import { parse } from 'shell-quote';

const object = value => value && typeof value === 'object' && !Array.isArray(value);
const distinct = values => [...new Set(values)];
const sourcePath = value => String(value).replace(/^\/?SOURCE\//, '').replace(/\/$/, '');
const expand = (value, variables) => {
    let text = String(value);
    for (let i = 0; i < 12; i++) {
        const next = text.replace(/\$\{([A-Z][A-Z0-9_]*)(?::-([^{}]*))?\}|\$([A-Z][A-Z0-9_]*)\b/g, (match, braced, fallback, bare) => {
            const replacement = variables[braced || bare];
            return replacement !== undefined && !String(replacement).includes(match) ? replacement : fallback ?? match;
        });
        if (text === next || next.length > 20000) break;
        text = next;
    }
    return text;
};
const tokens = line => parse(line, key => '${' + key + '}').filter(value => typeof value === 'string');
const clean = text => text.replace(/\\\n\s*/g, ' ').split('\n').filter(line => !line.trim().startsWith('#')).join('\n');

// Some source revisions document conditional deployment commands as commented alternatives.
// Interpret only the explicitly instructed, declarative model-server apply command.
const deploymentRun = step => {
    if (!step.path.startsWith('modelserver/') || !step.when?.ACCELERATOR_TYPE ||
        !/uncomment the below/i.test(step.run) || clean(step.run).trim()) return step.run;
    const lines = clean(step.run.replace(/^\s*# ?/gm, '')).split('\n');
    const commands = lines.filter(line => /^\s*kubectl apply -n \$\{NAMESPACE\}\s+-k \$\{REPO_ROOT\}\/guides\/[^\s;|&]+\s*$/.test(line));
    return commands.length === 1 ? commands[0] : step.run;
};

/** Shared support components do not establish a serving workload. */
export function isServingSourceUnit(unit) {
    return /\/modelserver(?:\/|$)/.test(unit.path) &&
        !/\/modelserver\/(?:components|common)(?:\/|$)/.test(unit.path) && !/mooncake-client/.test(unit.path);
}

/** Flatten declarative Guide steps while retaining conditions and branch identity. */
export function guideSteps(value, path = '') {
    if (Array.isArray(value)) return value.flatMap((item, index) => guideSteps(item, `${path}/${index}`));
    if (!object(value)) return [];
    if (typeof value.run === 'string') return [{ ...value, path }];
    return Object.entries(value).flatMap(([key, child]) => guideSteps(child, path ? `${path}/${key}` : key));
}

/** Defaults are read from the selected checkout; no vendor default is supplied by Lens. */
export function guideVariables(catalog, guide, input = {}) {
    const values = { ...(catalog.environment || {}), ...input };
    for (const [key, spec] of Object.entries(guide.variables || {})) {
        const value = object(spec) ? spec.default : spec;
        if (!Object.hasOwn(input, key) && value !== undefined && !String(value).includes('$(')) values[key] = expand(value, { ...values, ...input });
    }
    Object.assign(values, input, { REPO_ROOT: 'SOURCE' });
    for (let i = 0; i < 12; i++) for (const key of Object.keys(values)) values[key] = expand(values[key], values);
    return values;
}

/** Resolve complete Guides into an explainable dependency closure, shared by UI and server. */
export function resolveGuidePlan(catalog, selection = {}) {
    if (!Array.isArray(selection.guides || []) || (selection.guides || []).length > 60 || (selection.guides || []).some(id => typeof id !== 'string')) throw new Error('Select up to 60 Guide IDs.');
    catalog = { ...catalog, guides: catalog.guides.map(adaptMarkdownGuide) };
    const requested = distinct(selection.guides || []), resolved = [], reasons = {}, errors = [], requirements = [];
    const namespaces = new Set();
    const units = new Map(), layers = new Map(), variables = {}, replacements = [], releases = new Set(), documents = [], routerSet = [], workloadPatches = [];
    const mode = requested.some(id => ['coord-disaggregation', 'multi-model-routing'].includes(id)) ? 'gateway' : selection.mode || 'standalone';
    const add = (id, reason) => {
        if (resolved.includes(id)) { if (reason) reasons[id] = distinct([...(reasons[id] || []), reason]); return; }
        const guide = catalog.guides.find(item => item.id === id);
        if (!guide) { errors.push(`Guide ${id} is absent from the selected source revision.`); return; }
        const disposition = guideDisposition(guide, catalog);
        if (disposition.kind !== 'recipe') { errors.push(`${id}: ${disposition.reason}`); return; }
        resolved.push(id);
        if (reason) reasons[id] = [reason];
        const aliases = { 'workload-autoscaling/keda-epp-queue': ['workload-autoscaling/keda-epp', { SIGNAL: 'queue' }], 'workload-autoscaling/keda-epp-saturation': ['workload-autoscaling/keda-epp', { SIGNAL: 'saturation' }], 'multimodal-serving/aggregation': ['multimodal-serving', { TOPOLOGY: 'aggregation' }], 'multimodal-serving/e-disaggregation': ['multimodal-serving', { TOPOLOGY: 'e-pd' }] };
        if (aliases[id]) {
            const [target, parameters] = aliases[id];
            selection = { ...selection, variables: { ...selection.variables, [target]: { ...parameters, ...selection.variables?.[target] } } };
            add(target, `${id} uses this current upstream Guide`);
            return;
        }
        const inferred = { ...selection.variables?.[id] };
        if (!Object.hasOwn(inferred, 'TOPOLOGY') && guide.variables?.TOPOLOGY?.values) {
            const matches = requested.filter(candidate => guide.variables.TOPOLOGY.values.includes(candidate));
            if (matches.length === 1) inferred.TOPOLOGY = matches[0];
        }
        if (Object.hasOwn(guide.variables || {}, 'BASE_GUIDE_NAME') && !Object.hasOwn(inferred, 'BASE_GUIDE_NAME')) {
            const matches = requested.filter(candidate => candidate !== id && guideSteps(catalog.guides.find(item => item.id === candidate)?.steps?.deploy).some(step => /\/modelserver\//.test(step.run)));
            if (matches.length === 1) inferred.BASE_GUIDE_NAME = matches[0];
        }
        const settings = guideVariables(catalog, guide, { ...inferred, ...Object.fromEntries(['MODEL', 'MODEL_NAME'].filter(key => selection.model && Object.hasOwn(guide.variables || {}, key)).map(key => [key, selection.model])), ...(selection.accelerator && Object.hasOwn(guide.variables || {}, 'ACCELERATOR_TYPE') ? { ACCELERATOR_TYPE: selection.accelerator } : {}), NAMESPACE: selection.namespace || 'llm-d', MODE: mode });
        if (id === 'workload-autoscaling/wva') settings.WVA_NAMESPACE = selection.namespace || 'llm-d';
        if (id === 'batch-serving/asynchronous-processing') settings.TARGET_KEY = settings.MQ_PROVIDER === 'redis' ? 'ap.transportConfig.queues[0].igw_base_url' : 'ap.transportConfig.topics[0].igw_base_url';
        variables[id] = settings;
        const originalNamespace = guideVariables(catalog, guide).NAMESPACE;
        if (originalNamespace && !originalNamespace.includes('$')) namespaces.add(originalNamespace);
        // These add-ons explicitly target a serving Guide rather than owning a model workload.
        if (id === 'coord-disaggregation' && mode !== 'gateway') errors.push('coord-disaggregation requires Gateway router mode.');
        if (['workload-autoscaling/keda-epp-token-aware', 'workload-autoscaling/wva', 'batch-serving/asynchronous-processing', 'batch-serving/batch-gateway'].includes(id)) add(settings.TOPOLOGY || 'optimized-baseline', `${id} requires a monitored serving pool`);
        if (id === 'agentic-api') add(settings.BASE_GUIDE_NAME || 'optimized-baseline', `${id} needs an upstream inference service`);
        if (id === 'workload-autoscaling/keda-epp') add('optimized-baseline', `${id} scales the baseline worker pool`);
        if (id === 'workload-autoscaling/slo-aware') add('optimized-baseline', `${id} scales the baseline pool named in its ScaledObject`);
    };
    requested.forEach(id => add(id));
    for (const pair of [['workload-autoscaling/keda-epp-queue', 'workload-autoscaling/keda-epp-saturation'], ['multimodal-serving/aggregation', 'multimodal-serving/e-disaggregation']]) if (pair.every(id => requested.includes(id))) errors.push(`Mutually exclusive source variants: ${pair.join(', ')}.`);
    if (resolved.some(id => id.startsWith('workload-autoscaling/'))) for (const id of ['optimized-baseline', 'pd-disaggregation']) if (variables[id]) variables[id].MONITORING = 'true';
    // Dependencies render first. Stable order also keeps router feature layers after base values.
    resolved.sort((a, b) => Number(requested.includes(a)) - Number(requested.includes(b)));
    for (const id of resolved) {
        const guide = catalog.guides.find(item => item.id === id), values = variables[id];
        if (!values) continue;
        const accelerator = guide.support?.accelerators?.[values.ACCELERATOR_TYPE];
        if (guide.support?.accelerators) {
            const engine = accelerator?.engines?.[values.MODEL_SERVER];
            if (!accelerator || !engine || engine === 'unsupported' || engine.status === 'unsupported') errors.push(`${id}: ${values.ACCELERATOR_TYPE || 'unset hardware'} / ${values.MODEL_SERVER} is not supported by this source revision.`);
        }
        for (const [key, spec] of Object.entries(guide.variables || {})) if (key !== 'NAMESPACE' && spec?.values && !spec.values.map(String).includes(String(values[key]))) errors.push(`${id}: ${key} must be one of ${spec.values.join(', ')}.`);
        const steps = guideSteps(guide.steps?.deploy);
        if (!steps.length) {
            const chart = catalog.assets.filter(asset => asset.guide === id && asset.kind === 'chart');
            if (id === 'runtime-inventory' && chart.length === 1) units.set(chart[0].path, { path: chart[0].path, guide: id, reason: 'Runtime inventory controller', chartVersion: chart[0].version });
            else errors.push(`${id}: this source section has no declarative deployment steps. Its integration procedure cannot be treated as a complete Kubernetes preset.`);
            continue;
        }
        if (id === 'multi-model-routing') {
            const chart = catalog.assets.find(asset => asset.guide === id && asset.gitRepository);
            if (!chart) errors.push(`${id}: the pinned payload-processor chart source is not available.`);
            else units.set(chart.path, { path: chart.path, guide: id, reason: 'Payload processor extracts model identity for HTTPRoute matching' });
        }
        if (id === 'batch-serving/batch-gateway') {
            const pvc = catalog.assets.find(asset => asset.guide === id && asset.kind === 'resource' && asset.content?.includes('kind: PersistentVolumeClaim'));
            if (pvc) units.set(pvc.path, { path: pvc.path, guide: id, reason: 'Shared batch file storage from the source Guide' });
        }
        const prereqs = guideSteps(guide.steps?.prerequisites);
        requirements.push(...prereqs.filter(step => !/(?:^|\/)(clone|namespace)(?:\/|$)/.test(step.path)).map(step => ({ guide: id, step: step.path, instruction: step.run })));
        const activeSteps = [...prereqs.filter(step => /^router\//.test(step.path)), ...steps];
        for (const step of activeSteps) {
            if (id === 'p2p-kv-cache-sharing' && step.path.startsWith('calibrate/')) { requirements.push({ guide: id, step: step.path, instruction: 'Run the upstream calibration after the serving endpoint is ready; use its measurements to tune the latency predictor.' }); continue; }
            if (id === 'workload-autoscaling/wva' && step.path.startsWith('prometheus_ca/') || id === 'workload-autoscaling/keda-epp-token-aware' && step.path.startsWith('prometheus_auth/')) { requirements.push({ guide: id, step: step.path, instruction: 'Provision the Prometheus credential Secret in the target namespace as described by the pinned Guide. Generation never copies cluster credentials.' }); continue; }
            if (Object.entries(step.when || {}).some(([key, allowed]) => !allowed.map(String).includes(String(values[key])))) continue;
            if (step.path.split('/').includes(mode === 'standalone' ? 'gateway' : 'standalone')) continue;
            if (step.path.startsWith('modelserver/dynamic_slice') && !String(values.ACCELERATOR_TYPE).includes('dynamic-slice')) continue;
            if (step.path.startsWith('modelserver/standard') && String(values.ACCELERATOR_TYPE).includes('dynamic-slice')) continue;
            for (let line of clean(deploymentRun(step)).split('\n')) {
                line = line.trim(); if (!line) continue;
                const assignment = line.match(/^export\s+([A-Z][A-Z0-9_]*)=(.*)$/);
                if (assignment) {
                    const [, key, raw] = assignment;
                    if (raw.includes('$(')) { if (!values[key]) requirements.push({ guide: id, step: step.path, instruction: `Provide ${key} from the target environment.` }); continue; }
                    if (key === 'ACCELERATOR_TYPE' && selection.accelerator) continue;
                    if (!Object.hasOwn(selection.variables?.[id] || {}, key)) values[key] = tokens(expand(raw, values))[0] ?? '';
                    continue;
                }
                line = expand(line, values);
                if (id === 'workload-autoscaling/wva' && step.path.startsWith('controller/') && !line.startsWith('kubectl apply')) continue;
                if (/kubectl.*(?:apply|create).*(?:https:\/\/)/.test(line)) requirements.push({ guide: id, step: step.path, instruction: line });
                if (/\bhelm\s+(install|upgrade)\b/.test(line)) {
                    const args = tokens(line), chart = args.find(arg => arg.startsWith('oci://'));
                    if (chart?.includes('/llm-d-router-')) {
                        const command = args.indexOf('helm');
                        const release = args.slice(command + 2).find(arg => !arg.startsWith('-'));
                        if (release) releases.add(release);
                        for (let i = 0; i < args.length; i++) if (['-f', '--values'].includes(args[i])) {
                            const path = sourcePath(args[i + 1]), asset = catalog.assets.find(item => item.path === path && item.kind === 'values');
                            if (!asset) errors.push(`${id}: router values ${path} are missing from the source inventory.`);
                            else layers.set(path, { path, reason: `${id}: ${step.path}` });
                        }
                        for (let i = 0; i < args.length; i++) if (args[i] === '--set') routerSet.push(args[i + 1]);
                    } else {
                        const asset = catalog.assets.find(item => item.kind === 'chart' && item.chart === chart && item.guide === id);
                        const valuePaths = args.flatMap((arg, index) => ['-f', '--values'].includes(arg) ? [sourcePath(args[index + 1])] : []);
                        const sets = args.flatMap((arg, index) => arg === '--set' ? [args[index + 1]] : []);
                        if (!asset || valuePaths.some(path => !catalog.assets.some(item => item.path === path && item.kind === 'values'))) errors.push(`${id}: chart inputs for ${step.path} are absent from this source inventory.`);
                        else units.set(asset.path, { path: asset.path, guide: id, chartVersion: args.includes('--version') ? args[args.indexOf('--version') + 1] : asset.version, chartValues: valuePaths, chartSet: sets, variables: values, reason: `${id}: ${step.path}` });
                    }
                    continue;
                }
                const paths = [...line.matchAll(/(?:-k\s+|\bkustomize\s+(?:build\s+)?|-f\s+|envsubst\s*<\s*)["']?(SOURCE\/[^\s"'|;]+)/g)].map(match => sourcePath(match[1]));
                for (const path of paths) {
                    const asset = catalog.assets.find(item => item.path === path && item.kind !== 'values');
                    if (!asset) errors.push(`${id}: component ${path} is unavailable. Select a supported Guide variant.`);
                    else units.set(path, { path, guide: id, variables: values, edits: [], reason: `${id}: ${step.path}` });
                }
                // Source-prescribed literal substitutions, e.g. baseline labels in flow-control.
                for (const match of line.matchAll(/\bsed\s+["']s\/([^/]+)\/([^/]+)\/g["']/g)) replacements.push({ from: match[1], to: match[2], paths });
                if (id === 'workload-autoscaling/kueue-rebalancing' && line.startsWith('kubectl patch deployment ')) {
                    const args = tokens(line), flag = args.indexOf('-p');
                    const names = args.slice(3, args.findIndex((arg, index) => index > 2 && arg.startsWith('-')));
                    const patch = flag >= 0 ? yaml.load(args[flag + 1], { schema: yaml.JSON_SCHEMA }) : null;
                    if (!names.length || !object(patch) || !object(patch.spec)) errors.push(`${id}: cannot resolve the source workload patch.`);
                    else for (const name of names) workloadPatches.push({ kind: 'Deployment', name, namespace: selection.namespace || 'llm-d', patch, guide: id });
                    continue;
                }
                const binding = line.match(/^kubectl create (rolebinding|clusterrolebinding)\s/);
                if (binding) {
                    const args = tokens(line), flag = name => args.find(arg => arg.startsWith(name + '='))?.slice(name.length + 1);
                    const account = flag('--serviceaccount')?.split(':'), role = flag('--role') || flag('--clusterrole');
                    if (account?.length === 2 && role) documents.push({ apiVersion: 'rbac.authorization.k8s.io/v1', kind: binding[1] === 'rolebinding' ? 'RoleBinding' : 'ClusterRoleBinding', metadata: { name: args[3], ...(binding[1] === 'rolebinding' ? { namespace: selection.namespace || 'llm-d' } : {}) }, roleRef: { apiGroup: 'rbac.authorization.k8s.io', kind: flag('--role') ? 'Role' : 'ClusterRole', name: role }, subjects: [{ kind: 'ServiceAccount', namespace: account[0], name: account[1] }] });
                    else errors.push(`${id}: ${step.path} has an unresolved role binding.`);
                    continue;
                }
                if (/\b(?:kubectl\s+(?:create\s+(?!namespace)|patch)|sed\s+-i|kustomize\s+edit)\b/.test(line)) errors.push(`${id}: ${step.path} changes resources imperatively; the preset requires a declarative adapter for that operation.`);
                if (/\b(?:bash|python\S*|sh)\s|\.sh(?:\s|$)/.test(line) && !line.startsWith('echo')) errors.push(`${id}: ${step.path} requires generated inputs from an upstream script.`);
            }
        }
    }
    const workloads = [...units.values()].filter(isServingSourceUnit);
    const owners = distinct(workloads.map(unit => unit.guide));
    if (owners.length > 1) errors.push(`Competing serving topologies: ${owners.join(', ')}. Select one complete serving Guide and compatible add-ons; whole model workloads cannot be overlaid safely.`);
    if (releases.size > 1) errors.push(`Conflicting router releases: ${[...releases].join(', ')}. These Guides address different inference pools.`);
    if (resolved.includes('workload-autoscaling/keda-epp')) requirements.push({ guide: 'workload-autoscaling/keda-epp', step: 'controllers', instruction: 'KEDA and the Prometheus monitoring stack must be ready in the target cluster.' });
    if (requested.length && !workloads.length && !resolved.some(id => ['runtime-inventory', 'multi-model-routing', 'workload-autoscaling/multi-inference-pool', 'workload-autoscaling/kueue-rebalancing'].includes(id))) errors.push('The selected Guides do not provide a model workload. Select a serving Guide or an existing-workload integration.');
    const orderedLayers = [...layers.values()].sort((a, b) => Number(!a.path.endsWith('/router/base.values.yaml')) - Number(!b.path.endsWith('/router/base.values.yaml')));
    return { existingModelIntegration: resolved.includes('multi-model-routing'), workloadPatches, existingPoolIntegration: resolved.includes('workload-autoscaling/multi-inference-pool'), requested, guides: resolved, reasons, documents, sourceNamespaces: [...namespaces], units: [...units.values()], variables, replacements, requirements, errors: distinct(errors), namespaceLabels: resolved.includes('runtime-inventory') ? { 'aibom.k8saibom.dev/enabled': 'true' } : {}, router: { mode, release: [...releases][0] || 'llm-d', version: catalog.environment?.ROUTER_CHART_VERSION || '', set: routerSet, values: orderedLayers } };
}
