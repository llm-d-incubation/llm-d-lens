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
from .guides import Environment
from .model import Finding, object_ref, validate_instance_name, validate_namespace
from .policy import PolicyContext, PolicyResult, ScopeResolver, apply_policy, fallback_scope
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
    declarations = endpoint_declarations(stack, rendered.variables)
    endpoints, findings = resolve_endpoints(declarations, policy.documents, options.namespace)
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


def endpoint_declarations(stack: Stack, variables: dict[str, str]) -> Any:
    directory = stack.path if stack.path.is_dir() else stack.path.parent
    path = directory / ("guide.yaml" if stack.kind == "guide" else "lens.yaml")
    if not path.is_file():
        return []
    data = yamlio.load_file(path)
    if stack.kind == "guide" and isinstance(data, dict):
        data = data.get("lens", {})
    if not isinstance(data, dict):
        return None
    declarations = copy.deepcopy(data.get("endpoints", []))
    if isinstance(declarations, list):
        env = Environment()
        env.values.update(variables)
        for declaration in declarations:
            if isinstance(declaration, dict):
                for key in ("service", "port", "scheme", "purpose"):
                    value = declaration.get(key)
                    if isinstance(value, str):
                        env.assign("LENS_ENDPOINT_VALUE", value)
                        declaration[key] = env.get("LENS_ENDPOINT_VALUE")
    return declarations


def preferred_inference_endpoint(endpoints: list[dict[str, Any]]) -> dict[str, Any] | None:
    inference = [endpoint for endpoint in endpoints if endpoint["purpose"] == "inference"]
    preferred = [endpoint for endpoint in inference if endpoint["preferred"]]
    if len(preferred) == 1:
        return preferred[0]
    return inference[0] if len(inference) == 1 and not preferred else None


def resolve_endpoints(
    declarations: Any, documents: list[dict[str, Any]], namespace: str
) -> tuple[list[dict[str, Any]], list[Finding]]:
    endpoints, findings = [], []
    if not isinstance(declarations, list):
        return [], [Finding("error", "endpoint-invalid", "Endpoint declarations must be a list.")]
    services = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("apiVersion") == "v1" and doc.get("kind") == "Service"
    }
    for declaration in declarations:
        if not isinstance(declaration, dict):
            findings.append(Finding("error", "endpoint-invalid", "Each endpoint must be a mapping."))
            continue
        purpose, service, port = (declaration.get(key) for key in ("purpose", "service", "port"))
        scheme, preferred = declaration.get("scheme", "http"), declaration.get("preferred", False)
        if (
            not isinstance(purpose, str)
            or not purpose.strip()
            or not isinstance(service, str)
            or not service
            or type(port) not in (str, int)
            or scheme not in ("http", "https")
            or not isinstance(preferred, bool)
            or (preferred and purpose != "inference")
        ):
            findings.append(Finding("error", "endpoint-invalid", f"Invalid endpoint declaration: {declaration!r}"))
            continue
        document = services.get(service)
        ports = [
            candidate
            for candidate in ((document or {}).get("spec") or {}).get("ports", [])
            if (candidate.get("name") == port if isinstance(port, str) else candidate.get("port") == port)
            and candidate.get("protocol", "TCP") == "TCP"
        ]
        if len(ports) != 1:
            findings.append(
                Finding(
                    "error", "endpoint-unresolved", f"Endpoint requires one TCP port {port!r} on Service/{service}."
                )
            )
            continue
        resolved = ports[0]
        endpoints.append(
            {
                "purpose": purpose,
                "service": service,
                "namespace": namespace,
                "port": resolved["port"],
                "portName": resolved.get("name"),
                "portSelector": port,
                "scheme": scheme,
                "preferred": preferred,
                "url": f"{scheme}://{service}.{namespace}.svc.cluster.local:{resolved['port']}",
            }
        )
    inference = [endpoint for endpoint in endpoints if endpoint["purpose"] == "inference"]
    if inference and preferred_inference_endpoint(endpoints) is None:
        findings.append(Finding("error", "endpoint-ambiguous", "Declare exactly one preferred inference endpoint."))
    return endpoints, findings
