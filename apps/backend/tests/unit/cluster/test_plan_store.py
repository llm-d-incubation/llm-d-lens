"""Cluster provisioning-plan persistence and execution-lock CAS behavior."""

from datetime import UTC, datetime, timedelta

import pytest

from llm_d_lens.auth.records import UserRecord
from llm_d_lens.cluster import registry
from llm_d_lens.db.dao.cluster import ClusterDao
from llm_d_lens.db.dao.user import UserDao


def _create_cluster(
    name: str = "cluster-a",
    *,
    owner_user_id: str | None = None,
    draft: bool = False,
) -> registry.Cluster:
    return registry.create_cluster(name, "", "kubeconfig: {}", draft=draft, owner_user_id=owner_user_id)


def test_create_cluster_records_owner_user_id():
    owner = UserDao().create(UserRecord(username="owner"))
    cluster = _create_cluster(owner_user_id=owner.id)

    assert cluster.owner_user_id == owner.id
    assert "ownerUserId" not in cluster.to_dict()
    assert registry.Cluster.from_dict(cluster.to_dict()).owner_user_id is None
    reloaded = registry.get_cluster(cluster.id)
    assert reloaded is not None
    assert reloaded.owner_user_id == owner.id
    assert ClusterDao().get_owner_user_id(cluster.id) == owner.id
    assert ClusterDao().get_owner_user_id("missing") is None
    assert _create_cluster("unowned").owner_user_id is None


def test_provisioning_plan_round_trips():
    cluster = _create_cluster()
    dao = ClusterDao()

    assert dao.get_provisioning_plan(cluster.id) is None
    plan = {"planVersion": 1, "gateway": {"provider": "istio", "port": 30080}}
    assert dao.set_provisioning_plan(cluster.id, plan) is True
    assert dao.get_provisioning_plan(cluster.id) == plan

    updated = {**plan, "planVersion": 2}
    assert dao.set_provisioning_plan(cluster.id, updated) is True
    assert dao.get_provisioning_plan(cluster.id) == updated

    assert dao.get_provisioning_plan("missing") is None
    assert dao.set_provisioning_plan("missing", plan) is False


def test_execution_operation_ledger_round_trips():
    cluster = _create_cluster()
    dao = ClusterDao()

    assert dao.get_execution_operations(cluster.id) == {}
    assert dao.get_execution_operations("missing") is None

    ledger = {"gateway.namespace": "admin_done", "inotify.daemonset": "user_done"}
    assert dao.set_execution_operations(cluster.id, ledger) is True
    assert dao.get_execution_operations(cluster.id) == ledger
    assert dao.set_execution_operations("missing", ledger) is False

    updated = {**ledger, "gateway.crds": "failed"}
    assert dao.set_execution_operations(cluster.id, updated) is True
    assert dao.get_execution_operations(cluster.id) == updated

    assert dao.clear_execution_operations(cluster.id) is True
    assert dao.get_execution_operations(cluster.id) == {}
    assert dao.clear_execution_operations("missing") is False


def test_set_provisioning_plan_invalidates_execution_state_on_change():
    cluster = _create_cluster()
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    assert dao.try_acquire_execution(cluster.id, "token-a", now=now, stale_before=now) is True
    assert dao.set_execution_operations(cluster.id, {"gateway.namespace": "admin_done"}) is True
    assert dao.complete_execution(cluster.id, "token-a", now=now) is True
    assert dao.is_execution_completed(cluster.id) is True

    changed = {"planVersion": 2, "gateway": {"provider": "istio"}}
    assert dao.set_provisioning_plan(cluster.id, changed, invalidate_execution=True) is True
    assert dao.get_provisioning_plan(cluster.id) == changed
    assert dao.get_execution_operations(cluster.id) == {}
    assert dao.get_execution_completed_at(cluster.id) is None

    # Saving the same plan again must not clear a fresh run.
    assert dao.try_acquire_execution(cluster.id, "token-b", now=now, stale_before=now) is True
    assert dao.set_execution_operations(cluster.id, {"gateway.namespace": "admin_done"}) is True
    assert dao.complete_execution(cluster.id, "token-b", now=now) is True
    assert dao.set_provisioning_plan(cluster.id, changed) is True
    assert dao.get_execution_operations(cluster.id) == {"gateway.namespace": "admin_done"}
    assert dao.get_execution_completed_at(cluster.id) == now


def test_execution_lock_acquire_refresh_cancel_release():
    cluster = _create_cluster()
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stale_before = now - timedelta(minutes=5)

    assert dao.get_execution_state("missing") is None
    assert dao.try_acquire_execution(cluster.id, "token-a", now=now, stale_before=stale_before) is True
    assert dao.get_execution_state(cluster.id) == {
        "token": "token-a",
        "started_at": now,
        "heartbeat_at": now,
        "cancel_requested": False,
    }

    assert (
        dao.try_acquire_execution(
            cluster.id,
            "token-b",
            now=now + timedelta(seconds=1),
            stale_before=stale_before,
        )
        is False
    )

    later = now + timedelta(seconds=30)
    assert dao.refresh_execution_heartbeat(cluster.id, "token-a", later) is True
    assert dao.refresh_execution_heartbeat(cluster.id, "wrong-token", later) is False
    assert dao.get_execution_state(cluster.id)["heartbeat_at"] == later

    assert dao.request_execution_cancel(cluster.id, "wrong-token") is False
    assert dao.request_execution_cancel(cluster.id, "token-a") is True
    assert dao.get_execution_state(cluster.id)["cancel_requested"] is True

    assert dao.release_execution(cluster.id, "wrong-token") is False
    assert dao.release_execution(cluster.id, "token-a") is True
    assert dao.get_execution_state(cluster.id) == {
        "token": None,
        "started_at": None,
        "heartbeat_at": None,
        "cancel_requested": False,
    }


def test_execution_lock_stale_takeover_resets_cancel_flag():
    cluster = _create_cluster()
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stale_before = now - timedelta(minutes=5)

    assert dao.try_acquire_execution(cluster.id, "token-a", now=now, stale_before=stale_before) is True
    assert dao.request_execution_cancel(cluster.id, "token-a") is True

    takeover_at = now + timedelta(minutes=6)
    assert (
        dao.try_acquire_execution(
            cluster.id,
            "token-b",
            now=takeover_at,
            stale_before=takeover_at - timedelta(minutes=5),
        )
        is True
    )
    assert dao.get_execution_state(cluster.id) == {
        "token": "token-b",
        "started_at": takeover_at,
        "heartbeat_at": takeover_at,
        "cancel_requested": False,
    }


def test_execution_cancel_force_ignores_token():
    cluster = _create_cluster()
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    assert dao.try_acquire_execution(cluster.id, "token-a", now=now, stale_before=now) is True
    assert dao.request_execution_cancel(cluster.id, "someone-else") is False
    assert dao.request_execution_cancel(cluster.id, "someone-else", force=True) is True
    assert dao.get_execution_state(cluster.id)["cancel_requested"] is True


def test_execution_cancel_force_does_not_flag_idle_cluster():
    cluster = _create_cluster()
    dao = ClusterDao()

    assert dao.request_execution_cancel(cluster.id, "token-a", force=True) is False
    assert dao.get_execution_state(cluster.id)["cancel_requested"] is False

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert dao.try_acquire_execution(cluster.id, "token-a", now=now, stale_before=now) is True
    assert dao.release_execution(cluster.id, "token-a") is True
    assert dao.request_execution_cancel(cluster.id, "token-a", force=True) is False
    assert dao.get_execution_state(cluster.id)["cancel_requested"] is False


def test_execution_completion_is_bound_to_the_plan_hash():
    cluster = _create_cluster()
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    assert dao.try_acquire_execution(cluster.id, "token-a", now=now, stale_before=now) is True
    assert dao.complete_execution(cluster.id, "token-a", now=now, plan_hash="hash-a") is True
    assert dao.is_execution_completed(cluster.id, "hash-a") is True
    assert dao.is_execution_completed(cluster.id, "hash-b") is False
    assert dao.is_execution_completed(cluster.id) is True

    legacy = _create_cluster("legacy-completion")
    assert dao.try_acquire_execution(legacy.id, "token-b", now=now, stale_before=now) is True
    assert dao.complete_execution(legacy.id, "token-b", now=now, plan_hash=None) is True
    assert dao.is_execution_completed(legacy.id, "hash-a") is False


def test_compare_and_set_draft_provisioning_plan_mirrors_columns_and_invalidates():
    import pytest

    cluster = _create_cluster("cas-mirror", draft=True)
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stale_before = now - timedelta(minutes=5)

    initial = {"planVersion": 5, "tag": "v1"}
    assert dao.set_provisioning_plan(cluster.id, initial) is True
    assert dao.try_acquire_execution(cluster.id, "tok-1", now=now, stale_before=stale_before) is True
    assert dao.set_execution_operations(cluster.id, {"gateway.manifests": "admin_done"}) is True
    assert dao.complete_execution(cluster.id, "tok-1", now=now, plan_hash="hash-v1") is True

    def _hasher(raw):
        return f"hash-{raw['tag']}" if isinstance(raw, dict) and "tag" in raw else None

    mirrored = {
        "gateway_provider": "istio",
        "gateway_namespace": f"gw-ns-{cluster.id}",
        "gateway_name": "gw-name",
        "gateway_public_url": "https://gw.example",
        "gateway_port": 30443,
        "gateway_authz_host": "authz.example",
        "inotify_max_user_instances": 16384,
        "router_version": "v1.1",
        "gie_version": "v2.1",
        "ipp_version": "v3.1",
        "hf_token_secret_namespace": "team-ns",
        "hf_token_secret_name": "hf-secret",
    }
    updated = {"planVersion": 5, "tag": "v2"}
    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            updated,
            stale_before=stale_before,
            expected_plan_hash="hash-v1",
            new_plan_hash="hash-v2",
            plan_hasher=_hasher,
            mirrored_columns=mirrored,
            invalidate_execution=True,
        )
        == "updated"
    )
    assert dao.get_provisioning_plan(cluster.id) == updated
    assert dao.get_execution_operations(cluster.id) == {}
    assert dao.get_execution_completed_at(cluster.id) is None
    assert dao.get_execution_completed_plan_hash(cluster.id) is None
    reloaded = dao.get(cluster.id)
    assert reloaded is not None
    assert reloaded.gateway_provider == "istio"
    assert reloaded.gateway_namespace == f"gw-ns-{cluster.id}"
    assert reloaded.gateway_name == "gw-name"
    assert reloaded.gateway_public_url == "https://gw.example"
    assert reloaded.gateway_port == 30443
    assert reloaded.gateway_authz_host == "authz.example"
    assert reloaded.inotify_max_user_instances == 16384
    assert reloaded.router_version == "v1.1"
    assert reloaded.gie_version == "v2.1"
    assert reloaded.ipp_version == "v3.1"
    assert reloaded.hf_token_secret_namespace == "team-ns"  # noqa: S105 - namespace, not a secret
    assert reloaded.hf_token_secret_name == "hf-secret"  # noqa: S105 - secret name, not a secret

    # Re-saving an identical plan hash preserves execution state even with invalidate_execution=True.
    assert dao.try_acquire_execution(cluster.id, "tok-2", now=now, stale_before=stale_before) is True
    assert dao.set_execution_operations(cluster.id, {"gateway.manifests": "admin_done"}) is True
    assert dao.complete_execution(cluster.id, "tok-2", now=now, plan_hash="hash-v2") is True
    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            updated,
            stale_before=stale_before,
            expected_plan_hash="hash-v2",
            new_plan_hash="hash-v2",
            plan_hasher=_hasher,
            invalidate_execution=True,
        )
        == "updated"
    )
    assert dao.get_execution_operations(cluster.id) == {"gateway.manifests": "admin_done"}
    assert dao.get_execution_completed_at(cluster.id) == now
    assert dao.get_execution_completed_plan_hash(cluster.id) == "hash-v2"

    with pytest.raises(ValueError, match="unknown mirrored plan column"):
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            updated,
            stale_before=stale_before,
            mirrored_columns={"not_a_column": "bad"},
        )


def test_compare_and_set_draft_provisioning_plan_guards_and_stale_takeover():
    cluster = _create_cluster("cas-guards", draft=True)
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stale_before = now - timedelta(minutes=5)
    plan_a = {"tag": "a"}
    plan_b = {"tag": "b"}

    def _hasher(raw):
        return raw.get("tag") if isinstance(raw, dict) else None

    assert dao.set_provisioning_plan(cluster.id, plan_a) is True
    assert (
        dao.compare_and_set_draft_provisioning_plan(
            "missing",
            plan_b,
            stale_before=stale_before,
        )
        == "not_found"
    )
    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            plan_b,
            stale_before=stale_before,
            expected_plan_hash="wrong",
            plan_hasher=_hasher,
        )
        == "plan_conflict"
    )

    # Live lock rejects; expired heartbeat allows CAS update.
    assert dao.try_acquire_execution(cluster.id, "tok-live", now=now, stale_before=stale_before) is True
    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            plan_b,
            stale_before=stale_before,
            expected_plan_hash="a",
            plan_hasher=_hasher,
        )
        == "execution_in_progress"
    )
    expired_stale_before = now + timedelta(seconds=1)
    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            plan_b,
            stale_before=expired_stale_before,
            expected_plan_hash="a",
            new_plan_hash="b",
            plan_hasher=_hasher,
        )
        == "updated"
    )

    # Non-draft cluster is rejected.
    dao.update(cluster.id, draft=False)
    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            plan_a,
            stale_before=expired_stale_before,
        )
        == "not_draft"
    )


def test_compare_and_set_draft_provisioning_plan_retries_on_unrelated_version_bump():
    cluster = _create_cluster("cas-retry", draft=True)
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stale_before = now - timedelta(minutes=5)
    dao.set_provisioning_plan(cluster.id, {"tag": "a"})

    attempts = 0

    def _bumping_once_hasher(raw):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Simulate a concurrent unrelated write (e.g. repo download path) bumping version_id.
            ClusterDao().update(cluster.id, llm_d_repo_path="/tmp/repo-v1")
        return raw.get("tag") if isinstance(raw, dict) else None

    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            {"tag": "b"},
            stale_before=stale_before,
            expected_plan_hash="a",
            new_plan_hash="b",
            plan_hasher=_bumping_once_hasher,
        )
        == "updated"
    )
    assert attempts == 2

    # Exhausting all 3 retries returns plan_conflict.
    contend_attempts = 0

    def _always_bumping_hasher(raw):
        nonlocal contend_attempts
        contend_attempts += 1
        ClusterDao().update(cluster.id, llm_d_repo_path=f"/tmp/repo-contend-{contend_attempts}")
        return raw.get("tag") if isinstance(raw, dict) else None

    assert (
        dao.compare_and_set_draft_provisioning_plan(
            cluster.id,
            {"tag": "c"},
            stale_before=stale_before,
            expected_plan_hash="b",
            new_plan_hash="c",
            plan_hasher=_always_bumping_hasher,
        )
        == "plan_conflict"
    )
    assert contend_attempts == 3


def test_monitoring_url_host_kubeconfig_and_resolution_helpers():
    assert registry.validate_monitoring_http_url(None) is None
    assert registry.validate_monitoring_http_url("   ") is None
    assert registry.validate_monitoring_http_url("HTTP://127.0.0.1:9090/") == "http://127.0.0.1:9090"
    assert registry.validate_monitoring_http_url("https://prom.monitoring.svc.cluster.local:9090/sub/") == (
        "https://prom.monitoring.svc.cluster.local:9090/sub"
    )
    assert registry.validate_monitoring_http_url("http://[::1]:9090") == "http://[::1]:9090"

    for bad_url in (
        "http://169.254.169.254:9090",
        "http://169.254.169.254.:9090",
        "http://10.0.0.1.:9090",
        "http://2852039166:80",
        "http://0xa9fea9fe:80",
        "http://0251.0376.0251.0376:80",
        "http://169.254.43518:80",
        "http://0:80",
        "http://10.0.0.1\\foo:9090",
        "http://[fd00:ec2::254]:9090",
        "http://[::ffff:169.254.169.254]:9090",
        "http://[2002:a9fe:a9fe::1]:9090",
        "http://[2001:0000:a9fe:a9fe::1]:9090",
        "http://[2001:0000:0808:0808::5601:5601]:9090",
        "http://[64:ff9b::169.254.169.254]:9090",
        "http://[64:ff9b:1::a9fe:a9fe]:9090",
        "http://[fe80::1%eth0]:9090",
        "http://224.0.0.1:9090",
        "http://0.0.0.0:9090",
        "http://user:pass@10.0.0.1:9090",
        "http://10.0.0.1:0",
    ):
        with pytest.raises(ValueError):
            registry.validate_monitoring_http_url(bad_url)

    assert registry.extract_url_host("https://[fd00::1]:6443/v1") == "fd00::1"
    assert registry.extract_url_host("10.0.0.5:9090") == "10.0.0.5"
    for bad_host in (
        "169.254.169.254",
        "169.254.169.254.",
        "10.0.0.1.",
        "2852039166",
        "0xa9fea9fe",
        "0251.0376.0251.0376",
        "10.0.0.1\\foo",
        "fe80::1%eth0",
        "ftp://10.0.0.1:9090",
        "http://10.0.0.1:9090?x=1",
        "http://10.0.0.1:9090#frag",
        "[2002:a9fe:a9fe::1]",
        "[2001:0000:a9fe:a9fe::1]",
        "[64:ff9b::169.254.169.254]",
    ):
        assert registry.extract_url_host(bad_host) is None
    assert registry.format_host_port_url("fd00::1", 30090) == "http://[fd00::1]:30090"
    assert registry.format_host_port_url("10.0.0.5", 30090) == "http://10.0.0.5:30090"

    for bad_ns in ("--kubeconfig=/etc/passwd", "Bad_Namespace", "ns\n", "a" * 64, ""):
        resolved_bad_ns = registry.resolve_monitoring_settings({"monitoring": {"namespace": bad_ns}})
        assert resolved_bad_ns["monitoring_namespace"] == registry.DEFAULT_MONITORING_NAMESPACE
        from_dict_bad_ns = registry.Cluster.from_dict({"id": "c1", "monitoringNamespace": bad_ns})
        assert from_dict_bad_ns.monitoring_namespace == registry.DEFAULT_MONITORING_NAMESPACE

    kubeconfig = """
apiVersion: v1
kind: Config
current-context: ctx-b
contexts:
  - name: ctx-a
    context:
      cluster: cl-a
  - name: ctx-b
    context:
      cluster: cl-b
clusters:
  - name: cl-a
    cluster:
      server: https://10.0.0.1:6443
  - name: cl-b
    cluster:
      server: https://192.168.50.10:6443
"""
    assert registry.kubeconfig_server_host(kubeconfig) == "192.168.50.10"
    assert registry.is_monitoring_only_plan({"monitoring": {"mode": "existing"}}) is True
    assert registry.is_monitoring_only_plan({"monitoring": {}, "gateway": {}}) is False
    assert registry.is_monitoring_only_plan({}) is False

    # Precedence: explicit URL > monitoring.host > gateway_public_url > kubeconfig_server_host
    explicit = registry.resolve_monitoring_settings(
        {
            "monitoring": {
                "mode": "existing",
                "namespace": "custom-mon",
                "prometheusUrl": "http://10.10.0.1:9090",
                "prometheusNodePort": 30090,
                "grafanaNodePort": 30300,
                "host": "10.10.0.2",
            }
        },
        gateway_public_url="https://10.10.0.3:30443",
        kubeconfig_text=kubeconfig,
    )
    assert explicit["monitoring_mode"] == "existing"
    assert explicit["monitoring_namespace"] == "custom-mon"
    assert explicit["prometheus_url"] == "http://10.10.0.1:9090"
    assert explicit["prometheus_url_source"] == "explicit_url"
    assert explicit["grafana_url"] == "http://10.10.0.2:30300"

    from_gateway = registry.resolve_monitoring_settings(
        {"monitoring": {"prometheusNodePort": 30090}},
        gateway_public_url="https://10.10.0.3:30443",
        kubeconfig_text=kubeconfig,
    )
    assert from_gateway["prometheus_url"] == "http://10.10.0.3:30090"
    assert from_gateway["prometheus_url_source"] == "node_port"

    from_kubeconfig = registry.resolve_monitoring_settings(
        {"monitoring": {"prometheusNodePort": 30090}},
        kubeconfig_text=kubeconfig,
    )
    assert from_kubeconfig["prometheus_url"] == "http://192.168.50.10:30090"
    assert from_kubeconfig["prometheus_url_source"] == "node_port"


def test_monitoring_plan_persists_without_schema_changes_and_respects_invalidate_flag():
    kubeconfig = """
apiVersion: v1
kind: Config
clusters:
  - name: c1
    cluster:
      server: https://10.20.30.40:6443
"""
    cluster = registry.create_cluster("mon-cluster", "", kubeconfig, draft=True)
    dao = ClusterDao()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert dao.try_acquire_execution(cluster.id, "tok-m", now=now, stale_before=now) is True
    assert dao.set_execution_operations(cluster.id, {"gateway.namespace": "admin_done"}) is True
    assert dao.complete_execution(cluster.id, "tok-m", now=now, plan_hash="h1") is True

    # Updating monitoring with invalidate_execution=False keeps execution ledger intact.
    updated = registry.update_cluster_monitoring(
        cluster.id,
        {
            "mode": "existing",
            "namespace": "obs",
            "prometheusNodePort": 30090,
            "grafanaUrl": "https://grafana.internal/dash",
        },
        invalidate_execution=False,
    )
    assert updated.monitoring_mode == "existing"
    assert updated.monitoring_namespace == "obs"
    assert updated.prometheus_url == "http://10.20.30.40:30090"
    assert updated.grafana_url == "https://grafana.internal/dash"
    assert updated.prometheus_node_port == 30090
    assert dao.get_execution_operations(cluster.id) == {"gateway.namespace": "admin_done"}
    assert dao.get_execution_completed_plan_hash(cluster.id) == "h1"

    resolved = registry.get_cluster_monitoring_settings(cluster.id)
    assert resolved is not None
    assert resolved["prometheus_url_source"] == "node_port"
    assert registry.get_cluster_monitoring_settings("missing") is None

    # DTO round-trip preserves monitoring fields.
    round_trip = registry.Cluster.from_dict(updated.to_dict())
    assert round_trip.monitoring_mode == "existing"
    assert round_trip.monitoring_namespace == "obs"
    assert round_trip.prometheus_url == "http://10.20.30.40:30090"
    assert round_trip.grafana_url == "https://grafana.internal/dash"
    assert round_trip.prometheus_node_port == 30090

    # Atomic update_cluster(..., monitoring_plan=..., invalidate_execution=True) updates both & clears ledger.
    invalidated = registry.update_cluster(
        cluster.id,
        description="updated desc",
        monitoring_plan={
            "mode": "managed",
            "namespace": "llm-d-monitoring",
            "prometheusUrl": "http://10.20.30.41:9090",
        },
        invalidate_execution=True,
    )
    assert invalidated.description == "updated desc"
    assert invalidated.monitoring_mode == "managed"
    assert invalidated.prometheus_url == "http://10.20.30.41:9090"
    assert dao.get_execution_operations(cluster.id) == {}
    assert dao.get_execution_completed_at(cluster.id) is None
