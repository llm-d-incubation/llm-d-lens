"""Application service for creating and operating deployment runs."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from llm_d_lens.cluster import deployment_runtime_overrides, require_active_session
from llm_d_lens.cluster import registry as cluster_registry
from llm_d_lens.cluster.deployment_source import resolve_cluster_deployment_source
from llm_d_lens.deploy.contracts import (
    DeploymentCaseStatus,
    DeploymentRunCreateRequest,
    DeploymentRunStatus,
    DeploymentStatus,
)
from llm_d_lens.deploy.plan_service import DeploymentPlanService
from llm_d_lens.deploy.run_store import JsonDeploymentRunStore
from llm_d_lens.deploy.runtime.composition import (
    RuntimeConfigurationError,
    build_deployment_service_from_environment,
)
from llm_d_lens.deploy.worker import DeploymentRunWorker
from llm_d_lens.infrastructure.hostos import current_host


def cluster_deployment_source(session, requested: dict | None = None) -> dict:
    """Resolve source settings from the selected cluster; ignore request-supplied paths."""
    cluster = cluster_registry.require_cluster(session.server_id)
    source = dict(requested or {})
    for key in ("repository", "branch", "warning"):
        source.pop(key, None)
    source.update(resolve_cluster_deployment_source(cluster))
    return source


_ACCELERATOR_KEYS = ("hardware_profile", "accelerator", "accelerator_variant", "upstream_variant")


def _deploy_accelerator(run) -> str | None:
    """The accelerator/vendor a run targets, from its provenance/configurations.

    The Deploy runtime renders a vendor-specific overlay (Intel XPU vs NVIDIA
    GPU); without this it defaults to Intel, so an NVIDIA cluster would render
    gpu.intel.com claims. Returns None when nothing recorded it (keep default).
    """
    if run is None:
        return None

    def first(mapping: object) -> str | None:
        if not isinstance(mapping, dict):
            return None
        for key in _ACCELERATOR_KEYS:
            value = mapping.get(key)
            if value:
                return str(value)
        return None

    recorded = first(getattr(run, "provenance", None))
    if recorded:
        return recorded
    for configuration in getattr(run, "source_configurations", None) or []:
        content = getattr(configuration, "content", None)
        recorded = first(getattr(configuration, "provenance", None)) or first(content)
        if recorded:
            return recorded
        guide = content.get("officialGuide") if isinstance(content, dict) else None
        recorded = first(guide) or first(guide.get("source") if isinstance(guide, dict) else None)
        if recorded:
            return recorded
        if isinstance(content, dict):
            from llm_d_lens.hardware.resolver import resolve_configuration_profile

            profile = resolve_configuration_profile(content)
            if profile:
                return profile.id
    return None


def single_case_namespace_lookup(request, case) -> tuple[str | None, str | None]:
    """Return (agentic_deployment_run_id, deployment_name) for a single-case run."""
    case_prov = case.create_request.provenance or {}
    agentic_run_id = request.provenance.get("agentic_deployment_run_id") or case_prov.get("agentic_deployment_run_id")
    deployment_name = (
        request.provenance.get("deployment_name")
        or case_prov.get("deployment_name")
        or (case.create_request.deployment_policy.value or {}).get("deployment_name")
    )
    return (
        str(agentic_run_id) if agentic_run_id else None,
        str(deployment_name) if deployment_name else None,
    )


async def resolve_bootstrapped_target_namespace(cluster_id: str, request, case) -> str | None:
    """Validate an explicit target_namespace and confirm its cluster bootstrap marker."""
    from llm_d_lens.deploy import capabilities

    case_prov = case.create_request.provenance or {}
    requested_ns = request.provenance.get("target_namespace") or case_prov.get("target_namespace")
    if isinstance(requested_ns, str) and requested_ns.strip():
        validated_ns = capabilities.validate_deployment_namespace(requested_ns.strip(), cluster_id=cluster_id)
        if await capabilities.is_bootstrap_marker_present(cluster_id, validated_ns):
            return validated_ns
    return None


def ensure_namespace_not_used_by_active_execution(store, namespace: str) -> None:
    """Reject reusing a verified namespace that already belongs to an active execution."""
    list_executions = getattr(store, "list_executions", None)
    if not callable(list_executions):
        return
    active_owner = next(
        (
            execution
            for execution in list_executions()
            if execution.namespace == namespace
            and execution.status not in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}
        ),
        None,
    )
    if active_owner is not None:
        raise ValueError(
            f"target namespace '{namespace}' is already used by an active deployment ({active_owner.execution_id})"
        )


def bind_single_case_target_namespace(run, case, verified_ns: str | None) -> None:
    """Bind a verified target namespace onto a single-case run, or strip unverified ones."""
    if verified_ns:
        run.provenance["target_namespace"] = verified_ns
        policy_value = dict(case.create_request.deployment_policy.value or {})
        policy_value["namespace_policy"] = {"exact": verified_ns}
        case.create_request.deployment_policy.value = policy_value
        if case.create_request.provenance is None:
            case.create_request.provenance = {}
        case.create_request.provenance["target_namespace"] = verified_ns
    else:
        run.provenance.pop("target_namespace", None)
        if case.create_request.provenance is not None:
            case.create_request.provenance.pop("target_namespace", None)
        policy_value = dict(case.create_request.deployment_policy.value or {})
        ns_policy = policy_value.get("namespace_policy")
        if isinstance(ns_policy, dict) and "exact" in ns_policy:
            sanitized = {key: value for key, value in ns_policy.items() if key != "exact"}
            if sanitized:
                policy_value["namespace_policy"] = sanitized
            else:
                policy_value.pop("namespace_policy", None)
            case.create_request.deployment_policy.value = policy_value


class DeploymentRunManager:
    """Compose Deploy storage, runtime selection, and background scheduling."""

    def __init__(self, store: JsonDeploymentRunStore | None = None) -> None:
        self.store = store or JsonDeploymentRunStore()
        self._default_worker: DeploymentRunWorker | None = None
        self._run_workers: dict[str, DeploymentRunWorker] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}

    def _schedule_run(self, run_id: str, worker: DeploymentRunWorker) -> None:
        existing = self._run_tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(worker.execute_run(run_id))
        self._run_tasks[run_id] = task
        task.add_done_callback(
            lambda completed, scheduled_run_id=run_id: (
                self._run_tasks.pop(scheduled_run_id, None)
                if self._run_tasks.get(scheduled_run_id) is completed
                else None
            )
        )

    def _cached_worker_session_is_active(self, run_id: str) -> bool:
        if not callable(getattr(self.store, "get_run", None)):
            return True
        run = self.store.get_run(run_id)
        if run is None:
            return True
        session_id = run.provenance.get("cluster_session_id")
        if not session_id:
            return not run.provenance.get("cluster_server_id")
        try:
            deployment_runtime_overrides(session_id)
        except ValueError as error:
            if str(error) == "cluster session is no longer active":
                return False
            raise
        return True

    @staticmethod
    def _worker_has_active_tasks(worker: object | None, run_id: str) -> bool:
        for attr in ("_active_tasks", "_retry_tasks"):
            task_map = getattr(worker, attr, None)
            if isinstance(task_map, dict) and any(not task.done() for task in (task_map.get(run_id) or ())):
                return True
        return False

    def worker_for_run(self, run_id: str) -> DeploymentRunWorker:
        cached = self._run_workers.get(run_id)
        if cached is not None:
            if not getattr(cached, "_lifecycle_error", None) and (
                self._cached_worker_session_is_active(run_id) or self.is_run_active(run_id)
            ):
                return cached
            self._run_workers.pop(run_id, None)
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        worker = self._worker(run)
        self._run_workers[run_id] = worker
        return worker

    def worker_for_local_delete(
        self, run_id: str, *, store: JsonDeploymentRunStore | None = None
    ) -> DeploymentRunWorker:
        """Return a worker for local-only record deletion without validating kubeconfig or runtime."""
        cached = self._run_workers.get(run_id)
        if (
            cached is not None
            and callable(getattr(cached, "delete_case_execution", None))
            and (store is None or getattr(cached, "_store", store) is store)
        ):
            return cached
        return DeploymentRunWorker(store if store is not None else self.store, SimpleNamespace())

    def is_run_active(self, run_id: str) -> bool:
        """Whether this process is still executing the deployment operation."""
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            return True
        return self._worker_has_active_tasks(self._run_workers.get(run_id), run_id)

    async def start_run(self, request: DeploymentRunCreateRequest, *, owner_user_id: str | None = None):
        from llm_d_lens.deploy.prepared_plans import consume, requires_prepared_plan

        if any(key.startswith("_prepared") for key in request.provenance):
            raise ValueError("Prepared execution metadata is server-owned")
        if request.prepared_plan_id:
            return await consume(self, request, owner_user_id=owner_user_id)
        if requires_prepared_plan(request.configurations):
            raise ValueError("DisaggregatedSet deployment requires a reviewed prepared plan and digest")
        session_id = request.provenance.get("cluster_session_id")
        session = None
        if session_id:
            session = require_active_session(session_id)
            requested_server_id = request.provenance.get("cluster_server_id")
            if requested_server_id and requested_server_id != session.server_id:
                raise ValueError("cluster_server_id does not match the active cluster session")
            request.provenance["cluster_server_id"] = session.server_id
            for configuration in request.configurations:
                if configuration.format != "manifest":
                    continue
                cluster_ref = configuration.provenance.get("cluster_ref")
                if not isinstance(cluster_ref, dict) or not cluster_ref.get("id"):
                    raise ValueError("manifest configuration requires a target cluster binding")
                if cluster_ref["id"] != session.server_id:
                    raise ValueError(
                        "saved YAML belongs to a different cluster; select its original cluster before deploying"
                    )
        else:
            raise ValueError("deployment requires an explicit active cluster session")
        deployment_source = request.provenance.get("deployment_source")
        if deployment_source is not None and not isinstance(deployment_source, dict):
            raise ValueError("provenance.deployment_source must be an object")
        # A connected cluster is the authority for the llm-d checkout used by
        # every deployment path. Do not clone a second copy from an artifact's
        # guide_source or reconstruct a cache path.
        request.provenance["deployment_source"] = cluster_deployment_source(session, deployment_source)
        run = DeploymentPlanService(self.store).build_run(request)
        async with self.verified_namespace_binding(run, request, session.server_id, owner_user_id):
            run = self.store.create_run(run)
            if owner_user_id:
                from llm_d_lens.db.dao.deployment_batch import DeploymentBatchDao

                DeploymentBatchDao().set_owner(run.id, owner_user_id=owner_user_id)
            worker = self._worker(run)
            self._run_workers[run.id] = worker
        self._schedule_run(run.id, worker)
        return run

    @contextlib.asynccontextmanager
    async def verified_namespace_binding(self, run, request, server_id: str, owner_user_id: str | None):
        """Pin a single-case run to its verified target namespace; restore the binding if the body fails."""
        if len(run.cases) != 1:
            yield
            return
        from llm_d_lens.deploy import capabilities

        case = run.cases[0]
        agentic_run_id_str, deployment_name_str = single_case_namespace_lookup(request, case)
        consumed_ns = capabilities.consume_verified_namespace(
            server_id,
            owner_user_id=owner_user_id,
            agentic_deployment_run_id=agentic_run_id_str,
            deployment_name=deployment_name_str,
            prepared_plan_id=request.prepared_plan_id,
        )
        try:
            verified_ns = consumed_ns
            if verified_ns is None:
                verified_ns = await resolve_bootstrapped_target_namespace(server_id, request, case)
            if verified_ns:
                ensure_namespace_not_used_by_active_execution(self.store, verified_ns)
            bind_single_case_target_namespace(run, case, verified_ns)
            yield
        except BaseException:
            if consumed_ns:
                capabilities.record_verified_namespace(
                    server_id,
                    consumed_ns,
                    owner_user_id=owner_user_id,
                    agentic_deployment_run_id=agentic_run_id_str,
                    deployment_name=deployment_name_str,
                    prepared_plan_id=request.prepared_plan_id,
                )
            raise

    def rebind_cluster_session(self, run_id: str, session_id: str) -> None:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        session = require_active_session(session_id)
        bound_server_id = run.provenance.get("cluster_server_id")
        if bound_server_id and bound_server_id != session.server_id:
            raise ValueError("active cluster session is not connected to this deployment's original cluster")
        run.provenance["cluster_session_id"] = session.id
        run.provenance["cluster_server_id"] = session.server_id
        self.store.save_run(run)
        self._run_workers.pop(run_id, None)

    def resume_run(self, run_id: str) -> None:
        """Reschedule a persisted run interrupted before it created an execution."""
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("deployment run not found")
        interrupted = [
            case for case in run.cases if case.status == DeploymentCaseStatus.RENDERING and case.execution_id is None
        ]
        if run.status == DeploymentRunStatus.RUNNING and interrupted:
            for case in interrupted:
                case.status = DeploymentCaseStatus.QUEUED
            run.status = DeploymentRunStatus.QUEUED
            self.store.save_run(run)
        if run.status != DeploymentRunStatus.QUEUED:
            return
        worker = self.worker_for_run(run_id)
        self._schedule_run(run_id, worker)

    def reconcile_interrupted_cancellation(self, run_id: str) -> None:
        """Finish cancellation work that was interrupted before an execution existed."""
        run = self.store.get_run(run_id)
        if run is None or run.status != DeploymentRunStatus.CANCELLING:
            return
        for case in run.cases:
            if case.execution_id is None and case.status in {
                DeploymentCaseStatus.QUEUED,
                DeploymentCaseStatus.RENDERING,
                DeploymentCaseStatus.CANCELLING,
            }:
                case.status = DeploymentCaseStatus.CANCELLED
        active = {
            DeploymentCaseStatus.QUEUED,
            DeploymentCaseStatus.RENDERING,
            DeploymentCaseStatus.DEPLOYING,
            DeploymentCaseStatus.CANCELLING,
        }
        if not any(case.status in active for case in run.cases):
            run.status = DeploymentRunStatus.CANCELLED
            run.finished_at = datetime.now(UTC)
        self.store.save_run(run)

    @staticmethod
    def has_available_source(run) -> bool:
        """Return whether a persisted run's runtime can still be reconstructed."""
        source = run.provenance.get("deployment_source")
        repository = source.get("resolved_repository") if isinstance(source, dict) else None
        return not repository or Path(str(repository)).is_dir()

    def _rebind_session_for_registered_cluster(self, run, cluster_server_id: str) -> dict[str, str] | None:
        from llm_d_lens.cluster.errors import ClusterOverviewError
        from llm_d_lens.cluster.service import get_cluster_session

        try:
            _cluster, session = get_cluster_session(cluster_server_id)
        except (ClusterOverviewError, ValueError, OSError):
            return None
        if getattr(session, "server_id", None) != cluster_server_id:
            return None
        try:
            runtime_overrides = deployment_runtime_overrides(session.id)
        except ValueError as error:
            if str(error) == "cluster session is no longer active":
                return None
            raise
        run.provenance["cluster_session_id"] = session.id
        run_id = getattr(run, "id", None)
        if (
            run_id
            and callable(getattr(self.store, "get_run", None))
            and callable(getattr(self.store, "save_run", None))
        ):
            persisted = self.store.get_run(run_id)
            if persisted is not None:
                persisted.provenance["cluster_session_id"] = session.id
                self.store.save_run(persisted)
                self._run_workers.pop(run_id, None)
        return runtime_overrides

    def _resolve_run_runtime_overrides(self, run) -> tuple[dict[str, str], str | None]:
        if run is None:
            return {}, None
        session_id = run.provenance.get("cluster_session_id")
        cluster_server_id = run.provenance.get("cluster_server_id")
        if session_id:
            try:
                return deployment_runtime_overrides(session_id), None
            except ValueError as error:
                if str(error) != "cluster session is no longer active":
                    raise
        elif not cluster_server_id:
            return {}, None

        if cluster_server_id:
            recovered = self._rebind_session_for_registered_cluster(run, str(cluster_server_id))
            if recovered is not None:
                return recovered, None

        return (
            {"KUBECONFIG": current_host().null_device},
            "cluster session is no longer active; reconnect the original cluster before changing this deployment",
        )

    def _worker(self, run=None) -> DeploymentRunWorker:
        source = run.provenance.get("deployment_source") if run else None
        resolved_repository = source.get("resolved_repository") if isinstance(source, dict) else None
        if run is not None and not resolved_repository:
            cluster_server_id = run.provenance.get("cluster_server_id")
            if cluster_server_id:
                from llm_d_lens.cluster.errors import ClusterOverviewError

                try:
                    resolved_repository = cluster_deployment_source(
                        SimpleNamespace(server_id=cluster_server_id),
                        source if isinstance(source, dict) else None,
                    )["resolved_repository"]
                except ClusterOverviewError:
                    resolved_repository = None
        runtime_overrides, lifecycle_error = self._resolve_run_runtime_overrides(run)
        accelerator = _deploy_accelerator(run)
        accelerator_env = {"PRISM_DEPLOY_ACCELERATOR": accelerator} if accelerator else {}
        # The cluster's saved proxy configuration is authoritative for anything
        # this run deploys; resolve_proxy_env already falls back to this backend
        # process's environment for auto/unknown clusters.
        proxy_env: dict[str, str] = {}
        cluster_server_id = run.provenance.get("cluster_server_id") if run else None
        if cluster_server_id:
            try:
                from llm_d_lens.cluster.service import resolve_proxy_env

                proxy_env = resolve_proxy_env(str(cluster_server_id))
            except Exception:  # pragma: no cover - a bad proxy lookup must not block deploys
                proxy_env = {}
        if not resolved_repository:
            if runtime_overrides:
                return self._build_worker({**runtime_overrides, **accelerator_env, **proxy_env}, lifecycle_error)
            return self._build_worker({**accelerator_env, **proxy_env} or None)
        return self._build_worker(
            {"LLM_D_ROOT": str(resolved_repository), **runtime_overrides, **accelerator_env, **proxy_env},
            lifecycle_error,
        )

    def _build_worker(
        self, environment: dict[str, str] | None, lifecycle_error: str | None = None
    ) -> DeploymentRunWorker:
        configured = build_deployment_service_from_environment(environment)
        if configured is None:
            raise RuntimeConfigurationError("local Deploy runtime is disabled")
        service, _catalog = configured
        return DeploymentRunWorker(self.store, service, lifecycle_error)


deployment_run_manager = DeploymentRunManager()
