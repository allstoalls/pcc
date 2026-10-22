import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_string_percent_tuple_formats_without_libpython(tmp_path):
    src = tmp_path / "string_percent_tuple.py"
    src.write_text(
        textwrap.dedent(
            """
            row = (3, 4)
            print("%d:%04d" % row)
            print("%.2f" % 1.25)
            print("%s=%r" % ("name", "bob"))
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "string_percent_tuple.out"
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == ["3:0004", "1.25", "name='bob'"]


def test_string_percent_mapping_formats_without_libpython(tmp_path):
    # ``%(name)s`` mapping form: the argument is dict[name].  Previously raised
    # "unsupported format character"; now handled by py_str_mod.
    src = tmp_path / "string_percent_mapping.py"
    src.write_text(
        textwrap.dedent(
            """
            d = {"name": "bob", "age": 30, "pct": 0.5}
            print("%(name)s is %(age)d" % d)
            print("%(name)-10s|%(age)05d" % d)
            print("%(pct).2f" % d)
            print("%(name)r" % d)
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "string_percent_mapping.out"
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == [
        "bob is 30",
        "bob       |00030",
        "0.50",
        "'bob'",
    ]
