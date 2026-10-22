"""Native ``gc`` module lowering for the no-libpython Python subset."""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str = "on") -> str:
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


def test_gc_collect_dispatches_to_native_helper():
    program = textwrap.dedent(
        """
        import gc
        from gc import collect, isenabled

        def f() -> None:
            print(gc.collect())
            print(collect())
            print(gc.isenabled())
            print(isenabled())
        """
    )

    ir_text = _compile_to_ll(program, "native_gc_collect_ir")
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@pcc_gc_collect" in body, body
    assert "@py_gc_is_enabled" in body, body
    assert "@py_int_from_i64" in body, body
    assert "py_cpy_" not in body, body


def test_native_gc_public_api_runtime_for_refcount_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import gc

            def main() -> None:
                print(gc.collect())
                print(gc.collect(0))
                print(gc.isenabled())
                gc.disable()
                print(gc.isenabled())
                gc.enable()
                print(gc.isenabled())
                count = gc.get_count()
                print(count[0] >= 0)
                print(count[1] == 0)
                print(count[2] == 0)
                print(gc.get_threshold())
                gc.set_threshold(701, 11, 12)
                print(gc.get_threshold())
                print(gc.is_tracked(None))
                print(gc.is_tracked(1))
                print(gc.is_tracked("x"))

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
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == (
        "0\n"
        "0\n"
        "True\n"
        "False\n"
        "True\n"
        "True\n"
        "True\n"
        "True\n"
        "(2000, 10, 10)\n"
        "(701, 11, 12)\n"
        "False\n"
        "False\n"
        "False\n"
    )
