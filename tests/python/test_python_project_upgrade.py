"""Verify distribution-name upgrades with real pip and entry-point discovery."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


def test_backend_rename_removes_only_obsolete_metadata(tmp_path):
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / "bin/python"
    site = Path(
        subprocess.check_output(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip()
    )
    entry_points = "[llm_d_bench.hardware.providers]\nexample = example_provider:Provider\n"
    distributions = {}
    for name in ("llm-d-prism-backend", "llm-d-lens-backend", "unrelated-package"):
        directory = site / f"{name.replace('-', '_')}-0.1.0.dist-info"
        directory.mkdir()
        (directory / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n")
        if name != "unrelated-package":
            (directory / "entry_points.txt").write_text(entry_points)
        with (directory / "RECORD").open("w") as record:
            writer = csv.writer(record)
            for file in directory.iterdir():
                writer.writerow([str(file.relative_to(site)), "", ""])
        distributions[name] = directory

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "llm-d-lens-backend"\n')
    generated = project / "llm_d_prism_backend.egg-info"
    generated.mkdir()
    (generated / "PKG-INFO").write_text("Name: llm-d-prism-backend\nVersion: 0.1.0\n")
    (generated / "entry_points.txt").write_text(entry_points)
    retained = project / "retained-data.txt"
    retained.write_text("Keep existing application data.")

    inspect = [
        str(python),
        "-c",
        "import json; from importlib import metadata; "
        "print(json.dumps([(entry.name, entry.value) for entry in "
        "metadata.entry_points(group='llm_d_bench.hardware.providers')]))",
    ]
    assert len(json.loads(subprocess.check_output(inspect, cwd=project, text=True))) == 2
    script = Path(__file__).resolve().parents[2] / "scripts/prepare_python_project_upgrade.py"
    subprocess.run([str(python), str(script), "--project-dir", str(project)], check=True, capture_output=True)
    assert not generated.exists()
    assert not distributions["llm-d-prism-backend"].exists()
    assert distributions["llm-d-lens-backend"].is_dir()
    assert distributions["unrelated-package"].is_dir()
    assert retained.read_text() == "Keep existing application data."
    assert len(json.loads(subprocess.check_output(inspect, cwd=project, text=True))) == 1

    # Repeated upgrades must preserve metadata that is not the old distribution.
    generated.mkdir()
    (generated / "PKG-INFO").write_text("Name: unrelated-package\nVersion: 0.1.0\n")
    subprocess.run([str(python), str(script), "--project-dir", str(project)], check=True, capture_output=True)
    assert generated.is_dir()
    assert retained.is_file()
