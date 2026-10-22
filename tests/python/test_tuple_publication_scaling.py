"""Receipt-producing native tuple construction input for size-scaling probes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROGRAM = '''
import sys

def run(count: int) -> None:
    source = [1] * count
    values = tuple(source)
    assert values is not source
    assert len(values) == count
    assert values[0] == 1
    assert values[-1] == 1
    print(len(values))
    print(values[0])
    print(values[-1])

run(int(sys.argv[1]))
'''.lstrip()


@pytest.mark.pcc_gate(env="PCC_RUNTIME_ARCHIVE")
def test_native_tuple_from_list_preserves_contents(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    archive_name = os.environ.get("PCC_RUNTIME_ARCHIVE")
    if not archive_name:
        pytest.fail("requires explicit immutable PCC_RUNTIME_ARCHIVE")
    archive = Path(archive_name).resolve(strict=True)
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = tmp_path / "tuple_growth.py"
    source.write_text(PROGRAM, encoding="utf-8")
    binary = tmp_path / "tuple_growth"
    compile_python_multi(
        [str(source)], str(binary), entry_module="tuple_growth",
        module_names=["tuple_growth"], libpython_mode="off",
        ir_scaffold_mode="on", backend="self", recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0", runtime_archive=str(archive),
    )
    native = subprocess.run([str(binary), "64"], capture_output=True, text=True, timeout=10)
    oracle = subprocess.run([sys.executable, str(source), "64"], capture_output=True, text=True, timeout=10)
    assert native.returncode == oracle.returncode == 0, native.stderr + oracle.stderr
    assert native.stdout == oracle.stdout == "64\n1\n1\n"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_hash
    receipt = {
        "status": "COMPLETE", "mode": "host-built self/no-libpython native runtime probe",
        "binary": str(binary), "source": str(source),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runtime_archive": str(archive), "runtime_sha256": archive_hash,
        "stdout": native.stdout,
    }
    path = tmp_path / "tuple_growth.receipt.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"tuple growth receipt: {path}")
