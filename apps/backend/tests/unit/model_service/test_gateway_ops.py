"""Tests for model-service gateway operations (shared Gateway, model routes)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from llm_d_lens.cluster.registry import gateway_namespace_for
from llm_d_lens.db.dao.model_service_gateway_operation import GatewayOperationDao
from llm_d_lens.db.dao.model_service_group import ModelServiceGroupDao
from llm_d_lens.db.dao.model_service_member import ModelServiceMemberDao
from llm_d_lens.model_service import gateway_ops as gateway_ops_module
from llm_d_lens.model_service.contracts import ModelServiceGroup, ModelServiceMember
from llm_d_lens.model_service.gateway_contracts import GatewayOperation
from llm_d_lens.model_service.gateway_ops import REWRITE_CRD_ANNOTATION, GatewayOpsService
from llm_d_lens.model_service.gateway_providers import (
    CLUSTER_LABEL,
    MANAGED_LABEL,
    MANAGED_VALUE,
    MODEL_SERVICE_LABEL,
    REWRITE_MANAGED_LABEL,
)
from llm_d_lens.model_service.targets import ExecutionMissingError


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "unmanaged", "delete", "still-present", "missing"])
async def test_remove_model_route_deletes_exactly_the_models_named_resources(monkeypatch, failure):
    from llm_d_lens.model_service.gateway_ops import GatewayOpsError
    from llm_d_lens.model_service.gateway_providers import MANAGED_LABEL, MANAGED_VALUE, resource_name

    group = ModelServiceGroup(name="public", model_ref="provider", cluster_id="cluster-a")
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _: SimpleNamespace(gateway_namespace="lens-gateway"))
    ops = GatewayOpsService()
    deleted = set()
    calls = []

    async def kubectl(cluster_id, argv, **kwargs):
        assert cluster_id == group.cluster_id
        calls.append(argv)
        kind = argv[1]
        if argv[0] == "get":
            if failure == "missing" or (kind in deleted and failure != "still-present"):
                return _Result()
            return _Result(
                stdout=json.dumps(
                    {
                        "metadata": {
                            "labels": {
                                MANAGED_LABEL: "other" if failure == "unmanaged" else MANAGED_VALUE,
                            }
                        }
                    }
                )
            )
        assert argv[0] == "delete"
        name = resource_name(f"{group.name}-{group.id}") + ("-model-map" if kind == "configmap" else "")
        if failure == "unmanaged":
            assert name in argv
            assert "-l" not in argv
        else:
            assert argv[argv.index("--field-selector") + 1] == f"metadata.name={name}"
            assert argv[argv.index("-l") + 1] == f"{MANAGED_LABEL}={MANAGED_VALUE}"
        assert "--wait=true" in argv and "--timeout=60s" in argv
        deleted.add(kind)
        return _Result(
            returncode=1 if failure == "delete" else 0, stderr="deletion failed" if failure == "delete" else ""
        )

    monkeypatch.setattr(ops, "_kubectl", kubectl)
    if failure in ("none", "missing", "unmanaged"):
        await ops.remove_model_route(group)
        assert deleted == ({"httproute", "configmap"} if failure in ("none", "unmanaged") else set())
    else:
        with pytest.raises(GatewayOpsError):
            await ops.remove_model_route(group)
        assert not any(call[1] == "configmap" for call in calls)


@pytest.mark.asyncio
async def test_remove_model_route_treats_sdk_empty_list_as_absent(monkeypatch):
    group = ModelServiceGroup(name="public", model_ref="provider", cluster_id="cluster-a")
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _: SimpleNamespace(gateway_namespace="lens-gateway"))
    ops = GatewayOpsService()
    calls = []

    async def kubectl(cluster_id, argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "get"
        return _Result(stdout='{"items": []}')

    monkeypatch.setattr(ops, "_kubectl", kubectl)
    await ops.remove_model_route(group)

    assert len(calls) == 3
    assert all(call[0] == "get" for call in calls)


class _FakeRunner:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if any("infrastructure.parametersRef.name" in str(arg) for arg in argv):
            return _Result(stdout="")
        if list(argv[:3]) == ["kubectl", "get", "envoyproxy"]:
            return _Result(stdout="")
        if list(argv[:3]) == ["kubectl", "get", "httproute"]:
            return _Result(stdout="")
        if list(argv[:4]) == ["kubectl", "get", "gateway", "lens-inference-gateway"]:
            return _Result(stdout="True")
        if list(argv[:3]) == ["kubectl", "get", "gateway"]:
            return _Result(stdout="True")
        if list(argv[:3]) == ["kubectl", "get", "deploy"] and "json" in argv:
            return _Result(
                stdout=json.dumps(
                    {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {"name": "epp", "args": ["--pool-name=qwen", "--secure-serving=false"]}
                                    ]
                                }
                            }
                        }
                    }
                )
            )
        return _Result(stdout="applied")


class _ExistingRouteRunner(_FakeRunner):
    """Replays one Lens-managed HTTPRoute already on the cluster."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "httproute"]:
            return _Result(stdout="qwen-empty")
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


class _ExistingServiceTypeRunner(_FakeRunner):
    """Replays a Gateway already bound to a NodePort EnvoyProxy."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if any("infrastructure.parametersRef.name" in str(arg) for arg in argv):
            return _Result(stdout="lens-inference-gateway-proxy")
        if any("envoyService.type" in str(arg) for arg in argv):
            return _Result(stdout="NodePort")
        return _Result(stdout="True")


class _BusyPortRunner(_FakeRunner):
    """Gateway service plus another Service already using a nodePort."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        if list(argv[:3]) == ["kubectl", "get", "svc"] and "-l" in argv:
            self.calls.append((argv, input))
            return _Result(stdout="gw-svc")
        if list(argv[:3]) == ["kubectl", "get", "svc"] and "-A" in argv:
            self.calls.append((argv, input))
            return _Result(
                stdout='{"items":[{"metadata":{"namespace":"other","name":"other-svc"},'
                '"spec":{"ports":[{"nodePort":30999}]}}]}'
            )
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


def _seed_member(cluster_id: str = "cluster-a") -> ModelServiceMember:
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name=f"qwen3-8b-{cluster_id}",
            model_ref="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
            base_model="Qwen/Qwen3-8B",
        )
    )
    return ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id=f"exec-{cluster_id}",
            cluster_id=cluster_id,
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="qwen",
            endpoint_kind="llm-d-epp",
        )
    )


def _service(*, calls: list, runner_cls=_FakeRunner) -> GatewayOpsService:
    return GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cluster_id: runner_cls(calls),
    )


@pytest.mark.asyncio
async def test_preflight_missing_cluster(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: None)
    operation = await service.preflight("cluster-a")
    assert operation.status == "failed"
    assert "not found" in operation.message


@pytest.mark.asyncio
async def test_install_inference_gateway_applies_shared_gateway(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())

    operation = await service.install_inference_gateway("cluster-a", "istio", install_prerequisites=False)
    assert operation.status == "succeeded"
    assert operation.detail_json["provider"] == "istio"
    applies = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]]
    assert applies
    manifest = applies[-1][1]
    assert "kind: Gateway" in manifest
    assert "controllerName: istio.io/gateway-controller" in manifest
    assert "kind: HTTPRoute" not in manifest
    assert f"namespace: {gateway_namespace_for('cluster-a')}" in manifest


@pytest.mark.asyncio
async def test_uninstall_only_removes_this_registration_gateway_resources(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_namespace="lens-gateway",
            gateway_provider="istio",
            gateway_name="lens-inference-gateway",
            gateway_port=None,
        ),
    )
    operation = await _service(calls=calls).uninstall_inference_gateway("cluster-a")
    assert operation.status == "succeeded"
    deletions = [argv for argv, _ in calls if list(argv[:2]) == ["kubectl", "delete"]]
    assert len(deletions) == 1
    argv = deletions[0]
    assert argv[argv.index("-n") + 1] == gateway_namespace_for("cluster-a")
    assert "-A" not in argv
    assert not {"gatewayclass", "inferencepool", "namespace"} & set(argv[2].split(","))


@pytest.mark.asyncio
async def test_gateway_install_does_not_register_a_global_istio_auth_provider(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(gateway_authz_host="10.0.0.9", gateway_namespace="lens-gateway"),
    )
    monkeypatch.setattr(gateway_ops_module.hostinfo, "current_serving_port", lambda: 8084)
    monkeypatch.setattr(gateway_ops_module, "apply_gateway_crds", AsyncMock(return_value={}))
    monkeypatch.setattr(gateway_ops_module, "ensure_istioctl", lambda _: "istioctl")
    operation = await _service(calls=calls).install_inference_gateway("cluster-a", "istio")
    assert operation.status == "succeeded"
    installs = [argv for argv, _ in calls if argv[0] == "istioctl"]
    assert installs
    assert all("-f" not in argv for argv in installs)
    manifest = [body for argv, body in calls if list(argv[:3]) == ["kubectl", "apply", "-f"]][-1]
    assert "kind: EnvoyFilter" in manifest
    assert "meshConfig" not in manifest


@pytest.mark.asyncio
async def test_install_preserves_existing_envoy_service_type(monkeypatch):
    calls: list = []
    service = _service(calls=calls, runner_cls=_ExistingServiceTypeRunner)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())

    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "succeeded"
    manifest = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]][-1][1]
    assert "kind: EnvoyProxy" in manifest
    assert "type: NodePort" in manifest


@pytest.mark.asyncio
async def test_reconcile_routes_weighted_pools_per_service(monkeypatch):
    import yaml

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="qwen3-8b", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-a", base_model="Qwen/Qwen3-8B"
        )
    )
    for index, (service_name, namespace) in enumerate(
        [("optimized-baseline-epp", "ns-a"), ("optimized-baseline-b-epp", "ns-b")]
    ):
        ModelServiceMemberDao().create(
            ModelServiceMember(
                group_id=group.id,
                execution_id=f"exec-{index}",
                cluster_id="cluster-a",
                target_namespace=namespace,
                target_service=service_name,
                target_port=80,
                epp_ref=service_name,
                endpoint_kind="llm-d-epp",
                weight=90 if index == 0 else 10,
            )
        )
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["routes"] == 1
    manifest = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]][-1][1]
    route = next(doc for doc in yaml.safe_load_all(manifest) if doc and doc["kind"] == "HTTPRoute")
    refs = route["spec"]["rules"][0]["backendRefs"]
    assert len(refs) == 2
    assert {ref["namespace"]: ref["weight"] for ref in refs} == {"ns-a": 90, "ns-b": 10}
    assert all("port" not in ref for ref in refs)
    grants = [doc for doc in yaml.safe_load_all(manifest) if doc and doc["kind"] == "ReferenceGrant"]
    assert {grant["metadata"]["namespace"] for grant in grants} == {"ns-a", "ns-b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", ["zero", "disabled"])
async def test_reconcile_omits_non_routable_providers(monkeypatch, excluded):
    import yaml

    member = _seed_member()
    ModelServiceMemberDao().create(
        member.model_copy(
            update={
                "id": "other-member",
                "execution_id": "other-exec",
                "pool_name": "other",
                "weight": 0 if excluded == "zero" else 100,
                "status": "active" if excluded == "zero" else "disabled",
            }
        )
    )
    calls = []
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )
    operation = await _service(calls=calls).reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    manifest = [call for call in calls if list(call[0][:3]) == ["kubectl", "apply", "-f"]][-1][1]
    route = next(doc for doc in yaml.safe_load_all(manifest) if doc and doc["kind"] == "HTTPRoute")
    assert [ref["name"] for ref in route["spec"]["rules"][0]["backendRefs"]] == ["qwen"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_model", [True, False])
async def test_stop_and_resume_remove_and_restore_route_without_scaling(monkeypatch, stop_model):
    member = _seed_member()
    service = _service(calls=[], runner_cls=_ExistingRouteRunner)
    group = service.groups.get(member.group_id)
    if stop_model:
        group.status = "disabled"
        service.groups.save(group)
    else:
        member.status = "disabled"
        service.members.save(member)
    calls = []
    service = _service(calls=calls, runner_cls=_ExistingRouteRunner)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )
    stopped = await service.reconcile_cluster_gateway(member.cluster_id)
    assert stopped.status == "succeeded"
    assert stopped.detail_json["routes"] == 0
    assert any(argv[:4] == ["kubectl", "delete", "httproute", "qwen-empty"] for argv, _ in calls)
    assert not any("kind: HTTPRoute" in (manifest or "") for _, manifest in calls)
    assert not any("scale" in argv for argv, _ in calls)
    assert service.members.get(member.id).weight == 1

    if stop_model:
        group.status = "active"
        service.groups.save(group)
    else:
        member.status = "active"
        service.members.save(member)
    calls.clear()
    resumed = await service.reconcile_cluster_gateway(member.cluster_id)
    assert resumed.status == "succeeded"
    assert resumed.detail_json["routes"] == 1
    assert any("kind: HTTPRoute" in (manifest or "") for _, manifest in calls)
    assert not any("scale" in argv for argv, _ in calls)


@pytest.mark.asyncio
async def test_route_discovery_failure_does_not_report_stop_applied(monkeypatch):
    member = _seed_member()
    service = _service(calls=[])
    group = service.groups.get(member.group_id)
    group.status = "disabled"
    service.groups.save(group)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )
    original = service._kubectl

    async def fail_route_list(cluster_id, argv, **kwargs):
        if argv[:2] == ["get", "httproute"]:
            return _Result(returncode=1, stderr="route listing forbidden")
        return await original(cluster_id, argv, **kwargs)

    monkeypatch.setattr(service, "_kubectl", fail_route_list)
    operation = await service.reconcile_cluster_gateway(member.cluster_id)
    assert operation.status == "failed"
    assert operation.message == "route listing forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_model", [True, False])
async def test_intentionally_stopped_publication_is_not_probed_or_healed(monkeypatch, stop_model):
    member = _seed_member()
    service = _service(calls=[])
    if stop_model:
        group = service.groups.get(member.group_id)
        group.status = "disabled"
        service.groups.save(group)
    else:
        member.status = "disabled"
        service.members.save(member)
    health, install = AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, "_member_health", health)
    monkeypatch.setattr(service, "_heal_data_plane_locked", install)
    assert await service.probe_members() == 0
    await service._heal_data_plane(member.cluster_id)
    health.assert_not_awaited()
    install.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_during_probe_is_preserved_and_does_not_trigger_healing(monkeypatch):
    member = _seed_member()
    service = _service(calls=[])
    service._target_resolver = lambda _: SimpleNamespace(status="ready")

    async def health(_):
        offline = service.members.get(member.id)
        offline.status = "disabled"
        service.members.save(offline)
        return False, {"components": {"httpRoute": "missing"}}

    monkeypatch.setattr(service, "_member_health", health)
    heal = AsyncMock()
    monkeypatch.setattr(service, "_heal_data_plane", heal)
    await service.probe_members()
    assert service.members.get(member.id).status == "disabled"
    heal.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_prunes_member_whose_execution_is_gone():
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="orphan-model", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-a", base_model="Qwen/Qwen3-8B"
        )
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-gone",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=80,
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )

    def resolver(_execution_id: str):
        raise ExecutionMissingError("execution not found")

    calls: list = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cluster_id: _FakeRunner(calls),
        target_resolver=resolver,
    )
    await service.probe_members()

    assert ModelServiceMemberDao().get(member.id) is None


@pytest.mark.asyncio
async def test_reconcile_removes_route_for_memberless_group(monkeypatch):
    calls: list = []
    service = _service(calls=calls, runner_cls=_ExistingRouteRunner)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["removed"] == 1
    deletes = [c for c in calls if list(c[0][:3]) == ["kubectl", "delete", "httproute"]]
    assert deletes and deletes[0][0][3] == "qwen-empty"
    # Detachment only cleans publication resources; the standalone deployment
    # and its mounted configuration must not be changed or restarted.
    assert not any(list(argv[:2]) in (["kubectl", "patch"], ["kubectl", "rollout"]) for argv, _ in calls)
    assert not any(
        argv[:2] == ["kubectl", "delete"] and argv[2] in {"deploy", "statefulset", "svc", "inferencepool"}
        for argv, _ in calls
    )


@pytest.mark.asyncio
async def test_install_rejects_exposed_port_out_of_range(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="envoy-ai-gateway",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=80,
        ),
    )
    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "failed"
    assert "30000" in operation.message


@pytest.mark.asyncio
async def test_install_rejects_busy_exposed_port(monkeypatch):
    calls: list = []
    service = _service(calls=calls, runner_cls=_BusyPortRunner)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="envoy-ai-gateway",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=30999,
        ),
    )
    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "failed"
    assert "already in use" in operation.message


@pytest.mark.asyncio
async def test_install_falls_back_to_nodeport_without_loadbalancer(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())
    waits = [False, True]

    async def fake_wait(*_args, **_kwargs):
        return waits.pop(0)

    async def fake_unassigned(*_args, **_kwargs):
        return True

    async def fake_existing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_wait_gateway_programmed", fake_wait)
    monkeypatch.setattr(service, "_gateway_address_unassigned", fake_unassigned)
    monkeypatch.setattr(service, "_existing_gateway_service_type", fake_existing)

    operation = await service.install_inference_gateway("cluster-a", "envoy-ai-gateway", install_prerequisites=False)
    assert operation.status == "succeeded"
    assert operation.detail_json["programmed"] is True
    assert operation.detail_json["serviceType"] == "NodePort"
    manifests = [c[1] for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]]
    assert "kind: EnvoyProxy" in manifests[-1]


@pytest.mark.asyncio
async def test_scale_gateway_scales_data_plane_deployment(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="istio",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=None,
        ),
    )
    operation = await service.scale_gateway("cluster-a", 0)
    assert operation.status == "succeeded"
    scales = [c for c in calls if list(c[0][:2]) == ["kubectl", "scale"]]
    assert scales and scales[0][0][2] == "deployment/lens-inference-gateway-istio"


@pytest.mark.asyncio
async def test_resolve_log_target_for_ipp(monkeypatch):
    service = _service(calls=[])
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="istio",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=None,
        ),
    )
    namespace, name = await service.resolve_log_target("cluster-a", component="ipp", namespace=None, name=None)
    assert (namespace, name) == (gateway_namespace_for("cluster-a"), "lens-ipp")


@pytest.mark.asyncio
async def test_install_inference_gateway_rejects_unknown_provider(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: object())

    operation = await service.install_inference_gateway("cluster-a", "traefik")
    assert operation.status == "failed"
    assert "unsupported gateway provider" in operation.message


@pytest.mark.asyncio
async def test_reconcile_cluster_gateway_applies_model_routes(monkeypatch):
    _seed_member(cluster_id="cluster-a")
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["routes"] == 1
    applies = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]]
    route_manifest = applies[-1][1]
    assert "kind: HTTPRoute" in route_manifest
    assert "kind: ConfigMap" in route_manifest
    assert "name: qwen" in route_manifest
    assert "X-Gateway-Base-Model-Name" in route_manifest


@pytest.mark.asyncio
async def test_reconcile_derives_pool_name_for_legacy_member(monkeypatch):
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="legacy-model",
            model_ref="Qwen/Qwen3-0.6B",
            cluster_id="cluster-a",
            base_model="Qwen/Qwen3-0.6B",
        )
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-legacy",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="optimized-baseline-epp",
            target_port=8000,
            epp_ref="optimized-baseline-epp",
            endpoint_kind="llm-d-epp",
        )
    )
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "succeeded"
    assert operation.detail_json["routes"] == 1
    route_manifest = [c for c in calls if list(c[0][:3]) == ["kubectl", "apply", "-f"]][-1][1]
    assert "name: optimized-baseline" in route_manifest


@pytest.mark.asyncio
async def test_status_reports_gateway_state(monkeypatch):
    _seed_member(cluster_id="cluster-a")
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        name="Cluster A",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(
        "llm_d_lens.cluster.registry.list_clusters", lambda: [SimpleNamespace(id="cluster-a", name="Cluster A")]
    )

    status = await service.status()
    assert len(status.clusters) == 1
    cluster_status = status.clusters[0]
    assert cluster_status.gateway_provider == "istio"
    assert cluster_status.gateway_state == "installed"
    assert cluster_status.gateway_ready is True
    assert cluster_status.members[0].pool_name == "qwen"
    # A gateway operation is not created merely by reading status.
    assert cluster_status.last_operation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("disappeared", [False, True])
async def test_status_keeps_registered_empty_models_but_never_probes_orphan_clusters(monkeypatch, disappeared):
    ModelServiceGroupDao().create(ModelServiceGroup(name="empty-model", model_ref="provider", cluster_id="registered"))
    ModelServiceGroupDao().create(ModelServiceGroup(name="orphan-empty", model_ref="provider", cluster_id="deleted"))
    _seed_member(cluster_id="deleted-with-members")
    cluster = SimpleNamespace(
        id="registered",
        name="Registered cluster",
        gateway_provider=None,
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
    )
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    looked_up = []

    def get_cluster(cluster_id):
        looked_up.append(cluster_id)
        assert cluster_id == "registered"
        return None if disappeared else cluster

    monkeypatch.setattr(gateway_ops_module, "get_cluster", get_cluster)
    calls = []
    service = _service(calls=calls)
    status = await service.status()
    assert [item.cluster_id for item in status.clusters] == ([] if disappeared else ["registered"])
    assert looked_up == ["registered"]
    if disappeared:
        assert calls == []
    else:
        assert status.clusters[0].members == []


class _DriftedProviderRunner(_FakeRunner):
    """Gateway is istio-classed, but no provider-specific Service lookup finds it
    (provider mislabeled as envoy-ai-gateway, and the `-istio` Service is absent)."""

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "svc"] and "-l" in argv:
            return _Result(stdout="")  # envoy labelled lookup: not found
        if list(argv[:3]) == ["kubectl", "get", "svc"] and argv[3].endswith("-istio"):
            return _Result(returncode=1, stderr="not found")
        if any("status.addresses[0].value" in str(arg) for arg in argv):
            return _Result(stdout="lens-inference-gateway-istio.lens-gateway.svc.cluster.local")
        if any("spec.gatewayClassName" in str(arg) for arg in argv):
            return _Result(stdout="istio")
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@pytest.mark.asyncio
async def test_gateway_service_falls_back_to_discovery_on_provider_drift():
    """Regression: a wrong stored provider must not skip the public exposure."""
    calls: list = []
    service = _service(calls=calls, runner_cls=_DriftedProviderRunner)
    target = await service._gateway_service("cluster-a", "lens-gateway", "lens-inference-gateway", "envoy-ai-gateway")
    assert target == ("lens-gateway", "lens-inference-gateway-istio")


@pytest.mark.asyncio
async def test_actual_gateway_provider_maps_gateway_class():
    calls: list = []
    service = _service(calls=calls, runner_cls=_DriftedProviderRunner)
    assert await service._actual_gateway_provider("cluster-a", "lens-gateway", "lens-inference-gateway") == "istio"


@pytest.mark.asyncio
async def test_gateway_service_falls_back_when_istio_service_is_absent():
    """Regression: provider says istio but no such Service -> discover the real one."""
    calls: list = []
    service = _service(calls=calls, runner_cls=_DriftedProviderRunner)
    target = await service._gateway_service("cluster-a", "lens-gateway", "lens-inference-gateway", "istio")
    assert target == ("lens-gateway", "lens-inference-gateway-istio")


class _IppConfigRunner(_FakeRunner):
    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "configmap"]:
            return _Result(
                stdout=(
                    '{"custom-ipp-config.yaml":"plugins: []\\n",'
                    '"default-ipp-config.yaml":"plugins:\\n- type: body-field-to-header\\n"}'
                )
            )
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@pytest.mark.asyncio
async def test_get_ipp_config_reads_live_configmap(monkeypatch):
    """IPP config is read live from the cluster ConfigMap (not Lens storage)."""
    calls: list = []
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway"),
    )
    service = _service(calls=calls, runner_cls=_IppConfigRunner)
    cfg = await service.get_ipp_config("cluster-a")
    assert cfg["custom"] is True
    assert cfg["config"] == "plugins: []\n"
    assert "body-field-to-header" in cfg["default"]


@pytest.mark.asyncio
async def test_get_ipp_config_falls_back_to_default_when_no_custom(monkeypatch):
    calls: list = []

    class R(_IppConfigRunner):
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                return _Result(stdout='{"default-ipp-config.yaml":"plugins:\\n- type: x\\n"}')
            return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)

    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway"),
    )
    service = _service(calls=calls, runner_cls=R)
    cfg = await service.get_ipp_config("cluster-a")
    assert cfg["custom"] is False
    assert cfg["config"] == cfg["default"]


class _TlsEppRunner(_FakeRunner):
    """A live EPP and the standalone Envoy ConfigMap it mounts."""

    def __init__(self, calls, *, secure=True, proxy_secure=True, kind="Deployment"):
        super().__init__(calls)
        self.workload = {
            "apiVersion": "apps/v1",
            "kind": kind,
            "metadata": {"name": "qwen-epp", "uid": "router-uid"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "envoy-proxy", "volumeMounts": [{"name": "proxy", "mountPath": "/etc/envoy"}]},
                            {"name": "epp", "args": ["--pool-name", "qwen", f"--secure-serving={str(secure).lower()}"]},
                        ],
                        "volumes": [{"name": "proxy", "configMap": {"name": "qwen-proxy-config"}}],
                    }
                }
            },
        }
        cluster = {"name": "ext_proc", "health_checks": [{"grpc_health_check": {}}]}
        if proxy_secure:
            cluster["transport_socket"] = {"name": "envoy.transport_sockets.tls"}
            cluster["health_checks"][0]["tls_options"] = {"alpn_protocols": ["h2"]}
        self.configmap = {"data": {"envoy.yaml": yaml.safe_dump({"static_resources": {"clusters": [cluster]}})}}
        self.patches = []
        self.prepared = []

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:2]) == ["kubectl", "get"] and argv[2] in {"deploy", "statefulset"}:
            expected = "deploy" if self.workload["kind"] == "Deployment" else "statefulset"
            return _Result(stdout=json.dumps(self.workload) if argv[2] == expected else "")
        if list(argv[:3]) == ["kubectl", "get", "configmap"]:
            return _Result(stdout=json.dumps(self.configmap))
        if list(argv[:2]) == ["kubectl", "patch"]:
            self.patches.append(json.loads(argv[-1]))
            return _Result(stdout="patched")
        if list(argv[:3]) == ["kubectl", "apply", "-f"]:
            self.prepared.extend(yaml.safe_load_all(input))
            return _Result(stdout="applied")
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
@pytest.mark.parametrize(
    "secure,proxy_secure,target", [(True, True, False), (False, True, False), (False, False, True)]
)
async def test_epp_transport_updates_both_sides_together(kind, secure, proxy_secure, target):
    runner = _TlsEppRunner([], secure=secure, proxy_secure=proxy_secure, kind=kind)
    service = GatewayOpsService(runner_factory=lambda _: runner)
    assert await service._ensure_epp_transport("cluster-a", "ns", "qwen-epp", secure_serving=target)
    assert len(runner.patches) == len(runner.prepared) == 1
    patch = runner.patches[0]
    assert patch[0]["op"] == "test"
    refs = [p for p in patch if p["path"].endswith("/configMap/name")]
    assert refs[0]["value"] == runner.prepared[0]["metadata"]["name"]
    assert refs[0]["value"] != "qwen-proxy-config"
    clusters = yaml.safe_load(runner.prepared[0]["data"]["envoy.yaml"])["static_resources"]["clusters"]
    assert ("transport_socket" in clusters[0]) is target
    assert ("tls_options" in clusters[0]["health_checks"][0]) is target
    args = [p for p in patch if p["path"].endswith("/args")]
    if secure != target:
        assert f"--secure-serving={str(target).lower()}" in args[0]["value"]
    else:
        assert not args  # Repair an already-broken proxy even if EPP is correct.
    actions = [list(argv[:2]) for argv, _ in runner.calls]
    assert actions.index(["kubectl", "apply"]) < actions.index(["kubectl", "patch"])
    assert ["kubectl", "rollout"] in actions


@pytest.mark.asyncio
@pytest.mark.parametrize("secure", [False, True])
async def test_epp_transport_is_idempotent_when_both_sides_match(secure):
    runner = _TlsEppRunner([], secure=secure, proxy_secure=secure)
    service = GatewayOpsService(runner_factory=lambda _: runner)
    assert not await service._ensure_epp_transport("cluster-a", "ns", "qwen-epp", secure_serving=secure)
    assert not runner.patches and not runner.prepared


@pytest.mark.asyncio
async def test_epp_transport_rejects_unknown_proxy_before_patching_epp():
    runner = _TlsEppRunner([])
    runner.configmap = {"data": {"custom.yaml": "unknown: true"}}
    service = GatewayOpsService(runner_factory=lambda _: runner)
    with pytest.raises(gateway_ops_module.GatewayOpsError, match="unsupported"):
        await service._ensure_epp_transport("cluster-a", "ns", "qwen-epp", secure_serving=False)
    assert not runner.patches


@pytest.mark.asyncio
async def test_epp_transport_preparation_failure_does_not_patch_epp(monkeypatch):
    runner = _TlsEppRunner([])
    service = GatewayOpsService(runner_factory=lambda _: runner)
    monkeypatch.setattr(service, "_apply_documents", AsyncMock(return_value=_Result(returncode=1, stderr="denied")))
    with pytest.raises(gateway_ops_module.GatewayOpsError, match="denied"):
        await service._ensure_epp_transport("cluster-a", "ns", "qwen-epp", secure_serving=False)
    assert not runner.patches


@pytest.mark.asyncio
async def test_install_ipp_restarts_rollout_to_apply_config(monkeypatch):
    """A config change forces a restart so it is loaded even if inotify fails."""
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        ipp_version=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.install_ipp("cluster-a", config="plugins: []")

    assert operation.status == "succeeded"
    assert any(list(call[0][:3]) == ["kubectl", "rollout", "restart"] for call in calls)


@pytest.mark.asyncio
async def test_apply_node_inotify_limit_applies_daemonset(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        ipp_version=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.apply_node_inotify_limit("cluster-a", 8192)

    assert operation.status == "succeeded"
    apply_calls = [call for call in calls if list(call[0][:3]) == ["kubectl", "apply", "-f"]]
    assert apply_calls
    manifest = apply_calls[-1][1]
    assert "kind: DaemonSet" in manifest
    assert "fs.inotify.max_user_instances=8192" in manifest


@pytest.mark.asyncio
async def test_apply_node_inotify_limit_rejects_zero(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        ipp_version=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    operation = await service.apply_node_inotify_limit("cluster-a", 0)
    assert operation.status == "failed"


class _ComponentRunner(_FakeRunner):
    """Canned HTTPRoute/InferencePool/Service/EndpointSlice reads."""

    def __init__(
        self, calls: list, *, model_server_ready: bool = True, pool_statuses: dict[str, dict] | None = None
    ) -> None:
        super().__init__(calls)
        self._model_server_ready = model_server_ready
        self._pool_statuses = pool_statuses or {}

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "endpoints"]:
            return _Result(stdout='{"subsets":[{"addresses":[{"ip":"1.2.3.4"}]}]}')
        if list(argv[:3]) == ["kubectl", "get", "httproute"]:
            return _Result(stdout='{"status":{"parents":[{"conditions":[{"type":"Accepted","status":"True"}]}]}}')
        if list(argv[:3]) == ["kubectl", "get", "inferencepool"]:
            namespace = argv[argv.index("-n") + 1]
            status = self._pool_statuses.get(
                namespace, {"parents": [{"conditions": [{"type": "Accepted", "status": "True"}]}]}
            )
            return _Result(
                stdout=json.dumps({"spec": {"selector": {"matchLabels": {"llm-d.ai/guide": "pool"}}}, "status": status})
            )
        if list(argv[:3]) == ["kubectl", "get", "pods"]:
            ready = "True" if self._model_server_ready else "False"
            return _Result(
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "status": {
                                    "phase": "Running",
                                    "conditions": [{"type": "Ready", "status": ready}],
                                }
                            }
                        ]
                    }
                )
            )
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


def _seed_component_member(name: str, *, weight: int = 1) -> ModelServiceMember:
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name=name, model_ref=name, cluster_id="cluster-a", base_model=name)
    )
    return ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id=f"exec-{name}",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="pool-epp",
            target_port=80,
            pool_name="ns/pool",
            epp_ref="pool-epp",
            endpoint_kind="llm-d-epp",
            weight=weight,
        )
    )


@pytest.mark.asyncio
async def test_probe_members_reports_per_component_health(monkeypatch):
    member = _seed_component_member("percomp-ready")
    calls: list = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cid: _ComponentRunner(calls),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"),
    )
    await service.probe_members()

    saved = ModelServiceMemberDao().get(member.id)
    assert saved.status == "active"
    assert saved.health_json["components"] == {
        "epp": "ready",
        "httpRoute": "ready",
        "inferencePool": "ready",
        "modelServer": "ready",
        "ipp": "ready",
        "serving": "n/a",
    }


@pytest.mark.asyncio
async def test_serving_probe_uses_public_service_name_not_model_ref(monkeypatch):
    payloads: list[dict] = []
    sent_headers: list[dict] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, json, headers=None):  # noqa: A002
            payloads.append(json)
            sent_headers.append(dict(headers or {}))
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"),
    )
    monkeypatch.setattr(gateway_ops_module, "gateway_base_url", lambda *_args: "http://gateway")
    monkeypatch.setattr(gateway_ops_module, "local_public_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _Client)
    group = SimpleNamespace(name="qwen3-0.6b", model_ref="Qwen/Qwen3-0.6B")

    state = await GatewayOpsService()._gateway_serving_state(SimpleNamespace(cluster_id="cluster-a"), group)

    assert state == "ready"
    assert payloads[0]["model"] == "qwen3-0.6b"
    assert sent_headers[0]["X-Gateway-Base-Model-Name"] == "qwen3-0.6b"


@pytest.mark.asyncio
async def test_standby_provider_does_not_require_route_when_last_positive_provider_is_offline(monkeypatch):
    standby = _seed_component_member("no-routed-provider", weight=0)
    members = ModelServiceMemberDao()
    members.create(
        standby.model_copy(
            update={
                "id": "offline-provider",
                "execution_id": "offline-execution",
                "status": "disabled",
                "weight": 90,
            }
        )
    )
    calls = []
    service = GatewayOpsService(
        runner_factory=lambda _: _ComponentRunner(calls, pool_statuses={"ns": {}}),
        target_resolver=lambda _: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _: SimpleNamespace(gateway_namespace="lens-gateway"))
    heal = AsyncMock()
    monkeypatch.setattr(service, "_heal_data_plane", heal)
    await service.probe_members()
    saved = members.get(standby.id)
    assert saved.status == "active"
    assert saved.health_json["components"]["httpRoute"] == "standby"
    assert saved.health_json["components"]["inferencePool"] == "standby"
    assert saved.health_json["components"]["serving"] == "n/a"
    assert members.get("offline-provider").status == "disabled"
    assert not any(argv[:3] == ["kubectl", "get", "httproute"] for argv, _ in calls)
    heal.assert_not_awaited()


def test_member_is_routable_excludes_only_fully_unavailable_providers():
    from llm_d_lens.model_service.gateway_ops import member_is_routable

    base = SimpleNamespace(
        status="active",
        weight=1,
        endpoint_kind="llm-d-epp",
        pool_name="ns/pool",
        epp_ref="pool-epp",
        target_namespace="ns",
        target_service="pool-epp",
        health_json={
            "components": {"epp": "ready", "inferencePool": "ready", "modelServer": "ready", "serving": "ready"}
        },
    )
    assert member_is_routable(base)
    for component in ("epp", "inferencePool", "modelServer"):
        assert not member_is_routable(
            SimpleNamespace(
                **{
                    **vars(base),
                    "health_json": {
                        **base.health_json,
                        "components": {**base.health_json["components"], component: "missing"},
                    },
                }
            )
        )
    # A missing route is not a reason to drop the provider: the next reconcile
    # must be able to recreate that route and heal the provider.
    assert member_is_routable(
        SimpleNamespace(
            **{
                **vars(base),
                "health_json": {
                    **base.health_json,
                    "components": {**base.health_json["components"], "httpRoute": "missing"},
                },
            }
        )
    )
    assert not member_is_routable(SimpleNamespace(**{**vars(base), "weight": 0}))
    assert not member_is_routable(SimpleNamespace(**{**vars(base), "status": "disabled"}))


def test_data_plane_degraded_detects_broken_paths():
    assert gateway_ops_module._data_plane_degraded({"ipp": "missing"})
    assert gateway_ops_module._data_plane_degraded({"serving": "degraded"})
    assert gateway_ops_module._data_plane_degraded({"serving": "unreachable"})
    assert gateway_ops_module._data_plane_degraded({"httpRoute": "missing"})
    assert gateway_ops_module._data_plane_degraded({"inferencePool": "degraded"})
    assert not gateway_ops_module._data_plane_degraded({"epp": "ready", "ipp": "ready", "serving": "ready"})
    # A model server still starting is not a data-plane failure.
    assert not gateway_ops_module._data_plane_degraded({"modelServer": "missing"})
    assert not gateway_ops_module._data_plane_degraded({"inferencePool": "standby"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("weight", "pool_status", "model_ready", "expected_pool", "healthy", "heals"),
    [
        (0, {}, True, "standby", True, False),
        (0, {"parents": []}, False, "standby", False, False),
        (1, {}, True, "degraded", False, True),
        (
            0,
            {"parents": [{"conditions": [{"type": "Accepted", "status": "False"}]}]},
            True,
            "degraded",
            False,
            True,
        ),
        (
            0,
            {"parents": [{"conditions": [{"type": "Accepted", "status": "True"}]}]},
            True,
            "ready",
            True,
            False,
        ),
    ],
)
async def test_probe_distinguishes_standby_pools_from_failures(
    monkeypatch, weight, pool_status, model_ready, expected_pool, healthy, heals
):
    member = _seed_component_member("standby", weight=weight)
    calls: list = []
    service = GatewayOpsService(
        runner_factory=lambda _: _ComponentRunner(
            calls, model_server_ready=model_ready, pool_statuses={"ns": pool_status}
        ),
        target_resolver=lambda _: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"),
    )
    heal = AsyncMock()
    monkeypatch.setattr(service, "_heal_data_plane", heal)

    await service.probe_members()

    saved = ModelServiceMemberDao().get(member.id)
    assert saved.weight == weight
    assert saved.status == ("active" if healthy else "unhealthy")
    assert saved.health_json["ready"] == healthy
    assert saved.health_json["components"]["inferencePool"] == expected_pool
    assert saved.health_json["components"]["modelServer"] == ("ready" if model_ready else "missing")
    if heals:
        heal.assert_awaited_once_with("cluster-a")
    else:
        heal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "state"),
    [(_Result(returncode=1), "missing"), (_Result(stdout="not JSON"), "degraded")],
)
async def test_zero_weight_does_not_mask_missing_or_invalid_pool(monkeypatch, result, state):
    service = _service(calls=[])
    monkeypatch.setattr(service, "_kubectl", AsyncMock(return_value=result))
    assert await service._pool_state("cluster-a", "ns", "pool", routed=False) == (state, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("weights", [(1, 0), (0, 1), (90, 10)])
async def test_repeated_probe_and_reconcile_preserve_namespaced_pool_weights(monkeypatch, weights):
    import yaml

    first = _seed_component_member("shared-pool-name", weight=weights[0])
    second = ModelServiceMemberDao().create(
        first.model_copy(
            update={
                "id": "second-member",
                "execution_id": "second-execution",
                "target_namespace": "ns-b",
                "pool_name": "ns-b/pool",
                "weight": weights[1],
            }
        )
    )
    calls: list = []
    pool_statuses = {member.target_namespace: {} for member in (first, second) if member.weight == 0}
    service = GatewayOpsService(
        runner_factory=lambda _: _ComponentRunner(calls, pool_statuses=pool_statuses),
        target_resolver=lambda _: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )
    heal = AsyncMock()
    monkeypatch.setattr(service, "_heal_data_plane", heal)
    expected = {(member.target_namespace, "pool"): member.weight for member in (first, second) if member.weight > 0}
    for _ in range(2):
        await service.probe_members()
        operation = await service.reconcile_cluster_gateway("cluster-a")
        assert operation.status == "succeeded"
        manifest = [call[1] for call in calls if list(call[0][:3]) == ["kubectl", "apply", "-f"]][-1]
        routes = [doc for doc in yaml.safe_load_all(manifest) if doc and doc["kind"] == "HTTPRoute"]
        assert len(routes) == 1
        refs = routes[0]["spec"]["rules"][0]["backendRefs"]
        assert {(ref["namespace"], ref["name"]): ref["weight"] for ref in refs} == expected
        for member in (first, second):
            saved = ModelServiceMemberDao().get(member.id)
            assert saved.status == "active"
            assert saved.weight == member.weight
    heal.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_heals_secure_serving_on_an_unhealthy_member(monkeypatch):
    """An unhealthy probe may be caused by TLS on the EPP, so status must not suppress healing."""
    member = _seed_member("cluster-unhealthy")
    ModelServiceMemberDao().save_health(member.model_copy(update={"status": "unhealthy"}))
    service = _service(calls=[])
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )
    heal = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_ensure_epp_transport", heal)

    operation = await service.reconcile_cluster_gateway("cluster-unhealthy")

    assert operation.status == "succeeded"
    heal.assert_awaited_once_with("cluster-unhealthy", "ns", "qwen-epp", secure_serving=False, on_log=None)


@pytest.mark.asyncio
async def test_heal_data_plane_uses_no_actor_so_the_operation_can_be_recorded(monkeypatch):
    """`created_by_user_id` is a foreign key into `users`; a literal placeholder
    actor (for example "auto-heal") violates it and silently breaks the
    self-heal path, so automatic runs must pass actor=None like every other
    background reconcile (see GatewayOpsService._finish's `is_automatic` check).
    """
    _seed_member("cluster-heal")
    calls: list[tuple[str, object]] = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
    )

    async def fake_install_cluster_gateway(cluster_id, *, actor=None, install_prerequisites=True):
        assert install_prerequisites is False
        calls.append(("gateway", actor))

    async def fake_install_ipp(cluster_id, *, actor=None):
        calls.append(("ipp", actor))

    monkeypatch.setattr(service, "install_cluster_gateway", fake_install_cluster_gateway)
    monkeypatch.setattr(service, "install_ipp", fake_install_ipp)

    await service._heal_data_plane("cluster-heal")

    assert calls == [("gateway", None), ("ipp", None)]


@pytest.mark.asyncio
async def test_probe_members_flags_missing_model_server(monkeypatch):
    member = _seed_component_member("percomp-modelserver-down")
    calls: list = []
    service = GatewayOpsService(
        operations=GatewayOperationDao(),
        members=ModelServiceMemberDao(),
        groups=ModelServiceGroupDao(),
        runner_factory=lambda _cid: _ComponentRunner(calls, model_server_ready=False),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"),
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(service, "reconcile_cluster", reconcile)
    await service.probe_members()

    reconcile.assert_awaited_once_with("cluster-a")
    saved = ModelServiceMemberDao().get(member.id)
    assert saved.status == "unhealthy"
    assert saved.health_json["components"]["modelServer"] == "missing"
    assert saved.health_json["components"]["epp"] == "ready"


def test_resolve_authz_endpoint_uses_live_serving_port(monkeypatch):
    monkeypatch.setattr(gateway_ops_module.hostinfo, "current_serving_port", lambda: 8084)
    assert gateway_ops_module.resolve_authz_endpoint(None) == (None, None)
    assert gateway_ops_module.resolve_authz_endpoint("  ") == (None, None)
    assert gateway_ops_module.resolve_authz_endpoint("10.0.0.5") == ("10.0.0.5", 8084)
    # A stray user-supplied port is ignored: Lens always uses its own.
    assert gateway_ops_module.resolve_authz_endpoint("10.0.0.5:9999") == ("10.0.0.5", 8084)


def test_resolve_authz_endpoint_does_not_guess_before_first_request(monkeypatch):
    monkeypatch.setattr(gateway_ops_module.hostinfo, "current_serving_port", lambda: None)
    assert gateway_ops_module.resolve_authz_endpoint("10.0.0.5") == ("10.0.0.5", None)


@pytest.mark.asyncio
async def test_install_refuses_to_write_authz_endpoint_without_live_port(monkeypatch):
    calls: list = []
    service = _service(calls=calls)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(gateway_authz_host="10.0.0.9", gateway_namespace="lens-gateway"),
    )
    monkeypatch.setattr(gateway_ops_module.hostinfo, "current_serving_port", lambda: None)

    operation = await service.install_inference_gateway("cluster-a", "istio", install_prerequisites=False)

    assert operation.status == "failed"
    assert "serving port is unknown" in operation.message
    assert not [argv for argv, _ in calls if list(argv[:3]) == ["kubectl", "apply", "-f"]]


@pytest.mark.asyncio
async def test_model_server_health_checks_pods_selected_by_the_inference_pool():
    calls: list[list[str]] = []
    service = GatewayOpsService(operations=GatewayOperationDao())

    async def kubectl(_cluster_id, argv, **_kwargs):
        calls.append(argv)
        return _Result(
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "status": {
                                "phase": "Running",
                                "conditions": [{"type": "Ready", "status": "True"}],
                            }
                        }
                    ]
                }
            )
        )

    service._kubectl = kubectl

    assert await service._model_server_state("cluster-a", "ns", "llm-d.ai/guide=pool") == "ready"
    assert calls == [["get", "pods", "-n", "ns", "-l", "llm-d.ai/guide=pool", "-o", "json"]]


@pytest.mark.asyncio
async def test_managed_rewrites_are_scoped_to_this_lens_cluster():
    service = GatewayOpsService(operations=GatewayOperationDao())
    items = [
        {
            "metadata": {
                "name": "new-own",
                "namespace": "ns-own",
                "labels": {
                    REWRITE_MANAGED_LABEL: "true",
                    MODEL_SERVICE_LABEL: "ours",
                    CLUSTER_LABEL: "cluster-a",
                },
            }
        },
        {
            "metadata": {
                "name": "foreign-labeled",
                "namespace": "ns-other",
                "labels": {
                    REWRITE_MANAGED_LABEL: "true",
                    MODEL_SERVICE_LABEL: "theirs",
                    CLUSTER_LABEL: "cluster-b",
                },
            }
        },
        {
            "metadata": {
                "name": "legacy-foreign",
                "namespace": "ns-other",
                "labels": {MANAGED_LABEL: MANAGED_VALUE, MODEL_SERVICE_LABEL: "theirs"},
            }
        },
    ]

    async def kubectl(_cluster_id, argv, **_kwargs):
        assert list(argv[:2]) == ["get", "inferencemodelrewrite"]
        return _Result(stdout=json.dumps({"items": items}))

    service._kubectl = kubectl

    assert await service._managed_model_rewrites("cluster-a") == {("ns-own", "new-own")}


def test_automatic_gateway_operations_do_not_write_audit_events():
    service = GatewayOpsService(operations=GatewayOperationDao())
    audit = Mock()
    service._auth = SimpleNamespace(audit=audit)

    operation = service._record(GatewayOperation(kind="install-gateway", cluster_id="cluster-a"))
    service._finish(operation, ok=True, message="reconciled")

    audit.assert_not_called()


class _ModelRewriteRunner(_FakeRunner):
    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append((argv, input))
        if list(argv[:3]) == ["kubectl", "get", "crd"]:
            return _Result(stdout="inferencemodelrewrites.llm-d.ai\n")
        if list(argv[:3]) == ["kubectl", "get", "inferencemodelrewrite"]:
            return _Result(stdout='{"items": []}')
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@pytest.mark.asyncio
async def test_reconcile_maps_public_model_name_to_served_model_name(monkeypatch):
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="qwen3-0.6b",
            model_ref="Qwen/Qwen3-0.6B",
            cluster_id="cluster-a",
            served_name="Qwen/Qwen3-0.6B",
            base_model="qwen3-0.6b",
        )
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-alias",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=80,
            pool_name="ns/qwen-pool",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )
    calls: list = []
    service = _service(calls=calls, runner_cls=_ModelRewriteRunner)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )

    operation = await service.reconcile_cluster_gateway("cluster-a")

    assert operation.status == "succeeded"
    rewrites = [
        item[1]
        for item in calls
        if list(item[0][:3]) == ["kubectl", "apply", "-f"] and "kind: InferenceModelRewrite" in (item[1] or "")
    ]
    assert rewrites
    manifest = rewrites[-1]
    assert "value: qwen3-0.6b" in manifest
    assert "modelRewrite: Qwen/Qwen3-0.6B" in manifest
    assert "name: qwen-pool" in manifest
    assert "lens.llm-d.ai/cluster-id: cluster-a" in manifest
    assert "lens.llm-d.ai/rewrite-managed: 'true'" in manifest


def _seed_epp_member(
    *, cluster_id: str = "cluster-a", namespace: str = "ns", epp_ref: str = "qwen-epp", suffix: str = "a"
) -> ModelServiceMember:
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name=f"qwen-{suffix}",
            model_ref="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
            base_model="Qwen/Qwen3-8B",
        )
    )
    return ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id=f"exec-{suffix}",
            cluster_id=cluster_id,
            target_namespace=namespace,
            target_service=epp_ref,
            target_port=8000,
            pool_name="qwen",
            epp_ref=epp_ref,
            endpoint_kind="llm-d-epp",
        )
    )


def _workload(annotations: dict | None = None) -> str:
    return json.dumps({"spec": {"template": {"metadata": {"annotations": annotations or {}}}}})


@pytest.mark.asyncio
async def test_repair_router_rewrites_patches_only_workloads_with_a_stale_marker():
    member = _seed_epp_member()
    service = GatewayOpsService()
    patches: list[list[str]] = []

    async def kubectl(cluster_id, argv, **_kwargs):
        if list(argv[:2]) == ["get", "crd"]:
            return _Result(stdout="uid-1/7")
        if list(argv[:3]) == ["get", "deploy", member.epp_ref]:
            return _Result(stdout=_workload({REWRITE_CRD_ANNOTATION: "uid-1/6"}))
        if argv[0] == "patch":
            patches.append(list(argv))
            return _Result()
        return _Result(returncode=1)

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result == {"ok": True, "restarted": ["deploy/qwen-epp"], "message": ""}
    assert "--type=merge" in patches[0]
    patch = json.loads(patches[0][patches[0].index("-p") + 1])
    assert patch["spec"]["template"]["metadata"]["annotations"][REWRITE_CRD_ANNOTATION] == "uid-1/7"


@pytest.mark.asyncio
async def test_repair_router_rewrites_skips_a_matching_marker():
    member = _seed_epp_member()
    service = GatewayOpsService()
    calls: list[list[str]] = []

    async def kubectl(cluster_id, argv, **_kwargs):
        calls.append(list(argv))
        if list(argv[:2]) == ["get", "crd"]:
            return _Result(stdout="uid-1/7")
        if list(argv[:3]) == ["get", "deploy", member.epp_ref]:
            return _Result(stdout=_workload({REWRITE_CRD_ANNOTATION: "uid-1/7"}))
        return _Result(returncode=1)

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result == {"ok": True, "restarted": [], "message": ""}
    assert not any(call[0] == "patch" for call in calls)


@pytest.mark.asyncio
async def test_repair_router_rewrites_falls_back_to_a_statefulset():
    member = _seed_epp_member()
    service = GatewayOpsService()
    patches: list[list[str]] = []

    async def kubectl(cluster_id, argv, **_kwargs):
        if list(argv[:2]) == ["get", "crd"]:
            return _Result(stdout="uid-1/7")
        if list(argv[:3]) == ["get", "deploy", member.epp_ref]:
            return _Result(returncode=1, stderr="deployments.apps not found")
        if list(argv[:3]) == ["get", "statefulset", member.epp_ref]:
            return _Result(stdout=_workload())
        if argv[0] == "patch":
            patches.append(list(argv))
            return _Result()
        return _Result(returncode=1)

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result == {"ok": True, "restarted": ["statefulset/qwen-epp"], "message": ""}
    assert patches[0][:3] == ["patch", "statefulset", "qwen-epp"]


@pytest.mark.asyncio
async def test_repair_router_rewrites_reports_individual_failures_without_raising():
    first = _seed_epp_member(epp_ref="qwen-a", suffix="a")
    second = _seed_epp_member(epp_ref="qwen-b", suffix="b")
    service = GatewayOpsService()
    patches: list[list[str]] = []

    async def kubectl(cluster_id, argv, **_kwargs):
        if list(argv[:2]) == ["get", "crd"]:
            return _Result(stdout="uid-1/7")
        if list(argv[:3]) == ["get", "deploy", first.epp_ref]:
            return _Result(stdout=_workload())
        if list(argv[:3]) == ["get", "deploy", second.epp_ref]:
            return _Result(returncode=1, stderr="not found")
        if list(argv[:3]) == ["get", "statefulset", second.epp_ref]:
            return _Result(returncode=1, stderr="not found")
        if argv[0] == "patch":
            patches.append(list(argv))
            return _Result()
        return _Result(returncode=1)

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result["ok"] is False
    assert result["restarted"] == ["deploy/qwen-a"]
    assert "qwen-b" in result["message"]
    assert "workload not found" in result["message"]


@pytest.mark.asyncio
async def test_repair_router_rewrites_falls_back_to_a_statefulset_after_a_kubectl_error():
    member = _seed_epp_member()
    service = GatewayOpsService()
    patches: list[list[str]] = []

    async def kubectl(cluster_id, argv, **_kwargs):
        if list(argv[:2]) == ["get", "crd"]:
            return _Result(stdout="uid-1/7")
        if list(argv[:3]) == ["get", "deploy", member.epp_ref]:
            raise OSError("connection reset")
        if list(argv[:3]) == ["get", "statefulset", member.epp_ref]:
            return _Result(stdout=_workload())
        if argv[0] == "patch":
            patches.append(list(argv))
            return _Result()
        return _Result(returncode=1)

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result == {"ok": True, "restarted": ["statefulset/qwen-epp"], "message": ""}
    assert patches[0][:3] == ["patch", "statefulset", "qwen-epp"]


@pytest.mark.asyncio
async def test_repair_router_rewrites_reports_a_workload_lookup_error():
    member = _seed_epp_member()
    service = GatewayOpsService()

    async def kubectl(cluster_id, argv, **_kwargs):
        if list(argv[:2]) == ["get", "crd"]:
            return _Result(stdout="uid-1/7")
        if list(argv[:3]) == ["get", "deploy", member.epp_ref]:
            return _Result(returncode=1, stderr="forbidden")
        if list(argv[:3]) == ["get", "statefulset", member.epp_ref]:
            return _Result(returncode=1, stderr="not found")
        return _Result(returncode=1)

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result["ok"] is False
    assert result["restarted"] == []
    assert "forbidden" in result["message"]
    assert "not found" in result["message"]


@pytest.mark.asyncio
async def test_repair_router_rewrites_is_a_no_op_without_members():
    service = GatewayOpsService()
    calls: list[list[str]] = []

    async def kubectl(cluster_id, argv, **_kwargs):
        calls.append(list(argv))
        return _Result(stdout="uid-1/7")

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result == {"ok": True, "restarted": [], "message": ""}
    assert len(calls) == 1
    assert calls[0][:2] == ["get", "crd"]


@pytest.mark.asyncio
async def test_repair_router_rewrites_reports_a_missing_crd():
    service = GatewayOpsService()

    async def kubectl(cluster_id, argv, **_kwargs):
        return _Result(
            returncode=1,
            stderr='customresourcedefinitions.apiextensions.k8s.io "inferencemodelrewrites.llm-d.ai" not found',
        )

    service._kubectl = kubectl

    result = await service.repair_router_rewrites("cluster-a")

    assert result["ok"] is False
    assert result["restarted"] == []
    assert "not found" in result["message"]


def _stale_discovery_result(kind: str = "InferenceModelRewrite") -> _Result:
    return _Result(
        returncode=1,
        stderr=f'no matches for kind "{kind}" in version "llm-d.ai/v1alpha2"; ensure CRDs are installed first',
    )


def _gateway_cluster() -> SimpleNamespace:
    return SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )


def _intercept_apply(monkeypatch, service: GatewayOpsService, *, marker: str, responses: list[_Result]) -> list[str]:
    original = service._kubectl
    attempts: list[str] = []
    pending = list(responses)

    async def kubectl(cluster_id, argv, **kwargs):
        manifest = kwargs.get("input") or ""
        if list(argv) == ["apply", "-f", "-"] and marker in manifest:
            attempts.append(manifest)
            return pending.pop(0)
        return await original(cluster_id, argv, **kwargs)

    monkeypatch.setattr(service, "_kubectl", kubectl)
    return attempts


def _seed_model_rewrite_member() -> ModelServiceMember:
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="qwen3-0.6b",
            model_ref="Qwen/Qwen3-0.6B",
            cluster_id="cluster-a",
            served_name="Qwen/Qwen3-0.6B",
            base_model="qwen3-0.6b",
        )
    )
    return ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-alias",
            cluster_id="cluster-a",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=80,
            pool_name="ns/qwen-pool",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )


@pytest.mark.asyncio
async def test_reconcile_retries_the_route_apply_once_after_stale_discovery(monkeypatch):
    _seed_member(cluster_id="cluster-a")
    service = _service(calls=[])
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: _gateway_cluster())
    invalidated: list[str] = []
    monkeypatch.setattr(gateway_ops_module, "invalidate_cluster_discovery_cache", invalidated.append)
    attempts = _intercept_apply(
        monkeypatch,
        service,
        marker="kind: HTTPRoute",
        responses=[_stale_discovery_result("HTTPRoute"), _Result(stdout="applied")],
    )

    operation = await service.reconcile_cluster_gateway("cluster-a")

    assert operation.status == "succeeded"
    assert invalidated == ["cluster-a"]
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_reconcile_retries_the_rewrite_apply_once_after_stale_discovery(monkeypatch):
    _seed_model_rewrite_member()
    service = _service(calls=[], runner_cls=_ModelRewriteRunner)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: _gateway_cluster())
    invalidated: list[str] = []
    monkeypatch.setattr(gateway_ops_module, "invalidate_cluster_discovery_cache", invalidated.append)
    attempts = _intercept_apply(
        monkeypatch,
        service,
        marker="kind: InferenceModelRewrite",
        responses=[_stale_discovery_result(), _Result(stdout="applied")],
    )

    operation = await service.reconcile_cluster_gateway("cluster-a")

    assert operation.status == "succeeded"
    assert invalidated == ["cluster-a"]
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_reconcile_does_not_retry_a_different_apply_failure(monkeypatch):
    _seed_member(cluster_id="cluster-a")
    service = _service(calls=[])
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: _gateway_cluster())
    invalidated: list[str] = []
    monkeypatch.setattr(gateway_ops_module, "invalidate_cluster_discovery_cache", invalidated.append)
    attempts = _intercept_apply(
        monkeypatch,
        service,
        marker="kind: HTTPRoute",
        responses=[_Result(returncode=1, stderr="forbidden")],
    )

    operation = await service.reconcile_cluster_gateway("cluster-a")

    assert operation.status == "failed"
    assert "forbidden" in operation.message
    assert invalidated == []
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_reconcile_fails_after_only_one_stale_discovery_retry(monkeypatch):
    _seed_member(cluster_id="cluster-a")
    service = _service(calls=[])
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: _gateway_cluster())
    invalidated: list[str] = []
    monkeypatch.setattr(gateway_ops_module, "invalidate_cluster_discovery_cache", invalidated.append)
    attempts = _intercept_apply(
        monkeypatch,
        service,
        marker="kind: HTTPRoute",
        responses=[_stale_discovery_result("HTTPRoute"), _stale_discovery_result("HTTPRoute")],
    )

    operation = await service.reconcile_cluster_gateway("cluster-a")

    assert operation.status == "failed"
    assert "no matches for kind" in operation.message
    assert invalidated == ["cluster-a"]
    assert len(attempts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "stderr"),
    [
        ("istio", 'Error from server (Forbidden): services "lens-gateway-istio" is forbidden'),
        ("envoy-ai-gateway", "error: You must be logged in to the server (Unauthorized)"),
        (
            "agentgateway",
            'Error from server (Forbidden): gateways.gateway.networking.k8s.io "lens-gateway" is forbidden',
        ),
    ],
)
async def test_ensure_cluster_exposure_surfaces_auth_errors_while_non_raising_callers_ignore(provider, stderr):
    from llm_d_lens.model_service.gateway_ops import GatewayOpsError

    class _AuthErrorRunner(_FakeRunner):
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            self.calls.append((argv, input))
            return _Result(returncode=1, stderr=stderr)

    service = _service(calls=[], runner_cls=_AuthErrorRunner)

    with pytest.raises(GatewayOpsError, match=r"(Forbidden|Unauthorized|must be logged in)"):
        await service.ensure_cluster_exposure("cluster-a", provider, "lens-gateway", "lens-gateway", 30080)

    assert await service._gateway_service("cluster-a", "lens-gateway", "lens-gateway", provider) is None


@pytest.mark.asyncio
async def test_ensure_cluster_exposure_surfaces_auth_errors_on_discovery_svc_lookup():
    from llm_d_lens.model_service.gateway_ops import GatewayOpsError

    class _DiscoverySvcForbiddenRunner(_FakeRunner):
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            self.calls.append((argv, input))
            if list(argv[:3]) == ["kubectl", "get", "gateway"]:
                return _Result(returncode=0, stdout="")
            if list(argv[:3]) == ["kubectl", "get", "svc"]:
                return _Result(returncode=1, stderr="Error from server (Forbidden): services is forbidden")
            return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)

    service = _service(calls=[], runner_cls=_DiscoverySvcForbiddenRunner)
    with pytest.raises(GatewayOpsError, match="Forbidden"):
        await service.ensure_cluster_exposure("cluster-a", "agentgateway", "lens-gateway", "lens-gateway", 30080)


def _ssar_list_response(checks: list[dict], *, allow_fn) -> str:
    items = []
    for item in checks:
        attrs = (item.get("spec") or {}).get("resourceAttributes") or {}
        items.append(
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": attrs},
                "status": {"allowed": bool(allow_fn(attrs))},
            }
        )
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": items})


@pytest.mark.asyncio
async def test_probe_capabilities_ssar_admin_vs_non_admin_and_cache(monkeypatch):
    _seed_member(cluster_id="cluster-ssar")
    kubeconfig_value = (
        "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.10:6443}}]\n"
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig_value)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway"),
    )

    ssar_calls: list[dict] = []
    allow_admin = False

    class _SsarRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                ssar_calls.append(payload)
                if allow_admin:
                    return _Result(stdout=_ssar_list_response(payload["items"], allow_fn=lambda _a: True))
                return _Result(
                    stdout=_ssar_list_response(
                        payload["items"],
                        allow_fn=lambda a: a.get("namespace") == "ns",
                    )
                )
            return _Result(returncode=1)

    service = GatewayOpsService(runner_factory=lambda _cid: _SsarRunner())

    caps = await service.probe_capabilities("cluster-ssar")
    assert caps.probe_conclusive is True
    assert caps.access_mode == "non_admin"
    assert caps.can_manage_gateway_infra is False
    assert caps.can_manage_gateway_config is False
    assert caps.can_reconcile_routes is False
    assert caps.can_read_gateway_namespace is False
    assert caps.can_port_forward_gateway is False
    assert caps.permitted_member_namespaces == frozenset({"ns"})
    assert len(ssar_calls) == 1
    sent_attrs = [
        (i["spec"]["resourceAttributes"].get("group", ""), i["spec"]["resourceAttributes"]["resource"])
        for i in ssar_calls[0]["items"]
    ]
    assert ("llm-d.ai", "inferencemodelrewrites") in sent_attrs
    assert ("gateway.networking.k8s.io", "gatewayclasses") in sent_attrs

    # Second call uses 30s cache without another kubectl call.
    caps_cached = await service.probe_capabilities("cluster-ssar")
    assert caps_cached == caps
    assert len(ssar_calls) == 1

    # Rotating kubeconfig or refresh=True invalidates cache.
    allow_admin = True
    caps_refreshed = await service.probe_capabilities("cluster-ssar", refresh=True)
    assert caps_refreshed.access_mode == "admin"
    assert caps_refreshed.can_manage_gateway_infra is True
    assert caps_refreshed.can_manage_gateway_config is True
    assert caps_refreshed.can_reconcile_routes is True
    assert len(ssar_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expect_auth_error"),
    [
        (_Result(returncode=1, stderr="error: You must be logged in to the server (Unauthorized)"), True),
        (_Result(returncode=1, stderr="Error from server (Forbidden): selfsubjectaccessreviews is forbidden"), False),
    ],
)
async def test_probe_capabilities_registered_cluster_fails_closed_and_surfaces_401(
    monkeypatch, result, expect_auth_error
):
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: "kubeconfig-bytes")
    monkeypatch.setattr(
        gateway_ops_module, "get_cluster", lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway")
    )

    class _ErrRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            return result

    service = GatewayOpsService(runner_factory=lambda _cid: _ErrRunner())
    caps = await service.probe_capabilities("cluster-reg")
    assert caps.probe_conclusive is True
    assert caps.access_mode == "non_admin"
    assert caps.can_manage_gateway_infra is False
    assert caps.can_reconcile_routes is False
    assert (caps.auth_error is not None) is expect_auth_error


def test_external_gateway_probe_url_and_ssrf_guards(monkeypatch):
    from llm_d_lens.model_service.gateway_ops import _ensure_safe_gateway_url, _external_gateway_probe_url

    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)

    # Allows RFC1918 including 172.16.0.0/12 NodePorts
    c1 = SimpleNamespace(gateway_public_url=None, gateway_port=30080, gateway_node_address="172.18.0.2")
    assert _external_gateway_probe_url(c1, cluster_id="c1") == "http://172.18.0.2:30080/v1"

    # Normalizes bare host/IP and full URL
    c2 = SimpleNamespace(gateway_public_url="10.1.2.3", gateway_port=30080)
    assert _external_gateway_probe_url(c2) == "http://10.1.2.3:30080/v1"
    c3 = SimpleNamespace(gateway_public_url="https://gw.example.com/v1", gateway_port=30080)
    assert _external_gateway_probe_url(c3) == "https://gw.example.com/v1"

    # Blocks metadata, link-local, and IPv6 transition addresses
    for bad in (
        "169.254.169.254",
        "169.254.170.2",
        "100.100.100.200",
        "[fd00:ec2::254]",
        "[fe80::1]",
        "[::ffff:169.254.169.254]",
        "[64:ff9b::169.254.169.254]",
    ):
        assert _external_gateway_probe_url(SimpleNamespace(gateway_public_url=bad, gateway_port=30080)) is None

    # DNS rebinding check blocks hostnames resolving to metadata IPs
    import asyncio

    async def _check():
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(_host, _port):
            return [(2, 1, 6, "", ("169.254.169.254", 80))]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(ValueError, match="blocked"):
            await _ensure_safe_gateway_url("http://rebind.example.com/v1")

    asyncio.run(_check())


@pytest.mark.asyncio
async def test_non_admin_status_uses_http_probe_without_gateway_ns_reads(monkeypatch):
    member = _seed_member(cluster_id="cluster-na")
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://172.18.0.3:6443}}]\n"
    cluster = SimpleNamespace(
        id="cluster-na",
        name="Non-Admin Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=30080,
        hf_token_secret_namespace="ns",  # noqa: S106 - Kubernetes namespace name
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])

    kubectl_calls: list[list[str]] = []

    class _NonAdminRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            kubectl_calls.append(list(argv))
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(
                        payload["items"],
                        allow_fn=lambda a: a.get("namespace") == "ns",
                    )
                )
            raise AssertionError(f"unexpected kubectl read in non-admin status(): {argv}")

    class _HttpClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url):
            assert url == "http://172.18.0.3:30080/v1/models"
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HttpClient)
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    service = GatewayOpsService(runner_factory=lambda _cid: _NonAdminRunner())
    status = await service.status()
    assert len(status.clusters) == 1
    entry = status.clusters[0]
    assert entry.access_mode == "non_admin"
    assert entry.gateway_state == "installed"
    assert entry.gateway_ready is True
    assert entry.gateway_probe_source == "http"
    assert entry.gateway_node_address == "172.18.0.3"
    # Non-Admin ipp_state stays unknown unless Prometheus confirms IPP
    assert entry.ipp_state == "unknown"
    assert entry.ipp_ready is None
    assert entry.reconcile_status == "admin_handoff_required"
    assert entry.pending_operations_count >= 1
    assert entry.handoff_namespace == "ns"
    assert len(kubectl_calls) == 1  # only the batched SSAR!
    assert member.cluster_id == "cluster-na"


@pytest.mark.asyncio
async def test_get_ipp_config_separates_403_from_401(monkeypatch):
    from llm_d_lens.model_service.gateway_ops import GatewayUnauthenticatedError

    monkeypatch.setattr(
        gateway_ops_module, "get_cluster", lambda _cid: SimpleNamespace(gateway_namespace="lens-gateway")
    )
    service = GatewayOpsService()

    service._kubectl = AsyncMock(return_value=_Result(returncode=1, stderr="Error from server (Forbidden)"))
    res = await service.get_ipp_config("cluster-a")
    assert res["handoffRequired"] is True
    assert res["canManageGatewayConfig"] is False
    assert res["accessMode"] == "non_admin"

    service._kubectl = AsyncMock(return_value=_Result(returncode=1, stderr="Unauthorized"))
    with pytest.raises(GatewayUnauthenticatedError):
        await service.get_ipp_config("cluster-a")


@pytest.mark.asyncio
async def test_non_admin_reconcile_delete_handoff_script_and_verify(monkeypatch):
    import subprocess

    member = _seed_member(cluster_id="cluster-na2")
    group = ModelServiceGroupDao().get(member.group_id)
    # Also create a stopped group to verify idempotent delete rendering for inactive groups
    stopped_group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="stopped-model",
            model_ref="Qwen/Qwen3-0.6B",
            cluster_id="cluster-na2",
            base_model="Qwen/Qwen3-0.6B",
            status="disabled",
        )
    )
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.50:6443}}]\n"
    cluster = SimpleNamespace(
        id="cluster-na2",
        name="Non-Admin Cluster 2",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.50:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="ns",  # noqa: S106 - Kubernetes namespace name
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "ns")

    marker_store: dict[str, dict] = {}
    applies: list[str] = []

    class _NaRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "ns")
                )
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                applies.append(input or "")
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm_name = argv[3]
                if cm_name in marker_store:
                    return _Result(stdout=json.dumps(marker_store[cm_name]))
                return _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "get", "crd"]:
                return _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "delete", "referencegrant"]:
                return _Result(stdout="deleted")
            return _Result(returncode=1, stderr="Forbidden")

    service = GatewayOpsService(runner_factory=lambda _cid: _NaRunner())

    # 1. Reconcile on Non-Admin returns succeeded + admin_handoff_required + script
    op = await service.reconcile_cluster_gateway("cluster-na2", actor=None)
    assert op.status == "succeeded"
    handoff = service.handoff_from_operation(op)
    assert handoff is not None
    assert handoff["reconcileStatus"] == "admin_handoff_required"
    assert handoff["markerNamespace"] == "ns"
    script = handoff["adminScript"]
    assert "set -euo pipefail" in script
    assert "umask 077" in script
    assert "jq" not in script and "python3" not in script
    assert "inferencemodelrewrites.llm-d.ai" in script
    assert "delete httproute" in script  # stopped_group cleanup
    assert "lens.llm-d.ai/desired-hash=$DESIRED_HASH16" in script
    assert "lens.llm-d.ai/cluster=$CLUSTER_LABEL" in script
    subprocess.run(["bash", "-n", "-"], input=script, text=True, check=True)

    # 2. Gateway repair & IPP config scripts also pass bash -n and avoid --set
    repair = await service.create_remediation("cluster-na2", mode="gateway_repair")
    assert "kind: GatewayClass" in repair.admin_script
    assert "type: NodePort" in repair.admin_script
    subprocess.run(["bash", "-n", "-"], input=repair.admin_script, text=True, check=True)

    ipp_op = await service.set_ipp_config("cluster-na2", "plugins:\n  - type: test\n")
    assert ipp_op.status == "succeeded"
    ipp_handoff = service.handoff_from_operation(ipp_op)
    assert ipp_handoff is not None
    assert "--set" not in ipp_handoff["adminScript"]
    assert "ipp-values.yaml" in ipp_handoff["adminScript"]
    subprocess.run(["bash", "-n", "-"], input=ipp_handoff["adminScript"], text=True, check=True)

    # 3. Verify rejects 404 (pending) even with complete marker
    recheck_handoff = await service.create_remediation("cluster-na2", mode="routes")
    marker_store[recheck_handoff.marker_name] = {
        "data": {"desiredHash": recheck_handoff.desired_hash, "status": "complete"}
    }

    class _VerifyHttpClient:
        def __init__(self, status_for_post: int):
            self._status = status_for_post

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=200)

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=self._status)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", lambda *a, **kw: _VerifyHttpClient(404))
    res_pending = await service.verify_remediation("cluster-na2")
    assert res_pending.verified is False
    assert res_pending.route_checks[0]["status"] == "pending"

    # 4. Verify succeeds when marker is complete and HTTP route probe returns 401
    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", lambda *a, **kw: _VerifyHttpClient(401))
    res_ok = await service.verify_remediation("cluster-na2")
    assert res_ok.verified is True
    assert res_ok.reconcile_status == "synced"
    assert res_ok.marker_status == "complete"

    # 5. remove_model_route raises GatewayAdminHandoffRequired and persists group_deleted without lock deadlock
    from llm_d_lens.model_service.gateway_ops import GatewayAdminHandoffRequired

    async with gateway_ops_module.gateway_lock("cluster-na2"):
        with pytest.raises(GatewayAdminHandoffRequired) as exc_info:
            await service.remove_model_route(group)
    assert any(
        op_item.action == "delete" and op_item.kind == "httproute"
        for op_item in exc_info.value.handoff.pending_operations
    )

    # 6. record_member_deletion records pending delete on non-admin
    mem_handoff = await service.record_member_deletion(member, group, remaining_members=[])
    assert mem_handoff is not None
    subprocess.run(["bash", "-n", "-"], input=mem_handoff.admin_script, text=True, check=True)
    assert stopped_group.id


@pytest.mark.asyncio
async def test_remediation_validation_rejects_bad_inputs(monkeypatch):
    from llm_d_lens.model_service.gateway_ops import GatewayClusterNotFoundError, GatewayValidationError

    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda cid: None if cid == "missing" else SimpleNamespace(gateway_namespace="lens-gateway"),
    )
    service = GatewayOpsService()

    with pytest.raises(GatewayClusterNotFoundError):
        await service.get_remediation("missing")

    with pytest.raises(GatewayValidationError):
        await service.create_remediation("cluster-a", marker_namespace="Invalid_NS")

    with pytest.raises(GatewayValidationError):
        await service.create_remediation("cluster-a", ipp_config="plugins: []\r\n")

    with pytest.raises(GatewayValidationError):
        await service.create_remediation("a" * 33)


@pytest.mark.asyncio
async def test_custom_ipp_config_roundtrip_status_and_verify(monkeypatch):
    cluster = SimpleNamespace(
        id="cluster-ipp",
        name="IPP Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
    )
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.10:6443}}]\n"
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "ns")

    marker_store: dict[str, dict] = {}

    class _ReadOnlyGwRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                # Can read gateway_namespace (view role), cannot manage config/routes
                return _Result(
                    stdout=_ssar_list_response(
                        payload["items"],
                        allow_fn=lambda a: a.get("verb") in ("get", "list"),
                    )
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="not found")

    service = GatewayOpsService(runner_factory=lambda _cid: _ReadOnlyGwRunner())
    custom_yaml = "plugins:\n  - type: custom-test\n"
    op = await service.set_ipp_config("cluster-ipp", custom_yaml)
    assert op.status == "succeeded"
    handoff = service.handoff_from_operation(op)
    assert handoff is not None
    expected_hash = handoff["desiredHash"]

    # status() reproduces the exact same desiredHash (not empty-config hash)
    status = await service.status()
    assert status.clusters[0].desired_hash == expected_hash
    assert status.clusters[0].reconcile_status == "admin_handoff_required"

    # With read-only gateway_namespace and no marker yet, verify_remediation MUST NOT vacuously pass
    res_missing = await service.verify_remediation("cluster-ipp")
    assert res_missing.verified is False
    assert res_missing.desired_hash == expected_hash

    # Rejects invalid or stale expected_hash
    from llm_d_lens.model_service.gateway_ops import GatewayValidationError

    with pytest.raises(GatewayValidationError):
        await service.verify_remediation("cluster-ipp", expected_hash="not-a-sha256")

    stale_hash = "0" * 64
    marker_store[handoff["markerName"]] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": "cluster-ipp"}},
        "data": {"desiredHash": expected_hash, "status": "complete"},
    }
    res_stale_expected = await service.verify_remediation("cluster-ipp", expected_hash=stale_hash)
    assert res_stale_expected.verified is False
    assert res_stale_expected.marker_status == "stale"

    # Untrusted cluster label mismatch is rejected as stale
    marker_store[handoff["markerName"]]["metadata"]["labels"]["lens.llm-d.ai/cluster"] = "other-cluster"
    res_bad_label = await service.verify_remediation("cluster-ipp")
    assert res_bad_label.verified is False
    assert res_bad_label.marker_status == "stale"

    # Valid marker verifies cleanly
    marker_store[handoff["markerName"]]["metadata"]["labels"]["lens.llm-d.ai/cluster"] = "cluster-ipp"
    res_ok = await service.verify_remediation("cluster-ipp", expected_hash=expected_hash)
    assert res_ok.verified is True
    assert res_ok.reconcile_status == "synced"


@pytest.mark.asyncio
async def test_gateway_serving_404_returns_pending_on_non_admin_member_health(monkeypatch):
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="pending-model", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-404")
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-404",
            cluster_id="cluster-404",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns/qwen",
            endpoint_kind="llm-d-epp",
        )
    )
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.10:6443}}]\n"
    cluster = SimpleNamespace(
        id="cluster-404",
        gateway_namespace="lens-gateway",
        gateway_public_url="http://10.0.0.10:30080/v1",
        gateway_port=30080,
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    class _Client404:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=404)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _Client404)

    service = GatewayOpsService(
        runner_factory=lambda _cid: _ComponentRunner([]),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        service,
        "probe_capabilities",
        AsyncMock(
            return_value=gateway_ops_module.ClusterGatewayCapabilities(
                can_manage_gateway_infra=False,
                can_manage_gateway_config=False,
                can_reconcile_routes=False,
                access_mode="non_admin",
                can_read_gateway_namespace=False,
                can_port_forward_gateway=False,
                permitted_member_namespaces=frozenset({"ns"}),
                probe_conclusive=True,
            )
        ),
    )
    ready, detail = await service._member_health(member)
    assert ready is False
    assert detail["components"]["serving"] == "pending"
    assert detail["components"]["httpRoute"] == "unknown"


@pytest.mark.asyncio
async def test_qualified_pool_name_without_epp_ref_and_error_redaction(monkeypatch):
    import subprocess

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qual-pool", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-q.")
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-q",
            cluster_id="cluster-q.",
            target_namespace="ns-a",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns-a/qwen",
            epp_ref=None,
            endpoint_kind="llm-d-epp",
        )
    )
    cluster = SimpleNamespace(
        id="cluster-q.",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)

    service = GatewayOpsService()
    monkeypatch.setattr(
        service,
        "probe_capabilities",
        AsyncMock(
            return_value=gateway_ops_module.ClusterGatewayCapabilities(
                can_manage_gateway_infra=False,
                can_manage_gateway_config=False,
                can_reconcile_routes=False,
                access_mode="non_admin",
                can_read_gateway_namespace=False,
                can_port_forward_gateway=False,
                permitted_member_namespaces=frozenset(),
                probe_conclusive=True,
            )
        ),
    )
    handoff = await service.get_remediation("cluster-q.", marker_namespace="ns-a")
    epp_ops = [op for op in handoff.pending_operations if op.action == "patch"]
    assert len(epp_ops) == 1
    assert epp_ops[0].namespace == "ns-a"
    assert epp_ops[0].name == "qwen-epp"
    assert "set -f" in handoff.admin_script and "set +f" in handoff.admin_script
    assert "CLUSTER_LABEL=cluster-q" in handoff.admin_script
    subprocess.run(["bash", "-n", "-"], input=handoff.admin_script, text=True, check=True)

    # Error redaction & bare IPv6 URL normalization
    redacted = gateway_ops_module._sanitized_kubectl_error(
        _Result(
            returncode=1,
            stderr='Unable to read config file "/tmp/kc.yaml": Bearer secret-tok token=abc123',
        )
    )
    assert "secret-tok" not in redacted
    assert "abc123" not in redacted
    assert "/tmp/kc.yaml" not in redacted
    assert (
        gateway_ops_module._normalize_gateway_url("2001:db8::1", default_port=30080) == "http://[2001:db8::1]:30080/v1"
    )


@pytest.mark.asyncio
async def test_non_admin_bind_invalidates_stale_cache_patches_epp_online_and_verifies(monkeypatch):
    import subprocess

    from llm_d_lens.model_service.contracts import GroupPublishRequest
    from llm_d_lens.model_service.service import ModelService
    from llm_d_lens.model_service.targets import ExecutionTarget

    cluster_id = "e66f4591"
    boot_ns = "llmd-5b251a2c-test"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.50:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="sprocean-min",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.50:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    monkeypatch.setattr(
        "llm_d_lens.model_service.service.resolve_execution_target",
        lambda _eid: ExecutionTarget(
            execution_id="exec-bind-1",
            name="qwen3-deploy",
            display_name="Qwen3 Deploy",
            model="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
            namespace=boot_ns,
            service="qwen3-epp",
            port=8000,
            endpoint_kind="llm-d-epp",
            model_ref="Qwen/Qwen3-8B",
            status="ready",
        ),
    )

    marker_store: dict[str, dict] = {}
    epp_patches: list[tuple[str, str, str]] = []
    applied_manifests: list[str] = []
    fail_ssar_once = True

    class _BindRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            nonlocal fail_ssar_once
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                if fail_ssar_once:
                    fail_ssar_once = False
                    raise TimeoutError("transient SSAR timeout")
                payload = json.loads(input or "{}")
                resp_json = _ssar_list_response(
                    payload["items"],
                    allow_fn=lambda a: a.get("namespace") in (boot_ns, "lens-edit"),
                )
                obj = json.loads(resp_json)
                obj["kind"] = "SelfSubjectAccessReviewList"
                return _Result(stdout=json.dumps(obj))
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout=json.dumps(
                        {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {
                                                "name": "epp",
                                                "args": ["--pool-name", "qwen3", "--secure-serving=true"],
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    )
                )
            if list(argv[:3]) == ["kubectl", "patch", "deploy"]:
                epp_patches.append((argv[3], argv[5], argv[-1]))
                return _Result(stdout="patched")
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                applied_manifests.append(input or "")
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm_name = argv[3]
                if cm_name in marker_store:
                    return _Result(stdout=json.dumps(marker_store[cm_name]))
                return _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "get", "crd"]:
                return _Result(returncode=1, stderr="Error from server (Forbidden): crds is forbidden")
            return _Result(returncode=1, stderr="Forbidden")

    class _OkHttpClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=401)

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _OkHttpClient)
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    ops = GatewayOpsService(runner_factory=lambda _cid: _BindRunner())
    ms = ModelService()

    # 1. Transient SSAR timeout fails closed for that call but NEVER poisons the cache.
    caps_transient = await ops.probe_capabilities(cluster_id)
    assert caps_transient.access_mode == "non_admin"
    assert caps_transient.permitted_member_namespaces == frozenset()

    # 2. Pre-bind capability probe caches empty permitted_member_namespaces (no members yet).
    caps_before = await ops.probe_capabilities(cluster_id)
    assert caps_before.permitted_member_namespaces == frozenset()

    # 3. Publish group + member in boot_ns -> invalidates cache across instances.
    published_group = ms.publish_group(
        GroupPublishRequest(name="qwen3-alias", clusterId=cluster_id, executionId="exec-bind-1"),
        created_by=None,
    )
    assert published_group.served_name == "Qwen/Qwen3-8B"

    reconcile_op = await ops.reconcile_cluster_gateway(cluster_id)
    handoff = ops.handoff_from_operation(reconcile_op)
    assert handoff is not None
    assert handoff["reconcileStatus"] == "admin_handoff_required"
    assert handoff["markerNamespace"] == "lens-edit"
    # boot_ns IS permitted, so EPP plaintext patch ran online and is omitted from admin script.
    assert "patch_epp_transport" not in handoff["adminScript"]
    assert len(epp_patches) == 1
    assert epp_patches[0][:2] == ("qwen3-epp", boot_ns)
    assert "--secure-serving=false" in epp_patches[0][2]
    # ReferenceGrant and InferenceModelRewrite were applied online despite cluster-scoped get crd returning 403.
    assert any("kind: ReferenceGrant" in doc for doc in applied_manifests)
    assert any("kind: InferenceModelRewrite" in doc for doc in applied_manifests)
    subprocess.run(["bash", "-n", "-"], input=handoff["adminScript"], text=True, check=True)

    # 4. get_remediation, create_remediation, status, and verify_remediation all produce the EXACT same desiredHash.
    epp_patches.clear()
    get_payload = await ops.get_remediation(cluster_id, mode="routes")
    create_payload = await ops.create_remediation(cluster_id, mode="routes")
    assert len(epp_patches) == 1  # create_remediation also guarantees online EPP plaintext patch
    status_before = await ops.status()
    assert get_payload.desired_hash == handoff["desiredHash"]
    assert create_payload.desired_hash == handoff["desiredHash"]
    assert status_before.clusters[0].desired_hash == handoff["desiredHash"]

    # 5. Simulate Orchestration administrator running script -> Re-check passes cleanly!
    marker_store[handoff["markerName"]] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": handoff["desiredHash"], "status": "complete"},
    }
    verified = await ops.verify_remediation(
        cluster_id,
        expected_hash=handoff["desiredHash"],
        marker_namespace="lens-edit",
    )
    assert verified.verified is True
    assert verified.reconcile_status == "synced"
    assert verified.marker_status == "complete"
    assert verified.desired_hash == handoff["desiredHash"]


@pytest.mark.asyncio
async def test_verify_remediation_accepts_strict_epp_superset_marker_and_rejects_route_drift(monkeypatch):
    cluster_id = "cluster-sup"
    boot_ns = "llmd-sup-ns"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.60:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Superset Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.60:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="sup-model",
            model_ref="Qwen/Qwen3-8B",
            base_model="sup-model",
            served_name="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
        )
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-sup-1",
            cluster_id=cluster_id,
            target_namespace=boot_ns,
            target_service="qwen-epp",
            target_port=8000,
            pool_name=f"{boot_ns}/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
            weight=1,
        )
    )

    permit_boot_ns = False
    marker_store: dict[str, dict] = {}
    epp_patched_on_verify: list[str] = []

    class _SupRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                allowed_ns = {boot_ns, "lens-edit"} if permit_boot_ns else {"lens-edit"}
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") in allowed_ns)
                )
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "patch", "deploy"]:
                epp_patched_on_verify.append(argv[3])
                return _Result(stdout="patched")
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="Forbidden")

    class _HttpOk:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=401)

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HttpOk)
    ops = GatewayOpsService(runner_factory=lambda _cid: _SupRunner())

    # H1 generated before boot_ns RBAC was permitted (includes eppTargets).
    h1_handoff = await ops.create_remediation(cluster_id, mode="routes")
    assert "patch_epp_transport" in h1_handoff.admin_script
    marker_store[h1_handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": h1_handoff.desired_hash, "status": "complete"},
    }

    # Now boot_ns becomes permitted -> current canonical hash is H2 (without eppTargets).
    permit_boot_ns = True
    h2_preview = await ops.get_remediation(cluster_id, mode="routes")
    assert h2_preview.desired_hash != h1_handoff.desired_hash
    assert "patch_epp_transport" not in h2_preview.admin_script

    # Negative check 1: untrusted cluster label on superset marker MUST still fail as stale.
    marker_store[h1_handoff.marker_name]["metadata"]["labels"]["lens.llm-d.ai/cluster"] = "evil-cluster"
    bad_cluster = await ops.verify_remediation(cluster_id, expected_hash=h1_handoff.desired_hash)
    assert bad_cluster.verified is False
    assert bad_cluster.marker_status == "stale"

    # Valid strict eppTargets superset H1 marker verifies cleanly and patches EPP online!
    epp_patched_on_verify.clear()
    marker_store[h1_handoff.marker_name]["metadata"]["labels"]["lens.llm-d.ai/cluster"] = cluster_id
    ok_res = await ops.verify_remediation(cluster_id, expected_hash=h1_handoff.desired_hash)
    assert ok_res.verified is True
    assert ok_res.reconcile_status == "synced"
    assert ok_res.marker_status == "complete"
    assert ok_res.observed_hash == h1_handoff.desired_hash
    assert ok_res.desired_hash == h2_preview.desired_hash
    assert ok_res.marker_name == h1_handoff.marker_name
    assert epp_patched_on_verify == ["qwen-epp"]

    # Negative check 2: if route weights / manifests actually changed, H1 MUST be rejected as stale!
    ModelServiceMemberDao().save_traffic(
        group.id,
        [member.model_copy(update={"weight": 50})],
        {member.execution_id: 1},
    )
    stale_route = await ops.verify_remediation(cluster_id, expected_hash=h1_handoff.desired_hash)
    assert stale_route.verified is False
    assert stale_route.marker_status == "stale"


@pytest.mark.asyncio
async def test_non_admin_empty_group_and_unapplied_member_removal_stay_synced(monkeypatch):
    cluster_id = "cluster-unapplied"
    boot_ns = "llmd-unapplied"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.70:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Unapplied Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.70:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    online_deletes: list[tuple[str, str, str]] = []

    class _Runner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(
                        payload["items"], allow_fn=lambda a: a.get("namespace") in (boot_ns, "lens-edit")
                    )
                )
            if list(argv[:2]) == ["kubectl", "delete"]:
                online_deletes.append((argv[2], argv[3], argv[5]))
                return _Result(stdout="deleted")
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                return _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "get", "crd"]:
                return _Result(returncode=1, stderr="Forbidden")
            return _Result(returncode=1, stderr="Forbidden")

    class _HttpOk:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HttpOk)
    ops = GatewayOpsService(runner_factory=lambda _cid: _Runner())

    # 1. Standalone empty group never triggers inferred_deletes or admin_handoff_required.
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="empty-qwen",
            model_ref="Qwen/Qwen3-8B",
            base_model="empty-qwen",
            served_name="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
        )
    )
    st_empty = await ops.status()
    assert st_empty.clusters[0].reconcile_status == "synced"
    assert st_empty.clusters[0].pending_operations_count == 0

    # 2. Bind a member (script generated, NOT run on cluster), then immediately remove the member.
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-never-applied",
            cluster_id=cluster_id,
            target_namespace=boot_ns,
            target_service="qwen-epp",
            target_port=8000,
            pool_name=f"{boot_ns}/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )
    bind_op = await ops.reconcile_cluster_gateway(cluster_id)
    assert ops.handoff_from_operation(bind_op) is not None

    ModelServiceMemberDao().delete(member.id)
    del_handoff = await ops.record_member_deletion(member, group, remaining_members=[])
    assert del_handoff is not None
    assert del_handoff.reconcile_status == "synced"
    assert del_handoff.pending_operations == []
    # Member-namespace ReferenceGrant & InferenceModelRewrite were still cleaned up online!
    deleted_kinds = {kind for kind, _name, ns in online_deletes if ns == boot_ns}
    assert deleted_kinds == {"inferencemodelrewrite", "referencegrant"}

    st_after = await ops.status()
    assert st_after.clusters[0].reconcile_status == "synced"
    assert st_after.clusters[0].pending_operations_count == 0


@pytest.mark.asyncio
async def test_non_admin_applied_route_member_removal_and_status_auto_reconcile(monkeypatch):
    cluster_id = "cluster-cleanup"
    boot_ns = "llmd-cleanup-ns"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.80:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Cleanup Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.80:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    marker_store: dict[str, dict] = {}
    online_deletes: list[tuple[str, str, str]] = []

    class _CleanupRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(
                        payload["items"], allow_fn=lambda a: a.get("namespace") in (boot_ns, "lens-edit")
                    )
                )
            if list(argv[:2]) == ["kubectl", "delete"]:
                online_deletes.append((argv[2], argv[3], argv[5]))
                return _Result(stdout="deleted")
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "get", "crd"]:
                return _Result(returncode=1, stderr="Forbidden")
            return _Result(returncode=1, stderr="Forbidden")

    post_status = 404

    class _HttpProbe:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=401)

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=post_status)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HttpProbe)
    ops = GatewayOpsService(runner_factory=lambda _cid: _CleanupRunner())

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="qwen3",
            model_ref="Qwen/Qwen3-8B",
            base_model="qwen3",
            served_name="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
        )
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-clean-1",
            cluster_id=cluster_id,
            target_namespace=boot_ns,
            target_service="qwen3-epp",
            target_port=8000,
            pool_name=f"{boot_ns}/qwen3",
            epp_ref="qwen3-epp",
            endpoint_kind="llm-d-epp",
        )
    )

    # 1. Bind generates H_bind; admin runs script (marker complete), but Re-check failed earlier on 404.
    bind_op = await ops.reconcile_cluster_gateway(cluster_id)
    bind_handoff = ops.handoff_from_operation(bind_op)
    assert bind_handoff is not None
    marker_store[bind_handoff["markerName"]] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": bind_handoff["desiredHash"], "status": "complete"},
    }
    failed_verify = await ops.verify_remediation(cluster_id, expected_hash=bind_handoff["desiredHash"])
    assert failed_verify.verified is False  # HTTP 404 -> verifiedHash remains None in DB!

    # 2. User removes deployment from gateway -> member-ns resources deleted online, gateway-ns cleanup script generated.
    ModelServiceMemberDao().delete(member.id)
    cleanup_handoff = await ops.record_member_deletion(member, group, remaining_members=[])
    assert cleanup_handoff is not None
    assert cleanup_handoff.reconcile_status == "admin_handoff_required"
    pending_kinds = [(op.action, op.kind, op.namespace) for op in cleanup_handoff.pending_operations]
    expected_gw_ns = gateway_ops_module.gateway_namespace_for(cluster_id, cluster.gateway_namespace)
    # Only gateway-namespace HTTPRoute & ConfigMap remain in pendingDeletes; member-ns rewrite/grant deleted online!
    assert pending_kinds == [
        ("delete", "configmap", expected_gw_ns),
        ("delete", "httproute", expected_gw_ns),
    ]
    assert {kind for kind, _name, ns in online_deletes if ns == boot_ns} == {
        "inferencemodelrewrite",
        "referencegrant",
    }

    # 3. Admin runs cleanup script -> page refresh (status()) auto-reconciles from marker without Re-check click!
    marker_store[cleanup_handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": cleanup_handoff.desired_hash, "status": "complete"},
    }
    st_synced = await ops.status()
    assert st_synced.clusters[0].reconcile_status == "synced"
    assert st_synced.clusters[0].pending_operations_count == 0

    # 4. Empty group never re-emits inferred_deletes on later status/reconcile or when adding a second group!
    rec_after = await ops.reconcile_cluster_gateway(cluster_id)
    assert ops.handoff_from_operation(rec_after) is None

    # 5. Also verify disabling a synced group emits deletes until verified, then stays synced forever.
    post_status = 401
    member2 = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-clean-2",
            cluster_id=cluster_id,
            target_namespace=boot_ns,
            target_service="qwen3-epp",
            target_port=8000,
            pool_name=f"{boot_ns}/qwen3",
            epp_ref="qwen3-epp",
            endpoint_kind="llm-d-epp",
        )
    )
    rebind_handoff = ops.handoff_from_operation(await ops.reconcile_cluster_gateway(cluster_id))
    assert rebind_handoff is not None
    marker_store[rebind_handoff["markerName"]] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": rebind_handoff["desiredHash"], "status": "complete"},
    }
    assert (await ops.status()).clusters[0].reconcile_status == "synced"

    group.status = "disabled"
    ModelServiceGroupDao().save(group)
    online_deletes.clear()
    disable_handoff = ops.handoff_from_operation(await ops.reconcile_cluster_gateway(cluster_id))
    assert disable_handoff is not None
    assert disable_handoff["reconcileStatus"] == "admin_handoff_required"
    assert {kind for kind, _name, ns in online_deletes if ns == boot_ns} == {
        "inferencemodelrewrite",
        "referencegrant",
    }
    marker_store[disable_handoff["markerName"]] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": disable_handoff["desiredHash"], "status": "complete"},
    }
    assert (await ops.status()).clusters[0].reconcile_status == "synced"
    assert ops.handoff_from_operation(await ops.reconcile_cluster_gateway(cluster_id)) is None
    assert member2.id


@pytest.mark.asyncio
async def test_superset_verification_scales_linearly_for_12_namespaces_with_none_expected_hash(monkeypatch):
    cluster_id = "cluster-12ns"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.90:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="12-NS Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.90:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="qwen-12",
            model_ref="Qwen/Qwen3-8B",
            base_model="qwen-12",
            served_name="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
        )
    )
    namespaces = [f"llmd-ns-{idx:02d}" for idx in range(1, 13)]
    for idx, ns in enumerate(namespaces, start=1):
        ModelServiceMemberDao().create(
            ModelServiceMember(
                group_id=group.id,
                execution_id=f"exec-12-{idx:02d}",
                cluster_id=cluster_id,
                target_namespace=ns,
                target_service=f"pool-{idx:02d}-epp",
                target_port=8000,
                pool_name=f"{ns}/pool-{idx:02d}",
                epp_ref=f"pool-{idx:02d}-epp",
                endpoint_kind="llm-d-epp",
                weight=1,
            )
        )

    permitted_set: set[str] = {"lens-edit"}
    marker_store: dict[str, dict] = {}

    class _ScaleRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") in permitted_set)
                )
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","p","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="Forbidden")

    class _HttpOk:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=401)

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HttpOk)
    ops = GatewayOpsService(runner_factory=lambda _cid: _ScaleRunner())

    # 1. Full superset (0 member namespaces permitted at H_full generation).
    h_full = await ops.get_remediation(cluster_id, mode="routes")

    # 2. Single-new-namespace superset: 11 namespaces permitted, 12th namespace not yet permitted.
    permitted_set = {"lens-edit", *namespaces[:-1]}
    h_single = await ops.get_remediation(cluster_id, mode="routes")
    assert h_single.desired_hash != h_full.desired_hash

    # 3. Now all 12 namespaces are permitted, and saved_state["desiredHash"] is already H2 (no eppTargets).
    permitted_set = {"lens-edit", *namespaces}
    h2 = await ops.create_remediation(cluster_id, mode="routes")
    assert h2.desired_hash not in {h_full.desired_hash, h_single.desired_hash}

    hash_calls = 0
    orig_hash = GatewayOpsService._canonical_plan_hash

    def _counting_hash(**kwargs):
        nonlocal hash_calls
        hash_calls += 1
        return orig_hash(**kwargs)

    monkeypatch.setattr(GatewayOpsService, "_canonical_plan_hash", staticmethod(_counting_hash))

    # Normal get_remediation computes exactly 1 canonical hash (zero eager superset work).
    hash_calls = 0
    await ops.get_remediation(cluster_id, mode="routes")
    assert hash_calls == 1

    # Verify with expected_hash=None matches 12th-namespace single-superset marker in O(N) hashes!
    marker_store[h_single.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": h_single.desired_hash, "status": "complete"},
    }
    hash_calls = 0
    res_single = await ops.verify_remediation(cluster_id, expected_hash=None)
    assert res_single.verified is True
    assert res_single.observed_hash == h_single.desired_hash
    assert res_single.desired_hash == h2.desired_hash
    assert hash_calls <= 15  # strictly O(N): 1 primary + 1 full + 12 single-ns

    # Also verify status() auto-reconcile with expected_hash=None matches full_sup_hash in O(N)!
    marker_store.clear()
    await ops.create_remediation(cluster_id, mode="routes")
    marker_store[h_full.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": h_full.desired_hash, "status": "complete"},
    }
    hash_calls = 0
    st = await ops.status()
    assert st.clusters[0].reconcile_status == "synced"
    assert hash_calls <= 15


@pytest.mark.asyncio
async def test_status_auto_reconcile_does_not_clobber_concurrent_handoff_and_preserves_legacy_marker(monkeypatch):
    cluster_id = "cluster-race"
    boot_ns = "llmd-race-ns"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.95:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Race Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.95:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="race-model",
            model_ref="Qwen/Qwen3-8B",
            base_model="race-model",
            served_name="Qwen/Qwen3-8B",
            cluster_id=cluster_id,
        )
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-race-1",
            cluster_id=cluster_id,
            target_namespace=boot_ns,
            target_service="qwen-epp",
            target_port=8000,
            pool_name=f"{boot_ns}/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )

    marker_store: dict[str, dict] = {}

    class _RaceRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(
                        payload["items"], allow_fn=lambda a: a.get("namespace") in (boot_ns, "lens-edit")
                    )
                )
            if list(argv[:2]) == ["kubectl", "delete"]:
                return _Result(stdout="deleted")
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _RaceRunner())

    # Part A: Legacy route-handoff without pendingMarkerNames preserves legacy markerName across create_remediation.
    legacy_hash = "1" * 64
    legacy_marker = f"lens-ms-handoff-{legacy_hash[:16]}"
    ops._save_handoff_locked(
        cluster_id,
        {
            "reconcileStatus": "admin_handoff_required",
            "desiredHash": legacy_hash,
            "verifiedHash": None,
            "markerNamespace": "lens-edit",
            "markerName": legacy_marker,
            "mode": "routes",
            "provider": "istio",
            "ippConfigSha256": "",
            "pendingDeletes": [],
        },
        message="legacy handoff",
    )
    # Regenerate handoff before legacy_marker was visible -> legacy_marker stays in pendingMarkerNames!
    regen_handoff = await ops.create_remediation(cluster_id, mode="routes")
    _, after_regen = ops._load_handoff_locked(cluster_id)
    assert any(item["name"] == legacy_marker for item in after_regen["pendingMarkerNames"])

    # Now legacy_marker appears as complete; Part B tests TOCTOU race during status() HTTP probe!
    marker_store[regen_handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": regen_handoff.desired_hash, "status": "complete"},
    }
    mutated_mid_flight = False

    class _MutatingHttpProbe:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=401)

        async def post(self, _url, **_kwargs):
            nonlocal mutated_mid_flight
            if not mutated_mid_flight:
                mutated_mid_flight = True
                # Simulate concurrent member deletion writing a newer route-handoff while status() awaits HTTP!
                ModelServiceMemberDao().delete(member.id)
                await ops.record_member_deletion(member, group, remaining_members=[])
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _MutatingHttpProbe)
    st = await ops.status()
    assert mutated_mid_flight is True
    # status() MUST NOT clobber the newer delete route-handoff back to synced!
    assert st.clusters[0].reconcile_status == "admin_handoff_required"
    _, latest_state = ops._load_handoff_locked(cluster_id)
    assert latest_state["reconcileStatus"] == "admin_handoff_required"
    assert len(latest_state["pendingDeletes"]) == 2


@pytest.mark.asyncio
async def test_service_state_endpointslice_fallback_on_forbidden(monkeypatch):
    service = GatewayOpsService()
    calls: list[list[str]] = []

    async def fake_kubectl(_cluster_id, argv, **_kwargs):
        calls.append(list(argv))
        if argv[:2] == ["get", "endpoints"]:
            return _Result(returncode=1, stderr="Error from server (Forbidden): endpoints is forbidden")
        if argv[:2] == ["get", "endpointslices"]:
            return _Result(
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "endpoints": [
                                    {"addresses": ["10.233.115.33"], "conditions": {"ready": True, "serving": True}},
                                    {"addresses": ["10.233.115.34"], "conditions": {"ready": False, "serving": False}},
                                    {"addresses": ["10.233.115.35"], "conditions": {"serving": True}},
                                ]
                            },
                            {
                                "endpoints": [
                                    {"addresses": ["10.233.115.33"], "conditions": {"ready": True}},
                                ]
                            },
                        ]
                    }
                )
            )
        return _Result(returncode=1, stderr="unexpected")

    monkeypatch.setattr(service, "_kubectl", fake_kubectl)
    assert await service._service_state("cluster-es", "qwen-epp", "ns") == ("ready", 2)
    assert any(c[:2] == ["get", "endpointslices"] for c in calls)

    # Zero ready addresses -> missing
    async def fake_kubectl_empty(_cluster_id, argv, **_kwargs):
        if argv[:2] == ["get", "endpoints"]:
            return _Result(returncode=1, stderr="Forbidden")
        return _Result(
            stdout=json.dumps({"items": [{"endpoints": [{"addresses": ["10.0.0.1"], "conditions": {"ready": False}}]}]})
        )

    monkeypatch.setattr(service, "_kubectl", fake_kubectl_empty)
    assert await service._service_state("cluster-es", "qwen-epp", "ns") == ("missing", 0)

    # Both forbidden -> unknown
    monkeypatch.setattr(service, "_kubectl", AsyncMock(return_value=_Result(returncode=1, stderr="Forbidden")))
    assert await service._service_state("cluster-es", "qwen-epp", "ns") == ("unknown", 0)

    # Unauthenticated -> unknown WITHOUT calling endpointslices
    unauth_calls: list[list[str]] = []

    async def fake_kubectl_unauth(_cluster_id, argv, **_kwargs):
        unauth_calls.append(list(argv))
        return _Result(returncode=1, stderr="Unauthorized")

    monkeypatch.setattr(service, "_kubectl", fake_kubectl_unauth)
    assert await service._service_state("cluster-es", "qwen-epp", "ns") == ("unknown", 0)
    assert len(unauth_calls) == 1


@pytest.mark.asyncio
async def test_member_health_non_admin_unknown_components_and_probe_members_stability(monkeypatch):
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-na-h")
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-na-h",
            cluster_id="cluster-na-h",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns/qwen",
            endpoint_kind="llm-d-epp",
            status="active",
        )
    )
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.11:6443}}]\n"
    cluster = SimpleNamespace(
        id="cluster-na-h",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)

    class _NaProbeRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "ns")
                )
            if list(argv[:2]) == ["kubectl", "get"]:
                kind = argv[2]
                if kind == "endpoints":
                    return _Result(returncode=1, stderr="Forbidden")
                if kind == "endpointslices":
                    return _Result(
                        stdout=json.dumps(
                            {
                                "items": [
                                    {"endpoints": [{"addresses": ["10.233.115.33"], "conditions": {"ready": True}}]}
                                ]
                            }
                        )
                    )
                if kind == "inferencepool":
                    return _Result(returncode=1, stderr="Forbidden")
            return _Result(returncode=1, stderr="Forbidden")

    service = GatewayOpsService(
        runner_factory=lambda _cid: _NaProbeRunner(),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(service, "reconcile_cluster", reconcile)

    ready, detail = await service._member_health(member)
    assert ready is True
    assert detail["components"]["epp"] == "ready"
    assert detail["components"]["inferencePool"] == "unknown"
    assert detail["components"]["modelServer"] == "unknown"

    updated = await service.probe_members()
    assert updated == 1
    saved = ModelServiceMemberDao().get(member.id)
    assert saved.status == "active"
    assert saved.health_json["components"]["modelServer"] == "unknown"
    reconcile.assert_not_awaited()

    # Even if an active non-admin member has an inconclusive health fluctuation (e.g. epp temporarily unknown),
    # probe_members never triggers Non-Admin reconcile or flips active -> unhealthy on unknown-only.
    monkeypatch.setattr(
        service,
        "_member_health",
        AsyncMock(
            return_value=(
                False,
                {
                    "ready": False,
                    "addresses": 0,
                    "checkedAt": "2026-11-24T12:00:00Z",
                    "components": {"epp": "unknown", "inferencePool": "unknown", "modelServer": "unknown"},
                },
            )
        ),
    )
    await service.probe_members()
    assert ModelServiceMemberDao().get(member.id).status == "active"
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_handoff_plan_is_declarative_across_unhealthy_members(monkeypatch):
    cluster_id = "cluster-decl"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.12:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3", cluster_id=cluster_id)
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-decl-1",
            cluster_id=cluster_id,
            target_namespace="llmd-ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="llmd-ns/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
            weight=100,
            status="active",
        )
    )

    class _DeclRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _DeclRunner())
    healthy_plan = await ops.get_remediation(cluster_id, mode="routes")

    # Simulate transient runtime probe failure flipping member status + health_json["components"] to missing.
    member.status = "unhealthy"
    member.health_json = {"components": {"epp": "missing", "inferencePool": "missing", "modelServer": "missing"}}
    ModelServiceMemberDao().save_health(member)

    unhealthy_plan = await ops.get_remediation(cluster_id, mode="routes")
    assert unhealthy_plan.desired_hash == healthy_plan.desired_hash
    assert any(op.action == "apply" and op.kind == "HTTPRoute" for op in unhealthy_plan.pending_operations)
    assert not any(op.action == "delete" for op in unhealthy_plan.pending_operations)


@pytest.mark.asyncio
async def test_desired_hash_deterministic_ordering_and_service_type_pinning(monkeypatch):
    cluster_id = "cluster-det"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.13:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Multi Group Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_port=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")

    g1 = ModelServiceGroup(id="msg-b", name="model-b", model_ref="Qwen/B", cluster_id=cluster_id)
    g2 = ModelServiceGroup(id="msg-a", name="model-a", model_ref="Qwen/A", cluster_id=cluster_id)
    m1 = ModelServiceMember(
        id="msm-2",
        group_id="msg-a",
        execution_id="e2",
        cluster_id=cluster_id,
        target_namespace="ns2",
        target_service="p2-epp",
        target_port=8000,
        pool_name="ns2/p2",
        epp_ref="p2-epp",
        endpoint_kind="llm-d-epp",
        weight=40,
    )
    m2 = ModelServiceMember(
        id="msm-1",
        group_id="msg-a",
        execution_id="e1",
        cluster_id=cluster_id,
        target_namespace="ns1",
        target_service="p1-epp",
        target_port=8000,
        pool_name="ns1/p1",
        epp_ref="p1-epp",
        endpoint_kind="llm-d-epp",
        weight=60,
    )
    m3 = ModelServiceMember(
        id="msm-3",
        group_id="msg-b",
        execution_id="e3",
        cluster_id=cluster_id,
        target_namespace="ns3",
        target_service="p3-epp",
        target_port=8000,
        pool_name="ns3/p3",
        epp_ref="p3-epp",
        endpoint_kind="llm-d-epp",
        weight=100,
    )

    class _ViewRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("verb") in ("get", "list"))
                )
            return _Result(returncode=1, stderr="not found")

    ops = GatewayOpsService(runner_factory=lambda _cid: _ViewRunner())
    monkeypatch.setattr(ops.groups, "list", lambda: [g1, g2])
    monkeypatch.setattr(ops.members, "list_by_cluster", lambda _cid: [m1, m2, m3])
    p_forward = await ops.get_remediation(cluster_id, mode="routes")

    monkeypatch.setattr(ops.groups, "list", lambda: [g2, g1])
    monkeypatch.setattr(ops.members, "list_by_cluster", lambda _cid: [m3, m2, m1])
    p_reverse = await ops.get_remediation(cluster_id, mode="routes")
    assert p_forward.desired_hash == p_reverse.desired_hash

    # Service-type pinning on gateway_repair: even if live cluster changes from None to LoadBalancer after apply,
    # get_remediation and verify_remediation use the pinned gatewayServiceType.
    svc_type_state = {"val": None}
    monkeypatch.setattr(ops, "_existing_gateway_service_type", AsyncMock(side_effect=lambda *_a: svc_type_state["val"]))
    repair = await ops.create_remediation(cluster_id, mode="gateway_repair")
    svc_type_state["val"] = "NodePort"
    preview_after = await ops.get_remediation(cluster_id, mode="gateway_repair")
    assert preview_after.desired_hash == repair.desired_hash


@pytest.mark.asyncio
async def test_multi_mode_snapshots_preserve_ipp_and_verify_out_of_order(monkeypatch):
    cluster_id = "cluster-mm"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.14:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Multi Group Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_port=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3", cluster_id=cluster_id)
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-mm-1",
            cluster_id=cluster_id,
            target_namespace="llmd-mm",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="llmd-mm/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )
    marker_store: dict[str, dict] = {}

    class _MmRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _MmRunner())
    ipp_handoff = await ops.create_remediation(cluster_id, mode="ipp_config", ipp_config="plugins:\n  - type: mm\n")
    routes_handoff = await ops.create_remediation(cluster_id, mode="routes")

    # routes did NOT clobber ipp_config's snapshot or ippConfigSha256!
    assert (await ops.get_remediation(cluster_id, mode="ipp_config")).desired_hash == ipp_handoff.desired_hash
    _, saved_mid = ops._load_handoff_locked(cluster_id)
    assert saved_mid["ippConfigSha256"]
    assert set(saved_mid["modeSnapshots"]) == {"ipp_config", "routes"}

    # Verifying ipp_config out of order succeeds, while leaving routes pending in DB!
    marker_store[ipp_handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": ipp_handoff.desired_hash, "status": "complete"},
    }
    res_ipp = await ops.verify_remediation(cluster_id, mode="ipp_config", expected_hash=ipp_handoff.desired_hash)
    assert res_ipp.verified is True
    assert res_ipp.reconcile_status == "admin_handoff_required"
    assert res_ipp.desired_hash == ipp_handoff.desired_hash
    _, saved_after_ipp = ops._load_handoff_locked(cluster_id)
    assert saved_after_ipp["reconcileStatus"] == "admin_handoff_required"
    assert saved_after_ipp["mode"] == "routes"

    # Now verifying routes completes the sync!
    marker_store[routes_handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": routes_handoff.desired_hash, "status": "complete"},
    }
    res_routes = await ops.verify_remediation(cluster_id, expected_hash=routes_handoff.desired_hash)
    assert res_routes.verified is True
    _, saved_final = ops._load_handoff_locked(cluster_id)
    assert saved_final["reconcileStatus"] == "synced"

    # Reverse order: create ipp_config -> create routes -> verify routes FIRST -> verify ipp_config SECOND!
    # Cluster MUST remain synced with routes's verifiedHash and desiredHash intact (never flipped back to pending).
    ipp_handoff2 = await ops.create_remediation(cluster_id, mode="ipp_config", ipp_config="plugins:\n  - type: mm2\n")
    routes_handoff2 = await ops.create_remediation(cluster_id, mode="routes")
    marker_store[routes_handoff2.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": routes_handoff2.desired_hash, "status": "complete"},
    }
    assert (await ops.verify_remediation(cluster_id, expected_hash=routes_handoff2.desired_hash)).verified is True
    _, after_routes_first = ops._load_handoff_locked(cluster_id)
    assert after_routes_first["reconcileStatus"] == "synced"
    routes_verified_hash = after_routes_first["verifiedHash"]

    marker_store[ipp_handoff2.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": ipp_handoff2.desired_hash, "status": "complete"},
    }
    res_ipp2 = await ops.verify_remediation(cluster_id, expected_hash=ipp_handoff2.desired_hash)
    assert res_ipp2.verified is True
    assert res_ipp2.reconcile_status == "synced"
    _, after_ipp_second = ops._load_handoff_locked(cluster_id)
    assert after_ipp_second["reconcileStatus"] == "synced"
    assert after_ipp_second["mode"] == "routes"
    assert after_ipp_second["verifiedHash"] == routes_verified_hash
    assert after_ipp_second["desiredHash"] == routes_verified_hash
    assert after_ipp_second["modeSnapshots"] == {}


@pytest.mark.asyncio
async def test_multi_group_post_delete_steady_state_and_repeat_recheck(monkeypatch):
    cluster_id = "cluster-mg"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.15:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Multi Group Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_port=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    group_a = ModelServiceGroupDao().create(
        ModelServiceGroup(name="model-a", model_ref="Qwen/A", base_model="model-a", cluster_id=cluster_id)
    )
    member_a = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group_a.id,
            execution_id="exec-mg-a",
            cluster_id=cluster_id,
            target_namespace="ns-a",
            target_service="a-epp",
            target_port=8000,
            pool_name="ns-a/pool-a",
            epp_ref="a-epp",
            endpoint_kind="llm-d-epp",
            weight=100,
        )
    )
    group_b = ModelServiceGroupDao().create(
        ModelServiceGroup(name="model-b", model_ref="Qwen/B", base_model="model-b", cluster_id=cluster_id)
    )
    member_b = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group_b.id,
            execution_id="exec-mg-b",
            cluster_id=cluster_id,
            target_namespace="ns-b",
            target_service="b-epp",
            target_port=8000,
            pool_name="ns-b/pool-b",
            epp_ref="b-epp",
            endpoint_kind="llm-d-epp",
            weight=100,
        )
    )
    marker_store: dict[str, dict] = {}

    class _MgRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _MgRunner())
    apply_ab = await ops.create_remediation(cluster_id, mode="routes")
    marker_store[apply_ab.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": apply_ab.desired_hash, "status": "complete"},
    }
    assert (await ops.verify_remediation(cluster_id, expected_hash=apply_ab.desired_hash)).verified is True

    # Delete member_b & group_b -> generates cleanup handoff H_delB alongside apply A.
    ModelServiceMemberDao().delete(member_b.id)
    await ops.record_member_deletion(member_b, group_b, remaining_members=[member_a])
    ModelServiceGroupDao().delete(group_b.id)
    async with gateway_ops_module.gateway_lock(cluster_id):
        del_handoff = await ops._record_deleted_group_handoff_locked(group_b)
    assert del_handoff.reconcile_status == "admin_handoff_required"

    marker_store.clear()
    marker_store[del_handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": del_handoff.desired_hash, "status": "complete"},
    }
    res_del = await ops.verify_remediation(cluster_id, expected_hash=del_handoff.desired_hash)
    assert res_del.verified is True
    assert res_del.desired_hash == del_handoff.desired_hash

    # Post-verify steady-state status() stays synced!
    st = await ops.status()
    assert st.clusters[0].reconcile_status == "synced"
    assert st.clusters[0].pending_operations_count == 0
    steady_hash = st.clusters[0].desired_hash
    assert steady_hash != del_handoff.desired_hash

    # Repeat Re-check on steady-state succeeds via lastVerifiedMarker!
    repeat_res = await ops.verify_remediation(cluster_id, expected_hash=steady_hash)
    assert repeat_res.verified is True
    assert repeat_res.reconcile_status == "synced"

    # Actual weight drift on group_a is STILL rejected as stale!
    ModelServiceMemberDao().save_traffic(
        group_a.id, [member_a.model_copy(update={"weight": 50})], {member_a.execution_id: 100}
    )
    drift_res = await ops.verify_remediation(cluster_id, expected_hash=steady_hash)
    assert drift_res.verified is False
    assert drift_res.marker_status == "stale"


@pytest.mark.asyncio
async def test_unapplied_mutation_revert_restores_synced_and_no_work_ok_ignores_stale_expected(monkeypatch):
    cluster_id = "cluster-rev"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.16:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Multi Group Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_port=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")

    marker_store: dict[str, dict] = {}

    class _RevRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            if list(argv[:2]) == ["kubectl", "delete"]:
                return _Result(stdout="deleted")
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","r2","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _RevRunner())

    # 1. Empty cluster with zero pending ops -> stale expected_hash still returns verified=True (no_work_ok).
    no_work = await ops.verify_remediation(cluster_id, expected_hash="a" * 64)
    assert no_work.verified is True
    assert no_work.reconcile_status == "synced"

    # 2. Verified cluster -> bind m2 (unapplied) -> remove m2 -> reverts to verifiedHash -> synced!
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="rev-m", model_ref="Qwen/R", base_model="rev-m", cluster_id=cluster_id)
    )
    m1 = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-rev-1",
            cluster_id=cluster_id,
            target_namespace="ns-r1",
            target_service="r1-epp",
            target_port=8000,
            pool_name="ns-r1/r1",
            epp_ref="r1-epp",
            endpoint_kind="llm-d-epp",
            weight=100,
        )
    )
    h1 = await ops.create_remediation(cluster_id, mode="routes")
    marker_store[h1.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": h1.desired_hash, "status": "complete"},
    }
    assert (await ops.verify_remediation(cluster_id, expected_hash=h1.desired_hash)).verified is True

    # Bind m2 in permitted namespace lens-edit (creates H2 handoff, NOT applied on cluster)
    m2 = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-rev-2",
            cluster_id=cluster_id,
            target_namespace="lens-edit",
            target_service="r2-epp",
            target_port=8000,
            pool_name="lens-edit/r2",
            epp_ref="r2-epp",
            endpoint_kind="llm-d-epp",
            weight=50,
        )
    )
    op_h2 = await ops.reconcile_cluster_gateway(cluster_id)
    assert ops.handoff_from_operation(op_h2) is not None

    # Remove m2 before H2 was ever applied -> desiredHash reverts to H1 == verifiedHash -> restored to synced!
    ModelServiceMemberDao().delete(m2.id)
    del_res = await ops.record_member_deletion(m2, group, remaining_members=[m1])
    assert del_res is not None
    assert del_res.desired_hash == h1.desired_hash
    assert del_res.reconcile_status == "synced"


@pytest.mark.asyncio
async def test_superset_candidates_cover_omitted_inferred_deletes(monkeypatch):
    cluster_id = "cluster-sup-del"
    boot_ns = "llmd-sup-del"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.17:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="dis-model",
            model_ref="Qwen/Qwen3-8B",
            base_model="dis-model",
            cluster_id=cluster_id,
            status="disabled",
        )
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-sd-1",
            cluster_id=cluster_id,
            target_namespace=boot_ns,
            target_service="qwen-epp",
            target_port=8000,
            pool_name=f"{boot_ns}/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )

    permit_boot_ns = False
    marker_store: dict[str, dict] = {}

    class _Runner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                allowed = {boot_ns, "lens-edit"} if permit_boot_ns else {"lens-edit"}
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") in allowed)
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            if list(argv[:2]) == ["kubectl", "delete"]:
                return _Result(stdout="deleted")
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _Runner())
    # Pretend group was previously synced so disabled group emits inferred_deletes.
    ops._save_handoff_locked(cluster_id, {"syncedGroupIds": [group.id], "mode": "routes"}, message="prior")
    h1 = await ops.create_remediation(cluster_id, mode="routes")
    assert any(op.kind == "referencegrant" for op in h1.pending_operations)
    marker_store[h1.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": h1.desired_hash, "status": "complete"},
    }

    # Now boot_ns becomes permitted -> H2 omits member-ns deletes -> verify_remediation(expected_hash=H1) still passes!
    permit_boot_ns = True
    h2 = await ops.get_remediation(cluster_id, mode="routes")
    assert h2.desired_hash != h1.desired_hash
    res = await ops.verify_remediation(cluster_id, expected_hash=h1.desired_hash)
    assert res.verified is True
    assert res.observed_hash == h1.desired_hash


def test_handoff_script_mode_scoped_prune_and_gateway_discovery_syntax():
    import subprocess

    script = GatewayOpsService._build_handoff_script(
        cluster_id="e66f4591",
        gw_ns="lens-gateway-e66f4591",
        mode="routes",
        desired_hash="a" * 64,
        marker_namespace="lens-edit",
        marker_name="lens-ms-handoff-" + "a" * 16,
        apply_docs=[],
        delete_items=[],
        epp_targets=[],
        ipp_release=None,
        gw_name="lens-inference-gateway",
        gw_provider="istio",
    )
    subprocess.run(["bash", "-n", "-"], input=script, text=True, check=True)
    assert "jq" not in script and "python3" not in script
    assert "gatewayPublicUrl" not in script
    assert "discover_gateway_endpoint || true" in script
    assert "prune_old_markers || true" in script
    assert "lens.llm-d.ai/handoff-mode=$MODE" in script
    assert "*.svc|*.svc.*|*.cluster.local" in script
    assert "IFS='|'" in script
    assert 'n_ext="${n_ext%% *}"' in script
    assert 'GATEWAY_ADDRESS="${GATEWAY_ADDRESS%.}"' in script

    envoy_script = GatewayOpsService._build_handoff_script(
        cluster_id="e66f4591",
        gw_ns="lens-gateway-e66f4591",
        mode="routes",
        desired_hash="b" * 64,
        marker_namespace="lens-edit",
        marker_name="lens-ms-handoff-" + "b" * 16,
        apply_docs=[],
        delete_items=[],
        epp_targets=[],
        ipp_release=None,
        gw_name="lens-inference-gateway",
        gw_provider="envoy-ai-gateway",
    )
    subprocess.run(["bash", "-n", "-"], input=envoy_script, text=True, check=True)
    assert "envoy-gateway-system" in envoy_script
    assert "gateway.envoyproxy.io/owning-gateway-name=$GATEWAY_NAME" in envoy_script
    assert 'kubectl get service "$svc_name" -n "$svc_ns"' in envoy_script


@pytest.mark.asyncio
async def test_verify_remediation_http_probe_header_and_advisory_vs_blocking(monkeypatch):
    cluster_id = "cluster-http"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.18:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="HTTP Probe Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="http://10.0.0.18:30080/v1",
        gateway_port=30080,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(
            name="svc-alias",
            model_ref="Qwen/Qwen3-8B",
            base_model="qwen3",
            cluster_id=cluster_id,
        )
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-http-1",
            cluster_id=cluster_id,
            target_namespace="lens-edit",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="lens-edit/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )

    marker_store: dict[str, dict] = {}
    can_read_gw = False

    class _HttpRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(
                        payload["items"],
                        allow_fn=lambda a: (
                            a.get("namespace") == "lens-edit" or (can_read_gw and a.get("verb") in ("get", "list"))
                        ),
                    )
                )
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "get", "httproute"] and can_read_gw:
                return _Result(
                    stdout=json.dumps(
                        {"status": {"parents": [{"conditions": [{"type": "Accepted", "status": "True"}]}]}}
                    )
                )
            return _Result(returncode=1, stderr="Forbidden")

    captured_headers: list[dict] = []
    probe_mode: list[object] = [503]

    class _MockHttpClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=404)

        async def post(self, _url, **kwargs):
            captured_headers.append(dict(kwargs.get("headers") or {}))
            val = probe_mode[0]
            if isinstance(val, Exception):
                raise val
            if val == "authz-503":
                return SimpleNamespace(status_code=503, content=b"")
            return SimpleNamespace(status_code=int(val), content=b"upstream error")

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _MockHttpClient)
    ops = GatewayOpsService(runner_factory=lambda _cid: _HttpRunner())
    handoff = await ops.create_remediation(cluster_id, mode="routes")
    marker_store[handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": handoff.desired_hash, "status": "complete"},
    }

    # 1. 503 -> loading (advisory, verified=True) + header X-Gateway-Base-Model-Name sent
    probe_mode[0] = 503
    res_503 = await ops.verify_remediation(cluster_id, expected_hash=handoff.desired_hash)
    assert res_503.verified is True
    assert res_503.reconcile_status == "synced"
    assert res_503.route_checks[0]["status"] == "loading"
    assert res_503.route_checks[0]["advisory"] is True
    assert captured_headers[-1]["X-Gateway-Base-Model-Name"] == "qwen3"

    # 1b. empty 503 is the ext_authz failure, not a loading backend
    probe_mode[0] = "authz-503"
    res_authz = await ops.verify_remediation(cluster_id, expected_hash=handoff.desired_hash)
    assert res_authz.route_checks[0]["status"] == "degraded"
    assert "authorization" in res_authz.route_checks[0]["detail"]

    # 2. 500 -> degraded (advisory, verified=True)
    probe_mode[0] = 500
    res_500 = await ops.verify_remediation(cluster_id, expected_hash=handoff.desired_hash)
    assert res_500.verified is True
    assert res_500.route_checks[0]["status"] == "degraded"
    assert res_500.route_checks[0]["advisory"] is True

    # 3. Network error -> unreachable (advisory, verified=True)
    probe_mode[0] = RuntimeError("connect timeout")
    res_unreach = await ops.verify_remediation(cluster_id, expected_hash=handoff.desired_hash)
    assert res_unreach.verified is True
    assert res_unreach.route_checks[0]["status"] == "unreachable"
    assert res_unreach.route_checks[0]["advisory"] is True

    # 4. 404 on Non-Admin (can_read_gateway_namespace=False) -> blocking (verified=False)
    probe_mode[0] = 404
    res_404_na = await ops.verify_remediation(cluster_id, expected_hash=handoff.desired_hash)
    assert res_404_na.verified is False
    assert res_404_na.route_checks[0]["status"] == "pending"
    assert res_404_na.route_checks[0]["advisory"] is False

    # 5. 404 when can_read_gateway_namespace=True & k8s_routes_ok=True -> advisory (verified=True)
    can_read_gw = True
    res_404_ro = await ops.verify_remediation(cluster_id, expected_hash=handoff.desired_hash)
    assert res_404_ro.verified is True
    assert res_404_ro.route_checks[0]["status"] == "pending"
    assert res_404_ro.route_checks[0]["advisory"] is True

    # 6. _handoff_summary_for_status auto-verify also treats 503 as advisory (synced)
    can_read_gw = False
    await ops.create_remediation(cluster_id, mode="routes")
    probe_mode[0] = 503
    st = await ops.status()
    assert st.clusters[0].reconcile_status == "synced"


@pytest.mark.asyncio
async def test_probe_gateway_via_prometheus_ksm_excludes_sysctl_and_handles_zero_replicas(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_module

    class _DummyPromClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value="http://prom:9090"))
    monkeypatch.setattr(prom_module, "create_prometheus_client", lambda *_a, **_kw: _DummyPromClient())
    monkeypatch.setattr(prom_module, "check_prometheus_health", AsyncMock(return_value=True))

    seen_queries: list[str] = []
    avail_value = ["1"]

    async def fake_query_vector(_client, promql):
        seen_queries.append(promql)
        if promql.startswith("kube_deployment_status_replicas_available") and "lens-inference-gateway" in promql:
            return [{"metric": {"deployment": "lens-inference-gateway-istio"}, "value": [1700000000, avail_value[0]]}]
        return []

    monkeypatch.setattr(prom_module, "query_vector", fake_query_vector)
    ops = GatewayOpsService()

    gw_ready, ipp_state, ipp_ready = await ops._probe_gateway_via_prometheus(
        "e66f4591", "lens-gateway-e66f4591", gateway_name="lens-inference-gateway", provider="istio"
    )
    assert gw_ready is True
    assert ipp_state == "missing"
    assert ipp_ready is False
    assert any('deployment!="lens-ipp",deployment!="lens-node-sysctl"' in q for q in seen_queries)

    # Zero available replicas -> gw_ready=False
    avail_value[0] = "0"
    gw_ready_zero, _, _ = await ops._probe_gateway_via_prometheus(
        "e66f4591", "lens-gateway-e66f4591", gateway_name="lens-inference-gateway", provider="istio"
    )
    assert gw_ready_zero is False

    # KSM absent everywhere -> (None, None, None)
    async def empty_query_vector(_client, _promql):
        return []

    monkeypatch.setattr(prom_module, "query_vector", empty_query_vector)
    assert await ops._probe_gateway_via_prometheus(
        "e66f4591", "lens-gateway-e66f4591", gateway_name="lens-inference-gateway", provider="istio"
    ) == (None, None, None)


@pytest.mark.asyncio
async def test_status_uses_marker_discovered_gateway_with_ssrf_guard(monkeypatch):
    cluster_id = "cluster-disc"
    kubeconfig = (
        "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://169.254.169.254:6443}}]\n"
    )
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Discovered Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3", cluster_id=cluster_id)
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-disc-1",
            cluster_id=cluster_id,
            target_namespace="lens-edit",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="lens-edit/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
        )
    )

    marker_store: dict[str, dict] = {}

    class _DiscRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "apply", "-f"]:
                return _Result(stdout="applied")
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            return _Result(returncode=1, stderr="Forbidden")

    probed_urls: list[str] = []

    class _DiscHttp:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url):
            probed_urls.append(url)
            return SimpleNamespace(status_code=404)

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _DiscHttp)
    ops = GatewayOpsService(runner_factory=lambda _cid: _DiscRunner())
    handoff = await ops.create_remediation(cluster_id, mode="routes")
    marker_store[handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {
            "desiredHash": handoff.desired_hash,
            "status": "complete",
            "gatewayPort": "32592",
            "gatewayNodeAddress": "10.112.228.229",
            "gatewayAddress": "lens-inference-gateway-istio.lens-gateway.svc.cluster.local",
            "gatewayPublicUrl": "http://169.254.169.254:80/v1",
        },
    }

    res = await ops.verify_remediation(cluster_id, expected_hash=handoff.desired_hash)
    assert res.verified is True
    # Metadata IP from kubeconfig_server_host was blocked by SSRF guard, fell back to gatewayNodeAddress:32592!
    # .svc.cluster.local and injected gatewayPublicUrl were both rejected/ignored!
    assert probed_urls == ["http://10.112.228.229:32592/v1/models"]

    probed_urls.clear()
    st = await ops.status()
    entry = st.clusters[0]
    assert entry.gateway_state == "installed"
    assert entry.gateway_ready is True
    assert entry.gateway_probe_source == "http"
    assert entry.gateway_port == 32592
    assert entry.gateway_node_address == "10.112.228.229"
    assert entry.gateway_address is None
    assert entry.gateway_public_url is None
    assert probed_urls == ["http://10.112.228.229:32592/v1/models"]


@pytest.mark.asyncio
async def test_unauthenticated_401_never_masked_in_member_health_probe_or_handoff_status(monkeypatch):
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="auth401", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-401")
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-401",
            cluster_id="cluster-401",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns/qwen",
            endpoint_kind="llm-d-epp",
            status="active",
        )
    )
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.19:6443}}]\n"
    cluster = SimpleNamespace(
        id="cluster-401",
        name="Auth 401 Cluster",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])

    kubectl_calls: list[list[str]] = []

    class _Runner401:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            kubectl_calls.append(list(argv))
            return _Result(returncode=1, stderr="Unauthorized: Bearer secret-token-value")

    service = GatewayOpsService(
        runner_factory=lambda _cid: _Runner401(),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    service._save_handoff_locked(
        "cluster-401",
        {
            "reconcileStatus": "admin_handoff_required",
            "desiredHash": "a" * 64,
            "markerNamespace": "lens-edit",
            "markerName": "lens-ms-handoff-" + "a" * 16,
            "mode": "routes",
            "provider": "istio",
            "pendingDeletes": [],
        },
        message="pending",
    )

    # 1. caps.auth_error -> _member_health fails closed immediately; probe_members flips active -> unhealthy
    ready, detail = await service._member_health(member)
    assert ready is False
    assert "error" in detail
    assert "secret-token-value" not in detail["error"]
    await service.probe_members()
    assert ModelServiceMemberDao().get(member.id).status == "unhealthy"

    # 2. Even if caps was cached valid just before token expiry, mid-probe 401 still fails closed!
    ModelServiceMemberDao().save_health(member.model_copy(update={"status": "active"}))
    monkeypatch.setattr(
        service,
        "probe_capabilities",
        AsyncMock(
            return_value=gateway_ops_module.ClusterGatewayCapabilities(
                can_manage_gateway_infra=False,
                can_manage_gateway_config=False,
                can_reconcile_routes=False,
                access_mode="non_admin",
                can_read_gateway_namespace=False,
                can_port_forward_gateway=False,
                permitted_member_namespaces=frozenset({"ns"}),
                auth_error=None,
                probe_conclusive=True,
            )
        ),
    )
    ready_mid, detail_mid = await service._member_health(member)
    assert ready_mid is False
    assert "error" in detail_mid
    await service.probe_members()
    assert ModelServiceMemberDao().get(member.id).status == "unhealthy"

    # 3. _handoff_summary_for_status with caps.auth_error issues ZERO kubectl get configmap calls!
    kubectl_calls.clear()
    caps_err = gateway_ops_module.ClusterGatewayCapabilities(
        can_manage_gateway_infra=False,
        can_manage_gateway_config=False,
        can_reconcile_routes=False,
        access_mode="non_admin",
        can_read_gateway_namespace=False,
        can_port_forward_gateway=False,
        auth_error="Unauthorized",
        probe_conclusive=True,
    )
    await service._handoff_summary_for_status("cluster-401", cluster, caps=caps_err)
    assert not any(c[:3] == ["kubectl", "get", "configmap"] for c in kubectl_calls)


def test_prune_old_markers_prefix_and_newline_injection_safety(tmp_path):
    import os
    import stat
    import subprocess

    script = GatewayOpsService._build_handoff_script(
        cluster_id="e66f4591",
        gw_ns="lens-gateway-e66f4591",
        mode="routes",
        desired_hash="a" * 64,
        marker_namespace="lens-edit",
        marker_name="lens-ms-handoff-" + "a" * 16,
        apply_docs=[],
        delete_items=[],
        epp_targets=[],
        ipp_release=None,
        gw_name="lens-inference-gateway",
        gw_provider="istio",
    )
    assert "while IFS= read -r cm_name; do" in script
    assert 'if [[ "$cm_name" == lens-ms-handoff-* && "$cm_name" != "$MARKER_NAME" ]]; then' in script
    assert '[[ "$leg_name" == lens-ms-handoff-* && "$leg_name" != "$MARKER_NAME" ]] || continue' in script
    assert '[[ "$p" == "80" && "$np" =~ ^[0-9]+$ ]]' in script

    # Execute prune_old_markers in bash against a mock kubectl simulating:
    # - a non-handoff ConfigMap with handoff labels (must NOT be deleted)
    # - a legacy ConfigMap whose .data.mode contains newline+tab injection attempting to target kube-root-ca.crt
    # - a real old legacy routes marker lens-ms-handoff-0000000000000000 (SHOULD be deleted)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    deleted_log = tmp_path / "deleted.txt"
    mock_kubectl = bin_dir / "kubectl"
    mock_kubectl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "get" && "$2" == "configmap" ]]; then\n'
        '  if [[ "$*" == *"!lens.llm-d.ai/handoff-mode"* ]]; then\n'
        '    printf "kube-root-ca.crt\\nlens-ms-handoff-1111111111111111\\nlens-ms-handoff-0000000000000000\\n"\n'
        '  elif [[ "$3" == "lens-ms-handoff-1111111111111111" ]]; then\n'
        '    printf "ipp_config\\nkube-root-ca.crt\\troutes"\n'
        '  elif [[ "$3" == "lens-ms-handoff-0000000000000000" ]]; then\n'
        '    printf "routes"\n'
        '  elif [[ "$*" == *"-o name"* ]]; then\n'
        '    printf "configmap/arbitrary-cm\\nconfigmap/lens-ms-handoff-2222222222222222\\n"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "delete" && "$2" == "configmap" ]]; then\n'
        f'  printf "%s\\n" "$3" >> "{deleted_log}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    mock_kubectl.chmod(mock_kubectl.stat().st_mode | stat.S_IXUSR)

    # Extract function definitions only and call prune_old_markers directly.
    fn_end = script.index("discover_gateway_endpoint || true")
    harness = script[:fn_end] + "\nprune_old_markers\n"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    subprocess.run(["bash", "-c", harness], env=env, check=True)
    deleted_names = deleted_log.read_text(encoding="utf-8").splitlines()
    assert deleted_names == [
        "lens-ms-handoff-2222222222222222",
        "lens-ms-handoff-0000000000000000",
    ]


@pytest.mark.asyncio
async def test_discovered_gateway_rejects_loopback_and_dns_blocked_hosts(monkeypatch):
    # 1. Literal loopback & localhost variants rejected by _parse_discovered_gateway
    for bad_host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", "api.localhost"):
        parsed = GatewayOpsService._parse_discovered_gateway(
            {"gatewayPort": "32592", "gatewayNodeAddress": bad_host, "gatewayAddress": bad_host}
        )
        assert parsed == {"gatewayPort": 32592}

    # 2. DNS-blocked hostname (resolving to 169.254.169.254 or 127.0.0.1) is NOT probed or surfaced in status()
    cluster_id = "cluster-ssrf-dns"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.20:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="DNS SSRF Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    import asyncio

    loop = asyncio.get_running_loop()
    orig_getaddrinfo = loop.getaddrinfo

    async def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "evil-rebind.example.com":
            return [(2, 1, 6, "", ("127.0.0.1", int(port or 80)))]
        if host == "evil-meta.example.com":
            return [(2, 1, 6, "", ("169.254.169.254", int(port or 80)))]
        return await orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    class _NaRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            return _Result(returncode=1, stderr="Forbidden")

    probed_urls: list[str] = []

    class _ProbeHttp:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url):
            probed_urls.append(url)
            return SimpleNamespace(status_code=404)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _ProbeHttp)
    ops = GatewayOpsService(runner_factory=lambda _cid: _NaRunner())
    ops._save_handoff_locked(
        cluster_id,
        {
            "reconcileStatus": "synced",
            "desiredHash": "a" * 64,
            "verifiedHash": "a" * 64,
            "hasVerifiedSync": True,
            "mode": "routes",
            "discoveredGateway": {
                "gatewayPort": 32592,
                "gatewayNodeAddress": "evil-rebind.example.com",
                "gatewayAddress": "evil-meta.example.com",
            },
        },
        message="synced",
    )
    st = await ops.status()
    entry = st.clusters[0]
    # Only trusted kubeconfig_server_host:32592 was probed; DNS-blocked ConfigMap hosts were NOT surfaced!
    assert probed_urls == ["http://10.0.0.20:32592/v1/models"]
    assert entry.gateway_node_address == "10.0.0.20"
    assert entry.gateway_address is None

    # 3. cluster_gateway_base_url() validates final URL via _ensure_safe_gateway_url
    for bad_cluster in (
        gateway_ops_module.GatewayStatusCluster(
            cluster_id=cluster_id,
            gateway_ready=True,
            gateway_public_url="169.254.169.254",
        ),
        gateway_ops_module.GatewayStatusCluster(
            cluster_id=cluster_id,
            gateway_ready=True,
            gateway_address="evil-meta.example.com",
        ),
    ):
        monkeypatch.setattr(
            ops,
            "status",
            AsyncMock(return_value=gateway_ops_module.GatewayStatus(clusters=[bad_cluster])),
        )
        assert await ops.cluster_gateway_base_url(cluster_id) is None


@pytest.mark.asyncio
async def test_probe_gateway_via_prometheus_rejects_invalid_names_for_promql(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_module

    resolve_mock = AsyncMock(return_value="http://prom:9090")
    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", resolve_mock)
    ops = GatewayOpsService()

    assert await ops._probe_gateway_via_prometheus("cluster-x", 'ns"} or up{a="1') == (None, None, None)
    assert await ops._probe_gateway_via_prometheus("cluster-x", "lens-gateway", gateway_name='gw"\n') == (
        None,
        None,
        None,
    )
    resolve_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_serving_state_sends_base_model_header_for_ready_and_pending(monkeypatch):
    captured_headers: list[dict[str, str]] = []
    status_codes = [401, 404, 401]

    class _HeaderClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **kwargs):
            captured_headers.append(dict(kwargs.get("headers") or {}))
            return SimpleNamespace(status_code=status_codes.pop(0))

    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_public_url="http://10.0.0.10:30080/v1",
            gateway_port=30080,
        ),
    )
    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HeaderClient)
    service = GatewayOpsService()
    member = SimpleNamespace(cluster_id="cluster-hdr")

    # 1. group without base_model sends group.name and maps 401 -> ready
    group_plain = SimpleNamespace(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model=None)
    assert await service._gateway_serving_state(member, group_plain) == "ready"
    assert captured_headers[-1]["X-Gateway-Base-Model-Name"] == "qwen3"

    # 2. 404 still maps to pending even when header is sent
    assert await service._gateway_serving_state(member, group_plain) == "pending"
    assert captured_headers[-1]["X-Gateway-Base-Model-Name"] == "qwen3"

    # 3. aliased group with explicit base_model sends base_model
    group_aliased = SimpleNamespace(name="svc-alias", model_ref="Qwen/Qwen3-8B", base_model="qwen3")
    assert await service._gateway_serving_state(member, group_aliased) == "ready"
    assert captured_headers[-1]["X-Gateway-Base-Model-Name"] == "qwen3"


@pytest.mark.asyncio
async def test_member_health_non_admin_pending_handoff_empty_parents_standby_vs_degraded(monkeypatch):
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", cluster_id="cluster-na-pool")
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-na-pool",
            cluster_id="cluster-na-pool",
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns/pool",
            endpoint_kind="llm-d-epp",
            status="active",
        )
    )
    cluster = SimpleNamespace(
        id="cluster-na-pool",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)

    pool_status_box: list[dict] = [{}]
    calls: list = []

    class _PoolRunner(_ComponentRunner):
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "get", "inferencepool"]:
                self.calls.append((argv, input))
                return _Result(
                    stdout=json.dumps(
                        {
                            "spec": {"selector": {"matchLabels": {"llm-d.ai/guide": "pool"}}},
                            "status": pool_status_box[0],
                        }
                    )
                )
            return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)

    service = GatewayOpsService(
        runner_factory=lambda _cid: _PoolRunner(calls),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    non_admin_caps = gateway_ops_module.ClusterGatewayCapabilities(
        can_manage_gateway_infra=False,
        can_manage_gateway_config=False,
        can_reconcile_routes=False,
        access_mode="non_admin",
        can_read_gateway_namespace=False,
        can_port_forward_gateway=False,
        permitted_member_namespaces=frozenset({"ns"}),
        probe_conclusive=True,
    )
    admin_caps = gateway_ops_module.ClusterGatewayCapabilities(
        can_manage_gateway_infra=True,
        can_manage_gateway_config=True,
        can_reconcile_routes=True,
        access_mode="admin",
        can_read_gateway_namespace=True,
        can_port_forward_gateway=True,
        probe_conclusive=True,
    )
    caps_box = [non_admin_caps]
    monkeypatch.setattr(service, "probe_capabilities", AsyncMock(side_effect=lambda *_a, **_kw: caps_box[0]))

    # 1. Non-Admin with pending admin_handoff_required + InferencePool status: {} -> standby (not degraded)
    service._save_handoff_locked(
        "cluster-na-pool",
        {
            "reconcileStatus": "admin_handoff_required",
            "syncedGroupIds": [],
            "hasVerifiedSync": False,
        },
        message="pending",
    )
    pool_status_box[0] = {}
    ready_pending, detail_pending = await service._member_health(member)
    assert ready_pending is True
    assert detail_pending["components"]["inferencePool"] == "standby"
    assert not gateway_ops_module._data_plane_degraded(detail_pending["components"])

    # 2. Non-Admin with pending handoff STILL marks non-empty failing parents (Accepted=False) as degraded
    pool_status_box[0] = {"parents": [{"conditions": [{"type": "Accepted", "status": "False"}]}]}
    ready_rejected, detail_rejected = await service._member_health(member)
    assert ready_rejected is False
    assert detail_rejected["components"]["inferencePool"] == "degraded"

    # 3. Non-Admin once synced (reconcileStatus=synced, group in syncedGroupIds) marks empty parents as degraded
    service._save_handoff_locked(
        "cluster-na-pool",
        {
            "reconcileStatus": "synced",
            "syncedGroupIds": [group.id],
            "hasVerifiedSync": True,
        },
        message="synced",
    )
    pool_status_box[0] = {}
    ready_synced_empty, detail_synced_empty = await service._member_health(member)
    assert ready_synced_empty is False
    assert detail_synced_empty["components"]["inferencePool"] == "degraded"

    # 4. Admin cluster (can_read_gateway_namespace=True) STILL marks empty parents on routed pool as degraded
    caps_box[0] = admin_caps
    ready_admin_empty, detail_admin_empty = await service._member_health(member)
    assert ready_admin_empty is False
    assert detail_admin_empty["components"]["inferencePool"] == "degraded"


@pytest.mark.asyncio
async def test_install_gateway_installs_ipp_and_gateway_repair_includes_ipp(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda cid: SimpleNamespace(
            id=cid,
            name="Admin Cluster",
            gateway_provider="istio",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_port=None,
        ),
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)
    monkeypatch.setattr(gateway_ops_module, "apply_gateway_crds", AsyncMock(return_value={}))
    monkeypatch.setattr(gateway_ops_module, "ensure_istioctl", lambda _v: "istioctl")

    service = _service(calls=calls)
    op = await service.install_inference_gateway("cluster-ipp", "istio", install_prerequisites=True)
    assert op.status == "succeeded"
    assert op.detail_json["ippInstalled"] is True
    assert op.detail_json["ippName"] == "lens-ipp"
    assert any(list(argv[:4]) == ["helm", "upgrade", "--install", "lens-ipp"] for argv, _ in calls)
    assert any(list(argv[:4]) == ["kubectl", "rollout", "status", "deploy/lens-ipp"] for argv, _ in calls)

    # Explicit install_ipp=False skips IPP
    calls.clear()
    op_no_ipp = await service.install_inference_gateway(
        "cluster-ipp", "istio", install_prerequisites=True, install_ipp=False
    )
    assert op_no_ipp.status == "succeeded"
    assert "ippInstalled" not in op_no_ipp.detail_json
    assert not any(argv[0] == "helm" for argv, _ in calls)

    # gateway_repair remediation script includes helm-lens-ipp + TCP probes on port 9004
    repair = await service.get_remediation("cluster-ipp", mode="gateway_repair")
    assert any(item.id == "helm-lens-ipp" for item in repair.pending_operations)
    assert "helm upgrade --install lens-ipp " in repair.admin_script
    assert '"port":9004' in repair.admin_script
    assert "kubectl rollout status deploy/lens-ipp" in repair.admin_script


@pytest.mark.asyncio
async def test_install_ipp_unlinks_temp_values_file_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda cid: SimpleNamespace(id=cid, gateway_namespace="lens-gateway", gateway_provider="istio"),
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)
    created_paths: list[str] = []
    real_ntf = gateway_ops_module.tempfile.NamedTemporaryFile

    def tracking_ntf(*args, **kwargs):
        handle = real_ntf(*args, **kwargs)
        created_paths.append(handle.name)
        return handle

    monkeypatch.setattr(gateway_ops_module.tempfile, "NamedTemporaryFile", tracking_ntf)

    class _FailHelmRunner:
        async def run(self, argv, **_kw):
            if argv[0] == "helm":
                return _Result(returncode=1, stderr="helm boom")
            return _Result()

    service = GatewayOpsService(runner_factory=lambda _cid: _FailHelmRunner())
    op = await service.install_ipp("cluster-tmp", config="plugins:\n  - type: test\n")
    assert op.status == "failed"
    assert len(created_paths) == 1
    assert not gateway_ops_module.os.path.exists(created_paths[0])


@pytest.mark.asyncio
async def test_prometheus_ksm_details_scaled_zero_envoy_prefix_ssrf_and_status(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_module

    class _DummyPromClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value="http://prom:9090"))
    monkeypatch.setattr(prom_module, "create_prometheus_client", lambda *_a, **_kw: _DummyPromClient())
    monkeypatch.setattr(prom_module, "check_prometheus_health", AsyncMock(return_value=True))

    scenario = {"mode": "scaled_zero"}

    async def fake_query_vector(_client, promql):
        if scenario["mode"] == "scaled_zero":
            if promql.startswith("kube_namespace_status_phase"):
                return [{"metric": {"namespace": "lens-gateway-c1"}, "value": [1, "1"]}]
            if promql.startswith("kube_deployment_spec_replicas") and "lens-inference-gateway" in promql:
                return [{"metric": {"deployment": "lens-inference-gateway-istio"}, "value": [1, "0"]}]
            if promql.startswith("kube_pod_info") and "lens-inference-gateway" in promql:
                return [
                    {"metric": {"host_ip": "169.254.169.254"}, "value": [1, "1"]},
                    {"metric": {"host_ip": "10.239.11.215"}, "value": [1, "1"]},
                ]
            return []
        if scenario["mode"] == "envoy_running":
            if 'namespace="envoy-gateway-system"' in promql and "envoy-.*lens-inference-gateway.*" in promql:
                if promql.startswith("kube_deployment_spec_replicas"):
                    return [{"metric": {"deployment": "envoy-lens-gateway-c1"}, "value": [1, "1"]}]
                if promql.startswith("kube_deployment_status_replicas_available"):
                    return [{"metric": {"deployment": "envoy-lens-gateway-c1"}, "value": [1, "1"]}]
                if promql.startswith("kube_pod_info"):
                    return [{"metric": {"host_ip": "10.239.11.216"}, "value": [1, "1"]}]
            return []
        if scenario["mode"] == "cluster_ksm_empty_ns":
            if promql == "count(kube_namespace_status_phase)":
                return [{"metric": {}, "value": [1, "5"]}]
            return []
        return []

    monkeypatch.setattr(prom_module, "query_vector", fake_query_vector)
    ops = GatewayOpsService()

    res_zero = await ops._probe_gateway_details_via_prometheus(
        "c1", "lens-gateway-c1", gateway_name="lens-inference-gateway", provider="istio"
    )
    assert res_zero.gateway_state == "installed"
    assert res_zero.gateway_running is False
    assert res_zero.gateway_ready is False
    assert res_zero.ipp_state == "missing"
    assert res_zero.ipp_ready is False
    assert res_zero.gateway_node_address == "10.239.11.215"

    scenario["mode"] = "envoy_running"
    res_envoy = await ops._probe_gateway_details_via_prometheus(
        "c1", "lens-gateway-c1", gateway_name="lens-inference-gateway", provider="envoy-ai-gateway"
    )
    assert res_envoy.gateway_state == "installed"
    assert res_envoy.gateway_ready is True
    assert res_envoy.gateway_running is True
    assert res_envoy.ipp_state == "missing"
    assert res_envoy.gateway_node_address == "10.239.11.216"

    scenario["mode"] = "cluster_ksm_empty_ns"
    res_missing = await ops._probe_gateway_details_via_prometheus(
        "c1", "lens-gateway-c1", gateway_name="lens-inference-gateway", provider="istio"
    )
    assert res_missing.gateway_state == "missing"
    assert res_missing.gateway_ready is False
    assert res_missing.ipp_state == "missing"
    assert res_missing.ipp_ready is False


@pytest.mark.asyncio
async def test_non_admin_mutating_gates_403_fallback_and_stale_recovery(monkeypatch):
    cluster_id = "cluster-spr"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.239.11.215:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="sprocean-min",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_port=None,
        gateway_public_url=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    marker_store: dict[str, dict] = {}
    mutating_calls: list[list[str]] = []

    class _RestrictedRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            mutating_calls.append(list(argv))
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _RestrictedRunner())

    # 1. Stale historical 403 failed reconcile-gateway op + stale non-mutating ipp_config row auto-recover in status()
    ops._record(
        GatewayOperation(
            id="gop-23a73b805fc4",
            kind="reconcile-gateway",
            cluster_id=cluster_id,
            status="failed",
            message='gateways.gateway.networking.k8s.io is forbidden: User "lens" cannot create',
        )
    )
    ops._save_handoff_locked(
        cluster_id,
        {
            "reconcileStatus": "admin_handoff_required",
            "mode": "ipp_config",
            "desiredHash": "b" * 64,
            "markerNamespace": "lens-edit",
            "markerName": "lens-ms-handoff-" + "b" * 16,
            "provider": "istio",
            "ippConfigSha256": "",
            "modeSnapshots": {
                "ipp_config": {
                    "desiredHash": "b" * 64,
                    "markerName": "lens-ms-handoff-" + "b" * 16,
                    "ippConfigSha256": "",
                    "gatewayServiceType": None,
                    "provider": "istio",
                }
            },
        },
        message="remediation script generated (ipp_config)",
    )
    st0 = await ops.status()
    assert st0.clusters[0].reconcile_status == "synced"
    assert st0.clusters[0].pending_operations_count == 0
    assert st0.clusters[0].handoff_mode is None
    assert st0.clusters[0].last_operation is None

    # 2. All Non-Admin mutating paths are cleanly gated without executing forbidden kubectl/helm mutations
    for op in (
        await ops.install_inference_gateway(cluster_id, "istio"),
        await ops.install_ipp(cluster_id),
        await ops.uninstall_inference_gateway(cluster_id),
        await ops.uninstall_ipp(cluster_id),
        await ops.scale_gateway(cluster_id, 0),
        await ops.scale_component(cluster_id, "lens-gateway-cluster-spr", "lens-ipp", 0),
        await ops.apply_node_inotify_limit(cluster_id, 8192),
    ):
        assert op.status == "succeeded"
        assert "forbidden" not in op.message.lower()
    assert mutating_calls == []

    # 3. Non-mutating create_remediation previews do NOT pollute reconcileStatus or modeSnapshots
    repair_preview = await ops.create_remediation(cluster_id, mode="gateway_repair")
    assert repair_preview.reconcile_status == "admin_handoff_required"
    ipp_preview = await ops.create_remediation(cluster_id, mode="ipp_config")
    assert ipp_preview.reconcile_status == "admin_handoff_required"
    _, saved_after_previews = ops._load_handoff_locked(cluster_id)
    assert saved_after_previews["reconcileStatus"] == "synced"
    assert saved_after_previews.get("modeSnapshots", {}) == {}

    # 4. verify_remediation(mode="gateway_repair") on synced/empty DB keeps cluster synced & routes mode
    marker_store[repair_preview.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {"desiredHash": repair_preview.desired_hash, "status": "complete"},
    }
    res_repair = await ops.verify_remediation(
        cluster_id, mode="gateway_repair", expected_hash=repair_preview.desired_hash
    )
    assert res_repair.verified is True
    assert res_repair.reconcile_status == "synced"
    _, saved_after_verify = ops._load_handoff_locked(cluster_id)
    assert saved_after_verify["reconcileStatus"] == "synced"
    assert saved_after_verify["mode"] == "routes"

    # 5. Dynamic 403 fallback when probe_capabilities was inconclusive/optimistic (e.g. RBAC changed)
    admin_caps = gateway_ops_module.ClusterGatewayCapabilities(
        can_manage_gateway_infra=True,
        can_manage_gateway_config=True,
        can_reconcile_routes=True,
        access_mode="admin",
        can_read_gateway_namespace=True,
        can_port_forward_gateway=True,
        probe_conclusive=False,
    )
    monkeypatch.setattr(ops, "probe_capabilities", AsyncMock(return_value=admin_caps))
    dyn_gw = await ops.install_inference_gateway(cluster_id, "istio", install_prerequisites=False)
    assert dyn_gw.status == "succeeded"
    assert ops.handoff_from_operation(dyn_gw) is not None
    dyn_ipp = await ops.install_ipp(cluster_id)
    assert dyn_ipp.status == "succeeded"
    assert ops.handoff_from_operation(dyn_ipp) is not None
    dyn_rec = await ops.reconcile_cluster_gateway(cluster_id)
    assert dyn_rec.status == "succeeded"
    assert ops.handoff_from_operation(dyn_rec) is not None

    # 6. _heal_data_plane skips Non-Admin & inconclusive registered clusters
    heal_calls: list[str] = []
    monkeypatch.setattr(ops, "_heal_data_plane_locked", AsyncMock(side_effect=lambda cid: heal_calls.append(cid)))
    await ops._heal_data_plane(cluster_id)
    assert heal_calls == []


@pytest.mark.asyncio
async def test_custom_ipp_config_handoff_preserved_across_status_and_routes_handoff_mode(monkeypatch):
    cluster_id = "cluster-custom-ipp"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.21:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Custom IPP",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_port=None,
        gateway_public_url=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    class _NaIppRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            return _Result(returncode=1, stderr="Forbidden")

    ops = GatewayOpsService(runner_factory=lambda _cid: _NaIppRunner())
    op = await ops.set_ipp_config(cluster_id, "plugins:\n  - type: custom\n")
    assert op.status == "succeeded"

    st = await ops.status()
    assert st.clusters[0].reconcile_status == "admin_handoff_required"
    assert st.clusters[0].handoff_mode == "ipp_config"
    assert st.clusters[0].pending_operations_count == 1


@pytest.mark.asyncio
async def test_install_ipp_zero_set_flags_flag_injection_and_secret_redaction(monkeypatch):
    import yaml

    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda cid: SimpleNamespace(
            id=cid,
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_provider="istio",
        ),
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)

    recorded_helm_argv: list[list[str]] = []
    recorded_values: list[dict] = []

    class _HelmCaptureRunner:
        async def run(self, argv, **_kw):
            if argv[0] == "helm":
                recorded_helm_argv.append(list(argv))
                if "-f" in argv:
                    with open(argv[argv.index("-f") + 1], encoding="utf-8") as fh:
                        recorded_values.append(yaml.safe_load(fh))
            return _Result(stdout="ok")

    ops = GatewayOpsService(runner_factory=lambda _cid: _HelmCaptureRunner())
    ok_op = await ops.install_ipp("cluster-ipp-sec")
    assert ok_op.status == "succeeded"
    assert len(recorded_helm_argv) == 1
    assert "--set" not in recorded_helm_argv[0]
    assert "-f" in recorded_helm_argv[0]
    assert recorded_values[0] == {
        "payloadProcessor": {"name": "lens-ipp"},
        "provider": {"name": "istio"},
        "inferenceGateway": {"name": "lens-inference-gateway"},
    }

    # Leading-dash flag injection rejected before running helm/kubectl
    recorded_helm_argv.clear()
    for bad_op in (
        await ops.install_ipp("cluster-ipp-sec", name="--post-renderer=/bin/sh"),
        await ops.install_ipp("cluster-ipp-sec", chart="--kube-apiserver=https://evil.example"),
        await ops.uninstall_ipp("cluster-ipp-sec", name="--kubeconfig=/tmp/evil"),
        await ops.scale_component("cluster-ipp-sec", "ns", "--all", 0),
        await ops.scale_component("cluster-ipp-sec", "ns", "lens-ipp", 0, kind="--all"),
    ):
        assert bad_op.status == "failed"
    assert recorded_helm_argv == []

    # Secret redaction in GatewayOperation.message
    secret_op = ops._record(GatewayOperation(kind="install-ipp", cluster_id="cluster-ipp-sec"))
    finished = ops._finish(
        secret_op,
        ok=False,
        message='Unable to read config file "/tmp/kc.yaml": Bearer top-secret-tok token=xyz123',
    )
    assert "top-secret-tok" not in finished.message
    assert "xyz123" not in finished.message
    assert "/tmp/kc.yaml" not in finished.message

    # NAT64 & 6to4 loopback addresses blocked when block_loopback=True
    for loopback_url in ("http://[64:ff9b::7f00:1]/v1", "http://[2002:7f00:1::]/v1"):
        with pytest.raises(ValueError):
            await gateway_ops_module._ensure_safe_gateway_url(loopback_url, block_loopback=True)


@pytest.mark.asyncio
async def test_gateway_serving_state_falls_back_to_discovered_url_with_loopback_ssrf_guard(monkeypatch):
    captured_hooks: list[list] = []

    class _DiscoveredClient:
        def __init__(self, *args, **kwargs):
            captured_hooks.append(list((kwargs.get("event_hooks") or {}).get("request") or []))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda cid: SimpleNamespace(
            id=cid,
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_public_url=None,
            gateway_port=None,
            gateway_address=None,
        ),
    )
    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _DiscoveredClient)
    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)

    service = GatewayOpsService()
    member = SimpleNamespace(cluster_id="cluster-disc")
    group = SimpleNamespace(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model=None)

    # 1. Loopback discovered URL is blocked by block_loopback=True
    monkeypatch.setattr(service, "cluster_gateway_base_url", AsyncMock(return_value="http://127.0.0.1:32592/v1"))
    assert await service._gateway_serving_state(member, group) == "unreachable"
    assert captured_hooks == []

    # 2. Valid non-loopback discovered URL succeeds and uses _ssrf_discovered_gateway_request_hook
    monkeypatch.setattr(service, "cluster_gateway_base_url", AsyncMock(return_value="http://10.239.11.206:32592/v1"))
    assert await service._gateway_serving_state(member, group) == "ready"
    assert captured_hooks == [[gateway_ops_module._ssrf_discovered_gateway_request_hook]]


@pytest.mark.asyncio
async def test_non_admin_member_health_httproute_ipp_and_probe_members_caching(monkeypatch):
    cluster_id = "cluster-na-full"
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", cluster_id=cluster_id)
    )
    member_1 = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-na-1",
            cluster_id=cluster_id,
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns/qwen",
            endpoint_kind="llm-d-epp",
            status="active",
            weight=50,
        )
    )
    member_2 = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-na-2",
            cluster_id=cluster_id,
            target_namespace="ns",
            target_service="qwen-epp-2",
            target_port=8000,
            pool_name="ns/qwen-2",
            endpoint_kind="llm-d-epp",
            status="active",
            weight=50,
        )
    )
    cluster = SimpleNamespace(
        id=cluster_id,
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_provider="istio",
        gateway_public_url=None,
        gateway_port=None,
        gateway_address=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)

    service = GatewayOpsService(
        runner_factory=lambda _cid: _ComponentRunner([]),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        service,
        "probe_capabilities",
        AsyncMock(
            return_value=gateway_ops_module.ClusterGatewayCapabilities(
                can_manage_gateway_infra=False,
                can_manage_gateway_config=False,
                can_reconcile_routes=False,
                access_mode="non_admin",
                can_read_gateway_namespace=False,
                can_port_forward_gateway=False,
                permitted_member_namespaces=frozenset({"ns"}),
                probe_conclusive=True,
            )
        ),
    )

    gw_url_mock = AsyncMock(return_value="http://10.239.11.206:32592/v1")
    prom_mock = AsyncMock(
        return_value=gateway_ops_module._PrometheusGatewayProbeResult(
            gateway_state="installed",
            gateway_ready=True,
            gateway_running=True,
            ipp_state="installed",
            ipp_ready=True,
        )
    )
    monkeypatch.setattr(service, "cluster_gateway_base_url", gw_url_mock)
    monkeypatch.setattr(service, "_probe_gateway_details_via_prometheus", prom_mock)

    http_code_box = [401]

    class _StatusClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=http_code_box[0])

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _StatusClient)

    # (a) serving == ready + Prometheus ipp_ready=True -> httpRoute="ready", ipp="ready", serving="ready", ready=True
    service._save_handoff_locked(
        cluster_id,
        {"reconcileStatus": "synced", "syncedGroupIds": [group.id], "hasVerifiedSync": True},
        message="synced",
    )
    ready_a, detail_a = await service._member_health(member_1)
    assert ready_a is True
    assert detail_a["components"]["serving"] == "ready"
    assert detail_a["components"]["httpRoute"] == "ready"
    assert detail_a["components"]["ipp"] == "ready"

    # (f) probe_members caches discovered gateway URL & Prometheus probe per cluster across 2 members
    gw_url_mock.reset_mock()
    prom_mock.reset_mock()
    updated = await service.probe_members(cluster_id=cluster_id)
    assert updated == 2
    assert gw_url_mock.await_count == 1
    assert prom_mock.await_count == 1
    assert ModelServiceMemberDao().get(member_1.id).status == "active"
    assert ModelServiceMemberDao().get(member_2.id).status == "active"

    # (b) handoff_pending=True + serving="n/a" -> httpRoute="pending" without flipping member.status to unhealthy
    gw_url_mock.return_value = None
    service._save_handoff_locked(
        cluster_id,
        {"reconcileStatus": "admin_handoff_required", "syncedGroupIds": [], "hasVerifiedSync": False},
        message="pending",
    )
    ready_b, detail_b = await service._member_health(member_1)
    assert ready_b is True
    assert detail_b["components"]["serving"] == "n/a"
    assert detail_b["components"]["httpRoute"] == "pending"
    await service.probe_members(cluster_id=cluster_id)
    assert ModelServiceMemberDao().get(member_1.id).status == "active"

    # (b2) handoff_pending=True + reachable Gateway returning HTTP 404 -> serving="pending", httpRoute="pending",
    # keeps ready=True and member.status="active" before admin runs handoff script
    gw_url_mock.return_value = "http://10.239.11.206:32592/v1"
    http_code_box[0] = 404
    ready_b404, detail_b404 = await service._member_health(member_1)
    assert ready_b404 is True
    assert detail_b404["components"]["serving"] == "pending"
    assert detail_b404["components"]["httpRoute"] == "pending"
    await service.probe_members(cluster_id=cluster_id)
    assert ModelServiceMemberDao().get(member_1.id).status == "active"

    # (c) reconcileStatus="synced" + serving="n/a" -> httpRoute="ready"; 404 after sync -> httpRoute="missing"
    gw_url_mock.return_value = None
    service._save_handoff_locked(
        cluster_id,
        {"reconcileStatus": "synced", "syncedGroupIds": [group.id], "hasVerifiedSync": True},
        message="synced",
    )
    ready_c, detail_c = await service._member_health(member_1)
    assert ready_c is True
    assert detail_c["components"]["serving"] == "n/a"
    assert detail_c["components"]["httpRoute"] == "ready"

    gw_url_mock.return_value = "http://10.239.11.206:32592/v1"
    http_code_box[0] = 404
    ready_c404, detail_c404 = await service._member_health(member_1)
    assert ready_c404 is False
    assert detail_c404["components"]["serving"] == "pending"
    assert detail_c404["components"]["httpRoute"] == "missing"

    # (d) Prometheus ipp_state="installed", ipp_ready=False -> ipp="degraded", ready=False
    http_code_box[0] = 401
    prom_mock.return_value = gateway_ops_module._PrometheusGatewayProbeResult(
        gateway_state="installed",
        gateway_ready=True,
        gateway_running=True,
        ipp_state="installed",
        ipp_ready=False,
    )
    ready_d, detail_d = await service._member_health(member_1)
    assert ready_d is False
    assert detail_d["components"]["ipp"] == "degraded"

    # (e) Prometheus ipp_state="missing" -> ipp="missing", ready=False
    prom_mock.return_value = gateway_ops_module._PrometheusGatewayProbeResult(
        gateway_state="installed",
        gateway_ready=True,
        gateway_running=True,
        ipp_state="missing",
        ipp_ready=False,
    )
    ready_e, detail_e = await service._member_health(member_1)
    assert ready_e is False
    assert detail_e["components"]["ipp"] == "missing"


@pytest.mark.asyncio
async def test_verify_remediation_triggers_immediate_probe_members_only_when_verified(monkeypatch):
    cluster_id = "cluster-ver-probe"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.88:6443}}]\n"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Verify Probe Cluster",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
        gateway_address=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3", cluster_id=cluster_id)
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-ver-1",
            cluster_id=cluster_id,
            target_namespace="lens-edit",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="lens-edit/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
            status="unhealthy",
        )
    )

    marker_store: dict[str, dict] = {}

    class _VerifyProbeRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "get", "endpoints"]:
                return _Result(stdout=json.dumps({"subsets": [{"addresses": [{"ip": "10.1.0.2"}]}]}))
            if list(argv[:3]) == ["kubectl", "get", "inferencepool"]:
                return _Result(
                    stdout=json.dumps(
                        {
                            "spec": {"selector": {"matchLabels": {"app": "qwen"}}},
                            "status": {"parents": [{"conditions": [{"type": "Accepted", "status": "True"}]}]},
                        }
                    )
                )
            if list(argv[:3]) == ["kubectl", "get", "pods"]:
                return _Result(
                    stdout=json.dumps(
                        {
                            "items": [
                                {
                                    "status": {
                                        "phase": "Running",
                                        "conditions": [{"type": "Ready", "status": "True"}],
                                    }
                                }
                            ]
                        }
                    )
                )
            return _Result(returncode=1, stderr="Forbidden")

    class _HttpClient401:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return SimpleNamespace(status_code=200)

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HttpClient401)
    ops = GatewayOpsService(
        runner_factory=lambda _cid: _VerifyProbeRunner(),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        ops,
        "_probe_gateway_details_via_prometheus",
        AsyncMock(
            return_value=gateway_ops_module._PrometheusGatewayProbeResult(
                gateway_state="installed",
                gateway_ready=True,
                gateway_running=True,
                ipp_state="installed",
                ipp_ready=True,
            )
        ),
    )

    handoff = await ops.create_remediation(cluster_id, mode="routes")

    # 1. When verified=False, probe_members is NOT called
    probe_spy = AsyncMock(wraps=ops.probe_members)
    monkeypatch.setattr(ops, "probe_members", probe_spy)
    res_fail = await ops.verify_remediation(cluster_id)
    assert res_fail.verified is False
    probe_spy.assert_not_awaited()

    # 2. When verified=True, probe_members(cluster_id=cluster_id) runs immediately and updates DB healthJson
    marker_store[handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {
            "desiredHash": handoff.desired_hash,
            "status": "complete",
            "gatewayPort": "32592",
            "gatewayNodeAddress": "10.239.11.206",
        },
    }
    res_ok = await ops.verify_remediation(cluster_id)
    assert res_ok.verified is True
    probe_spy.assert_awaited_once_with(cluster_id=cluster_id)
    refreshed = ModelServiceMemberDao().get(member.id)
    assert refreshed.status == "active"
    assert refreshed.health_json["components"]["serving"] == "ready"
    assert refreshed.health_json["components"]["httpRoute"] == "ready"
    assert refreshed.health_json["components"]["ipp"] == "ready"

    # 3. Transient error in probe_members does not break verify_remediation
    monkeypatch.setattr(ops, "probe_members", AsyncMock(side_effect=RuntimeError("transient")))
    res_err = await ops.verify_remediation(cluster_id)
    assert res_err.verified is True


@pytest.mark.asyncio
async def test_cluster_gateway_base_url_blocks_loopback_for_non_admin_without_explicit_public_url(monkeypatch):
    from llm_d_lens.model_service.gateway_contracts import GatewayStatus, GatewayStatusCluster

    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.setattr(gateway_ops_module, "local_public_ip", lambda: "")

    svc = GatewayOpsService()
    status_box = [
        GatewayStatus(
            clusters=[
                GatewayStatusCluster(
                    cluster_id="cluster-na-lb",
                    access_mode="non_admin",
                    gateway_ready=True,
                    gateway_address="127.0.0.1",
                    gateway_public_url=None,
                    gateway_port=None,
                )
            ]
        )
    ]
    monkeypatch.setattr(svc, "status", AsyncMock(side_effect=lambda: status_box[0]))

    # Non-Admin loopback gateway_address with no global/public URL override is blocked
    assert await svc.cluster_gateway_base_url("cluster-na-lb") is None

    # Non-Admin valid external IP passes
    status_box[0] = GatewayStatus(
        clusters=[
            GatewayStatusCluster(
                cluster_id="cluster-na-lb",
                access_mode="non_admin",
                gateway_ready=True,
                gateway_address="10.239.11.206",
                gateway_public_url=None,
                gateway_port=None,
            )
        ]
    )
    assert await svc.cluster_gateway_base_url("cluster-na-lb") == "http://10.239.11.206/v1"


@pytest.mark.asyncio
async def test_public_url_with_none_gateway_port_uses_effective_port_across_all_call_sites(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_module

    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.setattr(prom_module, "resolve_prometheus_endpoint", AsyncMock(return_value=None))
    monkeypatch.setattr(gateway_ops_module, "local_public_ip", lambda: "")

    cluster_id = "sprocean-min-t40"
    kubeconfig = "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: https://10.0.0.88:6443}}]\n"
    cluster_box = [
        SimpleNamespace(
            id=cluster_id,
            name="sprocean-min",
            gateway_provider="istio",
            gateway_namespace="lens-gateway",
            gateway_name="lens-inference-gateway",
            gateway_public_url="10.112.228.229",
            gateway_port=None,
            gateway_address=None,
            hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
        )
    ]
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster_box[0])
    monkeypatch.setattr(gateway_ops_module, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster_box[0]])

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3", cluster_id=cluster_id)
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-t40-1",
            cluster_id=cluster_id,
            target_namespace="lens-edit",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="lens-edit/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
            status="active",
        )
    )

    marker_store: dict[str, dict] = {}

    class _Runner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "rollout", "status"]:
                return _Result(stdout="successfully rolled out")
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(
                    stdout=_ssar_list_response(payload["items"], allow_fn=lambda a: a.get("namespace") == "lens-edit")
                )
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(
                    stdout='{"spec":{"template":{"spec":{"containers":[{"name":"epp","args":["--pool-name","qwen","--secure-serving=false"]}]}}}}'
                )
            if list(argv[:3]) == ["kubectl", "get", "configmap"]:
                cm = marker_store.get(argv[3])
                return _Result(stdout=json.dumps(cm)) if cm else _Result(stdout="")
            if list(argv[:3]) == ["kubectl", "get", "endpoints"]:
                return _Result(stdout=json.dumps({"subsets": [{"addresses": [{"ip": "10.1.0.2"}]}]}))
            if list(argv[:3]) == ["kubectl", "get", "inferencepool"]:
                return _Result(
                    stdout=json.dumps(
                        {
                            "spec": {"selector": {"matchLabels": {"app": "qwen"}}},
                            "status": {"parents": [{"conditions": [{"type": "Accepted", "status": "True"}]}]},
                        }
                    )
                )
            if list(argv[:3]) == ["kubectl", "get", "pods"]:
                return _Result(
                    stdout=json.dumps(
                        {
                            "items": [
                                {
                                    "status": {
                                        "phase": "Running",
                                        "conditions": [{"type": "Ready", "status": "True"}],
                                    }
                                }
                            ]
                        }
                    )
                )
            return _Result(returncode=1, stderr="Forbidden")

    probed_urls: list[tuple[str, str]] = []

    class _HttpClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url):
            probed_urls.append(("GET", url))
            if ":32592/" in url:
                return SimpleNamespace(status_code=200)
            return SimpleNamespace(status_code=404)

        async def post(self, url, **_kwargs):
            probed_urls.append(("POST", url))
            if ":32592/" in url:
                return SimpleNamespace(status_code=401)
            return SimpleNamespace(status_code=404)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _HttpClient)
    ops = GatewayOpsService(
        runner_factory=lambda _cid: _Runner(),
        target_resolver=lambda _eid: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(
        ops,
        "_probe_gateway_details_via_prometheus",
        AsyncMock(
            return_value=gateway_ops_module._PrometheusGatewayProbeResult(
                gateway_state="installed",
                gateway_ready=True,
                gateway_running=True,
                ipp_state="installed",
                ipp_ready=True,
            )
        ),
    )

    handoff = await ops.create_remediation(cluster_id, mode="routes")
    marker_store[handoff.marker_name] = {
        "metadata": {"labels": {"lens.llm-d.ai/cluster": cluster_id}},
        "data": {
            "desiredHash": handoff.desired_hash,
            "status": "complete",
            "gatewayPort": "32592",
            "gatewayNodeAddress": "10.239.11.206",
        },
    }

    # 1. verify_remediation uses :32592 (not port 80) for both GET /models and POST /chat/completions
    probed_urls.clear()
    res = await ops.verify_remediation(cluster_id)
    assert res.verified is True
    assert ("GET", "http://10.112.228.229:32592/v1/models") in probed_urls
    assert ("POST", "http://10.112.228.229:32592/v1/chat/completions") in probed_urls
    assert all(":32592/" in url for _, url in probed_urls)

    # 2. status() reports gatewayPort=32592, gatewayPublicUrl="10.112.228.229", and probes :32592
    probed_urls.clear()
    st = await ops.status()
    assert len(st.clusters) == 1
    assert st.clusters[0].gateway_port == 32592
    assert st.clusters[0].gateway_public_url == "10.112.228.229"
    assert st.clusters[0].gateway_ready is True
    assert probed_urls == [("GET", "http://10.112.228.229:32592/v1/models")]

    # 3. cluster_gateway_base_url() returns http://10.112.228.229:32592/v1
    assert await ops.cluster_gateway_base_url(cluster_id) == "http://10.112.228.229:32592/v1"

    # 4. _gateway_serving_state() probes http://10.112.228.229:32592/v1/chat/completions (not port 80!)
    probed_urls.clear()
    assert await ops._gateway_serving_state(member, group) == "ready"
    assert ("POST", "http://10.112.228.229:32592/v1/chat/completions") in probed_urls

    # 5. SSRF trust split: explicit loopback gateway_public_url + discovered port 32592 is allowed (trusted host)
    cluster_box[0] = SimpleNamespace(
        id=cluster_id,
        name="sprocean-min",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="127.0.0.1",
        gateway_port=None,
        gateway_address=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    probed_urls.clear()
    st_loop = await ops.status()
    assert st_loop.clusters[0].gateway_port == 32592
    assert st_loop.clusters[0].gateway_ready is True
    assert ("GET", "http://127.0.0.1:32592/v1/models") in probed_urls
    assert await ops.cluster_gateway_base_url(cluster_id) == "http://127.0.0.1:32592/v1"
    assert await ops._gateway_serving_state(member, group) == "ready"

    # 6. SSRF trust split: untrusted loopback host from discoveredGateway (no gateway_public_url) stays blocked
    cluster_box[0] = SimpleNamespace(
        id=cluster_id,
        name="sprocean-min",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
        gateway_address=None,
        hf_token_secret_namespace="lens-edit",  # noqa: S106 - namespace fixture
    )
    monkeypatch.setattr(gateway_ops_module, "read_kubeconfig", lambda _cid: None)
    ops._save_handoff_locked(
        cluster_id,
        {
            "reconcileStatus": "synced",
            "hasVerifiedSync": True,
            "discoveredGateway": {"gatewayPort": 32592, "gatewayNodeAddress": "127.0.0.1"},
        },
        message="loopback discovered",
    )
    probed_urls.clear()
    st_blocked = await ops.status()
    assert st_blocked.clusters[0].gateway_ready is None
    assert probed_urls == []
    assert await ops.cluster_gateway_base_url(cluster_id) is None


@pytest.mark.asyncio
async def test_admin_status_and_serving_state_resolve_live_node_port_when_gateway_port_is_none(monkeypatch):
    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.setattr(gateway_ops_module, "local_public_ip", lambda: "")

    cluster_id = "admin-nodeport-t40"
    cluster = SimpleNamespace(
        id=cluster_id,
        name="Admin NodePort",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="10.112.228.229",
        gateway_port=None,
        gateway_address=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: cluster)
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [cluster])

    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3", cluster_id=cluster_id)
    )
    member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-admin-t40",
            cluster_id=cluster_id,
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
            status="active",
        )
    )

    class _AdminRunner:
        async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
            if list(argv[:3]) == ["kubectl", "create", "-f"]:
                payload = json.loads(input or "{}")
                return _Result(stdout=_ssar_list_response(payload["items"], allow_fn=lambda _a: True))
            if list(argv[:3]) == ["kubectl", "get", "gateway"]:
                if any("status.addresses" in str(a) for a in argv):
                    return _Result(stdout="")
                return _Result(stdout="True")
            if list(argv[:3]) == ["kubectl", "get", "svc"]:
                if "-o" in argv and "name" in argv:
                    return _Result(stdout="service/lens-inference-gateway-istio")
                return _Result(
                    stdout=json.dumps({"spec": {"type": "NodePort", "ports": [{"port": 80, "nodePort": 32592}]}})
                )
            if list(argv[:3]) == ["kubectl", "get", "nodes"]:
                return _Result(stdout="10.239.11.206")
            if list(argv[:3]) == ["kubectl", "get", "deploy"]:
                return _Result(stdout='{"spec":{"replicas":1},"status":{"readyReplicas":1,"availableReplicas":1}}')
            return _Result(stdout="")

    posted: list[str] = []

    class _AdminHttpClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            posted.append(url)
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _AdminHttpClient)
    ops = GatewayOpsService(runner_factory=lambda _cid: _AdminRunner())

    st = await ops.status()
    assert len(st.clusters) == 1
    assert st.clusters[0].gateway_port == 32592
    assert st.clusters[0].gateway_public_url == "10.112.228.229"
    assert await ops.cluster_gateway_base_url(cluster_id) == "http://10.112.228.229:32592/v1"
    assert await ops._gateway_serving_state(member, group) == "ready"
    assert posted == ["http://10.112.228.229:32592/v1/chat/completions"]


@pytest.mark.asyncio
async def test_t40_hardening_health_only_probe_scoped_status_unicode_ports_and_malformed_ipv6(monkeypatch):
    from llm_d_lens.model_service.targets import ExecutionMissingError

    # 1. Unicode non-ASCII digits are rejected without ValueError in port parsing
    for bad_port in ("²", "①", "٣", True, 0, 70000):
        assert gateway_ops_module._valid_gateway_port(bad_port) is None
        assert GatewayOpsService._parse_discovered_gateway({"gatewayPort": bad_port}) is None

    # 2. Malformed IPv6 brackets return None/False cleanly without raising ValueError
    for bad_ipv6 in ("[2001:db8::1", "2001:db8::1]", "[[2001:db8::1]]", "http://[2001:db8::1"):
        assert gateway_ops_module._gateway_public_url_needs_port(bad_ipv6) is False
        assert gateway_ops_module._normalize_gateway_url(bad_ipv6, default_port=32592) is None

    # 3. probe_members(allow_mutations=False) skips member deletion, _heal_data_plane, and reconcile_cluster
    cluster_id = "c-health-only"
    group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3", cluster_id=cluster_id)
    )
    missing_exec_member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-missing",
            cluster_id=cluster_id,
            target_namespace="ns",
            target_service="qwen-epp",
            target_port=8000,
            pool_name="ns/qwen",
            epp_ref="qwen-epp",
            endpoint_kind="llm-d-epp",
            status="active",
        )
    )
    unhealthy_member = ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=group.id,
            execution_id="exec-live",
            cluster_id=cluster_id,
            target_namespace="ns",
            target_service="qwen-epp-2",
            target_port=8000,
            pool_name="ns/qwen-2",
            epp_ref="qwen-epp-2",
            endpoint_kind="llm-d-epp",
            status="active",
        )
    )

    def _resolver(exec_id: str):
        if exec_id == "exec-missing":
            raise ExecutionMissingError("gone")
        return SimpleNamespace(status="ready")

    ops = GatewayOpsService(target_resolver=_resolver)
    heal_spy = AsyncMock()
    reconcile_spy = AsyncMock()
    monkeypatch.setattr(ops, "_heal_data_plane", heal_spy)
    monkeypatch.setattr(ops, "reconcile_cluster", reconcile_spy)
    monkeypatch.setattr(
        ops,
        "_member_health",
        AsyncMock(return_value=(False, {"ready": False, "components": {"serving": "unreachable"}})),
    )

    updated = await ops.probe_members(cluster_id=cluster_id, allow_mutations=False)
    assert updated == 1
    assert ModelServiceMemberDao().get(missing_exec_member.id) is not None
    assert ModelServiceMemberDao().get(unhealthy_member.id).status == "unhealthy"
    heal_spy.assert_not_awaited()
    reconcile_spy.assert_not_awaited()

    # 4. cluster_gateway_base_url(cluster_id) scopes status() to cluster_id (ignores unrelated slow/broken clusters)
    c_target = SimpleNamespace(
        id="c-target",
        name="Target",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="10.112.228.229",
        gateway_port=32592,
    )
    c_other = SimpleNamespace(
        id="c-other",
        name="Other",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url=None,
        gateway_port=None,
    )
    monkeypatch.setattr("llm_d_lens.cluster.registry.list_clusters", lambda: [c_target, c_other])
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda cid: c_target if cid == "c-target" else c_other)

    probed_cids: list[str] = []

    async def fake_caps(cid: str, **_kwargs):
        probed_cids.append(cid)
        if cid == "c-other":
            raise RuntimeError("unrelated cluster unreachable")
        return gateway_ops_module.ClusterGatewayCapabilities(
            can_manage_gateway_infra=True,
            can_manage_gateway_config=True,
            can_reconcile_routes=True,
            access_mode="admin",
            can_read_gateway_namespace=True,
            can_port_forward_gateway=True,
            permitted_member_namespaces=frozenset({"ns"}),
            probe_conclusive=True,
        )

    monkeypatch.setattr(ops, "probe_capabilities", fake_caps)
    monkeypatch.setattr(ops, "_gateway_state", AsyncMock(return_value=("installed", True)))
    monkeypatch.setattr(ops, "_gateway_address", AsyncMock(return_value=None))
    monkeypatch.setattr(ops, "_node_address", AsyncMock(return_value="10.239.11.206"))
    monkeypatch.setattr(ops, "_deployment_state", AsyncMock(return_value=("installed", True)))
    monkeypatch.setattr(ops, "_gateway_deployment", AsyncMock(return_value=("lens-gateway", "gw-istio")))

    assert await ops.cluster_gateway_base_url("c-target") == "http://10.112.228.229:32592/v1"
    assert probed_cids == ["c-target"]

    # 5. _gateway_serving_state catches cluster_gateway_base_url error and resolves Admin _gateway_node_port
    c_admin_unready = SimpleNamespace(
        id="c-admin-unready",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-inference-gateway",
        gateway_public_url="10.112.228.229",
        gateway_port=None,
        gateway_address=None,
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _cid: c_admin_unready)
    monkeypatch.setattr(ops, "cluster_gateway_base_url", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(ops, "_gateway_node_port", AsyncMock(return_value=32592))
    posted_fallback: list[str] = []

    class _FallbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            posted_fallback.append(url)
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(gateway_ops_module.httpx, "AsyncClient", _FallbackClient)
    state = await ops._gateway_serving_state(
        SimpleNamespace(cluster_id="c-admin-unready"),
        SimpleNamespace(name="qwen3", model_ref="Qwen/Qwen3-8B", base_model="qwen3"),
    )
    assert state == "ready"
    assert posted_fallback == ["http://10.112.228.229:32592/v1/chat/completions"]


@pytest.mark.asyncio
async def test_reconcile_transport_failure_does_not_submit_model_routes(monkeypatch):
    _seed_member("cluster-a")
    calls = []
    service = _service(calls=calls)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )
    monkeypatch.setattr(
        service, "_ensure_epp_transport", AsyncMock(side_effect=gateway_ops_module.GatewayOpsError("rollout failed"))
    )
    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "failed"
    assert "rollout failed" in operation.message
    applied = [
        doc
        for argv, content in calls
        if argv[:3] == ["kubectl", "apply", "-f"] and content
        for doc in yaml.safe_load_all(content)
        if doc
    ]
    assert not any(doc["kind"] == "HTTPRoute" for doc in applied)


@pytest.mark.asyncio
async def test_reconcile_isolates_unrepaired_router_from_healthy_models(monkeypatch):
    healthy = _seed_member("cluster-a")
    broken_group = ModelServiceGroupDao().create(
        ModelServiceGroup(name="llama", model_ref="meta/llama", cluster_id="cluster-a", base_model="meta/llama")
    )
    ModelServiceMemberDao().create(
        ModelServiceMember(
            group_id=broken_group.id,
            execution_id="exec-broken",
            cluster_id="cluster-a",
            target_namespace="ns2",
            target_service="llama-epp",
            target_port=8000,
            pool_name="llama",
            endpoint_kind="llm-d-epp",
        )
    )
    broken_route = gateway_ops_module.resource_name(f"llama-{broken_group.id}")

    class _Runner(_FakeRunner):
        async def run(self, argv, **kwargs):
            if list(argv[:3]) == ["kubectl", "get", "httproute"]:
                self.calls.append((argv, kwargs.get("input")))
                return _Result(stdout=broken_route)
            return await super().run(argv, **kwargs)

    calls = []
    service = _service(calls=calls, runner_cls=_Runner)
    monkeypatch.setattr(
        gateway_ops_module,
        "get_cluster",
        lambda _: SimpleNamespace(
            gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
        ),
    )

    async def ensure(_cluster_id, namespace, name, **_kwargs):
        if namespace == "ns2":
            raise gateway_ops_module.GatewayOpsError("rollout failed")
        return True

    monkeypatch.setattr(service, "_ensure_epp_transport", ensure)
    operation = await service.reconcile_cluster_gateway("cluster-a")
    assert operation.status == "failed"
    assert "ns2/llama-epp: rollout failed" in operation.message
    routes = [
        doc["metadata"]["name"]
        for argv, content in calls
        if argv[:3] == ["kubectl", "apply", "-f"] and content
        for doc in yaml.safe_load_all(content)
        if doc and doc["kind"] == "HTTPRoute"
    ]
    assert routes == [gateway_ops_module.resource_name(f"qwen3-8b-cluster-a-{healthy.group_id}")]
    assert not any(argv[:3] == ["kubectl", "delete", "httproute"] for argv, _ in calls)


@pytest.mark.asyncio
async def test_retry_waits_for_rollout_even_when_transport_already_matches(monkeypatch):
    runner = _TlsEppRunner([], secure=False, proxy_secure=False)
    service = GatewayOpsService(runner_factory=lambda _: runner)
    run = runner.run

    async def failed_rollout(argv, **kwargs):
        if list(argv[:3]) == ["kubectl", "rollout", "status"]:
            return _Result(returncode=1, stderr="rollout still pending")
        return await run(argv, **kwargs)

    monkeypatch.setattr(runner, "run", failed_rollout)
    with pytest.raises(gateway_ops_module.GatewayOpsError, match="rollout still pending"):
        await service._ensure_epp_transport("cluster-a", "ns", "qwen-epp", secure_serving=False)
    assert not runner.patches


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_secure", [False, True])
async def test_separate_proxy_is_validated_before_switching_epp(monkeypatch, proxy_secure):
    from copy import deepcopy

    runner = _TlsEppRunner([], proxy_secure=proxy_secure)
    separate = deepcopy(runner.workload)
    separate["metadata"]["name"] = "qwen-proxy"
    separate["spec"]["template"]["spec"]["containers"].pop()
    runner.workload["spec"]["template"]["spec"]["containers"].pop(0)
    runner.workload["spec"]["template"]["spec"]["volumes"] = []
    service = GatewayOpsService(runner_factory=lambda _: runner)
    run = runner.run

    async def with_proxy(argv, **kwargs):
        if list(argv[:4]) == ["kubectl", "get", "deploy", "qwen-proxy"]:
            return _Result(stdout=json.dumps(separate))
        return await run(argv, **kwargs)

    monkeypatch.setattr(runner, "run", with_proxy)
    if proxy_secure:
        with pytest.raises(gateway_ops_module.GatewayOpsError, match="Separate standalone proxy"):
            await service._ensure_epp_transport("cluster-a", "ns", "qwen-epp", secure_serving=False)
        assert not runner.patches and not runner.prepared
    else:
        assert await service._ensure_epp_transport("cluster-a", "ns", "qwen-epp", secure_serving=False)
        assert len(runner.patches) == 1
        assert not runner.prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reconcile", "create", "verify"])
async def test_non_admin_transport_failure_blocks_publication_and_verification(monkeypatch, action):
    _seed_member("cluster-a")
    calls = []
    service = _service(calls=calls)
    cluster = SimpleNamespace(
        gateway_provider="istio", gateway_namespace="lens-gateway", gateway_name="lens-inference-gateway"
    )
    monkeypatch.setattr(gateway_ops_module, "get_cluster", lambda _: cluster)
    caps = gateway_ops_module.ClusterGatewayCapabilities(
        can_reconcile_routes=False,
        access_mode="non_admin",
        permitted_member_namespaces=frozenset({"ns"}),
        probe_conclusive=True,
    )
    monkeypatch.setattr(service, "probe_capabilities", AsyncMock(return_value=caps))
    monkeypatch.setattr(
        service, "_ensure_epp_transport", AsyncMock(side_effect=gateway_ops_module.GatewayOpsError("rollout failed"))
    )
    if action == "reconcile":
        operation = await service.reconcile_cluster_gateway("cluster-a")
        assert operation.status == "failed"
        assert "rollout failed" in operation.message
        assert service.handoff_from_operation(operation) is None
    else:
        method = service.create_remediation if action == "create" else service.verify_remediation
        with pytest.raises(gateway_ops_module.GatewayOpsError, match="rollout failed"):
            await method("cluster-a", marker_namespace="ns")
    assert not any(list(argv[:3]) == ["kubectl", "apply", "-f"] for argv, _ in calls)
    assert service.operations.latest_for_kind("route-handoff", cluster_id="cluster-a") is None


@pytest.mark.parametrize("proxy", ["sidecar", "separate", "none"])
@pytest.mark.parametrize("secure", [True, False])
def test_handoff_transport_preserves_independent_proxy(tmp_path, proxy, secure):
    import os
    import subprocess
    import sys

    script = GatewayOpsService._build_handoff_script(
        cluster_id="cluster-a",
        gw_ns="lens-gateway",
        mode="routes",
        desired_hash="a" * 64,
        marker_namespace="ns",
        marker_name="lens-ms-handoff-" + "a" * 16,
        apply_docs=[{"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": {"name": "model"}}],
        delete_items=[],
        epp_targets=[("ns", "qwen-epp")],
        ipp_release=None,
    )
    assert script.index("patch_epp_transport ns qwen-epp") < script.index("kubectl apply -f - <<")
    start = script.index("EPP_SECURE_SERVING=")
    end = script.index("\n}\n", start) + 3
    calls_path = tmp_path / "calls.jsonl"
    mock = tmp_path / "kubectl"
    mock.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['CALLS_PATH']).open('a') as log: log.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['get', 'deploy']:\n"
        "    if args[2].endswith('-proxy'):\n"
        "        if os.environ['PROXY'] == 'separate': print('deployment.apps/qwen-proxy')\n"
        "    elif '-o' in args:\n"
        "        output = args[args.index('-o') + 1]\n"
        "        flags = '--pool-name=qwen --secure-serving=' + os.environ['SECURE']\n"
        "        if '.image' in output:\n"
        "            print('epp example/epp')\n"
        "            if os.environ['PROXY'] == 'sidecar': print('proxy envoyproxy/envoy')\n"
        "        elif '.name' in output: print('epp\\t' + flags)\n"
        "        else: print(flags)\n"
    )
    mock.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script[start:end] + "\npatch_epp_transport ns qwen-epp\n"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "CALLS_PATH": str(calls_path),
            "PROXY": proxy,
            "SECURE": str(secure).lower(),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    patches = [argv for argv in calls if argv[:2] == ["patch", "deploy"]]
    if secure and proxy != "none":
        assert result.returncode != 0
        assert "retains an independent proxy" in result.stderr
        assert patches == []
    else:
        assert result.returncode == 0, result.stderr
        assert len(patches) == int(secure)
        if patches:
            patch = json.loads(patches[0][patches[0].index("-p") + 1])
            assert patch[0]["value"] == "--secure-serving=false"
        assert any(argv[:2] == ["rollout", "status"] for argv in calls)
