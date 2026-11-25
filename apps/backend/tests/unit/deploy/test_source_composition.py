import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from llm_d_lens.deploy import source_catalog as catalog_module
from llm_d_lens.deploy import source_composition as composition
from llm_d_lens.deploy.guide_planning import GuideError, source_response
from llm_d_lens.infrastructure.process import shell


def workload(args=None, *, name="engine", role="serving", replicas=1):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "cache"},
        "spec": {
            "replicas": replicas,
            "template": {
                "metadata": {"labels": {"llm-d.ai/role": role}},
                "spec": {
                    "containers": [
                        {
                            "name": "modelserver",
                            "image": "engine:pinned",
                            "command": ["vllm", "serve"],
                            "args": ["original/model", *(args or [])],
                        }
                    ]
                },
            },
        },
    }


BINDING = {"model": "example/model", "revision": "pinned", "pvcName": "weights", "namespace": "cache"}


def test_single_node_disaggregated_roles_preserve_controller_and_use_requested_sizing():
    roles = []
    for name in ("prefill", "decode"):
        template = workload(role=name)["spec"]["template"]
        template["spec"]["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": 8}}
        roles.append(
            {
                "name": name,
                "spec": {
                    "replicas": 1,
                    "leaderWorkerTemplate": {
                        "size": 1,
                        "workerTemplate": template,
                    },
                },
            }
        )
    parsed = [{"kind": "DisaggregatedSet", "metadata": {"name": "pd"}, "spec": {"slices": 1, "roles": roles}}]
    runtime = {"profileId": "nvidia", "tp": 2, "replicas": 3, "prefillTp": 1, "prefillReplicas": 2}
    composition.apply_runtime(parsed, runtime, "example/model")
    assert parsed[0]["kind"] == "DisaggregatedSet"
    assert [role["spec"]["replicas"] for role in roles] == [2, 3]
    assert [
        role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]["containers"][0]["resources"]["limits"][
            "nvidia.com/gpu"
        ]
        for role in roles
    ] == [1, 2]
    roles[0]["spec"]["leaderWorkerTemplate"]["size"] = 2
    with pytest.raises(GuideError, match="single-node"):
        composition.apply_runtime(parsed, runtime, "example/model")


def test_shared_definition_and_scalar_validation():
    assert len(catalog_module.DEFINITION["items"]) == 40
    original = [{"value": 1, "nested": {"text": "old"}, "flag": True}]
    assert (
        composition.scalar_edits(original, [{"path": [0, "nested", "text"], "value": "new"}])[0]["nested"]["text"]
        == "new"
    )
    assert original[0]["nested"]["text"] == "old"
    for edit in [
        {"path": [-1, "value"], "value": 2},
        {"path": [0, "missing"], "value": 2},
        {"path": [0, "flag"], "value": 1},
        {"path": [0, "__proto__"], "value": 1},
    ]:
        with pytest.raises(GuideError):
            composition.scalar_edits(original, [edit])


def dra_workloads():
    parsed = []
    for role in ("prefill", "decode"):
        document = workload(name=role, role=role)
        pod = document["spec"]["template"]["spec"]
        pod["resourceClaims"] = [{"name": "devices", "resourceClaimTemplateName": f"{role}-devices"}]
        pod["containers"][0]["resources"] = {"claims": [{"name": "devices"}]}
        parsed.extend(
            [
                document,
                {
                    "apiVersion": "resource.k8s.io/v1",
                    "kind": "ResourceClaimTemplate",
                    "metadata": {"name": f"{role}-devices", "namespace": "cache"},
                    "spec": {
                        "spec": {
                            "devices": {
                                "requests": [
                                    {
                                        "name": "accelerator",
                                        "exactly": {"deviceClassName": "gpu.intel.com", "count": 1},
                                    },
                                    {"name": "nic", "exactly": {"deviceClassName": "dranet-rdma", "count": 2}},
                                ]
                            }
                        }
                    },
                },
            ]
        )
    return parsed


def test_pd_dra_sizing_preserves_source_transport_roles():
    parsed = dra_workloads()
    transports = []
    for document, role in zip((parsed[0], parsed[2]), ("kv_producer", "kv_consumer"), strict=True):
        connector = {"kv_connector": "NixlConnector", "kv_role": role, "engine_id": document["metadata"]["name"]}
        document["spec"]["template"]["spec"]["containers"][0]["args"].extend(
            ["--kv-transfer-config", json.dumps(connector)]
        )
        transports.append(connector)
    composition.apply_runtime(
        parsed,
        {"profileId": "intel-xpu", "tp": 2, "replicas": 1, "prefillTp": 1, "prefillReplicas": 1},
        "test/model",
    )
    for document, original in zip((parsed[0], parsed[2]), transports, strict=True):
        pod = document["spec"]["template"]["spec"]
        args = pod["containers"][0]["args"]
        assert json.loads(args[args.index("--kv-transfer-config") + 1]) == original
        assert not pod.get("initContainers")
    assert composition.accelerator_demand(parsed)[0] == {"dra:intel-xpu": 3}


def test_dra_runtime_sizes_each_role_and_counts_replicas_and_autoscaling():
    parsed = dra_workloads()
    composition.apply_runtime(
        parsed,
        {
            "profileId": "intel-xpu",
            "tp": 2,
            "replicas": 2,
            "prefillTp": 1,
            "prefillReplicas": 1,
        },
        "test/model",
    )
    assert parsed[1]["spec"]["spec"]["devices"]["requests"][0]["exactly"]["count"] == 1
    assert parsed[3]["spec"]["spec"]["devices"]["requests"][0]["exactly"]["count"] == 2
    assert parsed[3]["spec"]["spec"]["devices"]["requests"][1]["exactly"]["count"] == 2
    assert composition.accelerator_demand(parsed)[0] == {"dra:intel-xpu": 5}
    parsed.append(
        {
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": "scale-decode", "namespace": "cache"},
            "spec": {
                "scaleTargetRef": {"kind": "Deployment", "name": "decode"},
                "maxReplicas": 3,
            },
        }
    )
    assert composition.accelerator_demand(parsed)[0] == {"dra:intel-xpu": 7}
    assert {"kind": "DeviceClass", "name": "gpu.intel.com", "namespace": None} in composition.resource_references(
        parsed
    )


@pytest.mark.parametrize("change", ["external", "missing", "all", "alternatives", "zero", "shared", "unconsumed"])
def test_dra_ambiguous_or_external_allocation_stays_blocked(change):
    parsed = dra_workloads()
    reference = parsed[2]["spec"]["template"]["spec"]["resourceClaims"][0]
    request = parsed[3]["spec"]["spec"]["devices"]["requests"][0]
    if change == "external":
        reference["resourceClaimName"] = reference.pop("resourceClaimTemplateName")
    elif change == "missing":
        reference["resourceClaimTemplateName"] = "absent"
    elif change == "all":
        request["exactly"]["allocationMode"] = "All"
    elif change == "alternatives":
        request["firstAvailable"] = [request.pop("exactly")]
    elif change == "zero":
        request["exactly"]["count"] = 0
    elif change == "unconsumed":
        parsed[2]["spec"]["template"]["spec"]["containers"][0]["resources"]["claims"] = []
    else:
        reference["resourceClaimTemplateName"] = "prefill-devices"
    with pytest.raises(GuideError, match="DRA"):
        composition.apply_runtime(
            parsed,
            {
                "profileId": "intel-xpu",
                "tp": 2,
                "replicas": 1,
                "prefillTp": 1,
                "prefillReplicas": 1,
            },
            "test/model",
        )


def test_dra_budget_and_live_usable_inventory_are_both_enforced(monkeypatch):
    parsed = dra_workloads()
    body = {"clusterId": "fixture", "clusterSessionId": "session", "deploymentIntent": {"maxDevices": 2}}
    hardware = {"usableGpuCount": 2, "accelerators": [{"id": "intel-xpu", "gpuCount": 4}]}
    commands = []

    async def session(*_):
        return SimpleNamespace(server_id="fixture")

    async def overview(_):
        return {"hardware": hardware}

    async def kubectl(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(ok=True, stdout='{"items":[]}')

    monkeypatch.setattr(composition, "validate_planning_session", session)
    monkeypatch.setattr(composition, "run_kubectl", kubectl)
    monkeypatch.setattr("llm_d_lens.cluster.registry.require_cluster", lambda _: SimpleNamespace(id="fixture"))
    monkeypatch.setattr("llm_d_lens.cluster.service.build_overview", overview)
    result = asyncio.run(composition.check_target(None, body, parsed))
    assert result["demand"] == {"dra:intel-xpu": 2}
    assert result["available"]["dra:intel-xpu"] == 2
    assert ["get", "DeviceClass", "gpu.intel.com", "-o", "name"] in commands
    hardware["usableGpuCount"] = 1
    with pytest.raises(GuideError, match="only 1 currently available"):
        asyncio.run(composition.check_target(None, body, parsed))
    body["deploymentIntent"]["maxDevices"] = 1
    with pytest.raises(GuideError, match="above the budget"):
        asyncio.run(composition.check_target(None, body, parsed))
    body["deploymentIntent"]["maxDevices"] = 2
    hardware["accelerators"].append({"id": "different-profile", "gpuCount": 1})
    with pytest.raises(GuideError, match="unambiguous"):
        asyncio.run(composition.check_target(None, body, parsed))


def test_selected_node_capacity_is_checked_instead_of_cluster_total(monkeypatch):
    parsed = [workload(replicas=2)]
    pod = parsed[0]["spec"]["template"]["spec"]
    pod["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": 1}}
    parsed = composition.apply_reviewed_placement(
        parsed, {"podPlacement": "same-node", "nodeLabel": {"key": "pool", "value": "selected"}}
    )
    nodes = [
        {
            "metadata": {"name": name, "labels": {"pool": name}},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "allocatable": {"nvidia.com/gpu": str(count), "pods": "110"},
            },
        }
        for name, count in (("selected", 1), ("other", 8))
    ]

    async def session(*_):
        return SimpleNamespace(server_id="fixture")

    async def kubectl(args, **kwargs):
        return SimpleNamespace(ok=True, stdout=json.dumps({"items": nodes if args[1] == "nodes" else []}))

    monkeypatch.setattr(composition, "validate_planning_session", session)
    monkeypatch.setattr(composition, "run_kubectl", kubectl)
    body = {"clusterId": "fixture", "clusterSessionId": "session"}
    with pytest.raises(GuideError, match="Placement validation failed"):
        asyncio.run(composition.check_target(None, body, parsed))
    nodes[0]["status"]["allocatable"]["nvidia.com/gpu"] = "2"
    assert asyncio.run(composition.check_target(None, body, parsed))["demand"] == {"nvidia.com/gpu": 2}


def test_catalog_keeps_all_definitions_and_new_source_guides_visible():
    catalog = catalog_module.optimization_catalog(
        [{"id": "models/new-model", "title": "New model recipe", "support": {}}], "a" * 40
    )
    items = {item["id"]: item for item in catalog["items"]}
    assert {row[0] for row in catalog_module.DEFINITION["items"]} <= items.keys()
    assert items["guide:models/new-model"]["stage"] == "decode"
    assert items["guide:models/new-model"]["present"] is True
    assert items["affinity"]["guide"] != items["index"]["guide"]
    assert items["shared-cache"]["generation"] == "requires-adapter"
    assert items["transfer"]["present"] is False


@pytest.mark.parametrize(
    "flag",
    [
        "--tensor-parallel-size=-2",
        "--max-num-seqs=0",
        "--gpu-memory-utilization=-2",
        "--gpu-memory-utilization=nan",
        "--max-model-len=1.5",
    ],
)
def test_invalid_engine_parameters_are_rejected(flag):
    with pytest.raises(GuideError):
        composition.validate_engine_parameters([workload([flag])])


def test_model_binding_is_idempotent_and_preserves_user_resources():
    parsed = [workload(["--tensor-parallel-size=2"])]
    parsed.append(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "examples.test"},
            "spec": {"properties": {"name": {"type": "string"}}},
        }
    )
    composition.bind_model(parsed, BINDING)
    once = deepcopy(parsed)
    composition.bind_model(parsed, BINDING)
    assert parsed == once
    pod = parsed[0]["spec"]["template"]["spec"]
    assert len(pod["volumes"]) == 1
    assert pod["containers"][0]["args"][0] == "example/model"
    assert "--revision=pinned" in pod["containers"][0]["args"]
    pod["containers"][0]["volumeMounts"][0]["readOnly"] = False
    with pytest.raises(GuideError, match="Conflicting"):
        composition.bind_model(parsed, BINDING)


def test_effective_facts_are_role_scoped_and_not_runtime_success():
    parsed = [
        workload(["--enable-prefix-caching"], name="prefill", role="prefill"),
        workload(["--no-enable-prefix-caching"], name="decode", role="decode"),
    ]
    facts = composition.rendered_optimizations(parsed, "a" * 40)
    prefix = next(item for item in facts["features"] if item["id"] == "prefix")
    assert prefix["state"] == "mixed"
    assert len(prefix["paths"]) == 2
    assert any(item["id"] == "transfer" for item in facts["features"])
    assert facts["checksum"] == composition.checksum(composition.dump(parsed))
    assert all(item["evidence"] == "rendered-manifest" for item in facts["features"])


def test_full_gpu_utilization_is_a_valid_upper_boundary():
    composition.validate_engine_parameters([workload(["--gpu-memory-utilization=1"])])


def test_resource_conflicts_and_multi_role_budget(monkeypatch):
    class Profile:
        id = "fixture"

    monkeypatch.setattr(
        composition, "resolve_by_resource", lambda name: Profile() if name == "example.org/device" else None
    )
    parsed = [workload(name="prefill", role="prefill", replicas=2), workload(name="decode", role="decode", replicas=3)]
    for doc in parsed:
        pod = doc["spec"]["template"]["spec"]
        pod["containers"][0]["resources"] = {"limits": {"example.org/device": 2}}
        pod["initContainers"] = [{"resources": {"requests": {"example.org/device": 3}}}]
    demand, warnings = composition.accelerator_demand(parsed)
    assert demand == {"example.org/device": 15}
    assert warnings == []
    zero = deepcopy(parsed[0])
    zero["spec"]["replicas"] = 0
    assert composition.accelerator_demand([zero])[0] == {"example.org/device": 0}
    assert composition.pod_demand(
        {"containers": [{"resources": {"requests": {"example.org/device": 1}, "limits": {"example.org/device": 2}}}]}
    ) == {"example.org/device": 2}
    unsupported = deepcopy(parsed[0])
    unsupported["kind"] = "StatefulSet"
    with pytest.raises(GuideError, match="additional workload layout"):
        composition.accelerator_demand([unsupported])
    duplicate = deepcopy(parsed[0])
    duplicate["spec"]["replicas"] = 4
    with pytest.raises(GuideError, match="Conflicting"):
        composition.compose_documents([[parsed[0], duplicate]], "cache")
    assert len(composition.compose_documents([[parsed[0], parsed[0]]], "cache")) == 1


def test_leader_resources_are_checked_and_unadapted_model_leaders_fail(monkeypatch):
    monkeypatch.setattr(composition, "resolve_by_resource", lambda name: name == "example.org/device")
    worker = workload()["spec"]["template"]
    leader = deepcopy(worker)
    doc = {
        "apiVersion": "leaderworkerset.x-k8s.io/v1",
        "kind": "LeaderWorkerSet",
        "metadata": {"name": "workers", "namespace": "cache"},
        "spec": {
            "replicas": 2,
            "leaderWorkerTemplate": {"size": 3, "workerTemplate": worker, "leaderTemplate": leader},
        },
    }
    with pytest.raises(GuideError, match="leader templates"):
        composition.bind_model([doc], BINDING)
    leader["spec"]["containers"] = [
        {
            "name": "coordinator",
            "resources": {"limits": {"example.org/device": 1}},
            "envFrom": [{"secretRef": {"name": "leader-secret"}}],
        }
    ]
    worker["spec"]["containers"][0]["resources"] = {"limits": {"example.org/device": 2}}
    assert composition.accelerator_demand([doc])[0] == {"example.org/device": 10}
    assert {"kind": "Secret", "name": "leader-secret", "namespace": "cache"} in composition.resource_references([doc])


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    root = tmp_path / "source"
    root.mkdir()
    shell.run_sync(["git", "-C", str(root), "init", "-q"])
    guide = root / "guides" / "optimized-baseline"
    base = guide / "modelserver" / "base"
    overlay = guide / "modelserver" / "overlay"
    base.mkdir(parents=True)
    overlay.mkdir()
    (guide / "README.md").write_text("# Optimized baseline\n", encoding="utf-8")
    (guide / "guide.yaml").write_text(
        yaml.safe_dump({"name": "optimized-baseline", "env": {"static": {}}}), encoding="utf-8"
    )
    (root / "guides" / "env.sh").write_text(
        "export ROUTER_CHART_VERSION=v1.0.0\n"
        "export ROUTER_STANDALONE_CHART=${ROUTER_STANDALONE_CHART:-oci://ghcr.io/llm-d/charts/router}\n"
        'export QUOTED_VERSION="v2.3.4" # pinned release\n'
        "export SINGLE_QUOTED_VERSION='v3.4.5'\n"
        'export DEFAULT_VERSION="${DEFAULT_VERSION:-v4.5.6}"\n'
        "export DYNAMIC=$(touch /tmp/source-parser-must-not-execute)\n"
        "export REFERENCE=${ROUTER_CHART_VERSION}\n"
        "export NESTED=${NESTED:-$(command)}\n"
        "export CHAINED=v1.0.0; command\n",
        encoding="utf-8",
    )
    (base / "deployment.yaml").write_text(
        composition.dump([workload(["--enable-prefix-caching", "--max-num-seqs=128"])]), encoding="utf-8"
    )
    (base / "kustomization.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["deployment.yaml"],
            }
        ),
        encoding="utf-8",
    )
    (overlay / "kustomization.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["../base"],
                "patches": [{"path": "patch.yaml"}],
            }
        ),
        encoding="utf-8",
    )
    (overlay / "patch.yaml").write_text(composition.dump([workload(["--max-num-seqs=64"])]), encoding="utf-8")
    shell.run_sync(["git", "-C", str(root), "add", "."])
    result = shell.run_sync(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ]
    )
    assert result.ok
    return root


def test_source_environment_preserves_literal_and_default_chart_pins(source_tree):
    catalog = asyncio.run(catalog_module.inventory(source_tree))
    assert catalog["environment"] == {
        "ROUTER_CHART_VERSION": "v1.0.0",
        "ROUTER_STANDALONE_CHART": "oci://ghcr.io/llm-d/charts/router",
        "QUOTED_VERSION": "v2.3.4",
        "SINGLE_QUOTED_VERSION": "v3.4.5",
        "DEFAULT_VERSION": "v4.5.6",
    }


def test_real_kustomize_applies_replacement_before_feature_detection(source_tree):
    async def run():
        catalog = await catalog_module.inventory(source_tree)
        preview = await composition.render_unit(
            source_tree, catalog, {"path": "guides/optimized-baseline/modelserver/overlay"}
        )
        assert "--enable-prefix-caching" not in preview["content"]
        assert "--max-num-seqs=64" in preview["content"]
        facts = composition.rendered_optimizations(preview["documents"], catalog["commit"])
        assert next(item for item in facts["features"] if item["id"] == "prefix")["state"] == "not-declared"
        assert len(catalog["optimizations"]["sources"]) == 4

    asyncio.run(run())


def test_source_checkout_is_extracted_once_per_commit(source_tree, monkeypatch):
    archives = []
    original = composition.command

    async def counted(argv, timeout=90):
        archives.append(argv)
        return await original(argv, timeout)

    monkeypatch.setattr(composition, "command", counted)

    async def run():
        catalog = await catalog_module.inventory(source_tree)
        first, second = await asyncio.gather(
            composition.source_checkout(source_tree, catalog["commit"]),
            composition.source_checkout(source_tree, catalog["commit"]),
        )
        assert first == second and (first / "guides/optimized-baseline/modelserver/overlay").is_dir()
        for _ in range(2):
            await composition.render_unit(
                source_tree, catalog, {"path": "guides/optimized-baseline/modelserver/overlay"}
            )

    asyncio.run(run())
    assert sum(argv[:2] == ["git", "-C"] for argv in archives) == 1


def test_python_composition_round_trip_and_stale_edits(source_tree, monkeypatch):
    monkeypatch.setenv("PRISM_K8S_TARGET_NODE", "chosen")

    async def authorized(request, body):
        return source_tree, await catalog_module.inventory(source_tree)

    async def binding(request, body):
        return BINDING

    async def target(request, body, parsed):
        pod = next(item for item in parsed if item["kind"] == "Deployment")["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"kubernetes.io/hostname": "chosen"}
        return {"warnings": [], "level": "device-count", "demand": {}, "available": {}}

    monkeypatch.setattr(composition, "authorized_inventory", authorized)
    monkeypatch.setattr(composition, "model_binding", binding)
    monkeypatch.setattr(composition, "check_target", target)

    async def run():
        catalog = await catalog_module.inventory(source_tree)
        body = {
            "commit": catalog["commit"],
            "clusterId": "fixture",
            "namespace": "cache",
            "units": [{"path": "guides/optimized-baseline/modelserver/overlay"}],
            "validateCluster": False,
            "deploymentIntent": {"scenario": "chat", "objective": "latency", "contextLength": "2048"},
            "guideSelection": {"capabilities": {"prefix": True}},
        }
        result = await composition.compose(None, body)
        assert result["validation"]["clusterValidated"] is False
        assert result["validation"]["runtimeVerified"] is False
        assert "--max-model-len=2048" in result["files"][0]["content"]
        edited = {
            **body,
            "finalConfiguration": {
                "baseChecksum": result["compositionChecksum"],
                "content": result["files"][0]["content"].replace("--max-num-seqs=64", "--max-num-seqs=32"),
            },
        }
        regenerated = await composition.compose(None, edited)
        assert regenerated["compositionChecksum"] == result["compositionChecksum"]
        assert "--max-num-seqs=32" in regenerated["files"][0]["content"]
        assert regenerated["optimizations"]["checksum"] != result["optimizations"]["checksum"]
        edited["finalConfiguration"]["baseChecksum"] = "old"
        with pytest.raises(GuideError, match="Configuration changed"):
            await composition.compose(None, edited)

    asyncio.run(run())


def test_chart_archive_pulls_each_pinned_version_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path))
    pulls = []

    async def helm(argv, timeout=90):
        pulls.append(argv)
        await asyncio.sleep(0)
        (composition.Path(argv[-1]) / "chart-1.0.0.tgz").write_bytes(b"archive")
        return ""

    monkeypatch.setattr(composition, "command", helm)

    async def run():
        chart = "oci://ghcr.io/llm-d/charts/example"
        first, second = await asyncio.gather(
            composition.chart_archive(chart, "v1.0.0"), composition.chart_archive(chart, "v1.0.0")
        )
        assert first == second and first.read_bytes() == b"archive"
        assert first.is_relative_to(tmp_path.resolve())
        await composition.chart_archive(chart, "v2.0.0")

    asyncio.run(run())
    assert [argv[4] for argv in pulls] == ["v1.0.0", "v2.0.0"]


def test_shared_result_resolves_each_key_once():
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(0)
        return {"value": 1}

    async def run():
        shared = {}
        results = await asyncio.gather(*(composition.shared_result(shared, ("key",), factory) for _ in range(3)))
        assert all(result is results[0] for result in results)
        await composition.shared_result(None, ("key",), factory)

    asyncio.run(run())
    assert len(calls) == 2


def test_validation_errors_are_public_and_logged():
    async def rejected():
        raise GuideError("Select a source unit.")

    response = asyncio.run(source_response(rejected()))
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "Select a source unit."}


def test_source_file_rejects_escape(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (tmp_path / "outside").write_text("outside", encoding="utf-8")
    with pytest.raises(GuideError, match="escapes"):
        catalog_module.source_file(root, "../outside")


def test_real_json_patch_removal_and_generator(source_tree):
    overlay = source_tree / "guides/optimized-baseline/modelserver/overlay"
    definition = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": ["../base"],
        "patches": [
            {
                "target": {"kind": "Deployment", "name": "engine"},
                "patch": "- op: remove\n  path: /spec/template/spec/containers/0/args/1\n",
            }
        ],
        "configMapGenerator": [{"name": "engine-config", "literals": ["setting=effective"]}],
    }
    (overlay / "kustomization.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    shell.run_sync(["git", "-C", str(source_tree), "add", "."])
    shell.run_sync(
        [
            "git",
            "-C",
            str(source_tree),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "json patch",
        ]
    )

    async def run():
        catalog = await catalog_module.inventory(source_tree)
        preview = await composition.render_unit(
            source_tree, catalog, {"path": "guides/optimized-baseline/modelserver/overlay"}
        )
        assert "--enable-prefix-caching" not in preview["content"]
        assert any(
            doc["kind"] == "ConfigMap" and doc["data"] == {"setting": "effective"} for doc in preview["documents"]
        )

    asyncio.run(run())


def test_current_device_budget_threshold_and_session_scope(monkeypatch):
    profile = SimpleNamespace(id="fixture")
    monkeypatch.setattr(
        composition, "resolve_by_resource", lambda key: profile if key == "example.org/device" else None
    )
    parsed = [workload()]
    container = parsed[0]["spec"]["template"]["spec"]["containers"][0]
    container["resources"] = {"requests": {"example.org/device": 2}}
    body = {"clusterId": "fixture", "clusterSessionId": "session", "deploymentIntent": {"maxDevices": "2"}}

    async def session(request, identifier):
        return SimpleNamespace(server_id="fixture")

    async def kubectl(args, **kwargs):
        if args[1] == "nodes":
            items = [
                {
                    "status": {
                        "allocatable": {"example.org/device": "4"},
                        "conditions": [{"type": "Ready", "status": "True"}],
                    }
                }
            ]
        else:
            items = [
                {
                    "status": {"phase": "Running"},
                    "spec": {
                        "containers": [{"resources": {"requests": {"example.org/device": 2}}}],
                    },
                }
            ]
        return SimpleNamespace(ok=True, stdout=json.dumps({"items": items}))

    monkeypatch.setattr(composition, "validate_planning_session", session)
    monkeypatch.setattr(composition, "run_kubectl", kubectl)
    result = asyncio.run(composition.check_target(None, body, parsed))
    assert result["available"]["example.org/device"] == 2
    container["resources"]["requests"]["example.org/device"] = 3
    with pytest.raises(GuideError, match="above the budget"):
        asyncio.run(composition.check_target(None, body, parsed))
    body["deploymentIntent"]["maxDevices"] = "4"
    with pytest.raises(GuideError, match="only 2 currently available"):
        asyncio.run(composition.check_target(None, body, parsed))
    container["resources"]["requests"]["example.org/device"] = 2
    body["clusterId"] = "another"
    with pytest.raises(GuideError, match="do not match"):
        asyncio.run(composition.check_target(None, body, parsed))


def test_router_policies_and_unreferenced_configs():
    parsed = [workload()]
    pod = parsed[0]["spec"]["template"]["spec"]
    pod["volumes"] = [{"name": "config", "configMap": {"name": "router"}}]
    config = {
        "kind": "EndpointPickerConfig",
        "plugins": [{"type": "prefix-cache-affinity-filter"}, {"type": "token-load-scorer"}],
        "schedulingProfiles": [
            {
                "plugins": [
                    {"pluginRef": "prefix-cache-affinity-filter"},
                    {"pluginRef": "token-load-scorer"},
                ]
            }
        ],
    }
    parsed.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "router", "namespace": "cache"},
            "data": {"policy.yaml": yaml.safe_dump(config)},
        }
    )
    composition.router_controls(parsed, {"assets": []}, {"affinity": False})
    facts = composition.rendered_optimizations(parsed, "a" * 40)
    assert next(item for item in facts["features"] if item["id"] == "affinity")["state"] == "not-declared"
    assert next(item for item in facts["features"] if item["id"] == "load-routing")["state"] == "configured"
    pod["volumes"] = []
    facts = composition.rendered_optimizations(parsed, "a" * 40)
    assert next(item for item in facts["features"] if item["id"] == "load-routing")["state"] == "not-declared"


def test_prefix_affinity_insertion_preserves_decode_profile():
    config = {
        "kind": "EndpointPickerConfig",
        "plugins": [
            {"type": "token-load-scorer"},
            {"type": "approx-prefix-cache-producer"},
            {"type": "prefix-cache-affinity-filter"},
        ],
        "schedulingProfiles": [
            {"name": name, "plugins": [{"pluginRef": "token-load-scorer"}]} for name in ("default", "prefill", "decode")
        ],
    }
    parsed = [{"kind": "ConfigMap", "data": {"policy.yaml": yaml.safe_dump(config)}}]
    composition.router_controls(parsed, {"assets": []}, {"affinity": True})
    updated = yaml.safe_load(parsed[0]["data"]["policy.yaml"])
    assert updated["schedulingProfiles"][2] == config["schedulingProfiles"][2]
    assert all(
        {"pluginRef": "prefix-cache-affinity-filter"} in profile["plugins"]
        for profile in updated["schedulingProfiles"][:2]
    )


def test_check_target_handles_403_external_refs_prometheus_fallback_and_401(monkeypatch):
    from llm_d_lens.cluster.service import PrometheusNodeInventory
    from llm_d_lens.deploy import slice_capacity

    parsed = [
        workload(replicas=1),
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": "route", "namespace": "cache"},
            "spec": {"parentRefs": [{"kind": "Gateway", "name": "shared-gw", "namespace": "envoy-gateway-system"}]},
        },
    ]
    parsed[0]["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": 2}}
    parsed[0]["spec"]["template"]["spec"]["nodeSelector"] = {"nvidia.com/gpu.product": "NVIDIA-H100"}
    body = {"clusterId": "fixture", "clusterSessionId": "session"}

    inv = PrometheusNodeInventory(
        reachable=True,
        all_nodes=[
            {
                "name": "gpu-1",
                "role": "worker",
                "ready": True,
                "schedulingDisabled": False,
                "cpu": 16,
                "memoryBytes": 64 * 1024 * 1024 * 1024,
                "gpuByProfile": {"nvidia": 4},
                "gpuModelsByProfile": {"nvidia": ["NVIDIA-H100"]},
            }
        ],
        allocatable_by_node={
            "gpu-1": {"cpu": 16, "memoryBytes": 64 * 1024 * 1024 * 1024, "gpuByProfile": {"nvidia": 4}}
        },
        allocated_gpu_by_node={"gpu-1": 1},
    )

    async def session(*_):
        return SimpleNamespace(server_id="fixture")

    async def kubectl_403(args, **kwargs):
        return SimpleNamespace(ok=False, stdout="", stderr="Error from server (Forbidden): is forbidden")

    monkeypatch.setattr(composition, "validate_planning_session", session)
    monkeypatch.setattr(composition, "run_kubectl", kubectl_403)
    monkeypatch.setattr(slice_capacity, "fetch_prometheus_node_inventory", lambda _cid: asyncio.sleep(0, result=inv))

    result = asyncio.run(composition.check_target(None, body, parsed))
    assert result["available"]["nvidia.com/gpu"] == 3.0
    assert any("Could not verify external Gateway/shared-gw" in w for w in result["warnings"])
    assert any("Prometheus metrics" in w for w in result["warnings"])
    assert any("synthesized from Prometheus" in w for w in result["warnings"])

    async def kubectl_not_found(args, **kwargs):
        if args[1] == "Gateway":
            return SimpleNamespace(
                ok=False, stdout="", stderr='gateways.gateway.networking.k8s.io "shared-gw" not found'
            )
        return SimpleNamespace(ok=False, stdout="", stderr="is forbidden")

    monkeypatch.setattr(composition, "run_kubectl", kubectl_not_found)
    with pytest.raises(GuideError, match="unavailable"):
        asyncio.run(composition.check_target(None, body, parsed))

    async def kubectl_401(args, **kwargs):
        return SimpleNamespace(ok=False, stdout="", stderr="error: You must be logged in to the server (Unauthorized)")

    monkeypatch.setattr(composition, "run_kubectl", kubectl_401)
    with pytest.raises(GuideError) as exc:
        asyncio.run(composition.check_target(None, body, parsed))
    assert exc.value.status == 401


def test_compose_dry_run_403_warns_without_failing_and_401_raises(source_tree, monkeypatch):
    async def authorized(request, body):
        return source_tree, await catalog_module.inventory(source_tree)

    async def binding(request, body):
        return BINDING

    async def target(request, body, parsed):
        return {"warnings": [], "level": "device-count", "demand": {}, "available": {}}

    monkeypatch.setattr(composition, "authorized_inventory", authorized)
    monkeypatch.setattr(composition, "model_binding", binding)
    monkeypatch.setattr(composition, "check_target", target)

    async def dry_run_403(args, **kwargs):
        return SimpleNamespace(ok=False, stdout="", stderr="deployments.apps is forbidden")

    monkeypatch.setattr(composition, "run_kubectl", dry_run_403)

    async def run():
        catalog = await catalog_module.inventory(source_tree)
        body = {
            "commit": catalog["commit"],
            "clusterId": "fixture",
            "clusterSessionId": "session",
            "namespace": "cache",
            "units": [{"path": "guides/optimized-baseline/modelserver/overlay"}],
            "validateCluster": True,
        }
        result = await composition.compose(None, body)
        assert result["validation"]["clusterValidated"] is False
        assert any("dry-run skipped" in w for w in result["validation"]["warnings"])

        async def dry_run_401(args, **kwargs):
            return SimpleNamespace(ok=False, stdout="", stderr="Unauthorized")

        monkeypatch.setattr(composition, "run_kubectl", dry_run_401)
        with pytest.raises(GuideError) as exc:
            await composition.compose(None, body)
        assert exc.value.status == 401

    asyncio.run(run())


def test_discover_local_cluster_falls_back_to_prometheus_on_403_and_raises_401(monkeypatch):
    from llm_d_lens.cluster.service import PrometheusNodeInventory
    from llm_d_lens.deploy import guide_sources, slice_capacity

    inv = PrometheusNodeInventory(
        reachable=True,
        all_nodes=[
            {
                "name": "worker-1",
                "role": "worker",
                "version": "v1.32.0",
                "ready": True,
                "schedulingDisabled": False,
                "cpu": 16,
                "memoryBytes": 64 * 1024 * 1024 * 1024,
                "gpuByProfile": {"nvidia": 2},
                "gpuModelsByProfile": {},
            }
        ],
        allocatable_by_node={
            "worker-1": {"cpu": 16, "memoryBytes": 64 * 1024 * 1024 * 1024, "gpuByProfile": {"nvidia": 2}}
        },
        allocated_gpu_by_node={},
    )

    monkeypatch.setattr(guide_sources, "require_active_session", lambda _sid: SimpleNamespace(server_id="cluster-1"))
    monkeypatch.setattr(guide_sources, "current_principal", lambda _req: None)
    monkeypatch.setattr(guide_sources, "require_cluster_access", lambda _p, _c: None)
    monkeypatch.setattr(guide_sources, "require_internal_permission", lambda _req, _path: asyncio.sleep(0))
    monkeypatch.setattr(slice_capacity, "fetch_prometheus_node_inventory", lambda _cid: asyncio.sleep(0, result=inv))

    async def kubectl_403(argv, **kwargs):
        return SimpleNamespace(ok=False, stdout="", stderr="is forbidden")

    monkeypatch.setattr(guide_sources, "run_kubectl", kubectl_403)
    profile = asyncio.run(guide_sources.discover_local_cluster(None, "session-1"))
    assert profile is not None
    assert profile["version"] == "v1.32.0"
    assert profile["allocatable"]["accelerators"] == {"nvidia.com/gpu": 2}
    assert profile["allocatable"]["memoryGiB"] == 64

    async def kubectl_401(argv, **kwargs):
        return SimpleNamespace(ok=False, stdout="", stderr="Unauthorized")

    monkeypatch.setattr(guide_sources, "run_kubectl", kubectl_401)
    with pytest.raises(GuideError) as exc:
        asyncio.run(guide_sources.discover_local_cluster(None, "session-1"))
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_model_binding_resolves_local_disk_and_nfs_namespace(monkeypatch):
    from llm_d_lens.model_cache import router as model_cache_router
    from llm_d_lens.storage import router as storage_router

    async def fake_require_internal(_request, _path):
        return None

    async def fake_get_entry(entry_id, _request):
        return {
            "id": entry_id,
            "clusterId": "cluster-1",
            "storageVolumeId": "vol-1",
            "status": "ready",
            "nodeProgress": [{"node": "*", "status": "ready"}],
            "source": {"kind": "huggingface", "huggingface": {"repoId": "org/model", "revision": "main"}},
        }

    monkeypatch.setattr(composition, "require_internal_permission", fake_require_internal)
    monkeypatch.setattr(model_cache_router, "get_entry", fake_get_entry)

    for volume_payload in (
        {
            "id": "vol-1",
            "clusterId": "cluster-1",
            "status": "ready",
            "pvcName": "prism-storage-vol-1",
            "localDisk": {"hostPath": "/data/models", "namespace": "lens-edit"},
        },
        {
            "id": "vol-1",
            "clusterId": "cluster-1",
            "status": "ready",
            "pvcName": "prism-storage-vol-1",
            "nfs": {"server": "nfs.example.com", "path": "/exports", "namespace": "lens-edit"},
        },
    ):

        async def fake_get_volume(_volume_id, _request, _payload=volume_payload):
            return _payload

        monkeypatch.setattr(storage_router, "get_volume", fake_get_volume)
        binding = await composition.model_binding(
            None,
            {"clusterId": "cluster-1", "namespace": "lens-edit", "modelCacheEntryId": "mc-1"},
        )
        assert binding["namespace"] == "lens-edit"
        assert binding["pvcName"] == "prism-storage-vol-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["standalone", "gateway"])
async def test_render_router_applies_plaintext_epp_and_respects_overrides(tmp_path, monkeypatch, mode):
    from llm_d_lens.deploy.data_plane import PLAINTEXT_EPP_VALUES_PATH

    archive = tmp_path / f"llm-d-router-{mode}-1.0.0.tgz"
    archive.write_bytes(b"tgz")
    calls = []

    async def fake_chart_archive(chart, version):
        return archive

    async def fake_command(argv, timeout=90):
        calls.append(argv)
        return "kind: Service\nmetadata:\n  name: router\n"

    monkeypatch.setattr(composition, "chart_archive", fake_chart_archive)
    monkeypatch.setattr(composition, "command", fake_command)
    catalog = {
        "assets": [
            {"path": "router/default.yaml", "kind": "values", "content": "router:\n  replicas: 1\n"},
            {
                "path": "router/tls.yaml",
                "kind": "values",
                "content": "router:\n  epp:\n    flags:\n      secure-serving: true\n",
            },
        ]
    }
    base_body = {
        "namespace": "bench-ns",
        "router": {"mode": mode, "release": "rel", "values": [{"path": "router/default.yaml"}]},
    }

    await composition.render_router(catalog, base_body)
    args_default = calls[-1]
    assert ["-f", str(PLAINTEXT_EPP_VALUES_PATH)] == args_default[-2:]

    override_values_body = {
        "namespace": "bench-ns",
        "router": {"mode": mode, "release": "rel", "values": [{"path": "router/tls.yaml"}]},
    }
    await composition.render_router(catalog, override_values_body)
    assert str(PLAINTEXT_EPP_VALUES_PATH) not in calls[-1]

    override_set_body = {
        **base_body,
        "router": {**base_body["router"], "set": ["router.epp.flags.secure-serving=true"]},
    }
    await composition.render_router(catalog, override_set_body)
    assert str(PLAINTEXT_EPP_VALUES_PATH) not in calls[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("chart_name", ["llm-d-router-standalone", "llm-d-router-gateway"])
async def test_render_unit_applies_plaintext_epp_ignoring_chart_defaults(tmp_path, monkeypatch, chart_name):
    from llm_d_lens.deploy.data_plane import PLAINTEXT_EPP_VALUES_PATH

    archive = tmp_path / f"{chart_name}-1.0.0.tgz"
    archive.write_bytes(b"tgz")
    template_calls = []

    async def fake_chart_archive(chart, version):
        return archive

    async def fake_command(argv, timeout=90):
        if argv[:3] == ["helm", "show", "values"]:
            return "router:\n  epp:\n    flags:\n      secure-serving: true\n"
        template_calls.append(argv)
        return composition.dump(
            [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": "rel-epp", "namespace": "ns"},
                    "spec": {
                        "template": {
                            "spec": {
                                "volumes": [{"name": "envoy-cfg", "configMap": {"name": "envoy"}}],
                                "containers": [
                                    {"name": "epp", "args": ["--pool-name=p", "--secure-serving=false"]},
                                    {
                                        "name": "envoy-proxy",
                                        "volumeMounts": [{"name": "envoy-cfg", "mountPath": "/etc/envoy"}],
                                    },
                                ],
                            }
                        }
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "envoy", "namespace": "ns"},
                    "data": {
                        "envoy.yaml": yaml.safe_dump(
                            {
                                "static_resources": {
                                    "clusters": [
                                        {
                                            "name": "ext_proc",
                                            "transport_socket": {"name": "envoy.transport_sockets.tls"},
                                        }
                                    ]
                                }
                            },
                            sort_keys=False,
                        )
                    },
                },
            ]
        )

    monkeypatch.setattr(composition, "chart_archive", fake_chart_archive)
    monkeypatch.setattr(composition, "command", fake_command)
    catalog = {
        "commit": "a" * 40,
        "guides": [],
        "assets": [
            {
                "path": f"charts/{chart_name}",
                "kind": "chart",
                "guide": "router",
                "chart": f"oci://ghcr.io/llm-d/charts/{chart_name}",
                "release": "rel",
                "version": "v1.0.0",
                "checksum": "chk",
            },
            {
                "path": "values/tls.yaml",
                "kind": "values",
                "content": "router:\n  epp:\n    flags:\n      secure-serving: true\n",
            },
        ],
    }

    # Chart defaults (helm show values) must NOT suppress PLAINTEXT_EPP_VALUES_PATH,
    # and Envoy ext_proc TLS must be reconciled to plaintext when EPP uses --secure-serving=false.
    unit = await composition.render_unit(tmp_path, catalog, {"path": f"charts/{chart_name}", "namespace": "ns"})
    assert str(PLAINTEXT_EPP_VALUES_PATH) in template_calls[-1]
    envoy_cfg = yaml.safe_load(next(d for d in unit["documents"] if d["kind"] == "ConfigMap")["data"]["envoy.yaml"])
    assert "transport_socket" not in envoy_cfg["static_resources"]["clusters"][0]

    # Explicit selection["values"], chartValues, or chartSet overrides MUST be respected.
    await composition.render_unit(
        tmp_path,
        catalog,
        {
            "path": f"charts/{chart_name}",
            "namespace": "ns",
            "values": "router:\n  epp:\n    flags:\n      secure-serving: true\n",
        },
    )
    assert str(PLAINTEXT_EPP_VALUES_PATH) not in template_calls[-1]

    await composition.render_unit(
        tmp_path,
        catalog,
        {"path": f"charts/{chart_name}", "namespace": "ns", "chartValues": ["values/tls.yaml"]},
    )
    assert str(PLAINTEXT_EPP_VALUES_PATH) not in template_calls[-1]

    await composition.render_unit(
        tmp_path,
        catalog,
        {"path": f"charts/{chart_name}", "namespace": "ns", "chartSet": ["router.epp.flags.secure-serving=true"]},
    )
    assert str(PLAINTEXT_EPP_VALUES_PATH) not in template_calls[-1]


@pytest.mark.asyncio
async def test_render_router_reconciles_envoy_ext_proc_tls_only_when_epp_plaintext(tmp_path, monkeypatch):
    archive = tmp_path / "llm-d-router-standalone-0.9.0.tgz"
    archive.write_bytes(b"tgz")

    def make_rendered_manifest(*, epp_flag: str, with_tls: bool) -> str:
        ext_proc_cluster = {"name": "ext_proc", "type": "STRICT_DNS"}
        if with_tls:
            ext_proc_cluster["transport_socket"] = {
                "name": "envoy.transport_sockets.tls",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext"
                },
            }
            ext_proc_cluster["health_checks"] = [{"timeout": "1s", "tls_options": {"alpn_protocols": ["h2"]}}]
        other_cluster = {
            "name": "upstream_tls",
            "transport_socket": {"name": "envoy.transport_sockets.tls"},
        }
        envoy_yaml = yaml.safe_dump(
            {"static_resources": {"clusters": [ext_proc_cluster, other_cluster]}}, sort_keys=False
        )
        docs = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "rel-epp", "namespace": "ns"},
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [{"name": "envoy-cfg", "configMap": {"name": "envoy"}}],
                            "containers": [
                                {"name": "epp", "args": ["--pool-name=p", epp_flag]},
                                {
                                    "name": "envoy-proxy",
                                    "volumeMounts": [{"name": "envoy-cfg", "mountPath": "/etc/envoy"}],
                                },
                            ],
                        }
                    }
                },
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "envoy", "namespace": "ns"},
                "data": {"envoy.yaml": envoy_yaml},
            },
        ]
        return composition.dump(docs)

    rendered_text = make_rendered_manifest(epp_flag="--secure-serving=false", with_tls=True)

    async def fake_chart_archive(chart, version):
        return archive

    async def fake_command(argv, timeout=90):
        return rendered_text

    monkeypatch.setattr(composition, "chart_archive", fake_chart_archive)
    monkeypatch.setattr(composition, "command", fake_command)
    catalog = {"assets": [{"path": "router/default.yaml", "kind": "values", "content": "router: {}\n"}]}
    body = {
        "namespace": "ns",
        "router": {"mode": "standalone", "release": "rel", "values": [{"path": "router/default.yaml"}]},
    }

    docs = await composition.render_router(catalog, body)
    envoy_cfg = yaml.safe_load(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["envoy.yaml"])
    clusters = {c["name"]: c for c in envoy_cfg["static_resources"]["clusters"]}
    assert "transport_socket" not in clusters["ext_proc"]
    assert clusters["ext_proc"]["health_checks"] == [{"timeout": "1s"}]
    assert clusters["upstream_tls"]["transport_socket"] == {"name": "envoy.transport_sockets.tls"}

    # When EPP is explicitly on TLS (--secure-serving=true), Envoy TLS must stay intact.
    rendered_text = make_rendered_manifest(epp_flag="--secure-serving=true", with_tls=True)
    docs_tls = await composition.render_router(catalog, body)
    envoy_tls = yaml.safe_load(next(d for d in docs_tls if d["kind"] == "ConfigMap")["data"]["envoy.yaml"])
    assert envoy_tls["static_resources"]["clusters"][0]["transport_socket"]["name"] == "envoy.transport_sockets.tls"

    # Already-plaintext Envoy config is a clean no-op preserving raw formatting.
    rendered_text = make_rendered_manifest(epp_flag="--secure-serving=false", with_tls=False)
    raw_envoy = yaml.safe_load(rendered_text.split("---\n")[1])["data"]["envoy.yaml"]
    docs_noop = await composition.render_router(catalog, body)
    assert next(d for d in docs_noop if d["kind"] == "ConfigMap")["data"]["envoy.yaml"] == raw_envoy


def test_guide_planning_image_priority_and_override_preservation():
    from llm_d_lens.deploy import guide_planning
    from llm_d_lens.deploy.providers.hardware_profile import runtime_image

    profile_default = runtime_image(accelerator="nvidia")
    managed_repo = profile_default.rsplit(":", 1)[0]
    custom_tag = f"{managed_repo}:v0.31.0-custom"
    cluster_img = "registry.internal:5000/org/vllm:v0.8.5"

    assert (
        guide_planning.pin_model_server_image(
            custom_tag,
            accelerator="nvidia",
            cluster_vllm_image=cluster_img,
            explicit_override=True,
        )
        == custom_tag
    )

    doc_override = guide_planning.plan_documents(
        [workload()],
        "example/model",
        {},
        None,
        {
            "accelerator": "nvidia",
            "runtimeImage": custom_tag,
            "runtimeImageOverride": True,
        },
        cluster_vllm_image=cluster_img,
    )
    assert doc_override["documents"][0]["spec"]["template"]["spec"]["containers"][0]["image"] == custom_tag

    doc_cluster = guide_planning.plan_documents(
        [workload()],
        "example/model",
        {},
        None,
        {"accelerator": "nvidia"},
        cluster_vllm_image=cluster_img,
    )
    assert doc_cluster["documents"][0]["spec"]["template"]["spec"]["containers"][0]["image"] == cluster_img

    doc_stock_pinned = guide_planning.plan_documents(
        [workload()],
        "example/model",
        {},
        None,
        {"accelerator": "nvidia", "runtimeImage": profile_default, "runtimeImageOverride": False},
        cluster_vllm_image=cluster_img,
    )
    assert doc_stock_pinned["documents"][0]["spec"]["template"]["spec"]["containers"][0]["image"] == cluster_img

    doc_profile = guide_planning.plan_documents(
        [workload()],
        "example/model",
        {},
        None,
        {"accelerator": "nvidia"},
    )
    assert doc_profile["documents"][0]["spec"]["template"]["spec"]["containers"][0]["image"] == profile_default

    with pytest.raises(GuideError):
        guide_planning.plan_documents(
            [workload()],
            "example/model",
            {},
            None,
            {"accelerator": "nvidia", "runtimeImage": "untagged"},
        )


def test_compose_inherits_cluster_vllm_image_and_respects_explicit_override(source_tree, monkeypatch):
    async def authorized(request, body):
        return source_tree, await catalog_module.inventory(source_tree)

    async def binding(request, body):
        return BINDING

    async def target(request, body, parsed):
        return {"warnings": [], "level": "device-count", "demand": {}, "available": {}}

    monkeypatch.setattr(composition, "authorized_inventory", authorized)
    monkeypatch.setattr(composition, "model_binding", binding)
    monkeypatch.setattr(composition, "check_target", target)
    monkeypatch.setattr(
        composition,
        "resolve_cluster_vllm_image",
        lambda cid: "registry.internal:5000/org/vllm:cluster-1" if cid == "cluster-custom" else None,
    )

    async def run():
        catalog = await catalog_module.inventory(source_tree)
        base_body = {
            "commit": catalog["commit"],
            "clusterId": "cluster-custom",
            "namespace": "cache",
            "units": [{"path": "guides/optimized-baseline/modelserver/overlay"}],
            "validateCluster": False,
        }
        inherited = await composition.compose(None, base_body)
        inherited_docs = list(yaml.safe_load_all(inherited["files"][0]["content"]))
        inherited_deploy = next(d for d in inherited_docs if d["kind"] == "Deployment")
        assert (
            inherited_deploy["spec"]["template"]["spec"]["containers"][0]["image"]
            == "registry.internal:5000/org/vllm:cluster-1"
        )

        overridden = await composition.compose(
            None,
            {
                **base_body,
                "runtimeImage": "docker.io/vllm/vllm-openai:v0.31.0-manual",
                "runtimeImageOverride": True,
            },
        )
        overridden_docs = list(yaml.safe_load_all(overridden["files"][0]["content"]))
        overridden_deploy = next(d for d in overridden_docs if d["kind"] == "Deployment")
        assert (
            overridden_deploy["spec"]["template"]["spec"]["containers"][0]["image"]
            == "docker.io/vllm/vllm-openai:v0.31.0-manual"
        )

        with pytest.raises(GuideError):
            await composition.compose(None, {**base_body, "runtimeImage": "invalid image;id"})

        tampered = inherited["files"][0]["content"].replace(
            "registry.internal:5000/org/vllm:cluster-1",
            "registry:5000/untagged",
        )
        with pytest.raises(GuideError):
            await composition.compose(
                None,
                {
                    **base_body,
                    "finalConfiguration": {
                        "baseChecksum": inherited["compositionChecksum"],
                        "content": tampered,
                    },
                },
            )

    asyncio.run(run())


def test_plan_documents_ignores_smuggled_cluster_vllm_image_and_validates_kwarg():
    from llm_d_lens.deploy import guide_planning
    from llm_d_lens.deploy.providers.hardware_profile import runtime_image

    profile_default = runtime_image(accelerator="nvidia")
    smuggled = guide_planning.plan_documents(
        [workload()],
        "example/model",
        {},
        None,
        {"accelerator": "nvidia", "_clusterVllmImage": "-flag;injection"},
    )
    assert smuggled["documents"][0]["spec"]["template"]["spec"]["containers"][0]["image"] == profile_default

    with pytest.raises(GuideError):
        guide_planning.plan_documents(
            [workload()],
            "example/model",
            {},
            None,
            {"accelerator": "nvidia"},
            cluster_vllm_image="-flag;injection",
        )
