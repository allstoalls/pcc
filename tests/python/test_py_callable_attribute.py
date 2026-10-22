from __future__ import annotations

import textwrap
import subprocess
import sys

from pcc.frontends.python.pipeline import compile_python


def test_instance_field_callable_call_compiles_to_dynamic_attribute_call(tmp_path):
    src = tmp_path / "callable_attr.py"
    src.write_text(
        textwrap.dedent("""
            class Adder:
                def __call__(self, x: int, y: int) -> int:
                    return x + y

            class Box:
                def __init__(self, pyfunc) -> None:
                    self.pyfunc = pyfunc

                def run(self) -> int:
                    return self.pyfunc(20, 22)

            print(Box(Adder()).run())
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "callable_attr.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert exe.exists()


def test_local_callable_shadows_same_named_function_for_codegen(tmp_path):
    src = tmp_path / "local_callable_shadow.py"
    src.write_text(
        textwrap.dedent("""
            class Box:
                def put(self, ind, v, mode=None):
                    return v

            def put(a, ind, v, mode="raise"):
                put = a.put
                return put(ind, v, mode=mode)

            print(put(Box(), 0, 7))
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "local_callable_shadow.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert exe.exists()


def test_unhinted_module_global_dunder_new_does_not_bind_unrelated_class(tmp_path):
    src = tmp_path / "foreign_dunder_new.py"
    src.write_text(
        textwrap.dedent("""
            ndarray = None

            class recarray:
                def __new__(subtype, shape, buf=None):
                    return ndarray.__new__(
                        subtype, shape, buffer=buf,
                    )

            print("compiled")
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "foreign_dunder_new.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert exe.exists()


def test_class_call_uses_dunder_new_when_init_is_absent(tmp_path):
    src = tmp_path / "class_dunder_new_call.py"
    src.write_text(
        textwrap.dedent("""
            class Factory:
                def __new__(cls, left, right=2):
                    return left + right

            print(Factory(40, right=2))
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "class_dunder_new_call.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "42"


def test_dunder_new_private_class_attribute_uses_mangled_name(tmp_path):
    src = tmp_path / "private_singleton_dunder_new.py"
    src.write_text(
        textwrap.dedent("""
            class _NoValueType:
                __instance = None

                def __new__(cls):
                    if not cls.__instance:
                        cls.__instance = super().__new__(cls)
                    return cls.__instance

            first = _NoValueType()
            second = _NoValueType()
            print(first is second)
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "private_singleton_dunder_new.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\n"


def test_super_dunder_new_walks_through_intermediate_base(tmp_path):
    src = tmp_path / "inherited_singleton_dunder_new.py"
    src.write_text(
        textwrap.dedent("""
            class Root:
                pass

            class SingletonBase(Root):
                _instance = None

                def __new__(cls):
                    if cls._instance is None:
                        cls._instance = super().__new__(cls)
                    return cls._instance

            class Concrete(SingletonBase):
                pass

            first = Concrete()
            second = Concrete()
            print(first is second)
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "inherited_singleton_dunder_new.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\n"


def test_builtin_type_dunder_new_is_a_native_callable_value(tmp_path):
    src = tmp_path / "builtin_type_dunder_new_value.py"
    src.write_text(
        textwrap.dedent("""
            new = int.__new__
            print(new(int, "41"))
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "builtin_type_dunder_new_value.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "41\n"


def test_method_default_resolves_preceding_class_literal(tmp_path):
    src = tmp_path / "class_literal_method_default.py"
    src.write_text(
        textwrap.dedent("""
            class Parameter:
                POSITIONAL_OR_KEYWORD = 1

                def __init__(self, kind=POSITIONAL_OR_KEYWORD):
                    self.kind = kind

                POSITIONAL_OR_KEYWORD = 2

            print(Parameter().kind)
            print(Parameter.POSITIONAL_OR_KEYWORD)
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "class_literal_method_default.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "1\n2\n"


def test_dyn_receiver_dunder_new_does_not_bind_unrelated_class_method_self_backend(
    tmp_path,
):
    src = tmp_path / "dyn_receiver_dunder_new.py"
    src.write_text(
        textwrap.dedent("""
            class MaskedRecords:
                def __new__(cls, shape, dtype=None, mask=None, **options):
                    return cls

            def reconstruct(subtype, data, mask, basetype):
                return subtype.__new__(
                    subtype,
                    data,
                    mask=mask,
                    dtype=basetype,
                )

            print("compiled")
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "dyn_receiver_dunder_new.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert exe.exists()


def test_native_module_alias_call_does_not_bind_unrelated_class_method(tmp_path):
    src = tmp_path / "module_alias_call_no_class_method_bind.py"
    src.write_text(
        textwrap.dedent("""
            import math as N

            class Matrix:
                def sqrt(self, x):
                    return x

            print(N.sqrt(4.0))
            print(N.sqrt(9.0) + 1.0, N.floor(2.5) * 2)
            """).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "module_alias_call_no_class_method_bind.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert exe.exists()
    # The alias call goes through the module object and returns an object;
    # its inferred type is float, so the value must reach print unboxed.
    expected = subprocess.run([sys.executable, str(src)], capture_output=True, text=True, timeout=60)
    ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert (ran.returncode, ran.stdout) == (0, expected.stdout), ran.stderr
