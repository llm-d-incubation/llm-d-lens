"""Admin operation script rendering and chart-source resolution."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from llm_d_lens.cluster.operations import (
    ApplyOptions,
    ChartCRDSource,
    Operation,
    OperationApply,
    OperationRequirement,
    OperationWait,
    build_operations,
)
from llm_d_lens.cluster.plan import normalize_plan, plan_hash, plan_hash16
from llm_d_lens.cluster.remediation import (
    PROTECTED_NAMESPACES,
    AdminScriptSourceUnavailableError,
    AdminScriptValidationError,
    CleanupOperation,
    ResolvedChartCRDs,
    cleanup_cluster_label_value,
    cleanup_digest,
    cleanup_marker_name,
    render_admin_cleanup_script,
    render_admin_operations_script,
    resolve_chart_crds,
    validate_crd_yaml,
    validate_field_manager,
)

CLUSTER_ID = "0123456789abcdef"


def _plan(**overrides):
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


def _operations(plan):
    return build_operations(plan, cluster_id=CLUSTER_ID)


def _render(plan, operation_ids, *, resolver=None):
    operations = _operations(plan)
    return render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=plan,
        operations=operations,
        admin_operation_ids=operation_ids,
        resolver=resolver,
    )


def _custom_operation(operation_id: str, *, apply: OperationApply, depends_on=()) -> Operation:
    return Operation(
        id=operation_id,
        step="custom",
        title=operation_id,
        scope="cluster",
        namespace=None,
        apply=apply,
        requirement=OperationRequirement(checks=()),
        depends_on=depends_on,
    )


def _fake_resolver(_source):
    return ResolvedChartCRDs(
        yaml="apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: widgets.example.io\n",
        digest="sha256:" + "a" * 64,
    )


def test_manifests_render_with_wait_and_completion_marker():
    plan = _plan()

    generated = _render(plan, ["gateway.manifests"])

    assert "set -euo pipefail" in generated.script
    assert "umask 077" in generated.script
    assert "base64 -d >" in generated.script
    assert 'kubectl apply --namespace lens-gateway -f "${LENS_YAML_' in generated.script
    assert "kubectl wait --for=condition=Programmed gateway/lens-gateway" in generated.script
    assert "--timeout=300s" in generated.script
    assert "AddressNotAssigned" not in generated.script
    assert "append_done gateway.manifests" in generated.script
    assert "trap 'write_marker partial || true' ERR" in generated.script
    assert generated.script.rindex("write_marker complete") > generated.script.rindex("append_done gateway.manifests")
    assert generated.marker == {
        "namespace": "default",
        "name": f"lens-admin-handoff-{plan_hash16(plan)}",
    }
    assert generated.plan_hash == plan_hash(plan)
    assert generated.admin_operations == ("gateway.manifests",)
    assert f"PLAN_HASH16={plan_hash16(plan)}" in generated.script
    assert 'lens.llm-d.ai/plan-hash: "$PLAN_HASH16"' in generated.script
    assert 'planHash: "$PLAN_HASH"' in generated.script
    assert len(plan_hash16(plan)) <= 63


def test_helm_uses_values_stdin_and_never_set():
    plan = _plan(accelerators=[{"vendor": "nvidia", "mode": "plugin", "rbac": ["escalation"]}])

    generated = _render(plan, ["accelerator.nvidia.plugin.0"])

    assert "helm upgrade --install nvidia-device-plugin" in generated.script
    assert '--values "${LENS_STDIN_' in generated.script
    assert "LENS_STDIN_" in generated.script
    assert "base64 -d >" in generated.script
    assert "--set" not in generated.script
    assert "kubectl rollout status daemonset/nvidia-device-plugin --namespace kube-system" in generated.script


def test_command_operations_use_quoted_argv():
    plan = _plan(accelerators=[{"vendor": "intel", "mode": "dra"}])

    generated = _render(plan, ["accelerator.nfd"])

    assert "kubectl apply -k 'https://github.com/intel/intel-device-plugins-for-kubernetes" in generated.script
    assert "kubectl wait --for=condition=Established crd/nodefeaturerules.nfd.k8s-sigs.io" in generated.script


def test_manifests_from_chart_embeds_crds_and_never_helm_pull():
    plan = _plan(stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False})

    generated = _render(plan, ["lws.namespace", "lws.crds"], resolver=_fake_resolver)

    assert "helm pull" not in generated.script
    assert "kubectl apply --server-side --force-conflicts --field-manager=lens" in generated.script
    assert "base64 -d >" in generated.script
    assert "# source: oci://registry.k8s.io/lws/charts/lws@" in generated.script
    assert generated.source_digests["lws.crds"] == "sha256:" + "a" * 64


def test_apply_options_wait_kinds_and_delete_before_apply():
    operation = _custom_operation(
        "custom.manifests",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "widgets", "namespace": "team-a"},
                    "data": {"key": "value"},
                },
            ),
            namespace="team-a",
            apply_options=ApplyOptions(
                server_side=True,
                force_conflicts=True,
                timeout_seconds=42,
            ),
            wait=(
                OperationWait(kind="rollout", target="deployment/widgets", namespace="team-a", timeout_seconds=30),
                OperationWait(kind="pvc-bound", target="pvc/cache", namespace="team-a", timeout_seconds=31),
                OperationWait(kind="crd-established", target="crd/widgets.example.io", timeout_seconds=32),
                OperationWait(kind="job-complete", target="job/import", namespace="team-a", timeout_seconds=33),
                OperationWait(kind="gateway-programmed", target="gateway/main", namespace="team-a", timeout_seconds=34),
                OperationWait(kind="helm", target="widget-release", namespace="team-a", timeout_seconds=34),
                OperationWait(kind="node-label", target="example.io/gpu=true", timeout_seconds=35),
                OperationWait(kind="driver-status", target="a=b;c=d", timeout_seconds=36),
            ),
        ),
    )

    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=_plan(),
        operations=[operation],
        admin_operation_ids=[operation.id],
    )

    script = generated.script
    assert "--server-side --force-conflicts --timeout=42s" in script
    assert "kubectl rollout status deployment/widgets --namespace team-a --timeout=30s" in script
    assert "kubectl wait --for='jsonpath={.status.phase}=Bound' pvc/cache" in script
    assert "kubectl wait --for=condition=Established crd/widgets.example.io" in script
    assert "kubectl wait --for=condition=Complete job/import" in script
    assert "kubectl wait --for=condition=Programmed gateway/main" in script
    assert "helm status widget-release --namespace team-a" in script
    assert "kubectl get nodes -l example.io/gpu=true -o name" in script
    assert "kubectl get daemonsets --all-namespaces -l a=b" in script
    assert "kubectl get daemonsets --all-namespaces -l c=d" in script


def test_driver_status_and_skip_if_driver_ready_support_profile_matchers():
    operation = _custom_operation(
        "accelerator.intel.dra.0",
        apply=OperationApply(
            kind="helm",
            argv=(
                "helm",
                "upgrade",
                "--install",
                "intel-gpu-resource-driver",
                "oci://ghcr.io/intel/intel-resource-drivers-for-kubernetes/intel-gpu-resource-driver-chart",
                "--namespace",
                "intel-gpu-resource-driver",
                "--create-namespace",
                "--version",
                "0.11.0",
                "--values",
                "-",
            ),
            stdin="kubeletPlugin:\n  privileged: true\n",
            namespace="intel-gpu-resource-driver",
            wait=(
                OperationWait(
                    kind="rollout",
                    target="daemonset/intel-gpu-resource-driver-kubelet-plugin",
                    namespace="intel-gpu-resource-driver",
                ),
            ),
            skip_if_driver_ready=(
                "name_contains=intel-gpu-resource-driver;"
                "namespace=intel-gpu-resource-driver;"
                "label=app.kubernetes.io/name=intel-gpu-resource-driver"
            ),
        ),
    )

    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=_plan(),
        operations=[operation],
        admin_operation_ids=[operation.id],
    )

    script = generated.script
    assert 'index(tolower($1), "intel-gpu-resource-driver")>0' in script
    assert "kubectl get daemonsets --namespace intel-gpu-resource-driver " in script
    assert "kubectl get daemonsets --all-namespaces -l app.kubernetes.io/name=intel-gpu-resource-driver " in script
    assert "Driver already ready for %s; skipping install." in script
    assert "append_done accelerator.intel.dra.0" in script


def test_delete_before_apply_is_rejected_for_admin_operations():
    operation = _custom_operation(
        "custom.delete",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "widgets"},
                },
            ),
            apply_options=ApplyOptions(delete_before_apply=True),
        ),
    )
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[operation],
            admin_operation_ids=[operation.id],
        )


def test_dependencies_must_be_rendered_before_dependents():
    first = _custom_operation("first", apply=OperationApply(kind="command", argv=("true",)))
    second = _custom_operation(
        "second",
        apply=OperationApply(kind="command", argv=("true",)),
        depends_on=("first",),
    )

    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=_plan(),
        operations=[first, second],
        admin_operation_ids=["first", "second"],
    )
    assert generated.script.index("operation: first") < generated.script.index("operation: second")

    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[second, first],
            admin_operation_ids=["first", "second"],
        )


def test_user_secret_lens_and_handoff_identity_objects_are_rejected():
    for operation_id, kind in (
        ("secret.write", "Secret"),
        ("sa.write", "ServiceAccount"),
        ("role.write", "ClusterRole"),
        ("binding.write", "ClusterRoleBinding"),
    ):
        operation = _custom_operation(
            operation_id,
            apply=OperationApply(
                kind="manifests",
                manifests=({"apiVersion": "v1", "kind": kind, "metadata": {"name": "x"}},),
            ),
        )
        with pytest.raises(AdminScriptValidationError):
            render_admin_operations_script(
                cluster_id=CLUSTER_ID,
                plan=_plan(),
                operations=[operation],
                admin_operation_ids=[operation.id],
            )

    lens = _custom_operation("lens.op", apply=OperationApply(kind="lens"))
    lens = Operation(
        id=lens.id,
        step=lens.step,
        title=lens.title,
        scope=lens.scope,
        namespace=lens.namespace,
        apply=lens.apply,
        requirement=lens.requirement,
        executor="lens",
    )
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[lens],
            admin_operation_ids=[lens.id],
        )


def test_command_values_are_quoted_and_newlines_are_rejected():
    operation = _custom_operation(
        "command.quoted",
        apply=OperationApply(kind="command", argv=("printf", "%s", "a b; $(rm -rf /)")),
    )

    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=_plan(),
        operations=[operation],
        admin_operation_ids=[operation.id],
    )
    assert "'a b; $(rm -rf /)'" in generated.script

    invalid = _custom_operation(
        "command.invalid",
        apply=OperationApply(kind="command", argv=("printf", "line\nbreak")),
    )
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[invalid],
            admin_operation_ids=[invalid.id],
        )


@pytest.mark.parametrize("flag", ["--set", "--set=", "--set-string", "--set-file", "--set-literal"])
def test_helm_set_variants_are_rejected(flag):
    helm = _custom_operation(
        "helm.set",
        apply=OperationApply(kind="helm", argv=("helm", "upgrade", "--install", "x", flag, "a=b")),
    )
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[helm],
            admin_operation_ids=[helm.id],
        )


def _chart_archive(base: Path, *, files: dict[str, str]) -> Path:
    source = base / "source"
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    archive = base / "lws.tgz"
    with tarfile.open(archive, "w:gz") as handle:
        for path in sorted(source.rglob("*")):
            handle.add(path, arcname=str(path.relative_to(source)))
    return archive


def _patch_helm_pull(monkeypatch, archive: Path):
    def fake_pull(_source, destination):
        copied = destination / "lws.tgz"
        copied.write_bytes(archive.read_bytes())
        return copied

    monkeypatch.setattr("llm_d_lens.cluster.remediation._helm_pull", fake_pull)


def _fake_execution_bin(bindir: Path) -> Path:
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        """#!/usr/bin/env bash
set -e
case "$1" in
  apply)
    file=""
    previous=""
    for argument in "$@"; do
      if [[ "$previous" == "-f" ]]; then file="$argument"; fi
      previous="$argument"
    done
    if [[ -z "$file" || "$file" == "-" ]]; then
      cat > "${MARKER_STDIN:-/dev/null}"
      exit 0
    fi
    [[ -f "$file" ]] && printf 'yes' >"$RECORD_DIR/exists"
    stat -c '%a' "$file" >"$RECORD_DIR/mode"
    cp "$file" "$RECORD_DIR/content"
    printf '%s' "$file" >"$RECORD_DIR/path"
    exit 0
    ;;
  *) exit 0 ;;
esac
"""
    )
    kubectl.chmod(0o755)
    helm = bindir / "helm"
    helm.write_text(
        """#!/usr/bin/env bash
set -e
previous=""
for argument in "$@"; do
  if [[ "$previous" == "--values" ]]; then
    [[ -f "$argument" ]] && printf 'yes' >"$RECORD_DIR/exists"
    stat -c '%a' "$argument" >"$RECORD_DIR/mode"
    cp "$argument" "$RECORD_DIR/content"
    printf '%s' "$argument" >"$RECORD_DIR/path"
  fi
  previous="$argument"
done
exit 0
"""
    )
    helm.chmod(0o755)
    return bindir


def _run_admin_script(tmp_path: Path, script_text: str) -> tuple[subprocess.CompletedProcess, Path]:
    bindir = _fake_execution_bin(tmp_path / "bin")
    record = tmp_path / "record"
    record.mkdir()
    marker = tmp_path / "marker.yaml"
    script = tmp_path / "admin.sh"
    script.write_text(script_text)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "MARKER_CAPTURE": str(marker),
        "MARKER_STDIN": str(marker),
        "RECORD_DIR": str(record),
    }
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=15)
    return result, record


def test_manifest_temp_file_is_created_0600_and_applied(tmp_path):
    generated = _render(_plan(), ["gateway.manifests"])

    result, record = _run_admin_script(tmp_path, generated.script)

    assert result.returncode == 0, result.stderr
    assert (record / "exists").read_text() == "yes"
    assert (record / "mode").read_text().strip() == "600"
    assert "kind: GatewayClass" in (record / "content").read_text()
    assert not Path((record / "path").read_text()).exists()


def test_helm_stdin_temp_file_is_created_0600_and_passed_to_helm(tmp_path):
    generated = _render(
        _plan(accelerators=[{"vendor": "nvidia", "mode": "plugin", "rbac": ["escalation"]}]),
        ["accelerator.nvidia.plugin.0"],
    )

    result, record = _run_admin_script(tmp_path, generated.script)

    assert result.returncode == 0, result.stderr
    assert (record / "exists").read_text() == "yes"
    assert (record / "mode").read_text().strip() == "600"
    assert "nfd:" in (record / "content").read_text()
    assert not Path((record / "path").read_text()).exists()


def test_chart_source_downloads_verifies_caches_and_embed_crds(tmp_path, monkeypatch):
    archive = _chart_archive(
        tmp_path,
        files={"lws/crds/leaderworkersets.yaml": "kind: CustomResourceDefinition\nmetadata:\n  name: lws\n"},
    )
    _patch_helm_pull(monkeypatch, archive)
    source = ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="v1.0.0")

    first = resolve_chart_crds(source, cache_root=tmp_path / "cache")
    digest = first.digest
    assert digest.startswith("sha256:")
    assert "kind: CustomResourceDefinition" in first.yaml

    def fail_pull(_source, _destination):
        raise AssertionError("cached chart must not be downloaded again")

    monkeypatch.setattr("llm_d_lens.cluster.remediation._helm_pull", fail_pull)
    second = resolve_chart_crds(source, cache_root=tmp_path / "cache")
    assert second == first


def test_crd_only_rejects_non_crd_and_list(tmp_path, monkeypatch):
    plan = _plan(stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False})
    non_crd = ResolvedChartCRDs(
        yaml="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: not-a-crd\n",
        digest="sha256:" + "b" * 64,
    )
    with pytest.raises(AdminScriptSourceUnavailableError):
        _render(plan, ["lws.namespace", "lws.crds"], resolver=lambda _source: non_crd)

    listed = ResolvedChartCRDs(
        yaml=(
            "apiVersion: v1\nkind: List\nitems:\n"
            "  - apiVersion: apiextensions.k8s.io/v1\n"
            "    kind: CustomResourceDefinition\n    metadata:\n      name: nested\n"
        ),
        digest="sha256:" + "c" * 64,
    )
    with pytest.raises(AdminScriptSourceUnavailableError):
        _render(plan, ["lws.namespace", "lws.crds"], resolver=lambda _source: listed)


def test_normal_manifest_list_recurses_and_rejects_forbidden_items():
    operation = _custom_operation(
        "list.manifests",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {
                    "apiVersion": "v1",
                    "kind": "List",
                    "items": [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "token"}}],
                },
            ),
        ),
    )
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[operation],
            admin_operation_ids=[operation.id],
        )


def test_admin_dependency_on_non_admin_is_rejected():
    user = _custom_operation("user.op", apply=OperationApply(kind="command", argv=("true",)))
    admin = _custom_operation(
        "admin.op",
        apply=OperationApply(kind="command", argv=("true",)),
        depends_on=("user.op",),
    )
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[user, admin],
            admin_operation_ids=["admin.op"],
        )


def test_chart_yaml_delimiter_cannot_escape_base64_block():
    from llm_d_lens.cluster.remediation import _heredoc_name

    malicious = "LENS_BASE64_86A1A717F12F"
    plan = _plan(stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False})
    generated = _render(
        plan,
        ["lws.namespace", "lws.crds"],
        resolver=lambda _source: ResolvedChartCRDs(
            yaml=(
                "apiVersion: apiextensions.k8s.io/v1\n"
                "kind: CustomResourceDefinition\n"
                "metadata:\n  name: widgets.example.io\n"
                "  annotations:\n    description: |\n"
                f"      {malicious}\n"
            ),
            digest="sha256:" + "d" * 64,
        ),
    )

    delimiter = _heredoc_name("LENS_BASE64", "lws.crds")
    assert generated.script.count(delimiter) == 2
    assert malicious not in generated.script


def test_source_parse_error_is_fail_closed():
    plan = _plan(stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False})
    with pytest.raises(AdminScriptSourceUnavailableError):
        _render(
            plan,
            ["lws.namespace", "lws.crds"],
            resolver=lambda _source: ResolvedChartCRDs(
                yaml="not: [valid",
                digest="sha256:" + "e" * 64,
            ),
        )


def test_node_label_timeout_writes_partial_marker(tmp_path):
    operation = _custom_operation(
        "wait.node",
        apply=OperationApply(
            kind="command",
            argv=("true",),
            wait=(OperationWait(kind="node-label", target="example.io/gpu=true", timeout_seconds=1),),
        ),
    )
    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=_plan(),
        operations=[operation],
        admin_operation_ids=[operation.id],
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "marker.yaml"
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "apply" ]]; then cat >> "$MARKER_CAPTURE"; echo >> "$MARKER_CAPTURE"; exit 0; fi\n'
        'if [[ "$1" == "get" ]]; then exit 0; fi\n'
        "exit 0\n"
    )
    kubectl.chmod(0o755)
    sleep = bindir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\n/bin/sleep 0.2\n")
    sleep.chmod(0o755)
    script = tmp_path / "admin.sh"
    script.write_text(generated.script)
    env = {
        **__import__("os").environ,
        "PATH": f"{bindir}:{__import__('os').environ['PATH']}",
        "MARKER_CAPTURE": str(marker),
    }
    result = __import__("subprocess").run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=10)

    assert result.returncode != 0
    assert "Timed out waiting for the node label" in result.stderr
    assert 'status: "partial"' in marker.read_text()
    assert 'status: "complete"' not in marker.read_text()


def test_chart_pull_oserror_fail_closed(tmp_path, monkeypatch):
    def fail_pull(_source, _destination):
        raise FileNotFoundError("helm unavailable")

    monkeypatch.setattr("llm_d_lens.cluster.remediation._helm_pull", fail_pull)
    with pytest.raises(AdminScriptSourceUnavailableError):
        resolve_chart_crds(
            ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="v1.0.3"),
            cache_root=tmp_path / "cache",
        )


def test_chart_crds_invalid_utf8_fail_closed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    crd = source / "lws" / "crds" / "bad.yaml"
    crd.parent.mkdir(parents=True)
    crd.write_bytes(b"\xff\xfe\x00bad")
    archive = tmp_path / "bad.tgz"
    with tarfile.open(archive, "w:gz") as handle:
        for path in sorted(source.rglob("*")):
            handle.add(path, arcname=str(path.relative_to(source)))
    _patch_helm_pull(monkeypatch, archive)

    with pytest.raises(AdminScriptSourceUnavailableError):
        resolve_chart_crds(
            ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="v1.0.2"),
            cache_root=tmp_path / "cache",
        )


def test_chart_source_digest_mismatch_and_missing_subdir_fail_closed(tmp_path, monkeypatch):
    archive = _chart_archive(
        tmp_path,
        files={"lws/crds/leaderworkersets.yaml": "kind: CustomResourceDefinition\nmetadata:\n  name: lws\n"},
    )
    _patch_helm_pull(monkeypatch, archive)
    source = ChartCRDSource(
        oci_ref="oci://registry.k8s.io/lws/charts/lws",
        version="v1.0.0",
        digest="sha256:" + "0" * 64,
    )
    with pytest.raises(AdminScriptSourceUnavailableError):
        resolve_chart_crds(source, cache_root=tmp_path / "cache")

    empty_base = tmp_path / "empty"
    empty_base.mkdir()
    empty_archive = _chart_archive(empty_base, files={"lws/Chart.yaml": "name: lws\n"})
    _patch_helm_pull(monkeypatch, empty_archive)
    with pytest.raises(AdminScriptSourceUnavailableError):
        resolve_chart_crds(
            ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="v1.0.1"),
            cache_root=tmp_path / "cache-2",
        )


def test_script_has_no_handoff_identity_or_user_secret():
    plan = _plan(accelerators=[{"vendor": "nvidia", "mode": "plugin", "rbac": ["escalation"]}])
    operations = _operations(plan)
    admin_ids = [
        operation.id for operation in operations if operation.id in {"gateway.manifests", "accelerator.nvidia.plugin.0"}
    ]

    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=plan,
        operations=operations,
        admin_operation_ids=admin_ids,
    )

    assert "cache.hf-secret" not in generated.script
    assert "HF_TOKEN" not in generated.script
    assert "kind: ServiceAccount" not in generated.script
    assert "kind: ClusterRole" not in generated.script
    assert "kind: ClusterRoleBinding" not in generated.script
    assert "KUBECONFIG" not in generated.script


def test_istio_gateway_wait_is_300s_and_falls_back_to_nodeport():
    plan = _plan(
        gateway={
            "provider": "istio",
            "namespace": "lens-gateway",
            "name": "lens-gateway",
        }
    )
    generated = _render(plan, ["gateway.crds", "gateway.provider.0", "gateway.manifests"])

    script = generated.script
    assert "kubectl wait --for=condition=Programmed gateway/lens-gateway" in script
    assert "--timeout=300s" in script
    assert 'reason="$(kubectl get gateway/lens-gateway' in script
    assert '"AddressNotAssigned"' in script
    assert "retrying with NodePort" in script
    assert "kind: ConfigMap" in script
    assert "type: NodePort" in script
    assert "kubectl patch gateway/lens-gateway" in script
    assert script.count("kubectl wait --for=condition=Programmed gateway/lens-gateway") == 2
    # A failed fallback still leaves partial state for a safe rerun.
    assert "trap 'write_marker partial || true' ERR" in script


def test_cleanup_marker_name_dns1123_and_long_id_fallback():
    assert cleanup_marker_name("1234abcd") == "lens-admin-cleanup-1234abcd"
    long_or_upper = cleanup_marker_name("Cluster_Upper_" + "x" * 50)
    assert long_or_upper.startswith("lens-admin-cleanup-")
    assert len(long_or_upper) <= 63
    assert long_or_upper == long_or_upper.lower()


def test_cleanup_digest_deterministic_over_required_ops_only():
    op_a = CleanupOperation(
        id="gateway.namespace",
        title="Delete gw",
        kind="namespace",
        name="lens-gateway-1234abcd",
        required=True,
    )
    op_b = CleanupOperation(
        id="hf-secret.secret",
        title="Delete secret",
        kind="secret",
        name="hf-token",
        namespace="lens-edit",
        required=True,
    )
    op_opt = CleanupOperation(
        id="handoff.marker",
        title="Delete handoff marker",
        kind="configmap",
        name="lens-admin-handoff-1234",
        namespace="lens-edit",
        required=False,
    )

    d1 = cleanup_digest("1234abcd", [op_a, op_b, op_opt])
    d2 = cleanup_digest("1234abcd", [op_b, op_a])
    assert d1 == d2
    assert len(d1) == 64
    assert d1 != cleanup_digest("different", [op_a, op_b])
    assert d1 != cleanup_digest("1234abcd", [op_a])


def test_render_admin_cleanup_script_orders_namespaced_before_namespace_and_writes_marker_last(tmp_path):
    ops = [
        CleanupOperation(
            id="gateway.namespace",
            title="Delete Gateway namespace",
            kind="namespace",
            name="lens-gateway-1234abcd",
            required=True,
        ),
        CleanupOperation(
            id="gateway.resource",
            title="Delete shared Gateway",
            kind="gateway.gateway.networking.k8s.io",
            name="lens-inference-gateway",
            namespace="shared-gw",
            required=True,
        ),
        CleanupOperation(
            id="inotify.daemonset",
            title="Delete sysctl DaemonSet",
            kind="daemonset.apps",
            name="lens-node-sysctl",
            namespace="shared-gw",
            required=False,
        ),
        CleanupOperation(
            id="handoff.marker",
            title="Delete provisioning handoff marker",
            kind="configmap",
            name="lens-admin-handoff-abcd",
            namespace="lens-edit",
            required=False,
        ),
    ]
    rendered = render_admin_cleanup_script(
        cluster_id="1234abcd",
        operations=ops,
        marker_namespace="lens-edit",
        protected_namespaces=("lens-edit",),
    )

    script = rendered.script
    assert rendered.filename == "lens-admin-cleanup-1234abcd.sh"
    assert rendered.can_verify_marker is True
    assert "set -euo pipefail" in script
    assert "umask 077" in script
    assert "--ignore-not-found=true" in script
    assert "if kubectl get crd gateways.gateway.networking.k8s.io >/dev/null 2>&1; then" in script
    assert script.index("kubectl delete gateway.gateway.networking.k8s.io") < script.index(
        "kubectl delete namespace lens-gateway-1234abcd"
    )
    assert script.index("kubectl delete daemonset.apps lens-node-sysctl") < script.index(
        "kubectl delete namespace lens-gateway-1234abcd"
    )
    assert script.rindex("write_marker completed") > script.rindex("kubectl delete namespace lens-gateway-1234abcd")

    # Execute the rendered script against a fake kubectl binary: CRD missing
    # skips the CRD delete without failing, and marker ConfigMap is written last.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "kubectl.log"
    marker = tmp_path / "marker.yaml"
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
        'if [[ "$1" == "get" && "$2" == "crd" ]]; then exit 1; fi\n'
        'if [[ "$1" == "apply" ]]; then cat > "$MARKER_OUT"; fi\n'
        "exit 0\n"
    )
    kubectl.chmod(0o755)
    script_path = tmp_path / rendered.filename
    script_path.write_text(script)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "KUBECTL_LOG": str(log),
        "MARKER_OUT": str(marker),
    }
    res = subprocess.run(["bash", str(script_path)], env=env, capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr
    calls = log.read_text().splitlines()
    assert not any("gateway.gateway.networking.k8s.io" in line and line.startswith("delete") for line in calls)
    assert any("delete namespace lens-gateway-1234abcd --ignore-not-found=true" in line for line in calls)
    marker_text = marker.read_text()
    assert 'clusterId: "1234abcd"' in marker_text
    assert f'cleanupDigest: "{rendered.cleanup_digest}"' in marker_text
    assert 'status: "completed"' in marker_text
    assert "gateway.namespace" in marker_text


@pytest.mark.parametrize(
    "blocked_ns",
    [
        *sorted(PROTECTED_NAMESPACES),
        "lens-gateway",
        "lens-monitoring",
        "llm-d-bench-storage",
        "lens-storage",
        "lens-edit",
    ],
)
def test_render_admin_cleanup_script_rejects_protected_and_marker_namespaces(blocked_ns):
    op = CleanupOperation(
        id="bad.ns",
        title="Delete protected namespace",
        kind="namespace",
        name=blocked_ns,
        required=True,
    )
    with pytest.raises(AdminScriptValidationError):
        render_admin_cleanup_script(
            cluster_id="1234abcd",
            operations=[op],
            marker_namespace="lens-edit",
        )


def test_render_admin_cleanup_script_allows_cluster_scoped_lens_monitoring_namespace():
    rendered = render_admin_cleanup_script(
        cluster_id="1234abcd",
        operations=[
            CleanupOperation(
                id="monitoring.namespace",
                title="Delete cluster monitoring namespace",
                kind="namespace",
                name="lens-monitoring-1234abcd",
                required=True,
            )
        ],
        marker_namespace="lens-edit",
    )
    assert "kubectl delete namespace lens-monitoring-1234abcd --ignore-not-found=true" in rendered.script


def test_render_admin_cleanup_script_rejects_injection_invalid_names_and_unsupported_kinds():
    bad_ops = [
        CleanupOperation(id="bad.kind", title="Bad", kind="deployment", name="foo", namespace="ns"),
        CleanupOperation(id="bad.inj", title="Bad", kind="secret", name="foo; $(id)", namespace="ns"),
        CleanupOperation(id="bad.nl", title="Bad", kind="secret", name="foo\nbar", namespace="ns"),
        CleanupOperation(id="bad.long", title="Bad", kind="namespace", name="a" * 64),
        CleanupOperation(id="bad.ns", title="Bad", kind="namespace", name="valid-ns", namespace="extra"),
        CleanupOperation(id="bad.no_ns", title="Bad", kind="secret", name="foo", namespace=None),
    ]
    for op in bad_ops:
        with pytest.raises(AdminScriptValidationError):
            render_admin_cleanup_script(cluster_id="1234abcd", operations=[op], marker_namespace="lens-edit")


def test_render_admin_cleanup_script_without_marker_namespace_emits_warning_and_omits_marker():
    op = CleanupOperation(
        id="gateway.namespace",
        title="Delete Gateway namespace",
        kind="namespace",
        name="lens-gateway-1234abcd",
        required=True,
    )
    rendered = render_admin_cleanup_script(
        cluster_id="1234abcd",
        operations=[op],
        marker_namespace=None,
    )
    assert rendered.can_verify_marker is False
    assert rendered.marker == {"namespace": None, "name": "lens-admin-cleanup-1234abcd"}
    assert rendered.warnings
    assert "write_marker" not in rendered.script
    assert "MARKER_YAML" not in rendered.script


def test_cleanup_marker_label_value_handles_long_and_trailing_punctuation_cluster_ids():
    assert cleanup_cluster_label_value("1234abcd") == "1234abcd"
    for bad_id in ("a" * 65, "cluster-trailing-"):
        label = cleanup_cluster_label_value(bad_id)
        assert len(label) == 16
        assert label.isalnum()
        rendered = render_admin_cleanup_script(
            cluster_id=bad_id,
            operations=[
                CleanupOperation(
                    id="gateway.namespace",
                    title="Delete gw ns",
                    kind="namespace",
                    name="lens-gateway-abc",
                    required=True,
                )
            ],
            marker_namespace="lens-edit",
        )
        assert f"CLUSTER_ID={bad_id}" in rendered.script
        assert f"CLUSTER_LABEL={label}" in rendered.script
        assert 'lens.llm-d.ai/cleanup-cluster: "$CLUSTER_LABEL"' in rendered.script
        assert 'clusterId: "$CLUSTER_ID"' in rendered.script


def test_render_admin_script_includes_gateway_authz_without_requiring_gateway_manifests_as_admin(monkeypatch):
    import base64
    import re

    from llm_d_lens.cluster.remediation import _heredoc_name
    from llm_d_lens.infrastructure.network import hostinfo

    monkeypatch.setattr(hostinfo, "current_serving_port", lambda: 8084)
    monkeypatch.delenv("LENS_MODEL_GATEWAY_AUTHZ_TLS_INSECURE", raising=False)
    plan = _plan(
        gateway={
            "provider": "istio",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
            "authzHost": "10.0.0.5",
        }
    )
    # gateway.manifests is intentionally omitted from admin_operation_ids (simulating user-owned)
    # while gateway.authz is admin-owned and depends on gateway.namespace, gateway.crds, gateway.provider.0.
    generated = _render(
        plan,
        ["gateway.namespace", "gateway.crds", "gateway.provider.0", "gateway.authz"],
    )

    assert "gateway.authz" in generated.admin_operations
    assert "append_done gateway.authz" in generated.script
    assert "Apply the Gateway ext_authz resources" in generated.script
    delimiter = _heredoc_name("LENS_BASE64", "gateway.authz")
    match = re.search(rf"<<'{delimiter}'\n([A-Za-z0-9+/=\n]+)\n{delimiter}", generated.script)
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode("utf-8")
    assert "kind: EnvoyFilter" in decoded
    assert "kind: Service" in decoded
    assert "kind: Endpoints" in decoded


def test_command_bash_s_uses_single_quoted_heredoc_and_falls_back_on_delimiter_or_cr(tmp_path):
    from llm_d_lens.cluster.remediation import _heredoc_name, _render_command

    op_id = "monitoring.stack"
    delimiter = _heredoc_name("LENS_SCRIPT", op_id)
    script_body = 'set -euo pipefail\necho "$HOME" literal\n'
    rendered = _render_command(
        OperationApply(kind="command", argv=("bash", "-s"), stdin=script_body),
        op_id,
    )
    rendered_text = "\n".join(rendered)
    assert f"bash -s <<'{delimiter}'" in rendered_text
    assert "base64 -d" not in rendered_text

    # Single-quoted heredoc executes safely without expanding $HOME inside stdin.
    capture = tmp_path / "out.txt"
    proc = subprocess.run(
        ["bash", "-c", rendered_text.replace("bash -s", f"cat > {capture}")],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert proc.returncode == 0
    assert "$HOME" in capture.read_text()

    # Fallback to base64 if delimiter appears as a line or stdin contains CR.
    for unsafe_stdin in (f"echo ok\n{delimiter}\necho done\n", "echo ok\r\n"):
        fallback = "\n".join(
            _render_command(
                OperationApply(kind="command", argv=("bash", "-s"), stdin=unsafe_stdin),
                op_id,
            )
        )
        assert "base64 -d >" in fallback
        assert f"<<'{delimiter}'" not in fallback


def test_monitoring_stack_appends_nodeport_discovery_and_populates_marker(monkeypatch, tmp_path):
    from llm_d_lens.monitoring.cluster_stack import service as cluster_stack_service

    seen_calls: list[dict] = []

    def fake_snippet(*, monitoring_namespace: str = "llm-d-monitoring", emit_marker_vars: bool = True) -> list[str]:
        seen_calls.append({"monitoring_namespace": monitoring_namespace, "emit_marker_vars": emit_marker_vars})
        return [
            'MONITORING_PROMETHEUS_NODE_PORT="30900"',
            'MONITORING_GRAFANA_NODE_PORT="30300"',
            'MONITORING_HOST="10.0.0.42"',
        ]

    monkeypatch.setattr(cluster_stack_service, "render_nodeport_discovery_snippet", fake_snippet, raising=False)
    plan = _plan(monitoring={"mode": "managed", "namespace": "obs-mon"})
    mon_op = _custom_operation(
        "monitoring.stack",
        apply=OperationApply(
            kind="command",
            argv=("bash", "-s"),
            stdin="echo installing monitoring\n",
            namespace="obs-mon",
        ),
    )

    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=plan,
        operations=[mon_op],
        admin_operation_ids=["monitoring.stack"],
    )

    assert seen_calls == [{"monitoring_namespace": "obs-mon", "emit_marker_vars": True}]
    assert generated.script.rindex("write_marker complete") > generated.script.rindex(
        'MONITORING_PROMETHEUS_NODE_PORT="30900"'
    )
    assert generated.script.rindex('MONITORING_PROMETHEUS_NODE_PORT="30900"') > generated.script.rindex(
        "append_done monitoring.stack"
    )

    result, _record = _run_admin_script(tmp_path, generated.script)
    assert result.returncode == 0, result.stderr
    marker_text = (tmp_path / "marker.yaml").read_text()
    assert 'prometheusNodePort: "30900"' in marker_text
    assert 'grafanaNodePort: "30300"' in marker_text
    assert 'monitoringHost: "10.0.0.42"' in marker_text


def test_render_admin_cleanup_script_orders_helm_rbac_and_namespace_and_validates_cluster_scoped(tmp_path):
    ops = [
        CleanupOperation(
            id="monitoring.namespace",
            title="Delete monitoring namespace",
            kind="namespace",
            name="llm-d-monitoring",
            required=True,
        ),
        CleanupOperation(
            id="monitoring.rbac.role",
            title="Delete ClusterRole",
            kind="clusterrole",
            name="llm-d-prism-metrics-reader",
            required=True,
        ),
        CleanupOperation(
            id="monitoring.rbac.binding",
            title="Delete ClusterRoleBinding",
            kind="clusterrolebinding",
            name="llm-d-prism-metrics-reader",
            required=True,
        ),
        CleanupOperation(
            id="monitoring.release.llmd",
            title="Uninstall llmd release",
            kind="helm_release",
            name="llmd",
            namespace="llm-d-monitoring",
            required=True,
        ),
    ]
    rendered = render_admin_cleanup_script(
        cluster_id="1234abcd",
        operations=ops,
        marker_namespace="lens-edit",
    )
    script = rendered.script
    assert [op.kind for op in rendered.operations] == [
        "helm_release",
        "clusterrole",
        "clusterrolebinding",
        "namespace",
    ]
    assert (
        "if command -v helm >/dev/null 2>&1 && helm status llmd --namespace llm-d-monitoring >/dev/null 2>&1; then"
        in script
    )
    assert "helm uninstall llmd --namespace llm-d-monitoring" in script
    assert "--ignore-not-found" not in script.split("helm uninstall")[1].splitlines()[0]
    assert script.index("helm uninstall llmd --namespace llm-d-monitoring") < script.index(
        "kubectl delete clusterrolebinding llm-d-prism-metrics-reader --ignore-not-found=true"
    )
    assert script.index("kubectl delete clusterrole llm-d-prism-metrics-reader --ignore-not-found=true") < script.index(
        "kubectl delete namespace llm-d-monitoring --ignore-not-found=true"
    )

    with pytest.raises(AdminScriptValidationError):
        render_admin_cleanup_script(
            cluster_id="1234abcd",
            operations=[
                CleanupOperation(
                    id="bad.cr",
                    title="Bad ClusterRole",
                    kind="clusterrole",
                    name="llm-d-prism-metrics-reader",
                    namespace="default",
                )
            ],
            marker_namespace="lens-edit",
        )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "cmd.log"
    marker = tmp_path / "marker.yaml"
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'printf "kubectl %s\\n" "$*" >> "$CMD_LOG"\n'
        'if [[ "$1" == "apply" ]]; then cat > "$MARKER_CAPTURE"; fi\n'
        "exit 0\n"
    )
    kubectl.chmod(0o755)
    helm = bindir / "helm"
    helm.write_text('#!/usr/bin/env bash\nset -e\nprintf "helm %s\\n" "$*" >> "$CMD_LOG"\nexit 0\n')
    helm.chmod(0o755)
    script_path = tmp_path / "cleanup.sh"
    script_path.write_text(script)
    proc = subprocess.run(
        ["bash", str(script_path)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "CMD_LOG": str(log),
            "MARKER_CAPTURE": str(marker),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    cmds = log.read_text().splitlines()
    assert cmds[0] == "helm status llmd --namespace llm-d-monitoring"
    assert cmds[1] == "helm uninstall llmd --namespace llm-d-monitoring"
    assert "kubectl delete clusterrolebinding llm-d-prism-metrics-reader --ignore-not-found=true" in cmds
    assert "kubectl delete clusterrole llm-d-prism-metrics-reader --ignore-not-found=true" in cmds
    assert "kubectl delete namespace llm-d-monitoring --ignore-not-found=true" in cmds


def test_completed_operation_ids_allows_completed_upstream_and_rejects_uncompleted_or_out_of_order():
    plan = _plan(stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False})
    operations = _operations(plan)

    # Without completed_operation_ids, lws.crds alone fails because lws.namespace is not in admin_operation_ids.
    with pytest.raises(AdminScriptValidationError, match="depends on non-admin operation 'lws.namespace'"):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=plan,
            operations=operations,
            admin_operation_ids=["lws.crds"],
            resolver=_fake_resolver,
        )

    # When lws.namespace is already completed (e.g. user_done before runtime 403 on lws.crds),
    # rendering lws.crds alone succeeds and omits lws.namespace from the script.
    generated = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=plan,
        operations=operations,
        admin_operation_ids=["lws.crds"],
        completed_operation_ids=("lws.namespace",),
        resolver=_fake_resolver,
    )
    assert generated.admin_operations == ("lws.crds",)
    assert "# operation: lws.crds" in generated.script
    assert "# operation: lws.namespace" not in generated.script

    # Unrelated completed_operation_ids do NOT satisfy an uncompleted non-admin dependency.
    with pytest.raises(AdminScriptValidationError, match="depends on non-admin operation 'lws.namespace'"):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=plan,
            operations=operations,
            admin_operation_ids=["lws.crds"],
            completed_operation_ids=("gateway.manifests",),
            resolver=_fake_resolver,
        )

    # Out-of-order admin dependencies are still rejected even if listed in completed_operation_ids.
    first = _custom_operation("first", apply=OperationApply(kind="command", argv=("true",)))
    second = _custom_operation(
        "second",
        apply=OperationApply(kind="command", argv=("true",)),
        depends_on=("first",),
    )
    with pytest.raises(AdminScriptValidationError, match="ordered before dependency 'first'"):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=_plan(),
            operations=[second, first],
            admin_operation_ids=["first", "second"],
            completed_operation_ids=["first"],
        )


def test_validate_crd_yaml_and_validate_field_manager_enforce_strict_contracts():
    valid_crd_yaml = (
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\n"
        "metadata:\n"
        "  name: leaderworkersets.leaderworkerset.x-k8s.io\n"
        "---\n"
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\n"
        "metadata:\n"
        "  name: disaggregatedsets.disaggregatedset.x-k8s.io\n"
    )
    validate_crd_yaml(valid_crd_yaml, "lws.crds")

    for bad_yaml in (
        "",
        "   \n---\n",
        "not: [valid",
        "- item1\n- item2\n",
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\n",
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: sec\n",
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: cr\n",
        "apiVersion: apiextensions.k8s.io/v1beta1\nkind: CustomResourceDefinition\nmetadata:\n  name: old.io\n",
        (
            "apiVersion: v1\nkind: List\nitems:\n"
            "  - apiVersion: apiextensions.k8s.io/v1\n"
            "    kind: CustomResourceDefinition\n"
            "    metadata:\n      name: nested.io\n"
        ),
    ):
        with pytest.raises(AdminScriptValidationError):
            validate_crd_yaml(bad_yaml, "lws.crds")

    assert validate_field_manager("lens") == "lens"
    assert validate_field_manager("lens.crd-manager_1") == "lens.crd-manager_1"

    for bad_manager in (
        "",
        "   ",
        "--kubeconfig=/tmp/evil",
        "-f",
        "lens manager",
        "lens=evil",
        "lens/evil",
        "lens\nevil",
        "lens\x00evil",
        "a" * 129,
    ):
        with pytest.raises(AdminScriptValidationError):
            validate_field_manager(bad_manager)

    for bad_source in (
        ChartCRDSource(oci_ref="--post-renderer=/bin/sh", version="1.0.0"),
        ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="--help"),
        ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="1.0.0", subdir="../outside"),
        ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="1.0.0", subdir="/etc"),
    ):
        with pytest.raises(AdminScriptSourceUnavailableError):
            resolve_chart_crds(bad_source)


def test_render_admin_cleanup_script_allows_cluster_storage_namespace_and_role_kinds():
    ops = [
        CleanupOperation(
            id="storage.namespace",
            title="Delete cluster storage namespace",
            kind="namespace",
            name="lens-storage-283db094",
            required=True,
        ),
        CleanupOperation(
            id="storage.rolebinding",
            title="Delete storage operator RoleBinding",
            kind="rolebinding",
            name="lens-storage-operator",
            namespace="lens-storage-283db094",
            required=True,
        ),
        CleanupOperation(
            id="storage.role",
            title="Delete storage operator Role",
            kind="role",
            name="lens-storage-operator",
            namespace="lens-storage-283db094",
            required=True,
        ),
    ]
    rendered = render_admin_cleanup_script(
        cluster_id="283db094",
        operations=ops,
        marker_namespace="team-inference",
    )
    script = rendered.script
    idx_rb = script.index("kubectl delete rolebinding lens-storage-operator --namespace lens-storage-283db094")
    idx_role = script.index("kubectl delete role lens-storage-operator --namespace lens-storage-283db094")
    idx_ns = script.index("kubectl delete namespace lens-storage-283db094 --ignore-not-found=true")
    assert idx_rb < idx_ns
    assert idx_role < idx_ns


def test_render_admin_operations_script_emits_storage_rbac_for_sa_user_group_and_unknown(tmp_path):
    import yaml

    from llm_d_lens.cluster.operations import build_operations

    plan = _plan(
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        handoff={"namespace": "team-inference"},
    )
    storage_ns = f"lens-storage-{CLUSTER_ID}"
    assert plan.storage.namespace == storage_ns
    operations = build_operations(plan, cluster_id=CLUSTER_ID)

    # 1. ServiceAccount subject with storage.namespace in admin_operation_ids
    sa_rendered = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=plan,
        operations=operations,
        admin_operation_ids=("storage.namespace", "storage.pv", "storage.pvc"),
        storage_rbac_namespace=storage_ns,
        storage_rbac_subject={"kind": "ServiceAccount", "name": "runner-sa", "namespace": "team-inference"},
    )
    assert sa_rendered.warnings == ()
    assert sa_rendered.admin_operations == ("storage.namespace", "storage.pv", "storage.pvc")
    ns_done_idx = sa_rendered.script.index("append_done storage.namespace")
    rbac_idx = sa_rendered.script.index(f"# storage RBAC: lens-storage-operator in {storage_ns}")
    pvc_idx = sa_rendered.script.index("# operation: storage.pvc")
    assert ns_done_idx < rbac_idx < pvc_idx
    assert "ClusterRole" not in sa_rendered.script

    # Execute against fake kubectl to verify decoded YAML manifests
    bindir = tmp_path / "bin"
    bindir.mkdir()
    applied_dir = tmp_path / "applied"
    applied_dir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'if [[ "$1" == "get" && "$2" == "namespace" ]]; then exit 1; fi\n'
        'if [[ "$1" == "apply" ]]; then\n'
        '  file=""\n'
        "  while (($#)); do\n"
        '    if [[ "$1" == "-f" ]]; then file="$2"; shift 2; continue; fi\n'
        "    shift\n"
        "  done\n"
        '  if [[ "$file" == "-" ]]; then cat > "$APPLIED_DIR/marker.yaml";\n'
        '  elif [[ -n "$file" ]]; then cp "$file" "$APPLIED_DIR/$(basename "$file")"; fi\n'
        "fi\n"
        "exit 0\n"
    )
    kubectl.chmod(0o755)
    script_path = tmp_path / sa_rendered.filename
    script_path.write_text(sa_rendered.script)
    res = subprocess.run(
        ["bash", str(script_path)],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "APPLIED_DIR": str(applied_dir)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    docs = []
    for yaml_file in sorted(applied_dir.glob("manifests-*.yaml")):
        docs.extend(doc for doc in yaml.safe_load_all(yaml_file.read_text()) if doc)
    kinds = [(doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace")) for doc in docs]
    assert ("Role", "lens-storage-operator", storage_ns) in kinds
    assert ("RoleBinding", "lens-storage-operator", storage_ns) in kinds
    rb_doc = next(doc for doc in docs if doc["kind"] == "RoleBinding")
    assert rb_doc["subjects"] == [{"kind": "ServiceAccount", "name": "runner-sa", "namespace": "team-inference"}]

    # 2. Existing namespace (no storage.namespace op) + User / Group subjects
    existing_plan = _plan(
        storage={
            "kind": "local-disk",
            "namespace": "team-storage",
            "namespaceMode": "existing",
            "hostPath": "/data/models",
            "capacity": "50Gi",
        },
        handoff={"namespace": "team-inference"},
    )
    existing_ops = build_operations(existing_plan, cluster_id=CLUSTER_ID)
    for subj in (
        {"kind": "User", "name": "alice@example.com"},
        {"kind": "Group", "name": "oidc:ml-platform"},
    ):
        ug_rendered = render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=existing_plan,
            operations=existing_ops,
            admin_operation_ids=("storage.pv", "storage.pvc"),
            storage_rbac_namespace="team-storage",
            storage_rbac_subject=subj,
        )
        assert (
            "kubectl get namespace -- team-storage >/dev/null 2>&1 || kubectl create namespace -- team-storage"
            in ug_rendered.script
        )
        assert ug_rendered.script.index("# storage RBAC: lens-storage-operator in team-storage") < (
            ug_rendered.script.index("# operation: storage.pvc")
        )

    # 3. Unknown subject emits Role only + commented rolebinding guidance + warning
    unknown_rendered = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=plan,
        operations=operations,
        admin_operation_ids=("storage.namespace", "storage.pv", "storage.pvc"),
        storage_rbac_namespace=storage_ns,
        storage_rbac_subject={"kind": "unknown", "overrideRequired": True},
    )
    assert any("lens-storage-operator" in w and "without a RoleBinding" in w for w in unknown_rendered.warnings)
    assert (
        f"# kubectl -n {storage_ns} create rolebinding lens-storage-operator --role=lens-storage-operator"
        in unknown_rendered.script
    )

    # 4. Invalid namespace or subject fails closed with AdminScriptValidationError
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=plan,
            operations=operations,
            admin_operation_ids=("storage.namespace", "storage.pv", "storage.pvc"),
            storage_rbac_namespace="Bad_NS",
            storage_rbac_subject={"kind": "User", "name": "alice"},
        )
    with pytest.raises(AdminScriptValidationError):
        render_admin_operations_script(
            cluster_id=CLUSTER_ID,
            plan=plan,
            operations=operations,
            admin_operation_ids=("storage.namespace", "storage.pv", "storage.pvc"),
            storage_rbac_namespace=storage_ns,
            storage_rbac_subject={"kind": "User", "name": "bad; $(id)"},
        )


@pytest.mark.parametrize(
    ("provider", "expected_chart_provider", "admin_ids"),
    [
        (
            "istio",
            "istio",
            ["gateway.namespace", "gateway.crds", "gateway.provider.0", "gateway.manifests", "gateway.ipp"],
        ),
        (
            "gke",
            "gke",
            ["gateway.namespace", "gateway.manifests", "gateway.ipp"],
        ),
    ],
)
def test_gateway_ipp_renders_and_executes_in_admin_remediation_script(
    tmp_path,
    provider,
    expected_chart_provider,
    admin_ids,
):
    import yaml

    from llm_d_lens.cluster.remediation import _heredoc_name
    from llm_d_lens.versions import inference_payload_processor_chart, inference_payload_processor_version

    plan = _plan(
        gateway={
            "provider": provider,
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
        }
    )
    generated = _render(plan, admin_ids)

    script = generated.script
    delimiter = _heredoc_name("LENS_SCRIPT", "gateway.ipp")
    assert f"bash -s <<'{delimiter}'" in script
    ipp_section = script.split("# operation: gateway.ipp", 1)[1].split("append_done gateway.ipp", 1)[0]
    assert "--set" not in ipp_section
    assert "--create-namespace" not in ipp_section
    assert "kubectl rollout status deployment/lens-ipp --namespace lens-gateway --timeout=180s" in ipp_section
    assert "append_done gateway.ipp" in script
    assert script.index("# operation: gateway.manifests") < script.index("# operation: gateway.ipp")

    syntax = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr

    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    cmd_log = tmp_path / "commands.log"
    marker = tmp_path / "marker.yaml"

    istioctl = bindir / "istioctl"
    istioctl.write_text("#!/usr/bin/env bash\nexit 0\n")
    istioctl.chmod(0o755)

    kubectl = bindir / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'printf "kubectl %s\\n" "$*" >> "$CMD_LOG"\n'
        'if [[ "$1" == "apply" ]]; then\n'
        '  prev=""\n'
        '  for arg in "$@"; do\n'
        '    if [[ "$prev" == "-f" && "$arg" == "-" ]]; then cat > "$MARKER_OUT"; fi\n'
        '    prev="$arg"\n'
        "  done\n"
        "fi\n"
        "exit 0\n"
    )
    kubectl.chmod(0o755)

    helm = bindir / "helm"
    helm.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'printf "helm %s\\n" "$*" >> "$CMD_LOG"\n'
        'prev=""\n'
        'for arg in "$@"; do\n'
        '  if [[ "$prev" == "--values" ]]; then\n'
        '    stat -c "%a" "$arg" > "$RECORD_DIR/mode"\n'
        '    cp "$arg" "$RECORD_DIR/values.yaml"\n'
        '    printf "%s" "$arg" > "$RECORD_DIR/values_path"\n'
        "  fi\n"
        '  prev="$arg"\n'
        "done\n"
        "exit 0\n"
    )
    helm.chmod(0o755)

    script_path = tmp_path / "admin.sh"
    script_path.write_text(script)
    res = subprocess.run(
        ["bash", str(script_path)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "CMD_LOG": str(cmd_log),
            "MARKER_OUT": str(marker),
            "RECORD_DIR": str(record),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    assert (record / "mode").read_text().strip() == "600"
    assert yaml.safe_load((record / "values.yaml").read_text()) == {
        "payloadProcessor": {"name": "lens-ipp"},
        "provider": {"name": expected_chart_provider},
        "inferenceGateway": {"name": "lens-gateway"},
    }
    assert not Path((record / "values_path").read_text()).exists()

    cmds = cmd_log.read_text()
    assert (
        f"helm upgrade --install lens-ipp {inference_payload_processor_chart()} "
        f"--namespace lens-gateway --version {inference_payload_processor_version()} "
        "--values "
    ) in cmds
    assert "kubectl patch deploy lens-ipp -n lens-gateway --type=strategic -p " in cmds
    assert "kubectl rollout status deployment/lens-ipp -n lens-gateway --timeout=180s" in cmds or (
        "kubectl rollout status deployment/lens-ipp --namespace lens-gateway --timeout=180s" in cmds
    )
    assert "gateway.ipp" in marker.read_text()
