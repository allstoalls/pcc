from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.pipeline import compile_python_multi


def test_imported_decorator_factory_call_is_metadata_noop(tmp_path):
    decos = tmp_path / "decos.py"
    decos.write_text(
        textwrap.dedent(
            """
            def metadata_decorator(dispatcher, module=None):
                return dispatcher
            """
        ).lstrip(),
        encoding="utf-8",
    )
    entry = tmp_path / "entry.py"
    entry.write_text(
        textwrap.dedent(
            """
            from decos import metadata_decorator

            def dispatch(value):
                return value

            @metadata_decorator(dispatch, module="entry")
            def value() -> int:
                return 42

            print(value())
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "decorated.out"

    compile_python_multi(
        [str(decos), str(entry)],
        str(exe),
        entry_module="entry",
        module_names=["decos", "entry"],
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"


def test_module_global_decorator_factory_call_is_metadata_noop(tmp_path):
    entry = tmp_path / "entry.py"
    entry.write_text(
        textwrap.dedent(
            """
            def dispatch(value):
                return value

            decorator_factory = 0

            @decorator_factory(dispatch, module="entry")
            def value() -> int:
                return 43

            print(value())
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "module_global_decorator.out"

    compile_python(
        str(entry),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "43\n"


def test_noop_decorated_function_with_kwargs_uses_direct_call(tmp_path):
    entry = tmp_path / "entry_kwargs.py"
    entry.write_text(
        textwrap.dedent(
            """
            decorator_factory = 0

            def dispatch(value):
                return value

            @decorator_factory(dispatch, module="entry")
            def value(x: int, y: int = 0) -> int:
                return x + y

            print(value(40, y=4))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "noop_decorator_kwargs.out"

    compile_python(
        str(entry),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "44\n"


def test_native_decorated_function_call_accepts_kwargs_for_codegen(tmp_path):
    entry = tmp_path / "native_decorator_kwargs.py"
    entry.write_text(
        textwrap.dedent(
            """
            def identity(fn):
                return fn

            @identity
            def value(x: int, y: int = 0) -> int:
                return x + y

            result = value(40, y=4)
            print(result)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "native_decorator_kwargs.out"

    compile_python(
        str(entry),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )


def test_imported_bare_decorator_is_metadata_noop(tmp_path):
    # numpy _core/numeric.py shape: ``@finalize_array_function_like`` — a
    # BARE imported name (not a factory call). The call-shaped imported
    # decorator was already treated as compile-time metadata; the bare
    # shape crashed declaration with "Layer 1 does not handle decorators".
    # Bare imported (cpy-env) decorators now fall under the same
    # imported-decorators-are-metadata principle; the underlying function
    # stays directly callable.
    src = tmp_path / "bare_dec.py"
    src.write_text(
        textwrap.dedent(
            """
            from functools import singledispatch


            @singledispatch
            def f(x):
                return x + 1


            def main() -> int:
                print(f(1))
                return 0


            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "bare_dec.out"
    compile_python(
        str(src),
        str(exe),
        libpython_mode="auto",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "2\n"


def test_same_module_bare_decorator_still_applies(tmp_path):
    # Boundary pin for the bare-imported-decorator metadata rule: a bare
    # decorator DEFINED IN THE SAME MODULE is semantic user code and must
    # keep applying for real (not be swallowed as metadata).
    src = tmp_path / "same_dec.py"
    src.write_text(
        textwrap.dedent(
            """
            def double_result(f):
                def wrapped(x: int) -> int:
                    return f(x) * 2
                return wrapped


            @double_result
            def g(x: int) -> int:
                return x + 1


            def main() -> int:
                print(g(3))
                return 0


            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "same_dec.out"
    compile_python(
        str(src),
        str(exe),
        libpython_mode="auto",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "8\n"


def test_same_module_decorator_varargs_wrapper_forwards_args_self_backend(tmp_path):
    src = tmp_path / "same_dec_varargs.py"
    src.write_text(
        textwrap.dedent(
            """
            def bump_result(f):
                def wrapped(*args):
                    return f(*args) + 1
                return wrapped


            @bump_result
            def add(a, b):
                return a + b


            def main():
                print(add(2, 3))


            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "same_dec_varargs.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "6\n"


def test_module_global_partial_decorator_factory_replaces_function_self_backend(
    tmp_path,
):
    src = tmp_path / "partial_factory_decorator.py"
    src.write_text(
        textwrap.dedent(
            """
            import functools


            def choose_implementation(implementation, module=None):
                def decorator(dispatcher):
                    return implementation
                return decorator


            def implementation(value):
                return value + 10


            decorator_factory = functools.partial(
                choose_implementation, module="entry"
            )


            @decorator_factory(implementation)
            def public(value):
                return (value,)


            print(globals()["public"](2))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "partial_factory_decorator.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "12\n"


def test_cross_module_calls_use_partial_decorator_replacement_self_backend(
    tmp_path,
):
    provider = tmp_path / "provider.py"
    provider.write_text(
        textwrap.dedent(
            """
            import functools


            def choose_implementation(implementation, module=None):
                def decorator(dispatcher):
                    return implementation
                return decorator


            def implementation(value):
                return value + 10


            decorator_factory = functools.partial(
                choose_implementation, module="provider"
            )


            @decorator_factory(implementation)
            def public(value):
                return (value,)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    entry = tmp_path / "entry.py"
    entry.write_text(
        textwrap.dedent(
            """
            import provider
            from provider import public

            print(public(2))
            print(provider.public(3))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "cross_module_partial_factory_decorator.out"
    compile_python_multi(
        [str(provider), str(entry)],
        str(exe),
        entry_module="entry",
        module_names=["provider", "entry"],
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "12\n13\n"
