#!/usr/bin/env python3
"""Prepare the target virtualenv for the Lens backend distribution rename."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from email.parser import Parser
from importlib import metadata
from pathlib import Path


def prepare_python_project_upgrade(project_directory: Path) -> None:
    """Remove obsolete project metadata without touching another environment."""
    project_directory = project_directory.resolve()
    with (project_directory / "pyproject.toml").open("rb") as source:
        project_name = tomllib.load(source).get("project", {}).get("name")
    if project_name != "llm-d-lens-backend":
        return
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Run this command with the installation's virtual environment Python.")

    legacy_name = "llm-d-prism-backend"
    generated = project_directory / "llm_d_prism_backend.egg-info"
    information = generated / "PKG-INFO"
    if generated.is_dir() and not generated.is_symlink() and information.is_file():
        name = Parser().parsestr(information.read_text()).get("Name", "")
        if name.lower().replace("_", "-") == legacy_name:
            # Editable builds leave this generated directory in the source tree.
            # Remove it first so pip resolves the installed virtualenv metadata.
            shutil.rmtree(generated)

    environment_prefix = Path(sys.prefix).resolve()
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name", "").lower().replace("_", "-")
        location = Path(distribution.locate_file("")).resolve()
        if name == legacy_name and location.is_relative_to(environment_prefix):
            subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "--yes", legacy_name],
                check=True,
            )
            break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    prepare_python_project_upgrade(args.project_dir)


if __name__ == "__main__":
    main()
