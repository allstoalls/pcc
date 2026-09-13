"""Path construction accepts existing path objects without treating them as str."""

import os
import subprocess

from pcc.py_frontend.pipeline import compile_python


def test_path_copy_and_file_read(tmp_path, pcc_py_runtime_archive):
    payload = tmp_path / "payload.txt"
    payload.write_text("payload", encoding="utf-8")
    source = tmp_path / "path_copy.py"
    source.write_text(
        "from pathlib import Path\n"
        "def main():\n"
        "    first = Path(" + repr(str(payload)) + ")\n"
        "    second = Path(first)\n"
        "    third = Path(second)\n"
        "    print(str(first) == str(second), str(second) == str(third))\n"
        "    print(third.read_text(encoding='utf-8'))\n"
        "    print(third.read_bytes() == b'payload')\n"
        "main()\n", encoding="utf-8",
    )
    binary = tmp_path / "path_copy"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == "True True\npayload\nTrue\n", f"GC{backend}: {result.stdout}"
