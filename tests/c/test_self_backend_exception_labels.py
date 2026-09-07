"""Exceptional successors remain addressable after branch threading."""

import platform
import subprocess
import sys

import pytest

from pcc.backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_asm,
    emit_aarch64_darwin_indexed_transport,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.macho_exec import link_executable
from pcc.backend.native_object import NativeObject


_SOURCE = '''target triple = "arm64-apple-darwin23.6.0"
@state = global i64 0
define i64 @py_err_occurred() {
entry:
  %state = load i64, ptr @state
  ret i64 %state
}
define i64 @check() {
entry:
  %error = call i64 @py_err_occurred()
  %clear = icmp eq i64 %error, 0
  br i1 %clear, label %success, label %exception
success:
  ret i64 1
exception:
  br label %exit
exit:
  ret i64 0
}
define i32 @main() {
entry:
  %clear = call i64 @check()
  store i64 1, ptr @state
  %raised = call i64 @check()
  %bad1 = icmp ne i64 %clear, 1
  %bad2 = icmp ne i64 %raised, 0
  %bad = or i1 %bad1, %bad2
  %status = zext i1 %bad to i32
  ret i32 %status
}
'''


def test_optimized_exception_successor_retains_metadata_anchor(tmp_path):
    assembly = emit_aarch64_darwin_asm(_SOURCE, optimize=True)
    assert "L_check_exception:" in assembly
    transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(_SOURCE), optimize=True)
    sections, undefined = transport.assemble_sections()
    image = link_executable([NativeObject.from_sections(sections, undefined=undefined)])
    if transport.encoded_line_records is not None:
        transport.encoded_line_records.close()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("AArch64 Darwin execution")
    executable = tmp_path / "exceptions"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
