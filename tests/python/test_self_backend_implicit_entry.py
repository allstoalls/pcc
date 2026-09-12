"""Unlabelled LLVM entry blocks retain their implicit numbered identity."""

import platform
import subprocess
import sys

import pytest

from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_exec import link_executable
from pcc.backend.native_object import NativeObject
from pcc.backend.self_backend_dispatch import emit_self_asm


@pytest.mark.pcc_gate(unavailable=(
    None if sys.platform == "darwin" and platform.machine() == "arm64"
    else "requires native Darwin arm64 execution"
))
@pytest.mark.parametrize("source", [
    "define i32 @main() {\n  ret i32 42\n}\n",
    "define i32 @main() {\n  br label %entry\nentry:\n"
    "  %value = phi i32 [42, %0]\n  ret i32 %value\n}\n",
    "define i32 @add(i32 %0) {\n  %2 = add i32 %0, 1\n  ret i32 %2\n}\n"
    "define i32 @main() {\n  %value = call i32 @add(i32 41)\n"
    "  ret i32 %value\n}\n",
])
def test_implicit_entry_compiles_and_executes(tmp_path, source):
    ir = 'target triple = "arm64-apple-darwin"\n' + source
    sections, undefined = assemble_file(emit_self_asm(ir))
    image = link_executable(
        [NativeObject.from_sections(sections, undefined=undefined)], entry="_main",
    )
    output = tmp_path / "implicit_entry"
    output.write_bytes(image)
    output.chmod(0o755)
    result = subprocess.run([str(output)], capture_output=True, timeout=10)
    assert result.returncode == 42, result.stderr
