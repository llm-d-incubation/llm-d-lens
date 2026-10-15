"""Polling must not finalize executions while their creator still owns them."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from llm_d_lens.deploy.contracts import ConfigurationArtifact, DeploymentCreateRequest, DeploymentStatus
from llm_d_lens.deploy.providers.guide_adapter import GuideDefinition, ValidationResult
from llm_d_lens.deploy.providers.guide_catalog import GuideCatalog
from llm_d_lens.deploy.service import ExecutionLogArchive, GuideAdapterDeploymentService
from tests.unit.deploy.providers.test_storage_mount import cached_model as cached_model
from tests.unit.deploy.test_bundle_evidence import EvidenceAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("deploy_fails", [False, True])
async def test_refresh_during_creation_does_not_fail_missing_resources(tmp_path, deploy_fails, cached_model):
    entered = asyncio.Event()
    release = asyncio.Event()

    class DelayedAdapter(EvidenceAdapter):
        async def deploy(self, artifact, context):
            entered.set()
            await release.wait()
            if deploy_fails:
                raise RuntimeError("manifest apply rejected")
            return await super().deploy(artifact, context)

        async def readiness(self, context):
            if not release.is_set() or deploy_fails:
                return ValidationResult(False, reasons=['deployments.apps "modelserver" not found'])
            return ValidationResult(True)

    catalog = GuideCatalog([DelayedAdapter()])
    creator = GuideAdapterDeploymentService(catalog, lambda _: "ns", ExecutionLogArchive(tmp_path))
    poller = GuideAdapterDeploymentService(catalog, lambda _: "ns", ExecutionLogArchive(tmp_path))
    request = DeploymentCreateRequest(
        provenance={"cluster_server_id": "cluster"},
        configuration_artifacts=[
            ConfigurationArtifact(
                schema_version="v1",
                kind="helm-values",
                provider_ref="optimized-baseline",
                media_type="application/json",
                checksum="hash",
                content='{"model":{"name":"org/model"},"runtime":{"storageVolumeId":"volume"}}',
            )
        ],
    )
    initial = []
    task = asyncio.create_task(creator.create(request, on_initial_execution=initial.append))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        refreshed = await poller.refresh_diagnostics(initial[0])
        assert refreshed.status == DeploymentStatus.DEPLOYING
    finally:
        release.set()
        final = await task
    assert final.status == (DeploymentStatus.FAILED if deploy_fails else DeploymentStatus.READY)
    # Once creation settles, normal reconciliation must resume, including after errors.
    refreshed = await poller.refresh_diagnostics(final)
    assert refreshed.status == final.status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    ["baseline-vllm", "optimized-baseline", "pd-disaggregation", "precise-prefix-cache-routing", "tiered-prefix-cache"],
)
@pytest.mark.parametrize("content", ["{}", '{"model":{"name":"missing/model"},"runtime":{"storageVolumeId":"volume"}}'])
async def test_all_providers_reject_uncached_models_before_rendering(tmp_path, cached_model, provider, content):
    class CacheRequiredAdapter(EvidenceAdapter):
        def discover(self):
            return GuideDefinition(provider, "saved", "hash", "stable")

    adapter = CacheRequiredAdapter()
    adapter.render = AsyncMock()
    adapter.deploy = AsyncMock()
    creator = GuideAdapterDeploymentService(GuideCatalog([adapter]), lambda _: "ns", ExecutionLogArchive(tmp_path))
    request = DeploymentCreateRequest(
        provenance={"cluster_server_id": "cluster"},
        configuration_artifacts=[
            ConfigurationArtifact(
                schema_version="v1",
                kind="helm-values",
                provider_ref=provider,
                media_type="application/json",
                checksum="hash",
                content=content,
            )
        ],
    )
    execution = await creator.create(request)
    assert execution.status == DeploymentStatus.FAILED
    assert "Model Cache" in execution.diagnostics.value["reason"]
    adapter.render.assert_not_awaited()
    adapter.deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_failure_preserves_reason_on_case(tmp_path):
    from types import SimpleNamespace

    from llm_d_lens.deploy.contracts import (
        DeployableConfiguration,
        DeploymentCaseStatus,
        DeploymentRun,
        VersionedPayload,
    )
    from llm_d_lens.deploy.run_store import JsonDeploymentRunStore
    from llm_d_lens.deploy.worker import DeploymentRunWorker
    from tests.unit.deploy.test_deployment_management import _case, _execution

    store = JsonDeploymentRunStore(tmp_path)
    execution = _execution("execution-1", DeploymentStatus.DEPLOYING, "ns")
    case = _case("run-1", 0, execution.execution_id, DeploymentCaseStatus.DEPLOYING)
    run = DeploymentRun(
        id="run-1",
        cases=[case],
        source_configurations=[
            DeployableConfiguration(
                type="optimized-baseline", format="helm", provider_ref="optimized-baseline", checksum="hash"
            )
        ],
    )
    store.create_run(run)
    store.save_execution(execution)

    async def refresh(current):
        return current.model_copy(
            update={
                "status": DeploymentStatus.FAILED,
                "diagnostics": VersionedPayload(
                    schema_version="deploy-diagnostics.v1",
                    value={"readiness": ["modelserver exceeded progress deadline"]},
                ),
            }
        )

    await DeploymentRunWorker(store, SimpleNamespace(refresh_diagnostics=refresh)).refresh_run_executions(run.id)
    failed = store.get_case(run.id, case.id)
    assert failed.status == DeploymentCaseStatus.FAILED
    assert failed.failure is not None
    assert "modelserver exceeded progress deadline" in failed.failure.detail
