"""Model-service domain service: groups, members, usage and gateway authorize.

Design reference: docs/design/model-service-design.md#6-domain-model
"""

from __future__ import annotations

from llm_d_lens.auth.access import owner_for_create
from llm_d_lens.auth.contracts import Principal, ResourceRef
from llm_d_lens.auth.policy import authorize
from llm_d_lens.auth.service import default_service as default_auth_service
from llm_d_lens.core.exceptions import ForbiddenError, NotFoundError
from llm_d_lens.db.dao.model_service_group import ModelServiceGroupDao, ModelServiceGroupDaoError
from llm_d_lens.db.dao.model_service_member import ModelServiceMemberDao, ModelServiceMemberDaoError
from llm_d_lens.db.dao.usage_record import UsageRecordDao
from llm_d_lens.model_service.contracts import (
    AuthorizeRequest,
    AuthorizeResult,
    GroupCreateRequest,
    GroupUpdateRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    ModelAccessToken,
    ModelGrantRequest,
    ModelServiceGroup,
    ModelServiceMember,
    ProvidersStatusRequest,
    PublishableDeployment,
    TrafficUpdateRequest,
    UsageRecord,
    UsageRecordRequest,
    utcnow,
)
from llm_d_lens.model_service.gateway_providers import default_pool_name, qualified_pool_name, split_pool_name
from llm_d_lens.model_service.selection import select_member
from llm_d_lens.model_service.targets import ExecutionTarget, ExecutionTargetError, resolve_execution_target
from llm_d_lens.model_service.tokens import ModelTokenService
from llm_d_lens.model_service.usage_analytics import build_usage_analytics

INFERENCE_PERMISSION = "model-service:inference:use"


class ModelServiceError(Exception):
    """Base class for model-service domain failures."""


class ModelServiceNotFoundError(ModelServiceError):
    """The model name has no authorized service (deliberately merged with 'none')."""


class ModelServiceUnauthorizedError(ModelServiceError):
    """The token is missing, expired, revoked or the user is not active."""


class ModelServiceConflictError(ModelServiceError):
    """A name or membership already exists."""


class ModelService:
    def __init__(
        self,
        *,
        tokens: ModelTokenService | None = None,
        groups: ModelServiceGroupDao | None = None,
        members: ModelServiceMemberDao | None = None,
        usage: UsageRecordDao | None = None,
        auth_service=None,
    ) -> None:
        self.tokens = tokens or ModelTokenService()
        self.groups = groups or ModelServiceGroupDao()
        self.members = members or ModelServiceMemberDao()
        self.usage = usage or UsageRecordDao()
        self._auth = auth_service or default_auth_service()

    # --- groups --------------------------------------------------------------
    def list_groups(self) -> list[ModelServiceGroup]:
        return self.groups.list()

    def get_group(self, group_id: str) -> ModelServiceGroup | None:
        return self.groups.get(group_id)

    def create_group(
        self, request: GroupCreateRequest, *, created_by: str | None, principal: Principal | None = None
    ) -> ModelServiceGroup:
        """Create a service, atomically publishing its ready execution when supplied.

        Combined creation derives the deployment model names server-side and
        requires both management permissions in the deployment's cluster.
        Gateway reconciliation is scheduled by the HTTP caller after commit.
        """
        if principal is not None:
            resource = ResourceRef(
                resource_type="cluster", resource_id=request.cluster_id, cluster_id=request.cluster_id
            )
            if not authorize(principal, "model-service:group:manage", resource=resource).allowed:
                raise ForbiddenError("model-service group management is required in this cluster")
        target = None
        if request.execution_id is not None:
            try:
                target = resolve_execution_target(request.execution_id)
            except ExecutionTargetError as error:
                raise ModelServiceNotFoundError(str(error)) from error
            if target.cluster_id != request.cluster_id:
                raise ModelServiceConflictError("provider must be in the model service's cluster")
            if principal is not None:
                resource = ResourceRef(
                    resource_type="cluster", resource_id=target.cluster_id, cluster_id=target.cluster_id
                )
                for permission in ("model-service:group:manage", "model-service:member:manage"):
                    if not authorize(principal, permission, resource=resource).allowed:
                        raise ForbiddenError(f"permission denied: {permission}")
            if target.status != "ready" or not target.model:
                raise ModelServiceConflictError("provider must be ready and have a deployment model name")
        model_ref = (target.model_ref or target.model) if target else (request.model_ref or request.name)
        group = ModelServiceGroup(
            name=request.name,
            model_ref=model_ref,
            display_name=(request.display_name or request.name),
            description=request.description,
            selection_policy=request.selection_policy,
            cluster_id=request.cluster_id,
            served_name=target.model if target else request.served_name,
            base_model=request.name if target else (request.base_model or model_ref),
            created_by_user_id=created_by,
            owner_user_id=owner_for_create(principal) if principal is not None else created_by,
        )
        member = self._member_for_target(group.id, target, published_by=created_by) if target else None
        try:
            return self.groups.create(group, member=member) if member else self.groups.create(group)
        except Exception as error:  # noqa: BLE001 - map DAO conflict to domain error
            raise ModelServiceConflictError(str(error)) from error

    def update_group(
        self, group_id: str, request: GroupUpdateRequest, *, principal: Principal | None = None
    ) -> ModelServiceGroup:
        """Update a model's configuration, preserving provider status and weights when stopped."""
        group = self.groups.get(group_id)
        if group is None:
            raise ModelServiceNotFoundError(f"group not found: {group_id}")
        if principal is not None:
            cluster_ids = {value for value in (group.cluster_id, request.cluster_id) if value is not None}
            cluster_ids.update(member.cluster_id for member in self.members.list_by_group(group_id))
            for cluster_id in cluster_ids or {""}:
                resource = ResourceRef(resource_type="cluster", resource_id=cluster_id, cluster_id=cluster_id)
                if not authorize(principal, "model-service:group:manage", resource=resource).allowed:
                    raise ForbiddenError("model-service group management is required in this cluster")
        if (
            request.cluster_id is not None
            and request.cluster_id != group.cluster_id
            and (
                self.members.list_by_group(group_id) or self._auth.list_resource_shares("model_service_group", group_id)
            )
        ):
            raise ModelServiceConflictError("remove all providers and access grants before changing a model's cluster")
        for field in (
            "name",
            "model_ref",
            "display_name",
            "description",
            "selection_policy",
            "status",
            "cluster_id",
            "served_name",
            "base_model",
        ):
            value = getattr(request, field)
            if value is not None:
                setattr(group, field, value)
        group.updated_at = utcnow()
        try:
            return self.groups.save(group)
        except Exception as error:  # noqa: BLE001
            raise ModelServiceConflictError(str(error)) from error

    def require_empty_group(self, group_id: str, *, principal: Principal | None = None) -> ModelServiceGroup:
        group = self.groups.get(group_id)
        if group is None:
            raise ModelServiceNotFoundError(f"group not found: {group_id}")
        if principal is not None:
            resource = ResourceRef(resource_type="cluster", resource_id=group.cluster_id, cluster_id=group.cluster_id)
            if not authorize(principal, "model-service:group:manage", resource=resource).allowed:
                raise ForbiddenError("model-service group management is required in this cluster")
        if self.members.list_by_group(group_id):
            raise ModelServiceConflictError("remove every provider from the model before removing it")
        return group

    def delete_group(self, group_id: str, *, principal: Principal | None = None) -> None:
        """Delete an empty model record; callers must first confirm its route cleanup."""
        self.require_empty_group(group_id, principal=principal)
        try:
            self.groups.delete(group_id)
        except ModelServiceGroupDaoError as error:
            raise ModelServiceConflictError(str(error)) from error
        self._auth.revoke_resource_shares(resource_type="model_service_group", resource_ids=[group_id])

    def require_group_access(self, group_id: str, principal: Principal, permission: str) -> ModelServiceGroup:
        group = self.groups.get(group_id)
        if group is None:
            raise NotFoundError("model service not found")
        resource = ResourceRef(
            resource_type="cluster", resource_id=group.cluster_id or "", cluster_id=group.cluster_id or ""
        )
        if principal is None or not authorize(principal, permission, resource=resource).allowed:
            raise ForbiddenError(f"permission denied: {permission}")
        return group

    def list_access(self, group_id: str, *, principal: Principal) -> list[dict]:
        """List inference-only user/group grants, requiring scoped grant management."""
        self.require_group_access(group_id, principal, "model-service:group:share")
        entries = []
        for subject_type, binding in self._auth.list_resource_shares("model_service_group", group_id):
            subject_id = binding.user_id if subject_type == "user" else binding.group_id
            subject = (
                self._auth.user_dao.get(subject_id) if subject_type == "user" else self._auth.group_dao.get(subject_id)
            )
            entries.append(
                {
                    "id": binding.id,
                    "subjectType": subject_type,
                    "subjectId": subject_id,
                    "subjectName": (subject.username if subject_type == "user" else subject.name)
                    if subject
                    else subject_id,
                    "permission": INFERENCE_PERMISSION,
                    "createdAt": binding.created_at.isoformat(),
                }
            )
        return entries

    def grant_access(self, group_id: str, request: ModelGrantRequest, *, principal: Principal) -> dict:
        """Grant model inference to a scoped user/group; never assign caller-selected roles."""
        group = self.require_group_access(group_id, principal, "model-service:group:share")
        role = self._auth.role_dao.get_by_name("end-user")
        if role is None:
            raise ModelServiceConflictError("built-in end-user role is not initialized")
        binding = self._auth.share_resource(
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            role_id=role.id,
            resource_type="model_service_group",
            resource_id=group.id,
            cluster_id=group.cluster_id,
            granted_by=owner_for_create(principal),
        )
        return {
            "id": binding.id,
            "subjectType": request.subject_type,
            "subjectId": request.subject_id,
            "permission": INFERENCE_PERMISSION,
        }

    def revoke_access(self, group_id: str, binding_id: str, *, principal: Principal) -> None:
        """Revoke only a grant belonging to this caller-authorized model."""
        group = self.require_group_access(group_id, principal, "model-service:group:share")
        self._auth.revoke_resource_share(
            binding_id,
            resource_type="model_service_group",
            resource_id=group.id,
            cluster_id=group.cluster_id,
        )

    def access_options(self, group_id: str, *, principal: Principal) -> dict:
        """Minimal recipient directory restricted to inference-capable cluster scope."""
        group = self.require_group_access(group_id, principal, "model-service:group:share")
        return self._auth.model_share_options(group.cluster_id)

    def set_providers_status(
        self, group_id: str, request: ProvidersStatusRequest, *, principal: Principal
    ) -> list[ModelServiceMember]:
        group = self.groups.get(group_id)
        if group is None:
            raise ModelServiceNotFoundError(f"group not found: {group_id}")
        resource = ResourceRef(resource_type="cluster", resource_id=group.cluster_id, cluster_id=group.cluster_id)
        if not authorize(principal, "model-service:member:manage", resource=resource).allowed:
            raise ForbiddenError("model-service member management is required in this cluster")
        members = self.members.list_by_group(group_id)
        if not members:
            raise ModelServiceConflictError("model has no providers")
        if request.status == "active":
            for member in members:
                try:
                    target = resolve_execution_target(member.execution_id)
                except ExecutionTargetError as error:
                    raise ModelServiceConflictError(f"provider cannot go online: {error}") from error
                if target.status != "ready" or target.cluster_id != group.cluster_id:
                    raise ModelServiceConflictError("all providers must be ready in the model's cluster to go online")
        try:
            return self.members.set_group_status(
                group_id, request.status, {member.id: member.status for member in members}
            )
        except ModelServiceMemberDaoError as error:
            raise ModelServiceConflictError(str(error)) from error

    # --- members -------------------------------------------------------------
    def list_members(self, group_id: str | None = None) -> list[ModelServiceMember]:
        return self.members.list_by_group(group_id) if group_id else self.members.list_all()

    def create_member(
        self, request: MemberCreateRequest, *, published_by: str | None, principal: Principal | None = None
    ) -> ModelServiceMember:
        group = self.groups.get(request.group_id)
        if group is None:
            raise ModelServiceNotFoundError(f"group not found: {request.group_id}")
        try:
            target = resolve_execution_target(request.execution_id)
        except ExecutionTargetError as error:
            raise ModelServiceNotFoundError(str(error)) from error
        if target.cluster_id != group.cluster_id:
            raise ModelServiceConflictError(f"provider must be in the model service's cluster ({group.cluster_id})")
        if principal is not None:
            from llm_d_lens.auth.access import require_cluster_access  # noqa: PLC0415

            require_cluster_access(principal, target.cluster_id)
        member = self._member_for_target(
            request.group_id, target, pool_name=request.pool_name, published_by=published_by
        )
        try:
            return self.members.create(member)
        except Exception as error:  # noqa: BLE001
            raise ModelServiceConflictError(str(error)) from error

    @staticmethod
    def _member_for_target(
        group_id: str, target: ExecutionTarget, *, published_by: str | None, pool_name: str | None = None
    ) -> ModelServiceMember:
        return ModelServiceMember(
            group_id=group_id,
            execution_id=target.execution_id,
            cluster_id=target.cluster_id,
            target_namespace=target.namespace,
            target_service=target.service,
            target_port=target.port,
            endpoint_kind=target.endpoint_kind,
            pool_name=pool_name or qualified_pool_name(default_pool_name(target.service), target.namespace),
            epp_ref=target.service if target.endpoint_kind == "llm-d-epp" else None,
            owner_user_id=published_by,
            published_by_user_id=published_by,
        )

    def list_publishable_deployments(self, principal=None) -> list[PublishableDeployment]:
        """Ready executions with a derivable target, filtered to visible clusters."""
        from llm_d_lens.auth.access import visible_cluster_ids  # noqa: PLC0415
        from llm_d_lens.cluster.registry import list_clusters  # noqa: PLC0415
        from llm_d_lens.deploy.application import deployment_run_manager  # noqa: PLC0415

        allowed = visible_cluster_ids(principal)
        cluster_names = {cluster.id: cluster.name for cluster in list_clusters()}
        items: list[PublishableDeployment] = []
        for execution in deployment_run_manager.store.list_executions():
            try:
                target = resolve_execution_target(execution.execution_id)
            except ExecutionTargetError:
                continue
            if allowed is not None and target.cluster_id not in allowed:
                continue
            items.append(
                PublishableDeployment(
                    execution_id=target.execution_id,
                    name=target.name,
                    display_name=target.display_name,
                    model=target.model,
                    cluster_id=target.cluster_id,
                    cluster_name=cluster_names.get(target.cluster_id, ""),
                    namespace=target.namespace,
                    service=target.service,
                    port=target.port,
                    endpoint_kind=target.endpoint_kind,
                    model_ref=target.model_ref,
                    status=target.status,
                )
            )
        return items

    def update_member(
        self, member_id: str, request: MemberUpdateRequest, *, principal: Principal | None = None
    ) -> ModelServiceMember:
        """Take a provider offline or restore a ready deployment without changing its weight."""
        member = self.members.get(member_id)
        if member is None:
            raise ModelServiceNotFoundError(f"member not found: {member_id}")
        if principal is not None:
            resource = ResourceRef(resource_type="cluster", resource_id=member.cluster_id, cluster_id=member.cluster_id)
            if not authorize(principal, "model-service:member:manage", resource=resource).allowed:
                raise ForbiddenError("model-service member management is required in this cluster")
        if member.status == "disabled" and request.status not in (None, "disabled"):
            try:
                target = resolve_execution_target(member.execution_id)
            except ExecutionTargetError as error:
                raise ModelServiceConflictError(f"provider cannot go online: {error}") from error
            if target.status != "ready" or target.cluster_id != member.cluster_id:
                raise ModelServiceConflictError("provider must be ready in its registered cluster to go online")
        for field in ("status",):
            value = getattr(request, field)
            if value is not None:
                setattr(member, field, value)
        member.updated_at = utcnow()
        return self.members.save(member)

    def update_traffic(
        self, group_id: str, request: TrafficUpdateRequest, *, principal: Principal
    ) -> list[ModelServiceMember]:
        """Atomically replace relative weights and add ready, compatible deployment providers.

        Requires member management in the model's cluster. Existing providers must all
        be included; stale snapshots, incompatible deployments and all-zero allocations
        are conflicts. This method persists configuration; the HTTP handler reconciles it.
        """
        group = self.groups.get(group_id)
        if group is None:
            raise ModelServiceNotFoundError(f"group not found: {group_id}")
        resource = ResourceRef(resource_type="cluster", resource_id=group.cluster_id, cluster_id=group.cluster_id)
        if not authorize(principal, "model-service:member:manage", resource=resource).allowed:
            raise ForbiddenError("model-service member management is required in this cluster")
        existing = {member.execution_id: member for member in self.members.list_by_group(group_id)}
        members = []
        pools = set()
        for item in request.members:
            member = existing.get(item.execution_id)
            if member is None or (item.weight > 0 and member.weight == 0):
                try:
                    target = resolve_execution_target(item.execution_id)
                except ExecutionTargetError as error:
                    raise ModelServiceNotFoundError(str(error)) from error
                if target.cluster_id != group.cluster_id or target.status != "ready":
                    raise ModelServiceConflictError("providers must be ready deployments in the model's cluster")
                if target.endpoint_kind != "llm-d-epp":
                    raise ModelServiceConflictError(
                        "weighted routing requires a deployment-owned InferencePool and EPP"
                    )
                if not target.model or target.model != (group.served_name or group.model_ref):
                    raise ModelServiceConflictError(
                        "provider model name must match the model service's provider model name"
                    )
                if member is None:
                    member = self._member_for_target(group_id, target, published_by=principal.user_id)
            if member.endpoint_kind != "llm-d-epp":
                raise ModelServiceConflictError("weighted routing requires deployment-owned InferencePools")
            pool_namespace, pool_name = split_pool_name(member.pool_name)
            key = (
                pool_namespace or member.target_namespace,
                pool_name or default_pool_name(member.epp_ref or member.target_service),
            )
            if key in pools:
                raise ModelServiceConflictError("deployments must reference distinct InferencePools")
            pools.add(key)
            members.append(member.model_copy(update={"weight": item.weight}))
        try:
            return self.members.save_traffic(group_id, members, request.expected_weights)
        except ModelServiceMemberDaoError as error:
            raise ModelServiceConflictError(str(error)) from error

    def delete_member(self, member_id: str, *, principal: Principal | None = None) -> None:
        if principal is not None:
            from llm_d_lens.auth.access import require_cluster_access  # noqa: PLC0415

            member = self.members.get(member_id)
            if member is not None:
                require_cluster_access(principal, member.cluster_id)
        self.members.delete(member_id)

    # --- authorization / routing --------------------------------------------
    def _principal_for_token(self, token: ModelAccessToken):
        user = self._auth.user_dao.get(token.user_id)
        if user is None or user.status != "active":
            raise ModelServiceUnauthorizedError("token owner is not an active user")
        return user, self._auth.principal_for(user)

    def group_authorized(self, group: ModelServiceGroup, principal: Principal) -> bool:
        resource = ResourceRef(
            resource_type="model_service_group",
            resource_id=group.id,
            cluster_id=group.cluster_id or "",
        )
        return principal is not None and authorize(principal, INFERENCE_PERMISSION, resource=resource).allowed

    def authorized_members(self, group: ModelServiceGroup, principal) -> list[ModelServiceMember]:
        if not self.group_authorized(group, principal):
            return []
        published = self.members.list_by_group(group.id)
        return [
            member
            for member in published
            if member.cluster_id == group.cluster_id and member.status == "active" and member.weight > 0
        ]

    def list_models_for_user(self, user_id: str) -> list[ModelServiceGroup]:
        """Active groups with a healthy positive-weight provider the user may call.

        Consumers require inference cluster scope and an explicit model grant.
        Operators retain non-self-scoped inference within their binding's scope.
        """
        user = self._auth.user_dao.get(user_id)
        if user is None or user.status != "active":
            return []
        principal = self._auth.principal_for(user)
        visible: list[ModelServiceGroup] = []
        for group in self.groups.list():
            if group.status != "active":
                continue
            if self.authorized_members(group, principal):
                visible.append(group)
        return visible

    def list_model_entries_for_user(self, user_id: str) -> list[dict]:
        """One model entry per model service the caller may call.

        A model service is scoped to a single cluster, and its providers live in
        that cluster, so the caller sees it once with its own cluster's connection.
        Each entry carries ``clusterName`` (same enrichment as
        ``list_publishable_deployments``) so callers choosing between several
        model services -- often sharing the same public name across clusters --
        can tell them apart without a separate cluster lookup.
        """
        from llm_d_lens.cluster.registry import list_clusters  # noqa: PLC0415

        user = self._auth.user_dao.get(user_id)
        if user is None or user.status != "active":
            return []
        principal = self._auth.principal_for(user)
        cluster_names = {cluster.id: cluster.name for cluster in list_clusters()}
        entries: list[dict] = []
        for group in self.groups.list():
            if group.status != "active":
                continue
            if not self.authorized_members(group, principal):
                continue
            payload = group.api_payload()
            payload["clusterName"] = cluster_names.get(group.cluster_id, "")
            entries.append(payload)
        return entries

    def authorize_request(self, request: AuthorizeRequest, *, cluster_id: str | None = None) -> AuthorizeResult:
        token = self.tokens.validate(request.token, ip=request.client_ip)
        if token is None:
            raise ModelServiceUnauthorizedError("token is missing, expired or revoked")
        user, principal = self._principal_for_token(token)
        # A model service is scoped to one cluster, so the request's cluster
        # selects the group. Decision D2: never reveal that a model exists but is
        # forbidden.
        groups = [
            group
            for group in self.groups.list()
            if group.name == request.model
            and group.status == "active"
            and (cluster_id is None or group.cluster_id == cluster_id)
        ]
        group = None
        candidates: list = []
        for candidate in groups:
            accessible = self.authorized_members(candidate, principal)
            if accessible:
                group, candidates = candidate, accessible
                break
        if group is None:
            raise ModelServiceNotFoundError(f"no model service for '{request.model}'")
        selected = select_member(candidates, group.selection_policy, user_id=user.id, group_id=group.id)
        return AuthorizeResult(
            user_id=user.id,
            token_id=token.id,
            group_id=group.id,
            group_name=group.name,
            model_ref=group.model_ref,
            cluster_id=selected.cluster_id,
            execution_id=selected.execution_id,
            target_namespace=selected.target_namespace,
            target_service=selected.target_service,
            target_port=selected.target_port,
            endpoint_kind=selected.endpoint_kind,
        )

    # --- usage ---------------------------------------------------------------
    def usage_analytics(
        self,
        *,
        since=None,
        until=None,
        interval: str = "day",
        group_by: str = "model",
        user_ids: list[str] | None = None,
        group_ids: list[str] | None = None,
        cluster_ids: list[str] | None = None,
        principal: Principal | None = None,
    ) -> dict:
        """Aggregated usage for the admin dashboard (server-side, uncapped by page size).

        ``principal`` restricts everything (data, and the cluster/user/model
        dropdown facets) to the caller's reachable clusters -- a cluster-scoped
        maintainer never sees another cluster's usage. ``cluster_ids`` narrows
        further to the clusters selected in the UI; when set, the user/model
        facets cascade to only what's available on those clusters, while the
        cluster dropdown itself always lists every cluster the caller can reach.
        """
        from llm_d_lens.auth.access import effective_cluster_filter  # noqa: PLC0415

        reachable_cluster_ids = effective_cluster_filter(principal, cluster_ids)
        records = self.usage.list_range(
            since=since,
            until=until,
            user_ids=user_ids,
            group_ids=group_ids,
            cluster_ids=reachable_cluster_ids,
        )
        # Cascading facets: users/models are scoped to the selected (or all
        # reachable) clusters; the cluster facet itself always lists every
        # reachable cluster, regardless of the current cluster selection.
        facets = self.usage.list_range(since=since, until=until, cluster_ids=reachable_cluster_ids)
        cluster_facets = (
            facets
            if not cluster_ids
            else self.usage.list_range(since=since, until=until, cluster_ids=effective_cluster_filter(principal, None))
        )
        user_labels: dict[str, str] = {}
        cluster_labels: dict[str, str] = {}
        for record in [*records, *facets, *cluster_facets]:
            if record.user_id and record.user_id not in user_labels:
                user = self._auth.user_dao.get(record.user_id)
                user_labels[record.user_id] = (user.username if user is not None else "") or record.user_id
        if any(record.cluster_id for record in [*records, *facets, *cluster_facets]):
            from llm_d_lens.cluster.registry import list_clusters  # noqa: PLC0415

            cluster_labels = {cluster.id: (cluster.name or cluster.id) for cluster in list_clusters()}
        return build_usage_analytics(
            records,
            facets,
            interval=interval,
            group_by=group_by,
            user_labels=user_labels,
            cluster_labels=cluster_labels,
            cluster_facets=cluster_facets,
        )

    def record_usage(self, request: UsageRecordRequest) -> UsageRecord:
        existing = self.usage.get_by_request_id(request.request_id)
        if existing is not None:
            return existing
        user_id = None
        if request.token_id is not None:
            token = self.tokens.get(request.token_id)
            user_id = token.user_id if token is not None else None
        record = UsageRecord(
            request_id=request.request_id,
            user_id=user_id,
            token_id=request.token_id,
            group_id=request.group_id,
            group_name=request.group_name,
            cluster_id=request.cluster_id,
            execution_id=request.execution_id,
            model_ref=request.model_ref,
            provider=request.provider,
            input_tokens=request.input_tokens,
            cached_input_tokens=request.cached_input_tokens,
            cache_write_tokens=request.cache_write_tokens,
            output_tokens=request.output_tokens,
            requests=request.requests,
            usage_source=request.usage_source,
            status=request.status,
            error_code=request.error_code,
            streaming=request.streaming,
            ttft_ms=request.ttft_ms,
            duration_ms=request.duration_ms,
            client_ip=request.client_ip,
            user_agent=request.user_agent,
        )
        return self.usage.create(record)


_default_service: ModelService | None = None


def default_service() -> ModelService:
    global _default_service
    if _default_service is None:
        _default_service = ModelService()
    return _default_service
