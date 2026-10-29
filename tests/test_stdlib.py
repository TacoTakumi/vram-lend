"""The lend module must import only the standard library and run on its own."""

import ast
import subprocess
import sys
from pathlib import Path

LEND = Path(__file__).resolve().parents[1] / "src" / "vram_lend" / "lend.py"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
            else:
                names.add("<relative>")
    return names


def test_lend_imports_only_the_standard_library():
    names = imported_modules(LEND)
    assert "<relative>" not in names
    outside = names - sys.stdlib_module_names - {"__future__"}
    assert not outside, outside


def test_lend_runs_as_a_file():
    out = subprocess.run([sys.executable, str(LEND), "--help"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    for verb in ("state", "lend", "return", "run", "skills"):
        assert verb in out.stdout
