"""The owned getcwd seam uses the maintained exact raw-machine contract.

These source/validator checks neither emit objects nor execute a runtime.
Target leaf ownership and errno behavior have separate provider witnesses.
"""

import ast
from pathlib import Path

import pytest

from pcc.frontends.python.pipeline_freestanding import (
    freestanding_allowed_external_symbols,
    freestanding_module_scope_extern_bindings,
    validate_freestanding_ir,
)
from pcc.frontends.python.pipeline_modes import PyPipelineError


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime/py"
GETCWD_IR = """define ptr @pcc_platform_getcwd(ptr %buffer, i64 %size) {
entry:
  %result = call ptr (ptr, i64) @getcwd(ptr %buffer, i64 %size)
  ret ptr %result
}
"""
ERRNO_IR = """define void @publish_errno(i32 %status) {
entry:
  call void (i32) @pcc_errno_set(i32 %status)
  ret void
}
"""


def test_actual_platform_caller_admits_exact_borrowed_buffer_contract():
    source = (RUNTIME / "freestanding_platform_fs.py").read_text()
    assert ("getcwd", "(c_ptr,c_size_t)", "c_ptr") in (
        freestanding_module_scope_extern_bindings(source)
    )
    allowed = freestanding_allowed_external_symbols(source)
    assert "getcwd" in allowed
    validate_freestanding_ir(GETCWD_IR, allowed)
    # Do not acquire admission by adding an unused unsafe.getcwd import.
    tree = ast.parse(source)
    unsafe_names = {alias.name for node in tree.body
                    if isinstance(node, ast.ImportFrom) and node.module == "pcc.unsafe"
                    for alias in node.names}
    assert "getcwd" not in unsafe_names


@pytest.mark.parametrize("parameters,result", (
    ("(c_ptr,c_int64)", "c_ptr"),
    ("(c_ptr,c_int32)", "c_ptr"),
    ("(c_obj,c_size_t)", "c_ptr"),
    ("(c_ptr,c_size_t)", "c_obj"),
    ("(c_ptr,c_size_t)", "c_int64"),
    ("(c_ptr,)", "c_ptr"),
    ("(c_ptr,c_size_t,c_ptr)", "c_ptr"),
))
def test_getcwd_rejects_unregistered_or_managed_source_shapes(parameters, result):
    source = "query = extern('getcwd', " + parameters + ", " + result + ")\n"
    allowed = freestanding_allowed_external_symbols(source)
    assert "getcwd" not in allowed
    with pytest.raises(PyPipelineError, match="outside its verified closure"):
        validate_freestanding_ir(GETCWD_IR, allowed)


@pytest.mark.parametrize("source", (
    "query = extern('getcwd_unregistered', (c_ptr,c_size_t), c_ptr)\n",
    "def local():\n    query = extern('getcwd', (c_ptr,c_size_t), c_ptr)\n",
    "class Local:\n    query = extern('getcwd', (c_ptr,c_size_t), c_ptr)\n",
    "# query = extern('getcwd', (c_ptr,c_size_t), c_ptr)\n",
    "query = extern('getcwd', (c_ptr,c_size_t), c_ptr, variadic=True)\n",
))
def test_getcwd_name_alone_does_not_admit_a_call(source):
    allowed = freestanding_allowed_external_symbols(source)
    assert "getcwd" not in allowed
    with pytest.raises(PyPipelineError, match="outside its verified closure"):
        validate_freestanding_ir(GETCWD_IR, allowed)


def test_getcwd_is_not_a_blanket_external_call_exception():
    source = "query = extern('getcwd', (c_ptr,c_size_t), c_ptr)\n"
    allowed = freestanding_allowed_external_symbols(source)
    with pytest.raises(PyPipelineError, match="outside its verified closure"):
        validate_freestanding_ir(GETCWD_IR.replace("@getcwd(", "@unregistered_os_call("), allowed)
    with pytest.raises(PyPipelineError, match="outside its verified closure"):
        validate_freestanding_ir(GETCWD_IR)


def test_owned_target_leaf_exports_match_the_registered_wire_contract():
    for filename in ("freestanding_linux_libc.py", "freestanding_windows.py"):
        tree = ast.parse((RUNTIME / filename).read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "getcwd_c")
        export = function.decorator_list[0]
        assert export.func.id == "c_abi_typed_export"
        assert tuple(ast.literal_eval(value) for value in export.args) == (
            "getcwd", "ptr", ("ptr", "u64"),
        )


def test_actual_windows_errno_binding_uses_existing_canonical_i32_contract():
    source = (RUNTIME / "freestanding_windows.py").read_text()
    assert ("pcc_errno_set", "(c_int32,)", "c_void") in (
        freestanding_module_scope_extern_bindings(source)
    )
    allowed = freestanding_allowed_external_symbols(source)
    assert "pcc_errno_set" in allowed
    validate_freestanding_ir(ERRNO_IR, allowed)
    # c_int shares an LLVM width, but the strict source contract is canonical.
    wrong = "set_errno = extern('pcc_errno_set', (c_int,), c_void)\n"
    wrong_allowed = freestanding_allowed_external_symbols(wrong)
    assert "pcc_errno_set" not in wrong_allowed
    with pytest.raises(PyPipelineError, match="outside its verified closure"):
        validate_freestanding_ir(ERRNO_IR, wrong_allowed)
