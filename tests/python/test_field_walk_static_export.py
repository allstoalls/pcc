"""Standalone consumers must retain the field walk's native result domain."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("ir_scaffold_mode", ("off", "on"))
@pytest.mark.parametrize("module_name", (
    "pcc.frontends.python.codegen.class_gen",
    "pcc.frontends.python.type_infer",
))
def test_standalone_field_walk_import_iteration_and_field_access_are_native(
    tmp_path, module_name, ir_scaffold_mode,
):
    from pcc.diagnostics.ir_diff import IrSummary
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.type_infer import infer_module
    from scripts.probe_fallback_categories import _scan

    source = """
def count_values(body) -> int:
    from pcc.frontends.python.pipeline_exports import instance_field_assignment_statements
    total = 0
    for stmt in instance_field_assignment_statements(body):
        if stmt.value is not None:
            total += 1
    return total
""".lstrip()
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")
    typed = infer_module(parse_and_lift(source, str(path), module_name))
    ir_text = str(L1CodeGen(
        typed, emit_cpy_main_exitcode=False, ir_scaffold_mode=ir_scaffold_mode,
    ).generate(typed))
    path.with_suffix(".ll").write_text(ir_text, encoding="utf-8")
    function = IrSummary.parse(ir_text).functions[
        "user_" + module_name.replace(".", "_") + "_count_values"
    ]
    assert "user_pcc_frontends_python_pipeline_exports_instance_field_assignment_statements" in function.calls
    assert _scan(ir_text)["actions_total"] == 0
    assert "strict.nolib.stub" not in ir_text
