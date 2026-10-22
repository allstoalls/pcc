"""Mixed dynamic/scalar equality must preserve arbitrary-precision operands."""

import os
import subprocess


def test_dynamic_equality_keeps_integer_expression_precision(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "dynamic_bigint.py"
    source.write_text('''from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def check(value):
    assert value == 2 ** 128 - 1
    assert not (value != 2 ** 128 - 1)
    assert 2 ** 128 - 1 == value
    assert not (2 ** 128 - 1 != value)
    assert value != 2 ** 128 + 1
    assert not (value == 2 ** 128 + 1)
class Equal:
    def __eq__(self, other):
        return str(other) == "340282366920938463463374607431768211455"
    def __ne__(self, other):
        return not self.__eq__(other)
def main():
    marker = heap()
    check(int.from_bytes(b"\\xff" * 16, "big"))
    check(Equal())
    print("ok")
main()
''')
    binary = tmp_path / "dynamic_bigint"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=15,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "ok\n", (backend, result.stdout)
