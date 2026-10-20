import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import yaml from 'js-yaml';
import { adaptMarkdownGuide, guideDisposition } from '../../../frontend/src/features/inference-designer/guideAdapters.js';
import { applyEngineCapabilities, applyRouterCapabilities, assertRenderedCapabilities, capabilityPolicyDefaults } from '../../../frontend/src/features/inference-designer/capabilityRendering.js';
import { officialGuideIndex } from '../../../frontend/src/features/inference-designer/officialGuides.js';
import { deploymentSelection } from '../../../frontend/src/features/inference-designer/deploymentSelection.js';
import { resolveGuidePlan } from '../../../frontend/src/features/inference-designer/guidePlan.js';
import { applyKueueWorkloadPatch } from './guideCompositionInputs.ts';
import { bindCompositionModel, compositionBackendRequest } from './guideCompositionModel.ts';
import { applyCompositionRuntime } from './compositionRuntime.ts';
import { compositionReferences, compositionWorkloads } from './guideCompositionReferences.ts';
import { storagePath } from '../infrastructure/storagePaths.ts';

/* Source YAML is intentionally schema-dynamic. */
/* eslint-disable @typescript-eslint/no-explicit-any */
const execute = promisify(execFile);
const hash = (text: string | Uint8Array) => crypto.createHash('sha256').update(text).digest('hex');
const mapping = (x: any) => x !== null && typeof x === 'object' && !Array.isArray(x);
const fail = (message: string) => Object.assign(new Error(message), { status: 400 });
const excluded = /\/(?:benchmark[^/]*|test|tests|diagram|dashboards)\//;
const parse = (text: string): any[] => yaml.loadAll(text, undefined, { schema: yaml.JSON_SCHEMA }).filter(Boolean);
const dump = (docs: any[]) => docs.map(doc => yaml.dump(doc, { lineWidth: -1, noRefs: true })).join('---\n');

async function command(binary: string, args: string[]) {
    try { return (await execute(binary, args, { encoding: 'utf8', timeout: 90_000, maxBuffer: 24 * 1024 * 1024 })).stdout; }
    catch (error: any) { throw fail(`${binary} rendering failed: ${String(error.stderr || error.code || 'unknown error').slice(0, 2000)}`); }
}

async function pullChart(chart: string, version: string, directory: string) {
    await command('helm', ['pull', chart, '--version', version, '--destination', directory]);
    const archives = (await fs.readdir(directory)).filter(file => file.endsWith('.tgz'));
    if (archives.length !== 1) throw fail('Chart pull did not return one archive.');
    const archive = path.join(directory, archives[0]);
    return { archive, checksum: hash(await fs.readFile(archive)) };
}

/** Source-owned external charts are pinned before generation and never execute shell steps. */
async function sourceGitChart(asset: any, requestedCommit: string | undefined, directory: string) {
    if (!/^https:\/\/github\.com\/llm-d\/[\w-]+\.git$/.test(asset.gitRepository) || !/^[\w/-]+$/.test(asset.chartDirectory) || asset.chartDirectory.includes('..')) throw fail('Unsupported external chart source.');
    const commit = requestedCommit || (await command('git', ['ls-remote', asset.gitRepository, 'HEAD'])).split(/\s/)[0];
    if (!/^[a-f0-9]{40}$/.test(commit || '')) throw fail('Pin an exact external chart commit.');
    const checkout = path.join(directory, 'source');
    await fs.mkdir(checkout);
    await command('git', ['-C', checkout, 'init', '--quiet']);
    await command('git', ['-C', checkout, 'fetch', '--quiet', '--depth=1', asset.gitRepository, commit]);
    await command('git', ['-C', checkout, 'checkout', '--quiet', '--detach', 'FETCH_HEAD']);
    const chartPath = await fs.realpath(path.join(checkout, asset.chartDirectory));
    if (!chartPath.startsWith(`${await fs.realpath(checkout)}${path.sep}`)) throw fail('External chart escapes its source checkout.');
    const metadata = parse(await fs.readFile(path.join(chartPath, 'Chart.yaml'), 'utf8'))[0];
    if (metadata.dependencies?.length) {
        await fs.access(path.join(chartPath, 'Chart.lock')).catch(() => { throw fail('External chart dependencies require a committed Chart.lock.'); });
        await command('helm', ['dependency', 'build', chartPath]);
    }
    await command('helm', ['package', chartPath, '--destination', directory]);
    const archive = path.join(directory, `${metadata.name}-${metadata.version}.tgz`);
    return { archive, checksum: hash(await fs.readFile(archive)), commit, version: metadata.version };
}

/** Source must be a cluster-authorized checkout; no caller-supplied filesystem paths. */
export async function guideSourceInventory(root: string) {
    const commit = (await command('git', ['-C', root, 'rev-parse', 'HEAD'])).trim();
    if (!/^[a-f0-9]{40}$/.test(commit)) throw fail('Invalid source commit.');
    const dirty = await command('git', ['-C', root, 'status', '--porcelain', '--untracked-files=no', '--', 'guides', 'helpers']);
    if (dirty.trim()) throw fail('Guide source has local modifications. Use a clean pinned checkout before generating.');
    const paths = (await command('git', ['-C', root, 'ls-tree', '-r', '--name-only', commit, '--', 'guides', 'helpers', 'docs/infrastructure'])).trim().split('\n');
    const guideRoots = paths.filter(p => p.startsWith('guides/') && /\/(README\.md|guide\.yaml)$/.test(p) && !excluded.test(p) && !p.includes('/recipes/') && !p.includes('/modelserver/') && !p.includes('/manifests/') && !p.includes('/templates/'))
        .map(p => path.posix.dirname(p)).filter(p => p !== 'guides');
    const roots = [...new Set(guideRoots)];
    const guides = await Promise.all(roots.map(async directory => {
        const definition = paths.includes(`${directory}/guide.yaml`) ? parse(await fs.readFile(path.join(root, directory, 'guide.yaml'), 'utf8'))[0] : {};
        const readme = paths.includes(`${directory}/README.md`) ? await fs.readFile(path.join(root, directory, 'README.md'), 'utf8') : '';
        return { id: directory.slice(7), title: readme.match(/^#\s+(.+)$/m)?.[1] || definition.name || directory.slice(7),
            source: `${directory}/${definition.name ? 'guide.yaml' : 'README.md'}`, variables: definition.env?.static || {}, support: definition.support || {},
            instructions: readme, steps: { prerequisites: definition.prerequisites || {}, deploy: definition.deploy || {} } };
    }));
    const assets: any[] = [];
    for (const file of paths.filter(p => /\.ya?ml$/.test(p) && !excluded.test(p) && !p.endsWith('/guide.yaml'))) {
        const real = await fs.realpath(path.join(root, file));
        if (!real.startsWith(`${await fs.realpath(root)}${path.sep}`)) throw fail('Source symlink escapes checkout.');
        const content = await fs.readFile(real, 'utf8');
        let docs: any[];
        try { docs = parse(content); } catch { continue; }
        const component = /\/kustomization\.ya?ml$/.test(file) && docs[0]?.kind === 'Component';
        const kustomization = /\/kustomization\.ya?ml$/.test(file) && !component;
        const resource = docs.length > 0 && docs.every(d => d.apiVersion && d.kind && d.metadata?.name && !['Kustomization', 'Component'].includes(d.kind)) && !/\/(?:patch[^/]*|.*\.patch)\.ya?ml$/.test(file);
        const values = !kustomization && !resource && docs.length <= 1 && (mapping(docs[0]) || content.trim().startsWith('#')) && !docs[0]?.kind && /(?:values|\/router\/)/.test(file);
        if (component && Object.keys(docs[0]).every(key => ['apiVersion', 'kind', 'resources', 'configurations'].includes(key)) && (docs[0].resources || []).every((entry: string) => !entry.includes('://'))) {
            // Standalone resource-only components can be represented by their source resources.
            assets.push({ path: path.posix.dirname(file), file, guide: 'recipes', kind: 'component', checksum: hash(content), resources: docs[0].resources || [], content });
        }
        if (!kustomization && !resource && !values) continue;
        const owner = roots.filter(dir => file.startsWith(`${dir}/`)).sort((a, b) => b.length - a.length)[0];
        const requiredFiles: string[] = [];
        if (kustomization) for (const generator of [...(docs[0].configMapGenerator || []), ...(docs[0].secretGenerator || [])]) {
            for (const input of [...(generator.files || []), ...(generator.envs || [])]) {
                const relative = String(input).split('=').at(-1)!;
                const full = path.resolve(root, path.posix.dirname(file), relative);
                if (!full.startsWith(`${path.resolve(root)}${path.sep}`)) throw fail('Generator input escapes the source checkout.');
                try { await fs.access(full); } catch { requiredFiles.push(relative); }
            }
        }
        assets.push({ requiredFiles, path: kustomization ? path.posix.dirname(file) : file, file, guide: owner?.slice(7) || 'recipes', kind: kustomization ? 'kustomize' : resource ? 'resource' : 'values', checksum: hash(content), ...(kustomization ? {} : { content }) });
    }
    // Markdown-only Guides still contribute concrete resource examples and pinned OCI charts.
    for (const file of paths.filter(p => p.endsWith('.md') && !excluded.test(p) && !p.includes('/recipes/'))) {
        const owner = roots.filter(dir => file.startsWith(`${dir}/`)).sort((a, b) => b.length - a.length)[0];
        if (!owner) continue;
        const content = await fs.readFile(path.join(root, file), 'utf8');
        let index = 0;
        for (const block of content.matchAll(/```ya?ml[^\n]*\n([\s\S]*?)```/g)) {
            try {
                const documents = parse(block[1]);
                if (!documents.length || !documents.every(d => d.apiVersion && d.kind && d.metadata?.name)) continue;
                assets.push({ path: `${file}#resource-${index++}`, file, guide: owner.slice(7), kind: 'resource', checksum: hash(block[1]), content: block[1] });
            } catch { /* Documentation fragments are not complete resources. */ }
        }
        // Shell heredocs are data examples, not commands to execute.
        for (const match of content.matchAll(/<<EOF\n([\s\S]*?)\nEOF/g)) {
            try {
                const documents = parse(match[1]);
                if (documents.length && documents.every(doc => doc.apiVersion && doc.kind && doc.metadata?.name)) assets.push({ path: `${file}#heredoc-${index++}`, file, guide: owner.slice(7), kind: 'resource', checksum: hash(match[1]), content: match[1] });
            } catch { /* Incomplete examples are not resource inputs. */ }
        }
        if (owner === 'guides/multi-model-routing') {
            const source = content.match(/git clone (https:\/\/github\.com\/llm-d\/[\w-]+\.git) (\/tmp\/[\w-]+)/);
            const chart = source && content.match(new RegExp(`helm install ([\\w-]+) ${source[2]}(/[^\\s]+)`));
            if (source && chart) assets.push({ path: `${file}#payload-processor-chart`, file, guide: owner.slice(7), kind: 'chart', gitRepository: source[1], chartDirectory: chart[2].slice(1), release: chart[1], version: '', checksum: hash(chart[0]) });
        }
        const variables: Record<string, string> = {};
        const definition = guides.find(guide => guide.id === owner.slice(7));
        for (const [key, spec] of Object.entries<any>(definition?.variables || {})) {
            const value = mapping(spec) ? spec.default : spec;
            if (['string', 'number'].includes(typeof value) && !String(value).includes('$(')) variables[key] = String(value);
        }
        for (const declaration of content.matchAll(/export\s+([A-Z][A-Z0-9_]*)=["']?([^\s"'`]+)["']?/g)) if (!declaration[2].includes('$(')) variables[declaration[1]] ||= declaration[2];
        const commands = content.replace(/\\\n\s*/g, ' ').replace(/\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)\b/g, (match, braced, bare) => variables[braced || bare] ?? match);
        for (const match of commands.matchAll(/helm\s+(?:install|upgrade\s+--install)\s+([\w.-]+)\s+(oci:\/\/[^\s\\`]+)([^\n]*)/g)) {
            const version = match[3].match(/--version\s+["']?([v0-9][A-Za-z0-9._+-]*)/);
            const variable = match[3].match(/--version\s+\$\{?([A-Z][A-Z0-9_]*)/);
            const exported = variable && commands.match(new RegExp(`export\\s+${variable[1]}=["']?([v0-9][A-Za-z0-9._+-]*)`));
            const chartVersion = version?.[1] || exported?.[1] || '';
            const key = `${match[2]}@${chartVersion}`;
            if (assets.some(a => a.guide === owner.slice(7) && a.chartKey === key)) continue;
            assets.push({ path: `${file}#chart-${index++}`, file, guide: owner.slice(7), kind: 'chart', checksum: hash(match[0]), chart: match[2], version: chartVersion, release: match[1], chartKey: key });
        }
    }
    const environment: Record<string, string> = {};
    try {
        const shared = await fs.readFile(path.join(root, 'guides/env.sh'), 'utf8');
        for (const match of shared.matchAll(/export\s+([A-Z][A-Z0-9_]*)=\$\{[A-Z][A-Z0-9_]*:-([^}]+)\}/g)) if (!match[2].includes('$(')) environment[match[1]] = match[2];
    } catch { /* Older source revisions need explicit chart versions. */ }
    const indexText = paths.includes('guides/README.md') ? await fs.readFile(path.join(root, 'guides/README.md'), 'utf8') : '';
    const catalog = { commit, guides: guides.map(adaptMarkdownGuide), assets, environment, officialGuides: officialGuideIndex(indexText, guides) };
    return { ...catalog, guides: catalog.guides.map(guide => ({ ...guide, disposition: guideDisposition(guide, catalog) })) };
}

/** Typed scalar changes preserve resource structure and reject stale/nonexistent paths. */
export function editSourceDocuments(documents: any[], edits: any[] = []) {
    const result = structuredClone(documents);
    if (!Array.isArray(edits) || edits.length > 10000) throw fail('Invalid parameter edits.');
    for (const edit of edits) {
        if (!Array.isArray(edit.path) || !edit.path.length || edit.path.some((key: any) => !['string', 'number'].includes(typeof key) || ['__proto__', 'constructor', 'prototype'].includes(String(key)))) throw fail('Invalid parameter path.');
        let owner: any = result;
        for (const key of edit.path.slice(0, -1)) {
            if (!owner || !Object.hasOwn(owner, key)) throw fail('Parameter path no longer exists. Reload source.');
            owner = owner[key];
        }
        const key = edit.path.at(-1);
        if (!owner || !Object.hasOwn(owner, key)) throw fail('Parameter path no longer exists. Reload source.');
        const previous = owner[key];
        if (previous !== null && typeof previous === 'object') throw fail('Structured parameters must be edited through their scalar fields.');
        if (typeof previous !== typeof edit.value || (typeof edit.value === 'number' && !Number.isFinite(edit.value))) throw fail('Parameter type does not match source.');
        owner[key] = edit.value;
    }
    return result;
}

const clusterKinds = new Set(['Namespace', 'Node', 'PersistentVolume', 'StorageClass', 'CustomResourceDefinition', 'ClusterRole', 'ClusterRoleBinding', 'GatewayClass', 'RuntimeClass', 'PriorityClass', 'MutatingWebhookConfiguration', 'ValidatingWebhookConfiguration', 'APIService', 'ClusterQueue', 'ResourceFlavor']);

/** Exact duplicate resources collapse; competing definitions never silently overwrite. */
export function composeSourceDocuments(groups: { name: string; documents: any[] }[], namespace: string) {
    if (!/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(namespace) || namespace.length > 63) throw fail('Enter a valid Kubernetes namespace.');
    const resources = new Map<string, { document: any; owner: string }>();
    const warnings = new Set<string>();
    const scopes = new Map<string, string>();
    for (const group of groups) for (const doc of group.documents) if (doc.kind === 'CustomResourceDefinition') scopes.set(`${doc.spec?.group}/${doc.spec?.names?.kind}`, doc.spec?.scope);
    const add = (input: any, owner: string) => {
        if (input.kind === 'List') { for (const doc of input.items || []) add(doc, owner); return; }
        const doc = structuredClone(input);
        if (!doc.apiVersion || !doc.kind || !doc.metadata?.name) throw fail(`${owner}: output contains a patch or values file, not a complete resource.`);
        const group = String(doc.apiVersion).includes('/') ? String(doc.apiVersion).split('/')[0] : '';
        if (!clusterKinds.has(doc.kind) && scopes.get(`${group}/${doc.kind}`) !== 'Cluster') doc.metadata.namespace ||= namespace;
        const checkTemplates = (value: any, shellScript = false) => {
            if (typeof value === 'string') {
                if (/\bREPLACE_[A-Z_]+\b|<your[- _][^>]+>|[a-z-]+-placeholder/.test(value) || (!shellScript && /\$\{[A-Z][A-Z0-9_]*(?::[^}]*)?\}/.test(value))) throw fail(`${owner}: unresolved template values remain. Configure them before generating.`);
            } else if (Array.isArray(value)) value.forEach(child => checkTemplates(child, shellScript));
            else if (mapping(value)) {
                // LWS and container environments supply shell variables at runtime.
                // These are not envsubst placeholders in Kubernetes resource fields.
                const shell = Array.isArray(value.command) && value.command.some((part: unknown) => typeof part === 'string' && /(?:^|\/)(?:ba)?sh$/.test(part));
                for (const [key, child] of Object.entries(value)) checkTemplates(child, shell && ['command', 'args'].includes(key));
            }
        };
        checkTemplates(doc);
        const id = `${group}/${doc.kind}/${doc.metadata.namespace || ''}/${doc.metadata.name}`;
        const previous = resources.get(id);
        if (previous && !deepEqual(previous.document, doc)) throw fail(`Resource conflict: ${id} from ${previous.owner} and ${owner}. Choose one workload recipe or resolve the parameters.`);
        resources.set(id, { document: doc, owner });
        if (group && !['apps', 'batch', 'autoscaling', 'policy', 'rbac.authorization.k8s.io', 'networking.k8s.io', 'storage.k8s.io', 'apiextensions.k8s.io'].includes(group)) warnings.add(`Target cluster must provide ${doc.apiVersion} ${doc.kind}.`);
    };
    for (const group of groups) for (const doc of group.documents) add(doc, group.name);
    return { documents: [...resources.values()].map(x => x.document), warnings: [...warnings] };
}
function deepEqual(a: any, b: any): boolean {
    if (a === b) return true;
    if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
    const keys = Object.keys(a); return keys.length === Object.keys(b).length && keys.every(k => Object.hasOwn(b, k) && deepEqual(a[k], b[k]));
}

function substituteVariables(documents: any[], inventory: any, asset: any, selection: any) {
    const definitions = inventory.guides.find((guide: any) => guide.id === asset.guide)?.variables || {};
    const variables: Record<string, string> = {};
    for (const [key, spec] of Object.entries<any>(definitions)) {
        const value = mapping(spec) ? spec.default : spec;
        if (['string', 'number', 'boolean'].includes(typeof value) && !String(value).includes('$(')) variables[key] = String(value);
    }
    for (const [key, value] of Object.entries(selection.variables || {})) {
        if (!/^[A-Z][A-Z0-9_]*$/.test(key) || !['string', 'number', 'boolean'].includes(typeof value)) throw fail('Invalid Guide variable.');
        variables[key] = String(value);
    }
    if (selection.namespace) variables.NAMESPACE = selection.namespace;
    const expand = (text: string) => (text === 'REPLACE_MODEL_SERVER_IMAGE' && selection.runtimeImage ? selection.runtimeImage : text).replace(/\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)\b/g, (match, braced, bare) => variables[braced || bare] ?? match);
    for (let step = 0; step < 8; step++) for (const key of Object.keys(variables)) variables[key] = expand(variables[key]);
    const visit = (value: any): any => typeof value === 'string' ? expand(value) : Array.isArray(value) ? value.map(visit) : mapping(value) ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, visit(child)])) : value;
    return visit(documents);
}

async function renderRawSourceUnit(root: string, inventory: any, selection: any) {
    const asset = inventory.assets.find((x: any) => x.path === selection.path);
    if (!asset || asset.kind === 'values') throw fail('Select a Kubernetes manifest or Kustomize overlay from the source inventory.');
    if (asset.kind === 'chart') {
        const scratch = storagePath('scratch', ['guide-composition']); await fs.mkdir(scratch, { recursive: true });
        const directory = await fs.mkdtemp(path.join(scratch, 'chart-'));
        try {
            const gitChart = asset.gitRepository ? await sourceGitChart(asset, selection.chartCommit, directory) : null;
            const chartVersion = gitChart?.version || selection.chartVersion || asset.version || parse(await command('helm', ['show', 'chart', asset.chart]))[0]?.version;
            if (!/^[v0-9][A-Za-z0-9._+-]{0,80}$/.test(chartVersion || '')) throw fail('Could not resolve chart version.');
            const chart = gitChart || await pullChart(asset.chart, chartVersion, directory);
            const values = selection.values || await command('helm', ['show', 'values', chart.archive]);
            if (typeof values !== 'string' || parse(values).length !== 1 || !mapping(parse(values)[0])) throw fail('Chart values must be one mapping.');
            const valuesPath = path.join(directory, 'values.yaml'); await fs.writeFile(valuesPath, values);
            const args = ['template', asset.release, chart.archive, '--include-crds', '--namespace', selection.namespace || 'default', '-f', valuesPath];
            for (const [index, input] of (selection.chartValues || []).entries()) {
                const source = inventory.assets.find((item: any) => item.path === input && item.kind === 'values');
                if (!source) throw fail('Unknown chart values source.');
                const file = path.join(directory, `source-${index}.yaml`); await fs.writeFile(file, source.content); args.push('-f', file);
            }
            for (const override of selection.chartSet || []) {
                if (typeof override !== 'string' || !override.includes('=')) throw fail('Invalid chart override.');
                args.push('--set', override);
            }
            const content = await command('helm', args);
            const documents = editSourceDocuments(substituteVariables(parse(content), inventory, asset, selection), selection.edits);
            return { path: asset.path, checksum: asset.checksum, documents, values, chartVersion, chartCommit: gitChart?.commit, chartChecksum: chart.checksum, content: dump(documents) };
        } finally { await fs.rm(directory, { recursive: true, force: true }); }
    }
    if (asset.kind === 'component') {
        const resources = asset.resources.map((entry: string) => path.posix.normalize(path.posix.join(asset.path, entry)));
        const documents: any[] = [];
        for (const file of resources) {
            const member = inventory.assets.find((item: any) => item.path === file && item.kind === 'resource');
            if (!member) throw fail(`Component ${asset.path} requires unsupported nested input ${file}.`);
            documents.push(...parse(member.content));
        }
        const edited = editSourceDocuments(substituteVariables(documents, inventory, asset, selection), selection.edits);
        return { path: asset.path, checksum: asset.checksum, documents: edited, content: dump(edited) };
    }
    let content: string;
    if (asset.kind === 'kustomize' && asset.requiredFiles?.length) {
        for (const file of asset.requiredFiles) if (typeof selection.files?.[file] !== 'string' || !selection.files[file].trim()) throw fail(`Provide source input ${file} before rendering ${asset.path}.`);
        const scratch = storagePath('scratch', ['guide-composition']); await fs.mkdir(scratch, { recursive: true });
        const directory = await fs.mkdtemp(path.join(scratch, 'inputs-'));
        try {
            const checkout = path.join(directory, 'source'); await fs.mkdir(checkout);
            const archive = path.join(directory, 'source.tar');
            await command('git', ['-C', root, 'archive', '--format=tar', '--output', archive, inventory.commit]);
            await command('tar', ['-xf', archive, '-C', checkout]);
            for (const file of asset.requiredFiles) {
                const target = path.resolve(checkout, asset.path, file);
                if (!target.startsWith(`${checkout}${path.sep}`)) throw fail('Source input escapes temporary checkout.');
                await fs.mkdir(path.dirname(target), { recursive: true }); await fs.writeFile(target, selection.files[file], { mode: 0o600 });
            }
            content = await command('kubectl', ['kustomize', path.join(checkout, asset.path)]);
        } finally { await fs.rm(directory, { recursive: true, force: true }); }
    } else content = asset.kind === 'kustomize' ? await command('kubectl', ['kustomize', path.join(root, asset.path)]) : asset.content;
    const documents = editSourceDocuments(substituteVariables(parse(content), inventory, asset, selection), selection.edits);
    return { path: asset.path, checksum: asset.checksum, documents, content: dump(documents) };
}

/** Structured edits and complete YAML share source provenance and composition validation. */
export async function renderSourceUnit(root: string, inventory: any, selection: any) {
    const rendered = await renderRawSourceUnit(root, inventory, selection.replacementContent === undefined ? selection : { ...selection, edits: [] });
    if (selection.replacementContent === undefined) return rendered;
    if (typeof selection.replacementContent !== 'string' || selection.replacementContent.length > 2_000_000) throw fail('Resource YAML must be text below 2 MB.');
    const documents = editSourceDocuments(parse(selection.replacementContent), selection.edits);
    if (!documents.length || documents.some(doc => !mapping(doc) || !doc.apiVersion || !doc.kind || !doc.metadata?.name)) throw fail('Resource YAML must contain named Kubernetes resources.');
    return { ...rendered, documents, content: dump(documents) };
}

/** Render explicit source selections; never execute Guide shell scripts or apply to a cluster. */
export async function generateSourceComposition(root: string, request: any, modelBinding?: Parameters<typeof bindCompositionModel>[1], kubeconfig?: string) {
    const inventory = await guideSourceInventory(root);
    if (request.commit !== inventory.commit) throw fail('The source commit changed. Reload source parameters before generating.');
    let resolvedPlan: any = null;
    if (request.guideSelection) {
        const settings = { ...request.guideSelection, model: modelBinding?.model, namespace: request.namespace, variables: request.variables };
        resolvedPlan = request.guideSelection.capabilities !== undefined ? deploymentSelection(inventory, request.guideSelection.guides || [], settings) : resolveGuidePlan(inventory, settings);
        if (resolvedPlan.errors.length) throw fail(resolvedPlan.errors.join('\n'));
        const edits = new Map((request.units || []).map((unit: any) => [unit.path, unit]));
        request = { ...request, units: resolvedPlan.units.map((unit: any) => ({ ...unit, ...(edits.get(unit.path) as object || {}) })), variables: resolvedPlan.variables,
            router: { ...resolvedPlan.router, values: resolvedPlan.router.values.map((layer: any) => ({ ...layer, ...request.router?.values?.find((item: any) => item.path === layer.path) })) } };
    }
    if (!Array.isArray(request.units) || (!request.units.length && !resolvedPlan?.existingPoolIntegration) || request.units.length > 60) throw fail('Select between 1 and 60 source components.');
    composeSourceDocuments([], request.namespace);
    const groups: { name: string; documents: any[] }[] = [];
    const provenance: { path: string; checksum: string; chartChecksum?: string }[] = [];
    for (const selection of request.units) {
        const selectedAsset = inventory.assets.find((asset: any) => asset.path === selection.path);
        if (selectedAsset?.gitRepository && !/^[a-f0-9]{40}$/.test(selection.chartCommit || '')) throw fail('Inspect the payload-processor chart to pin its source commit before generating.');
        if (selectedAsset?.kind === 'chart' && !selectedAsset.version && !selection.chartVersion) throw fail('Inspect the chart to resolve and pin its version before generating.');
        const rendered = await renderSourceUnit(root, inventory, { ...selection, variables: request.variables?.[inventory.assets.find((asset: any) => asset.path === selection.path)?.guide] || selection.variables, namespace: request.namespace, runtimeImage: request.runtimeImage });
        if (resolvedPlan) {
            const substitutions = resolvedPlan.replacements.filter((item: any) => item.paths.includes(selection.path));
            const replace = (value: any): any => typeof value === 'string' ? substitutions.reduce((text: string, item: any) => text.split(item.from).join(item.to), value) : Array.isArray(value) ? value.map(replace) : mapping(value) ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, replace(child)])) : value;
            rendered.documents = replace(rendered.documents);
        }
        rendered.documents = applyEngineCapabilities(rendered.documents, resolvedPlan?.capabilityOverrides);
        groups.push({ name: selection.path, documents: rendered.documents });
        provenance.push({ path: rendered.path, checksum: hash(rendered.content), ...('chartChecksum' in rendered ? { chartChecksum: rendered.chartChecksum } : {}) });
    }
    if (request.router?.values?.length) {
        const { router } = request;
        if (!['standalone', 'gateway'].includes(router.mode) || !/^[a-z0-9][a-z0-9.-]{0,52}$/.test(router.release || '') || !/^[v0-9][A-Za-z0-9._+-]{0,80}$/.test(router.version || '')) throw fail('Configure a valid router mode, release and chart version.');
        const scratch = storagePath('scratch', ['guide-composition']); await fs.mkdir(scratch, { recursive: true });
        const directory = await fs.mkdtemp(path.join(scratch, 'render-'));
        try {
            const chart = await pullChart(`oci://ghcr.io/llm-d/charts/llm-d-router-${router.mode}`, router.version, directory);
            provenance.push({ path: `oci://ghcr.io/llm-d/charts/llm-d-router-${router.mode}@${router.version}`, checksum: chart.checksum });
            const args = ['template', router.release, chart.archive, '--namespace', request.namespace, '--include-crds'];
            for (const [index, layer] of router.values.entries()) {
                const asset = inventory.assets.find((x: any) => x.path === layer.path && x.kind === 'values');
                if (!asset) throw fail('Router layer must be a values file from the selected source.');
                const values = applyRouterCapabilities(layer.content === undefined ? asset.content : layer.content, resolvedPlan?.capabilityOverrides, capabilityPolicyDefaults(inventory));
                if (typeof values !== 'string' || (parse(values).length > 1 || parse(values).length === 1 && !mapping(parse(values)[0]))) throw fail('Router values must be a single YAML mapping.');
                const file = path.join(directory, `${index}.yaml`); await fs.writeFile(file, values); args.push('-f', file);
                provenance.push({ path: asset.path, checksum: hash(values) });
            }
            for (const value of router.set || []) { if (typeof value !== 'string' || !value.includes('=')) throw fail('Invalid router override.'); args.push('--set', value); }
            groups.push({ name: 'router-chart', documents: parse(await command('helm', args)) });
        } finally { await fs.rm(directory, { recursive: true, force: true }); }
    }
    if (resolvedPlan?.documents.length) groups.push({ name: 'guide-dependencies', documents: resolvedPlan.documents });
    if (resolvedPlan) {
        const renameNamespaces = (value: any): any => typeof value === 'string' ? resolvedPlan.sourceNamespaces.reduce((text: string, name: string) => text === name ? request.namespace : text.split(`.${name}.svc`).join(`.${request.namespace}.svc`).split(`namespace="${name}"`).join(`namespace="${request.namespace}"`), value) : Array.isArray(value) ? value.map(renameNamespaces) : mapping(value) ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, renameNamespaces(child)])) : value;
        for (const group of groups) group.documents = renameNamespaces(group.documents);
    }
    if (resolvedPlan?.workloadPatches?.length) {
        if (!kubeconfig) throw fail('Connect the target cluster to resolve the existing Kueue workloads.');
        const existing = new Map<string, any>();
        for (const entry of resolvedPlan.workloadPatches) {
            if (!/^[a-z0-9][a-z0-9.-]*$/.test(entry.name)) throw fail('Invalid existing workload name.');
            const key = `${entry.kind}/${entry.name}`;
            const document = existing.get(key) || JSON.parse(await command('kubectl', ['--kubeconfig', kubeconfig, '--request-timeout=15s', 'get', entry.kind, entry.name, '-n', request.namespace, '-o', 'json']));
            existing.set(key, applyKueueWorkloadPatch(document, entry.patch));
        }
        groups.push({ name: 'existing-kueue-workloads', documents: [...existing.values()] });
    }
    const result = composeSourceDocuments(groups, request.namespace);
    if (!result.documents.length) throw fail('Selected sources produced no resources.');
    assertRenderedCapabilities(result.documents, resolvedPlan?.capabilityOverrides);
    if (!result.documents.some(doc => doc.kind === 'Namespace' && doc.metadata.name === request.namespace)) result.documents.unshift({ apiVersion: 'v1', kind: 'Namespace', metadata: { name: request.namespace } });
    if (resolvedPlan?.guides.includes('workload-autoscaling/wva')) for (const { template } of compositionWorkloads(result.documents)) for (const container of template.spec.containers || []) container.args = container.args?.map((arg: string) => arg.startsWith('--watch-namespace=') ? `--watch-namespace=${request.namespace}` : arg);
    if (modelBinding && !resolvedPlan?.existingModelIntegration && !resolvedPlan?.workloadPatches?.length && !resolvedPlan?.existingPoolIntegration) bindCompositionModel(result.documents, modelBinding, request.runtimeImageOverride ? request.runtimeImage : undefined);
    if (request.runtimeConfiguration) {
        if (!modelBinding) throw fail('Planned runtime controls require a bound cached model.');
        const hardware = await compositionBackendRequest('/api/v1/hardware/capabilities');
        const profile = hardware.profiles?.find((item: any) => item.id === request.runtimeConfiguration.profileId);
        applyCompositionRuntime(result.documents, request.runtimeConfiguration, profile, modelBinding.model);
        assertRenderedCapabilities(result.documents, resolvedPlan?.capabilityOverrides);
    }
    if (resolvedPlan) {
        const namespace = result.documents.find(doc => doc.kind === 'Namespace' && doc.metadata.name === request.namespace);
        namespace.metadata.labels = { ...namespace.metadata.labels, ...resolvedPlan.namespaceLabels };
    }
    const compositionChecksum = hash(dump(result.documents));
    if (request.finalConfiguration !== undefined) {
        const edited = request.finalConfiguration;
        if (!mapping(edited) || edited.baseChecksum !== compositionChecksum) throw fail('The selected deployment changed. Reload the final configuration before editing.');
        if (typeof edited.content !== 'string' || !edited.content.trim() || edited.content.length > 5_000_000) throw fail('Provide a nonempty final deployment YAML smaller than 5 MB.');
        const checked = composeSourceDocuments([{ name: 'final-configuration', documents: parse(edited.content) }], request.namespace);
        if (!checked.documents.length || !checked.documents.some(doc => doc.kind === 'Namespace' && doc.metadata.name === request.namespace)) throw fail('The final configuration must retain its deployment namespace.');
        if (modelBinding && !resolvedPlan?.existingModelIntegration && !resolvedPlan?.workloadPatches?.length && !resolvedPlan?.existingPoolIntegration) bindCompositionModel(checked.documents, modelBinding, request.runtimeImageOverride ? request.runtimeImage : undefined);
        assertRenderedCapabilities(checked.documents, resolvedPlan?.capabilityOverrides);
        result.documents = checked.documents;
        result.warnings.push(...checked.warnings);
    }
    const externalPods: any[] = [];
    if (resolvedPlan?.existingPoolIntegration) {
        if (!kubeconfig) throw fail('Connect the target cluster to resolve the additional inference pool workers.');
        for (const pool of result.documents.filter(doc => doc.kind === 'InferencePool')) {
            const labels = pool.spec?.selector?.matchLabels || pool.spec?.selector;
            if (!mapping(labels) || !Object.keys(labels).length || Object.values(labels).some(value => typeof value !== 'string')) throw fail('The inference pool needs a concrete worker label selector.');
            const selector = Object.entries(labels).map(([key, value]) => `${key}=${value}`).join(',');
            const pods = JSON.parse(await command('kubectl', ['--kubeconfig', kubeconfig, '--request-timeout=15s', 'get', 'pods', '-n', request.namespace, '-l', selector, '-o', 'json']));
            if (!pods.items?.length) throw fail(`No existing model workers match ${selector}. Deploy the additional model first.`);
            externalPods.push(...pods.items.map((pod: any) => ({ ...pod, kind: 'Pod', apiVersion: 'v1' })));
        }
    }
    const references = compositionReferences(result.documents, externalPods);
    if (references.errors.length) throw fail(references.errors.join('\n'));
    result.warnings.push(...references.external.map(ref => `${ref.owner} requires existing ${ref.kind}/${ref.name} in ${ref.namespace}.`));
    if (resolvedPlan) {
        const namespace = result.documents.find(doc => doc.kind === 'Namespace' && doc.metadata.name === request.namespace);
        namespace.metadata.labels = { ...namespace.metadata.labels, ...resolvedPlan.namespaceLabels };
        result.warnings.push(...resolvedPlan.requirements.map((item: any) => `${item.guide}: ${item.instruction}`));
    }
    return { commit: inventory.commit, compositionChecksum, files: [{ name: 'deployment.yaml', content: dump(result.documents) }, { name: 'provenance.json', content: JSON.stringify({ commit: inventory.commit, modelBinding, finalConfiguration: request.finalConfiguration ? { baseChecksum: compositionChecksum, checksum: hash(dump(result.documents)) } : undefined, components: provenance, guidePlan: resolvedPlan, router: request.router ? { mode: request.router.mode, version: request.router.version, release: request.router.release } : null }, null, 2) }], validation: { errors: [], warnings: result.warnings, level: 'rendered', clusterValidated: false }, resourceCount: result.documents.length, existingPoolIntegration: !!resolvedPlan?.existingPoolIntegration };
}

/** Kubernetes admission validation is explicit and uses the selected authorized session. */
export async function validateSourceComposition(content: string, kubeconfig: string, namespace: string, existingPoolIntegration = false) {
    composeSourceDocuments([], namespace);
    const scratch = storagePath('scratch', ['guide-composition']); await fs.mkdir(scratch, { recursive: true });
    const directory = await fs.mkdtemp(path.join(scratch, 'validate-'));
    try {
        const file = path.join(directory, 'deployment.yaml'); await fs.writeFile(file, content, { mode: 0o600 });
        const existing = existingPoolIntegration ? JSON.parse(await command('kubectl', ['--kubeconfig', kubeconfig, '--request-timeout=15s', 'get', 'pods', '-n', namespace, '-o', 'json'])).items.map((pod: any) => ({ ...pod, kind: 'Pod' })) : [];
        const references = compositionReferences(parse(content), existing);
        if (references.errors.length) throw fail(references.errors.join('\n'));
        for (const ref of references.external) await command('kubectl', ['--kubeconfig', kubeconfig, '--request-timeout=15s', 'get', ref.kind, ref.name, '-n', ref.namespace, '-o', 'name']);
        await command('kubectl', ['--kubeconfig', kubeconfig, '--request-timeout=60s', 'apply', '--dry-run=server', '--validate=strict', '-f', file]);
    } finally { await fs.rm(directory, { recursive: true, force: true }); }
}
