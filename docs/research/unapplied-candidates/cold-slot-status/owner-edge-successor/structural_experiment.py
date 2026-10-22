"""Coordinator-only, host IR-shape experiment; no runtime/object/native build.

Use an ordinary materialized candidate source, never an import overlay. The
baseline class restores the byte-reviewed old method only. Both arms lower the
same small standalone function through normal parse, inference and L1 codegen.
This establishes an emitted mechanism, not heavy-module/Stage1 speedup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    source = arguments.source.resolve(strict=True)
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    packet = Path(__file__).resolve().parent
    manifest = json.loads((packet / "manifest.json").read_text())
    for entry in manifest["changed_files"]:
        assert digest(source / entry["path"]) == entry["post_sha256"], entry["path"]
    fixture = packet / "structural_input.py"
    assert digest(fixture) == manifest["structural_input_sha256"]
    for key in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT"):
        os.environ[key] = "0"
    os.environ["PCC_SELF_TARGET_PASSES"] = "off"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))

    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.compiled_default_passes import _has_py_cpy_call
    from pcc.frontends.python.compiled_owned_passes import run_owned_passes
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.ir.optimization.ir_mutator import MutableModule
    from tests.owned_ir_validation import verify_ir_text
    from tests.python.test_slot_call_status_reporting import _InlineReference

    for module_name in ("pcc", "pcc.frontends.python.codegen.layer1", "tests"):
        location = Path(sys.modules[module_name].__file__).resolve()
        assert location.is_relative_to(source), (module_name, str(location))

    def measure(text):
        parsed = MutableModule.parse(text)
        call_names = []
        for function in parsed.functions:
            for block in function.blocks:
                for instruction in block.instructions:
                    if instruction.opcode == "call":
                        match = re.search(r"@([\w.$-]+)\(", instruction.text)
                        if match:
                            call_names.append(match.group(1))
        return {
            "functions": len(parsed.functions),
            "blocks": sum(len(function.blocks) for function in parsed.functions),
            "instructions": sum(len(block.instructions) for function in parsed.functions
                                for block in function.blocks),
            "slot_failure_blocks": sum(block.name.startswith("call.slot.error")
                                       for function in parsed.functions for block in function.blocks),
            "object_slot_calls": call_names.count("py_obj_call_slots"),
            "exception_alloc_calls": call_names.count("py_exc_new"),
            "borrowed_raise_calls": call_names.count("py_raise"),
            "text_bytes": len(text.encode()),
        }

    result = {
        "status": "RUNNING",
        "scope": "host small-program emitted-IR mechanism; no native or Stage1 timing claim",
        "source": str(source),
        "source_base_commit": manifest["base_commit"],
        "packet_manifest_sha256": digest(packet / "manifest.json"),
        "input_sha256": digest(fixture),
        "passes": ["instsimplify", "simplifycfg", "inline", "inline-defined", "dce"],
        "arms": {},
    }
    result_path = output / "result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    for label, cls in (("baseline", _InlineReference), ("candidate", L1CodeGen)):
        module = infer_module(parse_and_lift(fixture.read_text(), str(fixture), "cold_status_shape"))
        codegen = cls(module, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        raw = str(codegen.generate(module))
        verify_ir_text(raw)
        assert not _has_py_cpy_call(raw), "Owned pass dispatch would skip this input"
        processed = run_owned_passes(raw, result["passes"], True)
        verify_ir_text(processed)
        (output / (label + ".ll")).write_text(raw)
        (output / (label + ".postpasses.ll")).write_text(processed)
        row = {"raw": measure(raw), "postpasses": measure(processed),
               "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
               "postpasses_sha256": hashlib.sha256(processed.encode()).hexdigest()}
        if label == "candidate":
            helper = codegen.runtime["__pcc_slot_call_status_report"]
            row["reporter"] = helper.name
            parsed = MutableModule.parse(processed)
            definition = parsed.function(helper.name)
            assert definition is not None and "noinline" in definition.header_line
            calls = [instruction for function in parsed.functions for block in function.blocks
                     for instruction in block.instructions if instruction.opcode == "call"
                     and re.search(r"@" + re.escape(helper.name) + r"\(", instruction.text)]
            row["postpasses_reporter_calls"] = len(calls)
            assert len(calls) == row["postpasses"]["slot_failure_blocks"] > 0
        result["arms"][label] = row
        result_path.write_text(json.dumps(result, indent=2) + "\n")
    for phase in ("raw", "postpasses"):
        baseline = result["arms"]["baseline"][phase]
        candidate = result["arms"]["candidate"][phase]
        assert baseline["object_slot_calls"] == candidate["object_slot_calls"] == 8
        assert baseline["slot_failure_blocks"] == candidate["slot_failure_blocks"] > 0
        assert candidate["instructions"] < baseline["instructions"]
        assert candidate["blocks"] < baseline["blocks"]
    for entry in manifest["changed_files"]:
        assert digest(source / entry["path"]) == entry["post_sha256"], entry["path"]
    result["status"] = "PASS"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
