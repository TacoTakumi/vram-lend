"""Console-script entry for vram-lend.

The lend verbs live in lend.py, which imports only the standard library so the
file also runs on its own. This module adds the one verb that needs a
dependency: ``skills``, AgentSquire's install/status/update/uninstall group.
"""

from __future__ import annotations

import sys

from . import lend


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "skills":
        from agentsquire.cli import skills_command_group

        group = skills_command_group("vram_lend", default_scope="user")
        return group.main(args=args[1:], prog_name="vram-lend skills",
                          standalone_mode=False) or 0
    return lend.main(args)


if __name__ == "__main__":
    sys.exit(main())
