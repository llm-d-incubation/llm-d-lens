/** Reviewed source snapshot; deployment provenance is resolved separately by the planner. */
const REFERENCE_SOURCE = { repository: 'llm-d/llm-d', commit: 'a9cf07a77a3016db21ae87134e5745b3a2d45ba6' };
const source = path => `https://github.com/${REFERENCE_SOURCE.repository}/blob/${REFERENCE_SOURCE.commit}/${path}`;
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
