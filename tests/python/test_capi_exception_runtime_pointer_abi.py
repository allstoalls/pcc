"""The runtime's implicit C pointer ABI is not an explicit raw Python view."""

from pathlib import Path
import re

import pytest

from pcc.frontends.python.owned_runtime_build import _compile_runtime_module
from pcc.frontends.python.pipeline import compile_python
from tests.owned_ir_validation import verify_ir_text


ROOT = Path(__file__).resolve().parents[2]
TARGET = "x86_64-unknown-linux-gnu"


def test_capi_exception_runtime_compiles_through_owned_builder(tmp_path):
    output = tmp_path / "py_capi_exc_runtime.ll"
    _compile_runtime_module(
        "py_capi_exc_runtime",
        str(ROOT / "pcc/runtime/py/py_capi_exc_runtime.py"),
        str(output),
        TARGET,
    )
    text = output.read_text()
    verify_ir_text(text)
    for symbol, parameters in (
        ("pcc_capi_exception_class", r"ptr %type"),
        ("PyErr_SetString", r"ptr %type, ptr %message"),
        ("PyErr_SetNone", r"ptr %type"),
        ("PyErr_SetObject", r"ptr %type, ptr %value"),
    ):
        assert re.search(r"^define[^\n]*@" + symbol + r"\(" + parameters + r"\)", text, re.M)
    for helper in ("_capi_is_unicode_sentinel", "_capi_is_unicode_encode_type",
                   "_capi_set_unicode_encode_value"):
        match = re.search(r"^define[^\n]*@[^\n]*" + helper + r"\(ptr %\w+\)[^\n{]*\{\n(.*?)^}", text, re.M | re.S)
        assert match, helper
        # Production runtime libraries own their roots explicitly. Passing a
        # sentinel through a helper must not synthesize a managed root/retain.
        assert not re.search(r"\bcall\b[^\n]*@pcc_gc_(?!safepoint)", match.group(1))
    assert re.search(r"\bcall\b[^\n]*@py_unicode_encode_error_normalize\(ptr", text)


@pytest.mark.parametrize("runtime_port", [False, True], ids=["application", "runtime-port"])
def test_unproven_pointer_argument_reports_source_location(tmp_path, runtime_port):
    source = tmp_path / "unproven_pointer.py"
    directive = "__pcc_runtime_port__ = True" if runtime_port else "# Application module"
    source.write_text(
        "from pcc.extern import c_abi_typed_export, c_ptr\n"
        f"{directive}\n"
        "def consume(value: c_ptr) -> None:\n"
        "    pass\n"
        "@c_abi_typed_export('unproven_pointer_entry', 'void', ('ptr',))\n"
        "def reject(value) -> None:\n"
        "    consume(value)\n"
    )
    with pytest.raises(NotImplementedError, match="raw pointer cannot cross") as error:
        compile_python(str(source), str(tmp_path / "unproven.ll"),
                       emit_llvm_only=True, python_library=True,
                       libpython_mode="off", backend="self", target_triple=TARGET)
    assert str(source) + ":7:" in str(error.value)
