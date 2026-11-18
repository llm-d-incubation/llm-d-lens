"""Selection-aware dispatch, independent of legacy AIC HTTP routes."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import redirect_stdout, suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import TypeAdapter

from llm_d_lens.infrastructure.process import spawn
from llm_d_lens.infrastructure.storage.paths import storage_path

from .blis import RuntimeProfile
from .models import (
    BatchResult,
    EffectiveConfiguration,
    EstimateRequest,
    Evaluation,
    ExecutionStatus,
    OptimizeRequest,
    Problem,
    ToolId,
    ToolSupport,
)


async def _isolated_aisimulate(request: EstimateRequest | OptimizeRequest, operation: str):
    if os.name != "posix":
        raise ValueError("Bounded exact evaluation requires POSIX process groups")
    encoded = request.model_dump_json().encode()
    if len(encoded) > 1024 * 1024:
        raise ValueError("Estimate input exceeds the worker input limit")
    launch = asyncio.create_task(
        spawn(
            [sys.executable, "-m", "llm_d_lens.estimate_tools.service", operation],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "HOME": str(Path.home()),
                "LANG": "C.UTF-8",
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    )
    readers: list[asyncio.Task] = []

    async def collect(process):
        output = bytearray()
        exceeded = False
        while chunk := await process.stdout.read(64 * 1024):
            if exceeded:
                continue
            if len(output) + len(chunk) > 8 * 1024 * 1024:
                exceeded = True
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                output.extend(chunk)
        if exceeded:
            raise ValueError("Estimate worker output exceeds its limit")
        return bytes(output)

    async def cleanup():
        try:
            process = await launch
        except OSError:
            return
        if process.stdin is not None:
            process.stdin.close()
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

        async def discard():
            while await process.stdout.read(64 * 1024):
                pass

        drained = asyncio.create_task(discard())
        finished = asyncio.create_task(process.wait())
        pending = asyncio.gather(drained, finished)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(pending), 1)
        except TimeoutError:
            pass
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await pending

    try:
        process = await asyncio.shield(launch)
        reader = asyncio.create_task(collect(process))
        readers.append(reader)
        process.stdin.write(encoded)
        await process.stdin.drain()
        process.stdin.close()
        output = await reader
        if await process.wait() != 0:
            raise ValueError("AISimulate worker failed")
        result_type = Evaluation if operation == "estimate" else ToolSupport
        return result_type.model_validate_json(output)
    finally:
        cleaning = asyncio.create_task(cleanup())
        interrupted = False
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                interrupted = True
        cleaning.result()
        if interrupted:
            raise asyncio.CancelledError


def _profiles(profiles: tuple[RuntimeProfile, ...]) -> tuple[RuntimeProfile, ...]:
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("Managed runtime profile IDs must be unique")
    return profiles


def discover(*, blis_profiles: tuple[RuntimeProfile, ...] = ()) -> list[ToolSupport]:
    from .aic import adapter
    from .blis import service as blis

    profiles = _profiles(blis_profiles)
    return (
        [adapter.availability(), *(blis.availability(profile) for profile in profiles)]
        if profiles
        else [
            adapter.availability(),
            blis.availability(),
        ]
    )


async def support(
    request: EstimateRequest | OptimizeRequest | dict,
    *,
    blis_profiles: tuple[RuntimeProfile, ...] = (),
) -> list[ToolSupport]:
    request = TypeAdapter(EstimateRequest | OptimizeRequest).validate_python(request)
    profiles = _profiles(blis_profiles)
    results = []
    for tool in request.tools:
        if tool == "aisimulate":
            from .aic import adapter

            results.append(await adapter.support(request))
        else:
            from .blis import service as blis

            results.append(blis.support(request, profiles))
    return results


async def estimate(
    request: EstimateRequest | dict,
    *,
    blis_profiles: tuple[RuntimeProfile, ...] = (),
) -> BatchResult:
    estimator = EstimateService(blis_profiles=blis_profiles) if blis_profiles else default_estimator()
    return await estimator.estimate(request)


def _failure(request: EstimateRequest, tool: ToolId, status: ExecutionStatus, code: str, message: str) -> Evaluation:
    return Evaluation(
        tool=tool,
        strategy="exact" if tool == "blis" else "native_exact",
        runtime_profile=request.runtime_profiles.get(tool),
        configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
        status=status,
        error=Problem(code=code, message=message),
    )


class EstimateService:
    """Process-local admission and deadlines for independent request-response work."""

    _aisimulate = staticmethod(_isolated_aisimulate)

    def __init__(
        self,
        *,
        blis_profiles: tuple[RuntimeProfile, ...] = (),
        max_active: int = 4,
        wall_timeout_s: float = 120,
    ):
        if max_active < 1 or not 0 < wall_timeout_s <= 120:
            raise ValueError("Estimate limits require positive admission and a deadline of at most 120 seconds")
        self.profiles = _profiles(blis_profiles)
        self.max_active = max_active
        self.wall_timeout_s = wall_timeout_s
        self._active = 0

    def discover(self) -> list[ToolSupport]:
        return [self._execution_support(item) for item in discover(blis_profiles=self.profiles)]

    async def support(self, request: EstimateRequest | OptimizeRequest | dict) -> list[ToolSupport]:
        from .blis import service as blis

        request = TypeAdapter(EstimateRequest | OptimizeRequest).validate_python(request)
        if self._active >= self.max_active:
            return [
                ToolSupport(
                    tool=tool,
                    available=False,
                    status="unknown",
                    reason=Problem(code="estimate_busy", message="Estimate admission limit reached"),
                )
                for tool in request.tools
            ]
        self._active += 1
        try:
            results = []
            for tool in request.tools:
                if tool == "blis":
                    results.append(self._execution_support(blis.support(request, self.profiles)))
                    continue
                try:
                    async with asyncio.timeout(self.wall_timeout_s):
                        results.append(await self._aisimulate(request, "support"))
                except Exception:
                    results.append(
                        ToolSupport(
                            tool="aisimulate",
                            available=False,
                            status="unknown",
                            reason=Problem(code="support_probe_failed", message="AISimulate support probe failed"),
                        )
                    )
            return results
        finally:
            self._active -= 1

    def _execution_support(self, item: ToolSupport) -> ToolSupport:
        confirmed = any(
            profile.id == item.runtime_profile and profile.kernel_construction == "confirmed" and profile.result_digest
            for profile in self.profiles
        )
        if item.tool == "blis" and item.available and confirmed:
            item = item.model_copy(deep=True)
            for capability in item.capabilities:
                if capability.operation == "native_exact":
                    capability.enabled = True
                    capability.reason = "Locked runtime with request-specific catalog/compiler checks"
            item.warnings = [
                warning for warning in item.warnings if warning != "Runtime support does not enable Lens BLIS execution"
            ]
        return item

    async def _blis(self, request: EstimateRequest) -> Evaluation:
        from .blis import service as blis

        supported = blis.support(request, self.profiles)
        if supported.status != "supported":
            return Evaluation(
                tool="blis",
                strategy="exact",
                runtime_profile=supported.runtime_profile,
                configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
                status="unsupported" if supported.available or supported.status == "unsupported" else "unavailable",
                error=supported.reason or Problem(code="execution_not_verified", message="BLIS input is not verified"),
                resolved_input_digest=supported.resolved_input_digest,
                revisions=supported.revisions,
                evidence_maturity=supported.evidence_maturity,
                warnings=supported.warnings,
            )
        profile = blis.resolve(request, self.profiles)
        assert isinstance(profile, RuntimeProfile)
        scratch = storage_path("scratch", "estimate-tools")
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="exact-", dir=scratch) as temporary:
            directory = Path(temporary) / "bundle"
            executed = await blis._execute_bundle(request, profile, directory, asyncio.Event())
            if isinstance(executed, Problem):
                return _failure(request, "blis", "failed", executed.code, executed.message)
            stderr_text = ""
            if executed.returncode != 0:
                stderr = directory / "stderr.log"
                stderr_text = stderr.read_text(errors="replace")[-8192:] if stderr.is_file() else "No stderr captured"
                logging.getLogger(__name__).warning(
                    "BLIS execution failed (profile=%s, exit=%s): %s",
                    profile.id,
                    executed.returncode,
                    stderr_text,
                )
            result = blis._parse_result(
                request,
                profile,
                directory,
                (directory / "stdout.log").read_bytes(),
                returncode=executed.returncode,
                execution_status=executed.status,
                output_truncated=executed.output_truncated,
            )
            if (
                result.error
                and result.error.code == "process_failed"
                and all(
                    marker in stderr_text
                    for marker in ("kernel reports", "per-rank fixed occupancy", "A per-rank figure cannot exceed")
                )
            ):
                result.error = Problem(
                    code="native_memory_layout_rejected",
                    message=(
                        "BLIS rejected the native per-rank memory layout for this model and topology; "
                        "check model size, TP and explicit expert parallelism. No memory budget was guessed"
                    ),
                )
            return result

    async def _one(self, request: EstimateRequest, tool: ToolId, deadline: float) -> Evaluation:
        try:
            async with asyncio.timeout_at(deadline):
                if tool == "blis":
                    return await self._blis(request)
                return await self._aisimulate(request, "estimate")
        except TimeoutError:
            return _failure(request, tool, "timed_out", "execution_timed_out", "Estimate deadline exceeded")
        except Exception:
            return _failure(request, tool, "failed", "tool_estimate_failed", f"{tool} estimation failed")

    async def estimate(self, request: EstimateRequest | dict) -> BatchResult:
        request = EstimateRequest.model_validate(request)
        if self._active >= self.max_active:
            return BatchResult(
                outcome="failed",
                evaluations=[
                    _failure(request, tool, "failed", "estimate_busy", "Estimate admission limit reached")
                    for tool in request.tools
                ],
            )
        self._active += 1
        deadline = asyncio.get_running_loop().time() + min(
            request.run.wall_timeout_s or self.wall_timeout_s, self.wall_timeout_s
        )
        tasks = [asyncio.create_task(self._one(request, tool, deadline)) for tool in request.tools]
        try:
            evaluations = await asyncio.gather(*tasks)
            successes = sum(evaluation.status == "succeeded" for evaluation in evaluations)
            outcome = "succeeded" if successes == len(evaluations) else "partial" if successes else "failed"
            return BatchResult(outcome=outcome, evaluations=evaluations)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            cleaning = asyncio.gather(*tasks, return_exceptions=True)
            interrupted = False
            while not cleaning.done():
                try:
                    await asyncio.shield(cleaning)
                except asyncio.CancelledError:
                    interrupted = True
            self._active -= 1
            if interrupted:
                raise asyncio.CancelledError


_default: EstimateService | None = None


def default_estimator() -> EstimateService:
    global _default
    if _default is None:
        binary, inputs = os.environ.get("LENS_BLIS_BINARY"), os.environ.get("LENS_BLIS_INPUTS")
        if bool(binary) != bool(inputs):
            raise ValueError("LENS_BLIS_BINARY and LENS_BLIS_INPUTS must be configured together")
        profiles = ()
        if binary and inputs:
            from .blis.service import provisioned_profile

            profiles = (
                provisioned_profile(
                    binary=Path(binary).expanduser().absolute(), inputs=Path(inputs).expanduser().absolute()
                ),
            )
        else:
            from .blis.install import installed_profile

            try:
                installed = installed_profile()
            except ValueError:
                logging.getLogger(__name__).warning(
                    "Ignoring invalid optional managed BLIS installation; run BLIS check"
                )
                installed = None
            if installed is not None:
                profiles = (installed,)
        _default = EstimateService(blis_profiles=profiles)
    return _default


if __name__ == "__main__":
    from .aic import adapter

    operation = sys.argv[1]
    if operation not in {"estimate", "support"}:
        raise SystemExit(2)
    request = TypeAdapter(EstimateRequest | OptimizeRequest).validate_json(sys.stdin.buffer.read(1024 * 1024 + 1))
    with redirect_stdout(sys.stderr):
        result = asyncio.run(adapter.estimate(request) if operation == "estimate" else adapter.support(request))
    sys.stdout.write(result.model_dump_json())
