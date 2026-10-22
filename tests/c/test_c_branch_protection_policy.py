from __future__ import annotations

import inspect

from pcc.ir import ir

from pcc.frontends.c.codegen import c_codegen
from pcc.ir.compat import add_raw_function_attribute
from pcc.frontends.c.parse.c_parser import CParser


def test_postprocess_ir_text_dispatches_only_varargs_rewrite():
    postprocess_source = inspect.getsource(c_codegen.postprocess_ir_text)
    report_source = inspect.getsource(c_codegen.postprocess_ir_text_with_report)

    assert "_postprocess_varargs_ir" in postprocess_source
    assert "_postprocess_varargs_ir" in report_source
    assert "branch_protection" not in postprocess_source
    assert "branch_protection" not in report_source
    assert not hasattr(c_codegen, "_postprocess_aarch64_branch_protection_ir")


def test_aarch64_branch_protection_is_attached_during_c_ir_construction():
    generator = c_codegen.CCodeGenerator()
    generator.module.triple = "arm64-apple-darwin23.6.0"
    generator.generate_code(CParser().parse("int f(int x) { return x + 1; }"))

    raw_ir = str(generator.module)
    assert '"branch-target-enforcement"' in raw_ir
    assert '"sign-return-address"="non-leaf"' in raw_ir
    assert '"sign-return-address-key"="a_key"' in raw_ir
    assert c_codegen.postprocess_ir_text(raw_ir) == raw_ir
