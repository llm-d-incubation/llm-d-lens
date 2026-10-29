"""Authorization policy core (design section 7.5 / 20.4).

The action + role-scope gate is delegated to the embedded Casbin adapter; the
model's ``dom`` carries the cluster scope, so a role granted on a cluster only
applies there and a ``global`` binding applies everywhere. Ownership,
owner-group rights and resource-level shares are not expressible as static
Casbin policies and stay here (design section 7.5).

The public contract (``permission_matches`` / ``authorize``) is unchanged from
the pure implementation, so callers and tests are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm_d_lens.auth.casbin_adapter import role_has_permission as _casbin_role_has_permission
from llm_d_lens.auth.contracts import Principal, ResourceRef, ScopeType
from llm_d_lens.auth.permissions import permission_granted, permission_matches  # noqa: F401  (re-export)
from llm_d_lens.auth.scope import (
    binding_applies,
    cluster_reachable,
    ownership_covers,
    reachable_clusters,
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _role_contains(principal: Principal, role: str, required: str) -> bool:
    return permission_granted(principal.permissions_for(role), required)


def role_has_permission(
    principal: Principal,
    required: str,
    *,
    resource: ResourceRef | None = None,
    target_cluster_id: str | None = None,
) -> bool:
    """Action + role-scope gate, evaluated by Casbin.

    Resource-level shares are intentionally excluded here (they have no static
    policy row) and are checked by ``_resource_share_allows``.
    """
    domain = resource.cluster_id if resource is not None else target_cluster_id
    return _casbin_role_has_permission(principal, required, domain=domain)


def accessible_cluster_ids(principal: Principal) -> set[str] | None:
    """Cluster ids used to filter list endpoints (``None`` means unrestricted)."""
    return reachable_clusters(principal)


def principal_permissions(principal: Principal) -> frozenset[str]:
    """Union of action permissions; resource grants only narrow existing scope."""
    codes: set[str] = set()
    for binding in principal.bindings:
        if binding.scope_type is not ScopeType.RESOURCE:
            codes |= set(principal.permissions_for(binding.role))
    return frozenset(codes)


def principal_global_permissions(principal: Principal) -> frozenset[str]:
    """Actions granted by GLOBAL bindings, without elevating cluster-only roles."""
    return frozenset(
        code
        for binding in principal.bindings
        if binding.scope_type is ScopeType.GLOBAL
        for code in principal.permissions_for(binding.role)
    )


def _resource_share_allows(principal: Principal, required: str, resource: ResourceRef) -> bool:
    # Resource grants only convey model inference, never management or cluster access.
    if resource.resource_type != "model_service_group" or required != "model-service:inference:use":
        return False
    if not role_has_permission(principal, required, resource=resource):
        return False
    for binding in principal.bindings:
        if binding.scope_type is not ScopeType.RESOURCE:
            continue
        if not binding_applies(binding, resource):
            continue
        if not cluster_reachable(principal, resource.cluster_id):
            continue
        if not _role_contains(principal, binding.role, required):
            continue
        if binding.role != "end-user":
            continue
        return True
    return False


def authorize(
    principal: Principal,
    required: str,
    *,
    resource: ResourceRef | None = None,
    target_cluster_id: str | None = None,
) -> Decision:
    """Decide whether ``principal`` may perform ``required`` on the target."""
    if principal.is_global_admin():
        return Decision(True, "global_admin")
    if resource is not None and _resource_share_allows(principal, required, resource):
        return Decision(True, "resource_share")
    if not role_has_permission(principal, required, resource=resource, target_cluster_id=target_cluster_id):
        return Decision(False, "action_not_granted")
    if resource is None:
        # List/global/create: the action gate is sufficient; callers apply
        # accessible_cluster_ids when querying and pass target_cluster_id on create.
        return Decision(True, "action_granted")
    for binding in principal.bindings:
        if binding.scope_type is ScopeType.RESOURCE:
            continue
        if not binding_applies(binding, resource):
            continue
        if not _role_contains(principal, binding.role, required):
            continue
        # Global/cluster bindings do not grant self-scoped actions (e.g. deleting
        # someone else's deployment) unless ownership still applies below.
        if required in principal.self_scoped_for(binding.role):
            continue
        return Decision(True, "binding_scope")
    if resource.resource_type == "model_service_group" and required == "model-service:inference:use":
        return Decision(False, "model_grant_required")
    if ownership_covers(principal, resource, required):
        return Decision(True, "ownership")
    return Decision(False, "out_of_scope")
