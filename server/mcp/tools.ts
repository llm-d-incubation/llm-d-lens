// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// -----------------------------------------------------------------------------
// AUTO-GENERATED FILE -- produced by `node scripts/generate-mcp-tools.mjs` from
// the llm_d_bench FastAPI app's OpenAPI schema (one tool per HTTP operation that
// declares BOTH a summary and a description). DO NOT HAND-EDIT THIS FILE -- it is
// overwritten wholesale every time the generator runs. To change a generated
// tool, edit the route's FastAPI decorator (summary/description/operation_id)
// instead and re-run the generator. riskTier is derived from HTTP method by
// default (GET/HEAD -> read, POST/PUT/PATCH -> write, DELETE -> approve), with a
// small named exception list (RISK_TIER_OVERRIDES in the generator script) for
// operationIds known to have a real side effect despite their HTTP method.
//
// Hand-written tools that are not a 1:1 wrapper over one FastAPI operation live
// in server/mcp/specialTools.ts instead -- edit that file directly, never this
// one.
// -----------------------------------------------------------------------------

import { z } from 'zod';
import { buildQuery, internalRequest } from './internal.ts';
import { jsonRequest, toPlainResult, prismTools, findTool, type PrismTool } from './specialTools.ts';

const generatedTools: PrismTool[] = [
    {
        name: "list_simulation_backends",
        description: "List Simulation backends Lens knows about and whether each backend is available in the current environment.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/simulation/backends")),
    },
    {
        name: "discover_simulation_models",
        description: "Probe a model endpoint and discover which models it reports. Use this before creating a simulation task when the target endpoint is not yet fully described.",
        riskTier: "write",
        inputShape: {
            "endpoint_url": z.string(),
        },
        handler: async (args) => jsonRequest("/api/simulation/models/discover", "POST", { "endpoint_url": args["endpoint_url"] }),
    },
    {
        name: "list_simulation_scenarios",
        description: "List the simulation scenario catalog Lens exposes, such as chat, API-calling, and coding.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/simulation/scenarios")),
    },
    {
        name: "list_trace_datasets",
        description: "List registered trace datasets and the backends that can replay each one.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/simulation/trace-datasets")),
    },
    {
        name: "download_trace_dataset",
        description: "Download a registered trace dataset into Lens\\'s local trace store. Use this before replaying a dataset that is not yet present on disk.",
        riskTier: "write",
        inputShape: {
            "dataset": z.string(),
            "force": z.boolean().optional(),
        },
        handler: async (args) => jsonRequest("/api/simulation/trace-datasets/download", "POST", { "dataset": args["dataset"], "force": args["force"] }),
    },
    {
        name: "get_trace_dataset_timeline",
        description: "Get a pre-aggregated timeline view for one trace dataset. Use this to inspect traffic intensity and replay shape before running Simulation.",
        riskTier: "read",
        inputShape: {
            "dataset_name": z.string(),
            "bins": z.number().int().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/simulation/trace-datasets/${encodeURIComponent(String(args["dataset_name"]))}/timeline` + buildQuery({ "bins": args["bins"] as string | number | undefined }))),
    },
    {
        name: "list_simulation_tasks",
        description: "List Simulation tasks with optional filters for status, scenario, backend, model, dataset, endpoint, and creation window.",
        riskTier: "read",
        inputShape: {
            "status": z.enum(["queued", "running", "completed", "failed", "cancelled"]).nullable().optional(),
            "scenario": z.enum(["chat", "api-calling", "coding"]).nullable().optional(),
            "search": z.string().nullable().optional(),
            "backend": z.enum(["aiperf", "trace-replayer"]).nullable().optional(),
            "model": z.string().nullable().optional(),
            "dataset": z.string().nullable().optional(),
            "trace_format": z.enum(["mooncake_trace", "bailian_trace", "baseten_trace", "burst_gpt_trace", "weka_public_dataset"]).nullable().optional(),
            "endpoint": z.string().nullable().optional(),
            "created_after": z.string().nullable().optional(),
            "created_before": z.string().nullable().optional(),
            "offset": z.number().int().optional(),
            "limit": z.number().int().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/simulation/tasks" + buildQuery({ "status": args["status"] as string | number | undefined, "scenario": args["scenario"] as string | number | undefined, "search": args["search"] as string | number | undefined, "backend": args["backend"] as string | number | undefined, "model": args["model"] as string | number | undefined, "dataset": args["dataset"] as string | number | undefined, "trace_format": args["trace_format"] as string | number | undefined, "endpoint": args["endpoint"] as string | number | undefined, "created_after": args["created_after"] as string | number | undefined, "created_before": args["created_before"] as string | number | undefined, "offset": args["offset"] as string | number | undefined, "limit": args["limit"] as string | number | undefined }))),
    },
    {
        name: "create_simulation_task",
        description: "Create a Simulation task that replays a trace dataset against a target model endpoint.",
        riskTier: "write",
        inputShape: {
            "name": z.string().optional(),
            "description": z.string().optional(),
            "scenario": z.enum(["chat", "api-calling", "coding"]),
            "backend": z.enum(["aiperf", "trace-replayer"]).optional(),
            "endpoint_mode": z.enum(["external", "in-cluster", "deployment"]).optional(),
            "cluster_session_id": z.string().nullable().optional(),
            "endpoint_namespace": z.string().nullable().optional(),
            "endpoint_service": z.string().nullable().optional(),
            "endpoint_deployment_execution_id": z.string().nullable().optional(),
            "model_service_group_id": z.string().nullable().optional(),
            "endpoint_cluster_id": z.string().nullable().optional(),
            "endpoint_cluster_name": z.string().nullable().optional(),
            "endpoint_deployment_name": z.string().nullable().optional(),
            "endpoint_url": z.string(),
            "model_name": z.string(),
            "api_key": z.string().nullable().optional(),
            "trace_dataset": z.string(),
            "trace_path": z.string(),
            "duration_seconds": z.number().int().optional(),
            "scale_factor": z.number().optional(),
            "trace_start_seconds": z.number().optional(),
            "trace_end_seconds": z.number().nullable().optional(),
            "trace_timeout_seconds": z.number().int().optional(),
            "fixed_schedule": z.boolean().optional(),
            "stream": z.boolean().optional(),
            "backend_options": z.record(z.string(), z.unknown()).optional(),
        },
        handler: async (args) => jsonRequest("/api/simulation/tasks", "POST", { "name": args["name"], "description": args["description"], "scenario": args["scenario"], "backend": args["backend"], "endpoint_mode": args["endpoint_mode"], "cluster_session_id": args["cluster_session_id"], "endpoint_namespace": args["endpoint_namespace"], "endpoint_service": args["endpoint_service"], "endpoint_deployment_execution_id": args["endpoint_deployment_execution_id"], "model_service_group_id": args["model_service_group_id"], "endpoint_cluster_id": args["endpoint_cluster_id"], "endpoint_cluster_name": args["endpoint_cluster_name"], "endpoint_deployment_name": args["endpoint_deployment_name"], "endpoint_url": args["endpoint_url"], "model_name": args["model_name"], "api_key": args["api_key"], "trace_dataset": args["trace_dataset"], "trace_path": args["trace_path"], "duration_seconds": args["duration_seconds"], "scale_factor": args["scale_factor"], "trace_start_seconds": args["trace_start_seconds"], "trace_end_seconds": args["trace_end_seconds"], "trace_timeout_seconds": args["trace_timeout_seconds"], "fixed_schedule": args["fixed_schedule"], "stream": args["stream"], "backend_options": args["backend_options"] }),
    },
    {
        name: "get_simulation_task",
        description: "Get one Simulation task by id, including progress, logs, and result artifacts when they exist.",
        riskTier: "read",
        inputShape: {
            "task_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/simulation/tasks/${encodeURIComponent(String(args["task_id"]))}`)),
    },
    {
        name: "delete_simulation_task",
        description: "Delete a Simulation task record. Use this for final cleanup after a task is complete, failed, or otherwise no longer needed.",
        riskTier: "approve",
        inputShape: {
            "task_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/simulation/tasks/${encodeURIComponent(String(args["task_id"]))}`, "DELETE", undefined),
    },
    {
        name: "download_simulation_artifact",
        description: "Download a saved simulation artifact by task id and artifact kind as a file attachment. Use the task result to discover available artifact kinds. Returns 404 when the task, result, or artifact file is unavailable.",
        riskTier: "read",
        inputShape: {
            "task_id": z.string(),
            "kind": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/simulation/tasks/${encodeURIComponent(String(args["task_id"]))}/artifacts/${encodeURIComponent(String(args["kind"]))}`)),
    },
    {
        name: "get_simulation_response_code_issues",
        description: "Get requests from a Simulation task that returned a specific HTTP status code. Use this to inspect failing or anomalous responses in detail.",
        riskTier: "read",
        inputShape: {
            "task_id": z.string(),
            "status_code": z.string(),
            "offset": z.number().int().optional(),
            "limit": z.number().int().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/simulation/tasks/${encodeURIComponent(String(args["task_id"]))}/response-code-issues` + buildQuery({ "status_code": args["status_code"] as string | number | undefined, "offset": args["offset"] as string | number | undefined, "limit": args["limit"] as string | number | undefined }))),
    },
    {
        name: "stop_simulation_task",
        description: "Stop an active Simulation task. Use this when a replay should be cancelled before it finishes.",
        riskTier: "write",
        inputShape: {
            "task_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/simulation/tasks/${encodeURIComponent(String(args["task_id"]))}/stop`, "POST", undefined),
    },
    {
        name: "rerun_simulation_task",
        description: "Queue a rerun of a previous Simulation task, optionally rebinding it to a fresh endpoint or deployment execution.",
        riskTier: "write",
        inputShape: {
            "task_id": z.string(),
            body: z.unknown().describe('Request body for this operation.'),
        },
        handler: async (args) => jsonRequest(`/api/simulation/tasks/${encodeURIComponent(String(args["task_id"]))}/rerun`, "POST", { "body": args["body"] }),
    },
    {
        name: "list_agentic_plans",
        description: "List Agentic Deployment plans (recommendations) created in this Lens instance, with their current status.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/agentic-deployments")),
    },
    {
        name: "create_agentic_plan",
        description: "Create a new Agentic Deployment plan. Provide the standard deployment inputs plus planning facts so Lens can propose, score, and stage candidate deployment topologies.",
        riskTier: "write",
        inputShape: {
            "model": z.string(),
            "deployment_name": z.string(),
            "description": z.string().optional(),
            "cluster_session_id": z.string(),
            "replicas": z.number().int(),
            "tensor_parallel_size": z.number().int(),
            "storage_type": z.enum(["local-cache", "pvc", "model-cache"]).optional(),
            "storage_volume_id": z.string().optional(),
            "model_cache_path": z.string().optional(),
            "pvc_name": z.string().optional(),
            "max_model_len": z.number().int().optional(),
            "gpu_memory_utilization": z.number().optional(),
            "max_num_seqs": z.number().int().optional(),
            "max_num_batched_tokens": z.number().int().optional(),
            "enable_prefix_caching": z.boolean().optional(),
            "enforce_eager": z.boolean().optional(),
            "vllm_arguments": z.array(z.record(z.string(), z.unknown())).optional(),
            "use_cluster_proxy": z.boolean().optional(),
            "http_proxy": z.string().optional(),
            "https_proxy": z.string().optional(),
            "no_proxy": z.string().optional(),
            "execution_policy": z.object({ "mode": z.enum(["approval_required", "automatic"]).optional(), "max_optimization_iterations": z.number().int().optional(), "max_benchmark_runs": z.number().int().optional(), "max_total_duration_minutes": z.number().int().optional() }).passthrough().optional(),
            "planning_facts": z.object({ "model_weight_gib": z.number(), "vram_per_gpu_gib": z.number(), "free_gpu_count": z.number().int(), "cpu_buffer_gib": z.number().optional(), "required_cpu_buffer_gib": z.number().optional(), "context_length": z.number().int().optional(), "shared_prefix_ratio": z.number().optional(), "prefill_heavy": z.boolean().optional(), "use_case": z.string().optional(), "ttft_slo_ms": z.number().int().nullable().optional(), "tpot_slo_ms": z.number().int().nullable().optional(), "end_to_end_latency_slo_ms": z.number().int().nullable().optional(), "operator_preference": z.string().optional(), "vllm_arguments": z.array(z.array(z.unknown())).optional(), "evidence_ids": z.array(z.string()).optional(), "workload_profile": z.object({ "mean_input_tokens": z.number().nullable().optional(), "p95_input_tokens": z.number().nullable().optional(), "mean_output_tokens": z.number().nullable().optional(), "concurrency": z.number().nullable().optional(), "request_rate": z.number().nullable().optional(), "shared_prefix_ratio": z.number().nullable().optional(), "prefill_p95_ms": z.number().nullable().optional(), "decode_p95_ms": z.number().nullable().optional() }).passthrough().nullable().optional(), "aic_predictions": z.array(z.object({ "mode": z.enum(["agg", "disagg"]), "tensor_parallel_size": z.number().int(), "replicas": z.number().int(), "prefill_tensor_parallel_size": z.number().int().nullable().optional(), "prefill_replicas": z.number().int().nullable().optional(), "ttft_ms": z.number().nullable().optional(), "tpot_ms": z.number().nullable().optional(), "throughput_tokens_per_sec": z.number().nullable().optional() }).passthrough()).optional(), "performance_score_inputs": z.array(z.object({ "candidate_id": z.string(), "source": z.string(), "status": z.enum(["measured", "interpolated", "estimated", "unknown"]), "ttft_p95_ms": z.number().nullable().optional(), "tpot_p95_ms": z.number().nullable().optional(), "throughput_tokens_per_s": z.number().nullable().optional(), "error_rate": z.number().nullable().optional(), "confidence": z.number().optional(), "effective_sample_size": z.number().optional(), "has_blocking_negative_evidence": z.boolean().optional(), "evidence_refs": z.array(z.string()).optional(), "weighted_samples": z.array(z.object({ "benchmark_id": z.string(), "similarity": z.number(), "time_decay": z.number(), "runtime_similarity": z.number(), "match_confidence": z.number(), "observed_at": z.string(), "outcome": z.enum(["succeeded", "failed"]), "ttft_p95_ms": z.number().nullable().optional(), "tpot_p95_ms": z.number().nullable().optional(), "throughput_tokens_per_s": z.number().nullable().optional(), "error_rate": z.number().nullable().optional(), "failure_reason": z.string().nullable().optional(), "topology": z.string().optional(), "decode_tp": z.number().int().optional(), "decode_replicas": z.number().int().optional(), "prefill_tp": z.number().int().nullable().optional(), "prefill_replicas": z.number().int().nullable().optional(), "optimizations": z.array(z.string()).optional() }).passthrough()).optional() }).passthrough()).optional(), "hardware_capacity_mode": z.enum(["free", "all", "custom"]).optional(), "custom_gpu_count": z.number().int().nullable().optional(), "model_name": z.string().nullable().optional(), "model_config_dict": z.record(z.string(), z.unknown()).nullable().optional() }).passthrough(),
            "planner_prompt": z.string().nullable().optional(),
            "ai_provider_id": z.string().nullable().optional(),
        },
        handler: async (args) => jsonRequest("/api/agentic-deployments", "POST", { "model": args["model"], "deployment_name": args["deployment_name"], "description": args["description"], "cluster_session_id": args["cluster_session_id"], "replicas": args["replicas"], "tensor_parallel_size": args["tensor_parallel_size"], "storage_type": args["storage_type"], "storage_volume_id": args["storage_volume_id"], "model_cache_path": args["model_cache_path"], "pvc_name": args["pvc_name"], "max_model_len": args["max_model_len"], "gpu_memory_utilization": args["gpu_memory_utilization"], "max_num_seqs": args["max_num_seqs"], "max_num_batched_tokens": args["max_num_batched_tokens"], "enable_prefix_caching": args["enable_prefix_caching"], "enforce_eager": args["enforce_eager"], "vllm_arguments": args["vllm_arguments"], "use_cluster_proxy": args["use_cluster_proxy"], "http_proxy": args["http_proxy"], "https_proxy": args["https_proxy"], "no_proxy": args["no_proxy"], "execution_policy": args["execution_policy"], "planning_facts": args["planning_facts"], "planner_prompt": args["planner_prompt"], "ai_provider_id": args["ai_provider_id"] }),
    },
    {
        name: "get_agentic_plan",
        description: "Get the full detail of one Agentic Deployment plan: candidates, evidence, scoring source (deterministic vs.",
        riskTier: "read",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/agentic-deployments/${encodeURIComponent(String(args["run_id"]))}`)),
    },
    {
        name: "select_agentic_plan_candidate",
        description: "Select one candidate inside an existing Agentic Deployment plan. Use this when the planner produced multiple candidates and you want to pin one before approval or refinement.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
            "candidate_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/agentic-deployments/${encodeURIComponent(String(args["run_id"]))}/select`, "POST", { "candidate_id": args["candidate_id"] }),
    },
    {
        name: "refine_agentic_plan",
        description: "Ask Lens to refine an Agentic Deployment plan with a new planner prompt. Use this to request another planning pass with tighter constraints or different priorities.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
            "planner_prompt": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/agentic-deployments/${encodeURIComponent(String(args["run_id"]))}/refine`, "POST", { "planner_prompt": args["planner_prompt"] }),
    },
    {
        name: "approve_agentic_plan",
        description: "Approve an Agentic Deployment plan so Lens can begin executing it. Use this only after reviewing the selected candidate and evidence.",
        riskTier: "approve",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/agentic-deployments/${encodeURIComponent(String(args["run_id"]))}/approve`, "POST", undefined),
    },
    {
        name: "check_aic_support",
        description: "Check whether AIConfigurator supports the requested model, system, backend, and database mode. Returns supported serving modes, constraints, and a reason when unsupported. Use this before candidate search.",
        riskTier: "write",
        inputShape: {
            "model_name": z.string(),
            "gpu_count": z.number().int().optional(),
            "mean_input_tokens": z.number().int().optional(),
            "mean_output_tokens": z.number().int().optional(),
            "ttft_target_ms": z.number().nullable().optional(),
            "tpot_target_ms": z.number().nullable().optional(),
            "aic_system_name": z.string(),
            "aic_backend_name": z.string().optional(),
            "aic_database_mode": z.string().optional(),
            "max_candidates": z.number().int().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/aic/support", "POST", { "model_name": args["model_name"], "gpu_count": args["gpu_count"], "mean_input_tokens": args["mean_input_tokens"], "mean_output_tokens": args["mean_output_tokens"], "ttft_target_ms": args["ttft_target_ms"], "tpot_target_ms": args["tpot_target_ms"], "aic_system_name": args["aic_system_name"], "aic_backend_name": args["aic_backend_name"], "aic_database_mode": args["aic_database_mode"], "max_candidates": args["max_candidates"] }),
    },
    {
        name: "search_aic_configurations",
        description: "Search for serving configurations using the requested model, hardware target, workload token lengths, and latency targets. Returns candidate configurations and the chosen serving mode, subject to max_candidates.",
        riskTier: "write",
        inputShape: {
            "model_name": z.string(),
            "gpu_count": z.number().int().optional(),
            "mean_input_tokens": z.number().int().optional(),
            "mean_output_tokens": z.number().int().optional(),
            "ttft_target_ms": z.number().nullable().optional(),
            "tpot_target_ms": z.number().nullable().optional(),
            "aic_system_name": z.string(),
            "aic_backend_name": z.string().optional(),
            "aic_database_mode": z.string().optional(),
            "max_candidates": z.number().int().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/aic/search", "POST", { "model_name": args["model_name"], "gpu_count": args["gpu_count"], "mean_input_tokens": args["mean_input_tokens"], "mean_output_tokens": args["mean_output_tokens"], "ttft_target_ms": args["ttft_target_ms"], "tpot_target_ms": args["tpot_target_ms"], "aic_system_name": args["aic_system_name"], "aic_backend_name": args["aic_backend_name"], "aic_database_mode": args["aic_database_mode"], "max_candidates": args["max_candidates"] }),
    },
    {
        name: "list_aic_experiments",
        description: "Parse an AIConfigurator experiment YAML file and return the top experiments Lens can extract from it.",
        riskTier: "write",
        inputShape: {
            "yaml_text": z.string(),
            "top_n": z.number().int().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/aic/experiments", "POST", { "yaml_text": args["yaml_text"], "top_n": args["top_n"] }),
    },
    {
        name: "estimate_aic_configuration",
        description: "Estimate TTFT and TPOT for an aggregated or P/D topology with explicit tensor parallelism, replicas and total GPU count. Reject results that do not match the requested topology.",
        riskTier: "write",
        inputShape: {
            "model_name": z.string(),
            "scenario": z.enum(["inference_scheduling", "pd_disaggregation"]),
            "gpu_count": z.number().int(),
            "mean_input_tokens": z.number().int().optional(),
            "mean_output_tokens": z.number().int().optional(),
            "ttft_target_ms": z.number().nullable().optional(),
            "tpot_target_ms": z.number().nullable().optional(),
            "aic_system_name": z.string(),
            "aic_backend_name": z.string().optional(),
            "aic_database_mode": z.string().optional(),
            "tp": z.number().int().optional(),
            "pp": z.number().int().optional(),
            "replicas": z.number().int().optional(),
            "prefill_tp": z.number().int().optional(),
            "prefill_replicas": z.number().int().optional(),
            "decode_tp": z.number().int().optional(),
            "decode_replicas": z.number().int().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/aic/estimate", "POST", { "model_name": args["model_name"], "scenario": args["scenario"], "gpu_count": args["gpu_count"], "mean_input_tokens": args["mean_input_tokens"], "mean_output_tokens": args["mean_output_tokens"], "ttft_target_ms": args["ttft_target_ms"], "tpot_target_ms": args["tpot_target_ms"], "aic_system_name": args["aic_system_name"], "aic_backend_name": args["aic_backend_name"], "aic_database_mode": args["aic_database_mode"], "tp": args["tp"], "pp": args["pp"], "replicas": args["replicas"], "prefill_tp": args["prefill_tp"], "prefill_replicas": args["prefill_replicas"], "decode_tp": args["decode_tp"], "decode_replicas": args["decode_replicas"] }),
    },
    {
        name: "list_lens_hosts",
        description: "Returns this Lens host's candidate IP addresses/hostnames so the cluster create/edit form can offer and auto-fill a reachable address for per-request identity injection. The port is Lens' own live serving port, added automatically.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/cluster/lens-hosts")),
    },
    {
        name: "check_gateway_port",
        description: "Whether ``port`` can be used as the cluster Gateway's NodePort.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
            "port": z.number().int(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/gateway-port-check` + buildQuery({ "port": args["port"] as string | number | undefined }))),
    },
    {
        name: "list_clusters",
        description: "List Lens-managed clusters, optionally including draft records that have not yet been finalized. Use this to discover cluster ids before other cluster-scoped calls.",
        riskTier: "read",
        inputShape: {
            "includeDrafts": z.boolean().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/cluster/clusters" + buildQuery({ "includeDrafts": args["includeDrafts"] as string | number | undefined }))),
    },
    {
        name: "create_cluster",
        description: "Register a cluster using multipart form data with a name and UTF-8 kubeconfig file, plus optional description, proxy settings, software refs, and draft flag. Returns the cluster record and a session id when a session is created.",
        riskTier: "write",
        inputShape: {
        },
        handler: async () => jsonRequest("/api/cluster/clusters", "POST", undefined),
    },
    {
        name: "update_cluster",
        description: "Patch a cluster\\'s editable settings such as name, description, proxy config, or pinned llm-d revisions. Use this for draft completion or later cluster maintenance.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string(),
            "name": z.string().nullable().optional(),
            "description": z.string().nullable().optional(),
            "proxy": z.object({ "mode": z.enum(["auto", "custom"]).optional(), "httpProxy": z.string().nullable().optional(), "httpsProxy": z.string().nullable().optional(), "noProxy": z.string().nullable().optional() }).passthrough().nullable().optional(),
            "llmDRef": z.string().nullable().optional(),
            "llmDBenchmarkRef": z.string().nullable().optional(),
            "gatewayProvider": z.string().nullable().optional(),
            "gatewayNamespace": z.string().nullable().optional(),
            "gatewayName": z.string().nullable().optional(),
            "gatewayPublicUrl": z.string().nullable().optional(),
            "gatewayPort": z.number().int().nullable().optional(),
            "gatewayAuthzHost": z.string().nullable().optional(),
            "inotifyMaxUserInstances": z.number().int().nullable().optional(),
            "routerVersion": z.string().nullable().optional(),
            "gieVersion": z.string().nullable().optional(),
            "ippVersion": z.string().nullable().optional(),
            "draft": z.boolean().nullable().optional(),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}`, "PATCH", { "name": args["name"], "description": args["description"], "proxy": args["proxy"], "llmDRef": args["llmDRef"], "llmDBenchmarkRef": args["llmDBenchmarkRef"], "gatewayProvider": args["gatewayProvider"], "gatewayNamespace": args["gatewayNamespace"], "gatewayName": args["gatewayName"], "gatewayPublicUrl": args["gatewayPublicUrl"], "gatewayPort": args["gatewayPort"], "gatewayAuthzHost": args["gatewayAuthzHost"], "inotifyMaxUserInstances": args["inotifyMaxUserInstances"], "routerVersion": args["routerVersion"], "gieVersion": args["gieVersion"], "ippVersion": args["ippVersion"], "draft": args["draft"] }),
    },
    {
        name: "delete_cluster",
        description: "Delete a Lens-managed cluster record and clean up related state. Use this only when the cluster should be removed from Lens entirely.",
        riskTier: "approve",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}`, "DELETE", undefined),
    },
    {
        name: "create_cluster_software_downloads",
        description: "Start downloading the llm-d and/or llm-d-benchmark source refs attached to a cluster. Use this after pinning software versions on a cluster record.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string(),
            "llmDRef": z.string().nullable().optional(),
            "llmDBenchmarkRef": z.string().nullable().optional(),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/software-downloads`, "POST", { "llmDRef": args["llmDRef"], "llmDBenchmarkRef": args["llmDBenchmarkRef"] }),
    },
    {
        name: "get_cluster_software_downloads",
        description: "Get the current llm-d and llm-d-benchmark download status for a cluster. Use this to poll cluster software preparation progress.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/software-downloads`)),
    },
    {
        name: "create_cluster_image_prepull",
        description: "Start a privileged DaemonSet that pulls the pinned llm-d-router (EPP, P/D sidecar) and model-server images for the selected accelerators onto every cluster node. Poll the GET below for per-node readiness.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string(),
            "accelerators": z.array(z.string()).optional(),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/images/prepull`, "POST", { "accelerators": args["accelerators"] }),
    },
    {
        name: "get_cluster_image_prepull",
        description: "Get the current image pre-pull status for a cluster: overall state, ready/desired node count and each puller pod's node readiness.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/images/prepull`)),
    },
    {
        name: "create_cluster_crds",
        description: "Apply the Gateway API and Gateway API Inference Extension CRD bundles pinned by the Lens stack profile. Returns a per-bundle result.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/crds`, "POST", undefined),
    },
    {
        name: "get_cluster_crds",
        description: "Report whether the pinned Gateway API and Gateway API Inference Extension CRDs are present on the cluster (ready/missing per bundle).",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/crds`)),
    },
    {
        name: "get_cluster_kubernetes_version",
        description: "Probe the cluster's Kubernetes API for reachability and its server version, and compare it with the minimum Kubernetes version pinned by the Lens stack profile.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/kubernetes-version`)),
    },
    {
        name: "create_cluster_hf_token_secret",
        description: "Create a standalone HF_TOKEN Kubernetes Secret on a Lens-managed cluster. Use this when a later Model Cache or deployment flow should reference an existing secret.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string(),
            "namespace": z.string(),
            "name": z.string().optional(),
            "token": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/hf-token-secrets`, "POST", { "namespace": args["namespace"], "name": args["name"], "token": args["token"] }),
    },
    {
        name: "get_cluster_session",
        description: "Get or create the active Lens session metadata for a cluster id. Use this when another API requires a cluster session id instead of a cluster id.",
        riskTier: "read",
        inputShape: {
            "clusterId": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/cluster/session" + buildQuery({ "clusterId": args["clusterId"] as string | number | undefined }))),
    },
    {
        name: "get_cluster_overview",
        description: "Get the live Kubernetes overview for a Lens-managed cluster, including node, GPU, cached container image, and component health summaries. Each worker node reports cachedImages from its Kubernetes node status; clients can use images shared by eligible workers to avoid an image pull at deployment time.",
        riskTier: "read",
        inputShape: {
            "clusterId": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/cluster/overview" + buildQuery({ "clusterId": args["clusterId"] as string | number | undefined }))),
    },
    {
        name: "list_cluster_model_secrets",
        description: "List model-related secrets visible to a cluster, including whether host-managed Hugging Face credentials are available.",
        riskTier: "read",
        inputShape: {
            "clusterId": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/cluster/model-secrets" + buildQuery({ "clusterId": args["clusterId"] as string | number | undefined }))),
    },
    {
        name: "cordon_cluster_nodes",
        description: "Mark one or more cluster worker nodes unschedulable so new pods stop landing there. Use this for maintenance windows or targeted isolation.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string(),
            "names": z.array(z.string()),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/nodes/cordon`, "POST", { "names": args["names"] }),
    },
    {
        name: "uncordon_cluster_nodes",
        description: "Mark one or more cluster worker nodes schedulable again. Use this to return previously cordoned nodes to normal service.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string(),
            "names": z.array(z.string()),
        },
        handler: async (args) => jsonRequest(`/api/cluster/clusters/${encodeURIComponent(String(args["cluster_id"]))}/nodes/uncordon`, "POST", { "names": args["names"] }),
    },
    {
        name: "discover_cluster_bootstrap_host_keys",
        description: "Read each target's SSH host key without authenticating, for explicit user verification.",
        riskTier: "write",
        inputShape: {
            "nodes": z.array(z.object({ "host": z.string(), "port": z.number().int().optional() }).passthrough()),
        },
        handler: async (args) => jsonRequest("/api/cluster/bootstrap/host-keys", "POST", { "nodes": args["nodes"] }),
    },
    {
        name: "preflight_cluster_bootstrap",
        description: "Run Kubespray bootstrap preflight checks against one or more target machines. Use this before starting a real cluster bootstrap job.",
        riskTier: "write",
        inputShape: {
            "nodes": z.array(z.object({ "host": z.string(), "port": z.number().int().optional(), "roles": z.array(z.enum(["control-plane", "worker"])).optional(), "username": z.string().optional(), "privateKey": z.string().nullable().optional(), "password": z.string().nullable().optional() }).passthrough()),
            "hostKeys": z.array(z.object({ "host": z.string(), "port": z.number().int().optional(), "fingerprint": z.string() }).passthrough()),
        },
        handler: async (args) => jsonRequest("/api/cluster/bootstrap/preflight", "POST", { "nodes": args["nodes"], "hostKeys": args["hostKeys"] }),
    },
    {
        name: "start_cluster_bootstrap",
        description: "Start a Kubespray-based cluster bootstrap job. Use this after preflight succeeds and you are ready to provision the cluster.",
        riskTier: "write",
        inputShape: {
            "nodes": z.array(z.object({ "host": z.string(), "port": z.number().int().optional(), "roles": z.array(z.enum(["control-plane", "worker"])).optional(), "username": z.string().optional(), "privateKey": z.string().nullable().optional(), "password": z.string().nullable().optional() }).passthrough()),
            "hostKeys": z.array(z.object({ "host": z.string(), "port": z.number().int().optional(), "fingerprint": z.string() }).passthrough()),
            "proxy": z.object({ "mode": z.enum(["auto", "custom"]).optional(), "httpProxy": z.string().nullable().optional(), "httpsProxy": z.string().nullable().optional(), "noProxy": z.string().nullable().optional() }).passthrough().nullable().optional(),
        },
        handler: async (args) => jsonRequest("/api/cluster/bootstrap", "POST", { "nodes": args["nodes"], "hostKeys": args["hostKeys"], "proxy": args["proxy"] }),
    },
    {
        name: "get_cluster_bootstrap_status",
        description: "Get the current status of a cluster bootstrap job, including phase, log tail, and one-time kubeconfig handoff when the job succeeds.",
        riskTier: "read",
        inputShape: {
            "bootstrap_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/cluster/bootstrap/${encodeURIComponent(String(args["bootstrap_id"]))}`)),
    },
    {
        name: "cancel_cluster_bootstrap",
        description: "Cancel a running cluster bootstrap job. Use this when the current bootstrap should stop and not continue provisioning.",
        riskTier: "write",
        inputShape: {
            "bootstrap_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/cluster/bootstrap/${encodeURIComponent(String(args["bootstrap_id"]))}/cancel`, "POST", undefined),
    },
    {
        name: "get_configuration_capabilities",
        description: "Get Lens Configuration capability metadata and provider catalog information. Use this to discover supported configuration providers and schema versions.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/configurations/capabilities")),
    },
    {
        name: "list_configuration_artifacts",
        description: "List published Lens configuration artifacts. Use this to discover artifact ids that can be deployed or benchmarked later.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/configurations/artifacts")),
    },
    {
        name: "get_configuration_artifact",
        description: "Get one published Lens configuration artifact by id, including the deployable configuration and saved file metadata.",
        riskTier: "read",
        inputShape: {
            "artifact_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/configurations/artifacts/${encodeURIComponent(String(args["artifact_id"]))}`)),
    },
    {
        name: "delete_configuration_artifact",
        description: "Delete a published configuration artifact from Lens. Use this when an obsolete or incorrect artifact should no longer be deployable.",
        riskTier: "approve",
        inputShape: {
            "artifact_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/configurations/artifacts/${encodeURIComponent(String(args["artifact_id"]))}`, "DELETE", undefined),
    },
    {
        name: "get_configuration_artifact_manifest",
        description: "Download the rendered YAML manifest file for a published configuration artifact. Use this when you need the exact manifest text Lens saved.",
        riskTier: "read",
        inputShape: {
            "artifact_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/configurations/artifacts/${encodeURIComponent(String(args["artifact_id"]))}/manifest`)),
    },
    {
        name: "get_configuration_artifact_bundle",
        description: "Download a ZIP containing the published configuration, rendered model-server manifest, deployment bundle, Helm values, auxiliary resources, and calibration recipe when present. Older artifacts without a deployment bundle return 409 and must be regenerated.",
        riskTier: "read",
        inputShape: {
            "artifact_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/configurations/artifacts/${encodeURIComponent(String(args["artifact_id"]))}/bundle`)),
    },
    {
        name: "create_deployment_run",
        description: "Start a deployment run from one or more already-rendered deployable configurations. Use this when you want Lens Deploy to execute a configuration artifact or custom deployable payload. The run starts queued and its cases may show pods stuck Pending if the cluster currently lacks free resources to schedule them -- tell the user right away that it looks resource-constrained and queued, rather than silently waiting for it to finish.",
        riskTier: "write",
        inputShape: {
            "configurations": z.array(z.object({ "schema_version": z.string().optional(), "type": z.string(), "format": z.string(), "content": z.record(z.string(), z.unknown()).optional(), "provider_ref": z.string(), "checksum": z.string(), "provenance": z.record(z.string(), z.unknown()).optional() }).passthrough()),
            "failure_policy": z.string().optional(),
            "provenance": z.record(z.string(), z.unknown()).optional(),
            "runtime_binding": z.object({ "schema_version": z.string().optional(), "environment": z.record(z.string(), z.unknown()).optional(), "cluster_session_id": z.string().nullable().optional() }).passthrough().nullable().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/deployments/runs", "POST", { "configurations": args["configurations"], "failure_policy": args["failure_policy"], "provenance": args["provenance"], "runtime_binding": args["runtime_binding"] }),
    },
    {
        name: "list_deployment_runs",
        description: "List deployment runs Lens has recorded. Use the optional query string to search by run id, provider ref, deployment name, or namespace.",
        riskTier: "read",
        inputShape: {
            "query": z.string().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/deployments/runs" + buildQuery({ "query": args["query"] as string | number | undefined }))),
    },
    {
        name: "create_standard_vllm_deployment_run",
        description: "Start a Standard Kubernetes Service deployment run using Lens\\'s allowlisted vLLM contract. Use this for direct baseline or simple model-service deployments without a prebuilt configuration artifact.",
        riskTier: "write",
        inputShape: {
            "model": z.string(),
            "deployment_name": z.string(),
            "description": z.string().optional(),
            "cluster_session_id": z.string(),
            "replicas": z.number().int(),
            "tensor_parallel_size": z.number().int(),
            "storage_type": z.enum(["local-cache", "pvc", "model-cache"]).optional(),
            "storage_volume_id": z.string().optional(),
            "model_cache_path": z.string().optional(),
            "pvc_name": z.string().optional(),
            "max_model_len": z.number().int().optional(),
            "gpu_memory_utilization": z.number().optional(),
            "max_num_seqs": z.number().int().optional(),
            "max_num_batched_tokens": z.number().int().optional(),
            "enable_prefix_caching": z.boolean().optional(),
            "enforce_eager": z.boolean().optional(),
            "vllm_arguments": z.array(z.record(z.string(), z.unknown())).optional(),
            "use_cluster_proxy": z.boolean().optional(),
            "http_proxy": z.string().optional(),
            "https_proxy": z.string().optional(),
            "no_proxy": z.string().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/deployments/standard-vllm-runs", "POST", { "model": args["model"], "deployment_name": args["deployment_name"], "description": args["description"], "cluster_session_id": args["cluster_session_id"], "replicas": args["replicas"], "tensor_parallel_size": args["tensor_parallel_size"], "storage_type": args["storage_type"], "storage_volume_id": args["storage_volume_id"], "model_cache_path": args["model_cache_path"], "pvc_name": args["pvc_name"], "max_model_len": args["max_model_len"], "gpu_memory_utilization": args["gpu_memory_utilization"], "max_num_seqs": args["max_num_seqs"], "max_num_batched_tokens": args["max_num_batched_tokens"], "enable_prefix_caching": args["enable_prefix_caching"], "enforce_eager": args["enforce_eager"], "vllm_arguments": args["vllm_arguments"], "use_cluster_proxy": args["use_cluster_proxy"], "http_proxy": args["http_proxy"], "https_proxy": args["https_proxy"], "no_proxy": args["no_proxy"] }),
    },
    {
        name: "list_deployments",
        description: "Return the same deployment run collection as the /runs endpoint. Use the optional query parameter to search by run id, provider ref, deployment name, or namespace.",
        riskTier: "read",
        inputShape: {
            "query": z.string().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/deployments" + buildQuery({ "query": args["query"] as string | number | undefined }))),
    },
    {
        name: "list_deployment_executions",
        description: "List deployment executions without creating new port-forwards. Use this to discover execution ids, readiness, cluster affinity, and endpoint metadata.",
        riskTier: "read",
        inputShape: {
            "status": z.enum(["draft", "validated", "rendered", "deploying", "ready", "failed", "rolling_back", "rolled_back", "cleaned", "cleaned_up"]).nullable().optional(),
            "statuses": z.array(z.enum(["draft", "validated", "rendered", "deploying", "ready", "failed", "rolling_back", "rolled_back", "cleaned", "cleaned_up"])).nullable().optional(),
            "cluster_id": z.string().optional(),
            "query": z.string().optional(),
            "limit": z.number().int().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/deployments/executions" + buildQuery({ "status": args["status"] as string | number | undefined, "statuses": args["statuses"] as string | number | undefined, "cluster_id": args["cluster_id"] as string | number | undefined, "query": args["query"] as string | number | undefined, "limit": args["limit"] as string | number | undefined }))),
    },
    {
        name: "get_deployment_execution",
        description: "Get one deployment execution by execution id. Use this to inspect the current deployment status, namespace, provenance, and endpoint metadata.",
        riskTier: "read",
        inputShape: {
            "execution_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/deployments/executions/${encodeURIComponent(String(args["execution_id"]))}`)),
    },
    {
        name: "patch_deployment_execution",
        description: "Update only the user-owned display metadata on a deployment execution, such as display name or description. Runtime and provenance fields remain immutable.",
        riskTier: "write",
        inputShape: {
            "execution_id": z.string(),
            "display_name": z.string().nullable().optional(),
            "description": z.string().nullable().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/executions/${encodeURIComponent(String(args["execution_id"]))}`, "PATCH", { "display_name": args["display_name"], "description": args["description"] }),
    },
    {
        name: "delete_deployment_execution",
        description: "Delete a deployment execution record and optionally clean up its Kubernetes namespace. Use this for definitive deployment teardown and record removal.",
        riskTier: "approve",
        inputShape: {
            "execution_id": z.string(),
            "delete_namespace": z.boolean().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/executions/${encodeURIComponent(String(args["execution_id"]))}` + buildQuery({ "delete_namespace": args["delete_namespace"] as string | number | undefined }), "DELETE", undefined),
    },
    {
        name: "get_deployment_execution_pods",
        description: "Get live pod status for all pods in a deployment execution\\'s namespace. Use this for Kubernetes-level rollout debugging. A pod phase of Pending usually means the cluster does not yet have free resources to schedule it -- report this to the user immediately as resource-constrained/queued instead of waiting for it to become Running.",
        riskTier: "read",
        inputShape: {
            "execution_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/deployments/executions/${encodeURIComponent(String(args["execution_id"]))}/pods`)),
    },
    {
        name: "get_deployment_pod_logs",
        description: "Get logs for one pod inside a deployment execution\\'s namespace. Optionally scope to one container, tail length, or previous container instance.",
        riskTier: "read",
        inputShape: {
            "execution_id": z.string(),
            "pod_name": z.string(),
            "container": z.string().nullable().optional(),
            "tail": z.number().int().optional(),
            "previous": z.boolean().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/deployments/executions/${encodeURIComponent(String(args["execution_id"]))}/pods/${encodeURIComponent(String(args["pod_name"]))}/logs` + buildQuery({ "container": args["container"] as string | number | undefined, "tail": args["tail"] as string | number | undefined, "previous": args["previous"] as string | number | undefined }))),
    },
    {
        name: "connect_deployment_execution_endpoint",
        description: "Create or reuse a reachable forwarded endpoint for a deployment execution and return the local URL. Use this before calling the model from Playground, Simulation, or evaluation tools.",
        riskTier: "write",
        inputShape: {
            "execution_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/executions/${encodeURIComponent(String(args["execution_id"]))}/endpoint`, "POST", undefined),
    },
    {
        name: "list_cluster_deployments",
        description: "List deployment case records targeting a specific cluster id. Use this when you want a cluster-scoped deployment history rather than execution-centric results.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/deployments/cluster/${encodeURIComponent(String(args["cluster_id"]))}`)),
    },
    {
        name: "list_orphan_deployment_namespaces",
        description: "List namespaces under the deployment prefix with no execution record -- leftovers from records deleted without namespace cleanup, or from a rebuilt database.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/deployments/orphans" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "clean_orphan_deployment_namespaces",
        description: "Delete the given orphan namespaces. Names outside the deployment prefix, or still referenced by a Lens record, are refused rather than deleted.",
        riskTier: "write",
        inputShape: {
            "clusterId": z.string(),
            "namespaces": z.array(z.string()).optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/deployments/orphans/clean", "POST", { "clusterId": args["clusterId"], "namespaces": args["namespaces"] }),
    },
    {
        name: "get_deployment_run",
        description: "Get the full detail of one deployment run, including all cases and their current status. A run or case status of queued (or pending pods reported by get_deployment_execution_pods) usually means the cluster does not yet have free resources -- tell the user it looks resource-constrained and queued right away instead of waiting for it to finish.",
        riskTier: "read",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}`)),
    },
    {
        name: "delete_deployment_run",
        description: "Delete a completed or otherwise deletable deployment run record. Use this when the run history should be removed from Lens.",
        riskTier: "approve",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}`, "DELETE", undefined),
    },
    {
        name: "cancel_deployment_run",
        description: "Request cancellation of an active deployment run. Use this to stop queued or running deployment orchestration work.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cancel`, "POST", undefined),
    },
    {
        name: "rebind_deployment_run_cluster_session",
        description: "Rebind a deployment run to a different active cluster session. Use this after reconnecting to the same cluster so refresh or cleanup actions can continue.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
            "cluster_session_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cluster-session`, "POST", { "cluster_session_id": args["cluster_session_id"] }),
    },
    {
        name: "get_deployment_case",
        description: "Get one deployment case within a deployment run, including its associated execution and deployment summary payloads.",
        riskTier: "read",
        inputShape: {
            "run_id": z.string(),
            "case_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cases/${encodeURIComponent(String(args["case_id"]))}`)),
    },
    {
        name: "refresh_deployment_case",
        description: "Refresh a deployment case\\'s execution record from the live cluster. Use this when rollout state or endpoint details may have changed and the stored record is stale.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
            "case_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cases/${encodeURIComponent(String(args["case_id"]))}/refresh`, "POST", undefined),
    },
    {
        name: "get_deployment_case_logs",
        description: "Get orchestration logs for one deployment case. Use this to inspect deploy-time actions, status changes, and provider output recorded by Lens Deploy.",
        riskTier: "read",
        inputShape: {
            "run_id": z.string(),
            "case_id": z.string(),
            "limit": z.number().int().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cases/${encodeURIComponent(String(args["case_id"]))}/logs` + buildQuery({ "limit": args["limit"] as string | number | undefined }))),
    },
    {
        name: "stop_deployment_case",
        description: "Stop a deployment case and its associated execution. Use this when a single case within a run must be halted without deleting the whole run.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
            "case_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cases/${encodeURIComponent(String(args["case_id"]))}/stop`, "POST", undefined),
    },
    {
        name: "restart_deployment_case",
        description: "Restart a deployment case. Optionally supply a model token override when the case needs a fresh secret or different runtime credential during restart.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
            "case_id": z.string(),
            "model_token": z.string().nullable().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cases/${encodeURIComponent(String(args["case_id"]))}/restart`, "POST", { "model_token": args["model_token"] }),
    },
    {
        name: "clean_deployment_case",
        description: "Clean up one deployment case\\'s runtime resources and marks it cleaned. Use preserve_rendered_overlay when you want to retain rendered overlays for later inspection.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
            "case_id": z.string(),
            "preserve_rendered_overlay": z.boolean().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/deployments/runs/${encodeURIComponent(String(args["run_id"]))}/cases/${encodeURIComponent(String(args["case_id"]))}/clean`, "POST", { "preserve_rendered_overlay": args["preserve_rendered_overlay"] }),
    },
    {
        name: "get_benchmark_defaults",
        description: "Get llm-d-benchmark default repository and runtime settings Lens Evaluate will use. Use this to understand whether benchmarking runs locally or from a managed source checkout.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/evaluate/benchmark-defaults")),
    },
    {
        name: "list_evaluate_runs",
        description: "List one-off benchmark runs Lens Evaluate has recorded.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/evaluate/runs")),
    },
    {
        name: "create_evaluate_run",
        description: "Start a benchmark using the same runner, monitoring, KV collection and analysis as deployment workflows. Set exactly one of deployment_execution_id (a specific deployment) or model_service_group_id (a published Model Service; resolves to one of its currently healthy, authorized members and routes through the cluster's shared Gateway using its published name and api_key, the same path real clients use -- 404 if missing, 409 if no member is currently healthy/authorized). Requires a ready execution and its active cluster session (404 if missing; 409 if not ready or the session is invalid). Requires healthy serving scrapes before traffic, reusing existing targets or enabling monitors. Preparation uses wait_timeout_seconds (600 seconds when unset); failure stops the run before traffic. Reads existing KV instrumentation when supported; never installs probes or redeploys the model. Persists deployment facts and completion-time resources without taking lifecycle ownership of an existing deployment. Returns the queued run with HTTP 202.",
        riskTier: "write",
        inputShape: {
            "harness": z.string().optional(),
            "workload": z.string().optional(),
            "workload_yaml": z.string().nullable().optional().describe("Optional inline workload YAML, scoped to this benchmark run."),
            "parallelism": z.number().int().optional(),
            "wait_timeout_seconds": z.number().int().nullable().optional().describe("Per-invocation execution limit in seconds. Null selects a workload-based automatic budget."),
            "harness_memory_gib": z.number().int().optional().describe("Host memory request and limit in GiB per benchmark worker; independent of model GPU memory."),
            "accelerator_profile": z.string().nullable().optional(),
            "storage_class_name": z.string().nullable().optional(),
            "matrix": z.array(z.object({ "isl": z.number().int().describe("Exact input sequence length in tokens"), "osl": z.number().int().describe("Exact output sequence length in tokens") }).passthrough()).optional(),
            "concurrency_stages": z.array(z.object({ "concurrency": z.number().int(), "num_requests": z.number().int() }).passthrough()).optional(),
            "warmup_requests": z.number().int().optional().describe("Uncounted requests run once before the matrix sweep starts (at the first point's ISL/OSL, concurrency 1) to absorb JIT/torch-compile cold-start cost before the first measured point. 0 disables warm-up. Ignored when matrix is empty."),
            "shared_prefix": z.object({ "num_groups": z.number().int().describe("Number of unique shared system-prompt groups"), "num_prompts_per_group": z.number().int().describe("Requests sharing each group's system prompt"), "system_prompt_len": z.number().int().describe("Shared system-prompt length in tokens"), "question_len": z.number().int().describe("Per-request user question length in tokens"), "output_len": z.number().int().describe("Generated output length in tokens"), "enable_multi_turn_chat": z.boolean().optional(), "stages": z.array(z.object({ "rate": z.number().describe("Target requests/second during this stage"), "duration": z.number().int().describe("Stage duration in seconds") }).passthrough()) }).passthrough().nullable().optional().describe("Optional open-loop, shared-system-prompt workload generator -- an alternative to `matrix` for stressing prefix-cache reuse/offloading. Mutually exclusive with `matrix`."),
            "sla_targets": z.object({ "ttft_ms": z.number().nullable().optional(), "ttft_percentile": z.enum(["p50", "p90", "p95", "p99"]).optional(), "tpot_ms": z.number().nullable().optional(), "tpot_percentile": z.enum(["p50", "p90", "p95", "p99"]).optional(), "throughput_min_tps": z.number().nullable().optional(), "success_rate_min_percent": z.number().nullable().optional() }).passthrough().optional().describe("Optional pass targets attached to one benchmark scenario."),
            "deployment_execution_id": z.string().nullable().optional().describe("A specific deployment execution to benchmark directly. Mutually exclusive with model_service_group_id."),
            "model_service_group_id": z.string().nullable().optional().describe("A published Model Service to benchmark instead of a raw deployment: the run resolves to one of the group's currently healthy, authorized members and calls it by its published name through the cluster's shared Gateway, the same path real clients use. Requires api_key. Mutually exclusive with deployment_execution_id."),
            "cluster_session_id": z.string().nullable().optional(),
            "specification_file": z.string().optional().describe("Benchmark specification; when omitted, resolved from the deployment's guide, with optimized-baseline used for plain vLLM or unknown guides."),
            "http_proxy": z.string().optional(),
            "https_proxy": z.string().optional(),
            "no_proxy": z.string().optional().describe("Comma-separated benchmark proxy bypass entries, merged with backend NO_PROXY and no_proxy plus localhost, 127.0.0.1, .svc, and .cluster.local. Applies to harness Pods and Helm; the benchmark CLI does not inherit external proxies."),
            "benchmark_source": z.object({ "repository": z.string(), "revision": z.string() }).passthrough().nullable().optional(),
            "use_baseline_endpoint": z.boolean().optional().describe("Target the deployment execution's `baseline_url` (a plain Kubernetes Service that bypasses the Guide's routing layer, e.g. round-robin across the same pods) instead of its normal routed `url`. Requires the Guide provider to expose a baseline endpoint; fails clearly otherwise."),
            "api_key": z.string().nullable().optional().describe("Model access token (lens-mk-...) the harness sends as OPENAI_API_KEY when the deployment is reached through the cluster's shared Gateway. Run-only; never persisted."),
        },
        handler: async (args) => jsonRequest("/api/v1/evaluate/runs", "POST", { "harness": args["harness"], "workload": args["workload"], "workload_yaml": args["workload_yaml"], "parallelism": args["parallelism"], "wait_timeout_seconds": args["wait_timeout_seconds"], "harness_memory_gib": args["harness_memory_gib"], "accelerator_profile": args["accelerator_profile"], "storage_class_name": args["storage_class_name"], "matrix": args["matrix"], "concurrency_stages": args["concurrency_stages"], "warmup_requests": args["warmup_requests"], "shared_prefix": args["shared_prefix"], "sla_targets": args["sla_targets"], "deployment_execution_id": args["deployment_execution_id"], "model_service_group_id": args["model_service_group_id"], "cluster_session_id": args["cluster_session_id"], "specification_file": args["specification_file"], "http_proxy": args["http_proxy"], "https_proxy": args["https_proxy"], "no_proxy": args["no_proxy"], "benchmark_source": args["benchmark_source"], "use_baseline_endpoint": args["use_baseline_endpoint"], "api_key": args["api_key"] }),
    },
    {
        name: "estimate_benchmark_timing",
        description: "Preview a configuration-only estimate without reading or creating cluster resources.",
        riskTier: "write",
        inputShape: {
            "harness": z.string().optional(),
            "workload": z.string().optional(),
            "workload_yaml": z.string().nullable().optional().describe("Optional inline workload YAML, scoped to this benchmark run."),
            "parallelism": z.number().int().optional(),
            "wait_timeout_seconds": z.number().int().nullable().optional().describe("Per-invocation execution limit in seconds. Null selects a workload-based automatic budget."),
            "harness_memory_gib": z.number().int().optional().describe("Host memory request and limit in GiB per benchmark worker; independent of model GPU memory."),
            "accelerator_profile": z.string().nullable().optional(),
            "storage_class_name": z.string().nullable().optional(),
            "matrix": z.array(z.object({ "isl": z.number().int().describe("Exact input sequence length in tokens"), "osl": z.number().int().describe("Exact output sequence length in tokens") }).passthrough()).optional(),
            "concurrency_stages": z.array(z.object({ "concurrency": z.number().int(), "num_requests": z.number().int() }).passthrough()).optional(),
            "warmup_requests": z.number().int().optional().describe("Uncounted requests run once before the matrix sweep starts (at the first point's ISL/OSL, concurrency 1) to absorb JIT/torch-compile cold-start cost before the first measured point. 0 disables warm-up. Ignored when matrix is empty."),
            "shared_prefix": z.object({ "num_groups": z.number().int().describe("Number of unique shared system-prompt groups"), "num_prompts_per_group": z.number().int().describe("Requests sharing each group's system prompt"), "system_prompt_len": z.number().int().describe("Shared system-prompt length in tokens"), "question_len": z.number().int().describe("Per-request user question length in tokens"), "output_len": z.number().int().describe("Generated output length in tokens"), "enable_multi_turn_chat": z.boolean().optional(), "stages": z.array(z.object({ "rate": z.number().describe("Target requests/second during this stage"), "duration": z.number().int().describe("Stage duration in seconds") }).passthrough()) }).passthrough().nullable().optional().describe("Optional open-loop, shared-system-prompt workload generator -- an alternative to `matrix` for stressing prefix-cache reuse/offloading. Mutually exclusive with `matrix`."),
        },
        handler: async (args) => jsonRequest("/api/v1/evaluate/timing-estimate", "POST", { "harness": args["harness"], "workload": args["workload"], "workload_yaml": args["workload_yaml"], "parallelism": args["parallelism"], "wait_timeout_seconds": args["wait_timeout_seconds"], "harness_memory_gib": args["harness_memory_gib"], "accelerator_profile": args["accelerator_profile"], "storage_class_name": args["storage_class_name"], "matrix": args["matrix"], "concurrency_stages": args["concurrency_stages"], "warmup_requests": args["warmup_requests"], "shared_prefix": args["shared_prefix"] }),
    },
    {
        name: "list_evaluation_workflows",
        description: "List evaluation workflow runs Lens has recorded, including multi-configuration evaluations and simple deployment-plus-benchmark workflows.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/evaluate/workflow-runs")),
    },
    {
        name: "create_evaluate_workflow_run",
        description: "Start a simple deployment-plus-benchmark workflow that first creates an optimized baseline deployment, then benchmarks it. Use this for quick end-to-end workflow tests.",
        riskTier: "write",
        inputShape: {
            "model": z.string(),
            "cluster_session_id": z.string(),
            "replicas": z.number().int().optional(),
            "tensor_parallel_size": z.number().int().optional(),
            "image": z.string().optional(),
            "harness": z.string().optional(),
            "workload": z.string().optional(),
            "parallelism": z.number().int().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/evaluate/workflow-runs", "POST", { "model": args["model"], "cluster_session_id": args["cluster_session_id"], "replicas": args["replicas"], "tensor_parallel_size": args["tensor_parallel_size"], "image": args["image"], "harness": args["harness"], "workload": args["workload"], "parallelism": args["parallelism"] }),
    },
    {
        name: "create_evaluation",
        description: "Queue deployment, benchmarking and comparison of published configuration artifacts. Independently deployed PD baselines default to the total prefill plus decode GPU budget divided by the effective baseline TP (source decode TP unless overridden). Explicit baseline replicas take precedence, including unequal budgets; same-pod Kubernetes Service comparisons do not create replicas. Returns 409 before queueing if an automatic PD budget cannot be represented exactly with 1-32 replicas; existing evaluation snapshots are unchanged.",
        riskTier: "write",
        inputShape: {
            "name": z.string().optional(),
            "cluster_session_id": z.string(),
            "runtime": z.object({ "http_proxy": z.string().optional(), "https_proxy": z.string().optional(), "no_proxy": z.string().optional().describe("Comma-separated benchmark proxy bypass entries, merged with backend NO_PROXY and no_proxy plus localhost, 127.0.0.1, .svc, and .cluster.local. Applies to harness Pods and Helm; the benchmark CLI does not inherit external proxies.") }).passthrough().optional(),
            "benchmark_source": z.object({ "repository": z.string(), "revision": z.string() }).passthrough().nullable().optional(),
            "benchmark_plans": z.array(z.object({ "id": z.string(), "configuration_artifact_id": z.string(), "deployment_name": z.string().nullable().optional(), "deployment_description": z.string().nullable().optional(), "benchmark": z.object({ "harness": z.string().optional(), "workload": z.string().optional(), "workload_yaml": z.string().nullable().optional().describe("Optional inline workload YAML, scoped to this benchmark run."), "parallelism": z.number().int().optional(), "wait_timeout_seconds": z.number().int().nullable().optional().describe("Per-invocation execution limit in seconds. Null selects a workload-based automatic budget."), "harness_memory_gib": z.number().int().optional().describe("Host memory request and limit in GiB per benchmark worker; independent of model GPU memory."), "accelerator_profile": z.string().nullable().optional(), "storage_class_name": z.string().nullable().optional(), "matrix": z.array(z.object({ "isl": z.number().int().describe("Exact input sequence length in tokens"), "osl": z.number().int().describe("Exact output sequence length in tokens") }).passthrough()).optional(), "concurrency_stages": z.array(z.object({ "concurrency": z.number().int(), "num_requests": z.number().int() }).passthrough()).optional(), "warmup_requests": z.number().int().optional().describe("Uncounted requests run once before the matrix sweep starts (at the first point's ISL/OSL, concurrency 1) to absorb JIT/torch-compile cold-start cost before the first measured point. 0 disables warm-up. Ignored when matrix is empty."), "shared_prefix": z.object({ "num_groups": z.number().int().describe("Number of unique shared system-prompt groups"), "num_prompts_per_group": z.number().int().describe("Requests sharing each group's system prompt"), "system_prompt_len": z.number().int().describe("Shared system-prompt length in tokens"), "question_len": z.number().int().describe("Per-request user question length in tokens"), "output_len": z.number().int().describe("Generated output length in tokens"), "enable_multi_turn_chat": z.boolean().optional(), "stages": z.array(z.object({ "rate": z.number().describe("Target requests/second during this stage"), "duration": z.number().int().describe("Stage duration in seconds") }).passthrough()) }).passthrough().nullable().optional().describe("Optional open-loop, shared-system-prompt workload generator -- an alternative to `matrix` for stressing prefix-cache reuse/offloading. Mutually exclusive with `matrix`.") }).passthrough().optional(), "include_configuration": z.boolean().optional(), "include_baseline": z.boolean().optional(), "baseline_type": z.enum(["direct-vllm", "router-neutral", "router-round-robin", "load-only", "affinity-only", "optimized-baseline", "kubernetes-service"]).optional(), "baseline_types": z.array(z.enum(["direct-vllm", "router-neutral", "router-round-robin", "load-only", "affinity-only", "optimized-baseline", "kubernetes-service"])).optional(), "baseline_parameters": z.record(z.string(), z.unknown()).optional(), "preserve_deployment": z.boolean().optional(), "scenarios": z.array(z.object({ "id": z.string(), "name": z.string(), "description": z.string().optional(), "benchmark": z.object({ "harness": z.string().optional(), "workload": z.string().optional(), "workload_yaml": z.string().nullable().optional().describe("Optional inline workload YAML, scoped to this benchmark run."), "parallelism": z.number().int().optional(), "wait_timeout_seconds": z.number().int().nullable().optional().describe("Per-invocation execution limit in seconds. Null selects a workload-based automatic budget."), "harness_memory_gib": z.number().int().optional().describe("Host memory request and limit in GiB per benchmark worker; independent of model GPU memory."), "accelerator_profile": z.string().nullable().optional(), "storage_class_name": z.string().nullable().optional(), "matrix": z.array(z.object({ "isl": z.number().int().describe("Exact input sequence length in tokens"), "osl": z.number().int().describe("Exact output sequence length in tokens") }).passthrough()).optional(), "concurrency_stages": z.array(z.object({ "concurrency": z.number().int(), "num_requests": z.number().int() }).passthrough()).optional(), "warmup_requests": z.number().int().optional().describe("Uncounted requests run once before the matrix sweep starts (at the first point's ISL/OSL, concurrency 1) to absorb JIT/torch-compile cold-start cost before the first measured point. 0 disables warm-up. Ignored when matrix is empty."), "shared_prefix": z.object({ "num_groups": z.number().int().describe("Number of unique shared system-prompt groups"), "num_prompts_per_group": z.number().int().describe("Requests sharing each group's system prompt"), "system_prompt_len": z.number().int().describe("Shared system-prompt length in tokens"), "question_len": z.number().int().describe("Per-request user question length in tokens"), "output_len": z.number().int().describe("Generated output length in tokens"), "enable_multi_turn_chat": z.boolean().optional(), "stages": z.array(z.object({ "rate": z.number().describe("Target requests/second during this stage"), "duration": z.number().int().describe("Stage duration in seconds") }).passthrough()) }).passthrough().nullable().optional().describe("Optional open-loop, shared-system-prompt workload generator -- an alternative to `matrix` for stressing prefix-cache reuse/offloading. Mutually exclusive with `matrix`.") }).passthrough().optional(), "sla_targets": z.object({ "ttft_ms": z.number().nullable().optional(), "ttft_percentile": z.enum(["p50", "p90", "p95", "p99"]).optional(), "tpot_ms": z.number().nullable().optional(), "tpot_percentile": z.enum(["p50", "p90", "p95", "p99"]).optional(), "throughput_min_tps": z.number().nullable().optional(), "success_rate_min_percent": z.number().nullable().optional() }).passthrough().optional().describe("Optional pass targets attached to one benchmark scenario.") }).passthrough()).optional() }).passthrough()),
            "compare_configurations": z.boolean().optional().describe("When multiple candidate configurations are in one Evaluation, generate pairwise absolute/matrix comparison rows in addition to each candidate's explicit baselines."),
        },
        handler: async (args) => jsonRequest("/api/v1/evaluate/evaluations", "POST", { "name": args["name"], "cluster_session_id": args["cluster_session_id"], "runtime": args["runtime"], "benchmark_source": args["benchmark_source"], "benchmark_plans": args["benchmark_plans"], "compare_configurations": args["compare_configurations"] }),
    },
    {
        name: "retry_evaluation_workflow",
        description: "Retry a failed or cancelled multi-workload evaluation workflow. Use this after fixing a transient cluster or runtime problem.",
        riskTier: "write",
        inputShape: {
            "workflow_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(String(args["workflow_id"]))}/retry`, "POST", undefined),
    },
    {
        name: "cancel_evaluation_case",
        description: "Stop the selected case and linked cases that depend on or share its owned deployment, cancel their benchmarks, and clean up evaluation-owned deployments. Borrowed endpoints are preserved. Unrelated pending cases can continue. A 409 response means deployment cleanup needs to be retried.",
        riskTier: "write",
        inputShape: {
            "workflow_id": z.string(),
            "case_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(String(args["workflow_id"]))}/cases/${encodeURIComponent(String(args["case_id"]))}/cancel`, "POST", undefined),
    },
    {
        name: "cancel_evaluation_workflow",
        description: "Cancel the workflow and its child benchmarks, then clean up evaluation-owned deployments while preserving borrowed endpoints. Returns cancelled after successful cleanup; a 409 response means cleanup must be retried.",
        riskTier: "write",
        inputShape: {
            "workflow_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(String(args["workflow_id"]))}/cancel`, "POST", undefined),
    },
    {
        name: "delete_evaluation_workflow",
        description: "Delete a terminal evaluation workflow and any retained child benchmark records it owns. Use this after the workflow is finished and its history is no longer needed.",
        riskTier: "approve",
        inputShape: {
            "workflow_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(String(args["workflow_id"]))}`, "DELETE", undefined),
    },
    {
        name: "get_evaluation_workflow",
        description: "Get the current top-level record for one evaluation workflow run.",
        riskTier: "read",
        inputShape: {
            "workflow_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(String(args["workflow_id"]))}`)),
    },
    {
        name: "delete_evaluate_run",
        description: "Delete a completed, failed, or cancelled standalone benchmark and its saved results directory. Active runs and runs linked to an evaluation workflow return 409; delete the owning workflow to remove its benchmark history.",
        riskTier: "approve",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/evaluate/runs/${encodeURIComponent(String(args["run_id"]))}`, "DELETE", undefined),
    },
    {
        name: "get_evaluate_run",
        description: "Return the benchmark record with its existing top-level fields plus saved configuration/model/namespace and deployment_cases for read-only live links. Terminal results use the same client-metric, request-evidence, capacity, mechanism and diagnosis enrichment as workflow cases. Resource snapshots are captured at benchmark completion, not reconstructed from live resources on GET. Legacy runs can recover configuration and derived results when source evidence still exists, but missing historical telemetry or KV traces are not fabricated. Invalid identifiers return 422 and missing runs return 404. This endpoint does not grant deployment lifecycle ownership.",
        riskTier: "read",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/evaluate/runs/${encodeURIComponent(String(args["run_id"]))}`)),
    },
    {
        name: "get_evaluation_workflow_details",
        description: "Return workflow cases, linked benchmark records, deployment evidence and comparison reports. Benchmark records and case results use the same client-metric, request-evidence, capacity, mechanism and diagnosis pipeline as standalone runs. Case resource snapshots are saved benchmark-completion evidence; deployment_cases may additionally contain current Kubernetes snapshots. Missing historical telemetry is not reconstructed from current samples. Missing workflows return 404; malformed identifiers return 422.",
        riskTier: "read",
        inputShape: {
            "workflow_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(String(args["workflow_id"]))}/details`)),
    },
    {
        name: "cancel_evaluate_run",
        description: "Cancel an active one-off benchmark run. Use this when a benchmark should stop before its normal completion.",
        riskTier: "write",
        inputShape: {
            "run_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/evaluate/runs/${encodeURIComponent(String(args["run_id"]))}/cancel`, "POST", undefined),
    },
    {
        name: "list_hardware_capabilities",
        description: "Return every registered hardware profile, built-in and plugin, with a content-hash version so gateway and browser clients can cache safely.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/hardware/capabilities")),
    },
    {
        name: "get_cluster_stack_status",
        description: "Check whether the cluster-level Prometheus/Grafana monitoring stack (kube-prometheus-stack) is installed and ready.",
        riskTier: "read",
        inputShape: {
            "namespace": z.string().optional(),
            "cluster_id": z.string().nullable().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/monitoring/cluster-stack/status" + buildQuery({ "namespace": args["namespace"] as string | number | undefined, "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "preflight_cluster_stack_install",
        description: "Run cluster monitoring stack preflight checks without installing anything. Use this before starting a Prometheus/Grafana stack installation.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string().nullable().optional(),
            "namespace": z.string().optional(),
            "mode": z.enum(["central", "individual"]).optional(),
            "enable_tls": z.boolean().optional(),
            "reinstall": z.boolean().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/monitoring/cluster-stack/installations/preflight" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }), "POST", { "namespace": args["namespace"], "mode": args["mode"], "enable_tls": args["enable_tls"], "reinstall": args["reinstall"] }),
    },
    {
        name: "install_cluster_stack",
        description: "Install or reinstall the cluster-level monitoring stack. Use this when Prometheus/Grafana cluster monitoring should be provisioned or repaired.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string().nullable().optional(),
            "namespace": z.string().optional(),
            "mode": z.enum(["central", "individual"]).optional(),
            "enable_tls": z.boolean().optional(),
            "reinstall": z.boolean().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/monitoring/cluster-stack/installations" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }), "POST", { "namespace": args["namespace"], "mode": args["mode"], "enable_tls": args["enable_tls"], "reinstall": args["reinstall"] }),
    },
    {
        name: "get_cluster_stack_operation",
        description: "Get the status and logs of one cluster monitoring stack installation operation.",
        riskTier: "read",
        inputShape: {
            "operation_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/monitoring/cluster-stack/operations/${encodeURIComponent(String(args["operation_id"]))}`)),
    },
    {
        name: "get_cluster_stack_links",
        description: "Get resolved Grafana and Prometheus links for the cluster monitoring stack.",
        riskTier: "read",
        inputShape: {
            "namespace": z.string().optional(),
            "cluster_id": z.string().nullable().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/monitoring/cluster-stack/links" + buildQuery({ "namespace": args["namespace"] as string | number | undefined, "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "list_deployment_monitoring",
        description: "List deployment monitoring status for every deployment in a cluster. Use this to discover which executions have observability installed, enabled, or unhealthy.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/monitoring/deployments" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "get_deployment_monitoring",
        description: "Get monitoring status for one deployment execution, optionally scoped to a cluster id.",
        riskTier: "read",
        inputShape: {
            "execution_id": z.string(),
            "cluster_id": z.string().nullable().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/monitoring/deployments/${encodeURIComponent(String(args["execution_id"]))}` + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "manage_deployment_monitoring",
        description: "Install, enable, disable, or uninstall monitoring resources for one deployment execution.",
        riskTier: "write",
        inputShape: {
            "execution_id": z.string(),
            "action": z.string(),
            "cluster_id": z.string().nullable().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/monitoring/deployments/${encodeURIComponent(String(args["execution_id"]))}/${encodeURIComponent(String(args["action"]))}` + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }), "POST", undefined),
    },
    {
        name: "get_flow_map_metrics",
        description: "Query the Prometheus-backed GPU idle/utilization flow-map for a specific deployment execution",
        riskTier: "read",
        inputShape: {
            "execution_id": z.string(),
            "cluster_id": z.string().nullable().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/monitoring/profiling/flow-map" + buildQuery({ "execution_id": args["execution_id"] as string | number | undefined, "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "list_monitoring_accelerators",
        description: "List accelerator observability capabilities Lens supports, including access modes and default namespaces.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/monitoring/accelerators")),
    },
    {
        name: "get_accelerator_monitoring_status",
        description: "Get observability status for one accelerator family, including release, component, and access-mode health.",
        riskTier: "read",
        inputShape: {
            "accelerator": z.string(),
            "namespace": z.string().optional(),
            "access_mode": z.enum(["dra", "plugin"]).nullable().optional(),
            "cluster_id": z.string().nullable().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/monitoring/accelerators/${encodeURIComponent(String(args["accelerator"]))}/status` + buildQuery({ "namespace": args["namespace"] as string | number | undefined, "access_mode": args["access_mode"] as string | number | undefined, "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "preflight_accelerator_install",
        description: "Run accelerator observability installation preflight checks without mutating the cluster. Use this before starting a real install.",
        riskTier: "write",
        inputShape: {
            "accelerator": z.string(),
            "cluster_id": z.string().nullable().optional(),
            "access_mode": z.enum(["dra", "plugin"]).optional(),
            "namespace": z.string().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/monitoring/accelerators/${encodeURIComponent(String(args["accelerator"]))}/installations/preflight` + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }), "POST", { "accelerator": args["accelerator"], "access_mode": args["access_mode"], "namespace": args["namespace"] }),
    },
    {
        name: "install_accelerator_monitoring",
        description: "Install accelerator observability components for a supported accelerator family.",
        riskTier: "write",
        inputShape: {
            "accelerator": z.string(),
            "cluster_id": z.string().nullable().optional(),
            "access_mode": z.enum(["dra", "plugin"]).optional(),
            "namespace": z.string().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/monitoring/accelerators/${encodeURIComponent(String(args["accelerator"]))}/installations` + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }), "POST", { "accelerator": args["accelerator"], "access_mode": args["access_mode"], "namespace": args["namespace"] }),
    },
    {
        name: "get_accelerator_monitoring_operation",
        description: "Get the status and logs of one accelerator monitoring installation operation.",
        riskTier: "read",
        inputShape: {
            "accelerator": z.string(),
            "operation_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/monitoring/accelerators/${encodeURIComponent(String(args["accelerator"]))}/operations/${encodeURIComponent(String(args["operation_id"]))}`)),
    },
    {
        name: "get_accelerator_monitoring_links",
        description: "Get observability links Lens can resolve for an accelerator monitoring installation, such as Grafana or Prometheus URLs.",
        riskTier: "read",
        inputShape: {
            "accelerator": z.string(),
            "cluster_id": z.string().nullable().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/monitoring/accelerators/${encodeURIComponent(String(args["accelerator"]))}/links` + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "get_gpu_driver_status",
        description: "Get the current GPU driver installation status for a cluster, including readiness and access mode.",
        riskTier: "read",
        inputShape: {
            "access_mode": z.enum(["dra", "plugin"]).optional(),
            "cluster_id": z.string().nullable().optional(),
            "hardware": z.string().nullable().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/monitoring/gpu-driver/status" + buildQuery({ "access_mode": args["access_mode"] as string | number | undefined, "cluster_id": args["cluster_id"] as string | number | undefined, "hardware": args["hardware"] as string | number | undefined }))),
    },
    {
        name: "install_gpu_driver",
        description: "Install the GPU driver/device-plugin stack Lens expects for the selected access mode.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string().nullable().optional(),
            "hardware": z.string().nullable().optional(),
            "access_mode": z.enum(["dra", "plugin"]).optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/monitoring/gpu-driver/install" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined, "hardware": args["hardware"] as string | number | undefined }), "POST", { "access_mode": args["access_mode"] }),
    },
    {
        name: "list_storage_volumes",
        description: "List Lens storage volumes with optional filters for cluster, kind, readiness, purpose, or name query.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string().optional(),
            "kind": z.string().optional(),
            "status": z.string().optional(),
            "purpose": z.string().optional(),
            "query": z.string().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/storage/volumes" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined, "kind": args["kind"] as string | number | undefined, "status": args["status"] as string | number | undefined, "purpose": args["purpose"] as string | number | undefined, "query": args["query"] as string | number | undefined }))),
    },
    {
        name: "create_storage_volume",
        description: "Create and provision a new Lens storage volume. Use this before deployments or Model Cache flows that require a registered shared volume.",
        riskTier: "write",
        inputShape: {
            "clusterId": z.string(),
            "name": z.string().optional(),
            "kind": z.enum(["local-disk", "nfs", "dynamic-pvc"]),
            "capacity": z.string(),
            "readOnly": z.boolean().optional(),
            "purposes": z.array(z.enum(["model-cache"])).optional(),
            "localDisk": z.object({ "hostPath": z.string() }).passthrough().nullable().optional(),
            "nfs": z.object({ "server": z.string(), "path": z.string() }).passthrough().nullable().optional(),
            "dynamicPvc": z.object({ "storageClass": z.string(), "namespace": z.string(), "accessMode": z.string().optional() }).passthrough().nullable().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/storage/volumes", "POST", { "clusterId": args["clusterId"], "name": args["name"], "kind": args["kind"], "capacity": args["capacity"], "readOnly": args["readOnly"], "purposes": args["purposes"], "localDisk": args["localDisk"], "nfs": args["nfs"], "dynamicPvc": args["dynamicPvc"] }),
    },
    {
        name: "get_storage_volume",
        description: "Get one Lens storage volume by id, including drift and usage metadata.",
        riskTier: "read",
        inputShape: {
            "volume_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/storage/volumes/${encodeURIComponent(String(args["volume_id"]))}`)),
    },
    {
        name: "delete_storage_volume",
        description: "Delete a Lens storage volume registration and, where relevant, its Kubernetes resources. Any Model Cache entries on the volume are deleted too -- pass keep_model_files=true to remove only their records and leave the model files on the underlying NFS/hostPath storage untouched, or omit it (false) to actually delete those files first. Use this only when the volume is no longer needed.",
        riskTier: "approve",
        inputShape: {
            "volume_id": z.string(),
            "keep_model_files": z.boolean().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/storage/volumes/${encodeURIComponent(String(args["volume_id"]))}` + buildQuery({ "keep_model_files": args["keep_model_files"] as string | number | undefined }), "DELETE", undefined),
    },
    {
        name: "acknowledge_storage_volume_nodes",
        description: "Acknowledge newly added nodes for a local-disk storage volume after you have replicated its contents. This clears node-drift warnings in Lens.",
        riskTier: "write",
        inputShape: {
            "volume_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/storage/volumes/${encodeURIComponent(String(args["volume_id"]))}/acknowledge-nodes`, "POST", undefined),
    },
    {
        name: "scan_storage_volume_models",
        description: "Re-scan a Lens storage volume for model files already present on disk and auto-register Model Cache entries for any that are found. Use this after copying model files onto the volume outside of Lens.",
        riskTier: "write",
        inputShape: {
            "volume_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/storage/volumes/${encodeURIComponent(String(args["volume_id"]))}/scan-models`, "POST", undefined),
    },
    {
        name: "list_storage_classes",
        description: "List Kubernetes StorageClasses visible to a cluster. Use this before creating dynamic-pvc storage volumes.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/storage/storage-classes" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "list_storage_nodes",
        description: "List cluster nodes as seen by the Storage module. Use this before creating local-disk storage volumes that assume replicated host paths across nodes.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/storage/nodes" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }))),
    },
    {
        name: "list_model_cache_entries",
        description: "List Model Cache entries, optionally filtered by cluster or storage volume. Use this to discover cached model ids, download state, and node sync drift.",
        riskTier: "read",
        inputShape: {
            "cluster_id": z.string().optional(),
            "storage_volume_id": z.string().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/model-cache/entries" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined, "storage_volume_id": args["storage_volume_id"] as string | number | undefined }))),
    },
    {
        name: "create_model_cache_entry",
        description: "Request a model download into Lens Model Cache. Use this to seed a ready shared model cache before deployments or benchmarks.",
        riskTier: "write",
        inputShape: {
            "clusterId": z.string(),
            "storageVolumeId": z.string(),
            "source": z.object({ "kind": z.enum(["huggingface", "model-catalog"]), "huggingface": z.object({ "repoId": z.string(), "revision": z.string().optional() }).passthrough().nullable().optional(), "modelCatalog": z.object({ "catalogEntryId": z.string() }).passthrough().nullable().optional() }).passthrough(),
            "tokenSource": z.object({ "mode": z.enum(["none", "existing-secret", "host"]).optional(), "namespace": z.string().nullable().optional(), "name": z.string().nullable().optional() }).passthrough().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/model-cache/entries", "POST", { "clusterId": args["clusterId"], "storageVolumeId": args["storageVolumeId"], "source": args["source"], "tokenSource": args["tokenSource"] }),
    },
    {
        name: "get_model_cache_entry",
        description: "Get one Model Cache entry by id, including source, token mode, node progress, and pending sync nodes.",
        riskTier: "read",
        inputShape: {
            "entry_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/model-cache/entries/${encodeURIComponent(String(args["entry_id"]))}`)),
    },
    {
        name: "delete_model_cache_entry",
        description: "Delete a Model Cache entry and, when required, schedule cleanup of its downloaded files. Set keep_files=true to remove the cache record only, leaving model files on the underlying storage untouched.",
        riskTier: "approve",
        inputShape: {
            "entry_id": z.string(),
            "keep_files": z.boolean().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/model-cache/entries/${encodeURIComponent(String(args["entry_id"]))}` + buildQuery({ "keep_files": args["keep_files"] as string | number | undefined }), "DELETE", undefined),
    },
    {
        name: "retry_model_cache_entry",
        description: "Retry a failed or retryable Model Cache entry download.",
        riskTier: "write",
        inputShape: {
            "entry_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/model-cache/entries/${encodeURIComponent(String(args["entry_id"]))}/retry`, "POST", undefined),
    },
    {
        name: "sync_model_cache_nodes",
        description: "Trigger sync for all local-disk Model Cache entries that are missing on newly added nodes. Optionally scope to one cluster.",
        riskTier: "write",
        inputShape: {
            "cluster_id": z.string().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/model-cache/entries/sync-nodes" + buildQuery({ "cluster_id": args["cluster_id"] as string | number | undefined }), "POST", undefined),
    },
    {
        name: "sync_model_cache_entry_nodes",
        description: "Trigger node sync for one specific Model Cache entry. Use this when a single cached model needs to be replicated onto newly added nodes.",
        riskTier: "write",
        inputShape: {
            "entry_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/model-cache/entries/${encodeURIComponent(String(args["entry_id"]))}/sync-nodes`, "POST", undefined),
    },
    {
        name: "get_model_cache_entry_logs",
        description: "Get logs for one Model Cache entry, optionally scoped to a specific node. Use this to debug download or cleanup failures.",
        riskTier: "read",
        inputShape: {
            "entry_id": z.string(),
            "node": z.string().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/model-cache/entries/${encodeURIComponent(String(args["entry_id"]))}/logs` + buildQuery({ "node": args["node"] as string | number | undefined }))),
    },
    {
        name: "search_huggingface_models",
        description: "Search Hugging Face model repositories from Lens Model Cache. Use this to discover repo ids before creating a cache entry.",
        riskTier: "read",
        inputShape: {
            "query": z.string().optional(),
            "limit": z.number().int().optional(),
        },
        handler: async (args) => toPlainResult(await internalRequest("/api/v1/model-cache/huggingface/search" + buildQuery({ "query": args["query"] as string | number | undefined, "limit": args["limit"] as string | number | undefined }))),
    },
    {
        name: "get_huggingface_model",
        description: "Get detailed Hugging Face metadata for one model repository, including the README snapshot Lens fetched.",
        riskTier: "read",
        inputShape: {
            "repo_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/model-cache/huggingface/models/${encodeURIComponent(String(args["repo_id"]))}`)),
    },
    {
        name: "list_ai_providers",
        description: "List the saved external AI provider connections Lens knows about. Returned records mask stored API keys and expose only a safe preview.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/ai-providers")),
    },
    {
        name: "create_ai_provider",
        description: "Create and persist a new external AI provider configuration. Use this for durable provider definitions Lens may later reuse for agentic planning or other integrations.",
        riskTier: "write",
        inputShape: {
            "name": z.string(),
            "baseUrl": z.string(),
            "model": z.string(),
            "apiKey": z.string().optional(),
            "providerType": z.enum(["openai", "anthropic"]).optional(),
            "timeoutSeconds": z.number().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/ai-providers", "POST", { "name": args["name"], "baseUrl": args["baseUrl"], "model": args["model"], "apiKey": args["apiKey"], "providerType": args["providerType"], "timeoutSeconds": args["timeoutSeconds"] }),
    },
    {
        name: "get_ai_provider",
        description: "Get one saved external AI provider connection by id. The response masks the stored API key and includes only safe metadata.",
        riskTier: "read",
        inputShape: {
            "provider_id": z.string(),
        },
        handler: async (args) => toPlainResult(await internalRequest(`/api/v1/ai-providers/${encodeURIComponent(String(args["provider_id"]))}`)),
    },
    {
        name: "update_ai_provider",
        description: "Update an existing external AI provider configuration by id. Omit apiKey to keep the stored key unchanged, or pass an empty string to clear it.",
        riskTier: "write",
        inputShape: {
            "provider_id": z.string(),
            "name": z.string().nullable().optional(),
            "baseUrl": z.string().nullable().optional(),
            "model": z.string().nullable().optional(),
            "apiKey": z.string().nullable().optional(),
            "providerType": z.enum(["openai", "anthropic"]).nullable().optional(),
            "timeoutSeconds": z.number().nullable().optional(),
        },
        handler: async (args) => jsonRequest(`/api/v1/ai-providers/${encodeURIComponent(String(args["provider_id"]))}`, "PUT", { "name": args["name"], "baseUrl": args["baseUrl"], "model": args["model"], "apiKey": args["apiKey"], "providerType": args["providerType"], "timeoutSeconds": args["timeoutSeconds"] }),
    },
    {
        name: "delete_ai_provider",
        description: "Delete a saved external AI provider configuration. Use this when a provider should no longer be available to Lens.",
        riskTier: "approve",
        inputShape: {
            "provider_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/ai-providers/${encodeURIComponent(String(args["provider_id"]))}`, "DELETE", undefined),
    },
    {
        name: "test_ai_provider_draft_connection",
        description: "Test connectivity for a not-yet-saved external AI provider configuration. Use this before creating or updating a durable provider entry.",
        riskTier: "write",
        inputShape: {
            "baseUrl": z.string(),
            "model": z.string(),
            "apiKey": z.string().optional(),
            "providerType": z.enum(["openai", "anthropic"]).optional(),
            "timeoutSeconds": z.number().optional(),
        },
        handler: async (args) => jsonRequest("/api/v1/ai-providers/test", "POST", { "baseUrl": args["baseUrl"], "model": args["model"], "apiKey": args["apiKey"], "providerType": args["providerType"], "timeoutSeconds": args["timeoutSeconds"] }),
    },
    {
        name: "test_ai_provider_connection",
        description: "Test connectivity for an already-saved external AI provider by id, reusing its stored API key. Use this to validate an existing provider definition.",
        riskTier: "write",
        inputShape: {
            "provider_id": z.string(),
        },
        handler: async (args) => jsonRequest(`/api/v1/ai-providers/${encodeURIComponent(String(args["provider_id"]))}/test`, "POST", undefined),
    },
    {
        name: "proxy_ai_provider_chat_completions",
        description: "Forward an OpenAI-compatible chat completion body to a saved openai-type provider using its server-held credentials. Defaults to the saved model if omitted and returns the provider JSON response. Other provider types are rejected.",
        riskTier: "write",
        inputShape: {
            "provider_id": z.string(),
            body: z.unknown().describe('Request body for this operation.'),
        },
        handler: async (args) => jsonRequest(`/api/v1/ai-providers/${encodeURIComponent(String(args["provider_id"]))}/chat/completions`, "POST", { "body": args["body"] }),
    },
    {
        name: "get_supported_versions",
        description: "Get the pinned llm-d component versions Lens supports (llm-d, llm-d-router, llm-d-benchmark, inference payload processor, Gateway API/GIE CRDs and gateway providers). Read-only; the wizard and API always use these values.",
        riskTier: "read",
        inputShape: {
        },
        handler: async () => toPlainResult(await internalRequest("/api/v1/versions")),
    },
];

// Both halves of the catalog live in the SAME array (declared in
// specialTools.ts) so hand-written tools like wait_for_status can look up any
// tool, generated or hand-written, by name without a circular import.
prismTools.push(...generatedTools);

export { prismTools, findTool };
