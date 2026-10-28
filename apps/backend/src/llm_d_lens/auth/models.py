"""Request and response contracts for the auth domain."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from llm_d_lens.auth.records import (
    AuditLogRecord,
    GroupRecord,
    GroupRoleBindingRecord,
    IdentityProviderRecord,
    RoleRecord,
    SessionRecord,
    UserGroupRecord,
    UserRecord,
    UserRoleBindingRecord,
)


class AuthModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


IsoDatetime = Annotated[
    datetime,
    PlainSerializer(lambda value: value.isoformat(), return_type=str, when_used="json"),
]


class LoginRequest(AuthModel):
    username: str
    password: str
    remember: bool = False


class BootstrapRequest(AuthModel):
    username: str
    password: str


class PasswordChangeRequest(AuthModel):
    old_password: str = Field(alias="oldPassword")
    new_password: str = Field(alias="newPassword")


class UserCreateRequest(AuthModel):
    username: str
    password: str | None = None
    display_name: str = Field(default="", alias="displayName")
    email: str | None = None


class UserUpdateRequest(AuthModel):
    display_name: str | None = Field(default=None, alias="displayName")
    email: str | None = None
    status: str | None = None


class ResetPasswordRequest(AuthModel):
    new_password: str = Field(alias="newPassword")


class GroupCreateRequest(AuthModel):
    name: str
    description: str = ""


class GroupUpdateRequest(AuthModel):
    name: str | None = None
    description: str | None = None


class MembersRequest(AuthModel):
    user_ids: list[str] = Field(default_factory=list, alias="userIds")


class RoleCreateRequest(AuthModel):
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)


class RoleUpdateRequest(AuthModel):
    name: str | None = None
    description: str | None = None


class RolePermissionsRequest(AuthModel):
    permissions: list[str]


class BindingRequest(AuthModel):
    role_id: str = Field(alias="roleId")
    scope_type: str = Field(default="global", alias="scopeType")
    scope_cluster_id: str | None = Field(default=None, alias="scopeClusterId")
    scope_resource_type: str | None = Field(
        default=None,
        alias="scopeResourceType",
        description="For resource scope, only model_service_group is supported, with the built-in end-user role. "
        "The recipient must independently have inference permission in the model's cluster.",
    )
    scope_resource_id: str | None = Field(default=None, alias="scopeResourceId")


class AccessRequest(AuthModel):
    subject_type: str = Field(alias="subjectType")
    subject_id: str = Field(alias="subjectId")
    role_id: str = Field(alias="roleId")
    cluster_id: str | None = Field(default=None, alias="clusterId")


class ProviderRequest(AuthModel):
    type: str
    name: str
    enabled: bool = False
    is_default: bool = Field(default=False, alias="isDefault")
    config: dict[str, Any] = Field(default_factory=dict)
    sync_mode: str = Field(default="login", alias="syncMode")
    secret: str | None = None


class RotateSecretKeyRequest(AuthModel):
    new_key: str | None = Field(default=None, alias="newKey")


class AuthProviderOption(AuthModel):
    type: str
    name: str
    enabled: bool
    is_default: bool = Field(alias="isDefault")


class PermissionResponse(AuthModel):
    code: str
    domain: str
    description: str
    risk: str


class UserRoleSummary(AuthModel):
    name: str
    scope_type: str = Field(alias="scopeType")
    scope_cluster_id: str | None = Field(default=None, alias="scopeClusterId")


class UserGroupSummary(AuthModel):
    id: str
    name: str
    source: str


class UserInheritedRole(AuthModel):
    name: str
    scope_type: str = Field(alias="scopeType")
    scope_cluster_id: str | None = Field(default=None, alias="scopeClusterId")
    scope_resource_type: str | None = Field(default=None, alias="scopeResourceType")
    scope_resource_id: str | None = Field(default=None, alias="scopeResourceId")
    group_id: str = Field(alias="groupId")
    group_name: str = Field(alias="groupName")


class UserResponse(AuthModel):
    username: str
    display_name: str = ""
    id: str
    email: str | None = None
    provider_id: str | None = None
    auth_source: str = "local"
    external_id: str | None = None
    status: str = "active"
    failed_login_count: int = 0
    locked_until: IsoDatetime | None = None
    must_change_password: bool = False
    password_changed_at: IsoDatetime | None = None
    last_login_at: IsoDatetime | None = None
    principal_version: int = 1
    created_at: IsoDatetime
    updated_at: IsoDatetime
    has_password: bool = False
    protected: bool = False

    @classmethod
    def from_record(cls, user: UserRecord, *, protected: bool) -> UserResponse:
        return cls(
            username=user.username,
            display_name=user.display_name,
            id=user.id,
            email=user.email,
            provider_id=user.provider_id,
            auth_source=user.auth_source,
            external_id=user.external_id,
            status=user.status,
            failed_login_count=user.failed_login_count,
            locked_until=user.locked_until,
            must_change_password=user.must_change_password,
            password_changed_at=user.password_changed_at,
            last_login_at=user.last_login_at,
            principal_version=user.principal_version,
            created_at=user.created_at,
            updated_at=user.updated_at,
            has_password=user.password_hash is not None,
            protected=protected,
        )


class UserWithAccessResponse(UserResponse):
    roles: list[UserRoleSummary] = Field(default_factory=list)
    groups: list[UserGroupSummary] = Field(default_factory=list)
    inherited_roles: list[UserInheritedRole] = Field(default_factory=list, alias="inheritedRoles")


class PrincipalResponse(AuthModel):
    user_id: str = Field(alias="userId")
    username: str
    permissions: list[str]
    reachable_clusters: list[str] | None = Field(default=None, alias="reachableClusters")
    must_change_password: bool = Field(default=False, alias="mustChangePassword")
    authz_version: int = Field(default=1, alias="authzVersion")


class LoginResponse(AuthModel):
    user: UserResponse
    principal: PrincipalResponse
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="Bearer", alias="tokenType")
    expires_in: int = Field(alias="expiresIn")


class RefreshResponse(AuthModel):
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="Bearer", alias="tokenType")
    expires_in: int = Field(alias="expiresIn")
    principal: PrincipalResponse


class SessionResponse(AuthModel):
    user_id: str
    id: str
    created_at: IsoDatetime
    expires_at: IsoDatetime | None = None
    idle_expires_at: IsoDatetime | None = None
    last_seen_at: IsoDatetime | None = None
    revoked_at: IsoDatetime | None = None
    auth_source: str = "local"
    ip: str | None = None
    user_agent: str | None = None
    username: str | None = None

    @classmethod
    def from_record(cls, record: SessionRecord, *, username: str | None = None) -> SessionResponse:
        data = asdict(record)
        data.pop("token_hash", None)
        return cls(username=username, **data)


class UserGroupResponse(AuthModel):
    user_id: str
    group_id: str
    source: str = "manual"
    created_at: IsoDatetime

    @classmethod
    def from_record(cls, record: UserGroupRecord) -> UserGroupResponse:
        return cls(**asdict(record))


class GroupRoleSummary(AuthModel):
    name: str
    scope_type: str = Field(alias="scopeType")
    scope_cluster_id: str | None = Field(default=None, alias="scopeClusterId")
    scope_resource_type: str | None = Field(default=None, alias="scopeResourceType")
    scope_resource_id: str | None = Field(default=None, alias="scopeResourceId")


class GroupResponse(AuthModel):
    name: str
    description: str = ""
    id: str
    source: str = "local"
    provider_id: str | None = None
    external_id: str | None = None
    authz_version: int = 1
    created_at: IsoDatetime
    updated_at: IsoDatetime

    @classmethod
    def from_record(cls, record: GroupRecord) -> GroupResponse:
        return cls(**asdict(record))


class GroupWithRolesResponse(GroupResponse):
    roles: list[GroupRoleSummary] = Field(default_factory=list)


class RoleResponse(AuthModel):
    name: str
    description: str = ""
    id: str
    is_builtin: bool = False
    created_at: IsoDatetime
    updated_at: IsoDatetime

    @classmethod
    def from_record(cls, record: RoleRecord) -> RoleResponse:
        return cls(**asdict(record))


class RoleWithPermissionsResponse(RoleResponse):
    permissions: list[str] = Field(default_factory=list)


class RoleBindingResponse(AuthModel):
    role_id: str
    id: str
    scope_type: str = "global"
    scope_cluster_id: str | None = None
    scope_resource_type: str | None = None
    scope_resource_id: str | None = None
    granted_by_user_id: str | None = None
    expires_at: IsoDatetime | None = None
    created_at: IsoDatetime


class UserRoleBindingResponse(RoleBindingResponse):
    user_id: str

    @classmethod
    def from_record(cls, record: UserRoleBindingRecord) -> UserRoleBindingResponse:
        return cls(**asdict(record))


class GroupRoleBindingResponse(RoleBindingResponse):
    group_id: str

    @classmethod
    def from_record(cls, record: GroupRoleBindingRecord) -> GroupRoleBindingResponse:
        return cls(**asdict(record))


class AccessUserRoleBindingResponse(UserRoleBindingResponse):
    subject_type: Literal["user"] = Field(default="user", alias="subjectType")


class AccessGroupRoleBindingResponse(GroupRoleBindingResponse):
    subject_type: Literal["group"] = Field(default="group", alias="subjectType")


class IdentityProviderResponse(AuthModel):
    type: str
    name: str
    id: str
    enabled: bool = False
    is_default: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    sync_mode: str = "login"
    last_sync_at: IsoDatetime | None = None
    created_at: IsoDatetime
    updated_at: IsoDatetime
    has_secret: bool = Field(default=False, alias="hasSecret")

    @classmethod
    def from_record(cls, record: IdentityProviderRecord) -> IdentityProviderResponse:
        data = asdict(record)
        data.pop("secret_encrypted", None)
        return cls(has_secret=record.secret_encrypted is not None, **data)


class ProviderTestResponse(AuthModel):
    ok: bool
    detail: str = ""


class DirectorySyncResponse(AuthModel):
    ok: bool
    groups: int = 0
    members: int = 0
    failed: list[str] = Field(default_factory=list)


class AuditLogResponse(AuthModel):
    event_type: str
    actor_username: str = ""
    id: str
    created_at: IsoDatetime
    actor_user_id: str | None = None
    permission: str | None = None
    method: str | None = None
    path: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    cluster_id: str | None = None
    result: str = "success"
    detail: dict[str, Any] | None = None
    ip: str | None = None
    source: str = "system"
    action: str = "execute"
    target_name: str | None = None
    status_code: int | None = None
    duration_ms: int | None = None
    request_id: str | None = None
    trace_id: str | None = None
    parent_request_id: str | None = None
    user_agent: str | None = None

    @classmethod
    def from_record(cls, record: AuditLogRecord) -> AuditLogResponse:
        return cls(**asdict(record))


class AuditLogsResponse(AuthModel):
    items: list[AuditLogResponse]
    total: int
    limit: int
    offset: int


class SecretKeyStatusResponse(AuthModel):
    source: str
    env_locked: bool = Field(alias="envLocked")
    configured: bool
    fingerprint: str
    key_length: int = Field(alias="keyLength")
    old_key_count: int = Field(alias="oldKeyCount")
    path: str
    provider_secret_count: int = Field(default=0, alias="providerSecretCount")


class SecretKeyRotateResponse(SecretKeyStatusResponse):
    rotated_secrets: int = Field(alias="rotatedSecrets")
