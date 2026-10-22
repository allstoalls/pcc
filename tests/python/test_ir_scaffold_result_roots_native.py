"""A supplied native compiler must execute nested scaffold result owners."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROGRAM = '''import gc
from pcc.ir.compat import ir
def take(*, value, later=None, **extras):
    gc.collect()
    return value
def mark(events, name, value):
    events.append(name)
    gc.collect()
    return value
def fail():
    gc.collect()
    raise ValueError("later")
def main():
    events = []
    values = take(value=[ir.Constant(ir.IntType(mark(events, "width", 64)), mark(events, "value", 12))])
    assert events == ["width", "value"]
    assert str(values[0]) == "12" and str(values[0].type) == "i64"
    array = take(value=ir.ArrayType(ir.IntType(8), 7))
    assert str(array) == "[7 x i8]"
    module = ir.Module(name="result-roots")
    ty = ir.IntType(64)
    fn = ir.Function(module, ir.FunctionType(ty, []), name="answer")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    value = take(value=builder.add(ir.Constant(ty, 20), ir.Constant(ty, 22)))
    take(value=builder.ret(value))
    text = str(module)
    assert "add i64 20, 22" in text and "ret i64" in text
    try:
        take(value=ir.Constant(ir.IntType(64), 99), later=fail())
    except ValueError as error:
        assert str(error) == "later"
    else:
        raise AssertionError("lost later exception")
    assert str(take(value=ir.Constant(ir.IntType(64), 13))) == "13"
    print("SCAFFOLD_RESULT_ROOTS_OK")
main()
'''


def test_scaffold_result_roots_reference(tmp_path):
    source = tmp_path / "scaffold_result_roots.py"
    source.write_text(PROGRAM)
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    result = subprocess.run([sys.executable, "-B", str(source)], env=environment,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == "SCAFFOLD_RESULT_ROOTS_OK\n"


@pytest.mark.integration
def test_scaffold_result_roots_native_five_gc(tmp_path):
    requested = os.environ.get("PCC_SCAFFOLD_NATIVE_COMPILER", "")
    if not requested:
        pytest.fail("set PCC_SCAFFOLD_NATIVE_COMPILER to a qualified pcc1 or pcc3 executable")
    compiler = Path(requested).resolve(strict=True)
    source = tmp_path / "scaffold_result_roots.py"
    binary = tmp_path / "scaffold_result_roots"
    source.write_text(PROGRAM)
    environment = dict(os.environ, PCC_PYTHON_LIBPYTHON="off", PCC_SELF_LINK="pcc",
                       PCC_IR_SCAFFOLD="on", PCC_NO_AUTO_PCC1="1")
    environment.pop("LC_ALL", None)
    compiled = subprocess.run(
        [str(compiler), "--backend", "self", "--python-libpython", "off", str(source), "-o", str(binary)],
        env=environment, capture_output=True, text=True, timeout=180,
    )
    assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(environment, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "SCAFFOLD_RESULT_ROOTS_OK\n"
        assert result.stderr == ""
