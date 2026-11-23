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
        raise ValueError(f"Cluster inventory unavailable for {resource}; verify access and installed APIs")
    response = json.loads(result.stdout)
    if not isinstance(response.get("items"), list) or response.get("metadata", {}).get("continue"):
        raise ValueError(f"Cluster inventory incomplete for {resource}")
    return response["items"]


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
    try:
        nodes = await inventory(plan.cluster_id, "nodes")
        pods = await inventory(plan.cluster_id, "pods")
        crds = await inventory(plan.cluster_id, "customresourcedefinitions")
        namespaces = await inventory(plan.cluster_id, "namespaces")
        planned_volumes = {item["metadata"]["name"] for item in documents if item.get("kind") == "PersistentVolume"}
        volumes = await inventory(plan.cluster_id, "persistentvolumes") if planned_volumes else []
        runtime_classes = (
            await inventory(plan.cluster_id, "runtimeclasses")
            if any(item["pod"].get("runtimeClassName") for item in workload_pods(documents))
            else []
        )
    except (ValueError, TimeoutError, OSError):
        return placement_preflight(documents, [], [], complete=False)
    report = placement_preflight(documents, nodes, pods, runtime_classes=runtime_classes)
    for volume in volumes:
        if volume["metadata"]["name"] in planned_volumes:
            report["errors"].append({"code": "PERSISTENT_VOLUME_ALREADY_EXISTS", "message": volume["metadata"]["name"]})
            report["status"] = "fail"
    selected = plan.payload["artifacts"] if index is None else [plan.payload["artifacts"][index]]
    occupied = {item["metadata"]["name"] for item in namespaces}
    for item in selected:
        if item["context"]["namespace"] in occupied:
            report["errors"].append({"code": "NAMESPACE_ALREADY_EXISTS", "message": item["context"]["namespace"]})
            report["status"] = "fail"
    if disaggregated_sets(documents):
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
    payload = {
        "schemaVersion": "deployment-plan.v1",
        "requestIdentity": identity,
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


async def consume(manager, request, *, owner_user_id: str | None = None):
    dao = DeploymentPlanDao()
    plan = dao.get(request.prepared_plan_id)
    if plan is None or plan.owner_user_id != owner_user_id:
        raise ValueError("Prepared plan is not available to this owner")
    session = require_active_session(request.provenance.get("cluster_session_id"))
    if plan.cluster_id != session.server_id or request_identity(request) != plan.payload["requestIdentity"]:
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
