"""Mount the versioned KV probe into newly rendered vLLM model containers."""

from dataclasses import replace
from pathlib import Path

import yaml

from llm_d_lens.core.hashing import stable_hash
from llm_d_lens.infrastructure.artifacts.manifests import text_checksum

MOUNT = "/opt/prism-kv-trace"
LABEL = "prism.llm-d.ai/kv-trace"


def _templates(resource):
    kind, spec = resource.get("kind"), resource.get("spec", {})
    if kind == "List":
        for item in resource.get("items", []):
            yield from _templates(item)
    elif kind == "DisaggregatedSet":
        for role in spec.get("roles", []):
            yield from _templates({"kind": "LeaderWorkerSet", "spec": role.get("spec", {})})
    elif kind == "LeaderWorkerSet":
        template = spec.get("leaderWorkerTemplate", {})
        for name in ("leaderTemplate", "workerTemplate"):
            if name in template:
                yield template[name]
    elif kind == "Pod":
        yield resource
    elif kind in {"Deployment", "StatefulSet", "DaemonSet"}:
        yield spec.get("template", {})


def instrument_manifest(artifact, output_root: Path):
    if not artifact.manifest_ref or not Path(artifact.manifest_ref).is_file():
        return artifact
    documents = list(yaml.safe_load_all(Path(artifact.manifest_ref).read_text()))
    patched, namespaces = 0, set()
    source = Path(__file__).parent
    data = {
        "sitecustomize.py": (source / "sitecustomize.py").read_text(),
        "prism_kv_engine.py": (source / "engine.py").read_text(),
    }
    probe_hash = stable_hash(data).removeprefix("sha256:")[:12]
    config_name = f"prism-kv-trace-{probe_hash}"
    for resource, template in [
        (document, pod_template)
        for document in documents
        if isinstance(document, dict)
        for pod_template in _templates(document)
    ]:
        if not isinstance(resource, dict):
            continue
        pod_spec = template.get("spec", {})
        for container in pod_spec.get("containers", []):
            args = " ".join(str(x) for x in container.get("command", []) + container.get("args", []))
            if "vllm" not in args and "vllm" not in str(container.get("image", "")):
                continue
            # Never install into EPP/render/tokenizer sidecars.
            if not (
                "vllm" in args or "--model" in args or container.get("name") in {"vllm", "model-server", "modelserver"}
            ):
                continue
            env = container.setdefault("env", [])
            if any(item.get("name") == "PRISM_KV_TRACE_NAMESPACE" for item in env):
                continue
            pythonpath = next((item for item in env if item.get("name") == "PYTHONPATH"), None)
            if pythonpath and "valueFrom" in pythonpath:
                continue  # Cannot safely compose an unresolved PYTHONPATH.
            identity = stable_hash({"image": container.get("image"), "args": args, "source": artifact.source_ref})
            # Identity need only be stable across replicas of this deployment.
            # Native KV hashes remain local; the probe canonicalizes token chains.
            env.append({"name": "PRISM_KV_TRACE_NAMESPACE", "value": identity})
            if pythonpath:
                pythonpath["value"] = f"{MOUNT}:{pythonpath.get('value', '')}"
            else:
                env.append({"name": "PYTHONPATH", "value": MOUNT})
            container.setdefault("volumeMounts", []).append(
                {"name": "prism-kv-trace", "mountPath": MOUNT, "readOnly": True}
            )
            if not any(v.get("name") == "prism-kv-trace" for v in pod_spec.setdefault("volumes", [])):
                pod_spec["volumes"].append({"name": "prism-kv-trace", "configMap": {"name": config_name}})
            template.setdefault("metadata", {}).setdefault("labels", {})[LABEL] = "v1"
            namespace = resource.get("metadata", {}).get("namespace")
            namespaces.add(namespace)
            patched += 1
    if not patched:
        return artifact
    for namespace in sorted(namespaces, key=lambda x: x or ""):
        documents.insert(
            0,
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": config_name, **({"namespace": namespace} if namespace else {})},
                "data": data,
            },
        )
    content = yaml.safe_dump_all(documents, sort_keys=False)
    checksum = text_checksum(content)
    path = output_root / checksum.removeprefix("sha256:") / "manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return replace(
        artifact,
        manifest_ref=str(path),
        manifest_checksum=checksum,
        artifact_hash=stable_hash({"provider": artifact.artifact_hash, "manifest": checksum}),
        deployment_contract={
            **artifact.deployment_contract,
            "kvTrace": {"probeVersion": 1, "containers": patched, "scope": "completed-full-prompt-blocks"},
        }
        if artifact.deployment_contract
        else {},
    )
