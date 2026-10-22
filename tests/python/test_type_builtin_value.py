"""The builtin ``type`` used as a value lowers natively.

Every other builtin type name had a native value, but ``type`` in a value
position (``(int, str, type)``, ``kind = type``) went through a CPython
``import builtins``. In strict no-libpython compilation that replaced the whole
enclosing function with a stub raising "no-libpython function unavailable", so
the object-identity native test failed on every target (first seen on Windows,
run 38567599226). Calling that value must then act as ``type`` does: the
generic class-call path built an instance of type instead, so ``kind(1)``
returned a fresh object and ``kind(name, bases, namespace)`` made no class.
Both spellings of the three-argument form also left the class pointing into
the name string's bytes, so a computed name read freed memory once the string
died (GC0-GC3 showed another class's name, GC4 showed ``__doc__``), and a
name with a NUL was silently truncated where CPython raises ValueError.
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROGRAM = '''
import gc


class Box:
    pass


def call(f, x):
    return f(x)


def main() -> None:
    kinds = (int, str, type)
    kind = type
    print(kinds[2] is type, kind is type, type(int) is type, type(type) is type)
    print(isinstance(int, type), isinstance(Box, kind), isinstance(1, kind))
    print(issubclass(type, object), issubclass(Box, object), kind.__name__)
    print([k.__name__ for k in kinds])
    print(kind(1) is int, kind("a") is str, kind(Box()) is Box, call(type, []) is list)
    made = kind("Made", (Box,), {"a": 1})
    print(made.__name__, made.a, isinstance(made(), Box), type(int)(2.5) is float)
    many = [kind("M" + str(i), (), {"i": i}) for i in range(40)]
    direct = [type("D" + str(i), (), {"i": i}) for i in range(40)]
    gc.collect()
    print(sum(m.i for m in many), many[7].__name__, many[7]().i)
    print(direct[7].__name__, direct[39].__name__, type("N" + str(3), (), {}).__name__)
    for maker in (type, kind):
        try:
            maker("A\\0B", (), {})
        except ValueError as exc:
            print(exc)
    try:
        kind()
    except TypeError as exc:
        print(exc)
    try:
        kind(1, 2)
    except TypeError as exc:
        print(exc)
    try:
        kind(1, a=2)
    except TypeError as exc:
        print(exc)


main()
'''


def test_type_value_does_not_stub_the_function(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "type_value.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    output = tmp_path / "type_value.ll"
    compile_python(str(source), str(output), emit_llvm_only=True, libpython_mode="off",
                   backend="self", ir_scaffold_mode="on")
    text = output.read_text(encoding="utf-8")
    assert "strict.nolib.stub" not in text
    assert "call ptr (ptr) @py_cpy_import(" not in text


def test_type_value_matches_cpython_under_five_collectors(tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "type_value.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "type_value"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
