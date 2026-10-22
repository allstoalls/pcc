"""A compiled program can import glob.

pcc/stdlib/glob.py imported its fnmatch sibling as ``pcc.stdlib.fnmatch``,
a name that exists only under CPython, so ``import glob`` in a native
program raised ImportError "No module named 'pcc.stdlib'". It now falls back
to the top-level ``fnmatch`` provider, as the other providers that import a
sibling do.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import glob
import os
import sys

root = sys.argv[1]
for name in ("a.py", "b.txt", ".hidden.py"):
    with open(os.path.join(root, name), "w") as handle:
        handle.write("x")
os.mkdir(os.path.join(root, "sub"))
with open(os.path.join(root, "sub", "c.py"), "w") as handle:
    handle.write("y")
prefix = len(root) + 1
print(sorted(path[prefix:] for path in glob.glob(os.path.join(root, "*.py"))))
print(sorted(path[prefix:] for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True)))
print(glob.escape("a*b?c[d"), glob.has_magic("a*"), glob.has_magic("plain"))
'''


@pytest.mark.integration
def test_glob_imports_and_matches_cpython_natively(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "glob_program.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected_root = tmp_path / "cpython"
    expected_root.mkdir()
    expected = subprocess.run(
        [sys.executable, str(source), str(expected_root)],
        capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "glob_program"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        root = tmp_path / ("native" + str(backend))
        root.mkdir()
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run(
            [str(binary), str(root)], capture_output=True, text=True, timeout=60,
            env=environment,
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"
