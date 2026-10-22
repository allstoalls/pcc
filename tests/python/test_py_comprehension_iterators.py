from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_comprehension_over_user_iterable_uses_native_iterator_protocol(tmp_path):
    src = tmp_path / "comp_iter.py"
    src.write_text(
        textwrap.dedent(
            """
            class Bag:
                def __init__(self) -> None:
                    self.values = [1, 2, 3]

                def __iter__(self):
                    return iter(self.values)

            print([x + 1 for x in Bag()][2])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "comp_iter.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "4\n"


def test_range_comprehension_discard_target_rebinds_after_object_loop(tmp_path):
    src = tmp_path / "comp_range_discard_rebind.py"
    src.write_text(
        """
for _ in [object()]:
    pass
values = [0 for _ in range(3)]
print(len(values))
print(values[2])
"""
    , encoding="utf-8")
    exe = tmp_path / "comp_range_discard_rebind.out"
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == ["3", "0"]


def test_range_comprehension_named_target_rebinds_after_object_loop(tmp_path):
    src = tmp_path / "comp_range_named_rebind.py"
    src.write_text(
        """
for k in [object()]:
    pass
values = [k for k in range(3)]
print(len(values))
print(values[2])
"""
    , encoding="utf-8")
    exe = tmp_path / "comp_range_named_rebind.out"
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == ["3", "2"]
