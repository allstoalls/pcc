from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python, count_py_cpy_fallback_calls


def test_shutil_rmtree_uses_owned_runtime(tmp_path):
    src = tmp_path / "rmtree_ir.py"
    ir_path = tmp_path / "rmtree_ir.ll"
    src.write_text(
        textwrap.dedent(
            """
            import shutil

            def remove(path: str, ignore: bool):
                shutil.rmtree(path, ignore_errors=ignore)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(ir_path),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ir_text = ir_path.read_text(encoding="utf-8")
    assert "call ptr (ptr, i32) @py_shutil_rmtree" in ir_text
    assert count_py_cpy_fallback_calls(ir_text) == 0


def test_shutil_rmtree_native_tree_and_error_paths(
    tmp_path, monkeypatch, pcc_runtime_archive
):
    monkeypatch.setenv("PCC_RUNTIME_CC", "pcc")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    tree = tmp_path / "tree"
    nested = tree / "nested"
    nested.mkdir(parents=True)
    (nested / "leaf").write_text("hello", encoding="utf-8")
    plain_file = tmp_path / "plain_file"
    plain_file.write_text("keep", encoding="utf-8")
    link = tmp_path / "tree_link"
    link.symlink_to(tree, target_is_directory=True)
    missing = tmp_path / "missing"

    src = tmp_path / "rmtree_native.py"
    exe = tmp_path / "rmtree_native"
    src.write_text(
        textwrap.dedent(
            f"""
            import shutil

            def remove(path: str, ignore: bool):
                shutil.rmtree(path, ignore_errors=ignore)

            for path in ({str(missing)!r}, {str(plain_file)!r}, {str(link)!r}):
                try:
                    remove(path, False)
                except OSError:
                    print("rejected")
                remove(path, True)
            remove({str(tree)!r}, False)
            print("removed")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off"
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "rejected\nrejected\nrejected\nremoved\n"
    assert not tree.exists()
    assert plain_file.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
