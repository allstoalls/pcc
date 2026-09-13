"""Dynamic mapping and user getitem calls must receive the original int key."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_dynamic_getitem_preserves_wide_integers_and_bool_keys(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "dynamic_keys.py"
    source.write_text('''
def mapping(flag: bool):
    if flag:
        return {}
    return []

class Receiver:
    def __getitem__(self, key):
        return key is True

def main():
    data = mapping(True)
    wide = 1 << 70
    data[wide] = 42
    print(wide)
    try:
        print(data[wide], data[1 << 70])
    except KeyError:
        print("wide key lost")
    receiver = Receiver()
    print(receiver[True])
    print(receiver[1])
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "dynamic_keys"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"


def test_extern_object_dict_keeps_arbitrary_precision_keys(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "extern_dict_keys.py"
    source.write_text('''
from pcc.extern import c_obj, c_int64, extern
new_dict = extern("py_dict_new_presized", (c_int64,), c_obj)
def main():
    data = new_dict(2)
    wide = 1 << 70
    data[wide] = 42
    print(wide)
    print(data[wide])
    print(data[1 << 70])
main()
''', encoding="utf-8")
    output = tmp_path / "extern_dict_keys"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == str(1 << 70) + "\n42\n42\n"


def test_computed_wide_mapping_keys_release_temporary_objects(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "wide_key_lifetime.py"
    source.write_text('''
from pcc.extern import c_obj, c_int64, extern
import gc
new_dict = extern("py_dict_new_presized", (c_int64,), c_obj)
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(data, count: int):
    for index in range(count):
        if data[1 << 4096] != 7:
            raise ValueError("bad value")
class RefusesWrites:
    def __setitem__(self, key, value):
        raise ValueError("read only")
def scan_failed_writes(count: int):
    receiver = RefusesWrites()
    for index in range(count):
        try:
            receiver[1 << 4096] = index
        except ValueError:
            pass
def main():
    data = new_dict(2)
    data[1 << 4096] = 7
    scan(data, 16)
    gc.collect()
    before = live_bytes()
    scan(data, 512)
    gc.collect()
    print(live_bytes() - before)
    scan_failed_writes(16)
    gc.collect()
    before = live_bytes()
    scan_failed_writes(512)
    gc.collect()
    print(live_bytes() - before)
main()
''', encoding="utf-8")
    output = tmp_path / "wide_key_lifetime"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        growth = [int(line) for line in result.stdout.splitlines()]
        assert len(growth) == 2
        assert max(growth) < 65536, f"GC{gc}: {result.stdout}"
