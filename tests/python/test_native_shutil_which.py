"""``shutil.which`` native lowering."""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
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


def test_shutil_which_dispatches_to_native_helper():
    program = textwrap.dedent(
        """
        import shutil

        def f():
            return shutil.which("sh")
        """
    )

    ir_text = _compile_to_ll(program, "native_shutil_which_ir", mode="on")
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@py_shutil_which" in body, body
    assert "cpy.import.shutil" not in body, body
    assert "cpy.fn.which" not in body, body


def test_native_shutil_which_runtime_matches_present_and_missing(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import shutil

            def main() -> None:
                print(shutil.which("sh") is not None)
                print(shutil.which("pcc_missing_command_zzz") is None)

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
    assert run.stdout == "True\nTrue\n"


def test_native_shutil_which_path_keyword_matches_cpython(tmp_path):
    # which(cmd, path=...) fell back to CPython, so pcc.package.toolchain
    # ._find_tool was a no-libpython stub and pcc1 build-exec failed.
    import shutil
    import sys

    from pcc.frontends.python.pipeline import compile_python

    tool = tmp_path / "bin" / "pcc-probe-tool"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    (tmp_path / "bin" / "not-executable").write_text("")
    src = tmp_path / "which_path.py"
    exe = tmp_path / "which_path.out"
    src.write_text(
        textwrap.dedent(
            f"""
            import shutil

            BIN = {str(tool.parent)!r}

            def main() -> None:
                print(shutil.which("pcc-probe-tool", path=BIN) == BIN + "/pcc-probe-tool")
                print(shutil.which("pcc-probe-tool", path="/nonexistent:" + BIN) == BIN + "/pcc-probe-tool")
                print(shutil.which("pcc-probe-tool", path="/nonexistent"))
                print(shutil.which("pcc-probe-tool", path=""))
                print(shutil.which("not-executable", path=BIN))
                print(shutil.which("sh", path=None) == shutil.which("sh"))

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert native.returncode == 0, native.stderr
    ref = subprocess.run([sys.executable, str(src)], capture_output=True, text=True, timeout=20)
    assert ref.returncode == 0, ref.stderr
    assert native.stdout == ref.stdout == "True\nTrue\nNone\nNone\nNone\nTrue\n"
    assert shutil.which("sh") is not None
