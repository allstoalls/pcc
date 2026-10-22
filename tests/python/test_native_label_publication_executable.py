"""Execute the production label builder with an explicit immutable runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


_MODULES = (
    "pcc.backend.arm64_asm_driver",
    "pcc.backend.arm64_encode",
    "pcc.backend.aarch64_fp_immediates",
    "pcc.backend.macho_obj",
    "pcc.backend.macho_spec",
    "pcc.backend.precise_stackmap",
    "pcc.backend.self_backend_value_arena",
    "pcc.unsafe",
)

_CANARY = '''
import sys
from pcc.backend.arm64_asm_driver import AArch64ModuleBuilder
from pcc.backend.arm64_encode import EncodeError, encode_emitted_nop_parts

def run(count: int) -> None:
    builder = AArch64ModuleBuilder()
    try:
        builder.append_chunk(".section __TEXT,__text,regular,pure_instructions")
        index = 0
        while index < count:
            builder.append_label("Litem" + str(index))
            builder.append_encoded(encode_emitted_nop_parts(), 0, -1)
            index += 1
        offsets = builder.text_label_offsets()
        assert len(offsets) == count
        assert offsets["Litem0"] == 0
        assert offsets["Litem" + str(count - 1)] == (count - 1) * 4
        builder.append_chunk(".section __DATA,__data")
        builder.append_label("_data_begin")
        builder.append_chunk(".quad 7")
        builder.append_label("_data_end")
        sections, undefined = builder.finish()
        assert len(undefined) == 0
        assert len(sections) == 2
        assert len(sections[0].data) == count * 4
        assert sections[1].symbols[0].offset == 0
        assert sections[1].symbols[1].offset == 8
        print(count * 4)
    finally:
        builder.close()
    invalid = AArch64ModuleBuilder()
    try:
        invalid.append_chunk(".section __TEXT,__text,regular,pure_instructions")
        try:
            invalid.append_label("Lbad:\\n  nop")
        except EncodeError:
            assert invalid.closed
            print("invalid-label-rejected")
        else:
            raise AssertionError("label parsed as instructions")
    finally:
        invalid.close()
    print("native-labels-ok")

run(int(sys.argv[1]))
'''.lstrip()


# requires explicit immutable PCC_RUNTIME_ARCHIVE
@pytest.mark.pcc_gate(env="PCC_RUNTIME_ARCHIVE")
def test_native_label_publication_preserves_offsets_and_errors(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    archive_name = os.environ.get("PCC_RUNTIME_ARCHIVE")
    archive = Path(archive_name).resolve()
    assert archive.is_file()
    repo = Path(__file__).resolve().parents[2]
    source_root = Path(os.environ.get("PCC_NATIVE_LABEL_SOURCE_ROOT", str(repo))).resolve()
    sources = [source_root / (name.replace(".", "/") + ".py") for name in _MODULES]
    sources[-1] = source_root / "pcc/unsafe/__init__.py"
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    runtime_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    consumer = tmp_path / "label_canary.py"
    consumer.write_text(_CANARY, encoding="utf-8")
    output = tmp_path / "label_canary"
    compile_python_multi(
        [str(consumer), *(str(path) for path in sources)], str(output),
        entry_module="pcc.backend.label_canary",
        module_names=["pcc.backend.label_canary", *_MODULES],
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
        recursive_stdlib=False, target_triple="arm64-apple-darwin23.6.0",
        runtime_archive=str(archive),
    )
    expected = "256\ninvalid-label-rejected\nnative-labels-ok\n"
    result = subprocess.run([str(output), "64"], capture_output=True, text=True, timeout=10)
    (tmp_path / "label_canary.stdout").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "label_canary.stderr").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected
    assert hashes == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == runtime_hash
    receipt = {
        "status": "COMPLETE", "compiler_mode": "host-pcc/self/no-libpython",
        "sources": hashes, "runtime_sha256": runtime_hash,
        "binary": str(output), "binary_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "consumer_sha256": hashlib.sha256(consumer.read_bytes()).hexdigest(),
        "stdout": result.stdout,
    }
    (tmp_path / "label_canary.receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"native label receipt: {tmp_path / 'label_canary.receipt.json'}")
