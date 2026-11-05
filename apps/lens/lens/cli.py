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


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_subparsers(dest="command", metavar="COMMAND")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not args.command:
        parser.print_help()
        return 2
    return 0
