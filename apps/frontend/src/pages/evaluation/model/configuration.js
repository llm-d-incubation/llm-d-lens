import { configurationChecksum, sha256 } from './checksum.js';

export const RUNTIME_CONTROL_ARGUMENTS = ['max-model-len', 'max-num-seqs', 'gpu-memory-utilization', 'block-size', 'max-num-batched-tokens'];

export function comparisonConfigurationDraft(source, provider) {
    const content = structuredClone(source.deployable_configuration.content);
    // The comparison arm renders through the legacy per-provider overlay (it
    // has no officialGuide/renderedManifest of its own), so the cluster's
    // accelerator must be carried forward explicitly rather than re-derived.
    const accelerator = content.officialGuide?.source?.accelerator || content.accelerator;
    delete content.officialGuide;
    // Guide-specific cache/network/router tuning must be reviewed for each comparison arm.
    delete content.guideSettings;
    if (accelerator) content.accelerator = accelerator;
    if (provider.id !== 'pd-disaggregation') {
        delete content.prefill;
        content.customParameters = (content.customParameters || content.custom_parameters || []).filter(item => item.target !== 'prefill');
        delete content.custom_parameters;
    }
    content.guideVariant = provider.evaluation?.default_variant || provider.variants?.[0] || '';
    return { deployable_configuration: { provider_ref: provider.id, content } };
}

export function configurationCustomRows(content = {}) {
    return (content.customParameters || content.custom_parameters || [])
        .filter(item => !((content.runtime?.modelServer || 'vllm') === 'vllm' && item.kind === 'argument' && item.target === 'both' && RUNTIME_CONTROL_ARGUMENTS.includes(item.name)))
        .map(item => ({ ...item, value: String(item.value) }));
}

// Keep the saved batch bound aligned with EvaluationCreateRequest.benchmark_plans.
export const MAX_EVALUATION_CONFIGURATIONS = 256;
export const MAX_TOPOLOGY_COMBINATIONS = 100_000;

export function parseTopologyValues(value) {
    const values = new Set();
    for (const part of String(value ?? '').replaceAll('，', ',').split(',')) {
        const match = /^(\d+)(?:\s*-\s*(\d+)(?:\s*\/\s*(\d+))?)?$/.exec(part.trim());
        if (!match) throw new Error('Enter positive integers, lists (1,2,4), or ranges (1-8 or 1-8/2).');
        const first = Number(match[1]), last = match[2] ? Number(match[2]) : first, step = match[3] ? Number(match[3]) : 1;
        if (![first, last, step].every(number => Number.isSafeInteger(number) && number > 0) || first > last) throw new Error('Use increasing positive integer ranges and a positive step.');
        const count = Math.floor((last - first) / step) + 1;
        if (count > 128) throw new Error('Each parameter supports up to 128 distinct values.');
        for (let index = 0; index < count; index++) {
            values.add(first + index * step);
            if (values.size > 128) throw new Error('Each parameter supports up to 128 distinct values.');
        }
    }
    return [...values].sort((a, b) => a - b);
}

export function topologySweepFromConfiguration({ prefillReplicaCount = 1, prefillTpCount = 1, replicaCount = 1, tpCount = 1 } = {}) {
    return { prefillReplicaVariants: String(prefillReplicaCount), prefillTensorParallelVariants: String(prefillTpCount), replicaVariants: String(replicaCount), tensorParallelVariants: String(tpCount), relation: 'all', maxAccelerators: '', fixedAccelerators: '' };
}

export function analyzeConfigurationSweep(sweep, maxConfigurations = MAX_EVALUATION_CONFIGURATIONS) {
    const values = {}, errors = [], variants = [];
    const result = { values, errors, variants, rawCount: 0 };
    for (const field of ['prefillReplicaVariants', 'prefillTensorParallelVariants', 'replicaVariants', 'tensorParallelVariants']) {
        try { values[field] = parseTopologyValues(sweep[field]); }
        catch (error) { errors.push({ field: `sweep.${field}`, message: error.message }); }
    }
    const relation = sweep.relation || 'all';
    if (!['all', 'equal', 'decode', 'prefill'].includes(relation)) errors.push({ field: 'sweep.relation', message: 'Select a supported P/D replica relationship.' });
    for (const field of ['maxAccelerators', 'fixedAccelerators']) {
        const value = sweep[field];
        values[field] = value == null || String(value).trim() === '' ? null : Number(value);
        if (values[field] !== null && (!Number.isSafeInteger(values[field]) || values[field] < 1)) errors.push({ field: `sweep.${field}`, message: 'Enter a positive integer, or leave blank for no limit.' });
    }
    if (errors.length) return result;
    result.rawCount = values.prefillReplicaVariants.length * values.prefillTensorParallelVariants.length * values.replicaVariants.length * values.tensorParallelVariants.length;
    if (result.rawCount > MAX_TOPOLOGY_COMBINATIONS) {
        errors.push({ field: 'topologies', message: `Narrow the parameter ranges to at most ${MAX_TOPOLOGY_COMBINATIONS.toLocaleString('en-US')} raw combinations.` });
        return result;
    }
    for (const prefillReplicaCount of values.prefillReplicaVariants) for (const replicaCount of values.replicaVariants) {
        if ((relation === 'equal' && prefillReplicaCount !== replicaCount) || (relation === 'decode' && prefillReplicaCount > replicaCount) || (relation === 'prefill' && prefillReplicaCount < replicaCount)) continue;
        for (const prefillTpCount of values.prefillTensorParallelVariants) for (const tpCount of values.tensorParallelVariants) {
            const gpuCount = prefillReplicaCount * prefillTpCount + replicaCount * tpCount;
            if (!Number.isSafeInteger(gpuCount)) {
                errors.push({ field: 'topologies', message: 'Accelerator totals are too large. Reduce the parameter values.' });
                return { ...result, variants: [] };
            }
            if ((values.maxAccelerators !== null && gpuCount > values.maxAccelerators) || (values.fixedAccelerators !== null && gpuCount !== values.fixedAccelerators)) continue;
            variants.push({ prefillReplicaCount, prefillTpCount, replicaCount, tpCount, gpuCount });
        }
    }
    if (!variants.length) errors.push({ field: 'topologies', message: 'No configurations match these filters. Widen the filters or change the parameter ranges.' });
    else if (variants.length > maxConfigurations) errors.push({ field: 'topologies', message: maxConfigurations === 1 ? 'Direct deployment requires one configuration. Narrow the ranges or filters to one result.' : `Keep at most ${maxConfigurations} configurations per Evaluation. Narrow the ranges or filters before saving.` });
    return result;
}

export function parsePdReplicaPairs(pdTopologyVariants) {
    return [...new Set(pdTopologyVariants.split(',').map(item => item.trim()))].map(item => {
        if (!/^\d+\s*:\s*\d+$/.test(item)) throw new Error('Explicit P/D topologies require P:D pairs, such as 1:1,1:3.');
        const pair = item.split(':').map(Number);
        if (pair.some(value => !Number.isSafeInteger(value) || value < 1)) throw new Error('P/D replica counts must be positive integers.');
        return pair;
    });
}

export function configurationTopologyErrors({ guide, topologies }) {
    if (!Array.isArray(topologies) || !topologies.length || topologies.length > 16) {
        return [{ field: 'topologies', message: 'Enter between 1 and 16 configurations.' }];
    }
    const fields = ['replicaCount', 'tpCount', ...(guide === 'pd-disaggregation' ? ['prefillReplicaCount', 'prefillTpCount'] : [])];
    return topologies.flatMap((row, index) => fields.flatMap(field => {
        const value = row?.[field];
        return !['number', 'string'].includes(typeof value) || !String(value).trim() || !Number.isSafeInteger(Number(value)) || Number(value) < 1
            ? [{ field: `topologies.${index}.${field}`, message: 'Enter a positive whole number.' }] : [];
    }));
}

export function expandConfigurationTopologies({ guide, replicaVariants, tensorParallelVariants, prefillReplicaVariants, prefillTensorParallelVariants, pdTopologyVariants = '', topologies, sweep, maxConfigurations = MAX_EVALUATION_CONFIGURATIONS }) {
    if (sweep !== undefined) {
        const result = analyzeConfigurationSweep(sweep, maxConfigurations);
        if (result.errors.length) throw new Error(result.errors[0].message);
        return result.variants;
    }
    if (topologies !== undefined) {
        const errors = configurationTopologyErrors({ guide, topologies });
        if (errors.length) throw new Error(errors[0].message);
        return topologies.map(row => {
            const prefillReplicaCount = guide === 'pd-disaggregation' ? Number(row.prefillReplicaCount) : 0;
            const prefillTpCount = guide === 'pd-disaggregation' ? Number(row.prefillTpCount) : 0;
            const replicaCount = Number(row.replicaCount), tpCount = Number(row.tpCount);
            return { prefillReplicaCount, prefillTpCount, replicaCount, tpCount, gpuCount: prefillReplicaCount * prefillTpCount + replicaCount * tpCount };
        });
    }
    const parse = (value, label) => {
        const parts = String(value).split(',').map(item => item.trim());
        const values = parts.map(Number);
        if (parts.some(item => !item) || values.some(item => !Number.isSafeInteger(item) || item < 1)) throw new Error(`${label} must contain positive integers.`);
        return [...new Set(values)];
    };
    const isPd = guide === 'pd-disaggregation';
    const tps = parse(tensorParallelVariants, 'TP variants');
    const prefillTps = isPd ? parse(prefillTensorParallelVariants, 'Prefill TP variants') : [0];
    let pairs;
    if (isPd && pdTopologyVariants.trim()) {
        pairs = parsePdReplicaPairs(pdTopologyVariants);
    } else {
        const replicas = parse(replicaVariants, 'Replica variants');
        const prefill = isPd ? parse(prefillReplicaVariants, 'Prefill replica variants') : [0];
        pairs = prefill.flatMap(p => replicas.map(d => [p, d]));
    }
    if (pairs.length * prefillTps.length * tps.length > 16) throw new Error('A configuration sweep is limited to 16 topology combinations.');
    return pairs.flatMap(([prefillReplicaCount, replicaCount]) => prefillTps.flatMap(prefillTpCount => tps.map(tpCount => ({
        prefillReplicaCount, replicaCount, prefillTpCount, tpCount,
        gpuCount: prefillReplicaCount * prefillTpCount + replicaCount * tpCount,
    }))));
}

// Reads slice_capacity output: null means the ceiling is unknown, not zero.
export function maxFeasibleSlices(capacity, policy) {
    if (!capacity) return null;
    const type = policy?.type || 'None';
    if (type === 'None') return capacity.maxSlices?.None ?? null;
    const key = (capacity.topologyKeys || []).find(item => item.key === policy.topology);
    if (!key) return null;
    return type === 'ExclusiveTopology' ? key.exclusiveTopologyDomains : key.feasibleDomains;
}

export function sliceHistoryMatches(entry, shape) {
    return entry.prefillReplicas === shape.prefillReplicas
        && entry.prefillTensorParallelSize === shape.prefillTp
        && entry.decodeReplicas === shape.decodeReplicas
        && entry.decodeTensorParallelSize === shape.decodeTp
        && (!entry.accelerator || !shape.accelerator || entry.accelerator === shape.accelerator);
}

// Fewest Slices whose measured SLO-stable rate meets the target, among runs with the same per-Slice shape.
export function sliceRecommendation(history = [], shape, { targetRps, maxSlices, evidenceKey } = {}) {
    const matched = history.filter(entry => sliceHistoryMatches(entry, shape));
    const measured = matched.filter(entry => typeof entry.maximumStableQps === 'number' && entry.slices && (!evidenceKey || entry.evidenceKey === evidenceKey));
    const target = Number(targetRps);
    if (!(target > 0)) return { status: measured.length ? 'needs-target' : 'no-evidence', matched };
    const meeting = measured.filter(entry => entry.maximumStableQps >= target);
    const feasible = meeting
        .filter(entry => maxSlices == null || entry.slices <= maxSlices)
        .sort((a, b) => a.slices - b.slices || String(b.finishedAt || '').localeCompare(String(a.finishedAt || '')));
    if (feasible.length) return { status: 'verified', entry: feasible[0], matched };
    return { status: meeting.length ? 'exceeds-capacity' : measured.length ? 'unmet' : 'no-evidence', matched };
}

export function sloTargetsLabel(targets = {}) {
    const parts = [];
    if (targets.ttft_ms != null) parts.push(`TTFT ≤ ${targets.ttft_ms} ms`);
    if (targets.tpot_ms != null) parts.push(`TPOT ≤ ${targets.tpot_ms} ms`);
    if (targets.success_rate_min_percent != null) parts.push(`success ≥ ${targets.success_rate_min_percent}%`);
    return parts.join(', ') || 'default success-rate SLO only';
}

// Distinct workload/SLO profiles among SLO-measured runs, newest first.
export function sliceEvidenceProfiles(entries = []) {
    const profiles = new Map();
    for (const entry of [...entries].sort((a, b) => String(b.finishedAt || '').localeCompare(String(a.finishedAt || '')))) {
        if (typeof entry.maximumStableQps !== 'number' || !entry.evidenceKey || profiles.has(entry.evidenceKey)) continue;
        profiles.set(entry.evidenceKey, { key: entry.evidenceKey, label: `${entry.workloadLabel || 'workload'} · ${sloTargetsLabel(entry.slaTargets)}` });
    }
    return [...profiles.values()];
}

export function expandSliceVariants(variants, sliceCounts, maxConfigurations = 16) {
    const counts = [...new Set(sliceCounts || [])].filter(count => Number.isSafeInteger(count) && count >= 1).sort((a, b) => a - b);
    if (variants.length * Math.max(1, counts.length) > maxConfigurations) throw new Error(`A configuration sweep is limited to ${maxConfigurations} combinations including Slice counts.`);
    if (!counts.length) return variants.map(variant => ({ ...variant, slices: null }));
    return variants.flatMap(variant => counts.map(slices => ({ ...variant, slices })));
}

function caseSliceFacts(entry) {
    const evaluationCase = entry.case || entry;
    const configuration = evaluationCase.deployment_configuration || {};
    const content = configuration.content || {};
    const settings = content.disaggregatedSet || {};
    const prefill = content.prefill || {};
    const decode = content.decode || content.serving || {};
    const shape = [prefill.replicaCount, prefill.tensorParallelSize, decode.replicaCount, decode.tensorParallelSize];
    const slices = settings.slices ?? configuration.provenance?.slice_sweep?.slices ?? null;
    if (evaluationCase.kind !== 'guide' || !slices || !shape.every(Number.isInteger)) return null;
    const metrics = evaluationCase.metrics || entry.evaluation?.metrics || {};
    return {
        caseId: evaluationCase.id,
        status: evaluationCase.status,
        artifactId: evaluationCase.configuration_artifact_id || null,
        model: content.model?.name || '',
        guide: configuration.provider_ref || '',
        scenarioId: evaluationCase.scenario_id || '',
        slaTargets: evaluationCase.sla_targets || {},
        slices,
        placementPolicy: settings.placementPolicy || null,
        sliceRouting: settings.sliceRouting || 'inherit',
        shape,
        accelerators: slices * (shape[0] * shape[1] + shape[2] * shape[3]),
        maximumStableQps: typeof metrics.maximum_stable_qps === 'number' ? metrics.maximum_stable_qps : null,
        throughputTps: typeof metrics.throughput_tps === 'number' ? metrics.throughput_tps : null,
    };
}

// Cases that differ only in Slice count form one comparison group.
export function sliceOptimizationGroups(caseEntries = []) {
    const groups = new Map();
    for (const facts of caseEntries.map(caseSliceFacts).filter(Boolean)) {
        const key = JSON.stringify([facts.model, facts.guide, facts.scenarioId, facts.shape, facts.placementPolicy, facts.sliceRouting]);
        if (!groups.has(key)) groups.set(key, { key, model: facts.model, guide: facts.guide, shape: facts.shape, placementPolicy: facts.placementPolicy, sliceRouting: facts.sliceRouting, slaTargets: facts.slaTargets, rows: [] });
        groups.get(key).rows.push(facts);
    }
    return [...groups.values()]
        .map(group => ({ ...group, rows: group.rows.sort((a, b) => a.slices - b.slices) }))
        .filter(group => new Set(group.rows.map(row => row.slices)).size > 1);
}

export function sliceGroupRecommendation(group, targetRps) {
    const measured = group.rows.filter(row => row.status === 'succeeded' && row.maximumStableQps != null);
    if (!measured.length) return { status: 'no-slo-rate' };
    const target = Number(targetRps);
    if (!(target > 0)) return { status: 'needs-target' };
    const row = measured.filter(item => item.maximumStableQps >= target).sort((a, b) => a.accelerators - b.accelerators || a.slices - b.slices)[0];
    return row ? { status: 'verified', row } : { status: 'unmet' };
}

export const RUNTIME_CONTROL_FIELDS = {
    maxModelLen: 'max-model-len', maxNumSeqs: 'max-num-seqs',
    gpuMemoryUtilization: 'gpu-memory-utilization', blockSize: 'block-size',
    maxNumBatchedTokens: 'max-num-batched-tokens',
};

export function runtimeControlOverrides(values, modelServer) {
    if (modelServer !== 'vllm') return [];
    return Object.entries(RUNTIME_CONTROL_FIELDS)
        .filter(([field]) => values[field] != null && String(values[field]).trim() !== '')
        .map(([field, name]) => ({ target: 'both', kind: 'argument', name, value: String(values[field]) }));
}

export function configurationRuntimeValues(content) {
    const parameters = content.customParameters || content.custom_parameters || [];
    const decode = content.decode || content.serving || {};
    const defaults = {
        maxModelLen: decode.maxModelLen ?? decode.max_model_len ?? '',
        maxNumSeqs: decode.maxNumSeqs ?? decode.max_num_seqs ?? '',
    };
    return Object.fromEntries(Object.entries(RUNTIME_CONTROL_FIELDS).map(([field, name]) => {
        const matches = parameters.filter(item => item.kind === 'argument' && item.name === name);
        const shared = matches.find(item => item.target === 'both');
        const value = shared ? shared.value : matches.length ? '' : defaults[field] ?? '';
        return [field, String(value)];
    }));
}

/** Keep published configurations immutable while recording an edited manifest. */
export async function editedEvaluationConfiguration(artifact, manifest, { now = () => new Date().toISOString() } = {}) {
    const source = artifact.deployable_configuration;
    const content = {
        ...source.content,
        officialGuide: {
            ...(source.content?.officialGuide || {}),
            renderedManifest: manifest,
            manifestChecksum: await sha256(manifest),
        },
    };
    return {
        ...source,
        content,
        checksum: await configurationChecksum(content),
        provenance: {
            ...(source.provenance || {}),
            edited_from_artifact_id: artifact.artifact_id,
            edited_at: now(),
        },
    };
}
