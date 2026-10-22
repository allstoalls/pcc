"""Native slicing must consume owned operands without stealing borrowed ones."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("annotation, initial", [
    ("bytes", '("x" + str(42)).encode()'),
    ("str", '"x" + str(42)'),
    ("list[int]", "[1, 2, 3]"),
    ("tuple", "tuple([1, 2, 3])"),
])
@pytest.mark.parametrize("receiver", ["attribute", "call", "borrowed"])
def test_slice_balances_receiver_reference(
    tmp_path, pcc_runtime_archive, python_program_compiler,
    monkeypatch, annotation, initial, receiver,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    expression = {
        "attribute": "holder.data", "call": "identity(payload)",
        "borrowed": "payload",
    }[receiver]
    source = tmp_path / "slice_owner.py"
    source.write_text(f'''from pcc.unsafe import load_i64, abi_constant
class Holder:
    def __init__(self, data: {annotation}):
        self.data = data
def identity(value: {annotation}) -> {annotation}:
    return value
def exercise(payload: {annotation}):
    holder = Holder(payload)
    before = load_i64(payload, abi_constant("object.header.refcount_offset"))
    for index in range(32):
        part = {expression}[1:2]
        assert len(part) == 1
    print(load_i64(payload, abi_constant("object.header.refcount_offset")) - before)
exercise({initial})
''')
    binary = tmp_path / "slice_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "0\n", (backend, result.stdout)


def test_slice_operand_cleanup_handles_bounds_errors_and_rebinding(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "slice_errors.py"
    source.write_text('''import gc
from pcc.unsafe import load_i64, abi_constant
destroyed = 0
rejected_destroyed = 0
bound_destroyed = 0
anchor = ("x" + str(42)).encode()
class Holder:
    def __init__(self, value: bytes):
        self.data = value
class Echo:
    def __getitem__(self, index):
        return self
    def __del__(self):
        global destroyed
        destroyed += 1
class Rejected:
    def __getitem__(self, index):
        raise ValueError("getitem")
    def __del__(self):
        global rejected_destroyed
        rejected_destroyed += 1
class Bound:
    def __del__(self):
        global bound_destroyed
        bound_destroyed += 1
def fail():
    raise ValueError("bound")
def rebind():
    global anchor
    anchor = b"changed"
    gc.collect()
    return 0
def main():
    data = ("y" + str(42)).encode()
    holder = Holder(data)
    before = load_i64(data, abi_constant("object.header.refcount_offset"))
    caught = 0
    for index in range(8):
        try:
            holder.data[fail():]
        except ValueError:
            caught += 1
        try:
            holder.data[Bound():fail()]
        except ValueError:
            caught += 1
        try:
            Rejected()[0:1]
        except ValueError:
            caught += 1
    print(caught, load_i64(data, abi_constant("object.header.refcount_offset")) - before,
          rejected_destroyed, bound_destroyed)
    print(anchor[rebind():1] == b"x")
    try:
        Echo()[fail():]
    except ValueError:
        pass
    gc.collect()
    print(destroyed)
    result = Echo()[0:1]
    gc.collect()
    print(destroyed)
    result = None
    gc.collect()
    print(destroyed)
main()
''')
    binary = tmp_path / "slice_errors"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "24 0 8 8\nTrue\n1\n1\n2\n", (backend, result.stdout)


@pytest.mark.parametrize("constructor", ["bytes", "bytearray", "memoryview"])
@pytest.mark.parametrize("receiver", ["holder.data", "payload"])
def test_bytes_family_constructor_consumes_only_owned_operand(
    tmp_path, pcc_runtime_archive, python_program_compiler,
    monkeypatch, constructor, receiver,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "buffer_owner.py"
    source.write_text(f'''from pcc.unsafe import load_i64, abi_constant
class Holder:
    def __init__(self, data: bytes):
        self.data = data
def exercise(payload: bytes):
    holder = Holder(payload)
    before = load_i64(payload, abi_constant("object.header.refcount_offset"))
    for index in range(32):
        result = {constructor}({receiver})
        assert len(result) == len(payload)
        result = None
    print(load_i64(payload, abi_constant("object.header.refcount_offset")) - before)
exercise(("buffer" + str(42)).encode())
''')
    binary = tmp_path / "buffer_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "0\n", (backend, result.stdout)
