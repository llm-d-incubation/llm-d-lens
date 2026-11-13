"""Tests for orphan deployment namespace scan/cleanup."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from llm_d_lens.deploy import orphans


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    def __init__(self, *, namespaces=None, configmaps=None, per_namespace=None, delete=None) -> None:
        self.namespaces = namespaces or _Result(stdout=_namespaces_json())
        self.configmaps = configmaps or _Result(stdout=_configmaps_json())
        self.per_namespace = per_namespace or {}
        self.delete = delete or _Result()
        self.calls: list = []

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append(argv)
        if argv[:3] == ["kubectl", "get", "namespaces"]:
            return self.namespaces
        if argv[:3] == ["kubectl", "get", "configmaps"]:
            assert argv[-4:] == [
                "-l",
                "lens.llm-d.ai/role=inventory,app.kubernetes.io/managed-by=llm-d-lens",
                "-o",
                "json",
            ]
            result = self.configmaps if "-A" in argv else self.per_namespace[argv[4]]
            if isinstance(result, Exception):
                raise result
            return result
        if argv[:3] == ["kubectl", "delete", "namespace"]:
            return self.delete
        raise AssertionError(f"unexpected command: {argv}")


class _Store:
    def __init__(self, namespaces: list[str]) -> None:
        self._namespaces = namespaces

    def list_executions(self):
        return [SimpleNamespace(namespace=name) for name in self._namespaces]


def _namespaces_json(*names: str) -> str:
    items = [
        {
            "metadata": {"name": name, "creationTimestamp": f"2026-09-0{index + 1}T00:00:00Z"},
            "status": {"phase": "Active"},
        }
        for index, name in enumerate(names)
    ]
    return json.dumps({"items": items})


def _configmaps_json(*namespaces: str) -> str:
    return json.dumps({"items": [{"metadata": {"namespace": name}} for name in namespaces]})


@pytest.mark.asyncio
async def test_scan_returns_only_untracked_prefixed_namespaces(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(
        namespaces=_Result(
            stdout=_namespaces_json(
                "llmd-",
                "llmd-known",
                "llmd-orphan",
                "other-ns",
            )
        )
    )
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    result = await orphans.scan_orphan_namespaces("cluster-a", store=_Store(["llmd-known"]))

    assert [orphan.name for orphan in result] == ["llmd-orphan"]
    assert result[0].phase == "Active"


@pytest.mark.asyncio
async def test_scan_raises_on_kubectl_failure(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: _Runner(namespaces=_Result(returncode=1, stderr="boom")))
    with pytest.raises(orphans.OrphanCleanupError, match="boom"):
        await orphans.scan_orphan_namespaces("cluster-a", store=_Store([]))


@pytest.mark.asyncio
async def test_clean_refuses_known_and_outside_prefix(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner()
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    cleaned, failed = await orphans.clean_orphan_namespaces(
        "cluster-a", ["llmd-orphan", "llmd-known", "other-ns"], store=_Store(["llmd-known"])
    )

    assert cleaned == ["llmd-orphan"]
    assert {row["name"] for row in failed} == {"llmd-known", "other-ns"}
    assert [argv[:4] for argv in runner.calls if argv[1] == "delete"] == [
        ["kubectl", "delete", "namespace", "llmd-orphan"]
    ]
    assert failed == [
        {"name": "llmd-known", "error": "namespace is still tracked by a Lens deployment"},
        {"name": "other-ns", "error": "namespace is outside the deployment prefix"},
    ]


@pytest.mark.asyncio
async def test_scan_excludes_engine_namespaces(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(
        namespaces=_Result(stdout=_namespaces_json("llmd-engine", "llmd-orphan")),
        configmaps=_Result(stdout=_configmaps_json("llmd-engine", "other-engine")),
    )
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    result = await orphans.scan_orphan_namespaces("cluster-a", store=_Store([]))

    assert [orphan.name for orphan in result] == ["llmd-orphan"]
    assert len(runner.calls) == 2
    assert runner.calls[1][3] == "-A"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [_Result(returncode=1, stderr="forbidden"), TimeoutError("timed out"), _Result(stdout=None)]
)
async def test_scan_falls_back_to_namespace_queries(monkeypatch, failure):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(
        namespaces=_Result(stdout=_namespaces_json("llmd-engine", "llmd-orphan")),
        configmaps=failure,
        per_namespace={
            "llmd-engine": _Result(stdout=_configmaps_json("llmd-engine")),
            "llmd-orphan": _Result(stdout=_configmaps_json()),
        },
    )
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    result = await orphans.scan_orphan_namespaces("cluster-a", store=_Store([]))

    assert [orphan.name for orphan in result] == ["llmd-orphan"]
    assert [argv[3:5] for argv in runner.calls[2:]] == [["-n", "llmd-engine"], ["-n", "llmd-orphan"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        _Result(returncode=1, stderr="forbidden"),
        OSError("unavailable"),
        _Result(stdout="invalid JSON"),
        _Result(stdout=None),
    ],
)
async def test_scan_raises_when_engine_check_fails(monkeypatch, failure):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(
        namespaces=_Result(stdout=_namespaces_json("llmd-unknown", "llmd-unqueried")),
        configmaps=_Result(returncode=1, stderr="forbidden"),
        per_namespace={"llmd-unknown": failure},
    )
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    with pytest.raises(orphans.OrphanCleanupError, match="llmd-unknown"):
        await orphans.scan_orphan_namespaces("cluster-a", store=_Store([]))

    assert [argv[3:5] for argv in runner.calls[2:]] == [["-n", "llmd-unknown"]]


@pytest.mark.asyncio
async def test_clean_refuses_engine_namespace(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(configmaps=_Result(stdout=_configmaps_json("llmd-engine")))
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    cleaned, failed = await orphans.clean_orphan_namespaces(
        "cluster-a", ["llmd-engine", "llmd-orphan"], store=_Store([])
    )

    assert cleaned == ["llmd-orphan"]
    assert failed == [{"name": "llmd-engine", "error": "namespace holds a Lens engine instance"}]
    assert [argv[3] for argv in runner.calls if argv[1] == "delete"] == ["llmd-orphan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_Result(returncode=1, stderr="forbidden"), FileNotFoundError("kubectl")])
async def test_clean_refuses_namespace_when_engine_check_fails(monkeypatch, failure):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(
        configmaps=_Result(returncode=1, stderr="forbidden"),
        per_namespace={"llmd-unknown": failure, "llmd-orphan": _Result(stdout=_configmaps_json())},
    )
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    cleaned, failed = await orphans.clean_orphan_namespaces(
        "cluster-a", ["llmd-unknown", "llmd-orphan"], store=_Store([])
    )

    assert cleaned == ["llmd-orphan"]
    assert len(failed) == 1
    assert failed[0]["name"] == "llmd-unknown"
    assert failed[0]["error"].startswith("could not check for Lens engine instances: ")
    assert [argv[3] for argv in runner.calls if argv[1] == "delete"] == ["llmd-orphan"]


@pytest.mark.asyncio
async def test_clean_checks_and_deletes_duplicate_candidate_only_once(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(
        configmaps=_Result(returncode=1, stderr="forbidden"),
        per_namespace={"llmd-orphan": _Result(stdout=_configmaps_json())},
    )
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    cleaned, failed = await orphans.clean_orphan_namespaces(
        "cluster-a", ["llmd-orphan", "llmd-orphan"], store=_Store([])
    )

    assert cleaned == ["llmd-orphan"]
    assert failed == []
    assert [argv[3:5] for argv in runner.calls if "-n" in argv] == [["-n", "llmd-orphan"]]
    assert [argv[3] for argv in runner.calls if argv[1] == "delete"] == ["llmd-orphan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("clean", [False, True])
async def test_no_engine_query_without_candidates(monkeypatch, clean):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    names = ["llmd-", "llmd-known", "other-ns"]
    runner = _Runner(namespaces=_Result(stdout=_namespaces_json(*names)))
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    if clean:
        cleaned, _failed = await orphans.clean_orphan_namespaces("cluster-a", names, store=_Store(["llmd-known"]))
        assert cleaned == []
    else:
        assert await orphans.scan_orphan_namespaces("cluster-a", store=_Store(["llmd-known"])) == []

    assert all(argv[:3] == ["kubectl", "get", "namespaces"] for argv in runner.calls)
