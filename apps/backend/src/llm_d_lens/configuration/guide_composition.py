"""Source-bound composition of a native CPU KV cache into a serving Guide."""

import json
import math
from copy import deepcopy

from kubernetes.utils.quantity import parse_quantity

from llm_d_lens.deploy.providers.guide_adapter import set_vllm_argument

from .manifest_facts import _invocation_tokens, _option, workload_roles


def compose_native_cpu_cache(base, donor, *, guide, capacity_gib):
    """Return a new manifest; never overwrite a transport connector or import a router."""
    if guide not in {"optimized-baseline", "precise-prefix-cache-routing", "pd-disaggregation"}:
        raise ValueError("No native CPU cache composition adapter exists for this Guide.")
    if (
        isinstance(capacity_gib, bool)
        or not isinstance(capacity_gib, (int, float))
        or not math.isfinite(capacity_gib)
        or not 0 < capacity_gib <= 1024
    ):
        raise ValueError("CPU cache capacity must be greater than zero and at most 1024 GiB per replica.")
    sources = [c for _, c in composition_servers(donor)]
    if not sources:
        raise ValueError("The native CPU cache source has no model server.")
    connectors = []
    for server in sources:
        tokens = _invocation_tokens(server, "cache source")
        value = json.loads(_option(tokens, {"--kv-transfer-config"}, "cache source") or "{}")
        if (
            not isinstance(value, dict)
            or value.get("kv_connector") != "OffloadingConnector"
            or value.get("kv_role") != "kv_both"
            or value.get("kv_connector_module_path")
        ):
            raise ValueError("Only the source-declared native OffloadingConnector with kv_both can be composed.")
        extra = value.get("kv_connector_extra_config", {})
        if (
            not isinstance(extra, dict)
            or type(extra.get("cpu_bytes_to_use")) is not int
            or set(extra) - {"cpu_bytes_to_use"}
        ):
            raise ValueError("The cache source requires unsupported connector dependencies.")
        connectors.append(value)
    if any(value != connectors[0] for value in connectors):
        raise ValueError("The source has inconsistent CPU cache connectors.")
    output = deepcopy(base)
    targets = composition_servers(output)
    if guide == "pd-disaggregation" and {w["role"] for w, _ in targets} != {"prefill", "decode"}:
        raise ValueError("P/D composition requires both prefill and decode transport roles.")
    if not targets:
        raise ValueError("The selected Guide has no supported model server.")
    source_images = {c.get("image") for c in sources}
    extra_bytes = int(capacity_gib * 1024**3)
    for workload, server in targets:
        if not server.get("image") or source_images != {server["image"]}:
            raise ValueError(
                "Composition requires identical source runtime images; choose compatible pinned Guide versions."
            )
        command = server.get("command") or []
        if command and command[0].rsplit("/", 1)[-1] in {"sh", "bash"}:
            raise ValueError("Composition requires a direct model-server invocation.")
        tokens = _invocation_tokens(server, "composition target")
        if any(
            t == "--no-enable-prefix-caching"
            or t.lower() == "--enable-prefix-caching=false"
            or (t == "--enable-prefix-caching" and i + 1 < len(tokens) and tokens[i + 1].lower() == "false")
            for i, t in enumerate(tokens)
        ):
            raise ValueError("CPU cache composition conflicts with explicitly disabled prefix caching.")
        existing = json.loads(_option(tokens, {"--kv-transfer-config"}, "composition target") or "{}")
        if not isinstance(existing, dict):
            raise ValueError("KV transfer configuration must be an object.")
        config = deepcopy(connectors[0])
        config["kv_connector_extra_config"]["cpu_bytes_to_use"] = extra_bytes
        if guide == "pd-disaggregation":
            expected_role = {"prefill": "kv_producer", "decode": "kv_consumer"}.get(workload["role"])
            if (
                existing.get("kv_connector") != "NixlConnector"
                or existing.get("kv_role") not in {"kv_both", expected_role}
                or existing.get("kv_connector_module_path")
            ):
                raise ValueError(
                    "Adding CPU KV cache to P/D is not yet supported for this transport. "
                    "The cache adapter requires the built-in NixlConnector with its role-preserving "
                    "kv_both, kv_producer, or kv_consumer configuration; use a P/D candidate without "
                    "CPU KV offload to preserve the original transport."
                )
            # Keep transport-specific settings in the child; engine identity belongs
            # to the wrapper and is inherited by both children in MultiConnector.
            transport = deepcopy(existing)
            transport.pop("engine_id", None)
            config = {
                **existing,
                "kv_connector": "MultiConnector",
                "kv_connector_extra_config": {"connectors": [transport, config]},
            }
            probe = runtime_probe(server, config)
            initializers = workload["pod"].setdefault("initContainers", [])
            if any(c.get("name") == probe["name"] for c in initializers):
                raise ValueError("The connector capability probe name is already in use.")
            initializers.append(probe)
        elif existing:
            raise ValueError(
                "The selected Guide already owns a KV connector; no composition adapter exists for this transport."
            )
        # Normalize only the direct invocation, handling options in command or args.
        server["command"] = ["vllm", "serve"]
        server["args"] = set_vllm_argument(tokens, "kv-transfer-config", "")[:-1] + [
            "--kv-transfer-config",
            json.dumps(config, separators=(",", ":")),
        ]

        resources = server.setdefault("resources", {})
        requests = resources.setdefault("requests", {})
        limits = resources.setdefault("limits", {})
        requested = parse_quantity(str(requests.get("memory", "0")))
        limited = parse_quantity(str(limits.get("memory", requests.get("memory", "0"))))
        requests["memory"] = str(int(requested) + extra_bytes)
        if "memory" in limits:
            limits["memory"] = str(int(max(requested, limited)) + extra_bytes)
        if guide == "pd-disaggregation":
            initializers[-1] = runtime_probe(server, config)
    return output


# Runs in the selected image before model startup, not on the Lens host. This is
# an import/schema capability check, not a claim of GPU correctness/performance.
RUNTIME_PROBE = """import json, sys
from importlib.metadata import version
runtime_version = version('vllm')
if runtime_version.split('+')[0] == '0.19.0':
    raise RuntimeError(
        'vLLM 0.19.0 has a known CPU offloading defect (#39500/#39617); '
        'use an image containing the upstream fix.'
    )
from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
config = json.loads(sys.argv[1])
for item in [config, *config['kv_connector_extra_config']['connectors']]:
    KVConnectorFactory.get_connector_class(KVTransferConfig(**item))
print(
    'vLLM ' + runtime_version
    + ': KV connector import and schema checks passed; workload validation is still required.'
)
"""


def runtime_probe(server, config):
    probe = {
        "name": "lens-kv-connector-check",
        "image": server["image"],
        "command": ["python3", "-c", RUNTIME_PROBE],
        "args": [json.dumps(config, separators=(",", ":"))],
    }
    for key in ("imagePullPolicy", "env", "envFrom", "volumeMounts", "securityContext", "resources"):
        if key in server:
            probe[key] = deepcopy(server[key])
    return probe


def composed_cache_config(connector, *, pd=False):
    """Validate the bounded adapter shape, returning its cache child."""
    if not isinstance(connector, dict):
        raise ValueError("Composed KV connector must be an object.")
    if pd:
        children = connector.get("kv_connector_extra_config", {}).get("connectors")
        transport_role = (
            children[0].get("kv_role")
            if isinstance(children, list) and children and isinstance(children[0], dict)
            else None
        )
        if (
            connector.get("kv_connector") != "MultiConnector"
            or not isinstance(children, list)
            or len(children) != 2
            or not all(isinstance(child, dict) for child in children)
            or children[0].get("kv_connector") != "NixlConnector"
            or transport_role not in {"kv_both", "kv_producer", "kv_consumer"}
            or connector.get("kv_role") != transport_role
            or any(child.get("kv_connector_module_path") for child in [connector, *children])
        ):
            raise ValueError("P/D composition requires ordered NixlConnector and OffloadingConnector children.")
        connector = children[1]
    if connector.get("kv_connector") != "OffloadingConnector" or connector.get("kv_role") != "kv_both":
        raise ValueError("Composed cache must use OffloadingConnector with kv_both.")
    return connector


def composition_servers(documents):
    """Include explicit leaders as well as worker templates in grouped workloads."""
    workloads = workload_roles(documents)
    for workload in list(workloads):
        leader = workload["spec"].get("leaderWorkerTemplate", {}).get("leaderTemplate", {}).get("spec")
        if leader:
            workloads.append({**workload, "pod": leader})
    return [(w, c) for w in workloads for c in w["pod"].get("containers", []) if c.get("name") == "modelserver"]
