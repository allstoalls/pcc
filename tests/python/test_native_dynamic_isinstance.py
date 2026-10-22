from __future__ import annotations

import subprocess
import textwrap


def test_annotated_builtin_buffers_keep_semantic_types_for_isinstance():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_ast import (
        ByteArrayType,
        BytesType,
        MemoryViewType,
        SetType,
    )
    from pcc.frontends.python.type_infer import infer_module

    source = textwrap.dedent("""
        def check_bytes(value: bytes) -> bool:
            return isinstance(value, bytes)

        def check_bytearray(value: bytearray) -> bool:
            return isinstance(value, bytearray)

        def check_memoryview(value: memoryview) -> bool:
            return isinstance(value, memoryview)

        def check_set(value: set[int]) -> bool:
            return isinstance(value, set)
    """)
    typed = infer_module(parse_and_lift(source, "annotated.py", "annotated"))

    annotations = tuple(fn.args[0].annotation for fn in typed.body)
    assert isinstance(annotations[0], BytesType)
    assert isinstance(annotations[1], ByteArrayType)
    assert isinstance(annotations[2], MemoryViewType)
    assert isinstance(annotations[3], SetType)

    ir_text = str(L1CodeGen(typed, ir_scaffold_mode="on").generate(typed))
    bytes_body = ir_text.split("define external i1 @user_annotated_check_bytes", 1)[1]
    bytes_body = bytes_body.split("\n}", 1)[0]
    assert "zext i1 0 to i32" not in bytes_body
    assert "zext i1 1 to i32" in bytes_body


def test_dynamic_isinstance_second_arg_dispatches_to_runtime(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dynamic_isinstance.py"
    src.write_text(
        textwrap.dedent("""
        class A:
            pass

        class B:
            pass

        def check(obj, cls):
            return isinstance(obj, cls)

        print(check(A(), A))
        print(check(B(), A))
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "dynamic_isinstance.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nFalse\n"


def test_isinstance_builtin_exception_matches(tmp_path):
    """``isinstance(exc, BuiltinExcClass)`` must match a builtin exception
    object via its exc_class MRO. The frontend previously constant-folded a
    builtin exception class name (not a user class, not a builtin type tag) to
    False, and the runtime py_isinstance returned 0 for a PY_TYPE_EXC object."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "isinstance_exc.py"
    src.write_text(
        textwrap.dedent("""
        print(isinstance(ValueError('a'), ValueError))     # True
        print(isinstance(ValueError('a'), (ValueError, RuntimeError)))  # True
        print(isinstance(OSError('b'), (ValueError, RuntimeError)))     # False
        print(isinstance(KeyError('k'), Exception))        # True (MRO)
        print(isinstance(KeyError('k'), LookupError))      # True (MRO)
        results = []
        for e in [ValueError('a'), OSError('b'), RuntimeError('c')]:
            try:
                raise e
            except Exception as exc:
                results.append(isinstance(exc, (ValueError, RuntimeError)))
        print(results)                                     # [True, False, True]
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "isinstance_exc.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == (
        "True\nTrue\nFalse\nTrue\nTrue\n[True, False, True]\n"
    ), run.stdout


def test_builtin_exception_class_can_be_captured_as_default(tmp_path):
    """Builtin exception names in value position stay native.

    Package hot paths commonly capture globals as defaults.  The class object
    must be the same cached native object used by exception construction and
    matching, without a CPython builtin lookup.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "exception_default.py"
    src.write_text(
        textwrap.dedent("""
            def matches(value, cls=ValueError):
                return isinstance(value, cls)

            print(matches(ValueError('bad')))
            print(matches(TypeError('bad')))
            print(ValueError is ValueError)
            """),
        encoding="utf-8",
    )
    exe = tmp_path / "exception_default.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nFalse\nTrue\n"


def test_dyn_builtin_isinstance_uses_runtime_type_tag(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "builtin_dyn_isinstance.py"
    src.write_text(
        textwrap.dedent("""
        def check_tuple(obj):
            return isinstance(obj, tuple)

        print(check_tuple((1, 2)))
        print(check_tuple([1, 2]))
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "builtin_dyn_isinstance.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nFalse\n"


def test_dyn_bytes_bytearray_and_set_isinstance_use_runtime_type_tags(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "builtin_buffer_isinstance.py"
    src.write_text(
        textwrap.dedent("""
        def classify(obj):
            return (
                isinstance(obj, (bytes, bytearray)),
                isinstance(obj, set),
            )

        print(classify(b"x"))
        print(classify(bytearray(b"x")))
        print(classify({1}))
        print(classify("x"))
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "builtin_buffer_isinstance.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == (
        "(True, False)\n"
        "(True, False)\n"
        "(False, True)\n"
        "(False, False)\n"
    )


def test_isinstance_tuple_accepts_type_none(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "isinstance_type_none.py"
    src.write_text(
        textwrap.dedent("""
        def check(obj):
            return isinstance(obj, (type(None), str))

        print(check(None))
        print(check("x"))
        print(check(1))
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "isinstance_type_none.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nTrue\nFalse\n"


def test_isinstance_accepts_dynamic_type_call(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "isinstance_dynamic_type.py"
    src.write_text(
        textwrap.dedent("""
        class Box:
            def same_type(self, other):
                return isinstance(other, type(self))

        print(Box().same_type(Box()))
        print(Box().same_type(1))
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "isinstance_dynamic_type.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nFalse\n"


def test_isinstance_tuple_accepts_dynamic_type_call(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "isinstance_tuple_dynamic_type.py"
    src.write_text(
        textwrap.dedent("""
        class Box:
            def same_or_text(self, other):
                return isinstance(other, (str, type(self)))

        print(Box().same_or_text(Box()))
        print(Box().same_or_text("x"))
        print(Box().same_or_text(1))
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "isinstance_tuple_dynamic_type.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nTrue\nFalse\n"


def test_isinstance_slice_and_getitem_slice(tmp_path):
    """``isinstance(x, slice)`` recognizes slice objects, and ``obj[a:b:c]`` on
    a user class dispatches ``__getitem__(slice(a,b,c))`` (the common
    ``if isinstance(key, slice):`` __getitem__ idiom). Both were previously
    broken: isinstance(x, slice) constant-folded to False, and slice subscript
    on a ClassType raised "Layer 1 slice on type ClassType not supported"."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "slice_gi.py"
    src.write_text(
        textwrap.dedent("""
        print(isinstance(slice(1, 4), slice))   # True
        print(isinstance(5, slice))             # False
        print(isinstance([1], slice))           # False

        class MyList:
            def __init__(self, data):
                self.data = data
            def __getitem__(self, key):
                if isinstance(key, slice):
                    return self.data[key.start:key.stop:key.step]
                return self.data[key]

        m = MyList([0, 1, 2, 3, 4, 5])
        print(m[2])         # 2
        print(m[1:4])       # [1, 2, 3]
        print(m[::2])       # [0, 2, 4]
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "slice_gi.out"
    compile_python(
        str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off", backend="self"
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == ("True\nFalse\nFalse\n2\n[1, 2, 3]\n[0, 2, 4]\n"), run.stdout
