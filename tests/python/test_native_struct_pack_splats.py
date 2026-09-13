"""Native struct packing preserves positional and starred values across modules."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_struct_pack_positional_and_splat_values(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'struct_splats.py'
    source.write_text('''import struct
def main():
    print(struct.pack("<I", 0).hex())
    print(struct.pack("<I", 7).hex())
    for values in [[], [0], [513], [1, 2]]:
        fmt = "<%dI" % len(values)
        print(struct.pack(fmt, *values).hex())
        shape = struct.Struct(fmt)
        print(shape.pack(*values).hex())
    for fmt in ["<0I", "<0c", "<0?", "<0x"]:
        shape = struct.Struct(fmt)
        print(shape.size, shape.pack(), shape.unpack(b""))
    zero_bytes = struct.Struct("<0s")
    print(zero_bytes.size, zero_bytes.pack(b"ignored"), zero_bytes.unpack(b""))
    for fmt in ["<0", "<00", "<1"]:
        try:
            struct.Struct(fmt)
        except struct.error:
            print("bad repeat")
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'struct_splats'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
