"""str methods on a module's str constants reached as attributes.

os.pathsep.join(...) raised "'str' object has no attribute 'join'" natively,
which stopped pcc1 -m pcc.package build-exec in toolchain._path_env. The
provider exported `pathsep: str = ";" if ... else ":"` as a dyn module
global (only literal values carried a type), and inference typed every
module attribute except classes as dyn, so the call went through a
generic getattr that knows three str methods. Annotated str globals now
export as str, and `alias.NAME` takes a provider's str global type the way
`from module import NAME` already did.
"""
from __future__ import annotations

import shutil
import subprocess


PROGRAM = r"""import os
from os import sep


def search_path(paths):
    return os.pathsep.join(str(path) for path in paths)


def joined(parts):
    return os.sep.join(parts) + "|" + sep.join(reversed(parts))


def describe():
    return os.curdir.join(["a", "b"]) + "|" + os.linesep.replace("\n", "NL") + "|" + os.name.upper()


def main():
    print(search_path(["/a", "/b"]), joined(["x", "y"]), describe())


main()
"""


def test_module_str_constant_methods_match_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "module_str_constants.py"
    exe = tmp_path / "module_str_constants.out"
    src.write_text(PROGRAM)
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert native.returncode == 0, native.stderr
    python3 = shutil.which("python3")
    assert python3 is not None, "python3 oracle not found on PATH"
    ref = subprocess.run([python3, str(src)], capture_output=True, text=True, timeout=30)
    assert ref.returncode == 0, ref.stderr
    assert native.stdout == ref.stdout
