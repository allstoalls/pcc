"""Module global reads reuse cached name keys without changing their meaning.

py_module_attr_get built a fresh str for the module name and another for the
attribute name on every module global read. Both now come from a cache of
pinned keys indexed by the C string's address and verified by its bytes. The
program below reads more distinct globals than the cache has slots, so
entries collide and are replaced; it rebinds a global, deletes one and
expects NameError, and reads and rebinds names of a second module. Native
output under every collector must match CPython. (Writes through
``globals()`` are not yet seen by compiled reads; docs/status.md tracks it.)
"""
from __future__ import annotations

import os
import subprocess
import sys

COUNT = 600


def _program() -> str:
    lines = [f"G{index} = {index * 3 + 1}" for index in range(COUNT)]
    lines.append("")
    lines.append("import helper")
    lines.append("")
    lines.append("def total():")
    lines.append("    acc = 0")
    lines.extend(f"    acc += G{index}" for index in range(COUNT))
    lines.append("    return acc")
    lines.append("")
    lines.append("def bump():")
    lines.append("    global G7")
    lines.append("    G7 = G7 + 1000")
    lines.append("")
    lines.append("def erase():")
    lines.append("    global G11")
    lines.append("    del G11")
    lines.append("")
    lines.append("def missing():")
    lines.append("    try:")
    lines.append("        return G11")
    lines.append("    except NameError:")
    lines.append("        return 'gone'")
    lines.append("")
    lines.append("def main():")
    lines.append("    first = 0")
    lines.append("    round_ = 0")
    lines.append("    while round_ < 50:")
    lines.append("        first += total()")
    lines.append("        round_ += 1")
    lines.append("    print(first, total())")
    lines.append("    bump()")
    lines.append("    print(total(), G7)")
    lines.append("    erase()")
    lines.append("    print(missing())")
    lines.append("    print(helper.scale(3), helper.NAME, helper.LIMIT)")
    lines.append("    helper.LIMIT = 77")
    lines.append("    print(helper.scale(3), helper.LIMIT)")
    lines.append("")
    lines.append("main()")
    return "\n".join(lines) + "\n"


_HELPER = """
NAME = 'helper'
LIMIT = 10


def scale(value):
    return value * LIMIT
"""


def test_module_globals_through_name_key_cache_match_cpython(tmp_path):
    source = tmp_path / "keys.py"
    source.write_text(_program(), encoding="utf-8")
    (tmp_path / "helper.py").write_text(_HELPER, encoding="utf-8")
    exe = tmp_path / "keys_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(source), "-o", str(exe)],
        text=True, capture_output=True, timeout=600, env=env,
    )
    assert built.returncode == 0, built.stderr
    expected = subprocess.run([sys.executable, str(source)], text=True, capture_output=True,
                              timeout=60, env=env, cwd=tmp_path)
    assert expected.returncode == 0, expected.stderr
    for backend in range(5):
        native = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60,
                                env=dict(env, PCC_GC_BACKEND=str(backend)))
        assert native.returncode == 0, (backend, native.stderr)
        assert native.stdout == expected.stdout, backend
