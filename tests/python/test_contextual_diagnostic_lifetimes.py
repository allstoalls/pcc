"""Bound contextual diagnostic retention without changing fallback counts."""
from types import SimpleNamespace
import weakref

import pytest

from pcc.frontends.python import pipeline_context


@pytest.mark.parametrize("separator", [
    "\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
])
def test_fallback_counter_preserves_splitlines_boundaries(separator):
    text = separator.join([
        "declare ptr @py_cpy_import(ptr)",
        "call ptr @py_cpy_a(); call ptr @py_cpy_b()",
        "@py_cpy_reversed call ptr elsewhere",
        "call ",
        "@py_cpy_on_another_line",
        "",
    ])
    assert pipeline_context.count_py_cpy_fallback_calls(text) == 2


def test_fallback_counter_does_not_materialize_a_line_list():
    class NoLineList(str):
        def splitlines(self, *args, **kwargs):
            raise AssertionError("whole-IR line materialization")

    text = NoLineList("declare @py_cpy_a\n" * 1000 + "call @py_cpy_b call @py_cpy_c\n")
    assert pipeline_context.count_py_cpy_fallback_calls(text) == 1
    assert pipeline_context.count_py_cpy_fallback_calls("") == 0
    assert pipeline_context.count_py_cpy_fallback_calls("call @py_cpy_a\x1f@py_cpy_b call ") == 1


@pytest.mark.parametrize("first_fails", [False, True])
def test_contextual_loop_releases_completed_codegen(first_fails, tmp_path, monkeypatch):
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen import layer1

    references = []

    class Codegen:
        def __init__(self, module, **kwargs):
            assert all(reference() is None for reference in references)
            self.ast_module = module
            self._native_module_exports = {}
            references.append(weakref.ref(self))

        def generate(self, module):
            if first_fails and module.name == "first":
                raise ValueError("preserved failure")
            return "declare ptr @py_cpy_import(ptr)\n"

    def count_after_release(text):
        assert all(reference() is None for reference in references)
        return 0

    monkeypatch.setattr(layer1, "L1CodeGen", Codegen)
    monkeypatch.setattr(type_infer, "infer_module", lambda module, **kwargs: module)
    monkeypatch.setattr(pipeline_context, "build_closed_world_context", lambda *args, **kwargs: (
        [SimpleNamespace(name="first"), SimpleNamespace(name="second")], {}, {},
    ))
    monkeypatch.setattr(pipeline_context, "_contextual_host_params_for_module", lambda *args: None)
    monkeypatch.setattr(pipeline_context, "count_py_cpy_fallback_calls", count_after_release)
    result = pipeline_context.compile_contextual_per_module_fallback_counts(
        ["first.py", "second.py"], ["first", "second"], ["first", "second"],
        ir_scaffold_mode="on", emit_ir_dir=str(tmp_path),
    )
    assert result == {"first": -1 if first_fails else 0, "second": 0}
    assert all(reference() is None for reference in references)
    if first_fails:
        assert (tmp_path / "first.error.txt").read_text() == "ValueError: preserved failure\n"
    else:
        assert (tmp_path / "first.ll").read_text() == "declare ptr @py_cpy_import(ptr)\n"
