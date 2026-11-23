"""Read actual ownership and placement; never infer KV transfer from HTTP success."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import uuid4

import httpx
from prometheus_client.parser import text_string_to_metric_families

from llm_d_lens.configuration.disaggregated_set import workload_pods
from llm_d_lens.deploy.prepared_plans import inventory
from llm_d_lens.infrastructure.artifacts.store import register_artifacts
from llm_d_lens.infrastructure.kubernetes import scoped_runner
from llm_d_lens.infrastructure.storage.paths import storage_path


def verify_runtime(
    desired: list[dict],
    sets: list[dict],
    groups: list[dict],
    pods: list[dict],
    nodes: list[dict],
    *,
    stateful_sets: list[dict] | None = None,
    deployments: list[dict] | None = None,
    replica_sets: list[dict] | None = None,
) -> dict:
    result = {
        name: {"status": "unknown", "reasons": []} for name in ("kubernetes", "placement", "routing", "kvTransfer")
    }
    result["routing"]["reasons"] = ["No request-correlated prefill/decode pair evidence is available."]
    result["kvTransfer"]["reasons"] = [
        "HTTP success and ready Pods do not prove KV transfer; recompute fallback may be active."
    ]
    result["observedAt"] = datetime.now(UTC).isoformat()
    result["pods"] = []
    expected = [item for item in workload_pods(desired) if item["kind"] == "DisaggregatedSet"]
    if not expected:
        result["kubernetes"]["reasons"].append("This verification requires a DisaggregatedSet plan.")
        return result
    by_name = {item["metadata"]["name"]: item for item in sets}
    objects = {
        item["metadata"].get("uid"): item
        for item in [*sets, *groups, *pods, *(stateful_sets or []), *(deployments or []), *(replica_sets or [])]
        if item.get("metadata", {}).get("uid")
    }
    node_labels = {item["metadata"]["name"]: item["metadata"].get("labels", {}) for item in nodes}
    roots, placement_errors, readiness_errors, missing = {}, [], [], []
    for name in {item["workload"] for item in expected}:
        actual = by_name.get(name)
        if actual is None:
            readiness_errors.append(f"DisaggregatedSet {name} is missing")
            continue
        generation = actual.get("metadata", {}).get("generation")
        observed = actual.get("status", {}).get("observedGeneration")
        if generation is None or observed is None:
            missing.append(f"DisaggregatedSet {name} does not expose observedGeneration")
        elif observed < generation:
            readiness_errors.append(f"DisaggregatedSet {name} has not reconciled its current generation")
        roots[actual["metadata"]["uid"]] = name
    for group in groups:
        metadata = group.get("metadata", {})
        if not any(
            owner.get("uid") in roots and owner.get("controller") for owner in metadata.get("ownerReferences", [])
        ):
            continue
        generation = metadata.get("generation")
        observed = group.get("status", {}).get("observedGeneration")
        if generation is None or observed is None:
            missing.append(f"LeaderWorkerSet {metadata['name']} does not expose observedGeneration")
        elif observed < generation:
            readiness_errors.append(f"LeaderWorkerSet {metadata['name']} has not reconciled its current generation")
    counts, domains = {}, {}
    for pod in pods:
        metadata = pod.get("metadata", {})
        if metadata.get("deletionTimestamp"):
            continue
        chain, current, seen = [], pod, set()
        while current is not None:
            uid = current.get("metadata", {}).get("uid")
            if uid in seen:
                break
            seen.add(uid)
            chain.append(current)
            if uid in roots:
                break
            owners = [
                owner for owner in current.get("metadata", {}).get("ownerReferences", []) if owner.get("controller")
            ]
            current = objects.get(owners[0].get("uid")) if len(owners) == 1 else None
        if current is None or current.get("metadata", {}).get("uid") not in roots:
            continue
        workload = roots[current["metadata"]["uid"]]
        labels = {}
        for ancestor in reversed(chain):
            labels.update(ancestor.get("metadata", {}).get("labels", {}))
        role = labels.get("disaggregatedset.x-k8s.io/role")
        slice_id = labels.get("disaggregatedset.x-k8s.io/slice")
        if role is None or slice_id is None:
            missing.append(f"Pod {metadata['name']} has no verifiable role/Slice labels")
            continue
        node = pod.get("spec", {}).get("nodeName")
        if node not in node_labels:
            missing.append(f"Pod {metadata['name']} has no observable assigned Node")
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        if not ready:
            readiness_errors.append(f"Pod {metadata['name']} is not ready")
        item = next((item for item in expected if item["workload"] == workload and item["role"] == role), None)
        if item is None:
            placement_errors.append(f"Pod {metadata['name']} has an unexpected role")
            continue
        counts[(workload, slice_id, role)] = counts.get((workload, slice_id, role), 0) + 1
        topology = item["placementPolicy"].get("topology")
        domain = node_labels.get(node, {}).get(topology) if topology else None
        if topology:
            if domain is None:
                missing.append(f"Pod {metadata['name']} has no actual Node topology label")
            else:
                domains.setdefault((workload, slice_id), set()).add(domain)
        revisions = sorted({value for key, value in labels.items() if "revision" in key.lower()})
        result["pods"].append(
            {
                "name": metadata["name"],
                "uid": metadata["uid"],
                "workload": workload,
                "role": role,
                "slice": slice_id,
                "node": node,
                "domain": domain,
                "ready": ready,
                "revisions": revisions,
                "revision": labels.get("disaggregatedset.x-k8s.io/revision"),
                "restarts": {
                    status["name"]: status.get("restartCount", 0)
                    for status in pod.get("status", {}).get("containerStatuses", [])
                },
            }
        )
    for name in {item["workload"] for item in expected}:
        planned = [item for item in expected if item["workload"] == name]
        actual_slices = {key[1] for key in counts if key[0] == name}
        if len(actual_slices) != len({item["slice"] for item in planned}):
            readiness_errors.append(f"DisaggregatedSet {name} does not have the planned number of Slices")
        for slice_id in actual_slices:
            for role in {item["role"] for item in planned}:
                required = sum(1 for item in planned if item["slice"] == 0 and item["role"] == role)
                if counts.get((name, slice_id, role), 0) != required:
                    readiness_errors.append(f"Slice {slice_id} role {role} has an unexpected Pod count")
    for identity, values in domains.items():
        if len(values) != 1:
            placement_errors.append(f"Slice {identity[1]} spans multiple topology domains")
        if any(
            other != identity and other[0] == identity[0] and values.intersection(other_values)
            for other, other_values in domains.items()
        ):
            placement_errors.append(f"Slice {identity[1]} shares a topology domain with another Slice")
    result["kubernetes"] = {
        "status": "fail" if readiness_errors else "unknown" if missing else "pass",
        "reasons": readiness_errors + missing,
    }
    result["placement"] = {
        "status": "fail" if placement_errors else "unknown" if missing or readiness_errors else "pass",
        "reasons": placement_errors + missing,
        "scope": "Observed Node labels, not physical-fabric certification",
    }
    return result


async def _metrics(cluster_id, namespace, pods):
    result = {}
    for pod in pods:
        containers = [
            container
            for container in pod.get("spec", {}).get("containers", [])
            if container.get("name") == "modelserver"
        ]
        ports = [
            port["containerPort"]
            for container in containers
            for port in container.get("ports", [])
            if port.get("name") in {"metrics", "http", "http-metrics", "modelserver"}
        ]
        if not ports:
            continue
        name = pod["metadata"]["name"]
        path = f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(name, safe='')}:{ports[0]}/proxy/metrics"
        response = await scoped_runner(cluster_id).run(["kubectl", "get", "--raw", path], timeout=15)
        if response.returncode:
            continue
        samples = {}
        for family in text_string_to_metric_families(response.stdout):
            for sample in family.samples:
                if any(
                    marker in sample.name.lower()
                    for marker in ("nixl", "kv_transfer", "prompt_tokens", "generation_tokens", "request_success")
                ):
                    key = sample.name + str(sorted(sample.labels.items()))
                    samples[key] = sample.value
        result[pod["metadata"]["uid"]] = samples
    return result


def correlate_transfer(report, snapshots, response_id, stable):
    result = {name: {"status": "unknown", "reasons": []} for name in ("routing", "kvTransfer")}
    try:
        if not stable or not snapshots or not response_id:
            raise ValueError("Stable topology, engine snapshots and a response ID are required")
        pods = {pod["uid"]: pod for pod in report["pods"]}
        events, engines = [], {}
        for uid, snapshot in snapshots:
            if uid not in pods or snapshot.get("incomplete") or snapshot["engines"] != snapshot["engines_end"]:
                raise ValueError("Engine identity changed or capture was incomplete")
            for engine in snapshot["engines"]:
                if not engine["supported"] or engine["vllm_version"].split("+")[0] != "0.26.0":
                    raise ValueError("Unsupported transfer probe")
                identity = (engine["engine_id"], engine["rank"])
                if identity in engines:
                    raise ValueError("Ambiguous engine rank identity")
                engines[identity] = {**engine, "podUid": uid}
            for event in snapshot["events"]:
                identity = (event["engine_id"], event["rank"])
                if (
                    identity not in engines
                    or engines[identity]["podUid"] != uid
                    or engines[identity]["world_size"] != event["world_size"]
                ):
                    raise ValueError("Event does not belong to the observed engine")
                if not snapshot["start"] <= event["timestamp"] < snapshot["end"]:
                    raise ValueError("Transfer event is outside the capture window")
                events.append({**event, "podUid": uid})

        def matches(identity):
            external = re.sub(r"-[0-9a-f]{8}$", "", identity)
            return response_id in {identity, external, external[:-2] if external.endswith("-0") else external}

        related = [
            event for event in events if matches(event["request_id"]) and pods[event["podUid"]]["role"] == "decode"
        ]
        if len({(event["engine_id"], event["request_id"]) for event in related}) != 1:
            raise ValueError("No unique decode request matches the server response ID")
        if any(event["event"] == "failed" for event in related):
            result["kvTransfer"] = {
                "status": "fail",
                "reasons": ["The correlated request encountered NIXL failure/recompute fallback"],
            }
            raise ValueError("Transfer failure prevents a successful P/D evidence verdict")
        received = [event for event in related if event["event"] == "received" and event.get("blocks", 0) > 0]
        if not received or len({(event["remote_engine_id"], event["remote_request_id"]) for event in received}) != 1:
            raise ValueError("A unique remote producer and nonempty receive completion are required")
        remote_engine, remote_request = received[0]["remote_engine_id"], received[0]["remote_request_id"]
        sent = [
            event
            for event in events
            if event["engine_id"] == remote_engine
            and event["request_id"] == remote_request
            and event["event"] == "send-acknowledged"
        ]
        for group in (received, sent):
            if not group or len({event["world_size"] for event in group}) != 1:
                raise ValueError("Transfer rank coverage is unavailable")
            size = group[0]["world_size"]
            if (
                type(size) is not int
                or size < 1
                or len(group) != size
                or {event["rank"] for event in group} != set(range(size))
            ):
                raise ValueError("Transfer completion does not cover every TP rank exactly once")
        producers = [pods[event["podUid"]] for event in sent]
        consumers = [pods[event["podUid"]] for event in received]
        participants = producers + consumers
        if any(pod["role"] != "prefill" for pod in producers) or len({pod["workload"] for pod in participants}) != 1:
            raise ValueError("Producer/consumer ownership does not identify this DS prefill/decode pair")
        if any(not pod.get("revision") for pod in participants) or len({pod["revision"] for pod in participants}) != 1:
            raise ValueError("P/D revision identity is missing or mixed")
        evidence = {
            "responseId": response_id,
            "decodeRequestId": received[0]["request_id"],
            "prefillRequestId": remote_request,
            "prefillPods": sorted({pod["uid"] for pod in producers}),
            "decodePods": sorted({pod["uid"] for pod in consumers}),
            "sameSlice": len({pod["slice"] for pod in participants}) == 1,
            "sameDomain": all(pod["domain"] is not None for pod in participants)
            and len({pod["domain"] for pod in participants}) == 1,
            "revision": participants[0]["revision"],
            "receivedBlocksByRank": {str(event["rank"]): event["blocks"] for event in received},
            "transferEvents": received + sent,
        }
        result = {
            name: {
                "status": "pass",
                "reasons": [],
                "evidence": evidence,
                "scope": "This bounded request only; NIXL pull completion and producer acknowledgements",
            }
            for name in result
        }
    except (ValueError, KeyError, TypeError) as error:
        for dimension in result.values():
            dimension["reasons"].append(str(error))
    return result


@asynccontextmanager
async def transfer_capture(cluster_id, namespace, pods):
    session = str(uuid4())
    captured = {"snapshots": [], "errors": []}
    started = []

    async def probe(pod, container, action):
        response = await scoped_runner(cluster_id).run(
            [
                "kubectl",
                "exec",
                "--namespace",
                namespace,
                pod["metadata"]["name"],
                "-c",
                container,
                "--",
                "python3",
                "/opt/prism-kv-trace/prism_kv_engine.py",
                action,
                session,
            ],
            timeout=15,
        )
        if response.returncode:
            raise ValueError("Transfer probe unavailable or already collecting")
        value = json.loads(response.stdout)
        if value.get("error"):
            raise ValueError(value["error"])
        return value

    try:
        if len(pods) > 32:
            captured["errors"].append("Transfer probe exceeds the 32-Pod interactive limit")
        else:
            for pod in pods:
                for container in pod.get("spec", {}).get("containers", []):
                    if not any(item.get("name") == "PRISM_KV_TRACE_NAMESPACE" for item in container.get("env", [])):
                        continue
                    try:
                        await probe(pod, container["name"], "transfer-begin")
                        started.append((pod, container["name"]))
                    except (ValueError, OSError, TimeoutError) as error:
                        captured["errors"].append(str(error))
        yield captured
    finally:
        for pod, container in started:
            try:
                snapshot = await probe(pod, container, "transfer-end")
                captured["snapshots"].append((pod["metadata"]["uid"], snapshot))
            except (ValueError, OSError, TimeoutError) as error:
                captured["errors"].append(str(error))


async def _smoke_request(context, inference):
    from llm_d_lens.deploy.executions import ensure_execution_endpoint

    endpoint = await ensure_execution_endpoint(context.execution_id)
    if not endpoint.endpoint:
        inference["reason"] = "Deployment endpoint unavailable"
        return
    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
        url = endpoint.endpoint.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        models = await client.get(url + "/models")
        models.raise_for_status()
        response = await client.post(
            url + "/completions",
            headers={"X-Request-Id": inference["requestId"]},
            json={
                "model": models.json()["data"][0]["id"],
                "prompt": "Summarize this numbered sequence: "
                + inference["requestId"]
                + " "
                + " ".join(str(number) for number in range(384)),
                "max_tokens": 16,
                "stream": False,
            },
        )
        inference.update(status="pass" if response.is_success else "fail", httpStatus=response.status_code)
        if response.is_success:
            payload = response.json()
            inference.update(responseId=payload.get("id"), usage=payload.get("usage"))


def persist_verification(context, report):
    from llm_d_lens.deploy.run_store import JsonDeploymentRunStore

    identity = str(uuid4())
    root = storage_path("data", "artifacts", "deployments", context.execution_id)
    relative = f"runtime-verification/{identity}.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    report["artifact"] = register_artifacts(
        root,
        owner_type="deployment",
        owner_id=context.execution_id,
        files={relative: {}},
        retention_class="evidence",
        manifest_name=f"runtime-verification-{identity}.manifest.json",
        source_version={"probe": "nixl-pull-v1", "supported_vllm": "0.26.0"},
    )
    JsonDeploymentRunStore().update_execution_provenance(context.execution_id, {"runtime_verification": report})
    return report


async def _observe(cluster_id, namespace, desired):
    """DisaggregatedSet roles are backed by LeaderWorkerSets or Deployments, so read both ownership chains."""
    return verify_runtime(
        desired,
        await inventory(cluster_id, "disaggregatedsets", namespace),
        await inventory(cluster_id, "leaderworkersets", namespace),
        await inventory(cluster_id, "pods", namespace),
        await inventory(cluster_id, "nodes"),
        stateful_sets=await inventory(cluster_id, "statefulsets", namespace),
        deployments=await inventory(cluster_id, "deployments", namespace),
        replica_sets=await inventory(cluster_id, "replicasets", namespace),
    )


async def verify_execution(context, desired, *, smoke=False, metrics_only=False):
    namespace, cluster_id = context.namespace, context.cluster_id
    if not namespace or not cluster_id:
        raise ValueError("Runtime verification requires a namespace and a bound cluster")
    pods = await inventory(cluster_id, "pods", namespace)
    report = await _observe(cluster_id, namespace, desired)
    if metrics_only:
        owned = {pod["uid"]: pod["name"] for pod in report["pods"]}
        samples = await _metrics(cluster_id, namespace, [pod for pod in pods if pod["metadata"]["uid"] in owned])
        report["metrics"] = {owned[uid]: values for uid, values in samples.items()}
        report["metricsCollectedAt"] = datetime.now(UTC).isoformat()
        return report
    if not smoke:
        from llm_d_lens.deploy.run_store import JsonDeploymentRunStore

        execution = JsonDeploymentRunStore().get_execution(context.execution_id)
        if execution and execution.provenance.get("runtime_verification"):
            report["lastProbe"] = execution.provenance["runtime_verification"]
        return report
    if report["kubernetes"]["status"] != "pass":
        return persist_verification(context, report)
    owned_uids = {pod["uid"] for pod in report["pods"]}
    before = await _metrics(cluster_id, namespace, [pod for pod in pods if pod["metadata"]["uid"] in owned_uids])
    report["inference"] = {"status": "unknown", "requestId": str(uuid4())}
    async with transfer_capture(
        cluster_id, namespace, [pod for pod in pods if pod["metadata"]["uid"] in owned_uids]
    ) as captured:
        try:
            await _smoke_request(context, report["inference"])
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
            report["inference"].update(status="fail", reason=type(error).__name__)
    after_pods = await inventory(cluster_id, "pods", namespace)
    after_report = await _observe(cluster_id, namespace, desired)
    stable = (
        {pod["uid"]: pod for pod in report["pods"]} == {pod["uid"]: pod for pod in after_report["pods"]}
        and after_report["kubernetes"]["status"] == "pass"
        and after_report["placement"]["status"] == "pass"
    )
    after = await _metrics(cluster_id, namespace, [pod for pod in after_pods if pod["metadata"]["uid"] in owned_uids])
    report["after"] = after_report
    report.update(
        correlate_transfer(
            report,
            captured["snapshots"] if not captured["errors"] else [],
            report["inference"].get("responseId"),
            stable and report["inference"]["status"] == "pass",
        )
    )
    report["captureErrors"] = captured["errors"]
    deltas, resets = {}, []
    for uid, values in before.items():
        for metric, value in values.items():
            if uid not in after or metric not in after[uid]:
                continue
            delta = after[uid][metric] - value
            if delta < 0:
                resets.append({"podUid": uid, "metric": metric})
            elif delta:
                deltas.setdefault(uid, {})[metric] = delta
    report["kvTransfer"].update(
        metricDeltas=deltas,
        counterResets=resets,
        topologyStable=stable,
        metricScope="Aggregate metric changes are supplementary, not request-correlated proof",
    )
    return persist_verification(context, report)
