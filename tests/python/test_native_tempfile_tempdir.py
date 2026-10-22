"""``with tempfile.TemporaryDirectory(prefix=...)`` through the owned provider."""
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


def test_tempdir_with_uses_the_owned_provider_type():
    program = textwrap.dedent(
        """
        import tempfile

        def f() -> None:
            with tempfile.TemporaryDirectory(prefix="pcc_native_") as tmp:
                print(tmp)
        """
    )

    ir_text = _compile_to_ll(program, "native_tempdir_ir", mode="on")
    body = _function_body(ir_text, "f")

    assert body is not None
    # The first-class owned TemporaryDirectory provider replaced the old
    # path-only with-lowering (py_tempdir_new/py_tempdir_cleanup): the manager
    # is a real object entered and exited through the context protocol.
    assert "@py_tempdir_type" in body, body
    assert "@py_context_enter" in body and "@py_context_exit" in body, body
    assert "@py_tempdir_new" not in body, body
    assert "cpy.fn.TemporaryDirectory" not in body, body
    assert "with.enter.fn" not in body, body
    assert "with.exit.fn" not in body, body


def test_native_tempdir_runtime_creates_and_cleans(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import os
            import tempfile

            def main() -> None:
                path = ""
                with tempfile.TemporaryDirectory(prefix="pcc_native_") as tmp:
                    path = tmp
                    print(os.path.isdir(tmp))
                print(os.path.exists(path))

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
        env=dict(os.environ, PATH="/nonexistent"),
    )
    assert run.returncode == 0, run.stderr
    assert run.stderr == ""
    assert run.stdout == "True\nFalse\n"


def test_native_tempdir_default_probe_and_nonempty_cleanup(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    temp_root = tmp_path / "default-root"
    temp_root.mkdir()
    src = tmp_path / "default_tempdir.py"
    exe = tmp_path / "default_tempdir.out"
    src.write_text(
        textwrap.dedent(
            """
            import os
            import tempfile

            def main() -> None:
                # No explicit dir: the owned provider must probe TMPDIR.
                path = ""
                with tempfile.TemporaryDirectory(prefix="pcc_directory_") as tmp:
                    path = tmp
                    print(tmp)
                    os.mkdir(tmp + "/nested")
                    with open(tmp + "/nested/entry.txt", "w") as handle:
                        handle.write("owned")
                    print(os.listdir(tmp) == ["nested"])
                    print(os.listdir(tmp + "/nested") == ["entry.txt"])
                print(os.path.exists(path))
                print(os.path.exists(path + "/nested/entry.txt"))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src), str(exe), backend="self", ir_scaffold_mode="on",
        libpython_mode="off", runtime_archive=str(pcc_runtime_archive),
    )
    run = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
        env=dict(os.environ, PATH="/nonexistent", TMPDIR=str(temp_root)),
    )
    assert run.returncode == 0, run.stderr
    assert run.stderr == ""
    lines = run.stdout.splitlines()
    assert len(lines) == 5, run.stdout
    assert Path(lines[0]).parent == temp_root
    assert lines[1:] == ["True", "True", "False", "False"]
    # Both the initial .pcc_probe_ directory and the nonempty tree are gone.
    assert list(temp_root.iterdir()) == []
