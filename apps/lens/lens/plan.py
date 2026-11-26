"""A plan: what a stack renders to in one namespace, and every finding about it.

A plan is the same object offline (no cluster) and against a cluster; cluster checks add
findings to it (``preflight``). Its digest covers what would be applied, so ``standup
--expected-digest`` refuses to apply anything other than the plan that was reviewed.
"""

from __future__ import annotations

import copy
import shlex
from dataclasses import dataclass, field
from typing import Any

from . import health, overrides, yamlio
from .catalog import Stack
from .model import Finding, object_ref, validate_instance_name, validate_namespace
from .policy import PolicyContext, PolicyResult, ScopeResolver, apply_policy, fallback_scope, pod_templates
from .process import Runner
from .render import Tools, render

LWS_WORKER_INDEX_LABEL = "leaderworkerset.sigs.k8s.io/worker-index"


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
    #: Replica counts set with ``llmd scale`` (workload -> replicas), applied over the files.
    overrides: dict[str, int] = field(default_factory=dict)

    def choices(self) -> dict[str, Any]:
        """What the user chose about the render, kept with the instance."""
        return {
            **({"variables": dict(sorted(self.variables.items()))} if self.variables else {}),
            **({"sets": list(self.sets)} if self.sets else {}),
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
    endpoint_details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def inference_endpoint(self) -> dict[str, Any] | None:
        return preferred_inference_endpoint(self.endpoint_details)

    @property
    def digest(self) -> str:
        content = {"namespace": self.namespace, "instance": self.instance, "documents": self.documents}
        if self.endpoint_details:
            content["endpoints"] = self.endpoint_details
        return yamlio.digest(content)

    def stack_record(self) -> dict[str, Any]:
        """The stack as the instance's record keeps it: its path, source, and the user's choices."""
        return {
            "id": self.stack.id,
            "source": self.stack.source.describe(),
            **self.options.choices(),
            "endpointDiscovery": "auto",
            "endpoints": self.endpoint_details,
        }

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
            "endpointDetails": self.endpoint_details,
            "inferenceEndpoint": self.inference_endpoint,
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
    scaled = overrides.apply(documents, options.overrides)
    policy = apply_policy(documents, context)
    policy.findings[:0] = rendered.findings
    policy.findings.extend(scaled)
    endpoints, findings = discover_endpoints(policy.documents, options.namespace)
    policy.findings.extend(findings)
    return Plan(
        stack=stack,
        options=options,
        command=rendered.command,
        policy=policy,
        steps=rendered.steps,
        variables=rendered.variables,
        endpoint_details=endpoints,
    )


def _container_role(container: dict[str, Any]) -> str | None:
    name = str(container.get("name", "")).lower()
    image = str(container.get("image", "")).lower()
    invocation = " ".join(str(value) for value in [*container.get("command", []), *container.get("args", [])])
    if any(token in name or token in image for token in ("envoy", "agentgateway")):
        return "router"
    if name in {"modelserver", "model-server", "vllm", "sglang"} or any(
        token in image or token in invocation for token in ("vllm", "sglang")
    ):
        return "model"
    return None


def _container_ports(container: dict[str, Any], role: str) -> dict[str | int, int]:
    ports = {}
    for port in container.get("ports") or []:
        number = port.get("containerPort")
        name = str(port.get("name") or "").lower()
        internal = any(part in name for part in ("metrics", "health", "status", "grpc", "admin"))
        if type(number) is int and port.get("protocol", "TCP") == "TCP" and not internal:
            ports[number] = number
            if port.get("name"):
                ports[port["name"]] = number
    if role == "model":
        try:
            tokens = shlex.split(
                " ".join(str(value) for value in [*container.get("command", []), *container.get("args", [])])
            )
        except ValueError:
            return ports
        number = None
        for index, argument in enumerate(tokens):
            if argument == "--port":
                if index + 1 == len(tokens):
                    return ports
                value = tokens[index + 1]
            elif argument.startswith("--port="):
                value = argument.partition("=")[2]
            else:
                continue
            if not value.isdigit():
                return ports
            number = int(value)
        if number is not None:
            ports[number] = number
        elif not container.get("ports"):
            ports[8000] = 8000
    return ports


def _lws_worker_index_matches(document: dict[str, Any], location: str, value: str) -> bool:
    spec = document.get("spec") or {}
    if document.get("kind") == "DisaggregatedSet":
        role_name = location.split("roles[", 1)[1].split("]", 1)[0]
        role = next((role for role in spec.get("roles") or [] if role.get("name") == role_name), {})
        spec = role.get("spec") or {}
    template = spec.get("leaderWorkerTemplate") or {}
    size = int(template.get("size", 1) or 1)
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return False
    index = int(value)
    if str(index) != value or not 0 <= index < size:
        return False
    if location.endswith("leaderTemplate"):
        return index == 0
    leader = (template.get("leaderTemplate") or {}).get("spec") or {}
    return index > 0 or not isinstance(leader.get("containers"), list)


def _template_labels(document: dict[str, Any], location: str, template: dict[str, Any]) -> tuple[dict, dict]:
    labels = copy.deepcopy((template.get("metadata") or {}).get("labels") or {})
    selector_labels = dict(labels)
    kind = document.get("kind")
    name = document["metadata"]["name"]
    if kind == "LeaderWorkerSet":
        labels[health.LWS_NAME_LABEL] = selector_labels[health.LWS_NAME_LABEL] = name
    elif kind == "DisaggregatedSet":
        labels[health.DS_NAME_LABEL] = selector_labels[health.DS_NAME_LABEL] = name
        role = location.split("roles[", 1)[1].split("]", 1)[0]
        labels[health.DS_ROLE_LABEL] = selector_labels[health.DS_ROLE_LABEL] = role
    return labels, selector_labels


def _pod_matches_backend(pod: dict[str, Any], selector: dict[str, str], labels: dict[str, Any]) -> bool:
    if not health.pod_ready(pod) or (pod.get("status") or {}).get("phase") != "Running":
        return False
    pod_labels = (pod.get("metadata") or {}).get("labels") or {}
    return all(pod_labels.get(key) == value for key, value in [*selector.items(), *labels.items()])


def discover_endpoints(
    documents: list[dict[str, Any]], namespace: str, *, available_pods: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], list[Finding]]:
    endpoints, ranks = [], {}
    templates = []
    for document in documents:
        if (document.get("metadata") or {}).get("namespace", namespace) != namespace:
            continue
        for location, template, _ in pod_templates(document):
            template = copy.deepcopy(template)
            labels, selector_labels = _template_labels(document, location, template)
            templates.append((document, location, template, labels, selector_labels))
    for service in documents:
        if service.get("kind") != "Service" or service.get("apiVersion") != "v1":
            continue
        if (service.get("metadata") or {}).get("namespace", namespace) != namespace:
            continue
        spec = service.get("spec") or {}
        selector = spec.get("selector") or {}
        name = service["metadata"]["name"]
        for port in spec.get("ports") or []:
            number = port.get("port")
            if type(number) is not int or port.get("protocol", "TCP") != "TCP":
                continue
            port_name = str(port.get("name") or "").lower()
            scheme = "https" if number == 443 or port_name == "https" else "http"
            endpoint = {
                "purpose": "unknown",
                "service": name,
                "namespace": namespace,
                "port": number,
                "portName": port.get("name"),
                "portSelector": port.get("name") or number,
                "scheme": scheme,
                "preferred": False,
                "url": f"{scheme}://{name}.{namespace}.svc.cluster.local:{number}",
                "discoverySource": "service-selector",
                "reason": "No matching serving container.",
            }
            excluded = number in {9002, 9090} or any(
                part in port_name for part in ("metrics", "health", "status", "grpc", "admin")
            )
            if "metrics" in port_name or number == 9090:
                endpoint["purpose"] = "metrics"
            matches = []
            for document, location, template, labels, selector_labels in templates:
                if (
                    document.get("kind") in {"LeaderWorkerSet", "DisaggregatedSet"}
                    and LWS_WORKER_INDEX_LABEL in selector
                ):
                    worker_index = selector[LWS_WORKER_INDEX_LABEL]
                    if not _lws_worker_index_matches(document, location, worker_index):
                        continue
                    selector_labels = {**selector_labels, LWS_WORKER_INDEX_LABEL: worker_index}
                if not selector or not all(selector_labels.get(key) == value for key, value in selector.items()):
                    continue
                if available_pods is not None and not any(
                    _pod_matches_backend(pod, selector, labels) for pod in available_pods
                ):
                    continue
                identity = " ".join(
                    [location, str(document.get("metadata", {}).get("name", "")), *map(str, labels.values())]
                ).lower()
                for container in (template.get("spec") or {}).get("containers") or []:
                    role = _container_role(container)
                    if role is None or excluded:
                        continue
                    target = port.get("targetPort", number)
                    if isinstance(target, str) and target.isdigit():
                        target = int(target)
                    resolved = _container_ports(container, role).get(target)
                    if resolved is None or resolved in {9002, 9090}:
                        continue
                    invocation = " ".join(str(value) for value in container.get("args") or []).lower()
                    if role == "model" and ("prefill" in identity or "kv_producer" in invocation):
                        continue
                    rank = 0 if role == "router" else 1 if "decode" in identity else 2
                    matches.append((rank, object_ref(document), location, container.get("name", ""), resolved))
            if matches:
                rank = min(match[0] for match in matches)
                endpoint.update(
                    purpose="inference",
                    endpointType="router" if rank == 0 else "model",
                    reason="Service targetPort matches a serving container.",
                    backends=[
                        {"object": ref, "template": location, "container": container, "targetPort": target}
                        for _, ref, location, container, target in sorted(set(matches))
                    ],
                )
                ranks[len(endpoints)] = (rank, spec.get("clusterIP") == "None")
            endpoints.append(endpoint)
    findings = []
    if ranks:
        best = min(ranks.values())
        selected = [index for index, rank in ranks.items() if rank == best]
        if len(selected) == 1:
            endpoints[selected[0]]["preferred"] = True
        else:
            findings.append(
                Finding(
                    "warning", "endpoint-ambiguous", "Multiple equally eligible inference endpoints; none selected."
                )
            )
    else:
        findings.append(
            Finding(
                "warning", "endpoint-not-found", "No inference Service could be identified from the stack's workloads."
            )
        )
    return endpoints, findings


def preferred_inference_endpoint(endpoints: list[dict[str, Any]]) -> dict[str, Any] | None:
    inference = [endpoint for endpoint in endpoints if endpoint["purpose"] == "inference"]
    preferred = [endpoint for endpoint in inference if endpoint["preferred"]]
    if len(preferred) == 1:
        return preferred[0]
    return inference[0] if len(inference) == 1 and not preferred else None


def revalidate_endpoint(
    recorded: dict[str, Any], candidates: list[dict[str, Any]], available_objects: set[str]
) -> tuple[dict[str, Any] | None, list[Finding]]:
    selector = recorded.get("portSelector", recorded["port"])
    matching = [
        endpoint
        for endpoint in candidates
        if endpoint["service"] == recorded["service"]
        and (endpoint["portName"] == selector if isinstance(selector, str) else endpoint["port"] == selector)
        and endpoint["purpose"] == "inference"
    ]
    backends = {backend["object"] for backend in recorded.get("backends", [])}
    if len(matching) != 1 or (backends and not backends.issubset(available_objects)):
        return None, [
            Finding(
                "warning",
                "endpoint-unresolved",
                "The recorded inference Service, port or backing workload is unavailable.",
            )
        ]
    endpoint = matching[0]
    expected = {
        (backend["object"], backend["template"], backend["container"], backend["targetPort"])
        for backend in recorded.get("backends", [])
    }
    actual = {
        (backend["object"], backend["template"], backend["container"], backend["targetPort"])
        for backend in endpoint.get("backends", [])
    }
    if expected and not expected.issubset(actual):
        return None, [
            Finding("warning", "endpoint-unresolved", "The recorded inference backend or targetPort changed.")
        ]
    return endpoint, []
