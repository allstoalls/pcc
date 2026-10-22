"""Lower the exact owned lock primitives; target execution is a separate gate."""
import re
from pathlib import Path

import pytest
from pcc.frontends.python.pipeline import compile_python


@pytest.mark.parametrize("target", ["arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"])
def test_posix_flock_intrinsic_uses_target_os(tmp_path, target):
    source = tmp_path / "lock.py"
    output = tmp_path / "lock.ll"
    source.write_text('''from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import file_flock
__pcc_freestanding__ = True
@c_abi_export("probe_lock")
def probe_lock(fd: i64, operation: i64) -> i64:
    return file_flock(fd, operation)
''')
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", target_triple=target)
    text = output.read_text()
    if "darwin" in target:
        assert "@flock(" in text and "@__error(" in text
        assert "-35" in text and "-11" in text
    else:
        assert "@flock(" not in text
        assert "syscall" in text if target.startswith("x86_64") else "svc" in text
        assert "i64 73" in text if target.startswith("x86_64") else "i64 32" in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


def test_windows_lock_region_uses_owned_handle_runtime(tmp_path):
    source = tmp_path / "lock.py"
    output = tmp_path / "lock.ll"
    source.write_text('''from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import file_lock_region
__pcc_freestanding__ = True
@c_abi_export("probe_lock")
def probe_lock(fd: i64) -> i64:
    return file_lock_region(fd, 1, 0, 1)
''')
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", target_triple="x86_64-pc-windows-msvc")
    text = output.read_text()
    assert "@pcc_win_file_lock_region(" in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


def test_file_lock_runtime_exports_actual_locking_operations(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = root / "pcc/runtime/py/py_file_lock.py"
    output = tmp_path / "file_lock.ll"
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", target_triple="arm64-apple-darwin")
    text = output.read_text()
    assert "@py_fcntl_flock(" in text
    assert "@py_msvcrt_locking(" in text
    assert "@flock(" in text
    assert "@py_index_i64_checked(" in text
    assert "@pcc_gc_scheduler_root_register_handle(" in text
    assert "@pcc_thread_safepoint(" in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)
