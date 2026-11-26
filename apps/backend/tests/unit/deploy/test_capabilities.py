from llm_d_lens.deploy.capabilities import (
    deployment_capabilities,
    provider_capability,
    provider_supports,
)


def test_capability_lookup_returns_copies() -> None:
    capability = provider_capability("precise-prefix-cache-routing")
    assert capability is not None
    assert capability["supports_kubernetes_service_baseline"] is True
    capability["supports_kubernetes_service_baseline"] = False
    assert provider_supports("precise-prefix-cache-routing", "supports_kubernetes_service_baseline")


def test_unknown_provider_and_capability_are_not_supported() -> None:
    assert provider_capability("unknown") is None
    assert not provider_supports("unknown", "supports_kubernetes_service_baseline")
    assert not provider_supports("optimized-baseline", "unknown_capability")
    assert provider_supports("optimized-baseline", "supports_kubernetes_service_baseline")


def test_deployment_capabilities_expose_provider_ids() -> None:
    capabilities = deployment_capabilities()
    assert len({item["id"] for item in capabilities}) == len(capabilities)
    assert all(item["supported"] is True for item in capabilities)


def test_evaluation_defaults_are_declarative_and_copy_safe() -> None:
    capability = provider_capability("tiered-prefix-cache")
    assert capability is not None
    assert "default_variant" not in capability["evaluation"]
    assert capability["variants"] == []
    assert "unavailable_variants" not in capability["evaluation"]
    capability["evaluation"]["required_baselines"].append("mutated")
    assert provider_capability("tiered-prefix-cache")["evaluation"]["required_baselines"] == ["direct-vllm"]


def test_three_additional_guides_expose_complete_evaluation_plans() -> None:
    expected = {
        "optimized-baseline": (["kubernetes-service", "load-only", "affinity-only"], "shared-prefix"),
        "precise-prefix-cache-routing": (["kubernetes-service", "optimized-baseline"], "shared-prefix"),
        "pd-disaggregation": (["direct-vllm"], "matrix"),
    }
    for provider, (baselines, workload_kind) in expected.items():
        evaluation = provider_capability(provider)["evaluation"]
        assert evaluation["required_baselines"] == baselines
        assert evaluation["recommended_workload"]["kind"] == workload_kind
        assert evaluation["default_goal"] == ("full-evaluation" if provider == "pd-disaggregation" else "quick-check")
        assert evaluation["goals"]
        assert evaluation["comparison_arms"]
        assert evaluation["evidence"]
        assert evaluation["validation"]


def test_routing_workload_instances_do_not_share_nested_mutable_state():
    from llm_d_lens.deploy.capabilities import shared_prefix_routing_workload

    first, second = shared_prefix_routing_workload(), shared_prefix_routing_workload()
    first["stages"][0]["rate"] = -1
    assert second["stages"][0]["rate"] == 0.4
    optimized = provider_capability("optimized-baseline")["evaluation"]
    precise = provider_capability("precise-prefix-cache-routing")["evaluation"]
    assert optimized["recommended_workload"]["shared_prefix"] is not precise["recommended_workload"]["shared_prefix"]
    assert (
        optimized["recommended_workload"]["shared_prefix"]
        is not optimized["goals"][0]["scenarios"][0]["benchmark"]["shared_prefix"]
    )


def test_routing_workload_defaults_are_lightweight_with_explicit_full_profile():
    from llm_d_lens.deploy.capabilities import shared_prefix_routing_workload

    quick = shared_prefix_routing_workload()
    full = shared_prefix_routing_workload(full=True)
    assert sum(int(stage["rate"] * stage["duration"]) for stage in quick["stages"]) == 24
    assert quick["system_prompt_len"] + quick["question_len"] == 640
    assert quick["output_len"] == 64
    assert sum(int(stage["rate"] * stage["duration"]) for stage in full["stages"]) == 780
    assert full["output_len"] == 1000


def _jwt_with_sa(namespace: str, name: str) -> str:
    import base64
    import json

    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode("ascii")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": f"system:serviceaccount:{namespace}:{name}"}).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"{header}.{payload}.sig"


class _FakeBootstrapRunner:
    def __init__(
        self,
        *,
        username: str = "system:serviceaccount:lens-edit:lens-user",
        whoami_username: str | None = None,
        unauthorized: bool = False,
        namespace_exists: bool = False,
        namespace_forbidden: bool = False,
        sa_accessible: bool = False,
        marker_doc: dict | None = None,
        allowed_fn=None,
    ) -> None:
        self.username = username
        self.whoami_username = whoami_username
        self.unauthorized = unauthorized
        self.namespace_exists = namespace_exists
        self.namespace_forbidden = namespace_forbidden
        self.sa_accessible = sa_accessible
        self.marker_doc = marker_doc
        self.allowed_fn = allowed_fn or (lambda _attrs: True)
        self.calls: list[list[str]] = []

    async def run(self, argv, *, input=None, timeout=15):  # noqa: A002
        import json

        from llm_d_lens.infrastructure.process import CommandResult

        self.calls.append(list(argv))
        if self.unauthorized:
            return CommandResult(
                argv=tuple(argv),
                returncode=1,
                stdout="",
                stderr="error: You must be logged in to the server (Unauthorized)",
            )
        if argv[:4] == ["kubectl", "create", "-o", "json"] and input:
            body = json.loads(input)
            if body.get("kind") == "SelfSubjectReview":
                if not self.username:
                    return CommandResult(argv=tuple(argv), returncode=1, stdout="", stderr="forbidden")
                return CommandResult(
                    argv=tuple(argv),
                    returncode=0,
                    stdout=json.dumps({"status": {"userInfo": {"username": self.username}}}),
                    stderr="",
                )
            if body.get("kind") == "List":
                items = []
                for item in body["items"]:
                    attrs = item["spec"]["resourceAttributes"]
                    items.append({**item, "status": {"allowed": bool(self.allowed_fn(attrs))}})
                return CommandResult(
                    argv=tuple(argv), returncode=0, stdout=json.dumps({"kind": "List", "items": items}), stderr=""
                )
        if argv[:3] == ["kubectl", "auth", "whoami"]:
            if not self.whoami_username:
                return CommandResult(argv=tuple(argv), returncode=1, stdout="", stderr="forbidden")
            return CommandResult(
                argv=tuple(argv),
                returncode=0,
                stdout=json.dumps({"status": {"userInfo": {"username": self.whoami_username}}}),
                stderr="",
            )
        if argv[:3] == ["kubectl", "get", "namespace"]:
            if self.namespace_forbidden:
                return CommandResult(argv=tuple(argv), returncode=1, stdout="", stderr="namespaces is forbidden")
            if self.namespace_exists:
                return CommandResult(
                    argv=tuple(argv), returncode=0, stdout=json.dumps({"metadata": {"name": argv[3]}}), stderr=""
                )
            return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="")
        if argv[:3] == ["kubectl", "get", "serviceaccounts"]:
            if self.sa_accessible:
                return CommandResult(argv=tuple(argv), returncode=0, stdout="serviceaccount/default\n", stderr="")
            return CommandResult(argv=tuple(argv), returncode=1, stdout="", stderr="serviceaccounts is forbidden")
        if argv[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
            if self.marker_doc is None:
                return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="")
            return CommandResult(argv=tuple(argv), returncode=0, stdout=json.dumps(self.marker_doc), stderr="")
        return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="")


def test_ssar_preflight_admin_sufficient_and_non_admin_denied(monkeypatch):
    import asyncio

    import llm_d_lens.deploy.capabilities as caps
    from llm_d_lens.cluster import egress as cluster_egress
    from llm_d_lens.cluster import registry as cluster_registry

    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    monkeypatch.setattr(cluster_egress, "validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster",
        lambda _cid: type(
            "C",
            (),
            {
                "hf_token_secret_namespace": "lens-edit",
                "hf_token_secret_name": "hf-secret",
                "gateway_namespace": "lens-gateway",
            },
        )(),
    )

    req = caps.DeploymentPermissionCheckRequest(
        cluster_id="cluster-a",
        namespace="llmd-qwen-demo",
        uses_storage=True,
        uses_lws=True,
        uses_router=True,
    )

    admin_runner = _FakeBootstrapRunner()
    admin_report = asyncio.run(caps.check_deployment_permissions(req, runner=admin_runner))
    assert admin_report["status"] == "sufficient"
    assert admin_report["sufficient"] is True
    assert admin_report["adminOperations"] == []

    # Non-Admin denied namespace creation, ns RBAC, and ClusterRoleBinding
    non_admin_runner = _FakeBootstrapRunner(
        allowed_fn=lambda attrs: attrs.get("namespace") == "lens-edit",
    )
    denied_report = asyncio.run(caps.check_deployment_permissions(req, runner=non_admin_runner))
    assert denied_report["status"] == "admin_required"
    assert denied_report["sufficient"] is False
    op_ids = [op["id"] for op in denied_report["adminOperations"]]
    assert op_ids == ["deploy.namespace", "deploy.rbac", "deploy.epp_auth_delegator"]
    missing_resources = {(m["apiGroup"], m["resource"]) for m in denied_report["missing"]}
    assert ("", "persistentvolumeclaims") in missing_resources
    assert ("leaderworkerset.x-k8s.io", "leaderworkersets") in missing_resources
    assert ("inference.networking.k8s.io", "inferencepools") in missing_resources
    assert ("llm-d.ai", "inferencemodelrewrites") in missing_resources
    assert ("gateway.networking.k8s.io", "referencegrants") in missing_resources
    assert ("monitoring.coreos.com", "servicemonitors") in missing_resources
    assert ("monitoring.coreos.com", "podmonitors") in missing_resources


def test_ssar_preflight_surfaces_401_immediately_and_validates_inputs(monkeypatch):
    import asyncio

    import pytest

    import llm_d_lens.deploy.capabilities as caps
    from llm_d_lens.cluster import egress as cluster_egress
    from llm_d_lens.cluster import registry as cluster_registry

    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    monkeypatch.setattr(cluster_egress, "validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: None)

    req = caps.DeploymentPermissionCheckRequest(cluster_id="cluster-a", namespace="llmd-demo")
    with pytest.raises(caps.DeploymentClusterAuthError):
        asyncio.run(caps.check_deployment_permissions(req, runner=_FakeBootstrapRunner(unauthorized=True)))

    for bad_ns in (
        "kube-system",
        "lens-edit",
        "llm-d-monitoring",
        "llm-d-bench-storage",
        "other-ns",
        "llmd-",
        "INVALID_NS",
    ):
        with pytest.raises(caps.DeploymentCapabilityValidationError):
            caps.validate_deployment_namespace(bad_ns, cluster_id="cluster-a")

    with pytest.raises(caps.DeploymentCapabilityValidationError):
        caps.validate_subject_override({"kind": "ServiceAccount", "name": "sa", "namespace": "bad\nns"})


def test_identity_detection_fallback_and_bootstrap_script_generation(monkeypatch):
    import asyncio

    import pytest

    import llm_d_lens.deploy.capabilities as caps
    from llm_d_lens.cluster import egress as cluster_egress
    from llm_d_lens.cluster import registry as cluster_registry

    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    monkeypatch.setattr(cluster_egress, "validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: None)
    kubeconfig = (
        "apiVersion: v1\nkind: Config\ncurrent-context: ctx\n"
        "contexts:\n- name: ctx\n  context:\n    user: edit-sa\n"
        f"users:\n- name: edit-sa\n  user:\n    token: {_jwt_with_sa('lens-edit', 'edit-user')}\n"
    )
    monkeypatch.setattr(cluster_registry, "read_kubeconfig", lambda _cid: kubeconfig)

    runner = _FakeBootstrapRunner(username="", whoami_username="")
    manifest = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: custom-epp\n"
        "spec:\n  template:\n    spec:\n      serviceAccountName: custom-epp-sa\n"
        "      containers:\n      - name: epp\n        image: ghcr.io/llm-d/epp:v1\n"
    )
    req = caps.DeploymentPermissionCheckRequest(
        cluster_id="cluster-a",
        namespace="llmd-qwen-1",
        manifest=manifest,
        namespace_labels={"team": "ml-infra"},
        agentic_deployment_run_id="agentic-1",
    )
    remediation = asyncio.run(caps.generate_deployment_bootstrap_script(req, runner=runner))
    script = remediation["script"]
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script
    assert "umask 077" in script
    assert "<<'LENS_NS_EOF'" in script
    assert "<<'LENS_RBAC_EOF'" in script
    assert "<<'LENS_EPP_EOF'" in script
    assert "team: ml-infra" in script
    assert "name: custom-epp-sa" in script
    assert "llm-d-prism-epp-auth-delegator-llmd-qwen-1" in script
    assert "HF_TOKEN" not in script
    assert "kind: Secret" not in script

    # Unknown identity raises subject_required
    monkeypatch.setattr(cluster_registry, "read_kubeconfig", lambda _cid: None)
    with pytest.raises(caps.DeploymentCapabilityValidationError) as exc:
        asyncio.run(caps.generate_deployment_bootstrap_script(req, runner=runner))
    assert exc.value.code == "subject_required"


def test_verify_bootstrap_marker_and_namespace_binding_and_runner_allowlist(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    import pytest

    import llm_d_lens.deploy.capabilities as caps
    from llm_d_lens.cluster import egress as cluster_egress
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.deploy.runtime.composition import (
        RegisteredGuideCommandRunner,
        RestrictedKubectlRunner,
        RuntimeConfigurationError,
        build_namespace_factory,
    )

    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    monkeypatch.setattr(cluster_egress, "validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: None)
    caps.clear_verified_namespaces()

    marker_doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": caps.BOOTSTRAP_MARKER_NAME, "namespace": "llmd-verified-ns"},
        "data": {
            "clusterId": "cluster-a",
            "namespace": "llmd-verified-ns",
            "bootstrapDigest": "a" * 64,
            "eppAuthDelegator": "applied",
            "eppServiceAccounts": "optimized-baseline-epp",
            "status": "completed",
        },
    }
    runner = _FakeBootstrapRunner(
        namespace_exists=True,
        marker_doc=marker_doc,
        allowed_fn=lambda attrs: attrs.get("namespace") == "llmd-verified-ns",
    )
    verify_req = caps.DeploymentBootstrapVerifyRequest(
        cluster_id="cluster-a",
        namespace="llmd-verified-ns",
        provider_ref="optimized-baseline",
        expected_digest="a" * 64,
        agentic_deployment_run_id="agentic-run-42",
    )
    res = asyncio.run(caps.verify_deployment_bootstrap(verify_req, runner=runner))
    assert res["verified"] is True
    assert res["status"] == "verified"
    assert (
        caps.consume_verified_namespace("cluster-a", agentic_deployment_run_id="agentic-run-42") == "llmd-verified-ns"
    )
    assert caps.consume_verified_namespace("cluster-a", agentic_deployment_run_id="agentic-run-42") is None

    # Stale digest fails verification
    stale_req = caps.DeploymentBootstrapVerifyRequest(
        cluster_id="cluster-a",
        namespace="llmd-verified-ns",
        expected_digest="b" * 64,
    )
    stale_res = asyncio.run(caps.verify_deployment_bootstrap(stale_req, runner=runner))
    assert stale_res["verified"] is False
    assert stale_res["status"] == "stale"

    # Runner allowlist + build_namespace_factory exact validation
    restricted = RestrictedKubectlRunner(tmp_path / "kubectl", "llmd-")
    restricted._validate(["kubectl", "get", "serviceaccounts", "--namespace", "llmd-verified-ns", "-o", "name"])
    restricted._validate(
        [
            "kubectl",
            "get",
            "configmap",
            "lens-deployment-bootstrap",
            "--namespace",
            "llmd-verified-ns",
            "--ignore-not-found=true",
            "-o",
            "json",
        ]
    )
    guide_runner = RegisteredGuideCommandRunner(
        tmp_path / "kubectl",
        tmp_path / "helm",
        "llmd-",
        SimpleNamespace(
            readiness_deployment_name="modelserver",
            router_release_name="r",
            router_chart="c",
            router_chart_version="v1",
            router_base_values_path=tmp_path,
            router_values_path=tmp_path,
            neutral_router_values_path=tmp_path,
        ),
        60,
        tmp_path,
    )
    guide_runner._validate_kustomize(
        ["kubectl", "get", "serviceaccounts", "--namespace", "llmd-verified-ns", "-o", "name"]
    )
    guide_runner._validate_kustomize(
        [
            "kubectl",
            "get",
            "configmap",
            "lens-deployment-bootstrap",
            "--namespace",
            "llmd-verified-ns",
            "--ignore-not-found=true",
            "-o",
            "json",
        ]
    )

    factory = build_namespace_factory("llmd-")
    req_exact = SimpleNamespace(
        provenance={"guide_id": "optimized-baseline"},
        deployment_policy=SimpleNamespace(value={"namespace_policy": {"exact": "llmd-verified-ns"}}),
        configuration_artifacts=[SimpleNamespace(content="{}")],
    )
    assert factory(req_exact) == "llmd-verified-ns"

    req_bad = SimpleNamespace(
        provenance={"guide_id": "optimized-baseline"},
        deployment_policy=SimpleNamespace(value={"namespace_policy": {"exact": "kube-system"}}),
        configuration_artifacts=[SimpleNamespace(content="{}")],
    )
    with pytest.raises(RuntimeConfigurationError):
        factory(req_bad)


def test_capability_security_hardening_isolation_and_redaction(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    import pytest

    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.deploy import capabilities as caps
    from llm_d_lens.deploy import executions as deploy_executions
    from llm_d_lens.deploy.contracts import DeploymentStatus
    from llm_d_lens.infrastructure.process import CommandResult

    caps.clear_verified_namespaces()

    # redact_kubectl_error strips bearer tokens, key/value secrets, --kubeconfig flag and .kubeconfig paths
    redacted = caps.redact_kubectl_error(
        "error --kubeconfig=/tmp/secret.kubeconfig /var/run/cluster.kubeconfig "
        "Authorization: Bearer abc.def.ghi token=tok123 password=pw1"
    )
    assert "/tmp/secret.kubeconfig" not in redacted
    assert "/var/run/cluster.kubeconfig" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "tok123" not in redacted
    assert "pw1" not in redacted

    # Newline injection rejected in cluster_id & identity re-validated in direct render call
    with pytest.raises(caps.DeploymentCapabilityValidationError):
        caps.render_deployment_bootstrap_script(
            cluster_id="cluster-a\nrm -rf /",
            namespace="llmd-safe-ns",
            identity={"kind": "User", "name": "alice", "subject": "alice", "overrideRequired": False},
        )
    with pytest.raises(
        caps.DeploymentCapabilityValidationError, match="unsupported characters|valid Kubernetes subject"
    ):
        caps.render_deployment_bootstrap_script(
            cluster_id="cluster-a",
            namespace="llmd-safe-ns",
            identity={"kind": "User", "name": 'alice"; id #', "subject": 'alice"; id #', "overrideRequired": False},
        )

    # Overlong / invalid prepared_request_id rejected cleanly instead of truncated
    with pytest.raises(caps.DeploymentCapabilityValidationError) as bad_req:
        caps.render_deployment_bootstrap_script(
            cluster_id="cluster-a",
            namespace="llmd-safe-ns",
            identity={"kind": "User", "name": "alice", "subject": "alice", "overrideRequired": False},
            prepared_request_id="req-" + ("x" * 70),
        )
    assert bad_req.value.code == "invalid_labels"

    rendered = caps.render_deployment_bootstrap_script(
        cluster_id="cluster-a",
        namespace="llmd-safe-ns",
        identity={"kind": "User", "name": "alice", "subject": "alice", "overrideRequired": False},
        prepared_request_id="req-123",
    )
    assert "set -f\n" in rendered["script"]

    # Cross-user isolation on _VERIFIED_NAMESPACES
    caps.record_verified_namespace(
        "cluster-a",
        "llmd-user-ns",
        owner_user_id="alice",
        agentic_deployment_run_id="run-shared",
    )
    assert (
        caps.consume_verified_namespace("cluster-a", owner_user_id="bob", agentic_deployment_run_id="run-shared")
        is None
    )
    assert (
        caps.consume_verified_namespace("cluster-a", owner_user_id=None, agentic_deployment_run_id="run-shared") is None
    )
    assert (
        caps.consume_verified_namespace("cluster-a", owner_user_id="alice", agentic_deployment_run_id="run-shared")
        == "llmd-user-ns"
    )

    # cluster_kubeconfig_namespace is treated as protected
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: None)
    monkeypatch.setattr(cluster_registry, "cluster_kubeconfig_namespace", lambda _cid: "llmd-kubeconfig-home")
    with pytest.raises(caps.DeploymentCapabilityValidationError) as prot_err:
        caps.validate_deployment_namespace("llmd-kubeconfig-home", cluster_id="cluster-a")
    assert prot_err.value.code == "protected_namespace"

    # Mismatched marker namespace becomes stale
    class _StaleMarkerRunner:
        async def run(self, argv, *, input=None, timeout=15):  # noqa: A002
            return CommandResult(
                tuple(argv),
                0,
                '{"data":{"status":"completed","clusterId":"cluster-a","namespace":"llmd-other"}}',
                "",
            )

    stale_marker = asyncio.run(caps.read_bootstrap_marker("cluster-a", "llmd-target", runner=_StaleMarkerRunner()))
    assert stale_marker["status"] == "stale"
    assert stale_marker["present"] is False
    assert stale_marker["verified"] is False

    # Active execution ownership rejects check_deployment_permissions with 409 namespace_in_use
    active_exec = SimpleNamespace(
        execution_id="exec-active",
        namespace="llmd-busy-ns",
        status=DeploymentStatus.READY,
    )
    monkeypatch.setattr(deploy_executions, "_store", SimpleNamespace(list_executions=lambda: [active_exec]))
    monkeypatch.setattr(cluster_registry, "cluster_kubeconfig_namespace", lambda _cid: None)
    with pytest.raises(caps.DeploymentCapabilityValidationError) as busy_err:
        asyncio.run(
            caps.check_deployment_permissions(
                caps.DeploymentPermissionCheckRequest(cluster_id="cluster-a", namespace="llmd-busy-ns"),
                runner=_StaleMarkerRunner(),
            )
        )
    assert busy_err.value.code == "namespace_in_use"
    assert busy_err.value.status_code == 409
    assert "lens-edit" in caps.PROTECTED_NAMESPACES


def test_source_composition_deploy_handles_already_exists_plus_403_with_bootstrap_marker(tmp_path):
    import asyncio
    import json

    import pytest
    import yaml

    from llm_d_lens.deploy.providers import configuration_manifest as cm_module

    manifest_file = tmp_path / "manifest.yaml"
    manifest_docs = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "epp-sa", "namespace": "llmd-sc"}},
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "epp", "namespace": "llmd-sc"},
            "spec": {"template": {"spec": {"serviceAccountName": "epp-sa", "containers": [{"name": "epp"}]}}},
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "llmd-sc-router-auth-0123456789ab"},
            "rules": [{"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "llmd-sc-router-auth-0123456789ab"},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "llmd-sc-router-auth-0123456789ab",
            },
            "subjects": [{"kind": "ServiceAccount", "name": "epp-sa", "namespace": "llmd-sc"}],
        },
    ]
    manifest_file.write_text(yaml.safe_dump_all(manifest_docs), encoding="utf-8")

    artifact = cm_module.GuideDeploymentArtifact(
        guide_id="source-composition",
        artifact_hash="hash",
        manifest_ref=str(manifest_file),
        manifest_checksum="sha256:abc",
        source_ref="a" * 40,
        deployment_contract={
            "readinessDeployments": ["epp"],
            "endpoint": {"protocol": "http", "serviceName": "epp", "port": 80},
            "namespaceLabels": {"example.test/team": "ai"},
            "routerRbac": [{"kind": "ClusterRole", "name": "llmd-sc-router-auth-0123456789ab"}],
        },
    )

    # Case 1: create namespace returns AlreadyExists, get namespace returns 403 Forbidden, marker has eppAuthDelegator=applied
    commands_case1 = []

    async def runner_already_exists_then_403(cmd):
        commands_case1.append(cmd)
        if cmd[:3] == ["kubectl", "create", "namespace"]:
            return 1, "", 'Error from server (AlreadyExists): namespaces "llmd-sc" already exists'
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            return 1, "", 'Error from server (Forbidden): namespaces "llmd-sc" is forbidden'
        if cmd[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
            return 0, json.dumps({"data": {"status": "completed", "eppAuthDelegator": "applied"}}), ""
        if cmd[:2] == ["kubectl", "apply"]:
            with open(cmd[-1], encoding="utf-8") as handle:
                applied = list(yaml.safe_load_all(handle.read()))
            assert all(doc["kind"] not in {"ClusterRole", "ClusterRoleBinding", "Namespace"} for doc in applied)
            return 0, "applied", ""
        return 1, "", f"unexpected command: {cmd}"

    adapter1 = cm_module.SourceCompositionAdapter(
        runner_already_exists_then_403, "llmd-", 30, output_root=tmp_path / "out1"
    )
    res1 = asyncio.run(adapter1.deploy(artifact, {"namespace": "llmd-sc", "provenance": {"cluster_server_id": "c1"}}))
    assert res1["namespace"] == "llmd-sc"
    assert not any(cmd[:3] == ["kubectl", "create", "--namespace"] for cmd in commands_case1)

    # Case 2: create namespace returns AlreadyExists, get namespace succeeds (view role), get clusterrole returns 403 Forbidden
    async def runner_view_ns_then_403_rbac(cmd):
        if cmd[:3] == ["kubectl", "create", "namespace"]:
            return 1, "", 'Error from server (AlreadyExists): namespaces "llmd-sc" already exists'
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            return 0, json.dumps({"metadata": {"name": "llmd-sc", "uid": "ns-uid-1"}}), ""
        if cmd[:2] == ["kubectl", "get"] and cmd[2].startswith("clusterrole/"):
            return 1, "", "Error from server (Forbidden): clusterroles.rbac.authorization.k8s.io is forbidden"
        if cmd[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
            return 0, json.dumps({"data": {"status": "completed", "eppAuthDelegator": "applied"}}), ""
        if cmd[:2] == ["kubectl", "apply"]:
            return 0, "applied", ""
        return 1, "", f"unexpected command: {cmd}"

    adapter2 = cm_module.SourceCompositionAdapter(
        runner_view_ns_then_403_rbac, "llmd-", 30, output_root=tmp_path / "out2"
    )
    res2 = asyncio.run(adapter2.deploy(artifact, {"namespace": "llmd-sc", "provenance": {"cluster_server_id": "c1"}}))
    assert res2["namespace"] == "llmd-sc"

    # Case 3: create namespace returns AlreadyExists, get namespace returns 403, marker missing -> raises Orchestration administrator error
    async def runner_missing_marker(cmd):
        if cmd[:3] == ["kubectl", "create", "namespace"]:
            return 1, "", 'Error from server (AlreadyExists): namespaces "llmd-sc" already exists'
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            return 1, "", 'Error from server (Forbidden): namespaces "llmd-sc" is forbidden'
        if cmd[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
            return 0, "", ""
        return 1, "", "unexpected"

    adapter3 = cm_module.SourceCompositionAdapter(runner_missing_marker, "llmd-", 30, output_root=tmp_path / "out3")
    with pytest.raises(RuntimeError, match="Orchestration administrator"):
        asyncio.run(adapter3.deploy(artifact, {"namespace": "llmd-sc", "provenance": {"cluster_server_id": "c1"}}))

    # Case 4: marker with mismatched clusterId or namespace is rejected
    async def runner_mismatched_cluster_marker(cmd):
        if cmd[:3] == ["kubectl", "create", "namespace"]:
            return 1, "", 'Error from server (AlreadyExists): namespaces "llmd-sc" already exists'
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            return 1, "", 'Error from server (Forbidden): namespaces "llmd-sc" is forbidden'
        if cmd[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
            return (
                0,
                json.dumps(
                    {
                        "data": {
                            "status": "completed",
                            "eppAuthDelegator": "applied",
                            "clusterId": "foreign-cluster",
                            "namespace": "llmd-sc",
                        }
                    }
                ),
                "",
            )
        return 1, "", "unexpected"

    adapter4 = cm_module.SourceCompositionAdapter(
        runner_mismatched_cluster_marker, "llmd-", 30, output_root=tmp_path / "out4"
    )
    with pytest.raises(RuntimeError, match="Orchestration administrator"):
        asyncio.run(adapter4.deploy(artifact, {"namespace": "llmd-sc", "provenance": {"cluster_server_id": "c1"}}))

    async def runner_mismatched_ns_marker(cmd):
        if cmd[:3] == ["kubectl", "create", "namespace"]:
            return 1, "", 'Error from server (Forbidden): namespaces "llmd-sc" is forbidden'
        if cmd[:3] == ["kubectl", "get", "serviceaccounts"]:
            return 1, "", 'Error from server (Forbidden): serviceaccounts in namespace "llmd-sc" is forbidden'
        if cmd[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
            return (
                0,
                json.dumps(
                    {
                        "data": {
                            "status": "completed",
                            "eppAuthDelegator": "applied",
                            "clusterId": "c1",
                            "namespace": "llmd-other",
                        }
                    }
                ),
                "",
            )
        return 1, "", "unexpected"

    adapter5 = cm_module.SourceCompositionAdapter(
        runner_mismatched_ns_marker, "llmd-", 30, output_root=tmp_path / "out5"
    )
    with pytest.raises(RuntimeError, match="Orchestration administrator"):
        asyncio.run(adapter5.deploy(artifact, {"namespace": "llmd-sc", "provenance": {"cluster_server_id": "c1"}}))


def test_protected_namespaces_includes_cluster_storage_and_volume_namespaces(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.deploy import capabilities as caps
    from llm_d_lens.storage import store as storage_store

    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llm-d-bench-")
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: None)
    monkeypatch.setattr(cluster_registry, "cluster_kubeconfig_namespace", lambda _cid: None)

    fake_volumes = [
        SimpleNamespace(
            cluster_id="cluster-a",
            local_disk=SimpleNamespace(namespace="llm-d-bench-shared-cache"),
            nfs=None,
            dynamic_pvc=None,
        ),
        SimpleNamespace(
            cluster_id="cluster-b",
            local_disk=SimpleNamespace(namespace="llm-d-bench-other-cluster-cache"),
            nfs=None,
            dynamic_pvc=None,
        ),
    ]
    monkeypatch.setattr(storage_store, "default_store", lambda: SimpleNamespace(list=lambda: fake_volumes))

    for protected_ns in ("llm-d-bench-storage-cluster-a", "llm-d-bench-shared-cache"):
        with pytest.raises(caps.DeploymentCapabilityValidationError) as exc_info:
            caps.validate_deployment_namespace(protected_ns, cluster_id="cluster-a")
        assert exc_info.value.code == "protected_namespace"

    # Storage namespace on cluster-b does not block cluster-a
    assert (
        caps.validate_deployment_namespace("llm-d-bench-other-cluster-cache", cluster_id="cluster-a")
        == "llm-d-bench-other-cluster-cache"
    )


def test_verify_prepared_bootstrap_marker_accepts_empty_and_matching_prepared_request(tmp_path):
    import asyncio
    import json

    from llm_d_lens.deploy import capabilities as caps
    from llm_d_lens.deploy.providers import configuration_manifest as cm_module

    expected = {"metadata": {"name": "llmd-ns", "labels": {"lens.io/prepared-request": "req-fresh"}}}

    def make_adapter(marker_data: dict, *, namespace_obj: dict | None = None):
        async def runner(cmd):
            if cmd[:3] == ["kubectl", "get", "namespace"]:
                if namespace_obj is None:
                    return 1, "", "Error from server (Forbidden): is forbidden"
                return 0, json.dumps(namespace_obj), ""
            if cmd[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
                return 0, json.dumps({"data": marker_data}), ""
            return 1, "", "unexpected"

        return cm_module.ConfigurationManifestAdapter(None, runner, "llmd-", 30, output_root=tmp_path)

    # Admin bootstrapped BEFORE prepare(): ConfigMap has empty preparedRequest
    assert (
        asyncio.run(
            make_adapter(
                {"status": "completed", "clusterId": "cluster-a", "namespace": "llmd-ns", "preparedRequest": ""}
            )._verify_prepared_bootstrap_marker("llmd-ns", expected, cluster_id="cluster-a")
        )
        is True
    )
    # Absent preparedRequest key is also accepted
    assert (
        asyncio.run(
            make_adapter(
                {"status": "completed", "clusterId": "cluster-a", "namespace": "llmd-ns"}
            )._verify_prepared_bootstrap_marker("llmd-ns", expected, cluster_id="cluster-a")
        )
        is True
    )
    # Exact matching preparedRequest is accepted
    assert (
        asyncio.run(
            make_adapter(
                {
                    "status": "completed",
                    "clusterId": "cluster-a",
                    "namespace": "llmd-ns",
                    "preparedRequest": "req-fresh",
                }
            )._verify_prepared_bootstrap_marker("llmd-ns", expected, cluster_id="cluster-a")
        )
        is True
    )
    # Non-empty mismatched preparedRequest is rejected
    assert (
        asyncio.run(
            make_adapter(
                {
                    "status": "completed",
                    "clusterId": "cluster-a",
                    "namespace": "llmd-ns",
                    "preparedRequest": "req-stale",
                }
            )._verify_prepared_bootstrap_marker("llmd-ns", expected, cluster_id="cluster-a")
        )
        is False
    )
    # Mismatched clusterId is rejected
    assert (
        asyncio.run(
            make_adapter(
                {"status": "completed", "clusterId": "cluster-b", "namespace": "llmd-ns", "preparedRequest": ""}
            )._verify_prepared_bootstrap_marker("llmd-ns", expected, cluster_id="cluster-a")
        )
        is False
    )
    # _check_prepared_namespace accepts a pre-bootstrapped namespace without lens.io/prepared-request label
    pre_bootstrapped_artifact = cm_module.GuideDeploymentArtifact(
        guide_id="pd-disaggregation",
        artifact_hash="hash",
        deployment_contract={"preparedNamespace": expected},
    )
    asyncio.run(
        make_adapter(
            {"status": "completed", "clusterId": "cluster-a", "namespace": "llmd-ns", "preparedRequest": ""},
            namespace_obj={"metadata": {"name": "llmd-ns", "labels": {}}},
        )._check_prepared_namespace(
            {"namespace": "llmd-ns", "provenance": {"cluster_server_id": "cluster-a"}}, pre_bootstrapped_artifact
        )
    )

    # _check_prepared_namespace accepts prepared_plan_id match in marker & label (post-prepare bootstrap fallback)
    asyncio.run(
        make_adapter(
            {"status": "completed", "clusterId": "cluster-a", "namespace": "llmd-ns", "preparedRequest": "plan-99"},
            namespace_obj={"metadata": {"name": "llmd-ns", "labels": {"lens.io/prepared-request": "plan-99"}}},
        )._check_prepared_namespace(
            {
                "namespace": "llmd-ns",
                "provenance": {"cluster_server_id": "cluster-a", "_prepared_plan_id": "plan-99"},
            },
            pre_bootstrapped_artifact,
        )
    )

    # peek_verified_namespace reads without popping; prepared_plan_id takes precedence over agentic_run
    caps.clear_verified_namespaces()
    caps.record_verified_namespace("cluster-a", "llmd-ns-run", owner_user_id="alice", agentic_deployment_run_id="run-1")
    caps.record_verified_namespace("cluster-a", "llmd-ns-plan", owner_user_id="alice", prepared_plan_id="plan-1")
    assert (
        caps.peek_verified_namespace(
            "cluster-a", owner_user_id="alice", agentic_deployment_run_id="run-1", prepared_plan_id="plan-1"
        )
        == "llmd-ns-plan"
    )
    assert (
        caps.consume_verified_namespace("cluster-a", owner_user_id="alice", prepared_plan_id="plan-1") == "llmd-ns-plan"
    )
    assert (
        caps.peek_verified_namespace("cluster-a", owner_user_id="alice", agentic_deployment_run_id="run-1")
        == "llmd-ns-run"
    )
    assert (
        caps.consume_verified_namespace("cluster-a", owner_user_id="alice", agentic_deployment_run_id="run-1")
        == "llmd-ns-run"
    )
    assert caps.peek_verified_namespace("cluster-a", owner_user_id="alice", agentic_deployment_run_id="run-1") is None


def test_generate_bootstrap_script_resolves_prepared_request_from_prepared_plan(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from llm_d_lens.db.dao.deployment_plan import DeploymentPlanDao
    from llm_d_lens.deploy import capabilities as caps
    from llm_d_lens.deploy.contracts import DeploymentPreparedPlan

    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    plan = DeploymentPlanDao().create(
        DeploymentPreparedPlan(
            cluster_id="cluster-a",
            digest="sha256:test",
            payload={
                "artifacts": [
                    {
                        "context": {"namespace": "llmd-pd-target"},
                        "artifact": {
                            "deployment_contract": {
                                "preparedNamespace": {
                                    "metadata": {
                                        "name": "llmd-pd-target",
                                        "labels": {"lens.io/prepared-request": "req-case-uuid-1"},
                                    }
                                }
                            }
                        },
                    }
                ]
            },
            preflight={},
        )
    )

    async def fake_check(req, runner=None):
        return {
            "status": "admin_required",
            "sufficient": False,
            "clusterId": req.cluster_id,
            "namespace": req.namespace,
            "namespaceLabels": {},
            "identity": {
                "kind": "ServiceAccount",
                "name": "sa",
                "namespace": "lens",
                "subject": "system:serviceaccount:lens:sa",
            },
            "adminOperations": [{"id": "deploy.namespace"}],
            "eppServiceAccounts": [],
            "usesRouter": False,
            "usesLws": True,
            "usesStorage": False,
            "satelliteStorage": None,
            "warnings": [],
        }

    monkeypatch.setattr(caps, "check_deployment_permissions", fake_check)
    monkeypatch.setattr(caps, "_resolve_prepared_plan_request_id", caps._resolve_prepared_plan_request_id)
    script_payload = asyncio.run(
        caps.generate_deployment_bootstrap_script(
            caps.DeploymentPermissionCheckRequest(
                cluster_id="cluster-a",
                namespace="llmd-pd-target",
                prepared_plan_id=plan.id,
            ),
            runner=SimpleNamespace(),
        )
    )
    assert "PREPARED_REQUEST=req-case-uuid-1" in script_payload["script"]
    assert "lens.io/prepared-request: req-case-uuid-1" in script_payload["script"]

    # Another user must NOT resolve the victim plan's lens.io/prepared-request label
    other_user_script = asyncio.run(
        caps.generate_deployment_bootstrap_script(
            caps.DeploymentPermissionCheckRequest(
                cluster_id="cluster-a",
                namespace="llmd-pd-target",
                prepared_plan_id=plan.id,
            ),
            owner_user_id="attacker",
            runner=SimpleNamespace(),
        )
    )
    assert "req-case-uuid-1" not in other_user_script["script"]
    assert f"PREPARED_REQUEST={plan.id}" in other_user_script["script"]


def test_deployment_service_and_adapter_propagate_provenance_to_check_prepared_namespace(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace

    import pytest

    from llm_d_lens.deploy.contracts import (
        ConfigurationArtifact,
        DeploymentArtifact,
        DeploymentEndpoint,
        DeploymentExecution,
        DeploymentStatus,
        VersionedPayload,
    )
    from llm_d_lens.deploy.providers import configuration_manifest as cm_module
    from llm_d_lens.deploy.service import GuideAdapterDeploymentService

    manifest_file = tmp_path / "workload.yaml"
    manifest_file.write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: svc\n", encoding="utf-8")
    expected_ns = {"metadata": {"name": "llmd-owned", "labels": {"lens.io/prepared-request": "req-1"}}}
    artifact = cm_module.GuideDeploymentArtifact(
        guide_id="source-composition",
        artifact_hash="hash-1",
        manifest_ref=str(manifest_file),
        deployment_contract={"readinessDeployments": [], "endpoint": {"port": 80}, "preparedNamespace": expected_ns},
    )

    async def runner(cmd):
        if cmd[:2] == ["kubectl", "apply"]:
            return 0, "applied", ""
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            return 1, "", "Error from server (Forbidden): is forbidden"
        if cmd[:4] == ["kubectl", "get", "configmap", "lens-deployment-bootstrap"]:
            # Marker belongs to foreign-cluster -> must be rejected once provenance.cluster_server_id is propagated!
            return (
                0,
                json.dumps(
                    {
                        "data": {
                            "status": "completed",
                            "clusterId": "foreign-cluster",
                            "namespace": "llmd-owned",
                            "preparedRequest": "",
                        }
                    }
                ),
                "",
            )
        return 1, "", "unexpected"

    adapter = cm_module.ConfigurationManifestAdapter(None, runner, "llmd-", 30, output_root=tmp_path)
    provider_exec = asyncio.run(
        adapter.deploy(
            artifact,
            {
                "namespace": "llmd-owned",
                "provenance": {"cluster_server_id": "cluster-a", "_prepared_plan_id": "plan-1"},
            },
            namespace_created=True,
        )
    )
    assert provider_exec["provenance"] == {"cluster_server_id": "cluster-a", "_prepared_plan_id": "plan-1"}
    with pytest.raises(ValueError, match="not owned by this prepared execution"):
        asyncio.run(adapter.cleanup(provider_exec, artifact))

    # Restored execution state in GuideAdapterDeploymentService also preserves execution.provenance
    svc = GuideAdapterDeploymentService(SimpleNamespace(get_adapter=lambda _ref: adapter), lambda _req: "llmd-owned")
    cfg_art = ConfigurationArtifact(
        schema_version="v1",
        kind="pd",
        provider_ref="pd-disaggregation",
        media_type="application/json",
        content="{}",
        checksum="sum",
    )
    restored_exec = DeploymentExecution(
        execution_id="exec-1",
        request_id="req-1",
        status=DeploymentStatus.READY,
        artifact=DeploymentArtifact(
            artifact_hash="hash-1",
            configuration_artifact_ids=[cfg_art.artifact_id],
            manifest_ref=str(manifest_file),
            rendered_payload=VersionedPayload(
                schema_version="v1",
                value={"deployment_contract": {"preparedNamespace": expected_ns}},
            ),
        ),
        namespace="llmd-owned",
        endpoint=DeploymentEndpoint(url="http://svc.llmd-owned.svc.cluster.local:8000"),
        configuration_artifacts=[cfg_art],
        provenance={"cluster_server_id": "cluster-a", "_prepared_plan_id": "plan-1"},
        diagnostics=VersionedPayload(schema_version="v1", value={}),
    )
    _adp, _art, restored_state = svc._restored_provider_state(restored_exec)
    assert restored_state["provenance"] == {"cluster_server_id": "cluster-a", "_prepared_plan_id": "plan-1"}
    with pytest.raises(ValueError, match="not owned by this prepared execution"):
        asyncio.run(adapter.cleanup(restored_state, artifact))
