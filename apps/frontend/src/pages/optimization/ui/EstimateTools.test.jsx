import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EstimateTools, { buildEstimateRequest, buildEstimateRequests, clusterEstimateHardware, editorEstimateContext, estimateCardCapacities, estimateCardSelection, estimateCardTopology, estimateHardwareOptions, estimateSemanticProblems, EstimateOutcome, prefillEstimateEngine, prefillEstimateHardware, prefillEstimateSettings, selectedEstimateTools } from './EstimateTools.jsx';
import { checkEstimateSupport, estimateConfiguration, loadEstimateTools } from './OptimizationWorkspace/configurationBackend.js';
import { loadClusterOverview } from './OptimizationWorkspace/clusterBackend.js';

const context = { model: { name: 'test/model' }, hardware: { profile_id: 'test', model: 'device', nodes: 2, devices_per_node: 4 }, configuration: { mode: 'agg', engine: 'vllm', roles: [{ role: 'colocated', tp: 4, pp: 2, replicas: 1 }] } };
const draft = { engineSettings: '{}', distribution: 'constant', inputTokens: '128', outputTokens: '32', prefixTokens: 0, includesPrefix: true, loadMode: 'rate', load: '2', seed: '42', numRequests: '8', horizon: '60', deadline: '30', timeout: '120' };

test('selection preserves independent tools and removes deselected profiles', () => {
    const selection = { tools: ['aisimulate', 'blis'], runtime_profiles: { blis: 'locked' } };
    assert.deepEqual(selectedEstimateTools(selection, 'aisimulate', false), { tools: ['blis'], runtime_profiles: { blis: 'locked' } });
    assert.deepEqual(selectedEstimateTools(selection, 'blis', false), { tools: ['aisimulate'], runtime_profiles: {} });
    const last = { tools: ['blis'] };
    assert.equal(selectedEstimateTools(last, 'blis', false), last);
});

test('request retains explicit workload, topology, run and selected tools', () => {
    const request = buildEstimateRequest(context, { tools: ['blis'], runtime_profiles: {} }, draft);
    assert.deepEqual(request.tools, ['blis']);
    assert.equal(request.configuration.roles[0].pp, 2);
    assert.equal(request.workload.distribution, 'constant');
    assert.equal(request.workload.request_rate, 2);
    assert.equal(request.workload.concurrency, null);
    assert.equal(request.run.seed, 42);
    assert.throws(() => buildEstimateRequest(context, { tools: ['blis'] }, { ...draft, engineSettings: '[]' }), /JSON object/);
});

test('native defaults clear unsupported estimate inputs without mutating deployment declarations', () => {
    const deployment = { ...context, model: { name: 'test/model', precision: 'fp16', quantization: 'nvfp4' }, configuration: {
        ...context.configuration, engine_version: '1.0', engine_settings: { max_model_len: 4096 }, router_settings: { policy: 'custom' },
        roles: [{ role: 'colocated', tp: 4, pp: 2, dp: 2, replicas: 1, expert_parallel: true }],
    } };
    const original = JSON.stringify(deployment);
    const request = buildEstimateRequest(deployment, { tools: ['aisimulate'] }, { ...draft, engineSettings: '{"max_model_len":4096}' }, 'native_defaults');
    assert.deepEqual(request.model, { name: 'test/model', precision: null, quantization: null });
    assert.equal(request.configuration.engine_version, null);
    assert.deepEqual(request.configuration.engine_settings, {});
    assert.deepEqual(request.configuration.router_settings, {});
    assert.deepEqual(request.configuration.roles, [{ role: 'colocated', tp: 4, pp: 1, dp: 1, replicas: 1, expert_parallel: false }]);
    assert.equal(request.workload.input_tokens, 128);
    assert.equal(request.workload.distribution, 'native_mean');
    assert.equal(request.workload.request_rate, null);
    assert.equal(request.workload.prefix_tokens, 0);
    assert.ok(Object.values(request.run).every(value => value === null));
    assert.deepEqual(estimateSemanticProblems(request, { tools: ['aisimulate'] }, { ...draft, engineSettings: '{}', distribution: 'native_mean', prefixTokens: 0, loadMode: 'none', seed: '', numRequests: '', horizon: '', deadline: '', timeout: '' }), []);
    assert.equal(JSON.stringify(deployment), original);
    assert.deepEqual(buildEstimateRequests(deployment, { tools: ['aisimulate'] }, { ...draft, engineSettings: 'not JSON', timeout: 'invalid' }).aisimulate.configuration.engine_settings, {});
    const strict = buildEstimateRequest(deployment, { tools: ['aisimulate'] }, draft);
    assert.equal(strict.configuration.engine_version, '1.0');
    assert.equal(strict.configuration.roles[0].pp, 2);
    assert.throws(() => buildEstimateRequest(deployment, { tools: ['aisimulate', 'blis'] }, draft, 'native_defaults'), /AISimulate-only/);
    const html = renderToStaticMarkup(createElement(EstimateTools, { context: deployment }));
    assert.doesNotMatch(html, /AISimulate options|AISimulate estimation mode|Native defaults: .* excluded/);
    assert.doesNotMatch(html, /Runtime capabilities and confirmed configurations|Native-default estimate, not verification|No additional deployment overrides/);
    assert.doesNotMatch(html, /AISimulate semantic incompatibility/);
});

test('editor mapping retains explicit overrides instead of silently dropping unsupported semantics', () => {
    const effective = editorEstimateContext({ model: 'test', engine: 'vllm', roles: [{ role: 'colocated', tp: 8, replicas: 1 }], overrides: [
        { kind: 'argument', target: 'both', name: 'pipeline-parallel-size', value: '2' },
        { kind: 'argument', target: 'both', name: 'quantization', value: 'nvfp4' },
        { kind: 'argument', target: 'both', name: 'max-num-seqs', value: '64' },
        { kind: 'environment', target: 'both', name: 'CUSTOM', value: '1' },
    ] });
    assert.equal(effective.configuration.roles[0].pp, 2);
    assert.equal(effective.model.quantization, 'nvfp4');
    assert.equal(effective.configuration.engine_settings.max_num_seqs, 64);
    assert.equal(effective.configuration.engine_settings.explicit_overrides[0].name, 'CUSTOM');
    assert.throws(() => buildEstimateRequest(context, { tools: ['blis'] }, { ...draft, timeout: 'invalid' }), /finite/);
});

test('panel restores selection and has independent result slots and no synthetic progress', () => {
    const html = renderToStaticMarkup(createElement(EstimateTools, { context, initialConfiguration: context, initialSelection: { tools: ['blis'] } }));
    assert.match(html, /aria-label="blis estimate result"/);
    assert.doesNotMatch(html, /aria-label="aisimulate estimate result"/);
    assert.match(html, /Check support/);
    assert.doesNotMatch(html, /Save declared simulation inputs/);
    assert.doesNotMatch(html, /[0-9]+%|winner|ranking/i);
});

test('BLIS defaults include required controls while deadlines and execution timeout remain optional', () => {
    const html = renderToStaticMarkup(createElement(EstimateTools, { context, initialSelection: { tools: ['blis'] } }));
    assert.match(html, /<option value="constant" selected="">Constant<\/option>/);
    assert.match(html, /<option value="rate" selected="">Requests \/ second<\/option>/);
    for (const [label, value] of [['Requests', '100'], ['Simulation duration \\(s\\)', '300'], ['Requests / second', '1']]) {
        assert.match(html, new RegExp(`aria-label="${label}"[^>]*value="${value}"`));
    }
    for (const label of ['Request deadline \\(s\\)', 'Wall timeout \\(s\\)']) {
        assert.match(html, new RegExp(`aria-label="${label}"[^>]*value=""`));
    }
    assert.match(html, /Advanced workload controls \(optional\)/);
    assert.match(html, /role="tooltip"/);
    assert.match(html, /Blank disables request deadlines/);
    assert.match(html, /Simulated time limit, not real elapsed time/);
    assert.match(html, /Server default: 120/);
    assert.doesNotMatch(html, /aria-label="Seed"|Horizon \(s\)/);
    assert.doesNotMatch(html.split('BLIS runtime declarations')[1], /<pre/);
});

test('AISimulate-only exposes native inputs without BLIS controls or internal hardware IDs', () => {
    const html = renderToStaticMarkup(createElement(EstimateTools, { context }));
    for (const label of ['Distribution', 'Wall timeout (s)', 'Load target', 'Seed', 'Requests', 'Prefix tokens', 'Hardware profile ID', 'Engine settings (JSON)']) {
        assert.ok(!html.includes(`aria-label="${label}"`), label);
    }
    assert.match(html, /aria-label="Mean input tokens"/);
    assert.match(html, /aria-label="Mean output tokens"/);
});

const profiles = [{ id: 'vendor', display_name: 'Readable accelerator', accelerator_keys: ['device-key'], planning: { aic_system_models: [{ pattern: '\\bchip\\b', system: 'chip_pcie' }] } }];
const clusterHardware = { accelerators: [{ id: 'vendor', models: ['Chip PCIe'], gpuCount: 8 }], gpuNodes: 1 };

test('cluster prefilling maps hardware identity and observed node capacity without guessing heterogeneity', () => {
    assert.deepEqual(clusterEstimateHardware(clusterHardware, profiles), { profile_id: 'vendor', model: 'Chip PCIe', nodes: 1, devices_per_node: 8 });
    const multiNode = { ...clusterHardware, gpuNodes: 2, nodeSummaries: [{ gpuByProfile: { vendor: 4 } }, { gpuByProfile: { vendor: 4 } }] };
    assert.equal(clusterEstimateHardware(multiNode, profiles).devices_per_node, 4);
    assert.equal(clusterEstimateHardware(multiNode, profiles).nodes, 2);
    multiNode.nodeSummaries[1].gpuByProfile.vendor = 8;
    assert.equal(clusterEstimateHardware(multiNode, profiles).devices_per_node, undefined);
    assert.equal(clusterEstimateHardware({ ...clusterHardware, accelerators: [...clusterHardware.accelerators, { id: 'another' }] }, profiles).profile_id, undefined);
    assert.equal(clusterEstimateHardware({ ...clusterHardware, accelerators: [{ id: 'vendor', models: ['Chip', 'Another chip'] }] }, profiles).model, undefined);
    assert.deepEqual(clusterEstimateHardware(clusterHardware, []), {});
});

test('prefilling preserves manual and saved declarations across refresh and clears stale automatic identity', () => {
    const current = { profile_id: 'custom', model: 'My chip', nodes: 3, devices_per_node: 2, fabric: 'manual' };
    const discovered = clusterEstimateHardware(clusterHardware, profiles);
    assert.deepEqual(prefillEstimateHardware(current, discovered, new Set(Object.keys(current))), current);
    const updated = prefillEstimateHardware(current, discovered, new Set(['model', 'nodes', 'fabric']));
    assert.equal(updated.model, 'My chip');
    assert.equal(updated.nodes, 3);
    assert.equal(updated.devices_per_node, 8);
    assert.equal(prefillEstimateHardware(updated, {}, new Set(['model'])).profile_id, '');
});

test('engine version prefills from a unique managed profile without overwriting declarations', () => {
    const configuration = { ...context.configuration, engine_version: null };
    const profile = { tool: 'blis', available: true, runtime_profile: 'managed', approved_inputs: [{ configuration: { engine: 'vllm', engine_version: '0.32.0' } }] };
    assert.equal(prefillEstimateEngine(configuration, [profile]).engine_version, '0.32.0');
    assert.equal(prefillEstimateEngine(configuration, [profile], 'managed').engine_version, '0.32.0');
    assert.equal(prefillEstimateEngine(configuration, [], 'managed'), configuration);
    assert.equal(prefillEstimateEngine(configuration, [profile], 'other'), configuration);
    assert.equal(prefillEstimateEngine(configuration, [profile, { ...profile, runtime_profile: 'other' }]), configuration);
    assert.equal(prefillEstimateEngine(configuration, [{ ...profile, available: false }]), configuration);
    assert.equal(prefillEstimateEngine({ ...configuration, engine: 'other' }, [profile]).engine_version, null);
    assert.equal(prefillEstimateEngine(configuration, [profile], 'managed', true), configuration);
    const saved = { ...configuration, engine_version: '0.31.0' };
    assert.equal(prefillEstimateEngine(saved, [profile]), saved);
    const request = buildEstimateRequest({ ...context, configuration: prefillEstimateEngine(configuration, [profile]) }, { tools: ['blis'] }, draft);
    assert.equal(request.configuration.engine_version, '0.32.0');
    assert.equal(configuration.engine_version, null);
});

test('cluster prefilling distinguishes a device model from architecture labels', () => {
    const hardware = { ...clusterHardware, accelerators: [{ id: 'vendor', models: ['Chip PCIe', 'architecture'], gpuCount: 8 }] };
    assert.equal(clusterEstimateHardware(hardware, profiles).model, 'Chip PCIe');
    const html = renderToStaticMarkup(createElement(EstimateTools, {
        context: { ...context, hardware: { profile_id: '', model: '', nodes: '', devices_per_node: '' } },
        clusterHardware: hardware, hardwareProfiles: profiles,
    }));
    assert.match(html, /<option[^>]*selected=""[^>]*>Readable accelerator \/ Chip PCIe<\/option>/);
    assert.match(html, /aria-label="Card count"[^>]*value="8"/);
    assert.doesNotMatch(html, /aria-label="Nodes"|aria-label="Devices per node"/);
});

test('BLIS prefills only required confirmed engine settings and preserves declarations', () => {
    const required = { block_size: 16, max_num_seqs: 64, max_num_batched_tokens: 8192, gpu_memory_utilization: 0.9 };
    const configuration = { ...context.configuration, engine_version: '0.32.0' };
    const profile = { tool: 'blis', available: true, runtime_profile: 'managed', approved_inputs: [{ configuration: {
        ...configuration, engine_settings: { ...required, moe_backend: 'marlin', max_model_len: 262144 },
    } }] };
    const settings = { gpu_memory_utilization: 0.8 };
    const prefilled = prefillEstimateSettings(settings, configuration, [profile], 'managed');
    assert.deepEqual(prefilled, { ...required, gpu_memory_utilization: 0.8 });
    assert.deepEqual(settings, { gpu_memory_utilization: 0.8 });
    assert.equal(prefillEstimateSettings(prefilled, configuration, [profile]), prefilled);
    assert.equal(prefillEstimateSettings(settings, configuration, [profile], 'managed', true), settings);
    assert.equal(prefillEstimateSettings(settings, configuration, [], 'managed'), settings);
    assert.equal(prefillEstimateSettings(settings, configuration, [profile], 'other'), settings);
    assert.equal(prefillEstimateSettings(settings, configuration, [profile, profile]), settings);
    assert.equal(prefillEstimateSettings(settings, configuration, [{ ...profile, available: false }]), settings);
    assert.equal(prefillEstimateSettings(settings, { ...configuration, engine_version: 'other' }, [profile]), settings);
    assert.equal(prefillEstimateSettings(settings, { ...configuration, engine: 'other' }, [profile]), settings);
    assert.equal(prefillEstimateSettings({}, configuration, [profile], 'managed', false, draft).max_model_len, 160);
    assert.equal(prefillEstimateSettings({}, configuration, [profile], 'managed', false, { ...draft, prefixTokens: 32, includesPrefix: false }).max_model_len, 192);
    assert.equal(prefillEstimateSettings({ max_model_len: 4096 }, configuration, [profile], 'managed', false, draft).max_model_len, 4096);
    for (const value of [64, 0, null]) {
        assert.equal(prefillEstimateSettings({ block_size: value }, configuration, [profile]).block_size, value);
    }
    const requests = buildEstimateRequests(context, { tools: ['aisimulate', 'blis'] }, { ...draft, engineSettings: JSON.stringify(prefilled) });
    assert.deepEqual(requests.blis.configuration.engine_settings, prefilled);
    assert.deepEqual(requests.aisimulate.configuration.engine_settings, {});
});

test('hardware options are profile-driven and include observed and custom models', () => {
    const options = estimateHardwareOptions(profiles, clusterHardware, { profile_id: 'vendor', model: 'Custom model' });
    assert.ok(options.some(item => item.model === 'chip pcie' && item.label === 'Readable accelerator / chip pcie'));
    assert.ok(options.some(item => item.model === 'Chip PCIe'));
    assert.ok(options.some(item => item.model === 'Custom model'));
    assert.deepEqual(estimateHardwareOptions([], clusterHardware), []);
});

test('dual selection uses native AISimulate inputs and preserves BLIS declarations', () => {
    const selection = { tools: ['aisimulate', 'blis'] };
    const problems = estimateSemanticProblems(context, selection, draft);
    assert.equal(problems.length, 4);
    const request = buildEstimateRequest(context, selection, draft);
    assert.equal(request.workload.distribution, 'constant');
    assert.equal(request.run.seed, 42);
    const requests = buildEstimateRequests(context, selection, draft);
    assert.deepEqual(requests.aisimulate.tools, ['aisimulate']);
    assert.equal(requests.aisimulate.workload.distribution, 'native_mean');
    assert.equal(requests.aisimulate.run.seed, null);
    assert.equal(requests.aisimulate.configuration.roles[0].pp, 1);
    assert.deepEqual(requests.blis.tools, ['blis']);
    assert.equal(requests.blis.workload.distribution, 'constant');
    assert.equal(requests.blis.run.seed, 42);
    assert.equal(requests.blis.configuration.roles[0].pp, 2);
    assert.throws(() => buildEstimateRequests(context, { tools: [] }, draft), /at least one/);
    const html = renderToStaticMarkup(createElement(EstimateTools, { context, initialSelection: selection }));
    assert.match(html, /Common inputs/);
    assert.match(html, /BLIS workload and run controls/);
    assert.doesNotMatch(html, /AISimulate semantic incompatibility|AISimulate estimation mode|Native defaults:|aria-label="Seed"/);
    assert.match(html, /aria-label="aisimulate estimate result"/);
    assert.match(html, /aria-label="blis estimate result"/);
    assert.deepEqual(estimateSemanticProblems(context, { tools: ['blis'] }, draft), []);
});

test('cards use model-specific capacity and never guess missing availability or uniform layouts', () => {
    const selected = { profile_id: 'vendor', model: 'Chip' };
    const node = (count, available, model = 'Chip') => ({ gpuByProfile: { vendor: count }, availableGpuByProfile: { vendor: available }, gpuModelsByProfile: { vendor: [model] } });
    const hardware = { nodeSummaries: [node(4, 2), node(4, 1), node(8, 8, 'Other')] };
    assert.deepEqual(estimateCardCapacities(hardware, selected), { all: [4, 4], available: [2, 1] });
    assert.deepEqual(estimateCardTopology(8, [4, 4]), { nodes: 2, devices_per_node: 4 });
    assert.deepEqual(estimateCardTopology(6, [4, 4]), { nodes: 2, devices_per_node: 3 });
    assert.deepEqual(estimateCardTopology(8, [8, 8], { nodes: 2, devices_per_node: 4 }), { nodes: 2, devices_per_node: 4 });
    assert.throws(() => estimateCardTopology(5, [4, 4]), /uniform/);
    assert.throws(() => estimateCardTopology(3, [2, 1]), /uniform/);
    assert.throws(() => estimateCardTopology(0, [4, 4]), /positive/);
    assert.throws(() => estimateCardTopology(2.5, [4, 4]), /whole number/);
    assert.throws(() => estimateCardTopology(9, [4, 4]), /cannot fit/);
    assert.equal(estimateCardCapacities({ nodeSummaries: [node(4, null)] }, selected).available, null);
    assert.deepEqual(estimateCardCapacities(hardware, { ...selected, model: 'Unknown' }), { all: null, available: null });
    assert.deepEqual(estimateCardCapacities(clusterHardware, { profile_id: 'other', model: 'Chip PCIe' }, profiles), { all: null, available: null });
});

test('card capacity matches canonical system identifiers to observed hardware without mixing PCIe and SXM', () => {
    const hardwareProfiles = [{ id: 'nvidia', planning: { aic_system_models: [
        { pattern: '\\bh100\\b(?=.*pcie)', system: 'h100_pcie' },
        { pattern: '\\bh100\\b(?!.*pcie)', system: 'h100_sxm' },
    ] } }];
    const hardware = { accelerators: [{ id: 'nvidia', models: ['NVIDIA H100 PCIe'], gpuCount: 8 }], gpuNodes: 1, nodeSummaries: [{
        gpuByProfile: { nvidia: 8 }, availableGpuByProfile: { nvidia: 4 }, gpuModelsByProfile: { nvidia: ['NVIDIA H100 PCIe'] },
    }] };
    for (const model of ['h100_pcie', 'h100 pcie', 'NVIDIA H100 PCIe']) {
        assert.deepEqual(estimateCardCapacities(hardware, { profile_id: 'nvidia', model }, hardwareProfiles), { all: [8], available: [4] });
    }
    assert.deepEqual(estimateCardCapacities(hardware, { profile_id: 'nvidia', model: 'h100_sxm' }, hardwareProfiles), { all: null, available: null });
    assert.deepEqual(estimateCardCapacities(hardware, { profile_id: 'nvidia', model: 'NVIDIA H100 PCIe 80GB' }, hardwareProfiles), { all: null, available: null });
    const canonicalHardware = { ...hardware, nodeSummaries: [{ ...hardware.nodeSummaries[0], gpuModelsByProfile: { nvidia: ['h100_pcie'] } }] };
    assert.deepEqual(estimateCardCapacities(canonicalHardware, { profile_id: 'nvidia', model: 'NVIDIA H100 PCIe' }, hardwareProfiles), { all: [8], available: [4] });
});

test('switching accelerators preserves card counts when cluster capacity is unavailable', () => {
    const unknown = { all: null, available: null };
    for (const mode of ['all', 'available']) {
        assert.deepEqual(estimateCardSelection({ mode, count: 8 }, unknown), { mode: 'custom', count: 8 });
    }
    const custom = { mode: 'custom', count: 3 };
    assert.deepEqual(estimateCardSelection(custom, { all: [8], available: [4] }), custom);
    assert.deepEqual(estimateCardSelection({ mode: 'available', count: 4 }, { all: [8], available: null }), { mode: 'all', count: 8 });
    assert.deepEqual(estimateCardSelection({ mode: 'available', count: 0 }, { all: [8], available: [0] }), { mode: 'available', count: 0 });
    assert.deepEqual(estimateCardSelection({ mode: 'all', count: 8 }, { all: [4], available: [2] }), { mode: 'all', count: 8 });
});

test('all tool selections keep Cancel visible and checked tool controls enabled', () => {
    for (const tools of [['aisimulate'], ['blis'], ['aisimulate', 'blis']]) {
        const html = renderToStaticMarkup(createElement(EstimateTools, { context, initialSelection: { tools } }));
        assert.match(html, /<button[^>]*disabled=""[^>]*>.*?Cancel<\/button>/);
        assert.doesNotMatch(html, /type="checkbox"[^>]*disabled=""/);
        assert.doesNotMatch(html, /Constraints: unknown/);
    }
});

test('support statuses have success and danger tones and known constraints remain visible', () => {
    for (const [status, color] of [['supported', 'emerald'], ['unsupported', 'red']]) {
        const html = renderToStaticMarkup(createElement(EstimateOutcome, { tool: 'aisimulate', support: { status } }));
        assert.match(html, new RegExp(color));
        assert.ok(html.includes(status));
        assert.doesNotMatch(html, /Constraints:/);
    }
    const html = renderToStaticMarkup(createElement(EstimateOutcome, { tool: 'blis', result: { status: 'succeeded', constraint_assessment: 'satisfied' } }));
    assert.match(html, /Constraints: satisfied/);
});

test('native AISimulate request contains no explicit run or BLIS workload controls', () => {
    const nativeContext = { ...context, configuration: { ...context.configuration, roles: [{ role: 'colocated', tp: 4, replicas: 1 }] } };
    const nativeDraft = { ...draft, distribution: 'native_mean', loadMode: 'none', load: '', seed: '', numRequests: '', horizon: '', deadline: '', timeout: '' };
    assert.deepEqual(estimateSemanticProblems(nativeContext, { tools: ['aisimulate'] }, nativeDraft), []);
    const request = buildEstimateRequest(nativeContext, { tools: ['aisimulate'] }, nativeDraft);
    assert.ok(Object.values(request.run).every(value => value === null));
    assert.equal(request.workload.request_rate, null);
    assert.equal(request.workload.concurrency, null);
    assert.equal(request.workload.distribution, 'native_mean');
    assert.throws(() => buildEstimateRequest({ ...nativeContext, hardware: { ...nativeContext.hardware, devices_per_node: '' } }, { tools: ['aisimulate'] }, nativeDraft), /positive whole number/);
});

test('missing BLIS runtime prompts installation, registration and activation for support and estimates', () => {
    const reason = { code: 'runtime_not_configured', message: 'No managed BLIS runtime is configured' };
    for (const outcome of [{ support: { reason } }, { result: { status: 'unavailable', error: reason } }]) {
        const html = renderToStaticMarkup(createElement(EstimateOutcome, { tool: 'blis', ...outcome }));
        assert.match(html, /install and register the managed BLIS runtime/);
        assert.match(html, /restart the backend to activate it and check support again/);
        assert.match(html, /Selecting vLLM does not install BLIS/);
        assert.match(html, /runtime_not_configured/);
    }
    for (const props of [{ tool: 'aisimulate', support: { reason } }, { tool: 'blis', support: { reason: { code: 'hardware_mapping_missing', message: 'Unsupported hardware' } } }]) {
        const html = renderToStaticMarkup(createElement(EstimateOutcome, props));
        assert.doesNotMatch(html, /BLIS installation and activation required/);
    }
});

test('unsupported AISimulate combination identifies the submitted model, hardware, engine and mode', () => {
    const request = buildEstimateRequest(context, { tools: ['aisimulate'] }, draft, 'native_defaults');
    const html = renderToStaticMarkup(createElement(EstimateOutcome, {
        tool: 'aisimulate', request, support: { reason: { code: 'model_backend_unsupported', message: 'Unsupported combination' } },
    }));
    for (const value of ['Model: test/model', 'Accelerator: device', 'Engine: vllm', 'Serving mode: agg']) assert.ok(html.includes(value), value);
    const supported = renderToStaticMarkup(createElement(EstimateOutcome, { tool: 'aisimulate', request, support: { status: 'supported' } }));
    assert.doesNotMatch(supported, /Serving mode:/);
});

test('results keep execution, constraints, full metric definition and provenance separate', () => {
    const html = renderToStaticMarkup(createElement(EstimateOutcome, { tool: 'blis', result: {
        status: 'succeeded', constraint_assessment: 'unknown', strategy: 'native_exact', evidence_maturity: 'experimental',
        metrics: [{ name: 'ttft', value: 12, unit: 'ms', statistic: 'mean', formula: 'first - arrival', population: 'completed', window: 'simulation' }],
        revisions: { runtime: 'locked' }, request_accounting: { completed: 8 }, configuration_fingerprint: 'fingerprint',
    } }));
    for (const value of ['succeeded', '12', 'ms', 'mean', 'first - arrival', 'completed', 'simulation', 'locked', 'fingerprint']) assert.ok(html.includes(value), value);
    assert.doesNotMatch(html, /Constraints: unknown/);
});

test('planning overview skips telemetry and preserves cancellation', async (testContext) => {
    const calls = [];
    testContext.mock.method(globalThis, 'fetch', async (url, options) => {
        calls.push({ url, options });
        return Response.json({ kubernetes: { hardware: {} } });
    });
    const controller = new AbortController();
    await loadClusterOverview('cluster/id', { signal: controller.signal });
    await loadClusterOverview('cluster/id', { includeTelemetry: true });
    assert.equal(calls[0].url, '/api/cluster/overview?clusterId=cluster%2Fid&includeTelemetry=false');
    assert.equal(calls[0].options.signal, controller.signal);
    assert.equal(calls[0].options.credentials, 'include');
    assert.match(calls[1].url, /includeTelemetry=true$/);
});

test('transport preserves abort signal, selected tools and saved artifact identity', async (testContext) => {
    const calls = [];
    testContext.mock.method(globalThis, 'fetch', async (url, options) => {
        calls.push({ url, options });
        return Response.json(url.endsWith('support') || options.method !== 'POST' ? [] : { evaluations: [] });
    });
    const controller = new AbortController();
    await loadEstimateTools({ signal: controller.signal });
    const request = buildEstimateRequest(context, { tools: ['blis'] }, draft);
    await checkEstimateSupport(request, { signal: controller.signal });
    await estimateConfiguration(request, { signal: controller.signal, artifactId: 'saved/id' });
    assert.ok(calls.every(call => call.options.signal === controller.signal && call.options.credentials === 'include'));
    assert.deepEqual(JSON.parse(calls[1].options.body).tools, ['blis']);
    assert.equal(calls[2].url, '/api/v1/estimate-tools/configurations/saved%2Fid/estimate');
    assert.equal(calls.length, 3);
});