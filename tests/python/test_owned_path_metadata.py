"""Owned path metadata lowering; no file-content reads are part of getsize."""

import re

import pytest

from pcc.frontends.python.pipeline import compile_python, count_py_cpy_fallback_calls


def test_path_metadata_calls_stay_native_and_check_errors(tmp_path):
    source = tmp_path / "metadata.py"
    output = tmp_path / "metadata.ll"
    source.write_text(
        "import os\n"
        "def inspect(path: str):\n"
        "    try:\n"
        "        return os.path.islink(path), os.path.getsize(path), os.path.getmtime(path)\n"
        "    except OSError:\n"
        "        return False, -1, -1.0\n",
        encoding="utf-8",
    )
    compile_python(str(source), str(output), emit_llvm_only=True,
                   libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text(encoding="utf-8")
    body = re.search(r"define [^\n]*@user_[^\n]*inspect\([^\n]*\)\s*[^\n]*\{(.*?)\n\}", text, re.S)
    assert body is not None
    assert "@py_os_path_islink" in body[1]
    assert "@py_os_path_getsize" in body[1]
    assert "@py_os_path_getmtime" in body[1]
    assert "@py_err_occurred" in body[1]
    assert "@py_cpy_" not in body[1]
    assert "strict.nolib.stub" not in body[1]


@pytest.mark.parametrize("target", [
    "arm64-apple-darwin", "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
])
def test_metadata_intrinsics_use_the_target_stat_abi(tmp_path, target):
    source = tmp_path / "stat_probe.py"
    output = tmp_path / "stat_probe.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export, c_ptr\n"
        "from pcc.unsafe import stat_size, is_symlink\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('metadata_size')\n"
        "def metadata_size(path: c_ptr) -> i64:\n"
        "    return stat_size(path)\n"
        "@c_abi_export('metadata_link')\n"
        "def metadata_link(path: c_ptr) -> i64:\n"
        "    return is_symlink(path)\n",
        encoding="utf-8",
    )
    compile_python(str(source), str(output), emit_llvm_only=True,
                   libpython_mode="off", python_library=True, target_triple=target)
    text = output.read_text(encoding="utf-8")
    assert count_py_cpy_fallback_calls(text) == 0
    assert "strict.nolib.stub" not in text
    if "windows" in target:
        assert "@pcc_win_stat_size" in text
        assert "@pcc_win_is_symlink" in text
    elif "darwin" in target:
        assert "@stat(" in text and "@lstat(" in text
        assert "i64 96" in text
        assert "@__error(" in text
    else:
        assert "i64 48" in text
        if target.startswith("aarch64"):
            assert "svc" in text
            assert "i64 79" in text
            assert "i64 256" in text
        else:
            assert "syscall" in text
            assert "i64 4" in text and "i64 6" in text
