"""Batched ``SelfSubjectAccessReview`` evaluation for a provisioning plan.

The evaluator derives the concrete checks from the capability catalog, submits
them as one ``v1 List`` of ``authorization.k8s.io/v1 SelfSubjectAccessReview``
objects through the cluster's scoped runner, and maps answers by attributes
rather than by position. See
``docs/design/cluster-permission-preflight-design.md`` sections 4.3, 4.3.1,
4.3.2 and 12.

Only resolved answers produce a report. A missing or duplicate answer, a count
mismatch, a non-zero exit, unparseable output or a timeout raises
:class:`CapabilityCheckUnavailable`; the evaluator never guesses ``allowed``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from llm_d_lens.infrastructure.process import CommandResult

from .capabilities import (
    CAPABILITY_UNITS,
    CheckRule,
    NonResourceCheck,
    check_rules_for_plan,
    non_resource_checks_for_units,
    unit_namespaces,
    units_for_plan,
)
from .egress import EgressBlockedError, validate_kubeconfig_egress, validate_stored_cluster_egress
from .plan import ProvisioningPlan
from .plan import plan_hash as compute_plan_hash

if TYPE_CHECKING:
    from llm_d_lens.hardware.models import HardwareProfile

__all__ = [
    "STORAGE_OPERATOR_ROLE_NAME",
    "CapabilityCheckUnavailable",
    "CapabilityReport",
    "OperationReport",
    "apply_live_prometheus_satisfaction",
    "check_hf_secret_namespaces",
    "check_storage_operator_permissions",
    "check_storage_pvc_namespaces",
    "clear_cache",
    "detect_kubeconfig_subject",
    "evaluate_capabilities",
    "evaluate_operations",
    "probe_live_monitoring_satisfaction",
    "promote_admin_dependencies",
    "storage_operator_role_binding_manifest",
    "storage_operator_role_manifest",
    "validate_operation_split",
]

#: Maximum reviews per ``kubectl create -f -`` call.
CHUNK_SIZE = 50
#: Total preflight budget in seconds, including every chunk and retry.
TOTAL_BUDGET_SECONDS = 60.0
#: In-process report cache TTL.
CACHE_TTL_SECONDS = 60.0
#: A non-resource review key starts with this marker so it cannot collide with
#: a resource key.
_NON_RESOURCE = "nonresource"
_RESOURCE = "resource"
_CLUSTER_WILDCARD_KEY = (_RESOURCE, "*", "*", "*", "", "", "")

CacheKey = tuple[str, str, str, tuple[str, ...]]


class CapabilityCheckUnavailable(Exception):  # noqa: N818 - name fixed by the design contract
    """The cluster could not answer every review; nothing about denied rules is known."""


class CapabilityReport(BaseModel):
    """The preflight result for one plan and one unit subset."""

    model_config = ConfigDict(populate_by_name=True)

    plan_hash: str = Field(alias="planHash")
    status: Literal["sufficient", "denied", "unavailable"]
    sufficient: bool
    identity: dict[str, Any] | None = None
    checked_at: datetime = Field(alias="checkedAt")
    #: Denied checks grouped by capability unit:
    #: ``[{"unit": id, "title": title, "rules": [{...}]}]``.
    missing: list[dict[str, Any]] = Field(default_factory=list)
    #: Rules intentionally not probed, e.g. an unserved CRD if a supported
    #: version rejects it. No skip is added by default (section 4.3.2).
    skipped: list[dict[str, Any]] = Field(default_factory=list)


_CACHE: dict[CacheKey, tuple[float, CapabilityReport]] = {}


def clear_cache() -> None:
    """Drop every cached report (used by tests and by explicit invalidation)."""
    _CACHE.clear()
    _OPERATION_CACHE.clear()


def _cache_get(key: CacheKey) -> CapabilityReport | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, report = entry
    if expires_at <= time.monotonic():
        _CACHE.pop(key, None)
        return None
    return report


def _cache_put(key: CacheKey, report: CapabilityReport) -> None:
    _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, report)


def _kubeconfig_digest(cluster_id: str, kubeconfig_path: str | None) -> str:
    """Hash the kubeconfig bytes so replacing it invalidates the cache entry."""

    data = b""
    if kubeconfig_path:
        try:
            with open(kubeconfig_path, "rb") as handle:  # noqa: PTH123 - explicit path supplied by caller
                data = handle.read()
        except OSError:
            data = b""
    else:
        from llm_d_lens.cluster import registry

        text = registry.read_kubeconfig(cluster_id)
        data = (text or "").encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _resource_key(check: CheckRule, namespace: str | None) -> tuple[str, ...]:
    return (
        _RESOURCE,
        check.verb,
        check.api_group,
        check.resource,
        check.subresource or "",
        namespace or "",
        check.name or "",
    )


def _non_resource_key(check: NonResourceCheck) -> tuple[str, ...]:
    return (_NON_RESOURCE, check.verb, check.path)


def _review_item(key: tuple[str, ...]) -> dict[str, Any]:
    if key[0] == _RESOURCE:
        _, verb, api_group, resource, subresource, namespace, name = key
        attributes: dict[str, Any] = {"verb": verb, "group": api_group, "resource": resource}
        if subresource:
            attributes["subresource"] = subresource
        if namespace:
            attributes["namespace"] = namespace
        if name:
            attributes["name"] = name
        spec = {"resourceAttributes": attributes}
    else:
        _, verb, path = key
        spec = {"nonResourceAttributes": {"verb": verb, "path": path}}
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": spec,
    }


def _key_from_review(item: Any) -> tuple[str, ...]:
    if not isinstance(item, Mapping):
        raise CapabilityCheckUnavailable("SelfSubjectAccessReview response is not an object")
    spec = item.get("spec")
    if not isinstance(spec, Mapping):
        raise CapabilityCheckUnavailable("SelfSubjectAccessReview response has no spec")

    resource_attributes = spec.get("resourceAttributes")
    if isinstance(resource_attributes, Mapping):
        verb = resource_attributes.get("verb")
        resource = resource_attributes.get("resource")
        if not isinstance(verb, str) or not isinstance(resource, str):
            raise CapabilityCheckUnavailable("SelfSubjectAccessReview resource attributes are incomplete")
        return (
            _RESOURCE,
            verb,
            str(resource_attributes.get("group") or ""),
            resource,
            str(resource_attributes.get("subresource") or ""),
            str(resource_attributes.get("namespace") or ""),
            str(resource_attributes.get("name") or ""),
        )

    non_resource_attributes = spec.get("nonResourceAttributes")
    if isinstance(non_resource_attributes, Mapping):
        verb = non_resource_attributes.get("verb")
        path = non_resource_attributes.get("path")
        if not isinstance(verb, str) or not isinstance(path, str):
            raise CapabilityCheckUnavailable("SelfSubjectAccessReview non-resource attributes are incomplete")
        return (_NON_RESOURCE, verb, path)

    raise CapabilityCheckUnavailable("SelfSubjectAccessReview response has no review attributes")


def _allowed_from_review(item: Mapping[str, Any]) -> bool:
    status = item.get("status")
    if not isinstance(status, Mapping):
        raise CapabilityCheckUnavailable("SelfSubjectAccessReview response has no status")
    allowed = status.get("allowed")
    if not isinstance(allowed, bool):
        raise CapabilityCheckUnavailable("SelfSubjectAccessReview response has no allowed answer")
    return allowed


def _missing_rule(key: tuple[str, ...]) -> dict[str, Any]:
    if key[0] == _RESOURCE:
        _, verb, api_group, resource, subresource, namespace, name = key
        rule: dict[str, Any] = {
            "verb": verb,
            "apiGroup": api_group,
            "resource": resource,
            "scope": "namespace" if namespace else "cluster",
        }
        if subresource:
            rule["subresource"] = subresource
        if namespace:
            rule["namespace"] = namespace
        if name:
            rule["name"] = name
        return rule
    _, verb, path = key
    return {"verb": verb, "path": path}


def _parse_review_documents(stdout: str) -> list[dict[str, Any]]:
    """Parse ``kubectl create -o json -f -`` output for one or more reviews.

    ``kubectl`` prints one JSON document per created object (a stream of
    consecutive documents) depending on its version, while other versions wrap
    the answers in a single ``kind: List``. Both shapes, and a single object,
    are accepted. Any other shape is unparseable.
    """

    text = (stdout or "").strip()
    if not text:
        raise CapabilityCheckUnavailable("capability check returned empty output")

    decoder = json.JSONDecoder()
    documents: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        try:
            value, position = decoder.raw_decode(text, position)
        except ValueError as exc:
            raise CapabilityCheckUnavailable("capability check returned unparseable JSON") from exc

        if isinstance(value, Mapping) and str(value.get("kind", "")).endswith("List"):
            items = value.get("items")
            if not isinstance(items, list):
                raise CapabilityCheckUnavailable("capability check List response has no items list")
            for item in items:
                if not isinstance(item, Mapping):
                    raise CapabilityCheckUnavailable("capability check List item is not an object")
                documents.append(dict(item))
        elif isinstance(value, Mapping):
            documents.append(dict(value))
        else:
            raise CapabilityCheckUnavailable("capability check returned unparseable JSON")

        while position < len(text) and text[position].isspace():
            position += 1

    if not documents:
        raise CapabilityCheckUnavailable("capability check returned no review objects")
    return documents


def _chunks(keys: Sequence[tuple[str, ...]], size: int) -> Iterable[Sequence[tuple[str, ...]]]:
    for start in range(0, len(keys), size):
        yield keys[start : start + size]


async def _run_checks(
    keys: Sequence[tuple[str, ...]],
    *,
    runner: Any,
    kubeconfig_path: str | None,
    deadline: float,
) -> dict[tuple[str, ...], bool]:
    answers: dict[tuple[str, ...], bool] = {}
    for chunk in _chunks(keys, CHUNK_SIZE):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CapabilityCheckUnavailable("capability check exceeded its 60-second budget")
        body = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [_review_item(key) for key in chunk],
            }
        )
        run_kwargs: dict[str, Any] = {"input": body, "timeout": remaining}
        if kubeconfig_path:
            run_kwargs["env"] = {"KUBECONFIG": str(kubeconfig_path)}
        try:
            result = await runner.run(
                ["kubectl", "create", "-o", "json", "-f", "-"],
                **run_kwargs,
            )
        except (FileNotFoundError, TimeoutError, OSError) as exc:
            raise CapabilityCheckUnavailable(f"capability check could not run kubectl: {exc}") from exc

        if not isinstance(result, CommandResult):
            raise CapabilityCheckUnavailable("capability check runner returned an invalid result")
        if result.returncode != 0:
            raise CapabilityCheckUnavailable(
                f"capability check failed: {(result.stderr or result.stdout or '').strip()}"
            )
        items = _parse_review_documents(result.stdout or "")
        expected = set(chunk)
        if len(items) != len(chunk):
            raise CapabilityCheckUnavailable(f"capability check returned {len(items)} answers for {len(chunk)} reviews")
        seen: set[tuple[str, ...]] = set()
        for item in items:
            key = _key_from_review(item)
            if key in seen or key not in expected:
                raise CapabilityCheckUnavailable("capability check returned a duplicate or unexpected review")
            seen.add(key)
            answers[key] = _allowed_from_review(item)
        if seen != expected:
            raise CapabilityCheckUnavailable("capability check response is missing a review answer")
    return answers


def _validate_egress(cluster_id: str, *, kubeconfig_path: str | None, runner: Any | None) -> None:
    """Enforce the outbound policy before any review is submitted.

    An explicit kubeconfig path is validated directly; otherwise the stored
    kubeconfig is validated. A caller-supplied runner without a kubeconfig path
    is the test seam used to exercise SSAR mapping without a cluster.
    """
    if kubeconfig_path is not None:
        try:
            kubeconfig_bytes = Path(kubeconfig_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise CapabilityCheckUnavailable(f"capability check could not read its kubeconfig: {exc}") from exc
        validate_kubeconfig_egress(kubeconfig_bytes)
    elif runner is None:
        validate_stored_cluster_egress(cluster_id)


async def evaluate_capabilities(
    cluster_id: str,
    plan: ProvisioningPlan,
    *,
    unit_ids: Sequence[str] | None = None,
    kubeconfig_path: str | None = None,
    plan_hash: str | None = None,
    runner: Any | None = None,
    use_cache: bool = True,
) -> CapabilityReport:
    """Evaluate the plan's capability units against the cluster's kubeconfig.

    ``unit_ids`` defaults to the plan's full unit set; the execution guard passes
    the endpoint's subset. A report is cached for 60 seconds keyed by
    ``(cluster_id, sha256(kubeconfig bytes), plan_hash, sorted(unit_ids))``.
    Unavailable checks are never cached.
    """

    # EgressBlockedError propagates; the router maps it to 400
    # ``kubeconfig_egress_blocked`` rather than a permission denial.
    _validate_egress(cluster_id, kubeconfig_path=kubeconfig_path, runner=runner)

    selected = tuple(sorted(set(unit_ids))) if unit_ids is not None else units_for_plan(plan)
    resolved_plan_hash = plan_hash or compute_plan_hash(plan)

    cache_key = (
        cluster_id,
        _kubeconfig_digest(cluster_id, kubeconfig_path),
        resolved_plan_hash,
        tuple(sorted(selected)),
    )
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    # Plan-scoped checks pair each rule with the unit that requested it, so a
    # named Secret review is attributed to the right unit without guessing from
    # the static catalog. The helper also validates unknown unit ids.
    checks = check_rules_for_plan(plan, selected)
    non_resource_checks = non_resource_checks_for_units(selected)

    namespaces_by_unit = unit_namespaces(plan)
    keys: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()

    def register(key: tuple[str, ...]) -> None:
        if key not in seen:
            seen.add(key)
            keys.append(key)

    # Attribute every unique review to each unit that requested it. Overlapping
    # units share one request; a denial is reported under all of them.
    unit_checks: dict[tuple[str, ...], set[str]] = {}
    for unit_id, check in checks:
        if check.scope == "namespace":
            if check.namespace:
                # A plan-specific check names its exact namespace (for example
                # the HF source Secret versus the model cache token Secret).
                targets: tuple[str, ...] = (check.namespace,)
            else:
                targets = namespaces_by_unit.get(unit_id, ())
            unit_keys = [_resource_key(check, namespace) for namespace in targets]
        else:
            unit_keys = [_resource_key(check, None)]
        for key in unit_keys:
            register(key)
            unit_checks.setdefault(key, set()).add(unit_id)
    for check in non_resource_checks:
        register(_non_resource_key(check))
    # Non-resource checks belong to the static unit that declares them.
    for unit_id in selected:
        unit = CAPABILITY_UNITS[unit_id]
        for check in unit.non_resource_checks:
            unit_checks.setdefault(_non_resource_key(check), set()).add(unit_id)

    answers: dict[tuple[str, ...], bool] = {}
    if keys:
        runner = runner if runner is not None else _default_runner(cluster_id)
        answers = await _run_checks(
            keys,
            runner=runner,
            kubeconfig_path=kubeconfig_path,
            deadline=time.monotonic() + TOTAL_BUDGET_SECONDS,
        )

    missing_by_unit: dict[str, list[dict[str, Any]]] = {}
    for key, allowed in answers.items():
        if allowed:
            continue
        rule = _missing_rule(key)
        for unit_id in sorted(unit_checks.get(key, ())):
            unit_rules = missing_by_unit.setdefault(unit_id, [])
            if rule not in unit_rules:
                unit_rules.append(rule)

    missing = [
        {
            "unit": unit_id,
            "title": CAPABILITY_UNITS[unit_id].title,
            "rules": missing_by_unit[unit_id],
        }
        for unit_id in sorted(missing_by_unit)
    ]
    sufficient = not missing
    report = CapabilityReport(
        plan_hash=resolved_plan_hash,
        status="sufficient" if sufficient else "denied",
        sufficient=sufficient,
        identity=None,
        checked_at=datetime.now(UTC),
        missing=missing,
        skipped=[],
    )
    if use_cache:
        _cache_put(cache_key, report)
    return report


def _default_runner(cluster_id: str) -> Any:
    from llm_d_lens.infrastructure.kubernetes import scoped_runner

    return scoped_runner(cluster_id)


_HF_SECRET_WRITE_VERBS: tuple[str, ...] = ("get", "create", "patch")
_STORAGE_PVC_WRITE_VERBS: tuple[str, ...] = ("get", "create", "patch")


async def _check_namespaced_resource_access(
    cluster_id: str,
    namespaces: Sequence[str],
    *,
    resource: str,
    verbs: Sequence[str],
    error_label: str,
    kubeconfig_path: str | None = None,
    runner: Any | None = None,
    timeout_seconds: float = TOTAL_BUDGET_SECONDS,
) -> dict[str, bool]:
    _validate_egress(cluster_id, kubeconfig_path=kubeconfig_path, runner=runner)
    unique_namespaces = list(dict.fromkeys(ns for ns in namespaces if ns))
    if not unique_namespaces:
        return {}

    keys: list[tuple[str, ...]] = []
    seen_keys: set[tuple[str, ...]] = set()
    keys_by_namespace: dict[str, list[tuple[str, ...]]] = {}
    for namespace in unique_namespaces:
        ns_keys = [
            _resource_key(
                CheckRule(verb=verb, api_group="", resource=resource, scope="namespace"),
                namespace,
            )
            for verb in verbs
        ]
        keys_by_namespace[namespace] = ns_keys
        for key in ns_keys:
            if key not in seen_keys:
                seen_keys.add(key)
                keys.append(key)

    try:
        active_runner = runner if runner is not None else _default_runner(cluster_id)
        answers = await _run_checks(
            keys,
            runner=active_runner,
            kubeconfig_path=kubeconfig_path,
            deadline=time.monotonic() + timeout_seconds,
        )
    except (CapabilityCheckUnavailable, EgressBlockedError):
        raise
    except Exception as exc:
        raise CapabilityCheckUnavailable(f"{error_label} failed: {exc}") from exc

    return {
        namespace: all(answers.get(key) is True for key in ns_keys) for namespace, ns_keys in keys_by_namespace.items()
    }


async def check_hf_secret_namespaces(
    cluster_id: str,
    namespaces: Sequence[str],
    *,
    kubeconfig_path: str | None = None,
    runner: Any | None = None,
    timeout_seconds: float = TOTAL_BUDGET_SECONDS,
) -> dict[str, bool]:
    """Batch-check core ``secrets`` ``get``/``create``/``patch`` SSAR per namespace."""
    return await _check_namespaced_resource_access(
        cluster_id,
        namespaces,
        resource="secrets",
        verbs=_HF_SECRET_WRITE_VERBS,
        error_label="HF secret namespace check",
        kubeconfig_path=kubeconfig_path,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )


async def check_storage_pvc_namespaces(
    cluster_id: str,
    namespaces: Sequence[str],
    *,
    kubeconfig_path: str | None = None,
    runner: Any | None = None,
    timeout_seconds: float = TOTAL_BUDGET_SECONDS,
) -> dict[str, bool]:
    """Batch-check core ``persistentvolumeclaims`` ``get``/``create``/``patch`` SSAR per namespace."""
    return await _check_namespaced_resource_access(
        cluster_id,
        namespaces,
        resource="persistentvolumeclaims",
        verbs=_STORAGE_PVC_WRITE_VERBS,
        error_label="Storage PVC namespace check",
        kubeconfig_path=kubeconfig_path,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )


STORAGE_OPERATOR_ROLE_NAME = "lens-storage-operator"
_SA_PREFIX = "system:serviceaccount:"
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_SUBJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,252}\Z")

_STORAGE_OPERATOR_VERBS: tuple[str, ...] = (
    "get",
    "list",
    "watch",
    "create",
    "update",
    "patch",
    "delete",
)

_STORAGE_OPERATOR_ROLE_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("",),
        (
            "persistentvolumeclaims",
            "pods",
            "pods/log",
            "secrets",
            "configmaps",
            "events",
            "serviceaccounts",
        ),
        _STORAGE_OPERATOR_VERBS,
    ),
    (
        ("batch",),
        ("jobs",),
        _STORAGE_OPERATOR_VERBS,
    ),
)

_STORAGE_OPERATOR_CHECK_RULES: tuple[CheckRule, ...] = (
    *(
        CheckRule(verb=verb, api_group="", resource="persistentvolumeclaims", scope="namespace")
        for verb in ("get", "list", "create", "patch", "delete")
    ),
    *(CheckRule(verb=verb, api_group="batch", resource="jobs", scope="namespace") for verb in _STORAGE_OPERATOR_VERBS),
    *(CheckRule(verb=verb, api_group="", resource="pods", scope="namespace") for verb in ("get", "list", "watch")),
    CheckRule(verb="get", api_group="", resource="pods", scope="namespace", subresource="log"),
    *(
        CheckRule(verb=verb, api_group="", resource="secrets", scope="namespace")
        for verb in ("get", "create", "update", "patch", "delete")
    ),
)


def _is_valid_dns1123_label(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 63 and bool(_DNS1123_LABEL_RE.fullmatch(value))


def _require_dns1123_label(value: Any, field: str) -> str:
    if not _is_valid_dns1123_label(value):
        raise ValueError(f"{field} must be a valid DNS-1123 label")
    return str(value)


def _storage_operator_role_grants(
    api_group: str,
    resource: str,
    subresource: str | None,
    verb: str,
) -> bool:
    target_resource = f"{resource}/{subresource}" if subresource else resource
    for api_groups, resources, verbs in _STORAGE_OPERATOR_ROLE_RULES:
        if api_group in api_groups and target_resource in resources and verb in verbs:
            return True
    return False


async def check_storage_operator_permissions(
    cluster_id: str,
    namespace: str,
    *,
    kubeconfig_path: str | None = None,
    runner: Any | None = None,
    timeout_seconds: float = 10.0,
) -> bool:
    """Return ``True`` iff all storage-operator SSAR checks pass in ``namespace``."""
    _validate_egress(cluster_id, kubeconfig_path=kubeconfig_path, runner=runner)
    if not _is_valid_dns1123_label(namespace):
        return False
    keys = [_resource_key(rule, namespace) for rule in _STORAGE_OPERATOR_CHECK_RULES]
    try:
        active_runner = runner if runner is not None else _default_runner(cluster_id)
        answers = await _run_checks(
            keys,
            runner=active_runner,
            kubeconfig_path=kubeconfig_path,
            deadline=time.monotonic() + timeout_seconds,
        )
    except (CapabilityCheckUnavailable, EgressBlockedError):
        raise
    except Exception as exc:
        raise CapabilityCheckUnavailable(f"Storage operator permission check failed: {exc}") from exc
    return all(answers.get(key) is True for key in keys)


def _unknown_kubeconfig_subject() -> dict[str, Any]:
    return {
        "kind": "unknown",
        "name": None,
        "namespace": None,
        "subject": None,
        "source": "none",
        "overrideRequired": True,
    }


def _subject_from_username(username: str, *, source: str) -> dict[str, Any]:
    if not isinstance(username, str) or "\n" in username or "\r" in username or "\x00" in username:
        return _unknown_kubeconfig_subject()
    cleaned = username.strip()
    if not cleaned or cleaned == "system:anonymous":
        return _unknown_kubeconfig_subject()
    if cleaned.startswith(_SA_PREFIX):
        rest = cleaned[len(_SA_PREFIX) :]
        if ":" not in rest:
            return _unknown_kubeconfig_subject()
        sa_ns, sa_name = rest.split(":", 1)
        if _is_valid_dns1123_label(sa_ns) and _is_valid_dns1123_label(sa_name):
            return {
                "kind": "ServiceAccount",
                "name": sa_name,
                "namespace": sa_ns,
                "subject": cleaned,
                "source": source,
                "overrideRequired": False,
            }
        return _unknown_kubeconfig_subject()
    if _SUBJECT_NAME_RE.fullmatch(cleaned):
        return {
            "kind": "User",
            "name": cleaned,
            "namespace": None,
            "subject": cleaned,
            "source": source,
            "overrideRequired": False,
        }
    return _unknown_kubeconfig_subject()


def _subject_from_jwt_token(token: str) -> dict[str, Any] | None:
    if "\n" in token or "\r" in token or "\x00" in token:
        return None
    parts = token.strip().split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    sub = payload.get("sub")
    if isinstance(sub, str) and sub.strip():
        parsed = _subject_from_username(sub, source="kubeconfig")
        if not parsed["overrideRequired"]:
            return parsed
    k8s_claims = payload.get("kubernetes.io/serviceaccount")
    if isinstance(k8s_claims, Mapping):
        sa_ns = k8s_claims.get("namespace")
        sa_name = k8s_claims.get("name")
        if _is_valid_dns1123_label(sa_ns) and _is_valid_dns1123_label(sa_name):
            return {
                "kind": "ServiceAccount",
                "name": str(sa_name),
                "namespace": str(sa_ns),
                "subject": f"{_SA_PREFIX}{sa_ns}:{sa_name}",
                "source": "kubeconfig",
                "overrideRequired": False,
            }
    ns_claim = payload.get("kubernetes.io/serviceaccount/namespace")
    name_claim = payload.get("kubernetes.io/serviceaccount/service-account.name")
    if _is_valid_dns1123_label(ns_claim) and _is_valid_dns1123_label(name_claim):
        return {
            "kind": "ServiceAccount",
            "name": str(name_claim),
            "namespace": str(ns_claim),
            "subject": f"{_SA_PREFIX}{ns_claim}:{name_claim}",
            "source": "kubeconfig",
            "overrideRequired": False,
        }
    return None


def _subject_from_inline_client_cert(cert_data: str) -> dict[str, Any] | None:
    try:
        from cryptography import x509  # noqa: PLC0415
        from cryptography.x509.oid import NameOID  # noqa: PLC0415

        decoded = base64.b64decode(cert_data.strip(), validate=False)
        if b"-----BEGIN CERTIFICATE-----" in decoded:
            cert = x509.load_pem_x509_certificate(decoded)
        else:
            cert = x509.load_der_x509_certificate(decoded)
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not attrs:
            return None
        cn = attrs[0].value
        if not isinstance(cn, str) or not cn.strip():
            return None
        parsed = _subject_from_username(cn, source="kubeconfig")
        return None if parsed["overrideRequired"] else parsed
    except Exception:  # noqa: BLE001 - invalid/unsupported inline certificate falls through cleanly
        return None


def _identity_from_kubeconfig(
    cluster_id: str,
    *,
    kubeconfig_path: str | None = None,
) -> dict[str, Any] | None:
    if kubeconfig_path is not None:
        try:
            raw: str | None = Path(kubeconfig_path).read_text(encoding="utf-8")
        except OSError:
            raw = None
    else:
        from llm_d_lens.cluster import registry as cluster_registry  # noqa: PLC0415

        raw = cluster_registry.read_kubeconfig(cluster_id)
    if not raw:
        return None
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    current_context = parsed.get("current-context")
    user_ref: str | None = None
    for entry in parsed.get("contexts") or ():
        if isinstance(entry, Mapping) and (not current_context or entry.get("name") == current_context):
            ctx = entry.get("context")
            if isinstance(ctx, Mapping) and isinstance(ctx.get("user"), str):
                user_ref = ctx["user"]
                break
    for entry in parsed.get("users") or ():
        if not isinstance(entry, Mapping):
            continue
        if user_ref and entry.get("name") != user_ref:
            continue
        user_obj = entry.get("user")
        if isinstance(user_obj, Mapping):
            # Never read arbitrary disk paths (tokenFile / client-certificate).
            token = user_obj.get("token")
            if isinstance(token, str) and token.strip():
                jwt_identity = _subject_from_jwt_token(token)
                if jwt_identity is not None:
                    return jwt_identity
            username = user_obj.get("username")
            if isinstance(username, str) and username.strip():
                ident = _subject_from_username(username, source="kubeconfig")
                if not ident["overrideRequired"]:
                    return ident
            cert_data = user_obj.get("client-certificate-data")
            if isinstance(cert_data, str) and cert_data.strip():
                cert_identity = _subject_from_inline_client_cert(cert_data)
                if cert_identity is not None:
                    return cert_identity
        name = entry.get("name")
        if isinstance(name, str) and name.startswith(_SA_PREFIX):
            ident = _subject_from_username(name, source="kubeconfig")
            if not ident["overrideRequired"]:
                return ident
    return None


async def detect_kubeconfig_subject(
    cluster_id: str,
    *,
    kubeconfig_path: str | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    """Auto-detect the Kubernetes subject via SelfSubjectReview -> whoami -> inline kubeconfig."""
    _validate_egress(cluster_id, kubeconfig_path=kubeconfig_path, runner=runner)
    active_runner = runner if runner is not None else _default_runner(cluster_id)
    run_kwargs: dict[str, Any] = {"timeout": 15}
    if kubeconfig_path:
        run_kwargs["env"] = {"KUBECONFIG": str(kubeconfig_path)}

    ssr_body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"})
    try:
        ssr = await active_runner.run(["kubectl", "create", "-o", "json", "-f", "-"], input=ssr_body, **run_kwargs)
    except (FileNotFoundError, TimeoutError, OSError):
        ssr = None
    if isinstance(ssr, CommandResult) and ssr.returncode == 0 and ssr.stdout:
        try:
            payload = json.loads(ssr.stdout)
            username = ((payload.get("status") or {}).get("userInfo") or {}).get("username")
            if isinstance(username, str) and username.strip() and username.strip() != "system:anonymous":
                ident = _subject_from_username(username, source="self_subject_review")
                if not ident["overrideRequired"]:
                    return ident
        except (ValueError, AttributeError):
            pass

    try:
        whoami = await active_runner.run(["kubectl", "auth", "whoami", "-o", "json"], **run_kwargs)
    except (FileNotFoundError, TimeoutError, OSError):
        whoami = None
    if isinstance(whoami, CommandResult) and whoami.returncode == 0 and whoami.stdout:
        try:
            payload = json.loads(whoami.stdout)
            username = ((payload.get("status") or {}).get("userInfo") or {}).get("username")
            if isinstance(username, str) and username.strip() and username.strip() != "system:anonymous":
                ident = _subject_from_username(username, source="whoami")
                if not ident["overrideRequired"]:
                    return ident
        except (ValueError, AttributeError):
            pass

    from_kubeconfig = _identity_from_kubeconfig(cluster_id, kubeconfig_path=kubeconfig_path)
    if from_kubeconfig is not None:
        return from_kubeconfig

    return _unknown_kubeconfig_subject()


def storage_operator_role_manifest(namespace: str) -> dict[str, Any]:
    """Build the per-namespace ``Role/lens-storage-operator`` manifest."""
    validated_ns = _require_dns1123_label(namespace, "namespace")
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": STORAGE_OPERATOR_ROLE_NAME,
            "namespace": validated_ns,
            "labels": {"app.kubernetes.io/managed-by": "lens"},
        },
        "rules": [
            {
                "apiGroups": list(api_groups),
                "resources": list(resources),
                "verbs": list(verbs),
            }
            for api_groups, resources, verbs in _STORAGE_OPERATOR_ROLE_RULES
        ],
    }


def storage_operator_role_binding_manifest(
    namespace: str,
    identity: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the per-namespace ``RoleBinding/lens-storage-operator`` manifest, or ``None`` if unknown."""
    validated_ns = _require_dns1123_label(namespace, "namespace")
    if identity is None or not isinstance(identity, Mapping):
        return None
    kind = identity.get("kind")
    if kind not in {"ServiceAccount", "User", "Group"}:
        return None
    raw_name = identity.get("name")
    if kind == "ServiceAccount":
        sa_name = _require_dns1123_label(raw_name, "ServiceAccount subject name")
        sa_ns = _require_dns1123_label(identity.get("namespace"), "ServiceAccount subject namespace")
        subject_entry: dict[str, Any] = {
            "kind": "ServiceAccount",
            "name": sa_name,
            "namespace": sa_ns,
        }
    else:
        if (
            not isinstance(raw_name, str)
            or "\n" in raw_name
            or "\r" in raw_name
            or "\x00" in raw_name
            or raw_name != raw_name.strip()
            or raw_name == "system:anonymous"
            or not _SUBJECT_NAME_RE.fullmatch(raw_name)
        ):
            raise ValueError(f"{kind} subject name is invalid")
        subject_entry = {
            "kind": str(kind),
            "name": raw_name,
            "apiGroup": "rbac.authorization.k8s.io",
        }
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": STORAGE_OPERATOR_ROLE_NAME,
            "namespace": validated_ns,
            "labels": {"app.kubernetes.io/managed-by": "lens"},
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": STORAGE_OPERATOR_ROLE_NAME,
        },
        "subjects": [subject_entry],
    }


class OperationReport(BaseModel):
    """The operation-level preflight/split report (design sections 4.3 and 5)."""

    model_config = ConfigDict(populate_by_name=True)

    plan_hash: str = Field(alias="planHash")
    plan_version: int = Field(alias="planVersion")
    status: Literal["ready", "admin_required", "blocked"]
    sufficient: bool
    identity: dict[str, Any] | None = None
    checked_at: datetime = Field(alias="checkedAt")
    user_operations: list[dict[str, Any]] = Field(default_factory=list, alias="userOperations")
    admin_operations: list[dict[str, Any]] = Field(default_factory=list, alias="adminOperations")
    lens_operations: list[dict[str, Any]] = Field(default_factory=list, alias="lensOperations")
    #: User-owned operations whose checks are denied: they cannot be handed to
    #: an Orchestration administrator, so the user must fix access before execution.
    blocked_operations: list[dict[str, Any]] = Field(default_factory=list, alias="blockedOperations")
    missing: list[dict[str, Any]] = Field(default_factory=list)
    marker: dict[str, Any] = Field(default_factory=dict)
    remediation: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None


OperationCacheKey = tuple[str, str, str, tuple[str, ...], str]
_OPERATION_CACHE: dict[OperationCacheKey, tuple[float, OperationReport]] = {}


def _operation_cache_get(key: OperationCacheKey) -> OperationReport | None:
    entry = _OPERATION_CACHE.get(key)
    if entry is None:
        return None
    expires_at, report = entry
    if expires_at <= time.monotonic():
        _OPERATION_CACHE.pop(key, None)
        return None
    return report


def _operation_cache_put(key: OperationCacheKey, report: OperationReport) -> None:
    _OPERATION_CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, report)


def _operation_entry(operation: Any, executor: str, missing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": operation.id,
        "step": operation.step,
        "title": operation.title,
        "executor": executor,
        "dependsOn": list(operation.depends_on),
        "userOwnedSecrets": list(operation.user_owned_secrets),
        "userSecretFields": list(operation.user_secret_fields),
        "secretCopy": operation.apply.secret_copy is not None,
    }
    if missing:
        entry["missing"] = missing
    return entry


def _operation_check_keys(operation: Any) -> tuple[list[tuple[str, ...]], list[Any]]:
    keys: list[tuple[str, ...]] = []
    unbound: list[Any] = []
    for check in operation.requirement.checks:
        if check.scope == "namespace":
            namespace = check.namespace or operation.namespace
            if namespace:
                keys.append(_resource_key(check, namespace))
            else:
                unbound.append(check)
        else:
            keys.append(_resource_key(check, None))
    return keys, unbound


_MARKER_NODE_PORT_MIN = 30000
_MARKER_NODE_PORT_MAX = 32767
_MARKER_DIGITS_RE = re.compile(r"^\d+\Z")


def _parse_marker_node_port(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if _MARKER_NODE_PORT_MIN <= raw <= _MARKER_NODE_PORT_MAX else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not _MARKER_DIGITS_RE.fullmatch(text):
        return None
    port = int(text)
    return port if _MARKER_NODE_PORT_MIN <= port <= _MARKER_NODE_PORT_MAX else None


def _parse_marker_host(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in raw):
        return None
    if any(token in raw for token in ("://", "/", "@", "?", "#", "%")):
        return None
    from .registry import extract_url_host  # noqa: PLC0415

    return extract_url_host(raw)


async def _read_marker(
    *,
    runner: Any,
    cluster_id: str,
    handoff_namespace: str | None,
    plan_hash: str,
    kubeconfig_path: str | None,
    required_operation_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    name = f"lens-admin-handoff-{plan_hash[:16]}"
    if not handoff_namespace:
        return {"namespace": None, "name": name, "status": "unknown", "verified": False}
    run_kwargs: dict[str, Any] = {"timeout": 15}
    if kubeconfig_path:
        run_kwargs["env"] = {"KUBECONFIG": str(kubeconfig_path)}
    try:
        result = await runner.run(
            ["kubectl", "get", "configmap", name, "-n", handoff_namespace, "-o", "json"],
            **run_kwargs,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        return {"namespace": handoff_namespace, "name": name, "status": "missing", "verified": False}
    if not isinstance(result, CommandResult):
        return {"namespace": handoff_namespace, "name": name, "status": "unknown", "verified": False}
    if result.returncode != 0:
        message = f"{result.stderr or ''} {result.stdout or ''}".lower()
        status = "unconfirmed" if ("forbidden" in message or "unauthorized" in message) else "missing"
        return {"namespace": handoff_namespace, "name": name, "status": status, "verified": False}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {"namespace": handoff_namespace, "name": name, "status": "unknown", "verified": False}
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return {"namespace": handoff_namespace, "name": name, "status": "unknown", "verified": False}
    marker_plan_hash = str(data.get("planHash") or "")
    marker_status = str(data.get("status") or "")
    operations = [item for item in str(data.get("operations") or "").split(",") if item]
    complete_operations = all(operation_id in operations for operation_id in required_operation_ids)
    plan_hash_matches = bool(plan_hash) and marker_plan_hash == plan_hash
    verified = plan_hash_matches and marker_status == "complete" and complete_operations
    if not plan_hash_matches:
        status = "stale" if marker_plan_hash else "missing"
    elif marker_status == "complete" and not complete_operations:
        # A marker that omits a current admin operation is not complete for
        # this plan, even though the ConfigMap says so.
        status = "partial"
    elif marker_status in {"complete", "partial"}:
        status = marker_status
    else:
        status = "missing"
    prom_port = _parse_marker_node_port(data.get("prometheusNodePort"))
    graf_port = _parse_marker_node_port(data.get("grafanaNodePort"))
    if prom_port is not None and graf_port is not None and prom_port == graf_port:
        prom_port = None
        graf_port = None
    mon_host = _parse_marker_host(data.get("monitoringHost"))
    monitoring_marker: dict[str, Any] = {}
    if prom_port is not None:
        monitoring_marker["prometheusNodePort"] = prom_port
    if graf_port is not None:
        monitoring_marker["grafanaNodePort"] = graf_port
    if mon_host is not None:
        monitoring_marker["monitoringHost"] = mon_host
    marker: dict[str, Any] = {
        "namespace": handoff_namespace,
        "name": name,
        "status": status,
        "verified": verified,
        "operations": operations,
    }
    if monitoring_marker:
        marker["monitoring"] = monitoring_marker
    return marker


_ACCELERATOR_OP_PREFIX = "monitoring.accelerator."


def _is_live_probeable_monitoring_op(operation_id: str) -> bool:
    if operation_id == "monitoring.stack":
        return True
    if operation_id.startswith(_ACCELERATOR_OP_PREFIX):
        return bool(operation_id[len(_ACCELERATOR_OP_PREFIX) :].strip())
    return False


def _profile_for_accelerator_provider(provider_id: str) -> HardwareProfile | None:
    from llm_d_lens.hardware.registry import all_profiles  # noqa: PLC0415
    from llm_d_lens.hardware.resolver import resolve_by_accelerator_key  # noqa: PLC0415
    from llm_d_lens.monitoring.accelerator.registry import provider_id_for_profile  # noqa: PLC0415

    clean = provider_id.strip()
    if not clean:
        return None
    for profile in all_profiles():
        if provider_id_for_profile(profile) == clean:
            return profile
    return resolve_by_accelerator_key(clean)


async def probe_live_monitoring_satisfaction(
    prometheus_url: str,
    operation_ids: Sequence[str],
    *,
    timeout: float = 3.0,
) -> set[str]:
    """Probe a validated Prometheus endpoint for live monitoring operation satisfaction.

    Only ``monitoring.stack`` and ``monitoring.accelerator.<provider_id>`` are
    eligible; RBAC, verification, and non-monitoring operations are ignored.
    Fails closed (returns an empty set or omits failing operations) on SSRF
    rejection, timeouts, HTTP errors, or missing/zero series.
    """
    probeable = list(dict.fromkeys(op_id for op_id in operation_ids if _is_live_probeable_monitoring_op(op_id)))
    clean_url = str(prometheus_url or "").strip()
    if not probeable or not clean_url:
        return set()

    from llm_d_lens.monitoring.prometheus import (  # noqa: PLC0415
        _ensure_safe_prometheus_url,
        create_prometheus_client,
        query_vector,
    )
    from llm_d_lens.monitoring.service_links import (  # noqa: PLC0415
        _parse_scalar_count,
        probe_accelerator_metrics_via_prometheus,
    )

    try:
        await _ensure_safe_prometheus_url(clean_url, allow_query=False)
        async with create_prometheus_client(clean_url, timeout=timeout) as client:

            async def _probe_one(op_id: str) -> bool:
                if op_id == "monitoring.stack":
                    kube_rows, node_cpu_rows, node_uname_rows = await asyncio.gather(
                        query_vector(client, "count(kube_node_info)"),
                        query_vector(client, "count(node_cpu_seconds_total)"),
                        query_vector(client, "count(node_uname_info)"),
                    )
                    return _parse_scalar_count(kube_rows) > 0 and (
                        _parse_scalar_count(node_cpu_rows) > 0 or _parse_scalar_count(node_uname_rows) > 0
                    )
                provider_id = op_id[len(_ACCELERATOR_OP_PREFIX) :]
                profile = _profile_for_accelerator_provider(provider_id)
                if profile is None:
                    return False
                result = await probe_accelerator_metrics_via_prometheus(client, profile)
                if not isinstance(result, Mapping) or not bool(result.get("detected")):
                    return False
                try:
                    return int(result.get("series_count") or 0) > 0
                except (TypeError, ValueError):
                    return False

            outcomes = await asyncio.gather(*(_probe_one(op_id) for op_id in probeable), return_exceptions=True)
    except Exception:  # noqa: BLE001 - fail closed on SSRF rejection, transport, or parse errors
        return set()

    return {op_id for op_id, outcome in zip(probeable, outcomes, strict=True) if outcome is True}


async def apply_live_prometheus_satisfaction(
    marker: Mapping[str, Any],
    *,
    admin_operation_ids: Sequence[str],
    prometheus_url: str | None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Reconcile remaining monitoring operations in a valid marker against live Prometheus.

    Only ``partial`` or ``complete`` markers (whose ``planHash`` already matched)
    are eligible. Only ``monitoring.stack`` and ``monitoring.accelerator.*``
    operations are probed; non-monitoring operations and ``monitoring.rbac``
    still require explicit marker membership.
    """
    updated = dict(marker)
    status = str(marker.get("status") or "")
    if not admin_operation_ids or status not in {"partial", "complete"}:
        return updated

    raw_ops = marker.get("operations")
    if isinstance(raw_ops, Sequence) and not isinstance(raw_ops, str | bytes):
        existing_ops = [str(item) for item in raw_ops if item]
    else:
        existing_ops = []
    existing_set = set(existing_ops)
    remaining = [op_id for op_id in admin_operation_ids if op_id not in existing_set]
    if not remaining:
        return updated

    probeable = [op_id for op_id in remaining if _is_live_probeable_monitoring_op(op_id)]
    clean_url = str(prometheus_url or "").strip()
    if not probeable or not clean_url:
        return updated

    satisfied = await probe_live_monitoring_satisfaction(clean_url, probeable, timeout=timeout)
    if not satisfied:
        return updated

    merged_ops = list(existing_ops)
    for op_id in admin_operation_ids:
        if op_id in satisfied and op_id not in existing_set:
            merged_ops.append(op_id)
            existing_set.add(op_id)

    new_remaining = [op_id for op_id in admin_operation_ids if op_id not in existing_set]
    updated["operations"] = merged_ops
    if not new_remaining:
        updated["status"] = "complete"
        updated["verified"] = True
    else:
        updated["status"] = "partial"
        updated["verified"] = False
    return updated


def _is_promotable_to_admin(operation: Any) -> bool:
    apply = operation.apply
    return (
        operation.executor != "lens"
        and apply.kind != "lens"
        and not operation.user_owned_secrets
        and apply.secret_copy is None
        and not (apply.apply_options is not None and apply.apply_options.delete_before_apply)
    )


def promote_admin_dependencies(
    ordered_operations: Sequence[Any],
    admin_ids: Iterable[str],
    *,
    completed_ids: Iterable[str] = (),
    blocked_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Close ``admin_ids`` under reverse ``depends_on`` edges in topological order.

    Traversal halts at ``completed_ids`` so already-satisfied prerequisites (and
    their ancestors) are not re-promoted, and never promotes ``blocked_ids`` or
    operations prohibited from Orchestration administrator scripts (``lens``,
    ``user_owned_secrets``, ``secret_copy``, ``delete_before_apply``). Any seed
    IDs absent from ``ordered_operations`` keep their relative seed position.
    """
    seed_list = list(dict.fromkeys(admin_ids))
    if not seed_list:
        return ()

    ops = list(ordered_operations)
    by_id = {op.id: op for op in ops}
    seen_order: set[str] = set()
    out_of_order = False
    for op in ops:
        if any(dep in by_id and dep not in seen_order for dep in op.depends_on):
            out_of_order = True
            break
        seen_order.add(op.id)
    if out_of_order:
        from .operations import _topological_order  # noqa: PLC0415

        ops = list(_topological_order(ops))
        by_id = {op.id: op for op in ops}

    completed = set(completed_ids)
    blocked = set(blocked_ids)
    promoted: set[str] = set(seed_list)

    for op in reversed(ops):
        if op.id not in promoted:
            continue
        for dep_id in op.depends_on:
            if dep_id in promoted or dep_id in completed or dep_id in blocked:
                continue
            dep_op = by_id.get(dep_id)
            if dep_op is not None and _is_promotable_to_admin(dep_op):
                promoted.add(dep_id)

    known_ordered = [op.id for op in ops if op.id in promoted]
    if all(seed_id in by_id for seed_id in seed_list):
        return tuple(known_ordered)

    before_known: dict[str, list[str]] = {}
    pending_unknown: list[str] = []
    for seed_id in seed_list:
        if seed_id not in by_id:
            pending_unknown.append(seed_id)
        elif pending_unknown:
            before_known.setdefault(seed_id, []).extend(pending_unknown)
            pending_unknown = []
    trailing_unknown = pending_unknown

    merged: list[str] = []
    for op_id in known_ordered:
        if op_id in before_known:
            merged.extend(before_known[op_id])
        merged.append(op_id)
    merged.extend(trailing_unknown)
    return tuple(dict.fromkeys(merged))


def validate_operation_split(
    ordered_operations: Sequence[Any],
    *,
    user_ids: Iterable[str],
    admin_ids: Iterable[str],
    lens_ids: Iterable[str] = (),
    blocked_ids: Iterable[str] = (),
    completed_ids: Iterable[str] = (),
) -> None:
    """Ensure no admin operation depends on an unfinished non-admin operation."""
    from .operations import OperationDependencyError  # noqa: PLC0415

    user_set = set(user_ids)
    admin_set = set(admin_ids)
    lens_set = set(lens_ids)
    blocked_set = set(blocked_ids)
    completed_set = set(completed_ids)

    for operation in ordered_operations:
        if operation.id not in admin_set:
            continue
        for dependency in operation.depends_on:
            if dependency in admin_set or dependency in completed_set:
                continue
            if dependency in blocked_set:
                kind = "blocked"
            elif dependency in lens_set:
                kind = "lens"
            elif dependency in user_set:
                kind = "user"
            else:
                kind = "non-admin"
            raise OperationDependencyError(
                f"admin operation {operation.id!r} depends on {kind} operation {dependency!r}"
            )


async def evaluate_operations(
    cluster_id: str,
    operations: Sequence[Any],
    *,
    kubeconfig_path: str | None = None,
    plan_hash: str | None = None,
    plan_version: int = 1,
    handoff_namespace: str | None = None,
    runner: Any | None = None,
    use_cache: bool = True,
) -> OperationReport:
    """Evaluate the catalog operations and split them into user/admin/lens.

    Checks are deduplicated by their resource attributes and evaluated with the
    same batched SSAR machinery as :func:`evaluate_capabilities`. Unfinished
    promotable upstream dependencies of admin operations are automatically
    promoted into ``admin_operations`` in topological order; prohibited
    dependencies (such as user-owned secrets or Lens-side actions) fail closed.
    """

    from .operations import _topological_order  # noqa: PLC0415

    # Validate the graph even when the caller hands the evaluator a list that did
    # not come through build_operations.
    ordered_operations = _topological_order(list(operations))

    _validate_egress(cluster_id, kubeconfig_path=kubeconfig_path, runner=runner)

    resolved_plan_hash = plan_hash or ""
    operation_signatures = tuple(
        f"{operation.id}:{int(bool(getattr(operation, 'admin_only', False)))}:"
        f"{int(bool(getattr(operation, 'requires_cluster_wildcard', False)))}"
        for operation in ordered_operations
    )
    cache_key: OperationCacheKey = (
        cluster_id,
        _kubeconfig_digest(cluster_id, kubeconfig_path),
        resolved_plan_hash,
        operation_signatures,
        handoff_namespace or "",
    )
    if use_cache:
        cached = _operation_cache_get(cache_key)
        if cached is not None:
            return cached

    keys: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    operation_keys: dict[str, list[tuple[str, ...]]] = {}
    unbound_checks: dict[str, list[Any]] = {}
    key_operations: dict[tuple[str, ...], set[str]] = {}
    needs_wildcard_probe = False
    for operation in ordered_operations:
        if getattr(operation, "requires_cluster_wildcard", False):
            needs_wildcard_probe = True
        operation_keys[operation.id], unbound_checks[operation.id] = _operation_check_keys(operation)
        for key in operation_keys[operation.id]:
            if key not in seen:
                seen.add(key)
                keys.append(key)
            key_operations.setdefault(key, set()).add(operation.id)
    if needs_wildcard_probe and _CLUSTER_WILDCARD_KEY not in seen:
        seen.add(_CLUSTER_WILDCARD_KEY)
        keys.append(_CLUSTER_WILDCARD_KEY)

    answers: dict[tuple[str, ...], bool] = {}
    if keys:
        active_runner = runner if runner is not None else _default_runner(cluster_id)
        answers = await _run_checks(
            keys,
            runner=active_runner,
            kubeconfig_path=kubeconfig_path,
            deadline=time.monotonic() + TOTAL_BUDGET_SECONDS,
        )

    cluster_wildcard_allowed = answers.get(_CLUSTER_WILDCARD_KEY) is True

    by_id = {operation.id: operation for operation in ordered_operations}
    lens_operations: list[dict[str, Any]] = []
    user_operations: list[dict[str, Any]] = []
    admin_operations: list[dict[str, Any]] = []
    blocked_operations: list[dict[str, Any]] = []
    denied_keys_by_id: dict[str, list[tuple[str, ...]]] = {}
    unbound_denied_by_id: dict[str, list[dict[str, Any]]] = {}
    wildcard_denied_by_id: dict[str, bool] = {}
    denied_rules_by_id: dict[str, list[dict[str, Any]]] = {}
    blocked_executor_by_id: dict[str, str] = {}
    lens_ids: set[str] = set()
    blocked_ids: set[str] = set()
    user_ids: set[str] = set()
    admin_ids: set[str] = set()

    for operation in ordered_operations:
        requires_wildcard = bool(getattr(operation, "requires_cluster_wildcard", False))
        is_lens = operation.executor == "lens" or operation.apply.kind == "lens"
        wildcard_bypass = (
            requires_wildcard
            and cluster_wildcard_allowed
            and not is_lens
            and not operation.user_owned_secrets
            and not operation.admin_only
        )
        denied_keys = [key for key in operation_keys[operation.id] if answers.get(key) is not True]
        denied_keys_by_id[operation.id] = denied_keys
        denied_rules = [_missing_rule(key) for key in denied_keys]
        wildcard_denied = requires_wildcard and not cluster_wildcard_allowed
        wildcard_denied_by_id[operation.id] = wildcard_denied
        if wildcard_denied:
            wildcard_rule = _missing_rule(_CLUSTER_WILDCARD_KEY)
            if wildcard_rule not in denied_rules:
                denied_rules.append(wildcard_rule)
        unbound_rules: list[dict[str, Any]] = []
        if not wildcard_bypass:
            for check in unbound_checks[operation.id]:
                unbound_rule = {
                    "verb": check.verb,
                    "apiGroup": check.api_group,
                    "resource": check.resource,
                    "scope": "namespace",
                    "reason": "namespace target is not bound",
                }
                unbound_rules.append(unbound_rule)
                denied_rules.append(unbound_rule)
        unbound_denied_by_id[operation.id] = unbound_rules
        denied_rules_by_id[operation.id] = denied_rules
        if is_lens:
            if denied_rules:
                blocked_ids.add(operation.id)
                blocked_executor_by_id[operation.id] = "lens"
            else:
                lens_ids.add(operation.id)
            continue
        forced_admin = (
            operation.admin_only
            or (requires_wildcard and not cluster_wildcard_allowed)
            or (bool(unbound_checks[operation.id]) and not wildcard_bypass)
        )
        if operation.user_owned_secrets:
            if denied_rules:
                blocked_ids.add(operation.id)
                blocked_executor_by_id[operation.id] = "user"
            else:
                user_ids.add(operation.id)
        elif forced_admin or denied_keys:
            admin_ids.add(operation.id)
        else:
            user_ids.add(operation.id)

    closed_admin_ids = set(
        promote_admin_dependencies(
            ordered_operations,
            tuple(operation.id for operation in ordered_operations if operation.id in admin_ids),
            blocked_ids=blocked_ids,
        )
    )
    user_ids -= closed_admin_ids - admin_ids
    admin_ids = closed_admin_ids

    required_admin_ids = tuple(operation.id for operation in ordered_operations if operation.id in admin_ids)
    marker_runner = runner
    if marker_runner is None and handoff_namespace:
        marker_runner = _default_runner(cluster_id)
    marker = (
        await _read_marker(
            runner=marker_runner,
            cluster_id=cluster_id,
            handoff_namespace=handoff_namespace,
            plan_hash=resolved_plan_hash,
            kubeconfig_path=kubeconfig_path,
            required_operation_ids=required_admin_ids,
        )
        if marker_runner is not None and resolved_plan_hash
        else {
            "namespace": handoff_namespace,
            "name": f"lens-admin-handoff-{resolved_plan_hash[:16]}" if resolved_plan_hash else None,
            "status": "unknown",
            "verified": False,
        }
    )

    storage_pvc_op = by_id.get("storage.pvc")
    storage_ns = getattr(storage_pvc_op, "namespace", None) if storage_pvc_op is not None else None
    if ("storage.namespace" in admin_ids or "storage.pvc" in admin_ids) and not marker.get("verified") and storage_ns:
        for op_id in ("model-cache.token-secret", "model-cache.job"):
            if op_id not in blocked_ids:
                continue
            op = by_id.get(op_id)
            if op is None:
                continue
            remaining_keys: list[tuple[str, ...]] = []
            for key in denied_keys_by_id.get(op_id, []):
                if key[0] == _RESOURCE:
                    _, verb, api_group, resource, subresource, namespace, _name = key
                    if namespace == storage_ns and _storage_operator_role_grants(
                        api_group, resource, subresource, verb
                    ):
                        continue
                remaining_keys.append(key)
            remaining_rules = [_missing_rule(key) for key in remaining_keys]
            if wildcard_denied_by_id.get(op_id):
                wildcard_rule = _missing_rule(_CLUSTER_WILDCARD_KEY)
                if wildcard_rule not in remaining_rules:
                    remaining_rules.append(wildcard_rule)
            remaining_rules.extend(unbound_denied_by_id.get(op_id, []))
            denied_rules_by_id[op_id] = remaining_rules
            if not remaining_rules:
                blocked_ids.discard(op_id)
                blocked_executor_by_id.pop(op_id, None)
                is_lens_op = op.executor == "lens" or op.apply.kind == "lens"
                if is_lens_op:
                    lens_ids.add(op_id)
                else:
                    user_ids.add(op_id)

    validate_operation_split(
        ordered_operations,
        user_ids=user_ids,
        admin_ids=admin_ids,
        lens_ids=lens_ids,
        blocked_ids=blocked_ids,
    )

    missing = [
        {"operationId": operation.id, **rule}
        for operation in ordered_operations
        for rule in denied_rules_by_id[operation.id]
    ]

    for operation in ordered_operations:
        op_id = operation.id
        op_denied = denied_rules_by_id[op_id]
        if op_id in blocked_ids:
            blocked_operations.append(_operation_entry(operation, blocked_executor_by_id[op_id], op_denied))
        elif op_id in lens_ids:
            lens_operations.append(_operation_entry(operation, "lens"))
        elif op_id in admin_ids:
            admin_operations.append(_operation_entry(operation, "admin", op_denied or None))
        else:
            user_operations.append(_operation_entry(operation, "user"))

    if blocked_operations:
        status: Literal["ready", "admin_required", "blocked"] = "blocked"
    elif admin_operations:
        status = "admin_required"
    else:
        status = "ready"
    report = OperationReport(
        plan_hash=resolved_plan_hash,
        plan_version=plan_version,
        status=status,
        sufficient=not admin_operations and not blocked_operations,
        identity=None,
        checked_at=datetime.now(UTC),
        user_operations=user_operations,
        admin_operations=admin_operations,
        lens_operations=lens_operations,
        blocked_operations=blocked_operations,
        missing=missing,
        marker=marker,
    )
    if use_cache:
        _operation_cache_put(cache_key, report)
    return report
