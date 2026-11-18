import { useEffect, useId, useRef, useState } from 'react';
import { Check, Play, Square } from 'lucide-react';
import { Badge } from '../../../shared/ui/Badge';
import { Button } from '../../../shared/ui/Button';
import { Checkbox, Input, Label, Select, Textarea } from '../../../shared/ui/FormControls';
import { usePolling } from '../../../shared/lib/usePolling';
import { profileForKey } from '@repo/shared/hardware/profiles';
import { checkEstimateSupport, estimateConfiguration, loadEstimateTools } from './OptimizationWorkspace/configurationBackend';

const TOOLS = [{ id: 'aisimulate', label: 'AISimulate' }, { id: 'blis', label: 'BLIS' }];
const numberOrNull = value => value === '' ? null : Number(value);
const emptyHardware = { profile_id: '', model: '', nodes: '', devices_per_node: '', fabric: '' };

function initialEstimateDraft(hasBlis, engineSettings = {}) {
    return {
        engineSettings: JSON.stringify(engineSettings, null, 2),
        distribution: hasBlis ? 'constant' : 'native_mean', inputTokens: 4000, outputTokens: 1000,
        prefixTokens: 0, includesPrefix: true,
        loadMode: hasBlis ? 'rate' : 'none', load: hasBlis ? 1 : '',
        seed: hasBlis ? 42 : '', numRequests: hasBlis ? 100 : '', horizon: hasBlis ? 300 : '',
        deadline: '', timeout: '',
    };
}

export function clusterEstimateHardware(hardware, profiles) {
    const accelerators = hardware?.accelerators || [];
    if (accelerators.length !== 1) return {};
    const accelerator = accelerators[0];
    const profile = profileForKey(profiles, accelerator?.id || accelerator);
    if (!profile) return {};
    const models = [...new Set([accelerator.model, ...(accelerator.models || [])].filter(Boolean))];
    const matchedModels = models.filter(model => (profile.planning?.aic_system_models || []).some(mapping => {
        try { return new RegExp(mapping.pattern, 'i').test(model); }
        catch { return false; }
    }));
    const model = models.length === 1 ? models[0] : matchedModels.length === 1 ? matchedModels[0] : null;
    const nodes = (hardware.nodeSummaries || []).filter(node => node.gpuByProfile?.[profile.id] > 0);
    const counts = [...new Set(nodes.map(node => node.gpuByProfile[profile.id]))];
    const capacity = counts.length === 1 ? { nodes: nodes.length, devices_per_node: counts[0] }
        : hardware.gpuNodes === 1 && accelerator.gpuCount > 0 ? { nodes: 1, devices_per_node: accelerator.gpuCount } : {};
    return { profile_id: profile.id, ...(model ? { model } : {}), ...capacity };
}

export function prefillEstimateHardware(current, discovered, edited = new Set()) {
    return { ...emptyHardware, ...discovered, ...Object.fromEntries(Object.entries(current).filter(([key]) => edited.has(key))) };
}

export function prefillEstimateEngine(configuration, discovery, runtimeProfile, edited = false) {
    if (edited || configuration.engine_version) return configuration;
    const profiles = discovery.filter(item => item.tool === 'blis' && item.available && (!runtimeProfile || item.runtime_profile === runtimeProfile));
    if (profiles.length !== 1) return configuration;
    const versions = [...new Set((profiles[0].approved_inputs || [])
        .map(input => input.configuration)
        .filter(input => input?.engine === configuration.engine && input.engine_version)
        .map(input => input.engine_version))];
    return versions.length === 1 ? { ...configuration, engine_version: versions[0] } : configuration;
}

export function prefillEstimateSettings(settings, configuration, discovery, runtimeProfile, edited = false, workload) {
    if (edited || !settings || typeof settings !== 'object' || Array.isArray(settings)) return settings;
    const profiles = discovery.filter(item => item.tool === 'blis' && item.available && (!runtimeProfile || item.runtime_profile === runtimeProfile));
    if (profiles.length !== 1) return settings;
    const approved = (profiles[0].approved_inputs || []).map(input => input.configuration)
        .filter(input => input?.engine === configuration.engine && (!configuration.engine_version || input.engine_version === configuration.engine_version));
    if (!approved.length) return settings;
    const defaults = {};
    for (const key of ['block_size', 'max_num_seqs', 'max_num_batched_tokens', 'gpu_memory_utilization']) {
        const values = [...new Set(approved.map(input => input.engine_settings?.[key]))];
        const valid = key === 'gpu_memory_utilization'
            ? Number.isFinite(values[0]) && values[0] > 0 && values[0] <= 1
            : Number.isInteger(values[0]) && values[0] > 0;
        if (!Object.hasOwn(settings, key) && values.length === 1 && valid) defaults[key] = values[0];
    }
    if (workload && !Object.hasOwn(settings, 'max_model_len')) {
        const window = Number(workload.inputTokens) + Number(workload.outputTokens) + (workload.includesPrefix ? 0 : Number(workload.prefixTokens));
        if (Number.isSafeInteger(window) && window > 0) defaults.max_model_len = window;
    }
    return Object.keys(defaults).length ? { ...defaults, ...settings } : settings;
}

export function estimateHardwareOptions(profiles, clusterHardware, current) {
    return profiles.flatMap(profile => {
        const models = new Set((profile.planning?.aic_system_models || []).flatMap(mapping => {
            const model = mapping.system.replaceAll('_', ' ');
            try { return new RegExp(mapping.pattern, 'i').test(model) ? [model] : []; }
            catch { return []; }
        }));
        for (const accelerator of clusterHardware?.accelerators || []) {
            if (profileForKey([profile], accelerator?.id || accelerator)) {
                for (const model of [accelerator.model, ...(accelerator.models || [])].filter(Boolean)) models.add(model);
            }
        }
        if (current?.profile_id === profile.id && current.model) models.add(current.model);
        return [...models, ''].map(model => ({ profile_id: profile.id, model, label: model ? `${profile.display_name} / ${model}` : `${profile.display_name} / Unspecified model` }));
    });
}

export function estimateCardCapacities(hardware, selected, profiles = []) {
    const accelerators = hardware?.accelerators || [];
    const accelerator = accelerators.find(item => item.id === selected.profile_id);
    const models = [...new Set([accelerator?.model, ...(accelerator?.models || [])].filter(Boolean))];
    const normalize = model => model.trim().toLowerCase().replaceAll('_', ' ').replace(/\s+/g, ' ');
    const selectedModel = normalize(selected.model);
    const mappings = profileForKey(profiles, selected.profile_id)?.planning?.aic_system_models || [];
    const canonical = model => {
        const matched = mappings.filter(mapping => normalize(mapping.system) === model);
        return matched.length === 1 ? matched[0] : null;
    };
    const selectedMapping = canonical(selectedModel);
    const matches = model => {
        const observed = normalize(model);
        if (observed === selectedModel) return true;
        const mapping = selectedMapping || canonical(observed);
        if (!mapping) return false;
        try { return new RegExp(mapping.pattern, 'i').test(selectedMapping ? observed : selectedModel); }
        catch { return false; }
    };
    const nodes = (hardware?.nodeSummaries || []).filter(node => node.gpuByProfile?.[selected.profile_id] > 0);
    const matching = nodes.filter(node => {
        const observed = node.gpuModelsByProfile?.[selected.profile_id] || models;
        return observed.some(matches);
    });
    if (matching.length) {
        const available = matching.map(node => node.availableGpuByProfile?.[selected.profile_id]);
        return {
            all: matching.map(node => node.gpuByProfile[selected.profile_id]),
            available: available.every(value => Number.isInteger(value) && value >= 0) ? available : null,
        };
    }
    const discovered = clusterEstimateHardware(hardware, profiles);
    if (!nodes.length && accelerator && (models.length === 1 && matches(models[0]) || discovered.profile_id === selected.profile_id && discovered.model && matches(discovered.model)) && hardware.gpuNodes === 1) {
        return {
            all: [accelerator.gpuCount],
            available: accelerators.length === 1 && Number.isInteger(hardware.availableGpuCount) ? [hardware.availableGpuCount] : null,
        };
    }
    return { all: null, available: null };
}

export function estimateCardSelection(current, capacities) {
    if (current.mode === 'custom' || capacities[current.mode]) return current;
    return capacities.all
        ? { mode: 'all', count: capacities.all.reduce((total, count) => total + count, 0) }
        : { mode: 'custom', count: current.count };
}

export function estimateCardTopology(count, capacities, declared) {
    if (!Number.isInteger(count) || count < 1) throw new Error('Declare a positive whole number of cards.');
    if (!capacities?.length) throw new Error('Card capacity is unavailable for this accelerator.');
    if (declared?.nodes * declared?.devices_per_node === count && capacities.filter(capacity => capacity >= declared.devices_per_node).length >= declared.nodes) return { nodes: declared.nodes, devices_per_node: declared.devices_per_node };
    for (let nodes = 1; nodes <= capacities.length; nodes += 1) {
        const devices_per_node = count / nodes;
        if (Number.isInteger(devices_per_node) && capacities.filter(capacity => capacity >= devices_per_node).length >= nodes) return { nodes, devices_per_node };
    }
    throw new Error('This card count cannot fit the observed nodes in the simulator\'s uniform cards-per-node layout.');
}

export function estimateSemanticProblems(effective, selection, draft) {
    if (!selection.tools.includes('aisimulate')) return [];
    const problems = [];
    if (effective.model.precision || effective.model.quantization) problems.push('AISimulate cannot verify explicit precision or quantization.');
    let settings;
    try { settings = JSON.parse(draft.engineSettings); } catch { settings = null; }
    if (effective.configuration.engine_version || Object.keys(settings || {}).length || Object.keys(effective.configuration.router_settings || {}).length) problems.push('AISimulate cannot verify explicit engine versions, engine settings or router settings.');
    if (effective.configuration.roles.some(role => (role.pp ?? 1) !== 1 || (role.dp ?? 1) !== 1 || role.expert_parallel)) problems.push('AISimulate exact estimates require PP 1, DP 1 and expert parallel disabled.');
    if (draft.distribution !== 'native_mean' || Number(draft.prefixTokens)) problems.push('AISimulate supports native mean token lengths, not an explicit distribution or shared prefix.');
    if (draft.loadMode !== 'none' && draft.load !== '') problems.push('AISimulate exact estimates cannot honor a request-rate or concurrency target.');
    if (['seed', 'numRequests', 'horizon', 'deadline', 'timeout'].some(key => draft[key] !== '' && draft[key] != null)) problems.push('AISimulate cannot honor explicit BLIS run controls.');
    return problems;
}

export function editorEstimateContext({ model, engine, roles, overrides = [], routerValues = '' }) {
    const configuration = { mode: roles.length === 1 ? 'agg' : 'disagg', engine: engine || 'unknown', roles: roles.map(role => ({ ...role })), engine_settings: {} };
    const effectiveModel = { name: model };
    const typed = value => value === '' || value === 'true' ? true : value === 'false' ? false : Number.isFinite(Number(value)) ? Number(value) : value;
    for (const row of overrides.filter(item => item.name?.trim())) {
        const key = row.name.trim().replace(/^--/, '').replaceAll('-', '_');
        if (row.kind !== 'argument' || (row.target && row.target !== 'both')) {
            configuration.engine_settings.explicit_overrides = [...(configuration.engine_settings.explicit_overrides || []), row];
        } else if (['quantization', 'dtype'].includes(key)) effectiveModel[key === 'dtype' ? 'precision' : key] = row.value;
        else if (['pipeline_parallel_size', 'data_parallel_size', 'enable_expert_parallel'].includes(key)) {
            for (const role of configuration.roles) role[{ pipeline_parallel_size: 'pp', data_parallel_size: 'dp', enable_expert_parallel: 'expert_parallel' }[key]] = typed(row.value);
        } else configuration.engine_settings[key] = typed(row.value);
    }
    if (routerValues.trim()) configuration.router_settings = { declared_values: routerValues };
    return { model: effectiveModel, configuration };
}

export function selectedEstimateTools(current, tool, checked) {
    const tools = checked ? [...new Set([...current.tools, tool])] : current.tools.filter(item => item !== tool);
    if (!tools.length) return current;
    return { tools, runtime_profiles: Object.fromEntries(Object.entries(current.runtime_profiles || {}).filter(([key]) => tools.includes(key))) };
}

export function buildEstimateRequest(effective, selection, draft, mode = 'deployment_exact') {
    if (!selection.tools.length) throw new Error('Select at least one estimate tool.');
    if (!effective.hardware.profile_id.trim() || !effective.hardware.model.trim()) throw new Error('Select an accelerator and device model.');
    if (['nodes', 'devices_per_node'].some(key => !Number.isInteger(effective.hardware[key]) || effective.hardware[key] < 1)) throw new Error('Declare a positive whole number of nodes and devices per node.');
    const engineSettings = mode === 'native_defaults' ? {} : JSON.parse(draft.engineSettings);
    if (!engineSettings || typeof engineSettings !== 'object' || Array.isArray(engineSettings)) throw new Error('Engine settings must be a JSON object.');
    const configuration = { ...effective.configuration, engine_settings: engineSettings };
    const hardware = { ...effective.hardware, fabric: effective.hardware.fabric || null };
    const request = {
        operation: 'estimate', ...selection, model: effective.model, hardware, configuration,
        workload: {
            distribution: draft.distribution, input_tokens: Number(draft.inputTokens), output_tokens: Number(draft.outputTokens),
            prefix_tokens: Number(draft.prefixTokens), input_includes_prefix: draft.includesPrefix,
            request_rate: draft.loadMode === 'rate' ? numberOrNull(draft.load) : null,
            concurrency: draft.loadMode === 'concurrency' ? numberOrNull(draft.load) : null,
        },
        run: Object.fromEntries(Object.entries({
            seed: draft.seed, num_requests: draft.numRequests, horizon_s: draft.horizon,
            request_deadline_s: draft.deadline, wall_timeout_s: draft.timeout,
        }).map(([key, value]) => [key, numberOrNull(value)])),
    };
    const finite = value => {
        if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('Numeric values must be finite.');
        if (value && typeof value === 'object') Object.values(value).forEach(finite);
    };
    if (mode === 'native_defaults') {
        if (selection.tools.length !== 1 || selection.tools[0] !== 'aisimulate') throw new Error('Native defaults require AISimulate-only selection.');
        request.model = { name: effective.model.name, precision: null, quantization: null };
        request.configuration = {
            ...configuration, engine_version: null, engine_settings: {}, router_settings: {},
            roles: configuration.roles.map(role => ({ ...role, pp: 1, dp: 1, expert_parallel: false })),
        };
        request.workload = { ...request.workload, distribution: 'native_mean', prefix_tokens: 0, input_includes_prefix: true, request_rate: null, concurrency: null };
        request.run = Object.fromEntries(Object.keys(request.run).map(key => [key, null]));
    }
    finite(request);
    return request;
}

export function buildEstimateRequests(effective, selection, draft) {
    if (!selection.tools.length) throw new Error('Select at least one estimate tool.');
    return Object.fromEntries(selection.tools.map(tool => [tool, buildEstimateRequest(effective, {
        tools: [tool], runtime_profiles: selection.runtime_profiles?.[tool] ? { [tool]: selection.runtime_profiles[tool] } : {},
    }, draft, tool === 'aisimulate' ? 'native_defaults' : 'deployment_exact')]));
}

export function EstimateOutcome({ tool, state, result, support, request }) {
    const status = result?.status || state || support?.status || 'not_run';
    const reason = result?.error || support?.reason;
    return <article className="min-w-0 border-t border-theme-border py-3" aria-label={`${tool} estimate result`}>
        <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-sm font-semibold text-theme-text">{TOOLS.find(item => item.id === tool)?.label || tool}</h4>
            <Badge tone={['succeeded', 'supported'].includes(status) ? 'success' : ['failed', 'timed_out', 'unsupported'].includes(status) ? 'danger' : 'neutral'}>{status.replaceAll('_', ' ')}</Badge>
            {result?.constraint_assessment && result.constraint_assessment !== 'unknown' && <span className="text-xs text-theme-muted">Constraints: {result.constraint_assessment}</span>}
        </div>
        {reason && <p className="mt-2 break-words text-xs text-theme-muted">{reason.code}: {reason.message}</p>}
        {tool === 'aisimulate' && reason?.code === 'model_backend_unsupported' && request && <p className="mt-2 break-words text-xs text-theme-muted">Model: {request.model.name} · Accelerator: {request.hardware.model} · Engine: {request.configuration.engine} · Serving mode: {request.configuration.mode}</p>}
        {tool === 'blis' && reason?.code === 'runtime_not_configured' && <p role="status" className="mt-2 break-words text-xs text-theme-text">BLIS installation and activation required. Ask your administrator to install and register the managed BLIS runtime, then restart the backend to activate it and check support again. Selecting vLLM does not install BLIS.</p>}
        {result && <>
            <p className="mt-2 break-all text-xs text-theme-muted">{result.strategy} · {result.runtime_profile || 'default runtime'} · {result.evidence_maturity}</p>
            {result.metrics?.length > 0 && <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs">
                <thead><tr>{['Metric', 'Value', 'Definition'].map(label => <th key={label} className="border-b border-theme-border p-2 font-medium text-theme-muted">{label}</th>)}</tr></thead>
                <tbody>{result.metrics.map((metric, index) => <tr key={`${metric.name}-${index}`}>
                    <td className="p-2 align-top text-theme-text">{metric.name}<span className="block text-theme-muted">{metric.statistic}</span></td>
                    <td className="whitespace-nowrap p-2 align-top font-mono text-theme-text">{metric.value} {metric.unit}</td>
                    <td className="min-w-48 break-words p-2 text-theme-muted">{metric.formula}<br />Population: {metric.population}<br />Window: {metric.window}</td>
                </tr>)}</tbody>
            </table></div>}
            {!result.metrics?.length && <p className="mt-2 text-xs text-theme-muted">No metrics returned.</p>}
            <details className="mt-3 text-xs text-theme-muted"><summary className="cursor-pointer">Provenance and request accounting</summary>
                <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({
                    configuration_fingerprint: result.configuration_fingerprint, resolved_input_digest: result.resolved_input_digest,
                    request_accounting: result.request_accounting, revisions: result.revisions,
                    coefficient_provenance: result.coefficient_provenance, artifact_ids: result.artifact_ids,
                }, null, 2)}</pre>
            </details>
        </>}
        {[...(support?.warnings || []), ...(result?.warnings || [])].map((warning, index) => <p key={index} className="mt-2 break-words text-xs text-theme-muted">{warning}</p>)}
    </article>;
}

export default function EstimateTools({ context, initialSelection, initialConfiguration, clusterHardware, hardwareProfiles = [], onChange, disabled = false }) {
    const [selection, setSelection] = useState(initialSelection || { tools: ['aisimulate'], runtime_profiles: {} });
    const hasBlis = selection.tools.includes('blis');
    const fieldId = useId();
    const editedEngineVersion = useRef(false);
    const editedEngineSettings = useRef(false);
    const editedHardware = useRef(new Set(Object.entries(initialConfiguration?.hardware || context.hardware || {})
        .filter(([, value]) => value !== '' && value != null).map(([key]) => key)));
    const [effective, setEffective] = useState(() => {
        const initial = initialConfiguration || context;
        return { ...initial, hardware: prefillEstimateHardware(initial.hardware || {}, clusterEstimateHardware(clusterHardware, hardwareProfiles), editedHardware.current) };
    });
    const [cardMode, setCardMode] = useState(initialConfiguration?.hardware?.nodes || context.hardware?.nodes ? 'custom' : 'all');
    const [customCards, setCustomCards] = useState(() => {
        const hardware = initialConfiguration?.hardware || context.hardware;
        return hardware?.nodes && hardware?.devices_per_node ? hardware.nodes * hardware.devices_per_node : '';
    });
    const capacities = estimateCardCapacities(clusterHardware, effective.hardware, hardwareProfiles);
    const totalCards = counts => counts ? counts.reduce((total, count) => total + count, 0) : null;
    const cardCount = cardMode === 'custom' ? customCards : totalCards(capacities[cardMode]);
    let cardError = '';
    let requestHardware = effective.hardware;
    try {
        const fallback = effective.hardware.nodes && effective.hardware.devices_per_node
            ? Array.from({ length: effective.hardware.nodes }, () => effective.hardware.devices_per_node) : null;
        const layout = estimateCardTopology(Number(cardCount), cardMode === 'custom' ? capacities.all || fallback : capacities[cardMode], cardMode === 'custom' ? effective.hardware : null);
        requestHardware = { ...effective.hardware, ...layout };
    } catch (failure) { cardError = failure.message; }
    const requestContext = { ...effective, hardware: requestHardware };
    const [drafts, setDrafts] = useState(() => {
        const settings = initialConfiguration?.configuration?.engine_settings || context.configuration.engine_settings || {};
        return { native: initialEstimateDraft(false, settings), blis: initialEstimateDraft(true, settings) };
    });
    const draftMode = hasBlis ? 'blis' : 'native';
    const draft = drafts[draftMode];
    const setDraft = update => setDrafts(current => {
        const next = update(current[draftMode]);
        const shared = { engineSettings: next.engineSettings, inputTokens: next.inputTokens, outputTokens: next.outputTokens };
        return { native: { ...current.native, ...shared }, blis: { ...current.blis, ...shared }, [draftMode]: next };
    });
    const [discovery, setDiscovery] = useState([]);
    const [discoveryError, setDiscoveryError] = useState('');
    const [states, setStates] = useState({});
    const [support, setSupport] = useState([]);
    const [results, setResults] = useState([]);
    const [lastRequest, setLastRequest] = useState(null);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const active = useRef(null);
    const discoveryActive = useRef(null);
    const contextKey = JSON.stringify(context);
    const previousContext = useRef(contextKey);
    const hardwareKey = JSON.stringify(clusterEstimateHardware(clusterHardware, hardwareProfiles));
    useEffect(() => {
        const discovered = JSON.parse(hardwareKey);
        setEffective(current => ({ ...current, hardware: prefillEstimateHardware(current.hardware, discovered, editedHardware.current) }));
    }, [hardwareKey]);
    useEffect(() => {
        if (!hasBlis) return;
        setEffective(current => {
            const configuration = prefillEstimateEngine(current.configuration, discovery, selection.runtime_profiles?.blis, editedEngineVersion.current);
            return configuration === current.configuration ? current : { ...current, configuration };
        });
        setDrafts(current => {
            let settings;
            try { settings = JSON.parse(current.blis.engineSettings); } catch { return current; }
            const prefilled = prefillEstimateSettings(settings, effective.configuration, discovery, selection.runtime_profiles?.blis, editedEngineSettings.current, current.blis);
            return prefilled === settings ? current : { ...current, blis: { ...current.blis, engineSettings: JSON.stringify(prefilled, null, 2) } };
        });
    }, [discovery, hasBlis, effective.configuration.engine, effective.configuration.engine_version, selection.runtime_profiles?.blis, contextKey, drafts.blis.engineSettings]);
    useEffect(() => {
        if (previousContext.current === contextKey) return;
        previousContext.current = contextKey;
        setEffective(current => ({ ...current, model: { ...current.model, ...context.model }, configuration: {
            ...current.configuration, ...context.configuration,
            roles: context.configuration.roles.map(role => ({ ...current.configuration.roles.find(item => item.role === role.role), ...role })),
        } }));
        const engineSettings = JSON.stringify(context.configuration.engine_settings || {}, null, 2);
        setDrafts(current => ({
            native: { ...current.native, engineSettings },
            blis: { ...current.blis, engineSettings },
        }));
    }, [contextKey]);
    const requestKey = JSON.stringify({ requestContext, selection, draft, cardMode, cardCount, cardError });
    useEffect(() => {
        active.current?.abort();
        active.current = null;
        setBusy(false);
        setStates({});
        setSupport([]);
        setResults([]);
        setLastRequest(null);
        setError('');
        let configuration = null;
        let configurationError = '';
        try {
            if (cardError) throw new Error(cardError);
            const request = buildEstimateRequest(requestContext, selection, draft);
            configuration = { model: request.model, hardware: request.hardware, configuration: request.configuration };
        } catch (failure) { configurationError = failure.message; }
        onChange?.({ selection, configuration, error: configurationError });
        return () => { active.current?.abort(); active.current = null; };
    }, [requestKey, onChange]);
    const refresh = async () => {
        if (discoveryActive.current) return;
        const controller = new AbortController();
        discoveryActive.current = controller;
        try {
            const items = await loadEstimateTools({ signal: controller.signal });
            if (!controller.signal.aborted) { setDiscovery(items); setDiscoveryError(''); }
        } catch (failure) {
            if (!controller.signal.aborted) setDiscoveryError(failure.message);
        } finally {
            if (discoveryActive.current === controller) discoveryActive.current = null;
        }
    };
    usePolling(refresh, { immediate: true, intervalMs: 15000 });
    useEffect(() => () => { discoveryActive.current?.abort(); }, []);
    const run = async (probeOnly = false) => {
        if (active.current) return;
        let requests;
        try {
            if (cardError) throw new Error(cardError);
            requests = buildEstimateRequests(requestContext, selection, draft);
        }
        catch (failure) { setError(failure.message); return; }
        setLastRequest(requests);
        const controller = new AbortController();
        active.current = controller;
        const current = () => active.current === controller && !controller.signal.aborted;
        setBusy(true); setError(''); setResults([]); setSupport([]);
        setStates(Object.fromEntries(selection.tools.map(tool => [tool, 'checking_support'])));
        try {
            await Promise.all(Object.entries(requests).map(async ([tool, request]) => {
                try {
                    const checked = await checkEstimateSupport(request, { signal: controller.signal });
                    if (!current()) return;
                    setSupport(items => [...items.filter(item => item.tool !== tool), ...checked]);
                    const status = checked.find(item => item.tool === tool)?.status || 'unknown';
                    setStates(items => ({ ...items, [tool]: status === 'supported' && !probeOnly ? 'running' : status }));
                    if (!probeOnly && status === 'supported') {
                        const batch = await estimateConfiguration(request, { signal: controller.signal });
                        if (!current()) return;
                        setResults(items => [...items.filter(item => item.tool !== tool), ...batch.evaluations]);
                        setStates(items => ({ ...items, [tool]: batch.evaluations.find(item => item.tool === tool)?.status || 'failed' }));
                    }
                } catch (failure) {
                    if (!current()) return;
                    setError(message => [message, `${tool}: ${failure.message}`].filter(Boolean).join(' '));
                    setStates(items => ({ ...items, [tool]: 'failed' }));
                }
            }));
        } finally {
            if (active.current === controller) { active.current = null; setBusy(false); }
        }
    };
    const cancel = () => {
        active.current?.abort();
        active.current = null;
        setBusy(false);
        setStates(current => Object.fromEntries(Object.entries(current).map(([tool, state]) => [tool, ['running', 'checking_support'].includes(state) ? 'cancelled' : state])));
    };
    const field = (label, key, { help, required, ...props } = {}) => <div key={key} className="min-w-0"><Label htmlFor={`${fieldId}-${key}`} help={help}>{required ? `${label} (required)` : label}</Label><Input id={`${fieldId}-${key}`} aria-label={label} required={required} value={draft[key]} onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} disabled={disabled || busy} {...props} /></div>;
    const changeAccelerator = (profile_id, model) => {
        const hardware = { ...requestHardware, profile_id, model };
        const next = estimateCardSelection({ mode: cardMode, count: cardCount }, estimateCardCapacities(clusterHardware, hardware, hardwareProfiles));
        editedHardware.current.add('profile_id'); editedHardware.current.add('model');
        setCardMode(next.mode);
        if (next.mode === 'custom') setCustomCards(next.count ?? '');
        setEffective(current => ({ ...current, hardware }));
    };
    const hardwareField = (label, key, { help, ...props } = {}) => <div key={key} className="min-w-0"><Label htmlFor={`${fieldId}-${key}`} help={help}>{label}</Label><Input id={`${fieldId}-${key}`} aria-label={label} value={effective.hardware[key] ?? ''} disabled={disabled || busy} onChange={event => {
        if (key === 'model') { changeAccelerator(effective.hardware.profile_id, event.target.value); return; }
        editedHardware.current.add(key);
        setEffective(current => ({ ...current, hardware: { ...current.hardware, [key]: props.type === 'number' ? Number(event.target.value) : event.target.value } }));
    }} {...props} /></div>;
    const dual = hasBlis && selection.tools.includes('aisimulate');
    const hardwareOptions = estimateHardwareOptions(hardwareProfiles, clusterHardware, effective.hardware);
    if (effective.hardware.model && !hardwareOptions.some(item => item.profile_id === effective.hardware.profile_id && item.model === effective.hardware.model)) hardwareOptions.push({ ...effective.hardware, label: effective.hardware.model });
    const hardwareValue = JSON.stringify([effective.hardware.profile_id, effective.hardware.model]);
    let nativePreview;
    if (!hasBlis) {
        try {
            const request = buildEstimateRequests(requestContext, selection, draft).aisimulate;
            nativePreview = { model: request.model, configuration: request.configuration };
        } catch { nativePreview = null; }
    }
    return <section className="min-w-0 border-t border-theme-border py-4" aria-label="Exact estimates">
        <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="text-sm font-semibold text-theme-text">Exact estimates</h3>
            <div className="flex flex-wrap gap-4">{TOOLS.map(tool => <Checkbox key={tool.id} label={tool.label} checked={selection.tools.includes(tool.id)} disabled={disabled || busy} onChange={event => setSelection(current => selectedEstimateTools(current, tool.id, event.target.checked))} />)}</div>
        </div>
        {discoveryError && <p role="alert" className="mt-3 break-words text-xs text-theme-muted">Tool discovery: {discoveryError}</p>}
        <div className="my-3 flex flex-wrap gap-3 text-xs text-theme-muted">{discovery.filter(item => selection.tools.includes(item.tool)).map(item => <span key={`${item.tool}-${item.runtime_profile}`}>{TOOLS.find(tool => tool.id === item.tool)?.label}: {item.available ? 'runtime available' : 'runtime unavailable'}{item.reason ? ` · ${item.reason.code}` : ''}</span>)}</div>
        {dual && <h4 className="my-3 text-xs font-semibold text-theme-text">Common inputs</h4>}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="min-w-0"><Label>Accelerator<Select aria-label="Accelerator" value={hardwareValue} disabled={disabled || busy} onChange={event => {
                const [profile_id, model] = JSON.parse(event.target.value);
                changeAccelerator(profile_id, model);
            }}><option value={JSON.stringify(['', ''])}>Select accelerator</option>{hardwareOptions.map(item => <option key={JSON.stringify([item.profile_id, item.model])} value={JSON.stringify([item.profile_id, item.model])}>{item.label}</option>)}</Select></Label></div>
            {hasBlis && hardwareField('Device model', 'model')}
            <div className="min-w-0"><Label htmlFor={`${fieldId}-cardMode`}>Cards</Label>
                <Select id={`${fieldId}-cardMode`} aria-label="Cards" value={cardMode} disabled={disabled || busy} onChange={event => setCardMode(event.target.value)}>
                    <option value="all" disabled={!capacities.all}>All cluster cards{capacities.all ? ` (${totalCards(capacities.all)})` : ''}</option>
                    <option value="available" disabled={!capacities.available}>Available cards{capacities.available ? ` (${totalCards(capacities.available)})` : ''}</option>
                    <option value="custom">Custom card count</option>
                </Select>
            </div>
            <div className="min-w-0"><Label htmlFor={`${fieldId}-cardCount`}>Card count</Label><Input id={`${fieldId}-cardCount`} aria-label="Card count" type="number" min="1" step="1" value={cardCount ?? ''} readOnly={cardMode !== 'custom'} disabled={disabled || busy} onChange={event => setCustomCards(event.target.value === '' ? '' : Number(event.target.value))} /></div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
            {field(hasBlis ? 'Input tokens' : 'Mean input tokens', 'inputTokens', { type: 'number', min: 1, step: 1 })}{field(hasBlis ? 'Output tokens' : 'Mean output tokens', 'outputTokens', { type: 'number', min: 1, step: 1 })}
        </div>
        {clusterHardware && <p className="my-2 text-xs text-theme-muted">Cluster hardware is a starting declaration, not a reservation or verified simulator match.</p>}
        {cardError && <p role="status" className="my-2 text-xs text-theme-muted">{cardError}</p>}
        {hasBlis && <fieldset className="my-4 min-w-0 border-t border-theme-border pt-3"><legend className="text-xs font-semibold text-theme-text">BLIS workload and run controls</legend>
        <p className="mb-3 text-xs text-theme-muted">Required fields are prefilled with an editable sample workload, not measured traffic.</p>
        {draft.distribution === 'native_mean' && <p role="status" className="mb-3 text-xs text-theme-muted">BLIS requires an explicit workload distribution and load target. Native mean lengths do not specify a BLIS workload.</p>}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div><Label htmlFor={`${fieldId}-distribution`} help="Required for BLIS. Constant uses the declared token lengths for every request. Native mean is AISimulate-only.">Distribution (required)</Label><Select id={`${fieldId}-distribution`} aria-label="Distribution" value={draft.distribution} disabled={disabled || busy} onChange={event => setDraft(current => ({ ...current, distribution: event.target.value }))}><option value="native_mean">Native mean</option><option value="constant">Constant</option></Select></div>
            <div><Label htmlFor={`${fieldId}-loadMode`} help="Required for BLIS. Choose either a request arrival rate or concurrent clients, never both. Default: 1 request per second.">Load target (required)</Label><Select id={`${fieldId}-loadMode`} aria-label="Load target" value={draft.loadMode} disabled={disabled || busy} onChange={event => setDraft(current => ({ ...current, loadMode: event.target.value }))}><option value="none">Unspecified</option><option value="rate">Requests / second</option><option value="concurrency">Concurrency</option></Select></div>
            {draft.loadMode !== 'none' && field(draft.loadMode === 'rate' ? 'Requests / second' : 'Concurrency', 'load', { type: 'number', min: draft.loadMode === 'rate' ? 0.000001 : 1, step: draft.loadMode === 'rate' ? 'any' : 1, required: true, help: draft.loadMode === 'rate' ? 'Required. Requests arriving per simulated second; default 1.' : 'Required. Positive whole number of concurrent clients. Shared prefixes are not supported with concurrency.' })}
            {field('Requests', 'numRequests', { type: 'number', min: 1, step: 1, required: true, help: 'Required. Maximum number of generated requests; default 100. The run can end earlier at the simulation horizon.' })}
            {field('Simulation duration (s)', 'horizon', { type: 'number', min: 0.000001, step: 'any', required: true, help: 'Required. Simulated time limit, not real elapsed time; default 300 seconds. Increase it if requests remain unfinished. Microsecond precision.' })}
        </div>
            <details className="mt-3 text-xs text-theme-text"><summary className="cursor-pointer">Advanced workload controls (optional)</summary>
                <div className="my-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    {hardwareField('Fabric', 'fabric', { help: 'Hardware declaration. Leave blank unless the selected runtime profile requires a specific interconnect. No fabric is inferred.' })}
                    {field('Wall timeout (s)', 'timeout', { type: 'number', min: 0.001, max: 120, step: 'any', placeholder: 'Server default: 120', help: 'Optional. Real elapsed-time limit for execution, capped at 120 seconds. Blank uses the server limit; it does not change simulated time.' })}
                    {field('Request deadline (s)', 'deadline', { type: 'number', min: 0.000001, step: 'any', placeholder: 'No deadline', help: 'Optional. Per-request simulated timeout. Blank disables request deadlines. Microsecond precision.' })}
                    {field('Prefix tokens', 'prefixTokens', { type: 'number', min: 0, step: 1, help: 'Optional. Shared prompt prefix length; default 0 means no shared prefix. Nonzero prefixes are not supported with concurrency.' })}
                </div>
                <Checkbox label="Input tokens include prefix" checked={draft.includesPrefix} disabled={disabled || busy} onChange={event => setDraft(current => ({ ...current, includesPrefix: event.target.checked }))} />
            </details>
        </fieldset>}
        {hasBlis && <details className="my-4 text-xs text-theme-text"><summary className="cursor-pointer">BLIS runtime declarations</summary>
            <div className="my-3 grid gap-3 sm:grid-cols-2">{effective.configuration.roles.map((role, index) => <fieldset key={role.role} className="min-w-0 border-l border-theme-border pl-3">
                <legend className="text-xs text-theme-muted">{role.role} · TP {role.tp} · replicas {role.replicas}</legend>
                <div className="grid grid-cols-2 gap-3">{['pp', 'dp'].map(key => <Label key={key}>{key.toUpperCase()}<Input aria-label={`${role.role} ${key.toUpperCase()}`} type="number" min="1" step="1" value={role[key] ?? 1} disabled={disabled || busy} onChange={event => setEffective(current => ({ ...current, configuration: { ...current.configuration, roles: current.configuration.roles.map((item, roleIndex) => roleIndex === index ? { ...item, [key]: Number(event.target.value) } : item) } }))} /></Label>)}</div>
                <Checkbox label={`${role.role} expert parallel`} checked={Boolean(role.expert_parallel)} disabled={disabled || busy} onChange={event => setEffective(current => ({ ...current, configuration: { ...current.configuration, roles: current.configuration.roles.map((item, roleIndex) => roleIndex === index ? { ...item, expert_parallel: event.target.checked } : item) } }))} />
            </fieldset>)}</div>
            <div className="my-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {['precision', 'quantization'].map(key => <div key={key}><Label>{key}<Input aria-label={key} value={effective.model[key] || ''} disabled={disabled || busy} onChange={event => setEffective(current => ({ ...current, model: { ...current.model, [key]: event.target.value || null } }))} /></Label></div>)}
                <div><Label>Engine version<Input aria-label="Engine version" value={effective.configuration.engine_version || ''} disabled={disabled || busy} onChange={event => { editedEngineVersion.current = true; setEffective(current => ({ ...current, configuration: { ...current.configuration, engine_version: event.target.value || null } })); }} /></Label></div>
            </div>
            <Label className="mt-3">Engine settings (JSON)<Textarea aria-label="Engine settings (JSON)" rows={5} value={draft.engineSettings} disabled={disabled || busy} onChange={event => { editedEngineSettings.current = true; setDraft(current => ({ ...current, engineSettings: event.target.value })); }} /></Label>
        </details>}
        {!hasBlis && <details className="my-4 text-xs text-theme-text"><summary className="cursor-pointer">Effective model and topology</summary><pre className="my-3 overflow-auto whitespace-pre-wrap break-all text-theme-muted">{JSON.stringify(nativePreview, null, 2)}</pre></details>}
        <div className="flex flex-wrap gap-2">
            <Button type="button" variant="secondary" disabled={disabled || busy} onClick={() => run(true)}><Check size={14} aria-hidden="true" />Check support</Button>
            <Button type="button" disabled={disabled || busy} onClick={() => run()}><Play size={14} aria-hidden="true" />Estimate</Button>
            <Button type="button" variant="secondary" disabled={!busy} onClick={cancel}><Square size={14} aria-hidden="true" />Cancel</Button>
        </div>
        {error && <p role="alert" className="mt-3 break-words text-xs text-theme-text">{error}</p>}
        <div className="mt-3" aria-live="polite" aria-busy={busy}>{selection.tools.map(tool => <EstimateOutcome key={tool} tool={tool} state={states[tool]} support={support.find(item => item.tool === tool)} result={results.find(item => item.tool === tool)} request={lastRequest?.[tool]} />)}</div>
    </section>;
}