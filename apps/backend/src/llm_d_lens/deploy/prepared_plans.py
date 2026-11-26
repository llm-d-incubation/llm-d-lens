"""Prepare immutable deployment inputs without creating a Run or cluster resources."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import yaml

from llm_d_lens.cluster import require_active_session
from llm_d_lens.configuration.disaggregated_set import (
    disaggregated_sets,
    workload_pods,
    workload_summary,
)
from llm_d_lens.db.dao.deployment_plan import DeploymentPlanDao
from llm_d_lens.deploy.contracts import DeploymentPreparedPlan, DeploymentRun
from llm_d_lens.deploy.placement_preflight import max_feasible_slices, placement_preflight, slice_capacity
from llm_d_lens.deploy.plan_service import DeploymentPlanService
from llm_d_lens.deploy.providers.deployment_bundle import validate_review_resources
from llm_d_lens.deploy.providers.guide_adapter import GuideDeploymentArtifact
from llm_d_lens.deploy.slice_capacity import (
    ClusterInventoryAuthError,
    ClusterInventoryForbiddenError,
    classify_kubectl_failure,
    fetch_cluster_nodes_and_pods,
    is_unauthorized_result,
)
from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum, text_checksum
from llm_d_lens.infrastructure.artifacts.store import register_artifacts
from llm_d_lens.infrastructure.kubernetes import scoped_runner
from llm_d_lens.infrastructure.storage.paths import storage_path


def requires_prepared_plan(configurations) -> bool:
    for configuration in configurations:
        content = configuration.content
        manifest = (content.get("officialGuide") or {}).get("renderedManifest") or ""
        if (
            content.get("comparisonBudget")
            or content.get("disaggregatedSet")
            or disaggregated_sets(list(yaml.safe_load_all(manifest)))
        ):
            return True
    return False


def request_identity(request) -> str:
    value = request.model_dump(mode="json", exclude={"prepared_plan_id", "expected_plan_digest", "acknowledge_unknown"})
    value["provenance"].pop("cluster_session_id", None)
    value["provenance"].pop("target_namespace", None)
    if value.get("runtime_binding"):
        value["runtime_binding"].pop("cluster_session_id", None)
    return configuration_checksum(value)


async def inventory(cluster_id: str, resource: str, namespace: str | None = None) -> list[dict]:
    command = ["kubectl", "get", resource, "-o", "json"]
    if namespace:
        command.extend(["--namespace", namespace])
    elif resource not in {"nodes", "customresourcedefinitions", "runtimeclasses", "namespaces", "persistentvolumes"}:
        command.append("--all-namespaces")
    result = await scoped_runner(cluster_id).run(command, timeout=30)
    if result.returncode:
        classify_kubectl_failure(result, resource=resource)
        raise ClusterInventoryForbiddenError(f"Cluster inventory access denied (403) for {resource}")
    response = json.loads(result.stdout)
    if not isinstance(response.get("items"), list) or response.get("metadata", {}).get("continue"):
        raise ValueError(f"Cluster inventory incomplete for {resource}")
    return response["items"]


async def _api_group_available(cluster_id: str, path: str) -> bool:
    result = await scoped_runner(cluster_id).run(["kubectl", "get", "--raw", path], timeout=15)
    if result.returncode:
        if is_unauthorized_result(result):
            raise ClusterInventoryAuthError(f"Cluster kubeconfig is unauthorized (401) while probing {path}.")
        return False
    return True


async def _namespace_bootstrapped(cluster_id: str, namespace: str) -> bool:
    try:
        from llm_d_lens.deploy.capabilities import is_bootstrap_marker_present
    except ImportError:
        return False
    return bool(await is_bootstrap_marker_present(cluster_id, namespace))


def plan_documents(plan: DeploymentPreparedPlan, index: int | None = None) -> list[dict]:
    cases = plan.payload["artifacts"] if index is None else [plan.payload["artifacts"][index]]
    documents = []
    for case in cases:
        documents.extend(case["artifact"]["deployment_contract"].get("preparedStorage", []))
        documents.extend(item for item in yaml.safe_load_all(case["manifest"]) if isinstance(item, dict))
        bundle = case["artifact"]["deployment_contract"].get("deploymentBundle")
        if bundle is None and case["artifact"].get("guide_id") == "source-composition":
            continue
        documents.extend(item for item in yaml.safe_load_all(bundle["frozen"]["manifest"]) if isinstance(item, dict))
        for asset in bundle["resources"]:
            documents.extend(item for item in yaml.safe_load_all(asset["content"]) if isinstance(item, dict))
    return documents


async def preflight(plan: DeploymentPreparedPlan, index: int | None = None) -> dict:
    documents = plan_documents(plan, index)
    extra_unmodeled: list[str] = []
    try:
        nodes, pods, inventory_source = await fetch_cluster_nodes_and_pods(plan.cluster_id)
    except ClusterInventoryAuthError:
        raise
    except (ClusterInventoryForbiddenError, ValueError, TimeoutError, OSError):
        return placement_preflight(documents, [], [], complete=False)

    crds: list[dict] | None = None
    try:
        crds = await inventory(plan.cluster_id, "customresourcedefinitions")
    except ClusterInventoryAuthError:
        raise
    except ClusterInventoryForbiddenError:
        crds = None
    except (ValueError, TimeoutError, OSError):
        return placement_preflight(documents, [], [], complete=False)

    namespaces: list[dict] | None = None
    try:
        namespaces = await inventory(plan.cluster_id, "namespaces")
    except ClusterInventoryAuthError:
        raise
    except ClusterInventoryForbiddenError:
        namespaces = None
    except (ValueError, TimeoutError, OSError):
        return placement_preflight(documents, [], [], complete=False)

    planned_volumes = {item["metadata"]["name"] for item in documents if item.get("kind") == "PersistentVolume"}
    volumes: list[dict] = []
    if planned_volumes:
        try:
            volumes = await inventory(plan.cluster_id, "persistentvolumes")
        except ClusterInventoryAuthError:
            raise
        except ClusterInventoryForbiddenError:
            extra_unmodeled.append("Cluster-wide PersistentVolume inspection is restricted (403)")
        except (ValueError, TimeoutError, OSError):
            return placement_preflight(documents, [], [], complete=False)

    needs_runtime_classes = any(item["pod"].get("runtimeClassName") for item in workload_pods(documents))
    runtime_classes: list[dict] | None = []
    if needs_runtime_classes:
        try:
            runtime_classes = await inventory(plan.cluster_id, "runtimeclasses")
        except ClusterInventoryAuthError:
            raise
        except ClusterInventoryForbiddenError:
            runtime_classes = None
            extra_unmodeled.append("RuntimeClass inspection is restricted (403)")
        except (ValueError, TimeoutError, OSError):
            return placement_preflight(documents, [], [], complete=False)

    report = placement_preflight(
        documents,
        nodes,
        pods,
        runtime_classes=runtime_classes,
        inventory_source=inventory_source,
    )
    if extra_unmodeled:
        report["unmodeled"] = sorted(set(report.get("unmodeled") or []) | set(extra_unmodeled))
        if not report["errors"] and report["status"] == "pass":
            report["status"] = "unknown"
    selected = plan.payload["artifacts"] if index is None else [plan.payload["artifacts"][index]]
    bootstrapped_cache: dict[str, bool] = {}

    async def _is_bootstrapped(ns: str) -> bool:
        if ns not in bootstrapped_cache:
            bootstrapped_cache[ns] = await _namespace_bootstrapped(plan.cluster_id, ns)
        return bootstrapped_cache[ns]

    satellite_pvs_by_ns: dict[str, set[str]] = {}
    for item in selected:
        case_ns = item["context"]["namespace"]
        for doc in item["artifact"]["deployment_contract"].get("preparedStorage", []):
            if isinstance(doc, dict) and doc.get("kind") == "PersistentVolume":
                pv_name = (doc.get("metadata") or {}).get("name")
                if isinstance(pv_name, str) and pv_name:
                    satellite_pvs_by_ns.setdefault(case_ns, set()).add(pv_name)

    for volume in volumes:
        pv_name = volume["metadata"]["name"]
        if pv_name in planned_volumes:
            claim_ns = ((volume.get("spec") or {}).get("claimRef") or {}).get("namespace")
            if (
                isinstance(claim_ns, str)
                and pv_name in satellite_pvs_by_ns.get(claim_ns, ())
                and await _is_bootstrapped(claim_ns)
            ):
                continue
            report["errors"].append({"code": "PERSISTENT_VOLUME_ALREADY_EXISTS", "message": pv_name})
            report["status"] = "fail"
    if namespaces is not None:
        occupied = {item["metadata"]["name"] for item in namespaces}
        for item in selected:
            ns = item["context"]["namespace"]
            if ns in occupied and not await _is_bootstrapped(ns):
                report["errors"].append({"code": "NAMESPACE_ALREADY_EXISTS", "message": ns})
                report["status"] = "fail"
    else:
        for item in selected:
            ns = item["context"]["namespace"]
            if await _is_bootstrapped(ns):
                continue
            sa_res = await scoped_runner(plan.cluster_id).run(
                ["kubectl", "get", "serviceaccounts", "--namespace", ns, "-o", "name"],
                timeout=15,
            )
            if is_unauthorized_result(sa_res):
                raise ClusterInventoryAuthError(f"Cluster kubeconfig is unauthorized (401) for namespace {ns}.")
            if sa_res.returncode == 0:
                report["errors"].append({"code": "NAMESPACE_ALREADY_EXISTS", "message": ns})
                report["status"] = "fail"
    if disaggregated_sets(documents):
        if crds is not None:
            required = {"disaggregatedsets.disaggregatedset.x-k8s.io", "leaderworkersets.leaderworkerset.x-k8s.io"}
            established = {
                item.get("metadata", {}).get("name")
                for item in crds
                if any(
                    condition.get("type") == "Established" and condition.get("status") == "True"
                    for condition in item.get("status", {}).get("conditions", [])
                )
            }
            for name in sorted(required - established):
                report["errors"].append({"code": "REQUIRED_CRD_UNAVAILABLE", "message": name})
        else:
            api_checks = {
                "disaggregatedsets.disaggregatedset.x-k8s.io": "/apis/disaggregatedset.x-k8s.io/v1",
                "leaderworkersets.leaderworkerset.x-k8s.io": "/apis/leaderworkerset.x-k8s.io/v1",
            }
            for name, path in sorted(api_checks.items()):
                if not await _api_group_available(plan.cluster_id, path):
                    report["errors"].append({"code": "REQUIRED_CRD_UNAVAILABLE", "message": name})
        if report["errors"]:
            report["status"] = "fail"
    report["slicePreview"] = [
        preview
        for case_index in (range(len(plan.payload["artifacts"])) if index is None else [index])
        if (preview := slice_preview(plan, case_index, report.get("assignments") or [], nodes, pods)) is not None
    ]
    return report


def slice_preview(plan: DeploymentPreparedPlan, case_index: int, assignments, nodes, pods) -> dict | None:
    """Summarize one case's DisaggregatedSet as reviewed: Slices, roles, resources and modeled placement."""
    item = plan.payload["artifacts"][case_index]
    documents = [document for document in yaml.safe_load_all(item["manifest"]) if isinstance(document, dict)]
    workloads = disaggregated_sets(documents)
    if len(workloads) != 1:
        return None
    workload = workloads[0]
    spec = workload.get("spec") or {}
    name = (workload.get("metadata") or {}).get("name", "")
    policy = spec.get("placementPolicy") or {"type": "None"}
    capacity = slice_capacity(documents, nodes, pods)
    helm = (item["artifact"]["deployment_contract"].get("deploymentBundle") or {}).get("helm", {})
    values = "\n".join(str(layer.get("content") or "") for layer in helm.get("values") or [])
    slices: dict[int, list[dict]] = {}
    for assignment in assignments:
        if assignment.get("workload") == name:
            slices.setdefault(assignment["slice"], []).append(
                {key: assignment[key] for key in ("role", "group", "rank", "node", "domain") if key in assignment}
            )
    return {
        "caseIndex": case_index,
        "workload": name,
        "slices": spec.get("slices", 1),
        "maxFeasibleSlices": max_feasible_slices(capacity, policy),
        "placementPolicy": policy,
        "sliceRouting": "prefer" if "topology-affinity-scorer" in values else "inherit",
        "roles": [
            {
                "name": role.get("name"),
                "replicas": (role.get("spec") or {}).get("replicas", 1),
                "groupSize": ((role.get("spec") or {}).get("leaderWorkerTemplate") or {}).get("size", 1),
                "scaling": (role.get("scaling") or {}).get("mode", "Static"),
            }
            for role in spec.get("roles") or []
        ],
        "sliceRequests": capacity["sliceRequests"],
        "acceleratorResources": capacity["acceleratorResources"],
        "placement": [{"slice": key, "pods": value} for key, value in sorted(slices.items())],
    }


async def storage_resources(context: dict) -> list[dict]:
    from llm_d_lens.storage.service import (
        StorageVolumeKind,
        _pvc_namespace,
        _satellite_pv_manifest,
        _satellite_pvc_manifest,
        get_ready_volume,
    )

    volume_id = (context.get("runtime") or {}).get("storageVolumeId")
    if not volume_id:
        return []
    volume = await get_ready_volume(volume_id, cluster_id=context["provenance"]["cluster_server_id"])
    namespace = context["namespace"]
    if namespace == _pvc_namespace(volume):
        return []
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC:
        raise ValueError("A dynamic PVC cannot be mirrored into the prepared deployment namespace")
    return [_satellite_pv_manifest(volume, namespace=namespace), _satellite_pvc_manifest(volume, namespace=namespace)]


def restart_request(run, case_id):
    from llm_d_lens.deploy.contracts import DeploymentRunCreateRequest

    case = next((case for case in run.cases if case.id == case_id), None)
    if case is None or case.status not in {"stopped", "failed"}:
        raise ValueError("Only stopped or failed deployment cases can be prepared for restart")
    if run.provenance.get("evaluation_id"):
        raise ValueError("Retry this deployment through its owning Evaluation workflow")
    return DeploymentRunCreateRequest(
        configurations=[run.source_configurations[case.source_configuration_ordinal].model_copy(deep=True)],
        failure_policy=run.failure_policy,
        runtime_binding=run.provenance.get("runtime_binding"),
        provenance={
            **{key: value for key, value in run.provenance.items() if not key.startswith("_prepared")},
            "retry_of": {"run_id": run.id, "case_id": case.id},
        },
    )


async def prepare(manager, request, *, owner_user_id: str | None = None) -> DeploymentPreparedPlan:
    from llm_d_lens.deploy.application import cluster_deployment_source

    if any(key.startswith("_prepared") for key in request.provenance):
        raise ValueError("Prepared execution metadata is server-owned")
    session = require_active_session(request.provenance.get("cluster_session_id"))
    if request.provenance.get("cluster_server_id") != session.server_id:
        raise ValueError("Prepared plan requires the selected cluster and its active session")
    identity = request_identity(request)
    bound = request.model_copy(deep=True)
    bound.provenance["deployment_source"] = cluster_deployment_source(
        session, bound.provenance.get("deployment_source")
    )
    for configuration in bound.configurations:
        if configuration.checksum != configuration_checksum(configuration.content):
            raise ValueError("Configuration checksum does not match content")
        if (configuration.provenance.get("cluster_ref") or {}).get("id") != session.server_id:
            raise ValueError("Configuration must be regenerated for the selected cluster")
    run = DeploymentPlanService(manager.store).build_run(bound)
    if len(run.cases) == 1:
        from llm_d_lens.deploy import capabilities
        from llm_d_lens.deploy.application import (
            bind_single_case_target_namespace,
            ensure_namespace_not_used_by_active_execution,
            resolve_bootstrapped_target_namespace,
            single_case_namespace_lookup,
        )

        case = run.cases[0]
        agentic_run_id_str, deployment_name_str = single_case_namespace_lookup(bound, case)
        verified_ns = capabilities.peek_verified_namespace(
            session.server_id,
            owner_user_id=owner_user_id,
            agentic_deployment_run_id=agentic_run_id_str,
            deployment_name=deployment_name_str,
            prepared_plan_id=None,
        )
        if verified_ns is None:
            verified_ns = await resolve_bootstrapped_target_namespace(session.server_id, bound, case)
        if verified_ns:
            ensure_namespace_not_used_by_active_execution(manager.store, verified_ns)
        bind_single_case_target_namespace(run, case, verified_ns)
    worker = manager._worker(run)
    artifacts = []
    for case in run.cases:
        artifact, context = await worker.prepare_case(case)
        manifest = Path(artifact.manifest_ref).read_text(encoding="utf-8")
        comparison = bound.configurations[case.source_configuration_ordinal].content.get("comparisonBudget")
        if comparison:
            actual = workload_summary(list(yaml.safe_load_all(manifest)))
            if actual["unmodeled"] or actual["resourceRequests"].get(comparison["resource"]) != comparison["quantity"]:
                raise ValueError("Rendered baseline accelerator requests do not match the source DS comparison budget")
            context["comparison_budget"] = comparison
        validate_review_resources(
            [item for item in yaml.safe_load_all(manifest) if item is not None], context["namespace"]
        )
        frozen = asdict(artifact)
        frozen["deployment_contract"]["preparedStorage"] = await storage_resources(context)
        frozen["manifest_ref"] = None
        frozen["manifest_checksum"] = text_checksum(manifest)
        artifacts.append({"artifact": frozen, "manifest": manifest, "context": context})
    raw_target_ns = request.provenance.get("target_namespace")
    requested_target_ns = raw_target_ns.strip() if isinstance(raw_target_ns, str) and raw_target_ns.strip() else None
    payload = {
        "schemaVersion": "deployment-plan.v1",
        "requestIdentity": identity,
        "requestedTargetNamespace": requested_target_ns,
        "run": run.model_dump(mode="json"),
        "artifacts": artifacts,
    }
    plan = DeploymentPreparedPlan(
        cluster_id=session.server_id,
        owner_user_id=owner_user_id,
        digest=configuration_checksum(payload),
        payload=payload,
        preflight={},
    )
    plan.preflight = await preflight(plan)
    return DeploymentPlanDao().create(plan)


def public_plan(plan: DeploymentPreparedPlan) -> dict:
    cases = []
    for item in plan.payload["artifacts"]:
        bundle = item["artifact"]["deployment_contract"].get("deploymentBundle")
        secret = (item["context"].get("deployment_policy") or {}).get("model_secret") or {"mode": "none"}
        cases.append(
            {
                "namespace": item["context"]["namespace"],
                "manifest": item["manifest"],
                "namespaceManifest": item["artifact"]["deployment_contract"].get("preparedNamespace"),
                "storageResources": item["artifact"]["deployment_contract"].get("preparedStorage", []),
                "comparisonBudget": item["context"].get("comparison_budget"),
                "secretReference": {
                    key: secret[key] for key in ("mode", "sourceNamespace", "sourceName") if key in secret
                },
                "manifestChecksum": item["artifact"]["manifest_checksum"],
                "routerManifest": bundle["frozen"]["manifest"] if bundle else "",
                "routerValues": bundle["helm"]["values"][-1]["content"] if bundle else "",
                "chartChecksum": bundle["frozen"]["chartChecksum"] if bundle else None,
                "resources": bundle["resources"] if bundle else [],
                "sourceCommit": item["artifact"]["source_ref"],
            }
        )
    return {
        "id": plan.id,
        "digest": plan.digest,
        "clusterId": plan.cluster_id,
        "status": plan.status,
        "createdAt": plan.created_at.isoformat() if isinstance(plan.created_at, datetime) else plan.created_at,
        "runId": plan.run_id,
        "preflight": plan.preflight,
        "cases": cases,
    }


async def _ensure_consume_permissions(
    plan: DeploymentPreparedPlan,
    run: DeploymentRun,
    request,
    *,
    verified_ns: str | None = None,
) -> None:
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.deploy import capabilities

    artifacts = plan.payload.get("artifacts") or []
    single_case = len(run.cases) == 1 and len(artifacts) == 1
    if single_case and verified_ns and artifacts[0]["context"]["namespace"] == verified_ns:
        return
    if cluster_registry.get_cluster(plan.cluster_id) is None:
        return

    for index, entry in enumerate(artifacts):
        case_ns = entry["context"]["namespace"]
        if single_case and verified_ns == case_ns:
            continue
        case = run.cases[index] if index < len(run.cases) else None
        case_prov = (case.create_request.provenance or {}) if case is not None else {}
        contract = entry["artifact"].get("deployment_contract") or {}
        bundle = contract.get("deploymentBundle")
        runtime = entry["context"].get("runtime") or {}
        storage_volume_id = runtime.get("storageVolumeId")
        uses_storage = bool(storage_volume_id or contract.get("preparedStorage"))
        documents = plan_documents(plan, index)
        uses_lws = bool(disaggregated_sets(documents)) or any(doc.get("kind") == "LeaderWorkerSet" for doc in documents)
        manifest_parts = [entry.get("manifest") or ""]
        if bundle:
            frozen_manifest = (bundle.get("frozen") or {}).get("manifest")
            if frozen_manifest:
                manifest_parts.append(frozen_manifest)
            for asset in bundle.get("resources") or ():
                if asset.get("content"):
                    manifest_parts.append(asset["content"])
        combined_manifest = "\n---\n".join(part for part in manifest_parts if part.strip()) or None
        deployment_name = (
            request.provenance.get("deployment_name")
            or case_prov.get("deployment_name")
            or (
                (case.create_request.deployment_policy.value or {}).get("deployment_name") if case is not None else None
            )
        )
        agentic_run_id = request.provenance.get("agentic_deployment_run_id") or case_prov.get(
            "agentic_deployment_run_id"
        )
        perm_request = capabilities.DeploymentPermissionCheckRequest(
            cluster_id=plan.cluster_id,
            namespace=case_ns,
            deployment_name=str(deployment_name) if deployment_name else None,
            provider_ref=case.provider_ref if case is not None else None,
            agentic_deployment_run_id=str(agentic_run_id) if agentic_run_id else None,
            prepared_plan_id=plan.id,
            uses_storage=uses_storage,
            storage_volume_id=storage_volume_id,
            uses_lws=uses_lws,
            uses_router=bool(bundle),
            manifest=combined_manifest,
        )
        report = await capabilities.check_deployment_permissions(perm_request)
        if not report.get("sufficient"):
            raise capabilities.DeploymentAdminSetupRequiredError(report)


async def consume(manager, request, *, owner_user_id: str | None = None):
    dao = DeploymentPlanDao()
    plan = dao.get(request.prepared_plan_id)
    if plan is None or plan.owner_user_id != owner_user_id:
        raise ValueError("Prepared plan is not available to this owner")
    session = require_active_session(request.provenance.get("cluster_session_id"))
    if plan.cluster_id != session.server_id or request_identity(request) != plan.payload["requestIdentity"]:
        raise ValueError("Configuration, runtime binding or cluster changed; prepare a new plan")
    artifacts = plan.payload.get("artifacts") or []
    raw_target_ns = request.provenance.get("target_namespace")
    requested_target_ns = raw_target_ns.strip() if isinstance(raw_target_ns, str) and raw_target_ns.strip() else None
    if requested_target_ns is not None:
        frozen_single_ns = artifacts[0]["context"]["namespace"] if len(artifacts) == 1 else None
        allowed_target_ns = {
            ns
            for ns in (frozen_single_ns, plan.payload.get("requestedTargetNamespace"))
            if isinstance(ns, str) and ns.strip()
        }
        if requested_target_ns not in allowed_target_ns:
            raise ValueError("Configuration, runtime binding or cluster changed; prepare a new plan")
    if request.expected_plan_digest != plan.digest or configuration_checksum(plan.payload) != plan.digest:
        raise ValueError("Reviewed deployment plan digest does not match")
    if plan.run_id:
        existing = manager.store.get_run(plan.run_id)
        if existing is None:
            raise ValueError("The consumed plan's Run was removed; prepare a new plan")
        return existing
    report = await preflight(plan)
    if report["status"] == "fail" or report["status"] == "unknown" and not request.acknowledge_unknown:
        raise ValueError("Deployment preflight failed or its unknown constraints have not been acknowledged")
    run = DeploymentRun.model_validate(plan.payload["run"])
    is_single_case = len(run.cases) == 1 and len(artifacts) == 1
    case_ns: str | None = None
    consumed_ns: str | None = None
    agentic_run_id_str: str | None = None
    deployment_name_str: str | None = None
    if is_single_case:
        from llm_d_lens.deploy import capabilities
        from llm_d_lens.deploy.application import single_case_namespace_lookup

        case_ns = artifacts[0]["context"]["namespace"]
        agentic_run_id_str, deployment_name_str = single_case_namespace_lookup(request, run.cases[0])
        peeked_ns = capabilities.peek_verified_namespace(
            plan.cluster_id,
            owner_user_id=owner_user_id,
            agentic_deployment_run_id=agentic_run_id_str,
            deployment_name=deployment_name_str,
            prepared_plan_id=plan.id,
        )
        if peeked_ns == case_ns:
            consumed_ns = capabilities.consume_verified_namespace(
                plan.cluster_id,
                owner_user_id=owner_user_id,
                agentic_deployment_run_id=agentic_run_id_str,
                deployment_name=deployment_name_str,
                prepared_plan_id=plan.id,
            )
    try:
        from llm_d_lens.deploy.application import ensure_namespace_not_used_by_active_execution

        for entry in artifacts:
            entry_ns = (entry.get("context") or {}).get("namespace")
            if isinstance(entry_ns, str) and entry_ns:
                ensure_namespace_not_used_by_active_execution(manager.store, entry_ns)
        await _ensure_consume_permissions(plan, run, request, verified_ns=consumed_ns)
        run.provenance.update({"_prepared_plan_id": plan.id, "cluster_session_id": session.id})
        for index, case in enumerate(run.cases):
            case.create_request.provenance.update(
                {
                    "_prepared_plan_id": plan.id,
                    "_prepared_plan_digest": plan.digest,
                    "_prepared_case_index": index,
                    "_prepared_acknowledge_unknown": request.acknowledge_unknown,
                    "cluster_session_id": session.id,
                }
            )
        run_id = dao.consume(plan.id, plan.digest, run)
        saved = manager.store.get_run(run_id)
        worker = manager.worker_for_run(run_id)
    except BaseException:
        if consumed_ns:
            from llm_d_lens.deploy import capabilities

            capabilities.record_verified_namespace(
                plan.cluster_id,
                consumed_ns,
                owner_user_id=owner_user_id,
                agentic_deployment_run_id=agentic_run_id_str,
                deployment_name=deployment_name_str,
                prepared_plan_id=plan.id,
            )
        raise
    manager._schedule_run(run_id, worker)
    return saved


async def execution_artifact(request) -> tuple[GuideDeploymentArtifact, dict]:
    plan = DeploymentPlanDao().get(request.provenance["_prepared_plan_id"])
    if plan is None or not plan.run_id or plan.digest != request.provenance.get("_prepared_plan_digest"):
        raise ValueError("Execution has no consumed, reviewed deployment plan")
    if configuration_checksum(plan.payload) != plan.digest:
        raise ValueError("Prepared plan payload checksum changed")
    if request.provenance.get("cluster_server_id") != plan.cluster_id:
        raise ValueError("Execution cluster differs from the reviewed plan")
    index = request.provenance["_prepared_case_index"]
    saved_artifact = plan.payload["artifacts"][index]
    if await storage_resources(saved_artifact["context"]) != saved_artifact["artifact"]["deployment_contract"].get(
        "preparedStorage", []
    ):
        raise ValueError("Storage resources differ from the reviewed plan")
    original = plan.payload["run"]["cases"][index]["create_request"]
    if (
        request.request_id != original["request_id"]
        or request.deployment_policy.model_dump(mode="json") != original["deployment_policy"]
        or [item.model_dump(mode="json") for item in request.configuration_artifacts]
        != original["configuration_artifacts"]
    ):
        raise ValueError("Execution inputs differ from the reviewed case")
    report = await preflight(plan, index)
    if (
        report["status"] == "fail"
        or report["status"] == "unknown"
        and not request.provenance.get("_prepared_acknowledge_unknown")
    ):
        raise ValueError("Execution-time deployment preflight failed")
    item = deepcopy(plan.payload["artifacts"][index])
    root = storage_path("data", "artifacts", "deployment-plans", plan.id, str(index))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "workload.yaml"
    if path.exists() and path.read_text(encoding="utf-8") != item["manifest"]:
        raise ValueError("Prepared workload artifact changed on disk")
    path.write_text(item["manifest"], encoding="utf-8")
    register_artifacts(root, owner_type="deployment-plan", owner_id=plan.id, retention_class="configuration")
    item["artifact"]["manifest_ref"] = str(path)
    return GuideDeploymentArtifact(**item["artifact"]), item["context"]
