"""Managed lifecycle for fully rendered, namespace-neutral source compositions."""

from dataclasses import replace
from pathlib import Path

import yaml

from llm_d_lens.core.hashing import stable_hash
from llm_d_lens.deploy.providers.configuration_manifest import ConfigurationManifestAdapter
from llm_d_lens.deploy.providers.guide_adapter import GuideDefinition, ValidationResult
from llm_d_lens.infrastructure.artifacts.store import register_artifacts


class SourceCompositionAdapter(ConfigurationManifestAdapter):
    """Reuse manifest deployment without running a second Guide router installation."""

    managed_resource_kinds = frozenset(
        {
            "Deployment",
            "Service",
            "ConfigMap",
            "Secret",
            "ServiceAccount",
            "Role",
            "RoleBinding",
            "ResourceClaimTemplate",
            "ResourceClaim",
            "InferencePool",
            "HTTPRoute",
            "Gateway",
            "PodMonitor",
            "ServiceMonitor",
            "HorizontalPodAutoscaler",
            "ScaledObject",
            "TriggerAuthentication",
            "NetworkPolicy",
            "PodDisruptionBudget",
        }
    )

    def __init__(self, command_runner, namespace_prefix, readiness_timeout_seconds, **kwargs):
        super().__init__(None, command_runner, namespace_prefix, readiness_timeout_seconds, **kwargs)

    def discover(self):
        return GuideDefinition("source-composition", "generated", "source-composition.v1", "experimental")

    def validate_inputs(self, definition, cluster_snapshot, overrides):
        if not self._is_exact(overrides):
            return ValidationResult(False, ["Source composition requires a rendered manifest."])
        result = super().validate_inputs(definition, cluster_snapshot, overrides)
        if not result.accepted:
            return result
        official = overrides["officialGuide"]
        if official.get("deploymentBundle"):
            return ValidationResult(
                False, ["Source composition must include rendered router resources, not Helm inputs."]
            )
        # Only resources whose lifecycle is covered by namespace cleanup and Deployment
        # rollout are admitted. Existing-workload integrations are not new deployments.
        allowed = self.managed_resource_kinds
        documents = list(yaml.safe_load_all(official["renderedManifest"]))
        unsupported = sorted(
            {str(doc.get("kind")) for doc in documents if isinstance(doc, dict) and doc.get("kind") not in allowed}
        )
        if unsupported:
            return ValidationResult(
                False, ["Managed composition lifecycle does not support: " + ", ".join(unsupported)]
            )
        return result

    async def render(self, definition, overrides):
        validation = self.validate_inputs(definition, {}, overrides)
        if not validation.accepted:
            raise ValueError(" ".join(validation.reasons))
        artifact = await super().render(definition, overrides)
        labels = (overrides.get("runtime") or {}).get("namespaceLabels") or {}
        if not isinstance(labels, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
        ):
            raise ValueError("Namespace labels must be string pairs.")
        return replace(
            artifact,
            artifact_hash=stable_hash({"manifest": artifact.artifact_hash, "namespaceLabels": labels}),
            deployment_contract={**artifact.deployment_contract, "namespaceLabels": labels},
        )

    async def deploy(self, artifact, execution_context):
        labels = artifact.deployment_contract.get("namespaceLabels")
        if labels:
            namespace = self._namespace(execution_context)
            path = Path(artifact.manifest_ref).with_name(f"{namespace}-namespace.yaml")
            path.write_text(
                yaml.safe_dump(
                    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": labels}}
                ),
                encoding="utf-8",
            )
            register_artifacts(
                path.parent,
                owner_type="deployment-manifest",
                owner_id=artifact.manifest_checksum.removeprefix("sha256:"),
                source_version=artifact.source_ref,
                retention_class="configuration",
            )
            status, stdout, stderr = await self._runner(
                ["kubectl", "apply", "--namespace", namespace, "--filename", str(path)]
            )
            if status:
                raise RuntimeError(stderr or stdout or "Namespace labels could not be applied")
        return await super().deploy(artifact, execution_context)

    def _uses_delegate_lifecycle(self, artifact):
        return False
