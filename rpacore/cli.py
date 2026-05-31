"""Command line entry point for RPA Core."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from rpacore import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the RPA Core command line parser."""
    parser = argparse.ArgumentParser(prog="rpacore")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Print the installed RPA Core version.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RPA Core command line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2
