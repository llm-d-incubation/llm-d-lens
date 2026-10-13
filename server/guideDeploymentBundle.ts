import { hardwareProfileSnapshot } from './hardwareProfiles.ts';
import { managedImageProfile, profileForKey } from '../src/features/hardware/profiles.js';
import crypto from 'node:crypto';
import fs from 'node:fs';
import yaml from 'js-yaml';

/* Helm values are schema-dynamic and include nested YAML plugin files. */
/* eslint-disable @typescript-eslint/no-explicit-any */
type RecordValue = Record<string, any>;
/* The pinned stack profile is the single source of truth for llm-d component
 * versions (llm_d_bench/versions/llm_d_stack.yaml), shared with the Python
 * backend so the planner and the renderer never disagree. */
const stackProfile = yaml.load(fs.readFileSync(new URL('../llm_d_bench/versions/llm_d_stack.yaml', import.meta.url), 'utf8')) as RecordValue;
export const ROUTER_CHART_VERSION = String(stackProfile.llm_d_router);
export const ROUTER_DISAGG_SIDECAR_IMAGE = `ghcr.io/llm-d/llm-d-router-disagg-sidecar:${ROUTER_CHART_VERSION}`;

/* Some Guides (e.g. tiered-prefix-cache) publish router values per topology
 * (a single-host overlay plus a multi-host LeaderWorkerSet one for multi-chip
 * accelerators). The topology is owned by the hardware profile registry,
 * resolved the same way as the model-server image, so the renderer never
 * hardcodes a single path for the whole guide. */
function routerTopologyForAccelerator(accelerator?: string): string {
    return profileForKey(hardwareProfileSnapshot(), accelerator)?.deployment?.router_topology || 'single-host';
}

export function pinModelServerImage(image: string, profiles = hardwareProfileSnapshot()): string {
    return managedImageProfile(profiles, image)?.deployment?.runtime_image || image;
}
const routerPaths = {
    'optimized-baseline': 'optimized-baseline.values.yaml',
    'pd-disaggregation': 'pd-disaggregation.values.yaml',
    'tiered-prefix-cache': 'tiered-prefix-cache-cpu.values.yaml',
    'precise-prefix-cache-routing': 'precise-prefix-cache-routing.values.yaml',
};
const asset = (name: string, content: string) => ({ name, content, checksum: `sha256:${crypto.createHash('sha256').update(content).digest('hex')}` });
const mapping = (value: any): value is RecordValue => Boolean(value && typeof value === 'object' && !Array.isArray(value));
function valuesYaml(content: string): RecordValue {
    const parsed = yaml.load(content);
    if (!mapping(parsed)) throw new Error('Router values must be a YAML mapping.');
    return parsed;
}
function merge(base: RecordValue, overlay: RecordValue): RecordValue {
    for (const [key, value] of Object.entries(overlay)) {
        if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('Invalid router values key.');
        base[key] = mapping(value) && mapping(base[key]) ? merge(base[key], value) : value;
    }
    return base;
}

/* Mirror configuration/router_compatibility.py for the planning boundary.
 * Original source layers remain intact; only effective values are adapted. */
function adaptRouterPlugins(values: RecordValue): void {
    const epp = values.router?.epp;
    const image = epp?.image || {};
    if (!mapping(image) || (image.repository || 'llm-d-router-endpoint-picker') !== 'llm-d-router-endpoint-picker') return;
    if (!/^v?0\.11\.\d+$/.test(String(image.tag || ROUTER_CHART_VERSION))) return;
    const key = epp?.pluginsConfigFile;
    if (!key || typeof epp.pluginsCustomConfig?.[key] !== 'string') return;
    const document = valuesYaml(epp.pluginsCustomConfig[key]);
    if (!Array.isArray(document.plugins)) throw new Error('Router plugin configuration requires a plugins list');
    const handlers = document.plugins.filter(p => p.type === 'disagg-profile-handler');
    const headers = document.plugins.filter(p => p.type === 'disagg-headers-handler');
    if (headers.length && (handlers.length !== 1 || headers.some(p => p.parameters && Object.keys(p.parameters).length))) {
        throw new Error('Legacy disaggregation headers require one profile handler and no custom header parameters');
    }
    let changed = headers.length > 0;
    for (const handler of handlers) {
        const parameters = handler.parameters || {};
        if (!Object.hasOwn(parameters, 'deciderPluginName')) continue;
        const legacy = parameters.deciderPluginName;
        parameters.deciders ||= {};
        if (Object.hasOwn(parameters.deciders, 'prefill') && parameters.deciders.prefill !== legacy) {
            throw new Error('Conflicting legacy and current P/D deciders');
        }
        parameters.deciders.prefill = legacy;
        delete parameters.deciderPluginName;
        handler.parameters = parameters;
        changed = true;
    }
    if (changed) {
        document.plugins = document.plugins.filter(p => p.type !== 'disagg-headers-handler');
        epp.pluginsCustomConfig[key] = yaml.dump(document, { lineWidth: -1 });
    }
}

/* Guides may publish their router values flat (`router/<file>`) or, on llm-d
 * versions/topologies that split per-topology, under a topology directory
 * (`router/<topology>/<file>`). Try the topology path first and fall back to
 * the flat one, so neither an older pinned llm-d ref nor a guide that never
 * adopted the split breaks, and a new split never needs a code change. */
async function readRouterValues(
    guide: string, routerPath: string, accelerator: string | undefined,
    // eslint-disable-next-line no-unused-vars
    readSource: (path: string) => Promise<string>,
): Promise<string> {
    const topology = routerTopologyForAccelerator(accelerator);
    try {
        return await readSource(`guides/${guide}/router/${topology}/${routerPath}`);
    } catch {
        return readSource(`guides/${guide}/router/${routerPath}`);
    }
}

export async function buildGuideDeploymentBundle({ guide, source, model, blockSize, routerValues = '', accelerator, readSource, renderSource }: {
    guide: string; source: RecordValue; model: string; blockSize?: number; routerValues?: string; accelerator?: string;
    // eslint-disable-next-line no-unused-vars
    readSource: (path: string) => Promise<string>; renderSource: (path: string) => Promise<string>;
}) {
    const routerPath = routerPaths[guide];
    if (!routerPath) throw new Error('This Guide does not have a supported deployment bundle.');
    const [baseText, guideText] = await Promise.all([
        readSource('guides/recipes/router/base.values.yaml'),
        readRouterValues(guide, routerPath, accelerator, readSource),
    ]);
    const values = merge(valuesYaml(baseText), valuesYaml(guideText));
    if (routerValues.trim()) merge(values, valuesYaml(routerValues));
    if (guide === 'precise-prefix-cache-routing') {
        const epp = values.router?.epp;
        const key = epp?.pluginsConfigFile;
        const plugins = key && typeof epp.pluginsCustomConfig?.[key] === 'string' ? valuesYaml(epp.pluginsCustomConfig[key]) : null;
        const tokenProducer = plugins?.plugins?.find(item => item.type === 'token-producer');
        const index = plugins?.plugins?.find(item => ['precise-prefix-cache-producer', 'precise-prefix-cache-scorer'].includes(item.type));
        if (!tokenProducer || !index) throw new Error('Precise routing requires token-producer and precise-prefix-cache plugins.');
        tokenProducer.parameters = { ...tokenProducer.parameters, modelName: model };
        if (blockSize == null || !Number.isSafeInteger(blockSize) || blockSize <= 0) throw new Error('Precise routing requires an explicit or inherited model-server block size.');
        index.parameters ||= {};
        index.parameters.tokenProcessorConfig = { ...index.parameters.tokenProcessorConfig, blockSizeTokens: blockSize };
        epp.pluginsCustomConfig[key] = yaml.dump(plugins, { lineWidth: -1 });
        if (Number(epp.replicas ?? 1) !== 1) throw new Error('Precise token-load routing currently requires one EPP replica.');
    }
    adaptRouterPlugins(values);
    const resources: ReturnType<typeof asset>[] = [];
    let calibration: ReturnType<typeof asset>[] | undefined;
    if (guide === 'precise-prefix-cache-routing') {
        const [rendered, script, jobTemplate] = await Promise.all([
            renderSource('guides/precise-prefix-cache-routing/render'),
            readSource('guides/recipes/router/calibration/calibrate.sh'),
            readSource('guides/recipes/router/calibration/calibration-peak-throughput.yaml'),
        ]);
        const baselineRoot = new URL('../llm_d_bench/deploy/providers/guide_overlays/precise-prefix-cache-routing/baseline/', import.meta.url);
        const baseline = valuesYaml(fs.readFileSync(new URL('service.yaml', baselineRoot), 'utf8'));
        const overlay = valuesYaml(fs.readFileSync(new URL('kustomization.yaml', baselineRoot), 'utf8'));
        baseline.metadata.name = `${overlay.namePrefix || ''}${baseline.metadata.name}`;
        for (const label of overlay.labels || []) baseline.metadata.labels = { ...baseline.metadata.labels, ...label.pairs };
        resources.push(asset('render.yaml', rendered), asset('baseline.yaml', yaml.dump(baseline)));
        /* calibrate.sh reads this Job template from its own directory. */
        calibration = [asset('calibrate.sh', script), asset('calibration-peak-throughput.yaml', jobTemplate)];
    }
    // Keep original layers for audit, and one merged effective layer for install.
    return {
        schemaVersion: 'guide-deployment-bundle.v1', guide, sourceCommit: source.commit,
        helm: { chart: 'oci://ghcr.io/llm-d/charts/llm-d-router-standalone', version: ROUTER_CHART_VERSION, releaseName: guide,
            values: [asset('router-base.yaml', baseText), asset('router-guide.yaml', guideText), asset('router-effective.yaml', yaml.dump(values, { lineWidth: -1 }))],
        },
        resources, ...(calibration ? { calibration } : {}),
    };
}
