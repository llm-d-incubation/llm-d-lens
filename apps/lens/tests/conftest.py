import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.delenv("LLMD_STATE_DIR", raising=False)
    monkeypatch.delenv("LLMD_NAMESPACE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
