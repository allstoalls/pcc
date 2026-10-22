from __future__ import annotations


VOID_SELF_RECURSION_IR = """\
define void @spin() {
entry:
  call void @spin()
  ret void
}
"""


def test_tailcall_pass_rewrites_through_real_pipeline(monkeypatch):
    # Importing pcc installs roadmap_deepwire from pcc.__init__, which wraps the
    # real pass-pipeline entry point.  This test intentionally calls that entry
    # point instead of pcc.ir.optimization.tailcall_ir directly.
    import pcc  # noqa: F401
    from pcc.frontends.python import ir_pass_pipeline

    monkeypatch.delenv("PCC_DISABLE_ROADMAP_DEEPWIRE", raising=False)
    out = ir_pass_pipeline.run_python_ir_pass_pipeline(
        VOID_SELF_RECURSION_IR,
        pass_names=("tailcall",),
        module_name="tailcall_only",
    )

    assert "; pcc.tailcall.self\n  br label %entry" in out
    assert "pcc.tailcall.preheader:" in out
    assert "call void @spin()" not in out


def test_tailcall_pass_explain_reports_tailcall_decision(tmp_path, monkeypatch):
    import pcc  # noqa: F401
    from pcc.frontends.python import ir_pass_pipeline

    explain_path = tmp_path / "passes.txt"
    monkeypatch.setenv("PCC_PASSES_EXPLAIN", "1")
    monkeypatch.setenv("PCC_PASSES_EXPLAIN_PATH", str(explain_path))

    out = ir_pass_pipeline.run_python_ir_pass_pipeline(
        VOID_SELF_RECURSION_IR,
        pass_names=("tailcall",),
        module_name="tailcall_explain",
    )

    assert "pcc.tailcall.self" in out
    text = explain_path.read_text(encoding="utf-8")
    assert "tailcall:" in text
    assert "ran" in text
    assert "spin" in text
    assert "rewrote" in text
