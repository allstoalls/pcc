from __future__ import annotations


def test_tailcall_is_wired_into_ir_pass_pipeline(monkeypatch):
    import pcc  # noqa: F401 - installs deepwire
    from pcc.frontends.python import ir_pass_pipeline

    monkeypatch.setenv("PCC_ENABLE_TAILCALL_REWRITE", "1")
    ir = '''define void @f() {
entry:
  call void @f()
  ret void
}
'''
    out = ir_pass_pipeline.run_python_ir_pass_pipeline(
        ir, pass_names=(), module_name="m"
    )
    assert "pcc.tailcall.self" in out


def test_pass_explain_does_not_crash_for_empty_pipeline(monkeypatch, capsys):
    import pcc  # noqa: F401
    from pcc.frontends.python import ir_pass_pipeline

    monkeypatch.setenv("PCC_PASSES_EXPLAIN", "1")
    ir = 'define void @f() {\nentry:\n  ret void\n}\n'
    out = ir_pass_pipeline.run_python_ir_pass_pipeline(ir, pass_names=(), module_name="m")
    assert out == ir
    captured = capsys.readouterr()
    assert "pcc.diagnostics.pass_explain" in captured.err
