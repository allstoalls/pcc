import subprocess

import pytest

from pcc.backend.ar import ArchiveFormatError, read_members
from pcc.backend.ar_writer import write_archive
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.native_object import NativeObject
from pcc.backend.macho_exec import link_executable


def _object(body):
    sections, undefined = assemble_file(".section __TEXT,__text,regular,pure_instructions\n" + body)
    return NativeObject.from_sections(sections, undefined=undefined).to_macho()


def test_owned_archive_roundtrip_index_and_native_link_execution(tmp_path):
    provider = _object(".globl _answer\n_answer:\n  movz x0, #42\n  ret\n")
    unused = _object(".globl _unused\n_unused:\n  movz x0, #9\n  ret\n")
    members = [("long_answer_object_name.o", provider), ("unused.o", unused)]
    archive = write_archive(members)
    assert archive == write_archive(members)
    assert read_members(archive) == members
    caller = _object(".globl _main\n_main:\n  b _answer\n")
    output = tmp_path / "program"
    output.write_bytes(link_executable([caller], archives=[archive], entry="_main"))
    output.chmod(0o755)
    result = subprocess.run([str(output)], capture_output=True, timeout=10)
    assert result.returncode == 42, result.stderr
    # The system archiver is a format oracle only; the compiler never invokes it.
    path = tmp_path / "library.a"
    path.write_bytes(archive)
    reference = subprocess.run(["/usr/bin/ar", "t", str(path)], capture_output=True, text=True, timeout=10)
    assert reference.returncode == 0, reference.stderr
    listed = [name for name in reference.stdout.splitlines() if not name.startswith("__.SYMDEF")]
    assert listed == [name for name, _payload in members]
    caller_path = tmp_path / "caller.o"
    caller_path.write_bytes(caller)
    oracle_output = tmp_path / "oracle-program"
    reference_link = subprocess.run(["/usr/bin/cc", str(caller_path), str(path), "-o", str(oracle_output)],
                                    capture_output=True, text=True, timeout=15)
    assert reference_link.returncode == 0, reference_link.stderr
    assert subprocess.run([str(oracle_output)], timeout=10).returncode == 42


def test_archive_writer_rejects_nonobjects_and_duplicate_names():
    with pytest.raises(ArchiveFormatError, match="relocatable"):
        write_archive([("not-an-object", b"text")])
    obj = _object(".globl _answer\n_answer:\n  ret\n")
    with pytest.raises(ArchiveFormatError, match="duplicate"):
        write_archive([("same.o", obj), ("same.o", obj)])
