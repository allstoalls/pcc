"""The compiler's dynamic container guard must survive native self compilation."""

from pathlib import Path
import re
import pytest

from pcc.parse.py_lift import parse_and_lift
from pcc.py_frontend.type_infer import infer_module
from pcc.py_frontend.codegen.layer1 import L1CodeGen
from pcc.py_frontend.codegen.layer1_support import _PCC_FRONTEND_STATIC_NATIVE_EXPORTS


@pytest.mark.parametrize("module,method,callee", [
    ("dyn_method_guard", "_emit_dyn_container_method_with_tag_guard",
     "_emit_generic_dyn_method_call_on_value"),
    ("list_method_lowering", "_emit_generic_dyn_method_call_on_value",
     "_emit_dynamic_call_args_tuple"),
])
def test_dynamic_container_guard_has_native_host_calls(module, method, callee):
    # These are the existing L1CodeGen forward declarations. An isolated
    # mixin has no sibling class schema and is not a self-host probe.
    source = Path(__file__).resolve().parents[2] / "pcc/py_frontend/codegen" / (module + ".py")
    name = "pcc.py_frontend.codegen." + module
    typed = infer_module(
        parse_and_lift(source.read_text(), str(source), name),
        external_exports=_PCC_FRONTEND_STATIC_NATIVE_EXPORTS,
    )
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = dict(_PCC_FRONTEND_STATIC_NATIVE_EXPORTS)
    text = str(codegen.generate(typed))
    body = re.search(
        r"(?m)^define[^\n]*@user_pcc_py_frontend_codegen_" + module
        + r"_[^ (]+_" + method + r"\([^\n]*\{\n([\s\S]*?)^\}",
        text,
    )
    assert body is not None
    assert "strict.nolib.stub" not in body[1]
    assert not re.search(r"call[^\n]*@py_cpy_", body[1])
    assert callee in body[1]
