"""Detect and clean deployment namespaces that Lens no longer tracks.

A deployment's namespace is the only handle Lens keeps on its cluster
resources. When the Lens record is removed without namespace cleanup (or the
database is rebuilt), the namespace and its InferencePool/EPP/model server are
left behind with nothing pointing at them. This module finds namespaces under
the configured deployment prefix that no v1 execution references and that hold
no Lens engine instance, and can delete a caller-selected subset. Uncheckable
namespaces are never deleted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from llm_d_lens.deploy.run_store import JsonDeploymentRunStore
from llm_d_lens.infrastructure.kubernetes import scoped_runner

DEFAULT_NAMESPACE_PREFIX = "llmd-"
# Inventory contract: apps/lens/lens/inventory.py and apps/lens/lens/model.py.
ENGINE_INVENTORY_SELECTOR = "lens.llm-d.ai/role=inventory,app.kubernetes.io/managed-by=llm-d-lens"


class OrphanCleanupError(Exception):
    """Orphan namespace scan or cleanup failed."""


@dataclass(frozen=True)
class OrphanNamespace:
    name: str
    created_at: str | None = None
    phase: str | None = None

    def api_payload(self) -> dict:
        return {"name": self.name, "createdAt": self.created_at, "phase": self.phase}


def namespace_prefix() -> str:
    """The configured deployment namespace prefix (mirrors LLM_D_BENCH_NAMESPACE_PREFIX)."""
    prefix = (os.environ.get("LLM_D_BENCH_NAMESPACE_PREFIX") or DEFAULT_NAMESPACE_PREFIX).strip()
    return prefix or DEFAULT_NAMESPACE_PREFIX


def known_namespaces(store: JsonDeploymentRunStore | None = None) -> set[str]:
    """Namespaces currently referenced by a Lens deployment execution record."""
    store = store or JsonDeploymentRunStore()
    return {execution.namespace for execution in store.list_executions() if getattr(execution, "namespace", None)}


async def _engine_instance_namespaces(
    cluster_id: str, namespaces: set[str], *, stop_on_failure: bool = False
) -> tuple[set[str], dict[str, str]]:
    """Find engine inventories, falling back to namespace-scoped checks on failure."""
    if not namespaces:
        return set(), {}
    runner = scoped_runner(cluster_id)

    async def query(scope: list[str]) -> list[dict]:
        try:
            result = await runner.run(
                ["kubectl", "get", "configmaps", *scope, "-l", ENGINE_INVENTORY_SELECTOR, "-o", "json"], timeout=30
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            raise OrphanCleanupError(f"kubectl unavailable: {error}") from error
        if result.returncode != 0:
            raise OrphanCleanupError((result.stderr or result.stdout or "kubectl failed").strip())
        try:
            payload = json.loads(result.stdout or "")
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise ValueError("expected a ConfigMap list")
            items = payload["items"]
            if any(not isinstance(item, dict) or not (item.get("metadata") or {}).get("namespace") for item in items):
                raise ValueError("missing ConfigMap namespace")
        except (ValueError, AttributeError) as error:
            raise OrphanCleanupError("invalid configmaps JSON") from error
        return items

    try:
        items = await query(["-A"])
    except OrphanCleanupError:
        pass
    else:
        return {item["metadata"]["namespace"] for item in items} & namespaces, {}

    protected: set[str] = set()
    failed: dict[str, str] = {}
    for name in sorted(namespaces):
        try:
            items = await query(["-n", name])
        except OrphanCleanupError as error:
            failed[name] = str(error)
            if stop_on_failure:
                break
        else:
            if items:
                protected.add(name)
    return protected, failed


async def scan_orphan_namespaces(
    cluster_id: str, *, store: JsonDeploymentRunStore | None = None
) -> list[OrphanNamespace]:
    """List prefixed namespaces with no v1 record or engine instance; fail if uncheckable."""
    prefix = namespace_prefix()
    known = known_namespaces(store)
    runner = scoped_runner(cluster_id)
    try:
        result = await runner.run(["kubectl", "get", "namespaces", "-o", "json"], timeout=30)
    except (FileNotFoundError, TimeoutError, OSError) as error:
        raise OrphanCleanupError(f"kubectl unavailable: {error}") from error
    if result.returncode != 0:
        raise OrphanCleanupError((result.stderr or result.stdout or "kubectl failed").strip())
    try:
        items = json.loads(result.stdout or "{}").get("items", [])
    except json.JSONDecodeError as error:
        raise OrphanCleanupError("invalid namespaces JSON") from error
    orphans: list[OrphanNamespace] = []
    for item in items:
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        if not name or name == prefix or not name.startswith(prefix) or name in known:
            continue
        orphans.append(
            OrphanNamespace(
                name=name,
                created_at=metadata.get("creationTimestamp"),
                phase=(item.get("status") or {}).get("phase"),
            )
        )
    protected, failed = await _engine_instance_namespaces(
        cluster_id, {orphan.name for orphan in orphans}, stop_on_failure=True
    )
    if failed:
        name, error = next(iter(failed.items()))
        raise OrphanCleanupError(f"could not check for Lens engine instances in namespace {name}: {error}")
    return sorted(
        (orphan for orphan in orphans if orphan.name not in protected), key=lambda orphan: orphan.created_at or ""
    )


async def clean_orphan_namespaces(
    cluster_id: str, namespaces: list[str], *, store: JsonDeploymentRunStore | None = None
) -> tuple[list[str], list[dict]]:
    """Delete the named orphan namespaces, returning (cleaned, failures).

    Names outside the deployment prefix, or still referenced by a Lens record,
    are refused rather than deleted. Names holding a Lens engine instance, or
    whose engine inventory cannot be checked, are also refused.
    """
    prefix = namespace_prefix()
    known = known_namespaces(store)
    runner = scoped_runner(cluster_id)
    cleaned: list[str] = []
    failed: list[dict] = []
    candidates = {name for name in namespaces if name != prefix and name.startswith(prefix) and name not in known}
    protected, check_failures = await _engine_instance_namespaces(cluster_id, candidates)
    for name in dict.fromkeys(namespaces):
        if name == prefix or not name.startswith(prefix):
            failed.append({"name": name, "error": "namespace is outside the deployment prefix"})
            continue
        if name in known:
            failed.append({"name": name, "error": "namespace is still tracked by a Lens deployment"})
            continue
        if name in protected:
            failed.append({"name": name, "error": "namespace holds a Lens engine instance"})
            continue
        if name in check_failures:
            failed.append({"name": name, "error": f"could not check for Lens engine instances: {check_failures[name]}"})
            continue
        try:
            result = await runner.run(["kubectl", "delete", "namespace", name, "--ignore-not-found=true"], timeout=120)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            failed.append({"name": name, "error": f"kubectl unavailable: {error}"})
            continue
        if result.returncode == 0:
            cleaned.append(name)
        else:
            failed.append({"name": name, "error": (result.stderr or result.stdout or "delete failed").strip()[:300]})
    return cleaned, failed
