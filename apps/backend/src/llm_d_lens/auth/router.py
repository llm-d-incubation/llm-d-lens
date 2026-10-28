"""Auth, user, group, role, identity-provider and audit HTTP endpoints.

See design sections 5.5 and 11. Enforcement is provided globally by
``auth.context.require_route_permission``; handlers own validation, scope
filtering and cookie/session mechanics.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request, Response

from llm_d_lens.auth import master_key
from llm_d_lens.auth.bootstrap import clear_initial_admin_credentials
from llm_d_lens.auth.context import CSRF_COOKIE, SESSION_COOKIE, _verify_csrf, cookie_token_from_request
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.auth.models import (
    AccessGroupRoleBindingResponse,
    AccessRequest,
    AccessUserRoleBindingResponse,
    AuditLogResponse,
    AuditLogsResponse,
    AuthProviderOption,
    BindingRequest,
    BootstrapRequest,
    DirectorySyncResponse,
    GroupCreateRequest,
    GroupResponse,
    GroupRoleBindingResponse,
    GroupRoleSummary,
    GroupUpdateRequest,
    GroupWithRolesResponse,
    IdentityProviderResponse,
    LoginRequest,
    LoginResponse,
    MembersRequest,
    PasswordChangeRequest,
    PermissionResponse,
    PrincipalResponse,
    ProviderRequest,
    ProviderTestResponse,
    RefreshResponse,
    ResetPasswordRequest,
    RoleCreateRequest,
    RolePermissionsRequest,
    RoleResponse,
    RoleUpdateRequest,
    RoleWithPermissionsResponse,
    RotateSecretKeyRequest,
    SecretKeyRotateResponse,
    SecretKeyStatusResponse,
    SessionResponse,
    UserCreateRequest,
    UserGroupResponse,
    UserGroupSummary,
    UserInheritedRole,
    UserResponse,
    UserRoleBindingResponse,
    UserRoleSummary,
    UserUpdateRequest,
    UserWithAccessResponse,
)
from llm_d_lens.auth.permissions import ALL_PERMISSIONS
from llm_d_lens.auth.policy import accessible_cluster_ids, authorize, principal_permissions
from llm_d_lens.auth.providers.registry import get_provider
from llm_d_lens.auth.records import (
    GroupRoleBindingRecord,
    IdentityProviderRecord,
    RoleRecord,
    UserRecord,
    UserRoleBindingRecord,
)
from llm_d_lens.auth.service import default_service, is_protected_user
from llm_d_lens.auth.settings import get_settings
from llm_d_lens.core.exceptions import DomainValidationError, NotFoundError, UnauthenticatedError

router = APIRouter(prefix="/api/v1", tags=["auth"])


# --- serialization helpers ---------------------------------------------------
def _user_public(user: UserRecord) -> UserResponse:
    return UserResponse.from_record(user, protected=is_protected_user(user))


def _binding_json(
    record: UserRoleBindingRecord | GroupRoleBindingRecord,
) -> UserRoleBindingResponse | GroupRoleBindingResponse:
    if isinstance(record, UserRoleBindingRecord):
        return UserRoleBindingResponse.from_record(record)
    return GroupRoleBindingResponse.from_record(record)


def _role_json(role: RoleRecord) -> RoleWithPermissionsResponse:
    service = default_service()
    permissions = sorted(p.permission for p in service.role_permission_dao.list_for_role(role.id))
    return RoleWithPermissionsResponse(
        **RoleResponse.from_record(role).model_dump(),
        permissions=permissions,
    )


def _provider_public(record: IdentityProviderRecord) -> IdentityProviderResponse:
    return IdentityProviderResponse.from_record(record)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise UnauthenticatedError("authentication required")
    return principal


def _set_session_cookies(response: Response, token: str, *, remember: bool = False) -> None:
    settings = get_settings()
    # Max-Age uses the absolute TTL, not the idle timeout: the idle cutoff is
    # enforced server-side on every request (session.idle_expires_at), and
    # slides forward while the user is active. Capping the cookie itself at
    # the (shorter) idle window would silently log an active user out the
    # instant that many seconds had passed since login, regardless of
    # activity -- exactly the sliding-renewal behavior this is meant to allow.
    max_age = settings.remember_session_ttl_seconds if remember else settings.session_ttl_seconds
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=max_age,
        httponly=False,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )


def _principal_view(principal: Principal) -> PrincipalResponse:
    clusters = accessible_cluster_ids(principal)
    return PrincipalResponse(
        user_id=principal.user_id,
        username=principal.username,
        permissions=sorted(principal_permissions(principal)),
        reachable_clusters=None if clusters is None else sorted(clusters),
        must_change_password=principal.must_change_password,
        authz_version=principal.authz_version,
    )


# --- auth --------------------------------------------------------------------
@router.get("/auth/providers", response_model=list[AuthProviderOption])
async def list_providers() -> list[AuthProviderOption]:
    service = default_service()
    providers = [AuthProviderOption(type="local", name="Lens", enabled=True, is_default=True)]
    for record in service.identity_provider_dao.list_enabled():
        if record.type == "local":
            continue
        providers.append(
            AuthProviderOption(
                type=record.type,
                name=record.name,
                enabled=record.enabled,
                is_default=record.is_default,
            )
        )
    return providers


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    service = default_service()
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    # No global auth-mode switch: ``login`` checks each username against
    # whichever source actually owns that account (local password, its own
    # identity provider, or — for a first-time directory login — every
    # enabled external provider).
    result = await service.login(body.username, body.password, ip=ip, user_agent=user_agent, remember=body.remember)
    _set_session_cookies(response, result.token, remember=body.remember)
    return LoginResponse(
        user=_user_public(result.user),
        principal=_principal_view(result.principal),
        access_token=result.access_token,
        expires_in=result.expires_in,
    )


@router.post("/auth/refresh", response_model=RefreshResponse)
async def refresh(request: Request) -> RefreshResponse:
    _verify_csrf(request)
    token = cookie_token_from_request(request)
    result = default_service().refresh_access_token(token or "")
    return RefreshResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        principal=_principal_view(result.principal),
    )


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    service = default_service()
    session_id = getattr(request.state, "principal_session_id", None)
    token = getattr(request.state, "principal_token", None)
    if session_id:
        service.logout_session(session_id)
    elif token:
        service.logout(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.get("/auth/session", response_model=PrincipalResponse)
async def current_session(request: Request) -> PrincipalResponse:
    return _principal_view(_principal(request))


@router.post("/auth/password", status_code=204)
async def change_password(body: PasswordChangeRequest, request: Request) -> None:
    principal = _principal(request)
    default_service().change_password(
        principal.user_id,
        body.old_password,
        body.new_password,
        keep_token=getattr(request.state, "principal_token", None),
        keep_session_id=getattr(request.state, "principal_session_id", None),
    )
    # The first-run credential file is no longer needed once the password changes.
    clear_initial_admin_credentials()


@router.get("/auth/sessions", response_model=list[SessionResponse])
async def my_sessions(request: Request) -> list[SessionResponse]:
    principal = _principal(request)
    return [SessionResponse.from_record(record) for record in default_service().list_sessions(principal.user_id)]


@router.get("/auth/permissions", response_model=list[PermissionResponse])
async def permission_catalog() -> list[PermissionResponse]:
    return [
        PermissionResponse(code=p.code, domain=p.domain, description=p.description, risk=p.risk)
        for p in sorted(ALL_PERMISSIONS.values(), key=lambda permission: permission.code)
    ]


@router.post("/auth/bootstrap", response_model=UserResponse)
async def bootstrap(body: BootstrapRequest) -> UserResponse:
    user = default_service().bootstrap_admin(body.username, body.password)
    return _user_public(user)


# --- sessions ---------------------------------------------------------------
@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(request: Request, include_revoked: bool = False) -> list[SessionResponse]:
    principal = _principal(request)
    service = default_service()
    if authorize(principal, "session:session:revoke").allowed:
        usernames = {user.id: user.username for user in service.user_dao.list()}
        return [
            SessionResponse.from_record(
                record,
                username=usernames.get(record.user_id, record.user_id),
            )
            for record in service.session_dao.list_all(include_revoked=include_revoked)
        ]
    return [SessionResponse.from_record(record) for record in service.list_sessions(principal.user_id)]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: str, request: Request) -> None:
    principal = _principal(request)
    default_service().revoke_session(
        session_id,
        actor=principal,
        allow_any=authorize(principal, "session:session:revoke").allowed,
    )


# --- users ------------------------------------------------------------------
@router.get("/users", response_model=list[UserWithAccessResponse])
async def list_users(query: str | None = None, status: str | None = None) -> list[UserWithAccessResponse]:
    service = default_service()
    role_names = {role.id: role.name for role in service.role_dao.list()}
    group_names = {group.id: group.name for group in service.group_dao.list()}
    bindings_by_user: dict[str, list[Any]] = {}
    for binding in service.user_binding_dao.list_all():
        bindings_by_user.setdefault(binding.user_id, []).append(binding)
    groups_by_user: dict[str, list[Any]] = {}
    for membership in service.user_group_dao.list_all():
        groups_by_user.setdefault(membership.user_id, []).append(membership)
    bindings_by_group: dict[str, list[Any]] = {}
    for binding in service.group_binding_dao.list_all():
        bindings_by_group.setdefault(binding.group_id, []).append(binding)
    items: list[UserWithAccessResponse] = []
    for user in service.user_dao.list(query=query, status=status):
        memberships = groups_by_user.get(user.id, [])
        items.append(
            UserWithAccessResponse(
                **_user_public(user).model_dump(),
                roles=[
                    UserRoleSummary(
                        name=role_names.get(binding.role_id, binding.role_id),
                        scope_type=binding.scope_type,
                        scope_cluster_id=binding.scope_cluster_id,
                    )
                    for binding in bindings_by_user.get(user.id, [])
                ],
                groups=[
                    UserGroupSummary(
                        id=membership.group_id,
                        name=group_names.get(membership.group_id, membership.group_id),
                        source=membership.source,
                    )
                    for membership in memberships
                ],
                inherited_roles=[
                    UserInheritedRole(
                        name=role_names.get(binding.role_id, binding.role_id),
                        scope_type=binding.scope_type,
                        scope_cluster_id=binding.scope_cluster_id,
                        scope_resource_type=binding.scope_resource_type,
                        scope_resource_id=binding.scope_resource_id,
                        group_id=membership.group_id,
                        group_name=group_names.get(membership.group_id, membership.group_id),
                    )
                    for membership in memberships
                    for binding in bindings_by_group.get(membership.group_id, [])
                ],
            )
        )
    return items


@router.post("/users", status_code=201, response_model=UserResponse)
async def create_user(body: UserCreateRequest) -> UserResponse:
    user = default_service().create_user(
        username=body.username,
        password=body.password,
        display_name=body.display_name,
        email=body.email,
    )
    return _user_public(user)


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: str) -> UserResponse:
    user = default_service().user_dao.get(user_id)
    if user is None:
        raise NotFoundError("user not found")
    return _user_public(user)


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(user_id: str, body: UserUpdateRequest) -> UserResponse:
    updates = body.model_dump(exclude_none=True)
    return _user_public(default_service().update_user(user_id, **updates))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, request: Request) -> None:
    default_service().delete_user(user_id, actor=_principal(request))


@router.post("/users/{user_id}/password", status_code=204)
async def reset_password(user_id: str, body: ResetPasswordRequest) -> None:
    default_service().reset_password(user_id, body.new_password)


@router.get("/users/{user_id}/groups", response_model=list[UserGroupResponse])
async def user_groups(user_id: str) -> list[UserGroupResponse]:
    return [UserGroupResponse.from_record(record) for record in default_service().user_group_dao.list_for_user(user_id)]


@router.put("/users/{user_id}/groups", status_code=204)
async def set_user_groups(user_id: str, body: MembersRequest) -> None:
    service = default_service()
    current = {membership.group_id for membership in service.user_group_dao.list_for_user(user_id)}
    target = set(body.user_ids)
    for group_id in target - current:
        service.add_group_member(group_id, user_id)
    for group_id in current - target:
        service.remove_group_member(group_id, user_id)


@router.get("/users/{user_id}/role-bindings", response_model=list[UserRoleBindingResponse])
async def user_bindings(user_id: str) -> list[UserRoleBindingResponse]:
    return [
        UserRoleBindingResponse.from_record(record)
        for record in default_service().user_binding_dao.list_for_user(user_id)
    ]


@router.post(
    "/users/{user_id}/role-bindings",
    status_code=201,
    response_model=UserRoleBindingResponse,
    summary="Assign a user role at global, cluster or model-inference resource scope.",
    description="Requires role binding management. Model resource bindings accept only model_service_group and "
    "the built-in end-user role, with a server-validated model cluster and existing recipient inference scope. "
    "Deployment resource bindings are retired and rejected.",
)
async def add_user_binding(
    user_id: str,
    body: BindingRequest,
    request: Request,
) -> UserRoleBindingResponse:
    actor = _principal(request)
    record = default_service().add_user_binding(
        user_id,
        body.role_id,
        scope_type=body.scope_type,
        scope_cluster_id=body.scope_cluster_id,
        scope_resource_type=body.scope_resource_type,
        scope_resource_id=body.scope_resource_id,
        granted_by=actor.user_id,
    )
    return _binding_json(record)


@router.delete("/users/{user_id}/role-bindings/{binding_id}", status_code=204)
async def remove_user_binding(user_id: str, binding_id: str) -> None:
    default_service().remove_user_binding(user_id, binding_id)


# --- groups -----------------------------------------------------------------
@router.get("/groups", response_model=list[GroupWithRolesResponse])
async def list_groups() -> list[GroupWithRolesResponse]:
    service = default_service()
    role_names = {role.id: role.name for role in service.role_dao.list()}
    bindings_by_group: dict[str, list[Any]] = {}
    for binding in service.group_binding_dao.list_all():
        bindings_by_group.setdefault(binding.group_id, []).append(binding)
    items: list[GroupWithRolesResponse] = []
    for group in service.group_dao.list():
        items.append(
            GroupWithRolesResponse(
                **GroupResponse.from_record(group).model_dump(),
                roles=[
                    GroupRoleSummary(
                        name=role_names.get(binding.role_id, binding.role_id),
                        scope_type=binding.scope_type,
                        scope_cluster_id=binding.scope_cluster_id,
                        scope_resource_type=binding.scope_resource_type,
                        scope_resource_id=binding.scope_resource_id,
                    )
                    for binding in bindings_by_group.get(group.id, [])
                ],
            )
        )
    return items


@router.post("/groups", status_code=201, response_model=GroupResponse)
async def create_group(body: GroupCreateRequest) -> GroupResponse:
    return GroupResponse.from_record(default_service().create_group(name=body.name, description=body.description))


@router.get("/groups/{group_id}", response_model=GroupResponse)
async def get_group(group_id: str) -> GroupResponse:
    group = default_service().group_dao.get(group_id)
    if group is None:
        raise NotFoundError("group not found")
    return GroupResponse.from_record(group)


@router.patch("/groups/{group_id}", response_model=GroupResponse)
async def update_group(group_id: str, body: GroupUpdateRequest) -> GroupResponse:
    updates = body.model_dump(exclude_none=True)
    return GroupResponse.from_record(default_service().update_group(group_id, **updates))


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: str) -> None:
    default_service().delete_group(group_id)


@router.get("/groups/{group_id}/members", response_model=list[UserGroupResponse])
async def list_group_members(group_id: str) -> list[UserGroupResponse]:
    return [
        UserGroupResponse.from_record(record) for record in default_service().user_group_dao.list_for_group(group_id)
    ]


@router.put("/groups/{group_id}/members", status_code=204)
async def set_group_members(group_id: str, body: MembersRequest) -> None:
    service = default_service()
    current = {membership.user_id for membership in service.user_group_dao.list_for_group(group_id)}
    target = set(body.user_ids)
    for user_id in target - current:
        service.add_group_member(group_id, user_id)
    for user_id in current - target:
        service.remove_group_member(group_id, user_id)


@router.get("/groups/{group_id}/role-bindings", response_model=list[GroupRoleBindingResponse])
async def group_bindings(group_id: str) -> list[GroupRoleBindingResponse]:
    return [
        GroupRoleBindingResponse.from_record(record)
        for record in default_service().group_binding_dao.list_for_group(group_id)
    ]


@router.post(
    "/groups/{group_id}/role-bindings",
    status_code=201,
    response_model=GroupRoleBindingResponse,
    summary="Assign a group role at global, cluster or model-inference resource scope.",
    description="Requires role binding management. Resource grants only support model_service_group with the "
    "built-in end-user role. The group must already have inference permission in the model's cluster. "
    "Deployment resource bindings are retired and rejected.",
)
async def add_group_binding(
    group_id: str,
    body: BindingRequest,
    request: Request,
) -> GroupRoleBindingResponse:
    actor = _principal(request)
    record = default_service().add_group_binding(
        group_id,
        body.role_id,
        scope_type=body.scope_type,
        scope_cluster_id=body.scope_cluster_id,
        scope_resource_type=body.scope_resource_type,
        scope_resource_id=body.scope_resource_id,
        granted_by=actor.user_id,
    )
    return _binding_json(record)


@router.delete("/groups/{group_id}/role-bindings/{binding_id}", status_code=204)
async def remove_group_binding(group_id: str, binding_id: str) -> None:
    default_service().remove_group_binding(group_id, binding_id)


# --- roles ------------------------------------------------------------------
@router.get("/roles", response_model=list[RoleWithPermissionsResponse])
async def list_roles() -> list[RoleWithPermissionsResponse]:
    return [_role_json(role) for role in default_service().role_dao.list()]


@router.post("/roles", status_code=201, response_model=RoleWithPermissionsResponse)
async def create_role(body: RoleCreateRequest) -> RoleWithPermissionsResponse:
    role = default_service().create_role(
        name=body.name, description=body.description, permissions=set(body.permissions)
    )
    return _role_json(role)


@router.get("/roles/{role_id}", response_model=RoleWithPermissionsResponse)
async def get_role(role_id: str) -> RoleWithPermissionsResponse:
    role = default_service().role_dao.get(role_id)
    if role is None:
        raise NotFoundError("role not found")
    return _role_json(role)


@router.patch("/roles/{role_id}", response_model=RoleResponse)
async def update_role(role_id: str, body: RoleUpdateRequest) -> RoleResponse:
    role = default_service().require_role(role_id)
    if role.is_builtin:
        raise DomainValidationError("built-in roles cannot be edited")
    updates = body.model_dump(exclude_none=True)
    return RoleResponse.from_record(default_service().role_dao.save(_replace(role, updates)))


def _replace(record: RoleRecord, updates: dict[str, Any]) -> RoleRecord:
    for key, value in updates.items():
        setattr(record, key, value)
    return record


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: str) -> None:
    role = default_service().require_role(role_id)
    if role.is_builtin:
        raise DomainValidationError("built-in roles cannot be deleted")
    default_service().role_dao.delete(role_id)


@router.put("/roles/{role_id}/permissions", status_code=204)
async def set_role_permissions(role_id: str, body: RolePermissionsRequest) -> None:
    default_service().set_role_permissions(role_id, set(body.permissions))


# --- identity providers -----------------------------------------------------
@router.get("/identity-providers", response_model=list[IdentityProviderResponse])
async def list_identity_providers() -> list[IdentityProviderResponse]:
    return [_provider_public(record) for record in default_service().identity_provider_dao.list()]


@router.post("/identity-providers", status_code=201, response_model=IdentityProviderResponse)
async def create_identity_provider(body: ProviderRequest) -> IdentityProviderResponse:
    service = default_service()
    record = service.identity_provider_dao.create(
        IdentityProviderRecord(
            type=body.type,
            name=body.name,
            enabled=body.enabled,
            is_default=body.is_default,
            config=body.config,
            sync_mode=body.sync_mode,
        )
    )
    if body.secret:
        record.secret_encrypted = service.encrypt_provider_secret(body.secret)
        record = service.identity_provider_dao.save(record)
    return _provider_public(record)


@router.get("/identity-providers/{provider_id}", response_model=IdentityProviderResponse)
async def get_identity_provider(provider_id: str) -> IdentityProviderResponse:
    record = default_service().identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    return _provider_public(record)


@router.patch("/identity-providers/{provider_id}", response_model=IdentityProviderResponse)
async def update_identity_provider(provider_id: str, body: ProviderRequest) -> IdentityProviderResponse:
    service = default_service()
    record = service.identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    record.type = body.type
    record.name = body.name
    record.enabled = body.enabled
    record.is_default = body.is_default
    record.config = body.config
    record.sync_mode = body.sync_mode
    if body.secret:
        record.secret_encrypted = service.encrypt_provider_secret(body.secret)
    return _provider_public(service.identity_provider_dao.save(record))


@router.delete("/identity-providers/{provider_id}", status_code=204)
async def delete_identity_provider(provider_id: str) -> None:
    default_service().delete_identity_provider(provider_id)


@router.post("/identity-providers/{provider_id}/test", response_model=ProviderTestResponse)
async def test_identity_provider(provider_id: str) -> ProviderTestResponse:
    service = default_service()
    record = service.identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    provider = get_provider(record, secret=service.decrypt_provider_secret(record.secret_encrypted))
    result = await provider.test_connection()
    return ProviderTestResponse(ok=result.ok, detail=result.detail)


@router.post("/identity-providers/{provider_id}/sync", response_model=DirectorySyncResponse)
async def sync_identity_provider(provider_id: str) -> DirectorySyncResponse:
    service = default_service()
    record = service.identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    return await service.sync_directory(provider_id)


# --- audit ------------------------------------------------------------------
@router.get("/audit-logs", response_model=AuditLogsResponse)
async def list_audit_logs(
    actor_user_id: str | None = None,
    event_type: str | None = None,
    result: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    cluster_id: str | None = None,
    source: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AuditLogsResponse:
    dao = default_service().audit_dao
    filters = {
        "actor_user_id": actor_user_id,
        "event_type": event_type,
        "result": result,
        "since": since,
        "until": until,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "cluster_id": cluster_id,
        "source": source,
        "request_id": request_id,
        "trace_id": trace_id,
    }
    return AuditLogsResponse(
        items=[AuditLogResponse.from_record(record) for record in dao.list(**filters, limit=limit, offset=offset)],
        total=dao.count(**filters),
        limit=limit,
        offset=offset,
    )


# --- system: stored-secret master key ---------------------------------------
def _provider_secret_count(service: Any) -> int:
    return sum(1 for record in service.identity_provider_dao.list() if record.secret_encrypted)


@router.get("/system/secret-key", response_model=SecretKeyStatusResponse)
async def get_secret_key_status() -> SecretKeyStatusResponse:
    service = default_service()
    data = master_key.status(get_settings())
    data["providerSecretCount"] = _provider_secret_count(service)
    return SecretKeyStatusResponse.model_validate(data)


@router.post("/system/secret-key/rotate", response_model=SecretKeyRotateResponse)
async def rotate_secret_key(body: RotateSecretKeyRequest, request: Request) -> SecretKeyRotateResponse:
    service = default_service()
    actor = _principal(request)
    result = master_key.rotate_master_key(get_settings(), body.new_key, provider_dao=service.identity_provider_dao)
    from llm_d_lens.auth.service import invalidate_access_token_keys_cache

    invalidate_access_token_keys_cache()
    service.audit(
        "secret_key_rotated",
        actor=actor,
        target_type="system",
        target_id="secret-key",
        detail={"rotatedSecrets": result.get("rotatedSecrets", 0), "fingerprint": result["fingerprint"]},
    )
    result["providerSecretCount"] = _provider_secret_count(service)
    return SecretKeyRotateResponse.model_validate(result)


@router.delete("/system/secret-key/old", response_model=SecretKeyStatusResponse)
async def clear_old_secret_keys(request: Request) -> SecretKeyStatusResponse:
    service = default_service()
    actor = _principal(request)
    result = master_key.clear_old_keys(get_settings())
    from llm_d_lens.auth.service import invalidate_access_token_keys_cache

    invalidate_access_token_keys_cache()
    service.audit("secret_key_old_cleared", actor=actor, target_type="system", target_id="secret-key")
    result["providerSecretCount"] = _provider_secret_count(service)
    return SecretKeyStatusResponse.model_validate(result)


# --- access management ------------------------------------------------------
def _list_access(
    scope_cluster_id: str | None,
    scope_resource_id: str | None,
    service,
) -> list[AccessUserRoleBindingResponse | AccessGroupRoleBindingResponse]:
    user_bindings = [
        binding
        for binding in service.user_binding_dao.list_all()
        if binding.scope_cluster_id == scope_cluster_id
        and (scope_resource_id is None or binding.scope_resource_id == scope_resource_id)
    ]
    group_bindings = [
        binding
        for binding in service.group_binding_dao.list_all()
        if binding.scope_cluster_id == scope_cluster_id
        and (scope_resource_id is None or binding.scope_resource_id == scope_resource_id)
    ]
    return [AccessUserRoleBindingResponse.from_record(binding) for binding in user_bindings] + [
        AccessGroupRoleBindingResponse.from_record(binding) for binding in group_bindings
    ]


@router.get(
    "/clusters/{cluster_id}/access",
    response_model=list[AccessUserRoleBindingResponse | AccessGroupRoleBindingResponse],
)
async def list_cluster_access(
    cluster_id: str,
) -> list[AccessUserRoleBindingResponse | AccessGroupRoleBindingResponse]:
    return _list_access(cluster_id, None, default_service())


@router.post(
    "/clusters/{cluster_id}/access",
    status_code=201,
    response_model=UserRoleBindingResponse | GroupRoleBindingResponse,
)
async def grant_cluster_access(
    cluster_id: str,
    body: AccessRequest,
    request: Request,
) -> UserRoleBindingResponse | GroupRoleBindingResponse:
    service = default_service()
    actor = _principal(request)
    if body.subject_type == "group":
        return _binding_json(
            service.add_group_binding(
                body.subject_id,
                body.role_id,
                scope_type="cluster",
                scope_cluster_id=cluster_id,
                granted_by=actor.user_id,
            )
        )
    return _binding_json(
        service.add_user_binding(
            body.subject_id, body.role_id, scope_type="cluster", scope_cluster_id=cluster_id, granted_by=actor.user_id
        )
    )


@router.delete("/clusters/{cluster_id}/access/{binding_id}", status_code=204)
async def revoke_cluster_access(cluster_id: str, binding_id: str) -> None:
    service = default_service()
    for binding in service.user_binding_dao.list_all():
        if binding.id == binding_id:
            service.remove_user_binding(binding.user_id, binding_id)
            return
    for binding in service.group_binding_dao.list_all():
        if binding.id == binding_id:
            service.remove_group_binding(binding.group_id, binding_id)
            return
    raise NotFoundError("binding not found")
