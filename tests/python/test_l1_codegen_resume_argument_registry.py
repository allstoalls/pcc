"""The generator helper's resumable-argument ABI survives native export."""
from __future__ import annotations

from inspect import signature
import re

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.test_shared_call_binding import _function


def test_generator_yield_resume_argument_target_native_call():
    # Bind the real callable as well as compiling the exported caller: an
    # entry existing in the registry is insufficient if its ABI is stale.
    bound = signature(L1CodeGen._emit_generator_yield_value).bind(
        object(), object(), resume_err_target=None,
        resume_args_target=object(), result_slot=None,
    )
    assert "resume_args_target" in bound.arguments
    source = '''from pcc.frontends.python.codegen.layer1 import L1CodeGen
def probe(codegen: L1CodeGen, value, error_target, args_target, output):
    return codegen._emit_generator_yield_value(
        value, resume_err_target=error_target,
        resume_args_target=args_target, result_slot=output)
'''
    exports = _default_native_module_exports("pcc.frontends.python.codegen.layer1")
    module = infer_module(parse_and_lift(source, "binding.py", "binding"), external_exports=exports)
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    text = str(codegen.generate(module))
    body = _function(text)
    symbol = "user_pcc_frontends_python_codegen_layer1_L1CodeGen__emit_generator_yield_value"
    declaration = re.search(r"declare [^\n]*@" + symbol + r"\(([^\n]*)\)", text)
    assert declaration is not None
    assert len(declaration.group(1).split(",")) == 5
    # Keywords in formal order bind statically: the caller passes all five
    # formals to the declared helper, so the declaration must include the
    # real helper's resumable-argument parameter.
    call = re.search(r"\bcall [^\n]*@" + symbol + r"\(([^\n]*)\)", body)
    assert call is not None
    assert len(call.group(1).split(", ")) == 5
    assert "@py_obj_call_slots(" not in body
    assert "@py_cpy_" not in body
