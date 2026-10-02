"""Structured XPU P/D disaggregation deployment adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.deploy.providers.deployment_bundle import (
    install_deployment_bundle,
    subprocess_bundle_runner,
)
from llm_d_bench.deploy.providers.gpu_selection import gpu_device_selectors
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact, ValidationResult
from llm_d_bench.deploy.providers.hardware_profile import device_class
from llm_d_bench.deploy.providers.model_cache_environment import model_cache_environment
from llm_d_bench.utils.paths import lens_temp_root


class PdDisaggregationAdapter:
    published_manifest_lifecycle = True

    def __init__(
        self,
        command_runner,
        guide_root: Path,
        namespace_prefix: str,
        timeout: int,
        helm_path: Path,
        kubeconfig: str | None,
    ) -> None:
        self._runner = command_runner
        self._sources = {
            "vllm": guide_root / "guides/pd-disaggregation/modelserver/xpu/vllm",
            "vllm-rdma": guide_root / "guides/pd-disaggregation/modelserver/xpu/vllm-rdma",
        }
        self._root = lens_temp_root("lens-pd-overlays")
        self._namespace_prefix = namespace_prefix
        self._timeout = timeout
        self._helm_path = helm_path
        self._environment = {**os.environ, "KUBECONFIG": kubeconfig} if kubeconfig else None
        self._guide_root = guide_root
        self._bundle_command_runner = subprocess_bundle_runner(
            helm_path,
            Path(getattr(command_runner, "_kubectl_path", "kubectl")),
            self._environment,
        )
        self._definition = GuideDefinition(
            "pd-disaggregation",
            "local-pd-disaggregation",
            stable_hash({"sources": {name: str(path) for name, path in self._sources.items()}}),
            "supported-core",
            {"variants": ["vllm", "vllm-rdma"], "default_variant": "vllm", "structured_custom_parameters": True},
        )

    def discover(self):
        return self._definition

    def register_restored_namespace(self, namespace: str) -> None:
        register = getattr(self._runner, "register_restored_namespace", None)
        if callable(register):
            register(namespace)

    def validate_inputs(self, definition, cluster_snapshot, overrides):
        try:
            parameters = self._parameters(overrides)
            if not self._sources[parameters["variant"]].is_dir():
                raise ValueError("pd-disaggregation XPU variant source is unavailable")
        except ValueError as error:
            return ValidationResult(False, [str(error)])
        return ValidationResult(True)

    async def render(self, definition, overrides):
        parameters = self._parameters(overrides)
        digest = stable_hash(parameters)
        directory = self._root / digest
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            "kustomize",
            str(self._sources[parameters["variant"]]),
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise ValueError(f"Kustomize render failed: {stderr.decode(errors='replace').strip()}")
        documents = [item for item in yaml.safe_load_all(stdout) if isinstance(item, dict)]
        for role in ("prefill", "decode"):
            deployment = next(
                item
                for item in documents
                if item.get("kind") == "Deployment" and item["metadata"]["name"].endswith(role)
            )
            deployment["spec"]["replicas"] = parameters[role]["replicas"]
            container = next(
                item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "modelserver"
            )
            args = list(container.get("args") or [])
            args[0] = "/model-cache" if parameters["mount_path"] else parameters["model"]
            args = self._set_argument(args, "tensor-parallel-size", str(parameters[role]["tensor_parallel_size"]))
            environment = list(container.get("env") or [])
            for name, value in sorted(parameters["environment"].items()):
                if value:
                    environment = self._set_environment(environment, name, value)
            for custom in parameters["custom_parameters"]:
                if custom["target"] not in {role, "both"}:
                    continue
                if custom["kind"] == "argument":
                    args = self._set_argument(args, custom["name"], custom["value"])
                else:
                    environment = self._set_environment(environment, custom["name"], custom["value"])
            for name, value in model_cache_environment(
                "shared-path", cache_mounted=bool(parameters["mount_path"])
            ).items():
                environment = self._set_environment(environment, name, value)
            container["args"] = args
            container["env"] = environment
            if parameters["mount_path"]:
                pod_spec = deployment["spec"]["template"]["spec"]
                pod_spec.setdefault("volumes", []).append(
                    {"name": "model-cache", "hostPath": {"path": parameters["mount_path"], "type": "DirectoryOrCreate"}}
                )
                container.setdefault("volumeMounts", []).append(
                    {"name": "model-cache", "mountPath": "/model-cache", "readOnly": True}
                )
        claim_documents = [item for item in documents if item.get("kind") == "ResourceClaimTemplate"]
        selectors = gpu_device_selectors()
        for claim in claim_documents:
            role = "prefill" if "prefill" in claim["metadata"]["name"] else "decode"
            requests = claim["spec"]["spec"]["devices"]["requests"]
            gpu_request = next(
                item for item in requests if item.get("exactly", {}).get("deviceClassName") == device_class()
            )
            gpu_request["exactly"]["count"] = parameters[role]["tensor_parallel_size"]
            if selectors:
                gpu_request["exactly"]["selectors"] = selectors
        for deployment in [item for item in documents if item.get("kind") == "Deployment"]:
            next(
                item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "modelserver"
            )["image"] = parameters["image"]
        manifest = directory / "manifest.yaml"
        manifest.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
        return GuideDeploymentArtifact(
            guide_id=self._definition.guide_id,
            artifact_hash=digest,
            manifest_ref=str(manifest),
            source_ref=self._definition.source_ref,
            guide_content_hash=self._definition.content_hash,
            manifest_checksum=stable_hash({"parameters": parameters}),
        )

    async def deploy(self, artifact, context):
        namespace = context["namespace"]
        status, stdout, stderr = await self._runner(["kubectl", "create", "namespace", namespace])
        if status != 0 and "AlreadyExists" not in stderr:
            raise RuntimeError((stderr or stdout).strip())
        bundle = artifact.deployment_contract.get("deploymentBundle")
        reproducibility = {}
        if bundle is not None:
            reproducibility = await install_deployment_bundle(
                bundle,
                guide=artifact.guide_id,
                source_commit=str(artifact.source_ref or ""),
                namespace=namespace,
                command_runner=self._bundle_command_runner,
                output_root=Path(artifact.manifest_ref or "").parent / "deployment-bundle",
            )
        else:
            base_values = self._guide_root / "guides/recipes/router/base.values.yaml"
            pd_values = self._guide_root / "guides/pd-disaggregation/router/pd-disaggregation.values.yaml"
            helm = await asyncio.create_subprocess_exec(
                str(self._helm_path),
                "upgrade",
                "--install",
                "pd-disaggregation",
                "oci://ghcr.io/llm-d/charts/llm-d-router-standalone",
                "--namespace",
                namespace,
                "--version",
                "v0.9.0",
                "--values",
                str(base_values),
                "--values",
                str(pd_values),
                env=self._environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            helm_stdout, helm_stderr = await helm.communicate()
            if helm.returncode:
                raise RuntimeError((helm_stderr or helm_stdout).decode(errors="replace").strip())
        status, stdout, stderr = await self._runner(
            ["kubectl", "apply", "--namespace", namespace, "--filename", artifact.manifest_ref]
        )
        if status != 0:
            raise RuntimeError((stderr or stdout).strip())
        return {
            "namespace": namespace,
            "artifact_hash": artifact.artifact_hash,
            "apply_output": stdout,
            **reproducibility,
        }

    async def readiness(self, execution):
        namespace = execution["namespace"]
        for name in ("pd-disaggregation-xpu-vllm-prefill", "pd-disaggregation-xpu-vllm-decode"):
            status, stdout, stderr = await self._runner(
                [
                    "kubectl",
                    "rollout",
                    "status",
                    f"deployment/{name}",
                    "--namespace",
                    namespace,
                    f"--timeout={self._timeout}s",
                ]
            )
            if status != 0:
                return ValidationResult(False, [(stderr or stdout).strip()])
        status, stdout, stderr = await self._runner(
            ["kubectl", "get", "service", "--namespace", namespace, "--output", "json"]
        )
        if status != 0:
            return ValidationResult(False, [(stderr or stdout).strip()])
        services = json.loads(stdout).get("items", [])
        service = next(
            (
                item
                for item in services
                if any(token in item["metadata"]["name"] for token in ("epp", "gateway", "inference", "router"))
            ),
            None,
        )
        if service is None:
            return ValidationResult(False, ["pd-disaggregation routing service was not discovered"])
        port = next(
            (
                item["port"]
                for item in service["spec"].get("ports", [])
                if item.get("name") in {"http", "default", "web"} or item.get("port") == 80
            ),
            None,
        )
        if port is None:
            return ValidationResult(False, ["pd-disaggregation routing service has no HTTP port"])
        execution["endpoint_url"] = f"http://{service['metadata']['name']}.{namespace}.svc:{port}"
        return ValidationResult(True)

    async def diagnostics(self, execution):
        namespace = execution["namespace"]

        async def capture(command):
            status, stdout, stderr = await self._runner(command)
            return (stdout if status == 0 else stderr).strip()

        return {
            "namespace": namespace,
            "pods": await capture(["kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"]),
            "events": await capture(["kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"]),
            "decode_modelserver_logs": await capture(
                [
                    "kubectl",
                    "logs",
                    "--namespace",
                    namespace,
                    "-l",
                    "llm-d.ai/role=decode",
                    "-c",
                    "modelserver",
                    "--tail=120",
                    "--prefix=true",
                ]
            ),
            "prefill_modelserver_logs": await capture(
                [
                    "kubectl",
                    "logs",
                    "--namespace",
                    namespace,
                    "-l",
                    "llm-d.ai/role=prefill",
                    "-c",
                    "modelserver",
                    "--tail=120",
                    "--prefix=true",
                ]
            ),
            "routing_proxy_logs": await capture(
                [
                    "kubectl",
                    "logs",
                    "--namespace",
                    namespace,
                    "-l",
                    "llm-d.ai/role=decode",
                    "-c",
                    "routing-proxy",
                    "--tail=120",
                    "--prefix=true",
                ]
            ),
            "routing_proxy_previous_logs": await capture(
                [
                    "kubectl",
                    "logs",
                    "--namespace",
                    namespace,
                    "-l",
                    "llm-d.ai/role=decode",
                    "-c",
                    "routing-proxy",
                    "--previous",
                    "--tail=120",
                    "--prefix=true",
                ]
            ),
            **{
                key: value
                for key, value in execution.items()
                if key.startswith("router_effective_") or key.startswith("router_rendered_")
            },
            "captured_at": datetime.now(UTC).isoformat(),
        }

    async def stop(self, execution, artifact):
        outputs = []
        for name in ("pd-disaggregation-xpu-vllm-prefill", "pd-disaggregation-xpu-vllm-decode"):
            status, stdout, stderr = await self._runner(
                ["kubectl", "scale", f"deployment/{name}", "--namespace", execution["namespace"], "--replicas=0"]
            )
            outputs.append(stderr or stdout)
        return {"stopped": True, "output": "\n".join(outputs), "error": None}

    async def rollback(self, execution, artifact):
        return await self.stop(execution, artifact)

    async def cleanup(self, execution, artifact, *, force=False):
        helm = await asyncio.create_subprocess_exec(
            str(self._helm_path),
            "uninstall",
            "pd-disaggregation",
            "--namespace",
            execution["namespace"],
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await helm.communicate()
        status, stdout, stderr = await self._runner(
            ["kubectl", "delete", "namespace", execution["namespace"], "--ignore-not-found=true"]
        )
        if artifact.manifest_ref:
            shutil.rmtree(Path(artifact.manifest_ref).parent, ignore_errors=True)
        return {"cleaned_up": status == 0, "output": stdout, "error": stderr or None}

    @staticmethod
    def _set_argument(args, name, value):
        if name in {"enable-auto-tool-choice", "enable-prefix-caching", "enforce-eager"} and str(value).lower() in {
            "true",
            "false",
        }:
            positive, negative = f"--{name}", f"--no-{name}"
            replacement = positive if str(value).lower() == "true" else negative
            return [item for item in args if item not in {positive, negative, f"--{name}=true", f"--{name}=false"}] + [
                replacement
            ]
        prefix = f"--{name}="
        return (
            [f"{prefix}{value}" if item.startswith(prefix) else item for item in args]
            if any(item.startswith(prefix) for item in args)
            else [*args, f"{prefix}{value}"]
        )

    @staticmethod
    def _set_environment(items, name, value):
        return (
            [{"name": name, "value": value} if item.get("name") == name else item for item in items]
            if any(item.get("name") == name for item in items)
            else [*items, {"name": name, "value": value}]
        )

    @staticmethod
    def _parameters(overrides):
        model, runtime = overrides.get("model") or {}, overrides.get("runtime") or {}
        if not model.get("name") or not runtime.get("image") or ":" not in runtime["image"]:
            raise ValueError("pd-disaggregation requires model.name and a tagged runtime.image")
        variant = str(overrides.get("guideVariant") or "vllm")
        if variant not in {"vllm", "vllm-rdma"}:
            raise ValueError(f"unsupported pd-disaggregation variant: {variant}")
        result: dict[str, Any] = {
            "variant": variant,
            "model": model["name"],
            "image": runtime["image"],
            "mount_path": runtime.get("mountPath") or "",
            "environment": runtime.get("environment") if isinstance(runtime.get("environment"), dict) else {},
            "custom_parameters": overrides.get("customParameters") or [],
            "official_guide": overrides.get("officialGuide")
            if isinstance(overrides.get("officialGuide"), dict)
            else None,
        }
        if not isinstance(result["mount_path"], str) or (
            result["mount_path"] and not result["mount_path"].startswith("/")
        ):
            raise ValueError("runtime.mountPath must be an absolute host path")
        for role in ("prefill", "decode"):
            values = overrides.get(role) or {}
            if not isinstance(values.get("replicaCount"), int) or not isinstance(values.get("tensorParallelSize"), int):
                raise ValueError(f"pd-disaggregation requires {role} replicaCount and tensorParallelSize")
            result[role] = {"replicas": values["replicaCount"], "tensor_parallel_size": values["tensorParallelSize"]}
        return result
