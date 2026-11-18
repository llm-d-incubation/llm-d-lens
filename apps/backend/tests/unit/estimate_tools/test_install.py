"""Provisioning is explicit, locked, verified and separate from request handling."""

import os
from pathlib import Path

import pytest
import yaml

from llm_d_lens.estimate_tools import service
from llm_d_lens.estimate_tools.blis import install
from llm_d_lens.estimate_tools.models import Problem


def test_missing_installation_is_not_provisioned(tmp_path):
    assert install.installed_profile(tmp_path / "absent") is None
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_existing_destination_is_never_overwritten(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    marker = root / "keep"
    marker.write_text("existing")
    with pytest.raises(ValueError, match="already exists"):
        await install.install_runtime(tmp_path / "binary", tmp_path / "inputs", root=root)
    assert marker.read_text() == "existing"


@pytest.mark.asyncio
async def test_mismatched_runtime_is_not_installed(tmp_path):
    binary = tmp_path / "blis"
    binary.write_text("not the pinned runtime")
    binary.chmod(0o700)
    with pytest.raises(ValueError, match="binary_digest_mismatch"):
        await install.install_runtime(binary, tmp_path / "inputs", root=tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()


def test_invalid_installation_record_fails_closed(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "installation.json").write_text('{"execution_verified": true}')
    with pytest.raises(ValueError, match="differs from its lock"):
        install.installed_profile(root)


def test_invalid_optional_install_does_not_block_aisimulate(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("LENS_BLIS_BINARY", raising=False)
    monkeypatch.delenv("LENS_BLIS_INPUTS", raising=False)
    monkeypatch.setattr(service, "_default", None)
    root = install.installation_root()
    root.mkdir(parents=True)
    (root / "installation.json").write_text("invalid")
    assert service.default_estimator().profiles == ()
    assert "Ignoring invalid optional managed BLIS" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("verification_failure", [False, True])
@pytest.mark.parametrize("complete_catalog", [False, True])
async def test_real_locked_installation_and_discovery(monkeypatch, tmp_path, verification_failure, complete_catalog):
    inputs_variable = "LENS_TEST_BLIS_COMPLETE_INPUTS" if complete_catalog else "LENS_TEST_BLIS_INPUTS"
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get(inputs_variable)
    if not binary or not inputs:
        pytest.skip("Set locked BLIS inputs for provisioning acceptance")
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("LENS_BLIS_BINARY", raising=False)
    monkeypatch.delenv("LENS_BLIS_INPUTS", raising=False)
    monkeypatch.setattr(service, "_default", None)
    if verification_failure:

        async def fail_verification(*args):
            return Problem(code="verification_rejected", message="Test verification failure")

        monkeypatch.setattr(install.service, "_execute_bundle", fail_verification)
        with pytest.raises(ValueError, match="verification_rejected"):
            await install.install_runtime(Path(binary), Path(inputs))
        assert not install.installation_root().exists()
        assert not list(install.installation_root().parent.glob(".blis-install-*"))
        return
    profile = await install.install_runtime(Path(binary), Path(inputs))
    assert profile == install.service.baseline_profile(
        binary=profile.binary, inputs=profile.inputs, complete_catalog=complete_catalog
    )
    assert install.installed_profile() == profile
    estimator = service.default_estimator()
    assert estimator.profiles == (profile,)
    support = await estimator.support(profile.verified_request)
    assert support[0].status == "supported"
    assert any(item.operation == "native_exact" and item.enabled for item in support[0].capabilities)
    assert not list(install.installation_root().parent.glob(".blis-install-*"))
    profile.binary.write_bytes(b"changed")
    assert not estimator.discover()[1].available


@pytest.mark.asyncio
async def test_complete_catalog_resolves_all_ids_and_sources(monkeypatch):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_COMPLETE_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set locked BLIS complete catalog inputs for mapping acceptance")
    monkeypatch.setenv("LENS_BLIS_BINARY", binary)
    monkeypatch.setenv("LENS_BLIS_INPUTS", inputs)
    monkeypatch.setattr(service, "_default", None)
    estimator = service.default_estimator()
    (profile,) = estimator.profiles
    assert profile.id == "blis-762f1605-full-catalog-b5550f75-experimental"
    assert install.service.availability(profile).available
    entries = sorted((profile.inputs / "catalog/models").iterdir())
    assert len(entries) == 42
    for entry in entries:
        metadata = yaml.safe_load((entry / "model.yaml").read_text())
        for alias in (entry.name, metadata["source"]["repo"]):
            request = profile.verified_request.model_copy(deep=True)
            request.model.name = alias.swapcase()
            request.model.precision = None
            request.model.quantization = None
            resolved = install.service._catalog_profile(request, profile)
            assert not isinstance(resolved, Problem), (alias, resolved)
            assert resolved.catalog_model == entry.name
    request.model.name = "Qwen/Qwen3-8B"
    (unsupported,) = await estimator.support(request)
    assert unsupported.reason.code == "model_mapping_missing"
