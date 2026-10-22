"""Generator print keyword paths retain their tuple across suspended calls."""

import contextlib
import io
import os
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


PROGRAM = '''def values():
    yield 0
    print("single", flush=True)
    print(end="empty\\n", flush=True)
    print("last", sep="-", end="!\\n", flush=False)
    yield 1
def main():
    for value in values():
        print(value)
main()
'''
EXPECTED = "0\nsingle\nempty\nlast!\n1\n"


def test_generator_print_kwargs_reference():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAM, {})
    assert output.getvalue() == EXPECTED


def test_generator_single_and_empty_print_kwargs_reach_owned_emitter(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "generator_print.py"
    source.write_text(PROGRAM)
    output = tmp_path / "generator_print.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    text = output.read_text()
    assert "@py_print_many(" in text
    assert emit_owned_object(text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
def test_native_generator_print_keywords_all_gcs(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "generator_print.py"
    source.write_text(PROGRAM)
    binary = tmp_path / "generator_print"
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", ir_scaffold_mode="on",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == EXPECTED
