"""Unit tests for the in-process LensLocal facade."""

from __future__ import annotations

import importlib
import inspect
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from fastapi import Form, Header, Query
from starlette.background import BackgroundTasks
from starlette.requests import Request

from llm_d_lens.auth.context import context_principal
from llm_d_lens.auth.service import default_service
from llm_d_lens.auth.settings import AuthSettings, set_settings_for_testing
from llm_d_lens.core.exceptions import ForbiddenError, UnauthenticatedError
from llm_d_lens.sdk import LensLocal
from llm_d_lens.sdk import local as local_sdk

STRONG_PASSWORD = "Correct-Horse-9!"  # noqa: S105 - test fixture


@pytest.fixture
def local_users():
    set_settings_for_testing(
        AuthSettings(
            auth_mode="local",
            auto_seed_admin=False,
            session_ttl_seconds=3600,
            session_idle_seconds=3600,
        )
    )
    service = default_service()
    service.ensure_builtin_roles()
    admin = service.bootstrap_admin("root", STRONG_PASSWORD)
    limited = service.create_user(username="limited", password=STRONG_PASSWORD)
    return admin, limited


def test_identity_constructor_context_manager_and_errors(local_users):
    admin, _ = local_users

    lens = LensLocal(admin.username)
    assert lens.user_id == admin.id
    assert LensLocal(user_id=admin.id).user_id == admin.id

    with LensLocal(admin.username) as entered:
        assert entered is not None
        principal = context_principal()
        assert principal is not None
        assert principal.user_id == admin.id

    assert context_principal() is None

    with pytest.raises(ValueError):
        LensLocal()
    with pytest.raises(ValueError):
        LensLocal(admin.username, user_id=admin.id)
    with pytest.raises(UnauthenticatedError):
        LensLocal("missing-user")


def test_from_env(monkeypatch, local_users):
    admin, _ = local_users

    monkeypatch.setenv("LENS_USERNAME", admin.username)
    monkeypatch.delenv("LENS_USER_ID", raising=False)
    assert LensLocal.from_env().user_id == admin.id

    monkeypatch.delenv("LENS_USERNAME")
    monkeypatch.setenv("LENS_USER_ID", admin.id)
    assert LensLocal.from_env().user_id == admin.id

    monkeypatch.delenv("LENS_USER_ID")
    with pytest.raises(ValueError):
        LensLocal.from_env()


@pytest.mark.asyncio
async def test_deployments_delegate_through_local_sdk(monkeypatch, local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)

    monkeypatch.setattr(local_sdk, "list_local_executions", Mock(return_value=["execution"]))
    monkeypatch.setattr(local_sdk, "get_local_execution", Mock(return_value="execution-detail"))
    monkeypatch.setattr(local_sdk, "create_local_run", AsyncMock(return_value="run-created"))
    monkeypatch.setattr(local_sdk, "get_local_run", AsyncMock(return_value="run-detail"))
    monkeypatch.setattr(local_sdk, "wait_local_run", AsyncMock(return_value="run-finished"))
    monkeypatch.setattr(local_sdk, "delete_local_execution", AsyncMock(return_value=None))

    executions = lens.deployments.list_executions(cluster_id="cluster-a")
    assert isinstance(executions, local_sdk.LensListResponse)
    assert executions.items == ["execution"]
    assert lens.deployments.get_execution("execution-1") == "execution-detail"
    assert await lens.deployments.create_run("request") == "run-created"
    assert await lens.deployments.get_run("run-1") == "run-detail"
    assert await lens.deployments.wait_run("run-1", timeout=10, poll_interval=1) == "run-finished"
    assert await lens.deployments.delete_execution("execution-1", delete_namespace=False) is None


@pytest.mark.asyncio
async def test_evaluation_delegates_every_method(monkeypatch, local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)
    sentinel = object()

    names = [
        "benchmark_defaults",
        "estimate_benchmark_timing",
        "create_run",
        "list_runs",
        "get_run",
        "cancel_run",
        "delete_benchmark_run",
        "create_workflow_run",
        "create_evaluation",
        "list_workflow_runs",
        "get_workflow_run",
        "get_workflow_run_details",
        "retry_workflow_run",
        "cancel_workflow_run",
        "cancel_evaluation_case",
        "delete_workflow_run",
    ]
    for name in names:
        monkeypatch.setattr(local_sdk, name, AsyncMock(return_value=sentinel))

    assert await lens.evaluation.benchmark_defaults() is sentinel
    assert await lens.evaluation.estimate_timing("spec") is sentinel
    assert await lens.evaluation.create_run("request") is sentinel
    assert await lens.evaluation.list_runs() is sentinel
    assert await lens.evaluation.get_run("run") is sentinel
    assert await lens.evaluation.cancel_run("run") is sentinel
    assert await lens.evaluation.delete_run("run") is sentinel
    assert await lens.evaluation.create_workflow_run("request") is sentinel
    assert await lens.evaluation.create_evaluation("request") is sentinel
    assert await lens.evaluation.list_workflow_runs() is sentinel
    assert await lens.evaluation.get_workflow_run("workflow") is sentinel
    assert await lens.evaluation.get_workflow_run_details("workflow") is sentinel
    assert await lens.evaluation.retry_workflow_run("workflow") is sentinel
    assert await lens.evaluation.cancel_workflow_run("workflow") is sentinel
    assert await lens.evaluation.cancel_evaluation_case("workflow", "case") is sentinel
    assert await lens.evaluation.delete_workflow_run("workflow") is sentinel


@pytest.mark.asyncio
async def test_dashboard_delegates_with_ambient_principal(monkeypatch, local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)
    service = SimpleNamespace(
        welcome_overview=AsyncMock(return_value="overview"),
        alerts=AsyncMock(return_value="alerts"),
    )
    monkeypatch.setattr("llm_d_lens.dashboard.service.default_service", lambda: service)

    assert await lens.dashboard.overview(cluster_id="cluster-a", refresh=True) == "overview"
    service.welcome_overview.assert_awaited_once_with(ANY, cluster_id="cluster-a", refresh=True)
    assert await lens.dashboard.alerts(severity="critical", limit=10) == "alerts"
    service.alerts.assert_awaited_once_with(
        ANY,
        cluster_id=None,
        severity="critical",
        category=None,
        search="",
        cursor=None,
        limit=10,
    )


def test_every_public_route_has_an_sdk_namespace(local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)
    namespaces = (
        "deploy",
        "remote_deploy",
        "clusters",
        "cluster_bootstrap",
        "storage",
        "model_cache",
        "model_services",
        "simulation",
        "external_providers",
        "configuration",
        "aic",
        "hardware",
        "administration",
        "system",
        "observability",
        "accelerator",
        "cluster_stack",
        "gpu_driver",
        "profiling",
        "deployment_monitoring",
    )
    module_paths = [
        "llm_d_lens.deploy.router",
        "llm_d_lens.deploy.remote_deploy",
        "llm_d_lens.cluster.router",
        "llm_d_lens.cluster.bootstrap.router",
        "llm_d_lens.storage.router",
        "llm_d_lens.model_cache.router",
        "llm_d_lens.model_service.router",
        "llm_d_lens.simulation.router",
        "llm_d_lens.ai_providers.router",
        "llm_d_lens.configuration.router",
        "llm_d_lens.aic.router",
        "llm_d_lens.hardware.router",
        "llm_d_lens.auth.router",
        "llm_d_lens.versions.router",
        "llm_d_lens.db.system_router",
        "llm_d_lens.monitoring.cluster_stack.router",
        "llm_d_lens.monitoring.deployment.router",
        "llm_d_lens.monitoring.accelerator.router",
        "llm_d_lens.monitoring.gpu_driver.router",
        "llm_d_lens.monitoring.profiling.router",
    ]
    facades = [getattr(lens, name) for name in namespaces]
    for module_path in module_paths:
        module = importlib.import_module(module_path)
        routers = [
            getattr(module, "router", None),
            getattr(module, "admin_router", None),
            getattr(module, "internal_router", None),
            getattr(module, "public_router", None),
        ]
        for router in [r for r in routers if r is not None]:
            for route in router.routes:
                endpoint = getattr(route, "endpoint", None)
                if endpoint is None:
                    continue
                assert any(facade._resolve(endpoint.__name__)[1] for facade in facades), (
                    module_path,
                    endpoint.__name__,
                )
                response_model = getattr(route, "response_model", None)
                assert response_model is not dict, (module_path, endpoint.__name__, "response_model=dict")


def test_every_namespace_route_endpoint_is_exposed_and_checked(local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)
    namespaces = (
        "deploy",
        "remote_deploy",
        "clusters",
        "cluster_bootstrap",
        "storage",
        "model_cache",
        "model_services",
        "simulation",
        "external_providers",
        "configuration",
        "aic",
        "hardware",
        "administration",
        "observability",
    )

    for namespace in namespaces:
        facade = getattr(lens, namespace)
        for module_path in facade._module_paths:
            module = importlib.import_module(module_path)
            endpoint_names = set()
            for router in facade._routers(module):
                for route in router.routes:
                    endpoint = getattr(route, "endpoint", None)
                    if endpoint is None:
                        continue
                    endpoint_names.add(endpoint.__name__)
                    resolved, found, _permission, _response_model = facade._resolve(endpoint.__name__)
                    assert found, (namespace, module_path, endpoint.__name__)
                    assert resolved is not None
                    wrapper = getattr(facade, endpoint.__name__)
                    assert callable(wrapper)
                    assert inspect.iscoroutinefunction(wrapper) == inspect.iscoroutinefunction(endpoint)

                    try:
                        injected = facade._with_http_context(endpoint, {})
                    except TypeError as error:
                        assert "is required" in str(error)
                        continue
                    for name, parameter in inspect.signature(endpoint).parameters.items():
                        if parameter.default is not inspect._empty:
                            continue
                        annotation = str(parameter.annotation)
                        if annotation in {"Request", "starlette.requests.Request", "fastapi.Request"}:
                            assert isinstance(injected[name], Request)
                        elif annotation in {"BackgroundTasks", "starlette.background.BackgroundTasks"}:
                            assert isinstance(injected[name], BackgroundTasks)

            for name, value in vars(module).items():
                if name.startswith("_") or name in endpoint_names:
                    continue
                if callable(value) and not inspect.ismodule(value):
                    with pytest.raises(AttributeError):
                        getattr(facade, name)


def test_services_loader_supports_modules_functions_and_service_instances(local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)

    with lens:
        versions = lens.services.versions.describe()
        assert isinstance(versions, local_sdk.LensResponse)
        assert versions["llm_d"]
        service = lens.services.cluster.service
        clusters = service.list_clusters(include_drafts=True)
        assert isinstance(clusters, local_sdk.LensListResponse)
        assert isinstance(clusters.items, list)
        storage_service = lens.services.storage.service.default_service()
        assert callable(storage_service.list)


def test_all_sdk_namespaces_are_wired(local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)

    assert isinstance(lens.deployments, local_sdk.LocalDeployments)
    assert isinstance(lens.evaluation, local_sdk.LocalEvaluation)
    assert isinstance(lens.dashboard, local_sdk.LocalDashboard)
    for name in (
        "deploy",
        "remote_deploy",
        "clusters",
        "cluster_bootstrap",
        "storage",
        "model_cache",
        "model_services",
        "simulation",
        "external_providers",
        "configuration",
        "aic",
        "hardware",
        "administration",
        "observability",
    ):
        assert isinstance(getattr(lens, name), local_sdk.LocalModule)


@pytest.mark.asyncio
async def test_module_facade_exposes_only_route_endpoints(local_users):
    admin, _ = local_users
    lens = LensLocal(admin.username)

    hosts = await lens.clusters.list_lens_hosts()
    assert isinstance(hosts.items, list)
    assert hosts.__class__.__name__ == "LensHostsResponse"

    clusters = await lens.clusters.list_clusters(include_drafts=True)
    assert isinstance(clusters.items, list)
    assert clusters.__class__.__name__ == "ClustersResponse"

    with pytest.raises(AttributeError):
        _ = lens.clusters.Request
    with pytest.raises(AttributeError):
        _ = lens.clusters.Query


def test_http_context_resolves_field_info_defaults():
    async def endpoint(
        include_drafts: bool = Query(default=False),
        limit: int = Query(default=20, ge=1),
        token: str = Header(default=""),
        name: str = Form(default=""),
        volume_ids: Annotated[list[str], Query(default_factory=list)] = None,
    ):
        return include_drafts, limit, token, name, volume_ids

    class FakeOwner:
        def _context(self):
            return context_manager()

        def _require(self, permission):
            return None

    from contextlib import contextmanager

    @contextmanager
    def context_manager():
        yield

    module = local_sdk.LocalModule(FakeOwner(), "llm_d_lens.cluster.router")
    injected = module._with_http_context(endpoint, {})
    assert injected == {"include_drafts": False, "limit": 20, "token": "", "name": "", "volume_ids": []}

    async def required(limit: int = Query(...)):
        return limit

    with pytest.raises(TypeError, match="limit is required"):
        module._with_http_context(required, {})


def test_http_context_injection():
    async def endpoint(request: Request, background: BackgroundTasks):
        return request, background

    class FakeOwner:
        def _context(self):
            return context_manager()

        def _require(self, permission):
            return None

    from contextlib import contextmanager

    @contextmanager
    def context_manager():
        yield

    module = local_sdk.LocalModule(FakeOwner(), "llm_d_lens.cluster.router")
    injected = module._with_http_context(endpoint, {})
    assert isinstance(injected["request"], Request)
    assert isinstance(injected["background"], BackgroundTasks)


@pytest.mark.asyncio
async def test_limited_user_is_forbidden(local_users):
    _, limited = local_users
    lens = LensLocal(limited.username)

    with pytest.raises(ForbiddenError):
        await lens.clusters.list_lens_hosts()
    with pytest.raises(ForbiddenError):
        await lens.evaluation.benchmark_defaults()
    with pytest.raises(ForbiddenError):
        lens.deployments.list_executions()
    with pytest.raises(ForbiddenError):
        await lens.remote_request("/start", {})
