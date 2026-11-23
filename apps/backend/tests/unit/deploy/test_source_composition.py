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
