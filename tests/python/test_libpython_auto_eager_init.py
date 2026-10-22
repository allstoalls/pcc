"""Main-thread CPython initialization for late ``auto`` fallbacks."""

from __future__ import annotations

import textwrap


def _main_ir(ir_text: str) -> str:
    start = ir_text.index("define i32 @main(")
    end = ir_text.index("\n}", start)
    return ir_text[start:end]


def _inject_late_worker_fallback(ir_text: str) -> str:
    """Model a fallback introduced after AST import classification."""
    marker = "call void @py_cpy_ensure_init()"
    if marker in ir_text:
        return ir_text
    lines = ir_text.splitlines(keepends=True)
    in_worker = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped.startswith("define ")
            and "@user_" in stripped
            and "_worker(" in stripped
        ):
            in_worker = True
            continue
        if in_worker and stripped == "entry:":
            lines.insert(index + 1, "  " + marker + "\n")
            return "".join(lines)
        if in_worker and stripped == "}":
            break
    raise AssertionError("generated worker function not found")


def test_auto_late_worker_fallback_initializes_cpython_in_main(
    tmp_path, monkeypatch
):
    """A worker-only fallback must not win CPython initialization."""
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    src = tmp_path / "late_worker.py"
    out = tmp_path / "late_worker.ll"
    src.write_text(
        textwrap.dedent(
            """
            from threading import Thread

            def worker(value):
                value.__copy__()

            def main() -> None:
                thread = Thread(target=worker, args=([1],))
                thread.start()
                thread.join()

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    ast_decisions = []
    real_ast_detector = pipeline._module_needs_libpython
    real_generate = L1CodeGen.generate

    def record_ast_decision(*args, **kwargs):
        decision = real_ast_detector(*args, **kwargs)
        ast_decisions.append(decision)
        return decision

    def inject_late_fallback(self, *args, **kwargs):
        return _inject_late_worker_fallback(
            real_generate(self, *args, **kwargs)
        )

    monkeypatch.setattr(
        pipeline,
        "_module_needs_libpython",
        record_ast_decision,
    )
    monkeypatch.setattr(
        L1CodeGen,
        "generate",
        inject_late_fallback,
    )

    pipeline.compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="auto",
    )

    ir_text = out.read_text(encoding="utf-8")
    main_ir = _main_ir(ir_text)
    assert ast_decisions == [False]
    assert pipeline._ir_needs_libpython(ir_text)
    assert ir_text.count("call void @py_cpy_ensure_init") >= 2
    assert main_ir.count("call void @py_cpy_ensure_init") == 1
    assert main_ir.index("call void @py_set_program_args") < main_ir.index(
        "call void @py_cpy_ensure_init"
    )
    assert main_ir.index("call void @py_cpy_ensure_init") < main_ir.index(
        "call void @user_late_worker_main"
    )


def test_auto_without_fallback_does_not_initialize_cpython(tmp_path):
    """The late-init repair must preserve ``auto`` as an opt-in fallback."""
    from pcc.frontends.python import pipeline

    src = tmp_path / "native_only.py"
    out = tmp_path / "native_only.ll"
    src.write_text("print('native')\n", encoding="utf-8")

    pipeline.compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="auto",
    )

    ir_text = out.read_text(encoding="utf-8")
    assert not pipeline._ir_needs_libpython(ir_text)
    assert "call void @py_cpy_ensure_init" not in _main_ir(ir_text)


def test_multi_auto_late_sibling_fallback_initializes_entry_main(tmp_path):
    """An aggregate fallback must initialize before sibling module code."""
    from pcc.frontends.python import pipeline

    entry = tmp_path / "entry.py"
    worker = tmp_path / "worker.py"
    out = tmp_path / "combined.ll"
    entry.write_text(
        "from .worker import launch\nlaunch()\n",
        encoding="utf-8",
    )
    worker.write_text(
        textwrap.dedent(
            """
            def worker(value):
                from decimal import Decimal
                return Decimal(value)

            def launch() -> None:
                worker(1)
            """
        ).lstrip(),
        encoding="utf-8",
    )

    pipeline.compile_python_multi(
        [str(entry), str(worker)],
        str(out),
        entry_module="pkg.entry",
        module_names=["pkg.entry", "pkg.worker"],
        emit_llvm_only=True,
        libpython_mode="auto",
    )

    ir_text = out.read_text(encoding="utf-8")
    main_ir = _main_ir(ir_text)
    assert pipeline._ir_needs_libpython(ir_text)
    assert "call ptr @py_cpy_import" in ir_text
    assert main_ir.count("call void @py_cpy_ensure_init") == 1
    assert main_ir.index("call void @py_set_program_args") < main_ir.index(
        "call void @py_cpy_ensure_init"
    )
    assert main_ir.index("call void @py_cpy_ensure_init") < main_ir.index(
        "call ptr @py_compiled_module_import_by_name"
    )
