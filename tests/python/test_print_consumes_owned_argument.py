"""``print`` must consume an argument that produced a fresh reference.

``py_print`` borrows: ``pcc/runtime/src/py_print_fmt.c`` defines it as
``py_format(stdout, o); fputc('\\n', stdout);`` with no refcount traffic.  The
generic print tail never released its argument, so ``print(a[0])`` leaked one
reference per call -- the subscript's root is balanced, but the new reference
``py_list_getitem`` returned was dropped on the floor.

The release must stay conditional.  ``x = a[0]; print(x)`` already owns the
value in a rooted local and releases it there; adding a second release would be
a double free.  That contrast is the control below.
"""

from __future__ import annotations

import os
import re
import subprocess

PROGRAM = """
class Boxed:
    def __init__(self, tag):
        self.tag = tag

    def __repr__(self):
        return 'B' + self.tag


def direct(a):
    print(a[0])


def bound(a):
    x = a[0]
    print(x)


keep = [Boxed('d'), Boxed('b')]
direct(keep)
bound(keep)
direct(keep)
print('end')
"""

# Runtime smoke only: a premature free or a double free from the new release
# shows up here as wrong output or a non-zero exit.  Reference *counts* are not
# observable from Python, so the precise assertion is the IR contrast below.
EXPECTED = ["Bd", "Bd", "Bd", "end"]

IR_PROGRAM = """
def direct(a: list) -> None:
    print(a[0])


def bound(a: list) -> None:
    x = a[0]
    print(x)
"""


def _run(cmd, **kw):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return subprocess.run(cmd, text=True, capture_output=True, env=env, **kw)


def test_direct_subscript_print_still_prints_correctly(tmp_path):
    src = tmp_path / "prog.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    build = _run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(src), "-o", str(exe),
        ],
        timeout=600,
    )
    assert build.returncode == 0, build.stderr
    run = _run([str(exe)], timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip().splitlines() == EXPECTED


def test_release_is_added_for_direct_and_not_duplicated_for_bound(tmp_path):
    src = tmp_path / "ir.py"
    src.write_text(IR_PROGRAM, encoding="utf-8")
    out = tmp_path / "ir.ll"
    build = _run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", "--python-library", f"--emit-llvm={out}",
            str(src),
        ],
        timeout=600,
    )
    assert build.returncode == 0, build.stderr
    ir_text = out.read_text(encoding="utf-8")

    def body(suffix: str) -> str:
        m = re.search(
            r"^define [^\n]*@user_\w*_" + suffix + r"\((?:.|\n)*?^\}",
            ir_text,
            re.MULTILINE,
        )
        assert m is not None, f"no definition for {suffix}"
        return m.group(0)

    direct = body("direct")
    printed = re.search(r"@py_print\(ptr (%[\w.]+)\)", direct)
    assert printed is not None, direct
    name = re.escape(printed.group(1))
    assert re.search(r"@pcc_gc_release\(ptr " + name + r"\)", direct), (
        "direct subscript print leaked its owned argument:\n" + direct
    )
    assert direct.count("@pcc_gc_release(") == 1, direct

    # Control: the bound form already owns the value in its rooted local and
    # releases it by clearing that slot (behind its owned flag) on the normal
    # and the error exit; it must not gain a release of the printed value.
    bound = body("bound")
    assert "@pcc_gc_release(" not in bound, bound
    assert len(re.findall(r"@pcc_gc_store_root\(ptr %x\.addr\.\d+, ptr null\)", bound)) == 2, bound
