"""``sysconfig`` routes through the pcc-owned recursive stdlib provider."""
from __future__ import annotations

import os
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
        libpython_mode="off",
        recursive_stdlib=True,
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


def test_sysconfig_get_config_var_dispatches_to_compiled_stdlib_provider():
    program = textwrap.dedent(
        """
        import sysconfig

        def f():
            return sysconfig.get_config_var("VERSION")
        """
    )

    ir_text = _compile_to_ll(program, "native_sysconfig_ir", mode="on")
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@user_sysconfig_get_config_var" in body, body
    assert "@py_sysconfig_get_config_var" not in body, body
    assert "cpy.import.sysconfig" not in body, body
    assert "cpy.fn.get_config_var" not in body, body


def test_native_sysconfig_runtime_returns_version_without_host_python(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import sysconfig

            def main() -> None:
                v = sysconfig.get_config_var("VERSION")
                print(v is not None)

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        recursive_stdlib=True,
    )
    run_env = os.environ.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run_env["PATH"] = str(tmp_path / "no-host-python")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\n"
