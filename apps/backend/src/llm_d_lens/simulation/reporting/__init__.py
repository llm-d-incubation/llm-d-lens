"""Offline HTML report and reproducible bundle builder for Simulation tasks."""

from __future__ import annotations

from .builder import (
    MAX_HTML_REPORT_BYTES,
    build_bundle_tar_gz,
    build_report_artifacts,
    render_html_report,
    render_run_manifest,
    render_summary_csv,
)

__all__ = [
    "MAX_HTML_REPORT_BYTES",
    "build_bundle_tar_gz",
    "build_report_artifacts",
    "render_html_report",
    "render_run_manifest",
    "render_summary_csv",
]
