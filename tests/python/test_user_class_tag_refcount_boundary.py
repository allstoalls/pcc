"""`py_incref`/`py_decref` must refcount a user class whose tag exceeds 500.

User-class tags are handed out from `PY_TYPE_USER_CLASS_START` (104) upward,
one per class in the compiled closure; C-extension tags start far above, at
`PY_TYPE_CEXT_TAG_BASE` (0x10000).  Both refcount paths screened their operand
with `tag > 500 and pcc_capi_is_cext_type_tag(tag) == 0`, which rejects a user
class from roughly the 440th onward: `py_incref` returned without incrementing
anything.

Nothing small reaches that boundary, so the defect was invisible to the
gateway benchmark, to the bottom-line gates and to every focused test.  pcc's
own closure crosses it, and pcc1 therefore stopped refcounting some of its own
instances: a `_PendingMember` stored into a call's argument tuple (which
retains through `py_incref`) was released by the caller, freed while the tuple
still pointed at it, and the callee's adapter then increffed a dead object --
reported as `TypeError: unsupported operand type(s) for +` from
`macho_archive`'s parallel member inspection, three frames away from anything
related.

These tests compile a program with more than 500 classes and check the
refcount survives a round trip through a call's argument tuple.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME = REPO_ROOT / "pcc" / "runtime"
PY_OBJ_PORT = (RUNTIME / "py" / "py_obj.py").read_text(encoding="utf-8")
HEADER = (RUNTIME / "include" / "py_runtime.h").read_text(encoding="utf-8")

CLASS_COUNT = 620


def test_no_mirror_still_spells_the_boundary_as_500() -> None:
    assert "PY_TYPE_CEXT_TAG_BASE = 0x10000" in HEADER
    for source, name in ((PY_OBJ_PORT, "py_obj.py"),):
        assert "> 500" not in source, name
    # Runtime-library imports are now statically resolved, so the named
    # generated boundary must protect all four strict refcount entry paths.
    assert PY_OBJ_PORT.count("tag >= (PY_TYPE_CEXT_TAG_BASE)") + PY_OBJ_PORT.count(
        "tag_dbg >= (PY_TYPE_CEXT_TAG_BASE)"
    ) >= 4



def _program() -> str:
    classes = "\n".join(
        f"class C{i}:\n"
        f"    def __init__(self, n: int) -> None:\n"
        f"        self.n = n\n"
        for i in range(CLASS_COUNT)
    )
    return (
        "from pcc.extern import extern, c_int64\n"
        'pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)\n\n\n'
        + classes
        + textwrap.dedent(
            f"""

            def take(item) -> int:
                # The adapter unpacks this argument from a tuple the caller
                # built, which is where a missing incref surfaces.
                return item.n

            def call_through_tuple(items, function) -> int:
                total = 0
                for index in range(len(items)):
                    total += function(items[index])
                return total

            def main() -> None:
                before = pcc_gc_telemetry(116)
                # The last classes defined get the highest tags; use them.
                items = [C{CLASS_COUNT - 1}(1), C{CLASS_COUNT - 2}(2), C{CLASS_COUNT - 3}(3)]
                total = 0
                for _ in range(200):
                    total += call_through_tuple(items, take)
                # Every element must still be alive and readable after all the
                # argument tuples that held it have been torn down.
                for item in items:
                    total += item.n
                print(total)
                print(pcc_gc_telemetry(116) - before)
                print(pcc_gc_telemetry(118))

            main()
            """
        )
    )


def test_a_high_tag_instance_survives_argument_tuple_round_trips(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "many_classes.py"
    exe = tmp_path / "many_classes.out"
    src.write_text(_program(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    env = dict(os.environ)
    env["PCC_GC_BACKEND"] = "0"
    env["PCC_GC_REFCOUNT_PROVENANCE_PROBE"] = "2"
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=180, env=env
    )
    assert result.returncode == 0, result.stderr
    total, unmanaged, double_frees = result.stdout.split()
    assert int(total) == 200 * (1 + 2 + 3) + (1 + 2 + 3)
    # A missing incref shows up as a refcount operation on a freed cell.
    assert unmanaged == "0", result.stderr
    assert double_frees == "0"
    assert "unmanaged pointer" not in result.stderr
