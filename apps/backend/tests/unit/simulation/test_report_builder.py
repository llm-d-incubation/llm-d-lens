from __future__ import annotations

import base64
import csv
import gzip
import importlib
import importlib.resources
import io
import json
import re
import shutil
import subprocess
import tarfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient

import llm_d_lens.simulation.service as simulation_service
from llm_d_lens.api import app
from llm_d_lens.auth.routes import ROUTE_PERMISSIONS, validate_route_registry
from llm_d_lens.core.exceptions import ForbiddenError
from llm_d_lens.simulation.backends import get_backend
from llm_d_lens.simulation.errors import SimulationConfigurationError
from llm_d_lens.simulation.models import (
    ArtifactRecord,
    SimulationArtifact,
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationResult,
    SimulationTask,
    SimulationTrace,
)
from llm_d_lens.simulation.reporting import (
    build_bundle_tar_gz,
    build_report_artifacts,
    render_html_report,
)
from llm_d_lens.simulation.reporting.builder import (
    GZIP_HTML_THRESHOLD_BYTES,
    MAX_HTML_REPORT_BYTES,
    _sanitize_endpoint_url,
    ensure_safe_subpath,
)
from llm_d_lens.simulation.service import (
    reset_service_state,
    resolve_task_artifact,
    save_task,
    task_root,
)

_EXPECTED_CSP = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; img-src data:;\">"
)


@pytest.fixture
def sim_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    traces_dir = tmp_path / "traces"
    data_dir.mkdir(parents=True, exist_ok=True)
    traces_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LENS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TRACE_REPLAY_DATA_DIR", str(traces_dir))
    reset_service_state()
    yield tmp_path
    reset_service_state()


def _make_task(
    task_id: str = "00000001",
    *,
    name: str = "Baseline Replay",
    status: str = "completed",
    backend: str = "aiperf",
    model_name: str = "Qwen/Qwen2.5-7B-Instruct",
    endpoint_url: str = "https://user:supersecret@llm.example.com:8443/v1?token=abc",
    endpoint_cluster_id: str | None = "cluster-a",
    api_key: str | None = None,
    backend_options: dict | None = None,
    fixed_schedule: bool = True,
    logs: list[str] | None = None,
    result: SimulationResult | None = None,
    with_result: bool = True,
) -> SimulationTask:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    t_dir = task_root() / task_id
    t_dir.mkdir(parents=True, exist_ok=True)
    if with_result and result is None:
        result = SimulationResult(
            run_id=task_id,
            backend=backend,
            backend_version="0.13.0",
            summary={
                "total_requests": 100,
                "successful_requests": 98,
                "failed_requests": 2,
                "throughput_rps": 12.5,
                "throughput_tps": 420.0,
                "ttft": {"mean": 42.0, "p50": 38.0, "p95": 80.0, "p99": 95.0},
                "tpot": {"mean": 11.2, "p50": 10.5, "p95": 18.0, "p99": 22.0},
                "latency": {"mean": 310.0, "p50": 290.0, "p95": 520.0, "p99": 610.0},
            },
            per_request=[],
            backend_metrics={"request_count": 100},
            warnings=[],
            artifacts=[],
        )
    return SimulationTask(
        id=task_id,
        name=name,
        description="Unit test simulation task",
        scenario="chat",
        status=status,
        endpoint_mode="external",
        endpoint_url=endpoint_url,
        endpoint_cluster_id=endpoint_cluster_id,
        endpoint_cluster_name="Cluster A" if endpoint_cluster_id else None,
        endpoint_namespace="llm-d",
        endpoint_deployment_name="vllm-qwen",
        model_name=model_name,
        api_key=api_key,
        simulation=SimulationConfig(
            backend=backend,
            backend_options=backend_options or {"warmup_seconds": 5, "e2e_slo": 2.0, "collect_server_telemetry": True},
            duration_seconds=60,
            num_requests=None,
            stream=True,
            warmup_enabled=False,
            grace_period_seconds=30,
        ),
        prompt=SimulationPrompt(
            type="trace",
            dataset=SimulationDataset(name="mooncake-arxiv", scenario="chat", tokenizer=None),
            trace=SimulationTrace(
                path="mooncake_trace.jsonl",
                format="mooncake_trace",
                fixed_schedule=fixed_schedule,
                timeout_seconds=900,
                synthesis_speedup_ratio=1.0,
            ),
        ),
        task_dir=str(t_dir),
        progress_percent=100 if status == "completed" else 50,
        progress_message="done" if status == "completed" else "running",
        logs=logs or ["Task started", "Task completed"],
        result=result,
        created_at=now,
        started_at=now,
        execution_started_at=now,
        completed_at=now if status in {"completed", "failed", "cancelled"} else None,
        updated_at=now,
    )


def _sample_client_series() -> dict:
    return {
        "schema_version": 1,
        "warmup_seconds": 5.0,
        "buckets": [
            {"t": 0.0, "rps": 10.0, "goodput_rps": 10.0, "ttft_p50_ms": 35.0, "e2e_p95_ms": 220.0},
            {"t": 5.0, "rps": 12.5, "goodput_rps": 12.0, "ttft_p50_ms": 38.0, "e2e_p95_ms": 240.0},
        ],
        "windows": {
            "profiling": {
                "total_requests": 90,
                "successful_requests": 88,
                "failed_requests": 2,
                "good_requests": 87,
                "error_rate_pct": 2.22,
                "throughput_rps": 12.5,
                "goodput_rps": 12.1,
                "output_tok_per_s": 420.0,
                "slo_attainment_pct": 96.7,
                "ttft_ms": {"mean_ms": 40.0, "p50_ms": 38.0, "p95_ms": 75.0, "p99_ms": 90.0},
                "tpot_ms": {"mean_ms": 11.0, "p50_ms": 10.5, "p95_ms": 17.0, "p99_ms": 21.0},
                "e2e_ms": {"mean_ms": 300.0, "p50_ms": 280.0, "p95_ms": 500.0, "p99_ms": 590.0},
            },
            "full": {
                "total_requests": 100,
                "successful_requests": 98,
                "failed_requests": 2,
                "throughput_rps": 12.0,
            },
        },
    }


def _sample_server_telemetry() -> dict:
    return {
        "available": True,
        "topology_mode": "aggregated",
        "pods": ["vllm-qwen-pod-0"],
        "hardware_profile": {
            "id": "nvidia-h100",
            "display_name": "NVIDIA H100",
            "vendor": "nvidia",
            "metric_group": "gpu",
            "device_count": 8,
        },
        "summary": {
            "kv_cache_usage_percent": {"mean": 42.5, "max": 68.0},
            "prefix_cache_hit_percent": {"mean": 55.0},
        },
        "energy": {
            "accelerator_avg_power_watts": 310.0,
            "accelerator_peak_power_watts": 420.0,
            "accelerator_energy_wh": 5.2,
            "output_tokens_per_wh": 8000.0,
        },
        "series": [{"t": 0.0, "kv_cache_pct": 40.0}, {"t": 5.0, "kv_cache_pct": 55.0}],
    }


def _extract_embedded_report_json(html_text: str) -> dict:
    match = re.search(
        r'<script[^>]*id="lens-report-data"[^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    assert match is not None, "Embedded JSON script tag not found"
    raw = match.group(1).strip()
    decoded = json.loads(raw)
    if isinstance(decoded, dict) and decoded.get("compressed") == "gzip-base64":
        raw_bytes = gzip.decompress(base64.b64decode(decoded["payload"]))
        return json.loads(raw_bytes.decode("utf-8"))
    return decoded


# ---------------------------------------------------------------------------
# 1. Offline Single-File HTML Report
# ---------------------------------------------------------------------------


def test_reporting_assets_packaged_and_loaded_via_importlib() -> None:
    pyproject_path = Path(__file__).resolve().parents[3] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert package_data["llm_d_lens.simulation.reporting"] == ["assets/*.html", "assets/*.js"]

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    assert assets_dir.joinpath("report_template.html").is_file()
    assert assets_dir.joinpath("report_charts.js").is_file()


def test_html_report_offline_csp_and_no_unsafe_dom_sinks(sim_env: Path) -> None:
    task = _make_task("00000001")
    grafana = [
        {"label": "vLLM Dashboard", "url": "https://grafana.example.com/d/vllm?orgId=1"},
    ]
    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": "healthy"},
        grafana_links=grafana,
    )

    assert _EXPECTED_CSP in html_text

    # No dangerous DOM / network sinks anywhere in rendered HTML/JS
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "fetch("):
        assert forbidden not in html_text

    # Only allowed external URLs: SVG namespace + explicit Grafana link
    urls = set(re.findall(r"https?://[^\s\"'<>\\)]+", html_text))
    allowed_urls = {
        "http://www.w3.org/2000/svg",
        "https://grafana.example.com/d/vllm?orgId=1",
        "https://llm.example.com:8443/v1",
    }
    unexpected = urls - allowed_urls
    assert not unexpected, f"Unexpected external URLs in HTML report: {unexpected}"


def test_html_report_help_badges_and_csp_safety(sim_env: Path) -> None:
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000014")
    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    assert _EXPECTED_CSP in html_text
    assert ".help-icon" in html_text
    assert ".tooltip.help-tip" in html_text
    assert ".help-tip-formula-wrap" in html_text
    assert ".help-tip-formula-label" in html_text
    assert ".help-tip-formula" in html_text
    assert "function helpBadge(" in html_text
    assert "function showHelpTip(" in html_text
    assert "function chartCard(" in html_text
    assert "function normalizeHelpKey(" in html_text
    assert "function resolveHelpFormula(" in html_text
    assert "HELP_TEXTS" in html_text
    assert "HELP_FORMULAS" in html_text

    # Ensure zero unsafe DOM or network sinks anywhere in rendered HTML/JS
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    # Key metric, chart, and table column explanations across all 6 report pages
    for expected_key in (
        "Automated Verdict",
        "SLO Attainment",
        "Goodput",
        "Completed / SLO-Compliant Requests",
        "Request Throughput / Goodput",
        "TTFT (P50 / P90 / P99)",
        "TPOT (P50 / P90 / P99)",
        "Peak KV Cache Usage",
        "o-thr",
        "b-stage-breakdown",
        "Stage Share Summary",
        "b-ttft-decomp",
        "Load skew ratio (max / mean)",
        "s-flow",
        "Server TTFT (P50)",
        "EPP Scheduling Latency",
        "Prefix Cache Hit Rate",
        "Total Preemptions",
        "r-fidelity",
        "TTFT by Input Sequence Length Bucket",
        "TTFT by Time and Input Sequence Length",
        "TPOT by Time and Output Sequence Length",
        "Schedule Drift (ms)",
        "Host CPU %",
        "c-manifest",
    ):
        assert f'"{expected_key}":' in html_text

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    for call_pat in (
        r'kpiTile\("([^"]+)"',
        r'chartCard\("([^"]+)"',
        r'helpBadge\("([^"]+)"\)',
    ):
        for title in re.findall(call_pat, charts_js):
            assert "GPU" not in title, f"Hardcoded GPU literal found in title: {title}"
            assert f'"{title}":' in charts_js, f"Missing HELP_TEXTS entry for: {title}"


def test_html_report_xss_escaping_and_placeholder_collision(sim_env: Path) -> None:
    xss_payload = "</script><img src=x onerror=alert(1)>"
    placeholder_payload = "/*__LENS_REPORT_CHARTS_JS__*/ <!--__LENS_REPORT_DATA__--> <!--__LENS_REPORT_TITLE__-->"
    task = _make_task(
        "00000002",
        name=f"XSS {xss_payload} {placeholder_payload}",
        model_name=f"model-{xss_payload}",
        logs=[f"error: {xss_payload} {placeholder_payload}"],
    )
    server = _sample_server_telemetry()
    server["pods"] = [f"pod-{xss_payload}"]
    records = [
        ArtifactRecord(
            arrived=1700000000.0,
            completed=1700000001.0,
            successful=False,
            status_code=500,
            error=f"Boom {xss_payload} {placeholder_payload}",
            request_id="req-1",
            latency_ms=1000.0,
            issue_failed=True,
        )
    ]

    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=server,
        forensics={"summary": f"verdict {xss_payload}"},
        records=records,
    )

    # Raw unescaped </script><img must never appear in HTML output
    assert "<img src=x onerror=alert(1)>" not in html_text
    assert "\\u003c/script\\u003e\\u003cimg" in html_text

    # Embedded JSON must parse cleanly without breaking out of <script type="application/json">
    payload = _extract_embedded_report_json(html_text)
    assert xss_payload in payload["task"]["name"]
    assert placeholder_payload in payload["task"]["name"]
    assert payload["error_samples"][0]["error"].startswith("Boom ")
    # Charts JS must still be injected intact after placeholder collision
    assert "SVG_NS" in html_text


def test_html_report_sanitizes_grafana_and_endpoint_links(sim_env: Path) -> None:
    task = _make_task(
        "00000003",
        endpoint_url="https://user:secretpass@llm.example.com:8443/v1?api_key=leak",
    )
    grafana_links = [
        {"label": "Bad JS", "url": "javascript:alert(1)"},
        {"label": "Bad Data", "url": "data:text/html,<script>alert(1)</script>"},
        {"label": "Good Grafana", "url": "https://grafana.example.com/d/abc?var-ns=llm-d"},
    ]
    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        grafana_links=grafana_links,
    )
    assert "javascript:alert(1)" not in html_text
    assert "data:text/html" not in html_text
    assert "secretpass" not in html_text
    assert "api_key=leak" not in html_text

    payload = _extract_embedded_report_json(html_text)
    assert payload["grafana_links"] == [
        {"label": "Good Grafana", "url": "https://grafana.example.com/d/abc?var-ns=llm-d"}
    ]
    assert payload["task"]["sanitized_endpoint_url"] == "https://llm.example.com:8443/v1"


def test_sanitize_endpoint_url_handles_malformed_ports_and_schemeless_queries() -> None:
    assert (
        _sanitize_endpoint_url("https://user:supersecret@host.example.com:badport/v1;p=1?token=x#frag")
        == "https://host.example.com:badport/v1"
    )
    assert (
        _sanitize_endpoint_url("user:supersecret@host.example.com:8443/v1;jsessionid=abc?sig=secret#frag")
        == "host.example.com:8443/v1"
    )
    assert _sanitize_endpoint_url("host.example.com/v1;foo=bar?sig=secret#frag") == "host.example.com/v1"
    assert (
        _sanitize_endpoint_url("https://user:pass@host.example.com:8443/v1;jsessionid=abc/chat?token=x#frag")
        == "https://host.example.com:8443/v1"
    )
    assert (
        _sanitize_endpoint_url("https://host.example.com/sk-supersecretapikey1234567890")
        == "https://host.example.com/[REDACTED]"
    )
    assert _sanitize_endpoint_url("   ") is None
    assert _sanitize_endpoint_url(None) is None


def test_secret_redaction_across_report_and_bundle(sim_env: Path) -> None:
    secret_hf = "HF_TOKEN=hf_" + "AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    secret_lens = "lens-mk-" + "99887766554433221100aabbccddeeff"
    secret_bearer = "Bearer " + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload"
    secret_url = "https://" + "tok_secret_value" + "@private.registry.local:8443/v1"
    secret_kubeconfig = "kubeconfig: |\n  client-key-data: LS0tLS1CRUdJTi1QUklWQVRF"  # noqa: S105
    secret_api_key = "sk-" + "supersecretapikey1234567890"

    task = _make_task(
        "00000004",
        endpoint_url=secret_url,
        api_key=secret_api_key,
        backend_options={
            "warmup_seconds": 5,
            "api_key": secret_api_key,
            "env": {"HF_TOKEN": "hf_AbCdEfGhIjKlMnOpQrStUvWxYz123456"},
            "kubeconfig": secret_kubeconfig,
        },
        logs=[
            f"Starting with {secret_hf}",
            f"Auth header {secret_bearer}",
            f"Token {secret_lens}",
        ],
    )
    backend_dir = Path(task.task_dir) / "artifacts" / task.simulation.backend
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "stdout.log").write_text(
        f"stdout line: {secret_hf} {secret_bearer} {secret_lens}\n",
        encoding="utf-8",
    )
    (backend_dir / "stderr.log").write_text(
        f"stderr line: {secret_url} api_key={secret_api_key}\n",
        encoding="utf-8",
    )

    written = build_report_artifacts(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics={"note": f"saw {secret_bearer} and {secret_lens}"},
    )
    bundle_path = build_bundle_tar_gz(task)

    leaks = [
        "hf_AbCdEfGhIjKlMnOpQrStUvWxYz123456",
        "lens-mk-99887766554433221100aabbccddeeff",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload",
        "tok_secret_value",
        "LS0tLS1CRUdJTi1QUklWQVRF",
        "supersecretapikey1234567890",
    ]
    for kind in ("run_manifest", "report_html", "summary_csv"):
        content = written[kind].read_text(encoding="utf-8")
        for leak in leaks:
            assert leak not in content, f"Secret leaked in {kind}: {leak}"

    with tarfile.open(bundle_path, "r:gz") as tar:
        for member_name in (
            "aiperf-00000004/config/run-manifest.yaml",
            "aiperf-00000004/report/index.html",
            "aiperf-00000004/summary.csv",
            "aiperf-00000004/logs/aiperf.log",
            "aiperf-00000004/logs/task-lifecycle.log",
        ):
            member = tar.getmember(member_name)
            text = tar.extractfile(member).read().decode("utf-8")
            for leak in leaks:
                assert leak not in text, f"Secret leaked in bundle {member_name}: {leak}"


def test_html_report_large_scale_gzip_compression_and_size_budget(sim_env: Path) -> None:
    task = _make_task("00000005")
    # Build >5 MB JSON payload that survives _compact_report_payload downsampling
    # (1500 lanes max * ~5.4 KB each > 5 MB raw JSON, compresses well under 10 MB)
    f1 = "x" * 1800
    f2 = "y" * 1800
    f3 = "z" * 1800
    large_sessions = {
        "available": True,
        "total_requests": 200_000,
        "lanes": [{"session_id": f"sess-{i:04d}", "turns": 4, "f1": f1, "f2": f2, "f3": f3} for i in range(1500)],
    }
    client_series = _sample_client_series()
    client_series["buckets"] = [
        {"t": float(i), "rps": 250.0, "goodput_rps": 248.0, "ttft_p50_ms": 30.0 + (i % 10)} for i in range(2000)
    ]

    html_text = render_html_report(
        task,
        client_series=client_series,
        server_telemetry=_sample_server_telemetry(),
        sessions_summary=large_sessions,
        forensics={"total_requests": 200_000},
    )
    encoded_bytes = html_text.encode("utf-8")
    assert len(encoded_bytes) < MAX_HTML_REPORT_BYTES

    match = re.search(
        r'<script[^>]*id="lens-report-data"[^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    assert match is not None
    wrapper = json.loads(match.group(1).strip())
    assert wrapper["compressed"] == "gzip-base64"
    decompressed = gzip.decompress(base64.b64decode(wrapper["payload"]))
    assert len(decompressed) > GZIP_HTML_THRESHOLD_BYTES

    restored = json.loads(decompressed.decode("utf-8"))
    assert len(restored["client_series"]["buckets"]) <= 240
    assert len(restored["sessions_summary"]["lanes"]) == 1500


# ---------------------------------------------------------------------------
# 2. Reproducible .tar.gz Bundle
# ---------------------------------------------------------------------------


def test_build_bundle_tar_gz_deterministic_sorted_no_symlinks_and_full_logs(sim_env: Path) -> None:
    task = _make_task("00000006")
    backend_dir = Path(task.task_dir) / "artifacts" / task.simulation.backend
    backend_dir.mkdir(parents=True, exist_ok=True)

    # Full log > 3000 chars with a secret near the end (must NOT be truncated at 2000 chars)
    long_prefix = "A" * 2500
    secret_tail = "HF_TOKEN=hf_" + "ZyXwVuTsRqPoNmLkJiHgFeDcBa654321"
    (backend_dir / "stdout.log").write_text(f"{long_prefix} {secret_tail} TAIL_MARKER\n", encoding="utf-8")
    (backend_dir / "stderr.log").write_text("warning: minor retry\n", encoding="utf-8")
    (backend_dir / "requests.jsonl").write_text(
        json.dumps({"request_id": "r1", "status_code": 200, "prompt": "secret user prompt", "latency_ms": 120.5})
        + "\n",
        encoding="utf-8",
    )

    written = build_report_artifacts(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        sessions_summary={"available": True, "lanes": []},
        forensics={"verdict": "healthy"},
    )
    assert "report_bundle" not in written
    assert not any(p.name == "bundle.tar.gz" for p in written.values())

    bundle_1 = build_bundle_tar_gz(task)
    bytes_1 = bundle_1.read_bytes()
    bundle_2 = build_bundle_tar_gz(task)
    bytes_2 = bundle_2.read_bytes()

    expected_members = sorted(
        [
            "aiperf-00000006/config/run-manifest.yaml",
            "aiperf-00000006/forensics.json",
            "aiperf-00000006/logs/aiperf.log",
            "aiperf-00000006/logs/task-lifecycle.log",
            "aiperf-00000006/report/index.html",
            "aiperf-00000006/requests/requests.jsonl",
            "aiperf-00000006/requests/sessions-summary.json",
            "aiperf-00000006/summary.csv",
            "aiperf-00000006/summary.json",
            "aiperf-00000006/timeseries/client-series.json",
            "aiperf-00000006/timeseries/server-telemetry.json",
        ]
    )

    with (
        tarfile.open(fileobj=io.BytesIO(bytes_1), mode="r:gz") as tar1,
        tarfile.open(fileobj=io.BytesIO(bytes_2), mode="r:gz") as tar2,
    ):
        names_1 = tar1.getnames()
        names_2 = tar2.getnames()
        assert names_1 == expected_members
        assert names_2 == expected_members

        for info in tar1.getmembers():
            assert not info.issym()
            assert not info.islnk()
            assert not info.name.startswith("/")
            assert ".." not in Path(info.name).parts
            assert info.mtime == 0
            assert info.uid == 0
            assert info.gid == 0
            assert info.mode == 0o644

        # Check full un-truncated log with secret redacted
        log_text = tar1.extractfile("aiperf-00000006/logs/aiperf.log").read().decode("utf-8")
        assert long_prefix in log_text
        assert "TAIL_MARKER" in log_text
        assert "ZyXwVuTsRqPoNmLkJiHgFeDcBa654321" not in log_text

        # Check prompt text stripped from requests.jsonl
        req_text = tar1.extractfile("aiperf-00000006/requests/requests.jsonl").read().decode("utf-8")
        assert "secret user prompt" not in req_text
        assert '"request_id":"r1"' in req_text

        # Check summary.csv is valid CSV
        csv_text = tar1.extractfile("aiperf-00000006/summary.csv").read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(csv_text)))
        assert rows[0] == ["window", "category", "metric", "unit", "value"]

        # Check run-manifest.yaml is valid YAML
        manifest = yaml.safe_load(tar1.extractfile("aiperf-00000006/config/run-manifest.yaml").read().decode("utf-8"))
        assert manifest["task_id"] == "00000006"


# ---------------------------------------------------------------------------
# 3. Artifact Resolution, Legacy/Partial Tasks & Security
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_task_artifact_registered_on_demand_and_partial(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 3a. Cancelled/partial task with task.result is None -> on-demand report_html & report_bundle
    partial_task = _make_task("00000007", status="cancelled", with_result=False)
    backend_dir = Path(partial_task.task_dir) / "artifacts" / "aiperf"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "stdout.log").write_text("partial stdout\n", encoding="utf-8")
    (backend_dir / "stderr.log").write_text("partial stderr\n", encoding="utf-8")
    await save_task(partial_task)

    monkeypatch.setattr(
        simulation_service,
        "get_task_telemetry",
        AsyncMock(
            return_value={
                "client_series": _sample_client_series(),
                "server_telemetry": _sample_server_telemetry(),
                "sessions_summary": {"available": False, "reason": "none"},
                "forensics": {"verdict": "cancelled"},
                "grafana_links": [],
            }
        ),
    )

    class _DummyBackend:
        def artifact_records(self, task: SimulationTask, *, tolerate_incomplete: bool = False) -> list[ArtifactRecord]:
            return []

    monkeypatch.setattr(simulation_service, "get_backend", lambda _name: _DummyBackend())

    html_path, html_media, html_name = await resolve_task_artifact(partial_task, "report_html")
    assert html_path.is_file()
    assert html_media == "text/html; charset=utf-8"
    assert html_name == "aiperf-report-00000007.html"

    bundle_path, bundle_media, bundle_name = await resolve_task_artifact(partial_task, "report_bundle")
    assert bundle_path.is_file()
    assert bundle_media == "application/gzip"
    assert bundle_name == "aiperf-bundle-00000007.tar.gz"

    for kind, expected_media in (
        ("forensics", "application/json"),
        ("summary_csv", "text/csv"),
        ("run_manifest", "application/yaml"),
        ("stdout", "text/plain"),
        ("stderr", "text/plain"),
    ):
        p, m, _ = await resolve_task_artifact(partial_task, kind)
        assert p.is_file()
        assert m == expected_media

    # 3b. Legacy task with registered summary_csv artifact
    legacy_task = _make_task("00000008")
    legacy_csv = Path(legacy_task.task_dir) / "artifacts" / "aiperf" / "legacy_summary.csv"
    legacy_csv.parent.mkdir(parents=True, exist_ok=True)
    legacy_csv.write_text("window,category,metric,unit,value\n", encoding="utf-8")
    legacy_task.result.artifacts = [
        SimulationArtifact(kind="summary_csv", path=str(legacy_csv), media_type="text/csv"),
    ]
    resolved_csv, csv_media, csv_name = await resolve_task_artifact(legacy_task, "summary_csv")
    assert resolved_csv == legacy_csv.resolve()
    assert csv_media == "text/csv"
    assert csv_name == "legacy_summary.csv"


@pytest.mark.asyncio
async def test_ensure_safe_subpath_and_resolve_reject_symlinks_and_cross_task_escape(sim_env: Path) -> None:
    task_a = _make_task("0000000a")
    task_b = _make_task("0000000b")
    secret_in_b = Path(task_b.task_dir) / "secret.txt"
    secret_in_b.write_text("cross-task-secret", encoding="utf-8")

    # Symlink inside task_a pointing to task_b under the same task_root()
    symlink_in_a = Path(task_a.task_dir) / "artifacts" / "aiperf" / "stdout.log"
    symlink_in_a.parent.mkdir(parents=True, exist_ok=True)
    symlink_in_a.symlink_to(secret_in_b)

    with pytest.raises(ValueError, match="Symlink"):
        ensure_safe_subpath(symlink_in_a, Path(task_a.task_dir), boundary_root=task_root())

    with pytest.raises(SimulationConfigurationError, match="symlink"):
        await resolve_task_artifact(task_a, "stdout")

    with pytest.raises(ValueError, match="escapes"):
        ensure_safe_subpath(secret_in_b, Path(task_a.task_dir), boundary_root=task_root())


# ---------------------------------------------------------------------------
# 4. RBAC, Cluster Access & API Endpoints
# ---------------------------------------------------------------------------


def test_route_registry_validation_and_telemetry_permissions() -> None:
    assert validate_route_registry(app) == []
    by_route = {(r.method, r.path): r.permission for r in ROUTE_PERMISSIONS}
    assert by_route[("GET", "/api/simulation/tasks/{task_id}/live-telemetry")] == "simulation:task:read"
    assert by_route[("GET", "/api/simulation/tasks/{task_id}/telemetry")] == "simulation:task:read"


def test_cluster_access_403_and_resolved_endpoint_precedence(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task("0000000c", endpoint_cluster_id="cluster-secret")
    sim_router = importlib.import_module("llm_d_lens.simulation.router")
    monkeypatch.setattr(simulation_service, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(simulation_service, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "get_task", AsyncMock(return_value=task))

    def deny_cluster(_principal, cluster_id: str | None) -> None:
        if cluster_id == "cluster-secret":
            raise ForbiddenError("cluster is not accessible")

    monkeypatch.setattr(sim_router, "require_cluster_access", deny_cluster)
    monkeypatch.setattr(simulation_service, "require_cluster_access", deny_cluster)

    with TestClient(app) as client:
        for method, path, kwargs in (
            ("GET", "/api/simulation/tasks/0000000c/live-telemetry", {}),
            ("GET", "/api/simulation/tasks/0000000c/telemetry", {}),
            ("GET", "/api/simulation/tasks/0000000c/artifacts/report_html", {}),
            ("GET", "/api/simulation/tasks/0000000c/response-code-issues", {}),
            ("POST", "/api/simulation/tasks/0000000c/stop", {}),
            ("POST", "/api/simulation/tasks/0000000c/rerun", {}),
            ("DELETE", "/api/simulation/tasks/0000000c", {}),
        ):
            resp = client.request(method, path, **kwargs)
            assert resp.status_code == 403, f"Expected 403 for {method} {path}, got {resp.status_code}: {resp.text}"

        # POST /tasks: resolved_endpoint.cluster_id takes precedence over client-supplied endpoint_cluster_id
        dataset_file = sim_env / "traces" / "mooncake_trace.jsonl"
        dataset_file.write_text('{"timestamp": 0, "input_length": 10, "output_length": 5}\n', encoding="utf-8")

        monkeypatch.setattr(
            simulation_service,
            "_resolve_task_endpoint_url",
            AsyncMock(
                return_value=SimpleNamespace(
                    url="http://127.0.0.1:8000",
                    cluster_id="cluster-secret",
                    cluster_name="Secret Cluster",
                    namespace="ns",
                    deployment_name="dep",
                )
            ),
        )
        monkeypatch.setattr(simulation_service, "discover_models", AsyncMock(return_value={"models": ["model"]}))

        post_resp = client.post(
            "/api/simulation/tasks",
            json={
                "name": "Spoofed Cluster Test",
                "scenario": "chat",
                "endpoint_mode": "external",
                "endpoint_url": "http://127.0.0.1:8000",
                "endpoint_cluster_id": "cluster-allowed",
                "model_name": "model",
                "backend": "aiperf",
                "trace_dataset": "mooncake-arxiv",
                "trace_path": str(dataset_file),
                "duration_seconds": 60,
            },
        )
        assert post_resp.status_code == 403


def test_live_telemetry_409_and_response_code_issues_filters(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task("0000000d", status="completed", backend="aiperf")
    req_file = Path(task.task_dir) / "artifacts" / "aiperf" / "profile_export.jsonl"
    req_file.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "request_id": "r1",
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 100_000_000,
                "request_end_ns": 500_000_000,
                "status_code": 200,
            },
            "metrics": {},
        },
        {
            "request_id": "r2",
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 1_000_000_000,
                "request_end_ns": 1_200_000_000,
            },
            "metrics": {},
            "error": {"code": 429, "message": "rate limit"},
        },
        {
            "request_id": "r3",
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 2_000_000_000,
                "request_end_ns": 3_000_000_000,
            },
            "metrics": {},
            "error": {"code": 504, "message": "upstream gateway error"},
        },
        {
            "request_id": "r4",
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 3_000_000_000,
                "request_end_ns": 4_000_000_000,
            },
            "metrics": {},
            "timeout": True,
        },
        {
            "request_id": "r5",
            "metadata": {"benchmark_phase": "profiling"},
            "metrics": {},
            "error": "Transport error: reset",
        },
        {
            "request_id": "r6",
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 5_000_000_000,
                "request_end_ns": 5_100_000_000,
            },
            "metrics": {},
            "error": "mystery failure",
        },
    ]
    req_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    sim_router = importlib.import_module("llm_d_lens.simulation.router")
    monkeypatch.setattr(simulation_service, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "require_cluster_access", lambda *_args: None)

    with TestClient(app) as client:
        live_resp = client.get("/api/simulation/tasks/0000000d/live-telemetry")
        assert live_resp.status_code == 409

        for status_filter, expected_total in (
            ("all", 5),
            ("4xx", 1),
            ("5xx", 1),
            ("timeout", 1),
            ("transport", 1),
            ("unknown", 3),
            ("504", 1),
        ):
            resp = client.get(
                f"/api/simulation/tasks/0000000d/response-code-issues?status_code={status_filter}&sort=newest"
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["total"] == expected_total, f"filter={status_filter}: {body}"
            assert body["counts_by_code"]["all"] == 5
            assert body["counts_by_code"]["unknown"] == 1
            assert body["counts_by_code"]["504"] == 1

        newest = client.get("/api/simulation/tasks/0000000d/response-code-issues?status_code=all&sort=newest").json()
        asc = client.get(
            "/api/simulation/tasks/0000000d/response-code-issues?status_code=all&sort=request_number"
        ).json()
        assert [i["request_number"] for i in newest["issues"]] == [6, 5, 4, 3, 2]
        assert [i["request_number"] for i in asc["issues"]] == [2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_backend_options_validation_warmup_e2e_slo_and_collect_telemetry(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = get_backend("aiperf")
    monkeypatch.setattr(backend, "validate_backend", AsyncMock(return_value=None))

    valid_task = _make_task(
        "0000000e",
        backend="aiperf",
        backend_options={"warmup_seconds": 0, "e2e_slo": 1.5, "collect_server_telemetry": False},
    )
    await backend.validate(valid_task)

    for bad_options in (
        {"warmup_seconds": -1},
        {"warmup_seconds": 60},  # == duration_seconds
        {"warmup_seconds": 90},  # > duration_seconds
        {"warmup_seconds": True},
        {"e2e_slo": -0.5},
        {"e2e_slo": "bad"},
        {"collect_server_telemetry": "yes"},
    ):
        bad_task = _make_task("0000000f", backend="aiperf", backend_options=bad_options)
        with pytest.raises(SimulationConfigurationError):
            await backend.validate(bad_task)


@pytest.mark.asyncio
async def test_stale_v1_client_series_auto_regenerates_report_and_bundle(sim_env: Path) -> None:
    from llm_d_lens.simulation.telemetry_post import CLIENT_SERIES_SCHEMA_VERSION, FORENSICS_SCHEMA_VERSION

    task = _make_task(
        "bc8de4e8",
        backend="aiperf",
        backend_options={"warmup_seconds": 0, "collect_server_telemetry": False},
    )
    task = task.model_copy(update={"simulation": task.simulation.model_copy(update={"duration_seconds": 32})})
    backend_dir = Path(task.task_dir) / "artifacts" / "aiperf"
    report_dir = Path(task.task_dir) / "artifacts" / "report"
    backend_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    # Burst arrivals in 0..8s, delayed completions in buckets 1..5 (7..28s)
    origin_ns = 1_795_597_273_000_000_000
    aiperf_lines = [
        {
            "metadata": {
                "request_id": f"r-{i}",
                "benchmark_phase": "profiling",
                "request_start_ns": origin_ns + i * 1_000_000_000,
                "request_end_ns": origin_ns + (7 + i * 5) * 1_000_000_000,
            },
            "metrics": {
                "input_sequence_length": {"value": 128},
                "output_sequence_length": {"value": 32},
                "time_to_first_token": {"value": 200.0 + i * 25.0},
                "inter_token_latency": {"value": 20.0 + i},
                "request_latency": {"value": float((7 + i * 4) * 1000)},
            },
        }
        for i in range(5)
    ]
    (backend_dir / "profile_export.jsonl").write_text(
        "\n".join(json.dumps(item) for item in aiperf_lines) + "\n",
        encoding="utf-8",
    )

    stale_v1 = {
        "schema_version": 1,
        "warmup_seconds": 0.0,
        "duration_seconds": 32.0,
        "bucket_seconds": 5,
        "buckets": [
            {
                "index": 0,
                "start_seconds": 0,
                "end_seconds": 5,
                "mid_seconds": 2.5,
                "ttft": {"p50": 225.0, "p90": 300.0, "p99": 300.0, "mean": 250.0},
            },
            {
                "index": 4,
                "start_seconds": 20,
                "end_seconds": 25,
                "mid_seconds": 22.5,
                "ttft": {"p50": None, "p90": None, "p99": None, "mean": None},
            },
        ],
        "windows": {"full": {"request_count": 5, "successful_requests": 5, "error_rate_pct": 0.0}},
    }

    def _seed_stale_report_files() -> None:
        (report_dir / "client-series.json").write_text(json.dumps(stale_v1), encoding="utf-8")
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": 1, "verdict": {"code": "healthy"}, "telemetry_scope": "client_only"}),
            encoding="utf-8",
        )
        (report_dir / "index.html").write_text("<html><body>stale-v1</body></html>", encoding="utf-8")

    _seed_stale_report_files()
    await save_task(task)

    # 1. Serving report_html refreshes stale v1 client-series.json, forensics.json, and index.html
    html_path, _, _ = await resolve_task_artifact(task, "report_html")
    embedded = _extract_embedded_report_json(html_path.read_text(encoding="utf-8"))
    assert embedded["client_series"]["schema_version"] == CLIENT_SERIES_SCHEMA_VERSION
    assert embedded["client_series"]["buckets"][0]["completed_requests"] == 0
    assert embedded["client_series"]["buckets"][0]["ttft"]["p50"] is None
    assert embedded["client_series"]["buckets"][4]["ttft"]["p50"] is not None
    assert (
        json.loads((report_dir / "client-series.json").read_text(encoding="utf-8"))["schema_version"]
        == CLIENT_SERIES_SCHEMA_VERSION
    )
    assert (
        json.loads((report_dir / "forensics.json").read_text(encoding="utf-8"))["schema_version"]
        == FORENSICS_SCHEMA_VERSION
    )

    # 2. Direct build_bundle_tar_gz on stale v1 directory auto-regenerates client-series.json & index.html before archiving
    _seed_stale_report_files()
    bundle_path = build_bundle_tar_gz(task)
    with tarfile.open(bundle_path, "r:gz") as tar:
        bundled_cs = json.loads(tar.extractfile("aiperf-bc8de4e8/timeseries/client-series.json").read().decode("utf-8"))
        bundled_fr = json.loads(tar.extractfile("aiperf-bc8de4e8/forensics.json").read().decode("utf-8"))
        bundled_html = tar.extractfile("aiperf-bc8de4e8/report/index.html").read().decode("utf-8")
    assert bundled_cs["schema_version"] == CLIENT_SERIES_SCHEMA_VERSION
    assert bundled_fr["schema_version"] == FORENSICS_SCHEMA_VERSION
    assert bundled_cs["buckets"][4]["ttft"]["p50"] is not None
    assert (
        _extract_embedded_report_json(bundled_html)["client_series"]["schema_version"] == CLIENT_SERIES_SCHEMA_VERSION
    )

    # 3. resolve_task_artifact(..., "report_bundle") refreshes even when bundle.tar.gz already exists on disk
    _seed_stale_report_files()
    resolved_bundle, _, _ = await resolve_task_artifact(task, "report_bundle")
    with tarfile.open(resolved_bundle, "r:gz") as tar:
        rebuilt_cs = json.loads(tar.extractfile("aiperf-bc8de4e8/timeseries/client-series.json").read().decode("utf-8"))
    assert rebuilt_cs["schema_version"] == CLIENT_SERIES_SCHEMA_VERSION
    assert rebuilt_cs["buckets"][-1]["ttft"]["p50"] is not None


@pytest.mark.asyncio
async def test_legacy_html_without_v3_marker_triggers_rebuild(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when JSONs are current schema, an index.html lacking REPORT_TEMPLATE_VERSION_MARKER is rebuilt."""
    from llm_d_lens.simulation.reporting import builder as report_builder
    from llm_d_lens.simulation.telemetry_post import CLIENT_SERIES_SCHEMA_VERSION, FORENSICS_SCHEMA_VERSION

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    task = _make_task("00000010")
    report_dir = Path(task.task_dir) / "artifacts" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    v3_cs = _sample_client_series()
    v3_cs["schema_version"] = CLIENT_SERIES_SCHEMA_VERSION
    v3_cs["drift_histogram"] = [{"label": "<10ms", "count": 4}]
    (report_dir / "client-series.json").write_text(json.dumps(v3_cs), encoding="utf-8")
    (report_dir / "forensics.json").write_text(
        json.dumps(
            {
                "schema_version": FORENSICS_SCHEMA_VERSION,
                "verdict": {"code": "healthy"},
                "telemetry_scope": "client_only",
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "index.html").write_text("<html><body>legacy-html-no-marker</body></html>", encoding="utf-8")
    await save_task(task)

    fake_template = (
        f"<!doctype html><html><head>{report_builder.REPORT_TEMPLATE_VERSION_MARKER}"
        "<title><!--__LENS_REPORT_TITLE__--></title></head>"
        '<body><script id="lens-report-data"><!--__LENS_REPORT_DATA__--></script>'
        "<script>/*__LENS_REPORT_CHARTS_JS__*/</script></body></html>"
    )
    monkeypatch.setattr(report_builder, "_load_reporting_assets", lambda: (fake_template, "window.__CHARTS_OK__=1;"))

    html_path, _, _ = await resolve_task_artifact(task, "report_html")
    rebuilt_html = html_path.read_text(encoding="utf-8")
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER in rebuilt_html
    embedded = _extract_embedded_report_json(rebuilt_html)
    assert embedded["client_series"]["drift_histogram"] == [{"label": "<10ms", "count": 4}]

    # Bundle path also rebuilds a stale HTML report lacking the marker
    (report_dir / "index.html").write_text("<html><body>legacy-again</body></html>", encoding="utf-8")
    bundle_path = build_bundle_tar_gz(task)
    with tarfile.open(bundle_path, "r:gz") as tar:
        bundled_html = tar.extractfile("aiperf-00000010/report/index.html").read().decode("utf-8")
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER in bundled_html


@pytest.mark.asyncio
async def test_unsanitized_server_telemetry_refreshes_on_bundle_and_artifact_download_without_raw_records(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsanitized server-telemetry.json must trigger sanitization and forensics rebuild even when records are absent."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000011")
    report_dir = Path(task.task_dir) / "artifacts" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    v2_cs = _sample_client_series()
    v2_cs["schema_version"] = 2
    v2_cs["duration_seconds"] = 32.0
    v2_cs["origin_epoch"] = 1700000000.0
    v2_cs["windows"] = {
        "full": {"request_count": 46, "successful_requests": 46, "error_rate_pct": 0.0, "slo_attainment_pct": 100.0}
    }
    dirty_server = {
        "status": "available",
        "available": True,
        "topology_mode": "disaggregated",
        "step_seconds": 5,
        "per_pod": [
            {"pod": "llm-d-lens-sim-deadbeef-abc", "role": "model-server", "request_rate_rps": {"mean": 0.0}},
            {"pod": "demo-epp-5c569b-xyz", "role": "model-server", "request_rate_rps": {"mean": 1.2}},
            {"pod": "demo-decode-545f64-uvw", "role": "decode", "request_rate_rps": {"mean": 1.2}},
        ],
        "derived": {
            "request_destination_distribution": [
                {"pod": "demo-epp-5c569b-xyz", "request_rate_rps": 1.2, "percent": 50.0},
                {"pod": "demo-decode-545f64-uvw", "request_rate_rps": 1.2, "percent": 50.0},
            ]
        },
        "series": [
            {
                "timestamp": "2023-11-14T22:13:22Z",
                "running_requests": 4.0,
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 20.0,
            }
        ],
    }
    stale_forensics = {
        "schema_version": 2,
        "verdict": {"code": "healthy"},
        "telemetry_scope": "full",
        "pod_skew": {"pods": [{"pod": "llm-d-lens-sim-deadbeef-abc"}]},
    }
    fake_template = (
        f"<!doctype html><html><head>{report_builder.REPORT_TEMPLATE_VERSION_MARKER}"
        "<title><!--__LENS_REPORT_TITLE__--></title></head>"
        '<body><script id="lens-report-data"><!--__LENS_REPORT_DATA__--></script>'
        "<script>/*__LENS_REPORT_CHARTS_JS__*/</script></body></html>"
    )
    monkeypatch.setattr(report_builder, "_load_reporting_assets", lambda: (fake_template, "window.__CHARTS_OK__=1;"))

    def _seed_dirty() -> None:
        (report_dir / "client-series.json").write_text(json.dumps(v2_cs), encoding="utf-8")
        (report_dir / "forensics.json").write_text(json.dumps(stale_forensics), encoding="utf-8")
        (report_dir / "server-telemetry.json").write_text(json.dumps(dirty_server), encoding="utf-8")
        (report_dir / "index.html").write_text(
            f"<html>{report_builder.REPORT_TEMPLATE_VERSION_MARKER}<body>stale-srv</body></html>",
            encoding="utf-8",
        )

    await save_task(task)

    # 1. Direct build_bundle_tar_gz sanitizes server-telemetry.json & rebuilds forensics/HTML even with v2+v3 & no records
    _seed_dirty()
    bundle_path = build_bundle_tar_gz(task)
    with tarfile.open(bundle_path, "r:gz") as tar:
        b_srv = json.loads(tar.extractfile("aiperf-00000011/timeseries/server-telemetry.json").read().decode("utf-8"))
        b_fr = json.loads(tar.extractfile("aiperf-00000011/forensics.json").read().decode("utf-8"))
    assert b_srv["topology_mode"] == "aggregated"
    assert [p["pod"] for p in b_srv["per_pod"]] == ["demo-epp-5c569b-xyz", "demo-decode-545f64-uvw"]
    assert [p["role"] for p in b_srv["per_pod"]] == ["epp", "worker"]
    assert [p["pod"] for p in b_fr["pod_skew"]["pods"]] == ["demo-decode-545f64-uvw"]

    # 2. resolve_task_artifact refreshes dirty server-telemetry.json without failing when index.html is a non-HTML symlink
    _seed_dirty()
    (report_dir / "index.html").unlink()
    (report_dir / "index.html").symlink_to("/etc/passwd")
    srv_path, _, _ = await resolve_task_artifact(task, "server_telemetry")
    persisted_srv = json.loads(srv_path.read_text(encoding="utf-8"))
    assert persisted_srv["topology_mode"] == "aggregated"
    assert [p["pod"] for p in persisted_srv["per_pod"]] == ["demo-epp-5c569b-xyz", "demo-decode-545f64-uvw"]
    rebuilt_fr = json.loads((report_dir / "forensics.json").read_text(encoding="utf-8"))
    assert [p["pod"] for p in rebuilt_fr["pod_skew"]["pods"]] == ["demo-decode-545f64-uvw"]


def test_render_html_report_preserves_dispatch_duration_and_fidelity_series(sim_env: Path) -> None:
    task = _make_task("00000012")
    cs = _sample_client_series()
    cs["schema_version"] = 3
    cs["dispatch_duration_seconds"] = 19.674
    cs["fidelity_series"] = [
        {
            "start_seconds": 0.0,
            "end_seconds": 1.0,
            "mid_seconds": 0.5,
            "is_warmup": True,
            "target_arrival_rps": 5.0,
            "arrived_rps": 5.0,
            "arrived_requests": 5,
            "drift_ms": {"mean": 1.2, "p50": 1.0, "p90": 2.0, "p99": 2.5},
        }
    ]
    from llm_d_lens.simulation.telemetry_post import REPORT_SCHEMA_VERSION

    html_text = render_html_report(task, client_series=cs)
    embedded = _extract_embedded_report_json(html_text)
    assert embedded["schema_version"] == REPORT_SCHEMA_VERSION
    assert embedded["client_series"]["dispatch_duration_seconds"] == 19.674
    assert embedded["client_series"]["fidelity_series"] == cs["fidelity_series"]


@pytest.mark.asyncio
async def test_bundle_and_artifact_refresh_stale_v2_server_telemetry_with_prom_fallback(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.simulation import telemetry_post
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000013", endpoint_cluster_id="c-1")
    task = task.model_copy(update={"endpoint_mode": "in-cluster", "endpoint_namespace": "ns-1"})
    report_dir = Path(task.task_dir) / "artifacts" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    fake_template = (
        f"<!doctype html><html><head>{report_builder.REPORT_TEMPLATE_VERSION_MARKER}"
        "<title><!--__LENS_REPORT_TITLE__--></title></head>"
        '<body><script id="lens-report-data"><!--__LENS_REPORT_DATA__--></script>'
        "<script>/*__LENS_REPORT_CHARTS_JS__*/</script></body></html>"
    )
    monkeypatch.setattr(report_builder, "_load_reporting_assets", lambda: (fake_template, "window.__CHARTS_OK__=1;"))

    v2_cs = _sample_client_series()
    v2_cs["schema_version"] = 2
    v2_cs["duration_seconds"] = 30.0
    v2_cs["windows"] = {
        "full": {
            "request_count": 20,
            "successful_requests": 20,
            "error_rate_pct": 0.0,
            "slo_attainment_pct": 100.0,
        }
    }
    v2_srv = {
        "schema_version": 2,
        "status": "available",
        "step_seconds": 5,
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:30Z"},
        "nodes": ["node-1"],
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "host_cpu_usage_percent": 40.0}],
        "summary": {"host_cpu_usage_percent": {"mean": 40.0, "max": 72.0}},
    }

    def _seed_v2() -> None:
        (report_dir / "client-series.json").write_text(json.dumps(v2_cs), encoding="utf-8")
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": 2, "verdict": {"code": "healthy"}, "telemetry_scope": "full"}),
            encoding="utf-8",
        )
        (report_dir / "server-telemetry.json").write_text(json.dumps(v2_srv), encoding="utf-8")
        (report_dir / "index.html").write_text("<html><body>v3-html</body></html>", encoding="utf-8")

    await save_task(task)

    # 1. Bundle refresh when Prometheus fails keeps saved series and upgrades to v3/v4
    _seed_v2()

    async def failing_prom(*_args, **_kwargs):
        raise RuntimeError("Prometheus unreachable")

    monkeypatch.setattr(telemetry_post, "collect_namespace_observability", failing_prom)
    bundle_path = build_bundle_tar_gz(task)
    with tarfile.open(bundle_path, "r:gz") as tar:
        b_srv = json.loads(tar.extractfile("aiperf-00000013/timeseries/server-telemetry.json").read().decode("utf-8"))
        b_fr = json.loads(tar.extractfile("aiperf-00000013/forensics.json").read().decode("utf-8"))
        b_html = tar.extractfile("aiperf-00000013/report/index.html").read().decode("utf-8")
    assert b_srv["schema_version"] == telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION
    assert len(b_srv["series"]) == 1
    assert b_srv["system"]["cpu_peak_pct"] == 72.0
    assert b_fr["schema_version"] == telemetry_post.FORENSICS_SCHEMA_VERSION
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER in b_html

    # 2. resolve_task_artifact("report_bundle") with fresh Prometheus data updates persisted JSONs and HTML
    _seed_v2()

    async def fresh_prom(cluster_id, namespace, start_iso, end_iso):
        assert (cluster_id, namespace, start_iso, end_iso) == (
            "c-1",
            "ns-1",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:30Z",
        )
        return {
            "status": "available",
            "step_seconds": 5,
            "window": {"start": start_iso, "end": end_iso},
            "nodes": ["node-1"],
            "series": [{"timestamp": "2026-01-01T00:00:00Z", "kv_offload_bytes_per_second": 200.0}],
            "summary": {"kv_offload_bytes_per_second": {"mean": 200.0}},
        }

    monkeypatch.setattr(telemetry_post, "collect_namespace_observability", fresh_prom)
    resolved_bundle, _, _ = await resolve_task_artifact(task, "report_bundle")
    with tarfile.open(resolved_bundle, "r:gz") as tar:
        b2_srv = json.loads(tar.extractfile("aiperf-00000013/timeseries/server-telemetry.json").read().decode("utf-8"))
    assert b2_srv["schema_version"] == telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION
    assert b2_srv["derived"]["offloaded_bytes"] == 1000
    assert (
        json.loads((report_dir / "server-telemetry.json").read_text(encoding="utf-8"))["schema_version"]
        == telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION
    )


def test_html_report_v7_preserves_kv_block_eviction_and_residency_telemetry(sim_env: Path) -> None:
    """Offline v7 HTML report preserves KV block eviction/residency fields and renders #s-evict charts/help."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000015")
    server = _sample_server_telemetry()
    server["cache_config"] = {
        "kv_cache_metrics_enabled": True,
        "kv_cache_metrics_sample": 0.01,
    }
    server["derived"] = {
        "kv_blocks_evicted_total": 4200,
    }
    server["availability"] = {
        "metrics": {
            "kv_blocks_evicted_per_minute": "available",
            "kv_block_lifetime_seconds": "available",
            "kv_block_idle_before_evict_seconds": "available",
            "kv_block_reuse_gap_seconds": "available",
        }
    }
    server["summary"].update(
        {
            "kv_blocks_evicted_per_minute": {
                "mean": 180.0,
                "max": 320.0,
                "min": 60.0,
                "latest": 150.0,
                "p50": 175.0,
                "p95": 300.0,
                "p99": 320.0,
            },
            "kv_block_idle_before_evict_p50_seconds": {"mean": 12.4, "max": 18.0},
            "kv_block_idle_before_evict_p90_seconds": {"mean": 45.2, "max": 60.0},
            "kv_block_idle_before_evict_p99_seconds": {"mean": 88.5, "max": 110.0},
            "kv_block_idle_before_evict_seconds": {"p50": 12.4, "p90": 45.2, "p99": 88.5},
            "kv_block_lifetime_p50_seconds": {"mean": 62.0, "max": 80.0},
            "kv_block_lifetime_p90_seconds": {"mean": 140.0, "max": 190.0},
            "kv_block_lifetime_p99_seconds": {"mean": 210.0, "max": 260.0},
            "kv_block_lifetime_seconds": {"p50": 62.0, "p90": 140.0, "p99": 210.0},
            "kv_block_reuse_gap_p50_seconds": {"mean": 3.5, "max": 5.0},
            "kv_block_reuse_gap_p90_seconds": {"mean": 14.8, "max": 21.0},
            "kv_block_reuse_gap_p99_seconds": {"mean": 28.0, "max": 35.0},
            "kv_block_reuse_gap_seconds": {"p50": 3.5, "p90": 14.8, "p99": 28.0},
        }
    )
    server["series"] = [
        {
            "t": 0.0,
            "kv_cache_usage_percent": 40.0,
            "kv_blocks_evicted_per_minute": 120.0,
            "kv_block_idle_before_evict_p50_seconds": 10.0,
            "kv_block_idle_before_evict_p90_seconds": 40.0,
            "kv_block_idle_before_evict_p99_seconds": 80.0,
            "kv_block_lifetime_p50_seconds": 55.0,
            "kv_block_lifetime_p90_seconds": 130.0,
            "kv_block_lifetime_p99_seconds": 200.0,
            "kv_block_reuse_gap_p50_seconds": 3.1,
            "kv_block_reuse_gap_p90_seconds": 13.5,
            "kv_block_reuse_gap_p99_seconds": 25.0,
        },
        {
            "t": 5.0,
            "kv_cache_usage_percent": 75.0,
            "kv_blocks_evicted_per_minute": 320.0,
            "kv_block_idle_before_evict_p50_seconds": 14.8,
            "kv_block_idle_before_evict_p90_seconds": 50.4,
            "kv_block_idle_before_evict_p99_seconds": 97.0,
            "kv_block_lifetime_p50_seconds": 69.0,
            "kv_block_lifetime_p90_seconds": 150.0,
            "kv_block_lifetime_p99_seconds": 220.0,
            "kv_block_reuse_gap_p50_seconds": 3.9,
            "kv_block_reuse_gap_p90_seconds": 16.1,
            "kv_block_reuse_gap_p99_seconds": 31.0,
        },
    ]

    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=server,
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    embedded = _extract_embedded_report_json(html_text)
    st = embedded["server_telemetry"]
    assert st["cache_config"]["kv_cache_metrics_enabled"] is True
    assert st["cache_config"]["kv_cache_metrics_sample"] == 0.01
    assert st["derived"]["kv_blocks_evicted_total"] == 4200
    assert st["summary"]["kv_blocks_evicted_per_minute"]["max"] == 320.0
    assert st["summary"]["kv_block_idle_before_evict_seconds"]["p50"] == 12.4
    assert st["summary"]["kv_block_lifetime_seconds"]["p90"] == 140.0
    assert st["summary"]["kv_block_reuse_gap_seconds"]["p99"] == 28.0
    assert st["series"][1]["kv_blocks_evicted_per_minute"] == 320.0
    assert st["series"][1]["kv_block_idle_before_evict_p90_seconds"] == 50.4
    assert st["series"][1]["kv_block_lifetime_p50_seconds"] == 69.0
    assert st["series"][1]["kv_block_reuse_gap_p50_seconds"] == 3.9

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    for expected_snippet in (
        "KV Block Eviction Rate (blocks/min)",
        "KV Block Idle Time Before Eviction (s)",
        "KV Block Lifetime (s)",
        "KV Block Reuse Gap (s)",
        "--kv-cache-metrics",
        "--kv-cache-metrics-sample 0.01",
        "vllm:kv_block_idle_before_evict_seconds",
        "vllm:kv_block_lifetime_seconds",
        "vllm:kv_block_reuse_gap_seconds",
    ):
        assert expected_snippet in charts_js

    for expected_help_key in (
        "s-evict",
        "KV Eviction · Block Churn, Residency & Preemptions",
        "KV Block Eviction Rate",
        "KV Block Idle Time Before Eviction (P50 / P90)",
        "KV Block Lifetime (P50 / P90)",
        "KV Block Reuse Gap (P50 / P90)",
        "KV Block Eviction Rate (blocks/min)",
        "KV Block Idle Time Before Eviction (s)",
        "KV Block Lifetime (s)",
        "KV Block Reuse Gap (s)",
    ):
        assert f'"{expected_help_key}":' in html_text


def test_html_report_stacked_area_charts_and_prefill_decode_naming(sim_env: Path) -> None:
    """Offline HTML report renders v8 StackedAreaChart wiring and Prefill/Decode naming alignment."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000018")
    server = _sample_server_telemetry()
    server["series"] = [
        {
            "t": 0.0,
            "router_scheduler_latency_p50_ms": None,
            "vllm_queue_time_p50_ms": 4.0,
            "vllm_prefill_time_p50_ms": 28.0,
            "vllm_decode_time_p50_ms": 210.0,
            "engine_prompt_local_cache_tps": 420.0,
            "engine_prompt_external_tps": None,
            "engine_prompt_local_compute_tps": 580.0,
            "input_token_rate_tps": 1000.0,
            "output_token_rate_tps": 260.0,
        },
        {
            "t": 5.0,
            "router_scheduler_latency_p50_ms": None,
            "vllm_queue_time_p50_ms": None,
            "vllm_prefill_time_p50_ms": None,
            "vllm_decode_time_p50_ms": None,
            "engine_prompt_local_cache_tps": None,
            "engine_prompt_external_tps": None,
            "engine_prompt_local_compute_tps": None,
            "input_token_rate_tps": None,
            "output_token_rate_tps": None,
        },
        {
            "t": 10.0,
            "router_scheduler_latency_p50_ms": 3.5,
            "vllm_queue_time_p50_ms": 9.0,
            "vllm_prefill_time_p50_ms": 36.0,
            "vllm_decode_time_p50_ms": 245.0,
            "engine_prompt_local_cache_tps": 500.0,
            "engine_prompt_external_tps": 110.0,
            "engine_prompt_local_compute_tps": 490.0,
            "input_token_rate_tps": 1100.0,
            "output_token_rate_tps": 310.0,
        },
    ]

    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=server,
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    assert _EXPECTED_CSP in html_text
    assert ".tooltip.stack-tip" in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")

    # StackedAreaChart implementation: partial-null guard, gap split, single-point fallback, Total tooltip
    assert "function StackedAreaChart(host, opts)" in charts_js
    assert "function showStackedTip(" in charts_js
    assert "m.has(x) ? m.get(x) : 0" in charts_js
    assert "if (!anyFinite)" in charts_js
    assert "if (row.isGap)" in charts_js
    assert "if (seg.length === 1)" in charts_js
    assert "rawMaxL > minL ? rawMaxL * 1.12 : minL + 1" in charts_js
    assert "if (!layerHasData && !(sIdx === 0 && segAllZero)) return;" in charts_js
    assert "if (!rect || !rect.width) return;" in charts_js
    assert 'textContent: "Total"' in charts_js
    assert '"fill-opacity": fillOpacity' in charts_js
    assert '"stroke-width": 1.75' in charts_js

    # All 4 charts wired to StackedAreaChart in bottom-to-top logical stacking order
    stage_layers_match = re.search(
        r"function offlineStageAreaLayers\(source,\s*hasKvTransfer\)\s*\{(?P<body>.*?)\n  \}",
        charts_js,
        re.DOTALL,
    )
    assert stage_layers_match is not None
    stage_body = stage_layers_match.group("body")
    assert "function buildOfflineStageLatencyView(" in charts_js
    assert "StackedAreaChart(stageHost," in charts_js
    stage_labels = re.findall(r'label:\s*"([^"]+)"', stage_body)
    assert "EPP Scheduling" in stage_labels
    assert "Queue Time" in stage_labels
    assert "Prefill Time" in stage_labels
    assert "Decode Time" in stage_labels
    assert 'chartCard("Request Stage Latency Breakdown", stageWrap' in charts_js

    split_match = re.search(
        r"StackedAreaChart\(promptSplitHost,\s*\{(?P<body>.*?)\}\);\s*const promptSplitCard",
        charts_js,
        re.DOTALL,
    )
    assert split_match is not None
    split_labels = re.findall(r'label:\s*"([^"]+)"', split_match.group("body"))
    assert split_labels == ["Prefix Cache (tok/s)", "Prefill Compute (tok/s)"]
    assert 'chartCard("Prompt Token Throughput: Prefix Cache vs Prefill Compute", promptSplitHost)' in charts_js

    hit_match = re.search(
        r"StackedAreaChart\(hitHost,\s*\{(?P<body>.*?)\}\);\s*StackedAreaChart\(srcTokHost",
        charts_js,
        re.DOTALL,
    )
    assert hit_match is not None
    hit_body = hit_match.group("body")
    assert "yMin: 0" in hit_body
    assert "yMax: 100" in hit_body
    assert "yTicks: [0, 25, 50, 75, 100]" in hit_body
    assert 'yUnit: "%"' in hit_body
    hit_labels = re.findall(r'label:\s*"([^"]+)"', hit_body)
    assert hit_labels == [
        "Local Prefix Cache (%)",
        "External KV Transfer (%)",
        "Recomputed Prefix (%)",
        "Uncached Prompt (%)",
    ]
    assert 'chartCard("Prefix Cache Hit Rate over Time", hitHost' in charts_js
    assert "Local Prefix Cache · External KV Transfer · Recomputed Prefix · Uncached Prompt (%)" in charts_js
    assert "Uncached Prompt (%)" in html_text
    assert "Recomputed Prefix (%)" in html_text

    src_match = re.search(
        r"StackedAreaChart\(srcTokHost,\s*\{(?P<body>.*?)\}\);\s*pageNode\.appendChild",
        charts_js,
        re.DOTALL,
    )
    assert src_match is not None
    src_labels = re.findall(r'label:\s*"([^"]+)"', src_match.group("body"))
    assert src_labels == ["Local Prefix Cache", "External KV Transfer", "Prefill Compute"]
    assert 'chartCard("Prompt Token Sources over Time", srcTokHost)' in charts_js

    # #s-engine Prefill vs Decode naming harmonization
    assert 'kpiTile("Prefill Throughput"' in charts_js
    assert 'kpiTile("Decode Throughput"' in charts_js
    assert 'chartCard("Prefill vs Decode Token Throughput", engTokHost)' in charts_js
    assert '{ label: "Prefill (tok/s)"' in charts_js
    assert '{ label: "Decode (tok/s)"' in charts_js
    assert '{ key: "in_tps", label: "Prefill (tok/s)"' in charts_js
    assert '{ key: "out_tps", label: "Decode (tok/s)"' in charts_js

    # Exact HELP_TEXTS alignment for every renamed/new chart title, KPI tile, and table header
    for expected_help_key in (
        "Request Stage Latency Breakdown",
        "Prompt Token Throughput: Prefix Cache vs Prefill Compute",
        "Prefix Cache Hit Rate over Time",
        "Prompt Token Sources over Time",
        "Prefill Throughput",
        "Decode Throughput",
        "Prefill vs Decode Token Throughput",
        "Prefill (tok/s)",
        "Decode (tok/s)",
    ):
        assert f'"{expected_help_key}":' in html_text


def test_html_report_v9_token_flow_charts_full_width_and_csp(sim_env: Path) -> None:
    """Offline v9 HTML report uses full width, passes CSP checks, and renders the 3 token-flow charts."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000019")
    server = _sample_server_telemetry()
    server["topology_mode"] = "disaggregated"
    server["derived"] = {
        "token_flow": {
            "sent_tokens": 100000,
            "cached_tokens": 42000,
            "recomputed_tokens": 18000,
            "cold_new_tokens": 40000,
            "cached_pct": 42.0,
            "recomputed_pct": 18.0,
            "cold_new_pct": 40.0,
            "theoretical_cached_tokens": 60000,
            "theoretical_ceiling_pct": 60.0,
        },
        "prompt_tokens_per_request": {
            "mean_prompt_tokens": 1200.5,
            "mean_cached_tokens": 480.2,
            "mean_computed_tokens": 720.3,
            "cached_pct": 40.0,
            "prompt_p50": 1100.0,
            "prompt_p90": 2400.0,
            "computed_p50": 650.0,
            "computed_p90": 1500.0,
            "source": "vllm_histogram",
        },
        "prompt_tokens_by_role_source": [
            {
                "role": "Prefill",
                "local_cache_tokens": 30000,
                "external_kv_tokens": 2000,
                "local_compute_tokens": 48000,
                "total_tokens": 80000,
            },
            {
                "role": "Decode",
                "local_cache_tokens": 10000,
                "external_kv_tokens": 5000,
                "local_compute_tokens": 5000,
                "total_tokens": 20000,
            },
        ],
    }

    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=server,
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    assert _EXPECTED_CSP in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    # Full-width layout (.content max-width: none) and horizontal bar CSS classes
    assert re.search(r"\.content\s*\{[^}]*max-width:\s*none;", html_text)
    assert "max-width: 1480px;" not in html_text
    for css_cls in (".hbar-track", ".hbar-ceiling", ".stat-strip", ".role-src-row"):
        assert css_cls in html_text

    # Exact 3 chart titles, HELP_TEXTS keys, and renderer helpers in embedded JS
    for expected_key in (
        "Prompt Token Breakdown",
        "Prompt Tokens per Request",
        "Prompt Token Sources by Role",
    ):
        assert f'"{expected_key}":' in html_text
        assert f'chartCard("{expected_key}"' in html_text

    assert "function HorizontalStackedBar(" in html_text
    assert "function deriveTokenFlowData(" in html_text
    assert "csTf.client_cached_tokens" in html_text
    assert "csTf.raw_session_reusable_tokens" in html_text
    assert "function derivePromptTokensPerReqData(" in html_text
    assert "function deriveRoleTokenSourcesData(" in html_text

    # Placed above podTable inside #s-engine
    idx_chart1 = html_text.index('chartCard("Prompt Token Breakdown"')
    idx_chart2 = html_text.index('chartCard("Prompt Tokens per Request"')
    idx_chart3 = html_text.index('chartCard("Prompt Token Sources by Role"')
    idx_pod_table = html_text.index("podTable,\n        ])")
    assert idx_chart1 < idx_chart2 < idx_chart3 < idx_pod_table

    embedded = _extract_embedded_report_json(html_text)
    derived = embedded["server_telemetry"]["derived"]
    assert derived["token_flow"] == server["derived"]["token_flow"]
    assert derived["prompt_tokens_per_request"] == server["derived"]["prompt_tokens_per_request"]
    assert derived["prompt_tokens_by_role_source"] == server["derived"]["prompt_tokens_by_role_source"]


def test_refresh_stale_report_for_bundle_passes_sessions_summary(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_refresh_stale_report_for_bundle computes/loads sessions_summary before refreshing server telemetry."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("0000001a", backend="aiperf")
    backend_dir = Path(task.task_dir) / "artifacts" / "aiperf"
    report_dir = Path(task.task_dir) / "artifacts" / "report"
    backend_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    origin_ns = 1_795_597_273_000_000_000
    (backend_dir / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "metadata": {
                    "request_id": "r-1",
                    "benchmark_phase": "profiling",
                    "request_start_ns": origin_ns,
                    "request_end_ns": origin_ns + 1_000_000_000,
                },
                "metrics": {
                    "input_sequence_length": {"value": 64},
                    "output_sequence_length": {"value": 16},
                    "time_to_first_token": {"value": 100.0},
                    "inter_token_latency": {"value": 10.0},
                    "request_latency": {"value": 500.0},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (report_dir / "client-series.json").write_text(
        json.dumps({"schema_version": 1, "buckets": [], "windows": {"full": {"request_count": 1}}}),
        encoding="utf-8",
    )
    (report_dir / "server-telemetry.json").write_text(
        json.dumps({"schema_version": 1, "status": "available", "series": [], "summary": {}}),
        encoding="utf-8",
    )
    (report_dir / "index.html").write_text("<html>stale</html>", encoding="utf-8")

    captured: dict = {}

    async def spy_refresh(t, srv, cs, *, sessions_summary=None):
        captured["sessions_summary"] = sessions_summary
        return srv

    monkeypatch.setattr(report_builder, "refresh_persisted_server_telemetry", spy_refresh)
    monkeypatch.setattr(
        report_builder,
        "correlate_trace_sessions",
        lambda *_args, **_kwargs: {"available": True, "theoretical_prefix_overlap_pct": 55.0, "lanes": []},
    )

    report_builder._refresh_stale_report_for_bundle(
        task,
        Path(task.task_dir),
        report_dir,
        report_builder._task_root(),
    )

    assert captured["sessions_summary"] == {
        "available": True,
        "theoretical_prefix_overlap_pct": 55.0,
        "lanes": [],
    }


def test_html_report_v10_system_page_per_node_and_trimming(sim_env: Path) -> None:
    """Offline v10 HTML report preserves per-node telemetry, bounds oversized payloads, and wires #page-system."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("0000001b")
    server = _sample_server_telemetry()
    server["hardware_profile"] = {
        "id": "amd-mi300x",
        "display_name": "AMD Instinct MI300X",
        "vendor": "amd",
        "metric_group": "gpu",
        "ui": {"labels": {"metric_group": "XPU"}},
    }
    server["system"] = {
        "available": True,
        "nodes": ["node-a", "node-b"],
        "cpu_avg_pct": 42.5,
        "cpu_peak_pct": 78.0,
        "mem_used_bytes": 120 * 1024**3,
        "mem_available_bytes": 392 * 1024**3,
        "mem_avail_bytes": 392 * 1024**3,
        "mem_avail_min_bytes": 360 * 1024**3,
        "mem_total_bytes": 512 * 1024**3,
        "mem_usage_pct": 23.4,
        "mem_peak_pct": 29.7,
        "disk_avail_bytes": 450 * 1024**3,
        "disk_total_bytes": 1000 * 1024**3,
        "disk_read_bps": 15.0 * 1024**2,
        "disk_write_bps": 8.0 * 1024**2,
        "net_rx_bps": 120.0 * 1024**2,
        "net_rx_peak_bps": 240.0 * 1024**2,
        "net_tx_bps": 95.0 * 1024**2,
        "net_tx_peak_bps": 190.0 * 1024**2,
        "host_power_watts": 920.0,
        "host_peak_power_watts": 1180.0,
        "host_energy_wh": 15.4,
    }
    server["per_node"] = [
        {
            "node": "node-a",
            "roles": ["prefill"],
            "pod_count": 2,
            "pods": ["vllm-prefill-0", "vllm-prefill-1"],
            "allocated_accelerators": 4,
            "cpu_usage_percent": {"mean": 51.0, "max": 82.0},
            "memory_used_bytes": {"mean": 64 * 1024**3, "max": 72 * 1024**3},
            "memory_available_bytes": {"mean": 192 * 1024**3, "min": 180 * 1024**3},
            "memory_total_bytes": 256 * 1024**3,
            "memory_usage_percent": {"mean": 25.0, "max": 28.1},
            "disk_read_bytes_per_second": {"mean": 10.0 * 1024**2, "max": 22.0 * 1024**2},
            "disk_written_bytes_per_second": {"mean": 5.0 * 1024**2, "max": 11.0 * 1024**2},
            "filesystem_avail_bytes": 200 * 1024**3,
            "filesystem_size_bytes": 500 * 1024**3,
            "network_receive_bytes_per_second": {"mean": 70.0 * 1024**2, "max": 130.0 * 1024**2},
            "network_transmit_bytes_per_second": {"mean": 55.0 * 1024**2, "max": 110.0 * 1024**2},
            "host_power_watts": {"mean": 480.0, "max": 610.0},
            "gpu_utilization_percent": {"mean": 74.0, "max": 95.0},
            "gpu_power_watts": {"mean": 330.0, "max": 440.0},
            "gpu_temperature_celsius": {"mean": 62.0, "max": 71.0},
            "gpu_memory_used_bytes": {"mean": 48 * 1024**3, "max": 52 * 1024**3},
            "gpu_memory_total_bytes": 80 * 1024**3,
        },
        {
            "node": "node-b",
            "roles": ["decode"],
            "pod_count": 1,
            "pods": ["vllm-decode-0"],
            "allocated_accelerators": 4,
            "cpu_usage_percent": {"mean": 34.0, "max": 60.0},
            "memory_used_bytes": {"mean": 56 * 1024**3, "max": 64 * 1024**3},
            "memory_available_bytes": {"mean": 200 * 1024**3, "min": 190 * 1024**3},
            "memory_total_bytes": 256 * 1024**3,
            "memory_usage_percent": {"mean": 21.8, "max": 25.0},
            "disk_read_bytes_per_second": {"mean": 5.0 * 1024**2, "max": 9.0 * 1024**2},
            "disk_written_bytes_per_second": {"mean": 3.0 * 1024**2, "max": 7.0 * 1024**2},
            "filesystem_avail_bytes": 250 * 1024**3,
            "filesystem_size_bytes": 500 * 1024**3,
            "network_receive_bytes_per_second": {"mean": 50.0 * 1024**2, "max": 110.0 * 1024**2},
            "network_transmit_bytes_per_second": {"mean": 40.0 * 1024**2, "max": 80.0 * 1024**2},
            "host_power_watts": {"mean": 440.0, "max": 570.0},
            "gpu_utilization_percent": {"mean": 61.0, "max": 88.0},
            "gpu_power_watts": {"mean": 290.0, "max": 400.0},
            "gpu_temperature_celsius": {"mean": 58.0, "max": 67.0},
            "gpu_memory_used_bytes": {"mean": 42 * 1024**3, "max": 46 * 1024**3},
            "gpu_memory_total_bytes": 80 * 1024**3,
        },
    ]
    server["per_node_series"] = {
        "node-a": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "cpu_usage_percent": 48.0,
                "memory_available_bytes": 192 * 1024**3,
                "memory_used_bytes": 64 * 1024**3,
                "memory_usage_percent": 25.0,
                "disk_read_bytes_per_second": 10.0 * 1024**2,
                "disk_written_bytes_per_second": 5.0 * 1024**2,
                "network_receive_bytes_per_second": 70.0 * 1024**2,
                "network_transmit_bytes_per_second": 55.0 * 1024**2,
                "host_power_watts": 470.0,
                "gpu_power_watts": 320.0,
                "gpu_utilization_percent": 70.0,
            },
            {
                "timestamp": "2026-01-01T00:00:05Z",
                "cpu_usage_percent": 54.0,
                "memory_available_bytes": 188 * 1024**3,
                "memory_used_bytes": 68 * 1024**3,
                "memory_usage_percent": 26.5,
                "disk_read_bytes_per_second": 12.0 * 1024**2,
                "disk_written_bytes_per_second": 6.0 * 1024**2,
                "network_receive_bytes_per_second": 80.0 * 1024**2,
                "network_transmit_bytes_per_second": 62.0 * 1024**2,
                "host_power_watts": 490.0,
                "gpu_power_watts": 340.0,
                "gpu_utilization_percent": 78.0,
            },
        ],
        "node-b": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "cpu_usage_percent": 32.0,
                "memory_available_bytes": 200 * 1024**3,
                "memory_used_bytes": 56 * 1024**3,
                "memory_usage_percent": 21.8,
                "disk_read_bytes_per_second": 5.0 * 1024**2,
                "disk_written_bytes_per_second": 3.0 * 1024**2,
                "network_receive_bytes_per_second": 50.0 * 1024**2,
                "network_transmit_bytes_per_second": 40.0 * 1024**2,
                "host_power_watts": 430.0,
                "gpu_power_watts": 280.0,
                "gpu_utilization_percent": 60.0,
            },
        ],
    }

    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=server,
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    assert _EXPECTED_CSP in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    embedded = _extract_embedded_report_json(html_text)
    assert [n["node"] for n in embedded["server_telemetry"]["per_node"]] == ["node-a", "node-b"]
    assert len(embedded["server_telemetry"]["per_node_series"]["node-a"]) == 2
    assert len(embedded["server_telemetry"]["per_node_series"]["node-b"]) == 1

    # Oversized per_node & per_node_series are bounded cleanly by _compact_report_payload
    oversized = {
        "server_telemetry": {
            "per_node": [{"node": f"node-{i:02d}"} for i in range(60)],
            "per_node_series": {
                f"node-{i:02d}": [
                    {"timestamp": f"2026-01-01T00:{j // 60:02d}:{j % 60:02d}Z", "cpu_usage_percent": 10.0}
                    for j in range(500)
                ]
                for i in range(60)
            },
        }
    }
    compacted = report_builder._compact_report_payload(oversized, max_points=120, max_pod_series=16)
    assert len(compacted["server_telemetry"]["per_node"]) == 16
    assert len(compacted["server_telemetry"]["per_node_series"]) == 16
    assert all(len(pts) == 120 for pts in compacted["server_telemetry"]["per_node_series"].values())

    # Verify template & charts JS contain all required System page v10 elements and HELP_TEXTS
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")

    assert "#y-nodes" in template_html
    assert "#y-net" in template_html
    for expected_snippet in (
        "All Nodes (Aggregate)",
        "Per-Node Summary",
        "Host CPU Utilization",
        "Host Memory Usage",
        "Disk I/O & Capacity",
        "Network Throughput",
        "Host Power",
        "Power & Thermal",
        "Host Memory & Disk I/O",
        "Network RX / TX Throughput",
        "Roles & Pods",
        "CPU (Mean / Peak)",
        "Memory (Used / Total · Peak %)",
        "Disk I/O (R / W)",
        "Free Disk",
        "Network (RX / TX)",
        "Host Power (Mean / Peak)",
        "(Count · Util · Power · Temp)",
        "profile.ui.labels.metric_group",
    ):
        assert expected_snippet in charts_js

    for help_key in (
        "y-kpis",
        "y-nodes",
        "y-cpu",
        "y-mem-disk",
        "y-net",
        "y-power",
        "Host CPU Utilization",
        "Host Memory Usage",
        "Disk I/O & Capacity",
        "Network Throughput",
        "Host Power",
        "Accelerator Power & Thermal",
        "Per-Node Summary",
        "Roles & Pods",
        "CPU (Mean / Peak)",
        "Memory (Used / Total · Peak %)",
        "Disk I/O (R / W)",
        "Free Disk",
        "Network (RX / TX)",
        "Host Power (Mean / Peak)",
        "Accelerator (Count · Util · Power · Temp)",
        "Host Memory & Disk I/O",
        "Network RX / TX Throughput",
        "Host & Accelerator Power",
    ):
        assert f'"{help_key}":' in charts_js


def test_html_report_v10_js_syntax_and_runtime_execution(sim_env: Path) -> None:
    """Validate report_charts.js syntax with node --check and execute renderSystem in a minimal DOM harness."""
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node is unavailable in PATH")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    check = subprocess.run([node_bin, "--check", str(charts_js_path)], capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr

    task = _make_task("0000001c")
    server = _sample_server_telemetry()
    server["hardware_profile"] = {
        "id": "nvidia-h100",
        "display_name": "NVIDIA H100",
        "vendor": "nvidia",
        "metric_group": "gpu",
        "ui": {"labels": {"metric_group": "GPU"}},
    }
    server["system"] = {
        "available": True,
        "nodes": ["node-1", "node-2"],
        "cpu_avg_pct": 55.0,
        "cpu_peak_pct": 85.0,
        "mem_used_bytes": 100 * 1024**3,
        "mem_avail_bytes": 156 * 1024**3,
        "mem_avail_min_bytes": 140 * 1024**3,
        "mem_total_bytes": 256 * 1024**3,
        "mem_usage_pct": 39.0,
        "mem_peak_pct": 45.0,
        "disk_avail_bytes": 300 * 1024**3,
        "disk_total_bytes": 500 * 1024**3,
        "disk_read_bps": 12 * 1024**2,
        "disk_write_bps": 6 * 1024**2,
        "net_rx_bps": 80 * 1024**2,
        "net_rx_peak_bps": 150 * 1024**2,
        "net_tx_bps": 60 * 1024**2,
        "net_tx_peak_bps": 120 * 1024**2,
        "host_power_watts": 800.0,
        "host_peak_power_watts": 950.0,
        "host_energy_wh": 12.0,
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "host_cpu_usage_percent": 50.0,
                "host_memory_used_bytes": 98 * 1024**3,
                "host_memory_available_bytes": 158 * 1024**3,
                "host_disk_read_bytes_per_second": 10 * 1024**2,
                "host_disk_written_bytes_per_second": 5 * 1024**2,
                "host_network_receive_bytes_per_second": 75 * 1024**2,
                "host_network_transmit_bytes_per_second": 55 * 1024**2,
                "host_power_watts": 780.0,
            },
            {
                "timestamp": "2026-01-01T00:00:05Z",
                "host_cpu_usage_percent": 60.0,
                "host_memory_used_bytes": 102 * 1024**3,
                "host_memory_available_bytes": 154 * 1024**3,
                "host_disk_read_bytes_per_second": 14 * 1024**2,
                "host_disk_written_bytes_per_second": 7 * 1024**2,
                "host_network_receive_bytes_per_second": 85 * 1024**2,
                "host_network_transmit_bytes_per_second": 65 * 1024**2,
                "host_power_watts": 820.0,
            },
        ],
    }
    server["per_node"] = [
        {
            "node": "node-1",
            "roles": ["worker"],
            "pod_count": 1,
            "pods": ["vllm-qwen-pod-0"],
            "allocated_accelerators": 4,
            "cpu_usage_percent": {"mean": 60.0, "max": 85.0},
            "memory_used_bytes": {"mean": 60 * 1024**3, "max": 64 * 1024**3},
            "memory_available_bytes": {"mean": 68 * 1024**3, "min": 64 * 1024**3},
            "memory_total_bytes": 128 * 1024**3,
            "memory_usage_percent": {"mean": 46.8, "max": 50.0},
            "disk_read_bytes_per_second": {"mean": 8 * 1024**2, "max": 12 * 1024**2},
            "disk_written_bytes_per_second": {"mean": 4 * 1024**2, "max": 8 * 1024**2},
            "filesystem_avail_bytes": 140 * 1024**3,
            "filesystem_size_bytes": 250 * 1024**3,
            "network_receive_bytes_per_second": {"mean": 50 * 1024**2, "max": 90 * 1024**2},
            "network_transmit_bytes_per_second": {"mean": 35 * 1024**2, "max": 70 * 1024**2},
            "host_power_watts": {"mean": 450.0, "max": 520.0},
            "gpu_utilization_percent": {"mean": 78.0, "max": 96.0},
            "gpu_power_watts": {"mean": 320.0, "max": 410.0},
            "gpu_temperature_celsius": {"mean": 64.0, "max": 73.0},
        },
        {
            "node": "node-2",
            "roles": ["worker"],
            "pod_count": 1,
            "pods": ["vllm-qwen-pod-1"],
            "allocated_accelerators": 4,
            "cpu_usage_percent": {"mean": 50.0, "max": 72.0},
            "memory_used_bytes": {"mean": 40 * 1024**3, "max": 44 * 1024**3},
            "memory_available_bytes": {"mean": 88 * 1024**3, "min": 84 * 1024**3},
            "memory_total_bytes": 128 * 1024**3,
            "memory_usage_percent": {"mean": 31.2, "max": 34.3},
            "disk_read_bytes_per_second": {"mean": 4 * 1024**2, "max": 8 * 1024**2},
            "disk_written_bytes_per_second": {"mean": 2 * 1024**2, "max": 5 * 1024**2},
            "filesystem_avail_bytes": 160 * 1024**3,
            "filesystem_size_bytes": 250 * 1024**3,
            "network_receive_bytes_per_second": {"mean": 30 * 1024**2, "max": 60 * 1024**2},
            "network_transmit_bytes_per_second": {"mean": 25 * 1024**2, "max": 50 * 1024**2},
            "host_power_watts": {"mean": 350.0, "max": 430.0},
            "gpu_utilization_percent": {"mean": 62.0, "max": 85.0},
            "gpu_power_watts": {"mean": 260.0, "max": 350.0},
            "gpu_temperature_celsius": {"mean": 59.0, "max": 68.0},
        },
    ]
    server["per_node_series"] = {
        "node-1": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "cpu_usage_percent": 58.0,
                "memory_available_bytes": 68 * 1024**3,
                "memory_used_bytes": 60 * 1024**3,
                "memory_usage_percent": 46.8,
                "disk_read_bytes_per_second": 8 * 1024**2,
                "disk_written_bytes_per_second": 4 * 1024**2,
                "network_receive_bytes_per_second": 50 * 1024**2,
                "network_transmit_bytes_per_second": 35 * 1024**2,
                "host_power_watts": 440.0,
                "gpu_power_watts": 310.0,
                "gpu_utilization_percent": 75.0,
            }
        ],
        "node-2": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "cpu_usage_percent": 48.0,
                "memory_available_bytes": 88 * 1024**3,
                "memory_used_bytes": 40 * 1024**3,
                "memory_usage_percent": 31.2,
                "disk_read_bytes_per_second": 4 * 1024**2,
                "disk_written_bytes_per_second": 2 * 1024**2,
                "network_receive_bytes_per_second": 30 * 1024**2,
                "network_transmit_bytes_per_second": 25 * 1024**2,
                "host_power_watts": 340.0,
                "gpu_power_watts": 250.0,
                "gpu_utilization_percent": 60.0,
            }
        ],
    }

    embedded_payload = _extract_embedded_report_json(
        render_html_report(
            task,
            client_series=_sample_client_series(),
            server_telemetry=server,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const payloadJson = fs.readFileSync(0, "utf8");
function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    style: {},
    className: "",
    textContent: text || "",
    clientWidth: 640,
    classList: {
      add() {},
      remove() {},
      toggle() {},
    },
    setAttribute(k, v) {
      attrs[k] = String(v);
      if (k === "class") node.className = String(v);
    },
    getAttribute(k) {
      if (k === "class") return node.className;
      return attrs[k] ?? null;
    },
    appendChild(child) {
      if (child !== null && child !== undefined) {
        node.children.push(typeof child === "string" ? makeNode("#text", child) : child);
      }
      return child;
    },
    append(...items) {
      for (const item of items) {
        if (item !== null && item !== undefined) node.appendChild(item);
      }
    },
    replaceChildren(...items) {
      node.children = [];
      node.append(...items);
    },
    addEventListener(ev, fn) {
      (listeners[ev] = listeners[ev] || []).push(fn);
    },
    click() {
      for (const fn of listeners.click || []) fn({ target: node, stopPropagation() {}, preventDefault() {} });
    },
    closest() { return null; },
    querySelectorAll() { return []; },
    getContext() {
      return {
        scale() {}, fillRect() {}, fillText() {}, clearRect() {},
        beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
      };
    },
    getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 200 }; },
  };
  return node;
}
const ids = {
  "chart-tooltip": makeNode("div"),
  "report-root": makeNode("main"),
  "report-title": makeNode("span"),
  "report-meta": makeNode("div"),
  "status-badges": makeNode("span"),
  "lens-report-data": makeNode("script", payloadJson),
  "sidebar-toggle": makeNode("button"),
  "sidebar": makeNode("aside"),
  "theme-toggle": makeNode("button"),
  "print-btn": makeNode("button"),
  "window-toggle": makeNode("div"),
  "nav-list": makeNode("nav"),
};
const document = {
  documentElement: makeNode("html"),
  getElementById(id) { return ids[id] || makeNode("div"); },
  createElement(tag) { return makeNode(tag); },
  createElementNS(_ns, tag) { return makeNode(tag); },
  createTextNode(txt) { return makeNode("#text", txt); },
  querySelectorAll() { return []; },
};
const windowObj = {
  devicePixelRatio: 1,
  innerWidth: 1280,
  innerHeight: 800,
  print() {},
};
const context = {
  console,
  document,
  window: windowObj,
  getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
  URL,
  Blob,
  Response,
  DecompressionStream: globalThis.DecompressionStream,
  atob,
};
(async () => {
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
  await new Promise((r) => setTimeout(r, 20));
  const sysPage = ids["report-root"].children.find((n) => n.getAttribute("data-page") === "system");
  if (!sysPage) throw new Error("System page not rendered");
  const texts = [];
  const buttons = [];
  function walk(n) {
    if (!n) return;
    if (n.textContent) texts.push(n.textContent);
    if (n.tagName === "BUTTON") buttons.push(n);
    for (const c of n.children || []) walk(c);
  }
  walk(sysPage);
  const joined = texts.join("\n");
  for (const needle of [
    "All Nodes (Aggregate)",
    "Per-Node Summary",
    "Host CPU Utilization",
    "Host Memory Usage",
    "Disk I/O & Capacity",
    "Network Throughput",
    "Host Power",
    "GPU Power & Thermal",
    "Host Memory & Disk I/O",
    "Network RX / TX Throughput",
    "Host & GPU Power",
    "node-1",
    "node-2",
    "73 °C",
  ]) {
    if (!joined.includes(needle)) throw new Error("Missing rendered text: " + needle);
  }
  const initialSubsCount = Array.isArray(sysPage._systemSubs) ? sysPage._systemSubs.length : -1;
  if (initialSubsCount !== 4) {
    throw new Error("Expected 4 system crosshair subscribers, got " + initialSubsCount);
  }
  const node1Btn = buttons.find((b) => b.textContent === "node-1");
  if (!node1Btn) throw new Error("Missing node-1 scope button");
  node1Btn.click();
  const textsAfter = [];
  const buttonsAfter = [];
  (function walk2(n) {
    if (!n) return;
    if (n.textContent) textsAfter.push(n.textContent);
    if (n.tagName === "BUTTON") buttonsAfter.push(n);
    for (const c of n.children || []) walk2(c);
  })(sysPage);
  if (!textsAfter.join("\n").includes("Scoped to node node-1")) {
    throw new Error("Node scope drilldown did not update system page");
  }
  const allBtn = buttonsAfter.find((b) => b.textContent === "All Nodes (Aggregate)");
  if (!allBtn) throw new Error("Missing All Nodes button after node drilldown");
  allBtn.click();
  if (!Array.isArray(sysPage._systemSubs) || sysPage._systemSubs.length !== 4) {
    throw new Error("Crosshair subscribers leaked or changed unexpectedly");
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", harness, str(charts_js_path)],
        input=json.dumps(embedded_payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node runtime check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_html_report_v11_slot_lanes_and_requests_swimlane(sim_env: Path) -> None:
    """Offline v11 HTML report preserves slot_lanes, bounds oversized items, and renders Sessions over Time."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("0000001d")
    cs = _sample_client_series()
    cs["duration_seconds"] = 12.0
    cs["warmup_seconds"] = 3.0
    cs["slot_lanes"] = {
        "peak_slots": 2,
        "total_requests": 3,
        "session_count": 2,
        "sample_stride": 1,
        "tier_counts": {"cold": 1, "partial": 1, "warm": 1},
        "items": [
            {
                "slot": 0,
                "start_s": 0.5,
                "end_s": 2.5,
                "cache_hit_pct": 0.0,
                "tier": "cold",
                "session_id": "sess-1",
                "turn": 1,
                "input_tokens": 500,
                "output_tokens": 50,
                "ttft_ms": 120.0,
            },
            {
                "slot": 1,
                "start_s": 1.0,
                "end_s": 4.0,
                "cache_hit_pct": 45.5,
                "tier": "partial",
                "session_id": "sess-2",
                "turn": 1,
                "input_tokens": 600,
                "output_tokens": 80,
                "ttft_ms": 80.0,
            },
            {
                "slot": 0,
                "start_s": 3.5,
                "end_s": 6.0,
                "cache_hit_pct": 85.0,
                "tier": "warm",
                "session_id": "sess-1",
                "turn": 2,
                "input_tokens": 800,
                "output_tokens": 60,
                "ttft_ms": 35.0,
            },
        ],
    }

    html_text = render_html_report(
        task,
        client_series=cs,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    assert _EXPECTED_CSP in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    embedded = _extract_embedded_report_json(html_text)
    assert embedded["client_series"]["slot_lanes"] == cs["slot_lanes"]
    assert embedded["run_manifest"]["replay"]["fixed_schedule"] == task.prompt.trace.fixed_schedule

    # _compact_report_payload preserves slot_lanes and bounds > 2000 items cleanly (even with non-finite sample_stride)
    oversized_payload = {
        "client_series": {
            "slot_lanes": {
                "peak_slots": 4,
                "total_requests": 2501,
                "session_count": 10,
                "sample_stride": float("nan"),
                "tier_counts": {"cold": 834, "partial": 834, "warm": 833},
                "items": [
                    {
                        "slot": i % 4,
                        "start_s": float(i),
                        "end_s": float(i) + 0.5,
                        "cache_hit_pct": 75.0,
                        "tier": "warm",
                        "session_id": f"s-{i % 10}",
                        "turn": 1,
                        "input_tokens": 256,
                        "output_tokens": 32,
                        "ttft_ms": 30.0,
                    }
                    for i in range(2501)
                ],
            }
        }
    }
    compacted = report_builder._compact_report_payload(oversized_payload, max_points=60, max_lanes=100)
    compacted_sl = compacted["client_series"]["slot_lanes"]
    assert compacted_sl["peak_slots"] == 4
    assert compacted_sl["total_requests"] == 2501
    assert compacted_sl["session_count"] == 10
    assert compacted_sl["sample_stride"] == 2
    assert 0 < len(compacted_sl["items"]) <= 2000
    assert compacted_sl["items"][0]["input_tokens"] == 256
    assert compacted_sl["items"][0]["session_id"] == "s-0"

    # Verify report_charts.js markers, HELP_TEXTS, 3-tier labels/colors, and slot packing copy
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    charts_js = charts_js_path.read_text(encoding="utf-8")
    for snippet in (
        '"Sessions over Time":',
        "One row per concurrent session slot · Requests packed into slots · a slot is reused once its last request finished",
        "shaded span = warmup",
        "cold (<10%)",
        "partial (10–70%)",
        "warm (≥70%)",
        "#f85149",
        "#f59e0b",
        "#10b981",
        "slots at peak",
        "function greedyPackSlotTurns(",
        "function resolveSlotLanesData(",
    ):
        assert snippet in charts_js

    node_bin = shutil.which("node")
    if node_bin is None:
        return

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
function runCase(casePayload) {
  return (async () => {
  const payloadJson = JSON.stringify(casePayload);
  function makeNode(tag, text) {
    const listeners = {};
    const attrs = {};
    const node = {
      tagName: String(tag || "div").toUpperCase(),
      children: [],
      style: {},
      className: "",
      textContent: text || "",
      clientWidth: 640,
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
      getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
      appendChild(c) { if (c != null) node.children.push(typeof c === "string" ? makeNode("#text", c) : c); return c; },
      append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
      replaceChildren(...items) { node.children = []; node.append(...items); },
      addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
      closest() { return null; },
      querySelectorAll() { return []; },
      getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
      getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 200 }; },
    };
    return node;
  }
  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", payloadJson),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const document = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(txt) { return makeNode("#text", txt); },
    querySelectorAll() { return []; },
  };
  const context = {
    console,
    document,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
  await new Promise((r) => setTimeout(r, 20));
  const reqPage = ids["report-root"].children.find((n) => n.getAttribute("data-page") === "requests");
  if (!reqPage) throw new Error("Requests page not rendered");
  const texts = [];
  const fills = [];
  (function walk(n) {
    if (!n) return;
    if (n.textContent) texts.push(n.textContent);
    const fill = n.getAttribute("fill");
    if (fill) fills.push(fill);
    for (const c of n.children || []) walk(c);
  })(reqPage);
  return { text: texts.join("\n"), fills };
  })();
}
(async () => {
  const res1 = await runCase(payload);
  for (const needle of [
    "Sessions over Time",
    "One row per concurrent session slot · Requests packed into slots · a slot is reused once its last request finished · shaded span = warmup",
    "2 slots at peak · 3 requests · 2 sessions",
    "cold (<10%)",
    "partial (10–70%)",
    "warm (≥70%)",
    "warmup",
    "S1",
    "S2",
  ]) {
    if (!res1.text.includes(needle)) throw new Error("Missing rendered text in primary case: " + needle);
  }
  for (const color of ["#f85149", "#f59e0b", "#10b981"]) {
    if (!res1.fills.includes(color)) throw new Error("Missing tier SVG rect fill: " + color);
  }

  // Legacy fallback: no client_series.slot_lanes, pack from sessions_summary.sessions turns
  const legacyPayload = JSON.parse(JSON.stringify(payload));
  delete legacyPayload.client_series.slot_lanes;
  legacyPayload.client_series.warmup_seconds = 0;
  legacyPayload.task.warmup_seconds = 0;
  legacyPayload.sessions_summary = {
    available: true,
    sessions: [
      {
        session_id: "leg-1",
        turns: [
          { start_s: 0.0, end_s: 2.0, cache_hit_pct: 5.0, ttft_ms: 100.0 },
          { start_s: 2.5, end_s: 4.0, cache_hit_pct: 75.0, ttft_ms: 30.0 },
        ],
      },
      {
        session_id: "leg-2",
        turns: [{ start_s: 1.0, end_s: 3.0, cache_hit_pct: 35.0, ttft_ms: 60.0 }],
      },
    ],
  };
  const res2 = await runCase(legacyPayload);
  for (const needle of [
    "2 slots at peak · 3 requests · 2 sessions",
    "S1",
    "S2",
  ]) {
    if (!res2.text.includes(needle)) throw new Error("Missing rendered text in legacy fallback case: " + needle);
  }
  for (const color of ["#f85149", "#f59e0b", "#10b981"]) {
    if (!res2.fills.includes(color)) throw new Error("Missing tier SVG rect fill in legacy fallback: " + color);
  }

  // Empty slot_lanes.items should also fall back cleanly to sessions_summary.sessions
  const emptyItemsPayload = JSON.parse(JSON.stringify(legacyPayload));
  emptyItemsPayload.client_series.slot_lanes = { peak_slots: 0, total_requests: 0, session_count: 0, sample_stride: 1, tier_counts: {}, items: [] };
  const res3 = await runCase(emptyItemsPayload);
  if (!res3.text.includes("2 slots at peak · 3 requests · 2 sessions")) {
    throw new Error("Empty slot_lanes.items did not fall back to sessions_summary.sessions");
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v11 Requests swimlane runtime check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v12_unified_fine_grained_svg_heatmap(sim_env: Path) -> None:
    """Offline v12 HTML report preserves up to 48x28=1344 heatmap cells and renders the SVG HeatmapCanvas."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    def _make_heatmap_cells(rows: int, cols: int) -> list[dict]:
        cells = []
        for t_idx in range(cols):
            for l_idx in range(rows):
                avg_ms = 20.0 + t_idx * 1.5 + l_idx * 0.8
                cells.append(
                    {
                        "t_idx": t_idx,
                        "l_idx": l_idx,
                        "start_seconds": round(t_idx * 1.0, 2),
                        "end_seconds": round((t_idx + 1) * 1.0, 2),
                        "seq_start": 100 + l_idx * 32,
                        "seq_end": 100 + (l_idx + 1) * 32,
                        "count": 3,
                        "avg_ms": round(avg_ms, 3),
                        "p50_ms": round(avg_ms * 0.95, 3),
                        "p90_ms": round(avg_ms * 1.35, 3),
                        "max_ms": round(avg_ms * 1.6, 3),
                    }
                )
        return cells

    full_cells = _make_heatmap_cells(28, 48)
    assert len(full_cells) == 1344

    cs = _sample_client_series()
    cs["buckets"] = [{"t": float(i), "rps": 10.0, "ttft_p50_ms": 30.0} for i in range(400)]
    cs["heatmap"] = {
        "ttft": {
            "time_bins": 48,
            "len_bins": 28,
            "min_ms": 20.0,
            "max_ms": 112.1,
            "cells": full_cells,
        },
        "tpot": {
            "time_bins": 48,
            "len_bins": 28,
            "min_ms": 8.0,
            "max_ms": 32.0,
            "cells": _make_heatmap_cells(28, 48),
        },
    }
    cs["ttft_heatmap"] = {
        "time_bins": 48,
        "len_bins": 28,
        "min_ms": 20.0,
        "max_ms": 112.1,
        "cells": _make_heatmap_cells(28, 48),
    }
    cs["tpot_heatmap"] = {
        "time_bins": 48,
        "len_bins": 28,
        "min_ms": 8.0,
        "max_ms": 32.0,
        "cells": _make_heatmap_cells(28, 48),
    }

    # _compact_report_payload must NEVER truncate <= 1344 heatmap cells regardless of max_points
    compacted_small = report_builder._compact_report_payload({"client_series": cs}, max_points=30)
    assert len(compacted_small["client_series"]["buckets"]) == 30
    assert len(compacted_small["client_series"]["heatmap"]["ttft"]["cells"]) == 1344
    assert len(compacted_small["client_series"]["heatmap"]["tpot"]["cells"]) == 1344
    assert len(compacted_small["client_series"]["ttft_heatmap"]["cells"]) == 1344
    assert len(compacted_small["client_series"]["tpot_heatmap"]["cells"]) == 1344

    # Oversized > 1344 heatmap cells are capped via striding without failing
    oversized_cells = _make_heatmap_cells(30, 50)
    assert len(oversized_cells) == 1500
    compacted_over = report_builder._compact_report_payload(
        {"client_series": {"heatmap": {"ttft": {"cells": oversized_cells}}}},
        max_points=60,
    )
    assert 0 < len(compacted_over["client_series"]["heatmap"]["ttft"]["cells"]) <= 1344

    # Malformed heatmap structures in _compact_report_payload do not raise
    malformed_compacted = report_builder._compact_report_payload(
        {"client_series": {"heatmap": {"ttft": {"cells": "invalid"}, "tpot": None}, "ttft_heatmap": "bad"}},
    )
    assert malformed_compacted["client_series"]["heatmap"]["ttft"]["cells"] == "invalid"

    task = _make_task("00000022")
    html_text = render_html_report(
        task,
        client_series=cs,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    assert _EXPECTED_CSP in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    embedded = _extract_embedded_report_json(html_text)
    assert len(embedded["client_series"]["heatmap"]["ttft"]["cells"]) == 1344
    assert len(embedded["client_series"]["heatmap"]["tpot"]["cells"]) == 1344

    for snippet in (
        "var(--border)",
        "var(--text-muted)",
        "var(--bg-card)",
        "var(--text)",
        'stroke-dasharray": "3 3"',
        'rx: "1.5"',
        "rgb(14, 165, 233)",
        "rgb(16, 185, 129)",
        "rgb(245, 158, 11)",
        "rgb(244, 63, 94)",
        "P50 ${title}:",
        "P90 ${title}:",
        "Max ${title}:",
        "Elapsed time",
    ):
        assert snippet in html_text

    node_bin = shutil.which("node")
    if node_bin is None:
        return

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
function runCase(casePayload) {
  return (async () => {
  const payloadJson = JSON.stringify(casePayload);
  function makeNode(tag, text) {
    const listeners = {};
    const attrs = {};
    const node = {
      tagName: String(tag || "div").toUpperCase(),
      children: [],
      style: {},
      className: "",
      textContent: text || "",
      clientWidth: 640,
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
      getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
      appendChild(c) { if (c != null) node.children.push(typeof c === "string" ? makeNode("#text", c) : c); return c; },
      append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
      replaceChildren(...items) { node.children = []; node.append(...items); },
      addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
      dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj); },
      closest() { return null; },
      querySelectorAll() { return []; },
      getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
      getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 256 }; },
    };
    return node;
  }
  const tipNode = makeNode("div");
  const ids = {
    "chart-tooltip": tipNode,
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", payloadJson),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const document = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(txt) { return makeNode("#text", txt); },
    querySelectorAll() { return []; },
  };
  const context = {
    console,
    document,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
  await new Promise((r) => setTimeout(r, 20));
  const reqPage = ids["report-root"].children.find((n) => n.getAttribute("data-page") === "requests");
  if (!reqPage) throw new Error("Requests page not rendered");
  const texts = [];
  const microCells = [];
  let svgCount = 0;
  let emptyNotes = 0;
  (function walk(n) {
    if (!n) return;
    if (n.textContent) texts.push(n.textContent);
    if (n.tagName === "SVG" && String(n.getAttribute("aria-label") || "").includes("heatmap")) svgCount += 1;
    if (n.tagName === "RECT" && n.getAttribute("rx") === "1.5") microCells.push(n);
    if (n.className === "empty-note") emptyNotes += 1;
    for (const c of n.children || []) walk(c);
  })(reqPage);
  return { text: texts.join("\n"), microCells, svgCount, emptyNotes, tipNode };
  })();
}
(async () => {
  const res1 = await runCase(payload);
  if (res1.svgCount !== 2) throw new Error("Expected 2 heatmap SVGs, got " + res1.svgCount);
  if (res1.microCells.length !== 2688) throw new Error("Expected 2688 micro-cells across TTFT+TPOT, got " + res1.microCells.length);
  for (const needle of [
    "Elapsed time",
    "Input sequence length (tokens)",
    "Output sequence length (tokens)",
    "48 × 28 grid · 4,032 requests (legacy binned fallback)",
    "Latency by Input Sequence Length, Cache-Hit & Request Scatter",
    "TTFT by Input Sequence Length Bucket",
    "TTFT by Time and Input Sequence Length",
    "TPOT by Time and Output Sequence Length",
  ]) {
    if (!res1.text.includes(needle)) throw new Error("Missing rendered heatmap text: " + needle);
  }
  const firstCell = res1.microCells[0];
  firstCell.dispatchEvent("mouseenter", { clientX: 100, clientY: 100 });
  if (!res1.tipNode.textContent.includes("P50 TTFT:") || !res1.tipNode.textContent.includes("P90 TTFT:") || !res1.tipNode.textContent.includes("Max TTFT:") || !res1.tipNode.textContent.includes("Requests: 3")) {
    throw new Error("Unexpected tooltip text: " + res1.tipNode.textContent);
  }
  if (firstCell.getAttribute("stroke") !== "var(--text)") {
    throw new Error("Expected hover highlight stroke var(--text), got " + firstCell.getAttribute("stroke"));
  }
  firstCell.dispatchEvent("mouseleave", {});
  if (firstCell.getAttribute("stroke") !== "var(--bg-card)") {
    throw new Error("Expected restored stroke var(--bg-card), got " + firstCell.getAttribute("stroke"));
  }

  // Edge cases: malformed/non-finite cells, <= 0 time_bins/len_bins, min_ms === max_ms, negative count, and all-invalid cells
  const edgePayload = JSON.parse(JSON.stringify(payload));
  edgePayload.client_series.heatmap = {
    ttft: {
      time_bins: 0,
      len_bins: -2,
      min_ms: 25.0,
      max_ms: 25.0,
      cells: [
        null,
        "invalid",
        { start_seconds: null, end_seconds: 1.0, seq_start: 100, seq_end: 132, avg_ms: 25.0, count: 2 },
        { start_seconds: 0.0, end_seconds: 1.0, seq_start: 100, seq_end: 132, avg_ms: 25.0, count: -5 },
        { start_seconds: 1.0, end_seconds: 2.0, seq_start: 100, seq_end: 132, avg_ms: 25.0, count: 4 },
      ],
    },
    tpot: {
      time_bins: 10,
      len_bins: 10,
      cells: [null, { start_seconds: 0, end_seconds: 1, seq_start: 10, seq_end: 20, avg_ms: null }],
    },
  };
  delete edgePayload.client_series.ttft_heatmap;
  delete edgePayload.client_series.tpot_heatmap;
  const resEdge = await runCase(edgePayload);
  if (resEdge.svgCount !== 1) throw new Error("Expected 1 valid SVG and 1 empty fallback, got svgCount=" + resEdge.svgCount);
  if (resEdge.microCells.length !== 2) throw new Error("Expected 2 valid micro-cells after filtering malformed entries, got " + resEdge.microCells.length);
  if (resEdge.microCells[0].getAttribute("fill") !== "rgb(14, 165, 233)") {
    throw new Error("Expected min_ms === max_ms to map to base sky/cyan stop, got " + resEdge.microCells[0].getAttribute("fill"));
  }
  if (!resEdge.text.includes("2 bins · 4 requests")) {
    throw new Error("Expected <=0 grid fallback & clamped non-negative request count '2 bins · 4 requests' in: " + resEdge.text);
  }
  if (!resEdge.text.includes("TPOT scatter plot unavailable.")) {
    throw new Error("Expected empty-note fallback for all-invalid TPOT cells");
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v12 SVG heatmap runtime check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v14_bottlenecks_stage_latency_breakdown(sim_env: Path) -> None:
    """Offline v14 HTML report renders #b-stage-breakdown, P50/P90 toggle, share summary, and engine-agnostic help."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")

    # 1. Template version, subnav order, and valid CSS theme variables
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in template_html
    b_stage_pos = template_html.find('<a href="#b-stage-breakdown" data-page="bottlenecks">Stage Latency</a>')
    b_ttft_pos = template_html.find('<a href="#b-ttft-decomp" data-page="bottlenecks">TTFT Decomposition</a>')
    assert b_stage_pos != -1 and b_ttft_pos != -1 and b_stage_pos < b_ttft_pos
    for css_cls in (".stage-controls", ".stage-share-grid", ".stage-share-card", ".stage-shift-note"):
        assert css_cls in template_html
    assert "var(--card)" not in template_html
    assert "var(--muted)" not in template_html

    # 2. Engine-agnostic HELP_TEXTS entries for t60 stage breakdown keys
    t60_help_keys = (
        "b-stage-breakdown",
        "Stage Share Summary",
        "EPP Scheduling",
        "Queue Time",
        "Prefill Time",
        "Decode Time",
        "Gateway / EPP Overhead",
        "Client TTFT (Unattributed)",
        "Decode Time (Est.)",
        "Request Stage Latency Breakdown",
    )
    for key in t60_help_keys:
        match = re.search(rf'"{re.escape(key)}":\s*"([^"]+)"', charts_js)
        assert match is not None, f"Missing HELP_TEXTS entry for: {key}"
        copy = match.group(1)
        for forbidden_term in ("vllm:", "vLLM", "GPU", "Router", "SLA"):
            assert forbidden_term not in copy, f"Non-agnostic term {forbidden_term!r} in HELP_TEXTS[{key!r}]: {copy}"

    # 3. Render HTML with canonical stage_latency_breakdown + array-shaped finding evidence
    forensics_payload = {
        "verdict": {
            "code": "decode_bound",
            "label": "Decode Bound",
            "severity": "info",
            "reason": "Decode dominates E2E latency.",
            "evidence": [
                "Elevated TPOT (tpot_p90_ms: 48.0 ms/tok)",
                "Stage Breakdown (P50): Decode Time 82.4% of E2E (820 ms)",
            ],
        },
        "findings": [
            {
                "code": "high_tpot",
                "severity": "warning",
                "category": "Decode",
                "title": "Elevated TPOT",
                "metric": "tpot_p90_ms",
                "value": "48.0 ms/tok",
                "evidence": [{"metric": "Decode Share", "value": "82.4%"}],
                "tab": "serving",
            }
        ],
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "has_p90_series": True,
            "has_epp_series": True,
            "primary_slice": "p50",
            "dominant_e2e_stage": "decode_time",
            "dominant_ttft_stage": "prefill_time",
            "dominant_e2e_stage_p90": "queue_time",
            "dominant_ttft_stage_p90": "queue_time",
            "p50": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 2.4,
                "queue_time_ms": 18.0,
                "prefill_time_ms": 140.0,
                "decode_time_ms": 820.0,
                "gateway_overhead_ms": 14.6,
                "ttft_stage_ms": 175.0,
                "total_stage_ms": 995.0,
                "share_of_e2e_pct": {
                    "epp_scheduling": 0.2,
                    "queue_time": 1.8,
                    "prefill_time": 14.1,
                    "decode_time": 82.4,
                    "gateway_overhead": 1.5,
                },
                "share_of_ttft_pct": {
                    "epp_scheduling": 1.4,
                    "queue_time": 10.3,
                    "prefill_time": 80.0,
                    "gateway_overhead": 8.3,
                },
            },
            "p90": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 5.0,
                "queue_time_ms": 1200.0,
                "prefill_time_ms": 260.0,
                "decode_time_ms": 900.0,
                "gateway_overhead_ms": 35.0,
                "ttft_stage_ms": 1500.0,
                "total_stage_ms": 2400.0,
                "share_of_e2e_pct": {
                    "epp_scheduling": 0.2,
                    "queue_time": 50.0,
                    "prefill_time": 10.8,
                    "decode_time": 37.5,
                    "gateway_overhead": 1.5,
                },
                "share_of_ttft_pct": {
                    "epp_scheduling": 0.3,
                    "queue_time": 80.0,
                    "prefill_time": 17.3,
                    "gateway_overhead": 2.3,
                },
            },
            "peak_window_shift": {
                "detected": True,
                "baseline_stage": "decode_time",
                "peak_stage": "queue_time",
                "peak_t_seconds": 145.0,
                "summary": "Peak window at t=145s shifted dominant stage from Decode Time to Queue Time (62.4% share, 1840 ms).",
            },
            "series": [
                {
                    "t": 15.0,
                    "epp_sched_ms": 2.1,
                    "queue_ms": 14.5,
                    "prefill_ms": 112.0,
                    "decode_ms": 840.0,
                    "epp_sched_p90_ms": 4.2,
                    "queue_p90_ms": 38.0,
                    "prefill_p90_ms": 210.0,
                    "decode_p90_ms": 1120.0,
                },
                {
                    "t": 30.0,
                    "epp_sched_ms": 2.6,
                    "queue_ms": 21.5,
                    "prefill_ms": 168.0,
                    "decode_ms": 800.0,
                    "epp_sched_p90_ms": 5.8,
                    "queue_p90_ms": 1840.0,
                    "prefill_p90_ms": 310.0,
                    "decode_p90_ms": 680.0,
                },
            ],
        },
    }

    task = _make_task("00000023")
    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics=forensics_payload,
    )
    assert "<!-- lens-report-v42 -->" in html_text
    assert _EXPECTED_CSP in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    embedded = _extract_embedded_report_json(html_text)
    node_bin = shutil.which("node")
    if node_bin is None:
        return

    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
function runCase(casePayload) {
  return (async () => {
    const payloadJson = JSON.stringify(casePayload);
    function makeNode(tag, text) {
      const listeners = {};
      const attrs = {};
      const node = {
        tagName: String(tag || "div").toUpperCase(),
        children: [],
        style: {},
        className: "",
        textContent: text || "",
        clientWidth: 640,
        classList: {
          add(c) {
            const parts = new Set(node.className.split(/\s+/).filter(Boolean));
            parts.add(c);
            node.className = Array.from(parts).join(" ");
          },
          remove(c) {
            node.className = node.className.split(/\s+/).filter((x) => x && x !== c).join(" ");
          },
          toggle(c, force) {
            const parts = new Set(node.className.split(/\s+/).filter(Boolean));
            const shouldAdd = force !== undefined ? Boolean(force) : !parts.has(c);
            if (shouldAdd) parts.add(c);
            else parts.delete(c);
            node.className = Array.from(parts).join(" ");
          },
        },
        setAttribute(k, v) {
          attrs[k] = String(v);
          if (k === "class") node.className = String(v);
          if (k === "id") ids[String(v)] = node;
        },
        getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
        appendChild(c) { if (c != null) node.children.push(typeof c === "string" ? makeNode("#text", c) : c); return c; },
        append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
        replaceChildren(...items) { node.children = []; node.append(...items); },
        addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
        dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || {}); },
        closest() { return null; },
        querySelectorAll(sel) {
          const out = [];
          const walk = (n) => {
            if (!n) return;
            if (sel === ".report-page" && String(n.className).split(/\s+/).includes("report-page")) out.push(n);
            for (const c of n.children || []) walk(c);
          };
          walk(ids["report-root"]);
          return out;
        },
        getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
        getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 220 }; },
      };
      return node;
    }
    const ids = {};
    Object.assign(ids, {
      "chart-tooltip": makeNode("div"),
      "report-root": makeNode("main"),
      "report-title": makeNode("span"),
      "report-meta": makeNode("div"),
      "status-badges": makeNode("span"),
      "lens-report-data": makeNode("script", payloadJson),
      "sidebar-toggle": makeNode("button"),
      "sidebar": makeNode("aside"),
      "theme-toggle": makeNode("button"),
      "print-btn": makeNode("button"),
      "window-toggle": makeNode("div"),
      "nav-list": makeNode("nav"),
    });
    const document = {
      documentElement: makeNode("html"),
      getElementById(id) { return ids[id] || makeNode("div"); },
      createElement(tag) { return makeNode(tag); },
      createElementNS(_ns, tag) { return makeNode(tag); },
      createTextNode(txt) { return makeNode("#text", txt); },
      querySelectorAll(sel) { return ids["report-root"].querySelectorAll(sel); },
    };
    const context = {
      console,
      document,
      window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
      getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
      URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
    };
    vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
    await new Promise((r) => setTimeout(r, 20));
    return { ids, root: ids["report-root"], findText: (root) => {
      const t = [];
      (function w(n) { if (!n) return; if (n.textContent) t.push(n.textContent); for (const c of n.children || []) w(c); })(root);
      return t.join("\n");
    }};
  })();
}
function findAll(root, pred) {
  const res = [];
  (function walk(n) {
    if (!n) return;
    if (pred(n)) res.push(n);
    for (const c of n.children || []) walk(c);
  })(root);
  return res;
}
(async () => {
  // Server-telemetry case
  const res1 = await runCase(payload);
  const bPage = res1.root.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const sPage = res1.root.children.find((n) => n.getAttribute("data-page") === "serving");
  const oPage = res1.root.children.find((n) => n.getAttribute("data-page") === "overview");
  const stageSec = findAll(bPage, (n) => n.getAttribute("id") === "b-stage-breakdown")[0];
  if (!stageSec) throw new Error("Missing #b-stage-breakdown section");
  const secText1 = res1.findText(stageSec);
  if (res1.findText(bPage).includes("[object Object]")) throw new Error("Array finding.evidence rendered [object Object]");
  for (const needle of [
    "Request Stage Latency Breakdown",
    "Dominant E2E: Decode Time · 82.40%",
    "Dominant TTFT: Prefill Time · 80%",
    "Peak Shift",
    "Peak window at t=145s shifted dominant stage from Decode Time to Queue Time",
    "Stage Share Summary",
    "82.40% of E2E",
    "Serving · Latency ->",
  ]) {
    if (!secText1.includes(needle)) throw new Error("Missing P50 stage text: " + needle);
  }
  const p90Btn = findAll(stageSec, (n) => n.tagName === "BUTTON" && n.textContent === "P90")[0];
  if (!p90Btn) throw new Error("Missing P90 toggle button");
  p90Btn.dispatchEvent("click", {});
  const secTextP90 = res1.findText(stageSec);
  if (!secTextP90.includes("Dominant E2E: Queue Time · 50%")) {
    throw new Error("Expected P90 toggle to update Dominant E2E badge, got:\n" + secTextP90);
  }
  if (!secTextP90.includes("80% of TTFT")) {
    throw new Error("Expected P90 toggle to update Queue Time TTFT share, got:\n" + secTextP90);
  }
  const jumpBtn = findAll(stageSec, (n) => n.tagName === "BUTTON" && n.textContent === "Serving · Latency ->")[0];
  jumpBtn.dispatchEvent("click", {});
  if (!sPage.className.includes("active")) throw new Error("Expected Serving · Latency -> button to activate serving page");

  // Overview evidence bullet fallback (idx=1 has no findings[1]) jumps to bottlenecks
  const evBtns = findAll(oPage, (n) => n.className === "evidence-btn");
  if (evBtns.length < 2) throw new Error("Expected 2 overview evidence buttons");
  evBtns[1].dispatchEvent("click", {});
  if (!bPage.className.includes("active")) throw new Error("Expected Stage Breakdown evidence bullet to activate bottlenecks page");

  // Client-fallback case (server unavailable -> hides Serving · Latency -> button, and P90 null dominant hides badge)
  const clientPayload = JSON.parse(JSON.stringify(payload));
  clientPayload.server_telemetry = { status: "unavailable", available: false, series: [], summary: {} };
  clientPayload.forensics.stage_latency_breakdown = {
    available: true,
    source: "client_fallback",
    has_p90_series: true,
    primary_slice: "p50",
    dominant_e2e_stage: "decode_time",
    dominant_ttft_stage: null,
    dominant_e2e_stage_p90: null,
    dominant_ttft_stage_p90: null,
    p50: {
      available: true,
      source: "client_fallback",
      ttft_stage_ms: 180.0,
      decode_time_ms: 720.0,
      total_stage_ms: 900.0,
      share_of_e2e_pct: { ttft_unattributed: 20.0, decode_time: 80.0 },
      share_of_ttft_pct: {},
    },
    p90: {
      available: true,
      source: "client_fallback",
      ttft_stage_ms: 900.0,
      decode_time_ms: 600.0,
      total_stage_ms: 1500.0,
      share_of_e2e_pct: { ttft_unattributed: 60.0, decode_time: 40.0 },
      share_of_ttft_pct: {},
    },
    series: [{ t: 5.0, ttft_ms: 180.0, decode_ms: 720.0, ttft_p90_ms: 900.0, decode_p90_ms: 600.0 }],
  };
  const resClient = await runCase(clientPayload);
  const bClient = resClient.root.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const stageClient = findAll(bClient, (n) => n.getAttribute("id") === "b-stage-breakdown")[0];
  const clientText = resClient.findText(stageClient);
  if (clientText.includes("Serving · Latency ->")) {
    throw new Error("Client-only report must not show Serving · Latency -> button");
  }
  if (!clientText.includes("Dominant E2E: Decode Time · 80%")) {
    throw new Error("Expected P50 Dominant E2E badge on client-fallback before P90 toggle");
  }
  if (!clientText.includes("Client TTFT (Unattributed)") || !clientText.includes("Decode Time (Est.)")) {
    throw new Error("Expected client-fallback labels in stage share summary");
  }
  const clientP90Btn = findAll(stageClient, (n) => n.tagName === "BUTTON" && n.textContent === "P90")[0];
  if (!clientP90Btn) throw new Error("Missing P90 toggle button in client-fallback case");
  clientP90Btn.dispatchEvent("click", {});
  const clientDomBadge = findAll(stageClient, (n) => String(n.textContent || "").startsWith("Dominant E2E:"))[0];
  if (!clientDomBadge || clientDomBadge.style.display !== "none") {
    throw new Error("Expected Dominant E2E badge hidden when P90 dominant_e2e_stage_p90 is null");
  }
  const clientP90Text = resClient.findText(stageClient);
  if (!clientP90Text.includes("60% of E2E") || !clientP90Text.includes("40% of E2E")) {
    throw new Error("Expected P90 share summary percentages in client-fallback case, got:\n" + clientP90Text);
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v14 #b-stage-breakdown runtime check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v15_help_formulas_engine_agnostic_and_dom_safety(sim_env: Path) -> None:
    """Offline v15 HTML report renders engine-agnostic HELP_TEXTS + HELP_FORMULAS safely via textContent."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")

    # 1. Version marker, CSS classes, widened tooltip max-width, and zero unsafe DOM sinks
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in template_html
    assert "max-width: 360px" in template_html
    for css_cls in (".help-tip-formula-wrap", ".help-tip-formula-label", ".help-tip-formula"):
        assert css_cls in template_html
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in charts_js
        assert forbidden not in template_html

    # 2. Extract HELP_TEXTS and HELP_FORMULAS dictionaries accurately
    help_texts_match = re.search(r"const HELP_TEXTS = (\{.*?\n  \});", charts_js, flags=re.DOTALL)
    help_formulas_match = re.search(r"const HELP_FORMULAS = (\{.*?\n  \});", charts_js, flags=re.DOTALL)
    assert help_texts_match is not None, "Missing HELP_TEXTS object in report_charts.js"
    assert help_formulas_match is not None, "Missing HELP_FORMULAS object in report_charts.js"

    static_metadata_allowlist = {
        "Pod",
        "Role",
        "Destination Endpoint",
        "Engine",
        "Node",
        "PCI BDF / Device",
        "Metric",
        "Telemetry Source",
        "Req #",
        "Time (s)",
        "Category",
        "Code",
        "Error Message (Redacted)",
        "s-grafana",
        "c-manifest",
        "c-logs",
    }

    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js tooltip behavior"

    # Validate parsed JS dictionaries (engine-agnostic scrub + formula coverage)
    dict_check_js = """
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8");
const m1 = src.match(/const HELP_TEXTS = (\\{[\\s\\S]*?\\n  \\});/);
const m2 = src.match(/const HELP_FORMULAS = (\\{[\\s\\S]*?\\n  \\});/);
if (!m1 || !m2) throw new Error("Failed to locate HELP_TEXTS / HELP_FORMULAS");
const ctx = {};
vm.runInNewContext("HELP_TEXTS = " + m1[1] + "; HELP_FORMULAS = " + m2[1] + ";", ctx);
process.stdout.write(JSON.stringify({ texts: ctx.HELP_TEXTS, formulas: ctx.HELP_FORMULAS }));
"""
    dict_proc = subprocess.run(
        [node_bin, "-e", dict_check_js, str(Path(str(assets_dir.joinpath("report_charts.js"))))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dict_proc.returncode == 0, f"Failed to parse HELP_TEXTS/HELP_FORMULAS:\n{dict_proc.stderr}"
    parsed_dicts = json.loads(dict_proc.stdout)
    help_texts: dict[str, str] = parsed_dicts["texts"]
    help_formulas: dict[str, str] = parsed_dicts["formulas"]

    assert len(help_texts) >= 120
    forbidden_re = re.compile(r"(vllm:|sglang:|trtllm:|--kv-cache-metrics-sample|\bvllm\b)", re.IGNORECASE)
    for key, prose in help_texts.items():
        assert isinstance(prose, str) and prose.strip(), f"Empty HELP_TEXTS[{key!r}]"
        assert not forbidden_re.search(prose), f"Forbidden engine-specific term in HELP_TEXTS[{key!r}]: {prose!r}"

    for key, formula in help_formulas.items():
        assert key in help_texts, f"Orphaned HELP_FORMULAS key not in HELP_TEXTS: {key!r}"
        assert isinstance(formula, str) and formula.strip(), f"Empty HELP_FORMULAS[{key!r}]"
        assert not forbidden_re.search(formula), (
            f"Forbidden engine-specific term in HELP_FORMULAS[{key!r}]: {formula!r}"
        )

    missing_formulas = [k for k in help_texts if k not in static_metadata_allowlist and k not in help_formulas]
    assert not missing_formulas, f"Missing HELP_FORMULAS entries for non-metadata keys: {missing_formulas}"

    # Verify canonical preserved lookup keys remain intact
    for preserved_key in (
        "vLLM TPOT over Time",
        "KV Block Idle Time Before Eviction (P50 / P90)",
        "KV Block Lifetime (P50 / P90)",
        "KV Block Reuse Gap (P50 / P90)",
    ):
        assert preserved_key in help_texts
        assert preserved_key in help_formulas

    # 3. Node VM runtime test verifying showHelpTip / helpBadge / makeSection / chartCard / kpiTile / makeSortableTable
    task = _make_task("00000024")
    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics={
            "verdict": {"code": "healthy", "label": "Healthy", "severity": "info"},
            "routing_vs_cache": {"epp_expected_hit_pct": 85.0, "vllm_actual_hit_pct": 70.0, "gap_pct": 15.0},
        },
    )
    assert "<!-- lens-report-v42 -->" in html_text
    embedded = _extract_embedded_report_json(html_text)
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const payloadJson = fs.readFileSync(0, "utf8");

function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    style: {},
    className: "",
    textContent: text || "",
    clientWidth: 640,
    offsetWidth: 340,
    offsetHeight: 120,
    classList: {
      add(c) {
        const parts = new Set(node.className.split(/\s+/).filter(Boolean));
        parts.add(c);
        node.className = Array.from(parts).join(" ");
      },
      remove(...cs) {
        const drop = new Set(cs);
        node.className = node.className.split(/\s+/).filter((x) => x && !drop.has(x)).join(" ");
      },
      toggle(c, force) {
        const parts = new Set(node.className.split(/\s+/).filter(Boolean));
        const shouldAdd = force !== undefined ? Boolean(force) : !parts.has(c);
        if (shouldAdd) parts.add(c);
        else parts.delete(c);
        node.className = Array.from(parts).join(" ");
      },
    },
    setAttribute(k, v) {
      attrs[k] = String(v);
      if (k === "class") node.className = String(v);
      if (k === "id") ids[String(v)] = node;
    },
    getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
    appendChild(c) { if (c != null) node.children.push(typeof c === "string" ? makeNode("#text", c) : c); return c; },
    append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
    replaceChildren(...items) { node.children = []; node.append(...items); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || {}); },
    closest() { return null; },
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => {
        if (!n) return;
        if (sel === ".report-page" && String(n.className).split(/\s+/).includes("report-page")) out.push(n);
        for (const c of n.children || []) walk(c);
      };
      walk(ids["report-root"]);
      return out;
    },
    getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
    getBoundingClientRect() { return { left: 20, top: 20, bottom: 40, width: 640, height: 220 }; },
  };
  return node;
}

const ids = {};
Object.assign(ids, {
  "chart-tooltip": makeNode("div"),
  "report-root": makeNode("main"),
  "report-title": makeNode("span"),
  "report-meta": makeNode("div"),
  "status-badges": makeNode("span"),
  "lens-report-data": makeNode("script", payloadJson),
  "sidebar-toggle": makeNode("button"),
  "sidebar": makeNode("aside"),
  "theme-toggle": makeNode("button"),
  "print-btn": makeNode("button"),
  "window-toggle": makeNode("div"),
  "nav-list": makeNode("nav"),
});
const document = {
  documentElement: makeNode("html"),
  getElementById(id) { return ids[id] || makeNode("div"); },
  createElement(tag) { return makeNode(tag); },
  createElementNS(_ns, tag) { return makeNode(tag); },
  createTextNode(txt) { return makeNode("#text", txt); },
  querySelectorAll(sel) { return ids["report-root"].querySelectorAll(sel); },
};
const context = {
  console,
  document,
  window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
  URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
};

function findAll(root, pred) {
  const res = [];
  (function walk(n) {
    if (!n) return;
    if (pred(n)) res.push(n);
    for (const c of n.children || []) walk(c);
  })(root);
  return res;
}

// Expose showHelpTip / helpBadge / makeSection via a minimal wrapper at end of IIFE test copy
const src = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\nglobalThis.__testExports = { showHelpTip, helpBadge, makeSection, chartCard, kpiTile, hideTip, resolveHelpText, resolveHelpFormula };\n})();\n"
);

(async () => {
  vm.runInNewContext(src, context);
  await new Promise((r) => setTimeout(r, 20));
  const ex = context.__testExports;
  const tip = ids["chart-tooltip"];

  // Guard: empty text and empty formula -> hides tooltip
  ex.showHelpTip({ clientX: 10, clientY: 10 }, "Empty", null, null);
  if (tip.style.display !== "none") throw new Error("Expected showHelpTip(null, null) to hide tooltip");

  // Prose + formula -> renders title, body above formula-wrap (label + formula)
  ex.showHelpTip({ clientX: 10, clientY: 10 }, "Sample Metric", "Prose explanation.", "Rate = Count / Window_s");
  if (tip.style.display !== "block" || !tip.className.includes("help-tip")) {
    throw new Error("Expected tooltip shown with help-tip class");
  }
  if (tip.children.length !== 3) throw new Error("Expected 3 children in tooltip, got " + tip.children.length);
  if (tip.children[0].className !== "help-tip-title" || tip.children[0].textContent !== "Sample Metric") {
    throw new Error("Unexpected title child");
  }
  if (tip.children[1].className !== "help-tip-body" || tip.children[1].textContent !== "Prose explanation.") {
    throw new Error("Unexpected body child");
  }
  const fWrap = tip.children[2];
  if (fWrap.className !== "help-tip-formula-wrap") throw new Error("Expected help-tip-formula-wrap as 3rd child");
  if (fWrap.children[0].className !== "help-tip-formula-label" || fWrap.children[0].textContent !== "Formula") {
    throw new Error("Expected Formula label");
  }
  if (fWrap.children[1].className !== "help-tip-formula" || fWrap.children[1].textContent !== "Rate = Count / Window_s") {
    throw new Error("Expected formula textContent");
  }

  // Prose without formula (static metadata) -> omits formula block cleanly
  ex.showHelpTip({ clientX: 10, clientY: 10 }, "Node", "Worker node name.", null);
  if (tip.children.length !== 2) throw new Error("Expected 2 children when formula omitted");

  // Formula-only without prose -> does not crash and renders formula block
  ex.showHelpTip({ clientX: 10, clientY: 10 }, "Formula Only", null, "X = A + B");
  if (tip.style.display !== "block" || tip.children.length !== 2) {
    throw new Error("Expected formula-only tooltip to render safely");
  }

  // Dynamic normalization check
  if (!ex.resolveHelpFormula("GPU Power (Avg / Peak)")) throw new Error("GPU normalization failed");
  if (!ex.resolveHelpFormula("XPU Power & Thermal")) throw new Error("XPU Power & Thermal normalization failed");
  if (!ex.resolveHelpFormula("Host & GPU Power")) throw new Error("Host & GPU Power normalization failed");
  if (!ex.resolveHelpFormula("HPU (Count · Util · Power · Temp)")) throw new Error("HPU table col normalization failed");

  // makeSection renders section id formula even when section title differs from section id
  const sec = ex.makeSection("o-thr", "Throughput over Time", "sub", makeNode("div"));
  const secIcon = findAll(sec, (n) => n.className === "help-icon")[0];
  if (!secIcon) throw new Error("Missing help icon on makeSection('o-thr', ...)");
  secIcon.dispatchEvent("click", { stopPropagation() {} });
  const formulaNode = findAll(tip, (n) => n.className === "help-tip-formula")[0];
  if (!formulaNode || !formulaNode.textContent.includes("Bucket_Throughput")) {
    throw new Error("makeSection('o-thr') failed to render o-thr formula");
  }

  // Trigger every rendered .help-icon across all 6 pages and verify zero hardcoded engine metric names
  const allIcons = findAll(ids["report-root"], (n) => n.className === "help-icon");
  if (allIcons.length < 30) throw new Error("Expected >= 30 rendered help icons across report, got " + allIcons.length);
  const forbiddenRe = /(vllm:|sglang:|trtllm:|--kv-cache-metrics-sample|\bvllm\b)/i;
  let formulaCount = 0;
  for (const icon of allIcons) {
    icon.dispatchEvent("mouseenter", { clientX: 50, clientY: 50 });
    const bodyEl = findAll(tip, (n) => n.className === "help-tip-body")[0];
    const formEl = findAll(tip, (n) => n.className === "help-tip-formula")[0];
    if (!bodyEl || !bodyEl.textContent.trim()) {
      throw new Error("Empty tooltip prose for icon " + icon.getAttribute("aria-label"));
    }
    if (forbiddenRe.test(bodyEl.textContent)) {
      throw new Error("Forbidden engine term in rendered prose for " + icon.getAttribute("aria-label") + ": " + bodyEl.textContent);
    }
    if (formEl) {
      formulaCount += 1;
      if (forbiddenRe.test(formEl.textContent)) {
        throw new Error("Forbidden engine term in rendered formula for " + icon.getAttribute("aria-label") + ": " + formEl.textContent);
      }
    }
  }
  if (formulaCount < 25) throw new Error("Expected >= 25 rendered formulas across report, got " + formulaCount);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v16 tooltip formula VM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_render_html_report_log_severity_classifier_and_highlighting(sim_env: Path):
    """Offline report classifies structured/unstructured/traceback logs and highlights via safe DOM nodes."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000025")
    task.logs = [
        "[info] Task started",
        "[warn] Slow startup detected",
        "[error] Runner exited with code 1",
    ]
    backend_dir = Path(task.task_dir) / "artifacts" / task.simulation.backend
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "stdout.log").write_text(
        "\n".join(
            [
                "\x1b[32m12:34:56.789 INFO     Request finished with HTTP/1.1 404 Not Found .451 :411\x1b[0m",
                "12:34:57 INFO     CLI Command: aiperf profile --error-rate-slo 0.05 --request-timeout 30",
                "12:34:58 WARNING  High queue depth observed",
                "│ Error Records │ 0 │",
                "Warning: You are sending unauthenticated requests to the HF Hub.",
            ]
        ),
        encoding="utf-8",
    )
    (backend_dir / "stderr.log").write_text(
        "\n".join(
            [
                "Traceback (most recent call last):",
                '  File "/app/runner.py", line 10, in run',
                "    raise ValueError('bad payload')",
                "ValueError: bad payload",
                "    Authorization: Bearer supersecret-indented-token",
                "12:35:00 INFO     Cleanup finished",
            ]
        ),
        encoding="utf-8",
    )

    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy", "severity": "info"}},
    )
    assert "supersecret-indented-token" not in html_text
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    assert ".log-line-err { color: var(--crit); }" in html_text
    assert ".log-line-warn { color: var(--warn); }" in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    embedded = _extract_embedded_report_json(html_text)
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js log classifier"

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const payloadJson = fs.readFileSync(0, "utf8");

function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    style: {},
    className: "",
    textContent: text !== undefined && text !== null ? String(text) : "",
    value: "",
    clientWidth: 640,
    offsetWidth: 340,
    offsetHeight: 120,
    classList: {
      add(c) {
        const parts = new Set(node.className.split(/\s+/).filter(Boolean));
        parts.add(c);
        node.className = Array.from(parts).join(" ");
      },
      remove(...cs) {
        const drop = new Set(cs);
        node.className = node.className.split(/\s+/).filter((x) => x && !drop.has(x)).join(" ");
      },
      toggle(c, force) {
        const parts = new Set(node.className.split(/\s+/).filter(Boolean));
        const shouldAdd = force !== undefined ? Boolean(force) : !parts.has(c);
        if (shouldAdd) parts.add(c);
        else parts.delete(c);
        node.className = Array.from(parts).join(" ");
      },
    },
    setAttribute(k, v) {
      attrs[k] = String(v);
      if (k === "class") node.className = String(v);
      if (k === "id") ids[String(v)] = node;
    },
    getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
    appendChild(c) {
      if (c != null) {
        if (typeof c === "string") node.children.push(makeNode("#text", c));
        else if (c.tagName === "#FRAGMENT") node.children.push(...c.children);
        else node.children.push(c);
      }
      return c;
    },
    append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
    replaceChildren(...items) { node.children = []; node.append(...items); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || {}); },
    closest() { return null; },
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => {
        if (!n) return;
        if (sel === ".report-page" && String(n.className).split(/\s+/).includes("report-page")) out.push(n);
        for (const c of n.children || []) walk(c);
      };
      walk(ids["report-root"]);
      return out;
    },
    getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
    getBoundingClientRect() { return { left: 20, top: 20, bottom: 40, width: 640, height: 220 }; },
  };
  return node;
}

const ids = {};
Object.assign(ids, {
  "chart-tooltip": makeNode("div"),
  "report-root": makeNode("main"),
  "report-title": makeNode("span"),
  "report-meta": makeNode("div"),
  "status-badges": makeNode("span"),
  "lens-report-data": makeNode("script", payloadJson),
  "sidebar-toggle": makeNode("button"),
  "sidebar": makeNode("aside"),
  "theme-toggle": makeNode("button"),
  "print-btn": makeNode("button"),
  "window-toggle": makeNode("div"),
  "nav-list": makeNode("nav"),
});
const document = {
  documentElement: makeNode("html"),
  getElementById(id) { return ids[id] || makeNode("div"); },
  createElement(tag) { return makeNode(tag); },
  createElementNS(_ns, tag) { return makeNode(tag); },
  createTextNode(txt) { return makeNode("#text", txt); },
  createDocumentFragment() { return makeNode("#fragment"); },
  querySelectorAll(sel) { return ids["report-root"].querySelectorAll(sel); },
};
const context = {
  console,
  document,
  window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
  URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
};

function nodeText(n) {
  if (!n) return "";
  if (n.tagName === "#TEXT") return n.textContent;
  if (n.children && n.children.length) return n.children.map(nodeText).join("");
  return n.textContent || "";
}

function findAll(root, pred) {
  const res = [];
  (function walk(n) {
    if (!n) return;
    if (pred(n)) res.push(n);
    for (const c of n.children || []) walk(c);
  })(root);
  return res;
}

const src = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\nglobalThis.__testExports = { stripAnsi, classifyLogLine, classifyLogLines, isLogErrorOrWarning };\n})();\n"
);

(async () => {
  vm.runInNewContext(src, context);
  await new Promise((r) => setTimeout(r, 20));
  const ex = context.__testExports;
  const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

  // 1. stripAnsi
  assert(ex.stripAnsi("\x1b[31mERR\x1b[0m \x1b[1;33mWARN\x1b[0m") === "ERR WARN", "stripAnsi failed");

  // 2. Rule 1 precedence over body keywords (HTTP 404, .451, :411, timeout, error_rate_slo, Failed: 0)
  const infoCases = [
    "12:34:56.789 INFO     HTTP/1.1 404 Not Found .451 :411 timeout error_rate_slo Failed: 0",
    "12:34:56 DEBUG    request_timeout=30",
    "12:34:56 TRACE    connection refused detail in trace",
    "12:34:56 NOTICE   startup complete",
    "2026-11-26 00:10:00,123 - aiperf.runner - INFO - timeout=30 error_rate_slo=0.05",
    "2026-11-26T00:10:00Z [INFO] Healthy status_code=500 probe ignored",
    "INFO:     127.0.0.1:411 - \"GET /health HTTP/1.1\" 200 OK",
    "[info] Task queued",
    "ts=2026-11-26T00:10:00Z level=\"info\" msg=\"error_rate_slo ok\"",
    "12:34:56 INFO     Traceback (most recent call last):",
  ];
  for (const line of infoCases) {
    assert(ex.classifyLogLine(line) === "info", `Expected info for: ${line}`);
  }

  const warnCases = [
    "\x1b[33m12:34:56 WARNING  High latency\x1b[0m",
    "12:34:56 WARN     Retrying request",
    "2026-11-26 00:10:00,123 - aiperf.runner - WARNING - Slow response",
    "2026-11-26T00:10:00Z [WARN] Queue pressure",
    "WARN: Low memory",
    "[warn] Deprecation notice",
    "ts=1 level=warning msg=\"slow\"",
    "Warning: You are sending unauthenticated requests to the HF Hub.",
    "UserWarning: CUDA is not available",
  ];
  for (const line of warnCases) {
    assert(ex.classifyLogLine(line) === "warning", `Expected warning for: ${line}`);
  }

  const errCases = [
    "\x1b[31m12:34:56 ERROR    Request failed\x1b[0m",
    "12:34:56 CRITICAL Fatal state",
    "12:34:56 FATAL    Aborting",
    "2026-11-26 00:10:00,123 - aiperf.runner - ERROR - Connection refused",
    "2026-11-26T00:10:00Z [ERROR] Worker crashed",
    "ERROR: Unhandled failure",
    "[error] Runner exited with code 1",
    "ts=1 level=error msg=\"boom\"",
    "ValueError: bad input",
    "Error: failed to bind port 8000",
    "Exception: unexpected EOF",
    "OOMKilled",
    "connection refused by peer",
    "request timed out after 30s",
    "exited with non-zero code 2",
    "Response HTTP/1.1 503 Service Unavailable",
    "Upstream status_code=502",
    "Task failed during execution",
    "Task failed with 0 warnings",
    "HTTP/1.1 500 Internal Server Error (0 warnings)",
    "Failed Requests: 0; RuntimeError: crash",
    "Error Records: 0 | connection refused",
    "subprocess failed",
  ];
  for (const line of errCases) {
    assert(ex.classifyLogLine(line) === "error", `Expected error for: ${line}`);
  }

  // 3. Rule 3 guardrails: zero-count summaries, CLI/config lines, bare 4xx/5xx numbers
  const guardrailCases = [
    "Error Records: 0",
    "Errors: 0",
    "Warnings: 0",
    "Failed Requests: 0",
    "0 errors",
    "0 warnings",
    "0 failed",
    "│ Error Records │ 0 │",
    "CLI Command: aiperf profile --error-rate-slo 0.05 --request-timeout 30 --log-level=error",
    "aiperf profile --log-level=error",
    "error_rate_slo: 0.05",
    "request_timeout: 30",
    "early_stop_error_threshold=0.1",
    "Latency p99: 451.2 ms on port :411 with 500 samples and 404 tokens",
  ];
  for (const line of guardrailCases) {
    assert(ex.classifyLogLine(line) === "info", `Expected info guardrail for: ${line}`);
  }

  // 4. Rule 2 multiline Python traceback continuation, chained tracebacks, back-to-back truncated tracebacks, and explicit INFO immunity
  const tbLines = [
    "12:34:56 INFO     Traceback (most recent call last):",
    "  not part of traceback because header was INFO",
    "Traceback (most recent call last):",
    '  File "/app/a.py", line 1, in <module>',
    "    run()",
    "ZeroDivisionError: division by zero",
    "",
    "During handling of the above exception, another exception occurred:",
    "",
    "Traceback (most recent call last):",
    '  File "/app/b.py", line 2, in run',
    "Traceback (most recent call last):",
    '  File "/app/c.py", line 3, in inner',
    "RuntimeError: boom",
    "12:34:59 INFO     After traceback",
  ];
  const tbClassified = ex.classifyLogLines(tbLines);
  assert(
    JSON.stringify(tbClassified.map((x) => x.severity)) === JSON.stringify([
      "info", "info", "error", "error", "error", "error", "info", "error", "info", "error", "error", "error", "error", "error", "info",
    ]),
    "Unexpected traceback classification: " + JSON.stringify(tbClassified.map((x) => x.severity))
  );

  // 4b. 50 KB adversarial ReDoS timing checks (must stay linear O(n) and finish < 50 ms)
  const adversarialLines = [
    "errors " + "=".repeat(50000) + "1",
    "│ errors " + " ".repeat(50000) + "= 1",
    " ".repeat(50000) + "x",
    "2026-11-26 00:00:00 - " + " ".repeat(50000) + "x",
    "2026-11-26 00:00:00 - logger " + " ".repeat(50000) + "x",
  ];
  const t0 = Date.now();
  const advOut = ex.classifyLogLines(adversarialLines);
  const elapsedMs = Date.now() - t0;
  assert(advOut.length === adversarialLines.length, "Unexpected adversarial line count");
  assert(elapsedMs < 50, `Adversarial 50KB ReDoS check too slow: ${elapsedMs}ms`);

  // 5. Verify rendered DOM inside Config & Logs page (including preserved leading indentation from _load_log_snapshot)
  const configPage = findAll(ids["report-root"], (n) => n.getAttribute("data-page") === "config")[0];
  assert(configPage, "Missing config page");
  const logPre = findAll(configPage, (n) => n.tagName === "PRE" && n.className === "log-box")[1];
  assert(logPre, "Missing logs <pre class='log-box'>");
  const errSpans = findAll(logPre, (n) => n.tagName === "SPAN" && n.className === "log-line-err");
  const warnSpans = findAll(logPre, (n) => n.tagName === "SPAN" && n.className === "log-line-warn");
  assert(errSpans.length === 5, `Expected 5 .log-line-err spans (1 lifecycle + 4 traceback), got ${errSpans.length}`);
  assert(warnSpans.length === 3, `Expected 3 .log-line-warn spans, got ${warnSpans.length}`);
  const fullText = nodeText(logPre);
  assert(!fullText.includes("\x1b["), "ANSI escapes must be stripped from rendered log text");
  assert(!fullText.includes("supersecret-indented-token"), "Indented secret must remain redacted");
  assert(fullText.includes('  File "/app/runner.py"'), "Indented traceback frame must survive end-to-end");
  assert(fullText.includes("│ Error Records │ 0 │"), "Expected zero-count summary in full log view");

  // Toggle "Errors / Warnings Only"
  const errBtn = findAll(configPage, (n) => n.tagName === "BUTTON" && n.textContent === "Errors / Warnings Only")[0];
  assert(errBtn, "Missing Errors / Warnings Only button");
  errBtn.dispatchEvent("click");
  const filteredText = nodeText(logPre);
  assert(!filteredText.includes("│ Error Records │ 0 │"), "Zero-count summary must be filtered out");
  assert(!filteredText.includes("CLI Command:"), "INFO CLI Command line must be filtered out");
  assert(!filteredText.includes("HTTP/1.1 404"), "INFO HTTP 404 line must be filtered out");
  assert(filteredText.includes('  File "/app/runner.py"'), "Indented traceback frame must be kept in filtered view");
  assert(filteredText.includes("    raise ValueError('bad payload')"), "Indented raise line must be kept in filtered view");
  assert(filteredText.includes("ValueError: bad payload"), "Traceback terminating exception must be kept");
  assert(filteredText.includes("High queue depth observed"), "WARNING line must be kept");
  assert(filteredText.includes("Warning: You are sending unauthenticated"), "Unstructured Warning must be kept");

  // Filter search input to unmatched string -> empty note
  const searchInput = findAll(configPage, (n) => n.tagName === "INPUT")[0];
  assert(searchInput, "Missing log search input");
  searchInput.dispatchEvent("input", { target: { value: "zzzz_nonexistent_zzzz" } });
  assert(nodeText(logPre) === "No matching log lines.", `Unexpected empty state text: ${nodeText(logPre)}`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v17 log classifier VM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_render_html_report_warmup_and_drain_boundary_labels(sim_env: Path) -> None:
    """Offline report labels both Warmup and Drain boundaries across LineBandChart, StackedAreaChart, and SwimlaneCanvas without clipping or collision."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000026", backend_options={"warmup_seconds": 10.0, "e2e_slo": 2.0})
    client_series = _sample_client_series()
    client_series["warmup_seconds"] = 10.0
    client_series["dispatch_duration_seconds"] = 45.0
    client_series["duration_seconds"] = 60.0
    client_series["buckets"] = [
        {
            "t": 5.0,
            "throughput_rps": 8.0,
            "goodput_rps": 7.5,
            "output_tok_per_s": 300.0,
            "ttft_p50_ms": 40.0,
            "ttft_p90_ms": 80.0,
            "in_flight_concurrency": 4.0,
        },
        {
            "t": 25.0,
            "throughput_rps": 12.0,
            "goodput_rps": 11.5,
            "output_tok_per_s": 420.0,
            "ttft_p50_ms": 35.0,
            "ttft_p90_ms": 65.0,
            "in_flight_concurrency": 6.0,
        },
        {
            "t": 55.0,
            "throughput_rps": 4.0,
            "goodput_rps": 4.0,
            "output_tok_per_s": 140.0,
            "ttft_p50_ms": 30.0,
            "ttft_p90_ms": 50.0,
            "in_flight_concurrency": 2.0,
        },
    ]
    client_series["slot_lanes"] = {
        "peak_slots": 2,
        "total_requests": 2,
        "session_count": 2,
        "sample_stride": 1,
        "tier_counts": {"cold": 1, "warm": 1},
        "items": [
            {"slot": 0, "start_s": 0.0, "end_s": 8.0, "cache_hit_pct": 0.0, "tier": "cold"},
            {"slot": 1, "start_s": 12.0, "end_s": 40.0, "cache_hit_pct": 85.0, "tier": "warm"},
        ],
    }

    html_text = render_html_report(
        task,
        client_series=client_series,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
        assert forbidden not in html_text

    embedded = _extract_embedded_report_json(html_text)
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js boundary labels"

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const payload = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    style: {},
    className: "",
    textContent: text || "",
    clientWidth: 640,
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
    getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
    appendChild(c) {
      if (c != null) {
        if (typeof c === "string") node.children.push(makeNode("#text", c));
        else node.children.push(c);
      }
      return c;
    },
    append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
    replaceChildren(...items) { node.children = []; node.append(...items); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || { target: node }); },
    closest() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 220 }; },
  };
  return node;
}

const ids = {
  "chart-tooltip": makeNode("div"),
  "report-root": makeNode("main"),
  "report-title": makeNode("span"),
  "report-meta": makeNode("div"),
  "status-badges": makeNode("span"),
  "lens-report-data": makeNode("script", JSON.stringify(payload)),
  "sidebar-toggle": makeNode("button"),
  "sidebar": makeNode("aside"),
  "theme-toggle": makeNode("button"),
  "print-btn": makeNode("button"),
  "window-toggle": makeNode("div"),
  "nav-list": makeNode("nav"),
};
const document = {
  documentElement: makeNode("html"),
  getElementById(id) { return ids[id] || makeNode("div"); },
  createElement(tag) { return makeNode(tag); },
  createElementNS(_ns, tag) { return makeNode(tag); },
  createTextNode(txt) { return makeNode("#text", txt); },
  querySelectorAll() { return []; },
};
const context = {
  console,
  document,
  window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
  URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
};

function findAll(root, pred, out = []) {
  if (!root) return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}
function nodeText(n) {
  if (!n) return "";
  if (n.tagName === "#TEXT") return n.textContent || "";
  if (n.children && n.children.length) return n.children.map(nodeText).join("");
  return n.textContent || "";
}
function svgTextNodes(svg) {
  return findAll(svg, (n) => n.tagName === "TEXT").map((n) => ({
    node: n,
    text: nodeText(n),
    x: Number(n.getAttribute("x")),
    y: Number(n.getAttribute("y")),
    anchor: n.getAttribute("text-anchor") || "start",
    fill: n.getAttribute("fill"),
    fontSize: n.getAttribute("font-size"),
  }));
}
function bbox(lbl) {
  const w = lbl.text === "Warmup" ? 44 : 32;
  const left = lbl.anchor === "end" ? lbl.x - w : lbl.x;
  return { left, right: left + w, y: lbl.y };
}
function boundaryLineX(svg, strokeColor) {
  const lines = findAll(svg, (n) => n.tagName === "LINE" && n.getAttribute("stroke-dasharray") === "4 3" && (!strokeColor || n.getAttribute("stroke") === strokeColor));
  return lines.length ? Number(lines[0].getAttribute("x1")) : null;
}
function assertInsideSvg(lbl, svgWidth) {
  const b = bbox(lbl);
  assert(b.left >= 2 - 0.01 && b.right <= svgWidth - 2 + 0.01,
    `${lbl.text} clipped outside SVG [2, ${svgWidth - 2}]: left=${b.left}, right=${b.right}, anchor=${lbl.anchor}`);
}
function assertNoCollision(a, b) {
  const ba = bbox(a);
  const bb = bbox(b);
  const sameRow = Math.abs(ba.y - bb.y) < 10;
  const horizOverlap = !(ba.right + 4 <= bb.left || bb.right + 4 <= ba.left);
  assert(!(sameRow && horizOverlap),
    `Collision between ${a.text} (${JSON.stringify(ba)}) and ${b.text} (${JSON.stringify(bb)})`);
}

const src = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\nglobalThis.__testExports = { LineBandChart, StackedAreaChart, SwimlaneCanvas };\n})();\n"
);

(async () => {
  vm.runInNewContext(src, context);
  await new Promise((r) => setTimeout(r, 20));
  const ex = context.__testExports;

  // 1a. Default "profiling" window renders Drain without Warmup on Overview, while Swimlane renders Warmup
  const profOverview = ids["report-root"].children.find((n) => n.getAttribute("data-page") === "overview");
  const profThrSvg = findAll(findAll(profOverview, (n) => n.getAttribute("id") === "o-thr")[0], (n) => n.tagName === "SVG")[0];
  const profThrLabels = svgTextNodes(profThrSvg);
  assert(!profThrLabels.some((l) => l.text === "Warmup"), "Profiling window should not render Warmup label");
  const profDrain = profThrLabels.find((l) => l.text === "Drain");
  assert(profDrain && profDrain.anchor === "start", "Profiling window should render Drain start-anchored on the right");

  // Switch windowMode to "full" via #window-toggle click
  const fullBtn = makeNode("button");
  fullBtn.setAttribute("data-window", "full");
  ids["window-toggle"].dispatchEvent("click", { target: { closest: () => fullBtn } });

  // 1b. Full rendered pages: Overview charts and Requests Swimlane have Warmup on LEFT and Drain on RIGHT
  const overviewPage = ids["report-root"].children.find((n) => n.getAttribute("data-page") === "overview");
  assert(overviewPage, "Missing overview page");
  for (const secId of ["o-thr", "o-perf", "o-conc"]) {
    const sec = findAll(overviewPage, (n) => n.getAttribute("id") === secId)[0];
    assert(sec, `Missing section ${secId}`);
    const svg = findAll(sec, (n) => n.tagName === "SVG")[0];
    assert(svg, `Missing SVG in ${secId}`);
    const labels = svgTextNodes(svg);
    const warmupLbl = labels.find((l) => l.text === "Warmup");
    const drainLbl = labels.find((l) => l.text === "Drain");
    assert(warmupLbl, `Missing Warmup label in ${secId}`);
    assert(drainLbl, `Missing Drain label in ${secId}`);
    assert(warmupLbl.fill === "#93a4bd" && warmupLbl.fontSize === "10", `Unexpected Warmup style in ${secId}`);
    const wx = boundaryLineX(svg, "#f5a623");
    const dx = boundaryLineX(svg, "#93a4bd");
    assert(wx !== null && warmupLbl.anchor === "end" && warmupLbl.x < wx,
      `${secId} Warmup must sit LEFT of wx (${wx}): x=${warmupLbl.x}, anchor=${warmupLbl.anchor}`);
    assert(dx !== null && drainLbl.anchor === "start" && drainLbl.x > dx,
      `${secId} Drain must sit RIGHT of dx (${dx}): x=${drainLbl.x}, anchor=${drainLbl.anchor}`);
    assertInsideSvg(warmupLbl, 640);
    assertInsideSvg(drainLbl, 640);
    assertNoCollision(warmupLbl, drainLbl);
  }

  const reqPage = ids["report-root"].children.find((n) => n.getAttribute("data-page") === "requests");
  const lanesSec = findAll(reqPage, (n) => n.getAttribute("id") === "r-lanes")[0];
  const swimSvg = findAll(lanesSec, (n) => n.tagName === "SVG")[0];
  assert(swimSvg, "Missing Swimlane SVG");
  const swimLabels = svgTextNodes(swimSvg);
  const swimWarmup = swimLabels.find((l) => l.text === "Warmup");
  assert(swimWarmup, "Missing Warmup label in SwimlaneCanvas");
  const swimWx = boundaryLineX(swimSvg, "#f59e0b");
  assert(swimWx !== null && swimWarmup.anchor === "end" && swimWarmup.x < swimWx,
    `SwimlaneCanvas Warmup must sit LEFT of wx (${swimWx}): x=${swimWarmup.x}, anchor=${swimWarmup.anchor}`);
  assertInsideSvg(swimWarmup, 640);
  // Warmup label in SwimlaneCanvas must sit in the reserved top band strictly above the first slot bar
  const slotBarRects = swimSvg.children.filter((c) => c.tagName === "RECT" && c.getAttribute("rx") != null);
  assert(slotBarRects.length > 0, "Expected SwimlaneCanvas slot bar rects");
  const firstBarY = Math.min(...slotBarRects.map((r) => Number(r.getAttribute("y"))));
  assert(swimWarmup.y < firstBarY, `Swimlane Warmup label y (${swimWarmup.y}) must be strictly above first slot bar y (${firstBarY})`);
  // Warmup label in SwimlaneCanvas must also be appended after slot bars
  const lastTextIdx = swimSvg.children.findIndex((c) => c === swimWarmup.node);
  const lastBarIdx = swimSvg.children.map((c) => c.tagName === "RECT" && c.getAttribute("rx") != null).lastIndexOf(true);
  assert(lastTextIdx > lastBarIdx, `Swimlane Warmup label (${lastTextIdx}) must render after slot bars (${lastBarIdx})`);

  // 2. LineBandChart & StackedAreaChart edge cases
  const baseSeries = [{ label: "S", points: [{ x: 0, y: 1 }, { x: 30, y: 2 }, { x: 60, y: 1.5 }] }];
  for (const [name, renderFn] of [
    ["LineBandChart", ex.LineBandChart],
    ["StackedAreaChart", ex.StackedAreaChart],
  ]) {
    // 2a. Small warmup (5s and 1.5s) -> stays LEFT of wx (anchor="end", x < wx), no SVG left-edge clipping, clear above Y ticks
    for (const smallWarmup of [5, 1.5]) {
      const hostSmallW = makeNode("div");
      renderFn(hostSmallW, { warmupEnd: smallWarmup, drainStart: 45, series: baseSeries });
      const svg = hostSmallW.children[0];
      const labels = svgTextNodes(svg);
      const wLbl = labels.find((l) => l.text === "Warmup");
      const dLbl = labels.find((l) => l.text === "Drain");
      const wx = boundaryLineX(svg, "#f5a623");
      const dx = boundaryLineX(svg, "#93a4bd");
      assert(wLbl && wLbl.anchor === "end" && wLbl.x < wx, `${name} small warmup (${smallWarmup}s) must stay LEFT of wx`);
      assert(dLbl && dLbl.anchor === "start" && dLbl.x > dx, `${name} drain must stay RIGHT of dx`);
      assertInsideSvg(wLbl, 640);
      assertInsideSvg(dLbl, 640);
      const yTicks = labels.filter((l) => l.text !== "Warmup" && l.text !== "Drain" && !l.text.endsWith("s"));
      const topYTick = Math.min(...yTicks.map((t) => t.y));
      assert(wLbl.y + 10 <= topYTick, `${name} small warmup (${smallWarmup}s) y (${wLbl.y}) must stay above top Y-axis tick (${topYTick})`);
    }

    // 2b. Near-right-edge drain (55s and 59s out of 60s) -> stays RIGHT of dx (anchor="start", x > dx), no SVG right-edge clipping
    for (const lateDrain of [55, 59]) {
      const hostRightD = makeNode("div");
      renderFn(hostRightD, { warmupEnd: 10, drainStart: lateDrain, series: baseSeries });
      const svg = hostRightD.children[0];
      const lRightD = svgTextNodes(svg);
      const w2 = lRightD.find((l) => l.text === "Warmup");
      const d2 = lRightD.find((l) => l.text === "Drain");
      const wx = boundaryLineX(svg, "#f5a623");
      const dx = boundaryLineX(svg, "#93a4bd");
      assert(w2 && w2.anchor === "end" && w2.x < wx, `${name} Warmup must stay LEFT of wx`);
      assert(d2 && d2.anchor === "start" && d2.x > dx, `${name} right-edge Drain (${lateDrain}s) must stay RIGHT of dx`);
      assertInsideSvg(w2, 640);
      assertInsideSvg(d2, 640);
      assertNoCollision(w2, d2);
    }

    // 2c. Warmup near right edge -> stays LEFT of wx, no clipping
    const hostRightW = makeNode("div");
    renderFn(hostRightW, { warmupEnd: 59, series: baseSeries });
    const rightWSvg = hostRightW.children[0];
    const wRight = svgTextNodes(rightWSvg).find((l) => l.text === "Warmup");
    const wxRight = boundaryLineX(rightWSvg, "#f5a623");
    assert(wRight && wRight.anchor === "end" && wRight.x < wxRight, `${name} right-edge Warmup must stay LEFT of wx`);
    assertInsideSvg(wRight, 640);

    // 2d. Close Warmup & Drain (28s/30s and 1.5s/2.5s) -> point away from each other, no collision
    for (const [wEnd, dStart] of [[28, 30], [1.5, 2.5]]) {
      const hostClose = makeNode("div");
      renderFn(hostClose, { warmupEnd: wEnd, drainStart: dStart, series: baseSeries });
      const svg = hostClose.children[0];
      const lClose = svgTextNodes(svg);
      const w3 = lClose.find((l) => l.text === "Warmup");
      const d3 = lClose.find((l) => l.text === "Drain");
      const wx = boundaryLineX(svg, "#f5a623");
      const dx = boundaryLineX(svg, "#93a4bd");
      assert(w3 && w3.anchor === "end" && w3.x < wx, `${name} (${wEnd}/${dStart}) Warmup must stay LEFT of wx`);
      assert(d3 && d3.anchor === "start" && d3.x > dx, `${name} (${wEnd}/${dStart}) Drain must stay RIGHT of dx`);
      assertInsideSvg(w3, 640);
      assertInsideSvg(d3, 640);
      assertNoCollision(w3, d3);
    }

    // 2e. Degenerate overlap (warmupEnd > drainStart with overlapping label spans) -> keep side invariants and stagger vertically
    const hostOverlap = makeNode("div");
    renderFn(hostOverlap, { warmupEnd: 30, drainStart: 28, series: baseSeries });
    const overlapSvg = hostOverlap.children[0];
    const lOverlap = svgTextNodes(overlapSvg);
    const w5 = lOverlap.find((l) => l.text === "Warmup");
    const d5 = lOverlap.find((l) => l.text === "Drain");
    assert(w5 && w5.anchor === "end", `${name} degenerate Warmup must stay end-anchored`);
    assert(d5 && d5.anchor === "start", `${name} degenerate Drain must stay start-anchored`);
    assert(d5.y > w5.y, `${name} degenerate Drain (${d5.y}) must stagger below Warmup (${w5.y})`);
    assertInsideSvg(w5, 640);
    assertInsideSvg(d5, 640);
    assertNoCollision(w5, d5);
  }

  // 2f. LineBandChart with rightLabel and near-right-edge drain -> stays clear above right Y-axis ticks
  const hostRightAxis = makeNode("div");
  ex.LineBandChart(hostRightAxis, {
    warmupEnd: 5,
    drainStart: 59,
    rightLabel: "tok/s",
    series: [
      { label: "L", points: [{ x: 0, y: 1 }, { x: 60, y: 2 }] },
      { label: "R", axis: "right", points: [{ x: 0, y: 100 }, { x: 60, y: 200 }] },
    ],
  });
  const rightAxisSvg = hostRightAxis.children[0];
  const rightAxisLabels = svgTextNodes(rightAxisSvg);
  const raDrain = rightAxisLabels.find((l) => l.text === "Drain");
  const raDx = boundaryLineX(rightAxisSvg, "#93a4bd");
  assert(raDrain && raDrain.anchor === "start" && raDrain.x > raDx, "Right-axis LineBandChart Drain must stay RIGHT of dx");
  assertInsideSvg(raDrain, 640);

  // 3. SwimlaneCanvas small-warmup & right-edge warmup -> stays LEFT of wx, never clips off x=0
  for (const [dur, wSec] of [[60, 5], [60, 0.2], [10, 10]]) {
    const swimHost = makeNode("div");
    ex.SwimlaneCanvas(
      swimHost,
      {
        duration_seconds: dur,
        slot_lanes: {
          peak_slots: 1,
          total_requests: 1,
          session_count: 1,
          sample_stride: 1,
          items: [{ slot: 0, start_s: 0, end_s: Math.min(5, dur), cache_hit_pct: 0, tier: "cold" }],
        },
      },
      null,
      wSec
    );
    const swimCaseSvg = findAll(swimHost, (n) => n.tagName === "SVG")[0];
    const swimLbl = svgTextNodes(swimCaseSvg).find((l) => l.text === "Warmup");
    const swimCaseWx = boundaryLineX(swimCaseSvg, "#f59e0b");
    assert(swimLbl && swimLbl.anchor === "end", `SwimlaneCanvas (${wSec}s/${dur}s) Warmup must stay end-anchored`);
    assert(swimLbl.x < swimCaseWx, `SwimlaneCanvas (${wSec}s/${dur}s) Warmup x (${swimLbl.x}) must be < wx (${swimCaseWx})`);
    assertInsideSvg(swimLbl, 640);
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v18 boundary label VM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v20_serving_flow_banner_and_grafana_subtitle_cleanup(sim_env: Path) -> None:
    """Offline v20 report omits topoBanner pod-list filter-bar from #s-flow and raw ISO Window subtitle from #s-grafana."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000027", model_name="Qwen/Qwen3-32B")
    server_with_pods = _sample_server_telemetry()
    server_with_pods.update(
        {
            "source": "prometheus",
            "namespace": "llm-d-sim",
            "topology_mode": "disaggregated",
            "window": {"start": "2026-11-26T00:00:00Z", "end": "2026-11-26T00:05:00Z"},
            "per_pod": [
                {"pod": "qwen-prefill-0", "role": "prefill", "request_rate_rps": {"mean": 4.2}},
                {"pod": "qwen-decode-0", "role": "decode", "request_rate_rps": {"mean": 8.4}},
            ],
        }
    )
    html_with_pods = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=server_with_pods,
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_with_pods

    html_no_pods = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry={"available": False, "status": "unavailable", "namespace": "llm-d-sim", "per_pod": []},
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )

    embedded_with_pods = _extract_embedded_report_json(html_with_pods)
    embedded_no_pods = _extract_embedded_report_json(html_no_pods)
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js renderServing"

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const rawSrc = fs.readFileSync(process.argv[1], "utf8");

function runCase(payload) {
  function makeNode(tag, text) {
    const listeners = {};
    const attrs = {};
    const node = {
      tagName: String(tag || "div").toUpperCase(),
      children: [],
      style: {},
      className: "",
      textContent: text || "",
      clientWidth: 640,
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
      getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
      appendChild(c) {
        if (c != null) {
          if (typeof c === "string") node.children.push(makeNode("#text", c));
          else node.children.push(c);
        }
        return c;
      },
      append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
      replaceChildren(...items) { node.children = []; node.append(...items); },
      addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
      closest() { return null; },
      querySelectorAll() { return []; },
      getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 220 }; },
    };
    return node;
  }
  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", JSON.stringify(payload)),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const document = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(txt) { return makeNode("#text", txt); },
    querySelectorAll() { return []; },
  };
  const context = {
    console,
    document,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  return (async () => {
    vm.runInNewContext(rawSrc, context);
    await new Promise((r) => setTimeout(r, 20));
    return ids["report-root"];
  })();
}

function findAll(root, pred, out = []) {
  if (!root) return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}

(async () => {
  const rootWithPods = await runCase(cases.withPods);
  const servingWithPods = rootWithPods.children.find((n) => n.getAttribute("data-page") === "serving");
  assert(servingWithPods, "Missing serving page");
  const flowWithPods = findAll(servingWithPods, (n) => n.getAttribute("id") === "s-flow")[0];
  assert(flowWithPods, "Missing #s-flow section");
  const badgesWithPods = findAll(servingWithPods, (n) => (n.className || "").split(/\s+/).includes("badge"));
  const badgeTexts = badgesWithPods.map((b) => b.textContent);
  for (const forbiddenPrefix of ["Source:", "Topology:", "NS:", "Model:"]) {
    assert(!badgeTexts.some((t) => t.startsWith(forbiddenPrefix)), `Unexpected header badge starting with ${forbiddenPrefix}: ${JSON.stringify(badgeTexts)}`);
  }
  const flowBadges = findAll(flowWithPods, (n) => (n.className || "").split(/\s+/).includes("badge")).map((b) => b.textContent);
  assert(!flowBadges.includes("prefill: qwen-prefill-0"), `Unexpected topoBanner pod badge in #s-flow: ${JSON.stringify(flowBadges)}`);
  assert(!flowBadges.includes("decode: qwen-decode-0"), `Unexpected topoBanner pod badge in #s-flow: ${JSON.stringify(flowBadges)}`);
  const flowFilterBarsWithBadges = findAll(
    flowWithPods,
    (n) => (n.className || "").split(/\s+/).includes("filter-bar") && findAll(n, (c) => (c.className || "").split(/\s+/).includes("badge")).length > 0
  );
  assert(flowFilterBarsWithBadges.length === 0, `Expected no topoBanner .filter-bar with pod badges in #s-flow, got ${flowFilterBarsWithBadges.length}`);

  const grafanaWithPods = findAll(servingWithPods, (n) => n.getAttribute("id") === "s-grafana")[0];
  assert(grafanaWithPods, "Missing #s-grafana section");
  const grafanaSubNodes = findAll(grafanaWithPods, (n) => (n.className || "").split(/\s+/).includes("section-sub"));
  const grafanaSubTexts = grafanaSubNodes.map((n) => n.textContent);
  assert(
    grafanaSubTexts.includes("Cluster Grafana dashboards scoped to this simulation run"),
    `Expected clean #s-grafana subtitle, got ${JSON.stringify(grafanaSubTexts)}`
  );
  assert(
    !grafanaSubTexts.some((t) => t.includes("Window [") || t.includes("2026-11-26T00:00:00Z")),
    `Unexpected raw ISO Window subtitle in #s-grafana: ${JSON.stringify(grafanaSubTexts)}`
  );

  const rootNoPods = await runCase(cases.noPods);
  const servingNoPods = rootNoPods.children.find((n) => n.getAttribute("data-page") === "serving");
  assert(servingNoPods, "Missing serving page (no pods)");
  const flowNoPods = findAll(servingNoPods, (n) => n.getAttribute("id") === "s-flow")[0];
  assert(flowNoPods, "Missing #s-flow section (no pods)");
  const emptyFilterBars = findAll(flowNoPods, (n) => (n.className || "").split(/\s+/).includes("filter-bar"));
  assert(emptyFilterBars.length === 0, `Expected no .filter-bar in #s-flow when no pods exist, got ${emptyFilterBars.length}`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"withPods": embedded_with_pods, "noPods": embedded_no_pods}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node v20 renderServing check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_html_report_v21_configured_concurrency_reference_line(sim_env: Path) -> None:
    """Offline v21 report renders a configured-concurrency reference line in #o-conc when set and omits it otherwise."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    aiperf_conc_task = _make_task(
        "00000028",
        backend="aiperf",
        backend_options={"concurrency": 64, "warmup_seconds": 0.0, "e2e_slo": 2.0},
        fixed_schedule=False,
    )

    aiperf_fixed_task = _make_task(
        "00000029",
        backend="aiperf",
        backend_options={"warmup_seconds": 0.0, "e2e_slo": 2.0},
        fixed_schedule=True,
    )

    trace_replayer_task = _make_task(
        "0000002a",
        backend="trace-replayer",
        backend_options={"threads": 32, "num_producer": 4, "channel_capacity": 128, "warmup_seconds": 0.0},
    )

    client_series = _sample_client_series()
    client_series["warmup_seconds"] = 0.0
    client_series["buckets"] = [
        {"t": 0.0, "rps": 10.0, "goodput_rps": 10.0, "in_flight_concurrency": 4.0},
        {"t": 5.0, "rps": 12.5, "goodput_rps": 12.0, "in_flight_concurrency": 12.0},
        {"t": 10.0, "rps": 11.0, "goodput_rps": 11.0, "in_flight_concurrency": 8.0},
    ]

    html_conc = render_html_report(
        aiperf_conc_task,
        client_series=client_series,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in html_conc
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
        assert forbidden not in html_conc

    # Also verify custom run_manifest lacking replay.backend_options still preserves task.backend_options & fixed_schedule
    html_custom_manifest = render_html_report(
        aiperf_conc_task,
        client_series=client_series,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        run_manifest={"schema_version": 1, "backend": "aiperf"},
    )
    html_fixed = render_html_report(
        aiperf_fixed_task,
        client_series=client_series,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    html_replayer = render_html_report(
        trace_replayer_task,
        client_series=client_series,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )

    embedded_conc = _extract_embedded_report_json(html_conc)
    embedded_custom = _extract_embedded_report_json(html_custom_manifest)
    embedded_fixed = _extract_embedded_report_json(html_fixed)
    embedded_replayer = _extract_embedded_report_json(html_replayer)

    assert embedded_conc["task"]["fixed_schedule"] is False
    assert embedded_conc["task"]["backend_options"]["concurrency"] == 64
    assert embedded_custom["task"]["backend_options"]["concurrency"] == 64
    assert embedded_fixed["task"]["fixed_schedule"] is True

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js #o-conc refLine"

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const rawSrc = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\nglobalThis.__testExports = { LineBandChart, resolveConfiguredConcurrency };\n})();\n"
);

function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    style: {},
    className: "",
    textContent: text || "",
    clientWidth: 640,
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
    getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
    appendChild(c) {
      if (c != null) {
        if (typeof c === "string") node.children.push(makeNode("#text", c));
        else node.children.push(c);
      }
      return c;
    },
    append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
    replaceChildren(...items) { node.children = []; node.append(...items); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    closest() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 220 }; },
  };
  return node;
}

function runCase(payload) {
  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", JSON.stringify(payload)),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const document = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(txt) { return makeNode("#text", txt); },
    querySelectorAll() { return []; },
  };
  const context = {
    console,
    document,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  return (async () => {
    vm.runInNewContext(rawSrc, context);
    await new Promise((r) => setTimeout(r, 20));
    return { root: ids["report-root"], ex: context.__testExports };
  })();
}

function findAll(root, pred, out = []) {
  if (!root) return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}
function nodeText(n) {
  if (!n) return "";
  if (n.tagName === "#TEXT") return n.textContent || "";
  if (n.children && n.children.length) return n.children.map(nodeText).join("");
  return n.textContent || "";
}

(async () => {
  // 1. AIPerf with concurrency=64 (observed max in_flight=12)
  for (const [label, payload] of [["withConc", cases.withConc], ["customManifest", cases.customManifest]]) {
    const { root, ex } = await runCase(payload);
    assert(ex.resolveConfiguredConcurrency(payload) === 64, `${label}: expected resolveConfiguredConcurrency=64`);
    const overview = root.children.find((n) => n.getAttribute("data-page") === "overview");
    assert(overview, `${label}: missing overview page`);
    const concSec = findAll(overview, (n) => n.getAttribute("id") === "o-conc")[0];
    assert(concSec, `${label}: missing #o-conc`);

    const svg = findAll(concSec, (n) => n.tagName === "SVG")[0];
    assert(svg, `${label}: missing SVG in #o-conc`);
    const svgHeight = Number(svg.getAttribute("height")) || 220;

    const refLines = findAll(svg, (n) => n.tagName === "LINE" && n.getAttribute("data-ref-line") === "configured-concurrency");
    assert(refLines.length === 1, `${label}: expected 1 configured-concurrency refLine, got ${refLines.length}`);
    const rl = refLines[0];
    const y1 = Number(rl.getAttribute("y1"));
    const y2 = Number(rl.getAttribute("y2"));
    assert(Number.isFinite(y1) && y1 === y2, `${label}: refLine must be horizontal (y1=${y1}, y2=${y2})`);
    assert(y1 >= 16 && y1 <= svgHeight - 28, `${label}: refLine y (${y1}) outside plot bounds`);
    assert(rl.getAttribute("stroke-dasharray") === "6 4", `${label}: unexpected refLine stroke-dasharray`);

    const texts = findAll(svg, (n) => n.tagName === "TEXT").map((n) => ({
      text: nodeText(n),
      x: Number(n.getAttribute("x")),
      y: Number(n.getAttribute("y")),
      anchor: n.getAttribute("text-anchor"),
    }));
    const concLabel = texts.find((t) => t.text.includes("Configured: 64"));
    assert(concLabel, `${label}: missing 'Configured: 64' SVG text label`);
    assert(concLabel.y >= 16 + 10 && concLabel.y <= svgHeight - 28, `${label}: label y (${concLabel.y}) clipped or outside plot`);
    assert(concLabel.anchor === "end", `${label}: label must be right-aligned (text-anchor=end)`);

    const legendItems = findAll(concSec, (n) => (n.className || "").split(/\s+/).includes("legend-item")).map(nodeText);
    assert(legendItems.some((t) => t.includes("Configured Concurrency (64)")), `${label}: missing legend item, got ${JSON.stringify(legendItems)}`);

    // Verify empty chart with refLine still renders empty-note
    const emptyHost = makeNode("div");
    ex.LineBandChart(emptyHost, {
      refLine: { value: 64, label: "Configured: 64", legendLabel: "Configured Concurrency (64)" },
      series: [{ label: "Client In-Flight Requests", points: [] }],
    });
    assert(emptyHost.children.length === 1 && emptyHost.children[0].className === "empty-note", "Empty chart with refLine must render empty-note");

    // Verify very small refLine (value=1) has headroom and is not clipped
    const smallHost = makeNode("div");
    ex.LineBandChart(smallHost, {
      refLine: { value: 1, label: "Configured: 1", legendLabel: "Configured Concurrency (1)" },
      series: [{ label: "Client In-Flight Requests", points: [{ x: 0, y: 0 }, { x: 10, y: 0 }] }],
    });
    const smallSvg = findAll(smallHost, (n) => n.tagName === "SVG")[0];
    const smallLbl = findAll(smallSvg, (n) => n.tagName === "TEXT").find((t) => nodeText(t) === "Configured: 1");
    assert(smallLbl && Number(smallLbl.getAttribute("y")) >= 26, "Small refLine=1 label must not clip at top");
  }

  // 2. Fixed-schedule AIPerf without concurrency & trace-replayer with threads=32 -> omitted completely
  for (const [label, payload] of [["fixedAiperf", cases.fixedAiperf], ["traceReplayer", cases.traceReplayer]]) {
    const { root, ex } = await runCase(payload);
    assert(ex.resolveConfiguredConcurrency(payload) === null, `${label}: expected resolveConfiguredConcurrency=null`);
    const overview = root.children.find((n) => n.getAttribute("data-page") === "overview");
    const concSec = findAll(overview, (n) => n.getAttribute("id") === "o-conc")[0];
    assert(concSec, `${label}: missing #o-conc`);
    const svg = findAll(concSec, (n) => n.tagName === "SVG")[0];
    const refLines = findAll(svg, (n) => n.tagName === "LINE" && n.getAttribute("data-ref-line") != null);
    assert(refLines.length === 0, `${label}: expected no refLine, got ${refLines.length}`);
    const texts = findAll(svg, (n) => n.tagName === "TEXT").map(nodeText);
    assert(!texts.some((t) => t.includes("Configured:")), `${label}: unexpected Configured label in SVG: ${JSON.stringify(texts)}`);
    const legendItems = findAll(concSec, (n) => (n.className || "").split(/\s+/).includes("legend-item")).map(nodeText);
    assert(!legendItems.some((t) => t.includes("Configured Concurrency")), `${label}: unexpected Configured Concurrency legend item: ${JSON.stringify(legendItems)}`);
  }

  // 3. Direct resolveConfiguredConcurrency edge cases
  const { ex } = await runCase(cases.withConc);
  assert(ex.resolveConfiguredConcurrency({ task: { backend: "trace_replayer", backend_options: { concurrency: 16, threads: 32 } } }) === null, "trace_replayer must return null");
  assert(ex.resolveConfiguredConcurrency({ task: { backend: "aiperf", backend_options: { max_concurrency: 32 } } }) === 32, "max_concurrency fallback");
  assert(ex.resolveConfiguredConcurrency({ task: { backend: "aiperf", backend_options: { session_concurrency: 12 } } }) === 12, "session_concurrency fallback");
  assert(ex.resolveConfiguredConcurrency({ task: { backend: "aiperf", backend_options: { concurrency: 0 } } }) === null, "zero concurrency must return null");
  assert(ex.resolveConfiguredConcurrency({ task: { backend: "aiperf", backend_options: { concurrency: true } } }) === null, "boolean concurrency must return null");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(
            {
                "withConc": embedded_conc,
                "customManifest": embedded_custom,
                "fixedAiperf": embedded_fixed,
                "traceReplayer": embedded_replayer,
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v21 #o-conc refLine check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v22_saturation_timeline_and_cache_routing_grid(sim_env: Path) -> None:
    """Offline v22 report renders quantitative per-role saturation timelines and side-by-side cache/routing cards."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    assert "<!-- lens-report-v42 -->" in template_html
    for css_cls in (
        ".sat-role-card",
        ".sat-state-bar",
        ".sat-state-chip",
        ".sat-fallback-notice",
        ".grid-2 > .section",
    ):
        assert css_cls in template_html

    # Engine-agnostic check for b-timeline HELP_TEXTS / HELP_FORMULAS
    b_tl_text = re.search(r'"b-timeline":\s*"([^"]+)"', charts_js)
    assert b_tl_text is not None
    assert not re.search(r"(vllm:|sglang:|trtllm:|\bvllm\b|\bGPU\b)", b_tl_text.group(1), re.IGNORECASE)

    # Case 1: Disaggregated P/D + EPP (flat timeline_buckets), Warmup + Drain boundaries
    task_disagg = _make_task(
        "0000002b",
        backend_options={"warmup_seconds": 5.0, "e2e_slo": 2.0},
    )
    cs_disagg = _sample_client_series()
    cs_disagg["warmup_seconds"] = 5.0
    cs_disagg["dispatch_duration_seconds"] = 20.0
    cs_disagg["duration_seconds"] = 30.0
    forensics_disagg = {
        "verdict": {"code": "kv_cache_pressure", "label": "KV Cache Pressure", "severity": "warning"},
        "routing_vs_cache": {
            "epp_expected_hit_pct": 80.0,
            "vllm_actual_hit_pct": 62.0,
            "gap_pct": 18.0,
            "series": [
                {"t": 0.0, "epp_hit_pct": 78.0, "vllm_hit_pct": 60.0},
                {"t": 10.0, "epp_hit_pct": 82.0, "vllm_hit_pct": 64.0},
            ],
        },
        "timeline_buckets": [
            {
                "bucket_index": 0,
                "start_seconds": 0.0,
                "end_seconds": 10.0,
                "role": "epp",
                "state": "healthy",
                "running": 4.0,
                "waiting": 0.0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 0,
                "start_seconds": 0.0,
                "end_seconds": 10.0,
                "role": "prefill",
                "state": "healthy",
                "running": 3.0,
                "waiting": 0.0,
                "kv_cache_pct": 42.0,
                "prefix_hit_pct": 65.0,
                "preemptions": 0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 0,
                "start_seconds": 0.0,
                "end_seconds": 10.0,
                "role": "decode",
                "state": "queueing",
                "running": 6.0,
                "waiting": 2.5,
                "kv_cache_pct": 74.0,
                "prefix_hit_pct": 58.0,
                "preemptions": 0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 1,
                "start_seconds": 10.0,
                "end_seconds": 20.0,
                "role": "epp",
                "state": "healthy",
                "running": 6.0,
                "waiting": 0.0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 1,
                "start_seconds": 10.0,
                "end_seconds": 20.0,
                "role": "prefill",
                "state": "queueing",
                "running": 5.0,
                "waiting": 1.5,
                "kv_cache_pct": 68.0,
                "prefix_hit_pct": 61.0,
                "preemptions": 0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 1,
                "start_seconds": 10.0,
                "end_seconds": 20.0,
                "role": "decode",
                "state": "kv_pressure",
                "running": 8.0,
                "waiting": 4.0,
                "kv_cache_pct": 93.5,
                "prefix_hit_pct": 52.0,
                "preemptions": 0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 2,
                "start_seconds": 20.0,
                "end_seconds": 30.0,
                "role": "epp",
                "state": "healthy",
                "running": 2.0,
                "waiting": 0.0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 2,
                "start_seconds": 20.0,
                "end_seconds": 30.0,
                "role": "prefill",
                "state": "healthy",
                "running": 1.0,
                "waiting": 0.0,
                "kv_cache_pct": 30.0,
                "prefix_hit_pct": 70.0,
                "preemptions": 0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 2,
                "start_seconds": 20.0,
                "end_seconds": 30.0,
                "role": "decode",
                "state": "evicting",
                "running": 7.0,
                "waiting": 3.0,
                "kv_cache_pct": 96.0,
                "prefix_hit_pct": 48.0,
                "preemptions": 2,
                "failed_requests": 0,
            },
        ],
    }
    html_disagg = render_html_report(
        task_disagg,
        client_series=cs_disagg,
        server_telemetry=_sample_server_telemetry(),
        forensics=forensics_disagg,
    )
    assert "<!-- lens-report-v42 -->" in html_disagg
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
        assert forbidden not in html_disagg

    # Case 2: Nested { roles } format with single worker role + EPP failed
    task_worker = _make_task("0000002c", backend_options={"warmup_seconds": 0.0})
    forensics_worker = {
        "verdict": {"code": "healthy", "label": "Healthy", "severity": "info"},
        "timeline_buckets": [
            {
                "bucket_index": 0,
                "start_seconds": 0.0,
                "end_seconds": 10.0,
                "roles": {
                    "epp": {"status": "error", "failed_requests": 3},
                    "worker": {
                        "status": "healthy",
                        "running": 4.0,
                        "waiting": 0.0,
                        "kv_cache_pct": 45.0,
                        "prefix_hit_pct": 55.0,
                        "preemptions": 0,
                        "failed_requests": 0,
                    },
                },
            },
            {
                "bucket_index": 1,
                "start_seconds": 10.0,
                "end_seconds": 20.0,
                "roles": {
                    "epp": {"status": "healthy", "failed_requests": 0},
                    "worker": {
                        "status": "kv_pressure",
                        "running": 8.0,
                        "waiting": 1.0,
                        "kv_cache_pct": 91.0,
                        "prefix_hit_pct": 50.0,
                        "preemptions": 0,
                        "failed_requests": 0,
                    },
                },
            },
        ],
    }
    html_worker = render_html_report(
        task_worker,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics=forensics_worker,
    )

    # Case 3: Client-only fallback (Prometheus unavailable)
    task_client = _make_task("0000002d", backend_options={"warmup_seconds": 0.0})
    forensics_client = {
        "verdict": {"code": "errors", "label": "Errors", "severity": "critical"},
        "timeline_buckets": [
            {
                "bucket_index": 0,
                "start_seconds": 0.0,
                "end_seconds": 10.0,
                "role": "client",
                "state": "healthy",
                "running": None,
                "waiting": None,
                "kv_cache_pct": None,
                "preemptions": 0,
                "failed_requests": 0,
            },
            {
                "bucket_index": 1,
                "start_seconds": 10.0,
                "end_seconds": 20.0,
                "role": "client",
                "state": "error",
                "running": None,
                "waiting": None,
                "kv_cache_pct": None,
                "preemptions": 0,
                "failed_requests": 4,
            },
        ],
    }
    html_client = render_html_report(
        task_client,
        client_series=_sample_client_series(),
        server_telemetry={"available": False, "status": "unavailable"},
        forensics=forensics_client,
    )

    # Case 4: Sparse role + single-bucket start === end + non-numeric bucket_index + all-null KV
    task_edge = _make_task("0000002e", backend_options={"warmup_seconds": 0.0})
    forensics_edge = {
        "verdict": {"code": "healthy", "label": "Healthy", "severity": "info"},
        "timeline_buckets": [
            {
                "bucket_index": "bad-idx",
                "start_seconds": 0.0,
                "end_seconds": 0.0,
                "role": "prefill",
                "state": "healthy",
                "running": 2.0,
                "waiting": 0.0,
                "kv_cache_pct": None,
                "preemptions": 0,
            },
            {
                "bucket_index": "bad-idx",
                "start_seconds": 0.0,
                "end_seconds": 0.0,
                "role": "decode",
                "state": "queueing",
                "running": 3.0,
                "waiting": 2.0,
                "kv_cache_pct": None,
                "preemptions": 0,
            },
            {
                "bucket_index": 1,
                "start_seconds": 5.0,
                "end_seconds": 10.0,
                "role": "decode",
                "state": "healthy",
                "running": 1.0,
                "waiting": 0.0,
                "kv_cache_pct": None,
                "preemptions": 0,
            },
        ],
    }
    html_edge = render_html_report(
        task_edge,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics=forensics_edge,
    )

    # Case 5: EPP-only fallback
    task_epp_only = _make_task("0000002f", backend_options={"warmup_seconds": 0.0})
    forensics_epp_only = {
        "verdict": {"code": "healthy", "label": "Healthy", "severity": "info"},
        "timeline_buckets": [
            {
                "bucket_index": 0,
                "start_seconds": 0.0,
                "end_seconds": 5.0,
                "role": "epp",
                "state": "healthy",
                "failed_requests": 0,
            },
        ],
    }
    html_epp_only = render_html_report(
        task_epp_only,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics=forensics_epp_only,
    )

    embedded_disagg = _extract_embedded_report_json(html_disagg)
    embedded_worker = _extract_embedded_report_json(html_worker)
    embedded_client = _extract_embedded_report_json(html_client)
    embedded_edge = _extract_embedded_report_json(html_edge)
    embedded_epp_only = _extract_embedded_report_json(html_epp_only)

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js v22 saturation timeline"

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const rawSrc = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\nglobalThis.__testExports = { ForensicsTimelineSVG, normalizeTimelineBuckets };\n})();\n"
);

function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    parentNode: null,
    children: [],
    style: {},
    className: "",
    textContent: text || "",
    clientWidth: 640,
    classList: {
      add(c) { node.className = Array.from(new Set([...(node.className || "").split(/\s+/).filter(Boolean), c])).join(" "); },
      remove(c) { node.className = (node.className || "").split(/\s+/).filter((x) => x && x !== c).join(" "); },
      toggle(c, f) { if (f) node.classList.add(c); else node.classList.remove(c); },
    },
    setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
    getAttribute(k) {
      if (k === "class") return node.className;
      if (k === "id") return attrs.id ?? null;
      return attrs[k] ?? null;
    },
    appendChild(c) {
      if (c != null) {
        if (typeof c === "string") {
          const t = makeNode("#text", c);
          t.parentNode = node;
          node.children.push(t);
        } else {
          if (c.parentNode && Array.isArray(c.parentNode.children)) {
            const idx = c.parentNode.children.indexOf(c);
            if (idx !== -1) c.parentNode.children.splice(idx, 1);
          }
          c.parentNode = node;
          node.children.push(c);
        }
      }
      return c;
    },
    append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
    replaceChildren(...items) {
      for (const old of node.children) old.parentNode = null;
      node.children = [];
      node.append(...items);
    },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || { target: node }); },
    closest() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 220 }; },
  };
  return node;
}

function runCase(payload) {
  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", JSON.stringify(payload)),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const document = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(txt) { return makeNode("#text", txt); },
    querySelectorAll() { return []; },
  };
  const context = {
    console,
    document,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  return (async () => {
    vm.runInNewContext(rawSrc, context);
    await new Promise((r) => setTimeout(r, 20));
    return { root: ids["report-root"], tooltip: ids["chart-tooltip"] };
  })();
}

function findAll(root, pred, out = []) {
  if (!root) return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}
function nodeText(n) {
  if (!n) return "";
  if (n.tagName === "#TEXT") return n.textContent || "";
  if (n.children && n.children.length) return n.children.map(nodeText).join("");
  return n.textContent || "";
}
function hasClass(n, cls) {
  return (n.className || "").split(/\s+/).includes(cls);
}

(async () => {
  // 1. Disaggregated P/D + EPP + side-by-side #b-cache-turns / #b-routing
  const { root: r1, tooltip: tip1 } = await runCase(cases.disagg);
  const bPage1 = r1.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  assert(bPage1, "Missing bottlenecks page");

  // Task t80: #b-cache-turns and #b-routing share a single .grid-2 parent
  const grids1 = findAll(bPage1, (n) => hasClass(n, "grid-2"));
  const cacheRoutingGrid = grids1.find((g) => {
    const childIds = (g.children || []).map((c) => c.getAttribute("id"));
    return childIds.includes("b-cache-turns") && childIds.includes("b-routing");
  });
  assert(cacheRoutingGrid, "Expected #b-cache-turns and #b-routing inside a single .grid-2 container");

  // Task t79: #b-timeline disaggregated P/D + EPP
  const tlSec1 = findAll(bPage1, (n) => n.getAttribute("id") === "b-timeline")[0];
  assert(tlSec1, "Missing #b-timeline section");
  const eppBadge1 = findAll(tlSec1, (n) => n.getAttribute("data-epp-health") != null)[0];
  assert(eppBadge1 && nodeText(eppBadge1) === "EPP Router: Healthy", `Expected EPP Router: Healthy chip, got ${eppBadge1 && nodeText(eppBadge1)}`);

  const roleCards1 = findAll(tlSec1, (n) => hasClass(n, "sat-role-card"));
  assert(roleCards1.length === 2, `Expected 2 engine role cards (prefill, decode), got ${roleCards1.length}`);
  assert(roleCards1.map((c) => c.getAttribute("data-role")).join(",") === "prefill,decode", "Expected prefill then decode order");
  const pdGrid = findAll(tlSec1, (n) => hasClass(n, "grid-2")).find((g) => g.children.length === 2 && g.children.every((c) => hasClass(c, "sat-role-card")));
  assert(pdGrid, "Expected 2+ engine role cards inside .grid-2");

  const decodeCard = roleCards1[1];
  const decodeText = nodeText(decodeCard);
  assert(decodeText.includes("Evicting"), "Decode worst-state badge should be Evicting");
  assert(decodeText.includes("Peak Waiting:") && decodeText.includes("4 reqs"), `Missing Peak Waiting KPI in decode card: ${decodeText}`);
  assert(decodeText.includes("Peak KV Cache:") && decodeText.includes("96%"), `Missing Peak KV Cache KPI in decode card: ${decodeText}`);
  assert(decodeText.includes("Preemptions:") && decodeText.includes("2"), `Missing Preemptions KPI in decode card: ${decodeText}`);

  const segs = findAll(decodeCard, (n) => hasClass(n, "sat-state-seg"));
  assert(segs.length === 3, `Decode should have 3 non-zero state segments, got ${segs.length}`);
  const chips = findAll(decodeCard, (n) => hasClass(n, "sat-state-chip"));
  assert(chips.length === 5, `Decode should show all 5 state chips, got ${chips.length}`);

  const decodeSvg = findAll(decodeCard, (n) => n.tagName === "SVG")[0];
  assert(decodeSvg, "Missing SVG in decode card");
  const zones = findAll(decodeSvg, (n) => n.getAttribute("data-saturation-zone") != null);
  assert(zones.length === 3, `Expected 3 shaded saturation zone rects in decode SVG, got ${zones.length}`);
  assert(zones.every((z) => z.getAttribute("opacity") === "0.14"), "Saturation zone opacity must be 0.14");
  const kv90Line = findAll(decodeSvg, (n) => n.getAttribute("data-ref-line") === "kv-90-threshold")[0];
  assert(kv90Line, "Missing KV 90% horizontal dashed threshold line");
  const svgTexts = findAll(decodeSvg, (n) => n.tagName === "TEXT").map(nodeText);
  assert(svgTexts.includes("KV 90%"), "Missing KV 90% text label");
  assert(svgTexts.includes("Warmup"), "Missing Warmup label in saturation chart");
  assert(svgTexts.includes("Drain"), "Missing Drain label in saturation chart");

  // Interactive hover tooltip check
  const overlay = findAll(decodeSvg, (n) => n.tagName === "RECT" && n.getAttribute("fill") === "transparent")[0];
  assert(overlay, "Missing hover overlay rect");
  overlay.dispatchEvent("mousemove", { clientX: 320, clientY: 100 });
  assert(tip1.style.display === "block", "Tooltip must display on hover");
  assert(tip1.textContent.includes("Decode") && tip1.textContent.includes("Status:") && tip1.textContent.includes("KV Cache:"), `Unexpected hover tooltip: ${tip1.textContent}`);
  overlay.dispatchEvent("mouseleave", {});
  assert(tip1.style.display === "none", "Tooltip must hide on mouseleave");

  // 2. Nested { roles } single worker role + EPP failed
  const { root: r2 } = await runCase(cases.worker);
  const bPage2 = r2.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const tlSec2 = findAll(bPage2, (n) => n.getAttribute("id") === "b-timeline")[0];
  const eppBadge2 = findAll(tlSec2, (n) => n.getAttribute("data-epp-health") != null)[0];
  assert(eppBadge2 && nodeText(eppBadge2) === "EPP Router: 3 failed", `Expected EPP Router: 3 failed, got ${eppBadge2 && nodeText(eppBadge2)}`);
  const roleCards2 = findAll(tlSec2, (n) => hasClass(n, "sat-role-card"));
  assert(roleCards2.length === 1 && roleCards2[0].getAttribute("data-role") === "worker", "Expected single full-width worker card");
  const workerGrid = findAll(tlSec2, (n) => hasClass(n, "grid-2"));
  assert(workerGrid.length === 0, "Single worker card must not be wrapped in .grid-2");

  // 3. Client-only fallback
  const { root: r3 } = await runCase(cases.clientOnly);
  const bPage3 = r3.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const tlSec3 = findAll(bPage3, (n) => n.getAttribute("id") === "b-timeline")[0];
  const fallbackNotice = findAll(tlSec3, (n) => n.getAttribute("data-client-fallback") === "true")[0];
  assert(fallbackNotice, "Expected client-only fallback notice");
  assert(nodeText(fallbackNotice).includes("Failed Requests: 4"), "Expected failed request count in client fallback");
  const kv90Client = findAll(tlSec3, (n) => n.getAttribute("data-ref-line") === "kv-90-threshold");
  assert(kv90Client.length === 0, "Client-only fallback must not render server KV 90% threshold");

  // 4. Sparse role + single-bucket start === end + non-numeric bucket_index + all-null KV
  const { root: r4 } = await runCase(cases.edge);
  const bPage4 = r4.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const tlSec4 = findAll(bPage4, (n) => n.getAttribute("id") === "b-timeline")[0];
  const prefillCard4 = findAll(tlSec4, (n) => n.getAttribute("data-role") === "prefill")[0];
  const decodeCard4 = findAll(tlSec4, (n) => n.getAttribute("data-role") === "decode")[0];
  assert(prefillCard4 && decodeCard4, "Expected both prefill and decode cards in sparse-role edge case");
  const prefillHealthySeg = findAll(prefillCard4, (n) => n.getAttribute("data-state") === "healthy")[0];
  assert(prefillHealthySeg && String(prefillHealthySeg.getAttribute("style")).includes("width:100.00%"),
    `Sparse prefill role must normalize time-in-state to 100%, got ${prefillHealthySeg && prefillHealthySeg.getAttribute("style")}`);
  const prefillHealthyChip = findAll(prefillCard4, (n) => n.getAttribute("data-state-chip") === "healthy")[0];
  assert(prefillHealthyChip && nodeText(prefillHealthyChip).includes("100%"),
    `Sparse prefill healthy chip must be 100%, got ${prefillHealthyChip && nodeText(prefillHealthyChip)}`);
  assert(nodeText(prefillCard4).includes("Peak KV Cache: N/A"), "All-null KV must render Peak KV Cache: N/A");
  for (const svgEl of findAll(tlSec4, (n) => n.tagName === "SVG")) {
    const serialized = JSON.stringify(svgEl, (k, v) => (k === "parentNode" ? undefined : v));
    assert(!serialized.includes("NaN") && !serialized.includes("Infinity"), `Found NaN/Infinity in SVG: ${serialized}`);
  }
  const singleBucketHost = makeNode("div");
  const { ForensicsTimelineSVG } = (function () {
    const tmpIds = {
      "chart-tooltip": makeNode("div"),
      "report-root": makeNode("main"),
      "report-title": makeNode("span"),
      "report-meta": makeNode("div"),
      "status-badges": makeNode("span"),
      "lens-report-data": makeNode("script", JSON.stringify(cases.edge)),
      "sidebar-toggle": makeNode("button"),
      "sidebar": makeNode("aside"),
      "theme-toggle": makeNode("button"),
      "print-btn": makeNode("button"),
      "window-toggle": makeNode("div"),
      "nav-list": makeNode("nav"),
    };
    const tmpDoc = {
      documentElement: makeNode("html"),
      getElementById(id) { return tmpIds[id] || makeNode("div"); },
      createElement(tag) { return makeNode(tag); },
      createElementNS(_ns, tag) { return makeNode(tag); },
      createTextNode(txt) { return makeNode("#text", txt); },
      querySelectorAll() { return []; },
    };
    const ctx = {
      console, document: tmpDoc,
      window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
      getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
      URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
    };
    vm.runInNewContext(rawSrc, ctx);
    return ctx.__testExports;
  })();
  ForensicsTimelineSVG(singleBucketHost, [
    { bucket_index: "non-num", start_seconds: 0, end_seconds: 0, role: "worker", state: "queueing", running: 2, waiting: 1, kv_cache_pct: null },
  ]);
  const sbSvg = findAll(singleBucketHost, (n) => n.tagName === "SVG")[0];
  assert(sbSvg, "Single-bucket start===end must render SVG");
  const sbZone = findAll(sbSvg, (n) => n.getAttribute("data-saturation-zone") === "queueing")[0];
  assert(sbZone && Number(sbZone.getAttribute("width")) >= 2, "Single-bucket saturation zone must have >=2px width");
  const sbStr = JSON.stringify(sbSvg, (k, v) => (k === "parentNode" ? undefined : v));
  assert(!sbStr.includes("NaN") && !sbStr.includes("Infinity"), `Single-bucket SVG must have finite coords: ${sbStr}`);

  // 5. EPP-only fallback: both header controls and fallback notice retain separate EPP badges (no DOM node detach)
  const { root: r5 } = await runCase(cases.eppOnly);
  const bPage5 = r5.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const tlSec5 = findAll(bPage5, (n) => n.getAttribute("id") === "b-timeline")[0];
  const eppBadges5 = findAll(tlSec5, (n) => n.getAttribute("data-epp-health") != null);
  assert(eppBadges5.length === 2, `Expected 2 distinct EPP badges (header + fallback notice), got ${eppBadges5.length}`);
  const eppNotice5 = findAll(tlSec5, (n) => n.getAttribute("data-epp-only") === "true")[0];
  assert(eppNotice5 && findAll(eppNotice5, (n) => n.getAttribute("data-epp-health") != null).length === 1,
    "EPP-only fallback notice must retain its EPP badge");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(
            {
                "disagg": embedded_disagg,
                "worker": embedded_worker,
                "clientOnly": embedded_client,
                "edge": embedded_edge,
                "eppOnly": embedded_epp_only,
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v22 saturation timeline & grid-2 check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v23_serving_slo_and_kv_tier_share(sim_env: Path) -> None:
    """Offline v23 report renders #s-latency/#o-perf SLO lines (ms units) and #s-cache HBM + Tier Share cards."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    assert "<!-- lens-report-v42 -->" in template_html
    for css_cls in (".kv-tier-grid", ".kv-tier-pill", ".kv-tier-head", ".kv-tier-val", ".kv-tier-sub"):
        assert css_cls in template_html
    for token in (
        "HBM KV Cache Usage over Time",
        "KV Cache Tier Share (HBM · Host Memory · NVMe)",
        "disk_cache_usage_percent",
        "* 1.12",
    ):
        assert token in charts_js

    for help_title in ("HBM KV Cache Usage over Time", "KV Cache Tier Share (HBM · Host Memory · NVMe)"):
        matches = re.findall(rf'"{re.escape(help_title)}":\s*"([^"]+)"', charts_js)
        assert len(matches) == 2, (
            f"Expected both HELP_TEXTS and HELP_FORMULAS entries for {help_title}, got {len(matches)}"
        )
        for entry in matches:
            assert not re.search(r"(vllm:|sglang:|trtllm:|\bvllm\b)", entry, re.IGNORECASE), (
                f"Help entry for {help_title} must be engine-agnostic: {entry!r}"
            )

    def _base_server(series, *, cache_config=None, summary=None):
        return {
            "status": "available",
            "topology_mode": "aggregated",
            "step_seconds": 5,
            "summary": summary
            or {
                "kv_cache_usage_percent": {"mean": 45.0, "max": 60.0},
                "vllm_ttft_p50_ms": {"mean": 40.0},
                "vllm_ttft_p90_ms": {"mean": 80.0},
                "vllm_tpot_p50_ms": {"mean": 8.0},
                "vllm_tpot_p90_ms": {"mean": 12.0},
                "vllm_e2e_p50_ms": {"mean": 200.0},
                "vllm_e2e_p90_ms": {"mean": 300.0},
            },
            "cache_config": cache_config or {"hbm_capacity_tokens": 100000, "source": "engine"},
            "series": series,
            "per_pod": [
                {
                    "pod": "worker-0",
                    "role": "worker",
                    "request_rate_rps": {"mean": 2.0},
                    "kv_cache_usage_percent": {"mean": 45.0, "max": 60.0},
                }
            ],
        }

    low_lat_series = [
        {
            "t": 0,
            "kv_cache_usage_percent": 40.0,
            "vllm_ttft_p50_ms": 40.0,
            "vllm_ttft_p90_ms": 80.0,
            "vllm_tpot_p50_ms": 8.0,
            "vllm_tpot_p90_ms": 12.0,
            "vllm_e2e_p50_ms": 200.0,
            "vllm_e2e_p90_ms": 300.0,
        },
        {
            "t": 5,
            "kv_cache_usage_percent": 50.0,
            "vllm_ttft_p50_ms": 45.0,
            "vllm_ttft_p90_ms": 85.0,
            "vllm_tpot_p50_ms": 9.0,
            "vllm_tpot_p90_ms": 13.0,
            "vllm_e2e_p50_ms": 210.0,
            "vllm_e2e_p90_ms": 310.0,
        },
    ]

    cs_low_lat = _sample_client_series()
    cs_low_lat["buckets"] = [
        {
            "mid_seconds": 5.0,
            "ttft_p50_ms": 40.0,
            "ttft_p90_ms": 80.0,
            "tpot_p50_ms": 8.0,
            "tpot_p90_ms": 12.0,
            "e2e_p50_ms": 200.0,
            "e2e_p90_ms": 300.0,
            "throughput_rps": 2.0,
            "goodput_rps": 2.0,
            "output_tok_per_s": 50.0,
        },
        {
            "mid_seconds": 10.0,
            "ttft_p50_ms": 45.0,
            "ttft_p90_ms": 85.0,
            "tpot_p50_ms": 9.0,
            "tpot_p90_ms": 13.0,
            "e2e_p50_ms": 210.0,
            "e2e_p90_ms": 310.0,
            "throughput_rps": 2.0,
            "goodput_rps": 2.0,
            "output_tok_per_s": 55.0,
        },
    ]

    # Case 1: Configured SLOs strictly above observed P90 (TTFT=500ms, TPOT=50ms, E2E=2000ms)
    html_slo = render_html_report(
        _make_task("00000023", backend_options={"ttft_slo": 0.5, "tpot_slo": 0.05, "e2e_slo": 2.0}),
        client_series=cs_low_lat,
        server_telemetry=_base_server(low_lat_series),
        forensics={
            "verdict": {
                "code": "healthy",
                "label": "Healthy",
                "severity": "info",
                "slo": {"ttft_s": 0.5, "tpot_ms": 50.0, "e2e_s": 2.0},
            }
        },
    )
    assert "<!-- lens-report-v42 -->" in html_slo
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
        assert forbidden not in html_slo
    embedded_slo = json.loads(
        re.search(r'<script type="application/json" id="lens-report-data">(.*?)</script>', html_slo, re.S).group(1)
    )

    # Case 2: No SLO configured anywhere (pass non-empty backend_options so _make_task does not inject default e2e_slo)
    html_no_slo = render_html_report(
        _make_task("00000024", backend_options={"warmup_seconds": 5}),
        client_series=cs_low_lat,
        server_telemetry=_base_server(low_lat_series),
        forensics={"verdict": {"code": "healthy", "label": "Healthy", "severity": "info"}},
    )
    embedded_no_slo = json.loads(
        re.search(r'<script type="application/json" id="lens-report-data">(.*?)</script>', html_no_slo, re.S).group(1)
    )

    # Case 3: Multi-tier HBM + Host Memory + NVMe with byte capacities and an idle sample
    multi_series = [
        {"t": 0, "kv_cache_usage_percent": 0.0, "cpu_cache_usage_percent": 0.0, "disk_cache_usage_percent": 0.0},
        {"t": 5, "kv_cache_usage_percent": 50.0, "cpu_cache_usage_percent": 25.0, "disk_cache_usage_percent": 10.0},
        {"t": 10, "kv_cache_usage_percent": 80.0, "cpu_cache_usage_percent": 40.0, "disk_cache_usage_percent": 20.0},
    ]
    html_multi = render_html_report(
        _make_task("00000025", backend_options={"warmup_seconds": 5}),
        client_series=cs_low_lat,
        server_telemetry=_base_server(
            multi_series,
            cache_config={
                "hbm_capacity_bytes": 80_000_000_000,
                "cpu_capacity_bytes": 160_000_000_000,
                "disk_capacity_bytes": 400_000_000_000,
                "hbm_capacity_tokens": 100000,
                "source": "engine",
            },
            summary={
                "kv_cache_usage_percent": {"mean": 43.33, "max": 80.0},
                "cpu_cache_usage_percent": {"mean": 21.67, "max": 40.0},
                "disk_cache_usage_percent": {"mean": 10.0, "max": 20.0},
            },
        ),
        forensics={"verdict": {"code": "healthy", "label": "Healthy", "severity": "info"}},
    )
    embedded_multi = json.loads(
        re.search(r'<script type="application/json" id="lens-report-data">(.*?)</script>', html_multi, re.S).group(1)
    )

    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js v23 rendering"
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const rawSrc = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\nglobalThis.__testExports = { resolveConfiguredSlos, computeKvTierShareData, LineBandChart };\n})();\n"
);

function findAll(root, pred, out = []) {
  if (!root) return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}

function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    parentNode: null,
    children: [],
    style: {},
    className: "",
    textContent: text || "",
    clientWidth: 640,
    classList: {
      add(c) { node.className = Array.from(new Set([...(node.className || "").split(/\s+/).filter(Boolean), c])).join(" "); },
      remove(c) { node.className = (node.className || "").split(/\s+/).filter((x) => x && x !== c).join(" "); },
      toggle(c, f) { if (f) node.classList.add(c); else node.classList.remove(c); },
    },
    setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
    getAttribute(k) {
      if (k === "class") return node.className;
      if (k === "id") return attrs.id ?? null;
      return attrs[k] ?? null;
    },
    appendChild(c) {
      if (c != null) {
        if (typeof c === "string") {
          const t = makeNode("#text", c);
          t.parentNode = node;
          node.children.push(t);
        } else {
          if (c.parentNode && Array.isArray(c.parentNode.children)) {
            const idx = c.parentNode.children.indexOf(c);
            if (idx !== -1) c.parentNode.children.splice(idx, 1);
          }
          c.parentNode = node;
          node.children.push(c);
        }
      }
      return c;
    },
    append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
    replaceChildren(...items) {
      for (const old of node.children) old.parentNode = null;
      node.children = [];
      node.append(...items);
    },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || { target: node }); },
    closest() { return null; },
    querySelectorAll(sel) {
      const targetTag = String(sel || "").toUpperCase();
      return findAll(node, (c) => c !== node && c.tagName === targetTag);
    },
    getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 220 }; },
  };
  return node;
}

function nodeText(n) {
  if (!n) return "";
  if (n.tagName === "#TEXT") return n.textContent || "";
  if (n.children && n.children.length) return n.children.map(nodeText).join("");
  return n.textContent || "";
}

function hasClass(n, cls) {
  return (n.className || "").split(/\s+/).includes(cls);
}

async function runCase(payload) {
  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", JSON.stringify(payload)),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const document = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(txt) { return makeNode("#text", txt); },
    querySelectorAll() { return []; },
  };
  const context = {
    console,
    document,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  vm.runInNewContext(rawSrc, context);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], ex: context.__testExports };
}

(async () => {
  // 1. #s-latency & #o-perf SLO reference lines, headroom when SLO > P90, and TPOT ms units (t82 + t84)
  const { root: rSlo, ex } = await runCase(cases.slo);
  const sPage = rSlo.children.find((n) => n.getAttribute("data-page") === "serving");
  const oPage = rSlo.children.find((n) => n.getAttribute("data-page") === "overview");
  assert(sPage && oPage, "Missing serving or overview page");

  const sLat = findAll(sPage, (n) => n.getAttribute("id") === "s-latency")[0];
  assert(sLat, "Missing #s-latency");
  const sLatCard = findAll(sLat, (n) => nodeText(n).includes("Server Latency Percentiles over Time"))
    .find((n) => findAll(n, (c) => c.tagName === "BUTTON").length === 3);
  assert(sLatCard, "Missing Server Latency Percentiles over Time card with 3 mode buttons");

  function verifySloCard(card, expectedTab, expectedSloText, expectedLegendSub) {
    const btn = findAll(card, (c) => c.tagName === "BUTTON" && nodeText(c) === expectedTab)[0];
    assert(btn, `Missing ${expectedTab} button`);
    btn.dispatchEvent("click", {});
    const svg = findAll(card, (c) => c.tagName === "SVG")[0];
    assert(svg, `${expectedTab}: missing SVG`);
    const svgH = Number(svg.getAttribute("height")) || 220;
    const sloLines = findAll(svg, (c) => c.tagName === "LINE" && c.getAttribute("data-slo-line") === "true");
    assert(sloLines.length === 1, `${expectedTab}: expected 1 SLO line, got ${sloLines.length}`);
    const sy = Number(sloLines[0].getAttribute("y1"));
    assert(Number.isFinite(sy) && sy >= 16 && sy <= svgH - 28, `${expectedTab}: SLO line y=${sy} out of bounds`);
    const texts = findAll(svg, (c) => c.tagName === "TEXT").map((t) => ({
      text: nodeText(t),
      y: Number(t.getAttribute("y")),
    }));
    const sloLbl = texts.find((t) => t.text === expectedSloText);
    assert(sloLbl, `${expectedTab}: expected SVG label ${expectedSloText}, got ${JSON.stringify(texts)}`);
    assert(sloLbl.y >= 16 + 11 && sloLbl.y <= svgH - 28, `${expectedTab}: SLO label y=${sloLbl.y} clipped`);
    assert(!sloLbl.text.includes("ms/tok") && !sloLbl.text.includes("ms/token"), `${expectedTab}: SLO label must not use ms/tok`);

    const legends = findAll(card, (c) => hasClass(c, "legend-item")).map(nodeText);
    const sloLeg = legends.find((t) => t.includes(expectedLegendSub));
    assert(sloLeg, `${expectedTab}: expected legend ${expectedLegendSub}, got ${JSON.stringify(legends)}`);
    assert(!sloLeg.includes("ms/tok") && !sloLeg.includes("ms/token"), `${expectedTab}: SLO legend must not use ms/tok`);
  }

  verifySloCard(sLatCard, "TTFT", "SLO: 500 ms", "TTFT SLO (500 ms)");
  verifySloCard(sLatCard, "TPOT", "SLO: 50 ms", "TPOT SLO (50 ms)");
  verifySloCard(sLatCard, "E2E", "SLO: 2 s", "E2E SLO (2 s)");

  const oPerf = findAll(oPage, (n) => n.getAttribute("id") === "o-perf")[0];
  assert(oPerf, "Missing #o-perf");
  verifySloCard(oPerf, "TPOT", "SLO: 50 ms", "TPOT SLO (50 ms)");

  // 2. No SLO configured -> omit SLO line & legend completely across all tabs
  const { root: rNoSlo } = await runCase(cases.noSlo);
  const sPageNo = rNoSlo.children.find((n) => n.getAttribute("data-page") === "serving");
  const sLatNo = findAll(sPageNo, (n) => n.getAttribute("id") === "s-latency")[0];
  const sLatCardNo = findAll(sLatNo, (n) => findAll(n, (c) => c.tagName === "BUTTON").length === 3)[0];
  for (const tab of ["TTFT", "TPOT", "E2E"]) {
    const btn = findAll(sLatCardNo, (c) => c.tagName === "BUTTON" && nodeText(c) === tab)[0];
    btn.dispatchEvent("click", {});
    const svg = findAll(sLatCardNo, (c) => c.tagName === "SVG")[0];
    assert(findAll(svg, (c) => c.tagName === "LINE" && c.getAttribute("data-slo-line") === "true").length === 0, `${tab}: unexpected SLO line`);
    const texts = findAll(svg, (c) => c.tagName === "TEXT").map(nodeText);
    assert(!texts.some((t) => t.startsWith("SLO:")), `${tab}: unexpected SLO text label`);
    const legends = findAll(sLatCardNo, (c) => hasClass(c, "legend-item")).map(nodeText);
    assert(!legends.some((t) => t.includes("SLO")), `${tab}: unexpected SLO legend item`);
  }

  // Direct resolveConfiguredSlos source coverage & positive/finite guards
  assert(
    ex.resolveConfiguredSlos({ run_manifest: { slo: { ttft_ms: 400, tpot_s: 0.04, e2e_ms: 1500 } } }).tpot_ms === 40,
    "run_manifest.slo tpot_s->ms failed"
  );
  assert(
    ex.resolveConfiguredSlos({ run_manifest: { replay: { backend_options: { ttft_slo: 0.6, tpot_slo: 0.03, e2e_slo: 3.0 } } } }).ttft_ms === 600,
    "run_manifest.replay.backend_options failed"
  );
  assert(
    ex.resolveConfiguredSlos({ task: { simulation: { backend_options: { tpot_slo: 0.02 } } } }).tpot_ms === 20,
    "task.simulation.backend_options failed"
  );
  const badSlo = ex.resolveConfiguredSlos({ forensics: { verdict: { slo: { ttft_s: 0, tpot_ms: -5, e2e_s: false } } } });
  assert(badSlo.ttft_ms === null && badSlo.tpot_ms === null && badSlo.e2e_ms === null, "Non-positive/boolean SLO must resolve to null");

  // 3. #s-cache HBM-only (t83)
  const sCacheHbm = findAll(sPage, (n) => n.getAttribute("id") === "s-cache")[0];
  assert(sCacheHbm, "Missing #s-cache");
  const cacheGridHbm = findAll(sCacheHbm, (n) => hasClass(n, "grid-2"))[0];
  assert(cacheGridHbm && cacheGridHbm.children.length === 2, "Expected .grid-2 with 2 chart cards in #s-cache");
  assert(nodeText(cacheGridHbm.children[0]).includes("HBM KV Cache Usage over Time"), "Expected renamed HBM KV Cache Usage over Time card");
  assert(nodeText(cacheGridHbm.children[1]).includes("KV Cache Tier Share (HBM · Host Memory · NVMe)"), "Expected KV Cache Tier Share card");
  assert(findAll(cacheGridHbm.children[0], (n) => hasClass(n, "help-icon")).length >= 1, "Missing help badge on HBM KV Cache Usage over Time");
  assert(findAll(cacheGridHbm.children[1], (n) => hasClass(n, "help-icon")).length >= 1, "Missing help badge on KV Cache Tier Share");

  const hbmPills = findAll(cacheGridHbm.children[1], (n) => hasClass(n, "kv-tier-pill"));
  assert(hbmPills.length === 3, `Expected 3 tier pills, got ${hbmPills.length}`);
  assert(nodeText(hbmPills[0]).includes("100%") && nodeText(hbmPills[0]).includes("Mean Usage:"), `Unexpected HBM pill: ${nodeText(hbmPills[0])}`);
  assert(nodeText(hbmPills[1]).includes("0%") && nodeText(hbmPills[1]).includes("Not configured"), `Unexpected Host pill: ${nodeText(hbmPills[1])}`);
  assert(nodeText(hbmPills[2]).includes("0%") && nodeText(hbmPills[2]).includes("Not configured"), `Unexpected NVMe pill: ${nodeText(hbmPills[2])}`);
  const tierLegends = findAll(cacheGridHbm.children[1], (n) => hasClass(n, "legend-item")).map(nodeText);
  assert(
    tierLegends.includes("HBM") && tierLegends.includes("Host Memory (Offload)") && tierLegends.includes("NVMe (Offload)"),
    `Unexpected tier share legend: ${JSON.stringify(tierLegends)}`
  );

  // 4. #s-cache Multi-tier HBM + Host Memory + NVMe (t83)
  const { root: rMulti } = await runCase(cases.multi);
  const sPageMulti = rMulti.children.find((n) => n.getAttribute("data-page") === "serving");
  const sCacheMulti = findAll(sPageMulti, (n) => n.getAttribute("id") === "s-cache")[0];
  const multiPills = findAll(sCacheMulti, (n) => hasClass(n, "kv-tier-pill"));
  assert(multiPills.length === 3, "Expected 3 tier pills in multi-tier case");
  for (const p of multiPills) {
    assert(nodeText(p).includes("Mean Usage:") && !nodeText(p).includes("Not configured"), `Multi-tier pill must show Mean Usage: ${nodeText(p)}`);
  }
  const multiTierRes = ex.computeKvTierShareData(
    [
      { _t: 0, kv_cache_usage_percent: 0, cpu_cache_usage_percent: 0, disk_cache_usage_percent: 0 },
      { _t: 5, kv_cache_usage_percent: 50, cpu_cache_usage_percent: 25, disk_cache_usage_percent: 10 },
    ],
    { hbm_capacity_bytes: 80e9, cpu_capacity_bytes: 160e9, nvme_capacity_bytes: 400e9 },
    {}
  );
  // Idle sample defaults to provisioned capacity share: 80/640 = 12.5%, 160/640 = 25%, 400/640 = 62.5%
  assert(Math.abs(multiTierRes.points[0].hbm_share_pct - 12.5) < 1e-6, "Idle HBM provisioned share mismatch");
  assert(Math.abs(multiTierRes.points[0].host_share_pct - 25.0) < 1e-6, "Idle Host provisioned share mismatch");
  assert(Math.abs(multiTierRes.points[0].nvme_share_pct - 62.5) < 1e-6, "Idle NVMe provisioned share mismatch");
  // Active sample: 50*80=4000, 25*160=4000, 10*400=4000 -> 33.333...% each
  assert(Math.abs(multiTierRes.points[1].hbm_share_pct - 100 / 3) < 1e-6, "Active HBM byte-weighted share mismatch");
  assert(Math.abs(multiTierRes.points[1].host_share_pct - 100 / 3) < 1e-6, "Active Host byte-weighted share mismatch");
  assert(Math.abs(multiTierRes.points[1].nvme_share_pct - 100 / 3) < 1e-6, "Active NVMe byte-weighted share mismatch");

  // 5. host_kv_configured / nvme_kv_configured flags without byte capacities + partial-capacity idle fallback
  const flagsPayload = JSON.parse(JSON.stringify(cases.noSlo));
  flagsPayload.server_telemetry.cache_config = {
    hbm_capacity_bytes: 80e9,
    cpu_capacity_bytes: 160e9,
    host_kv_configured: true,
    nvme_kv_configured: true,
    source: "engine",
  };
  flagsPayload.server_telemetry.series = [
    { t: 0, kv_cache_usage_percent: 0, cpu_cache_usage_percent: 0, disk_cache_usage_percent: 0 },
  ];
  flagsPayload.server_telemetry.summary = {
    kv_cache_usage_percent: { mean: 0, max: 0 },
    cpu_cache_usage_percent: { mean: 0, max: 0 },
    disk_cache_usage_percent: { mean: 0, max: 0 },
  };
  const { root: rFlags } = await runCase(flagsPayload);
  const sPageFlags = rFlags.children.find((n) => n.getAttribute("data-page") === "serving");
  const sCacheFlags = findAll(sPageFlags, (n) => n.getAttribute("id") === "s-cache")[0];
  const flagPills = findAll(sCacheFlags, (n) => hasClass(n, "kv-tier-pill"));
  assert(flagPills.length === 3, "Expected 3 tier pills in flag-configured case");
  assert(nodeText(flagPills[0]).includes("100%"), `Partial-capacity idle HBM must fall back to 100%, got ${nodeText(flagPills[0])}`);
  assert(nodeText(flagPills[1]).includes("0%") && nodeText(flagPills[1]).includes("Mean Usage: 0%"), `host_kv_configured must show Mean Usage, got ${nodeText(flagPills[1])}`);
  assert(nodeText(flagPills[2]).includes("0%") && nodeText(flagPills[2]).includes("Mean Usage: 0%"), `nvme_kv_configured must show Mean Usage, got ${nodeText(flagPills[2])}`);

  for (const svg of findAll(sCacheMulti, (n) => n.tagName === "SVG")) {
    const str = JSON.stringify(svg, (k, v) => (k === "parentNode" ? undefined : v));
    assert(!str.includes("NaN") && !str.includes("Infinity"), `Found NaN/Infinity in #s-cache SVG: ${str}`);
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(
            {
                "slo": embedded_slo,
                "noSlo": embedded_no_slo,
                "multi": embedded_multi,
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v23 serving SLO & KV tier share check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v25_pd_stage_latency_ttft_decomp_and_client_fallback(sim_env: Path) -> None:
    """v25 offline HTML report: role-separated P/D, legacy pooled P/D (with/without NIXL), client_fallback guard, and help entries."""
    from llm_d_lens.simulation.reporting import builder as report_builder
    from llm_d_lens.simulation.telemetry_post import (
        CLIENT_SERIES_SCHEMA_VERSION,
        FORENSICS_SCHEMA_VERSION,
        REPORT_SCHEMA_VERSION,
        SERVER_TELEMETRY_SCHEMA_VERSION,
    )

    assert REPORT_SCHEMA_VERSION >= 9
    assert CLIENT_SERIES_SCHEMA_VERSION >= 9
    assert FORENSICS_SCHEMA_VERSION == 8
    assert SERVER_TELEMETRY_SCHEMA_VERSION >= 8
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    # Verify all HELP_TEXTS / HELP_FORMULAS values are engine-agnostic and required keys exist
    for block_name in ("HELP_TEXTS", "HELP_FORMULAS"):
        m = re.search(rf"const {block_name} = \{{(?P<body>.*?)\n  \}};", charts_js, re.DOTALL)
        assert m is not None, f"Could not locate {block_name} in report_charts.js"
        for line in m.group("body").splitlines():
            if ":" not in line:
                continue
            _, val_part = line.split(":", 1)
            assert not re.search(r"\b(vllm|sglang|trtllm)\b", val_part, re.IGNORECASE), (
                f"{block_name} entry value must be engine-agnostic: {line!r}"
            )
    for required_help in (
        "Stage P50 (KV Transfer / P/D TTFT / Decode Est.)",
        "Stage P50 (Queue / Prefill / Decode)",
        "s-latency",
        "b-ttft-decomp",
        "b-stage-breakdown",
        "Request Stage Latency Breakdown",
    ):
        assert f'"{required_help}":' in charts_js, f"Missing HELP_TEXTS entry for {required_help}"

    # 1. Fresh role-separated P/D payload
    t_pd = _make_task("00000025", name="Role-Separated PD")
    cs_pd = _sample_client_series()
    cs_pd["windows"]["profiling"].update(
        {
            "ttft_ms": {"mean_ms": 200.0, "p50_ms": 180.0, "p95_ms": 340.0, "p99_ms": 400.0},
            "e2e_ms": {"mean_ms": 700.0, "p50_ms": 680.0, "p95_ms": 1100.0, "p99_ms": 1300.0},
        }
    )
    st_pd = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "metric_sources": {"vllm_stage_latencies": "role_separated"},
        "step_seconds": 5,
        "pods": ["prefill-0", "decode-0"],
        "per_pod": [
            {"pod": "prefill-0", "role": "prefill", "request_rate_rps": {"mean": 6.0}},
            {"pod": "decode-0", "role": "decode", "request_rate_rps": {"mean": 6.0}},
        ],
        "router": {
            "scheduler_latency_p50_ms": {"mean": 4.0},
            "scheduler_latency_p90_ms": {"mean": 8.0},
            "scheduler_latency_mean_ms": {"mean": 5.0},
        },
        "summary": {
            "kv_cache_usage_percent": {"mean": 40.0, "max": 55.0},
            "vllm_ttft_p50_ms": {"mean": 165.0},
            "vllm_ttft_p90_ms": {"mean": 300.0},
            "vllm_tpot_p50_ms": {"mean": 8.0},
            "vllm_tpot_p90_ms": {"mean": 12.0},
            "vllm_e2e_p50_ms": {"mean": 665.0},
            "vllm_e2e_p90_ms": {"mean": 1050.0},
            "vllm_queue_time_p50_ms": {"mean": 25.0},
            "vllm_prefill_time_p50_ms": {"mean": 110.0},
            "vllm_decode_time_p50_ms": {"mean": 500.0},
            "nixl_transfer_latency_p50_ms": {"mean": 18.0},
            "nixl_transfer_latency_p90_ms": {"mean": 32.0},
        },
        "series": [
            {
                "t": 0.0,
                "kv_cache_pct": 38.0,
                "router_scheduler_latency_p50_ms": 4.0,
                "vllm_queue_time_p50_ms": 25.0,
                "nixl_transfer_latency_p50_ms": 18.0,
                "vllm_prefill_time_p50_ms": 110.0,
                "vllm_decode_time_p50_ms": 500.0,
            },
            {
                "t": 5.0,
                "kv_cache_pct": 44.0,
                "router_scheduler_latency_p50_ms": 4.0,
                "vllm_queue_time_p50_ms": 28.0,
                "nixl_transfer_latency_p50_ms": 20.0,
                "vllm_prefill_time_p50_ms": 115.0,
                "vllm_decode_time_p50_ms": 510.0,
            },
        ],
    }
    forensics_pd = {
        "schema_version": FORENSICS_SCHEMA_VERSION,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "has_kv_transfer_series": True,
            "has_p90_series": True,
            "p50": {
                "available": True,
                "epp_scheduling_ms": 4.0,
                "queue_time_ms": 25.0,
                "kv_transfer_ms": 18.0,
                "prefill_time_ms": 110.0,
                "decode_time_ms": 500.0,
                "engine_overhead_ms": 12.0,
                "gateway_overhead_ms": 11.0,
                "ttft_stage_ms": 180.0,
                "total_stage_ms": 680.0,
                "share_of_e2e_pct": {
                    "decode_time": 73.5,
                    "prefill_time": 16.2,
                    "queue_time": 3.7,
                    "kv_transfer": 2.6,
                    "engine_overhead": 1.8,
                    "gateway_overhead": 1.6,
                    "epp_scheduling": 0.6,
                },
                "share_of_ttft_pct": {
                    "prefill_time": 61.1,
                    "queue_time": 13.9,
                    "kv_transfer": 10.0,
                    "engine_overhead": 6.7,
                    "gateway_overhead": 6.1,
                    "epp_scheduling": 2.2,
                },
            },
            "p90": {
                "available": True,
                "epp_scheduling_ms": 8.0,
                "queue_time_ms": 45.0,
                "kv_transfer_ms": 32.0,
                "prefill_time_ms": 190.0,
                "decode_time_ms": 750.0,
                "engine_overhead_ms": 35.0,
                "gateway_overhead_ms": 30.0,
                "ttft_stage_ms": 340.0,
                "total_stage_ms": 1090.0,
                "share_of_e2e_pct": {"decode_time": 68.8},
                "share_of_ttft_pct": {"prefill_time": 55.9},
            },
            "mean": {
                "available": True,
                "epp_scheduling_ms": 5.0,
                "queue_time_ms": 30.0,
                "kv_transfer_ms": 20.0,
                "prefill_time_ms": 120.0,
                "decode_time_ms": 510.0,
                "engine_overhead_ms": 14.0,
                "gateway_overhead_ms": 11.0,
                "ttft_stage_ms": 200.0,
                "total_stage_ms": 710.0,
                "share_of_e2e_pct": {"decode_time": 71.8},
                "share_of_ttft_pct": {"prefill_time": 60.0},
            },
            "series": [
                {
                    "t": 0.0,
                    "epp_sched_ms": 4.0,
                    "queue_ms": 25.0,
                    "kv_transfer_ms": 18.0,
                    "prefill_ms": 110.0,
                    "decode_ms": 500.0,
                },
                {
                    "t": 5.0,
                    "epp_sched_ms": 4.0,
                    "queue_ms": 28.0,
                    "kv_transfer_ms": 20.0,
                    "prefill_ms": 115.0,
                    "decode_ms": 510.0,
                },
            ],
        },
        "ttft_decomposition": {
            "available": True,
            "source": "server_telemetry",
            "p50": {
                "prefill_ms": 110.0,
                "queue_ms": 25.0,
                "kv_transfer_ms": 18.0,
                "epp_scheduling_ms": 4.0,
                "engine_overhead_ms": 12.0,
                "gateway_overhead_ms": 11.0,
            },
            "p90": {
                "prefill_ms": 190.0,
                "queue_ms": 45.0,
                "kv_transfer_ms": 32.0,
                "epp_scheduling_ms": 8.0,
                "engine_overhead_ms": 35.0,
                "gateway_overhead_ms": 30.0,
            },
            "mean": {
                "prefill_ms": 120.0,
                "queue_ms": 30.0,
                "kv_transfer_ms": 20.0,
                "epp_scheduling_ms": 5.0,
                "engine_overhead_ms": 14.0,
                "gateway_overhead_ms": 11.0,
            },
        },
    }
    html_pd = render_html_report(t_pd, client_series=cs_pd, server_telemetry=st_pd, forensics=forensics_pd)
    assert "<!-- lens-report-v42 -->" in html_pd
    emb_pd = _extract_embedded_report_json(html_pd)

    # 2. Legacy pooled P/D with NIXL
    t_pooled_nixl = _make_task("00000026", name="Pooled PD With NIXL")
    st_pooled_nixl = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "metric_sources": {"vllm_stage_latencies": "pooled_namespace_legacy"},
        "step_seconds": 5,
        "pods": ["prefill-0", "decode-0"],
        "per_pod": [
            {"pod": "prefill-0", "role": "prefill", "request_rate_rps": {"mean": 5.0}},
            {"pod": "decode-0", "role": "decode", "request_rate_rps": {"mean": 5.0}},
        ],
        "router": {"scheduler_latency_p50_ms": {"mean": 5.0}, "scheduler_latency_p90_ms": {"mean": 9.0}},
        "summary": {
            "kv_cache_usage_percent": {"mean": 30.0, "max": 45.0},
            "vllm_ttft_p50_ms": {"mean": 999.0},
            "vllm_e2e_p50_ms": {"mean": 999.0},
            "vllm_tpot_p50_ms": {"mean": 9.5},
            "nixl_transfer_latency_p50_ms": {"mean": 16.0},
            "nixl_transfer_latency_p90_ms": {"mean": 28.0},
        },
        "series": [
            {
                "t": 0.0,
                "kv_cache_pct": 30.0,
                "router_scheduler_latency_p50_ms": 5.0,
                "nixl_transfer_latency_p50_ms": 16.0,
                "vllm_ttft_p50_ms": 999.0,
                "vllm_tpot_p50_ms": 9.5,
            },
        ],
    }
    html_pooled_nixl = render_html_report(
        t_pooled_nixl,
        client_series=cs_pd,
        server_telemetry=st_pooled_nixl,
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    emb_pooled_nixl = _extract_embedded_report_json(html_pooled_nixl)

    # 3. Legacy pooled P/D without NIXL
    t_pooled_no_nixl = _make_task("00000027", name="Pooled PD No NIXL")
    st_pooled_no_nixl = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "metric_sources": {"vllm_stage_latencies": "pooled_namespace_legacy"},
        "step_seconds": 5,
        "pods": ["prefill-0", "decode-0"],
        "per_pod": [
            {"pod": "prefill-0", "role": "prefill", "request_rate_rps": {"mean": 5.0}},
            {"pod": "decode-0", "role": "decode", "request_rate_rps": {"mean": 5.0}},
        ],
        "router": {"scheduler_latency_p50_ms": {"mean": 5.0}},
        "summary": {
            "kv_cache_usage_percent": {"mean": 30.0, "max": 45.0},
            "vllm_ttft_p50_ms": {"mean": 999.0},
            "vllm_e2e_p50_ms": {"mean": 999.0},
            "vllm_tpot_p50_ms": {"mean": 9.5},
        },
        "series": [
            {
                "t": 0.0,
                "kv_cache_pct": 30.0,
                "router_scheduler_latency_p50_ms": 5.0,
                "vllm_ttft_p50_ms": 999.0,
                "vllm_tpot_p50_ms": 9.5,
            },
        ],
    }
    html_pooled_no_nixl = render_html_report(
        t_pooled_no_nixl,
        client_series=cs_pd,
        server_telemetry=st_pooled_no_nixl,
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    emb_pooled_no_nixl = _extract_embedded_report_json(html_pooled_no_nixl)

    # 4. client_fallback payload
    t_client = _make_task("00000028", name="Client Fallback")
    html_client = render_html_report(
        t_client,
        client_series=cs_pd,
        server_telemetry={"available": False, "status": "unavailable"},
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    emb_client = _extract_embedded_report_json(html_client)

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  const node = {
    nodeType: tag === "#text" ? 3 : 1,
    tagName: String(tag).toUpperCase(),
    attributes: {},
    children: [],
    style: {},
    className: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    innerText: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
  return node;
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": (() => { const s = makeNode("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const doc = {
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: (t) => makeNode(t),
    createElementNS: (_ns, t) => makeNode(t),
    createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || makeNode("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder,
    Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout,
    clearTimeout,
    console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"] };
}

(async () => {
  // 1. Fresh role-separated P/D
  const { root: rPd } = await runCase(cases.pd);
  const bPagePd = rPd.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const bDecomp = findAll(bPagePd, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  assert(bDecomp, `Missing #b-ttft-decomp. Root text: ${nodeText(rPd).slice(0, 300)}`);
  const decompTxt = nodeText(bDecomp);
  assert(decompTxt.includes("TTFT MEAN"), `Expected TTFT MEAN slice in #b-ttft-decomp, got: ${decompTxt.slice(0, 400)}`);
  assert(decompTxt.includes("KV Transfer (NIXL)"), "Expected KV Transfer (NIXL) in #b-ttft-decomp");
  assert(decompTxt.includes("Engine / API Overhead"), "Expected granular Engine / API Overhead pill");
  assert(decompTxt.includes("Client / Gateway / Network"), "Expected granular Client / Gateway / Network pill");
  const decompLegends = findAll(bDecomp, (n) => hasClass(n, "legend-item"));
  assert(decompLegends.length <= 6, `Expected <=6 TTFT bar legend series when unsplit, got ${decompLegends.length}`);

  const sPagePd = rPd.children.find((n) => n.getAttribute("data-page") === "serving");
  const sLatPd = findAll(sPagePd, (n) => n.getAttribute("id") === "s-latency")[0];
  assert(sLatPd, "Missing #s-latency");
  const sStageCardPd = findAll(sLatPd, (n) => n.tagName === "DIV" && (n.children || []).some((ch) => hasClass(ch, "chart-head") && nodeText(ch).includes("Request Stage Latency Breakdown")))[0];
  assert(sStageCardPd, "Missing Request Stage Latency Breakdown in #s-latency");
  const sStageLegendsPd = findAll(sStageCardPd, (n) => hasClass(n, "legend-item"));
  assert(sStageLegendsPd.length <= 5, `Expected <=5 area layers in #s-latency, got ${sStageLegendsPd.length}`);

  // 2. Legacy pooled P/D with NIXL
  const { root: rPoolNixl } = await runCase(cases.pooledNixl);
  const sPagePoolNixl = rPoolNixl.children.find((n) => n.getAttribute("data-page") === "serving");
  const sLatPoolNixl = findAll(sPagePoolNixl, (n) => n.getAttribute("id") === "s-latency")[0];
  const kpisPoolNixl = findAll(sLatPoolNixl, (n) => hasClass(n, "kpi"));
  const ttftKpi = kpisPoolNixl.find((k) => nodeText(k).includes("Server TTFT (P50)"));
  const e2eKpi = kpisPoolNixl.find((k) => nodeText(k).includes("Server E2E (P50)"));
  const stageKpi = kpisPoolNixl.find((k) => nodeText(k).includes("Stage P50 (KV Transfer / P/D TTFT / Decode Est.)"));
  assert(ttftKpi && nodeText(ttftKpi).includes("—"), "Legacy pooled P/D Server TTFT (P50) must show —");
  assert(e2eKpi && nodeText(e2eKpi).includes("—"), "Legacy pooled P/D Server E2E (P50) must show —");
  assert(stageKpi, "Legacy pooled P/D must switch Stage P50 KPI title");
  assert(nodeText(sLatPoolNixl).includes("Legacy P/D server telemetry pooled Prefill and Decode histograms"), "Missing legacy pooled P/D caveat banner");
  assert(nodeText(sLatPoolNixl).includes("KV Transfer (NIXL)"), "Expected KV Transfer (NIXL) in pooled P/D with NIXL");

  // 3. Legacy pooled P/D without NIXL
  const { root: rPoolNoNixl } = await runCase(cases.pooledNoNixl);
  const sPagePoolNoNixl = rPoolNoNixl.children.find((n) => n.getAttribute("data-page") === "serving");
  const sLatPoolNoNixl = findAll(sPagePoolNoNixl, (n) => n.getAttribute("id") === "s-latency")[0];
  assert(!nodeText(sLatPoolNoNixl).includes("KV Transfer (NIXL)"), "Pooled P/D without NIXL must not render KV Transfer (NIXL) layer");

  // 4. client_fallback KPI guard: Stage P50 must NOT leak client-estimated Decode
  const { root: rClient } = await runCase(cases.clientFallback);
  const sPageClient = rClient.children.find((n) => n.getAttribute("data-page") === "serving");
  const sLatClient = findAll(sPageClient, (n) => n.getAttribute("id") === "s-latency")[0];
  const kpisClient = findAll(sLatClient, (n) => hasClass(n, "kpi"));
  const stageKpiClient = kpisClient.find((k) => nodeText(k).includes("Stage P50 (Queue / Prefill / Decode)"));
  assert(stageKpiClient, "Missing Stage P50 KPI in client_fallback");
  const valNode = findAll(stageKpiClient, (n) => hasClass(n, "kpi-val"))[0];
  assert(valNode && nodeText(valNode) === "N/A / N/A / N/A", `client_fallback Stage P50 must not leak Decode, got ${valNode && nodeText(valNode)}`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(
            {
                "pd": emb_pd,
                "pooledNixl": emb_pooled_nixl,
                "pooledNoNixl": emb_pooled_no_nixl,
                "clientFallback": emb_client,
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v25 P/D stage latency & TTFT decomp check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v26_swimlane_height_parity(sim_env: Path) -> None:
    """v26 offline HTML report: SwimlaneCanvas matches sibling .chart-box height at low concurrency and caps scroll at 360px."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    task = _make_task("00000026", name="Swimlane Height Parity")
    cs = _sample_client_series()
    cs["warmup_seconds"] = 0.0
    cs["duration_seconds"] = 40.0
    cs["slot_lanes"] = {
        "peak_slots": 8,
        "total_requests": 8,
        "session_count": 8,
        "sample_stride": 1,
        "tier_counts": {"cold": 4, "partial": 2, "warm": 2},
        "items": [
            {
                "slot": idx,
                "start_s": float(idx),
                "end_s": float(idx + 5),
                "cache_hit_pct": 80.0 if idx % 3 == 0 else (30.0 if idx % 3 == 1 else 0.0),
                "tier": "warm" if idx % 3 == 0 else ("partial" if idx % 3 == 1 else "cold"),
                "session_id": f"sess-{idx}",
                "turn": 1,
                "input_tokens": 128,
                "output_tokens": 32,
                "ttft_ms": 25.0,
            }
            for idx in range(8)
        ],
    }
    html_text = render_html_report(
        task,
        forensics={"verdict": {"code": "healthy", "title": "Healthy"}, "findings": []},
        client_series=cs,
    )
    assert "<!-- lens-report-v42 -->" in html_text
    embedded = _extract_embedded_report_json(html_text)

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const src = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\nglobalThis.__testExports = { SwimlaneCanvas };\n})();\n"
);

function makeNode(tag) {
  const attrs = {};
  return {
    nodeType: tag === "#text" ? 3 : 1,
    tagName: String(tag).toUpperCase(),
    attributes: attrs,
    children: [],
    className: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    innerText: "",
    style: {},
    parentNode: null,
    clientWidth: 720,
    listeners: {},
    setAttribute(k, v) {
      attrs[k] = String(v);
      if (k === "id") this.id = String(v);
      if (k === "class" || k === "className") this.className = String(v);
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class") return this.className || attrs[k] || null;
      return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          this.children.push(t);
        } else {
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    remove() {},
    scrollIntoView() {},
    getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 720, height: 240, right: 720, bottom: 240 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").trim();
}

const ids = {
  "chart-tooltip": makeNode("div"),
  "report-root": makeNode("main"),
  "report-title": makeNode("span"),
  "report-meta": makeNode("div"),
  "status-badges": makeNode("span"),
  "lens-report-data": (() => { const s = makeNode("script"); s.textContent = JSON.stringify(payload); return s; })(),
  "sidebar-toggle": makeNode("button"),
  "sidebar": makeNode("aside"),
  "theme-toggle": makeNode("button"),
  "print-btn": makeNode("button"),
  "window-toggle": makeNode("div"),
  "nav-list": makeNode("nav"),
};
const doc = {
  documentElement: makeNode("html"),
  body: makeNode("body"),
  createElement: (t) => makeNode(t),
  createElementNS: (_ns, t) => makeNode(t),
  createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
  getElementById: (id) => ids[id] || makeNode("div"),
  querySelectorAll: () => [],
};
const context = {
  document: doc,
  window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
  navigator: {},
  TextDecoder, Uint8Array,
  atob: (s) => Buffer.from(s, "base64").toString("binary"),
  URL, Blob, Response,
  DecompressionStream: globalThis.DecompressionStream,
  ResizeObserver: undefined,
  requestAnimationFrame: (fn) => fn(),
  setTimeout, clearTimeout, console,
  Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
  isFinite, parseFloat, parseInt,
};
context.window.document = doc;

(async () => {
  vm.runInNewContext(src, context);
  await new Promise((r) => setTimeout(r, 20));
  const { SwimlaneCanvas } = context.__testExports;

  // 1. Built page check (#r-lanes with 8 slots)
  const reqPage = ids["report-root"].children.find((n) => n.getAttribute("data-page") === "requests");
  assert(reqPage, `Missing requests page. Root text: ${nodeText(ids["report-root"])}`);
  const lanesSec = findAll(reqPage, (n) => n.getAttribute("id") === "r-lanes")[0];
  assert(lanesSec, `Missing #r-lanes section. Req text: ${nodeText(reqPage).slice(0, 300)}`);
  const builtSvg = findAll(lanesSec, (n) => n.tagName === "SVG")[0];
  assert(builtSvg, "Missing Swimlane SVG in #r-lanes");
  const builtH = Number(builtSvg.getAttribute("height"));
  assert(builtH >= 220 && builtH <= 260, `8-slot built SVG height must match .chart-box [220, 260], got ${builtH}`);

  // 2. Low concurrency (peakSlots = 1 and 8, with & without warmup)
  for (const [slots, warmup] of [[1, 0], [1, 5], [8, 0], [8, 5]]) {
    const host = makeNode("div");
    const items = Array.from({ length: slots }, (_, idx) => ({
      slot: idx,
      start_s: idx * 0.5,
      end_s: idx * 0.5 + 3.0,
      cache_hit_pct: 80,
      tier: "warm",
    }));
    SwimlaneCanvas(host, { duration_seconds: 30, slot_lanes: { peak_slots: slots, total_requests: slots, session_count: slots, sample_stride: 1, items } }, null, warmup);
    assert.strictEqual(host.children.length, 2, "Expected topStrip + svgWrap");
    const svgWrap = host.children[1];
    const wrapStyle = svgWrap.getAttribute("style") || "";
    assert(!wrapStyle.includes("max-height") && !wrapStyle.includes("overflow-y"),
      `Low concurrency (${slots} slots) wrapper must not scroll, got style="${wrapStyle}"`);
    const svg = svgWrap.children[0];
    const svgH = Number(svg.getAttribute("height"));
    assert(svgH >= 220 && svgH <= 260,
      `Low concurrency (${slots} slots, warmup=${warmup}) SVG height must be in [220, 260], got ${svgH}`);

    const barRects = svg.children.filter((c) => c.tagName === "RECT" && c.getAttribute("rx") != null);
    assert.strictEqual(barRects.length, slots, `Expected ${slots} slot bar rects`);
    const padTop = warmup > 0 ? 20 : 10;
    const padBot = 24;
    const ih = svgH - padTop - padBot;
    const rowH = ih / slots;
    for (let s = 0; s < slots; s++) {
      const r = barRects[s];
      const barH = Number(r.getAttribute("height"));
      const barY = Number(r.getAttribute("y"));
      assert(barH > 0 && barH <= 20, `Slot bar height must be capped <= 20, got ${barH}`);
      const expectedCenter = padTop + s * rowH + rowH / 2;
      const actualCenter = barY + barH / 2;
      assert(Math.abs(actualCenter - expectedCenter) < 0.15,
        `Slot ${s} bar must be vertically centered: actual=${actualCenter}, expected=${expectedCenter}`);
    }
  }

  // 3. High concurrency (peakSlots = 60) -> SVG height > 360 and wrapper caps scroll at 360px
  const hiHost = makeNode("div");
  const hiItems = Array.from({ length: 60 }, (_, idx) => ({
    slot: idx,
    start_s: 0,
    end_s: 5,
    cache_hit_pct: 0,
    tier: "cold",
  }));
  SwimlaneCanvas(hiHost, { duration_seconds: 20, slot_lanes: { peak_slots: 60, total_requests: 60, session_count: 60, sample_stride: 1, items: hiItems } }, null, 0);
  const hiWrap = hiHost.children[1];
  const hiWrapStyle = hiWrap.getAttribute("style") || "";
  assert.strictEqual(hiWrapStyle, "max-height:360px;overflow-y:auto;",
    `High concurrency wrapper must cap scroll at 360px, got "${hiWrapStyle}"`);
  const hiSvg = hiWrap.children[0];
  const hiH = Number(hiSvg.getAttribute("height"));
  assert(hiH > 360, `High concurrency (60 slots) SVG height must exceed 360, got ${hiH}`);

  // 4. Y-axis slot label separation >= 12px across moderate/high slot counts (e.g. 20, 26, 32, 60)
  for (const slots of [20, 26, 32, 60]) {
    const lblHost = makeNode("div");
    const lblItems = Array.from({ length: slots }, (_, idx) => ({
      slot: idx,
      start_s: 0,
      end_s: 2,
      cache_hit_pct: 50,
      tier: "partial",
    }));
    SwimlaneCanvas(lblHost, { duration_seconds: 20, slot_lanes: { peak_slots: slots, total_requests: slots, session_count: slots, sample_stride: 1, items: lblItems } }, null, 0);
    const lblSvg = lblHost.children[1].children[0];
    const slotYs = lblSvg.children
      .filter((c) => c.tagName === "TEXT" && /^S\d+$/.test(nodeText(c)))
      .map((c) => Number(c.getAttribute("y")))
      .sort((a, b) => a - b);
    assert(slotYs.length >= 2, `Expected >=2 Y-axis slot labels for ${slots} slots`);
    for (let i = 1; i < slotYs.length; i++) {
      assert(slotYs[i] - slotYs[i - 1] >= 12,
        `Adjacent Y-axis slot labels for ${slots} slots must be >=12px apart, got ${slotYs[i] - slotYs[i - 1]}`);
    }
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v26 SwimlaneCanvas height parity check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_historical_trace_replayer_bundle_and_stale_refresh_succeeds(sim_env: Path) -> None:
    from llm_d_lens.simulation.reporting.builder import _refresh_stale_report_for_bundle, build_bundle_tar_gz
    from llm_d_lens.simulation.telemetry_post import CLIENT_SERIES_SCHEMA_VERSION, FORENSICS_SCHEMA_VERSION

    task = _make_task("0000003f", status="completed", backend="trace-replayer")
    raw_task_dir = Path(task.task_dir)
    report_dir = raw_task_dir / "artifacts" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    cs = _sample_client_series()
    cs["schema_version"] = CLIENT_SERIES_SCHEMA_VERSION
    (report_dir / "client-series.json").write_text(json.dumps(cs), encoding="utf-8")
    (report_dir / "forensics.json").write_text(
        json.dumps({"schema_version": 1, "verdict": {"code": "healthy"}}),
        encoding="utf-8",
    )
    (report_dir / "index.html").write_text("<html>stale</html>", encoding="utf-8")

    _refresh_stale_report_for_bundle(task, raw_task_dir, report_dir, simulation_service.task_root())
    refreshed_forensics = json.loads((report_dir / "forensics.json").read_text(encoding="utf-8"))
    assert refreshed_forensics["schema_version"] == FORENSICS_SCHEMA_VERSION
    assert "<!-- lens-report-v42 -->" in (report_dir / "index.html").read_text(encoding="utf-8")

    bundle = build_bundle_tar_gz(task)
    assert bundle.is_file()


def test_html_report_v27_offload_multi_tier_usage_and_split_charts(sim_env: Path) -> None:
    """v27 offline HTML report: #s-offload shows VRAM/CPU/NVMe tier usage KPIs and separates % tier usage from MiB/s throughput."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    required_keys = (
        "s-offload",
        "VRAM KV Cache Usage",
        "CPU KV Cache Usage",
        "NVMe KV Cache Usage",
        "KV Cache Usage by Tier",
        "KV Offload & Restore Throughput",
        "KV Offload, Restore & NIXL Transfer Throughput",
    )
    for block_name in ("HELP_TEXTS", "HELP_FORMULAS"):
        m = re.search(rf"const {block_name} = \{{(?P<body>.*?)\n  \}};", charts_js, re.DOTALL)
        assert m is not None, f"Could not locate {block_name} in report_charts.js"
        body = m.group("body")
        for key in required_keys:
            entry_m = re.search(rf'"{re.escape(key)}":\s*"([^"]*)"', body)
            assert entry_m is not None, f"Missing {block_name} entry for {key!r}"
            assert not re.search(r"\b(vllm|sglang|trtllm)\b", entry_m.group(1), re.IGNORECASE), (
                f"{block_name}[{key!r}] must be engine-agnostic: {entry_m.group(1)!r}"
            )
            assert "right axis" not in entry_m.group(1).lower(), (
                f"{block_name}[{key!r}] must not reference a dual-axis right axis: {entry_m.group(1)!r}"
            )

    # Case A: Aggregated topology with all 3 usage tiers and omitted summary stats (tests srvPts fallback)
    task_agg = _make_task("00000040", name="Aggregated Multi-Tier Offload")
    srv_agg = {
        "available": True,
        "summary": {
            "kv_offload_bytes_per_second": {"mean": 10 * 1024**2, "max": 20 * 1024**2},
            "kv_restore_bytes_per_second": {"mean": 5 * 1024**2, "max": 8 * 1024**2},
            "kv_offload_lookup_latency_p95_ms": {"mean": 1.5, "max": 2.0},
        },
        "series": [
            {
                "t": 0.0,
                "kv_cache_usage_percent": 60.0,
                "cpu_cache_usage_percent": 30.0,
                "disk_cache_usage_percent": 10.0,
                "kv_offload_bytes_per_second": 10 * 1024**2,
                "kv_restore_bytes_per_second": 4 * 1024**2,
            },
            {
                "t": 10.0,
                "kv_cache_usage_percent": 80.0,
                "cpu_cache_usage_percent": 50.0,
                "disk_cache_usage_percent": 20.0,
                "kv_offload_bytes_per_second": 20 * 1024**2,
                "kv_restore_bytes_per_second": 8 * 1024**2,
            },
        ],
    }
    html_agg = render_html_report(
        task_agg,
        forensics={"verdict": {"code": "healthy", "title": "Healthy"}, "findings": []},
        client_series=_sample_client_series(),
        server_telemetry=srv_agg,
    )
    assert "<!-- lens-report-v42 -->" in html_agg

    # Case B: Disaggregated / NIXL topology with missing CPU & NVMe tiers
    task_nixl = _make_task("00000041", name="Disagg NIXL Offload")
    srv_nixl = {
        "available": True,
        "topology": "disaggregated",
        "summary": {
            "kv_cache_usage_percent": {"mean": 45.0, "max": 55.0},
            "nixl_transfer_bytes_per_second": {"mean": 128 * 1024**2, "max": 200 * 1024**2},
            "nixl_transfer_latency_p95_ms": {"mean": 12.5},
            "nixl_num_failed_transfers": 0,
        },
        "series": [
            {
                "t": 0.0,
                "kv_cache_usage_percent": 40.0,
                "kv_offload_bytes_per_second": 2 * 1024**2,
                "nixl_transfer_bytes_per_second": 128 * 1024**2,
            },
            {
                "t": 10.0,
                "kv_cache_usage_percent": 50.0,
                "kv_offload_bytes_per_second": 4 * 1024**2,
                "nixl_transfer_bytes_per_second": 200 * 1024**2,
            },
        ],
    }
    html_nixl = render_html_report(
        task_nixl,
        forensics={"verdict": {"code": "healthy", "title": "Healthy"}, "findings": []},
        client_series=_sample_client_series(),
        server_telemetry=srv_nixl,
    )
    assert "<!-- lens-report-v42 -->" in html_nixl

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const src = fs.readFileSync(process.argv[1], "utf8");

function makeNode(tag) {
  const attrs = {};
  return {
    nodeType: tag === "#text" ? 3 : 1,
    tagName: String(tag).toUpperCase(),
    attributes: attrs,
    children: [],
    className: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    innerText: "",
    style: {},
    parentNode: null,
    clientWidth: 720,
    listeners: {},
    setAttribute(k, v) {
      attrs[k] = String(v);
      if (k === "id") this.id = String(v);
      if (k === "class" || k === "className") this.className = String(v);
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class") return this.className || attrs[k] || null;
      return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          this.children.push(t);
        } else {
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    remove() {},
    scrollIntoView() {},
    getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 720, height: 220, right: 720, bottom: 220 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  if (node.tagName === "#TEXT") return node.textContent || "";
  if (node.children && node.children.length) return node.children.map(nodeText).join("");
  return node.textContent || "";
}

async function runPayload(payload) {
  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": (() => { const s = makeNode("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const doc = {
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: (t) => makeNode(t),
    createElementNS: (_ns, t) => makeNode(t),
    createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || makeNode("div"),
    querySelectorAll: () => [],
  };
  const context = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  context.window.document = doc;
  vm.runInNewContext(src, context);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tip: ids["chart-tooltip"] };
}

(async () => {
// Case A assertions
const agg = await runPayload(cases.agg);
const aggOffload = findAll(agg.root, (n) => n.getAttribute && n.getAttribute("id") === "s-offload")[0];
assert(aggOffload, "Missing #s-offload in aggregated report");
const aggText = nodeText(aggOffload);
assert(aggText.includes("KV Offload · Multi-Tier Usage & Offload"), `Unexpected section title: ${aggText}`);
assert(aggText.includes("P/D NIXL: N/A (Aggregated)"), "Missing aggregated NIXL badge");
assert(!aggText.includes("NaN"), `Unexpected NaN in #s-offload: ${aggText}`);

const aggKpis = findAll(aggOffload, (n) => n.className === "kpi");
const kpiMap = Object.fromEntries(aggKpis.map((k) => {
  const lbl = nodeText(findAll(k, (c) => c.className === "kpi-label")[0]).replace(/\?$/, "");
  const val = nodeText(findAll(k, (c) => c.className === "kpi-val")[0]);
  const sub = nodeText(findAll(k, (c) => c.className === "kpi-sub")[0]);
  return [lbl, { val, sub }];
}));
assert.strictEqual(kpiMap["VRAM KV Cache Usage"].val, "70%");
assert.strictEqual(kpiMap["VRAM KV Cache Usage"].sub, "Peak: 80%");
assert.strictEqual(kpiMap["CPU KV Cache Usage"].val, "40%");
assert.strictEqual(kpiMap["CPU KV Cache Usage"].sub, "Peak: 50%");
assert.strictEqual(kpiMap["NVMe KV Cache Usage"].val, "15%");
assert.strictEqual(kpiMap["NVMe KV Cache Usage"].sub, "Peak: 20%");

const aggGrid2 = findAll(aggOffload, (n) => n.className === "grid-2")[0];
assert(aggGrid2 && aggGrid2.children.length === 2, "Expected 2 side-by-side chart cards inside .grid-2");
const [usageCard, thrCard] = aggGrid2.children;
assert(nodeText(usageCard).includes("KV Cache Usage by Tier"), "First chart must be KV Cache Usage by Tier");
assert(nodeText(thrCard).includes("KV Offload & Restore Throughput"), "Second chart must be KV Offload & Restore Throughput");

const usageLegends = findAll(usageCard, (n) => n.className === "legend-item").map(nodeText);
assert.deepStrictEqual(usageLegends, [
  "VRAM KV Cache Usage (%)",
  "CPU KV Cache Usage (%)",
  "NVMe KV Cache Usage (%)",
]);
const thrLegends = findAll(thrCard, (n) => n.className === "legend-item").map(nodeText);
assert.deepStrictEqual(thrLegends, [
  "KV Offload (MiB/s)",
  "KV Restore (MiB/s)",
]);
assert(!thrLegends.includes("CPU KV Cache Usage (%)"), "Throughput chart must NOT include CPU KV Cache Usage (%)");

for (const card of [usageCard, thrCard]) {
  const rightAxisTexts = findAll(card, (n) => n.tagName === "TEXT" && n.getAttribute("text-anchor") === "start");
  assert.strictEqual(rightAxisTexts.length, 0, "Separated charts must remain strictly single-axis (no right Y-axis)");
}

// Verify tooltip formatting for both charts
const usageOverlay = findAll(usageCard, (n) => n.tagName === "RECT" && n.listeners && n.listeners.mousemove)[0];
usageOverlay.dispatchEvent("mousemove", { clientX: 360, clientY: 100 });
assert(agg.tip.textContent.includes("VRAM KV Cache Usage (%): ") && agg.tip.textContent.includes("%"),
  `Usage tooltip must format with %, got: ${agg.tip.textContent}`);
assert(!agg.tip.textContent.includes("MiB/s"), `Usage tooltip must not mix MiB/s: ${agg.tip.textContent}`);

const thrOverlay = findAll(thrCard, (n) => n.tagName === "RECT" && n.listeners && n.listeners.mousemove)[0];
thrOverlay.dispatchEvent("mousemove", { clientX: 360, clientY: 100 });
assert(agg.tip.textContent.includes("KV Offload (MiB/s): ") && agg.tip.textContent.includes("MiB/s"),
  `Throughput tooltip must format with MiB/s, got: ${agg.tip.textContent}`);
assert(!agg.tip.textContent.includes("CPU KV Cache Usage"), `Throughput tooltip must not mix CPU KV Cache Usage: ${agg.tip.textContent}`);

// Case B assertions: NIXL topology with missing CPU & NVMe tiers
const nixl = await runPayload(cases.nixl);
const nixlOffload = findAll(nixl.root, (n) => n.getAttribute && n.getAttribute("id") === "s-offload")[0];
assert(nixlOffload, "Missing #s-offload in NIXL report");
const nixlText = nodeText(nixlOffload);
assert(nixlText.includes("KV Offload · Multi-Tier Usage & NIXL Transfer"), `Unexpected NIXL section title: ${nixlText}`);
assert(!nixlText.includes("NaN"), `Unexpected NaN in NIXL #s-offload: ${nixlText}`);

const nixlKpiMap = Object.fromEntries(findAll(nixlOffload, (n) => n.className === "kpi").map((k) => {
  const lbl = nodeText(findAll(k, (c) => c.className === "kpi-label")[0]).replace(/\?$/, "");
  const val = nodeText(findAll(k, (c) => c.className === "kpi-val")[0]);
  const sub = nodeText(findAll(k, (c) => c.className === "kpi-sub")[0]);
  return [lbl, { val, sub }];
}));
assert.strictEqual(nixlKpiMap["VRAM KV Cache Usage"].val, "45%");
assert.strictEqual(nixlKpiMap["CPU KV Cache Usage"].val, "Not enabled");
assert.strictEqual(nixlKpiMap["CPU KV Cache Usage"].sub, "Peak: N/A");
assert.strictEqual(nixlKpiMap["NVMe KV Cache Usage"].val, "Not enabled");
assert.strictEqual(nixlKpiMap["NVMe KV Cache Usage"].sub, "Peak: N/A");

const nixlGrid2 = findAll(nixlOffload, (n) => n.className === "grid-2")[0];
assert(nixlGrid2 && nixlGrid2.children.length === 2, "Expected 2 side-by-side chart cards in NIXL #s-offload");
const [nixlUsageCard, nixlThrCard] = nixlGrid2.children;
assert.deepStrictEqual(
  findAll(nixlUsageCard, (n) => n.className === "legend-item").map(nodeText),
  ["VRAM KV Cache Usage (%)"]
);
assert(nodeText(nixlThrCard).includes("KV Offload, Restore & NIXL Transfer Throughput"));
assert.deepStrictEqual(
  findAll(nixlThrCard, (n) => n.className === "legend-item").map(nodeText),
  ["KV Offload (MiB/s)", "NIXL KV Transfer (MiB/s)"]
);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(
            {
                "agg": _extract_embedded_report_json(html_agg),
                "nixl": _extract_embedded_report_json(html_nixl),
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v27 #s-offload multi-tier check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_v28_summary_csv_tpot_unit_pd_peak_kv_and_template_css(sim_env: Path) -> None:
    """Verify B13c summary.csv TPOT ms/tok unit, P/D peak KV max, and v28 template CSS classes."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    for css_token in (".badge.violet", ".finding-evidence-row", ".finding-evidence-chip", ".turn-analysis-card"):
        assert css_token in template_html

    client_series = _sample_client_series()
    pd_server = {
        "topology_mode": "disaggregated",
        "summary": {
            "kv_cache_usage_percent": {"mean": 28.2, "max": 28.2},
            "prefix_cache_hit_percent": {"mean": 46.18},
        },
        "summary_by_role": {
            "prefill": {"kv_cache_usage_percent": {"mean": 2.3, "max": 3.1}},
            "decode": {
                "kv_cache_usage_percent": {"mean": 46.5, "max": 96.4},
                "kv_cache_usage_max_percent": {"max": 97.5},
            },
        },
        "per_pod": [
            {"pod": "p-0", "role": "prefill", "kv_cache_usage_percent": {"mean": 2.3, "max": 3.1}},
            {"pod": "d-0", "role": "decode", "kv_cache_usage_percent": {"mean": 46.5, "max": 97.5}},
            {"pod": "epp-0", "role": "epp", "kv_cache_usage_percent": {"mean": 100.0, "max": 100.0}},
        ],
    }
    csv_text = report_builder.render_summary_csv(
        _make_task("00000087"),
        client_series=client_series,
        server_telemetry=pd_server,
        forensics=None,
    )
    rows = list(csv.reader(io.StringIO(csv_text)))
    by_metric = {(r[0], r[2]): (r[3], float(r[4])) for r in rows[1:]}
    assert by_metric[("profiling", "tpot_mean_ms")][0] == "ms/tok"
    assert by_metric[("profiling", "tpot_p50_ms")][0] == "ms/tok"
    assert by_metric[("profiling", "tpot_p95_ms")][0] == "ms/tok"
    assert by_metric[("profiling", "tpot_p99_ms")][0] == "ms/tok"
    assert by_metric[("profiling", "ttft_mean_ms")][0] == "ms"
    assert by_metric[("profiling", "e2e_mean_ms")][0] == "ms"
    assert by_metric[("profiling", "peak_kv_cache_pct")] == ("%", 97.5)


def test_v28_forensics_pd_routing_vs_cache_prefill_scope(sim_env: Path) -> None:
    """Verify A3 forensics.py heals routing_vs_cache to Prefill scope on historical and canonical P/D runs."""
    from llm_d_lens.simulation.forensics import analyze_forensics

    task = _make_task("00000088")
    client_series = _sample_client_series()
    base_series = [
        {
            "timestamp": "2026-09-02T10:00:05Z",
            "kv_cache_usage_percent": 28.0,
            "prefix_cache_hit_percent": 40.7,
            "input_token_rate_tps": 10938.5,
            "engine_prompt_local_cache_tps": 5300.5,
            "engine_prompt_local_compute_tps": 2964.4,
        }
    ]
    # 1. Historical P/D artifact (summary pooled across P+D, prompt_tokens_by_role_source present)
    hist_pd_server = {
        "status": "available",
        "topology_mode": "disaggregated",
        "series": base_series,
        "role_series": [
            {
                "timestamp": "2026-09-02T10:00:05Z",
                "prefill_kv_cache_usage_percent": 2.3,
                "decode_kv_cache_usage_percent": 46.5,
                "prefill_prefix_cache_hit_percent": 46.18,
            }
        ],
        "summary": {
            "kv_cache_usage_percent": {"mean": 28.2, "max": 28.2},
            "prefix_cache_hit_percent": {"mean": 40.7, "max": 40.7},
            "input_token_rate_tps": {"mean": 10938.5, "max": 10938.5},
            "engine_prompt_local_cache_tps": {"mean": 5300.5, "max": 5300.5},
            "engine_prompt_local_compute_tps": {"mean": 2964.4, "max": 2964.4},
            "engine_prompt_recomputed_tps": {"mean": 0.0, "max": 0.0},
        },
        "per_pod": [
            {"pod": "p-0", "role": "prefill", "kv_cache_usage_percent": {"mean": 2.3, "max": 3.0}},
            {"pod": "d-0", "role": "decode", "kv_cache_usage_percent": {"mean": 46.5, "max": 97.5}},
        ],
        "derived": {
            "avoided_prefill_ratio": 46.58,
            "role_summary": {
                "prefill": {
                    "input_token_rate_tps": 5512.5,
                    "prefix_cache_hit_percent": 37.74,
                },
                "decode": {
                    "input_token_rate_tps": 5426.0,
                },
            },
            "prompt_tokens_by_role_source": [
                {
                    "role": "Prefill",
                    "local_cache_tokens": 392673,
                    "external_kv_tokens": 0,
                    "local_compute_tokens": 457688,
                    "total_tokens": 850361,
                },
                {
                    "role": "Decode",
                    "local_cache_tokens": 423386,
                    "external_kv_tokens": 414851,
                    "local_compute_tokens": 0,
                    "total_tokens": 838237,
                },
            ],
        },
    }
    fr_hist = analyze_forensics(task=task, client_series=client_series, server_telemetry=hist_pd_server)
    rvc_hist = fr_hist["routing_vs_cache"]
    assert rvc_hist["role_scope"] == "prefill"
    assert rvc_hist["vllm_actual_hit_pct"] == pytest.approx(392673 / 850361 * 100.0)
    assert rvc_hist["gap_pct"] == pytest.approx(0.40)
    assert rvc_hist["engine_sent_tok_s"] == pytest.approx(5512.5)
    assert rvc_hist["engine_fresh_tok_s"] == pytest.approx(2964.4)
    assert rvc_hist["engine_cached_tok_s"] == pytest.approx(5300.5 * (392673 / (392673 + 423386)))

    # 2. Aggregated run remains untouched (no role_scope)
    agg_server = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": base_series,
        "summary": hist_pd_server["summary"],
        "per_pod": [{"pod": "w-0", "role": "worker", "kv_cache_usage_percent": {"mean": 46.0, "max": 89.0}}],
        "derived": {"avoided_prefill_ratio": 46.58},
    }
    fr_agg = analyze_forensics(task=task, client_series=client_series, server_telemetry=agg_server)
    assert "role_scope" not in fr_agg["routing_vs_cache"]
    assert fr_agg["routing_vs_cache"]["vllm_actual_hit_pct"] == pytest.approx(40.7)
    assert fr_agg["routing_vs_cache"]["engine_sent_tok_s"] == pytest.approx(10938.5)


def test_html_report_v29_granular_ttft_decomp_and_opacity(sim_env: Path) -> None:
    """v29 offline HTML report: granular P/D TTFT bars, role_breakdown fallback, v6 normalization, opacity swatches, and engine-neutral help."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    # 1. Verify all new HELP_TEXTS / HELP_FORMULAS entries exist and contain zero GPU or engine names
    for required_help in (
        "Decode First-Step Compute",
        "Prefill Engine / API Overhead",
        "Decode Handoff / SSE Overhead",
        "Engine / API Overhead",
    ):
        assert f'"{required_help}":' in charts_js, f"Missing HELP_TEXTS/HELP_FORMULAS entry for {required_help}"
    for block_name in ("HELP_TEXTS", "HELP_FORMULAS"):
        m = re.search(rf"const {block_name} = \{{(?P<body>.*?)\n  \}};", charts_js, re.DOTALL)
        assert m is not None
        for line in m.group("body").splitlines():
            if ":" not in line:
                continue
            _, val_part = line.split(":", 1)
            assert not re.search(r"\b(vllm|sglang|trtllm|gpu)\b", val_part, re.IGNORECASE), (
                f"{block_name} value leaked engine/GPU wording: {line!r}"
            )
    assert "TTFT stage breakdown requires serving engine stage histograms." in charts_js
    assert "TTFT stage breakdown requires vLLM stage histograms." not in charts_js

    cs = _sample_client_series()
    st_pd = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "metric_sources": {"vllm_stage_latencies": "role_separated"},
        "step_seconds": 5,
        "pods": ["prefill-0", "decode-0"],
        "per_pod": [
            {"pod": "prefill-0", "role": "prefill", "request_rate_rps": {"mean": 6.0}},
            {"pod": "decode-0", "role": "decode", "request_rate_rps": {"mean": 6.0}},
        ],
        "summary": {"kv_cache_usage_percent": {"mean": 40.0, "max": 55.0}},
        "series": [],
    }

    # Case A: Explicit P/D sub-fields (P50 has gateway_overhead_ms=0, P90 has 115.502 -> canonical legend order + zero-suppressed P50 pills)
    t_explicit = _make_task("00000029", name="Granular PD Explicit")
    f_explicit = {
        "schema_version": 8,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "has_kv_transfer_series": True,
            "p50": {
                "available": True,
                "epp_scheduling_ms": 0.25,
                "queue_time_ms": 0.0,
                "kv_transfer_ms": 393.896,
                "prefill_time_ms": 366.359,
                "decode_first_step_ms": 38.491,
                "prefill_engine_overhead_ms": 131.055,
                "decode_handoff_overhead_ms": 133.883,
                "engine_overhead_ms": 303.429,
                "gateway_overhead_ms": 0.0,
                "decode_time_ms": 5552.356,
                "ttft_stage_ms": 1063.959,
                "total_stage_ms": 6616.315,
            },
            "p90": {
                "available": True,
                "epp_scheduling_ms": 0.446,
                "queue_time_ms": 0.0,
                "kv_transfer_ms": 1777.468,
                "prefill_time_ms": 2143.608,
                "decode_first_step_ms": 270.0,
                "prefill_engine_overhead_ms": 644.258,
                "decode_handoff_overhead_ms": 750.528,
                "engine_overhead_ms": 1664.786,
                "gateway_overhead_ms": 115.502,
                "decode_time_ms": 11349.861,
                "ttft_stage_ms": 5701.829,
                "total_stage_ms": 17051.69,
            },
        },
        "ttft_decomposition": {
            "available": True,
            "source": "server_telemetry",
            "p50": {
                "prefill_ms": 366.359,
                "decode_first_step_ms": 38.491,
                "queue_ms": 0.0,
                "kv_transfer_ms": 393.896,
                "prefill_engine_overhead_ms": 131.055,
                "decode_handoff_overhead_ms": 133.883,
                "epp_scheduling_ms": 0.25,
                "engine_overhead_ms": 303.429,
                "gateway_overhead_ms": 0.0,
            },
            "p90": {
                "prefill_ms": 2143.608,
                "decode_first_step_ms": 270.0,
                "queue_ms": 0.0,
                "kv_transfer_ms": 1777.468,
                "prefill_engine_overhead_ms": 644.258,
                "decode_handoff_overhead_ms": 750.528,
                "epp_scheduling_ms": 0.446,
                "engine_overhead_ms": 1664.786,
                "gateway_overhead_ms": 115.502,
            },
        },
    }
    html_explicit = render_html_report(t_explicit, client_series=cs, server_telemetry=st_pd, forensics=f_explicit)
    assert "<!-- lens-report-v42 -->" in html_explicit
    emb_explicit = _extract_embedded_report_json(html_explicit)

    # Case B: Historical v7/v8 role_breakdown fallback (with 270ms floor bypass on prefill.queue_p90_ms)
    t_hist = _make_task("00000030", name="Historical PD Role Fallback")
    f_hist = {
        "schema_version": 8,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "has_kv_transfer_series": True,
            "role_breakdown": {
                "prefill": {
                    "ttft_p50_ms": 877.253,
                    "queue_p50_ms": 150.0,
                    "prefill_p50_ms": 366.359,
                    "ttft_p90_ms": 2787.885,
                    "queue_p90_ms": 270.0,
                    "prefill_p90_ms": 2143.608,
                    "ttft_mean_ms": 1141.335,
                    "queue_mean_ms": 0.019,
                    "prefill_mean_ms": 680.037,
                },
                "decode": {
                    "ttft_p50_ms": 794.254,
                    "queue_raw_p50_ms": 393.896,
                    "prefill_p50_ms": 150.0,
                    "ttft_p90_ms": 2797.997,
                    "queue_raw_p90_ms": 1777.468,
                    "prefill_p90_ms": 270.0,
                    "ttft_mean_ms": 1040.473,
                    "queue_raw_mean_ms": 614.656,
                    "prefill_mean_ms": 26.966,
                },
            },
            "p50": {
                "available": True,
                "epp_scheduling_ms": 0.25,
                "queue_time_ms": 0.019,
                "kv_transfer_ms": 393.896,
                "prefill_time_ms": 366.359,
                "engine_overhead_ms": 303.429,
                "gateway_overhead_ms": 0.0,
                "decode_time_ms": 5552.356,
                "server_ttft_clamped": False,
            },
            "p90": {
                "available": True,
                "epp_scheduling_ms": 0.446,
                "queue_time_ms": 0.019,
                "kv_transfer_ms": 1777.468,
                "prefill_time_ms": 2143.608,
                "engine_overhead_ms": 1664.786,
                "gateway_overhead_ms": 115.502,
                "decode_time_ms": 11349.861,
                "server_ttft_clamped": False,
            },
        },
        "ttft_decomposition": {
            "available": True,
            "source": "server_telemetry",
            "p50": {
                "prefill_ms": 366.359,
                "queue_ms": 0.019,
                "kv_transfer_ms": 393.896,
                "epp_scheduling_ms": 0.25,
                "engine_overhead_ms": 303.429,
                "gateway_overhead_ms": 0.0,
                "server_ttft_clamped": False,
            },
            "p90": {
                "prefill_ms": 2143.608,
                "queue_ms": 0.019,
                "kv_transfer_ms": 1777.468,
                "epp_scheduling_ms": 0.446,
                "engine_overhead_ms": 1664.786,
                "gateway_overhead_ms": 115.502,
                "server_ttft_clamped": False,
            },
        },
    }
    html_hist = render_html_report(t_hist, client_series=cs, server_telemetry=st_pd, forensics=f_hist)
    emb_hist = _extract_embedded_report_json(html_hist)

    # Case C: v6 slice normalization (P50 unclamped + P90 10s-clamped)
    t_v6 = _make_task("00000031", name="v6 Normalized")
    f_v6 = {
        "schema_version": 6,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "p50": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 1.0,
                "queue_time_ms": 100.0,
                "prefill_time_ms": 400.0,
                "decode_time_ms": 900.0,
                "gateway_overhead_ms": 500.0,
                "server_ttft_ms": 834.5,
                "client_ttft_ms": 1001.0,
                "ttft_stage_ms": 1001.0,
                "total_stage_ms": 1901.0,
                "share_of_e2e_pct": {},
                "share_of_ttft_pct": {},
            },
            "p90": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 2.0,
                "queue_time_ms": 500.0,
                "prefill_time_ms": 900.0,
                "decode_time_ms": 2000.0,
                "gateway_overhead_ms": 12000.0,
                "server_ttft_ms": 9800.0,
                "client_ttft_ms": 13402.0,
                "ttft_stage_ms": 13402.0,
                "total_stage_ms": 15402.0,
                "share_of_e2e_pct": {},
                "share_of_ttft_pct": {},
            },
        },
        "ttft_decomposition": {
            "vllm_prefill_p50_ms": 999.0,
            "vllm_queue_p50_ms": 999.0,
            "overhead_p50_ms": 999.0,
        },
    }
    html_v6 = render_html_report(
        t_v6, client_series=cs, server_telemetry={"available": True, "status": "available"}, forensics=f_v6
    )
    emb_v6 = _extract_embedded_report_json(html_v6)

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    nodeType: tag === "#text" ? 3 : 1,
    tagName: String(tag).toUpperCase(),
    attributes: {},
    children: [],
    style: {},
    className: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    innerText: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const tooltipNode = makeNode("div");
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": (() => { const s = makeNode("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const doc = {
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: (t) => makeNode(t),
    createElementNS: (_ns, t) => makeNode(t),
    createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || makeNode("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder,
    Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout,
    clearTimeout,
    console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tooltip: tooltipNode };
}

(async () => {
  // Case A: Explicit P/D + canonical legend order + opacity + tooltip zero-filter + pill zero-filter
  const { root: rExp, tooltip: tipExp } = await runCase(cases.explicit);
  const bExp = rExp.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const decompExp = findAll(bExp, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  const legendsExp = findAll(decompExp, (n) => hasClass(n, "legend-item")).map((n) => nodeText(n));
  assert.deepStrictEqual(legendsExp, [
    "Prefill Time",
    "Decode First-Step Compute",
    "KV Transfer (NIXL)",
    "Prefill Engine / API Overhead",
    "Decode Handoff / SSE Overhead",
    "EPP Scheduling",
    "Client / Gateway / Network",
  ]);
  const gwSwatch = findAll(decompExp, (n) => hasClass(n, "legend-swatch")).at(-1);
  assert(String(gwSwatch.attributes.style || "").includes("opacity:0.48"), `Expected 0.48 legend opacity, got ${gwSwatch.attributes.style}`);
  const rectOpacities = findAll(decompExp, (n) => n.tagName === "RECT" && n.attributes["fill-opacity"] !== undefined);
  assert(rectOpacities.length > 0, "Expected fill-opacity on semi-transparent bar rect");
  // Summary pills from P50 must omit Queue Time (0) and Client / Gateway / Network (0)
  const pillsExp = findAll(decompExp, (n) => hasClass(n, "stage-share-card")).map((n) => nodeText(n));
  assert(!pillsExp.some((p) => p.includes("Queue Time")), "Zero Queue Time must not appear in pills");
  assert(!pillsExp.some((p) => p.includes("Client / Gateway / Network")), "Zero P50 gateway must not appear in pills");
  assert(pillsExp.some((p) => p.includes("Decode First-Step Compute")));
  assert(pillsExp.some((p) => p.includes("Prefill Engine / API Overhead")));
  assert(pillsExp.some((p) => p.includes("Decode Handoff / SSE Overhead")));
  // Hover first rect (P50 bar): tooltip must exclude Client / Gateway / Network (0 ms on P50) and show dot opacity on P90
  const rectsExp = findAll(decompExp, (n) => n.tagName === "RECT");
  rectsExp[0].dispatchEvent("mousemove", { clientX: 10, clientY: 10 });
  const p50TipText = nodeText(tipExp);
  assert(!p50TipText.includes("Client / Gateway / Network"), `P50 tooltip must omit 0ms segment, got: ${p50TipText}`);
  rectsExp.at(-1).dispatchEvent("mousemove", { clientX: 10, clientY: 10 });
  const p90TipText = nodeText(tipExp);
  assert(p90TipText.includes("Client / Gateway / Network"), `P90 tooltip must include 115.50ms segment, got: ${p90TipText}`);
  const tipDots = findAll(tipExp, (n) => hasClass(n, "stack-tip-dot"));
  assert(tipDots.some((d) => String(d.attributes.style || "").includes("opacity:0.48")), "Expected tooltip dot opacity 0.48");

  // Case B: Historical role_breakdown fallback with 270ms floor bypass on P90
  const { root: rHist, tooltip: tipHist } = await runCase(cases.hist);
  const bHist = rHist.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const decompHist = findAll(bHist, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  const rectsHist = findAll(decompHist, (n) => n.tagName === "RECT");
  rectsHist.at(-1).dispatchEvent("mousemove", { clientX: 10, clientY: 10 });
  const histP90Tip = nodeText(tipHist);
  assert(histP90Tip.includes("Prefill Engine / API Overhead") && histP90Tip.includes("644.26 ms"), `Expected 644.26 ms Prefill Engine overhead in P90 tip, got: ${histP90Tip}`);
  assert(histP90Tip.includes("Decode Handoff / SSE Overhead") && histP90Tip.includes("750.53 ms"), `Expected 750.53 ms Decode Handoff overhead in P90 tip, got: ${histP90Tip}`);

  // Case C: v6 slice normalization (Stage Share Summary & TTFT Decomposition agree on Engine / API Overhead vs Server TTFT Residual)
  const { root: rV6 } = await runCase(cases.v6);
  const bV6 = rV6.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const stageV6 = findAll(bV6, (n) => n.getAttribute("id") === "b-stage-breakdown")[0];
  const decompV6 = findAll(bV6, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  assert(nodeText(stageV6).includes("334.50 ms"), `Expected normalized v6 engine_overhead_ms 334.50 ms in Stage Share Summary, got: ${nodeText(stageV6)}`);
  assert(nodeText(stageV6).includes("165.50 ms"), `Expected normalized v6 gateway_overhead_ms 165.50 ms in Stage Share Summary, got: ${nodeText(stageV6)}`);
  assert(nodeText(decompV6).includes("Server TTFT Residual"), "Expected Server TTFT Residual when v6 P90 is 10s-clamped");
  assert(nodeText(decompV6).includes("334.50 ms"), "Expected TTFT Decomp to agree with normalized Stage Share Summary");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"explicit": emb_explicit, "hist": emb_hist, "v6": emb_v6}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v29 granular TTFT decomp check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_bundle_tar_gz_prefers_root_and_skips_checkpoints_and_manifest_preserves_zero_ratios(
    sim_env: Path,
) -> None:
    from llm_d_lens.simulation.reporting.builder import render_run_manifest

    task = _make_task(
        "0000003a",
        backend="aiperf",
        backend_options={
            "concurrency": 4,
            "trajectory_start_min_ratio": 0.0,
            "warmup_seconds": 0.0,
        },
    )
    manifest = render_run_manifest(task)
    assert manifest["replay"]["backend_options"]["trajectory_start_min_ratio"] == 0.0
    assert "trajectory_start_max_ratio" not in manifest["replay"]["backend_options"]

    backend_dir = Path(task.task_dir) / "artifacts" / "aiperf"
    (backend_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    (backend_dir / "run_001").mkdir(parents=True, exist_ok=True)
    (backend_dir / "checkpoints" / "profile_export.jsonl").write_text(
        json.dumps({"request_id": "checkpoint-decoy", "metadata": {"benchmark_phase": "profiling"}}) + "\n",
        encoding="utf-8",
    )
    (backend_dir / "run_001" / "profile_export.jsonl").write_text(
        json.dumps({"request_id": "nested-valid", "metadata": {"benchmark_phase": "profiling"}}) + "\n",
        encoding="utf-8",
    )
    root_export = backend_dir / "profile_export.jsonl"
    root_export.write_text(
        json.dumps({"request_id": "root-valid", "metadata": {"benchmark_phase": "profiling"}}) + "\n",
        encoding="utf-8",
    )

    bundle_root = Path(task.task_dir) / "artifacts" / "report" / "bundle_root.tar.gz"
    bundle_root.parent.mkdir(parents=True, exist_ok=True)
    build_bundle_tar_gz(task, output_path=bundle_root)
    with tarfile.open(bundle_root, "r:gz") as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("requests/requests.jsonl"))
        content = tar.extractfile(member).read().decode("utf-8")
    assert "root-valid" in content
    assert "checkpoint-decoy" not in content

    # Without root profile_export.jsonl, bundle must skip checkpoints/ and pick run_001/profile_export.jsonl
    root_export.unlink()
    bundle_nested = Path(task.task_dir) / "artifacts" / "report" / "bundle_nested.tar.gz"
    build_bundle_tar_gz(task, output_path=bundle_nested)
    with tarfile.open(bundle_nested, "r:gz") as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("requests/requests.jsonl"))
        nested_content = tar.extractfile(member).read().decode("utf-8")
    assert "nested-valid" in nested_content
    assert "checkpoint-decoy" not in nested_content


def test_html_report_number_formatting_rule(sim_env: Path) -> None:
    """Offline report_charts.js keeps integers clean and formats non-integer floats with 2 decimals."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    task = _make_task("0000003b")
    cs = _sample_client_series()
    cs["windows"]["profiling"]["throughput_rps"] = 12.5
    cs["windows"]["profiling"]["goodput_rps"] = 12.346
    cs["windows"]["profiling"]["output_tok_per_s"] = 500.0
    cs["windows"]["profiling"]["error_rate_pct"] = 0.05
    cs["windows"]["profiling"]["ttft_ms"] = {
        "mean_ms": 42.0,
        "p50_ms": 0.7,
        "p90_ms": 1500.0,
        "p95_ms": 1500.0,
        "p99_ms": 0.0045,
    }
    cs["windows"]["profiling"]["tpot_ms"] = {
        "mean_ms": 11.0,
        "p50_ms": 11.2,
        "p90_ms": 18.0,
        "p95_ms": 18.0,
        "p99_ms": 0.004,
    }
    html_text = render_html_report(
        task,
        client_series=cs,
        server_telemetry=_sample_server_telemetry(),
        forensics={
            "verdict": {
                "code": "healthy",
                "label": "Healthy",
                "attainment_pct": 100.0,
                "goodput_rps": 12.5,
                "goodput_per_hr": 45000,
            }
        },
    )
    embedded = _extract_embedded_report_json(html_text)

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const chartsPath = process.argv[1];
const src = fs.readFileSync(chartsPath, "utf8");
const instrumented = src.replace(
  /\n\}\)\(\);\s*$/,
  "\nglobalThis.__fmtExports = { fmtDec, num, intFmt, pct, msFmt, tpotFmt, axisNum, secFmt, bytesFmt, formatPreemptions, formatPctChip, formatSecRange };\n})();\n"
);
assert(instrumented !== src, "Failed to instrument report_charts.js");

const embedded = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag).toUpperCase(),
    nodeType: tag === "#text" ? 3 : 1,
    className: "",
    textContent: text ?? "",
    style: {},
    children: [],
    attributes: attrs,
    listeners,
    classList: {
      toggle(c, force) {
        const set = new Set((node.className || "").split(/\s+/).filter(Boolean));
        if (force === undefined) { if (set.has(c)) set.delete(c); else set.add(c); }
        else if (force) set.add(c);
        else set.delete(c);
        node.className = Array.from(set).join(" ");
      },
    },
    setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
    getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
    appendChild(c) { if (c != null) node.children.push(typeof c === "string" ? makeNode("#text", c) : c); return c; },
    append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
    replaceChildren(...items) { node.children = []; node.append(...items); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    closest() { return null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 220 }; },
  };
  return node;
}

const ids = {
  "report-title": makeNode("h1"),
  "report-meta": makeNode("div"),
  "status-badges": makeNode("div"),
  "report-root": makeNode("div"),
  "chart-tooltip": makeNode("div"),
  "lens-report-data": makeNode("script", JSON.stringify(embedded)),
  "sidebar-toggle": makeNode("button"),
  "sidebar": makeNode("aside"),
  "theme-toggle": makeNode("button"),
  "print-btn": makeNode("button"),
  "window-toggle": makeNode("div"),
  "nav-list": makeNode("nav"),
};

const doc = {
  documentElement: makeNode("html"),
  body: { contains: () => true },
  createElement: (tag) => makeNode(tag),
  createElementNS: (_ns, tag) => makeNode(tag),
  createTextNode: (t) => makeNode("#text", String(t)),
  getElementById: (id) => ids[id] || null,
  querySelectorAll: () => [],
};

const ctx = {
  window: { innerWidth: 1400, innerHeight: 900, print() {} },
  document: doc,
  getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
  TextEncoder, TextDecoder, Uint8Array,
  atob: (s) => Buffer.from(s, "base64").toString("binary"),
  URL, Blob, Response,
  DecompressionStream: globalThis.DecompressionStream,
  ResizeObserver: undefined,
  requestAnimationFrame: (fn) => fn(),
  setTimeout, clearTimeout, console,
  Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
  isFinite, parseFloat, parseInt,
};
ctx.window.document = doc;
vm.runInNewContext(instrumented, ctx);

(async () => {
  await new Promise((r) => setTimeout(r, 20));
  const ex = ctx.__fmtExports || vm.runInNewContext("globalThis.__fmtExports", ctx);

  // Core fmtDec / num rules: clean integers, 2 decimals for non-integer floats, -0 guard
  assert.strictEqual(ex.fmtDec(0), "0");
  assert.strictEqual(ex.fmtDec(-0), "0");
  assert.strictEqual(ex.fmtDec(42), "42");
  assert.strictEqual(ex.fmtDec(500), "500");
  assert.strictEqual(ex.fmtDec(1234), "1,234");
  assert.strictEqual(ex.fmtDec(12.5), "12.50");
  assert.strictEqual(ex.fmtDec(12.346), "12.35");
  assert.strictEqual(ex.fmtDec(0.7), "0.70");
  assert.strictEqual(ex.fmtDec(0.05), "0.05");
  assert.strictEqual(ex.fmtDec(0.0045), "0.00");
  assert.strictEqual(ex.fmtDec(-0.001), "0.00");
  assert.strictEqual(ex.fmtDec(1234.5), "1,234.50");
  assert.strictEqual(ex.fmtDec(null), "N/A");
  assert.strictEqual(ex.fmtDec(""), "N/A");
  assert.strictEqual(ex.num(12.5, 1), "12.50");
  assert.strictEqual(ex.num(42, 2), "42");
  assert.strictEqual(ex.num(-0.001, 2), "0.00");

  // pct, msFmt, tpotFmt, axisNum, secFmt, bytesFmt, auxiliary formatters
  assert.strictEqual(ex.pct(0), "0%");
  assert.strictEqual(ex.pct(100), "100%");
  assert.strictEqual(ex.pct(12.5), "12.50%");
  assert.strictEqual(ex.pct(99.9), "99.90%");
  assert.strictEqual(ex.pct(-0.001), "0.00%");

  assert.strictEqual(ex.msFmt(42), "42 ms");
  assert.strictEqual(ex.msFmt(0.7), "0.70 ms");
  assert.strictEqual(ex.msFmt(0.0045), "0.00 ms");
  assert.strictEqual(ex.msFmt(1000), "1 s");
  assert.strictEqual(ex.msFmt(1500.5), "1.50 s");

  assert.strictEqual(ex.tpotFmt(18), "18 ms/tok");
  assert.strictEqual(ex.tpotFmt(11.2), "11.20 ms/tok");
  assert.strictEqual(ex.tpotFmt(0.004), "0.00 ms/tok");

  assert.strictEqual(ex.axisNum(100, 100), "100");
  assert.strictEqual(ex.axisNum(0.05, 0.1), "0.05");
  assert.strictEqual(ex.axisNum(2.5, 10), "2.50");

  assert.strictEqual(ex.secFmt(60), "60 s");
  assert.strictEqual(ex.secFmt(5.5), "5.50 s");

  assert.strictEqual(ex.bytesFmt(1024), "1 KiB");
  assert.strictEqual(ex.bytesFmt(1536), "1.50 KiB");
  assert.strictEqual(ex.bytesFmt(2.5 * 1024 ** 2), "2.50 MiB");
  assert.strictEqual(ex.bytesFmt(3 * 1024 ** 3), "3 GiB");

  assert.strictEqual(ex.formatPreemptions(2), "2");
  assert.strictEqual(ex.formatPreemptions(0.25), "0.25");
  assert.strictEqual(ex.formatPctChip(50), "50%");
  assert.strictEqual(ex.formatPctChip(12.5), "12.50%");
  assert.strictEqual(ex.formatSecRange(0, 5.5), "0–5.50s");

  // Verify rendered overview DOM text follows the exact formatting rules
  function walkText(n) {
    if (!n) return "";
    const parts = n.textContent ? [n.textContent] : [];
    for (const c of n.children || []) parts.push(walkText(c));
    return parts.join(" ");
  }
  const overviewText = walkText(ids["report-root"]);
  assert(overviewText.includes("SLO Attainment: 100%"), `Expected clean integer 100%, got: ${overviewText}`);
  assert(overviewText.includes("12.50 / 12.35 req/s"), `Expected 2-decimal throughput/goodput, got: ${overviewText}`);
  assert(overviewText.includes("500 tok/s"), `Expected clean integer 500 tok/s, got: ${overviewText}`);
  assert(overviewText.includes("0.70 ms / 1.50 s"), `Expected 0.70 ms / 1.50 s TTFT, got: ${overviewText}`);
  assert(overviewText.includes("11.20 ms/tok / 18 ms/tok"), `Expected 11.20 ms/tok / 18 ms/tok TPOT, got: ${overviewText}`);
  assert(overviewText.includes("0.05%"), `Expected 0.05% error rate, got: ${overviewText}`);
  assert(!overviewText.includes("-0.00"), "Must never render negative zero");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node formatting rule check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_html_report_v30_six_slice_ttft_decomp_and_historical_synthesis(sim_env: Path) -> None:
    """v30 offline HTML report: 6-slice TTFT decomposition (Min..Max), primaryRow P50 anchor, historical synthesis, EmptyState guard, and XSS-safe tooltips."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    assert ".stack-tip-note" in template_html
    assert '"Unattributed TTFT":' in charts_js
    assert "Min, P50, Mean, P90, P99, and Max" in charts_js
    for block_name in ("HELP_TEXTS", "HELP_FORMULAS"):
        m = re.search(rf"const {block_name} = \{{(?P<body>.*?)\n  \}};", charts_js, re.DOTALL)
        assert m is not None
        for line in m.group("body").splitlines():
            if ":" not in line:
                continue
            _, val_part = line.split(":", 1)
            assert not re.search(r"\b(vllm|sglang|trtllm|gpu)\b", val_part, re.IGNORECASE), (
                f"{block_name} value leaked engine/GPU wording: {line!r}"
            )

    # Case A: Explicit 6-slice v30 payload + XSS probe in slice_note + primaryRow anchored to P50
    xss_probe = "<img src=x onerror=alert(1)> Server stage histograms do not expose single-request minimums."
    cs_explicit = _sample_client_series()
    st_stub = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "metric_sources": {"vllm_stage_latencies": "role_separated"},
        "step_seconds": 5,
        "pods": ["prefill-0", "decode-0"],
        "per_pod": [
            {"pod": "prefill-0", "role": "prefill", "request_rate_rps": {"mean": 6.0}},
            {"pod": "decode-0", "role": "decode", "request_rate_rps": {"mean": 6.0}},
        ],
        "summary": {"kv_cache_usage_percent": {"mean": 40.0, "max": 55.0}},
        "series": [],
    }
    f_explicit = {
        "schema_version": 8,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "has_kv_transfer_series": True,
            "min": {
                "available": True,
                "source": "server_telemetry",
                "ttft_unattributed_ms": 120.25,
                "decode_time_ms": 400.0,
                "ttft_stage_ms": 120.25,
                "total_stage_ms": 520.25,
                "client_ttft_ms": 120.25,
                "slice_note": xss_probe,
            },
            "p50": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.25,
                "queue_time_ms": 0.0,
                "kv_transfer_ms": 110.0,
                "prefill_time_ms": 220.0,
                "decode_first_step_ms": 25.0,
                "prefill_engine_overhead_ms": 60.0,
                "decode_handoff_overhead_ms": 80.0,
                "engine_overhead_ms": 165.0,
                "gateway_overhead_ms": 0.0,
                "decode_time_ms": 1000.0,
                "ttft_stage_ms": 495.25,
                "total_stage_ms": 1495.25,
                "client_ttft_ms": 495.25,
            },
            "mean": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.3,
                "queue_time_ms": 10.0,
                "kv_transfer_ms": 130.0,
                "prefill_time_ms": 260.0,
                "decode_first_step_ms": 30.0,
                "prefill_engine_overhead_ms": 70.0,
                "decode_handoff_overhead_ms": 90.0,
                "engine_overhead_ms": 190.0,
                "gateway_overhead_ms": 12.0,
                "decode_time_ms": 1100.0,
                "ttft_stage_ms": 602.3,
                "total_stage_ms": 1702.3,
                "client_ttft_ms": 602.3,
            },
            "p90": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.5,
                "queue_time_ms": 25.0,
                "kv_transfer_ms": 200.0,
                "prefill_time_ms": 410.0,
                "decode_first_step_ms": 45.0,
                "prefill_engine_overhead_ms": 110.0,
                "decode_handoff_overhead_ms": 140.0,
                "engine_overhead_ms": 295.0,
                "gateway_overhead_ms": 30.0,
                "decode_time_ms": 1400.0,
                "ttft_stage_ms": 960.5,
                "total_stage_ms": 2360.5,
                "client_ttft_ms": 960.5,
            },
            "p99": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.9,
                "queue_time_ms": 45.0,
                "kv_transfer_ms": 290.0,
                "prefill_time_ms": 610.0,
                "decode_first_step_ms": 60.0,
                "prefill_engine_overhead_ms": 160.0,
                "decode_handoff_overhead_ms": 190.0,
                "engine_overhead_ms": 410.0,
                "gateway_overhead_ms": 40.0,
                "decode_time_ms": 1800.0,
                "ttft_stage_ms": 1395.9,
                "total_stage_ms": 3195.9,
                "client_ttft_ms": 1395.9,
            },
            "max": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 1.2,
                "queue_time_ms": 50.0,
                "kv_transfer_ms": 320.0,
                "prefill_time_ms": 680.0,
                "engine_overhead_ms": 548.8,
                "gateway_overhead_ms": None,
                "decode_time_ms": 2000.0,
                "ttft_stage_ms": 1600.0,
                "total_stage_ms": 3600.0,
                "client_ttft_ms": 1600.0,
                "slice_note": "Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.",
            },
        },
        "ttft_decomposition": {
            "available": True,
            "source": "server_telemetry",
            "min": {
                "available": True,
                "ttft_unattributed_ms": 120.25,
                "client_ttft_ms": 120.25,
                "slice_note": xss_probe,
            },
            "p50": {
                "available": True,
                "prefill_ms": 220.0,
                "decode_first_step_ms": 25.0,
                "queue_ms": 0.0,
                "kv_transfer_ms": 110.0,
                "prefill_engine_overhead_ms": 60.0,
                "decode_handoff_overhead_ms": 80.0,
                "epp_scheduling_ms": 0.25,
                "engine_overhead_ms": 165.0,
                "gateway_overhead_ms": 0.0,
                "client_ttft_ms": 495.25,
            },
            "mean": {
                "available": True,
                "prefill_ms": 260.0,
                "decode_first_step_ms": 30.0,
                "queue_ms": 10.0,
                "kv_transfer_ms": 130.0,
                "prefill_engine_overhead_ms": 70.0,
                "decode_handoff_overhead_ms": 90.0,
                "epp_scheduling_ms": 0.3,
                "engine_overhead_ms": 190.0,
                "gateway_overhead_ms": 12.0,
                "client_ttft_ms": 602.3,
            },
            "p90": {
                "available": True,
                "prefill_ms": 410.0,
                "decode_first_step_ms": 45.0,
                "queue_ms": 25.0,
                "kv_transfer_ms": 200.0,
                "prefill_engine_overhead_ms": 110.0,
                "decode_handoff_overhead_ms": 140.0,
                "epp_scheduling_ms": 0.5,
                "engine_overhead_ms": 295.0,
                "gateway_overhead_ms": 30.0,
                "client_ttft_ms": 960.5,
            },
            "p99": {
                "available": True,
                "prefill_ms": 610.0,
                "decode_first_step_ms": 60.0,
                "queue_ms": 45.0,
                "kv_transfer_ms": 290.0,
                "prefill_engine_overhead_ms": 160.0,
                "decode_handoff_overhead_ms": 190.0,
                "epp_scheduling_ms": 0.9,
                "engine_overhead_ms": 410.0,
                "gateway_overhead_ms": 40.0,
                "client_ttft_ms": 1395.9,
            },
            "max": {
                "available": True,
                "prefill_ms": 680.0,
                "queue_ms": 50.0,
                "kv_transfer_ms": 320.0,
                "epp_scheduling_ms": 1.2,
                "engine_overhead_ms": 548.8,
                "gateway_overhead_ms": None,
                "client_ttft_ms": 1600.0,
                "slice_note": "Server histograms do not report per-request max; stages reflect peak-window P99 bounds reconciled to exact client Max TTFT.",
            },
        },
    }
    html_explicit = render_html_report(
        _make_task("0000003c", name="v30 Explicit 6-Slice"),
        client_series=cs_explicit,
        server_telemetry=st_stub,
        forensics=f_explicit,
    )
    assert "<!-- lens-report-v42 -->" in html_explicit
    emb_explicit = _extract_embedded_report_json(html_explicit)

    # Case B: Historical v8 P/D run with only p50/p90/mean on disk -> live synthesis of min/p99/max with PD sub-split + 305ms floor bypass
    cs_hist = {
        "schema_version": 1,
        "warmup_seconds": 5.0,
        "origin_epoch": 1795700000.0,
        "windows": {
            "profiling": {
                "ttft_ms": {"min": 140.0, "p50": 500.0, "avg": 750.0, "p90": 1500.0, "p99": 2500.0, "max": 2800.0},
                "e2e_ms": {"min": 400.0, "p50": 1200.0, "avg": 1600.0, "p90": 3000.0, "p99": 4500.0, "max": 5000.0},
            },
        },
    }
    st_hist = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "metric_sources": {"vllm_stage_latencies": "role_separated"},
        "step_seconds": 5,
        "pods": ["prefill-0", "decode-0"],
        "per_pod": [
            {"pod": "prefill-0", "role": "prefill", "request_rate_rps": {"mean": 4.0}},
            {"pod": "decode-0", "role": "decode", "request_rate_rps": {"mean": 4.0}},
        ],
        "router": {
            "scheduler_latency_p95_ms": {"mean": 0.4, "p99": 1.0, "max": 1.5},
        },
        "summary": {
            "nixl_transfer_latency_p99_ms": {"mean": 600.0, "p99": 800.0, "max": 900.0},
            "vllm_ttft_p99_ms": {"mean": 2000.0, "p99": 2400.0, "max": 2700.0},
            "vllm_e2e_p99_ms": {"mean": 3500.0, "p99": 4200.0, "max": 4800.0},
            "vllm_prefill_time_p90_ms": {"mean": 800.0, "p99": 1000.0, "max": 1100.0},
            "vllm_queue_time_p90_ms": {"mean": 300.0, "p99": 310.0, "max": 320.0},
            "vllm_decode_time_p90_ms": {"mean": 1500.0, "p99": 1900.0, "max": 2100.0},
        },
        "summary_by_role": {
            "prefill": {
                "vllm_ttft_p99_ms": {"mean": 1200.0, "p99": 1500.0, "max": 1650.0},
                "vllm_queue_time_mean_ms": {"mean": 0.5, "p99": 0.5, "max": 0.5},
                "vllm_queue_time_p90_ms": {"mean": 270.0, "p99": 297.0, "max": 297.0},
                "vllm_prefill_time_p90_ms": {"mean": 800.0, "p99": 1000.0, "max": 1100.0},
            },
            "decode": {
                "vllm_ttft_p99_ms": {"mean": 1100.0, "p99": 1350.0, "max": 1450.0},
                "vllm_queue_time_mean_ms": {"mean": 500.0, "p99": 650.0, "max": 700.0},
                "vllm_queue_time_p90_ms": {"mean": 650.0, "p99": 850.0, "max": 920.0},
                "vllm_prefill_time_p90_ms": {"mean": 250.0, "p99": 297.0, "max": 297.0},
                "vllm_decode_time_p90_ms": {"mean": 1500.0, "p99": 1900.0, "max": 2100.0},
            },
        },
        "series": [],
        "role_series": [],
    }
    f_hist = {
        "schema_version": 8,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "has_kv_transfer_series": True,
            "role_breakdown": {
                "prefill": {
                    "ttft_p50_ms": 300.0,
                    "queue_p50_ms": 150.0,
                    "queue_mean_ms": 0.5,
                    "prefill_p50_ms": 200.0,
                    "ttft_p90_ms": 900.0,
                    "queue_p90_ms": 270.0,
                    "prefill_p90_ms": 600.0,
                    "ttft_mean_ms": 450.0,
                    "prefill_mean_ms": 300.0,
                },
                "decode": {
                    "ttft_p50_ms": 250.0,
                    "queue_raw_p50_ms": 150.0,
                    "queue_raw_mean_ms": 200.0,
                    "prefill_p50_ms": 40.0,
                    "ttft_p90_ms": 700.0,
                    "queue_raw_p90_ms": 400.0,
                    "prefill_p90_ms": 90.0,
                    "ttft_mean_ms": 350.0,
                    "prefill_mean_ms": 50.0,
                },
            },
            "p50": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.2,
                "queue_time_ms": 0.5,
                "kv_transfer_ms": 150.0,
                "prefill_time_ms": 200.0,
                "decode_first_step_ms": 30.0,
                "prefill_engine_overhead_ms": 50.0,
                "decode_handoff_overhead_ms": 69.3,
                "engine_overhead_ms": 149.3,
                "gateway_overhead_ms": 0.0,
                "decode_time_ms": 700.0,
                "client_ttft_ms": 500.0,
            },
            "mean": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.3,
                "queue_time_ms": 2.0,
                "kv_transfer_ms": 200.0,
                "prefill_time_ms": 300.0,
                "decode_first_step_ms": 40.0,
                "prefill_engine_overhead_ms": 80.0,
                "decode_handoff_overhead_ms": 90.0,
                "engine_overhead_ms": 210.0,
                "gateway_overhead_ms": 37.7,
                "decode_time_ms": 850.0,
                "client_ttft_ms": 750.0,
            },
            "p90": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.4,
                "queue_time_ms": 10.0,
                "kv_transfer_ms": 400.0,
                "prefill_time_ms": 600.0,
                "decode_first_step_ms": 80.0,
                "prefill_engine_overhead_ms": 160.0,
                "decode_handoff_overhead_ms": 190.0,
                "engine_overhead_ms": 430.0,
                "gateway_overhead_ms": 59.6,
                "decode_time_ms": 1500.0,
                "client_ttft_ms": 1500.0,
            },
        },
        "ttft_decomposition": {
            "available": True,
            "source": "server_telemetry",
            "p50": {
                "available": True,
                "prefill_ms": 200.0,
                "decode_first_step_ms": 30.0,
                "queue_ms": 0.5,
                "kv_transfer_ms": 150.0,
                "prefill_engine_overhead_ms": 50.0,
                "decode_handoff_overhead_ms": 69.3,
                "epp_scheduling_ms": 0.2,
                "engine_overhead_ms": 149.3,
                "gateway_overhead_ms": 0.0,
                "client_ttft_ms": 500.0,
            },
            "mean": {
                "available": True,
                "prefill_ms": 300.0,
                "decode_first_step_ms": 40.0,
                "queue_ms": 2.0,
                "kv_transfer_ms": 200.0,
                "prefill_engine_overhead_ms": 80.0,
                "decode_handoff_overhead_ms": 90.0,
                "epp_scheduling_ms": 0.3,
                "engine_overhead_ms": 210.0,
                "gateway_overhead_ms": 37.7,
                "client_ttft_ms": 750.0,
            },
            "p90": {
                "available": True,
                "prefill_ms": 600.0,
                "decode_first_step_ms": 80.0,
                "queue_ms": 10.0,
                "kv_transfer_ms": 400.0,
                "prefill_engine_overhead_ms": 160.0,
                "decode_handoff_overhead_ms": 190.0,
                "epp_scheduling_ms": 0.4,
                "engine_overhead_ms": 430.0,
                "gateway_overhead_ms": 59.6,
                "client_ttft_ms": 1500.0,
            },
        },
    }
    html_hist = render_html_report(
        _make_task("0000003d", name="v30 Historical PD Synthesis"),
        client_series=cs_hist,
        server_telemetry=st_hist,
        forensics=f_hist,
    )
    emb_hist = _extract_embedded_report_json(html_hist)

    # Case C: client_fallback EmptyState guard (decomp.available === false must render 0 bars)
    f_client = {
        "schema_version": 8,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "client_fallback",
            "p50": {
                "available": True,
                "source": "client_fallback",
                "ttft_unattributed_ms": 500.0,
                "ttft_stage_ms": 500.0,
                "decode_time_ms": 700.0,
            },
            "p90": {
                "available": True,
                "source": "client_fallback",
                "ttft_unattributed_ms": 1500.0,
                "ttft_stage_ms": 1500.0,
                "decode_time_ms": 1500.0,
            },
            "mean": {
                "available": True,
                "source": "client_fallback",
                "ttft_unattributed_ms": 750.0,
                "ttft_stage_ms": 750.0,
                "decode_time_ms": 850.0,
            },
        },
        "ttft_decomposition": {
            "available": False,
            "source": "client_fallback",
            "reason": "TTFT stage breakdown requires serving engine stage histograms.",
        },
    }
    html_client = render_html_report(
        _make_task("0000003e", name="v30 Client Fallback EmptyState"),
        client_series=cs_hist,
        server_telemetry={"available": False, "status": "unavailable"},
        forensics=f_client,
    )
    emb_client = _extract_embedded_report_json(html_client)

    # Case D: Schema-6 disaggregated run with P99 10s-clamping + NIXL deduction + unbounded MAX tail + mean client_fallback
    cs_s6 = {
        "schema_version": 1,
        "warmup_seconds": 10.0,
        "origin_epoch": 1795700000.0,
        "windows": {
            "profiling": {
                "ttft_ms": {"min": 250.0, "p50": 2000.0, "avg": 4000.0, "p90": 8000.0, "p99": 15000.0, "max": 18000.0},
                "e2e_ms": {"min": 600.0, "p50": 4000.0, "avg": 6500.0, "p90": 12000.0, "p99": 20000.0, "max": 24000.0},
            },
        },
    }
    st_s6 = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "pods": ["prefill-0", "decode-0"],
        "per_pod": [
            {"pod": "prefill-0", "role": "prefill", "request_rate_rps": {"mean": 4.0}},
            {"pod": "decode-0", "role": "decode", "request_rate_rps": {"mean": 4.0}},
        ],
        "step_seconds": 5,
        "router": {"scheduler_latency_p95_ms": {"mean": 0.2, "p99": 0.4, "max": 0.5}},
        "summary": {
            "vllm_ttft_p99_ms": {"mean": 7000.0, "p99": 9850.0, "max": 9900.0},
            "vllm_prefill_time_p90_ms": {"mean": 1500.0, "p99": 3000.0, "max": 3200.0},
            "vllm_queue_time_p90_ms": {"mean": 2000.0, "p99": 5000.0, "max": 5500.0},
            "vllm_decode_time_p90_ms": {"mean": 2500.0, "p99": 4000.0, "max": 4500.0},
            "nixl_transfer_latency_p99_ms": {"mean": 800.0, "p99": 1200.0, "max": 1300.0},
        },
        "series": [],
    }
    f_s6 = {
        "schema_version": 6,
        "verdict": {"code": "healthy", "label": "Healthy"},
        "stage_latency_breakdown": {
            "available": True,
            "source": "server_telemetry",
            "p50": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.2,
                "queue_time_ms": 300.0,
                "prefill_time_ms": 400.0,
                "decode_time_ms": 2000.0,
                "gateway_overhead_ms": 1299.8,
                "server_ttft_ms": 1800.0,
                "client_ttft_ms": 2000.0,
            },
            "mean": {
                "available": True,
                "source": "client_fallback",
                "client_ttft_ms": 4000.0,
                "ttft_stage_ms": 4000.0,
                "decode_time_ms": 2500.0,
            },
            "p90": {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": 0.3,
                "queue_time_ms": 1800.0,
                "prefill_time_ms": 1600.0,
                "decode_time_ms": 4000.0,
                "gateway_overhead_ms": 4599.7,
                "server_ttft_ms": 7200.0,
                "client_ttft_ms": 8000.0,
            },
        },
        "ttft_decomposition": {
            "available": True,
            "vllm_prefill_p50_ms": 400.0,
            "vllm_queue_p50_ms": 300.0,
            "overhead_p50_ms": 1300.0,
        },
    }
    html_s6 = render_html_report(
        _make_task("0000003f", name="v30 Schema-6 Clamped"), client_series=cs_s6, server_telemetry=st_s6, forensics=f_s6
    )
    emb_s6 = _extract_embedded_report_json(html_s6)

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    nodeType: tag === "#text" ? 3 : 1,
    tagName: String(tag).toUpperCase(),
    attributes: {},
    children: [],
    style: {},
    className: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    innerText: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const tooltipNode = makeNode("div");
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": (() => { const s = makeNode("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const doc = {
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: (t) => makeNode(t),
    createElementNS: (_ns, t) => makeNode(t),
    createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || makeNode("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tooltip: tooltipNode };
}

(async () => {
  // Case A: Explicit 6-slice order, Unattributed TTFT swatch, primaryRow=P50, XSS-safe tooltip note
  const { root: rExp, tooltip: tipExp } = await runCase(cases.explicit);
  const bExp = rExp.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const decompExp = findAll(bExp, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  const xLabelsExp = findAll(decompExp, (n) => n.tagName === "TEXT" && nodeText(n).startsWith("TTFT ")).map((n) => nodeText(n));
  assert.strictEqual(
    JSON.stringify(xLabelsExp),
    JSON.stringify(["TTFT MIN", "TTFT P50", "TTFT MEAN", "TTFT P90", "TTFT P99", "TTFT MAX"]),
    `Expected canonical 6-slice order, got ${xLabelsExp}`
  );
  const legendsExp = findAll(decompExp, (n) => hasClass(n, "legend-item")).map((n) => nodeText(n));
  assert(legendsExp.includes("Unattributed TTFT"), "Expected Unattributed TTFT legend item");
  const unattrSwatch = findAll(decompExp, (n) => hasClass(n, "legend-item") && nodeText(n) === "Unattributed TTFT")[0];
  const swatchEl = findAll(unattrSwatch, (n) => hasClass(n, "legend-swatch"))[0];
  assert(String(swatchEl.attributes.style || "").includes("opacity:0.38") && String(swatchEl.attributes.style || "").includes("#f5a623"), `Expected amber 0.38 swatch, got ${swatchEl.attributes.style}`);
  const pillsExp = findAll(decompExp, (n) => hasClass(n, "stage-share-card")).map((n) => nodeText(n));
  assert(!pillsExp.some((p) => p.includes("Unattributed TTFT")), `primaryRow must anchor to P50, not Min! pills=${pillsExp}`);
  assert(pillsExp.some((p) => p.includes("Prefill Time") && p.includes("220 ms")), `Expected P50 Prefill Time pill, got ${pillsExp}`);
  // Hover first rect (TTFT MIN): check fmtDec client_ttft_ms and XSS-safe slice_note
  const rectsExp = findAll(decompExp, (n) => n.tagName === "RECT");
  const minRect = rectsExp.find((r) => Number(r.attributes.height || 0) > 0);
  minRect.dispatchEvent("mousemove", { clientX: 10, clientY: 10 });
  const minTipTxt = nodeText(tipExp);
  assert(minTipTxt.includes("Client TTFT: 120.25 ms"), `Expected 2-decimal Client TTFT in tooltip, got: ${minTipTxt}`);
  assert(minTipTxt.includes("<img src=x onerror=alert(1)>"), `Expected escaped XSS text in slice_note, got: ${minTipTxt}`);
  assert(findAll(tipExp, (n) => n.tagName === "IMG").length === 0, "XSS probe must NOT create an <img> element inside tooltip!");

  // Case B: Historical P/D synthesis of Min, P99, Max with PD sub-split & 305ms queue floor bypass
  const { root: rHist, tooltip: tipHist } = await runCase(cases.hist);
  const bHist = rHist.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const decompHist = findAll(bHist, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  const xLabelsHist = findAll(decompHist, (n) => n.tagName === "TEXT" && nodeText(n).startsWith("TTFT ")).map((n) => nodeText(n));
  assert.strictEqual(
    JSON.stringify(xLabelsHist),
    JSON.stringify(["TTFT MIN", "TTFT P50", "TTFT MEAN", "TTFT P90", "TTFT P99", "TTFT MAX"]),
    `Expected synthesized 6 slices on historical PD run, got ${xLabelsHist}`
  );
  const legendsHist = findAll(decompHist, (n) => hasClass(n, "legend-item")).map((n) => nodeText(n));
  assert(!legendsHist.includes("Engine / API Overhead"), `P99/Max PD sub-split must avoid redundant Engine / API Overhead legend, got ${legendsHist}`);
  const pillsHist = findAll(decompHist, (n) => hasClass(n, "stage-share-card")).map((n) => nodeText(n));
  assert(pillsHist.some((p) => p.includes("Prefill Time") && p.includes("200 ms")), `Historical primaryRow must anchor to P50, got ${pillsHist}`);
  const rectsHist = findAll(decompHist, (n) => n.tagName === "RECT");
  rectsHist.at(-1).dispatchEvent("mousemove", { clientX: 10, clientY: 10 });
  const maxTipHist = nodeText(tipHist);
  assert(maxTipHist.includes("Client TTFT: 2.80 s"), `Expected Client TTFT: 2.80 s in Max tooltip, got: ${maxTipHist}`);
  assert(maxTipHist.includes("Server histograms do not report per-request max"), `Expected Max slice_note in tooltip, got: ${maxTipHist}`);

  // Case C: client_fallback EmptyState guard -> 0 bars
  const { root: rCli } = await runCase(cases.client);
  const bCli = rCli.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const decompCli = findAll(bCli, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  assert.strictEqual(findAll(decompCli, (n) => n.tagName === "RECT").length, 0, "client_fallback must render 0 TTFT bars");
  assert(nodeText(decompCli).includes("TTFT stage breakdown requires serving engine stage histograms."), "Expected empty-state text");

  // Case D: Schema-6 10s clamping on P99 (when P90 unclamped) + NIXL deduction + schema-6 mean client_fallback
  const { root: rS6 } = await runCase(cases.schema6);
  const bS6 = rS6.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const decompS6 = findAll(bS6, (n) => n.getAttribute("id") === "b-ttft-decomp")[0];
  const xLabelsS6 = findAll(decompS6, (n) => n.tagName === "TEXT" && nodeText(n).startsWith("TTFT ")).map((n) => nodeText(n));
  assert.strictEqual(
    JSON.stringify(xLabelsS6),
    JSON.stringify(["TTFT MIN", "TTFT P50", "TTFT MEAN", "TTFT P90", "TTFT P99", "TTFT MAX"]),
    `Expected 6 slices on schema-6 run, got ${xLabelsS6}`
  );
  const s6Txt = nodeText(decompS6);
  assert(s6Txt.includes("Server TTFT quantile hit the 10 s histogram ceiling"), `Expected hoisted P99 10s clamping caveat, got: ${s6Txt}`);
  assert(s6Txt.includes("Server TTFT Residual"), "Expected Server TTFT Residual label when P99 hits 10s ceiling");
  assert(s6Txt.includes("KV Transfer (NIXL)"), "Expected NIXL deduction on schema-6 disaggregated P99/Max");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"explicit": emb_explicit, "hist": emb_hist, "client": emb_client, "schema6": emb_s6}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v30 6-slice TTFT decomp check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v31_latency_over_time_6_stat_selector(sim_env: Path) -> None:
    """v31 offline report renders #o-perf 6-stat selector (Min..Max), dynamic Y-axis, legacy fallback, and 2-dec tooltips."""
    from llm_d_lens.simulation.models import ArtifactRecord
    from llm_d_lens.simulation.reporting import builder as report_builder
    from llm_d_lens.simulation.telemetry_post import build_client_series

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    assert ".pill-btn:disabled, .pill-btn.muted" in template_html
    assert ".perf-controls" in template_html
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in charts_js

    task = _make_task("0000003d", name="v31 Latency 6-Stat Selector")
    origin = 1_795_700_000.0
    sample_vals = [
        (80.25, 12.125, 450.5),
        (100.0, 14.0, 520.0),
        (120.5, 16.0, 610.25),
        (140.5, 18.5, 720.25),
        (160.0, 20.0, 790.0),
        (180.5, 21.5, 860.5),
        (200.25, 23.0, 930.0),
        (210.75, 24.75, 980.0),
        (225.0, 25.5, 1020.0),
        (240.0, 26.25, 1080.25),
        (2400.75, 82.25, 5100.75),
    ]
    records = []
    for bucket_idx in range(2):
        for idx, val in enumerate(sample_vals):
            t_off = bucket_idx * 5.0 + 0.2 + idx * 0.3
            records.append(
                ArtifactRecord(
                    request_id=f"r-{bucket_idx}-{idx}",
                    arrived=origin + t_off,
                    completed=origin + t_off + 0.1,
                    successful=True,
                    input_tokens=200,
                    output_tokens=32,
                    requested_output_tokens=32,
                    ttft_ms=val[0] + bucket_idx * 20.0,
                    tpot_ms=val[1],
                    latency_ms=val[2] + bucket_idx * 50.0,
                    s_time_drift_ms=5.0,
                )
            )
    cs_full = build_client_series(task, records)
    assert cs_full["buckets"][0]["ttft"]["min"] is not None
    assert cs_full["buckets"][0]["ttft"]["max"] is not None

    html_full = render_html_report(
        task,
        client_series=cs_full,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert "<!-- lens-report-v42 -->" in html_full
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_full
    emb_full = _extract_embedded_report_json(html_full)
    assert emb_full["client_series"]["buckets"][0]["ttft"]["min"] is not None
    assert emb_full["client_series"]["buckets"][0]["ttft"]["max"] is not None

    # Legacy v10-style buckets lacking min and max
    cs_legacy = {
        "schema_version": 10,
        "warmup_seconds": 0.0,
        "duration_seconds": 12.0,
        "bucket_seconds": 5,
        "buckets": [
            {
                "index": 0,
                "start_seconds": 0,
                "end_seconds": 5,
                "mid_seconds": 2.5,
                "ttft": {"p50": 150.25, "mean": 175.5, "p90": 240.75, "p99": 280.0},
                "tpot": {"p50": 18.5, "mean": 20.25, "p90": 28.5, "p99": 32.0},
                "e2e": {"p50": 720.0, "mean": 810.5, "p90": 1100.0, "p99": 1250.0},
            },
            {
                "index": 1,
                "start_seconds": 5,
                "end_seconds": 10,
                "mid_seconds": 7.5,
                "ttft": {"p50": 160.0, "mean": 180.0, "p90": 250.0, "p99": 290.0},
                "tpot": {"p50": 19.0, "mean": 21.0, "p90": 29.0, "p99": 33.0},
                "e2e": {"p50": 750.0, "mean": 830.0, "p90": 1150.0, "p99": 1300.0},
            },
        ],
        "windows": {"full": {"request_count": 10, "successful_requests": 10, "error_rate_pct": 0.0}},
    }
    html_legacy = render_html_report(
        _make_task("0000003e", name="v31 Legacy Buckets"),
        client_series=cs_legacy,
        server_telemetry={"available": False},
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    emb_legacy = _extract_embedded_report_json(html_legacy)

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    tagName: tag.toUpperCase(),
    id: "",
    children: [],
    attributes: {},
    style: {},
    className: "",
    disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    innerText: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
      if (k === "disabled") this.disabled = true;
    },
    removeAttribute(k) {
      delete this.attributes[k];
      if (k === "disabled") this.disabled = false;
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}
function parseAxisTickNum(txt) {
  if (!txt || txt.endsWith("s")) return null;
  const cleaned = txt.replace(/,/g, "");
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

async function runCase(payload) {
  const tooltipNode = makeNode("div");
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": (() => { const s = makeNode("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const doc = {
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: (t) => makeNode(t),
    createElementNS: (_ns, t) => makeNode(t),
    createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || makeNode("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tooltip: tooltipNode };
}

(async () => {
  // 1. Full 6-stat interactive #o-perf assertions
  const { root, tooltip } = await runCase(cases.full);
  const oPage = root.children.find((n) => n.getAttribute("data-page") === "overview");
  const oPerf = findAll(oPage, (n) => n.getAttribute("id") === "o-perf")[0];
  assert(oPerf, "Missing #o-perf section");

  const statButtons = findAll(oPerf, (n) => n.tagName === "BUTTON" && n.getAttribute("data-stat") !== null);
  assert.strictEqual(
    JSON.stringify(statButtons.map((b) => nodeText(b))),
    JSON.stringify(["Min", "P50", "Mean", "P90", "P99", "Max"]),
    "Stat buttons must appear in canonical Min, P50, Mean, P90, P99, Max order"
  );
  // Default selection: P50, Mean, P90 active with aria-pressed="true"
  const pressedDefault = statButtons.map((b) => [b.getAttribute("data-stat"), b.getAttribute("aria-pressed"), hasClass(b, "active")]);
  assert.strictEqual(
    JSON.stringify(pressedDefault),
    JSON.stringify([
      ["min", "false", false],
      ["p50", "true", true],
      ["mean", "true", true],
      ["p90", "true", true],
      ["p99", "false", false],
      ["max", "false", false],
    ]),
    `Unexpected default stat selection: ${JSON.stringify(pressedDefault)}`
  );
  const allToggleBtn = findAll(oPerf, (n) => n.tagName === "BUTTON" && n.getAttribute("data-stat-action") === "toggle-all-default")[0];
  assert(allToggleBtn && nodeText(allToggleBtn) === "All" && allToggleBtn.getAttribute("aria-pressed") === "false");

  // Default P50-P90 shaded band path exists
  assert(findAll(oPerf, (n) => n.tagName === "PATH" && n.getAttribute("opacity") === "0.16").length >= 1, "Expected P50-P90 shaded band");

  // Switch to TTFT mode and test dynamic Y-axis expansion when showing Max
  const ttftBtn = findAll(oPerf, (n) => n.tagName === "BUTTON" && nodeText(n) === "TTFT")[0];
  ttftBtn.dispatchEvent("click", {});
  const yTicksDefault = findAll(oPerf, (n) => n.tagName === "TEXT").map((n) => parseAxisTickNum(nodeText(n))).filter((v) => v !== null);
  const maxYDefault = Math.max(...yTicksDefault);

  const maxBtn = statButtons.find((b) => b.getAttribute("data-stat") === "max");
  maxBtn.dispatchEvent("click", {});
  assert.strictEqual(maxBtn.getAttribute("aria-pressed"), "true");
  const yTicksWithMax = findAll(oPerf, (n) => n.tagName === "TEXT").map((n) => parseAxisTickNum(nodeText(n))).filter((v) => v !== null);
  const maxYWithMax = Math.max(...yTicksWithMax);
  assert(maxYWithMax > maxYDefault, `Enabling Max must expand Y-axis: default=${maxYDefault}, withMax=${maxYWithMax}`);

  // Hide Max again -> Y-axis zooms back down
  maxBtn.dispatchEvent("click", {});
  const yTicksZoomed = findAll(oPerf, (n) => n.tagName === "TEXT").map((n) => parseAxisTickNum(nodeText(n))).filter((v) => v !== null);
  assert.strictEqual(Math.max(...yTicksZoomed), maxYDefault, "Hiding Max must zoom Y-axis back onto P50/Mean/P90");

  // All / Default toggle shows all 6 curves with distinct styles and resets cleanly
  allToggleBtn.dispatchEvent("click", {});
  assert.strictEqual(nodeText(allToggleBtn), "Default");
  assert.strictEqual(allToggleBtn.getAttribute("aria-pressed"), "true");
  const legendAll = findAll(oPerf, (n) => hasClass(n, "legend-item")).map(nodeText);
  assert.strictEqual(
    JSON.stringify(legendAll),
    JSON.stringify(["TTFT Min (ms)", "TTFT P50 (ms)", "TTFT Mean (ms)", "TTFT P90 (ms)", "TTFT P99 (ms)", "TTFT Max (ms)"]),
    `Expected all 6 TTFT legend entries, got ${JSON.stringify(legendAll)}`
  );
  const curveStyles = findAll(oPerf, (n) => n.tagName === "PATH" && n.getAttribute("fill") === "none")
    .map((p) => `${p.getAttribute("stroke")}|${p.getAttribute("stroke-dasharray")}|${p.getAttribute("opacity")}`);
  assert.strictEqual(new Set(curveStyles).size, 6, `All 6 stat curves must have distinct visual styles: ${JSON.stringify(curveStyles)}`);

  // Hover tooltip preserves 2-decimal formatting (msFmt)
  const overlay = findAll(oPerf, (n) => n.tagName === "RECT" && n.getAttribute("fill") === "transparent")[0];
  overlay.dispatchEvent("mousemove", { clientX: 100, clientY: 100 });
  const tipTxt = nodeText(tooltip);
  assert(tipTxt.includes("TTFT P50 (ms):") && (tipTxt.includes(" ms") || tipTxt.includes(" s")), `Unexpected TTFT tooltip: ${tipTxt}`);

  // Click Default -> resets back to P50, Mean, P90
  allToggleBtn.dispatchEvent("click", {});
  assert.strictEqual(nodeText(allToggleBtn), "All");
  assert.strictEqual(
    JSON.stringify(statButtons.filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.getAttribute("data-stat"))),
    JSON.stringify(["p50", "mean", "p90"])
  );

  // Guard against deselecting last active stat
  const p50Btn = statButtons.find((b) => b.getAttribute("data-stat") === "p50");
  const meanBtn = statButtons.find((b) => b.getAttribute("data-stat") === "mean");
  const p90Btn = statButtons.find((b) => b.getAttribute("data-stat") === "p90");
  meanBtn.dispatchEvent("click", {});
  p90Btn.dispatchEvent("click", {});
  // Without P90, P50-P90 shaded band disappears
  assert.strictEqual(findAll(oPerf, (n) => n.tagName === "PATH" && n.getAttribute("opacity") === "0.16").length, 0, "Shaded band must disappear when P90 is deselected");
  // Clicking last remaining active stat (P50) must be a no-op
  p50Btn.dispatchEvent("click", {});
  assert.strictEqual(p50Btn.getAttribute("aria-pressed"), "true", "Last active stat must remain selected");

  // 2. Legacy buckets lacking min/max -> disable Min and Max pills gracefully
  const { root: rootLeg } = await runCase(cases.legacy);
  const oPageLeg = rootLeg.children.find((n) => n.getAttribute("data-page") === "overview");
  const oPerfLeg = findAll(oPageLeg, (n) => n.getAttribute("id") === "o-perf")[0];
  const legStatBtns = findAll(oPerfLeg, (n) => n.tagName === "BUTTON" && n.getAttribute("data-stat") !== null);
  for (const key of ["min", "max"]) {
    const b = legStatBtns.find((x) => x.getAttribute("data-stat") === key);
    assert(b.disabled === true && b.getAttribute("disabled") === "disabled", `${key} must be disabled on legacy buckets`);
    assert(hasClass(b, "muted"), `${key} must have muted class on legacy buckets`);
    assert.strictEqual(b.getAttribute("aria-pressed"), "false");
    assert((b.getAttribute("title") || "").includes("unavailable"), `${key} missing unavailable title tooltip`);
  }
  const legLegend = findAll(oPerfLeg, (n) => hasClass(n, "legend-item")).map(nodeText);
  assert(!legLegend.some((l) => l.includes("Min") || l.includes("Max")), `Legacy chart must skip Min/Max: ${JSON.stringify(legLegend)}`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"full": emb_full, "legacy": emb_legacy}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v31 #o-perf 6-stat check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v32_warmup_vs_steady_state_6_stat_chart(sim_env: Path) -> None:
    """v32 offline report & service summary render #o-cold 6-stat grouped bar chart with TTFT/TPOT/E2E pills and legacy fallbacks."""
    from llm_d_lens.simulation.models import ArtifactRecord
    from llm_d_lens.simulation.reporting import builder as report_builder
    from llm_d_lens.simulation.service import _build_telemetry_evidence_pack
    from llm_d_lens.simulation.telemetry_post import build_client_series

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in charts_js
    for help_key in (
        "Warmup vs Steady-State Latency",
        "Warmup Phase",
        "Warmup Phase (Auto 10%)",
        "Steady-State Profiling Window",
        "o-cold",
    ):
        assert f'"{help_key}":' in charts_js

    task = _make_task("0000003f", name="v32 Cold Start 6-Stat Chart", backend_options={"warmup_seconds": 5.0})
    origin = 1_795_700_000.0
    records = []
    # Warmup window [0, 5s): slower TTFT/TPOT/E2E
    for idx, (ttft, tpot, e2e) in enumerate(
        [
            (210.25, 22.5, 920.5),
            (250.5, 25.25, 1050.0),
            (290.0, 28.0, 1180.25),
            (340.75, 31.5, 1320.5),
            (410.0, 35.75, 1540.75),
        ]
    ):
        t_off = 0.5 + idx * 0.8
        records.append(
            ArtifactRecord(
                request_id=f"w-{idx}",
                arrived=origin + t_off,
                completed=origin + t_off + 0.2,
                successful=True,
                input_tokens=200,
                output_tokens=32,
                requested_output_tokens=32,
                ttft_ms=ttft,
                tpot_ms=tpot,
                latency_ms=e2e,
                s_time_drift_ms=5.0,
            )
        )
    # Steady-state window [5s, 15s): faster TTFT/TPOT/E2E
    for idx, (ttft, tpot, e2e) in enumerate(
        [
            (95.25, 12.25, 480.25),
            (115.5, 14.5, 560.5),
            (130.0, 16.0, 620.0),
            (155.25, 18.25, 710.75),
            (185.75, 21.5, 840.25),
        ]
    ):
        t_off = 5.5 + idx * 1.2
        records.append(
            ArtifactRecord(
                request_id=f"s-{idx}",
                arrived=origin + t_off,
                completed=origin + t_off + 0.15,
                successful=True,
                input_tokens=200,
                output_tokens=32,
                requested_output_tokens=32,
                ttft_ms=ttft,
                tpot_ms=tpot,
                latency_ms=e2e,
                s_time_drift_ms=4.0,
            )
        )

    cs_full = build_client_series(task, records)
    cold_cmp = cs_full["cold_start_comparison"]
    for side in ("warmup", "steady_state"):
        for metric in ("ttft_ms", "tpot_ms", "e2e_ms"):
            for stat_key in ("min", "p50", "avg", "p90", "p99", "max"):
                assert cold_cmp[side][metric][stat_key] is not None

    # Verify service.py evidence pack preserves all 3 metrics while staying backwards-compatible when tpot/e2e are absent
    pack_full, _ = _build_telemetry_evidence_pack(task, {"client_series": cs_full})
    pack_cold = pack_full["client_kpis"]["cold_start_comparison"]
    assert pack_cold["mode"] == cold_cmp["mode"]
    assert pack_cold["cold_to_steady_ratio"] == cold_cmp["cold_to_steady_ratio"]
    for prefix in ("warmup", "steady"):
        for metric in ("ttft_ms", "tpot_ms", "e2e_ms"):
            summary_dict = pack_cold[f"{prefix}_{metric}"]
            for stat_key in ("min", "p50", "avg", "p90", "p99", "max"):
                assert stat_key in summary_dict

    pack_legacy, _ = _build_telemetry_evidence_pack(
        task,
        {
            "client_series": {
                "cold_start_comparison": {
                    "mode": "configured_warmup",
                    "cold_to_steady_ratio": 1.5,
                    "warmup": {"ttft_ms": {"p50": 300.0, "p90": 420.0}},
                    "steady_state": {"ttft_ms": {"p50": 200.0, "p90": 280.0}},
                }
            }
        },
    )
    legacy_cold_pack = pack_legacy["client_kpis"]["cold_start_comparison"]
    assert legacy_cold_pack["warmup_ttft_ms"] == {"p50": 300.0, "p90": 420.0}
    assert legacy_cold_pack["steady_ttft_ms"] == {"p50": 200.0, "p90": 280.0}
    for absent_key in ("warmup_tpot_ms", "steady_tpot_ms", "warmup_e2e_ms", "steady_e2e_ms"):
        assert absent_key not in legacy_cold_pack

    html_full = render_html_report(
        task,
        client_series=cs_full,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert "<!-- lens-report-v42 -->" in html_full
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_full
    emb_full = _extract_embedded_report_json(html_full)

    # Legacy/partial fixture: flat TTFT P50 + P90 keys + asymmetric warmup-only P99, no TPOT/E2E
    cs_legacy_partial = {
        "warmup_seconds": 5.0,
        "duration_seconds": 15.0,
        "buckets": [],
        "cold_start_comparison": {
            "mode": "configured_warmup",
            "cold_to_steady_ratio": 1.6,
            "warmup": {"request_count": 8, "ttft_p50_ms": 320.25, "ttft_p90_ms": 480.5, "ttft_p99_ms": 590.75},
            "steady_state": {"request_count": 40, "ttft_p50_ms": 200.0, "ttft_p90_ms": 310.25},
        },
        "windows": {"full": {"request_count": 48, "successful_requests": 48, "error_rate_pct": 0.0}},
    }
    emb_partial = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000040", name="v32 Partial Cold Start"),
            client_series=cs_legacy_partial,
            server_telemetry={"available": False},
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    # Fallback fixture without cold_start_comparison, relying on windows.warmup and windows.profiling
    cs_windows_fallback = {
        "warmup_seconds": 5.0,
        "duration_seconds": 15.0,
        "buckets": [],
        "windows": {
            "full": cs_full["windows"]["full"],
            "warmup": cs_full["windows"]["warmup"],
            "profiling": cs_full["windows"]["profiling"],
        },
    }
    emb_windows = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000041", name="v32 Windows Fallback"),
            client_series=cs_windows_fallback,
            server_telemetry={"available": False},
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    tagName: tag.toUpperCase(),
    id: "",
    children: [],
    attributes: {},
    style: {},
    className: "",
    disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    innerText: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
      if (k === "disabled") this.disabled = true;
    },
    removeAttribute(k) {
      delete this.attributes[k];
      if (k === "disabled") this.disabled = false;
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const tooltipNode = makeNode("div");
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": (() => { const s = makeNode("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": makeNode("div"),
    "nav-list": makeNode("nav"),
  };
  const doc = {
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: (t) => makeNode(t),
    createElementNS: (_ns, t) => makeNode(t),
    createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || makeNode("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tooltip: tooltipNode };
}

(async () => {
  // 1. Full 6-stat #o-cold assertions (grouped bar chart + TTFT/TPOT/E2E pills + summary KPI cards)
  const { root, tooltip } = await runCase(cases.full);
  const oPage = root.children.find((n) => n.getAttribute("data-page") === "overview");
  const oCold = findAll(oPage, (n) => n.getAttribute("id") === "o-cold")[0];
  assert(oCold, "Missing #o-cold section");

  const metricPills = findAll(oCold, (n) => n.tagName === "BUTTON" && n.getAttribute("data-cold-metric") !== null);
  assert.strictEqual(
    JSON.stringify(metricPills.map((b) => [b.getAttribute("data-cold-metric"), nodeText(b), b.getAttribute("aria-pressed")])),
    JSON.stringify([
      ["ttft", "TTFT", "true"],
      ["tpot", "TPOT", "false"],
      ["e2e", "E2E", "false"],
    ]),
    "Expected TTFT | TPOT | E2E pills with TTFT active by default"
  );

  const svg = findAll(oCold, (n) => n.tagName === "SVG")[0];
  assert(svg, "#o-cold must render an SVG grouped bar chart");
  const bars = findAll(svg, (n) => n.tagName === "RECT");
  assert.strictEqual(bars.length, 12, `Expected 12 grouped bars (6 stats x 2 series), got ${bars.length}`);
  for (const r of bars) {
    assert(!String(r.getAttribute("y")).includes("NaN") && !String(r.getAttribute("height")).includes("NaN"), "Bar rect has NaN geometry");
  }
  const textLabels = findAll(svg, (n) => n.tagName === "TEXT").map(nodeText);
  const canonicalStats = ["Min", "P50", "Mean", "P90", "P99", "Max"];
  const xStatOrder = textLabels.filter((t) => canonicalStats.includes(t));
  assert.strictEqual(JSON.stringify(xStatOrder), JSON.stringify(canonicalStats), `Expected monotonic 6-stat order, got ${JSON.stringify(xStatOrder)}`);
  assert(textLabels.includes("ms"), `Expected ms Y-axis label on TTFT, got ${JSON.stringify(textLabels)}`);

  const legend = findAll(oCold, (n) => hasClass(n, "legend-item")).map(nodeText);
  assert.strictEqual(JSON.stringify(legend), JSON.stringify(["Warmup Phase", "Steady-State"]));

  // Hover tooltip on TTFT bar formats with msFmt (2-decimal)
  bars[2].dispatchEvent("mousemove", { clientX: 120, clientY: 120 });
  assert(nodeText(tooltip).includes("P50 · Warmup Phase:") && nodeText(tooltip).includes("ms"), `Unexpected TTFT tooltip: ${nodeText(tooltip)}`);

  const kpiCards = findAll(oCold, (n) => hasClass(n, "kpi"));
  assert.strictEqual(kpiCards.length, 2, "Expected 2 summary KPI cards under #o-cold chart");
  const ttftSubText = nodeText(kpiCards[0]);
  assert(ttftSubText.includes("n=5") && ttftSubText.includes("Warmup / Steady-State:") && ttftSubText.includes("TTFT Min:") && ttftSubText.includes("Max:"));

  // Switch to TPOT pill -> Y-axis updates to ms/tok, tooltip uses tpotFmt, summary cards update
  const tpotBtn = metricPills.find((b) => b.getAttribute("data-cold-metric") === "tpot");
  tpotBtn.dispatchEvent("click", {});
  assert.strictEqual(tpotBtn.getAttribute("aria-pressed"), "true");
  assert.strictEqual(metricPills[0].getAttribute("aria-pressed"), "false");
  const tpotSvg = findAll(oCold, (n) => n.tagName === "SVG")[0];
  const tpotBars = findAll(tpotSvg, (n) => n.tagName === "RECT");
  assert.strictEqual(tpotBars.length, 12);
  const tpotTexts = findAll(tpotSvg, (n) => n.tagName === "TEXT").map(nodeText);
  assert(tpotTexts.includes("ms/tok"), `Expected ms/tok Y-axis label on TPOT, got ${JSON.stringify(tpotTexts)}`);
  tpotBars[2].dispatchEvent("mousemove", { clientX: 120, clientY: 120 });
  assert(nodeText(tooltip).includes("ms/tok"), `Unexpected TPOT tooltip: ${nodeText(tooltip)}`);
  assert(nodeText(findAll(oCold, (n) => hasClass(n, "kpi"))[0]).includes("TPOT Min:"));

  // Switch to E2E pill -> Y-axis updates to ms, tooltip uses msFmt, summary cards update
  const e2eBtn = metricPills.find((b) => b.getAttribute("data-cold-metric") === "e2e");
  e2eBtn.dispatchEvent("click", {});
  assert.strictEqual(e2eBtn.getAttribute("aria-pressed"), "true");
  const e2eSvg = findAll(oCold, (n) => n.tagName === "SVG")[0];
  const e2eBars = findAll(e2eSvg, (n) => n.tagName === "RECT");
  assert.strictEqual(e2eBars.length, 12);
  const e2eTexts = findAll(e2eSvg, (n) => n.tagName === "TEXT").map(nodeText);
  assert(e2eTexts.includes("ms"), `Expected ms Y-axis label on E2E, got ${JSON.stringify(e2eTexts)}`);
  e2eBars[2].dispatchEvent("mousemove", { clientX: 120, clientY: 120 });
  assert(nodeText(tooltip).includes("E2E") || nodeText(tooltip).includes(" s") || nodeText(tooltip).includes(" ms"), `Unexpected E2E tooltip: ${nodeText(tooltip)}`);
  assert(nodeText(findAll(oCold, (n) => hasClass(n, "kpi"))[0]).includes("E2E Min:"));

  // 2. Partial/legacy flat-key fixture (TTFT only, P50 + P90 + asymmetric warmup-only P99)
  const { root: rootPart } = await runCase(cases.partial);
  const oColdPart = findAll(rootPart, (n) => n.getAttribute("id") === "o-cold")[0];
  assert(oColdPart, "Missing #o-cold on partial fixture");
  assert.strictEqual(findAll(oColdPart, (n) => n.tagName === "BUTTON" && n.getAttribute("data-cold-metric") !== null).length, 0, "Should hide metric pills when only 1 metric is available");
  const partSvg = findAll(oColdPart, (n) => n.tagName === "SVG")[0];
  const partBars = findAll(partSvg, (n) => n.tagName === "RECT");
  assert.strictEqual(partBars.length, 5, `Expected 5 bars (P50x2 + P90x2 + warmup-only P99), got ${partBars.length}`);
  const partXLabels = findAll(partSvg, (n) => n.tagName === "TEXT").map(nodeText).filter((t) => canonicalStats.includes(t));
  assert.strictEqual(JSON.stringify(partXLabels), JSON.stringify(["P50", "P90", "P99"]));
  assert(!nodeText(oColdPart).includes("NaN"), "Partial #o-cold must not contain NaN");

  // 3. Fallback to windows.warmup / windows.profiling without cold_start_comparison
  const { root: rootWin } = await runCase(cases.windows);
  const oColdWin = findAll(rootWin, (n) => n.getAttribute("id") === "o-cold")[0];
  assert(oColdWin, "Missing #o-cold on windows fallback fixture");
  const winPills = findAll(oColdWin, (n) => n.tagName === "BUTTON" && n.getAttribute("data-cold-metric") !== null);
  assert.strictEqual(winPills.length, 3, "Windows fallback must expose TTFT, TPOT, E2E pills");
  const winBars = findAll(findAll(oColdWin, (n) => n.tagName === "SVG")[0], (n) => n.tagName === "RECT");
  assert.strictEqual(winBars.length, 12, "Windows fallback must plot all 12 grouped bars");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"full": emb_full, "partial": emb_partial, "windows": emb_windows}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v32 #o-cold 6-stat check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def _sample_workload_window(
    *,
    total: int = 184,
    completed: int = 180,
    failed: int = 4,
    duration_seconds: float = 90.0,
    isl_p50: float = 1024,
    is_multi_turn: bool = True,
    reusable_pct: float | None = 48.3,
) -> dict:
    return {
        "window_start_seconds": 30.0 if total < 200 else 0.0,
        "window_end_seconds": 30.0 + duration_seconds if total < 200 else duration_seconds,
        "duration_seconds": duration_seconds,
        "summary": {
            "counts": {"completed": completed, "failed": failed, "total": total},
            "total_input_tokens": 245120 if total < 200 else 295000,
            "total_output_tokens": 61240 if total < 200 else 73100,
            "isl": {"min": 64, "p50": isl_p50, "mean": 1332.17, "p90": 3200, "p99": 7900, "max": 8192, "cv": 0.94},
            "osl": {"min": 8, "p50": 256, "mean": 332.83, "p90": 768, "p99": 1024, "max": 1200, "cv": 0.78},
            "osl_isl_ratio": {
                "sample_count": total,
                "excluded_zero_isl_count": 0,
                "mean": 0.42,
                "p50": 0.28,
                "p90": 1.15,
                "regimes": {
                    "prefill_heavy": {"count": 72, "share_pct": 39.13},
                    "balanced": {"count": 88, "share_pct": 47.83},
                    "decode_heavy": {"count": 24, "share_pct": 13.04},
                },
            },
            "concurrency": {
                "mean": 11.43,
                "p50": 11,
                "p90": 17,
                "p99": 21,
                "peak": 22,
                "active_duration_seconds": duration_seconds - 0.6,
                "window_duration_seconds": duration_seconds,
            },
            "arrivals": {
                "mean_arrival_rps": 2.04,
                "peak_arrival_rps": 4.2,
                "burstiness_peak_to_mean": 2.05,
                "inter_arrival_cv": 1.03,
            },
        },
        "isl_histogram": [
            {"bin_start": 0, "bin_end": 512, "label": "0–512", "count": 40, "share_pct": 21.74},
            {"bin_start": 512, "bin_end": 2048, "label": "512–2K", "count": 96, "share_pct": 52.17},
            {"bin_start": 2048, "bin_end": 8192, "label": "2K–8K", "count": 48, "share_pct": 26.09},
        ],
        "isl_cdf": [
            {"percentile": 0, "value": 64},
            {"percentile": 50, "value": isl_p50},
            {"percentile": 90, "value": 3200},
            {"percentile": 99.9, "value": 8192},
        ],
        "osl_histogram": [
            {"bin_start": 0, "bin_end": 256, "label": "0–256", "count": 90, "share_pct": 48.91},
            {"bin_start": 256, "bin_end": 1200, "label": "256–1,200", "count": 94, "share_pct": 51.09},
        ],
        "osl_cdf": [
            {"percentile": 0, "value": 8},
            {"percentile": 50, "value": 256},
            {"percentile": 90, "value": 768},
            {"percentile": 99.9, "value": 1200},
        ],
        "isl_vs_osl_buckets": [
            {
                "label": "<512",
                "min_tokens": 0,
                "max_tokens": 512,
                "count": 40,
                "share_pct": 21.74,
                "osl_p50": 192,
                "osl_mean": 215.4,
                "osl_p90": 480,
                "ratio_p50": 0.63,
                "ratio_mean": 0.74,
            },
            {
                "label": "512-2K",
                "min_tokens": 512,
                "max_tokens": 2048,
                "count": 96,
                "share_pct": 52.17,
                "osl_p50": 280,
                "osl_mean": 310.2,
                "osl_p90": 720,
                "ratio_p50": 0.27,
                "ratio_mean": 0.31,
            },
        ],
        "concurrency_residency": [
            {"min_level": 0, "max_level": 0, "label": "0 (idle)", "duration_seconds": 0.6, "share_pct": 0.67},
            {"min_level": 1, "max_level": 4, "label": "1–4", "duration_seconds": 12.4, "share_pct": 13.78},
            {"min_level": 5, "max_level": 16, "label": "5–16", "duration_seconds": 60.0, "share_pct": 66.67},
            {"min_level": 17, "max_level": 22, "label": "17–22", "duration_seconds": 17.0, "share_pct": 18.89},
        ],
        "timeline": [
            {
                "t": 32.5,
                "start_seconds": 30.0,
                "end_seconds": 35.0,
                "is_warmup": False,
                "in_flight_avg": 10.42,
                "in_flight_max": 14,
                "arrival_count": 11,
                "arrival_rps": 2.2,
                "completed_count": 10,
                "completed_rps": 2.0,
            },
            {
                "t": 37.5,
                "start_seconds": 35.0,
                "end_seconds": 40.0,
                "is_warmup": False,
                "in_flight_avg": 12.15,
                "in_flight_max": 16,
                "arrival_count": 14,
                "arrival_rps": 2.8,
                "completed_count": 12,
                "completed_rps": 2.4,
            },
        ],
        "request_points": [
            {"t": 31.0, "isl": 1024, "osl": 128, "req_osl": 256, "ttft_ms": 60.0, "tpot_ms": 11.5, "ok": True},
            {"t": 33.0, "isl": 512, "osl": 256, "req_osl": 256, "ttft_ms": 50.0, "tpot_ms": 10.2, "ok": True},
            {"t": 35.0, "isl": 256, "osl": 512, "req_osl": 512, "ttft_ms": 42.0, "tpot_ms": 12.1, "ok": True},
            {"t": 36.0, "isl": 0, "osl": 32, "req_osl": 64, "ttft_ms": None, "tpot_ms": None, "ok": False},
        ],
        "request_points_total": total,
        "request_points_sampled": 4,
        "multi_turn": (
            {
                "available": True,
                "is_multi_turn": True,
                "reason": None,
                "session_count": 46,
                "max_turns": 5,
                "turns_per_session_mean": 4.0,
                "reusable_prefix_tokens": 118400,
                "reusable_prefix_pct": reusable_pct,
                "turns_distribution": [
                    {"turns": 1, "label": "1 turn", "sessions": 4, "share_pct": 8.7},
                    {"turns": 4, "label": "4 turns", "sessions": 42, "share_pct": 91.3},
                ],
                "isl_by_turn": [
                    {
                        "label": "Turn 1",
                        "min_turn": 1,
                        "max_turn": 1,
                        "request_count": 46,
                        "isl_p50": 420,
                        "isl_mean": 445.2,
                        "isl_p90": 810,
                        "reusable_prefix_pct": 0.0,
                    },
                    {
                        "label": "Turn 2",
                        "min_turn": 2,
                        "max_turn": 2,
                        "request_count": 42,
                        "isl_p50": 1180,
                        "isl_mean": 1210.5,
                        "isl_p90": 2150,
                        "reusable_prefix_pct": 54.2,
                    },
                ],
            }
            if is_multi_turn
            else {
                "available": True,
                "is_multi_turn": False,
                "reason": "Single-turn workload (all sessions had 1 turn).",
                "session_count": total,
                "max_turns": 1,
                "turns_per_session_mean": 1.0,
                "reusable_prefix_tokens": 12000 if reusable_pct is not None else None,
                "reusable_prefix_pct": reusable_pct,
                "turns_distribution": [{"turns": 1, "label": "1 turn", "sessions": total, "share_pct": 100.0}],
                "isl_by_turn": [],
            }
        ),
    }


def test_html_report_v33_workload_page_and_node_dom(sim_env: Path) -> None:
    """v33 offline report renders the Workload tab across profiling/full windows, multi-turn/single-turn, and legacy fallbacks."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in charts_js

    # Navigation button & subnav links appear right after Overview and before Bottlenecks
    ov_pos = template_html.find('data-page="overview"')
    wl_pos = template_html.find('data-page="workload"')
    bn_pos = template_html.find('data-page="bottlenecks"')
    assert 0 < ov_pos < wl_pos < bn_pos
    for anchor, subnav_label in (
        ("#w-config", "Configuration"),
        ("#w-kpis", "Summary"),
        ("#w-isl", "Input Sequence Length"),
        ("#w-osl", "Output Sequence Length"),
        ("#w-joint", "ISL vs OSL"),
        ("#w-conc", "Concurrency &amp; Arrival"),
        ("#w-multiturn", "Multi-Turn &amp; Prefix"),
    ):
        assert f'href="{anchor}" data-page="workload">{subnav_label}</a>' in template_html

    # Every Workload section, KPI, chart, and helpBadge title is registered in HELP_TEXTS & HELP_FORMULAS
    expected_workload_help_keys = (
        "w-config",
        "Dataset",
        "Replay Time Range",
        "Replay Duration & Warmup",
        "Pacing Mode",
        "Configured Concurrency",
        "Scale Factor",
        "w-kpis",
        "Requests & Tokens",
        "Input Sequence Length (ISL P50)",
        "Output Sequence Length (OSL P50)",
        "OSL / ISL Ratio (P50)",
        "In-Flight Concurrency (Mean)",
        "Mean Arrival Rate",
        "Workload Regime Mix (OSL / ISL Ratio)",
        "w-isl",
        "Request ISL Distribution",
        "Request ISL CDF",
        "w-osl",
        "Request OSL Distribution",
        "Request OSL CDF",
        "w-joint",
        "Input Sequence Length vs Output Sequence Length",
        "OSL by Input Sequence Length Bucket",
        "Decode Intensity (OSL / ISL Ratio) by Input Sequence Length",
        "w-conc",
        "In-Flight Concurrency Residency",
        "In-Flight Concurrency over Time",
        "Arrival vs Completion Rate over Time",
        "w-multiturn",
        "Turns per Session Distribution",
        "ISL Growth by Turn Depth",
    )
    for key in expected_workload_help_keys:
        assert charts_js.count(f'"{key}":') == 2, f"Expected HELP_TEXTS + HELP_FORMULAS entries for: {key}"

    # Compaction preserves workload and bounds oversized timeline & request_points lists
    oversized_tl = [
        {"t": float(i), "in_flight_avg": 4.0, "in_flight_max": 6, "arrival_rps": 2.0, "completed_rps": 2.0}
        for i in range(400)
    ]
    oversized_pts = [
        {
            "t": float(i),
            "isl": 100 + i,
            "osl": 50 + (i % 100),
            "req_osl": 200,
            "ttft_ms": 40.0,
            "tpot_ms": 10.0,
            "ok": True,
        }
        for i in range(2500)
    ]
    compacted = report_builder._compact_report_payload(
        {
            "client_series": {
                "workload": {
                    "fallback_to_full": False,
                    "fallback_synthesized": False,
                    "windows": {
                        "profiling": {
                            **_sample_workload_window(),
                            "timeline": list(oversized_tl),
                            "request_points": list(oversized_pts),
                            "request_points_total": 3000,
                            "request_points_sampled": 2500,
                        },
                        "full": {
                            **_sample_workload_window(total=220, completed=216, duration_seconds=120.0),
                            "timeline": list(oversized_tl),
                            "request_points": list(oversized_pts),
                            "request_points_total": True,  # invalid bool guarded cleanly
                            "request_points_sampled": 2500,
                        },
                    },
                }
            }
        },
        max_points=240,
    )
    comp_prof = compacted["client_series"]["workload"]["windows"]["profiling"]
    comp_full = compacted["client_series"]["workload"]["windows"]["full"]
    assert len(comp_prof["timeline"]) == 240
    assert len(comp_full["timeline"]) == 240
    assert len(comp_prof["request_points"]) == 2000
    assert comp_prof["request_points_sampled"] == 2000
    assert comp_prof["request_points_total"] == 3000
    assert comp_prof["request_points"][0]["t"] == 0.0
    assert comp_prof["request_points"][-1]["t"] == 2499.0
    assert len(comp_full["request_points"]) == 2000
    assert comp_full["request_points_sampled"] == 2000
    assert comp_full["request_points_total"] == 2500

    # 1. Rich multi-turn payload with distinct profiling vs full windows
    cs_rich = {
        **_sample_client_series(),
        "duration_seconds": 120.0,
        "workload": {
            "fallback_to_full": False,
            "fallback_synthesized": False,
            "windows": {
                "profiling": _sample_workload_window(
                    total=184, completed=180, failed=4, duration_seconds=90.0, isl_p50=1024, is_multi_turn=True
                ),
                "full": _sample_workload_window(
                    total=220, completed=214, failed=6, duration_seconds=120.0, isl_p50=1280, is_multi_turn=True
                ),
            },
        },
    }
    html_rich = render_html_report(
        _make_task("00000042", name="v33 Workload Rich"),
        client_series=cs_rich,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert "<!-- lens-report-v42 -->" in html_rich
    emb_rich = _extract_embedded_report_json(html_rich)

    # 2. Single-turn fallback-to-full payload
    cs_single = {
        **_sample_client_series(),
        "duration_seconds": 60.0,
        "workload": {
            "fallback_to_full": True,
            "fallback_synthesized": False,
            "windows": {
                "profiling": _sample_workload_window(
                    total=100, completed=98, failed=2, duration_seconds=60.0, is_multi_turn=False, reusable_pct=12.5
                ),
                "full": _sample_workload_window(
                    total=100, completed=98, failed=2, duration_seconds=60.0, is_multi_turn=False, reusable_pct=12.5
                ),
            },
        },
    }
    emb_single = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000043", name="v33 Workload Single-Turn"),
            client_series=cs_single,
            server_telemetry={"available": False},
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    # 3. Legacy payload lacking workload -> render_html_report synthesizes fallback automatically
    cs_legacy = {
        **_sample_client_series(),
        "duration_seconds": 60.0,
        "buckets": [
            {
                "t": 2.5,
                "start_seconds": 0.0,
                "end_seconds": 5.0,
                "mid_seconds": 2.5,
                "is_warmup": True,
                "arrived_requests": 40,
                "arrived_rps": 8.0,
                "completed_requests": 38,
                "throughput_rps": 7.6,
                "in_flight_concurrency": 4.5,
            },
            {
                "t": 7.5,
                "start_seconds": 5.0,
                "end_seconds": 10.0,
                "mid_seconds": 7.5,
                "is_warmup": False,
                "arrived_requests": 50,
                "arrived_rps": 10.0,
                "completed_requests": 50,
                "throughput_rps": 10.0,
                "in_flight_concurrency": 6.25,
            },
        ],
        "prompt_length_buckets": [
            {"label": "<512", "min_tokens": 0, "max_tokens": 512, "count": 30, "share_pct": 30.0},
            {"label": "512-2K", "min_tokens": 512, "max_tokens": 2048, "count": 70, "share_pct": 70.0},
        ],
    }
    emb_legacy = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000044", name="v33 Workload Legacy Synthesis"),
            client_series=cs_legacy,
            server_telemetry={"available": False},
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )
    assert emb_legacy["client_series"]["workload"]["fallback_synthesized"] is True

    # 4. Payload with workload stripped (verifies JS empty-state guard)
    emb_missing = {**emb_legacy, "client_series": _sample_client_series()}

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    tagName: tag.toUpperCase(),
    id: "",
    children: [],
    attributes: {},
    style: {},
    className: "",
    disabled: false,
    classList: {
      add(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.add(c); this._owner.className = Array.from(s).join(" "); },
      remove(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.delete(c); this._owner.className = Array.from(s).join(" "); },
      toggle(c, force) {
        const has = this.contains(c);
        const next = force !== undefined ? Boolean(force) : !has;
        if (next) this.add(c); else this.remove(c);
      },
      contains(c) { return (this._owner.className || "").split(/\s+/).includes(c); },
      _owner: null,
    },
    textContent: "",
    innerText: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
      if (k === "disabled") this.disabled = true;
    },
    removeAttribute(k) {
      delete this.attributes[k];
      if (k === "disabled") this.disabled = false;
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); },
    closest(sel) {
      let cur = this;
      while (cur) {
        if (sel === "button[data-window]" && cur.tagName === "BUTTON" && cur.getAttribute("data-window") !== null) return cur;
        cur = cur.parentNode;
      }
      return null;
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const mk = (tag) => { const n = makeNode(tag); n.classList._owner = n; return n; };
  const tooltipNode = mk("div");
  const winToggle = mk("div");
  const btnProf = mk("button"); btnProf.setAttribute("data-window", "profiling"); btnProf.className = "active";
  const btnFull = mk("button"); btnFull.setAttribute("data-window", "full");
  winToggle.append(btnProf, btnFull);
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": mk("main"),
    "report-title": mk("span"),
    "report-meta": mk("div"),
    "status-badges": mk("span"),
    "lens-report-data": (() => { const s = mk("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": mk("button"),
    "sidebar": mk("aside"),
    "theme-toggle": mk("button"),
    "print-btn": mk("button"),
    "window-toggle": winToggle,
    "nav-list": mk("nav"),
  };
  const doc = {
    documentElement: mk("html"),
    body: mk("body"),
    createElement: (t) => mk(t),
    createElementNS: (_ns, t) => mk(t),
    createTextNode: (t) => { const n = mk("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || mk("div"),
    querySelectorAll: (sel) => {
      if (sel === "#window-toggle button") return [btnProf, btnFull];
      if (sel === ".report-page") return ids["report-root"].children;
      return [];
    },
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tooltip: tooltipNode, winToggle, btnFull };
}

(async () => {
  // 1. Rich multi-turn payload
  const resRich = await runCase(cases.rich);
  const pageIds = resRich.root.children.map((n) => n.getAttribute("data-page"));
  assert.strictEqual(
    JSON.stringify(pageIds),
    JSON.stringify(["overview", "workload", "bottlenecks", "serving", "requests", "system", "hardware", "config"]),
    `Unexpected report page ordering: ${JSON.stringify(pageIds)}`
  );
  const wPage = resRich.root.children.find((n) => n.getAttribute("data-page") === "workload");
  const wText = nodeText(wPage);
  assert(!wText.includes("NaN") && !wText.includes("undefined"), `Found NaN/undefined in Workload page: ${wText}`);
  for (const secId of ["w-config", "w-kpis", "w-isl", "w-osl", "w-joint", "w-conc", "w-multiturn"]) {
    assert(findAll(wPage, (n) => n.getAttribute("id") === secId).length === 1, `Missing section #${secId}`);
  }
  assert(wText.includes("184") && wText.includes("1,024 tok") && wText.includes("11.43 req"), "Missing formatted KPIs in profiling window");
  assert(wText.includes("Workload Regime Mix (OSL / ISL Ratio)"), "Missing regime mix bar");

  // Toggle ISL histogram Count -> Share %
  const wIsl = findAll(wPage, (n) => n.getAttribute("id") === "w-isl")[0];
  const shareBtn = findAll(wIsl, (n) => n.tagName === "BUTTON" && n.getAttribute("data-hist-mode") === "share_pct")[0];
  assert(shareBtn, "Missing Share % toggle on #w-isl");
  shareBtn.dispatchEvent("click", {});
  assert.strictEqual(shareBtn.getAttribute("aria-pressed"), "true");
  assert(nodeText(wIsl).includes("% of Requests"), "ISL histogram Y-axis must switch to % of Requests");

  // Check #w-joint ISL vs OSL scatter SVG, diagonal line, regime legend, downsampling note, and hover tooltip
  const wJoint = findAll(wPage, (n) => n.getAttribute("id") === "w-joint")[0];
  const jointSvgs = findAll(wJoint, (n) => n.tagName === "SVG");
  assert.strictEqual(jointSvgs.length, 3, "Expected 3 SVGs in #w-joint (scatter + 2 bucket bar charts)");
  const scatterSvg = jointSvgs[0];
  const scatterCircles = findAll(scatterSvg, (n) => n.tagName === "CIRCLE");
  assert.strictEqual(scatterCircles.length, 4, `Expected 4 scatter circles (including isl===0), got ${scatterCircles.length}`);
  assert(nodeText(wJoint).includes("OSL = ISL (1:1)"), "Missing OSL = ISL (1:1) reference line label");
  assert(nodeText(wJoint).includes("Prefill-Heavy (OSL / ISL < 0.25)"), "Missing Prefill-Heavy scatter legend");
  assert(nodeText(wJoint).includes("Showing 4 of 184 requests"), "Missing scatter downsampling note");
  const scatterOverlay = findAll(scatterSvg, (n) => n.tagName === "RECT")[0];
  assert(scatterOverlay, "Missing scatter hover overlay rect");
  scatterOverlay.dispatchEvent("mousemove", { clientX: 60, clientY: 230 });
  const scatterTipText = nodeText(resRich.tooltip);
  assert(
    scatterTipText.includes("Input Sequence Length") && scatterTipText.includes("Output Sequence Length") && scatterTipText.includes("OSL / ISL Ratio"),
    `Unexpected scatter hover tooltip: ${scatterTipText}`
  );
  assert(!scatterTipText.includes("Total") && !scatterTipText.includes("NaN"), `Scatter tooltip must not include Total or NaN: ${scatterTipText}`);

  // Check residency bar tooltip formats duration with 2-decimal secFmt (0.60 s)
  const wConc = findAll(wPage, (n) => n.getAttribute("id") === "w-conc")[0];
  const concSvgs = findAll(wConc, (n) => n.tagName === "SVG");
  assert.strictEqual(concSvgs.length, 3, "Expected 3 SVGs in #w-conc (residency + concurrency timeline + rate timeline)");
  const resRects = findAll(concSvgs[0], (n) => n.tagName === "RECT");
  resRects[0].dispatchEvent("mousemove", { clientX: 80, clientY: 80 });
  assert(nodeText(resRich.tooltip).includes("0.60 s"), `Expected 2-decimal residency duration in tooltip, got: ${nodeText(resRich.tooltip)}`);

  // Check every .help-icon on Workload page has non-empty prose + formula and zero forbidden engine names
  const forbiddenRe = /(\bvllm\b|\bsglang\b|\btrt-?llm\b|\baiperf\b|\bprometheus\b|\bGPU\b)/i;
  const wIcons = findAll(wPage, (n) => hasClass(n, "help-icon"));
  assert(wIcons.length >= 20, `Expected >= 20 help icons on Workload page, got ${wIcons.length}`);
  for (const icon of wIcons) {
    icon.dispatchEvent("mouseenter", { clientX: 40, clientY: 40 });
    const bodyEl = findAll(resRich.tooltip, (n) => hasClass(n, "help-tip-body"))[0];
    const formEl = findAll(resRich.tooltip, (n) => hasClass(n, "help-tip-formula"))[0];
    assert(bodyEl && nodeText(bodyEl).length > 0, `Empty help prose for ${icon.getAttribute("aria-label")}`);
    assert(formEl && nodeText(formEl).length > 0, `Empty help formula for ${icon.getAttribute("aria-label")}`);
    assert(!forbiddenRe.test(nodeText(bodyEl)), `Forbidden term in help prose: ${nodeText(bodyEl)}`);
    assert(!forbiddenRe.test(nodeText(formEl)), `Forbidden term in help formula: ${nodeText(formEl)}`);
  }

  // Switch window toggle from profiling -> full and verify Workload page (including scatter note) updates
  resRich.winToggle.dispatchEvent("click", { target: resRich.btnFull });
  const wPageFull = resRich.root.children.find((n) => n.getAttribute("data-page") === "workload");
  const wFullText = nodeText(wPageFull);
  assert(wFullText.includes("Window: Full Run") && wFullText.includes("220") && wFullText.includes("1,280 tok"), `Window switch to full failed: ${wFullText}`);
  assert(wFullText.includes("Showing 4 of 220 requests"), `Scatter note did not update on full window toggle: ${wFullText}`);

  // 2. Single-turn + fallback_to_full payload
  const resSingle = await runCase(cases.single);
  const wSingle = resSingle.root.children.find((n) => n.getAttribute("data-page") === "workload");
  const wSingleText = nodeText(wSingle);
  assert(wSingleText.includes("displaying full-run workload metrics"), "Missing fallback_to_full notice");
  assert(wSingleText.includes("Single-turn workload"), "Missing single-turn reason note");
  assert(wSingleText.includes("12.50%"), "Missing reusable prefix share on single-turn payload");

  // 3. Legacy synthesized fallback payload renders empty state for scatter chart
  const resLegacy = await runCase(cases.legacy);
  const wLegacy = resLegacy.root.children.find((n) => n.getAttribute("data-page") === "workload");
  const wLegacyText = nodeText(wLegacy);
  assert(!wLegacyText.includes("NaN") && !wLegacyText.includes("undefined"), `Legacy workload had NaN/undefined: ${wLegacyText}`);
  assert(wLegacyText.includes("Synthesized from aggregate run telemetry"), "Missing legacy synthesized notice");
  assert(wLegacyText.includes("Per-request Input vs Output Sequence Length points require raw request export."), "Missing scatter empty-state note on legacy fallback");

  // Switch legacy fallback to full window so full-run prompt_length_buckets share strip renders
  resLegacy.winToggle.dispatchEvent("click", { target: resLegacy.btnFull });
  const wLegacyFull = resLegacy.root.children.find((n) => n.getAttribute("data-page") === "workload");
  const wLegacyFullText = nodeText(wLegacyFull);
  assert(!wLegacyFullText.includes("NaN") && !wLegacyFullText.includes("undefined"), `Legacy full workload had NaN/undefined: ${wLegacyFullText}`);
  assert(wLegacyFullText.includes("Window: Full Run"), "Legacy window switch to full failed");
  assert(
    wLegacyFullText.includes("<512") && wLegacyFullText.includes("30%") && wLegacyFullText.includes("512-2K") && wLegacyFullText.includes("70%"),
    `Missing prompt-length share strip on legacy full fallback: ${wLegacyFullText}`
  );

  // 4. Missing workload payload renders clean empty-note
  const resMissing = await runCase(cases.missing);
  const wMissing = resMissing.root.children.find((n) => n.getAttribute("data-page") === "workload");
  assert(nodeText(wMissing).includes("Workload distribution telemetry is unavailable"), "Missing empty-note when workload is absent");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"rich": emb_rich, "single": emb_single, "legacy": emb_legacy, "missing": emb_missing}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node v33 Workload DOM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


@pytest.mark.asyncio
async def test_v33_bundle_refresh_synthesizes_missing_workload_without_raw_records(
    sim_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy bundle refresh synthesizes workload into client-series.json while preserving schema_version < 12."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    task = _make_task("00000045", name="v33 Legacy Bundle Synthesis")
    report_dir = Path(task.task_dir) / "artifacts" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    legacy_cs = _sample_client_series()
    legacy_cs["schema_version"] = 11
    legacy_cs["duration_seconds"] = 30.0
    assert "workload" not in legacy_cs
    (report_dir / "client-series.json").write_text(json.dumps(legacy_cs), encoding="utf-8")
    (report_dir / "forensics.json").write_text(
        json.dumps({"schema_version": 8, "verdict": {"code": "healthy"}, "telemetry_scope": "client_only"}),
        encoding="utf-8",
    )
    (report_dir / "index.html").write_text("<html><body>legacy-v32</body></html>", encoding="utf-8")
    await save_task(task)

    fake_template = (
        f"<!doctype html><html><head>{report_builder.REPORT_TEMPLATE_VERSION_MARKER}"
        "<title><!--__LENS_REPORT_TITLE__--></title></head>"
        '<body><script id="lens-report-data"><!--__LENS_REPORT_DATA__--></script>'
        "<script>/*__LENS_REPORT_CHARTS_JS__*/</script></body></html>"
    )
    monkeypatch.setattr(report_builder, "_load_reporting_assets", lambda: (fake_template, "window.__CHARTS_OK__=1;"))

    bundle_path = build_bundle_tar_gz(task)
    with tarfile.open(bundle_path, "r:gz") as tar:
        bundled_cs = json.loads(tar.extractfile("aiperf-00000045/timeseries/client-series.json").read().decode("utf-8"))
        bundled_html = tar.extractfile("aiperf-00000045/report/index.html").read().decode("utf-8")
    assert bundled_cs["schema_version"] == 11
    assert bundled_cs["workload"]["fallback_synthesized"] is True
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER in bundled_html


def test_html_report_v34_per_node_and_per_nic_system_page(sim_env: Path) -> None:
    """v34 offline report supports synchronized per-node System charts, per-NIC Network RX/TX, select persistence, and legacy fallback."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in charts_js
        assert forbidden not in template_html

    # Zero raw .toFixed( on displayed values inside binary helpers, renderSystem and its per-NIC helpers
    bin_start = charts_js.index("const BINARY_POWER_STEP =")
    bin_end = charts_js.index("function computeKvTierShareData")
    assert ".toFixed(" not in charts_js[bin_start:bin_end]
    sys_start = charts_js.index("const BYTES_PER_GIB =")
    sys_end = charts_js.index("const LOG_ANSI_RE =")
    assert ".toFixed(" not in charts_js[sys_start:sys_end]

    # Required v34 System help entries exist in BOTH HELP_TEXTS and HELP_FORMULAS with zero GPU/engine leaks
    for key in ("Charts Node Scope", "Network Interface", "y-net", "Network RX / TX Throughput"):
        assert charts_js.count(f'"{key}":') == 2, f"Expected HELP_TEXTS + HELP_FORMULAS entries for: {key}"
    for block_name in ("HELP_TEXTS", "HELP_FORMULAS"):
        m = re.search(rf"const {block_name} = \{{(?P<body>.*?)\n  \}};", charts_js, re.DOTALL)
        assert m is not None
        for line in m.group("body").splitlines():
            if ":" not in line:
                continue
            key_part, val_part = line.split(":", 1)
            assert "GPU" not in key_part, f"Forbidden GPU in help key: {line!r}"
            assert not re.search(r"\b(vllm|sglang|trtllm|gpu)\b", val_part, re.IGNORECASE), (
                f"{block_name} value leaked engine/GPU wording: {line!r}"
            )

    # _compact_report_payload preserves and caps network_interfaces (<=16 NICs, <=240 pts)
    oversized_nics_summary = [
        {
            "device": f"eth{i}",
            "rx_bytes_per_second": {"mean": float((20 - i) * 1024**2), "max": float((25 - i) * 1024**2)},
            "tx_bytes_per_second": {"mean": float((20 - i) * 512**2), "max": float((25 - i) * 512**2)},
        }
        for i in range(20)
    ]
    oversized_nic_map = {
        f"eth{i}": {"rx": float((20 - i) * 1024**2), "tx": float((20 - i) * 512**2)} for i in range(20)
    }
    oversized_pts = [
        {
            "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
            "cpu_usage_percent": 40.0 + (i % 10),
            "network_receive_bytes_per_second": 50.0 * 1024**2,
            "network_transmit_bytes_per_second": 25.0 * 1024**2,
            "network_interfaces": dict(oversized_nic_map),
        }
        for i in range(300)
    ]
    compacted = report_builder._compact_report_payload(
        {
            "server_telemetry": {
                "series": list(oversized_pts),
                "per_node": [
                    {"node": "node-1", "network_interfaces": list(oversized_nics_summary)},
                ],
                "per_node_series": {"node-1": list(oversized_pts)},
                "system": {
                    "available": True,
                    "nodes": ["node-1"],
                    "network_interfaces": list(oversized_nics_summary),
                    "series": list(oversized_pts),
                },
            }
        },
        max_points=240,
    )
    cst = compacted["server_telemetry"]
    assert len(cst["series"]) == 240
    assert len(cst["system"]["series"]) == 240
    assert len(cst["per_node_series"]["node-1"]) == 240
    assert len(cst["per_node"][0]["network_interfaces"]) == 16
    assert len(cst["system"]["network_interfaces"]) == 16
    assert cst["per_node"][0]["network_interfaces"][0]["device"] == "eth0"
    assert len(cst["series"][0]["network_interfaces"]) == 16
    assert len(cst["system"]["series"][0]["network_interfaces"]) == 16
    assert len(cst["per_node_series"]["node-1"][0]["network_interfaces"]) == 16

    # Build 3 HTML report payloads for Node DOM harness verification
    def _mk_nic_summary(devs: list[str], scale: float = 1.0) -> list[dict[str, object]]:
        out = []
        for idx, dev in enumerate(devs):
            base = float((len(devs) - idx) * 10 * 1024**2) * scale
            out.append(
                {
                    "device": dev,
                    "rx_bytes_per_second": {"mean": base, "max": base * 1.4},
                    "tx_bytes_per_second": {"mean": base * 0.5, "max": base * 0.7},
                }
            )
        return out

    def _mk_nic_pt_map(devs: list[str], scale: float = 1.0) -> dict[str, dict[str, float]]:
        return {
            dev: {
                "rx": float((len(devs) - idx) * 10 * 1024**2) * scale,
                "tx": float((len(devs) - idx) * 5 * 1024**2) * scale,
            }
            for idx, dev in enumerate(devs)
        }

    # Case 1: 2 nodes, 3 NICs on node-1 (including dotted VLAN subinterface eno1.100), 2 NICs on node-2
    n1_devs = ["ib0", "eno1", "eno1.100"]
    n2_devs = ["ib0", "eno1"]
    st_rich2 = {
        **_sample_server_telemetry(),
        "system": {
            "available": True,
            "nodes": ["node-1", "node-2"],
            "cpu_avg_pct": 52.0,
            "cpu_peak_pct": 80.0,
            "net_rx_bps": 90 * 1024**2,
            "net_tx_bps": 45 * 1024**2,
            "network_interfaces": _mk_nic_summary(["ib0", "eno1", "eno1.100"], scale=1.5),
            "series": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "host_cpu_usage_percent": 48.0,
                    "host_memory_used_bytes": 90 * 1024**3,
                    "host_memory_available_bytes": 166 * 1024**3,
                    "host_disk_read_bytes_per_second": 10 * 1024**2,
                    "host_disk_written_bytes_per_second": 4 * 1024**2,
                    "host_network_receive_bytes_per_second": 85 * 1024**2,
                    "host_network_transmit_bytes_per_second": 42 * 1024**2,
                    "host_power_watts": 760.0,
                    "network_interfaces": _mk_nic_pt_map(["ib0", "eno1", "eno1.100"], scale=1.5),
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "host_cpu_usage_percent": 56.0,
                    "host_memory_used_bytes": 94 * 1024**3,
                    "host_memory_available_bytes": 162 * 1024**3,
                    "host_disk_read_bytes_per_second": 12 * 1024**2,
                    "host_disk_written_bytes_per_second": 5 * 1024**2,
                    "host_network_receive_bytes_per_second": 95 * 1024**2,
                    "host_network_transmit_bytes_per_second": 48 * 1024**2,
                    "host_power_watts": 800.0,
                    "network_interfaces": _mk_nic_pt_map(["ib0", "eno1", "eno1.100"], scale=1.7),
                },
            ],
        },
        "per_node": [
            {
                "node": "node-1",
                "roles": ["worker"],
                "pod_count": 1,
                "cpu_usage_percent": {"mean": 60.0, "max": 80.0},
                "network_receive_bytes_per_second": {"mean": 60 * 1024**2, "max": 80 * 1024**2},
                "network_transmit_bytes_per_second": {"mean": 30 * 1024**2, "max": 40 * 1024**2},
                "network_interfaces": _mk_nic_summary(n1_devs, scale=1.0),
            },
            {
                "node": "node-2",
                "roles": ["worker"],
                "pod_count": 1,
                "cpu_usage_percent": {"mean": 44.0, "max": 68.0},
                "network_receive_bytes_per_second": {"mean": 30 * 1024**2, "max": 45 * 1024**2},
                "network_transmit_bytes_per_second": {"mean": 15 * 1024**2, "max": 22 * 1024**2},
                "network_interfaces": _mk_nic_summary(n2_devs, scale=0.8),
            },
        ],
        "per_node_series": {
            "node-1": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 58.0,
                    "memory_used_bytes": 50 * 1024**3,
                    "memory_available_bytes": 78 * 1024**3,
                    "disk_read_bytes_per_second": 6 * 1024**2,
                    "disk_written_bytes_per_second": 2 * 1024**2,
                    "network_receive_bytes_per_second": 58 * 1024**2,
                    "network_transmit_bytes_per_second": 29 * 1024**2,
                    "host_power_watts": 410.0,
                    "gpu_power_watts": 280.0,
                    "network_interfaces": _mk_nic_pt_map(n1_devs, scale=1.0),
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "cpu_usage_percent": 62.0,
                    "memory_used_bytes": 52 * 1024**3,
                    "memory_available_bytes": 76 * 1024**3,
                    "disk_read_bytes_per_second": 8 * 1024**2,
                    "disk_written_bytes_per_second": 3 * 1024**2,
                    "network_receive_bytes_per_second": 62 * 1024**2,
                    "network_transmit_bytes_per_second": 31 * 1024**2,
                    "host_power_watts": 430.0,
                    "gpu_power_watts": 300.0,
                    "network_interfaces": _mk_nic_pt_map(n1_devs, scale=1.1),
                },
            ],
            "node-2": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 42.0,
                    "memory_used_bytes": 40 * 1024**3,
                    "memory_available_bytes": 88 * 1024**3,
                    "disk_read_bytes_per_second": 4 * 1024**2,
                    "disk_written_bytes_per_second": 2 * 1024**2,
                    "network_receive_bytes_per_second": 27 * 1024**2,
                    "network_transmit_bytes_per_second": 13 * 1024**2,
                    "host_power_watts": 350.0,
                    "gpu_power_watts": 230.0,
                    "network_interfaces": _mk_nic_pt_map(n2_devs, scale=0.8),
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "cpu_usage_percent": 46.0,
                    "memory_used_bytes": 42 * 1024**3,
                    "memory_available_bytes": 86 * 1024**3,
                    "disk_read_bytes_per_second": 4 * 1024**2,
                    "disk_written_bytes_per_second": 2 * 1024**2,
                    "network_receive_bytes_per_second": 33 * 1024**2,
                    "network_transmit_bytes_per_second": 17 * 1024**2,
                    "host_power_watts": 370.0,
                    "gpu_power_watts": 250.0,
                    "network_interfaces": _mk_nic_pt_map(n2_devs, scale=0.9),
                },
            ],
        },
    }
    emb_rich2 = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000046", name="v34 System Per-Node Per-NIC"),
            client_series=_sample_client_series(),
            server_telemetry=st_rich2,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    # Case 2: 6 nodes (>5 -> <select> for Scope & Charts Scope, clickable Target Nodes pills still present) & 7 NICs (>3 -> <select>, >5 -> top 4 + Other)
    many_nodes = [f"node-{i}" for i in range(1, 7)]
    many_devs = [f"nic{i}" for i in range(7)]
    st_many = {
        **_sample_server_telemetry(),
        "system": {
            "available": True,
            "nodes": many_nodes,
            "cpu_avg_pct": 50.0,
            "net_rx_bps": 100 * 1024**2,
            "net_tx_bps": 50 * 1024**2,
            "network_interfaces": _mk_nic_summary(many_devs, scale=2.0),
            "series": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "host_cpu_usage_percent": 50.0,
                    "host_network_receive_bytes_per_second": 100 * 1024**2,
                    "host_network_transmit_bytes_per_second": 50 * 1024**2,
                    "network_interfaces": _mk_nic_pt_map(many_devs, scale=2.0),
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "host_cpu_usage_percent": 55.0,
                    "host_network_receive_bytes_per_second": 110 * 1024**2,
                    "host_network_transmit_bytes_per_second": 55 * 1024**2,
                    "network_interfaces": _mk_nic_pt_map(many_devs, scale=2.2),
                },
            ],
        },
        "per_node": [
            {
                "node": n,
                "roles": ["worker"],
                "pod_count": 1,
                "cpu_usage_percent": {"mean": 45.0 + idx, "max": 70.0 + idx},
                "network_interfaces": _mk_nic_summary(many_devs, scale=0.5 + idx * 0.1),
            }
            for idx, n in enumerate(many_nodes)
        ],
        "per_node_series": {
            n: [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 45.0 + idx,
                    "network_receive_bytes_per_second": 20 * 1024**2,
                    "network_transmit_bytes_per_second": 10 * 1024**2,
                    "network_interfaces": _mk_nic_pt_map(many_devs, scale=0.5 + idx * 0.1),
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "cpu_usage_percent": 49.0 + idx,
                    "network_receive_bytes_per_second": 22 * 1024**2,
                    "network_transmit_bytes_per_second": 11 * 1024**2,
                    "network_interfaces": _mk_nic_pt_map(many_devs, scale=0.6 + idx * 0.1),
                },
            ]
            for idx, n in enumerate(many_nodes)
        },
    }
    emb_many = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000047", name="v34 System Many Nodes Many NICs"),
            client_series=_sample_client_series(),
            server_telemetry=st_many,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    # Case 3: Legacy v<=9 snapshot lacking network_interfaces
    st_legacy = {
        **_sample_server_telemetry(),
        "system": {
            "available": True,
            "nodes": ["node-legacy"],
            "cpu_avg_pct": 35.0,
            "net_rx_bps": 20 * 1024**2,
            "net_tx_bps": 10 * 1024**2,
            "series": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "host_cpu_usage_percent": 35.0,
                    "host_network_receive_bytes_per_second": 20 * 1024**2,
                    "host_network_transmit_bytes_per_second": 10 * 1024**2,
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "host_cpu_usage_percent": 38.0,
                    "host_network_receive_bytes_per_second": 22 * 1024**2,
                    "host_network_transmit_bytes_per_second": 12 * 1024**2,
                },
            ],
        },
        "per_node": [
            {
                "node": "node-legacy",
                "roles": ["worker"],
                "pod_count": 1,
                "cpu_usage_percent": {"mean": 35.0, "max": 38.0},
                "network_receive_bytes_per_second": {"mean": 20 * 1024**2, "max": 22 * 1024**2},
                "network_transmit_bytes_per_second": {"mean": 10 * 1024**2, "max": 12 * 1024**2},
            }
        ],
    }
    emb_legacy = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000048", name="v34 System Legacy No NIC"),
            client_series=_sample_client_series(),
            server_telemetry=st_legacy,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    # Case 4: Untrusted prototype-colliding node & device names ("__proto__", "constructor")
    st_proto = {
        **_sample_server_telemetry(),
        "system": {
            "available": True,
            "nodes": ["__proto__", "constructor"],
            "cpu_avg_pct": 30.0,
            "net_rx_bps": 18 * 1024**2,
            "net_tx_bps": 9 * 1024**2,
            "network_interfaces": _mk_nic_summary(["__proto__", "constructor", "eth0"], scale=1.0),
            "series": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "host_cpu_usage_percent": 30.0,
                    "host_network_receive_bytes_per_second": 18 * 1024**2,
                    "host_network_transmit_bytes_per_second": 9 * 1024**2,
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "host_cpu_usage_percent": 32.0,
                    "host_network_receive_bytes_per_second": 20 * 1024**2,
                    "host_network_transmit_bytes_per_second": 10 * 1024**2,
                },
            ],
        },
        "per_node": [
            {
                "node": "__proto__",
                "roles": ["worker"],
                "pod_count": 1,
                "cpu_usage_percent": {"mean": 28.0, "max": 31.0},
                "network_interfaces": _mk_nic_summary(["__proto__", "constructor"], scale=1.0),
            },
            {
                "node": "constructor",
                "roles": ["worker"],
                "pod_count": 1,
                "cpu_usage_percent": {"mean": 32.0, "max": 34.0},
                "network_interfaces": _mk_nic_summary(["eth0"], scale=0.5),
            },
        ],
        "per_node_series": {
            "__proto__": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 28.0,
                    "network_interfaces": _mk_nic_pt_map(["__proto__", "constructor"], scale=1.0),
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "cpu_usage_percent": 31.0,
                    "network_interfaces": _mk_nic_pt_map(["__proto__", "constructor"], scale=1.1),
                },
            ],
        },
    }
    emb_proto = _extract_embedded_report_json(
        render_html_report(
            _make_task("00000049", name="v34 System Prototype Collision"),
            client_series=_sample_client_series(),
            server_telemetry=st_proto,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    tagName: tag.toUpperCase(),
    id: "",
    value: "",
    selected: false,
    children: [],
    attributes: {},
    style: {},
    className: "",
    disabled: false,
    classList: {
      add(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.add(c); this._owner.className = Array.from(s).join(" "); },
      remove(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.delete(c); this._owner.className = Array.from(s).join(" "); },
      toggle(c, force) {
        const has = this.contains(c);
        const next = force !== undefined ? Boolean(force) : !has;
        if (next) this.add(c); else this.remove(c);
      },
      contains(c) { return (this._owner.className || "").split(/\s+/).includes(c); },
      _owner: null,
    },
    textContent: "",
    innerText: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
      if (k === "disabled") this.disabled = true;
    },
    removeAttribute(k) {
      delete this.attributes[k];
      if (k === "disabled") this.disabled = false;
    },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this, stopPropagation() {}, preventDefault() {} }); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); },
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const mk = (tag) => { const n = makeNode(tag); n.classList._owner = n; return n; };
  const tooltipNode = mk("div");
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": mk("main"),
    "report-title": mk("span"),
    "report-meta": mk("div"),
    "status-badges": mk("span"),
    "lens-report-data": (() => { const s = mk("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": mk("button"),
    "sidebar": mk("aside"),
    "theme-toggle": mk("button"),
    "print-btn": mk("button"),
    "window-toggle": mk("div"),
    "nav-list": mk("nav"),
  };
  const doc = {
    documentElement: mk("html"),
    body: mk("body"),
    createElement: (t) => mk(t),
    createElementNS: (_ns, t) => mk(t),
    createTextNode: (t) => { const n = mk("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || mk("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tooltip: tooltipNode };
}

(async () => {
  const ALLOWED_PALETTE_5 = new Set(["#4c9aff", "#2ec27e", "#f5a623", "#a78bfa", "#ef5350"]);
  const forbiddenRe = /(\bvllm\b|\bsglang\b|\btrt-?llm\b|\baiperf\b|\bprometheus\b|\bGPU\b)/i;

  // Case 1: 2 nodes, 3 NICs (including eno1.100 on node-1 only)
  const r1 = await runCase(cases.rich2);
  const sys1 = () => r1.root.children.find((n) => n.getAttribute("data-page") === "system");
  assert(sys1(), "System page missing");

  // Verify Target Nodes clickable pills, Scope bar, and Charts Scope bar placed directly above y-cpu
  const childIds1 = sys1().children.map((c) => c.getAttribute("id") || c.getAttribute("data-charts-scope") || "");
  const nodesIdx = childIds1.indexOf("y-nodes");
  const csIdx = childIds1.indexOf("true");
  const cpuIdx = childIds1.indexOf("y-cpu");
  assert(nodesIdx >= 0 && csIdx === nodesIdx + 1 && cpuIdx === csIdx + 1, `Expected Charts Scope right between y-nodes and y-cpu, got ${JSON.stringify(childIds1)}`);
  assert(nodeText(sys1()).includes("Target Nodes:"), "Missing Target Nodes row");
  assert(nodeText(sys1()).includes("Charts Scope:"), "Missing Charts Scope bar");

  // Click Target Nodes pill node-1 -> scoped; click again -> toggles back to All Nodes (Aggregate)
  const targetNode1Pill = findAll(sys1(), (n) => n.tagName === "BUTTON" && n.textContent === "node-1" && n.getAttribute("aria-pressed") !== null)[0];
  assert(targetNode1Pill, "Missing clickable Target Nodes pill for node-1");
  targetNode1Pill.dispatchEvent("click");
  assert(nodeText(sys1()).includes("Scoped to node node-1"), "Clicking Target Nodes pill did not scope to node-1");
  assert(nodeText(findAll(sys1(), (n) => n.getAttribute("id") === "y-mem-disk")[0]).includes("Node node-1:"), "y-mem-disk subtitle did not update to node-1");
  const targetNode1PillActive = findAll(sys1(), (n) => n.tagName === "BUTTON" && n.textContent === "node-1" && n.getAttribute("aria-pressed") === "true")[0];
  assert(targetNode1PillActive, "Active Target Nodes pill must have aria-pressed=true");
  targetNode1PillActive.dispatchEvent("click");
  assert(!nodeText(sys1()).includes("Scoped to node"), "Second click on Target Nodes pill did not toggle back to aggregate");

  // Click Charts Scope pill node-1 -> select dotted NIC eno1.100
  const chartsScopeBar = findAll(sys1(), (n) => n.getAttribute("data-charts-scope") === "true")[0];
  const csNode1Btn = findAll(chartsScopeBar, (n) => n.tagName === "BUTTON" && n.textContent === "node-1")[0];
  csNode1Btn.dispatchEvent("click");
  assert.strictEqual(sys1()._systemSubs.length, 4, "Expected exactly 4 crosshair subscribers");
  const yNet1 = findAll(sys1(), (n) => n.getAttribute("id") === "y-net")[0];
  const vlanBtn = findAll(yNet1, (n) => n.tagName === "BUTTON" && n.textContent === "eno1.100")[0];
  assert(vlanBtn, "Missing eno1.100 pill in y-net");
  vlanBtn.dispatchEvent("click");
  const yNetVlanText = nodeText(findAll(sys1(), (n) => n.getAttribute("id") === "y-net")[0]);
  assert(yNetVlanText.includes("eno1.100 RX (MiB/s)") && yNetVlanText.includes("eno1.100 TX (MiB/s)"), `Dotted NIC series missing: ${yNetVlanText}`);

  // Switch to node-2 (which lacks eno1.100) -> stale NIC must auto-reset to All NICs (Total)
  const csBar2 = findAll(sys1(), (n) => n.getAttribute("data-charts-scope") === "true")[0];
  findAll(csBar2, (n) => n.tagName === "BUTTON" && n.textContent === "node-2")[0].dispatchEvent("click");
  const yNetAfterReset = findAll(sys1(), (n) => n.getAttribute("id") === "y-net")[0];
  const yNetResetText = nodeText(yNetAfterReset);
  assert(yNetResetText.includes("Network RX (MiB/s)") && yNetResetText.includes("Network TX (MiB/s)"), `Stale NIC did not reset to All NICs: ${yNetResetText}`);

  // Switch to By NIC -> verify solid RX + dashed TX paths inside PALETTE[0..4]
  findAll(yNetAfterReset, (n) => n.tagName === "BUTTON" && n.textContent === "By NIC")[0].dispatchEvent("click");
  const yNetByNic = findAll(sys1(), (n) => n.getAttribute("id") === "y-net")[0];
  const byNicPaths = findAll(yNetByNic, (n) => n.tagName === "PATH");
  assert(byNicPaths.some((p) => !p.getAttribute("stroke-dasharray")), "Expected solid RX paths in By NIC mode");
  assert(byNicPaths.some((p) => p.getAttribute("stroke-dasharray") === "4 3"), "Expected dashed TX paths in By NIC mode");
  for (const p of byNicPaths) {
    assert(ALLOWED_PALETTE_5.has(p.getAttribute("stroke")), `By NIC color outside PALETTE[0..4]: ${p.getAttribute("stroke")}`);
  }

  // Audit every .help-icon on System page (including Charts Node Scope & Network Interface)
  const sysIcons = findAll(sys1(), (n) => hasClass(n, "help-icon"));
  assert(sysIcons.length >= 12, `Expected >= 12 help icons on System page, got ${sysIcons.length}`);
  const seenLabels = new Set();
  for (const icon of sysIcons) {
    const ariaLabel = icon.getAttribute("aria-label") || "";
    seenLabels.add(ariaLabel);
    icon.dispatchEvent("mouseenter", { clientX: 40, clientY: 40 });
    const bodyEl = findAll(r1.tooltip, (n) => hasClass(n, "help-tip-body"))[0];
    const formEl = findAll(r1.tooltip, (n) => hasClass(n, "help-tip-formula"))[0];
    assert(bodyEl && nodeText(bodyEl).length > 0, `Empty help prose for ${ariaLabel}`);
    assert(!forbiddenRe.test(nodeText(bodyEl)), `Forbidden term in help prose: ${nodeText(bodyEl)}`);
    if (ariaLabel !== "Help: Node") {
      assert(formEl && nodeText(formEl).length > 0, `Empty help formula for ${ariaLabel}`);
      assert(!forbiddenRe.test(nodeText(formEl)), `Forbidden term in help formula: ${nodeText(formEl)}`);
    }
  }
  assert(seenLabels.has("Help: Charts Node Scope"), "Missing Charts Node Scope help icon");
  assert(seenLabels.has("Help: Network Interface"), "Missing Network Interface help icon");

  // Case 2: 6 nodes (>5 -> <select> for Scope & Charts Scope, clickable Target Nodes pills preserved) & 7 NICs (>3 -> <select>, >5 -> top 4 + Other)
  const r2 = await runCase(cases.many);
  const sys2 = () => r2.root.children.find((n) => n.getAttribute("data-page") === "system");
  const targetPillsMany = findAll(sys2(), (n) => n.tagName === "BUTTON" && n.getAttribute("aria-pressed") !== null);
  assert.strictEqual(targetPillsMany.length, 6, "Expected 6 clickable Target Nodes pills even when >5 nodes");
  const chartsScopeSelect = findAll(sys2(), (n) => n.tagName === "SELECT" && n.getAttribute("aria-label") === "Charts node scope")[0];
  assert(chartsScopeSelect, "Expected Charts node scope <select> for >5 nodes");
  chartsScopeSelect.dispatchEvent("change", { target: { value: "node-6" } });

  // Verify selected option persistence across re-render on BOTH Node scope and Charts node scope <select>s
  for (const aria of ["Node scope", "Charts node scope"]) {
    const sel = findAll(sys2(), (n) => n.tagName === "SELECT" && n.getAttribute("aria-label") === aria)[0];
    assert(sel, `Missing <select aria-label="${aria}"> after re-render`);
    assert.strictEqual(sel.value, "node-6", `${aria} select.value did not persist across re-render`);
    const opt6 = findAll(sel, (n) => n.tagName === "OPTION" && n.getAttribute("value") === "node-6")[0];
    assert(opt6 && opt6.getAttribute("selected") === "selected", `${aria} option node-6 missing selected attribute`);
  }

  // Verify >3 NICs renders <select aria-label="Network interface">, >5 NICs By NIC folds into top 4 + Other in PALETTE[0..4], and selection persists
  const nicSel = findAll(sys2(), (n) => n.tagName === "SELECT" && n.getAttribute("aria-label") === "Network interface")[0];
  assert(nicSel, "Expected Network interface <select> for >3 NICs");
  nicSel.dispatchEvent("change", { target: { value: "__by_nic__" } });
  const nicSelAfter = findAll(sys2(), (n) => n.tagName === "SELECT" && n.getAttribute("aria-label") === "Network interface")[0];
  assert.strictEqual(nicSelAfter.value, "__by_nic__", "Network interface select.value did not persist __by_nic__");
  const byNicOpt = findAll(nicSelAfter, (n) => n.tagName === "OPTION" && n.getAttribute("value") === "__by_nic__")[0];
  assert(byNicOpt && byNicOpt.getAttribute("selected") === "selected", "By NIC option missing selected attribute");
  const yNetMany = findAll(sys2(), (n) => n.getAttribute("id") === "y-net")[0];
  const yNetManyText = nodeText(yNetMany);
  assert(yNetManyText.includes("Other RX (MiB/s)") && yNetManyText.includes("Other TX (MiB/s)"), `Expected top-4 + Other folding for >5 NICs: ${yNetManyText}`);
  const manyPaths = findAll(yNetMany, (n) => n.tagName === "PATH");
  assert.strictEqual(manyPaths.length, 10, `Expected 10 paths (5 entities x RX/TX), got ${manyPaths.length}`);
  for (const p of manyPaths) {
    assert(ALLOWED_PALETTE_5.has(p.getAttribute("stroke")), `Many-NIC By NIC color outside PALETTE[0..4]: ${p.getAttribute("stroke")}`);
  }

  // Case 3: Legacy v<=9 snapshot without network_interfaces shows aggregate RX/TX and subtle caption
  const r3 = await runCase(cases.legacy);
  const sys3 = r3.root.children.find((n) => n.getAttribute("data-page") === "system");
  const yNetLegacy = findAll(sys3, (n) => n.getAttribute("id") === "y-net")[0];
  assert.strictEqual(findAll(yNetLegacy, (n) => n.tagName === "SELECT" || n.tagName === "BUTTON").length, 0, "Legacy snapshot should not render NIC selector controls");
  const legacyText = nodeText(yNetLegacy);
  assert(legacyText.includes("Network RX (MiB/s)") && legacyText.includes("Network TX (MiB/s)"), "Legacy aggregate RX/TX series missing");
  assert(legacyText.includes("Per-interface breakdown is unavailable for this telemetry snapshot"), "Legacy fallback hint missing");

  // Case 4: Prototype-colliding node & device names ("__proto__", "constructor") must not pollute Object.prototype / Object or crash
  const r4 = await runCase(cases.proto);
  const sys4 = () => r4.root.children.find((n) => n.getAttribute("data-page") === "system");
  assert(sys4(), "System page missing for proto case");
  assert.strictEqual(Object.prototype.rx, undefined, "Object.prototype.rx was polluted");
  assert.strictEqual(Object.prototype.hasRx, undefined, "Object.prototype.hasRx was polluted");
  assert.strictEqual(Object.prototype.tx, undefined, "Object.prototype.tx was polluted");
  assert.strictEqual(Object.prototype.hasTx, undefined, "Object.prototype.hasTx was polluted");
  assert.strictEqual(Object.rx, undefined, "Object.rx was mutated");
  assert.strictEqual(Object.hasRx, undefined, "Object.hasRx was mutated");
  const yNet4 = findAll(sys4(), (n) => n.getAttribute("id") === "y-net")[0];
  findAll(yNet4, (n) => n.tagName === "BUTTON" && n.textContent === "By NIC")[0].dispatchEvent("click");
  findAll(findAll(sys4(), (n) => n.getAttribute("id") === "y-net")[0], (n) => n.tagName === "BUTTON" && n.textContent === "__proto__")[0].dispatchEvent("click");
  findAll(findAll(sys4(), (n) => n.getAttribute("id") === "y-net")[0], (n) => n.tagName === "BUTTON" && n.textContent === "constructor")[0].dispatchEvent("click");
  const csBar4 = findAll(sys4(), (n) => n.getAttribute("data-charts-scope") === "true")[0];
  findAll(csBar4, (n) => n.tagName === "BUTTON" && n.textContent === "constructor")[0].dispatchEvent("click");
  findAll(findAll(sys4(), (n) => n.getAttribute("data-charts-scope") === "true")[0], (n) => n.tagName === "BUTTON" && n.textContent === "__proto__")[0].dispatchEvent("click");
  assert.strictEqual(Object.prototype.rx, undefined, "Object.prototype.rx was polluted after interactive selection");
  assert.strictEqual(Object.prototype.hasRx, undefined, "Object.prototype.hasRx was polluted after interactive selection");
  assert.strictEqual(Object.rx, undefined, "Object.rx was mutated after interactive selection");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"rich2": emb_rich2, "many": emb_many, "legacy": emb_legacy, "proto": emb_proto}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node v34 System DOM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_html_report_v35_adaptive_system_units(sim_env: Path) -> None:
    """v35 offline report scales System bytes & byte-rates adaptively across B..PiB and B/s..PiB/s."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    for key in (
        "Host Memory Usage",
        "Disk I/O & Capacity",
        "Network Throughput",
        "Disk I/O (R / W)",
        "Network (RX / TX)",
        "Host Memory & Disk I/O",
        "Network RX / TX Throughput",
        "y-mem-disk",
        "y-net",
    ):
        assert charts_js.count(f'"{key}":') == 2, f"Expected HELP_TEXTS + HELP_FORMULAS entries for: {key}"

    tib = 1024**4
    pib = 1024**5
    gib = 1024**3
    mib = 1024**2
    kib = 1024

    st_adaptive = {
        **_sample_server_telemetry(),
        "system": {
            "available": True,
            "nodes": ["node-big", "node-small"],
            "cpu_avg_pct": 45.0,
            "cpu_peak_pct": 72.0,
            "mem_used_bytes": 512 * gib,
            "mem_total_bytes": 2 * tib,
            "mem_avail_min_bytes": 1.5 * tib,
            "mem_peak_pct": 65.0,
            "disk_read_bps": 120 * kib,
            "disk_write_bps": 12.5 * gib,
            "disk_avail_bytes": 1.5 * tib,
            "disk_total_bytes": 4 * tib,
            "net_rx_bps": 12.5 * gib,
            "net_tx_bps": 25 * gib,
            "net_rx_peak_bps": 1.25 * tib,
            "net_tx_peak_bps": 2 * tib,
            "network_interfaces": [
                {
                    "device": "ib0",
                    "rx_bytes_per_second": {"mean": 25 * gib, "max": 30 * gib},
                    "tx_bytes_per_second": {"mean": 12.5 * gib, "max": 15 * gib},
                },
                {
                    "device": "eth0",
                    "rx_bytes_per_second": {"mean": 400 * kib, "max": 500 * kib},
                    "tx_bytes_per_second": {"mean": 200 * kib, "max": 250 * kib},
                },
            ],
            "series": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "host_cpu_usage_percent": 40.0,
                    "host_memory_used_bytes": 1.5 * tib,
                    "host_memory_available_bytes": 0.5 * tib,
                    "host_disk_read_bytes_per_second": 2 * gib,
                    "host_disk_written_bytes_per_second": 512 * mib,
                    "network_interfaces": {
                        "ib0": {"rx": 25 * gib, "tx": 12.5 * gib},
                        "eth0": {"rx": 400 * kib, "tx": 200 * kib},
                    },
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "host_cpu_usage_percent": 50.0,
                    "host_memory_used_bytes": 2 * tib,
                    "host_memory_available_bytes": 1 * tib,
                    "host_disk_read_bytes_per_second": 3 * gib,
                    "host_disk_written_bytes_per_second": 1 * gib,
                    "network_interfaces": {
                        "ib0": {"rx": 30 * gib, "tx": 15 * gib},
                        "eth0": {"rx": 500 * kib, "tx": 250 * kib},
                    },
                },
            ],
        },
        "per_node": [
            {
                "node": "node-big",
                "roles": ["worker"],
                "pod_count": 2,
                "cpu_usage_percent": {"mean": 55.0, "max": 72.0},
                "memory_used_bytes": {"mean": 1.5 * tib},
                "memory_total_bytes": 4 * tib,
                "memory_usage_percent": {"mean": 37.5, "max": 50.0},
                "disk_read_bytes_per_second": {"mean": 2 * gib},
                "disk_written_bytes_per_second": {"mean": 512 * mib},
                "filesystem_avail_bytes": 2 * pib,
                "filesystem_size_bytes": 4 * pib,
                "network_receive_bytes_per_second": {"mean": 25 * gib},
                "network_transmit_bytes_per_second": {"mean": 12.5 * gib},
            },
            {
                "node": "node-small",
                "roles": ["worker"],
                "pod_count": 1,
                "cpu_usage_percent": {"mean": 25.0, "max": 40.0},
                "memory_used_bytes": {"mean": 32 * gib},
                "memory_total_bytes": 64 * gib,
                "memory_usage_percent": {"mean": 50.0, "max": 60.0},
                "disk_read_bytes_per_second": {"mean": 512 * kib},
                "disk_written_bytes_per_second": {"mean": 0.0},
                "filesystem_avail_bytes": 512 * gib,
                "filesystem_size_bytes": 2 * tib,
                "network_receive_bytes_per_second": {"mean": 400 * kib},
                "network_transmit_bytes_per_second": {"mean": 200 * kib},
            },
        ],
        "per_node_series": {
            "node-big": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 55.0,
                    "memory_used_bytes": 1.5 * tib,
                    "memory_available_bytes": 2.5 * tib,
                    "disk_read_bytes_per_second": 2 * gib,
                    "disk_written_bytes_per_second": 512 * mib,
                    "network_interfaces": {"ib0": {"rx": 25 * gib, "tx": 12.5 * gib}},
                },
            ],
            "node-small": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 25.0,
                    "memory_used_bytes": 32 * gib,
                    "memory_available_bytes": 32 * gib,
                    "disk_read_bytes_per_second": 512 * kib,
                    "disk_written_bytes_per_second": 128 * kib,
                    "network_interfaces": {"eth0": {"rx": 400 * kib, "tx": 200 * kib}},
                },
            ],
        },
    }
    emb_adaptive = _extract_embedded_report_json(
        render_html_report(
            _make_task("0000004a", name="v35 System Adaptive Units"),
            client_series=_sample_client_series(),
            server_telemetry=st_adaptive,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    st_legacy_units = {
        **_sample_server_telemetry(),
        "system": {
            "nodes": ["node-legacy-gb"],
            "mem_avail_bytes": 2.5 * tib,
            "disk_avail_bytes": 1.5 * tib,
            "series": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "host_cpu_usage_percent": 20.0,
                    "host_memory_used_gb": 512.0,
                    "host_memory_available_gb": 2048.0,
                    "host_disk_read_mbps": 2048.0,
                    "host_disk_write_mbps": 512.0,
                    "host_network_receive_mbps": 2048.0,
                    "host_network_transmit_mbps": 1024.0,
                },
            ],
        },
    }
    emb_legacy_units = _extract_embedded_report_json(
        render_html_report(
            _make_task("0000004b", name="v35 Legacy GB/Mbps Scaling"),
            client_series=_sample_client_series(),
            server_telemetry=st_legacy_units,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    st_empty_and_zero = {
        **_sample_server_telemetry(),
        "system": {
            "available": True,
            "nodes": ["node-empty", "node-zero"],
            "series": [],
        },
        "per_node_series": {
            "node-empty": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 15.0,
                },
            ],
            "node-zero": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 10.0,
                    "memory_used_bytes": 0.0,
                    "disk_read_bytes_per_second": 0.0,
                    "network_receive_bytes_per_second": 0.0,
                },
            ],
        },
    }
    emb_empty_zero = _extract_embedded_report_json(
        render_html_report(
            _make_task("0000004c", name="v35 Empty vs Zero Series"),
            client_series=_sample_client_series(),
            server_telemetry=st_empty_and_zero,
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
    )

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const rawSrc = fs.readFileSync(process.argv[1], "utf8");
const instrumented = rawSrc.replace(
  /\n\}\)\(\);\s*$/,
  "\nglobalThis.__unitExports = { bytesFmt, scaleByteValue, byteRateFmt, formatBytePair, formatByteRatePair, selectByteScale, selectByteRateScale };\n})();\n"
);
assert(instrumented !== rawSrc, "Failed to instrument report_charts.js");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    tagName: tag.toUpperCase(),
    id: "",
    value: "",
    selected: false,
    children: [],
    attributes: {},
    style: {},
    className: "",
    classList: {
      add(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.add(c); this._owner.className = Array.from(s).join(" "); },
      remove(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.delete(c); this._owner.className = Array.from(s).join(" "); },
      toggle(c, force) { const has = this.contains(c); const next = force !== undefined ? Boolean(force) : !has; if (next) this.add(c); else this.remove(c); },
      contains(c) { return (this._owner.className || "").split(/\s+/).includes(c); },
      _owner: null,
    },
    textContent: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) { this.attributes[k] = String(v); if (k === "class") this.className = String(v); },
    getAttribute(k) {
      if (k === "id" && this.id) return String(this.id);
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text"); t.textContent = child; t.parentNode = this; this.children.push(t);
        } else {
          child.parentNode = this; this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this, stopPropagation() {}, preventDefault() {} }); },
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const mk = (tag) => { const n = makeNode(tag); n.classList._owner = n; return n; };
  const ids = {
    "chart-tooltip": mk("div"),
    "report-root": mk("main"),
    "report-title": mk("span"),
    "report-meta": mk("div"),
    "status-badges": mk("span"),
    "lens-report-data": (() => { const s = mk("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": mk("button"),
    "sidebar": mk("aside"),
    "theme-toggle": mk("button"),
    "print-btn": mk("button"),
    "window-toggle": mk("div"),
    "nav-list": mk("nav"),
  };
  const doc = {
    documentElement: mk("html"),
    body: mk("body"),
    createElement: (t) => mk(t),
    createElementNS: (_ns, t) => mk(t),
    createTextNode: (t) => { const n = mk("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || mk("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(instrumented, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], exports: ctx.__unitExports || vm.runInNewContext("globalThis.__unitExports", ctx) };
}

(async () => {
  const { root, exports: ex } = await runCase(cases.adaptive);

  // 1. Direct unit checks of adaptive binary helpers
  assert.strictEqual(ex.bytesFmt(0), "0 B");
  assert.strictEqual(ex.bytesFmt(-0), "0 B");
  assert.strictEqual(ex.bytesFmt(-0.001), "0.00 B");
  assert.strictEqual(ex.bytesFmt(512), "512 B");
  assert.strictEqual(ex.bytesFmt(1.5 * 1024 ** 4), "1.50 TiB");
  assert.strictEqual(ex.bytesFmt(2 * 1024 ** 5), "2 PiB");
  assert.strictEqual(ex.bytesFmt(-2 * 1024 ** 4), "-2 TiB");
  assert.strictEqual(ex.bytesFmt(null), "N/A");
  assert.strictEqual(ex.bytesFmt(false), "N/A");
  assert.strictEqual(ex.bytesFmt([]), "N/A");

  const plain = (o) => JSON.parse(JSON.stringify(o));
  assert.deepStrictEqual(plain(ex.scaleByteValue(2 * 1024 ** 4)), { value: 2, unit: "TiB", unitIndex: 4, divisor: 1024 ** 4 });
  assert.strictEqual(ex.scaleByteValue(NaN), null);

  assert.strictEqual(ex.byteRateFmt(0), "0 B/s");
  assert.strictEqual(ex.byteRateFmt(400 * 1024), "400 KiB/s");
  assert.strictEqual(ex.byteRateFmt(12.5 * 1024 ** 3), "12.50 GiB/s");
  assert.strictEqual(ex.byteRateFmt(1.25 * 1024 ** 4), "1.25 TiB/s");
  assert.strictEqual(ex.byteRateFmt(2 * 1024 ** 5), "2 PiB/s");
  assert.strictEqual(ex.byteRateFmt(undefined), "N/A");

  assert.strictEqual(ex.formatBytePair(0, 2 * 1024 ** 4), "0 / 2 TiB");
  assert.strictEqual(ex.formatBytePair(1.5 * 1024 ** 4, 4 * 1024 ** 4), "1.50 / 4 TiB");
  assert.strictEqual(ex.formatBytePair(512 * 1024 ** 3, 2 * 1024 ** 4), "512 GiB / 2 TiB");

  assert.strictEqual(ex.formatByteRatePair(null, null), null);
  assert.strictEqual(ex.formatByteRatePair(null, 50 * 1024 ** 2), "0 / 50 MiB/s");
  assert.strictEqual(ex.formatByteRatePair(12.5 * 1024 ** 3, 25 * 1024 ** 3), "12.50 / 25 GiB/s");
  assert.strictEqual(ex.formatByteRatePair(120 * 1024, 12.5 * 1024 ** 3), "120 KiB/s / 12.50 GiB/s");

  assert.deepStrictEqual(plain(ex.selectByteScale([null, 0, -2 * 1024 ** 4])), { unit: "TiB", divisor: 1024 ** 4, unitIndex: 4 });
  assert.deepStrictEqual(plain(ex.selectByteScale([])), { unit: "B", divisor: 1, unitIndex: 0 });
  assert.deepStrictEqual(plain(ex.selectByteRateScale([512 * 1024, 2 * 1024 ** 3])), { unit: "GiB/s", divisor: 1024 ** 3, unitIndex: 3 });

  // Rounding-boundary promotion (avoid "1,024.00 <unit>", cap at PiB / PiB/s)
  assert.strictEqual(ex.bytesFmt(1023.999), "1.00 KiB");
  assert.strictEqual(ex.bytesFmt(1023.999 * 1024 ** 3), "1.00 TiB");
  assert.strictEqual(ex.bytesFmt(-1023.999 * 1024 ** 3), "-1.00 TiB");
  assert.strictEqual(ex.bytesFmt(1023.999 * 1024 ** 5), "1,024.00 PiB");
  assert.strictEqual(ex.byteRateFmt(1023.999 * 1024 ** 2), "1.00 GiB/s");
  assert.strictEqual(ex.formatBytePair(1023.999 * 1024 ** 3, 2 * 1024 ** 4), "1.00 / 2 TiB");
  assert.deepStrictEqual(plain(ex.selectByteScale([1023.999 * 1024 ** 3])), { unit: "TiB", divisor: 1024 ** 4, unitIndex: 4 });

  // 2. KPI tiles & Per-Node Summary Table rendering
  const sys = () => root.children.find((n) => n.getAttribute("data-page") === "system");
  const kpiText = nodeText(findAll(sys(), (n) => n.getAttribute("id") === "y-kpis")[0]);
  assert(kpiText.includes("512 GiB / 2 TiB"), `Expected mixed-tier Host Memory Usage KPI: ${kpiText}`);
  assert(kpiText.includes("Min Avail: 1.50 TiB"), `Expected TiB Min Avail: ${kpiText}`);
  assert(kpiText.includes("120 KiB/s / 12.50 GiB/s"), `Expected mixed-tier Disk I/O KPI: ${kpiText}`);
  assert(kpiText.includes("Free: 1.50 / 4 TiB"), `Expected shared TiB Free Disk subtitle: ${kpiText}`);
  assert(kpiText.includes("12.50 / 25 GiB/s"), `Expected GiB/s Network Throughput KPI: ${kpiText}`);
  assert(kpiText.includes("Peak RX / TX: 1.25 / 2 TiB/s"), `Expected TiB/s Peak Network subtitle: ${kpiText}`);

  const yNodes = findAll(sys(), (n) => n.getAttribute("id") === "y-nodes")[0];
  const thLabels = findAll(yNodes, (n) => n.tagName === "TH").map(nodeText);
  assert(thLabels.some((t) => t.startsWith("Disk I/O (R / W)") && !t.includes("MiB/s")), `Bad Disk I/O header: ${JSON.stringify(thLabels)}`);
  assert(thLabels.some((t) => t.startsWith("Network (RX / TX)") && !t.includes("MiB/s")), `Bad Network header: ${JSON.stringify(thLabels)}`);
  const tableText = nodeText(yNodes);
  assert(tableText.includes("1.50 / 4 TiB"), `Expected TiB memory cell: ${tableText}`);
  assert(tableText.includes("2 / 4 PiB"), `Expected PiB free disk cell: ${tableText}`);
  assert(tableText.includes("2 GiB/s / 512 MiB/s"), `Expected mixed GiB/s & MiB/s disk I/O cell: ${tableText}`);
  assert(tableText.includes("512 / 0 KiB/s"), `Expected shared-zero KiB/s disk I/O cell: ${tableText}`);
  assert(tableText.includes("400 / 200 KiB/s"), `Expected KiB/s network cell: ${tableText}`);

  // Verify numeric sorting on raw bytes/s (clicking Disk I/O header sorts ascending -> node-small first, then descending -> node-big first)
  const diskTh = findAll(yNodes, (n) => n.tagName === "TH" && nodeText(n).startsWith("Disk I/O (R / W)"))[0];
  diskTh.dispatchEvent("click");
  const rowsAsc = findAll(yNodes, (n) => n.tagName === "TBODY")[0].children.map(nodeText);
  assert(rowsAsc[0].includes("node-big"), `First click on numeric Disk I/O col sorts desc (node-big > node-small): ${rowsAsc}`);
  diskTh.dispatchEvent("click");
  const rowsDesc = findAll(yNodes, (n) => n.tagName === "TBODY")[0].children.map(nodeText);
  assert(rowsDesc[0].includes("node-small"), `Second click sorts asc (node-small < node-big): ${rowsDesc}`);

  // 3. y-mem-disk & y-net adaptive scales in aggregate vs node-scoped & NIC-scoped views
  const memDiskAgg = nodeText(findAll(sys(), (n) => n.getAttribute("id") === "y-mem-disk")[0]);
  assert(memDiskAgg.includes("Memory used / available (TiB) & Disk read / write throughput (GiB/s)"), memDiskAgg);
  assert(memDiskAgg.includes("Mem Used (TiB)") && memDiskAgg.includes("Disk Read (GiB/s)"), memDiskAgg);

  const yNetAgg = nodeText(findAll(sys(), (n) => n.getAttribute("id") === "y-net")[0]);
  assert(yNetAgg.includes("Host network receive and transmit throughput (GiB/s)"), yNetAgg);
  assert(yNetAgg.includes("Network RX (GiB/s)") && yNetAgg.includes("Network TX (GiB/s)"), yNetAgg);

  // Switch to By NIC -> shares GiB/s across visible NICs
  findAll(findAll(sys(), (n) => n.getAttribute("id") === "y-net")[0], (n) => n.tagName === "BUTTON" && n.textContent === "By NIC")[0].dispatchEvent("click");
  const yNetByNic = nodeText(findAll(sys(), (n) => n.getAttribute("id") === "y-net")[0]);
  assert(yNetByNic.includes("ib0 RX (GiB/s)") && yNetByNic.includes("eth0 RX (GiB/s)"), yNetByNic);
  assert(yNetByNic.includes("Aggregate per-NIC network RX / TX throughput (GiB/s)"), yNetByNic);

  // Switch to single NIC eth0 -> scales down to KiB/s
  findAll(findAll(sys(), (n) => n.getAttribute("id") === "y-net")[0], (n) => n.tagName === "BUTTON" && n.textContent === "eth0")[0].dispatchEvent("click");
  const yNetEth0 = nodeText(findAll(sys(), (n) => n.getAttribute("id") === "y-net")[0]);
  assert(yNetEth0.includes("eth0 RX (KiB/s)") && yNetEth0.includes("eth0 TX (KiB/s)"), yNetEth0);
  assert(yNetEth0.includes("All Nodes · eth0 RX / TX (KiB/s)"), yNetEth0);

  // Scope Charts to node-small -> y-mem-disk rescales to GiB and KiB/s
  const csBar = findAll(sys(), (n) => n.getAttribute("data-charts-scope") === "true")[0];
  findAll(csBar, (n) => n.tagName === "BUTTON" && n.textContent === "node-small")[0].dispatchEvent("click");
  const memDiskSmall = nodeText(findAll(sys(), (n) => n.getAttribute("id") === "y-mem-disk")[0]);
  assert(memDiskSmall.includes("Node node-small: Memory used / available (GiB) & Disk read / write throughput (KiB/s)"), memDiskSmall);
  assert(memDiskSmall.includes("Mem Used (GiB)") && memDiskSmall.includes("Disk Read (KiB/s)"), memDiskSmall);

  // 4. Legacy *_gb / *_mbps fallback (with sys.available omitted) + mem_avail_bytes-only KPI fallback
  const legacyRes = await runCase(cases.legacyUnits);
  const sysLeg = legacyRes.root.children.find((n) => n.getAttribute("data-page") === "system");
  const legKpis = nodeText(findAll(sysLeg, (n) => n.getAttribute("id") === "y-kpis")[0]);
  assert(legKpis.includes("2.50 TiB avail"), `Expected mem_avail_bytes-only KPI fallback: ${legKpis}`);
  assert(legKpis.includes("1.50 TiB") && legKpis.includes("Free: 1.50 TiB"), `Expected disk-only fallback in TiB: ${legKpis}`);
  const legMemDisk = nodeText(findAll(sysLeg, (n) => n.getAttribute("id") === "y-mem-disk")[0]);
  assert(legMemDisk.includes("Mem Used (TiB)") && legMemDisk.includes("Mem Available (TiB)") && legMemDisk.includes("Disk Read (GiB/s)"), legMemDisk);
  const legNet = nodeText(findAll(sysLeg, (n) => n.getAttribute("id") === "y-net")[0]);
  assert(legNet.includes("Network RX (GiB/s)") && legNet.includes("Network TX (GiB/s)"), legNet);

  // 5. Empty series omit fake (B)/(B/s) subtitles while all-zero finite series keep (B)/(B/s)
  const emptyZeroRes = await runCase(cases.emptyZero);
  const sysEZ = () => emptyZeroRes.root.children.find((n) => n.getAttribute("data-page") === "system");
  const ezCsBar = findAll(sysEZ(), (n) => n.getAttribute("data-charts-scope") === "true")[0];
  findAll(ezCsBar, (n) => n.tagName === "BUTTON" && n.textContent === "node-empty")[0].dispatchEvent("click");
  const emptyMemDiskSub = nodeText(findAll(findAll(sysEZ(), (n) => n.getAttribute("id") === "y-mem-disk")[0], (n) => n.className === "section-sub")[0]);
  assert.strictEqual(emptyMemDiskSub, "Node node-empty: Memory used / available & Disk read / write throughput");
  const emptyNetSub = nodeText(findAll(findAll(sysEZ(), (n) => n.getAttribute("id") === "y-net")[0], (n) => n.className === "section-sub")[0]);
  assert.strictEqual(emptyNetSub, "Node node-empty network RX / TX");

  const ezCsBar2 = findAll(sysEZ(), (n) => n.getAttribute("data-charts-scope") === "true")[0];
  findAll(ezCsBar2, (n) => n.tagName === "BUTTON" && n.textContent === "node-zero")[0].dispatchEvent("click");
  const zeroMemDiskSub = nodeText(findAll(findAll(sysEZ(), (n) => n.getAttribute("id") === "y-mem-disk")[0], (n) => n.className === "section-sub")[0]);
  assert.strictEqual(zeroMemDiskSub, "Node node-zero: Memory used / available (B) & Disk read / write throughput (B/s)");
  const zeroNetSub = nodeText(findAll(findAll(sysEZ(), (n) => n.getAttribute("id") === "y-net")[0], (n) => n.className === "section-sub")[0]);
  assert.strictEqual(zeroNetSub, "Node node-zero network RX / TX (B/s)");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"adaptive": emb_adaptive, "legacyUnits": emb_legacy_units, "emptyZero": emb_empty_zero}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v35 adaptive units check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v38_per_request_scatter_and_window_toggle(sim_env: Path) -> None:
    """Offline v38 report compacts heatmap scatter points, renders per-request dots, and respects window toggles."""
    import importlib.resources
    import shutil
    import subprocess

    from llm_d_lens.simulation.reporting import builder as report_builder

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = Path(str(assets_dir.joinpath("report_template.html"))).read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))
    charts_js = charts_js_path.read_text(encoding="utf-8")

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    assert '<a href="#r-buckets" data-page="requests">Buckets &amp; Scatter</a>' in template_html

    for key in (
        "r-buckets",
        "TTFT by Time and Input Sequence Length",
        "TPOT by Time and Output Sequence Length",
    ):
        assert charts_js.count(f'"{key}":') == 2, f"Expected HELP_TEXTS + HELP_FORMULAS entries for: {key}"

    # 1. _compact_heatmap_block caps points <= 2000 across top-level, legacy, and window heatmaps
    def _make_pts(n: int, *, start_t: float = 0.0, step_t: float = 0.01) -> list[dict]:
        return [
            {
                "t": round(start_t + i * step_t, 3),
                "seq": 64 + (i % 512),
                "ms": round(10.0 + (i % 100) * 0.5, 3),
            }
            for i in range(n)
        ]

    big_pts = _make_pts(2500)
    win_prof_pts = _make_pts(2400, start_t=5.0)
    win_full_pts = _make_pts(2600, start_t=0.0)
    win_warm_pts = _make_pts(2200, start_t=0.0)
    payload_big = {
        "client_series": {
            "heatmap": {
                "ttft": {
                    "points": big_pts,
                    "points_total": 3500,
                    "points_sampled": 2500,
                    "sequence_length": "input",
                    "sequence_length_source": "observed",
                    "min_ms": 10.0,
                    "max_ms": 59.5,
                },
                "tpot": {
                    "points": _make_pts(15),
                    "points_total": True,  # malformed bool must be sanitized
                    "points_sampled": 15,
                    "sequence_length": "output",
                    "sequence_length_source": "requested",
                },
            },
            "ttft_heatmap": {"points": _make_pts(2300)},
            "tpot_heatmap": {"points": _make_pts(2100), "points_total": 2100},
            "windows": {
                "profiling": {"heatmap": {"ttft": {"points": win_prof_pts, "points_total": 2400}, "tpot": None}},
                "full": {"heatmap": {"ttft": {"points": win_full_pts, "points_total": 2600}, "tpot": None}},
                "warmup": {"heatmap": {"ttft": {"points": win_warm_pts}, "tpot": None}},
            },
        }
    }
    compacted = report_builder._compact_report_payload(payload_big, max_workload_points=2000)
    cs_comp = compacted["client_series"]
    assert len(cs_comp["heatmap"]["ttft"]["points"]) == 2000
    assert cs_comp["heatmap"]["ttft"]["points_total"] == 3500
    assert cs_comp["heatmap"]["ttft"]["points_sampled"] == 2000
    assert len(cs_comp["heatmap"]["tpot"]["points"]) == 15
    assert cs_comp["heatmap"]["tpot"]["points_total"] == 15
    assert cs_comp["heatmap"]["tpot"]["points_sampled"] == 15
    assert len(cs_comp["ttft_heatmap"]["points"]) == 2000
    assert cs_comp["ttft_heatmap"]["points_total"] == 2300
    assert cs_comp["ttft_heatmap"]["points_sampled"] == 2000
    assert len(cs_comp["tpot_heatmap"]["points"]) == 2000
    assert cs_comp["tpot_heatmap"]["points_total"] == 2100
    assert cs_comp["tpot_heatmap"]["points_sampled"] == 2000
    assert len(cs_comp["windows"]["profiling"]["heatmap"]["ttft"]["points"]) == 2000
    assert cs_comp["windows"]["profiling"]["heatmap"]["ttft"]["points_total"] == 2400
    assert cs_comp["windows"]["profiling"]["heatmap"]["ttft"]["points_sampled"] == 2000
    assert len(cs_comp["windows"]["full"]["heatmap"]["ttft"]["points"]) == 2000
    assert cs_comp["windows"]["full"]["heatmap"]["ttft"]["points_total"] == 2600
    assert cs_comp["windows"]["full"]["heatmap"]["ttft"]["points_sampled"] == 2000
    assert len(cs_comp["windows"]["warmup"]["heatmap"]["ttft"]["points"]) == 2000
    assert cs_comp["windows"]["warmup"]["heatmap"]["ttft"]["points_total"] == 2200
    assert cs_comp["windows"]["warmup"]["heatmap"]["ttft"]["points_sampled"] == 2000

    # 2. Render full HTML report & check security / brand ratchets
    cs = _sample_client_series()
    cs["heatmap"] = {
        "ttft": {
            "points": [
                {"t": 1.25, "seq": 128, "ms": 20.0},
                {"t": 3.5, "seq": 256, "ms": 95.0},
                {"t": 6.0, "seq": 512, "ms": 42.5},
            ],
            "points_total": 3,
            "points_sampled": 3,
            "duration_seconds": 10.0,
            "window_start_seconds": 0.0,
            "sequence_length": "input",
            "sequence_length_source": "observed",
            "min_ms": 20.0,
            "max_ms": 95.0,
            "cells": [
                {
                    "start_seconds": 0.0,
                    "end_seconds": 5.0,
                    "seq_start": 100,
                    "seq_end": 300,
                    "count": 2,
                    "avg_ms": 57.5,
                }
            ],
        },
        "tpot": {
            "points": [
                {"t": 2.0, "seq": 64, "ms": 8.5},
                {"t": 4.5, "seq": 128, "ms": 14.2},
            ],
            "points_total": 2,
            "points_sampled": 2,
            "duration_seconds": 10.0,
            "window_start_seconds": 0.0,
            "sequence_length": "output",
            "sequence_length_source": "requested",
            "min_ms": 8.5,
            "max_ms": 14.2,
        },
    }
    cs["windows"]["profiling"]["heatmap"] = {
        "ttft": {
            "points": [
                {"t": 5.0, "seq": 256, "ms": 40.0},
                {"t": 7.5, "seq": 512, "ms": 110.0},
            ],
            "points_total": 2,
            "points_sampled": 2,
            "duration_seconds": 10.0,
            "window_start_seconds": 4.0,
            "sequence_length": "input",
            "sequence_length_source": "observed",
            "min_ms": 40.0,
            "max_ms": 110.0,
        },
        "tpot": {
            "points": [{"t": 6.25, "seq": 1, "ms": 11.5}],
            "points_total": 1,
            "points_sampled": 1,
            "duration_seconds": 10.0,
            "window_start_seconds": 4.0,
            "sequence_length": "output",
            "sequence_length_source": "requested",
            "min_ms": 11.5,
            "max_ms": 11.5,
        },
    }
    cs["windows"]["full"]["heatmap"] = {
        "ttft": {
            "points": [
                {"t": 1.0, "seq": 128, "ms": 20.0},
                {"t": 3.5, "seq": 256, "ms": 95.0},
                {"t": 5.0, "seq": 256, "ms": 40.0},
                {"t": 7.5, "seq": 512, "ms": 110.0},
            ],
            "points_total": 2500,
            "points_sampled": 4,
            "duration_seconds": 10.0,
            "window_start_seconds": 0.0,
            "sequence_length": "input",
            "sequence_length_source": "observed",
            "min_ms": 20.0,
            "max_ms": 110.0,
        },
        "tpot": cs["heatmap"]["tpot"],
    }

    task = _make_task("00000038")
    html_text = render_html_report(
        task,
        client_series=cs,
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    assert "<!-- lens-report-v42 -->" in html_text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert forbidden not in html_text

    node_bin = shutil.which("node")
    if node_bin is None:
        return

    embedded = _extract_embedded_report_json(html_text)
    vm_harness = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const payload = JSON.parse(fs.readFileSync(0, "utf8"));

async function bootReport(casePayload) {
  const payloadJson = JSON.stringify(casePayload);
  const allNodes = [];
  function makeNode(tag, text) {
    const listeners = {};
    const attrs = {};
    const classSet = new Set();
    const node = {
      tagName: String(tag || "div").toUpperCase(),
      children: [],
      style: {},
      className: "",
      textContent: text || "",
      clientWidth: 640,
      classList: {
        add(c) {
          classSet.clear();
          String(node.className || "").split(/\s+/).filter(Boolean).forEach((x) => classSet.add(x));
          classSet.add(c);
          node.className = Array.from(classSet).join(" ");
        },
        remove(c) {
          classSet.clear();
          String(node.className || "").split(/\s+/).filter(Boolean).forEach((x) => classSet.add(x));
          classSet.delete(c);
          node.className = Array.from(classSet).join(" ");
        },
        toggle(c, force) {
          classSet.clear();
          String(node.className || "").split(/\s+/).filter(Boolean).forEach((x) => classSet.add(x));
          const on = force === undefined ? !classSet.has(c) : Boolean(force);
          if (on) classSet.add(c); else classSet.delete(c);
          node.className = Array.from(classSet).join(" ");
        },
        contains(c) { return String(node.className || "").split(/\s+/).includes(c); },
      },
      setAttribute(k, v) { attrs[k] = String(v); if (k === "class") node.className = String(v); },
      getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
      appendChild(c) { if (c != null) node.children.push(typeof c === "string" ? makeNode("#text", c) : c); return c; },
      append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
      replaceChildren(...items) { node.children = []; node.append(...items); },
      addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
      dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || {}); },
      closest(sel) {
        if (sel === "button[data-window]" && node.tagName === "BUTTON" && attrs["data-window"]) return node;
        return null;
      },
      querySelectorAll() { return []; },
      getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
      getBoundingClientRect() { return { left: 0, top: 0, bottom: 10, width: 640, height: 256 }; },
    };
    allNodes.push(node);
    return node;
  }

  const tipNode = makeNode("div");
  const winProfBtn = makeNode("button", "Profiling Steady-State");
  winProfBtn.setAttribute("data-window", "profiling");
  winProfBtn.classList.add("active");
  const winFullBtn = makeNode("button", "Full Run");
  winFullBtn.setAttribute("data-window", "full");
  const winToggle = makeNode("div");
  winToggle.append(winProfBtn, winFullBtn);

  const ids = {
    "chart-tooltip": tipNode,
    "report-root": makeNode("main"),
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", payloadJson),
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": winToggle,
    "nav-list": makeNode("nav"),
  };
  const document = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(txt) { return makeNode("#text", txt); },
    querySelectorAll(sel) {
      if (sel === "#window-toggle button") return [winProfBtn, winFullBtn];
      return [];
    },
  };
  const context = {
    console,
    document,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    ResizeObserver: undefined,
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tipNode, winToggle, winProfBtn, winFullBtn };
}

function inspectRequestsPage(root) {
  const reqPage = root.children.find((n) => n.getAttribute("data-page") === "requests");
  assert(reqPage, "Requests page must be rendered");
  const texts = [];
  const scatterSvgs = [];
  const legacyRects = [];
  (function walk(n) {
    if (!n) return;
    if (n.textContent) texts.push(n.textContent);
    if (n.tagName === "SVG" && String(n.getAttribute("aria-label") || "").includes("per-request scatter plot")) {
      scatterSvgs.push(n);
    }
    if (n.tagName === "RECT" && n.getAttribute("rx") === "1.5") {
      legacyRects.push(n);
    }
    for (const c of n.children || []) walk(c);
  })(reqPage);
  const findCircles = (svg) => {
    const out = [];
    (function walk(n) {
      if (!n) return;
      if (n.tagName === "CIRCLE") out.push(n);
      for (const c of n.children || []) walk(c);
    })(svg);
    return out;
  };
  const findBgRect = (svg) => svg.children.find((c) => c.tagName === "RECT");
  const findTexts = (svg) => {
    const textOf = (n) => (n ? (n.textContent || "") + (n.children || []).map(textOf).join("") : "");
    const out = [];
    (function walk(n) {
      if (!n) return;
      if (n.tagName === "TEXT") out.push(textOf(n));
      else for (const c of n.children || []) walk(c);
    })(svg);
    return out;
  };
  return { reqPage, text: texts.join("\n"), scatterSvgs, legacyRects, findCircles, findBgRect, findTexts };
}

(async () => {
  // Case 1: Profiling steady-state active initially (2 TTFT dots, 1 TPOT requested-seq dot)
  const booted = await bootReport(payload);
  let snap = inspectRequestsPage(booted.root);
  assert.strictEqual(snap.scatterSvgs.length, 2, "Expected 2 per-request scatter SVGs");
  assert.strictEqual(snap.legacyRects.length, 0, "Expected scatter mode to take priority over legacy cells");
  assert(snap.text.includes("Latency by Input Sequence Length, Cache-Hit & Request Scatter"));
  assert(snap.text.includes("Color: TTFT (ms)"));
  assert(snap.text.includes("Color: TPOT (ms/tok)"));
  assert(snap.text.includes("Requested output sequence length (tokens)"));
  assert(snap.text.includes("1 request"), "Expected singular '1 request' for 1-point TPOT");
  assert(snap.text.includes("2 requests"), "Expected '2 requests' for TTFT");

  // Verify TTFT dots sorted ascending by ms + hover ring & tooltip + mouseleave clear
  const ttftSvg = snap.scatterSvgs[0];
  const ttftCircles = snap.findCircles(ttftSvg);
  // 2 data dots + 1 hover ring = 3 circles
  assert.strictEqual(ttftCircles.length, 3);
  const dotLow = ttftCircles[0];
  const dotHigh = ttftCircles[1];
  const hoverRing = ttftCircles[2];
  assert.strictEqual(dotLow.getAttribute("fill"), "rgb(14, 165, 233)");
  assert.strictEqual(dotHigh.getAttribute("fill"), "rgb(244, 63, 94)");
  assert.strictEqual(hoverRing.getAttribute("visibility"), "hidden");

  dotHigh.dispatchEvent("mouseenter", { clientX: 200, clientY: 100 });
  assert.strictEqual(hoverRing.getAttribute("visibility"), "visible");
  assert(booted.tipNode.textContent.includes("Elapsed time: 7.50 s"), booted.tipNode.textContent);
  assert(booted.tipNode.textContent.includes("Input sequence length: 512 tok"), booted.tipNode.textContent);
  assert(booted.tipNode.textContent.includes("TTFT: 110 ms"), booted.tipNode.textContent);

  // Background nearest-dot snap & >18px clear
  const bgRect = snap.findBgRect(ttftSvg);
  const dotLowX = Number(dotLow.getAttribute("cx"));
  const dotLowY = Number(dotLow.getAttribute("cy"));
  bgRect.dispatchEvent("mousemove", { clientX: dotLowX + 3, clientY: dotLowY + 2 });
  assert.strictEqual(hoverRing.getAttribute("visibility"), "visible");
  assert(booted.tipNode.textContent.includes("TTFT: 40 ms"), booted.tipNode.textContent);
  bgRect.dispatchEvent("mousemove", { clientX: 630, clientY: 15 });
  assert.strictEqual(hoverRing.getAttribute("visibility"), "hidden");

  // SVG mouseleave clears hover
  dotLow.dispatchEvent("mouseenter", { clientX: dotLowX, clientY: dotLowY });
  assert.strictEqual(hoverRing.getAttribute("visibility"), "visible");
  ttftSvg.dispatchEvent("mouseleave", {});
  assert.strictEqual(hoverRing.getAttribute("visibility"), "hidden");
  assert.strictEqual(booted.tipNode.style.display, "none");

  // Verify 1-point TPOT edge case: monotonic integer Y-axis ticks, Uniform legend, base stop color
  const tpotSvg = snap.scatterSvgs[1];
  const tpotCircles = snap.findCircles(tpotSvg);
  assert.strictEqual(tpotCircles.length, 2); // 1 dot + 1 hover ring
  assert.strictEqual(tpotCircles[0].getAttribute("fill"), "rgb(14, 165, 233)");
  assert(snap.text.includes("Uniform (11.50 ms/tok)"), snap.text);
  const tpotSvgTexts = snap.findTexts(tpotSvg);
  // First 5 X-axis ticks (fmtDec(tick)s), next 5 Y-axis ticks (strictly increasing integers)
  const yTickNums = tpotSvgTexts.slice(5, 10).map((s) => Number(String(s).replace(/,/g, "")));
  assert.strictEqual(yTickNums.length, 5);
  for (let i = 1; i < yTickNums.length; i++) {
    assert(Number.isInteger(yTickNums[i]) && yTickNums[i] > yTickNums[i - 1], `Non-monotonic Y ticks: ${yTickNums}`);
  }
  tpotCircles[0].dispatchEvent("mouseenter", { clientX: 250, clientY: 120 });
  assert(booted.tipNode.textContent.includes("Requested output sequence length: 1 tok"), booted.tipNode.textContent);
  assert(booted.tipNode.textContent.includes("TPOT: 11.50 ms/tok"), booted.tipNode.textContent);

  // Case 2: Toggle window to Full Run -> renders 4 TTFT dots + sampling caption
  booted.winToggle.dispatchEvent("click", { target: booted.winFullBtn });
  snap = inspectRequestsPage(booted.root);
  assert.strictEqual(snap.findCircles(snap.scatterSvgs[0]).length, 5); // 4 dots + 1 hover ring
  assert(snap.text.includes("Showing 4 of 2,500 requests (uniformly sampled)"), snap.text);

  // Case 3: Empty steady-state window (profiling.heatmap.ttft === null) must NOT leak full/top-level dots
  const emptyWinPayload = JSON.parse(JSON.stringify(payload));
  emptyWinPayload.client_series.windows.profiling.heatmap.ttft = null;
  emptyWinPayload.client_series.windows.profiling.heatmap.tpot = null;
  const bootedEmpty = await bootReport(emptyWinPayload);
  const snapEmpty = inspectRequestsPage(bootedEmpty.root);
  assert.strictEqual(snapEmpty.scatterSvgs.length, 0);
  assert(snapEmpty.text.includes("TTFT scatter plot unavailable."));
  assert(snapEmpty.text.includes("TPOT scatter plot unavailable."));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(embedded),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v38 scatter runtime check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_html_report_v39_responsive_charts_and_reflow(sim_env: Path) -> None:
    """Offline v39 report reflows all SVG charts across mount, switchPage, ResizeObserver, sidebar, resize, and print."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = Path(str(assets_dir.joinpath("report_template.html"))).read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    for css_snippet in (
        ".grid-2 > :last-child:nth-child(odd)",
        ".chart-box svg, .sat-chart-wrap svg, .report-page svg",
        "@media print",
        "height: auto !important",
    ):
        assert css_snippet in template_html

    cs = _sample_client_series()
    cs["duration_seconds"] = 20.0
    cs["warmup_seconds"] = 2.0
    cs["buckets"] = [
        {
            "start_seconds": 0.0,
            "end_seconds": 5.0,
            "mid_seconds": 2.5,
            "throughput_rps": 10.0,
            "goodput_rps": 9.5,
            "output_tok_per_s": 400.0,
            "in_flight_concurrency": 4.0,
            "ttft_ms": {"min": 40.0, "p50": 80.0, "mean": 85.0, "p90": 120.0, "p99": 150.0, "max": 180.0},
            "tpot_ms": {"min": 8.0, "p50": 12.0, "mean": 13.0, "p90": 18.0, "p99": 22.0, "max": 25.0},
            "e2e_ms": {"min": 200.0, "p50": 400.0, "mean": 420.0, "p90": 600.0, "p99": 720.0, "max": 800.0},
            "drift_ms": {"p90": 3.5},
        },
        {
            "start_seconds": 5.0,
            "end_seconds": 10.0,
            "mid_seconds": 7.5,
            "throughput_rps": 12.0,
            "goodput_rps": 11.8,
            "output_tok_per_s": 480.0,
            "in_flight_concurrency": 6.0,
            "ttft_ms": {"min": 45.0, "p50": 85.0, "mean": 90.0, "p90": 130.0, "p99": 160.0, "max": 190.0},
            "tpot_ms": {"min": 8.5, "p50": 12.5, "mean": 13.5, "p90": 19.0, "p99": 23.0, "max": 26.0},
            "e2e_ms": {"min": 220.0, "p50": 430.0, "mean": 450.0, "p90": 640.0, "p99": 760.0, "max": 850.0},
            "drift_ms": {"p90": 4.0},
        },
    ]
    cs["fidelity_series"] = cs["buckets"]
    cs["drift_histogram"] = [{"label": "0-5ms", "count": 50}, {"label": "5-10ms", "count": 10}]
    cs["prompt_length_buckets"] = [{"label": "0-512", "count": 40, "ttft_ms": {"p50": 70.0, "p90": 110.0}}]
    cs["cache_hit_buckets"] = [{"label": "0-50%", "count": 30, "ttft_ms": {"p50": 95.0, "p90": 140.0}}]
    cs["slot_lanes"] = {
        "peak_slots": 2,
        "total_requests": 2,
        "session_count": 2,
        "sample_stride": 1,
        "tier_counts": {"cold": 1, "partial": 0, "warm": 1},
        "items": [
            {"slot": 0, "start_s": 0.5, "end_s": 3.0, "cache_hit_pct": 0.0, "tier": "cold", "session_id": "s1"},
            {"slot": 1, "start_s": 1.0, "end_s": 4.0, "cache_hit_pct": 85.0, "tier": "warm", "session_id": "s2"},
        ],
    }
    cs["cdf"] = {
        "ttft_ms": [
            {"percentile": 0, "value": 40.0},
            {"percentile": 50, "value": 80.0},
            {"percentile": 100, "value": 180.0},
        ],
        "tpot_ms": [
            {"percentile": 0, "value": 8.0},
            {"percentile": 50, "value": 12.0},
            {"percentile": 100, "value": 25.0},
        ],
        "e2e_ms": [
            {"percentile": 0, "value": 200.0},
            {"percentile": 50, "value": 400.0},
            {"percentile": 100, "value": 800.0},
        ],
        "drift_ms": [
            {"percentile": 0, "value": 1.0},
            {"percentile": 50, "value": 2.5},
            {"percentile": 100, "value": 6.0},
        ],
    }
    cs["heatmap"] = {
        "ttft": {
            "points": [{"t": 1.0, "seq": 128, "ms": 40.0}, {"t": 6.0, "seq": 512, "ms": 110.0}],
            "points_total": 2,
            "points_sampled": 2,
            "duration_seconds": 10.0,
            "sequence_length": "input",
        },
        "tpot": {
            "cells": [
                {
                    "start_seconds": 0.0,
                    "end_seconds": 5.0,
                    "seq_start": 32,
                    "seq_end": 128,
                    "avg_ms": 12.0,
                    "count": 20,
                }
            ],
            "sequence_length": "output",
        },
    }
    cs["windows"]["profiling"]["heatmap"] = cs["heatmap"]
    cs["windows"]["full"]["heatmap"] = cs["heatmap"]
    cs["windows"]["profiling"]["workload"] = {
        "isl": {"min": 64, "p50": 256, "mean": 280, "p90": 512, "p99": 768, "max": 1024},
        "osl": {"min": 16, "p50": 64, "mean": 72, "p90": 128, "p99": 192, "max": 256},
        "isl_histogram": [{"label": "0-512", "count": 40, "share_pct": 80.0}],
        "osl_histogram": [{"label": "0-128", "count": 45, "share_pct": 90.0}],
        "isl_cdf": [{"percentile": 0, "value": 64}, {"percentile": 100, "value": 1024}],
        "osl_cdf": [{"percentile": 0, "value": 16}, {"percentile": 100, "value": 256}],
        "request_points": [{"isl": 256, "osl": 64, "ttft_ms": 80.0, "tpot_ms": 12.0, "t": 1.5, "ok": True}],
        "isl_vs_osl_buckets": [
            {
                "label": "0-512",
                "count": 40,
                "share_pct": 80.0,
                "osl_p50": 64,
                "osl_mean": 70,
                "osl_p90": 120,
                "ratio_p50": 0.25,
                "ratio_mean": 0.28,
            }
        ],
        "concurrency_residency": [{"label": "4", "share_pct": 60.0, "duration_seconds": 6.0}],
        "timeline": [{"t": 2.5, "in_flight_avg": 4.0, "in_flight_max": 6, "arrival_rps": 10.0, "completed_rps": 9.8}],
        "multi_turn": {
            "is_multi_turn": True,
            "session_count": 5,
            "turns_per_session_mean": 3.0,
            "max_turns": 5,
            "reusable_prefix_pct": 50.0,
            "turns_distribution": [{"turns": 3, "sessions": 5, "share_pct": 100.0}],
            "isl_by_turn": [{"label": "Turn 1", "request_count": 5, "isl_p50": 200, "isl_mean": 210, "isl_p90": 250}],
        },
    }
    cs["windows"]["full"]["workload"] = cs["windows"]["profiling"]["workload"]
    cs["windows"]["profiling"]["cold_start_comparison"] = {
        "warmup": {"ttft_ms": {"min": 60.0, "p50": 120.0, "mean": 130.0, "p90": 180.0, "p99": 210.0, "max": 240.0}},
        "steady_state": {"ttft_ms": {"min": 40.0, "p50": 80.0, "mean": 85.0, "p90": 120.0, "p99": 150.0, "max": 180.0}},
    }

    server = _sample_server_telemetry()
    server["series"] = [
        {
            "t": 0.0,
            "running_requests": 3.0,
            "waiting_requests": 1.0,
            "input_token_rate_tps": 1200.0,
            "output_token_rate_tps": 400.0,
            "kv_cache_usage_percent": 40.0,
            "prefix_cache_hit_percent": 50.0,
            "vllm_ttft_p50_ms": 75.0,
            "vllm_tpot_p50_ms": 11.5,
            "gpu_power_watts": 280.0,
            "gpu_utilization_percent": 75.0,
        },
        {
            "t": 5.0,
            "running_requests": 5.0,
            "waiting_requests": 2.0,
            "input_token_rate_tps": 1500.0,
            "output_token_rate_tps": 480.0,
            "kv_cache_usage_percent": 55.0,
            "prefix_cache_hit_percent": 60.0,
            "vllm_ttft_p50_ms": 82.0,
            "vllm_tpot_p50_ms": 12.2,
            "gpu_power_watts": 310.0,
            "gpu_utilization_percent": 82.0,
        },
    ]
    server["per_pod"] = [
        {"pod": "vllm-pod-0", "role": "worker", "gpu_power_watts": {"mean": 280.0, "max": 350.0}},
    ]
    server["system"] = {
        "available": True,
        "nodes": ["node-1"],
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "host_cpu_usage_percent": 50.0,
                "host_memory_used_bytes": 50 * 1024**3,
                "host_memory_available_bytes": 50 * 1024**3,
                "host_disk_read_bytes_per_second": 10 * 1024**2,
                "host_disk_written_bytes_per_second": 5 * 1024**2,
                "host_network_receive_bytes_per_second": 30 * 1024**2,
                "host_network_transmit_bytes_per_second": 20 * 1024**2,
                "host_power_watts": 400.0,
            },
            {
                "timestamp": "2026-01-01T00:00:05Z",
                "host_cpu_usage_percent": 60.0,
                "host_memory_used_bytes": 55 * 1024**3,
                "host_memory_available_bytes": 45 * 1024**3,
                "host_disk_read_bytes_per_second": 12 * 1024**2,
                "host_disk_written_bytes_per_second": 6 * 1024**2,
                "host_network_receive_bytes_per_second": 35 * 1024**2,
                "host_network_transmit_bytes_per_second": 25 * 1024**2,
                "host_power_watts": 420.0,
            },
        ],
    }

    forensics_roles = {
        "verdict": {"code": "healthy", "label": "Healthy"},
        "timeline_buckets": [
            {
                "start_seconds": 0.0,
                "end_seconds": 5.0,
                "roles": {
                    "prefill": {"status": "healthy", "running": 2.0, "waiting": 0.0, "kv_cache_pct": 30.0},
                    "decode": {"status": "healthy", "running": 3.0, "waiting": 1.0, "kv_cache_pct": 55.0},
                },
            },
            {
                "start_seconds": 5.0,
                "end_seconds": 10.0,
                "roles": {
                    "prefill": {"status": "queueing", "running": 3.0, "waiting": 2.0, "kv_cache_pct": 40.0},
                    "decode": {"status": "kv_pressure", "running": 4.0, "waiting": 2.0, "kv_cache_pct": 92.0},
                },
            },
        ],
        "stage_latency_breakdown": {
            "available": True,
            "source": "client_fallback",
            "series": [
                {"t": 2.5, "ttft_stage_ms": 80.0, "decode_time_ms": 320.0},
                {"t": 7.5, "ttft_stage_ms": 85.0, "decode_time_ms": 345.0},
            ],
            "slices": {
                "p50": {
                    "available": True,
                    "ttft_stage_ms": 82.0,
                    "decode_time_ms": 330.0,
                    "share_of_e2e_pct": {"ttft_unattributed": 20.0, "decode_time": 80.0},
                },
            },
        },
    }
    forensics_client_only = {
        "verdict": {"code": "healthy", "label": "Healthy"},
        "timeline_buckets": [
            {
                "start_seconds": 0.0,
                "end_seconds": 5.0,
                "roles": {"client": {"status": "healthy", "failed_requests": 0}},
            },
            {"start_seconds": 5.0, "end_seconds": 10.0, "roles": {"client": {"status": "error", "failed_requests": 2}}},
        ],
    }

    embedded_roles = _extract_embedded_report_json(
        render_html_report(_make_task("0000003a"), client_series=cs, server_telemetry=server, forensics=forensics_roles)
    )
    embedded_client_only = _extract_embedded_report_json(
        render_html_report(
            _make_task("0000003b"), client_series=cs, server_telemetry=server, forensics=forensics_client_only
        )
    )

    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node is unavailable in PATH")

    vm_harness = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));

function runScenario(payload, opts = {}) {
  let viewportWidth = opts.initialViewport || 1440;
  let printModeWidth = opts.printWidth || 700;
  let isPrinting = false;
  let rafQueue = [];
  const windowListeners = {};
  const observerInstances = [];
  let createdObservers = 0;
  let disconnectedObservers = 0;

  class MockResizeObserver {
    constructor(cb) {
      this.cb = cb;
      this.targets = new Set();
      this.disconnected = false;
      createdObservers += 1;
      observerInstances.push(this);
    }
    observe(target) {
      if (!this.disconnected && target) this.targets.add(target);
    }
    disconnect() {
      if (!this.disconnected) {
        this.disconnected = true;
        disconnectedObservers += 1;
      }
      this.targets.clear();
    }
    trigger(widthOverride) {
      if (this.disconnected) return;
      const entries = Array.from(this.targets).map((target) => {
        const w = typeof widthOverride === "number" ? widthOverride : computeWidth(target);
        return { target, contentRect: { width: w, height: 220 } };
      });
      if (entries.length) this.cb(entries);
    }
  }

  let reportRoot = null;
  let sidebarNode = null;

  function computeContentWidth() {
    if (isPrinting) return printModeWidth;
    const sbWidth = sidebarNode && sidebarNode.classList.contains("collapsed") ? 48 : 196;
    return Math.max(320, viewportWidth - sbWidth);
  }

  function computeWidth(node) {
    if (!node) return 0;
    if (typeof node._forcedWidth === "number") return node._forcedWidth;
    // Detached from reportRoot -> 0 (just like detached DOM in browser!)
    if (!reportRoot || (node !== reportRoot && !isDescendant(reportRoot, node))) {
      return 0;
    }
    // Check ancestor chain for hidden .report-page (display: none unless .active or printing)
    let cur = node;
    let inGrid2Cols = 1;
    while (cur) {
      if (cur.classList && cur.classList.contains("report-page")) {
        if (!isPrinting && !cur.classList.contains("active")) return 0;
      }
      if (cur.parentNode && cur.parentNode.classList && cur.parentNode.classList.contains("grid-2")) {
        const grid = cur.parentNode;
        const idx = grid.children.indexOf(cur);
        const isOddLast = idx === grid.children.length - 1 && (grid.children.length % 2 === 1);
        if (!isPrinting && viewportWidth > 900 && !isOddLast) {
          inGrid2Cols = 2;
        }
      }
      cur = cur.parentNode;
    }
    const base = computeContentWidth();
    if (inGrid2Cols === 2) return Math.floor((base - 14) / 2);
    return base;
  }

  function isDescendant(root, target) {
    let cur = target;
    while (cur) {
      if (cur === root) return true;
      cur = cur.parentNode;
    }
    return false;
  }

  function makeNode(tag, text) {
    const listeners = {};
    const attrs = {};
    const classSet = new Set();
    const node = {
      tagName: String(tag || "div").toUpperCase(),
      children: [],
      parentNode: null,
      style: {},
      className: "",
      textContent: text || "",
      listeners,
      get isConnected() {
        return Boolean(reportRoot && (node === reportRoot || isDescendant(reportRoot, node)));
      },
      get clientWidth() {
        return computeWidth(node);
      },
      classList: {
        add(c) {
          classSet.clear();
          String(node.className || "").split(/\s+/).filter(Boolean).forEach((x) => classSet.add(x));
          classSet.add(c);
          node.className = Array.from(classSet).join(" ");
        },
        remove(c) {
          classSet.clear();
          String(node.className || "").split(/\s+/).filter(Boolean).forEach((x) => classSet.add(x));
          classSet.delete(c);
          node.className = Array.from(classSet).join(" ");
        },
        toggle(c, force) {
          classSet.clear();
          String(node.className || "").split(/\s+/).filter(Boolean).forEach((x) => classSet.add(x));
          const on = force === undefined ? !classSet.has(c) : Boolean(force);
          if (on) classSet.add(c); else classSet.delete(c);
          node.className = Array.from(classSet).join(" ");
        },
        contains(c) { return String(node.className || "").split(/\s+/).includes(c); },
      },
      setAttribute(k, v) {
        attrs[k] = String(v);
        if (k === "class") {
          classSet.clear();
          String(v).split(/\s+/).filter(Boolean).forEach((c) => classSet.add(c));
          node.className = String(v);
        }
      },
      getAttribute(k) { return k === "class" ? node.className : (attrs[k] ?? null); },
      appendChild(c) {
        if (c != null) {
          const child = typeof c === "string" ? makeNode("#text", c) : c;
          child.parentNode = node;
          node.children.push(child);
          return child;
        }
        return c;
      },
      append(...items) { for (const i of items) if (i != null) node.appendChild(i); },
      replaceChildren(...items) {
        for (const old of node.children) old.parentNode = null;
        node.children = [];
        node.append(...items);
      },
      addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
      dispatchEvent(ev, evtObj) { for (const fn of listeners[ev] || []) fn(evtObj || { target: node }); },
      click() { node.dispatchEvent("click", { target: node, stopPropagation() {}, preventDefault() {} }); },
      closest(sel) {
        let cur = node;
        while (cur) {
          if (sel === "button[data-window]" && cur.tagName === "BUTTON" && cur.getAttribute("data-window")) return cur;
          if (sel === ".nav-btn[data-page]" && cur.classList.contains("nav-btn") && cur.getAttribute("data-page")) return cur;
          if (sel === ".subnav a[data-page]" && cur.tagName === "A" && cur.getAttribute("data-page")) return cur;
          cur = cur.parentNode;
        }
        return null;
      },
      querySelectorAll(sel) {
        const out = [];
        (function walk(n) {
          for (const ch of n.children || []) {
            if (sel === "button" && ch.tagName === "BUTTON") out.push(ch);
            walk(ch);
          }
        })(node);
        return out;
      },
      getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
      getBoundingClientRect() {
        const w = computeWidth(node);
        return { left: 0, top: 0, right: w, bottom: 220, width: w, height: w > 0 ? 220 : 0 };
      },
    };
    return node;
  }

  function findAll(root, pred, out = []) {
    if (!root || typeof root !== "object") return out;
    if (pred(root)) out.push(root);
    for (const ch of root.children || []) findAll(ch, pred, out);
    return out;
  }

  reportRoot = makeNode("main");
  reportRoot.setAttribute("id", "report-root");
  sidebarNode = makeNode("aside");
  sidebarNode.setAttribute("id", "sidebar");

  const winProfBtn = makeNode("button", "Profiling Steady-State");
  winProfBtn.setAttribute("data-window", "profiling");
  winProfBtn.classList.add("active");
  const winFullBtn = makeNode("button", "Full Run");
  winFullBtn.setAttribute("data-window", "full");
  const winToggle = makeNode("div");
  winToggle.append(winProfBtn, winFullBtn);

  const navList = makeNode("nav");
  const navBtns = {};
  const navGroups = {};
  for (const pid of ["overview", "workload", "bottlenecks", "serving", "requests", "system", "hardware", "config"]) {
    const grp = makeNode("div");
    grp.classList.add("nav-group");
    grp.setAttribute("data-page", pid);
    const btn = makeNode("button", pid);
    btn.classList.add("nav-btn");
    btn.setAttribute("data-page", pid);
    if (pid === "overview") {
      grp.classList.add("open");
      btn.classList.add("active");
    }
    grp.append(btn);
    navList.append(grp);
    navBtns[pid] = btn;
    navGroups[pid] = grp;
  }

  const ids = {
    "chart-tooltip": makeNode("div"),
    "report-root": reportRoot,
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": makeNode("script", JSON.stringify(payload)),
    "sidebar-toggle": makeNode("button"),
    "sidebar": sidebarNode,
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": winToggle,
    "nav-list": navList,
  };

  const doc = {
    documentElement: makeNode("html"),
    getElementById(id) { return ids[id] || makeNode("div"); },
    createElement(t) { return makeNode(t); },
    createElementNS(_ns, t) { return makeNode(t); },
    createTextNode(t) { return makeNode("#text", t); },
    querySelectorAll(sel) {
      if (sel === ".report-page") return reportRoot.children.slice();
      if (sel === ".nav-btn") return Object.values(navBtns);
      if (sel === ".nav-group") return Object.values(navGroups);
      if (sel === "#window-toggle button") return [winProfBtn, winFullBtn];
      return [];
    },
  };

  const windowObj = {
    devicePixelRatio: 1,
    get innerWidth() { return viewportWidth; },
    innerHeight: 900,
    addEventListener(ev, fn) { (windowListeners[ev] = windowListeners[ev] || []).push(fn); },
    dispatchEvent(ev, evtObj) { for (const fn of windowListeners[ev] || []) fn(evtObj || {}); },
    print() {},
  };

  const context = {
    console,
    document: doc,
    window: windowObj,
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    ResizeObserver: opts.useResizeObserver === false ? undefined : MockResizeObserver,
    requestAnimationFrame(fn) { rafQueue.push(fn); return rafQueue.length; },
    setTimeout, clearTimeout,
    URL, Blob, Response, DecompressionStream: globalThis.DecompressionStream, atob,
  };
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);

  function flushRaf() {
    let guard = 0;
    while (rafQueue.length && guard++ < 10) {
      const batch = rafQueue.splice(0, rafQueue.length);
      for (const fn of batch) fn();
    }
  }

  function svgViewBoxWidth(svg) {
    const vb = String(svg.getAttribute("viewBox") || "");
    const parts = vb.trim().split(/\s+/);
    assert.strictEqual(parts.length, 4, `Invalid viewBox: ${vb}`);
    assert.strictEqual(svg.getAttribute("width"), "100%", "SVG width must be 100%");
    return Number(parts[2]);
  }

  function assertPageChartsClean(pageId, expectedFullW, expectedHalfW) {
    const page = reportRoot.children.find((n) => n.getAttribute("data-page") === pageId);
    assert(page, `Missing page ${pageId}`);
    const boxes = findAll(page, (n) => {
      const cls = String(n.className || "").split(/\s+/);
      return cls.includes("chart-box") || cls.includes("sat-chart-wrap");
    });
    const svgs = findAll(page, (n) => n.tagName === "SVG");
    assert(svgs.length > 0, `Expected SVGs on page ${pageId}`);
    for (const box of boxes) {
      const boxSvgs = findAll(box, (n) => n.tagName === "SVG");
      const boxLegends = findAll(box, (n) => String(n.className || "").split(/\s+/).includes("chart-legend"));
      assert(boxSvgs.length <= 1, `Duplicate SVG in ${pageId} chart container: ${boxSvgs.length}`);
      assert(boxLegends.length <= 1, `Duplicate legend in ${pageId} chart container: ${boxLegends.length}`);
    }
    for (const svg of svgs) {
      const w = svgViewBoxWidth(svg);
      assert(
        w === expectedFullW || w === expectedHalfW,
        `Page ${pageId} SVG width=${w} did not match full=${expectedFullW} or half=${expectedHalfW}`
      );
    }
    return svgs;
  }

  return {
    reportRoot, sidebarNode, navList, navBtns, winToggle, winProfBtn, winFullBtn, ids,
    windowObj, observerInstances,
    getObserverStats: () => ({ created: createdObservers, disconnected: disconnectedObservers, active: createdObservers - disconnectedObservers }),
    setViewport(w) { viewportWidth = w; },
    setPrinting(flag) { isPrinting = flag; },
    findAll, flushRaf, svgViewBoxWidth, assertPageChartsClean,
  };
}

(async () => {
  // 1. Multi-page responsive lifecycle with ResizeObserver + rAF
  const app = runScenario(input.roles, { initialViewport: 1440, printWidth: 700 });
  await new Promise((r) => setTimeout(r, 20));
  // Initial render ran while detached (0px -> fallback 540/560), then flushRaf reflows active overview page to 1244px
  app.flushRaf();
  app.assertPageChartsClean("overview", 1244, 615);

  // Switch to every hidden page (previously 0px -> fallback) and confirm immediate reflow to 1244 / 615
  for (const pid of ["workload", "bottlenecks", "serving", "requests", "system"]) {
    app.navList.dispatchEvent("click", { target: app.navBtns[pid] });
    app.flushRaf();
    const svgs = app.assertPageChartsClean(pid, 1244, 615);
    if (pid === "requests") {
      const textOf = (n) => (n ? (n.textContent || "") + (n.children || []).map(textOf).join("") : "");
      // SwimlaneCanvas + scatter + legacy binned heatmap + 4 CDFs
      const swimSvg = svgs.find((s) => s.children.some((c) => c.tagName === "TEXT" && textOf(c) === "S1"));
      assert(swimSvg, "Missing Swimlane SVG on requests page");
      assert.strictEqual(app.svgViewBoxWidth(swimSvg), 1244);
      const scatterHm = svgs.find((s) => String(s.getAttribute("aria-label") || "").includes("per-request scatter plot"));
      const legacyHm = svgs.find((s) => String(s.getAttribute("aria-label") || "").includes("legacy binned heatmap"));
      assert(scatterHm && legacyHm, "Expected both scatter and legacy binned heatmaps on requests page");
      assert.strictEqual(app.svgViewBoxWidth(scatterHm), 615);
      assert.strictEqual(app.svgViewBoxWidth(legacyHm), 615);
    }
  }

  // 2. Sidebar collapse + transitionend updates visible charts on active page (1244 -> 1392)
  app.ids["sidebar-toggle"].click();
  assert(app.sidebarNode.classList.contains("collapsed"), "Sidebar should toggle collapsed class");
  app.sidebarNode.dispatchEvent("transitionend", { propertyName: "width" });
  app.flushRaf();
  app.assertPageChartsClean("system", 1392, 689);

  // 3. ResizeObserver resize 600px -> 1320px updates all chart types
  app.navList.dispatchEvent("click", { target: app.navBtns.requests });
  app.flushRaf();
  const reqPage = app.reportRoot.children.find((n) => n.getAttribute("data-page") === "requests");
  const reqContainers = app.findAll(reqPage, (n) => {
    const cls = String(n.className || "").split(/\s+/);
    return (cls.includes("chart-box") || n.getAttribute("style") === "width:100%;min-width:0;") && n.children.length > 0;
  });
  assert(reqContainers.length >= 8, `Expected >=8 responsive chart containers on requests, got ${reqContainers.length}`);
  for (const c of reqContainers) c._forcedWidth = 600;
  for (const ro of app.observerInstances) ro.trigger(600);
  app.assertPageChartsClean("requests", 600, 600);

  for (const c of reqContainers) c._forcedWidth = 1320;
  for (const ro of app.observerInstances) ro.trigger(1320);
  app.assertPageChartsClean("requests", 1320, 1320);
  for (const c of reqContainers) delete c._forcedWidth;

  // 4. Multiple resizes do NOT leak crosshairSubscribers (check system page 4 LineBandCharts)
  app.navList.dispatchEvent("click", { target: app.navBtns.system });
  app.flushRaf();
  const sysPage = app.reportRoot.children.find((n) => n.getAttribute("data-page") === "system");
  const sysBoxes = app.findAll(sysPage, (n) => String(n.className || "").split(/\s+/).includes("chart-box"));
  assert.strictEqual(sysBoxes.length, 4);
  for (const w of [700, 950, 1180, 1400]) {
    for (const b of sysBoxes) b._forcedWidth = w;
    for (const ro of app.observerInstances) ro.trigger(w);
  }
  for (const b of sysBoxes) delete b._forcedWidth;
  // Trigger mousemove on first system chart overlay and verify exactly 4 active crosshairs respond
  const sysSvgs = app.findAll(sysPage, (n) => n.tagName === "SVG");
  const overlays = sysSvgs.map((s) => s.children.find((c) => c.tagName === "RECT" && c.listeners && c.listeners.mousemove));
  overlays[0].dispatchEvent("mousemove", { clientX: 250, clientY: 100 });
  const visibleCrosshairs = sysSvgs.filter((s) =>
    s.children.some((c) => c.tagName === "LINE" && c.getAttribute("visibility") === "visible")
  );
  assert.strictEqual(visibleCrosshairs.length, 4, `Expected 4 active crosshairs, got ${visibleCrosshairs.length}`);

  // 5. Debounced window resize updates width
  app.setViewport(1600); // collapsed sidebar (48) -> 1552
  app.windowObj.dispatchEvent("resize", {});
  await new Promise((r) => setTimeout(r, 150));
  app.flushRaf();
  app.assertPageChartsClean("system", 1552, 769);

  // 6. beforeprint synchronously reflows visible charts to print width (no rAF wait needed)
  app.setPrinting(true);
  app.windowObj.dispatchEvent("beforeprint", {});
  app.assertPageChartsClean("system", 700, 700);
  app.setPrinting(false);
  app.windowObj.dispatchEvent("afterprint", {});
  app.flushRaf();
  app.assertPageChartsClean("system", 1552, 769);

  // 7. Window toggle (profiling <-> full) cleans up all prior ResizeObservers without leaking
  const statsBeforeToggle = app.getObserverStats();
  assert(statsBeforeToggle.active > 0);
  app.winToggle.dispatchEvent("click", { target: app.winFullBtn });
  app.flushRaf();
  const statsAfterFull = app.getObserverStats();
  app.winToggle.dispatchEvent("click", { target: app.winProfBtn });
  app.flushRaf();
  const statsAfterProf = app.getObserverStats();
  assert.strictEqual(statsAfterFull.active, statsBeforeToggle.active, "Full window toggle leaked ResizeObservers");
  assert.strictEqual(statsAfterProf.active, statsBeforeToggle.active, "Profiling window toggle leaked ResizeObservers");

  // 8. Client-only saturation timeline fallback (renderClientOnlyTimeline) also reflows cleanly
  const co = runScenario(input.clientOnly, { initialViewport: 1280 });
  await new Promise((r) => setTimeout(r, 20));
  co.flushRaf();
  co.navList.dispatchEvent("click", { target: co.navBtns.bottlenecks });
  co.flushRaf();
  const bPage = co.reportRoot.children.find((n) => n.getAttribute("data-page") === "bottlenecks");
  const coSvg = co.findAll(bPage, (n) => n.tagName === "SVG")[0];
  assert(coSvg, "Missing client-only timeline SVG");
  assert.strictEqual(co.svgViewBoxWidth(coSvg), 1084);
  for (const ro of co.observerInstances) ro.trigger(1320);
  const coSvgResized = co.findAll(bPage, (n) => n.tagName === "SVG")[0];
  assert.strictEqual(co.svgViewBoxWidth(coSvgResized), 1320);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"roles": embedded_roles, "clientOnly": embedded_client_only}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v39 responsive runtime check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_v40_swimlane_session_highlighting_node_vm() -> None:
    """Offline v40 SwimlaneCanvas highlights all turns of a clicked session across reflows."""
    import llm_d_lens.simulation.reporting.builder as report_builder

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js not installed")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = Path(str(assets_dir.joinpath("report_template.html"))).read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(process.argv[1], "utf8").replace(
  /\}\)\(\);\s*$/,
  "\n  globalThis.__testExports = { SwimlaneCanvas };\n})();\n"
);

function makeNode(tag) {
  const node = {
    tagName: String(tag || "").toUpperCase(),
    attributes: {},
    children: [],
    listeners: {},
    style: {},
    className: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    textContent: "",
    isConnected: true,
    _forcedWidth: 720,
    focusCalls: 0,
    focusOpts: [],
    focus(opts) {
      this.focusCalls += 1;
      this.focusOpts.push(opts || null);
    },
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
    },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k); },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          this.children.push(t);
        } else {
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    removeEventListener(evt, fn) {
      if (!this.listeners[evt]) return;
      this.listeners[evt] = this.listeners[evt].filter((f) => f !== fn);
    },
    dispatchEvent(evt, evtObj) { for (const fn of (this.listeners[evt] || []).slice()) fn(evtObj || { target: this }); },
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    remove() {},
    scrollIntoView() {},
    getContext() { return { scale() {}, fillRect() {}, fillText() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; },
    getBoundingClientRect() { return { left: 0, top: 0, width: this._forcedWidth, height: 240, right: this._forcedWidth, bottom: 240 }; },
  };
  return node;
}

function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}

function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").trim();
}

const observers = [];
class MockResizeObserver {
  constructor(cb) { this.cb = cb; this.target = null; observers.push(this); }
  observe(target) { this.target = target; }
  disconnect() { this.target = null; }
  trigger(width) {
    if (!this.target) return;
    this.target._forcedWidth = width;
    this.cb([{ target: this.target, contentRect: { width, height: 240 } }]);
  }
}

const tipNode = makeNode("div");
const ids = {
  "chart-tooltip": tipNode,
  "report-root": makeNode("main"),
  "report-title": makeNode("span"),
  "report-meta": makeNode("div"),
  "status-badges": makeNode("span"),
  "lens-report-data": (() => { const s = makeNode("script"); s.textContent = "{}"; return s; })(),
  "sidebar-toggle": makeNode("button"),
  "sidebar": makeNode("aside"),
  "theme-toggle": makeNode("button"),
  "print-btn": makeNode("button"),
  "window-toggle": makeNode("div"),
  "nav-list": makeNode("nav"),
};
const doc = {
  documentElement: makeNode("html"),
  body: makeNode("body"),
  createElement: (t) => makeNode(t),
  createElementNS: (_ns, t) => makeNode(t),
  createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
  getElementById: (id) => ids[id] || makeNode("div"),
  querySelectorAll: () => [],
};
const cssVars = {
  "--border": "#273244",
  "--text-muted": "#93a4bd",
  "--text": "#e6edf6",
  "--bg-card": "#171f2c",
};
const context = {
  document: doc,
  window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
  getComputedStyle: () => ({ getPropertyValue: (name) => cssVars[name] || "#93a4bd" }),
  navigator: {},
  TextDecoder, Uint8Array,
  atob: (s) => Buffer.from(s, "base64").toString("binary"),
  URL, Blob, Response,
  DecompressionStream: globalThis.DecompressionStream,
  ResizeObserver: MockResizeObserver,
  requestAnimationFrame: (fn) => fn(),
  setTimeout, clearTimeout, console,
  Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
  isFinite, parseFloat, parseInt,
};
context.window.document = doc;

vm.runInNewContext(src, context);
const { SwimlaneCanvas } = context.__testExports;

const host = makeNode("div");
host._forcedWidth = 720;
const clientSeries = {
  duration_seconds: 100,
  slot_lanes: {
    peak_slots: 4,
    total_requests: 7,
    session_count: 2,
    sample_stride: 1,
    items: [
      { slot: 0, start_s: 2, end_s: 8, cache_hit_pct: 5, tier: "cold", session_id: "sess-a", turn: 1 },
      { slot: 1, start_s: 12, end_s: 12.01, cache_hit_pct: 50, tier: "partial", session_id: "sess-a", turn: 2 },
      { slot: 2, start_s: 20, end_s: 30, cache_hit_pct: 90, tier: "warm", session_id: "sess-a", turn: 3 },
      { slot: 3, start_s: 5, end_s: 15, cache_hit_pct: 10, tier: "partial", session_id: "sess-b", turn: 1 },
      { slot: 0, start_s: 35, end_s: 45, cache_hit_pct: 80, tier: "warm", session_id: "sess-b", turn: 2 },
      { slot: 1, start_s: 50, end_s: 60, cache_hit_pct: 0, tier: "cold", session_id: null },
      { slot: 2, start_s: 65, end_s: 75, cache_hit_pct: 0, tier: "cold", session_id: "" },
    ],
  },
};

SwimlaneCanvas(host, clientSeries, null, 0);

const getSvg = () => host.children[1].children[0];
const getTopStrip = () => host.children[0];
const barsForSession = (sid) => findAll(getSvg(), (n) => n.tagName === "RECT" && n.getAttribute("data-session-id") === sid);
const sessionlessBars = () => findAll(
  getSvg(),
  (n) => n.tagName === "RECT" && n.getAttribute("rx") != null && n.getAttribute("data-session-id") == null && n.getAttribute("pointer-events") !== "none"
);

// 1. Initial unselected state (mount does not steal focus)
assert.strictEqual(getSvg().focusCalls, 0, "Initial mount must not call svg.focus");
assert.strictEqual(barsForSession("sess-a").length, 3);
assert.strictEqual(barsForSession("sess-b").length, 2);
assert.strictEqual(sessionlessBars().length, 2);
assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);
assert.strictEqual(findAll(getTopStrip(), (n) => n.hasAttribute("data-session-pill")).length, 0);

// 2. Clicking a sessionless bar when unselected is a no-op
for (const slBar of sessionlessBars()) {
  slBar.dispatchEvent("click", { stopPropagation() {} });
  assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);
  assert.strictEqual(findAll(getTopStrip(), (n) => n.hasAttribute("data-session-pill")).length, 0);
}

// 3. Hovering unselected multi-turn bar shows Turn k of M and highlight hint
const firstBarA = barsForSession("sess-a")[0];
firstBarA.dispatchEvent("mouseenter", { clientX: 100, clientY: 100 });
assert(tipNode.textContent.includes("Turn 1 of 3"), `Expected Turn 1 of 3 in tooltip, got: ${tipNode.textContent}`);
assert(tipNode.textContent.includes("Click to highlight all 3 turns"), `Expected click hint in tooltip, got: ${tipNode.textContent}`);
firstBarA.dispatchEvent("mouseleave", {});

// 4. Clicking Turn 1 of sess-a highlights all 3 turns, dims others, adds connectors & pill
let stopPropCalled = false;
firstBarA.dispatchEvent("click", { stopPropagation() { stopPropCalled = true; } });
assert(stopPropCalled, "Expected bar click to stopPropagation");
assert.strictEqual(getSvg().focusCalls, 1, "Selecting a session by bar click must focus the newly rendered SVG");
assert.strictEqual(Boolean(getSvg().focusOpts[0] && getSvg().focusOpts[0].preventScroll), true);

const selGroup = findAll(getSvg(), (n) => n.getAttribute("data-selected-session") === "sess-a")[0];
assert(selGroup, "Missing <g data-selected-session='sess-a'>");
const selBars = findAll(selGroup, (n) => n.tagName === "RECT" && n.getAttribute("data-session-id") === "sess-a");
assert.strictEqual(selBars.length, 3, "Expected all 3 sess-a turns inside selected group");
for (const b of selBars) {
  assert.strictEqual(b.getAttribute("fill-opacity"), "1");
  assert.strictEqual(b.getAttribute("stroke"), "#171f2c");
  assert.strictEqual(b.getAttribute("stroke-width"), "0.75");
  assert(Number(b.getAttribute("width")) >= 4, `Expected min width >= 4, got ${b.getAttribute("width")}`);
}
const halos = findAll(selGroup, (n) => n.tagName === "RECT" && n.getAttribute("fill") === "none");
assert.strictEqual(halos.length, 3, "Expected 3 outer halo rects");
for (const h of halos) {
  assert.strictEqual(h.getAttribute("stroke"), "#e6edf6");
  assert.strictEqual(h.getAttribute("stroke-opacity"), "0.95");
  assert.strictEqual(h.getAttribute("stroke-width"), "1.75");
  assert.strictEqual(h.getAttribute("pointer-events"), "none");
}

const connGroup = findAll(getSvg(), (n) => n.getAttribute("data-session-connectors") === "sess-a")[0];
assert(connGroup, "Missing <g data-session-connectors='sess-a'>");
assert.strictEqual(connGroup.getAttribute("pointer-events"), "none");
const connLines = findAll(connGroup, (n) => n.tagName === "LINE");
assert.strictEqual(connLines.length, 2, "Expected 2 chronological inter-turn connector lines for 3 turns");
for (const ln of connLines) {
  assert.strictEqual(ln.getAttribute("stroke-dasharray"), "3 3");
  assert.strictEqual(ln.getAttribute("stroke-opacity"), "0.55");
  assert.strictEqual(ln.getAttribute("pointer-events"), "none");
}

const dimmedBars = barsForSession("sess-b").concat(sessionlessBars());
assert.strictEqual(dimmedBars.length, 4);
for (const db of dimmedBars) {
  assert.strictEqual(db.getAttribute("fill-opacity"), "0.16");
  db.dispatchEvent("mouseenter", { clientX: 50, clientY: 50 });
  assert.strictEqual(db.getAttribute("fill-opacity"), "0.45");
  db.dispatchEvent("mouseleave", {});
  assert.strictEqual(db.getAttribute("fill-opacity"), "0.16");
}

const pill = findAll(getTopStrip(), (n) => n.getAttribute("data-session-pill") === "sess-a")[0];
assert(pill, "Missing selection pill");
assert(nodeText(pill).includes("Selected session: sess-a · 3 turns"), `Unexpected pill text: ${nodeText(pill)}`);
const clearBtn = findAll(pill, (n) => n.tagName === "BUTTON")[0];
assert(clearBtn && nodeText(clearBtn) === "Clear", "Missing Clear button in pill");

// Hovering selected bar shows clear hint
selBars[0].dispatchEvent("mouseenter", { clientX: 100, clientY: 100 });
assert(tipNode.textContent.includes("Click again to clear highlight"), `Expected clear hint, got: ${tipNode.textContent}`);

// 5. ResizeObserver width reflow preserves selectedSessionId
const hostObserver = observers.find((o) => o.target === host);
assert(hostObserver, "Expected ResizeObserver attached to host");
hostObserver.trigger(1080);
assert.strictEqual(getSvg().getAttribute("viewBox").split(/\s+/)[2], "1080");
assert.strictEqual(getSvg().focusCalls, 0, "ResizeObserver reflow must not steal focus via svg.focus");
assert(findAll(getSvg(), (n) => n.getAttribute("data-selected-session") === "sess-a").length === 1, "Selection lost on resize");
assert(findAll(getSvg(), (n) => n.getAttribute("data-session-connectors") === "sess-a").length === 1, "Connectors lost on resize");
assert(findAll(getTopStrip(), (n) => n.getAttribute("data-session-pill") === "sess-a").length === 1, "Pill lost on resize");

// 6. Clear button deselects
let clearStopped = false;
findAll(getTopStrip(), (n) => n.tagName === "BUTTON")[0].dispatchEvent("click", { stopPropagation() { clearStopped = true; } });
assert(clearStopped, "Clear button must stopPropagation");
assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);
assert.strictEqual(findAll(getTopStrip(), (n) => n.hasAttribute("data-session-pill")).length, 0);

// 7. Click-again on same/sibling turn deselects
barsForSession("sess-a")[0].dispatchEvent("click", { stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.getAttribute("data-selected-session") === "sess-a").length, 1);
barsForSession("sess-a")[2].dispatchEvent("click", { stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);

// 8. Background SVG click deselects
barsForSession("sess-b")[0].dispatchEvent("click", { stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.getAttribute("data-selected-session") === "sess-b").length, 1);
getSvg().dispatchEvent("click", { target: getSvg() });
assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);

// 9. Escape keydown on SVG and host container deselects
barsForSession("sess-a")[0].dispatchEvent("click", { stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.getAttribute("data-selected-session") === "sess-a").length, 1);
getSvg().dispatchEvent("keydown", { key: "Escape", stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);

barsForSession("sess-a")[0].dispatchEvent("click", { stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.getAttribute("data-selected-session") === "sess-a").length, 1);
host.dispatchEvent("keydown", { key: "Escape" });
assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);

// 10. Clicking a sessionless bar when active clears selection without selecting sessionless bars
barsForSession("sess-a")[0].dispatchEvent("click", { stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.getAttribute("data-selected-session") === "sess-a").length, 1);
sessionlessBars()[0].dispatchEvent("click", { stopPropagation() {} });
assert.strictEqual(findAll(getSvg(), (n) => n.hasAttribute("data-selected-session")).length, 0);
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v40 SwimlaneCanvas interaction check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_compact_report_payload_slot_lanes_preserves_whole_sessions() -> None:
    """builder._compact_report_payload preserves whole multi-turn sessions when slot_lanes exceeds 2000 items."""
    from llm_d_lens.simulation.reporting.builder import _compact_report_payload

    turns_per_session = 4
    num_sessions = 650  # 2600 items > 2000 limit
    items = []
    for s_idx in range(num_sessions):
        sid = f"sess-{s_idx:04d}"
        for turn in range(1, turns_per_session + 1):
            start_s = float(s_idx * 0.2 + (turn - 1) * 0.05)
            items.append(
                {
                    "slot": s_idx % 16,
                    "start_s": start_s,
                    "end_s": start_s + 0.03,
                    "cache_hit_pct": 80.0 if turn > 1 else 0.0,
                    "tier": "warm" if turn > 1 else "cold",
                    "session_id": sid,
                    "turn": turn,
                }
            )

    payload = {
        "client_series": {
            "duration_seconds": 200.0,
            "slot_lanes": {
                "peak_slots": 16,
                "total_requests": len(items),
                "session_count": num_sessions,
                "sample_stride": 2,
                "items": items,
            },
        }
    }

    compacted = _compact_report_payload(payload)
    slot_lanes = compacted["client_series"]["slot_lanes"]
    compacted_items = slot_lanes["items"]

    assert 0 < len(compacted_items) <= 2000
    assert slot_lanes["sample_stride"] >= 4

    counts_by_sid: dict[str, int] = {}
    for it in compacted_items:
        sid = str(it["session_id"])
        counts_by_sid[sid] = counts_by_sid.get(sid, 0) + 1

    assert len(counts_by_sid) > 0
    for sid, count in counts_by_sid.items():
        assert count == turns_per_session, f"Session {sid} was split during compaction: {count} != {turns_per_session}"


def test_v41_payload_and_manifest_completeness(sim_env: Path) -> None:
    """v41 render_run_manifest and render_html_report embed complete dataset and replay fields."""
    from llm_d_lens.simulation.reporting.builder import render_run_manifest

    base = _make_task(
        "00000046",
        backend="aiperf",
        fixed_schedule=False,
        backend_options={"concurrency": 16, "warmup_seconds": 10.0},
    )
    prompt = SimulationPrompt(
        type="trace",
        dataset=SimulationDataset(name=None, scenario="api-calling", tokenizer=None),
        trace=SimulationTrace(
            path="/traces/bailian_prod_slice.jsonl",
            format="bailian_trace",
            fixed_schedule=False,
            timeout_seconds=900,
            synthesis_speedup_ratio=1.5,
            start_seconds=5.0,
            end_seconds=95.0,
        ),
    )
    sim_cfg = SimulationConfig(
        backend="aiperf",
        backend_options={"concurrency": 16, "warmup_seconds": 10.0},
        duration_seconds=60.0,
        num_requests=None,
        stream=True,
        warmup_enabled=False,
        grace_period_seconds=20.0,
    )
    task = base.model_copy(
        update={
            "scenario": "api-calling",
            "prompt": prompt,
            "simulation": sim_cfg,
        }
    )

    manifest = render_run_manifest(task)
    assert manifest["dataset"]["name"] is None
    assert manifest["dataset"]["format"] == "bailian_trace"
    assert manifest["dataset"]["scenario"] == "api-calling"
    assert manifest["replay"]["start_seconds"] == 5.0
    assert manifest["replay"]["end_seconds"] == 95.0
    assert manifest["replay"]["fixed_schedule"] is False
    assert manifest["replay"]["scale_factor"] == 1.5
    assert manifest["replay"]["duration_seconds"] == 60.0
    assert manifest["replay"]["warmup_seconds"] == 10.0
    assert manifest["replay"]["grace_period_seconds"] == 20.0
    assert manifest["replay"]["concurrency"] == 16
    assert manifest["replay"]["backend_options"]["concurrency"] == 16

    html_text = render_html_report(
        task,
        client_series=_sample_client_series(),
        server_telemetry=_sample_server_telemetry(),
        forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
    )
    embedded = _extract_embedded_report_json(html_text)
    t = embedded["task"]
    assert t["dataset"] == "bailian_prod_slice.jsonl"
    assert t["trace_format"] == "bailian_trace"
    assert t["scenario"] == "api-calling"
    assert t["scale_factor"] == 1.5
    assert t["duration_seconds"] == 60.0
    assert t["warmup_seconds"] == 10.0
    assert t["grace_period_seconds"] == 20.0
    assert t["concurrency"] == 16
    assert t["start_seconds"] == 5.0
    assert t["end_seconds"] == 95.0
    assert t["fixed_schedule"] is False


def test_v41_workload_config_section_and_node_dom(sim_env: Path) -> None:
    """v41 Workload page renders #w-config parity summary across concurrency, fixed-schedule, unbounded, and summary-only runs."""
    from llm_d_lens.simulation.reporting import builder as report_builder

    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    assert template_html.splitlines()[1].strip() == "<!-- lens-report-v42 -->"
    w_cfg_pos = template_html.find('href="#w-config" data-page="workload">Configuration</a>')
    w_kpi_pos = template_html.find('href="#w-kpis" data-page="workload">Summary</a>')
    assert 0 < w_cfg_pos < w_kpi_pos

    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "fetch(",
    ):
        assert forbidden not in charts_js
        assert forbidden not in template_html

    for brand in ("TGI", "SGLang", "Triton", "NVIDIA", "DCGM", "NCCL"):
        assert not re.search(rf"\b{brand}\b", charts_js, re.IGNORECASE) or brand in ("NVIDIA",), (
            f"Unexpected brand {brand}"
        )

    w_config_keys = (
        "w-config",
        "Dataset",
        "Replay Time Range",
        "Replay Duration & Warmup",
        "Pacing Mode",
        "Configured Concurrency",
        "Scale Factor",
    )
    for key in w_config_keys:
        assert charts_js.count(f'"{key}":') == 2, f"Expected HELP_TEXTS + HELP_FORMULAS entries for: {key}"
        assert "GPU" not in key

    def _build_payload(
        task_id: str,
        *,
        dataset_name: str | None = "bailian-tool-trace",
        trace_path: str = "bailian_tool.jsonl",
        trace_format: str = "bailian_trace",
        scenario: str = "api-calling",
        backend: str = "aiperf",
        fixed_schedule: bool = False,
        scale_factor: float = 1.5,
        start_seconds: float = 5.0,
        end_seconds: float | None = 95.0,
        duration_seconds: float = 60.0,
        warmup_seconds: float = 10.0,
        grace_period_seconds: float = 20.0,
        concurrency: int | None = 16,
        with_workload: bool = True,
    ) -> dict[str, object]:
        opts: dict[str, object] = {"warmup_seconds": warmup_seconds}
        if concurrency is not None:
            opts["concurrency"] = concurrency
        base = _make_task(task_id, backend=backend, fixed_schedule=fixed_schedule, backend_options=opts)
        task = base.model_copy(
            update={
                "scenario": scenario,
                "prompt": SimulationPrompt(
                    type="trace",
                    dataset=SimulationDataset(name=dataset_name, scenario=scenario, tokenizer=None),
                    trace=SimulationTrace(
                        path=trace_path,
                        format=trace_format,
                        fixed_schedule=fixed_schedule,
                        timeout_seconds=900,
                        synthesis_speedup_ratio=scale_factor,
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                    ),
                ),
                "simulation": SimulationConfig(
                    backend=backend,
                    backend_options=opts,
                    duration_seconds=duration_seconds,
                    num_requests=None,
                    stream=True,
                    warmup_enabled=False,
                    grace_period_seconds=grace_period_seconds,
                ),
            }
        )
        cs = {
            **_sample_client_series(),
            "duration_seconds": duration_seconds,
            "warmup_seconds": warmup_seconds,
        }
        if with_workload:
            cs["workload"] = {
                "fallback_to_full": False,
                "fallback_synthesized": False,
                "windows": {
                    "profiling": _sample_workload_window(duration_seconds=max(1.0, duration_seconds - warmup_seconds)),
                    "full": _sample_workload_window(duration_seconds=duration_seconds),
                },
            }
        html_text = render_html_report(
            task,
            client_series=cs,
            server_telemetry=_sample_server_telemetry(),
            forensics={"verdict": {"code": "healthy", "label": "Healthy"}},
        )
        assert "<!-- lens-report-v42 -->" in html_text
        return _extract_embedded_report_json(html_text)

    emb_conc = _build_payload("00000047")
    emb_weka = _build_payload(
        "00000048",
        dataset_name="weka-coding-corpus",
        trace_format="weka_public_dataset",
        scenario="coding",
        fixed_schedule=True,
        concurrency=None,
        scale_factor=1.0,
        start_seconds=0.0,
        end_seconds=None,
        duration_seconds=60.0,
        warmup_seconds=5.0,
        grace_period_seconds=0.0,
    )
    emb_fixed = _build_payload(
        "00000049",
        dataset_name="mooncake-chat",
        trace_format="mooncake_trace",
        scenario="chat",
        fixed_schedule=True,
        concurrency=None,
        scale_factor=2.0,
        start_seconds=10.0,
        end_seconds=None,
        duration_seconds=40.0,
        warmup_seconds=5.0,
        grace_period_seconds=0.0,
    )
    emb_replayer = _build_payload(
        "0000004a",
        dataset_name="legacy-replayer-trace",
        trace_format="mooncake_trace",
        scenario="chat",
        backend="trace-replayer",
        fixed_schedule=False,
        concurrency=32,
        scale_factor=1.0,
        start_seconds=0.0,
        end_seconds=None,
        duration_seconds=50.0,
        warmup_seconds=0.0,
        grace_period_seconds=0.0,
    )
    emb_unbounded = {
        "task": {
            "id": "0000004b",
            "name": "Unbounded Legacy",
            "dataset": "unbounded-trace",
            "trace_format": "mooncake_trace",
            "scenario": "custom_agentic",
            "start_seconds": 25.0,
            "end_seconds": None,
            "duration_seconds": None,
            "warmup_seconds": 0.0,
            "scale_factor": 1.0,
            "fixed_schedule": True,
        },
        "run_manifest": {
            "dataset": {"name": "unbounded-trace", "format": "mooncake_trace", "scenario": "custom_agentic"},
            "replay": {"start_seconds": 25.0, "end_seconds": None, "fixed_schedule": True, "scale_factor": 1.0},
        },
        "client_series": _sample_client_series(),
        "server_telemetry": {"available": False},
        "forensics": {},
    }
    emb_summary_only = {
        **_build_payload("0000004c", with_workload=False),
        "client_series": _sample_client_series(),
    }
    emb_empty_ctx = {
        "task": {},
        "run_manifest": {},
        "client_series": _sample_client_series(),
        "server_telemetry": {"available": False},
        "forensics": {},
    }
    emb_large_and_invalid = {
        "task": {
            "id": "0000004d",
            "dataset": "long-trace",
            "trace_format": "mooncake_trace",
            "scenario": "chat",
            "start_seconds": 1200.0,
            "end_seconds": 1800.0,
            "duration_seconds": 600.0,
            "warmup_seconds": -5.0,
            "grace_period_seconds": -2.0,
            "scale_factor": 0.0,
            "fixed_schedule": True,
        },
        "run_manifest": {
            "dataset": {"name": "long-trace", "format": "mooncake_trace", "scenario": "chat"},
            "replay": {
                "start_seconds": 1200.0,
                "end_seconds": 1800.0,
                "duration_seconds": 600.0,
                "warmup_seconds": -5.0,
                "grace_period_seconds": -2.0,
                "scale_factor": 0.0,
                "fixed_schedule": True,
            },
        },
        "client_series": {**_sample_client_series(), "warmup_seconds": -1.0},
        "server_telemetry": {"available": False},
        "forensics": {},
    }

    vm_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  return {
    tagName: tag.toUpperCase(),
    id: "",
    children: [],
    attributes: {},
    style: {},
    className: "",
    classList: {
      add(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.add(c); this._owner.className = Array.from(s).join(" "); },
      remove(c) { const s = new Set((this._owner.className || "").split(/\s+/).filter(Boolean)); s.delete(c); this._owner.className = Array.from(s).join(" "); },
      toggle(c, force) { const has = this.contains(c); const next = force !== undefined ? Boolean(force) : !has; if (next) this.add(c); else this.remove(c); },
      contains(c) { return (this._owner.className || "").split(/\s+/).includes(c); },
      _owner: null,
    },
    textContent: "",
    parentNode: null,
    listeners: {},
    setAttribute(k, v) { this.attributes[k] = String(v); if (k === "class") this.className = String(v); },
    getAttribute(k) { if (k === "id" && this.id) return String(this.id); if (k === "class" && this.className) return String(this.className); return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") { const t = makeNode("#text"); t.textContent = child; t.parentNode = this; this.children.push(t); }
        else { child.parentNode = this; this.children.push(child); }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) { for (const fn of this.listeners[evt] || []) fn(evtObj || { target: this }); },
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  return (node.className || node.getAttribute("class") || "").split(/\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\s+/g, " ").trim();
}

async function runCase(payload) {
  const mk = (tag) => { const n = makeNode(tag); n.classList._owner = n; return n; };
  const tooltipNode = mk("div");
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": mk("main"),
    "report-title": mk("span"),
    "report-meta": mk("div"),
    "status-badges": mk("span"),
    "lens-report-data": (() => { const s = mk("script"); s.textContent = JSON.stringify(payload); return s; })(),
    "sidebar-toggle": mk("button"),
    "sidebar": mk("aside"),
    "theme-toggle": mk("button"),
    "print-btn": mk("button"),
    "window-toggle": mk("div"),
    "nav-list": mk("nav"),
  };
  const doc = {
    documentElement: mk("html"),
    body: mk("body"),
    createElement: (t) => mk(t),
    createElementNS: (_ns, t) => mk(t),
    createTextNode: (t) => { const n = mk("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || mk("div"),
    querySelectorAll: () => [],
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: ids["report-root"], tooltip: tooltipNode };
}

(async () => {
  // 1. Concurrency-paced run
  const resConc = await runCase(cases.conc);
  const wConc = resConc.root.children.find((n) => n.getAttribute("data-page") === "workload");
  assert.strictEqual(wConc.children[0].getAttribute("id"), "w-config", "w-config must be first section on Workload page");
  assert.strictEqual(wConc.children[1].getAttribute("id"), "w-kpis", "w-kpis must follow w-config");
  const concCfgText = nodeText(wConc.children[0]);
  assert(!concCfgText.includes("NaN") && !concCfgText.includes("undefined"));
  assert(concCfgText.includes("Workload & Replay Configuration"));
  assert(concCfgText.includes("bailian-tool-trace"));
  assert(concCfgText.includes("Format: bailian_trace"));
  assert(concCfgText.includes("Scenario: Tool & API Use"));
  assert(concCfgText.includes("5s – 95s"), `Expected explicit range 5s – 95s, got: ${concCfgText}`);
  assert(concCfgText.includes("60 s") && concCfgText.includes("Warmup: 10 s") && concCfgText.includes("Grace: 20 s"), concCfgText);
  assert(concCfgText.includes("Concurrency") && concCfgText.includes("16 slots"), concCfgText);
  assert(concCfgText.includes("1.50×"), concCfgText);

  // Verify every .help-icon in #w-config has prose + formula and zero forbidden terms
  const forbiddenRe = /(\bvllm\b|\bsglang\b|\btrt-?llm\b|\btgi\b|\btriton\b|\bnvidia\b|\bdcgm\b|\bnccl\b|\baiperf\b|\bprometheus\b|\bGPU\b)/i;
  const cfgIcons = findAll(wConc.children[0], (n) => hasClass(n, "help-icon"));
  assert.strictEqual(cfgIcons.length, 7, `Expected 7 help icons in #w-config (section + 6 tiles), got ${cfgIcons.length}`);
  for (const icon of cfgIcons) {
    icon.dispatchEvent("mouseenter", { clientX: 40, clientY: 40 });
    const bodyEl = findAll(resConc.tooltip, (n) => hasClass(n, "help-tip-body"))[0];
    const formEl = findAll(resConc.tooltip, (n) => hasClass(n, "help-tip-formula"))[0];
    assert(bodyEl && nodeText(bodyEl).length > 0, `Empty help prose for ${icon.getAttribute("aria-label")}`);
    assert(formEl && nodeText(formEl).length > 0, `Empty help formula for ${icon.getAttribute("aria-label")}`);
    assert(!forbiddenRe.test(nodeText(bodyEl)), `Forbidden term in help prose: ${nodeText(bodyEl)}`);
    assert(!forbiddenRe.test(nodeText(formEl)), `Forbidden term in help formula: ${nodeText(formEl)}`);
  }

  // 2. Weka dataset defaults to Concurrency (1 slot)
  const resWeka = await runCase(cases.weka);
  const wWekaCfg = nodeText(findAll(resWeka.root, (n) => n.getAttribute("id") === "w-config")[0]);
  assert(wWekaCfg.includes("weka-coding-corpus"));
  assert(wWekaCfg.includes("Scenario: Code Generation"));
  assert(wWekaCfg.includes("Concurrency") && wWekaCfg.includes("1 slot"), wWekaCfg);
  assert(!wWekaCfg.includes("Grace:"), "Grace should be omitted when grace_period_seconds === 0");

  // 3. Fixed-schedule run computes end = start + duration * scale_factor
  const resFixed = await runCase(cases.fixed);
  const wFixedCfg = nodeText(findAll(resFixed.root, (n) => n.getAttribute("id") === "w-config")[0]);
  assert(wFixedCfg.includes("mooncake-chat") && wFixedCfg.includes("Scenario: Chat"));
  assert(wFixedCfg.includes("10s – 90s"), `Expected computed range 10s – 90s, got: ${wFixedCfg}`);
  assert(wFixedCfg.includes("Fixed Schedule") && wFixedCfg.includes("Fixed Schedule (trace-driven)"), wFixedCfg);
  assert(wFixedCfg.includes("2×"), `Expected 2× scale factor, got: ${wFixedCfg}`);

  // 4. Legacy trace-replayer backend forces Fixed Schedule
  const resReplayer = await runCase(cases.replayer);
  const wRepCfg = nodeText(findAll(resReplayer.root, (n) => n.getAttribute("id") === "w-config")[0]);
  assert(wRepCfg.includes("Fixed Schedule") && wRepCfg.includes("Fixed Schedule (trace-driven)"), wRepCfg);
  assert(!wRepCfg.includes("32 slots"), "trace-replayer must not show Concurrency slots");

  // 5. Unbounded full-trace fallback when end_seconds and duration_seconds are both null
  const resUnbounded = await runCase(cases.unbounded);
  const wUnbCfg = nodeText(findAll(resUnbounded.root, (n) => n.getAttribute("id") === "w-config")[0]);
  assert(wUnbCfg.includes("From 25s (full trace)"), `Expected unbounded range fallback, got: ${wUnbCfg}`);
  assert(wUnbCfg.includes("Scenario: Custom Agentic"), `Expected humanized scenario fallback, got: ${wUnbCfg}`);

  // 6. Summary-only / missing workload window still renders w-config above unavailable notice
  const resSummaryOnly = await runCase(cases.summaryOnly);
  const wSumPage = resSummaryOnly.root.children.find((n) => n.getAttribute("data-page") === "workload");
  assert.strictEqual(wSumPage.children.length, 2);
  assert.strictEqual(wSumPage.children[0].getAttribute("id"), "w-config");
  assert.strictEqual(wSumPage.children[1].getAttribute("id"), "w-kpis");
  assert(nodeText(wSumPage.children[0]).includes("bailian-tool-trace"));
  assert(nodeText(wSumPage.children[1]).includes("Workload distribution telemetry is unavailable"));

  // 7. Empty context omits w-config cleanly
  const resEmptyCtx = await runCase(cases.emptyCtx);
  const wEmptyPage = resEmptyCtx.root.children.find((n) => n.getAttribute("data-page") === "workload");
  assert.strictEqual(wEmptyPage.children.length, 1);
  assert.strictEqual(wEmptyPage.children[0].getAttribute("id"), "w-kpis");

  // 8. >= 1000s formatting and invalid negative/zero bounds fallback safely
  const resLarge = await runCase(cases.largeInvalid);
  const wLargeCfg = nodeText(findAll(resLarge.root, (n) => n.getAttribute("id") === "w-config")[0]);
  assert(wLargeCfg.includes("1,200s – 1,800s"), `Expected 1,200s – 1,800s, got: ${wLargeCfg}`);
  assert(wLargeCfg.includes("600 s") && wLargeCfg.includes("Warmup: 0 s"), wLargeCfg);
  assert(!wLargeCfg.includes("-5") && !wLargeCfg.includes("-2") && !wLargeCfg.includes("0×"), wLargeCfg);
  assert(wLargeCfg.includes("1×"), `Expected 1× fallback for scale_factor=0, got: ${wLargeCfg}`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps(
            {
                "conc": emb_conc,
                "weka": emb_weka,
                "fixed": emb_fixed,
                "replayer": emb_replayer,
                "unbounded": emb_unbounded,
                "summaryOnly": emb_summary_only,
                "emptyCtx": emb_empty_ctx,
                "largeInvalid": emb_large_and_invalid,
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node v41 Workload Config DOM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_v42_hardware_page_and_node_dom(sim_env: Path) -> None:
    """Offline v42 HTML report includes Hardware page, manifest/payload hardware fields, and Node VM DOM checks."""
    from llm_d_lens.simulation.reporting import builder as report_builder
    from llm_d_lens.simulation.reporting.builder import _compact_report_payload, render_run_manifest

    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    charts_js_path = Path(str(assets_dir.joinpath("report_charts.js")))

    # 1. Template version marker, sidebar nav-group order, and zero unsafe DOM sinks
    assert report_builder.REPORT_TEMPLATE_VERSION_MARKER == "<!-- lens-report-v42 -->"
    assert "<!-- lens-report-v42 -->" in template_html
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "fetch(",
    ):
        assert forbidden not in charts_js, f"Forbidden DOM/eval sink {forbidden!r} in report_charts.js"
        assert forbidden not in template_html, f"Forbidden DOM/eval sink {forbidden!r} in report_template.html"

    nav_pages = re.findall(r'<div class="nav-group[^"]*" data-page="([^"]+)">', template_html)
    assert nav_pages == ["overview", "workload", "bottlenecks", "serving", "requests", "system", "hardware", "config"]
    for sub_id in ("#h-overview", "#h-nodes", "#h-pods", "#h-cache"):
        assert f'href="{sub_id}" data-page="hardware"' in template_html

    # 2. All 13 Hardware HELP_TEXTS + HELP_FORMULAS keys registered 1:1 with zero GPU/brand leaks
    hw_help_keys = (
        "h-overview",
        "h-nodes",
        "h-pods",
        "h-cache",
        "Target Environment",
        "Accelerator Profile",
        "Serving Nodes & Pods",
        "Allocated Accelerators & Device Memory",
        "Host Compute & Memory",
        "Device HBM KV Capacity",
        "Host CPU KV Offload Capacity",
        "Local Storage KV Capacity",
        "Effective KV Cache Capacity",
    )
    for key in hw_help_keys:
        key_count = charts_js.count(f'"{key}":')
        assert key_count == 2, f"Expected 1:1 HELP_TEXTS + HELP_FORMULAS entries for {key!r}, got {key_count}"

    # 3. Payload & manifest completeness + compaction preservation of per_node & per_pod hardware specs
    task_full = _make_task("00000042")
    task_full.endpoint_mode = "deployment"
    task_full.hardware = "8xH100-SXM5-80GB"
    server_full = {
        "available": True,
        "status": "available",
        "topology_mode": "disaggregated",
        "guide_type": "pd-disaggregation",
        "nodes": ["node-a", "node-b"],
        "pods": ["prefill-0", "decode-0", "epp-0"],
        "accelerator_summary": "H100-SXM5-80GB",
        "hardware_profiles": [
            {
                "id": "nvidia-h100",
                "display_name": "NVIDIA H100",
                "vendor": "nvidia",
                "metric_group": "Accelerator",
                "models": ["H100-SXM5-80GB"],
                "allocated_accelerators": 8,
            }
        ],
        "per_node": [
            {
                "node": "node-a",
                "roles": ["prefill"],
                "pod_count": 1,
                "pods": ["prefill-0"],
                "allocated_accelerators": 4,
                "accelerator_capacity": 8,
                "accelerator_allocatable": 4,
                "accelerator_model": "H100-SXM5-80GB",
                "cpu_capacity_cores": 64,
                "cpu_allocatable_cores": 62.5,
                "memory_capacity_bytes": 512 * 1024**3,
                "memory_total_bytes": 512 * 1024**3,
                "gpu_memory_total_bytes": 320 * 1024**3,
                "architecture": "amd64",
                "os_image": "Ubuntu 22.04.4 LTS",
                "kernel_version": "6.8.0-45-generic",
                "kubelet_version": "v1.30.2",
                "container_runtime": "containerd://1.7.16",
            },
            {
                "node": "node-b",
                "roles": ["decode"],
                "pod_count": 1,
                "pods": ["decode-0"],
                "allocated_accelerators": 4,
                "accelerator_capacity": 8,
                "accelerator_allocatable": 4,
                "accelerator_model": "H100-SXM5-80GB",
                "cpu_capacity_cores": 64,
                "cpu_allocatable_cores": 62.5,
                "memory_capacity_bytes": 512 * 1024**3,
                "memory_total_bytes": 512 * 1024**3,
                "gpu_memory_total_bytes": 320 * 1024**3,
                "architecture": "amd64",
                "os_image": "Ubuntu 22.04.4 LTS",
                "kernel_version": "6.8.0-45-generic",
                "kubelet_version": "v1.30.2",
                "container_runtime": "containerd://1.7.16",
            },
        ],
        "per_pod": [
            {
                "pod": "prefill-0",
                "role": "prefill",
                "node": "node-a",
                "allocated_accelerators": 4,
                "cpu_request_cores": 16,
                "cpu_limit_cores": 32,
                "memory_request_bytes": 64 * 1024**3,
                "memory_limit_bytes": 128 * 1024**3,
                "gpu_memory_total_bytes": {"mean": 320 * 1024**3, "min": 320 * 1024**3, "max": 320 * 1024**3},
            },
            {
                "pod": "decode-0",
                "role": "decode",
                "node": "node-b",
                "allocated_accelerators": 4,
                "cpu_request_cores": 16,
                "cpu_limit_cores": 32,
                "memory_request_bytes": 64 * 1024**3,
                "memory_limit_bytes": 128 * 1024**3,
                "gpu_memory_total_bytes": {"mean": 320 * 1024**3, "min": 320 * 1024**3, "max": 320 * 1024**3},
            },
            {
                "pod": "epp-0",
                "role": "epp",
                "cpu_request_cores": 2,
                "cpu_limit_cores": 4,
                "memory_request_bytes": 4 * 1024**3,
                "memory_limit_bytes": 8 * 1024**3,
            },
        ],
        "cache_config": {
            "hbm_capacity_tokens": 250000,
            "hbm_capacity_bytes": 640 * 1024**3,
            "prefill_hbm_capacity_tokens": 100000,
            "decode_hbm_capacity_tokens": 150000,
            "cpu_capacity_tokens": 500000,
            "cpu_capacity_bytes": 256 * 1024**3,
            "nvme_capacity_tokens": 1000000,
            "nvme_capacity_bytes": 1024 * 1024**3,
            "effective_capacity_tokens": 1750000,
            "block_size": 16,
            "num_gpu_blocks": 15625,
            "kv_cache_max_concurrency": 64.5,
        },
        "series": [{"t": 0.0, "kv_cache_pct": 40.0}, {"t": 5.0, "kv_cache_pct": 55.0}],
    }

    manifest_full = render_run_manifest(task_full, server_telemetry=server_full)
    assert manifest_full["hardware_profile"]["models"] == ["H100-SXM5-80GB"]
    assert manifest_full["hardware_profile"]["allocated_accelerators"] == 8

    manifest_legacy = render_run_manifest(task_full, server_telemetry=_sample_server_telemetry())
    assert "models" not in manifest_legacy["hardware_profile"]
    assert "allocated_accelerators" not in manifest_legacy["hardware_profile"]

    html_full = render_html_report(
        task_full,
        client_series=_sample_client_series(),
        server_telemetry=server_full,
        run_manifest=manifest_full,
    )
    assert "<!-- lens-report-v42 -->" in html_full
    emb_full = _extract_embedded_report_json(html_full)
    assert emb_full["task"]["hardware"] == "8xH100-SXM5-80GB"

    compacted_direct = _compact_report_payload({"server_telemetry": server_full})
    node_a_c = compacted_direct["server_telemetry"]["per_node"][0]
    assert node_a_c["cpu_capacity_cores"] == pytest.approx(64)
    assert node_a_c["cpu_allocatable_cores"] == pytest.approx(62.5)
    assert node_a_c["memory_capacity_bytes"] == 512 * 1024**3
    assert node_a_c["accelerator_capacity"] == 8
    assert node_a_c["accelerator_allocatable"] == 4
    assert node_a_c["accelerator_model"] == "H100-SXM5-80GB"
    assert node_a_c["architecture"] == "amd64"
    assert node_a_c["os_image"] == "Ubuntu 22.04.4 LTS"
    assert node_a_c["kernel_version"] == "6.8.0-45-generic"
    assert node_a_c["kubelet_version"] == "v1.30.2"
    assert node_a_c["container_runtime"] == "containerd://1.7.16"
    pod_0_c = compacted_direct["server_telemetry"]["per_pod"][0]
    assert pod_0_c["allocated_accelerators"] == 4
    assert pod_0_c["cpu_request_cores"] == pytest.approx(16)
    assert pod_0_c["cpu_limit_cores"] == pytest.approx(32)
    assert pod_0_c["memory_request_bytes"] == 64 * 1024**3
    assert pod_0_c["memory_limit_bytes"] == 128 * 1024**3

    # External endpoint without server telemetry
    task_ext = _make_task("00000043", endpoint_cluster_id=None)
    task_ext.endpoint_mode = "external"
    task_ext.hardware = "4xCustom-Accel"
    html_ext = render_html_report(
        task_ext,
        client_series=_sample_client_series(),
        server_telemetry={"available": False, "status": "unavailable", "reason": "External endpoint"},
    )
    emb_ext = _extract_embedded_report_json(html_ext)
    assert emb_ext["task"]["hardware"] == "4xCustom-Accel"

    # Legacy v10 snapshot without K8s node/pod spec fields or cache_config
    task_legacy = _make_task("00000044")
    task_legacy.endpoint_mode = "deployment"
    server_legacy = {
        "available": True,
        "status": "available",
        "topology_mode": "aggregated",
        "hardware_profile": {
            "id": "generic-accel",
            "display_name": "Generic Accelerator",
            "vendor": "generic",
            "metric_group": "Accelerator",
            "device_count": 2,
        },
        "per_node": [
            {
                "node": "legacy-node-0",
                "roles": ["worker"],
                "pod_count": 1,
                "pods": ["worker-0"],
                "allocated_accelerators": 2,
                "memory_total_bytes": 128 * 1024**3,
            }
        ],
        "per_pod": [
            {
                "pod": "worker-0",
                "role": "worker",
                "node": "legacy-node-0",
            }
        ],
        "series": [{"t": 0.0, "kv_cache_pct": 20.0}],
    }
    html_legacy = render_html_report(
        task_legacy,
        client_series=_sample_client_series(),
        server_telemetry=server_legacy,
    )
    emb_legacy = _extract_embedded_report_json(html_legacy)

    # 4. Node VM DOM tests across all 3 scenarios
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to validate report_charts.js Hardware page"

    vm_harness = """
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const chartsJs = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeNode(tag) {
  const node = {
    tagName: tag.toUpperCase(),
    children: [],
    attributes: {},
    listeners: {},
    style: {},
    textContent: "",
    className: "",
    parentNode: null,
    classList: {
      _owner: null,
      add(c) {
        const s = new Set((this._owner.className || "").split(/\\s+/).filter(Boolean));
        s.add(c);
        this._owner.className = [...s].join(" ");
      },
      remove(...cs) {
        const rm = new Set(cs);
        this._owner.className = (this._owner.className || "").split(/\\s+/).filter((c) => c && !rm.has(c)).join(" ");
      },
      toggle(c, force) {
        const s = new Set((this._owner.className || "").split(/\\s+/).filter(Boolean));
        const has = s.has(c);
        const on = force === undefined ? !has : Boolean(force);
        if (on) s.add(c);
        else s.delete(c);
        this._owner.className = [...s].join(" ");
        return on;
      },
      contains(c) { return (this._owner.className || "").split(/\\s+/).includes(c); },
    },
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") this.className = String(v);
    },
    getAttribute(k) {
      if (k === "class" && this.className) return String(this.className);
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    },
    appendChild(child) {
      if (child != null) {
        if (typeof child === "string") {
          const t = makeNode("#text");
          t.textContent = child;
          t.parentNode = this;
          this.children.push(t);
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
      }
      return child;
    },
    append(...items) { for (const i of items) if (i != null) this.appendChild(i); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); },
    dispatchEvent(evt, evtObj) {
      const payload = Object.assign({ target: this, stopPropagation() {}, preventDefault() {} }, evtObj || {});
      for (const fn of this.listeners[evt] || []) fn(payload);
    },
    closest(sel) {
      let cur = this;
      while (cur) {
        if (sel === "button[data-window]" && cur.tagName === "BUTTON" && cur.getAttribute("data-window") !== null) return cur;
        cur = cur.parentNode;
      }
      return null;
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 280, right: 640, bottom: 280 }; },
  };
  node.classList._owner = node;
  return node;
}
function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const ch of root.children || []) findAll(ch, pred, out);
  return out;
}
function hasClass(node, cls) {
  const cn = node.className || node.getAttribute("class") || "";
  return cn.split(/\\s+/).includes(cls);
}
function nodeText(node) {
  if (!node || typeof node !== "object") return "";
  const self = node.textContent ? [node.textContent] : [];
  return self.concat((node.children || []).map(nodeText)).join(" ").replace(/\\s+/g, " ").trim();
}

async function runCase(payload) {
  const tooltipNode = makeNode("div");
  const winToggle = makeNode("div");
  const btnProf = makeNode("button"); btnProf.setAttribute("data-window", "profiling"); btnProf.className = "active";
  const btnFull = makeNode("button"); btnFull.setAttribute("data-window", "full");
  winToggle.append(btnProf, btnFull);

  const navList = makeNode("nav");
  const navBtns = {};
  const navGroups = {};
  for (const pid of ["overview", "workload", "bottlenecks", "serving", "requests", "system", "hardware", "config"]) {
    const grp = makeNode("div");
    grp.className = "nav-group";
    grp.setAttribute("data-page", pid);
    const btn = makeNode("button");
    btn.className = "nav-btn";
    btn.setAttribute("data-page", pid);
    if (pid === "overview") { grp.classList.add("open"); btn.classList.add("active"); }
    grp.append(btn);
    navList.append(grp);
    navBtns[pid] = btn;
    navGroups[pid] = grp;
  }

  const reportRoot = makeNode("main");
  const dataScript = makeNode("script");
  dataScript.textContent = JSON.stringify(payload);
  const ids = {
    "chart-tooltip": tooltipNode,
    "report-root": reportRoot,
    "report-title": makeNode("span"),
    "report-meta": makeNode("div"),
    "status-badges": makeNode("span"),
    "lens-report-data": dataScript,
    "sidebar-toggle": makeNode("button"),
    "sidebar": makeNode("aside"),
    "theme-toggle": makeNode("button"),
    "print-btn": makeNode("button"),
    "window-toggle": winToggle,
    "nav-list": navList,
  };
  const doc = {
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: (t) => makeNode(t),
    createElementNS: (_ns, t) => makeNode(t),
    createTextNode: (t) => { const n = makeNode("#text"); n.textContent = String(t); return n; },
    getElementById: (id) => ids[id] || makeNode("div"),
    querySelectorAll: (sel) => {
      if (sel === "#window-toggle button") return [btnProf, btnFull];
      if (sel === ".report-page") return reportRoot.children.slice();
      if (sel === ".nav-btn") return Object.values(navBtns);
      if (sel === ".nav-group") return Object.values(navGroups);
      return [];
    },
  };
  const ctx = {
    document: doc,
    window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, addEventListener() {}, print() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#93a4bd" }),
    navigator: {},
    TextDecoder, Uint8Array,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Blob, Response,
    DecompressionStream: globalThis.DecompressionStream,
    ResizeObserver: undefined,
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, console,
    Math, Number, String, Array, Object, JSON, Set, Map, RegExp, Date, Error, Promise,
    isFinite, parseFloat, parseInt,
  };
  ctx.window.document = doc;
  vm.runInNewContext(chartsJs, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return { root: reportRoot, tooltip: tooltipNode, navBtns, navGroups };
}

(async () => {
  // (a) Full multi-node P/D disaggregated cluster with cache_config
  const resFull = await runCase(cases.full);
  const pageIds = resFull.root.children.map((n) => n.getAttribute("data-page"));
  assert.strictEqual(
    JSON.stringify(pageIds),
    JSON.stringify(["overview", "workload", "bottlenecks", "serving", "requests", "system", "hardware", "config"])
  );

  const hwPage = resFull.root.children.find((n) => n.getAttribute("data-page") === "hardware");
  const hwText = nodeText(hwPage);
  assert(!hwText.includes("NaN") && !hwText.includes("undefined"), `Found NaN/undefined in Hardware page: ${hwText}`);
  for (const secId of ["h-overview", "h-nodes", "h-pods", "h-cache"]) {
    assert.strictEqual(findAll(hwPage, (n) => n.getAttribute("id") === secId).length, 1, `Missing #${secId}`);
  }
  assert(hwText.includes("Cluster A / llm-d") && hwText.includes("P/D Disaggregation"), hwText);
  assert(hwText.includes("H100-SXM5-80GB") && hwText.includes("2 nodes · 3 pods"), hwText);
  assert(hwText.includes("8 · 640 GiB") && hwText.includes("128 vCPU · 1 TiB RAM"), hwText);
  assert(hwText.includes("Ubuntu 22.04.4 LTS") && hwText.includes("v1.30.2"), hwText);
  assert(hwText.includes("250,000 tok") && hwText.includes("500,000 tok") && hwText.includes("1,000,000 tok") && hwText.includes("1,750,000 tok"), hwText);

  // Every .help-icon on Hardware page has non-empty prose + formula and zero forbidden terms
  const forbiddenRe = /(\\bvllm\\b|\\bsglang\\b|\\btrt-?llm\\b|\\btgi\\b|\\btriton\\b|\\bnvidia\\b|\\bdcgm\\b|\\bnccl\\b|\\baiperf\\b|\\bprometheus\\b|\\bGPU\\b)/i;
  const hwIcons = findAll(hwPage, (n) => hasClass(n, "help-icon"));
  assert(hwIcons.length >= 20, `Expected >= 20 help icons on Hardware page, got ${hwIcons.length}`);
  for (const icon of hwIcons) {
    icon.dispatchEvent("mouseenter", { clientX: 40, clientY: 40 });
    const bodyEl = findAll(resFull.tooltip, (n) => hasClass(n, "help-tip-body"))[0];
    const formEl = findAll(resFull.tooltip, (n) => hasClass(n, "help-tip-formula"))[0];
    assert(bodyEl && nodeText(bodyEl).length > 0, `Empty help prose for ${icon.getAttribute("aria-label")}`);
    assert(formEl && nodeText(formEl).length > 0, `Empty help formula for ${icon.getAttribute("aria-label")}`);
    assert(!forbiddenRe.test(nodeText(bodyEl)), `Forbidden term in help prose: ${nodeText(bodyEl)}`);
    assert(!forbiddenRe.test(nodeText(formEl)), `Forbidden term in help formula: ${nodeText(formEl)}`);
  }

  // Click node-b row in #h-nodes -> jumpToSystemNode("node-b") switches to system page scoped to node-b
  const hNodes = findAll(hwPage, (n) => n.getAttribute("id") === "h-nodes")[0];
  const nodeRows = findAll(hNodes, (n) => n.tagName === "TR" && hasClass(n, "node-row"));
  assert.strictEqual(nodeRows.length, 2, "Expected 2 clickable node rows in #h-nodes");
  nodeRows[1].dispatchEvent("click", {});
  assert(hasClass(resFull.navBtns.system, "active"), "System nav button must become active after node row click");
  assert(hasClass(resFull.navGroups.system, "open"), "System nav group must become open after node row click");
  const sysAfterRowClick = resFull.root.children.find((n) => n.getAttribute("data-page") === "system");
  assert(hasClass(sysAfterRowClick, "active"), "System page must become active after node row click");
  const nodeBPillAfterRow = findAll(sysAfterRowClick, (n) => n.tagName === "BUTTON" && hasClass(n, "pill-btn") && n.textContent === "node-b")[0];
  assert(nodeBPillAfterRow && hasClass(nodeBPillAfterRow, "active"), "node-b scope pill must be active on System page");

  // Click node-a button in #h-pods -> jumpToSystemNode("node-a") switches to system page scoped to node-a
  const hwPage2 = resFull.root.children.find((n) => n.getAttribute("data-page") === "hardware");
  const hPods = findAll(hwPage2, (n) => n.getAttribute("id") === "h-pods")[0];
  const podNodeBtn = findAll(hPods, (n) => n.tagName === "BUTTON" && n.textContent === "node-a")[0];
  assert(podNodeBtn, "Expected clickable node-a button in #h-pods");
  podNodeBtn.dispatchEvent("click", {});
  const sysAfterPodClick = resFull.root.children.find((n) => n.getAttribute("data-page") === "system");
  assert(hasClass(sysAfterPodClick, "active"), "System page must become active after pod node click");
  const nodeAPillAfterPod = findAll(sysAfterPodClick, (n) => n.tagName === "BUTTON" && hasClass(n, "pill-btn") && n.textContent === "node-a")[0];
  assert(nodeAPillAfterPod && hasClass(nodeAPillAfterPod, "active"), "node-a scope pill must be active on System page");

  // (b) External endpoint graceful fallback
  const resExt = await runCase(cases.ext);
  const hwExt = resExt.root.children.find((n) => n.getAttribute("data-page") === "hardware");
  const hwExtText = nodeText(hwExt);
  assert(!hwExtText.includes("NaN") && !hwExtText.includes("undefined"), hwExtText);
  assert(hwExtText.includes("External Endpoint") && hwExtText.includes("4xCustom-Accel"), hwExtText);
  const extEmpties = findAll(hwExt, (n) => hasClass(n, "empty-note"));
  assert.strictEqual(extEmpties.length, 3, "Expected 3 empty-note fallbacks (#h-nodes, #h-pods, #h-cache) on external endpoint");

  // (c) Legacy v10 snapshot graceful fallback
  const resLeg = await runCase(cases.legacy);
  const hwLeg = resLeg.root.children.find((n) => n.getAttribute("data-page") === "hardware");
  const hwLegText = nodeText(hwLeg);
  assert(!hwLegText.includes("NaN") && !hwLegText.includes("undefined"), hwLegText);
  assert(hwLegText.includes("legacy-node-0") && hwLegText.includes("128 GiB RAM"), hwLegText);
  assert(hwLegText.includes("KV cache capacity hierarchy metadata was not exposed"), hwLegText);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", vm_harness, str(charts_js_path)],
        input=json.dumps({"full": emb_full, "ext": emb_ext, "legacy": emb_legacy}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node v42 Hardware DOM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
