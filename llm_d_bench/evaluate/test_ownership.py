"""Regression coverage for competing backend processes and premature cleanup."""

import asyncio
import importlib
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from llm_d_bench.evaluate import ownership

router = importlib.import_module("llm_d_bench.evaluate.router")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(router, "_results_root", tmp_path / "results")
    for name in ("_tasks", "_workflow_tasks", "_active_workflows"):
        monkeypatch.setattr(router, name, {})
    return tmp_path


def competitor(kind, identifier):
    # A fresh interpreter, not a second asyncio task sharing the same dictionary.
    code = """
import sys
from llm_d_bench.evaluate.ownership import evaluation_lock
with evaluation_lock(sys.argv[1], sys.argv[2]) as acquired:
    print('owned' if acquired else 'busy', flush=True)
    if acquired:
        sys.stdin.readline()
"""
    return subprocess.Popen(
        [sys.executable, "-c", code, kind, identifier],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )


def test_independent_process_exclusion_and_crash_release(isolated):
    process = competitor("workflow", "same-workflow")
    try:
        assert process.stdout.readline().strip() == "owned"
        with ownership.evaluation_lock("workflow", "same-workflow") as acquired:
            assert not acquired
        with ownership.evaluation_lock("workflow", "other-workflow") as acquired:
            assert acquired
        process.kill()
        process.wait(timeout=10)
        with ownership.evaluation_lock("workflow", "same-workflow") as acquired:
            assert acquired
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_local_control_retains_lock_until_control_finishes(isolated):
    with ownership.evaluation_lock("workflow", "task") as acquired:
        assert acquired
        with ownership.evaluation_lock("workflow", "task") as duplicate:
            assert not duplicate
        with ownership.evaluation_lock("workflow", "task", allow_local=True) as control:
            assert control
    with ownership.evaluation_lock("workflow", "task") as acquired:
        assert acquired


@pytest.mark.asyncio
async def test_second_startup_cannot_interrupt_live_workflow_or_launch_duplicate(isolated, monkeypatch):
    workflow_id, run_id = str(uuid4()), str(uuid4())
    router._save(
        {
            "kind": "workflow",
            "id": workflow_id,
            "status": "running",
            "cases": [
                {"id": "guide-1-1", "status": "benchmarking", "evaluation_run_id": run_id},
            ],
        }
    )
    router._save({"kind": "benchmark", "id": run_id, "status": "running", "evaluation_workflow_id": workflow_id})
    process = competitor("workflow", workflow_id)
    try:
        assert process.stdout.readline().strip() == "owned"
        await router.reconcile_evaluate_runs()
        assert router._get("benchmark", run_id)["status"] == "running"
        assert router._get("workflow", workflow_id)["status"] == "running"
        assert not router._workflow_tasks
        # Every execution entry is fenced, not only the startup scanner.
        await router._execute_evaluation(workflow_id)
        await router._execute_workflow(workflow_id)
        assert router._get("workflow", workflow_id)["status"] == "running"
        with pytest.raises(router.HTTPException) as error:
            await router._cancel_evaluation(router._get("workflow", workflow_id))
        assert error.value.status_code == 409
        assert router._get("benchmark", run_id)["status"] == "running"
    finally:
        process.communicate("release\n", timeout=10)


@pytest.mark.asyncio
async def test_second_startup_preserves_standalone_benchmark(isolated):
    run_id = str(uuid4())
    router._save({"kind": "benchmark", "id": run_id, "status": "running"})
    process = competitor("benchmark", run_id)
    try:
        assert process.stdout.readline().strip() == "owned"
        await router.reconcile_evaluate_runs()
        await router._execute(run_id)
        assert router._get("benchmark", run_id)["status"] == "running"
    finally:
        process.communicate("release\n", timeout=10)


@pytest.mark.parametrize("status", ["running", "queued", "cancelling"])
def test_cleanup_rejects_other_active_benchmark(isolated, status):
    run_id = str(uuid4())
    router._save({"kind": "benchmark", "id": run_id, "status": status, "deployment_execution_id": "shared-deployment"})
    with pytest.raises(ValueError, match="still used by benchmark"):
        router._ensure_benchmark_consumers_finished("shared-deployment")
    router._ensure_benchmark_consumers_finished("unrelated-deployment")
    record = router._get("benchmark", run_id)
    record["status"] = "succeeded"
    router._save(record)
    router._ensure_benchmark_consumers_finished("shared-deployment")


def test_cleanup_rejects_terminal_record_with_live_executor(isolated):
    run_id = str(uuid4())
    router._save(
        {"kind": "benchmark", "id": run_id, "status": "failed", "deployment_execution_id": "shared-deployment"}
    )
    with ownership.evaluation_lock("benchmark", run_id), pytest.raises(ValueError, match="still used by benchmark"):
        router._ensure_benchmark_consumers_finished("shared-deployment")


@pytest.mark.asyncio
async def test_execution_lock_covers_async_cleanup_and_cancellation(isolated):
    cleaning, release = asyncio.Event(), asyncio.Event()

    @ownership.owned_execution("workflow")
    async def execute(identifier):
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    task = asyncio.create_task(execute("cleanup"))
    await asyncio.sleep(0)
    task.cancel()
    await cleaning.wait()
    process = competitor("workflow", "cleanup")
    try:
        stdout, _ = process.communicate(timeout=10)
        assert stdout.strip() == "busy"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    with ownership.evaluation_lock("workflow", "cleanup") as acquired:
        assert acquired


@pytest.mark.asyncio
async def test_delayed_executor_does_not_replay_completed_workflow(isolated):
    workflow_id = str(uuid4())
    router._save({"kind": "workflow", "id": workflow_id, "status": "succeeded"})
    await router._execute_evaluation(workflow_id)
    await router._execute_workflow(workflow_id)
    assert router._get("workflow", workflow_id)["status"] == "succeeded"


def test_deployment_cleanup_excludes_new_consumers(isolated):
    with (
        router._deployment_cleanup_guard("deployment"),
        ownership.evaluation_lock("deployment", "deployment", shared=True) as acquired,
    ):
        assert not acquired
    with ownership.evaluation_lock("deployment", "deployment", shared=True) as acquired:
        assert acquired
        with ownership.evaluation_lock("deployment", "deployment", shared=True) as second:
            assert second
        with (
            pytest.raises(ValueError, match="active benchmark"),
            router._deployment_cleanup_guard("deployment"),
        ):
            pytest.fail("cleanup entered while a consumer holds the deployment")


@pytest.mark.asyncio
async def test_wait_for_benchmark_waits_for_runner_cleanup(isolated):
    run_id = str(uuid4())
    router._save({"kind": "benchmark", "id": run_id, "status": "succeeded"})
    with ownership.evaluation_lock("benchmark", run_id):
        waiter = asyncio.create_task(router._wait_for_benchmark(run_id))
        await asyncio.sleep(0)
        assert not waiter.done()
    result = await asyncio.wait_for(waiter, 5)
    assert result["status"] == "succeeded"
