"""cpython-compat dlopen'd C-extension import must use libpython's real C-API.

Regression for the bug where pcc's native C-API definitions shadowed
libpython's real C-API for dlopen'd extensions. CPython extensions are built
``-undefined dynamic_lookup``, so exported ``Py*`` symbols from the executable
won over libpython and ran pcc-object-model operations on real CPython objects.

The compatibility archive retains pcc's C-API objects for native runtime
semantics, while the final linker makes those symbols non-exported so CPython
extensions bind libpython. See
``docs/investigations/python-cpython-compat-import-numpy-multiarray-init-fails.md``.

This uses ``unicodedata`` - a stdlib C extension present in any CPython, so no
third-party install is needed; the test compares the pcc cpython-compat binary's
output against the same program under CPython (differential).
"""
from __future__ import annotations

import marshal as host_marshal
import shutil
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _have_libpython() -> bool:
    # cpython-compat needs the host Python's C headers/lib to link libpython.
    if shutil.which("python3-config") is None:
        return False
    probe = subprocess.run(
        ["python3-config", "--includes"], capture_output=True, text=True
    )
    return probe.returncode == 0 and bool(probe.stdout.strip())


@pytest.mark.pcc_gate(unavailable=None if _have_libpython() else "cpython-compat requires libpython headers/lib")
def test_cpython_compat_imports_stdlib_c_extension(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "ud.py"
    exe = tmp_path / "ud.out"
    src.write_text(
        textwrap.dedent(
            """
            import unicodedata

            def main() -> None:
                # category()/name() rely on attributes set by the extension's
                # module-init (Py_mod_exec) slot - the part that failed when the
                # shim shadowed libpython's C-API.
                print(unicodedata.category("A"))
                print(unicodedata.name("A"))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    compile_python(str(src), str(exe), libpython_mode="on")

    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, (
        "cpython-compat unicodedata import failed (shim shadowing libpython?):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The extension loaded and its module-init attributes are set: category() of
    # 'A' is 'Lu', name() is 'LATIN CAPITAL LETTER A'. Compare to CPython.
    assert result.stdout == cpython, f"pcc {result.stdout!r} != CPython {cpython!r}"
    assert "Lu" in result.stdout


@pytest.mark.pcc_gate(
    unavailable=None
    if _have_libpython()
    else "cpython-compat requires libpython headers/lib"
)
def test_cpython_compat_preserves_native_runtime_semantics(tmp_path):
    """libpython mode must isolate, not delete, pcc's native C-API owners."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "mixed.py"
    exe = tmp_path / "mixed.out"
    payloads = [
        repr(host_marshal.dumps(value))
        for value in (
            None,
            True,
            1.5,
            "bridge",
            [1, 2],
            (3, 4),
            {"answer": 42},
            {7},
            2**100,
            -(2**100),
        )
    ]
    src.write_text(
        textwrap.dedent(
            f"""
            import marshal
            import unicodedata

            def main() -> None:
                values = [3, 1, 2]
                getattr(values, "sort")()
                print(values)
                bridged = [
                    marshal.loads({payloads[0]}),
                    marshal.loads({payloads[1]}),
                    marshal.loads({payloads[2]}),
                    marshal.loads({payloads[3]}),
                    marshal.loads({payloads[4]}),
                    marshal.loads({payloads[5]}),
                    marshal.loads({payloads[6]}),
                    marshal.loads({payloads[7]}),
                    marshal.loads({payloads[8]}),
                    marshal.loads({payloads[9]}),
                ]
                print(bridged)
                print(unicodedata.category("A"))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    compile_python(str(src), str(exe), libpython_mode="on")
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, (
        "cpython-compat lost pcc-native runtime semantics:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout == expected.stdout

    symbols = subprocess.run(
        ["nm", "-g", str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    public_capi_definitions = []
    for line in symbols:
        fields = line.split()
        if len(fields) < 2 or fields[-2] == "U":
            continue
        symbol = fields[-1]
        bare = symbol[1:] if symbol.startswith("_") else symbol
        if bare.startswith("Py") or bare.startswith("_Py"):
            public_capi_definitions.append(symbol)
    assert not public_capi_definitions


def test_libpython_program_main_initializes_cpython_before_module_code(tmp_path):
    """The process main thread must win CPython initialization."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "main_thread_init.py"
    out = tmp_path / "main_thread_init.ll"
    src.write_text("print('ok')\n", encoding="utf-8")

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="on",
    )

    ir_text = out.read_text(encoding="utf-8")
    main_start = ir_text.index("define i32 @main(")
    main_end = ir_text.index("\n}", main_start)
    main_ir = ir_text[main_start:main_end]

    def calls(name):
        # The IR printer spells a call's function type: "call void (ptr) @f(".
        return [
            match.start()
            for match in re.finditer(
                r"call void (?:\([^)]*\) )?@" + name + r"\(", main_ir
            )
        ]

    args_call = calls("py_set_program_args")[0]
    init_call = calls("py_cpy_ensure_init")[0]
    print_call = calls("py_print")[0]
    assert args_call < init_call < print_call
    assert len(calls("py_cpy_ensure_init")) == 1


def test_chained_cpython_call_method_dispatches_via_libpython(tmp_path):
    """A method call on a CPython-call RESULT must use libpython's getattr.

    Regression: ``numpy.arange(10).sum()`` (method on a cpy-call result) lowered
    the ``.sum`` attribute access to NATIVE ``py_obj_getattr`` (pcc's object
    model), which mishandles the real CPython array object -> AttributeError,
    while the stored form ``a = numpy.arange(10); a.sum()`` worked. The
    method-call lowering now routes a ``Call`` receiver whose callable is a
    CPython-module attribute through the libpython method path
    (``py_cpy_getattr`` + ``py_cpy_call``). See
    ``method_call_expression_lowering.py`` Call-receiver branch and
    ``docs/investigations/python-cpython-compat-import-numpy-multiarray-init-fails.md``.

    Compile-only (``emit_llvm_only``): no numpy install or libpython link needed
    - ``import numpy`` marks ``numpy`` as a CPython module at compile time.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "chain.py"
    out = tmp_path / "chain.ll"
    src.write_text("import numpy\ndef f():\n    return numpy.arange(10).sum()\n", encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    # The chained ``.sum`` must dispatch via libpython (cpy attr constant +
    # py_cpy_getattr), NOT native py_obj_getattr on a CPython object.
    assert "@.cpy.attr.sum" in ir, "chained .sum should use the libpython method path"
    assert "@.pyattr.sum" not in ir, "chained .sum must NOT use native py_obj_getattr"


def test_type_builtin_on_cpython_value_dispatches_via_libpython(tmp_path):
    """``type(x)`` on a CPython value must return the real CPython type.

    Regression: ``type(numpy_array)`` lowered ``__class__`` via NATIVE
    ``py_obj_getattr``, which mishandles the real CPython object and returned a
    bogus value (``False``). The ``type()`` builtin now routes a cpy-value arg's
    ``__class__`` through ``py_cpy_getattr`` (and tags the result cpy). Compile-
    only; ``import numpy`` marks ``numpy`` as a CPython module at compile time.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "ty.py"
    out = tmp_path / "ty.ll"
    src.write_text("import numpy\ndef f():\n    return type(numpy.arange(5))\n", encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "@.cpy.attr.__class__" in ir, "type() of a cpy value should use libpython __class__"
    assert "py_cpy_getattr" in ir


def test_cpython_value_binary_op_dispatches_via_libpython(tmp_path):
    """Binary operators on a CPython value must use libpython's PyNumber_*.

    Regression: ``a + b`` on numpy arrays raised ``TypeError: unsupported
    operand type(s)`` because pcc lowered the binop NATIVELY (no handler for the
    real CPython object). The binop lowering now routes a binop where an operand
    is in ``_cpy_values`` through ``py_cpy_binop`` (over libpython
    ``PyNumber_Add``/etc.). Compile-only; ``import numpy`` marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "ar.py"
    out = tmp_path / "ar.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(3)\n"
        "    return a + a\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "py_cpy_binop" in ir, "a + a on a cpy value should route to py_cpy_binop"


def test_cpython_binop_receiver_method_dispatches_via_libpython(tmp_path):
    """A method call on a BINARY-OP result must use libpython's getattr.

    Regression: ``(a + b).sum()`` on numpy arrays - ``a + b`` produces a real
    CPython object (``py_cpy_binop``), but ``.sum`` lowered to NATIVE
    ``py_obj_getattr`` because the method-call lowering did not recognise a
    ``BinOp`` receiver as cpy (only ``Name``/``Attr``/``Call`` were handled).
    The stored form ``c = a + b; c.sum()`` worked via the assignment's cpy-value
    tagging. ``_expr_looks_cpython`` now detects a ``BinOp`` with a cpy operand,
    and the method-call lowering routes such a receiver through the libpython
    method path. Compile-only; ``import numpy`` marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "br.py"
    out = tmp_path / "br.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(3)\n"
        "    b = numpy.arange(3)\n"
        "    return (a + b).sum()\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "@.cpy.attr.sum" in ir, "(a+b).sum() should use the libpython method path"
    assert "@.pyattr.sum" not in ir, "(a+b).sum() must NOT use native py_obj_getattr"
    assert "py_cpy_binop" in ir


def test_cpython_value_power_op_dispatches_via_libpython(tmp_path):
    """The power operator ``**`` on a CPython value must use libpython.

    ``a ** 2`` on a numpy array routes through ``py_cpy_binop`` op 6, which calls
    libpython ``PyNumber_Power(base, exp, Py_None)``. Completes the cpy binary
    operator set (``+ - * / // % **``). Compile-only; ``import numpy`` marks
    ``numpy`` cpy. The native ``py_int_pow`` path is unaffected (it runs only for
    non-cpy operands, after this top-of-function cpy branch).
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "pw.py"
    out = tmp_path / "pw.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(3)\n"
        "    return a ** 2\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "py_cpy_binop" in ir, "a ** 2 on a cpy value should route to py_cpy_binop"


def test_cpython_subscript_receiver_method_dispatches_via_libpython(tmp_path):
    """A method call on a SUBSCRIPT result must use libpython's getattr.

    Regression: ``a[1:4].sum()`` on a numpy slice raised ``AttributeError: sum``
    because the method-call lowering recognised ``Call``/``BinOp`` receivers as
    cpy but not ``Subscript`` - so ``.sum`` lowered to NATIVE ``py_obj_getattr``
    on the real CPython slice object. The lowering now routes a ``Subscript``
    receiver (whose object is cpy via ``_expr_looks_cpython``) through the
    libpython method path; this also unblocked ``np.sum(a)``/``np.dot(b,b)``/
    ``a.shape[0]`` which followed the failing line. Compile-only; ``import
    numpy`` marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "sr.py"
    out = tmp_path / "sr.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(5)\n"
        "    return a[1:4].sum()\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "@.cpy.attr.sum" in ir, "a[1:4].sum() should use the libpython method path"
    assert "@.pyattr.sum" not in ir, "a[1:4].sum() must NOT use native py_obj_getattr"


def test_cpython_value_matmul_op_dispatches_via_libpython(tmp_path):
    """The matmul operator ``@`` on a CPython value must use libpython.

    Regression: ``a @ b`` on numpy arrays raised ``TypeError: unsupported
    operand type(s) for @`` - pcc lowered ``@`` to the native ``__matmul__``
    protocol (``py_user_matmul_dispatch``) on the real CPython object. ``a @ b``
    now routes through ``py_cpy_binop`` op 7 -> libpython
    ``PyNumber_MatrixMultiply``. Completes the cpy binary operator set
    ``+ - * / // % ** @``. Compile-only; ``import numpy`` marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "mm.py"
    out = tmp_path / "mm.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(4)\n"
        "    return a @ a\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "py_cpy_binop" in ir, "a @ a on a cpy value should route to py_cpy_binop"


def test_cpython_augassign_on_cpython_name_dispatches_via_libpython(tmp_path):
    """Augmented assignment on a CPython variable must use libpython.

    Regression: ``a += 2`` after ``a = numpy.ones(3)`` raised ``TypeError:
    unsupported operand type(s) for +``. The augassign loads ``a`` into a fresh
    SSA value that is NOT in ``_cpy_values`` (cpy-ness for names lives in
    ``_cpy_env_flags``), so ``_emit_binop_value``'s cpy branch missed it and the
    native ``+`` dispatch ran on the real CPython object. The augassign lowering
    now tags the loaded value cpy when the target name looks cpy, so the binop
    routes through ``py_cpy_binop``. Compile-only; ``import numpy`` marks
    ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "ag.py"
    out = tmp_path / "ag.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.ones(3)\n"
        "    a += 2\n"
        "    return a\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "py_cpy_binop" in ir, "a += 2 on a cpy name should route to py_cpy_binop"


def test_cpython_deep_chain_method_dispatches_via_libpython(tmp_path):
    """A method call on a DEEP CPython call chain must use libpython.

    Regression: ``numpy.arange(4).reshape(2, 2).sum()`` raised ``AttributeError:
    sum`` - the ``.sum()`` receiver is ``...reshape(2, 2)``, a ``Call`` whose
    callable is ``(Call).reshape`` (its ``cfunc.obj`` is a ``Call``, not a
    ``Name``), so the narrow Call-receiver fast path (which requires
    ``Name.attr``) missed it. The generalised receiver branch now includes
    ``Call`` and detects cpy via ``_expr_looks_cpython`` (which recurses through
    Call funcs), so arbitrarily deep cpy method chains route through libpython.
    Compile-only; ``import numpy`` marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dc.py"
    out = tmp_path / "dc.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    return numpy.arange(4).reshape(2, 2).sum()\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "@.cpy.attr.sum" in ir, "deep chain .sum() should use the libpython method path"
    assert "@.pyattr.sum" not in ir, "deep chain .sum() must NOT use native py_obj_getattr"


def test_cpython_value_binop_is_generic_not_numpy_specific(tmp_path):
    """The cpy operator routing is GENERIC across the C-extension ecosystem.

    Claim-hygiene guard: the binop fixes use cpy-value routing with NO
    package-name special-casing (no ``if package == "numpy"``). This exercises
    the SAME ``py_cpy_binop`` path on a DIFFERENT C-extension package,
    ``decimal.Decimal`` (stdlib ``_decimal``), so a future numpy-specific
    shortcut would not satisfy it. Compile-only; ``import decimal`` marks
    ``decimal`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dec.py"
    out = tmp_path / "dec.ll"
    src.write_text(
        "import decimal\n"
        "def f():\n"
        "    a = decimal.Decimal('10')\n"
        "    b = decimal.Decimal('3')\n"
        "    return a + b\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "py_cpy_binop" in ir, (
        "decimal.Decimal + must route to py_cpy_binop (generic cpy routing, "
        "not a numpy special-case)"
    )


def test_cpython_binop_receiver_attribute_dispatches_via_libpython(tmp_path):
    """Attribute LOAD on a BINARY-OP result must use libpython's getattr.

    Regression: ``(a + b).dtype`` on numpy arrays raised ``AttributeError:
    dtype`` - the attr-load lowering routed ``(Attr, Subscript, Call)`` cpy
    receivers through ``py_cpy_getattr`` but NOT ``BinOp``, so ``.dtype`` lowered
    to native ``py_obj_getattr`` on the real CPython binop result. Inline
    ``np.arange(5).shape`` (Call receiver) already worked; ``(a+b).dtype`` did
    not. The attr-load lowering now routes a ``BinOp`` receiver (cpy via
    ``_expr_looks_cpython``) through libpython. Compile-only; ``import numpy``
    marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "ba.py"
    out = tmp_path / "ba.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(3)\n"
        "    b = numpy.arange(3)\n"
        "    return (a + b).dtype\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "@.cpy.attr.dtype" in ir, "(a+b).dtype should use the libpython attr path"
    assert "py_cpy_getattr" in ir


def test_cpython_type_name_inline_dispatches_via_libpython(tmp_path):
    """Inline ``type(x).__name__`` on a cpy value must use libpython.

    Regression: ``type(a).__name__`` (inline) hit the native ``type(x).__name__``
    fast path, which calls ``py_obj_type_name`` using pcc's native type model -
    that mishandles a real CPython object and SILENTLY failed (no output). The
    stored form ``t = type(a); t.__name__`` worked. The native fast path now
    skips cpy args (``_expr_looks_cpython``), so inline ``type(a).__name__`` falls
    through to the cpy Call-receiver branch and routes via ``py_cpy_getattr`` on
    the real type object. Compile-only; ``import numpy`` marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "tn.py"
    out = tmp_path / "tn.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(3)\n"
        "    return type(a).__name__\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "@.cpy.attr.__name__" in ir, "inline type(a).__name__ should use py_cpy_getattr"
    assert "py_cpy_getattr" in ir


def test_cpython_list_of_cpy_values_builds_cpython_list(tmp_path):
    """A list literal containing cpy values must build a CPython list.

    Regression: ``numpy.concatenate([a, a])`` (a list whose elements are real
    CPython arrays) SILENTLY failed - the native pcc-list path bridged each cpy
    element to a pcc object (``_emit_value_as_pcc_object_or_bridge``), round-
    tripping a numpy array cpy->pcc->cpy and losing it. The list-literal lowering
    now routes a literal with ANY cpy element through ``_emit_cpython_list_ops``
    (builds a real CPython ``list`` and marshals each element - cpy borrowed,
    native converted). Compile-only; ``import numpy`` marks ``numpy`` cpy.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "lc.py"
    out = tmp_path / "lc.ll"
    src.write_text(
        "import numpy\n"
        "def f():\n"
        "    a = numpy.arange(3)\n"
        "    return numpy.concatenate([a, a])\n"
    , encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True, ir_scaffold_mode="auto", libpython_mode="on",
    )
    ir = out.read_text(encoding="utf-8")
    assert "cpy.list" in ir, "[a, a] of cpy values should build a CPython list (cpy.list)"
    assert "@.cpy.attr.append" in ir, "CPython list build should use list.append via libpython"
