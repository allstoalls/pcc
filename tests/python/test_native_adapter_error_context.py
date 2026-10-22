"""A generated adapter owns its exception edges and extracted arguments."""

import os
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


PROGRAM = '''from pcc import valueclass, i64
@valueclass
class Pair:
    first: i64
    second: i64
class Carrier:
    def consume(self, value: Pair) -> i64:
        return value.first
def run():
    carrier = Carrier()
    function = carrier.consume
    assert function(Pair(i64(7), i64(8))) == 7
    try:
        function(object())
    except TypeError:
        pass
    else:
        raise AssertionError("bad adapter argument accepted")
    print("ADAPTER_SCOPE_OK")
run()
'''


@pytest.mark.parametrize("direct", [False, True], ids=["text", "indexed"])
def test_adapter_error_edges_stay_in_the_adapter(tmp_path, monkeypatch, direct):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "adapter_scope.py"
    source.write_text(PROGRAM)
    output = tmp_path / "adapter_scope.ll"
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="arm64-apple-darwin")
    text = output.read_text()
    assert "native.adapter.input.unwind" in text
    assert emit_owned_object(text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
def test_native_adapter_propagates_argument_type_error(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "adapter_scope.py"
    source.write_text(PROGRAM)
    output = tmp_path / "adapter_scope"
    python_program_compiler(str(source), str(output), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        run = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == "ADAPTER_SCOPE_OK\n"
