"""llmd: plan, stand up, roll out, and remove llm-d guides inside one namespace.

A stack is an llm-d guide (``guides/<guide>``, deployed the way its README's commands deploy
it), one kustomize directory, or manifest files of an llm-d checkout. `llmd plan` shows what
`llmd standup` would change, without changing anything.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .commands import Context, Handler
from .commands import sources as sources_command
from .errors import LlmdError

#: Modules that add subcommands, in the order ``--help`` lists them.
COMMAND_MODULES = [
    sources_command,
]


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, Handler]]:
    parser = argparse.ArgumentParser(
        prog="llmd",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"llmd {__version__}")
    parser.add_argument("-o", "--output", choices=["text", "json", "yaml"], default="text")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("LLMD_STATE_DIR", ""),
        help="local state directory: registered sources (env LLMD_STATE_DIR)",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    handlers: dict[str, Handler] = {}
    for module in COMMAND_MODULES:
        handlers.update(module.register(commands))
    return parser, handlers


def main(argv: list[str] | None = None) -> int:
    parser, handlers = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not args.command:
        parser.print_help()
        return 2
    try:
        return handlers[args.command](Context(args))
    except LlmdError as error:
        print(f"error: {error.message}", file=sys.stderr)
        if error.hint:
            print(f"hint: {error.hint}", file=sys.stderr)
        if isinstance(error.details, list):
            for item in error.details[:20]:
                if isinstance(item, dict) and item.get("message"):
                    print(f"  - {item['message']}", file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
