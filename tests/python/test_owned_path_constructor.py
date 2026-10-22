"""Owned pathlib constructors remain real first-class imported values."""

import contextlib
import io
import os
import subprocess

import pytest


PROGRAM = '''from pathlib import Path as ImportedPath, PurePath
constructor = ImportedPath
def verify(archive, *, runtime_root):
    assert str(archive) == "artifact.a"
    assert str(runtime_root) == "runtime"
def main():
    verify(ImportedPath("artifact.a"), runtime_root=ImportedPath("runtime"))
    verify(constructor("artifact.a"), runtime_root=constructor("runtime"))
    pure_constructor = PurePath
    assert str(pure_constructor("artifact.a")) == "artifact.a"
    assert ImportedPath("artifact.a").suffix == ".a"
    print("OWNED_PATH_CONSTRUCTORS_OK")
main()
'''


def test_owned_path_constructor_reference():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAM, {})
    assert output.getvalue() == "OWNED_PATH_CONSTRUCTORS_OK\n"


@pytest.mark.integration
def test_native_owned_path_direct_and_first_class_constructors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "path_constructor.py"
    source.write_text(PROGRAM)
    binary = tmp_path / "path_constructor"
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", ir_scaffold_mode="on",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "OWNED_PATH_CONSTRUCTORS_OK\n"
