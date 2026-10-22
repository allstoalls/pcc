"""Function-local class statements execute at their source position."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_function_local_class_has_per_call_identity_capture_and_errors(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent(
        """
        events = []

        def record(cls):
            events.append("class")
            return cls

        def reject(cls):
            raise ValueError("class boom")

        def make(value):
            @record
            class Local:
                def get(self):
                    return value
            return Local, Local()

        def explode():
            @reject
            class Bad:
                pass
            print("class-error-missed")

        first_class, first = make(3)
        second_class, second = make(7)
        print(first_class is second_class)
        print(first.get(), second.get())
        print(len(events))
        try:
            explode()
        except ValueError as exc:
            print(str(exc))
        """
    ).lstrip()
    expected = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout

    src = tmp_path / "local_class.py"
    exe = tmp_path / "local_class.out"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    for backend in range(5):
        run_env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"PCC_GC_BACKEND={backend}\n" + result.stderr
        )
        assert result.stdout == expected, f"PCC_GC_BACKEND={backend}"


def test_function_local_class_super_uses_the_class_of_each_execution(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    # Zero-argument super() and __class__ name the class object that this
    # execution of the class statement created (CPython's __class__ cell),
    # never a shared module slot, including in a classmethod and along a
    # chain of local subclasses.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent(
        """
        import gc


        class Base:
            def value(self):
                return 10

            @classmethod
            def build(cls):
                return "base:" + cls.__name__


        def make(step):
            class Local(Base):
                def value(self):
                    return super().value() + step

                @classmethod
                def build(cls):
                    return "local:" + super().build()

                def defining(self):
                    return __class__

            class Child(Local):
                def value(self):
                    return super().value() * 100

            return Local, Child


        def main():
            first, first_child = make(1)
            second, second_child = make(2)
            gc.collect()
            print(first is second, first_child is second_child)
            a = first()
            b = second()
            print(a.value(), b.value(), a.value())
            print(first.build(), second.build(), first_child.build())
            print(a.defining() is first, b.defining() is second)
            child = first_child()
            other = second_child()
            gc.collect()
            print(child.value(), other.value(), child.defining() is first)


        main()
        """
    ).lstrip()
    expected = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout
    assert expected == (
        "False False\n11 12 11\n"
        "local:base:Local local:base:Local local:base:Child\n"
        "True True\n1100 1200 True\n"
    )

    src = tmp_path / "local_class_super.py"
    exe = tmp_path / "local_class_super.out"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    for backend in range(5):
        run_env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"PCC_GC_BACKEND={backend} rc={result.returncode}\n"
            + result.stdout + result.stderr
        )
        assert result.stdout == expected, f"PCC_GC_BACKEND={backend}"


def test_function_local_class_binding_has_owned_gc_root_shape():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.type_infer import infer_module

    source = textwrap.dedent(
        """
        def make(value):
            class Local:
                def get(self):
                    return value
            return Local, Local()
        """
    ).lstrip()
    typed = infer_module(
        parse_and_lift(source, "local_class_shape.py", "local_class_shape")
    )
    ir_text = str(L1CodeGen(typed, ir_scaffold_mode="on").generate(typed))
    start = ir_text.index(
        "define external ptr @user_local_class_shape_make("
    )
    end = ir_text.index("}\n", start)
    body = ir_text[start:end]

    assert "Local.class.addr" in body
    assert "@pcc_gc_frame_enter" in body
    assert "@pcc_gc_store_root" in body
    assert "@pcc_gc_load_ptr" in body
    assert "@pcc_gc_frame_leave" in body
    # The class statement publishes ``get`` as a function object whose
    # captures tuple carries the ``value`` cell (no hidden instance
    # attributes); the method body takes it as a trailing parameter.
    assert "@py_func_new_signature_slots" in body
    assert "Local_get_method_native_adapter" in body
    assert "define external ptr @user_local_class_shape_Local_get(ptr %self, ptr %value)" in ir_text
    assert any("store i1 1" in line and ".bound." in line and "Local" in line for line in body.splitlines())
