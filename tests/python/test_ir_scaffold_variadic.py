"""Phase 3 Tasks 11/13/15/16: bespoke handlers for variadic IRBuilder
methods.

Covered:
- ``builder.call(fn, [args])`` → ``user_pcc_ir_ir_IRBuilder_call<N>`` per arity
- ``builder.gep(ptr, [indices])`` → ``user_pcc_ir_ir_IRBuilder_gep<N>``
- ``builder.phi(ty)`` → ``user_pcc_ir_ir_IRBuilder_phi(builder, ty)``
- ``builder.landingpad(ty)`` → ``user_pcc_ir_ir_IRBuilder_landingpad(builder, ty)``

Negative cases:
- Non-literal args list to ``call`` / ``gep`` raises with a clear
  message (per-file migration tells you to inline the list).
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)
_PTR = r"(?:ptr|i8\s*\*)"


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    # These cases exercise methods on an owned builder. A parameter merely
    # named "builder" is an unknown user receiver and must not be scaffolded.
    if re.search(r"(?m)^def f\(builder(?:,|\))", source):
        source = "from pcc.ir.compat import ir\n" + source
        source = re.sub(r"(?m)^def f\(builder(?:, )?", "def f(", source)
        source = re.sub(
            r"(?m)^(def f\([^\n]*\):\n)",
            r"\1    builder = ir.IRBuilder()\n",
            source,
            count=1,
        )
    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


# -- call --------------------------------------------------------------

def test_call_zero_args():
    program = textwrap.dedent(
        """
        def f(builder, fn):
            return builder.call(fn, [])
        """
    )
    ir_text = _compile_to_ll(program, "v_call0", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_call0" in ir_text
    body = _function_body(ir_text, "f")
    assert body is not None and "py_cpy_" not in body


def test_call_three_args():
    program = textwrap.dedent(
        """
        def f(builder, fn, a, b, c):
            return builder.call(fn, [a, b, c])
        """
    )
    ir_text = _compile_to_ll(program, "v_call3", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_call3" in ir_text
    body = _function_body(ir_text, "f")
    assert body is not None and "py_cpy_" not in body


def test_call_eight_args_has_runtime_provider():
    """The largest owned C-ABI shim currently emits eight fixed args."""
    program = textwrap.dedent(
        """
        def f(builder, fn, a, b, c, d, e, f_arg, g, h):
            return builder.call(fn, [a, b, c, d, e, f_arg, g, h])
        """
    )
    ir_text = _compile_to_ll(program, "v_call8", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_call8" in ir_text
    body = _function_body(ir_text, "f")
    assert body is not None and "py_cpy_" not in body
    assert "def IRBuilder_call8(" in (
        _REPO_ROOT / "pcc" / "ir" / "ir.py"
    ).read_text(encoding="utf-8")


def test_call_with_name_kwarg_still_lowers():
    program = textwrap.dedent(
        """
        def f(builder, fn, a):
            return builder.call(fn, [a], name="result")
        """
    )
    ir_text = _compile_to_ll(program, "v_call_named", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_call1" in ir_text


@pytest.mark.parametrize(
    "case,call_text,extern",
    [
        ("kw", 'builder.call(fn, [a], name="resultname")', "call1"),
        ("pos", 'builder.call(fn, [a], "resultname")', "call1"),
        ("dyn", 'builder.call(fn, list(args), name="resultname")', "call_dyn"),
    ],
)
def test_call_passes_the_ssa_name_to_the_native_builder(case, call_text, extern):
    """The name reaches ``IRBuilder_call<N>``: pcc1 used to drop it, so every
    call result it emitted was anonymous and its IR differed from the host
    compiler's for the same module."""
    program = textwrap.dedent(
        f"""
        def f(builder, fn, a, args):
            return {call_text}
        """
    )
    ir_text = _compile_to_ll(program, "v_call_name_" + case, mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None, ir_text
    call_line = next(
        line for line in body.splitlines()
        if "@user_pcc_ir_ir_IRBuilder_" + extern + "(" in line
    )
    # Static str objects hold their bytes hex-escaped.
    escaped = "".join("\\%02X" % byte for byte in b"resultname")
    name_global = re.search(
        r"@(\.pystr\.obj\.\d+)\s*=[^\n]*c\"" + re.escape(escaped) + r"\\00\"",
        ir_text,
    )
    assert name_global is not None, ir_text
    name_handle = call_line.rsplit("ptr ", 1)[1].rstrip(")").strip()
    # The static str reaches the call directly or through an identity cast.
    if name_handle.startswith("@"):
        handle_def = name_handle
    else:
        handle_def = next(
            line for line in body.splitlines()
            if line.strip().startswith(name_handle + " = ")
        )
    assert "@" + name_global.group(1) in handle_def, (call_line, handle_def)
    # builder, fn, one argument (or the dynamic list), name
    arity = 4
    decl = re.search(
        r"declare[^\n]*@user_pcc_ir_ir_IRBuilder_" + extern + r"\(([^)]*)\)",
        ir_text,
    )
    assert decl is not None and len(decl.group(1).split(",")) == arity, ir_text


def test_call_dynamic_args_list_uses_dyn_fallback():
    """Non-literal args list lowers via the ``call_dyn`` fallback —
    the dispatch is still static (extern call), but the args list
    construction may still use ``py_cpy_*``.

    This is the per-file migration relief valve: a function with a
    runtime-built args list still gets static dispatch, even though
    full elimination requires inlining the list at the call site.
    """
    program = textwrap.dedent(
        """
        def f(builder, fn, args):
            return builder.call(fn, args)
        """
    )
    ir_text = _compile_to_ll(program, "v_call_dyn", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_call_dyn" in ir_text, (
        "non-literal args must lower via call_dyn fallback:\n"
        + ir_text
    )


# -- gep ---------------------------------------------------------------

def test_gep_two_indices():
    program = textwrap.dedent(
        """
        def f(builder, ptr, i0, i1):
            return builder.gep(ptr, [i0, i1])
        """
    )
    ir_text = _compile_to_ll(program, "v_gep2", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_gep2" in ir_text
    body = _function_body(ir_text, "f")
    assert body is not None and "py_cpy_" not in body


@pytest.mark.parametrize(
    "case,gep_text,extern,arity",
    [
        ("lit", 'builder.gep(ptr, [i0, i1], inbounds=True, name="gepname")',
         "gep2_inbounds", 5),
        ("dyn", 'builder.gep(ptr, list(indices), name="gepname")', "gep_dyn", 4),
    ],
)
def test_gep_passes_the_ssa_name_to_the_native_builder(case, gep_text, extern, arity):
    """Like ``builder.call``: pcc1 dropped ``name=`` and emitted every GEP
    anonymous (``%.130``) where the host compiler named it."""
    program = textwrap.dedent(
        f"""
        def f(builder, ptr, i0, i1, indices):
            return {gep_text}
        """
    )
    ir_text = _compile_to_ll(program, "v_gep_name_" + case, mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None, ir_text
    gep_line = next(
        line for line in body.splitlines()
        if "@user_pcc_ir_ir_IRBuilder_" + extern + "(" in line
    )
    escaped = "".join("\\%02X" % byte for byte in b"gepname")
    name_global = re.search(
        r"@(\.pystr\.obj\.\d+)\s*=[^\n]*c\"" + re.escape(escaped) + r"\\00\"",
        ir_text,
    )
    assert name_global is not None, ir_text
    name_handle = gep_line.rsplit("ptr ", 1)[1].rstrip(")").strip()
    if name_handle.startswith("@"):
        handle_def = name_handle
    else:
        handle_def = next(
            line for line in body.splitlines()
            if line.strip().startswith(name_handle + " = ")
        )
    assert "@" + name_global.group(1) in handle_def, (gep_line, handle_def)
    # builder, ptr, indices (or the dynamic list), name
    decl = re.search(
        r"declare[^\n]*@user_pcc_ir_ir_IRBuilder_" + extern + r"\(([^)]*)\)",
        ir_text,
    )
    assert decl is not None and len(decl.group(1).split(",")) == arity, ir_text


def test_gep_zero_indices():
    program = textwrap.dedent(
        """
        def f(builder, ptr):
            return builder.gep(ptr, [])
        """
    )
    ir_text = _compile_to_ll(program, "v_gep0", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_gep0" in ir_text


def test_gep_dynamic_indices_inbounds_uses_exported_helper():
    program = textwrap.dedent(
        """
        def f(builder, ptr, indices):
            return builder.gep(ptr, indices, inbounds=True)
        """
    )
    ir_text = _compile_to_ll(program, "v_gep_dyn_inbounds", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@user_pcc_ir_ir_IRBuilder_gep_dyn_inbounds" in body, body
    assert "py_cpy_" not in body, body

    helper_source = (_REPO_ROOT / "pcc" / "ir" / "ir.py").read_text(
        encoding="utf-8"
    )
    assert "def IRBuilder_gep_dyn_inbounds" in helper_source


# -- phi ---------------------------------------------------------------

def test_phi_one_arg():
    program = textwrap.dedent(
        """
        def f(builder, ty):
            return builder.phi(ty)
        """
    )
    ir_text = _compile_to_ll(program, "v_phi", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_phi" in ir_text
    body = _function_body(ir_text, "f")
    assert body is not None and "py_cpy_" not in body


def test_switch_lowers_to_irbuilder_scaffold():
    program = textwrap.dedent(
        """
        def f(builder, value, default_block):
            return builder.switch(value, default_block)
        """
    )
    ir_text = _compile_to_ll(program, "v_switch", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@user_pcc_ir_ir_IRBuilder_switch" in body, body
    assert "py_cpy_" not in body, body


def test_switch_add_case_lowers_to_switchinstr_scaffold():
    program = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        def f(value, target):
            builder = ir.IRBuilder()
            switch_inst = builder.switch(value, target)
            switch_inst.add_case(value, target)
        """
    )
    ir_text = _compile_to_ll(program, "v_switch_add_case", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@user_pcc_ir_ir_SwitchInstr_add_case" in body, body
    assert "py_cpy_" not in body, body


def test_switch_add_case_constant_i64_uses_i64_scaffold():
    program = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        _I64 = ir.IntType(64)

        def f(value: int, target):
            builder = ir.IRBuilder()
            switch_inst = builder.switch(ir.Constant(_I64, value), target)
            switch_inst.add_case(ir.Constant(_I64, value), target)
        """
    )
    ir_text = _compile_to_ll(program, "v_switch_add_case_i64", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert (
        "@user_pcc_ir_ir_scaffold_SwitchInstr_add_case_i64" in body
    ), body
    assert "py_cpy_" not in body, body


# -- append_basic_block (variable arity, unambiguous receiver) -----

def test_append_basic_block_zero_args_with_builder():
    program = textwrap.dedent(
        """
        def f(builder):
            return builder.append_basic_block()
        """
    )
    ir_text = _compile_to_ll(program, "v_abb_0_b", mode="on")
    assert (
        "@user_pcc_ir_ir_scaffold_IRBuilder_append_basic_block"
        in ir_text
    )
    body = _function_body(ir_text, "f")
    assert body is not None and "py_cpy_" not in body


def test_append_basic_block_with_name_arg():
    program = textwrap.dedent(
        """
        def f(builder, name: str):
            return builder.append_basic_block(name)
        """
    )
    ir_text = _compile_to_ll(program, "v_abb_named_b", mode="on")
    assert (
        "@user_pcc_ir_ir_scaffold_IRBuilder_append_basic_block"
        in ir_text
    )


def test_append_basic_block_on_function_receiver():
    """Issue 11.B: receiver may be a Function instance, not just a
    builder. The unambiguous-method match catches this."""
    program = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        def f(module, fn_type):
            some_function = ir.Function(module, fn_type)
            return some_function.append_basic_block()
        """
    )
    ir_text = _compile_to_ll(program, "v_abb_func_recv", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert (
        "@user_pcc_ir_ir_scaffold_Function_append_basic_block"
        in body
    ), body
    assert "py_cpy_" not in body, body


def test_add_incoming_on_phi_receiver():
    program = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        def f(ty, val, blk):
            builder = ir.IRBuilder()
            phi = builder.phi(ty)
            phi.add_incoming(val, blk)
        """
    )
    ir_text = _compile_to_ll(program, "v_addinc_phi", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@user_pcc_ir_ir_IRBuilder_add_incoming" in body, body
    assert "py_cpy_" not in body, body


def test_as_pointer_on_type_receiver():
    program = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        def f():
            ty = ir.IntType(64)
            return ty.as_pointer()
        """
    )
    ir_text = _compile_to_ll(program, "v_aspt", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@user_pcc_ir_ir_IRBuilder_as_pointer" in body, body
    assert "py_cpy_" not in body, body


# -- landingpad --------------------------------------------------------

def test_landingpad_one_arg():
    program = textwrap.dedent(
        """
        def f(builder, ty):
            return builder.landingpad(ty)
        """
    )
    ir_text = _compile_to_ll(program, "v_landingpad", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_landingpad" in ir_text
    body = _function_body(ir_text, "f")
    assert body is not None and "py_cpy_" not in body


@pytest.mark.parametrize(
    "case,call_text,extern",
    [
        ("load", 'builder.load(ptr, name="x", align=1)', "load"),
        ("store", "builder.store(value, ptr, align=1)", "store"),
    ],
)
def test_int_optional_params_reach_the_native_builder_boxed(case, call_text, extern):
    """``align=1`` is a Python int to the native IRBuilder; the bit-preserving
    ``inttoptr 1`` handle read as the tagged int 0 there, so pcc1 emitted no
    ``, align 1`` where the host compiler did."""
    program = textwrap.dedent(
        f"""
        def f(builder, ptr, value):
            return {call_text}
        """
    )
    ir_text = _compile_to_ll(program, "v_align_" + case, mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None, ir_text
    call_line = next(
        line for line in body.splitlines()
        if "@user_pcc_ir_ir_IRBuilder_" + extern + "(" in line
    )
    align_handle = call_line.rsplit("ptr ", 1)[1].rstrip(")").strip()
    handle_def = next(
        line for line in body.splitlines()
        if line.strip().startswith(align_handle + " = ")
    )
    assert "@py_int_from_i64(i64 1)" in handle_def, (call_line, handle_def)
