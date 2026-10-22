"""Dataclass fields named after builtins survive cross-module construction."""

import os
import subprocess
import sys


def test_cross_module_dataclass_builtin_named_fields(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    (tmp_path / "records.py").write_text('''
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Record:
    offset: int
    symbol: str
    type: int
    pcrel: bool
    length: int = 2
    addend: int = 0
    section: tuple[str, str] | None = None
    minuend: str | None = None
    target_offset: int | None = None

def make_records(offset_bias: int = 0) -> list[Record]:
    result: list[Record] = []
    for offset in range(2):
        result.append(Record(offset=offset + offset_bias, symbol="entry",
                             type=offset, pcrel=False, length=3))
    return result
''', encoding="utf-8")
    source = tmp_path / "main.py"
    source.write_text('''
from records import Record, make_records

def main():
    records = make_records(offset_bias=8)
    records.append(Record(10, "positional", 2, True))
    for record in records:
        print(record.offset, record.symbol, record.type, record.pcrel,
              record.length, record.addend, record.section, record.minuend,
              record.target_offset)
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "records"
    python_program_compiler(str(source), str(output), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(output)], capture_output=True, text=True, timeout=10,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
