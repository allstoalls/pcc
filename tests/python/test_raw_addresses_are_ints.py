"""Raw C addresses are ``int`` values in application modules.

Pointer intrinsics (``malloc``, ``ptr_add``, ``stack_alloc``, ...) and
``c_rawptr`` extern results type as ``int`` outside pointer-lane modules, so a
raw address can never be handed to the object refcount protocol as if it were
a ``PyObject*``.  ``c_ptr``/``c_str`` extern returns are rejected in those
modules because the frontend cannot tell an object from raw memory; callers
declare ``c_obj`` or ``c_rawptr``.  Freestanding kernels and runtime ports
(``__pcc_runtime_port__ = True``) keep the pointer lane unchanged.

The host tests pin the static typing; the pcc-compiled probe executes the
lowering (int locals, untyped parameters carrying tagged addresses, tuples of
addresses, extern round trips) on the self backend without libpython.
"""

from __future__ import annotations

import subprocess
import os
import textwrap
from pathlib import Path

import pytest

from pcc.parse.py_lift import parse_and_lift
from pcc.py_frontend.py_ast import Assign, DynType, FuncDef, FuncType, IntType, Return
from pcc.py_frontend.pipeline import compile_python
from pcc.py_frontend.pipeline_freestanding import (
    source_declares_freestanding_module,
    source_declares_runtime_port_module,
)
from pcc.py_frontend.type_infer import PyFrontendError, infer_module


def _infer(source: str, name: str = "raw_addr_mod"):
    module = parse_and_lift(textwrap.dedent(source).lstrip(), name + ".py", name)
    return infer_module(module)


def _return_type(typed, function_name: str):
    function = next(
        stmt for stmt in typed.body if isinstance(stmt, FuncDef) and stmt.name == function_name
    )
    returned = next(stmt for stmt in function.body if isinstance(stmt, Return))
    assert returned.value is not None
    return returned.value.ty


_INTRINSIC_SOURCE = """
    from pcc.unsafe import malloc, ptr_add, stack_alloc, load_i64, int_to_ptr, null

    def fresh(size: int):
        return malloc(size)

    def shifted(size: int):
        return ptr_add(malloc(size), 8)

    def on_stack():
        return stack_alloc(32)

    def zero():
        return null()

    def word(address: int):
        return load_i64(int_to_ptr(address), 0)
    """


def test_pointer_intrinsics_type_as_int_in_application_modules() -> None:
    typed = _infer(_INTRINSIC_SOURCE)
    for name in ("fresh", "shifted", "on_stack", "zero"):
        assert isinstance(_return_type(typed, name), IntType), name
    assert isinstance(_return_type(typed, "word"), IntType)


@pytest.mark.parametrize("directive", ["__pcc_freestanding__", "__pcc_runtime_port__"])
def test_pointer_lane_modules_keep_pointer_intrinsics_dynamic(directive: str) -> None:
    typed = _infer(directive + " = True\n" + textwrap.dedent(_INTRINSIC_SOURCE))
    for name in ("fresh", "shifted", "on_stack", "zero"):
        assert isinstance(_return_type(typed, name), DynType), name


def test_extern_c_rawptr_returns_int_and_c_obj_returns_object() -> None:
    typed = _infer(
        """
        from pcc.extern import c_int64, c_obj, c_ptr, c_rawptr, extern

        _utf8 = extern("py_str_utf8", (c_ptr,), c_rawptr)
        _bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_obj)

        def address(text: str):
            return _utf8(text)

        def payload(text: str):
            return _bytes_new(_utf8(text), 3)
        """
    )
    declarations = [stmt for stmt in typed.body if isinstance(stmt, Assign)]
    assert isinstance(declarations[0].targets[0].ty, FuncType)
    assert isinstance(declarations[0].targets[0].ty.ret, IntType)
    assert isinstance(declarations[1].targets[0].ty.ret, DynType)
    assert isinstance(_return_type(typed, "address"), IntType)
    assert isinstance(_return_type(typed, "payload"), DynType)


@pytest.mark.parametrize("marker", ["c_ptr", "c_str"])
def test_ambiguous_pointer_extern_returns_are_rejected(marker: str) -> None:
    with pytest.raises(PyFrontendError) as raised:
        _infer(
            f"""
            from pcc.extern import c_ptr, c_str, extern

            _lookup = extern("getenv", (c_ptr,), {marker})

            def home():
                return _lookup("HOME")
            """
        )
    message = str(raised.value)
    assert "ambiguous between a Python object and a raw address" in message
    assert "c_obj" in message and "c_rawptr" in message


@pytest.mark.parametrize("directive", ["__pcc_freestanding__", "__pcc_runtime_port__"])
def test_pointer_lane_modules_still_accept_c_ptr_returns(directive: str) -> None:
    typed = _infer(
        directive
        + " = True\n"
        + textwrap.dedent(
            """
            from pcc.extern import c_ptr, extern

            _lookup = extern("getenv", (c_ptr,), c_ptr)

            def home():
                return _lookup("HOME")
            """
        )
    )
    assert isinstance(_return_type(typed, "home"), DynType)


def test_runtime_port_directive_scanner_matches_the_freestanding_contract() -> None:
    assert source_declares_runtime_port_module("__pcc_runtime_port__ = True\n")
    assert not source_declares_runtime_port_module("__pcc_freestanding__ = True\n")
    assert not source_declares_freestanding_module("__pcc_runtime_port__ = True\n")
    assert not source_declares_runtime_port_module("def f():\n    __pcc_runtime_port__ = True\n")
    # A docstring line that starts with ``class `` must not hide the directive.
    grammar_docstring = '"""Grammar::\n\n    class := SP* candidate\n"""\n__pcc_runtime_port__ = True\n'
    assert source_declares_runtime_port_module(grammar_docstring)
    assert not source_declares_freestanding_module(grammar_docstring)


def test_every_non_freestanding_runtime_port_declares_the_directive() -> None:
    """The object-model kernel builds objects out of raw memory; every port
    module must opt into the pointer lane explicitly."""
    ports = Path(__file__).resolve().parents[2] / "pcc" / "py_runtime" / "py"
    missing = []
    for path in sorted(ports.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if source_declares_freestanding_module(source):
            continue
        if not source_declares_runtime_port_module(source):
            missing.append(path.name)
    assert missing == []


_PROBE = """
    from pcc.extern import c_int64, c_obj, c_ptr, c_rawptr, extern
    from pcc.unsafe import free, load_i64, load_i8, malloc, ptr_add, ptr_is_null, store_i64

    _bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_obj)
    _str_utf8 = extern("py_str_utf8", (c_ptr,), c_rawptr)


    def fill(base, count: int) -> int:
        i = 0
        total = 0
        while i < count:
            store_i64(base, i * 8, i * 3)
            total += i * 3
            i += 1
        return total


    def sum_words(base: int, count: int) -> int:
        i = 0
        total = 0
        while i < count:
            total += load_i64(base, i * 8)
            i += 1
        return total


    def pair_of_buffers(size: int):
        return (malloc(size), malloc(size))


    def main() -> int:
        buf = malloc(64)
        if ptr_is_null(buf) != 0:
            return 1
        print(fill(buf, 8), sum_words(buf, 8))
        print(load_i64(ptr_add(buf, 8), 0))
        pair = pair_of_buffers(16)
        a = pair[0]
        b = pair[1]
        store_i64(a, 0, 11)
        store_i64(b, 0, 22)
        print(load_i64(a, 0) + load_i64(b, 0))
        print(a == b, a != b)
        text = _str_utf8("hey")
        print(load_i8(text, 0), load_i8(text, 2))
        payload = _bytes_new(text, 3)
        print(payload == b"hey", len(payload))
        print(isinstance(buf, int))
        free(buf)
        free(a)
        free(b)
        return 0


    print("rc", main())
    """

_PROBE_EXPECTED = "84 84\n3\n33\nFalse True\n104 121\nTrue 3\nTrue\nrc 0\n"


def test_raw_addresses_execute_as_ints_on_the_self_backend(tmp_path: Path) -> None:
    src = tmp_path / "raw_addresses_probe.py"
    exe = tmp_path / "raw_addresses_probe"
    src.write_text(textwrap.dedent(_PROBE).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout == _PROBE_EXPECTED


@pytest.mark.parametrize("directive", ["", "__pcc_runtime_port__ = True\n"])
def test_pointer_difference_decodes_dynamic_address_arguments(tmp_path, pcc_py_runtime_archive, directive):
    src = tmp_path / "address_difference.py"
    exe = tmp_path / "address_difference"
    src.write_text(directive + '''from pcc.unsafe import malloc, free, ptr_add, ptr_diff, null
def difference(left, right):
    return ptr_diff(left, right)
def aligned_4k(pointer):
    address = ptr_diff(pointer, null())
    return ptr_add(pointer, ((address + 4095) // 4096) * 4096 - address)
def main():
    base = malloc(8192)
    interior = ptr_add(base, 3)
    assert difference(interior, base) == 3
    assert difference(base, interior) == -3
    aligned = aligned_4k(base)
    assert difference(aligned, null()) % 4096 == 0
    assert 0 <= difference(aligned, base) < 4096
    free(base)
    print("ADDRESS_DIFFERENCE_OK")
main()
''')
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on",
                   backend="self", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        done = subprocess.run([str(exe)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                              capture_output=True, text=True, timeout=20)
        assert done.returncode == 0, f"GC{backend}: {done.stdout}{done.stderr}"
        assert done.stdout.strip() == "ADDRESS_DIFFERENCE_OK"


def test_runtime_port_sibling_keeps_the_pointer_lane_when_compiled_in_a_closure(tmp_path: Path) -> None:
    """A runtime-port module imported by an application is compiled as a
    closure sibling; it must keep pointer-lane lowering (no run-time untag
    select, no precise-stack-map ambiguity) exactly as when compiled alone."""
    port = tmp_path / "port_words.py"
    port.write_text(
        textwrap.dedent(
            """
            \"\"\"A tiny runtime-port style module: raw memory becomes a payload.\"\"\"
            __pcc_runtime_port__ = True

            from pcc.extern import c_abi_export
            from pcc.unsafe import load_i64, malloc, store_i64


            def _fill(base, count: int) -> int:
                i = 0
                while i < count:
                    store_i64(base, i * 8, i + 1)
                    i += 1
                return count


            @c_abi_export("port_words_sum")
            def port_words_sum(count: int) -> int:
                base = malloc(count * 8)
                _fill(base, count)
                total = 0
                i = 0
                while i < count:
                    total += load_i64(base, i * 8)
                    i += 1
                return total
            """
        ).lstrip(),
        encoding="utf-8",
    )
    app = tmp_path / "app.py"
    app.write_text(
        textwrap.dedent(
            """
            from pcc.extern import c_int64, extern

            import port_words  # noqa: F401  (compiled into this closure)

            _sum = extern("port_words_sum", (c_int64,), c_int64)
            print(_sum(4))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "app"
    compile_python(str(app), str(exe), libpython_mode="off", ir_scaffold_mode="on", backend="self")
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "10\n"
