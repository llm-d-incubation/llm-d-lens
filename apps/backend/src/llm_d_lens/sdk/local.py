"""Local Lens SDK: trusted in-process calls with an explicit user id."""

from __future__ import annotations

import importlib
import inspect
import os
from functools import wraps
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from fastapi.params import FieldInfo
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import PydanticUndefined
from starlette.background import BackgroundTasks
from starlette.requests import Request
from starlette.responses import Response

from llm_d_lens.auth.context import LensContext, context_principal
from llm_d_lens.auth.policy import authorize
from llm_d_lens.auth.routes import resolve_route_permission
from llm_d_lens.core.exceptions import ForbiddenError, UnauthenticatedError
from llm_d_lens.deploy.executions import (
    create_local_run,
    delete_local_execution,
    get_local_execution,
    get_local_run,
    list_local_executions,
    wait_local_run,
)
from llm_d_lens.evaluation.router import (
    benchmark_defaults,
    cancel_evaluation_case,
    cancel_run,
    cancel_workflow_run,
    create_evaluation,
    create_run,
    create_workflow_run,
    delete_benchmark_run,
    delete_workflow_run,
    estimate_benchmark_timing,
    get_run,
    get_workflow_run,
    get_workflow_run_details,
    list_runs,
    list_workflow_runs,
    retry_workflow_run,
)


class LensResponse(BaseModel):
    """Generic Lens class for endpoints without a specific response model."""

    model_config = ConfigDict(extra="allow")

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


class LensListResponse(BaseModel):
    items: list[Any] = Field(default_factory=list)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.items[key]
        return getattr(self, key)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


class LocalDeployments:
    def __init__(self, owner: LensLocal) -> None:
        self._owner = owner

    def list_executions(self, **kwargs: Any):
        with self._owner._context():
            return LocalModule._normalize(list_local_executions(**kwargs), None)

    def get_execution(self, execution_id: str):
        with self._owner._context():
            return get_local_execution(execution_id)

    async def create_run(self, request: Any):
        with self._owner._context():
            return await create_local_run(request)

    async def get_run(self, run_id: str):
        with self._owner._context():
            return LocalModule._normalize(await get_local_run(run_id), None)

    async def wait_run(self, run_id: str, *, timeout: float = 1800, poll_interval: float = 5):
        with self._owner._context():
            return await wait_local_run(run_id, timeout=timeout, poll_interval=poll_interval)

    async def delete_execution(self, execution_id: str, *, delete_namespace: bool = True) -> None:
        with self._owner._context():
            await delete_local_execution(execution_id, delete_namespace=delete_namespace)


class LocalModule:
    """Lazy facade over a module's in-process Python callables.

    Public methods are the router endpoint functions. Permission comes from the
    same route registry used by the REST API; the current Principal comes from
    the ambient LensContext.
    """

    def __init__(self, owner: LensLocal, *module_paths: str) -> None:
        self._owner = owner
        self._module_paths = module_paths

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        func, found, permission, response_model = self._resolve(name)
        if not found:
            raise AttributeError(f"{name} is not exposed by {', '.join(self._module_paths)}")
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_call(*args, **kwargs):
                with self._owner._context():
                    self._authorize(permission)
                    kwargs = self._with_http_context(func, kwargs)
                    return self._normalize(await func(*args, **kwargs), response_model)

            return async_call

        @wraps(func)
        def sync_call(*args, **kwargs):
            with self._owner._context():
                self._authorize(permission)
                kwargs = self._with_http_context(func, kwargs)
                return self._normalize(func(*args, **kwargs), response_model)

        return sync_call

    def _resolve(self, name: str):
        for module_path in self._module_paths:
            module = importlib.import_module(module_path)
            for router in self._routers(module):
                for route in router.routes:
                    endpoint = getattr(route, "endpoint", None)
                    if getattr(endpoint, "__name__", "") != name:
                        continue
                    for method in getattr(route, "methods", ()) or ():
                        found, permission = resolve_route_permission(method, route.path)
                        if found:
                            return endpoint, True, permission, getattr(route, "response_model", None)
        return None, False, None, None

    @staticmethod
    def _routers(module):
        for attr in ("router", "admin_router", "internal_router", "public_router"):
            router = getattr(module, attr, None)
            if router is not None:
                yield router

    @staticmethod
    def _normalize(result, response_model):
        if result is None or isinstance(result, Response):
            return result
        if response_model is not None and response_model is not dict:
            try:
                if isinstance(result, response_model):
                    return result
            except TypeError:
                pass
            return TypeAdapter(response_model).validate_python(result)
        if isinstance(result, dict):
            return LensResponse.model_validate(result)
        if isinstance(result, list):
            return LensListResponse(items=result)
        return result

    def _authorize(self, permission: str | None) -> None:
        if permission == "":
            self._owner._require_authenticated()
        elif permission:
            self._owner._require(permission)

    def _with_http_context(self, func, kwargs: dict[str, Any]) -> dict[str, Any]:
        injected = dict(kwargs)
        try:
            hints = get_type_hints(func, include_extras=True)
        except Exception:  # pragma: no cover - defensive for unusual forward refs
            hints = {}
        for name, parameter in inspect.signature(func).parameters.items():
            if name in injected:
                continue
            annotation = hints.get(name, parameter.annotation)
            field_info = parameter.default if isinstance(parameter.default, FieldInfo) else None
            if get_origin(annotation) is Annotated:
                field_info = next(
                    (item for item in get_args(annotation)[1:] if isinstance(item, FieldInfo)),
                    field_info,
                )
            if field_info is not None:
                if field_info.default is not PydanticUndefined:
                    injected[name] = field_info.default
                elif field_info.default_factory is not None:
                    injected[name] = field_info.default_factory()
                else:
                    raise TypeError(f"{name} is required for {getattr(func, '__name__', 'SDK call')}")
                continue
            if parameter.default is not inspect._empty:
                continue
            annotation_name = getattr(annotation, "__name__", "") or str(annotation)
            if annotation is Request or annotation_name in {"Request", "starlette.requests.Request", "fastapi.Request"}:
                injected[name] = self._request()
            elif annotation is BackgroundTasks or annotation_name in {
                "BackgroundTasks",
                "starlette.background.BackgroundTasks",
            }:
                injected[name] = BackgroundTasks()
        return injected

    @staticmethod
    def _request() -> Request:
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("local", 0),
                "server": ("local", 0),
            }
        )
        request.state.principal = context_principal()
        return request


class _ServiceModule:
    """Trusted lazy access to a lower-level service/helper module."""

    def __init__(self, owner: LensLocal, path: str) -> None:
        self._owner = owner
        self._path = path

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        module = importlib.import_module(self._path)
        attr = getattr(module, name, None)
        if attr is None:
            factory = getattr(module, "default_service", None)
            if callable(factory):
                attr = getattr(factory(), name, None)
        if attr is None:
            raise AttributeError(f"{self._path}.{name} is not available")
        if inspect.ismodule(attr):
            return _ServiceModule(self._owner, f"{self._path}.{name}")
        if inspect.isclass(attr):
            return attr
        if callable(attr):
            return self._wrap(attr)
        return attr

    def _wrap(self, func):
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_call(*args, **kwargs):
                with self._owner._context():
                    self._owner._require_authenticated()
                    return LocalModule._normalize(await func(*args, **kwargs), None)

            return async_call

        @wraps(func)
        def sync_call(*args, **kwargs):
            with self._owner._context():
                self._owner._require_authenticated()
                return LocalModule._normalize(func(*args, **kwargs), None)

        return sync_call


class _ServiceRoot:
    def __init__(self, owner: LensLocal) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> _ServiceModule:
        if name.startswith("__"):
            raise AttributeError(name)
        return _ServiceModule(self._owner, f"llm_d_lens.{name}")


class LocalDashboard:
    def __init__(self, owner: LensLocal) -> None:
        self._owner = owner

    async def _call(self, method, *args, **kwargs):
        with self._owner._context():
            self._owner._require("cluster:cluster:read")
            return await method(context_principal(), *args, **kwargs)

    async def overview(self, *, cluster_id: str | None = None, refresh: bool = False):
        from llm_d_lens.dashboard.service import default_service  # noqa: PLC0415

        return await self._call(default_service().welcome_overview, cluster_id=cluster_id, refresh=refresh)

    async def alerts(
        self,
        *,
        cluster_id: str | None = None,
        severity: str | None = None,
        category=None,
        search: str = "",
        cursor: str | None = None,
        limit: int = 25,
    ):
        from llm_d_lens.dashboard.service import default_service  # noqa: PLC0415

        return await self._call(
            default_service().alerts,
            cluster_id=cluster_id,
            severity=severity,
            category=category,
            search=search,
            cursor=cursor,
            limit=limit,
        )


class LocalEvaluation:
    def __init__(self, owner: LensLocal) -> None:
        self._owner = owner

    async def _call(self, permission: str, func, *args, **kwargs):
        with self._owner._context():
            self._owner._require(permission)
            return LocalModule._normalize(await func(*args, **kwargs), None)

    async def benchmark_defaults(self):
        return await self._call("evaluate:run:read", benchmark_defaults)

    async def estimate_timing(self, spec):
        return await self._call("evaluate:run:read", estimate_benchmark_timing, spec)

    async def create_run(self, request):
        return await self._call("evaluate:run:create", create_run, request)

    async def list_runs(self):
        return await self._call("evaluate:run:read", list_runs)

    async def get_run(self, run_id: str):
        return await self._call("evaluate:run:read", get_run, run_id)

    async def cancel_run(self, run_id: str):
        return await self._call("evaluate:run:cancel", cancel_run, run_id)

    async def delete_run(self, run_id: str):
        return await self._call("evaluate:run:delete", delete_benchmark_run, run_id)

    async def create_workflow_run(self, request):
        return await self._call("evaluate:workflow:create", create_workflow_run, request)

    async def create_evaluation(self, request):
        return await self._call("evaluate:workflow:create", create_evaluation, request)

    async def list_workflow_runs(self):
        return await self._call("evaluate:run:read", list_workflow_runs)

    async def get_workflow_run(self, workflow_id: str):
        return await self._call("evaluate:run:read", get_workflow_run, workflow_id)

    async def get_workflow_run_details(self, workflow_id: str):
        return await self._call("evaluate:run:read", get_workflow_run_details, workflow_id)

    async def retry_workflow_run(self, workflow_id: str):
        return await self._call("evaluate:run:retry", retry_workflow_run, workflow_id)

    async def cancel_workflow_run(self, workflow_id: str):
        return await self._call("evaluate:run:cancel", cancel_workflow_run, workflow_id)

    async def cancel_evaluation_case(self, workflow_id: str, case_id: str):
        return await self._call("evaluate:run:cancel", cancel_evaluation_case, workflow_id, case_id)

    async def delete_workflow_run(self, workflow_id: str):
        return await self._call("evaluate:run:delete", delete_workflow_run, workflow_id)


class LensLocal:
    """Trusted in-process Lens API.

    The caller supplies a Lens username (or a user id). The identity is loaded
    into a real Principal; service/domain layers enforce permissions, cluster
    scope, ownership and audit.
    """

    def __init__(self, username: str | None = None, *, user_id: str | None = None) -> None:
        if username is not None and user_id is not None:
            raise ValueError("pass username or user_id, not both")
        if username is None and user_id is None:
            raise ValueError("username or user_id is required")
        self.username = username.strip() if username else None
        self._user_id = user_id
        context = self._context()
        self.user_id = context.principal.user_id
        self.principal = context.principal
        self.services = _ServiceRoot(self)
        self.deployments = LocalDeployments(self)
        self.deploy = LocalModule(self, "llm_d_lens.deploy.router")
        self.remote_deploy = LocalModule(self, "llm_d_lens.deploy.remote_deploy")
        self.evaluation = LocalEvaluation(self)
        self.dashboard = LocalDashboard(self)
        self.clusters = LocalModule(self, "llm_d_lens.cluster.router")
        self.cluster_bootstrap = LocalModule(self, "llm_d_lens.cluster.bootstrap.router")
        self.storage = LocalModule(self, "llm_d_lens.storage.router")
        self.model_cache = LocalModule(self, "llm_d_lens.model_cache.router")
        self.model_services = LocalModule(self, "llm_d_lens.model_service.router")
        self.accelerator = LocalModule(self, "llm_d_lens.monitoring.accelerator.router")
        self.cluster_stack = LocalModule(self, "llm_d_lens.monitoring.cluster_stack.router")
        self.gpu_driver = LocalModule(self, "llm_d_lens.monitoring.gpu_driver.router")
        self.profiling = LocalModule(self, "llm_d_lens.monitoring.profiling.router")
        self.deployment_monitoring = LocalModule(self, "llm_d_lens.monitoring.deployment.router")
        self.system = LocalModule(self, "llm_d_lens.db.system_router")
        self.simulation = LocalModule(self, "llm_d_lens.simulation.router")
        self.external_providers = LocalModule(self, "llm_d_lens.ai_providers.router")
        self.configuration = LocalModule(self, "llm_d_lens.configuration.router")
        self.aic = LocalModule(self, "llm_d_lens.aic.router")
        self.hardware = LocalModule(self, "llm_d_lens.hardware.router")
        self.administration = LocalModule(self, "llm_d_lens.auth.router", "llm_d_lens.versions.router")
        self.observability = LocalModule(
            self,
            "llm_d_lens.monitoring.cluster_stack.router",
            "llm_d_lens.monitoring.deployment.router",
            "llm_d_lens.monitoring.accelerator.router",
            "llm_d_lens.monitoring.gpu_driver.router",
            "llm_d_lens.monitoring.profiling.router",
        )

    async def remote_request(self, path: str, body: dict[str, Any] | None = None):
        """Call a remote-deploy operation through the in-process endpoint."""
        import json

        from starlette.responses import JSONResponse

        from llm_d_lens.deploy.remote_deploy import remote_operation

        payload = json.dumps(body or {}).encode()
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/v1/remote-deploy{path}",
            "raw_path": f"/api/v1/remote-deploy{path}".encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("local", 0),
            "server": ("local", 0),
        }
        request = Request(scope, receive)
        route_path = path if path.startswith("/api/") else f"/api/v1/remote-deploy{path}"
        found, permission = resolve_route_permission("POST", route_path)
        if not found:
            raise ForbiddenError(f"{route_path} is not registered for authorization")
        with self._context():
            if permission:
                self._require(permission)
            else:
                self._require_authenticated()
            result = await remote_operation(request)
        if isinstance(result, JSONResponse):
            return LocalModule._normalize(json.loads(result.body), None)
        return LocalModule._normalize(result, None)

    def __enter__(self) -> LensLocal:
        self._entered_context = LensContext(self.principal)
        self._entered_context.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if getattr(self, "_entered_context", None) is not None:
            self._entered_context.__exit__(exc_type, exc, tb)
            self._entered_context = None

    @classmethod
    def from_env(cls) -> LensLocal:
        username = os.environ.get("LENS_USERNAME", "").strip()
        if username:
            return cls(username)
        user_id = os.environ.get("LENS_USER_ID", "").strip()
        if user_id:
            return cls(user_id=user_id)
        raise ValueError("LENS_USERNAME or LENS_USER_ID is required")

    def _require_authenticated(self) -> None:
        if context_principal() is None:
            raise UnauthenticatedError("in-process principal context is required")

    def _require(self, permission: str) -> None:
        principal = context_principal()
        if principal is None:
            raise UnauthenticatedError("in-process principal context is required")
        if not authorize(principal, permission).allowed:
            raise ForbiddenError(f"{permission} is required")

    def _context(self) -> LensContext:
        if self.username is not None:
            return LensContext.from_username(self.username)
        return LensContext.from_user_id(self._user_id or "")
