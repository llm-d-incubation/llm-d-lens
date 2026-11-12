"""``llmd label``: label a workload's running pods without restarting them, and keep the labels.

A router that takes over from another can serve pods that already run once they carry the labels it
selects on, such as an aggregated Deployment's pods joining the pool of a router that also serves a
DisaggregatedSet. Adding the labels to the pod template would replace every pod, and lose what each
one has cached, so ``llmd label`` patches the running pods instead and keeps the labels in the
instance's record (``podLabels``). Pods the controller creates later start without them:
``llmd scale --wait`` labels the new pods of the workloads it raises, ``llmd label`` run again labels
any pod that lacks them, and ``llmd status`` names such pods.

Refused: keys the workload's controller selects pods by (a pod that no longer matches is orphaned,
and the controller starts a replacement), the labels lws keeps on its pods, and the instance's pool
label, which a drain sets.
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import health, inventory, overrides
from ..errors import CommandError, NotFoundError, PolicyError, UsageError
from ..kube import Kube
from . import Context, Handler, add_target_arguments, rollouts

#: Labels the workload controllers set on the pods they make.
CONTROLLER_KEYS = ("pod-template-hash", "controller-revision-hash", "statefulset.kubernetes.io/pod-name")
#: Label prefixes lws keeps on the pods of a LeaderWorkerSet or DisaggregatedSet.
LWS_PREFIXES = ("leaderworkerset.sigs.k8s.io/", "disaggregatedset.x-k8s.io/")


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser(
        "label",
        help="label a workload's running pods without restarting them; the labels are kept",
        description="Set KEY=VALUE (or remove KEY-) on every running pod of WORKLOAD (a Deployment, "
        "StatefulSet or LeaderWorkerSet name, or SET/ROLE for a DisaggregatedSet role), and keep the labels "
        "in the instance. Pods created later start without them; `llmd scale --wait` labels a raised "
        "workload's new pods.",
    )
    parser.add_argument("instance")
    parser.add_argument("workload", metavar="WORKLOAD")
    parser.add_argument("labels", nargs="+", metavar="KEY=VALUE|KEY-")
    add_target_arguments(parser)
    parser.add_argument("--pool-label", default=None, metavar="LABEL", help=rollouts.POOL_HELP)
    parser.add_argument("--dry-run", action="store_true", help="show which pods would change; change nothing")
    return {"label": command_label}


def parse(items: list[str]) -> dict[str, str | None]:
    """``KEY=VALUE`` sets a label, ``KEY-`` removes it (None)."""
    changes: dict[str, str | None] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if separator and key:
            changes[key] = value
        elif not separator and item.endswith("-") and len(item) > 1:
            changes[item[:-1]] = None
        else:
            raise UsageError(f"expected KEY=VALUE or KEY-, got {item!r}")
    return changes


def workload_object(kube: Kube, namespace: str, record: dict[str, Any], workload: str) -> dict[str, Any]:
    """The live object of one of the instance's workloads (``NAME`` or ``SET/ROLE``)."""
    name, _, role = workload.partition("/")
    for item in record.get("objects") or []:
        kinds = ("DisaggregatedSet",) if role else overrides.SCALABLE
        if item["name"] != name or item["kind"] not in kinds:
            continue
        live = kube.get(item["resource"], name, namespace)
        if live is None:
            raise NotFoundError(f"{item['kind']}/{name} of instance {record['instance']} is not in {namespace}")
        roles = [str(entry.get("name")) for entry in (live.get("spec") or {}).get("roles") or []]
        if role and role not in roles:
            raise UsageError(f"DisaggregatedSet/{name} has no role {role!r}", hint=f"Its roles: {', '.join(roles)}.")
        return live
    lives = [
        kube.get(item["resource"], item["name"], namespace)
        for item in record.get("objects") or []
        if item["kind"] in (*overrides.SCALABLE, "DisaggregatedSet")
    ]
    found = overrides.workloads([live for live in lives if live])
    raise UsageError(
        f"instance {record['instance']} has no workload {workload!r}",
        hint="Its workloads: " + (", ".join(found) or "none"),
    )


def workload_pods(kube: Kube, namespace: str, live: dict[str, Any], workload: str) -> list[dict[str, Any]]:
    """The workload's pods that are not terminating (a role's only, for ``SET/ROLE``)."""
    selector = health.pod_selector(live)
    pods = kube.get_json("pods", namespace, selector=selector) if selector else []
    role = workload.partition("/")[2]
    return [
        pod
        for pod in pods
        if not (pod.get("metadata") or {}).get("deletionTimestamp")
        and (not role or health.labels(pod).get(health.DS_ROLE_LABEL) == role)
    ]


def protected(live: dict[str, Any], key: str) -> bool:
    """Whether changing ``key`` on the workload's pods would take them away from their controller."""
    selector = (live.get("spec") or {}).get("selector") or {}
    selected = set(selector.get("matchLabels") or {}) | {
        item.get("key") for item in selector.get("matchExpressions") or []
    }
    if key in selected or key in CONTROLLER_KEYS:
        return True
    return live.get("kind") in ("LeaderWorkerSet", "DisaggregatedSet") and key.startswith(LWS_PREFIXES)


def lacking(pod: dict[str, Any], labels: dict[str, str]) -> bool:
    current = health.labels(pod)
    return any(current.get(key) != value for key, value in labels.items())


def label_pods(
    kube: Kube,
    namespace: str,
    instance: str,
    workload: str,
    changes: dict[str, str | None],
    *,
    pool_label: str = "",
    dry_run: bool = False,
    actor: str = "",
) -> dict[str, Any]:
    record = inventory.require(kube, namespace, instance)
    live = workload_object(kube, namespace, record, workload)
    refused = sorted(key for key in changes if protected(live, key))
    if refused:
        raise PolicyError(
            f"{live['kind']}/{live['metadata']['name']} owns its pods by {', '.join(refused)}",
            hint="A pod that no longer matches its controller is replaced; change those labels in the stack.",
        )
    if pool_label and pool_label in changes:
        raise PolicyError(
            f"{pool_label} is the instance's pool label: a drain sets it",
            hint="Use `llmd scale --drain-idle` to take pods out of the pool before they go.",
        )
    kept = dict((record.get("podLabels") or {}).get(workload) or {})
    for key, value in changes.items():
        if value is None:
            kept.pop(key, None)
        else:
            kept[key] = value
    pods = workload_pods(kube, namespace, live, workload)
    patched = []
    for pod in pods:
        current = health.labels(pod)
        if all(
            (current.get(key) == value) if value is not None else key not in current for key, value in changes.items()
        ):
            continue
        patched.append(pod["metadata"]["name"])
        if not dry_run:
            kube.patch_metadata("pods", pod["metadata"]["name"], namespace, labels=changes)
    if not dry_run:
        everything = {**(record.get("podLabels") or {}), workload: kept}
        updated = {
            **record,
            "updatedBy": actor,
            "podLabels": {key: value for key, value in everything.items() if value},
        }
        if not updated["podLabels"]:
            updated.pop("podLabels")
        inventory.write(kube, updated)
    return {
        "instance": instance,
        "namespace": namespace,
        "workload": workload,
        "labels": kept,
        "pods": [pod["metadata"]["name"] for pod in pods],
        "patched": patched,
        "dryRun": dry_run,
    }


def missing(kube: Kube, namespace: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """For each workload with kept labels, its running pods that lack them."""
    found = []
    for workload, labels in sorted((record.get("podLabels") or {}).items()):
        try:
            live = workload_object(kube, namespace, record, workload)
            running = workload_pods(kube, namespace, live, workload)
        except (NotFoundError, UsageError, CommandError):
            continue  # status reports a workload it cannot read on its own
        pods = [pod["metadata"]["name"] for pod in running if lacking(pod, labels)]
        if pods:
            found.append({"workload": workload, "labels": labels, "pods": pods})
    return found


def relabel(kube: Kube, namespace: str, record: dict[str, Any], workloads: list[str]) -> list[str]:
    """Give the running pods of ``workloads`` the labels the instance keeps for them; the pods changed."""
    changed = []
    for item in missing(kube, namespace, record):
        if item["workload"] not in workloads:
            continue
        for pod in item["pods"]:
            kube.patch_metadata("pods", pod, namespace, labels=item["labels"])
            changed.append(pod)
    return changed


def command_label(ctx: Context) -> int:
    args = ctx.args
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    record = inventory.require(kube, namespace, args.instance)
    result = label_pods(
        kube,
        namespace,
        args.instance,
        args.workload,
        parse(args.labels),
        pool_label=rollouts.pool_label(ctx, record),
        dry_run=args.dry_run,
        actor=kube.whoami(),
    )
    if args.output != "text":
        ctx.emit(result)
        return 0
    verb = "Would label" if args.dry_run else "Labelled"
    kept = ", ".join(f"{key}={value}" for key, value in sorted(result["labels"].items())) or "none"
    print(f"{verb} {len(result['patched'])} of {len(result['pods'])} pod(s) of {args.workload} in {namespace}")
    print(f"  kept for {args.workload}: {kept}")
    print(
        "  pods created later start without them: `llmd scale --wait` labels a raised workload's new pods, "
        "and `llmd status` names pods that lack them"
    )
    return 0
