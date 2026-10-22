import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_for_loop_falls_back_to_object_iterator_for_pointer_typed_iterable(tmp_path):
    src = tmp_path / "for_pointer_iterable.py"
    src.write_text(
        textwrap.dedent(
            """
            def values(flag):
                xs = None
                if flag:
                    xs = [1, 2, 3]
                total = 0
                for item in xs:
                    total += item
                return total

            print(values(True))
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "for_pointer_iterable.out"
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    out = subprocess.check_output([str(exe)], text=True).strip()
    assert out == "6"
