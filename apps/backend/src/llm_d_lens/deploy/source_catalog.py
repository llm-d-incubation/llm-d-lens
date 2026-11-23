"""Pinned, cluster-authorized source inputs. Guide shell commands are never executed."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from importlib.resources import files
from pathlib import Path

import yaml

from llm_d_lens.infrastructure.process import ShellClient

from .guide_planning import GuideError
from .guide_sources import load_guide_paths

logger = logging.getLogger(__name__)
DEFINITION = json.loads(
    files("llm_d_lens.configuration").joinpath("optimization_catalog.json").read_text(encoding="utf-8")
)
EXCLUDED = re.compile(r"/(?:benchmark[^/]*|tests?|diagram|dashboards)/")


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def command(argv: list[str], timeout: int = 90) -> str:
    result = await ShellClient().run(argv, timeout=timeout)
    if not result.ok:
        logger.warning("Source renderer %s failed: %s", argv[0], result.stderr[-2000:])
        raise GuideError(f"{argv[0]} rendering failed; inspect server logs.", 502)
    return result.stdout


def documents(text: str) -> list[dict]:
    try:
        parsed = [item for item in yaml.safe_load_all(text) if item is not None]
    except yaml.YAMLError as error:
        raise GuideError("Invalid YAML input.") from error
    if any(not isinstance(item, dict) for item in parsed):
        raise GuideError("YAML documents must be mappings.")
    return parsed


def source_file(root: Path, name: str) -> Path:
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise GuideError("Source input is missing or escapes its authorized checkout.")
    return candidate


def source_facts(path: str, parsed: list[dict]) -> dict:
    strings, roles, references = [], set(), []

    def visit(value):
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            if all(isinstance(item, str) for item in value):
                strings.append(" ".join(value))
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            role = value.get("llm-d.ai/role") or value.get("role")
            if isinstance(role, str) and role in {"prefill", "decode", "encode"}:
                roles.add(role)
            for entry in value.get("roles", []) if isinstance(value.get("roles"), list) else []:
                if isinstance(entry, dict) and entry.get("name") in {"prefill", "decode", "encode"}:
                    roles.add(entry["name"])
            for item in value.values():
                visit(item)

    for doc in parsed:
        visit(doc)
        if doc.get("kind") in {"Kustomization", "Component"}:
            for key in ("resources", "components", "bases"):
                references.extend(doc.get(key) or [])
            for patch in [*(doc.get("patches") or []), *(doc.get("patchesStrategicMerge") or [])]:
                ref = patch.get("path") if isinstance(patch, dict) else patch
                if isinstance(ref, str) and "\n" not in ref:
                    references.append(ref)
    text = "\n".join(strings)
    disabled = [
        identifier
        for identifier, flag in (("prefix", "prefix-caching"), ("chunked", "chunked-prefill"))
        if re.search(rf"--no-enable-{flag}\b|--enable-{flag}[= ]false\b", text)
    ]
    return {
        "path": path,
        "references": references,
        "features": [
            key for key, pattern in DEFINITION["detectors"] if re.search(pattern, text) and key not in disabled
        ],
        "disabled": disabled,
        "roles": sorted(roles),
        "implementations": [key for key, pattern in DEFINITION["implementations"] if re.search(pattern, text)],
    }


def optimization_catalog(guides: list[dict], commit: str) -> dict:
    by_id = {guide["id"]: guide for guide in guides}
    items = []
    for identifier, label, stage, guide_id, detail, *extra in DEFINITION["items"]:
        item = {
            "id": identifier,
            "label": label,
            "stage": stage,
            "guide": guide_id,
            "detail": detail,
            "inspector": identifier,
            "fields": [],
            "available": False,
            "selection": "guide",
        }
        item.update(extra[0] if extra else {})
        item["guide"] = next((key for key in [guide_id, *item.get("guideAliases", [])] if key in by_id), guide_id)
        guide = by_id.get(item["guide"])
        present = bool(guide) or item["selection"] in {"recipe", "switch"}
        item.update(present=present, hardwareSupport=guide.get("support", {}) if guide else None)
        item.setdefault(
            "generation",
            "recipe-only" if item["selection"] == "recipe" else "source-dependent" if present else "unavailable",
        )
        item.setdefault(
            "reason",
            "Inspect the selected source and rendered configuration."
            if present
            else "This Guide is absent from the selected revision.",
        )
        if guide and re.search(r"\bdeprecated\b", guide.get("instructions", "")[:3000], re.I):
            item["maturity"] = "deprecated"
        if item["guide"]:
            item["source"] = f"https://github.com/llm-d/llm-d/blob/{commit}/guides/{item['guide']}/README.md"
        items.append(item)
    known = {item["guide"] for item in items}
    for guide in guides:
        if guide["id"] in known:
            continue
        parent = sorted(
            (item for item in items if item["guide"] and guide["id"].startswith(item["guide"] + "/")),
            key=lambda item: len(item["guide"]),
            reverse=True,
        )
        items.append(
            {
                "id": "guide:" + guide["id"],
                "label": guide["title"],
                "guide": guide["id"],
                "stage": "decode" if guide["id"].startswith("models/") else parent[0]["stage"] if parent else "pods",
                "detail": "Revision-owned Guide.",
                "selection": "guide",
                "generation": "source-dependent",
                "present": True,
                "hardwareSupport": guide.get("support", {}),
                "available": False,
                "fields": [],
            }
        )
    return {"schemaVersion": DEFINITION["schemaVersion"], "commit": commit, "items": items}


async def inventory(root: Path) -> dict:
    commit = (await command(["git", "-C", str(root), "rev-parse", "HEAD"])).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise GuideError("Invalid source revision.")
    if (
        await command(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--",
                "guides",
                "helpers",
                "docs/infrastructure",
            ]
        )
    ).strip():
        raise GuideError("Source has local changes. Use a clean pinned checkout.", 409)
    paths = (
        await command(
            [
                "git",
                "-C",
                str(root),
                "ls-tree",
                "-r",
                "--name-only",
                commit,
                "--",
                "guides",
                "helpers",
                "docs/infrastructure",
            ]
        )
    ).splitlines()
    paths = [path for path in paths if not EXCLUDED.search(path)]
    roots = sorted(
        {
            str(Path(path).parent)
            for path in paths
            if path.startswith("guides/")
            and Path(path).name in {"README.md", "guide.yaml"}
            and not re.search(r"/(?:recipes|modelserver|manifests|templates)/", path)
            and str(Path(path).parent) != "guides"
        }
    )

    def read(path):
        return source_file(root, path).read_text(encoding="utf-8")

    guides, assets, facts, warnings = [], [], [], []
    for directory in roots:
        definition = documents(read(directory + "/guide.yaml"))[0] if directory + "/guide.yaml" in paths else {}
        text = read(directory + "/README.md") if directory + "/README.md" in paths else ""
        heading = re.search(r"^#\s+(.+)$", text, re.M)
        guide_id = directory[7:]
        guides.append(
            {
                "id": guide_id,
                "title": heading[1] if heading else definition.get("name", guide_id),
                "source": directory + ("/guide.yaml" if definition else "/README.md"),
                "variables": definition.get("env", {}).get("static", {}),
                "support": definition.get("support", {}),
                "instructions": text,
                "steps": {"prerequisites": definition.get("prerequisites", {}), "deploy": definition.get("deploy", {})},
            }
        )
    for path in paths:
        if not re.search(r"\.ya?ml$|/Kustomization$", path) or path.endswith("/guide.yaml"):
            continue
        content = read(path)
        try:
            parsed = documents(content)
        except GuideError:
            warnings.append(f"{path}: template or invalid YAML; not selectable as a resource.")
            continue
        facts.append(source_facts(path, parsed))
        first = parsed[0] if parsed else {}
        kustomize = Path(path).name.lower() in {"kustomization.yaml", "kustomization.yml", "kustomization"}
        resource = bool(parsed) and all(
            doc.get("apiVersion") and doc.get("kind") and doc.get("metadata", {}).get("name") for doc in parsed
        )
        values = (
            not kustomize and not first.get("kind") and len(parsed) <= 1 and ("values" in path or "/router/" in path)
        )
        if not (kustomize or values or resource):
            continue
        owner = max((directory for directory in roots if path.startswith(directory + "/")), key=len, default="")
        kind = (
            "component"
            if first.get("kind") == "Component"
            else "kustomize"
            if kustomize
            else ("values" if values else "resource")
        )
        if kind == "resource" and re.search(r"/(?:patch[^/]*|.*\.patch)\.ya?ml$", path):
            continue
        required = []
        for generator in [*first.get("configMapGenerator", []), *first.get("secretGenerator", [])]:
            for value in [*generator.get("files", []), *generator.get("envs", [])]:
                name = value.split("=", 1)[-1]
                if not (root / Path(path).parent / name).is_file():
                    required.append(name)
        assets.append(
            dict(
                path=str(Path(path).parent) if kustomize else path,
                file=path,
                guide=owner[7:] if owner else "recipes",
                kind=kind,
                checksum=checksum(content),
                requiredFiles=required,
                **({"resources": first.get("resources", [])} if kind == "component" else {}),
                **({} if kustomize else {"content": content}),
            )
        )
    for path in paths:
        if not path.endswith(".md") or "/recipes/" in path:
            continue
        owner = max((directory for directory in roots if path.startswith(directory + "/")), key=len, default="")
        if not owner:
            continue
        text, index = read(path), 0
        examples = re.findall(r"```ya?ml[^\n]*\n([\s\S]*?)```", text)
        examples += re.findall(r"<<EOF\n([\s\S]*?)\nEOF", text)
        for content in examples:
            try:
                parsed = documents(content)
            except GuideError:
                continue  # Documentation snippets are not necessarily complete YAML.
            if parsed and all(
                doc.get("apiVersion") and doc.get("kind") and doc.get("metadata", {}).get("name") for doc in parsed
            ):
                assets.append(
                    {
                        "path": f"{path}#resource-{index}",
                        "file": path,
                        "guide": owner[7:],
                        "kind": "resource",
                        "checksum": checksum(content),
                        "content": content,
                    }
                )
                index += 1
        for match in re.finditer(
            r"helm\s+(?:install|upgrade\s+--install)\s+([\w.-]+)\s+(oci://[^\s\\`]+)([^\n]*)",
            text.replace("\\\n", " "),
        ):
            version = re.search(r"--version\s+[\"']?([v0-9][A-Za-z0-9._+-]*)", match[3])
            assets.append(
                {
                    "path": f"{path}#chart-{index}",
                    "file": path,
                    "guide": owner[7:],
                    "kind": "chart",
                    "release": match[1],
                    "chart": match[2],
                    "version": version[1] if version else "",
                    "checksum": checksum(match[0]),
                }
            )
            index += 1
    environment = {}
    if "guides/env.sh" in paths:
        for name, raw in re.findall(r"^export[ \t]+([A-Z][A-Z0-9_]*)=(.*)$", read("guides/env.sh"), re.M):
            assignment = re.fullmatch(r"""(?:"([^"]*)"|'([^']*)'|([^\s'"]+))(?:[ \t]+#.*)?[ \t]*""", raw)
            if not assignment:
                continue
            value = next(part for part in assignment.groups() if part is not None)
            default = re.fullmatch(r"\$\{" + name + r":-([^{}]+)\}", value)
            if default and assignment[2] is None:
                value = default[1]
            # Read declarative literals only; never evaluate the Guide's shell code.
            if re.fullmatch(r"[A-Za-z0-9_./:@+,-]*", value):
                environment[name] = value
    return {
        "commit": commit,
        "guides": guides,
        "assets": assets,
        "environment": environment,
        "warnings": warnings,
        "indexText": read("guides/README.md") if "guides/README.md" in paths else "",
        "optimizations": {**optimization_catalog(guides, commit), "sources": facts},
    }


async def authorized_inventory(request, selection: dict) -> tuple[Path, dict]:
    source = await load_guide_paths(request, selection)
    root = Path(source["localRoot"]).resolve()
    catalog = await inventory(root)
    if selection.get("commit") and selection["commit"] != catalog["commit"]:
        raise GuideError("Source revision changed. Reload the catalog before generating.", 409)
    return root, catalog
