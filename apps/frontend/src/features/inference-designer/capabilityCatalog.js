/** Reviewed source snapshot; deployment provenance is resolved separately by the planner. */
export const REFERENCE_SOURCE = { repository: 'llm-d/llm-d', commit: 'a9cf07a77a3016db21ae87134e5745b3a2d45ba6' };
const source = path => `https://github.com/${REFERENCE_SOURCE.repository}/blob/${REFERENCE_SOURCE.commit}/${path}`;
export const referenceGuide = guide => source(`guides/${guide}/README.md`);
const capability = (id, label, stage, inspector, guide, detail, fields = [], available = false) => ({ id, label, stage, inspector, guide, detail, fields, available, source: referenceGuide(guide) });
export const CAPABILITIES = [
    capability('payload', 'Payload / multimodal encoding', 'prepare', 'payload', 'multimodal-serving', 'Parse media and encode image, audio or video inputs before text inference.'),
    capability('encode-split', 'Encode disaggregation', 'prepare', 'encode-split', 'multimodal-serving/e-disaggregation', 'Dedicated Encode model servers feed Prefill or model servers; requires an E/PD or E/P/D adapter.'),
    capability('multi-model', 'Multi-model / LoRA routing', 'prepare', 'multi-model', 'multi-model-routing', 'Resolve model and adapter identity before selecting a serving pool.'),
    capability('agentic', 'Stateful Responses API', 'request', 'agentic', 'agentic-api', 'Stateful conversations and tool loops require the Agentic API service and state store.'),
    capability('batch', 'Batch / async dispatch', 'request', 'batch', 'batch-serving', 'Queue batch jobs before dispatch; this is a separate ingress mode, not a hop in every request.'),
    capability('flow-control', 'Admission / fair queuing', 'routing', 'flow-control', 'flow-control', 'Admit and queue tenant requests before endpoint selection; requires flow-control policy and capacity calibration.'),
    capability('affinity', 'Prefix-cache affinity', 'routing', 'routing', 'optimized-baseline', 'The pinned Guide supplies cache-affinity producers, filter and load scorer. Plugin tuning uses the complete router overlay.', ['routerValues'], true),
    capability('load-routing', 'Token load scorer', 'routing', 'routing', 'optimized-baseline', 'Scores endpoints using in-flight token load. Keep producer, scorer and scheduling profile consistent.', ['routerValues'], true),
    capability('latency', 'Predicted latency routing', 'routing', 'latency', 'predicted-latency-routing', 'Predict endpoint latency using a trained live model; needs producer, calibration and predictor lifecycle.'),
    capability('index', 'Precise prefix-cache routing', 'routing', 'index', 'precise-prefix-cache-routing', 'Tokenization and KV events provide exact prefix-block locations to EPP.', ['blockSize'], true),
    capability('coordinator', 'Pipeline coordinator', 'routing', 'coordinator', 'coord-disaggregation', 'Schedules each Encode / Prefill / Decode phase just before execution; replaces fixed per-pod orchestration.'),
    capability('prefix', 'Prefix caching', 'prefill', 'prefix', 'tiered-prefix-cache', 'Reuse matching local KV blocks to skip repeated prompt computation.', ['prefixCaching', 'blockSize'], true),
    capability('chunked', 'Chunked prefill', 'prefill', 'chunked', 'tiered-prefix-cache', 'Split large prompts into token-budgeted prefill work alongside decoding.', ['chunkedPrefill', 'maxNumBatchedTokens', 'maxNumSeqs'], true),
    capability('transfer', 'Prefill/Decode disaggregation', 'prefill', 'transfer', 'pd-disaggregation', 'Separate worker roles and transport KV from Prefill to Decode.', ['prefillReplicas', 'prefillTp', 'replicas', 'tp', 'rdmaNicCount'], true),
    capability('parallel', 'Tensor parallelism / batching', 'decode', 'parallel', 'optimized-baseline', 'Shard model execution across devices and control engine scheduling capacity.', ['tp', 'maxModelLen', 'maxNumSeqs', 'maxNumBatchedTokens', 'gpuMemoryUtilization', 'blockSize', 'dtype', 'kvCacheDtype', 'eager'], true),
    capability('wide-ep', 'Expert / data parallelism', 'decode', 'wide-ep', 'wide-ep', 'MoE experts, DP ranks and distributed collectives require coordinated workload and networking changes.'),
    capability('speculation', 'Speculative decode / MTP', 'decode', 'speculation', 'models/glm-5-2', 'Draft and verify multiple tokens; model recipe and runtime compatibility are required.'),
    capability('omni', 'Omni output generation', 'response', 'omni', 'omni-serving', 'Audio or image generation uses specialized runtime stages rather than a text-only Decode pool.'),
    capability('cache', 'CPU RAM cache offloading', 'cache-plane', 'cache', 'tiered-prefix-cache', 'Move reusable KV between accelerator memory and a supported CPU cache connector.', ['cacheCpuGiB', 'blockSize'], true),
    capability('disk-cache', 'Filesystem cache offloading', 'cache-plane', 'disk-cache', 'tiered-prefix-cache', 'Persist offloaded KV in a disk tier; requires storage paths, connector settings and mounts.'),
    capability('p2p-cache', 'P2P KV cache sharing', 'cache-plane', 'p2p-cache', 'p2p-kv-cache-sharing', 'Fetch cached prefix blocks from another worker instead of recomputing them.'),
    capability('scaling', 'Workload autoscaling', 'pods', 'scaling', 'workload-autoscaling', 'A control loop changes replicas based on workload and service objectives.'),
    capability('actuation', 'Fast model actuation', 'pods', 'actuation', 'fast-model-actuation-base', 'Wake or launch pre-provisioned engines to reduce scale-out latency.'),
    capability('keda', 'KEDA actuation scaling', 'pods', 'keda', 'fast-model-actuation-keda', 'Scale requester workloads from saturation metrics with a KEDA controller.'),
    capability('snapshot', 'Pod snapshot restore', 'pods', 'snapshot', 'pod-snapshot', 'Restore checkpointed model-server Pods; platform and storage prerequisites apply.'),
    capability('weights', 'P2P model weight loading', 'workers', 'weights', 'modelexpress-p2p', 'Transfer weights from a seed replica during startup; independent of request KV transfer.'),
    capability('rollouts', 'Traffic / adapter rollouts', 'service', 'rollouts', 'rollouts', 'Shift traffic or adapter versions using route and rollout resources.'),
    capability('inventory', 'Runtime inventory', 'cluster', 'inventory', 'runtime-inventory', 'Audit serving models and runtime provenance with the inventory controller.'),
    capability('tracing', 'Distributed tracing', 'response', 'tracing', 'recipes/router', 'Export router spans to an existing OpenTelemetry collector.', ['tracingEnabled', 'otelEndpoint', 'sampler', 'samplerArg'], true),
];
const field = (key, label, group, type, output, hint, extra = {}) => ({ key, label, group, type, output, hint, ...extra });
export const PARAMETER_FIELDS = [
    field('replicas', 'Serving / Decode replicas', 'workers', 'integer', 'workload.spec.replicas', 'Desired instances of the serving or Decode role.', { min: 1 }),
    field('tp', 'Serving / Decode TP', 'workers', 'integer', '--tensor-parallel-size', 'Devices per serving or Decode instance.', { min: 1 }),
    field('prefillReplicas', 'Prefill replicas', 'workers', 'integer', 'prefill.spec.replicas', 'Used only with P/D enabled.', { min: 1 }),
    field('prefillTp', 'Prefill TP', 'workers', 'integer', 'prefill --tensor-parallel-size', 'Devices per Prefill instance.', { min: 1 }),
    field('maxModelLen', 'Context length', 'engine', 'integer', '--max-model-len', 'Tokens, including prompt and generated output.', { min: 1 }),
    field('maxNumSeqs', 'Concurrent sequences', 'engine', 'integer', '--max-num-seqs', 'Maximum sequences processed in an engine iteration.', { min: 1 }),
    field('maxNumBatchedTokens', 'Batch token budget', 'engine', 'integer', '--max-num-batched-tokens', 'Tokens per engine iteration; also affects Prefill calibration.', { min: 1 }),
    field('gpuMemoryUtilization', 'Engine memory fraction', 'engine', 'number', '--gpu-memory-utilization', 'Fraction of device memory reserved by the engine.', { min: 0, exclusiveMin: true, max: 1 }),
    field('blockSize', 'KV block size', 'engine', 'integer', '--block-size', 'Tokens per KV block; precise routing must use the same size.', { min: 1 }),
    field('prefixCaching', 'Prefix caching', 'engine', 'flag', '--enable-prefix-caching', 'Empty inherits the runtime. Emit the positive flag only; disabling varies by runtime version.'),
    field('chunkedPrefill', 'Chunked prefill', 'engine', 'flag', '--enable-chunked-prefill', 'Empty inherits the runtime; enabling emits the engine flag.'),
    field('dtype', 'Model compute dtype', 'engine', 'text', '--dtype', 'A dtype accepted by the selected engine image; model and hardware compatibility still apply.'),
    field('kvCacheDtype', 'KV cache dtype', 'engine', 'text', '--kv-cache-dtype', 'Changing precision affects block capacity and requires recalibration.'),
    field('eager', 'Eager execution', 'engine', 'flag', '--enforce-eager', 'Skip graph capture; may reduce startup time at the cost of steady-state performance.'),
    field('cacheCpuGiB', 'CPU cache capacity', 'cache', 'number', 'guideSettings.cacheCpuGiB', 'GiB, per selected CPU offload adapter.', { min: 0, exclusiveMin: true }),
    field('rdmaNicCount', 'RDMA NIC count', 'transfer', 'integer', 'guideSettings.rdmaNicCount', 'Only emitted for the vllm-rdma variant.', { min: 1 }),
    field('eppReplicas', 'EPP replicas', 'router', 'integer', 'router.epp.replicas', 'Precise token-load routing currently requires one replica.', { min: 1 }),
    field('eppVerbosity', 'EPP log verbosity', 'router', 'integer', 'router.epp.flags.v', 'Higher verbosity increases logging overhead.', { min: 0 }),
    field('eppCpu', 'EPP CPU request', 'router', 'text', 'router.epp.resources.requests.cpu', 'Kubernetes CPU quantity, for example 500m.'),
    field('eppMemory', 'EPP memory request', 'router', 'text', 'router.epp.resources.requests.memory', 'Kubernetes memory quantity, for example 2Gi.'),
    field('eppMemoryLimit', 'EPP memory limit', 'router', 'text', 'router.epp.resources.limits.memory', 'Kubernetes memory quantity.'),
    field('failureMode', 'Endpoint-picker failure mode', 'router', 'select', 'router.inferencePool.failureMode', 'Behavior when endpoint selection fails.', { options: ['FailOpen', 'FailClose'] }),
    field('serverProtocol', 'Model-server protocol', 'router', 'select', 'router.modelServers.protocol', 'Must match the selected runtime endpoint.', { options: ['http', 'grpc'] }),
    field('proxyCpu', 'Proxy CPU request', 'router', 'text', 'router.proxy.resources.requests.cpu', 'Kubernetes CPU quantity.'),
    field('proxyMemory', 'Proxy memory request', 'router', 'text', 'router.proxy.resources.requests.memory', 'Kubernetes memory quantity.'),
    field('proxyMemoryLimit', 'Proxy memory limit', 'router', 'text', 'router.proxy.resources.limits.memory', 'Kubernetes memory quantity.'),
    field('tracingEnabled', 'Router tracing', 'tracing', 'select', 'router.tracing.enabled', 'Requires an existing reachable collector.', { options: ['true', 'false'] }),
    field('otelEndpoint', 'OTLP exporter endpoint', 'tracing', 'text', 'router.tracing.otelExporterEndpoint', 'Collector URL; this does not deploy a collector.'),
    field('sampler', 'Trace sampler', 'tracing', 'select', 'router.tracing.sampling.sampler', 'Sampler supported by the router OpenTelemetry SDK.', { options: ['parentbased_traceidratio', 'traceidratio', 'always_on', 'always_off'] }),
    field('samplerArg', 'Trace sampling ratio', 'tracing', 'number', 'router.tracing.sampling.samplerArg', 'A fraction from 0 to 1; serialized as the chart string value.', { min: 0, max: 1 }),
].map(item => ({ ...item, source: source(item.group === 'router' || item.group === 'tracing' ? 'guides/recipes/router/base.values.yaml' : 'guides/README.md') }));
export function capabilityEnabled(item, draft) {
    if (item.id === 'transfer') return draft.pd;
    if (item.id === 'index') return draft.precise;
    if (item.id === 'cache') return draft.cache !== 'none';
    if (item.id === 'tracing') return draft.tracingEnabled === 'true';
    if (item.id === 'prefix') return draft.prefixCaching === 'enabled';
    if (item.id === 'chunked') return draft.chunkedPrefill === 'enabled';
    return item.id === 'parallel';
}
