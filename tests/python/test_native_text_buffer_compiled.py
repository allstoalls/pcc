"""Receipt-selected native pcc1 replay of the changed text-buffer implementation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import textwrap

import pytest

from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.native_object import encode_native_object_from_sections
from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_module
from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
from pcc.backend.self_backend_parse import parse_self_backend_module


@pytest.mark.integration
# requires an explicitly receipt-selected pcc1
@pytest.mark.pcc_gate(env="PCC_INDEXED_EMIT_TEST_COMPILER")
def test_receipt_pcc1_executes_current_native_text_buffer(tmp_path, monkeypatch):
    compiler_name = os.environ.get("PCC_INDEXED_EMIT_TEST_COMPILER")
    compiler = Path(compiler_name).resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    source_manifest = json.loads((compiler.parent / "source-manifest.json").read_text())
    for relative in (
        "pcc/backend/arm64_encode.py",
        "pcc/backend/arm64_asm_driver.py",
        "pcc/backend/self_backend_aarch64_darwin.py",
        "pcc/backend/self_backend_precise_stackmaps.py",
        "pcc/backend/self_backend_target_passes.py",
        "pcc/backend/self_backend_indexed_emit.py",
        "pcc/frontends/python/pipeline_frontend_worker_execution.py",
    ):
        assert source_manifest["files"][relative] == hashlib.sha256(
            (repo / relative).read_bytes()
        ).hexdigest(), "compiler is stale for " + relative

    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")

    def build():
        # This canary owns the indexed worker, not the frontend capture API.
        # Full IR retains fence and terminator text together before freezing.
        return parse_self_backend_module(textwrap.dedent("""
            target triple = "arm64-apple-darwin23.6.0"
            @counter = global i64 41
            define i64 @probe(i64 %arg) {
            entry:
              fence seq_cst
              %loaded = load i64, ptr @counter
              %result = add i64 %loaded, %arg
              %condition = icmp sgt i64 %result, 0
              br i1 %condition, label %yes, label %no
            yes:
              ret i64 %result
            no:
              ret i64 0
            }
        """).strip())

    expected_asm = emit_aarch64_darwin_indexed_module(build(), optimize=False)
    sections, undefined = assemble_file(expected_asm)
    expected_pco = encode_native_object_from_sections(sections, undefined=undefined)
    indexed_path = tmp_path / "native_text_buffer.pidx"
    encode_indexed_module_file(str(indexed_path), build())
    for mode, expected in (("ASM", expected_asm.encode()), ("PCO", expected_pco)):
        output = tmp_path / ("native_text_buffer." + mode.lower())
        result = subprocess.run(
            [str(compiler), "--pcc-self-backend-indexed-emit-worker",
             str(indexed_path), str(output), mode],
            capture_output=True, text=True, timeout=30,
        )
        (tmp_path / (mode + ".stdout")).write_text(result.stdout)
        (tmp_path / (mode + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert output.read_bytes() == expected
