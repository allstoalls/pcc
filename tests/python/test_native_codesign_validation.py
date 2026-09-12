"""The compiled signature verifier must validate and reject real images."""

import os
from pathlib import Path
import subprocess

from pcc.backend import macho_codesign
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_exec import link_executable
from pcc.backend.native_object import NativeObject
from pcc.py_frontend.pipeline import compile_python_multi


def test_compiled_signature_validator_checks_padding_and_page_hashes(
    tmp_path, pcc_py_runtime_archive,
):
    sections, undefined = assemble_file(
        ".section __TEXT,__text,regular,pure_instructions\n.globl _main\n"
        "_main:\n movz w0, #42\n ret\n"
    )
    target = tmp_path / "target"
    image = link_executable([NativeObject.from_sections(sections, undefined=undefined)])
    target.write_bytes(image)
    target.chmod(0o755)
    assert subprocess.run([str(target)], timeout=10).returncode == 42
    source = tmp_path / "validate_signature.py"
    source.write_text('''
import sys
from pcc.backend.macho_codesign import parse_signature, build_signature, CodesignError
def main():
    with open(sys.argv[1], "rb") as stream:
        data = stream.read()
    signature = parse_signature(data)
    expected = build_signature(data[:signature.dataoff], identifier=signature.identifier,
        exec_seg_base=signature.exec_seg_base, exec_seg_limit=signature.exec_seg_limit,
        exec_seg_flags=signature.exec_seg_flags)
    print(data[signature.dataoff:] == expected)
    bad = bytearray(data)
    bad[-1] = 1
    try:
        parse_signature(bytes(bad))
    except CodesignError:
        print("padding rejected")
main()
''', encoding="utf-8")
    output = tmp_path / "validate_signature"
    compile_python_multi(
        [str(Path(macho_codesign.__file__)), str(source)], str(output),
        module_names=["pcc.backend.macho_codesign", "validate_signature"],
        entry_module="validate_signature", recursive_stdlib=True,
        backend="self", libpython_mode="off", runtime_archive=str(pcc_py_runtime_archive),
    )
    for gc in range(5):
        result = subprocess.run([str(output), str(target)], capture_output=True, text=True,
                                timeout=20, env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == "True\npadding rejected\n", f"GC{gc}: {result.stdout}"
