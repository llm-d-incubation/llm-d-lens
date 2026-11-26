"""A plan: what a stack renders to in one namespace, and every finding about it.

A plan is the same object offline (no cluster) and against a cluster; cluster checks add
findings to it (``preflight``). Its digest covers what would be applied, so ``standup
--expected-digest`` refuses to apply anything other than the plan that was reviewed.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from . import overrides, yamlio
from .catalog import Stack
from .errors import UsageError
from .model import Finding, is_dns_label, object_ref, validate_instance_name, validate_namespace
from .policy import PolicyContext, PolicyResult, ScopeResolver, apply_policy, containers, fallback_scope, pod_templates
from .process import Runner
from .render import Tools, render


@dataclass
class Options:
    """What a user may change about a plan; everything else comes from the stack's files."""

    namespace: str
    instance: str
    allow_privileged: bool = False
    skip_admin_parts: bool = False
    #: Let the instance move to this stack path from the one it runs (the objects stay where they are).
    switch_stack: bool = False
    #: A guide's variables (``--var NAME=VALUE``): they pick its variant (accelerator, model server,
    #: provider) as the README's ``export`` lines do.
    variables: dict[str, str] = field(default_factory=dict)
    #: ``--set KEY=VALUE`` for every Helm chart the guide installs (the router chart).
    sets: list[str] = field(default_factory=list)
    #: Mount an existing model PVC at ``/model-cache`` and load this relative directory from it.
    model_pvc_claim_name: str = ""
    model_subpath: str = ""
    model_containers: list[str] = field(default_factory=list)
    #: Replica counts set with ``llmd scale`` (workload -> replicas), applied over the files.
    overrides: dict[str, int] = field(default_factory=dict)

    def choices(self) -> dict[str, Any]:
        """What the user chose about the render, kept with the instance."""
        return {
            **({"variables": dict(sorted(self.variables.items()))} if self.variables else {}),
            **({"sets": list(self.sets)} if self.sets else {}),
            **({"modelPvcClaimName": self.model_pvc_claim_name} if self.model_pvc_claim_name else {}),
            **({"modelSubpath": self.model_subpath} if self.model_subpath else {}),
            **({"modelContainers": sorted(set(self.model_containers))} if self.model_containers else {}),
        }


@dataclass
class Plan:
    stack: Stack
    options: Options
    command: str
    policy: PolicyResult
    #: Results of the checks against the cluster, when there was one.
    cluster: dict[str, Any] = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return self.options.namespace

    @property
    def instance(self) -> str:
        return self.options.instance

    @property
    def documents(self) -> list[dict[str, Any]]:
        return self.policy.documents

    @property
    def findings(self) -> list[Finding]:
        return self.policy.findings

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    #: For a guide: how each of its steps was followed, and the variables they saw.
    steps: list[dict[str, str]] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return yamlio.digest({"namespace": self.namespace, "instance": self.instance, "documents": self.documents})

    def stack_record(self) -> dict[str, Any]:
        """The stack as the instance's record keeps it: its path, source, and the user's choices."""
        return {"id": self.stack.id, "source": self.stack.source.describe(), **self.options.choices()}

    def objects(self) -> list[dict[str, str]]:
        return [
            {"apiVersion": doc["apiVersion"], "kind": doc["kind"], "name": doc["metadata"]["name"]}
            for doc in self.documents
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "stack": self.stack.id,
            "source": self.stack.source.describe(),
            "namespace": self.namespace,
            "instance": self.instance,
            "render": self.command,
            **({"steps": self.steps, "variables": self.variables} if self.stack.kind == "guide" else {}),
            "choices": self.options.choices(),
            "objects": [object_ref(doc) for doc in self.documents],
            "workloads": self.policy.workloads,
            "accelerators": self.policy.accelerators,
            "prerequisites": self.policy.prerequisites,
            "adminParts": [
                {"object": object_ref(part["document"]), "reason": part["reason"]} for part in self.policy.admin_parts
            ],
            "findings": [finding.as_dict() for finding in self.findings],
            "cluster": self.cluster,
            "digest": self.digest,
            "blocking": len(self.blocking),
        }


def discovered_scope(kube) -> ScopeResolver:
    """Each kind's scope by the cluster's API discovery, the built-in table where it is silent."""

    def scope(api_version: str, kind: str) -> bool | None:
        served = kube.scope(api_version, kind)
        return fallback_scope(api_version, kind) if served is None else served

    return scope


def _apply_model_pvc(
    documents: list[dict[str, Any]],
    claim_name: str,
    subpath: str,
    container_names: list[str] | None = None,
    served_model_name: str = "",
) -> None:
    if not claim_name:
        if subpath or container_names:
            raise UsageError("--model-subpath and --model-container require --model-pvc-claim-name")
        return
    if len(claim_name) > 253 or not all(is_dns_label(part) for part in claim_name.split(".")):
        raise UsageError("--model-pvc-claim-name must be a DNS subdomain")
    if subpath and (
        subpath.startswith("/")
        or "\\" in subpath
        or any(part in {"", ".", ".."} for part in subpath.split("/"))
        or any(char in subpath for char in "\r\n\0")
    ):
        raise UsageError("--model-subpath must be a relative path without empty, . or .. segments")

    selected = set(container_names or ("modelserver", "modelserver-leader", "modelserver-worker"))
    if any(not is_dns_label(name) for name in selected):
        raise UsageError("--model-container must be a container name (a DNS label)")
    model_path = "/model-cache" + (f"/{subpath}" if subpath else "")
    volume = {"name": "model-cache", "persistentVolumeClaim": {"claimName": claim_name}}
    env_values = {
        "HF_HOME": "/model-cache",
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_XET": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    found: set[str] = set()
    for document in documents:
        for _, template, _ in pod_templates(document):
            pod_spec = template["spec"]
            servers = [container for container in containers(template) if container.get("name") in selected]
            if not servers:
                continue
            found.update(container["name"] for container in servers)
            volumes = pod_spec.get("volumes") or []
            pod_spec["volumes"] = volumes
            existing_volume = next((item for item in volumes if item.get("name") == "model-cache"), None)
            if existing_volume and existing_volume != volume:
                raise UsageError("the modelserver Pod already defines a different model-cache volume")
            if not existing_volume:
                volumes.append(volume)
            for container in servers:
                mounts = container.get("volumeMounts") or []
                container["volumeMounts"] = mounts
                mount = {"name": "model-cache", "mountPath": "/model-cache"}
                existing_mounts = [
                    item
                    for item in mounts
                    if item.get("name") == "model-cache" or str(item.get("mountPath", "")).rstrip("/") == "/model-cache"
                ]
                if len(existing_mounts) > 1 or any(item != mount for item in existing_mounts):
                    raise UsageError("the modelserver container already defines a different model-cache mount")
                if not existing_mounts:
                    mounts.append(mount)
                _set_model_argument(container, model_path, served_model_name)
                env = container.get("env") or []
                container["env"] = env
                for name, value in env_values.items():
                    env[:] = [item for item in env if item.get("name") != name]
                    env.append({"name": name, "value": value})
    if container_names and selected - found:
        raise UsageError("model containers not found in the selected stack: " + ", ".join(sorted(selected - found)))
    if not found:
        raise UsageError("the selected stack has no modelserver container; select one with --model-container NAME")


def _set_model_argument(container: dict[str, Any], model_path: str, served_model_name: str = "") -> None:
    command = container.get("command") or []
    args = container.get("args") or []
    values = [*command, *args]
    if any(str(part).rsplit("/", 1)[-1] in {"sh", "bash", "dash", "ash", "zsh", "ksh"} for part in command or args[:1]):
        raise UsageError("cannot set a PVC model path in a shell-based modelserver command")
    executable = str(values[0]).rsplit("/", 1)[-1] if values else ""
    direct_server = executable in {"vllm", "sglang"} and values[1:2] == ["serve"]
    python_server = executable in {"python", "python3"} and values[1:3] in (
        ["-m", "vllm.entrypoints.openai.api_server"],
        ["-m", "sglang.launch_server"],
    )
    if command and not (direct_server or python_server):
        raise UsageError("cannot identify a supported vLLM or SGLang modelserver command")
    model_arguments = [
        index
        for index, value in enumerate(values)
        if value in {"--model", "--model-path"} or value.startswith(("--model=", "--model-path="))
    ]
    if len(model_arguments) > 1:
        raise UsageError("the modelserver container defines more than one model argument")
    if model_arguments:
        index = model_arguments[0]
        value = values[index]
        if value in {"--model", "--model-path"}:
            if index + 1 == len(values) or not values[index + 1] or values[index + 1].startswith("-"):
                raise UsageError(f"{value} has no model path in the modelserver container")
            original_model = values[index + 1]
            values[index + 1] = model_path
        else:
            flag, original_model = value.split("=", 1)
            if not original_model:
                raise UsageError(f"{flag} has no model path in the modelserver container")
            values[index] = flag + "=" + model_path
    else:
        position = 2 if direct_server and executable == "vllm" else 0
        if (command and not (direct_server and executable == "vllm")) or position >= len(values):
            raise UsageError("cannot identify the model argument in the modelserver container")
        if not values[position] or values[position].startswith("-"):
            raise UsageError("cannot identify the model argument in the modelserver container")
        original_model = values[position]
        values[position] = model_path
    api_model_name = served_model_name or original_model
    served_model_arguments = [
        index
        for index, value in enumerate(values)
        if value == "--served-model-name" or value.startswith("--served-model-name=")
    ]
    if not served_model_arguments:
        values.extend(["--served-model-name", api_model_name])
    else:
        for index in served_model_arguments:
            if values[index] == "--served-model-name":
                if index + 1 == len(values) or values[index + 1].startswith("-"):
                    raise UsageError("--served-model-name has no model name in the modelserver container")
                values[index + 1] = api_model_name
            else:
                values[index] = "--served-model-name=" + api_model_name
    if "command" in container or command:
        container["command"] = values[: len(command)]
    if "args" in container or values[len(command) :]:
        container["args"] = values[len(command) :]


def make_plan(
    stack: Stack,
    options: Options,
    *,
    runner: Runner,
    tools: Tools | None = None,
    scope: ScopeResolver = fallback_scope,
    label: bool = True,
) -> Plan:
    """Render the stack and apply the namespace policy. Never contacts the cluster."""
    validate_namespace(options.namespace)
    validate_instance_name(options.instance)
    rendered = render(
        stack,
        runner=runner,
        tools=tools,
        namespace=options.namespace,
        variables=options.variables,
        sets=options.sets,
    )
    context = PolicyContext(
        namespace=options.namespace,
        instance=options.instance,
        allow_privileged=options.allow_privileged,
        skip_admin_parts=options.skip_admin_parts,
        label=label,
        scope=scope,
    )
    documents = copy.deepcopy(rendered.documents)
    _apply_model_pvc(
        documents,
        options.model_pvc_claim_name,
        options.model_subpath,
        options.model_containers,
        options.variables.get("MODEL", "") if stack.kind == "guide" else "",
    )
    scaled = overrides.apply(documents, options.overrides)
    policy = apply_policy(documents, context)
    policy.findings[:0] = rendered.findings
    policy.findings.extend(scaled)
    return Plan(
        stack=stack,
        options=options,
        command=rendered.command,
        policy=policy,
        steps=rendered.steps,
        variables=rendered.variables,
    )
