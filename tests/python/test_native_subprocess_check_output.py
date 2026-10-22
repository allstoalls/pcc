"""``subprocess.check_output(..., text=True)`` native lowering."""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(
    source: str,
    name: str,
    *,
    mode: str,
    recursive_stdlib: bool = False,
) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
        recursive_stdlib=recursive_stdlib,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


def test_check_output_text_dispatches_to_native_helper():
    program = textwrap.dedent(
        """
        import subprocess

        def f():
            return subprocess.check_output(
                ["printf", "hi"], text=True,
            ).strip()
        """
    )

    ir_text = _compile_to_ll(program, "native_subprocess_check_output_ir", mode="on")
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@py_subprocess_check_output" in body, body
    assert "@py_str_strip" in body, body
    assert "@py_cpy_to_pcc_obj" not in body, body
    assert "cpy.fn.check_output" not in body, body


def test_check_output_text_stderr_stdout_dispatches_to_native_helper():
    program = textwrap.dedent(
        """
        import subprocess

        def f():
            return subprocess.check_output(
                ["printf", "hi"], text=True, stderr=subprocess.STDOUT,
            ).strip()
        """
    )

    ir_text = _compile_to_ll(
        program, "native_subprocess_check_output_stderr_stdout_ir",
        mode="on",
    )
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@py_subprocess_check_output" in body, body
    assert "cpy.fn.check_output" not in body, body
    assert "cpy.get.STDOUT" not in body, body


def test_check_output_without_text_lowers_native_bytes():
    # Previously a bare (no text=) check_output kept the CPython fallback —
    # which made the program uncompilable under --python-libpython=off. The
    # native helper already returns a real bytes object (verified equal to
    # CPython for print/len/.decode()), so the no-text form now lowers to
    # py_subprocess_check_output too; downstream bytes methods stay native
    # via _maybe_emit_bytes_method_via_dyn.
    program = textwrap.dedent(
        """
        import subprocess

        def f():
            return subprocess.check_output(["printf", "hi"])
        """
    )

    ir_text = _compile_to_ll(program, "native_subprocess_check_output_bytes", mode="on")
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@py_subprocess_check_output" in body, body
    assert "cpy.fn.check_output" not in body, body


def test_run_check_true_expr_statement_dispatches_native():
    program = textwrap.dedent(
        """
        import subprocess

        def f() -> None:
            subprocess.run(
                ["printf", "hi"], check=True, capture_output=True,
            )
        """
    )

    ir_text = _compile_to_ll(
        program,
        "native_subprocess_run_ir",
        mode="on",
        recursive_stdlib=True,
    )
    body = _function_body(ir_text, "f")

    assert body is not None
    # Captured output belongs to the CalledProcessError, so the call goes to
    # the owned provider's run(), never to the output-less status lane.
    assert "import.method.run" in body, body
    assert "@py_subprocess_run(" not in body, body
    assert "cpy.fn.run" not in body, body


def test_run_timeout_keyword_still_dispatches_native():
    program = textwrap.dedent(
        """
        import subprocess

        def f() -> None:
            subprocess.run(
                ["printf", "hi"], check=True, timeout=5,
            )
        """
    )

    ir_text = _compile_to_ll(
        program,
        "native_subprocess_run_timeout_ir",
        mode="on",
        recursive_stdlib=True,
    )
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@py_subprocess_run_timeout" in body, body
    assert "subprocess.run.timed_out" in body, body
    assert "cpy.fn.run" not in body, body


def test_run_capture_output_bool_expression_dispatches_native():
    program = textwrap.dedent(
        """
        import subprocess

        def f(verbose: bool) -> None:
            subprocess.run(
                ["printf", "hi"], check=True, capture_output=not verbose,
            )
        """
    )

    ir_text = _compile_to_ll(
        program,
        "native_subprocess_run_dyn_capture",
        mode="on",
        recursive_stdlib=True,
    )
    body = _function_body(ir_text, "f")

    assert body is not None
    # A capture decided at run time also needs the provider's run().
    assert "import.method.run" in body, body
    assert "@py_subprocess_run(" not in body, body
    assert "cpy.fn.run" not in body, body


def test_native_check_output_runtime(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import subprocess

            def main() -> None:
                out = subprocess.check_output(
                    ["printf", "hello"], text=True,
                ).strip()
                print(out)

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "hello\n"


def test_native_run_runtime(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import subprocess

            def main() -> None:
                subprocess.run(
                    ["printf", "hidden"], check=True, capture_output=True,
                )
                print("ok")

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "ok\n"
