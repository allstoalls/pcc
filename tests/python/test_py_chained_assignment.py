import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_class_chained_assignment_initializes_all_aliases(tmp_path):
    src = tmp_path / "class_chained_assignment.py"
    src.write_text(
        textwrap.dedent(
            """
            class C:
                a = b = c = 7

            print(C.a)
            print(C.b)
            print(C.c)
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "class_chained_assignment.out"
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == ["7", "7", "7"]
