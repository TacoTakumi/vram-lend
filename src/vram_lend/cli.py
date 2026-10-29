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
    _notice_stale_skill()
    if args and args[0] == "skills":
        from agentsquire.cli import skills_command_group

        group = skills_command_group("vram_lend", default_scope="user")
        return group.main(args=args[1:], prog_name="vram-lend skills",
                          standalone_mode=False) or 0
    return lend.main(args)


def _notice_stale_skill() -> None:
    """One advisory line on stderr when an installed copy of the skill is
    older than the shipped one. Never prompts, never updates, never changes
    the exit code; CI or AGENTSQUIRE_NO_UPDATE_CHECK silence it."""
    try:
        from agentsquire import BundledPackageDataSource, check_stale

        check_stale(BundledPackageDataSource("vram_lend"), prog_name="vram-lend",
                    update_command="vram-lend skills update")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
