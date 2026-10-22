from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).absolute().parents[2]


def test_all_owned_ir_pass_modules_emit_ir_in_strict_frontend_mode(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    pass_dir = _REPO_ROOT / "pcc" / "ir" / "optimization"
    modules = tuple(sorted(pass_dir.glob("*.py")))
    assert modules, "owned optimization module inventory is empty: " + str(pass_dir)
    failures: list[str] = []
    for src in modules:
        out = tmp_path / f"{src.stem}.ll"
        try:
            compile_python(
                str(src),
                str(out),
                emit_llvm_only=True,
                libpython_mode="off",
                ir_scaffold_mode="on",
                backend="self",
            )
        except Exception as exc:
            failures.append(f"{src.name}: {type(exc).__name__}: {exc}")
            continue
        if not out.exists():
            failures.append(f"{src.name}: did not write LLVM IR")

    assert not failures, "\n".join(failures)
