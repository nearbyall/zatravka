"""Command line: ideaminer <command>; see ideaminer <command> -h."""
from __future__ import annotations

import sys

from .common import setup_logging
from .parser import build_parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)


__all__ = ["main", "build_parser"]
