import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_list_slice_augassign_lowers_without_libpython(tmp_path):
    src = tmp_path / "slice_augassign.py"
    src.write_text(
        textwrap.dedent(
            """
            values = [1, 2, 3, 4]
            values[1:3] += [9]
            print(values)
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "slice_augassign.out"
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    out = subprocess.check_output([str(exe)], text=True).strip()
    assert out == "[1, 2, 3, 9, 4]"


def test_class_type_slice_assignment_compiles_without_libpython_self_backend(tmp_path):
    src = tmp_path / "class_slice_assignment.py"
    src.write_text(
        textwrap.dedent(
            """
            class Box:
                pass

            b = Box()
            b[0:1] = [1]
            print("compiled")
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "class_slice_assignment.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert exe.exists()
