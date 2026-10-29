"""vram-lend: lend yourself GPU memory from a serving inference engine.

This file imports only the standard library, so it runs on its own:
``python3 lend.py state``. The package installs it as ``vram-lend``.

The verbs mirror the VRAM Lend HTTP API (docs/api.md): ``state``, ``lend``,
``return``, and ``run``, which lends, runs a command and returns the lease.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_SERVER = "http://127.0.0.1:8080"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vram-lend",
        description="Lend yourself GPU memory from a serving inference engine, "
                    "directly or through llama-swap, and give it back.",
    )
    parser.add_argument(
        "--server", action="append", metavar="URL",
        help="an engine or llama-swap base URL; repeatable. Default: "
             "VRAM_LEND_SERVER (comma separated) or " + DEFAULT_SERVER,
    )
    sub = parser.add_subparsers(dest="verb", metavar="verb")

    sub.add_parser("state", help="list every lend endpoint found and what each GPU can lend")

    p_lend = sub.add_parser("lend", help="lend memory on one GPU and print the lease; return it later")
    p_lend.add_argument("--gpu", required=True, metavar="PCI", help="PCI address of the GPU")
    p_lend.add_argument("--mib", required=True, type=int, metavar="N", help="MiB to lend")

    p_return = sub.add_parser("return", help="return a lease by id")
    p_return.add_argument("--id", required=True, metavar="ID", help="lease id from lend")

    p_run = sub.add_parser("run", help="lend, run a command, return the lease when it exits")
    p_run.add_argument("--gpu", required=True, metavar="PCI", help="PCI address of the GPU")
    p_run.add_argument("--mib", required=True, type=int, metavar="N", help="MiB to lend")
    p_run.add_argument("command", nargs=argparse.REMAINDER, metavar="-- command",
                       help="the command to run")

    sub.add_parser("skills", help="install the vram-lend skill into agent harnesses "
                                  "(needs the pip package)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verb is None:
        parser.print_help()
        return 2
    if args.verb == "skills":
        print("vram-lend skills needs the pip package: pipx install vram-lend",
              file=sys.stderr)
        return 2
    print(f"{args.verb}: not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
