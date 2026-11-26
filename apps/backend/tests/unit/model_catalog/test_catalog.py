"""Catalog persistence, deployment inheritance and authorization regressions."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_d_lens.db.dao.base import ConcurrentUpdateError
from llm_d_lens.model_catalog import service
from llm_d_lens.model_catalog.contracts import CatalogInput, CatalogUpdate, ServingDefaults, serving_arguments


def custom(**kwargs):
    return service.create_entry(CatalogInput(name="Custom model", repository="example/custom", **kwargs))


def deployment(model="example/custom", **kwargs):
    from llm_d_lens.deploy.standard_kubernetes_service import StandardKubernetesServiceRequest

    return StandardKubernetesServiceRequest(
        model=model,
        deployment_name="custom-model",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
        storage_volume_id="volume",
        **kwargs,
    )


def test_builtin_seeds_and_custom_round_trip():
    assert len(service.builtin_entries()) == 101
    assert sum(entry.serving_defaults.tool_calling for entry in service.builtin_entries()) == 14
    entry = custom(
        revision="release-2",
        weightGiB=4.2,
        contextLength=32768,
        servingDefaults={"toolCalling": True, "toolParser": "hermes", "extra": "kv-cache-dtype=fp8"},
    )
    assert service.get_entry(entry.id) == entry
    assert entry in service.list_entries()
    assert entry.serving_defaults.tool_parser == "hermes"


def test_update_delete_versions_and_immutable_deployment_snapshot():
    entry = custom(servingDefaults={"toolCalling": True, "toolParser": "hermes"})
    request = service.resolve_deployment_model(
        deployment(model_catalog_id=entry.id, model_catalog_version=entry.version)
    )
    update = CatalogUpdate(**entry.model_dump(exclude={"id", "source", "release_date", "created_at", "updated_at"}))
    update.name = "Renamed model"
    saved = service.update_entry(entry.id, update)
    assert saved.version == 2
    assert request.model_catalog_snapshot["name"] == "Custom model"
    assert request.model_catalog_version == 1
    with pytest.raises(ConcurrentUpdateError):
        service.update_entry(entry.id, update)
    with pytest.raises(ConcurrentUpdateError):
        service.delete_entry(entry.id, 1)
    with pytest.raises(ValueError, match="defaults changed"):
        service.resolve_deployment_model(deployment(model_catalog_id=entry.id, model_catalog_version=1))
    service.delete_entry(entry.id, saved.version)
    assert service.get_entry(entry.id) is None
    assert request.model_catalog_snapshot["repository"] == "example/custom"


@pytest.mark.parametrize(
    "extra,expected",
    [
        ("tool-call-parser=qwen3_coder", {"enable-auto-tool-choice": "true", "tool-call-parser": "qwen3_coder"}),
        ("enable-auto-tool-choice=true", {"enable-auto-tool-choice": "true", "tool-call-parser": "hermes"}),
        ("enable-auto-tool-choice=false", {"enable-auto-tool-choice": "false"}),
    ],
)
def test_tool_overrides_merge_individual_fields(extra, expected):
    settings = ServingDefaults(toolCalling=True, toolParser="hermes", extra=extra)
    assert {item["name"]: item["value"] for item in serving_arguments(settings)} == expected


@pytest.mark.parametrize(
    "settings",
    [
        {"toolCalling": True},
        {"toolCalling": True, "toolParser": "bad parser"},
        {"extra": "max-model-len=8192"},
        {"extra": "revision=other"},
        {"extra": "enable-auto-tool-choice=maybe"},
        {"extra": "--enforce-eager"},
        {"chatTemplate": "{{ messages }}\n{{ eos }}"},
    ],
)
def test_invalid_settings_rejected(settings):
    with pytest.raises(ValueError):
        ServingDefaults.model_validate(settings)


def test_deployment_inherits_defaults_allows_disable_and_clearing():
    entry = custom(
        revision="release-2",
        contextLength=16384,
        servingDefaults={
            "toolCalling": True,
            "toolParser": "hermes",
            "reasoningParser": "qwen3",
            "extra": "kv-cache-dtype=fp8",
        },
    )
    request = service.resolve_deployment_model(
        deployment(
            model_catalog_id=entry.id,
            serving_overrides={
                "toolCalling": False,
                "reasoningParser": "",
                "extra": "",
            },
        )
    )
    assert request.max_model_len == 16384
    assert request.model_revision == "release-2"
    assert request.vllm_arguments == [
        {"name": "enable-auto-tool-choice", "value": "false"},
        {"name": "revision", "value": "release-2"},
    ]
    assert service.get_entry(entry.id).serving_defaults.tool_calling
    explicit = service.resolve_deployment_model(deployment(model_catalog_id=entry.id, max_model_len=8192))
    assert explicit.max_model_len == 8192
    with pytest.raises(ValueError, match="repository"):
        service.resolve_deployment_model(deployment(model="other/repo", model_catalog_id=entry.id))


def test_builtin_defaults_apply_to_api_requests_with_catalog_id():
    request = service.resolve_deployment_model(
        deployment(
            model="Qwen/Qwen3-8B",
            model_catalog_id="qwen-qwen3-8b",
            vllm_arguments=[{"name": "tool-call-parser", "value": "qwen3_coder"}],
        )
    )
    args = {item["name"]: item["value"] for item in request.vllm_arguments}
    assert args["enable-auto-tool-choice"] == "true"
    assert args["tool-call-parser"] == "qwen3_coder"
    assert "revision" not in args
    assert request.model_catalog_snapshot["source"] == "builtin"


def test_http_crud_and_builtin_protection():
    from llm_d_lens.model_catalog.router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/api/v1/model-catalog", json={"name": "Custom", "repository": "custom/repo"})
    assert response.status_code == 201
    record = response.json()
    body = {"name": "Updated", "repository": "custom/repo", "version": record["version"]}
    assert client.put(f"/api/v1/model-catalog/{record['id']}", json=body).status_code == 200
    assert client.put(f"/api/v1/model-catalog/{record['id']}", json=body).status_code == 409
    assert client.delete(f"/api/v1/model-catalog/{record['id']}?version=2").status_code == 200
    builtin = service.builtin_entries()[0]
    assert client.put(f"/api/v1/model-catalog/{builtin.id}", json=body).status_code == 422
    assert client.delete(f"/api/v1/model-catalog/{builtin.id}?version=1").status_code == 422
    assert len(client.get("/api/v1/model-catalog").json()["items"]) == 101


def test_catalog_permission_registration_and_role_boundary():
    from llm_d_lens.auth.permissions import role_permissions
    from llm_d_lens.auth.routes import ROUTE_PERMISSIONS

    codes = {f"model-catalog:entry:{action}" for action in ("read", "create", "update", "delete")}
    assert codes <= role_permissions("admin")
    assert codes <= role_permissions("maintainer")
    assert not codes & role_permissions("end-user")
    assert codes <= {route.permission for route in ROUTE_PERMISSIONS}


@pytest.mark.parametrize("shell", [False, True])
def test_disabling_tools_replaces_upstream_bare_flag(shell):
    from llm_d_lens.deploy.guide_runtime import apply_runtime_overrides

    container = (
        {"command": ["sh", "-c", "vllm serve model --enable-auto-tool-choice --port=8000"]}
        if shell
        else {
            "command": ["vllm", "serve", "model"],
            "args": ["--enable-auto-tool-choice", "--port=8000"],
        }
    )
    apply_runtime_overrides(container, [{"name": "no-enable-auto-tool-choice", "value": "", "kind": "argument"}])
    tokens = " ".join(container.get("command", []) + container.get("args", []))
    assert "--enable-auto-tool-choice" not in tokens
    assert "--no-enable-auto-tool-choice" in tokens
    assert "--port=8000" in tokens


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    "value,expected", [("true", "--enable-auto-tool-choice"), ("false", "--no-enable-auto-tool-choice")]
)
def test_catalog_boolean_values_become_bare_flags(shell, value, expected):
    from llm_d_lens.deploy.guide_runtime import apply_runtime_overrides

    container = (
        {"command": ["sh", "-c", "vllm serve model --enable-auto-tool-choice --port=8000"]}
        if shell
        else {"command": ["vllm", "serve", "model"], "args": ["--enable-auto-tool-choice", "--port=8000"]}
    )
    apply_runtime_overrides(container, [{"name": "enable-auto-tool-choice", "value": value, "kind": "argument"}])
    tokens = (" ".join(container.get("command", []) + container.get("args", []))).split()
    assert [token for token in tokens if "auto-tool-choice" in token] == [expected]


def test_cache_selection_matches_revision():
    from types import SimpleNamespace as Obj

    from llm_d_lens.deploy.router import _ready_model_cache_entry

    entries = [
        Obj(status="ready", source=Obj(huggingface=Obj(repo_id="custom/model", revision=revision)))
        for revision in ("main", "release-2")
    ]
    assert _ready_model_cache_entry(entries, "custom/model", "release-2") is entries[1]
    assert _ready_model_cache_entry(entries, "custom/model", "missing") is None


def test_legacy_api_retains_explicit_arguments_and_cache_revision_selection():
    original = deployment(model="Qwen/Qwen3-8B", vllm_arguments=[{"name": "revision", "value": "release"}])
    resolved = service.resolve_deployment_model(original)
    assert resolved.vllm_arguments == original.vllm_arguments
    assert resolved.model_revision is None
    assert resolved.model_catalog_id is None


def test_catalog_weight_is_used_for_planning_and_standard_provenance(monkeypatch):
    from llm_d_lens.agentic.models import AgenticDeploymentCreateRequest
    from llm_d_lens.agentic.planner import PlanningFacts
    from llm_d_lens.deploy.standard_kubernetes_service import build_standard_kubernetes_service_configuration

    entry = custom(weightGiB=12, contextLength=8192)
    request = deployment(model_catalog_id=entry.id, use_cluster_proxy=False)
    agentic = AgenticDeploymentCreateRequest(
        **request.model_dump(),
        planning_facts=PlanningFacts(
            model_weight_gib=1,
            vram_per_gpu_gib=24,
            free_gpu_count=1,
        ),
    )
    assert service.resolve_deployment_model(agentic).planning_facts.model_weight_gib == 12
    resolved = service.resolve_deployment_model(request)
    monkeypatch.setattr("llm_d_lens.deploy.providers.hardware_profile.runtime_image", lambda **kwargs: "fixture/image")
    configuration = build_standard_kubernetes_service_configuration(resolved)
    assert configuration.provenance["model_catalog_id"] == entry.id
    assert configuration.provenance["model_catalog_snapshot"]["weightGiB"] == 12
