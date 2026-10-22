"""Storing a borrowed alias back into its owning local keeps the reference.

pcc.* modules compile with the raw-int scaffold, where a Dyn operand of
``or`` is selected without a new reference. In
pcc.package.metadata.inspect_artifact, ``package_name = package_name or
"package"`` released the owned string and then stored the same pointer back
as borrowed, so the returned metadata's name read freed memory: natively it
printed "os", "darwin", the module's own name or an unknown object, and
``pcc1 -m pcc.package.inspect mlx`` reported ``"name": "os"``.
"""
from __future__ import annotations

import os
import subprocess
import sys

from pcc1_gate import repo_root


PROGRAM = """
import pcc.package.metadata as metadata


def main():
    names = []
    for name in ("mlx", "vllm", " tilelang ", ""):
        names.append(metadata.inspect_artifact(name).name)
    noise = [str(index) * 4 for index in range(512)]
    print(names, metadata.inspect_artifact("mlx", None).as_dict()["name"], len(noise))


main()
"""


def test_raw_scaffold_or_alias_store_keeps_owned_string(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "alias_store.py"
    exe = tmp_path / "alias_store.out"
    src.write_text(PROGRAM)
    # The program's imports resolve next to it, as for a script in a project.
    (tmp_path / "pcc").symlink_to(repo_root() / "pcc")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ref = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=str(repo_root())),
    )
    assert ref.returncode == 0, ref.stderr
    assert ref.stdout == "['mlx', 'vllm', 'tilelang', 'package'] mlx 512\n"
    for backend in range(5):
        native = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert native.returncode == 0, f"GC{backend}: {native.stderr}"
        assert native.stdout == ref.stdout, f"GC{backend}"


# ``cur = empty or keeper`` stores a borrowed parameter into an owned slot, so
# the slot may now hold a value it does not own. The later ``cur = fresh`` is a
# reference-transferring store that releases the previous slot value
# unconditionally unless the slot is known to be a borrowed root; without that
# it released ``keeper``, which the caller's list still held. Natively compiled
# pcc1 freed AST nodes this way and its export worker for ``os`` crashed.
REBIND_PROGRAM = """
import pcc.unsafe


class Box:
    def __init__(self, name):
        self.name = name


def make(name):
    return Box(name)


def rebind(keeper, empty):
    cur = make("first")
    cur = empty or keeper
    fresh = make("second")
    cur = fresh
    return cur.name


def main():
    holder = [Box("kept-" + str(i)) for i in range(3)]
    names = []
    for i in range(200):
        names.append(rebind(holder[i % 3], ""))
    noise = [Box(str(i) * 3) for i in range(256)]
    print(names[0], names[-1], [b.name for b in holder], len(noise))


main()
"""


def test_raw_scaffold_borrowed_alias_store_is_not_released_by_owned_rebind(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "alias_rebind.py"
    exe = tmp_path / "alias_rebind.out"
    src.write_text(REBIND_PROGRAM)
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ref = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=str(repo_root())),
    )
    assert ref.returncode == 0, ref.stderr
    assert ref.stdout == "second second ['kept-0', 'kept-1', 'kept-2'] 256\n"
    for backend in range(5):
        native = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert native.returncode == 0, f"GC{backend}: {native.stderr}"
        assert native.stdout == ref.stdout, f"GC{backend}"
