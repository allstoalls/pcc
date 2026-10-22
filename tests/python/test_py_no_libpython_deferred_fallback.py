from __future__ import annotations

import os
import subprocess
import sys
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_deferred_cpython_fallback_becomes_native_error_stub(tmp_path, monkeypatch):
    src = tmp_path / "deferred_fallback.py"
    src.write_text(
        textwrap.dedent("""
            def deferred(value):
                return locals()

            print("imported")
            try:
                deferred("1")
            except NotImplementedError as exc:
                print(str(exc))
            """),
        encoding="utf-8",
    )
    exe = tmp_path / "deferred_fallback.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "imported",
        "no-libpython function unavailable: deferred_fallback.deferred",
    ]


def test_deferred_method_and_generator_fallbacks_become_native_stubs(
    tmp_path, monkeypatch
):
    src = tmp_path / "deferred_shapes.py"
    src.write_text(
        textwrap.dedent("""
            class Deferred:
                def method(self, value):
                    return locals()

            def generator(value):
                yield locals()

            try:
                Deferred().method("1")
            except NotImplementedError as exc:
                print(str(exc))

            try:
                next(generator("1"))
            except NotImplementedError as exc:
                print(str(exc))
            """),
        encoding="utf-8",
    )
    exe = tmp_path / "deferred_shapes.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "no-libpython function unavailable: deferred_shapes.method",
        "no-libpython function unavailable: deferred_shapes.generator",
    ]


def test_cpython_value_at_a_slot_call_defers_its_function(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    """A CPython-backed value reaching a slot-call operand stubs its function.

    The slot-call path refused such a name with "slot-call CPython name
    requires an output-slot bridge", which failed the whole compile (most
    often a value from a module without a native provider, like ``array``).
    The function already holds CPython-backed code, so it becomes the same
    fail-closed stub as any other deferred fallback.
    """
    src = tmp_path / "deferred_operand.py"
    src.write_text(
        textwrap.dedent("""
            def deferred(value):
                names = locals()
                return iter(names)

            class Deferred:
                def method(self, value):
                    names = locals()
                    return iter(names)

            print("imported")
            for call in (deferred, Deferred().method):
                try:
                    call("1")
                except NotImplementedError as exc:
                    print(str(exc))
            """),
        encoding="utf-8",
    )
    exe = tmp_path / "deferred_operand.out"
    python_program_compiler(
        str(src), str(exe), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "imported",
        "no-libpython function unavailable: deferred_operand.deferred",
        "no-libpython function unavailable: deferred_operand.method",
    ]


def test_class_module_init_does_not_demand_libpython(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    """CPython values in the class module-init function do not fail a compile.

    ``_pcc_py_module_init_<mod>`` evaluates class bodies outside the module's
    executing scope, where a module without a native provider is a CPython
    binding, and nothing calls it at run time. A strict multi-file compile
    refused the whole program for that dead code ("requires libpython
    fallback for multi-file compile (modules: holder)"); the unavailable
    module now fails where its import executes.
    """
    program = tmp_path / "program"
    elsewhere = tmp_path / "elsewhere"
    program.mkdir()
    elsewhere.mkdir()
    (elsewhere / "extmod.py").write_text("VALUE = 7\n", encoding="utf-8")
    (program / "holder.py").write_text(
        textwrap.dedent("""
            import extmod


            class Holder:
                mod = extmod
            """),
        encoding="utf-8",
    )
    main = program / "main.py"
    main.write_text(
        "import holder\n\nprint(holder.Holder.mod.VALUE)\n", encoding="utf-8",
    )
    reference = subprocess.run(
        [sys.executable, str(main)], capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=str(elsewhere)),
    )
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == "7\n"

    exe = tmp_path / "main.out"
    python_program_compiler(
        str(main), str(exe), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 1, run.stdout + run.stderr
    lines = run.stderr.splitlines()
    assert lines[-2].strip() == "import extmod", run.stderr
    assert lines[-1].endswith(": No module named 'extmod'"), run.stderr
