"""Unit coverage for the Kubernetes API-server proxy bypass helper."""

from __future__ import annotations

import os
from urllib.request import getproxies_environment, proxy_bypass_environment

import pytest

from llm_d_lens.infrastructure.kubernetes.auth import load_configuration
from llm_d_lens.infrastructure.kubernetes.proxy import (
    api_server_host,
    ensure_api_server_proxy_bypass,
    server_host,
)

_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- name: smc-16
  cluster:
    server: https://10.112.110.140:33481
    insecure-skip-tls-verify: true
contexts:
- name: smc-16
  context:
    cluster: smc-16
    user: smc-16
current-context: smc-16
users:
- name: smc-16
  user:
    token: test-token
"""


def test_server_host_parses_hostname_and_defaults_to_none():
    assert server_host("https://10.112.110.140:33481") == "10.112.110.140"
    assert server_host("https://api.example.com") == "api.example.com"
    assert server_host(None) is None
    assert server_host("") is None


def test_api_server_host_reads_first_cluster_and_tolerates_bad_input(tmp_path):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(_KUBECONFIG)
    assert api_server_host(str(kubeconfig)) == "10.112.110.140"

    assert api_server_host(None) is None
    assert api_server_host(str(tmp_path / "missing")) is None
    (tmp_path / "garbage").write_text(": not yaml :\n")
    assert api_server_host(str(tmp_path / "garbage")) is None
    (tmp_path / "empty").write_text("apiVersion: v1\nkind: Config\n")
    assert api_server_host(str(tmp_path / "empty")) is None


def test_ensure_bypass_adds_exact_host_when_proxy_configured(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:912")
    monkeypatch.setenv("NO_PROXY", "localhost,10.112.0.0/16")
    monkeypatch.setenv("no_proxy", "localhost,10.112.0.0/16")

    assert ensure_api_server_proxy_bypass("10.112.110.140") is True
    assert os.environ["NO_PROXY"].split(",") == ["localhost", "10.112.0.0/16", "10.112.110.140"]
    assert os.environ["no_proxy"].split(",") == ["localhost", "10.112.0.0/16", "10.112.110.140"]
    # Idempotent: the already-listed host is not duplicated.
    assert ensure_api_server_proxy_bypass("10.112.110.140") is False


@pytest.mark.parametrize(
    ("uppercase", "lowercase", "expected"),
    [
        ("localhost,127.0.0.1", None, "localhost,127.0.0.1,10.112.110.140"),
        (None, "localhost,127.0.0.1", "localhost,127.0.0.1,10.112.110.140"),
        (
            " localhost,127.0.0.1,localhost, ",
            "127.0.0.1,.example.com",
            "localhost,127.0.0.1,.example.com,10.112.110.140",
        ),
        (
            "localhost,10.112.110.140",
            "127.0.0.1,10.112.110.140",
            "localhost,10.112.110.140,127.0.0.1",
        ),
    ],
)
def test_ensure_bypass_merges_case_variants_and_preserves_loopback(monkeypatch, uppercase, lowercase, expected):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:912")
    for name, value in (("NO_PROXY", uppercase), ("no_proxy", lowercase)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert ensure_api_server_proxy_bypass("10.112.110.140") is True
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == expected
    proxies = getproxies_environment()
    for host in ("localhost", "127.0.0.1", "10.112.110.140"):
        assert proxy_bypass_environment(host, proxies)
    assert ensure_api_server_proxy_bypass("10.112.110.140") is False


@pytest.mark.parametrize("host", [None, ""])
def test_ensure_bypass_noop_without_host(monkeypatch, host):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:912")
    monkeypatch.setenv("NO_PROXY", "localhost")
    assert ensure_api_server_proxy_bypass(host) is False
    assert os.environ["NO_PROXY"] == "localhost"


def test_ensure_bypass_noop_without_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost")
    assert ensure_api_server_proxy_bypass("10.112.110.140") is False
    assert os.environ["NO_PROXY"] == "localhost"


@pytest.mark.asyncio
async def test_load_configuration_bypasses_kubeconfig_api_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:912")
    monkeypatch.setenv("NO_PROXY", "localhost,10.112.0.0/16")
    monkeypatch.setenv("no_proxy", "localhost,10.112.0.0/16")
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(_KUBECONFIG)

    configuration, context = await load_configuration(str(kubeconfig))

    assert context["name"] == "smc-16"
    assert "10.112.110.140" in os.environ["NO_PROXY"].split(",")
    assert "10.112.110.140" in os.environ["no_proxy"].split(",")


@pytest.mark.asyncio
async def test_load_configuration_preserves_uppercase_only_loopback_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:912")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.delenv("no_proxy", raising=False)
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(_KUBECONFIG)

    await load_configuration(str(kubeconfig))

    assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == "localhost,127.0.0.1,10.112.110.140"
    assert proxy_bypass_environment("127.0.0.1", getproxies_environment())
