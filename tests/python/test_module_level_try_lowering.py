"""Module-level ``try``/``except`` must lower like the in-function form.

``@main`` and ``_pcc_py_module_top_*`` are IR functions, so module-level
statements need the same per-function reset of the exception-handler state that
``_emit_user_function`` does in its prologue. Without it a module-level ``try``
branched into whichever basic block the previously lowered function left in
``_try_err_block`` -- invalid IR, and invisible on the host because whether the
carried-over block happened to be ``None`` depended on lowering order.

That is why ``pcc1`` could not compile ``pcc/frontends/python/pipeline.py`` (it has a
module-level ``try`` at its ``PCC_DEBUG_RUNTIME`` probe) and stage2 could not run
at all, while the host compiler was green.

See docs/investigations/pcc1-module-level-try-except-unsupported.md
"""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python


def _build_and_run(tmp_path, source: str) -> str:
    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


def test_module_level_try_except_runs(tmp_path):
    """The nine-line reproducer the stage2 failure minimised to."""
    assert (
        _build_and_run(
            tmp_path,
            """
            try:
                Z = 1
            except Exception:
                pass


            def main() -> None:
                print("ok")


            main()
            """,
        )
        == "ok"
    )


def test_module_level_try_after_a_function_with_try(tmp_path):
    """The ordering that made this fail: a function containing its own ``try``
    is lowered first, so ``_try_err_block`` is non-``None`` when the
    module-level ``try`` is reached."""
    assert (
        _build_and_run(
            tmp_path,
            """
            def earlier() -> int:
                try:
                    return 1
                except Exception:
                    return 2


            try:
                Z = earlier()
            except Exception:
                Z = 0


            def main() -> None:
                print("z=" + str(Z))


            main()
            """,
        )
        == "z=1"
    )


def test_module_level_try_handler_runs(tmp_path):
    """The handler must be reachable, not merely present: a module-level
    ``except`` that branched into another function's blocks could still link."""
    assert (
        _build_and_run(
            tmp_path,
            """
            try:
                Z = 1 // 0
            except Exception:
                Z = 7


            def main() -> None:
                print("z=" + str(Z))


            main()
            """,
        )
        == "z=7"
    )
