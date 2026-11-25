"""SSAR request construction, attribute mapping, chunking and caching."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from llm_d_lens.cluster import evaluator
from llm_d_lens.cluster.capabilities import (
    CAPABILITY_UNITS,
    MODEL_CACHE_TOKEN_SECRET,
    unit_namespaces,
)
from llm_d_lens.cluster.evaluator import (
    CHUNK_SIZE,
    CapabilityCheckUnavailable,
    clear_cache,
    evaluate_capabilities,
)
from llm_d_lens.cluster.plan import normalize_plan, plan_hash
from llm_d_lens.infrastructure.process import CommandResult

CLUSTER_ID = "01234567"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def _minimal_plan(**overrides):
    data = {
        "gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
        "accelerators": [],
        "storage": {"kind": "none"},
        "hfTokenSecret": {"tokenSource": "create-secret", "secretNamespace": "lens-gateway"},
        "modelCache": {"enabled": False},
        "stack": {"lwsEnabled": False, "crdsEnabled": False, "imagePrepullEnabled": False},
        "runtime": {"inotifyEnabled": False},
    }
    data.update(overrides)
    return normalize_plan(data, cluster_id=CLUSTER_ID)


def _model_cache_storage(**overrides):
    data = {
        "kind": "local-disk",
        "hostPath": "/cache",
        "capacity": "10Gi",
        "purposes": ["model-cache"],
    }
    data.update(overrides)
    return data


def _large_plan():
    return _minimal_plan(
        gateway={"provider": "istio", "namespace": "lens-gateway", "namespaceMode": "create", "name": "lens-gateway"},
        accelerators=[
            {"vendor": "nvidia", "mode": "plugin", "rbac": ["escalation"]},
            {"vendor": "intel", "mode": "dra", "rbac": ["namespaced", "cluster"]},
        ],
        storage=_model_cache_storage(namespaceMode="create"),
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "team-b",
            "secretNamespaceMode": "create",
        },
        modelCache={"enabled": True, "repoId": "org/model", "revision": "main", "storageVolumeRef": "v"},
        stack={
            "lwsEnabled": True,
            "crdsEnabled": True,
            "imagePrepullEnabled": True,
            "images": ["ghcr.io/example/router:v1"],
            "imagesNamespace": "lens-images",
            "imagesNamespaceMode": "create",
        },
        runtime={"inotifyEnabled": True},
    )


class FakeRunner:
    """Answer constructed SSAR lists without touching a cluster."""

    def __init__(
        self,
        *,
        deny: Any | None = None,
        order: str = "same",
        duplicate: bool = False,
        drop: bool = False,
        returncode: int = 0,
        stdout: str | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
        mode: str = "list",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._deny = deny or (lambda _spec: False)
        self._order = order
        self._duplicate = duplicate
        self._drop = drop
        self._returncode = returncode
        self._stdout = stdout
        self._error = error
        self._delay = delay
        self._mode = mode

    async def run(self, argv, *, input=None, timeout=None, env=None, **_kwargs):  # noqa: A002
        body = json.loads(input)
        self.calls.append({"argv": tuple(argv), "items": body["items"], "timeout": timeout, "env": env})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        if self._returncode != 0:
            return CommandResult(tuple(argv), self._returncode, "", "kubectl failed")
        if self._stdout is not None:
            return CommandResult(tuple(argv), 0, self._stdout, "")

        answered = [
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": item["spec"],
                "status": {"allowed": not self._deny(item["spec"])},
            }
            for item in body["items"]
        ]
        if self._order == "reverse":
            answered.reverse()
        if self._duplicate:
            answered.append(answered[0])
        if self._drop:
            answered.pop()
        if self._mode == "stream":
            payload = "\n".join(json.dumps(item) for item in answered)
        elif self._mode == "single":
            payload = json.dumps(answered[0]) if answered else ""
        else:
            payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": answered})
        return CommandResult(tuple(argv), 0, payload, "")


def _resource_attributes(spec: dict[str, Any]) -> dict[str, Any]:
    attrs = spec.get("resourceAttributes")
    return attrs if isinstance(attrs, dict) else {}


def _non_resource_attributes(spec: dict[str, Any]) -> dict[str, Any]:
    attrs = spec.get("nonResourceAttributes")
    return attrs if isinstance(attrs, dict) else {}


def _resource(spec: dict[str, Any]) -> tuple[str, str, str, str, str]:
    attrs = _resource_attributes(spec)
    return (
        attrs.get("verb", ""),
        attrs.get("group", ""),
        attrs.get("resource", ""),
        attrs.get("namespace", ""),
        attrs.get("name", ""),
    )


def _missing_units(report) -> dict[str, list[dict[str, Any]]]:
    return {entry["unit"]: entry["rules"] for entry in report.missing}


@pytest.mark.asyncio
async def test_reports_denied_units_and_non_resource_rules():
    plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "lens-gateway", "namespaceMode": "create", "name": "lens-gateway"}
    )
    runner = FakeRunner(
        deny=lambda spec: (
            _resource(spec)[:3] == ("create", "", "namespaces")
            or _non_resource_attributes(spec).get("path") == "/version"
        )
    )

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert report.plan_hash == plan_hash(plan)
    assert report.identity is None
    assert report.skipped == []
    assert report.checked_at.tzinfo is not None
    assert report.status == "denied"
    assert report.sufficient is False

    requested = runner.calls[0]["items"]
    assert any(item["spec"].get("nonResourceAttributes", {}).get("path") == "/version" for item in requested)

    missing = _missing_units(report)
    assert missing["core.namespaces"] == [
        {"verb": "create", "apiGroup": "", "resource": "namespaces", "scope": "cluster"}
    ]
    assert missing["core.discovery"] == [{"verb": "get", "path": "/version"}]


@pytest.mark.asyncio
async def test_sufficient_report_has_no_missing_entries():
    plan = _minimal_plan()

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=FakeRunner())

    assert report.status == "sufficient"
    assert report.sufficient is True
    assert report.missing == []


def _secret_items(runner):
    return [
        item
        for call in runner.calls
        for item in call["items"]
        if _resource_attributes(item["spec"]).get("resource") == "secrets"
    ]


def _secret_coords(runner):
    return {
        (
            _resource_attributes(item["spec"]).get("name", ""),
            _resource_attributes(item["spec"]).get("namespace", ""),
        )
        for item in _secret_items(runner)
    }


@pytest.mark.asyncio
async def test_hf_secret_reviews_use_resource_name_and_a_named_only_role_passes():
    plan = _minimal_plan(
        hfTokenSecret={
            "tokenSource": "existing-secret:shared-hf-token",
            "secretNamespace": "lens-gateway",
        }
    )
    runner = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("resource") == "secrets" and not _resource_attributes(spec).get("name")
        )
    )

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert report.sufficient is True
    secret_items = _secret_items(runner)
    assert secret_items
    assert {_resource_attributes(item["spec"])["name"] for item in secret_items} == {"shared-hf-token"}
    assert {_resource_attributes(item["spec"])["namespace"] for item in secret_items} == {"lens-gateway"}


@pytest.mark.asyncio
async def test_model_cache_secret_reviews_use_the_source_and_job_namespaces():
    plan = _minimal_plan(
        storage=_model_cache_storage(),
        hfTokenSecret={
            "tokenSource": "existing-secret:shared-hf-token",
            "secretNamespace": "lens-gateway",
        },
        modelCache={"enabled": True, "repoId": "org/model", "revision": "main", "storageVolumeRef": "v"},
    )
    allowed_coords = {
        ("shared-hf-token", "lens-gateway"),
        (MODEL_CACHE_TOKEN_SECRET, f"lens-storage-{CLUSTER_ID}"),
    }
    runner = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("resource") == "secrets"
            and (
                _resource_attributes(spec).get("name", ""),
                _resource_attributes(spec).get("namespace", ""),
            )
            not in allowed_coords
        )
    )

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert report.sufficient is True
    assert _secret_coords(runner) == allowed_coords


@pytest.mark.asyncio
async def test_installer_and_hf_secret_checks_use_their_own_namespaces():
    plan = _minimal_plan(
        gateway={"provider": "istio", "namespace": "lens-gateway", "name": "lens-gateway"},
        hfTokenSecret={
            "tokenSource": "existing-secret:shared-hf-token",
            "secretNamespace": "hf-space",
        },
    )
    runner = FakeRunner()

    await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    installer_secrets = [item for item in _secret_items(runner) if not _resource_attributes(item["spec"]).get("name")]
    hf_secrets = [item for item in _secret_items(runner) if _resource_attributes(item["spec"]).get("name")]

    assert {_resource_attributes(item["spec"])["namespace"] for item in installer_secrets} == {"istio-system"}
    assert {_resource_attributes(item["spec"])["namespace"] for item in hf_secrets} == {"hf-space"}


@pytest.mark.asyncio
async def test_denied_named_secret_reports_the_name():
    plan = _minimal_plan(
        hfTokenSecret={
            "tokenSource": "existing-secret:shared-hf-token",
            "secretNamespace": "lens-gateway",
        }
    )
    runner = FakeRunner(deny=lambda spec: _resource_attributes(spec).get("name") == "shared-hf-token")

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert report.sufficient is False
    rules = _missing_units(report)["cache.hf-secret.read"]
    assert rules == [
        {
            "verb": "get",
            "apiGroup": "",
            "resource": "secrets",
            "scope": "namespace",
            "namespace": "lens-gateway",
            "name": "shared-hf-token",
        }
    ]


@pytest.mark.asyncio
async def test_overlapping_units_share_one_request_and_both_are_reported():
    plan = _minimal_plan(
        accelerators=[{"vendor": "nvidia", "mode": "plugin"}],
        hfTokenSecret={"tokenSource": "create-secret", "secretNamespace": "kube-system"},
    )
    selected = ["core.discovery", "accelerator.nvidia"]
    runner = FakeRunner(deny=lambda spec: _resource(spec)[:3] == ("get", "monitoring.coreos.com", "servicemonitors"))

    report = await evaluate_capabilities(CLUSTER_ID, plan, unit_ids=selected, runner=runner)

    requested = runner.calls[0]["items"]
    servicemonitors = [
        item for item in requested if item["spec"].get("resourceAttributes", {}).get("resource") == "servicemonitors"
    ]
    servicemonitor_keys = {_resource(item["spec"]) for item in servicemonitors}
    # ``core.discovery`` probes every plan namespace and ``accelerator.nvidia``
    # probes the device-plugin namespace; the shared kube-system review is one
    # request attributed to both units.
    assert ("get", "monitoring.coreos.com", "servicemonitors", "kube-system", "") in servicemonitor_keys
    assert len([key for key in servicemonitor_keys if key[3] == "kube-system"]) == 1
    assert set(unit_namespaces(plan)["core.discovery"]) >= {"lens-gateway", "kube-system"}

    missing = _missing_units(report)
    assert set(missing) == {"core.discovery", "accelerator.nvidia"}
    for rules in missing.values():
        assert any(rule["resource"] == "servicemonitors" for rule in rules)


@pytest.mark.asyncio
async def test_response_order_does_not_matter():
    plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "lens-gateway", "namespaceMode": "create", "name": "lens-gateway"}
    )
    runner = FakeRunner(
        order="reverse",
        deny=lambda spec: _resource(spec)[:3] == ("create", "", "namespaces"),
    )

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert report.sufficient is False
    assert "core.namespaces" in _missing_units(report)


@pytest.mark.asyncio
async def test_multi_document_stream_is_parsed_and_mapped(monkeypatch):
    plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "lens-gateway", "namespaceMode": "create", "name": "lens-gateway"}
    )
    runner = FakeRunner(
        mode="stream",
        deny=lambda spec: _resource(spec)[:3] == ("create", "", "namespaces"),
    )
    monkeypatch.setattr(evaluator, "CHUNK_SIZE", 2)

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert report.status == "denied"
    assert "core.namespaces" in _missing_units(report)
    assert len(runner.calls) > 1
    assert any(len(call["items"]) == 2 for call in runner.calls)
    assert all(len(call["items"]) <= 2 for call in runner.calls)


@pytest.mark.asyncio
async def test_single_list_wrapper_is_parsed():
    plan = _minimal_plan()

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=FakeRunner(mode="list"))

    assert report.status == "sufficient"


@pytest.mark.asyncio
async def test_single_object_per_chunk_is_parsed(monkeypatch):
    plan = _minimal_plan()
    runner = FakeRunner(mode="single")
    monkeypatch.setattr(evaluator, "CHUNK_SIZE", 1)

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert report.status == "sufficient"
    assert len(runner.calls) > 1
    assert all(len(call["items"]) == 1 for call in runner.calls)


def test_parser_accepts_stream_with_surrounding_whitespace():
    first = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": {"verb": "get", "resource": "pods"}},
        "status": {"allowed": True},
    }
    second = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"nonResourceAttributes": {"verb": "get", "path": "/version"}},
        "status": {"allowed": False},
    }
    stdout = "\n  " + json.dumps(first) + "  \n\n" + json.dumps(second) + "\n "

    documents = evaluator._parse_review_documents(stdout)

    assert [document["spec"] for document in documents] == [first["spec"], second["spec"]]


@pytest.mark.asyncio
async def test_review_requests_omit_metadata_name():
    plan = _minimal_plan()
    runner = FakeRunner()

    await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    items = [item for call in runner.calls for item in call["items"]]
    assert items
    assert all("metadata" not in item or "name" not in item["metadata"] for item in items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(duplicate=True),
        FakeRunner(drop=True),
        FakeRunner(returncode=1),
        FakeRunner(stdout="not json"),
        FakeRunner(stdout=""),
        FakeRunner(stdout="   \n"),
        FakeRunner(stdout='{"apiVersion": "v1", "kind": "List", "items": [{"spec":'),
        FakeRunner(stdout='{"apiVersion": "v1", "kind": "List"}'),
        FakeRunner(stdout='{"apiVersion": "v1", "kind": "List", "items": []}'),
        FakeRunner(stdout="5"),
        FakeRunner(error=TimeoutError("timed out")),
        FakeRunner(error=FileNotFoundError("kubectl")),
    ],
)
async def test_incomplete_or_failed_answers_are_unavailable(runner):
    plan = _minimal_plan()

    with pytest.raises(CapabilityCheckUnavailable):
        await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)


@pytest.mark.asyncio
async def test_requests_are_chunked_at_fifty_reviews():
    plan = _large_plan()
    runner = FakeRunner()

    await evaluate_capabilities(CLUSTER_ID, plan, unit_ids=list(CAPABILITY_UNITS), runner=runner)

    assert len(runner.calls) > 1
    assert all(len(call["items"]) <= CHUNK_SIZE for call in runner.calls)
    total = sum(len(call["items"]) for call in runner.calls)
    assert total > 50


@pytest.mark.asyncio
async def test_exceeding_the_total_budget_is_unavailable(monkeypatch):
    plan = _large_plan()
    runner = FakeRunner(delay=0.02)
    monkeypatch.setattr(evaluator, "TOTAL_BUDGET_SECONDS", 0.01)

    with pytest.raises(CapabilityCheckUnavailable):
        await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)


@pytest.mark.asyncio
async def test_successful_report_is_cached_and_use_cache_false_bypasses_it():
    plan = _minimal_plan()
    runner = FakeRunner()

    first = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)
    second = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner)

    assert len(runner.calls) == 1
    assert second is first

    third = await evaluate_capabilities(CLUSTER_ID, plan, runner=runner, use_cache=False)

    assert len(runner.calls) == 2
    assert third.plan_hash == first.plan_hash


@pytest.mark.asyncio
async def test_unavailable_result_is_not_cached():
    plan = _minimal_plan()

    with pytest.raises(CapabilityCheckUnavailable):
        await evaluate_capabilities(CLUSTER_ID, plan, runner=FakeRunner(drop=True))

    report = await evaluate_capabilities(CLUSTER_ID, plan, runner=FakeRunner())

    assert report.sufficient is True


def _op(
    operation_id: str,
    *,
    checks: tuple = (),
    namespace: str | None = "ns",
    depends_on: tuple[str, ...] = (),
    user_owned_secrets: tuple[str, ...] = (),
    executor: str = "auto",
    apply_kind: str = "manifests",
    admin_only: bool = False,
    requires_cluster_wildcard: bool = False,
    secret_copy: Any = None,
    apply_options: Any = None,
):
    from llm_d_lens.cluster.operations import (
        Operation,
        OperationApply,
        OperationRequirement,
    )

    return Operation(
        id=operation_id,
        step="test",
        title=operation_id,
        scope="namespace" if namespace else "cluster",
        namespace=namespace,
        apply=OperationApply(
            kind=apply_kind,
            namespace=namespace,
            secret_copy=secret_copy,
            apply_options=apply_options,
        ),
        requirement=OperationRequirement(checks=checks),
        depends_on=depends_on,
        user_owned_secrets=user_owned_secrets,
        executor=executor,
        admin_only=admin_only,
        requires_cluster_wildcard=requires_cluster_wildcard,
    )


@pytest.mark.asyncio
async def test_evaluate_operations_splits_user_admin_and_lens():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    user = _op("user.op", checks=(CheckRule(verb="get", api_group="", resource="configmaps", scope="namespace"),))
    admin = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
    )
    lens = _op("lens.op", executor="lens", apply_kind="lens", namespace=None)

    runner = FakeRunner(deny=lambda spec: spec["resourceAttributes"]["verb"] == "create")
    report = await evaluate_operations(
        CLUSTER_ID,
        [user, admin, lens],
        plan_hash="plan-hash",
        plan_version=5,
        runner=runner,
    )

    assert report.status == "admin_required"
    assert report.sufficient is False
    assert [item["id"] for item in report.user_operations] == ["user.op"]
    assert [item["id"] for item in report.admin_operations] == ["admin.op"]
    assert [item["id"] for item in report.lens_operations] == ["lens.op"]
    assert report.missing[0]["operationId"] == "admin.op"


@pytest.mark.asyncio
async def test_evaluate_operations_forces_user_owned_secret_operations_to_user():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    secret_write = _op(
        "cache.hf-secret",
        checks=(CheckRule(verb="create", api_group="", resource="secrets", scope="namespace"),),
        user_owned_secrets=("hf-token",),
    )
    runner = FakeRunner(deny=lambda _spec: True)
    report = await evaluate_operations(
        CLUSTER_ID,
        [secret_write],
        plan_hash="plan-hash",
        runner=runner,
    )

    assert report.status == "blocked"
    assert report.sufficient is False
    assert [item["id"] for item in report.blocked_operations] == ["cache.hf-secret"]
    assert report.user_operations == []
    assert report.admin_operations == []
    assert report.missing[0]["operationId"] == "cache.hf-secret"


@pytest.mark.asyncio
async def test_evaluate_operations_rejects_an_admin_operation_depending_on_user():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import ApplyOptions, OperationDependencyError, SecretCopy

    # Normal promotable user operation upstream of an admin operation promotes cleanly.
    user = _op("user.op", checks=(CheckRule(verb="get", api_group="", resource="configmaps", scope="namespace"),))
    admin = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
        depends_on=("user.op",),
    )
    runner = FakeRunner(deny=lambda spec: spec["resourceAttributes"]["verb"] == "create")
    promoted = await evaluate_operations(CLUSTER_ID, [user, admin], plan_hash="plan-hash", runner=runner)
    assert promoted.status == "admin_required"
    assert promoted.user_operations == []
    assert [item["id"] for item in promoted.admin_operations] == ["user.op", "admin.op"]
    assert "missing" not in promoted.admin_operations[0]
    assert [item["operationId"] for item in promoted.missing] == ["admin.op"]

    # Prohibited upstream operations (user_owned_secrets, secret_copy, delete_before_apply, lens, blocked)
    # are NEVER promoted and fail closed with OperationDependencyError.
    prohibited_upstreams = [
        _op(
            "secret.op",
            checks=(CheckRule(verb="get", api_group="", resource="secrets", scope="namespace"),),
            user_owned_secrets=("hf-token",),
        ),
        _op(
            "copy.op",
            checks=(CheckRule(verb="get", api_group="", resource="secrets", scope="namespace"),),
            secret_copy=SecretCopy("ns-a", "src", "ns-b", "dst"),
        ),
        _op(
            "delete.op",
            checks=(CheckRule(verb="get", api_group="batch", resource="jobs", scope="namespace"),),
            apply_options=ApplyOptions(delete_before_apply=True),
        ),
        _op("lens.op", executor="lens", apply_kind="lens", namespace=None),
    ]
    for upstream in prohibited_upstreams:
        downstream = _op(
            "admin.op",
            namespace=None,
            checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
            depends_on=(upstream.id,),
        )
        with pytest.raises(OperationDependencyError, match="operation_dependency_invalid"):
            await evaluate_operations(
                CLUSTER_ID,
                [upstream, downstream],
                plan_hash=f"plan-{upstream.id}",
                runner=runner,
                use_cache=False,
            )

    # Blocked upstream (denied user_owned_secrets) also fails closed.
    blocked_secret = _op(
        "blocked.secret",
        checks=(CheckRule(verb="get", api_group="", resource="secrets", scope="namespace"),),
        user_owned_secrets=("hf-token",),
    )
    downstream_blocked = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
        depends_on=("blocked.secret",),
    )
    with pytest.raises(OperationDependencyError, match="depends on blocked operation"):
        await evaluate_operations(
            CLUSTER_ID,
            [blocked_secret, downstream_blocked],
            plan_hash="plan-blocked",
            runner=FakeRunner(deny=lambda _spec: True),
            use_cache=False,
        )


@pytest.mark.asyncio
async def test_evaluate_operations_rejects_a_cycle():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import OperationDependencyError

    first = _op("a", checks=(CheckRule(verb="get", api_group="", resource="configmaps", scope="namespace"),))
    second = _op(
        "b",
        checks=(CheckRule(verb="get", api_group="", resource="configmaps", scope="namespace"),),
        depends_on=("a",),
    )
    first = first.__class__(**{**first.__dict__, "depends_on": ("b",)})

    with pytest.raises(OperationDependencyError, match="operation_dependency_invalid"):
        await evaluate_operations(CLUSTER_ID, [first, second], plan_hash="plan-hash", runner=FakeRunner())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["list", "stream", "single"])
async def test_evaluate_operations_accepts_every_ssar_output_shape_and_caches(mode):
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    operation = _op(
        "user.op",
        checks=(CheckRule(verb="get", api_group="", resource="configmaps", scope="namespace"),),
    )
    runner = FakeRunner(mode=mode)

    first = await evaluate_operations(CLUSTER_ID, [operation], plan_hash="plan-hash", runner=runner)
    calls = len(runner.calls)
    second = await evaluate_operations(CLUSTER_ID, [operation], plan_hash="plan-hash", runner=runner)

    assert first.status == "ready"
    assert second.status == "ready"
    assert len(runner.calls) == calls


class MarkerRunner(FakeRunner):
    def __init__(self, marker: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._marker = marker

    async def run(self, argv, *, input=None, timeout=None, env=None, **_kwargs):  # noqa: A002
        if tuple(argv[:3]) == ("kubectl", "get", "configmap"):
            return CommandResult(tuple(argv), 0, json.dumps({"data": self._marker}), "")
        return await super().run(argv, input=input, timeout=timeout, env=env, **_kwargs)


@pytest.mark.asyncio
async def test_evaluate_operations_reads_and_verifies_the_marker():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    operation = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
    )
    runner = MarkerRunner(
        {"planHash": "plan-hash", "status": "complete", "operations": "admin.op"},
        deny=lambda _spec: True,
    )
    report = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=runner,
    )

    assert report.status == "admin_required"
    assert report.marker == {
        "namespace": "lens-edit",
        "name": f"lens-admin-handoff-{'plan-hash'[:16]}",
        "status": "complete",
        "verified": True,
        "operations": ["admin.op"],
    }


@pytest.mark.asyncio
async def test_intel_dra_namespace_checks_are_bound_to_its_namespace():
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
        accelerators=[{"vendor": "intel", "mode": "dra"}],
    )
    runner = FakeRunner()
    report = await evaluate_operations(
        CLUSTER_ID,
        build_operations(plan, cluster_id=CLUSTER_ID),
        plan_hash="plan-hash",
        runner=runner,
    )

    assert report.status in {"ready", "admin_required"}
    namespaces = {
        item["spec"]["resourceAttributes"].get("namespace")
        for call in runner.calls
        for item in call["items"]
        if "resourceAttributes" in item["spec"]
    }
    assert "intel-gpu-resource-driver" in namespaces


@pytest.mark.asyncio
async def test_intel_plugin_is_admin_only_even_when_every_check_is_allowed():
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
        accelerators=[{"vendor": "intel", "mode": "plugin"}],
    )
    operations = build_operations(plan, cluster_id=CLUSTER_ID)

    # Full cluster-admin (*/*/* allowed) passes preflight directly as ready.
    ready_runner = FakeRunner()
    ready_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-ready",
        runner=ready_runner,
    )
    assert ready_report.status == "ready"
    assert ready_report.sufficient is True
    assert ready_report.admin_operations == []
    assert ready_report.missing == []
    user_ids = {item["id"] for item in ready_report.user_operations}
    assert {"accelerator.nfd", "accelerator.nfd.rules", "accelerator.intel.plugin.0"} <= user_ids
    submitted_wildcard = [
        item for call in ready_runner.calls for item in call["items"] if _resource(item["spec"])[:3] == ("*", "*", "*")
    ]
    assert len(submitted_wildcard) == 1

    # Explicit resource permissions WITHOUT cluster-wide wildcard (*/*/*) fall back to admin_required.
    no_wildcard_runner = FakeRunner(deny=lambda spec: _resource(spec)[:3] == ("*", "*", "*"))
    denied_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-no-wildcard",
        runner=no_wildcard_runner,
    )
    assert denied_report.status == "admin_required"
    assert denied_report.sufficient is False
    admin_ids = {item["id"] for item in denied_report.admin_operations}
    assert {"accelerator.nfd", "accelerator.nfd.rules", "accelerator.intel.plugin.0"} <= admin_ids
    wildcard_missing = [
        rule
        for rule in denied_report.missing
        if (rule.get("verb"), rule.get("apiGroup"), rule.get("resource"), rule.get("scope"))
        == ("*", "*", "*", "cluster")
    ]
    assert {rule["operationId"] for rule in wildcard_missing} == {
        "accelerator.nfd",
        "accelerator.nfd.rules",
        "accelerator.intel.plugin.0",
    }
    assert any(
        rule["operationId"] == "accelerator.intel.plugin.0" and rule.get("reason") == "namespace target is not bound"
        for rule in denied_report.missing
    )


@pytest.mark.asyncio
async def test_unbound_namespace_checks_are_never_user_or_ready():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    operation = _op(
        "unbound.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="apps", resource="deployments", scope="namespace"),),
    )
    runner = FakeRunner()
    report = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        runner=runner,
    )

    assert report.status == "admin_required"
    assert report.sufficient is False
    assert report.user_operations == []
    assert report.admin_operations[0]["id"] == "unbound.op"
    assert report.missing[0]["reason"] == "namespace target is not bound"
    # Non-wildcard plans never submit a cluster-wide wildcard SSAR probe.
    assert not any(_resource(item["spec"])[:3] == ("*", "*", "*") for call in runner.calls for item in call["items"])

    # Even when a wildcard op in the same batch triggers */*/*=True, non-wildcard unbound ops remain fail-closed.
    wildcard_op = _op(
        "overlay.op",
        namespace=None,
        requires_cluster_wildcard=True,
        checks=(CheckRule(verb="create", api_group="apps", resource="daemonsets", scope="namespace"),),
    )
    mixed_report = await evaluate_operations(
        CLUSTER_ID,
        [operation, wildcard_op],
        plan_hash="plan-hash-mixed",
        runner=FakeRunner(),
    )
    assert [item["id"] for item in mixed_report.user_operations] == ["overlay.op"]
    assert [item["id"] for item in mixed_report.admin_operations] == ["unbound.op"]
    assert mixed_report.missing[0]["operationId"] == "unbound.op"
    assert mixed_report.missing[0]["reason"] == "namespace target is not bound"


@pytest.mark.asyncio
async def test_marker_is_not_verified_without_every_current_admin_operation():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    operation = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
    )
    runner = MarkerRunner(
        {"planHash": "plan-hash", "status": "complete", "operations": "other.op"},
        deny=lambda _spec: True,
    )
    report = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=runner,
    )

    assert report.marker["status"] == "partial"
    assert report.marker["verified"] is False


@pytest.mark.asyncio
async def test_marker_read_forbidden_is_unconfirmed_not_missing():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    class ForbiddenMarkerRunner(FakeRunner):
        async def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            if tuple(argv[:3]) == ("kubectl", "get", "configmap"):
                return CommandResult(tuple(argv), 1, "", "Error from server (Forbidden)")
            return await super().run(argv, **kwargs)

    operation = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
    )
    report = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=ForbiddenMarkerRunner(deny=lambda _spec: True),
    )

    assert report.marker["status"] == "unconfirmed"
    assert report.marker["verified"] is False


@pytest.mark.asyncio
async def test_lws_admin_only_operations_do_not_create_admin_to_user_dependency():
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    plan = _minimal_plan(
        stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False},
    )
    operations = build_operations(plan, cluster_id=CLUSTER_ID)

    # Full cluster-admin kubeconfig runs all LWS operations directly as user operations.
    ready_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-ready",
        runner=FakeRunner(),
    )
    assert ready_report.status == "ready"
    assert ready_report.admin_operations == []
    assert {"lws.namespace", "lws.crds", "lws.controller"} <= {entry["id"] for entry in ready_report.user_operations}

    # Partial RBAC: when lws.controller is denied while namespaces/CRDs are allowed,
    # reverse dependency closure promotes lws.namespace and lws.crds into admin_operations.
    partial_runner = FakeRunner(
        deny=lambda spec: _resource_attributes(spec).get("group") == "admissionregistration.k8s.io"
    )
    partial_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-partial",
        runner=partial_runner,
    )
    admin_order = [entry["id"] for entry in partial_report.admin_operations]
    user_ids = {entry["id"] for entry in partial_report.user_operations}
    assert partial_report.status == "admin_required"
    assert admin_order.index("lws.namespace") < admin_order.index("lws.crds") < admin_order.index("lws.controller")
    assert "lws.controller" not in user_ids
    assert all(
        dependency in set(admin_order)
        for operation in operations
        if operation.id in set(admin_order)
        for dependency in operation.depends_on
    )


@pytest.mark.asyncio
async def test_gateway_and_storage_namespace_splits_avoid_admin_to_user_errors():
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    plan = _minimal_plan(
        gateway={
            "provider": "istio",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
        },
        storage={
            "kind": "nfs",
            "namespace": "llm-d-bench-storage",
            "namespaceMode": "create",
            "nfsServer": "nfs.example.com",
            "nfsPath": "/models",
            "capacity": "10Gi",
        },
    )
    operations = build_operations(plan, cluster_id=CLUSTER_ID)

    # Full cluster-admin passes directly as ready with zero admin operations.
    ready_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-ready",
        runner=FakeRunner(),
    )
    assert ready_report.status == "ready"
    assert ready_report.admin_operations == []
    ready_user_ids = {entry["id"] for entry in ready_report.user_operations}
    assert {
        "gateway.namespace",
        "gateway.manifests",
        "storage.namespace",
        "storage.pv",
        "storage.pvc",
    } <= ready_user_ids

    # Partial RBAC: denying gateway.manifests / storage.pvc while namespaces is allowed
    # promotes gateway.namespace, gateway.crds, gateway.provider.0, storage.namespace, storage.pv cleanly.
    partial_runner = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("resource") in {"gateways", "persistentvolumeclaims"}
            and _resource_attributes(spec).get("verb") == "create"
        )
    )
    partial_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-partial",
        runner=partial_runner,
    )
    admin_order = [entry["id"] for entry in partial_report.admin_operations]
    admin_ids = set(admin_order)
    assert partial_report.status == "admin_required"
    assert {"gateway.namespace", "gateway.manifests", "storage.namespace", "storage.pv", "storage.pvc"} <= admin_ids
    assert admin_order.index("gateway.namespace") < admin_order.index("gateway.manifests")
    assert admin_order.index("storage.namespace") < admin_order.index("storage.pvc")
    assert admin_order.index("storage.pv") < admin_order.index("storage.pvc")
    assert all(
        dependency in admin_ids
        for operation in operations
        if operation.id in admin_ids
        for dependency in operation.depends_on
    )


@pytest.mark.asyncio
async def test_check_hf_secret_namespaces_batches_verbs_and_requires_all_three():
    from llm_d_lens.cluster.evaluator import check_hf_secret_namespaces

    runner = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("namespace") == "ns-default"
            and _resource_attributes(spec).get("verb") == "patch"
        ),
    )
    result = await check_hf_secret_namespaces(
        CLUSTER_ID,
        ["ns-default", "ns-writable", "ns-default", ""],
        runner=runner,
    )

    assert result == {"ns-default": False, "ns-writable": True}
    assert len(runner.calls) == 1
    submitted = [
        (
            _resource_attributes(item["spec"]).get("namespace"),
            _resource_attributes(item["spec"]).get("group"),
            _resource_attributes(item["spec"]).get("resource"),
            _resource_attributes(item["spec"]).get("verb"),
            _resource_attributes(item["spec"]).get("name"),
        )
        for item in runner.calls[0]["items"]
    ]
    assert submitted == [
        ("ns-default", "", "secrets", "get", None),
        ("ns-default", "", "secrets", "create", None),
        ("ns-default", "", "secrets", "patch", None),
        ("ns-writable", "", "secrets", "get", None),
        ("ns-writable", "", "secrets", "create", None),
        ("ns-writable", "", "secrets", "patch", None),
    ]

    empty_runner = FakeRunner()
    assert await check_hf_secret_namespaces(CLUSTER_ID, [], runner=empty_runner) == {}
    assert empty_runner.calls == []


@pytest.mark.asyncio
async def test_check_hf_secret_namespaces_enforces_egress_and_wraps_runner_errors(monkeypatch):
    from llm_d_lens.cluster.egress import EgressBlockedError
    from llm_d_lens.cluster.evaluator import check_hf_secret_namespaces

    def _blocked(_cluster_id: str) -> None:
        raise EgressBlockedError("loopback", message="blocked loopback")

    monkeypatch.setattr(evaluator, "validate_stored_cluster_egress", _blocked)
    with pytest.raises(EgressBlockedError):
        await check_hf_secret_namespaces(CLUSTER_ID, ["lens-edit"])

    monkeypatch.setattr(evaluator, "validate_stored_cluster_egress", lambda _cid: None)

    def _broken_default_runner(_cluster_id: str):
        raise ValueError("unexpected runner error")

    monkeypatch.setattr(evaluator, "_default_runner", _broken_default_runner)
    with pytest.raises(CapabilityCheckUnavailable, match="HF secret namespace check failed"):
        await check_hf_secret_namespaces(CLUSTER_ID, ["lens-edit"])

    with pytest.raises(CapabilityCheckUnavailable, match="HF secret namespace check failed"):
        await check_hf_secret_namespaces(CLUSTER_ID, ["lens-edit"], runner=FakeRunner(error=RuntimeError("boom")))


@pytest.mark.asyncio
async def test_check_storage_pvc_namespaces_batches_verbs_and_enforces_egress(monkeypatch):
    from llm_d_lens.cluster.egress import EgressBlockedError
    from llm_d_lens.cluster.evaluator import check_storage_pvc_namespaces

    runner = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("namespace") == "ns-default"
            and _resource_attributes(spec).get("verb") == "create"
        ),
    )
    result = await check_storage_pvc_namespaces(
        CLUSTER_ID,
        ["ns-default", "lens-edit", "ns-default", ""],
        runner=runner,
    )

    assert result == {"ns-default": False, "lens-edit": True}
    assert len(runner.calls) == 1
    submitted = [
        (
            _resource_attributes(item["spec"]).get("namespace"),
            _resource_attributes(item["spec"]).get("group"),
            _resource_attributes(item["spec"]).get("resource"),
            _resource_attributes(item["spec"]).get("verb"),
        )
        for item in runner.calls[0]["items"]
    ]
    assert submitted == [
        ("ns-default", "", "persistentvolumeclaims", "get"),
        ("ns-default", "", "persistentvolumeclaims", "create"),
        ("ns-default", "", "persistentvolumeclaims", "patch"),
        ("lens-edit", "", "persistentvolumeclaims", "get"),
        ("lens-edit", "", "persistentvolumeclaims", "create"),
        ("lens-edit", "", "persistentvolumeclaims", "patch"),
    ]

    empty_runner = FakeRunner()
    assert await check_storage_pvc_namespaces(CLUSTER_ID, [], runner=empty_runner) == {}
    assert empty_runner.calls == []

    monkeypatch.setattr(
        evaluator,
        "validate_stored_cluster_egress",
        lambda _cid: (_ for _ in ()).throw(EgressBlockedError("loopback", message="blocked")),
    )
    with pytest.raises(EgressBlockedError):
        await check_storage_pvc_namespaces(CLUSTER_ID, ["lens-edit"])

    monkeypatch.setattr(evaluator, "validate_stored_cluster_egress", lambda _cid: None)
    with pytest.raises(CapabilityCheckUnavailable, match="Storage PVC namespace check failed"):
        await check_storage_pvc_namespaces(CLUSTER_ID, ["lens-edit"], runner=FakeRunner(error=RuntimeError("boom")))


@pytest.mark.asyncio
async def test_custom_storage_namespace_targets_capabilities_and_non_admin_operation_split():
    from llm_d_lens.cluster.capabilities import (
        MODEL_CACHE_TOKEN_SECRET,
        STORAGE_NAMESPACE,
        STORAGE_NAMESPACE_PREFIX,
        _model_cache_namespace_for_plan,
        _storage_namespace_for_plan,
        check_rules_for_plan,
        unit_namespaces,
    )
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    assert STORAGE_NAMESPACE == "llm-d-bench-storage"
    assert STORAGE_NAMESPACE_PREFIX == "lens-storage"

    plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "team-inference", "namespaceMode": "existing", "name": "gw"},
        storage={
            "kind": "local-disk",
            "namespace": "team-inference",
            "namespaceMode": "existing",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "team-inference",
            "secretNamespaceMode": "existing",
        },
        modelCache={"enabled": True, "repoId": "org/model", "revision": "main", "storageVolumeRef": "v"},
    )
    assert _model_cache_namespace_for_plan(plan) == "team-inference"
    assert unit_namespaces(plan)["cache.job"] == ("team-inference",)
    cache_secret_checks = {
        (rule.verb, rule.namespace, rule.name)
        for unit_id, rule in check_rules_for_plan(plan, ("cache.job",))
        if unit_id == "cache.job" and rule.resource == "secrets"
    }
    assert (
        "create",
        "team-inference",
        MODEL_CACHE_TOKEN_SECRET,
    ) in cache_secret_checks

    unnormalized_plan = plan.model_copy(update={"storage": plan.storage.model_copy(update={"namespace": None})})
    assert _storage_namespace_for_plan(unnormalized_plan) == STORAGE_NAMESPACE_PREFIX
    assert _model_cache_namespace_for_plan(unnormalized_plan) == STORAGE_NAMESPACE_PREFIX
    assert any(
        rule.resource == "secrets"
        and rule.name == MODEL_CACHE_TOKEN_SECRET
        and rule.namespace == STORAGE_NAMESPACE_PREFIX
        for _unit, rule in check_rules_for_plan(unnormalized_plan, ("cache.job",))
    )

    operations = build_operations(plan, cluster_id=CLUSTER_ID)
    runner = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("resource") in {"persistentvolumes", "nodes"}
            or (_resource_attributes(spec).get("namespace", "") not in {"", "team-inference"})
        ),
    )
    report = await evaluate_operations(CLUSTER_ID, operations, plan_hash="plan-hash", runner=runner)

    admin_ids = {entry["id"] for entry in report.admin_operations}
    user_ids = {entry["id"] for entry in report.user_operations}
    lens_ids = {entry["id"] for entry in report.lens_operations}
    assert "storage.namespace" not in admin_ids | user_ids | lens_ids
    assert "storage.pv" in admin_ids
    assert "storage.pvc" in user_ids
    assert "model-cache.job" in lens_ids
    assert report.blocked_operations == []


def test_operation_report_plan_defaults_to_none():
    from datetime import UTC, datetime

    from llm_d_lens.cluster.evaluator import OperationReport

    report = OperationReport(
        plan_hash="abc",
        plan_version=5,
        status="ready",
        sufficient=True,
        checked_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert report.plan is None
    assert report.model_dump(by_alias=True)["plan"] is None
    with_plan = report.model_copy(update={"plan": {"planVersion": 5}})
    assert with_plan.model_dump(by_alias=True)["plan"] == {"planVersion": 5}


@pytest.mark.asyncio
async def test_evaluate_operations_blocks_lens_operation_when_checks_are_denied():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    lens_allowed = _op(
        "gateway.exposure",
        executor="lens",
        apply_kind="lens",
        namespace="lens-gateway",
        checks=(
            CheckRule(
                verb="create",
                api_group="",
                resource="pods",
                subresource="portforward",
                scope="namespace",
                namespace="lens-gateway",
            ),
        ),
    )
    allowed_report = await evaluate_operations(
        CLUSTER_ID,
        [lens_allowed],
        plan_hash="plan-hash",
        runner=FakeRunner(),
        use_cache=False,
    )
    assert allowed_report.status == "ready"
    assert allowed_report.sufficient is True
    assert [item["id"] for item in allowed_report.lens_operations] == ["gateway.exposure"]
    assert allowed_report.blocked_operations == []

    denied_report = await evaluate_operations(
        CLUSTER_ID,
        [lens_allowed],
        plan_hash="plan-hash",
        runner=FakeRunner(deny=lambda spec: _resource_attributes(spec).get("subresource") == "portforward"),
        use_cache=False,
    )
    assert denied_report.status == "blocked"
    assert denied_report.sufficient is False
    assert denied_report.lens_operations == []
    assert len(denied_report.blocked_operations) == 1
    blocked = denied_report.blocked_operations[0]
    assert blocked["id"] == "gateway.exposure"
    assert blocked["executor"] == "lens"
    assert blocked["missing"] == [
        {
            "verb": "create",
            "apiGroup": "",
            "resource": "pods",
            "scope": "namespace",
            "namespace": "lens-gateway",
            "subresource": "portforward",
        }
    ]
    assert denied_report.missing == [
        {
            "operationId": "gateway.exposure",
            "verb": "create",
            "apiGroup": "",
            "resource": "pods",
            "scope": "namespace",
            "namespace": "lens-gateway",
            "subresource": "portforward",
        }
    ]

    unbound_lens = _op(
        "lens.unbound",
        executor="lens",
        apply_kind="lens",
        namespace=None,
        checks=(CheckRule(verb="get", api_group="", resource="secrets", scope="namespace"),),
    )
    unbound_report = await evaluate_operations(
        CLUSTER_ID,
        [unbound_lens],
        plan_hash="plan-hash",
        runner=FakeRunner(),
        use_cache=False,
    )
    assert unbound_report.status == "blocked"
    assert unbound_report.sufficient is False
    assert unbound_report.lens_operations == []
    assert unbound_report.blocked_operations[0]["id"] == "lens.unbound"
    assert unbound_report.blocked_operations[0]["executor"] == "lens"
    assert unbound_report.blocked_operations[0]["missing"][0]["reason"] == "namespace target is not bound"


@pytest.mark.asyncio
async def test_marker_defensively_parses_monitoring_endpoints():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    operation = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
    )

    valid_runner = MarkerRunner(
        {
            "planHash": "plan-hash",
            "status": "complete",
            "operations": "admin.op",
            "prometheusNodePort": "30090",
            "grafanaNodePort": "30300",
            "monitoringHost": "10.0.0.15",
        },
        deny=lambda _spec: True,
    )
    valid_report = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=valid_runner,
        use_cache=False,
    )
    assert valid_report.marker["monitoring"] == {
        "prometheusNodePort": 30090,
        "grafanaNodePort": 30300,
        "monitoringHost": "10.0.0.15",
    }

    # Invalid ports, equal ports, and SSRF/malformed hosts are discarded defensively.
    bad_runner = MarkerRunner(
        {
            "planHash": "plan-hash",
            "status": "complete",
            "operations": "admin.op",
            "prometheusNodePort": "80",
            "grafanaNodePort": "not-a-port",
            "monitoringHost": "169.254.169.254",
        },
        deny=lambda _spec: True,
    )
    bad_report = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=bad_runner,
        use_cache=False,
    )
    assert "monitoring" not in bad_report.marker

    equal_ports_runner = MarkerRunner(
        {
            "planHash": "plan-hash",
            "status": "complete",
            "operations": "admin.op",
            "prometheusNodePort": "30090",
            "grafanaNodePort": "30090",
            "monitoringHost": "http://10.0.0.15/bad",
        },
        deny=lambda _spec: True,
    )
    equal_report = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=equal_ports_runner,
        use_cache=False,
    )
    assert "monitoring" not in equal_report.marker


@pytest.mark.asyncio
async def test_monitoring_operations_admin_vs_user_dag_split_and_partial_rbac():
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    plan = _minimal_plan(
        accelerators=[{"vendor": "nvidia", "mode": "plugin"}],
        monitoring={"mode": "managed", "installAcceleratorExporters": True},
    )
    operations = build_operations(plan, cluster_id=CLUSTER_ID)

    # 1. ClusterAdmin (all SSAR allowed): stack + exporter -> user, verify -> lens.
    admin_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-admin",
        runner=FakeRunner(),
        use_cache=False,
    )
    assert admin_report.status == "ready"
    assert admin_report.blocked_operations == []
    assert {"monitoring.stack", "monitoring.accelerator.nvidia_gpu"} <= {
        op["id"] for op in admin_report.user_operations
    }
    assert "monitoring.verify" in {op["id"] for op in admin_report.lens_operations}

    # 2. Restricted edit kubeconfig (cluster-scoped checks denied): stack + exporter -> admin, verify -> lens.
    restricted_runner = FakeRunner(deny=lambda spec: not _resource_attributes(spec).get("namespace"))
    restricted_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-restricted",
        runner=restricted_runner,
        use_cache=False,
    )
    assert restricted_report.status == "admin_required"
    assert restricted_report.blocked_operations == []
    assert {"monitoring.stack", "monitoring.accelerator.nvidia_gpu"} <= {
        op["id"] for op in restricted_report.admin_operations
    }
    assert "monitoring.verify" in {op["id"] for op in restricted_report.lens_operations}

    # 3. Contrived partial RBAC: driver op allowed (user) while exporter namespace denied (admin).
    # Must NOT raise OperationDependencyError because monitoring.accelerator.* does not depend on driver ops.
    partial_runner = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("namespace") == f"lens-monitoring-{CLUSTER_ID}"
            and _resource_attributes(spec).get("resource") == "configmaps"
        )
    )
    partial_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="plan-hash-partial",
        runner=partial_runner,
        use_cache=False,
    )
    assert "accelerator.nvidia.plugin.0" in {op["id"] for op in partial_report.user_operations}
    assert {"monitoring.stack", "monitoring.accelerator.nvidia_gpu"} <= {
        op["id"] for op in partial_report.admin_operations
    }


def test_admin_handoff_status_response_accepts_optional_monitoring():
    from llm_d_lens.cluster.models import AdminHandoffStatusResponse

    default_resp = AdminHandoffStatusResponse(
        planHash="abc",
        status="pending",
        adminOperations=["monitoring.stack"],
        remainingOperations=["monitoring.stack"],
        marker={"status": "missing"},
    )
    assert default_resp.monitoring is None

    with_mon = AdminHandoffStatusResponse(
        planHash="abc",
        status="verified",
        adminOperations=["monitoring.stack"],
        remainingOperations=[],
        marker={"status": "complete"},
        monitoring={"prometheusUrl": "http://10.0.0.5:30090", "checked": True, "healthy": True},
    )
    assert with_mon.model_dump(by_alias=True)["monitoring"]["healthy"] is True


@pytest.mark.asyncio
async def test_read_marker_with_empty_plan_hash_returns_missing_never_partial():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    operation = _op(
        "admin.op",
        namespace=None,
        checks=(CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster"),),
    )

    empty_hash_partial = MarkerRunner(
        {"planHash": "", "status": "partial", "operations": "admin.op"},
        deny=lambda _spec: True,
    )
    report_empty = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=empty_hash_partial,
        use_cache=False,
    )
    assert report_empty.marker["status"] == "missing"
    assert report_empty.marker["verified"] is False

    missing_hash_complete = MarkerRunner(
        {"status": "complete", "operations": "admin.op"},
        deny=lambda _spec: True,
    )
    report_missing = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=missing_hash_complete,
        use_cache=False,
    )
    assert report_missing.marker["status"] == "missing"
    assert report_missing.marker["verified"] is False

    mismatched_hash = MarkerRunner(
        {"planHash": "other-hash", "status": "complete", "operations": "admin.op"},
        deny=lambda _spec: True,
    )
    report_stale = await evaluate_operations(
        CLUSTER_ID,
        [operation],
        plan_hash="plan-hash",
        handoff_namespace="lens-edit",
        runner=mismatched_hash,
        use_cache=False,
    )
    assert report_stale.marker["status"] == "stale"
    assert report_stale.marker["verified"] is False


@pytest.mark.asyncio
async def test_apply_live_prometheus_satisfaction_skips_ineligible_markers_without_http(monkeypatch):
    from llm_d_lens.cluster.evaluator import apply_live_prometheus_satisfaction
    from llm_d_lens.monitoring import prometheus as prom_mod

    called = False

    def fail_client(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("create_prometheus_client should not be called")

    monkeypatch.setattr(prom_mod, "create_prometheus_client", fail_client)

    for bad_status in ("missing", "stale", "unconfirmed", "unknown"):
        marker = {
            "namespace": "default",
            "name": "lens-admin-handoff-abc",
            "status": bad_status,
            "verified": False,
            "operations": [],
        }
        res = await apply_live_prometheus_satisfaction(
            marker,
            admin_operation_ids=["monitoring.stack", "monitoring.accelerator.intel_gpu"],
            prometheus_url="http://10.0.0.15:30090",
        )
        assert res == marker
        assert called is False

    # No probeable ops remaining -> zero HTTP calls
    partial_non_probeable = {
        "namespace": "default",
        "name": "lens-admin-handoff-abc",
        "status": "partial",
        "verified": False,
        "operations": ["monitoring.stack"],
    }
    res_non_probeable = await apply_live_prometheus_satisfaction(
        partial_non_probeable,
        admin_operation_ids=["monitoring.stack", "monitoring.rbac", "gateway.manifests"],
        prometheus_url="http://10.0.0.15:30090",
    )
    assert res_non_probeable == partial_non_probeable
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "metric_name"),
    [
        ("intel_gpu", "hw_gpu_utilization_ratio"),
        ("nvidia_gpu", "DCGM_FI_DEV_GPU_UTIL"),
    ],
)
async def test_apply_live_prometheus_satisfaction_promotes_accelerator_via_profiles(
    monkeypatch,
    provider_id: str,
    metric_name: str,
):
    import httpx

    from llm_d_lens.cluster.evaluator import apply_live_prometheus_satisfaction
    from llm_d_lens.monitoring import prometheus as prom_mod

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        if q == f"count({metric_name})":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "4"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        ),
    )

    marker = {
        "namespace": "default",
        "name": "lens-admin-handoff-abc",
        "status": "partial",
        "verified": False,
        "operations": ["monitoring.stack"],
        "monitoring": {"prometheusNodePort": 30090, "monitoringHost": "10.0.0.15"},
    }
    op_id = f"monitoring.accelerator.{provider_id}"
    res = await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=["monitoring.stack", op_id],
        prometheus_url="http://10.0.0.15:30090",
    )
    assert res["status"] == "complete"
    assert res["verified"] is True
    assert res["operations"] == ["monitoring.stack", op_id]
    assert res["monitoring"] == marker["monitoring"]


@pytest.mark.asyncio
async def test_apply_live_prometheus_satisfaction_fails_closed_on_zero_empty_or_unknown_provider(monkeypatch):
    import httpx

    from llm_d_lens.cluster.evaluator import apply_live_prometheus_satisfaction
    from llm_d_lens.monitoring import prometheus as prom_mod
    from llm_d_lens.monitoring import service_links

    def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(empty_handler),
        ),
    )

    marker = {
        "namespace": "default",
        "name": "lens-admin-handoff-abc",
        "status": "partial",
        "verified": False,
        "operations": ["monitoring.stack"],
    }
    # Empty Prometheus response -> stays partial
    res_empty = await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=["monitoring.stack", "monitoring.accelerator.intel_gpu"],
        prometheus_url="http://10.0.0.15:30090",
    )
    assert res_empty == marker

    # Unknown provider -> stays partial
    res_unknown = await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=["monitoring.stack", "monitoring.accelerator.unknown_vendor"],
        prometheus_url="http://10.0.0.15:30090",
    )
    assert res_unknown == marker

    # detected=True but series_count=0 -> stays partial
    async def zero_series_probe(_client, _profile):
        return {"detected": True, "series_count": 0, "node_count": None, "metrics_found": [], "source_label": "x"}

    monkeypatch.setattr(service_links, "probe_accelerator_metrics_via_prometheus", zero_series_probe)
    res_zero = await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=["monitoring.stack", "monitoring.accelerator.nvidia_gpu"],
        prometheus_url="http://10.0.0.15:30090",
    )
    assert res_zero == marker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kube_count", "cpu_count", "uname_count", "expected_complete"),
    [
        ("2", "4", "0", True),
        ("2", "0", "2", True),
        ("0", "4", "2", False),
        ("2", "0", "0", False),
    ],
)
async def test_apply_live_prometheus_satisfaction_monitoring_stack_requires_kube_and_node(
    monkeypatch,
    kube_count: str,
    cpu_count: str,
    uname_count: str,
    expected_complete: bool,
):
    import httpx

    from llm_d_lens.cluster.evaluator import apply_live_prometheus_satisfaction
    from llm_d_lens.monitoring import prometheus as prom_mod

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        mapping = {
            "count(kube_node_info)": kube_count,
            "count(node_cpu_seconds_total)": cpu_count,
            "count(node_uname_info)": uname_count,
        }
        val = mapping.get(q, "0")
        rows = [{"value": [0, val]}] if int(val) > 0 else []
        return httpx.Response(200, json={"status": "success", "data": {"result": rows}})

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        ),
    )

    marker = {
        "namespace": "default",
        "name": "lens-admin-handoff-abc",
        "status": "partial",
        "verified": False,
        "operations": [],
    }
    res = await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=["monitoring.stack"],
        prometheus_url="http://10.0.0.15:30090",
    )
    if expected_complete:
        assert res["status"] == "complete"
        assert res["verified"] is True
        assert res["operations"] == ["monitoring.stack"]
    else:
        assert res == marker


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking_op", ["monitoring.rbac", "gateway.manifests"])
async def test_apply_live_prometheus_satisfaction_never_spoofs_rbac_or_non_monitoring_ops(
    monkeypatch,
    blocking_op: str,
):
    import httpx

    from llm_d_lens.cluster.evaluator import apply_live_prometheus_satisfaction
    from llm_d_lens.monitoring import prometheus as prom_mod

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        if q in {"count(hw_gpu_utilization_ratio)", "count(DCGM_FI_DEV_GPU_UTIL)"}:
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "4"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        ),
    )

    marker = {
        "namespace": "default",
        "name": "lens-admin-handoff-abc",
        "status": "partial",
        "verified": False,
        "operations": [],
    }
    res = await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=[blocking_op, "monitoring.accelerator.intel_gpu"],
        prometheus_url="http://10.0.0.15:30090",
    )
    assert res["status"] == "partial"
    assert res["verified"] is False
    assert res["operations"] == ["monitoring.accelerator.intel_gpu"]


@pytest.mark.asyncio
async def test_apply_live_prometheus_satisfaction_fails_closed_on_ssrf_timeout_http_error_and_bad_json(monkeypatch):
    import httpx

    from llm_d_lens.cluster.evaluator import apply_live_prometheus_satisfaction
    from llm_d_lens.monitoring import prometheus as prom_mod

    marker = {
        "namespace": "default",
        "name": "lens-admin-handoff-abc",
        "status": "partial",
        "verified": False,
        "operations": ["monitoring.stack"],
    }
    admin_ops = ["monitoring.stack", "monitoring.accelerator.intel_gpu"]

    # 1. SSRF metadata target rejected by real _ensure_safe_prometheus_url
    res_ssrf = await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=admin_ops,
        prometheus_url="http://169.254.169.254:30090",
    )
    assert res_ssrf == marker

    # 2. Timeout
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(timeout_handler),
        ),
    )
    assert (
        await apply_live_prometheus_satisfaction(
            marker,
            admin_operation_ids=admin_ops,
            prometheus_url="http://10.0.0.15:30090",
        )
        == marker
    )

    # 3. HTTP 500
    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(lambda _req: httpx.Response(500, text="error")),
        ),
    )
    assert (
        await apply_live_prometheus_satisfaction(
            marker,
            admin_operation_ids=admin_ops,
            prometheus_url="http://10.0.0.15:30090",
        )
        == marker
    )

    # 4. Invalid JSON
    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, text="not-json")),
        ),
    )
    assert (
        await apply_live_prometheus_satisfaction(
            marker,
            admin_operation_ids=admin_ops,
            prometheus_url="http://10.0.0.15:30090",
        )
        == marker
    )


def test_promote_admin_dependencies_respects_completed_blocked_prohibited_and_unknown_seeds():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import promote_admin_dependencies
    from llm_d_lens.cluster.operations import ApplyOptions, SecretCopy

    chk = (CheckRule(verb="get", api_group="", resource="configmaps", scope="namespace"),)
    op_a = _op("op.a", checks=chk)
    op_b = _op("op.b", checks=chk, depends_on=("op.a",))
    op_c = _op("op.c", checks=chk, depends_on=("op.b",))
    ordered = (op_a, op_b, op_c)

    # Empty seed returns empty tuple.
    assert promote_admin_dependencies(ordered, ()) == ()

    # Transitive closure in topological order (even if caller passed unordered ops).
    assert promote_admin_dependencies([op_c, op_a, op_b], ["op.c"]) == ("op.a", "op.b", "op.c")

    # Traversal halts at completed_ids so neither op.b nor its ancestor op.a is promoted.
    assert promote_admin_dependencies(ordered, ["op.c"], completed_ids={"op.b"}) == ("op.c",)

    # Blocked ops are never promoted.
    assert promote_admin_dependencies(ordered, ["op.c"], blocked_ids={"op.b"}) == ("op.c",)

    # Prohibited ops (lens, user_owned_secrets, secret_copy, delete_before_apply) are never promoted.
    secret_op = _op("secret.op", checks=chk, user_owned_secrets=("s",))
    copy_op = _op("copy.op", checks=chk, secret_copy=SecretCopy("a", "b", "c", "d"))
    del_op = _op("del.op", checks=chk, apply_options=ApplyOptions(delete_before_apply=True))
    lens_op = _op("lens.op", executor="lens", apply_kind="lens", namespace=None)
    child = _op("child.op", checks=chk, depends_on=("secret.op", "copy.op", "del.op", "lens.op", "op.a"))
    assert promote_admin_dependencies(
        (secret_op, copy_op, del_op, lens_op, op_a, child),
        ["child.op"],
    ) == ("op.a", "child.op")

    # Unknown mocked seed IDs keep their relative positions around known_ordered without duplicates.
    assert promote_admin_dependencies(
        ordered,
        ["mock.leading", "op.c", "mock.trailing", "mock.leading"],
    ) == ("op.a", "op.b", "mock.leading", "op.c", "mock.trailing")
    assert promote_admin_dependencies(ordered, ["mock.only"]) == ("mock.only",)


def test_validate_operation_split_allows_completed_and_fails_closed_on_other_categories():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import validate_operation_split
    from llm_d_lens.cluster.operations import OperationDependencyError

    chk = (CheckRule(verb="get", api_group="", resource="configmaps", scope="namespace"),)
    dep = _op("dep.op", checks=chk)
    admin = _op("admin.op", checks=chk, depends_on=("dep.op",))
    ops = (dep, admin)

    # Allowed when dep is in admin_ids or completed_ids.
    validate_operation_split(ops, user_ids=(), admin_ids=("dep.op", "admin.op"))
    validate_operation_split(ops, user_ids=("dep.op",), admin_ids=("admin.op",), completed_ids=("dep.op",))

    # Fails closed with specific dependency classification otherwise.
    with pytest.raises(OperationDependencyError, match="depends on user operation 'dep.op'"):
        validate_operation_split(ops, user_ids=("dep.op",), admin_ids=("admin.op",))
    with pytest.raises(OperationDependencyError, match="depends on blocked operation 'dep.op'"):
        validate_operation_split(ops, user_ids=(), admin_ids=("admin.op",), blocked_ids=("dep.op",))
    with pytest.raises(OperationDependencyError, match="depends on lens operation 'dep.op'"):
        validate_operation_split(ops, user_ids=(), admin_ids=("admin.op",), lens_ids=("dep.op",))
    with pytest.raises(OperationDependencyError, match="depends on non-admin operation 'dep.op'"):
        validate_operation_split(ops, user_ids=(), admin_ids=("admin.op",))


@pytest.mark.asyncio
async def test_large_plan_with_intel_plugin_cluster_admin_passes_ready_zero_handoff():
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    base = _large_plan()
    plan = _minimal_plan(
        gateway={"provider": "istio", "namespace": "lens-gateway", "namespaceMode": "create", "name": "lens-gateway"},
        accelerators=[
            {"vendor": "nvidia", "mode": "plugin", "rbac": ["escalation"]},
            {"vendor": "intel", "mode": "plugin", "rbac": ["namespaced", "cluster"]},
        ],
        storage=_model_cache_storage(namespaceMode="create"),
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "team-b",
            "secretNamespaceMode": "create",
        },
        modelCache={"enabled": True, "repoId": "org/model", "revision": "main", "storageVolumeRef": "v"},
        stack={
            "lwsEnabled": True,
            "crdsEnabled": True,
            "imagePrepullEnabled": True,
            "images": ["ghcr.io/example/router:v1"],
            "imagesNamespace": "lens-images",
            "imagesNamespaceMode": "create",
        },
        runtime={"inotifyEnabled": True},
        monitoring=base.monitoring.model_dump(mode="json", by_alias=True),
    )
    operations = build_operations(plan, cluster_id=CLUSTER_ID)
    report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="large-ready-hash",
        runner=FakeRunner(),
    )

    assert report.status == "ready"
    assert report.sufficient is True
    assert report.admin_operations == []
    assert report.blocked_operations == []
    assert report.missing == []
    user_ids = {entry["id"] for entry in report.user_operations}
    assert {
        "gateway.namespace",
        "lws.namespace",
        "lws.crds",
        "hf-secret.namespace",
        "images.namespace",
        "storage.namespace",
        "accelerator.nfd",
        "accelerator.nfd.rules",
        "accelerator.intel.plugin.0",
    } <= user_ids


@pytest.mark.asyncio
async def test_wildcard_probe_never_bypasses_secret_lens_or_admin_only_and_cache_tracks_flags():
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import evaluate_operations

    unbound_check = (CheckRule(verb="get", api_group="", resource="secrets", scope="namespace"),)

    # 1. user_owned_secrets + requires_cluster_wildcard + unbound check stays blocked even when */*/* is allowed.
    secret_wildcard = _op(
        "secret.wildcard",
        namespace=None,
        checks=unbound_check,
        user_owned_secrets=("hf-token",),
        requires_cluster_wildcard=True,
    )
    secret_report = await evaluate_operations(
        CLUSTER_ID,
        [secret_wildcard],
        plan_hash="sec-wc",
        runner=FakeRunner(),
    )
    assert secret_report.status == "blocked"
    assert [item["id"] for item in secret_report.blocked_operations] == ["secret.wildcard"]
    assert secret_report.user_operations == []
    assert secret_report.admin_operations == []

    # 2. lens + requires_cluster_wildcard + unbound check stays blocked even when */*/* is allowed.
    lens_wildcard = _op(
        "lens.wildcard",
        namespace=None,
        checks=unbound_check,
        executor="lens",
        apply_kind="lens",
        requires_cluster_wildcard=True,
    )
    lens_report = await evaluate_operations(
        CLUSTER_ID,
        [lens_wildcard],
        plan_hash="lens-wc",
        runner=FakeRunner(),
    )
    assert lens_report.status == "blocked"
    assert [item["id"] for item in lens_report.blocked_operations] == ["lens.wildcard"]

    # 3. admin_only=True + requires_cluster_wildcard=True stays admin even when */*/* is allowed,
    # and toggling flags on the same op.id + plan_hash does NOT collide in _OPERATION_CACHE.
    bound_check = (CheckRule(verb="get", api_group="", resource="namespaces", scope="cluster"),)
    pinned_admin = _op(
        "same.id",
        namespace=None,
        checks=bound_check,
        admin_only=True,
        requires_cluster_wildcard=True,
    )
    unpinned_wildcard = _op(
        "same.id",
        namespace=None,
        checks=bound_check,
        admin_only=False,
        requires_cluster_wildcard=True,
    )
    unpinned_plain = _op(
        "same.id",
        namespace=None,
        checks=bound_check,
        admin_only=False,
        requires_cluster_wildcard=False,
    )
    r_pinned = await evaluate_operations(CLUSTER_ID, [pinned_admin], plan_hash="same-hash", runner=FakeRunner())
    r_wildcard = await evaluate_operations(
        CLUSTER_ID,
        [unpinned_wildcard],
        plan_hash="same-hash",
        runner=FakeRunner(deny=lambda spec: _resource(spec)[:3] == ("*", "*", "*")),
    )
    r_plain = await evaluate_operations(
        CLUSTER_ID,
        [unpinned_plain],
        plan_hash="same-hash",
        runner=FakeRunner(deny=lambda spec: _resource(spec)[:3] == ("*", "*", "*")),
    )
    assert r_pinned.status == "admin_required"
    assert r_wildcard.status == "admin_required"
    assert r_plain.status == "ready"


def test_storage_operator_role_and_binding_manifests_and_rule_matcher():
    from llm_d_lens.cluster.evaluator import (
        STORAGE_OPERATOR_ROLE_NAME,
        _storage_operator_role_grants,
        storage_operator_role_binding_manifest,
        storage_operator_role_manifest,
    )

    role = storage_operator_role_manifest("lens-storage-283db094")
    assert role["apiVersion"] == "rbac.authorization.k8s.io/v1"
    assert role["kind"] == "Role"
    assert role["metadata"] == {
        "name": STORAGE_OPERATOR_ROLE_NAME,
        "namespace": "lens-storage-283db094",
        "labels": {"app.kubernetes.io/managed-by": "lens"},
    }
    assert len(role["rules"]) == 2
    assert _storage_operator_role_grants("", "pods", "log", "get") is True
    assert _storage_operator_role_grants("", "pods", "exec", "create") is False
    assert _storage_operator_role_grants("", "persistentvolumeclaims", "", "create") is True
    assert _storage_operator_role_grants("batch", "jobs", None, "delete") is True
    assert _storage_operator_role_grants("", "secrets", "", "patch") is True
    assert _storage_operator_role_grants("", "nodes", "", "get") is False

    for bad_ns in ("", "Bad-NS", "ns\n", "a" * 64):
        with pytest.raises(ValueError, match="DNS-1123"):
            storage_operator_role_manifest(bad_ns)
        with pytest.raises(ValueError, match="DNS-1123"):
            storage_operator_role_binding_manifest(bad_ns, None)

    assert storage_operator_role_binding_manifest("lens-storage-283db094", None) is None
    assert storage_operator_role_binding_manifest("lens-storage-283db094", {"kind": "unknown"}) is None

    sa_binding = storage_operator_role_binding_manifest(
        "lens-storage-283db094",
        {"kind": "ServiceAccount", "name": "runner-sa", "namespace": "team-inference"},
    )
    assert sa_binding["kind"] == "RoleBinding"
    assert sa_binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": STORAGE_OPERATOR_ROLE_NAME,
    }
    assert sa_binding["subjects"] == [{"kind": "ServiceAccount", "name": "runner-sa", "namespace": "team-inference"}]

    user_binding = storage_operator_role_binding_manifest(
        "lens-storage-283db094",
        {"kind": "User", "name": "alice@example.com"},
    )
    assert user_binding["subjects"] == [
        {"kind": "User", "name": "alice@example.com", "apiGroup": "rbac.authorization.k8s.io"}
    ]

    group_binding = storage_operator_role_binding_manifest(
        "lens-storage-283db094",
        {"kind": "Group", "name": "oidc:ml-platform"},
    )
    assert group_binding["subjects"] == [
        {"kind": "Group", "name": "oidc:ml-platform", "apiGroup": "rbac.authorization.k8s.io"}
    ]

    for bad_identity in (
        {"kind": "ServiceAccount", "name": "Bad_SA", "namespace": "team-inference"},
        {"kind": "ServiceAccount", "name": "runner-sa", "namespace": "bad;ns"},
        {"kind": "User", "name": "alice; $(id)"},
        {"kind": "User", "name": "alice\nbob"},
        {"kind": "User", "name": "system:anonymous"},
        {"kind": "Group", "name": " group "},
    ):
        with pytest.raises(ValueError):
            storage_operator_role_binding_manifest("lens-storage-283db094", bad_identity)


@pytest.mark.asyncio
async def test_check_storage_operator_permissions_batches_rules_and_enforces_egress(monkeypatch):
    from llm_d_lens.cluster.egress import EgressBlockedError
    from llm_d_lens.cluster.evaluator import check_storage_operator_permissions

    allow_runner = FakeRunner()
    assert await check_storage_operator_permissions(CLUSTER_ID, "lens-storage-283db094", runner=allow_runner) is True
    assert len(allow_runner.calls) == 1
    assert len(allow_runner.calls[0]["items"]) == 21

    deny_pod_log = FakeRunner(
        deny=lambda spec: (
            _resource_attributes(spec).get("resource") == "pods"
            and _resource_attributes(spec).get("subresource") == "log"
        )
    )
    assert await check_storage_operator_permissions(CLUSTER_ID, "lens-storage-283db094", runner=deny_pod_log) is False
    assert await check_storage_operator_permissions(CLUSTER_ID, "Bad-NS", runner=FakeRunner()) is False

    monkeypatch.setattr(
        evaluator,
        "validate_stored_cluster_egress",
        lambda _cid: (_ for _ in ()).throw(EgressBlockedError("loopback", message="blocked")),
    )
    with pytest.raises(EgressBlockedError):
        await check_storage_operator_permissions(CLUSTER_ID, "lens-storage-283db094")

    monkeypatch.setattr(evaluator, "validate_stored_cluster_egress", lambda _cid: None)
    with pytest.raises(CapabilityCheckUnavailable, match="Storage operator permission check failed"):
        await check_storage_operator_permissions(
            CLUSTER_ID,
            "lens-storage-283db094",
            runner=FakeRunner(error=RuntimeError("boom")),
        )


@pytest.mark.asyncio
async def test_detect_kubeconfig_subject_covers_ssr_whoami_kubeconfig_cert_and_security(monkeypatch, tmp_path):
    import base64
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from llm_d_lens.cluster import registry
    from llm_d_lens.cluster.egress import EgressBlockedError
    from llm_d_lens.cluster.evaluator import detect_kubeconfig_subject

    def _jwt(claims: dict[str, Any]) -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"{header}.{body}.sig"

    class _SubjectRunner:
        def __init__(
            self, *, ssr_out: str | None = None, ssr_rc: int = 1, whoami_out: str | None = None, whoami_rc: int = 1
        ):
            self.ssr_out = ssr_out
            self.ssr_rc = ssr_rc
            self.whoami_out = whoami_out
            self.whoami_rc = whoami_rc

        async def run(self, argv, **_kwargs):
            if tuple(argv[:2]) == ("kubectl", "create"):
                return CommandResult(tuple(argv), self.ssr_rc, self.ssr_out or "", "")
            if tuple(argv[:3]) == ("kubectl", "auth", "whoami"):
                return CommandResult(tuple(argv), self.whoami_rc, self.whoami_out or "", "")
            return CommandResult(tuple(argv), 1, "", "unexpected")

    # 1. SelfSubjectReview SA
    ssr_sa = json.dumps({"status": {"userInfo": {"username": "system:serviceaccount:team-inference:runner-sa"}}})
    res_sa = await detect_kubeconfig_subject(CLUSTER_ID, runner=_SubjectRunner(ssr_out=ssr_sa, ssr_rc=0))
    assert res_sa == {
        "kind": "ServiceAccount",
        "name": "runner-sa",
        "namespace": "team-inference",
        "subject": "system:serviceaccount:team-inference:runner-sa",
        "source": "self_subject_review",
        "overrideRequired": False,
    }

    # 2. Anonymous SSR falls back to whoami User
    ssr_anon = json.dumps({"status": {"userInfo": {"username": "system:anonymous"}}})
    whoami_user = json.dumps({"status": {"userInfo": {"username": "oidc:dev@example.com"}}})
    res_whoami = await detect_kubeconfig_subject(
        CLUSTER_ID,
        runner=_SubjectRunner(ssr_out=ssr_anon, ssr_rc=0, whoami_out=whoami_user, whoami_rc=0),
    )
    assert res_whoami["kind"] == "User"
    assert res_whoami["name"] == "oidc:dev@example.com"
    assert res_whoami["source"] == "whoami"

    # 3. Inline kubeconfig JWT fallback + tokenFile / client-certificate disk paths NEVER read
    secret_token_file = tmp_path / "disk-token"
    secret_token_file.write_text(_jwt({"sub": "system:serviceaccount:team-inference:should-not-read"}))
    kubeconfig_disk_only = (
        "apiVersion: v1\nkind: Config\ncurrent-context: ctx\n"
        "clusters:\n- name: c\n  cluster:\n    server: https://10.0.0.1:6443\n"
        "contexts:\n- name: ctx\n  context:\n    cluster: c\n    user: u\n"
        f"users:\n- name: u\n  user:\n    tokenFile: {secret_token_file}\n"
    )
    monkeypatch.setattr(registry, "read_kubeconfig", lambda _cid: kubeconfig_disk_only)
    res_disk = await detect_kubeconfig_subject(CLUSTER_ID, runner=_SubjectRunner())
    assert res_disk == {
        "kind": "unknown",
        "name": None,
        "namespace": None,
        "subject": None,
        "source": "none",
        "overrideRequired": True,
    }

    # Inline JWT claim fallback
    inline_jwt_kc = (
        "apiVersion: v1\nkind: Config\ncurrent-context: ctx\n"
        "contexts:\n- name: ctx\n  context:\n    user: u\n"
        "users:\n- name: u\n  user:\n"
        f"    token: {_jwt({'kubernetes.io/serviceaccount': {'namespace': 'team-inference', 'name': 'inline-sa'}})}\n"
    )
    monkeypatch.setattr(registry, "read_kubeconfig", lambda _cid: inline_jwt_kc)
    res_jwt = await detect_kubeconfig_subject(CLUSTER_ID, runner=_SubjectRunner())
    assert res_jwt["kind"] == "ServiceAccount"
    assert res_jwt["name"] == "inline-sa"
    assert res_jwt["namespace"] == "team-inference"
    assert res_jwt["source"] == "kubeconfig"

    # Inline client-certificate-data CN fallback
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cert-user@example.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(subject_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode("ascii")
    cert_kc = (
        "apiVersion: v1\nkind: Config\ncurrent-context: ctx\n"
        "contexts:\n- name: ctx\n  context:\n    user: u\n"
        f"users:\n- name: u\n  user:\n    client-certificate-data: {pem_b64}\n"
    )
    monkeypatch.setattr(registry, "read_kubeconfig", lambda _cid: cert_kc)
    res_cert = await detect_kubeconfig_subject(CLUSTER_ID, runner=_SubjectRunner())
    assert res_cert["kind"] == "User"
    assert res_cert["name"] == "cert-user@example.com"
    assert res_cert["source"] == "kubeconfig"

    # Shell-injection username rejected -> unknown
    bad_user_ssr = json.dumps({"status": {"userInfo": {"username": "bad; $(whoami)"}}})
    monkeypatch.setattr(registry, "read_kubeconfig", lambda _cid: "")
    res_bad = await detect_kubeconfig_subject(CLUSTER_ID, runner=_SubjectRunner(ssr_out=bad_user_ssr, ssr_rc=0))
    assert res_bad["kind"] == "unknown"
    assert res_bad["overrideRequired"] is True

    # Egress enforced before running kubectl
    monkeypatch.setattr(
        evaluator,
        "validate_stored_cluster_egress",
        lambda _cid: (_ for _ in ()).throw(EgressBlockedError("loopback", message="blocked")),
    )
    with pytest.raises(EgressBlockedError):
        await detect_kubeconfig_subject(CLUSTER_ID)


@pytest.mark.asyncio
async def test_evaluate_operations_defers_storage_rbac_pre_handoff_and_enforces_post_marker():
    from llm_d_lens.cluster.evaluator import evaluate_operations
    from llm_d_lens.cluster.operations import build_operations

    storage_ns = f"lens-storage-{CLUSTER_ID}"
    plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "team-inference", "namespaceMode": "existing", "name": "gw"},
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "team-inference",
            "secretNamespaceMode": "existing",
        },
        modelCache={"enabled": True, "repoId": "org/model", "revision": "main", "storageVolumeRef": "v"},
        handoff={"namespace": "team-inference"},
    )
    assert plan.storage.namespace == storage_ns
    operations = build_operations(plan, cluster_id=CLUSTER_ID)

    # 1. Pre-handoff (missing marker): storage_ns denials are deferred; status is admin_required (NOT blocked).
    pre_runner = FakeRunner(
        deny=lambda spec: _resource_attributes(spec).get("namespace", "") != "team-inference",
    )
    pre_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="hash-pre",
        runner=pre_runner,
    )
    assert pre_report.status == "admin_required"
    assert pre_report.blocked_operations == []
    assert {"storage.namespace", "storage.pv", "storage.pvc"} <= {e["id"] for e in pre_report.admin_operations}
    assert "model-cache.token-secret" in {e["id"] for e in pre_report.user_operations}
    assert "model-cache.job" in {e["id"] for e in pre_report.lens_operations}
    assert not any(
        rule["operationId"] in {"model-cache.token-secret", "model-cache.job"} for rule in pre_report.missing
    )

    # 2. External secret namespace denial stays blocked with only the external rule!
    ext_plan = _minimal_plan(
        gateway={"provider": "gke", "namespace": "team-inference", "namespaceMode": "existing", "name": "gw"},
        storage={
            "kind": "nfs",
            "nfsServer": "nfs.example.test",
            "nfsPath": "/exports/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        hfTokenSecret={
            "tokenSource": "existing-secret:locked-hf",
            "secretNamespace": "locked-secrets",
        },
        modelCache={"enabled": True, "repoId": "org/model", "revision": "main", "storageVolumeRef": "v"},
    )
    ext_ops = build_operations(ext_plan, cluster_id=CLUSTER_ID)
    ext_report = await evaluate_operations(
        CLUSTER_ID,
        ext_ops,
        plan_hash="hash-ext",
        runner=pre_runner,
    )
    assert ext_report.status == "blocked"
    blocked_by_id = {e["id"]: e for e in ext_report.blocked_operations}
    assert "model-cache.token-secret" in blocked_by_id
    assert "model-cache.job" not in blocked_by_id
    assert "model-cache.job" in {e["id"] for e in ext_report.user_operations}
    assert blocked_by_id["model-cache.token-secret"]["missing"] == [
        {
            "verb": "get",
            "apiGroup": "",
            "resource": "secrets",
            "scope": "namespace",
            "namespace": "locked-secrets",
            "name": "locked-hf",
        }
    ]

    # 3. Once marker["verified"] is True (e.g. unknown subject without RoleBinding), real SSAR blocks!
    admin_op_ids = ",".join(e["id"] for e in pre_report.admin_operations)
    verified_no_rbac_runner = MarkerRunner(
        {"planHash": "hash-post", "status": "complete", "operations": admin_op_ids},
        deny=lambda spec: _resource_attributes(spec).get("namespace", "") != "team-inference",
    )
    post_report = await evaluate_operations(
        CLUSTER_ID,
        operations,
        plan_hash="hash-post",
        handoff_namespace="team-inference",
        runner=verified_no_rbac_runner,
    )
    assert post_report.marker["verified"] is True
    assert post_report.status == "blocked"
    assert {e["id"] for e in post_report.blocked_operations} == {"model-cache.token-secret", "model-cache.job"}
