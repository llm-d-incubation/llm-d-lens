"""``llmd sources``: register the checkouts and Git URLs stacks come from, and how they are laid out."""

from __future__ import annotations

import argparse

from ..sources import Layout
from . import Context, Handler


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("sources", help="register where stacks come from")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    add = actions.add_parser(
        "add",
        help="register a local checkout or a Git URL",
        description="Register a source. The layout options say which kustomize directories of the "
        "checkout are stacks; they are kept with the source and apply to every command that uses it.",
    )
    add.add_argument("name")
    add.add_argument("location", help="a local directory, or a Git URL (URL#REF pins a branch, tag, or ref)")
    add.add_argument("--ref", default="", help="branch, tag, commit, or ref of a Git URL")
    layout = add.add_argument_group("layout (kept with the source)")
    layout.add_argument(
        "--stacks",
        action="append",
        default=[],
        metavar="GLOB",
        help="a kustomize directory that is a stack, as a path glob (`*` within one segment, `**` across "
        "segments); repeatable. Without it, every directory no other one includes is a stack. Guides are "
        "always listed",
    )
    layout.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB", help="a kustomize directory that is not a stack"
    )
    layout.add_argument(
        "--companion",
        action="append",
        default=[],
        dest="companions",
        metavar="TEMPLATE",
        help="where a stack's companions are (what its namespace needs besides the stack, such as its "
        "network policies or a claim file), as a path relative to the source with {stack}, {name} (the "
        "stack's last segment) and {namespace}, `..`, and `*` within a segment; repeatable. Stood up "
        "before the stack with `standup --with-companions`, and kept by its teardown",
    )
    layout.add_argument(
        "--pool-label",
        default="",
        metavar="LABEL",
        help="the pod label that is 'true' while a group is in the router's pool, for stacks whose drain "
        "tool takes groups out by that label; `llmd rollouts` uses it unless given --pool-label",
    )
    actions.add_parser("list", help="list registered sources")
    remove = actions.add_parser("remove", help="forget a source (the checkout is left alone)")
    remove.add_argument("name")
    return {"sources": command_sources}


def command_sources(ctx: Context) -> int:
    args = ctx.args
    if args.action == "add":
        layout = Layout(
            stacks=tuple(args.stacks),
            exclude=tuple(args.exclude),
            companions=tuple(args.companions),
            pool_label=args.pool_label,
        )
        entry = ctx.registry.add(args.name, args.location, ref=args.ref, layout=layout)
        ref = f"#{entry['ref']}" if entry.get("ref") else ""
        print(f"Registered source {entry['name']} -> {entry['location']}{ref}")
        _print_layout(entry, "  ")
        return 0
    if args.action == "remove":
        ctx.registry.remove(args.name)
        print(f"Removed source {args.name}")
        return 0
    entries = ctx.registry.entries()
    if args.output != "text":
        ctx.emit(entries)
        return 0
    if not entries:
        print("No sources registered. Add one with `llmd sources add NAME PATH|URL`.")
    for entry in entries:
        ref = f"#{entry['ref']}" if entry.get("ref") else ""
        print(f"{entry['name']:<16} {entry['location']}{ref}")
        _print_layout(entry, " " * 17)
    return 0


def _print_layout(entry: dict, indent: str) -> None:
    for key, label in (("stacks", "stacks"), ("exclude", "exclude"), ("companions", "companion")):
        for value in entry.get(key) or []:
            print(f"{indent}{label:<10} {value}")
    if entry.get("poolLabel"):
        print(f"{indent}{'pool label':<10} {entry['poolLabel']}")
