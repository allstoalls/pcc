"""Native closure guard retained after denying the common/cold walker split."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path


_REPO_ROOT = Path(__file__).absolute().parents[2]
_MODULE = "pcc.frontends.python.codegen.hoist_free_names"


def _load_probe_module():
    path = _REPO_ROOT / "scripts" / "probe_stage1_closure.py"
    spec = importlib.util.spec_from_file_location("_hoist_shape_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compiled_free_name_analysis_stays_in_closed_world(tmp_path):
    from pcc.frontends.python.pipeline import (
        compile_contextual_per_module_fallback_counts,
    )

    probe = _load_probe_module()
    # The public CLI is now a two-module lazy entry, so its shallow closure
    # does not contain this analysis module. Build the actual subject's
    # same-package import context and require membership before compiling.
    entry = _REPO_ROOT / "pcc/frontends/python/codegen/hoist_free_names.py"
    srcs, mods = probe._tightened_closure(str(entry))
    assert _MODULE in mods
    ir_dir = tmp_path / "ir"
    ir_dir.mkdir()
    counts = compile_contextual_per_module_fallback_counts(
        srcs,
        mods,
        {_MODULE},
        ir_scaffold_mode="on",
        strict_no_libpython=True,
        emit_ir_dir=str(ir_dir),
    )
    assert counts == {_MODULE: 0}

    ir = (ir_dir / "pcc_frontends_python_codegen_hoist_free_names.ll").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"\bcall [^\n]*@py_obj_call\(", ir)
    assert "ptr %callback" not in ir


def test_span_ty_fields_can_never_carry_expressions():
    """The free-name walk skips fields literally named span/ty (metadata).

    That skip is sound only while no py_ast dataclass declares a span/ty
    field that can hold an expression.  Pin the contract: span is always a
    SourceSpan record and ty is always a Type record, never Expr.
    """
    import inspect
    from dataclasses import fields, is_dataclass

    from pcc.frontends.python import py_ast

    seen = 0
    for name in dir(py_ast):
        obj = getattr(py_ast, name)
        if not (inspect.isclass(obj) and is_dataclass(obj)):
            continue
        for field in fields(obj):
            if field.name == "span":
                seen += 1
                assert str(field.type) == "SourceSpan", (name, field.type)
            elif field.name == "ty":
                seen += 1
                assert str(field.type) == "Type", (name, field.type)
    assert seen >= 60
